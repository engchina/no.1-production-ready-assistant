"""文書の交差参照の抽出と解決(#1280。handoff §7.2)。

「第3章を参照」「3.2 節参照」「別紙1のとおり」「経費精算マニュアルの「権限」を参照」
「see Section 3.2」のような参照の表記と、手がかりの語の無い「第2章の共通の保守枠で保守します」
(番号の付いた章・条の直後の「の」+ 名詞。#1382)を、chunk の本文から決定論で抜き出す
(LLM は使わない)。

- 取込: 同じ文書の見出しの列(chunk の ``section_path``)に照らして参照先の節を決め、
  chunk の metadata(``reference_targets_json``)に残す(``annotate_cross_references``)。
  参照が無い chunk の metadata は変えない。参照が自分の節だけなら空の列を残す(解決済みの印)。
- 印の無い chunk(#1382 より前に取り込んだ chunk)は、回答のときに本文から抜き出し、その文書の
  見出しの列で解決する(``answer_engine``。再処理しなくても参照を辿れる)。
- 他の文書への参照は、文書名と節の表記だけを残す(未解決)。文書がどのナレッジベースに
  属するかは chunk に焼き込まない(KB は純スコープ。所属を変えても chunk に波及しない)ため、
  回答のときに検索範囲(KB)の中の文書から解決する(``answer_engine``)。
- 文書名で別の文書を指す文(「時間帯は定期保守計画で確かめます」「組織規程に従う」。#1400)は、
  文書名 + 参照の述語(で確かめる・を確認する・を見る・に従う・による・に記載 など)の形を、
  節を決めない文書全体への参照(``kind="document"``)として抜き出す。どの文書かは回答のときに
  検索範囲の文書のタイトル(見出しの列の先頭)・ファイル名と別名(年度・版・括弧書きを外した形)で
  照合して決め(``document_name_match``)、参照の先はその文書の質問に関連の高い chunk に限る。
  誤検出を避けるため、文書名は 4 文字以上・一般語(「システム」「台帳」など)でない・章や条の
  表記を含まないものに限り、照合は別名との完全一致(とファイル名が文書名を含むもの)だけにする。
- 回答: 検索で見つけた chunk の参照先の節の chunk を、上限付きで rerank の候補に足す。

参照先は「見出しの列のどこまでか」(``section_path`` の前方一致)で表す。chunk の id は
再索引で変わるので持たない。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Literal, cast

from rag_parser_core.extraction import is_derived_content
from rag_parser_core.language import current_pack
from rag_parser_core.language.text import parse_number

from app.rag.reference_grammar import NEXT_KEY, PREVIOUS_KEY, SAME_KEY, current_grammar

# chunk の metadata に参照先を載せる key(JSON の文字列。ChunkMetadata は scalar だけを持つ)。
REFERENCE_TARGETS_KEY = "reference_targets_json"
# 回答で参照先として足した chunk に付ける印(起点の chunk_id と参照の表記)。
REFERENCE_FROM_KEY = "reference_from_chunk_id"
REFERENCE_LABEL_KEY = "reference_label"
# 文書名だけの参照(#1400)の参照先 1 つの中で足した順(1 始まり。質問に関連の高い順)。
REFERENCE_RANK_KEY = "reference_rank"
# 1 つの chunk から残す参照の数の上限(目次や索引のような参照だらけの chunk を抑える)。
MAX_REFERENCES_PER_CHUNK = 6
# 見出しの列の区切り(chunking が「 > 」でつないで保存する)。
SECTION_PATH_SEPARATOR = " > "
# 参照の表記の直前を遡る文字数の上限(手がかりの語から前へ)。
_LOOKBACK_CHARS = 80

type ReferenceKind = Literal[
    "chapter", "section", "jsection", "article", "appendix", "title", "document"
]

# 番号(算用数字か漢数字)の書き方・漢数字の字と値は「参照の解析」の設定(#2179。
# ``ReferenceGrammar.number`` / ``kanji_digits`` / ``kanji_multipliers``)。

# 付録・別紙の種類(見出しと参照で同じ種類として扱う語をまとめる)。
# 別紙の語と種類は「参照の解析」の設定(#2175・#2177。``ReferenceGrammar.appendix_family``)。

# 参照を表す手がかりの語と、表記の間に置いてよい語は「参照の解析」の設定(#2171。
# ``ReferenceGrammar.cue`` / ``connector``)。
# 手がかりの語の無い参照(#1382): 番号の付いた章・節・条・別紙の直後の「の」+ 名詞
# (「第2章の共通の保守枠で保守します」「第4章の代理の規定」)。地の文の「第3章では」「全12章の構成」
# (「第」の無い章)・「第2条の2」(枝番)・「第2章の第3節」(続く表記に任せる)は拾わない。
# 「の」の後ろの名詞・文書名の前の助詞・文の区切り・英語の参照の書き方は「参照の解析」の設定
# (#2177。``ReferenceGrammar.possessive_tail`` / ``name_prefix_split`` / ``sentence_end`` /
# ``en_ref``)。

# 見出しの先頭の番号(見出し側の鍵)。章・節の書き方は設定(#2179。
# ``ReferenceGrammar.head_chapter`` など)。
_HEAD_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:\.(?!\d)|(?=\s|$|[^\d.]))")


def normalize_text(value: str) -> str:
    """全角の数字・記号を半角にそろえる(既定 NFKC。「参照の解析」の設定で NFC・しない。#2177)。"""
    form = current_pack().value("symbols", "normalization")
    return unicodedata.normalize(form, value or "") if form != "none" else value or ""


def title_key(value: str) -> str:
    """題名・文書名の比較用の形(NFKC・小文字・空白と記号を除く)。"""
    return current_grammar().title_noise.sub("", normalize_text(value).casefold())


def _number(value: str | None) -> str:
    """番号を比較用の形にする(漢数字は算用数字、英字は大文字、先頭の 0 は外す)。"""
    text = normalize_text(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    number = parse_number(text)
    if number is not None:
        return str(number)
    if "." in text and all(part.isdigit() for part in text.split(".")):
        return ".".join(str(int(part)) for part in text.split("."))
    return text.upper()


# 条の比較用の鍵の枝番のつなぎ(「7の2」)。保存した参照先(reference_targets_json)の鍵の形で、
# 言語の書き方ではない(言語資源の枝番のつなぎを変えても、鍵の形は変えない)。
ARTICLE_KEY_JOINER = "の"


def _article_key(number: str, branch: str | None) -> str:
    """条の比較用の鍵(枝番は ``ARTICLE_KEY_JOINER`` でつなぐ。「第七条の二」→「7の2」)。"""
    branches = re.findall(current_grammar().number, branch or "")
    return ARTICLE_KEY_JOINER.join([_number(number), *(_number(value) for value in branches)])


@dataclass(frozen=True)
class ReferenceSpec:
    """本文の中の参照の表記 1 件(まだ参照先を決めていない)。"""

    label: str
    kind: ReferenceKind
    # 比較用の鍵(chapter: "3"、section: "3.2"、appendix: "別紙:1"、title: 題名の比較形)。
    key: str
    # 他の文書への参照の文書名。None は同じ文書。
    document_title: str | None = None


@dataclass(frozen=True)
class ReferenceTarget:
    """参照の表記と、決まった参照先(同じ文書の節。決まらなければ None)。"""

    label: str
    kind: ReferenceKind
    key: str
    document_title: str | None = None
    # 参照先の節の見出しの列(「 > 」でつないだ形)。この列とその下の節が参照先。
    section_path: str | None = None

    @property
    def resolved(self) -> bool:
        return self.section_path is not None

    @property
    def spec(self) -> ReferenceSpec:
        return ReferenceSpec(self.label, self.kind, self.key, self.document_title)

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "kind": self.kind,
            "key": self.key,
            "document_title": self.document_title,
            "section_path": self.section_path,
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReferenceTarget | None:
        kind = value.get("kind")
        label = value.get("label")
        if kind not in _KINDS or not isinstance(label, str) or not label:
            return None
        document_title = value.get("document_title")
        section_path = value.get("section_path")
        return cls(
            label=label,
            kind=kind,
            key=str(value.get("key") or ""),
            document_title=document_title if isinstance(document_title, str) else None,
            section_path=section_path if isinstance(section_path, str) and section_path else None,
        )


_KINDS = {"chapter", "section", "jsection", "article", "appendix", "title", "document"}


# ---- 抽出 ----


def extract_references(text: str) -> list[ReferenceSpec]:
    """本文から参照の表記を抜き出す(重複は除き、最大 ``MAX_REFERENCES_PER_CHUNK`` 件)。

    日本語は「参照」「に定める」「のとおり」などの手がかりの語の直前に並んだ表記だけを拾う
    (「第3章では〜を説明する」のような地の文は拾わない)。英語は「see」「refer to」などの後ろの
    「Section 3.2」「Appendix A」「"題名"」を拾う。
    """
    normalized = normalize_text(text)
    found: list[ReferenceSpec] = []
    found.extend(_extract_japanese(normalized))
    found.extend(_extract_english(normalized))
    found.extend(_extract_document_names(normalized))
    # 「同条」は直前に出た条の参照にする(無ければ拾わない。#2153)。
    same = _same_article_spec(normalized)
    resolved: list[ReferenceSpec] = []
    for spec in found:
        if spec.kind == "article" and spec.key == SAME_KEY:
            if same is not None:
                resolved.append(replace(same, label=spec.label))
        else:
            resolved.append(spec)
    unique: dict[tuple[str, str, str], ReferenceSpec] = {}
    for spec in resolved:
        unique.setdefault((spec.kind, spec.key, spec.document_title or ""), spec)
    return list(unique.values())[:MAX_REFERENCES_PER_CHUNK]


def _same_article_spec(text: str) -> ReferenceSpec | None:
    """「同条」が指す条(同じ本文の中で、最初の「同条」の直前に出た条の参照)。無ければ None。"""
    previous: ReferenceSpec | None = None
    for match in current_grammar().label.finditer(text):
        if not match.group("article"):
            continue
        spec = _label_spec(match)
        if spec is None:
            continue
        if spec.key == SAME_KEY:
            return previous
        previous = replace(spec, document_title=_document_before(text[: match.start()]) or None)
    return None


def _extract_japanese(text: str) -> list[ReferenceSpec]:
    specs: list[ReferenceSpec] = []
    grammar = current_grammar()
    for cue in grammar.cue.finditer(text):
        specs.extend(_specs_before(text, cue.start()))
    for label in grammar.label.finditer(text):
        # 「第2章の共通の保守枠で」: 手がかりの語の代わりに、直後の「の」+ 名詞を手がかりにする。
        if _possessive_label(label) and grammar.possessive_tail.match(text, label.end()):
            specs.extend(_specs_before(text, label.end()))
    return specs


def _specs_before(text: str, cue_start: int) -> list[ReferenceSpec]:
    """手がかりの位置(``cue_start``)の直前に並んだ参照の表記(同じ文の中だけ)。"""
    start = max(0, cue_start - _LOOKBACK_CHARS)
    boundary = max(
        (match.end() for match in current_grammar().sentence_end.finditer(text, start, cue_start)),
        default=start,
    )
    segment = text[boundary:cue_start]
    labels = list(current_grammar().label.finditer(segment))
    accepted: list[re.Match[str]] = []
    # 表記の直後のつなぎに書いた、次の表記の文書名(「法第7条及び令第3条」の「令」。#2171)。
    gap_titles: list[str] = []
    cursor = len(segment)
    for match in reversed(labels):
        gap = segment[match.end() : cursor]
        title = "" if _connector_only(gap) else _enumerated_title(gap)
        if title is None:
            break
        accepted.insert(0, match)
        gap_titles.insert(0, title)
        cursor = match.start()
    if not accepted:
        return []
    specs: list[ReferenceSpec] = []
    document_title = _document_before(segment[:cursor])
    for index, match in enumerate(accepted):
        if index > 0 and gap_titles[index - 1]:
            # 列の途中で文書名が変わる。以降の表記はこの文書名(無ければ前の文書名を引き継ぐ)。
            document_title = gap_titles[index - 1]
        spec = _label_spec(match)
        if spec is None:
            continue
        following = accepted[index + 1] if index + 1 < len(accepted) else None
        if spec.kind == "title" and _document_like(match):
            if following is not None and re.fullmatch(
                current_grammar().name_label_tail.pattern.removesuffix("$"),
                segment[match.end() : following.start()],
            ):
                # 『経費精算マニュアル』の「権限」: 前の『』は後ろの表記の文書名。
                document_title = match.group("title").strip()
                continue
            specs.append(
                ReferenceSpec(match.group(0), "document", "", match.group("title").strip())
            )
            continue
        specs.append(replace(spec, document_title=document_title or None))
    return specs


def _without_items(gap: str) -> str:
    """条の表記に続けて書いた項・号(「第七条第一項及び第二項」の「第二項」)を外す(列のつなぎ)。

    項・号の書き方は「参照の解析」の設定(#2173)。
    """
    return current_grammar().item.sub("", gap)


def _connector_only(gap: str) -> bool:
    return current_grammar().connector.fullmatch(_without_items(gap)) is not None


def _enumerated_title(gap: str) -> str | None:
    """表記の間が「つなぎの語 + 文書名」(「及び令」)なら文書名(違えば None)。

    つなぎの語が 1 つ以上あり、残りの全体が 1 つの文書名のときだけ列を続ける(「及び」の無い
    「第7条令第3条」は続けない)。つなぎの語の終わりは長い方から試す(名前の頭をつなぎに取らない)。
    """
    stripped = _without_items(gap).rstrip()
    connector = current_grammar().connector
    for end in range(len(stripped) - 1, 0, -1):
        if not connector.fullmatch(stripped[:end]):
            continue
        name = stripped[end:].strip()
        if name and _document_before(name) == name:
            return name
    return None


def _possessive_label(match: re.Match[str]) -> bool:
    """「の」+ 名詞を手がかりにしてよい表記か(「第」の付いた章・節・条、番号付きの別紙、「3.2節」)。

    「第」の無い「12章」(「全12章の構成」)、番号の無い「別紙」、「節」「項」の付かない「3.2」
    (版の番号と区別できない)と、題名(「」)は、手がかりの語があるときだけ拾う。
    """
    label = match.group(0).strip()
    grammar = current_grammar()
    if match.group("chapter"):
        return bool(grammar.number_prefix.match(label))
    if match.group("article") or match.group("jsection"):
        return True
    if match.group("appendix"):
        return bool(match.group("app_no"))
    if match.group("numbered"):
        units = (*grammar.vocabulary.section_units, *grammar.vocabulary.paragraph_units)
        return bool(units) and label.endswith(units)
    return False


def _document_like(match: re.Match[str]) -> bool:
    """『』でくくった題名か、「マニュアル」などで終わる題名なら文書名として扱う。"""
    title = match.group("title") or ""
    return match.group(0)[:1] in current_grammar().document_title_brackets or bool(
        re.search(rf"{current_grammar().document_suffix}$", title)
    )


def _document_before(prefix: str) -> str:
    """表記の直前の文書名。同じ文書を指す語(本マニュアル)や文書名が無ければ空。"""
    grammar = current_grammar()
    if grammar.same_document_before.search(prefix):
        return ""
    match = grammar.document_before.search(prefix)
    if match is None:
        return _law_before(prefix)
    if match.group("quoted"):
        return match.group("quoted").strip()
    plain = grammar.name_prefix_split.split(match.group("plain"))[-1].strip()
    # 「マニュアルの第3章」のように名前が無いときは、どの文書か分からないので同じ文書とみなす。
    return "" if re.fullmatch(grammar.document_suffix, plain) else plain


def _law_before(prefix: str) -> str:
    """表記の直前の法令の名前(他の文書)。名前が無い・同じ文書を指す・一般語なら空。

    「法第5条」「令第3条」(施行令・施行規則が上位の法令を指す書き方)は、どの文書かを決めずに
    「法」「令」を文書名にする(同じ文書の条として解決しない。回答のときの照合にも当たらない)。
    """
    grammar = current_grammar()
    match = grammar.numbered_document_before.search(prefix)
    if match is None or grammar.same_numbered_document_before.search(prefix):
        return ""
    name = match.group("name")
    if grammar.starts_with_same_word(name):
        # 「同法第5条」は前に出た法令を指す。どれかは決めない(同じ文書の条として解決しない)。
        return name
    return "" if grammar.excluded_name_ending.search(name) else name


def _label_spec(match: re.Match[str]) -> ReferenceSpec | None:
    label = match.group(0).strip()
    if match.group("chapter"):
        chapter = _number(match.group("ch"))
        if match.group("ch_sec"):
            return ReferenceSpec(label, "section", f"{chapter}.{_number(match.group('ch_sec'))}")
        return ReferenceSpec(label, "chapter", chapter)
    if match.group("article"):
        if match.group("rel"):
            # 相対の参照(「前条」「次条」「同条」。#2153)。参照元の条から決める(``SectionIndex``)。
            return ReferenceSpec(
                label, "article", current_grammar().relative_key(match.group("rel"))
            )
        return ReferenceSpec(
            label, "article", _article_key(match.group("art"), match.group("branch"))
        )
    if match.group("jsection"):
        return ReferenceSpec(label, "jsection", _number(match.group("jsec")))
    if match.group("appendix"):
        family = current_grammar().appendix_family[match.group("app")]
        return ReferenceSpec(label, "appendix", f"{family}:{_number(match.group('app_no'))}")
    if match.group("numbered"):
        return ReferenceSpec(label, "section", _number(match.group("num")))
    if match.group("quoted"):
        key = title_key(match.group("title"))
        return ReferenceSpec(label, "title", key) if len(key) >= 2 else None
    return None


def _extract_english(text: str) -> list[ReferenceSpec]:
    specs: list[ReferenceSpec] = []
    grammar = current_grammar()
    for match in grammar.en_ref.finditer(text):
        label = match.group(0).strip()
        if match.group("title"):
            key = title_key(match.group("title"))
            if len(key) >= 2:
                specs.append(ReferenceSpec(label, "title", key))
            continue
        word = " ".join(match.group("kind").casefold().split())
        kind = cast(ReferenceKind, grammar.en_kinds[word])
        number = _number(match.group("no"))
        family = grammar.appendix_family.get(word, word)
        key = f"{family}:{number}" if kind == "appendix" else number
        document = (match.group("doc") or "").strip() or None
        specs.append(ReferenceSpec(label, kind, key, document))
    return specs


# ---- 文書名の参照(#1400) ----

# 文書名の直後に来る参照の述語(「定期保守計画で確かめます」「組織規程に従う」)。PDF の抽出で
# 文字の間に空白が入ることがある(「確かめま す」)ので、文字の間の空白を許す。
# 文書名だけの参照の述語は「参照の解析」の設定(#2175。
# ``ReferenceGrammar.document_name_predicate``)。
# 文書名に使う文字(漢字・カタカナ・英数字・「・」)。ひらがな・句読点・括弧で名前が切れる
# (「サンプル社の定期保守計画」は「定期保守計画」)。文字の間の空白 1 つは許す(PDF の抽出)。
# 文書名に使う文字は「参照の解析」の設定(#2179。``ReferenceGrammar.name_char``)。
# 参照の書き方の語（文書名の語尾・部の前置き・枝番・相対の参照など）は
# 「取込・検索・回答設定 > 文書分割」の設定にあり、``reference_grammar`` が正規表現にする（#2157）。
# 述語の直前の文書名と、述語が「で〜」のときの同じ文の前の「〈文書名〉で、」は、題名の括弧・読点を
# 「参照の解析」の設定から作る(#2177。``ReferenceGrammar.document_name_before`` /
# ``document_name_coordinated``。「担当部署はシステム台帳で、承認者は組織規程で確かめます」)。
# 文書名の最短の文字数(``title_key`` の形で数える)。「台帳」「規程」のような短い語は一般語と区別
# できないので拾わない。
MIN_DOCUMENT_NAME_CHARS = 4


@lru_cache(maxsize=16)
def _generic_name_keys(words: tuple[str, ...]) -> frozenset[str]:
    """文書名として扱わない一般語の ``title_key`` の形。"""
    return frozenset(title_key(word) for word in words)


def document_name_references(text: str) -> list[ReferenceSpec]:
    """本文の中の、文書名で別の文書を指す参照(``kind="document"``。#1400)。

    「時間帯は定期保守計画で確かめます」「担当部署はシステム台帳で、承認者は組織規程で確かめます」
    「『経費精算マニュアル』に従う」の文書名を拾う。どの文書かは決めない(回答のときに検索範囲の
    文書のタイトル・ファイル名で照合する)。``extract_references`` も同じ参照を返す。
    """
    return _extract_document_names(normalize_text(text))


def _extract_document_names(text: str) -> list[ReferenceSpec]:
    specs: list[ReferenceSpec] = []
    grammar = current_grammar()
    for predicate in grammar.document_name_predicate.finditer(text):
        start = max(0, predicate.start() - _LOOKBACK_CHARS)
        boundary = max(
            (
                match.end()
                for match in grammar.sentence_end.finditer(text, start, predicate.start())
            ),
            default=start,
        )
        segment = text[boundary : predicate.start()]
        match = grammar.document_name_before.search(segment)
        if match is None:
            continue
        names = [match]
        if predicate.group(0).startswith(grammar.vocabulary.coordination_particles):
            names[:0] = list(grammar.document_name_coordinated.finditer(segment[: match.start()]))
        for found in names:
            spec = _document_name_spec(found)
            if spec is not None:
                specs.append(spec)
    return specs


def _document_name_spec(match: re.Match[str]) -> ReferenceSpec | None:
    raw = match.group("name").strip()
    # 日本語の文字の間の空白(PDF の抽出で入る)は外す。英字の語の間の空白は残す。
    name = re.sub(r"(?<=[^\x00-\x7f])\s+|\s+(?=[^\x00-\x7f])", "", raw)
    key = title_key(name)
    grammar = current_grammar()
    if len(key) < MIN_DOCUMENT_NAME_CHARS or key in _generic_name_keys(
        grammar.vocabulary.generic_names
    ):
        return None
    if re.fullmatch(grammar.document_suffix, name) or grammar.demonstrative_name.match(name):
        return None
    # 「第2章」「3.2節」「別紙1」などの表記を含むものは、章・節の参照(別の規則)に任せる。
    if any(not label.group("quoted") for label in grammar.label.finditer(name)):
        return None
    # 「第4条の三第一項」の「三第一項」のような、条の表記の切れ端(項・号)は文書名にしない。
    if grammar.item.search(name):
        return None
    opening = match.group("open")
    closing = current_grammar().document_title_brackets.get(opening or "", "")
    label = f"{opening}{name}{closing}" if opening else name
    return ReferenceSpec(label, "document", "", name)


# 文書のタイトルの別名を作るときに外す、年度・版の表記(「2026年度」「第2版」「v1.2」「改訂版」)。
# 文書のタイトルの別名を作るときに外す年度・版の表記と会社名は、版の語・元号・会社名の語尾を
# 「参照の解析」の設定から作る(#2175。``ReferenceGrammar.trailing_edition`` など)。
# 括弧書き(「組織規程(システムの担当と承認者)」の「(…)」)。
# 先頭の会社名(「サンプル社 定期保守計画」の「サンプル社 」)。
_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def title_heading(heading: str) -> bool:
    """見出しの列の先頭の見出しが文書のタイトルとして使えるか(章・節・条などの番号が無い)。"""
    text = normalize_text(heading).strip()
    return bool(text) and current_grammar().head_numbering.match(text) is None


def document_name_aliases(value: str) -> set[str]:
    """文書のタイトル・ファイル名の、照合に使う形(``title_key``)の集合。

    そのままの形に加えて、括弧書き・年度や版の表記・先頭の会社名・拡張子を外した形を別名にする
    (「定期保守計画 2026年度」→「定期保守計画」、
    「組織規程(システムの担当と承認者)」→「組織規程」)。
    """
    text = normalize_text(value).strip()
    grammar = current_grammar()
    trailing, leading, company = (
        grammar.trailing_edition,
        grammar.leading_edition,
        grammar.company_prefix,
    )
    bases = {text, _FILE_EXTENSION.sub("", text)}
    variants: set[str] = set()
    for base in bases:
        for item in {base, grammar.parenthetical.sub(" ", base)}:
            for stripped in (
                item,
                trailing.sub("", leading.sub("", item)),
                company.sub("", item),
                trailing.sub("", leading.sub("", company.sub("", item))),
            ):
                variants.add(stripped.strip())
    return {key for key in (title_key(item) for item in variants) if len(key) >= 2}


def document_name_match(name: str, file_name: str, headings: Iterable[str] = ()) -> int | None:
    """参照の文書名が文書に当たるか(当たり方の順位。小さいほど確か。当たらなければ None)。

    - 0: 文書のタイトル(番号の無い先頭の見出し)・ファイル名、またはその別名と一致する。
    - 1: ファイル名(拡張子を除く)が文書名を含む(#1280 の他の文書への参照と同じ規則)。

    NFKC・大文字と小文字・空白と区切りの記号の違いは無視する。タイトルの部分一致は使わない
    (「保守計画」で「定期保守計画」を指さない)。
    """
    key = title_key(name)
    if len(key) < 2:
        return None
    aliases = document_name_aliases(file_name)
    for heading in headings:
        if title_heading(heading):
            aliases |= document_name_aliases(heading)
    if key in aliases:
        return 0
    stem = title_key(_FILE_EXTENSION.sub("", normalize_text(file_name)))
    if len(stem) >= 2 and key in stem:
        return 1
    return None


# ---- 解決 ----


def split_section_path(value: object) -> tuple[str, ...]:
    """保存した ``section_path``(「 > 」でつないだ文字列か list)を見出しの列にする。"""
    if isinstance(value, list):
        return tuple(str(part).strip() for part in value if str(part).strip())
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(SECTION_PATH_SEPARATOR) if part.strip())


def heading_keys(heading: str) -> set[tuple[str, str]]:
    """見出し 1 つが参照で呼ばれうる鍵((種類, 鍵) の集合)。"""
    text = normalize_text(heading).strip()
    keys: set[tuple[str, str]] = set()
    grammar = current_grammar()
    if match := grammar.head_chapter.match(text):
        keys.add(("chapter", _number(match.group(1))))
    if match := grammar.head_jsection.match(text):
        keys.add(("jsection", _number(match.group(1))))
    if match := grammar.head_article.match(text):
        keys.add(("article", _article_key(match.group(1), match.group(2))))
    if match := _HEAD_NUMBER.match(text):
        number = _number(match.group(1))
        keys.add(("section", number))
        if "." not in number:
            # 「3 経費の承認」は「第3章」とも呼ばれる(章の見出しが無い文書の代わり)。
            keys.add(("chapter_number", number))
    if match := grammar.head_appendix.match(text):
        word = match.group(1)
        family = grammar.appendix_family.get(word) or grammar.appendix_family[word.casefold()]
        number = _number(match.group(2))
        keys.add(("appendix", f"{family}:{number}"))
        keys.add(("appendix", f"{family}:"))
    if match := grammar.en_head.match(text):
        word = " ".join(match.group(1).casefold().split())
        kind = grammar.en_kinds.get(word, "section")
        keys.add((kind, _number(match.group(2))))
    title = grammar.head_numbering.sub("", text, count=1)
    for value in {title, text}:
        key = title_key(value)
        if len(key) >= 2:
            keys.add(("title", key))
    return keys


@dataclass(frozen=True)
class _Entry:
    path: tuple[str, ...]
    order: int
    keys: frozenset[tuple[str, str]]


class SectionIndex:
    """文書の見出しの列の一覧から、参照の表記に合う節を引く。"""

    def __init__(self, section_paths: Iterable[object]) -> None:
        entries: dict[tuple[str, ...], _Entry] = {}
        for value in section_paths:
            path = split_section_path(value)
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if prefix not in entries:
                    entries[prefix] = _Entry(
                        prefix, len(entries), frozenset(heading_keys(prefix[-1]))
                    )
        self._entries = list(entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, spec: ReferenceSpec, *, citing_path: Sequence[str] = ()) -> str | None:
        """参照先の節の見出しの列(「 > 」でつないだ形)。合う節が無い・曖昧なら None。

        候補が複数あるときは、参照元の節と共通の祖先が長いもの(同じ章の中の「第2節」など)、
        浅いもの、文書の前の方のものの順に選ぶ。
        """
        citing = tuple(citing_path)
        if spec.kind == "article" and spec.key in (PREVIOUS_KEY, NEXT_KEY):
            return self._relative_article(spec.key, citing)
        candidates = self._candidates(spec)
        if spec.kind == "article":
            candidates = _article_part_candidates(spec, candidates, citing)
        if not candidates:
            return None
        best = min(
            candidates,
            key=lambda entry: (-_common_prefix(entry.path, citing), len(entry.path), entry.order),
        )
        return SECTION_PATH_SEPARATOR.join(best.path)

    def _relative_article(self, key: str, citing: Sequence[str]) -> str | None:
        """「前条」「次条」: 参照元の条の前・次の条(本則・同じ附則の中の文書の順。#2153)。"""
        articles = [
            entry
            for entry in self._entries
            if any(kind == "article" for kind, _ in entry.keys)
            and _supplement_heading(entry.path) == _supplement_heading(citing)
        ]
        own = next(
            (
                index
                for index, entry in enumerate(articles)
                if tuple(citing[: len(entry.path)]) == entry.path
            ),
            None,
        )
        if own is None:
            return None
        other = own - 1 if key == PREVIOUS_KEY else own + 1
        if not 0 <= other < len(articles):
            return None
        return SECTION_PATH_SEPARATOR.join(articles[other].path)

    def _candidates(self, spec: ReferenceSpec) -> list[_Entry]:
        for wanted in _wanted_keys(spec):
            found = [entry for entry in self._entries if wanted in entry.keys]
            if found:
                return found
        if spec.kind == "section" and "." in spec.key:
            # 「3.2」は「第3章」の下の「第2節」とも書かれる。
            chapter, _, rest = spec.key.partition(".")
            parents = self._candidates(ReferenceSpec(spec.label, "chapter", chapter))
            if parents and "." not in rest:
                return [
                    entry
                    for entry in self._entries
                    if ("jsection", rest) in entry.keys
                    and any(entry.path[: len(parent.path)] == parent.path for parent in parents)
                ]
        if spec.kind == "title" and len(spec.key) >= 2:
            # 題名が見出しの一部にだけ含まれるとき(「権限」→「4.1 権限の設定」)。曖昧なら決めない。
            partial = [
                entry
                for entry in self._entries
                if any(kind == "title" and spec.key in key for kind, key in entry.keys)
            ]
            distinct = {entry.path[-1] for entry in partial}
            if 0 < len(distinct) <= 2:
                return partial
        return []


def _supplement_heading(path: Sequence[str]) -> str | None:
    """見出しの列の中の附則の見出し(「附則」「附則(制定時)」「附則(令和N年法律第N号)」)。"""
    part = current_grammar().part_heading
    return next((heading for heading in path if part.match(normalize_text(heading))), None)


def _amendment_supplement(heading: str) -> bool:
    """改正の附則(見出しに「〜第N号」が付く。改正法の附則で、条の番号は改正法のもの)。"""
    return bool(current_grammar().separate_part_pattern.search(normalize_text(heading)))


def _article_part_candidates(
    spec: ReferenceSpec, candidates: list[_Entry], citing: Sequence[str]
) -> list[_Entry]:
    """条の参照の候補を、本則・附則の範囲で絞る(法令・規程の書き方)。

    附則の条は「附則第N条」と書き、「附則」の付かない「第N条」は本則の条を指す(附則の中でも同じ)。
    改正の附則の中の「第N条」は改正法の本則の条なので、この文書の中では解決しない。
    「附則第N条」は、参照元が改正の附則ならその附則の条、それ以外は最初の版の附則の条。
    附則の見出しの無い文書では候補は変わらない。
    """
    citing_supplement = _supplement_heading(citing)
    citing_amendment = citing_supplement is not None and _amendment_supplement(citing_supplement)
    if current_grammar().part_prefix.match(normalize_text(spec.label)):
        if citing_amendment:
            return [entry for entry in candidates if citing_supplement in entry.path]
        return [
            entry
            for entry in candidates
            if (heading := _supplement_heading(entry.path)) is not None
            and not _amendment_supplement(heading)
        ]
    if citing_amendment:
        return []
    return [entry for entry in candidates if _supplement_heading(entry.path) is None]


def _wanted_keys(spec: ReferenceSpec) -> list[tuple[str, str]]:
    if spec.kind == "chapter":
        return [("chapter", spec.key), ("chapter_number", spec.key)]
    if spec.kind in {"section", "jsection", "article", "appendix", "title"}:
        return [(spec.kind, spec.key)]
    return []


def _common_prefix(left: Sequence[str], right: Sequence[str]) -> int:
    count = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        count += 1
    return count


def section_path_within(chunk_path: object, target: str) -> bool:
    """chunk の見出しの列が参照先の節(またはその下の節)か。"""
    path = split_section_path(chunk_path)
    target_path = split_section_path(target)
    return bool(target_path) and path[: len(target_path)] == target_path


def same_document_title(document_title: str, file_name: str) -> bool:
    """参照の文書名が文書名(拡張子を除く)に当たるか(どちらかがもう一方を含む)。"""
    title = title_key(document_title)
    stem = title_key(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", normalize_text(file_name)))
    if len(title) < 2 or len(stem) < 2:
        return False
    if title not in stem and stem not in title:
        return False
    # 「テスト法」と「テスト法施行令」は別の法令(長い方の残りが法令の種類の語で終わる。#2147)。
    shorter, longer = sorted((title, stem), key=len)
    return not current_grammar().document_kind_tail.search(longer.replace(shorter, "", 1))


# ---- 取込 ----


def annotate_cross_references(chunks: Sequence[Any], *, document_title: str = "") -> int:
    """chunk の metadata に参照先(``reference_targets_json``)を足し、足した chunk 数を返す。

    参照先は同じ文書の chunk 群の見出しの列から決める。自分の節・祖先の節への参照は残さない。
    参照の文書名が自分の文書名なら同じ文書への参照として解決する。他の文書への参照は未解決の
    まま残す(回答のときに検索範囲から解決する)。派生の chunk(要約・項目の値。#2641)には付けない。
    """
    index = SectionIndex(chunk.metadata.get("section_path") for chunk in chunks)
    citing_title = _citing_title(chunks, document_title)
    vocabulary = current_grammar().vocabulary
    annotated = 0
    for chunk in chunks:
        metadata = chunk.metadata
        if is_derived_content(metadata.get("content_kind"), metadata):
            continue
        specs = extract_references(chunk.text)
        if not specs:
            continue
        # 略称の参照(派生の文書の「法第N条」)は、参照元の文書の名前から本体の文書名にする(#2169)。
        specs = [
            replace(spec, document_title=expanded)
            if spec.document_title
            and (expanded := vocabulary.abbreviation_title(spec.document_title, citing_title))
            else spec
            for spec in specs
        ]
        targets = resolve_reference_specs(
            specs,
            index,
            citing_path=split_section_path(metadata.get("section_path")),
            document_title=document_title,
        )
        # 参照が自分の節・祖先の節だけでも空の列を残す(取込で解決済みの印。回答のときに
        # 本文から抜き出し直さない。#1382)。
        metadata[REFERENCE_TARGETS_KEY] = json.dumps(
            [target.to_dict() for target in targets], ensure_ascii=False
        )
        if targets:
            annotated += 1
    return annotated


def _citing_title(chunks: Sequence[Any], document_title: str) -> str:
    """参照元の文書の名前。

    先頭の見出しが番号の無いタイトルならそれ、無ければファイル名(拡張子を除く)。
    """
    for chunk in chunks:
        path = split_section_path(chunk.metadata.get("section_path"))
        if path:
            if title_heading(path[0]):
                return normalize_text(path[0]).strip()
            break
    return _FILE_EXTENSION.sub("", normalize_text(document_title)).strip()


def resolve_reference_specs(
    specs: Sequence[ReferenceSpec],
    index: SectionIndex,
    *,
    citing_path: Sequence[str] = (),
    document_title: str = "",
) -> list[ReferenceTarget]:
    """参照の表記を参照先にする(取込と、取込で解決していない chunk の回答のときに使う)。

    同じ文書の参照は ``index``(その文書の見出しの列)で節を決め、自分の節・祖先の節への参照は
    残さない。参照の文書名が ``document_title``(参照元の文書名)に当たれば同じ文書として扱う。
    他の文書への参照は文書名と節の表記だけを残す(回答のときに検索範囲から解決する)。
    """
    citing = tuple(citing_path)
    targets: list[ReferenceTarget] = []
    for spec in specs:
        if (
            spec.document_title
            and document_title
            and same_document_title(spec.document_title, document_title)
        ):
            if spec.kind == "document":
                continue
            spec = replace(spec, document_title=None)
        if spec.document_title:
            targets.append(ReferenceTarget(spec.label, spec.kind, spec.key, spec.document_title))
            continue
        if spec.kind == "document":
            continue
        path = index.resolve(spec, citing_path=citing)
        if path is not None and citing[: len(split_section_path(path))] == split_section_path(path):
            # 自分の節・祖先の節への参照は、根拠を足さないので残さない。
            continue
        targets.append(ReferenceTarget(spec.label, spec.kind, spec.key, None, path))
    return targets


def reference_targets(metadata: Mapping[str, object]) -> list[ReferenceTarget]:
    """chunk の metadata に保存した参照先を読む(壊れた値は飛ばす)。"""
    raw = metadata.get(REFERENCE_TARGETS_KEY)
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    targets = [ReferenceTarget.from_dict(item) for item in raw if isinstance(item, Mapping)]
    return [target for target in targets if target is not None]


__all__ = [
    "MAX_REFERENCES_PER_CHUNK",
    "MIN_DOCUMENT_NAME_CHARS",
    "REFERENCE_FROM_KEY",
    "REFERENCE_LABEL_KEY",
    "REFERENCE_RANK_KEY",
    "REFERENCE_TARGETS_KEY",
    "ReferenceSpec",
    "ReferenceTarget",
    "SectionIndex",
    "annotate_cross_references",
    "document_name_aliases",
    "document_name_match",
    "document_name_references",
    "extract_references",
    "heading_keys",
    "reference_targets",
    "resolve_reference_specs",
    "same_document_title",
    "section_path_within",
    "split_section_path",
    "title_heading",
    "title_key",
]
