# preprocess-word-to-jsonl

parse の **前** に Word(`.doc` / `.docx` / マクロ付きの `.docm`)の本文を見出しの階層で読み、見出しの下の段落・リスト・表と
表の前後の説明を **意味のまとまりの記録** にした構造化レコード JSONL(`rag_parser_core.structured_records`)へ変換する
前処理マイクロサービス(#2457)。表示の行ごとには分けない。文書解析のエンジン(Docling・Unstructured)のサービスが共通の契約で
記録を 1 つずつ要素にし(#21)、文書レシピの分割「固定分割」(既定の分割符 `<<<JSONL_RECORD_BOUNDARY>>>`)で 1 記録 1 chunk になる。

- 出力契約: `rag_parser_core` の `ConvertResponse`(`POST /convert`)。派生物の content type は
  `application/vnd.production-ready.structured-records+jsonl`。JSONL は `document`(変換器・位置の基準・原本と派生物の対応・診断)→
  本文の順に `section`(見出し・階層・位置)と `passage`(見出しの下のまとまり)。
- readiness: `GET /health`(python-docx と LibreOffice。LibreOffice が無いと `.doc` を変換できないので `degraded`)
- 変換できないとき(下の「拒否」)は `converted=false` と理由のコード(`word_<理由>`)を返す。backend は成功扱いにせず、
  理由の文を出して取込を止める(`app.clients.preprocess_service`)。

## 変換仕様

| 項目 | 挙動 |
|---|---|
| 形式の判定 | 先頭のバイトで決める(拡張子では決めない)。OLE の複合文書は `WordDocument` の stream と FIB の識別子で Word であることを確かめる。ZIP は本体の content type が wordprocessingml の文書・テンプレート・マクロ付きのものだけを受ける |
| .doc | 変換ごとに一時ディレクトリに作った LibreOffice のプロファイル(マクロの実行を無効)で .docx にしてから読む。時間の上限 120 秒・同時に 1 つ・終わったら(時間を超えたら)プロセスグループごと終了させ、一時ディレクトリを消す。`document.location_basis=derived` と、原本(.doc)と派生物(.docx)の sha256(`conversion`)を残す |
| .docx | そのまま読む(`location_basis=original`)。展開の大きさ(合計 512 MB・要素 20,000)を確かめる |
| .docm / .dotm / .dotx | VBA の部品(`vbaProject.bin`・`vbaData.xml` と関係)を捨て、本体を文書にしてから読む。マクロは実行しない(診断 `macros_not_executed`) |
| 見出し | 段落のアウトラインのレベル、無ければ段落のスタイル(基のスタイルもたどる)の名前 `heading N` / `Title`・アウトラインのレベル。見出しに番号があれば前に付ける |
| リスト | 番号の定義(`numbering.xml`)の書式(`decimal`・`lowerLetter`・`upperRoman`・`decimalEnclosedCircle` など)と `lvlText` で番号を表示する(`1.`・`(1)`・`①`)。記号のリストは `-` |
| 表 | セルの文字(セルの中の段落は空白でつなぐ)。表頭は `tblHeader` の行(無ければ先頭の行を Markdown の表頭の位置に置く)。横の結合は同じ値で埋め、縦の結合の続きは結合の始まりの値で埋める(HTML の `rowspan` と同じ読み方。#3565)。行の番号は元の表のまま |
| まとまり | 見出しから次の見出しまでの本文を 1 記録にする。段落の直後の表・リストはその段落(導入の文)と、表の直後の段落(次の表・リストの導入でないもの)は表の注記としてまとめる。本文が 1,500 字(見出しの行を含む)を超える節は、このまとまりの境目で複数の記録に分け、`part`(まとまりの ID・順番・数・節の全体の位置)を持たせる。1 つの表は行(表頭を繰り返す)、段落は文の境目で分ける(文より長いときは文字数で分け、診断 `paragraph_split_by_length`)。本文も下位の見出しも無い見出しは見出しだけの記録にする |
| 位置 | 要素化した .docx の本文の順の番号(段落と表を通した `body_*`・段落だけの `paragraph_*`・表の番号)。**原本に無いページ番号は作らない** |
| 読まないもの | ヘッダー・フッター(読まない)、画像・脚注・文末脚注(件数の診断 `images_not_extracted` など)。変更履歴は挿入を残し削除を除く(診断 `tracked_changes_accepted`) |
| 拒否 | 空 `word_empty`・大きすぎる `word_input_too_large`・Word でない `word_unsupported_format` / `word_not_word_document`・暗号化 `word_encrypted`・破損 `word_corrupt`・LibreOffice が無い(.doc)`word_libreoffice_unavailable`・時間の超過 `word_conversion_timeout`・変換の失敗 / 出力なし / 空 `word_conversion_failed` / `word_conversion_no_output` / `word_conversion_empty`・本文なし `word_no_content` |

PDF 経由(Office→PDF → Docling)との違い(合成の文書で比べた結果は #2457 の PR に書いた): 見出しの階層・リストの番号・表の
セル(結合を含む)を Word の構造から読み、表と注記・導入の文を同じ記録にする。ページ番号・PDF の上での位置の強調・画像の読み取りは
無い(画像や版面が大事な文書は Office→PDF を選ぶ。同じ文書に両方のレシピを持たせて融合もできる)。

## テスト

```bash
# CI(Lint / services & scripts)と同じ
uv run --locked --with pytest python -m pytest -q tests
```

テストのデータは合成だけ(`tests/fixtures.py`)。`tests/data/synthetic.doc` は `uv run --locked python -m tests.fixtures tests/data`
(LibreOffice が要る)で作り直せる。LibreOffice が無い環境(CI)では、.doc の実変換のテストだけ skip し、ほかは変換を差し替えて確かめる。

## ローカル実行(開発)

```bash
# repo root から(共有 package の path source を解決するため)
uv run --directory services/preprocess/word_to_jsonl \
  uvicorn app.main:app --host 127.0.0.1 --port 18017
```

## 起動(uv の venv + systemd。#286)

サービスごとの uv の venv(`uv sync --locked --no-dev --python 3.12`)で動き、本番・開発とも systemd の unit
(`production-ready-rag-preprocess-word-to-jsonl.service`)で起動 / 停止する。`.doc` の変換に LibreOffice(Writer)を OS に入れる
(`init_script.sh` が `libreoffice-core`・`libreoffice-writer` を入れる)。

```bash
# rag/ で実行する
scripts/rag-services.sh install preprocess-word-to-jsonl   # venv を作り unit と sudoers を登録して起動
scripts/rag-services.sh run preprocess-word-to-jsonl       # systemd の無い環境で前面に起動
```
