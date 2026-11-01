"""Agent Runtime の観測性ヘルパー。

Prometheus は依存に含まれているため即時に公開し、Langfuse / OpenTelemetry は
Secret 設定の有無を状態として露出する。SDK exporter は本番接続情報が揃った段階で差し込む。
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Event, Lock, Thread
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
from pr_backend_core.internal_http import http_client_options
from pr_backend_core.observability.tracing import current_span_fields
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

JsonObject = dict[str, Any]
TRACE_EVENTS: deque[TraceEvent] = deque()
TRACE_EVENTS_LOCK = Lock()
TRACE_EXPORT_STATUS_LOCK = Lock()
TRACE_POLICY_LOCK = Lock()
TRACE_EXPORT_RETRY_QUEUE: deque[TraceExportRetryItem] = deque()
TRACE_EXPORT_RETRY_QUEUE_LOCK = Lock()
TRACE_EXPORT_RETRY_WORKER: Thread | None = None
TRACE_EXPORT_RETRY_STOP: Event | None = None

TRACE_DELIVERIES = Counter(
    "agent_trace_deliveries_total",
    "Trace event delivery outcomes.",
    ("outcome",),
)

HTTP_REQUESTS = Counter(
    "agent_http_requests_total",
    "HTTP request count.",
    ("method", "path", "status"),
)
HTTP_LATENCY = Histogram(
    "agent_http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "path"),
)
RUNTIME_EVENTS = Counter(
    "agent_runtime_events_total",
    "Agent runtime event count.",
    ("event_type",),
)
RUNS_TOTAL = Counter(
    "agent_runs_total",
    "Agent run terminal status count.",
    ("status",),
)
TOOL_CALLS_TOTAL = Counter(
    "agent_tool_calls_total",
    "Agent tool call result count.",
    ("tool", "status"),
)
APPROVALS_TOTAL = Counter(
    "agent_approvals_total",
    "Agent approval decision count.",
    ("tool", "decision"),
)
GUARDRAIL_WARNINGS_TOTAL = Counter(
    "agent_guardrail_warnings_total",
    "Agent guardrail warning count.",
    ("code",),
)
# 使った出力が上限よりはるかに少ない未完了の応答を、呼び先の一時的な不具合として送り直した回数
# （#2355。組み込み Runtime の `_ModelApiResponses.create`）。
MODEL_INCOMPLETE_RETRIES_TOTAL = Counter(
    "agent_model_incomplete_retries_total",
    "Agent model call resent after an incomplete response with little output.",
    ("model",),
)
# モデルの入力の上限に収めるためにツールの結果を縮めた・収まらずに失敗した回数（#2383。組み込み
# Runtime の `context_window`）。trigger は budget（呼び出しの前の見積もり）/ provider_limit
# （プロバイダの入力の超過の後の送り直し）、outcome は compacted / failed。
CONTEXT_COMPACTIONS_TOTAL = Counter(
    "agent_context_compactions_total",
    "Agent model input compacted to fit the model input limit.",
    ("model", "trigger", "outcome"),
)
# 進展の無いツールの呼び出し（同じ呼び出しの反復・実行の失敗の連続・引数の誤りの連続）で Run を
# 終えた回数（#2580。組み込み Runtime の `tool_loop_guard`）。
TOOL_LOOP_STOPS_TOTAL = Counter(
    "agent_tool_loop_stops_total",
    "Agent runs stopped after repeated tool calls without progress.",
    ("reason",),
)
# モデルに渡していない名前のツールの呼び出し（宣言の閉包の外の呼び出し）の数（#3240。組み込み
# Runtime の `record_undeclared_call`）。reason は `builtin_runtime.UNDECLARED_CALL_REASONS`。
UNDECLARED_TOOL_CALLS_TOTAL = Counter(
    "agent_undeclared_tool_calls_total",
    "Agent tool calls to names not exposed to the run (not executed).",
    ("reason",),
)
ARTIFACTS_TOTAL = Counter(
    "agent_artifacts_total",
    "Agent artifact count.",
    ("kind",),
)


class TraceExporterStatus(BaseModel):
    configured: bool = False
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    retry_queue_size: int = 0
    retry_queue_max_size: int = 0
    retry_max_attempts: int = 0
    retry_worker_enabled: bool = False
    retry_worker_running: bool = False
    retry_worker_interval_seconds: float = 0.0


class ObservabilityStatus(BaseModel):
    metrics_enabled: bool
    prometheus_metrics_path: str
    trace_events_enabled: bool
    trace_events_buffer_size: int
    trace_events_retention_seconds: int
    trace_sample_rate: float
    trace_exporter_configured: bool
    trace_exporter_last_success_at: datetime | None = None
    trace_exporter_last_error: str | None = None
    retry_queue_size: int = 0
    retry_queue_max_size: int = 0
    retry_max_attempts: int = 0
    retry_worker_enabled: bool = False
    retry_worker_running: bool = False
    retry_worker_interval_seconds: float = 0.0
    langfuse_configured: bool
    opentelemetry_configured: bool


class TraceEvent(BaseModel):
    id: str
    event_type: str
    run_id: str | None = None
    step_id: str | None = None
    tool_name: str | None = None
    trace_id: str | None = None
    attributes: JsonObject
    created_at: datetime
    telemetry_trace_id: str | None = None
    telemetry_span_id: str | None = None
    sampled: bool = True


class TraceEventsData(BaseModel):
    total: int
    events: list[TraceEvent]


class TraceExportRetryItem(BaseModel):
    event: TraceEvent
    attempts: int = 1
    last_error: str
    queued_at: datetime
    next_retry_at: datetime


class TraceExportRetryData(BaseModel):
    attempted: int = 0
    succeeded: int = 0
    requeued: int = 0
    dropped: int = 0
    skipped: int = 0
    queue_size: int = 0


class TracePolicySettings(BaseModel):
    trace_events_enabled: bool = True
    trace_events_buffer_size: int = 500
    trace_events_retention_seconds: int = 86_400
    trace_sample_rate: float = 1.0


class TracePolicyPatch(BaseModel):
    trace_events_enabled: bool | None = None
    trace_events_buffer_size: int | None = None
    trace_events_retention_seconds: int | None = None
    trace_sample_rate: float | None = None


TRACE_EXPORT_STATUS = TraceExporterStatus()
TRACE_POLICY_OVERRIDE = TracePolicyPatch()


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def request_path_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return "/unmatched"


def observe_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    status = str(status_code)
    HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
    HTTP_LATENCY.labels(method=method, path=path).observe(duration_seconds)


def record_runtime_event(event_type: str, payload: JsonObject) -> None:
    RUNTIME_EVENTS.labels(event_type=event_type).inc()
    if event_type == "run.completed":
        RUNS_TOTAL.labels(status="completed").inc()
    elif event_type == "run.cancelled":
        RUNS_TOTAL.labels(status="cancelled").inc()
    elif event_type == "runtime.failed":
        RUNS_TOTAL.labels(status="failed").inc()
    elif event_type == "tool.completed":
        TOOL_CALLS_TOTAL.labels(tool=_payload_text(payload, "tool_name"), status="success").inc()
    elif event_type == "tool.failed":
        TOOL_CALLS_TOTAL.labels(tool=_payload_text(payload, "tool_name"), status="failed").inc()
    elif event_type == "approval.decided":
        decision = "approved" if payload.get("approved") is True else "rejected"
        APPROVALS_TOTAL.labels(tool=_payload_text(payload, "tool_name"), decision=decision).inc()
    elif event_type == "tool.guardrail_warning":
        for warning in _payload_list(payload, "warnings"):
            GUARDRAIL_WARNINGS_TOTAL.labels(code=warning).inc()
    elif event_type == "artifact.created":
        ARTIFACTS_TOTAL.labels(kind=_payload_text(payload, "kind")).inc()
    _append_trace_event(event_type, payload)


def list_trace_events(
    *,
    event_type: str | None = None,
    run_id: str | None = None,
    tool_name: str | None = None,
    limit: int = 100,
) -> TraceEventsData:
    policy = get_trace_policy()
    with TRACE_EVENTS_LOCK:
        _prune_trace_events_locked(datetime.now(UTC), policy)
        events = list(TRACE_EVENTS)
    filtered = [
        event
        for event in reversed(events)
        if (event_type is None or event.event_type == event_type)
        and (run_id is None or event.run_id == run_id)
        and (tool_name is None or event.tool_name == tool_name)
    ]
    return TraceEventsData(total=len(filtered), events=filtered[:limit])


def get_trace_policy() -> TracePolicySettings:
    from app.settings import get_settings

    settings = get_settings()
    policy = TracePolicySettings(
        trace_events_enabled=settings.agent_trace_events_enabled,
        trace_events_buffer_size=settings.agent_trace_events_buffer_size,
        trace_events_retention_seconds=settings.agent_trace_events_retention_seconds,
        trace_sample_rate=settings.agent_trace_sample_rate,
    )
    with TRACE_POLICY_LOCK:
        override = TRACE_POLICY_OVERRIDE.model_dump()
    data = policy.model_dump()
    for key, value in override.items():
        if value is not None:
            data[key] = value
    return TracePolicySettings.model_validate(data)


def patch_trace_policy(patch: TracePolicyPatch) -> TracePolicySettings:
    if patch.trace_events_buffer_size is not None and patch.trace_events_buffer_size < 1:
        raise ValueError("trace_events_buffer_size must be greater than or equal to 1")
    if (
        patch.trace_events_retention_seconds is not None
        and patch.trace_events_retention_seconds < 0
    ):
        raise ValueError("trace_events_retention_seconds must be greater than or equal to 0")
    if patch.trace_sample_rate is not None and not 0.0 <= patch.trace_sample_rate <= 1.0:
        raise ValueError("trace_sample_rate must be between 0.0 and 1.0")
    with TRACE_POLICY_LOCK:
        for key, value in patch.model_dump().items():
            if value is not None:
                setattr(TRACE_POLICY_OVERRIDE, key, value)
    policy = get_trace_policy()
    with TRACE_EVENTS_LOCK:
        _prune_trace_events_locked(datetime.now(UTC), policy)
    return policy


def reset_trace_policy_overrides() -> None:
    with TRACE_POLICY_LOCK:
        for key in TracePolicyPatch.model_fields:
            setattr(TRACE_POLICY_OVERRIDE, key, None)


async def start_trace_export_retry_worker() -> None:
    """初回の送信も再送も daemon worker に任せ、アプリの lock から通信しない。"""
    global TRACE_EXPORT_RETRY_WORKER, TRACE_EXPORT_RETRY_STOP
    if TRACE_EXPORT_RETRY_WORKER is not None and TRACE_EXPORT_RETRY_WORKER.is_alive():
        return
    stop = Event()
    TRACE_EXPORT_RETRY_STOP = stop
    TRACE_EXPORT_RETRY_WORKER = Thread(
        target=_trace_export_retry_worker_loop,
        args=(stop,),
        name="agent-trace-event-delivery",
        daemon=True,
    )
    TRACE_EXPORT_RETRY_WORKER.start()


async def stop_trace_export_retry_worker() -> None:
    """停止の待ちに上限を設ける。通信が止まってもプロセスの終了を妨げない。"""
    global TRACE_EXPORT_RETRY_WORKER, TRACE_EXPORT_RETRY_STOP
    worker, TRACE_EXPORT_RETRY_WORKER = TRACE_EXPORT_RETRY_WORKER, None
    stop, TRACE_EXPORT_RETRY_STOP = TRACE_EXPORT_RETRY_STOP, None
    if stop is not None:
        stop.set()
    if worker is not None:
        from app.settings import get_settings

        worker.join(timeout=get_settings().trace_shutdown_timeout_seconds)
        if worker.is_alive():
            _mark_trace_export_error("shutdown_deadline_exceeded")


def trace_export_retry_worker_running() -> bool:
    return TRACE_EXPORT_RETRY_WORKER is not None and TRACE_EXPORT_RETRY_WORKER.is_alive()


def trace_exporter_status() -> TraceExporterStatus:
    from app.settings import get_settings

    settings = get_settings()
    with TRACE_EXPORT_STATUS_LOCK:
        status = TRACE_EXPORT_STATUS.model_copy()
    with TRACE_EXPORT_RETRY_QUEUE_LOCK:
        retry_queue_size = len(TRACE_EXPORT_RETRY_QUEUE)
    status.configured = bool(
        settings.agent_trace_exporter_url
        or settings.agent_opentelemetry_endpoint
        or _langfuse_configured(settings)
    )
    status.retry_queue_size = retry_queue_size
    status.retry_queue_max_size = max(0, settings.agent_trace_exporter_retry_queue_size)
    status.retry_max_attempts = max(1, settings.agent_trace_exporter_retry_max_attempts)
    status.retry_worker_enabled = settings.agent_trace_exporter_retry_worker_enabled
    status.retry_worker_running = trace_export_retry_worker_running()
    status.retry_worker_interval_seconds = _trace_retry_worker_interval_seconds()
    return status


def observe_request_start() -> float:
    return perf_counter()


def elapsed_since(started_at: float) -> float:
    return perf_counter() - started_at


def _append_trace_event(event_type: str, payload: JsonObject) -> None:
    policy = get_trace_policy()
    if not policy.trace_events_enabled:
        return
    current = current_span_fields()
    event = TraceEvent(
        id=f"trace_event_{uuid4().hex}",
        event_type=event_type,
        telemetry_trace_id=current.get("trace_id"),
        telemetry_span_id=current.get("span_id"),
        run_id=_payload_text_or_none(payload, "run_id"),
        step_id=_payload_text_or_none(payload, "step_id"),
        tool_name=_payload_text_or_none(payload, "tool_name"),
        trace_id=_trace_id_from_payload(payload),
        attributes=_safe_trace_attributes(event_type, payload),
        created_at=datetime.now(UTC),
    )
    if not _should_sample_trace_event(event, policy.trace_sample_rate):
        return
    with TRACE_EVENTS_LOCK:
        TRACE_EVENTS.append(event)
        _prune_trace_events_locked(event.created_at, policy)
    if trace_exporter_status().configured:
        _enqueue_trace_export_retry(event, "", initial=True)


def _prune_trace_events_locked(now: datetime, policy: TracePolicySettings) -> None:
    max_events = max(1, policy.trace_events_buffer_size)
    while len(TRACE_EVENTS) > max_events:
        TRACE_EVENTS.popleft()
    if policy.trace_events_retention_seconds <= 0:
        return
    cutoff = now - timedelta(seconds=policy.trace_events_retention_seconds)
    while TRACE_EVENTS and TRACE_EVENTS[0].created_at < cutoff:
        TRACE_EVENTS.popleft()


def _should_sample_trace_event(event: TraceEvent, sample_rate: float) -> bool:
    bounded_rate = min(1.0, max(0.0, sample_rate))
    if bounded_rate >= 1.0:
        return True
    if bounded_rate <= 0.0:
        return False
    basis = event.trace_id or event.run_id or event.id
    digest = sha256(basis.encode()).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < bounded_rate


def _is_trace_priority_event(event: TraceEvent) -> bool:
    if event.event_type.endswith(".failed"):
        return True
    if event.event_type in {"tool.guardrail_warning", "approval.decided"}:
        return True
    return bool(event.attributes.get("error_code"))


def _export_trace_event(event: TraceEvent) -> None:
    errors, exported = _export_trace_event_once(event)
    if errors:
        message = ";".join(errors)
        _mark_trace_export_error(message)
        _enqueue_trace_export_retry(event, message)
    elif exported:
        _mark_trace_export_success()


def flush_trace_export_retry_queue(
    limit: int = 100, *, force: bool = False
) -> TraceExportRetryData:
    from app.settings import get_settings

    settings = get_settings()
    max_attempts = max(1, settings.agent_trace_exporter_retry_max_attempts)
    bounded_limit = max(1, limit)
    with TRACE_EXPORT_RETRY_QUEUE_LOCK:
        items = [
            TRACE_EXPORT_RETRY_QUEUE.popleft()
            for _ in range(min(bounded_limit, len(TRACE_EXPORT_RETRY_QUEUE)))
        ]
    data = TraceExportRetryData()
    now = datetime.now(UTC)
    for item in items:
        if not force and item.next_retry_at > now:
            _requeue_trace_export_retry(item)
            data.skipped += 1
            continue
        data.attempted += 1
        sinks = {error.split(":", 1)[0] for error in item.last_error.split(";") if error}
        errors, exported = _export_trace_event_once(item.event, sinks=sinks or None)
        if errors:
            item.attempts += 1
            item.last_error = ";".join(errors)
            _mark_trace_export_error(item.last_error)
            if (
                item.attempts >= max_attempts
                or not settings.agent_trace_exporter_retry_worker_enabled
            ):
                data.dropped += 1
                TRACE_DELIVERIES.labels(outcome="exhausted").inc()
                _mark_trace_export_error(item.last_error)
            else:
                item.next_retry_at = _next_trace_retry_at(item.attempts)
                _requeue_trace_export_retry(item)
                data.requeued += 1
                TRACE_DELIVERIES.labels(outcome="retried").inc()
        elif exported:
            data.succeeded += 1
            TRACE_DELIVERIES.labels(outcome="delivered").inc()
            _mark_trace_export_success()
    with TRACE_EXPORT_RETRY_QUEUE_LOCK:
        data.queue_size = len(TRACE_EXPORT_RETRY_QUEUE)
    return data


def _trace_export_retry_worker_loop(stop: Event) -> None:
    while True:
        try:
            flush_trace_export_retry_queue(
                limit=_trace_retry_worker_batch_size(), force=stop.is_set()
            )
        except Exception as exc:  # noqa: BLE001 - 観測の失敗は実行を止めない
            _mark_trace_export_error(type(exc).__name__)
        if stop.is_set():
            return
        stop.wait(_trace_retry_worker_interval_seconds())


def clear_trace_export_retry_queue() -> None:
    with TRACE_EXPORT_RETRY_QUEUE_LOCK:
        TRACE_EXPORT_RETRY_QUEUE.clear()


def _export_trace_event_once(
    event: TraceEvent, *, sinks: set[str] | None = None
) -> tuple[list[str], bool]:
    from app.settings import get_settings

    settings = get_settings()
    if (
        not settings.agent_trace_exporter_url
        and not settings.agent_opentelemetry_endpoint
        and not _langfuse_configured(settings)
    ):
        return [], False
    errors: list[str] = []
    exported = False
    if settings.agent_trace_exporter_url and (sinks is None or "webhook" in sinks):
        try:
            _export_trace_event_webhook(event)
            exported = True
        except Exception as exc:  # noqa: BLE001 - exporter は runtime を止めない
            errors.append(f"webhook:{_safe_trace_export_error(exc)}")
    if settings.agent_opentelemetry_endpoint and (sinks is None or "otlp" in sinks):
        try:
            _export_trace_event_otlp(event)
            exported = True
        except Exception as exc:  # noqa: BLE001 - exporter は runtime を止めない
            errors.append(f"otlp:{_safe_trace_export_error(exc)}")
    if _langfuse_configured(settings) and (sinks is None or "langfuse" in sinks):
        try:
            _export_trace_event_langfuse(event)
            exported = True
        except Exception as exc:  # noqa: BLE001 - exporter は runtime を止めない
            errors.append(f"langfuse:{_safe_trace_export_error(exc)}")
    return errors, exported


def _enqueue_trace_export_retry(
    event: TraceEvent, last_error: str, *, initial: bool = False
) -> None:
    from app.settings import get_settings

    settings = get_settings()
    max_size = max(0, settings.agent_trace_exporter_retry_queue_size)
    if max_size <= 0:
        return
    item = TraceExportRetryItem(
        event=event,
        attempts=0 if initial else 1,
        last_error=last_error,
        queued_at=datetime.now(UTC),
        next_retry_at=datetime.now(UTC) if initial else _next_trace_retry_at(1),
    )
    with TRACE_EXPORT_RETRY_QUEUE_LOCK:
        TRACE_EXPORT_RETRY_QUEUE.append(item)
        while len(TRACE_EXPORT_RETRY_QUEUE) > max_size:
            TRACE_EXPORT_RETRY_QUEUE.popleft()
            TRACE_DELIVERIES.labels(outcome="dropped").inc()


def _requeue_trace_export_retry(item: TraceExportRetryItem) -> None:
    from app.settings import get_settings

    settings = get_settings()
    max_size = max(0, settings.agent_trace_exporter_retry_queue_size)
    if max_size <= 0:
        return
    with TRACE_EXPORT_RETRY_QUEUE_LOCK:
        TRACE_EXPORT_RETRY_QUEUE.append(item)
        while len(TRACE_EXPORT_RETRY_QUEUE) > max_size:
            TRACE_EXPORT_RETRY_QUEUE.popleft()
            TRACE_DELIVERIES.labels(outcome="dropped").inc()


def _next_trace_retry_at(attempts: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=_trace_retry_delay_seconds(attempts))


def _trace_retry_delay_seconds(attempts: int) -> float:
    from app.settings import get_settings

    settings = get_settings()
    base_delay = float(max(0.0, settings.agent_trace_exporter_retry_base_delay_seconds))
    max_delay = float(max(base_delay, settings.agent_trace_exporter_retry_max_delay_seconds))
    return float(min(max_delay, base_delay * (2 ** max(0, attempts - 1))))


def _trace_retry_worker_interval_seconds() -> float:
    from app.settings import get_settings

    return float(max(0.01, get_settings().agent_trace_exporter_retry_worker_interval_seconds))


def _trace_retry_worker_batch_size() -> int:
    from app.settings import get_settings

    return max(1, int(get_settings().agent_trace_exporter_retry_worker_batch_size))


def _export_trace_event_webhook(event: TraceEvent) -> None:
    from app.settings import get_settings

    settings = get_settings()
    if settings.agent_trace_exporter_url is None:
        return
    payload: JsonObject = {
        "source": settings.service_name,
        "event": event.model_dump(mode="json"),
    }
    with httpx.Client(
        timeout=settings.agent_trace_exporter_timeout_seconds,
        **http_client_options(settings.agent_trace_exporter_url),
    ) as client:
        response = client.post(
            settings.agent_trace_exporter_url,
            json=payload,
            headers=_trace_export_headers(settings.agent_trace_exporter_api_key),
        )
        response.raise_for_status()


def _export_trace_event_otlp(event: TraceEvent) -> None:
    from app.settings import get_settings

    settings = get_settings()
    if settings.agent_opentelemetry_endpoint is None:
        return
    endpoint = _otlp_logs_endpoint(settings.agent_opentelemetry_endpoint)
    with httpx.Client(
        timeout=settings.agent_trace_exporter_timeout_seconds,
        **http_client_options(endpoint),
    ) as client:
        response = client.post(
            endpoint,
            json=_otlp_event_payload(event, service_name=settings.service_name),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()


def _export_trace_event_langfuse(event: TraceEvent) -> None:
    from app.settings import get_settings

    settings = get_settings()
    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError("sdk_missing") from exc
    client = Langfuse(
        public_key=settings.agent_langfuse_public_key,
        secret_key=settings.agent_langfuse_secret_key,
        base_url=settings.agent_langfuse_host,
    )
    metadata = {
        "event_type": event.event_type,
        "run_id": event.run_id,
        "step_id": event.step_id,
        "tool_name": event.tool_name,
        "trace_id": event.trace_id,
        **event.attributes,
    }
    client.create_event(
        name=f"agent.{event.event_type}",
        trace_context={
            "trace_id": event.telemetry_trace_id,
            "parent_span_id": event.telemetry_span_id,
        }
        if event.telemetry_trace_id and event.telemetry_span_id
        else None,
        metadata={key: value for key, value in metadata.items() if value is not None},
    )
    flush = getattr(client, "flush", None)
    if callable(flush):
        flush()


def _langfuse_configured(settings: Any) -> bool:
    return bool(
        settings.agent_langfuse_host
        and settings.agent_langfuse_public_key
        and settings.agent_langfuse_secret_key
    )


def _otlp_logs_endpoint(endpoint: str) -> str:
    normalized = endpoint.rstrip("/")
    if normalized.endswith("/v1/traces"):
        normalized = normalized.removesuffix("/v1/traces")
    return normalized if normalized.endswith("/v1/logs") else f"{normalized}/v1/logs"


def _otlp_event_payload(event: TraceEvent, *, service_name: str) -> JsonObject:
    # 終了イベントは時点の log。推測した開始時刻や span ID は作らない。
    record: JsonObject = {
        "timeUnixNano": str(_datetime_to_unix_nano(event.created_at)),
        "body": {"stringValue": f"agent.{event.event_type}"},
        "severityNumber": 17 if event.event_type.endswith(".failed") else 9,
        "attributes": _otlp_trace_attributes(event),
    }
    if event.telemetry_trace_id and event.telemetry_span_id:
        record.update(traceId=event.telemetry_trace_id, spanId=event.telemetry_span_id)
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [_otlp_attribute("service.name", service_name)]},
                "scopeLogs": [
                    {
                        "scope": {"name": "production-ready-agent.agent-runtime"},
                        "logRecords": [record],
                    }
                ],
            }
        ]
    }


def _otlp_trace_attributes(event: TraceEvent) -> list[JsonObject]:
    attributes: JsonObject = {
        "agent.event_type": event.event_type,
    }
    if event.run_id:
        attributes["agent.run_id"] = event.run_id
    if event.step_id:
        attributes["agent.step_id"] = event.step_id
    if event.tool_name:
        attributes["agent.tool_name"] = event.tool_name
    if event.trace_id:
        attributes["agent.trace_id"] = event.trace_id
    for key, value in event.attributes.items():
        attributes[f"agent.{key}"] = value
    return [_otlp_attribute(key, value) for key, value in attributes.items()]


def _otlp_attribute(key: str, value: Any) -> JsonObject:
    return {"key": key, "value": _otlp_any_value(value)}


def _otlp_any_value(value: Any) -> JsonObject:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"stringValue": ""}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, ensure_ascii=False, sort_keys=True)}


def _datetime_to_unix_nano(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _trace_export_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _mark_trace_export_success() -> None:
    global TRACE_EXPORT_STATUS
    with TRACE_EXPORT_STATUS_LOCK:
        TRACE_EXPORT_STATUS = TRACE_EXPORT_STATUS.model_copy(
            update={
                "last_success_at": datetime.now(UTC),
                "last_error": None,
                "last_error_at": None,
            }
        )


def _mark_trace_export_error(message: str) -> None:
    global TRACE_EXPORT_STATUS
    with TRACE_EXPORT_STATUS_LOCK:
        TRACE_EXPORT_STATUS = TRACE_EXPORT_STATUS.model_copy(
            update={
                "last_error": message,
                "last_error_at": datetime.now(UTC),
            }
        )


def _safe_trace_export_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_status:{exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return exc.__class__.__name__
    return exc.__class__.__name__


def _safe_trace_attributes(event_type: str, payload: JsonObject) -> JsonObject:
    keys = {
        "approval_id",
        "approved",
        "artifact_id",
        "duration_ms",
        "error_code",
        "kind",
        "policy_decision",
        "warnings",
    }
    attributes: JsonObject = {
        key: value for key, value in payload.items() if key in keys and _is_safe_trace_value(value)
    }
    attributes["event_type"] = event_type
    return attributes


def _is_safe_trace_value(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(isinstance(item, str | int | float | bool) for item in value)
    return False


def _payload_text_or_none(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _trace_id_from_payload(payload: JsonObject) -> str | None:
    trace_id = _payload_text_or_none(payload, "trace_id")
    if trace_id is not None:
        return trace_id
    audit_metadata = payload.get("audit_metadata")
    if isinstance(audit_metadata, dict):
        value = audit_metadata.get("trace_id")
        if isinstance(value, str) and value:
            return value
    return None


def _payload_text(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return "unknown"


def _payload_list(payload: JsonObject, key: str) -> list[str]:
    value = payload.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []
