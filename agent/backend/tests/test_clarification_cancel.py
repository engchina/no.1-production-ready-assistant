"""Agent のチャットの確認（NL2SQL の SQL の生成前の確認・RAG の確認）と取り消し（#1441・#1445）。

決定論のテスト。

モデルは SDK の `ScriptedModel`、RAG / NL2SQL は契約どおりの fake の MCP（`mcp_support`）にする。

- NL2SQL を使うと判断したときだけ確認が始まる（`nl2sql_query` が確認の項目を返したときだけ）。
- 確認の項目は 1 問ずつ利用者に聞き、回答を付けて `nl2sql_query` を呼び直す。
- 取り消し（操作「データベースの検索は不要」「資料の検索は不要」と、会話の文）では、その質問で
  そのツールを使わず、NL2SQL は確認の session を閉じ、Run に同じ形で記録する。
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
from mcp_support import (
    DEFAULT_OUTPUTS,
    FakeProductMcp,
    fake_product_mcp,
    nl2sql_needs_clarification,
)
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.clarification_cancel import (
    CANCEL_METADATA_KEY,
    CANCEL_TRACE_PREFIX,
    is_declined_tool,
    message_cancel_kinds,
)
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    reserved_run_metadata_keys,
    run_support_task,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import nl2sql_next_step
from app.features.agent.tools import ToolPolicy
from app.settings import get_settings

SKILL_ID = "test1441-skill"
AGENT_ID = "test1441-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
PROFILE = "profile-sales"
GOAL = "今月の売上を部門別に教えて"
ASK = (
    "SALES のどの日付の列で期間を絞りますか？"
    "（SALES.ORDER_DATE（受注日） / SALES.SHIP_DATE（出荷日））"
)
RAG_QUESTION = {
    "condition_id": "screen",
    "label": "画面",
    "question": "どの画面の操作ですか？",
    "options": ["受注入力", "請求"],
}
NL2SQL_PENDING = {"kind": "nl2sql", "requirement_session_id": "qs-1", "question": "どの列？"}
RAG_PENDING = {"kind": "rag", "condition_id": "screen", "question": "どの画面？"}


def _nl2sql_query(argument: Any) -> dict[str, Any]:
    """回答の無い呼び出しは確認の項目を返し、回答を付けた呼び出しで SQL を作る（NL2SQL と同じ）。"""
    if argument.requirement_session_id is None:
        return nl2sql_needs_clarification()
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["nl2sql_query"])
    return output


def _rag_search(argument: Any) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    if "conditions" not in argument.model_fields_set:
        output.update(
            answer="どの画面か確かめてください。",
            outcome="needs_clarification",
            clarifications=[RAG_QUESTION],
        )
    return output


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(
        monkeypatch, outputs={"nl2sql_query": _nl2sql_query, "rag_search": _rag_search}
    )
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    # nl2sql_query は承認が要る（readOnlyHint=false）。会話の流れの確認では承認なしで呼ぶ。
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


def _run(goal: str = GOAL, *, thread_id: str | None = None, cancel: str | None = None) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal=goal,
            agent_id=AGENT_ID,
            thread_id=thread_id,
            clarification_cancel=cancel,
        ),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _query(call_id: str, **extra: Any) -> list[Any]:
    arguments = {"question": GOAL, "profile_id": PROFILE, **extra}
    return [function_call("nl2sql__nl2sql_query", arguments, call_id=call_id)]


def _search(call_id: str, **extra: Any) -> list[Any]:
    arguments = {"query": "受注の取り消し 手順", **extra}
    return [function_call("rag__rag_search", arguments, call_id=call_id)]


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _tool_names(model: ScriptedModel, call_index: int = 0) -> set[str]:
    return {tool.name for tool in model.calls[call_index].tools if isinstance(tool, FunctionTool)}


def _outcome(run: RunState) -> dict[str, Any]:
    answer = next(item for item in run.artifacts if item.kind == "answer")
    assert isinstance(answer.content, dict)
    outcome: dict[str, Any] = answer.content["outcome"]
    return outcome


def _state(run: RunState) -> dict[str, Any]:
    state = run_support_task(run)
    assert state is not None
    return state


def _ask_nl2sql(monkeypatch: MonkeyPatch) -> RunState:
    _script(monkeypatch, _query("call-1"), [assistant_message(ASK)])
    run = _run()
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    return run


# ---- 決定的な判定 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "pending", "expected"),
    [
        ("データベースは関係ない質問でした", [NL2SQL_PENDING], ["nl2sql"]),
        ("ＤＢの検索は不要です", [NL2SQL_PENDING], ["nl2sql"]),
        ("これはデータベースの質問ではありません", [NL2SQL_PENDING], ["nl2sql"]),
        ("資料は関係ないです", [RAG_PENDING], ["rag"]),
        ("マニュアルを調べなくていいです", [RAG_PENDING], ["rag"]),
        ("確認はやめてください", [NL2SQL_PENDING, RAG_PENDING], ["nl2sql", "rag"]),
        # 問いへの答え・確かめ中ではない種類・確かめ中の問いが無いときは取り消しではない。
        ("受注日でお願いします", [NL2SQL_PENDING], []),
        ("資料は関係ない", [NL2SQL_PENDING], []),
        ("データベースは関係ない", [], []),
    ],
    ids=[
        "db-unrelated",
        "db-fullwidth",
        "db-not-question",
        "rag-unrelated",
        "rag-no-need",
        "any-stop",
        "answer",
        "other-kind",
        "no-pending",
    ],
)
def test_cancel_by_message_is_detected_only_for_pending_kinds(
    goal: str, pending: list[dict[str, Any]], expected: list[str]
) -> None:
    state = {"pending_clarifications": pending} if pending else None
    assert message_cancel_kinds(goal, state) == expected


def test_declined_tools_and_nl2sql_next_step() -> None:
    assert is_declined_tool("nl2sql__nl2sql_query", {"nl2sql"})
    assert not is_declined_tool("rag__rag_search", {"nl2sql"})
    assert is_declined_tool("crm__rag_lookup_guides", {"rag"})
    assert not is_declined_tool("skill_reference_read", {"rag", "nl2sql"})

    step = nl2sql_next_step(
        nl2sql_needs_clarification(), {"question": GOAL}, tool_name="nl2sql__nl2sql_query"
    )
    assert step is not None
    # RAG の確認と同じ次の手（1 問だけ。表・列は物理名の候補のまま）。
    assert (step["action"], step["kind"], step["requirement_session_id"]) == (
        "ask_clarification",
        "nl2sql",
        "qs-1",
    )
    assert [item["question"] for item in step["questions"]] == [
        "SALES のどの日付の列で期間を絞りますか？"
    ]
    assert step["questions"][0]["options"] == [
        "SALES.ORDER_DATE（受注日）",
        "SALES.SHIP_DATE（出荷日）",
    ]
    assert nl2sql_next_step(DEFAULT_OUTPUTS["nl2sql_query"], {}, tool_name="nl2sql__x") is None


def test_clarification_cancel_metadata_is_reserved() -> None:
    assert reserved_run_metadata_keys({CANCEL_METADATA_KEY: []}) == [CANCEL_METADATA_KEY]


# ---- NL2SQL の SQL の生成前の確認（#1441） -----------------------------------------------


def test_nl2sql_clarification_is_asked_one_at_a_time_and_the_answer_continues_the_session(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(monkeypatch, _query("call-1"), [assistant_message(ASK)])
    first = _run()
    assert first.status == RunStatus.COMPLETED
    # モデルは SQL を作る前の確認の項目と、1 問だけ聞く次の手を見た。
    seen = _tool_output(model, 1, "call-1")
    assert seen["status"] == "needs_clarification"
    assert seen["next_step"]["action"] == "ask_clarification"
    assert seen["next_step"]["kind"] == "nl2sql"
    assert len(seen["next_step"]["questions"]) == 1
    outcome = _outcome(first)
    assert (outcome["value"], outcome["basis"]) == ("needs_clarification", "nl2sql_clarification")
    state = _state(first)
    [pending] = state["pending_clarifications"]
    assert pending["kind"] == "nl2sql"
    assert pending["requirement_session_id"] == "qs-1"
    assert pending["condition_id"] == "item-period"
    assert pending["candidates"] == [
        {"id": "c-order", "label": "SALES.ORDER_DATE（受注日）"},
        {"id": "c-ship", "label": "SALES.SHIP_DATE（出荷日）"},
    ]
    assert (pending["profile_id"], pending["nl2sql_question"]) == (PROFILE, GOAL)

    # 次の発言は問いへの答え。指示に確かめ中の問いと呼び直し方が入り、回答を付けて呼び直す。
    answer = {"item_id": "item-period", "selected_candidate_ids": ["c-order"]}
    model = _script(
        monkeypatch,
        _query("call-2", requirement_session_id="qs-1", answer=answer),
        [assistant_message("今月の部門別の売上は、営業が 1,200 です。")],
    )
    second = _run("受注日でお願いします", thread_id=first.thread_id)
    assert second.status == RunStatus.COMPLETED
    instructions = str(model.calls[0].system_instructions)
    assert "データベース（NL2SQL）の確かめ中の問い" in instructions
    assert "requirement_session_id=qs-1" in instructions
    assert "「SALES.ORDER_DATE（受注日）」（id=c-order）" in instructions
    sent = mcp.calls_of("nl2sql_query")[-1]["arguments"]
    assert (sent["requirement_session_id"], sent["answer"]) == ("qs-1", answer)
    assert "next_step" not in _tool_output(model, 1, "call-2")
    # SQL を作ったので、確かめ中の問いは無い。取り消しも無い。
    assert _state(second)["pending_clarifications"] == []
    assert CANCEL_METADATA_KEY not in second.metadata
    assert mcp.calls_of("nl2sql_cancel_requirement") == []


def test_clarification_does_not_start_when_the_agent_does_not_use_nl2sql(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(
        monkeypatch,
        _search("call-1", conditions={"screen": "受注入力"}),
        [assistant_message("受注入力の画面で取り消します。")],
    )
    run = _run("受注を取り消す手順を教えて")
    assert mcp.calls_of("nl2sql_query") == []
    assert _state(run)["pending_clarifications"] == []
    assert _outcome(run)["basis"] != "nl2sql_clarification"
    # 確かめ中の問いが無ければ「データベースは関係ない」も取り消しではない。
    _script(monkeypatch, [assistant_message("はい、資料で答えました。")])
    follow = _run("データベースは関係ないです", thread_id=run.thread_id)
    assert CANCEL_METADATA_KEY not in follow.metadata
    assert mcp.calls_of("nl2sql_cancel_requirement") == []


@pytest.mark.parametrize(
    ("goal", "cancel", "trigger"),
    [
        ("データベースの検索は不要", "nl2sql", "button"),
        ("データベースは関係ない質問でした", None, "message"),
    ],
    ids=["button", "message"],
)
def test_cancelling_the_nl2sql_clarification_closes_the_session_and_continues_without_nl2sql(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,
    goal: str,
    cancel: str | None,
    trigger: str,
) -> None:
    first = _ask_nl2sql(monkeypatch)
    model = _script(
        monkeypatch, [assistant_message("データベースを使わずに答えます。何を知りたいですか？")]
    )
    run = _run(goal, thread_id=first.thread_id, cancel=cancel)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message

    # Control Plane が確認の session を閉じた（Run の利用者のトークン。予算には数えない）。
    [closed] = mcp.calls_of("nl2sql_cancel_requirement")
    assert closed["arguments"] == {"requirement_session_id": "qs-1"}
    assert closed["claims"]["sub"] == USER_UUID
    step = next(item for item in run.steps if item.tool_call is not None)
    assert step.tool_call is not None
    assert step.tool_call.name == "nl2sql__nl2sql_cancel_requirement"
    assert (step.tool_call.trace_id or "").startswith(CANCEL_TRACE_PREFIX)
    # この質問では NL2SQL のツールを使わない。ほかの手段で続けるか、意図を聞き直す指示を足す。
    tools = _tool_names(model)
    assert "rag__rag_search" in tools
    assert not any(name.startswith("nl2sql__") for name in tools)
    instructions = str(model.calls[0].system_instructions)
    assert "「データベースの検索は不要」" in instructions
    assert "意図" in instructions or "知りたいこと" in instructions

    # 取り消しは Run の記録（metadata・イベント）と支援タスクの状態に同じ形で残る。
    [record] = run.metadata[CANCEL_METADATA_KEY]
    assert (record["kind"], record["trigger"], record["run_id"]) == ("nl2sql", trigger, run.id)
    assert record["questions"] == ["SALES のどの日付の列で期間を絞りますか？"]
    assert record["requirement_sessions"] == [
        {"requirement_session_id": "qs-1", "status": "cancelled"}
    ]
    event = next(item for item in run.events if "clarification_cancel" in item.payload)
    assert event.payload["clarification_cancel"] == [record]
    state = _state(run)
    assert state["pending_clarifications"] == []
    assert state["clarification_cancellations"] == [record]
    assert state["budget"]["run"]["tool_calls"] == 0
    # session を閉じた呼び出しは、現場のデータを確かめた呼び出しに数えない（回答の対応・経路）。
    assert _outcome(run)["basis"] != "environment_tools"
    assert state["route"]["path"] == "none"


# ---- RAG の確認（#1445） ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "cancel", "trigger"),
    [("資料の検索は不要", "rag", "button"), ("資料は関係ないです", None, "message")],
    ids=["button", "message"],
)
def test_cancelling_the_rag_clarification_continues_without_rag_with_the_same_record(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,
    goal: str,
    cancel: str | None,
    trigger: str,
) -> None:
    _script(
        monkeypatch,
        _search("call-1"),
        [assistant_message("どの画面の操作ですか？（受注入力／請求）")],
    )
    first = _run("取り消しの手順を教えて")
    [pending] = _state(first)["pending_clarifications"]
    assert (pending["kind"], pending["condition_id"]) == ("rag", "screen")

    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    model = _script(monkeypatch, [assistant_message("一般的には、取り消しは管理者に依頼します。")])
    run = _run(goal, thread_id=first.thread_id, cancel=cancel)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    tools = _tool_names(model)
    assert "nl2sql__nl2sql_query" in tools
    assert not any(name.startswith("rag__") for name in tools)
    assert "「資料の検索は不要」" in str(model.calls[0].system_instructions)
    assert mcp.calls_of("nl2sql_cancel_requirement") == []

    # NL2SQL の取り消しと同じ形の記録（RAG は session が無い）。
    [record] = run.metadata[CANCEL_METADATA_KEY]
    assert set(record) == {"kind", "trigger", "run_id", "at", "questions", "requirement_sessions"}
    assert (record["kind"], record["trigger"]) == ("rag", trigger)
    assert record["questions"] == ["どの画面の操作ですか？"]
    assert record["requirement_sessions"] == []
    state = _state(run)
    assert state["pending_clarifications"] == []
    assert state["clarification_cancellations"] == [record]
    # 資料を使わずに答えた回答に「資料で確かめていない」を足さない（照合の対象外）。
    validation = next(item for item in run.artifacts if item.kind == "answer_validation")
    assert (validation.content["status"], validation.content["reason"]) == (
        "skipped",
        "rag_declined",
    )
    answer = next(item for item in run.artifacts if item.kind == "answer")
    assert answer.content["text"] == "一般的には、取り消しは管理者に依頼します。"


def test_button_without_a_pending_clarification_only_declines_the_tools(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(monkeypatch, [assistant_message("何を知りたいですか？")])
    run = _run("データベースの検索は不要", cancel="nl2sql")
    assert not any(name.startswith("nl2sql__") for name in _tool_names(model))
    [record] = run.metadata[CANCEL_METADATA_KEY]
    assert (record["kind"], record["questions"], record["requirement_sessions"]) == (
        "nl2sql",
        [],
        [],
    )
    assert mcp.calls_of("nl2sql_cancel_requirement") == []
