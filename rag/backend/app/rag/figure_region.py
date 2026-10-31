"""図の根拠の元の画像の領域を、保存した chunk の metadata から決める（#1282・#2433）。

MCP の ``image_ref`` と画像の切り出し（``app.mcp.tools``）、画面の図の API
（``app.api.routes.figures``）、根拠（``RetrievedChunk``）の ``figure_image_available`` が同じ判定を
使う。利用者が渡した座標は使わない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

BboxUnit = Literal["absolute", "ratio", "percent"]


@dataclass(frozen=True, slots=True)
class FigureRegion:
    """図の領域（頁・bbox・座標の単位・頁の大きさ）。"""

    page: int
    bbox: tuple[float, float, float, float]
    unit: BboxUnit
    page_size: tuple[float, float]


def bbox_numbers(value: object) -> list[float] | None:
    """chunk の metadata の bbox（JSON の文字列か list）を 4 つの数にする。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
        return None
    return [float(item) for item in value]


FigureEvidenceType = Literal["figure_description", "ocr"]


def figure_evidence_type(metadata: dict[str, Any]) -> FigureEvidenceType | None:
    """図の根拠の種類（図でなければ None。#1282）。

    MCP の根拠の ``evidence_type`` と、最終の検証に渡す図の根拠の印（#2678）が同じ判定を使う。
    図の本文の出どころが分からない古い記録は、VLM の説明として扱う（原文として扱わず、元の図で
    確かめる側に倒す）。
    """
    if _text(metadata, "content_kind") != "figure":
        return None
    return "ocr" if metadata.get("figure_text_source") == "ocr" else "figure_description"


def figure_region(metadata: dict[str, Any]) -> FigureRegion | None:
    """図の根拠の元の画像の領域。

    図でない、頁・領域・頁の大きさのどれかが分からない、頁が回転している、領域が頁からはみ出す根拠は
    画像を返さない（None）。
    """
    if _text(metadata, "content_kind") != "figure":
        return None
    page = _integer(metadata, "page_start") or _integer(metadata, "page_number")
    if not page or page < 1:
        return None
    rotation = _integer(metadata, "page_rotation")
    if rotation is not None and rotation % 360 != 0:
        return None
    unit: BboxUnit | None
    engine_bbox = _engine_image_bbox(metadata, page)
    if engine_bbox is not None:
        bbox, unit = engine_bbox, "absolute"
    else:
        stored = bbox_numbers(metadata.get("bbox"))
        if stored is None:
            return None
        bbox, unit = stored, _bbox_unit(_text(metadata, "bbox_unit"))
        if _text(metadata, "bbox_coordinate_mode") == "xywh":
            bbox = [bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3]]
    if unit == "absolute":
        width = _positive(metadata.get("page_width"))
        height = _positive(metadata.get("page_height"))
        if width is None or height is None:
            return None
        page_size = (width, height)
    elif unit == "ratio":
        page_size = (1.0, 1.0)
    elif unit == "percent":
        page_size = (100.0, 100.0)
    else:
        return None
    x0, y0, x1, y1 = bbox
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return None
    if x1 > page_size[0] * 1.001 or y1 > page_size[1] * 1.001:
        return None
    return FigureRegion(page=page, bbox=(x0, y0, x1, y1), unit=unit, page_size=page_size)


def _text(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() or None if isinstance(value, str) else None


def _integer(metadata: dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _engine_image_bbox(metadata: dict[str, Any], page: int) -> list[float] | None:
    """親子階層（small-to-big）の chunk の image_evidence から、その頁の図の bbox を取る。"""
    raw = metadata.get("engine_metadata_json")
    try:
        engine = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    images = engine.get("image_evidence") if isinstance(engine, dict) else None
    for image in images if isinstance(images, list) else []:
        if not isinstance(image, dict) or _integer(image, "page") != page:
            continue
        if (bbox := bbox_numbers(image.get("bbox"))) is not None:
            return bbox
    return None


def _bbox_unit(value: str | None) -> BboxUnit | None:
    if value in (None, "absolute"):
        return "absolute"
    if value in {"ratio", "normalized"}:
        return "ratio"
    if value == "percent":
        return "percent"
    return None
