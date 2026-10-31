"""必要な根拠（required evidence）と引用の chunk の照合（#1284・#2065・#2086）。

根拠の条件（文書・ページ・シート・行・章節・本文）のうち、書いた条件をすべて満たす chunk が 1 つでも
あれば、その根拠を取れたとみなす。chunk の場所は RAG の `EvidenceLocator` と同じ名前の項目
（`page_start` / `page_end` / `page_number` / `sheet_name` / `row_start` / `row_end` /
`section_path`）で受け取る（RAG の chunk の metadata と、Agent の観測の `locator` が同じ名前）。

行の条件は、同じ行の番号の基準（`row_basis`）の中だけで照合する（#3429）。評価セットの行は原本の
行（表計算のシート・CSV の記録・Word の表）なので、解析が組み立てた表の格子の行
（`row_basis=parsed_table`。原本の座標ではない）には一致させない。条件が基準（`row_basis`）を持つ
ときは、同じ基準の chunk だけに一致させる。
"""

from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Protocol

from pr_evaluation_scoring.text import contains_normalized, normalize_name


class EvidenceCriteria(Protocol):
    """根拠の条件（評価セットの `required_evidence[]` の項目と同じ名前）。"""

    @property
    def document_id(self) -> str | None: ...

    @property
    def text(self) -> str | None: ...

    @property
    def page_start(self) -> int | None: ...

    @property
    def page_end(self) -> int | None: ...

    @property
    def section(self) -> str | None: ...

    @property
    def sheet(self) -> str | None: ...

    @property
    def row_start(self) -> int | None: ...

    @property
    def row_end(self) -> int | None: ...


def location_matches(
    criteria: EvidenceCriteria, *, document_id: str, locator: Mapping[str, object]
) -> bool:
    """本文の条件を除く条件（文書・ページ・シート・行・章節）をすべて満たすか。

    `document_id` は比べる文書の ID（同じ文書とみなす文書を正解の文書の ID にそろえたもの）。
    場所の分からない chunk は、場所の条件に一致しない。
    """
    if criteria.document_id is not None and document_id != criteria.document_id:
        return False
    if criteria.page_start is not None and not _ranges_overlap(
        _chunk_range(locator, "page_start", "page_end", fallback="page_number"),
        criteria.page_start,
        criteria.page_end,
    ):
        return False
    if criteria.sheet is not None and normalize_name(locator.get("sheet_name")) != normalize_name(
        criteria.sheet
    ):
        return False
    if criteria.row_start is not None and not (
        _row_basis_matches(criteria, locator)
        and _ranges_overlap(
            _chunk_range(locator, "row_start", "row_end"), criteria.row_start, criteria.row_end
        )
    ):
        return False
    # 章節の見出しの道筋は、値の文字列の形（RAG の chunk の metadata と同じ）で照合する。
    return criteria.section is None or contains_normalized(
        str(locator.get("section_path") or ""), criteria.section
    )


def text_matches(chunk_text: str, phrase: str) -> bool:
    """chunk の本文が根拠の語句を含むか。

    表（`<table>`）を含む本文は、表の外の本文（表をまたいでつながない）と、表の 1 行のセルの本文
    （HTML のタグを含めない）で照合する。行を連結すると、別の行にある値を組み合わせて根拠に
    してしまう。
    """
    if "<table" not in chunk_text.lower():
        return contains_normalized(chunk_text, phrase)
    if any(contains_normalized(text, phrase) for text in text_outside_tables(chunk_text)):
        return True
    return any(contains_normalized(text, phrase) for text in table_row_texts(chunk_text))


def evidence_matches(
    criteria: EvidenceCriteria, *, document_id: str, text: str, locator: Mapping[str, object]
) -> bool:
    """chunk が根拠の条件（文書・ページ・シート・行・章節・本文）をすべて満たすか。"""
    if not location_matches(criteria, document_id=document_id, locator=locator):
        return False
    return criteria.text is None or text_matches(text, criteria.text)


# 解析が組み立てた表の格子の行の基準（RAG の `EvidenceLocator.row_basis`）。
PARSED_TABLE_ROW_BASIS = "parsed_table"


def _row_basis_matches(criteria: EvidenceCriteria, locator: Mapping[str, object]) -> bool:
    """行の番号の基準が同じか（基準を書かない条件の行は原本の行で、解析の表の行と比べない）。"""
    found = locator.get("row_basis")
    if not isinstance(found, str) or not found:
        found = "sheet" if locator.get("sheet_name") else None
    expected = getattr(criteria, "row_basis", None)
    if isinstance(expected, str) and expected:
        return found == expected
    return found != PARSED_TABLE_ROW_BASIS


def _ranges_overlap(found: tuple[int, int] | None, start: int, end: int | None) -> bool:
    """chunk の範囲（分からなければ None で、一致しない）が、根拠の範囲と重なるか。"""
    return found is not None and found[1] >= start and found[0] <= (end or start)


def _chunk_range(
    locator: Mapping[str, object], start_key: str, end_key: str, *, fallback: str | None = None
) -> tuple[int, int] | None:
    start = _positive_int(locator.get(start_key))
    if start is None and fallback is not None:
        start = _positive_int(locator.get(fallback))
    if start is None:
        return None
    end = _positive_int(locator.get(end_key)) or start
    return start, max(start, end)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number >= 1 else None
    return None


# ---- 表の本文 ---------------------------------------------------------------------------------


class _OutsideTableText(HTMLParser):
    """表の外の本文。表の前後は別の区切りにし、表をまたいで本文をつなげない。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.segments = [""]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self.depth == 0:
                self.segments.append("")
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.segments[-1] += data


class _TableRows(HTMLParser):
    """外側の表の行ごとのセルの本文。

    入れ子の表は外側のセルの本文として平坦化する。HTML5 で省略できる `</td>` `</th>` `</tr>` は、
    次の行・セルの開始で閉じる。表の見出し（caption）は行に入れない。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.depth += 1
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._close_cell()
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.depth > 1:
            if tag == "table":
                self.depth -= 1
            elif self._cell is not None and tag in {"td", "th", "tr"}:
                self._cell.append("\n" if tag == "tr" else " ")
            return
        if tag in {"td", "th"}:
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            self._close_row()
        elif tag == "table" and self.depth > 0:
            self._close_cell()
            self._close_row()
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
        self._cell = None

    def _close_row(self) -> None:
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None


def text_outside_tables(html: str) -> list[str]:
    """表の外の本文（表の前・表の間・表の後を別々に）。"""
    parser = _OutsideTableText()
    parser.feed(html)
    parser.close()
    return parser.segments


def table_row_texts(html: str) -> list[str]:
    """外側の表の行ごとに、セルの本文を空白でつないだ文字列（空の行は除く）。"""
    parser = _TableRows()
    parser.feed(str(html or ""))
    parser.close()
    return [" ".join(row) for row in parser.rows if row]
