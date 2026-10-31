"""解析が組み立てた表の格子の行（row_basis=parsed_table。#3429）。合成の表だけで確かめる。"""

from __future__ import annotations

import json

from rag_parser_core.extraction import (
    DocumentElement,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from rag_parser_core.parsed_table import (
    PARSED_TABLE_GRID_KEY,
    PARSED_TABLE_GRID_VERSION,
    bind_text_rows,
    grid_from_cells,
    html_table_layout,
    parsed_table_bindings,
    text_row_keys,
)

GRID = {PARSED_TABLE_GRID_KEY: PARSED_TABLE_GRID_VERSION}


def _cell(row: int, col: int, text: str, **extra: object) -> ExtractionTableCell:
    header = extra.pop("header", False)
    return ExtractionTableCell(
        row=row,
        col=col,
        text=text,
        metadata={"is_header": True} if header else {},
        **extra,  # type: ignore[arg-type]
    )


TWO_LEVEL = [
    _cell(0, 0, "Item", header=True, row_span=2),
    _cell(0, 1, "H1", header=True, col_span=2),
    _cell(1, 1, "Plan", header=True),
    _cell(1, 2, "Actual", header=True),
    _cell(2, 0, "Alpha"),
    _cell(2, 1, "10", bbox=[1, 2, 3, 4]),
    _cell(2, 2, ""),
    _cell(3, 0, "Beta"),
    _cell(3, 1, "20"),
    _cell(3, 2, "21"),
]
TWO_LEVEL_MARKDOWN = (
    "| Item | H1 |  |\n|  | Plan | Actual |\n| --- | --- | --- |\n| Alpha | 10 |  |\n"
    "| Beta | 20 | 21 |"
)


def test_grid_counts_header_rows_and_header_paths() -> None:
    grid = grid_from_cells(TWO_LEVEL)
    assert grid is not None
    assert (grid.row_count, grid.header_rows) == (4, 2)
    assert [grid.header_path(col) for col in (1, 2, 3)] == [
        ["Item"],
        ["H1", "Plan"],
        ["H1", "Actual"],
    ]
    # 値のあるセルだけ。解析が出したセルの領域を持つ。
    assert json.loads(grid.cells_metadata(3) or "") == [
        ["A", ["Item"]],
        ["B", ["H1", "Plan"], [1.0, 2.0, 3.0, 4.0]],
    ]


def test_grid_rejects_overlapping_cells() -> None:
    """結合セルの覆う位置に別のセルがある（格子のすべての位置を並べた）セルは格子にしない。"""
    assert grid_from_cells([_cell(0, 0, "A", col_span=2), _cell(0, 1, "A")]) is None


def test_text_rows_skip_markdown_separators_and_read_one_html_table() -> None:
    assert text_row_keys(TWO_LEVEL_MARKDOWN) == ["ItemH1", "PlanActual", "Alpha10", "Beta2021"]
    assert text_row_keys("<table><tr><th>A</th></tr><tr><td>1</td></tr></table>") == ["A", "1"]
    # 表が 2 つの本文・表の無い本文は行を読まない。
    two_tables = "<table><tr><td>1</td></tr></table><table><tr><td>2</td></tr></table>"
    assert text_row_keys(two_tables) == []
    assert text_row_keys("Item: Alpha, Value: 10") == []


def test_text_rows_bind_to_grid_rows_or_to_the_non_empty_rows() -> None:
    grid = grid_from_cells([_cell(0, 0, "A"), _cell(2, 0, "B")])
    assert grid is not None
    # 本文の無い格子の行（2 行目）を除いた本文の行にも対応させる。
    assert bind_text_rows(grid, "| A |\n| B |")[0] == (1, 3)
    assert bind_text_rows(grid, "| A |\n| C |") == ((), ())


def _extraction(*elements: DocumentElement, tables: list[ExtractionTable]) -> StructuredExtraction:
    return StructuredExtraction(raw_text="", elements=list(elements), tables=tables)


def _table_element(element_id: str, text: str, **metadata: object) -> DocumentElement:
    return DocumentElement(
        kind="table",
        text=text,
        element_id=element_id,
        content_kind="table",
        metadata=metadata,  # type: ignore[arg-type]
    )


def test_bindings_number_tables_in_order_and_skip_tables_without_a_grid() -> None:
    extraction = _extraction(
        _table_element("t1", "| X | Y |\n| 1 | 2 |"),
        _table_element("t2", TWO_LEVEL_MARKDOWN),
        tables=[
            ExtractionTable(
                table_id="t1",
                element_id="t1",
                cells=[_cell(0, 0, "X"), _cell(0, 1, "Y"), _cell(1, 0, "1"), _cell(1, 1, "2")],
            ),
            ExtractionTable(table_id="t2", element_id="t2", cells=TWO_LEVEL, metadata=GRID),
        ],
    )
    bindings = parsed_table_bindings(extraction)
    # 格子の印の無い表（t1）も表の番号を数える。
    assert list(bindings) == ["t2"]
    assert bindings["t2"].table_index == 2
    metadata = bindings["t2"].chunk_metadata(TWO_LEVEL_MARKDOWN)
    assert {key: metadata[key] for key in ("row_basis", "table_row_start", "table_row_end")} == {
        "row_basis": "parsed_table",
        "table_row_start": 1,
        "table_row_end": 4,
    }
    # 表頭を繰り返した部分は表頭を数えない。本文が合わない部分は何も返さない。
    part = "| Item | H1 |  |\n| --- | --- | --- |\n| Beta | 20 | 21 |"
    assert bindings["t2"].chunk_metadata(part, first_text_row=3, header_rows=1) == {
        "row_basis": "parsed_table",
        "table_index": 2,
        "table_row_start": 4,
        "table_row_end": 4,
        "table_row": 4,
        "table_cells_json": '[["A",["Item"]],["B",["H1","Plan"]],["C",["H1","Actual"]]]',
    }
    assert bindings["t2"].chunk_metadata(part, first_text_row=2, header_rows=1) == {}


def test_bindings_skip_tables_split_across_elements_and_record_rows() -> None:
    cells = [_cell(0, 0, "A"), _cell(1, 0, "B")]
    extraction = _extraction(
        _table_element("p1", "| A |\n| B |", table_id="same"),
        _table_element("p2", "| A |\n| B |", table_id="same"),
        _table_element("r1", "| A |\n| B |", row_basis="csv"),
        tables=[
            ExtractionTable(table_id=name, element_id=name, cells=cells, metadata=GRID)
            for name in ("p1", "p2", "r1")
        ],
    )
    assert parsed_table_bindings(extraction) == {}


HTML_WITH_HEAD = (
    '前の本文\n<table class="t"><caption>表題</caption><thead><tr><th>Item</th>'
    "<td>Note</td></tr></thead><tbody><tr><th>Alpha</th><td>10</td></tr>"
    "<tr><td>Beta</td><td><table><tr><td>内側</td></tr></table></td></tr></tbody>"
    "</table>\n後ろの本文"
)


def test_html_table_layout_splits_rows_and_keeps_header_caption_and_text() -> None:
    """HTML の表の部品（#3547）: thead の行は表頭、表の前の本文・表題・表の後ろの本文を分ける。"""
    layout = html_table_layout(HTML_WITH_HEAD)
    assert layout is not None
    assert (layout.prefix, layout.table_open, layout.caption) == (
        "前の本文\n",
        '<table class="t">',
        "<caption>表題</caption>",
    )
    assert layout.header_rows == 1
    # 入れ子の表は外側のセルの中のまま（外側の行だけを数える）。
    assert len(layout.rows) == 3 and "内側" in layout.rows[2]
    assert layout.suffix == "\n後ろの本文"
    part = layout.part(layout.rows[2:], last=True)
    assert part.startswith('前の本文\n<table class="t"><caption>表題</caption><tr><th>Item</th>')
    assert part.endswith("</table>\n後ろの本文")
    # 組み直した部分は 1 つの表として行を読める（表頭の行と選んだ行）。
    assert text_row_keys(part) == [
        text_row_keys(HTML_WITH_HEAD)[0],
        text_row_keys(HTML_WITH_HEAD)[2],
    ]


def test_html_table_layout_counts_leading_th_rows_as_header() -> None:
    """thead が無ければ、セルがすべて th の行が先頭から続く数を表頭にする（すべてなら 0）。"""
    two_level = (
        "<table><tr><th>A</th><th>B</th></tr><tr><th>a</th><th>b</th></tr>"
        "<tr><th>r1</th><td>1</td></tr></table>"
    )
    layout = html_table_layout(two_level)
    assert layout is not None and layout.header_rows == 2
    only_header = "<table><tr><th>A</th></tr><tr><th>B</th></tr></table>"
    layout = html_table_layout(only_header)
    assert layout is not None and layout.header_rows == 0


def test_html_table_layout_refuses_text_without_clear_row_boundaries() -> None:
    """表が 1 つでない・閉じていない tr・表でない本文は分けない（None）。"""
    for text in (
        "<table><tr><td>a</td></tr></table><table><tr><td>b</td></tr></table>",
        "<table><tr><td>a</td><tr><td>b</td></tr></table>",
        "<table></table>",
        "| a | b |",
    ):
        assert html_table_layout(text) is None, text
