"""交差参照の解析の語（参照の書き方）を言語資源から読み、正規表現にする（#2157・#2181）。

解析の仕組み（``cross_references``）は通用で、書き方の語は言語資源（``rag_parser_core.language``。
画面は「取込・検索・回答設定 > 言語資源」）にある。
このモジュールは語から正規表現を作る仕組みだけを持ち、
語の値は持たない。語の一覧が空なら、その書き方は解析しない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from functools import lru_cache

from rag_parser_core.language import FIELD_SPECS, LanguagePack, current_pack

# 一覧が空のときの、何にも当たらない正規表現。
_NEVER = r"(?!)"
# 略称と参照先の文書名の型の区切りと、型の中の本体の名前の位置（#2169）。
ABBREVIATION_SEPARATOR = "="
BASE_NAME_PLACEHOLDER = "*"
# 英語の章・節の語の種類(処理が対応する値。言語資源の項目の定義の選択肢)。
_EN_SECTION_KINDS = next(spec.choices for spec in FIELD_SPECS if spec.key == "en_section_words")

# 相対の参照の鍵（言語資源の語を、この鍵にそろえて解決する。保存した参照先の鍵の形で、言語の
# 書き方ではない）。
PREVIOUS_KEY, NEXT_KEY, SAME_KEY = "前", "次", "同"


@dataclass(frozen=True)
class ReferenceVocabulary:
    """参照の書き方の語。各項目は言語資源の同じ名前の項目(節はどれでもよい)。"""

    # 他の文書の名前とみなす語尾（「経費精算マニュアル」「出張旅費規程」）。
    document_suffixes: tuple[str, ...]
    # 番号の表記の直前にあるときだけ文書名とみなす語尾（「〈他の法令〉第5条」の「法」）。
    # 短い語は一般語と区別
    # できないので、番号の表記が続くときに限る。
    numbered_document_suffixes: tuple[str, ...]
    # 上の語尾で終わっても文書名でない語（「処理方法第3条」の「方法」）。
    excluded_name_endings: tuple[str, ...]
    # 文書名として扱わない一般語（「システム」「台帳」）。
    generic_names: tuple[str, ...]
    # 文書名の前に付くと、名前ではなく指示の語になる語（「本手順書」「同規程」「上記の計画書」）。
    demonstrative_words: tuple[str, ...]
    # 同じ文書を指す語（「本マニュアルの第3章」「この法律第2条」）。
    self_words: tuple[str, ...]
    # 部の前置きの語（「附則第3条」）。前置きのある参照はその部の中で、
    # 無い参照は前置きの部の外で解決する。
    part_prefixes: tuple[str, ...]
    # 部の見出しにあると、その部の番号が別の体系になる印
    # （N は数。「附則(令和二年法律第八号)」の「第N号」）。
    separate_part_patterns: tuple[str, ...]
    # 条の表記に続く項・号の書き方（N は数。「第N項」「各号」。#2173）。条の表記の範囲・列の
    # つなぎ（「第一項及び第二項」）・文書名の候補から外す表記に使う。
    item_patterns: tuple[str, ...]
    # 枝番のつなぎの語（「第7条の2」の「の」）。
    branch_connectors: tuple[str, ...]
    # 相対の参照の語（「前条」「次条」「同条」）。同は、同じ本文の中で直前に出た条・文書を指す。
    previous_words: tuple[str, ...]
    next_words: tuple[str, ...]
    same_words: tuple[str, ...]
    # 参照を表す手がかりの語（「を参照」「による」。#2171）。
    # 手がかりの直前に並んだ表記を参照にする。
    cue_words: tuple[str, ...]
    # 表記どうし・表記と手がかりの語の間に置いてよい語（「及び」「又は」「、」。#2171）。
    connector_words: tuple[str, ...]
    # 文書名だけの参照の述語（「〈文書名〉で確かめます」。#2175）。
    document_name_predicates: tuple[str, ...]
    # 別紙の語と種類（「別添=別紙」。#2175）。
    appendix_words: tuple[str, ...]
    # 文書のタイトルの別名で外す版の語・元号・先頭の会社名の語尾（#2175）。
    edition_words: tuple[str, ...]
    era_names: tuple[str, ...]
    company_suffixes: tuple[str, ...]
    # タイトルの別名で外す年度・版・日付の型（YYYY・MM・DD・N・G。#2177）。
    edition_patterns: tuple[str, ...]
    # 記号（#2177）。
    title_ignored_chars: tuple[str, ...]
    sentence_end_chars: tuple[str, ...]
    clause_separator_chars: tuple[str, ...]
    bracket_pairs: tuple[str, ...]
    quote_brackets: tuple[str, ...]
    document_title_brackets: tuple[str, ...]
    # 助詞と、指示の語の後に来る文書の語（#2177）。
    name_prefix_particles: tuple[str, ...]
    possessive_particles: tuple[str, ...]
    name_label_connectors: tuple[str, ...]
    document_nouns: tuple[str, ...]
    # 英語の参照の書き方（#2177）。
    en_cue_words: tuple[str, ...]
    en_section_words: tuple[str, ...]
    en_document_suffixes: tuple[str, ...]
    en_quote_brackets: tuple[str, ...]
    en_document_connectors: tuple[str, ...]
    en_articles: tuple[str, ...]
    # 番号の書き方・文書名の文字・並列の助詞（#2179）。
    number_prefixes: tuple[str, ...]
    chapter_units: tuple[str, ...]
    section_units: tuple[str, ...]
    article_units: tuple[str, ...]
    paragraph_units: tuple[str, ...]
    item_units: tuple[str, ...]
    kanji_digits: tuple[str, ...]
    kanji_multipliers: tuple[str, ...]
    branch_stop_chars: tuple[str, ...]
    name_chars: tuple[str, ...]
    coordination_particles: tuple[str, ...]
    # 本体の文書から派生した文書の名前の語尾（「〈本体〉施行令」の「施行令」。#2169）。参照元の
    # 文書の名前がこの語で終わるとき、語尾を除いた名前を本体の名前にする。
    derived_document_suffixes: tuple[str, ...]
    # 略称と参照先の文書名の型（「法=*」。* は本体の名前。#2169）。派生の文書の中の「法第N条」を
    # 本体の文書の参照にする。
    abbreviation_targets: tuple[str, ...]

    def appendix_families(self) -> dict[str, str]:
        """別紙の語 → 種類（「語=種類」の形でない行は語そのものを種類にする）。"""
        families: dict[str, str] = {}
        for item in self.appendix_words:
            word, _sep, family = item.partition(ABBREVIATION_SEPARATOR)
            if word.strip():
                families[word.strip()] = family.strip() or word.strip()
        return families

    def kanji_values(self, items: tuple[str, ...]) -> dict[str, int]:
        """「字=数」の行から、1 文字の字 → 数（形の違う行は飛ばす）。"""
        values: dict[str, int] = {}
        for item in items:
            char, _sep, value = item.partition(ABBREVIATION_SEPARATOR)
            if len(char.strip()) == 1 and value.strip().isdigit():
                values[char.strip()] = int(value.strip())
        return values

    def en_section_kinds(self) -> dict[str, str]:
        """英語の章・節の語（小文字）→ 種類。種類の分からない行は飛ばす。"""
        kinds: dict[str, str] = {}
        for item in self.en_section_words:
            word, _sep, kind = item.partition(ABBREVIATION_SEPARATOR)
            if word.strip() and kind.strip() in _EN_SECTION_KINDS:
                kinds[word.strip().casefold()] = kind.strip()
        return kinds

    def abbreviation_title(self, name: str, citing_title: str) -> str | None:
        """参照の文書名が略称なら、参照元の文書の名前から決めた参照先の文書名。

        決まらなければ None（略称でない・参照元の名前が派生の語尾で終わらない）。
        """
        pairs = (item.partition(ABBREVIATION_SEPARATOR) for item in self.abbreviation_targets)
        targets = {
            abbr.strip(): template.strip()
            for abbr, sep, template in pairs
            if sep and abbr.strip() and template.strip()
        }
        template = targets.get(name.strip())
        if template is None:
            return None
        title = citing_title.strip()
        for suffix in sorted(self.derived_document_suffixes, key=len, reverse=True):
            if title.endswith(suffix) and len(title) > len(suffix):
                return template.replace(BASE_NAME_PLACEHOLDER, title[: -len(suffix)])
        return None

    @classmethod
    def from_pack(cls, pack: LanguagePack) -> ReferenceVocabulary:
        """言語資源の各節から、同じ名前の項目を集める(#2181)。"""
        names = {item.name for item in fields(cls)}
        values = {
            spec.key: pack.words(spec.section, spec.key)
            for spec in FIELD_SPECS
            if spec.key in names
        }
        return cls(**values)


def _spaced(word: str) -> str:
    """字の間に空白を許す（PDF の抽出の「附 則」）。"""
    return r"\s*".join(re.escape(char) for char in word)


def _alternation(words: tuple[str, ...], *, spaced: bool = False) -> str:
    """長い語を先にした選択。空なら何にも当たらない。"""
    if not words:
        return _NEVER
    ordered = sorted(words, key=len, reverse=True)
    return "(?:" + "|".join(_spaced(word) if spaced else re.escape(word) for word in ordered) + ")"


def _marker_pattern(marker: str, number: str) -> str:
    """「第N号」の N を数（``number``）にした正規表現（N の無い語はそのまま）。"""
    return rf"\s*{number}\s*".join(_spaced(part) for part in marker.split("N"))


def _char_class(items: tuple[str, ...]) -> str:
    """1 文字か「最初-最後」の範囲の並びを、文字の集合にする。無ければ何にも当たらない。"""
    parts = []
    for item in items:
        if len(item) == 1:
            parts.append(re.escape(item))
        elif len(item) == 3 and item[1] == "-" and item[0] <= item[2]:
            parts.append(f"{re.escape(item[0])}-{re.escape(item[2])}")
    return f"[{''.join(parts)}]" if parts else _NEVER


@dataclass(frozen=True)
class ReferenceGrammar:
    """語から作った正規表現（``cross_references`` が使う）。"""

    vocabulary: ReferenceVocabulary
    branch: str
    # 番号（算用数字か漢数字）の正規表現・漢数字の字と値・位取りの字と倍率・文書名の文字（#2179）。
    number: str
    kanji_digits: dict[str, int]
    kanji_multipliers: dict[str, int]
    name_char: str
    number_prefix: re.Pattern[str]
    head_chapter: re.Pattern[str]
    head_jsection: re.Pattern[str]
    paragraph_label: re.Pattern[str]
    item_label: re.Pattern[str]
    line_number: re.Pattern[str]
    label: re.Pattern[str]
    head_article: re.Pattern[str]
    head_numbering: re.Pattern[str]
    document_suffix: str
    document_before: re.Pattern[str]
    same_document_before: re.Pattern[str]
    numbered_document_before: re.Pattern[str]
    same_numbered_document_before: re.Pattern[str]
    excluded_name_ending: re.Pattern[str]
    document_kind_tail: re.Pattern[str]
    demonstrative_name: re.Pattern[str]
    part_prefix: re.Pattern[str]
    part_heading: re.Pattern[str]
    separate_part_pattern: re.Pattern[str]
    cue: re.Pattern[str]
    connector: re.Pattern[str]
    item: re.Pattern[str]
    appendix_family: dict[str, str]
    head_appendix: re.Pattern[str]
    document_name_predicate: re.Pattern[str]
    trailing_edition: re.Pattern[str]
    leading_edition: re.Pattern[str]
    company_prefix: re.Pattern[str]
    # 記号・助詞から作った正規表現（#2177）。
    title_noise: re.Pattern[str]
    sentence_end: re.Pattern[str]
    name_prefix_split: re.Pattern[str]
    possessive_tail: re.Pattern[str]
    parenthetical: re.Pattern[str]
    name_label_tail: re.Pattern[str]
    document_name_before: re.Pattern[str]
    document_name_coordinated: re.Pattern[str]
    # 必ず文書名とみなす題名の括弧（開き → 閉じ）。
    document_title_brackets: dict[str, str]
    # 英語の参照の書き方（#2177）。
    en_ref: re.Pattern[str]
    en_head: re.Pattern[str]
    en_kinds: dict[str, str]

    def relative_key(self, word: str) -> str:
        """相対の参照の語を鍵（前・次・同）にする。"""
        for key, words in (
            (PREVIOUS_KEY, self.vocabulary.previous_words),
            (NEXT_KEY, self.vocabulary.next_words),
            (SAME_KEY, self.vocabulary.same_words),
        ):
            if word in words:
                return key
        return ""

    def starts_with_same_word(self, name: str) -> bool:
        return any(name.startswith(word) for word in self.vocabulary.same_words)


# 年度・版・日付の型の記号（#2177）。
_EDITION_PLACEHOLDERS = {
    "YYYY": r"(?:19|20)\d{2}",
    "MM": r"\d{1,2}",
    "DD": r"\d{1,2}",
    "N": r"\d+(?:\.\d+)*",
}
EDITION_PLACEHOLDER_NAMES = (*_EDITION_PLACEHOLDERS, "G")


def _edition_template(template: str, era: str) -> str:
    """年度・版・日付の型を正規表現にする（記号と字の間に空白を許す）。"""
    tokens: list[str] = []
    rest = template
    while rest:
        name = next((n for n in EDITION_PLACEHOLDER_NAMES if rest.startswith(n)), None)
        if name is not None:
            tokens.append(era if name == "G" else _EDITION_PLACEHOLDERS[name])
            rest = rest[len(name) :]
        else:
            tokens.append(re.escape(rest[0]))
            rest = rest[1:]
    return r"\s*".join(tokens)


def _edition_pattern(v: ReferenceVocabulary) -> str:
    """タイトルの別名で外す年度・版の表記(「2026年度」「令和6年度」「第2版」「v1.2」「改訂版」)。

    型（``edition_patterns``）・版の語・元号はすべて設定（#2175・#2177）。
    """
    era = _alternation(v.era_names)
    templates = sorted(v.edition_patterns, key=len, reverse=True)
    parts = [_edition_template(template, era) for template in templates if template.strip()]
    return "(?:" + "|".join([*parts, _alternation(v.edition_words)]) + ")"


def _pair_class(pairs: tuple[str, ...], position: int) -> str:
    """括弧の組（2 文字）の開き（0）か閉じ（1）の字の集合。無ければ何にも当たらない。"""
    chars = "".join(pair[position] for pair in pairs if len(pair) == 2)
    return f"[{re.escape(chars)}]" if chars else _NEVER


def _pair_chars(pairs: tuple[str, ...]) -> str:
    return re.escape("".join(pair for pair in pairs if len(pair) == 2))


def _en_words(words: tuple[str, ...]) -> str:
    """英語の語の選択（語の間の空白は 1 つ以上の空白）。"""
    if not words:
        return _NEVER
    ordered = sorted(words, key=len, reverse=True)
    return (
        "(?:" + "|".join(r"\s+".join(re.escape(w) for w in word.split()) for word in ordered) + ")"
    )


@lru_cache(maxsize=16)
def build_grammar(vocabulary: ReferenceVocabulary) -> ReferenceGrammar:
    """語から正規表現を作る（同じ語の組は使い回す）。"""
    v = vocabulary
    kanji_digits = v.kanji_values(v.kanji_digits)
    kanji_multipliers = v.kanji_values(v.kanji_multipliers)
    kanji = re.escape("".join([*kanji_digits, *kanji_multipliers]))
    num = rf"(?:\d+|[{kanji}]+)" if kanji else r"\d+"
    kanji_class = f"[{kanji}]+" if kanji else _NEVER
    NAME_CHAR = _char_class(v.name_chars)  # noqa: N806 - 文書名の文字の集合(以前の定数の名前のまま)
    prefix = _alternation(v.number_prefixes)
    chapter_unit = _alternation(v.chapter_units)
    section_unit = _alternation(v.section_units)
    article_unit = _alternation(v.article_units)
    paragraph_unit = _alternation(v.paragraph_units)
    item_unit = _alternation(v.item_units)
    branch_stop = re.escape("".join(v.branch_stop_chars))
    coordination = _alternation(v.coordination_particles)
    families = v.appendix_families()
    # 英字の別紙の語（appendix など）は英語の参照の書き方で拾い、日本語の表記には使わない。
    appendix_words = _alternation(tuple(w for w in families if not re.fullmatch(r"[A-Za-z]+", w)))
    en_kinds = v.en_section_kinds()
    en_section = _en_words(tuple(en_kinds))
    en_head_words = _en_words(tuple(w for w, kind in en_kinds.items() if kind != "appendix"))
    quote_open, quote_close = _pair_class(v.quote_brackets, 0), _pair_class(v.quote_brackets, 1)
    quote_chars = _pair_chars(v.quote_brackets)
    title_open = _pair_class(v.document_title_brackets, 0)
    title_close = _pair_class(v.document_title_brackets, 1)
    brackets = tuple(pair for pair in v.bracket_pairs if len(pair) == 2)
    bracket_chars = _pair_chars(brackets)
    # 括弧書き（「(〜)」）。括弧の組ごとに、中に括弧の無い 1 段だけ。
    parenthetical = (
        "|".join(
            rf"{re.escape(pair[0])}[^{bracket_chars}]*{re.escape(pair[1])}" for pair in brackets
        )
        or _NEVER
    )
    separators = _alternation(v.clause_separator_chars)
    sentence_ends = _alternation(v.sentence_end_chars)
    # 文書名に入らない字（空白・読点など・文の区切り・括弧・題名の括弧）。
    name_stop = rf"(?:\s|{separators}|{sentence_ends}|[{bracket_chars}{quote_chars}])"
    name_label = rf"\s*(?:{_alternation(v.name_label_connectors)})?\s*$"
    nouns = _alternation(v.document_nouns)
    edition = _edition_pattern(v)
    connectors = _alternation(v.branch_connectors)
    branch = rf"(?:\s*{connectors}\s*{num}(?![\d{kanji}{branch_stop}]))"
    relative = _alternation((*v.previous_words, *v.next_words, *v.same_words))
    item = (
        "(?:"
        + ("|".join(_marker_pattern(pattern, num) for pattern in v.item_patterns) or _NEVER)
        + ")"
    )
    part = _alternation(v.part_prefixes, spaced=True)
    label = re.compile(
        rf"(?P<chapter>{prefix}?\s*(?P<ch>{num})\s*{chapter_unit}"
        rf"(?:\s*{prefix}\s*(?P<ch_sec>{num})\s*{section_unit})?)"
        rf"|(?P<article>(?:{part}\s*)?(?:{prefix}\s*(?P<art>{num})\s*{article_unit}(?P<branch>{branch}*)"
        rf"|(?P<rel>{relative})\s*{article_unit})"
        rf"(?:\s*{item})*)"
        rf"|(?P<jsection>{prefix}\s*(?P<jsec>{num})\s*{section_unit})"
        rf"|(?P<appendix>(?P<app>{appendix_words})\s*"
        rf"(?P<app_no>\d+|[A-Za-z](?![A-Za-z])|{kanji_class})?)"
        r"|(?P<numbered>(?<![\d.])(?P<num>\d+(?:\.\d+)+)(?!\d)\.?\s*"
        rf"(?:{section_unit}|{paragraph_unit})?)"
        rf"|(?P<quoted>{quote_open}(?P<title>[^{quote_chars}\n]{{1,60}}){quote_close})"
    )
    head_numbering = re.compile(
        rf"^(?:{prefix}\s*{num}\s*(?:{chapter_unit}|{section_unit}|{article_unit}){branch}*"
        rf"(?:\s*{prefix}\s*{num}\s*(?:{section_unit}|{paragraph_unit}))?"
        r"|\d+(?:\.\d+)*\.?"
        rf"|{appendix_words}\s*(?:\d+|[A-Za-z](?![A-Za-z])|{kanji_class})?"
        rf"|{en_section}\s*[\dA-Z.]*"
        + "".join(rf"|{re.escape(pair[0])}\s*\d+\s*{re.escape(pair[1])}" for pair in brackets)
        + rf")\s*(?:{separators}|\.)?\s*",
        re.IGNORECASE,
    )
    suffix = _alternation(v.document_suffixes)
    numbered = _alternation(v.numbered_document_suffixes)
    self_words = _alternation(v.self_words)
    return ReferenceGrammar(
        vocabulary=v,
        branch=branch,
        number=num,
        kanji_digits=kanji_digits,
        kanji_multipliers=kanji_multipliers,
        name_char=NAME_CHAR,
        number_prefix=re.compile(rf"^{prefix}"),
        head_chapter=re.compile(rf"^{prefix}\s*({num})\s*{chapter_unit}"),
        head_jsection=re.compile(rf"^{prefix}\s*({num})\s*{section_unit}"),
        paragraph_label=re.compile(rf"{prefix}\s*({num})\s*{paragraph_unit}"),
        item_label=re.compile(rf"{prefix}\s*({num})\s*{item_unit}"),
        line_number=re.compile(rf"\s*({num})\s"),
        label=label,
        head_article=re.compile(rf"^{prefix}\s*({num})\s*{article_unit}({branch}*)"),
        head_numbering=head_numbering,
        document_suffix=suffix,
        # 表記の直前の文書名（『〜』か、「〜マニュアル」などの語尾で終わる名前）。
        document_before=re.compile(
            rf"(?:{quote_open}(?P<quoted>[^{quote_chars}\n]{{2,60}}){quote_close}"
            rf"|(?P<plain>(?:(?!{name_stop}).){{1,40}}?{suffix}))" + name_label
        ),
        # 同じ文書を指す語（「本マニュアルの第3章」「同規程の第2条」）。
        same_document_before=re.compile(
            rf"{_alternation((*v.self_words, *v.same_words))}\s*(?:{suffix}|{nouns})" + name_label
        ),
        # 番号の表記の直前の文書名
        # （「〈他の法令〉附則第4条の3」「〈他の法令〉(昭和N年法律第N号)第5条」）。
        numbered_document_before=re.compile(
            rf"(?P<name>{NAME_CHAR}{{0,40}}?{numbered})\s*(?:{parenthetical})?" + name_label
        ),
        same_numbered_document_before=re.compile(rf"{self_words}\s*{numbered}" + name_label),
        excluded_name_ending=re.compile(rf"{_alternation(v.excluded_name_endings)}$"),
        # 文書の種類の語尾（「テスト法」と「テスト法施行令」を別の文書にする）。
        document_kind_tail=re.compile(
            rf"{_alternation((*v.numbered_document_suffixes, *v.document_suffixes))}$"
        ),
        demonstrative_name=re.compile(
            rf"^{_alternation(v.demonstrative_words)}\s*(?:{suffix}|{nouns})$"
        ),
        part_prefix=re.compile(rf"^\s*{part}"),
        part_heading=re.compile(rf"^\s*{part}"),
        separate_part_pattern=re.compile(
            "|".join(_marker_pattern(marker, num) for marker in v.separate_part_patterns) or _NEVER
        ),
        # 参照の手がかり（字の間の空白を許す）と、表記の間に置いてよい語の並び（#2171）。
        cue=re.compile(_alternation(v.cue_words, spaced=True)),
        connector=re.compile(rf"(?:\s|{_alternation(v.connector_words)})*"),
        item=re.compile(item),
        # 別紙の語の種類（英字の語は小文字でも引く）。
        appendix_family={**{k.casefold(): f for k, f in families.items()}, **families},
        head_appendix=re.compile(
            rf"^({_alternation(tuple(families))})\s*"
            rf"(\d+|[A-Za-z](?![A-Za-z])|{kanji_class})?",
            re.IGNORECASE,
        ),
        document_name_predicate=re.compile(_alternation(v.document_name_predicates, spaced=True)),
        trailing_edition=re.compile(rf"(?:[\s_\-]*{edition})+\s*$", re.IGNORECASE),
        leading_edition=re.compile(rf"^\s*(?:{edition}[\s_\-]*)+", re.IGNORECASE),
        company_prefix=re.compile(rf"^\S{{1,20}}?{_alternation(v.company_suffixes)}\s+"),
        title_noise=re.compile(rf"(?:\s|{_alternation(v.title_ignored_chars)})+"),
        sentence_end=re.compile(rf"\n|{sentence_ends}"),
        name_prefix_split=re.compile(_alternation(v.name_prefix_particles)),
        # 「第2章の共通の保守枠」の「の」+ 名詞
        # （文の区切り・読点・括弧・「第」・数字が続けば違う）。
        possessive_tail=re.compile(
            rf"\s*{_alternation(v.possessive_particles)}\s*"
            rf"(?![\s{bracket_chars}{quote_chars}]|{separators}|{sentence_ends}|\.|{prefix}|\d)"
        ),
        parenthetical=re.compile(parenthetical),
        name_label_tail=re.compile(rf"^{name_label}"),
        document_name_before=re.compile(
            rf"(?P<open>{title_open})?(?P<name>(?:{NAME_CHAR}(?:\s(?={NAME_CHAR}))?){{2,40}})"
            rf"(?(open){title_close})\s*$"
        ),
        document_name_coordinated=re.compile(
            rf"(?<!{NAME_CHAR})(?P<open>{title_open})?"
            rf"(?P<name>(?:{NAME_CHAR}(?:\s(?={NAME_CHAR}))?){{2,40}})"
            rf"(?(open){title_close})\s*{coordination}\s*(?:{separators})"
        ),
        document_title_brackets={
            pair[0]: pair[1] for pair in v.document_title_brackets if len(pair) == 2
        },
        en_ref=re.compile(
            rf"\b{_en_words(v.en_cue_words)}\s+(?:{_en_words(v.en_articles)}\s+)?"
            rf"(?:(?P<kind>{en_section})\s*(?P<no>\d+(?:\.\d+)*|[A-Z])\b"
            rf"(?:\s+{_en_words(v.en_document_connectors)}\s+(?:{_en_words(v.en_articles)}\s+)?"
            rf"(?P<doc>(?:[A-Z][\w&-]*\s+){{0,6}}{_en_words(v.en_document_suffixes)}))?"
            rf"|{_pair_class(v.en_quote_brackets, 0)}"
            rf"(?P<title>[^{_pair_chars(v.en_quote_brackets)}\n]{{2,80}})"
            rf"{_pair_class(v.en_quote_brackets, 1)})",
            re.IGNORECASE,
        ),
        en_head=re.compile(rf"^({en_head_words})\s*(\d+(?:\.\d+)*)", re.IGNORECASE),
        en_kinds=en_kinds,
    )


# 言語資源の hash → 組み立てた正規表現(解析の関数は 1 回の取込で何万回も呼ぶ)。
_by_hash: dict[str, ReferenceGrammar] = {}


def current_grammar() -> ReferenceGrammar:
    """今の言語資源(#2181)から作った正規表現(言語資源の hash ごとに 1 回だけ作る)。"""
    pack = current_pack()
    grammar = _by_hash.get(pack.hash)
    if grammar is None:
        grammar = build_grammar(ReferenceVocabulary.from_pack(pack))
        if len(_by_hash) >= 16:
            _by_hash.clear()
        _by_hash[pack.hash] = grammar
    return grammar
