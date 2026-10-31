# 取込チャンク構成 materialization — staging 引き継ぎ

> 「1 文書 × N 構築レシピ(複数チャンク構成)を同時保持・共有」設計の残作業を、**実 Oracle AI Database が
> 使える staging 環境**で進めるための引き継ぎ。本リポジトリの CI は実 Oracle 依存(実 DB なしでは
> 検証不能)のため、DDL・永続化・GC 実行はここで配線・検証する。設計の正本はメモリ
> `multi-recipe-variants-decision` と本リポジトリの `docs/` 各設計書。

最終更新: 2026-06-22 / 対象ブランチ: RAG V3 cleanup

> V3 状態: chunk_set / extraction_recipe / 派生 layer の永続状態は実装済み。
> 本文の Phase A 記述は設計経緯として残し、現在の残作業は GraphRAG / navigation など
> 個別 layer builder の実体化拡張と staging 運用検証に絞る。

---

## 1. 前提:すでに完了している基盤(この上に積む)

決定論で CI 検証済みの「頭脳」が揃っている。**新規に keying / dedup / GC 計算ロジックを書かない**。
以下を呼び出して永続化(「手」)だけを足す。

| 既存資産 | 役割 | 場所 |
|---|---|---|
| `variant_keys` | 層別 keying。`compute_extraction_recipe_id(source_sha256, settings)` / `compute_chunk_set_id(...)` / `compute_layer_ids(...)`。抽出 recipe → `er_*`、chunk 軸 → `cs_*`、派生層 → `gr_*`/`md_*`/`nv_*`。 | [variant_keys.py](../backend/app/rag/variant_keys.py) |
| `variant_planner` | dedup/refcount/GC の計画。`plan_document_materializations(source, global_settings, kb_configs)` → `MaterializationPlan`(層 ID→参照 KB 群=refcount、chunk_set→extraction_recipe)。`diff_plan(existing_ids, plan)` → `to_create` / `to_collect`(GC)。 | [variant_planner.py](../backend/app/rag/variant_planner.py) |
| KB 取込上書き | `_INGESTION_FIELD_MAP` に preprocess/parser/vision/chunking(+params)/graph/field/nav(図表 VLM 要約の asset は #497 で Vision に統合)。`apply_adapter_config_or_global(scope="ingestion")` で effective 取込 settings を解決。 | [kb_adapter_config.py](../backend/app/rag/kb_adapter_config.py) |
| query per-field merge | **配線済み**(`compose_query_settings` + `resolve_search_answer_profile_settings(kb_query=…)`)。query 側はこの引き継ぎ範囲外。 | [search.py](../backend/app/api/routes/search.py) |

ユニットテスト済み:`tests/test_variant_keys.py` / `tests/test_variant_planner.py` / `tests/test_kb_adapter_config.py`。
**staging でもこれらは回し、回帰の番人にする。**

---

## 2. 不変条件(壊してはいけない)

1. **Camp B**: 文書↔KB は N:N、chunk は共有(不要に複製しない)、**embedding は OCI Cohere v4 / 1536 グローバル固定**。
   チャンク構成は **同一 1536 空間内**で chunk 集合を増やすだけ。per-KB embedding / per-KB 物理表(Camp A)にしない。
2. **確定スタック**: OCI Enterprise AI / OCI GenAI Cohere / Oracle AI Database。外部ベクトル DB・別 LLM provider を入れない。
3. **VECTOR 索引は単一・共有**: `rag_chunks.embedding` の HNSW は 1 本のまま。`chunk_set_id` は**フィルタ列**であって索引を分割しない。
4. **版管理 DDL は自動変更しない**: スキーマ追加は DDL artifact(下記)へ明示追加し、`requires_reprovision` 扱い。
5. **2 段階処理**: parse → 人手プレビュー確認 → index。チャンク構成の materialize は **index 段**に入る。同じ extraction recipe だけ parse artifact を共有・再利用する。

---

## 3. 設計経緯(staging Phase A → C)

### Phase A — chunk_set レベルの共有 + GC(最大の御利益)

派生層(graph/nav)は後回しにし、まず **chunk text + embedding の共有**を実装する。
V3 時点ではこの Phase A は完了済み。

**A-1. DDL 追加**(版管理 artifact へ)
- 生成箇所: 新規テーブルは `app/clients/oracle.py` の `oracle_*_schema_sql()` に倣って追加し、
  `app/rag/oracle_schema.py` の `oracle_schema_sections()` に登録。既存テーブルへの列追加は
  `oracle_schema_migration_sections()`(ALTER)へ。`oracle_schema_manifest()` の決定論テストで artifact 内容を固定。
- 追加テーブル `rag_chunk_sets`(chunk text/embedding 層。refcount は binding から導出するので列に持たない):

```sql
CREATE TABLE rag_chunk_sets (
    chunk_set_id    VARCHAR2(64) PRIMARY KEY,   -- = compute_chunk_set_id(...) "cs_..."
    document_id     VARCHAR2(64) NOT NULL,
    tenant_id_hash  CHAR(64),
    recipe_subset   JSON,                        -- chunk 軸の値(監査/デバッグ用)
    status          VARCHAR2(32) DEFAULT 'INGESTING' NOT NULL,
    chunk_count     NUMBER(10) DEFAULT 0 NOT NULL,
    vector_count    NUMBER(10) DEFAULT 0 NOT NULL,
    metrics_json    JSON,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_chunk_sets_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE,
    CONSTRAINT rag_chunk_sets_status_ck
        CHECK (status IN ('INGESTING','INDEXED','ERROR'))
);
CREATE INDEX rag_chunk_sets_document_idx ON rag_chunk_sets (document_id, status);
```

- `rag_chunks` に列追加(migration):`ALTER TABLE rag_chunks ADD chunk_set_id VARCHAR2(64);`
  既存 [oracle_vector_schema_sql](../backend/app/clients/oracle.py) の `rag_chunks` 定義にも `chunk_set_id` を入れ、
  索引 `CREATE INDEX rag_chunks_chunk_set_idx ON rag_chunks (chunk_set_id, chunk_index);` を追加。
- KB→chunk_set の参照(refcount の実体・配信フラグ):

```sql
CREATE TABLE rag_kb_chunk_set_bindings (
    knowledge_base_id VARCHAR2(64) NOT NULL,
    document_id       VARCHAR2(64) NOT NULL,
    chunk_set_id      VARCHAR2(64) NOT NULL,
    tenant_id_hash    CHAR(64),
    is_serving        NUMBER(1) DEFAULT 1 NOT NULL,   -- KB の既定 variant(検索配信元)
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    PRIMARY KEY (knowledge_base_id, document_id, chunk_set_id),
    CONSTRAINT rag_kb_cs_bind_cs_fk
        FOREIGN KEY (chunk_set_id) REFERENCES rag_chunk_sets (chunk_set_id) ON DELETE CASCADE
);
CREATE INDEX rag_kb_cs_bind_cs_idx ON rag_kb_chunk_set_bindings (chunk_set_id);
```
- **refcount = `SELECT COUNT(*) FROM rag_kb_chunk_set_bindings WHERE chunk_set_id = :id`**(列で持たず導出=drift しない)。

> ⚠️ 上記 DDL は **本リポジトリのスタイルに合わせた素案**。Oracle AI Database 構文・制約名長(30 byte)・
> VECTOR 索引との相互作用を実 DB で必ず検証してから artifact 確定すること。

**A-2. 取込フローを planner 駆動へ**(owning-KB footgun の解消)
- 現状: [documents.py `_resolve_ingestion_settings`](../backend/app/api/routes/documents.py) は
  `OracleClient.get_owning_knowledge_base`(最古割当 KB)1 枚で effective settings を決める
  → **共有文書で他 KB の取込設定が無視される**。これを置き換える。
- 新フロー(index 段):
  1. 文書の所属 KB 群 + 各 KB の `adapter_config` を取得(`list_document_knowledge_bases` を adapter_config 込みへ)。
  2. `plan_document_materializations(source_sha256, global_settings, {kb_id: adapter_config})` → `MaterializationPlan`。
  3. 既存 chunk_set 群(`rag_chunk_sets` の当該 document)と `diff_plan` → `to_create` / `to_collect`。
  4. `to_create` の各 chunk_set を、その chunk_set_id を生んだ effective settings で materialize。
     同じ extraction_recipe の chunk_set は保存済み extraction を共有し、chunking だけを変えて再索引する。
     extraction_recipe が違う場合は原本 bytes がある取込経路で extract+index を実行する。
     review-only 経路で原本再抽出できない場合は再取込要求として止める。**上限**(例 ≤4 chunk_set/doc)+ 追加 embedding コスト preview。
  5. `rag_kb_chunk_set_bindings` を計画どおり upsert(KB→chunk_set)。`is_serving` は KB 既定を 1 件。
  6. `to_collect`(refcount 0)を GC(下記)。
- 既存 1 chunk_set 文書の **backfill**: 既存 `rag_chunks` に、現 owning-KB effective settings から計算した
  `chunk_set_id` を後埋め + binding 作成(全 KB を当面その 1 chunk_set に向ける)。移行は安全側(全文書を既存集合に紐付け)。

**A-3. GC 実行**(最重要・データ消失リスク)
- `diff_plan(...).to_collect` の chunk_set だけを削除。**必ず refcount 0 を DB でも再確認してから**
  `DELETE FROM rag_chunks WHERE chunk_set_id=:id` → `DELETE FROM rag_chunk_sets WHERE chunk_set_id=:id`。
- KB を文書から外す / KB 設定変更で参照が変わったときも同じ diff→GC を通す。
- **早すぎる削除 = 他 KB のデータ消失**。binding の FK(ON DELETE CASCADE)に頼り切らず、削除前 refcount 検査を必須に。

**A-4. 検索の variant フィルタ**
- retrieval SQL に `chunk_set_id` 条件を追加。既定は KB の `is_serving=1` の chunk_set のみ
  (`global < KB 既定 variant < … ` の解決順、`multi-recipe-variants-decision` 参照)。
- citation metadata に `chunk_set_id`(variant バッジ用)を低機密で付与。

### Phase B — 派生層の層別共有(graph / nav / metadata)
- `compute_graph_layer_id` / `compute_nav_layer_id` / `compute_metadata_layer_id` を使い、
  chunk_set を共有したまま graph/nav/metadata だけ別 artifact に。`rag_artifact_layers`(layer_id, layer_kind,
  parent_chunk_set_id, …)+ `rag_kb_layer_bindings` で同型に refcount/GC。`variant_planner.MaterializationPlan` は
  既に `graph_layers` / `nav_layers` / `metadata_layers` を返すのでそのまま流用。

### Phase C — 配信モード(検索・回答プロファイル層)
- **実装済み(配信モードは持たない)**: 文書の全 active レシピを常に RRF 融合する。配信モードの設定
  (`rag_serving_mode` / 検索・回答プロファイルの `serving_mode`)は runtime で使っていなかったため #1331 で削除した。
  `routed` は作らない。
- 二重ヒットの除去は storage dedup とは別問題なので、回答に渡す根拠の選択(`rag_engine` の `evidence_spans`)で行う
  (#1331)。同じ文書で要素の ID(`element_ids`)が小さい側の 8 割以上重なり、短い側の本文の 9 割以上がもう一方に
  含まれる根拠を 1 つにまとめ、まとめた chunk_id は `source_aliases` に残す。要素の ID はレシピごとの解析の結果の
  番号で、解析の設定が違うと同じ ID が別の箇所を指すため、本文でも確かめる。page / bbox だけでの判定はしない
  (要素の ID の無い分割は、本文が同じ根拠だけをまとめる)。

---

## 4. VLM parse の非決定論対策(全 Phase 共通)
- OCI Enterprise AI VLM parse は再実行で出力が揺れる。「両方走らせて同一出力を dedup」は前提が崩れる。
- **`hash(source_sha256, parse 影響 sub-config)` で parse artifact をキャッシュ**し、同 chunk_set_id の再 materialize は
  **走らせ直さずキャッシュ再利用**。Object Storage の既存 segment artifact 機構を流用候補。

---

## 5. 検証計画
- **CI(実 Oracle なし)**: `variant_keys` / `variant_planner` / `kb_adapter_config` / `compose_query_settings` の
  決定論ユニットは緑を維持。DDL artifact は `oracle_schema_manifest` の決定論テストで内容固定。
- **staging(実 Oracle AI Database)**: 新テーブル/列適用 → 取込→共有→GC の統合テスト。`docs/evaluation` /
  `file_processing_staging` の既存ゲート(retrieval recall / table QA / page hit / ingestion p95)に、
  **chunk_set 共有時に検索品質が劣化しない**こと、**GC が他 KB chunk を消さない**ことを追加。
- 方針は `docs/pipeline-advanced-strategies.md` の **scaffold(safe-degrade)→ 実配線 → staging 検証** を踏襲。
  未配線時・失敗時は **従来の単一 chunk_set 挙動へ安全縮退**(既定挙動・レイテンシを変えない)。

---

## 6. 受け入れ基準
- 同一取込設定の複数 KB に属する文書 → chunk_set を**共有**(複製ゼロ・refcount=KB 数)。
- chunk 軸が違う KB → **別 chunk_set**(差分複製)、各 KB は自分の chunk_set を検索。
- KB を 1 つ外しても、他 KB 参照中の chunk_set は **GC されない**。参照 0 で初めて GC。
- embedding は 1536 固定・索引は単一のまま。検索品質ゲートが劣化しない。
- VLM parse はキャッシュ再利用で二重実行しない。
- すべて確定スタック内・外部ベクトル DB なし。決定論ユニットは CI 緑のまま。

---

## 7. 着手順の推奨
A-1(DDL)→ A-4(検索フィルタ・既定 is_serving のみ)→ A-2(取込 planner 駆動 + backfill)→ A-3(GC)→ B → C。
A だけで「1 文書 × 複数 chunk_set の共有」という中核価値が出る。B/C は opt-in 拡張。

## 8. 実装進捗(2026-06-22)
- **A-1 DDL 適用済**(`79152e7`)、**A-2 永続化メソッド**(`a0affb5`)、**A-2 取込 reconcile 配線**(`490b015`)、**A-4 検索 chunk_set フィルタ**(`53eb9d9`)。
  単一 materialization(所属 KB 全部を 1 chunk_set に bind)で「取込→記録→検索 is_serving 尊重」が実 DB で一貫動作。
- **V2 で追加済み**: `extraction_recipe_id` と `chunk_set_id` を分離。chunking 差分は extraction を共有し、preprocess/parser 差分は原本 bytes 経路で再抽出、review-only では再取込要求にする。
- **V3 で追加済み**: `rag_document_extractions` / `rag_artifact_layers` に extraction と layer 状態を永続化。`GET /api/documents/{id}/chunk-sets` は `extraction_recipe_id`、`extraction_status`、metadata / graph / navigation の `planned_only` / `materialized` / `needs_reingest` などの実状態を返す。
- **V3 で除去済み**: RAG repo の Select AI 公開面(`/api/search/select-ai`、frontend panel/API/hook、設定値、schema)を削除。SQL 専用能力は sibling repo `../no.1-production-ready-nl2sql` の責務。
- **V4-0 で追加済み**: [oracle-variant-backfill-runbook.md](./oracle-variant-backfill-runbook.md) と `app.rag.variant_backfill_cli` により、schema migration 後の read-only validation SQL / JSON manifest / Markdown runbook を生成できる。実データの回填は staging でレビュー済み手順として実行し、CLI は production data を変更しない。
- **V4-1 で追加済み**: navigation layer は保存済み extraction から決定論的に章節 tree を作れる場合、`materialized` として永続化し `navigation_node_count` を metrics に残す。
- **残り = 個別 layer builder の拡張**。metadata は保存済み extraction に実 payload があれば `materialized`、GraphRAG は実 builder が未接続なら `planned_only` と表示する。

### per-recipe 複数 materialization の実装計画(コード調査済・要注意の深い変更)
**重要な前提**: parse(`StructuredExtraction`)は **extract 相で実行され文書に保存**、index 相([`ingestion._run_index_phase`](../backend/app/rag/ingestion.py))が再利用する。
→ **chunking 軸だけ違う chunk_set は re-parse 不要**(同一 extraction を別 chunking で再 chunk するだけ)。preprocess/parser 軸が分岐する場合は別 extraction が必要で、V2 では原本 bytes がある取込経路のみ再抽出し、review-only 経路では 409/re-ingest warning として止める。

**解消済み**: チャンクストアは `chunk_set_id` 指定時に対象 chunk_set だけを差し替える。
これにより同一文書の複数 chunk_set が共存できる。

**必要な改修**:
1. `save_index` / `_save_index_with_oracle` / `_chunk_insert_rows` に `chunk_set_id: str | None = None` を通す。`None` のとき現行どおり(全削除・NULL タグ)で**完全後方互換**。指定時は DELETE を `AND chunk_set_id = :chunk_set_id` に scope し、INSERT 列に `chunk_set_id` を含めて**挿入時タグ付け**(現 reconcile の事後 UPDATE タグは不要に)。
2. `_run_index_phase` を「1 chunk_set 分の chunk→embed→save」と「文書 finalize(status/audit)」に分け、**所属 KB を extraction_recipe → chunk_set_id でグルーピングした plan**(`variant_planner.plan_document_materializations`)に従う。同一 extraction recipe 内では保存済み extraction を再利用し、recipe が違う場合は原本から再抽出できる経路だけ materialize する。各 KB グループを binding、stale を GC。
3. 検証: 実 Oracle で「同一文書を chunk_size 違いの 2 KB が参照 → chunk_set 2 つ共存、各 KB スコープ検索が自分の chunk_set だけを取得(§A-4 フィルタが効く)」を `oracle_db` fixture で。embedding は決定論スタブ。
- 着手は専用セッション推奨(チャンクストア hot path・破壊的リスク)。`chunk_set_id=None` 既定で段階導入し、既存の `test_two_phase_review` / `test_oracle_chunk_set_adapter` を回帰の番人にする。
