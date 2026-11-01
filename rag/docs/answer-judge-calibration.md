# 回答の評価器の校正（#65）

品質評価の回答の評価器（`rag_engine.evaluation.answer_eval.evaluate_answer_payload` と指標を足す `app.rag.answer_metrics.score_answer_evaluation`）は、標準回答に照らした正しさ・必要な項目の充足・根拠への忠実性を LLM で判定する。評価器が理由つきの構造化出力を返せても、人の判定と一致するとは限らない。ここでは、人が判定した校正セットに対する**評価器自身の**誤合格・誤不合格を測る。

**回答の性能の評価と、評価器の性能の検査は別の作業である。** スタブ（台本の評価器）のテストは集計の検査で、評価器の能力の証明ではない。校正の結果が無い・閾値に届かない観点の値は参考値として読む。

## 検索の評価器の校正（CRAG）との違い

既存の CRAG の校正は作り直さない。対象・入力・指標が違うので、ラベルの形も集計も分ける。

| | 検索の評価器（CRAG） | 回答の評価器（この文書） |
|---|---|---|
| 問い | 検索の候補は質問に関係するか、根拠は答えるのに足りるか | 生成された回答は正しいか、必要な項目を満たすか、根拠に忠実か |
| 評価器 | `_grade_crag_retrieval` | `evaluate_answer_payload` + `score_answer_evaluation`（画面の品質評価・評価の job と同じ経路） |
| 入力 | 質問・検索の入力（run_id・検索文）・候補の parent chunk | 質問・回答の本文・根拠・標準回答 |
| 人手のラベル | 候補ごとの `relevant`、質問ごとの `answerable` | 項目ごと・観点ごとの `pass` / `fail`（LabelSet） |
| 指標 | 候補単位の precision / recall / F1、質問単位の誤拒・誤受理 | 観点ごと・区分ごとの一致率・Cohen の κ・混同行列・誤合格 / 誤不合格の率・判定率 |
| コード | `rag_engine/evaluation/crag_grader_eval.py` | `rag/backend/app/rag/answer_judge_calibration.py` |

観点と評価器の指標の対応（指標の値が `--pass-at`、既定 1.0 以上なら pass）:

| 観点 | 評価器の指標 | 判定できなかった件（一致にも不一致にも数えない） |
|---|---|---|
| `requirement_coverage` | `requirement_coverage`（必要な比較項目への対応率） | 評価が完了しない（`status` が `completed` 以外） |
| `answer_correctness` | `answer_correctness`（1 / 0） | 同上・判定のモデルが `unassessed` を返した |
| `claim_support` | `claim_support_rate`（監査した主張のうち裏付けられた割合） | 同上・出典の無い段落など、監査できない主張が 1 つでもある |

評価器の呼び出しが例外で失敗した項目も、校正全体を止めずに判定できなかった件（`status=error`・例外の型）にする。例外の本文は接続の情報を含み得るので結果に残さない。

## 校正セット

同梱: [`rag/evaluation/judge-calibration/rag-answer-judge-calibration.v1.json`](../evaluation/judge-calibration/rag-answer-judge-calibration.v1.json)（架空の社内規程の合成。業務の内容を含まない。29 項目・dev 9 / holdout 20）。形は suite と共有の LabelSet（`rubric_id`・`items[]` の `id`・`split`・`question`・`answer`・`standard_answer`・`evidence`・`labels`・`note`・`origin`・`error_types`）。

- **ラベルは作成者が設計した判定で、2 人の記入と裁定を経ていない**（`labeler=author`・`provenance.adjudicated=false`）。評価器の誤りの形を見るための小さなセットで、合否に使う校正ではない。合否に使う校正は、[suite の手順](../../suite/evaluation/docs/judge-calibration.md)（記入者 2 人・κ・裁定）で holdout を観点ごとに 50 件以上集めて行う。
- 観点は項目ごとに、人が迷わず決められるものだけを付ける（例: 根拠に無い補足を足した回答は `claim_support` だけ。標準回答が触れない内容の正しさは付けない）。
- 区分は `pr_evaluation_scoring.assign_splits`（`error_types` の先頭の層・holdout 0.7・salt `rag-answer-judge-calibration-v1`）で決めて固定した。同じ質問の別の回答が dev と holdout に分かれることがある（質問の単位の漏れ）。
- `error_types` は platform の `ERROR_TYPE_IDS`（知らない値は読み込みで断る）。#65 で対照 `partial_answer`（資料で答えられる部分だけ答え、残りは資料に無いと明示する部分回答）を足した。

| 誤りの型・対照 | 件数 | 例 |
|---|---|---|
| `correct_paraphrase` | 10 | 金額の表記の違い・根拠のある補足・短く素っ気ない正答 |
| `partial_omission` | 3 | 5 万円超の事前承認の欠落・私物 USB の禁止の欠落 |
| `unsupported_claim` | 4 | 資料に無い料金・営業時間の創作、根拠と矛盾する「許可は不要」 |
| `correct_refusal` | 1 | 資料に無い営業時間を「記載が無い」と答える |
| `partial_answer` | 1 | 回数だけ答え、料金は記載が無いと明示する |
| `attribute_swap` | 2 | 海外の日当を国内に取り違え・日当と宿泊費の入れ替え |
| `step_order` | 2 | 連絡と申請の前後の逆転・入館手順の逆順 |
| `scope_expansion` | 1 | 1 日の基準を 1 か月へ広げる |
| `general_rule_overreach` | 2 | 通常の申請期限・一般の上限を例外の対象に当てはめる |
| （型なし） | 3 | 回答の中の評価者への指示、出席回数の誤り、資料にある内容を無いと答える誤った拒答 |

## 実行

```bash
cd rag/backend
SET=../evaluation/judge-calibration/rag-answer-judge-calibration.v1.json
W=../../tmp/judge-calibration
# 評価器を動かして校正する（モデルの API を呼ぶ。判定のモデルは既定の判定モデル、--judge-model で指定）
uv run python -m app.rag.answer_judge_calibration $SET --output $W/rag-answer.json
# 人手の判定だけを直したら、保存した評価を使って集計だけやり直す（モデルを呼ばない）
uv run python -m app.rag.answer_judge_calibration $SET --evaluations $W/rag-answer.json --output $W/v2.json
# 指示・モデル・pass_at を変えた後、前の結果を基線にして違いを出す
uv run python -m app.rag.answer_judge_calibration $SET --baseline $W/rag-answer.json --output $W/v3.json
```

- 稼働中の配備のモデル設定を使うときは、その共通 `.env` を指す（`PLATFORM_ENV_FILE=<repo>/platform/.env`）。校正は回答の記録・DB に書かない。
- 閾値は suite / Agent と同じ `CalibrationThresholds`（holdout の観点ごとに件数 50・判定率 0.95・κ 0.6・誤合格の率 0.1。`--min-items` などで変えられ、`provenance.thresholds` に残る）。
- 終了コード: 結果を書けたら 0（合否は結果の `passed`）、人手の判定・前の結果の誤りは 2。
- 画面の「品質評価 › 判定の校正」（#3578）も同じ `calibrate_evaluations` で集計し、同じ形の結果を残す（`--repeats` に当たる回数と揺れの率は画面の校正だけが持つ）。

## 結果の読み方

形は Agent の評価者の校正（#66）にそろえた。

| key | 内容 |
|---|---|
| `aspects.<観点>.splits.<dev\|holdout\|synthetic>` | 件数（`total`・`judged`・`unjudged`）・判定率・一致率・κ・混同行列（`confusion[人][評価器]`）・誤合格 / 誤不合格の率と項目（`false_pass_items`・`false_fail_items`）・食い違い（`disagreements`）・判定できなかった項目 |
| `aspects.<観点>.error_types.<型>.<区分>` | 誤りの型・対照ごとの同じ集計（件数が小さいので率より項目を読む） |
| `aspects.<観点>.passed` / `failures` | holdout の人手の項目だけで決めた合否と、届かない理由 |
| `judgements.<id>` | 区分・誤りの型・人の判定（`human`）・評価器の判定（`judge`）・評価の `status`・観点ごとの理由（`reasons`。正しさは判定の理由、網羅は対応しなかった比較項目、根拠は裏付けられなかった主張と理由） |
| `evaluations.<id>` | 評価器の出力そのもの（`--evaluations` の集計のやり直しに使う） |
| `provenance.labels` | 人手の判定の `rubric_id`・sha256・項目の数 |
| `provenance.evaluator` | 判定のモデルの ID・指示の sha256（`prompt_sha256`。`answer_eval` の `*_PROMPT`）・`seed: null` |
| `provenance.judge_rubric_version` / `pass_at` / `thresholds` | 評価器の rubric の版・pass にする指標の下限・合否の閾値 |
| `baseline` | `--baseline` の比較: `comparable` と `reasons`（人手の判定の sha256・結果の形の版が違えば比べない）、`changes`（モデル・指示の sha256・rubric の版・`pass_at`・閾値の違い）、観点ごとの holdout の指標の差、判定の変わった項目（`flipped`） |

**`passed=false` を成功扱いにしない。** 同梱のセットは holdout が観点ごとに 14〜19 件で、件数の閾値 50 に届かないので、一致率が 1.0 でも不合格になる。

## 人手の判定の見直しと holdout

- 食い違い（`judgements` の `human` と `judge`）は、理由（`reasons`）と人の根拠（`note`）を並べて読む。評価器が正しく人のラベルが誤っていたら、人のラベルを直して `--evaluations` で集計だけやり直す。ラベルを直すと LabelSet の sha256 が変わり、前の結果とは `baseline.comparable=false` になる（直す前と後の数字を並べない）。
- **dev の項目だけを見直しに使う。** holdout の項目を評価器の判定を見て直したら、その holdout はもう holdout ではない（新しい項目で holdout を作り直す）。
- 評価器の判定に合わせるためにラベルを直さない。rubric の読み方で決める（下の `telework-overreach` のように、同じ評価器でも回によって判定が変わる境界の項目がある）。

## 実モデルでの測定（2026-11-01）

条件: 判定のモデル `deepseek-v4-flash`（OpenAI 互換の Responses API）、`prompt_sha256=ec25925f…2019`、評価器の rubric の版 14、`pass_at` 既定 1.0、同梱のセット v1（29 項目）。1 回の校正は約 2.5 分。

| 観点 | holdout の件数 | 判定率 | 一致率 | κ | 誤合格 | 誤不合格 | 合否 |
|---|---|---|---|---|---|---|---|
| `requirement_coverage` | 14 | 1.0 | 1.0 | 1.0 | 0.0 | 0.0 | 不合格（件数 14 < 50） |
| `answer_correctness` | 19 | 1.0 | 1.0 | 1.0 | 0.0 | 0.0 | 不合格（件数 19 < 50） |
| `claim_support` | 19 | 0.8947 | 1.0 | 1.0 | 0.0 | 0.0 | 不合格（件数 19 < 50・判定率 0.8947 < 0.95） |

読み取り:

1. **易しい合成のセットでは一致が 1.0 になり、何も保証しない。** 件数の閾値で不合格になるのは正しい。業務に近いケース・境界のケースを足して 50 件以上で測るまで、評価器の判定は参考値。
2. **`claim_support` は、出典の無い段落が 1 つでもあると判定できない。** 部分回答の「利用料金は資料に記載がありませんでした」（`resort-partial-answer`）と、回答に混ぜた評価者への指示（`travel-judge-injection`）が出典の無い段落として `unassessed` になり、`claim_support_rate` が測れなかった（判定率 0.8947）。注入の文は合格にならず（正しさは fail と判定）、未評価として残った。部分回答の判定率を上げるには「資料に記載が無い」と述べる段落を主張から外す規則が要るが、評価器の挙動の変更なので別に判断する（残課題）。
3. **境界の項目は回によって判定が変わる。** `telework-overreach`（一般の規則「週 2 日まで」を試用期間中の社員に当てはめた回答）の `claim_support` は、作成時に「文自体は根拠にある」として pass と付けた。1 回目の評価器は「質問の対象には但し書きと矛盾する」として fail と判定し、dev の不一致の見直しで人のラベルを fail に直した（`--evaluations` で集計をやり直すと `baseline.comparable=false`）。2 回目（`--baseline` で 1 回目と比較。`changes` は空）では同じ項目を pass と判定し、`flipped` に 1 件出た。判定のモデルは seed を使わないので、同じ条件でも境界の項目は揺れる。揺れのある観点は、κ が閾値を超えても合否に使う前に rubric の読み方をそろえる。

## 限界

- 固定できるのはモデルの ID・指示の sha256・rubric の版まで。provider がモデルの snapshot を公開しなければ、同じ ID の中身の変更は検出できない（変更の疑いは `baseline.flipped` で見る）。
- CLI は 1 回だけ判定する。回の間の揺れの率は、画面の校正（回数を指定）か、`--baseline` で同じ条件の 2 回を比べて見る。
- 校正の結果は評価の job の合否に自動では結び付けない。画面の品質評価は、今の判定のモデル・指示の sha256 で合格した校正があるときだけ「校正済み」を出す（#3578）。
