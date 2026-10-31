"""文書の検索の中の工程の名前（処理の経過の段。#3436・#3443・#3464）。

埋め込み・並べ替えと同じく span だけを足し（``observe_call("stage", 名前)``）、工程の記録（回答の記録の文・
``execution_steps``）には書かない。名前は利用者向けの名詞（画面にそのまま出る）。rag_engine の検索の実装
（``adapters/oracle/store.py``）と、検索を差し替える実行環境（backend の検索）が同じ名前を使う。
"""

# DB の検索（検索文ごとの vector / keyword の検索）。
VECTOR_SEARCH_STAGE = "ベクトル検索"
TEXT_SEARCH_STAGE = "全文検索"
CHUNK_POOL_LOAD_STAGE = "検索対象のチャンクの読み込み"

# DB の検索の後に、候補から根拠を集める工程（backend の検索。#3464）。行うことの無い工程は段を出さない。
DERIVED_SOURCE_STAGE = "要約・抽出値の原文の読み込み"
CONTEXT_SIBLINGS_STAGE = "前後の文脈の読み込み"
REFERENCE_EXPANSION_STAGE = "参照先の根拠の追加"
ENTITY_EXPANSION_STAGE = "実体・別名による検索拡張"
GRAPH_EXPANSION_STAGE = "文書構造による検索拡張"
IMAGE_EVIDENCE_STAGE = "根拠の図の切り出し"
# 短い読み取り（文書の分類・文書の背景〔1 ページ目の本文〕）は 1 つの段にまとめる。
DOCUMENT_INFO_STAGE = "文書の分類と背景の読み込み"
