# 品質評価の評価者の校正（#66）

品質評価（`features/agent/evaluation.py`）は、回答が期待する要点を満たすかと、出力の項目（比べ方が「意味が同じ」の文字の項目）が期待する値と同じ内容かを LLM で判定する。評価者が構造化出力と理由を返せても、人の判定と一致するとは限らない。ここでは、人が判定した校正セットに対する評価者自身の誤合格・誤不合格を測る。

**Agent の性能の評価と、評価者の性能の検査は別の作業である。** スタブが期待した判定を返すテストは集計の検査で、評価者の能力の証明ではない。校正の結果が無い・閾値に届かない評価者の合格率は、参考値として読む。

## 対象と対応

| 観点（`aspect`） | 評価者 | pass | fail | 判定できなかった件 |
|---|---|---|---|---|
| `answer_points` | `evaluation.judge_answer`（品質評価の job と同じ指示・既定のテキストモデル・再試行） | `correct` | `incorrect` | `uncertain`・出力の上限で止まった（`incomplete`）・呼び出しや出力の形の検証の失敗（`failed`。`error_type` に例外の型） |
| `output_meaning` | `evaluation.judge_output_meaning` | 同じ内容 | 違う内容 | 意味の判定の失敗・項目の判定が返らない（`failed`） |

判定できなかった件は一致にも不一致にも数えず、`unjudged`・`coverage`（判定率）に出す。不合格に数えて誤判定を隠さない。

**安全・状態の遷移は、コードの判定が正本である。** 未実行の操作の完了報告（`unexecuted_claim`）・禁止したツールの実行（`forbidden_tool_executed`）・承認の迂回（`unapproved_write`）は、品質評価の `safety_check`（#63）が Run の事実（step・承認・ツールの結果）からコードで判定し、LLM の判定で上書きしない。校正の観点 `unexecuted_completion` は「評価者が、未実行の完了報告を誤って合格にする率」を測るもので、コードの判定の代わりではない。

## 校正セット

同梱: [`agent/evaluation/judge-calibration/agent-judge-calibration.v1.json`](../evaluation/judge-calibration/agent-judge-calibration.v1.json)（合成。業務の内容を含まない。44 項目）。項目の名前は RAG / suite の人手の判定（LabelSet）にそろえる。

| 項目 | 内容 |
|---|---|
| `id` | 項目の ID（重ならない） |
| `split` | `dev`（評価者の指示の調整に使う）/ `holdout`（合否に使う。見て指示を直さない） |
| `aspect` | `answer_points` / `output_meaning` |
| `perspective` | 下の観点の ID |
| `question` | 質問（`output_meaning` は項目の名前） |
| `standard_answer` | 期待する回答の要点（`output_meaning` は期待する値） |
| `answer` | 実際の回答（`output_meaning` は記録した値） |
| `clarifications` | 問い返しへの利用者の答え（任意。`answer_points` だけ） |
| `label` | 人の判定 `pass` / `fail` |
| `note` | 人の判定の根拠（必須） |
| `origin` | `human`（既定）/ `synthetic`（コードで作った対照。合否に入れない） |

区分は `pr_evaluation_scoring.assign_splits`（`aspect:perspective` の層・holdout 0.7・salt `agent-judge-calibration-v1`）で決めて固定した。

| 観点（`perspective`） | 見るもの |
|---|---|
| `fact_consistency` | 起点・対象・役割の取り違え |
| `unexecuted_completion` | 承認待ち・未実行の操作を「完了した」と報告する回答 |
| `expected_points` | 期待する要点の一部の欠落 |
| `meaning_match` | 文字の項目の言い換え（pass）と中身の違い（fail） |
| `negation` / `number` / `date` / `condition` | 否定・数値・日付・条件の違い（表記の違いだけなら pass） |
| `length_bias` / `politeness_bias` / `order_bias` | 長い・丁寧な誤答、短い・素っ気ない正答、順序に意味の有る / 無い並び |
| `judge_injection` | 回答・記録した値の中の評価者への指示・判定の体裁 |

業務の評価セットで校正するときは、同じ形の JSON を repo の外か `./tmp/` に作る（業務の内容を repo・Issue・PR に書かない）。

## 実行

```bash
cd agent/backend
SET=../evaluation/judge-calibration/agent-judge-calibration.v1.json
W=../../tmp/judge-calibration
# 判定のモデルを呼ぶ（システム設定 > モデル の既定のテキストモデル。品質評価と同じ）
uv run python -m app.cli.judge_calibration $SET --output $W/agent.json
# 指示・モデルを変えた後、前の結果を基線にして違いを出す
uv run python -m app.cli.judge_calibration $SET --baseline $W/agent.json --output $W/v2.json
# 人手の判定だけを直したら、前の判定を使って集計だけやり直す（モデルを呼ばない）
uv run python -m app.cli.judge_calibration $SET --judgements $W/agent.json --output $W/v3.json
```

- 稼働中の配備のモデル設定を使うときは、その共通 `.env` を指す（`PLATFORM_ENV_FILE=<repo>/platform/.env`）。校正は Run を作らず、DB に書かない（`AGENT_RUNTIME_REPOSITORY_BACKEND=memory` で十分）。
- 閾値は RAG / suite と同じ `CalibrationThresholds`（holdout の件数 50・判定率 0.95・κ 0.6・誤合格の率 0.1。`--min-items` などで変えられ、`provenance.thresholds` に残る）。
- 終了コード: 結果を書けたら 0（合否は結果の `passed`）、校正セット・前の結果の誤り・モデルの設定の誤りは 2。

## 結果の読み方

形は RAG の回答の評価器の校正（`rag/backend/app/rag/answer_judge_calibration.py`）にそろえ、観点の内訳を足した。

| key | 内容 |
|---|---|
| `aspects.<aspect>.splits.<dev\|holdout\|synthetic>` | 件数（`total`・`judged`・`unjudged`）・判定率・一致率・Cohen の κ・混同行列（`confusion[人][評価者]`）・誤合格 / 誤不合格の率と項目（`false_pass_items`・`false_fail_items`）・判定できなかった項目 |
| `aspects.<aspect>.perspectives.<観点>.<区分>` | 観点ごとの同じ集計（どの観点で誤るかを見る。件数が小さいので率より項目を読む） |
| `aspects.<aspect>.passed` / `failures` | holdout の人手の項目だけで決めた合否と、届かない理由 |
| `passed` | すべての aspect が合格 |
| `judgements.<id>` | 評価者の判定（`label`・`status`・理由 `reason`・`verdict`・`score`・欠けた要点・`error_type`・呼んだ回数）と人の判定・観点・区分 |
| `provenance.labels` | 校正セットの `rubric_id`・sha256・項目の数 |
| `provenance.evaluator` | 判定のモデルの ID・推論の量と出力の上限・`judge_fingerprint`（品質評価の job の `judge_fingerprint` と同じ値）・指示の sha256（aspect ごと）・`seed: null` |
| `baseline` | `--baseline` を付けたときの比較（下） |

**`passed=false` を成功扱いにしない。** holdout の件数が閾値に届かない（同梱のセットは holdout が 26 件で、件数の閾値 50 に届かない）・holdout の無い aspect・判定率が低いものは、正解率が高くても不合格になる。

### 基線との比較（`baseline`）

- `comparable=false` と `reasons`: 校正セットの sha256 が違う（項目・人手の判定を変えた）、結果の形の版が違う。比べた指標は出さない。
- `changes`: 判定のモデル・判定の指示の sha256（aspect ごと）・モデルの設定・閾値の違い。評価者を変えた比較ではここに理由が出る。
- `aspects.<aspect>.holdout.<指標>`: 基線と今回の値と差（一致率・κ・誤合格 / 誤不合格の率・判定率）、`passed` の前後。
- `flipped`: 評価者の判定が変わった項目（人の判定・区分つき）。

判定のモデルは seed を使わない。`changes` が空でも差が出る（判定の揺れ）。揺れのある項目は、κ が閾値を超えても合否に使う前に指示を見直す。

## 限界

- 固定できるのはモデルの ID・設定・指示の hash まで。provider がモデルの snapshot を公開しなければ、同じ ID の中身の変更は検出できない（変更の疑いは基線との比較の `flipped` で見る）。
- `output_meaning` は品質評価の job ではケースの項目をまとめて 1 回で判定するが、校正では 1 項目ずつ判定する（項目の間の影響は測らない）。
- 同梱のセットは評価者の誤りの形を見るための小さな合成のセットで、閾値の件数に届かない。合否に使う校正は、業務の評価セットに近い合成のケースで holdout を 50 件以上集めて行う（記入・裁定の手順は [suite の手順](../../suite/evaluation/docs/judge-calibration.md)）。
- 校正の結果は品質評価の job・公開前の門に自動では結び付けない。job の `judge_fingerprint` と校正の結果の `provenance.evaluator.judge_fingerprint` が一致するかで、その評価者が校正済みかを人が確かめる。
