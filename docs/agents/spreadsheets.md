# Excel の作成・編集・分析

Codex では `spreadsheet` skill を使う。Excel の既存の書式と数式を保ち、集計値には数式を使う。成果物は root の `AGENTS.md` に従って `tmp/<作業名>/` に保存し、利用者の業務データを commit しない。

## 導入

配布元は [OpenAI の skills リポジトリ](https://github.com/openai/skills/tree/e6afb0d74cc75d220df2faf3dd6c635c2dc6a108/skills/.curated/spreadsheet)。この版のライセンスは Apache-2.0。最新の curated 一覧からは削除されているため、利用できる公開版の commit を固定して導入する。

Codex の `skill-installer` skill の `scripts/install-skill-from-github.py` を使う。

```bash
python3 <skill-installer のディレクトリ>/scripts/install-skill-from-github.py \
  --repo openai/skills \
  --path skills/.curated/spreadsheet \
  --ref e6afb0d74cc75d220df2faf3dd6c635c2dc6a108
```

既定の導入先は `~/.codex/skills/spreadsheet/`（`CODEX_HOME` を指定した場合はその下の `skills/spreadsheet/`）。次のターンから利用できる。使用する前に導入先の `SKILL.md` を読み、同梱の参照資料も必要に応じて読む。

主な依存は openpyxl。集計の分析には pandas を使える。数式の再計算と表示の確認には LibreOffice、PDF の画像化には Poppler を使う。依存は作業対象の Python 環境で確認する。

Anthropic の `document-skills:xlsx` は配布元の独自ライセンスを持つ。Agent Skills の形式が共通でも、Codex へコピーできるとは限らないため、代替として複製しない。

## 成果物の検証

- 保存した workbook を再読込し、シート名・件数・入力値と根拠を確認する。
- 数式がある場合は、利用できるツールで再計算してキャッシュを更新し、参照先と集計範囲、数式エラーがないことを確かめる。
- 表示が重要なシートは PDF や画像にして、文字の切れ・列幅・書式を確認する。
- 検証できなかった項目と不足するツールは、結果の報告に明記する。
