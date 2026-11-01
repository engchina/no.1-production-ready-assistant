"""ツールで調べずに範囲外・答えられないと判断した拒答（#2608）の決定論テスト。

モデルは SDK の `ScriptedModel`、RAG は契約どおりの fake の MCP（`mcp_support`）にする。範囲内で
索引済みの根拠がある質問と範囲外の質問、調べる前の拒答と調べた結果が 0 件だった拒答を合成の
ケースで並べ、次を確かめる。

- 調べずに範囲外・答えられないと判断した回答は、範囲の内外をツールの結果で判断させる文で差し戻す
  （差し戻しの文は、範囲外かを調べずに決めてよいとは書かない）。
- 差し戻しでツールを呼べば、根拠があれば回答し、0 件なら調べた後の拒答（basis は
  `rag_search` など）になる。
- 差し戻しの後も調べなければ、拒答の basis を `declined_without_tools` にし、ツールで確かめずに
  答えた回答の拒答（`no_tool_grounding`）と分ける。Run の経過にも `declined` を残す。
- 品質評価は、調べる前の拒答と調べた後の拒答を分けて数える。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime, evaluation, tool_grounding
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.control_plane_store import FileItemStore, set_control_plane_store
from app.features.agent.evaluation import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationJob,
    evaluation_store,
    run_evaluation_job,
    summarize,
)
from app.features.agent.runtime import (
    EVALUATION_DRY_RUN_KEY,
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    run_answer_text,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ToolPolicy
from app.settings import get_settings

SKILL_ID = "test2608-skill"
AGENT_ID = "test2608-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
SEARCH = "rag__rag_search"
# 合成のケース（業務の内容を含まない）。
IN_SCOPE = "契約の期間は？"
OUT_OF_SCOPE = "明日の天気は？"
DECLINED = "この質問は業務の範囲外のため、お答えできません。"
ABSENCE = "資料には該当する記載が見つかりませんでした。"
_GUARD_BASES = {
    tool_grounding.BASIS_NO_TOOL_GROUNDING,
    tool_grounding.BASIS_DECLINED_WITHOUT_TOOLS,
}
_NO_EVIDENCE = {
    **DEFAULT_OUTPUTS["rag_search"],
    "answer": "",
    "outcome": "insufficient_evidence",
    "requests": [],
    "insufficient_reason": "資料に根拠がありません。",
    "evidence": [],
}


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[FakeProductMcp]:
    """資料の検索（RAG）だけを持つ業務 Agent。"""
    fake = fake_product_mcp(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    # ツール権限は保存先の状態（ほかのテストの変更）に依らず、すべて許可にする（並列・直列のどちら
    # でも同じ結果にする）。
    monkeypatch.setattr(builtin_runtime, "_active_policy", lambda: ToolPolicy())
    # 台本のモデルは呼ばれた順に応答するため、評価のケースを 1 件ずつ実行する。
    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda: 1)
    set_control_plane_store(FileItemStore(tmp_path / "items.json"))
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="資料の調べ物",
            instructions="資料を検索して答える。",
            mcp_requirements=[SkillMcpRequirement(server_id="rag", tool_names=["rag_search"])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name="資料の業務 Agent",
            description="契約の資料の質問に答えます。",
            skill_ids=[SKILL_ID],
        )
    )
    evaluation_store.clear()
    try:
        yield fake
    finally:
        evaluation_store.clear()
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
        set_control_plane_store(None)


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


def _run(goal: str, *, metadata: dict[str, Any] | None = None) -> RunState:
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID, metadata=metadata or {}),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, created.id)
    return runtime_repository.get_run(created.id)


def _outcome(run: RunState) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == "answer"]
    outcome = artifact.content["outcome"]
    assert isinstance(outcome, dict)
    return outcome


def _grounding_events(run: RunState) -> list[dict[str, Any]]:
    return [
        event.payload["tool_grounding"] for event in run.events if "tool_grounding" in event.payload
    ]


def _retry_text(model: ScriptedModel) -> str:
    call_input = model.calls[1].input
    assert isinstance(call_input, list)
    return str(call_input[-1]["content"])


def _tool_names(run: RunState) -> list[str]:
    return [step.tool_call.name for step in run.steps if step.tool_call is not None]


def test_in_scope_question_declined_before_search_is_sent_back_to_search(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """範囲内で索引済みの根拠がある質問を、調べずに範囲外と拒答した（#2608 の事象）。

    範囲の内外をツールの結果で判断させる文で差し戻し、検索して根拠で答えた回答を出す。
    """
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(DECLINED)]),
        ModelStep(output=[function_call(SEARCH, {"query": IN_SCOPE}, call_id="call-1")]),
        ModelStep(output=[assistant_message("契約の期間は 2 年です。")]),
    )

    run = _run(IN_SCOPE)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(mcp.calls_of("rag_search")) == 1
    assert run_answer_text(run) == "契約の期間は 2 年です。"
    retry = _retry_text(model)
    # チャットの Run は確認のツールを渡す（#1881）。頭は調べずに判断した拒答の文にする。
    assert retry.startswith(tool_grounding.retry_message(confirmable=True, declined=True))
    assert retry.startswith(tool_grounding.RETRY_HEAD_DECLINED)
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": DECLINED, "declined": True}
    ]
    assert run.metadata[tool_grounding.METADATA_KEY] == {"retries": 1}
    outcome = _outcome(run)
    assert outcome["basis"] not in _GUARD_BASES
    assert tool_grounding.refusal_stage(outcome["value"], _tool_names(run)) is None


def test_out_of_scope_question_searched_after_the_retry_is_refused_after_tools(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """範囲外の質問は、差し戻しで検索して 0 件なら、調べた後の拒答として出す。"""
    mcp.outputs["rag_search"] = _NO_EVIDENCE
    _script(
        monkeypatch,
        ModelStep(output=[assistant_message(DECLINED)]),
        ModelStep(output=[function_call(SEARCH, {"query": OUT_OF_SCOPE}, call_id="call-1")]),
        ModelStep(output=[assistant_message(ABSENCE)]),
    )

    run = _run(OUT_OF_SCOPE)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(mcp.calls_of("rag_search")) == 1
    # 一般的な知識で答えず、調べた結果の不足をそのまま出す。
    assert run_answer_text(run) == ABSENCE
    outcome = _outcome(run)
    # 回答は最後の検索に拠る（basis `rag_search`）。ツールで確かめずに答えた拒答の basis ではない。
    assert (outcome["value"], outcome["basis"]) == ("insufficient_evidence", "rag_search")
    assert tool_grounding.refusal_stage(outcome["value"], _tool_names(run)) == (
        tool_grounding.REFUSAL_AFTER_TOOLS
    )
    assert [event["action"] for event in _grounding_events(run)] == ["retry"]


@pytest.mark.parametrize(
    ("metadata", "confirmable", "assume"),
    [
        pytest.param({}, True, False, id="chat"),
        pytest.param({EVALUATION_DRY_RUN_KEY: True}, False, True, id="evaluation"),
    ],
)
def test_decline_without_search_after_the_retry_is_refused_as_declined(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,
    metadata: dict[str, Any],
    confirmable: bool,
    assume: bool,
) -> None:
    """差し戻しの後も調べずに範囲外と判断したら、`declined_without_tools` の拒答にする。"""
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(DECLINED)]),
        ModelStep(output=[assistant_message(DECLINED)]),
    )

    run = _run(OUT_OF_SCOPE, metadata=metadata)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert mcp.calls_of("rag_search") == []
    assert _retry_text(model).startswith(
        tool_grounding.retry_message(confirmable=confirmable, assume=assume, declined=True)
    )
    text = run_answer_text(run) or ""
    assert text.startswith(tool_grounding.REFUSAL_HEAD)
    assert _outcome(run) == {
        "schema_version": 1,
        "value": "insufficient_evidence",
        "basis": tool_grounding.BASIS_DECLINED_WITHOUT_TOOLS,
        "rag_outcome": None,
        "signals": [],
    }
    # 最終の検証の成果物は、ツールで確かめずに答えた回答の拒答と同じ（画面の説明は変えない）。
    [validation] = [item for item in run.artifacts if item.kind == "answer_validation"]
    assert (validation.content["status"], validation.content["reason"]) == (
        "skipped",
        tool_grounding.REASON_NO_TOOL_GROUNDING,
    )
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": DECLINED, "declined": True},
        {"action": "refused", "original_answer": DECLINED, "declined": True},
    ]
    assert run.metadata[tool_grounding.METADATA_KEY] == {
        "retries": 1,
        "refused": True,
        "declined": True,
    }
    refused = [event for event in run.events if event.payload.get("tool_grounding")][-1]
    assert "調べずに範囲外" in refused.message
    assert tool_grounding.refusal_stage("insufficient_evidence", _tool_names(run)) == (
        tool_grounding.REFUSAL_BEFORE_TOOLS
    )


@pytest.mark.parametrize(
    ("first", "second", "basis", "declined_events"),
    [
        # 調べずに範囲外と判断した後、差し戻しで一般的な知識の回答に変えた: 確かめずに答えた拒答。
        pytest.param(
            DECLINED,
            "明日は晴れです。",
            tool_grounding.BASIS_NO_TOOL_GROUNDING,
            [True, False],
            id="declined-then-answered",
        ),
        # 一般的な知識で答えた後、差し戻しで調べずに答えられないと判断した: 調べずに判断した拒答。
        pytest.param(
            "明日は晴れです。",
            DECLINED,
            tool_grounding.BASIS_DECLINED_WITHOUT_TOOLS,
            [False, True],
            id="answered-then-declined",
        ),
    ],
)
def test_the_last_answer_decides_the_basis_of_the_refusal(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,
    first: str,
    second: str,
    basis: str,
    declined_events: list[bool],
) -> None:
    """拒答の basis は、拒答に置き換えた（差し戻しの後の）回答で決める。根拠の無い回答は出さない。

    差し戻しの頭の文は、差し戻した回答（1 つ目）で決める。
    """
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(first)]),
        ModelStep(output=[assistant_message(second)]),
    )

    run = _run(OUT_OF_SCOPE)

    assert mcp.calls_of("rag_search") == []
    retry = _retry_text(model)
    assert retry.startswith(tool_grounding.RETRY_HEAD_DECLINED) is declined_events[0]
    assert "明日は晴れです。" not in (run_answer_text(run) or "")
    assert _outcome(run)["basis"] == basis
    assert [bool(event.get("declined")) for event in _grounding_events(run)] == declined_events


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (DECLINED, True),
        ("申し訳ありません。ご質問の内容は、この業務 Agent が扱う範囲外です。", True),
        ("## 回答\nご質問は本業務 Agent の対象外です。", True),
        (ABSENCE, True),
        ("確かめられないため、お答えできません。", True),
        # 実サービスの評価で判定から漏れた言い回し（#2608。対象は合成の語）。謙譲の否定
        # （「含まれておりません」）と、理由の節（「…ありませんので、」）の後の拒答。
        (
            "申し訳ございませんが、社内の規程・手順・記録などの資料には「来週の会議室の空き」に"
            "関する情報は含まれておりません。そのため、回答することができません。",
            True,
        ),
        (
            "申し訳ございませんが、社内資料や規程・手順書に基づいて確認できる情報がありません"
            "ので、来週の会議室の空きについてはお答えできません。",
            True,
        ),
        ("資料には該当する記載がございませんため、お答えいたしかねます。", True),
        # 理由の節の後が拒答ではない文・数量や操作を含む文は、拒答にしない。
        ("この規程には例外の定めがありませんので、全員が対象です。", False),
        ("資料に記載がありませんので、設定画面を開いて確認してください。", False),
        ("資料には 3 件の記載しかありませんので、それ以上はお答えできません。", False),
        # 事実の主張（「対象外」だけでは範囲外の拒答にしない）。
        ("交通費は課税の対象外です。", False),
        ("出張の業務は手当の対象外です。", False),
        ("契約の期間は 2 年です。", False),
        # 操作の指示を含む文と、確認の質問。
        ("その件は担当外のため、経理の窓口で申請してください。", False),
        ("どの契約のことですか？", False),
    ],
)
def test_declined_reads_the_conclusion_of_the_answer(answer: str, expected: bool) -> None:
    assert tool_grounding.declined(answer) is expected


@pytest.mark.parametrize(("confirmable", "assume"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("declined", [False, True])
def test_retry_message_never_lets_the_model_decide_the_scope_without_tools(
    confirmable: bool, assume: bool, declined: bool
) -> None:
    """差し戻しは調べていない回答にだけ行うので、範囲外なら調べずに答えられないと答えてよいとは
    書かない（#2608）。結びは調べても確かめられないときの拒答にする。"""
    message = tool_grounding.retry_message(
        confirmable=confirmable, assume=assume, declined=declined
    )
    assert "範囲外の質問のときは" not in message
    assert "ツールで調べても確かめられないときは" in message
    assert "推測や一般的な知識で答えず" in message
    assert message.startswith(tool_grounding.RETRY_HEAD_DECLINED) is declined
    if declined:
        assert "範囲の内外と、資料・業務データに根拠があるかは、ツールの結果で判断" in message


def test_refusal_stage_separates_refusals_before_and_after_tools() -> None:
    insufficient = "insufficient_evidence"
    assert tool_grounding.refusal_stage(insufficient, []) == tool_grounding.REFUSAL_BEFORE_TOOLS
    # 根拠・データのツールでない呼び出し（プロファイルの推薦・Control Plane のツール）は数えない。
    assert (
        tool_grounding.refusal_stage(
            insufficient, ["rag__rag_recommend_search_answer_profile", "skill_reference_read"]
        )
        == tool_grounding.REFUSAL_BEFORE_TOOLS
    )
    assert tool_grounding.refusal_stage(insufficient, [SEARCH]) == (
        tool_grounding.REFUSAL_AFTER_TOOLS
    )
    assert tool_grounding.refusal_stage("answered", []) is None
    assert tool_grounding.refusal_stage(None, [SEARCH]) is None
    case = EvaluationCase(id="c1", question=IN_SCOPE, expected="2 年")
    summary = summarize(
        [
            EvaluationCaseResult(case=case, refusal_stage="before_tools"),
            EvaluationCaseResult(case=case, refusal_stage="before_tools"),
            EvaluationCaseResult(case=case, refusal_stage="after_tools"),
            EvaluationCaseResult(case=case),
        ]
    )
    assert (summary.refused_before_tools, summary.refused_after_tools) == (2, 1)


def _judgement(verdict: str, score: float) -> Any:
    return [
        assistant_message(
            json.dumps(
                {"verdict": verdict, "score": score, "summary": "判定", "missing_points": []},
                ensure_ascii=False,
            )
        )
    ]


def test_evaluation_counts_refusals_before_and_after_tools(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """品質評価のケースの結果と集計で、調べる前の拒答と調べた後の拒答を分ける。"""
    mcp.outputs["rag_search"] = _NO_EVIDENCE
    _script(
        monkeypatch,
        # 1 件目: 差し戻しの後も調べずに範囲外と判断した。
        [assistant_message(DECLINED)],
        [assistant_message(DECLINED)],
        _judgement("incorrect", 0.0),
        # 2 件目: 差し戻しで調べ、根拠が 0 件だった。
        [assistant_message(DECLINED)],
        [function_call(SEARCH, {"query": OUT_OF_SCOPE}, call_id="call-1")],
        [assistant_message(ABSENCE)],
        _judgement("correct", 1.0),
    )
    job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            agent_name="資料の業務 Agent",
            set_id="evset_2608",
            set_name="拒答の評価",
            created_by_user_uuid=USER_UUID,
            results=[
                EvaluationCaseResult(
                    case=EvaluationCase(id="c1", question=IN_SCOPE, expected="2 年")
                ),
                EvaluationCaseResult(
                    case=EvaluationCase(id="c2", question=OUT_OF_SCOPE, expected="答えられない")
                ),
            ],
        )
    )

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    first, second = finished.results
    assert (first.answer_outcome, first.answer_basis, first.refusal_stage) == (
        "insufficient_evidence",
        tool_grounding.BASIS_DECLINED_WITHOUT_TOOLS,
        "before_tools",
    )
    assert (second.answer_outcome, second.answer_basis, second.refusal_stage) == (
        "insufficient_evidence",
        "rag_search",
        "after_tools",
    )
    summary = finished.summary
    assert (summary.refused_before_tools, summary.refused_after_tools) == (1, 1)


# 範囲の内外をモデルが調べる前に決めてよいと読める指示（#58。検索の前の拒答を指示が認めていた）。
_PRE_SEARCH_SCOPE_REFUSAL = (
    "業務の範囲外の質問のときは",
    "範囲外・確かめられない質問は確認せずに",
)


def _assert_search_first(instructions: str) -> None:
    for phrase in _PRE_SEARCH_SCOPE_REFUSAL:
        assert phrase not in instructions, phrase
    assert tool_grounding.SEARCH_FIRST_INSTRUCTION in instructions


def test_model_is_told_to_search_before_judging_scope(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """資料のツールを渡した Run の system の指示は、範囲の内外を調べる前に決めさせない（#58）。

    範囲外の質問は答えられないと答える、という指示がツールで調べる前の判断を認めていたため、
    範囲内で索引済みの根拠がある質問も、モデルが調べずに範囲外と拒答した（lesson06: 先に検索し、
    根拠が無いときだけ拒答する）。チャット（確認のツールあり）・品質評価（確認しない）の両方で
    同じ指示にする。
    """
    for metadata in ({}, {EVALUATION_DRY_RUN_KEY: True}):
        model = _script(
            monkeypatch,
            ModelStep(output=[function_call(SEARCH, {"query": IN_SCOPE}, call_id="call-1")]),
            ModelStep(output=[assistant_message("契約の期間は 2 年です。")]),
        )

        run = _run(IN_SCOPE, metadata=metadata)

        assert run.status == RunStatus.COMPLETED, run.events[-1].message
        instructions = model.calls[0].system_instructions or ""
        _assert_search_first(instructions)


def test_builtin_skills_do_not_allow_refusing_before_search() -> None:
    """組み込みのスキルの指示も、範囲外かを調べる前に決めてよいと書かない（#58）。"""
    from app.features.agent.skills import BUILTIN_SKILL_IDS, resolve_skill

    for skill_id in BUILTIN_SKILL_IDS:
        skill = resolve_skill(skill_id)
        assert skill is not None, skill_id
        for phrase in _PRE_SEARCH_SCOPE_REFUSAL:
            assert phrase not in skill.instructions, (skill_id, phrase)
        # 根拠が無いときの拒答（#1835）は残す。
        assert "確かめられないため答えられないと答える" in skill.instructions, skill_id


def test_agent_without_grounding_tools_keeps_the_refusal_instruction() -> None:
    """根拠・データのツールを持たない Agent は、調べる手段が無いので範囲外は拒答のまま（#1835）。"""
    instructions = builtin_runtime.compose_instructions("", [], ["echo"])
    assert tool_grounding.SEARCH_FIRST_INSTRUCTION not in instructions
    assert "確かめられないため答えられないと答えてください。" in instructions
    grounded = builtin_runtime.compose_instructions("", [], [SEARCH])
    _assert_search_first(grounded)
