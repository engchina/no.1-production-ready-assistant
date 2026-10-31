"""根拠の選び方の手がかりの語（言語資源の evidence_cues。#2181 段 4）。

根拠の関連度・引用の窓・手順の行・機能の見出し・図の要否・操作の接続を見分ける語は言語資源のデータで、
ここは語の一覧を正規表現の部品にするだけを持つ（言語資源の hash ごとに 1 回だけ作る）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_parser_core.language import FIELD_SPECS, LanguagePack, per_pack
from rag_parser_core.language.text import alternation, range_class

_SECTION = "evidence_cues"


@dataclass(frozen=True)
class EvidenceCues:
    """根拠の選び方の語と、語から作る正規表現の部品。"""

    words: dict[str, tuple[str, ...]]
    alt: dict[str, str]
    # 漢字・カタカナの範囲（言語資源の search_terms）。
    kana_kanji: str
    # 囲みの見出しの括弧（「【画面説明】」の【】）の開きと閉じ。
    label_opens: str
    label_closes: str

    def has(self, key: str, text: str) -> bool:
        """語の一覧のどれかが text にあるか。"""
        return bool(re.search(self.alt[key], text))

    def chars(self, *keys: str) -> str:
        """語の一覧の字を、文字の集合の中に書ける形でつないだもの。"""
        return re.escape("".join(char for key in keys for char in self.words[key]))


def _build(pack: LanguagePack) -> EvidenceCues:
    words = {spec.key: pack.words(_SECTION, spec.key) for spec in FIELD_SPECS if spec.section == _SECTION}
    brackets = words["section_label_brackets"]
    return EvidenceCues(
        words=words,
        alt={key: alternation(values) for key, values in words.items()},
        kana_kanji=range_class(pack.words("search_terms", "kanji_chars") + pack.words("search_terms", "katakana_chars")),
        label_opens=re.escape("".join(pair[0] for pair in brackets)),
        label_closes=re.escape("".join(pair[1] for pair in brackets)),
    )


evidence_cues = per_pack(_build)
