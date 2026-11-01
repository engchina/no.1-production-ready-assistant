"""RAG/HTTP の観測性ヘルパー。"""

import logging
import re
from contextlib import suppress
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import perf_counter
from typing import Literal, Protocol
from uuid import uuid4

import httpx
from pr_backend_core.internal_http import http_client_options
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from app.clients.http_retry import HttpRetryConfig, request_with_retry, retry_config_from_settings
from app.config import Settings

trace_logger = logging.getLogger("app.trace")
TraceOutcome = Literal["success", "error", "cancelled", "skipped"]
TraceAttribute = str | int | float | bool | None
SAFE_TRACE_ATTRIBUTE_SUFFIXES = (
    "_chars",
    "_bytes",
    "_count",
    "_id",
    "_ms",
    "_seconds",
    "_status",
    "_type",
)
SENSITIVE_TRACE_ATTRIBUTE_PATTERN = re.compile(
    r"(query|prompt|secret|context|raw_text|ocr|field_value|payload|content)",
    re.IGNORECASE,
)


class TraceSpanEvent(BaseModel):
    """OpenTelemetry / Langfuse へ橋渡ししやすい span event。"""

    event_type: Literal["rag.trace_span"] = "rag.trace_span"
    trace_id: str
    span_name: str
    outcome: TraceOutcome
    duration_ms: float
    attributes: dict[str, TraceAttribute] = Field(default_factory=dict)
    error_type: str | None = None


class TraceExporter(Protocol):
    """脱機密化済み trace span event の export 先。"""

    def export(self, event: TraceSpanEvent) -> None:
        """event を外部 sink へ渡す。"""

    def close(self) -> None:
        """exporter の後始末を行う。"""


class NoopTraceExporter:
    """trace export 無効時の no-op exporter。"""

    def export(self, event: TraceSpanEvent) -> None:
        """何もしない。"""

    def close(self) -> None:
        """何もしない。"""


class HttpTraceExporter:
    """脱機密化済み trace span event を HTTP JSON で非同期 export する。"""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str = "",
        timeout_seconds: float = 2.0,
        queue_size: int = 1024,
        retry: HttpRetryConfig | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._headers = {"content-type": "application/json", "accept": "application/json"}
        if bearer_token.strip():
            self._headers["authorization"] = f"Bearer {bearer_token.strip()}"
        self._timeout_seconds = timeout_seconds
        self._retry = retry or HttpRetryConfig()
        self._queue: Queue[TraceSpanEvent | None] = Queue(maxsize=queue_size)
        self._closed = Event()
        self._worker = Thread(
            target=self._run,
            name="rag-trace-exporter",
            daemon=True,
        )
        self._worker.start()

    def export(self, event: TraceSpanEvent) -> None:
        """event を queue に積む。満杯時は RAG 処理を止めず drop する。"""
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(event)
        except Full:
            trace_logger.warning(
                "rag_trace_export_dropped",
                extra={
                    "trace_export_event": {
                        "event_type": "rag.trace_export",
                        "outcome": "dropped",
                        "reason": "queue_full",
                    }
                },
            )

    def close(self) -> None:
        """worker を短時間だけ待って停止する。"""
        if self._closed.is_set():
            return
        self._closed.set()
        with suppress(Full):
            self._queue.put_nowait(None)
        self._worker.join(timeout=min(5.0, self._timeout_seconds + 1.0))

    def _run(self) -> None:
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            **http_client_options(self._endpoint),
        ) as client:
            while not self._closed.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.2)
                except Empty:
                    continue
                try:
                    if event is None:
                        return
                    response = request_with_retry(
                        client,
                        "POST",
                        self._endpoint,
                        retry=self._retry,
                        logger=trace_logger,
                        log_extra={"trace_endpoint": self._endpoint},
                        json=event.model_dump(mode="json"),
                        headers=self._headers,
                    )
                    response.raise_for_status()
                except Exception as exc:
                    trace_logger.warning(
                        "rag_trace_export_failed",
                        extra={
                            "trace_export_event": {
                                "event_type": "rag.trace_export",
                                "outcome": "error",
                                "error_type": type(exc).__name__,
                            }
                        },
                    )
                finally:
                    self._queue.task_done()


_TRACE_EXPORTER: TraceExporter = NoopTraceExporter()


HTTP_REQUESTS = Counter(
    "rag_http_requests_total",
    "HTTP リクエスト数",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "rag_http_request_duration_seconds",
    "HTTP リクエスト処理時間",
    ["method", "path"],
)
RAG_REQUESTS = Counter(
    "rag_search_requests_total",
    "RAG 検索リクエスト数",
    ["mode", "outcome"],
)
RAG_LATENCY = Histogram(
    "rag_search_duration_seconds",
    "RAG 検索処理時間",
    ["mode"],
)
RAG_STAGE_LATENCY = Histogram(
    "rag_search_stage_duration_seconds",
    "RAG 検索の stage 別処理時間",
    ["mode", "stage", "outcome"],
)
INGESTION_DOCUMENTS = Counter(
    "rag_ingestion_documents_total",
    "取込ドキュメント数",
    ["outcome"],
)
INGESTION_CHUNKS = Histogram(
    "rag_ingestion_chunks",
    "1 ドキュメントあたりのチャンク数",
)
INGESTION_STAGE_LATENCY = Histogram(
    "rag_ingestion_stage_duration_seconds",
    "RAG 取込の stage 別処理時間",
    ["stage", "outcome"],
)
EVALUATION_CASES = Counter(
    "rag_evaluation_cases_total",
    "RAG 評価ケース数",
    ["mode", "status"],
)
EVALUATION_CASE_DURATION = Histogram(
    "rag_evaluation_case_duration_seconds",
    "RAG 評価ケース処理時間",
    ["mode", "status"],
)
RETRIEVAL_HITS = Histogram(
    "rag_retrieval_hits",
    "検索で取得した候補チャンク数",
    ["mode"],
)
# 回答の経路の理由と、現場のデータの確認の要否（#1283）。
# 理由は回答の対応の 7 値だけ（低 cardinality）。
ANSWER_ROUTES = Counter(
    "rag_answer_routes_total",
    "回答の経路の理由（回答の対応）と現場のデータの確認の要否",
    ["reason", "requires_environment_data"],
)
RATE_LIMIT_DECISIONS = Counter(
    "rag_rate_limit_decisions_total",
    "高コスト API の rate limit 判定数",
    ["scope", "outcome"],
)
# 検索のメトリクスの mode ラベル。回答は回答フロー(hybrid + RRF)だけで、利用者が選ぶ検索モードは
# #595 で削除したため固定値にする(既存のダッシュボードの系列を保つため値は hybrid のまま)。
SEARCH_METRIC_MODE = "hybrid"


def new_trace_id() -> str:
    """検索・取込の追跡に使う軽量 trace ID を発行する。"""
    return uuid4().hex


def now() -> float:
    """monotonic clock の現在値を返す。"""
    return perf_counter()


def elapsed_ms(started_at: float) -> float:
    """開始時刻からの経過ミリ秒。"""
    return round((perf_counter() - started_at) * 1000, 2)


def record_http_request(method: str, path: str, status: int, seconds: float) -> None:
    """HTTP メトリクスを記録する。"""
    status_label = str(status)
    HTTP_REQUESTS.labels(method=method, path=path, status=status_label).inc()
    HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(seconds)


def record_rag_request(mode: str, outcome: str, seconds: float, hits: int) -> None:
    """RAG 検索メトリクスを記録する。"""
    RAG_REQUESTS.labels(mode=mode, outcome=outcome).inc()
    RAG_LATENCY.labels(mode=mode).observe(seconds)
    RETRIEVAL_HITS.labels(mode=mode).observe(hits)


def record_rag_stage(mode: str, stage: str, outcome: str, seconds: float) -> None:
    """RAG 検索の stage 別メトリクスを記録する。"""
    RAG_STAGE_LATENCY.labels(mode=mode, stage=stage, outcome=outcome).observe(seconds)


def record_ingestion(outcome: str, chunk_count: int) -> None:
    """取込メトリクスを記録する。"""
    INGESTION_DOCUMENTS.labels(outcome=outcome).inc()
    INGESTION_CHUNKS.observe(chunk_count)


def record_ingestion_stage(stage: str, outcome: str, seconds: float) -> None:
    """RAG 取込の stage 別メトリクスを記録する。"""
    INGESTION_STAGE_LATENCY.labels(stage=stage, outcome=outcome).observe(seconds)


def record_evaluation_case(mode: str, status: str, seconds: float) -> None:
    """golden set 評価 case のメトリクスを記録する。"""
    EVALUATION_CASES.labels(mode=mode, status=status).inc()
    EVALUATION_CASE_DURATION.labels(mode=mode, status=status).observe(seconds)


def record_answer_route(reason: str, requires_environment_data: bool) -> None:
    """回答の経路の理由を記録する（#1283）。"""
    ANSWER_ROUTES.labels(
        reason=reason,
        requires_environment_data="true" if requires_environment_data else "false",
    ).inc()


def record_rate_limit_decision(scope: str, outcome: str) -> None:
    """rate limit 判定を低 cardinality label で記録する。"""
    RATE_LIMIT_DECISIONS.labels(scope=scope, outcome=outcome).inc()


def record_trace_span(
    *,
    trace_id: str,
    span_name: str,
    outcome: TraceOutcome,
    seconds: float,
    attributes: dict[str, object] | None = None,
    error: BaseException | None = None,
) -> TraceSpanEvent:
    """単一 RAG stage の trace span event を構造化ログへ出す。"""
    event = TraceSpanEvent(
        trace_id=trace_id,
        span_name=span_name,
        outcome=outcome,
        duration_ms=round(seconds * 1000, 2),
        attributes=_safe_trace_attributes(attributes or {}),
        error_type=type(error).__name__ if error is not None else None,
    )
    trace_logger.info(
        "rag_trace_span",
        extra={"trace_event": event.model_dump(mode="json")},
    )
    try:
        _TRACE_EXPORTER.export(event)
    except Exception as exc:
        trace_logger.warning(
            "rag_trace_export_failed",
            extra={
                "trace_export_event": {
                    "event_type": "rag.trace_export",
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                }
            },
        )
    return event


def _safe_trace_attributes(attributes: dict[str, object]) -> dict[str, TraceAttribute]:
    """trace attribute を低機密・低 cardinality な scalar に絞る。"""
    safe: dict[str, TraceAttribute] = {}
    for key, value in attributes.items():
        if not _is_safe_trace_attribute_key(key):
            continue
        if value is None or isinstance(value, bool | int | float):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:200]
    return safe


def _is_safe_trace_attribute_key(key: str) -> bool:
    """本文系 key を落とし、件数・サイズなどの運用メタデータだけ通す。"""
    normalized = key.strip().lower()
    if not normalized:
        return False
    if normalized.endswith(SAFE_TRACE_ATTRIBUTE_SUFFIXES):
        return True
    return SENSITIVE_TRACE_ATTRIBUTE_PATTERN.search(normalized) is None


def configure_trace_exporter(settings: Settings) -> None:
    """設定に基づいて trace exporter を初期化する。"""
    endpoint = settings.trace_export_http_endpoint.strip()
    set_trace_exporter(
        HttpTraceExporter(
            endpoint=endpoint,
            bearer_token=settings.trace_export_http_bearer_token,
            timeout_seconds=settings.trace_export_http_timeout_seconds,
            queue_size=settings.trace_export_queue_size,
            retry=retry_config_from_settings(settings),
        )
        if endpoint
        else NoopTraceExporter()
    )


def set_trace_exporter(exporter: TraceExporter) -> None:
    """テストや lifespan から trace exporter を差し替える。"""
    global _TRACE_EXPORTER
    _TRACE_EXPORTER.close()
    _TRACE_EXPORTER = exporter


def close_trace_exporter() -> None:
    """現在の trace exporter を閉じ、no-op に戻す。"""
    set_trace_exporter(NoopTraceExporter())
