"""Picture レコードを RAG 対象・装飾・OCR 統合対象へ分類する。"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from rag_parser_core.language.markers import boilerplate_patterns

from rag_engine.parsing.picture_text import picture_ocr_text_label

CONTENT_VISUAL_ROLE = "content"
DECORATIVE_VISUAL_ROLE = "decorative"
INLINE_ICON_VISUAL_ROLE = "inline_icon"

PictureVisualRole = Literal["content", "decorative", "inline_icon"]

SMALL_PICTURE_MAX_AREA_RATIO = 0.006
SMALL_PICTURE_MAX_WIDTH_RATIO = 0.08
SMALL_PICTURE_MAX_HEIGHT_RATIO = 0.08
SMALL_PICTURE_MAX_SIDE_PX = 96.0
PAGE_EDGE_BAND_RATIO = 0.12
NEARBY_TEXT_MAX_SEQ_DISTANCE = 2
NEARBY_TEXT_MAX_VERTICAL_RATIO = 0.055
NEARBY_TEXT_MAX_VERTICAL_PX = 96.0

# ロゴ・定型文・装飾の種類・操作の語・業務の語は言語資源の「定型文と装飾」(#2181)。


@dataclass(frozen=True)
class PictureRecordRole:
    """Picture レコードを VLM/RAG/テキスト統合でどう扱うかを表します。"""

    role: PictureVisualRole
    reason: str = ""
    merge_target_id: str = ""

    @property
    def skip_vlm(self) -> bool:
        """この Picture を Vision 説明の対象外にするかを返します。"""
        return self.role in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}

    @property
    def exclude_from_rag(self) -> bool:
        """この Picture を RAG 検索対象から除外するかを返します。"""
        return self.role == DECORATIVE_VISUAL_ROLE

    @property
    def merge_with_text(self) -> bool:
        """この Picture を隣接 text chunk へ統合するかを返します。"""
        return self.role == INLINE_ICON_VISUAL_ROLE and bool(self.merge_target_id)


def classify_picture_record(record: Any, records: Sequence[Any] = ()) -> PictureRecordRole:
    """Picture record のサイズ、本文、近傍から役割を分類します。"""
    if str(_record_value(record, "category") or "") != "Picture":
        return PictureRecordRole(CONTENT_VISUAL_ROLE)

    # logo という視覚種別だけで業務タイトルや操作情報まで除外しません。
    if _has_meaningful_content(record, records):
        return PictureRecordRole(CONTENT_VISUAL_ROLE, "meaningful_text_in_visual")

    explicit_role = _normalized_visual_role(_raw_value(record, "visual_role"))
    if explicit_role == DECORATIVE_VISUAL_ROLE:
        return PictureRecordRole(DECORATIVE_VISUAL_ROLE, str(_raw_value(record, "visual_role_reason") or "explicit"))
    if explicit_role == INLINE_ICON_VISUAL_ROLE:
        target = str(_raw_value(record, "merge_target_id") or _related_operation_record_id(record, records) or "")
        return PictureRecordRole(
            INLINE_ICON_VISUAL_ROLE,
            str(_raw_value(record, "visual_role_reason") or "explicit"),
            target,
        )

    if _has_explicit_decorative_type(record):
        return PictureRecordRole(DECORATIVE_VISUAL_ROLE, "explicit_decorative_picture_type")

    # ページの帯画像（節見出しの再掲 + 著作権表示だけの OCR）は幅が広く small ではないが、業務情報を持たない。
    # 解析時に decorative にして Vision を呼ばない (#899)。チャンキング側にも節パスによる保険がある (#897)。
    if _is_section_banner(record, records):
        return PictureRecordRole(DECORATIVE_VISUAL_ROLE, "section_banner_ocr")

    if not is_small_picture_record(record):
        return PictureRecordRole(CONTENT_VISUAL_ROLE)

    related_text_id = _related_operation_record_id(record, records)
    if related_text_id and not _looks_like_logo_or_boilerplate(record) and not _is_near_page_edge(record):
        return PictureRecordRole(INLINE_ICON_VISUAL_ROLE, "small_picture_near_operation_text", related_text_id)

    if _is_near_page_edge(record):
        return PictureRecordRole(DECORATIVE_VISUAL_ROLE, "small_picture_in_page_edge_band")
    if _looks_like_logo_or_boilerplate(record):
        return PictureRecordRole(DECORATIVE_VISUAL_ROLE, "small_logo_or_boilerplate_picture")

    return PictureRecordRole(CONTENT_VISUAL_ROLE)


def _has_meaningful_content(record: Any, records: Sequence[Any]) -> bool:
    """同一 bbox の OCR と具体的な Vision field から業務情報の存在を確認します。"""
    description = _raw_value(record, "vision_description")
    if isinstance(description, dict):
        fields = (
            "visible_screen_names",
            "visible_buttons",
            "visible_fields",
            "operation_steps",
            "condition_result_pairs",
            "codes_and_errors",
            "table_rows",
            "diagram_nodes",
        )
        if any(description.get(field) for field in fields):
            return True
    texts = [_record_text(record)] if str(_record_value(record, "raw_type")) == "picture_ocr_text" else []
    for other in records:
        if (
            _record_value(other, "page") == _record_value(record, "page")
            and _record_value(other, "engine") == _record_value(record, "engine")
            and _record_value(other, "raw_type") == "picture_ocr_text"
            and _bbox(other) == _bbox(record)
        ):
            texts.append(_record_text(other))
    heading_keys = _heading_keys(record, records)
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(f"{picture_ocr_text_label()}:") or _is_boilerplate_line(line):
                continue
            # 節見出し・柱の再掲は業務情報ではない（「Ａ受注管理 －（３）受注入力」の「管理」で反応しない）(#899)
            if _repeats_heading(line, heading_keys):
                continue
            if boilerplate_patterns().operation.search(line) or (len(line) >= 4 and boilerplate_patterns().business.search(line)):
                return True
    return False


_HEADING_MIN_CONTAINMENT_CHARS = 4


def _is_boilerplate_line(line: str) -> bool:
    return bool(boilerplate_patterns().logo.search(line) or boilerplate_patterns().company_or_url.search(line))


def _compare_key(text: str) -> str:
    return boilerplate_patterns().compare_ignored.sub("", unicodedata.normalize("NFKC", str(text or "")))


def _heading_keys(record: Any, records: Sequence[Any]) -> list[str]:
    """帯画像の再掲と比べる見出し: 同じ engine の Section-header / Title 全件と、同一ページの本文 record。"""
    keys: list[str] = []
    for other in records:
        if other is record or _record_value(other, "engine") != _record_value(record, "engine"):
            continue
        category = str(_record_value(other, "category") or "")
        if category in {"Section-header", "Title"} or (
            category == "Text" and _record_value(other, "page") == _record_value(record, "page")
        ):
            key = _compare_key(_record_text(other))
            if key:
                keys.append(key)
    return keys


def _repeats_heading(line: str, heading_keys: Sequence[str]) -> bool:
    key = _compare_key(line)
    if not key:
        return True
    for heading in heading_keys:
        if key == heading:
            return True
        if min(len(key), len(heading)) >= _HEADING_MIN_CONTAINMENT_CHARS and (key in heading or heading in key):
            return True
    return False


def _ocr_lines(record: Any, records: Sequence[Any]) -> list[str]:
    """同一 bbox の OCR record の本文をラベル行を除いて行に分ける。"""
    texts = [_record_text(record)] if str(_record_value(record, "raw_type")) == "picture_ocr_text" else []
    for other in records:
        if (
            _record_value(other, "page") == _record_value(record, "page")
            and _record_value(other, "engine") == _record_value(record, "engine")
            and _record_value(other, "raw_type") == "picture_ocr_text"
            and _bbox(other) == _bbox(record)
        ):
            texts.append(_record_text(other))
    lines: list[str] = []
    for text in texts:
        for line in str(text or "").splitlines():
            line = line.strip()
            if line and not line.startswith(f"{picture_ocr_text_label()}:"):
                lines.append(line)
    return lines


def _is_section_banner(record: Any, records: Sequence[Any]) -> bool:
    """OCR が定型文と見出し・柱の再掲だけの Picture か。OCR が無い、または他の行がある画像は False (#899)。"""
    lines = _ocr_lines(record, records)
    if not lines:
        return False
    heading_keys = _heading_keys(record, records)
    return all(_is_boilerplate_line(line) or _repeats_heading(line, heading_keys) for line in lines)


def is_small_picture_record(record: Any) -> bool:
    """Picture record が装飾扱いに近い小領域かを判定します。"""
    bbox = _bbox(record)
    page_width = _finite_float(_record_value(record, "page_width"))
    page_height = _finite_float(_record_value(record, "page_height"))
    if not bbox or not page_width or not page_height:
        return False

    left, top, right, bottom = bbox
    width = abs(right - left)
    height = abs(bottom - top)
    if width <= 0 or height <= 0:
        return False

    area_ratio = (width * height) / max(1.0, page_width * page_height)
    width_ratio = width / max(1.0, page_width)
    height_ratio = height / max(1.0, page_height)
    ratio_small = (
        area_ratio <= SMALL_PICTURE_MAX_AREA_RATIO
        and width_ratio <= SMALL_PICTURE_MAX_WIDTH_RATIO
        and height_ratio <= SMALL_PICTURE_MAX_HEIGHT_RATIO
    )
    pixel_small = width <= SMALL_PICTURE_MAX_SIDE_PX and height <= SMALL_PICTURE_MAX_SIDE_PX
    return ratio_small or (pixel_small and area_ratio <= SMALL_PICTURE_MAX_AREA_RATIO * 2)


def mark_picture_record_role(record: Any, role: PictureRecordRole) -> None:
    """Picture role を record metadata に保存します。"""
    if role.role == CONTENT_VISUAL_ROLE:
        if role.reason == "meaningful_text_in_visual":
            raw = _ensure_raw(record)
            raw["visual_role"] = role.role
            raw["visual_role_reason"] = role.reason
            raw.pop("rag_excluded", None)
            raw.pop("vision_excluded_after_classification", None)
            raw.pop("merge_target_id", None)
            raw["vision_skipped"] = False
            raw["vision_status"] = "failed" if raw.get("vision_error") else "succeeded" if raw.get("vision_description") else "pending"
        return

    raw = _ensure_raw(record)
    raw["visual_role"] = role.role
    raw["visual_role_reason"] = role.reason
    # 実行済みの結果を crop 保存や chunking 時に「未実行」へ戻さないための不変条件。
    completed = bool(raw.get("vision_description"))
    raw["vision_skipped"] = role.skip_vlm and not completed and not raw.get("vision_error")
    if raw.get("vision_error"):
        raw["vision_status"] = "failed"
    elif completed:
        raw["vision_status"] = "succeeded"
    elif role.skip_vlm:
        raw["vision_status"] = "skipped"
    if role.merge_target_id:
        raw["merge_target_id"] = role.merge_target_id
    if role.exclude_from_rag:
        raw["rag_excluded"] = True


def visual_role_from_ref(ref: dict[str, Any]) -> str:
    """保存済み source ref から visual role を復元します。"""
    return str(ref.get("visual_role") or "").strip().lower()


def _related_operation_record_id(record: Any, records: Sequence[Any]) -> str:
    page = _int_value(_record_value(record, "page"))
    seq_no = _int_value(_record_value(record, "seq_no"))
    if page is None:
        return ""

    candidates: list[tuple[float, int, str]] = []
    for other in records:
        if other is record:
            continue
        if _int_value(_record_value(other, "page")) != page:
            continue
        # merge 先は地の文だけ。chunking で inline icon を child に取り込むのは地の文の branch だけなので、
        # Table や見出しを merge 先にすると icon がどの child にも入らず落ちる (#789)。
        if str(_record_value(other, "category") or "") in {
            "Picture",
            "Page-footer",
            "Page-header",
            "Section-header",
            "Table",
            "Title",
        }:
            continue
        text = _record_text(other)
        if not boilerplate_patterns().operation.search(text):
            continue
        other_id = str(_record_value(other, "id") or "")
        if not other_id:
            continue

        other_seq = _int_value(_record_value(other, "seq_no"))
        seq_distance = abs((seq_no or 0) - (other_seq or 0)) if seq_no is not None and other_seq is not None else 999
        vertical_distance = _vertical_distance(record, other)
        if seq_distance > NEARBY_TEXT_MAX_SEQ_DISTANCE and not _nearby_by_geometry(record, other, vertical_distance):
            continue
        candidates.append((vertical_distance, seq_distance, other_id))

    if not candidates:
        return ""
    return sorted(candidates, key=lambda item: (item[0], item[1], item[2]))[0][2]


def _nearby_by_geometry(record: Any, other: Any, vertical_distance: float) -> bool:
    page_height = _finite_float(_record_value(record, "page_height")) or _finite_float(_record_value(other, "page_height"))
    if not page_height:
        return False
    limit = max(NEARBY_TEXT_MAX_VERTICAL_PX, page_height * NEARBY_TEXT_MAX_VERTICAL_RATIO)
    return vertical_distance <= limit


def _vertical_distance(record: Any, other: Any) -> float:
    record_bbox = _bbox(record)
    other_bbox = _bbox(other)
    if not record_bbox or not other_bbox:
        return float("inf")
    _, top, _, bottom = sorted_box(record_bbox)
    _, other_top, _, other_bottom = sorted_box(other_bbox)
    if bottom < other_top:
        return other_top - bottom
    if other_bottom < top:
        return top - other_bottom
    return 0.0


def _is_near_page_edge(record: Any) -> bool:
    bbox = _bbox(record)
    page_height = _finite_float(_record_value(record, "page_height"))
    if not bbox or not page_height:
        return False
    _, top, _, bottom = sorted_box(bbox)
    center_y = (top + bottom) / 2
    return center_y <= page_height * PAGE_EDGE_BAND_RATIO or center_y >= page_height * (1 - PAGE_EDGE_BAND_RATIO)


def _looks_like_logo_or_boilerplate(record: Any) -> bool:
    values = [
        _record_value(record, "raw_type"),
        _record_text(record),
        _raw_value(record, "picture_kind"),
        _raw_value(record, "label"),
        _raw_value(record, "type"),
        _raw_value(record, "name"),
        _raw_value(record, "text"),
        _raw_value(record, "orig"),
    ]
    return any(boilerplate_patterns().logo.search(str(value or "")) for value in values)


def _has_explicit_decorative_type(record: Any) -> bool:
    """本文とは切り離された parser type が装飾を明示するかを判定します。"""
    values = [
        _record_value(record, "raw_type"),
        _raw_value(record, "picture_kind"),
        _raw_value(record, "visual_kind"),
        _raw_value(record, "visual_artifact_type"),
        _raw_value(record, "label"),
        _raw_value(record, "type"),
        _raw_value(record, "name"),
    ]
    return any(boilerplate_patterns().decorative_type.search(str(value or "")) for value in values)


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _record_text(record: Any) -> str:
    return str(_record_value(record, "text") or _record_value(record, "sentence") or "").strip()


def _raw_value(record: Any, key: str) -> Any:
    raw = _record_value(record, "raw")
    if isinstance(raw, dict):
        return raw.get(key)
    return None


def _ensure_raw(record: Any) -> dict[str, Any]:
    raw = _record_value(record, "raw")
    if not isinstance(raw, dict):
        raw = {}
        if isinstance(record, dict):
            record["raw"] = raw
        else:
            record.raw = raw
    return raw


def _normalized_visual_role(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {DECORATIVE_VISUAL_ROLE, "ignore", "ignored", "noise"}:
        return DECORATIVE_VISUAL_ROLE
    if normalized in {INLINE_ICON_VISUAL_ROLE, "inline-icon", "ui_icon", "ui-icon"}:
        return INLINE_ICON_VISUAL_ROLE
    return ""


def _bbox(record: Any) -> tuple[float, float, float, float] | None:
    value = _record_value(record, "bbox")
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
        return None
    return x1, y1, x2, y2


def sorted_box(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """bbox を左上・右下順に正規化して返します。"""
    x1, y1, x2, y2 = bbox
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
