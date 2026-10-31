"""Report: ケースの採点の集計（六指標・内訳・分母・合否）と、評価の結果の比較（#3240）。

集計の意味は製品の評価と同じ（成功したケースのうち測れたものだけの平均。失敗したケースは指標の
分母に入れず、1 つでもあれば合格にしない）。suite は加えて、指標ごとに測れた / 対象外 / 評価不能の
数と評価不能の理由（`denominators`）を出し、閾値を付けた指標に評価不能のケースがあれば合格にしない
（`passed=null`）。比較（`summarize_result` / `summary_markdown`）は suite の結果（RAG の Target と
業務 Agent の Target を同じ Evaluator で採点したもの）を並べる（#3417）。
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from pr_evaluation_scoring import (
    DECISIONS,
    MetricAggregate,
    count_reasons,
    overall_passed,
    rounded_mean,
    threshold_failures,
)
from pydantic import BaseModel, Field

from pr_suite_evaluation.dataset import METRIC_NAMES
from pr_suite_evaluation.evaluators import CaseScore

UNCATEGORIZED = "uncategorized"
UNASSIGNED_SPLIT = "unassigned"
UNSPECIFIED_REASONING = "unspecified"


class GroupSummary(BaseModel):
    case_count: int
    error_count: int = 0
    metrics: dict[str, float | None] = Field(default_factory=dict)
    metric_case_counts: dict[str, int] = Field(default_factory=dict)


class SplitSummary(GroupSummary):
    failure_reason_counts: dict[str, int] = Field(default_factory=dict)
    reasoning_type_breakdown: dict[str, GroupSummary] = Field(default_factory=dict)
    hops_breakdown: dict[str, GroupSummary] = Field(default_factory=dict)


class ThresholdFailureItem(BaseModel):
    metric: str
    actual: float
    threshold: float


class MetricDenominator(BaseModel):
    """1 つの指標の分母（成功したケースの内訳）。"""

    scored: int = 0
    not_applicable: int = 0
    unevaluable: int = 0
    unevaluable_reasons: dict[str, int] = Field(default_factory=dict)


class Denominators(BaseModel):
    """ケース・失敗・往復の終わり方・指標ごとの分母。"""

    case_count: int
    error_count: int
    # ケースの最後の往復の状態ごとの数（completed / confirmation / failed / timeout / … ）。
    final_statuses: dict[str, int] = Field(default_factory=dict)
    # 失敗の種類ごとのケースの数（error_type）。
    error_types: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, MetricDenominator] = Field(default_factory=dict)


class Report(BaseModel):
    """評価の結果（製品の評価の結果と同じ名前の項目と、suite の分母）。"""

    metrics_version: str
    case_count: int
    error_count: int = 0
    required_evidence_recall: float | None = None
    mrr: float | None = None
    answer_correctness: float | None = None
    requirement_coverage: float | None = None
    claim_support_rate: float | None = None
    response_decision_accuracy: float | None = None
    metric_case_counts: dict[str, int] = Field(default_factory=dict)
    passed: bool | None = None
    threshold_failures: list[ThresholdFailureItem] = Field(default_factory=list)
    failure_reason_counts: dict[str, int] = Field(default_factory=dict)
    category_breakdown: dict[str, GroupSummary] = Field(default_factory=dict)
    split_breakdown: dict[str, SplitSummary] = Field(default_factory=dict)
    reasoning_type_breakdown: dict[str, GroupSummary] = Field(default_factory=dict)
    hops_breakdown: dict[str, GroupSummary] = Field(default_factory=dict)
    decision_breakdown: dict[str, GroupSummary] = Field(default_factory=dict)
    denominators: Denominators
    case_results: list[CaseScore] = Field(default_factory=list)


def _aggregate(results: Iterable[CaseScore]) -> MetricAggregate:
    aggregate = MetricAggregate(names=METRIC_NAMES)
    for result in results:
        if result.status == "success":
            aggregate.add_all({name: getattr(result, name) for name in METRIC_NAMES})
    return aggregate


def _group(results: Sequence[CaseScore]) -> GroupSummary:
    aggregate = _aggregate(results)
    return GroupSummary(
        case_count=len(results),
        error_count=sum(1 for item in results if item.status != "success"),
        metrics=aggregate.means(),
        metric_case_counts=aggregate.counts(),
    )


def _grouped(
    results: Sequence[CaseScore], key: Callable[[CaseScore], str]
) -> dict[str, list[CaseScore]]:
    grouped: dict[str, list[CaseScore]] = {}
    for result in results:
        grouped.setdefault(key(result), []).append(result)
    return grouped


def category_breakdown(results: Sequence[CaseScore]) -> dict[str, GroupSummary]:
    if not any(item.category for item in results):
        return {}
    groups = _grouped(results, lambda item: item.category or UNCATEGORIZED)
    return {name: _group(members) for name, members in groups.items()}


def reasoning_type_breakdown(results: Sequence[CaseScore]) -> dict[str, GroupSummary]:
    if not any(item.reasoning_type for item in results):
        return {}
    groups = _grouped(results, lambda item: item.reasoning_type or UNSPECIFIED_REASONING)
    return {name: _group(members) for name, members in groups.items()}


def hops_breakdown(results: Sequence[CaseScore]) -> dict[str, GroupSummary]:
    if not any(item.hops is not None for item in results):
        return {}
    ordered = sorted(results, key=lambda item: (item.hops is None, item.hops or 0))
    groups = _grouped(
        ordered, lambda item: str(item.hops) if item.hops is not None else UNSPECIFIED_REASONING
    )
    return {name: _group(members) for name, members in groups.items()}


def split_breakdown(results: Sequence[CaseScore]) -> dict[str, SplitSummary]:
    if not any(item.split for item in results):
        return {}
    breakdown: dict[str, SplitSummary] = {}
    for name, members in _grouped(results, lambda item: item.split or UNASSIGNED_SPLIT).items():
        reasons: dict[str, int] = {}
        for item in members:
            count_reasons(reasons, item.failure_reasons)
        breakdown[name] = SplitSummary(
            **_group(members).model_dump(),
            failure_reason_counts=reasons,
            reasoning_type_breakdown=reasoning_type_breakdown(members),
            hops_breakdown=hops_breakdown(members),
        )
    return breakdown


def decision_breakdown(results: Sequence[CaseScore]) -> dict[str, GroupSummary]:
    """期待する回答・確認・保留ごとの、判断の正しさ（測れたケースだけ）。"""
    breakdown: dict[str, GroupSummary] = {}
    for decision in DECISIONS:
        members = [item for item in results if decision in item.expected_decisions]
        if not members:
            continue
        values = [
            item.response_decision_accuracy
            for item in members
            if item.response_decision_accuracy is not None
        ]
        breakdown[decision] = GroupSummary(
            case_count=len(members),
            error_count=sum(1 for item in members if item.status == "error"),
            metrics={"response_decision_accuracy": rounded_mean(values)},
            metric_case_counts={"response_decision_accuracy": len(values)},
        )
    return breakdown


def denominators(
    results: Sequence[CaseScore], final_statuses: Mapping[str, int] | None = None
) -> Denominators:
    metrics = {name: MetricDenominator() for name in METRIC_NAMES}
    error_types: dict[str, int] = {}
    for result in results:
        if result.status != "success":
            count_reasons(error_types, [result.error_type or "unknown"])
            continue
        for name, status in result.metric_status.items():
            item = metrics[name]
            if status.status == "scored":
                item.scored += 1
            elif status.status == "not_applicable":
                item.not_applicable += 1
            else:
                item.unevaluable += 1
                count_reasons(item.unevaluable_reasons, [status.reason or "unknown"])
    return Denominators(
        case_count=len(results),
        error_count=sum(1 for item in results if item.status != "success"),
        final_statuses=dict(sorted((final_statuses or {}).items())),
        error_types=dict(sorted(error_types.items())),
        metrics=metrics,
    )


def build_report(
    results: Sequence[CaseScore],
    *,
    metrics_version: str,
    thresholds: Mapping[str, float],
    final_statuses: Mapping[str, int] | None = None,
) -> Report:
    """ケースの採点を集計する。"""
    items = list(results)
    aggregate = _aggregate(items)
    values = aggregate.means()
    reasons: dict[str, int] = {}
    for item in items:
        count_reasons(reasons, item.failure_reasons)
    error_count = sum(1 for item in items if item.status != "success")
    failures = threshold_failures(thresholds, values)
    passed = overall_passed(
        failures=failures,
        error_count=error_count,
        incomplete_count=0,
        measured_counts=aggregate.counts(),
        thresholds=thresholds,
        values=values,
    )
    counts = denominators(items, final_statuses)
    if passed is True and any(counts.metrics[name].unevaluable for name in thresholds):
        # 閾値を付けた指標に測れなかったケースがあれば、測れたケースだけで合格にしない。
        passed = None
    return Report(
        metrics_version=metrics_version,
        case_count=len(items),
        error_count=error_count,
        metric_case_counts=aggregate.counts(),
        passed=passed,
        threshold_failures=[
            ThresholdFailureItem(metric=item.metric, actual=item.actual, threshold=item.threshold)
            for item in failures
        ],
        failure_reason_counts=reasons,
        category_breakdown=category_breakdown(items),
        split_breakdown=split_breakdown(items),
        reasoning_type_breakdown=reasoning_type_breakdown(items),
        hops_breakdown=hops_breakdown(items),
        decision_breakdown=decision_breakdown(items),
        denominators=counts,
        case_results=items,
        **values,
    )


# ---- 比較の要約 ---------------------------------------------------------------------------

SUMMARY_METRICS: tuple[tuple[str, str], ...] = (
    ("required_evidence_recall", "必要な根拠の取得率"),
    ("mrr", "正解文書の順位（MRR）"),
    ("answer_correctness", "回答の正確性"),
    ("requirement_coverage", "回答の網羅性"),
    ("claim_support_rate", "根拠への忠実性"),
    ("response_decision_accuracy", "回答・確認・保留の判断の正しさ"),
)
# 勧めてはいけない操作を回答が勧めた、という失敗の理由（製品の評価の失敗の理由の語彙）。
FORBIDDEN_ACTION_REASON = "forbidden_action"


def _records(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _breakdown_summary(breakdown: object) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    if not isinstance(breakdown, Mapping):
        return summary
    for name, item in breakdown.items():
        if not isinstance(item, Mapping):
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
        summary[str(name)] = {
            "case_count": item.get("case_count"),
            "error_count": item.get("error_count"),
            **{metric: (metrics or {}).get(metric) for metric, _ in SUMMARY_METRICS},
        }
    return summary


def _median_seconds(values: Sequence[float]) -> float | None:
    return round(statistics.median(values) / 1000, 1) if values else None


def summarize_result(data: Mapping[str, Any]) -> dict[str, Any]:
    """suite の結果の report（製品の評価の結果と同じ名前の項目）の比較用の要約。"""
    cases = _records(data.get("case_results"))
    succeeded = [case for case in cases if case.get("status", "success") == "success"]
    # 危険な回答のケースは、失敗の理由に勧めてはいけない操作が付いた成功のケース。六指標の採点は
    # この理由を付けないため、該当が無ければ 0 ではなく測れない（None）とする。
    dangerous = [
        case.get("case_id")
        for case in succeeded
        if FORBIDDEN_ACTION_REASON in (case.get("failure_reasons") or [])
    ]
    summary: dict[str, Any] = {
        "case_count": data.get("case_count"),
        "error_count": data.get("error_count"),
        **{metric: data.get(metric) for metric, _ in SUMMARY_METRICS},
        "dangerous_case_count": len(dangerous) if dangerous else None,
        "dangerous_case_ids": dangerous,
        "failed_case_ids": [
            case.get("case_id") for case in cases if case.get("status", "success") != "success"
        ],
        "median_elapsed_seconds": _median_seconds(
            [float(case["elapsed_ms"]) for case in succeeded if case.get("elapsed_ms") is not None]
        ),
        "splits": {},
        "categories": {},
        "reasoning_types": _breakdown_summary(data.get("reasoning_type_breakdown")),
        "hops": _breakdown_summary(data.get("hops_breakdown")),
    }
    splits = data.get("split_breakdown")
    if isinstance(splits, Mapping):
        for split, item in splits.items():
            if not isinstance(item, Mapping):
                continue
            metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
            summary["splits"][split] = {
                "case_count": item.get("case_count"),
                "error_count": item.get("error_count"),
                **{metric: (metrics or {}).get(metric) for metric, _ in SUMMARY_METRICS},
                "reasoning_types": _breakdown_summary(item.get("reasoning_type_breakdown")),
            }
    categories = data.get("category_breakdown")
    if isinstance(categories, Mapping):
        for category, item in categories.items():
            if isinstance(item, Mapping):
                metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
                summary["categories"][category] = {
                    "case_count": item.get("case_count"),
                    "handling_correct_rate": (metrics or {}).get("response_decision_accuracy"),
                }
    denominator = data.get("denominators")
    if isinstance(denominator, Mapping) and isinstance(denominator.get("metrics"), Mapping):
        summary["unevaluable"] = {
            str(name): item.get("unevaluable")
            for name, item in denominator["metrics"].items()
            if isinstance(item, Mapping)
        }
    return summary


def _format(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _ordered_names(groups: Iterable[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for group in groups:
        for name in group:
            if name not in names:
                names.append(name)
    return names


def summary_markdown(summaries: Mapping[str, Mapping[str, Any]]) -> str:
    """要約を Markdown の表にする（docs の記録用）。"""
    labels = list(summaries)
    rows = ["| 指標 | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]

    def row(name: str, values: Sequence[object]) -> None:
        rows.append(f"| {name} | " + " | ".join(_format(value) for value in values) + " |")

    if any("target_kind" in summaries[label] for label in labels):
        row("対象", [summaries[label].get("target_kind") for label in labels])
        row("採点の版", [summaries[label].get("metrics_version") for label in labels])
    for metric, name in SUMMARY_METRICS:
        row(name, [summaries[label].get(metric) for label in labels])
    if any("unevaluable" in summaries[label] for label in labels):
        for metric, name in SUMMARY_METRICS:
            values = [summaries[label].get("unevaluable", {}).get(metric) for label in labels]
            if any(values):
                row(f"評価不能のケース: {name}", values)
    judged = [summaries[label].get("judge") or {} for label in labels]
    for aspect_id in _ordered_names(judged):
        rates: list[object] = []
        for item in judged:
            aspect = item.get(aspect_id)
            if not aspect or aspect.get("pass_rate") is None:
                rates.append(None)
                continue
            mark = "" if aspect.get("calibrated") else "（参考）"
            rates.append(f"{aspect['pass_rate']:.2f}{mark}")
        row(f"LLM の判定: {aspect_id} の合格の率", rates)
    row("危険な回答のケース", [summaries[label].get("dangerous_case_count") for label in labels])
    row("失敗したケース", [summaries[label].get("error_count") for label in labels])
    row(
        "1 問の時間の中央値（秒）",
        [summaries[label].get("median_elapsed_seconds") for label in labels],
    )
    splits = sorted(
        {split for summary in summaries.values() for split in summary.get("splits", {})}
    )
    for split in splits:
        for metric, name in SUMMARY_METRICS:
            row(
                f"{split}: {name}",
                [summaries[label].get("splits", {}).get(split, {}).get(metric) for label in labels],
            )
    for key, suffix in (("reasoning_types", ""), ("hops", " 段")):
        groups = {label: summaries[label].get(key, {}) for label in labels}
        for name in _ordered_names(groups.values()):
            for metric, metric_name in SUMMARY_METRICS:
                row(
                    f"{name}{suffix}: {metric_name}",
                    [groups[label].get(name, {}).get(metric) for label in labels],
                )
    for split in splits:
        groups = {
            label: summaries[label].get("splits", {}).get(split, {}).get("reasoning_types", {})
            for label in labels
        }
        for name in _ordered_names(groups.values()):
            row(
                f"{split} / {name}: 必要な根拠の取得率",
                [groups[label].get(name, {}).get("required_evidence_recall") for label in labels],
            )
    categories = sorted(
        {category for summary in summaries.values() for category in summary.get("categories", {})}
    )
    for category in categories:
        row(
            f"{category}: 回答・確認・保留の判断の正しさ",
            [
                summaries[label]
                .get("categories", {})
                .get(category, {})
                .get("handling_correct_rate")
                for label in labels
            ],
        )
    return "\n".join(rows) + "\n"
