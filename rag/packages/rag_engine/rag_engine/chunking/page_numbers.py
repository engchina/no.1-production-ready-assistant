"""頁番号のフッターから論理頁を決める（#2289）。

頁の下端の頁番号（``Page-footer`` の本文が頁番号だけの形）を文書全体で集め、物理頁に対して差が一定に
増える並びに乗る値だけを論理頁にする。1 頁だけ別の数（年・図の番号）を持つ頁は並びから外れるので使わない。

- 数の体系は算用数字・ローマ数字・漢数字。字と値は言語資源（``numbers.roman_digits`` /
  ``kanji_digits`` / ``kanji_multipliers``）。並びは同じ体系の頁番号だけでつなぐ（前付きのローマ数字と
  本文の算用数字は別の並び）。
- 論理頁の値は、算用数字・漢数字は数、ローマ数字は書かれた字のまま（「ii」）にする（本文の頁 2 と
  見分ける）。
- PDF の ``/PageLabels``（backend の ``page_labels.py``。#1244）は chunk の ``page_label_start`` /
  ``page_label_end`` に入る別の項目で、ここでは読まない・上書きしない。印刷の頁番号の正本は
  ``/PageLabels`` で、ここで決める論理頁（``layout.logical_page_labels``）は検索の文の補助に使う。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rag_parser_core.language.text import parse_number, parse_roman

from rag_engine.chunking.constants import _compact_page_text, _page_number_footer_pattern

ARABIC = "arabic"
ROMAN = "roman"
KANJI = "kanji"


@dataclass(frozen=True)
class PageNumber:
    """1 つの頁番号の候補（物理頁・値・数の体系・論理頁に入れる値）。"""

    page: int
    value: int
    system: str
    label: int | str


def read_page_number(text: str, page: int) -> PageNumber | None:
    """頁番号だけの文を読む（頁番号の形でない・読めない・0 以下なら None）。"""
    match = _page_number_footer_pattern().fullmatch(_compact_page_text(text))
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        value, system, label = int(token), ARABIC, int(token)
    elif (roman := parse_roman(token)) is not None:
        value, system, label = roman, ROMAN, token
    else:
        kanji = parse_number(token)
        if kanji is None:
            return None
        value, system, label = kanji, KANJI, kanji
    return PageNumber(page=page, value=value, system=system, label=label) if value > 0 else None


def select_logical_pages(
    candidates: Sequence[PageNumber],
    *,
    page_count: int,
    min_run_pages: int,
    offset_tolerance: int,
) -> dict[int, int | str]:
    """物理頁に対して差が一定に増える並びに乗る頁番号だけを、物理頁 → 論理頁で返す。

    頁の順に、同じ体系で値が増え、前の頁番号からの差のずれ（値の差 - 物理頁の差）が
    ``offset_tolerance`` 以下の並びへつなぐ（つなげる並びが複数あれば、ずれの小さい・長い並び）。
    ``min_run_pages``（文書の頁数がこれより少なければ文書の頁数）以上の頁を持つ並びを長い順に採り、
    採った並びの頁の範囲と重なる短い並び（並びの途中の外れた数）は使わない。
    """
    runs: list[list[PageNumber]] = []
    for candidate in sorted(candidates, key=lambda item: item.page):
        best: list[PageNumber] | None = None
        best_rank: tuple[int, int] | None = None
        for run in runs:
            last = run[-1]
            if last.system != candidate.system or last.page >= candidate.page or candidate.value <= last.value:
                continue
            drift = abs((candidate.value - last.value) - (candidate.page - last.page))
            if drift > offset_tolerance:
                continue
            rank = (drift, -len(run))
            if best_rank is None or rank < best_rank:
                best, best_rank = run, rank
        if best is None:
            runs.append([candidate])
        else:
            best.append(candidate)
    required = max(1, min(min_run_pages, page_count))
    accepted: list[tuple[int, int]] = []
    labels: dict[int, int | str] = {}
    for run in sorted(runs, key=len, reverse=True):
        if len(run) < required:
            break
        start, end = run[0].page, run[-1].page
        if any(start <= other_end and other_start <= end for other_start, other_end in accepted):
            continue
        accepted.append((start, end))
        for item in run:
            labels[item.page] = item.label
    return labels


__all__ = ["ARABIC", "KANJI", "ROMAN", "PageNumber", "read_page_number", "select_logical_pages"]
