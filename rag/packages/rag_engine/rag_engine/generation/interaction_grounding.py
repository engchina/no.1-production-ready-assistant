"""項目名・ボタン名・shortcutを別々の証拠として照合する。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.text import alternation

from rag_engine.generation.grounding_cues import grounding_cues
from rag_engine.parsing.content_labels import content_labels

_KEY = r"(?<![A-Za-z0-9])F(?:1[0-2]|[1-9])(?![0-9])"


def _label() -> str:
    """項目名・ボタン名（漢字・カタカナの 1〜24 字。文字の範囲は言語資源）。"""
    return rf"{grounding_cues().kana_kanji}{{1,24}}"


def _pairs() -> re.Pattern[str]:
    """キー番号とボタン名の対応（「F5（実行）」「実行(F5)」）。"""
    label = _label()
    return re.compile(
        rf"[「【(]?(?P<key>{_KEY})[)\]]?\s*[=:＝：]?\s*[「【（(]?\s*(?P<label>{label})[」】）)]?"
        rf"|[「【]?(?P<reverse>{label})[」】]?\s*[（(\[](?P<reverse_key>{_KEY})[）)\]]"
    )


def _native(spans: Sequence[dict[str, Any]]) -> str:
    return "\n".join(
        content_labels().image_marker.split(unicodedata.normalize("NFKC", s.get("text", "")), maxsplit=1)[0]
        for s in spans
        if s.get("origin") != "image_extraction"
    )


def _pair(match: re.Match) -> tuple[str, str]:
    label = match.group("label") or match.group("reverse")
    for word in current_pack().words("answer_check_cues", "button_words"):
        label = label.removesuffix(word)
    return (match.group("key") or match.group("reverse_key"), label)


def interaction_binding_error(
    answer: str, spans: Sequence[dict[str, Any]], *, screen_texts: Sequence[str] = (), body_texts: Sequence[str] = ()
) -> str:
    """未裏付けshortcutと、明示根拠のない引用符付き項目の同一視を検出する。

    一般的な意味同一性は独立監査に残す。検索別名や同じページ内の共存は証明にしない。
    screen_texts は同じ機能の画面キャプチャの説明（Vision）。画面に表示されるキー番号とボタン名の対応
    （「実行(F5)」）は本文に書かれないため、対応の照合にだけ使う。ボタン名の存在や別名の同一視には使わない (#637)。
    body_texts は同じ機能の本文（生成説明を除く）。手順が隣の span に分かれても、案内したボタン名の存在は
    機能の本文で確認できる (#658, #676)。キー番号との対応や別名の同一視には使わない。
    """
    cues = grounding_cues()
    alt = cues.alt
    keys = {"", *cues.words["key_words"]}
    pairs_pattern = _pairs()
    open_q, close_q = re.escape(cues.quote_open), re.escape(cues.quote_close)
    button = alternation(current_pack().words("answer_check_cues", "button_words"))
    value, native = unicodedata.normalize("NFKC", answer), _native(spans)
    screen = "\n".join(unicodedata.normalize("NFKC", text) for text in screen_texts)
    # 「実行または保存」の代替候補にも各ボタン名の根拠が必要。
    # 登録という結果の記載を、保存ボタンの存在証明にはしない。
    label_text = "\n".join(
        unicodedata.normalize("NFKC", line)
        for text in (*(s.get("text", "") for s in spans), *body_texts)
        for line in text.splitlines()
        if not re.match(r"\s*" + content_labels().field_line("search_keywords", "main_topic"), line)
    )
    for match in re.finditer(rf"(?:{open_q}[^{close_q}]{{1,24}}{close_q}(?:\s*{alt['alternative_connectors']}\s*)?)+{button}", value):
        labels = re.findall(rf"{open_q}([^{close_q}]+){close_q}", match.group())
        if any(label not in label_text for label in labels):
            return "案内したボタン名が引用本文にない"
    pairs = {_pair(m) for m in pairs_pattern.finditer(native + "\n" + screen) if _pair(m)[1] not in keys}
    for match in pairs_pattern.finditer(value):
        if _pair(match)[1] not in keys and _pair(match) not in pairs:
            return "キー番号とボタン名の対応が本文で未確認"
    # 無効ボタンはラベルを持たないまま画面に写る（`(F3)`）。対応が無くても根拠にそのキーがあれば、
    # 回答がボタン列を引き写しただけなので未確認にしない。根拠のどこにも無いキーは従来どおり拒む (#776)。
    keys_in_evidence = set(re.findall(_KEY, native + "\n" + screen))
    for key in re.findall(_KEY, value):
        if not any(p[0] == key for p in pairs) and key not in keys_in_evidence:
            return "キー番号の操作が本文で未確認"
    aliases = re.findall(rf"{open_q}([^{close_q}]{{1,30}}){close_q}\s*\(\s*{open_q}?([^{close_q})]{{1,30}}){close_q}?\s*\)", value)
    aliases += re.findall(rf"({_label()})\(({_label()})\){alt['alias_followers']}", value)
    for a, b in aliases:
        if (
            a != b
            and a in native
            and b in native
            and not any(
                a in line and b in line and cues.has("alias_words", line) and not cues.has("alias_negations", line)
                for line in cues.sentences(native)
            )
        ):
            return "別項目を同義のラベルとする原文対応が未確認"
    return ""
