"""Excel のシートのセルの格子（読み取りの結果。表・手順書・帳票の読み方が共通に使う）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 列幅を指定していないシートの既定の列幅（Excel の標準。文字数の単位）。
DEFAULT_COLUMN_WIDTH = 8.43
# 行の高さを指定していないシートの既定の高さ（ポイント）。
DEFAULT_ROW_HEIGHT = 15.0
# 図形の大きさ（EMU）の換算: 1 ピクセル = 9525 EMU、1 ポイント = 12700 EMU。標準の列幅は
# 64 ピクセル、列幅（文字数）のピクセルは標準のフォントの数字の幅（7 ピクセル）で換算する
# （Office Open XML の式）。
_EMU_PER_PIXEL = 9525
_EMU_PER_POINT = 12700
_DEFAULT_COLUMN_PIXELS = 64
_DIGIT_PIXELS = 7
# 図形の範囲を探すときの上限（壊れた大きさで時間をかけない）。
_MAX_ANCHOR_STEPS = 10000
_MAX_ROW = 1048576
_MAX_COLUMN = 16384
_NUMERIC_TEXT = re.compile(r"^[+-]?[\d,]+(\.\d+)?%?$|^\d{4}-\d{2}-\d{2}([ T][\d:.]+)?$")


@dataclass
class Grid:
    """1 シートのセル（1 始まりの行・列 → 表示の文字列）。

    ``column_widths`` は列幅を指定した列（文字数の単位）、``label_hints`` は塗りつぶしか太字の
    セル（帳票のラベルの手がかり。#2516）。``shape_boxes`` は図形・テキストボックスの文字を置いた
    単位（左上 → 範囲）、``hidden_cells`` は読まなかった非表示の行・列の値のあるセル、
    ``hidden_rows`` / ``hidden_columns`` は非表示の行・列、``filter_rows`` はオートフィルターで
    隠れていて読んだ行（#2526）。``notes`` は位置で切り分けない読み取りの診断（コードと詳細）。
    """

    name: str
    hidden: bool
    cells: dict[tuple[int, int], str] = field(default_factory=dict)
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    missing_formula_cells: set[tuple[int, int]] = field(default_factory=set)
    column_widths: dict[int, float] = field(default_factory=dict)
    default_column_width: float = DEFAULT_COLUMN_WIDTH
    label_hints: set[tuple[int, int]] = field(default_factory=set)
    row_heights: dict[int, float] = field(default_factory=dict)
    default_row_height: float = DEFAULT_ROW_HEIGHT
    shape_boxes: dict[tuple[int, int], tuple[int, int, int, int]] = field(default_factory=dict)
    hidden_cells: set[tuple[int, int]] = field(default_factory=set)
    hidden_rows: set[int] = field(default_factory=set)
    hidden_columns: set[int] = field(default_factory=set)
    filter_rows: set[int] = field(default_factory=set)
    notes: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    # データの行で、横の結合の範囲を左上の値で埋めたセル（判定には使い、記録には入れない。#2531）。
    merge_filled: set[tuple[int, int]] = field(default_factory=set)
    # 行 → 値のある列（`row_columns` の索引。セルの数が変わるか `invalidate_rows` で
    # 作り直す。#2527）。
    _columns_by_row: dict[int, list[int]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _indexed_cells: int = field(default=-1, init=False, repr=False, compare=False)

    @property
    def max_row(self) -> int:
        return max((row for row, _ in self.cells), default=0)

    def row_columns(self, row: int) -> list[int]:
        """行の値のある列（昇順）。行ごとに全セルを走査しないよう、索引を 1 回だけ作る。"""
        if self._columns_by_row is None or self._indexed_cells != len(self.cells):
            index: dict[int, list[int]] = {}
            for (r, col), value in self.cells.items():
                if value.strip():
                    index.setdefault(r, []).append(col)
            for cols in index.values():
                cols.sort()
            self._columns_by_row = index
            self._indexed_cells = len(self.cells)
        return list(self._columns_by_row.get(row, ()))

    def invalidate_rows(self) -> None:
        """セルの値を書き換えたあとに呼ぶ（`row_columns` の索引を作り直す）。"""
        self._columns_by_row = None

    def column_width(self, col: int) -> float:
        return self.column_widths.get(col, self.default_column_width)

    def column_emu(self, col: int) -> int:
        """列の幅（EMU。非表示の列は 0）。"""
        if col in self.hidden_columns:
            return 0
        width = self.column_widths.get(col)
        if width is None and self.default_column_width == DEFAULT_COLUMN_WIDTH:
            return _DEFAULT_COLUMN_PIXELS * _EMU_PER_PIXEL
        chars = width if width is not None else self.default_column_width
        pixels = int((256 * chars + int(128 / _DIGIT_PIXELS)) / 256 * _DIGIT_PIXELS)
        return pixels * _EMU_PER_PIXEL

    def row_emu(self, row: int) -> int:
        """行の高さ（EMU。非表示の行は 0）。"""
        if row in self.hidden_rows:
            return 0
        return int(self.row_heights.get(row, self.default_row_height) * _EMU_PER_POINT)

    def anchor_box(
        self, top: int, left: int, row_offset: int, column_offset: int, height: int, width: int
    ) -> tuple[int, int, int, int]:
        """セル（``top``, ``left``）の中の位置と大きさ（EMU）の図形がかかるセルの範囲。"""
        first_row, last_row = _span(top, row_offset, height, self.row_emu, _MAX_ROW)
        first_col, last_col = _span(left, column_offset, width, self.column_emu, _MAX_COLUMN)
        return first_row, first_col, last_row, last_col

    def shape_refs(self, top: int, left: int, bottom: int, right: int) -> list[str]:
        """範囲の中に左上がある、図形の文字を置いた単位のセル範囲（左上の順）。"""
        return [
            cell_ref(*box)
            for (row, col), box in sorted(self.shape_boxes.items())
            if top <= row <= bottom and left <= col <= right
        ]

    def place_shape_text(self, box: tuple[int, int, int, int], text: str) -> str | None:
        """図形の文字を、アンカーの範囲を 1 つの単位（結合セル）にして置く（置いた範囲を返す）。

        図形は、セルの上に浮く。範囲の中に値のあるセルが無ければ範囲（重なる値の無い結合セルと
        合わせた範囲）に置き、値のあるセルと重なるときは範囲の中の最初の空いたセルに置く。置けない
        （範囲のすべてのセルに値がある）ときは None。
        """
        top, left, bottom, right = box
        blanks = [merge for merge in self.merges if _overlaps(merge, box)]
        if not any(self.cells.get((merge[0], merge[1]), "").strip() for merge in blanks):
            for merge in blanks:
                top, left = min(top, merge[0]), min(left, merge[1])
                bottom, right = max(bottom, merge[2]), max(right, merge[3])
            area = [(row, col) for row in range(top, bottom + 1) for col in range(left, right + 1)]
            if not any(self.cells.get(cell, "").strip() for cell in area):
                self.merges = [merge for merge in self.merges if merge not in blanks]
                if (top, left) != (bottom, right):
                    self.merges.append((top, left, bottom, right))
                return self._put_shape((top, left, bottom, right), text)
        top, left, bottom, right = box
        for row in range(top, bottom + 1):
            for col in range(left, right + 1):
                if self.cells.get((row, col), "").strip() or any(
                    _overlaps(merge, (row, col, row, col)) for merge in self.merges
                ):
                    continue
                return self._put_shape((row, col, row, col), text)
        return None

    def _put_shape(self, box: tuple[int, int, int, int], text: str) -> str:
        self.cells[(box[0], box[1])] = text
        self.invalidate_rows()
        self.shape_boxes[(box[0], box[1])] = box
        return cell_ref(*box)


def _span(start: int, offset: int, size: int, measure: Any, limit: int) -> tuple[int, int]:
    """``start`` の行・列から ``offset`` 進んだ位置から ``size`` の長さがかかる、最初と最後の番号。

    長さが 0 の行・列（非表示）は飛ばす。図形の端がちょうど境目なら、次の行・列は入れない。
    """
    index, position, steps = start, max(offset, 0), 0
    while index < limit and steps < _MAX_ANCHOR_STEPS and position >= measure(index):
        position -= measure(index)
        index += 1
        steps += 1
    first = index
    end = position + max(size, 0)
    while index < limit and steps < _MAX_ANCHOR_STEPS and end > measure(index):
        end -= measure(index)
        index += 1
        steps += 1
    return first, index


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def column_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def cell_ref(top: int, left: int, bottom: int, right: int) -> str:
    """セルの範囲の表記（1 つのセルは ``B3``、範囲は ``B3:E3``）。"""
    start = f"{column_letter(left)}{top}"
    if top == bottom and left == right:
        return start
    return f"{start}:{column_letter(right)}{bottom}"


def is_textual(value: str) -> bool:
    """数値・日付でない文字か。"""
    return not _NUMERIC_TEXT.match(value.strip())
