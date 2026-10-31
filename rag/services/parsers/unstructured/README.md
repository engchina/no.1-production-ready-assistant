# parser-unstructured

Unstructured を独立したプロセス(サービスごとの uv の venv)で動かす parser マイクロサービス。

- 出力契約: `rag_parser_core` の `StructuredExtraction`(`POST /parse`)
- readiness: `GET /health`
- unstructured のバージョンは本サービス単独で upgrade 可能。extra は残す形式の分（`pdf,image,docx,pptx,xlsx,csv,tsv,md`）だけで、
  `all-docs` にしない（#3351）
- 入れない依存（ライセンスの理由。#3351）: `pi-heif`（同梱の libheif / libde265 が LGPL-3.0）と `pypandoc-binary`（同梱の pandoc が
  GPL-2.0+）。`pyproject.toml` の `exclude-dependencies` で外す。そのため次の形式は Unstructured では原本を読まない:
  - HEIC / HEIF・EPUB: アップロードで止める（画像は JPEG / PNG、EPUB は PDF に変換してからアップロードする）
  - ODT / RTF: 処理レシピのファイル準備で Office→PDF を選び、PDF にしてから読む
  - reStructuredText（`.rst`）・Org（`.org`）: pandoc を通さず、プレーンテキストとして読む
- unstructured の PDF・画像の解析（`unstructured/partition/pdf.py`）は module の先頭で `pi_heif` を import する。pi-heif が無いときは
  `rag_parser_core.unstructured_compat` が何もしない `register_heif_opener` の stub を置く（HEIC / HEIF は Pillow が開けないまま）
- OS 依存(poppler / tesseract)は host に入れる(`rag/init_script.sh` / `rag/docs/deployment.md`)

## 起動(uv の venv + systemd。#286)

サービスごとの uv の venv(`uv sync --locked --no-dev --python 3.12`)で動き、本番・開発とも systemd の unit
(`production-ready-rag-parser-unstructured.service`)で起動 / 停止する。Docker は使わない(Dockerfile は #356 で削除した)。

```bash
# rag/ で実行する
scripts/rag-services.sh install parser-unstructured   # venv を作り unit と sudoers を登録して起動
scripts/rag-services.sh run parser-unstructured       # systemd の無い環境で前面に起動
```
