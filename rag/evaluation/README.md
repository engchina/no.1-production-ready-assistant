# Golden Set Evaluation

`golden-set.example.json` は評価（`POST /api/evaluation/jobs/run`）に渡す評価ファイルのテンプレートです。
実データ投入後、各 `relevant_document_ids` を環境内の document id に置き換えて `golden-set.json` として管理します。

評価は job で動きます（#390）。投入すると `202` で job（`job_id`・`status=RUNNING`・`total_cases`）を返し、
`GET /api/evaluation/jobs/{job_id}` で進捗（`completed_cases` / `total_cases`・実行中のケース `current_case_id`）と、
終わったら結果（`run_result`）を返します。実行中の job は
`POST /api/evaluation/jobs/{job_id}/cancel` で取り消せます。job は投入した利用者だけが見られます。

```bash
cp evaluation/golden-set.example.json evaluation/golden-set.json
curl -X POST http://localhost:8100/api/evaluation/jobs/run \
  -H 'Content-Type: application/json' \
  -d @evaluation/golden-set.json
# 返った job_id で、status が RUNNING でなくなるまで取得する
curl http://localhost:8100/api/evaluation/jobs/<job_id>
```

job 全体の時間の上限は、投入時の回答の回数（ケース × 往復）× 回答生成の上限で、
`RAG_EVALUATION_JOB_TIMEOUT_SECONDS`（既定 3600 秒）を最小値、86400 秒を最大値にします（#1430。例: 20 問なら
20 × 600 = 12000 秒）。上限は job の `time_limit_seconds` で返ります。1 ケースは回答生成の上限
（`RAG_ANSWER_TIMEOUT_SECONDS`、既定 600 秒）で打ち切り、時間切れになった工程を `error_stage` / `error_message` に
残して次のケースへ進みます。全体の上限に達したら、実行中のケースを打ち切り、残りのケースは実行せずに
`error_type=EvaluationTimeBudgetExceeded` の失敗として結果を返します（job は `SUCCEEDED`、評価は `passed=false`）。
同時に実行できる評価の job は 2 件までです。以前の同期の `POST /api/evaluation/run` は、外部から直接呼ぶ
利用のために残していますが、評価全体を 600 秒で打ち切ります（#383）。

## 評価の六指標

「検索2・回答3・応答の判断1」の順に統一します。定義・閾値・Excelの入力は
[評価・観測性・ガードレール](../docs/evaluation-observability-guardrails.md#評価)が正本です。

| 分類 | 指標 | 評価入力 |
|---|---|---|
| 検索 | `required_evidence_recall` | 必要な本文・位置を指定した `required_evidence` |
| 検索 | `mrr` | `relevant_document_ids`。重複を除いた引用文書の最初の正解の順位 |
| 回答 | `answer_correctness` | 標準回答または確認済みの `required_answer_points` |
| 回答 | `requirement_coverage` | 標準回答・必須要点・`required_conditions` |
| 回答 | `claim_support_rate` | 回答の主張と取得した根拠。標準回答は不要 |
| 応答の判断 | `response_decision_accuracy` | `expected_response_decision`: `answer` / `clarify` / `hold` |

前提不足は `null`（未評価）で、0点や合格にしません。`metric_case_counts`が指標ごとの対象件数です。
標準回答や根拠が空でも、保留を期待するとは推定しません。判断の期待は明示します。
`expected_outcomes`を使う場合も、明示値を三つの判断へまとめます。

`reasoning_type_breakdown`・`hops_breakdown`・`split_breakdown`・`category_breakdown`も同じ六指標と件数です。
`decision_breakdown`で期待する回答・確認・保留ごとの内訳を確認できます。
`reasoning_type`・`hops`・`conditions`・`turns`・`include_superseded`は検索と会話の条件として使います。
実行の失敗は平均に入れず`error_count`で数え、合格にしません。評価の未完了も合格にしません。
総合状態`passed`は残る指標から導き、CLIは未評価・不合格・実行エラーを成功扱いにしません。

検索・回答プロファイルの用語・同義語・回答ルール・業務ガイド・回答の設定を含めて評価するときは、`search_answer_profile_id`
（`/run` の request）を指定します（#1249）。ケースごとに検索・回答と同じ解決（参照する
ナレッジベース・回答の設定・業務ガイド）をしてから回答します。指定しなければ全体の既定で評価します（#301）。
アーカイブ済み・参照するナレッジベースが無いプロファイルは、そのケースを失敗にします。

単発の `/run` でも任意の `rag_overrides` を指定でき、回答エンジンの設定（`query_strategy`・`answer_flow`・
`neighbor_child_count`・`rerank_enabled`）と、RRF 定数（`rrf_k`）・同じ group から足す child の上限
（`context_group_max_chunks`）・Oracle vector target accuracy を一時的に上書きできます。

複数の設定を比べるときは、`rag_overrides` を変えた評価をそれぞれ実行し、結果の指標を並べます（同じケースを複数の設定で一度に比べる API（設定比較）は #2085 で削除しました）。nightly（`rag-evaluation-nightly.yml`）は `golden-set.example.json` を使います。

CI / nightly では CLI を使うと、評価結果 JSON を artifact として保存しつつ終了コードで gate できます。CLI は評価の job を投入し、終わるまで状態を取得して（`--poll-interval`、既定 5 秒）、結果を `{"data": 結果, "job": job の状態}` として保存します。

```bash
cd backend
uv run python -m app.rag.evaluation_cli \
  ../evaluation/golden-set.json \
  --api-base-url http://localhost:8100 \
  --output ../evaluation/evaluation-result.json
```

### 実行条件の固定と、外部の AI の応答の記録・再生（#59）

評価の結果（`data.run_manifest`）に、その評価の実行条件を残します。評価の開始時と終了時に作って比べ、実行中に変わった
条件を `changed_during_run` に出します（変わったら `comparable: false`。比べてはいけない結果）。#59 より前の結果は
`run_manifest` が無く、そのまま読めます。

| 項目 | 内容 |
|---|---|
| `as_of` / `as_of_source` | 検索の基準日。`filters.as_of` が無ければ評価の開始時の今日（`run_start`）にし、評価の間は固定する（日付を跨いでも全ケースが同じ日。通常の質問は今日のまま） |
| `evaluation_set` | 評価セット（ケース）の sha256 と件数 |
| `request` | top_k・条件の key と値の hash・ナレッジベース・検索・回答プロファイル・`rag_overrides` |
| `settings` | 設定の fingerprint（`rag_config_fingerprint`）・プロンプトの版（`answer_prompt_version`） |
| `models` | 回答・判定・埋め込み（次元）・再ランクのモデル。提供側の重みの版は API で取れないため `snapshot: unverified` |
| `profiles` | 検索・回答プロファイルの更新時刻・設定の sha256・用語と回答ルール・ドメインキーワードの sha256・参照 KB |
| `corpus` | 検索に出る（配信中の）chunk_set の id・件数と、レシピの版・抽出の元の hash と更新時刻を含む行の sha256。active の切替・抽出の更新・削除・権限の剥奪で変わる |
| `code_version` / `external_calls` | backend の git の commit（手元の変更は `+dirty`）と、外部の AI の応答の扱い（`live` / `record` / `replay`）。この 2 つは条件の比較に入れない |

秘密（API キー）・質問と根拠の原文は入れません（条件の値は hash）。読めなかった条件は `unverified` にします。

評価を backend の process の中で流す CLI（`evaluation_run_cli`。job と同じ処理）で、生成・判定・質問の拡張・埋め込み・
再ランクの応答を記録し、API キー・外部のネットワーク無しで同じ評価を再生できます。Oracle は設定どおりに接続します
（検索の結果は固定しない）。

```bash
cd backend
# 1. 実の API で流し、応答を記録する（記録は実データの応答を含む。リポジトリの外か /tmp/ など git に入らない所へ。権限 0600）
uv run python -m app.rag.evaluation_run_cli run ../evaluation/golden-set.json \
  --record ../../tmp/eval/recording.json --output ../../tmp/eval/live.json
# 2. API キー無しで再生する（記録の日を「今日」にする。入力・呼び出しの数・条件が違えば終了コード 2）
uv run python -m app.rag.evaluation_run_cli run ../evaluation/golden-set.json \
  --replay ../../tmp/eval/recording.json --output ../../tmp/eval/replay.json
# 3. 2 つの結果の実行条件の差（job の CLI の出力も読める。差・実行中の変化があれば終了コード 1）
uv run python -m app.rag.evaluation_run_cli diff ../../tmp/eval/live.json ../../tmp/eval/replay.json
```

- 再生は、入力（種類・path・本文の正規化した JSON）の hash ごとに記録の順で応答を返します。記録に無い入力・同じ入力の
  余分な呼び出しは失敗し、**実の API へ fallback しません**。使わなかった記録（呼び出しの不足）も失敗にします。
  並行の呼び出しがあるため、順は同じ入力の中で照合します。記録・再生ではケースを 1 件ずつ流します。
- 時間切れ・接続の失敗・HTTP の失敗（429 など）も記録し、再生で同じ例外を起こします（再試行・時間切れの経路の回帰）。
  不正な構造化出力・拒否・引用も応答の本文として同じに戻ります。
- 記録には入力の本文と API キーを残しません。応答の本文（回答・埋め込み）は残るため、実データの記録は明示した保存先だけに
  置き、公開の場所・GitHub に置かないでください。公開のテストの記録は合成のものだけです。
- 再生は制御の回帰の確認です。今のモデルの能力・実サービスの速さの証明には使いません（結果の
  `run_manifest.external_calls.mode` が `replay`）。

### 2 つの評価の、同じケースの対応のある比較（#60）

平均の数ポイントの差だけでは、改善とケースの少なさ・生成の揺れを区別できません。`compare` は、同じ評価セットの
同じ `case_id` を対にし、指標ごとに改善・悪化・同値の件数と、差（target − base）の平均の区間を出します。
検索・回答プロファイルと比べる評価（`compare_search_answer_profile_id`）の結果にも、同じ比較が
`comparison.paired` として入ります（全体の既定 → プロファイル）。

```bash
cd backend
# 設定を変える前（base）と後（target）を同じ評価セットで流した結果を比べる（job の CLI の出力も読める）
uv run python -m app.rag.evaluation_run_cli compare ../../tmp/eval/before.json ../../tmp/eval/after.json \
  --seed 20260 --resamples 2000 --output ../../tmp/eval/paired.json
```

- 対にするのは両方で測れたケースだけです。片方でもエラーのケース（誤り）・測れなかったケース（欠測）は平均から除き、
  改善にも悪化にも数えません。
- 二値の指標（値が 0 / 1 だけ。応答の判断の正解など）は改善・悪化の件数の正確な符号検定（McNemar の正確検定）の
  p 値で、ほかの指標（MRR・回答の正確性など）は seed を固定した配対 bootstrap（ケースを単位に復元抽出）の
  percentile 区間で、差の向きを判定します。区間はどちらにも出します。同じ結果・seed・反復数なら同じ区間です。
- 判定: `improved` / `regressed`（差の向きが確か）、`unchanged`（全ケースが同値）、`inconclusive`（区間が 0 を
  またぐ・p 値が大きい）、`insufficient_cases`（対が `--min-cases`〔既定 10〕未満の小標本）、`not_measured`。
- 評価セット（`run_manifest.evaluation_set.sha256`）・ケースの id が違う、実行中に条件が変わった結果は比較不能
  （`not_comparable`）にし、差を出しません。比べたい違い（設定・プロファイルなど）は「条件の差」に出します。
- 既存の絶対の閾値・失敗したケースによる不合格（gate）は、統計の判定とは別に出し、差の有無に関係なく終了コード 1
  にします。比較不能・確かな悪化も 1、それ以外は 0 です。教材の近似式・固定の差の閾値は使いません。

### 検索の工程ごとの除去実験（#61）

`ablate` は、全部の工程を使う構成（full）と、工程を 1 つずつ無効にした構成（評価の上書き `rag_overrides`）を、
同じ評価セット・基準日・範囲・索引で順に流し、full との差を表にします。工程は `rerank`・`neighbor_children`・
`reference_expansion`・`entity_expansion`・`graph_expansion`・`section_expansion`（`--stages` で選ぶ）です。

```bash
cd backend
# 1. 実の API で流し、構成ごとの応答も記録する（記録は実データを含む。git に入らない所へ）
uv run python -m app.rag.evaluation_run_cli ablate ../evaluation/golden-set.json \
  --stages rerank,section_expansion,reference_expansion --full-again --record --output-dir ../../tmp/eval/ablation
# 2. 同じ除去実験を API キー無しで再生する（結果は *.replay.json。時間は実の速さではない）
uv run python -m app.rag.evaluation_run_cli ablate ../evaluation/golden-set.json \
  --stages rerank,section_expansion,reference_expansion --full-again --replay --output-dir ../../tmp/eval/ablation
```

`--output-dir` に構成ごとの結果（`full.json`・`no-rerank.json` など。`compare` / `diff` でも読める）と、
表（`ablation.json`）を書きます。表の項目:

| 項目 | 内容 |
|---|---|
| `quality` | 指標ごとの差・区間・判定と、悪化したケースの id（#60 の対応のある比較） |
| `evidence_recall` | 期待する文書が、検索直後（`candidate_fusion`）・rerank の後（`rerank_output`）・生成に渡した根拠（`answer_context`）の選ばれた chunk に入った割合（回答の診断の `retrieval_stages`）。記録の無い位置は `null` |
| `wall_seconds` / `case_median_ms` | 評価全体の wall-clock とケースの時間の中央値（工程の時間は `runs.<構成>.steps`。並列の工程の合計を全体の時間にしない） |
| `calls` / `tokens` | 実の client が送った LLM（`llm`）・埋め込み・再ランクの request の数（再試行・失敗〔`failed`〕を含む）と、応答の `usage` の token。取れない値は `null`（0 にしない） |
| `changed_steps` / `observed_effect` | 動いたケースの数が変わった工程（回答の診断の `execution_steps`）。工程の記録・呼び出し・品質のどれにも差が無ければ `observed_effect: false`（非適用・対象データ無しの可能性。「効かない」と断定しない） |
| `status` | `compared`・`already_off`（full で既に無効なので流さない）・`not_comparable`（評価セットの違い・実行中の変化・上書き以外の条件の差） |
| `full_again` | `--full-again` で最後に full をもう一度流した差（生成の揺れ・実行順の偏りの目安。工程の差はこれと比べて読む） |
| `cache` / `order` | 埋め込み・再ランクの cache は構成ごとに空から始まり、構成の中では効く。DB・OS の cache は最初の構成の後は温まる |
| `unsupported` | 評価の上書きで無効にできず測らない工程（質問の拡張・取込の前置きと分割・画像の検索） |

- 構成の間で順と時間の条件を揃えるため、ケースを 1 件ずつ流します。比較不能の構成があれば終了コード 1、再生が記録と
  一致しなければ 2 です。
- 取込の前置き・分割の比較は同じ chunk の集合の検索の切替ではないため扱いません（別の文書レシピの成果物を比べます）。
  費用は単価の出典を持たないため出さず、呼び出しの数と token だけを出します。

### 業務支援の合成の評価セット（#1231）

`business-support/` に、架空の「サンプル業務ポータル」の資料（PDF 4 件・xlsx 1 件）と、5 分類（資料で答えられる・
確認が要る・現場のデータが要る・資料に答えが無い・資料が矛盾する）の質問 19 問（`business-support.json`。#1284 で区分・往復・既知の条件・必要な根拠を足した）を置いて
います。資料の作り方・取り込み方・実行の手順は [business-support/README.md](./business-support/README.md) を見て
ください。

### 多段の質問の合成の評価セット（#1335・#1352）

`multi-hop/` に、架空の「サンプル社」（と子会社）の業務文書（PDF 13 件・xlsx 2 件。旧版・別の会社・似た規程を含む
子 chunk 約 240）と、資料をまたいで実体をたどる質問 69 問（`multi-hop.json`。dev 40 問・holdout 29 問。種類
`reasoning_type` と段の数 `hops`、段ごとの必要な根拠つき）を置いています。資料と評価セットは
`rag/scripts/multi_hop_corpus.py` が実体のデータから作ります。RAG の内部に実体の層を入れるかを、検索・回答（A）と業務 Agent（D）の必要な根拠の取得率などで確かめる
ための評価セットです。資料の作り方・取り込み方・実行の手順は [multi-hop/README.md](./multi-hop/README.md) を
見てください。`evaluation_cli` の `--split dev` で、区分のケースだけを流せます。評価セットの文書の版
（`document_versions`。#1366）は、`evaluation_corpus_cli` が取り込みのときに旧版を新しい版に置き換えた文書として
登録します（`--keep-superseded-active` で登録しない）。

検索 latency / p95 gate には `search-load.example.json` を使います。`cases`、`repeat`、`concurrency`、`thresholds` を定義し、`/api/search` の client/server p50/p95、error rate、回答フローの工程ごとの p95（`diagnostics.answer.execution_steps` の工程名と時間。例: `質問の理解`・`文書検索（1回目）`）を artifact 化します。`thresholds.stage_p95_ms` の key は工程名です。case の `rerank_top_n`・`mode`・`strategy` は #595 で削除した旧 standard の指定で、書いてあっても読み捨てます。結果 JSON と trend JSON には query / answer / context 原文を残しません。

```bash
cd backend
uv run python -m app.rag.search_load_cli \
  ../evaluation/search-load.example.json \
  --api-base-url https://<staging-host> \
  --output ../evaluation/search-load-result.json \
  --trend-output ../evaluation/search-load-trend.json
```

終了コード:

- `0`: gate 成功。
- `1`: API は応答したが評価 threshold、search load threshold、または error rate が gate 条件を満たさない。
- `2`: golden set / search load scenario ファイルや CLI 引数が不正。
- `3`: 評価 CLI で API 接続、HTTP 応答、レスポンス形式の問題が起きた。

`RAG_EVALUATION_API_BASE_URL`、`RAG_EVALUATION_RUN_API_URL`、`RAG_EVALUATION_API_URL`、`RAG_EVALUATION_TIMEOUT_SECONDS`（job の終わりを待つ秒数。未指定なら job の上限（`time_limit_seconds`）+ 300 秒で、job の上限で打ち切った結果を受け取れるよう、それより長く待つ。超えたら job を取り消して終了コード 3 を返す。#390 / #1430）、`RAG_EVALUATION_POLL_INTERVAL_SECONDS`、`RAG_EVALUATION_TENANT_ID`、`RAG_EVALUATION_USER_ID` でも指定できます。`--api-url` は最優先で、`--api-base-url` は `/api/evaluation/jobs/run` を付与します。`--api-url` や `RAG_EVALUATION_*_API_URL` に以前の同期 API の URL（`/api/evaluation/run`）を指定していても、job の API に読み替えます。nightly（`RAG Evaluation Nightly`）は、workflow の timeout に収まるよう `RAG_EVALUATION_TIMEOUT_SECONDS=900` で待ちます。tenant/user の raw 値は CLI 出力には表示しません。

`RAG_SEARCH_LOAD_API_BASE_URL`、`RAG_SEARCH_LOAD_API_URL`、`RAG_SEARCH_LOAD_TIMEOUT_SECONDS`、`RAG_SEARCH_LOAD_TENANT_ID`、`RAG_SEARCH_LOAD_USER_ID` でも検索 load CLI を指定できます。GitHub Actions の `RAG Evaluation Nightly` workflow は evaluation trend と search-load trend を同じ `rag-evaluation-nightly` artifact に保存します。`workflow_dispatch` の `search_load_path` を空文字にすると search load gate だけを skip できます。

## File Processing Golden Fixtures

`docs/evaluation/file-processing-golden-set.json` は PDF / image / Office / HTML / Markdown / TSV table / email / duplicate / corrupted file の取込品質を追跡する manifest です。各 case は `scenario`、期待 parser profile、期待 chunk template、必須 check を持ち、`evaluation/file-processing-fixtures/` のサンプルファイルを参照します。音声・動画は取り込まない形式で、アップロードで止めるため(#3369)、manifest に case を持ちません。

同梱 fixture は標準ライブラリだけで再生成できます。

```bash
python3 scripts/generate_file_processing_fixtures.py
```

local CI では backend から contract gate を実行できます。出力 JSON には OCR 原文や chunk 本文を含めず、case ごとの parser/template/check 結果、manifest の全 metric に対する `metric_summary`、および manifest `thresholds` を評価した `threshold_results` だけを保存します。各 metric は `measured` / `partial` / `requires_staging` を明示し、staging が必要な metric を local gate で未検証のまま成功扱いしません。local では parser fallback 率、表 QA、element lineage、低信頼文書率、失敗 segment 率などを測定し、モデルの API が必要な page coverage / OCR / reading order は staging pending として残します。local で測定できる threshold が未達の場合は CLI が exit `1` を返します。

```bash
cd backend
uv run python -m app.rag.file_processing_golden_cli \
  ../docs/evaluation/file-processing-golden-set.json \
  --output ../evaluation/file-processing-report.json \
  --trend-output ../evaluation/file-processing-trend.json
```

出力の `staging_requirements` は、モデルの API / 保存先 / Oracle AI Database / UI preview が必要な pending check を case 単位で列挙します。`--trend-output` は `file-processing-trend.json` として、parser fallback rate、表 QA、page hit、bbox / preview addressability、source/backend coverage、real-world staging dataset policy summary、threshold status、result hash だけを含む非機密 trend snapshot を保存します。case detail、fixture path、OCR 原文、chunk 本文、検索 query / answer は含めません。nightly workflow はこの gate を先に実行し、`file-processing-report.json` と `file-processing-trend.json` を artifact に保存します。前回の非機密 trend を baseline として保持している場合は、trend regression gate で table QA / page hit / bbox / fallback rate / real-world policy / ingestion p95 などの退化を CI で止められます。

```bash
cd backend
uv run python -m app.rag.file_processing_trend_cli \
  ../evaluation/file-processing-trend.json \
  --baseline ../evaluation/baselines/file-processing-trend.json \
  --output ../evaluation/file-processing-trend-regression.json
```

staging で pending を残したくない場合は CLI の `--fail-on-pending`、または workflow dispatch の `fail_on_file_processing_pending=true` を使います。

実 staging 環境で pending check を閉じる場合は、保存先に fixture を保存し、一時 KB / document を作成して ingestion / search / chunk metadata / segment checkpoint / extraction artifact cache を検証する staging gate を実行します。結果 JSON は parser、segment、bbox、citation、canonical duplicate、artifact reuse の非機密 evidence と、retrieval recall / groundedness / ingestion p95 / bbox / citation traceability / element lineage / page hit / extraction page coverage / low confidence rate / failed segment rate の aggregate metrics、`threshold_results` だけを持ち、OCR 原文・chunk 本文・tenant/user secret は保存しません。staging report には `adapter_golden_gate` と `object_storage_artifact_chain` も含まれ、Docling / Unstructured の enabled/installed 表示ではなく同一 golden set の table QA / page hit / fallback / bbox / source coverage / contract 結果と、Object Storage put/get roundtrip、ingestion 後の full/segment artifact readback、artifact identity metadata 検証、segment artifact reuse を promotion blocker として扱います。artifact 本体は `raw_text` を含み得ますが、staging report / trend には readable/identity/count/byte 数だけを残し、OCR 原文は audit payload へ出しません。`--parser-adapter-contract-strict` は manifest の `fixture_root` と正向きの schema-remap `cases[].fixture` を contract runner に渡し、selected adapter ごとに少なくとも 1 件の passed remap case を要求します。インストール済み adapter が実 package + 実 fixture で `StructuredExtraction` へ remap できた証跡として `case_id` hash / `scenario` / parser backend / adapter import・distribution・version / schema count / reason code を残し、distribution/version metadata や schema-remap 証跡がない場合は `adapter_distribution_name_missing`、`adapter_package_version_missing`、`adapter_schema_remap_evidence_missing` で promotion blocker にします。corrupted / unsupported fixture は adapter smoke ではなく staging の safe-error / warning taxonomy gate で検証します。`staging_dataset_policy` が設定されているのに promotion 必須でない場合は `staging_dataset_policy_not_required`、必須 policy の実データ数 / source kind / scenario / 非機密レビュー / fixture 隔離が未達の場合は `staging_dataset_policy_failed` として止めます。blocker は件数と不足 source kind / scenario だけを出し、real-world case id や fixture path は出しません。staging で測定された threshold が未達の場合も report は `passed=false` になります。

外部依存へ接続する前に、まず preflight でモデルの API / Oracle / 保存先の必須設定だけを確認できます。preflight 出力も secret 値を含みません。

```bash
cd backend
uv run python -m app.rag.file_processing_staging_cli \
  ../docs/evaluation/file-processing-golden-set.json \
  --preflight-only \
  --output ../evaluation/file-processing-staging-preflight.json
```

```bash
cd backend
uv run python -m app.rag.file_processing_staging_cli \
  ../docs/evaluation/file-processing-golden-set.json \
  --output ../evaluation/file-processing-staging-report.json \
  --trend-output ../evaluation/file-processing-staging-trend.json \
  --cleanup
```

nightly workflow でも `run_file_processing_staging=true` を指定すると同じ staging gate を実行し、`file-processing-staging-report.json` と `file-processing-staging-trend.json` を artifact に保存します。staging trend は retrieval recall、表 QA、page hit、bbox / preview addressability、adapter contract、adapter golden gate、保存先の artifact chain、segment artifact reuse、runtime check status、real-world dataset policy coverage を時系列比較するための非機密 summary で、case detail や gate evidence は含めません。staging 昇格では `--require-promotion-ready` を付けると、baseline が promotion ready だったのに current が blocker 付きへ戻る退化も失敗扱いにできます。promotion gate は閾値を緩める変更だけでなく、`required_for_promotion=false`、`pending_checks_block_promotion=false`、必須 runtime check の削除も blocker にします。

```bash
cd backend
uv run python -m app.rag.file_processing_trend_cli \
  ../evaluation/file-processing-staging-trend.json \
  --baseline ../evaluation/baselines/file-processing-staging-trend.json \
  --output ../evaluation/file-processing-staging-trend-regression.json \
  --require-promotion-ready
```

GitHub Actions では `file_processing_trend_baseline_path` と `file_processing_staging_trend_baseline_path` を指定すると、同じ比較結果を `file-processing-trend-regression.json` / `file-processing-staging-trend-regression.json` として artifact に保存し、退化時に workflow を失敗させます。モデルの API / Oracle / 保存先の接続情報は runner 環境の `.env` または GitHub Actions の secret/variable から注入してください。

これらは local CI で parser routing、chunk lineage、manifest/asset 契約を安定検証するための小さな合成サンプルです。モデルの API の OCR/reading order 品質、実スキャン PDF、実 Office レイアウト、実メール添付を gate する場合は、staging 用の非機密実データセットを追加し、同 manifest の scenario と required check に対応付けて nightly で評価してください。staging 用 manifest には `staging_dataset_policy` を入れると、real-world case 数、source kind / scenario coverage、非機密レビュー、fixture 隔離を manifest validation と promotion blocker の両方で強制できます。real-world case は synthetic fixture の流用ではなく、`fixture_kind: "real_world"`、`data_sensitivity: "non_sensitive"`、`reviewed_for_public_ci: true` を持ち、既定では `staging/` 配下の fixture を参照する必要があります。

```json
{
  "staging_dataset_policy": {
    "required_for_promotion": true,
    "min_real_world_cases": 6,
    "required_source_kinds": ["pdf", "office", "html", "email", "image"],
    "required_scenarios": [
      "scanned_pdf_ocr",
      "two_column_pdf_reading_order",
      "japanese_docx_layout",
      "japanese_pptx_slides",
      "japanese_xlsx_sheets",
      "email_thread_headers"
    ],
    "required_fixture_prefix": "staging/"
  }
}
```
