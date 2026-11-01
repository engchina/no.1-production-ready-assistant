# 実行補助の寄与の比較（除去実験。#69）

Agent の実行補助（回答の最終の検証・根拠のツールを呼ばない回答の差し戻し・文脈の縮小）が、同じタスクの完了品質にどれだけ寄与し、どれだけ遅延・token・呼び出しを増やすかを、**補助を 1 つずつ外した品質評価**で測る。最終の合格率だけでは、品質への寄与が小さいのに手間だけを増やす補助を見分けられない。

RAG の検索の工程（rerank・近傍・参照・実体・関係・章節の拡張）の寄与と MRR / Recall は、RAG の除去実験（`rag/backend/app/rag/evaluation_ablation.py`。`evaluation_run_cli ablate`）が別に出す。検索の指標が良くなったことと、Agent の最終の判断が良くなったことは別に報告する。

## 外せる補助

| `disabled_aids` の値 | 外したときの動き | 全体の既定の設定 | 動いたことの記録 |
|---|---|---|---|
| `final_validation` | 回答の保存の前の RAG の `rag_validate_answer` の検証をしない | `AGENT_FINAL_VALIDATION_ENABLED` | 成果物 `answer_validation`（`status=completed`） |
| `tool_grounding` | 根拠のツールを呼ばずに作った回答の差し戻し・拒答をしない（回答前の確認の前に調べさせることもしない） | `AGENT_TOOL_GROUNDING_REQUIRED` | Run のイベント `tool_grounding` |
| `context_compaction` | モデルの入力の上限に収めるための古いツールの結果の縮小をしない | `AGENT_CONTEXT_COMPACTION_ENABLED` | Run のイベント `context_compaction` |

- 外すのは**その品質評価の Run（dry-run）だけ**。印は Run の metadata `evaluation_disabled_aids`（予約の key）で、評価の Run でなければ読まない。利用者の Run・本番の設定・全体の既定は変えない。
- 外せるのは回答の品質のための補助だけ。ツール権限・承認・データの範囲・機微情報の保護・実行予算は外せない（値に無い名前は 422）。評価の Run は承認が要るツールを実行しない（dry-run）。
- 外せない補助: 支援タスクの状態（前の Run から引き継ぐ状態）・スキルの参照文書の開示は、外すとスキルの宣言どおりに動かなくなるので対象にしていない。

## 手順

同じ評価セット・同じ版（下書き / 公開中）・同じ試行の回数で、全構成 → 補助を 1 つずつ外した評価 → もう一度全構成、の順に流す（品質評価は 1 度に 1 つしか動かないので、前の評価が終わってから次を始める）。

```bash
B=http://localhost:8120/api   # 画面と同じ認証（Cookie / CSRF）で呼ぶ。手順は品質評価の API と同じ
# 1. 全構成（基準）
curl -X POST $B/evaluations -d '{"set_id":"<評価セット>","agent_version":"draft","trials":3}'
# 2. 補助を 1 つずつ外す
curl -X POST $B/evaluations -d '{"set_id":"<評価セット>","agent_version":"draft","trials":3,"disabled_aids":["final_validation"]}'
curl -X POST $B/evaluations -d '{"set_id":"<評価セット>","agent_version":"draft","trials":3,"disabled_aids":["tool_grounding"]}'
curl -X POST $B/evaluations -d '{"set_id":"<評価セット>","agent_version":"draft","trials":3,"disabled_aids":["context_compaction"]}'
# 3. 全構成をもう一度（順序・cache の温まりの目安）
curl -X POST $B/evaluations -d '{"set_id":"<評価セット>","agent_version":"draft","trials":3}'
# 4. 比較の表（保存した job と Run の記録だけから作る。モデル・MCP は呼ばない）
curl "$B/evaluations/ablation?base_job_id=<1>&target_job_id=<2a>&target_job_id=<2b>&target_job_id=<2c>&target_job_id=<3>"
```

- 補助を外した評価は、公開前の合格率・回帰の門の評価にも、同じ評価セットの「前回」にも使わない（公開する構成の評価ではない）。**安全違反は外した評価でも公開を止める**（外せるのは品質の補助だけで、権限・承認は同じなので、違反は同じ下書きの違反である）。
- 評価の Run は通常の品質評価と同じく、評価を始めた利用者の権限で RAG / NL2SQL を呼ぶ（書き込みはしない）。評価の job と Run は通常の品質評価と同じ保存先に残る。

## 表の読み方（`GET /api/evaluations/ablation`）

| key | 内容 |
|---|---|
| `base` | 基準の評価（業務 Agent・版・評価セット・試行の回数・評価者の指紋）と `metrics` |
| `order` | 流した順（最初の評価は cache が冷えている） |
| `rows[].name` | `no-<補助>`（外した補助）、`full-again`（全構成の繰り返し） |
| `rows[].decision` / `reason` | 判定と根拠の文（下） |
| `rows[].paired` | 同じ定義のケースの成功率の対応のある差（比べる − 基準）と 95% 区間（#62 の MOVER。単位はケース） |
| `rows[].regressed_questions` / `improved_questions` | 外して悪化 / 改善したケースの質問 |
| `rows[].observed.<補助>` | 外した補助のコード経路が動いた回数（基準 / 外した評価）。基準で 0 なら外しても同じ |
| `metrics.quality` | 合格率と区間・判定できたケース・正解 |
| `metrics.completion` | ケースの状態の数（判定・実行の失敗・時間切れ・承認の停止・取消など）・確認で終わったケース・調べる前に断った回答 |
| `metrics.safety` | 安全違反（#63） |
| `metrics.model_calls` / `tokens` / `tool_calls` / `mcp_calls` / `rag_calls` | Run ごとの合計・p50・p95（モデルの呼び出しと token は Run の usage、ツールは step） |
| `metrics.latency_ms` | ケースの所要時間の p50 / p95 |
| `metrics.aid_activity` | 補助ごとの動いた回数と、動いた Run の数 |

判定:

| `decision` | 条件 | 読み方 |
|---|---|---|
| `adopt` | 外すと品質が下がり、区間が 0 をまたがない | 寄与がある。補助を続ける |
| `reconsider` | 外すと品質が上がり、区間が 0 をまたがない | 補助が害になっている可能性。中身を見直す |
| `undetermined` | 区間が 0 をまたぐ | 差が確かでない。**差が見えないことは不要の証明ではない**。外したときの token・呼び出し・p50 の差を添える |
| `not_applicable` | 基準の評価で補助のコード経路が動いていない | 対象のケースが無い。外しても同じ（判断の材料にしない） |
| `not_comparable` | 評価ケース・業務 Agent / 版・下書きの中身・評価者・試行の回数のどれかが違う | 比べない |
| `baseline_repeat` | 同じ構成の繰り返し | 揺れ・順序の偏りの目安。基準との差がこの幅より小さい補助の差は読まない |

## RAG の検索の寄与との関係

1. RAG の除去実験を、同じ質問の RAG の評価セットで流す（`cd rag/backend && uv run python -m app.rag.evaluation_run_cli ablate <set> --output-dir <dir> --full-again`）。工程ごとの品質・根拠の再現率（検索直後・rerank の後・生成に渡した根拠）・呼び出し・時間が出る。
2. Agent の表の `rag_calls` と完了品質を並べる。RAG の工程を外すと根拠の再現率が下がっても、Agent の完了品質が変わらなければ、その工程は「検索の指標には効くが、このタスクの最終の判断には効いていない」（逆も同じ）。
3. 同じ検索の条件で比べたかは、RAG の除去実験の実行条件（`run_manifest` の検索・回答プロファイルの版）で確かめる。

## 実験の条件

- 同じモデル・同じケース・同じ評価者・同じ試行の回数・同じ実行予算で流す（全体の設定を評価の間に変えない）。
- cold / warm: 最初の評価は RAG / DB / モデル側の cache が冷えている。全構成をもう一度流し（`full-again`）、遅延・品質の差の幅を見る。
- 小さな評価セットでは区間が広く、ほとんどが `undetermined` になる。試行の回数（`trials`）とケースを増やして区間を狭める（#62）。
