"""Caption / Footnote / List / Formula / Form の注釈付けと、ページのボイラープレート判定。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from rag_parser_core.language.markers import boilerplate_patterns

from rag_engine.chunking.constants import (
    ANNOTATION_CATEGORIES,
    ATOMIC_CATEGORIES,
    CAPTION_TARGET_CATEGORIES,
    FOOTNOTE_TARGET_CATEGORIES,
    METADATA_ONLY_CATEGORIES,
    SECTION_CATEGORIES,
    _int_value,
)
from rag_engine.chunking.records import (
    _bbox_contains,
    _bbox_gap,
    _bbox_horizontal_overlap,
    _bbox_intersects,
    _bbox_tuple,
    _form_field_payload,
    _formula_payload,
    _list_level,
    _list_marker,
    _record_text,
    _record_text_for_chunk,
    _record_visual_role,
    _relative_position,
    _table_structure,
    _visual_group_key,
)
from rag_engine.parsing.decorative_pictures import DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE
from rag_engine.parsing.picture_text import LEGACY_PICTURE_OCR_TEXT_LABELS, picture_ocr_text_label


def _is_excluded_page_boilerplate_record(
    record: dict[str, Any],
    metadata_by_page: dict[int, dict[str, Any]],
) -> bool:
    page = _int_value(record.get("page"))
    if page is None:
        return False
    record_id = str(record.get("id") or "")
    seq_no = int(record.get("seq_no") or 0)
    excluded_layout = metadata_by_page.get(page, {}).get("excluded_layout")
    if not isinstance(excluded_layout, list):
        return False
    for item in excluded_layout:
        if not isinstance(item, dict):
            continue
        if record_id and str(item.get("record_id") or "") == record_id:
            return True
        if not record_id and int(item.get("seq_no") or 0) == seq_no:
            return True
    return False


def _is_page_boilerplate_item(item: dict[str, Any]) -> bool:
    role = str(item.get("role") or item.get("category") or "")
    text = str(item.get("text") or "")
    if role == "page_number":
        return True
    if bool(item.get("repeated")):
        return True
    return _looks_like_page_boilerplate_text(text)


# 社名の語尾・定型文の語・見出しを比べるときに外す記号は言語資源の「定型文と装飾」(#2181)。


def _is_section_banner_text(ocr_texts: Sequence[str], section_path: Sequence[str], heading_texts: Sequence[str]) -> bool:
    """Picture の OCR が「節見出しの再掲 + 著作権などの定型文」だけなら True（ページの帯画像）。

    帯画像は説明書の各ページに現れ、chunk にすると同じ文の child が 1 ページ 1 件できて候補枠を消費する。
    OCR の各行から定型文（著作権・URL・社名）を除き、残りの行がすべて現在の節パスまたは直前の見出しに
    含まれる（またはそれらを含む）なら帯とみなす。OCR が無い画像や、見出し以外の行がある画像は対象外 (#897)。
    """
    lines: list[str] = []
    for text in ocr_texts:
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.rstrip(":：") in {picture_ocr_text_label(), *LEGACY_PICTURE_OCR_TEXT_LABELS}:
                continue
            lines.append(line)
    if not lines:
        return False
    ignored = boilerplate_patterns().compare_ignored
    headings = [ignored.sub("", unicodedata.normalize("NFKC", item)) for item in (*section_path, *heading_texts)]
    headings = [item for item in headings if item]
    for line in lines:
        if _looks_like_page_boilerplate_text(line) or boilerplate_patterns().company.search(line):
            continue
        key = boilerplate_patterns().compare_ignored.sub("", unicodedata.normalize("NFKC", line))
        if not key:
            continue
        if not any(key in heading or heading in key for heading in headings):
            return False
    return True


def _looks_like_page_boilerplate_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False
    return bool(boilerplate_patterns().boilerplate.search(normalized))


def _visual_groups(records: Sequence[dict[str, Any]]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        if str(record.get("category") or "") != "Picture":
            continue
        if _record_visual_role(record) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            continue
        key = _visual_group_key(record)
        if key is None:
            continue
        groups.setdefault(key, []).append(record)
    return groups


def _inline_icons_by_merge_target(records: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if str(record.get("category") or "") != "Picture":
            continue
        if _record_visual_role(record) != INLINE_ICON_VISUAL_ROLE:
            continue
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        target_id = str(raw.get("merge_target_id") or "").strip()
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(record)
    for icons in grouped.values():
        icons.sort(
            key=lambda item: (
                int(item.get("page") or 0),
                int(item.get("seq_no") or 0),
                str(item.get("id") or ""),
            )
        )
    return grouped


def _annotate_layout_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for record in records:
        copied = dict(record)
        copied["raw"] = dict(copied.get("raw") if isinstance(copied.get("raw"), dict) else {})
        annotated.append(copied)

    _annotate_list_records(annotated)
    for record in annotated:
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        if str(record.get("category") or "") == "Formula":
            formula = _formula_payload(record)
            if formula:
                raw["formula"] = formula
        form_field = _form_field_payload(record)
        if form_field:
            raw["form_field"] = form_field

    table_contained_visual_ids = _table_contained_visual_ids(annotated)
    # 注釈の対象は同じ engine・page にしかないので、(engine, page) ごとの索引を 1 回作って渡す (#871)。
    by_page: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for record in annotated:
        by_page.setdefault(_engine_page_key(record), []).append(record)
    for record in annotated:
        category = str(record.get("category") or "")
        if category == "Caption":
            target = _nearest_layout_target(
                record,
                by_page.get(_engine_page_key(record), ()),
                target_categories=CAPTION_TARGET_CATEGORIES,
                excluded_target_ids=table_contained_visual_ids,
                max_seq_gap=4,
                prefer_previous=False,
            )
            if target is not None:
                _mark_layout_relationship(record, target, "caption_for")
        elif category == "Footnote":
            target = _nearest_layout_target(
                record,
                by_page.get(_engine_page_key(record), ()),
                target_categories=FOOTNOTE_TARGET_CATEGORIES,
                excluded_target_ids=table_contained_visual_ids,
                max_seq_gap=None,
                prefer_previous=True,
            )
            if target is not None:
                _mark_layout_relationship(record, target, "footnote_for")
    return annotated


def _engine_page_key(record: dict[str, Any]) -> tuple[str, int | None]:
    return (str(record.get("engine") or ""), _int_value(record.get("page")))


def _annotate_list_records(records: Sequence[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(_engine_page_key(record), []).append(record)

    for page_records in grouped.values():
        page_records.sort(key=lambda item: (int(item.get("seq_no") or 0), str(item.get("id") or "")))
        current_list: list[dict[str, Any]] = []
        for record in page_records:
            if str(record.get("category") or "") == "List-item":
                current_list.append(record)
                continue
            if _is_list_sequence_boundary(record):
                _annotate_list_group(current_list)
                current_list = []
        _annotate_list_group(current_list)


def _annotate_list_group(records: Sequence[dict[str, Any]]) -> None:
    for index, record in enumerate(records):
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        previous_id = str(records[index - 1].get("id") or "") if index > 0 else ""
        next_id = str(records[index + 1].get("id") or "") if index + 1 < len(records) else ""
        raw["list_item"] = {
            "ordinal": index + 1,
            "level": _list_level(record),
            "marker": _list_marker(record),
            "previous_record_id": previous_id,
            "next_record_id": next_id,
        }


def _is_list_sequence_boundary(record: dict[str, Any]) -> bool:
    category = str(record.get("category") or "")
    if category in METADATA_ONLY_CATEGORIES or category in ANNOTATION_CATEGORIES:
        return False
    if category == "Picture" and _record_visual_role(record) == INLINE_ICON_VISUAL_ROLE:
        return False
    return category in SECTION_CATEGORIES or category in ATOMIC_CATEGORIES or bool(_record_text_for_chunk(record))


def _nearest_layout_target(
    annotation: dict[str, Any],
    records: Sequence[dict[str, Any]],
    *,
    target_categories: set[str],
    excluded_target_ids: set[str] | None = None,
    max_seq_gap: int | None,
    prefer_previous: bool,
) -> dict[str, Any] | None:
    excluded_target_ids = excluded_target_ids or set()
    annotation_id = str(annotation.get("id") or "")
    annotation_engine = str(annotation.get("engine") or "")
    annotation_page = _int_value(annotation.get("page"))
    annotation_seq = _int_value(annotation.get("seq_no"))
    candidates: list[tuple[float, dict[str, Any]]] = []
    for target in records:
        target_id = str(target.get("id") or "")
        if not target_id or target_id == annotation_id:
            continue
        if target_id in excluded_target_ids:
            continue
        if str(target.get("engine") or "") != annotation_engine:
            continue
        if _int_value(target.get("page")) != annotation_page:
            continue
        target_category = str(target.get("category") or "")
        if target_category not in target_categories:
            continue
        if not _is_annotation_target_record(target):
            continue
        target_seq = _int_value(target.get("seq_no"))
        if (
            annotation_seq is not None
            and target_seq is not None
            and max_seq_gap is not None
            and abs(annotation_seq - target_seq) > max_seq_gap
        ):
            continue
        candidates.append((_layout_target_score(annotation, target, prefer_previous=prefer_previous), target))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], int(item[1].get("seq_no") or 0), str(item[1].get("id") or "")))
    return candidates[0][1]


def _is_annotation_target_record(record: dict[str, Any]) -> bool:
    category = str(record.get("category") or "")
    if category in ANNOTATION_CATEGORIES or category in METADATA_ONLY_CATEGORIES:
        return False
    if category == "Picture":
        if str(record.get("raw_type") or "") == "picture_ocr_text":
            return False
        return _record_visual_role(record) not in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}
    if category == "Table":
        return True
    return bool(_record_text(record)) or bool(_form_field_payload(record))


def _layout_target_score(annotation: dict[str, Any], target: dict[str, Any], *, prefer_previous: bool) -> float:
    annotation_seq = _int_value(annotation.get("seq_no")) or 0
    target_seq = _int_value(target.get("seq_no")) or 0
    seq_gap = abs(annotation_seq - target_seq)
    score = float(seq_gap * 1000)
    annotation_bbox = _bbox_tuple(annotation.get("bbox"))
    target_bbox = _bbox_tuple(target.get("bbox"))
    if annotation_bbox is not None and target_bbox is not None:
        score += _bbox_gap(annotation_bbox, target_bbox)
        if _bbox_horizontal_overlap(annotation_bbox, target_bbox) > 0:
            score -= 100
        else:
            score += 100
    if prefer_previous:
        score += 0 if target_seq <= annotation_seq else 300
    if str(target.get("category") or "") in ATOMIC_CATEGORIES:
        score -= 50
    return score


def _mark_layout_relationship(annotation: dict[str, Any], target: dict[str, Any], relationship: str) -> None:
    raw = annotation.get("raw") if isinstance(annotation.get("raw"), dict) else {}
    raw["layout_relationship"] = {
        "relationship": relationship,
        "target_record_id": str(target.get("id") or ""),
        "target_category": str(target.get("category") or ""),
        "target_raw_type": str(target.get("raw_type") or ""),
        "position": _relative_position(annotation, target),
        "reason": "nearest_same_page_layout_element",
    }


def _attached_layout_annotations_by_target(records: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if str(record.get("category") or "") not in ANNOTATION_CATEGORIES:
            continue
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        relationship = raw.get("layout_relationship") if isinstance(raw.get("layout_relationship"), dict) else {}
        target_id = str(relationship.get("target_record_id") or "")
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(record)
    for items in grouped.values():
        items.sort(key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0), str(item.get("id") or "")))
    return grouped


def _attached_layout_annotations(
    target_id: str,
    attached_annotations_by_target: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [dict(item) for item in attached_annotations_by_target.get(str(target_id or ""), [])]


def _attached_layout_annotations_for_targets(
    target_ids: set[str],
    attached_annotations_by_target: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target_id in sorted(target_ids):
        for annotation in attached_annotations_by_target.get(target_id, []):
            annotation_id = str(annotation.get("id") or "")
            if annotation_id in seen:
                continue
            seen.add(annotation_id)
            annotations.append(dict(annotation))
    annotations.sort(key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0), str(item.get("id") or "")))
    return annotations


def _table_contained_visual_ids(records: Sequence[dict[str, Any]]) -> set[str]:
    table_scopes: list[tuple[str, int | None, tuple[float, float, float, float]]] = []
    for record in records:
        if str(record.get("category") or "") != "Table":
            continue
        # _table_record_with_metadata と同じ HTML で判定します。基準がずれると、表内 Picture が
        # 単独 chunk と表の visual_evidence の両方に現れます。
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        if not _table_structure(str(raw.get("table_enhanced_html") or "") or _record_text(record)):
            continue
        bbox = _bbox_tuple(record.get("bbox"))
        if bbox is None:
            continue
        table_scopes.append((str(record.get("engine") or ""), _int_value(record.get("page")), bbox))
    if not table_scopes:
        return set()

    contained: set[str] = set()
    for record in records:
        if str(record.get("category") or "") != "Picture":
            continue
        if _record_visual_role(record) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            continue
        picture_bbox = _bbox_tuple(record.get("bbox"))
        if picture_bbox is None:
            continue
        engine = str(record.get("engine") or "")
        page = _int_value(record.get("page"))
        if any(
            table_engine == engine
            and table_page == page
            and (_bbox_contains(table_bbox, picture_bbox) or _bbox_intersects(table_bbox, picture_bbox))
            for table_engine, table_page, table_bbox in table_scopes
        ):
            record_id = str(record.get("id") or "").strip()
            if record_id:
                contained.add(record_id)
    return contained
