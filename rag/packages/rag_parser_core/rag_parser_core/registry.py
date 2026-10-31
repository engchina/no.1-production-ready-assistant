"""ファイル種別ごとの軽量 parser registry。

Docling / Unstructured 系の「ファイルタイプ別 partition -> 共通 schema」
という考え方を、本プロジェクトの `StructuredExtraction` へ再マップする。
外部 parser は任意依存として扱い、feature flag 有効時だけ呼び出す。
得られた出力は必ず本プロジェクトの `StructuredExtraction` へ再マップする。
"""

from __future__ import annotations

import csv
import html
import importlib
import importlib.util
import inspect
import logging
import os
import re
import sys
import tempfile
import types
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import Path, PurePath
from typing import Any, cast
from xml.etree import ElementTree

from charset_normalizer import from_bytes

from rag_parser_core.capabilities import adapter_supports_source
from rag_parser_core.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionMetadataValue,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from rag_parser_core.parsed_table import (
    HEADER_CELL_KEY,
    PARSED_TABLE_GRID_KEY,
    PARSED_TABLE_GRID_VERSION,
)
from rag_parser_core.result import (
    EXTERNAL_ADAPTER_PACKAGES as EXTERNAL_ADAPTER_PACKAGES,
)
from rag_parser_core.result import (
    LOCAL_PARSER_VERSION,
    UNSTRUCTURED_TEXT_CONTRACT,
    ExternalAdapterRunner,
)
from rag_parser_core.result import (
    SERVICE_ADAPTER_BACKENDS as SERVICE_ADAPTER_BACKENDS,
)
from rag_parser_core.result import (
    ParserRegistryResult as ParserRegistryResult,
)
from rag_parser_core.source import (
    SourceModality,
    SourceProfile,
    is_plain_text_markup,
    template_for_source_profile,
)
from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_JSONL_BACKEND,
    STRUCTURED_RECORDS_JSONL_PARSER_VERSION,
    STRUCTURED_RECORDS_REQUIRES_JSONL_BACKEND_WARNING,
    StructuredRecordsError,
    is_structured_records_content_type,
    oversized_record_line,
    parse_structured_records,
    structured_records_extraction,
    with_record_boundaries,
)
from rag_parser_core.unstructured_compat import ensure_pi_heif_absent_stub

# 共有 contract 型・定数は result.py(軽量)へ移設済み。registry は実装専用とし、backend が
# registry を読み込まずに型を参照できるようにする。本モジュール固有の定数のみ残す。
TABLE_PRESERVE_ROWS_TEMPLATE = "table_preserve_rows"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfficeSegmentExtraction:
    """OpenXML Office の slide/sheet 単位の抽出結果。"""

    segment_kind: str
    number: int
    source_path: str
    extraction: StructuredExtraction


@dataclass(frozen=True)
class OfficeSegmentFailure:
    """OpenXML Office の slide/sheet 単位 parser 失敗。"""

    segment_kind: str
    number: int
    source_path: str
    error_code: str = "office_segment_parse_failed"


@dataclass(frozen=True)
class OfficeSegmentParseResult:
    """OpenXML Office segment parser の成功/失敗一覧。"""

    segments: tuple[OfficeSegmentExtraction, ...] = ()
    failures: tuple[OfficeSegmentFailure, ...] = ()


@dataclass(frozen=True)
class _OfficeTableElementMatch:
    """Office table element と tables[] の lineage metadata 対応。"""

    element_id: str | None
    page_number: int | None
    metadata: dict[str, ExtractionMetadataValue]


@dataclass(frozen=True)
class _HTMLBlock:
    """HTML block parser が抽出した読み順 block。"""

    tag: str
    text: str
    links: tuple[_HTMLLink, ...] = ()
    images: tuple[_HTMLImage, ...] = ()
    code_language: str | None = None


@dataclass(frozen=True)
class _HTMLLink:
    """HTML anchor の表示 text と URL。"""

    text: str
    url: str


@dataclass(frozen=True)
class _HTMLImage:
    """HTML / Markdown image の安全な参照 metadata。"""

    asset_id: str
    src: str | None
    alt_text: str
    title: str | None = None


@dataclass(frozen=True)
class _HTMLTableCell:
    """HTML table cell の text と span。"""

    text: str
    row_span: int = 1
    col_span: int = 1
    # th のセル（表頭）か（解析の表の格子の表頭の段。#3429）。
    header: bool = False


@dataclass(frozen=True)
class _HTMLTable:
    """HTML table parser が抽出した行列。"""

    rows: tuple[tuple[_HTMLTableCell, ...], ...]
    caption: str | None = None


@dataclass(frozen=True)
class _AdapterElementView:
    """adapter の nested block に親 container metadata を継承させる軽量 view。"""

    item: object
    inherited_metadata: Mapping[str, object]


@dataclass(frozen=True)
class _XlsxFormulaCell:
    """XLSX formula cell を table / equation lineage へ戻すための中間表現。"""

    cell_ref: str
    row: int
    col: int
    formula: str
    value: str


ADAPTER_CHILD_CONTAINER_ATTRS = (
    "elements",
    "chunks",
    "blocks",
    "children",
    "texts",
    "tables",
    "figures",
    "pictures",
    "pages",
    "groups",
    "items",
    "body",
)


class _TextHTMLParser(HTMLParser):
    """HTML から読み順に近い text block を抽出する。"""

    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }

    def __init__(self, *, retain_single_column_tables: bool = False) -> None:
        super().__init__()
        self._retain_single_column_tables = retain_single_column_tables
        self._parts: list[str] = []
        self._blocks: list[_HTMLBlock] = []
        self._current_tag: str | None = None
        self._buffer: list[str] = []
        self._block_links: list[_HTMLLink] = []
        self._active_link_href: str | None = None
        self._active_link_text: list[str] = []
        self._tables: list[_HTMLTable] = []
        self._image_count = 0
        self._code_depth = 0
        self._code_language: str | None = None
        self._table_depth = 0
        self._table_rows: list[list[_HTMLTableCell]] | None = None
        self._table_row: list[_HTMLTableCell] | None = None
        self._table_cell_buffer: list[str] | None = None
        self._table_cell_row_span = 1
        self._table_cell_col_span = 1
        self._table_cell_header = False
        self._table_caption_buffer: list[str] | None = None
        self._table_caption: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if self._table_depth == 0 and normalized == "a":
            self._active_link_href = _html_safe_href(attrs)
            self._active_link_text = []
            return
        if self._table_depth == 0 and normalized == "img":
            self._append_image_block(attrs)
            return
        if self._table_depth == 0 and normalized in {"pre", "code"}:
            self._start_code_block(attrs)
            return
        if normalized == "table":
            self._flush_block()
            self._table_depth += 1
            if self._table_depth == 1:
                self._table_rows = []
            return
        if self._table_depth > 0:
            if normalized == "caption":
                self._flush_table_cell()
                self._flush_table_row()
                self._table_caption_buffer = []
                return
            if normalized == "tr":
                self._flush_table_cell()
                self._flush_table_row()
                self._table_row = []
            elif normalized in {"td", "th"}:
                self._flush_table_cell()
                self._table_cell_buffer = []
                self._table_cell_row_span = _html_span_attr(attrs, "rowspan")
                self._table_cell_col_span = _html_span_attr(attrs, "colspan")
                self._table_cell_header = normalized == "th"
            return
        if normalized in self.BLOCK_TAGS:
            self._flush_block()
            self._current_tag = normalized
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self._table_depth == 0 and normalized == "a":
            self._flush_link()
            return
        if self._table_depth == 0 and normalized in {"pre", "code"} and self._code_depth > 0:
            self._code_depth -= 1
            if self._code_depth == 0:
                self._flush_block()
                self._parts.append("\n")
            return
        if self._table_depth > 0:
            if normalized in {"td", "th"}:
                self._flush_table_cell()
            elif normalized == "tr":
                self._flush_table_cell()
                self._flush_table_row()
            elif normalized == "caption":
                self._flush_table_caption()
            elif normalized == "table":
                self._flush_table_cell()
                self._flush_table_row()
                self._flush_table_caption()
                if self._table_depth == 1:
                    self._flush_table()
                self._table_depth = max(0, self._table_depth - 1)
            return
        if normalized in self.BLOCK_TAGS:
            self._flush_block()
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._code_depth > 0:
            code = html.unescape(data).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            if code.strip():
                self._parts.append(code)
                self._current_tag = "code"
                self._buffer.append(code)
            return
        cleaned = re.sub(r"\s+", " ", html.unescape(data)).strip()
        if cleaned:
            if self._table_depth > 0 and self._table_caption_buffer is not None:
                self._table_caption_buffer.append(cleaned)
                return
            if self._table_depth > 0 and self._table_cell_buffer is not None:
                self._table_cell_buffer.append(cleaned)
                return
            self._parts.append(cleaned)
            if self._current_tag is None:
                self._current_tag = "text"
            self._buffer.append(cleaned)
            if self._active_link_href is not None:
                self._active_link_text.append(cleaned)

    def text(self) -> str:
        return _clean_text("\n".join(self._parts))

    def blocks(self) -> tuple[_HTMLBlock, ...]:
        self._flush_link()
        self._flush_block()
        return tuple(self._blocks)

    def tables(self) -> tuple[_HTMLTable, ...]:
        self._flush_table_cell()
        self._flush_table_row()
        self._flush_table_caption()
        self._flush_table()
        return tuple(self._tables)

    def _flush_block(self) -> None:
        if not self._buffer:
            self._current_tag = None
            self._block_links = []
            return
        text = _clean_text(" ".join(self._buffer))
        if text:
            self._blocks.append(
                _HTMLBlock(
                    tag=self._current_tag or "text",
                    text=text,
                    links=tuple(self._block_links),
                    code_language=(self._code_language if self._current_tag == "code" else None),
                )
            )
        self._buffer = []
        self._block_links = []
        if self._current_tag == "code":
            self._code_language = None
        self._current_tag = None

    def _flush_link(self) -> None:
        if self._active_link_href is None:
            return
        text = _clean_text(" ".join(self._active_link_text))
        if text:
            self._block_links.append(_HTMLLink(text=text, url=self._active_link_href))
        self._active_link_href = None
        self._active_link_text = []

    def _append_image_block(self, attrs: Sequence[tuple[str, str | None]]) -> None:
        src = _html_safe_src(attrs)
        alt_text = _html_attr(attrs, "alt") or ""
        title = _html_attr(attrs, "title")
        text = _clean_text(alt_text or title or src or "")
        if not text:
            return
        self._flush_block()
        asset_id = f"html-image-{self._image_count:04d}"
        self._image_count += 1
        image = _HTMLImage(
            asset_id=asset_id,
            src=src,
            alt_text=alt_text or text,
            title=title,
        )
        links: list[_HTMLLink] = []
        if self._active_link_href is not None:
            links.append(_HTMLLink(text=text, url=self._active_link_href))
            self._active_link_text.append(text)
        if src is not None:
            links.append(_HTMLLink(text=text, url=src))
        self._parts.append(text)
        self._blocks.append(
            _HTMLBlock(
                tag="figure",
                text=text,
                links=tuple(links),
                images=(image,),
            )
        )

    def _start_code_block(self, attrs: Sequence[tuple[str, str | None]]) -> None:
        language = _html_code_language(attrs)
        if self._code_depth == 0:
            self._flush_block()
            self._current_tag = "code"
            self._code_language = language
            self._parts.append("\n")
        elif self._code_language is None:
            self._code_language = language
        self._code_depth += 1

    def _flush_table_cell(self) -> None:
        if self._table_cell_buffer is None:
            return
        text = _clean_table_cell(" ".join(self._table_cell_buffer))
        # 空のセルも列の位置を持つので行に足す（捨てると後ろのセルが左の列にずれる。#3428）。
        # すべてのセルが空の行は ``_flush_table_row`` が捨てる。
        if self._table_row is not None:
            self._table_row.append(
                _HTMLTableCell(
                    text=text,
                    row_span=self._table_cell_row_span,
                    col_span=self._table_cell_col_span,
                    header=self._table_cell_header,
                )
            )
        self._table_cell_buffer = None
        self._table_cell_row_span = 1
        self._table_cell_col_span = 1
        self._table_cell_header = False

    def _flush_table_row(self) -> None:
        if self._table_row is None:
            return
        if self._table_rows is not None and any(cell.text.strip() for cell in self._table_row):
            self._table_rows.append(self._table_row)
        self._table_row = None

    def _flush_table_caption(self) -> None:
        if self._table_caption_buffer is None:
            return
        caption = _clean_text(" ".join(self._table_caption_buffer))
        if caption:
            self._table_caption = caption
        self._table_caption_buffer = None

    def _flush_table(self) -> None:
        if not self._table_rows:
            self._table_rows = None
            self._table_caption = None
            return
        html_table = _HTMLTable(
            rows=tuple(tuple(cell for cell in row) for row in self._table_rows),
            caption=self._table_caption,
        )
        column_count = _html_table_shape(html_table)[1]
        if column_count > 1 or self._retain_single_column_tables:
            self._tables.append(html_table)
            self._blocks.append(_HTMLBlock(tag="table", text=_html_table_markdown(html_table)))
        self._table_rows = None
        self._table_caption = None


def _html_span_attr(attrs: Sequence[tuple[str, str | None]], name: str) -> int:
    for key, value in attrs:
        if key.strip().casefold() != name or value is None:
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            return 1
        return max(1, min(parsed, 100))
    return 1


def _html_safe_href(attrs: Sequence[tuple[str, str | None]]) -> str | None:
    return _html_safe_url_attr(attrs, "href")


def _html_safe_src(attrs: Sequence[tuple[str, str | None]]) -> str | None:
    return _html_safe_url_attr(attrs, "src")


def _html_safe_url_attr(attrs: Sequence[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key.strip().casefold() != name or value is None:
            continue
        return _safe_link_url(html.unescape(value))
    return None


def _html_attr(attrs: Sequence[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key.strip().casefold() != name or value is None:
            continue
        cleaned = _clean_text(html.unescape(value))
        return cleaned[:500] if cleaned else None
    return None


def _html_code_language(attrs: Sequence[tuple[str, str | None]]) -> str | None:
    for name in ("data-language", "data-lang", "language", "lang"):
        value = _html_attr(attrs, name)
        language = _normalize_code_language(value)
        if language is not None:
            return language
    class_value = _html_attr(attrs, "class")
    if not class_value:
        return None
    for token in re.split(r"\s+", class_value):
        language = _normalize_code_language(token)
        if language is not None:
            return language
    return None


def _normalize_code_language(value: object) -> str | None:
    if not isinstance(value, str | int):
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    if not cleaned:
        return None
    for prefix in ("language-", "lang-", "highlight-", "brush:"):
        if cleaned.casefold().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    cleaned = cleaned.strip(";:,. ").casefold()
    if not cleaned or cleaned in {"none", "text", "plain", "plaintext", "sourcecode"}:
        return None
    return re.sub(r"[^a-z0-9_+.#-]+", "_", cleaned)[:40]


def _safe_link_url(value: str) -> str | None:
    url = re.sub(r"\s+", " ", value).strip().strip("<>")
    if not url:
        return None
    scheme = url.split(":", 1)[0].casefold() if ":" in url else ""
    if scheme in {"javascript", "data", "vbscript"}:
        return None
    return url[:500]


def _html_table_markdown(table: _HTMLTable) -> str:
    return "\n".join(_xlsx_markdown_row(row) for row in _html_table_plain_rows(table))


def _table_text_with_caption(markdown: str, caption: str | None) -> str:
    if not caption:
        return markdown
    return f"{caption}\n{markdown}"


def _html_table_plain_rows(table: _HTMLTable) -> list[list[str]]:
    return _table_plain_rows_from_cells(_table_cells_from_html_table(table))


def _html_table_shape(table: _HTMLTable) -> tuple[int, int]:
    return _table_shape_from_cells(_table_cells_from_html_table(table))


def _table_cells_from_html_table(table: _HTMLTable) -> list[ExtractionTableCell]:
    cells: list[ExtractionTableCell] = []
    active_spans: dict[int, int] = {}
    for row_index, row in enumerate(table.rows):
        col_index = 0
        new_spans: dict[int, int] = {}
        for html_cell in row:
            while active_spans.get(col_index, 0) > 0:
                col_index += 1
            cells.append(
                ExtractionTableCell(
                    row=row_index,
                    col=col_index,
                    text=html_cell.text,
                    row_span=html_cell.row_span,
                    col_span=html_cell.col_span,
                    metadata={
                        "cell_ref": _a1_cell_ref(row_index, col_index),
                        **({HEADER_CELL_KEY: True} if html_cell.header else {}),
                    },
                )
            )
            if html_cell.row_span > 1:
                for offset in range(html_cell.col_span):
                    new_spans[col_index + offset] = max(
                        new_spans.get(col_index + offset, 0),
                        html_cell.row_span - 1,
                    )
            col_index += html_cell.col_span
        active_spans = {
            col: remaining - 1 for col, remaining in active_spans.items() if remaining - 1 > 0
        }
        for col, remaining in new_spans.items():
            active_spans[col] = max(active_spans.get(col, 0), remaining)
    return cells


def parse_with_registry(
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
    adapter_backend: str = "local",
    docling_enabled: bool = False,
    unstructured_enabled: bool = False,
    mineru_enabled: bool = False,
    dots_ocr_enabled: bool = False,
    external_adapter_runner: ExternalAdapterRunner | None = None,
) -> ParserRegistryResult:
    """source profile に基づき、ローカル parser で処理できる場合は抽出する。

    `external_adapter_runner` を渡すと、外部 adapter(docling/unstructured/...)の実行を
    その callable へ委譲する。backend は in-process import の代わりに parser
    マイクロサービスを呼ぶ HTTP runner を注入する。None の場合は同一プロセス内で
    optional package を import する既定挙動(`_external_adapter_result`)を使う。
    """
    modality = source_profile.modality if source_profile is not None else SourceModality.UNKNOWN
    if _is_unsupported_outlook_msg(source_profile):
        return ParserRegistryResult(
            extraction=None,
            parser_backend="unsupported",
            template="unsupported_outlook_msg",
            warnings=("unsupported_outlook_msg",),
            unsupported_reason="outlook_msg_not_supported",
        )
    if _is_unsupported_tiff_image(source_profile):
        return ParserRegistryResult(
            extraction=None,
            parser_backend="unsupported",
            template="unsupported_tiff_image",
            warnings=("unsupported_tiff_image",),
            unsupported_reason="tiff_image_not_supported",
        )
    if _is_unsupported_legacy_office_binary(source_profile):
        return ParserRegistryResult(
            extraction=None,
            parser_backend="unsupported",
            template="unsupported_legacy_office_binary",
            warnings=("unsupported_legacy_office_binary",),
            unsupported_reason="legacy_office_binary_not_supported",
        )
    if _is_unsupported_odt_rtf(source_profile):
        # ODT / RTF は原本のままでは解析しない(unstructured は pandoc で読むが、pandoc は入れない。
        # ファイル準備の Office→PDF で PDF にしてから読む。#3351)。
        return ParserRegistryResult(
            extraction=None,
            parser_backend="unsupported",
            template="unsupported_odt_rtf",
            warnings=("unsupported_odt_rtf",),
            unsupported_reason="odt_rtf_not_supported",
        )

    normalized_backend = adapter_backend.strip().casefold()
    if normalized_backend in SERVICE_ADAPTER_BACKENDS:
        # 明示選択された service backend は core では実行せず、backend(ingestion)が
        # OCI クラウドサービスを直接呼ぶ。fallback ではなく意図的な選択なので
        # fallback_used は立てない。
        return ParserRegistryResult(
            extraction=None,
            parser_backend=normalized_backend,
            template="enterprise_ai_fallback",
        )

    adapter_warnings = _external_adapter_disabled_warnings(
        adapter_backend=adapter_backend,
        source_profile=source_profile,
        content_type=content_type,
        docling_enabled=docling_enabled,
        unstructured_enabled=unstructured_enabled,
        mineru_enabled=mineru_enabled,
        dots_ocr_enabled=dots_ocr_enabled,
    )
    adapter_fallback_used = bool(adapter_warnings)
    for backend in _requested_external_adapters(
        adapter_backend=adapter_backend,
        source_profile=source_profile,
        content_type=content_type,
        docling_enabled=docling_enabled,
        unstructured_enabled=unstructured_enabled,
        mineru_enabled=mineru_enabled,
        dots_ocr_enabled=dots_ocr_enabled,
    ):
        runner = external_adapter_runner or _default_external_adapter_runner
        adapter_result = runner(
            backend,
            source_bytes,
            source_profile,
            content_type,
        )
        if adapter_result.extraction is not None:
            return _with_adapter_fallback_context(
                adapter_result,
                adapter_warnings=adapter_warnings,
                adapter_fallback_used=adapter_fallback_used,
            )
        adapter_fallback_used = adapter_fallback_used or adapter_result.fallback_used
        adapter_warnings = tuple(dict.fromkeys([*adapter_warnings, *adapter_result.warnings]))

    if modality == SourceModality.TEXT:
        return _with_adapter_fallback_context(
            _text_result(
                source_bytes,
                parser_profile="local_text_structure",
                source_profile=source_profile,
                content_type=content_type,
            ),
            adapter_warnings=adapter_warnings,
            adapter_fallback_used=adapter_fallback_used,
        )
    if modality == SourceModality.HTML:
        return _with_adapter_fallback_context(
            _html_result(source_bytes),
            adapter_warnings=adapter_warnings,
            adapter_fallback_used=adapter_fallback_used,
        )
    if modality == SourceModality.EMAIL:
        return _with_adapter_fallback_context(
            _email_result(source_bytes),
            adapter_warnings=adapter_warnings,
            adapter_fallback_used=adapter_fallback_used,
        )
    if modality == SourceModality.OFFICE:
        result = _office_result(source_bytes, source_profile=source_profile)
        if result.extraction is not None:
            return _with_adapter_fallback_context(
                result,
                adapter_warnings=adapter_warnings,
                adapter_fallback_used=adapter_fallback_used,
            )
        return _with_adapter_fallback_context(
            ParserRegistryResult(
                extraction=None,
                parser_backend="enterprise_ai",
                fallback_used=True,
                template="office_enterprise_ai_fallback",
                warnings=result.warnings or ("office_local_parse_failed",),
            ),
            adapter_warnings=adapter_warnings,
            adapter_fallback_used=adapter_fallback_used,
        )
    _ = content_type
    return _with_adapter_fallback_context(
        ParserRegistryResult(extraction=None, parser_backend="enterprise_ai"),
        adapter_warnings=adapter_warnings,
        adapter_fallback_used=adapter_fallback_used,
    )


def _requested_external_adapters(
    *,
    adapter_backend: str,
    source_profile: SourceProfile | None,
    content_type: str,
    docling_enabled: bool,
    unstructured_enabled: bool,
    mineru_enabled: bool,
    dots_ocr_enabled: bool,
) -> tuple[str, ...]:
    normalized = adapter_backend.strip().casefold()
    if normalized in EXTERNAL_ADAPTER_PACKAGES:
        if _external_adapter_flag_enabled(
            normalized,
            docling_enabled=docling_enabled,
            unstructured_enabled=unstructured_enabled,
            mineru_enabled=mineru_enabled,
            dots_ocr_enabled=dots_ocr_enabled,
        ) and _external_adapter_supports_source(
            normalized,
            source_profile=source_profile,
            content_type=content_type,
        ):
            return (normalized,)
        return ()
    return ()


def _external_adapter_disabled_warnings(
    *,
    adapter_backend: str,
    source_profile: SourceProfile | None,
    content_type: str,
    docling_enabled: bool,
    unstructured_enabled: bool,
    mineru_enabled: bool,
    dots_ocr_enabled: bool,
) -> tuple[str, ...]:
    normalized = adapter_backend.strip().casefold()
    if normalized not in EXTERNAL_ADAPTER_PACKAGES:
        return ()
    if _external_adapter_flag_enabled(
        normalized,
        docling_enabled=docling_enabled,
        unstructured_enabled=unstructured_enabled,
        mineru_enabled=mineru_enabled,
        dots_ocr_enabled=dots_ocr_enabled,
    ):
        if _external_adapter_supports_source(
            normalized,
            source_profile=source_profile,
            content_type=content_type,
        ):
            return ()
        return (f"{normalized}_adapter_source_unsupported",)
    return (f"{normalized}_adapter_feature_flag_disabled",)


def _external_adapter_supports_source(
    backend: str,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> bool:
    # 判定の正本は capabilities.ADAPTER_CAPABILITIES(宣言的 matrix)
    return adapter_supports_source(
        backend, source_profile=source_profile, content_type=content_type
    )


def _source_extension(source_profile: SourceProfile | None) -> str:
    if source_profile is None:
        return ""
    return (source_profile.extension or PurePath(source_profile.sanitized_file_name).suffix).lower()


def _source_route_kind(source_profile: SourceProfile | None, content_type: str) -> str:
    modality = source_profile.modality if source_profile is not None else SourceModality.UNKNOWN
    extension = _source_extension(source_profile)
    normalized_content_type = _normalized_content_type_for_parser(
        content_type or (source_profile.content_type if source_profile is not None else "")
    )
    if modality == SourceModality.TEXT:
        return "text"
    if modality == SourceModality.EMAIL or extension == ".eml":
        return "email"
    if modality == SourceModality.HTML or normalized_content_type in {
        "text/html",
        "application/xhtml+xml",
    }:
        return "html"
    if modality == SourceModality.OFFICE or extension in {".docx", ".pptx", ".xlsx"}:
        return "office"
    if modality == SourceModality.IMAGE or normalized_content_type.startswith("image/"):
        return "image"
    if modality == SourceModality.PDF or normalized_content_type == "application/pdf":
        return "pdf"
    return "unknown"


def _external_adapter_flag_enabled(
    backend: str,
    *,
    docling_enabled: bool,
    unstructured_enabled: bool,
    mineru_enabled: bool,
    dots_ocr_enabled: bool,
) -> bool:
    return {
        "docling": docling_enabled,
        "unstructured": unstructured_enabled,
        "mineru": mineru_enabled,
        "dots_ocr": dots_ocr_enabled,
    }.get(backend, False)


def run_external_adapter(
    backend: str,
    source_bytes: bytes,
    source_profile: SourceProfile | None,
    content_type: str,
) -> ParserRegistryResult:
    """単一の外部 adapter を同一プロセス内で実行する公開 API。

    parser マイクロサービスはこの関数を呼んで、その image に導入済みの adapter
    (docling/unstructured/dots_ocr)で parse し、結果を
    `ParseResponse` として返す。package 未導入なら `*_adapter_package_missing`、
    parse 失敗なら `*_adapter_failed: <例外の型名>` の fallback を返す。
    """
    return _external_adapter_result(
        backend,
        source_bytes=source_bytes,
        source_profile=source_profile,
        content_type=content_type,
    )


def _default_external_adapter_runner(
    backend: str,
    source_bytes: bytes,
    source_profile: SourceProfile | None,
    content_type: str,
) -> ParserRegistryResult:
    """external_adapter_runner 未指定時の既定 runner(同一プロセス内 import)。"""
    return _external_adapter_result(
        backend,
        source_bytes=source_bytes,
        source_profile=source_profile,
        content_type=content_type,
    )


def _external_adapter_result(
    backend: str,
    *,
    source_bytes: bytes,
    source_profile: SourceProfile | None,
    content_type: str,
) -> ParserRegistryResult:
    """任意 parser adapter を呼び出し、失敗時は fallback 用 warning だけ返す。"""
    if backend == STRUCTURED_RECORDS_JSONL_BACKEND:
        # 構造化レコード JSONL の解析（#2457）。content type に関係なく JSONL の契約で読む
        # （契約に合わない入力は診断で止める。任意の JSONL を構造化レコードとみなさない）。
        return _structured_records_jsonl_result(source_bytes)
    if is_structured_records_content_type(content_type):
        # 前処理（csv_to_jsonl / excel_to_jsonl / word_to_jsonl）が作った構造化レコードの JSONL は、
        # 構造化レコード JSONL の解析エンジンだけが読む。ほかの adapter は黙って変換せず診断で
        # 止める（#2457）。
        return _adapter_fallback_result(backend, STRUCTURED_RECORDS_REQUIRES_JSONL_BACKEND_WARNING)
    if not _external_adapter_package_available(backend):
        return _adapter_fallback_result(backend, f"{backend}_adapter_package_missing")
    try:
        if backend == "docling":
            return _docling_adapter_result(
                source_bytes,
                source_profile=source_profile,
                content_type=content_type,
            )
        if backend == "unstructured":
            return _unstructured_adapter_result(
                source_bytes,
                source_profile=source_profile,
                content_type=content_type,
            )
        if backend == "dots_ocr":
            return _dots_ocr_adapter_result(
                source_bytes,
                source_profile=source_profile,
                content_type=content_type,
            )
        if backend == "xml_tree_json":
            return _xml_tree_json_adapter_result(source_bytes)
    except Exception as exc:
        invalid_input = _is_invalid_input_error(exc)
        logger.exception(
            "external parser adapter failed",
            extra={
                "parser_backend": backend,
                "content_type": content_type,
                "source_bytes": len(source_bytes),
                "source_sha256": (source_profile.content_sha256 if source_profile else None),
                "source_modality": (source_profile.modality.value if source_profile else None),
                "failure_kind": "invalid_input" if invalid_input else "adapter_error",
            },
        )
        if invalid_input:
            return _adapter_fallback_result(backend, f"{backend}_adapter_invalid_input")
        # 例外の型名を添える(例: ``docling_adapter_failed: ImportError``。#310)。利用者が
        # コンテナのログを開く前に失敗の種類(依存の import 失敗など)を切り分けられるようにする。
        # メッセージは内部情報を含みうるため型名だけにする。
        # backend は ``:`` の前を code として扱う。
        return _adapter_fallback_result(backend, f"{backend}_adapter_failed: {type(exc).__name__}")
    return _adapter_fallback_result(backend, f"{backend}_adapter_unsupported")


# 「入力ファイル自体が不正(破損・偽装拡張子・暗号化)」を示す例外の型名。
# core は pypdfium2 / PIL 等の adapter 依存を import できない(adapter 未導入環境でも
# registry は import される)ため、型そのものではなく型名でマッチする。
_INVALID_INPUT_EXCEPTION_NAMES = frozenset(
    {
        "PdfiumError",  # pypdfium2: "Data format error" 等
        "PDFSyntaxError",  # pdfminer(unstructured)
        "PSEOF",
        "PSSyntaxError",
        "UnidentifiedImageError",  # PIL(画像系 OCR adapter)
        "BadZipFile",  # docx/pptx/xlsx(unstructured/docling)
        "FileDataError",  # dots.ocr の公式 parser が内部で PDF を開けないとき
        "EmptyFileError",
    }
)
_INVALID_INPUT_MESSAGE_MARKERS = (
    "data format error",
    "cannot open broken document",
    "not a pdf",
    "cannot identify image",
    "file has not been decrypted",
    "password",
)


def _is_invalid_input_error(exc: BaseException) -> bool:
    """例外連鎖を辿り、入力データ不正(破損ファイル等)由来かを判定する。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _INVALID_INPUT_EXCEPTION_NAMES:
            return True
        message = str(current).casefold()
        if any(marker in message for marker in _INVALID_INPUT_MESSAGE_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _external_adapter_package_available(backend: str) -> bool:
    """adapter の実行に必要な import があるか確認する。"""
    return _module_available(EXTERNAL_ADAPTER_PACKAGES[backend])


def _module_available(name: str) -> bool:
    """テスト用 fake module と通常インストールの両方を安全に検出する。"""
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _adapter_fallback_result(backend: str, warning: str) -> ParserRegistryResult:
    return ParserRegistryResult(
        extraction=None,
        parser_backend=backend,
        parser_version=_adapter_version(backend),
        fallback_used=True,
        template=f"{backend}_fallback",
        warnings=(warning,),
    )


def _docling_adapter_result(
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> ParserRegistryResult:
    """Docling の document export を共通抽出 schema へ再マップする。"""
    converter_module = importlib.import_module("docling.document_converter")
    converter_type = converter_module.DocumentConverter
    with _temporary_source_file(source_bytes, source_profile, content_type) as path:
        converted = converter_type().convert(str(path))
    document = getattr(converted, "document", converted)
    structured_elements = _adapter_child_elements(document)
    text, export_kind = _export_adapter_text(
        document,
        preferred_methods=("export_to_markdown", "export_to_text"),
    )
    if not structured_elements and not text.strip():
        return _adapter_fallback_result("docling", "docling_adapter_empty")
    version = _adapter_version("docling")
    artifacts = {
        "adapter_export": "structured_elements" if structured_elements else export_kind,
        "external_adapter": "docling",
        **_docling_artifacts(document),
    }
    if structured_elements:
        extraction = _structured_from_adapter_elements(
            structured_elements,
            document_type=_document_type_for_source(source_profile),
            source_parser="docling_adapter",
            template=template_for_source_profile(source_profile),
            parser_backend="docling",
            parser_version=version,
            pages=_adapter_pages_from_source(document),
            extra_artifacts=artifacts,
        )
    else:
        extraction = _structured_from_text(
            text,
            document_type=_document_type_for_source(source_profile),
            source_parser="docling_adapter",
            template=template_for_source_profile(source_profile),
            default_content_kind=_default_content_kind_for_source(source_profile),
            parser_backend="docling",
            parser_version=version,
            extra_artifacts=artifacts,
        )
    extraction = _adapter_extraction_with_source_lineage(
        extraction,
        source_profile=source_profile,
        source_parser="docling_adapter",
        parser_backend="docling",
        parser_version=version,
        template=template_for_source_profile(source_profile),
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="docling",
        parser_version=version,
        template=template_for_source_profile(source_profile),
    )


def _structured_records_jsonl_result(source_bytes: bytes) -> ParserRegistryResult:
    """構造化レコード JSONL を、記録を 1 要素にし境界で区切った抽出にする（#2457）。

    要素は記録ごとの共通の形（`structured_records_extraction`）。raw_text は
    記録の本文を境界のマーカーで区切った検索用テキストにする（`with_record_boundaries`）。
    契約に合わない・本文が 1 要素の上限を超える記録があれば、黙って切らずに
    ``structured_records_invalid:<理由>@line<n>`` の fallback を返す（backend は取込を止める）。
    """
    backend = STRUCTURED_RECORDS_JSONL_BACKEND
    document = parse_structured_records(source_bytes)
    if isinstance(document, StructuredRecordsError):
        return _adapter_fallback_result(backend, document.warning())
    oversized = oversized_record_line(document)
    if oversized is not None:
        return _adapter_fallback_result(
            backend, StructuredRecordsError("record_too_large", oversized).warning()
        )
    extraction = with_record_boundaries(
        structured_records_extraction(
            document,
            source_parser=backend,
            parser_backend=backend,
            parser_version=STRUCTURED_RECORDS_JSONL_PARSER_VERSION,
        )
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend=backend,
        parser_version=STRUCTURED_RECORDS_JSONL_PARSER_VERSION,
        template=backend,
    )


def _xml_tree_json_adapter_result(source_bytes: bytes) -> ParserRegistryResult:
    """XML ツリー JSON を、段落を 1 要素にした抽出にする(#2024)。

    対応するスキーマでなければ、外部の parser に渡さず ``xml_tree_json_unsupported_tree`` の
    fallback を返す(backend は取込を止める)。
    """
    from rag_parser_core.xml_tree_json import XML_TREE_JSON_PARSER_VERSION, xml_tree_json_extraction

    extraction = xml_tree_json_extraction(source_bytes)
    if extraction is None:
        return _adapter_fallback_result("xml_tree_json", "xml_tree_json_unsupported_tree")
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="xml_tree_json",
        parser_version=XML_TREE_JSON_PARSER_VERSION,
        template="xml_tree_json",
    )


def _html_full_document_input(source_bytes: bytes) -> tuple[bytes, bool]:
    """main の特別な解析範囲指定だけを外し、元の順序・属性・本文を保持する。"""
    text = _decode_text_bytes(source_bytes)
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", text))
    replacements: list[tuple[int, int]] = []

    class MainTags(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)

        def record(self, tag: str, *, closing: bool = False) -> None:
            if tag != "main":
                return
            line, column = self.getpos()
            start = starts[line - 1] + column
            pattern = r"</\s*(main)" if closing else r"<(main)"
            if match := re.match(pattern, text[start:], re.IGNORECASE):
                replacements.append((start + match.start(1), start + match.end(1)))

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.record(tag)

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.record(tag)

        def handle_endtag(self, tag: str) -> None:
            self.record(tag, closing=True)

    parser = MainTags()
    parser.feed(text)
    parser.close()
    if not replacements:
        return source_bytes, False
    for start, end in reversed(replacements):
        text = text[:start] + "div" + text[end:]
    return text.encode("utf-8"), True


def _unstructured_adapter_result(
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> ParserRegistryResult:
    """Unstructured の partition elements を共通抽出 schema へ再マップする。"""
    # pi-heif は入れない(#3351)。unstructured の PDF・画像の解析が import する pi_heif を
    # stub で満たす。
    ensure_pi_heif_absent_stub()
    partition_module = importlib.import_module("unstructured.partition.auto")
    partition = partition_module.partition
    suffix: str | None = None
    if is_plain_text_markup(
        source_profile.extension if source_profile is not None else None,
        content_type or (source_profile.content_type if source_profile is not None else ""),
    ):
        # .rst / .org を unstructured は拡張子・MIME type で pandoc(入れない)へ回すので、
        # プレーンテキストとして渡す(#3351)。
        content_type, suffix = "text/plain", ".txt"
    partition_kwargs = _unstructured_partition_kwargs(source_profile, content_type)
    is_html = bool(
        source_profile is not None and source_profile.modality == "html"
    ) or _normalized_content_type_for_parser(content_type) in {"text/html", "application/xhtml+xml"}
    partition_bytes, main_expanded = (
        _html_full_document_input(source_bytes) if is_html else (source_bytes, False)
    )
    if main_expanded:
        partition_kwargs.update(encoding="utf-8", skip_headers_and_footers=False)
    with _temporary_source_file(
        partition_bytes, source_profile, content_type, suffix=suffix
    ) as path:
        partitioned = _call_with_supported_kwargs(
            partition,
            {
                "filename": str(path),
                "content_type": content_type,
                **partition_kwargs,
            },
        )
    elements = _adapter_sequence_items(partitioned)
    if not elements:
        return _adapter_fallback_result("unstructured", "unstructured_adapter_empty")
    version = _adapter_version("unstructured")
    extraction = _structured_from_adapter_elements(
        elements,
        document_type=_document_type_for_source(source_profile),
        source_parser="unstructured_adapter",
        template=template_for_source_profile(source_profile),
        parser_backend="unstructured",
        parser_version=version,
        extra_artifacts={
            "unstructured_text_contract": UNSTRUCTURED_TEXT_CONTRACT,
            **({"html_main_scope": "full_document"} if main_expanded else {}),
            **_unstructured_partition_artifacts(partition_kwargs),
        },
    )
    if (
        source_profile is not None and source_profile.modality == "html"
    ) or _normalized_content_type_for_parser(content_type) in {
        "text/html",
        "application/xhtml+xml",
    }:
        extraction = _restore_html_table_captions(extraction, source_bytes)
        extraction = _restore_html_region_roles(extraction, source_bytes)
    extraction = _adapter_extraction_with_source_lineage(
        extraction,
        source_profile=source_profile,
        source_parser="unstructured_adapter",
        parser_backend="unstructured",
        parser_version=version,
        template=template_for_source_profile(source_profile),
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="unstructured",
        parser_version=version,
        template=template_for_source_profile(source_profile),
    )


def _html_table_cell_signature(
    cells: Sequence[ExtractionTableCell],
) -> tuple[tuple[object, ...], ...]:
    """表題の対応はセルの位置・本文・結合だけで照合し、語の類似や表の順番で推測しない。"""
    return tuple(
        sorted(
            (cell.row, cell.col, _clean_text(cell.text), cell.row_span, cell.col_span)
            for cell in cells
        )
    )


def _restore_html_region_roles(
    extraction: StructuredExtraction, source_bytes: bytes
) -> StructuredExtraction:
    """原本の header / footer / caption に一意に含まれる要素の役割を保持する。"""

    class Regions(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.visible: list[str] = []
            self.regions: list[tuple[str, list[str]]] = []
            self.active: list[tuple[int, list[str]]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {
                "area",
                "base",
                "br",
                "col",
                "embed",
                "hr",
                "img",
                "input",
                "link",
                "meta",
                "param",
                "source",
                "track",
                "wbr",
            }:
                return
            self.stack.append(tag)
            if tag in {"header", "footer", "caption"}:
                pieces: list[str] = []
                self.regions.append((tag, pieces))
                self.active.append((len(self.stack), pieces))

        def handle_endtag(self, tag: str) -> None:
            if tag not in self.stack:
                return
            depth = len(self.stack) - self.stack[::-1].index(tag)
            self.stack = self.stack[: depth - 1]
            self.active = [(level, pieces) for level, pieces in self.active if level < depth]

        def handle_data(self, data: str) -> None:
            if any(tag in {"script", "style", "nav"} for tag in self.stack):
                return
            self.visible.append(data)
            for _level, pieces in self.active:
                pieces.append(data)

    parser = Regions()
    parser.feed(_decode_text_bytes(source_bytes))

    def compact(text: str) -> str:
        return "".join(text.split())

    visible = compact("".join(parser.visible))
    regions = [(role, compact("".join(pieces))) for role, pieces in parser.regions]
    elements = []
    for element in extraction.elements:
        text = compact(element.text)
        roles = {role for role, region in regions if text and text in region}
        if len(roles) == 1 and visible.count(text) == 1:
            element = element.model_copy(
                update={"metadata": {**element.metadata, "html_region_role": next(iter(roles))}}
            )
        elements.append(element)
    return extraction.model_copy(update={"elements": elements})


def _restore_html_table_captions(
    extraction: StructuredExtraction, source_bytes: bytes
) -> StructuredExtraction:
    """Unstructured が落とす HTML caption を、同じセルの格子を持つ表だけへ戻す。"""
    parser = _TextHTMLParser(retain_single_column_tables=True)
    parser.feed(_decode_text_bytes(source_bytes))
    captions: dict[tuple[tuple[object, ...], ...], set[str]] = {}
    for source_table in parser.tables():
        signature = _html_table_cell_signature(_table_cells_from_html_table(source_table))
        if signature:
            captions.setdefault(signature, set()).add(source_table.caption or "")
    captions = {signature: values for signature, values in captions.items() if any(values)}
    if not captions:
        return extraction
    elements = list(extraction.elements)
    added: list[DocumentElement] = []
    restored = 0
    resolved_signatures: set[tuple[tuple[object, ...], ...]] = set()
    table_elements = {
        element.element_id: element for element in elements if element.kind == "table"
    }
    for table in extraction.tables:
        signature = _html_table_cell_signature(table.cells)
        candidates = captions.get(signature, set())
        if not candidates:
            continue
        if table.caption:
            resolved_signatures.add(signature)
            continue
        parent = table_elements.get(table.element_id)
        if len(candidates) != 1 or parent is None or not parent.element_id:
            continue
        caption = next(iter(candidates))
        added.append(
            DocumentElement(
                kind="table_caption",
                text=caption,
                order=parent.order,
                element_id=f"{parent.element_id}-html-caption",
                parent_id=parent.element_id,
                content_kind="table",
                source_parser=parent.source_parser,
                page_number=parent.page_number,
                section_path=list(parent.section_path),
                metadata={"html_tag": "caption", "caption_source": "original_html"},
            )
        )
        restored += 1
        resolved_signatures.add(signature)
    unresolved = len(captions.keys() - resolved_signatures)
    if not added and not unresolved:
        return extraction
    # 読み順は表の直後に表題を置き、既存の adapter の表題の結び付けを共用する。
    by_parent = {element.parent_id: element for element in added}
    ordered = []
    for element in elements:
        ordered.append(element)
        if element.element_id in by_parent:
            ordered.append(by_parent[element.element_id])
    ordered = [element.model_copy(update={"order": order}) for order, element in enumerate(ordered)]
    ordered, tables = _attach_adapter_table_captions(ordered, extraction.tables)
    warnings = list(extraction.warnings)
    if unresolved:
        warnings.append(
            "HTML の表題と解析済みの表を一意に対応付けられませんでした。"
            "原本の表題を確認してください。"
        )
    return extraction.model_copy(
        update={
            "elements": ordered,
            "tables": tables,
            "raw_text": _clean_text(
                "\n\n".join(element.text for element in ordered if element.text.strip())
            ),
            "warnings": warnings,
            "parser_artifacts": {
                **extraction.parser_artifacts,
                "html_captions_restored": restored,
                "html_captions_unresolved": unresolved,
            },
        }
    )


def _run_dots_ocr(path: Path) -> object:
    """Dots.OCR(GPU)で 1 ファイルを OCR して document/markdown を得る(GPU 統合シーム)。"""
    module = importlib.import_module("dots_ocr")
    for attr in ("parse", "ocr", "to_markdown", "convert", "infer", "run"):
        candidate = getattr(module, attr, None)
        if callable(candidate):
            return candidate(str(path))
    return _run_dots_ocr_parser(path)


def _run_dots_ocr_parser(path: Path) -> str:
    """Dots.OCR 公式 parser を実行し、生成 markdown を返す。"""
    runtime = os.environ.get("DOTS_OCR_RUNTIME", "vllm").strip().lower() or "vllm"
    parser: Any
    if runtime in {"vllm", "official_vllm"}:
        parser = _load_dots_ocr_vllm_parser()
    elif runtime in {"hf", "hf_explicit_cuda"}:
        parser = _load_dots_ocr_hf_parser()
    else:
        raise RuntimeError(
            "dots_ocr_invalid_runtime: set DOTS_OCR_RUNTIME to vllm or hf_explicit_cuda"
        )
    prompt_mode = os.environ.get("DOTS_OCR_PROMPT_MODE", "prompt_layout_all_en").strip()
    if not prompt_mode:
        prompt_mode = "prompt_layout_all_en"
    fitz_preprocess = _env_enabled("DOTS_OCR_FITZ_PREPROCESS", default=True)
    with tempfile.TemporaryDirectory(prefix="dots-ocr-output-") as output_dir:
        result = parser.parse_file(
            str(path),
            output_dir=output_dir,
            prompt_mode=prompt_mode,
            fitz_preprocess=fitz_preprocess,
        )
        markdown_files = _dots_ocr_markdown_files(result, Path(output_dir))
        if not markdown_files:
            raise RuntimeError("dots_ocr: parser did not produce markdown")
        return "\n\n".join(
            markdown_file.read_text(encoding="utf-8", errors="replace")
            for markdown_file in markdown_files
        )


_DOTS_OCR_VLLM_PARSER_CACHE: dict[str, object] = {}
_DOTS_OCR_PARSER_CACHE: dict[str, object] = {}


def _load_dots_ocr_vllm_parser() -> object:
    """Dots.OCR 公式推奨の vLLM server 向け parser を遅延ロードする。"""
    protocol = os.environ.get("DOTS_OCR_PROTOCOL", "http").strip() or "http"
    ip = os.environ.get("DOTS_OCR_IP", "127.0.0.1").strip()
    if not ip:
        ip = "127.0.0.1"
    port = int(os.environ.get("DOTS_OCR_PORT", "8080"))
    model_name = os.environ.get("DOTS_OCR_MODEL_NAME", "model").strip() or "model"
    cache_key = "|".join((protocol, ip, str(port), model_name))
    cached = _DOTS_OCR_VLLM_PARSER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    parser_module = importlib.import_module("dots_ocr.parser")
    parser = parser_module.DotsOCRParser(
        protocol=protocol,
        ip=ip,
        port=port,
        model_name=model_name,
        temperature=float(os.environ.get("DOTS_OCR_TEMPERATURE", "0.1")),
        top_p=float(os.environ.get("DOTS_OCR_TOP_P", "1.0")),
        max_completion_tokens=int(os.environ.get("DOTS_OCR_MAX_COMPLETION_TOKENS", "16384")),
        num_thread=int(os.environ.get("DOTS_OCR_NUM_THREAD", "1")),
        dpi=int(os.environ.get("DOTS_OCR_DPI", "200")),
        output_dir=os.environ.get("DOTS_OCR_OUTPUT_DIR", "/tmp/dots-ocr-output"),
        min_pixels=_optional_int_env("DOTS_OCR_MIN_PIXELS"),
        max_pixels=_optional_int_env("DOTS_OCR_MAX_PIXELS"),
        use_hf=False,
    )
    _DOTS_OCR_VLLM_PARSER_CACHE[cache_key] = parser
    return parser


def _load_dots_ocr_hf_parser() -> object:
    """Dots.OCR HF parser を明示 CUDA mode で遅延ロードする(公式 vLLM 不可時の退避経路)。"""
    model_id = (
        os.environ.get("DOTS_OCR_MODEL_ID", "rednote-hilab/dots.mocr").strip()
        or "rednote-hilab/dots.mocr"
    )
    dtype_name = os.environ.get("DOTS_OCR_TORCH_DTYPE", "bfloat16").strip() or "bfloat16"
    device_name = os.environ.get("DOTS_OCR_DEVICE", "cuda:0").strip() or "cuda:0"
    attention_impl = os.environ.get("DOTS_OCR_ATTENTION_IMPLEMENTATION", "sdpa").strip() or "sdpa"
    cache_key = "|".join((model_id, dtype_name, device_name, attention_impl))
    cached = _DOTS_OCR_PARSER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    parser_module = importlib.import_module("dots_ocr.parser")
    base_parser = parser_module.DotsOCRParser

    class ExplicitCudaDotsOCRParser(base_parser):  # type: ignore[misc, valid-type]
        def _load_hf_model(self) -> None:
            torch = importlib.import_module("torch")
            if not torch.cuda.is_available():
                raise RuntimeError("dots_ocr_cuda_unavailable: Dots.OCR requires a CUDA GPU")
            transformers = importlib.import_module("transformers")
            qwen_vl_utils = importlib.import_module("qwen_vl_utils")
            model_ref = _resolve_dots_ocr_model_ref(model_id)
            dtype = _torch_dtype(
                torch,
                dtype_name,
                error_prefix="dots_ocr",
            )
            device = torch.device(device_name)
            config = transformers.AutoConfig.from_pretrained(model_ref, trust_remote_code=True)
            vision_config = getattr(config, "vision_config", None)
            if isinstance(vision_config, dict):
                vision_config["attn_implementation"] = attention_impl
            elif vision_config is not None:
                vision_config.attn_implementation = attention_impl
            _ensure_flash_attn_importable_for_dots(attention_impl)
            self.processor = transformers.AutoProcessor.from_pretrained(
                model_ref,
                trust_remote_code=True,
                use_fast=True,
            )
            model = transformers.AutoModelForCausalLM.from_pretrained(
                model_ref,
                config=config,
                trust_remote_code=True,
                dtype=dtype,
            )
            model.to(device)
            model.eval()
            self.model = model
            self.process_vision_info = qwen_vl_utils.process_vision_info
            self._rag_cuda_device = device

        def _inference_with_hf(self, image: object, prompt: str) -> str:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
            text = self.processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            image_inputs, video_inputs = self.process_vision_info(messages)
            inputs = self.processor(
                text=[text],
                images=image_inputs,
                videos=video_inputs,
                padding=True,
                return_tensors="pt",
            )
            inputs = inputs.to(self._rag_cuda_device)
            max_new_tokens = int(os.environ.get("DOTS_OCR_MAX_NEW_TOKENS", "24000"))
            generated_ids = self.model.generate(**inputs, max_new_tokens=max_new_tokens)
            generated_ids_trimmed = [
                out_ids[len(in_ids) :]
                for in_ids, out_ids in zip(inputs.input_ids, generated_ids, strict=False)
            ]
            decoded = self.processor.batch_decode(
                generated_ids_trimmed,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            return decoded[0] if decoded else ""

    parser = ExplicitCudaDotsOCRParser(
        num_thread=1,
        dpi=int(os.environ.get("DOTS_OCR_DPI", "200")),
        output_dir=os.environ.get("DOTS_OCR_OUTPUT_DIR", "/tmp/dots-ocr-output"),
        min_pixels=_optional_int_env("DOTS_OCR_MIN_PIXELS"),
        max_pixels=_optional_int_env("DOTS_OCR_MAX_PIXELS"),
        use_hf=True,
    )
    _DOTS_OCR_PARSER_CACHE[cache_key] = parser
    return parser


def _resolve_dots_ocr_model_ref(model_id: str) -> str:
    """HF repo id を local snapshot path へ解決し、remote code の相対 import を安定させる。"""
    local_path = Path(model_id).expanduser()
    if local_path.exists():
        return str(local_path)
    huggingface_hub = importlib.import_module("huggingface_hub")
    return str(huggingface_hub.snapshot_download(model_id))


def _ensure_flash_attn_importable_for_dots(attention_impl: str) -> None:
    """Dots.OCR remote code の無条件 import を sdpa/eager 利用時だけ安全に満たす。"""
    if attention_impl == "flash_attention_2":
        return
    if "flash_attn" in sys.modules:
        return
    try:
        if importlib.util.find_spec("flash_attn") is not None:
            return
    except (ImportError, ValueError):
        pass

    fake_flash_attn = types.ModuleType("flash_attn")
    fake_flash_attn.__spec__ = importlib.machinery.ModuleSpec("flash_attn", loader=None)
    fake_flash_attn.__dict__["__version__"] = "0.0.0"

    def _flash_attn_varlen_func(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(
            "dots_ocr_flash_attention_unavailable: set "
            "DOTS_OCR_ATTENTION_IMPLEMENTATION=sdpa or install flash_attn"
        )

    fake_flash_attn.__dict__["flash_attn_varlen_func"] = _flash_attn_varlen_func
    sys.modules["flash_attn"] = fake_flash_attn


def _dots_ocr_markdown_files(result: object, output_dir: Path) -> list[Path]:
    """Dots.OCR parse_file の戻り値と出力 directory から markdown path を収集する。"""
    markdown_files: list[Path] = []
    if isinstance(result, Sequence) and not isinstance(result, str | bytes | bytearray):
        for item in result:
            if isinstance(item, Mapping):
                for key in ("md_content_path", "md_content_nohf_path"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        markdown_files.append(Path(value))
    if not markdown_files:
        markdown_files = sorted(output_dir.rglob("*.md"))
    seen: set[Path] = set()
    existing: list[Path] = []
    for markdown_file in markdown_files:
        resolved = markdown_file.resolve()
        if resolved in seen or not markdown_file.exists():
            continue
        seen.add(resolved)
        existing.append(markdown_file)
    return existing


def _env_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _optional_int_env(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return int(value)


def _torch_dtype(torch: Any, dtype_name: str | None, *, error_prefix: str) -> object:
    """GPU OCR parser の dtype を明示的に解決する。未知値は実行時に誤設定として落とす。"""
    normalized = (dtype_name or "bfloat16").strip().lower()
    bfloat16 = torch.bfloat16
    float16 = torch.float16
    float32 = torch.float32
    dtype_by_name = {
        "bfloat16": bfloat16,
        "float16": float16,
        "fp16": float16,
        "float32": float32,
        "fp32": float32,
    }
    try:
        return dtype_by_name[normalized]
    except KeyError as exc:
        raise RuntimeError(
            f"{error_prefix}_invalid_torch_dtype: set {error_prefix.upper()}_TORCH_DTYPE to "
            "bfloat16, float16, or float32"
        ) from exc


def _ocr_engine_adapter_result(
    backend: str,
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
    runner: Callable[[Path], object],
) -> ParserRegistryResult:
    """GPU OCR engine の document/markdown 出力を共通抽出 schema へ再マップする。

    GPU 上の実 OCR 実行(`runner`)以外は docling と同じ汎用 remap を再利用するため、
    fixture(fake module)で remap 層だけを決定論テストできる。
    """
    with _temporary_source_file(source_bytes, source_profile, content_type) as path:
        rendered = runner(path)
    return remap_external_ocr_output(
        backend,
        rendered,
        source_profile=source_profile,
    )


def remap_external_ocr_output(
    backend: str,
    rendered: object,
    *,
    source_profile: SourceProfile | None,
    parser_version: str | None = None,
    pages: Sequence[ExtractionPage] | None = None,
    extra_artifacts: Mapping[str, ExtractionMetadataValue] | None = None,
) -> ParserRegistryResult:
    """外部 OCR API の出力を共通 ``StructuredExtraction`` へ再マップする。"""
    if isinstance(rendered, Sequence) and not isinstance(rendered, str | bytes | bytearray):
        structured_elements = tuple(
            element for item in rendered for element in _adapter_child_elements(item)
        )
    else:
        structured_elements = _adapter_child_elements(rendered)
    if structured_elements:
        text = ""
        export_kind = "structured_elements"
    else:
        text, export_kind = _export_adapter_text(
            rendered,
            preferred_methods=(
                "export_to_markdown",
                "markdown",
                "export_to_text",
                "text",
                "content",
            ),
        )
    if not structured_elements and (not isinstance(text, str) or not text.strip()):
        return _adapter_fallback_result(backend, f"{backend}_adapter_empty")
    version = parser_version or _adapter_version(backend)
    template = template_for_source_profile(source_profile)
    source_parser = f"{backend}_adapter"
    artifacts: dict[str, ExtractionMetadataValue] = {
        "adapter_export": export_kind,
        "external_adapter": backend,
        "ocr_engine": True,
    }
    artifacts.update(extra_artifacts or {})
    if structured_elements:
        extraction = _structured_from_adapter_elements(
            structured_elements,
            document_type=_document_type_for_source(source_profile),
            source_parser=source_parser,
            template=template,
            parser_backend=backend,
            parser_version=version,
            pages=pages if pages is not None else _adapter_pages_from_source(rendered),
            extra_artifacts=artifacts,
        )
    else:
        extraction = _structured_from_text(
            text,
            document_type=_document_type_for_source(source_profile),
            source_parser=source_parser,
            template=template,
            default_content_kind=_default_content_kind_for_source(source_profile),
            parser_backend=backend,
            parser_version=version,
            extra_artifacts=artifacts,
        )
    extraction = _adapter_extraction_with_source_lineage(
        extraction,
        source_profile=source_profile,
        source_parser=source_parser,
        parser_backend=backend,
        parser_version=version,
        template=template,
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend=backend,
        parser_version=version,
        template=template,
    )


def _dots_ocr_adapter_result(
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> ParserRegistryResult:
    """Dots.OCR(GPU)の OCR 結果を共通抽出 schema へ再マップする。"""
    return _ocr_engine_adapter_result(
        "dots_ocr",
        source_bytes,
        source_profile=source_profile,
        content_type=content_type,
        runner=_run_dots_ocr,
    )


def _adapter_extraction_with_source_lineage(
    extraction: StructuredExtraction,
    *,
    source_profile: SourceProfile | None,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
    template: str,
) -> StructuredExtraction:
    """外部 adapter の粒度差を source kind ごとの lineage contract へ寄せる。"""
    if source_profile is None:
        return extraction
    enriched = extraction
    if source_profile.modality == SourceModality.EMAIL:
        enriched = _adapter_email_extraction_with_lineage(enriched)
    if source_profile.modality == SourceModality.IMAGE:
        enriched = _adapter_image_extraction_with_full_frame_asset(
            enriched,
            source_profile=source_profile,
            source_parser=source_parser,
            parser_backend=parser_backend,
            parser_version=parser_version,
            template=template,
        )
    return enriched


def _adapter_email_extraction_with_lineage(
    extraction: StructuredExtraction,
) -> StructuredExtraction:
    """Unstructured などの email block に headers/body lineage を補う。"""
    updated_elements: list[DocumentElement] = []
    for index, element in enumerate(extraction.elements):
        metadata = dict(element.metadata)
        metadata.setdefault("email_part", _adapter_email_part(element, index=index))
        subject = metadata.get("subject")
        if isinstance(subject, str) and subject.strip():
            metadata.setdefault("subject_chars", len(subject.strip()))
        updated_elements.append(
            element.model_copy(
                update={
                    "content_kind": "email",
                    "page_number": element.page_number or 1,
                    "metadata": metadata,
                }
            )
        )
    artifacts = dict(extraction.parser_artifacts)
    artifacts.setdefault("email_lineage_normalized", True)
    return extraction.model_copy(
        update={
            "elements": updated_elements,
            "parser_artifacts": artifacts,
        }
    )


def _adapter_email_part(element: DocumentElement, *, index: int) -> str:
    metadata = element.metadata
    if metadata.get("email_part") in {"headers", "body"}:
        return str(metadata["email_part"])
    header_keys = {
        "subject",
        "from",
        "to",
        "cc",
        "bcc",
        "date",
        "sent_from",
        "sent_to",
    }
    if index == 0 or any(key in metadata for key in header_keys):
        return "headers"
    return "body"


def _adapter_image_extraction_with_full_frame_asset(
    extraction: StructuredExtraction,
    *,
    source_profile: SourceProfile,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
    template: str,
) -> StructuredExtraction:
    """bbox を返さない OCR adapter にも preview 可能な source image asset を持たせる。"""
    if extraction.assets or any(element.bbox for element in extraction.elements):
        return extraction
    page_number = _first_adapter_extraction_page_number(extraction)
    asset_id = "source-image-0000"
    asset = ExtractionAsset(
        asset_id=asset_id,
        kind="source_image",
        page_number=page_number,
        bbox=[0.0, 0.0, 1.0, 1.0],
        alt_text=source_profile.sanitized_file_name,
        metadata={
            "source_parser": source_parser,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            "chunk_template": template,
            "element_id": asset_id,
            "bbox_coordinate_mode": "xyxy",
            "bbox_unit": "ratio",
            "bbox_scope": "source_image_full_frame",
            "source_file_name": source_profile.sanitized_file_name,
            "content_sha256": source_profile.content_sha256,
        },
    )
    pages = extraction.pages
    if not pages:
        pages = [
            ExtractionPage(
                page_number=page_number,
                label=f"page {page_number}",
                element_ids=[
                    element.element_id for element in extraction.elements if element.element_id
                ],
            )
        ]
    artifacts = dict(extraction.parser_artifacts)
    artifacts.setdefault("source_image_full_frame_asset_count", 1)
    artifacts["adapter_asset_count"] = len(extraction.assets) + 1
    return extraction.model_copy(
        update={
            "pages": pages,
            "assets": [*extraction.assets, asset],
            "parser_artifacts": artifacts,
        }
    )


def _first_adapter_extraction_page_number(extraction: StructuredExtraction) -> int:
    if extraction.pages:
        return extraction.pages[0].page_number
    for element in extraction.elements:
        if element.page_number is not None:
            return element.page_number
    return 1


def _unstructured_partition_kwargs(
    source_profile: SourceProfile | None,
    content_type: str,
) -> dict[str, object]:
    """Unstructured adapter に渡す高保真 partition option。"""
    normalized_content_type = _normalized_content_type_for_parser(
        content_type or (source_profile.content_type if source_profile is not None else "")
    )
    extension = (source_profile.extension if source_profile is not None else "") or ""
    kwargs: dict[str, object] = {}
    if _unstructured_supports_page_breaks(normalized_content_type, extension):
        kwargs["include_page_breaks"] = True
    if _unstructured_supports_table_inference(normalized_content_type, extension):
        kwargs["strategy"] = "hi_res"
        kwargs["languages"] = ["jpn", "eng"]
        kwargs["infer_table_structure"] = True
    return kwargs


def _unstructured_partition_artifacts(
    partition_kwargs: Mapping[str, object],
) -> dict[str, ExtractionMetadataValue]:
    return {
        f"partition_{key}": value
        for key, value in partition_kwargs.items()
        if isinstance(value, str | int | float | bool) or value is None
    }


def _unstructured_supports_page_breaks(content_type: str, extension: str) -> bool:
    return content_type in {
        "application/pdf",
        "text/html",
        "application/xhtml+xml",
        "text/markdown",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    } or extension in {
        ".pdf",
        ".html",
        ".htm",
        ".xhtml",
        ".md",
        ".markdown",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".png",
        ".jpg",
        ".jpeg",
    }


def _unstructured_supports_table_inference(content_type: str, extension: str) -> bool:
    return (
        content_type == "application/pdf"
        or content_type.startswith("image/")
        or extension
        in {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".bmp",
        }
    )


def _call_with_supported_kwargs(
    func: object,
    kwargs: Mapping[str, object],
) -> object:
    """adapter 関数が受け取れる kwargs だけを渡す。"""
    if not callable(func):
        raise TypeError("adapter function is not callable")
    callable_func = cast(Callable[..., object], func)
    accepted_kwargs = _supported_kwargs(callable_func, kwargs)
    return callable_func(**accepted_kwargs)


def _supported_kwargs(
    func: Callable[..., object],
    kwargs: Mapping[str, object],
) -> dict[str, object]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


def _with_adapter_fallback_context(
    result: ParserRegistryResult,
    *,
    adapter_warnings: tuple[str, ...],
    adapter_fallback_used: bool,
) -> ParserRegistryResult:
    fallback_used = result.fallback_used or adapter_fallback_used
    if not adapter_warnings and fallback_used == result.fallback_used:
        return result
    return ParserRegistryResult(
        extraction=result.extraction,
        parser_backend=result.parser_backend,
        parser_version=result.parser_version,
        fallback_used=fallback_used,
        template=result.template,
        warnings=tuple(dict.fromkeys([*result.warnings, *adapter_warnings])),
        unsupported_reason=result.unsupported_reason,
    )


def _text_result(
    source_bytes: bytes,
    *,
    parser_profile: str,
    source_profile: SourceProfile | None = None,
    content_type: str = "",
) -> ParserRegistryResult:
    text = _decode_text_bytes(source_bytes)
    if _is_delimited_table_source(source_profile=source_profile, content_type=content_type):
        delimited = _delimited_table_extraction(
            text,
            source_profile=source_profile,
            content_type=content_type,
            parser_profile=parser_profile,
        )
        if delimited is not None:
            return ParserRegistryResult(
                extraction=delimited,
                parser_backend="local_partition",
                template="table_preserve_rows",
            )
    extraction = _structured_from_text(
        text,
        document_type="テキスト文書",
        source_parser=parser_profile,
        template="markdown_by_heading" if _looks_like_markdown(text) else "text_blocks",
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="local_partition",
        template="markdown_by_heading" if _looks_like_markdown(text) else "text_blocks",
    )


def _html_result(source_bytes: bytes) -> ParserRegistryResult:
    parser = _TextHTMLParser()
    parser.feed(_decode_text_bytes(source_bytes))
    extraction = _structured_from_html_blocks(parser.blocks(), parser.tables())
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="local_partition",
        template="html_semantic",
    )


def _structured_from_html_blocks(
    blocks: Sequence[_HTMLBlock],
    tables: Sequence[_HTMLTable] = (),
) -> StructuredExtraction:
    """HTML heading / section lineage を保持する構造化抽出へ変換する。"""
    elements: list[DocumentElement] = []
    extraction_tables: list[ExtractionTable] = []
    extraction_assets: list[ExtractionAsset] = []
    raw_parts: list[str] = []
    section_path: list[str] = []
    table_index = 0
    last_figure_element_id: str | None = None
    for order, block in enumerate(blocks):
        if block.tag == "table":
            last_figure_element_id = None
            if table_index >= len(tables):
                continue
            table = tables[table_index]
            table_index += 1
            table_id = f"html-table-{table_index - 1:04d}"
            table_cells = _table_cells_from_html_table(table)
            markdown = _html_table_markdown(table)
            if not markdown.strip():
                continue
            table_text = _table_text_with_caption(markdown, table.caption)
            row_count, column_count = _table_shape_from_cells(table_cells)
            metadata: dict[str, ExtractionMetadataValue] = {
                "source_parser": "local_html_semantic",
                "parser_backend": "local_partition",
                "parser_version": LOCAL_PARSER_VERSION,
                "chunk_template": "html_semantic",
                "html_tag": "table",
                "row_count": row_count,
                "column_count": column_count,
            }
            if table.caption:
                metadata["table_caption"] = table.caption
            raw_parts.append(table_text)
            elements.append(
                DocumentElement(
                    kind="table",
                    text=table_text,
                    order=len(elements),
                    element_id=table_id,
                    content_kind="table",
                    source_parser="local_html_semantic",
                    page_number=1,
                    section_path=list(section_path),
                    confidence=1.0,
                    metadata=metadata,
                )
            )
            extraction_tables.append(
                ExtractionTable(
                    table_id=table_id,
                    element_id=table_id,
                    page_number=1,
                    caption=table.caption,
                    cells=table_cells,
                    # セルは HTML の表の格子（span と th の表頭の印つき。#3429）。
                    metadata={**metadata, PARSED_TABLE_GRID_KEY: PARSED_TABLE_GRID_VERSION},
                )
            )
            continue
        heading_level = _html_heading_level(block.tag)
        element_id = f"html-{order:04d}"
        parent_id: str | None = None
        if heading_level is not None:
            last_figure_element_id = None
            section_path = section_path[: heading_level - 1]
            section_path.append(block.text)
            kind = "title"
            content_kind = "text"
            raw_parts.append(f"{'#' * heading_level} {block.text}")
        elif block.tag == "figure":
            kind = "figure"
            content_kind = "figure"
            last_figure_element_id = element_id
            raw_parts.append(block.text)
        elif block.tag == "figcaption":
            kind = "figure_caption"
            content_kind = "figure"
            parent_id = last_figure_element_id
            last_figure_element_id = None
            raw_parts.append(block.text)
        elif block.tag == "code":
            last_figure_element_id = None
            kind = "code"
            content_kind = "code"
            raw_parts.append(block.text)
        else:
            last_figure_element_id = None
            kind = "list" if block.tag == "li" else "text"
            content_kind = "list" if block.tag == "li" else "text"
            raw_parts.append(block.text)
        link_metadata = _html_link_metadata(block.links)
        asset_metadata = _html_image_block_metadata(block.images)
        element_metadata: dict[str, ExtractionMetadataValue] = {
            "source_parser": "local_html_semantic",
            "parser_backend": "local_partition",
            "chunk_template": "html_semantic",
            "html_tag": block.tag,
            **link_metadata,
            **asset_metadata,
        }
        if block.code_language is not None:
            element_metadata["code_language"] = block.code_language
        for image in block.images:
            extraction_assets.append(
                _asset_from_html_image(
                    image,
                    element_id=element_id,
                    page_number=1,
                )
            )
        elements.append(
            DocumentElement(
                kind=kind,
                text=block.text,
                order=order,
                element_id=element_id,
                parent_id=parent_id,
                content_kind=content_kind,
                source_parser="local_html_semantic",
                page_number=1,
                section_path=list(section_path),
                confidence=1.0,
                metadata=element_metadata,
            )
        )
    return StructuredExtraction(
        raw_text=_clean_text("\n\n".join(raw_parts)),
        document_type="HTML",
        confidence=1.0 if elements else 0.0,
        elements=elements,
        tables=extraction_tables,
        assets=extraction_assets,
        parser_artifacts={
            "source_parser": "local_html_semantic",
            "chunk_template": "html_semantic",
            "parser_backend": "local_partition",
            "parser_version": LOCAL_PARSER_VERSION,
            "html_block_count": len(elements),
            "table_count": len(extraction_tables),
            "asset_count": len(extraction_assets),
        },
    )


def _html_image_block_metadata(
    images: Sequence[_HTMLImage],
) -> dict[str, ExtractionMetadataValue]:
    if not images:
        return {}
    metadata: dict[str, ExtractionMetadataValue] = {"asset_count": len(images)}
    asset_ids = [image.asset_id for image in images]
    if len(asset_ids) == 1:
        metadata["asset_id"] = asset_ids[0]
    else:
        metadata["asset_ids"] = "\n".join(asset_ids)[:1000]
    return metadata


def _asset_from_html_image(
    image: _HTMLImage,
    *,
    element_id: str,
    page_number: int,
) -> ExtractionAsset:
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": "local_html_semantic",
        "parser_backend": "local_partition",
        "parser_version": LOCAL_PARSER_VERSION,
        "chunk_template": "html_semantic",
        "element_id": element_id,
    }
    if image.src is not None:
        metadata["source_url"] = image.src
    if image.title is not None:
        metadata["title"] = image.title
    return ExtractionAsset(
        asset_id=image.asset_id,
        kind="image",
        page_number=page_number,
        alt_text=image.alt_text or image.title or image.src,
        metadata=metadata,
    )


def _html_link_metadata(
    links: Sequence[_HTMLLink],
) -> dict[str, ExtractionMetadataValue]:
    if not links:
        return {}
    urls = _dedupe_adapter_labels([link.url for link in links])
    texts = _dedupe_adapter_labels([link.text for link in links])
    metadata: dict[str, ExtractionMetadataValue] = {"link_count": len(urls)}
    if urls:
        metadata["link_urls"] = "\n".join(urls)[:1000]
    if texts:
        metadata["link_texts"] = "\n".join(texts)[:1000]
    return metadata


MARKDOWN_INLINE_LINK = re.compile(r"(?<!!)\[([^\]\n]+)]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MARKDOWN_REFERENCE_LINK = re.compile(r"(?<!!)\[([^\]\n]+)]\[([^\]\n]*)]")
MARKDOWN_INLINE_IMAGE = re.compile(r"!\[([^\]\n]*)]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")
MARKDOWN_REFERENCE_IMAGE = re.compile(r"!\[([^\]\n]*)]\[([^\]\n]*)]")
MARKDOWN_REFERENCE_DEF = re.compile(r"^\s{0,3}\[([^\]\n]+)]:\s*(\S+)", re.MULTILINE)
MARKDOWN_AUTOLINK = re.compile(r"<(https?://[^>\s]+|mailto:[^>\s]+)>")


def _markdown_reference_links(text: str) -> dict[str, str]:
    references: dict[str, str] = {}
    for match in MARKDOWN_REFERENCE_DEF.finditer(text):
        url = _safe_link_url(match.group(2))
        if url is None:
            continue
        references[_markdown_reference_key(match.group(1))] = url
    return references


def _markdown_link_metadata(
    text: str,
    *,
    reference_links: Mapping[str, str],
) -> dict[str, ExtractionMetadataValue]:
    links: list[_HTMLLink] = []
    for label, raw_url in MARKDOWN_INLINE_LINK.findall(text):
        url = _safe_link_url(raw_url)
        if url is not None:
            links.append(_HTMLLink(text=_clean_text(label), url=url))
    for label, raw_reference in MARKDOWN_REFERENCE_LINK.findall(text):
        reference = raw_reference or label
        url = reference_links.get(_markdown_reference_key(reference))
        if url is not None:
            links.append(_HTMLLink(text=_clean_text(label), url=url))
    for raw_url in MARKDOWN_AUTOLINK.findall(text):
        url = _safe_link_url(raw_url)
        if url is not None:
            links.append(_HTMLLink(text=url, url=url))
    links.extend(_markdown_image_links(text, reference_links=reference_links))
    return _html_link_metadata(links)


def _markdown_image_links(
    text: str,
    *,
    reference_links: Mapping[str, str],
) -> list[_HTMLLink]:
    links: list[_HTMLLink] = []
    for label, raw_url, raw_title in MARKDOWN_INLINE_IMAGE.findall(text):
        url = _safe_link_url(raw_url)
        if url is not None:
            links.append(
                _HTMLLink(
                    text=_markdown_image_label(label, raw_title, url),
                    url=url,
                )
            )
    for label, raw_reference in MARKDOWN_REFERENCE_IMAGE.findall(text):
        reference = raw_reference or label
        url = reference_links.get(_markdown_reference_key(reference))
        if url is not None:
            links.append(_HTMLLink(text=_markdown_image_label(label, None, url), url=url))
    return links


def _markdown_image_label(label: str, title: str | None, url: str) -> str:
    for value in (label, title, url):
        if value and value.strip():
            return _clean_text(value)
    return url


def _markdown_reference_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


ADAPTER_LINK_COLLECTION_FIELDS = (
    "links",
    "references",
    "citations",
    "urls",
    "hrefs",
    "link_urls",
    "reference_urls",
)
ADAPTER_LINK_SCALAR_FIELDS = (
    "url",
    "uri",
    "href",
    "link",
    "link_url",
    "source_url",
    "reference_url",
)
ADAPTER_LINK_TEXT_FIELDS = ("text", "title", "label", "anchor", "caption", "name")


def _adapter_link_metadata(
    item: object,
    *,
    fallback_text: str,
) -> dict[str, ExtractionMetadataValue]:
    metadata = _object_member(item, "metadata")
    links: list[_HTMLLink] = []
    for field_name in ADAPTER_LINK_COLLECTION_FIELDS:
        for value in (
            _object_member(item, field_name),
            _metadata_get(metadata, field_name),
        ):
            links.extend(_adapter_links_from_value(value))
    fallback_label = _adapter_link_text_label(fallback_text)
    for field_name in ADAPTER_LINK_SCALAR_FIELDS:
        for value in (
            _object_member(item, field_name),
            _metadata_get(metadata, field_name),
        ):
            links.extend(_adapter_links_from_value(value, fallback_text=fallback_label))
    return _html_link_metadata(links)


def _adapter_links_from_value(
    value: object,
    *,
    fallback_text: str | None = None,
) -> list[_HTMLLink]:
    if value is None:
        return []
    if isinstance(value, str):
        url = _safe_link_url(value)
        return [_HTMLLink(text=fallback_text or url, url=url)] if url is not None else []
    if isinstance(value, Mapping):
        return _adapter_links_from_mapping(value, fallback_text=fallback_text)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        links: list[_HTMLLink] = []
        for item in value:
            links.extend(_adapter_links_from_value(item, fallback_text=fallback_text))
        return links
    mapped = _mapping_from_object(value)
    if mapped:
        return _adapter_links_from_mapping(mapped, fallback_text=fallback_text)
    return []


def _adapter_links_from_mapping(
    value: Mapping[str, object],
    *,
    fallback_text: str | None,
) -> list[_HTMLLink]:
    lowered = {str(key).strip().casefold(): item for key, item in value.items()}
    for field_name in ADAPTER_LINK_SCALAR_FIELDS:
        if field_name not in lowered:
            continue
        raw_url = lowered[field_name]
        if not isinstance(raw_url, str):
            continue
        url = _safe_link_url(raw_url)
        if url is None:
            continue
        label = _adapter_link_mapping_text(lowered) or fallback_text or url
        return [_HTMLLink(text=label, url=url)]
    links: list[_HTMLLink] = []
    for field_name in ADAPTER_LINK_COLLECTION_FIELDS:
        if field_name in lowered:
            links.extend(
                _adapter_links_from_value(
                    lowered[field_name],
                    fallback_text=fallback_text,
                )
            )
    return links


def _adapter_link_mapping_text(value: Mapping[str, object]) -> str | None:
    for field_name in ADAPTER_LINK_TEXT_FIELDS:
        candidate = value.get(field_name)
        if isinstance(candidate, str) and candidate.strip():
            return _adapter_link_text_label(candidate)
    return None


def _adapter_link_text_label(value: str) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= 120:
        return cleaned
    return cleaned[:117].rstrip() + "..."


def _html_heading_level(tag: str) -> int | None:
    if len(tag) == 2 and tag.startswith("h") and tag[1].isdigit():
        level = int(tag[1])
        if 1 <= level <= 6:
            return level
    return None


def _email_result(source_bytes: bytes) -> ParserRegistryResult:
    message = BytesParser(policy=policy.default).parsebytes(source_bytes)
    header_values = _email_header_values(message)
    headers = [f"{label}: {value}" for label, value in header_values.items()]
    body = _email_body_text(message)
    text = _clean_text("\n".join([*headers, "", body]))
    attachments = list(message.iter_attachments())
    elements = _email_elements(headers=headers, body=body, header_values=header_values)
    assets = _email_attachment_assets(attachments)
    extraction = StructuredExtraction(
        raw_text=text,
        document_type="メール",
        confidence=1.0 if text else 0.0,
        elements=elements,
        assets=assets,
        parser_artifacts={
            "source_parser": "local_email_thread",
            "chunk_template": "email_thread",
            "parser_backend": "local_partition",
            "parser_version": LOCAL_PARSER_VERSION,
            "subject_chars": len(header_values["Subject"]),
            "attachment_count": len(assets),
            "has_body": bool(body.strip()),
        },
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="local_partition",
        template="email_thread",
    )


def _email_header_values(message: object) -> dict[str, str]:
    return {
        "Subject": _email_header_value(message, "subject"),
        "From": _email_header_value(message, "from"),
        "To": _email_header_value(message, "to"),
        "Date": _email_header_value(message, "date"),
    }


def _email_header_value(message: object, key: str) -> str:
    get_header = getattr(message, "get", None)
    if callable(get_header):
        value = get_header(key, "")
        return str(value or "").strip()
    return ""


def _email_elements(
    *,
    headers: Sequence[str],
    body: str,
    header_values: Mapping[str, str],
) -> list[DocumentElement]:
    metadata = {
        "source_parser": "local_email_thread",
        "parser_backend": "local_partition",
        "chunk_template": "email_thread",
    }
    elements = [
        DocumentElement(
            kind="text",
            text=_clean_text("\n".join(headers)),
            order=0,
            element_id="email-headers",
            content_kind="email",
            source_parser="local_email_thread",
            page_number=1,
            confidence=1.0,
            metadata={
                **metadata,
                "email_part": "headers",
                "subject_chars": len(header_values.get("Subject", "")),
                "from_present": bool(header_values.get("From")),
                "to_present": bool(header_values.get("To")),
                "date_present": bool(header_values.get("Date")),
            },
        )
    ]
    if body.strip():
        elements.append(
            DocumentElement(
                kind="text",
                text=_clean_text(body),
                order=1,
                element_id="email-body",
                content_kind="email",
                source_parser="local_email_thread",
                page_number=1,
                confidence=1.0,
                metadata={
                    **metadata,
                    "email_part": "body",
                },
            )
        )
    return elements


def _email_attachment_assets(attachments: Sequence[object]) -> list[ExtractionAsset]:
    assets: list[ExtractionAsset] = []
    for index, attachment in enumerate(attachments):
        filename = _email_attachment_filename(attachment, index)
        content_type = _email_attachment_content_type(attachment)
        size_bytes = _email_attachment_size_bytes(attachment)
        metadata: dict[str, ExtractionMetadataValue] = {
            "source_parser": "local_email_thread",
            "parser_backend": "local_partition",
            "chunk_template": "email_thread",
            "attachment_index": index,
            "file_name": filename,
            "content_type": content_type,
        }
        if size_bytes is not None:
            metadata["size_bytes"] = size_bytes
        assets.append(
            ExtractionAsset(
                asset_id=f"email-attachment-{index:04d}",
                kind="email_attachment",
                page_number=1,
                alt_text=filename,
                metadata=metadata,
            )
        )
    return assets


def _email_attachment_filename(attachment: object, index: int) -> str:
    get_filename = getattr(attachment, "get_filename", None)
    if callable(get_filename):
        filename = get_filename()
        if isinstance(filename, str) and filename.strip():
            return filename.strip()[:255]
    return f"attachment-{index + 1}"


def _email_attachment_content_type(attachment: object) -> str:
    get_content_type = getattr(attachment, "get_content_type", None)
    if callable(get_content_type):
        content_type = get_content_type()
        if isinstance(content_type, str) and content_type.strip():
            return content_type.strip()[:128]
    return "application/octet-stream"


def _email_attachment_size_bytes(attachment: object) -> int | None:
    get_payload = getattr(attachment, "get_payload", None)
    if callable(get_payload):
        payload = get_payload(decode=True)
        if isinstance(payload, bytes):
            return len(payload)
    get_content = getattr(attachment, "get_content", None)
    if callable(get_content):
        try:
            content = get_content()
        except Exception:
            return None
        if isinstance(content, bytes):
            return len(content)
        if isinstance(content, str):
            return len(content.encode("utf-8"))
    return None


def _office_result(
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
) -> ParserRegistryResult:
    office_kind = _office_kind(source_profile)
    tables: list[ExtractionTable] = []
    formula_elements: list[DocumentElement] = []
    if office_kind is None:
        return ParserRegistryResult(
            extraction=None,
            parser_backend="enterprise_ai",
            fallback_used=True,
            warnings=("unsupported_legacy_office_binary",),
        )
    try:
        with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
            if office_kind == "docx":
                text, tables = _office_docx_text_and_tables(archive)
                template = "office_document"
                document_type = "Word 文書"
                content_kind = "text"
            elif office_kind == "pptx":
                text, tables = _office_pptx_text_and_tables(archive)
                template = "office_slide"
                document_type = "PowerPoint"
                content_kind = "slide"
            else:
                text, tables, formula_elements = _office_xlsx_text_and_tables(archive)
                template = "office_sheet"
                document_type = "Excel"
                content_kind = "sheet"
    except Exception:
        return ParserRegistryResult(
            extraction=None,
            parser_backend="enterprise_ai",
            fallback_used=True,
            warnings=("office_local_parse_failed",),
        )
    if not text.strip():
        return ParserRegistryResult(
            extraction=None,
            parser_backend="enterprise_ai",
            fallback_used=True,
            warnings=("office_local_parse_empty",),
        )
    extraction = _structured_from_text(
        text,
        document_type=document_type,
        source_parser="local_office_structure",
        template=template,
        default_content_kind=content_kind,
    )
    if tables or formula_elements:
        table_matches = _office_table_element_matches(tables)
        elements: list[DocumentElement] = []
        for element in extraction.elements:
            metadata = dict(element.metadata)
            update: dict[str, object] = {"metadata": metadata}
            if element.content_kind == "table" or element.kind == "table":
                match = table_matches.get(element.text.strip())
                if match is not None:
                    metadata.update(match.metadata)
                    if match.element_id:
                        update["element_id"] = match.element_id
                    if match.page_number is not None:
                        update["page_number"] = match.page_number
            elements.append(element.model_copy(update=update))
        elements.extend(formula_elements)
        payload = extraction.to_document_payload()
        payload["elements"] = [element.to_payload() for element in elements]
        payload["tables"] = [table.model_dump(exclude_none=True) for table in tables]
        payload["parser_artifacts"] = {
            **extraction.parser_artifacts,
            "table_count": len(tables),
            "formula_count": len(formula_elements),
        }
        extraction = StructuredExtraction.model_validate(payload)
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend="local_partition",
        template=template,
    )


def parse_openxml_office_segment_extractions(
    source_bytes: bytes,
    *,
    source_profile: SourceProfile | None,
) -> OfficeSegmentParseResult:
    """OpenXML PPTX/XLSX を slide/sheet 単位の StructuredExtraction にする。"""
    office_kind = _office_kind(source_profile)
    if office_kind not in {"pptx", "xlsx"}:
        return OfficeSegmentParseResult()
    try:
        with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
            if office_kind == "pptx":
                return _pptx_segment_parse_result(archive)
            return _xlsx_segment_parse_result(archive)
    except Exception:
        return OfficeSegmentParseResult()


def _office_kind(source_profile: SourceProfile | None) -> str | None:
    """拡張子または MIME type から OpenXML Office 種別を返す。"""
    extension = (source_profile.extension if source_profile is not None else "") or ""
    content_type = (source_profile.content_type if source_profile is not None else "") or ""
    if extension == ".docx" or content_type.endswith("officedocument.wordprocessingml.document"):
        return "docx"
    if extension == ".pptx" or content_type.endswith("officedocument.presentationml.presentation"):
        return "pptx"
    if extension == ".xlsx" or content_type.endswith("officedocument.spreadsheetml.sheet"):
        return "xlsx"
    return None


def _pptx_segment_parse_result(
    archive: zipfile.ZipFile,
) -> OfficeSegmentParseResult:
    segments: list[OfficeSegmentExtraction] = []
    failures: list[OfficeSegmentFailure] = []
    for number, name in _openxml_numbered_members(archive, r"ppt/slides/slide(\d+)\.xml"):
        try:
            text, tables = _office_pptx_slide_text_and_tables(
                archive,
                name,
                slide_number=number,
            )
        except Exception:
            failures.append(
                OfficeSegmentFailure(
                    segment_kind="slide",
                    number=number,
                    source_path=name,
                )
            )
            continue
        if not text.strip():
            continue
        segments.append(
            OfficeSegmentExtraction(
                segment_kind="slide",
                number=number,
                source_path=name,
                extraction=_structured_office_segment(
                    text,
                    number=number,
                    source_path=name,
                    document_type="PowerPoint",
                    template="office_slide",
                    content_kind="slide",
                    tables=tables,
                ),
            )
        )
    return OfficeSegmentParseResult(segments=tuple(segments), failures=tuple(failures))


def _xlsx_segment_parse_result(
    archive: zipfile.ZipFile,
) -> OfficeSegmentParseResult:
    try:
        shared_strings = _xlsx_shared_strings(archive)
    except Exception:
        shared_strings = {}
    segments: list[OfficeSegmentExtraction] = []
    failures: list[OfficeSegmentFailure] = []
    for number, name in _openxml_numbered_members(archive, r"xl/worksheets/sheet(\d+)\.xml"):
        try:
            text, table, formula_elements = _office_xlsx_sheet_text_and_table(
                archive,
                name,
                shared_strings=shared_strings,
                sheet_number=number,
            )
        except Exception:
            failures.append(
                OfficeSegmentFailure(
                    segment_kind="sheet",
                    number=number,
                    source_path=name,
                )
            )
            continue
        if not text.strip():
            continue
        segments.append(
            OfficeSegmentExtraction(
                segment_kind="sheet",
                number=number,
                source_path=name,
                extraction=_structured_office_segment(
                    text,
                    number=number,
                    source_path=name,
                    document_type="Excel",
                    template="office_sheet",
                    content_kind="sheet",
                    tables=[table] if table is not None else [],
                    extra_elements=formula_elements,
                ),
            )
        )
    return OfficeSegmentParseResult(segments=tuple(segments), failures=tuple(failures))


def _openxml_numbered_members(
    archive: zipfile.ZipFile,
    pattern: str,
) -> list[tuple[int, str]]:
    regex = re.compile(pattern)
    members: list[tuple[int, str]] = []
    for name in archive.namelist():
        match = regex.fullmatch(name)
        if match:
            members.append((int(match.group(1)), name))
    return sorted(members)


def _structured_office_segment(
    text: str,
    *,
    number: int,
    source_path: str,
    document_type: str,
    template: str,
    content_kind: str,
    tables: Sequence[ExtractionTable] = (),
    extra_elements: Sequence[DocumentElement] = (),
) -> StructuredExtraction:
    extraction = _structured_from_text(
        text,
        document_type=document_type,
        source_parser="local_office_structure",
        template=template,
        default_content_kind=content_kind,
        extra_artifacts={
            "office_segment_number": number,
            "office_segment_path": source_path,
        },
    )
    table_matches = _office_table_element_matches(tables)
    elements: list[DocumentElement] = []
    for element in extraction.elements:
        metadata = {
            **element.metadata,
            "office_segment_number": number,
            "office_segment_path": source_path,
        }
        update: dict[str, object] = {
            "page_number": number,
            "metadata": metadata,
        }
        if element.content_kind == "table" or element.kind == "table":
            match = table_matches.get(element.text.strip())
            if match is not None:
                metadata.update(match.metadata)
                if match.element_id:
                    update["element_id"] = match.element_id
        elements.append(element.model_copy(update=update))
    for element in extra_elements:
        metadata = {
            **element.metadata,
            "office_segment_number": number,
            "office_segment_path": source_path,
        }
        elements.append(
            element.model_copy(
                update={
                    "page_number": element.page_number or number,
                    "metadata": metadata,
                }
            )
        )
    payload = extraction.to_document_payload()
    payload["elements"] = [element.to_payload() for element in elements]
    payload["tables"] = [table.model_dump(exclude_none=True) for table in tables]
    payload["parser_artifacts"] = {
        **extraction.parser_artifacts,
        "table_count": len(tables),
        "formula_count": len(extra_elements),
    }
    payload["pages"] = []
    return StructuredExtraction.model_validate(payload)


def _office_table_element_matches(
    tables: Sequence[ExtractionTable],
) -> dict[str, _OfficeTableElementMatch]:
    """tables[] を inferred table element metadata へ戻すための lookup を作る。"""
    matches: dict[str, _OfficeTableElementMatch] = {}
    for table in tables:
        markdown = _table_markdown_from_cells(table.cells)
        if not markdown:
            continue
        row_count, column_count = _table_shape_from_cells(table.cells)
        metadata = dict(table.metadata)
        metadata["table_id"] = table.table_id
        if table.element_id:
            metadata["element_id"] = table.element_id
        configured_row_count = _int_value(metadata.get("row_count"))
        configured_column_count = _int_value(metadata.get("column_count"))
        metadata["row_count"] = configured_row_count or row_count
        metadata["column_count"] = configured_column_count or column_count
        matches[markdown] = _OfficeTableElementMatch(
            element_id=table.element_id or table.table_id,
            page_number=table.page_number,
            metadata=metadata,
        )
    return matches


def _table_markdown_from_cells(cells: Sequence[ExtractionTableCell]) -> str:
    return "\n".join(_xlsx_markdown_row(row) for row in _table_plain_rows_from_cells(cells))


def _table_plain_rows_from_cells(
    cells: Sequence[ExtractionTableCell],
) -> list[list[str]]:
    row_count, column_count = _table_shape_from_cells(cells)
    if row_count <= 0 or column_count <= 0:
        return []
    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    for cell in cells:
        if cell.row < row_count and cell.col < column_count:
            grid[cell.row][cell.col] = cell.text
    return [row for row in grid if any(value.strip() for value in row)]


def _table_shape_from_cells(cells: Sequence[ExtractionTableCell]) -> tuple[int, int]:
    if not cells:
        return 0, 0
    return (
        max(cell.row + cell.row_span for cell in cells),
        max(cell.col + cell.col_span for cell in cells),
    )


def _structured_from_text(
    text: str,
    *,
    document_type: str,
    source_parser: str,
    template: str,
    default_content_kind: str = "text",
    parser_backend: str = "local_partition",
    parser_version: str = LOCAL_PARSER_VERSION,
    extra_artifacts: Mapping[str, ExtractionMetadataValue] | None = None,
) -> StructuredExtraction:
    markdown_reference_links = _markdown_reference_links(text)
    extraction = StructuredExtraction(
        raw_text=text,
        document_type=document_type,
        confidence=1.0 if text.strip() else 0.0,
        warnings=[],
        parser_artifacts={
            "source_parser": source_parser,
            "chunk_template": template,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            **dict(extra_artifacts or {}),
        },
    )
    elements: list[DocumentElement] = []
    assets: list[ExtractionAsset] = []
    tables: list[ExtractionTable] = []
    markdown_image_count = 0
    markdown_table_count = 0
    source_elements = tuple(extraction.elements)
    for element_index, element in enumerate(source_elements):
        image_refs = _markdown_image_refs(
            element.text,
            reference_links=markdown_reference_links,
        )
        element_assets: list[ExtractionAsset] = []
        for image_ref in image_refs:
            asset = _asset_from_markdown_image(
                image_ref,
                order=markdown_image_count,
                element_id=element.element_id or f"el-{element.order:04d}",
                page_number=element.page_number,
                source_parser=source_parser,
                parser_backend=parser_backend,
                parser_version=parser_version,
                template=template,
            )
            markdown_image_count += 1
            element_assets.append(asset)
        assets.extend(element_assets)
        link_metadata = _markdown_link_metadata(
            element.text,
            reference_links=markdown_reference_links,
        )
        asset_metadata = _markdown_asset_metadata(element_assets)
        figure_text = _markdown_image_only_text(element.text, image_refs)
        kind = element.kind
        content_kind = default_content_kind if element.kind == "text" else element.content_kind
        text = element.text
        if figure_text is not None:
            kind = "figure"
            content_kind = "figure"
            text = figure_text
        metadata = {
            **element.metadata,
            "source_parser": source_parser,
            "parser_backend": parser_backend,
            "chunk_template": template,
            **link_metadata,
            **asset_metadata,
        }
        if kind == "table":
            caption = _markdown_table_caption_before(source_elements, element_index)
            if caption is not None:
                text = _table_text_with_caption(text, caption)
            table = _markdown_table_from_element(
                element.model_copy(update={"text": text}),
                table_index=markdown_table_count,
                caption=caption,
                source_parser=source_parser,
                parser_backend=parser_backend,
                parser_version=parser_version,
                template=template,
            )
            if table is not None:
                markdown_table_count += 1
                tables.append(table)
                row_count, column_count = _table_shape_from_cells(table.cells)
                metadata.setdefault("table_id", table.table_id)
                metadata.setdefault("row_count", row_count)
                metadata.setdefault("column_count", column_count)
                metadata["table_source"] = "markdown_table"
                if caption is not None:
                    metadata["table_caption"] = caption
        elements.append(
            element.model_copy(
                update={
                    "kind": kind,
                    "text": text,
                    "source_parser": source_parser,
                    "content_kind": content_kind,
                    "metadata": metadata,
                }
            )
        )
    existing_table_count = _int_value(extraction.parser_artifacts.get("table_count")) or 0
    artifacts = {
        **extraction.parser_artifacts,
        "table_count": max(existing_table_count, markdown_table_count),
    }
    return extraction.model_copy(
        update={
            "elements": elements,
            "tables": tables,
            "assets": assets,
            "parser_artifacts": artifacts,
        }
    )


def _markdown_table_from_element(
    element: DocumentElement,
    *,
    table_index: int,
    caption: str | None,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
    template: str,
) -> ExtractionTable | None:
    cells = _table_cells_from_adapter_text(element.text)
    if not cells:
        return None
    table_id = _metadata_table_id(element.metadata) or f"markdown-table-{table_index:04d}"
    row_count, column_count = _table_shape_from_cells(cells)
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": source_parser,
        "parser_backend": parser_backend,
        "parser_version": parser_version,
        "chunk_template": template,
        "table_source": "markdown_table",
        "row_count": row_count,
        "column_count": column_count,
    }
    if caption is not None:
        metadata["table_caption"] = caption
    for key in (
        "table_cross_page",
        "table_page_start",
        "table_page_end",
        "table_page_count",
        "table_continuation_index",
        "table_continuation_count",
        "table_data_row_offset",
    ):
        value = element.metadata.get(key)
        if value is not None:
            metadata[key] = value
    return ExtractionTable(
        table_id=table_id,
        element_id=element.element_id or _metadata_table_id(element.metadata),
        page_number=element.page_number,
        caption=caption,
        cells=cells,
        metadata=metadata,
    )


def _markdown_table_caption_before(
    elements: Sequence[DocumentElement],
    table_index: int,
) -> str | None:
    if table_index <= 0:
        return None
    table = elements[table_index]
    previous = elements[table_index - 1]
    if previous.kind != "text" or table.page_number != previous.page_number:
        return None
    if previous.section_path != table.section_path:
        return None
    text = _clean_text(previous.text)
    if "\n" in text or len(text) > 160:
        return None
    if _looks_like_markdown_table_caption(text):
        return text
    return None


def _looks_like_markdown_table_caption(text: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:table\s*\d*|tbl\.?\s*\d*|表\s*\d*|表[一二三四五六七八九十百千]+)"
            r"[\s:：.．、-]+",
            text,
            flags=re.IGNORECASE,
        )
    )


def _metadata_table_id(metadata: Mapping[str, object]) -> str | None:
    value = metadata.get("table_id")
    if isinstance(value, str | int) and str(value).strip():
        return str(value).strip()[:128]
    return None


def _markdown_image_refs(
    text: str,
    *,
    reference_links: Mapping[str, str],
) -> list[_HTMLImage]:
    images: list[_HTMLImage] = []
    for label, raw_url, raw_title in MARKDOWN_INLINE_IMAGE.findall(text):
        url = _safe_link_url(raw_url)
        if url is None:
            continue
        images.append(
            _HTMLImage(
                asset_id="",
                src=url,
                alt_text=_markdown_image_label(label, raw_title, url),
                title=(_clean_text(raw_title) if raw_title and raw_title.strip() else None),
            )
        )
    for label, raw_reference in MARKDOWN_REFERENCE_IMAGE.findall(text):
        reference = raw_reference or label
        url = reference_links.get(_markdown_reference_key(reference))
        if url is None:
            continue
        images.append(
            _HTMLImage(
                asset_id="",
                src=url,
                alt_text=_markdown_image_label(label, None, url),
            )
        )
    return images


def _asset_from_markdown_image(
    image: _HTMLImage,
    *,
    order: int,
    element_id: str,
    page_number: int | None,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
    template: str,
) -> ExtractionAsset:
    asset_id = f"markdown-image-{order:04d}"
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": source_parser,
        "parser_backend": parser_backend,
        "parser_version": parser_version,
        "chunk_template": template,
        "element_id": element_id,
    }
    if image.src is not None:
        metadata["source_url"] = image.src
    if image.title is not None:
        metadata["title"] = image.title
    return ExtractionAsset(
        asset_id=asset_id,
        kind="image",
        page_number=page_number,
        alt_text=image.alt_text or image.title or image.src,
        metadata=metadata,
    )


def _markdown_asset_metadata(
    assets: Sequence[ExtractionAsset],
) -> dict[str, ExtractionMetadataValue]:
    if not assets:
        return {}
    asset_ids = [asset.asset_id for asset in assets]
    metadata: dict[str, ExtractionMetadataValue] = {"asset_count": len(asset_ids)}
    if len(asset_ids) == 1:
        metadata["asset_id"] = asset_ids[0]
    else:
        metadata["asset_ids"] = "\n".join(asset_ids)[:1000]
    return metadata


def _markdown_image_only_text(text: str, images: Sequence[_HTMLImage]) -> str | None:
    if not images:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != len(images):
        return None
    if not all(
        MARKDOWN_INLINE_IMAGE.fullmatch(line) or MARKDOWN_REFERENCE_IMAGE.fullmatch(line)
        for line in lines
    ):
        return None
    labels = [image.alt_text or image.title or image.src or "" for image in images]
    return "\n".join(label for label in labels if label).strip() or None


def _structured_from_adapter_elements(
    elements: Sequence[object],
    *,
    document_type: str,
    source_parser: str,
    template: str,
    parser_backend: str,
    parser_version: str,
    pages: Sequence[ExtractionPage] | None = None,
    extra_artifacts: Mapping[str, ExtractionMetadataValue] | None = None,
) -> StructuredExtraction:
    mapped_elements: list[DocumentElement] = []
    mapped_tables: list[ExtractionTable] = []
    mapped_assets: list[ExtractionAsset] = []
    section_stack: list[str] = []
    last_figure_element_id: str | None = None
    last_table_element_id: str | None = None
    adapter_pages = tuple(pages or ())
    page_metadata = {page.page_number: page for page in adapter_pages}
    adapter_page_break_count = sum(
        1 for item in elements if _adapter_element_kind(item) == "page_break"
    )
    inferred_page_number: int | None = 1 if adapter_page_break_count else None
    for order, item in enumerate(elements):
        kind = _adapter_element_kind(item)
        if kind == "page_break":
            inferred_page_number = _adapter_page_break_next_page(
                item,
                current_page=inferred_page_number,
            )
            continue
        content_kind = _content_kind_for_adapter_kind(kind)
        adapter_cells = _adapter_table_cells(item) if content_kind == "table" else []
        adapter_rows = (
            [] if adapter_cells else (_adapter_table_rows(item) if content_kind == "table" else [])
        )
        text = _adapter_element_text(item)
        if not text and kind == "figure":
            text = _adapter_asset_alt_text(item)
        if adapter_cells:
            text = _table_markdown_from_cells(adapter_cells)
        if adapter_rows:
            text = "\n".join(_xlsx_markdown_row(row) for row in adapter_rows)
        element_id = _adapter_element_id(item, order)
        page_number = _adapter_element_page_number(item)
        if page_number is None:
            page_number = inferred_page_number
        elif inferred_page_number is not None:
            inferred_page_number = page_number
        bbox = _adapter_element_bbox(item)
        if not text:
            adapter_asset = _asset_from_adapter_element(
                item,
                kind=kind,
                text="",
                order=order,
                element_id=element_id,
                page_number=page_number,
                bbox=bbox,
                source_parser=source_parser,
                parser_backend=parser_backend,
                parser_version=parser_version,
            )
            if adapter_asset is not None:
                mapped_assets.append(adapter_asset)
            continue
        parent_id = _adapter_element_parent_id(item)
        if parent_id is None and kind == "figure_caption":
            parent_id = last_figure_element_id
        if parent_id is None and kind == "table_caption":
            parent_id = last_table_element_id
        confidence = _adapter_element_confidence(item)
        metadata = _adapter_element_metadata(item)
        for key, value in _adapter_link_metadata(item, fallback_text=text).items():
            metadata.setdefault(key, value)
        explicit_section_path = _adapter_element_section_path(item)
        if kind == "title" and not explicit_section_path:
            level = _adapter_element_section_level(item) or 1
            section_stack = section_stack[: level - 1]
            section_stack.append(text)
            section_path = list(section_stack)
        elif explicit_section_path:
            section_path = explicit_section_path
            if kind == "title":
                section_stack = list(explicit_section_path)
        else:
            section_path = list(section_stack)
        for key, value in _adapter_bbox_lineage_metadata(bbox).items():
            metadata.setdefault(key, value)
        # bbox の座標系の寸法(Unstructured の coordinates の layout_width / height)は、ページの
        # 寸法より正確に bbox の基準を表すので先に入れる(解析後の Vision の切り出し。#497)。
        for key, value in _adapter_coordinate_space(item).items():
            metadata.setdefault(key, value)
        if page_number is not None and page_number in page_metadata:
            page = page_metadata[page_number]
            if page.width is not None:
                metadata.setdefault("page_width", page.width)
            if page.height is not None:
                metadata.setdefault("page_height", page.height)
            if page.rotation is not None:
                metadata.setdefault("page_rotation", page.rotation)
        if kind == "equation":
            for key, value in _adapter_equation_metadata(item).items():
                metadata.setdefault(key, value)
        if kind == "code":
            for key, value in _adapter_code_metadata(item).items():
                metadata.setdefault(key, value)
        metadata.update(
            {
                "source_parser": source_parser,
                "parser_backend": parser_backend,
                "chunk_template": template,
                "adapter_element_type": kind,
            }
        )
        if raw_kind := _adapter_raw_element_kind(item):
            metadata.setdefault("adapter_raw_element_type", raw_kind)
        adapter_tables: list[ExtractionTable] = []
        if content_kind == "table":
            adapter_tables = (
                _adapter_table_from_cells(
                    adapter_cells,
                    element_id=element_id,
                    page_number=page_number,
                    source_parser=source_parser,
                    parser_backend=parser_backend,
                    parser_version=parser_version,
                    table_source="adapter_cells",
                )
                if adapter_cells
                else (
                    _tables_from_adapter_rows(
                        adapter_rows,
                        element_id=element_id,
                        page_number=page_number,
                        source_parser=source_parser,
                        parser_backend=parser_backend,
                        parser_version=parser_version,
                    )
                    if adapter_rows
                    else _tables_from_adapter_text(
                        text,
                        element_id=element_id,
                        page_number=page_number,
                        source_parser=source_parser,
                        parser_backend=parser_backend,
                        parser_version=parser_version,
                    )
                )
            )
            if adapter_tables and page_number is not None and page_number in page_metadata:
                page = page_metadata[page_number]
                adapter_tables = [
                    _adapter_table_with_page_metadata(table, page=page) for table in adapter_tables
                ]
            if adapter_tables:
                table = adapter_tables[0]
                row_count, column_count = _table_shape_from_cells(table.cells)
                metadata.setdefault("table_id", table.table_id)
                metadata.setdefault("row_count", row_count)
                metadata.setdefault("column_count", column_count)
                metadata.setdefault("parser_version", parser_version)
                table_source = table.metadata.get("table_source")
                if table_source is not None:
                    metadata.setdefault("table_source", table_source)
        adapter_asset = _asset_from_adapter_element(
            item,
            kind=kind,
            text=text,
            order=order,
            element_id=element_id,
            page_number=page_number,
            bbox=bbox,
            source_parser=source_parser,
            parser_backend=parser_backend,
            parser_version=parser_version,
        )
        if adapter_asset is not None:
            mapped_assets.append(adapter_asset)
            metadata.setdefault("asset_id", adapter_asset.asset_id)
            metadata.setdefault("asset_kind", adapter_asset.kind)
        mapped_elements.append(
            DocumentElement(
                kind=kind,
                text=text,
                order=order,
                element_id=element_id,
                parent_id=parent_id,
                content_kind=content_kind,
                source_parser=source_parser,
                page_number=page_number,
                bbox=bbox,
                section_path=section_path,
                confidence=confidence,
                metadata=metadata,
            )
        )
        mapped_tables.extend(adapter_tables)
        if kind == "figure":
            last_figure_element_id = element_id
        elif kind == "figure_caption":
            last_figure_element_id = None
        else:
            last_figure_element_id = None
        if kind == "table":
            last_table_element_id = element_id
        elif kind == "table_caption":
            last_table_element_id = None
        else:
            last_table_element_id = None

    mapped_elements, mapped_tables = _attach_adapter_table_captions(
        mapped_elements,
        mapped_tables,
    )
    raw_text = _clean_text(
        "\n\n".join(element.text for element in mapped_elements if element.text.strip())
    )

    extraction = StructuredExtraction(
        raw_text=raw_text,
        document_type=document_type,
        confidence=1.0 if raw_text else 0.0,
        warnings=[],
        elements=mapped_elements,
        pages=_adapter_pages_for_elements(adapter_pages, mapped_elements),
        tables=mapped_tables,
        assets=mapped_assets,
        parser_artifacts={
            "source_parser": source_parser,
            "chunk_template": template,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            "external_adapter": parser_backend,
            "adapter_element_count": len(mapped_elements),
            "adapter_table_count": len(mapped_tables),
            "adapter_asset_count": len(mapped_assets),
            "adapter_page_break_count": adapter_page_break_count,
            **dict(extra_artifacts or {}),
        },
    )
    return extraction


def _attach_adapter_table_captions(
    elements: Sequence[DocumentElement],
    tables: Sequence[ExtractionTable],
) -> tuple[list[DocumentElement], list[ExtractionTable]]:
    """adapter の TableCaption element を親 table / tables[] へ回填する。"""
    caption_by_parent: dict[str, DocumentElement] = {}
    for element in elements:
        if element.kind != "table_caption" or not element.parent_id or not element.text.strip():
            continue
        caption_by_parent.setdefault(element.parent_id, element)
    if not caption_by_parent:
        return list(elements), list(tables)

    table_id_by_parent: dict[str, str] = {}
    for table in tables:
        for key in (table.element_id, table.table_id):
            if key:
                table_id_by_parent.setdefault(key, table.table_id)

    updated_elements: list[DocumentElement] = []
    for element in elements:
        if element.kind == "table" and element.element_id in caption_by_parent:
            caption = caption_by_parent[element.element_id]
            table_id = table_id_by_parent.get(element.element_id, element.element_id)
            metadata = {
                **element.metadata,
                "table_caption": caption.text,
                "caption_element_id": caption.element_id,
            }
            if table_id:
                metadata.setdefault("table_id", table_id)
            updated_elements.append(
                element.model_copy(
                    update={
                        "text": _table_text_with_caption(element.text, caption.text),
                        "metadata": metadata,
                    }
                )
            )
            continue
        if element.kind == "table_caption" and element.parent_id:
            table_id = table_id_by_parent.get(element.parent_id, element.parent_id)
            updated_elements.append(
                element.model_copy(
                    update={
                        "metadata": {
                            **element.metadata,
                            "table_caption": element.text,
                            "table_id": table_id,
                        }
                    }
                )
            )
            continue
        updated_elements.append(element)

    updated_tables: list[ExtractionTable] = []
    for table in tables:
        caption_element: DocumentElement | None = None
        for key in (table.element_id, table.table_id):
            if key and key in caption_by_parent:
                caption_element = caption_by_parent[key]
                break
        if caption_element is None:
            updated_tables.append(table)
            continue
        updated_tables.append(
            table.model_copy(
                update={
                    "caption": table.caption or caption_element.text,
                    "metadata": {
                        **table.metadata,
                        "table_caption": table.caption or caption_element.text,
                        "caption_element_id": caption_element.element_id,
                    },
                }
            )
        )
    return updated_elements, updated_tables


def _delimited_table_extraction(
    text: str,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
    parser_profile: str,
) -> StructuredExtraction | None:
    delimiter = _delimiter_for_delimited_source(
        source_profile=source_profile,
        content_type=content_type,
    )
    rows = _delimited_rows(text, delimiter=delimiter)
    if not rows or max((len(row) for row in rows), default=0) <= 1:
        return None
    table_kind = "tsv" if delimiter == "\t" else "csv"
    table_id = f"{table_kind}-table-0000"
    markdown = "\n".join(_xlsx_markdown_row(row) for row in rows)
    column_count = max(len(row) for row in rows)
    cells = _table_cells_from_rows(rows)
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": parser_profile,
        "parser_backend": "local_partition",
        "parser_version": LOCAL_PARSER_VERSION,
        "chunk_template": TABLE_PRESERVE_ROWS_TEMPLATE,
        "table_format": table_kind,
        "row_count": len(rows),
        "column_count": column_count,
    }
    return StructuredExtraction(
        raw_text=markdown,
        document_type=table_kind.upper(),
        confidence=1.0,
        elements=[
            DocumentElement(
                kind="table",
                text=markdown,
                order=0,
                element_id=table_id,
                content_kind="table",
                source_parser=parser_profile,
                page_number=1,
                confidence=1.0,
                metadata=metadata,
            )
        ],
        tables=[
            ExtractionTable(
                table_id=table_id,
                element_id=table_id,
                page_number=1,
                cells=cells,
                metadata=metadata,
            )
        ],
        parser_artifacts={
            "source_parser": parser_profile,
            "chunk_template": TABLE_PRESERVE_ROWS_TEMPLATE,
            "parser_backend": "local_partition",
            "parser_version": LOCAL_PARSER_VERSION,
            "table_count": 1,
            "row_count": len(rows),
            "column_count": column_count,
            "table_format": table_kind,
        },
    )


def _is_delimited_table_source(
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> bool:
    extension = (source_profile.extension if source_profile is not None else "") or ""
    normalized_content_type = _normalized_content_type_for_parser(
        content_type or (source_profile.content_type if source_profile is not None else "")
    )
    return extension in {".csv", ".tsv"} or normalized_content_type in {
        "text/csv",
        "application/csv",
        "text/tab-separated-values",
    }


def _delimiter_for_delimited_source(
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> str:
    extension = (source_profile.extension if source_profile is not None else "") or ""
    normalized_content_type = _normalized_content_type_for_parser(
        content_type or (source_profile.content_type if source_profile is not None else "")
    )
    if extension == ".tsv" or normalized_content_type == "text/tab-separated-values":
        return "\t"
    return ","


def _delimited_rows(text: str, *, delimiter: str) -> list[list[str]]:
    try:
        reader = csv.reader(StringIO(text), delimiter=delimiter, skipinitialspace=True)
        return [
            [_clean_table_cell(value) for value in row]
            for row in reader
            if any(value.strip() for value in row)
        ]
    except csv.Error:
        return []


def _adapter_version(backend: str) -> str:
    package = EXTERNAL_ADAPTER_PACKAGES[backend]
    try:
        module = importlib.import_module(package)
    except Exception:
        return f"{backend}_adapter_v1"
    version = getattr(module, "__version__", None)
    if isinstance(version, str) and version.strip():
        return version.strip()[:80]
    return f"{backend}_adapter_v1"


def _tables_from_adapter_text(
    text: str,
    *,
    element_id: str | None,
    page_number: int | None,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> list[ExtractionTable]:
    cells = _table_cells_from_adapter_text(text)
    return _adapter_table_from_cells(
        cells,
        element_id=element_id,
        page_number=page_number,
        source_parser=source_parser,
        parser_backend=parser_backend,
        parser_version=parser_version,
        table_source="adapter_text",
    )


def _tables_from_adapter_rows(
    rows: Sequence[Sequence[str]],
    *,
    element_id: str | None,
    page_number: int | None,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> list[ExtractionTable]:
    cells = _table_cells_from_rows(rows)
    return _adapter_table_from_cells(
        cells,
        element_id=element_id,
        page_number=page_number,
        source_parser=source_parser,
        parser_backend=parser_backend,
        parser_version=parser_version,
        table_source="adapter_rows",
    )


def _adapter_table_cells(item: object) -> list[ExtractionTableCell]:
    metadata = _object_member(item, "metadata")
    html_cells = _adapter_html_table_cells(item)
    if html_cells:
        return html_cells
    for value in (
        _object_member(item, "cells"),
        _object_member(item, "table_cells"),
        _metadata_get(metadata, "cells"),
        _metadata_get(metadata, "table_cells"),
    ):
        cells = _table_cells_from_adapter_cell_value(value)
        if cells:
            return cells
    for value in (_object_member(item, "rows"), _object_member(item, "data")):
        cells = _table_cells_from_adapter_cell_value(value)
        has_layout = any(
            _adapter_cell_has_layout(cell) for cell in _flatten_table_cell_values(value)
        )
        if cells and has_layout:
            return cells
    return []


def _adapter_table_from_cells(
    cells: Sequence[ExtractionTableCell],
    *,
    element_id: str | None,
    page_number: int | None,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
    table_source: str,
) -> list[ExtractionTable]:
    if not cells:
        return []
    row_count, column_count = _table_shape_from_cells(cells)
    table_id = element_id or "adapter-table-0000"
    return [
        ExtractionTable(
            table_id=table_id,
            element_id=element_id,
            page_number=page_number,
            cells=list(cells),
            metadata={
                "source_parser": source_parser,
                "parser_backend": parser_backend,
                "parser_version": parser_version,
                "row_count": row_count,
                "column_count": column_count,
                "table_source": table_source,
                # 解析が渡したセル・行は解析の表の格子（本文から推したセルは除く。#3429）。
                **(
                    {PARSED_TABLE_GRID_KEY: PARSED_TABLE_GRID_VERSION}
                    if table_source in _PARSED_GRID_TABLE_SOURCES
                    else {}
                ),
            },
        )
    ]


# 解析が表の格子を渡した表の出どころ（``adapter_text`` は本文から推した行なので含めない）。
_PARSED_GRID_TABLE_SOURCES = frozenset({"adapter_cells", "adapter_rows"})


def _adapter_table_with_page_metadata(
    table: ExtractionTable,
    *,
    page: ExtractionPage,
) -> ExtractionTable:
    """page size/rotation を table/cell metadata へ伝播する。"""
    page_metadata = _page_size_metadata(page)
    if not page_metadata:
        return table
    updated_cells = [
        (
            cell.model_copy(update={"metadata": {**cell.metadata, **page_metadata}})
            if cell.bbox
            else cell
        )
        for cell in table.cells
    ]
    return table.model_copy(
        update={
            "cells": updated_cells,
            "metadata": {**table.metadata, **page_metadata},
        }
    )


def _page_size_metadata(page: ExtractionPage) -> dict[str, ExtractionMetadataValue]:
    metadata: dict[str, ExtractionMetadataValue] = {}
    if page.width is not None:
        metadata["page_width"] = page.width
    if page.height is not None:
        metadata["page_height"] = page.height
    if page.rotation is not None:
        metadata["page_rotation"] = page.rotation
    return metadata


def _table_cells_from_adapter_text(text: str) -> list[ExtractionTableCell]:
    rows = _markdown_table_rows(text)
    if not rows:
        rows = _tabular_text_rows(text)
    return _table_cells_from_rows(rows)


def _table_cells_from_rows(rows: Sequence[Sequence[str]]) -> list[ExtractionTableCell]:
    return [
        ExtractionTableCell(
            row=row_index,
            col=col_index,
            text=str(value).strip(),
            metadata={"cell_ref": _a1_cell_ref(row_index, col_index)},
        )
        for row_index, row in enumerate(rows)
        for col_index, value in enumerate(row)
    ]


def _a1_cell_ref(row_index: int, col_index: int) -> str:
    """0-based row/column を A1 形式へ変換する。"""
    return f"{_spreadsheet_column_label(col_index)}{row_index + 1}"


def _spreadsheet_column_label(col_index: int) -> str:
    index = max(0, col_index)
    letters: list[str] = []
    while True:
        index, remainder = divmod(index, 26)
        letters.append(chr(ord("A") + remainder))
        if index == 0:
            break
        index -= 1
    return "".join(reversed(letters))


def _table_cells_from_adapter_cell_value(value: object) -> list[ExtractionTableCell]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    items = list(value)
    if not items:
        return []
    flat_cells = _table_cells_from_flat_adapter_cells(items)
    if flat_cells:
        return flat_cells
    row_cells = _table_cells_from_adapter_cell_rows(items)
    if row_cells:
        return row_cells
    return []


def _table_cells_from_flat_adapter_cells(
    items: Sequence[object],
) -> list[ExtractionTableCell]:
    cells: list[ExtractionTableCell] = []
    for item in items:
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray | Mapping):
            return []
        cell = _adapter_table_cell(item, row_index=None, col_index=None)
        if cell is None:
            return []
        cells.append(cell)
    return _usable_table_cells(cells)


def _table_cells_from_adapter_cell_rows(
    items: Sequence[object],
) -> list[ExtractionTableCell]:
    cells: list[ExtractionTableCell] = []
    for row_index, row in enumerate(items):
        if not isinstance(row, Sequence) or isinstance(row, str | bytes | bytearray):
            return []
        for col_index, item in enumerate(row):
            cell = _adapter_table_cell(item, row_index=row_index, col_index=col_index)
            if cell is None:
                return []
            cells.append(cell)
    return _usable_table_cells(cells)


def _adapter_table_cell(
    item: object,
    *,
    row_index: int | None,
    col_index: int | None,
) -> ExtractionTableCell | None:
    row = _adapter_cell_index(item, ("row", "row_index", "rowindex"), row_index)
    col = _adapter_cell_index(item, ("col", "column", "column_index", "col_index"), col_index)
    if row is None or col is None:
        return None
    text = _adapter_table_cell_text(item)
    if not text:
        return None
    bbox = _adapter_table_cell_bbox(item)
    metadata = _adapter_table_cell_metadata(item, row=row, col=col)
    for key, value in _adapter_bbox_lineage_metadata(bbox).items():
        metadata.setdefault(key, value)
    return ExtractionTableCell(
        row=row,
        col=col,
        text=text,
        row_span=_adapter_cell_span(item, ("row_span", "rowspan"), 1),
        col_span=_adapter_cell_span(item, ("col_span", "colspan", "column_span"), 1),
        # bbox は ExtractionTableCell の before-validator(normalize_bbox)が正規化する。
        bbox=cast("list[float] | None", bbox),
        confidence=_adapter_cell_confidence(item),
        metadata=metadata,
    )


def _adapter_table_cell_metadata(
    item: object,
    *,
    row: int,
    col: int,
) -> dict[str, ExtractionMetadataValue]:
    """adapter cell の安全な構造 metadata だけを共通 schema へ remap する。"""
    metadata: dict[str, ExtractionMetadataValue] = {}
    nested_metadata = _object_member(item, "metadata")
    for target_key, aliases in _adapter_cell_metadata_aliases():
        for alias in aliases:
            value = _object_member(item, alias)
            if value is None:
                value = _metadata_get(nested_metadata, alias)
            scalar = _metadata_scalar(value)
            if scalar is _NON_SCALAR or scalar is None:
                continue
            if isinstance(scalar, str):
                scalar = _clean_text(scalar)
                if not scalar:
                    continue
            metadata[target_key] = cast(ExtractionMetadataValue, scalar)
            break
    metadata.setdefault("cell_ref", _a1_cell_ref(row, col))
    if "formula" in metadata and "formula_cell_ref" not in metadata:
        metadata["formula_cell_ref"] = metadata["cell_ref"]
    return metadata


def _adapter_cell_metadata_aliases() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return (
        ("cell_id", ("cell_id", "cellId", "id", "cell_identifier")),
        (
            "cell_ref",
            (
                "cell_ref",
                "cell_reference",
                "reference",
                "ref",
                "address",
                "cell_address",
            ),
        ),
        (
            "formula_cell_ref",
            (
                "formula_cell_ref",
                "formula_cell_reference",
                "formula_ref",
                "formula_address",
            ),
        ),
        (
            "formula",
            ("formula", "formula_text", "excel_formula", "spreadsheet_formula"),
        ),
        (
            "formula_value",
            (
                "formula_value",
                "cached_value",
                "cached_result",
                "computed_value",
                "result",
            ),
        ),
        ("equation_format", ("equation_format", "formula_format", "formula_type")),
        ("is_header", ("is_header", "isHeader", "header", "header_cell")),
        ("row_header", ("row_header", "is_row_header", "isRowHeader")),
        (
            "column_header",
            ("column_header", "col_header", "is_column_header", "isColumnHeader"),
        ),
        ("header_scope", ("header_scope", "scope")),
        ("cell_role", ("cell_role", "role")),
        ("cell_kind", ("cell_kind", "kind", "cell_type")),
    )


def _adapter_table_cell_text(item: object) -> str:
    if isinstance(item, str | int | float | bool):
        return _clean_table_cell(str(item))
    for key in ("text", "value", "content", "markdown", "raw_text"):
        value = _object_member(item, key)
        if isinstance(value, str | int | float | bool) and str(value).strip():
            return _clean_table_cell(str(value))
    return ""


def _adapter_cell_index(
    item: object,
    keys: Sequence[str],
    fallback: int | None,
) -> int | None:
    for key in keys:
        value = _object_member(item, key)
        index = _int_value(value)
        if index is not None and index >= 0:
            return index
    return fallback


def _adapter_cell_span(item: object, keys: Sequence[str], default: int) -> int:
    for key in keys:
        value = _object_member(item, key)
        span = _int_value(value)
        if span is not None and span >= 1:
            return span
    return default


def _adapter_table_cell_bbox(item: object) -> object | None:
    for key in ("bbox", "coordinates", "bounding_box", "boundingbox"):
        value = _object_member(item, key)
        bbox = _adapter_bbox_value(value)
        if bbox is not None:
            return bbox
    return None


def _adapter_cell_confidence(item: object) -> float | None:
    for key in ("confidence", "detection_class_prob", "probability"):
        confidence = _float_value(_object_member(item, key))
        if confidence is not None and 0.0 <= confidence <= 1.0:
            return confidence
    return None


def _adapter_cell_has_layout(item: object) -> bool:
    return _adapter_table_cell_bbox(item) is not None or any(
        _object_member(item, key) is not None
        for key in ("row_span", "rowspan", "col_span", "colspan", "confidence")
    )


def _flatten_table_cell_values(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    items = list(value)
    flattened: list[object] = []
    for item in items:
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray | Mapping):
            flattened.extend(list(item))
        else:
            flattened.append(item)
    return flattened


def _usable_table_cells(
    cells: Sequence[ExtractionTableCell],
) -> list[ExtractionTableCell]:
    if not cells:
        return []
    if max((cell.col for cell in cells), default=0) <= 0:
        return []
    return list(cells)


def _markdown_table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "|" not in stripped:
            continue
        values = _markdown_table_values(stripped)
        if len(values) <= 1 or _is_markdown_separator_row(values):
            continue
        rows.append(values)
    return rows


def _markdown_table_values(line: str) -> list[str]:
    body = line.strip().strip("|")
    values = [
        _clean_table_cell(value.replace("\\|", "|")) for value in re.split(r"(?<!\\)\|", body)
    ]
    return values


def _is_markdown_separator_row(values: Sequence[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", value.replace(" ", "")) for value in values)


def _tabular_text_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "\t" not in stripped:
            continue
        values = [_clean_table_cell(value) for value in stripped.split("\t")]
        if len(values) > 1:
            rows.append(values)
    return rows


def _clean_table_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


@contextmanager
def _temporary_source_file(
    source_bytes: bytes,
    source_profile: SourceProfile | None,
    content_type: str,
    *,
    suffix: str | None = None,
) -> Iterator[Path]:
    suffix = suffix or _source_suffix(source_profile, content_type)
    with tempfile.NamedTemporaryFile(prefix="rag-parser-", suffix=suffix) as handle:
        handle.write(source_bytes)
        handle.flush()
        yield Path(handle.name)


def _source_suffix(source_profile: SourceProfile | None, content_type: str) -> str:
    if source_profile is not None and source_profile.extension:
        return source_profile.extension
    by_content_type = {
        "application/pdf": ".pdf",
        "text/html": ".html",
        "message/rfc822": ".eml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    if content_type.startswith("image/"):
        return f".{content_type.split('/', 1)[1].split(';', 1)[0] or 'img'}"
    if content_type.startswith("text/"):
        return ".txt"
    return by_content_type.get(content_type.split(";", 1)[0].strip().casefold(), ".bin")


def _normalized_content_type_for_parser(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().casefold()


def _export_adapter_text(
    value: object,
    *,
    preferred_methods: Sequence[str],
) -> tuple[str, str]:
    if isinstance(value, str):
        return _clean_text(value), "str"
    for method_name in preferred_methods:
        exported = getattr(value, method_name, None)
        if callable(exported):
            exported = exported()
        text = _adapter_text_value(exported)
        if text:
            return _clean_text(text), method_name
    text = _adapter_text_value(value)
    return (_clean_text(text), "str") if text else ("", "unknown")


def _adapter_child_elements(value: object) -> tuple[object, ...]:
    """adapter 出力に含まれる block/chunk/table sequence を抽出する。"""
    return tuple(
        _adapter_descendant_elements(
            value,
            inherited_metadata={},
            depth=0,
            seen=frozenset(),
        )
    )


def _adapter_descendant_elements(
    value: object,
    *,
    inherited_metadata: Mapping[str, object],
    depth: int,
    seen: frozenset[int],
) -> list[object]:
    """page/group container を再帰展開し、leaf block に親 metadata を継承する。"""
    if value is None or depth > 12:
        return []
    value_id = id(value)
    if value_id in seen:
        return []
    next_seen = seen | {value_id}
    current_metadata = _adapter_inherited_metadata(value, inherited_metadata)
    child_elements: list[object] = []
    for attr in ADAPTER_CHILD_CONTAINER_ATTRS:
        child = _object_member(value, attr)
        if child is None:
            continue
        for item in _adapter_child_collection_items(child):
            child_elements.extend(
                _adapter_descendant_elements(
                    item,
                    inherited_metadata=current_metadata,
                    depth=depth + 1,
                    seen=next_seen,
                )
            )
    if child_elements:
        self_element = _adapter_leaf_element(value, inherited_metadata)
        if self_element is not None:
            if _adapter_element_kind(self_element) == "table":
                return [self_element]
            return [self_element, *child_elements]
        return child_elements
    self_element = _adapter_leaf_element(value, inherited_metadata)
    return [self_element] if self_element is not None else []


def _adapter_leaf_element(
    value: object,
    inherited_metadata: Mapping[str, object],
) -> object | None:
    """container と element を兼ねる adapter node から本文または figure asset を取り出す。"""
    if not _adapter_looks_like_element(value):
        return None
    if not _adapter_element_text(value) and _adapter_element_kind(value) != "figure":
        return None
    if inherited_metadata:
        return _AdapterElementView(value, inherited_metadata)
    return value


def _adapter_child_collection_items(value: object) -> list[object]:
    if isinstance(value, Mapping):
        if _mapping_looks_like_adapter_element(value):
            return [value]
        return list(value.values())
    return _adapter_sequence_items(value)


def _mapping_looks_like_adapter_element(value: Mapping[object, object]) -> bool:
    keys = {str(key).strip().casefold() for key in value}
    return bool(
        keys
        & {
            "category",
            "type",
            "kind",
            "text",
            "raw_text",
            "content",
            "markdown",
            "cells",
            "table_cells",
            "rows",
            "data",
            "latex",
            "formula",
            "equation",
            "math",
            "mathml",
            "bbox",
            "coordinates",
            "bounding_box",
            "metadata",
            "element_id",
            "id",
        }
    )


def _adapter_looks_like_element(value: object) -> bool:
    if isinstance(value, _AdapterElementView):
        value = value.item
    if isinstance(value, Mapping):
        return _mapping_looks_like_adapter_element(value)
    for attr in (
        "category",
        "type",
        "kind",
        "id",
        "element_id",
        "text",
        "raw_text",
        "content",
        "latex",
        "formula",
        "equation",
        "math",
        "mathml",
        "cells",
        "rows",
        "data",
    ):
        if getattr(value, attr, None) is not None:
            return True
    return _canonical_adapter_kind(value.__class__.__name__) != "other"


def _adapter_inherited_metadata(
    value: object,
    inherited_metadata: Mapping[str, object],
) -> dict[str, object]:
    metadata = dict(inherited_metadata)
    value_metadata = _object_member(value, "metadata")
    for target_key, aliases in {
        "page_number": ("page_number", "page_no", "page"),
        "section_path": ("section_path", "heading_path", "headings", "parent_titles"),
    }.items():
        candidate = _first_adapter_member(value, value_metadata, aliases)
        if candidate is not None:
            metadata[target_key] = candidate
    parent_id = _adapter_parent_id_for_descendants(value)
    if parent_id is not None:
        metadata["parent_id"] = parent_id
    return metadata


def _adapter_parent_id_for_descendants(value: object) -> str | None:
    if not _adapter_looks_like_element(value):
        return None
    kind = _adapter_element_kind(value)
    if kind in {"table_caption", "figure_caption"}:
        return None
    return _adapter_explicit_element_id(value)


def _adapter_explicit_element_id(value: object) -> str | None:
    if isinstance(value, _AdapterElementView):
        value = value.item
    metadata = _object_member(value, "metadata")
    for candidate in (
        _object_member(value, "id"),
        _object_member(value, "element_id"),
        _metadata_get(metadata, "element_id"),
    ):
        label = _adapter_reference_label(candidate)
        if label is not None:
            return label
    return None


def _first_adapter_member(
    value: object,
    metadata: object,
    aliases: Sequence[str],
) -> object | None:
    for alias in aliases:
        direct = _object_member(value, alias)
        if direct is not None:
            return direct
        nested = _metadata_get(metadata, alias)
        if nested is not None:
            return nested
    return None


def _adapter_sequence_items(value: object) -> list[object]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return list(value)
    values = getattr(value, "values", None)
    if callable(values):
        try:
            raw_values = values()
        except Exception:
            return []
        if isinstance(raw_values, Sequence) and not isinstance(raw_values, bytes | bytearray | str):
            return list(raw_values)
        return list(raw_values) if isinstance(raw_values, Iterator) else []
    return []


def _object_member(value: object, name: str) -> object | None:
    if isinstance(value, _AdapterElementView):
        if name == "metadata":
            return _merged_adapter_metadata(
                _object_member(value.item, name),
                value.inherited_metadata,
            )
        direct = _object_member(value.item, name)
        if direct is not None:
            return direct
        return value.inherited_metadata.get(name)
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _merged_adapter_metadata(
    metadata: object,
    inherited_metadata: Mapping[str, object],
) -> Mapping[str, object]:
    merged = dict(inherited_metadata)
    merged.update(_mapping_from_object(metadata))
    return merged


def _adapter_text_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in (
            "markdown",
            "text",
            "raw_text",
            "content",
            "latex",
            "formula",
            "equation",
            "math",
            "mathml",
        ):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item
        return ""
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        for item in value:
            text = _adapter_text_value(item)
            if text:
                return text
        return ""
    text = str(value)
    return "" if text.startswith("<") and text.endswith(">") else text


def _docling_artifacts(document: object) -> dict[str, ExtractionMetadataValue]:
    artifacts: dict[str, ExtractionMetadataValue] = {}
    for attr, key in (
        ("pages", "page_count"),
        ("tables", "table_count"),
        ("pictures", "asset_count"),
    ):
        value = getattr(document, attr, None)
        count = _safe_len(value)
        if count is not None:
            artifacts[key] = count
    return artifacts


def _safe_len(value: object) -> int | None:
    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        return None


def _adapter_pages_from_source(value: object) -> list[ExtractionPage]:
    """adapter の page metadata を preview/citation 用の共通 schema へ寄せる。"""
    pages_value = _object_member(value, "pages")
    if pages_value is None:
        return []
    pages: list[ExtractionPage] = []
    for fallback_number, page in _adapter_page_items(pages_value):
        page_number = _adapter_page_number(page, fallback=fallback_number)
        if page_number is None:
            continue
        width, height = _adapter_page_size(page)
        pages.append(
            ExtractionPage(
                page_number=page_number,
                label=_adapter_page_label(page, page_number),
                width=width,
                height=height,
                rotation=_adapter_page_rotation(page),
                metadata=_adapter_page_metadata(page),
            )
        )
    return pages


def _adapter_page_items(value: object) -> list[tuple[int | None, object]]:
    if isinstance(value, Mapping):
        items: list[tuple[int | None, object]] = []
        for key, page in value.items():
            items.append((_int_value(key), page))
        return items
    sequence = _adapter_sequence_items(value)
    return [(index, page) for index, page in enumerate(sequence, start=1)]


def _adapter_page_number(page: object, *, fallback: int | None) -> int | None:
    metadata = _object_member(page, "metadata")
    for value in (
        _object_member(page, "page_number"),
        _object_member(page, "page_no"),
        _object_member(page, "page"),
        _metadata_get(metadata, "page_number"),
        _metadata_get(metadata, "page_no"),
        _metadata_get(metadata, "page"),
        fallback,
    ):
        number = _int_value(value)
        if number is not None and number >= 1:
            return number
    return None


def _adapter_page_size(page: object) -> tuple[float | None, float | None]:
    metadata = _object_member(page, "metadata")
    for container in (
        page,
        metadata,
        _object_member(page, "size"),
        _object_member(page, "dimensions"),
        _object_member(page, "page_size"),
        _metadata_get(metadata, "size"),
        _metadata_get(metadata, "dimensions"),
        _metadata_get(metadata, "page_size"),
    ):
        width = _adapter_page_dimension(container, ("width", "w", "page_width", "page_w"))
        height = _adapter_page_dimension(
            container,
            ("height", "h", "page_height", "page_h"),
        )
        if width is not None and height is not None:
            return width, height
    return None, None


def _adapter_page_dimension(container: object, aliases: Sequence[str]) -> float | None:
    if container is None:
        return None
    for alias in aliases:
        value = _object_member(container, alias)
        number = _float_value(value)
        if number is not None and number > 0:
            return number
    return None


def _adapter_page_label(page: object, page_number: int) -> str:
    metadata = _object_member(page, "metadata")
    for value in (
        _object_member(page, "label"),
        _object_member(page, "page_label"),
        _object_member(page, "name"),
        _metadata_get(metadata, "label"),
        _metadata_get(metadata, "page_label"),
        _metadata_get(metadata, "name"),
    ):
        if isinstance(value, str | int) and str(value).strip():
            return str(value).strip()[:128]
    return f"page {page_number}"


def _adapter_page_rotation(page: object) -> int | None:
    metadata = _object_member(page, "metadata")
    for value in (
        _object_member(page, "rotation"),
        _object_member(page, "angle"),
        _metadata_get(metadata, "rotation"),
        _metadata_get(metadata, "angle"),
    ):
        number = _int_value(value)
        if number is not None:
            return number
    return None


def _adapter_page_break_next_page(
    item: object,
    *,
    current_page: int | None,
) -> int:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "page_number"),
        _object_member(item, "page_no"),
        _object_member(item, "page"),
        _metadata_get(metadata, "page_number"),
        _metadata_get(metadata, "page_no"),
        _metadata_get(metadata, "page"),
    ):
        number = _int_value(value)
        if number is not None and number >= 1:
            return number + 1
    return (current_page or 1) + 1


def _adapter_page_metadata(page: object) -> dict[str, ExtractionMetadataValue]:
    metadata: dict[str, ExtractionMetadataValue] = {}
    source = _mapping_from_object(_object_member(page, "metadata"))
    for key, value in source.items():
        normalized_key = str(key).strip()[:80]
        scalar = _metadata_scalar(value)
        if normalized_key and scalar is not _NON_SCALAR:
            metadata[normalized_key] = cast(ExtractionMetadataValue, scalar)
    return metadata


def _adapter_pages_for_elements(
    pages: Sequence[ExtractionPage],
    elements: Sequence[DocumentElement],
) -> list[ExtractionPage]:
    """adapter page metadata と element lineage を page_number で統合する。"""
    by_page = {page.page_number: page for page in pages}
    element_ids_by_page: dict[int, list[str]] = {}
    for element in elements:
        if element.page_number is None or element.element_id is None:
            continue
        element_ids_by_page.setdefault(element.page_number, []).append(element.element_id)
    merged: list[ExtractionPage] = []
    for page_number in sorted(set(by_page) | set(element_ids_by_page)):
        page = by_page.get(page_number)
        element_ids = _dedupe_adapter_labels(
            [
                *(page.element_ids if page is not None else []),
                *element_ids_by_page.get(page_number, []),
            ]
        )
        if page is None:
            merged.append(
                ExtractionPage(
                    page_number=page_number,
                    label=f"page {page_number}",
                    element_ids=element_ids,
                )
            )
        else:
            merged.append(page.model_copy(update={"element_ids": element_ids}))
    return merged


def _dedupe_adapter_labels(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned[:128])
    return result


def _adapter_element_text(item: object) -> str:
    for attr in ("text", "raw_text", "content"):
        value = _object_member(item, attr)
        if isinstance(value, str) and value.strip():
            return _clean_text(value)
    for method_name in (
        "export_to_markdown",
        "to_markdown",
        "export_to_text",
        "to_text",
    ):
        exported = getattr(item, method_name, None)
        if callable(exported):
            exported = exported()
        text = _adapter_text_value(exported)
        if text:
            return _clean_text(text)
    formula_text = _adapter_formula_text(item)
    if formula_text:
        return formula_text
    cells = _adapter_table_cells(item)
    if cells:
        return _table_markdown_from_cells(cells)
    rows = _adapter_table_rows(item)
    if rows:
        return "\n".join(_xlsx_markdown_row(row) for row in rows)
    if isinstance(item, Mapping):
        text = _adapter_text_value(item)
        if text:
            return _clean_text(text)
        return ""
    text = str(item)
    return "" if text.startswith("<") and text.endswith(">") else _clean_text(text)


def _adapter_element_kind(item: object) -> str:
    for attr in ("category", "type", "kind"):
        value = _object_member(item, attr)
        if isinstance(value, str) and value.strip():
            return _canonical_adapter_kind(value)
    if _adapter_table_cells(item):
        return "table"
    if _adapter_table_rows(item):
        return "table"
    return _canonical_adapter_kind(item.__class__.__name__)


def _adapter_table_rows(item: object) -> list[list[str]]:
    for attr in ("rows", "data"):
        rows = _rows_from_table_value(_object_member(item, attr))
        if rows:
            return rows
    rows = _adapter_html_table_rows(item)
    if rows:
        return rows
    rows = _rows_from_table_value(item if isinstance(item, Sequence) else None)
    return rows


def _adapter_html_table_rows(item: object) -> list[list[str]]:
    html_cells = _adapter_html_table_cells(item)
    if html_cells:
        return _table_plain_rows_from_cells(html_cells)
    return []


def _adapter_html_table_cells(item: object) -> list[ExtractionTableCell]:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "text_as_html"),
        _object_member(item, "table_as_html"),
        _object_member(item, "table_html"),
        _metadata_get(metadata, "text_as_html"),
        _metadata_get(metadata, "table_as_html"),
        _metadata_get(metadata, "table_html"),
    ):
        if not isinstance(value, str) or "<table" not in value.casefold():
            continue
        cells = _html_table_cells(value)
        if cells:
            return cells
    return []


def _html_table_cells(value: str) -> list[ExtractionTableCell]:
    parser = _TextHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return []
    tables = parser.tables()
    if not tables:
        return []
    return _table_cells_from_html_table(tables[0])


def _rows_from_table_value(value: object) -> list[list[str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    rows: list[list[str]] = []
    for row in value:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes | bytearray):
            return []
        cells = [_clean_table_cell(str(cell)) for cell in row]
        if any(cells):
            rows.append(cells)
    if rows and max((len(row) for row in rows), default=0) > 1:
        return rows
    return []


def _canonical_adapter_kind(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
    compact = normalized.replace("_", "")
    if "pagebreak" in compact:
        return "page_break"
    if "table" in compact and "caption" in compact:
        return "table_caption"
    if _adapter_visual_kind(compact) is not None and "caption" in compact:
        return "figure_caption"
    if "title" in compact or "heading" in compact or "section" in compact:
        return "title"
    if "table" in compact:
        return "table"
    if "formula" in compact or "equation" in compact:
        return "equation"
    if "code" in compact:
        return "code"
    if "list" in compact or "bullet" in compact:
        return "list"
    if _adapter_visual_kind(compact) is not None:
        return "figure"
    if "caption" in compact:
        return "figure_caption"
    if "email" in compact:
        return "text"
    if "slide" in compact:
        return "text"
    if "sheet" in compact:
        return "table"
    if "text" in compact or "narrative" in compact or "paragraph" in compact:
        return "text"
    return "other"


def _adapter_visual_kind(compact: str) -> str | None:
    if "chart" in compact:
        return "chart"
    if "diagram" in compact:
        return "diagram"
    if "graph" in compact or "plot" in compact:
        return "chart"
    if "image" in compact:
        return "image"
    if "picture" in compact:
        return "picture"
    if "figure" in compact:
        return "figure"
    return None


def _content_kind_for_adapter_kind(kind: str) -> str:
    normalized = kind.strip().casefold()
    if normalized in {"table", "table_caption"}:
        return "table"
    if normalized in {"figure", "figure_caption"}:
        return "figure"
    if normalized == "equation":
        return "equation"
    if normalized == "code":
        return "code"
    if normalized == "list":
        return "list"
    return "text"


def _adapter_element_id(item: object, order: int) -> str | None:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "id"),
        _object_member(item, "element_id"),
        _metadata_get(metadata, "element_id"),
    ):
        if isinstance(value, str | int) and str(value).strip():
            return str(value).strip()[:128]
    return f"adapter-el-{order:04d}"


def _adapter_element_parent_id(item: object) -> str | None:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "parent_id"),
        _object_member(item, "parent"),
        _object_member(item, "parent_ref"),
        _metadata_get(metadata, "parent_id"),
        _metadata_get(metadata, "parent"),
        _metadata_get(metadata, "parent_ref"),
    ):
        parent_id = _adapter_reference_label(value)
        if parent_id is not None:
            return parent_id
    return None


def _adapter_element_section_path(item: object) -> list[str]:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "section_path"),
        _object_member(item, "heading_path"),
        _object_member(item, "headings"),
        _metadata_get(metadata, "section_path"),
        _metadata_get(metadata, "heading_path"),
        _metadata_get(metadata, "headings"),
        _metadata_get(metadata, "parent_titles"),
    ):
        path = _adapter_section_path_values(value)
        if path:
            return path
    return []


def _adapter_element_section_level(item: object) -> int | None:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "section_level"),
        _object_member(item, "heading_level"),
        _object_member(item, "level"),
        _metadata_get(metadata, "section_level"),
        _metadata_get(metadata, "heading_level"),
        _metadata_get(metadata, "level"),
    ):
        level = _int_value(value)
        if level is not None:
            return max(1, min(level, 6))
    return None


def _asset_from_adapter_element(
    item: object,
    *,
    kind: str,
    text: str,
    order: int,
    element_id: str | None,
    page_number: int | None,
    bbox: object,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> ExtractionAsset | None:
    """adapter の figure/image block を first-class asset へ昇格する。"""
    if kind != "figure":
        return None
    asset_kind = _adapter_asset_kind(item)
    asset_id = _adapter_asset_id(item, element_id=element_id, order=order)
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": source_parser,
        "parser_backend": parser_backend,
        "parser_version": parser_version,
        "adapter_element_type": kind,
        "asset_kind": asset_kind,
    }
    if raw_kind := _adapter_raw_element_kind(item):
        metadata["adapter_raw_element_type"] = raw_kind
    if element_id:
        metadata["element_id"] = element_id
    confidence = _adapter_element_confidence(item)
    if confidence is not None:
        metadata["confidence"] = confidence
    # 図だけの block(本文の要素を作らない)も、解析後の Vision が切り出せるよう bbox の基準を残す。
    metadata.update(_adapter_bbox_lineage_metadata(bbox))
    metadata.update(_adapter_coordinate_space(item))
    return ExtractionAsset(
        asset_id=asset_id,
        kind=asset_kind,
        page_number=page_number,
        # bbox は ExtractionAsset の before-validator(normalize_bbox)が正規化する。
        bbox=cast("list[float] | None", bbox),
        alt_text=text or None,
        metadata=metadata,
    )


def _adapter_asset_kind(item: object) -> str:
    raw_kind = _adapter_raw_element_kind(item)
    if raw_kind:
        visual_kind = _adapter_visual_kind(
            re.sub(r"[\s-]+", "_", raw_kind.strip().casefold()).replace("_", "")
        )
        if visual_kind is not None:
            return visual_kind
    return "figure"


def _adapter_raw_element_kind(item: object) -> str | None:
    for attr in ("category", "type", "kind"):
        value = _object_member(item, attr)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return None


def _adapter_asset_id(item: object, *, element_id: str | None, order: int) -> str:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "asset_id"),
        _object_member(item, "image_id"),
        _object_member(item, "picture_id"),
        _metadata_get(metadata, "asset_id"),
        _metadata_get(metadata, "image_id"),
        _metadata_get(metadata, "picture_id"),
    ):
        label = _adapter_reference_label(value)
        if label is not None:
            return label
    return element_id or f"adapter-asset-{order:04d}"


def _adapter_asset_alt_text(item: object) -> str:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "alt_text"),
        _object_member(item, "caption"),
        _object_member(item, "description"),
        _metadata_get(metadata, "alt_text"),
        _metadata_get(metadata, "image_description"),
        _metadata_get(metadata, "caption"),
        _metadata_get(metadata, "description"),
    ):
        if isinstance(value, str) and value.strip():
            return _clean_text(value)
    return ""


def _adapter_formula_text(item: object) -> str:
    for _field_name, value in _adapter_formula_candidates(item):
        if isinstance(value, str) and value.strip():
            return _clean_text(value)
    return ""


def _adapter_equation_metadata(item: object) -> dict[str, ExtractionMetadataValue]:
    for field_name, value in _adapter_formula_candidates(item):
        if isinstance(value, str) and value.strip():
            return {
                "equation_format": _adapter_equation_format(field_name),
                "equation_source_field": field_name,
            }
    return {}


def _adapter_code_metadata(item: object) -> dict[str, ExtractionMetadataValue]:
    language = _adapter_code_language(item)
    return {"code_language": language} if language is not None else {}


def _adapter_code_language(item: object) -> str | None:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "code_language"),
        _object_member(item, "language"),
        _object_member(item, "lang"),
        _object_member(item, "programming_language"),
        _object_member(item, "lexer"),
        _metadata_get(metadata, "code_language"),
        _metadata_get(metadata, "language"),
        _metadata_get(metadata, "lang"),
        _metadata_get(metadata, "programming_language"),
        _metadata_get(metadata, "lexer"),
    ):
        language = _normalize_code_language(value)
        if language is not None:
            return language
    return None


def _adapter_formula_candidates(item: object) -> tuple[tuple[str, object | None], ...]:
    metadata = _object_member(item, "metadata")
    fields = (
        "latex",
        "formula",
        "equation",
        "math",
        "mathml",
        "asciimath",
        "ascii_math",
    )
    candidates: list[tuple[str, object | None]] = []
    for field_name in fields:
        candidates.append((field_name, _object_member(item, field_name)))
        candidates.append((field_name, _metadata_get(metadata, field_name)))
    return tuple(candidates)


def _adapter_equation_format(field_name: str) -> str:
    normalized = field_name.strip().casefold()
    if normalized == "mathml":
        return "mathml"
    if normalized in {"asciimath", "ascii_math"}:
        return "asciimath"
    if normalized == "latex":
        return "latex"
    return "plain"


def _adapter_reference_label(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int) and str(value).strip():
        return str(value).strip()[:128]
    if isinstance(value, Mapping):
        for key in ("id", "element_id", "ref", "cref", "$ref"):
            candidate = value.get(key)
            if candidate is None:
                continue
            label = _adapter_reference_label(candidate)
            if label is not None:
                return label
        return None
    for attr in ("id", "element_id", "ref", "cref"):
        candidate = getattr(value, attr, None)
        if candidate is None:
            continue
        label = _adapter_reference_label(candidate)
        if label is not None:
            return label
    return None


def _adapter_section_path_values(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items: list[object] = [cast(object, item) for item in value.split("/")]
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        raw_items = list(value)
    else:
        return []
    path: list[str] = []
    for item in raw_items:
        if isinstance(item, Mapping):
            item = item.get("title") or item.get("text") or item.get("name")
        cleaned = re.sub(r"\s+", " ", str(item)).strip()
        if cleaned:
            path.append(cleaned[:80])
    return path


def _adapter_element_page_number(item: object) -> int | None:
    metadata = _object_member(item, "metadata")
    for value in (
        _metadata_get(metadata, "page_number"),
        _metadata_get(metadata, "page_no"),
        _metadata_get(metadata, "page"),
        _object_member(item, "page_number"),
        _object_member(item, "page_no"),
        _object_member(item, "page"),
    ):
        number = _int_value(value)
        if number is not None and number >= 1:
            return number
    return None


def _adapter_element_bbox(item: object) -> Any:
    metadata = _object_member(item, "metadata")
    for value in (
        _metadata_get(metadata, "coordinates"),
        _metadata_get(metadata, "bbox"),
        _metadata_get(metadata, "bounding_box"),
        _object_member(item, "bbox"),
    ):
        bbox = _adapter_bbox_value(value)
        if bbox is not None:
            return bbox
    return None


def _adapter_bbox_value(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, Mapping | Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    for attr in ("points", "coordinates", "bbox", "bounding_box"):
        nested: object | None = getattr(value, attr, None)
        if nested is not None:
            return nested
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped: object = to_dict()
        return dumped
    return None


def _adapter_coordinate_space(item: object) -> dict[str, ExtractionMetadataValue]:
    """bbox の座標系の寸法を返す(分からなければ空)。

    Unstructured の ``metadata.coordinates`` は ``points`` と座標系(``system``: PixelSpace /
    PointSpace 等。``width`` / ``height`` を持つ)を持ち、dict 化すると ``layout_width`` /
    ``layout_height`` / ``system``(クラス名)になる。どちらの形でも ``page_width`` /
    ``page_height`` に写す。
    """
    coordinates = _metadata_get(_object_member(item, "metadata"), "coordinates")
    if coordinates is None:
        return {}
    if isinstance(coordinates, Mapping):
        width = _float_value(coordinates.get("layout_width"))
        height = _float_value(coordinates.get("layout_height"))
        system: object = coordinates.get("system")
    else:
        system_object = getattr(coordinates, "system", None)
        width = _float_value(getattr(system_object, "width", None))
        height = _float_value(getattr(system_object, "height", None))
        system = type(system_object).__name__ if system_object is not None else None
    if width is None or height is None or width <= 0 or height <= 0:
        return {}
    space: dict[str, ExtractionMetadataValue] = {"page_width": width, "page_height": height}
    if isinstance(system, str) and system.strip():
        space["bbox_coordinate_system"] = system.strip()[:40]
    return space


def _adapter_bbox_lineage_metadata(
    bbox: object,
) -> dict[str, ExtractionMetadataValue]:
    metadata: dict[str, ExtractionMetadataValue] = {}
    mode = _adapter_bbox_coordinate_mode(bbox)
    if mode is not None:
        metadata["bbox_coordinate_mode"] = mode
    unit = _adapter_bbox_unit(bbox)
    if unit is not None:
        metadata["bbox_unit"] = unit
    return metadata


def _adapter_bbox_coordinate_mode(bbox: object) -> str | None:
    if isinstance(bbox, Mapping):
        lowered = {str(key).strip().casefold(): value for key, value in bbox.items()}
        if any(
            all(key in lowered for key in keys)
            for keys in (
                ("x", "y", "width", "height"),
                ("x", "y", "w", "h"),
                ("left", "top", "width", "height"),
            )
        ):
            return "xyxy"
        if any(
            all(key in lowered for key in keys)
            for keys in (
                ("x1", "y1", "x2", "y2"),
                ("xmin", "ymin", "xmax", "ymax"),
                ("x_min", "y_min", "x_max", "y_max"),
                ("left", "top", "right", "bottom"),
                ("l", "t", "r", "b"),
            )
        ):
            return "xyxy"
        for key in ("points", "vertices", "polygon", "coordinates"):
            if key in lowered:
                return _adapter_bbox_coordinate_mode(lowered[key]) or "xyxy"
        for key in ("bbox", "bounding_box", "boundingbox"):
            if key in lowered:
                return _adapter_bbox_coordinate_mode(lowered[key])
    if _is_flat_numeric_bbox(bbox):
        return "xyxy"
    if _is_adapter_point_sequence(bbox):
        return "xyxy"
    return None


def _is_flat_numeric_bbox(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return False
    items = list(value)
    return len(items) == 4 and all(_float_value(item) is not None for item in items)


def _adapter_bbox_unit(bbox: object) -> str | None:
    coords = _adapter_bbox_numeric_values(bbox)
    if not coords:
        return None
    max_value = max(abs(item) for item in coords)
    if max_value <= 1:
        return "ratio"
    if max_value <= 100:
        return "percent"
    return "absolute"


def _adapter_bbox_numeric_values(value: object) -> list[float]:
    if isinstance(value, Mapping):
        mapped_values: list[float] = []
        for item in value.values():
            mapped_values.extend(_adapter_bbox_numeric_values(item))
        return mapped_values
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence_values: list[float] = []
        for item in value:
            sequence_values.extend(_adapter_bbox_numeric_values(item))
        return sequence_values
    number = _float_value(value)
    return [number] if number is not None else []


def _is_adapter_point_sequence(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return False
    items = list(value)
    if len(items) < 2:
        return False
    return all(_adapter_point_value(item) is not None for item in items)


def _adapter_point_value(value: object) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        lowered = {str(key).strip().casefold(): item for key, item in value.items()}
        x = _float_value(lowered.get("x"))
        y = _float_value(lowered.get("y"))
        if x is not None and y is not None:
            return (x, y)
        return None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = list(value)
        if len(items) < 2:
            return None
        x = _float_value(items[0])
        y = _float_value(items[1])
        if x is not None and y is not None:
            return (x, y)
    return None


def _adapter_element_confidence(item: object) -> float | None:
    metadata = _object_member(item, "metadata")
    for value in (
        _object_member(item, "confidence"),
        _metadata_get(metadata, "confidence"),
        _metadata_get(metadata, "detection_class_prob"),
        _metadata_get(metadata, "probability"),
    ):
        confidence = _float_value(value)
        if confidence is not None and 0.0 <= confidence <= 1.0:
            return confidence
    return None


def _adapter_element_metadata(item: object) -> dict[str, ExtractionMetadataValue]:
    metadata: dict[str, ExtractionMetadataValue] = {}
    source = _mapping_from_object(_object_member(item, "metadata"))
    for key, value in source.items():
        normalized_key = str(key).strip()[:80]
        scalar = _metadata_scalar(value)
        if normalized_key and scalar is not _NON_SCALAR:
            metadata[normalized_key] = cast(ExtractionMetadataValue, scalar)
    return metadata


_NON_SCALAR = object()


def _mapping_from_object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, Mapping):
        return {str(key): item for key, item in attrs.items() if not str(key).startswith("_")}
    return {}


def _metadata_get(metadata: object, key: str) -> object | None:
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return getattr(metadata, key, None)


def _metadata_scalar(value: object) -> ExtractionMetadataValue | object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return _NON_SCALAR


def _int_value(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _float_value(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _document_type_for_source(source_profile: SourceProfile | None) -> str:
    if source_profile is None:
        return "ドキュメント"
    return {
        SourceModality.PDF: "PDF",
        SourceModality.IMAGE: "画像",
        SourceModality.TEXT: "テキスト文書",
        SourceModality.HTML: "HTML",
        SourceModality.EMAIL: "メール",
        SourceModality.OFFICE: "Office 文書",
        SourceModality.UNKNOWN: "ドキュメント",
    }[source_profile.modality]


def _is_unsupported_outlook_msg(source_profile: SourceProfile | None) -> bool:
    if source_profile is None:
        return False
    return (
        source_profile.modality == SourceModality.EMAIL
        and source_profile.unsupported_reason == "outlook_msg_not_supported"
    )


def _is_unsupported_tiff_image(source_profile: SourceProfile | None) -> bool:
    if source_profile is None:
        return False
    return (
        source_profile.modality == SourceModality.IMAGE
        and source_profile.unsupported_reason == "tiff_image_not_supported"
    )


def _is_unsupported_legacy_office_binary(source_profile: SourceProfile | None) -> bool:
    if source_profile is None:
        return False
    return (
        source_profile.modality == SourceModality.OFFICE
        and source_profile.unsupported_reason == "legacy_office_binary_not_supported"
    )


def _is_unsupported_odt_rtf(source_profile: SourceProfile | None) -> bool:
    if source_profile is None:
        return False
    return (
        source_profile.modality == SourceModality.OFFICE
        and source_profile.unsupported_reason == "odt_rtf_not_supported"
    )


def _default_content_kind_for_source(source_profile: SourceProfile | None) -> str:
    if source_profile is None:
        return "text"
    if source_profile.modality == SourceModality.EMAIL:
        return "email"
    if source_profile.extension == ".pptx":
        return "slide"
    if source_profile.extension == ".xlsx":
        return "sheet"
    return "text"


def _decode_text_bytes(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    match = from_bytes(data).best()
    if match is not None:
        return str(match)
    return data.decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed: list[str] = []
    blank_seen = False
    for line in lines:
        if not line:
            if not blank_seen:
                collapsed.append("")
            blank_seen = True
            continue
        collapsed.append(line)
        blank_seen = False
    return "\n".join(collapsed).strip()


def _looks_like_markdown(text: str) -> bool:
    return any(line.lstrip().startswith("#") for line in text.splitlines())


def _email_body_text(message: object) -> str:
    get_body = getattr(message, "get_body", None)
    if callable(get_body):
        body = get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            if isinstance(content, str):
                if body.get_content_type() == "text/html":
                    parser = _TextHTMLParser()
                    parser.feed(content)
                    return parser.text()
                return content
    walk = getattr(message, "walk", None)
    if callable(walk):
        parts: list[str] = []
        for part in walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() in {"text/plain", "text/html"}:
                content = part.get_content()
                if isinstance(content, str):
                    parts.append(content)
        return "\n\n".join(parts)
    return ""


def _office_xml_text(archive: zipfile.ZipFile, name: str) -> str:
    with archive.open(name) as handle:
        root = ElementTree.fromstring(handle.read())
    return _xml_text(root)


def _office_many_xml_text(archive: zipfile.ZipFile, pattern: str) -> str:
    regex = re.compile(pattern)
    names = sorted(name for name in archive.namelist() if regex.fullmatch(name))
    return "\n\n".join(_office_xml_text(archive, name) for name in names)


def _office_docx_text_and_tables(
    archive: zipfile.ZipFile,
) -> tuple[str, list[ExtractionTable]]:
    with archive.open("word/document.xml") as handle:
        root = ElementTree.fromstring(handle.read())
    body = root.find(".//{*}body")
    if body is None:
        body = root
    parts: list[str] = []
    tables: list[ExtractionTable] = []
    table_index = 0
    for child in list(body):
        local_name = _xml_local_name(child.tag)
        if local_name == "p":
            text = _xml_text(child)
            if text:
                parts.append(text)
        elif local_name == "tbl":
            rows = _docx_table_rows(child)
            if not rows:
                continue
            parts.extend(_xlsx_markdown_row(row) for row in rows)
            table = _docx_table_from_rows(rows, table_index=table_index)
            if table is not None:
                tables.append(table)
                table_index += 1
    if not parts:
        return _xml_text(root), tables
    return "\n".join(parts), tables


def _docx_table_rows(table_node: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table_node.findall(".//{*}tr"):
        cells = [_xml_text(cell) for cell in row.findall("./{*}tc")]
        cleaned = [_clean_table_cell(cell) for cell in cells if cell.strip()]
        if cleaned:
            rows.append(cleaned)
    return rows


def _docx_table_from_rows(
    rows: Sequence[Sequence[str]],
    *,
    table_index: int,
) -> ExtractionTable | None:
    if not rows:
        return None
    column_count = max((len(row) for row in rows), default=0)
    table_id = f"docx-table-{table_index:04d}"
    return ExtractionTable(
        table_id=table_id,
        element_id=table_id,
        page_number=1,
        cells=_table_cells_from_rows(rows),
        metadata={
            "source_parser": "local_office_structure",
            "parser_backend": "local_partition",
            "parser_version": LOCAL_PARSER_VERSION,
            "chunk_template": "office_document",
            "row_count": len(rows),
            "column_count": column_count,
        },
    )


def _office_pptx_text_and_tables(
    archive: zipfile.ZipFile,
) -> tuple[str, list[ExtractionTable]]:
    parts: list[str] = []
    tables: list[ExtractionTable] = []
    table_index = 0
    for slide_number, name in _openxml_numbered_members(archive, r"ppt/slides/slide(\d+)\.xml"):
        text, slide_tables = _office_pptx_slide_text_and_tables(
            archive,
            name,
            slide_number=slide_number,
            table_start_index=table_index,
        )
        if text:
            parts.append(text)
        tables.extend(slide_tables)
        table_index += len(slide_tables)
    return "\n\n".join(parts), tables


def _office_pptx_slide_text_and_tables(
    archive: zipfile.ZipFile,
    name: str,
    *,
    slide_number: int,
    table_start_index: int = 0,
) -> tuple[str, list[ExtractionTable]]:
    with archive.open(name) as handle:
        root = ElementTree.fromstring(handle.read())
    tables: list[ExtractionTable] = []
    for offset, table_node in enumerate(root.findall(".//{*}tbl")):
        rows = _pptx_table_rows(table_node)
        if not rows:
            continue
        table = _pptx_table_from_rows(
            rows,
            slide_number=slide_number,
            table_index=table_start_index + offset,
            source_path=name,
        )
        if table is not None:
            tables.append(table)
    return _xml_text(root), tables


def _pptx_table_rows(table_node: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table_node.findall("./{*}tr"):
        cells = [_xml_text(cell) for cell in row.findall("./{*}tc")]
        cleaned = [_clean_table_cell(cell) for cell in cells if cell.strip()]
        if cleaned:
            rows.append(cleaned)
    return rows


def _pptx_table_from_rows(
    rows: Sequence[Sequence[str]],
    *,
    slide_number: int,
    table_index: int,
    source_path: str,
) -> ExtractionTable | None:
    if not rows:
        return None
    column_count = max((len(row) for row in rows), default=0)
    table_id = f"pptx-slide-{slide_number}-table-{table_index:04d}"
    return ExtractionTable(
        table_id=table_id,
        element_id=table_id,
        page_number=slide_number,
        cells=_table_cells_from_rows(rows),
        metadata={
            "source_parser": "local_office_structure",
            "parser_backend": "local_partition",
            "parser_version": LOCAL_PARSER_VERSION,
            "chunk_template": "office_slide",
            "office_segment_number": slide_number,
            "office_segment_path": source_path,
            "row_count": len(rows),
            "column_count": column_count,
        },
    )


def _office_xlsx_text(archive: zipfile.ZipFile) -> str:
    text, _tables, _formula_elements = _office_xlsx_text_and_tables(archive)
    return text


def _office_xlsx_text_and_tables(
    archive: zipfile.ZipFile,
) -> tuple[str, list[ExtractionTable], list[DocumentElement]]:
    shared_strings = _xlsx_shared_strings(archive)
    sheet_names = sorted(
        name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
    )
    rows: list[str] = []
    tables: list[ExtractionTable] = []
    formula_elements: list[DocumentElement] = []
    for sheet_index, name in enumerate(sheet_names, start=1):
        sheet_text, table, sheet_formula_elements = _office_xlsx_sheet_text_and_table(
            archive,
            name,
            shared_strings=shared_strings,
            sheet_number=sheet_index,
        )
        if sheet_text:
            rows.append(sheet_text)
        if table is not None:
            tables.append(table)
        formula_elements.extend(sheet_formula_elements)
    return "\n\n".join(rows), tables, formula_elements


def _office_xlsx_sheet_text(
    archive: zipfile.ZipFile,
    name: str,
    *,
    shared_strings: Mapping[int, str],
    sheet_number: int,
) -> str:
    text, _table, _formula_elements = _office_xlsx_sheet_text_and_table(
        archive,
        name,
        shared_strings=shared_strings,
        sheet_number=sheet_number,
    )
    return text


def _office_xlsx_sheet_text_and_table(
    archive: zipfile.ZipFile,
    name: str,
    *,
    shared_strings: Mapping[int, str],
    sheet_number: int,
) -> tuple[str, ExtractionTable | None, tuple[DocumentElement, ...]]:
    rows = [f"# sheet {sheet_number}"]
    table_rows: list[list[str]] = []
    formula_cells: list[_XlsxFormulaCell] = []
    with archive.open(name) as handle:
        root = ElementTree.fromstring(handle.read())
    for row in root.findall(".//{*}row"):
        cell_values: dict[int, str] = {}
        row_formula_cells: list[tuple[int, str, str, str]] = []
        for fallback_col_index, cell in enumerate(row.findall("{*}c")):
            detected_col_index = _xlsx_cell_col_index(cell.attrib.get("r"))
            col_index = detected_col_index if detected_col_index is not None else fallback_col_index
            value_text = _xlsx_cell_text(cell, shared_strings=shared_strings)
            formula_text = _xlsx_cell_formula(cell)
            if formula_text and not value_text:
                value_text = f"={formula_text}"
            if value_text:
                cell_values[col_index] = value_text
            if formula_text:
                cell_ref = cell.attrib.get("r") or _xlsx_cell_ref(
                    col_index=col_index,
                    row_ref=row.attrib.get("r"),
                    fallback_row_index=len(table_rows),
                )
                row_formula_cells.append((col_index, cell_ref, formula_text, value_text))
        if cell_values:
            cells = _xlsx_row_values(cell_values)
            table_row_index = len(table_rows)
            table_rows.append(cells)
            rows.append(_xlsx_markdown_row(cells))
            formula_cells.extend(
                _XlsxFormulaCell(
                    cell_ref=cell_ref,
                    row=table_row_index,
                    col=col_index,
                    formula=formula_text,
                    value=value_text,
                )
                for col_index, cell_ref, formula_text, value_text in row_formula_cells
            )
    table = _xlsx_table_from_rows(
        table_rows,
        sheet_number=sheet_number,
        source_path=name,
        formula_cells=formula_cells,
    )
    formula_elements = _xlsx_formula_elements(
        formula_cells,
        sheet_number=sheet_number,
        source_path=name,
        table_id=table.table_id if table is not None else f"xlsx-sheet-{sheet_number}",
    )
    return "\n".join(rows), table, tuple(formula_elements)


def _xlsx_table_from_rows(
    rows: Sequence[Sequence[str]],
    *,
    sheet_number: int,
    source_path: str,
    formula_cells: Sequence[_XlsxFormulaCell] = (),
) -> ExtractionTable | None:
    if not rows:
        return None
    table_id = f"xlsx-sheet-{sheet_number}"
    formula_by_position = {(cell.row, cell.col): cell for cell in formula_cells}
    cells: list[ExtractionTableCell] = []
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            formula_cell = formula_by_position.get((row_index, col_index))
            cells.append(
                ExtractionTableCell(
                    row=row_index,
                    col=col_index,
                    text=str(value).strip(),
                    metadata=(
                        _xlsx_formula_cell_metadata(
                            formula_cell,
                            table_id=table_id,
                            sheet_number=sheet_number,
                            source_path=source_path,
                        )
                        if formula_cell is not None
                        else {"cell_ref": _a1_cell_ref(row_index, col_index)}
                    ),
                )
            )
    column_count = max((len(row) for row in rows), default=0)
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": "local_office_structure",
        "parser_backend": "local_partition",
        "parser_version": LOCAL_PARSER_VERSION,
        "office_segment_number": sheet_number,
        "office_segment_path": source_path,
        "row_count": len(rows),
        "column_count": column_count,
    }
    if formula_cells:
        formula_refs = [cell.cell_ref for cell in formula_cells]
        metadata.update(
            {
                "formula_count": len(formula_cells),
                "formula_format": "excel_formula",
                "formula_cell_refs": "\n".join(formula_refs)[:1000],
                "formula_cells": "\n".join(
                    _xlsx_formula_metadata_line(cell) for cell in formula_cells
                )[:4000],
            }
        )
    return ExtractionTable(
        table_id=table_id,
        element_id=table_id,
        page_number=sheet_number,
        cells=cells,
        metadata=metadata,
    )


def _xlsx_formula_cell_metadata(
    formula_cell: _XlsxFormulaCell,
    *,
    table_id: str,
    sheet_number: int,
    source_path: str,
) -> dict[str, ExtractionMetadataValue]:
    metadata: dict[str, ExtractionMetadataValue] = {
        "source_parser": "local_office_structure",
        "parser_backend": "local_partition",
        "parser_version": LOCAL_PARSER_VERSION,
        "table_id": table_id,
        "office_segment_number": sheet_number,
        "office_segment_path": source_path,
        "equation_format": "excel_formula",
        "cell_ref": formula_cell.cell_ref,
        "formula_cell_ref": formula_cell.cell_ref,
        "formula_cell_row": formula_cell.row,
        "formula_cell_col": formula_cell.col,
        "formula": formula_cell.formula[:1000],
    }
    if formula_cell.value:
        metadata["formula_value"] = formula_cell.value[:1000]
    return metadata


def _xlsx_cell_text(
    cell: ElementTree.Element,
    *,
    shared_strings: Mapping[int, str],
) -> str:
    value = cell.find("{*}v")
    if value is not None and value.text is not None:
        text = value.text.strip()
        if cell.attrib.get("t") == "s" and text.isdigit():
            return shared_strings.get(int(text), text)
        return text
    if cell.attrib.get("t") == "inlineStr":
        inline_text = _xml_text(cell)
        return inline_text
    return ""


def _xlsx_cell_formula(cell: ElementTree.Element) -> str:
    formula = cell.find("{*}f")
    if formula is None:
        return ""
    text = _clean_text("".join(formula.itertext()))
    return text.removeprefix("=").strip()


def _xlsx_cell_col_index(cell_ref: str | None) -> int | None:
    if not cell_ref:
        return None
    match = re.match(r"^\$?([A-Za-z]+)", cell_ref.strip())
    if match is None:
        return None
    col = 0
    for char in match.group(1).upper():
        col = col * 26 + (ord(char) - ord("A") + 1)
    return col - 1 if col > 0 else None


def _xlsx_cell_ref(
    *,
    col_index: int,
    row_ref: str | None,
    fallback_row_index: int,
) -> str:
    row_number = int(row_ref) if row_ref and row_ref.isdigit() else fallback_row_index + 1
    return f"{_xlsx_column_name(col_index)}{row_number}"


def _xlsx_column_name(col_index: int) -> str:
    number = max(0, col_index) + 1
    chars: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars)) or "A"


def _xlsx_row_values(cell_values: Mapping[int, str]) -> list[str]:
    if not cell_values:
        return []
    max_col = max(cell_values)
    values = [cell_values.get(index, "") for index in range(max_col + 1)]
    while values and not values[-1].strip():
        values.pop()
    return values


def _xlsx_formula_elements(
    formula_cells: Sequence[_XlsxFormulaCell],
    *,
    sheet_number: int,
    source_path: str,
    table_id: str,
) -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    for index, formula_cell in enumerate(formula_cells, start=1):
        metadata: dict[str, ExtractionMetadataValue] = {
            "source_parser": "local_office_structure",
            "parser_backend": "local_partition",
            "parser_version": LOCAL_PARSER_VERSION,
            "chunk_template": "office_sheet",
            "equation_format": "excel_formula",
            "formula_cell_ref": formula_cell.cell_ref,
            "formula_cell_row": formula_cell.row,
            "formula_cell_col": formula_cell.col,
            "table_id": table_id,
            "office_segment_number": sheet_number,
            "office_segment_path": source_path,
        }
        if formula_cell.value:
            metadata["formula_value"] = formula_cell.value
        elements.append(
            DocumentElement(
                kind="equation",
                content_kind="equation",
                text=_xlsx_formula_element_text(formula_cell),
                order=10_000 + index,
                element_id=f"{table_id}-formula-{index}",
                parent_id=table_id,
                source_parser="local_office_structure",
                page_number=sheet_number,
                metadata=metadata,
            )
        )
    return elements


def _xlsx_formula_element_text(formula_cell: _XlsxFormulaCell) -> str:
    text = f"{formula_cell.cell_ref} = {formula_cell.formula}"
    if formula_cell.value and formula_cell.value != f"={formula_cell.formula}":
        return f"{text} (値: {formula_cell.value})"
    return text


def _xlsx_formula_metadata_line(formula_cell: _XlsxFormulaCell) -> str:
    if formula_cell.value:
        return f"{formula_cell.cell_ref}={formula_cell.formula}\tvalue={formula_cell.value}"
    return f"{formula_cell.cell_ref}={formula_cell.formula}"


def _xlsx_markdown_row(cells: Sequence[str]) -> str:
    """XLSX 行を table chunking が認識できる Markdown table row にする。"""
    escaped = [cell.replace("|", "\\|").strip() for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> dict[int, str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return {}
    with archive.open("xl/sharedStrings.xml") as handle:
        root = ElementTree.fromstring(handle.read())
    values: dict[int, str] = {}
    for index, item in enumerate(root.findall(".//{*}si")):
        values[index] = _xml_text(item)
    return values


def _xml_text(root: ElementTree.Element) -> str:
    texts = [text.strip() for text in root.itertext() if text and text.strip()]
    return _clean_text("\n".join(texts))


def _xml_local_name(tag: object) -> str:
    text = str(tag)
    if "}" in text:
        return text.rsplit("}", 1)[1]
    return text


# template_for_source_profile は rag_parser_core.source へ移設済み(上で re-import)。
