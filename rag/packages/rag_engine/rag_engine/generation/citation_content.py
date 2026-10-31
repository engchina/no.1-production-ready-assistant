"""引用した原文が構造的な区切り線・書式だけかを検査する。意味支持は判定しない。"""

from rag_engine.parsing.structural_text import structural_only


def citation_content_unusable(quote: str) -> bool:
    """短い文字・数字・記号を一律に除かず、構造的な行だけの範囲を識別する。"""
    return structural_only(quote)
