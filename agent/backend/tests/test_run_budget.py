"""Run 全体の実行予算（モデルの呼び出し・token・稼働時間。#68）の決定論テスト。

モデルは SDK の `ScriptedModel`、ツールは合成の handler、保存先は memory、時計は差し替える。
外部への通信はしない。限界直前・同時の予約・usage の欠測・失敗・取消・承認の後の再開を扱う。
差し戻しが同じ予算に積むことは `test_tool_grounding.py` が確かめる。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime, run_budget
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.run_budget import (
    RUN_BUDGET_EXCEEDED_CODE,
    RUN_BUDGET_METADATA_KEY,
    RunBudget,
    RunBudgetLimits,
)
from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import (
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicy,
    tool_registry,
)
from app.settings import get_settings

READ = "test68_read"
WRITE = "test68_write"
SKILL_ID = "test68-skill"
AGENT_ID = "test68-agent"
USER_UUID = "11111111-2222-3333-4444-555555550068"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---- 予算の数え方（単体） ----------------------------------------------------------------------


def _usage(total: int) -> Usage:
    return Usage(requests=1, input_tokens=total - 10, output_tokens=10, total_tokens=total)


def test_limits_of_zero_record_consumption_without_stopping() -> None:
    budget = RunBudget(RunBudgetLimits())
    for _ in range(50):
        assert budget.reserve_model_call(1000) is None
        budget.settle(_usage(1200))
    assert budget.reserve_tool_call() is None
    snapshot = budget.snapshot()
    assert snapshot["consumed"]["model_calls"] == 50
    assert snapshot["consumed"]["total_tokens"] == 60000
    assert snapshot["limits"] == {"model_calls": None, "total_tokens": None, "active_seconds": None}
    assert snapshot["remaining"]["model_calls"] is None
    assert snapshot["exceeded"] is None


def test_model_call_limit_allows_the_last_call_and_refuses_the_next() -> None:
    budget = RunBudget(RunBudgetLimits(model_calls=2))
    assert budget.reserve_model_call() is None
    assert budget.reserve_tool_call() is None
    assert budget.reserve_model_call() is None
    # 最後のモデルの呼び出しの後のツールは、結果を読む呼び出しが残らないので実行しない。
    assert budget.reserve_tool_call() == {
        "scope": "model_calls",
        "limit": 2,
        "used": 2,
        "at": "tool_call",
    }
    assert budget.reserve_model_call() is not None
    assert budget.model_calls == 2
    assert budget.snapshot()["remaining"]["model_calls"] == 0


def test_token_reservation_refuses_a_call_that_would_cross_the_limit() -> None:
    """限界直前: 入力の見積もりを足すと超える呼び出しは呼ばない。収まる呼び出しは呼ぶ。"""
    budget = RunBudget(RunBudgetLimits(total_tokens=1000))
    assert budget.reserve_model_call(500) is None
    budget.settle(_usage(800))
    refused = budget.reserve_model_call(201)
    assert refused is not None
    assert (refused["scope"], refused["used"], refused["reserved"]) == ("total_tokens", 800, 201)
    # 止めた理由は最初の 1 つのまま（見積もりが小さい次の呼び出しでも再開しない）。
    assert budget.reserve_model_call(10) == refused
    assert budget.model_calls == 1


def test_token_reservation_at_exactly_the_limit_is_allowed() -> None:
    budget = RunBudget(RunBudgetLimits(total_tokens=1000))
    budget.reserve_model_call(0)
    budget.settle(_usage(800))
    assert budget.reserve_model_call(200) is None


def test_missing_usage_counts_the_reserved_estimate_and_is_not_exact() -> None:
    budget = RunBudget(RunBudgetLimits(total_tokens=1000))
    budget.reserve_model_call(300)
    budget.settle(Usage(requests=1))
    budget.reserve_model_call(100)
    budget.settle(None)
    budget.reserve_model_call(100)
    budget.settle(_usage(150))
    snapshot = budget.snapshot()
    assert snapshot["consumed"]["measured_tokens"] == 150
    assert snapshot["consumed"]["estimated_tokens"] == 400
    assert snapshot["consumed"]["unmeasured_calls"] == 2
    assert snapshot["consumed"]["total_tokens"] == 550
    assert snapshot["tokens_exact"] is False
    assert "usage の無い 2 回の見積もり 400" in run_budget.exceeded_message(
        {"scope": "total_tokens", "limit": 1000}, budget
    )


def test_parallel_tool_calls_after_exhaustion_are_all_refused() -> None:
    """同時の予約: 同じ応答の並列の呼び出しは、どれも予算に達した後なら実行しない。"""
    budget = RunBudget(RunBudgetLimits(total_tokens=1000))
    budget.reserve_model_call(100)
    budget.settle(_usage(1000))
    assert budget.reserve_tool_call() is not None
    assert budget.reserve_tool_call() is not None
    assert budget.snapshot()["operations"] == {"not_executed": 2, "unknown_result": 0}


def test_active_time_continues_across_executions_without_the_wait(monkeypatch: MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(run_budget, "monotonic", clock)
    first = RunBudget.for_run({}, RunBudgetLimits(active_seconds=30))
    clock.now += 12
    metadata = {RUN_BUDGET_METADATA_KEY: first.snapshot()}
    # 承認を待つ 1 時間は実行の外（数えない）。
    clock.now += 3600
    second = RunBudget.for_run(metadata, RunBudgetLimits(active_seconds=30))
    clock.now += 10
    assert second.executions == 2
    assert second.active_seconds == pytest.approx(22)
    assert second.reserve_model_call() is None
    clock.now += 8
    exceeded = second.reserve_model_call()
    assert exceeded is not None and exceeded["scope"] == "active_seconds"


# ---- Run の実行 -------------------------------------------------------------------------------


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_call: Any = None

    def of(self, name: str) -> list[dict[str, Any]]:
        return [arguments for item, arguments in self.calls if item == name]

    def handler(self, name: str) -> Any:
        def handle(arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            self.calls.append((name, dict(arguments)))
            if self.on_call is not None:
                self.on_call(context)
            return {"name": name, "cursor": arguments.get("cursor"), "items": ["a"]}

        return handle


@pytest.fixture
def tools(monkeypatch: MonkeyPatch) -> Iterator[_Handler]:
    recorded = _Handler()
    monkeypatch.setattr(builtin_runtime, "_active_policy", lambda: ToolPolicy(ask={WRITE}))
    for name, level in ((READ, ToolPermissionLevel.READ), (WRITE, ToolPermissionLevel.WRITE)):
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
                permission_level=level,
                side_effects=level == ToolPermissionLevel.WRITE,
            ),
            recorded.handler(name),
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="予算のテストの Skill",
            instructions="一覧を読んで書く。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="control-plane", tool_names=[READ, WRITE])
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="予算のテストの業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield recorded
    finally:
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


def _limits(monkeypatch: MonkeyPatch, **values: float) -> None:
    for key in ("model_calls", "total_tokens", "active_seconds"):
        monkeypatch.setattr(get_settings(), f"agent_run_max_{key}", values.get(key, 0))


def _script(monkeypatch: MonkeyPatch, *steps: Any, usage: Usage | None = None) -> ScriptedModel:
    model = ScriptedModel(list(steps), default_usage=usage or _usage(100))
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


def _create_run() -> str:
    return runtime_repository.create_builtin_run(
        RunCreateRequest(goal="一覧を読んで書いて", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    ).id


def _execute() -> RunState:
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    return runtime_repository.get_run(run_id)


def _read(cursor: int) -> list[Any]:
    return [function_call(READ, json.dumps({"cursor": cursor}), call_id=f"call-{cursor}")]


def _failure(run: RunState) -> tuple[str, str]:
    [failed] = [event for event in run.events if event.type == "runtime.failed"]
    return str(failed.payload.get("error_code")), failed.message


def _budget(run: RunState) -> dict[str, Any]:
    recorded: dict[str, Any] = run.metadata[RUN_BUDGET_METADATA_KEY]
    return recorded


def test_run_without_limits_records_the_baseline(monkeypatch: MonkeyPatch, tools: _Handler) -> None:
    _limits(monkeypatch)
    _script(monkeypatch, _read(0), _read(1), [assistant_message("2 件です。")])

    run = _execute()

    assert run.status == RunStatus.COMPLETED
    consumed = _budget(run)["consumed"]
    assert (consumed["model_calls"], consumed["total_tokens"], consumed["executions"]) == (
        3,
        300,
        1,
    )
    assert _budget(run)["tokens_exact"] is True


def test_model_call_limit_stops_the_run_and_the_tool_after_it(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    _limits(monkeypatch, model_calls=2)
    model = _script(monkeypatch, _read(0), _read(1), [assistant_message("2 件です。")])

    run = _execute()

    assert run.status == RunStatus.FAILED
    code, message = _failure(run)
    assert code == RUN_BUDGET_EXCEEDED_CODE
    # 2 回目の応答の呼び出しは、結果を読むモデルの呼び出しが残らないので実行しない。
    assert tools.of(READ) == [{"cursor": 0}]
    assert len(model.calls) == 2
    recorded = _budget(run)
    assert recorded["exceeded"]["scope"] == "model_calls"
    assert recorded["consumed"]["model_calls"] == 2
    assert recorded["remaining"]["model_calls"] == 0
    assert recorded["operations"] == {"not_executed": 1, "unknown_result": 0}
    assert "モデルの呼び出し 2 回" in message
    assert "1 件は実行していません" in message


def test_parallel_calls_in_one_response_share_the_token_budget(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    _limits(monkeypatch, total_tokens=20000)
    model = _script(
        monkeypatch,
        ModelStep(
            output=[*_read(0), *_read(1)],
            usage=Usage(requests=1, input_tokens=19990, output_tokens=20, total_tokens=20010),
        ),
        [assistant_message("2 件です。")],
    )

    run = _execute()

    assert run.status == RunStatus.FAILED
    assert _failure(run)[0] == RUN_BUDGET_EXCEEDED_CODE
    assert tools.calls == []
    assert len(model.calls) == 1
    recorded = _budget(run)
    assert recorded["exceeded"]["scope"] == "total_tokens"
    assert recorded["consumed"]["total_tokens"] == 20010
    assert recorded["operations"]["not_executed"] == 2


def test_missing_usage_is_counted_by_the_estimate(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    _limits(monkeypatch, total_tokens=1_000_000)
    _script(monkeypatch, _read(0), [assistant_message("1 件です。")], usage=Usage())

    run = _execute()

    assert run.status == RunStatus.COMPLETED
    recorded = _budget(run)
    assert recorded["consumed"]["unmeasured_calls"] == 2
    assert recorded["consumed"]["measured_tokens"] == 0
    # 入力の見積もり（指示・ツールの定義・会話）を消費にした。
    assert recorded["consumed"]["estimated_tokens"] > 0
    assert recorded["tokens_exact"] is False


def test_approval_resume_continues_the_budget(monkeypatch: MonkeyPatch, tools: _Handler) -> None:
    """承認を待つ時間は数えず、再開しても消費を 0 に戻さない。"""
    clock = _Clock()
    monkeypatch.setattr(run_budget, "monotonic", clock)
    _limits(monkeypatch, model_calls=2)
    write = [function_call(WRITE, json.dumps({"cursor": 1}), call_id="call-w")]
    model = _script(monkeypatch, _read(0), write, [assistant_message("書きました。")])
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    assert _budget(waiting)["consumed"]["model_calls"] == 2
    [approval] = waiting.approvals
    clock.now += 3600
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    # 承認した呼び出しは実行するが、結果を読むモデルの呼び出しは予算を超えるので呼ばない。
    assert run.status == RunStatus.FAILED
    assert _failure(run)[0] == RUN_BUDGET_EXCEEDED_CODE
    assert len(model.calls) == 2
    consumed = _budget(run)["consumed"]
    assert (consumed["model_calls"], consumed["executions"]) == (2, 2)
    assert consumed["active_seconds"] < 60


def test_approval_resume_within_the_budget_completes(
    monkeypatch: MonkeyPatch, tools: _Handler
) -> None:
    _limits(monkeypatch, model_calls=3)
    write = [function_call(WRITE, json.dumps({"cursor": 1}), call_id="call-w")]
    _script(monkeypatch, _read(0), write, [assistant_message("書きました。")])
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))

    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert tools.of(WRITE) == [{"cursor": 1}]
    consumed = _budget(run)["consumed"]
    assert (consumed["model_calls"], consumed["total_tokens"], consumed["executions"]) == (
        3,
        300,
        2,
    )
    assert _budget(run)["remaining"]["model_calls"] == 0


def test_cancelled_run_keeps_the_consumption(monkeypatch: MonkeyPatch, tools: _Handler) -> None:
    _limits(monkeypatch, model_calls=10)
    _script(monkeypatch, _read(0), _read(1), [assistant_message("2 件です。")])
    run_id = _create_run()
    tools.on_call = lambda _context: runtime_repository.cancel_run(run_id)

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    assert tools.of(READ) == [{"cursor": 0}]
    assert _budget(run)["consumed"]["model_calls"] == 1
    assert _budget(run)["exceeded"] is None


def test_failed_model_call_keeps_the_consumption(monkeypatch: MonkeyPatch, tools: _Handler) -> None:
    _limits(monkeypatch, model_calls=10)
    _script(monkeypatch, _read(0), RuntimeError("model down"))

    run = _execute()

    assert run.status == RunStatus.FAILED
    assert _failure(run)[0] == "runtime.model_failed"
    # 失敗した 2 回目も予約した呼び出しとして数える（usage は無い）。
    consumed = _budget(run)["consumed"]
    assert consumed["model_calls"] == 2
    assert consumed["measured_tokens"] == 100
