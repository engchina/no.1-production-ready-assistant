"""XML ツリー JSON の解析（#2024）。

XML の要素の木を ``{"tag", "attr", "children"}`` の JSON にした文書（文字は
``children`` の中の文字列）を、Markdown などに変換せず、要素の階層のまま読む。

対応するスキーマ: 根の要素が ``Law`` の木を ``*_full_text`` に持ち、文書の情報
（``*_info``）と版の情報（``revision_info``）を持つ JSON（``is_xml_tree_json``）。
ほかの JSON は対象にしない（今までどおり）。

- 単位: 番号つきの段落（``Paragraph``）を 1 要素にする。番号つきの単位
  （``Article``）を親、段落を子にするのは backend の分割（親子階層 small-to-big）。
  細目（``Item``・``Subitem1``…）は段落の本文の中で 1 行ずつにし、番号と本文を
  全角空白で分ける（細目の下の細目は行頭を全角空白で下げる）。単位の見出し・
  単位の番号・段落の番号は本文に入れず、要素の先頭の行のラベルにする。
- 階層の見出し: ``Part``・``Chapter``・``Section``・``Subsection``・``Division``
  を ``section_path`` にする（文書名 › 〔附属の部〕 › 階層の見出し › 単位）。
  見出しだけの要素は作らない。目次（``TOC``）は本文にしない。
- 本文の部と附属の部: 本文の部（``MainProvision``）と附属の部
  （``SupplProvision``）を区別する。附属の部は、最初の版のもの（``AmendLawNum``
  無し）と、後の版で足したもの（``AmendLawNum`` あり。``Extract="true"`` は
  抜粋）を区別し、ラベルに入れる。
- 引用ラベル: 要素の metadata に、本文の書き方（番号の漢数字）と算用数字の両方の
  引用ラベルを持つ（``xml_tree_citation_label`` / ``_arabic``）。検索の文には
  両方を入れる（``xml_tree_search_text``）。
- そのほか: ルビ（``Ruby``）は本文の文字だけ（読み ``Rt`` は捨てる）、表
  （``TableStruct``）は列見出しを保った Markdown の表、別表（``AppdxTable``
  など）は単独の要素、備考（``Remarks``）は表の後ろ、前文（``EnactStatement`` /
  ``Preamble``）は単独の要素、図（``Fig``）は本文に入れない。
- 文書のメタデータ（文書 ID・文書番号・文書名・略称・種類・分類・日付・版の ID）は
  ``parser_artifacts["xml_tree_document"]`` と ``fields`` に置く。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rag_parser_core.extraction import (
    DocumentElement,
    DocumentNavigationNode,
    ExtractionField,
    StructuredExtraction,
)
from rag_parser_core.language import current_pack
from rag_parser_core.language.text import (
    NEVER,
    alternation,
    format_kanji_number,
    parse_number,
    range_class,
    text_patterns,
)

XML_TREE_JSON_BACKEND = "xml_tree_json"
XML_TREE_JSON_SOURCE_FORMAT = "xml_tree_json"
XML_TREE_JSON_PARSER_VERSION = "xml_tree_json_v1"
# registry の adapter の版（``_adapter_version``）が読む。
__version__ = XML_TREE_JSON_PARSER_VERSION
XML_TREE_JSON_FORMAT_VERSION = 1
# parser_artifacts の key（文書のメタデータの dict）と、分割がこの解析の抽出を見分ける版の key。
XML_TREE_DOCUMENT_ARTIFACT_KEY = "xml_tree_document"
XML_TREE_JSON_FORMAT_VERSION_KEY = "xml_tree_json_format_version"
# 要素・chunk の metadata の key の接頭辞（rag_pipeline_core の分割が chunk の metadata へ写す）。
XML_TREE_METADATA_PREFIX = "xml_tree_"
# 引用ラベルで単位を読む道具（rag_read_section の citation）が照合する key。
XML_TREE_LOOKUP_KEYS_KEY = "xml_tree_lookup_keys"
# 判定のために全体を読む JSON の上限。
_DETECTION_MAX_BYTES = 128 * 1024 * 1024
# 1 つの要素の本文の上限（極端に長い別表で chunk を壊さない）。分割は backend が子の上限で行う。
_ELEMENT_TEXT_MAX_CHARS = 60_000

# スキーマの文書の情報・版の情報・木の key。
_TREE_KEY = "law_full_text"
_INFO_KEY = "law_info"
_REVISION_KEY = "revision_info"
_ROOT_TAG = "Law"

# 漢数字の字・位取り・元号・元年の字は言語資源（#2181。``rag_parser_core.language.text``）。

# 番号つきの要素のラベルの単位の字（スキーマの要素 → 「第N〈字〉」）。
_UNIT_KEYS = {"Article": "article_units", "Paragraph": "paragraph_units", "Item": "item_units"}
# 本文の部のラベルと、最初の版の附属の部に添える語。
# 本文の部のラベル・最初の版の附属の部に添える語・附属の部の既定のラベルは言語資源（#2181）。


def _first(section: str, key: str) -> str:
    """言語資源の語の一覧の最初の語（ラベルを作るときの書き方）。"""
    words = current_pack().words(section, key)
    return words[0] if words else ""


def _label(key: str) -> str:
    return current_pack().text("labels", key)


def _search_label(key: str) -> str:
    """検索の文の行の見出し（「引用」「本文」など。言語資源）。"""
    return current_pack().text("search_labels", key)


def _unit_suffix(tag: str) -> str:
    """番号つきの要素のラベルの単位の字（スキーマの要素 → 「第N〈字〉」。言語資源の最初の単位）。"""
    return _first("numbers", _UNIT_KEYS[tag])


def _bracketed(text: str) -> str:
    """ラベルの括弧書き（「附則（制定時）」）。括弧は言語資源。"""
    pair = _first("labels", "label_brackets")
    return f"{pair[0]}{text}{pair[1]}" if len(pair) == 2 else f"({text})"


def _numbered(number: str, suffix: str, branches: Sequence[str] = ()) -> str:
    """「第N〈単位〉のM」（前置き・枝番のつなぎは言語資源の最初の語）。"""
    prefix = _first("numbers", "number_prefixes")
    connector = _first("numbers", "branch_connectors")
    return f"{prefix}{number}{suffix}" + "".join(f"{connector}{part}" for part in branches)


_STRUCTURE_TAGS = ("Part", "Chapter", "Section", "Subsection", "Division")
_ITEM_TAGS = ("Item", *(f"Subitem{level}" for level in range(1, 11)))
_APPENDIX_TAGS = frozenset(
    {
        "AppdxTable",
        "AppdxStyle",
        "AppdxNote",
        "Appdx",
        "AppdxFig",
        "AppdxFormat",
        "SupplProvisionAppdxTable",
        "SupplProvisionAppdxStyle",
        "SupplProvisionAppdx",
    }
)
# 本文として読まない要素（目次・ルビの読み・図・範囲の表示）。
_SKIPPED_TAGS = frozenset({"TOC", "Rt", "Fig", "ArticleRange"})
_revision_refs: dict[str, re.Pattern[str]] = {}


def _revision_ref() -> re.Pattern[str]:
    """版の参照（「平成一六年六月一一日〈種類〉第一〇四号」の形。元号・年・種類・番号）。"""
    text = text_patterns()
    key = current_pack().hash
    pattern = _revision_refs.get(key)
    if pattern is None:
        year = f"(?:{text.kanji}|{alternation(text.first_year_words)})+"
        year_label, month, day = _label("year"), _label("month"), _label("day")
        prefix, item = text.prefix, alternation(current_pack().words("numbers", "item_units"))
        pattern = re.compile(
            rf"^(?P<era>{text.era})(?P<year>{year}){re.escape(year_label)}"
            rf"(?:{text.kanji}+{re.escape(month)}{text.kanji}+{re.escape(day)})?"
            rf"(?P<kind>.+?){prefix}(?P<num>{text.kanji}+){item}$"
        )
        _revision_refs.clear()
        _revision_refs[key] = pattern
    return pattern


# --- 数字 ---------------------------------------------------------------------------------


def kanji_number(value: int) -> str:
    """算用数字を位取りの漢数字（「二十八」「百四」「千二百」）にする（字は言語資源。#2181）。"""
    return format_kanji_number(value)


def parse_kanji_number(text: str) -> int | None:
    """漢数字（「二十八」「百四」の位取りの形と「一〇四」の 1 字ずつの形）を数にする。"""
    return parse_number(text)


def _num_parts(num: str) -> list[int]:
    """``Num`` の枝番（``44_3`` → [44, 3]）。数でない部分があれば空。"""
    parts: list[int] = []
    for part in num.split("_"):
        cleaned = part.strip()
        if not cleaned.isdigit():
            return []
        parts.append(int(cleaned))  # 全角の数字も int が読む
    return parts


def numbered_label(num: str, suffix: str, *, arabic: bool) -> str | None:
    """番号のラベル（「第四十四〈字〉の三」/「第44〈字〉の3」）。範囲（``1:3``）は「…から…まで」。"""
    if ":" in num:
        start, _, end = num.partition(":")
        first = numbered_label(start, suffix, arabic=arabic)
        last = numbered_label(end, suffix, arabic=arabic)
        if not (first and last):
            return None
        return f"{first}{_label('range_start')}{last}{_label('range_end')}"
    parts = _num_parts(num)
    if not parts:
        return None
    rendered = [str(part) if arabic else kanji_number(part) for part in parts]
    return _numbered(rendered[0], suffix, rendered[1:])


def revision_ref_labels(ref: str) -> tuple[str, str] | None:
    """版の参照（「平成一六年六月一一日〈種類〉第一〇四号」）を (漢数字, 算用数字) の短い形にする。

    例: (「平成十六年〈種類〉第百四号」, 「平成16年〈種類〉第104号」)。1 年は「元年」。
    読めなければ None。
    """
    match = _revision_ref().match(re.sub(r"\s+", "", ref))
    if match is None:
        return None
    year = parse_kanji_number(match["year"])
    number = parse_kanji_number(match["num"])
    if year is None or number is None:
        return None
    era = match["era"]
    kind = match["kind"]
    first_year = text_patterns().first_year_words
    year_kanji = first_year[0] if year == 1 and first_year else kanji_number(year)
    year_arabic = first_year[0] if year == 1 and first_year else str(year)
    year_label, item = _label("year"), _unit_suffix("Item")
    return (
        f"{era}{year_kanji}{year_label}{kind}{_numbered(kanji_number(number), item)}",
        f"{era}{year_arabic}{year_label}{kind}{_numbered(str(number), item)}",
    )


# --- 判定と読み込み -----------------------------------------------------------------------


def _is_tree_payload(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    tree = payload.get(_TREE_KEY)
    return (
        isinstance(tree, Mapping)
        and tree.get("tag") == _ROOT_TAG
        and isinstance(tree.get("children"), list)
        and isinstance(payload.get(_INFO_KEY), Mapping)
        and isinstance(payload.get(_REVISION_KEY), Mapping)
    )


def _load_json(data: bytes) -> object | None:
    if len(data) > _DETECTION_MAX_BYTES:
        return None
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None


def looks_like_xml_tree_json(data: bytes) -> bool:
    """対応するスキーマの XML ツリー JSON の可能性があるか（全体を読まない軽い判定）。"""
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    return (
        head.startswith(b"{")
        and f'"{_TREE_KEY}"'.encode() in data
        and f'"{_INFO_KEY}"'.encode() in data
        and f'"{_REVISION_KEY}"'.encode() in data
    )


def is_xml_tree_json(data: bytes) -> bool:
    """対応するスキーマの XML ツリー JSON（根の要素と文書・版の情報を持つ）か。"""
    return looks_like_xml_tree_json(data) and _is_tree_payload(_load_json(data))


@dataclass(frozen=True)
class XmlTreeDocumentMetadata:
    """文書のメタデータ（文書の属性・絞り込み・出典に使う）。"""

    document_id: str
    document_number: str
    title: str
    title_kana: str = ""
    abbrev: str = ""
    document_type: str = ""
    category: str = ""
    published_date: str = ""
    revision_date: str = ""
    revision_ref: str = ""
    revision_id: str = ""
    updated: str = ""
    revision_status: str = ""

    def to_artifact(self) -> dict[str, str]:
        return {
            "document_id": self.document_id,
            "document_number": self.document_number,
            "title": self.title,
            "title_kana": self.title_kana,
            "abbrev": self.abbrev,
            "document_type": self.document_type,
            "category": self.category,
            "published_date": self.published_date,
            "revision_date": self.revision_date,
            "revision_ref": self.revision_ref,
            "revision_id": self.revision_id,
            "updated": self.updated,
            "revision_status": self.revision_status,
        }


def _text_value(value: object) -> str:
    return str(value).strip() if isinstance(value, str | int | float) else ""


def _document_metadata(
    payload: Mapping[str, Any], root: Mapping[str, Any]
) -> XmlTreeDocumentMetadata:
    info = payload.get(_INFO_KEY) or {}
    revision = payload.get(_REVISION_KEY) or {}
    body = _child(root, f"{_ROOT_TAG}Body")
    title = _text_value(revision.get("law_title")) or _clean(
        _node_text(_child(body, f"{_ROOT_TAG}Title"))
    )
    return XmlTreeDocumentMetadata(
        document_id=_text_value(info.get("law_id")),
        document_number=_text_value(info.get("law_num"))
        or _clean(_node_text(_child(root, f"{_ROOT_TAG}Num"))),
        title=title,
        title_kana=_text_value(revision.get("law_title_kana")),
        abbrev=_text_value(revision.get("abbrev")),
        document_type=_text_value(info.get("law_type")) or _attr(root, f"{_ROOT_TAG}Type"),
        category=_text_value(revision.get("category")),
        published_date=_text_value(info.get("promulgation_date")),
        revision_date=_text_value(revision.get("amendment_enforcement_date")),
        revision_ref=_text_value(revision.get("amendment_law_num")),
        revision_id=_text_value(revision.get("law_revision_id")),
        updated=_text_value(revision.get("updated")),
        revision_status=_text_value(revision.get("current_revision_status")),
    )


# --- 木の読み取り -------------------------------------------------------------------------


def _children(node: object) -> list[object]:
    if not isinstance(node, Mapping):
        return []
    children = node.get("children")
    return list(children) if isinstance(children, list) else []


def _tag(node: object) -> str:
    return str(node.get("tag") or "") if isinstance(node, Mapping) else ""


def _attr(node: object, name: str) -> str:
    if not isinstance(node, Mapping):
        return ""
    attrs = node.get("attr")
    return str(attrs.get(name) or "").strip() if isinstance(attrs, Mapping) else ""


def _child(node: object, tag: str) -> Mapping[str, Any] | None:
    for child in _children(node):
        if _tag(child) == tag and isinstance(child, Mapping):
            return child
    return None


def _children_by_tag(node: object, tags: Sequence[str] | str) -> list[Mapping[str, Any]]:
    wanted = {tags} if isinstance(tags, str) else set(tags)
    return [
        child for child in _children(node) if isinstance(child, Mapping) and _tag(child) in wanted
    ]


def _node_text(node: object) -> str:
    """要素の文字（ルビの読み・図は捨てる）。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if _tag(node) in _SKIPPED_TAGS:
        return ""
    if _tag(node) == "Line":
        return "―" + "".join(_node_text(child) for child in _children(node)) + "―"
    return "".join(_node_text(child) for child in _children(node))


def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", text).strip()


def _sentence_block_text(node: object) -> str:
    """``ParagraphSentence`` など: 文はつなぎ、列（``Column``）は全角空白で分ける。"""
    columns = _children_by_tag(node, "Column")
    if columns:
        return "　".join(
            _clean(_node_text(column)) for column in columns if _node_text(column).strip()
        )
    return _clean(
        "".join(_node_text(child) for child in _children(node) if _tag(child) != "TableStruct")
    )


# --- 表 -----------------------------------------------------------------------------------


def _cell_text(node: object) -> str:
    text = " ".join(
        _clean(_block_text(child)) if isinstance(child, Mapping) else _clean(str(child))
        for child in _children(node)
    )
    # Markdown の表の区切り（|）と紛れない字に替える（書式の規則。言語の書き方ではない）。
    return _clean(text).replace("|", "｜")


def _block_text(node: object) -> str:
    """表のセルの中など、構造を問わず文字を読む（細目は 1 行ずつ）。"""
    tag = _tag(node)
    if tag in _ITEM_TAGS:
        return "\n".join(_item_lines(node, depth=0))
    if tag in {"Sentence", "Column"} or not isinstance(node, Mapping):
        return _node_text(node)
    return " ".join(_block_text(child) for child in _children(node))


def _table_lines(table_struct: object) -> list[str]:
    """``TableStruct`` を Markdown の表にする（1 行目を列見出し。結合のセルは値を繰り返す）。"""
    lines: list[str] = []
    title = _clean(_node_text(_child(table_struct, "TableStructTitle")))
    if title:
        lines.append(title)
    for table in _children_by_tag(table_struct, "Table"):
        grid: list[list[str]] = []
        pending: dict[int, tuple[str, int]] = {}
        for row in _children_by_tag(table, "TableRow"):
            cells: list[str] = []
            column = 0
            columns = _children_by_tag(row, ("TableColumn", "TableHeaderColumn"))
            index = 0
            while index < len(columns) or column in pending:
                if column in pending:
                    text, remaining = pending[column]
                    cells.append(text)
                    if remaining <= 1:
                        del pending[column]
                    else:
                        pending[column] = (text, remaining - 1)
                    column += 1
                    continue
                cell = columns[index]
                index += 1
                text = _cell_text(cell)
                colspan = max(1, _int_attr(cell, "colspan"))
                rowspan = max(1, _int_attr(cell, "rowspan"))
                for _ in range(colspan):
                    cells.append(text)
                    if rowspan > 1:
                        pending[column] = (text, rowspan - 1)
                    column += 1
            grid.append(cells)
        width = max((len(row) for row in grid), default=0)
        if not width:
            continue
        for row_index, row in enumerate(grid):
            padded = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(padded) + " |")
            if row_index == 0:
                lines.append("|" + "|".join(" --- " for _ in range(width)) + "|")
    for remarks in _children_by_tag(table_struct, "Remarks"):
        lines.extend(_remarks_lines(remarks))
    return lines


def _int_attr(node: object, name: str) -> int:
    value = _attr(node, name)
    return int(value) if value.isdigit() else 1


def _remarks_lines(remarks: object) -> list[str]:
    label = _clean(_node_text(_child(remarks, "RemarksLabel"))) or _label("remarks")
    lines = [label]
    for child in _children(remarks):
        tag = _tag(child)
        if tag == "RemarksLabel":
            continue
        if tag in _ITEM_TAGS:
            lines.extend(_item_lines(child, depth=0))
        else:
            text = _clean(_block_text(child))
            if text:
                lines.append(text)
    return lines


# --- 細目 ---------------------------------------------------------------------------------


def _item_lines(item: object, *, depth: int) -> list[str]:
    """細目を 1 行ずつにする（「一　本文」。細目の下の細目は行頭を全角空白で下げる）。"""
    tag = _tag(item)
    sentence_node = _child(item, f"{tag}Sentence")
    title = _clean(_node_text(_child(item, f"{tag}Title")))
    sentence = _sentence_block_text(sentence_node)
    indent = "　" * depth
    head = f"{indent}{title}　{sentence}" if title and sentence else f"{indent}{title or sentence}"
    lines = [head.rstrip()] if head.strip() else []
    for table in _children_by_tag(sentence_node, "TableStruct") + _children_by_tag(
        item, "TableStruct"
    ):
        lines.extend(_table_lines(table))
    for child in _children(item):
        if _tag(child) in _ITEM_TAGS:
            lines.extend(_item_lines(child, depth=depth + 1))
        elif _tag(child) == "List":
            lines.extend(f"{indent}　{_clean(_node_text(entry))}" for entry in _children(child))
    return lines


@dataclass(frozen=True)
class _ItemRef:
    label: str
    label_arabic: str


def _item_refs(paragraph: object) -> list[_ItemRef]:
    suffix = _unit_suffix("Item")
    refs: list[_ItemRef] = []
    for item in _children_by_tag(paragraph, "Item"):
        num = _attr(item, "Num")
        title = _clean(_node_text(_child(item, "ItemTitle")))
        label = numbered_label(num, suffix, arabic=False) or (
            _numbered(title, suffix) if title else ""
        )
        label_arabic = numbered_label(num, suffix, arabic=True) or label
        if label:
            refs.append(_ItemRef(label=label, label_arabic=label_arabic))
    return refs


# --- 単位 ---------------------------------------------------------------------------------


@dataclass
class _Part:
    """本文の部・附属の部の 1 つ（ラベルと区別）。"""

    kind: str  # main / supplement_original / supplement_revision
    label: str  # 本文の部は ""
    label_arabic: str
    revision_ref: str = ""
    extract: bool = False


@dataclass
class _Unit:
    """要素 1 つ分（段落・単位の無い附属の段落・別表・前文など）。"""

    unit_kind: str
    text_lines: list[str]
    section_path: list[str]
    part: _Part
    citation: str
    citation_arabic: str
    parent_label: str = ""
    parent_label_arabic: str = ""
    parent_caption: str = ""
    parent_num: str = ""
    paragraph_num: str = ""
    aliases: list[str] = field(default_factory=list)
    items: list[_ItemRef] = field(default_factory=list)
    has_table: bool = False
    deleted: bool = False


def _structure_title(node: object) -> str:
    return _clean(_node_text(_child(node, f"{_tag(node)}Title")))


def _paragraph_lines(paragraph: object) -> tuple[list[str], bool]:
    """段落の本文（文・細目・表）。細目は 1 行ずつ。表があれば真を返す。"""
    lines: list[str] = []
    sentence = _child(paragraph, "ParagraphSentence")
    lead = _sentence_block_text(sentence)
    if lead:
        lines.append(lead)
    has_table = False
    for table in _children_by_tag(sentence, "TableStruct"):
        lines.extend(_table_lines(table))
        has_table = True
    for child in _children(paragraph):
        tag = _tag(child)
        if tag in _ITEM_TAGS:
            lines.extend(_item_lines(child, depth=0))
        elif tag == "TableStruct":
            lines.extend(_table_lines(child))
            has_table = True
        elif tag in {"List", "FigStruct", "StyleStruct", "AmendProvision", "Class"}:
            text = _clean(_block_text(child))
            if text:
                lines.append(text)
    return lines, has_table


def _paragraph_label(
    parent_label: str,
    parent_label_arabic: str,
    paragraph: object,
    *,
    paragraph_count: int,
) -> tuple[str, str, str, list[str]]:
    """(漢数字のラベル, 算用数字のラベル, 段落の番号, 別の書き方)。段落が 1 つの単位は単位だけ。"""
    suffix = _unit_suffix("Paragraph")
    num = _attr(paragraph, "Num") or "1"
    kanji = numbered_label(num, suffix, arabic=False) or ""
    arabic = numbered_label(num, suffix, arabic=True) or kanji
    if paragraph_count <= 1:
        aliases = [f"{parent_label}{kanji}", f"{parent_label_arabic}{arabic}"] if kanji else []
        return parent_label, parent_label_arabic, num, aliases
    return f"{parent_label}{kanji}", f"{parent_label_arabic}{arabic}", num, []


def _caption(node: object, tag: str) -> str:
    return _clean(_node_text(_child(node, tag)))


def _arabic_title(title: str) -> str:
    """「別表第一」→「別表第1」（番号の前置きの後の漢数字だけを算用数字にする）。"""

    def replace(match: re.Match[str]) -> str:
        number = parse_kanji_number(match.group(2))
        return f"{match.group(1)}{number}" if number is not None else match.group(0)

    text = text_patterns()
    return re.sub(rf"({text.prefix})({text.kanji}+)", replace, title)


class _Builder:
    def __init__(self) -> None:
        self.units: list[_Unit] = []

    @staticmethod
    def _prefix(part: _Part) -> tuple[str, str]:
        return ("", "") if part.kind == "main" else (part.label, part.label_arabic)

    def numbered_unit(self, node: Mapping[str, Any], path: list[str], part: _Part) -> None:
        """番号つきの単位（``Article``）と、その段落。"""
        suffix = _unit_suffix("Article")
        num = _attr(node, "Num")
        title = _clean(_node_text(_child(node, "ArticleTitle")))
        kanji = numbered_label(num, suffix, arabic=False) or title
        arabic = numbered_label(num, suffix, arabic=True) or kanji
        prefix, prefix_arabic = self._prefix(part)
        parent_label = f"{prefix}{kanji}"
        parent_label_arabic = f"{prefix_arabic}{arabic}"
        caption = _caption(node, "ArticleCaption")
        unit_path = [*path, f"{title or kanji}{caption}"]
        paragraphs = _children_by_tag(node, "Paragraph")
        deleted = _attr(node, "Delete") == "true"
        if not paragraphs:
            body = _clean(
                "".join(
                    _node_text(child)
                    for child in _children(node)
                    if _tag(child) not in {"ArticleTitle", "ArticleCaption"}
                )
            )
            self.units.append(
                _Unit(
                    unit_kind="unit",
                    text_lines=[body] if body else [],
                    section_path=unit_path,
                    part=part,
                    citation=parent_label,
                    citation_arabic=parent_label_arabic,
                    parent_label=parent_label,
                    parent_label_arabic=parent_label_arabic,
                    parent_caption=caption,
                    parent_num=num,
                    deleted=True,
                )
            )
            return
        for paragraph in paragraphs:
            label, label_arabic, paragraph_num, aliases = _paragraph_label(
                parent_label, parent_label_arabic, paragraph, paragraph_count=len(paragraphs)
            )
            lines, has_table = _paragraph_lines(paragraph)
            paragraph_caption = _caption(paragraph, "ParagraphCaption")
            if paragraph_caption and not caption:
                lines = [paragraph_caption, *lines]
            self.units.append(
                _Unit(
                    unit_kind="paragraph",
                    text_lines=lines,
                    section_path=unit_path,
                    part=part,
                    citation=label,
                    citation_arabic=label_arabic,
                    parent_label=parent_label,
                    parent_label_arabic=parent_label_arabic,
                    parent_caption=caption,
                    parent_num=num,
                    paragraph_num=paragraph_num,
                    aliases=aliases,
                    items=_item_refs(paragraph),
                    has_table=has_table,
                    deleted=deleted,
                )
            )

    def bare_paragraphs(
        self, paragraphs: list[Mapping[str, Any]], path: list[str], part: _Part
    ) -> None:
        """番号つきの単位の無い段落（附属の部の直下の段落など）。"""
        suffix = _unit_suffix("Paragraph")
        prefix, prefix_arabic = self._prefix(part)
        for paragraph in paragraphs:
            num = _attr(paragraph, "Num") or "1"
            if len(paragraphs) <= 1:
                label, label_arabic = prefix, prefix_arabic
            else:
                label = f"{prefix}{numbered_label(num, suffix, arabic=False) or ''}"
                label_arabic = f"{prefix_arabic}{numbered_label(num, suffix, arabic=True) or ''}"
            lines, has_table = _paragraph_lines(paragraph)
            paragraph_caption = _caption(paragraph, "ParagraphCaption")
            if paragraph_caption:
                lines = [paragraph_caption, *lines]
            self.units.append(
                _Unit(
                    unit_kind="paragraph",
                    text_lines=lines,
                    section_path=path,
                    part=part,
                    citation=label,
                    citation_arabic=label_arabic,
                    paragraph_num=num,
                    items=_item_refs(paragraph),
                    has_table=has_table,
                )
            )

    def structure(self, node: Mapping[str, Any], path: list[str], part: _Part) -> None:
        """階層の見出し（編・章・節など）と単位を順に読む。"""
        for child in _children(node):
            tag = _tag(child)
            if not isinstance(child, Mapping):
                continue
            if tag in _STRUCTURE_TAGS:
                title = _structure_title(child)
                self.structure(child, [*path, title] if title else path, part)
            elif tag == "Article":
                self.numbered_unit(child, path, part)
            elif tag in _APPENDIX_TAGS:
                self.appendix(child, path, part)

    def appendix(self, node: Mapping[str, Any], path: list[str], part: _Part) -> None:
        """別表（表・細目・備考。図は持たず、題名と関係する単位の表示だけ）。"""
        tag = _tag(node)
        title = _clean(_node_text(_child(node, f"{tag}Title"))) or _label("appendix")
        related = _clean(_node_text(_child(node, "RelatedArticleNum")))
        lines: list[str] = []
        has_table = False
        for child in _children(node):
            child_tag = _tag(child)
            if child_tag in {f"{tag}Title", "RelatedArticleNum"}:
                continue
            if child_tag == "TableStruct":
                lines.extend(_table_lines(child))
                has_table = True
            elif child_tag in _ITEM_TAGS:
                lines.extend(_item_lines(child, depth=0))
            elif child_tag == "Remarks":
                lines.extend(_remarks_lines(child))
            elif child_tag in {"StyleStruct", "FigStruct", "FormatStruct", "NoteStruct"}:
                text = _clean(_block_text(child))
                if text:
                    lines.append(text)
        prefix, prefix_arabic = self._prefix(part)
        qualified = not title.startswith(prefix) if prefix else True
        self.units.append(
            _Unit(
                unit_kind="appendix",
                text_lines=lines or [title],
                section_path=[*path, f"{title}{related}"],
                part=part,
                citation=f"{prefix}{title}" if qualified else title,
                citation_arabic=(
                    f"{prefix_arabic}{_arabic_title(title)}" if qualified else _arabic_title(title)
                ),
                parent_caption=related,
                has_table=has_table,
            )
        )

    def supplement(self, node: Mapping[str, Any], base_path: list[str], first: bool) -> None:
        """附属の部（最初の版のものと、後の版で足したもの）。"""
        base_label = re.sub(r"\s+", "", _node_text(_child(node, "SupplProvisionLabel"))) or _first(
            "references", "part_prefixes"
        )
        revision_ref = _attr(node, "AmendLawNum")
        extract = _attr(node, "Extract") == "true"
        if revision_ref:
            short = revision_ref_labels(revision_ref)
            kanji, arabic = short if short else (revision_ref, revision_ref)
            part = _Part(
                kind="supplement_revision",
                label=f"{base_label}{_bracketed(kanji)}",
                label_arabic=f"{base_label}{_bracketed(arabic)}",
                revision_ref=revision_ref,
                extract=extract,
            )
        else:
            label = (
                f"{base_label}{_bracketed(_label('original_supplement'))}" if first else base_label
            )
            part = _Part(
                kind="supplement_original", label=label, label_arabic=label, extract=extract
            )
        path = [*base_path, part.label + (_label("excerpt") if extract else "")]
        bare: list[Mapping[str, Any]] = []
        for child in _children(node):
            tag = _tag(child)
            if not isinstance(child, Mapping):
                continue
            if tag == "Paragraph":
                bare.append(child)
                continue
            if bare:
                self.bare_paragraphs(bare, path, part)
                bare = []
            if tag in _STRUCTURE_TAGS:
                title = _structure_title(child)
                self.structure(child, [*path, title] if title else path, part)
            elif tag == "Article":
                self.numbered_unit(child, path, part)
            elif tag in _APPENDIX_TAGS:
                self.appendix(child, path, part)
        if bare:
            self.bare_paragraphs(bare, path, part)


def _read_units(payload: Mapping[str, Any]) -> tuple[XmlTreeDocumentMetadata, list[_Unit]]:
    root = payload[_TREE_KEY]
    metadata = _document_metadata(payload, root)
    builder = _Builder()
    body = _child(root, f"{_ROOT_TAG}Body") or {}
    base_path = [metadata.title] if metadata.title else []
    main = _Part(kind="main", label="", label_arabic="")
    first_supplement = True
    for child in _children(body):
        tag = _tag(child)
        if not isinstance(child, Mapping) or tag in {f"{_ROOT_TAG}Title", "TOC"}:
            continue
        if tag in {"EnactStatement", "Preamble"}:
            label = _label("preamble")
            lines = [_clean(_node_text(item)) for item in _children_by_tag(child, "Paragraph")]
            if not lines:
                lines = [_clean(_node_text(child))]
            builder.units.append(
                _Unit(
                    unit_kind="statement",
                    text_lines=[line for line in lines if line],
                    section_path=[*base_path, label],
                    part=main,
                    citation=label,
                    citation_arabic=label,
                )
            )
        elif tag == "MainProvision":
            bare = _children_by_tag(child, "Paragraph")
            if bare and not _children_by_tag(child, ("Article", *_STRUCTURE_TAGS)):
                builder.bare_paragraphs(bare, base_path, main)
            else:
                builder.structure(child, base_path, main)
        elif tag == "SupplProvision":
            original = not _attr(child, "AmendLawNum")
            builder.supplement(child, base_path, first_supplement and original)
            if original:
                first_supplement = False
        elif tag in _APPENDIX_TAGS:
            builder.appendix(child, base_path, main)
    return metadata, builder.units


# --- 抽出 ---------------------------------------------------------------------------------


def _part_label(part: _Part, *, arabic: bool) -> str:
    if part.kind == "main":
        return _label("main_part")
    return part.label_arabic if arabic else part.label


def _unit_metadata(
    metadata: XmlTreeDocumentMetadata, unit: _Unit
) -> dict[str, str | int | bool | None]:
    title = metadata.title
    values: dict[str, str | int | bool | None] = {
        "xml_tree_document_id": metadata.document_id or None,
        "xml_tree_document_title": title,
        "xml_tree_document_abbrev": metadata.abbrev or None,
        "xml_tree_document_number": metadata.document_number or None,
        "xml_tree_document_type": metadata.document_type or None,
        "xml_tree_unit_kind": unit.unit_kind,
        "xml_tree_part": unit.part.kind,
        "xml_tree_part_label": _part_label(unit.part, arabic=False),
        "xml_tree_part_label_arabic": _part_label(unit.part, arabic=True),
        "xml_tree_citation_label": f"{title} {unit.citation}".strip(),
        "xml_tree_citation_label_arabic": f"{title} {unit.citation_arabic}".strip(),
        "xml_tree_label": unit.citation,
        "xml_tree_label_arabic": unit.citation_arabic,
    }
    if unit.part.revision_ref:
        values["xml_tree_part_ref"] = unit.part.revision_ref
    if unit.part.extract:
        values["xml_tree_part_extract"] = True
    if unit.parent_label:
        values["xml_tree_parent_label"] = f"{title} {unit.parent_label}".strip()
        values["xml_tree_parent_label_arabic"] = f"{title} {unit.parent_label_arabic}".strip()
        values["xml_tree_parent_num"] = unit.parent_num
    if unit.parent_caption:
        values["xml_tree_parent_caption"] = unit.parent_caption
    if unit.paragraph_num:
        values["xml_tree_paragraph_num"] = unit.paragraph_num
    aliases = list(unit.aliases)
    if unit.part.kind == "supplement_original":
        # 最初の版の附属の部は、添えた語（制定時）を付けない書き方でも引ける。
        qualified = _bracketed(_label("original_supplement"))
        for label in (unit.citation, unit.citation_arabic, *unit.aliases):
            plain = label.replace(qualified, "", 1)
            if plain != label and plain not in aliases:
                aliases.append(plain)
    if aliases:
        values["xml_tree_label_aliases"] = "\n".join(aliases)
    if unit.items:
        values["xml_tree_item_labels"] = ",".join(item.label for item in unit.items)
        values["xml_tree_item_labels_arabic"] = ",".join(item.label_arabic for item in unit.items)
    if unit.deleted:
        values["xml_tree_deleted"] = True
    return values


def xml_tree_json_extraction_from_payload(
    payload: Mapping[str, Any],
    *,
    parser_backend: str = XML_TREE_JSON_BACKEND,
    parser_version: str = XML_TREE_JSON_PARSER_VERSION,
) -> StructuredExtraction:
    """XML ツリー JSON（dict）を、段落を 1 要素にした抽出にする。"""
    metadata, units = _read_units(payload)
    title = metadata.title
    elements: list[DocumentElement] = []
    raw_lines: list[str] = [f"# {title}"] if title else []
    if metadata.document_number:
        raw_lines.append(metadata.document_number)
    for unit in units:
        body_lines = [line for line in unit.text_lines if line.strip()]
        if not body_lines:
            continue
        heading = f"{title} {unit.citation}{unit.parent_caption}".strip()
        text = "\n".join([heading, *body_lines])[:_ELEMENT_TEXT_MAX_CHARS]
        content_kind = "table" if unit.unit_kind == "appendix" and unit.has_table else "text"
        order = len(elements)
        elements.append(
            DocumentElement(
                kind="text",
                text=text,
                order=order,
                element_id=f"xt-{order:05d}",
                content_kind=content_kind,
                source_parser=parser_backend,
                section_path=unit.section_path,
                confidence=1.0,
                metadata={
                    **_unit_metadata(metadata, unit),
                    "section_level": len(unit.section_path),
                },
            )
        )
        raw_lines.append(text)
    fields = [
        ExtractionField(name=name, value=value)
        for name, value in (
            (_label("field_title"), title),
            (_label("field_number"), metadata.document_number),
            (_label("field_id"), metadata.document_id),
            (_label("field_type"), metadata.document_type),
            (_label("field_abbrev"), metadata.abbrev),
            (_label("field_category"), metadata.category),
            (_label("field_revision_date"), metadata.revision_date),
        )
        if value
    ]
    unit_count = len(
        {
            (
                element.metadata.get("xml_tree_part_label"),
                element.metadata.get("xml_tree_parent_num"),
            )
            for element in elements
            if element.metadata.get("xml_tree_parent_num")
        }
    )
    supplement_count = len(
        {
            element.metadata.get("xml_tree_part_label")
            for element in elements
            if element.metadata.get("xml_tree_part") != "main"
        }
    )
    return StructuredExtraction(
        raw_text="\n".join(raw_lines),
        document_type=_label("xml_tree_document_type"),
        confidence=1.0,
        elements=elements,
        navigation=_navigation(elements),
        fields=fields,
        parser_artifacts={
            "source_parser": parser_backend,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            "source_format": XML_TREE_JSON_SOURCE_FORMAT,
            XML_TREE_JSON_FORMAT_VERSION_KEY: XML_TREE_JSON_FORMAT_VERSION,
            XML_TREE_DOCUMENT_ARTIFACT_KEY: {
                **metadata.to_artifact(),
                "unit_count": unit_count,
                "element_count": len(elements),
                "supplement_count": supplement_count,
            },
        },
    )


def _navigation(elements: Sequence[DocumentElement]) -> list[DocumentNavigationNode]:
    """``section_path`` から章節の木を作る（backend は取込で作り直す）。"""
    nodes: dict[tuple[str, ...], DocumentNavigationNode] = {}
    ordered: list[DocumentNavigationNode] = []
    for element in elements:
        path = tuple(element.section_path)
        for depth in range(1, len(path) + 1):
            key = path[:depth]
            node = nodes.get(key)
            if node is None:
                parent = nodes.get(key[:-1]) if depth > 1 else None
                node = DocumentNavigationNode(
                    section_id=f"sec-{len(ordered):05d}",
                    title=key[-1],
                    section_path=list(key),
                    depth=depth - 1,
                    parent_section_id=parent.section_id if parent else None,
                )
                nodes[key] = node
                ordered.append(node)
                if parent is not None:
                    parent.child_section_ids.append(node.section_id)
            if element.element_id:
                node.element_ids.append(element.element_id)
    return ordered


def parse_xml_tree_json(data: bytes) -> Mapping[str, Any] | None:
    """XML ツリー JSON を読む（対応するスキーマでなければ None）。"""
    payload = _load_json(data)
    return payload if _is_tree_payload(payload) and isinstance(payload, Mapping) else None


def xml_tree_json_extraction(
    data: bytes,
    *,
    parser_backend: str = XML_TREE_JSON_BACKEND,
    parser_version: str = XML_TREE_JSON_PARSER_VERSION,
) -> StructuredExtraction | None:
    """XML ツリー JSON（bytes）を抽出にする。対応するスキーマでなければ None。"""
    payload = parse_xml_tree_json(data)
    if payload is None:
        return None
    return xml_tree_json_extraction_from_payload(
        payload, parser_backend=parser_backend, parser_version=parser_version
    )


# --- 引用ラベル・検索の文・照合の key -----------------------------------------------------


def _metadata_str(metadata: Mapping[str, object], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) else ""


def xml_tree_citation_labels(metadata: Mapping[str, object]) -> list[str]:
    """要素・chunk の引用ラベルのすべての書き方（漢数字・算用数字・別の書き方）。

    文書名つきと文書名なしの両方を返す（重複なし。並びは漢数字 → 算用数字）。
    """
    title = _metadata_str(metadata, "xml_tree_document_title")
    labels: list[str] = []
    for key in ("xml_tree_label", "xml_tree_label_arabic"):
        if label := _metadata_str(metadata, key):
            labels.append(label)
    labels.extend(
        line.strip()
        for line in _metadata_str(metadata, "xml_tree_label_aliases").splitlines()
        if line.strip()
    )
    result: list[str] = []
    for label in labels:
        for candidate in (f"{title} {label}".strip() if title else label, label):
            if candidate and candidate not in result:
                result.append(candidate)
    return result


def xml_tree_item_citation_labels(metadata: Mapping[str, object]) -> list[str]:
    """段落の細目の引用ラベル（段落のラベル + 細目の番号。漢数字と算用数字）。"""
    labels: list[str] = []
    for label_key, items_key in (
        ("xml_tree_label", "xml_tree_item_labels"),
        ("xml_tree_label_arabic", "xml_tree_item_labels_arabic"),
    ):
        base = _metadata_str(metadata, label_key)
        for item in _metadata_str(metadata, items_key).split(","):
            if item.strip():
                labels.append(f"{base}{item.strip()}")
    return labels


def xml_tree_citation_key(text: str) -> str:
    """引用ラベルを照合の key にする（NFKC・空白を除く・「第」「の」の後の漢数字を算用数字に）。

    漢数字の書き方と算用数字の書き方、空白の有無は同じ key になる。
    """
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))

    def replace(match: re.Match[str]) -> str:
        number = parse_kanji_number(match.group(2))
        return f"{match.group(1)}{number}" if number is not None else match.group(0)

    text = text_patterns()
    connectors = alternation(current_pack().words("numbers", "branch_connectors"))
    return re.sub(rf"({text.prefix}|{connectors})({text.kanji}+)", replace, normalized)


_query_citations: dict[str, re.Pattern[str]] = {}


def _query_citation() -> re.Pattern[str]:
    """質問の中の引用ラベルと、その直前の語（文書名）。

    ラベルは〔附属の部〕第N〈単位〉〔のN〕〔第N〈段落〉〕〔第N〈細目〉〕。部の前置き・番号の前置き・
    枝番のつなぎ・単位・記号は言語資源（#2181）。質問は NFKC でそろえてから当てる。
    """
    pack = current_pack()
    text = text_patterns(pack)
    cached = _query_citations.get(pack.hash)
    if cached is not None:
        return cached
    number = text.number
    connector = alternation(pack.words("numbers", "branch_connectors"))
    part = alternation(pack.words("references", "part_prefixes"))
    brackets = [pair for pair in pack.words("symbols", "bracket_pairs") if len(pair) == 2]
    qualifier = (
        "|".join(
            rf"{re.escape(open_)}[^{re.escape(close)}]{{1,40}}{re.escape(close)}"
            for open_, close in brackets
        )
        or NEVER
    )
    stops = "".join(
        [
            *(w for w in pack.words("symbols", "clause_separator_chars") if len(w) == 1),
            *(w for w in pack.words("symbols", "sentence_end_chars") if len(w) == 1),
            *(char for pair in pack.words("symbols", "quote_brackets") for char in pair),
            *(char for pair in brackets for char in pair),
            ".",
        ]
    )
    prefix = text.prefix
    pattern = re.compile(
        rf"(?P<title>[^\s{re.escape(stops)}]{{1,40}}?)?\s*"
        rf"(?P<label>(?:{part}(?:{qualifier})?)?"
        rf"{prefix}{number}{text.article_units}(?:{connector}{number})*"
        rf"(?:{prefix}{number}{text.paragraph_units})?"
        rf"(?:{prefix}{number}{text.item_units}(?:{connector}{number})*)?)"
    )
    if len(_query_citations) >= 16:
        _query_citations.clear()
    _query_citations[pack.hash] = pattern
    return pattern


_MAX_QUERY_CITATIONS = 3


def _supplement_tail() -> re.Pattern[str]:
    """語の末尾の附属の部の印（「附則」「附則(〜)」。部の前置き・括弧は言語資源）。"""
    pack = current_pack()
    part = alternation(pack.words("references", "part_prefixes"))
    brackets = [pair for pair in pack.words("symbols", "bracket_pairs") if len(pair) == 2]
    qualifier = (
        "|".join(
            rf"{re.escape(open_)}[^{re.escape(close)}]*{re.escape(close)}"
            for open_, close in brackets
        )
        or NEVER
    )
    return re.compile(rf"{part}(?:{qualifier})?$")


def query_citation_keys(query: str) -> list[str]:
    """質問の中の引用ラベルの照合の key（文書名つきの key を先に。最大 3 つのラベル）。

    番号は漢数字でも算用数字でもよい（``xml_tree_citation_key`` で同じ key にする）。
    """
    keys: list[str] = []
    query = unicodedata.normalize("NFKC", query)
    for match in list(_query_citation().finditer(query))[:_MAX_QUERY_CITATIONS]:
        label = match.group("label")
        title = (match.group("title") or "").strip()
        # 附属の部の印（附則）は直前の語に入りやすいので、ラベルの側へ戻す。
        if supplement := _supplement_tail().search(title):
            title, label = title[: supplement.start()], supplement.group(0) + label
        label = xml_tree_citation_key(label)
        for key in ((xml_tree_citation_key(f"{title}{label}") if title else ""), label):
            if key and key not in keys:
                keys.append(key)
    return keys


def xml_tree_lookup_keys(metadata: Mapping[str, object]) -> list[str]:
    """chunk の引用ラベルの照合の key（段落・細目・別の書き方。文書名つきと無し）。"""
    title = _metadata_str(metadata, "xml_tree_document_title")
    labels = xml_tree_citation_labels(metadata)
    for item in xml_tree_item_citation_labels(metadata):
        labels.extend([f"{title} {item}" if title else item, item])
    keys: list[str] = []
    for label in labels:
        key = xml_tree_citation_key(label)
        if key and key not in keys:
            keys.append(key)
    return keys


_number_phrases: dict[str, re.Pattern[str]] = {}


def _number_phrase() -> re.Pattern[str]:
    """本文の漢数字の数（助数詞・条・項・号の付いたもの）と、続く語（「以上」「被保険者」など）。

    続く語に漢数字は含めない。助数詞・単位・語の字は言語資源（#2181）。
    """
    pack = current_pack()
    cached = _number_phrases.get(pack.hash)
    if cached is not None:
        return cached
    text = text_patterns(pack)
    units = alternation(
        (
            *pack.words("numbers", "counters"),
            *pack.words("numbers", "article_units"),
            *pack.words("numbers", "paragraph_units"),
            *pack.words("numbers", "item_units"),
        )
    )
    kanji_chars = text.kanji[1:-1] if text.kanji.startswith("[") else ""
    term_chars = range_class(pack.words("numbers", "number_term_chars"))
    pattern = re.compile(
        rf"({text.prefix})?({text.kanji}+)({units})"
        rf"((?:(?![{kanji_chars}]|{text.prefix}){term_chars}){{0,5}})"
    )
    if len(_number_phrases) >= 16:
        _number_phrases.clear()
    _number_phrases[pack.hash] = pattern
    return pattern


# 検索の文に足す数の表記の上限（本文の意味を薄めない）。
_MAX_NUMBER_PHRASES = 12


def arabic_number_phrases(text: str) -> list[str]:
    """本文の漢数字の数を算用数字にした表記（「六十五歳以上」→「65歳以上」。#2148）。

    質問は算用数字で書かれることが多いので、検索の文に両方の書き方を持たせる（全文検索の語と
    embedding の両方）。本文はそのまま回答に使う。
    """
    phrases: list[str] = []
    pack = current_pack()
    citation_units = {
        *pack.words("numbers", "article_units"),
        *pack.words("numbers", "paragraph_units"),
        *pack.words("numbers", "item_units"),
    }
    stop_chars = "".join(pack.words("numbers", "term_stop_chars"))
    for match in _number_phrase().finditer(text):
        number = parse_kanji_number(match.group(2))
        word = match.group(4)
        if number is None or (
            match.group(3) in citation_units and (len(word) < 2 or word[0] in stop_chars)
        ):
            continue
        phrase = f"{match.group(1) or ''}{number}{match.group(3)}{match.group(4)}"
        if phrase not in phrases:
            phrases.append(phrase)
            if len(phrases) >= _MAX_NUMBER_PHRASES:
                break
    return phrases


def xml_tree_search_text(
    metadata: Mapping[str, object],
    text: str,
    *,
    section_path: Sequence[str] = (),
) -> str:
    """検索・embedding 用の文（引用ラベルの漢数字と算用数字の両方・細目・部・階層の見出しと本文）。

    番号を算用数字で書いた質問でも、漢数字で書いた質問でも全文検索とベクトル検索で当たるようにする。
    """
    lines: list[str] = []
    title = _metadata_str(metadata, "xml_tree_document_title")
    number = _metadata_str(metadata, "xml_tree_document_number")
    if title:
        abbrev = _metadata_str(metadata, "xml_tree_document_abbrev")
        suffix = _bracketed(number) if number else ""
        abbrev_line = f" {_search_label('abbrev')}: {abbrev}" if abbrev else ""
        lines.append(f"{_search_label('document')}: {title}{suffix}" + abbrev_line)
    # 文書名は上の行にあるので、引用ラベルは文書名なしの書き方にする。別の書き方は算用数字の形
    # だけにする（漢数字の形は本文の先頭の行にある。ラベルが多すぎて本文の意味を薄めない）。
    labels = [
        label
        for label in (
            _metadata_str(metadata, "xml_tree_label"),
            _metadata_str(metadata, "xml_tree_label_arabic"),
            *(
                alias.strip()
                for alias in _metadata_str(metadata, "xml_tree_label_aliases").splitlines()
                if re.search(r"[0-9]", alias)
            ),
        )
        if label
    ]
    if labels:
        lines.append(f"{_search_label('citation')}: " + " / ".join(dict.fromkeys(labels)))
    if items := xml_tree_item_citation_labels(metadata):
        lines.append(f"{_search_label('items')}: " + " ".join(items))
    part = _metadata_str(metadata, "xml_tree_part_label")
    part_arabic = _metadata_str(metadata, "xml_tree_part_label_arabic")
    if part:
        lines.append(
            f"{_search_label('part')}: "
            + " / ".join(dict.fromkeys(item for item in (part, part_arabic) if item))
        )
    if caption := _metadata_str(metadata, "xml_tree_parent_caption"):
        lines.append(f"{_search_label('caption')}: {caption}")
    if section_path:
        lines.append(f"{_search_label('path')}: " + " > ".join(section_path))
    if numbers := arabic_number_phrases(text):
        lines.append(f"{_search_label('numbers')}: " + " / ".join(numbers))
    lines.append(f"{_search_label('body')}:")
    lines.append(text)
    return "\n".join(lines)


def iter_xml_tree_metadata(metadata: Mapping[str, object]) -> Iterator[tuple[str, object]]:
    """要素・chunk の metadata のうちこの解析の key（``xml_tree_`` で始まる）。"""
    for key, value in metadata.items():
        if key.startswith(XML_TREE_METADATA_PREFIX):
            yield key, value


def is_xml_tree_json_extraction(extraction: StructuredExtraction) -> bool:
    """XML ツリー JSON の解析の抽出か（分割が単位 = 親・段落 = 子にする）。"""
    return extraction.parser_artifacts.get(XML_TREE_JSON_FORMAT_VERSION_KEY) is not None


__all__ = [
    "XML_TREE_DOCUMENT_ARTIFACT_KEY",
    "XML_TREE_JSON_BACKEND",
    "XML_TREE_JSON_FORMAT_VERSION",
    "XML_TREE_JSON_FORMAT_VERSION_KEY",
    "XML_TREE_JSON_PARSER_VERSION",
    "XML_TREE_JSON_SOURCE_FORMAT",
    "XML_TREE_LOOKUP_KEYS_KEY",
    "XML_TREE_METADATA_PREFIX",
    "XmlTreeDocumentMetadata",
    "is_xml_tree_json",
    "is_xml_tree_json_extraction",
    "iter_xml_tree_metadata",
    "kanji_number",
    "looks_like_xml_tree_json",
    "numbered_label",
    "parse_kanji_number",
    "parse_xml_tree_json",
    "query_citation_keys",
    "revision_ref_labels",
    "xml_tree_citation_key",
    "xml_tree_citation_labels",
    "xml_tree_item_citation_labels",
    "xml_tree_json_extraction",
    "xml_tree_json_extraction_from_payload",
    "xml_tree_lookup_keys",
    "xml_tree_search_text",
]
