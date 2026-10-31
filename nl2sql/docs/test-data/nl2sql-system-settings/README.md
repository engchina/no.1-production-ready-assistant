# NL2SQL システム設定 テストデータ

このディレクトリは `docs/nl2sql-system-settings-test-plan.md` で参照する fixture 一式です。

## ディレクトリ

| パス | 内容 |
| --- | --- |
| `api/` | PATCH / POST リクエスト payload のサンプル |
| `mock-responses/` | Playwright / API mock 用の response JSON |
| `oci/` | OCI config / 秘密鍵アップロード用ファイル |
| `upload-storage/` | 保存先設定の異常系データ |
| `model/` | モデル設定の正常 / 異常 payload |
| `database/` | wallet ZIP、tnsnames、DB 設定関連ファイル |
| `system-tables/` | システムテーブル説明用 SQL / migration メモ |
| `appearance/` | `production-ready-nl2sql.ui` localStorage fixture |

## 注意

- すべてダミー値です。実 OCI / DB へ接続するための secret は含みません。
- `.pem` と `.zip` はアップロード UI の拡張子検証と mock テスト用です。
- `wallet-valid.zip` は最小構成のダミー wallet であり、Oracle へ接続できません。
- JSON は CI / Playwright fixture としてそのまま import できます。

