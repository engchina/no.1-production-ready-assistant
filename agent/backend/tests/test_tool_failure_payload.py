"""失敗したツールの結果にモデルが直せる内容を入れる（#2498）の決定論テスト（合成。RAG はスタブ）。

MCP の呼び先が引数の検証の誤り（`MCP_TOOL_ARGUMENTS_INVALID`）を返したとき、モデルへの結果に
違反の場所と文（`argument_errors`）と呼び先の誤りの code（`tool_error_code`）が入ることを確かめる。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import FakeProductMcp, McpToolError, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import (
    MAX_ARGUMENT_VIOLATIONS,
    ModelTarget,
    tool_failure_payload,
)
from app.features.agent.runtime import AgentProfile, RunCreateRequest, runtime_repository
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ToolResult
from app.settings import get_settings

SKILL_ID = "test2498-skill"
AGENT_ID = "test2498-agent"
USER_UUID = "11111111-2222-3333-4444-555555552498"


def _failed(
    details: dict[str, Any] | None, error: str = "ツールの引数が正しくありません。"
) -> ToolResult:
    return ToolResult(
        name="rag__rag_read_section",
        success=False,
        error=error,
        error_code="mcp.tool_error",
        error_details=details or {},
    )


def test_payload_adds_callee_code_and_argument_errors() -> None:
    payload = tool_failure_payload(
        _failed(
            {
                "server_id": "rag",
                "tool_name": "rag_read_section",
                "error_code": "MCP_TOOL_ARGUMENTS_INVALID",
                "status": None,
                "details": {
                    "errors": [
                        {
                            "loc": "",
                            "message": "section_id・cursor のどれか 1 つだけを渡してください。",
                            "type": "value_error",
                            "raw_message": "Value error, 内部の原文",
                        },
                        {"loc": ["cases", 0, "query"], "message": "入力してください。"},
                        {"loc": "max_chars", "message": "1 以上にしてください。"},
                        {"loc": "x", "message": "   "},
                        "壊れた項目",
                    ]
                },
            }
        )
    )
    assert payload == {
        "error": "ツールの引数が正しくありません。",
        "error_code": "mcp.tool_error",
        "tool_error_code": "MCP_TOOL_ARGUMENTS_INVALID",
        "argument_errors": [
            {"argument": "", "message": "section_id・cursor のどれか 1 つだけを渡してください。"},
            {"argument": "cases.0.query", "message": "入力してください。"},
            {"argument": "max_chars", "message": "1 以上にしてください。"},
        ],
    }
    # 型の名前・元の文・接続の情報はモデルへ渡さない。
    text = json.dumps(payload, ensure_ascii=False)
    for hidden in ("value_error", "内部の原文", "server_id", "rag_read_section"):
        assert hidden not in text


def test_payload_limits_count_and_length_and_skips_unknown_codes() -> None:
    errors = [{"loc": "a" * 500, "message": "b" * 1000} for _ in range(MAX_ARGUMENT_VIOLATIONS + 3)]
    payload = tool_failure_payload(
        _failed({"error_code": "bad code with spaces", "details": {"errors": errors}})
    )
    assert "tool_error_code" not in payload
    assert len(payload["argument_errors"]) == MAX_ARGUMENT_VIOLATIONS
    assert all(len(item["argument"]) == 100 for item in payload["argument_errors"])
    assert all(len(item["message"]) == 300 for item in payload["argument_errors"])
    # 呼び先の詳細が無い失敗は、これまでと同じ内容。
    assert tool_failure_payload(_failed(None, error="")) == {
        "error": "tool failed",
        "error_code": "mcp.tool_error",
    }


# ---- 組み込み Runtime: モデルへの結果に違反の内容が入る --------------------------------------


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="資料は rag_read_section で読む。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag", tool_names=["rag_retrieve_evidence", "rag_read_section"]
                )
            ],
        )
    )
    runtime_repository.create_agent(AgentProfile(id=AGENT_ID, name=AGENT_ID, skill_ids=[SKILL_ID]))
    try:
        yield fake
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run() -> None:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="設定 A の章節を読んでください。", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def test_model_sees_the_argument_violation_of_the_input_schema(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """schema で分かる違反は呼び先へ送らず、Agent の実行の境界で返す（#2630。値は含めない）。"""
    model = _script(
        monkeypatch,
        # max_chars は 1 以上（ツールの input schema の minimum）。
        [
            function_call(
                "rag__rag_read_section",
                {"document_id": "doc-1", "section_id": "sec-5", "max_chars": 0},
                call_id="c1",
            )
        ],
        [assistant_message("章節を読めませんでした。")],
    )

    _run()

    output = _tool_output(model, 1, "c1")
    assert output["error_code"] == "tool.invalid_arguments"
    [violation] = output["argument_errors"]
    assert violation == {
        "argument": "max_chars",
        "code": "minimum",
        "message": "1 以上にしてください。",
    }
    assert not mcp.calls_of("rag_read_section")


def test_model_sees_the_callee_error_code_of_a_tool_error(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_read_section"] = McpToolError("cursor_invalid", "cursor の形が違います。")
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_read_section",
                {"document_id": "doc-1", "cursor": "broken"},
                call_id="c1",
            )
        ],
        [assistant_message("章節を読めませんでした。")],
    )

    _run()

    assert _tool_output(model, 1, "c1") == {
        "error": "cursor の形が違います。",
        "error_code": "mcp.tool_error",
        "tool_error_code": "cursor_invalid",
    }
