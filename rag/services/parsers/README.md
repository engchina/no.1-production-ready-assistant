# parser マイクロサービス群

外部 parser を **backend から切り離した独立 FastAPI サービス**として動かす。各サービスは
独自の依存と uv の venv を持ち、**単独で upgrade しても他 parser / backend に影響しない**。

| サービス | 実行 | 既定で配備 | 備考 |
|---|---|---|---|
| `docling` | CPU | ✅ | 既定の解析エンジン。**PDF と画像だけ**(親子階層（small-to-big）の分割に使うレイアウト解析) |
| `unstructured` | CPU | — | 多形式 partition(テキスト・HTML・Office・メール など)。stack の `rag_enable_parser_unstructured` / `rag-services.sh --unstructured` |
| `structured_records_jsonl` | CPU | ✅ | 構造化レコード JSONL（前処理 CSV→JSONL・Excel→JSONL・Word→JSONL の成果物）。記録を 1 要素にし、検索用テキストを記録の境界で区切る。使用エンジン（`RAG_PARSER_ADAPTER_BACKEND=structured_records_jsonl`）に選んだときだけ使う（#2457） |
| `xml_tree_json` | CPU | ✅ | XML ツリー JSON（XML の要素の木を tag / attr / children の JSON にした文書。対応する形の `.json`）。使用エンジン（`RAG_PARSER_ADAPTER_BACKEND=xml_tree_json`）に選んだときだけ使う（#2042） |
| `oci_document_understanding` | OCI | — | **準備中（ロードマップ。#3374）**。OCI Document Understanding を呼ぶ proxy。実装は残すが、まだ選べず、stack・`rag-services.sh` も配備しない |

音声・動画は取り込まない（アップロードで止める。#3369）。

## 共通 HTTP 契約(`rag_parser_core`)

- `POST /parse`(multipart: `file` / `content_type` / `source_profile` JSON)→ `ParseResponse`
  (= `StructuredExtraction` + parser メタ)
- `GET /health` → `{status, backend, package_name, package_version}`(readiness 用)

backend は取込時に `ParserServiceClient` で HTTP 委譲し、未達・空振り時は別経路へ縮退せず取込を止める。
既定の解析エンジンは `docling`(`RAG_PARSER_ADAPTER_BACKEND` の既定、#286)で、PDF と画像だけを解析する。
それ以外の形式は取込を始める前に止め、処理レシピで `unstructured`(`.eml` なども受ける汎用 partition)または
ファイル準備の Office→PDF を選ぶよう案内する(自動では振り分けない。判定は `backend/app/rag/parser_source_guard.py`)。
詳細は [AGENTS.md](../../AGENTS.md) の「Parser マイクロサービス」節。

## 起動

各 parser はサービスごとの uv の venv で動き、systemd の unit(`production-ready-rag-parser-<name>.service`)で
起動 / 停止する(#286。Docker は使わない)。本番は `rag/init_script.sh`、開発は `rag/scripts/rag-services.sh` が
venv(`uv sync --locked --no-dev --python 3.12`)と unit を作る。起動 / 停止は「運用設定 › サービス管理」画面から行う。

```bash
# rag/ で実行する
scripts/rag-services.sh install            # 前処理と既定の CPU parser(docling・structured-records-jsonl・xml-tree-json)
scripts/rag-services.sh install --unstructured  # Unstructured(Docling が扱えない形式を取り込む場合)
scripts/rag-services.sh run parser-docling # systemd の無い環境で前面に起動(127.0.0.1:18020)
```

OS のコマンド: unstructured は poppler-utils / tesseract-ocr(jpn)を使う。docling のモデルは venv を作った後に
`docling-tools models download` で実行ユーザーの `~/.cache` へ取得する(スクリプトが行う)。

MinerU、Dots.OCR はこのリポジトリでは構築・起動しない。
「取込・検索・回答設定 › 文書解析」で、外部運用済み API の Endpoint / Model / API key を設定する。

> 依存(`rag-parser-core` path 依存)を変更したら、各 pyproject の `uv lock` を再生成すること
> (配備は `uv sync --locked` で lock どおりに入れる)。

> cv2 は headless 版(`opencv-python-headless`)だけを入れる(#310)。GUI 版(`opencv-python`)は
> libxcb / libGL / glib(GUI の共有ライブラリ)を要し、それらの無いサーバーでは`import cv2` が失敗する。依存が GUI 版を
> 要求する parser(docling の rapidocr、unstructured の unstructured-inference)は pyproject の
> `[tool.uv] exclude-dependencies = ["opencv-python"]` で外し、`uv.lock` に GUI 版を残さない。
> 配備は `uv sync --locked` で lock どおりに入れる。
