# NL2SQL データ準備 全機能テスト仕様・テストデータ

この文書は、画面左ナビの **データ準備** セクションにある全機能を手動/E2E/API で検証するための Markdown 版テスト仕様です。テストデータ本体は [docs/test-data/nl2sql-data-preparation/README.md](./test-data/nl2sql-data-preparation/README.md) を起点に配置しています。

## 対象機能

| メニュー | 主な検証範囲 | 主要 API |
|---|---|---|
| 管理 SQL を実行 | SELECT/WITH、確認済み DML/DDL/PLSQL、SQL ファイル読込、確認語、部分成功 | `POST /api/nl2sql/db-admin/execute` |
| テーブルの管理 | 一覧/検索/絞込/並び替え、詳細、DDL 後追い取得、正確件数、作成、表形式取込、列 Excel 出力、削除 | `GET /api/nl2sql/db-admin/objects`, `GET /tables/{name}`, `POST /db-admin/statements`, `POST /db-admin/import-tabular`, `POST /db-admin/drop-table` |
| ビューの管理 | 一覧/詳細/作成/削除、JOIN/WHERE 抽出、列 Excel 出力 | `GET /views/{name}`, `POST /db-admin/extract-join-where`, `POST /db-admin/drop-view` |
| データの管理 | テーブル/ビュー表示、WHERE/limit、Excel 出力、既存表 CSV 取込、合成データ生成 | `POST /db-admin/preview-data`, `POST /db-admin/upload-csv`, `POST /synthetic-data/generate` |
| コメント管理 | 対象選択、構造/サンプル取得、COMMENT SQL 生成、実行、policy block | `POST /metadata-samples`, `POST /comments/generate-sql`, `POST /db-admin/statements` |
| アノテーション管理 | 対象選択、構造/サンプル取得、ANNOTATIONS SQL 生成、実行、policy block | `POST /annotations/generate-sql`, `POST /db-admin/statements` |
| ドメイン管理 | 対象選択、構造/サンプル/既存ドメイン取得、作成・更新・再作成・削除の SQL 生成、実行、policy block | `POST /domains/inventory`, `POST /domains/generate-sql`, `POST /db-admin/statements` |
| 用語・同義語 | Excel 取込/出力、プレビュー、ページング、エラー表示 | `GET /legacy-learning-material`, `POST /legacy-learning-material/terms/import`, `GET /legacy-learning-material/terms/export.xlsx` |
| 共通ルール | Excel 取込/出力、プレビュー、ページング、エラー表示 | `POST /legacy-learning-material/rules/import`, `GET /legacy-learning-material/rules/export.xlsx` |
| 検証用サンプルデータ | sample package 状態、tables/views/data/all 取込、削除、確認語 | `GET /sample-data`, `POST /sample-data/import`, `POST /sample-data/delete` |

## 前提条件

- ローカル DEBUG または `SYSTEM_ADMIN` 相当のユーザーでログインしていること。
- 破壊的操作や更新操作は、画面に表示される確認語と完全一致する入力が必要。
- 汎用の管理 SQL、テーブル/ビュー作成カード、コメント/アノテーション実行カードは `ADMIN_EXECUTE` を要求する。
- テーブル削除/ビュー削除/CSV 既存表取込など、対象名確認を要求する操作は `ADMIN_EXECUTE` では代替できない。対象名そのものを入力する。
- 実 Oracle 実行を検証する場合は `NL2SQL_RUNTIME_MODE=oracle` が必要。未設定時は mutating 操作が `requires_oracle` 警告を返すことを確認する。
- DDL/COMMENT/ALTER 後は schema refresh job が投入され、一覧や詳細に反映されることを確認する。

## テストデータ準備

1. 再実行時だけ [00_cleanup.sql](./test-data/nl2sql-data-preparation/sql/00_cleanup.sql) を「管理 SQL を実行」から `ADMIN_EXECUTE` 付きで流す。
2. [01_create_base_schema.sql](./test-data/nl2sql-data-preparation/sql/01_create_base_schema.sql) を実行する。
3. [02_insert_base_data.sql](./test-data/nl2sql-data-preparation/sql/02_insert_base_data.sql) を実行する。
4. [03_create_views.sql](./test-data/nl2sql-data-preparation/sql/03_create_views.sql) を実行する。
5. 任意で [04_comments.sql](./test-data/nl2sql-data-preparation/sql/04_comments.sql) と [05_annotations.sql](./test-data/nl2sql-data-preparation/sql/05_annotations.sql) を実行する。
6. 「データの管理」または各管理画面の「スキーマ再取得」を実行する。
7. 「用語・同義語」に `learning/terms.xlsx`、「共通ルール」に `learning/rules.xlsx` を取り込む。

基準件数:

| オブジェクト | 期待件数 |
|---|---:|
| `TD_NL2SQL_DEPARTMENTS` | 4 |
| `TD_NL2SQL_CUSTOMERS` | 6 |
| `TD_NL2SQL_PRODUCTS` | 6 |
| `TD_NL2SQL_ORDERS` | 8 |
| `TD_NL2SQL_ORDER_LINES` | 12 |
| `TD_NL2SQL_AUDIT_EVENTS` | 2 |
| `V_TD_NL2SQL_ORDER_SUMMARY` | 8 |
| `V_TD_NL2SQL_CUSTOMER_SALES` | 6 |
| `V_TD_NL2SQL_OPEN_ORDERS` | 5 |

## 共通テスト観点

| ID | 観点 | 手順 | 期待結果 |
|---|---|---|---|
| DP-COM-001 | 権限 gate | `SYSTEM_ADMIN` で各メニューを開く | データ準備メニューが表示され、画面にアクセスできる |
| DP-COM-002 | DB 未設定 gate | DB 接続未設定状態でデータ準備画面を開く | DatabaseGate が表示され、危険操作は実行できない |
| DP-COM-003 | 更新確認語 | 更新系ボタンを確認語なし/不一致で押す | ボタン disabled または `confirmation_required` |
| DP-COM-004 | 処理中表示 | refresh/import/execute 中に画面を見る | `ProcessingIndicator` または skeleton が表示され、古い空状態と重ならない |
| DP-COM-005 | エラー表示 | API を 500/timeout に mock | danger notice と再試行導線が表示される |
| DP-COM-006 | ファイル拡張子 | 対象外拡張子を drop | 対応形式エラーが表示され、既存入力は壊れない |

## 管理 SQL を実行

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-SQL-001 | `sql/06_admin_select.sql` | SQL ファイルを読み込み、確認語なしで実行 | `executed=true`, `committed=false`, `select_result.total=8` |
| DP-SQL-002 | `sql/07_admin_confirmed_dml_batch.sql` | 確認語なしで実行を試す | `ADMIN_EXECUTE` 入力欄が表示され、実行不可 |
| DP-SQL-003 | 同上 | `ADMIN_EXECUTE` 入力後に実行 | pure DML として成功文が commit され、監査 event が 1 件増える |
| DP-SQL-004 | `sql/08_admin_partial_success_dml.sql` | `ADMIN_EXECUTE` 付きで実行 | 1 文目は success、重複 PK の 2 文目は error、部分成功警告が出る |
| DP-SQL-005 | `sql/negative_mixed_select_and_dml.sql` | `ADMIN_EXECUTE` 付きで実行 | SELECT を含む複数 statement として blocked、commit されない |
| DP-SQL-006 | 空 SQL | 実行ボタンを見る | disabled または `SQL statement がありません` |
| DP-SQL-007 | `.md` ファイル | SQL ファイル領域へ drop | `.SQL / .TXT` 以外のエラーが表示される |

## テーブルの管理

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-TBL-001 | setup 済み schema | 一覧を開き `TD_NL2SQL` で検索 | `TD_NL2SQL_*` tables が表示される |
| DP-TBL-002 | `TD_NL2SQL_ORDERS` | 詳細を開く | columns/row count/comment が表示される |
| DP-TBL-003 | 同上 | DDL タブを開く | DDL が後追い取得される |
| DP-TBL-004 | 同上 | 正確な件数取得 | `row_count=8` が表示される |
| DP-TBL-005 | 同上 | 列 Excel 出力 | `td_nl2sql_orders_columns.xlsx` が download される |
| DP-TBL-006 | table create | 作成画面で `CREATE INDEX ...` を実行 | `table_ddl` policy で blocked |
| DP-TBL-007 | `import/td_nl2sql_table_import.csv` / `.xlsx` | 取込画面で table=`TD_NL2SQL_IMPORTED_QUOTES`, confirmation=`ADMIN_EXECUTE` | 新規 table が作成され、sample rows が表示される |
| DP-TBL-008 | 同上 | 同名 table に create mode で再取込 | ORA-00955 相当のエラーが表示される |
| DP-TBL-009 | `TD_NL2SQL_IMPORTED_QUOTES` | 削除 dialog に `ADMIN_EXECUTE` を入力 | `ADMIN_EXECUTE では代替できません` |
| DP-TBL-010 | 同上 | 削除 dialog に `TD_NL2SQL_IMPORTED_QUOTES` を入力 | drop success、一覧から消える |

## ビューの管理

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-VIEW-001 | setup 済み views | 一覧で `V_TD_NL2SQL` を検索 | 3 views が表示される |
| DP-VIEW-002 | `V_TD_NL2SQL_OPEN_ORDERS` | 詳細/DDL を開く | columns と DDL が表示される |
| DP-VIEW-003 | 同上 | JOIN/WHERE 抽出 `sql_structure` | JOIN 条件と `STATUS IN (...)` / `TOTAL_AMOUNT > 0` が抽出され、`structure_markdown` が表示される。Enterprise AI 未設定時は deterministic warning |
| DP-VIEW-004 | view create | `CREATE OR REPLACE VIEW V_TD_NL2SQL_REVIEW_QUEUE ...` を実行 | view が作成され、schema refresh 後に一覧へ出る |
| DP-VIEW-005 | drop view | confirmation に `ADMIN_EXECUTE` | 対象名確認エラー |
| DP-VIEW-006 | drop view | confirmation に view 名 | drop success |

## データの管理

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-DATA-001 | `TD_NL2SQL_ORDERS` | limit=5 で表示 | 5 行まで表示(行数上限は取得時の fetch 上限で適用。SQL へ `FETCH FIRST` は付与しない) |
| DP-DATA-002 | `TD_NL2SQL_ORDERS` | WHERE=`STATUS = 'NEW'` | 2 行表示 |
| DP-DATA-003 | `sql/negative_invalid_preview_where.txt` | WHERE 欄に貼り付ける | `WHERE 句に複数 statement は指定できません` |
| DP-DATA-004 | `V_TD_NL2SQL_ORDER_SUMMARY` | 表示して Excel 出力 | preview workbook に `data` と `query` sheet |
| DP-DATA-005 | `import/td_nl2sql_orders_delta.csv` / `.xlsx` | 表形式取込: table=`TD_NL2SQL_ORDERS`, mode=`insert`, confirmation=`TD_NL2SQL_ORDERS` | 2 行 inserted、件数が 10 になる |
| DP-DATA-006 | `import/td_nl2sql_orders_delta_extra_column.csv` | CSV 取込 | `unmatched_csv_columns` に `MEMO_FOR_TEST` が出る |
| DP-DATA-007 | `import/td_nl2sql_orders_bad_type.csv` | CSV 取込 | 数値/日付不正の row error が表示される |
| DP-DATA-008 | 合成データ | Business profile を選択し table を 1 つ選ぶ。confirmation は table 名 | operation が submitted/executed になり status/results を取得できる |
| DP-DATA-009 | 合成データ複数 table | 2 tables 選択。confirmation=`ADMIN_EXECUTE` | 複数 table では `ADMIN_EXECUTE` が期待確認語になる |

## コメント管理

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-COMMENT-001 | `TD_NL2SQL_CUSTOMERS`, `V_TD_NL2SQL_CUSTOMER_SALES` | 対象を複数選択し情報取得 | 構造/PK/FK/サンプルが入力欄へ反映 |
| DP-COMMENT-002 | 同上 | SQL 生成 | `COMMENT ON TABLE/COLUMN/VIEW` のみ生成 |
| DP-COMMENT-003 | 生成 SQL | `ADMIN_EXECUTE` で実行 | success、schema refresh job が投入される |
| DP-COMMENT-004 | `DROP TABLE TD_NL2SQL_CUSTOMERS` | コメント実行欄へ貼り付ける | `comment_sql` policy で blocked |
| DP-COMMENT-005 | sample_limit=0 | SQL 生成 | sample なしでも構造から deterministic/AI 生成できる |

## アノテーション管理

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-ANN-001 | `TD_NL2SQL_ORDERS` | 対象選択、情報取得、SQL 生成 | `ALTER TABLE ... ANNOTATIONS` または `MODIFY (... ANNOTATIONS ...)` |
| DP-ANN-002 | 生成 SQL | `ADMIN_EXECUTE` で実行 | success。素の `ADD` は `ADD IF NOT EXISTS` へ正規化され再実行安全 |
| DP-ANN-003 | 不正 annotation | `ALTER TABLE TD_NL2SQL_ORDERS DROP COLUMN STATUS` | `annotation_sql` policy で blocked |
| DP-ANN-004 | view annotation | `V_TD_NL2SQL_OPEN_ORDERS` | `ALTER VIEW ... ANNOTATIONS` が実行できる |
| DP-ANN-005 | view 列 annotation | `V_TD_NL2SQL_OPEN_ORDERS` の列 | `ALTER VIEW ... MODIFY (<列> ANNOTATIONS (...))` が生成・実行できる(MV 列は生成されない) |
| DP-ANN-006 | 語彙 | `TD_NL2SQL_ORDERS` の STATUS 列(サンプルあり) | `"DESCRIPTION"` / `"ALIASES"` / `"VALUES"` / `"UNITS"` / `"JOIN COLUMN"` が生成され、`data_type` / `nullable` / `sample_*` は生成されない。ドメイン付き列には `"JOIN COLUMN"` 等の表固有情報だけ |
| DP-ANN-007 | Select AI 連携 | 業務プロファイルの「アノテーションを利用」= true で SQL 生成 | `DBMS_CLOUD_AI.GENERATE(action=>'showprompt')` に annotation が含まれ、コード値(例: `'A'`)の WHERE 条件が正しく生成される |

## ドメイン管理

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-DOM-001 | `TD_NL2SQL_CUSTOMERS`, `TD_NL2SQL_ORDERS` | 対象選択、情報取得、SQL 生成 | 共通列(例: `CUSTOMER_ID`)の `CREATE DOMAIN IF NOT EXISTS ... ANNOTATIONS ("DESCRIPTION" ...)` と各表の `ALTER TABLE ... MODIFY (<列>) ADD DOMAIN <ドメイン>` |
| DP-DOM-002 | 生成 SQL | `ADMIN_EXECUTE` で実行 | success。`USER_TAB_COLS.DOMAIN_NAME` と `USER_ANNOTATIONS_USAGE` に継承 annotation が載る。CREATE DOMAIN 単独では Schema job を投入しない |
| DP-DOM-003 | 列型変更だけの MODIFY | `ALTER TABLE TD_NL2SQL_ORDERS MODIFY (STATUS VARCHAR2(40))` | `domain_sql` policy で blocked(DOMAIN 句が無い) |
| DP-DOM-004 | 単一テーブル | `TD_NL2SQL_ORDERS` のみ選択、Enterprise AI 未設定 | deterministic 候補なしの warning。SQL 欄は空で実行不可 |
| DP-DOM-005 | 関連付け解除 | `ALTER TABLE TD_NL2SQL_ORDERS MODIFY (CUSTOMER_ID) DROP DOMAIN` → `DROP DOMAIN CUSTOMER_ID_D` | いずれも `domain_sql` policy で実行できる |
| DP-DOM-006 | 既存ドメイン | DP-DOM-002 の後に情報取得 | 構造情報の列に `DOMAIN=<OWNER>.CUSTOMER_ID_D`、「既存ドメイン」に型・CHECK・ANNOTATIONS・COLUMNS(選択外の表を含む)が表示される。作成を選ぶと当該列は候補から外れ warning |
| DP-DOM-007 | 操作=更新 | SQL 生成 → 実行 | `ALTER DOMAIN ... ANNOTATIONS (ADD OR REPLACE ...)` だけが生成され、型・制約の変更は再作成を促す warning |
| DP-DOM-008 | 操作=再作成 | SQL 生成 → 実行 | `DROP DOMAIN ... FORCE` → `CREATE DOMAIN`(型・STRICT・NOT NULL・CHECK・ANNOTATIONS を引き継ぎ)→ COLUMNS の全列へ `ADD DOMAIN`。`USER_ANNOTATIONS_USAGE` の継承 annotation が復元される |
| DP-DOM-009 | 操作=削除 | 一部の表だけ選択して SQL 生成 | 選択列の `MODIFY (<列>) DROP DOMAIN` のみ。他表で使用中なら `DROP DOMAIN` は生成されず warning。全ての関連付け先を選ぶと `DROP DOMAIN` が付く |

DP-DOM-001〜009 の実 DB 版は `backend/tests/test_domain_management_oracle_live.py` に自動化している(CI では実行しない)。`.env` の接続先で `cd backend && NL2SQL_RUN_ORACLE_INTEGRATION=1 uv run pytest tests/test_domain_management_oracle_live.py -s` を実行すると、一意な接頭辞の検証表を作成して作成 → 把握 → 更新 → 再作成 → CHECK/STRICT ドメインへの付け替え → 削除を `domain_sql` policy で流し、finally で削除する(Oracle AI Database 23.26.3.3.0 で確認済み)。

## 用語・同義語

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-TERM-001 | `learning/terms.xlsx` | Excel 取込 | 12 件取込。プレビューに TERM/DEFINITION |
| DP-TERM-002 | 同上 | ページングで次ページへ | 11 件目以降が表示される |
| DP-TERM-003 | export | Excel 出力 | `terms.xlsx` を download |
| DP-TERM-004 | `learning/rules.xlsx` を誤投入 | 用語画面へ取込 | TERM/DEFINITION 不足のエラーまたは 0 件 warning |

## 共通ルール

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-RULE-001 | `learning/rules.xlsx` | Excel 取込 | 12 件取込。RULE 列が表示される |
| DP-RULE-002 | 同上 | ページングで次ページへ | 11 件目以降が表示される |
| DP-RULE-003 | export | Excel 出力 | `rules.xlsx` を download |
| DP-RULE-004 | `learning/terms.xlsx` を誤投入 | 共通ルール画面へ取込 | RULE 不足のエラーまたは 0 件 warning |

## 検証用サンプルデータ

| ID | データ | 手順 | 期待結果 |
|---|---|---|---|
| DP-SAMPLE-001 | なし | 画面を開く | `objects` と `imported_objects` が表示され、SQL preview が出る |
| DP-SAMPLE-002 | step=`tables` | confirmation なし | 実行ボタン disabled |
| DP-SAMPLE-003 | step=`tables` | confirmation=`ADMIN_EXECUTE` | `DEPARTMENT`, `EMPLOYEE`, `PROJECT` が作成される |
| DP-SAMPLE-004 | step=`views` | confirmation=`ADMIN_EXECUTE` | `V_EMP_DEPT`, `V_DEPT_PROJECT` が作成される |
| DP-SAMPLE-005 | step=`data` | confirmation=`ADMIN_EXECUTE` | sample rows が inserted |
| DP-SAMPLE-006 | delete | confirmation=`ADMIN_EXECUTE` | views/tables が削除され、imported count が 0 |

## 後片付け

テスト終了後は [00_cleanup.sql](./test-data/nl2sql-data-preparation/sql/00_cleanup.sql) を実行し、必要に応じて「検証用サンプルデータ」の削除も行います。cleanup 後に schema refresh を実行し、`TD_NL2SQL_%` / `V_TD_NL2SQL_%` が一覧に残っていないことを確認します。
