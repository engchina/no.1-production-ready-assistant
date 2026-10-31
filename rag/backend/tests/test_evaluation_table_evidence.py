"""HTML 表の必要根拠は、同じ行の表示本文と位置条件を照合する。"""

import pytest

from app.rag.evaluation import evidence_matches_chunk
from app.schemas.evaluation import EvaluationEvidence
from app.schemas.search import RetrievedChunk


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-a",
        chunk_id="chunk-a",
        text=text,
        score=1.0,
        metadata={"page_start": 2, "page_end": 2},
    )


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<table><tr><td>行 A</td><td>10</td><td>20</td></tr></table>", "行 A 10 20"),
        ("<TABLE><TR><TD>A &amp; B</TD><TD><b>10</b><br>円</TD></TR></TABLE>", "A & B 10 円"),
        ("<table><tr><th colspan='2'>行 A</th><td>10</td></tr></table>", "行 A 10"),
        ("<table><tr><td>行 A<td>10<td>20</table>", "行 A 10 20"),
        ("本文の条件 42。<table><tr><td>別の値</td></tr></table>", "本文の条件 42"),
        ("<table><tr><td>別の値</td></tr></table>末尾の条件 43。", "末尾の条件 43"),
    ],
)
def test_visible_table_row_matches(html: str, expected: str) -> None:
    assert evidence_matches_chunk(
        EvaluationEvidence(id="e", document_id="doc-a", page_start=2, text=expected),
        _chunk(html),
    )


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<table><tr><td>行 A</td><td>10</td><td>20</td></tr></table>", "行 A 11 20"),
        ("<table><tr><td>行 A</td><td>10</td><td>20</td></tr></table>", "行 A 20 10"),
        ("<table><tr><td>行 A</td><td>10</td></tr><tr><td>20</td></tr></table>", "行 A 10 20"),
        ("<table><tr><td title='行 A 10 20'>別の値</td></tr></table>", "行 A 10 20"),
        ("<div>行 A</div><div>10</div><div>20</div>", "行 A 10 20"),
        ("行 A | 10 | 20", "行 A 10 20"),
        ("行 A<table><tr><td>別の値</td></tr></table>10 20", "行 A 10 20"),
    ],
)
def test_different_or_unrelated_text_does_not_match(html: str, expected: str) -> None:
    assert not evidence_matches_chunk(EvaluationEvidence(id="e", text=expected), _chunk(html))


@pytest.mark.parametrize("condition", [{"document_id": "other"}, {"page_start": 3}])
def test_table_match_still_requires_document_and_page(condition: dict[str, object]) -> None:
    evidence = EvaluationEvidence.model_validate({"id": "e", "text": "行 A 10", **condition})
    assert not evidence_matches_chunk(
        evidence, _chunk("<table><tr><td>行 A</td><td>10</td></tr></table>")
    )


def _row_chunk(metadata: dict[str, object]) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-a", chunk_id="chunk-r", text="合成の行", score=1.0, metadata=metadata
    )


PARSED_ROW = {
    "row_basis": "parsed_table",
    "table_index": 1,
    "table_row_start": 3,
    "table_row_end": 3,
}
SHEET_ROW = {"sheet_name": "シート 1", "row_start": 3, "row_end": 3}
# CSV の記録の番号（table_row_start）と原本の物理行（row_start。引用符の中の改行で 2 行）。
CSV_ROW = {
    "row_basis": "csv",
    "table_row_start": 3,
    "table_row_end": 3,
    "row_start": 4,
    "row_end": 5,
}


@pytest.mark.parametrize(
    ("condition", "metadata", "expected"),
    [
        # 基準を書かない行は原本の行（解析の表の格子の行には一致させない）。
        ({}, PARSED_ROW, False),
        ({}, SHEET_ROW, True),
        # 基準を書けば、その基準の行だけに一致する（#3547）。
        ({"row_basis": "parsed_table"}, PARSED_ROW, True),
        ({"row_basis": "parsed_table"}, SHEET_ROW, False),
        ({"row_basis": "sheet"}, SHEET_ROW, True),
        # CSV の行は MCP の根拠の場所と同じ記録の番号で照合する（物理行ではない）。
        ({"row_basis": "csv"}, CSV_ROW, True),
        ({"row_basis": "csv", "row_start": 4}, CSV_ROW, False),
    ],
    ids=[
        "default-not-parsed",
        "default-sheet",
        "parsed",
        "parsed-not-sheet",
        "sheet",
        "csv-record",
        "csv-not-physical-line",
    ],
)
def test_row_condition_matches_rows_of_the_same_basis(
    condition: dict[str, object], metadata: dict[str, object], expected: bool
) -> None:
    evidence = EvaluationEvidence.model_validate({"id": "e", "row_start": 3, **condition})
    assert evidence_matches_chunk(evidence, _row_chunk(metadata)) is expected


def test_row_basis_needs_a_row() -> None:
    with pytest.raises(ValueError, match="row_basis"):
        EvaluationEvidence.model_validate({"id": "e", "text": "合成", "row_basis": "csv"})
