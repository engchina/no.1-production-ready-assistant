"""設定に基づいて有効なレイアウト解析アダプターを組み立てる。"""

from __future__ import annotations

from rag_engine.adapters.parsers.base import LayoutAdapter
from rag_engine.adapters.parsers.docling_adapter import DoclingAdapter
from rag_engine.config import Settings

ENGINE_ORDER = [
    "docling",
]

ENGINE_LABELS = {
    "docling": "Docling",
}


def build_adapters(settings: Settings) -> dict[str, LayoutAdapter]:
    """adaptersを構築します。"""
    adapters: dict[str, LayoutAdapter] = {
        "docling": DoclingAdapter(settings),
    }
    enabled = set(ENGINE_ORDER if "all" in settings.enabled_engines else settings.enabled_engines)
    return {engine_id: adapters[engine_id] for engine_id in ENGINE_ORDER if engine_id in enabled}
