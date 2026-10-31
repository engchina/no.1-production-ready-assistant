"""製品の評価と suite の評価が共有する、決定的な採点の部品（#3240）。

- `text`: 語の照合の正規化（NFKC・大小文字・空白を無視）。
- `outcomes`: 回答の対応の語彙と、回答・確認・保留の判断。
- `evidence`: 必要な根拠と引用の chunk の照合（文書・ページ・シート・行・章節・本文・表の行）。
- `retrieval`: 正解の文書の順位（MRR）と取れた文書。
- `aggregate`: 測れたケースだけの平均と分母、閾値の判定、合否。
- `agreement`: 人手の判定と評価器の判定の一致（一致率・Cohen の κ・混同行列・誤合格 / 誤不合格）と、
  評価器の校正の合否（holdout の件数・判定率・κ・誤合格の率）。
- `labeling`: 人手の判定を集める部品（区分の決め方・誤りの型と目標・記入者の間の一致・判定の揺れ・
  LabelSet の項目の名前）。suite の記入用のブックと RAG の判定の校正の画面が共有する（#3578）。
- `perturbation`: 故意に壊した回答（合成の対照）の壊し方と選び方。
- `rubric`: 回答の判定の基準（同梱の JSON）と、観点の基準の文・判定する観点。

製品の型（RAG の `SearchResponse` など）は持たない。使う側が自分の型から、ここの関数の入力
（文書の ID・本文・場所の Mapping・対応の文字列）へ変換する。LLM・HTTP・DB を呼ばない。
"""

from pr_evaluation_scoring.aggregate import (
    MetricAggregate,
    ThresholdFailure,
    count_reasons,
    overall_passed,
    rounded_mean,
    threshold_failures,
)
from pr_evaluation_scoring.agreement import (
    Agreement,
    CalibrationThresholds,
    LabelPair,
    agreement,
    calibration_failures,
    cohen_kappa,
)
from pr_evaluation_scoring.evidence import (
    EvidenceCriteria,
    evidence_matches,
    location_matches,
    table_row_texts,
    text_matches,
    text_outside_tables,
)
from pr_evaluation_scoring.labeling import (
    ASPECT_HOLDOUT_TARGET,
    ASPECT_LABEL_TARGET,
    CANNOT_JUDGE,
    ERROR_TYPE_IDS,
    ERROR_TYPES,
    FAIL,
    HUMAN_LABELS,
    LABEL_CHOICES,
    PASS,
    ErrorType,
    LabelVariance,
    PairAgreement,
    agreed_label,
    assign_splits,
    label_variance,
    pairwise_agreement,
)
from pr_evaluation_scoring.outcomes import (
    DECISIONS,
    NO_RETRIEVAL_OUTCOMES,
    OUTCOMES,
    Decision,
    all_correct,
    expected_decisions,
    response_decision,
    skips_retrieval,
)
from pr_evaluation_scoring.retrieval import document_hits, reciprocal_rank, unique_in_order
from pr_evaluation_scoring.rubric import (
    applicable_aspects,
    aspect_criteria,
    aspect_ids,
    load_rubric_data,
)
from pr_evaluation_scoring.text import contains_normalized, normalize_for_match, normalize_name

__all__ = [
    "ASPECT_HOLDOUT_TARGET",
    "ASPECT_LABEL_TARGET",
    "CANNOT_JUDGE",
    "ERROR_TYPE_IDS",
    "ERROR_TYPES",
    "FAIL",
    "HUMAN_LABELS",
    "LABEL_CHOICES",
    "PASS",
    "ErrorType",
    "LabelVariance",
    "PairAgreement",
    "agreed_label",
    "applicable_aspects",
    "aspect_criteria",
    "aspect_ids",
    "assign_splits",
    "label_variance",
    "load_rubric_data",
    "pairwise_agreement",
    "Agreement",
    "CalibrationThresholds",
    "LabelPair",
    "agreement",
    "calibration_failures",
    "cohen_kappa",
    "DECISIONS",
    "NO_RETRIEVAL_OUTCOMES",
    "OUTCOMES",
    "Decision",
    "EvidenceCriteria",
    "MetricAggregate",
    "ThresholdFailure",
    "all_correct",
    "contains_normalized",
    "count_reasons",
    "document_hits",
    "evidence_matches",
    "expected_decisions",
    "location_matches",
    "normalize_for_match",
    "normalize_name",
    "overall_passed",
    "reciprocal_rank",
    "response_decision",
    "rounded_mean",
    "skips_retrieval",
    "table_row_texts",
    "text_matches",
    "text_outside_tables",
    "threshold_failures",
    "unique_in_order",
]
