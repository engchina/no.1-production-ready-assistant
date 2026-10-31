"""必須の子本文は任意の前置きより先に確保する。実サービスは使わない。"""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from rag_engine.generation.answer_models import AnswerRecord
from rag_engine.retrieval.evidence_selection import evidence_excerpt, evidence_spans
from rag_engine.retrieval.source_context import prompt_source_context_text

BODY = "対象を選択し、詳細を変更して確定します。"


def pair(index: int = 1) -> tuple[AnswerRecord, AnswerRecord]:
    header = "変更方法の概要です。\n" * 35
    parent = AnswerRecord(
        id=f"p{index}",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=index,
        category="Chunk",
        text=header + BODY,
        source=f"sample-{index}.pdf",
        chunk_level="parent",
        chunk_id=f"p{index}",
        chunk_uid=f"v:p{index}",
        source_run_id="run",
        metadata={"section_path": ["概要"], "layout": {"native_text_ranges": [{"start": 0, "end": len(header) - 1}]}},
    )
    child = replace(
        parent,
        id=f"c{index}",
        chunk_uid=f"v:c{index}",
        chunk_id=f"c{index}",
        parent_chunk_uid=parent.chunk_uid,
        chunk_level="child",
        text=BODY,
        metadata={"section_path": ["必要な操作"]},
    )
    return parent, child


def cost(spans: Sequence[dict[str, Any]]) -> int:
    """予算が数える字数: 本文と、生成の入力に描く補助情報（#3831）。"""
    return sum(
        len(row["text"])
        + len(row.get("answer_context", ""))
        + len(prompt_source_context_text(row["source_context"], document_name=row["source"]))
        for row in spans
    )


def test_explicit_child_body_precedes_native_heading_and_optional_parents() -> None:
    parent, child = pair()
    noise = [
        replace(parent, id=f"n{i}", chunk_uid=f"v:n{i}", chunk_id=f"n{i}", text="変更方法の補足です。" * 40, metadata={}) for i in range(4)
    ]
    spans = evidence_spans(
        "変更方法を教えて",
        [*noise, parent],
        budget=900,
        anchors=[child],
        scope_records=[child],
        priority_source_groups=[(child.chunk_uid,)],
    )
    assert BODY in "".join(row["text"] for row in spans)
    assert spans[0]["source_id"] == parent.chunk_uid
    assert cost(spans) <= 900


def test_each_required_child_keeps_its_body_with_a_shared_budget() -> None:
    pairs = [pair(i) for i in (1, 2)]
    spans = evidence_spans(
        "変更方法を教えて",
        [p for p, _ in pairs],
        budget=900,
        anchors=[c for _, c in pairs],
        scope_records=[c for _, c in pairs],
        priority_source_groups=[(c.chunk_uid,) for _, c in pairs],
    )
    for parent, _ in pairs:
        assert any(row["source_id"] == parent.chunk_uid and BODY in row["text"] for row in spans)
    assert cost(spans) <= 900


@pytest.mark.parametrize("field,value", [("source_run_id", "other"), ("source", "other.pdf"), ("parent_chunk_uid", "v:other")])
def test_unrelated_child_is_not_promoted_by_its_identifier(field: str, value: str) -> None:
    parent, child = pair()
    change: dict[str, Any] = {field: value}
    wrong = replace(child, **change)
    diagnostics: list[dict[str, Any]] = []
    evidence_spans(
        "変更方法",
        [parent],
        budget=200,
        anchors=[wrong],
        scope_records=[wrong],
        priority_source_groups=[(wrong.chunk_uid,)],
        diagnostics=diagnostics,
    )
    assert diagnostics[0]["located_child_ids"] == []
    assert diagnostics[0]["body_status"] == "unlocated"


def test_insufficient_budget_is_reported_without_claiming_body_delivery() -> None:
    parent, child = pair()
    diagnostics: list[dict[str, Any]] = []
    spans = evidence_spans(
        "変更方法",
        [parent],
        budget=20,
        anchors=[child],
        scope_records=[child],
        priority_source_groups=[(child.chunk_uid,)],
        diagnostics=diagnostics,
    )
    assert spans == []
    assert diagnostics[0]["body_status"] == "missing"
    assert diagnostics[0]["selected_chars"] == 0
    assert diagnostics[0]["required_chars"] == len(BODY)


def test_explicit_priority_window_precedes_earlier_protected_metadata_text() -> None:
    heading = "変更方法の概要です。\n" * 20
    text = heading + BODY
    actual = evidence_excerpt("変更方法", text, 80, required_ranges=[(0, len(heading) - 1)], priority_ranges=[(len(heading), len(text))])
    assert BODY in actual
    assert len(actual) <= 80


def test_optional_prefix_cannot_consume_the_required_child_source_cost() -> None:
    parent, child = pair()
    diagnostics: list[dict[str, Any]] = []
    spans = evidence_spans(
        "変更方法",
        [parent],
        budget=185,
        anchors=[child],
        scope_records=[child],
        priority_source_groups=[(child.chunk_uid,)],
        diagnostics=diagnostics,
    )
    assert BODY in "".join(row["text"] for row in spans)
    assert diagnostics[0]["body_status"] == "complete"
    assert cost(spans) <= 185


def test_partial_group_does_not_claim_complete_from_a_child_identifier() -> None:
    parent, child = pair()
    second_body = "別の対象を選択し、内容を確認して保存します。"
    parent = replace(parent, text=parent.text + "\n" + second_body)
    second = replace(child, id="c2", chunk_uid="v:c2", chunk_id="c2", text=second_body)
    diagnostics: list[dict[str, Any]] = []
    spans = evidence_spans(
        "変更方法",
        [parent],
        budget=150,
        anchors=[child, second],
        scope_records=[child, second],
        priority_source_groups=[(child.chunk_uid, second.chunk_uid)],
        diagnostics=diagnostics,
    )
    assert diagnostics[0]["body_status"] == "partial"
    assert 0 < diagnostics[0]["selected_chars"] < diagnostics[0]["required_chars"]
    assert cost(spans) <= 150


def test_parent_source_priority_protects_its_verified_child() -> None:
    parent, child = pair()
    noise = replace(parent, id="noise", chunk_uid="v:noise", chunk_id="noise", text="補足。" * 200, metadata={})
    diagnostics: list[dict[str, Any]] = []
    spans = evidence_spans(
        "変更方法",
        [noise, parent],
        budget=350,
        anchors=[child],
        scope_records=[child],
        priority_source_groups=[(parent.chunk_uid,)],
        diagnostics=diagnostics,
    )
    assert BODY in "".join(row["text"] for row in spans)
    assert diagnostics[0]["located_child_ids"] == [child.chunk_uid]
    assert diagnostics[0]["body_status"] == "complete"


def test_same_physical_child_range_is_counted_once_with_all_located_aliases() -> None:
    parent, child = pair()
    alias = replace(child, id="alias", chunk_uid="v:alias", chunk_id="alias")
    diagnostics: list[dict[str, Any]] = []
    spans = evidence_spans(
        "変更方法",
        [parent],
        budget=500,
        anchors=[child, alias],
        scope_records=[child, alias],
        priority_source_groups=[(parent.chunk_uid,)],
        diagnostics=diagnostics,
    )
    assert BODY in "".join(row["text"] for row in spans)
    assert set(diagnostics[0]["located_child_ids"]) == {child.chunk_uid, alias.chunk_uid}
    assert diagnostics[0]["required_chars"] == len(BODY)
    assert diagnostics[0]["selected_chars"] == len(BODY)
    assert diagnostics[0]["body_status"] == "complete"


@pytest.mark.parametrize("budget,whole_expected", [(300, False), (1000, True)])
def test_whole_parent_uses_complete_source_cost_before_skipping_child_priority(budget: int, whole_expected: bool) -> None:
    parent, child = pair()
    parent = replace(parent, text="変更方法の概要です。\n" * 9 + BODY, metadata={"section_path": ["概要"]})
    diagnostics: list[dict[str, Any]] = []
    spans = evidence_spans(
        "変更方法",
        [parent],
        budget=budget,
        anchors=[child],
        scope_records=[child],
        priority_source_groups=[(parent.chunk_uid,)],
        whole_records=True,
        diagnostics=diagnostics,
    )
    assert BODY in "".join(row["text"] for row in spans)
    assert diagnostics[0]["body_status"] == "complete"
    assert cost(spans) <= budget
    if whole_expected:
        assert "".join(row["text"] for row in spans) == parent.text
