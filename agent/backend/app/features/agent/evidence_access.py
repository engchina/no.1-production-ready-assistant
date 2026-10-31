"""保存した根拠を出し直すときに、見る人の今の権限で確かめ直す（#3252）。

Run・会話の詳細（実行履歴・チャットの会話）は、Run が RAG から受け取った根拠（ツールの結果・
イベント・成果物「検索の根拠」「回答の根拠」）を保存している。画面・API が出すたびに、画面を見て
いる利用者として RAG に根拠の文書を見られるかを確かめ（MCP の内部のツール
``rag_readable_documents``。``sub`` は見る人。図・原文の URL と同じく Run の利用者の権限は借り
ない）、今見られない文書の根拠は抜粋・場所・文書名を出さず、件数（``withheld_evidence_count``）
だけを返す（OWASP API1: 物を読むたびにその時点の権限で認可する）。

- 確かめられないとき（RAG の接続が無い・失敗した）は、RAG の根拠をすべて見られないものとして扱う
  （fail closed。ログ ``agent_evidence_access_check_failed``）。
- 会話の添付の根拠（``document_id`` が ``attachment:``）は RAG の文書ではないので確かめない。
- 回答の本文は保存した文のまま返す（その資料から作った文で、本文から該当の部分は消せない。既知の
  限界。docs/security-rbac.md「保存した根拠の再表示」）。
- 保存した Run は変えない（応答に使う写しだけを変える）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from pr_backend_core.chat_progress import ChatProgressStepDetails
from pr_backend_core.observability.call_details import withhold_call_details

from app.features.agent.answer_evidence import (
    ANSWER_EVIDENCE_KIND,
    ATTACHMENT_DOCUMENT_PREFIX,
    passed_evidence,
)
from app.features.agent.answer_validation_view import project_validation_artifacts
from app.features.agent.rag_figures import (
    EVIDENCE_ARTIFACT_KINDS,
    RAG_CONNECTION_ID,
    RunFigureUrlError,
    _rag_service_client,
)
from app.features.agent.runtime import Artifact, RunEvent, RunState, RunStep
from app.features.agent.tools import MCP_TOOL_SEPARATOR, ExternalToolError

logger = logging.getLogger(__name__)

READABLE_DOCUMENTS_TOOL = "rag_readable_documents"
# 画面の操作の印（RAG は Run の中のモデルの呼び出しと分けて記録する）。
EVIDENCE_ACCESS_PURPOSE = "evidence_access"
# 1 回の呼び出しで渡す文書の数（RAG の `READABLE_DOCUMENTS_MAX`）。
READABLE_DOCUMENTS_BATCH = 200
# 見られない文書を 1 つだけ読んだツールの結果の代わりに出す値。
WITHHELD_OUTPUT: dict[str, Any] = {"evidence_withheld": True}
_RAG_TOOL_PREFIX = f"{RAG_CONNECTION_ID}{MCP_TOOL_SEPARATOR}"


def _document_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    document_id = value.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        return None
    return None if document_id.startswith(ATTACHMENT_DOCUMENT_PREFIX) else document_id


def _document_ids(value: object) -> set[str]:
    found: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            document_id = _document_id(node)
            if document_id is not None:
                found.add(document_id)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _prune(value: Any, unreadable: frozenset[str]) -> Any:
    """見られない文書を指す要素（``document_id`` を持つ dict）を除く（一覧は要素、dict は項目）。"""

    def hidden(node: object) -> bool:
        return _document_id(node) in unreadable

    if isinstance(value, Mapping):
        return {key: _prune(item, unreadable) for key, item in value.items() if not hidden(item)}
    if isinstance(value, list):
        return [_prune(item, unreadable) for item in value if not hidden(item)]
    return value


def _prune_output(output: Any, unreadable: frozenset[str]) -> Any:
    """ツールの結果。見られない文書 1 つを読んだ結果（``rag_read_source`` など）は丸ごと伏せる。"""
    if _document_id(output) in unreadable:
        return dict(WITHHELD_OUTPUT)
    return _prune(output, unreadable)


def _is_rag_step(step: RunStep) -> bool:
    return step.tool_call is not None and step.tool_call.name.startswith(_RAG_TOOL_PREFIX)


def rag_document_ids(runs: Iterable[RunState], artifacts: Iterable[Artifact] = ()) -> set[str]:
    """Run の RAG の根拠の文書（RAG のツールの結果・画面の根拠の一覧・根拠の成果物）。

    ``artifacts`` は中身を読み込んだ成果物（Run の成果物の中身が別の保存先にあるとき）。確かめる
    文書はここで集めた RAG の文書だけにし、除くとき（``_prune``）もその文書を指す要素だけを除く
    （NL2SQL の表の結果などの ``document_id`` の列を RAG の文書として扱わない）。
    """
    found: set[str] = set()
    for run in runs:
        for step in run.steps:
            if _is_rag_step(step) and step.tool_result is not None:
                found |= _document_ids(step.tool_result.output)
        found |= _document_ids(passed_evidence(run.steps))
        found |= _document_ids(
            [item.content for item in run.artifacts if item.kind in EVIDENCE_ARTIFACT_KINDS]
        )
    found |= _document_ids(
        [item.content for item in artifacts if item.kind in EVIDENCE_ARTIFACT_KINDS]
    )
    return found


def unreadable_documents(
    run: RunState, viewer_user_uuid: str, document_ids: set[str]
) -> frozenset[str]:
    """``document_ids`` のうち、見る人が今見られない文書（同期。thread で呼ぶ）。"""
    if not document_ids:
        return frozenset()
    try:
        client, trace_id = _rag_service_client(run, viewer_user_uuid, EVIDENCE_ACCESS_PURPOSE)
    except RunFigureUrlError:
        logger.warning(
            "agent_evidence_access_check_failed",
            extra={"run_id": run.id, "error_code": "rag_connection_unavailable"},
        )
        return frozenset(document_ids)
    ordered = sorted(document_ids)
    readable: set[str] = set()
    for start in range(0, len(ordered), READABLE_DOCUMENTS_BATCH):
        batch = ordered[start : start + READABLE_DOCUMENTS_BATCH]
        try:
            output = client.call_tool(
                READABLE_DOCUMENTS_TOOL,
                {"document_ids": batch},
                idempotent=True,
                trace_id=trace_id,
            )
        except ExternalToolError as exc:
            logger.warning(
                "agent_evidence_access_check_failed",
                extra={"run_id": run.id, "error_code": exc.details.get("error_code") or exc.code},
            )
            return frozenset(document_ids)
        values = output.get("readable_document_ids")
        readable |= (
            {item for item in values if isinstance(item, str)}
            if isinstance(values, list)
            else set()
        )
    return frozenset(document_ids - readable)


def _withhold_answer_evidence(content: Mapping[str, Any], unreadable: frozenset[str]) -> Any:
    """成果物「回答の根拠」: 見られない根拠を除き、その根拠の原文（参考情報）も除く。"""
    items = content.get("items")
    hidden_chunks = {
        str(item.get("chunk_id"))
        for item in (items if isinstance(items, list) else [])
        if _document_id(item) in unreadable
    }
    pruned = _prune(content, unreadable)

    def visible_chunks(value: object) -> list[str]:
        return (
            [item for item in value if item not in hidden_chunks] if isinstance(value, list) else []
        )

    for key in ("markers", "passages"):
        entries = pruned.get(key)
        if isinstance(entries, list):
            pruned[key] = [
                {**entry, "chunk_ids": visible_chunks(entry.get("chunk_ids"))}
                if isinstance(entry, Mapping)
                else entry
                for entry in entries
            ]
    references = pruned.get("references")
    if isinstance(references, list):
        # 参考情報の quote は根拠の原文なので、根拠が見られなければ出さない。
        pruned["references"] = [
            {**entry, "chunk_ids": visible_chunks(entry.get("chunk_ids"))}
            for entry in references
            if isinstance(entry, Mapping) and visible_chunks(entry.get("chunk_ids"))
        ]
    kept = pruned.get("items")
    if isinstance(kept, list):
        pruned["passed"] = len(kept)
        pruned["cited"] = sum(
            1 for item in kept if isinstance(item, Mapping) and item.get("cited") is True
        )
    return pruned


def _withhold_artifact(artifact: Artifact, unreadable: frozenset[str]) -> Artifact:
    if artifact.kind == ANSWER_EVIDENCE_KIND:
        content = _withhold_answer_evidence(artifact.content, unreadable)
    else:
        content = _prune(artifact.content, unreadable)
    return artifact.model_copy(update={"content": content})


def _withhold_step(step: RunStep, unreadable: frozenset[str]) -> RunStep:
    result = step.tool_result
    if result is None or result.output is None:
        return step
    return step.model_copy(
        update={
            "tool_result": result.model_copy(
                update={"output": _prune_output(result.output, unreadable)}
            )
        }
    )


def _withhold_event(event: RunEvent, unreadable: frozenset[str]) -> RunEvent:
    payload = dict(event.payload)
    if "output" in payload:
        payload["output"] = _prune_output(payload["output"], unreadable)
    return event.model_copy(update={"payload": _prune(payload, unreadable)})


def withhold_events(
    events: Iterable[RunEvent | None], unreadable: frozenset[str]
) -> Iterator[RunEvent | None]:
    """イベント（ツールの結果を持つ ``tool.completed`` など）から見られない根拠を除く。

    ``None``（購読の keepalive）はそのまま流す。
    """
    for event in events:
        yield _withhold_event(event, unreadable) if unreadable and event is not None else event


def withheld_count(
    run: RunState, unreadable: frozenset[str], artifacts: Sequence[Artifact] = ()
) -> int:
    """画面の根拠の一覧（成果物「回答の根拠」。無ければ AI に渡した根拠）のうち、見られない件数。"""
    if not unreadable:
        return 0
    loaded = {item.id: item for item in artifacts}
    answer = [
        loaded.get(item.id, item) for item in run.artifacts if item.kind == ANSWER_EVIDENCE_KIND
    ]
    items: Any = answer[-1].content.get("items") if answer else passed_evidence(run.steps)
    return sum(1 for item in items if isinstance(items, list) and _document_id(item) in unreadable)


def withhold_run(run: RunState, unreadable: frozenset[str]) -> RunState:
    """見る人が今見られない根拠を除いた Run の写し（保存した Run は変えない）。"""
    if not unreadable:
        return run.model_copy(update={"artifacts": project_validation_artifacts(run.artifacts)})
    return run.model_copy(
        update={
            "span_details": {
                key: ChatProgressStepDetails.model_validate(
                    withhold_call_details(
                        value.model_dump(by_alias=True), allow_restricted=False, all_calls=True
                    )
                )
                for key, value in run.span_details.items()
            },
            "steps": [_withhold_step(step, unreadable) for step in run.steps],
            "events": [_withhold_event(event, unreadable) for event in run.events],
            "artifacts": project_validation_artifacts(
                [_withhold_artifact(item, unreadable) for item in run.artifacts],
                allow_history_notice=False,
            ),
        }
    )


def withhold_artifacts(artifacts: Iterable[Artifact], unreadable: frozenset[str]) -> list[Artifact]:
    """成果物の一覧・1 件の応答から見られない根拠を除く。"""
    return project_validation_artifacts(
        [_withhold_artifact(item, unreadable) if unreadable else item for item in artifacts],
        allow_history_notice=not bool(unreadable),
    )
