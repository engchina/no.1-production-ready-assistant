"""MinerU の解析結果のページの境目の続きの整理(#2266)。

record は合成の内容(MinerU の Middle JSON を mineru_layout が写した形)。
"""

from __future__ import annotations

from typing import Any, cast

from app.rag.pdf_segment_merge import (
    CONSOLIDATION_ARTIFACT,
    consolidate_document_extraction,
    extraction_with_page_offset,
    merge_segment_extractions,
)
from app.schemas.extraction import DocumentElement, ExtractionTable, StructuredExtraction

_CATEGORIES = {
    "text": "Text",
    "ref_text": "Text",
    "list": "List-item",
    "paragraph_title": "Section-header",
    "image": "Picture",
    "table": "Table",
    "header": "Page-header",
    "footer": "Page-footer",
    "page_footnote": "Footnote",
}
_HEADER_TABLE = (
    "<table><tbody><tr><th>項目</th><th>値</th></tr><tr><td>A</td><td>1</td></tr></tbody></table>"
)
_BODY_TABLE = "<table><tbody><tr><td>B</td><td>2</td></tr></tbody></table>"


def _block(
    page: int,
    index: int,
    mineru_type: str,
    text: str = "",
    *,
    local_page: int | None = None,
    continues_prev: bool = False,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "mineru_type": mineru_type,
        "mineru_page_idx": page - 1 if local_page is None else local_page,
        "mineru_block_index": index,
    }
    if continues_prev:
        raw["continues_prev"] = True
    return {
        "id": f"mineru-p{page}-b{index}",
        "engine": "mineru",
        "page": page,
        "seq_no": index + 1,
        "bbox": [100.0, 200.0, 2000.0, 300.0],
        "coord_system": "image_top_left",
        "page_width": 2480.0,
        "page_height": 3508.0,
        "category": _CATEGORIES[mineru_type],
        "text": text,
        "raw": raw,
    }


def _extraction(records: list[dict[str, Any]]) -> StructuredExtraction:
    return StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table" if r["category"] == "Table" else "text",
                text=r["text"] or "図",
                element_id=r["id"],
                page_number=r["page"],
            )
            for r in records
            if r["category"] not in {"Page-header", "Page-footer"}
        ],
        tables=[
            ExtractionTable(
                table_id=f"mineru-table-{r['id']}", element_id=r["id"], page_number=r["page"]
            )
            for r in records
            if r["category"] == "Table"
        ],
        parser_artifacts={"layout_records": {"version": 1, "pages": [], "records": records}},
    )


def _records(extraction: StructuredExtraction) -> dict[str, dict[str, Any]]:
    layout = cast(dict[str, Any], extraction.parser_artifacts["layout_records"])
    return {record["id"]: record for record in layout["records"]}


def _report(extraction: StructuredExtraction) -> dict[str, Any]:
    return cast(dict[str, Any], extraction.parser_artifacts[CONSOLIDATION_ARTIFACT])


def test_mineru_continues_prev_maps_to_previous_text_across_picture_and_page_furniture() -> None:
    """図・脚注・ページの定型をまたぐ MinerU の本文の続きを、前の本文の record に写す。"""
    extraction = _extraction(
        [
            _block(1, 0, "text", "前のページの本文は文の途中で"),
            _block(1, 1, "image"),
            _block(1, 2, "page_footnote", "注の本文"),
            _block(1, 3, "footer", "1"),
            _block(2, 0, "header", "柱"),
            _block(2, 1, "text", "次のページへ続きます。", continues_prev=True),
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=b"")

    records = _records(result)
    assert records["mineru-p2-b1"]["raw"]["continues_record_id"] == "mineru-p1-b0"
    element = next(e for e in result.elements if e.element_id == "mineru-p2-b1")
    assert element.metadata["continues_element_id"] == "mineru-p1-b0"
    assert _report(result)["continued_paragraphs"] == 1


def test_mineru_text_continuation_stops_at_heading() -> None:
    extraction = _extraction(
        [
            _block(1, 0, "text", "前の本文"),
            _block(1, 1, "paragraph_title", "2. 見出し"),
            _block(2, 0, "text", "続きの印のある本文", continues_prev=True),
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=b"")

    assert "continues_record_id" not in _records(result)["mineru-p2-b0"]["raw"]


def test_mineru_table_continuation_carries_header_and_uses_table_id() -> None:
    extraction = _extraction(
        [
            _block(1, 0, "table", _HEADER_TABLE),
            _block(1, 1, "footer", "1"),
            _block(2, 0, "table", _BODY_TABLE, continues_prev=True),
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=b"")

    raw = _records(result)["mineru-p2-b0"]["raw"]
    assert raw["continues_record_id"] == "mineru-p1-b0"
    assert "<th>項目</th>" in raw["table_enhanced_html"]
    table = next(t for t in result.tables if t.element_id == "mineru-p2-b0")
    assert table.metadata["continued_from_table_id"] == "mineru-table-mineru-p1-b0"
    report = _report(result)
    assert (report["continued_tables"], report["table_headers_carried"]) == (1, 1)


def test_mineru_judged_boundary_without_mark_is_left_as_is() -> None:
    """MinerU が両方のページを見た境目は、印が無ければ(文が途中でも)つながない。"""
    extraction = _extraction(
        [
            _block(1, 0, "text", "文が次のページへ"),
            _block(2, 0, "text", "続いているように見える本文。"),
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=b"")

    assert "continues_record_id" not in _records(result)["mineru-p2-b0"]["raw"]


def test_mineru_segment_boundary_uses_layout_rule() -> None:
    """段ごとの解析の段の境目(MinerU が見ていない境目)は、Docling と同じ規則で続きを判定する。"""
    first = _extraction(
        [
            _block(1, 0, "text", "段の終わりの本文は文の途中で"),
            _block(2, 0, "text", "前の段の本文。"),
        ]
    )
    second = _extraction(
        [_block(1, 0, "text", "次の段へ続きます。"), _block(2, 0, "text", "文が途中で")]
    )
    merged = merge_segment_extractions(
        [
            (1, extraction_with_page_offset(first, page_start=1, page_end=2)),
            (3, extraction_with_page_offset(second, page_start=3, page_end=4)),
        ]
    )
    records = _records(merged)
    # 段の中のページ(mineru_page_idx)は段ごとに 0 から。
    assert records["p3-mineru-p1-b0"]["page"] == 3

    result = consolidate_document_extraction(merged, source_bytes=b"")

    records = _records(result)
    # 2 → 3 ページは段の境目。前の本文が文の終わりで終わるのでつながない。
    assert "continues_record_id" not in records["p3-mineru-p1-b0"]["raw"]
    # 段の中の 1 → 2 ページは MinerU の判定(印が無い)のまま。
    assert "continues_record_id" not in records["mineru-p2-b0"]["raw"]

    unfinished = _extraction(
        [_block(1, 0, "text", "前の段"), _block(2, 0, "text", "段の終わりの本文は文の途中で")]
    )
    merged = merge_segment_extractions(
        [
            (1, extraction_with_page_offset(unfinished, page_start=1, page_end=2)),
            (3, extraction_with_page_offset(second, page_start=3, page_end=4)),
        ]
    )
    result = consolidate_document_extraction(merged, source_bytes=b"")
    assert _records(result)["p3-mineru-p1-b0"]["raw"]["continues_record_id"] == "mineru-p2-b0"


def test_mineru_consolidation_is_idempotent_and_skips_mixed_engines() -> None:
    extraction = _extraction(
        [
            _block(1, 0, "text", "前のページの本文は文の途中で"),
            _block(2, 0, "text", "次のページへ続きます。", continues_prev=True),
            _block(2, 1, "table", _HEADER_TABLE),
            _block(3, 0, "table", _BODY_TABLE, continues_prev=True),
        ]
    )
    once = consolidate_document_extraction(extraction, source_bytes=b"")
    twice = consolidate_document_extraction(once, source_bytes=b"")
    assert twice.parser_artifacts == once.parser_artifacts
    assert (_report(once)["continued_paragraphs"], _report(once)["continued_tables"]) == (1, 1)

    mixed_records = [
        _block(1, 0, "text", "文が次のページへ"),
        {**_block(2, 0, "text", "続いています。"), "engine": "docling"},
    ]
    mixed = _extraction(mixed_records)
    assert consolidate_document_extraction(mixed, source_bytes=b"") == mixed
