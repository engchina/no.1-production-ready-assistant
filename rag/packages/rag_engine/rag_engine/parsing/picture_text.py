"""図の中の文字（Picture OCR）の整形と、持ち主の図への結び付け。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rag_engine.parsing.content_labels import content_labels

# 旧い解析の結果に残る OCR の欄の名前（保存済みのデータの形。読むときだけ使う）。
LEGACY_PICTURE_OCR_TEXT_LABELS = ("Docling OCR",)

# 図の中の文字の record（Docling・Vision の解析が図と同じ bbox で作る）。図そのものではなく、
# 持ち主の図の文字として扱う（#3455）。
PICTURE_OCR_TEXT_RAW_TYPE = "picture_ocr_text"

# 図と OCR の record の bbox を同じ図とみなす誤差（解析の ``_paired_picture_ocr_text`` と同じ）。
PICTURE_BBOX_TOLERANCE = 1.0


def picture_ocr_text_label() -> str:
    """OCR の欄の名前（言語資源の content_labels.picture_ocr。#2181）。"""
    return content_labels().text("picture_ocr")


def format_docling_picture_ocr_text(text: str) -> str:
    """Picture OCR 原文を検索向けのラベル付き text に整形します。"""
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    prefix = f"{picture_ocr_text_label()}:"
    if normalized.startswith(prefix):
        return normalized
    for legacy_label in LEGACY_PICTURE_OCR_TEXT_LABELS:
        legacy_prefix = f"{legacy_label}:"
        if normalized.startswith(legacy_prefix):
            normalized = normalized[len(legacy_prefix) :].strip()
            break
    return f"{prefix}\n{normalized}"


def is_picture_ocr_text(value: Mapping[str, Any]) -> bool:
    """図の中の文字の record・ref・画像の entry か。"""
    return str(value.get("raw_type") or "") == PICTURE_OCR_TEXT_RAW_TYPE


def same_picture_bbox(left: Any, right: Any, *, tolerance: float = PICTURE_BBOX_TOLERANCE) -> bool:
    """2 つの bbox が誤差の中で同じ領域か（どちらかが不正なら False）。"""
    a, b = _bbox(left), _bbox(right)
    return a is not None and b is not None and all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def link_picture_ocr_records(records: Sequence[dict[str, Any]]) -> None:
    """OCR の record の ``raw`` に、持ち主の図の id（``picture_record_id``）と bbox（``picture_bbox``）を書きます。

    持ち主は、解析が残した明示の結び付き（``raw.source_picture_ref`` が図の record id か図の
    ``raw.self_ref``）を優先し、無ければ同じ engine・page で bbox が誤差の中で一致する図です。
    ``raw`` は置き換えるので、呼出元の record を複製してから渡します。
    """
    pictures: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for record in records:
        if str(record.get("category") or "") == "Picture" and not is_picture_ocr_text(record):
            pictures.setdefault((str(record.get("engine") or ""), record.get("page")), []).append(record)
    for record in records:
        if not is_picture_ocr_text(record):
            continue
        candidates = pictures.get((str(record.get("engine") or ""), record.get("page")), [])
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        link = str(raw.get("source_picture_ref") or "").strip()
        owner = next((p for p in candidates if link and link in {str(p.get("id") or ""), _raw_self_ref(p)}), None)
        if owner is None:
            owner = next((p for p in candidates if same_picture_bbox(p.get("bbox"), record.get("bbox"))), None)
        if owner is not None:
            record["raw"] = {**raw, "picture_record_id": str(owner.get("id") or ""), "picture_bbox": owner.get("bbox")}


def fold_picture_ocr_entries(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """画像の entry（chunk の ref・``image_evidence``）の OCR を、持ち主の図の entry にまとめます。

    持ち主の図の entry（``picture_record_id`` の id か、同じ page で bbox が一致する図）があれば、OCR の
    文字をその ``ocr_text`` に入れて OCR の entry を消します（同じ図を 2 回数えない）。無ければ OCR の
    page・bbox を図の領域にして、持ち主の図の id の図の entry に置き換えます。位置の無い OCR は図を
    決められないので消します。装飾などの除外は呼出元が結果に対して行います（図の役割は OCR にも写る）。
    """
    folded: list[dict[str, Any]] = [dict(entry) for entry in entries if not is_picture_ocr_text(entry)]
    for entry in entries:
        if not is_picture_ocr_text(entry):
            continue
        picture_id = str(entry.get("picture_record_id") or "").strip()
        text = str(entry.get("ocr_text") or entry.get("text_preview") or "").strip()
        owner = _owner_entry(folded, picture_id, entry)
        if owner is not None:
            if text and not str(owner.get("ocr_text") or "").strip():
                owner["ocr_text"] = text
            continue
        if _bbox(entry.get("bbox")) is None or not str(entry.get("page") or "").strip():
            continue
        image_id = picture_id or str(entry.get("image_id") or entry.get("record_id") or "").strip()
        folded.append({**entry, "raw_type": "picture", "image_id": image_id, "record_id": image_id, "text_preview": "", "ocr_text": text})
    return folded


def _owner_entry(entries: Sequence[dict[str, Any]], picture_id: str, ocr: Mapping[str, Any]) -> dict[str, Any] | None:
    if picture_id:
        for item in entries:
            if picture_id in {str(item.get("image_id") or ""), str(item.get("record_id") or "")}:
                return item
    for item in entries:
        # 表自身の切り出し（raw_type=table）は図ではない。
        if str(item.get("raw_type") or "") == "table":
            continue
        if str(item.get("page") or "") == str(ocr.get("page") or "") and same_picture_bbox(item.get("bbox"), ocr.get("bbox")):
            return item
    return None


def _raw_self_ref(record: Mapping[str, Any]) -> str:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    return str(raw.get("self_ref") or "").strip()


def _bbox(values: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(values, list | tuple) or len(values) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in values)
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1
