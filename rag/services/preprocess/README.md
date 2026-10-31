# 前処理(Preprocess)マイクロサービス群

parse の **前** に原本を一度だけ canonical な中間物へ変換する前処理ステージ(画面では「ファイル準備」)。
`services/parsers/<name>` と同じく **1 変換 = 1 独立マイクロサービス**で、各々独自依存・
独自の依存と uv の venv で独立 upgrade / 独立スケールでき、相互・backend に非干渉。

| サービス | profile | 画面の名前 | 変換 | 主依存 | 既定 URL |
|---|---|---|---|---|---|
| [office_to_pdf](./office_to_pdf) | `office_to_pdf` | Office→PDF | Word・PowerPoint(.doc/.docx/.docm/.ppt/.pptx/.pptm)と ODT・RTF(.odt/.rtf。#3351)→ PDF。Excel は対象外 | LibreOffice | `http://127.0.0.1:18010` |
| [csv_to_jsonl](./csv_to_jsonl) | `csv_to_jsonl` | CSV→JSONL | CSV → 構造化レコード JSONL | (純 Python) | `http://127.0.0.1:18012` |
| [excel_to_jsonl](./excel_to_jsonl) | `excel_to_jsonl` | Excel→JSONL | Excel(.xls/.xlsx/.xlsm) → 構造化レコード JSONL | openpyxl + xlrd | `http://127.0.0.1:18013` |
| [word_to_jsonl](./word_to_jsonl) | `word_to_jsonl` | Word→JSONL | Word(.doc/.docx/.docm) → 構造化レコード JSONL(見出しの下の段落・リスト・表のまとまり) | LibreOffice(.doc だけ)+ python-docx | `http://127.0.0.1:18017` |

PDF を画像として読み直す変換(`pdf_to_page_images`)は #3347 で削除した(スキャンの PDF は Docling の OCR が読む)。
取込時の PIIマスク(`pii_redact`。Presidio + spaCy の日本語モデル)は #3358 で削除した。

画面の名前と説明は `rag/frontend/src/lib/i18n.ts` の `settings.preprocess.profile.*` が正本。

`passthrough` / `text_normalize` は backend in-process で処理するため、サービスは呼ばれない。

- 出力契約は共有 package `rag_parser_core` の `ConvertResponse`(`POST /convert`)で統一。
- CSV / Excel / Word の派生物は構造化レコードの JSON Lines(`rag_parser_core.structured_records`。#2457)。
  UTF-8・BOM なし・1 物理行 1 オブジェクト・LF。すべての行が `schema`・`schema_version`・`record_id`・
  `source_type`・`record_type` を持ち、parser は未知の schema・未対応の版・不正な行を行番号つきで拒否する。
- backend は `app.clients.preprocess_service.PreprocessServiceClient` が profile ごとに
  対応サービス URL(`RAG_PREPROCESS_<PROFILE>_SERVICE_URL`)へ HTTP 委譲する。
  サービス無効・未達・timeout 時は warning を付けて **passthrough(原本そのまま parse)** へ
  安全に縮退する。
- 選択は `RAG_PREPROCESS_PROFILE`、委譲の有効化は `RAG_PREPROCESS_ENABLED`、timeout は
  `RAG_PREPROCESS_SERVICE_TIMEOUT_SECONDS`。

## 起動

- **開発**: 触る変換のサービスだけ起動する(`rag/scripts/rag-services.sh run preprocess-<name>`、または install で unit を登録)。
  in-process(passthrough / text_normalize)は何も起動不要。
- **本番 / 開発**: 各サービスをサービスごとの uv の venv と systemd の unit
  (`production-ready-rag-preprocess-<name>.service`、`127.0.0.1:18010・18012・18013・18017`)で動かす(#286。Docker は使わない)。
  本番は `rag/init_script.sh`、開発は `rag/scripts/rag-services.sh install` が venv と unit を作る。
  office_to_pdf は LibreOffice と CJK フォント、word_to_jsonl は LibreOffice(Writer。.doc の変換)を OS に入れる。
