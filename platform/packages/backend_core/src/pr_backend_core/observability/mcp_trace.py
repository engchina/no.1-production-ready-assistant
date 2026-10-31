"""製品をまたぐ MCP の呼び出しの trace の受け渡し（#3226 の案 A）。

- 送る側（Agent）は W3C の `traceparent` を、HTTP header（今どおり）と JSON-RPC の `params._meta`
  （OpenTelemetry の MCP の semantic conventions の置き場所）の両方に入れる（`mcp_trace_meta`）。
- 受ける側（RAG / NL2SQL の `POST /api/mcp`）は `mcp_server_trace` の中で要求を処理する。
  **サービストークン（Agent からの呼び出し）の traceparent だけを親にする**（`params._meta`
  を優先し、
  無ければ header）。受けた要求の server の span を共通の span の表に書き、Agent の `mcp.request` の
  下に木をつなげる。
- API キーの外部の MCP クライアントとブラウザの traceparent は親にせず、新しい trace を始める
  （他人の trace に span を差し込めないようにする）。相関のため、外部の span context は
  OpenTelemetry の span link として新しい trace の root に結ぶ（export だけ。表には入れない）。
- 製品の実行（回答・検索・ジョブ）の全体の span は `operation_trace_parent` の中で始める。信頼した
  MCP の要求（`mcp_server_trace` の trusted）の中なら今の trace を続け、それ以外（画面の要求・
  API キー・評価）は新しい trace にして、今の span は span link にだけ残す。別の process で実行する
  ジョブは、受け付けた要求の traceparent を `traceparent_links` で span link にする。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .request_context import validated_traceparent
from .tracing import SpanListener, TraceSummary, capture_trace_context, operation_trace

_PROPAGATOR = TraceContextTextMapPropagator()
MCP_SERVER_SPAN = "mcp.server"
# 信頼した MCP の要求（サービストークン）の server の span の中か。中なら製品の実行は今の trace
# を続ける。
_IN_TRUSTED_SERVER_TRACE: ContextVar[bool] = ContextVar(
    "pr_in_trusted_mcp_server_trace", default=False
)


def mcp_trace_meta() -> dict[str, str]:
    """JSON-RPC の `params._meta` に入れる trace の値（今の span の traceparent。無ければ空）。"""
    return capture_trace_context()


def incoming_mcp_traceparent(
    meta: Mapping[str, object] | None, header_traceparent: str | None
) -> str | None:
    """来た traceparent（`params._meta` を優先し、無ければ HTTP header）。形の違う値は捨てる。"""
    candidate = meta.get("traceparent") if isinstance(meta, Mapping) else None
    from_meta = validated_traceparent(candidate) if isinstance(candidate, str) else None
    return from_meta or validated_traceparent(header_traceparent)


def _span_context(traceparent: str) -> trace.SpanContext | None:
    extracted = _PROPAGATOR.extract({"traceparent": traceparent})
    span_context = trace.get_current_span(extracted).get_span_context()
    return span_context if span_context.is_valid else None


@contextmanager
def mcp_server_trace(
    *,
    operation_id: str,
    trusted: bool,
    traceparent: str | None,
    listener: SpanListener | None = None,
    method: str | None = None,
    tool_name: str | None = None,
) -> Iterator[TraceSummary]:
    """MCP の要求を server の span（`mcp.server`）の中で処理する。

    `trusted`（サービストークンで認証した要求）なら来た traceparent を親にする。そうでなければ新しい
    trace を始め、来た span context は span link にだけ残す。`listener` は共通の span
    の表への書き込み
    （`TraceSpanWriter.listener(...)`）で、server の span は実行の全体の span として表に入る。
    `method`・`tool_name` は OpenTelemetry の MCP の semantic conventions の `mcp.method.name`・
    `gen_ai.tool.name`（#3272。引数・結果は入れない）。
    """
    attributes: dict[str, object] = {}
    if method:
        attributes["mcp.method.name"] = method
    if tool_name:
        attributes["gen_ai.tool.name"] = tool_name
    parent = traceparent if trusted else None
    linked = _span_context(traceparent) if traceparent and not trusted else None
    carrier_context = _PROPAGATOR.extract({"traceparent": parent}) if parent else context.Context()
    token = context.attach(carrier_context)
    trusted_token = _IN_TRUSTED_SERVER_TRACE.set(trusted)
    try:
        with operation_trace(
            MCP_SERVER_SPAN,
            operation_id=operation_id,
            attributes=attributes,
            listener=listener,
            kind="server",
            links=[trace.Link(linked)] if linked is not None else (),
        ) as summary:
            yield summary
    finally:
        _IN_TRUSTED_SERVER_TRACE.reset(trusted_token)
        context.detach(token)


@contextmanager
def operation_trace_parent() -> Iterator[Sequence[trace.Link]]:
    """製品の実行の全体の span を置く trace の文脈。

    中で `operation_trace(..., links=...)` を始める。信頼した MCP の要求（`mcp_server_trace` の
    trusted）の中なら今の trace を続ける（links は空）。それ以外（画面の要求・API キーの外部の
    MCP クライアント・品質評価）は新しい trace を始め、今の span（ブラウザの traceparent を親に
    した HTTP の span など）は span link にだけ残す。
    """
    if _IN_TRUSTED_SERVER_TRACE.get():
        yield ()
        return
    current = trace.get_current_span().get_span_context()
    token = context.attach(context.Context())
    try:
        yield [trace.Link(current)] if current.is_valid else ()
    finally:
        context.detach(token)


def traceparent_links(traceparent: str | None) -> list[trace.Link]:
    """受け付けた要求の traceparent を span link にする（親にはしない。形の違う値は捨てる）。

    別の process で実行するジョブが、新しい trace の root に受け付けた要求との関係を残すときに使う。
    """
    valid = validated_traceparent(traceparent)
    span_context = _span_context(valid) if valid else None
    return [trace.Link(span_context)] if span_context is not None else []


__all__ = [
    "MCP_SERVER_SPAN",
    "incoming_mcp_traceparent",
    "mcp_server_trace",
    "mcp_trace_meta",
    "operation_trace_parent",
    "traceparent_links",
]
