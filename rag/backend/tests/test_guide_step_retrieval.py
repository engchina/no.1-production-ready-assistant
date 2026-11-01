"""業務ガイドの手順ごとの検索と参照の頁の固定(#2395)。

モデルの API・Oracle はスタブ(合成の fixture)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.rag.answer_engine import AnswerEngine, _guide_step_diagnostics
from app.rag.answer_engine import guide_step_plan as engine_guide_step_plan
from app.rag.support_guide import reference_issues
from app.rag.support_guide_runtime import (
    apply_guide_to_diagnostics,
    guide_notices,
    guide_step_plan,
    match_guide,
)
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from app.schemas.support_guide import SupportGuideContent
from tests.test_answer_engine import FakeGenAi, FakeOracle, _doc_chunk, _evidence_ids, _fake_llm


class GuideStepOracle(FakeOracle):
    """質問の文では手順 A の資料だけを、手順 B の手がかり・参照の頁でだけ手順 B の資料を返す。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.step_a = _doc_chunk("doc-a", "設定 A は画面 A で変更します。")
        self.step_b = _doc_chunk("doc-b", "設定 A を変えたあとは、画面 B で反映ボタンを押します。")
        self.page = _doc_chunk("doc-c", "画面 B の項目の説明です。反映の前に確認します。")
        # 参照の2〜3ページに保存された原文。検索スタブも実際のページ位置を返す。
        self.page.metadata.update(page_start=2, page_end=2, parent_page_start=2, parent_page_end=3)

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        filters = dict(filters or {})
        self.calls.append((query, filters))
        if "page_ranges" in filters:
            return [self.page]
        return [self.step_b] if "画面 B" in query else [self.step_a]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []


def _guide(decision: str = "answer") -> dict[str, Any]:
    return {
        "guide_id": "g-1",
        "revision": 2,
        "decision": decision,
        "step_plan": [
            {"id": "s1", "title": "画面 A で変える", "retrieval_hints": [], "references": []},
            {
                "id": "s2",
                "title": "画面 B で反映する",
                "retrieval_hints": ["画面 B 反映"],
                "references": [
                    {
                        "document_id": "doc-c",
                        "title": "文書 C",
                        "page_ranges": [{"page_start": 2, "page_end": 3}],
                    }
                ],
            },
        ],
    }


async def _answer(monkeypatch: pytest.MonkeyPatch, oracle: GuideStepOracle, **settings: Any) -> Any:
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
            **{"rag_support_guide_enabled": True, **settings},
        ),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    return await engine.run(
        SearchRequest(query="設定 A の変更方法は？", filters={"knowledge_base_id": "kb-1"})
    )


async def test_guide_steps_add_step_b_and_reference_page_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手順ごとに検索し、手順 B の根拠と参照の頁の根拠を回答の材料に入れる。"""
    oracle = GuideStepOracle()

    outcome = await _answer(monkeypatch, oracle, rag_support_guide=_guide())

    assert {"doc-a:c1", "doc-b:c1", "doc-c:c1"} <= _evidence_ids(outcome)
    # 参照の頁の検索は、その文書の頁の範囲に絞る(利用者の KB の範囲は保つ)。
    page_filters = [filters for _, filters in oracle.calls if "page_ranges" in filters]
    assert page_filters
    assert json.loads(page_filters[0]["page_ranges"]) == [
        {"document_id": "doc-c", "page_start": 2, "page_end": 3}
    ]
    assert page_filters[0]["knowledge_base_id"] == "kb-1"
    trace = outcome.diagnostics["guide_step_retrieval"]
    assert trace["guide_id"] == "g-1" and trace["llm_calls"] == 0
    step_b = trace["steps"][1]
    assert step_b["has_references"] is True
    assert step_b["evidence_count"] >= 1
    assert [search["kind"] for search in step_b["searches"]] == ["reference", "step"]
    assert any(
        step["name"] == "業務ガイドの手順ごとの検索"
        for step in outcome.diagnostics["execution_steps"]
    )
    # 手順ごとの充足（#2686）は id・状態・必須かだけを回答の診断に写す。
    coverage = outcome.diagnostics["guide_step_coverage"]
    assert [step["step_id"] for step in coverage] == ["s1", "s2"]
    assert all(
        set(step) == {"step_id", "status", "required", "assignment", "evidence_flow"}
        for step in coverage
    )


async def test_guide_steps_skip_without_answer_decision_or_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認・引き継ぎのガイド、設定で無効なときは手順ごとに検索しない。"""
    oracle = GuideStepOracle()
    outcome = await _answer(monkeypatch, oracle, rag_support_guide=_guide(decision="clarify"))
    assert "guide_step_retrieval" not in outcome.diagnostics
    assert "doc-b:c1" not in _evidence_ids(outcome)

    disabled = GuideStepOracle()
    outcome = await _answer(
        monkeypatch,
        disabled,
        rag_support_guide=_guide(),
        rag_guide_step_retrieval_enabled=False,
    )
    assert outcome.diagnostics["guide_step_retrieval"]["skipped"] == "disabled"
    assert not any("page_ranges" in filters for _, filters in disabled.calls)


def test_with_page_ranges_intersects_user_scope() -> None:
    """利用者の確認で絞った頁の範囲があれば、重なる頁だけを検索する(範囲を広げない)。"""
    from app.rag.answer_engine import _with_page_ranges

    wanted = [{"document_id": "doc-c", "page_start": 2, "page_end": 5}]
    plain = _with_page_ranges({"knowledge_base_id": "kb-1"}, wanted)
    assert plain is not None
    assert json.loads(plain["page_ranges"]) == wanted
    scoped = _with_page_ranges(
        {"page_ranges": json.dumps([{"document_id": "doc-c", "page_start": 4, "page_end": 9}])},
        wanted,
    )
    assert scoped is not None
    assert json.loads(scoped["page_ranges"]) == [
        {"document_id": "doc-c", "page_start": 4, "page_end": 5}
    ]
    other = json.dumps([{"document_id": "doc-x", "page_start": 1, "page_end": 2}])
    assert _with_page_ranges({"page_ranges": other}, wanted) is None


def test_with_page_ranges_keeps_sheet_rows_apart_from_pages() -> None:
    """表計算のシートの行の範囲(#2663)は、同じシートの行とだけ重ね、頁の範囲とは重ねない。"""
    from app.rag.answer_engine import _with_page_ranges

    wanted = [{"document_id": "doc-s", "sheet_name": "シートA", "row_start": 40, "row_end": 50}]
    plain = _with_page_ranges({}, wanted)
    assert plain is not None
    assert json.loads(plain["page_ranges"]) == [
        {
            "document_id": "doc-s",
            "page_start": None,
            "page_end": None,
            "sheet_name": "シートA",
            "row_start": 40,
            "row_end": 50,
        }
    ]

    def scope(*items: dict[str, Any]) -> dict[str, str]:
        return {"page_ranges": json.dumps(list(items))}

    rows = _with_page_ranges(
        scope({"document_id": "doc-s", "sheet_name": "シートA", "row_start": 45}), wanted
    )
    assert rows is not None
    assert json.loads(rows["page_ranges"])[0]["row_start"] == 45
    whole = _with_page_ranges(scope({"document_id": "doc-s"}), wanted)
    assert whole is not None
    assert json.loads(whole["page_ranges"])[0]["sheet_name"] == "シートA"
    other_sheet = scope({"document_id": "doc-s", "sheet_name": "シートB", "row_start": 45})
    assert _with_page_ranges(other_sheet, wanted) is None
    pages = scope({"document_id": "doc-s", "page_start": 40, "page_end": 50})
    assert _with_page_ranges(pages, wanted) is None


async def test_guide_step_sheet_reference_searches_sheet_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表計算の資料を参照する手順は、シートの行に絞って検索する(行を頁として絞らない。#2663)。"""
    oracle = GuideStepOracle()
    guide = _guide()
    guide["step_plan"][1]["references"] = [
        {
            "document_id": "doc-c",
            "title": "表計算 C",
            "page_ranges": [],
            "sheet_ranges": [{"sheet_name": "シートA", "row_start": 42, "row_end": 45}],
        }
    ]

    await _answer(monkeypatch, oracle, rag_support_guide=guide)

    page_filters = [filters for _, filters in oracle.calls if "page_ranges" in filters]
    assert page_filters
    assert json.loads(page_filters[0]["page_ranges"]) == [
        {
            "document_id": "doc-c",
            "page_start": None,
            "page_end": None,
            "sheet_name": "シートA",
            "row_start": 42,
            "row_end": 45,
        }
    ]


def test_build_engine_settings_passes_guide_step_settings(tmp_path: Any) -> None:
    """手順ごとの検索の設定と追加の検索の枠を rag_engine の設定へ渡す。"""
    from app.rag.answer_engine import build_engine_settings

    off = build_engine_settings(Settings(_env_file=None), output_dir=tmp_path)
    assert off.guide_step_retrieval_enabled is False
    default = build_engine_settings(Settings(rag_support_guide_enabled=True), output_dir=tmp_path)
    custom = build_engine_settings(
        Settings(
            rag_guide_step_retrieval_enabled=False,
            rag_guide_step_max_queries=2,
            rag_guide_step_chunks_per_step=3,
            rag_guide_reference_max_chunks=1,
            rag_guide_step_deadline_seconds=2.5,
            rag_extra_retrieval_max_queries=5,
        ),
        output_dir=tmp_path,
    )

    assert default.guide_step_retrieval_enabled is True
    assert (default.guide_step_max_queries, default.extra_retrieval_max_queries) == (4, 8)
    # 手順の多い業務ガイドで後ろの手順が残らないよう、期限の既定は上限の 60 秒（#2826）。
    assert default.guide_step_deadline_seconds == 60.0
    assert custom.guide_step_retrieval_enabled is False
    assert (custom.guide_step_max_queries, custom.guide_step_chunks_per_step) == (2, 3)
    assert (custom.guide_reference_max_chunks, custom.guide_step_deadline_seconds) == (1, 2.5)
    assert custom.extra_retrieval_max_queries == 5


def _content(**changes: Any) -> SupportGuideContent:
    base: dict[str, Any] = {
        "title": "設定の変更",
        "goal": {"expected_result": "設定が変わる", "match_terms": ["設定 A"]},
        "conditions": [
            {
                "id": "kind",
                "label": "種類",
                "allowed_values": ["甲", "乙"],
                "question": "種類は？",
            }
        ],
        "steps": [
            {"id": "s1", "title": "画面 A で変える", "retrieval_hints": ["画面 A"]},
            {
                "id": "s2",
                "title": "画面 B で反映する",
                "depends_on": ["s1"],
                "references": [
                    {"document_id": "doc-b", "page_ranges": [{"page_start": 4, "page_end": 6}]}
                ],
            },
            {
                "id": "s3",
                "title": "乙の場合の手順",
                "references": [{"document_id": "doc-c", "page_ranges": [{"page_start": 7}]}],
            },
        ],
        "branches": [
            {"id": "b1", "when": {"condition_id": "kind", "values": ["乙"]}, "goto_step": "s3"}
        ],
    }
    base.update(changes)
    return SupportGuideContent.model_validate(base)


def test_schema_v2_page_ranges_and_step_references() -> None:
    content = _content()

    assert content.schema_version == 2
    # 終わりの頁を省けば 1 頁。
    assert content.steps[2].references[0].page_ranges[0].page_end == 7
    with pytest.raises(ValueError, match="頁の範囲の終わり"):
        _content(
            references=[{"document_id": "d", "page_ranges": [{"page_start": 5, "page_end": 2}]}]
        )
    with pytest.raises(ValueError):
        SupportGuideContent.model_validate({**content.model_dump(), "schema_version": 1})


def test_guide_step_plan_excludes_steps_of_branches_that_do_not_apply() -> None:
    """分かっている条件で外れた分岐の手順は、手順ごとの検索に渡さない。"""
    match = match_guide([("g", 1, _content())], "設定 A を変えたい", {"kind": "甲"})
    assert match is not None

    plan = guide_step_plan(match)

    assert [step["id"] for step in plan] == ["s1", "s2"]
    assert plan[1]["references"] == [
        {
            "document_id": "doc-b",
            "title": "",
            "page_ranges": [{"page_start": 4, "page_end": 6}],
            "sheet_ranges": [],
        }
    ]
    applies = match_guide([("g", 1, _content())], "設定 A を変えたい", {"kind": "乙"})
    assert applies is not None
    assert [step["id"] for step in guide_step_plan(applies)] == ["s1", "s2", "s3"]


def test_guide_step_plan_marks_steps_of_undecided_branches_optional() -> None:
    """条件が決まらない分岐の行き先とその後続は、回答の充足で必須の手順に数えない(#2491)。"""
    content = _content(
        steps=[
            *_content().model_dump()["steps"],
            {"id": "s4", "title": "乙の後の確認", "depends_on": ["s3"]},
        ]
    )
    undecided = match_guide([("g", 1, content)], "設定 A を変えたい", {})
    assert undecided is not None

    plan = guide_step_plan(undecided)

    assert [(step["id"], step["required"]) for step in plan] == [
        ("s1", True),
        ("s2", True),
        ("s3", False),
        ("s4", False),
    ]
    decided = match_guide([("g", 1, content)], "設定 A を変えたい", {"kind": "乙"})
    assert decided is not None
    assert all(step["required"] for step in guide_step_plan(decided))
    plan_object = engine_guide_step_plan(
        {"decision": "branch", "guide_id": "g", "revision": 1, "step_plan": plan}
    )
    assert plan_object is not None
    assert plan_object.optional_step_ids == ("s3", "s4")


def test_guide_notices_reach_the_answer_plan_apart_from_the_documents() -> None:
    """業務ガイドの窓口・引き継ぐ条件・承認は、資料と分けて回答の案内の材料にする(#2792)。"""
    content = _content(
        handoff={"conditions": ["種類が分からない場合"], "contact": "窓口 X"},
        impact={"approval_required": True, "approval_note": "担当者の承認", "steps": ["s3"]},
    )
    undecided = match_guide([("g", 1, content)], "設定 A を変えたい", {})
    assert undecided is not None
    notices = guide_notices(undecided)
    assert notices == {
        "contact": "窓口 X",
        "handoff_conditions": ["種類が分からない場合"],
        "approval_required": True,
        "approval_note": "担当者の承認",
        "branch_notes": [],
    }
    # 承認が係る手順（s3）が分かっている条件で外れれば、承認は案内しない。
    excluded = match_guide([("g", 1, content)], "設定 A を変えたい", {"kind": "甲"})
    assert excluded is not None
    assert guide_notices(excluded)["approval_required"] is False

    plan = engine_guide_step_plan(
        {
            "decision": "answer",
            "guide_id": "g",
            "revision": 1,
            "title": content.title,
            "step_plan": guide_step_plan(undecided),
            "notices": notices,
        }
    )
    assert plan is not None
    assert (plan.title, plan.contact, plan.handoff_conditions) == (
        content.title,
        "窓口 X",
        ("種類が分からない場合",),
    )


async def test_reference_issues_check_step_references() -> None:
    async def lookup(document_id: str) -> str | None:
        return "見つかりません" if document_id == "doc-c" else None

    issues = await reference_issues(_content(), lookup)

    assert [issue.path for issue in issues] == ["steps[2].references[0]"]


def test_step_retrieval_record_goes_to_guide_diagnostics_not_envelope() -> None:
    diagnostics: dict[str, Any] = {
        "envelope": {"outcome": "answered"},
        "guide_step_retrieval": {"steps": [{"id": "s2", "evidence_count": 1}]},
    }

    apply_guide_to_diagnostics(diagnostics, {"guide_id": "g-1", "revision": 2})

    assert diagnostics["guide"]["step_retrieval"]["steps"][0]["id"] == "s2"
    assert "step_retrieval" not in diagnostics["envelope"]["guide"]
    assert "guide_step_retrieval" not in diagnostics


def test_step_coverage_and_embedding_go_to_guide_diagnostics() -> None:
    """手順の充足と embedding の呼び出しを業務ガイドの記録に移す（#2686。envelope は除く）。"""
    diagnostics: dict[str, Any] = {
        "envelope": {"outcome": "conditional"},
        "guide_step_coverage": [{"step_id": "s1", "status": "unused", "required": True}],
        "guide_step_embedding": {"calls": 1, "texts": 4, "elapsed_ms": 30, "failed": False},
    }

    apply_guide_to_diagnostics(diagnostics, {"guide_id": "g-1", "revision": 2})

    assert diagnostics["guide"]["step_coverage"] == [
        {"step_id": "s1", "status": "unused", "required": True}
    ]
    assert diagnostics["guide"]["step_embedding"]["calls"] == 1
    assert "step_retrieval" not in diagnostics["guide"]
    assert {"step_coverage", "step_embedding"}.isdisjoint(diagnostics["envelope"]["guide"])
    assert {"guide_step_coverage", "guide_step_embedding"}.isdisjoint(diagnostics)


def test_answer_diagnostics_keep_only_step_ids_statuses_and_call_counts() -> None:
    """記録には手順の id・状態・必須かと呼び出しの回数・時間だけを写す（#2686）。"""
    trace = {
        "guide_step_coverage": [
            {
                "step_id": "s1",
                "title": "手順 1",
                "required": True,
                "status": "explained",
                "evidence_refs": [{"evidence_id": "e1", "text": "根拠の本文"}],
            },
            "壊れた値",
        ],
        "guide_step_embedding": {
            "calls": 2,
            "texts": 9,
            "elapsed_ms": 120,
            "failed": True,
            "extra": "x",
        },
    }

    assert _guide_step_diagnostics(trace) == {
        "guide_step_coverage": [{"step_id": "s1", "status": "explained", "required": True}],
        "guide_step_embedding": {"calls": 2, "texts": 9, "elapsed_ms": 120, "failed": True},
    }
    assert _guide_step_diagnostics({}) == {}
    assert _guide_step_diagnostics(None) == {}


def test_mcp_guide_reference_carries_sheet_ranges_apart_from_pages() -> None:
    """rag_lookup_guides の参照は、表計算の資料のシートの行を頁と別に返す(#2663)。"""
    from app.mcp.tools import _guide_reference
    from app.schemas.support_guide import SupportGuideReference

    sheet = _guide_reference(
        SupportGuideReference.model_validate(
            {
                "document_id": "doc-s",
                "sheet_ranges": [{"sheet_name": "シートA", "cell_range": "B42:D45"}],
            }
        )
    )
    assert sheet.page_ranges == []
    assert [(r.sheet_name, r.row_start, r.row_end, r.cell_range) for r in sheet.sheet_ranges] == [
        ("シートA", 42, 45, "B42:D45")
    ]
    pages = _guide_reference(
        SupportGuideReference.model_validate(
            {"document_id": "doc-a", "page_ranges": [{"page_start": 3}]}
        )
    )
    assert [(r.page_start, r.page_end) for r in pages.page_ranges] == [(3, 3)]
    assert pages.sheet_ranges == []


async def test_master_off_discards_stale_guide_and_does_not_run_step_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = GuideStepOracle()
    outcome = await _answer(
        monkeypatch,
        oracle,
        rag_support_guide_enabled=False,
        rag_guide_step_retrieval_enabled=True,
        rag_support_guide=_guide(),
    )
    assert "guide_step_retrieval" not in outcome.diagnostics
    assert not any("page_ranges" in filters for _, filters in oracle.calls)
    assert "doc-b:c1" not in _evidence_ids(outcome)
