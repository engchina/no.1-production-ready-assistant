"""Docling の解析(Docling)を実行し、共通抽出 schema へ変換する。

rag_poc の analyze_pdf から UI・run 保存・再開・Vision を除いた最小のオーケストレーション。
LayoutRecord 全体(raw を含む)は parser_artifacts["layout_records"] に保持し、
Small-to-Big チャンク化(backend)が rag_poc と同じ入力で分割できるようにする。
図・画像の Vision は解析エンジンに依存しない backend の共通の段(``app.rag.vision``)が
layout_records の record を読み取って反映する(#497)。このサービスは LLM を呼ばない。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rag_engine.adapters.parsers.base import AnalysisContext
from rag_engine.adapters.parsers.docling_adapter import DoclingAdapter, docling_table_cells
from rag_engine.chunking.headings import layout_section_paths
from rag_engine.config import Settings, get_settings
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.layout_metadata import (
    layout_record_element_metadata,
    layout_record_vision_summary,
)
from rag_engine.parsing.rendering import (
    SUPPORTED_SOURCE_FILE_TYPES,
    get_source_page_count,
    prepare_source_for_analysis,
    source_frame_warnings,
)
from rag_engine.parsing.visual_artifacts import persist_semantic_visual_crops
from rag_parser_core.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionMetadataValue,
    ExtractionPage,
    ExtractionTable,
    StructuredExtraction,
)
from rag_parser_core.parsed_table import PARSED_TABLE_GRID_KEY, PARSED_TABLE_GRID_VERSION

logger = logging.getLogger(__name__)

# 解析中の進捗の id → 実行のディレクトリ(#1999)。サービスは 1 worker で動き、解析は thread で
# 実行するため、同じプロセスの event loop から進捗を読める。解析の終わりに消す。
_ACTIVE_RUNS: dict[str, Path] = {}

LAYOUT_ARTIFACT = "layout_records"
LAYOUT_RECORDS_VERSION = 1

# rag_poc の正規化カテゴリ → 共通抽出 schema の element kind。
CATEGORY_KINDS = {
    "Title": "title",
    "Section-header": "title",
    "Caption": "figure_caption",
    "Footnote": "text",
    "Formula": "equation",
    "List-item": "list",
    "Page-footer": "footer",
    "Page-header": "header",
    "Picture": "figure",
    "Table": "table",
    "Text": "text",
}


def analyze_source(
    source_bytes: bytes,
    *,
    file_name: str,
    content_type: str,
    settings: Settings | None = None,
    progress_id: str | None = None,
) -> StructuredExtraction:
    """PDF / 画像を解析し、LayoutRecord を保持した StructuredExtraction を返す。

    ``progress_id`` を渡すと、解析の間はその id で実行のディレクトリの進捗(Docling のページの
    ``progress.json``)を ``read_progress`` で読めるようにする(取込の job の進捗。#1999)。
    """
    # env(RAG_ENGINE_* / DOCLING_*)から解決する。.env は読まない。
    settings = settings or get_settings(dotenv_path=None)
    suffix = _source_suffix(file_name, content_type)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.output_dir) as work:
        run_dir = Path(work)
        if progress_id is not None:
            _ACTIVE_RUNS[progress_id] = run_dir
        try:
            source_path = run_dir / f"input{suffix}"
            source_path.write_bytes(source_bytes)
            page_count = get_source_page_count(source_path)
            pdf_path, pages = prepare_source_for_analysis(
                source_path, list(range(1, page_count + 1)), run_dir, settings.render_dpi
            )
            records = DoclingAdapter(settings).analyze(
                AnalysisContext(
                    pdf_path=Path(pdf_path), run_dir=run_dir, pages=pages, settings=settings
                )
            )
            persist_semantic_visual_crops(records, pages, run_dir=run_dir)
            warnings = list(source_frame_warnings(source_path))
            return layout_to_extraction(
                records,
                pages,
                source_page_count=page_count,
                warnings=warnings,
            )
        finally:
            if progress_id is not None:
                _ACTIVE_RUNS.pop(progress_id, None)


def read_progress(progress_id: str) -> dict[str, Any] | None:
    """解析中の ``progress_id`` のページの進捗(``progress.json``)。解析中でなければ None。

    ページの描画が終わって Docling の変換が始まるまでは ``progress.json`` が無いため、
    ``stage=preparing_pages`` を返す(取込の画面は「解析を準備しています」と出す)。
    """
    run_dir = _ACTIVE_RUNS.get(progress_id)
    if run_dir is None:
        return None
    try:
        payload = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"engine": "docling", "stage": "preparing_pages"}
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def layout_to_extraction(
    records: list[LayoutRecord],
    pages: list[PageImage],
    *,
    source_page_count: int,
    warnings: list[str] | None = None,
) -> StructuredExtraction:
    """LayoutRecord 群を共通抽出 schema(要素・ページ・表・図)へ写す。"""
    elements: list[DocumentElement] = []
    tables: list[ExtractionTable] = []
    assets: list[ExtractionAsset] = []
    # 要素の section_path(章節ナビゲーションの見出しの木の元)は、chunk の section_path と同じ規則
    # (番号の体系から深さを決める。rag_engine の layout_section_paths)で作る(#1517)。
    section_paths = layout_section_paths([record.to_dict() for record in records])
    for order, record in enumerate(records):
        kind = CATEGORY_KINDS.get(record.category, "other")
        text = str(record.text or "")
        section_path = section_paths.get(record.id, [])
        elements.append(
            DocumentElement(
                kind=kind,
                text=text,
                order=order,
                element_id=record.id,
                source_parser="docling_layout",
                page_number=record.page,
                bbox=[float(value) for value in record.bbox],
                section_path=list(section_path),
                metadata=_element_metadata(record),
            )
        )
        if record.category == "Table" and record.raw_type != "table_unassigned_text":
            tables.append(_table(record))
        if record.category == "Picture" and record.raw_type != "picture_ocr_text":
            assets.append(_asset(record))
    raw_text = "\n\n".join(e.text for e in elements if e.text.strip() and e.kind != "table")
    return StructuredExtraction(
        raw_text=raw_text,
        document_type="ドキュメント",
        confidence=0.9 if records else 0.0,
        warnings=list(warnings or []),
        elements=elements,
        pages=[
            ExtractionPage(
                page_number=page.page,
                width=float(page.width),
                height=float(page.height),
                element_ids=[r.id for r in records if r.page == page.page],
                metadata={"pdf_width": page.pdf_width, "pdf_height": page.pdf_height},
            )
            for page in pages
        ],
        tables=tables,
        assets=assets,
        parser_artifacts={
            "external_adapter": "docling",
            "adapter_export": "layout_records",
            "source_page_count": source_page_count,
            "table_count": len(tables),
            "picture_count": len(assets),
            LAYOUT_ARTIFACT: {
                "version": LAYOUT_RECORDS_VERSION,
                "pages": [_page_payload(page) for page in pages],
                "records": [_json_value(record.to_dict()) for record in records],
            },
        },
    )


def _element_metadata(record: LayoutRecord) -> dict[str, ExtractionMetadataValue]:
    return dict(layout_record_element_metadata(record))


def _table(record: LayoutRecord) -> ExtractionTable:
    """表の record を、Docling の格子のセル（``docling_table_cells``）を持つ表にする（#3429）。

    セルを格子として読めない表（位置の無い・重なるセル）は、``PARSED_TABLE_GRID_KEY`` を付けない。
    """
    cells = docling_table_cells(record.raw)
    return ExtractionTable(
        table_id=f"table-{record.id}",
        element_id=record.id,
        page_number=record.page,
        cells=cells or [],
        metadata={
            "html": record.text or "",
            **({PARSED_TABLE_GRID_KEY: PARSED_TABLE_GRID_VERSION} if cells else {}),
        },
    )


def _asset(record: LayoutRecord) -> ExtractionAsset:
    summary = layout_record_vision_summary(record)
    return ExtractionAsset(
        asset_id=f"asset-{record.id}",
        kind="figure",
        page_number=record.page,
        bbox=[float(value) for value in record.bbox],
        alt_text=(record.text or "")[:2000] or None,
        summary=summary or None,
        metadata={
            "element_id": record.id,
            "vision_status": str(record.raw.get("vision_status") or ""),
            "visual_role": str(record.raw.get("visual_role") or ""),
        },
    )


def _page_payload(page: PageImage) -> dict[str, Any]:
    payload = asdict(page)
    # 画像ファイルはサービス内の一時領域。backend は PDF から再描画する。
    payload.pop("image_path", None)
    payload.pop("image_url", None)
    return payload


def _source_suffix(file_name: str, content_type: str) -> str:
    suffix = Path(file_name or "").suffix.lower()
    if suffix in SUPPORTED_SOURCE_FILE_TYPES:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    if guessed in SUPPORTED_SOURCE_FILE_TYPES:
        return guessed
    if guessed == ".jpe":
        return ".jpg"
    raise ValueError("docling は PDF または画像のみ解析できます。")


def _json_value(value: Any) -> Any:
    """Path など JSON 化できない値を文字列へ寄せた純 JSON 値にする。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_value(item) for item in value]
    return str(value)
