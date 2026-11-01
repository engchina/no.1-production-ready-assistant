"""失敗したツールの文・詳細の機微情報を、モデル・Run・イベント・ログへ渡す前に伏せる（#2611）。

成功の出力と同じ規則（機微な名前の項目・文の中の資格情報・Bearer・個人情報）を、例外の経路
（`ExternalToolError` の message / details、一般の例外の文、HTTP の応答の本文、JSON-RPC の error、
入れ子の詳細）にも当てる。値は合成のマーカーだけを使い、外部への通信はしない。
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import LogCaptureFixture, MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget, tool_failure_payload
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tool_arguments import INVALID_ARGUMENTS_CODE
from app.features.agent.tools import (
    ExternalToolError,
    ToolCall,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicy,
    sanitize_tool_error,
    tool_registry,
)

# 合成のマーカー（実の資格情報ではない）。
MARKER = "aaaa2611aaaa2611aaaa"
TOOL = "test2611_fetch"
SKILL_ID = "test2611-skill"
AGENT_ID = "test2611-agent"
USER_UUID = "11111111-2222-3333-4444-555555552611"


def _failing(error: Exception) -> Any:
    def handle(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        raise error

    return handle


def _register(handler: Any) -> None:
    tool_registry.register(
        ToolDefinition(
            name=TOOL,
            description="合成の読み取りのツール",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            permission_level=ToolPermissionLevel.READ,
        ),
        handler,
    )


@pytest.fixture
def agent() -> Iterator[None]:
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="マスクのテストの Skill",
            instructions="値を読む。",
            mcp_requirements=[SkillMcpRequirement(server_id="control-plane", tool_names=[TOOL])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="マスクのテストの業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield
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
        tool_registry._definitions.pop(TOOL, None)  # noqa: SLF001 - テスト用の登録を戻す
        tool_registry._handlers.pop(TOOL, None)  # noqa: SLF001


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


def _log_text(caplog: LogCaptureFixture) -> str:
    return "\n".join(
        f"{record.getMessage()} {json.dumps(record.__dict__, ensure_ascii=False, default=str)}"
        for record in caplog.records
    )


def _external_error() -> ExternalToolError:
    """呼び先の誤り（本文の Bearer、Authorization の項目、入れ子の詳細と JSON の本文）。"""
    return ExternalToolError(
        "mcp.tool_error",
        f"呼び先が拒否しました: Authorization: Bearer {MARKER}",
        {
            "status": 401,
            "error_code": "unauthorized",
            "Authorization": f"Bearer {MARKER}",
            "details": {
                "body": json.dumps({"error": "denied", "access_token": MARKER}),
                "headers": {"x-api-key": MARKER},
                "errors": [{"loc": ["key"], "message": f"token={MARKER} は使えません"}],
            },
        },
    )


def test_external_tool_error_is_masked_for_model_run_events_and_logs(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture, agent: None
) -> None:
    del agent
    _register(_failing(_external_error()))
    model = _script(
        monkeypatch,
        [function_call(TOOL, '{"key":"k1"}', call_id="call-1")],
        [assistant_message("取得できませんでした。")],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="値を読んで", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    caplog.set_level(logging.DEBUG)

    anyio.run(builtin_runtime.execute_run, run.id)

    stored = runtime_repository.get_run(run.id)
    assert stored.status == RunStatus.COMPLETED
    # モデルへの入力・Run のシリアライズ（step・イベント）・ログのどこにもマーカーが残らない。
    assert MARKER not in json.dumps(model.calls[1].input, ensure_ascii=False, default=str)
    assert MARKER not in stored.model_dump_json()
    failed = [event for event in stored.events if event.type == "tool.failed"]
    assert failed
    assert MARKER not in json.dumps([event.payload for event in failed], default=str)
    assert MARKER not in _log_text(caplog)
    # 診断に要る技術の情報（誤りの code・status・呼び先の code）は残す。
    [step] = stored.steps
    result = step.tool_result
    assert result is not None
    assert result.error_code == "mcp.tool_error"
    assert result.error_details["status"] == 401
    assert result.error_details["error_code"] == "unauthorized"
    assert result.error_details["Authorization"] == "***MASKED***"
    assert "secret.bearer_token_masked" in result.guardrail_warnings
    assert "sensitive_field_masked:Authorization" in result.audit_metadata["error_masked"]
    # 伏せた記録に元の値を再掲しない。
    assert MARKER not in json.dumps(result.audit_metadata, default=str)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"connect failed (Authorization: Bearer {MARKER})"),
        ExternalToolError(
            "external_mcp.http_error",
            "呼び先が HTTP 500 を返しました。",
            {"status_code": 500, "body": f'{{"message":"failed","client_secret":"{MARKER}"}}'},
        ),
        ExternalToolError(
            "mcp.rpc_error",
            "MCP 接続が JSON-RPC のエラーを返しました。",
            {"error": {"code": -32000, "message": f"Bearer {MARKER}", "data": {"token": MARKER}}},
        ),
        ExternalToolError(
            "external_mcp.oauth_request_error",
            f"https://user:{MARKER}@mcp.example/token?api_key={MARKER} に接続できません",
            {"reason": f"password: {MARKER}"},
        ),
    ],
    ids=["unhandled", "http_body", "jsonrpc_error", "url_and_inline"],
)
def test_error_paths_mask_markers_in_text_and_nested_details(error: Exception) -> None:
    _register(_failing(error))
    try:
        result = tool_registry.invoke(
            ToolCall(name=TOOL, arguments={"key": "k"}),
            policy=ToolPolicy(allow={TOOL}),
            context=ToolInvocationContext(),
        )
    finally:
        tool_registry._definitions.pop(TOOL, None)  # noqa: SLF001
        tool_registry._handlers.pop(TOOL, None)  # noqa: SLF001

    assert result.success is False
    assert MARKER not in result.model_dump_json()
    assert MARKER not in json.dumps(tool_failure_payload(result), ensure_ascii=False)
    # 分類（code・status）は残す。
    if isinstance(error, ExternalToolError):
        assert result.error_code == error.code
        if "status_code" in error.details:
            assert result.error_details["status_code"] == error.details["status_code"]
    else:
        assert result.error_code == "tool.unhandled_error"


def test_success_output_masking_is_unchanged() -> None:
    """成功の出力の既存のマスク（対照）。同じ内容が成功の経路でも消える。"""

    def ok(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        return {"message": f"Authorization: Bearer {MARKER}", "api_key": MARKER, "ok": True}

    _register(ok)
    try:
        result = tool_registry.invoke(
            ToolCall(name=TOOL, arguments={"key": "k"}),
            policy=ToolPolicy(allow={TOOL}),
            context=ToolInvocationContext(),
        )
    finally:
        tool_registry._definitions.pop(TOOL, None)  # noqa: SLF001
        tool_registry._handlers.pop(TOOL, None)  # noqa: SLF001
    assert result.success is True
    assert MARKER not in result.model_dump_json()
    assert result.output is not None
    assert result.output["ok"] is True


def test_argument_errors_and_flags_are_kept() -> None:
    """値を含まない引数の検証の結果（#2630）と、有無を表す真偽の項目はそのまま残す。"""
    _register(_failing(AssertionError("呼ばれない")))
    try:
        invalid = tool_registry.invoke(
            ToolCall(name=TOOL, arguments={"key": 1}),
            policy=ToolPolicy(allow={TOOL}),
            context=ToolInvocationContext(),
        )
    finally:
        tool_registry._definitions.pop(TOOL, None)  # noqa: SLF001
        tool_registry._handlers.pop(TOOL, None)  # noqa: SLF001
    assert invalid.error_code == INVALID_ARGUMENTS_CODE
    assert invalid.error_details["argument_errors"][0]["argument"] == "key"

    error, details, warnings = sanitize_tool_error(
        "OAuth の資格情報がそろっていません。",
        {"client_secret_configured": False, "token_url_configured": True, "token": None},
    )
    assert error == "OAuth の資格情報がそろっていません。"
    assert details == {
        "client_secret_configured": False,
        "token_url_configured": True,
        "token": None,
    }
    assert warnings == []
