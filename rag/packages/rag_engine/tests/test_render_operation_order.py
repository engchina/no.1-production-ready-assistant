"""文書を往復する操作の順序と、表示順の出典対応を保持する（#3767）。"""

import pytest

from rag_engine.generation import grounded as g
from rag_engine.models.llm import GroundedCitation, GroundedItem


def entry(text, doc, index, kind="operation"):
    span = {
        "function": (doc, "機能" + doc),
        "document_scope": [doc],
        "source": doc + ".pdf",
        "page": 1,
        "source_id": doc + str(index),
        "evidence_id": "E" + str(index),
        "text": text,
    }
    return g.CheckedItem(
        GroundedItem(kind=kind, text=text, citations=[GroundedCitation(evidence_id=span["evidence_id"], quote=text)]), span
    )


def test_cross_document_round_trip_keeps_operational_and_citation_order():
    items = [entry("最初の操作。", "A", 1), entry("中間の操作。", "B", 2), entry("最後の操作。", "A", 3)]
    result = g.render_answer("", items)
    assert result.text.index("最初の操作") < result.text.index("中間の操作") < result.text.index("最後の操作")
    assert [row["source_id"] for row in result.citation_lines] == ["A1", "B2", "A3"]
    assert result.text.count("操作手順（機能A）") == 2


def test_adjacent_same_function_remains_one_numbered_section():
    result = g.render_answer("", [entry("選択の操作。", "A", 1), entry("入力の操作。", "A", 2)])
    assert result.text.count("操作手順（機能A）") == 1
    assert "1. 選択の操作。" in result.text and "2. 入力の操作。" in result.text


def test_document_boundary_is_not_erased_when_function_names_match():
    items = [entry("選択の操作。", "A", 1), entry("入力の操作。", "B", 2)]
    for item in items:
        item.span["function"] = ("共通名",)
    result = g.render_answer("", items)
    assert result.text.count("操作手順（共通名）") == 2
    assert [row["source_id"] for row in result.citation_lines] == ["A1", "B2"]


@pytest.mark.parametrize("quote_only", [False, True])
def test_rule_and_reference_grouping_remains_document_based(quote_only):
    items = [entry("A1の説明です。", "A", 1, "rule"), entry("B1の説明です。", "B", 2, "rule"), entry("A2の説明です。", "A", 3, "rule")]
    for item in items:
        item.quote_only = quote_only
    result = g.render_answer("", items)
    assert result.text.index("A1の説明") < result.text.index("A2の説明") < result.text.index("B1の説明")
