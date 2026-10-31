"""文書解析・検索・根拠付き回答を組み合わせる RAG エンジン（rag_engine）。"""

__version__ = "0.2.0"

_EXPORTS = {
    "ChunkingService": ("rag_engine.chunking.service", "ChunkingService"),
    "ParsingService": ("rag_engine.parsing.service", "ParsingService"),
    "IndexingService": ("rag_engine.indexing", "IndexingService"),
    "RetrievalService": ("rag_engine.retrieval.service", "RetrievalService"),
    "GenerationService": ("rag_engine.generation.service", "GenerationService"),
    "IngestionPipeline": ("rag_engine.workflows", "IngestionPipeline"),
    "QueryPipeline": ("rag_engine.workflows", "QueryPipeline"),
    "create_oracle_application": ("rag_engine.composition", "create_oracle_application"),
    "AnswerRequest": ("rag_engine.generation.application", "AnswerRequest"),
}
__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str):
    """公開サービスを必要時にだけ読み込み、import による外部初期化を避ける。"""
    if name not in _EXPORTS:
        raise AttributeError(name)
    from importlib import import_module

    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module), symbol)
    globals()[name] = value
    return value
