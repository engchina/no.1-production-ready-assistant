"""PDF・HTML・画像の表の根拠の、解析の表の格子の行（row_basis=parsed_table。#3429）。

PDF は reportlab で作った合成の表（``fixtures.synthetic_pdf.merged_header_table_pdf``。多段の表頭と
結合セル）を、Docling の解析のサービス（``rag/services/parsers/docling``）で解析した出力の記録
（``fixtures/docling_merged_header_table.json``。作業の場所の path・図の切り出し・grid を除いた
``StructuredExtraction``）から確かめる。記録は Docling 2.132.0 の CPU の解析で作った（作り直すときは
同じ PDF をサービスの ``app.extraction.analyze_source`` に通す）。Docling はテストの環境に入れない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from rag_engine.parsing.rendering import pdf_text_lines_in_bbox
from rag_parser_core.extraction import (
    DocumentElement,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from rag_parser_core.parsed_table import PARSED_TABLE_GRID_KEY, grid_rows_metadata
from rag_parser_core.registry import _html_result
from rag_pipeline_core.chunking import chunk_extraction

from app.mcp.tools import EvidenceLocator, _locator
from app.rag import pdf_segment_merge
from app.rag.chunking_small_to_big import _layout_tables, build_parent_child_chunks
from app.rag.chunking_strategy import SmallToBigParams
from tests.fixtures.synthetic_pdf import MERGED_HEADER_TABLE_ROWS, merged_header_table_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "docling_merged_header_table.json"


def _docling_extraction() -> StructuredExtraction:
    return StructuredExtraction.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _layout_records() -> list[dict[str, Any]]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = payload["parser_artifacts"]["layout_records"]["records"]
    return records


def _table_locators(chunks: list[Any]) -> list[EvidenceLocator]:
    return [
        _locator(chunk.metadata)
        for chunk in chunks
        if chunk.metadata.get("content_kind") == "table"
    ]


def test_recorded_docling_table_matches_the_synthetic_pdf(tmp_path: Path) -> None:
    """記録の表は合成の PDF の表（text layer の行）と同じ値を持つ（記録が PDF とずれていない）。"""
    pdf = tmp_path / "table.pdf"
    pdf.write_bytes(merged_header_table_pdf())
    table = next(record for record in _layout_records() if record["category"] == "Table")
    lines = pdf_text_lines_in_bbox(pdf, 1, table["raw"]["prov"][0]["bbox"])
    # 本文の行の値（縦に結合した「Group」の列は行の間に描くので除く）。
    for row in MERGED_HEADER_TABLE_ROWS[2:]:
        values = [value for value in row[1:] if value]
        assert any(all(value in line for value in values) for line in lines), row
    cells = {
        (cell["start_row_offset_idx"], cell["start_col_offset_idx"]): cell
        for cell in table["raw"]["data"]["table_cells"]
    }
    # Docling は 2 段の表頭（列の見出し）と、表頭・本文の結合セルを格子で返している。
    assert cells[(0, 2)]["text"] == "H1" and cells[(0, 2)]["end_col_offset_idx"] == 4
    assert cells[(0, 0)]["end_row_offset_idx"] == 2
    assert cells[(1, 3)]["text"] == "Actual" and cells[(1, 3)]["column_header"] is True
    assert cells[(2, 0)]["text"] == "G1" and cells[(2, 0)]["end_row_offset_idx"] == 4


def test_docling_table_child_has_the_parsed_grid_rows() -> None:
    """親子階層の表の子は、解析の表の番号と格子の行（表頭の 2 段も数える）を持つ。"""
    whole = _table_locators(build_parent_child_chunks(_docling_extraction()))
    assert [(loc.row_basis, loc.table_index, loc.row_start, loc.row_end) for loc in whole] == [
        ("parsed_table", 1, 1, 6)
    ]
    assert whole[0].table_row is None and whole[0].table_cells == []
    # 表を行のまとまりに分けると、各まとまりのデータの行（先頭の列見出しは数えない）。
    groups = _table_locators(
        build_parent_child_chunks(
            _docling_extraction(), params=SmallToBigParams(table_child_target_chars=300)
        )
    )
    assert [(loc.row_basis, loc.row_start, loc.row_end) for loc in groups] == [
        ("parsed_table", 3, 4),
        ("parsed_table", 5, 6),
    ]


def test_docling_single_data_row_has_cells_with_header_path_and_region() -> None:
    """1 つのデータの行は、列・2 段の表頭・Docling のセルの領域を持つ（結合セルは左上の行だけ）。"""
    table = next(iter(_layout_tables(_layout_records()).values()))
    table_index, grid = table.table_index, table.grid
    assert (table_index, grid.row_count, grid.header_rows) == (1, 6, 2)
    first = _locator(grid_rows_metadata(grid, table_index, [3]))
    assert (first.row_basis, first.row_start, first.row_end, first.table_row) == (
        "parsed_table",
        3,
        3,
        3,
    )
    assert [(cell.table_col, cell.header_path) for cell in first.table_cells] == [
        ("A", ["Group"]),
        ("B", ["Item"]),
        ("C", ["H1", "Plan"]),
        ("D", ["H1", "Actual"]),
        ("E", ["H2", "Plan"]),
        ("F", ["H2", "Actual"]),
    ]
    assert all(cell.bbox is not None and len(cell.bbox) == 4 for cell in first.table_cells)
    # 上の行から結合で続くセル（G1）は、続きの行の座標にしない。
    second = _locator(grid_rows_metadata(grid, table_index, [4]))
    assert [cell.table_col for cell in second.table_cells] == ["B", "C", "D", "E", "F"]


def test_docling_structure_chunk_has_rows_only_when_the_text_rows_are_known() -> None:
    """構造認識の分割: 表全体の chunk は格子の行を持つ。"""
    whole = _table_locators(chunk_extraction(_docling_extraction(), chunk_size=2000, overlap=0))
    assert [(loc.row_basis, loc.table_index, loc.row_start, loc.row_end) for loc in whole] == [
        ("parsed_table", 1, 1, 6)
    ]


def _docling_table_chunks(chunk_size: int) -> list[Any]:
    chunks = chunk_extraction(_docling_extraction(), chunk_size=chunk_size, overlap=0)
    return [chunk for chunk in chunks if chunk.metadata.get("content_kind") == "table"]


def test_docling_structure_splits_a_long_html_table_at_row_boundaries() -> None:
    """長い HTML の表は tr の境目で分け、各部分は 2 段の表頭を繰り返した表で格子の行を持つ。

    最初の部分は表頭の 2 段を含めて先頭から、続きの部分は繰り返した表頭を数えずにデータの行を
    数える（#3547）。
    """
    chunks = _docling_table_chunks(420)
    locators = [_locator(chunk.metadata) for chunk in chunks]
    assert [(loc.row_basis, loc.table_index, loc.row_start, loc.row_end) for loc in locators] == [
        ("parsed_table", 1, 1, 4),
        ("parsed_table", 1, 5, 6),
    ]
    for chunk in chunks:
        # 各部分は 1 つの表として閉じ、表頭の 2 段を先頭に持つ。
        assert chunk.text.startswith('<table><tr><th rowspan="2">Group</th>')
        assert chunk.text.endswith("</table>") and chunk.text.count("<table") == 1
        assert "<th>Plan</th><th>Actual</th>" in chunk.text
    assert [chunk.metadata["table_header_repeated"] for chunk in chunks] == [False, True]
    assert [
        (chunk.metadata["table_data_row_start"], chunk.metadata["table_data_row_end"])
        for chunk in chunks
    ] == [(1, 2), (3, 4)]
    # 1 つのデータの行だけの部分はセルの座標を持つ（上の行から結合で続くセルは座標にしない）。
    single = [_locator(chunk.metadata) for chunk in _docling_table_chunks(300)]
    assert [(loc.row_start, loc.row_end, loc.table_row) for loc in single] == [
        (1, 3, 3),
        (4, 4, 4),
        (5, 5, 5),
        (6, 6, 6),
    ]
    assert [cell.table_col for cell in single[1].table_cells] == ["B", "C", "D", "E", "F"]
    assert [(cell.table_col, cell.header_path) for cell in single[2].table_cells][:3] == [
        ("A", ["Group"]),
        ("B", ["Item"]),
        ("C", ["H1", "Plan"]),
    ]


HTML_TABLE = """<html><body><h1>Synthetic report</h1>
<p>Intro paragraph.</p>
<table><caption>Synthetic table</caption>
<tr><th rowspan="2">Item</th><th colspan="2">H1</th><th>Note</th></tr>
<tr><th>Plan</th><th>Actual</th><th></th></tr>
<tr><td>Alpha</td><td>10</td><td></td><td>first</td></tr>
<tr><td>Beta</td><td colspan="2">20</td><td>merged</td></tr>
<tr><td>Gamma</td><td>30</td><td>31</td><td>third</td></tr>
</table>
<table><tr><th>Key</th><th>Value</th></tr><tr><td>K1</td><td>V1</td></tr></table>
</body></html>"""


def test_html_table_keeps_empty_cells_in_their_columns() -> None:
    """空のセルを捨てず、後ろのセルを原本の列に置く（#3428）。"""
    extraction = _html_result(HTML_TABLE.encode()).extraction
    assert extraction is not None
    table = extraction.tables[0]
    assert {(cell.row, cell.col): cell.text for cell in table.cells}[(2, 3)] == "first"
    element = next(item for item in extraction.elements if item.kind == "table")
    assert "| Alpha | 10 |  | first |" in element.text


def test_html_tables_have_parsed_grid_rows_and_single_row_cells() -> None:
    """HTML の表: 表の番号は文書の順、行は表頭の 2 段を含む格子の行、1 行の部分はセルを持つ。"""
    extraction = _html_result(HTML_TABLE.encode()).extraction
    assert extraction is not None
    whole = _table_locators(chunk_extraction(extraction, chunk_size=2000, overlap=0))
    assert [(loc.row_basis, loc.table_index, loc.row_start, loc.row_end) for loc in whole] == [
        ("parsed_table", 1, 1, 5),
        ("parsed_table", 2, 1, 2),
    ]
    # 2 つ目の表は表頭 1 段・データの行 1 つなので、その行のセルを持つ。
    assert whole[1].table_row == 2
    assert [(cell.table_col, cell.header_path, cell.bbox) for cell in whole[1].table_cells] == [
        ("A", ["Key"], None),
        ("B", ["Value"], None),
    ]
    parts = _table_locators(chunk_extraction(extraction, chunk_size=80, overlap=0))
    first_table = [loc for loc in parts if loc.table_index == 1]
    # 最初の部分は表頭の 2 段、続きの部分は先頭に繰り返した表頭を数えず、1 つのデータの行。
    assert [(loc.row_start, loc.row_end, loc.table_row) for loc in first_table] == [
        (1, 2, None),
        (3, 3, 3),
        (4, 4, 4),
        (5, 5, 5),
    ]
    alpha, beta = first_table[1], first_table[2]
    # 空のセル（列 C）は値のあるセルに入れない。
    assert [cell.table_col for cell in alpha.table_cells] == ["A", "B", "D"]
    # 結合セル（colspan=2）は左の列に置き、表頭の段はその列の段。
    assert [(cell.table_col, cell.header_path) for cell in beta.table_cells] == [
        ("A", ["Item"]),
        ("B", ["H1", "Plan"]),
        ("D", ["Note"]),
    ]


def _image_table_extraction(*, table: ExtractionTable | None) -> StructuredExtraction:
    element = DocumentElement(
        kind="table",
        text="| Item | Value |\n| --- | --- |\n| Alpha | 10 |",
        element_id="image-table-1",
        content_kind="table",
        source_parser="vision_text",
        page_number=1,
    )
    return StructuredExtraction(
        raw_text=element.text, elements=[element], tables=[table] if table else []
    )


@pytest.mark.parametrize(
    "table",
    [
        None,
        # 本文から推したセル（解析の格子の印が無い）。
        ExtractionTable(
            table_id="image-table-1",
            element_id="image-table-1",
            cells=[
                ExtractionTableCell(row=0, col=0, text="Item"),
                ExtractionTableCell(row=0, col=1, text="Value"),
                ExtractionTableCell(row=1, col=0, text="Alpha"),
                ExtractionTableCell(row=1, col=1, text="10"),
            ],
            metadata={"table_source": "adapter_text"},
        ),
        # 格子の印はあるが、本文の行と格子の行が合わない。
        ExtractionTable(
            table_id="image-table-1",
            element_id="image-table-1",
            cells=[
                ExtractionTableCell(row=0, col=0, text="Item"),
                ExtractionTableCell(row=1, col=0, text="Other"),
            ],
            metadata={PARSED_TABLE_GRID_KEY: 1},
        ),
    ],
    ids=["no-grid", "text-derived-cells", "grid-does-not-match-text"],
)
def test_image_table_without_a_parser_grid_has_no_rows(table: ExtractionTable | None) -> None:
    """解析が格子を出さなかった表は、行の位置を作らない（null のまま）。"""
    locators = _table_locators(
        chunk_extraction(_image_table_extraction(table=table), chunk_size=2000, overlap=0)
    )
    assert len(locators) == 1
    assert locators[0].row_basis is None
    assert locators[0].table_index is None
    assert locators[0].row_start is None
    assert locators[0].table_cells == []


_DOCLING_ROWS = re.compile(r"<tr\b.*?</tr>", re.DOTALL)


def _two_page_table_extraction(*, continuation: str, rows: int = 0) -> StructuredExtraction:
    """記録の Docling の表（表頭 2 段・データの行 4 つ）を、頁 1（データの行 2 つ）と頁 2 に分ける。

    ``continuation``: carried=続きの表は表頭なし（取込が前の表の表頭を付ける）/ repeated=続きの表も
    同じ表頭を繰り返す / other_columns=続きの表の列の数が違う。``rows`` を渡すと、続きの表を
    その数の合成のデータの行にする（行のまとまりに分ける大きさ）。取込の文書全体の整理と同じ
    ``pdf_segment_merge._table_continuation`` で続きの印を付ける（形が合わなければ解析の判定と
    する）。
    """
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    layout = payload["parser_artifacts"]["layout_records"]
    layout["pages"].append({**layout["pages"][0], "page": 2})
    table = next(record for record in layout["records"] if record["category"] == "Table")
    html_rows = _DOCLING_ROWS.findall(table["text"])
    header, first, second = html_rows[:2], html_rows[2:4], html_rows[4:]
    if continuation == "other_columns":
        second = ["<tr><td>Gamma</td><td>310</td></tr>", "<tr><td>Delta</td><td>410</td></tr>"]
    if rows:
        second = [
            f"<tr><th>G3</th><th>Item {index}</th>"
            + "".join(f"<td>{index}{col}</td>" for col in range(4))
            + "</tr>"
            for index in range(rows)
        ]
    table["text"] = "<table><tbody>" + "".join([*header, *first]) + "</tbody></table>"
    table["raw"]["table_cell_bboxes"] = [
        entry for entry in table["raw"]["table_cell_bboxes"] if entry["row"] < 4
    ]
    continued_rows = [*header, *second] if continuation == "repeated" else second
    piece = {
        **table,
        "id": "docling-p2-1",
        "page": 2,
        "seq_no": 1,
        "text": "<table><tbody>" + "".join(continued_rows) + "</tbody></table>",
        "raw": {},
    }
    pdf_segment_merge._table_continuation(  # noqa: SLF001 - 取込と同じ続きの印を付ける
        table, piece, known=continuation == "other_columns"
    )
    layout["records"].append(piece)
    return StructuredExtraction.model_validate(payload)


def _two_page_locators(continuation: str, rows: int = 0, **params: Any) -> list[tuple[Any, ...]]:
    chunks = build_parent_child_chunks(
        _two_page_table_extraction(continuation=continuation, rows=rows),
        params=SmallToBigParams(**params) if params else None,
    )
    return [
        (loc.row_basis, loc.table_index, loc.row_start, loc.row_end, loc.table_row)
        for loc in _table_locators(chunks)
    ]


@pytest.mark.parametrize("continuation", ["carried", "repeated"])
def test_table_continued_on_the_next_page_counts_rows_after_the_first_page(
    continuation: str,
) -> None:
    """頁をまたぐ表は 1 つの表として、続きの表の行を前の頁の行の後ろに続けて数える（#3547）。

    続きの表の先頭の表頭（取込が付けた・繰り返した）は行として数えない。
    """
    assert _two_page_locators(continuation) == [
        ("parsed_table", 1, 1, 4, None),
        ("parsed_table", 1, 5, 6, None),
    ]
    # 続きの表の 1 つのデータの行は、表全体の格子の行と元の表の表頭の段のセルを持つ。
    layout = cast(
        "dict[str, Any]",
        _two_page_table_extraction(continuation=continuation).parser_artifacts["layout_records"],
    )
    tables = _layout_tables(layout["records"])
    piece = tables["docling-p2-1"]
    assert (piece.table_index, piece.grid.row_count, piece.grid.header_rows) == (1, 6, 2)
    gamma = _locator(grid_rows_metadata(piece.grid, piece.table_index, piece.grid_rows([3])))
    assert (gamma.row_start, gamma.table_row) == (5, 5)
    assert [(cell.table_col, cell.header_path) for cell in gamma.table_cells][:3] == [
        ("A", ["Group"]),
        ("B", ["Item"]),
        ("C", ["H1", "Plan"]),
    ]


def test_row_groups_of_a_continued_table_use_the_whole_table_rows() -> None:
    """行のまとまりに分けた続きの表も、表全体の格子の行の番号を持つ（#3547）。"""
    locators = _two_page_locators("carried", rows=40, table_child_target_chars=300)
    # 前の頁の表（データの行 2 つ）は 1 つの子、続きの表は行のまとまりの子に分かれる。
    assert locators[0] == ("parsed_table", 1, 1, 4, None)
    groups = locators[1:]
    assert len(groups) > 1
    assert all(basis == "parsed_table" and index == 1 for basis, index, *_ in groups)
    # まとまりは続きの表のデータの行（格子の 5〜44 行）を順に隙間なく覆う。
    assert groups[0][2] == 5 and groups[-1][3] == 44
    assert all(left[3] + 1 == right[2] for left, right in zip(groups, groups[1:], strict=False))


def test_continued_table_of_another_shape_has_no_rows() -> None:
    """形の合わない続きの表は行を作らない（前の頁の表の行は変わらない）。"""
    assert _two_page_locators("other_columns") == [
        ("parsed_table", 1, 1, 4, None),
        (None, None, None, None, None),
    ]
