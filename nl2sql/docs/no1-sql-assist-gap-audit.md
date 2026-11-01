# no.1-sql-assist Gap Audit

基準: `/u01/workspace/No.1-SQL-Assist`

本表は旧版の主要低層機能を、現在の FastAPI + React 構成へ再マップした吸収状況の正本です。外部 LLM provider、外部 vector DB、旧 Gradio UI は意図的に除外し、LLM/VLM はモデルの API、embedding/rerank は埋め込み / 再ランクのモデル（OpenAI 互換の API）、状態とデータソースは Oracle に集約します。

| 旧版領域 | 旧版参照 | 現在の実装 | UI | 状態 |
|---|---|---|---|---|
| 完整数据库管理 | `utils/management_util.py` の table/view/DDL/comment/drop/general SQL | `/api/nl2sql/db-admin/tables`, `/views`, `/execute`, `/drop-table`, `/import-tabular`, `/tables/{name}/export.xlsx` | Data Tools: Database Admin / Excel 運用 | extended |
| Excel 运维流程 | `samples/*.xlsx`, training/terms/rules import/export | CSV/XLSX import preview/execute、training data XLSX/JSONL export、profile learning material import/export | Data Tools, Learning, Glossary Rules | extended |
| 分类模型训练 | `models/*.joblib`, `*.meta.json`, embedding + LogisticRegression | 埋め込みモデル（1024 次元）+ sklearn LogisticRegression、Oracle state に current artifact 1 件だけを保存、学習/legacy artifact import は上書き | Learning: training data / train / test / assist | implemented |
| SQL 深度分析/逆生成 | `utils/query_util.py` SQL 分析・逆生成 | `/analyze` deterministic deep fields + optional モデルの API structured JSON、`/reverse/deep` fallback付き逆生成 | SQL Analysis | extended |
| OCI/ADB Wallet 運用 | `utils/oci_util.py` の4ファイル確認・ADB Wallet生成・自動配置 | Wallet ZIP の手動アップロード（`POST /api/settings/database/wallet`）だけ。OCI Database API からの自動取得は削除した（#11） | Database Settings: Wallet ZIP のアップロード・状態表示 | removed |

質問分類 model は単一 current artifact に収斂した。version 一覧・activate・個別 delete API は削除し、
artifact import は `POST /classifier/model/import` で上書きする。旧
`POST /classifier/models/import` は `activate=true` の上書きだけを受理する互換 alias として残す。

## Wallet の OCI からの自動取得（削除）

旧 `utils/oci_util.py` の「必須4ファイルが不足していれば OCI Database API で Wallet を生成して配置する」動作は、
OCI に依存しない構成へ移すために削除した（#11）。Wallet は Database Settings から ZIP をアップロードする
（Zip Slip / symlink / 必須ファイルの検証と原子的な配置は共通の `pr_system_settings.database` が持つ）。

## 実行境界

通常の `/api/nl2sql/execute` は従来どおり SELECT/WITH のみです。DB 管理系の DML/DDL/PLSQL/COMMENT、comment/annotation apply は管理 UI 専用 API に分離し、`execute=true` に加えて `confirmation` が対象名または `ADMIN_EXECUTE` と一致する場合のみ実行します。

## 意図的に除外

- 旧 Gradio UI。
- 外部 LLM provider。
- 外部 vector DB。
- 旧版の DB 内 AI profile の管理・Agent の操作・DB 内のデータ生成（ADB 専用の package を前提にするため。SQL の生成はモデルの API を直接呼ぶ。#7）。
- `.xls` legacy import。実 fixture 受領後に `xlrd` 導入可否を判断します。

## Blocked-by-env

以下はコード実装済みですが、production live 受入には実環境が必要です。

- 埋め込みモデルを使う実 training。
- モデルの API の direct structured output。

## SQL 論理構造からの往復生成（Issue #321）

固定 revision の `utils/selectai_util.py` にある物理構造分析・業務論理構造・質問・用語正規化の4プロンプトを原文保存し、3段階の Enterprise AI 呼び出しに再マッピングした。`/reverse/deep` の AI 構造表示を同期し、編集した構造から `/reverse/sql` で安全検証済みの SELECT/WITH を再生成する。対応表、修正点、検証範囲と同値性の限界は [SQL 論理構造の再生成](./sql-structure-roundtrip.md) を参照。
