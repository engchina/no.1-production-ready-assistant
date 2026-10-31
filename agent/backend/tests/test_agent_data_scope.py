"""業務 Agent のデータの範囲（使える RAG / NL2SQL のプロファイル。#1378）。

- 定義: 接続ごとの一覧と既定の検証、保存のときに編集者が使えるかを MCP で確かめる。
- 組み込み Runtime: 1 つなら埋めて上書きし schema から除く、複数なら範囲外を呼び先へ送らずに
  拒否し、無ければ既定を使う。一覧・推薦の結果を絞る。RAG は knowledge_base_ids を使わない。
- 範囲を設定していない Agent は今までどおり（既存のテストが確かめる。ここでも 1 件）。
"""

from __future__ import annotations

import contextlib
import threading
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
    Nl2SqlQueryIn,
    RagSearchIn,
    fake_product_mcp,
)
from pydantic import ConfigDict, ValidationError
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import builtin_runtime, data_scope
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.data_scope import AgentDataScope, scoped_input_schema
from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ToolInvocationContext, ToolPolicy
from app.security.service import set_security_service
from app.settings import get_settings

SKILL_ID = "test1378-skill"
AGENT_ID = "test1378-agent"
USER_UUID = "13781378-2222-3333-4444-555555555555"


# Control Plane が決定的に付ける引数（利用者の元の質問。#2261。根拠の取得の検索文を作る主体と
# 追跡 ID。#2651）。
_RUNTIME_ARGUMENTS = frozenset({"original_question", "planning", "request_id"})


def _scope_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """範囲の確認に関係する引数（Control Plane が付ける引数は除く）。"""
    return {key: value for key, value in call["arguments"].items() if key not in _RUNTIME_ARGUMENTS}


def _scope(*ids: str, default: str = "") -> AgentDataScope:
    return AgentDataScope(profile_ids=list(ids), default_profile_id=default)


# ---------------------------------------------------------------------------
# 定義の検証
# ---------------------------------------------------------------------------


def test_single_profile_becomes_the_default() -> None:
    scope = _scope(" profile-sales ", "profile-sales")
    assert scope.profile_ids == ["profile-sales"]
    assert scope.default_profile_id == "profile-sales"


def test_multiple_profiles_need_a_default_in_the_list() -> None:
    with pytest.raises(ValidationError, match="既定のプロファイルを選んでください"):
        _scope("a", "b")
    with pytest.raises(ValidationError, match="使えるプロファイルの中から"):
        _scope("a", "b", default="c")
    assert _scope("a", "b", default="b").default_profile_id == "b"
    # 一覧が空は「範囲なし」（既定も持たない）。
    assert _scope(default="x").default_profile_id == ""


def test_only_builtin_connections_can_have_a_scope() -> None:
    with pytest.raises(ValidationError, match="データの範囲を設定できない接続"):
        AgentProfile(name="x", data_scopes={"crm": _scope("a")})
    # 一覧が空の接続は保存しない（範囲なし）。
    agent = AgentProfile(name="x", data_scopes={"rag": _scope(), "nl2sql": _scope("a")})
    assert list(agent.data_scopes) == ["nl2sql"]


def test_scope_is_part_of_the_published_version() -> None:
    agent_id = f"{AGENT_ID}-version"
    runtime_repository.create_agent(
        AgentProfile(id=agent_id, name="版", data_scopes={"nl2sql": _scope("a")})
    )
    try:
        published = runtime_repository.publish_agent(agent_id)
        assert published.published() is not None
        assert published.published().data_scopes["nl2sql"].profile_ids == ["a"]  # type: ignore[union-attr]
        assert published.unpublished_changes is False
        from app.features.agent.runtime import AgentProfilePatch

        changed = runtime_repository.patch_agent(
            agent_id, AgentProfilePatch(data_scopes={"nl2sql": _scope("a", "b", default="b")})
        )
        # 範囲の変更は下書きの変更（公開するまで利用者の Run には効かない）。
        assert changed.unpublished_changes is True
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(agent_id)


def test_single_scope_hides_the_profile_argument_from_the_model() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "search_answer_profile_id": {"type": "string"},
            "knowledge_base_ids": {"type": "array"},
        },
        "required": ["query", "search_answer_profile_id"],
    }
    single = scoped_input_schema("rag__rag_lookup_guides", schema, {"rag": _scope("bv-sales")})
    assert set(single["properties"]) == {"query", "knowledge_base_ids"}
    assert single["required"] == ["query"]
    searched = scoped_input_schema("rag__rag_search", schema, {"rag": _scope("bv-sales")})
    assert set(searched["properties"]) == {"query"}
    many = scoped_input_schema(
        "rag__rag_search", schema, {"rag": _scope("bv-a", "bv-b", default="bv-b")}
    )
    assert "bv-a、bv-b" in many["properties"]["search_answer_profile_id"]["description"]
    assert "knowledge_base_ids" not in many["properties"]
    # 範囲の無い接続・別の接続のツールは元のまま。
    assert scoped_input_schema("rag__rag_search", schema, {}) is schema
    assert scoped_input_schema("crm__rag_search", schema, {"rag": _scope("a")}) is schema


# ---------------------------------------------------------------------------
# 組み込み Runtime の強制
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="範囲の Skill",
            instructions="RAG と NL2SQL で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag"),
                SkillMcpRequirement(server_id="nl2sql"),
            ],
        )
    )
    # nl2sql_query は承認が要る（readOnlyHint=false）。強制の確認では承認なしで呼ぶ。
    monkeypatch.setattr(
        builtin_runtime,
        "_active_policy",
        lambda: ToolPolicy(allow={"nl2sql__nl2sql_query"}),
    )
    try:
        yield fake
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


def _agent(**scopes: AgentDataScope) -> None:
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="範囲の Agent", skill_ids=[SKILL_ID], data_scopes=scopes)
    )


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
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    return model


def _run(goal: str = "今月の売上を教えて", metadata: dict[str, Any] | None = None) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID, draft=True, metadata=metadata or {}),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return run.id


def _tools(model: ScriptedModel) -> dict[str, FunctionTool]:
    return {tool.name: tool for tool in model.calls[0].tools if isinstance(tool, FunctionTool)}


def test_single_profile_is_filled_and_overrides_the_model(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _agent(nl2sql=_scope("profile-sales"))
    model = _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_query",
                {"question": "今月の売上", "profile_id": "profile-hr"},
                call_id="call-1",
            )
        ],
        [assistant_message("売上は 1200 です。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # モデルに見せる schema に profile_id は無い（選ばせない）。
    params = _tools(model)["nl2sql__nl2sql_query"].params_json_schema
    assert "profile_id" not in params["properties"]
    # 指示に範囲の案内が入る（ツールの名前はモデルに渡す名前）。
    instructions = str(model.calls[0].system_instructions)
    assert "# データの範囲" in instructions
    assert "「profile-sales」に決まっている" in instructions
    # モデルが別の ID を渡しても、設定したプロファイルで呼ぶ。
    [call] = mcp.calls_of("nl2sql_query")
    assert call["arguments"]["profile_id"] == "profile-sales"
    [step] = run.steps
    assert step.tool_call is not None
    assert step.tool_call.arguments["profile_id"] == "profile-sales"
    assert step.tool_call.data_scope == {
        "connection": "nl2sql",
        "allowed_profile_ids": ["profile-sales"],
        "argument": "profile_id",
        "requested_profile_id": "profile-hr",
        "profile_id": "profile-sales",
        "action": "overridden",
    }


def test_out_of_scope_profile_is_rejected_without_calling_the_product(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _agent(nl2sql=_scope("profile-sales", "profile-cost", default="profile-cost"))
    model = _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_query",
                {"question": "人事の数", "profile_id": "profile-hr"},
                call_id="call-1",
            )
        ],
        [function_call("nl2sql__nl2sql_query", {"question": "原価"}, call_id="call-2")],
        [assistant_message("原価を答えました。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # 範囲外の 1 回目は呼び先へ送らず、ID の無い 2 回目は Agent の既定で呼ぶ（"default" ではない）。
    calls = mcp.calls_of("nl2sql_query")
    assert [call["arguments"]["profile_id"] for call in calls] == ["profile-cost"]
    rejected, filled = run.steps
    assert rejected.status == "failed"
    assert rejected.tool_result is not None
    assert rejected.tool_result.error_code == data_scope.DATA_SCOPE_VIOLATION_CODE
    assert "profile-sales、profile-cost" in (rejected.tool_result.error or "")
    assert rejected.tool_call is not None and rejected.tool_call.data_scope is not None
    assert rejected.tool_call.data_scope["action"] == "rejected"
    assert filled.tool_call is not None and filled.tool_call.data_scope is not None
    assert filled.tool_call.data_scope["action"] == "filled"
    assert filled.tool_call.data_scope["profile_id"] == "profile-cost"
    # モデルにはエラーと使えるプロファイルを返す。
    tool_output = str(model.calls[1].input)
    assert data_scope.DATA_SCOPE_VIOLATION_CODE in tool_output
    # 複数なら schema に残し、使える ID を説明に足す。
    params = _tools(model)["nl2sql__nl2sql_query"].params_json_schema
    assert "profile-sales、profile-cost" in params["properties"]["profile_id"]["description"]


def test_list_and_recommendation_are_narrowed_to_the_scope(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["nl2sql_list_profiles"] = {
        "profiles": [
            {"id": "profile-sales", "name": "売上", "category": "", "description": ""},
            {"id": "profile-hr", "name": "人事", "category": "", "description": ""},
            {"id": "profile-cost", "name": "原価", "category": "", "description": ""},
        ]
    }
    mcp.outputs["nl2sql_recommend_profile"] = {
        "recommended_profile_id": "profile-hr",
        "rewritten_question": None,
        "candidates": [
            {"id": "profile-hr", "name": "人事", "reason": None, "score": 0.9},
            {"id": "profile-sales", "name": "売上", "reason": None, "score": 0.5},
        ],
    }
    _agent(nl2sql=_scope("profile-sales", "profile-cost", default="profile-sales"))
    _script(
        monkeypatch,
        [
            function_call("nl2sql__nl2sql_list_profiles", {"limit": 1}, call_id="call-1"),
            function_call(
                "nl2sql__nl2sql_recommend_profile", {"question": "売上"}, call_id="call-2"
            ),
        ],
        [assistant_message("売上の業務プロファイルで答えます。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    listed, recommended = run.steps
    # 一覧は範囲で絞ってから、モデルが求めた件数に切る（呼び先には上限まで求める）。
    [list_call] = mcp.calls_of("nl2sql_list_profiles")
    assert list_call["arguments"]["limit"] == 100
    assert listed.tool_result is not None and listed.tool_result.output is not None
    assert [item["id"] for item in listed.tool_result.output["profiles"]] == ["profile-sales"]
    # 範囲外は 1 件（人事）。原価は範囲内だが、求めた件数（1）で切る。
    assert listed.tool_result.audit_metadata["data_scope"] == {"filtered_out": 1}
    # 範囲外の推薦は null、候補も範囲で絞る。
    assert recommended.tool_result is not None and recommended.tool_result.output is not None
    assert recommended.tool_result.output["recommended_profile_id"] is None
    assert [item["id"] for item in recommended.tool_result.output["candidates"]] == [
        "profile-sales"
    ]
    assert recommended.tool_call is not None and recommended.tool_call.data_scope is not None
    assert recommended.tool_call.data_scope["action"] == "filtered"


def test_rag_recommendation_is_narrowed_to_the_scope(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """RAG の推薦（#1850）も NL2SQL と同じく、候補を範囲で埋めて結果を範囲で絞る。"""
    mcp.outputs["rag_recommend_search_answer_profile"] = {
        "recommended_profile_id": "bv-hr",
        "candidates": [
            {"id": "bv-hr", "name": "人事", "score": 1.0, "documents": []},
            {"id": "bv-sales", "name": "営業", "score": 0.8, "documents": []},
        ],
    }
    _agent(rag=_scope("bv-sales", "bv-legal", default="bv-sales"))
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_recommend_search_answer_profile",
                {"question": "契約の更新"},
                call_id="call-1",
            )
        ],
        [assistant_message("営業の検索・回答プロファイルで調べます。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    [recommended] = run.steps
    [call] = mcp.calls_of("rag_recommend_search_answer_profile")
    assert call["arguments"]["profile_ids"] == ["bv-sales", "bv-legal"]
    assert call["claims"]["profile_ids"] == ["bv-sales", "bv-legal"]
    assert recommended.tool_result is not None and recommended.tool_result.output is not None
    assert recommended.tool_result.output["recommended_profile_id"] is None
    assert [item["id"] for item in recommended.tool_result.output["candidates"]] == ["bv-sales"]
    params = _tools(model)["rag__rag_recommend_search_answer_profile"].params_json_schema
    assert "profile_ids" not in params["properties"]


def test_rag_scope_ignores_knowledge_base_ids(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _agent(rag=_scope("bv-sales"))
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search",
                {"query": "契約の更新", "knowledge_base_ids": ["kb-hr"]},
                call_id="call-1",
            )
        ],
        [assistant_message("更新は第 5 条です。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [call] = mcp.calls_of("rag_search")
    # プロファイルの範囲を、ナレッジベースの直接の指定で迂回させない。
    assert _scope_arguments(call) == {"query": "契約の更新", "search_answer_profile_id": "bv-sales"}
    params = _tools(model)["rag__rag_search"].params_json_schema
    assert "knowledge_base_ids" not in params["properties"]
    assert "search_answer_profile_id" not in params["properties"]
    step = run.steps[0]
    assert step.tool_call is not None and step.tool_call.data_scope is not None
    assert step.tool_call.data_scope["action"] == "filled"
    assert step.tool_call.data_scope["ignored_knowledge_base_ids"] == ["kb-hr"]


def test_single_rag_profile_raises_small_top_k_to_the_default(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # プロファイルを固定した業務 Agent では、モデルが残りの任意の引数（top_k）を小さく埋めやすい
    # （#1403）。既定より小さい top_k は既定（50）に引き上げ、モデルへの結果に残す。
    _agent(rag=_scope("bv-sales"))
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "システム台帳 販売管理システム 担当部署", "top_k": 5},
                call_id="call-1",
            )
        ],
        [assistant_message("担当部署は営業部です。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [call] = mcp.calls_of("rag_retrieve_evidence")
    assert _scope_arguments(call) == {
        "query": "システム台帳 販売管理システム 担当部署",
        "top_k": 50,
        "search_answer_profile_id": "bv-sales",
    }
    params = _tools(model)["rag__rag_retrieve_evidence"].params_json_schema
    assert "search_answer_profile_id" not in params["properties"]
    assert "多段の質問では省略する" in params["properties"]["top_k"]["description"]
    output = next(
        item["output"]
        for item in model.calls[1].input
        if isinstance(item, dict) and item.get("call_id") == "call-1" and "output" in item
    )
    assert '"adjusted_limits"' in str(output)


def test_approval_shows_the_scoped_arguments(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    _agent(nl2sql=_scope("profile-sales"))
    # ツール権限で nl2sql_query を「承認を要求」にした構成（明示の ask は既定より優先。#1832）。
    monkeypatch.setattr(
        builtin_runtime,
        "_active_policy",
        lambda: ToolPolicy(default_mode="approval", ask={"nl2sql__nl2sql_query"}),
    )
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", {"question": "売上"}, call_id="call-1")],
        [assistant_message("売上は 1200 です。")],
    )
    run_id = _run()

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    [approval] = waiting.approvals
    # 承認者には、実行するときに送る（範囲を当てた）引数を見せる。
    assert approval.tool_call.arguments["profile_id"] == "profile-sales"
    assert approval.tool_call.data_scope is not None
    runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by="approver")
    )
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [call] = mcp.calls_of("nl2sql_query")
    assert call["arguments"]["profile_id"] == "profile-sales"
    [step] = run.steps
    assert step.tool_call is not None and step.tool_call.data_scope is not None
    assert step.tool_call.data_scope["action"] == "filled"


def test_agent_without_scope_keeps_the_model_choice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _agent()
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search",
                {"query": "契約", "knowledge_base_ids": ["kb-1"]},
                call_id="call-1",
            )
        ],
        [assistant_message("回答です。")],
    )
    run_id = _run()

    [call] = mcp.calls_of("rag_search")
    assert _scope_arguments(call) == {"query": "契約", "knowledge_base_ids": ["kb-1"]}
    # 範囲の無い Agent はサービストークンに範囲の claim を付けない（#1379）。
    assert "profile_ids" not in call["claims"]
    assert "# データの範囲" not in str(model.calls[0].system_instructions)
    step = runtime_repository.get_run(run_id).steps[0]
    assert step.tool_call is not None and step.tool_call.data_scope is None


def test_scope_is_sent_to_the_product_as_a_token_claim(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """呼び先でも範囲を強制させるため、接続ごとの範囲をサービストークンの claim に入れる。

    #1379。
    """
    _agent(
        nl2sql=_scope("profile-sales", "profile-cost", default="profile-sales"),
        rag=_scope("bv-sales"),
    )
    model = _script(
        monkeypatch,
        [
            function_call("nl2sql__nl2sql_query", {"question": "売上"}, call_id="call-1"),
            function_call(
                "nl2sql__nl2sql_recommend_profile",
                {"question": "原価", "profile_ids": ["profile-cost", "profile-hr"]},
                call_id="call-2",
            ),
            function_call("rag__rag_search", {"query": "契約"}, call_id="call-3"),
        ],
        [assistant_message("売上は 1200 です。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [query] = mcp.calls_of("nl2sql_query")
    assert query["claims"]["profile_ids"] == ["profile-sales", "profile-cost"]
    [recommend] = mcp.calls_of("nl2sql_recommend_profile")
    assert recommend["claims"]["profile_ids"] == ["profile-sales", "profile-cost"]
    # 推薦の候補は範囲の中だけ（範囲外の profile-hr は外す）。
    assert recommend["arguments"]["profile_ids"] == ["profile-cost"]
    params = _tools(model)["nl2sql__nl2sql_recommend_profile"].params_json_schema
    assert "profile_ids" not in params["properties"]
    # 接続ごとの範囲だけを入れる（RAG の token に NL2SQL の範囲を入れない）。
    [search] = mcp.calls_of("rag_search")
    assert search["claims"]["profile_ids"] == ["bv-sales"]
    assert all("profile_ids" in call["claims"] for call in mcp.tool_calls)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, "user"),
        ({"evaluation_job_id": "job-1", "evaluation_case_id": "case-1"}, "evaluation"),
    ],
    ids=["user", "evaluation"],
)
def test_run_source_is_sent_to_the_product_as_a_token_claim(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, metadata: dict[str, Any], expected: str
) -> None:
    """Run のきっかけ（品質評価の Run は `evaluation`）をサービストークンの claim に入れる。

    呼び先（NL2SQL）の実行履歴が品質評価の Run の実行を既定で隠すため（#1986）。`metadata` は API
    では予約で渡せないので、repository で作る（品質評価と同じ）。
    """
    _agent(nl2sql=_scope("profile-sales"))
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", {"question": "売上"}, call_id="call-1")],
        [assistant_message("売上は 1200 です。")],
    )
    run_id = _run(metadata=metadata)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [query] = mcp.calls_of("nl2sql_query")
    assert query["claims"]["run_source"] == expected
    assert query["claims"]["run_id"] == run_id


def test_final_validation_uses_the_scope_claim(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """回答の最終の検証（根拠の読み直し）も、Run と同じ範囲の token で呼ぶ（#1379）。"""
    _agent(rag=_scope("bv-sales"))
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "契約"}, call_id="call-1")],
        [assistant_message("契約の更新は第 5 条で定めています。")],
    )
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [validate] = mcp.calls_of("rag_validate_answer")
    assert validate["claims"]["profile_ids"] == ["bv-sales"]


def test_missing_profile_id_is_left_to_the_product_to_select(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """範囲の無い Agent で profile_id の無い nl2sql_query は、そのまま呼び先へ送る。

    呼び先（NL2SQL）が質問から業務プロファイルを選ぶ（"default" には切り替えない。#1379・#2355）。
    モデルに ID を推測させるために拒否しない。
    """
    _agent()
    model = _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_query", {"question": "売上"}, call_id="call-1")],
        [assistant_message("売上は 1200 です。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    calls = mcp.calls_of("nl2sql_query")
    assert len(calls) == 1
    assert "profile_id" not in calls[0]["arguments"]
    # 契約どおり、モデルに見せる schema でも profile_id は任意。
    params = _tools(model)["nl2sql__nl2sql_query"].params_json_schema
    assert "profile_id" not in params.get("required", [])


# ---------------------------------------------------------------------------
# 編集画面の候補と保存の確認（編集者のサービストークン）
# ---------------------------------------------------------------------------


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_candidates_are_listed_as_the_editor(auth: ProductionAuth, mcp: FakeProductMcp) -> None:
    editor = auth.create_user("scope-editor", system_admin=True)
    headers = login("scope-editor")

    response = client.get("/api/agent-data-scopes/nl2sql/candidates", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["connection"] == "nl2sql"
    assert [item["id"] for item in data["profiles"]] == ["profile-sales"]
    [call] = mcp.calls_of("nl2sql_list_profiles")
    # 編集者として呼ぶ（編集者が使えるものだけが返る）。
    assert call["claims"]["sub"] == editor.user_uuid
    assert call["arguments"] == {"limit": 100}

    unknown = client.get("/api/agent-data-scopes/crm/candidates", headers=headers)
    assert unknown.status_code == 404


def test_candidates_report_an_unconfigured_connection(
    auth: ProductionAuth, mcp: FakeProductMcp
) -> None:
    del mcp
    from app.features.agent.config import runtime_config_store

    runtime_config_store.upsert_mcp_server("rag", base_url="")
    auth.create_user("scope-editor2", system_admin=True)
    headers = login("scope-editor2")

    response = client.get("/api/agent-data-scopes/rag/candidates", headers=headers)

    assert response.status_code == 409
    assert "URL が設定されていない" in response.json()["error_messages"][0]


def test_profiles_the_editor_cannot_use_are_not_saved(
    auth: ProductionAuth, mcp: FakeProductMcp
) -> None:
    auth.create_user("scope-editor3", system_admin=True)
    headers = login("scope-editor3")
    body = {
        "id": AGENT_ID,
        "name": "範囲の Agent",
        "data_scopes": {"nl2sql": {"profile_ids": ["profile-hr"], "default_profile_id": ""}},
    }

    rejected = client.post("/api/agents", json=body, headers=headers)

    assert rejected.status_code == 400, rejected.text
    assert "profile-hr" in rejected.json()["error_messages"][0]
    assert all(agent.id != AGENT_ID for agent in runtime_repository.list_agents())

    body["data_scopes"] = {"nl2sql": {"profile_ids": ["profile-sales"], "default_profile_id": ""}}
    created = client.post("/api/agents", json=body, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["data"]["data_scopes"] == {
        "nl2sql": {"profile_ids": ["profile-sales"], "default_profile_id": "profile-sales"}
    }

    # 変更でも、加えたプロファイルを確かめる（既に範囲にあるものは確かめない）。
    patched = client.patch(
        f"/api/agents/{AGENT_ID}",
        json={
            "data_scopes": {
                "nl2sql": {
                    "profile_ids": ["profile-sales", "profile-hr"],
                    "default_profile_id": "profile-sales",
                }
            }
        },
        headers=headers,
    )
    assert patched.status_code == 400
    calls_before = len(mcp.calls_of("nl2sql_list_profiles"))
    unchanged = client.patch(
        f"/api/agents/{AGENT_ID}",
        json={
            "name": "名前だけ変える",
            "data_scopes": {"nl2sql": {"profile_ids": ["profile-sales"]}},
        },
        headers=headers,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert len(mcp.calls_of("nl2sql_list_profiles")) == calls_before
    # 範囲を外す（未設定に戻す）のは確かめずに保存できる。
    cleared = client.patch(f"/api/agents/{AGENT_ID}", json={"data_scopes": {}}, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["data"]["data_scopes"] == {}


def test_scopes_keep_their_type_across_repeated_saves(
    auth: ProductionAuth, mcp: FakeProductMcp
) -> None:
    """範囲を保存した後も型を保ち、続けて加える・外す保存が 200 になる（#2694）。"""
    mcp.outputs["nl2sql_list_profiles"] = {
        "profiles": [
            {"id": "profile-a", "name": "A", "category": "", "description": ""},
            {"id": "profile-b", "name": "B", "category": "", "description": ""},
        ]
    }
    auth.create_user("scope-editor4", system_admin=True)
    headers = login("scope-editor4")
    created = client.post(
        "/api/agents", json={"id": AGENT_ID, "name": "範囲の Agent"}, headers=headers
    )
    assert created.status_code == 200, created.text

    def save(*ids: str) -> None:
        response = client.patch(
            f"/api/agents/{AGENT_ID}",
            json={
                "data_scopes": {"nl2sql": {"profile_ids": list(ids), "default_profile_id": ids[0]}},
                "output_fields": [{"key": "item", "label": "項目"}],
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["data_scopes"]["nl2sql"]["profile_ids"] == list(ids)
        agent = next(item for item in runtime_repository.list_agents() if item.id == AGENT_ID)
        assert isinstance(agent.data_scopes["nl2sql"], AgentDataScope)
        # 保存した値は、保存先から読み直したときと同じ型（モデル）のまま。
        assert agent == AgentProfile.model_validate(agent.model_dump())

    save("profile-a")
    # 2 回目の保存（範囲に加える）も、保存済みの範囲を型のまま比べて 200。
    save("profile-a", "profile-b")
    save("profile-b")
    # 編集者で確かめるのは加えたものだけ（1 回目の profile-a と 2 回目の profile-b）。
    assert len(mcp.calls_of("nl2sql_list_profiles")) == 2


# ---- 候補の名前をプロファイルの ID に引き直す（#2230） ------------------------------------

_CANDIDATES: list[dict[str, Any]] = [
    {"id": "profile-hr", "name": "人事", "reason": None, "score": 0.0},
    {"id": "profile-sales", "name": "売上", "reason": None, "score": 0.0},
]
_TWO_CANDIDATES: dict[str, Any] = {
    "recommended_profile_id": None,
    "rewritten_question": None,
    "candidates": _CANDIDATES,
}


def test_profile_name_from_the_candidates_is_sent_as_the_id(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """確認で選んだ候補の名前を profile_id に渡しても、候補の ID で呼び先へ送る（#2230）。"""
    mcp.outputs["nl2sql_recommend_profile"] = _TWO_CANDIDATES
    _agent()
    _script(
        monkeypatch,
        [function_call("nl2sql__nl2sql_recommend_profile", {"question": "売上"}, call_id="c-1")],
        [
            function_call(
                "nl2sql__nl2sql_query", {"question": "売上", "profile_id": "売上"}, call_id="c-2"
            )
        ],
        [assistant_message("売上は 1,200 です。")],
    )
    _run()
    [sent] = mcp.calls_of("nl2sql_query")
    assert sent["arguments"]["profile_id"] == "profile-sales"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("profile-hr", "profile-hr"),
        ("経理", "経理"),
        ("重複", "重複"),
    ],
    ids=["id-kept", "unknown-name-kept", "ambiguous-name-kept"],
)
def test_profile_value_is_kept_unless_one_candidate_name_matches(value: str, expected: str) -> None:
    from app.features.agent.runtime import RunStep
    from app.features.agent.tools import ToolCall, ToolResult

    output = {
        **_TWO_CANDIDATES,
        "candidates": [
            *_CANDIDATES,
            {"id": "profile-a", "name": "重複", "reason": None, "score": 0.0},
            {"id": "profile-b", "name": "重複", "reason": None, "score": 0.0},
        ],
    }
    steps = [
        RunStep(
            run_id="run-x",
            status="completed",
            tool_call=ToolCall(name="nl2sql__nl2sql_recommend_profile", arguments={}),
            tool_result=ToolResult(
                name="nl2sql__nl2sql_recommend_profile", success=True, output=output
            ),
        )
    ]
    arguments = {"question": "売上", "profile_id": value}
    resolved, note = data_scope.resolve_profile_name("nl2sql__nl2sql_query", arguments, steps)
    assert resolved["profile_id"] == expected
    assert note is None
    # 別の接続の候補では引き直さない（RAG の検索に NL2SQL の候補の名前は使わない）。
    rag, rag_note = data_scope.resolve_profile_name(
        "rag__rag_search", {"query": "売上", "search_answer_profile_id": "売上"}, steps
    )
    assert (rag["search_answer_profile_id"], rag_note) == ("売上", None)


# ---------------------------------------------------------------------------
# 呼び出しの文脈（主体・範囲）の不変（#3253）
# ---------------------------------------------------------------------------


def test_tool_context_cannot_be_changed_after_creation() -> None:
    """文脈は属性も入れ子の範囲も書き換えられない。変えるときは model_copy で新しく作る。"""
    source = {"rag": ["bv-sales"]}
    context = ToolInvocationContext(user_uuid=USER_UUID, profile_scopes=source)
    source["rag"].append("bv-other")
    source["nl2sql"] = ["profile-hr"]
    assert dict(context.profile_scopes) == {"rag": ("bv-sales",)}

    with pytest.raises(ValidationError):
        context.user_uuid = "other-user"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        context.profile_scopes = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        context.profile_scopes["rag"] = ("bv-other",)  # type: ignore[index]
    with pytest.raises(AttributeError):
        context.profile_scopes["rag"].append("bv-other")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        ToolInvocationContext().profile_scopes["rag"] = ("bv-other",)  # type: ignore[index]

    # Runtime が範囲を入れる経路（model_copy の update は検証を通らない）も書き換えられない。
    scopes = data_scope.token_profile_scopes(
        {"rag": _scope("bv-sales"), "nl2sql": _scope("p-a", "p-b", default="p-a")}
    )
    copied = context.model_copy(update={"profile_scopes": scopes})
    assert dict(copied.profile_scopes) == {"rag": ("bv-sales",), "nl2sql": ("p-a", "p-b")}
    with pytest.raises(TypeError):
        copied.profile_scopes["rag"] = ("bv-other",)  # type: ignore[index]
    with pytest.raises(ValidationError):
        copied.user_uuid = "other-user"  # type: ignore[misc]
    assert context.profile_scopes["rag"] == ("bv-sales",)


def test_model_arguments_do_not_change_the_token_subject_or_scope(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """モデルが主体・範囲に当たる引数を渡しても、サービストークンの `sub` と範囲は変わらない。

    fake の契約は未知の項目を拒否するので、ここでは未知の項目を受け取る schema に差し替え、
    引数が呼び先まで届いたうえで token が文脈（Run の利用者・業務 Agent の範囲）のままかを見る。
    """

    class OpenQueryIn(Nl2SqlQueryIn):
        model_config = ConfigDict(extra="allow")

    class OpenSearchIn(RagSearchIn):
        model_config = ConfigDict(extra="allow")

    mcp.servers["nl2sql"].tools["nl2sql_query"] = mcp._tool(  # noqa: SLF001 - テストの差し替え
        "nl2sql_query", OpenQueryIn, read_only=False
    )
    mcp.servers["rag"].tools["rag_search"] = mcp._tool("rag_search", OpenSearchIn)  # noqa: SLF001
    _agent(
        nl2sql=_scope("profile-sales", "profile-cost", default="profile-sales"),
        rag=_scope("bv-sales"),
    )
    spoofed = {
        "user_uuid": "99999999-0000-0000-0000-000000000000",
        "sub": "99999999-0000-0000-0000-000000000000",
        "profile_ids": ["profile-hr"],
        "profile_scopes": {"nl2sql": ["profile-hr"], "rag": ["bv-hr"]},
    }
    _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_query", {"question": "売上", **spoofed}, call_id="call-1"
            ),
            function_call("rag__rag_search", {"query": "契約", **spoofed}, call_id="call-2"),
        ],
        [assistant_message("売上は 1200 です。")],
    )
    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [query] = mcp.calls_of("nl2sql_query")
    [search] = mcp.calls_of("rag_search")
    assert query["claims"]["sub"] == USER_UUID
    assert query["claims"]["profile_ids"] == ["profile-sales", "profile-cost"]
    # 引数は呼び先に届いている（届いた上で token が変わらないことを確かめている）。
    assert query["arguments"]["user_uuid"] == spoofed["user_uuid"]
    assert search["arguments"]["profile_ids"] == ["profile-hr"]
    assert search["claims"]["sub"] == USER_UUID
    assert search["claims"]["profile_ids"] == ["bv-sales"]


def test_concurrent_runs_of_different_users_keep_their_own_subject_and_scope(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """別の利用者の 2 つの Run を並行で動かしても、各 Run のツールのトークンの `sub` と
    `profile_ids` は、その Run の利用者と業務 Agent の範囲のまま（#3251）。

    呼び先の中で 2 つの呼び出しが同時にいる状態を作り（threadpool の経路）、交差しないことを見る。
    """
    other_agent_id = f"{AGENT_ID}-other"
    users = {
        AGENT_ID: ("aaaaaaaa-3251-0000-0000-000000000001", ["bv-a"]),
        other_agent_id: ("bbbbbbbb-3251-0000-0000-000000000002", ["bv-b1", "bv-b2"]),
    }
    _agent(rag=_scope("bv-a"))
    runtime_repository.create_agent(
        AgentProfile(
            id=other_agent_id,
            name="範囲の Agent（別）",
            skill_ids=[SKILL_ID],
            data_scopes={"rag": _scope("bv-b1", "bv-b2", default="bv-b1")},
        )
    )
    barrier = threading.Barrier(2, timeout=10)

    def search(_argument: Any) -> dict[str, Any]:
        # 2 つの呼び出しが同時に呼び先の中にいる状態を作る（片方だけなら timeout で失敗する）。
        barrier.wait()
        return deepcopy(DEFAULT_OUTPUTS["rag_search"])

    mcp.outputs["rag_search"] = search
    _script(monkeypatch)  # モデルの接続の設定と、最後の検証を外す
    # Run ごとに別の台本のモデル（同じモデルを 2 つの Run で共有しない）。
    monkeypatch.setattr(
        builtin_runtime,
        "model_factory",
        lambda _target: ScriptedModel(
            [
                [function_call("rag__rag_search", {"query": "契約"}, call_id="call-1")],
                [assistant_message("契約は第 5 条です。")],
            ]
        ),
    )
    run_ids = {
        agent_id: runtime_repository.create_builtin_run(
            RunCreateRequest(goal="契約を調べて", agent_id=agent_id, draft=True),
            created_by_user_uuid=user_uuid,
        ).id
        for agent_id, (user_uuid, _profiles) in users.items()
    }

    async def run_both() -> None:
        async with anyio.create_task_group() as group:
            for run_id in run_ids.values():
                group.start_soon(builtin_runtime.execute_run, run_id)

    try:
        anyio.run(run_both)
        for run_id in run_ids.values():
            run = runtime_repository.get_run(run_id)
            assert run.status == RunStatus.COMPLETED, run.events[-1].message
        calls = mcp.calls_of("rag_search")
        assert len(calls) == 2
        by_run = {call["claims"]["run_id"]: call["claims"] for call in calls}
        for agent_id, (user_uuid, profiles) in users.items():
            claims = by_run[run_ids[agent_id]]
            assert claims["sub"] == user_uuid
            assert claims["agent_id"] == agent_id
            assert claims["profile_ids"] == profiles
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            other = repository._runs.pop(run_ids[other_agent_id], None)
            for approval in other.approvals if other is not None else []:
                repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(other_agent_id)
