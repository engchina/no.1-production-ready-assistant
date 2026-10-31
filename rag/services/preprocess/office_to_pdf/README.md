# preprocess-office-to-pdf

parse の **前** に Word・PowerPoint の文書(.doc/.docx/.docm/.ppt/.pptx/.pptm/.odt/.odp/.rtf)を
LibreOffice headless で PDF へ変換する前処理マイクロサービス。

- ODT・RTF(.odt/.rtf)は原本のままでは解析しない形式としてアップロードでき、このサービスで PDF にしてから読む
  (Unstructured は pandoc で読むが、pandoc は GPL のため入れない。#3351)。
- Excel(.xls/.xlsx/.xlsm/.ods)は変換しない(`office_to_pdf_spreadsheet_not_supported`)。Excel は前処理
  `excel_to_jsonl` で読む(#2470)。
- マクロは実行しない。変換ごとに一時ディレクトリに LibreOffice の利用者のプロファイルを作り、マクロの実行を
  無効にする(`MacroSecurityLevel=3`・`DisableMacrosExecution=true`)。一時ディレクトリは変換の後に消す。

- 出力契約: `rag_parser_core` の `ConvertResponse`(`POST /convert`)
- readiness: `GET /health`(LibreOffice 導入状況)
- 変換依存(LibreOffice)は本サービス単独で upgrade 可能(他 parser / backend に非干渉)
- 依存未導入・変換失敗のときは `converted=false`(passthrough)を返し、backend は原本のまま parse する

## ローカル実行(開発)

```bash
# repo root から(共有 package の path source を解決するため)
uv run --directory services/preprocess/office_to_pdf \
  uvicorn app.main:app --host 127.0.0.1 --port 18010
```

## 起動(uv の venv + systemd。#286)

サービスごとの uv の venv(`uv sync --locked --no-dev --python 3.12`)で動き、本番・開発とも systemd の unit
(`production-ready-rag-preprocess-office-to-pdf.service`)で起動 / 停止する。Docker は使わない(Dockerfile は #356 で削除した)。

```bash
# rag/ で実行する
scripts/rag-services.sh install preprocess-office-to-pdf   # venv を作り unit と sudoers を登録して起動
scripts/rag-services.sh run preprocess-office-to-pdf       # systemd の無い環境で前面に起動
```
