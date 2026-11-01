"""Control Plane の業務ガイドの照合と確認の質問の次の手（#1321・#1322）の決定論テスト。

モデルが業務ガイド（`rag_lookup_guides`・`rag_search`）を引かずに `rag_retrieve_evidence` で根拠を
集めると、確かめる条件があってもモデルが分岐ごとに答えてしまう（#1317 の実環境の評価の
cr-grant-permission）。Control Plane が同じ接続の `rag_lookup_guides` を 1 回呼び、最上位の
業務ガイドと次の手（条件が分からなければ確認の質問）を根拠の結果に足すこと、回答の対応がその判断を使うことを、
SDK の `ScriptedModel` と契約どおりの fake の RAG の MCP（`mcp_support`）で確かめる。
RAG から Agent へは呼ばない（向きは Agent → RAG だけ）。
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
from app.features.agent.builtin_runtime import (
    ModelTarget,
    guide_check_query,
    support_task_goal,
    support_task_known_conditions,
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
from app.features.agent.support_task import (
    GUIDE_CHECK_TRACE_PREFIX,
    GUIDE_IS_NOT_EVIDENCE,
    guide_check_note,
    rag_next_step,
)
from app.settings import get_settings

SKILL_ID = "test1322-skill"
AGENT_ID = "test1322-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
PROFILE = "sap-1"
GOAL = "アクセス権限を付与したいです。どうすればよいですか？"
TARGET_QUESTION = {
    "condition_id": "target",
    "label": "付与先",
    "question": "権限の付与先は、個別の利用者ですか、グループですか？",
    "options": ["個別", "グループ"],
}

# #1317 の実環境の D（run3）の cr-grant-permission の回答（付与先を確かめずに両方の分岐で答えた）。
GRANT_ANSWER = "\n".join(
    [
        "**アクセス権限の付与手順**",
        "",
        "1. **個別の利用者へ付与**",
        "   - 利用者の詳細画面を開き、**「権限」タブ**で付与したい権限を選択し、"
        "**「付与」** ボタンを押す。",
        "",
        "2. **グループへ付与**",
        "   - グループの詳細画面を開き、**「権限」タブ**で同様に権限を選択し付与する。",
        "   - 影響範囲が広いため、**付与前に部門長の承認**を得ること。",
    ]
)
CLARIFICATION = "権限の付与先は、個別の利用者ですか、グループですか？（個別／グループ）"


def _access_guide(decision: str, known: dict[str, str] | None = None) -> dict[str, Any]:
    guide: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"]["guides"][0])
    guide.update(
        guide_id="g-access",
        title="アクセス権限の付与",
        decision=decision,
        expected_result="付与先に合った画面で権限を付与し、反映を確かめる。",
        known_conditions=[
            {"id": key, "label": "付与先", "value": value, "source": "question", "state": "known"}
            for key, value in (known or {}).items()
        ],
        unknown_conditions=(
            [{"id": "target", "label": "付与先", "handling": "ask", "state": "unknown"}]
            if decision == "clarify"
            else []
        ),
        clarifications=[TARGET_QUESTION] if decision == "clarify" else [],
        impact_scope="group",
        approval_required=True,
        impact_applies=decision != "answer" or (known or {}).get("target") != "個別",
        handoff_contact="サポート窓口",
    )
    return guide


def _lookup_output(argument: Any) -> dict[str, Any]:
    """付与先が質問・条件で分かれば answer、分からなければ clarify（RAG の rank_guides と同じ）。"""
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"])
    if "権限" not in argument.query:
        output["guides"] = []
        return output
    conditions = dict(argument.conditions or {})
    if "target" not in conditions and "個別の利用者に" in argument.query:
        conditions["target"] = "個別"
    if "target" in conditions:
        output["guides"] = [_access_guide("answer", {"target": conditions["target"]})]
    else:
        output["guides"] = [_access_guide("clarify")]
    return output


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch, outputs={"rag_lookup_guides": _lookup_output})
    # 最終の検証（#1246）はこのテストの対象外（対応は回答の段落と step から決める）。
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="手順は rag_retrieve_evidence で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=["rag_search", "rag_retrieve_evidence", "rag_lookup_guides"],
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


def _run(goal: str = GOAL, *, thread_id: str | None = None) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID, thread_id=thread_id),
        created_by_user_uuid=USER_UUID,
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


def _retrieve(call_id: str, **extra: Any) -> Any:
    arguments = {"query": "アクセス権限 を 付与 する 方法", "search_answer_profile_id": PROFILE}
    arguments.update(extra)
    return [function_call("rag__rag_retrieve_evidence", arguments, call_id=call_id)]


def _outcome(run: RunState) -> dict[str, Any]:
    answer = next(item for item in run.artifacts if item.kind == "answer")
    assert isinstance(answer.content, dict)
    outcome: dict[str, Any] = answer.content["outcome"]
    return outcome


# ---- 次の手（決定的） -------------------------------------------------------------------------


def test_guide_check_note_turns_the_guide_decision_into_the_next_step() -> None:
    clarify = guide_check_note({"guides": [_access_guide("clarify")]})
    assert clarify is not None
    assert clarify["guide"]["decision"] == "clarify"
    assert clarify["next_step"]["action"] == "ask_clarification"
    assert clarify["next_step"]["questions"] == [TARGET_QUESTION]
    assert "分岐ごと" in clarify["next_step"]["instruction"]

    # 分かっている条件の場合だけを答える。係らない影響範囲・承認は求めない（#1320）。
    single = guide_check_note({"guides": [_access_guide("answer", {"target": "個別"})]})
    assert single is not None and single["next_step"]["action"] == "answer_with_guide"
    assert "付与先=個別" in single["next_step"]["instruction"]
    assert "承認" not in single["next_step"]["instruction"]
    group = guide_check_note({"guides": [_access_guide("answer", {"target": "グループ"})]})
    assert group is not None and "承認が要るか" in group["next_step"]["instruction"]

    handoff = guide_check_note({"guides": [{**_access_guide("clarify"), "decision": "handoff"}]})
    assert handoff is not None and handoff["next_step"]["action"] == "handoff"
    assert "サポート窓口" in handoff["next_step"]["instruction"]
    branch = guide_check_note({"guides": [{**_access_guide("clarify"), "decision": "branch"}]})
    assert branch is not None and branch["next_step"]["action"] == "answer_by_conditions"

    assert guide_check_note({"guides": []}) is None
    assert guide_check_note(None) is None

    # モデルが自分で引いた業務ガイド（根拠を集める前）は、業務ガイドだけで答えさせない。
    own = guide_check_note(
        {"guides": [_access_guide("answer", {"target": "グループ"})]}, evidence_gathered=False
    )
    assert own is not None and GUIDE_IS_NOT_EVIDENCE in own["next_step"]["instruction"]
    assert GUIDE_IS_NOT_EVIDENCE not in group["next_step"]["instruction"]
    own_clarify = guide_check_note({"guides": [_access_guide("clarify")]}, evidence_gathered=False)
    assert own_clarify is not None
    assert own_clarify["next_step"]["action"] == "ask_clarification"


def test_rag_search_asking_a_clarification_gets_the_question_first() -> None:
    output = {"outcome": "needs_clarification", "clarifications": [TARGET_QUESTION]}
    step = rag_next_step(output, [])
    assert step is not None
    assert (step["action"], step["reason"]) == ("ask_clarification", "needs_clarification")
    assert step["questions"] == [TARGET_QUESTION]
    assert rag_next_step({"outcome": "answered"}, []) is None


def test_guide_check_query_and_previous_state() -> None:
    assert guide_check_query(GOAL, "アクセス権限 付与") == f"{GOAL}\nアクセス権限 付与"
    assert guide_check_query(GOAL, f" {GOAL} ") == GOAL
    assert guide_check_query(GOAL, None) == GOAL
    pending = {"goal": GOAL, "pending_clarifications": [TARGET_QUESTION]}
    # 問いを確かめ中の答え（「個別の利用者に付けます」）には、前の質問を含める。
    assert (
        support_task_goal("個別の利用者に付けます。", pending)
        == f"{GOAL}\n個別の利用者に付けます。"
    )
    assert support_task_goal("別の質問", {"goal": GOAL}) == "別の質問"
    assert support_task_goal(GOAL, None) == GOAL
    known = {"known_conditions": {"target": {"value": "個別", "source": "user_answer"}, "x": {}}}
    assert support_task_known_conditions(known) == {"target": "個別"}
    assert support_task_known_conditions(None) == {}


# ---- Run ----------------------------------------------------------------------------------------


def test_evidence_without_the_guide_gets_the_question_and_all_branches_are_conditional(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(monkeypatch, _retrieve("call-evidence"), [assistant_message(GRANT_ANSWER)])
    run = _run()
    assert run.status == RunStatus.COMPLETED

    # Control Plane が同じ接続の業務ガイドを 1 回照合した（質問と、根拠を集めた質問を渡す）。
    lookups = mcp.calls_of("rag_lookup_guides")
    assert len(lookups) == 1
    arguments = lookups[0]["arguments"]
    assert arguments["query"] == f"{GOAL}\nアクセス権限 を 付与 する 方法"
    assert (arguments["search_answer_profile_id"], arguments["limit"]) == (PROFILE, 1)
    step = next(item for item in run.steps if item.tool_call and "lookup" in item.tool_call.name)
    assert step.tool_call is not None and step.tool_call.name == "rag__rag_lookup_guides"
    assert (step.tool_call.trace_id or "").startswith(GUIDE_CHECK_TRACE_PREFIX)

    # モデルは根拠の結果で、業務ガイドと確認の質問の次の手を見た。
    seen = _tool_output(model, 1, "call-evidence")
    assert seen["evidence"]
    assert seen["guide_check"]["guide"]["title"] == "アクセス権限の付与"
    assert seen["guide_check"]["next_step"]["action"] == "ask_clarification"
    assert seen["guide_check"]["next_step"]["questions"] == [TARGET_QUESTION]

    # 確かめずに両方の分岐で答えたので、条件付きの回答（#1322）。
    outcome = _outcome(run)
    assert outcome["value"] == "conditional"
    assert "guide_conditions" in outcome["signals"]

    # 照合は Control Plane の呼び出しなので、予算・消費に数えない。問いは確かめ中として残す。
    state = run_support_task(run)
    assert state is not None
    assert state["budget"]["run"]["tool_calls"] == 1
    assert state["guide"]["decision"] == "clarify"
    assert [item["condition_id"] for item in state["pending_clarifications"]] == ["target"]


def test_clarification_only_answer_after_the_guide_check_is_needs_clarification(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(monkeypatch, _retrieve("call-evidence"), [assistant_message(CLARIFICATION)])
    outcome = _outcome(_run())
    assert (outcome["value"], outcome["basis"]) == ("needs_clarification", "clarification_question")


def test_guide_is_not_checked_twice_or_after_the_model_looked_it_up(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # モデルが自分で業務ガイドを引いたら、Control Plane は照合しない（引いた結果に次の手を足す）。
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_lookup_guides",
                {"query": GOAL, "search_answer_profile_id": PROFILE},
                call_id="call-lookup",
            )
        ],
        _retrieve("call-evidence"),
        [assistant_message(CLARIFICATION)],
    )
    _run()
    assert len(mcp.calls_of("rag_lookup_guides")) == 1
    own = _tool_output(model, 1, "call-lookup")
    assert own["next_step"]["action"] == "ask_clarification"
    assert "guide_check" not in _tool_output(model, 2, "call-evidence")

    # 根拠を 2 回集めても、照合は 1 回だけ。
    mcp.tool_calls.clear()
    model = _script(
        monkeypatch,
        _retrieve("call-1"),
        _retrieve("call-2", query="グループ 権限"),
        [assistant_message(CLARIFICATION)],
    )
    _run()
    assert len(mcp.calls_of("rag_lookup_guides")) == 1
    assert "guide_check" not in _tool_output(model, 2, "call-2")


def test_known_condition_answers_the_matched_branch_and_stays_answered(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    answer = "利用者の詳細画面の「権限」タブで、付与する権限を選んで「付与」を押します。"
    model = _script(
        monkeypatch,
        _retrieve("call-evidence", conditions={"target": "個別"}),
        [assistant_message(answer)],
    )
    run = _run()
    assert mcp.calls_of("rag_lookup_guides")[0]["arguments"]["conditions"] == {"target": "個別"}
    note = _tool_output(model, 1, "call-evidence")["guide_check"]
    assert note["next_step"]["action"] == "answer_with_guide"
    outcome = _outcome(run)
    assert outcome["value"] == "answered"
    assert "guide_conditions" not in outcome["signals"]


def test_no_guide_check_without_the_lookup_tool_or_the_profile(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # 検索・回答プロファイルが分からなければ照合しない（rag_lookup_guides の必須の引数）。
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "アクセス権限"}, call_id="call-evidence"
            )
        ],
        [assistant_message(GRANT_ANSWER)],
    )
    run = _run()
    assert mcp.calls_of("rag_lookup_guides") == []
    assert "guide_check" not in _tool_output(model, 1, "call-evidence")
    assert _outcome(run)["value"] == "answered"


def test_answer_to_the_question_is_looked_up_with_the_previous_goal(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(monkeypatch, _retrieve("call-1"), [assistant_message(CLARIFICATION)])
    first = _run()
    assert _outcome(first)["value"] == "needs_clarification"

    # 次の Run の発言は問いへの答え。前の質問も照合に渡すので、付与先が分かった業務ガイドが当たる。
    reply = "個別の利用者に付けます。"
    answer = "利用者の詳細画面の「権限」タブで、付与する権限を選んで「付与」を押します。"
    model = _script(monkeypatch, _retrieve("call-2"), [assistant_message(answer)])
    second = _run(reply, thread_id=first.thread_id)
    lookup = mcp.calls_of("rag_lookup_guides")[-1]["arguments"]
    assert lookup["query"].startswith(f"{GOAL} {reply}")
    note = _tool_output(model, 1, "call-2")["guide_check"]
    assert note["guide"]["decision"] == "answer"
    assert _outcome(second)["value"] == "answered"


# ---- 業務ガイドの確認と Agent 自身の確認（#2726） ---------------------------------------------

SCOPE_REPLY = "個別の利用者だけに付与し、グループには付与しません。"


def test_guide_clarification_counts_as_a_pending_tool_question_until_answered() -> None:
    from app.features.agent.runtime import RunStep
    from app.features.agent.support_task import (
        guide_clarification_pending,
        tool_clarification_pending,
    )
    from app.features.agent.tools import ToolCall, ToolResult

    def step(name: str, output: dict[str, Any], index: int) -> RunStep:
        return RunStep(
            id=f"step-{index}",
            run_id="run-2726",
            title=name,
            status="completed",
            tool_call=ToolCall(name=f"rag__{name}", arguments={}),
            tool_result=ToolResult(name=f"rag__{name}", success=True, output=output),
        )

    clarify = step("rag_lookup_guides", {"guides": [_access_guide("clarify")]}, 1)
    evidence = step("rag_retrieve_evidence", {"evidence": [{"chunk_id": "c"}]}, 2)
    assert [item["condition_id"] for item in guide_clarification_pending([clarify, evidence])] == [
        "target"
    ]
    assert tool_clarification_pending([clarify, evidence])
    # 答えた条件は数えない（カードで聞き直さない）。
    assert not tool_clarification_pending([clarify, evidence], {"target"})
    # 条件が分かった新しい判断（答える）が、前の確かめる判断に代わる。
    answer = step("rag_lookup_guides", {"guides": [_access_guide("answer", {"target": "個別"})]}, 3)
    assert not tool_clarification_pending([clarify, evidence, answer])
    searched = step("rag_search", {"outcome": "answered", "guide": _access_guide("answer")}, 4)
    assert not guide_clarification_pending([clarify, searched])


def test_agent_question_on_a_guide_condition_becomes_the_condition_card(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """業務ガイドが条件を確かめる判断のとき、Agent 自身の確認ではなく条件の id 付きのカードで聞く。

    答え（自由入力）は次の Run で業務ガイドの条件になり、照合・最終の検証の業務ガイドに渡る。
    """
    from app.features.agent.answer_validation import connection_check_inputs
    from app.features.agent.confirmation import CONFIRMATION_TOOL, ConfirmationAnswer

    ask = {
        "question": "権限の付与先は、個別の利用者ですか、グループですか？",
        "reason": "付与先で手順が変わります。",
        "options": [
            {"label": "個別", "description": ""},
            {"label": "グループ", "description": ""},
        ],
        "multiple": False,
    }
    model = _script(
        monkeypatch,
        _retrieve("call-evidence"),
        [function_call(CONFIRMATION_TOOL, ask, call_id="call-ask")],
        [assistant_message("付与先の確認事項に答えてください。")],
    )
    first = _run()
    assert first.status == RunStatus.COMPLETED
    assert _tool_output(model, 2, "call-ask")["status"] == "tool_confirmation_pending"
    # Agent 自身の確認は出さず、業務ガイドの条件の確認を 1 つだけ出す。
    [card] = first.confirmations
    assert card.source == "rag"
    [item] = card.items
    assert item.id == "target"
    assert [option.label for option in item.options] == ["個別", "グループ"]

    outcome = runtime_repository.decide_confirmation(
        first.id,
        card.id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="target", other_text=SCOPE_REPLY)],
    )
    next_run = outcome.next_run
    assert next_run is not None
    answer = "利用者の詳細画面の「権限」タブで、付与する権限を選んで「付与」を押します。"
    model = _script(monkeypatch, _retrieve("call-2"), [assistant_message(answer)])
    anyio.run(builtin_runtime.execute_run, next_run.id)
    second = runtime_repository.get_run(next_run.id)
    assert second.status == RunStatus.COMPLETED
    # 答えは業務ガイドの照合の条件として渡り、条件の分かった判断で答える。
    lookup = mcp.calls_of("rag_lookup_guides")[-1]["arguments"]
    assert lookup["conditions"] == {"target": SCOPE_REPLY}
    assert _tool_output(model, 1, "call-2")["guide_check"]["guide"]["decision"] == "answer"
    # 同じ条件の確認を出し直さない（回答済みの確認と確かめ中の確認が食い違わない）。
    assert second.confirmations == []
    state = run_support_task(second)
    assert state is not None
    assert state["pending_clarifications"] == []
    assert state["known_conditions"]["target"]["value"] == SCOPE_REPLY
    # 最終の検証には、条件の分かった業務ガイドを渡す。
    inputs = connection_check_inputs(list(second.steps), "rag__rag_retrieve_evidence")
    assert inputs["guide"]["guide_id"] == "g-access"
    assert inputs["guide"]["conditions"] == {"target": SCOPE_REPLY}
