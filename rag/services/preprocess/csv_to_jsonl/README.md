# preprocess-csv-to-jsonl

parse の **前** に CSV 原本を決定論で構造化レコードの JSON Lines(1 データ行 = 1 記録)へ
変換する前処理マイクロサービス(#2457)。

- 出力契約: `rag_parser_core` の `ConvertResponse`(`POST /convert`)。派生物は構造化レコードの JSONL
  (`application/vnd.production-ready.structured-records+jsonl`。schema は `rag_parser_core.structured_records`)で、
  Docling / Unstructured の parser のサービスが外部の parser に渡さずに 1 記録 1 要素にする。
- readiness: `GET /health`(外部依存なしで常時 ready)
- 純 Python(`csv` + pydantic)のみ。LibreOffice を含まない軽量な venv。

## 変換仕様

| 項目 | 挙動 |
|---|---|
| 文字コード | UTF-8(BOM 可)優先、失敗時 charset-normalizer で推定 |
| デリミタ | `,` `\t` `;` `\|` を Sniffer で推定(失敗時はカンマ) |
| 出力 | 1 行目が `document`(変換器・表頭の物理行・診断)、続く行が `row`(`fields` = 列名・列の位置・値、`location` = 原本の物理行の範囲)の JSONL。UTF-8・BOM なし・LF・末尾 LF。値の改行は JSON のエスケープのまま 1 行に収める |
| 表頭・列名 | 最初の空でない行が表頭。空の列名は `col_<位置>`、重複は `<名前>_<n>`。補う前の表頭のセルの文字は `document` の `header` に残す(根拠のセルの表頭。#3262) |
| 行の位置 | `row_start` / `row_end` は原本の物理行(1 始まり。引用符の中の改行を含む記録は `row_start` < `row_end`)。`record` は記録の番号(RFC 7111 の行。表頭の記録を含め、空の行も 1 記録として先頭から数える。表計算のソフトで開いたときの行。#3262)。空の行は記録にせず、行番号・記録の番号は振り直さない |
| 列の多い行 | 表頭より多い値は `col_<位置>` の列にして捨てない(警告 `csv_row_wider_than_header:<件数>`) |
| 縮退 | 空・復号失敗・データ行 0・CSV として読めないときは `converted=false`(passthrough) |

## ローカル実行(開発)

```bash
# repo root から(共有 package の path source を解決するため)
uv run --directory services/preprocess/csv_to_jsonl \
  uvicorn app.main:app --host 127.0.0.1 --port 18012
```

## 起動(uv の venv + systemd。#286)

サービスごとの uv の venv(`uv sync --locked --no-dev --python 3.12`)で動き、本番・開発とも systemd の unit
(`production-ready-rag-preprocess-csv-to-jsonl.service`)で起動 / 停止する。Docker は使わない(Dockerfile は #356 で削除した)。

```bash
# rag/ で実行する
scripts/rag-services.sh install preprocess-csv-to-jsonl   # venv を作り unit と sudoers を登録して起動
scripts/rag-services.sh run preprocess-csv-to-jsonl       # systemd の無い環境で前面に起動
```
