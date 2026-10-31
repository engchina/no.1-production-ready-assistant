"""六指標の評価基準の決定論的な解決。

標準は根拠取得・正確性・網羅性・応答判断の九割、主張の支持の九割五分を目安とする。
厳格は MRR を除く全指標で完全一致を求める。MRR は標準 0.6、厳格 0.8。
設定の解決だけを担当し、前提不足の未評価は合格に置き換えない。"""

from __future__ import annotations

from dataclasses import dataclass

EVALUATION_SUITES: tuple[str, ...] = ("standard", "strict")
DEFAULT_EVALUATION_SUITE = "standard"
# 旧プリセット(#591 で削除)の名前。保存済みの設定値を後継へ寄せる。
LEGACY_EVALUATION_SUITES: dict[str, str] = {
    "request_only": "standard",
    "retrieval_focused": "standard",
    "balanced": "standard",
    "ragas_like": "standard",
    "strict_ci": "strict",
}


@dataclass(frozen=True)
class EvaluationSpec:
    name: str
    origin: str
    recommended_for: tuple[str, ...]
    thresholds: dict[str, float]


EVALUATION_SPECS: dict[str, EvaluationSpec] = {
    "standard": EvaluationSpec(
        "standard",
        "general_rag",
        ("general", "nightly"),
        {
            "required_evidence_recall": 0.9,
            "mrr": 0.6,
            "answer_correctness": 0.9,
            "requirement_coverage": 0.9,
            "claim_support_rate": 0.95,
            "response_decision_accuracy": 0.9,
        },
    ),
    "strict": EvaluationSpec(
        "strict",
        "release_gate",
        ("release", "regression"),
        {
            "required_evidence_recall": 1.0,
            "mrr": 0.8,
            "answer_correctness": 1.0,
            "requirement_coverage": 1.0,
            "claim_support_rate": 1.0,
            "response_decision_accuracy": 1.0,
        },
    ),
}


@dataclass(frozen=True)
class EvaluationResolved:
    suite: str
    thresholds: dict[str, float]


def normalize_evaluation_suite(value: object) -> str:
    """基準の名前を正規化する。旧プリセットは後継へ、未知の名前は既定(標準)へ寄せる。"""
    normalized = str(value).strip().casefold()
    normalized = LEGACY_EVALUATION_SUITES.get(normalized, normalized)
    return normalized if normalized in EVALUATION_SPECS else DEFAULT_EVALUATION_SUITE


def resolve_evaluation(suite: object) -> EvaluationResolved:
    """基準から CI gate 用の閾値 dict を解決する。"""
    name = normalize_evaluation_suite(suite)
    return EvaluationResolved(suite=name, thresholds=dict(EVALUATION_SPECS[name].thresholds))
