# NL2SQL AI 活用 テストデータ

このディレクトリは `docs/nl2sql-ai-utilization-test-plan.md` で参照する fixture 一式です。

## ディレクトリ

| パス | 内容 |
| --- | --- |
| `api/` | POST request payload のサンプル |
| `mock-responses/` | Playwright / API mock 用 response JSON |
| `sql/` | textarea / file upload 用 SQL 入力ファイル |
| `questions/` | 自然言語質問、rewrite、edge case 入力 |
| `schema/` | profile / schema をまとめて確認する補助 fixture |
| `history/` | 履歴・feedback 検証用の補助 fixture |

## 注意

- すべてダミー値です。実 OCI / Oracle secret は含みません。
- DML / 複文 SQL は「ブロックされること」を確認するための異常系データです。
- `mock-responses/` の JSON は `{ data, error_messages, warning_messages }` envelope 形式です。
- `api/` の JSON は request body としてそのまま利用できます。

