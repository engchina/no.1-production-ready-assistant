"""回答前の確認（#1839）。

決定論のテスト。モデルは SDK の `ScriptedModel`、RAG / NL2SQL は契約どおりの fake の MCP
（`mcp_support`）にする。

- Agent 自身の確認（`ask_user_confirmation`）: Run は `waiting_confirmation` で止まり、答え・
  スキップで同じ Run を再開する（ツールの結果が利用者の答え）。やめると Run を取り消し、会話から
  外して質問を返す。
- 業務 Agent の方針（前提を示して回答する）・確認の上限・無人の Run では確認しない。
- ツールの結果の確認（NL2SQL・RAG）: 完了した Run にカードの確認を出し、答えを構造化して次の Run に
  渡す（RAG は分かっている条件、NL2SQL は `nl2sql_query` の引数）。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents import FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from mcp_support import (
    DEFAULT_OUTPUTS,
    FakeProductMcp,
    fake_product_mcp,
    nl2sql_needs_clarification,
)
from pr_backend_core.chat_progress import ChatProgressStepEvent
from pydantic import ValidationError
from pytest import MonkeyPatch
from trace_support import run_progress_events

from app.features.agent import builtin_runtime, router
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.confirmation import (
    CONFIRMATION_TOOL,
    RESPONSE_METADATA_KEY,
    ROUND_METADATA_KEY,
    SKIPPED_INSTRUCTION,
    TOOL_FAILED_INSTRUCTION,
    WITHDRAWN_METADATA_KEY,
    ConfirmationAnswer,
    ConfirmationAnswerError,
    ConfirmationBudget,
    ConfirmationItem,
    ConfirmationOption,
    ConfirmationRequest,
    asks_data_schema,
    card_answer,
    is_confirmation_payload,
    item_from_arguments,
    items_from_pending,
    nl2sql_answer,
    policy_instructions,
    rag_conditions,
    record_entries,
    response_instructions,
    response_record,
    validate_answers,
    validation_premises,
)
from app.features.agent.runtime import (
    EVALUATION_DRY_RUN_KEY,
    AgentProfile,
    AgentProfilePatch,
    ConfirmationClosedError,
    RunCreateRequest,
    RunState,
    RunStatus,
    reserved_run_metadata_keys,
    run_support_task,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import (
    NL2SQL_ANSWER_FROM_USER_INSTRUCTION,
    assume_step,
    nl2sql_settles,
    nl2sql_skip_step,
)
from app.features.agent.tools import ToolCall, ToolPolicy, ToolResult
from app.main import app
from app.settings import get_settings

SKILL_ID = "test1839-skill"
AGENT_ID = "test1839-agent"
USER_UUID = "11111111-2222-3333-4444-666666666666"
PROFILE = "profile-sales"
GOAL = "売上を教えて"
RAG_QUESTION = {
    "condition_id": "screen",
    "label": "画面",
    "question": "どの画面の操作ですか？",
    "options": ["受注入力", "請求"],
}
ASK_ARGUMENTS = {
    "question": "どの期間の売上を見ますか？",
    "reason": "期間が質問から決められません。",
    "options": [
        {"label": "今月", "description": ""},
        {"label": "今年度", "description": "4 月から"},
    ],
    "multiple": False,
}


def _rag_search(argument: Any) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    if "conditions" not in argument.model_fields_set:
        output.update(
            answer="どの画面か確かめてください。",
            outcome="needs_clarification",
            clarifications=[RAG_QUESTION],
        )
    return output


def _nl2sql_query(argument: Any) -> dict[str, Any]:
    if argument.requirement_session_id is None:
        return nl2sql_needs_clarification()
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["nl2sql_query"])
    return output


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(
        monkeypatch, outputs={"nl2sql_query": _nl2sql_query, "rag_search": _rag_search}
    )
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    # 台本のモデルはツールを呼ばずに答えるので、ツールで確かめていない回答の拒答（#1835）は外す
    # （ここで確かめるのは確認の往復。拒答のガードは test_tool_grounding.py）。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    monkeypatch.setattr(
        builtin_runtime, "_active_policy", lambda: ToolPolicy(allow={"nl2sql__nl2sql_query"})
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="資料は rag_search、数値は nl2sql_query で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_query"]),
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
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run(
    goal: str = GOAL, *, thread_id: str | None = None, metadata: dict[str, Any] | None = None
) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal=goal, agent_id=AGENT_ID, thread_id=thread_id, metadata=metadata or {}
        ),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _resume(run_id: str) -> RunState:
    anyio.run(builtin_runtime.resume_run, run_id)
    return runtime_repository.get_run(run_id)


def _execute(run_id: str) -> RunState:
    anyio.run(builtin_runtime.execute_run, run_id)
    return runtime_repository.get_run(run_id)


def _ask(call_id: str, **extra: Any) -> list[Any]:
    return [function_call(CONFIRMATION_TOOL, {**ASK_ARGUMENTS, **extra}, call_id=call_id)]


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _tool_names(model: ScriptedModel, call_index: int = 0) -> set[str]:
    return {tool.name for tool in model.calls[call_index].tools if isinstance(tool, FunctionTool)}


def _set_policy(policy: str, max_rounds: int = 2) -> None:
    runtime_repository.patch_agent(
        AGENT_ID,
        AgentProfilePatch(clarification_policy=policy, clarification_max_rounds=max_rounds),
    )
    runtime_repository.publish_agent(AGENT_ID)


# ---- 決定的な部品 ------------------------------------------------------------------------------


def test_tool_arguments_become_one_item_with_short_options() -> None:
    item = item_from_arguments(
        {
            "question": "  どの期間？ ",
            "reason": "理由",
            "options": [
                {"label": "今月", "description": "x"},
                {"label": "今月", "description": "重複は外す"},
                {"label": "", "description": "空は外す"},
                {"label": "その他", "description": "画面が出すので外す"},
                *({"label": f"選択肢{index}", "description": ""} for index in range(12)),
            ],
            "multiple": True,
        }
    )
    assert (item.prompt, item.reason, item.selection) == ("どの期間？", "理由", "multiple")
    assert [option.label for option in item.options][:2] == ["今月", "選択肢0"]
    assert len(item.options) == 8
    assert [option.id for option in item.options][:2] == ["option_1", "option_2"]


def test_answers_are_validated_against_the_items() -> None:
    request = ConfirmationRequest(
        source="agent",
        items=[
            ConfirmationItem(
                id="item_1",
                prompt="どの期間？",
                options=[ConfirmationOption(id="a", label="今月")],
            )
        ],
    )
    [answer] = validate_answers(
        request, [ConfirmationAnswer(item_id="item_1", option_ids=["a", "a"])]
    )
    assert answer.option_ids == ["a"]
    assert validate_answers(request, [ConfirmationAnswer(item_id="item_1", other_text=" 先月 ")])[
        0
    ].other_text == ("先月")
    with pytest.raises(ConfirmationAnswerError):
        validate_answers(request, [])
    with pytest.raises(ConfirmationAnswerError):
        validate_answers(request, [ConfirmationAnswer(item_id="item_1", option_ids=["z"])])
    with pytest.raises(ConfirmationAnswerError):
        # 1 つだけ選ぶ確認事項では、選択肢と自由入力はどちらか一方。
        validate_answers(
            request, [ConfirmationAnswer(item_id="item_1", option_ids=["a"], other_text="x")]
        )
    with pytest.raises(ValidationError):
        ConfirmationAnswer(item_id="", option_ids=[])


def test_record_entries_keep_whether_the_item_had_options() -> None:
    """会話に残す記録は、選択肢の無い確認事項を `free_text_only` で残す。

    画面はこの値で、自由入力の答えに「その他: 」を付けない。
    """
    request = ConfirmationRequest(
        source="rag",
        items=[
            ConfirmationItem(
                id="item_1",
                prompt="どの画面？",
                options=[ConfirmationOption(id="a", label="画面 A")],
            ),
            ConfirmationItem(id="item_2", prompt="対象の名前は？"),
        ],
        answers=[
            ConfirmationAnswer(item_id="item_1", other_text="画面 B"),
            ConfirmationAnswer(item_id="item_2", other_text="名前 A"),
        ],
    )
    entries = record_entries(request)
    assert [(entry["other_text"], entry["free_text_only"]) for entry in entries] == [
        ("画面 B", False),
        ("名前 A", True),
    ]
    response = response_record(
        request, action="answer", source_run_id="run-a", question_run_id="run-a"
    )
    assert [entry["free_text_only"] for entry in response["entries"]] == [False, True]


def test_budget_and_policy_instructions() -> None:
    budget = ConfirmationBudget(policy="confirm", max_rounds=2, used=1)
    assert budget.asks and budget.remaining == 1
    exposed = policy_instructions(budget, tool_exposed=True)
    assert "あと 1 回" in exposed
    # 業務の手順・規程・業務データの質問は、曖昧でも先に資料・業務データのツールで調べる（#1881）。
    assert "を呼ぶ前に、先に資料・業務データのツールで調べてください" in exposed
    assert "結果を見ても決まらないときだけ ask_user_confirmation で聞いてください" in exposed
    # 推薦の候補が割れたら、本文の表で選ばせずに確認のツールで聞く（#1881）。
    assert "本文の表や箇条書きで選ばせずに ask_user_confirmation で聞いてください" in exposed
    assert budget.reserve() == 2
    assert budget.reserve() is None
    assert "上限（2 回）に達しました" in policy_instructions(budget, tool_exposed=False)
    assume = ConfirmationBudget(policy="assume")
    assert not assume.asks
    assert "前提を示して回答します" in policy_instructions(assume, tool_exposed=False)
    assert policy_instructions(ConfirmationBudget(unattended=True), tool_exposed=False) == ""
    # 「確認せずに回答」を選んだ Run（#1903）: 方針は前提を示して回答し、NL2SQL の確認は
    # skip_clarification で呼び直させる。
    skipped = ConfirmationBudget(policy="confirm", skipped=True)
    assert skipped.policy == "assume" and not skipped.asks and skipped.reserve() is None
    text = policy_instructions(skipped, tool_exposed=True)
    assert "「確認せずに回答」を選びました" in text
    assert "skip_clarification: true" in text
    assert "ask_user_confirmation" not in text


def test_nl2sql_skip_step_retries_once_then_answers_with_assumptions() -> None:
    output = nl2sql_needs_clarification()
    arguments = {"question": GOAL, "profile_id": PROFILE}
    step = nl2sql_skip_step(output, arguments, tool_name="nl2sql__nl2sql_query")
    assert step is not None and step["action"] == "retry_without_clarification"
    assert step["arguments"] == {
        "question": GOAL,
        "profile_id": PROFILE,
        "requirement_session_id": "qs-1",
        "skip_clarification": True,
    }
    # skip_clarification で呼んでも確認事項が返ったら、呼び直させずに前提を示して答えさせる。
    again = nl2sql_skip_step(output, {**step["arguments"]}, tool_name="nl2sql__nl2sql_query")
    assert again is not None and again["action"] == "answer_with_assumptions"
    assert nl2sql_skip_step({"status": "completed"}, arguments, tool_name="q") is None


def test_assume_step_keeps_nl2sql_but_answers_rag_with_assumptions() -> None:
    ask = {"action": "ask_clarification", "reason": "needs_clarification", "questions": [{}]}
    assumed = assume_step(ask)
    assert assumed is not None and assumed["action"] == "answer_with_assumptions"
    nl2sql = {**ask, "kind": "nl2sql"}
    assert assume_step(nl2sql) is nl2sql
    other = {"action": "handoff"}
    assert assume_step(other) is other


def test_response_instructions_pass_structured_answers() -> None:
    rag = {
        "source": "rag",
        "action": "answer",
        "entries": [
            {
                "item_id": "screen",
                "prompt": "どの画面？",
                "selected": ["受注入力"],
                "other_text": "",
            }
        ],
    }
    assert rag_conditions(rag) == {"screen": "受注入力"}
    text = response_instructions(rag)
    assert '"screen": "受注入力"' in text and "rag_search の conditions" in text

    entry = {
        "item_id": "item-period",
        "prompt": "どの列？",
        "selected": ["受注日"],
        "option_ids": ["c-order"],
        "other_text": "",
    }
    assert nl2sql_answer(entry) == {"item_id": "item-period", "selected_candidate_ids": ["c-order"]}
    # 候補と自由入力の両方は、どちらも捨てずに 1 つの free_text にする。
    both = nl2sql_answer({**entry, "other_text": "月末締め"})
    assert both == {"item_id": "item-period", "free_text": "受注日。その他: 月末締め"}
    nl2sql = {
        "source": "nl2sql",
        "action": "answer",
        "entries": [entry],
        "context": {"requirement_session_id": "qs-1", "profile_id": PROFILE, "question": GOAL},
    }
    text = response_instructions(nl2sql)
    assert '"requirement_session_id": "qs-1"' in text
    assert '"selected_candidate_ids": ["c-order"]' in text
    skipped = response_instructions({"source": "rag", "action": "skip", "entries": [entry]})
    assert "確認せずに回答を求めました" in skipped
    assert response_instructions(None) == ""


def test_confirmation_metadata_is_reserved() -> None:
    assert reserved_run_metadata_keys({RESPONSE_METADATA_KEY: {}, ROUND_METADATA_KEY: 1}) == [
        RESPONSE_METADATA_KEY,
        ROUND_METADATA_KEY,
    ]


def test_agent_profile_keeps_the_policy_in_versions(mcp: FakeProductMcp) -> None:
    _set_policy("assume", 3)
    agent = next(item for item in runtime_repository.list_agents() if item.id == AGENT_ID)
    assert (agent.clarification_policy, agent.clarification_max_rounds) == ("assume", 3)
    published = agent.published()
    assert published is not None
    assert (published.clarification_policy, published.clarification_max_rounds) == ("assume", 3)
    # 0 は既定（2 回）。1 回は選べない（NL2SQL の業務プロファイルと同じ）。
    assert AgentProfilePatch(clarification_max_rounds=0).clarification_max_rounds == 0
    with pytest.raises(ValidationError):
        AgentProfilePatch(clarification_max_rounds=1)
    with pytest.raises(ValidationError):
        AgentProfilePatch(clarification_max_rounds=7)
    with pytest.raises(ValidationError):
        AgentProfilePatch(clarification_policy="never")


# ---- Agent 自身の確認（ask_user_confirmation） -----------------------------------------------


def test_agent_confirmation_waits_and_the_answer_resumes_the_same_run(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        _ask("call-ask"),
        [assistant_message("今年度の売上は 1,200 です。")],
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    assert CONFIRMATION_TOOL in _tool_names(model)
    assert "回答前の確認" in str(model.calls[0].system_instructions)
    [confirmation] = run.confirmations
    assert (confirmation.source, confirmation.call_id, confirmation.round) == (
        "agent",
        "call-ask",
        1,
    )
    assert confirmation.items[0].prompt == "どの期間の売上を見ますか？"
    assert [option.label for option in confirmation.items[0].options] == ["今月", "今年度"]
    assert run.approvals == []
    # 待っている間は処理の経過に待ちの行を出さない（見出しの状態とカードで示す。#3226）。
    assert not any(
        getattr(event, "kind", None) == "confirmation" for event in run_progress_events(run)
    )

    outcome = runtime_repository.decide_confirmation(
        run.id,
        confirmation.id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", option_ids=["option_2"])],
    )
    assert outcome.run.status == RunStatus.QUEUED and outcome.next_run is None
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    seen = _tool_output(model, 1, "call-ask")
    assert (seen["status"], seen["selected"]) == ("answered", ["今年度"])
    assert resumed.confirmations[0].status == "answered"
    # 答えを待った区間は、再開した区間の span（回答前の確認の待ち）として残る（#3226）。確認の
    # ツールの呼び出しは処理の経過に出さない。
    waits = [
        event
        for event in run_progress_events(resumed)
        if isinstance(event, ChatProgressStepEvent) and event.name == "agent.confirmation_wait"
    ]
    assert [(event.kind, event.status, event.params) for event in waits] == [
        ("confirmation", "done", {"round": 1})
    ]
    assert not any(
        (getattr(event, "params", None) or {}).get("tool") == CONFIRMATION_TOOL
        for event in run_progress_events(resumed)
    )
    # 確認のツールの呼び出しも step（監査）に残る。
    assert any(
        step.tool_call is not None and step.tool_call.name == CONFIRMATION_TOOL
        for step in resumed.steps
    )
    with pytest.raises(ConfirmationClosedError):
        runtime_repository.decide_confirmation(run.id, confirmation.id, action="skip")


def test_agent_confirmation_skip_tells_the_model_to_answer_with_assumptions(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(monkeypatch, _ask("call-ask"), [assistant_message("前提: 今月として…")])
    run = _run()
    runtime_repository.decide_confirmation(run.id, run.confirmations[0].id, action="skip")
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    seen = _tool_output(model, 1, "call-ask")
    assert seen["status"] == "skipped"
    assert "前提" in seen["instruction"]


def test_agent_confirmation_skip_retries_nl2sql_without_a_card(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """Agent 自身の確認に「確認せずに回答」を選んだ Run の続き（#1903）。

    NL2SQL が確認事項を返しても、カードを出さずに skip_clarification で呼び直させる。
    """
    query = {"question": "全期間の売上合計金額を教えてください。", "profile_id": PROFILE}
    skip = {**query, "requirement_session_id": "qs-1", "skip_clarification": True}
    model = _script(
        monkeypatch,
        _ask("call-ask"),
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [function_call("nl2sql__nl2sql_query", skip, call_id="call-2")],
        [assistant_message("前提: 全期間・受注日で集計しました。営業が 1,200 です。")],
    )
    run = _run()
    runtime_repository.decide_confirmation(run.id, run.confirmations[0].id, action="skip")
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    # 再開した Run の指示は「確認せずに回答」の方針（NL2SQL は skip_clarification で呼び直す）。
    instructions = str(model.calls[1].system_instructions)
    assert "「確認せずに回答」を選びました" in instructions
    assert "skip_clarification: true" in instructions
    assert "nl2sql__nl2sql_query" in instructions
    # NL2SQL の確認事項の結果には、聞かずに呼び直す次の手と引数を付ける。
    next_step = _tool_output(model, 2, "call-1")["next_step"]
    assert next_step["action"] == "retry_without_clarification"
    assert next_step["arguments"] == skip
    sent = mcp.calls_of("nl2sql_query")[-1]["arguments"]
    assert (sent["requirement_session_id"], sent["skip_clarification"]) == ("qs-1", True)
    # カードは出さない（Agent 自身の確認のスキップだけが残る）・確かめ中の問いも残さない。
    assert [(item.source, item.status) for item in resumed.confirmations] == [("agent", "skipped")]
    state = run_support_task(resumed)
    assert state is not None and state["pending_clarifications"] == []


def test_agent_confirmation_skip_never_shows_a_tool_card(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """呼び直さずに本文で答えても、NL2SQL・RAG の確認のカードは出さない（#1903 のガード）。"""
    query = {"question": GOAL, "profile_id": PROFILE}
    model = _script(
        monkeypatch,
        _ask("call-ask"),
        [
            function_call("nl2sql__nl2sql_query", query, call_id="call-1"),
            function_call("rag__rag_search", {"query": "売上 定義"}, call_id="call-2"),
        ],
        _ask("call-ask-2", question="どのテーブルから売上金額を取得しますか？"),
        [assistant_message("前提: 全期間で集計する想定です。")],
    )
    run = _run()
    runtime_repository.decide_confirmation(run.id, run.confirmations[0].id, action="skip")
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    assert _tool_output(model, 2, "call-2")["next_step"]["action"] == "answer_with_assumptions"
    # 2 回目の ask_user_confirmation は中断せず、確認せずに答える指示を返す。
    again = _tool_output(model, 3, "call-ask-2")
    assert (again["status"], again["instruction"]) == ("skipped", SKIPPED_INSTRUCTION)
    assert [(item.source, item.status) for item in resumed.confirmations] == [("agent", "skipped")]
    state = run_support_task(resumed)
    assert state is not None and state["pending_clarifications"] == []


def test_agent_confirmation_answer_keeps_the_nl2sql_card(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """答えた（スキップではない）ときは今どおり、NL2SQL の確認はカードにする（#1903）。"""
    query = {"question": GOAL, "profile_id": PROFILE}
    _script(
        monkeypatch,
        _ask("call-ask"),
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message("どの日付の列で期間を絞りますか？")],
    )
    run = _run()
    runtime_repository.decide_confirmation(
        run.id,
        run.confirmations[0].id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", option_ids=["option_1"])],
    )
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    assert [(item.source, item.status, item.round) for item in resumed.confirmations] == [
        ("agent", "answered", 1),
        ("nl2sql", "pending", 2),
    ]


def test_agent_confirmation_cancel_withdraws_the_turn_and_returns_the_question(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(monkeypatch, [assistant_message("前の回答")])
    first = _run("前の質問")
    _script(monkeypatch, _ask("call-ask"))
    run = _run(thread_id=first.thread_id)
    outcome = runtime_repository.decide_confirmation(
        run.id, run.confirmations[0].id, action="cancel"
    )
    assert outcome.question == GOAL
    cancelled = runtime_repository.get_run(run.id)
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.metadata[WITHDRAWN_METADATA_KEY] is True
    assert cancelled.confirmations[0].status == "cancelled"
    thread = runtime_repository.get_thread(str(first.thread_id), user_uuid=USER_UUID)
    assert [item.id for item in thread.runs] == [first.id]
    [summary] = [
        item
        for item in runtime_repository.list_threads(user_uuid=USER_UUID, agent_id=AGENT_ID)
        if item.thread_id == first.thread_id
    ]
    assert summary.run_count == 1


def test_assume_policy_does_not_offer_the_tool(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _set_policy("assume")
    model = _script(monkeypatch, [assistant_message("前提を示して答えます。")])
    run = _run()
    assert run.status == RunStatus.COMPLETED
    assert CONFIRMATION_TOOL not in _tool_names(model)
    assert "前提を示して回答します" in str(model.calls[0].system_instructions)


def test_unattended_runs_do_not_offer_the_tool(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(monkeypatch, [assistant_message("答え")])
    run = _run(metadata={EVALUATION_DRY_RUN_KEY: True})
    assert run.status == RunStatus.COMPLETED
    assert CONFIRMATION_TOOL not in _tool_names(model)
    assert "回答前の確認" not in str(model.calls[0].system_instructions)


def test_the_round_limit_answers_without_asking(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        _ask("call-1"),
        _ask("call-2", question="もう 1 つ確認？"),
        [assistant_message("前提を示して答えます。")],
    )
    # 前の往復で 2 回確認した（既定の上限 3 回の 3 回目）。
    run = _run(metadata={ROUND_METADATA_KEY: 2})
    assert run.status == RunStatus.WAITING_CONFIRMATION
    assert (run.confirmations[0].round, run.confirmations[0].max_rounds) == (3, 3)
    runtime_repository.decide_confirmation(
        run.id,
        run.confirmations[0].id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", other_text="先月")],
    )
    resumed = _resume(run.id)
    # 上限（3 回）を超えた呼び出しは中断せず、上限に達したことを返す。
    assert resumed.status == RunStatus.COMPLETED
    assert len(resumed.confirmations) == 1
    assert _tool_output(model, 1, "call-1")["other_text"] == "先月"
    assert _tool_output(model, 2, "call-2")["status"] == "limit_reached"


# ---- ツールの結果の確認（RAG・NL2SQL） -------------------------------------------------------


def test_rag_clarification_becomes_a_card_and_the_answer_is_passed_as_conditions(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("どの画面の操作ですか？（受注入力 / 請求）")],
    )
    first = _run("受注を取り消す手順は？")
    assert first.status == RunStatus.COMPLETED
    [confirmation] = first.confirmations
    assert (confirmation.source, confirmation.can_skip, confirmation.round) == ("rag", True, 1)
    [item] = confirmation.items
    assert (item.id, item.prompt) == ("screen", "どの画面の操作ですか？")
    assert [option.label for option in item.options] == ["受注入力", "請求"]
    assert "画面" in item.reason

    outcome = runtime_repository.decide_confirmation(
        first.id,
        confirmation.id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="screen", option_ids=["option_1"])],
    )
    next_run = outcome.next_run
    assert next_run is not None and next_run.thread_id == first.thread_id
    response = next_run.metadata[RESPONSE_METADATA_KEY]
    assert (response["source"], response["action"], response["outcome"]) == (
        "rag",
        "answer",
        "answered",
    )
    assert response["question_run_id"] == first.id
    assert response["entries"][0]["selected"] == ["受注入力"]
    assert next_run.metadata[ROUND_METADATA_KEY] == 1
    assert next_run.goal == "どの画面の操作ですか？: 受注入力"

    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search",
                {"query": "取り消し 手順", "conditions": {"screen": "受注入力"}},
                call_id="call-2",
            )
        ],
        [assistant_message("受注入力の画面で取り消します。")],
    )
    second = _execute(next_run.id)
    assert second.status == RunStatus.COMPLETED
    instructions = str(model.calls[0].system_instructions)
    assert "回答前の確認の答え" in instructions
    assert '"screen": "受注入力"' in instructions
    state = run_support_task(second)
    assert state is not None
    assert state["known_conditions"]["screen"]["value"] == "受注入力"
    assert state["known_conditions"]["screen"]["source"] == "user_answer"
    assert second.confirmations == []


def test_validation_premises_are_the_answered_items() -> None:
    """最終の検証に渡す前提は、答えた確認事項の文と答え（選択肢の名前・自由入力）（#1898）。

    この Run の Agent 自身の確認（答えたものだけ）と、前の Run の確認に答えて作った Run の
    `clarification_response`。「確認せずに回答」・答えを待つ確認は前提にしない。
    """
    item = item_from_arguments(ASK_ARGUMENTS)
    answered = ConfirmationRequest(
        source="agent",
        items=[item],
        status="answered",
        answers=[ConfirmationAnswer(item_id="item_1", option_ids=["option_2"])],
    )
    other = ConfirmationRequest(
        source="agent",
        items=[item],
        status="answered",
        answers=[ConfirmationAnswer(item_id="item_1", other_text="  先月 と 今月  ")],
    )
    pending = ConfirmationRequest(source="agent", items=[item])
    response = {
        "source": "rag",
        "action": "answer",
        "entries": [
            {"item_id": "screen", "prompt": "どの画面の操作ですか？", "selected": ["受注入力"]},
            {"item_id": "empty", "prompt": "空", "selected": [], "other_text": ""},
        ],
    }

    premises = validation_premises(
        {RESPONSE_METADATA_KEY: response}, [answered, other, pending, answered]
    )

    assert premises == [
        {"question": "どの画面の操作ですか？", "answer": "受注入力"},
        {"question": "どの期間の売上を見ますか？", "answer": "今年度"},
        {"question": "どの期間の売上を見ますか？", "answer": "先月 と 今月"},
    ]
    skipped = {**response, "action": "skip"}
    assert validation_premises({RESPONSE_METADATA_KEY: skipped}, [pending]) == []
    assert validation_premises({}, []) == []


def test_final_validation_receives_the_agent_confirmation_answer(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """Agent 自身の確認の答えを、最終の検証（rag_validate_answer）に前提として渡す（#1898）。"""
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    _script(
        monkeypatch,
        _ask("call-ask"),
        [function_call("rag__rag_search", {"query": "売上 今年度"}, call_id="call-1")],
        [assistant_message("今年度の売上は 1,200 です。")],
    )
    run = _run()
    runtime_repository.decide_confirmation(
        run.id,
        run.confirmations[0].id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", option_ids=["option_2"])],
    )
    resumed = _resume(run.id)

    assert resumed.status == RunStatus.COMPLETED
    [call] = mcp.calls_of("rag_validate_answer")
    assert call["arguments"]["premises"] == [
        {"question": "どの期間の売上を見ますか？", "answer": "今年度"}
    ]


def test_final_validation_receives_the_tool_confirmation_answer(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """前の Run の RAG の確認への答え（`clarification_response`）も前提として渡す（#1898）。

    答えの無い Run（確認を出した Run）の検証には前提を付けない。
    """
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("どの画面の操作ですか？（受注入力 / 請求）")],
    )
    first = _run("受注を取り消す手順は？")
    outcome = runtime_repository.decide_confirmation(
        first.id,
        first.confirmations[0].id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="screen", option_ids=["option_1"])],
    )
    assert outcome.next_run is not None
    _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search",
                {"query": "取り消し 手順", "conditions": {"screen": "受注入力"}},
                call_id="call-2",
            )
        ],
        [assistant_message("受注入力の画面で取り消します。")],
    )
    second = _execute(outcome.next_run.id)

    assert second.status == RunStatus.COMPLETED
    calls = mcp.calls_of("rag_validate_answer")
    assert all("premises" not in call["arguments"] for call in calls[:-1])
    assert calls[-1]["arguments"]["premises"] == [
        {"question": "どの画面の操作ですか？", "answer": "受注入力"}
    ]


def test_rag_skip_answers_with_assumptions_without_asking_again(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("どの画面ですか？")],
    )
    first = _run("受注を取り消す手順は？")
    outcome = runtime_repository.decide_confirmation(
        first.id, first.confirmations[0].id, action="skip"
    )
    assert outcome.next_run is not None
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-2")],
        [assistant_message("画面ごとに分けて示します。")],
    )
    second = _execute(outcome.next_run.id)
    assert second.status == RunStatus.COMPLETED
    assert CONFIRMATION_TOOL not in _tool_names(model)
    seen = _tool_output(model, 1, "call-2")
    assert seen["next_step"]["action"] == "answer_with_assumptions"
    # 聞き直さない（カードを出さない・確かめ中の問いを残さない）。
    assert second.confirmations == []
    state = run_support_task(second)
    assert state is not None and state["pending_clarifications"] == []


def test_assume_policy_answers_rag_clarifications_with_assumptions(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _set_policy("assume")
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("画面ごとに示します。")],
    )
    run = _run("受注を取り消す手順は？")
    assert _tool_output(model, 1, "call-1")["next_step"]["action"] == "answer_with_assumptions"
    assert run.confirmations == []


def test_tool_clarification_is_skipped_only_when_not_asking() -> None:
    """呼び先の回答前の確認を出させないのは、利用者に確認しない Run だけ（#2117）。"""
    assert not ConfirmationBudget().skips_tool_clarification
    assert ConfirmationBudget(policy="assume").skips_tool_clarification
    assert ConfirmationBudget(skipped=True).skips_tool_clarification
    assert ConfirmationBudget(max_rounds=1, used=1).skips_tool_clarification
    # 品質評価は評価ケースの答えで答える（#1861）。
    assert ConfirmationBudget(unattended=True).skips_tool_clarification
    assert not ConfirmationBudget(unattended=True, evaluation=True).skips_tool_clarification
    assert ConfirmationBudget(
        unattended=True, evaluation=True, policy="assume"
    ).skips_tool_clarification


def test_rag_search_gets_skip_clarification_when_the_agent_does_not_ask(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """確認しない Run は Control Plane が rag_search に skip_clarification を付ける（#2117）。"""
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("どの画面ですか？")],
    )
    _run("受注を取り消す手順は？")
    assert "skip_clarification" not in mcp.calls_of("rag_search")[-1]["arguments"]

    _set_policy("assume")
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-2")],
        [assistant_message("画面ごとに示します。")],
    )
    _run("受注を取り消す手順は？")
    assert mcp.calls_of("rag_search")[-1]["arguments"]["skip_clarification"] is True


def test_multiple_rag_clarification_is_a_multiple_selection_card() -> None:
    """複数選べるルールの確認は、カードでも複数選べる（#2117）。"""
    [multiple] = items_from_pending([{**RAG_QUESTION, "multiple": True}], "rag")
    assert multiple.selection == "multiple"
    [single] = items_from_pending([RAG_QUESTION], "rag")
    assert single.selection == "single"


def test_nl2sql_clarification_card_passes_the_answer_to_nl2sql_query(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    query = {"question": GOAL, "profile_id": PROFILE}
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message("どの日付の列で期間を絞りますか？")],
    )
    first = _run()
    [confirmation] = first.confirmations
    # NL2SQL の確認も「確認せずに回答」を出す（skip_clarification で呼び直す。#1839）。
    assert (confirmation.source, confirmation.can_skip) == ("nl2sql", True)
    assert [option.id for option in confirmation.items[0].options] == ["c-order", "c-ship"]
    assert confirmation.context["requirement_session_id"] == "qs-1"
    outcome = runtime_repository.decide_confirmation(
        first.id,
        confirmation.id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item-period", option_ids=["c-order"])],
    )
    assert outcome.next_run is not None
    answer = {"item_id": "item-period", "selected_candidate_ids": ["c-order"]}
    model = _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_query",
                {**query, "requirement_session_id": "qs-1", "answer": answer},
                call_id="call-2",
            )
        ],
        [assistant_message("営業が 1,200 です。")],
    )
    second = _execute(outcome.next_run.id)
    assert second.status == RunStatus.COMPLETED
    instructions = str(model.calls[0].system_instructions)
    assert '"selected_candidate_ids": ["c-order"]' in instructions
    assert "nl2sql__nl2sql_query を次の引数で" in instructions


def test_nl2sql_skip_calls_nl2sql_query_with_skip_clarification(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    query = {"question": GOAL, "profile_id": PROFILE}
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message("どの日付の列で期間を絞りますか？")],
    )
    first = _run()
    outcome = runtime_repository.decide_confirmation(
        first.id, first.confirmations[0].id, action="skip"
    )
    assert outcome.next_run is not None
    response = outcome.next_run.metadata[RESPONSE_METADATA_KEY]
    assert (response["action"], response["outcome"]) == ("skip", "skipped")
    skip = {**query, "requirement_session_id": "qs-1", "skip_clarification": True}
    model = _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", skip, call_id="call-2")],
        [assistant_message("前提: 受注日で集計しました。営業が 1,200 です。")],
    )
    second = _execute(outcome.next_run.id)
    assert second.status == RunStatus.COMPLETED
    instructions = str(model.calls[0].system_instructions)
    assert '"skip_clarification": true' in instructions
    assert '"requirement_session_id": "qs-1"' in instructions
    sent = mcp.calls_of("nl2sql_query")[-1]["arguments"]
    assert (sent["requirement_session_id"], sent["skip_clarification"]) == ("qs-1", True)
    assert "answer" not in sent
    # 確認せずに作ったので、確かめ中の問いもカードも残らない。
    assert second.confirmations == []
    state = run_support_task(second)
    assert state is not None and state["pending_clarifications"] == []


def test_nl2sql_contract_rejects_skip_with_answer() -> None:
    from mcp_support import Nl2SqlQueryIn

    with pytest.raises(ValidationError):
        Nl2SqlQueryIn(
            question=GOAL,
            profile_id=PROFILE,
            requirement_session_id="qs-1",
            answer={"item_id": "item-period", "selected_candidate_ids": ["c-order"]},
            skip_clarification=True,
        )


def test_nl2sql_cancel_withdraws_and_closes_the_requirement_session(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_query", {"question": GOAL, "profile_id": PROFILE}, call_id="c1"
            )
        ],
        [assistant_message("どの列ですか？")],
    )
    first = _run()
    outcome = runtime_repository.decide_confirmation(
        first.id, first.confirmations[0].id, action="cancel"
    )
    assert outcome.question == GOAL
    assert outcome.closed_sessions == [{"connection": "nl2sql", "requirement_session_id": "qs-1"}]
    anyio.run(builtin_runtime.close_requirement_sessions, first.id, outcome.closed_sessions)
    assert mcp.calls_of("nl2sql_cancel_requirement")[-1]["arguments"] == {
        "requirement_session_id": "qs-1"
    }
    assert runtime_repository.get_run(first.id).metadata[WITHDRAWN_METADATA_KEY] is True


def test_a_new_question_expires_the_previous_card(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("どの画面ですか？")],
    )
    first = _run("受注を取り消す手順は？")
    _script(monkeypatch, [assistant_message("別の答え")])
    _run("別の質問", thread_id=first.thread_id)
    assert runtime_repository.get_run(first.id).confirmations[0].status == "expired"


# ---- API ---------------------------------------------------------------------------------------


def test_confirmation_api_checks_owner_answers_and_state(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    client = TestClient(app)
    monkeypatch.setattr(router, "_schedule_builtin_run", lambda _run: None)
    created = client.post("/api/runs", json={"goal": GOAL, "agent_id": AGENT_ID})
    assert created.status_code == 200, created.text
    run_id = created.json()["data"]["id"]
    request = ConfirmationRequest(
        source="agent",
        call_id="call-ask",
        items=[item_from_arguments(ASK_ARGUMENTS)],
    )
    runtime_repository.request_builtin_approvals(run_id, [], state="{}", confirmations=[request])
    assert runtime_repository.get_run(run_id).status == RunStatus.WAITING_CONFIRMATION
    url = f"/api/runs/{run_id}/confirmations/{request.id}"

    invalid = client.post(url, json={"action": "answer", "answers": []})
    assert invalid.status_code == 422
    missing = client.post(
        f"/api/runs/{run_id}/confirmations/confirmation_x", json={"action": "skip"}
    )
    assert missing.status_code == 404

    answered = client.post(
        url,
        json={
            "action": "answer",
            "answers": [{"item_id": "item_1", "option_ids": ["option_1"], "other_text": ""}],
        },
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["data"]["run"]["status"] == "queued"
    closed = client.post(url, json={"action": "skip"})
    assert closed.status_code == 409

    # system_admin（local のローカル利用者）は、他の利用者の Run の確認には答えられない
    # （403。#3244。system_admin でない利用者の 404 は test_admin_operates_others.py）。
    # やめる（取り消し）は運用の操作としてできる。
    other = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=GOAL, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    other_request = ConfirmationRequest(
        source="agent", call_id="c", items=[item_from_arguments(ASK_ARGUMENTS)]
    )
    runtime_repository.request_builtin_approvals(
        other.id, [], state="{}", confirmations=[other_request]
    )
    other_url = f"/api/runs/{other.id}/confirmations/{other_request.id}"
    by_admin = client.post(other_url, json={"action": "skip"})
    assert by_admin.status_code == 403, by_admin.text
    assert runtime_repository.get_run(other.id).status == RunStatus.WAITING_CONFIRMATION
    withdrawn = client.post(other_url, json={"action": "cancel"})
    assert withdrawn.status_code == 200, withdrawn.text
    [decided] = [
        event.payload
        for event in runtime_repository.get_run(other.id).events
        if event.payload.get("confirmation_id") == other_request.id
        and "decided_by" in event.payload
    ]
    assert decided["decided_by"] == "local"
    assert runtime_repository.get_run(other.id).status == RunStatus.CANCELLED


# ---- 先に資料・業務データのツールで調べる（#1917） -------------------------------------------


def test_policy_instructions_leave_tool_owned_clarifications_to_the_tools() -> None:
    exposed = policy_instructions(ConfirmationBudget(policy="confirm"), tool_exposed=True)
    # 期間など NL2SQL・RAG が確かめる観点は、自分で先に聞かずに呼び先に任せる。
    assert "自分で先に" in exposed
    assert "利用者の質問のまま nl2sql_query" in exposed
    # 選択肢はツールの結果から作り、作れないときは自由入力にする。
    assert "options を空にして、自由入力で聞いてください" in exposed


def test_confirmation_before_any_tool_asks_with_free_text_only(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """ツールを呼ぶ前の確認も中断して聞き、モデルが作った選択肢は外して自由入力にする。

    中断せずに「先にツールで調べる」を返すと、モデルは確認を出したと取り違える（#3352）。
    選択肢はツールの結果で確かめられないので外す（#1917）。
    """
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    model = _script(
        monkeypatch,
        _ask("call-early"),
        [assistant_message("今年度の売上は 1,200 です。")],
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    assert len(model.calls) == 1
    [confirmation] = run.confirmations
    assert (confirmation.source, confirmation.call_id) == ("agent", "call-early")
    assert confirmation.items[0].prompt == "どの期間の売上を見ますか？"
    assert confirmation.items[0].options == []


_ANSWERED_QUERY = {"question": GOAL, "profile_id": PROFILE, "requirement_session_id": "qs-1"}


def test_confirmation_after_a_tool_keeps_the_options_in_its_result(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """ツールで調べた後の確認は中断し、ツールの結果にある選択肢を出す（#1917・#1934）。"""
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    options = [
        {"label": "営業", "description": ""},
        {"label": "部門別の売上（全期間）", "description": ""},
        {"label": "はい", "description": "答えの形はツールの結果に無くてもよい"},
    ]
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", _ANSWERED_QUERY, call_id="call-1")],
        _ask("call-ask", options=options),
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    [confirmation] = [item for item in run.confirmations if item.source == "agent"]
    assert [option.label for option in confirmation.items[0].options] == [
        "営業",
        "部門別の売上（全期間）",
        "はい",
    ]


def test_options_not_in_the_tool_results_become_free_text(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """ツールの結果に無い選択肢（モデルが作った期間）は外し、2 つ未満なら自由入力（#1934）。"""
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", _ANSWERED_QUERY, call_id="call-1")],
        _ask("call-ask"),
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    [confirmation] = run.confirmations
    assert confirmation.items[0].prompt == "どの期間の売上を見ますか？"
    assert confirmation.items[0].options == []


def test_agent_does_not_ask_again_what_nl2sql_asks(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """NL2SQL が確認事項を返した後の Agent 自身の確認は中断せず、NL2SQL のカードに任せる。"""
    query = {"question": GOAL, "profile_id": PROFILE}
    model = _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        _ask("call-ask"),
        [assistant_message("確認事項に答えてください。")],
    )
    run = _run()
    seen = _tool_output(model, 2, "call-ask")
    assert seen["status"] == "tool_confirmation_pending"
    assert run.status == RunStatus.COMPLETED
    [confirmation] = run.confirmations
    assert confirmation.source == "nl2sql"
    assert confirmation.items[0].prompt == "SALES のどの日付の列で期間を絞りますか？"


def test_option_grounding_ignores_spacing_width_and_trailing_notes() -> None:
    from app.features.agent.confirmation import ground_options, option_grounded

    corpus = '{"rows": [{"PRODUCT_NAME": "ノートパソコン"}], "profile": "MCP_TEST_SALES"}'
    key = "".join(corpus.split()).casefold()
    assert option_grounded("ノートパソコン（情報機器）", key)
    assert option_grounded("ＭＣＰ＿ＴＥＳＴ＿ＳＡＬＥＳ", key)
    assert option_grounded("いいえ", key)
    assert not option_grounded("2026年10月", key)
    item = item_from_arguments(
        {
            "question": "どの商品ですか？",
            "options": [
                {"label": "ノートパソコン", "description": ""},
                {"label": "タブレット", "description": ""},
                {"label": "MCP_TEST_SALES", "description": ""},
            ],
        }
    )
    grounded = ground_options(item, [corpus])
    assert [(option.id, option.label) for option in grounded.options] == [
        ("option_1", "ノートパソコン"),
        ("option_2", "MCP_TEST_SALES"),
    ]


def test_final_validation_receives_the_data_query_results(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """最終の検証に、この Run の業務データ（NL2SQL）の照会の結果を data_results で渡す（#1950）。

    資料とデータを組み合わせた回答の件数・金額の段落を、資料の根拠だけで外さないようにする。
    SQL を作らずに確認事項を返した照会（needs_clarification）は渡さない。
    """
    from app.features.agent.answer_validation import validation_data_results

    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    _script(
        monkeypatch,
        [
            function_call("rag__rag_search", {"query": "部門 売上 規程"}, call_id="call-1"),
            function_call(
                "nl2sql__nl2sql_query",
                {"question": GOAL, "profile_id": PROFILE},
                call_id="call-2",
            ),
            function_call(
                "nl2sql__nl2sql_query",
                {**_ANSWERED_QUERY, "question": "部門別の売上"},
                call_id="call-3",
            ),
        ],
        [assistant_message("契約条項のとおり、営業の売上は 1,200 です。")],
    )
    run = _run()
    assert run.status == RunStatus.COMPLETED
    [call] = mcp.calls_of("rag_validate_answer")
    [data] = call["arguments"]["data_results"]
    assert data["question"] == "部門別の売上"
    assert data["sql"].startswith("select department, sum(amount)")
    assert '"DEPARTMENT": "営業"' in data["result"]
    assert data["source"] == "データ問い合わせ（NL2SQL）"
    assert validation_data_results([]) == []


# ---- NL2SQL の確認事項の扱い（#2223） ----------------------------------------------------------

_UNRELATED_QUERY = {"question": "表の列の一覧を教えてください。", "profile_id": PROFILE}


def _nl2sql_unanswerable_for_other_questions(argument: Any) -> dict[str, Any]:
    """利用者の質問は確認事項を返し、別の問い（表の列の一覧など）は SQL を作れない。"""
    if argument.requirement_session_id is None and argument.question != GOAL:
        return {
            **nl2sql_needs_clarification(session_id="qs-other"),
            "status": "unanswerable",
            "error_code": "REQUIREMENT_UNANSWERABLE",
            "error_message": "この業務プロファイルでは SQL を作れません。",
        }
    return _nl2sql_query(argument)


def test_nl2sql_settles_only_the_same_session_or_a_generated_sql() -> None:
    pending = {"requirement_session_id": "qs-1"}

    def result(status: str, *, success: bool = True) -> ToolResult:
        return ToolResult(name="nl2sql__nl2sql_query", success=success, output={"status": status})

    retry = {"question": GOAL, "requirement_session_id": "qs-1"}
    assert nl2sql_settles(pending, retry, result("unanswerable"))
    assert nl2sql_settles(pending, retry, result("error", success=False))
    assert nl2sql_settles(pending, {"question": "別の問い"}, result("done"))
    assert nl2sql_settles(pending, {"question": "別の問い"}, result("running"))
    # 別の session で SQL を作らずに終わった呼び出しは、確かめ中の問いを片付けない。
    other = {"question": "別の問い", "requirement_session_id": "qs-other"}
    assert not nl2sql_settles(pending, other, result("unanswerable"))
    assert not nl2sql_settles(pending, {"question": "別の問い"}, result("error"))
    assert not nl2sql_settles(pending, {"question": "別の問い"}, result("done", success=False))


def test_unrelated_nl2sql_query_keeps_the_nl2sql_card(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """確認事項の後に無関係の nl2sql_query が SQL を作れなくても、確認はカードになる（#2223）。"""
    mcp.outputs["nl2sql_query"] = _nl2sql_unanswerable_for_other_questions
    query = {"question": GOAL, "profile_id": PROFILE}
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [function_call("nl2sql__nl2sql_query", _UNRELATED_QUERY, call_id="call-2")],
        [assistant_message("SALES のどの日付の列で期間を絞りますか？")],
    )
    run = _run()
    assert run.status == RunStatus.COMPLETED
    assert [call["arguments"]["question"] for call in mcp.calls_of("nl2sql_query")] == [
        GOAL,
        _UNRELATED_QUERY["question"],
    ]
    [confirmation] = run.confirmations
    assert (confirmation.source, confirmation.status) == ("nl2sql", "pending")
    assert confirmation.context["requirement_session_id"] == "qs-1"
    state = run_support_task(run)
    assert state is not None
    assert [item["requirement_session_id"] for item in state["pending_clarifications"]] == ["qs-1"]


def test_nl2sql_clarification_after_an_agent_answer_passes_the_answer(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """同じ Run の利用者の答えを NL2SQL の確認の次の手に添え、聞き直さずに答えさせる（#2223）。"""
    query = {"question": GOAL, "profile_id": PROFILE}
    answer = {"item_id": "item-period", "free_text": "全期間"}
    model = _script(
        monkeypatch,
        _ask("call-ask"),
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [
            function_call(
                "nl2sql__nl2sql_query",
                {**query, "requirement_session_id": "qs-1", "answer": answer},
                call_id="call-2",
            )
        ],
        [assistant_message("全期間の売上は営業が 1,200 です。")],
    )
    run = _run()
    runtime_repository.decide_confirmation(
        run.id,
        run.confirmations[0].id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", other_text="全期間で見てください。")],
    )
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    next_step = _tool_output(model, 2, "call-1")["next_step"]
    assert next_step["instruction"] == NL2SQL_ANSWER_FROM_USER_INSTRUCTION
    assert next_step["user_answers"] == [
        {"question": "どの期間の売上を見ますか？", "answer": "全期間で見てください。"}
    ]
    assert next_step["requirement_session_id"] == "qs-1"
    sent = mcp.calls_of("nl2sql_query")[-1]["arguments"]
    assert (sent["requirement_session_id"], sent["answer"]) == ("qs-1", answer)
    # 呼び直しで片付いたので、NL2SQL の確認のカードは出ない。
    assert [(item.source, item.status) for item in resumed.confirmations] == [("agent", "answered")]


def test_nl2sql_clarification_without_an_agent_answer_asks_the_user(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    query = {"question": GOAL, "profile_id": PROFILE}
    model = _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message("SALES のどの日付の列で期間を絞りますか？")],
    )
    _run()
    next_step = _tool_output(model, 1, "call-1")["next_step"]
    assert "user_answers" not in next_step
    assert next_step["instruction"] != NL2SQL_ANSWER_FROM_USER_INSTRUCTION


def test_asks_data_schema_detects_table_and_column_questions() -> None:
    assert asks_data_schema({"question": "年齢を示す列名を教えてください。"})
    assert asks_data_schema({"question": "どのテーブルを使いますか？"})
    assert asks_data_schema({"question": "Which column holds the date?"})
    assert not asks_data_schema({"question": "どの期間の売上を見ますか？"})
    assert not asks_data_schema({"question": 1})
    assert not asks_data_schema(None)


def test_schema_question_before_nl2sql_is_sent_back_to_nl2sql_first(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """nl2sql_query の前に表・列を利用者に聞く確認は中断せず、先に NL2SQL を呼ばせる（#2223）。"""
    query = {"question": GOAL, "profile_id": PROFILE}
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search",
                {"query": "売上 定義", "conditions": {"screen": "請求"}},
                call_id="call-rag",
            )
        ],
        _ask("call-col", question="売上金額の列名を教えてください。", options=[]),
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message("SALES のどの日付の列で期間を絞りますか？")],
    )
    run = _run()
    seen = _tool_output(model, 2, "call-col")
    assert seen["status"] == "query_data_first"
    assert "nl2sql_query" in seen["instruction"]
    # Agent 自身の列の確認は出ず、NL2SQL の確認事項がカードになる。
    [confirmation] = run.confirmations
    assert confirmation.source == "nl2sql"
    assert run.status == RunStatus.COMPLETED


def test_schema_question_after_nl2sql_is_asked(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """nl2sql_query を呼んだ後の表・列の確認は差し戻さない（NL2SQL の結果を見ても決まらない）。"""
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", _ANSWERED_QUERY, call_id="call-1")],
        _ask("call-col", question="どのテーブルの売上を見ますか？", options=[]),
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    [confirmation] = run.confirmations
    assert (confirmation.source, confirmation.call_id) == ("agent", "call-col")


def test_unanswerable_data_query_is_passed_to_the_final_validation() -> None:
    """SQL を作れないと返した照会（unanswerable）も理由つきで data_results に入れる（#2232）。

    「要る列が無いので集計できない」の結論を、資料の根拠だけで外さない。実行の失敗（error）と、
    まだ SQL を作っていない確認（needs_clarification）は入れない。
    """
    from app.features.agent.answer_validation import validation_data_results
    from app.features.agent.runtime import RunStep

    def step(question: str, output: dict[str, Any]) -> RunStep:
        return RunStep(
            run_id="run-x",
            status="completed",
            tool_call=ToolCall(
                name="nl2sql__nl2sql_query",
                arguments={"question": question, "profile_id": PROFILE},
            ),
            tool_result=ToolResult(name="nl2sql__nl2sql_query", success=True, output=output),
        )

    message = "期間を絞る日付の列が schema にありません。"
    steps = [
        step("期間ごとの件数", {"status": "unanswerable", "error_message": message}),
        step("失敗した照会", {"status": "error", "error_message": "接続できません。"}),
        step("確認の照会", nl2sql_needs_clarification()),
        step("理由の無い照会", {"status": "unanswerable", "error_message": ""}),
    ]
    [data] = validation_data_results(steps)
    assert data["question"] == "期間ごとの件数"
    assert data["sql"] == ""
    assert json.loads(data["result"]) == {"status": "unanswerable", "message": message}


# ---- ツールのエラーの後の確認（#2234） ----------------------------------------------------------


def test_confirmation_right_after_a_tool_error_is_sent_back(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """ツールがエラーで失敗した直後の確認は中断せず、失敗を伝えて終えさせる（#2234）。

    根拠・データのツールが 1 つも結果を返さずに終えた Run は、失敗にする（#2475）。
    """
    mcp.outputs["nl2sql_query"] = RuntimeError("呼び先の LLM が時間内に応答しませんでした。")
    query = {"question": GOAL, "profile_id": PROFILE}
    model = _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        _ask("call-ask"),
        [assistant_message("業務データの照会がエラーで失敗しました。時間をおいてお試しください。")],
    )
    run = _run()
    seen = _tool_output(model, 2, "call-ask")
    assert seen["status"] == "tool_failed"
    assert seen["instruction"] == TOOL_FAILED_INSTRUCTION
    assert run.status == RunStatus.FAILED
    assert run.events[-1].payload["error_code"] == "runtime.grounding_tool_failed"
    assert "nl2sql_query" in run.events[-1].message
    assert run.confirmations == []


def test_second_confirmation_after_a_tool_error_is_asked(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """差し戻しは Run で 1 回だけ（それでも聞くなら、質問の解釈の確認として出す）。"""
    mcp.outputs["nl2sql_query"] = RuntimeError("呼び先の LLM が時間内に応答しませんでした。")
    query = {"question": GOAL, "profile_id": PROFILE}
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        _ask("call-ask"),
        _ask("call-ask-2"),
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    [confirmation] = run.confirmations
    assert confirmation.call_id == "call-ask-2"


# ---- 確認の問いの JSON だけの本文（#2236） ----------------------------------------------------


def test_confirmation_payload_answers_are_detected() -> None:
    payload = {"question": "どの期間ですか？", "reason": "期間が曖昧です。", "options": []}
    text = json.dumps(payload, ensure_ascii=False)
    assert is_confirmation_payload(text)
    assert is_confirmation_payload(f"```json\n{text}\n```")
    assert not is_confirmation_payload("どの期間で集計しますか？")
    assert not is_confirmation_payload('{"answer": "1,200"}')
    assert not is_confirmation_payload("{壊れた JSON")


def test_confirmation_payload_answer_becomes_the_card_prompt(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """NL2SQL のカードを出す Run で、本文が確認の問いの JSON だけなら、カードの問いの文にする。"""
    query = {"question": GOAL, "profile_id": PROFILE}
    payload = {"question": "どの日付の列で期間を絞りますか？", "options": [], "multiple": False}
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message(json.dumps(payload, ensure_ascii=False))],
    )
    run = _run()
    [confirmation] = run.confirmations
    assert confirmation.source == "nl2sql"
    answer = next(item for item in run.artifacts if item.kind == "answer").content["text"]
    assert answer == card_answer([confirmation])
    assert answer.startswith("回答の前に確認が必要です。")
    assert "SALES のどの日付の列で期間を絞りますか？" in answer


def test_plain_answer_with_a_card_is_kept(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    query = {"question": GOAL, "profile_id": PROFILE}
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", query, call_id="call-1")],
        [assistant_message("どの日付の列で期間を絞りますか？")],
    )
    run = _run()
    answer = next(item for item in run.artifacts if item.kind == "answer").content["text"]
    assert answer == "どの日付の列で期間を絞りますか？"


def test_rag_evidence_tools_get_the_run_goal_as_original_question(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """Control Plane が RAG の根拠を集めるツールに利用者の元の質問を付ける（#2261）。

    モデルが検索文を短く言い換えても、RAG が元の質問の要求の単位を失わない。
    モデルが付けた値はそのまま送る。
    """
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "取り消し 手順"}, call_id="call-1")],
        [assistant_message("画面ごとに示します。")],
    )
    _run("受注を取り消す手順は？")
    assert (
        mcp.calls_of("rag_search")[-1]["arguments"]["original_question"] == "受注を取り消す手順は？"
    )

    _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search",
                {"query": "取り消し", "original_question": "モデルが付けた質問"},
                call_id="call-2",
            )
        ],
        [assistant_message("画面ごとに示します。")],
    )
    _run("受注を取り消す手順は？")
    assert mcp.calls_of("rag_search")[-1]["arguments"]["original_question"] == "モデルが付けた質問"


# ---- 確認のツールの呼び出しは確認待ちにする（#3352） --------------------------------------------

_RAG_WITH_CONDITIONS = {"query": "資料 概要", "conditions": {"screen": "請求"}}


def test_confirmation_after_the_grounding_retry_waits_for_the_user(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """ツールを呼ばずに答えて差し戻された後の確認も、Run を確認待ちにして問いを出す（#3352）。

    ツールを呼ぶ前の確認を中断せずにモデルへ返すと、モデルは確認を出したと取り違え、ツールを
    呼んだ後に「確認の答えを待っている」だけの本文で完了した。
    """
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    model = _script(
        monkeypatch,
        [assistant_message("対象の資料を教えてください。")],
        _ask("call-ask", question="どの資料の概要ですか？"),
        [function_call("rag__rag_search", _RAG_WITH_CONDITIONS, call_id="call-rag")],
        [assistant_message("（上記の確認質問に対するご回答をお待ちしております。）")],
    )
    run = _run("この資料の概要を教えてください")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    [confirmation] = run.confirmations
    assert (confirmation.source, confirmation.call_id, confirmation.status) == (
        "agent",
        "call-ask",
        "pending",
    )
    assert confirmation.items[0].prompt == "どの資料の概要ですか？"
    # ツールの結果から作っていない選択肢は出さない（自由入力で聞く。#1917）。
    assert confirmation.items[0].options == []
    # 確認を待つ間は、次のモデルの呼び出し・ツールの呼び出しをしない。
    assert len(model.calls) == 2
    assert mcp.calls_of("rag_search") == []
    assert not any(item.kind == "answer" for item in run.artifacts)


def _mcp_call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    structured: dict[str, Any] = response.json()["result"]["structuredContent"]
    return structured


def test_mcp_chat_returns_the_confirmation_after_the_grounding_retry(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """MCP の agent_chat も、差し戻しの後の確認を confirmation で返し、答えで続ける（#3352）。"""
    from app.features.agent import mcp_server

    monkeypatch.setattr(mcp_server, "wait_poll_seconds", 0.01)
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    _script(
        monkeypatch,
        [assistant_message("対象の資料を教えてください。")],
        _ask("call-ask", question="どの資料の概要ですか？", options=[]),
        [function_call("rag__rag_search", _RAG_WITH_CONDITIONS, call_id="call-rag")],
        [assistant_message("請求の資料の概要です。")],
    )
    client = TestClient(app)
    asked = _mcp_call(
        client, "agent_chat", {"agent_id": AGENT_ID, "question": "この資料の概要を教えてください"}
    )
    assert asked["status"] == "waiting_confirmation"
    assert asked["answer"] in {None, ""}
    confirmation = asked["confirmation"]
    assert confirmation["source"] == "agent"
    [item] = confirmation["items"]
    assert (item["prompt"], item["options"]) == ("どの資料の概要ですか？", [])

    answered = _mcp_call(
        client,
        "agent_answer_confirmation",
        {
            "run_id": asked["run_id"],
            "confirmation_id": confirmation["id"],
            "answers": [{"item_id": item["id"], "other_text": "請求の資料"}],
        },
    )
    assert (answered["status"], answered["answer"]) == ("completed", "請求の資料の概要です。")
    assert answered["confirmation"] is None
    assert len(mcp.calls_of("rag_search")) == 1


def test_confirmation_alone_waits_and_resumes(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """確認だけの応答: 確認待ちにし、答えで同じ Run を再開して続きを答える（#3352）。"""
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", _RAG_WITH_CONDITIONS, call_id="call-rag")],
        _ask("call-ask", question="どの資料の概要ですか？", options=[]),
        [assistant_message("請求の資料の概要です。")],
    )
    run = _run("この資料の概要を教えてください")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    [confirmation] = run.confirmations
    assert confirmation.call_id == "call-ask"
    assert len(model.calls) == 2

    runtime_repository.decide_confirmation(
        run.id,
        confirmation.id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", other_text="請求の資料")],
    )
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    seen = _tool_output(model, 2, "call-ask")
    assert (seen["status"], seen["other_text"]) == ("answered", "請求の資料")


@pytest.mark.parametrize("consulted", [False, True], ids=["before-tools", "after-tools"])
def test_confirmation_holds_the_other_tool_calls_of_the_same_turn(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, consulted: bool
) -> None:
    """確認と同じ応答のほかのツールは、確認の答えを待つ間は実行しない（#3352）。

    答えて再開すると、モデルは実行しなかったことを結果で知り、答えを見て呼び直せる。承認の要る
    ツールも承認を求めない（確認の答えの後に呼び直したときに求める）。
    """
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    monkeypatch.setattr(
        builtin_runtime,
        "_active_policy",
        lambda: ToolPolicy(allow={"rag__rag_search"}, ask={"nl2sql__nl2sql_query"}),
    )
    query = {"question": GOAL, "profile_id": PROFILE, "requirement_session_id": "qs-1"}
    first = (
        [[function_call("rag__rag_search", _RAG_WITH_CONDITIONS, call_id="call-rag-0")]]
        if consulted
        else []
    )
    model = _script(
        monkeypatch,
        *first,
        [
            *_ask("call-ask", options=[]),
            function_call("rag__rag_search", _RAG_WITH_CONDITIONS, call_id="call-rag"),
            function_call("nl2sql__nl2sql_query", query, call_id="call-sql"),
        ],
        [function_call("rag__rag_search", _RAG_WITH_CONDITIONS, call_id="call-rag-2")],
        [assistant_message("今年度の売上の資料の説明です。")],
    )
    run = _run()
    assert run.status == RunStatus.WAITING_CONFIRMATION
    assert [item.source for item in run.confirmations] == ["agent"]
    assert run.approvals == []
    assert len(mcp.calls_of("rag_search")) == (1 if consulted else 0)
    assert mcp.calls_of("nl2sql_query") == []
    # 実行しなかった呼び出しは step を作らない（処理の経過には実行しなかったツールの行を残す）。
    assert not any(
        step.tool_call is not None and step.tool_call.trace_id in {"call-rag", "call-sql"}
        for step in run.steps
    )

    [confirmation] = run.confirmations
    runtime_repository.decide_confirmation(
        run.id,
        confirmation.id,
        action="answer",
        answers=[ConfirmationAnswer(item_id="item_1", other_text="今年度")],
    )
    resumed = _resume(run.id)
    assert resumed.status == RunStatus.COMPLETED
    turn = 2 if consulted else 1
    for call_id in ("call-rag", "call-sql"):
        held = _tool_output(model, turn, call_id)
        assert held["error_code"] == builtin_runtime.CONFIRMATION_PENDING_CODE
    assert _tool_output(model, turn, "call-ask")["status"] == "answered"
    # 答えの後にモデルが呼び直したツールは実行する。
    assert len(mcp.calls_of("rag_search")) == (2 if consulted else 1)
    assert mcp.calls_of("nl2sql_query") == []
