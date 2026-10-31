"""章節の抽出規則(#715)。

保存済みの抽出結果の要素に規則を当てて、章節(並び順 + 階層の一覧)を作る。抽出のやり直しは
要らず、規則を変えるとすぐ章節ナビゲーションに出る。取込で章節の要約を生成するときにも
同じ規則を使う。保存済みの要約を現在の規則で作り直すときは抽出から再処理する。

- 方式: ``parser``(解析エンジンの見出し。今までどおり抽出結果の章節を使う)/ プリセット
  ``legal``(法令)・``official``(公用文)・``numbered``(番号付き)/ ``custom``(独自の規則)。
- 照合: 各要素の 1 行目を NFKC で全角・半角をそろえ、規則の正規表現を行頭から当てる(並びが優先
  順位)。長すぎる行・文で終わる行は見出しにしない。
- 全体の既定は ``section-rules.json``(``RAG_SECTION_RULES_FILE`` で場所を変えられる。抽出項目の
  定義と同じ形)。処理レシピの ``section_rules_mode`` で方式を上書きできる。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from rag_parser_core.extraction import is_derived_content
from rag_parser_core.language import current_pack
from rag_parser_core.language.text import alternation, numbered, text_patterns

from app.config import BACKEND_ROOT
from app.rag.document_sections import sections_from_navigation
from app.schemas.document import DocumentSection, SectionRulesMode
from app.schemas.extraction import DocumentElement, DocumentNavigationNode, StructuredExtraction

SECTION_RULES_MODES: tuple[SectionRulesMode, ...] = (
    "parser",
    "legal",
    "official",
    "numbered",
    "custom",
)
SECTION_RULES_FILE_ENV = "RAG_SECTION_RULES_FILE"
DEFAULT_SECTION_RULES_FILE = "section-rules.json"
MAX_SECTION_RULES = 30
# 見出しとみなす行の長さの上限(本文の 1 文を見出しにしない)。
MAX_HEADING_CHARS = 80
# 図・表など、見出しにしない要素の種類。
_NON_HEADING_KINDS = frozenset({"table", "figure", "image", "section_summary", "formula", "code"})


class SectionRule(BaseModel):
    """章節の抽出規則 1 件。``pattern`` を要素の 1 行目の行頭から当て、合えば ``level`` の章節。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=80)
    pattern: str = Field(min_length=1, max_length=300)
    level: int = Field(ge=1, le=6)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("規則の名前を入力してください。")
        return cleaned

    @field_validator("pattern")
    @classmethod
    def _compilable(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"正規表現として読めません（{exc.msg}）。") from exc
        return value


class SectionRulesSettingsData(BaseModel):
    """章節の抽出規則の全体の既定と、プリセットの中身(画面の表示用)。"""

    mode: SectionRulesMode
    rules: list[SectionRule]
    presets: dict[str, list[SectionRule]]


class SectionRulesStore(BaseModel):
    """章節の抽出規則の全体の既定。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    mode: SectionRulesMode = "parser"
    rules: list[SectionRule] = Field(default_factory=list, max_length=MAX_SECTION_RULES)


def preset_rules() -> dict[SectionRulesMode, list[SectionRule]]:
    """プリセット(rag_engine/chunking/headings.py・rag_parser_core の見出しの規則を下敷きにした)。

    番号の前置き・数・単位・枝番のつなぎ・番号の代わりの仮名は言語資源(#2181)から作る。
    """
    text = text_patterns()
    pack = current_pack()
    connector = alternation(pack.words("numbers", "branch_connectors"))
    legal_levels = (
        ("part_units", 1),
        ("chapter_units", 2),
        ("section_units", 3),
        ("subsection_units", 4),
        ("division_units", 5),
        ("article_units", 6),
    )
    legal = []
    for key, level in legal_levels:
        words = pack.words("numbers", key)
        if not words:
            continue
        branch = rf"(?:{connector}{text.number})*" if key == "article_units" else ""
        legal.append(
            SectionRule(
                name=" / ".join(words),
                pattern=rf"^{numbered(text, getattr(text, key))}{branch}",
                level=level,
            )
        )
    kana = text.listing_kana
    prefix = (pack.words("numbers", "number_prefixes") or ("",))[0]
    first_kana = (pack.words("numbers", "listing_kana") or ("",))[0][:1]
    return {
        "legal": legal,
        "official": [
            SectionRule(name=f"{prefix}1", pattern=rf"^{text.prefix}[0-9]+(\s|$)", level=1),
            SectionRule(name="1", pattern=r"^[0-9]+(\s|[.、]\s*)\S", level=2),
            SectionRule(name="(1)", pattern=r"^\([0-9]+\)", level=3),
            SectionRule(name=first_kana or "-", pattern=rf"^{kana}(\s|[.、])", level=4),
            SectionRule(name=f"({first_kana or '-'})", pattern=rf"^\({kana}\)", level=5),
        ],
        "numbered": [
            SectionRule(name="1.1.1", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(\s|\.)", level=3),
            SectionRule(name="1.1", pattern=r"^[0-9]+\.[0-9]+(\s|\.)", level=2),
            SectionRule(name="1.", pattern=r"^[0-9]+\.\s", level=1),
        ],
    }


def _section_rules_path() -> Path:
    raw = os.environ.get(SECTION_RULES_FILE_ENV, "").strip() or DEFAULT_SECTION_RULES_FILE
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


def load_section_rules() -> SectionRulesStore:
    """全体の既定。保存していない・読めないときは「解析エンジンの見出し」。"""
    try:
        data = _section_rules_path().read_text(encoding="utf-8")
    except OSError:
        return SectionRulesStore()
    try:
        return SectionRulesStore.model_validate_json(data)
    except ValueError:
        return SectionRulesStore()


def save_section_rules(store: SectionRulesStore) -> SectionRulesStore:
    path = _section_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")
    return store


def reset_section_rules() -> None:
    """保存した全体の既定を消し、「解析エンジンの見出し」に戻す。"""
    _section_rules_path().unlink(missing_ok=True)


def rules_for_mode(mode: SectionRulesMode, store: SectionRulesStore) -> list[SectionRule] | None:
    """方式の規則。``parser`` は None(抽出結果の章節を使う)。"""
    if mode == "parser":
        return None
    rules = store.rules if mode == "custom" else preset_rules()[mode]
    return [rule for rule in rules if rule.enabled]


def _heading_line(element: DocumentElement) -> str:
    if is_derived_content(element.content_kind, element.metadata):
        return ""
    if (element.kind or "").lower() in _NON_HEADING_KINDS:
        return ""
    if (element.content_kind or "").lower() in _NON_HEADING_KINDS:
        return ""
    line = element.text.strip().split("\n", 1)[0].strip()
    # Markdown の見出しの印は、規則を当てる前に外す。
    return re.sub(r"^#{1,6}\s+", "", line)


def _rule_sections(
    elements: Sequence[DocumentElement], rules: Sequence[SectionRule]
) -> list[tuple[DocumentSection, str | None]]:
    """要素に規則を当てた章節と、その見出しの要素の ID(章節と chunk の対応に使う。#1438)。"""
    compiled = [(re.compile(rule.pattern), rule.level) for rule in rules]
    ordered = sorted(elements, key=lambda element: element.order)
    # (要素の位置, 名前, 階層, ページ, 要素の ID)
    headings: list[tuple[int, str, int, int | None, str | None]] = []
    for position, element in enumerate(ordered):
        line = _heading_line(element)
        if (
            not line
            or len(line) > MAX_HEADING_CHARS
            or re.search(rf"{text_patterns().heading_terminal}$", line)
        ):
            continue
        normalized = unicodedata.normalize("NFKC", line)
        level = next((lvl for pattern, lvl in compiled if pattern.match(normalized)), None)
        if level is not None:
            headings.append((position, line, level, element.page_number, element.element_id))

    sections: list[tuple[DocumentSection, str | None]] = []
    # 規則の階層(1〜6)を、章節の木の深さに写す。浅い・同じ階層が来たら戻り、規則の階層が飛んで
    # いても 1 段ずつにする(例: 法令の「章(2)」「条(6)」は 1・2)。
    stack: list[int] = []
    for index, (position, title, raw_level, page, element_id) in enumerate(headings):
        end_position = next(
            (later[0] for later in headings[index + 1 :] if later[2] <= raw_level), len(ordered)
        )
        pages = [
            element.page_number
            for element in ordered[position:end_position]
            if element.page_number is not None
        ]
        while stack and stack[-1] >= raw_level:
            stack.pop()
        stack.append(raw_level)
        level = min(len(stack), 6)
        section_id = (
            "rule-"
            + hashlib.sha1(f"{index}:{title}".encode(), usedforsecurity=False).hexdigest()[:16]
        )
        sections.append(
            (
                DocumentSection(
                    id=section_id,
                    title=title[:200],
                    level=level,
                    page_start=page if page is not None else (min(pages) if pages else None),
                    page_end=max(pages) if pages else None,
                    origin="extraction",
                    source_section_id=section_id,
                ),
                element_id,
            )
        )
    return sections


def sections_from_rules(
    elements: Sequence[DocumentElement], rules: Sequence[SectionRule]
) -> list[DocumentSection]:
    """要素に規則を当てて章節を作る。ページ範囲は次の同じか浅い階層の章節の直前まで。"""
    return [section for section, _element_id in _rule_sections(elements, rules)]


def extraction_sections(
    extraction: StructuredExtraction | None,
    mode: SectionRulesMode,
    store: SectionRulesStore | None = None,
) -> list[DocumentSection]:
    """抽出結果の章節(方式が ``parser`` なら解析エンジンの見出し、それ以外は規則を当てた章節)。"""
    if extraction is None:
        return []
    rules = rules_for_mode(mode, store or load_section_rules())
    if rules is None:
        return sections_from_navigation(parser_navigation(extraction))
    return sections_from_rules(extraction.elements, rules)


def extraction_section_anchors(
    extraction: StructuredExtraction | None,
    mode: SectionRulesMode,
    store: SectionRulesStore | None = None,
) -> dict[str, frozenset[str]]:
    """抽出結果の章節の id → その章節の始まりを示す解析の要素の ID(#1438)。

    章節と chunk の対応(``app.rag.section_chunk_map``)で、同じ解析の結果の chunk の中から章節の
    始まりを決めるのに使う。解析エンジンの見出しは章節の木の要素(その章節に直接属する要素)、規則の
    章節は見出しの要素。要素の ID の無い章節は含めない(ページと名前で決める)。
    """
    if extraction is None:
        return {}
    rules = rules_for_mode(mode, store or load_section_rules())
    if rules is None:
        return {
            node.section_id: frozenset(node.element_ids)
            for node in parser_navigation(extraction)
            if node.element_ids
        }
    return {
        section.id: frozenset({element_id})
        for section, element_id in _rule_sections(extraction.elements, rules)
        if element_id
    }


def section_rules_settings(store: SectionRulesStore | None = None) -> SectionRulesSettingsData:
    current = store or load_section_rules()
    return SectionRulesSettingsData(
        mode=current.mode,
        rules=current.rules,
        presets=preset_rules(),
    )


# 規則の木で要約した抽出でも、解析エンジンの見出しの木へ再取込なしで切り替えられる。
PARSER_NAVIGATION_ARTIFACT = "parser_navigation"
NAVIGATION_RULES_MODE_ARTIFACT = "navigation_rules_mode"


def parser_navigation(extraction: StructuredExtraction) -> list[DocumentNavigationNode]:
    """規則の要約と別に保持した解析見出しの木。既存の抽出結果は従来の navigation。"""
    stored = extraction.parser_artifacts.get(PARSER_NAVIGATION_ARTIFACT)
    if isinstance(stored, list):
        return [DocumentNavigationNode.model_validate(node) for node in stored]
    return extraction.navigation


def build_rule_navigation(
    extraction: StructuredExtraction, rules: Sequence[SectionRule]
) -> list[DocumentNavigationNode]:
    """表示と同じ章節 ID の木へ、原文の要素を直属の章節だけに割り当てる。"""
    originals = [
        element
        for element in sorted(extraction.elements, key=lambda element: element.order)
        if not is_derived_content(element.content_kind, element.metadata)
    ]
    entries = _rule_sections(originals, rules)
    nodes: list[DocumentNavigationNode] = []
    stack: list[DocumentNavigationNode] = []
    by_heading: dict[str, DocumentNavigationNode] = {}
    for section, heading_id in entries:
        while stack and stack[-1].depth >= section.level:
            stack.pop()
        parent = stack[-1] if stack else None
        node = DocumentNavigationNode(
            section_id=section.id,
            title=section.title,
            depth=section.level,
            section_path=[*(parent.section_path if parent else []), section.title],
            parent_section_id=parent.section_id if parent else None,
            page_start=section.page_start,
            page_end=section.page_end,
        )
        if parent:
            parent.child_section_ids.append(node.section_id)
        nodes.append(node)
        stack.append(node)
        if heading_id:
            by_heading[heading_id] = node
    current: DocumentNavigationNode | None = None
    for element in originals:
        if element.element_id in by_heading:
            current = by_heading[element.element_id]
        if current is not None and element.element_id:
            current.element_ids.append(element.element_id)
    return nodes


NAVIGATION_RULES_FINGERPRINT_ARTIFACT = "navigation_rules_fingerprint"


def section_rules_fingerprint(mode: SectionRulesMode, store: SectionRulesStore) -> str:
    """要約に使った規則の一致を確かめる。章節 ID が同じでも階層・境界が変わり得る。"""
    rules = rules_for_mode(mode, store)
    payload = {
        "mode": mode,
        "rules": [rule.model_dump(mode="json") for rule in rules] if rules is not None else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
