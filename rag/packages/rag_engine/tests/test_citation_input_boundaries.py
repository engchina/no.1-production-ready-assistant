"""引用入力の構造・親子境界と表示番号を匿名の原文で確かめる。外部I/Oなし。"""

from dataclasses import replace

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.answer_models import AnswerRecord
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.models.llm import GroundedCitation, GroundedItem
from rag_engine.retrieval.evidence_selection import evidence_spans


def record(text: str, *, key: str = "parent") -> AnswerRecord:
    return AnswerRecord(
        id=key,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source="synthetic.pdf",
        source_run_id="run-a",
        chunk_id=key,
        chunk_uid=f"v:{key}",
        chunk_level="parent",
    )


def child(parent: AnswerRecord, text: str, key: str) -> AnswerRecord:
    return replace(parent, id=key, chunk_id=key, chunk_uid=f"v:{key}", chunk_level="child", parent_chunk_uid=parent.chunk_uid, text=text)


@pytest.mark.parametrize("text", ["=" * 99, "---", "===\n___", "|---|---|", "***\n```\n---"])
def test_structure_only_record_is_not_an_independent_evidence_span(text: str) -> None:
    diagnostics: list[dict] = []
    assert evidence_spans("操作方法", [record(text)], budget=4000, diagnostics=diagnostics) == []
    assert any(row.get("reason_code") == "structural_only" for row in diagnostics)


def test_structure_only_parent_fragment_is_excluded_without_moving_other_offsets() -> None:
    first, separator, last = "対象を選択します。", "\n---\n", "設定を保存します。"
    parent = record(first + separator + last)
    siblings = [child(parent, first, "first"), child(parent, separator.strip(), "separator"), child(parent, last, "last")]
    spans = evidence_spans("操作方法", [parent], scope_records=siblings, budget=4000, whole_records=True)
    assert not any(row["text"].strip() == "---" for row in spans)
    for text in (first, last):
        span = next(row for row in spans if text in row["text"])
        assert parent.text[span["start"] : span["end"]] == span["text"]
    assert "[…]" not in "".join(row["text"] for row in spans)


@pytest.mark.parametrize(
    "text", ["保存", "0", "100", "-1", "A", "=", "==", ">", "○", "→", "≠", "| 項目 | 値 |\n| --- | --- |\n| 件数 | 0 |"]
)
def test_short_labels_values_and_table_data_remain_quotable(text: str) -> None:
    spans = evidence_spans("表示内容", [record(text)], budget=4000, whole_records=True)
    assert [row["text"] for row in spans] == [text]
    spans = grounded.tag_spans("表示内容", spans)
    grounded.format_functions(spans)
    count = len(grounded._citation_view(spans[0]).units)
    lines = "1" if count == 1 else f"1-{count}"
    proposal = GroundedItem(kind="rule", text=text, citations=[GroundedCitation(evidence_id=spans[0]["label"], lines=lines)])
    cited, error = grounded.cited_quote(proposal, spans)
    assert not error and cited.quote == text


def test_sibling_offsets_display_labels_and_line_ranges_bind_the_same_original_text() -> None:
    bodies = ["対象Aの選択を確認します。" * 24, "対象Bの設定を確認します。" * 24, "対象Cの結果を確認します。" * 24]
    parent = record("\n---\n".join(bodies))
    siblings = [child(parent, body, f"child-{index}") for index, body in enumerate(bodies)]
    spans = evidence_spans("操作方法", [parent], scope_records=siblings, budget=12000, whole_records=True)
    spans = grounded.tag_spans("操作方法", spans)
    display = grounded.format_functions(spans)
    assert len({row["label"] for row in spans}) == len(spans)
    for body in bodies:
        span = next(row for row in spans if body in row["text"])
        assert parent.text[span["start"] : span["end"]] == span["text"]
        assert span["label"] in display
        units = grounded.citation_units(span["text"])
        assert len(units) >= 2
        wire = generation_draft_wire_model().model_validate(
            {
                "items": [
                    {"kind": "rule", "text": "合成説明", "citations": [{"evidence_id": span["label"], "line_start": 2, "line_end": 2}]}
                ],
                "summary": "",
                "confidence": "low",
            }
        )
        proposal = canonical_generation_draft(wire).items[0]
        cited, error = grounded.cited_quote(proposal, spans)
        assert not error and cited.quote == span["text"][units[1][0] : units[1][1]]
        assert cited.quote in parent.text
        assert cited.citations[0]._extraction["resolved_evidence_id"] == span["evidence_id"]
        assert cited.citations[0]._extraction["requested_evidence_id"] == span["label"]


def test_child_ending_inside_a_sentence_does_not_publish_an_independent_tail() -> None:
    sentence, next_sentence = "対象を選択します。", "設定を保存します。"
    parent = record(sentence + "\n" + next_sentence)
    # 子の文字列は実際に親に含まれるが、境界が述語の途中で切れている。
    incomplete = child(parent, "対象を選択し", "partial")
    spans = evidence_spans("操作方法", [parent], scope_records=[incomplete], budget=4000, whole_records=True)
    assert not any(row["text"].strip() == "ます。" for row in spans)
    assert any(sentence in row["text"] for row in spans)
    assert next_sentence in "".join(row["text"] for row in spans)
