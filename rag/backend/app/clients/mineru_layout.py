"""MinerU の Middle JSON 2.0 から、親子階層（small-to-big）が読む LayoutRecord を作る（#1334）。

親子階層の分割（rag_engine の ``build_small_to_big_chunks``）は Docling の LayoutRecord
（``parser_artifacts["layout_records"]``）を入力にする。分割の処理は変えず、MinerU の block を
同じ形の record に写す。容れ物の形（``version`` / ``pages`` / ``records``）と座標系は Docling の
サービス（``services/parsers/docling/app/extraction.py``）と同じにする。

- 座標は Docling と同じ ``image_top_left`` のページ画像の px。PDF は MinerU の
  ``extensions.docvortex_layout.pages`` の寸法（pt。bbox と同じ向き）を、Docling サービスの既定と
  同じ 300 dpi の px にする。画像ファイルは元の画像の px（EXIF の向きを直した寸法。Vision の
  ``prepare_source_for_analysis`` と同じ）。Middle JSON の bbox（0〜1・左上原点）にその寸法を
  掛ける。
  寸法の分からない頁・bbox の無い block（Office の文書など）がある文書は record を作らない
  （親子階層は従来どおり構造認識へ縮退する）。
- block の種類は ``MINERU_LAYOUT_CATEGORIES`` の 1 か所で Docling の category へ写す。表に無い種類は
  ``Text`` にし、``raw_type`` に元の種類を残す。
- record の ``id`` は要素の ``element_id``（``mineru-p{頁}-b{block の番号}``）と同じにし、``raw`` に
  MinerU の元の block の番号（``mineru_block_index``）と頁（``mineru_page_idx``）を残す（#1330 の
  要素の定位子）。図・表の caption・footnote は親の id に接尾辞を付けた別の record にする。
- 頁のヘッダー・フッター・頁番号は、Docling と同じく ``Page-header`` / ``Page-footer`` の
  record にする。要素（本文）には入れない（``mineru_middle_json_blocks`` のまま）。分割はこれを
  頁の定型（柱・著作権・頁番号）として本文から外し、頁番号を論理頁に使う。record に無いと、
  柱の繰り返しを見出しと区別できない。
- 頁・段・図をまたいで続く本文・リスト・表（``continues_prev``）は、Docling と同じく頁ごとの別の
  record のままにし、印を ``raw.continues_prev`` に残す。文書全体の整理（``pdf_segment_merge``
  の ``consolidate_document_extraction``。#2266）が続きの元の record の ID
  （``raw.continues_record_id``）に写し、親子階層の分割が同じ子 chunk に入れる。表の
  ``cell_merge``（MinerU のサーバー側の LLM の後処理が付けるセルの続き。使っていないため通常は
  無い）は ``raw.cell_merge`` に残すだけにする。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Mapping
from typing import Any

from app.clients.external_parser import (
    _list,
    _mapping,
    _mineru_leaf_texts,
    _mineru_plain_text,
)
from app.schemas.document import SourceProfile

logger = logging.getLogger(__name__)

LAYOUT_RECORDS_VERSION = 1
MINERU_ENGINE = "mineru"
# PDF のページ画像の解像度。Docling サービスの既定（RAG_ENGINE_RENDER_DPI=300）と、Vision が
# 描き直す解像度（``app.rag.vision.VISION_RENDER_DPI``）にそろえる。
MINERU_LAYOUT_RENDER_DPI = 300
_DOCVORTEX_LAYOUT = "docvortex_layout"
_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".jp2"}
)

# MinerU の block の種類 → (Docling の category, raw_type)。raw_type は Docling の label の名前。
# 対応表に無い種類は ("Text", 元の種類) にする。
MINERU_LAYOUT_CATEGORIES: dict[str, tuple[str, str]] = {
    "doc_title": ("Title", "title"),
    "paragraph_title": ("Section-header", "section_header"),
    "text": ("Text", "text"),
    "ref_text": ("Text", "text"),
    "aside_text": ("Text", "text"),
    "list": ("List-item", "list_item"),
    "index": ("List-item", "list_item"),
    "table": ("Table", "table"),
    "image": ("Picture", "picture"),
    "chart": ("Picture", "picture"),
    "equation": ("Formula", "formula"),
    # コードは本文として読む（code の印は raw_type と raw の ``code`` に残す）。
    "code": ("Text", "code"),
    "header": ("Page-header", "page_header"),
    "footer": ("Page-footer", "page_footer"),
    "page_number": ("Page-footer", "page_footer"),
    "page_footnote": ("Footnote", "footnote"),
    "image_caption": ("Caption", "caption"),
    "table_caption": ("Caption", "caption"),
    "chart_caption": ("Caption", "caption"),
    "image_footnote": ("Footnote", "footnote"),
    "table_footnote": ("Footnote", "footnote"),
    "chart_footnote": ("Footnote", "footnote"),
}
_VISUAL_PARENT_TYPES = frozenset({"image", "table", "chart"})


def mineru_layout_category(block_type: str) -> tuple[str, str]:
    """MinerU の block の種類の (category, raw_type)。対応表に無ければ Text と元の種類。"""
    return MINERU_LAYOUT_CATEGORIES.get(block_type, ("Text", block_type or "text"))


def mineru_layout_records(
    middle_json: object,
    *,
    source_bytes: bytes,
    content_type: str,
    source_profile: SourceProfile | None = None,
) -> dict[str, Any] | None:
    """Middle JSON 2.0 を ``parser_artifacts["layout_records"]`` の容れ物にする。作れなければ None。

    Middle JSON の形は ``mineru_middle_json_blocks`` が先に確かめている前提で、ここでは寸法と
    bbox がそろうかだけを見る。
    """
    document = _mapping(middle_json)
    pages = [_mapping(page) for page in _list(document.get("pages"))]
    geometry = _page_geometry(
        document,
        [page.get("page_idx") for page in pages],
        source_bytes=source_bytes,
        content_type=content_type,
        source_profile=source_profile,
    )
    if geometry is None:
        return None
    page_payloads: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for page in pages:
        page_idx = page.get("page_idx")
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx not in geometry:
            return None
        page_payload = geometry[page_idx]
        page_payloads.append(page_payload)
        builder = _PageRecords(page_idx, page_payload)
        for block in _list(page.get("blocks")):
            if not builder.add_block(_mapping(block)):
                logger.info(
                    "mineru_layout_bbox_missing",
                    extra={"page_idx": page_idx, "block_type": _mapping(block).get("type")},
                )
                return None
        records.extend(builder.records)
    if not records:
        return None
    return {"version": LAYOUT_RECORDS_VERSION, "pages": page_payloads, "records": records}


class _PageRecords:
    """1 頁の block を読み順のまま record にする（``seq_no`` は頁の中で 1 から）。"""

    def __init__(self, page_idx: int, page: Mapping[str, Any]) -> None:
        self.page_idx = page_idx
        self.page_number = page_idx + 1
        self.width = float(page["width"])
        self.height = float(page["height"])
        self.records: list[dict[str, Any]] = []

    def add_block(self, block: Mapping[str, object]) -> bool:
        """block を record にする。

        出す record に bbox が無ければ False を返す（文書の record を作らない）。
        """
        block_type = str(block.get("type") or "")
        if not block_type:
            return True
        index = block.get("index")
        block_index = index if isinstance(index, int) and not isinstance(index, bool) else None
        record_id = (
            f"mineru-p{self.page_number}-b{block_index}"
            if block_index is not None
            else f"mineru-p{self.page_number}-s{len(self.records) + 1}"
        )
        bbox = _normalized_bbox(block.get("bbox"))
        raw: dict[str, Any] = {
            "mineru_type": block_type,
            "mineru_page_idx": self.page_idx,
            **({"mineru_block_index": block_index} if block_index is not None else {}),
        }
        # 見出しの深さ（doc_title=1、paragraph_title=2〜6）は参考に残す。分割の見出しの深さは
        # Docling と同じく本文の番号の形から決める（分割の深さの尺度は MinerU の level と違う）。
        level = block.get("level")
        if isinstance(level, int) and not isinstance(level, bool):
            raw["mineru_level"] = level
        if block.get("continues_prev") is True:
            raw["continues_prev"] = True
        cell_merge = block.get("cell_merge")
        if isinstance(cell_merge, list) and all(
            type(flag) is int and flag in (0, 1) for flag in cell_merge
        ):
            raw["cell_merge"] = list(cell_merge)
        for key in ("sub_type", "guess_lang"):
            value = block.get(key)
            if isinstance(value, str) and value:
                raw[key] = value
        content = block.get("content")
        if block_type in _VISUAL_PARENT_TYPES:
            return self._add_visual(block_type, content, record_id, bbox, raw)
        text = _block_text(block_type, content)
        if not text:
            return True
        if bbox is None:
            return False
        category, raw_type = mineru_layout_category(block_type)
        if block_type == "code":
            raw["code"] = True
        if block_type == "equation":
            raw["text_format"] = "latex"
        self._append(record_id, bbox, category, raw_type, text, raw)
        return True

    def _add_visual(
        self,
        block_type: str,
        content: object,
        record_id: str,
        bbox: list[float] | None,
        raw: dict[str, Any],
    ) -> bool:
        """図・表・グラフの body と caption・footnote を、読み順の別々の record にする。"""
        if bbox is None:
            return False
        category, raw_type = mineru_layout_category(block_type)
        counts: dict[str, int] = {}
        body_added = False
        for child in _list(content):
            child_info = _mapping(child)
            child_type = str(child_info.get("type") or "")
            child_content = child_info.get("content")
            child_bbox = _normalized_bbox(child_info.get("bbox")) or bbox
            if child_type.endswith("_body"):
                if body_added:
                    continue
                body_added = True
                body = _body_text(child_content)
                self._add_visual_body(category, raw_type, record_id, bbox, body, raw)
                continue
            text = _mineru_plain_text(child_content)
            if not text:
                continue
            child_category, child_raw_type = mineru_layout_category(child_type)
            suffix = "caption" if child_category == "Caption" else "footnote"
            counts[suffix] = counts.get(suffix, 0) + 1
            self._append(
                f"{record_id}-{suffix}{counts[suffix]}",
                child_bbox,
                child_category,
                child_raw_type,
                text,
                {**raw, "mineru_type": child_type, "mineru_parent_record_id": record_id},
            )
        if not body_added:
            self._add_visual_body(category, raw_type, record_id, bbox, "", raw)
        return True

    def _add_visual_body(
        self,
        category: str,
        raw_type: str,
        record_id: str,
        bbox: list[float],
        body: str,
        raw: dict[str, Any],
    ) -> None:
        if category == "Table":
            # 表の本文は Docling と同じく HTML（分割が表の構造と行グループを読む）。
            self._append(record_id, bbox, category, raw_type, body, raw)
            return
        # 図は本文を空にして Vision の対象にし、MinerU の図の中の文字（image_body / chart_body）は
        # Docling と同じく同じ bbox の ``picture_ocr_text`` record にする（分割が 1 つの図に
        # 束ねる）。
        self._append(record_id, bbox, category, raw_type, "", raw)
        if body:
            from rag_engine.parsing.picture_text import format_docling_picture_ocr_text

            self._append(
                f"{record_id}-ocr",
                bbox,
                "Picture",
                "picture_ocr_text",
                format_docling_picture_ocr_text(body),
                {**raw, "aggregation": "mineru_visual_body", "source_picture_ref": record_id},
            )

    def _append(
        self,
        record_id: str,
        bbox: list[float],
        category: str,
        raw_type: str,
        text: str,
        raw: dict[str, Any],
    ) -> None:
        self.records.append(
            {
                "id": record_id,
                "engine": MINERU_ENGINE,
                "page": self.page_number,
                "seq_no": len(self.records) + 1,
                "bbox": [
                    round(bbox[0] * self.width, 2),
                    round(bbox[1] * self.height, 2),
                    round(bbox[2] * self.width, 2),
                    round(bbox[3] * self.height, 2),
                ],
                "coord_system": "image_top_left",
                "page_width": self.width,
                "page_height": self.height,
                "category": category,
                "text": text,
                "confidence": None,
                "raw_type": raw_type,
                "raw": dict(raw),
            }
        )


def _block_text(block_type: str, content: object) -> str:
    if block_type == "equation":
        return str(content or "").strip() if isinstance(content, str) else ""
    if block_type in {"list", "index"}:
        return "\n".join(_mineru_leaf_texts(content)).strip()
    if block_type == "code":
        parts: list[str] = []
        for child in _list(content):
            child_info = _mapping(child)
            child_content = child_info.get("content")
            text = (
                _body_text(child_content)
                if str(child_info.get("type") or "").endswith("_body")
                else _mineru_plain_text(child_content)
            )
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    # InlineSpan の列。子の block を持つ未知の容れ物（対応表に無い種類）は葉の本文を読み順に並べる。
    return _mineru_plain_text(content) or "\n".join(_mineru_leaf_texts(content)).strip()


def _body_text(content: object) -> str:
    return content.strip() if isinstance(content, str) else _mineru_plain_text(content)


def _normalized_bbox(value: object) -> list[float] | None:
    """Middle JSON の 0〜1 の bbox。値が不正・面積が無ければ None。"""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    if not all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
        return None
    x0, y0, x1, y1 = (min(max(float(item), 0.0), 1.0) for item in value)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _page_geometry(
    document: Mapping[str, object],
    page_indices: list[object],
    *,
    source_bytes: bytes,
    content_type: str,
    source_profile: SourceProfile | None,
) -> dict[int, dict[str, Any]] | None:
    """頁ごとのページ画像の寸法（Docling の ``layout_records.pages`` と同じ項目）。"""
    mime = (content_type or "").split(";", 1)[0].strip().casefold()
    extension = (source_profile.extension or "").casefold() if source_profile else ""
    if mime == "application/pdf" or extension == ".pdf":
        return _pdf_geometry(document)
    if mime.startswith("image/") or extension in _IMAGE_EXTENSIONS:
        size = _image_size(source_bytes)
        if size is None or any(index != 0 for index in page_indices):
            return None
        width, height = size
        return {
            0: {
                "page": 1,
                "width": width,
                "height": height,
                "pdf_width": float(width),
                "pdf_height": float(height),
            }
        }
    # Office の文書などは頁の寸法が無い（Middle JSON の bbox も任意）。
    return None


def _pdf_geometry(document: Mapping[str, object]) -> dict[int, dict[str, Any]] | None:
    extensions = _mapping(document.get("extensions"))
    layout = _mapping(extensions.get(_DOCVORTEX_LAYOUT))
    geometry: dict[int, dict[str, Any]] = {}
    scale = MINERU_LAYOUT_RENDER_DPI / 72.0
    for page in _list(layout.get("pages")):
        info = _mapping(page)
        page_idx = info.get("page_idx")
        width_pt = _positive_number(info.get("width_pt"))
        height_pt = _positive_number(info.get("height_pt"))
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx < 0:
            continue
        if width_pt is None or height_pt is None:
            continue
        geometry[page_idx] = {
            "page": page_idx + 1,
            "width": round(width_pt * scale),
            "height": round(height_pt * scale),
            "pdf_width": width_pt,
            "pdf_height": height_pt,
        }
    if not geometry:
        logger.info("mineru_layout_geometry_missing")
        return None
    return geometry


def _image_size(data: bytes) -> tuple[int, int] | None:
    """画像の寸法（px。EXIF の向きを直した後）。読めなければ None。"""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as image:
            transposed = ImageOps.exif_transpose(image)
            width, height = transposed.size
    except Exception:
        return None
    return (int(width), int(height)) if width > 0 and height > 0 else None


def _positive_number(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    number = float(value)
    return number if number > 0 else None
