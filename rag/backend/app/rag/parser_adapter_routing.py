"""Parser adapter の source-aware routing 定義(re-export shim)。

正本は共有 package `rag_parser_core.routing`。backend は本モジュール経由で
従来の import パス(`app.rag.parser_adapter_routing`)を維持する。
"""

from rag_parser_core.routing import (
    ADAPTER_ORDER_BY_SOURCE_KIND,
    SOURCE_ROUTE_KINDS,
    AdapterOrderBySourceKind,
    ParserAdapterRouteBackend,
    ParserAdapterSourceKind,
    adapter_order_for_source_kind,
    normalize_source_kind,
)

__all__ = [
    "ADAPTER_ORDER_BY_SOURCE_KIND",
    "SOURCE_ROUTE_KINDS",
    "AdapterOrderBySourceKind",
    "ParserAdapterRouteBackend",
    "ParserAdapterSourceKind",
    "adapter_order_for_source_kind",
    "normalize_source_kind",
]
