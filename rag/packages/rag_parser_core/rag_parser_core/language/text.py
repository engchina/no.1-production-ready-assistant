"""言語資源から作る、数・単位・文末の共通の正規表現と数の読み方（#2181 段 1b）。

各モジュールが別々に持っていた漢数字の字の集合・章や条の単位・元号・文末の記号を、言語資源の
1 か所から作る。言語の書き方の値は持たない（値は言語資源のデータ）。組み立ては言語資源の hash ごとに
1 回だけ（解析の関数は 1 回の取込で何万回も呼ぶ）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import LanguagePack, current_pack

# 何にも当たらない正規表現（語の一覧が空のとき）。
NEVER = r"(?!)"
# 位取りの字のうち、これ以上の倍率は下の位の並び全体に掛ける（「三千二百万」= 3200 × 10000）。
_LARGE_MULTIPLIER = 10000


def alternation(words: tuple[str, ...]) -> str:
    """長い語を先にした選択（空なら何にも当たらない）。"""
    if not words:
        return NEVER
    return "(?:" + "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)) + ")"


def char_class(words: tuple[str, ...]) -> str:
    """1 文字の語の集合（無ければ何にも当たらない）。"""
    chars = "".join(word for word in words if len(word) == 1)
    return f"[{re.escape(chars)}]" if chars else NEVER


def range_class(items: tuple[str, ...]) -> str:
    """1 文字か「最初-最後」の範囲の並びを文字の集合にする（無ければ何にも当たらない）。"""
    parts = []
    for item in items:
        if len(item) == 1:
            parts.append(re.escape(item))
        elif len(item) == 3 and item[1] == "-" and item[0] <= item[2]:
            parts.append(f"{re.escape(item[0])}-{re.escape(item[2])}")
    return f"[{''.join(parts)}]" if parts else NEVER


def numbered(patterns: TextPatterns, units: str) -> str:
    """「第 N 〈単位〉」の正規表現（前置き・数・単位の間の空白を許す）。"""
    return rf"{patterns.prefix}\s*{patterns.number}\s*{units}"


@dataclass(frozen=True)
class TextPatterns:
    """言語資源から作った、数・単位・文末の正規表現の部品（文字列。各モジュールが組み合わせる）。"""

    # 数（算用数字か漢数字の並び）と、漢数字の字の集合。
    number: str
    kanji: str
    # 漢数字の字 → 値、位取りの字 → 倍率、元年の字 → 値。
    kanji_digits: dict[str, int]
    kanji_multipliers: dict[str, int]
    # ローマ数字の字の集合（英字の大小を区別しない正規表現で使う）と、字（小文字）→ 値。
    roman: str
    roman_digits: dict[str, int]
    first_year_words: tuple[str, ...]
    # 番号の前置き（第）と、編・章・節・款・目・条・項・号の単位。
    prefix: str
    part_units: str
    chapter_units: str
    section_units: str
    subsection_units: str
    division_units: str
    article_units: str
    paragraph_units: str
    item_units: str
    # 元号と、番号の代わりに使う仮名の集合（「ア」「(ア)」）。
    era: str
    # 元号の略記の字の集合（「R6」の「R」）・西暦を表す語・日付の単位・年度の単位。
    era_letter: str
    calendar_words: str
    date_units: str
    fiscal_year_units: str
    listing_kana: str
    # 文の区切りの記号・この記号で終わる行は見出しにしない記号・ページをまたぐ段落の終わりの記号・
    # 文末の後の閉じ括弧・文として終わる語。
    sentence_end: str
    heading_terminal: str
    paragraph_terminal: frozenset[str]
    sentence_closer: str
    sentence_like_ending: str
    # 文として終わる語の後に続いてよい記号（「〜します。」「〜です）」）。
    # 引用の閉じ（「」」）は含めない。
    sentence_ending_follower: str


def _values(pack: LanguagePack, key: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for word, value in pack.mapping("numbers", key).items():
        if len(word) == 1 and value.isdigit():
            values[word] = int(value)
    return values


_by_hash: dict[str, TextPatterns] = {}


def text_patterns(pack: LanguagePack | None = None) -> TextPatterns:
    """言語資源（既定は今の言語資源）から作った部品（言語資源の hash ごとに 1 回だけ作る）。"""
    pack = pack or current_pack()
    cached = _by_hash.get(pack.hash)
    if cached is not None:
        return cached
    digits = _values(pack, "kanji_digits")
    multipliers = _values(pack, "kanji_multipliers")
    roman_digits = {char.casefold(): value for char, value in _values(pack, "roman_digits").items()}
    first_year = pack.words("numbers", "first_year_words")
    kanji_chars = "".join(dict.fromkeys([*digits, *multipliers]))
    kanji = f"[{re.escape(kanji_chars)}]" if kanji_chars else NEVER
    units = {
        key: alternation(pack.words("numbers", key))
        for key in (
            "part_units",
            "chapter_units",
            "section_units",
            "subsection_units",
            "division_units",
            "article_units",
            "paragraph_units",
            "item_units",
        )
    }
    patterns = TextPatterns(
        number=rf"(?:\d+|{kanji}+)" if kanji_chars else r"\d+",
        kanji=kanji,
        kanji_digits=digits,
        kanji_multipliers=multipliers,
        roman=f"[{re.escape(''.join(roman_digits))}]" if roman_digits else NEVER,
        roman_digits=roman_digits,
        first_year_words=first_year,
        prefix=alternation(pack.words("numbers", "number_prefixes")),
        era=alternation(pack.words("numbers", "era_names")),
        listing_kana=range_class(pack.words("numbers", "listing_kana")),
        era_letter=char_class(tuple(pack.mapping("numbers", "era_letters"))),
        calendar_words=alternation(pack.words("numbers", "calendar_words")),
        date_units=alternation(pack.words("numbers", "date_units")),
        fiscal_year_units=alternation(pack.words("numbers", "fiscal_year_units")),
        sentence_end=char_class(pack.words("symbols", "sentence_end_chars")),
        heading_terminal=char_class(pack.words("symbols", "heading_terminal_chars")),
        paragraph_terminal=frozenset(pack.words("symbols", "paragraph_terminal_chars")),
        sentence_closer=char_class(pack.words("symbols", "sentence_closers")),
        sentence_like_ending=alternation(pack.words("symbols", "sentence_like_endings")),
        sentence_ending_follower=char_class(pack.words("symbols", "sentence_ending_followers")),
        **units,
    )
    if len(_by_hash) >= 16:
        _by_hash.clear()
    _by_hash[pack.hash] = patterns
    return patterns


def parse_number(text: str, pack: LanguagePack | None = None) -> int | None:
    """数の表記を数にする（算用数字・全角数字・位取りの漢数字・1 字ずつの漢数字・元年の字）。

    「二十八」「百四」「三千二百万」（位取り）、「一〇四」（1 字ずつ）、「元」（元年）。
    読めなければ None。
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)  # 全角の数字も int が読む
    patterns = text_patterns(pack)
    if cleaned in patterns.first_year_words:
        return 1
    digits, multipliers = (
        {**{str(n): n for n in range(10)}, **patterns.kanji_digits},
        patterns.kanji_multipliers,
    )
    if any(char not in digits and char not in multipliers for char in cleaned):
        return None
    if not any(char in multipliers for char in cleaned):
        number = 0
        for char in cleaned:
            number = number * 10 + digits[char]
        return number
    total = section = current = 0
    for char in cleaned:
        if char in digits:
            current = current * 10 + digits[char]
        elif multipliers[char] >= _LARGE_MULTIPLIER:
            total += ((section + current) or 1) * multipliers[char]
            section = current = 0
        else:
            section += (current or 1) * multipliers[char]
            current = 0
    return total + section + current


def _roman_tokens(digits: dict[str, int]) -> list[tuple[str, int]]:
    """ローマ数字を書く字と減算の組（「iv」= 4）を、値の大きい順に並べる。

    減算の組は、10 の冪の字をその 5 倍・10 倍の字の前に置くもの（i → v・x、x → l・c）。
    """
    chars = sorted(digits.items(), key=lambda item: item[1])
    tokens = [(char, value) for char, value in chars]
    for small, small_value in chars:
        if str(small_value).strip("0") != "1":
            continue
        for large, large_value in chars:
            if large_value in (small_value * 5, small_value * 10):
                tokens.append((small + large, large_value - small_value))
    return sorted(tokens, key=lambda item: item[1], reverse=True)


def format_roman(value: int, pack: LanguagePack | None = None) -> str:
    """数をローマ数字（小文字）にする。書けない数（0 以下・字が無い）は空文字。"""
    tokens = _roman_tokens(text_patterns(pack).roman_digits)
    if value <= 0 or not tokens:
        return ""
    parts: list[str] = []
    for token, number in tokens:
        count, value = divmod(value, number)
        parts.append(token * count)
    return "".join(parts) if value == 0 else ""


def parse_roman(text: str, pack: LanguagePack | None = None) -> int | None:
    """ローマ数字を数にする（英字の大小は区別しない）。

    標準の書き方（「iv」「xiv」。「iiii」「vx」は読まない）だけを読み、英語の語を数と取り違えにくくする。
    読めなければ None。
    """
    cleaned = text.strip().casefold()
    digits = text_patterns(pack).roman_digits
    if not cleaned or any(char not in digits for char in cleaned):
        return None
    total = 0
    for index, char in enumerate(cleaned):
        value = digits[char]
        following = digits[cleaned[index + 1]] if index + 1 < len(cleaned) else 0
        total += -value if value < following else value
    return total if total > 0 and format_roman(total, pack) == cleaned else None


def format_kanji_number(value: int, pack: LanguagePack | None = None) -> str:
    """数を位取りの漢数字にする（「二十八」「百四」「千二百」「三万五千」）。字は言語資源の値の最初の字。"""
    patterns = text_patterns(pack)
    digit_chars: dict[int, str] = {}
    for char, number in patterns.kanji_digits.items():
        digit_chars.setdefault(number, char)
    multiplier_chars: dict[int, str] = {}
    for char, number in patterns.kanji_multipliers.items():
        multiplier_chars.setdefault(number, char)
    multipliers = sorted(multiplier_chars.items(), reverse=True)

    def digits(number: int) -> str:
        if number in digit_chars:
            return digit_chars[number]
        return "".join(digit_chars.get(int(d), d) for d in str(number))

    def render(number: int) -> str:
        if number <= 0:
            return digits(0)
        parts: list[str] = []
        for unit, char in multipliers:
            if number < unit:
                continue
            quotient, number = divmod(number, unit)
            if unit >= _LARGE_MULTIPLIER:
                parts.append(f"{render(quotient)}{char}")
            else:
                # 倍率 1 では商が元の数と同じなので、再帰せず数字を書く。
                prefix = (
                    "" if quotient == 1 else digits(quotient) if unit == 1 else render(quotient)
                )
                parts.append(prefix + char)
        if number:
            parts.append(digits(number))
        return "".join(parts)

    return render(value)


# 桁区切りのカンマ（「14,000」）。3 桁ずつの組の前のカンマだけを除く。
_THOUSANDS_SEPARATOR = re.compile(r"(?<=[0-9]),(?=[0-9]{3}(?![0-9]))")


def arabic_numbers(
    text: str, pack: LanguagePack | None = None, *, kanji: bool = True, spaced: bool = False
) -> str:
    """数の書き方をそろえる（桁区切りのカンマを除く）。

    ``kanji`` なら漢数字の連なり（「一万四千」）を算用数字の値（「14000」）にする。
    漢数字の字と位取りは言語資源。読めない連なりはそのまま残す。
    ``spaced`` なら直した値の前後に空白を置き、隣の算用数字と 1 つの数に混ざらないようにする
    （数を取り出すとき）。
    """
    patterns = text_patterns(pack)
    text = _THOUSANDS_SEPARATOR.sub("", text)
    chars = "0-9" + re.escape("".join([*patterns.kanji_digits, *patterns.kanji_multipliers]))
    run = f"[{chars}]+"

    def value(match: re.Match[str]) -> str:
        original = match.group()
        compact = re.sub(r"[ \u3000\n]", "", original)
        if compact.isdigit():
            return original
        mixed = any(c.isascii() and c.isdigit() for c in compact) and any(
            c in patterns.kanji_multipliers for c in compact
        )
        if not kanji and not mixed:
            return original
        if compact != original:
            # 位取りが降順の連なりだけを結ぶ。tab・複数空白・独立した数は結ばない。
            units = [
                patterns.kanji_multipliers[c] for c in compact if c in patterns.kanji_multipliers
            ]
            digit_chars = "0-9" + re.escape("".join(patterns.kanji_digits))
            joins_digits = bool(
                re.search(
                    rf"[{digit_chars}][ \u3000\n][{digit_chars}]",
                    original,
                )
            )
            if (
                not units
                or any(a <= b for a, b in zip(units, units[1:], strict=False))
                or joins_digits
            ):
                return re.sub(run, value, original)
        number = parse_number(compact, pack)
        if number is None:
            return original
        return f" {number} " if spaced else str(number)

    return re.sub(rf"{run}(?:[ \u3000\n]{run})*", value, text)


__all__ = [
    "NEVER",
    "arabic_numbers",
    "format_kanji_number",
    "format_roman",
    "TextPatterns",
    "alternation",
    "char_class",
    "numbered",
    "parse_number",
    "parse_roman",
    "range_class",
    "text_patterns",
]
