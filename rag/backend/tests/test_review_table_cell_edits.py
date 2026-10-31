"""REVIEW 中の表セルテキスト修正 `_apply_table_cell_edits` の単体テスト。"""

import pytest
from fastapi import HTTPException

from app.api.routes.documents import (
    _apply_table_cell_edits,
    _canonicalize_reviewed_extraction,
)
from app.rag.chunking import CHUNKING_STRATEGIES, chunk_extraction_with_strategy
from app.schemas.document import DocumentTableCellTextEdit
from app.schemas.extraction import (
    DocumentElement,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)


def _extraction_with_table() -> StructuredExtraction:
    return StructuredExtraction(
        raw_text="# 料金表\n説明\n| 項目 | 値 |\n| 交通費 | 1000円 |",
        elements=[
            DocumentElement(
                kind="title",
                text="# 料金表",
                order=0,
                element_id="title-1",
                page_number=1,
                bbox=[0, 0, 100, 10],
                section_path=["料金表"],
                metadata={"section_level": 1, "raw_start": 0, "raw_end": 5},
            ),
            DocumentElement(
                kind="text",
                text="説明",
                order=1,
                element_id="text-1",
                page_number=1,
                bbox=[0, 10, 100, 20],
                section_path=["料金表"],
            ),
            DocumentElement(
                kind="table",
                text="| 項目 | 値 |\n| 交通費 | 1000円 |",
                order=2,
                element_id="table-element-1",
                page_number=1,
                bbox=[0, 20, 100, 40],
                section_path=["料金表"],
                metadata={"table_id": "tbl-1"},
            ),
        ],
        pages=[
            ExtractionPage(
                page_number=1,
                element_ids=["title-1", "text-1", "table-element-1"],
            )
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="table-element-1",
                page_number=1,
                cells=[
                    ExtractionTableCell(row=0, col=0, text="項目", bbox=[0, 0, 50, 10]),
                    ExtractionTableCell(row=0, col=1, text="値", bbox=[50, 0, 100, 10]),
                    ExtractionTableCell(row=1, col=0, text="交通費", bbox=[0, 10, 50, 20]),
                    ExtractionTableCell(row=1, col=1, text="1000円", bbox=[50, 10, 100, 20]),
                ],
            )
        ],
    )


def test_apply_table_cell_edits_replaces_only_targeted_cell_text() -> None:
    """指定セルの text のみ差し替え、他セル・bbox・構造は保持する。"""
    extraction = _extraction_with_table()
    updated = _apply_table_cell_edits(
        extraction,
        [
            DocumentTableCellTextEdit(
                table_id="tbl-1", row=1, col=1, text="2000円", base_text="1000円"
            )
        ],
    )

    cells = {(c.row, c.col): c for c in updated.tables[0].cells}
    assert cells[(1, 1)].text == "2000円"
    assert cells[(1, 1)].bbox == [50, 10, 100, 20]
    # 他セルは不変。
    assert cells[(0, 0)].text == "項目"
    assert cells[(1, 0)].text == "交通費"


def test_apply_table_cell_edits_rejects_unknown_cell() -> None:
    """存在しない表セルの修正は 400。"""
    extraction = _extraction_with_table()
    with pytest.raises(HTTPException) as exc_info:
        _apply_table_cell_edits(
            extraction,
            [DocumentTableCellTextEdit(table_id="tbl-1", row=9, col=9, text="x", base_text="")],
        )
    assert exc_info.value.status_code == 400


def test_apply_table_cell_edits_rejects_unknown_table() -> None:
    """存在しない table_id の修正は 400。"""
    extraction = _extraction_with_table()
    with pytest.raises(HTTPException) as exc_info:
        _apply_table_cell_edits(
            extraction,
            [DocumentTableCellTextEdit(table_id="missing", row=0, col=0, text="x", base_text="")],
        )
    assert exc_info.value.status_code == 400


def test_canonicalize_reviewed_extraction_syncs_structure_and_raw_text() -> None:
    """要素・表セル修正から章節、offset、表要素、raw_text を一貫して再生成する。"""
    extraction = _extraction_with_table()
    elements = [
        (
            element.model_copy(update={"text": "# 新しい料金表"})
            if element.element_id == "title-1"
            else element
        )
        for element in extraction.elements
    ]
    edited = _apply_table_cell_edits(
        extraction.model_copy(update={"elements": elements}),
        [
            DocumentTableCellTextEdit(
                table_id="tbl-1", row=1, col=1, text="2000円", base_text="1000円"
            )
        ],
    )

    normalized = _canonicalize_reviewed_extraction(edited)

    table_element = next(
        element for element in normalized.elements if element.element_id == "table-element-1"
    )
    assert "2000円" in table_element.text
    assert "2000円" in normalized.raw_text
    assert table_element.bbox == [0.0, 20.0, 100.0, 40.0]
    assert normalized.tables[0].cells[3].bbox == [50.0, 10.0, 100.0, 20.0]
    assert all(element.section_path == ["新しい料金表"] for element in normalized.elements)
    assert [node.section_path for node in normalized.navigation] == [["新しい料金表"]]
    assert normalized.pages[0].element_ids == ["title-1", "text-1", "table-element-1"]
    for element in normalized.elements:
        start = element.metadata.get("raw_start")
        end = element.metadata.get("raw_end")
        assert isinstance(start, int)
        assert isinstance(end, int)
        assert normalized.raw_text[start:end] == element.text


@pytest.mark.parametrize("strategy", CHUNKING_STRATEGIES)
def test_saved_review_text_is_used_by_every_chunk_strategy(strategy: str) -> None:
    """保存後の構造化要素を全 Chunk 戦略の入力として利用する。"""
    edited = _apply_table_cell_edits(
        _extraction_with_table(),
        [
            DocumentTableCellTextEdit(
                table_id="tbl-1", row=1, col=1, text="2000円", base_text="1000円"
            )
        ],
    )
    normalized = _canonicalize_reviewed_extraction(edited)

    chunks = chunk_extraction_with_strategy(
        normalized,
        strategy=strategy,
        chunk_size=200,
        overlap=0,
    )

    assert chunks, strategy
    assert "2000円" in "\n".join(chunk.text for chunk in chunks), strategy
