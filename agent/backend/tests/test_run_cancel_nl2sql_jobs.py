"""Run の取消で、その Run が始めた NL2SQL のジョブを止める（#3318）の決定論テスト。

- 実行中の Run の取消（#2691 の経路）: 実行している側が Run の終わりに、まだ終わっていない
  ジョブ（取消の後に届いた `nl2sql_query` の結果のジョブも）へ `nl2sql_cancel_job` を Run の
  利用者のサービストークン（ジョブを作ったときと同じ範囲の claim）で送る。
- 実行している側の無い Run（承認の待ち）の取消: 画面・MCP の取消の入口が同じ後始末を予約する。
- 終わったジョブ・取消を送ったジョブには送らない。ポリシーの「拒否」は守る。

モデルは SDK の `ScriptedModel`、NL2SQL は契約どおりの fake の MCP（`mcp_support`）、保存先は
memory。外部への通信はしない。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch
from security_support import client

from app.features.agent import builtin_runtime
from app.features.agent import router as agent_api
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.data_scope import AgentDataScope
from app.features.agent.job_cancel import RUN_CANCEL_TRACE_PREFIX, in_flight_nl2sql_jobs
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    RunStep,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ToolCall, ToolPolicy, ToolResult
from app.settings import get_settings

SKILL_ID = "test3318-skill"
AGENT_ID = "test3318-agent"
USER_UUID = "11111111-2222-3333-4444-555555553318"
PROFILE = "profile-sales"
GOAL = "今月の売上を部門別に教えて"
QUERY = "nl2sql__nl2sql_query"
CANCEL = "nl2sql__nl2sql_cancel_job"


def _running_job(job_id: str = "job-1") -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["nl2sql_query"])
    output.update(
        job_id=job_id, status="running", rows=[], columns=[], returned_count=0, total=None
    )
    return output


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch, outputs={"nl2sql_query": _running_job()})
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    # ジョブの完了を待たずに返す（実行中のジョブを Run の step に残す）。
    monkeypatch.setattr(get_settings(), "agent_nl2sql_job_wait_seconds", 0.0)
    monkeypatch.setattr(
        builtin_runtime,
        "_active_policy",
        lambda: ToolPolicy(allow={QUERY}, ask={"rag__rag_search"}),
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="数値は nl2sql_query、資料は rag_search で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_query"]),
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name=AGENT_ID,
            skill_ids=[SKILL_ID],
            data_scopes={"nl2sql": AgentDataScope(profile_ids=[PROFILE])},
        )
    )
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


def _create_run() -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=GOAL, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    return run.id


def _query(call_id: str) -> list[Any]:
    return [function_call(QUERY, {"question": GOAL, "profile_id": PROFILE}, call_id=call_id)]


def _cancel_steps(run: RunState) -> list[RunStep]:
    return [step for step in run.steps if step.tool_call and step.tool_call.name == CANCEL]


def _waiting_run(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> str:
    """ジョブが実行中のまま、承認の待ちで止まった Run（実行している側が無い）。"""
    _script(
        monkeypatch,
        _query("call-query"),
        [function_call("rag__rag_search", {"query": "売上の定義"}, call_id="call-search")],
    )
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    assert runtime_repository.get_run(run_id).status == RunStatus.WAITING_APPROVAL
    assert mcp.calls_of("nl2sql_cancel_job") == []
    return run_id


def test_cancelling_a_running_run_cancels_the_job_its_query_started(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """実行中の Run の取消（#2691）: 取消の後に届いた nl2sql_query のジョブも止める。"""
    holder: dict[str, str] = {}

    def query_then_cancelled(_argument: Any) -> dict[str, Any]:
        # 呼び先がジョブを作っている間に、利用者が Run を取り消した。
        runtime_repository.cancel_run(holder["run_id"], cancelled_by="user-a")
        return _running_job("job-late")

    mcp.outputs["nl2sql_query"] = query_then_cancelled
    model = _script(monkeypatch, _query("call-query"), [assistant_message("集計しました。")])
    holder["run_id"] = run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    # 取消の後はモデルを呼ばない（台本の 2 つ目は使わない）。
    assert len(model.calls) == 1
    [query] = mcp.calls_of("nl2sql_query")
    [cancel] = mcp.calls_of("nl2sql_cancel_job")
    assert cancel["arguments"] == {"job_id": "job-late"}
    # Run の利用者のサービストークンで、ジョブを作ったときと同じ範囲の claim。
    assert cancel["claims"]["sub"] == USER_UUID
    assert cancel["claims"]["profile_ids"] == query["claims"]["profile_ids"] == [PROFILE]
    [step] = _cancel_steps(run)
    assert step.tool_call is not None
    assert (step.tool_call.trace_id or "").startswith(RUN_CANCEL_TRACE_PREFIX)
    assert step.tool_result is not None and step.tool_result.success is True
    assert (step.tool_result.output or {})["result"] == "cancelled"
    # 失敗ではなく取消のまま。
    assert not [event for event in run.events if event.type == "runtime.failed"]


def test_cancelling_a_waiting_run_cancels_the_job_left_running(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    run_id = _waiting_run(monkeypatch, mcp)
    runtime_repository.cancel_run(run_id, cancelled_by="user-a")

    anyio.run(builtin_runtime.cancel_run_nl2sql_jobs, run_id)
    [cancel] = mcp.calls_of("nl2sql_cancel_job")
    assert cancel["arguments"] == {"job_id": "job-1"}
    # 範囲は Run が使う版から引く（ジョブを作ったときと同じ claim）。
    assert cancel["claims"]["profile_ids"] == [PROFILE]
    # 取消を送ったジョブには、もう一度は送らない。
    anyio.run(builtin_runtime.cancel_run_nl2sql_jobs, run_id)
    assert len(mcp.calls_of("nl2sql_cancel_job")) == 1


def test_cancel_entry_points_schedule_the_job_cancel_only_without_an_executor(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """画面の取消・MCP の agent_cancel_run は、実行している側の無い Run だけ後始末を予約する。"""
    scheduled: list[tuple[RunStatus, str]] = []
    monkeypatch.setattr(
        agent_api,
        "_schedule_nl2sql_job_cancel",
        lambda before, run: scheduled.append((before, run.id)),
    )
    rest_run = _waiting_run(monkeypatch, mcp)
    assert client.post(f"/api/runs/{rest_run}/cancel").status_code == 200
    mcp_run = _waiting_run(monkeypatch, mcp)
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agent_cancel_run", "arguments": {"run_id": mcp_run}},
        },
    )
    assert response.json()["result"]["structuredContent"]["result"] == "cancelled"
    assert scheduled == [
        (RunStatus.WAITING_APPROVAL, rest_run),
        (RunStatus.WAITING_APPROVAL, mcp_run),
    ]


def test_schedule_skips_running_runs_and_cancels_jobs_of_waiting_runs(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    run_id = _waiting_run(monkeypatch, mcp)
    run = runtime_repository.cancel_run(run_id, cancelled_by="user-a")

    async def schedule(before: RunStatus) -> None:
        agent_api._schedule_nl2sql_job_cancel(before, run)  # noqa: SLF001
        await asyncio.gather(*agent_api._builtin_tasks)  # noqa: SLF001

    # 実行中だった Run は実行している側が止める（ここでは送らない）。
    anyio.run(schedule, RunStatus.RUNNING)
    assert mcp.calls_of("nl2sql_cancel_job") == []
    anyio.run(schedule, RunStatus.WAITING_APPROVAL)
    assert [call["arguments"] for call in mcp.calls_of("nl2sql_cancel_job")] == [
        {"job_id": "job-1"}
    ]


def test_finished_jobs_and_denied_policy_are_not_cancelled(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    run_id = _waiting_run(monkeypatch, mcp)
    runtime_repository.cancel_run(run_id, cancelled_by="user-a")
    # ポリシーで「拒否」したツールは、後始末でも送らない。
    monkeypatch.setattr(
        builtin_runtime, "_active_policy", lambda: ToolPolicy(allow={QUERY}, deny={CANCEL})
    )
    anyio.run(builtin_runtime.cancel_run_nl2sql_jobs, run_id)
    assert mcp.calls_of("nl2sql_cancel_job") == []

    # 取り消していない Run には送らない。
    monkeypatch.setattr(builtin_runtime, "_active_policy", lambda: ToolPolicy(allow={QUERY}))
    _script(monkeypatch, _query("call-query"), [assistant_message("集計しました。")])
    mcp.outputs["nl2sql_query"] = _running_job("job-open")
    completed = _create_run()
    anyio.run(builtin_runtime.execute_run, completed)
    assert runtime_repository.get_run(completed).status == RunStatus.COMPLETED
    anyio.run(builtin_runtime.cancel_run_nl2sql_jobs, completed)
    assert mcp.calls_of("nl2sql_cancel_job") == []


def _step(name: str, output: dict[str, Any] | None, **arguments: Any) -> RunStep:
    return RunStep(
        run_id="run-x",
        tool_call=ToolCall(name=name, arguments=arguments),
        tool_result=ToolResult(name=name, success=output is not None, output=output),
    )


def test_in_flight_jobs_follow_the_latest_status_of_each_job() -> None:
    steps = [
        _step(QUERY, {"job_id": "job-a", "status": "running"}),
        _step("nl2sql__nl2sql_get_job", {"job_id": "job-a", "status": "done"}, job_id="job-a"),
        _step(QUERY, {"job_id": "job-b", "status": "pending"}),
        _step(QUERY, {"job_id": "job-c", "status": "running"}),
        _step(CANCEL, {"job_id": "job-c", "result": "cancelled"}, job_id="job-c"),
        # 失敗した続きの取得は状態を変えない（最後に見た状態のまま）。
        _step("nl2sql__nl2sql_get_job", None, job_id="job-b"),
        # 確認で止めた（ジョブの無い）結果・ほかの接続のツールは対象外。
        _step(QUERY, {"job_id": None, "status": "needs_clarification"}),
        _step("crm__nl2sql_query", {"job_id": "job-d", "status": "running"}),
    ]
    assert in_flight_nl2sql_jobs(steps) == ["job-b"]
