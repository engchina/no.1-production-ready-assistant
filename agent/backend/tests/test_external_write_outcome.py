"""外部への書き込みの結果不明と重複の契約の決定論テスト（#72）。

合成の外部 MCP（`httpx.MockTransport`）が反映した副作用の件数を数え、次の窓を再現する。

- 反映した後に応答が失われる（読み取りの timeout・502）: 結果不明として記録し、通信の層でも
  Runtime でも送り直さない。同じ操作の呼び直しは人の承認を要し、承認したら最初と同じ操作の ID で
  送る（ID で重複を判定する相手は 2 回反映しない）。
- 冪等の申告（idempotentHint）: 信頼した接続だけ届いた後の失敗を再送する（同じ ID）。
- 同じ承認の再開（同じ call ID がもう一度来る）: 送り直さず、記録した結果を返す。
- 送る前の記録を保存できない: 送らない。
- 送った後・結果の保存の前に落ちる: 起動時の後始末で結果不明にする（未実行と断定しない）。
- 利用者の再実行: 元の Run に結果不明があれば、明示の確認なしには始めない。

モデルは SDK の `ScriptedModel`、保存先は memory。外部への通信はしない。ツールの名前・内容は合成。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from agents import FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import MonkeyPatch

import app.features.agent.tools as tools_module
from app.features.agent import builtin_runtime, external_writes
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.config import runtime_config_store
from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunStatus,
    StepStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import (
    OPERATION_KEY_META,
    OUTCOME_UNKNOWN,
    ExternalToolError,
    ToolCall,
    mcp_function_name,
)

SERVER_ID = "test72-ext"
SKILL_ID = "test72-skill"
AGENT_ID = "test72-agent"
USER_UUID = "11111111-2222-3333-4444-555555550072"
TOOL = "create_ticket"
FUNCTION = mcp_function_name(SERVER_ID, TOOL)
ARGS = {"title": "合成の件名"}


class _TicketMcp:
    """副作用を数える合成の MCP。`operation_key` を見て重複を判定できる（`dedupe`）。"""

    def __init__(self) -> None:
        self.idempotent = False
        self.dedupe = False
        # 届いた `tools/call`（引数・操作の ID）と、反映した副作用。
        self.received: list[tuple[dict[str, Any], str | None]] = []
        self.applied: list[dict[str, Any]] = []
        # 反映した後に返す失敗（例外か HTTP status）。順に使う。
        self.after_apply: list[Exception | int] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            return self._result(body, {"capabilities": {}})
        if method == "tools/list":
            annotations = {"readOnlyHint": False, "idempotentHint": self.idempotent}
            tool = {
                "name": TOOL,
                "description": "合成の起票",
                "inputSchema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
                "annotations": annotations,
            }
            return self._result(body, {"tools": [tool]})
        params = body["params"]
        key = (params.get("_meta") or {}).get(OPERATION_KEY_META)
        self.received.append((params["arguments"], key))
        if not (self.dedupe and key and any(item.get("_key") == key for item in self.applied)):
            self.applied.append({**params["arguments"], "_key": key})
        failure = self.after_apply.pop(0) if self.after_apply else None
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return httpx.Response(failure, text="合成の失敗")
        return self._result(body, {"structuredContent": {"ticket": "T-1", "ok": True}})

    @staticmethod
    def _result(body: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def server(monkeypatch: MonkeyPatch) -> Iterator[_TicketMcp]:
    fake = _TicketMcp()
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(fake.handle), timeout=kwargs["timeout"])

    monkeypatch.setattr("app.features.agent.tools.httpx.Client", client)
    monkeypatch.setattr(tools_module, "_retry_sleep", lambda _seconds: None)
    original = runtime_config_store.get_tool_policy()
    runtime_config_store.upsert_mcp_server(
        SERVER_ID, base_url="https://ext72.example.test/mcp", auth_mode="none"
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="合成の起票の Skill",
            instructions="依頼を起票する。",
            mcp_requirements=[SkillMcpRequirement(server_id=SERVER_ID, tool_names=[TOOL])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="合成の起票の業務 Agent", skill_ids=[SKILL_ID])
    )
    # 起票は「自動実行」から始める（結果不明の後の承認が、ツール権限に依らず効くことを試す）。
    runtime_config_store.patch_tool_policy(allow=[FUNCTION], ask=[], deny=[])
    try:
        yield fake
    finally:
        runtime_config_store.patch_tool_policy(
            default_mode=original.default_mode,
            allow=sorted(original.allow),
            ask=sorted(original.ask),
            deny=sorted(original.deny),
        )
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        with contextlib.suppress(KeyError):
            runtime_config_store.remove_mcp_server(SERVER_ID)


def _trust(trusted: bool) -> None:
    runtime_config_store.upsert_mcp_server(SERVER_ID, trust_tool_annotations=trusted)


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


def _create_run(**metadata: Any) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="起票して", agent_id=AGENT_ID, metadata=metadata),
        created_by_user_uuid=USER_UUID,
    )
    return run.id


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _function_tool(run_id: str) -> FunctionTool:
    tools, _warnings = builtin_runtime.discover_mcp_tools(
        [SKILL_ID], context=tools_module.ToolInvocationContext(user_uuid=USER_UUID)
    )
    [tool] = builtin_runtime.build_function_tools(run_id, [], mcp_tools=tools)
    return tool


async def _invoke(tool: FunctionTool, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    output = await tool.on_invoke_tool(
        SimpleNamespace(tool_call_id=call_id),  # type: ignore[arg-type]
        json.dumps(arguments, ensure_ascii=False),
    )
    loaded: dict[str, Any] = json.loads(str(output))
    return loaded


# ---- 反映した後の応答の喪失 ------------------------------------------------------------------


def test_lost_response_is_unknown_and_the_same_operation_needs_approval(
    monkeypatch: MonkeyPatch, server: _TicketMcp
) -> None:
    """反映 → 応答の喪失 → 結果不明。呼び直しは承認待ち。承認後は同じ ID で 1 回だけ反映。"""
    server.dedupe = True
    server.after_apply.append(httpx.ReadTimeout("合成の応答の喪失"))
    model = _script(
        monkeypatch,
        [function_call(FUNCTION, ARGS, call_id="call-1")],
        [function_call(FUNCTION, ARGS, call_id="call-2")],
        [assistant_message("起票しました。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    # 通信の層でも Runtime でも送り直していない（相手は 1 回だけ受け取った）。
    assert len(server.received) == 1
    assert len(server.applied) == 1
    first = waiting.steps[0]
    assert first.tool_result is not None
    assert first.tool_result.error_code == "mcp.timeout"
    assert first.tool_result.error_details["outcome"] == OUTCOME_UNKNOWN
    assert external_writes.step_outcome_unknown(first)
    first_key = server.received[0][1]
    assert first_key is not None and first.tool_call is not None
    assert first.tool_call.operation_key == first_key
    # 引数には足さない（契約に無い項目を送らない）。
    assert server.received[0][0] == ARGS
    # モデルには未実行と扱わせない案内を返す。
    told = _tool_output(model, 1, "call-1")
    assert told["outcome"] == OUTCOME_UNKNOWN
    assert told["operation_key"] == first_key
    assert "繰り返さない" in told["next_step"]
    # 自動実行のツールでも、結果不明の操作の呼び直しは照合を求める承認になる。
    [approval] = waiting.approvals
    assert approval.tool_call.trace_id == "call-2"
    assert approval.reason == external_writes.approval_reason(FUNCTION)

    runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by="approver-72")
    )
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    # 承認した送り直しは最初と同じ操作の ID（相手は ID で重複と判定し、反映は 1 件のまま）。
    assert [key for _args, key in server.received] == [first_key, first_key]
    assert len(server.applied) == 1
    resent = next(step for step in run.steps if step.approval_id == approval.id)
    assert resent.tool_result is not None and resent.tool_result.success
    assert resent.tool_result.audit_metadata["operation_key"] == first_key
    # 成功したので、結果不明は解消した（同じ操作をもう一度呼んでも承認は要らない）。
    assert external_writes.unresolved_unknown(run.steps, FUNCTION, ARGS) is None


def test_unattended_run_never_resends_an_unknown_write(
    monkeypatch: MonkeyPatch, server: _TicketMcp
) -> None:
    """無人の Run（MCP）は承認できないので、結果不明の操作を送り直さない。

    書き込みが 1 つも結果を返さずに終わった Run は失敗にする（#2475）。その文は呼び先の文の
    「再実行してください」ではなく、結果不明と照合を伝える。
    """
    server.after_apply.append(502)
    _script(
        monkeypatch,
        [function_call(FUNCTION, ARGS, call_id="call-1")],
        [function_call(FUNCTION, ARGS, call_id="call-2")],
        [assistant_message("結果が分かりません。")],
    )
    run_id = _create_run(source="mcp", api_key_id="0123456789abcdef")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert len(server.received) == 1
    assert run.approvals == []
    message = run.events[-1].message
    assert external_writes.OUTCOME_UNKNOWN_RUN_MESSAGE in message
    assert "時間をおいて再実行" not in message


def test_different_arguments_are_a_different_operation(
    monkeypatch: MonkeyPatch, server: _TicketMcp
) -> None:
    """引数が違えば別の論理操作（別の ID）。結果不明の照合の対象にしない。"""
    server.after_apply.append(httpx.ReadError("合成の切断"))
    _script(
        monkeypatch,
        [function_call(FUNCTION, ARGS, call_id="call-1")],
        [function_call(FUNCTION, {"title": "別の件名"}, call_id="call-2")],
        [assistant_message("起票しました。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    keys = [key for _args, key in server.received]
    assert len(keys) == 2 and keys[0] != keys[1]


# ---- 冪等の申告 -------------------------------------------------------------------------------


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_idempotent_hint_is_resent_with_the_same_key_only_when_trusted(
    server: _TicketMcp, trusted: bool
) -> None:
    server.idempotent = True
    server.dedupe = True
    server.after_apply.append(504)
    _trust(trusted)
    run_id = _create_run()
    assert runtime_repository.begin_builtin_run(run_id) is not None

    output = anyio.run(_invoke, _function_tool(run_id), ARGS, "call-1")

    keys = [key for _args, key in server.received]
    assert len(server.applied) == 1
    if trusted:
        assert output["ok"] is True
        assert len(keys) == 2 and keys[0] == keys[1] and keys[0]
    else:
        assert output["outcome"] == OUTCOME_UNKNOWN
        assert len(keys) == 1


# ---- 同じ承認の再開・保存の前後の中断 ---------------------------------------------------------


def test_same_call_id_is_not_sent_twice(server: _TicketMcp) -> None:
    """同じ call ID の呼び出しがもう一度来ても（同じ承認の再開など）、送り直さない。"""
    run_id = _create_run()
    assert runtime_repository.begin_builtin_run(run_id) is not None
    tool = _function_tool(run_id)

    first = anyio.run(_invoke, tool, ARGS, "call-1")
    again = anyio.run(_invoke, tool, ARGS, "call-1")

    assert first["ok"] is True
    assert again == {**first, "already_executed": True}
    assert len(server.received) == 1
    assert len(runtime_repository.get_run(run_id).steps) == 1


def test_write_is_not_sent_when_the_intent_cannot_be_saved(
    monkeypatch: MonkeyPatch, server: _TicketMcp
) -> None:
    run_id = _create_run()
    assert runtime_repository.begin_builtin_run(run_id) is not None
    tool = _function_tool(run_id)

    def broken_flush(_ticket: int | None = None) -> None:
        raise RuntimeError("合成の保存先の障害")

    monkeypatch.setattr(runtime_repository, "flush_writes", broken_flush)
    output = anyio.run(_invoke, tool, ARGS, "call-1")

    assert output["error_code"] == external_writes.INTENT_NOT_PERSISTED_CODE
    assert server.received == []
    [step] = runtime_repository.get_run(run_id).steps
    assert step.status == StepStatus.FAILED
    # 送っていないので結果不明ではない（同じ call ID で送り直してよい）。
    assert not external_writes.step_outcome_unknown(step)


def test_crash_after_send_is_unknown_after_restart(server: _TicketMcp) -> None:
    """送った後・結果を保存する前に落ちた書き込みは、起動時の後始末で結果不明にする。"""
    run_id = _create_run()
    assert runtime_repository.begin_builtin_run(run_id) is not None
    runtime_repository.start_builtin_tool_step(
        run_id, ToolCall(name=FUNCTION, arguments=ARGS, trace_id="call-1", operation_key="op_x")
    )
    runtime_repository.start_builtin_tool_step(
        run_id, ToolCall(name="test72_read", arguments={}, trace_id="call-2")
    )

    failed = runtime_repository.fail_interrupted_runs(
        updated_before=datetime.now(UTC) + timedelta(seconds=1)
    )

    assert run_id in failed
    write, read = runtime_repository.get_run(run_id).steps
    assert write.tool_result is not None
    assert write.tool_result.error_code == external_writes.OUTCOME_UNKNOWN_CODE
    assert write.tool_result.error_details == {"outcome": OUTCOME_UNKNOWN, "operation_key": "op_x"}
    # 副作用の無い呼び出し（ID の無い step）は従来どおりの失敗。
    assert read.status == StepStatus.FAILED and read.tool_result is None


# ---- 利用者の再実行 ---------------------------------------------------------------------------


def test_replay_of_a_run_with_an_unknown_write_needs_explicit_confirmation(
    monkeypatch: MonkeyPatch, server: _TicketMcp
) -> None:
    from security_support import client

    server.after_apply.append(httpx.ReadTimeout("合成の応答の喪失"))
    _script(
        monkeypatch,
        [function_call(FUNCTION, ARGS, call_id="call-1")],
        [assistant_message("結果が分かりません。")],
    )
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    [step] = runtime_repository.get_run(run_id).steps
    assert step.tool_call is not None

    refused = client.post(f"/api/runs/{run_id}/replay")
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error_code"] == "run_outcome_unknown"
    assert body["error_details"]["steps"] == [
        {
            "step_id": step.id,
            "tool_name": FUNCTION,
            "operation_key": step.tool_call.operation_key,
        }
    ]

    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda _run: None)
    confirmed = client.post(f"/api/runs/{run_id}/replay?confirm_outcome_unknown=true")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["metadata"]["replayed_from_run_id"] == run_id


# ---- 通信の層の分類と操作の ID ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "unknown"),
    [
        (httpx.ReadTimeout("t"), True),
        (httpx.RemoteProtocolError("p"), True),
        (500, True),
        (502, True),
        (504, True),
        (400, False),
    ],
    ids=["read-timeout", "disconnect", "500", "502", "504", "400"],
)
def test_failures_after_delivery_are_marked_unknown(
    server: _TicketMcp, failure: Exception | int, unknown: bool
) -> None:
    server.after_apply.append(failure)
    config = runtime_config_store.get_mcp(SERVER_ID)
    client = tools_module.McpConnectionClient(
        config, context=tools_module.ToolInvocationContext(user_uuid=USER_UUID)
    )
    with pytest.raises(ExternalToolError) as raised:
        client.call_tool(TOOL, ARGS, operation_key="op_y")

    assert (raised.value.details.get("outcome") == OUTCOME_UNKNOWN) is unknown
    assert server.received == [(ARGS, "op_y")]


def test_operation_key_is_bound_to_run_user_tool_arguments_and_call() -> None:
    base: dict[str, Any] = {
        "run_id": "run-1",
        "user_uuid": USER_UUID,
        "tool_name": FUNCTION,
        "arguments": {"b": 1, "a": 2},
        "call_id": "call-1",
    }
    key = external_writes.operation_key(**base)
    assert key == external_writes.operation_key(**{**base, "arguments": {"a": 2, "b": 1}})
    for change in (
        {"run_id": "run-2"},
        {"user_uuid": "other-user"},
        {"tool_name": "other_tool"},
        {"arguments": {"a": 3, "b": 1}},
        {"call_id": "call-2"},
    ):
        assert external_writes.operation_key(**{**base, **change}) != key
