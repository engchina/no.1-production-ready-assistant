"""言語資源から作る、段落・箇条書き・ページの印の正規表現の部品（#2181 段 2b）。

箇条書きの記号・丸数字・番号の閉じ・括弧・画面の経路の記号・ページの語などは言語資源のデータで、
このモジュールは部品を組み立てる仕組みだけを持つ。組み立ては言語資源の hash ごとに 1 回だけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import LanguagePack, current_pack
from .text import NEVER, alternation, range_class


def _chars(words: tuple[str, ...]) -> str:
    """1 文字の語の集合（無ければ何にも当たらない）。"""
    chars = "".join(word for word in words if len(word) == 1)
    return f"[{re.escape(chars)}]" if chars else NEVER


def _pair_side(pairs: tuple[str, ...], side: int) -> str:
    chars = "".join(pair[side] for pair in pairs if len(pair) == 2)
    return f"[{re.escape(chars)}]" if chars else NEVER


@dataclass(frozen=True)
class MarkerPatterns:
    """段落・箇条書き・ページの印の部品（正規表現の文字列と語）。"""

    bullet: str
    circled: str
    number_closer: str
    number_open: str
    number_close: str
    route_open: str
    route_close: str
    route_arrow: str
    series_open: str
    heading_form_openers: str
    page_words: str
    header_words: tuple[str, ...]
    footer_words: tuple[str, ...]
    range_dash: str
    all_pages_words: tuple[str, ...]
    instruction_ending: str
    element_kinds: dict[str, str]
    checked_values: tuple[str, ...]
    unchecked_values: tuple[str, ...]


_by_hash: dict[str, MarkerPatterns] = {}


def marker_patterns(pack: LanguagePack | None = None) -> MarkerPatterns:
    """言語資源（既定は今の言語資源）から作った部品（hash ごとに 1 回だけ作る）。"""
    pack = pack or current_pack()
    cached = _by_hash.get(pack.hash)
    if cached is not None:
        return cached

    def words(key: str) -> tuple[str, ...]:
        return pack.words("markers", key)

    patterns = MarkerPatterns(
        bullet=_chars(words("bullet_chars")),
        circled=range_class(words("circled_numbers")),
        number_closer=_chars(words("number_closers")),
        number_open=_pair_side(words("number_brackets"), 0),
        number_close=_pair_side(words("number_brackets"), 1),
        route_open=_pair_side(words("route_brackets"), 0),
        route_close=_pair_side(words("route_brackets"), 1),
        route_arrow=_chars(words("route_arrows")),
        series_open=_chars(words("series_openers")),
        heading_form_openers="".join(w for w in words("heading_form_openers") if len(w) == 1),
        page_words=alternation(words("page_words")),
        header_words=words("header_words"),
        footer_words=words("footer_words"),
        range_dash=_chars(words("range_dashes")),
        all_pages_words=words("all_pages_words"),
        instruction_ending=alternation(words("instruction_endings")),
        element_kinds=pack.mapping("markers", "element_kind_words"),
        checked_values=words("form_checked_values"),
        unchecked_values=words("form_unchecked_values"),
    )
    if len(_by_hash) >= 16:
        _by_hash.clear()
    _by_hash[pack.hash] = patterns
    return patterns


def word_alternation(words: tuple[str, ...]) -> str:
    """語の選択。英数字だけの語は語の境界で区切る（「co」が「color」に当たらない）。"""
    if not words:
        return NEVER
    parts = []
    for word in sorted(words, key=len, reverse=True):
        escaped = re.escape(word)
        parts.append(rf"\b{escaped}\b" if re.fullmatch(r"[A-Za-z0-9]+", word) else escaped)
    return "(?:" + "|".join(parts) + ")"


@dataclass(frozen=True)
class BoilerplatePatterns:
    """定型文と装飾の正規表現（英字の大小は区別しない）。"""

    company: re.Pattern[str]
    company_or_url: re.Pattern[str]
    boilerplate: re.Pattern[str]
    logo: re.Pattern[str]
    decorative_type: re.Pattern[str]
    operation: re.Pattern[str]
    business: re.Pattern[str]
    compare_ignored: re.Pattern[str]


_boilerplate_by_hash: dict[str, BoilerplatePatterns] = {}
# URL の形（書式の規則。言語の書き方ではない）。
_URL = r"https?://|www\."


def boilerplate_patterns(pack: LanguagePack | None = None) -> BoilerplatePatterns:
    """言語資源（既定は今の言語資源）から作った定型文と装飾の正規表現。

    言語資源の hash ごとに 1 回だけ作る。
    """
    pack = pack or current_pack()
    cached = _boilerplate_by_hash.get(pack.hash)
    if cached is not None:
        return cached

    def words(key: str) -> tuple[str, ...]:
        return pack.words("boilerplate", key)

    ignored = "".join(re.escape(w) for w in words("compare_ignored_chars") if len(w) == 1)
    patterns = BoilerplatePatterns(
        company=re.compile(f"({word_alternation(words('company_words'))})", re.I),
        company_or_url=re.compile(f"({word_alternation(words('company_words'))}|{_URL})", re.I),
        boilerplate=re.compile(f"({word_alternation(words('boilerplate_words'))}|{_URL})", re.I),
        logo=re.compile(word_alternation(words("logo_words")), re.I),
        decorative_type=re.compile(word_alternation(words("decorative_type_words")), re.I),
        operation=re.compile(word_alternation(words("operation_words")), re.I),
        business=re.compile(alternation(words("business_words"))),
        compare_ignored=re.compile(rf"[\s{ignored}]+"),
    )
    if len(_boilerplate_by_hash) >= 16:
        _boilerplate_by_hash.clear()
    _boilerplate_by_hash[pack.hash] = patterns
    return patterns


__all__ = [
    "BoilerplatePatterns",
    "MarkerPatterns",
    "boilerplate_patterns",
    "marker_patterns",
    "word_alternation",
]
