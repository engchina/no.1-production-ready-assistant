# preprocess-excel-to-jsonl

parse の **前** に Excel(`.xls` / `.xlsx`)原本を決定論で「行の記録」(シートごとの表頭・元の行番号・
セル範囲・表示の値)の構造化レコード JSONL へ変換する前処理マイクロサービス(#1221・#2457)。読み方は engchina/no.1-rag の前処理
(`utils/office_preprocess_util.py`)に合わせる。

- 出力契約: `rag_parser_core` の `ConvertResponse`(`POST /convert`)。派生物の content type は
  `application/vnd.production-ready.structured-records+jsonl`(schema は `rag_parser_core.structured_records`)で、
  Docling / Unstructured の parser のサービスが外部の parser に渡さずに 1 記録 1 要素にする。
  JSONL は `document`(変換器・読まなかったシート)→ シート(複数の表に分けたときは表)ごとに `sheet`(表頭・
  推定の信頼度・読み方・版式の判定・表の番号と範囲・列と役割・診断)→ `preamble` / `row` / `procedure_step` /
  `form_field` / `note`(表の下の注記。#3566)(`parent_id` = シートの記録、`location` = 元の行番号とセル範囲。図形の文字を含む記録は `shape_cells`)の順。
- 読み方の本体は platform の `pr_backend_core.spreadsheet.read_excel(bytes, extension=…, options=…)`(#2525)。
  原本の bytes と選択肢だけで `SheetRecordsDocument` を返し、チャットの添付などの別の経路も同じ読み方を使う。
  このサービスの `app.converters.convert` はそれを呼び、構造化レコードの JSONL にするだけ。読み方のテストは
  `platform/packages/backend_core/tests/test_spreadsheet.py` と、このサービスの `tests/`(JSONL まで通す)。
- 選択肢: `POST /convert` の form の `options`(JSON)。backend は文書レシピの `excel_options`(無ければ
  `RAG_PREPROCESS_EXCEL_OPTIONS`)を渡す。
- readiness: `GET /health`(openpyxl / xlrd 導入状況)
- 依存未導入・解析失敗・空・選択肢の誤りのときは `converted=false`(passthrough)を返す

## 変換仕様

| 項目 | 挙動 |
|---|---|
| 対応形式 | `.xlsx` / マクロ付きの `.xlsm`(openpyxl。結合セル・書式を読むため read_only にしない。VBA は読まない)/ `.xls`(xlrd 2.x、formatting_info。マクロは読まない)。マクロは実行しない(#2470) |
| 読み方(`mode`) | `table` は 1 行 1 記録。`procedure` は手順書(表頭の無い番号の列と、表頭のある最初の列=題名)を、番号と題名のある行から次の手順までで 1 記録にし、詳細の無い題名の行を章にする。`form` は帳票(下の行)。`auto`(既定)はまず帳票の点数が `form_detection_threshold`(既定 0.5)以上なら form、次に番号と題名の列があれば procedure、無ければデータの行の埋まり方(中央値 60% 以上)で table。判定の根拠(特徴の値)と信頼度はシートの `layout_detection` に残し、帳票と判定したときは診断 `layout_detected_form` も残す |
| 帳票(方眼紙。#2516) | 判定: 結合セルを 1 つの単位にし、結合セルの割合・ラベルのすぐ右の値(すぐ右に塗りつぶし・太字・末尾のコロンの無い値の単位があり、値が結合しているかラベルに手がかりがある。ラベルの数を単位の半分で割り 1 を上限にする。表頭の下に値の並ぶ表は数えない)・行どうしの単位の位置の不揃い・狭い列(列幅 4 文字以下)の割合を 0.3 / 0.4 / 0.15 / 0.15 の重みで点数にする(#2530。狭い列は加点で、普通の列幅の帳票も判定する。単位 4 個未満は帳票にしない。狭い列が半分未満で位置の不揃いが 20% 未満の格子は普通の表らしいので点数を半分にする)。業務の語は使わない。読み方: ラベルの単位と、すぐ右(空の列 2 つまで)の値を組にし、右に無ければすぐ下の同じ左端の値と組にして `form_field`(ラベル・値・`label_cell`・`value_cell`・全体のセル範囲)にする。縦に結合したラベルは右の複数行の値をまとめ、値のすぐ右の単位のセル(「円」など)は値に続ける。値の無い結合セルが右かすぐ下(押印欄の枠など)にあるラベル(空欄)は記録にせず `form_empty_fields` の数に残す。1 つのセルの「ラベル：値」(全角・半角のコロン。時刻・URL は分けない)はそれだけで項目にし、右の単位と組にしない(#2529)。ラベルの末尾のコロンは除く。行に 1 つだけの単位は以降の項目の見出し(`section_path`)、縦に結合して右に複数の項目を持つ単位は項目のまとまりの見出し。塗りつぶし・太字のラベルが横に並び、すぐ下に同じ位置の値の行があれば上下で組にし、値の行が 2 行以上なら表の行(`row`)にする。記録の ID は `sheet-000-r3-B`(1 行に複数の項目があるので列を付ける) |
| 選択の記号(#2526) | 帳票の値が選択の記号の付いた選択肢(`■A □B`・`☑A ☐B`・`✓A ☐B`・`●A ○B`・`[x] A [ ] B`)なら、選ばれた選択肢を値にし(`■A □B` → `A`、複数なら「、」でつなぐ、記号の前の文は前置きとして残す)、原文を `form_field` の `value_original`(要素の metadata `form_value_original`)に残す。記号と選択肢を別々のセルに置いた値(`□`・`A`・`☑`・`B`)は 1 つの値にしてから読む。記号は汎用の集合(選んだ: `■ ☑ ☒ ✓ ✔ ● ◉ ◼ ▣`、選んでいない: `□ ☐ ○ ◯ ◻ ⬜`)で判断し、業務の語は使わない。記号の付いた選択肢が 2 つ以上あり、選んだものと選んでいないものが両方あるときだけ変える(`■概要`・`□A □B`・`✓` は変えない)。読んだ数は診断 `form_choices_read` |
| 図形・テキストボックスの文字(#2526・#2537) | openpyxl は図形の文字を読まないので、`.xlsx` の zip のシートの drawing(`xl/drawings/drawingN.xml`)を直接読む。`twoCellAnchor` は from / to(to のオフセットが 0 ならそのセルは入れない)、`oneCellAnchor`(from + 大きさ)と `absoluteAnchor`(シートの左上からの位置 + 大きさ)は大きさ(EMU)をシートの列幅(文字数 → ピクセル。既定 64 ピクセル)と行の高さ(ポイント。既定 15)でセルの範囲に換算し(非表示の行・列は 0)、図形(グループの中も。`mc:AlternateContent` は wsDr の直下でも図形の中でも `mc:Choice` だけ。非表示の図形は読まない)の `txBody` の段落を改行でつないだ文字を、その範囲の 1 つの単位(結合セル)としてセルの格子に置く。表・手順書・帳票の読み方はそのまま使い(帳票の空欄に重ねた図形はその項目の値、表の上の図形は前書き、離れた図形は 1 つの項目)、記録の `shape_cells`(要素の metadata `sheet_shape_cells`)に置いた範囲を残す。範囲に値のあるセルがあれば範囲の中の最初の空いたセルに置き、置けなければ診断 `shape_text_not_placed`(範囲と文字)。`.xls` の図形は読まず、図形の文字があれば診断 `shape_text_not_supported_xls`(数) |
| フォームコントロール(#2537) | シートの `<controls>` の `control`(`shapeId`・`r:id`)と ctrlProp(`xl/ctrlProps/ctrlPropN.xml`)の `objectType`・`checked` を読み、チェックボックスはオン `☑` / オフ `☐`、オプションボタンは `●` / `○` の記号を、図形(`cNvPr` の `id` = `shapeId`)のラベルの前に付ける。図形の無い(ラベルの無い)コントロールは `controlPr` の `anchor` の位置に記号だけを置く。記号とラベル(または右のセルの選択肢)は帳票の選択の記号の読み方で値になる(`☐ 甲 ☑ 乙` → `乙`、原文は `value_original`)。リンクしたセルの `TRUE` / `FALSE` はセルの値のまま |
| 非表示の行・列(#2526) | 既定は非表示の行・列のセル(と、その上だけにある図形)を読まない(`include_hidden_cells=true` で読む)。読まなかった行・列は診断 `hidden_rows_skipped` / `hidden_columns_skipped`。オートフィルターの範囲(表頭の行の下)で隠れた行は、条件で隠れた行なので読み、診断 `hidden_filter_rows_read` に残す。読まなかった行・列は表を分ける空きに数えない。`.xls` は formatting_info で開けたときの `rowinfo_map` / `colinfo_map` |
| 1 シートの複数の表(#2516) | `split_tables`(既定 true)で、空の行(`table_gap_rows`、既定 1)・空の列(`table_gap_columns`、既定 1)で領域に分け(XY-cut。結合セルは分けない)、領域ごとに版式を判定して表頭を推定し、別々の `sheet`(同じシート名・`table_index`・`region`・題名 `title` = 表頭より上の最初の行)にする。セル 1 つの 1 行の領域(題名)は下の表につなぎ、セルが 2 つ以上の 1 行の領域(空の行の後の表の続きの行・合計の行)と、表頭らしい行で始まらない領域(空の 1 行で区切った表の続き)は上の表につなぐ(#2528)(列の組が違う・空の行が 2 行以上・題名の行で始まる・後の行に数値の入る表頭の行で始まる領域は新しい表)。シート全体が帳票のとき・`header_row` を指定したときは分けない。要素の節は `[シート名, 題名]`、metadata の `sheet_table_index` で親子階層の親を表ごとに分ける |
| 表頭 | `header_row`(1 始まり)・`header_row_count`(1〜5)の指定を優先。無ければ先頭 30 行から採点して選び(行ごとの列は索引から引き、行の数に比例する時間で選ぶ。#2527)、`header_row_count` を指定しなければ、表頭の行の横の結合の下の行がすべて文字で、その下に数値・日付があるとき 2 段の表頭にする(#2532)。信頼度と理由を残す(0.6 未満は警告 `excel_header_low_confidence`。抽出の warning にもなり、文書レシピを確認(REVIEW)で止める。#1229)。表頭より上の行は `preamble` |
| 列名 | 複数行の表頭は「 / 」でつなぐ。空は `column_<列>`、重複は `<名前>__<列>` |
| 表の下の注記(#3566) | 表の後ろの、セルが 1 つ(横の結合を含む)の行のうち、注記の印で始まる行と、印が無くても長い文(30 文字以上)で横に結合しているか表との間に空の行がある行は、表の行・表頭にせず `note` の記録(`text`・先頭の印 `marker`)にする。印は選択肢 `note_markers`(行の先頭に当てる正規表現。既定は `※` `＊` `*`・`注` `(注)` `注1:`・`Note:`・`(1)` `1)` の形)で替えられる。空の行で区切った注記の行だけの領域は新しい表にせず上の表につなぎ、表と表の間の印の行(番号だけの印は上の表に近いときだけ)は下の表の題名にしない。数字の付いた印(`※1`・`注2` など)をセルに含む行は、`row` の `notes` にその注記の文を持ち、本文の後ろに続ける(1 行 = 1 chunk のまま注記が読める)。1 列の表と手順書(手順の続きの行)は注記に分けない |
| 行・場所 | 空行を除く前の元の行番号(`row_start` / `row_end`)とセル範囲(`A5:F5`) |
| セル値 | 表示の書式(ゼロ埋め `000`・桁区切り・小数桁・百分率)で文字列にする。日付は ISO 8601、真偽は `TRUE` / `FALSE`。`.xls` の日付もシリアル値にしない |
| 数式 | キャッシュ値を読む。キャッシュの無い数式は数式の文字列にし、セルの位置付きの診断(`formula_without_cached_value`)を残す。マクロは実行しない |
| 結合セル | 表頭の中は結合の範囲を埋める。データの行の横の結合は版式・表頭の判定のためには埋めるが、記録では左上の列にだけ値を置き、結合した列に同じ値を繰り返さない(#2531)。縦の結合の値は後の行へ漏らさない |
| シート・列 | 既定は表示されているシート。`sheets`(読む)・`exclude_sheets`(読まない)・`include_hidden_sheets`・`include_hidden_cells`(非表示の行・列。下の行)・`exclude_columns`(列名か列の記号) |
| 読む範囲 | `ranges`(`A3:F200` は選んだすべてのシート、`シート名!A3:F200` / `'名前 1'!A3:F200` はそのシートだけ。シート名を付けた範囲があるシートはそれだけを使う)。範囲の外のセルは表頭の推定にも記録にも使わない。結合セルは範囲と重なる部分に切り詰める。無いシート名は警告 `excel_range_sheet_not_found` |
| 列の役割(#1281) | 設定値の表の列に役割(`definition` 説明 / `default` 既定値 / `example` 例示 / `current` 記載時点の設定値 / `recommended` 推奨値 / `allowed` 設定できる範囲)を付け、`columns[].role`・`role_method`(`configured` / `detected`)・`role_term` に残す。`column_role_detection=auto`(既定)は表頭の語(既定値・初期値・デフォルト・記入例・設定例・サンプル・現在値・本番値・推奨値・設定可能範囲・説明 など。英語も)で決める。語が完全に一致(括弧の単位は無視)か、3 文字以上の語が表頭の末尾にあるときだけ付け、「設定値」「値」のように資料ごとに意味の違う語(`column_role_uncertain`)・2 つの役割の語を含む表頭(`column_role_ambiguous`)・役割の列が 1 つだけのシート(`column_role_not_corroborated`)・表頭の推定の信頼度が低いシート(`column_roles_skipped_low_header_confidence`)には付けない。`column_roles`(列名か列の記号 → 役割。`none` は付けない)は判定より優先し、どのシートにも無い列は警告 `excel_column_role_target_not_found`。parser は値の列の本文に `既定値［資料の既定値］: 30` のように役割の表示を付ける(`rag_parser_core.sheet_records.COLUMN_ROLE_TEXT_LABELS`) |
| 縮退 | 空・依存欠如・解析失敗・記録 0・選択肢の誤りのときは passthrough |
| 分割(#1349) | 1 記録(表の 1 行・手順書の 1 手順)を 1 chunk にし、他の行・前書きと結合せず、途中で切らない(engchina/no.1-rag の 1 行 = 1 chunk と同じ考え方)。本文は表頭の列名つきの値(`列名: 値 / 列名: 値`。空のセルは書かない)、場所はその行のセル範囲(`A5:F5`)。表頭より上の行(前書き)は続く行をまとめて 1 chunk。空の行は記録にしない(元の行番号は変えない)。親子階層(small-to-big)では、同じシート(手順書は同じ章)の続く記録を親(表の一部。`parent_target_chars` / `parent_max_children` まで)にまとめ、行を子にする。chunk も親もシートをまたがない |

## テスト

```bash
# CI(Lint / services & scripts)と同じ
uv run --locked --with pytest python -m pytest -q tests
```

## ローカル実行(開発)

```bash
# repo root から(共有 package の path source を解決するため)
uv run --directory services/preprocess/excel_to_jsonl \
  uvicorn app.main:app --host 127.0.0.1 --port 18013
```

## 起動(uv の venv + systemd。#286)

サービスごとの uv の venv(`uv sync --locked --no-dev --python 3.12`)で動き、本番・開発とも systemd の unit
(`production-ready-rag-preprocess-excel-to-jsonl.service`)で起動 / 停止する。Docker は使わない(Dockerfile は #356 で削除した)。

```bash
# rag/ で実行する
scripts/rag-services.sh install preprocess-excel-to-jsonl   # venv を作り unit と sudoers を登録して起動
scripts/rag-services.sh run preprocess-excel-to-jsonl       # systemd の無い環境で前面に起動
```
