"""チャンク分割。日本語テキストと章節構造を考慮した分割を行う。

実体は共有パッケージ ``rag_pipeline_core.chunking`` にあり、backend 内で直接実行する。
従来の import パス(`app.rag.chunking`)は本 re-export で維持する。
"""

from rag_pipeline_core.chunking import (
    CHUNKING_STRATEGIES,
    Chunk,
    ChunkMetadata,
    chunk_extraction,
    chunk_extraction_with_strategy,
    chunk_text,
    decode_fixed_delimiter,
)

__all__ = [
    "CHUNKING_STRATEGIES",
    "Chunk",
    "ChunkMetadata",
    "chunk_extraction",
    "chunk_extraction_with_strategy",
    "chunk_text",
    "decode_fixed_delimiter",
]
