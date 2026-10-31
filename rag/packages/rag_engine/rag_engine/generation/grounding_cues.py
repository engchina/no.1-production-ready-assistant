"""回答の値と操作部品の裏付けの手がかりの語（言語資源の grounding_cues。#2181 段 5）。

数の規則・今回の値・ボタンとキー・別名・算式の単位・画像の操作を見分ける語は言語資源のデータで、
ここは語を正規表現の部品にするだけを持つ（言語資源の hash ごとに 1 回だけ）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_parser_core.language import FIELD_SPECS, LanguagePack, per_pack
from rag_parser_core.language.text import alternation, range_class

_SECTION = "grounding_cues"


@dataclass(frozen=True)
class GroundingCues:
    """裏付けの手がかりの語と、語から作った正規表現の部品。"""

    words: dict[str, tuple[str, ...]]
    alt: dict[str, str]
    # 漢字・カタカナ（言語資源の search_terms）。
    kana_kanji: str
    # 文を区切る字（言語資源の question_cues）と、最初の引用の括弧（symbols）。
    sentence_chars: str
    quote_open: str
    quote_close: str
    # 金額などの桁の単位（「万」→ 10000）。
    scales: dict[str, int]

    def has(self, key: str, text: str) -> bool:
        """語の一覧のどれかが text にあるか。"""
        return bool(re.search(self.alt[key], text))

    def sentences(self, text: str) -> list[str]:
        """文を区切る字と改行で分けた文。"""
        return re.split(rf"[{self.sentence_chars}\n]", text)


def _build(pack: LanguagePack) -> GroundingCues:
    words = {spec.key: pack.words(_SECTION, spec.key) for spec in FIELD_SPECS if spec.section == _SECTION}
    quotes = pack.words("symbols", "quote_brackets")
    first = quotes[0] if quotes else "「」"
    return GroundingCues(
        words=words,
        alt={key: alternation(values) for key, values in words.items()},
        kana_kanji=range_class(pack.words("search_terms", "kanji_chars") + pack.words("search_terms", "katakana_chars")),
        sentence_chars=re.escape("".join(pack.words("question_cues", "sentence_split_chars"))),
        quote_open=first[0],
        quote_close=first[1],
        scales={char: int(value) for char, value in pack.mapping(_SECTION, "amount_scale_units").items()},
    )


grounding_cues = per_pack(_build)
