# no.1-sql-assist Gap Audit

基準: `/u01/workspace/No.1-SQL-Assist`

本表は旧版の主要低層機能を、現在の FastAPI + React 構成へ再マップした吸収状況の正本です。外部 LLM provider、外部 vector DB、旧 Gradio UI は意図的に除外し、LLM/VLM は OCI Enterprise AI、embedding/rerank は OCI GenAI、状態とデータソースは Oracle に集約します。

| 旧版領域 | 旧版参照 | 現在の実装 | UI | 状態 |
|---|---|---|---|---|
| 完整数据库管理 | `utils/management_util.py` の table/view/DDL/comment/drop/general SQL | `/api/nl2sql/db-admin/tables`, `/views`, `/execute`, `/drop-table`, `/import-tabular`, `/tables/{name}/export.xlsx` | Data Tools: Database Admin / Excel 運用 | extended |
| Excel 运维流程 | `samples/*.xlsx`, training/terms/rules import/export | CSV/XLSX import preview/execute、training data XLSX/JSONL export、profile learning material import/export | Data Tools, Learning, Glossary Rules | extended |
| 分类模型训练 | `models/*.joblib`, `*.meta.json`, embedding + LogisticRegression | OCI GenAI 1536 embedding + sklearn LogisticRegression、Oracle state に current artifact 1 件だけを保存、学習/legacy artifact import は上書き | Learning: training data / train / test / assist | implemented |
| Select AI Profile 低层管理 | `utils/selectai_util.py` profile create/drop/set/list/detail | DB profile list/detail/create/update/drop、attributes JSON import/export、confirmation/audit | Engine Operations: DB profile JSON editor | extended |
| SQL 深度分析/逆生成 | `utils/query_util.py` SQL 分析・逆生成 | `/analyze` deterministic deep fields + optional OCI Enterprise AI structured JSON、`/reverse/deep` fallback付き逆生成 | SQL Analysis | extended |
| DBMS_CLOUD_AI 合成数据 | `DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA` | single object / object_list、profile_name/profile_id、prompt/sample/comment options、operation status/results | Data Tools: DBMS_CLOUD_AI Synthetic Data | extended |
| Select AI Agent 独立操作 | `utils/selectai_agent_util.py` asset/tool/team/conversation | assets list、run-team、run-tool、create conversation、history、privilege check、cleanup | Engine Operations: Agent low-level panel | extended |
| OCI/ADB Wallet 運用 | `utils/oci_util.py` の4ファイル確認・ADB Wallet生成・自動配置 | `POST /api/settings/database/wallet/download`、Database SDK の上限付きストリーミング取得、検証済み原子的配置、手動アップロード fallback | Database Settings: 欠損時の自動取得・状態表示・再取得 | secured remap |

質問分類 model は単一 current artifact に収斂した。version 一覧・activate・個別 delete API は削除し、
artifact import は `POST /classifier/model/import` で上書きする。旧
`POST /classifier/models/import` は `activate=true` の上書きだけを受理する互換 alias として残す。

## Wallet 自動取得の安全な再マッピング

旧 `utils/oci_util.py` の「必須4ファイルが不足していれば画面表示時に取得する」動作は維持するが、
固定 password、共通 `/tmp/wallet.zip`、無制限の `extractall`、既存 Wallet への直接展開は継承しない。
現実装は保存・返却・ログ出力をしない32文字の一時 password を生成し、Serverless はインスタンス
Wallet (`generate_type=SINGLE`)、Dedicated は `generate_type` 省略で OCI Database API を呼ぶ。
20 MB のダウンロード上限、100 MB の展開上限、Zip Slip / symlink / 必須4ファイル検証、worker 間
file lock を通過した内容だけを `PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin` へ原子的に配置する。Wallet
directory は `0700`、配下ファイルは `0600` とし、有効な既存 Wallet は自動上書き・自動輪換しない。

実行 identity には対象 Autonomous AI Database を読める policy が必要で、Wallet 生成には少なくとも
`read autonomous-databases`（permission: `AUTONOMOUS_DATABASE_CONTENT_READ`）を許可する。
OCI config、region、ADB OCID、IAM policy のいずれかが不足する場合は、Database Settings に常設した
Wallet ZIP の手動アップロードを復旧経路として使う。

## 実行境界

通常の `/api/nl2sql/execute` は従来どおり SELECT/WITH のみです。DB 管理系の DML/DDL/PLSQL/COMMENT、profile drop、comment/annotation apply、synthetic data execute は管理 UI 専用 API に分離し、`execute=true` に加えて `confirmation` が対象名または `ADMIN_EXECUTE` と一致する場合のみ実行します。

## 意図的に除外

- 旧 Gradio UI。
- 外部 LLM provider。
- 外部 vector DB。
- `.xls` legacy import。実 fixture 受領後に `xlrd` 導入可否を判断します。

## Blocked-by-env

以下はコード実装済みですが、production live 受入には実環境が必要です。

- OCI GenAI embedding 実 training。
- OCI Enterprise AI direct structured output。
- Oracle DBMS_CLOUD_AI profile create/drop/set/generate。
- Select AI Agent package/tool/team/conversation 実行。
- DBMS_CLOUD_AI synthetic data 実 execute/status/result。
- OCI Database API による実 ADB Wallet 生成（対象環境の IAM policy と永続 Wallet volume が必要）。

## SQL 論理構造からの往復生成（Issue #321）

固定 revision の `utils/selectai_util.py` にある物理構造分析・業務論理構造・質問・用語正規化の4プロンプトを原文保存し、3段階の Enterprise AI 呼び出しに再マッピングした。`/reverse/deep` の AI 構造表示を同期し、編集した構造から `/reverse/sql` で安全検証済みの SELECT/WITH を再生成する。対応表、修正点、検証範囲と同値性の限界は [SQL 論理構造の再生成](./sql-structure-roundtrip.md) を参照。
