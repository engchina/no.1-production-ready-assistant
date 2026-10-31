"""LayoutRecord から共通抽出 schema の要素 metadata を作る(Docling サービスと backend で共用)。

Docling サービスは解析結果を、backend は解析後の Vision(#497)の結果を、同じ規則で要素の
metadata へ写す。プレビューと診断に使う raw のスカラー値と、Vision の検索用 text を載せる。
"""

from __future__ import annotations

from typing import Any

from rag_engine.models.layout import LayoutRecord

# element metadata へ写す raw のスカラー値(プレビューと診断に使う)。
RAW_SCALAR_KEYS = (
    "vision_status",
    "vision_error",
    "vision_model",
    "vision_skipped",
    "visual_role",
    "visual_role_reason",
    "visual_kind",
    "picture_kind",
    "rag_excluded",
    "table_vision_text",
    "table_image_detection_status",
)


def layout_record_element_metadata(record: LayoutRecord) -> dict[str, str | int | float | bool]:
    """LayoutRecord の座標・分類と Vision の結果を、要素の metadata(JSON スカラーだけ)にする。"""
    metadata: dict[str, str | int | float | bool] = {
        "category": record.category,
        "raw_type": record.raw_type,
        "page_width": float(record.page_width),
        "page_height": float(record.page_height),
        "bbox_unit": "absolute",
        "bbox_mode": "xyxy",
        "seq_no": record.seq_no,
    }
    metadata.update(layout_record_vision_metadata(record))
    return metadata


def layout_record_vision_metadata(record: LayoutRecord) -> dict[str, str | int | float | bool]:
    """raw のうち Vision・画像の分類に関わるスカラー値と、Vision の検索用 text を返す。"""
    metadata: dict[str, str | int | float | bool] = {}
    for key in RAW_SCALAR_KEYS:
        value: Any = record.raw.get(key)
        if isinstance(value, str | int | float | bool) and value != "":
            metadata[key] = value
    description = record.raw.get("vision_description")
    if isinstance(description, dict) and description.get("retrieval_text"):
        metadata["vision_retrieval_text"] = str(description["retrieval_text"])
    return metadata


def layout_record_vision_summary(record: LayoutRecord) -> str:
    """図の asset の要約(Vision の検索用 text)。読み取っていなければ空文字。"""
    description = record.raw.get("vision_description")
    if isinstance(description, dict):
        return str(description.get("retrieval_text") or "")
    return ""
