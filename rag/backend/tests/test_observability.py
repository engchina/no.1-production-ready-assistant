"""観測性 helper のテスト。"""

import logging
from typing import Any, cast

from pytest import LogCaptureFixture

from app.rag.observability import (
    TraceSpanEvent,
    close_trace_exporter,
    record_trace_span,
    set_trace_exporter,
)


def test_record_trace_span_keeps_low_cardinality_safe_attributes(
    caplog: LogCaptureFixture,
) -> None:
    """trace span は scalar attribute と error type だけを構造化ログへ出す。"""
    with caplog.at_level(logging.INFO, logger="app.trace"):
        event = record_trace_span(
            trace_id="trace-1",
            span_name="generation",
            outcome="error",
            seconds=0.12345,
            attributes={
                "model": "local",
                "context_chars": 120,
                "raw_query": "x" * 300,
                "nested": {"secret": "do-not-log"},
            },
            error=RuntimeError("secret policy INV-SECRET"),
        )

    assert event.duration_ms == 123.45
    assert event.error_type == "RuntimeError"
    assert event.attributes["model"] == "local"
    assert event.attributes["context_chars"] == 120
    assert "raw_query" not in event.attributes
    assert "nested" not in event.attributes

    record = next(record for record in caplog.records if record.message == "rag_trace_span")
    trace_event = cast(Any, record).trace_event
    assert trace_event["error_type"] == "RuntimeError"
    assert "INV-SECRET" not in str(trace_event)
    assert "do-not-log" not in str(trace_event)


def test_record_trace_span_exports_sanitized_event() -> None:
    """外部 exporter へ渡す event も構造化ログと同じ脱機密化済み payload にする。"""
    exporter = CapturingTraceExporter()
    set_trace_exporter(exporter)
    try:
        record_trace_span(
            trace_id="trace-export",
            span_name="retrieval",
            outcome="success",
            seconds=0.01,
            attributes={
                "query": "INV-SECRET" * 50,
                "top_k": 20,
                "raw_context": {"secret": "do-not-export"},
            },
        )
    finally:
        close_trace_exporter()

    assert len(exporter.events) == 1
    event = exporter.events[0]
    assert event.trace_id == "trace-export"
    assert event.span_name == "retrieval"
    assert event.attributes["top_k"] == 20
    assert "query" not in event.attributes
    assert "raw_context" not in event.attributes
    assert "do-not-export" not in event.model_dump_json()


def test_record_trace_span_does_not_fail_when_exporter_fails(
    caplog: LogCaptureFixture,
) -> None:
    """export 失敗は request 本体へ波及させず warning に閉じ込める。"""
    set_trace_exporter(FailingTraceExporter())
    try:
        with caplog.at_level(logging.WARNING, logger="app.trace"):
            event = record_trace_span(
                trace_id="trace-export-error",
                span_name="generation",
                outcome="success",
                seconds=0.01,
                attributes={"model": "local"},
            )
    finally:
        close_trace_exporter()

    assert event.trace_id == "trace-export-error"
    record = next(
        record for record in caplog.records if record.message == "rag_trace_export_failed"
    )
    trace_export_event = cast(Any, record).trace_export_event
    assert trace_export_event["error_type"] == "RuntimeError"
    assert "secret" not in str(trace_export_event).lower()


class CapturingTraceExporter:
    """テスト用 trace exporter。"""

    def __init__(self) -> None:
        self.events: list[TraceSpanEvent] = []
        self.closed = False

    def export(self, event: TraceSpanEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class FailingTraceExporter:
    """失敗する exporter。"""

    def export(self, event: TraceSpanEvent) -> None:
        raise RuntimeError("secret exporter failure")

    def close(self) -> None:
        return None
