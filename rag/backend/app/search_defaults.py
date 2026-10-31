"""検索する件数の既定と、リクエストを検証するときの設定解決。"""

from typing import Any

SEARCH_TOP_K_DEFAULT = 50
SEARCH_TOP_K_MAX = 100


def search_top_k_default() -> int:
    """import 時に固定せず、現在の設定から省略時の値を得る。"""
    from app.config import get_settings

    return get_settings().rag_search_top_k_default


def search_top_k_schema(schema: dict[str, Any]) -> None:
    """公開する契約にも現在の既定を示す。"""
    schema["default"] = search_top_k_default()
