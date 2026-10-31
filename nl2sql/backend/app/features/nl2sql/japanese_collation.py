"""日本語の辞書順の並べ替えのキー（#1777）。

実行履歴の「質問」の並べ替えは、Oracle では ``NLSSORT(..., 'NLS_SORT=JAPANESE_M')`` で行う。
memory の store（ローカルの開発・単体テスト）も同じ順になるよう、JAPANESE_M の主な規則を
Python で近似する。実 ADB（Oracle AI Database）で確かめた JAPANESE_M の順は次のとおり。

- 数字 → 英字 → ギリシャ文字・キリル文字など → かな → 長音符「ー」→ 漢字。
- ひらがなとカタカナは同じ音で並ぶ（「あいう」「アイウ」「ｱｲｳ」が隣り合う）。濁音・半濁音・小書きの
  かなは、清音と同じ位置で比べ、全体が同じときだけ区別する（「かき」→「カキ」→「がき」→「きく」）。
- 英字は大文字・小文字を同じ位置で比べ、全体が同じときは小文字が先（「abc」→「Abc」→「ABC」）。
- 全角の英数字・半角のカタカナは、全角・半角の違いを最後にだけ区別する。
- 漢字は JIS X 0208 の順（第 1 水準は音読みの順）。cp932 の符号の順と同じになる。

記号・句読点の扱い（JAPANESE_M は一部の記号を無視して比べる）までは再現しない。記号は数字より前に
符号の順で並べる。
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

# かなの小書き → 清音（主の比較では同じ音として扱う）。
_SMALL_KANA = str.maketrans("ぁぃぅぇぉっゃゅょゎゕゖ", "あいうえおつやゆよわかけ")
_VOICED_MARK = "゙"
_SEMI_VOICED_MARK = "゚"
_PROLONGED_SOUND_MARK = "ー"

# 主の比較の文字の種類の順（実 ADB の JAPANESE_M の順）。
_GROUP_SYMBOL = 0
_GROUP_DIGIT = 1
_GROUP_LATIN = 2
_GROUP_OTHER_LETTER = 3
_GROUP_CJK_SYMBOL = 4
_GROUP_KANA = 5
_GROUP_PROLONGED = 6
_GROUP_KANJI = 7
_GROUP_OTHER = 8

# 3 番目の比較（同じ音・同じ文字のときの区別）。
_TERTIARY_SMALL = 0
_TERTIARY_HIRAGANA = 1
_TERTIARY_KATAKANA = 2

type _Weight = tuple[int, int, bytes]
type CollationKey = tuple[tuple[_Weight, ...], tuple[int, ...], tuple[int, ...], str]


def _kanji_weight(char: str) -> bytes:
    try:
        encoded = char.encode("cp932")
    except UnicodeEncodeError:
        # JIS X 0208 に無い漢字は、JIS の漢字の後に符号の順で並べる。
        return b"\xff\xff" + ord(char).to_bytes(4, "big")
    return encoded if len(encoded) == 2 else b"\xff\xff" + ord(char).to_bytes(4, "big")


def _char_weights(char: str) -> tuple[_Weight, int, int]:
    """1 文字の (主の重み, 濁音の区別, 3 番目の区別) を返す。``char`` は NFKC の後の文字。"""
    code = ord(char)
    hiragana = ""
    tertiary = 0
    if 0x3041 <= code <= 0x3096:
        hiragana = char
        tertiary = _TERTIARY_HIRAGANA
    elif 0x30A1 <= code <= 0x30F6:
        hiragana = chr(code - 0x60)
        tertiary = _TERTIARY_KATAKANA
    if hiragana:
        decomposed = unicodedata.normalize("NFD", hiragana)
        base = decomposed[0]
        voicing = 1 if _VOICED_MARK in decomposed else 2 if _SEMI_VOICED_MARK in decomposed else 0
        large = base.translate(_SMALL_KANA)
        if large != base:
            tertiary = _TERTIARY_SMALL
        return (_GROUP_KANA, ord(large), b""), voicing, tertiary
    if char == _PROLONGED_SOUND_MARK:
        return (_GROUP_PROLONGED, 0, b""), 0, 0
    if char.isdigit():
        return (_GROUP_DIGIT, unicodedata.digit(char, code), b""), 0, 0
    category = unicodedata.category(char)
    if category[0] == "L":
        # アクセント付きの英字（é など）は元の英字と同じ位置で比べ、アクセントは 2 番目で区別する。
        decomposed = unicodedata.normalize("NFD", char)
        base = decomposed[0]
        if base.isascii() and base.isalpha():
            accent = 1 if len(decomposed) > 1 else 0
            return (_GROUP_LATIN, ord(base.casefold()), b""), accent, 0 if base.islower() else 1
    if category in {"Lo"} and unicodedata.name(char, "").startswith("CJK "):
        return (_GROUP_KANJI, 0, _kanji_weight(char)), 0, 0
    if 0x3000 <= code <= 0x303F:
        return (_GROUP_CJK_SYMBOL, code, b""), 0, 0
    if category[0] in {"Z", "P", "S", "C"}:
        return (_GROUP_SYMBOL, code, b""), 0, 0
    if category[0] == "L":
        folded = char.casefold()
        return (_GROUP_OTHER_LETTER, ord(folded[0]), b""), 0, 0 if char == folded else 1
    return (_GROUP_OTHER, code, b""), 0, 0


@lru_cache(maxsize=4096)
def japanese_collation_key(text: str) -> CollationKey:
    """``text`` の日本語の辞書順（JAPANESE_M の近似）の比較のキーを返す。

    主の重み（文字の種類と音）→ 濁音・半濁音 → かなの種類・大文字小文字 → 元の文字列の順で比べる。
    最後に元の文字列を置くので、違う文字列が同じキーになることはない。
    """

    normalized = unicodedata.normalize("NFKC", text)
    primary: list[_Weight] = []
    secondary: list[int] = []
    tertiary: list[int] = []
    for char in normalized:
        weight, voicing, kind = _char_weights(char)
        primary.append(weight)
        secondary.append(voicing)
        tertiary.append(kind)
    return tuple(primary), tuple(secondary), tuple(tertiary), text
