# parser-structured-records-jsonl

前処理（`csv_to_jsonl` / `excel_to_jsonl` / `word_to_jsonl`）が作った構造化レコードの JSON Lines
（`application/vnd.production-ready.structured-records+jsonl`）を、記録の単位で解析する parser マイクロサービス（#2457）。

- 出力契約: `rag_parser_core` の `StructuredExtraction`（`POST /parse`）。解析の実体は `rag_parser_core.structured_records`
- readiness: `GET /health`
- 依存は `rag-parser-core[service]` だけ（外部の parser の package は使わない）
- 記録を 1 要素にする（要素の ID・metadata は Docling / Unstructured の記録の経路と同じ。Excel はシート・行・セル範囲・列の役割、CSV は原本の物理行）
- 検索用テキスト（`raw_text`）は、記録と記録の間に独立した行の `<<<JSONL_RECORD_BOUNDARY>>>` を置く（先頭・末尾には置かない。JSONL のファイルには置かない）。
  原文の同じ文字列は `<<<` + k 個の `\` + `JSONL_RECORD_BOUNDARY>>>` を k+1 個にする可逆のエスケープで区別する
- 分割方式「固定分割」（`fixed_delimiter`）は、この抽出を記録の境界で 1 記録 1 chunk にする（chunk の本文はエスケープを戻した原文で、
  マーカーと境界の改行を含まない。chunk size / overlap で再分割・結合しない）。境界の数と記録の数が合わないときは診断して止める
- 契約に合わない JSONL・本文が 1 要素の上限（8,000 字）を超える記録は、`structured_records_invalid:<理由>@line<n>` の fallback を返す
  （backend は取込を止める。黙って切らない）

Docling・Unstructured と同じ「使用エンジン」の 1 つ（`RAG_PARSER_ADAPTER_BACKEND=structured_records_jsonl`）。
「取込・検索・回答設定 › 文書解析」の既定か、文書の処理レシピの使用エンジンで構造化レコード JSONL を選んだときだけ、
backend がこのサービスへ回す（形式を見て自動では回さない）。サービスは統合 stack と `rag-services.sh install` の既定で配備・起動する。

## 起動（uv の venv + systemd。#286）

```bash
# rag/ で実行する
scripts/rag-services.sh install parser-structured-records-jsonl   # venv を作り unit と sudoers を登録して起動（--cpu にも含まれる）
scripts/rag-services.sh run parser-structured-records-jsonl       # systemd の無い環境で前面に起動（127.0.0.1:18023）
```

## テスト

```bash
cd services/parsers/structured_records_jsonl && uv run --locked --all-extras --with pytest python -m pytest -q tests
```
