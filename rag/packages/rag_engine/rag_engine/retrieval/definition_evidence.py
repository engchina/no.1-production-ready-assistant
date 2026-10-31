"""短い画面項目の定義を、質問と原文の構造から照合する。業務用語は固定しない。

手がかりの語・括弧・区切りは言語資源（definition_cues。#2181 段 4）。このモジュールは組み立てだけを持つ。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from rag_parser_core.language import LanguagePack, per_pack
from rag_parser_core.language.text import alternation, char_class, numbered, range_class, text_patterns


@dataclass(frozen=True)
class _Patterns:
    question: re.Pattern[str]
    quoted: re.Pattern[str]
    reference_heading: re.Pattern[str]
    listed: re.Pattern[str]
    separator: re.Pattern[str]
    single: re.Pattern[str]
    suffixed: re.Pattern[str]
    label_chars: str
    line_head: str
    line_tail: str
    stop_head: str
    sentence_ends: tuple[str, ...]
    body: re.Pattern[str]
    topic_words: tuple[str, ...]
    ellipsis: str
    delimiters: str


def _build(pack: LanguagePack) -> _Patterns:
    def words(key: str) -> tuple[str, ...]:
        return pack.words("definition_cues", key)

    def alt(key: str) -> str:
        return alternation(words(key))

    terms = pack.words("search_terms", "kanji_chars") + pack.words("search_terms", "katakana_chars")
    label_chars = range_class((*terms, "A-Z", "a-z", "0-9", "_"))
    label = f"{label_chars}{{1,24}}"
    brackets = words("definition_label_brackets")
    opens = char_class(tuple(pair[0] for pair in brackets))
    closes = char_class(tuple(pair[1] for pair in brackets))
    separator = char_class(words("label_separators"))
    preceders = rf"(?:^|[\s{re.escape(''.join(words('label_preceders')))}])"
    fields = alt("field_words")
    particle = alt("label_particles")
    ellipsis = char_class(words("definition_ellipsis_chars"))
    delimiters = rf"(?:{ellipsis}+|{alt('definition_delimiters')})"
    number = text_patterns(pack)
    heading_units = "|".join((number.part_units, number.chapter_units, number.section_units, number.article_units, number.paragraph_units))
    return _Patterns(
        question=re.compile(alt("definition_question_cues")),
        quoted=re.compile(rf"{opens}({label}){closes}"),
        reference_heading=re.compile(rf"{numbered(number, f'(?:{heading_units})')}\s*$"),
        listed=re.compile(rf"({label}(?:\s*{separator}\s*{label})+)\s*{particle}?{fields}"),
        separator=re.compile(rf"\s*{separator}\s*"),
        # 引用符なしの単独項目も、項目・列・欄という構造が明示された時だけ採る。
        single=re.compile(rf"{preceders}({label}){particle}?{fields}(?=[\s{re.escape(''.join(words('label_followers')))}])"),
        suffixed=re.compile(rf"{preceders}({label}){alt('definition_suffixes')}"),
        label_chars=label_chars,
        line_head=rf"(?m)^[ \t]*(?:{char_class(words('definition_bullets'))}\s*)?{opens}?",
        line_tail=rf"{closes}?\s*{delimiters}\s*",
        stop_head=rf"\n\s*(?:{alt('stop_heading_words')}\s*[０-９0-9]+|{char_class(words('stop_bullet_chars'))}",
        sentence_ends=pack.words("question_cues", "sentence_split_chars"),
        body=re.compile(alt("definition_body_cues")),
        topic_words=words("definition_topic_words"),
        ellipsis=ellipsis,
        delimiters=rf"(?:{ellipsis}|{char_class(tuple(w for w in words('definition_delimiters') if len(w) == 1))})",
    )


_patterns = per_pack(_build)


def definition_labels(question: str) -> list[str]:
    """意味を尋ねる質問から明示項目だけを返す。普通の文章中の一文字は採らない。"""
    text = unicodedata.normalize("NFKC", question)
    patterns = _patterns()
    if not patterns.question.search(text):
        return []
    # 「第 N 章『題名』では何ですか」は参照先の指定であり、画面項目の定義要求ではない。
    labels = [match.group(1) for match in patterns.quoted.finditer(text) if not patterns.reference_heading.search(text[: match.start()])]
    for match in patterns.listed.finditer(text):
        labels.extend(patterns.separator.split(match.group(1)))
    if not labels:
        labels.extend(patterns.single.findall(text))
    if not labels:
        labels.extend(patterns.suffixed.findall(text))
    return list(dict.fromkeys(labels))


def label_mentioned(text: str, label: str) -> bool:
    """取消の中の「消」のような部分一致を除外し、単独の項目表記を照合する。"""
    text = unicodedata.normalize("NFKC", text)
    chars = _patterns().label_chars
    return bool(re.search(rf"(?<!{chars}){re.escape(label)}(?!{chars})", text))


def definition_ranges(text: str, labels: list[str]) -> list[dict[str, Any]]:
    """定義行と折返し条件を原文位置で返す。列一覧・検索語・途中切れは定義にしない。

    句点までを一体で扱い、次の項目・節を越えない。句点なしの場合は単一行だけを
    対象とする。文字位置は元のtextに対するもので、NFKC変換後の位置ではない。
    """
    patterns = _patterns()
    labels_alt = "|".join(re.escape(v) for v in labels)
    stop_pattern = re.compile(rf"{patterns.stop_head}|(?:{labels_alt}){patterns.delimiters})")
    results = []
    for label in labels:
        pattern = patterns.line_head + re.escape(label) + patterns.line_tail
        for match in re.finditer(pattern, text):
            tail = text[match.end() :]
            stop = stop_pattern.search(tail)
            tail = tail[: stop.start()] if stop else tail
            ends = [index for index in (tail.find(char) for char in patterns.sentence_ends) if index >= 0]
            end_sentence = min(ends) if ends else -1
            body = tail[: end_sentence + 1] if end_sentence >= 0 else tail.split("\n", 1)[0]
            if len(body.strip()) < 4 or len(body) > 1200 or not patterns.body.search(body):
                continue
            end = match.end() + len(body.rstrip())
            results.append(dict(label=label, start=match.start(), end=end, text=text[match.start() : end]))
    return sorted(results, key=lambda value: value["start"])


def definition_windows(text: str, labels: list[str]) -> list[tuple[int, int]]:
    """定義の直前にある対象項目の見出しも保持するための引用窓を返す。"""
    windows = []
    for item in definition_ranges(text, labels):
        start = item["start"]
        before = text[:start].rstrip("\n").splitlines()
        topic = any(word in before[-1] for word in _patterns().topic_words) if before else False
        if before and len(before[-1]) <= 100 and topic and any(label_mentioned(before[-1], v) for v in labels):
            start = text.rfind(before[-1], 0, start)
        windows.append((start, item["end"]))
    return windows
