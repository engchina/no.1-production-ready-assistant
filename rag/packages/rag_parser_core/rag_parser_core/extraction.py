"""VLM/LLM テキスト抽出スキーマ。"""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, Self, TypeGuard

from pydantic import BaseModel, Field, field_validator, model_validator

from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import numbered, text_patterns

type ExtractionMetadataValue = str | int | float | bool | None
# parser_artifacts は source_derivation(派生系譜)など **nested な JSON** も保持するため、
# scalar に限らず list / dict を許容する再帰型にする(model_validate での round-trip を保証)。
type ExtractionArtifactValue = (
    str
    | int
    | float
    | bool
    | None
    | list["ExtractionArtifactValue"]
    | dict[str, "ExtractionArtifactValue"]
)

# 取込が原文から作った派生の要素・chunk（章節ナビゲーションの要約・項目抽出の値・RAPTOR の要約）の
# content_kind と印（#2590）。索引の手がかりで、引用できる原文ではない。見出しとして読み直さず、
# 分割で原文の要素と結合せず、章節の本文に入れない。
SECTION_SUMMARY_CONTENT_KIND = "section_summary"
EXTRACTED_FIELD_CONTENT_KIND = "field"
DERIVED_CONTENT_KINDS = frozenset({SECTION_SUMMARY_CONTENT_KIND, EXTRACTED_FIELD_CONTENT_KIND})
NAV_SUMMARY_KEY = "nav_summary"
EXTRACTED_FIELD_KEY = "extracted_field"
RAPTOR_SUMMARY_KEY = "raptor_summary"
# 項目抽出の値を読み取った元の要素の ID。分割で chunk に引き継ぎ、検索で当たった値の chunk を
# 根拠に渡すときに、この要素を含む原文の chunk を読む（#2641）。
FIELD_SOURCE_ELEMENT_KEY = "field_source_element_id"
DERIVED_FLAG_KEYS = (NAV_SUMMARY_KEY, EXTRACTED_FIELD_KEY, RAPTOR_SUMMARY_KEY)

# 書式の飾りとして文字に重ねる結合の線（上線 U+0305・下線 U+0332・二重下線 U+0333・
# 二重上線 U+033F）。前後の文字の線とつながるように作られた記号で、文字の読みと意味を変えない。
# PDF などの抽出が下線・上線の書式をこの形で本文に残すと、表示の書体に字形が無く豆腐になり、
# 検索の語も切れる（#3354）。文字の一部になる結合文字（濁点・半濁点 U+3099 / U+309A、
# アクセント U+0301、マクロン U+0304、下のマクロン U+0331 など）は除かない。
FORMATTING_COMBINING_MARKS = frozenset({"\u0305", "\u0332", "\u0333", "\u033f"})
_FORMATTING_MARK_TABLE = dict.fromkeys(map(ord, FORMATTING_COMBINING_MARKS))


def strip_formatting_marks(text: str) -> str:
    """書式の飾りの結合の線（``FORMATTING_COMBINING_MARKS``）を除く。ほかの文字は変えない。"""
    return text.translate(_FORMATTING_MARK_TABLE)


def is_derived_content(content_kind: object, metadata: Mapping[str, object]) -> bool:
    """派生の要素・chunk か（content_kind か印のどちらかで判定する。#2590）。"""
    if content_kind in DERIVED_CONTENT_KINDS:
        return True
    return any(metadata.get(key) for key in DERIVED_FLAG_KEYS)


MARKDOWN_HEADING = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+)$")
TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$")
FENCED_CODE_START = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<language>[\w.+#-]*)\s*$")
FENCED_CODE_ENDS = {
    "`": re.compile(r"^\s*`{3,}\s*$"),
    "~": re.compile(r"^\s*~{3,}\s*$"),
}
INLINE_DISPLAY_MATH = re.compile(r"^\s*\$\$(?P<body>.+?)\$\$\s*$")
INLINE_BRACKET_MATH = re.compile(r"^\s*\\\[(?P<body>.+?)\\\]\s*$")
DISPLAY_MATH_START = re.compile(r"^\s*(?:\$\$|\\\[)")
DISPLAY_MATH_ENDS = {
    "$$": re.compile(r"^.*\$\$\s*$"),
    "\\[": re.compile(r"^.*\\\]\s*$"),
}
LATEX_EQUATION_ENV_NAMES = r"(?:equation|align|gather|multline)\*?"
LATEX_EQUATION_BEGIN = re.compile(rf"^\s*\\begin\{{(?P<env>{LATEX_EQUATION_ENV_NAMES})\}}")
LATEX_EQUATION_END = re.compile(rf"^.*\\end\{{(?P<env>{LATEX_EQUATION_ENV_NAMES})\}}\s*$")
_marker_regexes: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {}


def _marker_regexes_now() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """ページの印の行と箇条書きの行（ページの語・箇条書きの記号・番号の閉じと括弧は言語資源。#2181）。"""
    pack = current_pack()
    cached = _marker_regexes.get(pack.hash)
    if cached is None:
        m = marker_patterns(pack)
        cached = (
            re.compile(
                rf"^\s*(?:-{{2,}}\s*)?{m.page_words}\s*(?P<page>\d+)\s*(?:-{{2,}})?\s*$",
                re.IGNORECASE,
            ),
            re.compile(
                rf"^\s*(?:{m.bullet}|\d+{m.number_closer}|{m.number_open}\d+{m.number_close})\s+"
            ),
        )
        if len(_marker_regexes) >= 16:
            _marker_regexes.clear()
        _marker_regexes[pack.hash] = cached
    return cached


def page_marker() -> re.Pattern[str]:
    """ページの印だけの行（「--- page 3 ---」「ページ 3」）。"""
    return _marker_regexes_now()[0]


def bullet_line() -> re.Pattern[str]:
    """箇条書きの行（記号・「1.」「(1)」で始まる）。"""
    return _marker_regexes_now()[1]


ELEMENT_KIND_ALIASES = {
    "paragraph": "text",
    "body": "text",
    "body_text": "text",
    "caption": "figure_caption",
    "figcaption": "figure_caption",
    "heading": "title",
    "header": "header",
    "footer": "footer",
    "list_item": "list",
    "bullet": "list",
    "bullet_list": "list",
    "ordered_list": "list",
    "table_chunk": "table",
    "tsr": "table",
    "formula": "equation",
    "image": "figure",
    "picture": "figure",
}
# 解析エンジンの出力の種類の別名（英語の名前は解析エンジンの出力の形）。日本語の種類の語は言語資源の
# ``element_kind_words``（#2181）。

SEARCHABLE_ELEMENT_KINDS = {
    "title",
    "text",
    "list",
    "table",
    "table_caption",
    "figure",
    "figure_caption",
    "reference",
    "equation",
    "code",
    "other",
}


def section_path_items(items: Iterable[str]) -> list[str]:
    """章節名の並びの正規化（書式の飾りの結合の線を除き、空白を詰め、1 項目 80 文字まで）。

    ``DocumentElement.section_path`` の検証と、検証を通らない ``model_copy`` で章節を
    作り直す所が共有する。切った後の末尾の空白も除き、保存と読み戻しで同じ値にする。
    空になった項目は落とす。
    """
    cleaned = (
        re.sub(r"\s+", " ", strip_formatting_marks(item)).strip()[:80].strip() for item in items
    )
    return [item for item in cleaned if item]


class DocumentElement(BaseModel):
    """ページ・見出し・表などを保持する文書構造要素。"""

    kind: str = "text"
    text: str = ""
    order: int = Field(default=0, ge=0)
    element_id: str | None = Field(default=None, max_length=128)
    parent_id: str | None = Field(default=None, max_length=128)
    content_kind: str | None = Field(default=None, max_length=40)
    source_parser: str | None = Field(default=None, max_length=80)
    page_number: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = None
    section_path: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, ExtractionMetadataValue] = Field(default_factory=dict)

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value: object) -> str:
        """VLM/parser ごとの kind 表記を低 cardinality に寄せる。"""
        if value is None:
            return "text"
        normalized = re.sub(r"[\s-]+", "_", str(value).strip().casefold())
        if not normalized:
            return "text"
        aliases = {**ELEMENT_KIND_ALIASES, **marker_patterns().element_kinds}
        return aliases.get(normalized, normalized)[:40]

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """要素本文の改行と書式の飾りの結合の線だけ正規化し、表・リストの構造は残す。"""
        return strip_formatting_marks(value.replace("\r\n", "\n").replace("\r", "\n")).strip()

    @field_validator("element_id", "parent_id", "content_kind", "source_parser")
    @classmethod
    def normalize_optional_label(cls, value: str | None) -> str | None:
        """識別子・分類ラベルは空文字を None に寄せる。"""
        if value is None:
            return None
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned or None

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: object) -> list[float] | None:
        """bbox は x1,y1,x2,y2 の 4 値へ正規化して保存する。"""
        return normalize_bbox(value)

    @field_validator("section_path", mode="before")
    @classmethod
    def normalize_section_path(cls, value: object) -> list[str]:
        """str/None も許容し、空の章節名を落として長すぎる値を切り詰める。

        VLM が ``/章/節`` のようなスラッシュ区切り文字列で返す場合があるため、
        ``/`` で分割してから正規化する。
        """
        if value is None:
            items: list[object] = []
        elif isinstance(value, str):
            items = list(value.split("/"))
        elif isinstance(value, list | tuple):
            items = list(value)
        else:
            items = [value]
        return section_path_items(str(item) for item in items)

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(
        cls,
        value: dict[str, ExtractionMetadataValue],
    ) -> dict[str, ExtractionMetadataValue]:
        """metadata は JSON scalar だけを残す。"""
        normalized: dict[str, ExtractionMetadataValue] = {}
        for key, item in value.items():
            clean_key = str(key).strip()[:80]
            if clean_key:
                normalized[clean_key] = item
        return normalized

    def to_payload(self) -> dict[str, object]:
        """DocumentDetail.extraction に入れる JSON 互換 payload を返す。"""
        return self.model_dump(exclude_none=True)


class IngestionQualityReport(BaseModel):
    """取込後に評価へ渡す非機密な文書品質レポート。"""

    parser_profile: str = "enterprise_ai_generic"
    parser_backend: str = "enterprise_ai"
    parser_version: str = "v1"
    fallback_used: bool = False
    risk_level: str = "low"
    page_count: int = 0
    page_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    table_count: int = 0
    figure_count: int = 0
    formula_count: int = 0
    element_count: int = 0
    low_confidence_count: int = 0
    failed_segment_count: int = 0
    long_document: bool = False
    quality_warnings: list[str] = Field(default_factory=list)

    @field_validator("risk_level")
    @classmethod
    def normalize_risk_level(cls, value: str) -> str:
        """評価 UI で扱う低 cardinality の risk level に寄せる。"""
        normalized = value.strip().casefold()
        return normalized if normalized in {"low", "medium", "high"} else "low"


class ExtractionPage(BaseModel):
    """原本上のページ/スライド/シート単位 metadata。"""

    page_number: int = Field(default=1, ge=1)
    label: str | None = Field(default=None, max_length=128)
    width: float | None = Field(default=None, gt=0.0)
    height: float | None = Field(default=None, gt=0.0)
    rotation: int | None = None
    element_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, ExtractionMetadataValue] = Field(default_factory=dict)


class ExtractionTableCell(BaseModel):
    """表セル metadata。"""

    row: int = Field(ge=0)
    col: int = Field(ge=0)
    text: str = ""
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    bbox: list[float] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, ExtractionMetadataValue] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """セルの本文から書式の飾りの結合の線を除く（要素の本文と同じ規則）。"""
        return strip_formatting_marks(value)

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: object) -> list[float] | None:
        """cell bbox も element と同じ座標方言を許容して保存する。"""
        return normalize_bbox(value)

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(
        cls,
        value: dict[str, ExtractionMetadataValue],
    ) -> dict[str, ExtractionMetadataValue]:
        """cell-level lineage metadata を低 cardinality key で保持する。"""
        normalized: dict[str, ExtractionMetadataValue] = {}
        for key, item in value.items():
            clean_key = str(key).strip()[:80]
            if clean_key:
                normalized[clean_key] = item
        return normalized


class ExtractionTable(BaseModel):
    """表全体の構造 metadata。"""

    table_id: str
    element_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    caption: str | None = None
    cells: list[ExtractionTableCell] = Field(default_factory=list)
    metadata: dict[str, ExtractionMetadataValue] = Field(default_factory=dict)

    @field_validator("caption")
    @classmethod
    def normalize_caption(cls, value: str | None) -> str | None:
        """表の題から書式の飾りの結合の線を除く（要素の本文と同じ規則）。"""
        return strip_formatting_marks(value) if value is not None else None


class ExtractionAsset(BaseModel):
    """図・画像・添付などの派生 asset metadata。"""

    asset_id: str
    kind: str = "figure"
    object_path: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = None
    alt_text: str | None = None
    # Model API VLM/LLM が生成した図表の要約（Knowhere の multimodal 要約 由来）。
    summary: str | None = None
    metadata: dict[str, ExtractionMetadataValue] = Field(default_factory=dict)

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: object) -> list[float] | None:
        """asset bbox も preview / citation 用に同じ規則で正規化する。"""
        return normalize_bbox(value)


class DocumentNavigationNode(BaseModel):
    """文書の章節階層を再構築した navigation tree のノード（Knowhere 由来）。

    section_path/parent_id/reading order から決定論的に組み、各 node に要素・ページ範囲・
    （任意で）LLM 要約を持たせて progressive disclosure / Navigate retrieval に使う。
    """

    section_id: str
    title: str
    section_path: list[str] = Field(default_factory=list)
    depth: int = Field(default=0, ge=0)
    parent_section_id: str | None = None
    child_section_ids: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    summary: str | None = None

    # 要素の本文・章節の道と同じ規則で書式の飾りの結合の線を除く。読むたびに当たるので、
    # 線を除く前に保存した木（``parser_navigation`` の artifact など）も再解析なしで表示が直る。
    # ``section_id`` は保存時の値のまま（人の修正・章節と chunk の対応が参照する。#3434）。
    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return strip_formatting_marks(value).strip()

    @field_validator("section_path")
    @classmethod
    def normalize_section_path(cls, value: list[str]) -> list[str]:
        cleaned = (strip_formatting_marks(item).strip() for item in value)
        return [item for item in cleaned if item]

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str | None) -> str | None:
        return strip_formatting_marks(value).strip() if value is not None else None


class ExtractionField(BaseModel):
    """schema 駆動で抽出した named field/entity（PoweRAG の LangExtract 抽出 由来）。

    請求書番号・日付・当事者などを typed record として保持し、citation/フィルタに使う。
    """

    name: str = Field(max_length=120)
    value: str = Field(max_length=4000)
    value_type: Literal["string", "number", "date", "bool"] = "string"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_element_id: str | None = Field(default=None, max_length=128)
    page_number: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = None

    @field_validator("name", "value")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: object) -> list[float] | None:
        """field bbox も preview / citation 用に同じ規則で正規化する。"""
        return normalize_bbox(value)


class StructuredExtraction(BaseModel):
    """VLM/LLM 出力を検証して保存するための正規化形。"""

    raw_text: str = ""
    # 既定の文書の種類の表示は言語資源（#2181）。
    document_type: str = Field(
        default_factory=lambda: current_pack().text("labels", "default_document_type")
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    elements: list[DocumentElement] = Field(default_factory=list)
    pages: list[ExtractionPage] = Field(default_factory=list)
    tables: list[ExtractionTable] = Field(default_factory=list)
    assets: list[ExtractionAsset] = Field(default_factory=list)
    navigation: list[DocumentNavigationNode] = Field(default_factory=list)
    fields: list[ExtractionField] = Field(default_factory=list)
    parser_artifacts: dict[str, ExtractionArtifactValue] = Field(default_factory=dict)
    quality_report: IngestionQualityReport | None = None

    @model_validator(mode="after")
    def normalize_structure(self) -> Self:
        """raw_text と elements のどちらか片方だけでも検索可能な形へ補完する。"""
        self.raw_text = strip_formatting_marks(
            self.raw_text.replace("\r\n", "\n").replace("\r", "\n")
        ).strip()
        self.elements = normalize_document_elements(self.elements, fallback_text=self.raw_text)
        if not self.pages:
            self.pages = infer_extraction_pages(self.elements)
        if not self.raw_text and self.elements:
            self.raw_text = "\n".join(
                element.text
                for element in self.elements
                if element.kind in SEARCHABLE_ELEMENT_KINDS
            ).strip()
        return self

    def to_document_payload(self) -> dict[str, object]:
        """DocumentDetail.extraction に格納する JSON 互換 dict を返す。"""
        from rag_parser_core.structured_records import record_boundary_plain_text

        payload: dict[str, object] = {
            "raw_text": self.raw_text,
            "display_text": record_boundary_plain_text(self),
            "document_type": self.document_type,
            "confidence": self.confidence,
            "warnings": self.warnings,
            "elements": [element.to_payload() for element in self.elements],
            "pages": [page.model_dump(exclude_none=True) for page in self.pages],
            "tables": [table.model_dump(exclude_none=True) for table in self.tables],
            "assets": [asset.model_dump(exclude_none=True) for asset in self.assets],
            "navigation": [node.model_dump(exclude_none=True) for node in self.navigation],
            "parser_artifacts": self.parser_artifacts,
            "quality_report": (
                self.quality_report.model_dump(exclude_none=True)
                if self.quality_report is not None
                else None
            ),
        }
        # fields は抽出された場合のみ含める（既存の field 抽出なし payload と後方互換）。
        if self.fields:
            payload["fields"] = [field.model_dump(exclude_none=True) for field in self.fields]
        return payload


def normalize_document_elements(
    elements: list[DocumentElement],
    *,
    fallback_text: str,
) -> list[DocumentElement]:
    """VLM が返した要素列を正規化し、なければ raw_text から軽量推定する。"""
    if not elements:
        return infer_document_elements(fallback_text)

    indexed = list(enumerate(element for element in elements if element.text.strip()))
    if any(element.order > 0 for _, element in indexed):
        indexed.sort(key=lambda item: (item[1].order, item[0]))

    normalized: list[DocumentElement] = []
    path_by_level: dict[int, str] = {}
    current_path: list[str] = []
    for order, (_, element) in enumerate(indexed):
        metadata = dict(element.metadata)
        kind = element.kind
        element_id = element.element_id or _metadata_text(metadata.get("element_id"))
        content_kind = element.content_kind or _content_kind_for_element_kind(kind)
        derived = is_derived_content(content_kind, metadata)
        # 派生の要素（「章節名: 要約」など）は見出しに見えても見出しとして読み直さない(#2590)。
        if not derived and (heading := _parse_heading(element.text)):
            level, title = heading
            kind = "title"
            content_kind = "text"
            path_by_level = {
                existing_level: existing_title
                for existing_level, existing_title in path_by_level.items()
                if existing_level < level
            }
            path_by_level[level] = title
            current_path = [path_by_level[key] for key in sorted(path_by_level)]
            metadata.setdefault("section_level", level)
        elif not derived and element.section_path:
            current_path = element.section_path

        normalized.append(
            element.model_copy(
                update={
                    "kind": kind,
                    "order": order,
                    "element_id": element_id or f"el-{order:04d}",
                    "content_kind": content_kind,
                    "section_path": element.section_path
                    if derived
                    else element.section_path or current_path,
                    "metadata": metadata,
                }
            )
        )
    return normalized


def infer_extraction_pages(elements: list[DocumentElement]) -> list[ExtractionPage]:
    """element の page_number から軽量 page metadata を作る。"""
    by_page: dict[int, list[str]] = {}
    for element in elements:
        page_number = element.page_number
        if page_number is None:
            continue
        element_id = element.element_id or _metadata_text(element.metadata.get("element_id"))
        if element_id:
            by_page.setdefault(page_number, []).append(element_id)
        else:
            by_page.setdefault(page_number, [])
    return [
        ExtractionPage(
            page_number=page_number,
            label=f"page {page_number}",
            element_ids=element_ids,
        )
        for page_number, element_ids in sorted(by_page.items())
    ]


def _metadata_text(value: object) -> str | None:
    """metadata scalar を短い文字列として読む。"""
    if isinstance(value, str | int):
        cleaned = str(value).strip()
        return cleaned[:128] if cleaned else None
    return None


def _content_kind_for_element_kind(kind: str) -> str:
    """element kind から retrieval/filter 用の content_kind を推定する。"""
    if kind in {"table", "table_caption"}:
        return "table"
    if kind in {"figure", "figure_caption"}:
        return "figure"
    if kind == "equation":
        return "equation"
    if kind == "code":
        return "code"
    if kind == "list":
        return "list"
    return "text"


def normalize_bbox(value: object) -> list[float] | None:
    """bbox を x1,y1,x2,y2 の 4 値へ正規化して保存する。"""
    if value is None:
        return None
    coords = _bbox_coordinates(value)
    if coords is None or not coords or not all(math.isfinite(item) for item in coords):
        return None
    if len(coords) == 4:
        return coords
    if len(coords) >= 6 and len(coords) % 2 == 0:
        xs = coords[0::2]
        ys = coords[1::2]
        return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _bbox_coordinates(value: object) -> list[float] | None:
    """VLM/parser ごとの bbox 表現を数値列へ寄せる。"""
    if isinstance(value, Mapping):
        return _bbox_coordinates_from_mapping(value)
    if not _is_bbox_sequence(value):
        return None
    items = list(value)
    numeric_coords: list[float] = []
    for item in items:
        number = _number(item)
        if number is None:
            break
        numeric_coords.append(number)
    else:
        return numeric_coords
    point_coords: list[float] = []
    for item in items:
        point = _bbox_point(item)
        if point is None:
            return None
        point_coords.extend(point)
    return point_coords


def _bbox_coordinates_from_mapping(value: Mapping[object, object]) -> list[float] | None:
    """dict 形式の bbox / polygon / point list を数値列へ変換する。"""
    lowered = {str(key).strip().casefold(): item for key, item in value.items()}
    for key in ("bbox", "bounding_box", "boundingbox", "polygon", "points", "vertices"):
        if key in lowered:
            return _bbox_coordinates(lowered[key])
    for keys in (
        ("x", "y", "width", "height"),
        ("x", "y", "w", "h"),
        ("left", "top", "width", "height"),
    ):
        xywh = _bbox_mapping_values(lowered, keys)
        if xywh is not None:
            x, y, width, height = xywh
            return [x, y, x + width, y + height]
    for keys in (
        ("x1", "y1", "x2", "y2"),
        ("xmin", "ymin", "xmax", "ymax"),
        ("x_min", "y_min", "x_max", "y_max"),
        ("left", "top", "right", "bottom"),
        ("l", "t", "r", "b"),
    ):
        xyxy = _bbox_mapping_values(lowered, keys)
        if xyxy is not None:
            return xyxy
    return _bbox_point(lowered)


def _bbox_mapping_values(
    lowered: Mapping[str, object],
    keys: tuple[str, str, str, str],
) -> list[float] | None:
    if not all(key in lowered for key in keys):
        return None
    coords: list[float] = []
    for key in keys:
        coord = _number(lowered[key])
        if coord is None:
            return None
        coords.append(coord)
    return coords


def _bbox_point(value: object) -> list[float] | None:
    """1 点を x,y の 2 値として取り出す。"""
    if isinstance(value, Mapping):
        lowered = {str(key).strip().casefold(): item for key, item in value.items()}
        if "x" in lowered and "y" in lowered:
            x = _number(lowered["x"])
            y = _number(lowered["y"])
            if x is not None and y is not None:
                return [x, y]
        return None
    if not _is_bbox_sequence(value):
        return None
    items = list(value)
    if len(items) < 2:
        return None
    x = _number(items[0])
    y = _number(items[1])
    if x is None or y is None:
        return None
    return [x, y]


def _is_bbox_sequence(value: object) -> TypeGuard[Sequence[object]]:
    """文字列以外の sequence かどうかを判定する。"""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _is_number(value: object) -> TypeGuard[int | float]:
    """bool を除外した数値判定。"""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _number(value: object) -> float | None:
    """bbox 用の数値へ変換できなければ None を返す。"""
    if isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    if isinstance(value, int | float) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def infer_document_elements(text: str) -> list[DocumentElement]:
    """raw_text だけの抽出結果から Docling/Unstructured 風の block 列を推定する。"""
    source = text.replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        return []

    elements: list[DocumentElement] = []
    path_by_level: dict[int, str] = {}
    current_page = 1
    offset = 0
    pending_kind: str | None = None
    pending_lines: list[str] = []
    pending_start = 0
    pending_page = current_page
    active_block_kind: str | None = None
    active_block_lines: list[str] = []
    active_block_start = 0
    active_block_page = current_page
    active_block_section_path: list[str] = []
    active_block_metadata: dict[str, ExtractionMetadataValue] = {}
    active_code_fence: str | None = None
    active_equation_end: str | None = None

    def flush_pending(end_offset: int) -> None:
        nonlocal pending_kind, pending_lines, pending_start, pending_page
        if not pending_kind or not pending_lines:
            return
        _append_element(
            elements,
            kind=pending_kind,
            text="\n".join(pending_lines),
            page_number=pending_page,
            section_path=[path_by_level[key] for key in sorted(path_by_level)],
            raw_start=pending_start,
            raw_end=end_offset,
        )
        pending_kind = None
        pending_lines = []

    def start_block(
        *,
        kind: str,
        line: str,
        start_offset: int,
        page_number: int,
        extra_metadata: dict[str, ExtractionMetadataValue],
        code_fence: str | None = None,
        equation_end: str | None = None,
    ) -> None:
        nonlocal active_block_kind, active_block_lines, active_block_start
        nonlocal active_block_page, active_block_section_path, active_block_metadata
        nonlocal active_code_fence, active_equation_end
        active_block_kind = kind
        active_block_lines = [line]
        active_block_start = start_offset
        active_block_page = page_number
        active_block_section_path = [path_by_level[key] for key in sorted(path_by_level)]
        active_block_metadata = extra_metadata
        active_code_fence = code_fence
        active_equation_end = equation_end

    def flush_active_block(end_offset: int) -> None:
        nonlocal active_block_kind, active_block_lines, active_block_start
        nonlocal active_block_page, active_block_section_path, active_block_metadata
        nonlocal active_code_fence, active_equation_end
        if not active_block_kind or not active_block_lines:
            return
        _append_element(
            elements,
            kind=active_block_kind,
            text="\n".join(active_block_lines),
            page_number=active_block_page,
            section_path=active_block_section_path,
            raw_start=active_block_start,
            raw_end=end_offset,
            extra_metadata=active_block_metadata,
        )
        active_block_kind = None
        active_block_lines = []
        active_block_metadata = {}
        active_code_fence = None
        active_equation_end = None

    for raw_line in source.splitlines(keepends=True):
        line_start = offset
        offset += len(raw_line)
        line = raw_line.removesuffix("\n")
        stripped = raw_line.strip()
        if active_block_kind is not None:
            active_block_lines.append(line)
            if _is_block_end(
                stripped,
                kind=active_block_kind,
                code_fence=active_code_fence,
                equation_end=active_equation_end,
            ):
                flush_active_block(offset)
            continue

        if not stripped:
            flush_pending(line_start)
            continue

        if code_start := FENCED_CODE_START.match(stripped):
            flush_pending(line_start)
            fence = code_start.group("fence")
            language = code_start.group("language") or None
            start_block(
                kind="code",
                line=line,
                start_offset=line_start,
                page_number=current_page,
                code_fence=fence[0],
                extra_metadata={
                    "block_delimiter": "fenced_code",
                    "code_language": language,
                },
            )
            continue

        if equation_metadata := _single_line_equation_metadata(stripped):
            flush_pending(line_start)
            _append_element(
                elements,
                kind="equation",
                text=line,
                page_number=current_page,
                section_path=[path_by_level[key] for key in sorted(path_by_level)],
                raw_start=line_start,
                raw_end=offset,
                extra_metadata=equation_metadata,
            )
            continue

        if equation_start := _equation_block_start(stripped):
            flush_pending(line_start)
            equation_end, equation_metadata = equation_start
            start_block(
                kind="equation",
                line=line,
                start_offset=line_start,
                page_number=current_page,
                equation_end=equation_end,
                extra_metadata=equation_metadata,
            )
            continue

        if page_match := page_marker().match(stripped):
            flush_pending(line_start)
            current_page = max(1, int(page_match.group("page")))
            continue

        if heading := _parse_heading(stripped):
            flush_pending(line_start)
            level, title = heading
            path_by_level = {
                existing_level: existing_title
                for existing_level, existing_title in path_by_level.items()
                if existing_level < level
            }
            path_by_level[level] = title
            _append_element(
                elements,
                kind="title",
                text=stripped,
                page_number=current_page,
                section_path=[path_by_level[key] for key in sorted(path_by_level)],
                raw_start=line_start,
                raw_end=offset,
                extra_metadata={"section_level": level},
            )
            continue

        line_kind = _line_kind(stripped)
        if pending_kind is not None and (pending_kind != line_kind or pending_page != current_page):
            flush_pending(line_start)
        if pending_kind is None:
            pending_kind = line_kind
            pending_start = line_start
            pending_page = current_page
        pending_lines.append(stripped)

    flush_active_block(offset)
    flush_pending(offset)
    return _annotate_inferred_table_continuity(elements)


def _annotate_inferred_table_continuity(
    elements: list[DocumentElement],
) -> list[DocumentElement]:
    """page marker をまたいで同じ表頭が続く inferred table に lineage を付ける。"""
    groups: dict[tuple[str, ...], list[tuple[int, DocumentElement]]] = {}
    for index, element in enumerate(elements):
        if element.kind != "table":
            continue
        signature = _inferred_table_signature(element.text)
        if signature:
            groups.setdefault(signature, []).append((index, element))

    replacements: dict[int, DocumentElement] = {}
    table_sequence = 0
    for signature, indexed_elements in groups.items():
        pages = sorted(
            {
                element.page_number
                for _index, element in indexed_elements
                if element.page_number is not None
            }
        )
        if len(pages) <= 1:
            continue
        table_sequence += 1
        table_id = f"inferred-table-{table_sequence:04d}"
        total_body_rows = sum(
            _inferred_table_body_row_count(element.text) for _, element in indexed_elements
        )
        column_count = len(signature)
        row_offset = 0
        continuation_count = len(indexed_elements)
        for continuation_index, (index, element) in enumerate(indexed_elements, start=1):
            body_rows = _inferred_table_body_row_count(element.text)
            metadata = {
                **element.metadata,
                "table_id": table_id,
                "table_header_signature": "|".join(signature),
                "table_cross_page": True,
                "table_page_start": pages[0],
                "table_page_end": pages[-1],
                "table_page_count": len(pages),
                "table_continuation_index": continuation_index,
                "table_continuation_count": continuation_count,
                "table_data_row_offset": row_offset,
                "row_count": total_body_rows + 1,
                "column_count": column_count,
            }
            replacements[index] = element.model_copy(update={"metadata": metadata})
            row_offset += body_rows
    if not replacements:
        return elements
    return [replacements.get(index, element) for index, element in enumerate(elements)]


def _inferred_table_signature(text: str) -> tuple[str, ...]:
    """Markdown table の表頭を signature 化する。"""
    lines = [line.strip() for line in text.splitlines() if TABLE_LINE.match(line.strip())]
    if not lines:
        return ()
    cells = _markdown_table_cells(lines[0])
    return tuple(cell for cell in cells if cell)


def _inferred_table_body_row_count(text: str) -> int:
    """Markdown table の body row 数を返す。"""
    lines = [line.strip() for line in text.splitlines() if TABLE_LINE.match(line.strip())]
    if not lines:
        return 0
    body_lines = lines[1:]
    if body_lines and _is_markdown_separator_line(body_lines[0]):
        body_lines = body_lines[1:]
    return len(body_lines)


def _is_markdown_separator_line(line: str) -> bool:
    """Markdown table separator 行かどうかを判定する。"""
    cells = _markdown_table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _markdown_table_cells(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]


def _append_element(
    elements: list[DocumentElement],
    *,
    kind: str,
    text: str,
    page_number: int,
    section_path: list[str],
    raw_start: int,
    raw_end: int,
    extra_metadata: dict[str, ExtractionMetadataValue] | None = None,
) -> None:
    """推定 element を順序と raw offset 付きで追加する。"""
    metadata: dict[str, ExtractionMetadataValue] = {
        "raw_start": raw_start,
        "raw_end": raw_end,
        **(extra_metadata or {}),
    }
    elements.append(
        DocumentElement(
            kind=kind,
            text=text,
            order=len(elements),
            element_id=f"el-{len(elements):04d}",
            content_kind=_content_kind_for_element_kind(kind),
            page_number=page_number,
            section_path=section_path,
            metadata=metadata,
        )
    )


def _line_kind(line: str) -> str:
    """1 行から element kind を推定する。"""
    if TABLE_LINE.match(line):
        return "table"
    if bullet_line().match(line):
        return "list"
    markers = marker_patterns()
    folded = line.casefold()
    if folded.startswith(tuple(f"{word.casefold()}:" for word in markers.header_words)):
        return "header"
    if folded.startswith(tuple(f"{word.casefold()}:" for word in markers.footer_words)):
        return "footer"
    return "text"


def _is_block_end(
    line: str,
    *,
    kind: str,
    code_fence: str | None,
    equation_end: str | None,
) -> bool:
    """active block の終了行かどうかを判定する。"""
    if kind == "code" and code_fence:
        return bool(FENCED_CODE_ENDS[code_fence].match(line))
    if kind == "equation" and equation_end:
        if equation_end.startswith("latex:"):
            env_name = equation_end.removeprefix("latex:")
            match = LATEX_EQUATION_END.match(line)
            return bool(match and match.group("env") == env_name)
        return bool(DISPLAY_MATH_ENDS[equation_end].match(line))
    return False


def _single_line_equation_metadata(
    line: str,
) -> dict[str, ExtractionMetadataValue] | None:
    """1 行で閉じる display math / LaTeX equation を識別する。"""
    if INLINE_DISPLAY_MATH.match(line):
        return {"block_delimiter": "display_math", "equation_delimiter": "$$"}
    if INLINE_BRACKET_MATH.match(line):
        return {"block_delimiter": "display_math", "equation_delimiter": "\\[\\]"}
    begin = LATEX_EQUATION_BEGIN.match(line)
    end = LATEX_EQUATION_END.match(line)
    if begin and end and begin.group("env") == end.group("env"):
        return {
            "block_delimiter": "latex_environment",
            "equation_environment": begin.group("env"),
        }
    return None


def _equation_block_start(
    line: str,
) -> tuple[str, dict[str, ExtractionMetadataValue]] | None:
    """複数行 display math / LaTeX equation の開始行を識別する。"""
    if not DISPLAY_MATH_START.match(line):
        begin = LATEX_EQUATION_BEGIN.match(line)
        if not begin:
            return None
        env_name = begin.group("env")
        return (
            f"latex:{env_name}",
            {
                "block_delimiter": "latex_environment",
                "equation_environment": env_name,
            },
        )
    delimiter = "\\[" if line.lstrip().startswith("\\[") else "$$"
    return (
        delimiter,
        {
            "block_delimiter": "display_math",
            "equation_delimiter": "\\[\\]" if delimiter == "\\[" else "$$",
        },
    )


def _parse_heading(line: str) -> tuple[int, str] | None:
    """Markdown / 番号付き / 日本語章節見出しを推定する。"""
    if not line or len(line) > 120:
        return None
    if match := MARKDOWN_HEADING.match(line):
        return len(match.group("marks")), _clean_heading_title(match.group("title"))
    if match := numbered_heading().match(line):
        prefix = match.group("prefix")
        title = _clean_heading_title(match.group("title"))
        return heading_level_from_prefix(prefix), title
    return None


_numbered_headings: dict[str, re.Pattern[str]] = {}


def numbered_heading() -> re.Pattern[str]:
    """番号の付いた見出しの行（「1.2 概要」「第 4 章 概要」「(3) 概要」）。

    「第 N 〈編・部・章・節〉」の書き方は言語資源（#2181）から作る。
    """
    text = text_patterns()
    key = text.number + text.prefix + text.part_units + text.chapter_units + text.section_units
    pattern = _numbered_headings.get(key)
    if pattern is None:
        units = "|".join((text.part_units, text.chapter_units, text.section_units))
        pattern = re.compile(
            rf"^(?P<prefix>(?:\d+(?:\.\d+)*|{numbered(text, f'(?:{units})')}|"
            r"[（(]?\d+[）)]))[\s:：.．、-]+(?P<title>.+)$"
        )
        _numbered_headings.clear()
        _numbered_headings[key] = pattern
    return pattern


def heading_level_from_prefix(prefix: str) -> int:
    """番号形式から章節 level を推定する（編・部・章は 1、節は 2。単位は言語資源。#2181）。"""
    text = text_patterns()
    if re.fullmatch(numbered(text, f"(?:{text.part_units}|{text.chapter_units})"), prefix):
        return 1
    if re.fullmatch(numbered(text, text.section_units), prefix):
        return 2
    dotted_depth = prefix.count(".") + 1
    return min(6, max(1, dotted_depth))


def _clean_heading_title(title: str) -> str:
    """metadata に入れる見出し文字列を短く正規化する。"""
    return re.sub(r"\s+", " ", title).strip().strip("#")[:80]
