"""LayoutRecord を pdf-jsonl-viewer 互換の JSONL へ変換する。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.coordinates import image_top_left_to_pdf_bottom_left


def record_to_legacy_jsonl(record: LayoutRecord, page: PageImage | None = None) -> dict:
    """pdf-jsonl-viewer 互換フィールドを含む 1 行を作る。"""
    location = record.bbox
    if record.coord_system == "image_top_left" and page:
        location = image_top_left_to_pdf_bottom_left(
            record.bbox,
            image_width=page.width,
            image_height=page.height,
            page_width=page.pdf_width,
            page_height=page.pdf_height,
        )
    return {
        "id": record.id,
        "engine": record.engine,
        "page": record.page,
        "seq_no": record.seq_no,
        "sentence": record.text,
        "type": "layout",
        "detected_type": record.category,
        "confidence": record.confidence,
        "bbox": record.bbox,
        "coord_system": record.coord_system,
        "text_location": {"location": [location]},
        "raw_type": record.raw_type,
    }


def dumps_jsonl(records: Iterable[LayoutRecord], pages: Iterable[PageImage]) -> str:
    """LayoutRecord と PageImage を JSONL 文字列へ変換します。"""
    by_page = {page.page: page for page in pages}
    lines = [json.dumps(record_to_legacy_jsonl(record, by_page.get(record.page)), ensure_ascii=False) for record in records]
    return "\n".join(lines) + ("\n" if lines else "")


def write_jsonl(path: str | Path, records: Iterable[LayoutRecord], pages: Iterable[PageImage]) -> None:
    """LayoutRecord と PageImage を JSONL ファイルへ書き出します。"""
    Path(path).write_text(dumps_jsonl(records, pages), encoding="utf-8")
