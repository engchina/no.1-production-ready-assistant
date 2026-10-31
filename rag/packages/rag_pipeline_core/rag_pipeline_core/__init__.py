"""rag_pipeline_core: pipeline の段の決定論ロジック。

文書分割(chunking)・vector index・guardrail・evaluation・文書構造索引の構築(graph)・RAPTOR の設定の
解決を、backend の中で実行する決定論ロジックとして提供する(段のサービスへの委譲は持たない。#3218)。
``rag_parser_core`` と同じく軽量(依存は pydantic + rag_parser_core のみ)。
"""
