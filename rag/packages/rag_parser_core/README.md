# rag-parser-core

backend と parser マイクロサービス(docling / unstructured など)が
共有する **parser 契約**パッケージ。

- `extraction` — `StructuredExtraction` ほか抽出スキーマ(全 parser の共通出力契約)
- `source` — `SourceProfile` / `SourceModality` など原本メタデータ
- `routing` — source kind 別の外部 adapter 優先順
- `registry` — ローカル parser + 外部 adapter remap(`parse_with_registry`)。
  外部 parser 依存は遅延 import の任意依存。
- `result` — HTTP 契約(`ParseResponse` / `ParseHealth`)

依存は **pydantic + charset-normalizer のみ**。重い parser 依存(docling 等)や oracle は
含めない。各 parser サービスがそれぞれの重い依存を独自に持ち、本 package を共有する。
