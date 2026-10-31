"""解析が組み立てた表の格子の行・列（根拠の場所の ``row_basis=parsed_table``。#3429）。

PDF・HTML・画像の表は、原本に行・列の番号が無い。解析（Docling の表のセル・HTML の表・解析の
adapter が渡したセル）が組み立てた格子の行・列を、原本の座標ではない「解析の表」の基準として根拠の
場所に出す。Azure Document Intelligence の ``rowIndex`` / ``columnIndex`` と
``boundingRegions``、Docling の ``TableCell`` の ``start_row_offset_idx`` と ``bbox`` と
同じく、解析の結果の格子として扱う（同じ文書でも解析の版・設定が変われば番号が変わる）。

番号の決まり:

- 行・列は格子の 1 始まりの番号。表頭の行（解析が表頭と印を付けたセルだけでできた先頭の行）も
  数える（表頭が 2 段の表の最初のデータの行は 3）。結合したセルは左上の行・列に置く。
- 根拠の行の範囲は、その chunk が行として含む格子の行。表を分けた部分の先頭に繰り返した表頭は
  数えない。1 つのデータの行だけを含む chunk は、その行（``table_row``）と値のあるセル
  （``table_cells_json``。列の記号・表頭の段・解析が出したときはセルの領域）も持つ。
- 表の番号（``table_index``）は文書の中の表の順（1 始まり。格子の無い表も数える）。
- 格子を出したと印を付けた表（``ExtractionTable.metadata`` の ``PARSED_TABLE_GRID_KEY``）だけを
  読む。格子が無い表・本文から推した表・格子と本文の行が合わない表・ページをまたいで別の要素に
  分かれた表は記録しない（座標を作らない）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

from rag_parser_core.extraction import (
    DocumentElement,
    ExtractionTableCell,
    StructuredExtraction,
    is_derived_content,
)
from rag_parser_core.sheet_records import (
    ROW_BASIS_KEY,
    TABLE_CELLS_KEY,
    TABLE_INDEX_KEY,
    TABLE_ROW_END_KEY,
    TABLE_ROW_KEY,
    TABLE_ROW_START_KEY,
    column_letters,
)

ROW_BASIS_PARSED_TABLE: Final = "parsed_table"
# 表（``ExtractionTable.metadata``）のセルが解析の格子そのもの（結合セルは左上の 1 つ・span と表頭の
# 印つき）であることの印と版。印の無い表（本文から推したセル・この版より前の取込）は読まない。
PARSED_TABLE_GRID_KEY = "parsed_table_grid"
PARSED_TABLE_GRID_VERSION = 1
# セル（``ExtractionTableCell.metadata``）の表頭の印（HTML の th・解析の adapter の is_header）と、
# 列の見出しの印（Docling の column_header）。どちらかが True のセルを表頭のセルとして読む。
HEADER_CELL_KEY = "is_header"
COLUMN_HEADER_CELL_KEY = "column_header"

_TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$")
_SEPARATOR_CELL = re.compile(r":?-{3,}:?")
_COMPACT_DROP = re.compile(r"[\s|\\]+")


@dataclass(frozen=True)
class ParsedCell:
    """格子の 1 つのセル（結合セルは左上。行・列は 1 始まり）。"""

    row: int
    col: int
    text: str
    row_span: int = 1
    col_span: int = 1
    header: bool = False
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ParsedTableGrid:
    """解析が組み立てた表の格子。"""

    row_count: int
    cells: tuple[ParsedCell, ...]
    header_rows: int = 0
    # 行ごとのセル（``cells`` から作る索引。比較・hash には使わない）。
    _by_row: Mapping[int, tuple[ParsedCell, ...]] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    @classmethod
    def build(cls, cells: Iterable[ParsedCell]) -> ParsedTableGrid | None:
        ordered = tuple(sorted(cells, key=lambda cell: (cell.row, cell.col)))
        if not ordered:
            return None
        row_count = max(cell.row + cell.row_span - 1 for cell in ordered)
        by_row: dict[int, list[ParsedCell]] = {}
        for cell in ordered:
            by_row.setdefault(cell.row, []).append(cell)
        rows = {row: tuple(items) for row, items in by_row.items()}
        return cls(
            row_count=row_count,
            cells=ordered,
            header_rows=_header_row_count(rows, row_count),
            _by_row=rows,
        )

    def row_cells(self, row: int) -> tuple[ParsedCell, ...]:
        return self._by_row.get(row, ())

    def row_key(self, row: int) -> str:
        """行の本文の照合の鍵（セルの本文を列の順につなぎ、空白・罫線の記号を除いたもの）。"""
        return _compact("".join(cell.text for cell in self.row_cells(row)))

    def header_path(self, col: int) -> list[str]:
        """列の表頭の段（上の段から。同じ文字が続く段は 1 つ）。"""
        path: list[str] = []
        for row in range(1, self.header_rows + 1):
            for cell in self.row_cells(row):
                if not cell.header or not (cell.col <= col < cell.col + cell.col_span):
                    continue
                text = " ".join(cell.text.split())
                if text and (not path or path[-1] != text):
                    path.append(text)
        return path

    def cells_metadata(self, row: int) -> str | None:
        """データの行の値のあるセル（``table_cells_json`` の値。列の記号・表頭の段・領域）。"""
        items: list[list[object]] = []
        for cell in self.row_cells(row):
            if not cell.text.strip():
                continue
            item: list[object] = [column_letters(cell.col), self.header_path(cell.col)]
            if cell.bbox is not None:
                item.append([round(value, 2) for value in cell.bbox])
            items.append(item)
        if not items:
            return None
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class ParsedTableBinding:
    """表の要素の本文の行と格子の行の対応（``text_rows[i]`` が本文の i 番目の行の格子の行）。"""

    table_index: int
    grid: ParsedTableGrid
    text_rows: tuple[int, ...]
    text_keys: tuple[str, ...]

    def chunk_metadata(
        self, text: str, *, first_text_row: int = 0, header_rows: int = 0
    ) -> dict[str, str | int]:
        """chunk の本文が含む格子の行の場所（本文の行が合わなければ空）。

        ``first_text_row`` は chunk の最初の行（表頭を繰り返した部分は、表頭の次の行）が要素の本文の
        何番目の行か。``header_rows`` は部分の先頭に繰り返した表頭の行の数で、要素の本文の先頭の
        行（表頭）と同じであることを確かめてから数えない（#3547。HTML の表は表頭が複数の行）。
        """
        keys = text_row_keys(text)
        if not keys:
            return {}
        if header_rows:
            if len(keys) <= header_rows or tuple(keys[:header_rows]) != tuple(
                self.text_keys[:header_rows]
            ):
                return {}
            keys = keys[header_rows:]
        end = first_text_row + len(keys)
        if not keys or first_text_row < 0 or end > len(self.text_keys):
            return {}
        if tuple(keys) != self.text_keys[first_text_row:end]:
            return {}
        return self.rows_metadata(self.text_rows[first_text_row:end])

    def rows_metadata(self, rows: Sequence[int]) -> dict[str, str | int]:
        return grid_rows_metadata(self.grid, self.table_index, rows)


def grid_rows_metadata(
    grid: ParsedTableGrid, table_index: int, rows: Sequence[int]
) -> dict[str, str | int]:
    """格子の行の範囲の場所（chunk の metadata）。1 つのデータの行だけならセルも付ける。"""
    selected = sorted({row for row in rows if 1 <= row <= grid.row_count})
    if not selected or table_index < 1:
        return {}
    metadata: dict[str, str | int] = {
        ROW_BASIS_KEY: ROW_BASIS_PARSED_TABLE,
        TABLE_INDEX_KEY: table_index,
        TABLE_ROW_START_KEY: selected[0],
        TABLE_ROW_END_KEY: selected[-1],
    }
    data_rows = [row for row in selected if row > grid.header_rows]
    if len(data_rows) == 1:
        metadata[TABLE_ROW_KEY] = data_rows[0]
        cells = grid.cells_metadata(data_rows[0])
        if cells is not None:
            metadata[TABLE_CELLS_KEY] = cells
    return metadata


def grid_from_cells(cells: Sequence[ExtractionTableCell]) -> ParsedTableGrid | None:
    """``ExtractionTable`` のセル（0 始まり）から格子を作る。重なるセルがあれば None。"""
    parsed: list[ParsedCell] = []
    covered: set[tuple[int, int]] = set()
    for cell in cells:
        positions = {
            (row, col)
            for row in range(cell.row, cell.row + cell.row_span)
            for col in range(cell.col, cell.col + cell.col_span)
        }
        if positions & covered:
            return None
        covered |= positions
        parsed.append(
            ParsedCell(
                row=cell.row + 1,
                col=cell.col + 1,
                text=cell.text,
                row_span=cell.row_span,
                col_span=cell.col_span,
                header=any(
                    cell.metadata.get(key) is True
                    for key in (HEADER_CELL_KEY, COLUMN_HEADER_CELL_KEY)
                ),
                bbox=_bbox(cell.bbox),
            )
        )
    return ParsedTableGrid.build(parsed)


def text_row_keys(text: str) -> list[str]:
    """表の本文（HTML の表か Markdown の表）の行ごとの照合の鍵。表が読めなければ空。

    HTML は外側の表の ``tr`` ごと（表が 2 つ以上なら空）、Markdown は表の行（区切りの行を除く）。
    """
    if "<table" in text.casefold():
        rows = _html_rows(text)
        return [_compact("".join(row)) for row in rows] if rows is not None else []
    keys: list[str] = []
    for line in text.splitlines():
        if not _TABLE_LINE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(_SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            continue
        keys.append(_compact(line))
    return keys


def bind_text_rows(grid: ParsedTableGrid, text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """要素の本文の行を格子の行に対応させる（合わなければ空）。

    本文が格子のすべての行を順に持つか、本文の無い行を除いた行を順に持つときだけ対応させる。
    """
    keys = tuple(text_row_keys(text))
    if not keys:
        return (), ()
    all_rows = tuple(range(1, grid.row_count + 1))
    for rows in (all_rows, tuple(row for row in all_rows if grid.row_key(row))):
        if len(rows) == len(keys) and all(
            grid.row_key(row) == key for row, key in zip(rows, keys, strict=True)
        ):
            return rows, keys
    return (), ()


def parsed_table_bindings(extraction: StructuredExtraction) -> dict[str, ParsedTableBinding]:
    """表の要素（``element_id``）ごとの格子の行の対応。格子を読めない表は含めない。"""
    tables = {
        table.element_id: table
        for table in extraction.tables
        if table.element_id
        and table.metadata.get(PARSED_TABLE_GRID_KEY) == PARSED_TABLE_GRID_VERSION
    }
    elements = [element for element in extraction.elements if _is_table_element(element)]
    keys = [_table_key(element) for element in elements]
    counts = Counter(keys)
    bindings: dict[str, ParsedTableBinding] = {}
    numbers: dict[str, int] = {}
    for element, key in zip(elements, keys, strict=True):
        index = numbers.setdefault(key, len(numbers) + 1)
        table = tables.get(element.element_id or "")
        if (
            table is None
            or not element.element_id
            or counts[key] > 1
            or element.metadata.get(ROW_BASIS_KEY)
        ):
            continue
        grid = grid_from_cells(table.cells)
        if grid is None:
            continue
        rows, row_keys = bind_text_rows(grid, element.text)
        if rows:
            bindings[element.element_id] = ParsedTableBinding(
                table_index=index, grid=grid, text_rows=rows, text_keys=row_keys
            )
    return bindings


def _is_table_element(element: DocumentElement) -> bool:
    if not element.text.strip() or is_derived_content(element.content_kind, element.metadata):
        return False
    return element.kind == "table" or (
        element.content_kind == "table" and element.kind != "table_caption"
    )


def _table_key(element: DocumentElement) -> str:
    table_id = element.metadata.get("table_id")
    if isinstance(table_id, str) and table_id.strip():
        return f"table:{table_id.strip()}"
    return f"element:{element.element_id or element.order}"


def _header_row_count(rows: Mapping[int, Sequence[ParsedCell]], row_count: int) -> int:
    """表頭の行の数（表頭の印のセルがあり、値のあるセルがすべて表頭の先頭の行）。"""
    count = 0
    for row in range(1, row_count + 1):
        cells = rows.get(row, ())
        if not any(cell.header for cell in cells):
            break
        if not all(cell.header or not cell.text.strip() for cell in cells):
            break
        count += 1
    # すべての行が表頭なら、データの行の無い表（表頭だけ）として数えない。
    return 0 if count == row_count else count


def _bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    if not all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
        return None
    x0, y0, x1, y1 = (float(item) for item in value)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _compact(text: str) -> str:
    return _COMPACT_DROP.sub("", text)


class _TableRows(HTMLParser):
    """外側の表の ``tr`` ごとのセルの本文（入れ子の表は外側のセルの本文にする）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.tables = 0
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.tables += 1
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._close_cell()
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self.depth == 1:
                self._close_row()
            self.depth = max(0, self.depth - 1)
            return
        if self.depth != 1:
            return
        if tag in {"td", "th"}:
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None


@dataclass(frozen=True)
class HtmlTableLayout:
    """1 つの HTML の表の本文の部品（行の境目で分けて組み直すため。#3547）。

    ``rows`` は外側の表の ``tr`` の HTML（原文のまま）。``header_rows`` は先頭の表頭の行の数
    （``thead`` の中か、セルがすべて ``th`` の行が先頭から続く数。すべての行が表頭なら 0）。
    """

    prefix: str
    table_open: str
    caption: str
    rows: tuple[str, ...]
    header_rows: int
    suffix: str

    def part(self, rows: Sequence[str], *, last: bool = False) -> str:
        """表頭の行と ``rows`` の行だけの表の HTML（最後の部分は表の後ろの本文も付ける）。"""
        header = "".join(self.rows[: self.header_rows])
        body = "".join(rows)
        suffix = self.suffix if last else ""
        return f"{self.prefix}{self.table_open}{self.caption}{header}{body}</table>{suffix}"


class _TableLayoutParser(HTMLParser):
    """外側の表の ``table`` / ``caption`` / ``tr`` の位置（文字の位置）と行の表頭の印。"""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = [0]
        for match in re.finditer("\n", text):
            self._line_starts.append(match.end())
        self.text = text
        self.depth = 0
        self.tables = 0
        self.broken = False
        self.table_span: tuple[int, int] | None = None
        self.table_open = ""
        self.caption: tuple[int, int] | None = None
        self._caption_start: int | None = None
        self.rows: list[tuple[int, int, bool]] = []
        self._row_start: int | None = None
        self._row_cells: list[str] = []
        self._in_head = False
        self._table_start = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def _end_of_tag(self, start: int) -> int:
        end = self.text.find(">", start)
        if end < 0:
            self.broken = True
            return len(self.text)
        return end + 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self.depth == 0:
                self.tables += 1
                self._table_start = self._offset()
                self.table_open = self.get_starttag_text() or "<table>"
            self.depth += 1
            return
        if self.depth != 1:
            return
        if tag == "caption":
            self._caption_start = self._offset()
        elif tag == "thead":
            self._in_head = True
        elif tag in {"tbody", "tfoot"}:
            self._in_head = False
        elif tag == "tr":
            if self._row_start is not None:
                # 閉じていない tr（省略した終わりのタグ）は行の境目が決まらない。
                self.broken = True
            self._row_start = self._offset()
            self._row_cells = []
        elif tag in {"td", "th"} and self._row_start is not None:
            self._row_cells.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self.depth == 1:
                if self._row_start is not None:
                    self.broken = True
                self.table_span = (self._table_start, self._end_of_tag(self._offset()))
            self.depth = max(0, self.depth - 1)
            return
        if self.depth != 1:
            return
        if tag == "caption" and self._caption_start is not None:
            self.caption = (self._caption_start, self._end_of_tag(self._offset()))
            self._caption_start = None
        elif tag == "thead":
            self._in_head = False
        elif tag == "tr" and self._row_start is not None:
            header = bool(self._row_cells) and (
                self._in_head or all(cell == "th" for cell in self._row_cells)
            )
            self.rows.append((self._row_start, self._end_of_tag(self._offset()), header))
            self._row_start = None


def html_table_layout(text: str) -> HtmlTableLayout | None:
    """1 つの HTML の表の本文を部品に分ける（表が 1 つでない・行の境目が決まらなければ None）。

    分けた部品で組み直す部分の表は ``thead`` / ``tbody`` を持たない（行の ``tr`` は原文のまま）。
    """
    if "<table" not in text.casefold():
        return None
    parser = _TableLayoutParser(text)
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - 読めない HTML は分けない
        return None
    if parser.broken or parser.tables != 1 or parser.table_span is None or not parser.rows:
        return None
    start, end = parser.table_span
    header_rows = 0
    for _row_start, _row_end, header in parser.rows:
        if not header:
            break
        header_rows += 1
    if header_rows == len(parser.rows):
        header_rows = 0
    caption = text[parser.caption[0] : parser.caption[1]] if parser.caption else ""
    return HtmlTableLayout(
        prefix=text[:start],
        table_open=parser.table_open,
        caption=caption,
        rows=tuple(text[row_start:row_end] for row_start, row_end, _header in parser.rows),
        header_rows=header_rows,
        suffix=text[end:],
    )


def _html_rows(html: str) -> list[list[str]] | None:
    parser = _TableRows()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - 読めない HTML は表として扱わない
        return None
    return parser.rows if parser.tables == 1 and parser.rows else None


__all__ = [
    "COLUMN_HEADER_CELL_KEY",
    "HEADER_CELL_KEY",
    "HtmlTableLayout",
    "PARSED_TABLE_GRID_KEY",
    "PARSED_TABLE_GRID_VERSION",
    "ROW_BASIS_PARSED_TABLE",
    "ParsedCell",
    "ParsedTableBinding",
    "ParsedTableGrid",
    "bind_text_rows",
    "grid_from_cells",
    "grid_rows_metadata",
    "html_table_layout",
    "parsed_table_bindings",
    "text_row_keys",
]
