"""Incremental NL2SQL state の低 cardinality Prometheus metrics。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram

PROCESS_STARTED_AT = time.monotonic()

STARTUP_READY_SECONDS = Histogram(
    "nl2sql_startup_ready_duration_seconds",
    "Process startから最初の successful DB readiness までの時間。",
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30),
)
REPOSITORY_SQL = Counter(
    "nl2sql_repository_sql_statements_total",
    "Incremental repository が実行した SQL statement 数。",
    ("operation",),
)
REPOSITORY_ROWS = Histogram(
    "nl2sql_repository_rows",
    "Incremental repository operation が読み書きした row 数。",
    ("operation",),
    buckets=(0, 1, 2, 5, 10, 50, 100, 500, 1_000, 10_000),
)
REPOSITORY_CLOB_BYTES = Counter(
    "nl2sql_repository_clob_bytes_total",
    "Incremental repository が読み込んだ JSON CLOB byte 数。",
    ("collection",),
)
CACHE_REQUESTS = Counter(
    "nl2sql_incremental_cache_requests_total",
    "Cache-aside lookup result。",
    ("cache", "outcome"),
)
CHANGE_TOKEN_LAG = Gauge(
    "nl2sql_change_token_lag",
    "前回観測した change token との差。",
    ("namespace",),
)
SCHEMA_CHANGED_OBJECTS = Histogram(
    "nl2sql_schema_refresh_changed_objects",
    "Schema refresh で metadata を再取得した object 数。",
    buckets=(0, 1, 5, 10, 50, 100, 500, 1_000, 10_000),
)
SCHEMA_REFRESH_SECONDS = Histogram(
    "nl2sql_schema_refresh_duration_seconds",
    "Persistent schema refresh job duration。",
    ("status",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, 900),
)
SCHEMA_REFRESH_PENDING_AGE_SECONDS = Gauge(
    "nl2sql_schema_refresh_pending_age_seconds",
    "最古の pending Schema refresh job の経過秒数。",
)
SCHEMA_REFRESH_PHASE_SECONDS = Histogram(
    "nl2sql_schema_refresh_phase_duration_seconds",
    "Schema refresh の固定 phase 別処理時間。",
    ("phase",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
SCHEMA_REFRESH_ERRORS = Counter(
    "nl2sql_schema_refresh_errors_total",
    "Schema refresh job の安定 error code 別失敗数。",
    ("error_code",),
)
PERSISTENCE_FAILURES = Counter(
    "nl2sql_persistence_failures_total",
    "Incremental repository failure の operation / 分類別件数。",
    ("operation", "category"),
)
PERSISTENCE_RECOVERIES = Counter(
    "nl2sql_persistence_recoveries_total",
    "Persistence circuit の回復試行結果。",
    ("outcome",),
)
PERSISTENCE_CIRCUIT_STATE = Gauge(
    "nl2sql_persistence_circuit_state",
    "Persistence circuit state。closed=0, open=1, half_open=2。",
)

_ready_lock = threading.Lock()
_ready_recorded = False


def record_ready_once() -> None:
    global _ready_recorded  # noqa: PLW0603 - process-wide one-shot metric
    with _ready_lock:
        if _ready_recorded:
            return
        STARTUP_READY_SECONDS.observe(time.monotonic() - PROCESS_STARTED_AT)
        _ready_recorded = True


def record_repository(
    operation: str,
    *,
    statements: int = 1,
    rows: int = 0,
    clob_collection: str = "",
    clob_bytes: int = 0,
) -> None:
    REPOSITORY_SQL.labels(operation=operation).inc(max(0, statements))
    REPOSITORY_ROWS.labels(operation=operation).observe(max(0, rows))
    if clob_collection and clob_bytes:
        REPOSITORY_CLOB_BYTES.labels(collection=clob_collection).inc(max(0, clob_bytes))


def record_cache(cache: str, outcome: str) -> None:
    CACHE_REQUESTS.labels(cache=cache, outcome=outcome).inc()


def record_token_lag(namespace: str, lag: int) -> None:
    CHANGE_TOKEN_LAG.labels(namespace=namespace).set(max(0, lag))


def record_persistence_failure(operation: str, category: str) -> None:
    PERSISTENCE_FAILURES.labels(operation=operation, category=category).inc()


def record_persistence_recovery(outcome: str) -> None:
    PERSISTENCE_RECOVERIES.labels(outcome=outcome).inc()


def set_persistence_circuit_state(state: str) -> None:
    PERSISTENCE_CIRCUIT_STATE.set({"closed": 0, "open": 1, "half_open": 2}.get(state, 1))


@contextmanager
def observe_schema_refresh() -> Iterator[dict[str, str]]:
    started = time.monotonic()
    state = {"status": "error"}
    try:
        yield state
    finally:
        SCHEMA_REFRESH_SECONDS.labels(status=state["status"]).observe(time.monotonic() - started)
