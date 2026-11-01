"""固定の RAG が回答を確定できないときの Agent の続け方と、経路の記録（#1283）の決定論テスト。

RAG は経路を知らない（RAG から Agent へは呼ばない）。Agent は `rag_search` の対応（outcome）が
`needs_environment_data` のとき、現場のデータを確かめる自分の道具（RAG 以外の MCP 接続のツール）で
続けるよう、モデルへの結果に `next_step` を足し、この Run の経路と理由を支援タスクの状態に残す。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.rag_retrieval_state import (
    DEGRADED_ZERO_EVIDENCE_HINT,
    GUARDRAIL_DEGRADED_HINT,
    retrieval_notice,
)
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    run_support_task,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import is_environment_tool, rag_next_step
from app.settings import get_settings

SKILL_RAG = "test1283-rag"
SKILL_RAG_NL2SQL = "test1283-rag-nl2sql"
AGENT_RAG = "test1283-agent-rag"
AGENT_RAG_NL2SQL = "test1283-agent-rag-nl2sql"
USER_UUID = "11111111-2222-3333-4444-555555555555"
CONFIRMATIONS = ["受注 A-100 の今の承認状態"]


def _search_output(outcome: str) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    output.update(outcome=outcome)
    if outcome == "needs_environment_data":
        output.update(confirmations=CONFIRMATIONS)
    return output


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    # 最終の検証（#1246）はこのテストの対象外。
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    for skill_id, requirements in (
        (SKILL_RAG, [SkillMcpRequirement(server_id="rag", tool_names=["rag_search"])]),
        (
            SKILL_RAG_NL2SQL,
            [
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_get_job"]),
            ],
        ),
    ):
        skill_registry.upsert_custom(
            AgentSkillDefinition(
                id=skill_id,
                name=skill_id,
                instructions="rag_search で調べる。",
                mcp_requirements=requirements,
            )
        )
    for agent_id, skill_id in ((AGENT_RAG, SKILL_RAG), (AGENT_RAG_NL2SQL, SKILL_RAG_NL2SQL)):
        runtime_repository.create_agent(
            AgentProfile(id=agent_id, name=agent_id, skill_ids=[skill_id])
        )
    try:
        yield fake
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id in {AGENT_RAG, AGENT_RAG_NL2SQL}
            ]:
                repository._runs.pop(run_id)
        for agent_id in (AGENT_RAG, AGENT_RAG_NL2SQL):
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)
        for skill_id in (SKILL_RAG, SKILL_RAG_NL2SQL):
            with contextlib.suppress(KeyError, ValueError):
                skill_registry.remove(skill_id)


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


def _run(agent_id: str, goal: str = "受注 A-100 の承認は終わった？") -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=agent_id), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    """モデルの `call_index` 回目の入力にある、ツールの結果（モデルが見たもの）。"""
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _route(run: RunState) -> dict[str, Any]:
    state = run_support_task(run)
    assert state is not None
    return dict(state["route"])


def test_environment_tool_is_a_non_rag_mcp_tool() -> None:
    assert is_environment_tool("nl2sql__nl2sql_query")
    assert is_environment_tool("crm__crm_get_order")
    assert not is_environment_tool("rag__rag_search")
    assert not is_environment_tool("rag__rag_read_source")
    # Control Plane のツール（MCP 接続ではない）は含めない。
    assert not is_environment_tool("skill_reference_read")


@pytest.mark.parametrize(
    "outcome",
    ["answered", "conditional", "needs_human", "insufficient_evidence"],
)
def test_next_step_only_for_outcomes_the_rag_cannot_finish(outcome: str) -> None:
    assert rag_next_step(_search_output(outcome), ["nl2sql__nl2sql_query"]) is None
    # 確認の質問を求められたら、道具で続けずに問いを先に返す（#1322。tools は空）。
    asked = rag_next_step(_search_output("needs_clarification"), ["nl2sql__nl2sql_query"])
    assert asked is not None and (asked["action"], asked["tools"]) == ("ask_clarification", [])


def test_next_step_is_deterministic_from_the_tools_of_the_run() -> None:
    output = _search_output("needs_environment_data")
    with_tools = rag_next_step(output, ["nl2sql__nl2sql_query", "nl2sql__nl2sql_query"])
    assert with_tools is not None
    assert with_tools["action"] == "continue_with_tools"
    assert with_tools["reason"] == "needs_environment_data"
    assert with_tools["tools"] == ["nl2sql__nl2sql_query"]
    without = rag_next_step(output, [])
    assert without is not None
    assert without["action"] == "answer_with_confirmations"
    assert without["tools"] == []


def test_agent_continues_with_its_own_tools_and_records_the_route(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_search"] = _search_output("needs_environment_data")
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "受注 A-100 の承認"}, call_id="c1")],
        [function_call("nl2sql__nl2sql_get_job", {"job_id": "job-1"}, call_id="c2")],
        [assistant_message("受注 A-100 は承認済みです（NL2SQL の結果）。")],
    )

    run = _run(AGENT_RAG_NL2SQL)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # モデルが見た rag_search の結果には、Control Plane が決めた次の手がある。
    seen = _tool_output(model, 1, "c1")
    assert seen["outcome"] == "needs_environment_data"
    assert seen["confirmations"] == CONFIRMATIONS
    assert seen["next_step"]["action"] == "continue_with_tools"
    assert seen["next_step"]["tools"] == ["nl2sql__nl2sql_get_job"]
    # 記録する step の結果は RAG の結果のまま（次の手は足さない）。
    search_step = next(
        step for step in run.steps if step.tool_call and step.tool_call.name == "rag__rag_search"
    )
    assert search_step.tool_result is not None
    assert "next_step" not in (search_step.tool_result.output or {})
    assert _route(run) == {
        "path": "rag_then_tools",
        "reason": "needs_environment_data",
        "environment_data_required": True,
        "continued_with": ["nl2sql__nl2sql_get_job"],
    }


def test_without_environment_tools_the_agent_answers_with_confirmations(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_search"] = _search_output("needs_environment_data")
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "受注 A-100 の承認"}, call_id="c1")],
        [assistant_message("受注 A-100 の今の承認状態を確かめてください。")],
    )

    run = _run(AGENT_RAG)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    seen = _tool_output(model, 1, "c1")
    assert seen["next_step"]["action"] == "answer_with_confirmations"
    assert seen["next_step"]["tools"] == []
    assert _route(run) == {
        "path": "rag",
        "reason": "needs_environment_data",
        "environment_data_required": True,
        "continued_with": [],
    }


def test_answered_rag_result_has_no_next_step(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_search"] = _search_output("answered")
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "契約の更新"}, call_id="c1")],
        [assistant_message("契約書の第 5 条の手順です。")],
    )

    run = _run(AGENT_RAG_NL2SQL)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert "next_step" not in _tool_output(model, 1, "c1")
    assert _route(run) == {
        "path": "rag",
        "reason": "answered",
        "environment_data_required": False,
        "continued_with": [],
    }


@pytest.mark.parametrize(
    ("degraded", "reason", "hint"),
    [
        (
            {"degraded_steps": [{"kind": "reference_load", "count": 1}]},
            "degraded_steps",
            DEGRADED_ZERO_EVIDENCE_HINT,
        ),
        ({"guardrail_degraded": True}, "guardrail_degraded", GUARDRAIL_DEGRADED_HINT),
    ],
    ids=["degraded-steps", "guardrail"],
)
def test_degraded_zero_evidence_result_tells_the_model_it_is_not_an_absence(
    degraded: dict[str, Any], reason: str, hint: str, monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """根拠 0 件が縮退・安全チェックの未完了によるとき、モデルへの結果に案内を足す（#3546）。"""
    output = _search_output("insufficient_evidence")
    output.update(evidence=[], **degraded)
    mcp.outputs["rag_search"] = output
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "契約の更新"}, call_id="c1")],
        [assistant_message("資料を確かめられませんでした。")],
    )

    run = _run(AGENT_RAG)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert _tool_output(model, 1, "c1")["retrieval_notice"] == {
        "reason": reason,
        "next_step": hint,
    }
    # 記録する step の結果は RAG の結果のまま。
    search_step = next(
        step for step in run.steps if step.tool_call and step.tool_call.name == "rag__rag_search"
    )
    assert search_step.tool_result is not None
    assert "retrieval_notice" not in (search_step.tool_result.output or {})


@pytest.mark.parametrize(
    "output",
    [
        # 根拠がある（縮退しても根拠は使える）。
        {"evidence": [{"chunk_id": "c"}], "degraded_steps": [{"kind": "rerank", "count": 1}]},
        # 縮退していない 0 件（検索で当たらなかった）。
        {"evidence": [], "degraded_steps": [], "zero_evidence": {"stage": "retrieval"}},
        # 形の合わない縮退の記録。
        {"evidence": [], "degraded_steps": [{"kind": "", "count": 1}], "guardrail_degraded": 1},
    ],
    ids=["with-evidence", "no-hit", "malformed"],
)
def test_no_retrieval_notice_unless_zero_evidence_is_degraded(output: dict[str, Any]) -> None:
    assert retrieval_notice(output) is None
