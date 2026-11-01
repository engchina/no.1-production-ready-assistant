"""2 つの RAG の評価の、同じケースの対応のある比較（#60）。

平均の数ポイントの差だけでは、改善とケースの少なさ・生成の揺れを区別できない。同じ評価セットの
同じ ``case_id`` を対にし、指標ごとに改善・悪化・同値の件数と差（target − base）の平均の区間を出す。

- 対にするのは両方で測れたケースだけ。片方でもエラーのケース（``error_cases``）・測れなかった
  ケース（``missing_cases``）は平均から除き、改善にも悪化にも数えない（エラーを落として改善扱いにしない）。
- 二値の指標（全ての値が 0 / 1。応答の判断の正解など）は、改善・悪化の件数の正確な符号検定
  （McNemar の正確検定）で、ほかの指標（MRR・回答の正確性など）は seed を固定した配対 bootstrap の
  区間で、差の向きを判定する。どちらも区間（bootstrap）を出す。
- 対の数が ``min_cases`` に満たなければ小標本として判定しない（``insufficient_cases``）。
- 評価セット（manifest の ``evaluation_set.sha256``）・ケースの id が違う、実行中に条件が変わった
  （``comparable: false``）結果は比較不能にし、指標の差を出さない。条件の差（設定・プロファイル
  など）は、比べたい違いとして ``condition_differences`` に出す。
- 既存の絶対の閾値・失敗したケースによる不合格（``target_gate_failed``）は、統計の判定とは別に出す。
  教材の近似式・固定の差の閾値は使わない。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import fsum
from typing import Any

from pr_evaluation_scoring import exact_sign_test, paired_bootstrap_interval

from app.rag.evaluation_manifest import comparison_differences
from app.schemas.evaluation import (
    EVALUATION_METRIC_NAMES,
    EvaluationCaseResult,
    EvaluationMetrics,
    EvaluationPairedCase,
    EvaluationPairedComparison,
    EvaluationPairedMetric,
    PairedVerdict,
)

DEFAULT_SEED = 20260
DEFAULT_RESAMPLES = 2000
DEFAULT_CONFIDENCE = 0.95
# これより対が少なければ小標本として判定しない（bootstrap の区間が狭く出やすい）。
DEFAULT_MIN_CASES = 10


def compare_paired(
    base: EvaluationMetrics,
    target: EvaluationMetrics,
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    min_cases: int = DEFAULT_MIN_CASES,
) -> EvaluationPairedComparison:
    """base と target の評価の、同じケースの対応のある比較。"""
    base_cases = {result.case_id: result for result in base.case_results}
    target_cases = {result.case_id: result for result in target.case_results}
    reasons, warnings, differences = _comparability(base, target)
    base_only = sorted(base_cases.keys() - target_cases.keys())
    target_only = sorted(target_cases.keys() - base_cases.keys())
    if base_only or target_only:
        reasons.append("case_ids_differ")
    common = [case_id for case_id in base_cases if case_id in target_cases]
    comparison = EvaluationPairedComparison(
        status="not_comparable" if reasons else "compared",
        reasons=reasons,
        warnings=warnings,
        paired_case_count=len(common),
        base_only_case_ids=base_only,
        target_only_case_ids=target_only,
        condition_differences=differences,
        seed=seed,
        resamples=resamples,
        confidence=confidence,
        min_cases=min_cases,
        base_gate_failed=_gate_failed(base),
        target_gate_failed=_gate_failed(target),
        target_threshold_failures=list(target.threshold_failures),
    )
    if reasons:
        return comparison
    pairs = [(base_cases[case_id], target_cases[case_id]) for case_id in common]
    metrics = [
        _metric(
            name, pairs, seed=seed, resamples=resamples, confidence=confidence, min_cases=min_cases
        )
        for name in EVALUATION_METRIC_NAMES
    ]
    cases = [
        EvaluationPairedCase(
            case_id=left.case_id,
            base_status=left.status,
            target_status=right.status,
            differences={name: _difference(left, right, name) for name in EVALUATION_METRIC_NAMES},
        )
        for left, right in pairs
    ]
    return comparison.model_copy(update={"metrics": metrics, "cases": cases})


def _comparability(
    base: EvaluationMetrics, target: EvaluationMetrics
) -> tuple[list[str], list[str], list[str]]:
    """比較不能の理由・注意・条件の差。manifest の無い（#59 より前の）結果は id で対にする。"""
    reasons: list[str] = []
    warnings: list[str] = []
    left, right = base.run_manifest, target.run_manifest
    if left is None or right is None:
        warnings.append("run_manifest_missing")
        return reasons, warnings, []
    if _set_hash(left) != _set_hash(right):
        reasons.append("evaluation_set_differs")
    for side, manifest in (("base", left), ("target", right)):
        if manifest.get("comparable") is False:
            reasons.append(f"changed_during_run:{side}")
    differences = [
        path
        for path in comparison_differences(left, right)
        if not path.startswith("evaluation_set.")
    ]
    return reasons, warnings, differences


def _set_hash(manifest: Mapping[str, Any]) -> object:
    evaluation_set = manifest.get("evaluation_set")
    return evaluation_set.get("sha256") if isinstance(evaluation_set, Mapping) else None


def _metric(
    name: str,
    pairs: list[tuple[EvaluationCaseResult, EvaluationCaseResult]],
    *,
    seed: int,
    resamples: int,
    confidence: float,
    min_cases: int,
) -> EvaluationPairedMetric:
    values: list[tuple[float, float]] = []
    errors = missing = 0
    for left, right in pairs:
        if left.status == "error" or right.status == "error":
            errors += 1
            continue
        base_value, target_value = getattr(left, name), getattr(right, name)
        if base_value is None or target_value is None:
            missing += 1
            continue
        values.append((float(base_value), float(target_value)))
    binary = bool(values) and all(value in (0.0, 1.0) for pair in values for value in pair)
    differences = [target_value - base_value for base_value, target_value in values]
    improved = sum(1 for value in differences if value > 0)
    regressed = sum(1 for value in differences if value < 0)
    unchanged = len(differences) - improved - regressed
    common: dict[str, Any] = {
        "metric": name,
        "binary": binary,
        "method": "exact_sign_test" if binary else "paired_bootstrap",
        "paired_cases": len(values),
        "missing_cases": missing,
        "error_cases": errors,
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
    }
    if not values:
        return EvaluationPairedMetric(**common, verdict="not_measured")
    n = len(values)
    interval = paired_bootstrap_interval(
        differences, seed=seed, resamples=resamples, confidence=confidence
    )
    p_value = exact_sign_test(improved, regressed) if binary else None
    mean_difference = fsum(differences) / n
    return EvaluationPairedMetric(
        **common,
        base_mean=fsum(value for value, _ in values) / n,
        target_mean=fsum(value for _, value in values) / n,
        mean_difference=mean_difference,
        interval=interval,
        p_value=p_value,
        verdict=_verdict(
            improved=improved,
            regressed=regressed,
            n=n,
            interval=interval,
            p_value=p_value,
            alpha=1.0 - confidence,
            min_cases=min_cases,
        ),
    )


def _verdict(
    *,
    improved: int,
    regressed: int,
    n: int,
    interval: tuple[float, float] | None,
    p_value: float | None,
    alpha: float,
    min_cases: int,
) -> PairedVerdict:
    if improved == 0 and regressed == 0:
        return "unchanged"
    if n < min_cases:
        return "insufficient_cases"
    if p_value is not None:
        if p_value >= alpha:
            return "inconclusive"
        return "improved" if improved > regressed else "regressed"
    if interval is not None and interval[0] > 0:
        return "improved"
    if interval is not None and interval[1] < 0:
        return "regressed"
    return "inconclusive"


def _difference(left: EvaluationCaseResult, right: EvaluationCaseResult, name: str) -> float | None:
    base_value, target_value = getattr(left, name), getattr(right, name)
    if (
        left.status == "error"
        or right.status == "error"
        or base_value is None
        or target_value is None
    ):
        return None
    return float(target_value) - float(base_value)


def _gate_failed(metrics: EvaluationMetrics) -> bool:
    """既存の gate と同じ不合格（合格でない・失敗したケース・閾値を下回った指標）。"""
    return metrics.passed is not True or metrics.error_count > 0 or bool(metrics.threshold_failures)


__all__ = [
    "DEFAULT_MIN_CASES",
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "compare_paired",
]
