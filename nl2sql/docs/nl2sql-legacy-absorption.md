# no.1-sql-assist 旧版機能吸収メモ

## 追加された管理 API

主要な旧版吸収 API は `/api/nl2sql` 配下に集約する。

- `POST /classifier/training-data/import`: CSV/XLSX の `CATEGORY,TEXT` または `CATEGORY,QUESTION` を取り込む。
- `POST /classifier/train`: 取り込み済みデータを埋め込みモデルで 1024 次元のベクトルに変換し、LogisticRegression を学習する。
- `POST /classifier/predict`: 学習済み分類器で profile category を予測する。
- `GET /classifier`: training data と classifier artifact の状態を確認する。
- `POST /classifier/model/import`: joblib / JSON artifact で唯一の classifier model を置き換える。旧 `/classifier/models/import` は互換 alias として `activate=true` の場合だけ受理する。
- `POST /rewrite`: glossary(用語・同義語)の置換だけを決定論で行う。**LLM による自由な書き換えはしない**(件数・抽出条件など質問の意味を変える追加を裏で行わないため)。`use_glossary=false` は無変換、`extra_prompt` は未使用(client 互換のため field のみ存置)。
- `POST /comments/suggest`, `POST /comments/apply`: COMMENT ON 候補生成と確認後の実行。
- `POST /annotations/generate`, `POST /annotations/apply`: Oracle annotations 候補生成と確認後の実行。

LLM/VLM はモデルの API のみ、embedding は既定の埋め込みモデルのみ、永続 state と SQL/DB 操作は Oracle に集約する。外部 LLM provider や外部 vector DB は使わない。

Classifier model は `classifier_artifact` 1 件のみを保存する。学習または artifact import が成功すると
現在の model を置き換え、model registry・version 切替・個別削除 API は提供しない。旧 snapshot に
`classifier_model_registry` が残っていても active な `classifier_artifact` だけを引き継ぎ、履歴 version は
次回保存時に破棄する。

## 管理 UI

- `Learning`: LogisticRegression 分類器の training import/train/predict と単一 model 状態、feedback vector entries/config/rebuild を扱う。
- `Query Workbench`: 実行オプション内で glossary(用語・同義語)の使用有無を切り替える。
- `Data Tools`: Excel/CSV import/export、COMMENT ON、annotations を扱う。
- `SQL Analysis`: deterministic analysis/reverse と Enterprise AI deep reverse を扱う。

DB を変更する UI 操作は管理コンソール扱いとし、通常の NL2SQL query 導線から分離する。`Drop 実行`、`COMMENT 実行`、`ANNOTATION 実行`は、チェックボックスによる明示確認が必要。

## 環境変数

最小構成:

```bash
NL2SQL_RUNTIME_MODE=oracle
NL2SQL_PERSISTENCE_MODE=oracle
PLATFORM_ORACLE_USER=...
PLATFORM_ORACLE_PASSWORD=...
PLATFORM_ORACLE_DSN=...
```

Enterprise AI:

```bash
PLATFORM_ENTERPRISE_AI_ENDPOINT=...
PLATFORM_ENTERPRISE_AI_API_KEY=...
PLATFORM_ENTERPRISE_AI_DEFAULT_TEXT_MODEL=...
```

埋め込みモデル（endpoint と API key は、システム設定 › モデル の既定の埋め込みモデルを登録した接続のもの。#45）:

```bash
PLATFORM_GENAI_EMBED_MODEL_ID=...
NL2SQL_FEEDBACK_EMBEDDING_ENABLED=true
```

## Live Smoke

旧版吸収の本番 smoke は backend から実行する。destructive DB 操作は明示実行フラグが無い場合はスキップする。

```bash
cd backend
uv run python scripts/nl2sql_manual_integration.py \
  --check-legacy-absorption \
  --require-oracle \
  --require-oracle-persistence \
  --require-feedback-embedding \
  --require-classifier-oracle-state \
  --engines enterprise_ai_direct \
  --allowed-table YOUR_TABLE \
  --question "YOUR_QUESTION" \
  --json-report reports/nl2sql-legacy-absorption.json
```

実行を伴う個別オプション:

- `--execute-comments`: COMMENT ON を実行する。
- `--execute-annotations`: `ALTER ... ANNOTATIONS` を実行する。
- `--execute-feedback-index`: feedback vector index rebuild を実行する。

Destructive smoke の table mutation (`--execute-comments`, `--execute-annotations`)
は `--allowed-table NL2SQL_<DISPOSABLE_TABLE>` が必須。
COMMENT / ANNOTATION 候補も allowed table に絞るため、既存業務表を誤更新しない。

## 危険操作ポリシー

- destructive/live DB 操作はすべて確認後に直接実行し、事前実行モードは提供しない。
- CLI では `--execute-*` が無い限り対象ステップをスキップし、DB 変更を行わない。
- table mutation の実行は `NL2SQL_` prefix の disposable allowed table に限定する。
- UI では実行チェックボックスをオンにしたときだけ danger variant の実行ボタンに切り替える。
- 実行結果は `executed`、`status`、`runtime`、`warning`、対象 asset/table/object を表示する。
