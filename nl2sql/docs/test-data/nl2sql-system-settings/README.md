# NL2SQL システム設定 テストデータ

このディレクトリは `docs/nl2sql-system-settings-test-plan.md` で参照する fixture 一式です。

## ディレクトリ

| パス | 内容 |
| --- | --- |
| `api/` | PATCH / POST リクエスト payload のサンプル |
| `mock-responses/` | Playwright / API mock 用の response JSON |
| `upload-storage/` | 保存先設定（ローカルのディレクトリ）の `.env` の例 |
| `model/` | モデル設定の正常 / 異常 payload |
| `database/` | DB 設定の `.env` の例 |
| `system-tables/` | システムテーブル説明用 SQL / migration メモ |

## 注意

- すべてダミー値です。実 DB へ接続するための secret は含みません。
- JSON は CI / Playwright fixture としてそのまま import できます。

