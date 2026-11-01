"""実際の呼び出し境界と stream の終了を、合成応答だけで検査する。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.tracing import (
    configure_tracing,
    operation_trace,
    shutdown_tracing,
)

from app import observability as obs
from app.features.agent.builtin_runtime import _ModelApiResponses


@pytest.mark.asyncio
async def test_stream_span_lives_until_consumption_and_records_known_usage() -> None:
    class Stream:
        def __init__(self) -> None:
            self.count = 0

        async def __anext__(self) -> Any:
            if self.count:
                raise StopAsyncIteration
            self.count += 1
            return SimpleNamespace(
                response=SimpleNamespace(usage={"input_tokens": 12, "output_tokens": 5})
            )

        async def close(self) -> None:
            pass

    class Client:
        async def create(self, **kwargs: Any) -> Any:
            return Stream()

    configure_tracing(TracingSettings(), service_name="synthetic")
    try:
        with operation_trace("agent.execute", operation_id="run-a") as summary:
            stream = await _ModelApiResponses(Client()).create(
                model="model-a", stream=True, input="private-payload"
            )
            assert not summary.spans
            async for _ in stream:
                assert not summary.spans
            assert len(summary.spans) == 1
        child = next(span for span in summary.spans if span.operation_name == "gen_ai.chat")
        assert child.parent_span_id == summary.root_span_id
        assert child.attributes["pr.gen_ai.purpose"] == "agent.choose_action"
        assert child.attributes["gen_ai.usage.input_tokens"] == 12
        assert child.attributes["gen_ai.usage.output_tokens"] == 5
        assert "private-payload" not in str(child.attributes)
        assert child.display and child.display.details and child.display.details.call
        assert "private-payload" in child.display.details.call.input.text
    finally:
        shutdown_tracing()


@pytest.mark.asyncio
async def test_model_call_span_carries_the_turn_kind_for_progress() -> None:
    """モデルの呼び出しの span は、応答がツールを求めたら進め方の検討、最終の回答なら回答の作成の
    工程の種類を持ち、実行中から span の知らせで届く（処理の経過。#3226）。"""
    updates: list[Any] = []

    class Client:
        def __init__(self, output: list[Any]) -> None:
            self.output = output

        async def create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(status="completed", output=self.output, usage=None)

    with operation_trace("agent.execute", operation_id="run-k", listener=updates.append):
        await _ModelApiResponses(Client([SimpleNamespace(type="function_call")])).create(
            model="model-a", input="x"
        )
        await _ModelApiResponses(Client([SimpleNamespace(type="message")])).create(
            model="model-a", input="x"
        )
    chats = [update for update in updates if update.operation_name == "gen_ai.chat"]
    # 呼び出しの間は種類が決まっていない（共通の名前「モデルの呼び出し」で出す）。
    assert (chats[0].status, chats[0].display) == ("running", None)
    assert all(update.attributes["pr.gen_ai.purpose"] == "agent.choose_action" for update in chats)
    finished = [update for update in chats if update.status == "done"]
    assert [update.display.kind if update.display else None for update in finished] == [
        "plan",
        "respond",
    ]


@pytest.mark.asyncio
async def test_streamed_model_call_takes_the_turn_kind_from_the_completed_event() -> None:
    class Stream:
        def __init__(self) -> None:
            self.sent = False

        async def __anext__(self) -> Any:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(output=[{"type": "message"}], usage=None),
            )

        async def close(self) -> None:
            pass

    class Client:
        async def create(self, **kwargs: Any) -> Any:
            return Stream()

    with operation_trace("agent.execute", operation_id="run-s") as summary:
        stream = await _ModelApiResponses(Client()).create(model="model-a", stream=True, input="x")
        async for _ in stream:
            pass
    [chat] = [span for span in summary.spans if span.operation_name == "gen_ai.chat"]
    assert chat.display is not None and chat.display.kind == "respond"


def test_initial_event_export_never_calls_transport_inline() -> None:
    obs.clear_trace_export_retry_queue()
    with (
        patch.object(
            obs, "_export_trace_event_once", side_effect=AssertionError("transport-inline")
        ),
        patch.object(
            obs, "trace_exporter_status", return_value=obs.TraceExporterStatus(configured=True)
        ),
    ):
        obs.record_runtime_event("tool.completed", {"run_id": "synthetic-a"})
    try:
        assert obs.trace_exporter_status().retry_queue_size == 1
    finally:
        obs.clear_trace_export_retry_queue()


def test_retry_only_repeats_failed_sink() -> None:
    from app.settings import get_settings

    settings = get_settings().model_copy(
        update={
            "agent_trace_exporter_url": "https://collector.example.test/events",
            "agent_opentelemetry_endpoint": "https://collector.example.test",
        }
    )
    event = obs.TraceEvent(
        id="synthetic",
        event_type="tool.completed",
        attributes={},
        created_at=datetime.now(UTC),
    )
    with (
        patch("app.settings.get_settings", return_value=settings),
        patch.object(obs, "_export_trace_event_webhook") as webhook,
        patch.object(obs, "_export_trace_event_otlp") as otlp,
    ):
        obs._export_trace_event_once(event, sinks={"otlp"})
        webhook.assert_not_called()
        otlp.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_event_transport_has_bounded_daemon_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from threading import Event

    from app.settings import get_settings

    entered, release = Event(), Event()
    settings = get_settings().model_copy(update={"trace_shutdown_timeout_seconds": 0.02})
    monkeypatch.setattr("app.settings.get_settings", lambda: settings)

    def blocked_flush(*args: object, **kwargs: object) -> obs.TraceExportRetryData:
        entered.set()
        release.wait(timeout=2)
        return obs.TraceExportRetryData()

    monkeypatch.setattr(obs, "flush_trace_export_retry_queue", blocked_flush)
    await obs.start_trace_export_retry_worker()
    worker = obs.TRACE_EXPORT_RETRY_WORKER
    try:
        assert entered.wait(timeout=1)
        start = time.monotonic()
        obs.record_runtime_event("tool.completed", {"run_id": "synthetic-independent"})
        await obs.stop_trace_export_retry_worker()
        assert time.monotonic() - start < 0.5
        assert worker is not None and worker.daemon
    finally:
        release.set()
        if worker is not None:
            worker.join(timeout=1)


def test_event_delivery_queue_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.settings import get_settings

    settings = get_settings().model_copy(update={"agent_trace_exporter_retry_queue_size": 1})
    monkeypatch.setattr("app.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        obs, "trace_exporter_status", lambda: obs.TraceExporterStatus(configured=True)
    )
    obs.clear_trace_export_retry_queue()
    try:
        for index in range(3):
            obs.record_runtime_event("tool.completed", {"run_id": f"synthetic-{index}"})
        with obs.TRACE_EXPORT_RETRY_QUEUE_LOCK:
            assert len(obs.TRACE_EXPORT_RETRY_QUEUE) == 1
            assert obs.TRACE_EXPORT_RETRY_QUEUE[0].event.run_id == "synthetic-2"
    finally:
        obs.clear_trace_export_retry_queue()


@pytest.fixture(autouse=True)
def enable_trace_content_for_existing_detail_tests() -> Any:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
