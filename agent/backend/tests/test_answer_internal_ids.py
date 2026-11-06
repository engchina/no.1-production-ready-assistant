"""回答の本文に内部の識別子を出さない後処理（#169）の決定論テスト。

スキルが禁じても、モデルはツールの結果の `document_id`・`section_id` などの値と項目名を回答に
書くことがある。組み込み Runtime は検証の前にそれらを含む括弧の補足を消し、残る段落を外す。
内容はすべて合成のデータ。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import anyio
from agents.testing import assistant_message, function_call
from mcp_support import RAG_OUTPUTS, FakeProductMcp
from pytest import MonkeyPatch
from test_answer_validation import AGENT_ID, ANSWER, USER_UUID, _script, mcp  # noqa: F401 - fixture

from app.features.agent import builtin_runtime
from app.features.agent.answer_validation import (
    INTERNAL_ID_FIELDS,
    internal_id_values,
    without_lookup_notes,
)
from app.features.agent.runtime import (
    RunCreateRequest,
    RunStep,
    StepStatus,
    run_answer_text,
    runtime_repository,
)
from app.features.agent.tools import ToolCall, ToolResult

CONTRACT = Path(__file__).resolve().parents[3] / "platform/contracts/mcp/rag-tools.json"
DOC = "0f3a9c2e7b1d4e5f0a1b"
SECTION = "nav-87b461b3efd932e0"
CHUNK = f"{DOC}:cs-a:7"


def _output_properties(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for item in value.values():
            names |= _output_properties(item)
    elif isinstance(value, list):
        for item in value:
            names |= _output_properties(item)
    return names


def test_internal_fields_are_output_fields_of_the_rag_contract() -> None:
    tools = json.loads(CONTRACT.read_text(encoding="utf-8"))["tools"]
    outputs = _output_properties([tool.get("outputSchema") for tool in tools])
    assert {*INTERNAL_ID_FIELDS, "section_complete"} <= outputs


def _evidence(excerpt: str = "受付は平日 9 時からです。") -> dict[str, Any]:
    return {
        "evidence_id": CHUNK,
        "document_id": DOC,
        "chunk_id": CHUNK,
        "file_name": "文書 A.pdf",
        "excerpt": excerpt,
        "section": {"section_id": SECTION, "title": "第 1 章", "chars": 10},
    }


def _step(output: dict[str, Any], arguments: dict[str, Any] | None = None) -> RunStep:
    return RunStep(
        run_id="run-internal-ids",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name="rag__rag_retrieve_evidence", arguments=arguments or {}),
        tool_result=ToolResult(name="rag__rag_retrieve_evidence", success=True, output=output),
    )


def test_values_come_from_tool_calls_and_results_but_not_from_data_rows_or_sources() -> None:
    steps = [
        _step({"evidence": [_evidence()]}, {"search_answer_profile_id": "ignored-profile-id"}),
        _step({"rows": [{"document_id": "business-value-1"}], "next_cursor": "cursor-123456"}),
    ]
    assert internal_id_values(steps) == {
        DOC: "document_id",
        SECTION: "section_id",
        CHUNK: "evidence_id",
        "cursor-123456": "next_cursor",
    }
    # 資料の原文に書かれた値は、資料の内容として回答に書いてよい。
    quoted = [_step({"evidence": [_evidence(f"管理番号 {SECTION} を付ける。")]})]
    assert SECTION not in internal_id_values(quoted)


def test_internal_ids_and_field_names_are_removed_from_the_answer() -> None:
    ids = {DOC: "document_id", SECTION: "section_id", CHUNK: "chunk_id"}
    answer = (
        f"受付は平日 9 時からです（{SECTION}、2〜3 ページ）。\n\n"
        "### 使用ツールと根拠\n"
        f"- 文書 A.pdf（document_id: {DOC}）section_id: {SECTION}\n"
        "- いずれも `section_complete: true`"
    )
    # 括弧の補足は消して主張は残し、中身の無くなった節は見出しごと外す。
    assert without_lookup_notes(answer, ids) == "受付は平日 9 時からです。"
    # 識別子の無い括弧・リンクの URL は変えない。
    kept = f"受付は平日 9 時からです（第 1 章、p.2）。[文書 A](https://example.com/{DOC})"
    assert without_lookup_notes(kept) == kept
    # 根拠の ID の印は画面が根拠の行にするので残す（`answer_evidence` の markers）。
    marked = f"受付は平日 9 時からです【証拠 ID: {CHUNK}】。"
    assert without_lookup_notes(marked, ids) == marked
    # 外すと本文が残らないときも、値は出さない。
    assert DOC not in without_lookup_notes(f"- {DOC}", ids)


def test_the_run_answer_has_no_internal_ids(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811 - fixture
) -> None:
    output = deepcopy(RAG_OUTPUTS["rag_search"])
    output["evidence"][0].update(document_id=DOC, chunk_id=CHUNK, evidence_id=CHUNK)
    mcp.outputs["rag_search"] = output
    answer = (
        f"{ANSWER.rstrip('。')}（document_id: {DOC}）。\n\n"
        "---\n"
        f"- 契約書.pdf（{CHUNK}）を終わりまで読んだ: `section_complete: true`"
    )
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "合成の質問"}, call_id="call-1")],
        [assistant_message(answer)],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    run = runtime_repository.get_run(run.id)

    # 検証にも利用者にも、識別子を除いた本文だけを渡す。
    [call] = mcp.calls_of("rag_validate_answer")
    assert call["arguments"]["answer"] == ANSWER
    published = run_answer_text(run) or ""
    assert published == ANSWER
    for value in (DOC, CHUNK, "section_complete", "document_id"):
        assert value not in published
