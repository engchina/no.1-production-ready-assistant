# rag_pipeline_core

pipeline の段の決定論ロジックの共有パッケージ。backend の中で直接実行する(段のサービスへの HTTP の
委譲は持たない。#3218)。`rag_parser_core` と同じ軽量方針(依存は pydantic + rag_parser_core のみ)。

- `chunking.py` — chunk 分割の決定論ロジック(backend `app.rag.chunking` はここを再 export)。
- `vector_index.py` — 索引・検索の精度のプリセットの解決(`app.rag.vector_index_adapter`)。
- `guardrail.py` — groundedness の閾値の静的な解決(`app.rag.guardrail_adapter`)。
- `evaluation.py` — 評価の suite から閾値への解決。
- `graph.py` — 文書構造索引の構築のプロファイルの解決(`app.rag.graph_adapter`)。
- `raptor.py` — RAPTOR の再帰要約索引の決定論の部分(クラスタリングと summary chunk の構築)。
