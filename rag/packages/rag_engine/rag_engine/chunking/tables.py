"""表レコードの metadata・行グループ・表内画像の対応付け。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag_engine.chunking.constants import (
    SOURCE_TABLE_CROP_RELATIONSHIP,
    ChunkingConfig,
    _compact_json_text,
    _int_value,
    _preview,
)
from rag_engine.chunking.layout import _engine_page_key, _visual_group_key
from rag_engine.chunking.records import (
    _bbox_contains,
    _bbox_intersects,
    _bbox_tuple,
    _filter_image_evidence,
    _join_records_text,
    _record_text,
    _record_visual_role,
    _table_structure,
)
from rag_engine.parsing.decorative_pictures import DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE, visual_role_from_ref
from rag_engine.parsing.picture_text import format_docling_picture_ocr_text
from rag_engine.parsing.table_structure import table_row_groups


def _table_record_with_metadata(record: dict[str, Any], records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    enhanced_html = str(raw.get("table_enhanced_html") or "")
    structure = _table_structure(enhanced_html or _record_text(record))
    contained_visuals = _contained_table_visual_evidence(record, records)
    visual_evidence = [
        *raw.get("table_row_image_evidence", []),
        *_source_table_crop_evidence(record),
        *contained_visuals,
    ]
    if not structure and not visual_evidence:
        return record
    enriched = dict(record)
    if enhanced_html:
        enriched["text"] = enhanced_html
    raw = dict(enriched.get("raw") if isinstance(enriched.get("raw"), dict) else {})
    if structure:
        raw["table_structure"] = structure
        raw["table_chunking_strategy"] = "structured_table"
    raw["table_visual_evidence"] = visual_evidence
    # 表内 Picture は単独 chunk にならず、説明が本文へ入る経路は表 chunk だけです。evidence の
    # text_preview は表示用に 240 字で切るため、本文用の全文は metadata へ写らない key で渡します。
    contained_ids = {str(visual.get("image_id") or "") for visual in contained_visuals}
    raw["table_visual_texts"] = {
        record_id: _record_text(item)
        for item in records
        if (record_id := str(item.get("id") or "")) in contained_ids and _record_text(item)
    }
    enriched["raw"] = raw
    return enriched


def _table_supplements_by_table(records: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """表の直後にある補足レコード（text layer から補った原文。#564）を、元の表のレコード ID ごとに集めます。

    補足は解析側が表と同じページ・直後の seq に置く。通常の Text として扱うと表と別の child / parent に
    入ることがあるため、表の child に同梱する。
    """
    by_table: dict[str, list[dict[str, Any]]] = {}
    ordered = sorted(records, key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0)))
    previous_table_id = ""
    for record in ordered:
        if str(record.get("category") or "") == "Table":
            previous_table_id = str(record.get("id") or "")
            continue
        if str(record.get("raw_type") or "") == "table_unassigned_text" and previous_table_id:
            by_table.setdefault(previous_table_id, []).append(record)
            continue
        previous_table_id = ""
    return by_table


def _table_row_group_records(
    table_record: dict[str, Any],
    heading_records: Sequence[dict[str, Any]],
    config: ChunkingConfig,
) -> list[dict[str, Any]]:
    text = _record_text(table_record)
    raw = table_record.get("raw") if isinstance(table_record.get("raw"), dict) else {}
    structure = raw.get("table_structure") if isinstance(raw.get("table_structure"), dict) else {}
    # 親の上限まで待つと、検索単位より大きい表が一つの vector に集約される。閾値は表専用
    # （table_child_target_chars）。列見出しが各グループに付くので本文より大きな単位でよい (#894)。
    if not structure or len(text) <= config.table_child_target_chars:
        return []

    heading_text = _join_records_text(heading_records)
    groups = table_row_groups(
        structure,
        target_chars=config.table_child_target_chars,
        heading_text=heading_text,
    )
    if len(groups) <= 1:
        return []

    records: list[dict[str, Any]] = []
    table_id = str(table_record.get("id") or "")
    table_visuals = raw.get("table_visual_evidence") if isinstance(raw.get("table_visual_evidence"), list) else []
    group_visuals = [
        _table_visual_evidence_for_row_group(
            table_record,
            table_visuals,
            group,
            structure,
            include_source_table_crop=index == 1,
        )
        for index, group in enumerate(groups, start=1)
    ]
    _attach_unmatched_table_visuals(table_record, table_visuals, groups, structure, group_visuals)
    for index, group in enumerate(groups, start=1):
        row_record = dict(table_record)
        row_record["id"] = f"{table_id}-rows-{group['row_start']}-{group['row_end']}"
        row_record["text"] = str(group.get("text") or "")
        row_record["raw_type"] = "table_row_group"
        row_raw = {
            "source_table_record_id": table_id,
            "table_structure": group.get("structure") if isinstance(group.get("structure"), dict) else {},
            "table_row_group": {
                "index": index,
                "row_start": int(group.get("row_start") or 0),
                "row_end": int(group.get("row_end") or 0),
                "row_count": int(group.get("row_count") or 0),
                "total_row_count": int(structure.get("row_count") or 0),
                "column_count": int(structure.get("column_count") or 0),
                "source_table_record_id": table_id,
            },
            "table_visual_evidence": group_visuals[index - 1],
            "table_visual_texts": raw.get("table_visual_texts") if isinstance(raw.get("table_visual_texts"), dict) else {},
            "table_chunking_strategy": "row_group",
            # 表全体の補足は先頭 group に一度だけ保持し、行との対応を捏造しません。
            "table_vision_text": str(raw.get("table_vision_text") or "") if index == 1 else "",
        }
        row_record["raw"] = row_raw
        records.append(row_record)
    return records


def _table_visual_evidence_for_row_group(
    table_record: dict[str, Any],
    visual_evidence: Sequence[Any],
    group: dict[str, Any],
    structure: dict[str, Any],
    *,
    include_source_table_crop: bool,
) -> list[dict[str, Any]]:
    visuals = _filter_image_evidence(visual_evidence)
    if not visuals:
        return []

    # 同じ table crop の重複 vector が全 row group を占有しないよう、先頭 group だけへ関連付ける。
    source_table_crops = [visual for visual in visuals if str(visual.get("relationship") or "") == SOURCE_TABLE_CROP_RELATIONSHIP]
    row_visuals = [visual for visual in visuals if str(visual.get("relationship") or "") != SOURCE_TABLE_CROP_RELATIONSHIP]
    selected_table_crops = source_table_crops if include_source_table_crop else []

    table_bbox = _bbox_tuple(table_record.get("bbox"))
    row_start = _int_value(group.get("row_start"))
    row_end = _int_value(group.get("row_end"))
    total_row_count = _int_value(structure.get("row_count"))
    if (
        table_bbox is None
        or row_start is None
        or row_end is None
        or total_row_count is None
        or row_start <= 0
        or row_end <= 0
        or total_row_count <= 0
    ):
        return [*selected_table_crops, *row_visuals]

    row_group_bbox = _estimated_table_row_group_bbox(
        table_bbox,
        row_start=row_start,
        row_end=row_end,
        total_row_count=total_row_count,
    )
    matching_row_visuals = [
        visual
        for visual in row_visuals
        if (
            row_start <= int(visual["row_index"]) <= row_end
            if visual.get("row_index") is not None
            else _visual_evidence_matches_row_group(visual, row_group_bbox)
        )
    ]
    return [*selected_table_crops, *matching_row_visuals]


def _attach_unmatched_table_visuals(
    table_record: dict[str, Any],
    visual_evidence: Sequence[Any],
    groups: Sequence[dict[str, Any]],
    structure: dict[str, Any],
    group_visuals: list[list[dict[str, Any]]],
) -> None:
    """どの row group にも一致しなかった表内画像を、最寄りの group へ追加します。

    表に重なる Picture は単独 chunk の対象から外れるため、行帯の判定（中心または高さ 25%）に
    漏れた画像はここで拾わないとどの chunk にも残りません。表の端に少しだけ重なる図や、
    見出し行に属する画像が該当します。group_visuals は呼び出し側のリストを直接更新します。
    """
    matched = {_table_visual_key(visual) for visuals in group_visuals for visual in visuals}
    table_bbox = _bbox_tuple(table_record.get("bbox"))
    total_row_count = _int_value(structure.get("row_count")) or 0
    for visual in _filter_image_evidence(visual_evidence):
        if str(visual.get("relationship") or "") == SOURCE_TABLE_CROP_RELATIONSHIP:
            continue
        if _table_visual_key(visual) in matched:
            continue
        matched.add(_table_visual_key(visual))
        row_index = _int_value(visual.get("row_index"))
        visual_bbox = _bbox_tuple(visual.get("bbox"))

        # 反復ごとの値は既定値で束縛する（min の key として同じ反復の中でだけ呼ぶ。B023）。
        def distance(
            group: dict[str, Any],
            row_index: int | None = row_index,
            visual_bbox: tuple[float, float, float, float] | None = visual_bbox,
        ) -> float:
            row_start = _int_value(group.get("row_start")) or 0
            row_end = _int_value(group.get("row_end")) or 0
            if row_index is not None:
                return float(max(row_start - row_index, row_index - row_end, 0))
            if table_bbox is None or visual_bbox is None or total_row_count <= 0:
                return 0.0
            band = _estimated_table_row_group_bbox(table_bbox, row_start=row_start, row_end=row_end, total_row_count=total_row_count)
            center_y = (visual_bbox[1] + visual_bbox[3]) / 2
            return max(band[1] - center_y, center_y - band[3], 0.0)

        nearest = min(range(len(groups)), key=lambda position: distance(groups[position]))
        group_visuals[nearest].append(visual)


def _table_visual_key(visual: dict[str, Any]) -> str:
    # 行画像の record_id は表自身の ID で重複するため、image_id を優先します。
    return str(visual.get("image_id") or visual.get("record_id") or "") or _compact_json_text(visual, 2000)


def _estimated_table_row_group_bbox(
    table_bbox: tuple[float, float, float, float],
    *,
    row_start: int,
    row_end: int,
    total_row_count: int,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = table_bbox
    row_height = (bottom - top) / max(1, total_row_count)
    band_top = top + max(0, row_start - 1) * row_height
    band_bottom = top + min(total_row_count, row_end) * row_height
    pad = min(max(1.0, row_height * 0.25), max(1.0, (bottom - top) * 0.05))
    return (left, max(top, band_top - pad), right, min(bottom, band_bottom + pad))


def _visual_evidence_matches_row_group(
    visual: dict[str, Any],
    row_group_bbox: tuple[float, float, float, float],
) -> bool:
    visual_bbox = _bbox_tuple(visual.get("bbox"))
    if visual_bbox is None:
        return True
    if not _bbox_intersects(row_group_bbox, visual_bbox):
        return False
    overlap_height = min(row_group_bbox[3], visual_bbox[3]) - max(row_group_bbox[1], visual_bbox[1])
    if overlap_height <= 0:
        return False
    visual_height = visual_bbox[3] - visual_bbox[1]
    visual_center_y = (visual_bbox[1] + visual_bbox[3]) / 2
    return row_group_bbox[1] <= visual_center_y <= row_group_bbox[3] or overlap_height >= visual_height * 0.25


def _contained_table_visual_evidence(
    table_record: dict[str, Any],
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    table_bbox = _bbox_tuple(table_record.get("bbox"))
    if table_bbox is None:
        return []
    # _table_contained_visual_ids と同じ条件にする。構造化できない表で片方だけが Picture を表内と
    # みなすと、その Picture が表 child の「表内画像」と単独 child の両方に出る。
    raw = table_record.get("raw") if isinstance(table_record.get("raw"), dict) else {}
    if not _table_structure(str(raw.get("table_enhanced_html") or "") or _record_text(table_record)):
        return []
    images: list[dict[str, Any]] = []
    # 表内 Picture とその OCR 文は表と同じ engine・page にしかないので、1 回絞り込んで使い回す (#871)。
    page_records = [
        record for record in records if record is not table_record and _engine_page_key(record) == _engine_page_key(table_record)
    ]
    for record in page_records:
        if str(record.get("category") or "") != "Picture":
            continue
        if str(record.get("raw_type") or "") == "picture_ocr_text":
            continue
        if _record_visual_role(record) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            continue
        picture_bbox = _bbox_tuple(record.get("bbox"))
        if picture_bbox is None:
            continue
        contains = _bbox_contains(table_bbox, picture_bbox)
        intersects = _bbox_intersects(table_bbox, picture_bbox)
        if not contains and not intersects:
            continue
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        paired_ocr_text = _paired_picture_ocr_text(record, page_records)
        images.append(
            {
                "image_id": str(record.get("id") or ""),
                "record_id": str(record.get("id") or ""),
                "page": int(record.get("page") or 0),
                "seq_no": int(record.get("seq_no") or 0),
                "bbox": record.get("bbox") if isinstance(record.get("bbox"), list) else [],
                "raw_type": str(record.get("raw_type") or ""),
                "relationship": "contained_in_table" if contains else "intersects_table",
                "text_preview": _preview(_record_text(record), 240),
                "ocr_text": paired_ocr_text,
                "vision_crop": str(raw.get("vision_crop") or ""),
                "vision_context_crop": str(raw.get("vision_context_crop") or ""),
                "crop_path": str(raw.get("crop_path") or raw.get("vision_crop") or ""),
                "context_crop_path": str(raw.get("context_crop_path") or raw.get("vision_context_crop") or ""),
                "vision_input_mode": str(raw.get("vision_input_mode") or ""),
                "vision_provider": str(raw.get("vision_provider") or ""),
                "vision_model": str(raw.get("vision_model") or ""),
                "vision_error": str(raw.get("vision_error") or ""),
                "visual_kind": str(raw.get("visual_kind") or raw.get("picture_kind") or raw.get("visual_artifact_type") or ""),
                "visual_structure": (dict(raw.get("visual_structure")) if isinstance(raw.get("visual_structure"), dict) else {}),
                "visual_role": visual_role_from_ref(raw),
            }
        )
    images.sort(key=lambda item: (int(item.get("seq_no") or 0), str(item.get("record_id") or "")))
    return images


def _source_table_crop_evidence(table_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Table 自身の保存済み crop を構造化表と同じ evidence group に関連付けます。"""
    raw = table_record.get("raw") if isinstance(table_record.get("raw"), dict) else {}
    crop_path = str(raw.get("crop_path") or "").strip()
    record_id = str(table_record.get("id") or "").strip()
    if not crop_path or not record_id:
        return []
    return [
        {
            "image_id": record_id,
            "record_id": record_id,
            "page": int(table_record.get("page") or 0),
            "seq_no": int(table_record.get("seq_no") or 0),
            "bbox": table_record.get("bbox") if isinstance(table_record.get("bbox"), list) else [],
            "raw_type": str(table_record.get("raw_type") or "table"),
            "relationship": SOURCE_TABLE_CROP_RELATIONSHIP,
            "text_preview": _preview(_record_text(table_record), 240),
            "ocr_text": "",
            "crop_path": crop_path,
            "context_crop_path": "",
            "visual_kind": "table",
            "visual_role": "content",
        }
    ]


def _paired_picture_ocr_text(
    picture_record: dict[str, Any],
    records: Sequence[dict[str, Any]],
) -> str:
    picture_key = _visual_group_key(picture_record)
    if picture_key is None:
        return ""
    for record in records:
        if record is picture_record:
            continue
        if str(record.get("raw_type") or "") != "picture_ocr_text":
            continue
        if _visual_group_key(record) != picture_key:
            continue
        return format_docling_picture_ocr_text(_record_text(record))
    return ""


def _table_context(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        if str(ref.get("category") or "") != "Table":
            continue
        structure = ref.get("table_structure") if isinstance(ref.get("table_structure"), dict) else {}
        row_group = ref.get("table_row_group") if isinstance(ref.get("table_row_group"), dict) else {}
        visual_evidence = _filter_image_evidence(
            ref.get("table_visual_evidence") if isinstance(ref.get("table_visual_evidence"), list) else []
        )
        if not structure and not row_group and not visual_evidence:
            continue
        table_id = str(ref.get("source_table_record_id") or ref.get("record_id") or "").strip()
        key = str(ref.get("record_id") or table_id)
        if key in seen:
            continue
        seen.add(key)
        tables.append(
            {
                "table_id": table_id,
                "record_id": str(ref.get("record_id") or ""),
                "page": int(ref.get("page") or 0),
                "seq_no": int(ref.get("seq_no") or 0),
                "bbox": ref.get("bbox") if isinstance(ref.get("bbox"), list) else [],
                "structure": structure,
                "row_group": row_group,
                "visual_evidence": list(visual_evidence),
                "chunking_strategy": str(ref.get("table_chunking_strategy") or ""),
            }
        )
    return tables


def _normalized_table_context(table_context: Sequence[Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for table in table_context:
        if not isinstance(table, dict):
            continue
        item = dict(table)
        visual_evidence = item.get("visual_evidence")
        item["visual_evidence"] = _filter_image_evidence(visual_evidence) if isinstance(visual_evidence, list) else []
        tables.append(item)
    return tables
