# 前処理(Preprocess)動作確認サンプル

サービス管理画面に並ぶ前処理マイクロサービスのうち 3 つを、実入力で確認するためのサンプルファイル。
各サービスの `convert()` を実依存込みで叩く(LibreOffice / openpyxl)。

| サンプル | サービス (slug) | 機能 |
|---|---|---|
| `document.docx` | `preprocess-office-to-pdf` | Office → PDF |
| `table.csv` | `preprocess-csv-to-jsonl` | CSV → 構造化レコード JSONL |
| `table.xlsx` | `preprocess-excel-to-jsonl` | Excel → 構造化レコード JSONL |

## 実行方法

各サービスは独立 venv。`<service>` ディレクトリで `uv run` する。
`EX` は本フォルダの絶対パス(`examples/test-preprocess`)に読み替える。

```bash
# 例: Excel → 構造化レコード JSONL
cd services/preprocess/excel_to_jsonl
EX=../../../examples/test-preprocess uv run python - <<'PY'
import os, app.converters as c
data = open(f"{os.environ['EX']}/table.xlsx","rb").read()
o = c.convert(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "excel_to_jsonl", None)
print(o.converted, o.derived_content_type, o.warnings)
print((o.derived_bytes or b"").decode("utf-8","replace"))
PY
```

`preprocess_profile`(第3引数)は各サービスの slug 末尾と一致させる:
`office_to_pdf` / `csv_to_jsonl` / `excel_to_jsonl`。
プロファイルが一致しない・依存欠如・変換失敗時は `ConvertOutcome.passthrough`(`converted=False`)へ縮退する。

### 注意

- `office_to_pdf` は `source_profile.extension`(例 `.docx`)で入力 suffix を決める。`SourceProfile` を渡すか、既定では `.docx` 扱い。
