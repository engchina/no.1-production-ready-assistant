"""Agent が編成する根拠の取得（#2651）の決定論テスト。

- 組み込み Runtime は、スキルが Agent 主体の取得を宣言した接続（`evidence_planning=caller`）の
  `rag_retrieve_evidence` にだけ、検索文を作る主体（planning=caller）と追跡 ID を決定的に付け、
  モデルに見せない（モデルが渡した値は上書きする）。宣言の無い古いスキルを固定した公開版・固定の
  中身の無い公開版は RAG が計画し、caller でだけ効く引数（planning・queries など）をモデルに見せず、
  モデルが渡しても呼び先へ送らない（#2676）。
- 合成のケース（本文・表・図）で、取得 → 図の確認（`rag_read_source`）→ 回答 → 最終の検証が
  RAG に回答を作らせずに（`rag_search` を呼ばずに）通る。
- 根拠の取得が検索・回答プロファイルのルールの確認を返したら、確認の次の手と確かめ中の問いにする。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents import FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    AgentProfilePatch,
    AgentVersion,
    RunCreateRequest,
    RunStatus,
    run_support_task,
    runtime_repository,
)
from app.features.agent.skills import (
    AgentSkillDefinition,
    EvidencePlanning,
    SkillMcpRequirement,
    skill_registry,
)
from app.features.agent.skills_loader import load_skills_from_dir
from app.features.agent.support_task import (
    caller_planned_schema,
    evidence_clarification_step,
    rag_planned_arguments,
    rag_planned_schema,
)
from app.settings import get_settings

SKILL_ID = "test2651-skill"
AGENT_ID = "test2651-agent"
USER_UUID = "26512651-2222-3333-4444-555555555555"
GOAL = "機器 A の点検の周期と、点検の画面の確定ボタンの位置は？"
ANSWER = "機器 A の点検は毎月 1 回です。確定ボタンは画面の右下にあります。"

_TEXT = {
    **DEFAULT_OUTPUTS["rag_retrieve_evidence"]["evidence"][0],
    "evidence_id": "chunk-text",
    "chunk_id": "chunk-text",
    "file_name": "点検の手順.pdf",
    "excerpt": "機器 A の点検は毎月 1 回行う。",
    "text_length": 15,
    "retrieval_rank": 1,
}
_TABLE = {
    **_TEXT,
    "evidence_id": "chunk-table",
    "chunk_id": "chunk-table",
    "content_kind": "table",
    "evidence_type": "table",
    "excerpt": "| 機器 | 周期 |\n| A | 毎月 |",
    "retrieval_rank": 2,
}
_FIGURE = {
    **_TEXT,
    "evidence_id": "chunk-figure",
    "chunk_id": "chunk-figure",
    "content_kind": "figure",
    "evidence_type": "figure_description",
    "excerpt": "点検の画面の図。確定のボタンがある。",
    "retrieval_rank": 3,
    "image_ref": {
        "document_id": "doc-1",
        "chunk_id": "chunk-figure",
        "chunk_set_id": "cs-1",
        "page": 2,
        "page_label": None,
        "bbox": [10.0, 20.0, 110.0, 220.0],
        "bbox_unit": "absolute",
    },
}


def _skill(planning: EvidencePlanning) -> AgentSkillDefinition:
    return AgentSkillDefinition(
        id=SKILL_ID,
        name="根拠の取得の Skill",
        instructions=f"rag_retrieve_evidence で根拠を集め、根拠から答える（{planning}）。",
        mcp_requirements=[
            SkillMcpRequirement(
                server_id="rag",
                tool_names=["rag_retrieve_evidence", "rag_read_source"],
                evidence_planning=planning,
            )
        ],
    )


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    skill_registry.upsert_custom(_skill("caller"))
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID, name="根拠の取得の業務 Agent", skill_ids=[SKILL_ID], versioned=True
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


def _script(monkeypatch: MonkeyPatch, *steps: Any, validation: bool) -> ScriptedModel:
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
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", validation)
    return model


def _run(goal: str = GOAL, *, draft: bool = True) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID, draft=draft),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return run.id


def _tools(model: ScriptedModel) -> dict[str, FunctionTool]:
    return {tool.name: tool for tool in model.calls[0].tools if isinstance(tool, FunctionTool)}


def test_runtime_fixes_caller_planning_and_hides_it_from_the_model(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence",
                # モデルが planning を渡しても、実行環境が caller に上書きする。
                {"query": "機器 A 点検 周期", "queries": ["機器A 定期点検"], "planning": "rag"},
                call_id="call-1",
            )
        ],
        [assistant_message(ANSWER)],
        validation=False,
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [call] = mcp.calls_of("rag_retrieve_evidence")
    arguments = call["arguments"]
    assert arguments["planning"] == "caller"
    assert arguments["request_id"] == f"{run_id}:call-1"
    assert arguments["queries"] == ["機器A 定期点検"]
    # 利用者の元の質問は今までどおり決定的に付く（#2261）。
    assert arguments["original_question"] == GOAL
    params = _tools(model)["rag__rag_retrieve_evidence"].params_json_schema
    assert "planning" not in params["properties"]
    assert "request_id" not in params["properties"]
    assert "queries" in params["properties"]


def test_published_version_pinning_an_undeclared_skill_keeps_rag_planning(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """宣言の無いスキルを固定した公開版は今までどおり（RAG が計画）。下書きは caller。"""
    skill_registry.upsert_custom(_skill("rag"))
    runtime_repository.publish_agent(AGENT_ID, published_by="admin")
    # スキルを Agent 主体の取得に更新する（公開版は再公開するまで固定した中身で動く）。
    skill_registry.upsert_custom(_skill("caller"))
    call = function_call("rag__rag_retrieve_evidence", {"query": "機器 A 点検"}, call_id="c1")
    model = _script(
        monkeypatch,
        [call],
        [assistant_message(ANSWER)],
        [call],
        [assistant_message(ANSWER)],
        validation=False,
    )

    _run(draft=False)
    _run(draft=True)

    published, draft = mcp.calls_of("rag_retrieve_evidence")
    assert "planning" not in published["arguments"]
    assert "request_id" not in published["arguments"]
    assert draft["arguments"]["planning"] == "caller"
    published_params = _tools(model)["rag__rag_retrieve_evidence"].params_json_schema
    # 公開版のモデルには、検索文を作る主体と caller でだけ効く引数を見せない（#2676）。
    assert not _CALLER_ONLY & set(published_params["properties"])
    assert "query" in published_params["properties"]

    # 再公開すると、公開版も Agent 主体の取得になる。
    runtime_repository.publish_agent(AGENT_ID, published_by="admin")
    _script(monkeypatch, [call], [assistant_message(ANSWER)], validation=False)
    _run(draft=False)
    assert mcp.calls_of("rag_retrieve_evidence")[-1]["arguments"]["planning"] == "caller"


def _legacy_published_version() -> None:
    """固定の中身の無い公開の版（#1855 より前の版・組み込みの業務 Agent の版と同じ形）にする。"""
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 固定していない版を作る
        stored = repository._agents[AGENT_ID]
        stored.versions = [
            AgentVersion(
                version=1,
                name=stored.name,
                instructions=stored.instructions,
                skill_ids=list(stored.skill_ids),
            )
        ]
        stored.published_version = 1


# caller でだけ効く引数（#2676。門が閉じた Run ではモデルに見せず、送らない）。
_CALLER_ONLY = frozenset({"planning", "queries", "request_id", "skip_clarification"})
# モデルが自分で caller を指定する（今のスキルの本文が言い換えを queries に入れて引くと指示する）。
_MODEL_CALLER_ARGUMENTS = {
    "query": "機器 A 点検",
    "planning": "caller",
    "queries": ["機器A 定期点検"],
    "request_id": "model-chosen",
    "skip_clarification": True,
}


def _assert_rag_planned(run_id: str, call: dict[str, Any], model: ScriptedModel) -> None:
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # 引数の誤りとして差し戻さず、呼び先へ送る前に取り除く。
    results = [step.tool_result for step in run.steps if step.tool_result is not None]
    assert results and all(result.success for result in results)
    assert not _CALLER_ONLY & set(call["arguments"])
    assert call["arguments"]["query"] == "機器 A 点検"
    params = _tools(model)["rag__rag_retrieve_evidence"].params_json_schema
    assert not _CALLER_ONLY & set(params["properties"])


def test_published_version_without_pinned_skills_keeps_rag_planning(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """固定の中身の無い公開の版は、今のスキルが宣言しても caller にしない（配備で切り替えない）。

    モデルが planning=caller と queries を渡しても、呼び先へ送らない（#2676）。
    """
    _legacy_published_version()
    call = function_call("rag__rag_retrieve_evidence", _MODEL_CALLER_ARGUMENTS, call_id="c1")
    model = _script(monkeypatch, [call], [assistant_message(ANSWER)], validation=False)
    published_run = _run(draft=False)
    [published] = mcp.calls_of("rag_retrieve_evidence")
    _assert_rag_planned(published_run, published, model)

    # 下書きは今どおり caller（モデルの queries を送り、planning・追跡 ID は実行環境が決める）。
    model = _script(monkeypatch, [call], [assistant_message(ANSWER)], validation=False)
    draft_run = _run(draft=True)
    draft = mcp.calls_of("rag_retrieve_evidence")[-1]
    assert draft["arguments"]["planning"] == "caller"
    assert draft["arguments"]["queries"] == ["機器A 定期点検"]
    assert draft["arguments"]["request_id"] == f"{draft_run}:c1"
    params = _tools(model)["rag__rag_retrieve_evidence"].params_json_schema
    assert "queries" in params["properties"]
    assert "planning" not in params["properties"]


def test_latest_policy_version_follows_the_current_skill(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """「常に最新」を選んだ公開の版は今のスキルの宣言に従う（利用者が今のスキルを選んでいる）。"""
    skill_registry.upsert_custom(_skill("rag"))
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(skill_policy="latest"))
    runtime_repository.publish_agent(AGENT_ID, published_by="admin")
    skill_registry.upsert_custom(_skill("caller"))
    call = function_call("rag__rag_retrieve_evidence", {"query": "機器 A 点検"}, call_id="c1")
    _script(monkeypatch, [call], [assistant_message(ANSWER)], validation=False)

    _run(draft=False)

    [published] = mcp.calls_of("rag_retrieve_evidence")
    assert published["arguments"]["planning"] == "caller"


@pytest.mark.parametrize("agent_id", ["default", "rag-research"])
def test_builtin_published_agents_keep_rag_planning(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, agent_id: str
) -> None:
    """組み込みの業務 Agent の公開の版（固定の中身が無い）は、配備の時点では RAG が計画する。"""
    agent = next(item for item in runtime_repository.list_agents() if item.id == agent_id)
    assert agent.published_version is not None
    version = agent.published()
    assert version is not None and version.skills is None
    # モデルが planning=caller と queries を渡しても、呼び先へ送らない（#2676）。
    call = function_call("rag__rag_retrieve_evidence", _MODEL_CALLER_ARGUMENTS, call_id="c1")
    model = _script(monkeypatch, [call], [assistant_message(ANSWER)], validation=False)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=GOAL, agent_id=agent_id), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)

    [published] = mcp.calls_of("rag_retrieve_evidence")
    _assert_rag_planned(run.id, published, model)


@pytest.mark.parametrize(
    "skill_id",
    ["business-rag-research", "rag-and-structured-data", "article-catalog-lookup"],
)
def test_builtin_rag_skills_declare_caller_planning(skill_id: str) -> None:
    skill = skill_registry.get(skill_id)
    assert skill is not None
    [rag] = [item for item in skill.mcp_requirements if item.server_id == "rag"]
    assert rag.evidence_planning == "caller"


def test_skill_md_metadata_declares_evidence_planning(tmp_path: Any) -> None:
    folder = tmp_path / "evidence-skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: evidence-skill\ndescription: 根拠を集めるときに使う。\nmetadata:\n"
        '  production-ready-mcp-tools: "rag:rag_retrieve_evidence nl2sql"\n'
        '  production-ready-evidence-planning: "rag:caller nl2sql:unknown"\n'
        "---\n本文。\n",
        encoding="utf-8",
    )
    [skill] = load_skills_from_dir(str(tmp_path))
    planning = {item.server_id: item.evidence_planning for item in skill.mcp_requirements}
    assert planning == {"rag": "caller", "nl2sql": "rag"}


def test_caller_planned_schema_keeps_old_contracts() -> None:
    """planning を持たない（古い）RAG の契約・ほかのツールの schema は変えない。"""
    old = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    assert caller_planned_schema("rag__rag_retrieve_evidence", old) is old
    current = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "planning": {"enum": ["rag", "caller"]},
            "request_id": {"type": "string"},
        },
        "required": ["query"],
    }
    assert caller_planned_schema("rag__rag_search", current) is current
    assert set(caller_planned_schema("rag__rag_retrieve_evidence", current)["properties"]) == {
        "query"
    }


def test_rag_planned_schema_and_arguments_drop_caller_only_arguments() -> None:
    """門が閉じた接続の根拠の取得は caller の引数を見せず・送らない。

    古い契約・ほかのツールはそのまま。
    """
    current = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "planning": {"enum": ["rag", "caller"]},
            "queries": {"type": "array", "items": {"type": "string"}},
            "request_id": {"type": "string"},
            "skip_clarification": {"type": "boolean"},
            "conditions": {"type": "object"},
        },
        "required": ["query", "planning"],
    }
    visible = rag_planned_schema("rag__rag_retrieve_evidence", current)
    assert set(visible["properties"]) == {"query", "conditions"}
    assert visible["required"] == ["query"]
    assert set(current["properties"]) >= _CALLER_ONLY  # 契約（呼び先へ送る schema）は変えない
    assert rag_planned_arguments(
        "rag__rag_retrieve_evidence", current, _MODEL_CALLER_ARGUMENTS
    ) == {"query": "機器 A 点検"}
    assert rag_planned_schema("rag__rag_search", current) is current
    assert rag_planned_arguments("rag__rag_search", current, _MODEL_CALLER_ARGUMENTS) == (
        _MODEL_CALLER_ARGUMENTS
    )
    old = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    assert rag_planned_schema("rag__rag_retrieve_evidence", old) is old


def test_agent_orchestrates_retrieval_figure_reading_answer_and_validation(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """本文・表・図の根拠を取得し、図の根拠だけ元の画像を読んでから答え、最終の検証を通す。"""
    retrieved = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    retrieved["evidence"] = [_TEXT, _TABLE, _FIGURE]
    mcp.outputs["rag_retrieve_evidence"] = retrieved
    mcp.outputs["rag_read_source"] = {
        **deepcopy(DEFAULT_OUTPUTS["rag_read_source"]),
        "evidence_id": "chunk-figure",
        "chunk_id": "chunk-figure",
        "content_kind": "figure",
        "evidence_type": "figure_description",
        "image_ref": _FIGURE["image_ref"],
        "text": "点検の画面の図。確定のボタンは右下。",
    }
    _script(
        monkeypatch,
        [function_call("rag__rag_retrieve_evidence", {"query": "機器 A 点検"}, call_id="c1")],
        [
            function_call(
                "rag__rag_read_source",
                {"document_id": "doc-1", "chunk_id": "chunk-figure", "include_image": True},
                call_id="c2",
            )
        ],
        [assistant_message(ANSWER)],
        validation=True,
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # RAG に回答を作らせない（回答の生成・監査は rag_search の経路）。
    assert mcp.calls_of("rag_search") == []
    assert len(mcp.calls_of("rag_retrieve_evidence")) == 1
    assert mcp.calls_of("rag_read_source")[0]["arguments"]["include_image"] is True
    # 最終の検証は、取得した根拠と読んだ図の根拠で回答を確かめる。
    [validation] = mcp.calls_of("rag_validate_answer")
    checked = {item["chunk_id"] for item in validation["arguments"]["evidence"]}
    assert {"chunk-text", "chunk-table", "chunk-figure"} <= checked
    assert validation["arguments"]["answer"].startswith("機器 A の点検は毎月 1 回です。")


def test_rule_clarification_of_retrieval_becomes_the_next_step_and_pending(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    clarification = {
        "condition_id": "rule:target",
        "label": "対象",
        "question": "どの機器の点検ですか",
        "options": ["機器 A", "機器 B"],
        "multiple": False,
    }
    asked = {
        **deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]),
        "evidence": [],
        "clarifications": [clarification],
    }
    mcp.outputs["rag_retrieve_evidence"] = asked
    model = _script(
        monkeypatch,
        [function_call("rag__rag_retrieve_evidence", {"query": "点検 周期"}, call_id="c1")],
        [assistant_message("どの機器の点検ですか。")],
        validation=False,
    )
    run_id = _run("点検の周期は？")

    output = next(
        item["output"]
        for item in model.calls[1].input
        if isinstance(item, dict) and item.get("call_id") == "c1" and "output" in item
    )
    assert '"next_step"' in str(output)
    assert "どの機器の点検ですか" in str(output)
    state = run_support_task(runtime_repository.get_run(run_id)) or {}
    pending = state.get("content", state).get("pending_clarifications") or []
    assert [item["condition_id"] for item in pending] == ["rule:target"]


def test_evidence_clarification_step_only_when_asked() -> None:
    assert evidence_clarification_step({"evidence": [], "clarifications": []}) is None
    step = evidence_clarification_step(
        {"clarifications": [{"condition_id": "rule:x", "label": "x", "question": "どれ？"}]}
    )
    assert step is not None
    assert step["reason"] == "needs_clarification"
