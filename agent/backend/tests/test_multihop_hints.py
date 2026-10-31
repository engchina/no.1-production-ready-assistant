"""多段の質問の案内（#1345・#1351・#1365）の決定論テスト。

RAG は部品（根拠・構成・順に読む）を出し、多段の組み立ては Agent が行う。Control Plane は、根拠を
集める・読むツールの本文に同じ文書の別の箇所への参照（「第 4 章を参照」）があれば読む先を、同じ
（ほぼ同じ）query の検索を繰り返したら繰り返しを止める案内を、RAG の予算があればこの Run で残る
検索の回数を、モデルへの結果に足す（記録する step の結果は RAG のまま）。モデルが既定より小さくした
`evidence_limit`・`top_k` は既定に引き上げ、上限を超える値は上限に丸め、直したことをモデルへの結果に
残す（#1351・#1403）。query の実体に当たる台帳・一覧の行に略号・区分の
短い値があれば、その意味を引く次の段を案内する（#1365）。
SDK の `ScriptedModel` と契約どおりの fake の RAG の MCP（`mcp_support`）で確かめる。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import anyio
import pytest
from agents import FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import (
    DEFAULT_OUTPUTS,
    FakeProductMcp,
    RagRetrieveEvidenceIn,
    RagSearchIn,
    fake_product_mcp,
)
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStep,
    runtime_repository,
)
from app.features.agent.skills import (
    AgentSkillDefinition,
    SkillMcpRequirement,
    skill_registry,
)
from app.features.agent.support_task import (
    ADJUSTED_LIMITS_HINT,
    MAX_RECORD_CODES,
    MAX_REFERENCES,
    MAX_SECTION_READS,
    RECORD_CODE_HINT,
    REFERENCE_READ_HINT,
    REPEATED_QUERY_HINT,
    SEARCH_LIMIT_SCHEMA_NOTE,
    SECTION_CONTINUE_HINT,
    SECTION_PARENT_HINT,
    SECTION_READ_HINT,
    SUPERSEDED_VERSION_HINT,
    UNRESOLVED_HOPS_HINT,
    adjusted_search_limits,
    asks_for_grouped_content,
    normalized_query,
    record_codes,
    repeated_query_note,
    search_limit_schema,
    section_read_note,
    similar_queries,
    superseded_versions_note,
    text_references,
    unresolved_hops,
    unresolved_hops_message,
)
from app.features.agent.tool_grounding import METADATA_KEY
from app.features.agent.tools import ToolCall, ToolResult
from app.settings import Settings, get_settings

SKILL_ID = "test1345-skill"
AGENT_ID = "test1345-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
GOAL = "HRM の変更の承認者の代理は誰ですか？"


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    # 最終の検証（#1246）はこのテストの対象外。
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="段ごとに rag_retrieve_evidence で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=[
                        "rag_search",
                        "rag_retrieve_evidence",
                        "rag_outline",
                        "rag_read_document",
                    ],
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
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run(goal: str = GOAL) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
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


def _evidence_with(excerpt: str) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    output["evidence"][0].update(document_id="doc-org", file_name="組織規程.pdf", excerpt=excerpt)
    return output


# ---- 参照の検出（決定的） ---------------------------------------------------------------------


def test_references_are_found_in_evidence_and_read_text() -> None:
    evidence = {
        "evidence": [
            {
                "document_id": "doc-org",
                "file_name": "組織規程.pdf",
                # 全角の数字は NFKC で同じに扱い、空白だけが違う同じ参照（第 4 章も）は 1 つにする。
                "excerpt": "代理は第４章を参照してください。期限は第 2 章 を参照。第 4 章も参照。",
            },
            {"document_id": "doc-ledger", "excerpt": "詳しくは別表 2 参照のこと。"},
            {"document_id": "", "excerpt": "第 9 章を参照"},
        ]
    }
    assert text_references("rag_retrieve_evidence", evidence) == [
        {
            "document_id": "doc-org",
            "file_name": "組織規程.pdf",
            "reference": "第4章",
            "how_to_read": REFERENCE_READ_HINT,
        },
        {
            "document_id": "doc-org",
            "file_name": "組織規程.pdf",
            "reference": "第 2 章",
            "how_to_read": REFERENCE_READ_HINT,
        },
        {
            "document_id": "doc-ledger",
            "file_name": None,
            "reference": "別表 2",
            "how_to_read": REFERENCE_READ_HINT,
        },
    ]

    # 読み取りのツールは本文（rag_read_source は親の本文も）から探す。
    read_source = {"document_id": "doc-1", "text": "本文", "parent_text": "付録 A を参照"}
    assert [item["reference"] for item in text_references("rag_read_source", read_source)] == [
        "付録 A"
    ]
    read_document = {"document_id": "doc-1", "text": "--- p.2 ---\n第3条を参照する。"}
    assert [item["reference"] for item in text_references("rag_read_document", read_document)] == [
        "第3条"
    ]


def test_references_ignore_mentions_without_a_reference_and_other_tools() -> None:
    # 「参照」を伴わない章の言及・ほかのツールの出力は案内しない。
    assert (
        text_references(
            "rag_retrieve_evidence",
            {"evidence": [{"document_id": "d", "excerpt": "第 4 章では代理を定める。"}]},
        )
        == []
    )
    assert (
        text_references(
            "rag_search", {"evidence": [{"document_id": "d", "excerpt": "第 4 章を参照"}]}
        )
        == []
    )
    assert text_references("rag_read_document", {"text": "第 4 章を参照"}) == []

    many = "".join(f"第 {index} 章を参照。" for index in range(1, 10))
    found = text_references("rag_read_document", {"document_id": "d", "text": many})
    assert len(found) == MAX_REFERENCES


# ---- Run ----------------------------------------------------------------------------------------


def test_evidence_with_a_reference_gets_where_to_read_and_the_remaining_budget(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_retrieve_evidence"] = _evidence_with("代理は第 4 章を参照してください。")
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "情報システム部 承認者"}, call_id="call-1"
            )
        ],
        [function_call("rag__rag_read_document", {"document_id": "doc-org"}, call_id="call-2")],
        [assistant_message("代理は管理本部長です。")],
    )
    run = _run()

    limit = int(get_settings().agent_max_rag_calls_per_run)
    seen = _tool_output(model, 1, "call-1")
    assert seen["references"] == [
        {
            "document_id": "doc-org",
            "file_name": "組織規程.pdf",
            "reference": "第 4 章",
            "how_to_read": REFERENCE_READ_HINT,
        }
    ]
    assert seen["rag_calls_remaining"] == limit - 1
    # 読み取りは RAG の上限に数えないので、残りは変わらない。
    # 参照の無い本文には references を足さない。
    read = _tool_output(model, 2, "call-2")
    assert read["rag_calls_remaining"] == limit - 1
    assert "references" not in read

    # 記録する step の結果は RAG の結果のまま。
    recorded = [step.tool_result.output for step in run.steps if step.tool_result is not None]
    assert all("references" not in (output or {}) for output in recorded)
    assert all("rag_calls_remaining" not in (output or {}) for output in recorded)


def test_no_budget_means_no_remaining_count(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_rag_calls_per_run", 0)
    model = _script(
        monkeypatch,
        [function_call("rag__rag_retrieve_evidence", {"query": "承認者"}, call_id="call-1")],
        [assistant_message("承認者は部長です。")],
    )
    _run()
    seen = _tool_output(model, 1, "call-1")
    assert "rag_calls_remaining" not in seen
    assert "references" not in seen


def test_default_rag_budget_covers_five_hops_and_a_rephrase() -> None:
    # 5 段の橋渡し（段ごとに 1 回の rag_retrieve_evidence）と 1 回の言い換え（#1345）。
    assert Settings.model_fields["agent_max_rag_calls_per_run"].default == 6


# ---- 段の検索（#1351） --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "similar"),
    [
        # 空白・助詞・疑問の記号・全角の違いは同じ query。
        ("勤怠管理システムの担当部署は？", "勤怠管理システム 担当 部署", True),
        ("ＨＲＭ　担当部署", "HRM 担当部署", True),
        ("勤怠管理システム 締め 期限", "勤怠管理システムの締めの期限", True),
        # 属性が違う・属性を足した検索は次の段（繰り返しではない）。
        ("経費精算ポータル 担当部署", "経費精算ポータル 承認者", False),
        ("経費精算ポータル 担当部署", "経費精算ポータル 担当部署 承認者", False),
        ("経費精算ポータル 担当部署", "総務部 承認者", False),
        ("", "担当部署", False),
    ],
    ids=["particles", "full-width", "paraphrase", "attribute", "added", "entity", "empty"],
)
def test_similar_queries_are_deterministic(left: str, right: str, similar: bool) -> None:
    assert similar_queries(normalized_query(left), normalized_query(right)) is similar


def test_evidence_limit_below_the_default_is_raised_to_the_schema_default() -> None:
    schema = {"properties": {"evidence_limit": {"type": "integer", "default": 12}}}
    assert adjusted_search_limits({"evidence_limit": 5}, schema) == {
        "evidence_limit": {"requested": 5, "sent": 12}
    }
    # 既定以上・指定なし・schema に既定が無いときは変えない。
    assert adjusted_search_limits({"evidence_limit": 12}, schema) == {}
    assert adjusted_search_limits({"evidence_limit": 20}, schema) == {}
    assert adjusted_search_limits({}, schema) == {}
    assert adjusted_search_limits({"evidence_limit": 5}, {"properties": {}}) == {}
    assert adjusted_search_limits({"evidence_limit": True}, schema) == {}


def _contract_schema(tool_name: str) -> dict[str, Any]:
    contract = json.loads((CONTRACTS / "rag-tools.json").read_text(encoding="utf-8"))
    [schema] = [tool["inputSchema"] for tool in contract["tools"] if tool["name"] == tool_name]
    loaded: dict[str, Any] = schema
    return loaded


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"top_k": 5}, {"top_k": {"requested": 5, "sent": 50}}),
        (
            {"top_k": 10, "evidence_limit": 5},
            {
                "top_k": {"requested": 10, "sent": 50},
                "evidence_limit": {"requested": 5, "sent": 50},
            },
        ),
        (
            {"top_k": 100, "evidence_limit": 100},
            {
                "evidence_limit": {"requested": 100, "sent": 50},
            },
        ),
        ({"top_k": 500}, {"top_k": {"requested": 500, "sent": 100}}),
        ({"top_k": 0}, {"top_k": {"requested": 0, "sent": 50}}),
        ({"top_k": 50, "evidence_limit": 50}, {}),
        ({"top_k": None}, {}),
        ({"query": "勤怠管理システム 担当部署"}, {}),
    ],
    ids=["small", "both-small", "over-max", "top-k-over-max", "zero", "default", "null", "none"],
)
def test_search_limits_follow_the_rag_contract(
    arguments: dict[str, Any], expected: dict[str, Any]
) -> None:
    # 既定と上限は RAG の契約から取る（#1403・#3240）。
    schema = _contract_schema("rag_retrieve_evidence")
    assert adjusted_search_limits(arguments, schema) == expected


def test_rag_search_evidence_limit_default_stays_at_the_contract_value() -> None:
    schema = _contract_schema("rag_search")
    assert adjusted_search_limits({"top_k": 5, "evidence_limit": 5}, schema) == {
        "top_k": {"requested": 5, "sent": 50},
        "evidence_limit": {"requested": 5, "sent": 12},
    }


def test_search_limits_use_the_top_k_default_of_the_contract() -> None:
    """件数の既定は契約の `default` から取り、RAG の内部の既定を写さない（#3240）。契約の既定が
    変われば、引き上げる値もそれに従う。"""
    schema = _contract_schema("rag_retrieve_evidence")
    default = schema["properties"]["top_k"]["default"]
    assert isinstance(default, int) and default > 5
    assert adjusted_search_limits({"top_k": 5}, schema) == {
        "top_k": {"requested": 5, "sent": default}
    }
    changed = json.loads(json.dumps(schema))
    changed["properties"]["top_k"]["default"] = 30
    assert adjusted_search_limits({"top_k": 5}, changed) == {"top_k": {"requested": 5, "sent": 30}}


def test_search_limit_schema_tells_the_model_to_omit_the_limits() -> None:
    schema = _contract_schema("rag_retrieve_evidence")
    shown = search_limit_schema("rag__rag_retrieve_evidence", schema)
    for name in ("top_k", "evidence_limit"):
        description = shown["properties"][name]["description"]
        assert description.endswith(SEARCH_LIMIT_SCHEMA_NOTE)
        assert "多段の質問では省略する" in description
    # 呼び先の契約（元の schema）は変えない。2 回通しても重ねない。
    assert SEARCH_LIMIT_SCHEMA_NOTE not in schema["properties"]["top_k"]["description"]
    assert search_limit_schema("rag__rag_retrieve_evidence", shown) == shown
    # 根拠のツール以外は元のまま。
    guides = _contract_schema("rag_lookup_guides")
    assert search_limit_schema("rag__rag_lookup_guides", guides) is guides


def _step(name: str, arguments: dict[str, Any], *, success: bool = True) -> RunStep:
    return RunStep(
        run_id="run-1",
        tool_call=ToolCall(name=name, arguments=arguments),
        tool_result=ToolResult(name=name, success=success, output={} if success else None),
    )


def test_repeated_query_compares_the_same_tool_and_scope_only() -> None:
    tool = "rag__rag_retrieve_evidence"
    arguments: dict[str, Any] = {"query": "勤怠管理システムの担当部署"}
    current = _step(tool, arguments)
    steps = [
        _step(tool, {"query": "勤怠管理システム 担当部署"}),
        # 失敗した呼び出し・別のツール・別のプロファイル・条件を足した呼び直しは数えない。
        _step(tool, {"query": "勤怠管理システム 担当部署"}, success=False),
        _step("rag__rag_search", {"query": "勤怠管理システム 担当部署"}),
        _step(tool, {"query": "勤怠管理システム 担当部署", "search_answer_profile_id": "p2"}),
        _step(tool, {"query": "勤怠管理システム 担当部署", "conditions": {"kind": "年間"}}),
        _step(tool, {"query": "総務部 承認者"}),
        current,
    ]
    note = repeated_query_note(tool, arguments, steps, current_step_id=current.id)
    assert note == {
        "count": 1,
        "similar_queries": ["勤怠管理システム 担当部署"],
        "next_step": REPEATED_QUERY_HINT,
    }
    # 件数（evidence_limit・top_k）だけが違う呼び出しも同じ範囲。空のリストは指定なしと同じ。
    assert repeated_query_note(
        tool,
        {"query": "総務部 承認者", "evidence_limit": 20, "knowledge_base_ids": []},
        steps,
        current_step_id=current.id,
    ) == {"count": 1, "similar_queries": ["総務部 承認者"], "next_step": REPEATED_QUERY_HINT}
    # 初めての query・読み取りのツールには付けない。
    assert (
        repeated_query_note(tool, {"query": "管理本部長 期限"}, steps, current_step_id=current.id)
        is None
    )
    assert (
        repeated_query_note(
            "rag__rag_read_document", {"query": "総務部 承認者"}, steps, current_step_id="x"
        )
        is None
    )


def test_query_with_superseded_versions_is_not_a_repeat() -> None:
    """旧版も含めた呼び直し（include_superseded: true。#1392）は繰り返しではない。"""
    tool = "rag__rag_search"
    query = "承認の期限 旧版との違い"
    current = _step(tool, {"query": query, "include_superseded": True})
    steps = [_step(tool, {"query": query}), current]
    assert (
        repeated_query_note(
            tool, {"query": query, "include_superseded": True}, steps, current_step_id=current.id
        )
        is None
    )
    # 既定の false は省いたときと同じ範囲。
    assert repeated_query_note(
        tool, {"query": query, "include_superseded": False}, steps, current_step_id="x"
    ) == {"count": 1, "similar_queries": [query], "next_step": REPEATED_QUERY_HINT}


def test_repeated_hop_query_gets_a_stop_hint_and_small_evidence_limit_is_raised(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "勤怠管理システム 担当部署", "evidence_limit": 5},
                call_id="call-1",
            )
        ],
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "勤怠管理システムの担当部署は？", "evidence_limit": 50},
                call_id="call-2",
            )
        ],
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "システム台帳 勤怠管理システム 担当部署"},
                call_id="call-3",
            )
        ],
        [assistant_message("担当部署は総務部です。")],
    )
    run = _run()

    # 既定より小さい evidence_limit は既定（RAG の契約の 50。#1365）に引き上げ、既定以上は変えない。
    sent = [
        call["arguments"].get("evidence_limit") for call in mcp.calls_of("rag_retrieve_evidence")
    ]
    assert sent == [50, 50, None]
    # step には送った値を残す。
    recorded = [step.tool_call.arguments for step in run.steps if step.tool_call is not None]
    assert recorded[0]["evidence_limit"] == 50

    assert "repeated_query" not in _tool_output(model, 1, "call-1")
    assert _tool_output(model, 1, "call-1")["adjusted_limits"]["evidence_limit"] == {
        "requested": 5,
        "sent": 50,
    }
    assert "adjusted_limits" not in _tool_output(model, 2, "call-2")
    assert _tool_output(model, 2, "call-2")["repeated_query"] == {
        "count": 1,
        "similar_queries": ["勤怠管理システム 担当部署"],
        "next_step": REPEATED_QUERY_HINT,
    }
    # 台帳を引く次の段は繰り返しではない。
    assert "repeated_query" not in _tool_output(model, 3, "call-3")
    # 記録する step の結果は RAG の結果のまま。
    outputs = [step.tool_result.output for step in run.steps if step.tool_result is not None]
    assert all("repeated_query" not in (output or {}) for output in outputs)


def test_small_top_k_is_raised_and_over_max_limits_are_clamped(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "システム台帳 販売管理システム エスカレーション先", "top_k": 5},
                call_id="call-1",
            )
        ],
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "第三日曜 停止 システム", "top_k": 100, "evidence_limit": 100},
                call_id="call-2",
            )
        ],
        [
            function_call(
                "rag__rag_search",
                {"query": "注文の承認者 期限", "top_k": 10},
                call_id="call-3",
            )
        ],
        [assistant_message("エスカレーション先は営業部です。")],
    )
    run = _run()

    # 既定（50）より小さい top_k は既定に引き上げ、上限（evidence_limit 50）を超える値は上限に
    # 丸める。呼び先の入力検証（fake は契約の上限で検証する）で失敗させない（#1403）。
    sent = [call["arguments"] for call in mcp.calls_of("rag_retrieve_evidence")]
    assert [(args.get("top_k"), args.get("evidence_limit")) for args in sent] == [
        (50, None),
        (100, 50),
    ]
    [searched] = mcp.calls_of("rag_search")
    assert searched["arguments"]["top_k"] == 50
    assert all(step.tool_result is not None and step.tool_result.success for step in run.steps)
    # step には送った値を残す。
    recorded = [step.tool_call.arguments for step in run.steps if step.tool_call is not None]
    assert recorded[0]["top_k"] == 50
    assert recorded[1]["evidence_limit"] == 50
    # モデルへの結果に、直した値と次から省略する案内を残す（記録する step の結果は RAG のまま）。
    assert _tool_output(model, 1, "call-1")["adjusted_limits"] == {
        "top_k": {"requested": 5, "sent": 50},
        "next_step": ADJUSTED_LIMITS_HINT,
    }
    assert _tool_output(model, 2, "call-2")["adjusted_limits"] == {
        "evidence_limit": {"requested": 100, "sent": 50},
        "next_step": ADJUSTED_LIMITS_HINT,
    }
    assert _tool_output(model, 3, "call-3")["adjusted_limits"]["top_k"] == {
        "requested": 10,
        "sent": 50,
    }
    outputs = [step.tool_result.output for step in run.steps if step.tool_result is not None]
    assert all("adjusted_limits" not in (output or {}) for output in outputs)
    # モデルに見せる schema の件数の説明に「多段の質問では省略する」を足す。
    tools = {tool.name: tool for tool in model.calls[0].tools if isinstance(tool, FunctionTool)}
    for name in ("rag__rag_retrieve_evidence", "rag__rag_search"):
        properties = tools[name].params_json_schema["properties"]
        assert SEARCH_LIMIT_SCHEMA_NOTE in properties["top_k"]["description"]
        assert SEARCH_LIMIT_SCHEMA_NOTE in properties["evidence_limit"]["description"]


def test_business_rag_research_instructions_cover_hop_queries() -> None:
    skill = skill_registry.get("business-rag-research")
    assert skill is not None
    # 識別子は Markdown の `code`（#1795）。規則の文は記号を除いて確かめる。
    instructions = skill.instructions.replace("`", "")
    # 段の query は実体と属性だけ・件数は既定より小さくしない・言い換えは 2 回まで・
    # 台帳を示されたらその実体で台帳を引く（#1351）。Runtime の案内（record_codes・repeated_query・
    # superseded_versions・section_reads）は、それぞれ結果の中の next_step に次の手を持つので、
    # 本文は「next_step に従う」の 1 か所だけにする（#2269。契約は test_skill_md_spec.py）。
    for phrase in (
        "実体（前の段で分かった正式名・略号・ID・役職名を根拠の表記のまま）と引く属性だけ",
        "件数（top_k・evidence_limit）は既定より小さくしない（多段の質問では省略する）",
        "1 回目の結果だけで答えず、その意味を次の段で引く",
        "言い換えて 2 回引いても根拠が出なければ",
        "その台帳・一覧の名前と実体と属性で次の段を引く",
        "組み込みの Agent では、ツールの結果（とその中の案内）に next_step（次にすること）が"
        "付くことがある。付いたらそれに従う。",
        "rag_calls_remaining",
        "references",
        # 旧版・変更点を尋ねる質問は旧版も検索する（#1392）。
        "include_superseded: true",
        # 旧版の年度を名指しする質問の例（#1405）。
        "「2025 年度と 2026 年度では」",
        "older_versions",
        # 台帳の略称・別表記で書かれたほかの資料の記載も同じ対象として使う（#1958）。
        "ほかの資料はその名前で書くことがある",
    ):
        assert phrase in instructions, phrase


def test_business_rag_research_instructions_do_not_report_the_work() -> None:
    """回答に作業の経過（ツール名・検索の状態・業務ガイドの有無・旧版の有無）を書かない（#1452）。

    #1362 の評価で、これらの段落が最終の検証で「根拠で確かめられない」になって外れ、資料の答えは
    正しいのに回答が conditional になっていた（`da-regional-report-export` の「業務ガイドは
    見つかりませんでした」、`km-license-fee` の「rag_search の結果は insufficient_evidence」、
    `ir-emergency-change` の「旧版もあることが確認できました」、`br-crm-retention` の
    「確かめられなかった点はありません」）。"""
    skill = skill_registry.get("business-rag-research")
    assert skill is not None
    # 識別子は Markdown の `code`（#1795）。規則の文は記号を除いて確かめる。
    instructions = skill.instructions.replace("`", "")
    for phrase in (
        "回答には、資料の内容と、資料で確かめられなかった点だけを書く。",
        "ツールの名前（rag_retrieve_evidence・rag_outline など）",
        "ツールの結果の項目の値（role・origin など）",
        "業務ガイドが見つかった・見つからなかったこと",
        "検索の結果に旧版（older_versions）があったこと",
        "自分の作業の経過は回答に書かない",
        "「確かめられなかった点はありません」のような作業の報告",
        # 旧版を比べる質問で旧版の内容を書くのは除く。
        "旧版・改定前との違いを尋ねる質問で、旧版の内容を書くのは除く。",
    ):
        assert phrase in instructions, phrase


def test_business_rag_research_instructions_do_not_add_absence_or_exhaustive_supplements() -> None:
    """質問が求めない不在・網羅の補足を書かない（#1470）。

    e1467-deepseek の D で、質問に答えた後の補足の「…は SYS-107 の 1 件だけでした」
    （`tl-it-department-system`）・「A かつ極秘のものはこの 2 件だけです」
    （`tl-severity-a-top-secret`）・「入出荷管理システム固有の別頻度は見当たりませんでした」
    （`br-shipping-backup`）などの 8 段落が、渡された根拠の範囲で不在・網羅を確かめられず
    unsupported になり、結論は正しいのに回答が conditional になっていた。"""
    skill = skill_registry.get("business-rag-research")
    assert skill is not None
    # 識別子は Markdown の `code`（#1795）。規則の文は記号を除いて確かめる。
    instructions = skill.instructions.replace("`", "")
    for phrase in (
        "質問が求めない限り、資料に無いこと",
        "ほかに無いこと・全件で何件だけ",
        "を補足に書かない",
        "無いと断定せず、確かめられなかった点として書く。",
        # 全件を求める質問は、読んだ範囲を結論に書き、補足で繰り返さない。
        "読んだ範囲（例: 台帳の SYS-101〜SYS-180 の 80 行を読んだ）を結論に書き、",
        "補足で繰り返さない。",
    ):
        assert phrase in instructions, phrase
    # 資料で確かめられなかった点を書く決まり（#1452）と矛盾させない（その文の後に置く）。
    assert instructions.index("資料で確かめられなかった点だけを書く。") < instructions.index(
        "質問が求めない限り、資料に無いこと"
    )


# ---- 台帳の行の略号・区分（#1365） --------------------------------------------------------------

CONTRACTS = Path(__file__).resolve().parents[3] / "platform/contracts/mcp"


def _record(excerpt: str, chunk_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "document_id": "doc-ledger",
        "file_name": "system-ledger.xlsx",
        "chunk_id": chunk_id,
        "content_kind": "record",
        "excerpt": excerpt,
        **extra,
    }


# #1335 の再評価の D の形: 「経費精算ポータル 担当部署」の 1 回目の結果に、台帳の行（担当部署が
# 略号の「経」）と別のシステムの行、略号の表を指さない規程の本文が並ぶ。
_LEDGER_EVIDENCE: dict[str, Any] = {
    "evidence": [
        {
            "document_id": "doc-guide",
            "chunk_id": "guide-1",
            "content_kind": "text",
            "excerpt": "変更の承認は担当部署: 経 の承認者が行う。",
        },
        _record(
            "システムID: SYS-102 / 正式名: 勤怠管理システム / 担当部署: 人 / 重要度: A",
            "ledger-102",
        ),
        _record(
            "システムID: SYS-101 / 正式名: 経費精算ポータル / 略称・別表記: 経費 Portal"
            " / 担当部署: 経 / 重要度: B / 機密区分: 社外秘 / 保管年数: 7",
            "ledger-101",
        ),
    ]
}


def test_record_codes_point_to_short_values_of_the_matching_row() -> None:
    note = record_codes(_LEDGER_EVIDENCE, "経費精算ポータル 担当部署", [])
    assert note == {
        "values": [
            {
                "field": "担当部署",
                "value": "経",
                "record_of": "経費精算ポータル",
                "document_id": "doc-ledger",
                "file_name": "system-ledger.xlsx",
                "chunk_id": "ledger-101",
            },
            {
                "field": "重要度",
                "value": "B",
                "record_of": "経費精算ポータル",
                "document_id": "doc-ledger",
                "file_name": "system-ledger.xlsx",
                "chunk_id": "ledger-101",
            },
        ],
        "next_step": RECORD_CODE_HINT,
    }
    # 略称・全角の表記でも同じ行に当たる。数字だけの値・長い値は略号ではない。
    by_alias = record_codes(_LEDGER_EVIDENCE, "経費 Ｐｏｒｔａｌ 担当部署", [])
    assert by_alias is not None
    assert [(item["field"], item["value"]) for item in by_alias["values"]] == [
        ("担当部署", "経"),
        ("重要度", "B"),
    ]


def test_record_codes_skip_values_already_searched_and_unrelated_rows() -> None:
    # この Run で「担当部署 経」を引いた後は、その値を案内しない。
    note = record_codes(_LEDGER_EVIDENCE, "経費精算ポータル 担当部署", ["担当部署 経 正式名"])
    assert note is not None
    assert [item["value"] for item in note["values"]] == ["B"]
    # query の実体に当たる行が無い・記録の無い結果・query の無い呼び出しは案内しない。
    assert record_codes(_LEDGER_EVIDENCE, "予算管理システム 担当部署", []) is None
    assert (
        record_codes({"evidence": _LEDGER_EVIDENCE["evidence"][:1]}, "経費精算ポータル", []) is None
    )
    assert record_codes(_LEDGER_EVIDENCE, "", []) is None
    assert record_codes({}, "経費精算ポータル", []) is None


def test_record_codes_read_multi_row_headers_and_sheet_rows_without_kind() -> None:
    # 複数行の表頭（「 / 」でつないだ列名）と、種類の無い表計算の 1 行の根拠。
    row = {
        "document_id": "doc-ledger",
        "chunk_id": "row-5",
        "excerpt": "システム名: 予算管理システム / 区分 / 重要度: S / 担当部署: 企",
        "locator": {"sheet_name": "台帳", "row_start": 5, "row_end": 5},
    }
    note = record_codes({"evidence": [row]}, "予算管理システム 重要度", [])
    assert note is not None
    assert [(item["field"], item["value"]) for item in note["values"]] == [
        ("区分 / 重要度", "S"),
        ("担当部署", "企"),
    ]
    # 手順書の手順（1 行に 1 つの「列名: 値」）も同じに読む。
    step = _record("手順名: 予算管理システムの再起動\n担当: 企\n所要時間: 30", "step-1")
    steps_note = record_codes({"evidence": [step]}, "予算管理システムの再起動 担当", [])
    assert steps_note is not None
    assert [(item["field"], item["value"]) for item in steps_note["values"]] == [("担当", "企")]
    # 複数の行の範囲（表のかたまり）は 1 行の記録ではない。
    block = {**row, "locator": {"sheet_name": "台帳", "row_start": 5, "row_end": 9}}
    assert record_codes({"evidence": [block]}, "予算管理システム 重要度", []) is None


def test_record_codes_are_bounded() -> None:
    excerpt = "正式名: 経費精算ポータル / " + " / ".join(f"項目{n}: {n}a" for n in range(10))
    note = record_codes({"evidence": [_record(excerpt, "wide")]}, "経費精算ポータル", [])
    assert note is not None
    assert len(note["values"]) == MAX_RECORD_CODES


def test_ledger_row_with_codes_gets_a_next_hop_hint(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    output["evidence"] = deepcopy(_LEDGER_EVIDENCE["evidence"])
    mcp.outputs["rag_retrieve_evidence"] = output
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "経費精算ポータル 担当部署"}, call_id="c1"
            )
        ],
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "担当部署 経 正式名"}, call_id="c2"
            )
        ],
        [assistant_message("担当部署は経理部です。")],
    )
    run = _run()

    first = _tool_output(model, 1, "c1")
    assert [item["value"] for item in first["record_codes"]["values"]] == ["経", "B"]
    assert first["record_codes"]["next_step"] == RECORD_CODE_HINT
    # 略号を引く次の段（「担当部署 経 正式名」）は台帳の行の実体に当たらないので案内しない。
    second = _tool_output(model, 2, "c2")
    assert "record_codes" not in second
    # 記録する step の結果は RAG の結果のまま。
    recorded = [step.tool_result.output for step in run.steps if step.tool_result is not None]
    assert all("record_codes" not in (output or {}) for output in recorded)


# ---- 引いていない段の差し戻し（#1958） ----------------------------------------------------------

# #1865 の base の形: 「ID管理システム 障害 連絡」の結果に、障害連絡規程（「担当部署の承認者」）と
# 台帳の行（担当部署が略号の「安」）が並び、略号の表は「人」の行だけ。保守計画は略称「ＨＲＭ」
# で書く。
_HOP_EVIDENCE: list[dict[str, Any]] = [
    {
        "document_id": "doc-incident",
        "file_name": "incident-contact-rules.pdf",
        "chunk_id": "incident-1",
        "content_kind": "text",
        "excerpt": "重要度 A: 検知から 30 分以内に、担当部署の承認者と情シス部長に連絡します。",
    },
    {
        "document_id": "doc-plan",
        "file_name": "maintenance-plan.pdf",
        "chunk_id": "plan-1",
        "content_kind": "text",
        "excerpt": "ＨＲＭ: 毎月第 2 土曜日の 22:00〜翌 2:00",
    },
    {
        "document_id": "doc-org",
        "file_name": "organization-rules.pdf",
        "chunk_id": "org-2",
        "content_kind": "text",
        "excerpt": "略号「人」: 人事部。管理本部に属します。",
    },
    _record(
        "システムID: SYS-139 / 正式名: ID管理システム / 略称・別表記: IDM"
        " / 担当部署: 安 / 重要度: A",
        "ledger-139",
    ),
    _record(
        "システムID: SYS-103 / 正式名: 人事評価システム / 略称・別表記: HRM"
        " / 担当部署: 人 / 重要度: A",
        "ledger-103",
    ),
]


def _hop_steps(*queries: str, evidence: list[dict[str, Any]] | None = None) -> list[RunStep]:
    return [
        RunStep(
            id=f"step-{index}",
            run_id="run-1958",
            title="根拠",
            tool_call=ToolCall(
                name="rag__rag_retrieve_evidence", arguments={"query": query}, trace_id=f"t{index}"
            ),
            tool_result=ToolResult(
                name="rag__rag_retrieve_evidence",
                success=True,
                output={"evidence": deepcopy(evidence or _HOP_EVIDENCE)},
            ),
        )
        for index, query in enumerate(queries)
    ]


def test_unresolved_hops_find_codes_and_aliases_left_unsearched() -> None:
    # 略号「安」は検索にも資料の本文にも意味が無い。重要度 A は規程の本文（「重要度 A:」）に出る。
    steps = _hop_steps("ID管理システム 障害 連絡")
    pending = unresolved_hops(
        steps, "30 分以内に安部の承認者へ連絡します。", "ID管理システムで障害が起きたら？"
    )
    assert [(item["kind"], item["field"], item["value"]) for item in pending] == [
        ("code", "担当部署", "安")
    ]
    message = unresolved_hops_message(pending)
    assert message.startswith(UNRESOLVED_HOPS_HINT)
    assert "ID管理システム の「担当部署 安」（system-ledger.xlsx）" in message
    # 略称「HRM」はほかの資料の本文（全角の「ＨＲＭ」）に出ているのに、回答に無い。
    hr = unresolved_hops(
        _hop_steps("人事評価システム 本番 変更"), "人事評価システムの変更は毎月最終金曜日です。"
    )
    assert [(item["kind"], item["value"]) for item in hr] == [("alias", "HRM")]


def test_unresolved_hops_are_empty_once_the_next_hop_is_taken() -> None:
    # 次の段（「担当部署 安」）を引いた・回答が略称を使った・資料に略号の意味が出た。
    assert unresolved_hops(_hop_steps("ID管理システム 障害 連絡", "担当部署 安 承認者"), "") == []
    assert unresolved_hops(_hop_steps("人事評価システム 本番 変更"), "HRM の保守枠") == []
    glossary = {
        "document_id": "doc-org",
        "file_name": "organization-rules.pdf",
        "chunk_id": "org-1",
        "content_kind": "text",
        "excerpt": "略号「安」: 情報セキュリティ部。",
    }
    steps = _hop_steps("ID管理システム 障害 連絡", evidence=[*_HOP_EVIDENCE, glossary])
    assert unresolved_hops(steps, "情報セキュリティ部長") == []
    # 記録に当たらない検索・根拠の無い Run は対象外。
    assert unresolved_hops(_hop_steps("予算管理システム"), "") == []
    assert unresolved_hops([], "") == []


def test_answer_with_an_unresolved_code_is_sent_back_once(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    output["evidence"] = deepcopy(_HOP_EVIDENCE)
    mcp.outputs["rag_retrieve_evidence"] = output
    first = "30 分以内に安部の承認者へ連絡します。"
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "ID管理システム 障害 連絡"}, call_id="c1"
            )
        ],
        [assistant_message(first)],
        [function_call("rag__rag_retrieve_evidence", {"query": "担当部署 安"}, call_id="c2")],
        [assistant_message("30 分以内に情報セキュリティ部長へ連絡します。")],
    )

    run = _run("ＩＤ管理システムで障害が起きたら、何分以内に誰へ連絡しますか？")

    assert len(model.calls) == 4
    users = [item for item in model.calls[2].input if isinstance(item, dict)]
    retry = [item for item in users if item.get("role") == "user"][-1]["content"]
    assert retry.startswith(UNRESOLVED_HOPS_HINT)
    assert "「担当部署 安」" in retry
    assert first in retry
    assert len(mcp.calls_of("rag_retrieve_evidence")) == 2
    answer = [item for item in run.artifacts if item.kind == "answer"][-1].content["text"]
    assert answer == "30 分以内に情報セキュリティ部長へ連絡します。"
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1}
    events = [
        event.payload["tool_grounding"] for event in run.events if "tool_grounding" in event.payload
    ]
    assert events == [{"action": "hop_retry", "original_answer": first}]


def test_unresolved_code_is_not_sent_back_twice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    output["evidence"] = deepcopy(_HOP_EVIDENCE)
    mcp.outputs["rag_retrieve_evidence"] = output
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "ID管理システム 障害 連絡"}, call_id="c1"
            )
        ],
        [assistant_message("安部の承認者です。")],
        [assistant_message("担当部署（安）の承認者です。")],
    )

    run = _run("ＩＤ管理システムで障害が起きたら、誰へ連絡しますか？")

    # 差し戻しは 1 回だけ。差し戻しの後は引かなくてもそのまま出す（拒答にはしない）。
    assert len(model.calls) == 3
    answer = [item for item in run.artifacts if item.kind == "answer"][-1].content["text"]
    assert answer == "担当部署（安）の承認者です。"


# ---- 差し戻す値を回答が使う値に絞る（#1978） ----------------------------------------------------

# #1865 の測り直しの形: 台帳の行には質問に関係しない列（重要度・機密区分）もある。
_SCOPE_EVIDENCE: list[dict[str, Any]] = [
    {
        "document_id": "doc-plan",
        "file_name": "maintenance-plan.pdf",
        "chunk_id": "plan-1",
        "content_kind": "text",
        "excerpt": "ＨＲＭ: 毎月第 2 土曜日の 22:00〜翌 2:00 ＭＥＳ: 毎月第 3 日曜日の 2:00〜6:00",
        "locator": {"section_path": ["定期保守計画 2026年度"]},
    },
    {
        "document_id": "doc-org",
        "file_name": "organization-rules.pdf",
        "chunk_id": "org-2",
        "content_kind": "text",
        "excerpt": "略号「製」: 製造部。生産本部に属します。",
    },
    _record(
        "システムID: SYS-103 / 正式名: 人事評価システム / 略称・別表記: HRM"
        " / 担当部署: 人 / 重要度: A / 機密区分: 極秘",
        "ledger-103",
    ),
    _record(
        "システムID: SYS-102 / 正式名: 勤怠管理システム / 略称・別表記: 勤怠"
        " / 担当部署: 人 / 重要度: B / 機密区分: 社外秘",
        "ledger-102",
    ),
    _record(
        "システムID: SYS-124 / 正式名: 製造実行システム / 略称・別表記: MES"
        " / 担当部署: 製 / 重要度: A / 機密区分: 社外秘",
        "ledger-124",
    ),
    _record(
        "システムID: SYS-145 / 正式名: 中期計画管理システム / 略称・別表記: 中計"
        " / 担当部署: 企 / 重要度: C / 機密区分: 極秘",
        "ledger-145",
    ),
]


def _scope_steps(*queries: str, search_answer: str | None = None) -> list[RunStep]:
    steps = _hop_steps(*queries, evidence=_SCOPE_EVIDENCE)
    if search_answer is not None:
        output = {"answer": search_answer, "evidence": deepcopy(_SCOPE_EVIDENCE)}
        steps.append(
            RunStep(
                id="step-search",
                run_id="run-1978",
                title="検索",
                tool_call=ToolCall(
                    name="rag__rag_search", arguments={"query": queries[0]}, trace_id="ts"
                ),
                tool_result=ToolResult(name="rag__rag_search", success=True, output=output),
            )
        )
    return steps


def test_unresolved_hops_skip_codes_the_answer_does_not_use() -> None:
    # 「人事評価システムの本番の変更は…」: 回答は HRM の保守枠で答え、機密区分・重要度を使わない。
    steps = _scope_steps("人事評価システム 本番 変更")
    answer = "人事評価システム（HRM）の変更は、毎月第 2 土曜日の 22:00〜翌 2:00 にだけ作業します。"
    assert (
        unresolved_hops(steps, answer, "人事評価システムの本番の変更は、いつ作業できますか？") == []
    )
    # 回答が重要度（半角の 1 文字は語として）を使えば、意味を引いていない値として挙げる。
    used = f"人事評価システムは重要度 A です。{answer}"
    assert [(item["field"], item["value"]) for item in unresolved_hops(steps, used)] == [
        ("重要度", "A")
    ]
    # 「SYS-103」「HRM」の中の英字は重要度の値ではない。
    assert unresolved_hops(steps, f"SYS-103 の {answer}") == []


def test_unresolved_hops_skip_records_the_answer_does_not_name() -> None:
    # 「毎月第 3 日曜日に保守するシステム…」: 検索で別のシステム（中期計画管理システム）の行にも
    # 当たったが、回答は製造実行システム（MES・製）で答えた。答えに使わない行の値は挙げない。
    steps = _scope_steps("MES 正式名", "中期計画管理システム 正式名", "担当部署 製 正式名")
    answer = "第 3 日曜日に保守するのは製造実行システム（MES）で、担当部署は製造部です。"
    assert unresolved_hops(steps, answer, "毎月第 3 日曜日に保守するシステムは？") == []


def test_unresolved_hops_skip_aliases_in_documents_the_answer_does_not_use() -> None:
    # 「人事評価システムと勤怠管理システムでは…保管しますか？」: 根拠の一覧は件数で切られ、保管の
    # 規程は rag_search の回答の文にだけ出る。保守計画（ＨＲＭ）は回答が使っていない資料。
    steps = _scope_steps(
        "人事評価システム 勤怠管理システム 保管 期間",
        search_answer=(
            "人事評価システムが長く保管されます。\n"
            "・極秘のデータは10年間、社外秘のデータは7年間保管すると定められています。\n"
            "・「システムID: SYS-103 / 正式名: 人事評価システム / 機密区分: 極秘」"
        ),
    )
    answer = (
        "人事評価システムは極秘で 10 年、勤怠管理システムは社外秘で 7 年保管します"
        "（data-retention-rules.pdf・system-ledger.xlsx）。"
    )
    question = "人事評価システムと勤怠管理システムでは、どちらのデータを長く保管しますか？"
    assert unresolved_hops(steps, answer, question) == []
    # 回答が資料の名前を挙げなければ、どの資料の記載も候補にする（#1958 の判定のまま）。
    bare = "人事評価システムは極秘で 10 年、勤怠管理システムは社外秘で 7 年保管します。"
    assert [item["value"] for item in unresolved_hops(steps, bare, question)] == ["HRM"]
    # 保守計画を回答が挙げたなら、その資料の略称の記載を結び付けていない。
    cited = "人事評価システムの変更は定期保守計画の共通の保守枠です（system-ledger.xlsx）。"
    assert [item["value"] for item in unresolved_hops(steps, cited, question)] == ["HRM"]


def test_unresolved_hops_skip_codes_the_question_names() -> None:
    # 「重要度が A で、機密区分が極秘のシステム…」: 質問が条件として挙げた値は引く段ではない。
    steps = _scope_steps("重要度 A 機密区分 極秘 システム", "人事評価システム", "製造実行システム")
    answer = "人事評価システム（HRM。重要度 A・極秘）が当たります。"
    assert unresolved_hops(steps, answer, "重要度が A で、機密区分が極秘のシステムは？") == []
    # 質問に無い値（重要度 A）は、回答が使えば挙げる。
    steps = _scope_steps("極秘 システム", "人事評価システム")
    assert [item["value"] for item in unresolved_hops(steps, answer, "極秘のシステムは？")] == ["A"]


def _validation_by_answer(bad: str) -> Any:
    """`bad` の回答の段落だけを根拠で確かめられないとする `rag_validate_answer`。"""

    def validate(argument: Any) -> dict[str, Any]:
        output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
        status = "unsupported" if argument.answer == bad else "supported"
        output.update(
            valid=status == "supported",
            counts={
                "supported": int(status == "supported"),
                "unsupported": int(status != "supported"),
            },
            claims=[{"answer_quote": argument.answer, "status": status, "chunk_id": "incident-1"}],
        )
        return output

    return validate


def _hop_retry_run(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, first: str, second: str
) -> RunState:
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    output["evidence"] = deepcopy(_HOP_EVIDENCE)
    mcp.outputs["rag_retrieve_evidence"] = output
    _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "ID管理システム 障害 連絡"}, call_id="c1"
            )
        ],
        [assistant_message(first)],
        [assistant_message(second)],
    )
    return _run("ＩＤ管理システムで障害が起きたら、何分以内に誰へ連絡しますか？")


def test_worse_answer_after_the_hop_retry_falls_back_to_the_first_answer(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    first = "30 分以内に安部の承認者へ連絡します。"
    second = "60 分以内に総務部へ連絡します。"
    mcp.outputs["rag_validate_answer"] = _validation_by_answer(second)

    run = _hop_retry_run(monkeypatch, mcp, first, second)

    # 差し戻しの後の回答の段落を検証が外した: 前の回答も検証し、外した内容が軽い前の回答を使う。
    assert [call["arguments"]["answer"] for call in mcp.calls_of("rag_validate_answer")] == [
        second,
        first,
    ]
    answer = [item for item in run.artifacts if item.kind == "answer"][-1].content["text"]
    assert answer == first
    [validation] = [item for item in run.artifacts if item.kind == "answer_validation"]
    assert validation.content["valid"] is True
    events = [
        event.payload["tool_grounding"] for event in run.events if "tool_grounding" in event.payload
    ]
    assert events == [
        {"action": "hop_retry", "original_answer": first},
        {"action": "hop_reverted", "original_answer": second},
    ]
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1, "hop_reverted": True}


def test_answer_after_the_hop_retry_is_kept_unless_the_first_was_better(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    first = "30 分以内に安部の承認者へ連絡します。"
    second = "30 分以内に情報セキュリティ部長へ連絡します。"
    # 差し戻しの後の回答を検証が通した: 前の回答は検証しない。
    mcp.outputs["rag_validate_answer"] = _validation_by_answer(first)
    run = _hop_retry_run(monkeypatch, mcp, first, second)
    assert [call["arguments"]["answer"] for call in mcp.calls_of("rag_validate_answer")] == [second]
    assert [item for item in run.artifacts if item.kind == "answer"][-1].content["text"] == second


def test_answer_after_the_hop_retry_is_kept_when_both_are_withheld(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    first = "30 分以内に安部の承認者へ連絡します。"
    second = "30 分以内に安部長へ連絡します。"

    def validate(argument: Any) -> dict[str, Any]:
        output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
        output.update(
            valid=False,
            counts={"supported": 0, "unsupported": 1},
            claims=[{"answer_quote": argument.answer, "status": "unsupported"}],
        )
        return output

    mcp.outputs["rag_validate_answer"] = validate
    run = _hop_retry_run(monkeypatch, mcp, first, second)

    # どちらもすべて外した（前の回答も良くない）: 差し戻しの後の回答とその検証のまま。
    assert len(mcp.calls_of("rag_validate_answer")) == 2
    [validation] = [item for item in run.artifacts if item.kind == "answer_validation"]
    assert validation.content["result"]["claims"][0]["answer_quote"] == second
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1}


def test_hop_retry_compares_removed_then_unverified_then_warnings() -> None:
    """差し戻しの前後の回答は、矛盾で外した段落 → 未確認の段落 → 警告（印と回答の警告）の数の順に
    比べる（#2738）。判定が出なかった検証は比べない。"""

    def content(removed: int, unverified: int, warnings: int, answers: int = 0) -> dict[str, Any]:
        return {
            "status": "completed",
            "withheld": {"removed": removed, "unverified": unverified, "warnings": warnings},
            "answer_warnings": [{"code": "requests"}] * answers,
        }

    def rank(validation: dict[str, Any]) -> tuple[int, int, int]:
        value = builtin_runtime._withheld_rank(validation)  # noqa: SLF001 - 比べ方の単体の確認
        assert value is not None
        return value

    assert rank(content(0, 5, 5)) < rank(content(1, 0, 0))
    assert rank(content(0, 1, 0)) > rank(content(0, 0, 9))
    assert rank(content(0, 0, 1, answers=1)) == (0, 0, 2)
    assert builtin_runtime._withheld_rank({"status": "unvalidated"}) is None  # noqa: SLF001


def test_fake_rag_evidence_limit_defaults_match_the_contract() -> None:
    """fake の既定の件数は RAG の契約と同じ（rag_retrieve_evidence 50・rag_search 12。#1365）。"""
    contract = json.loads((CONTRACTS / "rag-tools.json").read_text(encoding="utf-8"))
    defaults = {
        tool["name"]: tool["inputSchema"]["properties"]["evidence_limit"]["default"]
        for tool in contract["tools"]
        if tool["name"] in {"rag_search", "rag_retrieve_evidence"}
    }
    assert defaults == {
        "rag_search": RagSearchIn.model_fields["evidence_limit"].default,
        "rag_retrieve_evidence": RagRetrieveEvidenceIn.model_fields["evidence_limit"].default,
    }
    assert defaults == {"rag_search": 12, "rag_retrieve_evidence": 50}


def test_fake_rag_search_inputs_match_the_contract() -> None:
    """fake の検索の引数は RAG の契約と同じ（旧版の指定は真偽値の include_superseded。#1392）。"""
    contract = json.loads((CONTRACTS / "rag-tools.json").read_text(encoding="utf-8"))
    tools = {tool["name"]: tool["inputSchema"] for tool in contract["tools"]}
    for name, model in (
        ("rag_search", RagSearchIn),
        ("rag_retrieve_evidence", RagRetrieveEvidenceIn),
    ):
        schema = tools[name]
        assert set(schema["properties"]) == set(model.model_fields), name
        assert schema["properties"]["include_superseded"] == {
            **schema["properties"]["include_superseded"],
            "type": "boolean",
            "default": False,
        }
        assert model.model_fields["include_superseded"].default is False


# ---- 旧版の年度・版を名指しする質問（#1405） ----------------------------------------------------

_PLAN_2025 = {
    "document_id": "plan-2025",
    "file_name": "maintenance-plan-2025.pdf",
    "title": "定期保守計画 2025年度",
    "superseded_by_document_id": "plan",
}
_RULES_2023 = {
    "document_id": "rules-2023",
    "file_name": "approval-rules-2023.pdf",
    "title": "承認規程（2023年度版）",
    "superseded_by_document_id": "rules",
}
VERSION_GOAL = "定期保守計画の 2025 年度と 2026 年度では、ＨＲＭ の保守枠はどう変わりましたか？"


def _older(*versions: dict[str, Any]) -> dict[str, Any]:
    return {"evidence": [], "older_versions": [dict(version) for version in versions]}


def test_superseded_versions_note_matches_the_named_year_or_edition() -> None:
    """質問の年度・版が旧版のタイトル・文書名に当たるときだけ、旧版の検索し直しを勧める。"""
    output = _older(_PLAN_2025, _RULES_2023)
    query = {"query": "定期保守計画 ＨＲＭ 保守枠"}
    note = superseded_versions_note(query, output, [VERSION_GOAL, query["query"]])
    # 2026 年度（今の版）は旧版に当たらない。2023 年度版の規程は質問に無い。
    assert note == {
        "versions": [{**_PLAN_2025, "matched": ["2025"]}],
        "next_step": SUPERSEDED_VERSION_HINT,
    }
    # 旧版も検索した呼び出し・旧版の無い結果には出さない。
    assert (
        superseded_versions_note({**query, "include_superseded": True}, output, [VERSION_GOAL])
        is None
    )
    assert superseded_versions_note(query, _older(), [VERSION_GOAL]) is None
    assert superseded_versions_note(query, {"evidence": []}, [VERSION_GOAL]) is None
    # 今の版を尋ねる質問（年度・版を名指ししない・今の版の年度だけ）には出さない。
    for question in ("ＨＲＭ の保守枠はいつですか？", "2026 年度の ＨＲＭ の保守枠は？"):
        assert superseded_versions_note(query, output, [question]) is None, question
    # 全角の数字・文書名だけの年・和暦・第 N 版も当たる。
    title_less = {**_PLAN_2025, "title": None}
    note = superseded_versions_note(query, _older(title_less), ["２０２５年度の保守枠は？"])
    assert note is not None
    assert note["versions"][0]["matched"] == ["2025"]
    reiwa = {**_RULES_2023, "title": "承認規程（令和5年度版）"}
    note = superseded_versions_note(query, _older(reiwa), ["令和 5 年度版の承認規程の期限は？"])
    assert note is not None
    assert note["versions"][0]["matched"] == ["令和5年"]
    edition = {**_RULES_2023, "title": "承認規程 第2版"}
    note = superseded_versions_note(query, _older(edition), ["承認規程の第 2 版との違いは？"])
    assert note is not None
    assert note["versions"][0]["matched"] == ["第2版"]
    # 年の数字の一部（20250 など）は当たらない。
    other = {**_PLAN_2025, "title": "保守計画 20250", "file_name": "plan.pdf"}
    assert superseded_versions_note(query, _older(other), ["2025 年度の保守枠は？"]) is None
    # 旧版・改定前を尋ねる質問は、年度を名指ししなくても当たった文書の旧版を全部挙げる。
    note = superseded_versions_note(query, output, ["承認の期限は改定前と比べてどう変わった？"])
    assert note is not None
    assert [version["matched"] for version in note["versions"]] == [["旧版"], ["旧版"]]


def test_named_old_year_gets_a_superseded_search_hint(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """旧版の年度を名指しする質問で旧版を検索しなければ、旧版も含めた検索し直しを勧める。"""

    def evidence(argument: Any) -> dict[str, Any]:
        output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
        # RAG は旧版を検索しなかったときだけ、当たった文書の旧版を返す。
        if not argument.include_superseded:
            output["older_versions"] = [dict(_PLAN_2025)]
        return output

    mcp.outputs["rag_retrieve_evidence"] = evidence
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "定期保守計画 ＨＲＭ 保守枠"},
                call_id="call-1",
            )
        ],
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "定期保守計画 2025年度 ＨＲＭ 保守枠", "include_superseded": True},
                call_id="call-2",
            )
        ],
        [assistant_message("2025 年度は第 1 土曜日、2026 年度は第 2 土曜日です。")],
    )
    run = _run(VERSION_GOAL)

    assert _tool_output(model, 1, "call-1")["superseded_versions"] == {
        "versions": [{**_PLAN_2025, "matched": ["2025"]}],
        "next_step": SUPERSEDED_VERSION_HINT,
    }
    assert "superseded_versions" not in _tool_output(model, 2, "call-2")
    assert [
        call["arguments"].get("include_superseded")
        for call in mcp.calls_of("rag_retrieve_evidence")
    ] == [None, True]
    # 記録する step の結果は RAG の結果のまま。
    outputs = [step.tool_result.output for step in run.steps if step.tool_result is not None]
    assert all("superseded_versions" not in (output or {}) for output in outputs)


def test_fake_rag_evidence_outputs_match_the_contract() -> None:
    """fake の根拠の結果の項目は RAG の契約の出力にある（旧版の older_versions を含む。#1405）。"""
    contract = json.loads((CONTRACTS / "rag-tools.json").read_text(encoding="utf-8"))
    tools = {tool["name"]: tool["outputSchema"] for tool in contract["tools"]}
    for name in ("rag_search", "rag_retrieve_evidence"):
        properties = tools[name]["properties"]
        assert set(DEFAULT_OUTPUTS[name]) <= set(properties), name
        assert "older_versions" in DEFAULT_OUTPUTS[name], name
        assert properties["older_versions"]["type"] == "array", name


def test_fake_rag_read_chunks_match_the_contract() -> None:
    """fake の読み取りのツールの chunk の項目は RAG の契約と同じ（表計算の場所を含む。#1471）。"""
    contract = json.loads((CONTRACTS / "rag-tools.json").read_text(encoding="utf-8"))
    tools = {tool["name"]: tool["outputSchema"] for tool in contract["tools"]}
    for name in ("rag_read_document", "rag_read_section"):
        chunk = tools[name]["$defs"]["ReadDocumentChunk"]["properties"]
        assert {"sheet_name", "row_start", "row_end", "cell_range"} <= set(chunk), name
        for item in DEFAULT_OUTPUTS[name]["chunks"]:
            assert set(item) == set(chunk), name
        assert (
            DEFAULT_OUTPUTS[name]["schema_version"]
            == tools[name]["properties"]["schema_version"]["default"]
        )


# ---- 根拠の章節を読み切る（#1438） --------------------------------------------------------------

_GROUPED_GOAL = "契約の更新の手順をすべて教えてください。"


def _section_evidence(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]["evidence"][0])
    item.update(overrides)
    return item


def test_section_read_note_points_to_sections_longer_than_the_excerpt() -> None:
    used = _section_evidence(chunk_id="c-used", used_in_answer=True)
    first = _section_evidence(chunk_id="c-1", used_in_answer=False)
    same_section = _section_evidence(chunk_id="c-2", used_in_answer=False)
    short = _section_evidence(
        chunk_id="c-3",
        used_in_answer=False,
        section={**first["section"], "section_id": "sec-short", "chars": 2},
    )
    other = _section_evidence(
        chunk_id="c-4",
        document_id="doc-2",
        used_in_answer=False,
        section={**first["section"], "section_id": "sec-9"},
    )
    no_section = _section_evidence(chunk_id="c-5", section=None)
    output = {"evidence": [first, same_section, short, used, other, no_section]}

    note = section_read_note("rag_retrieve_evidence", output, [_GROUPED_GOAL])

    assert note is not None and note["next_step"] == SECTION_READ_HINT
    # 回答に使った根拠を先に、同じ章節は 1 つにし、抜粋より短い章節は案内しない。
    found = [
        (item["document_id"], item["section_id"], item["chunk_id"]) for item in note["sections"]
    ]
    assert found == [("doc-1", "sec-5", "c-used"), ("doc-2", "sec-9", "c-4")]
    assert note["sections"][0] == {
        "document_id": "doc-1",
        "chunk_id": "c-used",
        "section_id": "sec-5",
        "title": "第5条（更新）",
        "page_start": 3,
        "page_end": 4,
        "chars": 1200,
        "parent_section_id": "sec-ch2",
    }
    many = {
        "evidence": [
            _section_evidence(
                chunk_id=f"c{index}", section={**first["section"], "section_id": f"s{index}"}
            )
            for index in range(5)
        ]
    }
    limited = section_read_note("rag_search", many, [_GROUPED_GOAL])
    assert limited is not None and len(limited["sections"]) == MAX_SECTION_READS


def test_section_read_note_needs_a_grouped_question() -> None:
    output = {"evidence": [_section_evidence()]}

    assert section_read_note("rag_retrieve_evidence", output, ["契約の更新の期限は？"]) is None
    assert asks_for_grouped_content(["確認する項目を挙げてください"]) is True
    assert asks_for_grouped_content(["ＨＲＭの申請の流れ"]) is True
    assert asks_for_grouped_content(["承認者は誰？", None]) is False
    # 読み取りのツール以外・根拠の無い結果は案内しない。
    assert section_read_note("rag_read_source", output, [_GROUPED_GOAL]) is None
    assert section_read_note("rag_retrieve_evidence", {"evidence": []}, [_GROUPED_GOAL]) is None


def test_section_read_note_after_reading_a_section() -> None:
    read: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_read_section"])

    complete = section_read_note("rag_read_section", read, [_GROUPED_GOAL])
    assert complete == {
        "section_id": "sec-5",
        "parent_section_id": "sec-ch2",
        "next_step": SECTION_PARENT_HINT,
    }
    # 続きがあれば、質問に関係なく続きを読む案内。
    partial = {**read, "section_complete": False, "next_cursor": "cur-2"}
    assert section_read_note("rag_read_section", partial, ["期限は？"]) == {
        "section_id": "sec-5",
        "next_step": SECTION_CONTINUE_HINT,
    }
    # 階層 1 の章節の親は文書全体なので、上がる案内はしない。
    top = {**read, "section": {**read["section"], "level": 1, "parent_section_id": "document"}}
    assert section_read_note("rag_read_section", top, [_GROUPED_GOAL]) is None
    assert section_read_note("rag_read_section", read, ["期限は？"]) is None


def test_run_reads_the_section_of_the_evidence_when_the_question_asks_for_steps(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="根拠の章節を rag_read_section で読む。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=["rag_retrieve_evidence", "rag_read_section"],
                )
            ],
        )
    )
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "契約 更新 手順"}, call_id="call-1"
            )
        ],
        [
            function_call(
                "rag__rag_read_section",
                {"document_id": "doc-1", "chunk_id": "chunk-1"},
                call_id="call-2",
            )
        ],
        [assistant_message("更新の手順は p.3〜4 のとおりです。")],
    )
    run = _run(_GROUPED_GOAL)

    limit = int(get_settings().agent_max_rag_calls_per_run)
    evidence = _tool_output(model, 1, "call-1")
    assert evidence["section_reads"]["next_step"] == SECTION_READ_HINT
    assert [item["section_id"] for item in evidence["section_reads"]["sections"]] == ["sec-5"]
    read = _tool_output(model, 2, "call-2")
    assert read["section_reads"]["next_step"] == SECTION_PARENT_HINT
    # 章節の読み取りは RAG の検索の回数に数えない。
    assert read["rag_calls_remaining"] == limit - 1
    assert [call["arguments"] for call in mcp.calls_of("rag_read_section")] == [
        {"document_id": "doc-1", "chunk_id": "chunk-1"}
    ]
    # 記録する step の結果は RAG の結果のまま。
    recorded = [step.tool_result.output for step in run.steps if step.tool_result is not None]
    assert all("section_reads" not in (output or {}) for output in recorded)


def test_business_rag_research_skill_reads_sections() -> None:
    skill = skill_registry.get("business-rag-research")
    assert skill is not None
    assert "rag_read_section" in skill.mcp_requirements[0].tool_names
    # 章節の読み方は本文（Step 4）に置く（参照文書は #1816 の評価で読まれなかった。#1846）。
    assert "`rag_read_section` に `document_id`" in skill.instructions
    assert "`up` を 1 ずつ増やして" in skill.instructions
    assert skill.references == []
