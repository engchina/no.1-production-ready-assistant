"""ツールを呼び先へ送る直前の境界（取消 → 引数 → ツール権限・承認 → handler）の決定論テスト。

- #2609: 取消が確定した Run では新しいツールの step を作らず、handler を呼ばない（別のプロセスの
  取消も保存先で判定する）。取消の前に送った操作の結果は事実のまま記録する。
- #2610: 承認が要るかを構築の時点で固定せず、実行の直前の最新のツール権限で決める。「承認」の
  ツールは、この Run・呼び出し ID・ツール・引数の承認の記録がある呼び出しだけを実行する。
- #2630: 壊れた JSON・object 以外を空の引数にせず、モデルに見せた schema で実行の前に確かめる。

モデルは SDK の `ScriptedModel`、ツールは合成の handler、保存先は memory（別のプロセスは偽の
Oracle の表を共有する 2 つの repository）。外部への通信はしない。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from agents import Agent, FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from checkpoint_support import CheckpointTable
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime, run_claim
from app.features.agent.builtin_runtime import (
    APPROVAL_REQUIRED_CODE,
    RUN_CANCELLED_CODE,
    ModelTarget,
)
from app.features.agent.config import runtime_config_store
from app.features.agent.runtime import (
    AUTOMATION_APPROVAL_SKIPPED_CODE,
    AgentProfile,
    AgentRuntimeOracleCheckpointRepository,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunNotActiveError,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tool_arguments import (
    INVALID_ARGUMENTS_CODE,
    SchemaUnsupported,
    parse_arguments,
    validate_arguments,
)
from app.features.agent.tools import (
    ToolCall,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicy,
    ToolResult,
    tool_registry,
)

READ = "test2630_read"
WRITE = "test2630_write"
SKILL_ID = "test2630-skill"
AGENT_ID = "test2630-agent"
USER_UUID = "11111111-2222-3333-4444-555555552630"


def _count_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"],
        "additionalProperties": False,
    }


class _Calls:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any], ToolInvocationContext]] = []
        # 読み取りの handler の中で行う操作（実行中に利用者が取り消した、を再現する）。
        self.on_read: Any = None

    def of(self, name: str) -> list[dict[str, Any]]:
        return [arguments for item, arguments, _ in self.items if item == name]

    def handler(self, name: str) -> Any:
        def handle(arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            self.items.append((name, dict(arguments), context))
            if name == READ and self.on_read is not None:
                self.on_read(context)
            return {"name": name, "ok": True}

        return handle


@pytest.fixture
def calls() -> Iterator[_Calls]:
    recorded = _Calls()
    original = runtime_config_store.get_tool_policy()
    for name, level, side_effects in (
        (READ, ToolPermissionLevel.READ, False),
        (WRITE, ToolPermissionLevel.WRITE, True),
    ):
        tool_registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} の合成のツール",
                input_schema=_count_schema(),
                output_schema={"type": "object"},
                permission_level=level,
                side_effects=side_effects,
            ),
            recorded.handler(name),
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="境界のテストの Skill",
            instructions="件数を読んでから書く。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="control-plane", tool_names=[READ, WRITE])
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="境界のテストの業務 Agent", skill_ids=[SKILL_ID])
    )
    # 書き込みのツールは検証のために「自動実行」から始める（承認の有無とは別に境界を試す）。
    runtime_config_store.patch_tool_policy(allow=[WRITE], ask=[], deny=[])
    try:
        yield recorded
    finally:
        runtime_config_store.patch_tool_policy(
            default_mode=original.default_mode,
            allow=sorted(original.allow),
            ask=sorted(original.ask),
            deny=sorted(original.deny),
        )
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        for name in (READ, WRITE):
            tool_registry._definitions.pop(name, None)  # noqa: SLF001 - テスト用の登録を戻す
            tool_registry._handlers.pop(name, None)  # noqa: SLF001


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _create_run(goal: str = "件数を読んで書いて", **metadata: Any) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID, metadata=metadata),
        created_by_user_uuid=USER_UUID,
    )
    return run.id


def _running_run() -> str:
    run_id = _create_run()
    assert runtime_repository.begin_builtin_run(run_id) is not None
    return run_id


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _tools(run_id: str) -> dict[str, FunctionTool]:
    return {tool.name: tool for tool in builtin_runtime.build_function_tools(run_id, [READ, WRITE])}


async def _invoke(tool: FunctionTool, arguments: str, call_id: str) -> str:
    output = await tool.on_invoke_tool(SimpleNamespace(tool_call_id=call_id), arguments)  # type: ignore[arg-type]
    assert isinstance(output, str)
    return output


def _ask_after_build(monkeypatch: MonkeyPatch) -> None:
    """ツールを作った直後（モデルが呼ぶ前）に書き込みのツールを「自動実行」から「承認」へ変える
    （実行中のツール権限の変更を再現する）。"""
    original = builtin_runtime.build_function_tools

    def build(*args: Any, **kwargs: Any) -> list[FunctionTool]:
        tools = original(*args, **kwargs)
        runtime_config_store.patch_tool_policy(allow=[], ask=[WRITE])
        return tools

    monkeypatch.setattr(builtin_runtime, "build_function_tools", build)


# ---- #2630: 引数の検証 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ('{"count":"v-2630-secret"}', "type"),
        ("{}", "required"),
        ('{"count":0}', "minimum"),
        ('{"count":1,"extra":true}', "additional_properties"),
        ('{"count":', "invalid_json"),
        ("[]", "not_object"),
    ],
)
def test_invalid_arguments_never_reach_the_handler(
    monkeypatch: MonkeyPatch, calls: _Calls, arguments: str, code: str
) -> None:
    model = _script(
        monkeypatch,
        [function_call(WRITE, arguments, call_id="call-bad")],
        [assistant_message("引数が誤っていたため書いていません。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert calls.of(WRITE) == []
    # 承認に回さない（誤った引数の承認を人に求めない）。
    assert run.approvals == []
    output = _tool_output(model, 1, "call-bad")
    assert output["error_code"] == INVALID_ARGUMENTS_CODE
    assert [item["code"] for item in output["argument_errors"]] == [code]
    # 値は誤りの文に写さない（#2611）。
    assert "v-2630-secret" not in json.dumps(output, ensure_ascii=False)
    [step] = run.steps
    assert step.status == "failed"
    assert step.tool_result is not None
    assert step.tool_result.success is False
    assert step.tool_result.error_code == INVALID_ARGUMENTS_CODE


def test_valid_arguments_run_once_and_a_fixed_retry_runs(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """正しい対照は 1 回実行し、誤りを受けて直した呼び出しだけを実行する。"""
    _script(
        monkeypatch,
        [function_call(WRITE, '{"count":0}', call_id="call-1")],
        [function_call(WRITE, '{"count":2}', call_id="call-2")],
        [assistant_message("2 件を書きました。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert calls.of(WRITE) == [{"count": 2}]
    assert [step.status for step in run.steps] == ["failed", "completed"]
    completed = run.steps[1].tool_result
    assert completed is not None
    assert completed.audit_metadata["argument_validation"] == "passed"


def test_unreadable_arguments_of_an_approval_tool_are_not_sent_for_approval(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """SDK は読めない引数を承認待ちにするが、人に承認を求めず引数の誤りとして返す。"""
    runtime_config_store.patch_tool_policy(allow=[], ask=[WRITE])
    model = _script(
        monkeypatch,
        [function_call(WRITE, '{"count":', call_id="call-broken")],
        [assistant_message("書いていません。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.approvals == []
    assert calls.of(WRITE) == []
    assert INVALID_ARGUMENTS_CODE in str(model.calls[1].input)


def test_empty_arguments_of_an_allowed_tool_without_parameters_run(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """引数なし（空の文字列）は `{}` として扱い、自動実行のツールは承認なしで実行する。"""
    del calls
    seen: list[dict[str, Any]] = []
    name = "test2630_ping"

    def ping(arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        seen.append(dict(arguments))
        return {"ok": True}

    tool_registry.register(
        ToolDefinition(
            name=name,
            description="引数の無い合成のツール",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
        ),
        ping,
    )
    try:
        run_id = _running_run()
        agent = Agent(
            name="ping",
            instructions="",
            tools=list(builtin_runtime.build_function_tools(run_id, [name])),
            model=ScriptedModel(
                [[function_call(name, "", call_id="call-ping")], [assistant_message("ok")]]
            ),
        )
        result = anyio.run(builtin_runtime._run_sdk, run_id, agent, "ping")  # noqa: SLF001
    finally:
        tool_registry._definitions.pop(name, None)  # noqa: SLF001
        tool_registry._handlers.pop(name, None)  # noqa: SLF001
    assert not list(result.interruptions)
    assert seen == [{}]


def test_registered_tool_contract_is_checked_by_the_registry(calls: _Calls) -> None:
    """`tool_registry.invoke` も呼び先の契約で引数を確かめる（MCP・登録済みツールの共通の境界）。"""
    result = tool_registry.invoke(
        ToolCall(name=READ, arguments={"count": "1"}),
        policy=ToolPolicy(allow={READ}),
        context=ToolInvocationContext(),
    )
    assert result.success is False
    assert result.error_code == INVALID_ARGUMENTS_CODE
    assert result.error_details["argument_errors"][0]["argument"] == "count"
    assert result.audit_metadata["argument_validation"] == "failed"
    assert calls.of(READ) == []


def test_hidden_arguments_are_not_required_from_the_model() -> None:
    """モデルに見せていない引数（実行環境が埋める）は、モデルの引数の検証で求めない。"""
    contract = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "profile_id": {"type": "string"}},
        "required": ["query", "profile_id"],
        "additionalProperties": False,
    }
    visible = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    from app.features.agent.tool_arguments import hidden_arguments

    hidden = hidden_arguments(contract, visible)
    assert hidden == frozenset({"profile_id"})
    # モデルが渡しても実行環境が上書きする引数は、モデルの引数の誤りにしない。
    assert validate_arguments(visible, {"query": "q", "profile_id": "x"}, ignore=hidden) == []
    assert validate_arguments(contract, {"query": "q", "profile_id": "x"}) == []


def test_external_refs_are_not_fetched(monkeypatch: MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("外部の $ref を取りに行った")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    checked = validate_arguments(
        {
            "type": "object",
            "properties": {"a": {"$ref": "https://schemas.example/remote.json"}},
        },
        {"a": 1},
    )
    assert isinstance(checked, SchemaUnsupported)
    assert checked.reason == "unresolvable_ref"
    # 同じ schema の中の $ref は解決する。
    local = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A"}},
        "$defs": {"A": {"type": "string"}},
    }
    [error] = validate_arguments(local, {"a": 1})  # type: ignore[misc]
    assert (error.argument, error.code) == ("a", "type")


def test_argument_limits_and_messages_do_not_echo_values() -> None:
    nested: Any = "x"
    for _ in range(70):
        nested = {"n": nested}
    [deep] = validate_arguments({"type": "object"}, {"n": nested})  # type: ignore[misc]
    assert deep.code == "too_deep"
    secret = "s3cr3t-token-value"
    errors = validate_arguments(_count_schema(), {"count": secret, secret: True})
    assert isinstance(errors, list)
    text = json.dumps([item.as_dict() for item in errors], ensure_ascii=False)
    assert secret not in text.replace(f'"argument": "{secret}"', "")
    parsed, parse_errors = parse_arguments('{"count": NaN}')
    assert parsed is None
    assert [item.code for item in parse_errors] == ["invalid_json"]


# ---- #2610: 実行の直前の最新のツール権限 -------------------------------------------------------


def test_policy_changed_to_ask_after_build_waits_for_approval(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """自動実行で構築 → 承認に変更 → 新しい呼び出し は承認待ち。承認した後だけ 1 回実行する。"""
    _ask_after_build(monkeypatch)
    _script(
        monkeypatch,
        [function_call(WRITE, {"count": 3}, call_id="call-w")],
        [assistant_message("3 件を書きました。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    assert calls.of(WRITE) == []
    [approval] = waiting.approvals
    assert approval.tool_call.trace_id == "call-w"

    runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by="approver-2630")
    )
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert calls.of(WRITE) == [{"count": 3}]
    [(_, _, context)] = [item for item in calls.items if item[0] == WRITE]
    assert context.approval_id == approval.id
    [step] = run.steps
    assert step.tool_call is not None and step.tool_call.trace_id == "call-w"
    assert step.tool_result is not None
    # 監査に実際の判断を残す（承認で実行した「承認」のツール）。
    assert step.tool_result.policy_decision == "ask"
    assert step.tool_result.audit_metadata["authorization"] == "approval"
    assert step.tool_result.audit_metadata["force"] is True


def test_policy_changed_to_ask_is_rejected_in_unattended_runs(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """無人の実行（自動実行）は承認を待たず、承認が要るツールを呼ばない（既存の拒否の方針）。"""
    _ask_after_build(monkeypatch)
    _script(
        monkeypatch,
        [function_call(WRITE, {"count": 3}, call_id="call-auto")],
        [assistant_message("書けませんでした。")],
    )
    run_id = _create_run(automation_id="auto_2630", automation_trigger="schedule")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.approvals == []
    assert calls.of(WRITE) == []
    [step] = run.steps
    assert step.tool_result is not None
    assert step.tool_result.error_code == AUTOMATION_APPROVAL_SKIPPED_CODE


def test_parallel_calls_each_wait_for_approval(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    _ask_after_build(monkeypatch)
    _script(
        monkeypatch,
        [
            function_call(WRITE, {"count": 1}, call_id="call-p1"),
            function_call(WRITE, {"count": 2}, call_id="call-p2"),
        ],
        [assistant_message("書きました。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.WAITING_APPROVAL
    assert sorted(item.tool_call.trace_id or "" for item in run.approvals) == ["call-p1", "call-p2"]
    assert calls.of(WRITE) == []


def test_policy_changed_to_deny_after_approval_is_not_executed(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    runtime_config_store.patch_tool_policy(allow=[], ask=[WRITE])
    _script(
        monkeypatch,
        [function_call(WRITE, {"count": 1}, call_id="call-d")],
        [assistant_message("書けませんでした。")],
    )
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))
    runtime_config_store.patch_tool_policy(ask=[], deny=[WRITE])

    anyio.run(builtin_runtime.resume_run, run_id)

    assert calls.of(WRITE) == []


def test_approval_is_required_at_the_tool_boundary(calls: _Calls) -> None:
    """SDK の判定の後に「承認」へ変わった呼び出しも、承認の記録が無ければ実行しない。"""
    run_id = _running_run()
    tools = _tools(run_id)
    runtime_config_store.patch_tool_policy(allow=[], ask=[WRITE])

    output = json.loads(anyio.run(_invoke, tools[WRITE], '{"count":1}', "call-late"))

    assert output["error_code"] == APPROVAL_REQUIRED_CODE
    assert calls.of(WRITE) == []
    [step] = runtime_repository.get_run(run_id).steps
    assert step.tool_result is not None
    assert step.tool_result.approval_required is True
    assert step.tool_result.policy_decision == "ask"


def test_approval_is_bound_to_the_call_id_and_arguments(calls: _Calls) -> None:
    """承認の記録は同じ呼び出し ID・ツール・引数にだけ効く（別の操作へ流用しない）。"""
    runtime_config_store.patch_tool_policy(allow=[], ask=[WRITE])
    run_id = _running_run()
    tools = _tools(run_id)
    runtime_repository.request_builtin_approvals(
        run_id, [ToolCall(name=WRITE, arguments={"count": 1}, trace_id="call-a")], state="{}"
    )
    [approval] = runtime_repository.get_run(run_id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))

    other_arguments = json.loads(anyio.run(_invoke, tools[WRITE], '{"count":9}', "call-a"))
    other_call = json.loads(anyio.run(_invoke, tools[WRITE], '{"count":1}', "call-b"))
    approved = json.loads(anyio.run(_invoke, tools[WRITE], '{"count":1}', "call-a"))

    assert other_arguments["error_code"] == APPROVAL_REQUIRED_CODE
    assert other_call["error_code"] == APPROVAL_REQUIRED_CODE
    assert approved == {"name": WRITE, "ok": True}
    assert calls.of(WRITE) == [{"count": 1}]


# ---- #2609: 取消の後に新しいツールを始めない -------------------------------------------------


def test_cancel_during_a_read_stops_the_following_write(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """読み取り中の取消 → 結果の復帰 → モデルが書き込みを求める、で書き込みの handler は 0 回。"""
    calls.on_read = lambda context: runtime_repository.cancel_run(context.run_id)
    model = _script(
        monkeypatch,
        [function_call(READ, {"count": 1}, call_id="call-r")],
        [function_call(WRITE, {"count": 1}, call_id="call-w")],
        [assistant_message("書きました。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    assert calls.of(WRITE) == []
    # 取消の後はモデルを呼ばない。取消の前に送った読み取りの結果は事実のまま残す。
    assert len(model.calls) == 1
    [step] = run.steps
    assert step.tool_call is not None and step.tool_call.name == READ
    assert step.status == "completed"
    completed = next(event for event in run.events if event.type == "tool.completed")
    assert completed.payload["run_status"] == "cancelled"
    cancelled = next(event for event in run.events if event.type == "run.cancelled")
    assert cancelled.payload["in_flight_step_ids"] == [step.id]
    # 失敗ではなく取消のまま（取消の後の停止を失敗として記録しない）。
    assert not [event for event in run.events if event.type == "runtime.failed"]


def test_tool_call_after_cancel_creates_no_step(calls: _Calls) -> None:
    run_id = _running_run()
    tools = _tools(run_id)
    runtime_repository.cancel_run(run_id)

    output = json.loads(anyio.run(_invoke, tools[WRITE], '{"count":1}', "call-after"))

    assert output["error_code"] == RUN_CANCELLED_CODE
    assert calls.of(WRITE) == []
    assert runtime_repository.get_run(run_id).steps == []


def test_start_step_after_cancel_is_refused_but_cleanup_is_allowed(calls: _Calls) -> None:
    del calls
    run_id = _running_run()
    runtime_repository.cancel_run(run_id)

    with pytest.raises(RunNotActiveError):
        runtime_repository.start_builtin_tool_step(run_id, ToolCall(name=WRITE, trace_id="c1"))
    step_id, _context = runtime_repository.start_builtin_tool_step(
        run_id, ToolCall(name=READ, trace_id="c2"), cleanup=True
    )
    assert [step.id for step in runtime_repository.get_run(run_id).steps] == [step_id]


@pytest.mark.parametrize("success", [True, False])
def test_late_result_after_cancel_is_recorded_as_it_happened(calls: _Calls, success: bool) -> None:
    """取消の前に始めた操作の遅れて届いた成功・失敗は、事実のまま記録する（巻き戻ったとしない）。"""
    del calls
    run_id = _running_run()
    started, _ = runtime_repository.start_builtin_tool_step(
        run_id, ToolCall(name=WRITE, arguments={"count": 1}, trace_id="c-late")
    )
    runtime_repository.cancel_run(run_id)

    runtime_repository.finish_builtin_tool_step(
        run_id,
        started,
        ToolResult(name=WRITE, success=success, output={"ok": True} if success else None),
    )

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    [step] = run.steps
    assert step.status == ("completed" if success else "failed")
    event = run.events[-1]
    assert event.type == ("tool.completed" if success else "tool.failed")
    assert event.payload["run_status"] == "cancelled"


def test_approved_call_is_not_run_after_cancel(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    runtime_config_store.patch_tool_policy(allow=[], ask=[WRITE])
    _script(
        monkeypatch,
        [function_call(WRITE, {"count": 1}, call_id="call-c")],
        [assistant_message("書きました。")],
    )
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))
    runtime_repository.cancel_run(run_id)

    anyio.run(builtin_runtime.resume_run, run_id)

    assert runtime_repository.get_run(run_id).status == RunStatus.CANCELLED
    assert calls.of(WRITE) == []


# ---- #2609: 別のプロセスの取消（偽の Oracle の表を共有する 2 つの repository） ------------------


def _stored_run(run_id: str) -> RunState:
    return RunState(
        id=run_id,
        goal="合成の質問",
        agent_id="default",
        runtime_id="builtin",
        status=RunStatus.RUNNING,
    )


def _process(table: CheckpointTable) -> AgentRuntimeOracleCheckpointRepository:
    return AgentRuntimeOracleCheckpointRepository(connect_factory=table.connect)


def test_cancel_by_another_process_is_seen_before_the_next_tool() -> None:
    table = CheckpointTable()
    table.seed([_stored_run("run_x")])
    worker = _process(table)
    api = _process(table)
    assert worker.builtin_run_ended("run_x") is False

    api.cancel_run("run_x", cancelled_by="user-a")

    assert worker.builtin_run_ended("run_x") is True
    with pytest.raises(RunNotActiveError):
        worker.start_builtin_tool_step("run_x", ToolCall(name=WRITE, trace_id="c1"))
    cancelled = next(
        event for event in worker.get_run("run_x").events if event.type == "run.cancelled"
    )
    assert cancelled.payload["cancelled_by"] == "user-a"


def test_cancel_by_another_process_wins_over_a_stale_write() -> None:
    """古いメモリのプロセスが Run を書いても、別のプロセスの取消を消さない（取消が勝つ）。"""
    table = CheckpointTable()
    table.seed([_stored_run("run_y")])
    worker = _process(table)
    api = _process(table)

    api.cancel_run("run_y")
    # worker はまだ取消を知らない（メモリは実行中）まま、step を書く。
    step_id, _ = worker.start_builtin_tool_step("run_y", ToolCall(name=READ, trace_id="c1"))

    assert table.snapshot().runs[0].status == RunStatus.CANCELLED
    run = worker.get_run("run_y")
    assert run.status == RunStatus.CANCELLED
    assert [step.id for step in run.steps] == [step_id]


# ---- #2609 と #2606: 置き換わった実行は新しいツールを始めない --------------------------------


def _with_execution(run: RunState, token: str) -> RunState:
    return run.model_copy(
        update={
            "metadata": {
                run_claim.EXECUTION_METADATA_KEY: {"token": token, "generation": 1},
            }
        }
    )


def test_superseded_execution_starts_no_new_tool() -> None:
    """別の実行が始まった後の旧い実行は、取消と同じく新しい step を作らない（#2606 の世代）。"""
    table = CheckpointTable()
    table.seed([_with_execution(_stored_run("run_g"), "token-old")])
    worker = _process(table)
    with run_claim.execution_fence("run_g", worker.get_run("run_g").metadata):
        assert worker.builtin_run_ended("run_g") is False
        # 別のプロセスで次の実行が始まった（保存先の実行の token が変わった）。
        table.seed([_with_execution(_stored_run("run_g"), "token-new")], version=5)
        assert worker.builtin_run_ended("run_g") is True
    # 同じプロセスのメモリで実行が置き換わったときも、step を作らない。
    memory = _process(CheckpointTable())
    with memory._lock:  # noqa: SLF001 - テストの準備
        memory._runs["run_m"] = _with_execution(_stored_run("run_m"), "token-new")  # noqa: SLF001
        memory._mark_run_changed("run_m")  # noqa: SLF001
        memory._persist_locked()  # noqa: SLF001
    old = {run_claim.EXECUTION_METADATA_KEY: {"token": "token-old", "generation": 1}}
    with run_claim.execution_fence("run_m", old), pytest.raises(RunNotActiveError):
        memory.start_builtin_tool_step("run_m", ToolCall(name=READ, trace_id="c1"))
    assert memory.get_run("run_m").steps == []
