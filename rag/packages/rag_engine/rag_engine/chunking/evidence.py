"""image_evidence と表・フォーム・注釈の context、表示領域（display_regions）の組み立て。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from rag_engine.chunking.constants import FORM_VISUAL_RAW_TYPES, _bool_value, _int_value, _preview
from rag_engine.chunking.records import (
    _bbox_contains,
    _bbox_tuple,
    _image_evidence_embedding_modality,
    _record_text_for_chunk,
)
from rag_engine.parsing.decorative_pictures import DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE, visual_role_from_ref
from rag_engine.parsing.picture_text import fold_picture_ocr_entries


def _source_record_ref(record: dict[str, Any]) -> dict[str, Any]:
    text_preview = _preview(_record_text_for_chunk(record), 240)
    ref = {
        "record_id": str(record.get("id") or ""),
        "page": int(record.get("page") or 0),
        "seq_no": int(record.get("seq_no") or 0),
        "category": str(record.get("category") or ""),
        "raw_type": str(record.get("raw_type") or ""),
        "bbox": record.get("bbox") if isinstance(record.get("bbox"), list) else [],
    }
    if text_preview:
        ref["text_preview"] = text_preview
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    for key in (
        "crop_path",
        "context_crop_path",
        "vision_crop",
        "vision_context_crop",
        "vision_context_bbox",
        "vision_context_record_refs",
        "vision_input_mode",
        "vision_provider",
        "vision_model",
        "vision_error",
        "visual_artifact_type",
        "visual_kind",
        "picture_kind",
        "picture_record_id",
    ):
        value = raw.get(key)
        if value:
            ref[key] = value if isinstance(value, list) else str(value)
    for key in (
        "source_table_record_id",
        "table_structure",
        "table_row_group",
        "table_visual_evidence",
        "table_chunking_strategy",
        "visual_structure",
    ):
        value = raw.get(key)
        if value:
            ref[key] = value if isinstance(value, (dict, list)) else str(value)
    for key in (
        "layout_relationship",
        "list_item",
        "formula",
        "form_field",
        "visual_role",
        "visual_role_reason",
        "merge_target_id",
        "rag_excluded",
        "vision_skipped",
    ):
        value = raw.get(key)
        if value is not None and value != "":
            ref[key] = value
    return ref


def _image_evidence(
    refs: Sequence[dict[str, Any]],
    *,
    source_run_id: str,
    source_file_name: str,
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    # 図の中の文字（OCR）の ref は持ち主の図にまとめる。図が同じ chunk に無い OCR も、その図の画像として
    # 数える（#3455）。
    for ref in fold_picture_ocr_entries([ref for ref in refs if str(ref.get("category") or "") == "Picture"]):
        if visual_role_from_ref(ref) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            continue
        record_id = str(ref.get("record_id") or "").strip()
        page = int(ref.get("page") or 0)
        seq_no = int(ref.get("seq_no") or 0)
        image_id = record_id or f"{source_run_id}-p{page}-{seq_no}"
        if image_id in seen:
            continue
        seen.add(image_id)
        crop_path = str(ref.get("crop_path") or ref.get("vision_crop") or "").strip()
        context_crop_path = str(ref.get("context_crop_path") or ref.get("vision_context_crop") or "").strip()
        images.append(
            {
                "image_id": image_id,
                "source_run_id": source_run_id,
                "source_file_name": source_file_name,
                "page": page,
                "seq_no": seq_no,
                "bbox": ref.get("bbox") if isinstance(ref.get("bbox"), list) else [],
                "raw_type": str(ref.get("raw_type") or ""),
                "text_preview": str(ref.get("text_preview") or ""),
                **({"ocr_text": str(ref["ocr_text"])} if ref.get("ocr_text") else {}),
                "asset_kind": "crop" if crop_path else "page_region",
                "crop_path": crop_path,
                "context_crop_path": context_crop_path,
                "vision_input_mode": str(ref.get("vision_input_mode") or ""),
                "vision_provider": str(ref.get("vision_provider") or ""),
                "vision_model": str(ref.get("vision_model") or ""),
                "vision_error": str(ref.get("vision_error") or ""),
                "visual_kind": str(ref.get("visual_kind") or ref.get("picture_kind") or ref.get("visual_artifact_type") or ""),
                "visual_structure": (dict(ref.get("visual_structure")) if isinstance(ref.get("visual_structure"), dict) else {}),
                "visual_role": visual_role_from_ref(ref),
                "embedding_modality": _image_evidence_embedding_modality(ref),
            }
        )
    return images


def _merged_image_evidence(*groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for image in group:
            if not isinstance(image, dict):
                continue
            if str(image.get("raw_type") or "") == "picture_ocr_text":
                continue
            if visual_role_from_ref(image) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
                continue
            if image.get("rag_excluded"):
                continue
            image_id = str(image.get("image_id") or image.get("record_id") or "").strip()
            if not image_id:
                continue
            key = _evidence_image_key(image_id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(image))
    return merged


def _table_image_evidence(
    table_context: Sequence[dict[str, Any]],
    *,
    source_run_id: str,
    source_file_name: str,
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for table in table_context:
        if not isinstance(table, dict):
            continue
        raw_visuals = table.get("visual_evidence")
        if not isinstance(raw_visuals, list):
            continue
        table_id = str(table.get("table_id") or table.get("record_id") or "")
        for raw in raw_visuals:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("raw_type") or "") == "picture_ocr_text":
                continue
            if visual_role_from_ref(raw) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
                continue
            if raw.get("rag_excluded"):
                continue
            image_id = str(raw.get("image_id") or raw.get("record_id") or "").strip()
            if not image_id:
                continue
            crop_path = str(raw.get("crop_path") or raw.get("vision_crop") or "").strip()
            context_crop_path = str(raw.get("context_crop_path") or raw.get("vision_context_crop") or "").strip()
            images.append(
                {
                    "image_id": image_id,
                    "record_id": str(raw.get("record_id") or image_id),
                    "source_run_id": str(raw.get("source_run_id") or source_run_id),
                    "source_file_name": str(raw.get("source_file_name") or source_file_name),
                    "page": int(raw.get("page") or table.get("page") or 0),
                    "seq_no": int(raw.get("seq_no") or table.get("seq_no") or 0),
                    "bbox": raw.get("bbox") if isinstance(raw.get("bbox"), list) else [],
                    "raw_type": str(raw.get("raw_type") or "picture"),
                    "text_preview": str(raw.get("text_preview") or ""),
                    "ocr_text": str(raw.get("ocr_text") or ""),
                    "asset_kind": "crop" if crop_path else "page_region",
                    "crop_path": crop_path,
                    "context_crop_path": context_crop_path,
                    "vision_input_mode": str(raw.get("vision_input_mode") or ""),
                    "vision_provider": str(raw.get("vision_provider") or ""),
                    "vision_model": str(raw.get("vision_model") or ""),
                    "vision_error": str(raw.get("vision_error") or ""),
                    "visual_kind": str(
                        raw.get("visual_kind")
                        or raw.get("picture_kind")
                        or raw.get("visual_artifact_type")
                        or ("table" if table_id else "")
                    ),
                    "visual_structure": (dict(raw.get("visual_structure")) if isinstance(raw.get("visual_structure"), dict) else {}),
                    "visual_role": visual_role_from_ref(raw),
                    "relationship": str(raw.get("relationship") or "contained_in_table"),
                    "table_id": table_id,
                    "embedding_modality": _image_evidence_embedding_modality(raw),
                }
            )
    return images


def _form_image_evidence(
    refs: Sequence[dict[str, Any]],
    form_context: Sequence[dict[str, Any]],
    *,
    source_run_id: str,
    source_file_name: str,
) -> list[dict[str, Any]]:
    """Form field を、保存済み領域 crop または page image の画像根拠へ関連付けます。

    個別 field の細かな crop は作らず、Form 全体の record があればその crop を優先します。
    それ以外は解析 run で必ず保存される page image を使い、複雑な配置関係を保持します。
    """
    if not form_context:
        return []

    images: list[dict[str, Any]] = []
    pages = sorted({int(field.get("page") or 0) for field in form_context if int(field.get("page") or 0) > 0})
    for page in pages:
        page_fields = [dict(field) for field in form_context if int(field.get("page") or 0) == page]
        page_refs = [ref for ref in refs if int(ref.get("page") or 0) == page]
        regions = [
            ref for ref in page_refs if _is_form_visual_ref(ref) and str(ref.get("crop_path") or ref.get("vision_crop") or "").strip()
        ]
        region = regions[0] if len(regions) == 1 and _form_region_covers_fields(regions[0], page_fields) else None
        if region is not None:
            record_id = str(region.get("record_id") or "").strip()
            crop_path = str(region.get("crop_path") or region.get("vision_crop") or "").strip()
            context_crop_path = str(region.get("context_crop_path") or region.get("vision_context_crop") or "").strip()
            image_id = record_id or f"{source_run_id or 'run'}-form-page-{page}"
            seq_no = int(region.get("seq_no") or 0)
            bbox = region.get("bbox") if isinstance(region.get("bbox"), list) else []
            asset_kind = "crop"
        else:
            image_id = f"{source_run_id or 'run'}-form-page-{page}"
            record_id = ""
            seq_no = min((int(field.get("seq_no") or 0) for field in page_fields), default=0)
            bbox = _union_ref_bboxes(page_fields)
            crop_path = ""
            context_crop_path = ""
            asset_kind = "page"
        images.append(
            {
                "image_id": image_id,
                "record_id": record_id,
                "source_run_id": source_run_id,
                "source_file_name": source_file_name,
                "page": page,
                "seq_no": seq_no,
                "bbox": bbox,
                "raw_type": "form" if region is not None else "form_page",
                "asset_kind": asset_kind,
                "crop_path": crop_path,
                "context_crop_path": context_crop_path,
                "visual_kind": "form",
                "visual_structure": {"visual_kind": "form", "form_fields": page_fields},
                "visual_role": "content",
                "relationship": "form_region" if region is not None else "form_page_layout",
                "embedding_modality": "image_evidence",
            }
        )
    return images


def _is_form_visual_ref(ref: dict[str, Any]) -> bool:
    category = str(ref.get("category") or "")
    raw_type = str(ref.get("raw_type") or "").strip().lower()
    return category == "Form" or raw_type in FORM_VISUAL_RAW_TYPES


def _form_region_covers_fields(region: dict[str, Any], fields: Sequence[dict[str, Any]]) -> bool:
    """単一 Form crop が同じ chunk の全 field bbox を覆う場合だけ再利用します。"""
    region_bbox = _bbox_tuple(region.get("bbox"))
    field_bboxes = [_bbox_tuple(field.get("bbox")) for field in fields]
    valid_field_bboxes = [bbox for bbox in field_bboxes if bbox is not None]
    return (
        region_bbox is not None
        and bool(valid_field_bboxes)
        and all(_bbox_contains(region_bbox, field_bbox) for field_bbox in valid_field_bboxes)
    )


def _union_ref_bboxes(refs: Sequence[dict[str, Any]]) -> list[float]:
    """同一 page の複数 field bbox を回答時のハイライト範囲へまとめます。"""
    boxes = [_bbox_tuple(ref.get("bbox")) for ref in refs]
    valid = [box for box in boxes if box is not None]
    if not valid:
        return []
    return [
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    ]


def _evidence_image_key(image_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", image_id.lower()).strip("-")


def _caption_context(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return _annotation_context(refs, category="Caption")


def _footnote_context(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return _annotation_context(refs, category="Footnote")


def _annotation_context(refs: Sequence[dict[str, Any]], *, category: str) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for ref in refs:
        if str(ref.get("category") or "") != category:
            continue
        relationship = ref.get("layout_relationship") if isinstance(ref.get("layout_relationship"), dict) else {}
        item = {
            "record_id": str(ref.get("record_id") or ""),
            "page": int(ref.get("page") or 0),
            "seq_no": int(ref.get("seq_no") or 0),
            "bbox": ref.get("bbox") if isinstance(ref.get("bbox"), list) else [],
            "text": str(ref.get("text_preview") or ""),
            "relationship": str(relationship.get("relationship") or ""),
            "target_record_id": str(relationship.get("target_record_id") or ""),
            "target_category": str(relationship.get("target_category") or ""),
            "position": str(relationship.get("position") or ""),
        }
        annotations.append(item)
    return annotations


def _list_context(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for ref in refs:
        if str(ref.get("category") or "") != "List-item":
            continue
        list_item = ref.get("list_item") if isinstance(ref.get("list_item"), dict) else {}
        items.append(
            {
                "record_id": str(ref.get("record_id") or ""),
                "page": int(ref.get("page") or 0),
                "seq_no": int(ref.get("seq_no") or 0),
                # 本文と bbox は chunk 本文・display_regions と重複するので持たない。prompt には JSON の先頭 240 字しか
                # 載らないため、順序・階層・前後の record（この構造の本来の情報）を先頭に置く (#812)。
                "ordinal": int(list_item.get("ordinal") or len(items) + 1),
                "level": int(list_item.get("level") or 1),
                "marker": str(list_item.get("marker") or ""),
                "previous_record_id": str(list_item.get("previous_record_id") or ""),
                "next_record_id": str(list_item.get("next_record_id") or ""),
            }
        )
    return items


def _formula_context(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    formulas: list[dict[str, Any]] = []
    for ref in refs:
        if str(ref.get("category") or "") != "Formula":
            continue
        formula = ref.get("formula") if isinstance(ref.get("formula"), dict) else {}
        expression = str(formula.get("expression") or ref.get("text_preview") or "").strip()
        if not expression:
            continue
        formulas.append(
            {
                "record_id": str(ref.get("record_id") or ""),
                "page": int(ref.get("page") or 0),
                "seq_no": int(ref.get("seq_no") or 0),
                "bbox": ref.get("bbox") if isinstance(ref.get("bbox"), list) else [],
                "expression": expression,
                "format": str(formula.get("format") or "text"),
                "display": _bool_value(formula.get("display"), default=True),
            }
        )
    return formulas


def _form_context(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for ref in refs:
        form_field = ref.get("form_field") if isinstance(ref.get("form_field"), dict) else {}
        if not form_field:
            continue
        item: dict[str, Any] = {
            "record_id": str(ref.get("record_id") or ""),
            "page": int(ref.get("page") or 0),
            "seq_no": int(ref.get("seq_no") or 0),
            "bbox": ref.get("bbox") if isinstance(ref.get("bbox"), list) else [],
            "raw_type": str(form_field.get("raw_type") or ref.get("raw_type") or ""),
            "category": str(form_field.get("category") or ref.get("category") or ""),
        }
        for key in ("key", "value", "selection_status"):
            value = str(form_field.get(key) or "").strip()
            if value:
                item[key] = value
        if isinstance(form_field.get("selected"), bool):
            item["selected"] = form_field["selected"]
        fields.append(item)
    return fields


def _source_categories(refs: Sequence[dict[str, Any]]) -> list[str]:
    seen = set()
    categories = []
    for ref in refs:
        category = str(ref.get("category") or "")
        if category and category not in seen:
            seen.add(category)
            categories.append(category)
    return categories


def _display_regions(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    boxes_by_page: dict[int, list[dict[str, Any]]] = {}
    for ref in refs:
        page = _int_value(ref.get("page"))
        bbox = _bbox_tuple(ref.get("bbox"))
        if page is None or page <= 0 or bbox is None:
            continue
        box = {
            "record_id": str(ref.get("record_id") or ""),
            "seq_no": int(ref.get("seq_no") or 0),
            "category": str(ref.get("category") or ""),
            "bbox": [float(value) for value in bbox],
        }
        text_preview = str(ref.get("text_preview") or "").strip()
        if text_preview:
            box["text_preview"] = text_preview
        visual_role = str(ref.get("visual_role") or "").strip()
        if visual_role:
            box["visual_role"] = visual_role
        boxes_by_page.setdefault(page, []).append(box)
    return [
        {
            "page": page,
            "boxes": sorted(
                boxes,
                key=lambda box: (int(box.get("seq_no") or 0), str(box.get("record_id") or "")),
            ),
        }
        for page, boxes in sorted(boxes_by_page.items())
    ]
