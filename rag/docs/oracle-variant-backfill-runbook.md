# RAG 構築 variant migration / backfill runbook

最終更新: 2026-06-22

この runbook は、V3 で追加した構築 artifact を Oracle AI Database 環境へ安全に反映するための運用手順です。対象は `rag_chunk_sets`、`rag_document_extractions`、`rag_artifact_layers`、`rag_kb_chunk_set_bindings`、および `rag_chunks.chunk_set_id` です。

方針は安全側です。CLI が生成する SQL は read-only の検証だけに限定し、既存データの回填は staging でレビュー済みの手順として実行します。

## 1. 成果物を生成する

backend container または CI runner で、migration SQL、manifest、検証 SQL を生成します。

```bash
uv run python -m app.rag.oracle_schema --migration \
  --output ../artifacts/oracle-schema-migration.sql \
  --manifest-output ../artifacts/oracle-schema-migration.manifest.json

uv run python -m app.rag.variant_backfill_cli --format sql --checks-only \
  --output ../artifacts/variant-backfill-checks.sql

uv run python -m app.rag.variant_backfill_cli --format json \
  --output ../artifacts/variant-backfill.manifest.json
```

受け入れ条件:

- `oracle-schema-migration.manifest.json` の hash をレビュー済み artifact として保存する。
- `variant-backfill-checks.sql` に書き込み文が含まれない。
- RAG repo に SQL 専用機能の公開面が復活していない。

## 2. schema migration を適用する

maintenance window を取り、レビュー済みの migration SQL を SQLcl などの管理手順で適用します。

```bash
sqlcl @../artifacts/oracle-schema-migration.sql
```

適用直後に `variant-backfill-checks.sql` を実行します。まず次の check が 0 であることを確認します。

- `required_variant_tables_missing`
- `required_variant_columns_missing`

ここで 0 にならない場合は、構築 artifact の回填へ進まず migration を修正します。

## 3. 既存文書を構築 artifact に紐付ける

既存の indexed 文書は、安全側の初期状態として「既存 chunk 集合を 1 つの chunk_set として扱う」形へ寄せます。

回填で行うこと:

- document の `source_sha256` と当時の有効な構築設定から `extraction_recipe_id` を算出する。
- 既存 chunk 集合に対応する `chunk_set_id` を算出する。
- `rag_document_extractions` に `status='materialized'` の extraction artifact を作る。
- `rag_chunk_sets` に indexed chunk_set を作る。
- 既存 `rag_chunks` へ `chunk_set_id` を紐付ける。
- KB membership ごとに `rag_kb_chunk_set_bindings` の `is_serving=1` を作る。
- parser / preprocess 差分があり、原本 bytes から再抽出できない文書は `needs_reingest` として分離する。

回填後、次の check が 0 であることを確認します。

- `indexed_chunks_without_chunk_set`
- `chunk_sets_missing_extraction_recipe`
- `chunk_sets_missing_extraction_artifact`
- `indexed_chunk_sets_without_materialized_extraction`
- `kb_memberships_without_serving_chunk_set`

## 4. 検索配信を検証する

Search Answer Profile 検索が KB の serving chunk_set だけを使っていることを staging で確認します。

```bash
uv run pytest tests/test_search_api.py tests/test_knowledge_bases_api.py tests/test_search_answer_profiles_api.py -q
```

staging では代表的な Search Answer Profile で検索し、diagnostics を artifact として保存します。設定解決順は `request 明示 > Search Answer Profile > global defaults` です。KB に残る legacy query 設定は検索に使いません。

次の check が 0 であることを確認します。

- `serving_bindings_without_indexed_chunk_set`
- `chunks_referencing_missing_chunk_set`
- `indexed_chunk_sets_without_chunks`

## 5. 派生 layer の状態を記録する

`GET /api/documents/{id}/chunk-sets` で、metadata / graph / navigation の状態を sampling します。

状態の扱い:

- `materialized`: 実 artifact が存在し、検索または表示に使える。
- `planned_only`: 設定として要求されているが、builder が未接続または未生成。
- `needs_reingest`: 現在の構築設定では再取込が必要。
- `error`: 生成に失敗した。
- `not_requested`: その layer は要求されていない。

GraphRAG の builder が未接続の環境では、`planned_only` が残ること自体は許容できます。navigation は保存済み抽出結果から章節 tree を作れる場合だけ `materialized` になり、章節構造が無い文書では `planned_only` のままです。いずれの場合も、完了表示にしてはいけない状態は運用メモに件数と対象 layer を残します。

次の check を確認します。

- `requested_layers_needing_action` は 0。
- `requested_layers_planned_only` は、残る場合に件数と理由が説明されている。

## 6. rollback / 停止条件

次のいずれかに該当する場合は Search Answer Profile の本番検索配信へ進みません。

- required table / column check が 0 でない。
- indexed chunk に `chunk_set_id` が無い。
- serving binding が indexed chunk_set を指していない。
- extraction artifact が `materialized` でない indexed chunk_set がある。
- `needs_reingest` または `error` の layer を完了扱いしている。

schema migration 後に application rollback する場合でも、追加 table / column は残して構いません。旧 application は `chunk_set_id` を無視できます。ただし、新 application へ戻す前に validation SQL を再実行し、回填の途中状態が検索配信に混ざらないことを確認します。
