"""PDF の印刷の頁番号（ページラベル）を読み、chunk の場所に付ける（#1244、handoff §7.2）。

前付き（i, ii, …）や章ごとの頁番号（2-1 など）を持つ PDF では、物理頁（何枚目か）と利用者が見る
頁番号が違う。PDF の `/PageLabels` を pypdfium2 で読み、物理頁と違うラベルがあるときだけ抽出の
`parser_artifacts.page_labels`（JSON。物理頁 → ラベル）に残し、chunk の metadata に
`page_label_start` / `page_label_end` を入れる。読めない PDF は何もしない（取込は止めない）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from rag_engine.parsing.rendering import PDFIUM_LOCK

logger = logging.getLogger(__name__)

PAGE_LABELS_ARTIFACT_KEY = "page_labels"
# 極端に頁の多い PDF で artifact を大きくしない。
MAX_LABELED_PAGES = 5000


def pdf_page_labels(pdf_bytes: bytes) -> dict[int, str]:
    """物理頁（1 始まり）→ 印刷の頁番号。物理頁と同じラベル・空のラベルは含めない。

    PDFium はスレッド安全ではないため、開く・読む・閉じるを process 全体の PDFium の lock の中で行う
    （#3362）。同期の処理なので、event loop からは ``asyncio.to_thread`` で呼ぶ。
    """
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except ImportError:
        return {}
    labels: dict[int, str] = {}
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(pdf_bytes)
        except Exception:  # noqa: BLE001 - 読めない PDF はラベル無しとして扱う
            return {}
        try:
            for index in range(min(len(document), MAX_LABELED_PAGES)):
                try:
                    label = (document.get_page_label(index) or "").strip()
                except Exception:  # noqa: BLE001 - その頁のラベルだけ諦める（ラベル無しとして扱う）
                    label = ""
                if label and label != str(index + 1):
                    labels[index + 1] = label
        finally:
            document.close()
    return labels


def page_labels_artifact(labels: Mapping[int, str]) -> str | None:
    """抽出の parser_artifacts に入れる JSON（ラベルが無ければ None）。"""
    if not labels:
        return None
    return json.dumps(
        {str(page): label for page, label in sorted(labels.items())}, ensure_ascii=False
    )


def labels_from_artifacts(parser_artifacts: Mapping[str, Any]) -> dict[int, str]:
    raw = parser_artifacts.get(PAGE_LABELS_ARTIFACT_KEY)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    labels: dict[int, str] = {}
    for key, value in payload.items():
        if str(key).isdigit() and isinstance(value, str) and value:
            labels[int(key)] = value
    return labels


def _page(metadata: Mapping[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def apply_page_labels(chunks: Sequence[Any], labels: Mapping[int, str]) -> None:
    """chunk の metadata（物理頁 page_start / page_end）に印刷の頁番号を足す。"""
    if not labels:
        return
    for chunk in chunks:
        metadata = chunk.metadata
        start = _page(metadata, "page_start") or _page(metadata, "page_number")
        if start is None:
            continue
        end = _page(metadata, "page_end") or start
        if start in labels or end in labels:
            metadata["page_label_start"] = labels.get(start, str(start))
            metadata["page_label_end"] = labels.get(end, str(end))


__all__ = [
    "PAGE_LABELS_ARTIFACT_KEY",
    "apply_page_labels",
    "labels_from_artifacts",
    "page_labels_artifact",
    "pdf_page_labels",
]
