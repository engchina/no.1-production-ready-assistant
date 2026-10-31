"""低カーディナリティの Ontology query observability helpers。"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)

ONTOLOGY_SESSION_TRANSITIONS = Counter(
    "nl2sql_ontology_session_transitions_total",
    "Ontology query session state transitions.",
    ("state",),
)
ONTOLOGY_VALIDATION_FINDINGS = Counter(
    "nl2sql_ontology_validation_findings_total",
    "Ontology validation findings by severity and code.",
    ("severity", "code"),
)
ONTOLOGY_STAGE_SECONDS = Histogram(
    "nl2sql_ontology_stage_duration_seconds",
    "Ontology query stage duration.",
    ("stage",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
ONTOLOGY_JOBS = Counter(
    "nl2sql_ontology_jobs_total",
    "Ontology build and publish job outcomes.",
    ("job_type", "status", "error_code"),
)
ONTOLOGY_SOURCE_EXTRACTIONS = Counter(
    "nl2sql_ontology_source_extractions_total",
    "Ontology source extraction outcomes by controlled file format.",
    ("format", "status"),
)
ONTOLOGY_SHACL_VALIDATIONS = Counter(
    "nl2sql_ontology_shacl_validations_total",
    "SHACL Core validation outcomes and controlled result codes.",
    ("conforms", "code"),
)
ONTOLOGY_REASONING_TRIPLES = Histogram(
    "nl2sql_ontology_reasoning_triples",
    "Number of triples in an OWL 2 RL materialized closure.",
    buckets=(10, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 50_000),
)
ONTOLOGY_PROFILE_RECOMMENDATIONS = Counter(
    "nl2sql_ontology_profile_recommendations_total",
    "Ontology profile recommendation outcomes.",
    ("outcome",),
)
ONTOLOGY_CONTEXT_HITS = Histogram(
    "nl2sql_ontology_context_hits",
    "Number of nodes returned by bounded ontology context retrieval.",
    buckets=(0, 1, 2, 4, 8, 12, 16, 24),
)
ONTOLOGY_CLARIFICATION_EVENTS = Counter(
    "nl2sql_ontology_clarification_events_total",
    "Guided clarification lifecycle events by controlled outcome and category.",
    ("outcome", "category"),
)
ONTOLOGY_CLARIFICATION_TURNS = Histogram(
    "nl2sql_ontology_clarification_turns",
    "Number of guided clarification turns when the flow reaches a terminal action.",
    buckets=(0, 1, 2, 3, 4, 5, 8, 12),
)

_JOB_TYPES = frozenset({"build", "publish", "profile_sync"})
_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_SOURCE_FORMATS = frozenset({"pdf", "docx", "txt", "md", "csv", "xlsx", "xls", "xlsm"})
_SOURCE_STATUSES = frozenset({"extracted", "failed", "duplicate"})
_RECOMMENDATION_OUTCOMES = frozenset(
    {"with_candidates", "no_candidates", "accepted", "manually_changed"}
)
_CLARIFICATION_OUTCOMES = frozenset({"started", "answered", "ready", "cancelled", "expired"})
_CLARIFICATION_CATEGORIES = frozenset(
    {"business_meaning", "relationship_path", "filter_value", "time_range", "granularity", "output"}
)


def record_transition(*, session_id: str, revision_id: str, state: str) -> None:
    """状態遷移を記録する。質問や filter 値はログへ含めない。"""

    ONTOLOGY_SESSION_TRANSITIONS.labels(state=state).inc()
    logger.info(
        "NL2SQL Ontology session transitioned",
        extra={
            "session_id": session_id,
            "ontology_revision_id": revision_id,
            "ontology_state": state,
        },
    )


def record_findings(findings: list[object]) -> None:
    for finding in findings:
        severity = str(getattr(finding, "severity", "unknown"))
        code = str(getattr(finding, "code", "unknown"))[:64]
        ONTOLOGY_VALIDATION_FINDINGS.labels(severity=severity, code=code).inc()


def record_job(*, job_type: str, status: str, error_code: str = "none") -> None:
    """永続 job の終端結果だけを、制御済み label で記録する。"""

    normalized_type = job_type if job_type in _JOB_TYPES else "unknown"
    normalized_status = status if status in _JOB_STATUSES else "unknown"
    normalized_code = error_code[:64] if error_code else "none"
    ONTOLOGY_JOBS.labels(
        job_type=normalized_type,
        status=normalized_status,
        error_code=normalized_code,
    ).inc()


def record_source_extraction(*, file_format: str, status: str) -> None:
    normalized_format = file_format.lower().lstrip(".")
    if normalized_format not in _SOURCE_FORMATS:
        normalized_format = "unknown"
    normalized_status = status if status in _SOURCE_STATUSES else "unknown"
    ONTOLOGY_SOURCE_EXTRACTIONS.labels(
        format=normalized_format,
        status=normalized_status,
    ).inc()


def record_shacl_validation(*, conforms: bool) -> None:
    ONTOLOGY_SHACL_VALIDATIONS.labels(
        conforms=str(conforms).lower(),
        code="none" if conforms else "ONTOLOGY_SHACL_VIOLATION",
    ).inc()


def record_reasoning_triples(count: int) -> None:
    ONTOLOGY_REASONING_TRIPLES.observe(max(0, count))


def record_profile_recommendation(outcome: str) -> None:
    normalized = outcome if outcome in _RECOMMENDATION_OUTCOMES else "unknown"
    ONTOLOGY_PROFILE_RECOMMENDATIONS.labels(outcome=normalized).inc()


def record_context_hits(count: int) -> None:
    ONTOLOGY_CONTEXT_HITS.observe(max(0, count))


def record_clarification(*, outcome: str, category: str = "none", turns: int | None = None) -> None:
    """質問本文・回答値を含めず、制御済み label の lifecycle だけを記録する。"""

    normalized_outcome = outcome if outcome in _CLARIFICATION_OUTCOMES else "unknown"
    normalized_category = category if category in _CLARIFICATION_CATEGORIES else "none"
    ONTOLOGY_CLARIFICATION_EVENTS.labels(
        outcome=normalized_outcome,
        category=normalized_category,
    ).inc()
    if turns is not None:
        ONTOLOGY_CLARIFICATION_TURNS.observe(max(0, turns))


@contextmanager
def observe_stage(stage: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        ONTOLOGY_STAGE_SECONDS.labels(stage=stage).observe(time.monotonic() - started)
