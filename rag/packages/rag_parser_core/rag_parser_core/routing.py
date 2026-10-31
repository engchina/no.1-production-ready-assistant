"""Parser adapter の source-aware routing 定義。"""

from __future__ import annotations

from typing import Literal

from rag_parser_core.capabilities import ADAPTER_CAPABILITIES
from rag_parser_core.source import SourceModality

ParserAdapterRouteBackend = Literal[
    "docling",
    "unstructured",
]
ParserAdapterSourceKind = Literal[
    "pdf",
    "image",
    "office",
    "html",
    "email",
    "text",
    "unknown",
]
AdapterOrderBySourceKind = dict[
    ParserAdapterSourceKind,
    tuple[ParserAdapterRouteBackend, ...],
]

SOURCE_ROUTE_KINDS: tuple[ParserAdapterSourceKind, ...] = (
    "pdf",
    "image",
    "office",
    "html",
    "email",
    "text",
    "unknown",
)

# source kind ごとの外部 adapter の優先順。未導入時は readiness が missing として fallback する
# ため、順序は導入後に効く。
# ここは「順番」だけを持ち、対応形式は持たない。実際の候補は、対応形式の正本
# (capabilities.ADAPTER_CAPABILITIES)がその source kind を宣言する backend だけに絞る(#366)。
# 例: Docling の parser サービスは PDF と画像だけを解析するため、office / html / unknown の
# 候補に入らない。
_ADAPTER_PREFERENCE_BY_SOURCE_KIND: AdapterOrderBySourceKind = {
    "pdf": ("docling", "unstructured"),
    "image": ("unstructured", "docling"),
    "office": ("docling", "unstructured"),
    "html": ("docling", "unstructured"),
    "email": ("unstructured",),
    "text": (),
    "unknown": ("unstructured", "docling"),
}


def _backend_supports_source_kind(
    backend: ParserAdapterRouteBackend,
    source_kind: ParserAdapterSourceKind,
) -> bool:
    capability = ADAPTER_CAPABILITIES.get(backend)
    return capability is not None and SourceModality(source_kind) in capability.modalities


ADAPTER_ORDER_BY_SOURCE_KIND: AdapterOrderBySourceKind = {
    source_kind: tuple(
        backend for backend in order if _backend_supports_source_kind(backend, source_kind)
    )
    for source_kind, order in _ADAPTER_PREFERENCE_BY_SOURCE_KIND.items()
}


def normalize_source_kind(value: object) -> ParserAdapterSourceKind:
    """manifest modality / runtime source label を routing 用の低 cardinality へ寄せる。"""
    normalized = str(value or "").strip().casefold()
    if normalized in {"pdf"}:
        return "pdf"
    if normalized in {"image", "ocr", "scan", "scanned_image"}:
        return "image"
    if normalized in {"office", "docx", "pptx", "xlsx", "word", "powerpoint", "excel"}:
        return "office"
    if normalized in {"html", "xhtml", "web"}:
        return "html"
    if normalized in {"email", "eml", "message"}:
        return "email"
    if normalized in {"text", "markdown", "md", "csv", "tsv", "json", "jsonl", "ndjson"}:
        return "text"
    return "unknown"


def adapter_order_for_source_kind(
    source_kind: object,
) -> tuple[ParserAdapterRouteBackend, ...]:
    """source kind に対する外部 adapter 候補順を返す。"""
    return ADAPTER_ORDER_BY_SOURCE_KIND[normalize_source_kind(source_kind)]
