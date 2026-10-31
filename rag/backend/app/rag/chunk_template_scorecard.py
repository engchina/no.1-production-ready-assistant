"""Chunk template の評価 scorecard。

Adaptive Chunking / AutoRAGTuner の考え方を、現行の file-processing
golden/staging 指標へ再マップする。ここでは chunking 実装を切り替えず、
template ごとの健康度と promotion blocker を機械可読に返す。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

MetricDirection = Literal["higher", "lower", "latency"]
ChunkTemplateStatus = Literal["recommended", "healthy", "watch", "blocked", "unmeasured"]


@dataclass(frozen=True)
class ChunkTemplateMetricSpec:
    """chunk template 評価に使う指標の方向と重み。"""

    direction: MetricDirection
    weight: float


@dataclass(frozen=True)
class ChunkTemplateProfile:
    """template ごとの主な適用場面と評価指標。"""

    template: str
    use_case: str
    metric_weights: Mapping[str, float]


@dataclass(frozen=True)
class ChunkTemplateScorecardEntry:
    """1 chunk template の評価結果。"""

    template: str
    rank: int
    score: float | None
    status: ChunkTemplateStatus
    promotion_blocking: bool
    metric_source: str
    metric_count: int
    use_case: str
    signals: Mapping[str, float] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    expected_case_count: int = 0
    measured_case_count: int = 0
    expected_source_kinds: tuple[str, ...] = ()
    covered_source_kinds: tuple[str, ...] = ()
    missing_source_kinds: tuple[str, ...] = ()
    expected_scenarios: tuple[str, ...] = ()
    covered_scenarios: tuple[str, ...] = ()
    missing_scenarios: tuple[str, ...] = ()
    observed_chunk_templates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkTemplateScorecard:
    """chunk template portfolio の評価結果。"""

    metrics_source: str
    observed_templates: tuple[str, ...]
    recommended_template: str | None
    promotion_blocking: bool
    entries: tuple[ChunkTemplateScorecardEntry, ...]


METRIC_SPECS: Mapping[str, ChunkTemplateMetricSpec] = {
    "chunk_block_integrity": ChunkTemplateMetricSpec("higher", 10.0),
    "chunk_contextual_coherence": ChunkTemplateMetricSpec("higher", 10.0),
    "chunk_size_compliance": ChunkTemplateMetricSpec("higher", 8.0),
    "element_lineage_coverage": ChunkTemplateMetricSpec("higher", 8.0),
    "visual_chunk_metadata_completeness": ChunkTemplateMetricSpec("higher", 6.0),
    "citation_traceability_coverage": ChunkTemplateMetricSpec("higher", 6.0),
    "preview_addressability_coverage": ChunkTemplateMetricSpec("higher", 5.0),
    "bbox_citation_coverage": ChunkTemplateMetricSpec("higher", 5.0),
    "bbox_coordinate_validity_coverage": ChunkTemplateMetricSpec("higher", 5.0),
    "structural_section_coverage": ChunkTemplateMetricSpec("higher", 7.0),
    "dependency_context_recall": ChunkTemplateMetricSpec("higher", 7.0),
    "table_structure_fidelity": ChunkTemplateMetricSpec("higher", 8.0),
    "table_row_tree_fidelity": ChunkTemplateMetricSpec("higher", 8.0),
    "cross_page_table_continuity_coverage": ChunkTemplateMetricSpec("higher", 8.0),
    "table_qa_accuracy": ChunkTemplateMetricSpec("higher", 8.0),
    "page_hit_accuracy": ChunkTemplateMetricSpec("higher", 6.0),
    "retrieval_recall": ChunkTemplateMetricSpec("higher", 6.0),
    "groundedness": ChunkTemplateMetricSpec("higher", 5.0),
    "failed_segment_rate": ChunkTemplateMetricSpec("lower", 5.0),
    "parser_fallback_rate": ChunkTemplateMetricSpec("lower", 4.0),
    "ingestion_p95_ms": ChunkTemplateMetricSpec("latency", 3.0),
}
CORE_CHUNK_METRICS = frozenset(
    {"chunk_block_integrity", "chunk_contextual_coherence", "chunk_size_compliance"}
)
PROMOTION_MIN_SCORE = 80.0
WATCH_MIN_SCORE = 90.0
LATENCY_FULL_CREDIT_MS = 5_000.0
LATENCY_ZERO_CREDIT_MS = 60_000.0


TEMPLATE_PROFILES: Mapping[str, ChunkTemplateProfile] = {
    "pdf_layout": ChunkTemplateProfile(
        template="pdf_layout",
        use_case="PDF layout / page-aware text, figure, table chunks",
        metric_weights={
            "chunk_block_integrity": 10.0,
            "chunk_contextual_coherence": 9.0,
            "chunk_size_compliance": 7.0,
            "element_lineage_coverage": 8.0,
            "preview_addressability_coverage": 7.0,
            "bbox_citation_coverage": 7.0,
            "page_hit_accuracy": 8.0,
            "retrieval_recall": 7.0,
        },
    ),
    "ocr_page": ChunkTemplateProfile(
        template="ocr_page",
        use_case="Scanned image / OCR page chunks with bbox citations",
        metric_weights={
            "chunk_block_integrity": 8.0,
            "chunk_contextual_coherence": 8.0,
            "chunk_size_compliance": 7.0,
            "preview_addressability_coverage": 9.0,
            "bbox_coordinate_validity_coverage": 9.0,
            "bbox_citation_coverage": 9.0,
            "page_hit_accuracy": 7.0,
        },
    ),
    "office_document": ChunkTemplateProfile(
        template="office_document",
        use_case="DOCX sections and paragraph/list blocks",
        metric_weights={
            "chunk_block_integrity": 10.0,
            "chunk_contextual_coherence": 10.0,
            "chunk_size_compliance": 8.0,
            "structural_section_coverage": 9.0,
            "element_lineage_coverage": 8.0,
            "retrieval_recall": 6.0,
        },
    ),
    "office_slide": ChunkTemplateProfile(
        template="office_slide",
        use_case="PPTX slide-bounded chunks",
        metric_weights={
            "chunk_block_integrity": 9.0,
            "chunk_contextual_coherence": 9.0,
            "chunk_size_compliance": 7.0,
            "element_lineage_coverage": 8.0,
            "page_hit_accuracy": 8.0,
            "citation_traceability_coverage": 6.0,
        },
    ),
    "office_sheet": ChunkTemplateProfile(
        template="office_sheet",
        use_case="XLSX sheet and table row group chunks",
        metric_weights={
            "chunk_block_integrity": 9.0,
            "chunk_contextual_coherence": 9.0,
            "chunk_size_compliance": 8.0,
            "table_structure_fidelity": 10.0,
            "table_row_tree_fidelity": 10.0,
            "table_qa_accuracy": 9.0,
        },
    ),
    "table_preserve_rows": ChunkTemplateProfile(
        template="table_preserve_rows",
        use_case="Long table chunks preserving row groups and headers",
        metric_weights={
            "chunk_block_integrity": 10.0,
            "chunk_contextual_coherence": 10.0,
            "chunk_size_compliance": 9.0,
            "table_structure_fidelity": 10.0,
            "table_row_tree_fidelity": 10.0,
            "cross_page_table_continuity_coverage": 9.0,
            "table_qa_accuracy": 9.0,
        },
    ),
    "markdown_by_heading": ChunkTemplateProfile(
        template="markdown_by_heading",
        use_case="Markdown heading, code, equation aware chunks",
        metric_weights={
            "chunk_block_integrity": 10.0,
            "chunk_contextual_coherence": 9.0,
            "chunk_size_compliance": 8.0,
            "structural_section_coverage": 8.0,
            "element_lineage_coverage": 8.0,
            "retrieval_recall": 6.0,
        },
    ),
    "html_semantic": ChunkTemplateProfile(
        template="html_semantic",
        use_case="HTML semantic section and dependency chunks",
        metric_weights={
            "chunk_block_integrity": 9.0,
            "chunk_contextual_coherence": 9.0,
            "chunk_size_compliance": 8.0,
            "structural_section_coverage": 10.0,
            "dependency_context_recall": 10.0,
            "element_lineage_coverage": 8.0,
        },
    ),
    "email_thread": ChunkTemplateProfile(
        template="email_thread",
        use_case="Email header, body, thread and attachment metadata chunks",
        metric_weights={
            "chunk_block_integrity": 9.0,
            "chunk_contextual_coherence": 10.0,
            "chunk_size_compliance": 8.0,
            "element_lineage_coverage": 7.0,
            "citation_traceability_coverage": 6.0,
        },
    ),
    "text_blocks": ChunkTemplateProfile(
        template="text_blocks",
        use_case="Plain text blocks with inferred sections",
        metric_weights={
            "chunk_block_integrity": 8.0,
            "chunk_contextual_coherence": 8.0,
            "chunk_size_compliance": 8.0,
            "element_lineage_coverage": 6.0,
            "retrieval_recall": 6.0,
        },
    ),
    "enterprise_ai_fallback": ChunkTemplateProfile(
        template="enterprise_ai_fallback",
        use_case="Enterprise AI fallback chunks for unknown layouts",
        metric_weights={
            "chunk_block_integrity": 8.0,
            "chunk_contextual_coherence": 8.0,
            "chunk_size_compliance": 8.0,
            "element_lineage_coverage": 7.0,
            "parser_fallback_rate": 8.0,
            "retrieval_recall": 6.0,
        },
    ),
}


def build_chunk_template_scorecard(
    *,
    metrics: Mapping[str, float],
    observed_templates: Sequence[str],
    metrics_source: str,
    template_evidence: Mapping[str, Mapping[str, object]] | None = None,
) -> ChunkTemplateScorecard:
    """observed chunk template と metrics から評価 scorecard を作る。"""
    templates = tuple(dict.fromkeys(template for template in observed_templates if template))
    entries = tuple(
        _ranked_entries(
            [
                _entry_for_template(
                    template,
                    metrics=metrics,
                    metrics_source=metrics_source,
                    evidence=(
                        template_evidence.get(template) if template_evidence is not None else None
                    ),
                )
                for template in templates
            ]
        )
    )
    recommended = next(
        (entry.template for entry in entries if entry.status == "recommended"),
        None,
    )
    return ChunkTemplateScorecard(
        metrics_source=metrics_source,
        observed_templates=templates,
        recommended_template=recommended,
        promotion_blocking=any(entry.promotion_blocking for entry in entries),
        entries=entries,
    )


def _ranked_entries(
    entries: Sequence[ChunkTemplateScorecardEntry],
) -> tuple[ChunkTemplateScorecardEntry, ...]:
    sorted_entries = sorted(
        entries,
        key=lambda entry: -1.0 if entry.score is None else entry.score,
        reverse=True,
    )
    ranked: list[ChunkTemplateScorecardEntry] = []
    first_recommendable = next(
        (entry.template for entry in sorted_entries if not entry.promotion_blocking),
        None,
    )
    for index, entry in enumerate(sorted_entries):
        recommended = entry.template == first_recommendable and entry.score is not None
        status: ChunkTemplateStatus = "recommended" if recommended else entry.status
        ranked.append(
            ChunkTemplateScorecardEntry(
                template=entry.template,
                rank=index + 1,
                score=entry.score,
                status=status,
                promotion_blocking=entry.promotion_blocking,
                metric_source=entry.metric_source,
                metric_count=entry.metric_count,
                use_case=entry.use_case,
                signals=entry.signals,
                reason_codes=entry.reason_codes,
                expected_case_count=entry.expected_case_count,
                measured_case_count=entry.measured_case_count,
                expected_source_kinds=entry.expected_source_kinds,
                covered_source_kinds=entry.covered_source_kinds,
                missing_source_kinds=entry.missing_source_kinds,
                expected_scenarios=entry.expected_scenarios,
                covered_scenarios=entry.covered_scenarios,
                missing_scenarios=entry.missing_scenarios,
                observed_chunk_templates=entry.observed_chunk_templates,
            )
        )
    return tuple(ranked)


def _entry_for_template(
    template: str,
    *,
    metrics: Mapping[str, float],
    metrics_source: str,
    evidence: Mapping[str, object] | None = None,
) -> ChunkTemplateScorecardEntry:
    profile = TEMPLATE_PROFILES.get(template) or _generic_profile(template)
    signals = _signals_for_profile(profile, metrics)
    metric_count = len(signals)
    core_metric_count = len(CORE_CHUNK_METRICS & set(signals))
    evidence_summary = _template_evidence_summary(evidence)
    evidence_reason_codes = _template_evidence_reason_codes(evidence_summary)
    evidence_blocking = bool(evidence_reason_codes)
    if metric_count == 0 or core_metric_count == 0:
        return ChunkTemplateScorecardEntry(
            template=template,
            rank=0,
            score=None,
            status="blocked" if evidence_blocking else "unmeasured",
            promotion_blocking=evidence_blocking,
            metric_source="none",
            metric_count=metric_count,
            use_case=profile.use_case,
            signals=signals,
            reason_codes=("chunk_template_metrics_missing", *evidence_reason_codes),
            **evidence_summary,
        )
    score = _template_score(profile, signals)
    promotion_blocking = score < PROMOTION_MIN_SCORE or evidence_blocking
    status: ChunkTemplateStatus
    if promotion_blocking:
        status = "blocked"
    elif score < WATCH_MIN_SCORE:
        status = "watch"
    else:
        status = "healthy"
    reason_codes = ["adaptive_chunking_metrics_applied"]
    if promotion_blocking:
        if score < PROMOTION_MIN_SCORE:
            reason_codes.append("chunk_template_score_below_promotion_threshold")
    elif status == "watch":
        reason_codes.append("chunk_template_score_watch")
    reason_codes.extend(evidence_reason_codes)
    return ChunkTemplateScorecardEntry(
        template=template,
        rank=0,
        score=round(score, 2),
        status=status,
        promotion_blocking=promotion_blocking,
        metric_source=metrics_source,
        metric_count=metric_count,
        use_case=profile.use_case,
        signals=dict(sorted(signals.items())),
        reason_codes=tuple(reason_codes),
        **evidence_summary,
    )


def _template_evidence_summary(
    evidence: Mapping[str, object] | None,
) -> dict[str, Any]:
    if not evidence:
        return {
            "expected_case_count": 0,
            "measured_case_count": 0,
            "expected_source_kinds": (),
            "covered_source_kinds": (),
            "missing_source_kinds": (),
            "expected_scenarios": (),
            "covered_scenarios": (),
            "missing_scenarios": (),
            "observed_chunk_templates": (),
        }
    expected_source_kinds = _string_tuple(evidence.get("expected_source_kinds"))
    covered_source_kinds = _string_tuple(evidence.get("covered_source_kinds"))
    expected_scenarios = _string_tuple(evidence.get("expected_scenarios"))
    covered_scenarios = _string_tuple(evidence.get("covered_scenarios"))
    return {
        "expected_case_count": _int_value(evidence.get("expected_case_count")),
        "measured_case_count": _int_value(evidence.get("measured_case_count")),
        "expected_source_kinds": expected_source_kinds,
        "covered_source_kinds": covered_source_kinds,
        "missing_source_kinds": tuple(
            source_kind
            for source_kind in expected_source_kinds
            if source_kind not in covered_source_kinds
        ),
        "expected_scenarios": expected_scenarios,
        "covered_scenarios": covered_scenarios,
        "missing_scenarios": tuple(
            scenario for scenario in expected_scenarios if scenario not in covered_scenarios
        ),
        "observed_chunk_templates": _string_tuple(evidence.get("observed_chunk_templates")),
    }


def _template_evidence_reason_codes(
    evidence: Mapping[str, object],
) -> tuple[str, ...]:
    reason_codes: list[str] = []
    expected_case_count = _int_value(evidence.get("expected_case_count"))
    measured_case_count = _int_value(evidence.get("measured_case_count"))
    if expected_case_count > measured_case_count:
        reason_codes.append("chunk_template_case_evidence_missing")
    if _string_tuple(evidence.get("missing_source_kinds")):
        reason_codes.append("chunk_template_source_kind_evidence_missing")
    if _string_tuple(evidence.get("missing_scenarios")):
        reason_codes.append("chunk_template_scenario_evidence_missing")
    return tuple(reason_codes)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    return 0


def _signals_for_profile(
    profile: ChunkTemplateProfile,
    metrics: Mapping[str, float],
) -> dict[str, float]:
    signals: dict[str, float] = {}
    for metric in profile.metric_weights:
        spec = METRIC_SPECS.get(metric)
        value = metrics.get(metric)
        if spec is None or value is None or not math.isfinite(value):
            continue
        signals[metric] = _normalize_metric(value, spec.direction)
    return signals


def _template_score(
    profile: ChunkTemplateProfile,
    signals: Mapping[str, float],
) -> float:
    weighted = 0.0
    total = 0.0
    for metric, value in signals.items():
        weight = profile.metric_weights.get(metric) or METRIC_SPECS[metric].weight
        weighted += weight * value
        total += weight
    if total <= 0.0:
        return 0.0
    return 100.0 * (weighted / total)


def _generic_profile(template: str) -> ChunkTemplateProfile:
    return ChunkTemplateProfile(
        template=template,
        use_case="Custom or fallback chunk template",
        metric_weights={
            "chunk_block_integrity": 8.0,
            "chunk_contextual_coherence": 8.0,
            "chunk_size_compliance": 8.0,
            "element_lineage_coverage": 6.0,
            "retrieval_recall": 6.0,
        },
    )


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


def _clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
