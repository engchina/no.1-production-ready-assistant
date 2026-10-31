"""HTML table をチャンク検索向けの構造化テキストへ変換する。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Any

from rag_engine.parsing.content_labels import content_labels

DEFAULT_TABLE_ROW_GROUP_TARGET_CHARS = 900
# Vision 由来の HTML に極端な rowspan/colspan があっても、span の展開量を入力値に比例させない。
# `_position_cells` は span を 1 セルずつ展開するため上限の 2 乗が処理量になる。1000 では 1 セルで 10^6 に
# なり数十秒・数百 MB を消費した (#749)。実在の表で 50 行 / 50 列を超える結合セルは想定しない。
MAX_TABLE_SPAN = 50


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.caption_parts: list[str] = []
        self.current_row: list[dict[str, Any]] | None = None
        self.current_cell: dict[str, Any] | None = None
        self.rows: list[list[dict[str, Any]]] = []
        self.caption = ""
        self._in_caption = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self.table_depth += 1
            return
        if self.table_depth < 1:
            return
        if self.table_depth > 1:
            # 入れ子の表は外側のセル本文として平坦化する。内側の tr/td で外側の行を作り直さない。
            return
        if tag == "caption":
            self._in_caption = True
            self.caption_parts = []
            return
        # HTML5 では </td> </th> </tr> の省略が合法。次の tr / td の開始で、未確定の行・セルを先に確定する (#788)。
        if tag == "tr":
            self._close_cell()
            self._close_row()
            self.current_row = []
            return
        if tag in {"td", "th"} and self.current_row is not None:
            self._close_cell()
            self.current_cell = {
                "tag": tag,
                "attrs": {str(key).lower(): value for key, value in attrs},
                "parts": [],
            }
            return
        if tag == "br" and self.current_cell is not None:
            self.current_cell["parts"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.table_depth > 1:
            if tag == "table":
                self.table_depth -= 1
            elif self.current_cell is not None and tag in {"td", "th", "tr"}:
                self.current_cell["parts"].append("\n" if tag == "tr" else " ")
            return
        if tag in {"td", "th"}:
            self._close_cell()
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            return
        if tag == "caption" and self._in_caption:
            self.caption = _normalize_cell_text("".join(self.caption_parts))
            self.caption_parts = []
            self._in_caption = False
            return
        if tag == "table" and self.table_depth > 0:
            self._close_cell()
            self._close_row()
            self.table_depth -= 1

    def _close_cell(self) -> None:
        """未確定のセルを現在の行へ確定する。閉じタグ省略時にも呼ばれる。"""
        if self.current_cell is None or self.current_row is None:
            self.current_cell = None
            return
        cell = self.current_cell
        self.current_row.append(
            {
                "tag": cell["tag"],
                "attrs": cell["attrs"],
                "text": _normalize_cell_text("".join(cell["parts"])),
            }
        )
        self.current_cell = None

    def _close_row(self) -> None:
        """未確定の行を確定する。閉じタグ省略時にも呼ばれる。"""
        if self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell["parts"].append(data)
        elif self._in_caption:
            self.caption_parts.append(data)


def parse_html_table_structure(html: str) -> dict[str, Any]:
    """HTML table を行・セル・span 情報を持つ構造へ解析します。"""
    parser = _HTMLTableParser()
    parser.feed(str(html or ""))
    parser.close()
    positioned_rows = _position_cells(parser.rows)
    if not positioned_rows:
        return {}

    row_count = len(positioned_rows)
    column_count = max(
        (cell["column_index"] + cell["colspan"] - 1 for row in positioned_rows for cell in row["cells"]),
        default=0,
    )
    header_row_count = _header_row_count(positioned_rows)
    column_headers = _column_headers(positioned_rows[:header_row_count])
    return {
        "format": "html_table",
        "caption": parser.caption,
        "row_count": row_count,
        "column_count": column_count,
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        "rows": positioned_rows,
    }


def table_row_groups(
    structure: dict[str, Any],
    *,
    target_chars: int = DEFAULT_TABLE_ROW_GROUP_TARGET_CHARS,
    heading_text: str = "",
) -> list[dict[str, Any]]:
    """大きな table を検索 context 向けの行 group に分割します。"""
    rows = structure.get("rows") if isinstance(structure.get("rows"), list) else []
    if not rows:
        return []
    header_row_count = max(0, int(structure.get("header_row_count") or 0))
    data_rows = rows[header_row_count:] if header_row_count < len(rows) else rows
    if not data_rows:
        data_rows = rows

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    target = max(120, int(target_chars or DEFAULT_TABLE_ROW_GROUP_TARGET_CHARS))
    # group の境界は table_row_group_text の長さで決める。候補ごとに全文を作り直すと行数の 2 乗になる
    # （1500 行で 13 秒）ので、固定部と各行の長さの累積で同じ長さを求める (#805)。
    fixed = "\n".join(_fixed_group_pieces(structure, heading_text))
    inherited = _inherited_cells_by_row(structure)
    line_count = line_total = 0
    for row in data_rows:
        line = _row_line(row, structure, inherited)
        candidate_count = line_count + (1 if line else 0)
        candidate_total = line_total + len(line)
        if current and _joined_length(fixed, candidate_count, candidate_total) > target:
            groups.append(current)
            current, line_count, line_total = [row], (1 if line else 0), len(line)
        else:
            current.append(row)
            line_count, line_total = candidate_count, candidate_total
    if current:
        groups.append(current)

    return [_row_group_payload(structure, group, heading_text=heading_text) for group in groups]


def _joined_length(fixed: str, line_count: int, line_total: int) -> int:
    """`"\n".join([*固定部, *行]).strip()` の長さを、文字列を作らずに求める。

    固定部の先頭と行の末尾は空白で終わらない（行は `_row_text` の stripped な部分の結合）ため、行が
    1 つでもあれば strip は長さを変えない。行が無いときだけ固定部の末尾空白が strip で落ちる。
    """
    if line_count == 0:
        return len(fixed.strip())
    return (len(fixed) + 1 if fixed else 0) + line_total + line_count - 1


def _fixed_group_pieces(structure: dict[str, Any], heading_text: str) -> list[str]:
    """行 group の先頭に付く固定部（関連見出し・表タイトル・列見出し）。"""
    labels = content_labels()
    pieces: list[str] = []
    if heading_text:
        pieces.append(f"{labels.text('related_headings')}:\n{heading_text}")
    caption = str(structure.get("caption") or "").strip()
    if caption:
        pieces.append(f"{labels.text('table_title')}: {caption}")
    headers_text = _headers_text(structure)
    if headers_text:
        pieces.append(f"{labels.text('column_headers')}: {headers_text}")
    return pieces


def _row_line(row: dict[str, Any], structure: dict[str, Any], inherited: dict[int, list[dict[str, Any]]]) -> str:
    row_text = _row_text(row, structure, inherited)
    return f"行 {row.get('row_index')}: {row_text}" if row_text else ""


def _inherited_cells_by_row(structure: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """rowspan で下の行へ続くセルを、続く先の行 index ごとに 1 回で集める（`_row_text` の探索の前計算）。"""
    inherited: dict[int, list[dict[str, Any]]] = {}
    for other in structure.get("rows") or []:
        if not isinstance(other, dict):
            continue
        for cell in other.get("cells") or []:
            start = int(cell.get("row_index") or 0)
            for row_index in range(start + 1, start + max(1, int(cell.get("rowspan") or 1))):
                inherited.setdefault(row_index, []).append(cell)
    return inherited


def table_row_group_text(
    structure: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    *,
    heading_text: str = "",
) -> str:
    """table heading と行 group を検索・回答用 text に整形します。"""
    pieces = _fixed_group_pieces(structure, heading_text)
    inherited = _inherited_cells_by_row(structure)
    for row in rows:
        line = _row_line(row, structure, inherited)
        if line:
            pieces.append(line)
    return "\n".join(pieces).strip()


def table_structure_excerpt(
    structure: dict[str, Any],
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """table 全体の短い構造 summary を作ります。"""
    return {
        "format": str(structure.get("format") or "html_table"),
        "caption": str(structure.get("caption") or ""),
        "row_count": int(structure.get("row_count") or 0),
        "column_count": int(structure.get("column_count") or 0),
        "header_row_count": int(structure.get("header_row_count") or 0),
        "column_headers": list(structure.get("column_headers") or []),
        "rows": [dict(row) for row in rows],
    }


def _position_cells(raw_rows: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    positioned_rows: list[dict[str, Any]] = []
    covered: set[tuple[int, int]] = set()
    for row_index, raw_cells in enumerate(raw_rows, start=1):
        cells = []
        column_index = 1
        for raw_cell in raw_cells:
            while (row_index, column_index) in covered:
                column_index += 1
            rowspan = _positive_int(raw_cell.get("attrs", {}).get("rowspan"))
            colspan = _positive_int(raw_cell.get("attrs", {}).get("colspan"))
            cell = {
                "row_index": row_index,
                "column_index": column_index,
                "rowspan": rowspan,
                "colspan": colspan,
                "is_header": raw_cell.get("tag") == "th",
                "text": str(raw_cell.get("text") or ""),
            }
            cells.append(cell)
            for covered_row in range(row_index, row_index + rowspan):
                for covered_col in range(column_index, column_index + colspan):
                    if covered_row == row_index and covered_col == column_index:
                        continue
                    covered.add((covered_row, covered_col))
            column_index += colspan
        if cells:
            positioned_rows.append({"row_index": row_index, "cells": cells})
    return positioned_rows


def _header_row_count(rows: Sequence[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        cells = row.get("cells") if isinstance(row.get("cells"), list) else []
        # 本文行の行見出し（th）だけで、行全体を列見出しとして扱わないようにします。
        if not any(cell.get("is_header") for cell in cells) or not all(cell.get("is_header") or not cell.get("text") for cell in cells):
            break
        count += 1
    return count


def _column_headers(header_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    headers_by_column: dict[int, str] = {}
    for row in header_rows:
        for cell in row.get("cells") or []:
            if not cell.get("is_header"):
                continue
            text = str(cell.get("text") or "").strip()
            if not text:
                continue
            start = int(cell.get("column_index") or 0)
            colspan = max(1, int(cell.get("colspan") or 1))
            for column_index in range(start, start + colspan):
                headers_by_column[column_index] = text
    return [{"column_index": column_index, "text": text} for column_index, text in sorted(headers_by_column.items())]


def _row_group_payload(
    structure: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    *,
    heading_text: str,
) -> dict[str, Any]:
    row_indexes = [int(row.get("row_index") or 0) for row in rows]
    return {
        "row_start": min(row_indexes) if row_indexes else 0,
        "row_end": max(row_indexes) if row_indexes else 0,
        "row_count": len(rows),
        "column_count": int(structure.get("column_count") or 0),
        "text": table_row_group_text(structure, rows, heading_text=heading_text),
        "structure": table_structure_excerpt(structure, rows),
    }


def _row_text(row: dict[str, Any], structure: dict[str, Any], inherited_by_row: dict[int, list[dict[str, Any]]] | None = None) -> str:
    """1行を「列見出し: 値」で表す。結合セルの適用範囲を落とさない。

    colspan の値は先頭列だけでなく覆う列全体（「A～B」）に対応付ける。上の行から
    rowspan で続く行見出しは、この行にも付ける。どちらも欠けると、期間全体の値が
    初日だけの値に見えたり、内訳行（入金・出金など）が何の内訳か分からなくなる。
    inherited_by_row は `_inherited_cells_by_row` の前計算。無ければこの行の分だけ全行を走査する。
    """
    headers = {
        int(item.get("column_index") or 0): str(item.get("text") or "")
        for item in structure.get("column_headers") or []
        if isinstance(item, dict)
    }
    row_index = int(row.get("row_index") or 0)
    # 上の行から rowspan で続くセルは、見出し（th）でも値（td。「入金」の内訳行など）でも引き継ぐ。
    if inherited_by_row is None:
        inherited_by_row = _inherited_cells_by_row(structure)
    inherited = inherited_by_row.get(row_index, [])
    labels = content_labels()
    column, joiner = labels.text("column_number"), labels.text("column_range")
    parts = []
    for cell in sorted([*inherited, *(row.get("cells") or [])], key=lambda c: int(c.get("column_index") or 0)):
        text = str(cell.get("text") or "").strip()
        if not text:
            continue
        first = int(cell.get("column_index") or 0)
        last = first + max(1, int(cell.get("colspan") or 1)) - 1
        names = list(dict.fromkeys(headers[i] for i in range(first, last + 1) if headers.get(i)))
        header = (names[0] if len(names) == 1 else f"{names[0]}{joiner}{names[-1]}") if names else f"{column}{first}"
        parts.append(f"{header}: {text}")
    return " / ".join(parts)


def _headers_text(structure: dict[str, Any]) -> str:
    headers = []
    for item in structure.get("column_headers") or []:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            headers.append(str(item["text"]).strip())
    return " / ".join(dict.fromkeys(headers))


def _normalize_cell_text(text: str) -> str:
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return min(max(1, parsed), MAX_TABLE_SPAN)


def unassigned_table_lines(native_lines: Sequence[str], rows: Sequence[Sequence[str]], other_texts: Sequence[str] = ()) -> list[str]:
    """表の範囲にある原文の行のうち、どの行のセルにも収まっていない行を返します。

    native_lines は PDF の text layer から取った表の範囲の行、rows は解析した表の各行のセル本文。
    解析器は text cell を bbox の重なりでセルへ割り当てるため、グリフの bbox が不正な PDF では
    文字がどのセルにも入らない、または隣の行のセルへ入ることがある。行内の語がすべて同じ1行の
    セルに含まれていれば、その行は割り当て済みとみなす（複数行のセルでは原文の1行が列をまたぐため、
    行全体の連続一致は要求しない）。other_texts は同じページの表以外の本文で、表の範囲に重なる脚注や
    見出しなど、既に別の record として出ている行を補足に含めないために使う。
    """
    compact_rows = [_compact("".join(cells)) for cells in rows] + [_compact(text) for text in other_texts]
    missing: list[str] = []
    for line in native_lines:
        tokens = [_compact(token) for token in str(line).split()]
        tokens = [token for token in tokens if token]
        if not tokens:
            continue
        if any(all(token in row for token in tokens) for row in compact_rows):
            continue
        text = " ".join(str(line).split())
        if text not in missing:
            missing.append(text)
    return missing


def misplaced_cell_repairs(rows: Sequence[Sequence[tuple[str, bool]]], native_lines: Sequence[str]) -> list[tuple[int, int, str]]:
    """隣の行のセルへ入った文字を text layer の行を根拠に元のセルへ戻す修正を返します。

    rows は表の各行のセルを (本文, 自セルか) の並びで表したもの。span で覆われた位置は
    span 元の本文と False を置く。native_lines は PDF の text layer から取った表の範囲の行。
    グリフの bbox が下へずれた PDF では、ある行の値が隣の行の同じ列のセルへ入り、
    「Input Input」と空セルの組になる。Docling の割り当ては変えられないので、事後に直す。

    対象は (a) ほぼ全行が埋まっている列（非空 3 件以上・空は 1 件か非空の 4 分の 1 以下）の空セル、
    (b) その上下隣にある複数 token のセル、(c) 同じ token が連続するセル。
    行の他の自セル本文をすべて含む text layer の行が一意に決まる場合だけ、その行から
    同じ行のセル（span で覆う分も含む）に含まれる token を除いた残りを候補にする。
    候補は同じ列にある既存の文字の再配分に限る。非空セルは元本文の一部、空セルは上下隣の
    セル本文の一部であることを必須にし、text layer から新しい文字を発明しない。
    戻り値は (行 index, 列 index, 修正後本文)。
    """
    if not rows or not native_lines:
        return []
    compact_lines = [_compact(line) for line in native_lines]

    def own_text(row: int, col: int) -> str | None:
        if 0 <= row < len(rows) and col < len(rows[row]) and rows[row][col][1]:
            return rows[row][col][0]
        return None

    flagged: set[tuple[int, int]] = set()
    for col in range(max(len(row) for row in rows)):
        column = [(row, text) for row in range(len(rows)) if (text := own_text(row, col)) is not None]
        filled = [row for row, text in column if text.strip()]
        empty = [row for row, text in column if not text.strip()]
        if len(filled) < 3 or len(empty) > max(1, len(filled) // 4):
            continue
        for row in empty:
            flagged.add((row, col))
            for neighbor in (row - 1, row + 1):
                if len((own_text(neighbor, col) or "").split()) >= 2:
                    flagged.add((neighbor, col))
    for row, cells in enumerate(rows):
        for col, (text, is_own) in enumerate(cells):
            tokens = text.split()
            if is_own and any(a == b for a, b in zip(tokens, tokens[1:], strict=False)):
                flagged.add((row, col))

    repairs: list[tuple[int, int, str]] = []
    for row, col in sorted(flagged):
        old = rows[row][col][0]
        others = [_compact(t) for i, (t, is_own) in enumerate(rows[row]) if i != col and is_own and _compact(t)]
        if not others:
            continue
        matched = [i for i, line in enumerate(compact_lines) if all(other in line for other in others)]
        if len(matched) != 1:
            continue
        covering = [_compact(t) for i, (t, _) in enumerate(rows[row]) if i != col and _compact(t)]
        residual = " ".join(token for token in native_lines[matched[0]].split() if not any(_compact(token) in text for text in covering))
        compact_residual = _compact(residual)
        if not compact_residual:
            continue
        # ponytail: 同じ列の既存文字の再配分だけを許す。完全に落ちた文字は直さず、
        # 従来どおり unassigned_table_lines の補足に任せる。
        if old.strip():
            valid = compact_residual != _compact(old) and compact_residual in _compact(old)
        else:
            valid = any(compact_residual in _compact(own_text(neighbor, col) or "") for neighbor in (row - 1, row + 1))
        if valid:
            repairs.append((row, col, residual))
    return repairs


def _compact(value: str) -> str:
    return "".join(str(value).split())
