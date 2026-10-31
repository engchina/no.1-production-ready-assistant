# parser-xml-tree-json

XML ツリー JSON（XML の要素の木を `tag` / `attr` / `children` の JSON にした文書）を、Markdown などに変換せず
要素の階層のまま解析する parser マイクロサービス（#2024）。

- 出力契約: `rag_parser_core` の `StructuredExtraction`（`POST /parse`）。解析の実体は `rag_parser_core.xml_tree_json`
- readiness: `GET /health`
- 依存は `rag-parser-core[service]` だけ（標準ライブラリの `json` で木を読む。外部の parser の package は使わない）
- 対応する形でない JSON は `xml_tree_json_unsupported_tree` の fallback を返す（backend は扱えない旨のエラーで取込を止める）

Docling・Unstructured と同じ「使用エンジン」の 1 つ（`RAG_PARSER_ADAPTER_BACKEND=xml_tree_json`。対応形式は `.json`）。
「取込・検索・回答設定 › 文書解析」の既定か、文書の処理レシピの使用エンジンで XML ツリー JSON を選んだときだけ、
backend がこのサービスへ回す（形式を見て自動では回さない。#2042）。サービスは統合 stack と `rag-services.sh install` の既定で
配備・起動する。

## 起動（uv の venv + systemd。#286）

```bash
# rag/ で実行する
scripts/rag-services.sh install parser-xml-tree-json   # venv を作り unit と sudoers を登録して起動（--cpu にも含まれる）
scripts/rag-services.sh run parser-xml-tree-json       # systemd の無い環境で前面に起動（127.0.0.1:18021）
```

## テスト

```bash
cd services/parsers/xml_tree_json && uv run --locked --all-extras --with pytest python -m pytest -q tests
```
