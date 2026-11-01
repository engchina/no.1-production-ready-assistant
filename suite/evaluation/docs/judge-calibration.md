# 人手の判定による校正の手順（#3418・#2577）

LLM による回答の判定（`suite-evaluation judge`）と RAG の回答の評価器（`app.rag.answer_judge_calibration`）を、人手の判定（ラベル）との一致で校正する手順。校正に通った観点だけ、判定を合否に使う（通らない観点は参考値）。

**人の判定はコードで作らない。** ラベルは記入者（2 人以上）が付け、食い違いは裁定者が決める。コードが作るのは記入用のブック・合成の対照（故意に壊した回答。`origin=synthetic` として人手の判定と分け、合否に入れない）・一致の集計だけ。

## 全体の流れ

| 段 | 誰が | すること | 命令 |
|---|---|---|---|
| 1 | 運用者 | 校正のケースを作り、A・C・D に流す | `rag` の `evaluation_corpus_cli`・`suite-evaluation run` |
| 2 | 運用者 | 記入用のブックを書き出す | `suite-evaluation label-template` |
| 3 | 記入者 2 人 | それぞれ別のブックに判定を付ける | （Excel） |
| 4 | 運用者 | 記入者の間の一致を測り、裁定用のブックを作る | `suite-evaluation label-import --adjudication-output` |
| 5 | 裁定者 | 食い違いの行を決める | （Excel） |
| 6 | 運用者 | 人手の判定（LabelSet の JSON）を書く | `suite-evaluation label-import --adjudicated --output` |
| 7 | 運用者 | 判定のモデルで校正し、結果を記録する | `suite-evaluation calibrate`・`app.rag.answer_judge_calibration` |

**RAG の画面でも同じ手順を行える（#3578）。** 「改善・運用 › 品質評価」のタブ「判定の校正」で、終わった品質評価の job から記入の集合を作り（段 2）、記入者がブックの代わりに画面で記入し（段 3。自分の判定だけを見る）、記入者の間の κ と食い違いを見て裁定し（段 4〜6）、RAG の回答の評価器を校正する（段 7）。区分・誤りの型・合成の対照・記入者の間の κ・閾値は CLI と同じ（platform の `pr_evaluation_scoring`）。画面の「JSON を書き出す」「JSON を取り込む」は、この文書の LabelSet の JSON と同じ形で、CLI と画面の間で人手の判定をやり取りできる。

作るファイル（回答・根拠の本文を含む）は repo に commit せず、repo の直下の `./tmp/<作業の名前>/` に置く。ブックの内容（質問・回答・根拠）を Issue・チャット・repo に書き写さない。

## 観点ごとの目標

`calibrate` の既定の閾値（holdout の観点ごと）: 件数 50 以上・判定率 0.95 以上・Cohen の κ 0.6 以上・誤合格の率 0.1 以下。これに届くように集める。

| 観点 | holdout の行 | pass | fail | 対象の項目 |
|---|---|---|---|---|
| `requirement_coverage`（必要な論点の充足） | 50 以上 | 20 以上 | 20 以上 | 標準回答のある項目 |
| `answer_correctness`（事実・推論の正しさ） | 50 以上 | 20 以上 | 20 以上 | 標準回答のある項目 |
| `claim_support`（根拠への忠実性） | 50 以上 | 20 以上 | 20 以上 | すべての項目 |

- fail が 20 件なら、誤合格の率 0.1 は 2 件まで。fail が少ないと 1 件の誤合格で閾値を超えるので、fail を集めるケースを意図して作る。
- cannot_judge の行は件数に入らない。区分は `--holdout-ratio`（既定 0.7）で決まるので、holdout で 50 行を得るには観点ごとにおよそ 72 項目（cannot_judge の分の余裕を見て 80 項目）が要る。
- #2577 の誤りの型と対照（ブックの「誤りの型と対照」の sheet。記入者が誤りの型の列で付ける）を、holdout でそれぞれ次の件数以上含める:

| 区分 | ID | 内容 | 主に fail になる観点 | holdout の目標 |
|---|---|---|---|---|
| 誤りの型 | `attribute_swap` | 対象・属性・比較の相手の取り違え | answer_correctness・claim_support | 5 |
| 誤りの型 | `general_rule_overreach` | 一般の規則を質問の個別の範囲を超えて当てはめる | answer_correctness | 5 |
| 誤りの型 | `step_order` | 手順の前後関係の誤り | answer_correctness | 5 |
| 誤りの型 | `scope_expansion` | 処理の段階・時間の単位を根拠なく広げる | answer_correctness・claim_support | 5 |
| 誤りの型 | `unsupported_claim` | 根拠に無い主張・根拠と矛盾する主張 | claim_support | 5 |
| 誤りの型 | `partial_omission` | 必要な論点の一部の不足 | requirement_coverage | 5 |
| 対照 | `correct_paraphrase` | 正しい言い換え（誤不合格を見る） | （すべて pass） | 10 |
| 対照 | `correct_refusal` | 正しい拒答・利用者への確認 | （すべて pass） | 5 |

## 1. 校正のケースを作る

- 資料と評価セットは合成でよい（業務の文書・質問を使わない。#2019）。既存の合成の評価セット（`rag/evaluation/business-support/`・`multi-hop/`）に足すか、校正用の評価セットを別に作る。
- 標準回答（`standard_answer`）を必ず書く（requirement_coverage・answer_correctness は標準回答のある項目だけを判定する）。`category` を書くと、区分をその層ごとに分ける。
- 上の誤りの型が起きやすい質問を意図して入れる: 2 つの対象の別々の属性を与える質問、一般の規則と個別の範囲を区別する質問、順序のある手順、処理の段階・日と月の境界をまたぐ質問、資料に無いことを尋ねる質問（正しい拒答）、言い換えで答えられる質問。
- 1 つのケースから A（RAG・ナレッジベース）・C（RAG・検索・回答プロファイル）・D（業務 Agent）の 3 つの回答が取れる。30〜40 ケースで観点ごとの目標に届く。足りないと後から足し直しになる（下の「足りないとき」）ので、目標の 1.5 倍を目安に作る。
- 取り込みと実行は README の「使い方」の 1・2 と同じ（`evaluation_corpus_cli` → `suite-evaluation run --target rag-mcp` / `agent-mcp`）。

## 2. 記入用のブックを書き出す

```bash
cd suite/evaluation
W=../../tmp/judge-calibration
uv run suite-evaluation label-template A=$W/a.json C=$W/c.json D=$W/d.json \
    --dataset "$SET" --document-map "$MAP" --perturb all --output $W/labels.xlsx
# → $W/labels.xlsx（記入用のブック）と $W/labels.items.json（項目のファイル。機械の正本）
```

- 1 行 = 項目 × 観点。項目は保存した結果の成功したケースの最後の回答で、判定のモデルに渡すものと同じ（質問・回答の本文・根拠・標準回答）。失敗したケース・空の回答は項目にしない（件数は出力の `skipped`）。
- 区分（dev / holdout）はケースごとに決まる: `category` の層の中で `sha256(<salt>:<ケースの ID>)` の順に並べ、先頭の `1 - holdout_ratio` を dev にする（決定的。`--split-salt`・`--holdout-ratio`）。同じケースの A・C・D の回答と、その合成の項目は同じ区分になる。
- `--perturb`（`all` か `swap_values,drop_condition,add_unsupported_claim,reverse_steps`）は、項目の回答をコードで壊した合成の対照を足す（壊し方ごとに `--perturb-max` 件まで）。壊した観点を fail と決め、ブックの「合成の対照」の sheet に出す（記入者は判定しない）。

| 壊し方 | 変え方 | fail にする観点 |
|---|---|---|
| `swap_values` | 回答の中の違う 2 つの数値を入れ替える（番号つきの行の番号・ID の数字は除く） | answer_correctness・claim_support |
| `drop_condition` | 条件・範囲を表す文のうち、標準回答といちばん重なる 1 文を消す | requirement_coverage |
| `add_unsupported_claim` | 根拠に無い断定の文（例外が無い・変わらない・すべてに当てはまる）を足す | claim_support |
| `reverse_steps` | 番号つきの手順の並びを逆にする（番号は保つ） | answer_correctness |

- 合成の fail はコードの仮定（壊したので誤りのはず）で、人の判定ではない。校正の合否（holdout の人手の判定）には入れず、`synthetic` の区分として「判定が壊した回答を合格にしないか」（誤合格の率）を別に見る（参照の要らない反例）。
- `labels.xlsx` を記入者の数だけ複製して配る（例: `a1.xlsx`・`a2.xlsx`）。`labels.items.json` は運用者が持つ（取り込みで使う）。

## 3. 記入する（記入者）

ブックの「判定の基準」の sheet に付け方と観点の基準がある。要点:

- 判定の列で、観点ごとに独立に pass / fail / cannot_judge を選ぶ（入力が足りず決められないときだけ cannot_judge。校正に使わない）。
- 判断は、その行の質問・回答の本文・根拠の抜粋・標準回答だけにもとづく。一般の知識で補わない。
- LLM の判定・ほかの記入者の判定を見ない（ブックに LLM の判定は入れていない）。記入者どうしで相談しない。
- 誤りの型・対照の列（任意）で当てはまる型を選ぶ。メモに判断の理由を短く書く。記入者の列に匿名の ID（英数字）を書く。
- 区分・項目の ID・観点の列を変えない。行を足したり消したりしない。
- 「合成の対照」の sheet は判定しない。壊した結果が誤りにならない回答（例: 順序に意味の無い箇条書きを逆にした）を見つけたら、除外の列に `x` を書く。
- 「誤りの型と対照」の sheet の式で、記入済みの件数（holdout）と観点ごとの pass / fail の件数を確かめる。

根拠の抜粋はセルの上限（約 32,000 字）で切ることがある（切った件数を書く）。切ったときの全文は項目のファイルにある。

## 4. 記入者の間の一致を測り、裁定用のブックを作る

```bash
uv run suite-evaluation label-import --items $W/labels.items.json \
    --annotations a1=$W/a1.xlsx a2=$W/a2.xlsx \
    --report $W/iaa.json --adjudication-output $W/adjudication.xlsx
```

- 観点ごと・記入者の組ごとに、両方が pass / fail を付けた行の一致率と Cohen の κ を出す（cannot_judge・未記入の行は数を別に出す）。κ が 0.6 に届かない観点は、基準の読み方がそろっていない。dev の項目で記入者と基準の読み方を話し合い、「判定の基準」の補足をそろえてから付け直す（holdout の判定を、LLM の判定を見て直さない）。
- 食い違い（全員が同じラベルでない行。未記入を含む）は `iaa.json` の `disagreements` と、裁定用のブックの「要裁定」の行。一致した行は判定を埋めてある。
- ブックは、項目のファイルの sha256（`_meta` の sheet）・行の過不足・ラベルの語彙で確かめ、違えば取り込まない。

## 5. 裁定する（裁定者）

`adjudication.xlsx` の「状態」が「要裁定」の行の判定を決める（各記入者の判定とメモが横にある）。記入者の列に裁定者の匿名の ID を書く。一致した行も、明らかな誤りなら直してよい（直した行は記入者ごとの元の判定と並べて LabelSet に残る）。合成の対照の除外の列も見直す。

## 6. 人手の判定（LabelSet）を書く

```bash
uv run suite-evaluation label-import --items $W/labels.items.json \
    --annotations a1=$W/a1.xlsx a2=$W/a2.xlsx \
    --adjudicated adj1=$W/adjudication.xlsx --output $W/labels.json
```

- 全員が一致していれば `--adjudicated` なしで書ける。決まらない行が残れば書かない（終了コード 2）。
- 項目ごとに最終の pass / fail（cannot_judge は書かない。全観点が cannot_judge の項目は落とす）、記入者ごとの元の判定（`annotator_labels`）、誤りの型、メモ、裁定者を残す。合成の対照は除外したもの以外を `origin=synthetic` で足す。`provenance` に項目のファイルの sha256・記入者・裁定者・記入者の間の κ を残す。
- 目標に届いているかを、ブックの「誤りの型と対照」の sheet と `labels.json`（holdout の観点ごとの pass / fail の数）で確かめる。

### 足りないとき

書き出しをやり直すと項目のファイル（sha256）が変わり、前のブックは取り込めない。ケースを足すときは、足したケースの結果だけで別の書き出し・記入・取り込みをし、LabelSet の `items` を 1 つにまとめる（項目の ID は `<結果の名前>:<ケースの ID>` なので、結果の名前を変えれば重ならない）。まとめた LabelSet の `provenance` には両方の項目のファイルの sha256 を書く。

## 7. 校正して記録する

### suite の判定（LLM-as-judge）

```bash
export SUITE_EVALUATION_JUDGE_ENDPOINT=...   # モデルの API の Endpoint URL
export SUITE_EVALUATION_JUDGE_API_KEY=...    # 引数・ログに出さない
export SUITE_EVALUATION_JUDGE_MODEL=...      # 被評価の回答を作ったモデルと別の系統を選ぶ
uv run suite-evaluation calibrate $W/labels.json --repeats 3 --output $W/calibration.json
```

- 観点ごと・区分ごと（dev・holdout・synthetic）に一致率・κ・混同行列・誤合格 / 誤不合格・判定率を出し、holdout の人手の項目で合否を決める（`passed`・`failures`）。synthetic は合否に入れない参考（壊した回答の誤合格の率）。
- **判定のモデルの seed は使わない**（モデルの API の呼び出しから外した。#3529）。再現の条件として、判定のモデルの ID・rubric の sha256・指示と出力の形の sha256（`prompt_sha256`）を結果の `provenance` に残し、`--repeats N`（2 以上）で同じ項目を N 回判定して、観点ごとに回の間でラベル（か評価不能）が変わった項目の率（`variance.<観点>.unstable_rate`）を記録する。合否は最初の回で決める。揺れの率が高い観点は、κ が閾値を超えても合否に使う前に rubric を見直す。
- 人手の判定を直したら、`--judgements $W/calibration.json` でモデルを呼ばずに集計だけやり直す（`--repeats` の判定も引き継ぐ）。rubric を変えたら判定からやり直す（rubric の sha256 が違う校正の結果は使えない）。
- dev の項目は rubric の調整に使ってよい。holdout の項目を見て rubric を直したら、その holdout はもう holdout ではない（新しいケースで holdout を作り直す）。

### RAG の回答の評価器

```bash
cd rag/backend
uv run python -m app.rag.answer_judge_calibration ../../tmp/judge-calibration/labels.json \
    --output ../../tmp/judge-calibration/rag-calibration.json
```

- 項目ごとに RAG の評価器（画面の品質評価と同じ `evaluate_answer_payload` と指標の計算。接続とモデルは RAG の設定）を動かし、指標の値が `--pass-at`（既定 1.0）以上を pass として、同じ一致の集計・同じ閾値（platform の `pr_evaluation_scoring`）で合否を出す。`--evaluations <前の結果>` でモデルを呼ばずに集計だけやり直す。

### Agent の品質評価の評価者

Agent の回答の要点の判定・出力の項目の意味の判定は、LabelSet と同じ名前の項目に観点（`perspective`）と人の根拠（`note`）を足した校正セットで、同じ閾値・同じ結果の形で校正する（`agent/backend` の `python -m app.cli.judge_calibration`。#66）。手順は [agent/docs/agent-judge-calibration.md](../../../agent/docs/agent-judge-calibration.md)。

### 記録する

結果のファイルは `./tmp/` に置いたまま、#3418（RAG の評価器は #2577）にコメントで次を残す（回答・根拠・質問の本文、業務の内容、endpoint・Project ID などの実環境の情報は書かない）:

- 判定のモデルの ID、rubric の id・版・sha256、`prompt_sha256`、判定の設定（`max_attempts`・`max_evidence_chars`）
- LabelSet の sha256、人手 / 合成の項目の数、観点ごとの holdout の行と pass / fail の数、記入者の数と記入者の間の κ（観点ごと）、裁定した行の数
- 観点ごと: holdout の κ・判定率・誤合格の率・誤不合格の率・合否と理由、synthetic の誤合格の率、`--repeats` の回数と揺れの率
- RAG の評価器の同じ値（`--pass-at` の値を添える）

校正に通った観点だけ、`suite-evaluation judge --calibration $W/calibration.json` が合否を出す（同じ rubric・同じモデルのときだけ。違えば `calibration_status=mismatch`）。

## 時間の目安

| 作業 | 目安 |
|---|---|
| ケースと標準回答を作る（30〜40 ケース。合成の資料を含む） | 3〜5 時間 |
| A・C・D に流す | 30 分〜1 時間（待ちを含む） |
| 記入（1 人。約 100 項目 × 1〜3 観点。1 項目 3〜4 分） | 1 人あたり 5〜7 時間（2〜3 回に分ける） |
| 裁定（食い違いが 1〜2 割として 30〜60 行） | 1〜2 時間 |
| 取り込み・校正（判定のモデルを 3 回。項目と合成の対照で約 200 件 × 3） | 30 分〜1 時間（待ちが主） |
| 記録 | 30 分 |

合計で、記入者 2 人の作業を含めておよそ 2〜3 人日。
