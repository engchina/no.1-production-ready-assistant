# 業務支援の合成の評価セット（#1231）

業務支援の改修（#1218）で、回答が「資料で答えられる」「確認が要る」「現場のデータが要る」「資料に答えが無い」
「資料が矛盾する」の 5 分類の質問に正しく対応するか（対応・手順・危険な回答・条件）を測るための、合成の評価セットです。
資料はすべて架空の「サンプル業務ポータル」の内容で、実在の顧客・製品の内容は入れていません。

| ファイル | 内容 |
|---|---|
| `portal-operations-manual.pdf` | 運用手順書（検証用アカウントの登録・権限の付与（個別 / グループ）・通知・反映待ち・削除） |
| `regional-report-guide-v2.pdf` / `-v1.pdf` | 地域別集計表の出力手順。第 2 版（締め日 10 日）が第 1 版（5 日）を置き換える（資料の矛盾） |
| `error-e1023-notes.pdf` | 障害対応メモ。原因は 2 つのどちらかで、認証ログを見ないと分からない（現場のデータ） |
| `portal-parameters.xlsx` | パラメータの一覧（既定値・設定例。現場の現在の設定値ではない） |
| `business-support.json` | 評価セット（19 問。うち往復のあるケース 3 問・既知の条件を渡すケース 1 問）。`relevant_document_ids` と `required_evidence[].document_id` は `file:<ファイル名>` |
| `support-guides.json` | C（業務ガイドあり）の業務ガイド 2 つ（アクセス権限の付与: 付与先 `target` 個別 / グループ、アカウントの削除: 部門長の承認 `approved` はい / いいえ）。運用手順書の記載だけから作った（#1289）。参照は `file:<ファイル名>`。アクセス権限の付与の承認・影響範囲（グループ）は、グループに付与する分岐の手順だけに係る（`impact.steps`。#1320） |
| `sources/*.html` | PDF の原稿 |
| `sources/portal-parameters.workbook.json` | `portal-parameters.xlsx` の原稿（シートの前書き・表頭・行） |

## 資料を作り直す

原稿（`sources/*.html` と `sources/*.workbook.json`）を直したら、リポジトリ直下から次で PDF / xlsx を作り直して
commit します（PDF の生成には LibreOffice と日本語のフォントが要ります。xlsx だけなら `--xlsx-only`）。生成の script は
多段の質問の評価セット（[../multi-hop/](../multi-hop/README.md)。#1335）と共通です。

```bash
uv run --project rag/backend python rag/scripts/generate_evaluation_corpus.py rag/evaluation/business-support
```

## 取り込んで評価する

評価セットの `file:` の参照は、配備先で取り込んだ文書の ID に置き換えてから実行します。`evaluation_corpus_cli` が
ナレッジベースを作り、資料をアップロードして索引まで待ち（確認待ちのゲートは承認する。Excel は前処理
`excel_to_jsonl`）、置き換えた評価セットを書き出します。

```bash
cd rag/backend
uv run python -m app.rag.evaluation_corpus_cli ../evaluation/business-support/business-support.json \
  --api-base-url http://127.0.0.1:8100 --output /tmp/business-support.resolved.json
uv run python -m app.rag.evaluation_cli /tmp/business-support.resolved.json \
  --api-base-url http://127.0.0.1:8100 --output /tmp/business-support.result.json
```

同じナレッジベースへ取り込み直すときは `--knowledge-base-id` を渡します。質問ごとに回答（モデルの呼び出し 3〜5 回）を
作るため、19 問（往復は返答ごとに回答を作る）で 20 分前後かかります。

## A / C / D を比べる（handoff §14.3。#1289・#3417）

A・C・D は製品横断の評価（`suite/evaluation`。[README](../../../suite/evaluation/README.md)）で、同じ評価セットを
同じ採点器（Evaluator）で採点して比べます。

| 経路 | 内容 | 実行 |
|---|---|---|
| A | 業務ガイドなし（ナレッジベースの指定） | `suite-evaluation run --target rag-mcp --knowledge-base-id` |
| C | 業務ガイドあり（検索・回答プロファイルを指定。#1249） | `suite-evaluation run --target rag-mcp --search-answer-profile-id` |
| D | C と同じ検索・回答プロファイルを使う業務 Agent（スキル `business-rag-research`）の Run | `suite-evaluation run --target agent-mcp --agent-id` |

`evaluation_corpus_cli` に `--guides` を渡すと、資料を取り込んだナレッジベースを参照する検索・回答プロファイルを作り、
`support-guides.json`（参照の `file:` は取り込んだ文書の ID に置き換える）を取り込んで公開します（出力にナレッジベースと
検索・回答プロファイルの ID が出ます）。`--document-map-output` は、取り込んだファイル名 → 文書 ID の対応表を書きます
（suite の評価が評価セットの `file:` を解決する）。

```bash
cd rag/backend
uv run python -m app.rag.evaluation_corpus_cli ../evaluation/business-support/business-support.json \
  --api-base-url http://127.0.0.1:8100 --output ../../tmp/eval/bs.resolved.json \
  --document-map-output ../../tmp/eval/document-map.json \
  --guides ../evaluation/business-support/support-guides.json --guided-output ../../tmp/eval/bs.guided.json
cd ../../suite/evaluation
export SUITE_EVALUATION_API_KEY=prak_...   # アカウントの画面の「API キー」のタブで発行したキー（使える製品に rag と agent）
SET=../../rag/evaluation/business-support/business-support.json
MAP=../../tmp/eval/document-map.json
# A / C（RAG の公開の MCP の rag_search）
uv run suite-evaluation run "$SET" --document-map "$MAP" --target rag-mcp \
  --mcp-url http://127.0.0.1:8100/api/mcp --knowledge-base-id <ナレッジベース> --output ../../tmp/eval/a.json
uv run suite-evaluation run "$SET" --document-map "$MAP" --target rag-mcp \
  --mcp-url http://127.0.0.1:8100/api/mcp --search-answer-profile-id <検索・回答プロファイル> --output ../../tmp/eval/c.json
# D（業務 Agent の Run。公開済みの業務 Agent を ID で指定する。業務 Agent は画面で、C と同じ検索・回答プロファイルを
# データの範囲にしてスキル business-rag-research を付けて作り、公開しておく）
uv run suite-evaluation run "$SET" --document-map "$MAP" --target agent-mcp \
  --mcp-url http://127.0.0.1:8120/api/mcp --agent-id <公開済みの業務 Agent> \
  --guides ../../rag/evaluation/business-support/support-guides.json --output ../../tmp/eval/d.json
# 比べる表（docs/answer-baseline-2026-10.md に貼る）
uv run suite-evaluation compare A=../../tmp/eval/a.json C=../../tmp/eval/c.json D=../../tmp/eval/d.json
```

A / C は RAG の公開の MCP（`rag_search`）だけを、D は業務 Agent の公開の MCP（`agent_chat`・`agent_get_run`・
`agent_answer_confirmation`・`agent_get_run_observation`・`agent_cancel_run`）だけを、利用者が発行した API キーで呼びます。
採点は六指標で、照合・順位・判断は platform の採点の部品（`pr_evaluation_scoring`）を RAG 自身の評価と共有します。
回答の対応（`outcome`）の無い Run・本文を省かれた根拠・見られない根拠は推測で埋めず「評価不能」と理由を残します
（採点の規則と版は suite の README）。API キーの上限（1 分あたりの呼び出し・直近 24 時間の Run の数。
`platform/docs/mcp-api-keys.md`）に数えられます。評価は 1 回 5 問ずつ流します（`--case`）。

RAG 自身の評価（`evaluation_cli` に `--output` / `--guided-output` の評価セット。評価の job・LLM による回答の判定・
`rag_overrides` を含む）は、RAG の設定の調整のために使えます（閾値を下回ると終了コード 1 を返しますが、結果の JSON は
書きます）。その結果は suite の `compare` には渡しません（採点器が違うため）。

## 採点の考え方

- `expected_outcomes` は受け入れる対応の集合です。確認が要る質問は、確認の質問（`needs_clarification`）か、条件ごとに
  分けた回答（`conditional`）のどちらでも正しいとします（handoff §8.1「条件ごとの回答を安全に出せるなら条件付きの回答
  でよい」）。
- `forbidden_phrases` は、影響範囲を広げる操作・承認の無い削除・現場の値の断定など、勧めると危険な表現です。回答が
  「〜しない」と否定して書く場合に当たらないよう、勧める言い方で書きます。
- 回答の対応は、回答の記録が `outcome` を持つまでは診断からの推定です（結果の `outcome_source=inferred`）。推定は
  `needs_human_review` を「条件付きの回答」として扱うなど粗いため、分類ごとの内訳を見るときは推定であることに注意して
  ください。
- 語の照合（手順・危険な表現・条件・必要な根拠）は NFKC・大小文字・空白を無視します（全角の「３０」と「30」、
  改行の入り方の違いで結果が変わりません）。

## 区分・往復・既知の条件・別解・必要な根拠（#1284。handoff §14.1 / §14.2）

| 欄 | 意味 |
|---|---|
| `split` | `dev`（設定・業務ガイドの調整に使ってよい）/ `holdout`（調整に使わず、最後の確認だけに使う）。結果の `split_breakdown` に区分ごとの全指標・件数・失敗理由が出ます |
| `conditions` | 質問と一緒に渡す既知の条件の値（条件の id → 値）。検索・回答の request の `conditions` に渡します（チャットの確認の答え・MCP の `conditions` と同じ） |
| `turns` | 確認の質問への返答の列。各往復は `reply`（利用者の次の発話）・`conditions`（返答で分かった条件の値。前の値に足す）と、その往復の回答への期待（`expected_outcomes` / `expected_steps` / `acceptable_alternatives` / `forbidden_phrases` / `required_conditions`）を持ちます |
| `acceptable_alternatives` | `expected_steps` と同等の別の手順の列（例: 「ログアウト → ログインし直す」と「再ログイン」）。いちばん点の高い列で採点します |
| `required_evidence` | 最後の回答に必要な根拠（`id`・`document_id`・`text`）。引用のうち、その文書の chunk の本文が `text` を含めば取れたとみなし、ケースの `evidence_recall` と全体の `required_evidence_recall`（参考の集計。閾値には使わない）を出します |

- 往復のあるケースは、チャットと同じく返答を次の質問にし、前の往復（質問と回答）を会話の履歴として順に実行します。
  検索・回答プロファイルを指定した評価では、前の質問も業務ガイドの照合に使います。ケースの直下の対応・手順・危険な
  表現・条件の期待は最初の回答を、各往復の期待はその往復の回答を採点し、正解の文書・期待する語・必要な根拠は最後の
  回答で採点します。往復ごとの採点は結果の `turn_results` にあります。
- 渡した条件を確認の質問でもう一度聞いたら（回答の記録の `clarifications` の `condition_id`）、失敗理由
  `known_condition_reasked` を付けます。
- `conditions` の id は、業務ガイド（#1238）の条件の id に合わせます。この評価セットは、付与先を `target`（個別 /
  グループ）、部門長の承認を `approved`（はい / いいえ）とする業務ガイドを前提にしています（業務ガイドは
  `support-guides.json`。#1289）。業務ガイドを使わない評価（検索・回答プロファイルを指定しない評価）では、条件は回答に
  影響しません。
- 区分は、業務の流れ・資料の族ごとに分けています（同じ族の言い換えが dev と holdout に分かれないように）。holdout は
  アカウントの削除・パラメータの一覧・地域別集計表の版・資料に無いアカウントの自動の無効化の族で、19 問のうち 7 問
  （5 分類のどれも含む）です。holdout の結果を見て評価セットや業務ガイドを直したら、そのケースは dev に移します。
- 往復の返答・必要な根拠の語句と業務ガイドは、同梱の資料の記載だけから作っています（実在の顧客のデータや、資料に無い
  値を足していません）。業務ガイドの質問の例・照合の語には、評価セットの質問（特に holdout）を写していません（handoff
  §14.1「指南の生成・設定に使った質問は未見のテストにしない」）。アカウントの削除の業務ガイドは holdout の族に当たる
  ため、holdout の結果を見て業務ガイドを直したら、そのケースは dev に移します。

## nightly で流さない理由

nightly（`.github/workflows/rag-evaluation-nightly.yml`）の評価は、repo variable の `RAG_EVALUATION_API_BASE_URL`
が指す配備先の API に評価セットを送るだけで、variable が無ければ skip します（2026-10-08 の時点で未設定）。この
評価セットは、配備先に資料を取り込み（`evaluation_corpus_cli`。parser とモデルの API / Oracle が要る）、`file:` の参照を
文書 ID に置き換えてからでないと採点できず、回答も 1 問 3〜5 回のモデルの呼び出しで 20 分前後かかります（nightly
の評価の待ちの上限は 15 分）。新しい secret・変数（取り込んだナレッジベースの ID など）と実環境が要るため、nightly
には入れず、手動（上の「取り込んで評価する」）で流します。
