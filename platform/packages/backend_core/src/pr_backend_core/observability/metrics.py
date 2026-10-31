"""Prometheus メトリクス + HTTP ミドルウェア（サービス横断で共通）。"""

import asyncio
import logging
from time import perf_counter
from typing import Protocol

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, make_asgi_app
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .request_context import (
    REQUEST_ID_PATTERN,
    access_summary_var,
    generate_request_id,
    log_context_var,
    request_id_var,
    traceparent_var,
    validated_traceparent,
)
from .tracing import ObservedSpan, attach_trace_context, capture_trace_context, observed_span

logger = logging.getLogger("pr_backend_core.http")

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP リクエスト総数",
    labelnames=("method", "path", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP リクエスト処理時間（秒）",
    labelnames=("method", "path"),
)


def record_http_request(*, method: str, path: str, status: int, seconds: float) -> None:
    """HTTP リクエストのメトリクスを記録する。"""
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(seconds)


def metrics_asgi_app() -> ASGIApp:
    """Prometheus エクスポジション用 ASGI アプリ（`/metrics` に mount）。"""
    return make_asgi_app()


def _route_path(request: Request) -> str:
    """label cardinality を抑えるため route template を返す。"""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "/unmatched"


class HttpRecorder(Protocol):
    def __call__(self, *, method: str, path: str, status: int, seconds: float) -> None: ...


class MetricsMiddleware:
    """body / SSE の終了まで相関を保つ ASGI 境界。HTTP summary は1 hopにつき1件。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        enable_metrics: bool = True,
        record_request: HttpRecorder = record_http_request,
    ) -> None:
        self.app = app
        self.enable_metrics = enable_metrics
        self.record_request = record_request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        with (
            attach_trace_context({"traceparent": headers.get("traceparent", "")}),
            observed_span(
                "http.request",
                kind="server",
                attributes={"http.request.method": scope.get("method", "GET")},
            ) as span,
        ):
            await self._call_with_context(scope, receive, send, span)

    async def _call_with_context(
        self, scope: Scope, receive: Receive, send: Send, span: ObservedSpan
    ) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        request_id = generate_request_id(headers.get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = request_id
        request_token = request_id_var.set(request_id)
        trace_token = traceparent_var.set(validated_traceparent(headers.get("traceparent")))
        incoming_ids = {
            key: value
            for key in ("run_id", "job_id")
            if (value := headers.get(f"x-correlation-{key.replace('_', '-')}"))
            and REQUEST_ID_PATTERN.fullmatch(value)
        }
        domain_token = log_context_var.set(incoming_ids)
        access_token = access_summary_var.set(True)
        started = perf_counter()
        status = 500
        outcome = "error"
        headers_ms: float | None = None

        async def send_with_context(message: Message) -> None:
            nonlocal status, headers_ms, outcome
            if message["type"] == "http.response.start":
                status = message["status"]
                headers_ms = (perf_counter() - started) * 1000
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                parent = capture_trace_context().get("traceparent")
                if parent:
                    response_headers["traceparent"] = parent
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                outcome = "success" if status < 400 else "error"
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            try:
                if scope["type"] == "http":
                    elapsed = perf_counter() - started
                    request = Request(scope)
                    route = getattr(scope.get("route"), "path", None)
                    # 未解決 URL の値は利用者入力。route template がなければ原文を出さない。
                    path = route if isinstance(route, str) else "/unmatched"
                    span.set_attributes(
                        {
                            "http.route": path,
                            "http.response.status_code": status,
                            "pr.operation.status": outcome,
                        }
                    )
                    if status >= 500 and outcome != "cancelled":
                        span.set_status("failed")
                    if self.enable_metrics:
                        self.record_request(
                            method=request.method, path=path, status=status, seconds=elapsed
                        )
                    poll_success = (
                        request.method == "GET"
                        and 200 <= status < 300
                        and outcome == "success"
                        and (path.startswith("/api/services/") and path.endswith("/status"))
                    )
                    if not poll_success:
                        severity = (
                            logging.ERROR
                            if status >= 500 or outcome != "success" and status < 400
                            else (logging.WARNING if status >= 400 else logging.INFO)
                        )
                        logger.log(
                            severity,
                            "HTTP リクエストが終了しました",
                            extra={
                                "event": "http_access",
                                "http_method": request.method,
                                "http_route": path,
                                "http_status": status,
                                "duration_ms": round(elapsed * 1000, 3),
                                "headers_duration_ms": round(headers_ms, 3)
                                if headers_ms is not None
                                else None,
                                "outcome": outcome,
                            },
                        )
            finally:
                log_context_var.reset(domain_token)
                access_summary_var.reset(access_token)
                traceparent_var.reset(trace_token)
                request_id_var.reset(request_token)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "MetricsMiddleware",
    "metrics_asgi_app",
    "record_http_request",
]
