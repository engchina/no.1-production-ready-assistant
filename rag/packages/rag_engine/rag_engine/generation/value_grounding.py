"""表示例だけから数値制約・現在値を断定する回答を保守的に検出する。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import arabic_numbers, text_patterns

from rag_engine.generation.grounding_cues import grounding_cues
from rag_engine.parsing.content_labels import content_labels


def _rule() -> re.Pattern[str]:
    """数の入力規則の語（「上限」「最大」「8 桁」。言語資源の grounding_cues）。"""
    cues = grounding_cues()
    return re.compile(rf"{cues.alt['numeric_rule_words']}|[0-9]+\s*{cues.alt['digit_count_units']}")


def _date() -> re.Pattern[str]:
    """日付の表記（「令和6年度」「R6年」「西暦2024年4月1日」）。元号・略記・西暦の語・単位は言語資源(#2181)。"""
    text = text_patterns()
    era = f"(?:{text.era}|{text.calendar_words}|{text.era_letter})"
    return re.compile(rf"{era}?\s*[0-9]+\s*{text.date_units}(?:\s*[0-9]+\s*{text.date_units})*")


def _denial() -> re.Pattern[str]:
    """確認できないことを述べる語（言語資源の grounding_cues）。"""
    alt = grounding_cues().alt
    return re.compile(rf"{alt['denial_verbs']}{alt['denial_endings']}|{alt['denial_words']}")


def _numbers(text: str, *, kanji: bool = False) -> set[str]:
    """和暦年月の区切りを小数と誤認せず、質問の年月と同じ値として照合する。

    桁区切りのカンマ（「14,000」）は 1 つの数にする。``kanji`` なら漢数字（「一万四千」。字は言語資源）も
    算用数字の値として読む（#3070）。裏付けの側（根拠の本文・質問）だけで読み、回答の側では読まない
    （「一般」などの語を回答の主張の数値に数えない）。
    """
    patterns = text_patterns()
    text = arabic_numbers(text, kanji=kanji, spaced=True)
    text = re.sub(rf"{patterns.era_letter}([0-9]+)\.([0-9]{{1,2}})(?![0-9])", r"\1 \2", text)
    return set(re.findall(r"[0-9]+(?:\.[0-9]+)?", text))


# 段落の数値が引用した根拠のどこにも無いときの理由の先頭（#3088）。呼び出し元はこの理由に未確認の印を付ける。
ABSENT_VALUE_REASON = "根拠の原文に無い数値"
CURRENT_VALUE_REASON = "文書の表示例・Vision説明だけでは今回の実値を確認できない"


def _number_order(value: str) -> tuple[float, str]:
    try:
        return (float(value), value)
    except ValueError:
        return (float("inf"), value)


def unsupported_document_value_claim(text: str, cited: Sequence[dict[str, Any]], question: str) -> str:
    """明示数値の制約・現在値に、生成画像説明以外の必要な裏付けがあるか確認する。

    日本語の代表的な制約語と数値に限定する補助ガード。意味上の適用性は独立監査を
    継続する。本文に明示した制約と利用者の値は保持し、OCRやVisionの値だけで
    規則へ昇格させない。原文・入力を変更せず、未裏付けの場合だけ理由を返す。
    """
    cues = grounding_cues()
    alt = cues.alt
    rule, denial = _rule(), _denial()
    normalized = unicodedata.normalize("NFKC", text)
    labels = "|".join(f"(?<={re.escape(label)})" for label in cues.words["digit_range_labels"])
    if labels:
        normalized = re.sub(rf"(?:{labels})\s*\(0{marker_patterns().range_dash}9\)", "", normalized)
    normalized = re.sub(r"^\s*[0-9]+[.)]\s+", "", normalized)
    normalized = re.sub(r"(?:PDF\s*)?p\.?\s*[0-9]+(?:\s*[-–]\s*[0-9]+)?", "", normalized, flags=re.I)
    # 非数値の所属・選択状態も、図の例から今回の実値へ昇格させない。
    quotes = current_pack().words("symbols", "quote_brackets")
    opens = re.escape("".join(pair[0] for pair in quotes))
    closes = re.escape("".join(pair[1] for pair in quotes))
    actual = re.search(
        rf"{alt['current_subject_words']}[^{cues.sentence_chars}\n]{{0,30}}{alt['attribute_words']}{alt['attribute_particles']}"
        rf"[{opens}]([^{closes}]+)[{closes}]",
        normalized,
    )
    if actual and actual.group(1) not in question and not cues.has("hedge_words", normalized):
        value = actual.group(1)
        if not any(
            value in line and not cues.has("example_words", line)
            for span in cited
            if span.get("origin") != "image_extraction"
            for line in cues.sentences(content_labels().image_marker.split(span.get("text", ""), maxsplit=1)[0])
        ):
            return "文書の表示例・Vision説明だけでは今回の所属・選択状態を確認できない"
    numbers = _numbers(normalized)
    if not numbers:
        return ""
    # 文書の範囲に限定した未確認の説明は、数値規則の肯定主張ではない。
    rules = list(rule.finditer(normalized))
    denied_rule = re.compile(rf"{alt['rule_denial_prefixes']}{alt['source_qualifiers']}?{alt['denial_verbs']}{alt['denial_endings']}")
    if rules and all(denied_rule.match(normalized[m.end() :].strip()) for m in rules):
        return ""
    native = []
    for span in cited:
        if span.get("origin") == "image_extraction":
            continue
        source = unicodedata.normalize("NFKC", span.get("text", ""))
        if span.get("origin") != "document_text":
            # 旧metadataでも生成説明の境界が明示される場合は、その前の本文だけを使う。
            source = content_labels().image_marker.split(source, maxsplit=1)[0]
        native.extend(cues.sentences(source))
    if rule.search(normalized):
        supported = set()
        for sentence in native:
            # 本文の文は、規則の語（「上限」「最大」）が無くても規則を述べる（「14日以内とする」「7日まで認める」）。
            # 守るのは表示例・Vision の値なので、本文の文は例の文でなければ裏付けにする（#2209）。
            if not denial.search(sentence) and (rule.search(sentence) or not cues.has("example_words", sentence)):
                supported.update(_numbers(sentence, kanji=True))
        # 「平成27年度改正で上限額は100万円から120万円に」の 27 は日付で、規則値ではない。
        # 規則文での裏付けは、日付を除いた数字にだけ要求する。質問に明示された数値（「年３回から４回」の 4）は
        # 利用者が与えた条件で、図の例示値ではない (#629)。
        supported |= _numbers(unicodedata.normalize("NFKC", question), kanji=True)
        missing = _numbers(_date().sub("", normalized)) - supported
        if missing:
            # 引用した根拠のどこにも無い数値（表示例・画像の説明にも無い）は、資料の外から足した値（#3088）。
            anywhere = set().union(*(_numbers(unicodedata.normalize("NFKC", span.get("text", "")), kanji=True) for span in cited))
            if absent := missing - anywhere:
                return f"{ABSENT_VALUE_REASON}（{'、'.join(sorted(absent, key=_number_order))}）"
            return "文書の表示例・Vision説明だけでは数値の入力規則を確認できない"
    if re.search(rf"{alt['user_subject_words']}{alt['user_subject_particles']}", normalized):
        known = _numbers(unicodedata.normalize("NFKC", question), kanji=True)
        if not numbers <= known and not any(
            (rule.search(s) or cues.has("rule_condition_words", s)) and numbers <= _numbers(s, kanji=True) for s in native
        ):
            return CURRENT_VALUE_REASON
    return ""
