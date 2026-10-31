"""1 行の記録の根拠を、同じ表の行として組み直す（#2594・#3262）。

表計算・CSV の表は 1 データ行 = 1 根拠（「列名: 値 / 列名: 値」）で、1 つの根拠の本文には表の行が 1 つしか無い。
表の行・列の対応の検査（`table_binding`）は根拠の本文の表を読むので、別の行の根拠の値を結び付けた段落
（ある行の名前と、別の行の根拠の値）を、根拠ごとの本文では見つけられない。

ここでは、同じ表の 1 行の記録の根拠（呼び出し側が ``table_record`` に、同じ表を示す ``group``〔文書・処理の版
``chunk_set_id``・行の番号の基準 ``row_basis``・表の番号〕、行の番号 ``row``、値のあるセルの列と表頭の段
``cells``〔`table_cells` と同じ。本文の順〕を入れたもの）を、行の番号の順に 1 つの HTML の表に組み直す。検査は
組み直した表も根拠の表として読む。

本文から値を取り出すのは、セルの数・表頭の段と本文の区切りから 1 通りに決まるときだけ（決まらない根拠は使わない。
推測で表を作らない）。行が 2 つ以上の表だけを返す。
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rag_engine.generation.table_binding import index_column

# 根拠の項目の key（呼び出し側が chunk の metadata から作る）。
TABLE_RECORD_KEY = "table_record"
# 1 行の記録の本文の区切り（`rag_parser_core` の記録の本文と同じ）。
_FIELD_SEPARATOR = " / "
_NAME_SEPARATOR = ": "
# 1 つの表に組み直す行・列の上限（大きい表を丸ごと組み直さない）。
_MAX_ROWS = 500
_MAX_COLUMNS = 100


@dataclass(frozen=True)
class TableRecord:
    """1 行の記録の根拠の、表の中の位置（同じ表の印・行の番号・値のあるセルの列と表頭の段）。"""

    group: str
    row: int
    cells: tuple[tuple[str, tuple[str, ...]], ...]


def table_record(item: Mapping[str, Any]) -> TableRecord | None:
    """根拠の項目の ``table_record``。形の合わない値は None（使わない）。"""
    raw = item.get(TABLE_RECORD_KEY)
    if not isinstance(raw, Mapping):
        return None
    group, row, cells = raw.get("group"), raw.get("row"), raw.get("cells")
    if not isinstance(group, str) or not group or not isinstance(row, int) or isinstance(row, bool) or row < 1:
        return None
    if not isinstance(cells, Sequence) or isinstance(cells, str) or not cells or len(cells) > _MAX_COLUMNS:
        return None
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for cell in cells:
        if not isinstance(cell, Sequence) or isinstance(cell, str) or len(cell) < 2:
            return None
        column, path = cell[0], cell[1]
        if not isinstance(column, str) or not column.isalpha() or not column.isupper():
            return None
        if not isinstance(path, Sequence) or isinstance(path, str) or not all(isinstance(part, str) for part in path):
            return None
        parsed.append((column, tuple(path)))
    if len({column for column, _ in parsed}) != len(parsed):
        return None
    return TableRecord(group=group, row=row, cells=tuple(parsed))


def _column_number(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return number


def _field_value(part: str, prefix: str) -> str | None:
    """「列名: 値」の部分の値。列名が表頭の段（``prefix``）で始まらない・値が空なら None。"""
    if not part.startswith(prefix):
        return None
    position = part.find(_NAME_SEPARATOR, len(prefix))
    if position < 0:
        return None
    value = part[position + len(_NAME_SEPARATOR) :].strip()
    return value or None


def record_values(text: str, cells: Sequence[tuple[str, Sequence[str]]]) -> list[str] | None:
    """1 行の記録の本文（「列名: 値 / 列名: 値」）から、セルごとの値（``cells`` の順）を取り出す。

    列名は表頭の段を「 / 」でつないだ文字で始まる（補った列名「値_1」・役割の表示を付けた列名もそのまま）。列名・値が
    区切り「 / 」を含むと分け方が複数ありうるので、表頭の段と合う分け方が 1 通りのときだけ値を返す。
    """
    pieces = text.strip().split(_FIELD_SEPARATOR)
    prefixes = [_FIELD_SEPARATOR.join(path) for _, path in cells]
    count = len(prefixes)
    if not count or len(pieces) < count:
        return None
    # ways[i][j]: 先頭の i 個の部分を j 個のセルに分ける分け方の数（2 以上は 2 にまとめる）。
    ways = [[0] * (count + 1) for _ in range(len(pieces) + 1)]
    ways[0][0] = 1
    for end in range(1, len(pieces) + 1):
        for cell in range(1, min(end, count) + 1):
            total = 0
            for start in range(cell - 1, end):
                if ways[start][cell - 1] and _field_value(_FIELD_SEPARATOR.join(pieces[start:end]), prefixes[cell - 1]) is not None:
                    total += ways[start][cell - 1]
            ways[end][cell] = min(total, 2)
    if ways[len(pieces)][count] != 1:
        return None
    values: list[str] = []
    end = len(pieces)
    for cell in range(count, 0, -1):
        for start in range(end - 1, cell - 2, -1):
            value = _field_value(_FIELD_SEPARATOR.join(pieces[start:end]), prefixes[cell - 1])
            if ways[start][cell - 1] and value is not None:
                values.append(value)
                end = start
                break
    return values[::-1]


def record_tables(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """同じ表の 1 行の記録の根拠を、表ごとに 1 つの HTML の表（表頭の行と、行の番号の順のデータの行）にする。

    ``items`` は根拠の項目（``text`` と ``table_record``）。同じ行の根拠が 2 つあれば最初のものを使う。値を
    取り出せない根拠は入れず、行が 2 つ以上の表だけを返す。表頭の行は列ごとに表頭の段を「 / 」でつないだ文字。
    """
    groups: dict[str, dict[int, dict[str, str]]] = {}
    headers: dict[str, dict[str, str]] = {}
    for item in items:
        record = table_record(item)
        if record is None:
            continue
        rows = groups.setdefault(record.group, {})
        if record.row in rows or len(rows) >= _MAX_ROWS:
            continue
        values = record_values(str(item.get("text") or ""), record.cells)
        if values is None:
            continue
        rows[record.row] = {column: value for (column, _), value in zip(record.cells, values, strict=True)}
        header = headers.setdefault(record.group, {})
        for column, path in record.cells:
            header.setdefault(column, _FIELD_SEPARATOR.join(path))
    tables: list[str] = []
    for group, all_rows in groups.items():
        columns = sorted(headers.get(group, {}), key=_column_number)[:_MAX_COLUMNS]
        if not columns:
            continue
        # 行の区分・名前の列（先頭の列。先頭が行の番号の列なら次の列。検査と同じ読み方）の値が無い行は入れない。
        # 縦の結合の続き（表計算は上の値を写さない）と空欄を見分けられず、区分の無い行として読むと、その行の値を
        # 上の行の区分と結び付けた正しい段落を疑ってしまう。
        ordered = [[cells.get(column, "") for column in columns] for _, cells in sorted(all_rows.items())]
        label = columns[1] if index_column(ordered) else columns[0]
        rows = {row: cells for row, cells in all_rows.items() if cells.get(label)}
        if len(rows) < 2:
            continue
        lines = ["<table>"]
        if any(headers[group][column] for column in columns):
            lines.append("<tr>" + "".join(f"<th>{html.escape(headers[group][column])}</th>" for column in columns) + "</tr>")
        for row in sorted(rows):
            lines.append("<tr>" + "".join(f"<td>{html.escape(rows[row].get(column, ''))}</td>" for column in columns) + "</tr>")
        lines.append("</table>")
        tables.append("\n".join(lines))
    return tables


__all__ = ["TABLE_RECORD_KEY", "TableRecord", "record_tables", "record_values", "table_record"]
