"""確認の保存から実際の Docling / MinerU 親子分割へ訂正を渡す。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from app.api.routes.documents import _reviewed_extraction_with_edits
from app.rag.chunking_small_to_big import build_parent_child_chunks
from app.schemas.document import (
    DocumentElementTextEdit,
    DocumentReviewEditsRequest,
    DocumentTableCellTextEdit,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)


def _layout(extraction: StructuredExtraction) -> dict[str, Any]:
    return cast(dict[str, Any], extraction.parser_artifacts["layout_records"])


def _source(engine: str) -> StructuredExtraction:
    heading = "Heading OLD"
    body = "The recorded body value is old-body. " * 8
    html = (
        "<table><tr><th colspan='2'>Columns</th></tr><tr><td>A</td><td>old-cell</td></tr></table>"
    )
    records: list[dict[str, Any]] = []
    elements = []
    for index, (key, kind, category, text, box) in enumerate(
        [
            ("title-1", "title", "Section-header", heading, [40, 40, 500, 60]),
            ("body-1", "text", "Text", body, [40, 70, 500, 140]),
            ("table-1", "table", "Table", html, [40, 150, 500, 220]),
        ]
    ):
        elements.append(
            DocumentElement(
                kind=kind,
                text=text,
                element_id=key,
                page_number=1,
                order=index,
                bbox=box,
                section_path=[heading],
                metadata={"section_level": 1}
                if kind == "title"
                else {"table_id": "table-1"}
                if kind == "table"
                else {},
            )
        )
        records.append(
            {
                "id": key,
                "engine": engine,
                "page": 1,
                "seq_no": index,
                "category": category,
                "text": text,
                "bbox": box,
                "page_width": 600,
                "page_height": 800,
                "coord_system": "image_top_left",
                "raw": {
                    "orig": {"parser_text": text},
                    **(
                        {"heading_level": 1}
                        if kind == "title"
                        else {
                            "table_enhanced_html": html,
                            "table_vision_text": "Independent figure description.",
                        }
                        if kind == "table"
                        else {}
                    ),
                },
            }
        )
    return StructuredExtraction(
        raw_text="\n".join(element.text for element in elements),
        elements=elements,
        pages=[
            ExtractionPage(
                page_number=1, element_ids=[e.element_id for e in elements if e.element_id]
            )
        ],
        tables=[
            ExtractionTable(
                table_id="table-1",
                element_id="table-1",
                page_number=1,
                cells=[
                    ExtractionTableCell(
                        row=0, col=0, col_span=2, text="Columns", bbox=[0, 0, 460, 35]
                    ),
                    ExtractionTableCell(row=1, col=0, text="A", bbox=[0, 35, 230, 70]),
                    ExtractionTableCell(row=1, col=1, text="old-cell", bbox=[230, 35, 460, 70]),
                ],
            )
        ],
        parser_artifacts={
            "layout_records": {
                "records": records,
                "pages": [{"page": 1, "width": 600, "height": 800}],
            }
        },
    )


@pytest.mark.parametrize("engine", ["docling", "mineru"])
def test_review_updates_real_parent_child_body_headers_cells_and_search_text(engine: str) -> None:
    before = _source(engine)
    original = deepcopy(before.model_dump())
    body = before.elements[1].text.replace("old-body", "new-body")
    after = _reviewed_extraction_with_edits(
        before,
        DocumentReviewEditsRequest(
            element_edits=[
                DocumentElementTextEdit(
                    element_id="title-1", base_text="Heading OLD", text="Heading NEW"
                ),
                DocumentElementTextEdit(
                    element_id="body-1", base_text=before.elements[1].text, text=body
                ),
            ],
            table_cell_edits=[
                DocumentTableCellTextEdit(
                    table_id="table-1", row=1, col=1, base_text="old-cell", text="new-cell"
                )
            ],
        ),
    )
    chunks = build_parent_child_chunks(after, source_name="synthetic.pdf")
    assert chunks
    visible = "\n".join(
        str(part)
        for chunk in chunks
        for part in (
            chunk.text,
            chunk.metadata.get("parent_text", ""),
            chunk.metadata.get("engine_search_text", ""),
        )
    )
    for value in ("new-body", "new-cell", "Heading NEW", "Independent figure description."):
        assert value in visible
    for value in ("old-body", "old-cell", "Heading OLD"):
        assert value not in visible
    assert all("Heading OLD" not in str(c.metadata.get("section_path", "")) for c in chunks)
    assert before.model_dump() == original
    records = _layout(after)["records"]
    previous = _layout(before)["records"]
    for current, source in zip(records, previous, strict=True):
        assert current["bbox"] == source["bbox"]
        assert current["raw"]["orig"] == source["raw"]["orig"]
        assert current["raw"]["review_original_text"] == source["text"]
    assert after.tables[0].cells[0].col_span == 2
    assert after.tables[0].cells[2].bbox == before.tables[0].cells[2].bbox
    assert _layout(after)["pages"] == _layout(before)["pages"]


def test_second_review_keeps_the_parser_original_and_uses_the_latest_text() -> None:
    before = _source("docling")
    first = _reviewed_extraction_with_edits(
        before,
        DocumentReviewEditsRequest(
            element_edits=[
                DocumentElementTextEdit(
                    element_id="body-1",
                    base_text=before.elements[1].text,
                    text="First reviewed body. " * 8,
                )
            ]
        ),
    )
    second = _reviewed_extraction_with_edits(
        first,
        DocumentReviewEditsRequest(
            element_edits=[
                DocumentElementTextEdit(
                    element_id="body-1",
                    base_text=first.elements[1].text,
                    text="Second reviewed body. " * 8,
                )
            ]
        ),
    )
    assert "Second reviewed body" in "\n".join(
        c.text for c in build_parent_child_chunks(second, source_name="synthetic.pdf")
    )
    records = _layout(second)["records"]
    assert records[1]["raw"]["review_original_text"] == _layout(before)["records"][1]["text"]
    assert _layout(first)["records"][2] == _layout(before)["records"][2]
    assert first.elements[1].text.startswith("First reviewed")
