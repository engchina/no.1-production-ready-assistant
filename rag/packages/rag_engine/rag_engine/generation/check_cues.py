"""回答の検査の手がかりの語（言語資源の answer_check_cues。#2181 段 5）。

確定の操作・限定・未説明・保証・推測・条件の句・但し書き・版の廃止などを見分ける語は言語資源のデータで、
ここは語を正規表現に組み立てるだけを持つ（言語資源の hash ごとに 1 回だけ）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_parser_core.language import FIELD_SPECS, LanguagePack, per_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import alternation, char_class, range_class

_SECTION = "answer_check_cues"


@dataclass(frozen=True)
class CheckCues:
    """回答の検査の語と、語から作った正規表現。"""

    words: dict[str, tuple[str, ...]]
    alt: dict[str, str]
    # 文字の種類（漢字・カタカナ、英数字を足したもの、ひらがなも足したもの）。
    kana_kanji: str
    word_chars: str
    any_word_chars: str
    sentence_split: re.Pattern[str]
    quote_sentence_split: re.Pattern[str]
    completion: re.Pattern[str]
    constraint: re.Pattern[str]
    heading_brackets: re.Pattern[str]
    route_separator: re.Pattern[str]
    field_label: re.Pattern[str]
    field_label_open: str
    field_label_close: str
    enumeration: re.Pattern[str]
    unexplained: re.Pattern[str]
    guarantee: re.Pattern[str]
    speculation: re.Pattern[str]
    button: re.Pattern[str]
    option_value: re.Pattern[str]
    ignored_quote_chars: frozenset[str]
    trailing_punctuation: str
    step_split: re.Pattern[str]
    # 行の先頭で ``match`` する手順の番号（①・⑴・(1)・（１）・1.・１．）。小数（1.5）には当たらない。
    step_line_start: re.Pattern[str]
    ellipsis_split: re.Pattern[str]
    object_kinds: tuple[tuple[str, re.Pattern[str]], ...]
    condition_sentence: re.Pattern[str]
    condition_clause: re.Pattern[str]
    condition_strip: re.Pattern[str]
    condition_trailing_particle: re.Pattern[str]
    condition_adverb: re.Pattern[str]
    proviso: re.Pattern[str]
    bare_label: re.Pattern[str]
    sentence_line: re.Pattern[str]
    navigation_residue: re.Pattern[str]
    name_value_pair: re.Pattern[str]
    version_label: str
    abolished_version: tuple[re.Pattern[str], ...]
    asks_about_versions: re.Pattern[str]
    question_version_token: re.Pattern[str]
    limit: re.Pattern[str]
    multi_condition_modes: tuple[re.Pattern[str], re.Pattern[str]]
    quoted_name: re.Pattern[str]
    bare_title_marks: re.Pattern[str]
    claim_word: re.Pattern[str]
    # 生成説明が列挙する画面のボタン一覧の欄（`ボタン: (F5) 実行 / 戻る`。content_labels の visible_buttons）。
    screen_button_field: re.Pattern[str]
    # 番号付きの機能見出し（「〜-(1)…」。線の字は evidence_cues）。
    function_label: re.Pattern[str]

    def has(self, key: str, text: str) -> bool:
        """語の一覧のどれかが text にあるか。"""
        return bool(re.search(self.alt[key], text))

    def chars(self, *keys: str) -> str:
        """語の一覧の字を、文字の集合の中に書ける形でつないだもの。"""
        return re.escape("".join(char for key in keys for char in self.words[key]))


def _body(items: tuple[str, ...]) -> str:
    """1 文字か「最初-最後」の範囲の並びを、文字の集合の中身（[ ] の中）にする。"""
    parts = []
    for item in items:
        if len(item) == 1:
            parts.append(re.escape(item))
        elif len(item) == 3 and item[1] == "-" and item[0] <= item[2]:
            parts.append(f"{re.escape(item[0])}-{re.escape(item[2])}")
    return "".join(parts)


def _build(pack: LanguagePack) -> CheckCues:
    words = {spec.key: pack.words(_SECTION, spec.key) for spec in FIELD_SPECS if spec.section == _SECTION}
    alt = {key: alternation(values) for key, values in words.items()}

    def chars(*keys: str) -> str:
        return re.escape("".join(char for key in keys for char in words[key]))

    kanji_items = pack.words("search_terms", "kanji_chars")
    katakana_items = pack.words("search_terms", "katakana_chars")
    hiragana_items = pack.words("search_terms", "hiragana_chars")
    kana_kanji = range_class(kanji_items + katakana_items)
    word_chars = range_class((*kanji_items, *katakana_items, "A-Z", "a-z", "0-9"))
    any_body = _body((*kanji_items, *hiragana_items, *katakana_items, "A-Z", "a-z", "0-9"))
    any_word_chars = f"[{any_body}]"
    kanji = _body(kanji_items)
    markers = marker_patterns(pack)
    quotes = pack.words("symbols", "quote_brackets")
    first_quote = quotes[0] if quotes else "「」"
    commas = re.escape("".join(pack.words("question_cues", "comma_chars")))
    quote_opens = re.escape("".join(pair[0] for pair in quotes))
    quote_closes = re.escape("".join(pair[1] for pair in quotes))
    sentence = re.escape("".join(pack.words("question_cues", "sentence_split_chars")))
    quote_ends = chars("quote_sentence_end_chars")
    pairs = words["heading_bracket_pairs"]
    heading_opens = re.escape("".join(pair[0] for pair in pairs))
    heading_closes = re.escape("".join(pair[1] for pair in pairs))
    colon = re.escape("".join(pack.words("evidence_cues", "label_colon_chars")))
    labels = pack.words("evidence_cues", "section_label_brackets")
    label_open = labels[0][0] if labels else "【"
    label_close = labels[0][1] if labels else "】"
    item_bullets = re.escape("".join(pack.words("evidence_cues", "item_bullet_chars")))
    notes = re.escape("".join(pack.words("evidence_cues", "note_mark_chars")))
    heading_bullets = re.escape("".join(pack.words("evidence_cues", "heading_bullet_chars")))
    circled = range_class(pack.words("evidence_cues", "step_circled_numbers"))
    number_open = markers.number_open
    number_close = markers.number_close
    numbers = pack.words("numbers", "kanji_digits")
    kanji_digits = "".join(item.partition("=")[0] for item in numbers) + "".join(
        item.partition("=")[0] for item in pack.words("numbers", "kanji_multipliers")
    )
    prefix = alternation(pack.words("numbers", "number_prefixes"))
    version_label = rf"{prefix}\s*[0-9{re.escape(kanji_digits)}]+\s*{alt['version_units']}"
    constraint = rf"{alt['constraint_words']}|{alt['condition_preceders']}{alt['condition_nouns']}{alt['condition_followers']}"
    unexplained = alt["unexplained_words"]
    guarantee = rf"{alt['guarantee_verbs']}{alt['guarantee_endings']}(?!{alt['guarantee_exceptions']})|{alt['match_claims']}"
    adverb_kanji = char_class(words["condition_adverb_kanji"])
    range_particles = "|".join(f"(?<={re.escape(word)})" for word in words["condition_range_particles"])
    trailing_particles = alt["condition_trailing_particles"]
    particle_head = _body((*kanji_items, "〆", "ヵ", "ヶ", *katakana_items, "A-Z", "a-z", "0-9", "Ａ-Ｚ", "ａ-ｚ", "０-９"))
    closers = re.escape("".join(pair[1] for pair in quotes) + "".join(pair[1] for pair in pack.words("markers", "number_brackets")))
    buttons_label = next(
        (label for label, field in pack.mapping("content_labels", "vision_fields").items() if field == "visible_buttons"),
        "visible_buttons",
    )
    dashes = re.escape("".join(pack.words("evidence_cues", "function_number_dashes")))
    return CheckCues(
        words=words,
        alt=alt,
        kana_kanji=kana_kanji,
        word_chars=word_chars,
        any_word_chars=any_word_chars,
        sentence_split=re.compile(rf"(?<=[{sentence}])|\n"),
        quote_sentence_split=re.compile(rf"(?<=[{quote_ends}])"),
        completion=re.compile(
            rf"({alt['completion_verbs']})[{chars('closing_quote_chars')}]?{alt['button_words']}?"
            rf"{alternation(pack.words('question_cues', 'object_particles'))}?"
            rf"(?:{alt['press_words']}[^{sentence}/\n]{{0,16}}?|{alt['completion_connectors']})"
            rf"[^{sentence}/\n]{{0,16}}?{alt['completion_words']}"
        ),
        constraint=re.compile(constraint),
        heading_brackets=re.compile(rf"^[{heading_opens}]\s*(?:{alt['heading_screen_words']}\s*[{colon}])?\s*|\s*[{heading_closes}]$"),
        route_separator=re.compile(markers.route_arrow),
        # 【X】の直後に欄・項目などが続くのは欄名の参照で、節見出しではない。
        field_label=re.compile(
            rf"{re.escape(label_open)}([^{re.escape(label_close)}]{{1,40}}){re.escape(label_close)}"
            rf"(?={alt['field_label_followers']})"
        ),
        field_label_open=label_open,
        field_label_close=label_close,
        enumeration=re.compile(markers.circled),
        unexplained=re.compile(unexplained),
        guarantee=re.compile(guarantee),
        speculation=re.compile(alt["speculation_words"]),
        button=re.compile(rf"({word_chars}{{1,12}}){alt['button_words']}"),
        option_value=re.compile(rf"^[{quote_opens}]?[0-9]{{1,3}}[{colon}]"),
        ignored_quote_chars=frozenset("".join(words["ignored_quote_chars"])),
        trailing_punctuation="".join(words["quote_sentence_end_chars"]),
        step_split=re.compile(
            rf"(?={circled})|(?={number_open}[0-9０-９]{{1,2}}{number_close})"
            rf"|(?<=[{sentence}\s])(?=[0-9０-９]{{1,2}}[{chars('step_split_closers')}])"
        ),
        step_line_start=re.compile(
            rf"[ \t\u3000]*(?:{circled}|{markers.circled}|{number_open}[0-9０-９]{{1,2}}{number_close}"
            rf"|[0-9０-９]{{1,2}}[{chars('step_split_closers')}](?![0-9０-９]))"
        ),
        ellipsis_split=re.compile(
            r"\s*(?:"
            + "|".join(f"{re.escape(char)}{{2,}}" for char in words["repeated_ellipsis_chars"])
            + (f"|[{chars('ellipsis_chars')}]+" if words["ellipsis_chars"] else "")
            + r")\s*"
        ),
        object_kinds=(
            ("report", re.compile(rf"{alt['report_suffixes']}$")),
            ("screen", re.compile(rf"{alt['screen_object_suffixes']}$")),
        ),
        condition_sentence=re.compile(rf"[{sentence}]|{alt['condition_sentence_endings']}$"),
        condition_clause=re.compile(
            rf"^.*?(?:{alt['condition_clause_nouns']}|{alt['condition_time_nouns']}"
            rf"(?=[{chars('condition_time_followers')}]|$))"
        ),
        condition_strip=re.compile(
            rf"(?:{re.escape(pack.text('answer_labels', 'condition_link'))})?"
            rf"(?:{alt['condition_clause_nouns']}|{alt['condition_time_nouns']}){alt['condition_strip_particles']}?$"
        ),
        # 名詞の後に残った格助詞・係助詞（「検証用アカウントには」）。直前が漢字・カタカナ・英数字・閉じ括弧のとき
        # だけ落とす。範囲を表す「までは」「からは」は「は」だけを落とす (#1501)。
        condition_trailing_particle=re.compile(
            rf"(?:(?<=[{particle_head}{closers}]){trailing_particles}"
            + (rf"|(?:{range_particles}){alt['condition_topic_particles']}" if range_particles else "")
            + ")$"
        ),
        condition_adverb=re.compile(rf"(?:^|[^{kanji}]){adverb_kanji}{alt['condition_adverb_particles']}$"),
        proviso=re.compile(rf"\s*((?:{alt['proviso_words']})[^{sentence}\n]{{4,200}}[{sentence}]?)"),
        bare_label=re.compile(rf"[{kanji}A-Za-z0-9{chars('bare_label_chars')}]{{1,12}}"),
        sentence_line=re.compile(rf"{alt['sentence_line_endings']}[{chars('sentence_line_closers')}]?$"),
        navigation_residue=re.compile(rf"^{alt['navigation_residue_words']}*$"),
        name_value_pair=re.compile(rf"[^\s/={quote_opens}{quote_closes}]+\s*=\s*[^\s/{quote_closes}]+"),
        version_label=version_label,
        abolished_version=(
            re.compile(rf"({version_label}){alt['abolish_particles']}[^{sentence}{commas}\n]{{0,24}}?{alt['abolish_words']}"),
            re.compile(rf"{alt['abolished_forms']}\s*({version_label})"),
        ),
        asks_about_versions=re.compile(alt["version_question_cues"]),
        question_version_token=re.compile(rf"{version_label}|[0-9]{{4}}"),
        limit=re.compile(rf"{unexplained}|{constraint}"),
        multi_condition_modes=(
            re.compile(rf"(?m)^([^\n{sentence}{colon}{heading_bullets}{item_bullets}{notes}]{{2,32}})\n(?=[{item_bullets}{notes}])"),
            re.compile(rf"([{any_body}_]{{2,32}}){alt['mode_topic_particles']}"),
        ),
        quoted_name=re.compile(rf"{re.escape(first_quote[0])}([^{re.escape(first_quote[1])}]+){re.escape(first_quote[1])}"),
        claim_word=re.compile(rf"{word_chars}{{2,}}"),
        screen_button_field=re.compile(rf"^{re.escape(buttons_label)}[:：]\s*(.+)$", re.M),
        function_label=re.compile(rf"[{dashes}]\([0-9]+\).+"),
        bare_title_marks=re.compile(
            rf"[{re.escape(''.join(w for w in words['bare_title_marks'] if len(w) == 1))}]"
            + "".join(f"|{re.escape(w)}" for w in words["bare_title_marks"] if len(w) > 1)
        ),
    )


check_cues = per_pack(_build)
