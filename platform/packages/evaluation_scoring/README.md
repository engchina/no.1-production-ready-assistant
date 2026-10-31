# production-ready-evaluation-scoring (`pr_evaluation_scoring`)

製品の評価（RAG の評価ランナー）と製品横断の評価（`suite/evaluation/`）が共有する、決定的な採点の部品（#3240）。標準 library だけで書き、LLM・HTTP・DB を呼ばない。

| モジュール | 内容 |
|---|---|
| `text` | 語の照合の正規化（`normalize_for_match`: NFKC・casefold・空白を除く）と部分一致（`contains_normalized`） |
| `outcomes` | 回答の対応の語彙（`OUTCOMES`。RAG の `rag_search.outcome` と Agent の `RunOutput.outcome` の公開の契約と同じ 6 つ）、回答・確認・保留の判断（`response_decision`・`expected_decisions`）、検索の指標の対象外（`skips_retrieval`）、往復の判断の 3 値の論理積（`all_correct`） |
| `evidence` | 必要な根拠と引用の chunk の照合（`evidence_matches`。文書・ページ・シート・行・章節・本文。表は行ごとに照合する）。場所は RAG の `EvidenceLocator` と同じ名前の Mapping で受け取る。行は同じ行の番号の基準（`row_basis`）の中だけで照合し、評価セットの行（原本の行）を解析の表の格子の行（`parsed_table`）に一致させない |
| `retrieval` | 正解の文書の順位の逆数（`reciprocal_rank`）・取れた文書（`document_hits`） |
| `aggregate` | 測れたケースだけの平均と分母（`MetricAggregate`）、閾値の判定（`threshold_failures`）、合否（`overall_passed`） |
| `agreement` | 人手の判定と評価器の判定の一致（`agreement`: 一致率・Cohen の κ・混同行列・誤合格 / 誤不合格）と、評価器の校正の合否（`CalibrationThresholds`・`calibration_failures`: holdout の件数・判定率・κ・誤合格の率）。suite の LLM による判定と RAG の回答の評価器が同じ基準で校正する（#3418） |
| `labeling` | 人手の判定を集める部品（区分 `assign_splits`・誤りの型と目標 `ERROR_TYPES`・記入者の間の一致 `agreed_label` / `pairwise_agreement`・判定の揺れ `label_variance`・LabelSet の項目の名前）。suite の記入用のブックと RAG の判定の校正の画面が共有する（#3578） |
| `perturbation` | 故意に壊した回答（合成の対照）の壊し方と選び方（`perturb_items`） |
| `rubric` | 回答の判定の基準（同梱の `rubrics/answer_quality_v1.json`）と、観点の基準の文・判定する観点 |

## 置くもの・置かないもの

- 置くもの: 製品の評価と suite の評価の両方が、同じ意味で使う採点の関数だけ。
- 置かないもの: 製品の型（RAG の `SearchResponse`・`EvaluationCase` など）、評価セットの読み込み、製品の呼び出し、LLM による判定、旧形式の推定（Agent の旧形式は `suite/evaluation/` の Agent の adapter）。使う側が自分の型から、ここの関数の入力（文書の ID・本文・場所の Mapping・対応の文字列）へ変換する。
- 依存の向き: 製品と suite がこの package を使う。この package は製品・suite を import しない。

## テスト

```bash
uv sync --dev
uv run pytest -n auto
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

RAG の評価が同じ結果を出すこと（表の行の照合を含む）は、RAG の `tests/test_evaluation_scoring_parity.py` が確かめる。
