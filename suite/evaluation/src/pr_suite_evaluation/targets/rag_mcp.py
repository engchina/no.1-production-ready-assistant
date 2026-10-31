"""Target: RAG の検索・回答を公開の RAG MCP（`rag_search`）で呼ぶ adapter（#3417）。

業務 Agent の Target（`agent_mcp`）と同じ Dataset・Observation・Evaluator・Report で、RAG の
A（業務ガイドなし。ナレッジベースを指定）と C（業務ガイドあり。検索・回答プロファイルを指定）を
採点する（1 つの採点器を複数の Target に当てる）。

- 呼ぶのは公開の MCP（`POST /api/mcp`。契約は `platform/contracts/mcp/rag-tools.json`）の
  `rag_search` だけで、利用者が発行した API キー（使える製品に `rag`）で呼ぶ。RAG の画面の
  API・DB・コードは読まない。
- ケースの既知の条件（`conditions`）は `rag_search` の `conditions` に構造化して渡す。複数往復の
  ケース（`turns`）は、`rag_search` が会話を持たないので、最初の質問とそれまでの返答をつないだ
  質問と、返答までに分かった条件で呼び直す（契約の「clarifications に答えた値を conditions に
  入れて呼び直す」）。
- 旧版も検索して答えるケース（`include_superseded`）は `include_superseded: true` を渡す。
- 引用は `rag_search` の `evidence[]`（AI に渡した資料の箇所。返った順。`evidence_limit` は既定で
  契約の上限 50）。本文は `excerpt`（`text_omitted` は本文を省いた根拠）、場所は `locator`。

観測（`CaseObservation`）には `rag_search` の出力をそのまま保存し、採点の入力への変換
（`rag_case_view`）は採点のときに行う。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pr_evaluation_scoring import OUTCOMES

from pr_suite_evaluation.dataset import Case
from pr_suite_evaluation.mcp_client import McpClient, TargetError
from pr_suite_evaluation.observation import CaseObservation, TurnObservation
from pr_suite_evaluation.views import CaseFailure, CaseView, Citation, TurnView

TARGET_KIND = "rag_mcp"
# RAG の経路の規則の版（rag_v1: 公開の rag_search の出力だけで採点する最初の版。#3417）。
RULES_VERSION = "rag_v1"
TOOL = "rag_search"
# 読める出力の版（契約 rag-tools.json の rag_search の schema_version。#3240 で
# rag_validate_answer の段落の位置・top_k の既定・失敗の code の一覧を足して 37（rag_search の
# 出力の形は 36 と同じ）、#3546 で安全チェックの未完了 guardrail_degraded と根拠 0 件の段
# zero_evidence を足して 38）。
RAG_SEARCH_SCHEMA_VERSION = 38
# 返す根拠の上限（契約の evidence_limit の maximum）。
DEFAULT_EVIDENCE_LIMIT = 50
MAX_EVIDENCE_LIMIT = 50
# rag_search は回答まで同期で返す（1 回 50〜110 秒）。HTTP の timeout はこれより長くする。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 600.0
MAX_CONDITIONS = 30
OUTCOME_SOURCE_ANSWER = "rag_answer"


class RagEvaluationError(TargetError):
    """RAG の公開の MCP を呼べない・出力を読めない。"""

    error_type = "RagEvaluationError"


def _records(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


@dataclass(frozen=True)
class RagTargetConfig:
    """対象構成（結果に記録し、hash を取る。資格情報は持たない）。

    A はナレッジベース（`knowledge_base_ids`）、C
    は検索・回答プロファイル（`search_answer_profile_id`）
    を指定する。どちらか一方は必須（両方を渡すと、プロファイルの参照先の中のナレッジベースに絞る）。
    """

    mcp_url: str
    knowledge_base_ids: tuple[str, ...] = ()
    search_answer_profile_id: str | None = None
    top_k: int | None = None
    evidence_limit: int = DEFAULT_EVIDENCE_LIMIT
    filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.knowledge_base_ids and not self.search_answer_profile_id:
            raise ValueError(
                "RAG の Target には --knowledge-base-id か --search-answer-profile-id を"
                "渡してください。"
            )
        if not 1 <= self.evidence_limit <= MAX_EVIDENCE_LIMIT:
            raise ValueError(f"evidence_limit は 1〜{MAX_EVIDENCE_LIMIT} にしてください。")

    def provenance(self) -> dict[str, Any]:
        return {
            "kind": TARGET_KIND,
            "mcp_url": self.mcp_url,
            "knowledge_base_ids": list(self.knowledge_base_ids),
            "search_answer_profile_id": self.search_answer_profile_id,
            "top_k": self.top_k,
            "evidence_limit": self.evidence_limit,
            "filters": dict(sorted(self.filters.items())),
            "output_schema_versions": {TOOL: RAG_SEARCH_SCHEMA_VERSION},
        }


def case_requests(case: Case) -> list[tuple[str, dict[str, str]]]:
    """往復ごとの（質問, 条件）。返答はそれまでの質問の後ろにつなぎ、条件は足していく。"""
    conditions = dict(case.conditions)
    texts = [case.query]
    requests = [(case.query, dict(conditions))]
    for turn in case.turns:
        conditions = {**conditions, **turn.conditions}
        texts.append(turn.reply)
        requests.append(("\n".join(texts), dict(conditions)))
    return requests


class RagMcpTarget:
    """評価のケースを公開の RAG MCP の `rag_search` で流し、出力を観測として返す。

    `clock` は往復の時間を測る時計（テストで差し替える）。
    """

    kind = TARGET_KIND

    def __init__(
        self,
        mcp: McpClient,
        config: RagTargetConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mcp = mcp
        self._config = config
        self._clock = clock

    def _arguments(self, case: Case, query: str, conditions: Mapping[str, str]) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": query, "evidence_limit": self._config.evidence_limit}
        if query != case.query:
            arguments["original_question"] = query
        if self._config.search_answer_profile_id:
            arguments["search_answer_profile_id"] = self._config.search_answer_profile_id
        if self._config.knowledge_base_ids:
            arguments["knowledge_base_ids"] = list(self._config.knowledge_base_ids)
        if self._config.top_k is not None:
            arguments["top_k"] = self._config.top_k
        if self._config.filters:
            arguments["filters"] = dict(self._config.filters)
        if case.include_superseded:
            arguments["include_superseded"] = True
        if conditions:
            arguments["conditions"] = dict(list(conditions.items())[:MAX_CONDITIONS])
        return arguments

    def run_case(self, case: Case) -> CaseObservation:
        observation = CaseObservation(case_id=case.id, target_kind=TARGET_KIND)
        for index, (query, conditions) in enumerate(case_requests(case)):
            started = self._clock()
            arguments = self._arguments(case, query, conditions)
            request = {
                "utterance": index,
                "tool": TOOL,
                "conditions": sorted(conditions),
                "include_superseded": case.include_superseded,
            }
            try:
                output = self._mcp.call(TOOL, arguments, schema_version=RAG_SEARCH_SCHEMA_VERSION)
            except (TargetError, httpx.HTTPError) as error:
                error_type = getattr(error, "error_type", None)
                observation.turns.append(
                    TurnObservation(
                        turn=index,
                        status="target_error",
                        error_type=str(error_type or RagEvaluationError.error_type)
                        if isinstance(error, TargetError)
                        else RagEvaluationError.error_type,
                        error_message=str(error)[:300],
                        request=request,
                    )
                )
                break
            elapsed = (self._clock() - started) * 1000
            observation.turns.append(
                TurnObservation(
                    turn=index,
                    status="completed",
                    target_status=str(output.get("outcome") or ""),
                    elapsed_ms=elapsed,
                    refs={"trace_id": str(output.get("trace_id") or "") or None},
                    output_versions={TOOL: RAG_SEARCH_SCHEMA_VERSION},
                    request=request,
                    payload={TOOL: output},
                )
            )
        return observation


# ---- 観測 → 採点の入力 --------------------------------------------------------------------


def evidence_citations(output: Mapping[str, Any]) -> tuple[Citation, ...]:
    """`rag_search` の根拠（返った順・重複なし）。文書と chunk の ID の無い根拠は数えない。"""
    citations: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for item in _records(output.get("evidence")):
        document_id = str(item.get("document_id") or "")
        chunk_id = str(item.get("chunk_id") or item.get("evidence_id") or "")
        if not document_id or not chunk_id or (document_id, chunk_id) in seen:
            continue
        seen.add((document_id, chunk_id))
        locator = item.get("locator")
        citations.append(
            Citation(
                document_id=document_id,
                chunk_id=chunk_id,
                text=str(item.get("excerpt") or ""),
                text_omitted=item.get("text_omitted") is True,
                locator=(
                    {key: value for key, value in locator.items() if value is not None}
                    if isinstance(locator, Mapping)
                    else {}
                ),
            )
        )
    return tuple(citations)


def _turn_view(turn: TurnObservation) -> TurnView:
    output = turn.payload.get(TOOL) or {}
    outcome = output.get("outcome")
    known = outcome if isinstance(outcome, str) and outcome in OUTCOMES else None
    return TurnView(
        turn=turn.turn,
        trace_id=str(turn.refs.get("trace_id") or ""),
        elapsed_ms=turn.elapsed_ms,
        outcome=known,
        outcome_source=OUTCOME_SOURCE_ANSWER if known else None,
        citations=evidence_citations(output),
        answer=str(output.get("answer") or ""),
    )


def rag_case_view(observation: CaseObservation, **_options: Any) -> CaseView:
    """保存した観測を採点の入力にする（規則は RULES_VERSION）。

    呼べなかった往復があれば、そこで止めてケースの失敗にする（それまでの往復は採点しない）。
    """
    views: list[TurnView] = []
    failure: CaseFailure | None = None
    for turn in observation.turns:
        if turn.status == "completed":
            views.append(_turn_view(turn))
            continue
        failure = CaseFailure(
            error_type=turn.error_type or RagEvaluationError.error_type,
            error_message=turn.error_message or "",
            error_stage=f"turn_{turn.turn}",
            trace_id=str(turn.refs.get("trace_id") or ""),
        )
        break
    if failure is None and not views:
        failure = CaseFailure(
            error_type=RagEvaluationError.error_type,
            error_message="観測した往復がありません。",
            error_stage=None,
            trace_id="",
        )
    return CaseView(
        case_id=observation.case_id,
        turns=tuple(views),
        failure=failure,
        elapsed_ms=round(sum(turn.elapsed_ms for turn in observation.turns), 3),
    )


def target_summary(
    cases: Sequence[Case], observations: Sequence[CaseObservation], **_options: Any
) -> dict[str, Any]:
    """RAG の検索・回答の診断（往復ごとの対応・根拠の数・省いた根拠の数。本文は含めない）。"""
    runs: list[dict[str, Any]] = []
    sources: dict[str, int] = {}
    for case in observations:
        for turn in case.turns:
            output = turn.payload.get(TOOL) or {}
            view = _turn_view(turn) if turn.status == "completed" else None
            if view and view.outcome_source:
                sources[view.outcome_source] = sources.get(view.outcome_source, 0) + 1
            runs.append(
                {
                    "case_id": case.case_id,
                    "turn": turn.turn,
                    "trace_id": turn.refs.get("trace_id"),
                    "status": turn.status,
                    "elapsed_ms": round(turn.elapsed_ms, 1),
                    "outcome": view.outcome if view else None,
                    "evidence_count": len(_records(output.get("evidence"))),
                    "evidence_omitted": output.get("evidence_omitted"),
                    "evidence_text_omitted": output.get("evidence_text_omitted"),
                    "clarification_conditions": [
                        str(item.get("condition_id"))
                        for item in _records(output.get("clarifications"))
                        if item.get("condition_id")
                    ],
                }
            )
    return {"outcome_sources": sources, "runs": runs}
