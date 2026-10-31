"""解析レコード（Docling 等の layout 要素）の本文・bbox・視覚役割・表構造 cache など、レコード単位の helper。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from rag_parser_core.language.markers import marker_patterns

from rag_engine.chunking.constants import (
    ANNOTATION_CATEGORIES,
    ATOMIC_CATEGORIES,
    FORM_FIELD_RAW_KEYS,
    FORM_LIKE_CATEGORIES,
    FORM_LIKE_RAW_TYPES,
    SECTION_CATEGORIES,
    SOURCE_TABLE_CROP_RELATIONSHIP,
    _bool_value,
    _int_value,
)
from rag_engine.parsing.content_labels import content_labels
from rag_engine.parsing.decorative_pictures import DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE, visual_role_from_ref
from rag_engine.parsing.picture_text import format_docling_picture_ocr_text, is_picture_ocr_text
from rag_engine.parsing.table_structure import parse_html_table_structure


def _record_can_supply_image_evidence(record: dict[str, Any], visual_role: Any | None = None) -> bool:
    if str(record.get("category") or "") != "Picture":
        return False
    if str(record.get("raw_type") or "") == "picture_ocr_text":
        return False
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    if _bool_value(record.get("rag_excluded") or raw.get("rag_excluded"), default=False):
        return False
    role = str(getattr(visual_role, "role", "") or _record_visual_role(record))
    return role not in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}


def _record_visual_role(record: dict[str, Any]) -> str:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    return str(raw.get("visual_role") or "").strip().lower()


def _is_attached_atomic_annotation(record: dict[str, Any]) -> bool:
    if str(record.get("category") or "") not in ANNOTATION_CATEGORIES:
        return False
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    relationship = raw.get("layout_relationship") if isinstance(raw.get("layout_relationship"), dict) else {}
    target_id = str(relationship.get("target_record_id") or "")
    target_category = str(relationship.get("target_category") or "")
    return bool(target_id and target_category in ATOMIC_CATEGORIES)


@lru_cache(maxsize=64)
def _table_structure(html: str) -> dict[str, Any]:
    """表 HTML の構造を 1 回だけ解析して共有する (#804)。

    同じ表を _table_contained_visual_ids（2 回）・_table_record_with_metadata・_contained_table_visual_evidence が
    別々に解析していた。parse_html_table_structure は入力文字列だけで決まる純関数なので、文字列を key に
    共有する。返り値の dict は呼び出し側で共有されるため書き換えない（現状、書き換える箇所はない）。
    """
    return parse_html_table_structure(html)


def _bbox_tuple(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    left = min(x1, x2)
    top = min(y1, y2)
    right = max(x1, x2)
    bottom = max(y1, y2)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _bbox_intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return left[0] < right[2] and left[2] > right[0] and left[1] < right[3] and left[3] > right[1]


def _bbox_gap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    horizontal_gap = max(0.0, max(left[0], right[0]) - min(left[2], right[2]))
    vertical_gap = max(0.0, max(left[1], right[1]) - min(left[3], right[3]))
    return math.hypot(horizontal_gap, vertical_gap)


def _bbox_horizontal_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _relative_position(annotation: dict[str, Any], target: dict[str, Any]) -> str:
    annotation_bbox = _bbox_tuple(annotation.get("bbox"))
    target_bbox = _bbox_tuple(target.get("bbox"))
    if annotation_bbox is None or target_bbox is None:
        annotation_seq = _int_value(annotation.get("seq_no")) or 0
        target_seq = _int_value(target.get("seq_no")) or 0
        return "after" if annotation_seq > target_seq else "before"
    if annotation_bbox[3] <= target_bbox[1]:
        return "above"
    if annotation_bbox[1] >= target_bbox[3]:
        return "below"
    if annotation_bbox[2] <= target_bbox[0]:
        return "left"
    if annotation_bbox[0] >= target_bbox[2]:
        return "right"
    return "overlap"


def _list_level(record: dict[str, Any]) -> int:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    for key in ("level", "list_level", "indent_level"):
        value = _int_value(raw.get(key))
        if value and value > 0:
            return value
    return 1


def _list_marker(record: dict[str, Any]) -> str:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    marker = _first_text_value(raw, ("marker", "list_marker", "bullet", "number"))
    if marker:
        return marker
    markers = marker_patterns()
    match = re.match(
        rf"\A\s*((?:\d+|[A-Za-z]){markers.number_closer}|{markers.circled}|{markers.bullet})",
        _record_text(record),
    )
    return match.group(1) if match else ""


def _formula_payload(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    existing = raw.get("formula")
    if isinstance(existing, dict):
        return dict(existing)
    expression = _first_text_value(raw, ("latex", "formula_latex", "math", "expression"))
    if not expression and isinstance(existing, str):
        expression = existing.strip()
    if not expression:
        expression = _record_text(record)
    if not expression:
        return {}
    return {
        "expression": expression,
        "format": "latex" if _first_text_value(raw, ("latex", "formula_latex")) else "text",
        "display": _bool_value(raw.get("display"), default=True),
    }


def _form_field_payload(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    existing = raw.get("form_field")
    if isinstance(existing, dict):
        return dict(existing)
    if not _is_form_like_record(record):
        return {}
    explicit_role = _has_explicit_form_role(record)
    field_key_candidates = ("key", "field_name", "label", "name") if explicit_role else ("key", "field_name")
    field_key = _first_text_value(raw, field_key_candidates)
    field_value = _first_text_value(raw, ("value", "field_value", "text"))
    selection_status = _first_text_value(raw, ("selection_status", "status"))
    selected = _optional_bool_value(raw.get("selected"))
    if selected is None:
        selected = _optional_bool_value(raw.get("checked"))
    if selected is None and selection_status:
        selected = _optional_bool_value(selection_status)
    payload: dict[str, Any] = {
        "raw_type": str(record.get("raw_type") or ""),
        "category": str(record.get("category") or ""),
    }
    if field_key:
        payload["key"] = field_key
    if field_value:
        payload["value"] = field_value
    if selection_status:
        payload["selection_status"] = selection_status
    if selected is not None:
        payload["selected"] = selected
    return payload


def _is_form_like_record(record: dict[str, Any]) -> bool:
    if _has_explicit_form_role(record):
        return True
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    if isinstance(raw.get("form_field"), dict):
        return True
    return any(_raw_has_value(raw, key) for key in FORM_FIELD_RAW_KEYS)


def _has_explicit_form_role(record: dict[str, Any]) -> bool:
    category = str(record.get("category") or "")
    raw_type = str(record.get("raw_type") or "").strip().lower()
    return category in FORM_LIKE_CATEGORIES or raw_type in FORM_LIKE_RAW_TYPES


def _form_record_text(record: dict[str, Any]) -> str:
    field = _form_field_payload(record)
    if not field:
        return ""
    key = str(field.get("key") or "").strip()
    value = str(field.get("value") or "").strip()
    selected = field.get("selected")
    status = ""
    if isinstance(selected, bool):
        status = "選択済み" if selected else "未選択"
    elif field.get("selection_status"):
        status = str(field.get("selection_status") or "").strip()
    pieces = []
    if key and value:
        pieces.append(f"{key}: {value}")
    elif key:
        pieces.append(key)
    elif value:
        pieces.append(value)
    if status:
        pieces.append(status)
    return "フォーム抽出: " + " / ".join(pieces) if pieces else ""


def _first_text_value(raw: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _raw_has_value(raw: dict[str, Any], key: str) -> bool:
    if key not in raw:
        return False
    value = raw.get(key)
    if value is None:
        return False
    return bool(str(value).strip())


def _optional_bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    markers = marker_patterns()
    # 英語の値は解析エンジンの出力の形。日本語の選択の値は言語資源(#2181)。
    if text in {"1", "checked", "on", "selected", "select", "true", "yes", "y", *markers.checked_values}:
        return True
    unchecked = {"0", "false", "no", "n", "off", "unchecked", "unselected", "not_selected"}
    if text in {*unchecked, *markers.unchecked_values}:
        return False
    return None


def _visual_group_key(record: dict[str, Any]) -> tuple[Any, ...] | None:
    """同じ engine・page・bbox の Picture と `picture_ocr_text` を 1 つの図として束ねる key を返します。

    持ち主の図が分かった OCR（``raw.picture_bbox``。``link_picture_ocr_records``）は図の bbox で束ね、
    丸めの境目で図と OCR を別の図にしない（#3455）。
    """
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    bbox = raw.get("picture_bbox") if is_picture_ocr_text(record) and raw.get("picture_bbox") else record.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        rounded_bbox = tuple(round(float(value), 1) for value in bbox)
    except (TypeError, ValueError):
        return None
    return (str(record.get("engine") or ""), _int_value(record.get("page")), rounded_bbox)


def _picture_vision_described(record: dict[str, Any]) -> bool:
    """Vision 説明が本文になっている Picture か（失敗・未実施・説明が空なら False）。"""
    if str(record.get("category") or "") != "Picture" or str(record.get("raw_type") or "") == "picture_ocr_text":
        return False
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    return isinstance(raw.get("vision_description"), dict) and not raw.get("vision_error") and bool(_record_text(record))


def _join_records_text(records: Sequence[dict[str, Any]], *, for_retrieval: bool = False) -> str:
    """record の本文を改行で連結します。

    `for_retrieval=True` は検索文（retrieval_text）の土台用で、Vision 説明が本文になっている図の
    `picture_ocr_text` を含めません。Vision は OCR を読み取り補助として受け取り、意味のある語を
    項目・操作・検索語の field に取り込んでいるため、OCR 原文は検索文では重複語と UI ラベルの羅列・
    誤読のノイズになります (#900)。Vision 失敗・未実施の図は OCR が唯一の文字情報なので残します。
    回答・表示用の text は変えません。
    """
    described_groups = {_visual_group_key(record) for record in records if for_retrieval and _picture_vision_described(record)}
    pieces = []
    for record in records:
        if str(record.get("raw_type") or "") == "picture_ocr_text" and _visual_group_key(record) in described_groups:
            continue
        text = _record_text_for_chunk(record, for_retrieval=for_retrieval)
        if text:
            pieces.append(text)
    return "\n".join(pieces).strip()


def _record_text_for_chunk(record: dict[str, Any], *, for_retrieval: bool = False) -> str:
    """chunk 本文に載せる record の text。`for_retrieval` は表内画像の OCR を検索文から外します (#900)。"""
    text = _record_text(record)
    if str(record.get("raw_type") or "") == "picture_ocr_text":
        return format_docling_picture_ocr_text(text)
    if str(record.get("category") or "") == "Table":
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        supplement = str(raw.get("table_vision_text") or "").strip()
        table_visual_text = "\n".join(
            filter(
                None,
                [
                    f"{content_labels().text('table_picture_supplement')}:\n{supplement}" if supplement else "",
                    _table_visual_evidence_text(record, for_retrieval=for_retrieval),
                ],
            )
        )
        if text and table_visual_text:
            return f"{text}\n{table_visual_text}"
        if table_visual_text:
            return table_visual_text
    if not text:
        return _form_record_text(record)
    return text


def _image_fallback_text(records: Sequence[dict[str, Any]], source_file_name: str) -> str:
    lines: list[str] = []
    for record in records:
        is_form = _is_form_like_record(record)
        if not _record_can_supply_image_evidence(record) and not is_form:
            continue
        record_id = str(record.get("id") or record.get("record_id") or "").strip()
        page = _int_value(record.get("page")) or 0
        seq_no = _int_value(record.get("seq_no")) or 0
        bbox = record.get("bbox") if isinstance(record.get("bbox"), list) else []
        pieces = [
            "Form image evidence" if is_form else "Image evidence",
            f"id={record_id or f'p{page}-s{seq_no}'}",
            f"file={source_file_name}",
            f"page={page}",
        ]
        if bbox:
            pieces.append(f"bbox={bbox}")
        lines.append(" | ".join(pieces))
    return "\n".join(lines).strip()


def _table_visual_evidence_text(record: dict[str, Any], *, for_retrieval: bool = False) -> str:
    """表内 Picture の説明と OCR を `表内画像:` の箇条書きにします。

    `for_retrieval=True` では、Vision の画像説明があり `vision_error` のない項目の OCR 行を
    載せません（図の child の `picture_ocr_text` と同じ理由。#900）。
    """
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    visual_evidence = raw.get("table_visual_evidence")
    visuals = _filter_image_evidence(visual_evidence if isinstance(visual_evidence, list) else [])
    full_texts = raw.get("table_visual_texts") if isinstance(raw.get("table_visual_texts"), dict) else {}
    labels = content_labels()
    lines: list[str] = []
    for visual in visuals:
        own_crop = str(visual.get("relationship") or "") == SOURCE_TABLE_CROP_RELATIONSHIP
        # 表自身の切り出し画像の text_preview は表 HTML の先頭で、本文の複製にしかならない。
        # Vision 説明や OCR 本文があるときだけ載せる。
        description = full_texts.get(str(visual.get("image_id") or "")) or ("" if own_crop else visual.get("text_preview"))
        text_preview = re.sub(r"\s+", " ", str(description or "")).strip()
        ocr_text = format_docling_picture_ocr_text(str(visual.get("ocr_text") or "").strip())
        if for_retrieval and text_preview and not str(visual.get("vision_error") or "").strip():
            ocr_text = ""
        if not text_preview and not ocr_text:
            continue
        label = _table_visual_evidence_label(visual)
        lines.append(f"- {label or labels.text('table_pictures')}")
        if text_preview:
            lines.append(f"  {labels.text('picture_description')}: {text_preview}")
        if ocr_text:
            lines.extend(f"  {line}" for line in ocr_text.splitlines() if line.strip())
    return f"{labels.text('table_pictures')}:\n" + "\n".join(lines) if lines else ""


def _table_visual_evidence_label(visual: dict[str, Any]) -> str:
    pieces = []
    image_id = str(visual.get("record_id") or visual.get("image_id") or "").strip()
    if image_id:
        pieces.append(image_id)
    page = _int_value(visual.get("page"))
    seq_no = _int_value(visual.get("seq_no"))
    if page is not None and seq_no is not None:
        pieces.append(f"p.{page} #{seq_no}")
    elif page is not None:
        pieces.append(f"p.{page}")
    return " / ".join(pieces)


def _record_text(record: dict[str, Any]) -> str:
    return re.sub(r"\s+\n", "\n", str(record.get("text") or record.get("sentence") or "")).strip()


def _records_char_count(records: Sequence[dict[str, Any]]) -> int:
    return sum(len(_record_text_for_chunk(record)) for record in records) + max(0, len(records) - 1)


def _buffer_has_body(records: Sequence[dict[str, Any]]) -> bool:
    """見出し以外に非空白の本文、または画像・フォームの根拠があるか。"""
    body = [record for record in records if str(record.get("category") or "") not in SECTION_CATEGORIES]
    if not body:
        return False
    if any(_record_can_supply_image_evidence(record) or _is_form_like_record(record) for record in body):
        return True
    return any(_record_text(record).strip() for record in body)


def _filter_image_evidence(images: Sequence[Any]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        if visual_role_from_ref(image) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            continue
        if image.get("rag_excluded"):
            continue
        if str(image.get("raw_type") or "") == "picture_ocr_text":
            continue
        filtered.append(dict(image))
    return filtered


def _image_evidence_embedding_modality(value: dict[str, Any]) -> str:
    if str(value.get("text_preview") or value.get("ocr_text") or "").strip():
        return "image_caption_fallback"
    return "image_evidence"
