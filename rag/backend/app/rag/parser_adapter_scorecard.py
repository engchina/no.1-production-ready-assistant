"""Parser adapter の評価 scorecard。

任意 adapter を導入しても、実運用では「どれを使うべきか」を golden/staging
指標で判断できる必要がある。この module は AutoRAG 的な評価駆動の考え方を、
本プロジェクトの parser adapter readiness と file-processing 指標へ閉じ込める。
外部 parser engine は実行せず、OCI/Oracle の確定スタックも変更しない。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from app.config import ParserAdapterBackend
from app.rag.parser_adapter_readiness import (
    ParserAdapterName,
    ParserAdapterRuntimeSettings,
    ParserAdapterRuntimeStatus,
)
from app.rag.parser_adapter_routing import (
    SOURCE_ROUTE_KINDS,
    adapter_order_for_source_kind,
    normalize_source_kind,
)

# source routing には GPU adapter(mineru/dots_ocr)も流れるため、型ドメインは全 backend を
# 含める(schema の ParserAdapterScoreBackendName と一致)。採点対象集合は ADAPTER_SCORE_BACKENDS で
# 別途 CPU/local に限定する。
ParserAdapterScoreBackend = Literal[
    "local",
    "docling",
    "unstructured",
    "mineru",
    "dots_ocr",
]
ParserAdapterScoreStatus = Literal[
    "recommended",
    "eligible",
    "available",
    "disabled",
    "ignored",
    "missing",
]
MetricDirection = Literal["higher", "lower", "latency"]


@dataclass(frozen=True)
class ParserAdapterMetricSpec:
    """downstream 指標の重みと方向。"""

    direction: MetricDirection
    weight: float


@dataclass(frozen=True)
class ParserAdapterScorecardEntry:
    """1 backend の評価結果。"""

    backend: ParserAdapterScoreBackend
    rank: int
    score: float
    status: ParserAdapterScoreStatus
    recommended: bool
    executable: bool
    selected: bool
    enabled: bool
    installed: bool
    metric_source: str
    metric_count: int
    signals: Mapping[str, float] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParserAdapterScorecard:
    """adapter backend の推奨結果。"""

    selected_backend: ParserAdapterBackend
    recommended_backend: ParserAdapterScoreBackend
    metrics_source: str
    metrics_applied_to: ParserAdapterScoreBackend | None
    entries: tuple[ParserAdapterScorecardEntry, ...]


@dataclass(frozen=True)
class ParserAdapterSourceRoute:
    """source kind ごとの adapter routing evidence。"""

    source_kind: str
    candidate_order: tuple[ParserAdapterScoreBackend, ...]
    attempted_order: tuple[ParserAdapterScoreBackend, ...]
    active_order: tuple[ParserAdapterScoreBackend, ...]
    selected_backend: ParserAdapterScoreBackend
    reason_codes: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()


METRIC_SPECS: Mapping[str, ParserAdapterMetricSpec] = {
    "retrieval_recall": ParserAdapterMetricSpec("higher", 8.0),
    "table_qa_accuracy": ParserAdapterMetricSpec("higher", 8.0),
    "page_hit_accuracy": ParserAdapterMetricSpec("higher", 8.0),
    "citation_traceability_coverage": ParserAdapterMetricSpec("higher", 5.0),
    "bbox_citation_coverage": ParserAdapterMetricSpec("higher", 5.0),
    "bbox_coordinate_validity_coverage": ParserAdapterMetricSpec("higher", 5.0),
    "preview_addressability_coverage": ParserAdapterMetricSpec("higher", 5.0),
    "element_lineage_coverage": ParserAdapterMetricSpec("higher", 6.0),
    "chunk_block_integrity": ParserAdapterMetricSpec("higher", 6.0),
    "reading_order_consistency": ParserAdapterMetricSpec("higher", 6.0),
    "structural_section_coverage": ParserAdapterMetricSpec("higher", 7.0),
    "dependency_context_recall": ParserAdapterMetricSpec("higher", 7.0),
    "table_structure_fidelity": ParserAdapterMetricSpec("higher", 7.0),
    "table_row_tree_fidelity": ParserAdapterMetricSpec("higher", 7.0),
    "visual_chunk_metadata_completeness": ParserAdapterMetricSpec("higher", 5.0),
    "chunk_size_compliance": ParserAdapterMetricSpec("higher", 4.0),
    "chunk_contextual_coherence": ParserAdapterMetricSpec("higher", 6.0),
    "cross_page_table_continuity_coverage": ParserAdapterMetricSpec("higher", 7.0),
    "ingestion_quality_report_completeness": ParserAdapterMetricSpec("higher", 4.0),
    "parser_warning_taxonomy_coverage": ParserAdapterMetricSpec("higher", 4.0),
    "parser_routing_accuracy": ParserAdapterMetricSpec("higher", 6.0),
    "source_kind_coverage": ParserAdapterMetricSpec("higher", 7.0),
    "backend_source_kind_coverage": ParserAdapterMetricSpec("higher", 7.0),
    "adapter_contract_coverage": ParserAdapterMetricSpec("higher", 9.0),
    "extraction_page_coverage": ParserAdapterMetricSpec("higher", 7.0),
    "groundedness": ParserAdapterMetricSpec("higher", 6.0),
    "low_confidence_document_rate": ParserAdapterMetricSpec("lower", 5.0),
    "failed_segment_rate": ParserAdapterMetricSpec("lower", 6.0),
    "parser_fallback_rate": ParserAdapterMetricSpec("lower", 8.0),
    "ingestion_p95_ms": ParserAdapterMetricSpec("latency", 4.0),
}
ADAPTER_SCORE_BACKENDS: tuple[ParserAdapterScoreBackend, ...] = (
    "local",
    "docling",
    "unstructured",
)
LATENCY_FULL_CREDIT_MS = 5_000.0
LATENCY_ZERO_CREDIT_MS = 60_000.0
METRIC_SCORE_DELTA_POINTS = 35.0
CORE_ADAPTER_EVIDENCE_METRICS = frozenset(
    {
        "retrieval_recall",
        "table_qa_accuracy",
        "page_hit_accuracy",
        "element_lineage_coverage",
        "source_kind_coverage",
        "backend_source_kind_coverage",
        "adapter_contract_coverage",
        "parser_fallback_rate",
    }
)
INCOMPLETE_ADAPTER_EVIDENCE_WARNING = "adapter_metric_evidence_incomplete"


def build_parser_adapter_scorecard(
    runtime: ParserAdapterRuntimeSettings,
    *,
    metrics: Mapping[str, float] | None = None,
    metrics_source: str = "runtime",
    metrics_backend: ParserAdapterScoreBackend | None = None,
) -> ParserAdapterScorecard:
    """runtime readiness と任意の downstream 指標から scorecard を作る。"""
    normalized_metrics = _normalized_metric_signals(metrics or {})
    metrics_applied_to = (
        (metrics_backend or _infer_metrics_backend(runtime)) if normalized_metrics else None
    )
    adapter_by_backend: dict[ParserAdapterName, ParserAdapterRuntimeStatus] = {
        adapter.backend: adapter for adapter in runtime.adapters
    }
    raw_entries = [
        _entry_for_backend(
            backend,
            runtime=runtime,
            adapter_by_backend=adapter_by_backend,
            signals=normalized_metrics if backend == metrics_applied_to else {},
            metrics_source=metrics_source,
            metrics_applied_to=metrics_applied_to,
        )
        for backend in ADAPTER_SCORE_BACKENDS
    ]
    recommended_backend = _recommended_backend(raw_entries)
    entries = tuple(
        _with_rank_and_recommendation(
            entry,
            rank=index + 1,
            recommended_backend=recommended_backend,
        )
        for index, entry in enumerate(
            sorted(raw_entries, key=lambda item: item.score, reverse=True)
        )
    )
    return ParserAdapterScorecard(
        selected_backend=runtime.adapter_backend,
        recommended_backend=recommended_backend,
        metrics_source=metrics_source,
        metrics_applied_to=metrics_applied_to,
        entries=entries,
    )


def build_parser_adapter_source_routes(
    runtime: ParserAdapterRuntimeSettings,
    *,
    source_kinds: Sequence[object] | None = None,
) -> tuple[ParserAdapterSourceRoute, ...]:
    """runtime 設定で source kind ごとにどの adapter を試すかを返す。"""
    kinds = _normalized_source_kinds(source_kinds)
    return tuple(_source_route(runtime, source_kind=source_kind) for source_kind in kinds)


def _entry_for_backend(
    backend: ParserAdapterScoreBackend,
    *,
    runtime: ParserAdapterRuntimeSettings,
    adapter_by_backend: Mapping[ParserAdapterName, ParserAdapterRuntimeStatus],
    signals: Mapping[str, float],
    metrics_source: str,
    metrics_applied_to: ParserAdapterScoreBackend | None,
) -> ParserAdapterScorecardEntry:
    if backend == "local":
        score = 62.0
        selected = runtime.adapter_backend == "local"
        reason_codes = ["local_parser_available"]
        if selected:
            score += 8.0
            reason_codes.append("selected_backend")
        warning_codes: list[str] = []
        executable = True
        enabled = True
        installed = True
        status: ParserAdapterScoreStatus = "eligible"
    else:
        adapter = adapter_by_backend[backend]
        score, executable, reason_codes = _adapter_readiness_score(adapter)
        selected = adapter.selected
        enabled = adapter.enabled
        installed = adapter.installed
        warning_codes = [adapter.warning_code] if adapter.warning_code else []
        status = _score_status(adapter.status)
        if selected and executable:
            score += 8.0
            reason_codes.append("selected_backend")
        if backend in runtime.effective_order and executable:
            score += 4.0
            reason_codes.append("effective_order_candidate")

    metric_count = len(signals)
    if signals:
        score += _metric_score_adjustment(signals)
        reason_codes.append("downstream_metrics_applied")
        if backend != "local" and not _adapter_metric_evidence_complete(signals):
            reason_codes.append("core_downstream_metrics_missing")
            warning_codes.append(INCOMPLETE_ADAPTER_EVIDENCE_WARNING)
    elif metrics_applied_to is not None:
        reason_codes.append("no_downstream_metrics_for_backend")
    else:
        reason_codes.append("readiness_only")

    return ParserAdapterScorecardEntry(
        backend=backend,
        rank=0,
        score=_round_score(score),
        status=status,
        recommended=False,
        executable=executable,
        selected=selected,
        enabled=enabled,
        installed=installed,
        metric_source=metrics_source if signals else "none",
        metric_count=metric_count,
        signals=dict(sorted(signals.items())),
        reason_codes=tuple(reason_codes),
        warning_codes=tuple(warning_codes),
    )


def _source_route(
    runtime: ParserAdapterRuntimeSettings,
    *,
    source_kind: str,
) -> ParserAdapterSourceRoute:
    candidate_order: tuple[ParserAdapterScoreBackend, ...] = tuple(
        adapter_order_for_source_kind(source_kind)
    )
    adapter_by_backend: dict[str, ParserAdapterRuntimeStatus] = {
        adapter.backend: adapter for adapter in runtime.adapters
    }
    attempted_order = _attempted_source_order(runtime, candidate_order)
    active_order = tuple(
        backend
        for backend in attempted_order
        if (adapter := adapter_by_backend.get(backend)) is not None and adapter.status == "active"
    )
    selected_backend: ParserAdapterScoreBackend = active_order[0] if active_order else "local"
    reason_codes = _source_route_reason_codes(
        runtime,
        source_kind=source_kind,
        candidate_order=candidate_order,
        attempted_order=attempted_order,
        active_order=active_order,
    )
    warning_codes = _source_route_warning_codes(
        runtime,
        source_kind=source_kind,
        candidate_order=candidate_order,
        attempted_order=attempted_order,
        adapter_by_backend=adapter_by_backend,
    )
    return ParserAdapterSourceRoute(
        source_kind=source_kind,
        candidate_order=candidate_order,
        attempted_order=attempted_order,
        active_order=active_order,
        selected_backend=selected_backend,
        reason_codes=reason_codes,
        warning_codes=warning_codes,
    )


def _attempted_source_order(
    runtime: ParserAdapterRuntimeSettings,
    candidate_order: tuple[ParserAdapterScoreBackend, ...],
) -> tuple[ParserAdapterScoreBackend, ...]:
    if runtime.adapter_backend == "local":
        return ()
    if runtime.adapter_backend in candidate_order:
        return (cast(ParserAdapterScoreBackend, runtime.adapter_backend),)
    return ()


def _source_route_reason_codes(
    runtime: ParserAdapterRuntimeSettings,
    *,
    source_kind: str,
    candidate_order: tuple[ParserAdapterScoreBackend, ...],
    attempted_order: tuple[ParserAdapterScoreBackend, ...],
    active_order: tuple[ParserAdapterScoreBackend, ...],
) -> tuple[str, ...]:
    reason_codes: list[str] = []
    if not candidate_order:
        reason_codes.append("local_parser_preferred_for_source")
    if runtime.adapter_backend == "local":
        reason_codes.append("local_backend_selected")
    elif attempted_order:
        reason_codes.append("selected_adapter_supported_for_source")
    else:
        reason_codes.append("selected_adapter_unsupported_for_source")
    if active_order:
        reason_codes.append("active_adapter_available_for_source")
    elif attempted_order:
        reason_codes.append("adapter_attempt_requires_fallback")
    return tuple(reason_codes)


def _source_route_warning_codes(
    runtime: ParserAdapterRuntimeSettings,
    *,
    source_kind: str,
    candidate_order: tuple[ParserAdapterScoreBackend, ...],
    attempted_order: tuple[ParserAdapterScoreBackend, ...],
    adapter_by_backend: Mapping[str, ParserAdapterRuntimeStatus],
) -> tuple[str, ...]:
    warning_codes: list[str] = []
    if runtime.adapter_backend in adapter_by_backend and candidate_order and not attempted_order:
        warning_codes.append(f"{runtime.adapter_backend}_adapter_source_unsupported")
    for backend in candidate_order:
        adapter = adapter_by_backend.get(backend)
        if adapter is None:
            continue
        if not adapter.enabled:
            warning_codes.append(f"{backend}_adapter_feature_flag_disabled")
    for backend in attempted_order:
        adapter = adapter_by_backend.get(backend)
        if adapter is not None and adapter.warning_code:
            warning_codes.append(f"{backend}_{adapter.warning_code}")
    return tuple(dict.fromkeys(warning_codes))


def _normalized_source_kinds(source_kinds: Sequence[object] | None) -> tuple[str, ...]:
    raw_kinds = source_kinds if source_kinds else SOURCE_ROUTE_KINDS
    return tuple(dict.fromkeys(normalize_source_kind(source_kind) for source_kind in raw_kinds))


def _adapter_readiness_score(
    adapter: ParserAdapterRuntimeStatus,
) -> tuple[float, bool, list[str]]:
    if adapter.status == "active":
        return 64.0, True, ["adapter_active"]
    if adapter.status == "available":
        return 48.0, False, ["adapter_installed_but_not_enabled"]
    if adapter.status == "ignored":
        return 42.0, False, ["adapter_flag_ignored_by_backend"]
    if adapter.status == "missing":
        return 18.0, False, ["adapter_package_missing"]
    if adapter.status == "disabled":
        return 24.0, False, ["adapter_disabled"]
    return 20.0, False, ["adapter_not_ready"]


def _score_status(status: str) -> ParserAdapterScoreStatus:
    if status == "available":
        return "available"
    if status == "disabled":
        return "disabled"
    if status == "ignored":
        return "ignored"
    if status == "missing":
        return "missing"
    return "eligible"


def _with_rank_and_recommendation(
    entry: ParserAdapterScorecardEntry,
    *,
    rank: int,
    recommended_backend: ParserAdapterScoreBackend,
) -> ParserAdapterScorecardEntry:
    recommended = entry.backend == recommended_backend
    status: ParserAdapterScoreStatus = "recommended" if recommended else entry.status
    return ParserAdapterScorecardEntry(
        backend=entry.backend,
        rank=rank,
        score=entry.score,
        status=status,
        recommended=recommended,
        executable=entry.executable,
        selected=entry.selected,
        enabled=entry.enabled,
        installed=entry.installed,
        metric_source=entry.metric_source,
        metric_count=entry.metric_count,
        signals=entry.signals,
        reason_codes=entry.reason_codes,
        warning_codes=entry.warning_codes,
    )


def _recommended_backend(
    entries: list[ParserAdapterScorecardEntry],
) -> ParserAdapterScoreBackend:
    executable_entries = [
        entry
        for entry in entries
        if entry.executable and INCOMPLETE_ADAPTER_EVIDENCE_WARNING not in set(entry.warning_codes)
    ]
    if not executable_entries:
        return "local"
    return max(executable_entries, key=lambda entry: entry.score).backend


def _adapter_metric_evidence_complete(signals: Mapping[str, float]) -> bool:
    """adapter 推奨に必要な中核 downstream 証拠が揃っているか。"""
    return set(signals) >= CORE_ADAPTER_EVIDENCE_METRICS


def _infer_metrics_backend(
    runtime: ParserAdapterRuntimeSettings,
) -> ParserAdapterScoreBackend:
    if runtime.adapter_backend == "docling":
        return "docling"
    if runtime.adapter_backend == "unstructured":
        return "unstructured"
    return "local"


def _normalized_metric_signals(metrics: Mapping[str, float]) -> dict[str, float]:
    signals: dict[str, float] = {}
    for metric, spec in METRIC_SPECS.items():
        value = metrics.get(metric)
        if value is None or not math.isfinite(value):
            continue
        signals[metric] = _normalize_metric(value, spec.direction)
    return signals


def _normalize_metric(value: float, direction: MetricDirection) -> float:
    if direction == "higher":
        return _clamp_ratio(value)
    if direction == "lower":
        return 1.0 - _clamp_ratio(value)
    if value <= LATENCY_FULL_CREDIT_MS:
        return 1.0
    if value >= LATENCY_ZERO_CREDIT_MS:
        return 0.0
    return 1.0 - (
        (value - LATENCY_FULL_CREDIT_MS) / (LATENCY_ZERO_CREDIT_MS - LATENCY_FULL_CREDIT_MS)
    )


def _metric_score_adjustment(signals: Mapping[str, float]) -> float:
    total_weight = sum(METRIC_SPECS[metric].weight for metric in signals)
    if total_weight <= 0.0:
        return 0.0
    weighted = sum(METRIC_SPECS[metric].weight * value for metric, value in signals.items())
    quality = weighted / total_weight
    return METRIC_SCORE_DELTA_POINTS * ((quality - 0.5) * 2.0)


def _clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _round_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)
