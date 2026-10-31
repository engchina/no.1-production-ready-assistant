# Reference RAG / NL2SQL Project Catalog

この catalog は `AGENTS.md` の参照先であり、新機能設計時の比較検討メモを置く。
優れた考え方は取り込むが、実装は必ず本リポジトリの確定スタックへ再マッピングする。

## Product Foundation

- Palantir Foundry Ontology
  - 参考点: 業務現実を Object Type / Property / Link Type として表し、物理テーブルの写像に閉じない。
  - 本プロジェクトでの採用: 共有 `BusinessOntology`、物理 `SchemaOntology`、問い合わせごとの `QueryOntologyTrace` を分離する。
  - 導入しないもの: Palantir 固有 runtime、外部 graph DB、Foundry 依存の権限モデル。

## GraphRAG / Business Graph

- Oracle Schema Discovery Agent for NL2SQL AI
  - 参考点: schema discovery、関係 path、説明 metadata、利用者による確認を NL2SQL の前段に置く。
  - 本プロジェクトでの採用: Oracle catalog から owner-qualified table/view/column/FK/view lineage を取り込み、published ontology revision と profile view に固定する。
  - 導入しないもの: 別 LLM provider、外部 vector DB、未確認 schema metadata による自動実行。

- Microsoft GraphRAG / LightRAG 系
  - 参考点: entity/relation 抽出、graph traversal、query-focused subgraph selection。
  - 本プロジェクトでの採用: profile scope 内の承認済み edge だけを有界探索し、SQL 生成 context に渡す。
  - 導入しないもの: Neo4j などの外部 graph DB。永続化は Oracle AI Database に集約する。

## Vector Search

- Oracle AI Vector Search
  - 参考点: relational metadata と vector index を同一 Oracle DB 内に置き、権限・監査・検索を統合する。
  - 本プロジェクトでの採用: ontology node embedding は `VECTOR(1536, FLOAT32)` に保存し、`VECTOR_DISTANCE(..., COSINE)` で profile-scoped retrieval を行う。
  - 導入しないもの: pgvector、Qdrant、Milvus、Pinecone などの外部 vector DB。

## Agentic / Query Planning

- Agentic NL2SQL planners
  - 参考点: 質問解釈、曖昧性提示、実行前確認、再生成の状態機械。
  - 本プロジェクトでの採用: `interpreting → awaiting_intent_confirmation → generating_sql → awaiting_sql_confirmation → executing` の二段階確認。
  - 導入しないもの: 自律的な未確認 SQL 実行、未承認 join の推測実行。

## Evaluation / Guardrails

- Deterministic SQL AST validation
  - 参考点: LLM の説明ではなく AST と schema mapping で検証する。
  - 本プロジェクトでの採用: `sqlglot` Oracle dialect で SQL semantic graph を作り、intent graph / profile view と三方照合する。
  - 導入しないもの: LLM judge だけに依存した SQL 正誤判定。
