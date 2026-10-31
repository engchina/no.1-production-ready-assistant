# NL2SQL データ準備テストデータ

このディレクトリは [NL2SQL データ準備 全機能テスト仕様](../../nl2sql-data-preparation-test-plan.md) で使うテストデータ一式です。既存 sample package とは衝突しないよう、DB オブジェクトは `TD_NL2SQL_` / `V_TD_NL2SQL_` 接頭辞で統一しています。

## ファイル一覧

| パス | 用途 |
|---|---|
| `sql/00_cleanup.sql` | 再実行/後片付け用の idempotent cleanup |
| `sql/01_create_base_schema.sql` | テーブル 6 件を作成 |
| `sql/02_insert_base_data.sql` | 基準データを投入 |
| `sql/03_create_views.sql` | ビュー 3 件を作成 |
| `sql/04_comments.sql` | COMMENT ON の手動/実行カード検証 |
| `sql/05_annotations.sql` | Oracle AI Database の annotations の検証 |
| `sql/06_admin_select.sql` | 管理 SQL の SELECT 検証 |
| `sql/07_admin_confirmed_dml_batch.sql` | 管理 SQL の確認済み DML 検証 |
| `sql/08_admin_partial_success_dml.sql` | DML 部分成功検証 |
| `sql/negative_mixed_select_and_dml.sql` | SELECT+DML 混在 block 検証 |
| `sql/negative_invalid_preview_where.txt` | データ表示 WHERE injection block 検証 |
| `import/td_nl2sql_table_import.csv` / `.xlsx` | テーブル管理の新規表取込 |
| `import/td_nl2sql_orders_delta.csv` / `.xlsx` | データ管理の既存表 insert |
| `import/td_nl2sql_orders_delta_extra_column.csv` | 未一致 CSV 列 warning 検証 |
| `import/td_nl2sql_orders_bad_type.csv` | 行単位エラー検証 |
| `learning/terms.csv` / `learning/terms.xlsx` | 用語・同義語取込 |
| `learning/rules.csv` / `learning/rules.xlsx` | 共通ルール取込 |
| `api/*.json` | API 直叩き用 payload 例 |

## 推奨セットアップ順

1. `sql/00_cleanup.sql`
2. `sql/01_create_base_schema.sql`
3. `sql/02_insert_base_data.sql`
4. `sql/03_create_views.sql`
5. `sql/04_comments.sql`
6. `sql/05_annotations.sql`
7. schema refresh
8. `learning/terms.xlsx` と `learning/rules.xlsx` を UI から取込

`00_cleanup.sql` は匿名 PL/SQL なので、画面では **管理 SQL を実行** から `ADMIN_EXECUTE` 付きで使ってください。テーブル/ビュー作成カードの whitelist 用ではありません。
