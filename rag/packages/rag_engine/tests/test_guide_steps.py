"""業務ガイドの手順ごとの検索と参照の頁の固定（#2395）。外部 I/O と LLM は使わない（合成の fixture）。"""

from __future__ import annotations

import itertools
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.grounded import build_context_block, guide_step_evidence_lines
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.retrieval.context_builder import ContextParentEvidence
from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget
from rag_engine.retrieval.guide_steps import (
    GuidePlan,
    StepSearch,
    planned_searches,
    run_step_searches,
    select_step_parents,
    step_queries,
)

QUESTION = "設定 A の変更方法は？"


def record(uid: str, text: str = "本文") -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source="manual.pdf",
        source_run_id="source",
        chunk_id=uid,
        chunk_uid=f"run:{uid}",
        chunk_level="parent",
    )


def parent(uid: str, text: str = "本文") -> ContextParentEvidence:
    return ContextParentEvidence(record=record(uid, text), role="synthesis_parent", reason="top")


def context(*parents: ContextParentEvidence) -> AnswerContext:
    return AnswerContext(records=[p.record for p in parents], text="x", evidence_tree=tuple(parents))


def plan(*steps: dict[str, Any]) -> GuidePlan:
    result = GuidePlan.from_payload("g-1", 3, steps)
    assert result is not None
    return result


STEP_A = {"id": "s1", "title": "画面 A で設定を変える", "retrieval_hints": ["画面 A"]}
STEP_B = {
    "id": "s2",
    "title": "画面 B で反映する",
    "retrieval_hints": ["画面 B", "反映"],
    "references": [{"document_id": "doc-b", "title": "文書 B", "page_ranges": [{"page_start": 4, "page_end": 6}]}],
}


def test_plan_from_payload_reads_steps_and_page_ranges() -> None:
    result = plan(STEP_A, STEP_B, {"title": "id の無い手順は読まない"})

    assert [step.step_id for step in result.steps] == ["s1", "s2"]
    assert result.steps[1].page_filters() == [{"document_id": "doc-b", "page_start": 4, "page_end": 6}]
    # 頁の無い参照は文書の全体に絞る。
    whole = plan({"id": "s1", "title": "t", "references": [{"document_id": "doc-c"}]})
    assert whole.steps[0].page_filters() == [{"document_id": "doc-c"}]
    assert GuidePlan.from_payload("g", 1, []) is None


def test_plan_from_payload_reads_sheet_row_ranges_apart_from_pages() -> None:
    """表計算の資料はシートの行で絞る（行の番号を頁にしない。#2663）。"""
    sheets = [{"sheet_name": "シートA", "row_start": 42, "row_end": 45}, {"sheet_name": "シートB"}, {"row_start": 3}]
    result = plan({"id": "s1", "title": "t", "references": [{"document_id": "doc-s", "sheet_ranges": sheets}]})

    assert result.steps[0].page_filters() == [
        {"document_id": "doc-s", "sheet_name": "シートA", "row_start": 42, "row_end": 45},
        {"document_id": "doc-s", "sheet_name": "シートB"},
    ]
    assert result.steps[0].references[0].to_payload()["page_ranges"] == []


def test_step_queries_use_question_and_hints_or_title() -> None:
    steps = plan(STEP_A, {"id": "s3", "title": "題名だけの手順"}).steps

    assert step_queries(QUESTION, steps[0]) == (f"{QUESTION} 画面 A", (f"{QUESTION} 画面 A", "画面 A"))
    # 手がかりが無ければ手順の題名で探す。
    assert step_queries(QUESTION, steps[1])[0] == f"{QUESTION} 題名だけの手順"


def test_planned_searches_give_every_step_one_search_before_second_ones() -> None:
    steps = plan(STEP_B, STEP_A)

    assert [(step.step_id, kind) for step, kind in planned_searches(steps, 10)] == [
        ("s2", "reference"),
        ("s1", "step"),
        ("s2", "step"),
    ]
    # 上限で後ろの手順の検索が、前の手順の 2 回目に押し出されない。
    assert [(step.step_id, kind) for step, kind in planned_searches(steps, 2)] == [("s2", "reference"), ("s1", "step")]


def test_run_step_searches_passes_page_ranges_only_for_reference_search() -> None:
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    def retrieve(text: str, queries: tuple[str, ...], page_ranges: list[dict[str, Any]]) -> AnswerContext:
        calls.append((text, page_ranges))
        return context(parent(f"p{len(calls)}"))

    results, stop = run_step_searches(QUESTION, plan(STEP_A, STEP_B), limit=3, deadline_seconds=10, retrieve=retrieve)

    assert stop == ""
    assert [(r.step_id, r.kind) for r in results] == [("s1", "step"), ("s2", "reference"), ("s2", "step")]
    assert calls[1][1] == [{"document_id": "doc-b", "page_start": 4, "page_end": 6}]
    assert calls[0][1] == [] and calls[2][1] == []
    # 上限で切れたら理由を残す。
    _, stopped = run_step_searches(QUESTION, plan(STEP_A, STEP_B), limit=2, deadline_seconds=10, retrieve=retrieve)
    assert stopped == "max_queries"


def test_run_step_searches_records_errors_and_continues() -> None:
    def retrieve(text: str, queries: tuple[str, ...], page_ranges: list[dict[str, Any]]) -> AnswerContext:
        if "画面 A" in text:
            raise RuntimeError("search failed")
        return context(parent("b1"))

    results, _ = run_step_searches(QUESTION, plan(STEP_A, {"id": "s3", "title": "画面 C"}), limit=4, deadline_seconds=10, retrieve=retrieve)

    # 例外の型の名前だけを残し、本文は残さない（#3259）。
    assert results[0].error == "RuntimeError" and results[0].parents == ()
    assert [p.record.chunk_uid for p in results[1].parents] == ["run:b1"]


def test_select_step_parents_links_existing_and_pins_reference_pages() -> None:
    results = [
        StepSearch("s1", "step", "q", ("q",), (parent("a1"), parent("a2"), parent("a3"))),
        StepSearch("s2", "reference", "q", ("q",), (parent("b1"), parent("b2"), parent("b3"))),
        StepSearch("s3", "reference", "q", ("q",), (parent("c1"),)),
    ]

    selection = select_step_parents(results, {"run:a1"}, chunks_per_step=2, reference_max_chunks=3)

    # 既にある根拠は足さずに手順へ紐づける。1 手順は上位 2 件まで。
    assert [(step, kind, p.record.chunk_uid) for step, kind, p in selection.added] == [
        ("s1", "step", "run:a2"),
        ("s2", "reference", "run:b1"),
        ("s3", "reference", "run:c1"),
        ("s2", "reference", "run:b2"),
    ]
    assert selection.step_sources == {"s1": ("run:a1", "run:a2"), "s2": ("run:b1", "run:b2"), "s3": ("run:c1",)}


def _settings(**changes: Any) -> Any:
    return replace(get_settings(environ={}, dotenv_path=None), **changes)


def _patch_search(monkeypatch: pytest.MonkeyPatch, results: dict[str, AnswerContext]) -> list[tuple[str, list[dict[str, Any]]]]:
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    def fake_build(question: str, run_id: Any, engines: Any, settings: Any, **kwargs: Any) -> AnswerContext:
        # 文書の選択・画面目録・条の目録（LLM）は使わない。
        assert settings.document_selection_enabled is False and settings.screen_linking_enabled is False
        assert settings.article_catalog_enabled is False
        page_ranges = list(kwargs.get("page_ranges") or [])
        calls.append((question, page_ranges))
        key = "reference" if page_ranges else question
        return results.get(key, context())

    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", fake_build)
    return calls


def _cover(ctx: AnswerContext, settings: Any, guide: GuidePlan, budget: ExtraRetrievalBudget | None = None) -> Any:
    return a._cover_guide_steps(
        QUESTION,
        ctx,
        settings,
        guide,
        extra_budget=budget or ExtraRetrievalBudget.from_settings(settings),
        run_id="run",
        preferred_engines=["docling"],
        top_k=8,
        neighbor_child_count=0,
        rerank_enabled=False,
        inquiry_conditions=None,
        retrieval_scope="knowledge_base",
        classification_filter=None,
        runtime_knowledge=None,
    )


def test_cover_guide_steps_adds_step_b_evidence_and_reference_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(
        monkeypatch,
        {
            f"{QUESTION} 画面 A": context(parent("a1")),
            f"{QUESTION} 画面 B 反映": context(parent("b-top")),
            "reference": context(_paged("b-page", "doc-b", 5)),
        },
    )
    ctx = context(parent("a1"), parent("other"))

    merged, trace, sources = _cover(ctx, _settings(), plan(STEP_A, STEP_B))

    assert [q for q, _ in calls] == [f"{QUESTION} 画面 A", f"{QUESTION} 画面 B 反映", f"{QUESTION} 画面 B 反映"]
    # 既存の根拠を先に残し、手順 B の根拠と参照の頁の根拠を後ろに足す。
    assert [r.chunk_uid for r in merged.records] == ["run:a1", "run:other", "run:b-page", "run:b-top"]
    assert {p.reason for p in merged.evidence_tree[2:]} == {"guide_reference", "guide_step"}
    assert sources == (("s1", "画面 A で設定を変える", ("run:a1",)), ("s2", "画面 B で反映する", ("run:b-page", "run:b-top")))
    assert trace["llm_calls"] == 0
    step_b = trace["steps"][1]
    assert step_b["has_references"] is True and step_b["evidence_count"] == 2
    assert step_b["reference_chunk_count"] == 1
    assert [s["kind"] for s in step_b["searches"]] == ["reference", "step"]
    assert trace["budget"]["used"] == {"guide_required": 2, "guide_steps": 1}


def _paged(uid: str, document_id: str, page: int) -> ContextParentEvidence:
    rec = replace(record(uid), page=page, page_end=page, metadata={"document_id": document_id})
    return ContextParentEvidence(record=rec, role="synthesis_parent", reason="top")


def test_cover_guide_steps_links_reference_pages_of_steps_cut_by_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """期限で手順 B を検索しなくても、手順 B の参照の頁の根拠（回答の材料にある）を手順 B の根拠にする（#2706）。"""
    step_a = {**STEP_A, "references": [{"document_id": "doc-b", "page_ranges": [{"page_start": 5, "page_end": 5}]}]}
    calls = _patch_search(monkeypatch, {"reference": context(_paged("shared", "doc-b", 5))})
    ctx = context(_paged("shared", "doc-b", 5), _paged("other", "doc-b", 9))
    # 開始・検索の開始・1 回目の期限の確認・1 回目の検索の開始は 0 秒、検索 1 回が 30 秒かかる。
    ticks = itertools.chain([0.0, 0.0, 0.0, 0.0], itertools.repeat(30.0))

    merged, trace, sources = a._cover_guide_steps(
        QUESTION,
        ctx,
        _settings(guide_step_deadline_seconds=10.0),
        plan(step_a, STEP_B),
        extra_budget=ExtraRetrievalBudget.from_settings(_settings()),
        run_id="run",
        preferred_engines=["docling"],
        top_k=8,
        neighbor_child_count=0,
        rerank_enabled=False,
        inquiry_conditions=None,
        retrieval_scope="knowledge_base",
        classification_filter=None,
        runtime_knowledge=None,
        clock=lambda: next(ticks),
    )

    # 手順 A の参照の検索 1 回で期限。手順 B は検索していない。
    assert len(calls) == 1
    assert trace["stop_reason"] == "deadline"
    assert trace["unsearched_step_ids"] == []
    assert trace["required_acquisition"]["targets"][0]["execution"] == "reused"
    # 手順 B の参照の頁（文書 B の 4〜6 頁）に当たる材料の根拠は、手順 A と共有する根拠として手順 B にも紐づく。
    assert sources == (("s1", "画面 A で設定を変える", ("run:shared",)), ("s2", "画面 B で反映する", ("run:shared",)))
    assert [step["referenced_count"] for step in trace["steps"]] == [1, 1]
    assert trace["required_acquisition"]["targets"][1]["scope_coverage"] == "unknown"
    assert [r.chunk_uid for r in merged.records] == ["run:shared", "run:other"]


def test_cover_guide_steps_respects_disabled_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, {})
    ctx = context(parent("a1"))

    merged, trace, sources = _cover(ctx, _settings(guide_step_retrieval_enabled=False), plan(STEP_A))
    assert (merged, trace["skipped"], sources, calls) == (ctx, "disabled", (("s1", STEP_A["title"], ()),), [])

    merged, trace, _ = _cover(ctx, _settings(extra_retrieval_max_queries=0), plan(STEP_A))
    assert (merged, trace["stop_reason"], calls) == (ctx, "required_budget_exhausted", [])
    assert trace["required_acquisition"]["incomplete_step_ids"] == ["s1"]


def test_guide_steps_and_request_coverage_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """手順ごとの検索と根拠の無い要求の再検索は、追加の検索の枠を 1 か所で数える。"""
    _patch_search(monkeypatch, {})
    settings = _settings(extra_retrieval_max_queries=3, guide_step_max_queries=4)
    budget = ExtraRetrievalBudget.from_settings(settings)

    _, trace, _ = _cover(context(parent("a1")), settings, plan(STEP_A, STEP_B, {"id": "s3", "title": "画面 C"}), budget)

    # 手順の検索は枠の 3 回まで（手順 B の 2 回目の検索は枠の外）。
    assert trace["stop_reason"] == "max_queries"
    assert budget.used == {"guide_required": 3} and budget.remaining == 0
    # 残りの無い枠では、再検索の回数は 0 になる。
    assert budget.allowance(settings.request_coverage_max_queries) == 0


def test_build_context_block_lists_step_evidence_labels() -> None:
    spans = [
        {"label": "E1", "source_id": "run:a1", "pinned": False, "function": ("doc", "F"), "text": "a"},
        {"label": "E2", "source_id": "run:b1", "pinned": False, "function": ("doc", "F"), "text": "b"},
    ]
    steps = (("s1", "画面 A", ("run:a1",)), ("s2", "画面 B", ("run:b1", "run:zz")), ("s3", "画面 C", ()))

    assert guide_step_evidence_lines(steps, spans) == ["- s1「画面 A」: E1", "- s2「画面 B」: E2", "- s3「画面 C」: 根拠なし"]


def test_build_context_block_shows_step_evidence_only_with_guide_steps() -> None:
    spans = [{"label": "E1", "source_id": "run:a1", "pinned": False, "function": ("doc", "F"), "text": "a", "tags": []}]

    with_steps = build_context_block(QUESTION, spans, preface="", feedback=None, guide_steps=(("s1", "画面 A", ("run:a1",)),))
    without = build_context_block(QUESTION, spans, preface="", feedback=None)

    assert "業務ガイドの手順ごとの根拠" in with_steps and "- s1「画面 A」: E1" in with_steps
    assert "業務ガイドの手順ごとの根拠" not in without
    assert RuntimeKnowledgeContext(original_question="q", expanded_question="q", path=Path("x")).guide_steps == ()


def test_engine_settings_default_and_env_for_guide_steps() -> None:
    default = get_settings(environ={}, dotenv_path=None)
    custom = get_settings(
        environ={
            "RAG_ENGINE_GUIDE_STEP_RETRIEVAL": "0",
            "RAG_ENGINE_GUIDE_STEP_MAX_QUERIES": "2",
            "RAG_ENGINE_GUIDE_STEP_CHUNKS_PER_STEP": "3",
            "RAG_ENGINE_GUIDE_REFERENCE_MAX_CHUNKS": "1",
            "RAG_ENGINE_GUIDE_STEP_DEADLINE_SECONDS": "2.5",
            "RAG_ENGINE_EXTRA_RETRIEVAL_MAX_QUERIES": "5",
        },
        dotenv_path=None,
    )

    assert (default.guide_step_retrieval_enabled, default.guide_step_max_queries, default.extra_retrieval_max_queries) == (True, 4, 8)
    assert default.guide_step_deadline_seconds == 60.0  # 期限の既定は上限の 60 秒（#2826）
    assert (custom.guide_step_retrieval_enabled, custom.guide_step_max_queries, custom.guide_step_chunks_per_step) == (False, 2, 3)
    assert (custom.guide_reference_max_chunks, custom.guide_step_deadline_seconds, custom.extra_retrieval_max_queries) == (1, 2.5, 5)


def test_guide_step_instructions_keep_steps_the_user_cannot_perform() -> None:
    """残った業務ガイドの手順は、分かっている条件で利用者が自分で行えなくても説明させる（#2813）。

    条件で外れる手順は分岐（visible_plan）がコードで外すので、生成の指示は手順を省く理由に条件を挙げない。
    """
    from rag_engine.generation.grounded import _GUIDE_STEP_HINT, GENERATE_SYSTEM_PROMPT

    guide_line = next(line for line in GENERATE_SYSTEM_PROMPT.splitlines() if line.startswith("- guide_task_scope は"))
    assert "利用者が自分で行えない必須手順も省かず" in guide_line
    assert "required=true" in guide_line and "required=false" in guide_line
    assert "外れる操作は混ぜない" not in guide_line
    assert "条件" not in _GUIDE_STEP_HINT
