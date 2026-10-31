"""シートの版式の判定と、帳票（方眼紙）・1 シートの複数の表の読み方（#2516）。

- 領域の分け方（`split_regions`）: 空の行・空の列で格子を再帰的に分け（XY-cut）、表ごとの領域に
  する。セル 1 つの 1 行の領域（表の題名）は下の表につなぎ、セルが 2 つ以上の 1 行の領域と、
  表頭らしい行で始まらない領域（空の行で区切った表の続き）は上の表につなぐ（#2528）。結合セルの
  範囲は分けない。表の下の注記の行（どの行もセルが 1 つで、注記の印で始まるか長い文）だけの領域は、
  新しい表にせず上の表につなぐ（#3566。注記にするのは表の読み方 `reader`）。
- 帳票の判定（`detect_form_layout`）: 結合セルを 1 つの単位にし、構造の特徴（結合セルの割合・
  ラベルのすぐ右の値・行どうしの単位の位置の不揃い・列幅の狭さ）を 0〜1 の点数にする。狭い列は
  加点で、普通の列幅の帳票も判定する。位置のそろった格子（普通の表）は点数を下げる（#2530）。
  業務の語は使わない。
- 帳票の読み方（`form_blocks`）: ラベルの単位と、その右（空の列 2 つまで）か下の値の単位を組にし、
  「ラベル: 値」の記録（ラベル・値・それぞれのセル・全体のセル範囲）にする。値の無い行の単位は
  見出し（以降の項目の節）、縦に結合して右に複数の項目を持つ単位は項目のまとまりの見出しにする。
  塗りつぶし・太字（またはコロンで終わる）のラベルが横に並び、その下に同じ位置の値の行があるときは、
  上のラベルと下の値を組にする（値の行が 2 行以上なら表の行にする）。1 つのセルの
  「ラベル：値」はそれだけで項目にし、下に値の無い結合セル（押印欄の枠など）のあるラベルは
  空欄として数える（#2529）。
- 値の選択の記号（「■A □B」）は選ばれた選択肢を値にし、原文を ``value_original`` に残す
  （`choices`。#2526）。図形・テキストボックスの文字の単位は ``shape_cells`` に残す。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence, Set
from dataclasses import dataclass

from .choices import is_choice_mark, read_choice, starts_with_choice_mark
from .grid import Grid, cell_ref, column_letter, is_textual
from .models import SheetBlock, SheetDiagnostic

# 列幅がこれ以下の列を「狭い列」（方眼紙の格子）とみなす（文字数の単位）。
NARROW_COLUMN_WIDTH = 4.0
# 帳票の点数の重み（結合セル・狭い列・位置の不揃い・ラベルと値の隣接）。
# ラベルと値の隣接（ラベルのすぐ右の値）を主にし、狭い列（方眼紙）は加点にする。普通の列幅の帳票
# （見積書・届出など）も判定できるようにする（#2530）。
_FORM_WEIGHTS = {"merged": 0.3, "narrow": 0.15, "misaligned": 0.15, "label_value": 0.4}
# 狭い列でなく、行どうしの単位の位置がそろった格子（結合したデータの列のある普通の表）は、
# 点数をこの倍率で下げる（帳票はラベルと値の並びが行ごとに違う）。
_REGULAR_GRID_NARROW = 0.5
_REGULAR_GRID_MISALIGNED = 0.2
_REGULAR_GRID_FACTOR = 0.5
# これより単位の少ない領域は帳票と判定しない（特徴が安定しない）。
FORM_MIN_UNITS = 4
# 判定に使う先頭の行の数（大きい表で時間をかけない）。
_FORM_SAMPLE_ROWS = 200
# ラベルと値の間に置ける空の列・空の行の数。
_FORM_MAX_GAP_COLUMNS = 2
_FORM_MAX_GAP_ROWS = 1
_LABEL_SUFFIXES = (":", "：")
# 1 つのセルの「ラベル：値」（全角か半角のコロン）。ラベルは改行を含まない 30 文字まで（#2529）。
_INLINE_FIELD = re.compile(r"^(?P<label>[^：:\n]{1,30}?)\s*[：:]\s*(?P<value>\S.*)$", re.DOTALL)
# 表の続きか新しい表かを見る、表頭の候補の行の後の行の数。
_CONTINUATION_LOOKAHEAD_ROWS = 5
# これ以上の空の行で離れた領域は、表の続きにしない（表の中の区切りの空の行は 1 行が多い）。
_NEW_TABLE_GAP_ROWS = 2

# 表の下の注記（#3566）: 注記の印の無い行を注記とみなす文の長さの下限（文字数）。
NOTE_MIN_CHARS = 30
# 番号だけの印（「(1)」「1)」）を作る文字。番号だけの印は表の題名の番号と区別できないので、
# 題名の位置（下の表に近い 1 行）では注記にしない。
_NUMBERING_CHARS = frozenset("()（）[]［］.．:：、")

Box = tuple[int, int, int, int]


def note_marker(text: str, patterns: Sequence[re.Pattern[str]]) -> str | None:
    """文の先頭の注記の印（``patterns`` は ``ExcelOptions.note_markers``。無ければ None）。"""
    stripped = text.strip()
    for pattern in patterns:
        match = pattern.match(stripped)
        if match and match.group(0).strip():
            return match.group(0).strip()
    return None


def is_numbering(marker: str) -> bool:
    """番号だけの印（「(1)」「1)」）か。"""
    return all(char.isdigit() or char in _NUMBERING_CHARS for char in marker if not char.isspace())


@dataclass(frozen=True)
class Unit:
    """結合セル（または 1 つのセル）の単位。``text`` が空なら値の無い結合セル。

    ``shape`` は図形・テキストボックスの文字の単位（#2526）。
    """

    top: int
    left: int
    bottom: int
    right: int
    text: str
    hinted: bool = False
    shape: bool = False

    @property
    def merged(self) -> bool:
        return self.bottom > self.top or self.right > self.left

    @property
    def ref(self) -> str:
        return cell_ref(self.top, self.left, self.bottom, self.right)

    @property
    def textual(self) -> bool:
        return bool(self.text) and is_textual(self.text)


# ---- 領域（1 シートの複数の表）--------------------------------------------------------


def _occupied(grid: Grid) -> set[tuple[int, int]]:
    """値のあるセルと、値のある結合セルの範囲。"""
    cells = {key for key, value in grid.cells.items() if value.strip()}
    for top, left, bottom, right in grid.merges:
        if grid.cells.get((top, left), "").strip():
            cells.update(
                (row, col) for row in range(top, bottom + 1) for col in range(left, right + 1)
            )
    return cells


def _gap(start: int, end: int, skipped: Set[int]) -> int:
    """``start`` と ``end`` の間の空きの数（読まなかった非表示の行・列は空きに数えない）。"""
    return sum(1 for index in range(start + 1, end) if index not in skipped)


def _runs(values: list[int], gap: int, skipped: Set[int] = frozenset()) -> list[tuple[int, int]]:
    """昇順の番号を、``gap`` 以上の空きで区切った連続の範囲にする。"""
    runs: list[tuple[int, int]] = []
    for value in values:
        if runs and _gap(runs[-1][1], value, skipped) < gap:
            runs[-1] = (runs[-1][0], value)
        else:
            runs.append((value, value))
    return runs


def _xy_cut(
    cells: set[tuple[int, int]],
    gap_rows: int,
    gap_columns: int,
    skipped: tuple[Set[int], Set[int]] = (frozenset(), frozenset()),
) -> list[Box]:
    rows = sorted({row for row, _ in cells})
    cols = sorted({col for _, col in cells})
    if not rows:
        return []
    row_runs = _runs(rows, gap_rows, skipped[0])
    if len(row_runs) > 1:
        return [
            box
            for start, end in row_runs
            for box in _xy_cut(
                {cell for cell in cells if start <= cell[0] <= end}, gap_rows, gap_columns, skipped
            )
        ]
    col_runs = _runs(cols, gap_columns, skipped[1])
    if len(col_runs) > 1:
        return [
            box
            for start, end in col_runs
            for box in _xy_cut(
                {cell for cell in cells if start <= cell[1] <= end}, gap_rows, gap_columns, skipped
            )
        ]
    return [(rows[0], cols[0], rows[-1], cols[-1])]


class _RowIndex:
    """値のあるセルの列を行ごとに持つ索引（領域の行・列をすぐに引く）。"""

    def __init__(self, grid: Grid) -> None:
        self.grid = grid
        self.columns: dict[int, list[int]] = defaultdict(list)
        for (row, col), value in grid.cells.items():
            if value.strip():
                self.columns[row].append(col)
        for cols in self.columns.values():
            cols.sort()

    def rows(self, box: Box) -> list[int]:
        top, left, bottom, right = box
        return [
            row
            for row in range(top, bottom + 1)
            if any(left <= col <= right for col in self.columns.get(row, ()))
        ]

    def row_columns(self, box: Box, row: int) -> list[int]:
        return [col for col in self.columns.get(row, ()) if box[1] <= col <= box[3]]


def _overlaps_columns(a: Box, b: Box) -> bool:
    return a[1] <= b[3] and b[1] <= a[3]


def _union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _starts_new_table(index: _RowIndex, above: Box, below: Box) -> bool:
    """下の領域が新しい表か（False なら上の表の続き）。

    新しい表: 上の表の列の外にはみ出す・空の行が 2 行以上空く・題名の行（1 つのセル）で始まる・
    表頭らしい行（2 つ以上のセルがすべて文字）で始まり、列の組が上の表と違うか、後の行の同じ列に
    数値・日付がある。
    """
    if below[1] < above[1] or below[3] > above[3]:
        return True
    if _gap(above[2], below[0], _skipped_rows(index.grid)) >= _NEW_TABLE_GAP_ROWS:
        return True
    grid = index.grid
    rows = index.rows(below)
    if not rows:
        return False
    first = index.row_columns(below, rows[0])
    if len(first) == 1 and len(rows) > 1:
        return True
    if len(first) < 2 or not all(is_textual(grid.cells[(rows[0], col)]) for col in first):
        return False
    above_columns = {col for row in index.rows(above) for col in index.row_columns(above, row)}
    below_columns = {col for row in rows for col in index.row_columns(below, row)}
    if below_columns != above_columns:
        return True
    following = rows[1 : 1 + _CONTINUATION_LOOKAHEAD_ROWS]
    return any(
        not is_textual(value)
        for row in following
        for col in first
        if (value := grid.cells.get((row, col), "").strip())
    )


def _note_region(
    index: _RowIndex, above: Box, below: Box, markers: Sequence[re.Pattern[str]]
) -> bool:
    """下の領域が上の表の注記の行だけか（#3566）。

    どの行もセルが 1 つ（横の結合を含む）で、注記の印で始まるか長い文。印の無い長い文と番号だけの
    印（「(1)」）の行は、表のすぐ下（空の行が ``_NEW_TABLE_GAP_ROWS`` 未満）のときだけ注記にする。
    """
    rows = index.rows(below)
    if not rows:
        return False
    near = _gap(above[2], below[0], _skipped_rows(index.grid)) < _NEW_TABLE_GAP_ROWS
    for row in rows:
        cols = index.row_columns(below, row)
        if len(cols) != 1:
            return False
        text = index.grid.cells[(row, cols[0])].strip()
        marker = note_marker(text, markers)
        if marker is None:
            if not near or len(text) < NOTE_MIN_CHARS:
                return False
        elif is_numbering(marker) and not near:
            return False
    return True


def _skipped_rows(grid: Grid) -> set[int]:
    return grid.hidden_rows - grid.filter_rows


def _skipped_columns(grid: Grid) -> set[int]:
    return grid.hidden_columns


def split_regions(
    grid: Grid,
    *,
    gap_rows: int = 1,
    gap_columns: int = 1,
    note_markers: Sequence[re.Pattern[str]] = (),
) -> list[Box]:
    """シートを表ごとの領域（上・左・下・右）に分ける（上から下、左から右の順）。

    ``note_markers`` は表の下の注記の行の先頭の印（#3566）。注記の行だけの領域は上の表につなぐ。
    """
    # 読まなかった非表示の行・列は、表の区切りの空きにしない（#2526）。
    skipped = (_skipped_rows(grid), _skipped_columns(grid))
    boxes = _xy_cut(_occupied(grid), gap_rows, gap_columns, skipped)
    if len(boxes) <= 1:
        return boxes
    index = _RowIndex(grid)
    # 1 行だけの領域: セルが 1 つ（題名の形）なら、すぐ下の列の重なる領域の題名にする（無ければ
    # すぐ上の領域につなぐ）。セルが 2 つ以上の行（空の行の後の表の続き・合計の行など）は、すぐ上の
    # 列の重なる表の続きにする（無ければすぐ下の領域につなぐ）。データの 1 行を下の表の題名にして
    # 上下の表をつながない（#2528）。
    merged: list[Box] = []
    pending = list(boxes)
    while pending:
        box = pending.pop(0)
        rows = index.rows(box)
        if len(rows) == 1:
            below = [
                other for other in pending if other[0] > box[2] and _overlaps_columns(box, other)
            ]
            above = [
                other for other in merged if other[2] < box[0] and _overlaps_columns(box, other)
            ]
            title = len(index.row_columns(box, rows[0])) == 1
            if title and above and below:
                # 注記の印で始まる 1 行は、下の表の題名にせず上の表の注記にする（#3566）。番号だけの
                # 印（「(1)」）は題名の番号と区別できないので、上の表に近いときだけ。
                target_above = max(above, key=lambda other: other[2])
                target_below = min(below, key=lambda other: other[0])
                skipped_rows = _skipped_rows(grid)
                marker = note_marker(
                    grid.cells[(rows[0], index.row_columns(box, rows[0])[0])], note_markers
                )
                if marker is not None and (
                    not is_numbering(marker)
                    or _gap(target_above[2], box[0], skipped_rows)
                    < _gap(box[2], target_below[0], skipped_rows)
                ):
                    merged[merged.index(target_above)] = _union(target_above, box)
                    continue
            if below and (title or not above):
                target = min(below, key=lambda other: other[0])
                pending[pending.index(target)] = _union(box, target)
                continue
            if above:
                target = max(above, key=lambda other: other[2])
                merged[merged.index(target)] = _union(target, box)
                continue
        merged.append(box)
    # 表頭らしい行で始まらない領域は、すぐ上の列の重なる表の続きにする。
    result: list[Box] = []
    for box in merged:
        above = [other for other in result if other[2] < box[0] and _overlaps_columns(box, other)]
        if above:
            target = max(above, key=lambda other: other[2])
            if _note_region(index, target, box, note_markers) or not _starts_new_table(
                index, target, box
            ):
                result[result.index(target)] = _union(target, box)
                continue
        result.append(box)
    return sorted(result, key=lambda box: (box[0], box[1]))


# ---- 帳票の単位と判定 ------------------------------------------------------------------


def grid_units(grid: Grid) -> tuple[list[Unit], list[Unit]]:
    """値のある単位と、値の無い結合セル（空欄の項目）の単位（左上の順）。"""
    covered: set[tuple[int, int]] = set()
    units: list[Unit] = []
    blanks: list[Unit] = []
    for top, left, bottom, right in grid.merges:
        covered.update(
            (row, col) for row in range(top, bottom + 1) for col in range(left, right + 1)
        )
        text = grid.cells.get((top, left), "").strip()
        shape = (top, left) in grid.shape_boxes
        unit = Unit(top, left, bottom, right, text, _hinted(grid, top, left, text), shape)
        (units if text else blanks).append(unit)
    for (row, col), value in grid.cells.items():
        text = value.strip()
        if text and (row, col) not in covered:
            shape = (row, col) in grid.shape_boxes
            units.append(Unit(row, col, row, col, text, _hinted(grid, row, col, text), shape))
    key = lambda unit: (unit.top, unit.left)  # noqa: E731
    return sorted(units, key=key), sorted(blanks, key=key)


def _hinted(grid: Grid, row: int, col: int, text: str) -> bool:
    return (row, col) in grid.label_hints or text.endswith(_LABEL_SUFFIXES)


class _Index:
    """単位を始まりの行ごとに左から並べた索引（右・下の隣を探す）。"""

    def __init__(self, units: list[Unit]) -> None:
        self.by_row: dict[int, list[Unit]] = defaultdict(list)
        for unit in units:
            self.by_row[unit.top].append(unit)
        for row_units in self.by_row.values():
            row_units.sort(key=lambda unit: unit.left)

    def right_of(self, unit: Unit, row: int, skip: set[Unit] | None = None) -> Unit | None:
        """``row`` に始まる、``unit`` のすぐ右（空の列 2 つまで）の単位。"""
        for other in self.by_row.get(row, ()):
            if other.left <= unit.right or (skip and other in skip):
                continue
            return other if other.left - unit.right - 1 <= _FORM_MAX_GAP_COLUMNS else None
        return None

    def below(self, unit: Unit, skip: set[Unit] | None = None) -> list[Unit]:
        """``unit`` のすぐ下（空の行 1 つまで）の行に始まる単位。"""
        for row in range(unit.bottom + 1, unit.bottom + 2 + _FORM_MAX_GAP_ROWS):
            found = [other for other in self.by_row.get(row, ()) if not (skip and other in skip)]
            if found:
                return found
        return []


def form_features(grid: Grid) -> tuple[dict[str, float], int]:
    """帳票の特徴（0〜1）と、判定に使った単位の数。"""
    units, _ = grid_units(grid)
    if units:
        first_row = units[0].top
        units = [unit for unit in units if unit.top < first_row + _FORM_SAMPLE_ROWS]
    if not units:
        return {name: 0.0 for name in _FORM_WEIGHTS}, 0
    merged = sum(1 for unit in units if unit.merged) / len(units)
    left = min(unit.left for unit in units)
    right = max(unit.right for unit in units)
    span = range(left, right + 1)
    narrow = sum(1 for col in span if grid.column_width(col) <= NARROW_COLUMN_WIDTH) / len(span)
    index = _Index(units)
    rows = sorted(index.by_row)
    similarities = []
    for upper, lower in zip(rows, rows[1:], strict=False):
        a = {(unit.left, unit.right) for unit in index.by_row[upper]}
        b = {(unit.left, unit.right) for unit in index.by_row[lower]}
        similarities.append(len(a & b) / len(a | b))
    misaligned = 1 - sum(similarities) / len(similarities) if similarities else 0.0
    # ラベルと値の隣接: 文字の単位のうち、すぐ右に手がかり（塗りつぶし・太字・末尾のコロン）の
    # 無い値の単位があり、その値が結合しているか、自分に手がかりがあるもの（ラベル）。表頭の下に
    # 値の並ぶ表は数えない（表頭の右は表頭）。帳票はおよそ半分の単位がラベルなので、ラベルの数を
    # 単位の数の半分で割って 1 を上限にする（#2530）。
    textual = [unit for unit in units if unit.textual]
    paired = 0
    for unit in textual:
        right_unit = index.right_of(unit, unit.top)
        if right_unit is not None and not right_unit.hinted and (right_unit.merged or unit.hinted):
            paired += 1
    label_value = min(1.0, 2 * paired / len(textual)) if textual else 0.0
    features = {
        "merged": round(merged, 3),
        "narrow": round(narrow, 3),
        "misaligned": round(misaligned, 3),
        "label_value": round(label_value, 3),
    }
    return features, len(units)


def detect_form_layout(grid: Grid) -> tuple[float, dict[str, float], str]:
    """帳票らしさの点数（0〜1）・特徴・理由。"""
    features, count = form_features(grid)
    if count < FORM_MIN_UNITS:
        return 0.0, features, f"単位が {count} 個で少ない（{FORM_MIN_UNITS} 個未満は帳票にしない）"
    score = sum(weight * features[name] for name, weight in _FORM_WEIGHTS.items())
    reason = (
        f"単位 {count}・結合セル {features['merged']:.0%}・狭い列 {features['narrow']:.0%}・"
        f"位置の不揃い {features['misaligned']:.0%}・ラベルと値の隣接 {features['label_value']:.0%}"
    )
    if (
        features["narrow"] < _REGULAR_GRID_NARROW
        and features["misaligned"] < _REGULAR_GRID_MISALIGNED
    ):
        score *= _REGULAR_GRID_FACTOR
        reason += "・位置のそろった格子（表らしい）"
    return round(min(max(score, 0.0), 1.0), 3), features, reason


# ---- 帳票の読み方 ----------------------------------------------------------------------


def _bounds(units: list[Unit]) -> str:
    return cell_ref(
        min(unit.top for unit in units),
        min(unit.left for unit in units),
        max(unit.bottom for unit in units),
        max(unit.right for unit in units),
    )


def _shape_cells(units: list[Unit]) -> list[str]:
    return [unit.ref for unit in units if unit.shape]


def _label_text(text: str) -> str:
    """ラベルの表示（末尾のコロンを除く。「届出日：」→「届出日」）。"""
    return text.strip().rstrip(":：").strip() or text.strip()


def _field(label: Unit | None, values: list[Unit], section: list[str]) -> SheetBlock:
    units = ([label] if label else []) + values
    value = " ".join(unit.text for unit in values)
    chosen = read_choice(value)
    return SheetBlock(
        kind="form_field",
        row_start=min(unit.top for unit in units),
        row_end=max(unit.bottom for unit in units),
        cell_range=_bounds(units),
        section_path=section,
        label=_label_text(label.text) if label else None,
        value=chosen if chosen is not None else value,
        label_cell=label.ref if label else None,
        value_cell=_bounds(values) if values else None,
        value_original=value if chosen is not None else None,
        shape_cells=_shape_cells(units),
    )


def inline_field(text: str) -> tuple[str, str] | None:
    """1 つのセルに書いた「ラベル：値」を分ける（分けられなければ None）。

    数値・日付のラベル（時刻の「10:30」）、数字で終わるラベルと数字で始まる値（「午前10:30」）、
    URL（「https://…」）は分けない。
    """
    match = _INLINE_FIELD.match(text.strip())
    if match is None:
        return None
    label = match.group("label").strip()
    value = match.group("value").strip()
    if not label or not value or not is_textual(label) or value.startswith("//"):
        return None
    if label[-1].isdigit() and value[0].isdigit():
        return None
    return label, value


def _inline_block(unit: Unit, label: str, value: str, section: list[str]) -> SheetBlock:
    chosen = read_choice(value)
    return SheetBlock(
        kind="form_field",
        row_start=unit.top,
        row_end=unit.bottom,
        cell_range=unit.ref,
        section_path=section,
        label=label,
        value=chosen if chosen is not None else value,
        label_cell=unit.ref,
        value_cell=unit.ref,
        value_original=value if chosen is not None else None,
        shape_cells=_shape_cells([unit]),
    )


def form_blocks(grid: Grid) -> tuple[list[SheetBlock], list[SheetDiagnostic]]:
    """帳票のシートを「ラベル: 値」の記録にする（左上の順）。"""
    units, blanks = grid_units(grid)
    index = _Index(units)
    blank_index = _Index(blanks)
    consumed: set[Unit] = set()
    blocks: list[SheetBlock] = []
    groups: list[Unit] = []

    # 縦に結合し、右の 2 行以上がそれぞれ「ラベル: 値」になる単位は、項目のまとまりの見出し。
    for unit in units:
        if unit.bottom == unit.top or not unit.textual:
            continue
        rights = {
            right
            for row in range(unit.top, unit.bottom + 1)
            if (right := index.right_of(unit, row)) is not None
        }
        if len(rights) >= 2 and all(index.right_of(item, item.top) for item in rights):
            groups.append(unit)
            consumed.add(unit)

    def group_path(unit: Unit) -> list[str]:
        return [
            group.text
            for group in groups
            if group.top <= unit.top <= group.bottom and unit.left > group.right
        ]

    # 手がかりのあるラベルが横に並び、すぐ下に同じ位置の値の行がある: 上のラベルと下の値を組にする。
    pending_vertical: list[tuple[Unit, SheetBlock]] = []
    for row in sorted(index.by_row):
        band = [unit for unit in index.by_row[row] if unit not in consumed]
        if len(band) < 2 or not all(unit.textual and unit.hinted for unit in band):
            continue
        labels = {(unit.left, unit.right): unit for unit in band}
        run: list[list[Unit]] = []
        anchor = band
        while True:
            below = [
                other
                for other in index.below(max(anchor, key=lambda unit: unit.bottom), consumed)
                if (other.left, other.right) in labels
            ]
            if len(below) < max(2, (len(labels) + 1) // 2) or any(item.hinted for item in below):
                break
            run.append(below)
            anchor = below
        if not run:
            continue
        consumed.update(band)
        if len(run) == 1:
            for cell in run[0]:
                label = labels[(cell.left, cell.right)]
                consumed.add(cell)
                pending_vertical.append((label, _field(label, [cell], group_path(label))))
            continue
        for values in run:
            consumed.update(values)
            names: dict[str, str] = {}
            for cell in values:
                name = _label_text(labels[(cell.left, cell.right)].text)
                if name in names:
                    name = f"{name}__{column_letter(cell.left)}"
                names[name] = cell.text
            blocks.append(
                SheetBlock(
                    kind="row",
                    row_start=min(cell.top for cell in values),
                    row_end=max(cell.bottom for cell in values),
                    cell_range=_bounds(values),
                    section_path=group_path(values[0]),
                    values=names,
                    shape_cells=_shape_cells(values),
                )
            )

    # 残りを左上から読む: ラベルとすぐ右（無ければすぐ下）の値を組にする。
    empty_fields = 0
    unpaired = 0
    section: list[str] = []
    heading_unused: Unit | None = None
    ordered = sorted(
        [unit for unit in units if unit not in consumed] + [label for label, _ in pending_vertical],
        key=lambda unit: (unit.top, unit.left),
    )
    vertical = dict(pending_vertical)
    for unit in ordered:
        if unit in vertical:
            blocks.append(
                vertical[unit].model_copy(update={"section_path": section + group_path(unit)})
            )
            heading_unused = None
            continue
        if unit in consumed:
            continue
        consumed.add(unit)
        path = section + group_path(unit)
        if not unit.textual:
            unpaired += 1
            blocks.append(_field(None, [unit], path))
            heading_unused = None
            continue
        inline = inline_field(unit.text)
        if inline is not None:
            # 1 つのセルの「ラベル：値」: それだけで項目にし、右の単位と組にしない（#2529）。
            blocks.append(_inline_block(unit, *inline, path))
            heading_unused = None
            continue
        value = index.right_of(unit, unit.top, consumed)
        blank = blank_index.right_of(unit, unit.top)
        if blank is not None and (value is None or blank.left < value.left):
            # 値の無い結合セルが右にある: 記入されていない項目（記録にしない）。
            empty_fields += 1
            continue
        if value is not None and value.hinted and not unit.hinted:
            value = None
        elif value is not None and value.hinted:
            # 手がかりのあるラベルが続く: このラベルの値は空。
            empty_fields += 1
            continue
        if value is None:
            candidates = [
                other
                for other in index.below(unit, consumed)
                if other.left == unit.left and not other.hinted
            ]
            if candidates and index.right_of(candidates[0], candidates[0].top) is None:
                value = candidates[0]
        if value is None and any(blank.left == unit.left for blank in blank_index.below(unit)):
            # すぐ下に値の無い結合セル（押印欄の枠など）: 記入されていない項目（#2529）。
            empty_fields += 1
            continue
        if value is None:
            if all(other in consumed for other in index.by_row[unit.top]):
                # 行にこの単位だけ: 以降の項目の見出し。
                if heading_unused is not None:
                    blocks.append(_field(None, [heading_unused], []))
                section = [unit.text]
                heading_unused = unit
            else:
                unpaired += 1
                blocks.append(_field(None, [unit], path))
                heading_unused = None
            continue
        consumed.add(value)
        values = [value]
        # 選択の記号と選択肢を別々のセルに置いた値（「□」「A」「☑」「B」）と、選択肢ごとの
        # チェックボックス（「☑ A」「☐ B」。#2537）は 1 つの値にする。
        while (
            starts_with_choice_mark(values[0].text)
            and (following := index.right_of(values[-1], values[-1].top, consumed)) is not None
            and not following.hinted
            and (is_choice_mark(values[-1].text) or starts_with_choice_mark(following.text))
        ):
            consumed.add(following)
            values.append(following)
        # 値の続き（単位の表示「円」・縦に結合したラベルの下の行の値）。
        while (
            (following := index.right_of(values[-1], values[-1].top, consumed)) is not None
            and not following.hinted
            and index.right_of(following, following.top) is None
            and blank_index.right_of(following, following.top) is None
        ):
            consumed.add(following)
            values.append(following)
        for row in range(unit.top + 1, unit.bottom + 1):
            extra = index.right_of(unit, row, consumed)
            if extra is not None and not extra.hinted and index.right_of(extra, extra.top) is None:
                consumed.add(extra)
                values.append(extra)
        blocks.append(_field(unit, values, path))
        heading_unused = None
    if heading_unused is not None:
        blocks.append(_field(None, [heading_unused], []))
    diagnostics: list[SheetDiagnostic] = []
    if empty_fields:
        diagnostics.append(SheetDiagnostic(code="form_empty_fields", detail=str(empty_fields)))
    if unpaired:
        diagnostics.append(SheetDiagnostic(code="form_unpaired_texts", detail=str(unpaired)))
    choices = sum(1 for block in blocks if block.value_original is not None)
    if choices:
        diagnostics.append(SheetDiagnostic(code="form_choices_read", detail=str(choices)))
    blocks.sort(key=lambda block: (block.row_start, _column_of(block.cell_range)))
    return blocks, diagnostics


def _column_of(cell_range: str) -> int:
    letters = "".join(char for char in cell_range.split(":", 1)[0] if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index
