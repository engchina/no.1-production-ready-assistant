"""進展の無いツールの呼び出し（同じ呼び出しの反復・実行の失敗の連続・引数の誤りの連続）で
Run を終える（#2580）決定論テスト。

モデルは SDK の `ScriptedModel`、ツールは合成の handler、保存先は memory。外部への通信はしない。
正常な反復（結果が変わる polling・引数が変わるページング・並列の呼び出し・失敗の後の成功）は
終えないことも確かめる。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from agents import FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime, tool_loop_guard
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tool_arguments import INVALID_ARGUMENTS_CODE
from app.features.agent.tool_loop_guard import (
    ARGUMENT_ERROR_LIMIT,
    FAILURE_LIMIT,
    REPEAT_LIMIT,
    TOOL_ARGUMENT_ERRORS_REPEATED_CODE,
    TOOL_CALL_REPEATED_CODE,
    TOOL_FAILURES_REPEATED_CODE,
)
from app.features.agent.tools import (
    ExternalToolError,
    ToolCall,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolResult,
    tool_registry,
)

READ = "test2580_read"
OTHER = "test2580_other"
SKILL_ID = "test2580-skill"
AGENT_ID = "test2580-agent"
USER_UUID = "11111111-2222-3333-4444-555555552580"


class _Handler:
    """合成の handler（呼ばれた引数を残し、`behavior` で結果を決める）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.behavior: dict[str, Any] = {}

    def of(self, name: str) -> list[dict[str, Any]]:
        return [arguments for item, arguments in self.calls if item == name]

    def handler(self, name: str) -> Any:
        def handle(arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
            self.calls.append((name, dict(arguments)))
            behavior = self.behavior.get(name, "same")
            if behavior == "fail":
                raise ExternalToolError("test.unavailable", "合成の呼び先が応答しませんでした。")
            if callable(behavior):
                result: dict[str, Any] = behavior(len(self.of(name)))
                return result
            return {"status": "pending", "items": ["a"]}

        return handle


@pytest.fixture
def tools() -> Iterator[_Handler]:
    recorded = _Handler()
    for name in (READ, OTHER):
        tool_registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} の合成のツール",
                input_schema={
                    "type": "object",
                    "properties": {"cursor": {"type": "integer", "minimum": 0}},
                    "required": ["cursor"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permission_level=ToolPermissionLevel.READ,
            ),
            recorded.handler(name),
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="反復のテストの Skill",
            instructions="一覧を読む。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="control-plane", tool_names=[READ, OTHER])
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="反復のテストの業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield recorded
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
        for name in (READ, OTHER):
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


def _run(model_steps: list[Any], monkeypatch: MonkeyPatch) -> tuple[RunState, ScriptedModel]:
    model = _script(monkeypatch, *model_steps)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="一覧を読んで", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id), model


def _call(name: str, arguments: str, call_id: str) -> list[Any]:
    return [function_call(name, arguments, call_id=call_id)]


def _output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _failure_code(run: RunState) -> str | None:
    failed = [event for event in run.events if event.type == "runtime.failed"]
    return str(failed[-1].payload.get("error_code")) if failed else None


# ---- 終える ------------------------------------------------------------------------------------


def test_same_call_with_the_same_result_stops_the_run(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    calls = [_call(READ, '{"cursor":0}', f"call-{index}") for index in range(REPEAT_LIMIT + 1)]
    run, model = _run([*calls, [assistant_message("一覧です。")]], monkeypatch)

    assert run.status == RunStatus.FAILED
    assert _failure_code(run) == TOOL_CALL_REPEATED_CODE
    # 上限を超えて呼び先へ送らない。上限に達した後はモデルを呼ばない。
    assert len(tools.of(READ)) == REPEAT_LIMIT
    assert len(model.calls) == REPEAT_LIMIT
    # 上限の 1 回前の結果で、次も同じなら終えることをモデルへ返す（差し戻し）。
    notice = _output(model, REPEAT_LIMIT - 1, f"call-{REPEAT_LIMIT - 2}")
    assert "loop_notice" in notice
    assert "loop_notice" not in _output(model, 1, "call-0")
    # 画面の失敗の文に理由が出る。引数の値は出さない。
    failed = next(event for event in run.events if event.type == "runtime.failed")
    assert READ in failed.message
    assert "cursor" not in failed.message


def test_consecutive_execution_failures_stop_the_run(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    tools.behavior[READ] = "fail"
    calls = [
        _call(READ, json.dumps({"cursor": index}), f"call-{index}")
        for index in range(FAILURE_LIMIT + 1)
    ]
    run, model = _run([*calls, [assistant_message("取得できませんでした。")]], monkeypatch)

    assert run.status == RunStatus.FAILED
    assert _failure_code(run) == TOOL_FAILURES_REPEATED_CODE
    assert len(tools.of(READ)) == FAILURE_LIMIT
    assert len(model.calls) == FAILURE_LIMIT
    notice = _output(model, FAILURE_LIMIT - 1, f"call-{FAILURE_LIMIT - 2}")
    assert notice["error_code"] == "test.unavailable"
    assert "loop_notice" in notice


def test_consecutive_argument_errors_stop_the_run(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    """引数の誤りの再試行は max_turns ではなく、引数の誤りの連続の上限で止まる。"""
    calls = [
        _call(READ, '{"cursor":-1}', f"call-{index}") for index in range(ARGUMENT_ERROR_LIMIT + 1)
    ]
    run, model = _run([*calls, [assistant_message("読めませんでした。")]], monkeypatch)

    assert run.status == RunStatus.FAILED
    assert _failure_code(run) == TOOL_ARGUMENT_ERRORS_REPEATED_CODE
    assert tools.of(READ) == []
    assert len(model.calls) == ARGUMENT_ERROR_LIMIT
    notice = _output(model, ARGUMENT_ERROR_LIMIT - 1, f"call-{ARGUMENT_ERROR_LIMIT - 2}")
    assert notice["error_code"] == INVALID_ARGUMENTS_CODE
    assert notice["argument_errors"]
    assert "loop_notice" in notice


def test_count_is_rebuilt_from_steps_and_later_calls_are_not_executed(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    """上限に達した呼び出しの後に、別の recorder（再開・再起動）から呼んでも実行しない。

    回数は保存した step から数える（承認の後の再開・再起動で数え直さない）。
    """
    tools.behavior[READ] = "fail"
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="一覧を読んで", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    assert runtime_repository.begin_builtin_run(run.id) is not None

    def build() -> FunctionTool:
        [tool] = [
            item
            for item in builtin_runtime.build_function_tools(run.id, [READ, OTHER])
            if item.name == READ
        ]
        return tool

    async def invoke(tool: FunctionTool, arguments: str, call_id: str) -> dict[str, Any]:
        output = await tool.on_invoke_tool(SimpleNamespace(tool_call_id=call_id), arguments)  # type: ignore[arg-type]
        loaded: dict[str, Any] = json.loads(str(output))
        return loaded

    first = build()
    for index in range(FAILURE_LIMIT):
        anyio.run(invoke, first, json.dumps({"cursor": index}), f"call-{index}")
    # 新しい recorder（再開・再起動の後）でも、保存した step から上限を判定する。
    refused = anyio.run(invoke, build(), '{"cursor":9}', "call-after")

    assert refused["error_code"] == TOOL_FAILURES_REPEATED_CODE
    assert len(tools.of(READ)) == FAILURE_LIMIT
    assert len(runtime_repository.get_run(run.id).steps) == FAILURE_LIMIT


# ---- 正常な反復は終えない ------------------------------------------------------------------------


def test_polling_with_changing_results_is_not_stopped(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    tools.behavior[READ] = lambda count: {"status": "running", "progress": count}
    calls = [_call(READ, '{"cursor":0}', f"call-{index}") for index in range(REPEAT_LIMIT + 2)]
    run, model = _run([*calls, [assistant_message("完了しました。")]], monkeypatch)

    assert run.status == RunStatus.COMPLETED
    assert len(tools.of(READ)) == REPEAT_LIMIT + 2
    assert all("loop_notice" not in str(call.input) for call in model.calls)


def test_paging_with_different_arguments_is_not_stopped(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    """引数（cursor）が変われば、結果が同じでも反復にしない（ページング・別の条件の検索）。"""
    calls = [
        _call(READ, json.dumps({"cursor": index}), f"call-{index}")
        for index in range(REPEAT_LIMIT + 2)
    ]
    run, _model = _run([*calls, [assistant_message("一覧です。")]], monkeypatch)

    assert run.status == RunStatus.COMPLETED
    assert len(tools.of(READ)) == REPEAT_LIMIT + 2


def test_parallel_calls_in_one_turn_are_not_stopped(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    parallel = [
        function_call(READ, json.dumps({"cursor": index}), call_id=f"call-{index}")
        for index in range(REPEAT_LIMIT + 1)
    ]
    run, _model = _run([parallel, [assistant_message("一覧です。")]], monkeypatch)

    assert run.status == RunStatus.COMPLETED
    assert len(tools.of(READ)) == REPEAT_LIMIT + 1


def test_success_after_failures_resets_the_count(monkeypatch: MonkeyPatch, tools: _Handler) -> None:
    def fail_except_middle(count: int) -> dict[str, Any]:
        if count != FAILURE_LIMIT:
            raise ExternalToolError("test.unavailable", "合成の失敗")
        return {"status": "done", "page": count}

    tools.behavior[READ] = fail_except_middle
    total = 2 * FAILURE_LIMIT - 1
    calls = [_call(READ, json.dumps({"cursor": index}), f"call-{index}") for index in range(total)]
    run, _model = _run([*calls, [assistant_message("一覧です。")]], monkeypatch)

    # 失敗 ×(上限 - 1) → 成功 → 失敗 ×(上限 - 1)。連続は成功で数え直す。
    assert run.status == RunStatus.COMPLETED
    assert len(tools.of(READ)) == total


def test_failures_of_different_tools_and_kinds_are_not_summed(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    """別のツールの失敗・実行の失敗と引数の誤りは足さない。"""
    tools.behavior[READ] = "fail"
    tools.behavior[OTHER] = "fail"
    steps: list[Any] = []
    for index in range(FAILURE_LIMIT - 1):
        steps.append(_call(READ, json.dumps({"cursor": index}), f"read-{index}"))
        steps.append(_call(OTHER, json.dumps({"cursor": index}), f"other-{index}"))
    for index in range(ARGUMENT_ERROR_LIMIT - 1):
        steps.append(_call(READ, '{"cursor":-1}', f"bad-{index}"))
    run, _model = _run([*steps, [assistant_message("取得できませんでした。")]], monkeypatch)

    assert run.status == RunStatus.COMPLETED
    assert len(tools.of(READ)) == FAILURE_LIMIT - 1
    assert len(tools.of(OTHER)) == FAILURE_LIMIT - 1


# ---- 数えない step ------------------------------------------------------------------------------


def _step(name: str, result: ToolResult, *, status: StepStatus = StepStatus.FAILED) -> RunStep:
    return RunStep(
        run_id="run-2580",
        status=status,
        tool_call=ToolCall(name=name, arguments={"cursor": 0}),
        tool_result=result,
    )


def test_control_plane_approval_budget_and_running_steps_are_not_counted() -> None:
    failure = ToolResult(name=READ, success=False, error="x", error_code="test.unavailable")
    control_plane = failure.model_copy(
        update={"audit_metadata": {"authorization": "control_plane"}}
    )
    approval = failure.model_copy(update={"approval_required": True, "error_code": None})
    budget = failure.model_copy(update={"error_code": "budget_exceeded"})
    steps = [
        *[_step(READ, control_plane) for _ in range(FAILURE_LIMIT)],
        *[_step(READ, approval) for _ in range(FAILURE_LIMIT)],
        *[_step(READ, budget) for _ in range(FAILURE_LIMIT)],
        *[_step(READ, failure, status=StepStatus.RUNNING) for _ in range(FAILURE_LIMIT)],
    ]
    assert tool_loop_guard.stop_finding(steps) is None
    # 数える失敗だけが上限に達したら終える。
    counted = [_step(READ, failure) for _ in range(FAILURE_LIMIT)]
    finding = tool_loop_guard.stop_finding([*steps, *counted])
    assert finding is not None
    assert finding.code == TOOL_FAILURES_REPEATED_CODE
