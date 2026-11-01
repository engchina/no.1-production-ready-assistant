# 製品横断の評価の基線（#3240 の段 1〜3）

業務 Agent の Run の採点・集計を、合成の入力で固定する。段 1・段 2 は rag の比較評価の CLI（`app.rag.agent_evaluation_cli`。#3240 の段 3 で削除）の値で、段 3 でこの fixture を rag から `suite/evaluation/` へ移した。suite の Target・Evaluator・Report が、旧形式の推定（`--infer-missing-outcome`）を選ぶと段 2 と同じ値を出し、既定（推定しない）では suite の基線（`expected.v3`）と同じ値を出すことを確かめる。値を意図して変えるときは、採点の版（`metrics_version`）を上げ、差と理由を PR とこのファイルに書く。

内容はすべて合成のデータで、業務の内容を含まない（AGENTS.md「業務の内容は書かない」）。

## ファイル

| ファイル | 内容 | テスト |
|---|---|---|
| `cases/<id>.json` | 1 ケース（`case` は評価セットのケースの形）と、往復ごとの観測の種類と時間と Run の ID（`turns[]`: `observation`・`elapsed_ms`・`run_id`）。`expected.scores` は段 2 の採点、`expected.cli` は段 2 の CLI の出力（Agent に送った依頼・取り消した Run・Run の記録。suite は送った依頼・取消・Run の要約を比べる）、`expected.v3` は suite の既定の採点（段 2 の項目と、指標ごとの状態 `metric_status`・取れたか分からない根拠 `undetermined_evidence`） | `tests/test_baseline.py` |
| `platform/contracts/mcp/fixtures/agent-evaluation-baseline.json`（共有） | Run の ID ごとの公開の Agent MCP の出力（`agent_get_run`・`agent_get_run_observation`、終わっていない Run は `agent_cancel_run`）と、その元の合成の Run の記録（`source`。Agent の内部の形で、suite は読まない） | Agent の `tests/test_evaluation_baseline_observation.py`（本物の MCP の入口で作った出力と一致し、契約の outputSchema に合う）・suite の上のテスト（公開の出力だけで再生） |
| `summaries.json` | 評価セット（ケースの並びと閾値）ごとの段 2 の集計（`expected.metrics`）・分母（`expected.denominators`）・比較の要約（`expected.cli`）と、suite の既定の集計と分母（`expected.v3`） | `tests/test_baseline.py` |
| `agent_internal_dependencies.json` | 段 1 の CLI が Run の内部の記録から読んでいた観測と、それを返す公開の Agent MCP の項目（`public_fields`）の対応。各項目は公開の項目を消すと変わる採点の項目を `witness` に持つ。足さなかった観測は `not_added` | 同上（旧形式の推定を選んで比べる） |
| `answer_sections.json` | Agent の回答の節（今の見出しと #2738 より前の見出し）を除いた回答の本文の基線（段 3 で rag の `answer_sections.json` の `legacy_agent_sections` から移した。RAG 自身の回答の節は rag に残る） | `tests/test_agent_legacy.py` |

## 保証すること

- **採点**: ケースの結果のうち Agent の Run の ID と本文を含まない項目（六指標・引用の文書・正解の文書・取れなかった根拠・対応と出所・往復ごとの判断・失敗の理由・失敗の種類と段・時間）。段 2 の結果にあった RAG の評価だけの項目（`answer_evaluation`。Agent の経路ではいつも空）は suite に無い。
- **集計**: 六指標の平均（成功したケースのうち測れたものだけ）、`metric_case_counts`、`error_count`、区分・分類・種類・段の数・期待する判断ごとの内訳、閾値の判定（`threshold_failures`・`passed`）、採点の版。段 2 の結果にあった RAG の評価だけの項目（`evaluation_suite`・`comparison`）は suite に無い。
- **分母**: 段 2 の形（ケースの数・失敗の数・最後の往復の観測の種類ごとの数・指標ごとの測れた数と測れなかった数）と、suite の形（`denominators`: 最後の往復の状態・失敗の種類・指標ごとの測れた / 対象外 / 評価不能の数と評価不能の理由）。失敗は指標の分母に入らず、失敗が 1 つでもあれば `passed=false`。どの指標も測れなければ `passed=null`。
- **失敗の扱い**: failed・cancelled・waiting_approval・時間切れ（取り消す）・承認の要るツールを呼ばずに完了した Run（`skipped_approval_tools`・観測の `tool_calls[].status=approval_skipped`。#3240 の Q7）・Run を作れない（HTTP の失敗・ツールのエラー・知らない `schema_version`）は、どれもケースの失敗（`case_error`）。複数往復の途中で失敗したら、それまでの往復を採点せずケースの失敗にする（`error_stage=turn_<n>`、時間は往復の和）。
- **旧形式の扱い**: 公開の `outcome` が null の完了の Run は、既定では対応を推定せず、判断の正しさを評価不能（`outcome_missing`）にする。`--infer-missing-outcome` を選ぶと段 2 と同じく回答の文から推定する（最終の検証の定型の注記と Agent の回答の節を除いた最後の行が問いなら確認の質問、引用の無い回答は拒答。`outcome_source=inferred`）。`rag_search` の outcome は公開しないので使わない。
- **保存した観測からの採点**: 結果のファイルの観測だけから採点し直すと、流したときと同じ集計になる。推定の有無を変えた採点し直しは、その設定で流したときと同じ集計になる（Target を呼ばない）。

## 再生の条件

`tests/baseline_support.py` の `replay` が、Agent の公開の MCP（`POST /api/mcp` の `tools/call`）を共有の fixture の公開の出力で再生して、suite の `run_experiment` を呼ぶ。送る引数は契約（`agent-tools.json` の inputSchema）で確かめる。

- 往復の時間は `turns[].elapsed_ms`（往復の始まりを 0 秒、Run の終わりを elapsed_ms の時刻にする時計）。
- 時間切れの上限は 2 秒（`RUN_TIMEOUT_SECONDS`）。時間切れの Run（`observation=timeout`）の `agent_get_run` は running（`in_progress=true`）のまま返し、待ちの時計は読むたびに 1 秒進む。期限を過ぎたら `agent_cancel_run` を送る。
- `observation=api_error` の往復は `agent_chat` が `http_status` の HTTP の失敗を返す。
- 観測は 1 回（`limit=100`）で全件を返す（ページのたどり方は `tests/test_agent_mcp_target.py`）。
- 業務ガイドの条件の名前は渡さない（既知の条件は条件の id で書く）。
- 回答の前の確認・本文を省かれた根拠・見られない根拠は共有の fixture に無いため、`tests/test_agent_mcp_target.py` の合成の出力で確かめる。

## 公開の Agent MCP の項目

`agent_internal_dependencies.json` の一覧。段 1 で公開の Agent MCP に無かった観測は、段 2（設計は draft PR #3316、実装は #3319・#3320・#3322・#3326・#3327・#3330）で次の公開の項目になった。テストは公開の項目を消すと、段 1 で内部の記録を消したときと同じ採点の項目が変わることを確かめる。

| 観測 | 使う項目 | 公開の Agent MCP |
|---|---|---|
| 根拠の `document_id`・`chunk_id` | MRR・必要な根拠の取得率・引用の文書・`retrieval_miss` / `evidence_miss`・引用の重複の除去 | `agent_get_run_observation.evidence[].document_id` / `chunk_id`（`rank` の順） |
| 根拠の構造の場所（頁・章節・シート・行） | 必要な根拠の取得率（場所の条件の付いた根拠） | `evidence[].locator`（RAG の `EvidenceLocator` と同じ名前） |
| 回答の対応 | 対応・出所・判断の正しさ・拒答の判定・検索の指標の対象外の判定 | `agent_get_run.outcome` |
| 最終の検証の状態 | Run の記録の `validation_status` | `agent_get_run_observation.validation_status` |
| モデルの呼び出しの数 | Run の記録の `model_requests` | `agent_get_run_observation.usage.requests` |
| 旧版も検索したか | Run の記録の `superseded_searches`・旧版を検索したケース | `tool_calls[].include_superseded`（真偽だけ） |
| 回答に使わなかった根拠 | CLI は返った根拠をすべて引用に数える | `evidence[]` は全件（`next_cursor`）、引用は `cited` |
| ツールごとの呼び出しの数 | Run の記録の `tool_calls` | `tool_calls[]`（呼び出しごと） |
| Run の取消 | 時間切れの取消 | `agent_cancel_run`（冪等。`result` を記録） |
| 業務 Agent の作成・公開 | 段 1 の `--create-agent` | 足さない（#3240 の Q4）。公開済みの業務 Agent を `--agent-id` で指定し、`agent_version` を記録する |
| `rag_search` の outcome・clarifications・insufficient_reason | 旧形式の対応と出所・診断 | 足さない（下の「意図して作り直した値」） |

回答の指標（`answer_correctness`・`requirement_coverage`・`claim_support_rate`）は、今の CLI が標準回答による評価を渡さないため、Agent の経路ではいつも測れない（`not_evaluable` に全ケースが入る）。

## 意図して作り直した値

### #3282: CLI の比較の要約の不具合の修正

段 1 の基線は、CLI の比較の要約の 2 つの不具合をそのまま固定していた。#3282 で CLI を直し、`summaries.json` の `expected.cli` のうち次の値だけを作り直した。採点（`cases/<id>.json` の `expected.scores`）・集計（`expected.metrics`）・分母（`expected.denominators`）は変えていない。

- `summary_markdown` の分類ごとの行（`<分類>: 回答・確認・保留の判断の正しさ`）: 要約の分類の `handling_correct_rate` を読むようにした。前は `metrics.response_decision_accuracy` を読んでいつも `-` だった。`all` / `completed` は `document_answerable` が 0.50、`clarification_required`・`environment_data_required`・`knowledge_missing`・`conflicting_sources` が 1.00、`passing` は `clarification_required` が 1.00（`uncategorized` は測れないので `-` のまま）。
- `summary.dangerous_case_count` と表の「危険な回答のケース」: 失敗の理由に `forbidden_action` が付いた成功のケースを数え、該当が無ければ 0 ではなく測れない（`null`・表は `-`）とした。前は今のケースの結果に無い項目（`forbidden_hits`）を数えていつも 0 だった。今の採点（六指標）は勧めてはいけない操作を測らないため、4 つの評価セットとも `null` / `-` になる。

### #3240 の段 2: 公開の Agent MCP だけで採点する（採点の版 `six_metrics_v1+agent_v2`）

CLI は Agent の画面の API（`POST /api/runs`・`GET /api/runs/{id}`）と Run の内部の記録をやめ、公開の MCP（`agent_chat`・`agent_get_run`・`agent_get_run_observation`・`agent_cancel_run`）の出力だけで採点する。段 1 の 14 ケースの Run を公開の出力にして再生した結果、六指標・集計の値（`metrics` の平均・`metric_case_counts`・内訳・合否）と分母は変わらず、採点の項目は次の 3 ケースの対応の出所だけが変わった（どれも `rag_search` の outcome を公開しないため。設計の §3）。

- `legacy-rag-search-outcome`: `observed_outcome` が conditional → answered、`outcome_source` が agent_rag_search → inferred（回答の文に問いが無く引用があるので answered と推定する。期待する対応に answered も含むので判断の正しさは変わらない）。
- `multi-turn-clarify-then-answer`: 1 往復目の `turn_results[0].outcome_source` が agent_rag_search → inferred（最後の行が問いなので推定も needs_clarification）。
- `unknown-outcome-vocabulary`: `outcome_source` が agent_rag_search → inferred（推定も answered）。

あわせて次を変えた。

- **承認の要るツールを呼ばなかった Run（#3240 の Q7）**: MCP の Run は承認の要るツールを呼ばずに答える（`waiting_approval` にならない）。成功に数えると必要な操作をしていない回答を採点するので、ケースの失敗（`error_type=AgentRunApprovalSkipped`）にした。ケース `approval-skipped-run` を足し、評価セット `all` に入れた（`all` のケースの数・失敗の数・`document_answerable` の数・`failed_case_ids` が 1 つずつ増えた）。
- **採点の版**: Agent の経路の `metrics_version` を `six_metrics_v1` → `six_metrics_v1+agent_v2` にした（RAG 自身の評価の版 `six_metrics_v1` は変えない）。
- **`expected.cli`**: 診断から `rag_search` 由来の `insufficient_reason` と、確認の質問で聞いた条件（`clarifications`）が無くなった。Run の記録に `agent_version`・`skipped_approval_tools`・`withheld_evidence_count`・`evidence_text_omitted`・`cancel_result` を足し、送った依頼は `run_requests`（`goal`）から `chat_requests`（`question`）にした。fixture の `thread_id` は契約の形（`thread_<32 桁の hex>`）にした。
- `cases/<id>.json` の `turns[].run`（Agent の内部の記録）は共有の fixture の `source` に移し、ケースは Run の ID だけを持つ。

### #3240 の段 3: suite へ移した（採点の版 `six_metrics_v1+agent_v3`）

比較評価を rag から `suite/evaluation/` の独立の uv project に移し、採点の部品（照合・順位・判断・集計）を platform の `pr_evaluation_scoring` に出した（RAG の評価ランナーも同じ部品を使い、RAG 自身の評価の値は変えていない）。旧形式の推定を選ぶと（`six_metrics_v1+agent_v3+legacy_outcome`）、段 2 の採点・集計・分母・比較の要約と同じ値になる。差は次だけ。

- `run-create-error`: `error_stage` が null → `turn_0`（Run を作れなかった往復にも段を付ける。段 2 は Run を作れない失敗だけ段と時間を捨てていた）。
- 採点の版: `six_metrics_v1+agent_v2` → `six_metrics_v1+agent_v3+legacy_outcome`。
- Run の要約: 推定した対応を `outcome` に出す（段 2 の Run の記録は、推定した対応のうち確認の質問だけを残していた。`legacy-inferred-abstained`・`legacy-rag-search-outcome`・`unknown-outcome-vocabulary`）。承認の要るツールを呼ばなかった往復は採点しないので対応を出さない（段 2 と同じ）。

既定（推定しない。`six_metrics_v1+agent_v3`）の値（`expected.v3`）は、段 2 から次が変わる。

- **outcome の無い完了の Run は判断の正しさを評価不能にする**（`outcome_missing`）: `legacy-inferred-abstained`・`legacy-inferred-question`・`legacy-rag-search-outcome`・`unknown-outcome-vocabulary` の `observed_outcome`・`outcome_source` が null、`response_decision_accuracy` が null（`legacy-inferred-question` は推定が誤っていた `unexpected_handling` も付かない）。`multi-turn-clarify-then-answer` は 1 往復目が評価不能なので、ケースの判断も評価不能（2 往復目は answered で正しい）。
- その結果、`response_decision_accuracy` の測れた数が減り（`all`・`completed` は 7 → 2、`passing` は 2 → 1）、平均は 1.0。`passing` は閾値を付けた判断の正しさに評価不能のケースがあるので、合格にしない（`passed` が true → null）。検索の指標（MRR・必要な根拠の取得率）は、引用がある Run なので変わらない。
- 回答の前の確認（`agent_answer_confirmation` で答える）・本文を省かれた根拠（`evidence_text_omitted`）・見られない根拠（`evidence_withheld`）の規則は、共有の fixture にその Run が無いので基線の値は変わらない（合成の出力のテストで確かめる）。

## 基線を作り直すとき

採点を意図して変えたときだけ作り直す。`UPDATE_SUITE_EVALUATION_BASELINE=1 uv run pytest tests/test_baseline.py -p no:xdist`（並列にしない）で `expected.v3` を書き直し、変わった値を PR とこのファイルで一つずつ説明する。段 2 の値（`expected.scores`・`expected.metrics`・`expected.denominators`・`expected.cli`）は段 2 の記録として書き換えず、意図した差は `tests/test_baseline.py` の `STAGE2_DIFFS` に書く。Agent の公開の出力を意図して変えたときは、Agent の `UPDATE_AGENT_EVALUATION_BASELINE=1 uv run pytest tests/test_evaluation_baseline_observation.py` で共有の fixture を書き直し、この基線の採点が変わらないことを確かめる。
