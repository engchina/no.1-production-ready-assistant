"""PDF の段（ページの範囲）ごとの解析結果を、原本全体の 1 つの抽出結果にまとめる（#2001）。

取込は PDF をページの段（既定 10 ページ）に分けて段ごとに解析サービスを呼ぶ。解析サービスは
段の PDF の中のページ番号（1 始まり）で結果を返すため、段の結果は次の 2 つの手順で原本全体の
ページにそろえる。

1. :func:`extraction_with_page_offset`: 段の結果のページ番号（要素・ページ・表・図・
   ``layout_records`` の page と record）を原本のページにずらし、ページを含む ID（Docling の
   ``docling-p3-5`` など）を原本のページの ID にする。ページを含まない ID は段の先頭のページを
   前に付けて段の間で重ならないようにする。座標（bbox）はページの中の値なのでそのまま。
2. :func:`merge_segment_extractions`: ページの順に要素・ページ・表・図・``layout_records`` を
   つなぎ、要素の順番を振り直す。見出しの階層（``section_path``）は段の境目で前の段の見出しを
   失うため、``layout_records`` がある解析エンジン（Docling）は原本全体の record から作り直し
   （解析サービスと同じ規則。``rag_engine`` の ``layout_section_paths``）、それ以外は段の先頭の
   見出しより前の要素に前の段の最後の ``section_path`` を引き継ぐ。

まとめた後に :func:`consolidate_document_extraction` で文書全体を整える（PDF のしおりに当たる
見出しの深さ・ページの境目で切れた段落・リスト・表の続き。原本全体を 1 回で解析した結果にも
同じ規則で当てる。#2266）。
章節の木（ナビゲーション）・Vision・項目抽出・chunk は、まとめた結果から作る（呼び出し側）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, cast

from pypdf import PdfReader
from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import numbered, text_patterns

from app.rag.pdf_backend import PdfSource, SourceOpenError
from app.rag.pdf_text_correction import correct_docling_pdf_text
from app.schemas.extraction import (
    DocumentElement,
    ExtractionArtifactValue,
    ExtractionAsset,
    ExtractionMetadataValue,
    ExtractionPage,
    ExtractionTable,
    StructuredExtraction,
    section_path_items,
    strip_formatting_marks,
)

LAYOUT_ARTIFACT = "layout_records"
DOCLING_LAYOUT_ENGINE = "docling"
# 段の結果を足し合わせる件数の parser_artifacts(解析サービスが段ごとに数えた値)。
_SUMMED_ARTIFACT_KEYS = (
    "source_page_count",
    "table_count",
    "picture_count",
    "adapter_table_count",
    "figure_count",
    "image_count",
    "asset_count",
)
# ページを含む ID(``docling-p3-5`` / ``table-docling-p3-5``)。ページの部分を原本のページにする。
_PAGE_SCOPED_ID = re.compile(r"^(?P<head>.*?-p)(?P<page>\d+)(?P<tail>-\d+)$")
# 要素の metadata のうち、ページ番号を持つ key。
_PAGE_METADATA_KEYS = ("page", "page_number", "page_start", "page_end")
_ID_MAX_LENGTH = 128
_HEADING_KINDS = frozenset({"title", "heading", "section_header"})


def extraction_with_page_offset(
    extraction: StructuredExtraction,
    *,
    page_start: int,
    page_end: int,
) -> StructuredExtraction:
    """段（原本の ``page_start``〜``page_end``）の解析結果を原本のページ番号へずらす。"""
    offset = page_start - 1
    if offset < 0:
        raise ValueError("page_start は 1 以上で指定してください。")
    segment_page_count = page_end - page_start + 1

    def shift(page: int | None) -> int | None:
        return None if page is None else page + offset

    def scoped(value: str) -> str:
        return _scoped_identifier(
            value,
            offset=offset,
            page_start=page_start,
            segment_page_count=segment_page_count,
        )

    elements = [
        element.model_copy(
            update={
                "element_id": scoped(element.element_id) if element.element_id else None,
                "parent_id": scoped(element.parent_id) if element.parent_id else None,
                "page_number": shift(element.page_number),
                "metadata": _metadata_with_page_offset(element.metadata, offset),
            }
        )
        for element in extraction.elements
    ]
    pages = [
        page.model_copy(
            update={
                "page_number": page.page_number + offset,
                "label": None,
                "element_ids": [scoped(value) for value in page.element_ids],
            }
        )
        for page in extraction.pages
    ]
    tables = [
        table.model_copy(
            update={
                "table_id": scoped(table.table_id),
                "element_id": scoped(table.element_id) if table.element_id else None,
                "page_number": shift(table.page_number),
            }
        )
        for table in extraction.tables
    ]
    assets = [
        asset.model_copy(
            update={
                "asset_id": scoped(asset.asset_id),
                "page_number": shift(asset.page_number),
                "metadata": _asset_metadata_with_scope(asset.metadata, scoped),
            }
        )
        for asset in extraction.assets
    ]
    fields = [
        field.model_copy(
            update={
                "page_number": shift(field.page_number),
                "source_element_id": (
                    scoped(field.source_element_id) if field.source_element_id else None
                ),
            }
        )
        for field in extraction.fields
    ]
    parser_artifacts: dict[str, ExtractionArtifactValue] = dict(extraction.parser_artifacts)
    layout = parser_artifacts.get(LAYOUT_ARTIFACT)
    if isinstance(layout, Mapping):
        parser_artifacts[LAYOUT_ARTIFACT] = _layout_with_page_offset(layout, offset, scoped)
    return extraction.model_copy(
        update={
            "elements": elements,
            "pages": pages,
            "tables": tables,
            "assets": assets,
            "fields": fields,
            # 章節の木はまとめた後に原本全体で作る。
            "navigation": [],
            "parser_artifacts": parser_artifacts,
        }
    )


def merge_segment_extractions(
    segments: Sequence[tuple[int, StructuredExtraction]],
) -> StructuredExtraction:
    """原本のページにそろえた段の結果（``(page_start, extraction)``）をページの順にまとめる。"""
    ordered = [extraction for _, extraction in sorted(segments, key=lambda item: item[0])]
    if not ordered:
        return StructuredExtraction()
    if len(ordered) == 1:
        return ordered[0]

    elements: list[DocumentElement] = []
    pages: list[ExtractionPage] = []
    tables: list[ExtractionTable] = []
    assets: list[ExtractionAsset] = []
    raw_parts: list[str] = []
    warnings: list[str] = []
    fields = []
    layout_pages: list[Any] = []
    layout_records: list[Any] = []
    layout_version: object = None
    has_layout = False
    default_type = current_pack().text("labels", "default_document_type")
    document_type = default_type
    previous_section_path: list[str] = []
    for extraction in ordered:
        if document_type == default_type and extraction.document_type:
            document_type = extraction.document_type
        text = extraction.raw_text.strip()
        if text:
            raw_parts.append(text)
        warnings.extend(extraction.warnings)
        segment_elements = _elements_with_inherited_section_path(
            extraction.elements, previous_section_path
        )
        for element in segment_elements:
            elements.append(element.model_copy(update={"order": len(elements)}))
        if segment_elements:
            previous_section_path = list(segment_elements[-1].section_path)
        pages.extend(extraction.pages)
        tables.extend(extraction.tables)
        assets.extend(extraction.assets)
        fields.extend(extraction.fields)
        layout = extraction.parser_artifacts.get(LAYOUT_ARTIFACT)
        if isinstance(layout, Mapping):
            has_layout = True
            layout_version = layout_version if layout_version is not None else layout.get("version")
            layout_pages.extend(_list(layout.get("pages")))
            layout_records.extend(_list(layout.get("records")))

    parser_artifacts: dict[str, ExtractionArtifactValue] = {
        key: value
        for key, value in ordered[0].parser_artifacts.items()
        if key != LAYOUT_ARTIFACT and key not in _SUMMED_ARTIFACT_KEYS
    }
    for key in _SUMMED_ARTIFACT_KEYS:
        counts = [extraction.parser_artifacts.get(key) for extraction in ordered]
        ints = [value for value in counts if isinstance(value, int) and _is_count(value)]
        if counts and len(ints) == len(counts):
            parser_artifacts[key] = sum(ints)
    if has_layout:
        layout_payload: dict[str, Any] = {"pages": layout_pages, "records": layout_records}
        if layout_version is not None:
            layout_payload = {"version": layout_version, **layout_payload}
        parser_artifacts[LAYOUT_ARTIFACT] = layout_payload
        elements = elements_with_layout_section_paths(elements, layout_records)

    return StructuredExtraction(
        raw_text="\n\n".join(raw_parts),
        document_type=document_type,
        confidence=sum(extraction.confidence for extraction in ordered) / len(ordered),
        warnings=_dedupe(warnings),
        elements=elements,
        pages=sorted(pages, key=lambda page: page.page_number),
        tables=tables,
        assets=assets,
        fields=fields,
        parser_artifacts=parser_artifacts,
    )


def _scoped_identifier(
    value: str,
    *,
    offset: int,
    page_start: int,
    segment_page_count: int,
) -> str:
    """段の中の ID を原本で一意な ID にする（ページを含む ID はページを原本のページにする）。"""
    match = _PAGE_SCOPED_ID.match(value)
    if match is not None:
        page = int(match.group("page"))
        if 1 <= page <= segment_page_count:
            return f"{match.group('head')}{page + offset}{match.group('tail')}"[:_ID_MAX_LENGTH]
    if offset == 0:
        return value
    return f"p{page_start}-{value}"[:_ID_MAX_LENGTH]


def _metadata_with_page_offset(
    metadata: Mapping[str, ExtractionMetadataValue], offset: int
) -> dict[str, ExtractionMetadataValue]:
    updated = dict(metadata)
    for key in _PAGE_METADATA_KEYS:
        value = updated.get(key)
        if _is_count(value) and int(value) >= 1:  # type: ignore[arg-type]
            updated[key] = int(value) + offset  # type: ignore[arg-type]
    return updated


def _asset_metadata_with_scope(
    metadata: Mapping[str, ExtractionMetadataValue], scoped: Any
) -> dict[str, ExtractionMetadataValue]:
    updated = dict(metadata)
    element_id = updated.get("element_id")
    if isinstance(element_id, str) and element_id:
        updated["element_id"] = scoped(element_id)
    return updated


def _layout_with_page_offset(layout: Mapping[str, Any], offset: int, scoped: Any) -> dict[str, Any]:
    pages = []
    for page in _list(layout.get("pages")):
        if isinstance(page, Mapping) and _is_count(page.get("page")):
            pages.append({**page, "page": int(page["page"]) + offset})
        else:
            pages.append(page)
    records = []
    for record in _list(layout.get("records")):
        if not isinstance(record, Mapping):
            records.append(record)
            continue
        updated = dict(record)
        if _is_count(record.get("page")):
            updated["page"] = int(record["page"]) + offset
        if isinstance(record.get("id"), str) and record["id"]:
            updated["id"] = scoped(record["id"])
        records.append(updated)
    return {**layout, "pages": pages, "records": records}


def _elements_with_inherited_section_path(
    elements: Sequence[DocumentElement], previous: Sequence[str]
) -> list[DocumentElement]:
    """段の先頭の見出しより前の要素に、前の段の最後の ``section_path`` を引き継ぐ。

    段の解析は前の段の見出しを知らないため、段の先頭の本文の ``section_path`` は空になる
    （原本全体の解析なら前の見出しの下に入る）。``layout_records`` の無い解析エンジンのための補い。
    """
    if not previous:
        return list(elements)
    result: list[DocumentElement] = []
    inheriting = True
    for element in elements:
        if inheriting and (element.kind in _HEADING_KINDS or element.section_path):
            inheriting = False
        if inheriting:
            result.append(element.model_copy(update={"section_path": _section_path(previous)}))
        else:
            result.append(element)
    return result


def elements_with_layout_section_paths(
    elements: Sequence[DocumentElement], records: Sequence[Any]
) -> list[DocumentElement]:
    """Docling の要素の ``section_path`` を、原本全体の ``layout_records`` から作り直す。

    Docling サービスは段の中の record だけで見出しの階層（番号の体系から決める深さ）を作るため、
    段の境目で前の段の見出しを失い、番号の体系の判定も段ごとに変わりうる。解析サービスと同じ規則
    （``layout_section_paths``）を原本全体の record に当て、原本全体の解析と同じ ``section_path``
    にする。Docling 以外の engine の record は要素の作り方が違うため変えない。
    """
    dict_records = [record for record in records if isinstance(record, Mapping)]
    if not dict_records or any(
        str(record.get("engine") or "").strip().casefold() != DOCLING_LAYOUT_ENGINE
        for record in dict_records
    ):
        return list(elements)
    from rag_engine.chunking.headings import layout_section_paths

    paths = layout_section_paths([dict(record) for record in dict_records])
    return [
        (
            element.model_copy(update={"section_path": _section_path(paths[element.element_id])})
            if element.element_id is not None and element.element_id in paths
            else element
        )
        for element in elements
    ]


def _section_path(items: Sequence[str]) -> list[str]:
    """``DocumentElement.section_path`` の検証と同じ正規化(``section_path_items``)。

    ``model_copy`` は検証を通らないため、解析サービスが返す要素と同じ形にそろえる。
    """
    return section_path_items(str(item) for item in items)


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# --- 文書全体の整理(#2001) -----------------------------------------------------------------
#
# 段をまとめた後(と原本全体の解析の後)に、layout_records を文書全体で整える。
# 1. 見出しの階層(Docling だけ): PDF のしおり(outline)があれば、しおりの見出しと同じ本文の
#    見出しの深さをしおりの深さにする(record の raw.heading_level。chunk の section_path と章節の
#    木の両方が読む)。
#    しおりの無い見出しは、今までどおり番号の体系から深さを決める(rag_engine の headings)。
# 2. ページの境目で切れた段落・リスト・表をつなぐ: 前のページの最後の本文と次のページの最初の本文が
#    続きなら、続きの record に前の record の ID(raw.continues_record_id)を付ける。親子階層の分割
#    (rag_engine の builder)はこの印のある record を前の record と同じ子 chunk に入れる(段落が
#    ページの境目で別の chunk に割れない)。表の続きには前の表の列見出しの行を付ける
#    (raw.table_enhanced_html。続きの行だけの chunk でも列の意味が分かる)。record はページごとに
#    残すため、ページ番号・座標(引用・プレビューの位置)は変わらない。
# 3. 段落の続きの元・続きは、図・表・caption・脚注・ページの定型を読み飛ばして探し(ページ下端の
#    図の後ろの続き。#2273)、文の終わりの規則に加えて PDF の文字の層の行の位置(前の段落の最後の行が
#    右端まで届く・次の段落の最初の行に字下げが無い・幅が近い)で確かめる(閾値は設定。行の取れない
#    ページは文の終わりの規則だけで判定する)。

CONSOLIDATION_ARTIFACT = "document_consolidation"
OUTLINE_HEADING_SOURCE = "pdf_outline"
_SECTION_CATEGORIES = frozenset({"Title", "Section-header"})
_PARAGRAPH_CATEGORIES = frozenset({"Text", "List-item"})
# ページの境目の判定で読み飛ばす record(ページの上下の定型・脚注)。
_BOUNDARY_SKIP_CATEGORIES = frozenset({"Page-header", "Page-footer", "Footnote"})
# 段落の続きの元・続きを探すときに読み飛ばす record(図・表・caption と、表の外の文字の補い)。
# 見出し・数式などほかの record では探すのをやめる(#2273)。
_TEXT_TRANSPARENT_CATEGORIES = _BOUNDARY_SKIP_CATEGORIES | {"Picture", "Table", "Caption"}
_TEXT_TRANSPARENT_RAW_TYPES = frozenset({"table_unassigned_text", "picture_ocr_text"})
# 文・項目の終わり(前のページの本文がこれで終わっていれば、次のページは続きではない)と、章・条の
# 書き方は言語資源(#2181)。
_item_starts: dict[str, re.Pattern[str]] = {}


def _item_start() -> re.Pattern[str]:
    """次のページの本文が新しい項目・段落の始まりとみなす先頭(箇条書きの記号・番号)。"""
    text = text_patterns()
    markers = marker_patterns()
    key = current_pack().hash
    pattern = _item_starts.get(key)
    if pattern is None:
        units = "|".join(
            (text.chapter_units, text.section_units, text.article_units, text.paragraph_units)
        )
        pattern = re.compile(
            rf"^\s*(?:{markers.bullet}|{markers.circled}"
            rf"|{markers.number_open}?\d{{1,3}}{markers.number_closer}"
            rf"|{markers.number_open}{text.number}{markers.number_close}"
            rf"|{numbered(text, f'(?:{units})')})"
        )
        _item_starts.clear()
        _item_starts[key] = pattern
    return pattern


_TABLE_ROW = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_BODY_START = re.compile(r"<tbody\b[^>]*>|<table\b[^>]*>", re.IGNORECASE)
_OUTLINE_MAX_TITLE_CHARS = 120
_OUTLINE_TEXT_HEADING_MAX_CHARS = 80
# しおりと本文の見出しを同じとみなす近さ(表記の揺れ・Docling の改行の位置の違い)。
_OUTLINE_SIMILARITY = 0.75
_OUTLINE_MIN_CONTAINED_CHARS = 4
# しおりのうち本文の見出しに当たった割合がこれ未満なら、しおりを使わない。
_OUTLINE_MIN_COVERAGE = 0.6


@dataclass(frozen=True)
class ContinuationGeometry:
    """ページの境目の段落の続きの幾何の条件(#2273)。

    ``right_edge_tolerance`` / ``indent_tolerance`` は行の高さの倍数、``width_difference_ratio`` は
    2 つの段落の狭いほうの幅の倍数。値は設定(``RAG_PAGE_CONTINUATION_*``)から作る。
    """

    enabled: bool = True
    right_edge_tolerance: float = 1.0
    indent_tolerance: float = 0.5
    width_difference_ratio: float = 1.0

    @classmethod
    def from_settings(cls, settings: object) -> ContinuationGeometry:
        default = cls()
        return cls(
            enabled=bool(
                getattr(settings, "rag_page_continuation_geometry_enabled", default.enabled)
            ),
            right_edge_tolerance=float(
                getattr(
                    settings,
                    "rag_page_continuation_right_edge_tolerance",
                    default.right_edge_tolerance,
                )
            ),
            indent_tolerance=float(
                getattr(
                    settings, "rag_page_continuation_indent_tolerance", default.indent_tolerance
                )
            ),
            width_difference_ratio=float(
                getattr(
                    settings,
                    "rag_page_continuation_width_difference_ratio",
                    default.width_difference_ratio,
                )
            ),
        )


def consolidate_document_extraction(
    extraction: StructuredExtraction,
    *,
    source_bytes: bytes,
    geometry: ContinuationGeometry | None = None,
) -> StructuredExtraction:
    """Docling の解析結果を文書全体で整える(しおりの見出しの深さ・ページの境目の続き)。

    record の解析エンジンが Docling にそろった layout_records が無い解析結果は変えない。
    ``geometry`` は段落の続きの幾何の条件(省略は既定値)。何度当てても同じ結果になる。
    """
    layout = extraction.parser_artifacts.get(LAYOUT_ARTIFACT)
    if not isinstance(layout, Mapping):
        return extraction
    raw_records = [record for record in _list(layout.get("records")) if isinstance(record, Mapping)]
    engines = {str(record.get("engine") or "").strip().casefold() for record in raw_records}
    if engines != {DOCLING_LAYOUT_ENGINE}:
        return extraction
    records = [_copy_record(record) for record in raw_records]
    corrected_text = correct_docling_pdf_text(records, source_bytes)
    # 原文で裏付けて戻した後に、残る書式の飾りの結合の線を除く(#3354)。record の text は章節の
    # 道・見出しの深さ・親子分割の入力になる。record の raw は診断のため解析の出力のまま残す。
    for layout_record in records:
        if isinstance(layout_record.get("text"), str):
            layout_record["text"] = strip_formatting_marks(layout_record["text"])
    outline = pdf_outline_entries(source_bytes)
    outline_headings = _apply_outline_levels(records, outline)
    resolved_geometry = geometry or ContinuationGeometry()
    lines = _PdfTextLines(source_bytes) if resolved_geometry.enabled else None
    try:
        continuations = _link_page_continuations(records, geometry=resolved_geometry, lines=lines)
    finally:
        if lines is not None:
            lines.close()
    continued_paragraphs, continued_tables, carried_headers = continuations[:3]
    geometry_rejected = continuations[3]

    by_id = {str(record.get("id") or ""): record for record in records}
    elements: list[DocumentElement] = []
    for element in extraction.elements:
        record = by_id.get(element.element_id or "")
        if record is None:
            elements.append(element)
            continue
        raw = _raw(record)
        metadata = dict(element.metadata)
        updates: dict[str, Any] = {}
        if element.element_id in corrected_text:
            # model_copy は検証を通らないため、要素の本文の検証と同じ規則を当てる(#3354)。
            updates["text"] = strip_formatting_marks(str(record["text"])).strip()
            metadata["pdf_text_correction"] = raw["pdf_text_correction"]
        continues = raw.get("continues_record_id")
        if isinstance(continues, str) and continues:
            metadata["continues_element_id"] = continues
        if raw.get("heading_source") == OUTLINE_HEADING_SOURCE:
            metadata["heading_source"] = OUTLINE_HEADING_SOURCE
            metadata["heading_level"] = int(raw.get("heading_level") or 0)
            if str(record.get("category") or "") in _SECTION_CATEGORIES and element.kind != "title":
                updates["kind"] = "title"
        updates["metadata"] = metadata
        elements.append(element.model_copy(update=updates))
    elements = elements_with_layout_section_paths(elements, records)

    table_ids = {
        table.element_id: table.table_id for table in extraction.tables if table.element_id
    }
    tables: list[ExtractionTable] = []
    for table in extraction.tables:
        record = by_id.get(table.element_id or "")
        continues = _raw(record).get("continues_record_id") if record is not None else None
        if isinstance(continues, str) and continues:
            tables.append(
                table.model_copy(
                    update={
                        "metadata": {
                            **table.metadata,
                            "continued_from_table_id": table_ids.get(
                                continues, f"table-{continues}"
                            ),
                        }
                    }
                )
            )
        else:
            tables.append(table)

    parser_artifacts: dict[str, ExtractionArtifactValue] = {
        **extraction.parser_artifacts,
        LAYOUT_ARTIFACT: cast(ExtractionArtifactValue, {**layout, "records": records}),
        CONSOLIDATION_ARTIFACT: {
            "outline_entries": len(outline),
            "outline_headings": outline_headings,
            "continued_paragraphs": continued_paragraphs,
            "continued_tables": continued_tables,
            "table_headers_carried": carried_headers,
            "continuations_rejected_by_geometry": geometry_rejected,
        },
    }
    updates = {"elements": elements, "tables": tables, "parser_artifacts": parser_artifacts}
    if corrected_text:
        # Vision が足した本文などを再構成で落とさず、元の要素の出現順で訂正箇所だけ変える。
        original_raw_text = extraction.raw_text
        parts: list[str] = []
        cursor = 0
        for before, after in zip(extraction.elements, elements, strict=True):
            if not before.text.strip():
                continue
            start = original_raw_text.find(before.text, cursor)
            if start < 0:
                continue
            parts.extend((original_raw_text[cursor:start], after.text))
            cursor = start + len(before.text)
        updates["raw_text"] = "".join(parts) + original_raw_text[cursor:]
    return extraction.model_copy(update=updates)


def pdf_outline_entries(source_bytes: bytes) -> list[tuple[int, str, int]]:
    """PDF のしおり(outline)の (深さ 1〜, 見出し, ページ 1〜)。読めなければ空。"""
    if not source_bytes.startswith(b"%PDF"):
        return []
    try:
        reader = PdfReader(BytesIO(source_bytes))
        if reader.is_encrypted:
            reader.decrypt("")
        outline = reader.outline
    except Exception:  # noqa: BLE001 - しおりは補助。読めない PDF は番号の体系で決める。
        return []
    entries: list[tuple[int, str, int]] = []

    def walk(items: Sequence[Any], depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            page_index = _outline_page_index(reader, item)
            if title and page_index is not None and page_index >= 0:
                entries.append((depth, title[:_OUTLINE_MAX_TITLE_CHARS], page_index + 1))

    walk(outline if isinstance(outline, list) else [], 1)
    return entries


def _outline_page_index(reader: PdfReader, item: Any) -> int | None:
    """しおりの行き先のページ(0 始まり)。壊れた entry は None(飛ばす)。"""
    try:
        return reader.get_destination_page_number(item)
    except Exception:  # noqa: BLE001 - 壊れた entry は飛ばす。
        return None


def _apply_outline_levels(
    records: list[dict[str, Any]], outline: Sequence[tuple[int, str, int]]
) -> int:
    """しおりの見出しに当たる本文の見出し record に、しおりの深さを付ける。付けた数を返す。

    しおりの行き先のページ(前後 1 ページまで。目次のページの同じ文字列に当てない)の見出しで、
    文字が同じ・一方が他方を含む・よく似る(表記の揺れ・Docling の改行の位置の違い)ものに当てる。
    本文(Text)と分類された見出しは、短く文字が同じときだけ見出しにする。
    しおりの多くが本文の見出しに当たらない文書(スライドの区分だけのしおりなど)には当てない。
    一部の見出しだけに深さを付けると、残りの見出しが前の見出しの配下に入り、階層がずれるため。
    """
    if not outline:
        return 0
    remaining = list(enumerate(outline))
    matches: list[tuple[dict[str, Any], int]] = []
    for record in sorted(records, key=_record_order):
        category = str(record.get("category") or "")
        text = str(record.get("text") or "").strip()
        if category not in _SECTION_CATEGORIES | _PARAGRAPH_CATEGORIES or not text:
            continue
        if category in _PARAGRAPH_CATEGORIES and len(text) > _OUTLINE_TEXT_HEADING_MAX_CHARS:
            continue
        page = int(record.get("page") or 0)
        key = _heading_key(text)
        best: tuple[float, int] | None = None
        for position, (_index, (_level, title, outline_page)) in enumerate(remaining):
            if abs(outline_page - page) > 1:
                continue
            score = _outline_title_score(
                key, _heading_key(title), exact=category not in _SECTION_CATEGORIES
            )
            score -= abs(outline_page - page) * 0.01
            if score > 0 and (best is None or score > best[0]):
                best = (score, position)
        if best is None:
            continue
        _index, (level, _title, _page) = remaining.pop(best[1])
        matches.append((record, level))
    if len(matches) < len(outline) * _OUTLINE_MIN_COVERAGE:
        return 0
    for record, level in matches:
        record["raw"] = {
            **_raw(record),
            "heading_level": level,
            "heading_source": OUTLINE_HEADING_SOURCE,
        }
        if str(record.get("category") or "") in _PARAGRAPH_CATEGORIES:
            record["category"] = "Section-header"
    return len(matches)


def _outline_title_score(text_key: str, title_key: str, *, exact: bool) -> float:
    """本文の見出しとしおりの見出しの近さ(0 は当てない)。"""
    if not text_key or not title_key:
        return 0.0
    if text_key == title_key:
        return 1.0
    if exact:
        return 0.0
    shorter, longer = sorted((text_key, title_key), key=len)
    if len(shorter) >= _OUTLINE_MIN_CONTAINED_CHARS and shorter in longer:
        return 0.9 if len(shorter) / len(longer) >= 0.5 else 0.0
    ratio = SequenceMatcher(None, text_key, title_key).ratio()
    return ratio if ratio >= _OUTLINE_SIMILARITY else 0.0


def _link_page_continuations(
    records: list[dict[str, Any]],
    *,
    geometry: ContinuationGeometry | None = None,
    lines: _PdfTextLines | None = None,
) -> tuple[int, int, int, int]:
    """ページの境目で切れた段落・リスト・表の続きに印を付ける。

    表の続きはページの端の表どうし、段落の続きは図・表・定型を読み飛ばした端の本文どうしで判定する。
    ``lines`` があれば段落の続きを行の位置(``geometry``)でも確かめる。
    返り値は (つないだ段落, つないだ表, 列見出しの行を付けた表, 行の位置で続きにしなかった段落)
    の数。
    """
    by_page: dict[int, list[dict[str, Any]]] = {}
    for record in sorted(records, key=_record_order):
        page = int(record.get("page") or 0)
        if page >= 1:
            by_page.setdefault(page, []).append(record)
    resolved_geometry = geometry or ContinuationGeometry()
    paragraphs = tables = headers = rejected = 0
    for page in sorted(by_page):
        following = by_page.get(page + 1)
        if not following:
            continue
        ending = list(reversed(by_page[page]))
        previous = _boundary_record(ending)
        current = _boundary_record(following)
        if (
            previous is not None
            and current is not None
            and str(previous.get("category") or "") == "Table"
            and str(current.get("category") or "") == "Table"
            and str(previous.get("id") or "")
        ):
            if _raw(current).get("continues_record_id"):
                # 前に当てた整理(cache から再開した抽出など)。数だけ数え、印は変えない。
                tables += 1
                headers += int(bool(_raw(current).get("table_header_carried")))
                continue
            carried = _table_continuation(previous, current)
            if carried is not None:
                tables += 1
                headers += int(carried)
            continue
        previous_text = _boundary_text_record(ending)
        current_text = _boundary_text_record(following)
        if previous_text is None or current_text is None:
            continue
        previous_id = str(previous_text.get("id") or "")
        if not previous_id:
            continue
        if _raw(current_text).get("continues_record_id"):
            paragraphs += 1
            continue
        if not _paragraph_continues(
            str(previous_text.get("text") or ""), str(current_text.get("text") or "")
        ):
            continue
        if (
            lines is not None
            and resolved_geometry.enabled
            and not _geometry_allows_continuation(
                lines.lines_in(previous_text), lines.lines_in(current_text), resolved_geometry
            )
        ):
            rejected += 1
            continue
        current_text["raw"] = {**_raw(current_text), "continues_record_id": previous_id}
        paragraphs += 1
    return paragraphs, tables, headers, rejected


def _boundary_record(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """ページの端の本文の record(定型・脚注・空の record は読み飛ばす)。"""
    for record in records:
        category = str(record.get("category") or "")
        if category in _BOUNDARY_SKIP_CATEGORIES:
            continue
        if not str(record.get("text") or "").strip() and category != "Picture":
            continue
        return record
    return None


def _boundary_text_record(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """ページの端の段落の record(図・表・caption・定型・脚注・空の record は読み飛ばす)。

    見出し・数式など段落でない record に当たったら、続きの元・続きは無い(None)。
    """
    for record in records:
        category = str(record.get("category") or "")
        if category in _TEXT_TRANSPARENT_CATEGORIES:
            continue
        if str(record.get("raw_type") or "") in _TEXT_TRANSPARENT_RAW_TYPES:
            continue
        if not str(record.get("text") or "").strip():
            continue
        return record if category in _PARAGRAPH_CATEGORIES else None
    return None


# 行の bbox(PDF の pt。左上原点)。
_LineBox = tuple[float, float, float, float]
# 同じ行とみなす縦の重なり(低いほうの行の高さに対する割合)と、record の bbox に行を含めるときの
# 余白(pt)。PDF の文字の層の行の切れ方の揺れを吸収する値で、文書の内容には依らない。
_SAME_ROW_OVERLAP = 0.5
_RECORD_BBOX_MARGIN_PT = 2.0


def _geometry_allows_continuation(
    previous: Sequence[_LineBox] | None,
    current: Sequence[_LineBox] | None,
    geometry: ContinuationGeometry,
) -> bool:
    """行の位置が段落の続きと矛盾しないか(行が取れない・1 行だけの側の条件は判定しない)。

    - 前の段落が 2 行以上: 最後の行の右端が段落の右端まで届く。
    - 次の段落が 2 行以上: 最初の行に字下げが無い。
    - 両方が 2 行以上: 2 つの段落の幅が近い。
    """
    if previous and len(previous) >= 2:
        last = previous[-1]
        right = max(line[2] for line in previous)
        if right - last[2] >= (last[3] - last[1]) * geometry.right_edge_tolerance:
            return False
    if current and len(current) >= 2:
        first = current[0]
        left = min(line[0] for line in current)
        if first[0] - left >= (first[3] - first[1]) * geometry.indent_tolerance:
            return False
    if previous and current and len(previous) >= 2 and len(current) >= 2:
        previous_width = max(line[2] for line in previous) - min(line[0] for line in previous)
        current_width = max(line[2] for line in current) - min(line[0] for line in current)
        narrower = min(previous_width, current_width)
        if narrower <= 0:
            return True
        if abs(previous_width - current_width) >= narrower * geometry.width_difference_ratio:
            return False
    return True


class _PdfTextLines:
    """PDF の文字の層の横書きの行(record の bbox の中の行)を読む。

    文字の層の無いページ(スキャン)・回転したページ・縦書きの行は返さない(None)。PDF でない・
    開けない原本も None。
    """

    def __init__(self, source_bytes: bytes) -> None:
        self._source = source_bytes
        self._document: PdfSource | None = None
        self._opened = False
        self._pages: dict[int, list[_LineBox] | None] = {}

    def close(self) -> None:
        if self._document is not None:
            self._document.close()
            self._document = None

    def lines_in(self, record: Mapping[str, Any]) -> list[_LineBox] | None:
        """record の bbox の中の行(上から順。同じ高さの行はまとめる)。取れなければ None。"""
        page_number = int(record.get("page") or 0)
        bbox = record.get("bbox")
        if (
            str(record.get("coord_system") or "") != "image_top_left"
            or not isinstance(bbox, list | tuple)
            or len(bbox) < 4
        ):
            return None
        page = self._page(page_number)
        if page is None:
            return None
        page_lines, page_width, page_height = page
        try:
            scale_x = page_width / float(record.get("page_width") or 0)
            scale_y = page_height / float(record.get("page_height") or 0)
            x0, y0, x1, y1 = (float(value) for value in bbox[:4])
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        margin = _RECORD_BBOX_MARGIN_PT
        left, top = x0 * scale_x - margin, y0 * scale_y - margin
        right, bottom = x1 * scale_x + margin, y1 * scale_y + margin
        inside = [
            line
            for line in page_lines
            if left <= (line[0] + line[2]) / 2 <= right and top <= (line[1] + line[3]) / 2 <= bottom
        ]
        rows = _merge_rows(inside)
        return rows or None

    def _page(self, page_number: int) -> tuple[list[_LineBox], float, float] | None:
        if page_number < 1:
            return None
        if page_number not in self._pages:
            self._pages[page_number] = self._read_page(page_number)
        lines = self._pages[page_number]
        if lines is None or self._document is None:
            return None
        width, height = self._document.page_size(page_number - 1)
        return lines, width, height

    def _read_page(self, page_number: int) -> list[_LineBox] | None:
        document = self._open()
        if document is None or page_number > document.page_count:
            return None
        try:
            if document.rotation(page_number - 1):
                return None
            text_lines = document.text_lines(page_number - 1)
        except Exception:  # noqa: BLE001 - 行の位置は補助。読めないページは文の終わりの規則だけ。
            return None
        lines: list[_LineBox] = []
        for line in text_lines:
            if not line.text.strip():
                continue
            if abs(line.dir[1]) > 0.1 or line.dir[0] <= 0:
                continue  # 縦書き・回転した行。
            x0, y0, x1, y1 = line.bbox
            if x1 > x0 and y1 > y0:
                lines.append((x0, y0, x1, y1))
        return lines

    def _open(self) -> PdfSource | None:
        if not self._opened:
            self._opened = True
            if self._source.startswith(b"%PDF"):
                try:
                    self._document = PdfSource(self._source)
                except SourceOpenError:  # 開けない PDF は文の終わりの規則だけ。
                    self._document = None
        return self._document


def _merge_rows(lines: Sequence[_LineBox]) -> list[_LineBox]:
    """縦に重なる行(同じ行が文字の層で分かれたもの)を 1 行にまとめ、上から順に並べる。"""
    rows: list[_LineBox] = []
    for line in sorted(lines, key=lambda item: (item[1], item[0])):
        if rows:
            last = rows[-1]
            overlap = min(last[3], line[3]) - max(last[1], line[1])
            height = min(last[3] - last[1], line[3] - line[1])
            if height > 0 and overlap >= height * _SAME_ROW_OVERLAP:
                rows[-1] = (
                    min(last[0], line[0]),
                    min(last[1], line[1]),
                    max(last[2], line[2]),
                    max(last[3], line[3]),
                )
                continue
        rows.append(line)
    return rows


def _paragraph_continues(previous: str, current: str) -> bool:
    previous = previous.rstrip()
    current = current.lstrip()
    if not previous or not current:
        return False
    if previous[-1] in text_patterns().paragraph_terminal:
        return False
    return not _item_start().match(current)


def _table_continuation(
    previous: dict[str, Any], current: dict[str, Any], *, known: bool = False
) -> bool | None:
    """表の続きなら印を付け、列見出しの行を付けたかを返す。続きでなければ None。

    列の数が同じで、前の表に列見出しの行があり、続きの表の先頭が列見出しでない(または前の表と
    同じ列見出しを繰り返している)ときに続きとみなす。``known`` は解析エンジンが続きと判定した表で、
    形が合わなくても印を付け、列見出しの行は形が合うときだけ付ける。
    """
    from rag_engine.parsing.table_structure import parse_html_table_structure

    previous_html = str(previous.get("text") or "")
    current_html = str(current.get("text") or "")
    previous_structure = parse_html_table_structure(previous_html)
    current_structure = parse_html_table_structure(current_html)
    header_rows = int(previous_structure.get("header_row_count") or 0) if previous_structure else 0
    current_headers = (
        int(current_structure.get("header_row_count") or 0) if current_structure else 0
    )
    compatible = bool(
        previous_structure
        and current_structure
        and previous_structure.get("column_count") == current_structure.get("column_count")
        and header_rows >= 1
        and (
            not current_headers
            or current_structure.get("column_headers") == previous_structure.get("column_headers")
        )
    )
    if not compatible and not known:
        return None
    raw = {**_raw(current), "continues_record_id": str(previous.get("id") or "")}
    carried = False
    if compatible and not current_headers:
        header_html = "".join(_TABLE_ROW.findall(previous_html)[:header_rows])
        match = _TABLE_BODY_START.search(current_html)
        if header_html and match is not None and not raw.get("table_enhanced_html"):
            raw["table_enhanced_html"] = (
                current_html[: match.end()] + header_html + current_html[match.end() :]
            )
            raw["table_header_carried"] = True
            carried = True
    current["raw"] = raw
    return carried


def _heading_key(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).casefold()


def _record_order(record: Mapping[str, Any]) -> tuple[int, int]:
    return int(record.get("page") or 0), int(record.get("seq_no") or 0)


def _raw(record: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = record.get("raw") if record is not None else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def _copy_record(record: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(record)
    if isinstance(record.get("raw"), Mapping):
        copied["raw"] = dict(record["raw"])
    return copied
