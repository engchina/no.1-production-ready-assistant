"""共通 trace の親子関係・安全性・sampling と async/thread の境界。"""

import asyncio
from collections.abc import Iterator
from threading import Event

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.tracing import (
    attach_trace_context,
    capture_trace_context,
    configure_tracing,
    current_span_fields,
    observed_span,
    operation_trace,
    shutdown_tracing,
)


@pytest.fixture(autouse=True)
def reset_tracing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pr_backend_core.observability import tracing

    # 設定（記録の span の上限など）をテストの後に戻す（同じ process の後のテストに残さない）。
    monkeypatch.setattr(tracing, "_SETTINGS", tracing._SETTINGS)
    yield
    shutdown_tracing()


def test_hierarchy_does_not_replace_global_provider_or_capture_payload() -> None:
    global_provider = trace.get_tracer_provider()
    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    with operation_trace("task", operation_id="operation-a") as summary:
        with observed_span(
            "llm", attributes={"gen_ai.request.model": "model-a", "prompt": "private-payload"}
        ) as span:
            span.set_attributes({"gen_ai.usage.input_tokens": 12, "response": "private-payload"})
        with (
            observed_span("tool", attributes={"gen_ai.tool.name": "tool-a"}),
            pytest.raises(ValueError),
            observed_span("recoverable"),
        ):
            raise ValueError("private-payload")
    assert trace.get_tracer_provider() is global_provider
    assert summary.completeness == "complete"
    assert len(summary.spans) == 4
    root = next(span for span in summary.spans if span.span_id == summary.root_span_id)
    assert root.status == "done"
    tool = next(span for span in summary.spans if span.operation_name == "tool")
    failed = next(span for span in summary.spans if span.operation_name == "recoverable")
    assert failed.parent_span_id == tool.span_id
    assert tool.parent_span_id == root.span_id
    assert failed.status == "failed"
    assert "private-payload" not in summary.model_dump_json()
    assert shutdown_tracing()
    exported = exporter.get_finished_spans()
    assert len(exported) == 4
    assert all(not span.events for span in exported)
    assert all("prompt" not in (span.attributes or {}) for span in exported)


@pytest.mark.asyncio
async def test_parallel_tasks_and_thread_keep_their_parent_and_restore_context() -> None:
    configure_tracing(TracingSettings(), service_name="synthetic")

    async def child(label: str) -> None:
        with observed_span(label):
            await asyncio.sleep(0)
            await asyncio.to_thread(thread_child)

    def thread_child() -> None:
        with observed_span("thread"):
            assert current_span_fields().get("span_id")

    with operation_trace("task", operation_id="operation-a") as summary:
        await asyncio.gather(child("left"), child("right"))
    root = summary.root_span_id
    parents = {span.span_id for span in summary.spans if span.operation_name in {"left", "right"}}
    assert len(parents) == 2
    assert all(
        span.parent_span_id == root
        for span in summary.spans
        if span.operation_name in {"left", "right"}
    )
    assert {
        span.parent_span_id for span in summary.spans if span.operation_name == "thread"
    } == parents
    assert current_span_fields() == {}
    assert capture_trace_context() == {}


def test_worker_carrier_is_allowlisted_and_invalid_context_does_not_inherit() -> None:
    configure_tracing(TracingSettings(), service_name="synthetic")
    with operation_trace("submit", operation_id="operation-a") as submission:
        carrier = capture_trace_context()
        with attach_trace_context({"traceparent": "invalid"}):
            assert current_span_fields() == {}
    with attach_trace_context(
        {**carrier, "Authorization": "private-payload", "baggage": "private-payload"}
    ):
        assert "span_id" not in current_span_fields()
        with operation_trace("worker", operation_id="operation-a") as execution:
            pass
    assert set(carrier) == {"traceparent"}
    assert submission.telemetry_trace_id == execution.telemetry_trace_id
    assert execution.spans[0].parent_span_id == submission.root_span_id
    assert current_span_fields() == {}


def test_ui_summary_is_bounded_and_keeps_root_even_when_not_sampled() -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(
        TracingSettings(trace_sample_rate=0, trace_ui_max_spans=2),
        service_name="synthetic",
        exporter=exporter,
    )
    with operation_trace("task", operation_id="operation-a") as summary:
        for _ in range(8):
            with observed_span("llm"):
                pass
    assert not summary.sampled
    assert summary.completeness == "partial"
    assert summary.truncated
    assert len(summary.spans) == 2
    assert summary.root_span_id in {span.span_id for span in summary.spans}
    assert shutdown_tracing()
    assert not exporter.get_finished_spans()


def test_blocked_export_does_not_block_producer_or_shutdown_deadline() -> None:
    entered, release = Event(), Event()

    class BlockedExporter(SpanExporter):
        def export(self, spans: object) -> SpanExportResult:
            entered.set()
            release.wait(5)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    configure_tracing(
        TracingSettings(
            trace_queue_size=2, trace_batch_size=1, trace_shutdown_timeout_seconds=0.05
        ),
        service_name="synthetic",
        exporter=BlockedExporter(),
    )
    try:
        with observed_span("first"):
            pass
        assert entered.wait(2)
        for _ in range(8):
            with observed_span("next"):
                pass
        assert not shutdown_tracing()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_http_span_is_local_and_stays_open_until_response_body() -> None:
    from starlette.types import Message, Receive, Scope, Send

    from pr_backend_core.observability.metrics import MetricsMiddleware
    from pr_backend_core.observability.request_context import (
        correlation_fields,
        outbound_correlation_headers,
    )

    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    seen: list[dict[str, str]] = []
    sent: list[Message] = []

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(correlation_fields())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        with observed_span("llm"):
            seen.append(outbound_correlation_headers())
            await send({"type": "http.response.body", "body": b"part", "more_body": True})
        await send({"type": "http.response.body", "body": b"done", "more_body": False})
        assert current_span_fields()["span_id"] == seen[0]["span_id"]

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    incoming = f"00-{'1' * 32}-{'2' * 16}-01"
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/synthetic",
        "headers": [(b"traceparent", incoming.encode()), (b"authorization", b"private-payload")],
        "query_string": b"private-payload",
        "scheme": "http",
        "server": ("test", 80),
    }
    await MetricsMiddleware(application, enable_metrics=False)(scope, receive, send)
    assert seen[0]["trace_id"] == "1" * 32
    assert seen[0]["span_id"] != "2" * 16
    assert seen[1]["traceparent"].split("-")[2] != seen[0]["span_id"]
    assert "Authorization" not in seen[1]
    assert current_span_fields() == {}
    assert shutdown_tracing()
    spans = exporter.get_finished_spans()
    server = next(span for span in spans if span.name == "http.request")
    assert server.parent and server.parent.span_id == int("2" * 16, 16)
    assert server.end_time and server.end_time >= max(span.end_time or 0 for span in spans)
    assert all("private-payload" not in str(span.attributes) for span in spans)


def test_terminal_snapshot_finishes_only_its_own_operation_once() -> None:
    from pr_backend_core.observability.tracing import finish_operation_trace

    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    with operation_trace("task", operation_id="operation-a") as summary:
        with observed_span("llm"):
            pass
        assert finish_operation_trace("operation-b") is None
        saved = finish_operation_trace("operation-a", status="failed")
        assert saved is not None
        assert len(saved.spans) == 2
        assert (
            next(span for span in saved.spans if span.span_id == saved.root_span_id).status
            == "failed"
        )
    assert len(summary.spans) == 2
    assert shutdown_tracing()
    assert len(exporter.get_finished_spans()) == 2


def test_exporter_failure_does_not_log_response_or_exception_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    class FailingExporter(SpanExporter):
        def export(self, spans: object) -> SpanExportResult:
            logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").error(
                "collector rejected: %s", "private-payload"
            )
            raise ValueError("private-payload")

        def shutdown(self) -> None:
            pass

    configure_tracing(TracingSettings(), service_name="synthetic", exporter=FailingExporter())
    with observed_span("synthetic"):
        pass
    assert shutdown_tracing()
    assert "trace_export_failed" in caplog.text
    assert "private-payload" not in caplog.text


def test_usage_keeps_unknown_separate_from_zero_and_does_not_double_count() -> None:
    from pr_backend_core.observability.genai import record_genai_usage

    configure_tracing(TracingSettings(), service_name="synthetic")
    with operation_trace("task", operation_id="synthetic") as summary:
        with observed_span("known") as span:
            record_genai_usage(
                span,
                {
                    "input_tokens": 12,
                    "output_tokens": 0,
                    "input_tokens_details": {"cached_tokens": 4},
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            )
        with observed_span("unknown") as span:
            record_genai_usage(span, {"input_tokens": True, "output_tokens": -1})
    known = next(item for item in summary.spans if item.operation_name == "known")
    unknown = next(item for item in summary.spans if item.operation_name == "unknown")
    assert known.attributes["gen_ai.usage.input_tokens"] == 12
    assert known.attributes["gen_ai.usage.cache_read.input_tokens"] == 4
    assert known.attributes["gen_ai.usage.output_tokens"] == 0
    assert "gen_ai.usage.reasoning.output_tokens" not in known.attributes
    assert not unknown.attributes


@pytest.mark.asyncio
async def test_cancelled_operation_restores_parent_and_keeps_cancelled_status() -> None:
    configure_tracing(TracingSettings(), service_name="synthetic")
    with observed_span("parent"):
        parent = capture_trace_context()
        with (
            pytest.raises(asyncio.CancelledError),
            operation_trace("task", operation_id="synthetic") as summary,
            observed_span("child"),
        ):
            raise asyncio.CancelledError()
        assert capture_trace_context() == parent
    assert all(span.status == "cancelled" for span in summary.spans)


def test_otlp_http_collector_contract_keeps_real_parent_ids() -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    received: list[tuple[str, bytes]] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        configure_tracing(
            TracingSettings(trace_otlp_endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces"),
            service_name="synthetic",
        )
        with (
            operation_trace("task", operation_id="synthetic") as summary,
            observed_span("llm", attributes={"prompt": "private-payload"}),
        ):
            pass
        assert shutdown_tracing()
        assert len(received) == 1 and received[0][0] == "/v1/traces"
        payload = ExportTraceServiceRequest.FromString(received[0][1])
        spans = payload.resource_spans[0].scope_spans[0].spans
        root = next(span for span in spans if span.name == "task")
        child = next(span for span in spans if span.name == "llm")
        assert child.trace_id == root.trace_id
        assert child.parent_span_id == root.span_id
        assert child.end_time_unix_nano >= child.start_time_unix_nano
        assert root.trace_id.hex() == summary.telemetry_trace_id
        assert b"private-payload" not in received[0][1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_unfinished_child_is_partial_and_keeps_original_capture() -> None:
    configure_tracing(TracingSettings(), service_name="synthetic")
    with (
        operation_trace("task", operation_id="synthetic") as summary,
        observed_span("stream", finish_on_exit=False) as child,
    ):
        pass
    assert summary.completeness == "partial"
    child.finish()
    assert len(summary.spans) == 2
    assert summary.completeness == "partial"


def test_trace_schema_matches_published_contract() -> None:
    import json
    from pathlib import Path

    from pr_backend_core.observability.tracing import TraceSummary

    contract = Path(__file__).resolve().parents[3] / "contracts/trace/trace-summary.schema.json"
    assert json.loads(contract.read_text()) == TraceSummary.model_json_schema()


def test_span_updates_stream_display_without_exporting_it() -> None:
    """span の開始・表示用のデータ・終了を流し、表示用のデータは export しない（#3226）。"""
    from datetime import UTC, datetime, timedelta

    from pr_backend_core.chat_progress import chat_progress_details
    from pr_backend_core.observability.tracing import SpanDisplay, SpanUpdate, record_span

    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    updates: list[SpanUpdate] = []
    with operation_trace(
        "task", operation_id="operation-a", listener=updates.append, display=SpanDisplay(kind="run")
    ) as summary:
        with observed_span("stage", attributes={"pr.stage": "first"}) as span:
            span.set_display(
                kind="first",
                params={"count": 2},
                details=chat_progress_details(fields=[("条件", "synthetic-detail")]),
            )
            span.set_display(params={"note": "完了"})
        now = datetime.now(UTC)
        record_span("wait", started_at=now - timedelta(seconds=2), finished_at=now)
    root_id = summary.root_span_id
    assert updates[0].root and updates[0].status == "running" and updates[0].span_id == root_id
    stage = [update for update in updates if update.operation_name == "stage"]
    assert [update.status for update in stage] == ["running", "running", "running", "done"]
    assert stage[-1].display is not None
    assert stage[-1].display.kind == "first"
    assert stage[-1].display.params == {"count": 2, "note": "完了"}
    assert stage[-1].parent_span_id == root_id and not stage[-1].root
    wait = next(update for update in updates if update.operation_name == "wait")
    assert wait.status == "done" and wait.finished_at is not None
    assert abs((wait.finished_at - wait.started_at).total_seconds() - 2) < 0.01
    assert updates[-1].span_id == root_id and updates[-1].status == "done"
    recorded = {span.operation_name: span for span in summary.spans}
    assert recorded["stage"].display is not None
    assert recorded["stage"].display.details is not None
    assert recorded["task"].display == SpanDisplay(kind="run")
    assert shutdown_tracing()
    exported = exporter.get_finished_spans()
    assert {span.name for span in exported} == {"task", "stage", "wait"}
    assert all("synthetic-detail" not in str(span.attributes) for span in exported)


def test_ui_record_does_not_depend_on_export_settings() -> None:
    """export が無効・未設定でも、表示用の記録と span のイベントは作る（#3226）。"""
    for configure in (False, True):
        if configure:
            configure_tracing(TracingSettings(trace_enabled=False), service_name="synthetic")
        updates: list[object] = []
        with (
            operation_trace("task", operation_id="operation-a", listener=updates.append) as summary,
            observed_span("llm"),
        ):
            pass
        assert summary.completeness == "complete"
        assert not summary.sampled
        assert {span.operation_name for span in summary.spans} == {"task", "llm"}
        assert len(updates) == 4
        shutdown_tracing()


def test_span_updates_are_bounded_and_listener_failure_does_not_stop_work() -> None:
    configure_tracing(TracingSettings(trace_ui_max_spans=2), service_name="synthetic")
    seen: list[str] = []

    def listener(update: object) -> None:
        seen.append(getattr(update, "operation_name", ""))
        raise RuntimeError("synthetic")

    with operation_trace("task", operation_id="operation-a", listener=listener):
        for _ in range(5):
            with observed_span("llm"):
                pass
    # root と 1 つ目の span だけ（開始と終了）を知らせ、上限を超えた span は知らせない。
    assert seen == ["task", "llm", "llm", "task"]


def test_observe_spans_sees_spans_with_or_without_an_operation_trace() -> None:
    """`observe_spans` は記録の外の要求でも span を知らせ、記録の知らせは変えない（#3552）。"""
    from datetime import UTC, datetime, timedelta

    from pr_backend_core.observability.tracing import SpanUpdate, observe_spans, record_span

    outside: list[SpanUpdate] = []
    with observe_spans(outside.append):
        with observed_span("llm"):
            pass
        now = datetime.now(UTC)
        record_span("wait", started_at=now - timedelta(seconds=1), finished_at=now)
    with observed_span("after"):
        pass
    assert [(update.operation_name, update.status) for update in outside] == [
        ("llm", "running"),
        ("llm", "done"),
        ("wait", "done"),
    ]
    assert not any(update.root for update in outside)

    recorded: list[SpanUpdate] = []
    observed: list[SpanUpdate] = []

    def failing(update: SpanUpdate) -> None:
        observed.append(update)
        raise RuntimeError("synthetic")

    with (
        operation_trace("task", operation_id="operation-a", listener=recorded.append) as summary,
        observe_spans(failing),
        observed_span("llm"),
    ):
        pass
    # 記録の listener は root を含めて今どおり受け、observer の失敗は処理を止めない。
    assert [update.operation_name for update in recorded] == ["task", "llm", "llm", "task"]
    assert [update.operation_name for update in observed] == ["llm", "llm"]
    assert {span.operation_name for span in summary.spans} == {"task", "llm"}


@pytest.fixture(autouse=True)
def enable_trace_content_for_snapshot_tests():
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
