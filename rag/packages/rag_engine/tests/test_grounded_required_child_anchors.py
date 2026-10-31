"""必須子の本文を生成用抜粋へ渡す境界を合成資料で検証する。"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rag_engine.generation import answering
from rag_engine.generation.answering import AnswerContext, AnswerRecord, _grounded_spans
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence

BODY = "対象を選択します。内容を変更し、確定ボタンを押します。"
QUESTION = "設定の操作を教えてください"


def fixture() -> tuple[AnswerContext, AnswerRecord]:
    parent = AnswerRecord(
        id="parent",
        chunk_id="parent",
        chunk_uid="doc:parent",
        source="sample.pdf",
        source_run_id="run",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        chunk_level="parent",
        text=("設定の操作の概要です。\n" * 160) + BODY,
    )
    child = replace(
        parent,
        id="child",
        chunk_id="child",
        chunk_uid="doc:child",
        parent_chunk_uid=parent.chunk_uid,
        chunk_level="child",
        text="検索用の見出し",
        body_text=BODY,
        page=2,
    )
    ctx = AnswerContext(
        records=[parent],
        text=parent.text,
        evidence_tree=(
            ContextParentEvidence(
                record=parent,
                role="synthesis_parent",
                reason="合成",
                children=(ContextChildEvidence(record=child, role="retrieved_anchor", reason="合成", retrieval_rank=1),),
            ),
        ),
    )
    return ctx, child


def knowledge(source: str) -> RuntimeKnowledgeContext:
    return RuntimeKnowledgeContext(
        original_question=QUESTION, expanded_question=QUESTION, path=Path("synthetic.json"), guide_steps=(("change", "変更", (source,)),)
    )


@pytest.mark.parametrize("source_attr", ["chunk_uid", "chunk_id", "id"])
def test_explicit_guide_child_is_pinned_using_original_body(monkeypatch: pytest.MonkeyPatch, source_attr: str) -> None:
    ctx, child = fixture()
    observed: dict[str, Any] = {}

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(answering, "evidence_spans", capture)
    _grounded_spans(QUESTION, ctx, (), knowledge(getattr(child, source_attr)), budget=350)
    assert len(observed["anchors"]) == 1
    assert observed["anchors"][0].text == BODY
    assert observed["scope_records"] == [child]
    assert child.text == "検索用の見出し"


def test_required_tail_body_reaches_citable_span_under_budget() -> None:
    ctx, child = fixture()
    spans = _grounded_spans(QUESTION, ctx, (), knowledge(child.chunk_uid), budget=350)
    assert any(BODY in span["text"] for span in spans)
    assert sum(len(span["text"]) for span in spans) <= 350


def test_unselected_child_does_not_become_required_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, child = fixture()
    observed: dict[str, Any] = {}

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(answering, "evidence_spans", capture)
    _grounded_spans(QUESTION, ctx, (), knowledge("doc:other"), budget=350)
    assert observed["anchors"] == []
    assert observed["scope_records"] == [child]


def test_visual_anchor_is_retained_alongside_guide_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, child = fixture()
    visual = replace(child, id="visual", chunk_id="visual", chunk_uid="doc:visual", text="画像の操作説明", body_text="")
    observed: dict[str, Any] = {}
    monkeypatch.setattr(answering, "_visual_evidence_anchors", lambda context, images: [visual])

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(answering, "evidence_spans", capture)
    _grounded_spans(QUESTION, ctx, (), knowledge(child.chunk_uid), budget=350)
    assert {record.chunk_uid for record in observed["anchors"]} == {child.chunk_uid, visual.chunk_uid}


def test_missing_required_body_is_recorded_even_without_selected_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, child = fixture()
    stages: list[dict[str, Any]] = []

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs["diagnostics"].append(
            {"requested_source_ids": [child.chunk_uid], "body_status": "missing", "selected_chars": 0, "required_chars": len(BODY)}
        )
        return []

    monkeypatch.setattr(answering, "evidence_spans", capture)
    monkeypatch.setattr(answering, "record_retrieval_stage", stages.append)
    assert _grounded_spans(QUESTION, ctx, (), knowledge(child.chunk_uid), budget=10) == []
    assert stages == [
        {
            "stage": "generation_evidence_preparation",
            "input_stage": "prepared_for_generation",
            "groups": [
                {"requested_source_ids": [child.chunk_uid], "body_status": "missing", "selected_chars": 0, "required_chars": len(BODY)}
            ],
        }
    ]


def test_definition_reselection_records_only_final_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, child = fixture()
    stages: list[dict[str, Any]] = []
    calls: list[bool] = []
    monkeypatch.setattr(answering, "definition_labels", lambda question: ["対象"])
    monkeypatch.setattr(answering, "definition_ranges", lambda text, labels: [{"label": "対象"}] if text == ctx.records[0].text else [])

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        final = bool(kwargs.get("definitions_only"))
        calls.append(final)
        kwargs["diagnostics"].append({"requested_source_ids": [child.chunk_uid], "body_status": "unlocated" if final else "missing"})
        return []

    monkeypatch.setattr(answering, "evidence_spans", capture)
    monkeypatch.setattr(answering, "record_retrieval_stage", stages.append)
    assert _grounded_spans(QUESTION, ctx, (), knowledge(child.chunk_uid), budget=20) == []
    assert calls == [False, True]
    assert len(stages) == 1
    assert stages[0]["groups"] == [{"requested_source_ids": [child.chunk_uid], "body_status": "unlocated"}]


@pytest.mark.parametrize("source_attr", ["chunk_uid", "chunk_id", "id"])
def test_guide_parent_protects_retrieved_child_without_pinning_neighbor(monkeypatch: pytest.MonkeyPatch, source_attr: str) -> None:
    ctx, child = fixture()
    parent = ctx.evidence_tree[0]
    neighbor = replace(
        child, id="neighbor", chunk_id="neighbor", chunk_uid="doc:neighbor", text=ctx.records[0].text[: -len(BODY)], body_text=""
    )
    ctx = replace(
        ctx,
        evidence_tree=(
            replace(
                parent,
                children=(
                    *parent.children,
                    ContextChildEvidence(record=neighbor, role="neighbor_context", reason="隣接", retrieval_rank=2),
                ),
            ),
        ),
    )
    observed: dict[str, Any] = {}
    select = answering.evidence_spans

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed.update(kwargs)
        return select(question, records, **kwargs)

    monkeypatch.setattr(answering, "evidence_spans", capture)
    source = getattr(parent.record, source_attr)
    spans = _grounded_spans(QUESTION, ctx, (), knowledge(source), budget=350)
    assert any(BODY in span["text"] for span in spans)
    assert [record.chunk_uid for record in observed["anchors"]] == [child.chunk_uid]
    assert observed["priority_source_groups"] == [(source,)]
    assert observed["scope_records"] == [child, neighbor]
    assert sum(len(span["text"]) for span in spans) <= 350


def test_explicit_guide_child_can_protect_neighbor_role(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, child = fixture()
    parent = ctx.evidence_tree[0]
    ctx = replace(ctx, evidence_tree=(replace(parent, children=(replace(parent.children[0], role="neighbor_context"),)),))
    observed: dict[str, Any] = {}

    def capture(question: str, records: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(answering, "evidence_spans", capture)
    _grounded_spans(QUESTION, ctx, (), knowledge(child.chunk_uid), budget=350)
    assert [record.chunk_uid for record in observed["anchors"]] == [child.chunk_uid]
