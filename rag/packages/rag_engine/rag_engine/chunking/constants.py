"""チャンキングの定数・設定と結果のデータモデル・汎用 helper。他の submodule に依存しない。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any, Literal

from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import parse_roman, text_patterns

CHUNK_SCHEMA_VERSION = 3
CHUNK_STRATEGY = "small_to_big_parent_child"
CHUNKS_DIRECTORY = "chunks"
LATEST_CHUNKS_FILE = "latest.json"
CHILD_CHUNK_LEVEL = "child"
PARENT_CHUNK_LEVEL = "parent"

# 親は番号付き機能見出しの単位をまたがない（#660）。1 機能は画面キャプチャの Vision 説明（1 枚 1,000〜3,000 字）を
# 含むため、親 6,000 字・3 ページ・12 child で「1 機能 = 1 親」に近づける (#665)。
DEFAULT_CHILD_TARGET_CHARS = 1000
# 表は列見出し・関連見出しが各行グループに繰り返し付き、行単位で意味が閉じるため本文より大きな単位に保つ。
# この文字数を超える表だけ行グループに分ける (#894)。
DEFAULT_TABLE_CHILD_TARGET_CHARS = 3000
DEFAULT_PARENT_TARGET_CHARS = 6000
DEFAULT_PARENT_MAX_PAGES = 3
DEFAULT_PARENT_MAX_CHILDREN = 12
# 広めに取得した候補から20 childを選び、前後各3件を補います。最終contextは別途件数・文字数で制限します。
DEFAULT_RETRIEVAL_TOP_K = 20
DEFAULT_NEIGHBOR_CHILD_COUNT = 3
DEFAULT_CONTEXTUAL_SEARCH_TEXT_ENABLED = True
DEFAULT_SEARCH_TEXT_CONTEXT_MAX_CHARS = 700
DEFAULT_CHILD_SEARCH_TEXT_MAX_CHARS = 2200
# 頁番号のフッターから論理頁を決めるとき（#2289）、物理頁に対して差が一定の並びに乗る頁番号だけを使う。
# 並びとして使う最小の頁数（文書の頁数がこれより少なければ文書の頁数）と、隣り合う頁番号の間で許す差のずれ
# （0 = 差が一定。1 以上は番号の無い挿入頁などで差が変わる文書のため）。
DEFAULT_PAGE_NUMBER_MIN_RUN_PAGES = 2
DEFAULT_PAGE_NUMBER_OFFSET_TOLERANCE = 0

CHILD_TARGET_CHARS_RANGE = (300, 1600, 50)
TABLE_CHILD_TARGET_CHARS_RANGE = (300, 8000, 100)
# 上限は既定値ではなく、1 機能が既定より大きい文書を検証するための余地（#779）。
PARENT_TARGET_CHARS_RANGE = (1200, 10000, 100)
PARENT_MAX_PAGES_RANGE = (1, 5, 1)
PARENT_MAX_CHILDREN_RANGE = (3, 20, 1)
RETRIEVAL_TOP_K_RANGE = (3, 50, 1)
NEIGHBOR_CHILD_COUNT_RANGE = (0, 20, 1)
SEARCH_TEXT_CONTEXT_CHARS_RANGE = (200, 1600, 50)
CHILD_SEARCH_TEXT_CHARS_RANGE = (1000, 4000, 100)
PAGE_NUMBER_MIN_RUN_PAGES_RANGE = (1, 10, 1)
PAGE_NUMBER_OFFSET_TOLERANCE_RANGE = (0, 5, 1)
SEARCH_TEXT_SCHEMA_VERSION = 9
CHUNK_METADATA_SCHEMA_VERSION = 4
SOURCE_TABLE_CROP_RELATIONSHIP = "source_table_crop"

ATOMIC_CATEGORIES = {"Picture", "Table"}
METADATA_ONLY_CATEGORIES = {"Page-header", "Page-footer"}
SECTION_CATEGORIES = {"Title", "Section-header"}
ANNOTATION_CATEGORIES = {"Caption", "Footnote"}
CAPTION_TARGET_CATEGORIES = {"Picture", "Table"}
FOOTNOTE_TARGET_CATEGORIES = {"Picture", "Table", "Text", "List-item", "Formula"}
FORM_VISUAL_RAW_TYPES = {"complex-layout", "complex_layout", "form", "form-region", "form_region", "key_value_region"}
FORM_LIKE_RAW_TYPES = {
    "checkbox",
    *FORM_VISUAL_RAW_TYPES,
    "form_field",
    "key_value",
    "key_value_item",
    "key_value_region",
    "key-value",
    "key-value-item",
    "radio",
    "selection_element",
    "selection_mark",
    "selection-mark",
}
FORM_LIKE_CATEGORIES = {"Form", "Form-field", "Key-value", "Selection-element", "Selection-mark"}
FORM_FIELD_RAW_KEYS = (
    "key",
    "value",
    "field_name",
    "field_value",
    "selection_status",
    "selected",
    "checked",
)
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{6,64}\Z")
CHUNK_TABLE_HEADERS = [
    "chunk_id",
    "chunk_level",
    "chunk_seq",
    "parent_chunk_id",
    "child_count",
    "pages",
    "seq_ranges",
    "categories",
    "char_count",
    "atomic",
    "text_preview",
    "metadata_json",
]


@dataclass(frozen=True)
class ChunkingConfig:
    """Small-to-Big チャンキングのサイズ上限と近傍条件を保持します。"""

    child_target_chars: int = DEFAULT_CHILD_TARGET_CHARS
    # 表の行グループ分割の閾値と各グループの目標。本文の child_target_chars とは独立 (#894)。
    table_child_target_chars: int = DEFAULT_TABLE_CHILD_TARGET_CHARS
    parent_target_chars: int = DEFAULT_PARENT_TARGET_CHARS
    parent_max_pages: int = DEFAULT_PARENT_MAX_PAGES
    parent_max_children: int = DEFAULT_PARENT_MAX_CHILDREN
    contextual_search_text_enabled: bool = DEFAULT_CONTEXTUAL_SEARCH_TEXT_ENABLED
    search_text_context_max_chars: int = DEFAULT_SEARCH_TEXT_CONTEXT_MAX_CHARS
    child_search_text_max_chars: int = DEFAULT_CHILD_SEARCH_TEXT_MAX_CHARS
    page_number_min_run_pages: int = DEFAULT_PAGE_NUMBER_MIN_RUN_PAGES
    page_number_offset_tolerance: int = DEFAULT_PAGE_NUMBER_OFFSET_TOLERANCE

    def validate(self) -> ChunkingConfig:
        """チャンキング設定が許容範囲内かを検証します。"""
        return ChunkingConfig(
            child_target_chars=_checked_int(
                self.child_target_chars,
                "子チャンク目標文字数",
                CHILD_TARGET_CHARS_RANGE[0],
                CHILD_TARGET_CHARS_RANGE[1],
            ),
            table_child_target_chars=_checked_int(
                self.table_child_target_chars,
                "表の子チャンク目標文字数",
                TABLE_CHILD_TARGET_CHARS_RANGE[0],
                TABLE_CHILD_TARGET_CHARS_RANGE[1],
            ),
            parent_target_chars=_checked_int(
                self.parent_target_chars,
                "親チャンク目標文字数",
                PARENT_TARGET_CHARS_RANGE[0],
                PARENT_TARGET_CHARS_RANGE[1],
            ),
            parent_max_pages=_checked_int(
                self.parent_max_pages,
                "親チャンク最大ページ数",
                PARENT_MAX_PAGES_RANGE[0],
                PARENT_MAX_PAGES_RANGE[1],
            ),
            parent_max_children=_checked_int(
                self.parent_max_children,
                "親チャンク最大 child 数",
                PARENT_MAX_CHILDREN_RANGE[0],
                PARENT_MAX_CHILDREN_RANGE[1],
            ),
            contextual_search_text_enabled=_checked_bool(
                self.contextual_search_text_enabled,
                "contextual search text",
            ),
            search_text_context_max_chars=_checked_int(
                self.search_text_context_max_chars,
                "search text context 最大文字数",
                SEARCH_TEXT_CONTEXT_CHARS_RANGE[0],
                SEARCH_TEXT_CONTEXT_CHARS_RANGE[1],
            ),
            child_search_text_max_chars=_checked_int(
                self.child_search_text_max_chars,
                "child search text 最大文字数",
                CHILD_SEARCH_TEXT_CHARS_RANGE[0],
                CHILD_SEARCH_TEXT_CHARS_RANGE[1],
            ),
            page_number_min_run_pages=_checked_int(
                self.page_number_min_run_pages,
                "頁番号の並びの最小頁数",
                PAGE_NUMBER_MIN_RUN_PAGES_RANGE[0],
                PAGE_NUMBER_MIN_RUN_PAGES_RANGE[1],
            ),
            page_number_offset_tolerance=_checked_int(
                self.page_number_offset_tolerance,
                "頁番号の並びの差のずれ",
                PAGE_NUMBER_OFFSET_TOLERANCE_RANGE[0],
                PAGE_NUMBER_OFFSET_TOLERANCE_RANGE[1],
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON 保存に使う dict 表現へ変換します。"""
        return asdict(self)


@dataclass
class DocumentChunk:
    """親または子チャンクの本文、ページ範囲、根拠 record を保持します。"""

    chunk_id: str
    chunk_level: Literal["parent", "child"]
    chunk_seq: int
    parent_chunk_id: str
    child_chunk_ids: list[str]
    text: str
    retrieval_text: str
    char_count: int
    token_estimate: int
    source_run_id: str
    source_file_name: str
    source_engine_id: str
    source_engine_label: str
    page_start: int
    page_end: int
    source_seq_ranges: list[dict[str, int]]
    source_record_refs: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 保存に使う dict 表現へ変換します。"""
        return asdict(self)


@dataclass(frozen=True)
class ChunkingResult:
    """1 回のチャンキング実行で生成されたチャンクと入力 metadata を保持します。"""

    source_run_id: str
    chunk_run_id: str
    source_file_name: str
    selected_engine_ids: list[str]
    config: ChunkingConfig
    config_hash: str
    created_at_utc: str
    active: bool
    source_file_sha256: str
    source_page_count: int
    chunks: list[DocumentChunk]
    json_path: str
    jsonl_path: str
    latest_path: str
    classification: dict[str, Any] = field(default_factory=dict)
    document_metadata: dict[str, Any] = field(default_factory=dict)
    # SDK 文書の識別子。PDF のない索引で使用し、未指定の過去 manifest は空文字で読む。
    source_document_id: str = ""

    @property
    def child_count(self) -> int:
        """生成済み child chunk の件数を返します。"""
        return sum(1 for chunk in self.chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

    @property
    def parent_count(self) -> int:
        """生成済み parent chunk の件数を返します。"""
        return sum(1 for chunk in self.chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL)


def _compact_json_text(value: Any, max_chars: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value or "")
    return _trim_search_text(text, max_chars)


def _clean_search_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _trim_search_text(value: Any, max_chars: int) -> str:
    text = _clean_search_text(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "…"


def _ordered_nonempty(values: Iterable[Any]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_search_text(value)
        if not text or text in seen:
            continue
        selected.append(text)
        seen.add(text)
    return selected


# section_path の各見出しの出所。section_header と title は Docling の分類、それ以外は chunking の昇格・推定。
SECTION_HEADER_HEADING_SOURCE = "section_header"
TITLE_HEADING_SOURCE = "title"
RUNNING_HEAD_HEADING_SOURCE = "running_head"
ROUTE_CAPTION_HEADING_SOURCE = "route_caption"
PROMOTED_TEXT_HEADING_SOURCE = "promoted_text"
HEADING_SOURCES = frozenset(
    {
        SECTION_HEADER_HEADING_SOURCE,
        TITLE_HEADING_SOURCE,
        RUNNING_HEAD_HEADING_SOURCE,
        ROUTE_CAPTION_HEADING_SOURCE,
        PROMOTED_TEXT_HEADING_SOURCE,
    }
)


def _metadata_classification(metadata: dict[str, Any]) -> Any:
    """構築中（top-level）でも投影後（document 配下）でも分類を返す。"""
    if metadata.get("classification") is not None:
        return metadata.get("classification")
    document = metadata.get("document") if isinstance(metadata.get("document"), dict) else {}
    return document.get("classification")


# 空白除去後のフッター全体がページ番号だけの表記（3 / - 3 - / 3ページ / 3頁 / P.3 / Page 3 / 3/20 / ii / 三頁）。
def _page_number_footer_pattern() -> re.Pattern[str]:
    """ページ番号だけのフッター。ページの語・範囲の記号・数の体系（ローマ数字・漢数字）は言語資源(#2181・#2289)。

    group 1 が頁番号の字（算用数字・ローマ数字・漢数字）。値の読み取りは ``page_numbers.read_page_number``。
    """
    return _page_regexes(current_pack().hash)[0]


@lru_cache(maxsize=16)
def _page_regexes(_pack_hash: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    m = marker_patterns()
    t = text_patterns()
    return (
        re.compile(
            rf"(?:p\.?|{m.page_words})?{m.range_dash}?(\d{{1,3}}|{t.roman}+|{t.kanji}+)"
            rf"(?:{m.range_dash}|{m.page_words}|/\d{{1,4}})?",
            re.IGNORECASE,
        ),
        re.compile(rf"(?:\d{{1,4}}|{t.kanji}+)(?=\s*{m.page_words})", re.IGNORECASE),
        # 前後が語の字でないローマ数字の語（「ii」「Part iv」）。標準の書き方かは parse_roman で確かめる。
        re.compile(rf"(?<!\w){t.roman}+(?!\w)", re.IGNORECASE),
    )


def _boilerplate_text_key(text: str) -> str:
    """柱・フッターの繰り返しを比べる鍵（頁番号の数を伏せる）。数の体系は言語資源(#2289)。"""
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    patterns = _page_regexes(current_pack().hash)
    if patterns[0].fullmatch(_compact_page_text(normalized)):
        # 頁番号だけの文（「- 3 -」「ii」「三頁」）は数の体系・飾りによらず同じ鍵にする。
        return "#"
    normalized = re.sub(r"\b\d{1,4}\b", "#", normalized)
    normalized = patterns[2].sub(lambda match: "#" if parse_roman(match.group(0)) is not None else match.group(0), normalized)
    # 漢字・かなは \w 扱いで \b が成立しないため、ページ表記の数字（算用数字・漢数字）は別に正規化します。
    # 「第3章」のような章番号は対象外とし、章ごとに異なるヘッダーを同一視しません。
    normalized = patterns[1].sub("#", normalized)
    return normalized


def _compact_page_text(text: str) -> str:
    """頁番号の形を比べる文（言語資源の文字の正規化（既定 NFKC）をして空白を除く）。"""
    form = current_pack().value("symbols", "normalization")
    normalized = unicodedata.normalize(form, text or "") if form != "none" else text or ""
    return re.sub(r"\s+", "", normalized)


def _seq_ranges(refs: Sequence[dict[str, Any]]) -> list[dict[str, int]]:
    sorted_refs = sorted(refs, key=lambda ref: (int(ref.get("page") or 0), int(ref.get("seq_no") or 0)))
    ranges: list[dict[str, int]] = []
    for ref in sorted_refs:
        page = int(ref.get("page") or 0)
        seq = int(ref.get("seq_no") or 0)
        if not ranges or ranges[-1]["page"] != page or ranges[-1]["seq_end"] + 1 < seq:
            ranges.append({"page": page, "seq_start": seq, "seq_end": seq})
        else:
            ranges[-1]["seq_end"] = max(ranges[-1]["seq_end"], seq)
    return ranges


def _unique_refs(refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for ref in sorted(refs, key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0), str(item.get("record_id") or ""))):
        key = (int(ref.get("page") or 0), int(ref.get("seq_no") or 0), str(ref.get("record_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _token_estimate(text: str) -> int:
    return max(1, math.ceil(len(text) / 2))


def _text_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _flatten_table_html(text: str) -> str:
    """表の HTML を、セルを ` | `、行を ` / ` で区切った平文にします。tag の途中で切れる要約を作らないため。"""
    if "<" not in text:
        return text
    flat = re.sub(r"</t[dh]>", " | ", text, flags=re.I)
    flat = re.sub(r"</tr>", " / ", flat, flags=re.I)
    flat = re.sub(r"<[^<>]{1,40}>", " ", flat)
    flat = _drop_cell_separators_before_row_end(flat)
    return re.sub(r"\s+", " ", flat).strip(" |/")


def _drop_cell_separators_before_row_end(text: str) -> str:
    """行の区切り ` / ` の直前に残るセルの区切り ` | `（と空白）を外します。

    以前の `(?:\\s*\\|\\s*)+/` は `/` の無い `| | | …` で指数的に後戻りした（#1646）。
    `/` で分け、各区間の末尾の空白と `|` を 1 回だけ読んで外す（`|` が無い区間は変えない）。
    """
    parts = text.split("/")
    for index in range(len(parts) - 1):
        part = parts[index]
        end = len(part)
        while end > 0 and (part[end - 1].isspace() or part[end - 1] == "|"):
            end -= 1
        if "|" in part[end:]:
            parts[index] = part[:end] + " "
    return "/".join(parts)


def _context_preview(text: str, max_chars: int) -> str:
    """検索文脈用の要約。表は平文にし、上限に収まらないときは文・セル・語の区切りで切る。

    途中で切れた語や HTML tag は埋め込みと全文検索のノイズになるだけで、意味を足さない。
    """
    normalized = re.sub(r"\s+", " ", _flatten_table_html(text or "")).strip()
    if len(normalized) <= max_chars:
        return normalized
    head = normalized[: max_chars - 1]
    for boundary in ("。", " / ", " | ", "、", " "):
        position = head.rfind(boundary)
        if position >= max_chars // 2:
            head = head[: position + (len(boundary) if boundary == "。" else 0)]
            break
    return head.rstrip(" |/、") + "…"


def _preview(text: str, max_chars: int = 160) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1] + "…"


def _page_span(page_start: int, page_end: int) -> str:
    if page_start == page_end:
        return f"p.{page_start}"
    return f"p.{page_start}-{page_end}"


def _format_seq_ranges(ranges: Sequence[dict[str, int]]) -> str:
    pieces = []
    for item in ranges:
        page = item.get("page")
        start = item.get("seq_start")
        end = item.get("seq_end")
        if start == end:
            pieces.append(f"p.{page} #{start}")
        else:
            pieces.append(f"p.{page} #{start}-{end}")
    return ", ".join(pieces)


def _checked_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"{label} は true/false で指定してください。")


def _checked_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        # int(12.7) は黙って 12 になる。設定の小数は入力ミスなので受け付けない (#802)。
        raise ValueError(f"{label} は整数で指定してください。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} は整数で指定してください。") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label} は {minimum}〜{maximum} の範囲で指定してください。")
    return number


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_string(payload: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{key} が文字列ではありません。")
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "active"}:
            return True
        if normalized in {"false", "0", "no", "n", "inactive"}:
            return False
    if value is None:
        return default
    return bool(value)
