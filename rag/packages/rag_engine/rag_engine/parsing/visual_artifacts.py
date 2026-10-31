"""表や図などの意味を持つ visual block を再利用可能な crop として保存する。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.decorative_pictures import classify_picture_record, mark_picture_record_role

VISUAL_CROP_PADDING = 8
VISUAL_CATEGORIES = {"Picture", "Table"}
FORM_VISUAL_CATEGORIES = {"Form"}
FORM_VISUAL_RAW_TYPES = {
    "form",
    "form_region",
    "form-region",
    "complex_layout",
    "complex-layout",
    "key_value_region",
}


@dataclass(frozen=True)
class VisualArtifactStats:
    """Visual crop の対象数、保存数、失敗数を保持します。"""

    targets: int
    saved: int
    failed: int


def persist_semantic_visual_crops(
    records: Sequence[LayoutRecord],
    pages: Sequence[PageImage],
    *,
    run_dir: Path,
) -> VisualArtifactStats:
    """Picture/Table/Form の検索根拠と生成済み装飾判定の確認画像を保存します。

    Args:
        records: 同じ解析単位に属する LayoutRecord。装飾判定の近傍 context にも使用します。
        pages: crop 元となる page image とページ番号の対応。
        run_dir: crop を保存する現在の analysis run directory。

    Returns:
        crop の対象数、保存成功数、失敗数。個別失敗は record.raw の crop_error に保持します。

    Side Effects:
        crop PNG を run directory に作成し、対象 record.raw の visual asset metadata を更新します。
    """

    targets = _visual_targets(records)
    if not targets:
        return VisualArtifactStats(0, 0, 0)

    from PIL import Image

    page_lookup = {page.page: page for page in pages}
    page_images: dict[int, Image.Image] = {}
    saved = 0
    failed = 0
    try:
        for record in targets:
            record.raw.pop("crop_path", None)
            record.raw.pop("crop_error", None)
            record.raw.pop("visual_asset_kind", None)
            record.raw.pop("visual_artifact_type", None)
            record.raw.pop("visual_asset_bbox", None)
            try:
                page = page_lookup.get(record.page)
                if page is None:
                    raise RuntimeError(f"{record.page} ページ目の画像がありません。")
                page_picture = page_images.get(record.page)
                if page_picture is None:
                    with Image.open(page.image_path) as opened:
                        page_picture = opened.convert("RGB")
                    page_images[record.page] = page_picture
                crop_dir = run_dir / _safe_path_component(record.engine) / "visuals"
                crop_dir.mkdir(parents=True, exist_ok=True)
                crop_path = _save_record_crop(page_picture, record, crop_dir)
                record.raw["crop_path"] = crop_path.relative_to(run_dir).as_posix()
                record.raw["visual_asset_kind"] = "crop"
                record.raw["visual_artifact_type"] = _visual_artifact_type(record)
                record.raw["visual_asset_bbox"] = [float(value) for value in record.bbox]
                if record.category == "Table" and isinstance(record.raw.get("vision_description"), dict):
                    from rag_engine.parsing.table_visual_rows import enrich_table_visual_rows

                    enrich_table_visual_rows(record, page, record.raw["vision_description"], run_dir)
                saved += 1
            except Exception as exc:
                record.raw["crop_error"] = f"{type(exc).__name__}: {exc}"[:500]
                failed += 1
    finally:
        for page_picture in page_images.values():
            page_picture.close()
    return VisualArtifactStats(len(targets), saved, failed)


def _visual_targets(records: Sequence[LayoutRecord]) -> list[LayoutRecord]:
    targets: list[LayoutRecord] = []
    for record in records:
        if record.category not in VISUAL_CATEGORIES and not _is_form_visual_record(record):
            continue
        if record.category == "Picture":
            if record.raw_type == "picture_ocr_text":
                continue
            visual_role = classify_picture_record(record, records)
            mark_picture_record_role(record, visual_role)
            if visual_role.skip_vlm and not record.raw.get("vision_description"):
                continue
            if visual_role.reason == "meaningful_text_in_visual" and not record.text.strip():
                from rag_engine.parsing.picture_descriptions import vision_description_text

                record.text = vision_description_text(record.raw.get("vision_description") or {})
        targets.append(record)
    return sorted(targets, key=lambda item: (item.page, item.seq_no, item.bbox[1], item.bbox[0]))


def _is_form_visual_record(record: LayoutRecord) -> bool:
    """個別 field ではなく、Form 全体を表す bbox record かを判定します。"""
    return record.category in FORM_VISUAL_CATEGORIES or str(record.raw_type or "").strip().lower() in FORM_VISUAL_RAW_TYPES


def _visual_artifact_type(record: LayoutRecord) -> str:
    """Parser/Vision が識別した最も具体的な visual type を返します。"""
    if record.category == "Table":
        return "table"
    if _is_form_visual_record(record):
        return "form"
    for key in ("visual_kind", "picture_kind"):
        value = str(record.raw.get(key) or "").strip().lower()
        if value:
            return value
    return str(record.raw_type or "picture")


def _save_record_crop(page_picture, record: LayoutRecord, crop_dir: Path) -> Path:
    left, top, right, bottom = _crop_box(record, page_picture.width, page_picture.height)
    crop_path = crop_dir / f"{_safe_path_component(record.id)}.png"
    page_picture.crop((left, top, right, bottom)).save(crop_path)
    return crop_path


def _crop_box(record: LayoutRecord, page_width: int, page_height: int) -> tuple[int, int, int, int]:
    if not isinstance(record.bbox, list) or len(record.bbox) != 4:
        raise RuntimeError(f"Visual bbox が不正です: {record.bbox}")
    try:
        x1, y1, x2, y2 = (float(value) for value in record.bbox)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Visual bbox が不正です: {record.bbox}") from exc
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        raise RuntimeError(f"Visual bbox が不正です: {record.bbox}")
    left = max(0, math.floor(min(x1, x2)) - VISUAL_CROP_PADDING)
    top = max(0, math.floor(min(y1, y2)) - VISUAL_CROP_PADDING)
    right = min(page_width, math.ceil(max(x1, x2)) + VISUAL_CROP_PADDING)
    bottom = min(page_height, math.ceil(max(y1, y2)) + VISUAL_CROP_PADDING)
    if right <= left or bottom <= top:
        raise RuntimeError(f"Visual bbox が空です: {record.bbox}")
    return left, top, right, bottom


def _safe_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-._") or "visual"
