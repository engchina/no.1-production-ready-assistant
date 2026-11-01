"""公開の Agent MCP の観測 `agent_get_run_observation`（#3240 の段 2 の PR 4）。

Run は合成の記録を保存先に直接置き、RAG の MCP は契約どおりの fake（``mcp_support``）にする
（組み込み Runtime・モデルの API は呼ばない）。認可の行列（主体 × 範囲）は
``test_evidence_read_authz``。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from mcp_support import FakeProductMcp, fake_product_mcp
from pr_backend_core.source_text import SOURCE_TEXT_PAGE_BUDGET_TOKENS
from pr_system_settings.auth.api_keys import api_key_rate_limiter
from pytest import MonkeyPatch
from security_support import client, enable_production_auth, login

from app.features.agent import mcp_server, run_observation
from app.features.agent.runtime import (
    MCP_APPROVAL_SKIPPED_CODE,
    AgentProfile,
    Artifact,
    RunEvent,
    RunEventType,
    RunState,
    RunStatus,
    RunStep,
    RunUsage,
    StepStatus,
    runtime_repository,
)
from app.features.agent.tools import ToolCall, ToolResult, mcp_function_name
from app.security.permissions import RUNS_OPERATE, RUNS_VIEW
from app.security.service import set_security_service

# 出してはいけない値の目印（ツールの引数・NL2SQL の SQL / 行 / 列 / profile_id・エラーの文・
# outcome の basis / signals・Run の metadata・スコア・bbox）。
CANARIES = {
    "argument": "canary-argument-3240",
    "sql": "canary-sql-3240",
    "row": "canary-row-3240",
    "column": "canary_column_3240",
    "profile": "canary-profile-3240",
    "error": "canary-error-message-3240",
    "error_details": "canary-error-details-3240",
    "basis": "canary-basis-3240",
    "signals": "canary-signals-3240",
    "metadata": "canary-metadata-3240",
    "key_name": "canary-key-name-3240",
    "question": "canary-question-3240",
    "reference": "canary-reference-3240",
}
CANARY_SCORE = 0.987654321
CANARY_BBOX = 123.456789
TRACE_ID = "0123456789abcdef0123456789abcdef"


@dataclass
class Setup:
    agent_id: str
    owner: str
    token: str
    rag: FakeProductMcp


@pytest.fixture
def setup(monkeypatch: MonkeyPatch) -> Iterator[Setup]:
    auth = enable_production_auth(monkeypatch)
    suffix = uuid4().hex[:8]
    agent_id = f"agent3240obs-{suffix}"
    runtime_repository.create_agent(
        AgentProfile(id=agent_id, name="Agent A", instructions="答える。")
    )
    owner = auth.user_with_permissions(
        f"obs-{suffix}", [RUNS_VIEW, RUNS_OPERATE], agent_ids=[agent_id]
    )
    rag = fake_product_mcp(monkeypatch)
    api_key_rate_limiter.reset()
    created = client.post(
        "/api/account/api-keys",
        json={"name": "評価の機械", "products": ["agent"]},
        headers=login(owner.login_user_id),
    )
    assert created.status_code == 200, created.text
    try:
        yield Setup(
            agent_id=agent_id,
            owner=owner.user_uuid,
            token=created.json()["data"]["token"],
            rag=rag,
        )
    finally:
        set_security_service(None)
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == agent_id
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(agent_id)


def _observe(setup: Setup, run_id: str, **arguments: Any) -> dict[str, Any]:
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agent_get_run_observation",
                "arguments": {"run_id": run_id, **arguments},
            },
        },
        headers={"authorization": f"Bearer {setup.token}"},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


def _store(run: RunState) -> RunState:
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - テスト用に Run を直接置く
        repository._runs[run.id] = run  # noqa: SLF001
    return run


T0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def _step(
    run_id: str,
    connection: str,
    tool: str,
    *,
    arguments: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    status: StepStatus = StepStatus.COMPLETED,
    success: bool = True,
    error: str | None = None,
    error_code: str | None = None,
    error_details: dict[str, Any] | None = None,
    minute: int = 0,
) -> RunStep:
    name = mcp_function_name(connection, tool)
    started = T0 + timedelta(minutes=minute)
    result = (
        ToolResult(
            name=name,
            success=success,
            output=output,
            error=error,
            error_code=error_code,
            error_details=error_details or {},
        )
        if status not in {StepStatus.PENDING, StepStatus.RUNNING}
        else None
    )
    return RunStep(
        run_id=run_id,
        status=status,
        tool_call=ToolCall(name=name, arguments=arguments or {}),
        tool_result=result,
        started_at=started,
        completed_at=started + timedelta(seconds=5) if result is not None else None,
    )


def _evidence(document_id: str, chunk_id: str, text: str, **fields: Any) -> dict[str, Any]:
    return {
        "evidence_id": chunk_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "file_name": f"{document_id}.pdf",
        "chunk_set_id": f"set-{document_id}",
        "evidence_type": "text",
        "origin": "source",
        "excerpt": text,
        "text_length": len(text),
        "score": CANARY_SCORE,
        "rerank_score": CANARY_SCORE,
        "locator": {
            "section_path": ["章 1", "節 2"],
            "page_start": 3,
            "page_end": 4,
            "bbox": [CANARY_BBOX, 0, 1, 1],
            "bbox_unit": "absolute",
            "element_locator": f"doc:{document_id}/el:1",
        },
        "references": [{"label": CANARIES["reference"]}],
        **fields,
    }


def _full_run(setup: Setup) -> RunState:
    """観測のすべての種類の記録を持つ、完了した合成の Run。"""
    run_id = f"run3240obs-{uuid4().hex[:8]}"
    search = {
        "outcome": "answered",
        # 縮退した任意の補助の処理（#3546）。形の合わない項目は写さない。
        "degraded_steps": [{"kind": "rerank", "count": 2}, {"kind": "", "count": 1}],
        "guardrail_degraded": False,
        "evidence": [
            _evidence("doc-a", "a:1", "文書 A の合成の本文。", retrieval_rank=1),
            # RAG が 1 件の上限で切った根拠（生成には一部だけを渡した）。
            _evidence(
                "doc-b",
                "b:1",
                "文書 B の前半。",
                retrieval_rank=2,
                truncated=True,
                superseded=True,
                evidence_type="table",
                origin="derived",
                # 表の 1 行の記録は表の対応の検査が読めない（#3546）。
                table_binding="not_checkable",
                # 表の 1 行の根拠のセルの座標（RAG の EvidenceLocator の table_row・table_cells）。
                locator={
                    "sheet_name": "シート 1",
                    "row_basis": "sheet",
                    "table_index": 2,
                    "row_start": 5,
                    "row_end": 5,
                    "cell_range": "A5:C5",
                    "table_row": 5,
                    "table_cells": [
                        {"table_col": "A", "header_path": []},
                        {"table_col": "B", "header_path": ["上の段", "下の段"]},
                    ],
                },
            ),
            # 会話の添付の根拠は観測に含めない。
            _evidence("attachment:att-1", "att:1", "添付の本文。"),
        ],
    }
    read_document = {
        "document_id": "doc-c",
        "file_name": "doc-c.xlsx",
        "chunk_set_id": "set-c",
        "text": "シート の 1 行目。シート の 2 行目。",
        "chunks": [
            {
                "chunk_id": "c:1",
                "start": 0,
                "end": 11,
                "sheet_name": "シート 1",
                "row_start": 1,
                "row_end": 1,
                "cell_range": "A1:C1",
            },
            {"chunk_id": "c:2", "start": 11, "end": 22, "sheet_name": "シート 1", "row_start": 2},
        ],
    }
    # 切られた doc-b を後で全文で読んだ（まだ無い本文をつなぎ、全文を渡したことになる）。
    read_source = {
        "document_id": "doc-b",
        "chunk_id": "b:1",
        "text": "文書 B の前半。文書 B の後半。",
        "offset": 0,
        "text_length": 17,
        "truncated": False,
        "locator": {"section_path": []},
        "parent_text": CANARIES["reference"],
    }
    nl2sql = {
        "job_id": "job-1",
        "status": "done",
        "profile_id": CANARIES["profile"],
        "generated_sql": f"SELECT {CANARIES['column']} FROM t -- {CANARIES['sql']}",
        "executable_sql": CANARIES["sql"],
        "columns": [{"name": CANARIES["column"]}],
        "rows": [[CANARIES["row"]]],
        "returned_count": 1,
        "truncated": False,
        "explanation": CANARIES["sql"],
    }
    steps = [
        _step(
            run_id,
            "rag",
            "rag_search",
            arguments={"query": CANARIES["argument"], "include_superseded": True},
            output=search,
            minute=0,
        ),
        _step(
            run_id,
            "rag",
            "rag_retrieve_evidence",
            arguments={"query": CANARIES["argument"]},
            status=StepStatus.FAILED,
            success=False,
            error=CANARIES["error"],
            error_code="rag.timeout",
            error_details={"detail": CANARIES["error_details"]},
            minute=1,
        ),
        _step(run_id, "rag", "rag_read_document", output=read_document, minute=2),
        _step(run_id, "rag", "rag_read_source", output=read_source, minute=3),
        _step(
            run_id,
            "nl2sql",
            "nl2sql_query",
            arguments={"question": CANARIES["argument"], "profile_id": CANARIES["profile"]},
            output=nl2sql,
            minute=4,
        ),
        _step(
            run_id,
            "nl2sql",
            "nl2sql_execute_write",
            arguments={"sql": CANARIES["sql"]},
            status=StepStatus.FAILED,
            success=False,
            error=CANARIES["error"],
            error_code=MCP_APPROVAL_SKIPPED_CODE,
            minute=5,
        ),
        # 外部の MCP の結果の分類に任意の文が入っていても写さない。
        _step(
            run_id,
            "ext",
            "lookup",
            status=StepStatus.FAILED,
            success=False,
            error=CANARIES["error"],
            error_code=f"error {CANARIES['error']}",
            minute=6,
        ),
    ]
    run = RunState(
        id=run_id,
        goal=CANARIES["question"],
        agent_id=setup.agent_id,
        runtime_id="builtin",
        status=RunStatus.COMPLETED,
        created_by_user_uuid=setup.owner,
        thread_id=f"thread_{uuid4().hex}",
        trace_id=TRACE_ID,
        metadata={
            "agent_version": 3,
            "mcp_api_key_name": CANARIES["key_name"],
            "note": CANARIES["metadata"],
        },
        usage=RunUsage(
            model="model-a", requests=4, input_tokens=1200, output_tokens=300, total_tokens=1500
        ),
        steps=steps,
        events=[
            RunEvent(
                run_id=run_id,
                type=RunEventType.RUN_COMPLETED,
                message="完了しました。",
                created_at=T0 + timedelta(minutes=10),
            )
        ],
        artifacts=[
            Artifact(
                name="回答",
                kind="answer",
                content={
                    "text": "合成の回答",
                    "outcome": {
                        "schema_version": 1,
                        "value": "answered",
                        "basis": CANARIES["basis"],
                        "signals": [CANARIES["signals"]],
                        "rag_outcome": CANARIES["signals"],
                    },
                },
            ),
            Artifact(
                name="回答の根拠",
                kind="answer_evidence",
                content={
                    "items": [
                        {
                            **_evidence("doc-a", "a:1", "文書 A"),
                            "cited": True,
                            "answer_use": "confirmed",
                        },
                        {**_evidence("doc-b", "b:1", "文書 B"), "cited": False, "answer_use": None},
                    ],
                },
            ),
            Artifact(
                name="資料との照合",
                kind="answer_validation",
                content={"status": "completed", "detail": CANARIES["error"]},
            ),
        ],
        created_at=T0,
    )
    return _store(run)


def test_observation_returns_the_run_metadata_tool_calls_and_all_evidence(setup: Setup) -> None:
    run = _full_run(setup)
    result = _observe(setup, run.id)
    assert result.get("isError") is not True, result
    output = result["structuredContent"]
    assert output["schema_version"] == mcp_server.OBSERVATION_SCHEMA_VERSION
    assert {key: output[key] for key in ("run_id", "agent_id", "thread_id")} == {
        "run_id": run.id,
        "agent_id": setup.agent_id,
        "thread_id": run.thread_id,
    }
    assert (output["agent_version"], output["trace_id"]) == (3, TRACE_ID)
    assert (output["status"], output["outcome"], output["validation_status"]) == (
        "completed",
        "answered",
        "completed",
    )
    assert output["created_at"].startswith("2026-10-25T01:00:00")
    assert output["finished_at"].startswith("2026-10-25T01:10:00")
    assert output["usage"] == {
        "model": "model-a",
        "requests": 4,
        "input_tokens": 1200,
        "output_tokens": 300,
    }

    calls = output["tool_calls"]
    assert output["tool_call_total"] == len(calls) == 7
    assert [call["index"] for call in calls] == list(range(7))
    assert [call["call_id"] for call in calls] == [step.id for step in run.steps]
    assert [(call["name"], call["status"], call["error_code"]) for call in calls] == [
        ("rag__rag_search", "completed", None),
        ("rag__rag_retrieve_evidence", "failed", "rag.timeout"),
        ("rag__rag_read_document", "completed", None),
        ("rag__rag_read_source", "completed", None),
        ("nl2sql__nl2sql_query", "completed", None),
        ("nl2sql__nl2sql_execute_write", "approval_skipped", MCP_APPROVAL_SKIPPED_CODE),
        ("ext__lookup", "failed", "tool_error"),
    ]
    # 添付の根拠は数えない。
    assert [call["evidence_count"] for call in calls] == [2, 0, 2, 1, None, None, None]
    assert [call["include_superseded"] for call in calls] == [
        True,
        False,
        None,
        None,
        None,
        None,
        None,
    ]
    assert (calls[4]["job_status"], calls[4]["row_count"], calls[4]["rows_truncated"]) == (
        "done",
        1,
        False,
    )
    assert calls[0]["started_at"].startswith("2026-10-25T01:00:00")
    # RAG の根拠のツールの縮退（#3546）。失敗した呼び出し・ほかのツールは null。
    assert [
        (call["degraded_steps"], call["guardrail_degraded"], call["zero_evidence_stage"])
        for call in calls[:3]
    ] == [
        ([{"kind": "rerank", "count": 2}], False, None),
        (None, None, None),
        (None, None, None),
    ]

    evidence = output["evidence"]
    assert (output["evidence_total"], output["withheld_evidence_count"]) == (4, 0)
    assert output["next_cursor"] is None
    assert [(item["rank"], item["document_id"], item["chunk_id"]) for item in evidence] == [
        (1, "doc-a", "a:1"),
        (2, "doc-b", "b:1"),
        (3, "doc-c", "c:1"),
        (4, "doc-c", "c:2"),
    ]
    first, second, third, fourth = evidence
    assert first | {"locator": None} == {
        "rank": 1,
        "tool_call_index": 0,
        "source": "search",
        "document_id": "doc-a",
        "chunk_id": "a:1",
        "file_name": "doc-a.pdf",
        "chunk_set_id": "set-doc-a",
        "evidence_type": "text",
        "origin": "source",
        "superseded": False,
        "retrieval_rank": 1,
        "cited": True,
        "answer_use": "confirmed",
        "locator": None,
        "table_binding": None,
        "text": "文書 A の合成の本文。",
        "text_length": len("文書 A の合成の本文。"),
        "text_omitted": False,
        "truncated": False,
    }
    # 場所は RAG の EvidenceLocator と同じ名前（bbox は出さない）。
    assert first["locator"] == {
        "section_path": ["章 1", "節 2"],
        "page_start": 3,
        "page_end": 4,
        "page_label_start": None,
        "page_label_end": None,
        "sheet_name": None,
        "row_basis": None,
        "table_index": None,
        "row_start": None,
        "row_end": None,
        "source_line_start": None,
        "source_line_end": None,
        "cell_range": None,
        "element_locator": "doc:doc-a/el:1",
        "table_row": None,
        "table_cells": [],
    }
    # 行の番号の基準と表の番号（#3262）。
    assert (second["locator"]["row_basis"], second["locator"]["table_index"]) == ("sheet", 2)
    assert (second["locator"]["table_row"], second["locator"]["table_cells"]) == (
        5,
        [
            {"table_col": "A", "header_path": []},
            {"table_col": "B", "header_path": ["上の段", "下の段"]},
        ],
    )
    # RAG が切った根拠を後で全文で読んだ: まだ無い本文をつなぎ、全文を渡したことになる。
    assert second["text"] == "文書 B の前半。\n文書 B の前半。文書 B の後半。"
    assert (second["truncated"], second["cited"], second["answer_use"]) == (False, False, None)
    assert (second["superseded"], second["evidence_type"], second["origin"]) == (
        True,
        "table",
        "derived",
    )
    # 表の行・列の対応の検査が読めるか（RAG の table_binding。読み取りの chunk は null）。
    assert (second["table_binding"], third["table_binding"]) == ("not_checkable", None)
    assert (third["source"], third["tool_call_index"], third["retrieval_rank"]) == ("read", 2, None)
    assert (third["evidence_type"], third["origin"]) == (None, None)
    assert third["text"] == "シート の 1 行目。"
    assert third["locator"] | {"section_path": None} == {
        "section_path": None,
        "page_start": None,
        "page_end": None,
        "page_label_start": None,
        "page_label_end": None,
        "sheet_name": "シート 1",
        "row_basis": None,
        "table_index": None,
        "row_start": 1,
        "row_end": 1,
        "source_line_start": None,
        "source_line_end": None,
        "cell_range": "A1:C1",
        "element_locator": None,
        "table_row": None,
        "table_cells": [],
    }
    assert fourth["text"] == "シート の 2 行目。"
    # text と structuredContent は同じ JSON（MCP の structured tool output）。
    assert json.loads(result["content"][0]["text"]) == output


def test_observation_does_not_leak_arguments_sql_errors_basis_or_metadata(setup: Setup) -> None:
    """出しすぎの検査（OWASP API3）: 目印の文字列が観測にも agent_get_run にも出ない。"""
    run = _full_run(setup)
    observation = _observe(setup, run.id)
    assert observation.get("isError") is not True
    assert len(observation["structuredContent"]["evidence"]) == 4
    observed = json.dumps(observation, ensure_ascii=False)
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agent_get_run", "arguments": {"run_id": run.id, "wait_seconds": 0}},
        },
        headers={"authorization": f"Bearer {setup.token}"},
    )
    assert response.json()["result"].get("isError") is not True
    assert response.json()["result"]["structuredContent"]["sources"]
    got = response.text
    for name, text in (("observation", observed), ("agent_get_run", got)):
        leaked = [key for key, canary in CANARIES.items() if canary in text]
        assert leaked == [], (name, leaked)
        assert str(CANARY_SCORE) not in text and str(CANARY_BBOX) not in text, name


def _paged_run(setup: Setup, texts: list[str]) -> RunState:
    run_id = f"run3240page-{uuid4().hex[:8]}"
    evidence = [
        _evidence(f"doc-{index}", f"{index}:1", text, retrieval_rank=index + 1)
        for index, text in enumerate(texts)
    ]
    return _store(
        RunState(
            id=run_id,
            goal="合成の質問",
            agent_id=setup.agent_id,
            runtime_id="builtin",
            status=RunStatus.COMPLETED,
            created_by_user_uuid=setup.owner,
            steps=[_step(run_id, "rag", "rag_retrieve_evidence", output={"evidence": evidence})],
        )
    )


def _pages(setup: Setup, run_id: str, **arguments: Any) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        extra = {"cursor": cursor} if cursor is not None else {}
        output = _observe(setup, run_id, **arguments, **extra)["structuredContent"]
        pages.append(output)
        cursor = output["next_cursor"]
        if cursor is None:
            return pages


def test_observation_pages_the_evidence_by_limit(setup: Setup) -> None:
    run = _paged_run(setup, [f"本文 {index}。" for index in range(5)])
    pages = _pages(setup, run.id, limit=2)
    assert [[item["rank"] for item in page["evidence"]] for page in pages] == [[1, 2], [3, 4], [5]]
    assert {page["evidence_total"] for page in pages} == {5}
    # ツールの呼び出しはどのページにも返す。
    assert all(len(page["tool_calls"]) == 1 for page in pages)
    default = _observe(setup, run.id)["structuredContent"]
    assert len(default["evidence"]) == 5 and default["next_cursor"] is None


def test_observation_pages_full_texts_within_the_page_budget(setup: Setup) -> None:
    """本文は切らずに返し、1 ページの合計の予算で分ける。1 件で予算を超える根拠は本文を省く。"""
    half = SOURCE_TEXT_PAGE_BUDGET_TOKENS * 3 // 5
    texts = ["い" * half, "う" * half, "え" * (SOURCE_TEXT_PAGE_BUDGET_TOKENS + 10), "短い本文。"]
    run = _paged_run(setup, texts)
    pages = _pages(setup, run.id)
    assert [[item["rank"] for item in page["evidence"]] for page in pages] == [[1], [2, 3, 4]]
    returned = [item for page in pages for item in page["evidence"]]
    assert [item["text"] for item in returned] == [texts[0], texts[1], "", texts[3]]
    assert [item["text_omitted"] for item in returned] == [False, False, True, False]
    assert [item["text_length"] for item in returned] == [len(text) for text in texts]
    for page in pages:
        assert len(json.dumps(page, ensure_ascii=False)) < SOURCE_TEXT_PAGE_BUDGET_TOKENS * 2


def test_observation_cursor_is_bound_to_the_run(setup: Setup) -> None:
    run = _paged_run(setup, ["本文 1。", "本文 2。", "本文 3。"])
    other = _paged_run(setup, ["本文 1。"])
    cursor = _observe(setup, run.id, limit=1)["structuredContent"]["next_cursor"]
    assert cursor
    for bad in (cursor, "!!not-a-cursor", "e30"):
        result = _observe(setup, other.id, cursor=bad)
        assert result["isError"] is True
        assert result["structuredContent"]["error_code"] == "INVALID_CURSOR"
    for limit in (0, 101):
        assert _observe(setup, run.id, limit=limit)["isError"] is True


def test_observation_pages_do_not_skip_evidence_when_permissions_change(setup: Setup) -> None:
    """ページの間で権限が変わっても、続きの根拠を飛ばさない（毎回その時点の権限で認可する）。"""
    run = _paged_run(setup, ["本文 1。", "本文 2。", "本文 3。"])
    first = _observe(setup, run.id, limit=1)["structuredContent"]
    assert [item["rank"] for item in first["evidence"]] == [1]
    setup.rag.unreadable_documents = {"doc-1"}
    second = _observe(setup, run.id, limit=1, cursor=first["next_cursor"])["structuredContent"]
    assert [item["rank"] for item in second["evidence"]] == [3]
    assert (second["evidence_total"], second["withheld_evidence_count"]) == (2, 1)
    assert second["next_cursor"] is None
    assert second["tool_calls"][0]["evidence_count"] == 2


def test_observation_of_a_running_run(setup: Setup) -> None:
    run_id = f"run3240run-{uuid4().hex[:8]}"
    run = _store(
        RunState(
            id=run_id,
            goal="合成の質問",
            agent_id=setup.agent_id,
            runtime_id="builtin",
            status=RunStatus.RUNNING,
            created_by_user_uuid=setup.owner,
            steps=[
                _step(
                    run_id,
                    "rag",
                    "rag_search",
                    output={"evidence": [_evidence("doc-a", "a:1", "本文。")]},
                ),
                _step(run_id, "rag", "rag_read_source", status=StepStatus.RUNNING, minute=1),
            ],
            artifacts=[
                Artifact(name="回答", kind="answer", content={"outcome": {"value": "answered"}})
            ],
        )
    )
    output = _observe(setup, run.id)["structuredContent"]
    assert (output["status"], output["outcome"], output["finished_at"]) == ("running", None, None)
    assert (output["usage"], output["trace_id"], output["agent_version"]) == (None, None, None)
    assert [call["status"] for call in output["tool_calls"]] == ["completed", "running"]
    assert output["tool_calls"][1]["completed_at"] is None
    assert [item["chunk_id"] for item in output["evidence"]] == ["a:1"]


def test_observation_limits_the_tool_calls_and_reports_the_total(setup: Setup) -> None:
    run_id = f"run3240many-{uuid4().hex[:8]}"
    total = 205
    run = _store(
        RunState(
            id=run_id,
            goal="合成の質問",
            agent_id=setup.agent_id,
            runtime_id="builtin",
            status=RunStatus.COMPLETED,
            created_by_user_uuid=setup.owner,
            steps=[_step(run_id, "ext", "lookup", output={}) for _ in range(total)],
        )
    )
    output = _observe(setup, run.id)["structuredContent"]
    assert output["tool_call_total"] == total
    assert len(output["tool_calls"]) == 200


@pytest.mark.parametrize(
    "cells",
    [
        [{"table_col": "B", "header_path": "上の段"}],
        [{"table_col": "", "header_path": []}],
        [{"table_col": "B", "header_path": [1]}],
        ["B"],
        {"table_col": "B", "header_path": []},
    ],
    ids=["path-not-list", "empty-column", "path-not-text", "cell-not-object", "not-list"],
)
def test_observation_drops_table_cells_of_another_shape(cells: object) -> None:
    """RAG の table_cells の形に合わないセルがあれば、セルを 1 つも写さない（行の番号は写す）。"""
    locator = run_observation._locator({"table_row": 5, "table_cells": cells})
    assert locator == {"table_row": 5}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"row_basis": "word_table", "table_index": 3},
            {"row_basis": "word_table", "table_index": 3},
        ),
        (
            {"row_basis": "csv", "row_start": 4, "source_line_start": 5, "source_line_end": 6},
            {"row_basis": "csv", "row_start": 4, "source_line_start": 5, "source_line_end": 6},
        ),
        # 解析の表の格子の行（#3429）。セルの領域（bbox）は写さない。
        (
            {
                "row_basis": "parsed_table",
                "table_index": 2,
                "row_start": 3,
                "row_end": 3,
                "table_row": 3,
                "table_cells": [
                    {"table_col": "B", "header_path": ["上の段", "下の段"], "bbox": [1, 2, 3, 4]}
                ],
            },
            {
                "row_basis": "parsed_table",
                "table_index": 2,
                "row_start": 3,
                "row_end": 3,
                "table_row": 3,
                "table_cells": [{"table_col": "B", "header_path": ["上の段", "下の段"]}],
            },
        ),
        # 知らない基準・形の合わない番号は写さない。
        ({"row_basis": "page", "table_index": "3"}, {}),
    ],
    ids=["word-table", "csv", "parsed-table", "unknown"],
)
def test_observation_copies_the_row_basis_of_rag(
    value: dict[str, object], expected: object
) -> None:
    """行の番号の基準と表の番号（RAG の EvidenceLocator と同じ名前。#3262）を写す。"""
    assert run_observation._locator(value) == expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # 補助の処理の縮退で根拠 0 件（当たらなかったのか読めなかったのかを分ける）。
        (
            {
                "evidence": [],
                "degraded_steps": [{"kind": "reference_load", "count": 1}],
                "zero_evidence": {"stage": "retrieval", "candidate_count": 0, "rejected_count": 0},
            },
            ([{"kind": "reference_load", "count": 1}], False, "retrieval"),
        ),
        # 安全チェックを完了できず止めた。
        (
            {"evidence": [], "guardrail_degraded": True, "zero_evidence": None},
            ([], True, None),
        ),
        # 知らない段・壊れた記録は写さない。
        (
            {
                "evidence": [],
                "degraded_steps": "rerank",
                "guardrail_degraded": "yes",
                "zero_evidence": {"stage": "unknown"},
            },
            ([], False, None),
        ),
    ],
    ids=["degraded-zero", "guardrail", "unknown"],
)
def test_observation_copies_the_degraded_retrieval_of_rag(
    output: dict[str, Any], expected: tuple[object, object, object]
) -> None:
    """RAG の根拠のツールの縮退・安全チェックの未完了・根拠 0 件の段を写す（#3546）。"""
    run_id = f"run3546obs-{uuid4().hex[:8]}"
    run = RunState(
        id=run_id,
        goal="合成の質問",
        agent_id="agent-a",
        runtime_id="builtin",
        status=RunStatus.COMPLETED,
        steps=[_step(run_id, "rag", "rag_retrieve_evidence", output=output)],
        created_at=T0,
    )
    (call,) = run_observation.observe_run(run).tool_calls
    assert (call.degraded_steps, call.guardrail_degraded, call.zero_evidence_stage) == expected
