"""file-processing trend の非機密 regression gate CLI。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pr_backend_core.logging import configure_cli_logging

from app.rag.file_processing_evaluation import FILE_PROCESSING_THRESHOLD_DIRECTIONS

DEFAULT_ALLOWED_DROP = 0.02
DEFAULT_ALLOWED_INCREASE = 0.02
DEFAULT_LATENCY_INCREASE_RATIO = 0.25
DEFAULT_LATENCY_INCREASE_MS = 10_000.0
PARSER_ADAPTER_CONTRACT_BAD_STATUSES = frozenset(
    {"failed", "fallback", "fixture_missing", "missing", "disabled"}
)
STRICT_ZERO_DROP_METRICS = frozenset(
    {
        "chunk_block_integrity",
        "chunk_contextual_coherence",
        "cross_page_table_continuity_coverage",
        "dependency_context_recall",
        "ingestion_quality_report_completeness",
        "parser_routing_accuracy",
        "parser_warning_taxonomy_coverage",
        "reading_order_consistency",
        "source_kind_coverage",
        "structural_section_coverage",
        "table_qa_accuracy",
        "page_hit_accuracy",
        "bbox_coordinate_validity_coverage",
        "backend_source_kind_coverage",
        "preview_addressability_coverage",
        "table_structure_fidelity",
        "table_cell_lineage_coverage",
        "table_row_tree_fidelity",
        "visual_chunk_metadata_completeness",
        "adapter_contract_coverage",
    }
)


class FileProcessingTrendCliError(RuntimeError):
    """CLI 利用者へ返す安全なエラー。"""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class TrendRegression:
    """trend regression の低機密詳細。"""

    metric: str
    direction: str
    baseline: float
    current: float
    allowed_delta: float
    delta: float
    reason: str


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    configure_cli_logging("rag")
    parser = argparse.ArgumentParser(
        prog="rag-file-processing-trend",
        description=(
            "file-processing trend JSON を baseline と比較し、"
            "parser fallback / 表 QA / page hit / bbox などの退化を検出します。"
        ),
    )
    parser.add_argument("current", type=Path, help="今回の file-processing trend JSON")
    parser.add_argument("--baseline", type=Path, required=True, help="比較元 trend JSON")
    parser.add_argument("--output", type=Path, help="比較結果 JSON の保存先。未指定なら stdout。")
    parser.add_argument(
        "--allowed-drop",
        type=float,
        default=DEFAULT_ALLOWED_DROP,
        help=f"高いほど良い metric の許容低下幅。既定値: {DEFAULT_ALLOWED_DROP}",
    )
    parser.add_argument(
        "--allowed-increase",
        type=float,
        default=DEFAULT_ALLOWED_INCREASE,
        help=f"低いほど良い metric の許容悪化幅。既定値: {DEFAULT_ALLOWED_INCREASE}",
    )
    parser.add_argument(
        "--latency-increase-ratio",
        type=float,
        default=DEFAULT_LATENCY_INCREASE_RATIO,
        help=(f"ingestion_p95_ms の許容増加率。既定値: {DEFAULT_LATENCY_INCREASE_RATIO}"),
    )
    parser.add_argument(
        "--latency-increase-ms",
        type=float,
        default=DEFAULT_LATENCY_INCREASE_MS,
        help=(f"ingestion_p95_ms の許容増加絶対値。既定値: {DEFAULT_LATENCY_INCREASE_MS}"),
    )
    parser.add_argument(
        "--require-promotion-ready",
        action="store_true",
        help="current trend の promotion_ready=false も regression として扱います。",
    )
    args = parser.parse_args(argv)

    try:
        payload = compare_file_processing_trends(
            current=_load_trend(args.current),
            baseline=_load_trend(args.baseline),
            allowed_drop=args.allowed_drop,
            allowed_increase=args.allowed_increase,
            latency_increase_ratio=args.latency_increase_ratio,
            latency_increase_ms=args.latency_increase_ms,
            require_promotion_ready=args.require_promotion_ready,
        )
        _write_payload(payload, args.output)
    except FileProcessingTrendCliError as exc:
        print(f"file-processing trend エラー: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0 if payload["passed"] else 1


def compare_file_processing_trends(
    *,
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    allowed_drop: float = DEFAULT_ALLOWED_DROP,
    allowed_increase: float = DEFAULT_ALLOWED_INCREASE,
    latency_increase_ratio: float = DEFAULT_LATENCY_INCREASE_RATIO,
    latency_increase_ms: float = DEFAULT_LATENCY_INCREASE_MS,
    require_promotion_ready: bool = False,
) -> dict[str, Any]:
    """2 つの trend payload を比較して regression summary を返す。"""
    _validate_tolerances(
        allowed_drop=allowed_drop,
        allowed_increase=allowed_increase,
        latency_increase_ratio=latency_increase_ratio,
        latency_increase_ms=latency_increase_ms,
    )
    current_metrics = _metric_values(current)
    baseline_metrics = _metric_values(baseline)
    regressions: list[TrendRegression] = []
    skipped_metrics: list[str] = []
    compared_metrics: list[str] = []
    missing_metrics = _missing_comparable_metrics(
        current_metrics=current_metrics,
        baseline_metrics=baseline_metrics,
    )
    regressions.extend(_missing_metric_regressions(missing_metrics))
    for metric in sorted(set(current_metrics) & set(baseline_metrics)):
        direction = FILE_PROCESSING_THRESHOLD_DIRECTIONS.get(metric)
        if direction is None and metric != "gate_pass_rate":
            skipped_metrics.append(metric)
            continue
        direction = direction or "min"
        current_value = current_metrics[metric]
        baseline_value = baseline_metrics[metric]
        compared_metrics.append(metric)
        regression = _metric_regression(
            metric=metric,
            direction=direction,
            current=current_value,
            baseline=baseline_value,
            allowed_drop=allowed_drop,
            allowed_increase=allowed_increase,
            latency_increase_ratio=latency_increase_ratio,
            latency_increase_ms=latency_increase_ms,
        )
        if regression is not None:
            regressions.append(regression)
    regressions.extend(_status_regressions(current, baseline, require_promotion_ready))
    payload = {
        "kind": "file_processing_trend_regression",
        "passed": not regressions,
        "current_kind": current.get("kind"),
        "baseline_kind": baseline.get("kind"),
        "current_result_sha256": current.get("result_sha256"),
        "baseline_result_sha256": baseline.get("result_sha256"),
        "result_sha256": _comparison_hash(current=current, baseline=baseline),
        "metrics_compared": compared_metrics,
        "metrics_missing": missing_metrics,
        "metrics_skipped": skipped_metrics,
        "regression_count": len(regressions),
        "regressions": [asdict(regression) for regression in regressions],
    }
    return payload


def _missing_comparable_metrics(
    *,
    current_metrics: Mapping[str, float],
    baseline_metrics: Mapping[str, float],
) -> list[str]:
    return sorted(
        metric
        for metric in set(baseline_metrics) - set(current_metrics)
        if metric in FILE_PROCESSING_THRESHOLD_DIRECTIONS or metric == "gate_pass_rate"
    )


def _missing_metric_regressions(metrics: Sequence[str]) -> list[TrendRegression]:
    return [
        TrendRegression(
            metric=metric,
            direction=FILE_PROCESSING_THRESHOLD_DIRECTIONS.get(metric, "min"),
            baseline=1.0,
            current=0.0,
            allowed_delta=0.0,
            delta=-1.0,
            reason="metric_missing_from_current",
        )
        for metric in metrics
    ]


def _metric_regression(
    *,
    metric: str,
    direction: str,
    current: float,
    baseline: float,
    allowed_drop: float,
    allowed_increase: float,
    latency_increase_ratio: float,
    latency_increase_ms: float,
) -> TrendRegression | None:
    if direction == "min":
        metric_allowed_drop = 0.0 if metric in STRICT_ZERO_DROP_METRICS else allowed_drop
        floor = baseline - metric_allowed_drop
        if current >= floor:
            return None
        return TrendRegression(
            metric=metric,
            direction=direction,
            baseline=baseline,
            current=current,
            allowed_delta=metric_allowed_drop,
            delta=current - baseline,
            reason="metric_decreased",
        )
    if metric == "ingestion_p95_ms":
        allowed_delta = max(latency_increase_ms, baseline * latency_increase_ratio)
    else:
        allowed_delta = allowed_increase
    ceiling = baseline + allowed_delta
    if current <= ceiling:
        return None
    return TrendRegression(
        metric=metric,
        direction=direction,
        baseline=baseline,
        current=current,
        allowed_delta=allowed_delta,
        delta=current - baseline,
        reason="metric_increased",
    )


def _status_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    require_promotion_ready: bool,
) -> list[TrendRegression]:
    regressions: list[TrendRegression] = []
    regressions.extend(_trend_identity_regressions(current, baseline))
    regressions.extend(
        _count_regression(
            metric="failure_count",
            current=_number(current.get("failure_count")),
            baseline=_number(baseline.get("failure_count")),
            reason="failure_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="promotion_blocker_count",
            current=_number(current.get("promotion_blocker_count")),
            baseline=_number(baseline.get("promotion_blocker_count")),
            reason="promotion_blocker_count_increased",
        )
    )
    regressions.extend(_runtime_check_status_regressions(current, baseline))
    regressions.extend(_promotion_blocker_code_regressions(current, baseline))
    regressions.extend(_threshold_status_regressions(current, baseline))
    if bool(baseline.get("passed", False)) and not bool(current.get("passed", False)):
        regressions.append(
            TrendRegression(
                metric="passed",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="passed_regressed",
            )
        )
    if (
        require_promotion_ready
        and bool(baseline.get("promotion_ready", False))
        and not bool(current.get("promotion_ready", False))
    ):
        regressions.append(
            TrendRegression(
                metric="promotion_ready",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="promotion_ready_regressed",
            )
        )
    regressions.extend(_staging_dataset_policy_regressions(current, baseline))
    regressions.extend(_parser_adapter_contract_regressions(current, baseline))
    regressions.extend(_adapter_golden_gate_regressions(current, baseline))
    regressions.extend(_parser_adapter_source_route_regressions(current, baseline))
    regressions.extend(_parser_adapter_scorecard_regressions(current, baseline))
    regressions.extend(_backend_source_kind_matrix_regressions(current, baseline))
    regressions.extend(_object_storage_artifact_chain_regressions(current, baseline))
    regressions.extend(_segment_artifact_reuse_regressions(current, baseline))
    regressions.extend(_table_cell_lineage_regressions(current, baseline))
    regressions.extend(_preview_addressability_regressions(current, baseline))
    regressions.extend(_chunk_template_scorecard_regressions(current, baseline))
    return regressions


def _trend_identity_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """比較対象 trend の種類と実行面積の退化を検出する。"""
    regressions: list[TrendRegression] = []
    baseline_kind = _optional_str(baseline.get("kind"))
    current_kind = _optional_str(current.get("kind"))
    if baseline_kind is not None and current_kind != baseline_kind:
        regressions.append(
            TrendRegression(
                metric="kind",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="trend_kind_changed",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="case_count",
            current=_number(current.get("case_count")),
            baseline=_number(baseline.get("case_count")),
            reason="case_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="gate_count",
            current=_number(current.get("gate_count")),
            baseline=_number(baseline.get("gate_count")),
            reason="gate_count_decreased",
        )
    )
    return regressions


def _runtime_check_status_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """runtime smoke の ok 面縮小と bad status 増加を検出する。"""
    current_counts = _mapping(current.get("runtime_check_status_counts"))
    baseline_counts = _mapping(baseline.get("runtime_check_status_counts"))
    regressions: list[TrendRegression] = []
    regressions.extend(
        _count_decrease_regression(
            metric="runtime_check_count",
            current=_status_total_count(current_counts),
            baseline=_status_total_count(baseline_counts),
            reason="runtime_check_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="runtime_check_status_count:ok",
            current=_status_count(current_counts, "ok"),
            baseline=_status_count(baseline_counts, "ok"),
            reason="runtime_check_ok_status_count_decreased",
        )
    )
    for status in sorted(set(current_counts) | set(baseline_counts)):
        if status == "ok":
            continue
        regressions.extend(
            _count_regression(
                metric=f"runtime_check_status_count:{status}",
                current=_status_count(current_counts, str(status)),
                baseline=_status_count(baseline_counts, str(status)),
                reason="runtime_check_bad_status_count_increased",
            )
        )
    return regressions


def _promotion_blocker_code_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """promotion blocker の code 別増加を検出する。"""
    return _code_count_regressions(
        metric_prefix="promotion_blocker_code_count",
        reason="promotion_blocker_code_count_increased",
        current_counts=_mapping(current.get("promotion_blocker_code_counts")),
        baseline_counts=_mapping(baseline.get("promotion_blocker_code_counts")),
    )


def _threshold_status_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """threshold 結果の passed 面縮小と failed/pending 増加を検出する。"""
    current_counts = _mapping(current.get("threshold_status_counts"))
    baseline_counts = _mapping(baseline.get("threshold_status_counts"))
    regressions: list[TrendRegression] = []
    regressions.extend(
        _count_decrease_regression(
            metric="threshold_result_count",
            current=_status_total_count(current_counts),
            baseline=_status_total_count(baseline_counts),
            reason="threshold_result_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="threshold_status_count:passed",
            current=_status_count(current_counts, "passed"),
            baseline=_status_count(baseline_counts, "passed"),
            reason="threshold_passed_status_count_decreased",
        )
    )
    for status in sorted(set(current_counts) | set(baseline_counts)):
        if status == "passed":
            continue
        reason = (
            "threshold_failed_status_count_increased"
            if status == "failed"
            else "threshold_bad_status_count_increased"
        )
        regressions.extend(
            _count_regression(
                metric=f"threshold_status_count:{status}",
                current=_status_count(current_counts, str(status)),
                baseline=_status_count(baseline_counts, str(status)),
                reason=reason,
            )
        )
    regressions.extend(_threshold_failure_metric_regressions(current, baseline))
    return regressions


def _threshold_failure_metric_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    current_failures = _threshold_failure_metrics(current.get("threshold_failures"))
    baseline_failures = _threshold_failure_metrics(baseline.get("threshold_failures"))
    added_failures = current_failures - baseline_failures
    if not added_failures:
        return []
    return [
        TrendRegression(
            metric="threshold_failure_metric_added_count",
            direction="max",
            baseline=0.0,
            current=float(len(added_failures)),
            allowed_delta=0.0,
            delta=float(len(added_failures)),
            reason="threshold_failure_metrics_added",
        )
    ]


def _parser_adapter_source_route_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """source kind 別 parser routing 証跡の退化を検出する。"""
    baseline_routes = _parser_adapter_routes_by_source(baseline.get("parser_adapter_source_routes"))
    if not baseline_routes:
        return []
    current_routes = _parser_adapter_routes_by_source(current.get("parser_adapter_source_routes"))
    regressions: list[TrendRegression] = []
    removed_source_kinds = set(baseline_routes) - set(current_routes)
    if removed_source_kinds:
        regressions.append(
            TrendRegression(
                metric="parser_adapter_source_route_removed_count",
                direction="max",
                baseline=0.0,
                current=float(len(removed_source_kinds)),
                allowed_delta=0.0,
                delta=float(len(removed_source_kinds)),
                reason="parser_adapter_source_routes_removed",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="parser_adapter_source_route_count",
            current=float(len(current_routes)),
            baseline=float(len(baseline_routes)),
            reason="parser_adapter_source_route_count_decreased",
        )
    )
    for source_kind, baseline_route in sorted(baseline_routes.items()):
        current_route = current_routes.get(source_kind)
        if current_route is None:
            continue
        regressions.extend(
            _parser_adapter_source_route_entry_regressions(
                source_kind=source_kind,
                current_route=current_route,
                baseline_route=baseline_route,
            )
        )
    return regressions


def _parser_adapter_source_route_entry_regressions(
    *,
    source_kind: str,
    current_route: Mapping[str, object],
    baseline_route: Mapping[str, object],
) -> list[TrendRegression]:
    regressions: list[TrendRegression] = []
    baseline_selected = _optional_str(baseline_route.get("selected_backend"))
    current_selected = _optional_str(current_route.get("selected_backend"))
    if baseline_selected is not None and current_selected != baseline_selected:
        regressions.append(
            TrendRegression(
                metric=f"parser_adapter_source_route_selected_backend:{source_kind}",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="parser_adapter_source_route_selected_backend_changed",
            )
        )
    for field_name, reason in (
        ("candidate_order", "parser_adapter_source_route_candidate_order_count_decreased"),
        ("attempted_order", "parser_adapter_source_route_attempted_order_count_decreased"),
        ("active_order", "parser_adapter_source_route_active_order_count_decreased"),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"parser_adapter_source_route_{field_name}:{source_kind}",
                current=_sequence_count(current_route.get(field_name)),
                baseline=_sequence_count(baseline_route.get(field_name)),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("reason_codes", "parser_adapter_source_route_reason_code_count_increased"),
        ("warning_codes", "parser_adapter_source_route_warning_code_count_increased"),
    ):
        regressions.extend(
            _count_regression(
                metric=f"parser_adapter_source_route_{field_name}:{source_kind}",
                current=_sequence_count(current_route.get(field_name)),
                baseline=_sequence_count(baseline_route.get(field_name)),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("reason_codes", "parser_adapter_source_route_reason_codes_added"),
        ("warning_codes", "parser_adapter_source_route_warning_codes_added"),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"parser_adapter_source_route_{field_name}_added_count:{source_kind}",
                current=current_route.get(field_name),
                baseline=baseline_route.get(field_name),
                reason=reason,
            )
        )
    regressions.extend(
        _count_regression(
            metric=f"parser_adapter_source_route_contract_gap_warning_count:{source_kind}",
            current=_route_contract_gap_warning_count(current_route),
            baseline=_route_contract_gap_warning_count(baseline_route),
            reason="parser_adapter_source_route_contract_gap_warning_count_increased",
        )
    )
    baseline_candidates = _string_set(baseline_route.get("candidate_order"))
    current_candidates = _string_set(current_route.get("candidate_order"))
    removed_candidate_count = len(baseline_candidates - current_candidates)
    if baseline_candidates and removed_candidate_count:
        regressions.append(
            TrendRegression(
                metric=f"parser_adapter_source_route_candidate_removed_count:{source_kind}",
                direction="max",
                baseline=0.0,
                current=float(removed_candidate_count),
                allowed_delta=0.0,
                delta=float(removed_candidate_count),
                reason="parser_adapter_source_route_candidates_removed",
            )
        )
    for field_name, metric_name, reason in (
        (
            "attempted_order",
            "parser_adapter_source_route_attempted_removed_count",
            "parser_adapter_source_route_attempted_backends_removed",
        ),
        (
            "active_order",
            "parser_adapter_source_route_active_removed_count",
            "parser_adapter_source_route_active_backends_removed",
        ),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=f"{metric_name}:{source_kind}",
                current=current_route.get(field_name),
                baseline=baseline_route.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _route_contract_gap_warning_count(route: Mapping[str, object]) -> float | None:
    warnings = _string_set(route.get("warning_codes"))
    if not warnings:
        return 0.0
    return float(
        sum(
            1 for warning in warnings if warning.endswith("_adapter_contract_unverified_for_source")
        )
    )


def _parser_adapter_scorecard_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """parser adapter scorecard の推奨・entry 証跡退化を検出する。"""
    current_scorecard = _mapping(current.get("parser_adapter_scorecard"))
    baseline_scorecard = _mapping(baseline.get("parser_adapter_scorecard"))
    if not baseline_scorecard:
        return []
    regressions: list[TrendRegression] = []
    for field_name, reason in (
        ("selected_backend", "parser_adapter_scorecard_selected_backend_changed"),
        ("recommended_backend", "parser_adapter_scorecard_recommended_backend_changed"),
        ("metrics_source", "parser_adapter_scorecard_metrics_source_changed"),
        ("metrics_applied_to", "parser_adapter_scorecard_metrics_applied_to_changed"),
    ):
        baseline_value = _optional_str(baseline_scorecard.get(field_name))
        if baseline_value is None:
            continue
        current_value = _optional_str(current_scorecard.get(field_name))
        if current_value != baseline_value:
            regressions.append(
                TrendRegression(
                    metric=f"parser_adapter_scorecard_{field_name}",
                    direction="min",
                    baseline=1.0,
                    current=0.0,
                    allowed_delta=0.0,
                    delta=-1.0,
                    reason=reason,
                )
            )
    baseline_entries = _parser_adapter_entries_by_backend(baseline_scorecard.get("entries"))
    current_entries = _parser_adapter_entries_by_backend(current_scorecard.get("entries"))
    if baseline_entries:
        removed_backends = set(baseline_entries) - set(current_entries)
        if removed_backends:
            regressions.append(
                TrendRegression(
                    metric="parser_adapter_scorecard_entry_removed_count",
                    direction="max",
                    baseline=0.0,
                    current=float(len(removed_backends)),
                    allowed_delta=0.0,
                    delta=float(len(removed_backends)),
                    reason="parser_adapter_scorecard_entries_removed",
                )
            )
        regressions.extend(
            _count_decrease_regression(
                metric="parser_adapter_scorecard_entry_count",
                current=float(len(current_entries)),
                baseline=float(len(baseline_entries)),
                reason="parser_adapter_scorecard_entry_count_decreased",
            )
        )
    for backend, baseline_entry in sorted(baseline_entries.items()):
        current_entry = current_entries.get(backend)
        if current_entry is None:
            continue
        regressions.extend(
            _parser_adapter_scorecard_entry_regressions(
                backend=backend,
                current_entry=current_entry,
                baseline_entry=baseline_entry,
            )
        )
    return regressions


def _parser_adapter_scorecard_entry_regressions(
    *,
    backend: str,
    current_entry: Mapping[str, object],
    baseline_entry: Mapping[str, object],
) -> list[TrendRegression]:
    regressions: list[TrendRegression] = []
    baseline_status = _optional_str(baseline_entry.get("status"))
    current_status = _optional_str(current_entry.get("status"))
    if baseline_status in {"recommended", "eligible", "available"} and current_status not in {
        "recommended",
        "eligible",
        "available",
    }:
        regressions.append(
            TrendRegression(
                metric=f"parser_adapter_scorecard_status:{backend}",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="parser_adapter_scorecard_status_regressed",
            )
        )
    for field_name, reason in (
        ("recommended", "parser_adapter_scorecard_recommended_flag_regressed"),
        ("executable", "parser_adapter_scorecard_executable_flag_regressed"),
        ("enabled", "parser_adapter_scorecard_enabled_flag_regressed"),
        ("installed", "parser_adapter_scorecard_installed_flag_regressed"),
    ):
        if baseline_entry.get(field_name) is True and current_entry.get(field_name) is not True:
            regressions.append(
                TrendRegression(
                    metric=f"parser_adapter_scorecard_{field_name}:{backend}",
                    direction="min",
                    baseline=1.0,
                    current=0.0,
                    allowed_delta=0.0,
                    delta=-1.0,
                    reason=reason,
                )
            )
    baseline_rank = _number(baseline_entry.get("rank"))
    current_rank = _number(current_entry.get("rank"))
    if baseline_rank is not None and current_rank is not None and current_rank > baseline_rank:
        regressions.append(
            TrendRegression(
                metric=f"parser_adapter_scorecard_rank:{backend}",
                direction="max",
                baseline=baseline_rank,
                current=current_rank,
                allowed_delta=0.0,
                delta=current_rank - baseline_rank,
                reason="parser_adapter_scorecard_rank_regressed",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric=f"parser_adapter_scorecard_score:{backend}",
            current=_number(current_entry.get("score")),
            baseline=_number(baseline_entry.get("score")),
            reason="parser_adapter_scorecard_score_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric=f"parser_adapter_scorecard_metric_count:{backend}",
            current=_number(current_entry.get("metric_count")),
            baseline=_number(baseline_entry.get("metric_count")),
            reason="parser_adapter_scorecard_metric_count_decreased",
        )
    )
    for field_name, reason in (
        ("reason_codes", "parser_adapter_scorecard_reason_code_count_increased"),
        ("warning_codes", "parser_adapter_scorecard_warning_code_count_increased"),
    ):
        regressions.extend(
            _count_regression(
                metric=f"parser_adapter_scorecard_{field_name}:{backend}",
                current=_sequence_count(current_entry.get(field_name)),
                baseline=_sequence_count(baseline_entry.get(field_name)),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("reason_codes", "parser_adapter_scorecard_reason_codes_added"),
        ("warning_codes", "parser_adapter_scorecard_warning_codes_added"),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"parser_adapter_scorecard_{field_name}_added_count:{backend}",
                current=current_entry.get(field_name),
                baseline=baseline_entry.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _chunk_template_scorecard_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """chunk template scorecard の template 別証跡退化を検出する。"""
    current_scorecard = _mapping(current.get("chunk_template_scorecard"))
    baseline_scorecard = _mapping(baseline.get("chunk_template_scorecard"))
    if not baseline_scorecard:
        return []
    regressions: list[TrendRegression] = []
    if (
        baseline_scorecard.get("promotion_blocking") is False
        and current_scorecard.get("promotion_blocking") is True
    ):
        regressions.append(
            TrendRegression(
                metric="chunk_template_scorecard_promotion_blocking",
                direction="max",
                baseline=0.0,
                current=1.0,
                allowed_delta=0.0,
                delta=1.0,
                reason="chunk_template_scorecard_promotion_blocking_regressed",
            )
        )
    baseline_entries = _chunk_template_entries_by_template(baseline_scorecard.get("entries"))
    current_entries = _chunk_template_entries_by_template(current_scorecard.get("entries"))
    baseline_templates = set(baseline_entries) or _string_set(
        baseline_scorecard.get("observed_templates")
    )
    current_templates = set(current_entries) or _string_set(
        current_scorecard.get("observed_templates")
    )
    if baseline_templates and current_templates:
        removed_templates = baseline_templates - current_templates
        if removed_templates:
            regressions.append(
                TrendRegression(
                    metric="chunk_template_scorecard_template_removed_count",
                    direction="max",
                    baseline=0.0,
                    current=float(len(removed_templates)),
                    allowed_delta=0.0,
                    delta=float(len(removed_templates)),
                    reason="chunk_template_scorecard_templates_removed",
                )
            )
    regressions.extend(
        _count_decrease_regression(
            metric="chunk_template_scorecard_template_count",
            current=(float(len(current_templates)) if current_templates else None),
            baseline=(float(len(baseline_templates)) if baseline_templates else None),
            reason="chunk_template_scorecard_template_count_decreased",
        )
    )
    for template in sorted(set(baseline_entries) | set(current_entries)):
        baseline_entry = baseline_entries.get(template)
        current_entry = current_entries.get(template)
        if baseline_entry is None:
            continue
        if current_entry is None:
            regressions.append(
                TrendRegression(
                    metric=f"chunk_template_scorecard_entry:{template}",
                    direction="min",
                    baseline=1.0,
                    current=0.0,
                    allowed_delta=0.0,
                    delta=-1.0,
                    reason="chunk_template_scorecard_entry_removed",
                )
            )
            continue
        regressions.extend(
            _chunk_template_entry_regressions(
                template=template,
                current_entry=current_entry,
                baseline_entry=baseline_entry,
            )
        )
    return regressions


def _chunk_template_entry_regressions(
    *,
    template: str,
    current_entry: Mapping[str, object],
    baseline_entry: Mapping[str, object],
) -> list[TrendRegression]:
    regressions: list[TrendRegression] = []
    baseline_status = _optional_str(baseline_entry.get("status"))
    current_status = _optional_str(current_entry.get("status"))
    if baseline_status in {"recommended", "healthy"} and current_status not in {
        "recommended",
        "healthy",
    }:
        regressions.append(
            TrendRegression(
                metric=f"chunk_template_status:{template}",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="chunk_template_status_regressed",
            )
        )
    if (
        baseline_entry.get("promotion_blocking") is False
        and current_entry.get("promotion_blocking") is True
    ):
        regressions.append(
            TrendRegression(
                metric=f"chunk_template_promotion_blocking:{template}",
                direction="max",
                baseline=0.0,
                current=1.0,
                allowed_delta=0.0,
                delta=1.0,
                reason="chunk_template_promotion_blocking_regressed",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric=f"chunk_template_score:{template}",
            current=_number(current_entry.get("score")),
            baseline=_number(baseline_entry.get("score")),
            reason="chunk_template_score_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric=f"chunk_template_metric_count:{template}",
            current=_number(current_entry.get("metric_count")),
            baseline=_number(baseline_entry.get("metric_count")),
            reason="chunk_template_metric_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric=f"chunk_template_expected_case_count:{template}",
            current=_number(current_entry.get("expected_case_count")),
            baseline=_number(baseline_entry.get("expected_case_count")),
            reason="chunk_template_expected_case_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric=f"chunk_template_measured_case_count:{template}",
            current=_number(current_entry.get("measured_case_count")),
            baseline=_number(baseline_entry.get("measured_case_count")),
            reason="chunk_template_measured_case_count_decreased",
        )
    )
    for field_name, reason in (
        ("covered_source_kinds", "chunk_template_covered_source_kind_count_decreased"),
        ("covered_scenarios", "chunk_template_covered_scenario_count_decreased"),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"chunk_template_{field_name}:{template}",
                current=_sequence_count(current_entry.get(field_name)),
                baseline=_sequence_count(baseline_entry.get(field_name)),
                reason=reason,
            )
        )
    for field_name, metric_name, reason in (
        (
            "covered_source_kinds",
            "chunk_template_covered_source_kind_removed_count",
            "chunk_template_covered_source_kinds_removed",
        ),
        (
            "covered_scenarios",
            "chunk_template_covered_scenario_removed_count",
            "chunk_template_covered_scenarios_removed",
        ),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=f"{metric_name}:{template}",
                current=current_entry.get(field_name),
                baseline=baseline_entry.get(field_name),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("missing_source_kinds", "chunk_template_missing_source_kind_count_increased"),
        ("missing_scenarios", "chunk_template_missing_scenario_count_increased"),
        ("reason_codes", "chunk_template_reason_code_count_increased"),
    ):
        regressions.extend(
            _count_regression(
                metric=f"chunk_template_{field_name}:{template}",
                current=_sequence_count(current_entry.get(field_name)),
                baseline=_sequence_count(baseline_entry.get(field_name)),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("missing_source_kinds", "chunk_template_missing_source_kinds_added"),
        ("missing_scenarios", "chunk_template_missing_scenarios_added"),
        ("reason_codes", "chunk_template_reason_codes_added"),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"chunk_template_{field_name}_added_count:{template}",
                current=current_entry.get(field_name),
                baseline=baseline_entry.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _object_storage_artifact_chain_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """Object Storage extraction artifact chain の退化を検出する。"""
    current_chain = _mapping(current.get("object_storage_artifact_chain"))
    baseline_chain = _mapping(baseline.get("object_storage_artifact_chain"))
    if not baseline_chain:
        return []
    regressions: list[TrendRegression] = []
    if bool(baseline_chain.get("passed")) and current_chain.get("passed") is not True:
        regressions.append(
            TrendRegression(
                metric="object_storage_artifact_chain_passed",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="object_storage_artifact_chain_passed_regressed",
            )
        )
    if (
        baseline_chain.get("roundtrip_check") == "ok"
        and current_chain.get("roundtrip_check") != "ok"
    ):
        regressions.append(
            TrendRegression(
                metric="object_storage_artifact_roundtrip_check",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="object_storage_artifact_roundtrip_check_regressed",
            )
        )
    if (
        baseline_chain.get("roundtrip_object_uri_scheme") == "oci"
        and current_chain.get("roundtrip_object_uri_scheme") != "oci"
    ):
        regressions.append(
            TrendRegression(
                metric="object_storage_artifact_roundtrip_uri_scheme",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="object_storage_artifact_roundtrip_uri_scheme_regressed",
            )
        )
    if (
        baseline_chain.get("audit_payload_redaction_enforced") is True
        and current_chain.get("audit_payload_redaction_enforced") is not True
    ):
        regressions.append(
            TrendRegression(
                metric="object_storage_audit_payload_redaction_enforced",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="object_storage_audit_payload_redaction_regressed",
            )
        )
    for metric, reason in (
        (
            "full_artifact_cached_case_count",
            "object_storage_full_artifact_cached_case_count_decreased",
        ),
        (
            "full_artifact_oci_case_count",
            "object_storage_full_artifact_oci_case_count_decreased",
        ),
        (
            "full_artifact_identity_present_case_count",
            "object_storage_full_artifact_identity_count_decreased",
        ),
        (
            "full_artifact_readable_case_count",
            "object_storage_full_artifact_readable_count_decreased",
        ),
        (
            "full_artifact_identity_verified_case_count",
            "object_storage_full_artifact_identity_verified_count_decreased",
        ),
        (
            "segment_artifact_expected_count",
            "object_storage_segment_artifact_expected_count_decreased",
        ),
        (
            "segment_artifact_oci_uri_count",
            "object_storage_segment_artifact_oci_uri_count_decreased",
        ),
        (
            "segment_artifact_readable_count",
            "object_storage_segment_artifact_readable_count_decreased",
        ),
        (
            "segment_artifact_identity_verified_count",
            "object_storage_segment_artifact_identity_verified_count_decreased",
        ),
        (
            "retry_case_count",
            "object_storage_retry_case_count_decreased",
        ),
        (
            "retained_successful_segment_artifact_count",
            "object_storage_retained_successful_segment_artifact_count_decreased",
        ),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"object_storage_{metric}",
                current=_number(current_chain.get(metric)),
                baseline=_number(baseline_chain.get(metric)),
                reason=reason,
            )
        )
    regressions.extend(
        _count_regression(
            metric="object_storage_artifact_integrity_error_count",
            current=_number(current_chain.get("artifact_integrity_error_count")),
            baseline=_number(baseline_chain.get("artifact_integrity_error_count")),
            reason="object_storage_artifact_integrity_error_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="object_storage_segment_cache_miss_count",
            current=_number(current_chain.get("segment_cache_miss_count")),
            baseline=_number(baseline_chain.get("segment_cache_miss_count")),
            reason="object_storage_segment_cache_miss_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="object_storage_segment_artifact_non_oci_uri_count",
            current=_number(current_chain.get("segment_artifact_non_oci_uri_count")),
            baseline=_number(baseline_chain.get("segment_artifact_non_oci_uri_count")),
            reason="object_storage_segment_artifact_non_oci_uri_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="object_storage_rewritten_successful_segment_artifact_count",
            current=_number(current_chain.get("rewritten_successful_segment_artifact_count")),
            baseline=_number(baseline_chain.get("rewritten_successful_segment_artifact_count")),
            reason="object_storage_rewritten_successful_segment_artifact_count_increased",
        )
    )
    for field_name, reason in (
        ("full_artifact_cached_case_refs", "object_storage_full_artifact_case_refs_removed"),
        (
            "full_artifact_identity_verified_case_refs",
            "object_storage_full_artifact_identity_case_refs_removed",
        ),
        ("retry_case_refs", "object_storage_retry_case_refs_removed"),
        (
            "retained_successful_segment_artifact_case_refs",
            "object_storage_retained_segment_artifact_case_refs_removed",
        ),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=f"object_storage_{field_name}_removed_count",
                current=current_chain.get(field_name),
                baseline=baseline_chain.get(field_name),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("segment_cache_miss_case_refs", "object_storage_segment_cache_miss_case_refs_added"),
        (
            "artifact_integrity_error_case_refs",
            "object_storage_artifact_integrity_error_case_refs_added",
        ),
        (
            "successful_segment_rewrite_case_refs",
            "object_storage_successful_segment_rewrite_case_refs_added",
        ),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"object_storage_{field_name}_added_count",
                current=current_chain.get(field_name),
                baseline=baseline_chain.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _backend_source_kind_matrix_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """backend/source kind coverage matrix の証跡退化を検出する。"""
    current_matrix = _mapping(current.get("backend_source_kind_matrix"))
    baseline_matrix = _mapping(baseline.get("backend_source_kind_matrix"))
    if not baseline_matrix:
        return []
    regressions: list[TrendRegression] = []
    for key, metric, reason in (
        (
            "required_source_kinds",
            "backend_source_kind_matrix_required_source_kind_count",
            "backend_source_kind_matrix_required_source_kind_count_decreased",
        ),
        (
            "covered_source_kinds",
            "backend_source_kind_matrix_covered_source_kind_count",
            "backend_source_kind_matrix_covered_source_kind_count_decreased",
        ),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=metric,
                current=_sequence_count(current_matrix.get(key)),
                baseline=_sequence_count(baseline_matrix.get(key)),
                reason=reason,
            )
        )
    regressions.extend(
        _count_regression(
            metric="backend_source_kind_matrix_missing_source_kind_count",
            current=_sequence_count(current_matrix.get("missing_source_kinds")),
            baseline=_sequence_count(baseline_matrix.get("missing_source_kinds")),
            reason="backend_source_kind_matrix_missing_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="backend_source_kind_matrix_missing_source_kind_added_count",
            current=current_matrix.get("missing_source_kinds"),
            baseline=baseline_matrix.get("missing_source_kinds"),
            reason="backend_source_kind_matrix_missing_source_kinds_added",
        )
    )
    for key, metric, reason in (
        (
            "required_source_kinds",
            "backend_source_kind_matrix_required_source_kind_removed_count",
            "backend_source_kind_matrix_required_source_kinds_removed",
        ),
        (
            "covered_source_kinds",
            "backend_source_kind_matrix_covered_source_kind_removed_count",
            "backend_source_kind_matrix_covered_source_kinds_removed",
        ),
    ):
        baseline_values = _string_set(baseline_matrix.get(key))
        if not baseline_values:
            continue
        current_values = _string_set(current_matrix.get(key))
        removed_count = len(baseline_values - current_values)
        if removed_count:
            regressions.append(
                TrendRegression(
                    metric=metric,
                    direction="max",
                    baseline=0.0,
                    current=float(removed_count),
                    allowed_delta=0.0,
                    delta=float(removed_count),
                    reason=reason,
                )
            )
    baseline_pairs = _backend_source_pairs(baseline_matrix.get("backend_source_kinds"))
    current_pairs = _backend_source_pairs(current_matrix.get("backend_source_kinds"))
    if baseline_pairs:
        removed_pair_count = len(baseline_pairs - current_pairs)
        if removed_pair_count:
            regressions.append(
                TrendRegression(
                    metric="backend_source_kind_matrix_backend_source_pair_removed_count",
                    direction="max",
                    baseline=0.0,
                    current=float(removed_pair_count),
                    allowed_delta=0.0,
                    delta=float(removed_pair_count),
                    reason="backend_source_kind_matrix_backend_source_pairs_removed",
                )
            )
        regressions.extend(
            _count_decrease_regression(
                metric="backend_source_kind_matrix_backend_source_pair_count",
                current=float(len(current_pairs)),
                baseline=float(len(baseline_pairs)),
                reason="backend_source_kind_matrix_backend_source_pair_count_decreased",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="backend_source_kind_matrix_value",
            current=_number(current_matrix.get("value")),
            baseline=_number(baseline_matrix.get("value")),
            reason="backend_source_kind_matrix_value_decreased",
        )
    )
    return regressions


def _segment_artifact_reuse_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """segment checkpoint retry/reuse の実測証跡退化を検出する。"""
    current_reuse = _mapping(current.get("segment_artifact_reuse"))
    baseline_reuse = _mapping(baseline.get("segment_artifact_reuse"))
    if not baseline_reuse:
        return []
    regressions: list[TrendRegression] = []
    for metric, reason in (
        ("case_count", "segment_artifact_reuse_case_count_decreased"),
        ("retry_case_count", "segment_artifact_reuse_retry_case_count_decreased"),
        (
            "initial_failed_segment_count",
            "segment_artifact_reuse_initial_failed_segment_count_decreased",
        ),
        (
            "initial_successful_segment_artifact_count",
            "segment_artifact_reuse_initial_successful_artifact_count_decreased",
        ),
        (
            "retained_successful_segment_artifact_count",
            "segment_artifact_reuse_retained_successful_artifact_count_decreased",
        ),
        (
            "failed_segment_retried_count",
            "segment_artifact_reuse_failed_segment_retried_count_decreased",
        ),
        (
            "failed_segment_succeeded_count",
            "segment_artifact_reuse_failed_segment_succeeded_count_decreased",
        ),
        (
            "full_artifact_cached_case_count",
            "segment_artifact_reuse_full_artifact_cached_case_count_decreased",
        ),
        (
            "full_artifact_identity_present_case_count",
            "segment_artifact_reuse_full_artifact_identity_count_decreased",
        ),
        (
            "segment_artifact_expected_count",
            "segment_artifact_reuse_segment_artifact_expected_count_decreased",
        ),
        (
            "segment_artifact_readable_count",
            "segment_artifact_reuse_segment_artifact_readable_count_decreased",
        ),
        (
            "segment_artifact_identity_verified_count",
            "segment_artifact_reuse_segment_artifact_identity_count_decreased",
        ),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"segment_artifact_reuse_{metric}",
                current=_number(current_reuse.get(metric)),
                baseline=_number(baseline_reuse.get(metric)),
                reason=reason,
            )
        )
    for metric, reason in (
        (
            "rewritten_successful_segment_artifact_count",
            "segment_artifact_reuse_rewritten_successful_artifact_count_increased",
        ),
        (
            "reprocessed_successful_segment_count",
            "segment_artifact_reuse_reprocessed_successful_segment_count_increased",
        ),
        (
            "segment_cache_miss_count",
            "segment_artifact_reuse_cache_miss_count_increased",
        ),
        (
            "segment_cache_miss_case_count",
            "segment_artifact_reuse_cache_miss_case_count_increased",
        ),
        (
            "segment_artifact_non_oci_uri_count",
            "segment_artifact_reuse_non_oci_uri_count_increased",
        ),
        (
            "artifact_integrity_error_count",
            "segment_artifact_reuse_integrity_error_count_increased",
        ),
    ):
        regressions.extend(
            _count_regression(
                metric=f"segment_artifact_reuse_{metric}",
                current=_number(current_reuse.get(metric)),
                baseline=_number(baseline_reuse.get(metric)),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("case_refs", "segment_artifact_reuse_case_refs_removed"),
        ("retry_case_refs", "segment_artifact_reuse_retry_case_refs_removed"),
        (
            "full_artifact_cached_case_refs",
            "segment_artifact_reuse_full_artifact_case_refs_removed",
        ),
        (
            "full_artifact_identity_verified_case_refs",
            "segment_artifact_reuse_full_artifact_identity_case_refs_removed",
        ),
        (
            "retained_successful_segment_artifact_case_refs",
            "segment_artifact_reuse_retained_segment_artifact_case_refs_removed",
        ),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=f"segment_artifact_reuse_{field_name}_removed_count",
                current=current_reuse.get(field_name),
                baseline=baseline_reuse.get(field_name),
                reason=reason,
            )
        )
    for field_name, reason in (
        (
            "successful_segment_rewrite_case_refs",
            "segment_artifact_reuse_successful_segment_rewrite_case_refs_added",
        ),
        (
            "successful_segment_reprocess_case_refs",
            "segment_artifact_reuse_successful_segment_reprocess_case_refs_added",
        ),
        ("segment_cache_miss_case_refs", "segment_artifact_reuse_cache_miss_case_refs_added"),
        (
            "artifact_integrity_error_case_refs",
            "segment_artifact_reuse_integrity_error_case_refs_added",
        ),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"segment_artifact_reuse_{field_name}_added_count",
                current=current_reuse.get(field_name),
                baseline=baseline_reuse.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _table_cell_lineage_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """table cell lineage の実測件数退化を検出する。"""
    current_lineage = _mapping(current.get("table_cell_lineage"))
    baseline_lineage = _mapping(baseline.get("table_cell_lineage"))
    if not baseline_lineage:
        return []
    regressions: list[TrendRegression] = []
    for metric, reason in (
        (
            "expected_case_count",
            "table_cell_lineage_expected_case_count_decreased",
        ),
        (
            "expected_ref_count",
            "table_cell_lineage_expected_ref_count_decreased",
        ),
        (
            "resolved_ref_count",
            "table_cell_lineage_resolved_ref_count_decreased",
        ),
        (
            "covered_ref_count",
            "table_cell_lineage_covered_ref_count_decreased",
        ),
        (
            "lineage_ref_count",
            "table_cell_lineage_lineage_ref_count_decreased",
        ),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"table_cell_lineage_{metric}",
                current=_number(current_lineage.get(metric)),
                baseline=_number(baseline_lineage.get(metric)),
                reason=reason,
            )
        )
    for metric, reason in (
        (
            "unresolved_ref_count",
            "table_cell_lineage_unresolved_ref_count_increased",
        ),
        (
            "uncovered_ref_count",
            "table_cell_lineage_uncovered_ref_count_increased",
        ),
    ):
        regressions.extend(
            _count_regression(
                metric=f"table_cell_lineage_{metric}",
                current=_number(current_lineage.get(metric)),
                baseline=_number(baseline_lineage.get(metric)),
                reason=reason,
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="table_cell_lineage_evidence_coverage",
            current=_number(current_lineage.get("coverage")),
            baseline=_number(baseline_lineage.get("coverage")),
            reason="table_cell_lineage_evidence_coverage_decreased",
        )
    )
    if (
        baseline_lineage.get("all_expected_refs_resolved") is True
        and current_lineage.get("all_expected_refs_resolved") is not True
    ):
        regressions.append(
            TrendRegression(
                metric="table_cell_lineage_all_expected_refs_resolved",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="table_cell_lineage_all_expected_refs_resolved_regressed",
            )
        )
    if (
        baseline_lineage.get("all_expected_refs_covered") is True
        and current_lineage.get("all_expected_refs_covered") is not True
    ):
        regressions.append(
            TrendRegression(
                metric="table_cell_lineage_all_expected_refs_covered",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="table_cell_lineage_all_expected_refs_covered_regressed",
            )
        )
    for field_name, reason in (
        ("expected_case_refs", "table_cell_lineage_expected_case_refs_removed"),
        ("resolved_case_refs", "table_cell_lineage_resolved_case_refs_removed"),
        ("covered_case_refs", "table_cell_lineage_covered_case_refs_removed"),
        ("lineage_case_refs", "table_cell_lineage_lineage_case_refs_removed"),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=f"table_cell_lineage_{field_name}_removed_count",
                current=current_lineage.get(field_name),
                baseline=baseline_lineage.get(field_name),
                reason=reason,
            )
        )
    for field_name, reason in (
        ("unresolved_case_refs", "table_cell_lineage_unresolved_case_refs_added"),
        ("uncovered_case_refs", "table_cell_lineage_uncovered_case_refs_added"),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"table_cell_lineage_{field_name}_added_count",
                current=current_lineage.get(field_name),
                baseline=baseline_lineage.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _preview_addressability_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """preview / bbox overlay の実測件数退化を検出する。"""
    current_preview = _mapping(current.get("preview_addressability"))
    baseline_preview = _mapping(baseline.get("preview_addressability"))
    if not baseline_preview:
        return []
    regressions: list[TrendRegression] = []
    for metric, reason in (
        (
            "preview_gate_case_count",
            "preview_addressability_gate_case_count_decreased",
        ),
        (
            "chunk_target_count",
            "preview_addressability_chunk_target_count_decreased",
        ),
        (
            "chunk_bbox_count",
            "preview_addressability_chunk_bbox_count_decreased",
        ),
        (
            "chunk_addressable_count",
            "preview_addressability_chunk_addressable_count_decreased",
        ),
        (
            "extraction_bbox_target_count",
            "preview_addressability_extraction_bbox_target_count_decreased",
        ),
        (
            "extraction_addressable_target_count",
            "preview_addressability_extraction_addressable_target_count_decreased",
        ),
        (
            "target_count",
            "preview_addressability_target_count_decreased",
        ),
        (
            "addressable_target_count",
            "preview_addressability_addressable_target_count_decreased",
        ),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"preview_addressability_{metric}",
                current=_number(current_preview.get(metric)),
                baseline=_number(baseline_preview.get(metric)),
                reason=reason,
            )
        )
    regressions.extend(
        _count_regression(
            metric="preview_addressability_unaddressable_target_count",
            current=_number(current_preview.get("unaddressable_target_count")),
            baseline=_number(baseline_preview.get("unaddressable_target_count")),
            reason="preview_addressability_unaddressable_target_count_increased",
        )
    )
    for metric, reason in (
        ("coverage", "preview_addressability_evidence_coverage_decreased"),
        ("chunk_bbox_coverage", "preview_addressability_chunk_bbox_coverage_decreased"),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"preview_addressability_{metric}",
                current=_number(current_preview.get(metric)),
                baseline=_number(baseline_preview.get(metric)),
                reason=reason,
            )
        )
    if (
        baseline_preview.get("all_targets_addressable") is True
        and current_preview.get("all_targets_addressable") is not True
    ):
        regressions.append(
            TrendRegression(
                metric="preview_addressability_all_targets_addressable",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="preview_addressability_all_targets_addressable_regressed",
            )
        )
    if (
        baseline_preview.get("all_chunks_have_bbox") is True
        and current_preview.get("all_chunks_have_bbox") is not True
    ):
        regressions.append(
            TrendRegression(
                metric="preview_addressability_all_chunks_have_bbox",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="preview_addressability_all_chunks_have_bbox_regressed",
            )
        )
    for field_name, reason in (
        ("preview_gate_case_refs", "preview_addressability_gate_case_refs_removed"),
        ("addressable_case_refs", "preview_addressability_addressable_case_refs_removed"),
        ("chunk_bbox_case_refs", "preview_addressability_chunk_bbox_case_refs_removed"),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=f"preview_addressability_{field_name}_removed_count",
                current=current_preview.get(field_name),
                baseline=baseline_preview.get(field_name),
                reason=reason,
            )
        )
    for field_name, reason in (
        (
            "unaddressable_case_refs",
            "preview_addressability_unaddressable_case_refs_added",
        ),
        (
            "chunk_missing_bbox_case_refs",
            "preview_addressability_chunk_missing_bbox_case_refs_added",
        ),
    ):
        regressions.extend(
            _string_set_added_regression(
                metric=f"preview_addressability_{field_name}_added_count",
                current=current_preview.get(field_name),
                baseline=baseline_preview.get(field_name),
                reason=reason,
            )
        )
    return regressions


def _adapter_golden_gate_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """同一 golden set adapter gate の証跡退化を検出する。"""
    current_gate = _mapping(current.get("adapter_golden_gate"))
    baseline_gate = _mapping(baseline.get("adapter_golden_gate"))
    if not baseline_gate:
        return []
    regressions: list[TrendRegression] = []
    if bool(baseline_gate.get("passed")) and current_gate.get("passed") is not True:
        regressions.append(
            TrendRegression(
                metric="adapter_golden_gate_passed",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="adapter_golden_gate_passed_regressed",
            )
        )
    for field_name, reason in (
        ("mode", "adapter_golden_gate_mode_changed"),
        ("metrics_source", "adapter_golden_gate_metrics_source_changed"),
        ("selected_backend", "adapter_golden_gate_selected_backend_changed"),
        ("recommended_backend", "adapter_golden_gate_recommended_backend_changed"),
        ("metrics_applied_to", "adapter_golden_gate_metrics_applied_to_changed"),
    ):
        baseline_value = _optional_str(baseline_gate.get(field_name))
        if baseline_value is None:
            continue
        current_value = _optional_str(current_gate.get(field_name))
        if current_value != baseline_value:
            regressions.append(
                TrendRegression(
                    metric=f"adapter_golden_gate_{field_name}",
                    direction="min",
                    baseline=1.0,
                    current=0.0,
                    allowed_delta=0.0,
                    delta=-1.0,
                    reason=reason,
                )
            )
    baseline_contract_passed = baseline_gate.get("contract_passed")
    current_contract_passed = current_gate.get("contract_passed")
    if baseline_contract_passed is True and current_contract_passed is not True:
        regressions.append(
            TrendRegression(
                metric="adapter_golden_gate_contract_passed",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="adapter_golden_gate_contract_regressed",
            )
        )
    for field_name, reason in (
        (
            "required_source_kinds",
            "adapter_golden_gate_required_source_kind_count_decreased",
        ),
        (
            "manifest_source_kinds",
            "adapter_golden_gate_manifest_source_kind_count_decreased",
        ),
        (
            "covered_source_kinds",
            "adapter_golden_gate_covered_source_kind_count_decreased",
        ),
    ):
        regressions.extend(
            _count_decrease_regression(
                metric=f"adapter_golden_gate_{field_name}",
                current=_sequence_count(current_gate.get(field_name)),
                baseline=_sequence_count(baseline_gate.get(field_name)),
                reason=reason,
            )
        )
        regressions.extend(
            _string_set_removed_regression(
                metric=f"adapter_golden_gate_{field_name}_removed_count",
                current=current_gate.get(field_name),
                baseline=baseline_gate.get(field_name),
                reason=reason.replace("_count_decreased", "s_removed"),
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="adapter_golden_gate_contract_case_count",
            current=_number(current_gate.get("contract_case_count")),
            baseline=_number(baseline_gate.get("contract_case_count")),
            reason="adapter_golden_gate_contract_case_count_decreased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_missing_source_kind_count",
            current=_sequence_count(current_gate.get("missing_source_kinds")),
            baseline=_sequence_count(baseline_gate.get("missing_source_kinds")),
            reason="adapter_golden_gate_missing_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_missing_source_kind_added_count",
            current=current_gate.get("missing_source_kinds"),
            baseline=baseline_gate.get("missing_source_kinds"),
            reason="adapter_golden_gate_missing_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_missing_manifest_source_kind_count",
            current=_sequence_count(current_gate.get("missing_manifest_source_kinds")),
            baseline=_sequence_count(baseline_gate.get("missing_manifest_source_kinds")),
            reason="adapter_golden_gate_missing_manifest_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_missing_manifest_source_kind_added_count",
            current=current_gate.get("missing_manifest_source_kinds"),
            baseline=baseline_gate.get("missing_manifest_source_kinds"),
            reason="adapter_golden_gate_missing_manifest_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_contract_missing_source_kind_count",
            current=_sequence_count(current_gate.get("contract_missing_source_kinds")),
            baseline=_sequence_count(baseline_gate.get("contract_missing_source_kinds")),
            reason="adapter_golden_gate_contract_missing_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_contract_missing_source_kind_added_count",
            current=current_gate.get("contract_missing_source_kinds"),
            baseline=baseline_gate.get("contract_missing_source_kinds"),
            reason="adapter_golden_gate_contract_missing_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_source_route_contract_gap_source_kind_count",
            current=_sequence_count(current_gate.get("source_route_contract_gap_source_kinds")),
            baseline=_sequence_count(baseline_gate.get("source_route_contract_gap_source_kinds")),
            reason=("adapter_golden_gate_source_route_contract_gap_source_kind_count_increased"),
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_source_route_contract_gap_source_kind_added_count",
            current=current_gate.get("source_route_contract_gap_source_kinds"),
            baseline=baseline_gate.get("source_route_contract_gap_source_kinds"),
            reason="adapter_golden_gate_source_route_contract_gap_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_missing_metric_count",
            current=_sequence_count(current_gate.get("missing_metric_names")),
            baseline=_sequence_count(baseline_gate.get("missing_metric_names")),
            reason="adapter_golden_gate_missing_metric_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_missing_metric_added_count",
            current=current_gate.get("missing_metric_names"),
            baseline=baseline_gate.get("missing_metric_names"),
            reason="adapter_golden_gate_missing_metric_names_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_failed_metric_count",
            current=_number(current_gate.get("failed_metric_count")),
            baseline=_number(baseline_gate.get("failed_metric_count")),
            reason="adapter_golden_gate_failed_metric_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_contract_blocking_failure_count",
            current=_number(current_gate.get("contract_blocking_failure_count")),
            baseline=_number(baseline_gate.get("contract_blocking_failure_count")),
            reason="adapter_golden_gate_contract_blocking_failure_count_increased",
        )
    )
    regressions.extend(
        _string_set_removed_regression(
            metric="adapter_golden_gate_contract_passed_case_ref_removed_count",
            current=current_gate.get("contract_passed_case_refs"),
            baseline=baseline_gate.get("contract_passed_case_refs"),
            reason="adapter_golden_gate_contract_passed_case_refs_removed",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_contract_blocking_failure_case_ref_added_count",
            current=current_gate.get("contract_blocking_failure_case_refs"),
            baseline=baseline_gate.get("contract_blocking_failure_case_refs"),
            reason="adapter_golden_gate_contract_blocking_failure_case_refs_added",
        )
    )
    baseline_backend_case_ref_pairs = _backend_string_pairs(
        baseline_gate.get("contract_backend_passed_case_refs")
    )
    current_backend_case_ref_pairs = _backend_string_pairs(
        current_gate.get("contract_backend_passed_case_refs")
    )
    removed_backend_case_ref_pair_count = len(
        baseline_backend_case_ref_pairs - current_backend_case_ref_pairs
    )
    if removed_backend_case_ref_pair_count:
        regressions.append(
            TrendRegression(
                metric="adapter_golden_gate_contract_backend_case_ref_pair_removed_count",
                direction="max",
                baseline=0.0,
                current=float(removed_backend_case_ref_pair_count),
                allowed_delta=0.0,
                delta=float(removed_backend_case_ref_pair_count),
                reason="adapter_golden_gate_contract_backend_passed_case_refs_removed",
            )
        )
    regressions.extend(
        _count_regression(
            metric="adapter_golden_gate_blocker_code_count",
            current=_sequence_count(current_gate.get("blocker_codes")),
            baseline=_sequence_count(baseline_gate.get("blocker_codes")),
            reason="adapter_golden_gate_blocker_code_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="adapter_golden_gate_blocker_code_added_count",
            current=current_gate.get("blocker_codes"),
            baseline=baseline_gate.get("blocker_codes"),
            reason="adapter_golden_gate_blocker_codes_added",
        )
    )
    return regressions


def _parser_adapter_contract_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """strict adapter contract / remap matrix の証跡退化を検出する。"""
    regressions: list[TrendRegression] = []
    baseline_mode = _optional_str(baseline.get("parser_adapter_contract_mode"))
    current_mode = _optional_str(current.get("parser_adapter_contract_mode"))
    if baseline_mode == "strict" and current_mode != "strict":
        regressions.append(
            TrendRegression(
                metric="parser_adapter_contract_mode",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="parser_adapter_contract_strict_mode_removed",
            )
        )

    baseline_contract = _mapping(baseline.get("parser_adapter_contract"))
    current_contract = _mapping(current.get("parser_adapter_contract"))
    if not baseline_contract:
        return regressions
    if bool(baseline_contract.get("passed")) and current_contract.get("passed") is not True:
        regressions.append(
            TrendRegression(
                metric="parser_adapter_contract_passed",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="parser_adapter_contract_passed_regressed",
            )
        )
    baseline_case_count = _number(baseline_contract.get("case_count"))
    current_case_count = _number(current_contract.get("case_count"))
    if (
        baseline_case_count is not None
        and current_case_count is not None
        and current_case_count < baseline_case_count
    ):
        regressions.append(
            TrendRegression(
                metric="parser_adapter_contract_case_count",
                direction="min",
                baseline=baseline_case_count,
                current=current_case_count,
                allowed_delta=0.0,
                delta=current_case_count - baseline_case_count,
                reason="parser_adapter_contract_case_count_decreased",
            )
        )
    baseline_scenarios = _string_set(baseline_contract.get("scenarios"))
    current_scenarios = _string_set(current_contract.get("scenarios"))
    if baseline_scenarios and current_scenarios:
        removed_scenario_count = len(baseline_scenarios - current_scenarios)
        if removed_scenario_count:
            regressions.append(
                TrendRegression(
                    metric="parser_adapter_contract_scenario_removed_count",
                    direction="max",
                    baseline=0.0,
                    current=float(removed_scenario_count),
                    allowed_delta=0.0,
                    delta=float(removed_scenario_count),
                    reason="parser_adapter_contract_scenarios_removed",
                )
            )
    for field_name, metric, reason in (
        (
            "source_kinds",
            "parser_adapter_contract_source_kind_removed_count",
            "parser_adapter_contract_source_kinds_removed",
        ),
        (
            "backends",
            "parser_adapter_contract_backend_removed_count",
            "parser_adapter_contract_backends_removed",
        ),
    ):
        regressions.extend(
            _string_set_removed_regression(
                metric=metric,
                current=current_contract.get(field_name),
                baseline=baseline_contract.get(field_name),
                reason=reason,
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="parser_adapter_contract_scenario_count",
            current=_sequence_count(current_contract.get("scenarios")),
            baseline=_sequence_count(baseline_contract.get("scenarios")),
            reason="parser_adapter_contract_scenario_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="parser_adapter_contract_passed_scenario_count",
            current=_sequence_count(current_contract.get("passed_scenarios")),
            baseline=_sequence_count(baseline_contract.get("passed_scenarios")),
            reason="parser_adapter_contract_passed_scenario_count_decreased",
        )
    )
    regressions.extend(
        _string_set_removed_regression(
            metric="parser_adapter_contract_passed_scenario_removed_count",
            current=current_contract.get("passed_scenarios"),
            baseline=baseline_contract.get("passed_scenarios"),
            reason="parser_adapter_contract_passed_scenarios_removed",
        )
    )
    regressions.extend(
        _string_set_removed_regression(
            metric="parser_adapter_contract_passed_source_kind_removed_count",
            current=current_contract.get("passed_source_kinds"),
            baseline=baseline_contract.get("passed_source_kinds"),
            reason="parser_adapter_contract_passed_source_kinds_removed",
        )
    )
    regressions.extend(
        _string_set_removed_regression(
            metric="parser_adapter_contract_passed_case_ref_removed_count",
            current=current_contract.get("passed_case_refs"),
            baseline=baseline_contract.get("passed_case_refs"),
            reason="parser_adapter_contract_passed_case_refs_removed",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="parser_adapter_contract_blocking_failure_case_ref_added_count",
            current=current_contract.get("blocking_failure_case_refs"),
            baseline=baseline_contract.get("blocking_failure_case_refs"),
            reason="parser_adapter_contract_blocking_failure_case_refs_added",
        )
    )
    baseline_backend_case_ref_pairs = _backend_string_pairs(
        baseline_contract.get("backend_passed_case_refs")
    )
    current_backend_case_ref_pairs = _backend_string_pairs(
        current_contract.get("backend_passed_case_refs")
    )
    removed_backend_case_ref_pair_count = len(
        baseline_backend_case_ref_pairs - current_backend_case_ref_pairs
    )
    if removed_backend_case_ref_pair_count:
        regressions.append(
            TrendRegression(
                metric="parser_adapter_contract_backend_case_ref_pair_removed_count",
                direction="max",
                baseline=0.0,
                current=float(removed_backend_case_ref_pair_count),
                allowed_delta=0.0,
                delta=float(removed_backend_case_ref_pair_count),
                reason="parser_adapter_contract_backend_passed_case_refs_removed",
            )
        )
    baseline_backend_source_pairs = _backend_source_pairs(
        baseline_contract.get("backend_passed_source_kinds")
    )
    current_backend_source_pairs = _backend_source_pairs(
        current_contract.get("backend_passed_source_kinds")
    )
    if baseline_backend_source_pairs and current_backend_source_pairs:
        removed_backend_source_pair_count = len(
            baseline_backend_source_pairs - current_backend_source_pairs
        )
        if removed_backend_source_pair_count:
            regressions.append(
                TrendRegression(
                    metric="parser_adapter_contract_backend_source_pair_removed_count",
                    direction="max",
                    baseline=0.0,
                    current=float(removed_backend_source_pair_count),
                    allowed_delta=0.0,
                    delta=float(removed_backend_source_pair_count),
                    reason="parser_adapter_contract_backend_source_pairs_removed",
                )
            )
    baseline_backend_source_pair_count = (
        float(len(baseline_backend_source_pairs)) if baseline_backend_source_pairs else None
    )
    current_backend_source_pair_count = (
        float(len(current_backend_source_pairs)) if current_backend_source_pairs else None
    )
    regressions.extend(
        _count_decrease_regression(
            metric="parser_adapter_contract_backend_source_pair_count",
            current=current_backend_source_pair_count,
            baseline=baseline_backend_source_pair_count,
            reason="parser_adapter_contract_backend_source_pair_count_decreased",
        )
    )
    baseline_backend_scenario_pairs = _backend_string_pairs(
        baseline_contract.get("backend_passed_scenarios")
    )
    current_backend_scenario_pairs = _backend_string_pairs(
        current_contract.get("backend_passed_scenarios")
    )
    if baseline_backend_scenario_pairs and current_backend_scenario_pairs:
        removed_backend_scenario_pair_count = len(
            baseline_backend_scenario_pairs - current_backend_scenario_pairs
        )
        if removed_backend_scenario_pair_count:
            regressions.append(
                TrendRegression(
                    metric="parser_adapter_contract_backend_scenario_pair_removed_count",
                    direction="max",
                    baseline=0.0,
                    current=float(removed_backend_scenario_pair_count),
                    allowed_delta=0.0,
                    delta=float(removed_backend_scenario_pair_count),
                    reason="parser_adapter_contract_backend_scenario_pairs_removed",
                )
            )
    baseline_backend_scenario_pair_count = (
        float(len(baseline_backend_scenario_pairs)) if baseline_backend_scenario_pairs else None
    )
    current_backend_scenario_pair_count = (
        float(len(current_backend_scenario_pairs)) if current_backend_scenario_pairs else None
    )
    regressions.extend(
        _count_decrease_regression(
            metric="parser_adapter_contract_backend_scenario_pair_count",
            current=current_backend_scenario_pair_count,
            baseline=baseline_backend_scenario_pair_count,
            reason="parser_adapter_contract_backend_scenario_pair_count_decreased",
        )
    )
    regressions.extend(
        _backend_source_bad_status_regressions(
            current_contract=current_contract,
            baseline_contract=baseline_contract,
        )
    )
    regressions.extend(
        _backend_source_passed_status_regressions(
            current_contract=current_contract,
            baseline_contract=baseline_contract,
        )
    )
    regressions.extend(
        _parser_adapter_package_version_regressions(
            current_contract=current_contract,
            baseline_contract=baseline_contract,
        )
    )
    regressions.extend(
        _code_count_regressions(
            metric_prefix="parser_adapter_contract_warning_code_count",
            reason="parser_adapter_contract_warning_code_count_increased",
            current_counts=_mapping(current_contract.get("warning_code_counts")),
            baseline_counts=_mapping(baseline_contract.get("warning_code_counts")),
        )
    )
    regressions.extend(
        _code_count_regressions(
            metric_prefix="parser_adapter_contract_blocking_failure_reason_count",
            reason="parser_adapter_contract_blocking_failure_reason_count_increased",
            current_counts=_mapping(current_contract.get("blocking_failure_reason_counts")),
            baseline_counts=_mapping(baseline_contract.get("blocking_failure_reason_counts")),
        )
    )
    regressions.extend(
        _count_regression(
            metric="parser_adapter_contract_blocking_failure_count",
            current=_number(current_contract.get("blocking_failure_count")),
            baseline=_number(baseline_contract.get("blocking_failure_count")),
            reason="parser_adapter_contract_blocking_failure_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="parser_adapter_contract_missing_source_kind_count",
            current=_sequence_count(current_contract.get("missing_source_kinds")),
            baseline=_sequence_count(baseline_contract.get("missing_source_kinds")),
            reason="parser_adapter_contract_missing_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="parser_adapter_contract_missing_source_kind_added_count",
            current=current_contract.get("missing_source_kinds"),
            baseline=baseline_contract.get("missing_source_kinds"),
            reason="parser_adapter_contract_missing_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="parser_adapter_contract_missing_scenario_count",
            current=_sequence_count(current_contract.get("missing_scenarios")),
            baseline=_sequence_count(baseline_contract.get("missing_scenarios")),
            reason="parser_adapter_contract_missing_scenario_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="parser_adapter_contract_missing_scenario_added_count",
            current=current_contract.get("missing_scenarios"),
            baseline=baseline_contract.get("missing_scenarios"),
            reason="parser_adapter_contract_missing_scenarios_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="parser_adapter_contract_blocking_source_kind_count",
            current=_sequence_count(current_contract.get("blocking_failure_source_kinds")),
            baseline=_sequence_count(baseline_contract.get("blocking_failure_source_kinds")),
            reason="parser_adapter_contract_blocking_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="parser_adapter_contract_blocking_source_kind_added_count",
            current=current_contract.get("blocking_failure_source_kinds"),
            baseline=baseline_contract.get("blocking_failure_source_kinds"),
            reason="parser_adapter_contract_blocking_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="parser_adapter_contract_blocking_scenario_count",
            current=_sequence_count(current_contract.get("blocking_failure_scenarios")),
            baseline=_sequence_count(baseline_contract.get("blocking_failure_scenarios")),
            reason="parser_adapter_contract_blocking_scenario_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="parser_adapter_contract_blocking_scenario_added_count",
            current=current_contract.get("blocking_failure_scenarios"),
            baseline=baseline_contract.get("blocking_failure_scenarios"),
            reason="parser_adapter_contract_blocking_scenarios_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="parser_adapter_contract_blocking_backend_count",
            current=_sequence_count(current_contract.get("blocking_failure_backends")),
            baseline=_sequence_count(baseline_contract.get("blocking_failure_backends")),
            reason="parser_adapter_contract_blocking_backend_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="parser_adapter_contract_blocking_backend_added_count",
            current=current_contract.get("blocking_failure_backends"),
            baseline=baseline_contract.get("blocking_failure_backends"),
            reason="parser_adapter_contract_blocking_backends_added",
        )
    )
    return regressions


def _parser_adapter_package_version_regressions(
    *,
    current_contract: Mapping[str, Any],
    baseline_contract: Mapping[str, Any],
) -> list[TrendRegression]:
    """adapter package/version 証跡の drift を regression として扱う。"""
    baseline_pairs = _string_set(baseline_contract.get("adapter_package_version_pairs"))
    if not baseline_pairs:
        return []
    current_pairs = _string_set(current_contract.get("adapter_package_version_pairs"))
    regressions: list[TrendRegression] = []
    removed_pair_count = len(baseline_pairs - current_pairs)
    if removed_pair_count:
        regressions.append(
            TrendRegression(
                metric="parser_adapter_contract_package_version_pair_removed_count",
                direction="max",
                baseline=0.0,
                current=float(removed_pair_count),
                allowed_delta=0.0,
                delta=float(removed_pair_count),
                reason="parser_adapter_contract_package_version_pairs_removed",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="parser_adapter_contract_package_version_pair_count",
            current=float(len(current_pairs)),
            baseline=float(len(baseline_pairs)),
            reason="parser_adapter_contract_package_version_pair_count_decreased",
        )
    )
    return regressions


def _staging_dataset_policy_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[TrendRegression]:
    """real-world staging dataset policy の証跡退化を検出する。"""
    current_policy = _mapping(current.get("staging_dataset_policy"))
    baseline_policy = _mapping(baseline.get("staging_dataset_policy"))
    if not baseline_policy:
        return []
    regressions: list[TrendRegression] = []
    if bool(baseline_policy.get("configured")) and not bool(current_policy.get("configured")):
        regressions.append(
            TrendRegression(
                metric="staging_dataset_policy_configured",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="staging_dataset_policy_removed",
            )
        )
    if bool(baseline_policy.get("promotion_ready")) and not bool(
        current_policy.get("promotion_ready")
    ):
        regressions.append(
            TrendRegression(
                metric="staging_dataset_policy_promotion_ready",
                direction="min",
                baseline=1.0,
                current=0.0,
                allowed_delta=0.0,
                delta=-1.0,
                reason="staging_dataset_policy_promotion_ready_regressed",
            )
        )
    baseline_real_world_count = _number(baseline_policy.get("real_world_case_count"))
    current_real_world_count = _number(current_policy.get("real_world_case_count"))
    if (
        baseline_real_world_count is not None
        and current_real_world_count is not None
        and current_real_world_count < baseline_real_world_count
    ):
        regressions.append(
            TrendRegression(
                metric="real_world_case_count",
                direction="min",
                baseline=baseline_real_world_count,
                current=current_real_world_count,
                allowed_delta=0.0,
                delta=current_real_world_count - baseline_real_world_count,
                reason="real_world_case_count_decreased",
            )
        )
    regressions.extend(
        _count_decrease_regression(
            metric="executed_real_world_case_count",
            current=_number(current_policy.get("executed_real_world_case_count")),
            baseline=_number(baseline_policy.get("executed_real_world_case_count")),
            reason="executed_real_world_case_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="executed_compliant_real_world_case_count",
            current=_number(current_policy.get("executed_compliant_real_world_case_count")),
            baseline=_number(baseline_policy.get("executed_compliant_real_world_case_count")),
            reason="executed_compliant_real_world_case_count_decreased",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="staging_dataset_executed_source_kind_count",
            current=_sequence_count(current_policy.get("executed_source_kinds")),
            baseline=_sequence_count(baseline_policy.get("executed_source_kinds")),
            reason="staging_dataset_executed_source_kind_count_decreased",
        )
    )
    regressions.extend(
        _string_set_removed_regression(
            metric="staging_dataset_executed_source_kind_removed_count",
            current=current_policy.get("executed_source_kinds"),
            baseline=baseline_policy.get("executed_source_kinds"),
            reason="staging_dataset_executed_source_kinds_removed",
        )
    )
    regressions.extend(
        _count_decrease_regression(
            metric="staging_dataset_executed_scenario_count",
            current=_sequence_count(current_policy.get("executed_scenarios")),
            baseline=_sequence_count(baseline_policy.get("executed_scenarios")),
            reason="staging_dataset_executed_scenario_count_decreased",
        )
    )
    regressions.extend(
        _string_set_removed_regression(
            metric="staging_dataset_executed_scenario_removed_count",
            current=current_policy.get("executed_scenarios"),
            baseline=baseline_policy.get("executed_scenarios"),
            reason="staging_dataset_executed_scenarios_removed",
        )
    )
    regressions.extend(
        _count_regression(
            metric="staging_dataset_policy_error_count",
            current=_number(current_policy.get("policy_error_count")),
            baseline=_number(baseline_policy.get("policy_error_count")),
            reason="staging_dataset_policy_error_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="staging_dataset_missing_source_kind_count",
            current=_sequence_count(current_policy.get("missing_source_kinds")),
            baseline=_sequence_count(baseline_policy.get("missing_source_kinds")),
            reason="staging_dataset_missing_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="staging_dataset_missing_source_kind_added_count",
            current=current_policy.get("missing_source_kinds"),
            baseline=baseline_policy.get("missing_source_kinds"),
            reason="staging_dataset_missing_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="staging_dataset_missing_scenario_count",
            current=_sequence_count(current_policy.get("missing_scenarios")),
            baseline=_sequence_count(baseline_policy.get("missing_scenarios")),
            reason="staging_dataset_missing_scenario_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="staging_dataset_missing_scenario_added_count",
            current=current_policy.get("missing_scenarios"),
            baseline=baseline_policy.get("missing_scenarios"),
            reason="staging_dataset_missing_scenarios_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="staging_dataset_execution_error_count",
            current=_number(current_policy.get("execution_error_count")),
            baseline=_number(baseline_policy.get("execution_error_count")),
            reason="staging_dataset_execution_error_count_increased",
        )
    )
    regressions.extend(
        _count_regression(
            metric="staging_dataset_missing_executed_source_kind_count",
            current=_sequence_count(current_policy.get("missing_executed_source_kinds")),
            baseline=_sequence_count(baseline_policy.get("missing_executed_source_kinds")),
            reason="staging_dataset_missing_executed_source_kind_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="staging_dataset_missing_executed_source_kind_added_count",
            current=current_policy.get("missing_executed_source_kinds"),
            baseline=baseline_policy.get("missing_executed_source_kinds"),
            reason="staging_dataset_missing_executed_source_kinds_added",
        )
    )
    regressions.extend(
        _count_regression(
            metric="staging_dataset_missing_executed_scenario_count",
            current=_sequence_count(current_policy.get("missing_executed_scenarios")),
            baseline=_sequence_count(baseline_policy.get("missing_executed_scenarios")),
            reason="staging_dataset_missing_executed_scenario_count_increased",
        )
    )
    regressions.extend(
        _string_set_added_regression(
            metric="staging_dataset_missing_executed_scenario_added_count",
            current=current_policy.get("missing_executed_scenarios"),
            baseline=baseline_policy.get("missing_executed_scenarios"),
            reason="staging_dataset_missing_executed_scenarios_added",
        )
    )
    return regressions


def _backend_source_bad_status_regressions(
    *,
    current_contract: Mapping[str, object],
    baseline_contract: Mapping[str, object],
) -> list[TrendRegression]:
    current_counts = _backend_source_status_counts(
        current_contract.get("backend_source_status_counts")
    )
    baseline_counts = _backend_source_status_counts(
        baseline_contract.get("backend_source_status_counts")
    )
    regressions: list[TrendRegression] = []
    for key in sorted(set(current_counts) | set(baseline_counts)):
        backend, source_kind, status = key
        if status not in PARSER_ADAPTER_CONTRACT_BAD_STATUSES:
            continue
        current_value = float(current_counts.get(key, 0))
        baseline_value = float(baseline_counts.get(key, 0))
        if current_value <= baseline_value:
            continue
        regressions.append(
            TrendRegression(
                metric=(
                    "parser_adapter_contract_backend_source_status_count:"
                    f"{backend}:{source_kind}:{status}"
                ),
                direction="max",
                baseline=baseline_value,
                current=current_value,
                allowed_delta=0.0,
                delta=current_value - baseline_value,
                reason="parser_adapter_contract_backend_source_bad_status_count_increased",
            )
        )
    return regressions


def _code_count_regressions(
    *,
    metric_prefix: str,
    reason: str,
    current_counts: Mapping[str, object],
    baseline_counts: Mapping[str, object],
) -> list[TrendRegression]:
    regressions: list[TrendRegression] = []
    for code in sorted(set(current_counts) | set(baseline_counts)):
        current_value = float(_int_count(current_counts.get(code)))
        baseline_value = float(_int_count(baseline_counts.get(code)))
        if current_value <= baseline_value:
            continue
        regressions.append(
            TrendRegression(
                metric=f"{metric_prefix}:{code}",
                direction="max",
                baseline=baseline_value,
                current=current_value,
                allowed_delta=0.0,
                delta=current_value - baseline_value,
                reason=reason,
            )
        )
    return regressions


def _backend_source_passed_status_regressions(
    *,
    current_contract: Mapping[str, object],
    baseline_contract: Mapping[str, object],
) -> list[TrendRegression]:
    current_counts = _backend_source_status_counts(
        current_contract.get("backend_source_status_counts")
    )
    baseline_counts = _backend_source_status_counts(
        baseline_contract.get("backend_source_status_counts")
    )
    regressions: list[TrendRegression] = []
    for key in sorted(set(current_counts) | set(baseline_counts)):
        backend, source_kind, status = key
        if status != "passed":
            continue
        current_value = float(current_counts.get(key, 0))
        baseline_value = float(baseline_counts.get(key, 0))
        if current_value >= baseline_value:
            continue
        regressions.append(
            TrendRegression(
                metric=(
                    "parser_adapter_contract_backend_source_status_count:"
                    f"{backend}:{source_kind}:passed"
                ),
                direction="min",
                baseline=baseline_value,
                current=current_value,
                allowed_delta=0.0,
                delta=current_value - baseline_value,
                reason=("parser_adapter_contract_backend_source_passed_status_count_decreased"),
            )
        )
    return regressions


def _count_decrease_regression(
    *,
    metric: str,
    current: float | None,
    baseline: float | None,
    reason: str,
) -> list[TrendRegression]:
    if current is None or baseline is None or current >= baseline:
        return []
    return [
        TrendRegression(
            metric=metric,
            direction="min",
            baseline=baseline,
            current=current,
            allowed_delta=0.0,
            delta=current - baseline,
            reason=reason,
        )
    ]


def _count_regression(
    *,
    metric: str,
    current: float | None,
    baseline: float | None,
    reason: str,
) -> list[TrendRegression]:
    if current is None or baseline is None or current <= baseline:
        return []
    return [
        TrendRegression(
            metric=metric,
            direction="max",
            baseline=baseline,
            current=current,
            allowed_delta=0.0,
            delta=current - baseline,
            reason=reason,
        )
    ]


def _metric_values(trend: Mapping[str, Any]) -> dict[str, float]:
    metrics = _mapping(trend.get("metrics"))
    values: dict[str, float] = {}
    for metric, raw_value in metrics.items():
        value = _metric_value(raw_value)
        if value is not None:
            values[str(metric)] = value
    return values


def _metric_value(raw_value: object) -> float | None:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int | float):
        return float(raw_value)
    if isinstance(raw_value, dict):
        status = raw_value.get("status")
        if status not in {None, "measured", "partial"}:
            return None
        return _number(raw_value.get("value"))
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _sequence_count(value: object) -> float | None:
    if not isinstance(value, list | tuple | set):
        return None
    return float(len(value))


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list | tuple | set):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


def _string_set_removed_regression(
    *,
    metric: str,
    current: object,
    baseline: object,
    reason: str,
) -> list[TrendRegression]:
    baseline_values = _string_set(baseline)
    current_values = _string_set(current)
    removed_count = len(baseline_values - current_values)
    if not baseline_values or removed_count == 0:
        return []
    return [
        TrendRegression(
            metric=metric,
            direction="max",
            baseline=0.0,
            current=float(removed_count),
            allowed_delta=0.0,
            delta=float(removed_count),
            reason=reason,
        )
    ]


def _string_set_added_regression(
    *,
    metric: str,
    current: object,
    baseline: object,
    reason: str,
) -> list[TrendRegression]:
    baseline_values = _string_set(baseline)
    current_values = _string_set(current)
    added_count = len(current_values - baseline_values)
    if added_count == 0:
        return []
    return [
        TrendRegression(
            metric=metric,
            direction="max",
            baseline=0.0,
            current=float(added_count),
            allowed_delta=0.0,
            delta=float(added_count),
            reason=reason,
        )
    ]


def _backend_source_pairs(value: object) -> set[str]:
    return _backend_string_pairs(value)


def _chunk_template_entries_by_template(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list | tuple):
        return {}
    entries: dict[str, Mapping[str, object]] = {}
    for item in value:
        entry = _mapping(item)
        template = _optional_str(entry.get("template"))
        if template is None:
            continue
        entries[template] = entry
    return entries


def _parser_adapter_entries_by_backend(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list | tuple):
        return {}
    entries: dict[str, Mapping[str, object]] = {}
    for item in value:
        entry = _mapping(item)
        backend = _optional_str(entry.get("backend"))
        if backend is None:
            continue
        entries[backend] = entry
    return entries


def _parser_adapter_routes_by_source(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list | tuple):
        return {}
    routes: dict[str, Mapping[str, object]] = {}
    for item in value:
        route = _mapping(item)
        source_kind = _optional_str(route.get("source_kind"))
        if source_kind is None:
            continue
        routes[source_kind] = route
    return routes


def _backend_source_status_counts(value: object) -> dict[tuple[str, str, str], int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[tuple[str, str, str], int] = {}
    for backend, raw_source_counts in value.items():
        if not isinstance(backend, str) or not backend.strip():
            continue
        if not isinstance(raw_source_counts, dict):
            continue
        for source_kind, raw_status_counts in raw_source_counts.items():
            if not isinstance(source_kind, str) or not source_kind.strip():
                continue
            if not isinstance(raw_status_counts, dict):
                continue
            for status, raw_count in raw_status_counts.items():
                count = _int_count(raw_count)
                if not isinstance(status, str) or not status.strip() or count <= 0:
                    continue
                counts[(backend.strip(), source_kind.strip(), status.strip())] = count
    return counts


def _threshold_failure_metrics(value: object) -> set[str]:
    if not isinstance(value, list | tuple):
        return set()
    metrics: set[str] = set()
    for item in value:
        failure = _mapping(item)
        metric = _optional_str(failure.get("metric"))
        if metric is not None:
            metrics.add(metric)
    return metrics


def _status_count(counts: Mapping[str, object], status: str) -> float:
    return float(_int_count(counts.get(status)))


def _status_total_count(counts: Mapping[str, object]) -> float:
    return float(sum(_int_count(count) for count in counts.values()))


def _int_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def _backend_string_pairs(value: object) -> set[str]:
    if not isinstance(value, dict):
        return set()
    pairs: set[str] = set()
    for backend, raw_source_kinds in value.items():
        if not isinstance(backend, str) or not backend.strip():
            continue
        source_kinds = _string_set(raw_source_kinds)
        pairs.update(f"{backend.strip()}:{source_kind}" for source_kind in source_kinds)
    return pairs


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _validate_tolerances(
    *,
    allowed_drop: float,
    allowed_increase: float,
    latency_increase_ratio: float,
    latency_increase_ms: float,
) -> None:
    if allowed_drop < 0:
        raise FileProcessingTrendCliError("allowed-drop は 0 以上にしてください。")
    if allowed_increase < 0:
        raise FileProcessingTrendCliError("allowed-increase は 0 以上にしてください。")
    if latency_increase_ratio < 0:
        raise FileProcessingTrendCliError("latency-increase-ratio は 0 以上にしてください。")
    if latency_increase_ms < 0:
        raise FileProcessingTrendCliError("latency-increase-ms は 0 以上にしてください。")


def _load_trend(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileProcessingTrendCliError(f"trend JSON が見つかりません: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FileProcessingTrendCliError(
            f"trend JSON が JSON として読めません: line={exc.lineno}, column={exc.colno}"
        ) from exc
    if not isinstance(raw, dict):
        raise FileProcessingTrendCliError("trend JSON root は object にしてください。")
    if not isinstance(raw.get("metrics"), dict):
        raise FileProcessingTrendCliError("trend JSON に metrics object がありません。")
    return raw


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _comparison_hash(*, current: Mapping[str, Any], baseline: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {
            "current": current.get("result_sha256"),
            "baseline": baseline.get("result_sha256"),
            "current_kind": current.get("kind"),
            "baseline_kind": baseline.get("kind"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_payload(payload: Mapping[str, Any], output: Path | None) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        print(encoded)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
