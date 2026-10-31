"""検証済みの HTTP 相関と、worker の業務相関。認証情報とは分離する。"""

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from uuid import uuid4

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
traceparent_var: ContextVar[str | None] = ContextVar("traceparent", default=None)
log_context_var: ContextVar[Mapping[str, str]] = ContextVar(
    "log_context", default=MappingProxyType({})
)
access_summary_var: ContextVar[bool] = ContextVar("access_summary", default=False)
_DOMAIN_IDS = frozenset(
    {"run_id", "job_id", "worker_id", "document_id", "profile_id", "operation_id"}
)


def generate_request_id(incoming: str | None) -> str:
    """不正な値は相関 ID として信用せず新規発行する。"""
    candidate = (incoming or "").strip()
    return candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else uuid4().hex


def validated_traceparent(incoming: str | None) -> str | None:
    """対応する W3C version 00 のみ受け付け、業務 ID から trace を捏造しない。"""
    match = _TRACEPARENT.fullmatch(incoming or "")
    if not match or int(match[1], 16) == 0 or int(match[2], 16) == 0:
        return None
    return match[0]


def correlation_fields() -> dict[str, str]:
    fields = dict(log_context_var.get())
    request_id = request_id_var.get()
    if request_id:
        fields["request_id"] = request_id
    from .tracing import current_span_fields

    local_fields = current_span_fields()
    if local_fields:
        fields.update(local_fields)
        return fields
    traceparent = validated_traceparent(traceparent_var.get())
    if traceparent:
        _, trace_id, parent_span_id, flags = traceparent.split("-")
        # HTTP で受け取った span は親。ローカル span がないのに span_id として出さない。
        fields.update(trace_id=trace_id, parent_span_id=parent_span_id, trace_flags=flags)
    return fields


def outbound_correlation_headers() -> dict[str, str]:
    """プロセス境界では検証済みの相関だけを明示的に引き継ぐ。"""
    headers: dict[str, str] = {}
    request_id = request_id_var.get()
    if request_id and REQUEST_ID_PATTERN.fullmatch(request_id):
        headers["X-Request-ID"] = request_id
    from .tracing import capture_trace_context

    traceparent = capture_trace_context().get("traceparent") or validated_traceparent(
        traceparent_var.get()
    )
    if traceparent:
        headers["traceparent"] = traceparent
    for key in ("run_id", "job_id"):
        value = log_context_var.get().get(key)
        if value and REQUEST_ID_PATTERN.fullmatch(value):
            headers[f"X-Correlation-{key.replace('_', '-')}"] = value
    return headers


@contextmanager
def bind_log_context(**fields: str) -> Iterator[None]:
    """claim した job / Run の範囲にだけ業務 ID を付け、例外・取消でも復元する。"""
    values = dict(log_context_var.get())
    values.update(
        {
            key: value
            for key, value in fields.items()
            if key in _DOMAIN_IDS and REQUEST_ID_PATTERN.fullmatch(value)
        }
    )
    token = log_context_var.set(values)
    try:
        yield
    finally:
        log_context_var.reset(token)
