"""支援タスクの状態とタスクの予算（#1243）の決定論テスト。

モデルは SDK の `ScriptedModel`、RAG は契約どおりの fake の MCP（`mcp_support`）にする。
状態の生成（条件・確かめ中の問い・業務ガイド・根拠の参照）、同じ会話・同じ持ち主の次の Run への
引き継ぎ、予算（Run ごとの RAG・会話の通し・承認後の再開）を確かめる。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.data_scope import AgentDataScope
from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    run_support_task,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import (
    GUIDE_CHECK_TRACE_PREFIX,
    SUPPORT_TASK_KIND,
    SUPPORT_TASK_NAME,
    abandoned_run_consumption,
    build_support_task,
    support_task_instructions,
)
from app.features.agent.tools import (
    ToolCall,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolResult,
    tool_registry,
)
from app.settings import get_settings

LOOKUP = "test1243_lookup"
WRITE = "test1243_write"
SKILL_ID = "test1243-skill"
AGENT_ID = "test1243-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-0000-0000-0000-000000000000"
STATE_HEADING = "支援タスクの状態（前の実行から引き継ぎ）"

_CLARIFICATION = {
    "condition_id": "kind",
    "label": "契約の種類",
    "question": "契約の種類は何ですか？",
    "options": ["年間", "月額"],
}


def _evidence(chunk_id: str) -> dict[str, Any]:
    evidence: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_search"]["evidence"][0])
    evidence.update(evidence_id=chunk_id, chunk_id=chunk_id)
    return evidence


def _guided_search(argument: Any) -> dict[str, Any]:
    """業務ガイドを使う rag_search（#1238 の出力）。契約の種類が分からなければ確かめる。"""
    conditions = dict(argument.conditions or {})
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    region = {"id": "region", "label": "地域", "value": "東日本", "source": "question"}
    guide: dict[str, Any] = {
        "guide_id": "guide-1",
        "revision": 2,
        "title": "契約の更新",
        "known_conditions": [region],
        "unknown_conditions": [],
    }
    if "kind" not in conditions:
        guide.update(
            decision="clarify",
            unknown_conditions=[{"id": "kind", "label": "契約の種類", "handling": "ask"}],
        )
        output.update(
            answer="契約の種類を確かめてください。",
            outcome="needs_clarification",
            clarifications=[_CLARIFICATION],
            guide=guide,
            gaps=["更新の期限"],
            evidence=[_evidence("chunk-1")],
        )
    else:
        kind = {"id": "kind", "label": "契約の種類", "value": conditions["kind"], "source": "user"}
        guide.update(decision="answer", known_conditions=[region, kind])
        output.update(
            outcome="answered",
            clarifications=[],
            guide=guide,
            gaps=[],
            evidence=[_evidence("chunk-2"), _evidence("chunk-1")],
        )
    return output


@dataclass
class _Env:
    mcp: FakeProductMcp
    calls: list[str] = field(default_factory=list)


@pytest.fixture
def env(monkeypatch: MonkeyPatch) -> Iterator[_Env]:
    """RAG（fake の MCP）と、Control Plane の読み取り・書き込みのツールを使う業務 Agent。"""
    mcp = fake_product_mcp(monkeypatch, outputs={"rag_search": _guided_search})
    recorded = _Env(mcp=mcp)

    def handler(name: str) -> Any:
        def handle(arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            del arguments, context
            recorded.calls.append(name)
            return {"rows": 3}

        return handle

    for name, level, side_effects in (
        (LOOKUP, ToolPermissionLevel.READ, False),
        (WRITE, ToolPermissionLevel.WRITE, True),
    ):
        tool_registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} のテスト用ツール",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permission_level=level,
                side_effects=side_effects,
            ),
            handler(name),
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="支援の Skill",
            instructions="契約の手続きは rag_search で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search", "rag_read_source"]),
                SkillMcpRequirement(server_id="control-plane", tool_names=[LOOKUP, WRITE]),
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="支援の業務 Agent", skill_ids=[SKILL_ID])
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
        for name in (LOOKUP, WRITE):
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


def _run(goal: str, *, thread_id: str | None = None, user_uuid: str = USER_UUID) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID, thread_id=thread_id),
        created_by_user_uuid=user_uuid,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _state(run: RunState) -> dict[str, Any]:
    artifacts = [item for item in run.artifacts if item.kind == SUPPORT_TASK_KIND]
    assert len(artifacts) == 1
    assert artifacts[0].name == SUPPORT_TASK_NAME
    state = run_support_task(run)
    assert state is not None
    return state


def _search(call_id: str, conditions: dict[str, str] | None = None) -> Any:
    arguments: dict[str, Any] = {"query": "契約を更新したい"}
    if conditions is not None:
        arguments["conditions"] = conditions
    return [function_call("rag__rag_search", arguments, call_id=call_id)]


def _lookup(call_id: str, tool: str = LOOKUP) -> Any:
    return [function_call(tool, {"query": "売上"}, call_id=call_id)]


def test_state_is_built_from_rag_search_and_carried_to_the_next_run(
    monkeypatch: MonkeyPatch, env: _Env
) -> None:
    model = _script(
        monkeypatch,
        _search("call-1"),
        [assistant_message("契約の種類は何ですか？（年間 / 月額）")],
        _search("call-2", {"kind": "年間"}),
        [assistant_message("年間契約の更新は契約書の第 5 条の手順です。")],
        _search("call-3", {"kind": "月額"}),
        [assistant_message("月額契約の更新は第 6 条です。")],
    )

    first = _run("契約を更新したい")
    assert first.status == RunStatus.COMPLETED, first.events[-1].message
    state = _state(first)
    assert state["schema_version"] == 1
    assert (state["thread_id"], state["owner_user_uuid"]) == (first.thread_id, USER_UUID)
    assert state["goal"] == "契約を更新したい"
    assert state["outcome"] == "needs_clarification"
    # 確かめ中の問いは種類（RAG の確認）を持つ（NL2SQL の SQL の生成前の確認と分ける。#1441）。
    assert state["pending_clarifications"] == [{**_CLARIFICATION, "kind": "rag"}]
    assert state["guide"] == {
        "guide_id": "guide-1",
        "revision": 2,
        "title": "契約の更新",
        "decision": "clarify",
    }
    # RAG が質問の文から読んだ条件。
    assert state["known_conditions"]["region"]["value"] == "東日本"
    assert state["known_conditions"]["region"]["source"] == "rag_guide"
    assert state["known_conditions"]["region"]["label"] == "地域"
    assert state["gaps"] == ["更新の期限"]
    # 根拠は参照だけ（本文・抜粋を持たない）。
    assert state["evidence"] == [
        {
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "chunk_set_id": "cs-1",
            "file_name": "契約書.pdf",
        }
    ]
    assert state["budget"]["run"]["tool_calls"] == 1
    assert state["budget"]["run"]["rag_calls"] == 1
    assert state["budget"]["task"]["runs"] == 1
    # 1 回目の指示に引き継ぎは無い。
    assert STATE_HEADING not in str(model.calls[0].system_instructions)

    second = _run("年間です", thread_id=first.thread_id)
    assert second.status == RunStatus.COMPLETED, second.events[-1].message
    # 2 回目の指示に、目的・分かっている条件・確かめ中の問い・ガイド・予算を足す。
    instructions = str(model.calls[2].system_instructions)
    assert STATE_HEADING in instructions
    assert "目的: 「契約を更新したい」" in instructions
    assert "地域（region） = 「東日本」" in instructions
    assert "kind: 「契約の種類は何ですか？」 選択肢: 「年間」 / 「月額」" in instructions
    assert "guide_id=guide-1、版 2" in instructions
    assert "「更新の期限」" in instructions
    assert "残り 59 回（上限 60 回）" in instructions
    assert "rag_search の conditions" in instructions
    # モデルは利用者の答えを conditions に入れて呼び直した。
    assert env.mcp.calls_of("rag_search")[1]["arguments"]["conditions"] == {"kind": "年間"}
    state = _state(second)
    assert state["goal"] == "契約を更新したい"
    assert state["known_conditions"]["kind"]["value"] == "年間"
    assert state["known_conditions"]["kind"]["source"] == "user_answer"
    assert state["known_conditions"]["kind"]["label"] == "契約の種類"
    # 答えた問いは確かめ終え、不足も残らない。
    assert state["pending_clarifications"] == []
    assert state["gaps"] == []
    assert state["guide"]["decision"] == "answer"
    assert state["outcome"] == "answered"
    # 同じ根拠は 1 つにまとめ、最後に使った順に並べる。
    assert [item["chunk_id"] for item in state["evidence"]] == ["chunk-2", "chunk-1"]
    assert state["budget"]["run"]["rag_calls"] == 1
    assert state["budget"]["task"]["runs"] == 2
    assert state["budget"]["task"]["tool_calls"] == 2
    assert state["budget"]["task"]["rag_calls"] == 2

    # 新しい発言で条件が変わったら、新しい値で上書きし、古い値は出所と時刻ごと残す。
    third = _run("やっぱり月額でした", thread_id=first.thread_id)
    assert "契約の種類（kind） = 「年間」（利用者の答え）" in str(
        model.calls[4].system_instructions
    )
    kind = _state(third)["known_conditions"]["kind"]
    assert kind["value"] == "月額"
    assert [item["value"] for item in kind["previous"]] == ["年間"]
    assert kind["previous"][0]["source"] == "user_answer"


def test_state_of_another_users_run_is_not_used(monkeypatch: MonkeyPatch, env: _Env) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    del env
    model = _script(
        monkeypatch,
        _search("call-1"),
        [assistant_message("契約の種類は何ですか？")],
        [assistant_message("別の利用者への回答")],
    )
    first = _run("契約を更新したい")
    assert run_support_task(first) is not None
    # 会話は持ち主しか続けられない（#768）が、保存先の不整合で別の利用者の Run が同じ会話に
    # 入っていても、その状態は使わない。
    other = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="年間です", agent_id=AGENT_ID), created_by_user_uuid=OTHER_UUID
    )
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 不整合の状態を作る
        repository._runs[other.id].thread_id = first.thread_id

    assert runtime_repository.support_task_context(other.id) == ("年間です", None)
    anyio.run(builtin_runtime.execute_run, other.id)

    run = runtime_repository.get_run(other.id)
    assert run.status == RunStatus.COMPLETED
    assert STATE_HEADING not in str(model.calls[2].system_instructions)
    # ツールを呼ばず、引き継ぐ状態も無い Run には状態を残さない。
    assert run_support_task(run) is None


def test_rag_calls_over_the_run_budget_return_budget_exceeded(
    monkeypatch: MonkeyPatch, env: _Env
) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_rag_calls_per_run", 1)
    env.mcp.outputs["rag_search"] = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    model = _script(
        monkeypatch,
        _search("call-1"),
        _search("call-2"),
        # 根拠の本文の読み取りは RAG の上限に数えない。
        [
            function_call(
                "rag__rag_read_source",
                {"document_id": "doc-1", "chunk_id": "chunk-1"},
                call_id="call-3",
            )
        ],
        [assistant_message("集めた根拠で回答します。")],
    )

    run = _run("契約を更新したい")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(env.mcp.calls_of("rag_search")) == 1
    assert len(env.mcp.calls_of("rag_read_source")) == 1
    # 最後の step は回答の最終の検証（既定 on。予算には数えない。#1277）。
    assert [step.status for step in run.steps] == ["completed", "failed", "completed", "completed"]
    blocked = run.steps[1].tool_result
    assert blocked is not None
    assert blocked.error_code == "budget_exceeded"
    assert blocked.error_details == {"budget": {"scope": "run_rag_calls", "limit": 1, "used": 1}}
    # モデルには例外ではなく、ツールの結果として予算の上限を返す。
    assert "budget_exceeded" in str(model.calls[2].input)
    assert "ここまでに集めた根拠で回答する" in str(model.calls[2].input)
    state = _state(run)
    assert state["budget"]["run"]["tool_calls"] == 2
    assert state["budget"]["run"]["rag_calls"] == 1
    assert state["budget"]["run"]["budget_exceeded"] == 1
    assert state["budget"]["limits"] == {"rag_calls_per_run": 1, "tool_calls_per_task": 60}
    # 業務ガイドの項目の無い（#1238 より前の）RAG の出力でも状態を作る。
    assert state["guide"] is None
    assert state["pending_clarifications"] == []
    assert state["outcome"] == "answered"


def test_task_budget_accumulates_across_runs_in_the_thread(
    monkeypatch: MonkeyPatch, env: _Env
) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    monkeypatch.setattr(get_settings(), "agent_max_tool_calls_per_task", 3)
    model = _script(
        monkeypatch,
        _lookup("call-1"),
        _lookup("call-2"),
        [assistant_message("2 回調べました。")],
        _lookup("call-3"),
        _lookup("call-4"),
        [assistant_message("上限に達したため、ここまでの結果で答えます。")],
    )

    first = _run("今月の売上は？")
    assert _state(first)["budget"]["task"]["tool_calls"] == 2

    second = _run("先月は？", thread_id=first.thread_id)

    assert second.status == RunStatus.COMPLETED, second.events[-1].message
    assert "残り 1 回（上限 3 回）" in str(model.calls[3].system_instructions)
    # 会話の通しで 3 回まで（新しい Run でも上限は 0 に戻らない）。
    assert env.calls == [LOOKUP, LOOKUP, LOOKUP]
    blocked = second.steps[-1].tool_result
    assert blocked is not None and blocked.error_code == "budget_exceeded"
    assert blocked.error_details["budget"]["scope"] == "task_tool_calls"
    state = _state(second)
    assert state["budget"]["run"]["tool_calls"] == 1
    assert state["budget"]["run"]["budget_exceeded"] == 1
    assert state["budget"]["task"]["tool_calls"] == 3
    assert state["budget"]["task"]["runs"] == 2


def test_resume_after_approval_keeps_the_consumption(monkeypatch: MonkeyPatch, env: _Env) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    monkeypatch.setattr(get_settings(), "agent_max_tool_calls_per_task", 2)
    _script(
        monkeypatch,
        _lookup("call-1"),
        _lookup("call-w", WRITE),
        _lookup("call-2"),
        [assistant_message("登録しました。")],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="調べて登録して", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )

    anyio.run(builtin_runtime.execute_run, run.id)

    waiting = runtime_repository.get_run(run.id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    # 承認待ちで止めるときも状態を残す。
    assert _state(waiting)["budget"]["run"]["tool_calls"] == 1
    [approval] = waiting.approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))

    anyio.run(builtin_runtime.resume_run, run.id)

    done = runtime_repository.get_run(run.id)
    assert done.status == RunStatus.COMPLETED, done.events[-1].message
    # 再開しても消費は 0 に戻らない（前の LOOKUP と承認した WRITE で上限の 2 回）。
    assert env.calls == [LOOKUP, WRITE]
    blocked = done.steps[-1].tool_result
    assert blocked is not None and blocked.error_code == "budget_exceeded"
    state = _state(done)
    assert state["budget"]["run"]["tool_calls"] == 2
    assert state["budget"]["run"]["budget_exceeded"] == 1
    assert state["budget"]["task"] == {
        "runs": 1,
        "tool_calls": 2,
        "rag_calls": 0,
        "tool_seconds": state["budget"]["task"]["tool_seconds"],
        "rag_seconds": 0.0,
    }


def test_failed_runs_count_toward_the_task_budget(monkeypatch: MonkeyPatch, env: _Env) -> None:
    """失敗する Run を同じ会話で繰り返しても、会話の通しの上限を超えない（#1277）。"""
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    monkeypatch.setattr(get_settings(), "agent_max_tool_calls_per_task", 3)
    model = _script(
        monkeypatch,
        _lookup("call-1"),
        _lookup("call-2"),
        RuntimeError("model down"),
        _lookup("call-3"),
        _lookup("call-4"),
        RuntimeError("model down"),
        _lookup("call-5"),
        [assistant_message("上限に達したため、ここまでの結果で答えます。")],
    )

    first = _run("今月の売上は？")
    assert first.status == RunStatus.FAILED
    second = _run("もう一度", thread_id=first.thread_id)
    assert second.status == RunStatus.FAILED
    third = _run("もう一度", thread_id=first.thread_id)

    assert third.status == RunStatus.COMPLETED, third.events[-1].message
    # 失敗した Run の呼び出しも数える（2 回 + 1 回で上限の 3 回。以降は呼ばない）。
    assert env.calls == [LOOKUP, LOOKUP, LOOKUP]
    blocked = second.steps[-1].tool_result
    assert blocked is not None and blocked.error_code == "budget_exceeded"
    assert "残り 1 回（上限 3 回）" in str(model.calls[3].system_instructions)
    assert "残り 0 回（上限 3 回）" in str(model.calls[6].system_instructions)
    state = _state(third)
    assert state["budget"]["run"]["tool_calls"] == 0
    assert state["budget"]["run"]["budget_exceeded"] == 1
    assert state["budget"]["task"]["tool_calls"] == 3
    assert state["budget"]["task"]["runs"] == 3


def test_cancelled_run_counts_toward_the_task_budget(monkeypatch: MonkeyPatch, env: _Env) -> None:
    """取り消した Run の実行済みの呼び出しも数え、承認待ちのまま取り消した呼び出しは数えない。"""
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    monkeypatch.setattr(get_settings(), "agent_max_tool_calls_per_task", 2)
    _script(
        monkeypatch,
        _lookup("call-1"),
        _lookup("call-w", WRITE),
        _lookup("call-2"),
        _lookup("call-3"),
        [assistant_message("ここまでの結果で答えます。")],
    )
    first = _run("調べて登録して")
    assert first.status == RunStatus.WAITING_APPROVAL
    runtime_repository.cancel_run(first.id)

    second = _run("やはり調べるだけ", thread_id=first.thread_id)

    assert second.status == RunStatus.COMPLETED, second.events[-1].message
    assert env.calls == [LOOKUP, LOOKUP]
    blocked = second.steps[-1].tool_result
    assert blocked is not None and blocked.error_code == "budget_exceeded"
    state = _state(second)
    assert state["budget"]["task"]["tool_calls"] == 2
    assert state["budget"]["task"]["runs"] == 2


def test_abandoned_run_consumption_counts_calls_stopped_before_the_result() -> None:
    def step(name: str, status: str, approval_id: str | None = None) -> RunStep:
        return RunStep(
            run_id="run-1",
            status=StepStatus(status),
            tool_call=ToolCall(name=name, arguments={}),
            approval_id=approval_id,
        )

    consumed = abandoned_run_consumption(
        [
            # 結果を受け取る前に Run が失敗・取消になった呼び出し（呼び先には届いている見込み）。
            step("rag__rag_search", "running"),
            step(LOOKUP, "cancelled"),
            # 承認待ちのまま取り消した呼び出しと、Control Plane の検証は数えない。
            step(WRITE, "cancelled", approval_id="approval-1"),
            step("rag__rag_validate_answer", "running"),
        ]
    )

    assert (consumed["tool_calls"], consumed["rag_calls"]) == (2, 1)


# --- 前の実行で使ったプロファイル（#1880） ---------------------------------------------

RAG_PROFILE_LINE = "検索・回答プロファイル（接続 rag、search_answer_profile_id）=「{}」"
FOLLOW_UP_RULE = "会話の続きの質問では同じプロファイルを使う"


def _tool_step(
    name: str, arguments: dict[str, Any], *, success: bool = True, trace_id: str | None = None
) -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED if success else StepStatus.FAILED,
        tool_call=ToolCall(name=name, arguments=arguments, trace_id=trace_id),
        tool_result=ToolResult(
            name=name,
            success=success,
            output={"status": "ok"} if success else None,
            error=None if success else "失敗",
        ),
    )


def _build(previous: dict[str, Any] | None, steps: list[RunStep]) -> dict[str, Any]:
    return build_support_task(
        previous,
        steps=steps,
        run_id="run-1",
        thread_id="thread-1",
        owner_user_uuid=USER_UUID,
        goal="経費精算の締め日は？",
        max_rag_calls_per_run=5,
        max_tool_calls_per_task=60,
    )


def _instructions(state: dict[str, Any], fixed: tuple[str, ...] = ()) -> str:
    return support_task_instructions(
        state,
        goal="経費精算の締め日は？",
        max_rag_calls_per_run=5,
        max_tool_calls_per_task=60,
        fixed_profile_connections=fixed,
    )


def test_profiles_keep_the_last_successful_profile_per_connection() -> None:
    previous = {"profiles": {"rag-sub": {"kind": "rag", "id": "prof-old", "tool": "rag_search"}}}
    state = _build(
        previous,
        [
            _tool_step("rag__rag_search", {"query": "締め日", "search_answer_profile_id": "p-1"}),
            _tool_step(
                "rag__rag_retrieve_evidence", {"query": "締め日", "search_answer_profile_id": "p-2"}
            ),
            # 失敗した呼び出し・Control Plane の業務ガイドの照合・プロファイルの無い呼び出しは
            # 残さない。
            _tool_step(
                "rag__rag_search",
                {"query": "x", "search_answer_profile_id": "p-bad"},
                success=False,
            ),
            _tool_step(
                "rag__rag_lookup_guides",
                {"question": "x", "search_answer_profile_id": "p-guide"},
                trace_id=f"{GUIDE_CHECK_TRACE_PREFIX}1",
            ),
            _tool_step("rag__rag_search", {"query": "x"}),
            _tool_step("nl2sql__nl2sql_query", {"question": "件数", "profile_id": "sales"}),
        ],
    )

    # 前の状態の接続は引き継ぎ、同じ接続は最後の値で上書きする。
    assert state["profiles"] == {
        "rag-sub": {"kind": "rag", "id": "prof-old", "tool": "rag_search"},
        "rag": {"kind": "rag", "id": "p-2", "tool": "rag_retrieve_evidence"},
        "nl2sql": {"kind": "nl2sql", "id": "sales", "tool": "nl2sql_query"},
    }
    # 次の Run でプロファイルを使わなければ、前の値をそのまま引き継ぐ。
    assert _build(state, [])["profiles"] == state["profiles"]


def test_profile_instructions_hide_connections_fixed_by_the_data_scope() -> None:
    state = _build(
        None,
        [
            _tool_step("rag__rag_search", {"query": "締め日", "search_answer_profile_id": "p-1"}),
            _tool_step("nl2sql__nl2sql_query", {"question": "件数", "profile_id": "sales"}),
        ],
    )

    text = _instructions(state)
    assert RAG_PROFILE_LINE.format("p-1") in text
    assert "業務プロファイル（接続 nl2sql、profile_id）=「sales」" in text
    assert FOLLOW_UP_RULE in text
    assert "rag_recommend_search_answer_profile / nl2sql_recommend_profile" in text
    assert "指示語・省略を前の質問で補った質問を渡す" in text

    # 範囲で 1 つに決まっている接続は出さない（データの範囲の指示は id を渡さないと案内する）。
    only_nl2sql = _instructions(state, ("rag",))
    assert "p-1" not in only_nl2sql
    assert "rag_recommend_search_answer_profile" not in only_nl2sql
    assert "「sales」" in only_nl2sql
    assert FOLLOW_UP_RULE not in _instructions(state, ("rag", "nl2sql"))


def _profile_search(call_id: str, profile_id: str) -> Any:
    arguments = {"query": "経費精算の締め日", "search_answer_profile_id": profile_id}
    return [function_call("rag__rag_search", arguments, call_id=call_id)]


def test_next_run_is_told_to_reuse_the_previous_profile(
    monkeypatch: MonkeyPatch, env: _Env
) -> None:
    model = _script(
        monkeypatch,
        _profile_search("call-1", "prof-expense"),
        [assistant_message("締め日は毎月 25 日です。")],
        [assistant_message("期限を過ぎると翌月の精算になります。")],
    )

    first = _run("経費精算の締め日は？")
    assert first.status == RunStatus.COMPLETED, first.events[-1].message
    assert _state(first)["profiles"] == {
        "rag": {"kind": "rag", "id": "prof-expense", "tool": "rag_search"}
    }

    second = _run("その期限を過ぎたらどうなる？", thread_id=first.thread_id)
    assert second.status == RunStatus.COMPLETED, second.events[-1].message
    instructions = str(model.calls[2].system_instructions)
    assert RAG_PROFILE_LINE.format("prof-expense") in instructions
    assert FOLLOW_UP_RULE in instructions
    # 指示のツールの名前はモデルに渡す名前にする（推薦のツールを渡していなければ素の名前のまま）。
    assert "rag_recommend_search_answer_profile" in instructions


def test_profile_is_not_shown_when_the_data_scope_fixes_it(
    monkeypatch: MonkeyPatch, env: _Env
) -> None:
    runtime_repository.delete_agent(AGENT_ID)
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name="支援の業務 Agent",
            skill_ids=[SKILL_ID],
            data_scopes={"rag": AgentDataScope(profile_ids=["prof-expense"])},
        )
    )
    model = _script(
        monkeypatch,
        _search("call-1", {"kind": "年間"}),
        [assistant_message("年間契約の更新は第 5 条です。")],
        [assistant_message("期限を過ぎると更新できません。")],
    )

    first = _run("契約を更新したい")
    assert first.status == RunStatus.COMPLETED, first.events[-1].message
    # Runtime が埋めたプロファイルも状態には残す。
    assert _state(first)["profiles"]["rag"]["id"] == "prof-expense"

    second = _run("その期限を過ぎたらどうなる？", thread_id=first.thread_id)
    assert second.status == RunStatus.COMPLETED, second.events[-1].message
    instructions = str(model.calls[2].system_instructions)
    assert STATE_HEADING in instructions
    assert FOLLOW_UP_RULE not in instructions
    assert "「prof-expense」に決まっている" in instructions
