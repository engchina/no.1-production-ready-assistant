# suite/evaluation（`pr_suite_evaluation`）

製品横断の評価の CLI（`suite-evaluation`。#3240）。評価セットを製品の**公開の MCP の契約だけ**で流し、観測を保存して、製品の評価と同じ六指標で採点する。Target は RAG の検索・回答（RAG の公開の MCP の `rag_search`。#3417）と業務 Agent（Agent の公開の MCP）で、どちらも同じ Evaluator・Report で採点する。

## 置き場所と依存の向き

| 置き場所 | 持つもの |
|---|---|
| `rag/`・`nl2sql/`・`agent/` | 各製品の機能と、各製品自身の評価（RAG の評価ランナー・評価の job・評価の取り込みの CLI） |
| `suite/evaluation/` | 製品をまたぐ評価（この project）。独立の uv project で、常駐のサービスは持たない |
| `platform/packages/evaluation_scoring/` | 製品の評価と suite の評価が同じ意味で使う、決定的な採点の部品（`pr_evaluation_scoring`） |

- suite は platform の公開の package（`pr_evaluation_scoring`・`pr_enterprise_ai`）だけに依存する。製品の Python の module・DB の表・保存ファイル・設定を読まない（製品は `POST /api/mcp` を API キーで呼び、画面の API・Cookie / CSRF を使わない）。
- platform は suite・製品を import しない。製品は suite を import しない。製品同士はコードで依存しない（RAG は Agent を呼ばない）。
- 機械的な検査: `tests/test_boundaries.py`（suite の import・製品の path・`/api/mcp` 以外の API・製品の設定と DB の object の名前・依存の閉包〔`uv.lock`〕の自分たちの package・suite が使う platform の package の import・製品と platform の manifest）と、製品の側は `rag/backend/tests/test_product_boundaries.py`（rag の import・他の製品と suite のファイル・使わない契約・Agent の表と API・manifest。既知の例外は Issue つき）。

## 構成

| 層 | module | 内容 |
|---|---|---|
| Dataset | `dataset.py` | 評価セット（製品の評価と同じ JSON）を読み、`file:<ファイル名>` を文書の対応表で配備先の `document_id` にし、内容の hash を取る |
| Target | `targets/rag_mcp.py` | RAG の検索・回答を `rag_search` で呼ぶ（A はナレッジベース、C は検索・回答プロファイルを指定。既知の条件・往復の条件は `conditions`） |
| Target | `targets/agent_mcp.py` | 業務 Agent を `agent_chat`・`agent_get_run`・`agent_answer_confirmation`・`agent_get_run_observation`・`agent_cancel_run` で呼ぶ。会話の継続・回答の前の確認への答え・時間切れの取消 |
| Observation | `observation.py` | 往復ごとの外枠（状態・時間・参照・読んだ出力の版・失敗）と、公開の出力そのもの（`payload`） |
| Evaluator | `evaluators.py` | 観測から作った採点の入力（`views.py`）を、評価セットの期待と照らして採点する。指標ごとに scored / not_applicable / unevaluable と理由 |
| Experiment | `experiment.py` | 流す・採点する・保存した観測から採点し直す。評価セット・対象構成・採点の版と hash を記録する |
| Report | `report.py` | 集計（六指標・内訳・分母・合否）と、結果の比較の表 |

旧形式（#1305 より前の outcome の無い Run・#2738 より前の回答の節）の扱いは `targets/agent_legacy.py` にだけ置く。

## 使い方

A（RAG。業務ガイドなし＝ナレッジベースを指定）・C（RAG。業務ガイドあり＝検索・回答プロファイルを指定）・D（業務 Agent）を、同じ評価セット・同じ Evaluator で採点して比べる（#3417）。

```bash
cd suite/evaluation
uv sync --dev
export SUITE_EVALUATION_API_KEY=prak_...   # 使える製品に rag と agent を含む API キー（引数に書かない）

# 1. 評価の資料を RAG に取り込み、文書の対応表を書く（RAG の評価の取り込みの CLI。rag/backend で）。
#    ナレッジベースの ID と（--guides を渡したときは）検索・回答プロファイルの ID が出力に出る。
#    uv run python -m app.rag.evaluation_corpus_cli ../evaluation/business-support/business-support.json \
#        --api-base-url http://127.0.0.1:8100 --output ../../tmp/eval/bs.resolved.json \
#        --document-map-output ../../tmp/eval/document-map.json \
#        --guides ../evaluation/business-support/support-guides.json --guided-output ../../tmp/eval/bs.guided.json

SET=../../rag/evaluation/business-support/business-support.json
MAP=../../tmp/eval/document-map.json
# 2. A / C（RAG の公開の MCP の rag_search）と D（業務 Agent の公開の MCP）に流して採点する
uv run suite-evaluation run "$SET" --document-map "$MAP" --target rag-mcp \
    --mcp-url http://127.0.0.1:8100/api/mcp --knowledge-base-id <ナレッジベース> --output ../../tmp/eval/a.json
uv run suite-evaluation run "$SET" --document-map "$MAP" --target rag-mcp \
    --mcp-url http://127.0.0.1:8100/api/mcp --search-answer-profile-id <検索・回答プロファイル> \
    --output ../../tmp/eval/c.json
uv run suite-evaluation run "$SET" --document-map "$MAP" --target agent-mcp \
    --mcp-url http://127.0.0.1:8120/api/mcp --agent-id <公開済みの業務 Agent> \
    --guides ../../rag/evaluation/business-support/support-guides.json --output ../../tmp/eval/d.json

# 3. 保存した観測を採点し直す（規則・期待を直したとき。モデル・製品を呼ばない）
uv run suite-evaluation score ../../tmp/eval/d.json --dataset "$SET" --document-map "$MAP" \
    --output ../../tmp/eval/d.rescored.json

# 4. 比べる（同じ評価セットの suite の結果どうしだけ。対象と採点の版の行が付く）
uv run suite-evaluation compare A=../../tmp/eval/a.json C=../../tmp/eval/c.json D=../../tmp/eval/d.json
```

- `rag-mcp` は `rag_search` だけを呼ぶ。既知の条件は `conditions` に構造化して渡し、複数往復のケースは、`rag_search` が会話を持たないので、最初の質問とそれまでの返答をつないだ質問と、返答までに分かった条件で呼び直す。引用は `evidence[]`（返った順。`--evidence-limit` の既定は契約の上限 50）。
- `compare` は suite の結果のファイルだけを読む（評価セットの内容の hash が違う結果は比べない）。RAG 自身の評価（`evaluation_cli`・評価の job・画面の品質評価。LLM による回答の判定を含む）は製品の調整のために RAG に残り、その結果は suite の `compare` に渡さない。

結果のファイルは回答の本文と根拠の本文を含む。repo に commit せず、repo の直下の `./tmp/` などに置く。API キーは結果・ログに出さない。

## 採点の規則

指標は製品の評価の六指標（`required_evidence_recall`・`mrr`・`answer_correctness`・`requirement_coverage`・`claim_support_rate`・`response_decision_accuracy`）と同じ名前・同じ意味。平均は成功したケースのうち測れたものだけで、失敗したケースは分母に入らない。

- **ケースの失敗**（`status=error`・`case_error`）: Run の failed・cancelled・承認待ち・時間切れ（取り消す）・承認の要るツールを呼ばずに完了した Run（#3240 の Q7）・MCP を呼べない / 知らない `schema_version`。途中の往復で失敗したら、それまでの往復を採点しない（`error_stage=turn_<n>`、時間は往復の和）。
- **回答の前の確認**（`waiting_confirmation`）: 次の返答（評価セットの `turns[].reply`）を `agent_answer_confirmation` で答える（分かった条件の値と同じ名前の選択肢があれば選び、無ければ返答の文を自由入力にする）。確認の往復は「利用者に条件を確かめた」（needs_clarification）として採点する。次の返答が無ければ、確認で終わったケースとして Run を取り消す。
- **評価不能**（`metric_status.<指標>.status=unevaluable`。平均・分母に入れない。`denominators.metrics` に数と理由）:
  - `outcome_missing`: `outcome` の無い完了の Run。判断の正しさは測らず、引用も無ければ検索の指標も測らない（`--infer-missing-outcome` で旧形式の推定を選べる）。
  - `evidence_text_omitted`: 場所の条件に合う根拠の本文が省かれていて（`text_omitted`）、本文の条件を照合できない。
  - `evidence_withheld`: 見る人の今の権限で見られない根拠があり（`withheld_evidence_count`）、取れなかった根拠・1 位でない順位がその根拠のせいかを確かめられない。
  - `answer_judge_unavailable`: 標準回答はあるが、決定的な採点では回答の判定（LLM）を行わない（LLM による判定は下の「LLM による判定と校正」で別に記録する）。
- 閾値を付けた指標に評価不能のケースがあれば、測れたケースだけで合格にしない（`passed=null`）。

## LLM による判定と校正（#3418・#2577。draft）

回答の正しさ・必要な論点の充足・根拠への忠実性は、決定的な採点では測れない（決定的な report では `answer_judge_unavailable` として評価不能に数える）。LLM による判定（LLM-as-judge）は、決定的な採点と**分けて**結果のファイルの `judge` に記録し、人手の判定で校正に通った観点だけを合否に使う。

| module | 内容 |
|---|---|
| `judge/rubric.py`（JSON は platform の `pr_evaluation_scoring/rubrics/answer_quality_v1.json`。RAG の判定の校正の画面と共有。#3578） | 判定の基準（指示・観点・ラベル）。観点は `requirement_coverage`（必要な論点の充足）・`answer_correctness`（事実・推論の正しさ。対象・属性・数量・期間・条件の保持、手順の前後関係）・`claim_support`（根拠への忠実性）。ラベルは pass / fail / cannot_judge。内容の sha256 を記録する |
| `judge/client.py` | OCI Enterprise AI の Responses API（`{Endpoint URL}/responses`。URL の規則と HTTP の失敗の分類は platform の `pr_enterprise_ai`。FastAPI を入れない）を構造化出力（JSON schema・strict）で呼ぶ |
| `judge/judging.py` | 1 件の判定（Pydantic で出力を確かめ、形が違えば呼び直し、それでも違えば評価不能）、観点ごとの集計、校正の照合 |
| `judge/run.py` | 保存した観測の回答を判定する（製品は呼ばない） |
| `judge/calibration.py` | 人手の判定の形（`LabelSet`。人手 / 合成の別・記入者ごとの元の判定）と、一致の集計（platform の `pr_evaluation_scoring.agreement`: 一致率・Cohen の κ・混同行列・誤合格 / 誤不合格）・合否（閾値も platform の `CalibrationThresholds`）・判定の分散（`--repeats`） |
| `judge/labeling.py`・`judge/workbook.py` | 人手の判定を集める（区分の決め方・誤りの型と目標・記入者の間の κ は platform の `pr_evaluation_scoring.labeling` を RAG の判定の校正の画面と共有。#3578）: 記入用のブック（.xlsx。1 行 = 項目 × 観点）と項目のファイルの書き出し（`label-template`。区分は層ごとの hash で決める）、記入者の間の一致と食い違い・裁定用のブック・LabelSet の書き出し（`label-import`） |
| platform の `pr_evaluation_scoring.perturbation`（#3578 で移した） | 故意に壊した回答（合成の対照）: 数値の入れ替え・条件の文の削除・根拠に無い断定の追加・手順の逆順。壊した観点を fail とし、人手の判定と分けて集計する |

判定のモデルと接続は環境変数で決める（既定のモデルは持たない）: `SUITE_EVALUATION_JUDGE_ENDPOINT`・`SUITE_EVALUATION_JUDGE_API_KEY`・`SUITE_EVALUATION_JUDGE_MODEL`（必須）、`SUITE_EVALUATION_JUDGE_PROJECT_OCID`・`SUITE_EVALUATION_JUDGE_MAX_OUTPUT_TOKENS`・`SUITE_EVALUATION_JUDGE_TIMEOUT_SECONDS`（任意）。

**人手の判定の集め方と校正の手順（ケースの作り方・記入・裁定・校正・記録・目標の件数・時間の目安）は [docs/judge-calibration.md](./docs/judge-calibration.md)。**

```bash
# 0. 人手の判定を集める（記入用のブック → 2 人が記入 → 一致と裁定 → LabelSet）
uv run suite-evaluation label-template A=../../tmp/eval/a.json C=../../tmp/eval/c.json \
    D=../../tmp/eval/d.json --dataset "$SET" --document-map "$MAP" --perturb all \
    --output ../../tmp/eval/labels.xlsx
uv run suite-evaluation label-import --items ../../tmp/eval/labels.items.json \
    --annotations a1=../../tmp/eval/a1.xlsx a2=../../tmp/eval/a2.xlsx \
    --report ../../tmp/eval/iaa.json --adjudication-output ../../tmp/eval/adjudication.xlsx
uv run suite-evaluation label-import --items ../../tmp/eval/labels.items.json \
    --annotations a1=../../tmp/eval/a1.xlsx a2=../../tmp/eval/a2.xlsx \
    --adjudicated adj1=../../tmp/eval/adjudication.xlsx --output ../../tmp/eval/labels.json
# 1. 人手の判定で校正する（holdout の観点ごとに κ・件数・判定率・誤合格の率を閾値と比べる。
#    --repeats で同じ項目を複数回判定し、判定の分散を記録する）
uv run suite-evaluation calibrate ../../tmp/eval/labels.json --repeats 3 \
    --output ../../tmp/eval/calibration.json
#    人手の判定を直したら、モデルを呼ばずに集計だけやり直す
uv run suite-evaluation calibrate ../../tmp/eval/labels.json --judgements ../../tmp/eval/calibration.json \
    --output ../../tmp/eval/calibration.v2.json
# 2. 保存した観測の回答を判定する（校正に通った観点だけ合否を出す。ほかは参考値）
uv run suite-evaluation judge ../../tmp/eval/d.json --dataset "$SET" --document-map "$MAP" \
    --calibration ../../tmp/eval/calibration.json --output ../../tmp/eval/d.judged.json
uv run suite-evaluation compare A=../../tmp/eval/a.judged.json C=../../tmp/eval/c.judged.json D=../../tmp/eval/d.judged.json
```

- 校正の既定の閾値（holdout の観点ごと）: 件数 50 以上・判定率 0.95 以上・κ 0.6 以上・誤合格の率 0.1 以下（`--min-items` などで変えられ、結果に記録する）。
- 校正の結果は rubric の sha256 と判定のモデルに結ぶ。どちらかが違えば、判定は合否に使わない（`calibration_status=mismatch`）。
- 判定のモデルの seed は使わない（#3529 で LLM の呼び出しから外した）。再現の条件は、判定のモデルの ID・rubric の sha256・指示と出力の形の sha256（`prompt_sha256`）を記録し、`--repeats` で測った判定の分散（観点ごとに回の間で判定が変わった項目の率 `variance.<観点>.unstable_rate`）を添えて示す。
- 故意に壊した回答（`label-template --perturb`）は `origin=synthetic` の合成の対照で、holdout の合否に入れず `synthetic` の区分として別に集計する（壊した回答の誤合格の率）。
- 検索の評価器の校正（RAG の `rag_engine.evaluation.crag_grader_eval`）とは対象が違うので分ける。一致の集計と校正の閾値の部品は platform で共有し、RAG の回答の評価器（`rag_engine.evaluation.answer_eval`）は同じ LabelSet を RAG の `app.rag.answer_judge_calibration` で校正する。
- テストの人手の判定と判定の応答は合成（`tests/fixtures/judge_calibration/`・`tests/test_labeling.py` の中の合成の記入）で、合否の閾値に届かない件数にしてある（実の校正は利用者の人手の判定で行う）。

## 版と記録

- `metrics_version`: Evaluator の版 `six_metrics_v1` に Target の規則の版を足す。業務 Agent は `six_metrics_v1+agent_v3`（`--infer-missing-outcome` では `+legacy_outcome` を足す）、RAG は `six_metrics_v1+rag_v1`（公開の `rag_search` の出力だけで採点する最初の版。#3417）。
  - `agent_v1`: 段 1（rag の CLI。Agent の画面の API と Run の内部の記録）。
  - `agent_v2`: 段 2（rag の CLI。公開の Agent MCP の出力だけ。承認の要るツールを呼ばなかった Run を失敗にする）。
  - `agent_v3`: suite へ移した版。回答の前の確認に答える・`outcome` の無い Run と本文を省かれた根拠と見られない根拠を評価不能にする・Run を作れなかった往復にも `error_stage` を付ける。
- 結果の `provenance`: 評価セット（名前・解決した後の内容の sha256・ケースの id）、対象構成（MCP の URL・業務 Agent の ID・待ちの上限・読む出力の版と、その sha256）、採点（`metrics_version`・package の版・設定と sha256）。採点し直した結果は、観測を取ったときの評価セットを `observed_dataset` に残す。
- 基線: `tests/fixtures/agent_baseline/`（段 1・段 2 の合成の基線。差は fixture の README.md）。

## テスト

```bash
uv run pytest -n auto
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

Agent の MCP は `httpx.MockTransport` の台本（共有の fixture `platform/contracts/mcp/fixtures/agent-evaluation-baseline.json` の公開の出力と、テストの中の合成の出力）で再生し、実サービスは呼ばない。
