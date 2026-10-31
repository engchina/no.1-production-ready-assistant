"""固定のモデル用途が、並行・入れ子・失敗で別の処理に混ざらない。"""

import asyncio
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.model_purpose import (
    MODEL_CALL_PURPOSES,
    async_call_with_model_purpose,
    call_with_model_purpose,
    model_call_purpose,
    model_purpose_attributes,
)
from pr_backend_core.observability.tracing import (
    configure_tracing,
    observed_span,
    operation_trace,
    shutdown_tracing,
)


def test_nested_purposes_restore_parent_after_failure_and_none_inherits() -> None:
    assert model_purpose_attributes() == {}
    with model_call_purpose("answer.generate"):
        with model_call_purpose(None):
            assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.generate"}
        with pytest.raises(ValueError), model_call_purpose("answer.audit"):
            assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.audit"}
            raise ValueError("合成の失敗")
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.generate"}
    assert model_purpose_attributes() == {}


@pytest.mark.parametrize("purpose", sorted(MODEL_CALL_PURPOSES))
def test_only_fixed_purposes_and_valid_defaults_are_returned(purpose: str) -> None:
    assert model_purpose_attributes(default=purpose) == {"pr.gen_ai.purpose": purpose}
    with model_call_purpose(purpose):
        assert model_purpose_attributes(default="invalid") == {"pr.gen_ai.purpose": purpose}
    assert model_purpose_attributes() == {}


@pytest.mark.parametrize(
    "purpose", ["", "untrusted text", "answer.generate.extra", "ANSWER.GENERATE"]
)
def test_invalid_purpose_clears_parent_and_never_exports_arbitrary_text(purpose: str) -> None:
    assert model_purpose_attributes(default=purpose) == {}
    with model_call_purpose("query.rewrite"):
        with model_call_purpose(purpose):
            assert model_purpose_attributes() == {}
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "query.rewrite"}
    assert model_purpose_attributes() == {}


def test_sync_wrapper_preserves_arguments_result_and_exception_identity() -> None:
    sentinel = object()
    calls: list[tuple[object, object]] = []
    error = RuntimeError("合成の失敗")

    def operation(first: object, *, second: object) -> object:
        calls.append((first, second))
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "sql.generate"}
        return sentinel

    assert call_with_model_purpose("sql.generate", operation, sentinel, second=sentinel) is sentinel
    assert len(calls) == 1 and calls[0][0] is sentinel and calls[0][1] is sentinel

    def fail() -> None:
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "sql.repair"}
        raise error

    with pytest.raises(RuntimeError) as raised:
        call_with_model_purpose("sql.repair", fail)
    assert raised.value is error
    assert model_purpose_attributes() == {}


async def test_async_wrappers_keep_concurrent_tasks_isolated_and_preserve_values() -> None:
    ready = asyncio.Event()
    entered: list[str] = []
    sentinel = object()

    async def operation(value: object, *, purpose: str) -> object:
        entered.append(purpose)
        if len(entered) == 2:
            ready.set()
        await ready.wait()
        assert value is sentinel
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": purpose}
        with model_call_purpose(None):
            await asyncio.sleep(0)
            assert model_purpose_attributes() == {"pr.gen_ai.purpose": purpose}
        return value

    with model_call_purpose("context.compact"):
        results = await asyncio.gather(
            async_call_with_model_purpose(
                "answer.generate", operation, sentinel, purpose="answer.generate"
            ),
            async_call_with_model_purpose(
                "query.expand", operation, sentinel, purpose="query.expand"
            ),
        )
        assert all(value is sentinel for value in results)
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "context.compact"}
    assert len(entered) == 2 and model_purpose_attributes() == {}


async def test_async_exception_and_cancellation_restore_calling_context() -> None:
    error = RuntimeError("合成の失敗")

    async def fail() -> None:
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.audit"}
        await asyncio.sleep(0)
        raise error

    with model_call_purpose("answer.generate"):
        with pytest.raises(RuntimeError) as raised:
            await async_call_with_model_purpose("answer.audit", fail)
        assert raised.value is error
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.generate"}

        async def cancel() -> None:
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await async_call_with_model_purpose("query.expand", cancel)
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.generate"}
    assert model_purpose_attributes() == {}


@pytest.fixture
def tracing_scope(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    from pr_backend_core.observability import tracing

    monkeypatch.setattr(tracing, "_SETTINGS", tracing._SETTINGS)
    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    try:
        yield exporter
    finally:
        shutdown_tracing()


def test_actual_model_spans_receive_context_but_non_model_spans_do_not(
    tracing_scope: InMemorySpanExporter,
) -> None:
    with operation_trace("task", operation_id="operation-a") as summary:
        with model_call_purpose("sql.generate"):
            with observed_span("oracle.select_ai"):
                pass
            with observed_span("gen_ai.chat", attributes={"gen_ai.request.model": "model-a"}):
                pass
            with observed_span("oracle.query"):
                pass
            with model_call_purpose("untrusted text"), observed_span("gen_ai.chat"):
                pass
        with observed_span("gen_ai.chat"):
            pass
    spans = summary.spans
    model_spans = [item for item in spans if item.operation_name == "oracle.select_ai"]
    assert len(model_spans) == 1
    assert model_spans[0].attributes["pr.gen_ai.purpose"] == "sql.generate"
    chat_spans = [item for item in spans if item.operation_name == "gen_ai.chat"]
    assert [item.attributes.get("pr.gen_ai.purpose") for item in chat_spans] == [
        "sql.generate",
        None,
        None,
    ]
    assert all(
        "pr.gen_ai.purpose" not in item.attributes
        for item in spans
        if item.operation_name not in {"gen_ai.chat", "oracle.select_ai"}
    )
    assert shutdown_tracing()
    exported = tracing_scope.get_finished_spans()
    assert any(
        item.name == "oracle.select_ai"
        and (item.attributes or {}).get("pr.gen_ai.purpose") == "sql.generate"
        for item in exported
    )
    assert all(
        "pr.gen_ai.purpose" not in (item.attributes or {})
        for item in exported
        if item.name in {"task", "oracle.query"}
    )


def test_wrapper_does_not_reserve_keywords_of_the_wrapped_operation() -> None:
    first, second = object(), object()

    def operation(*, purpose: object, operation: object) -> tuple[object, object]:
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "sql.generate"}
        return purpose, operation

    result = call_with_model_purpose("sql.generate", operation, purpose=first, operation=second)
    assert result[0] is first and result[1] is second
    assert model_purpose_attributes() == {}


async def test_cancelled_task_restores_its_inherited_context_without_changing_parent() -> None:
    started = asyncio.Event()
    released = asyncio.Event()
    restored: list[dict[str, str]] = []

    async def operation() -> None:
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "query.expand"}
        started.set()
        await released.wait()

    async def child() -> None:
        try:
            await async_call_with_model_purpose("query.expand", operation)
        finally:
            restored.append(model_purpose_attributes())

    with model_call_purpose("answer.generate"):
        task = asyncio.create_task(child())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert restored == [{"pr.gen_ai.purpose": "answer.generate"}]
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "answer.generate"}
    assert model_purpose_attributes() == {}


@pytest.mark.parametrize("operation", ["gen_ai.embeddings", "gen_ai.rerank"])
def test_embedding_and_rerank_probe_purpose_does_not_change_normal_calls(
    tracing_scope: InMemorySpanExporter,
    operation: str,
) -> None:
    with operation_trace("task", operation_id="probe-a") as summary:
        with observed_span(operation, attributes={"gen_ai.request.model": "model-a"}):
            pass
        with (
            model_call_purpose("connection.check"),
            observed_span(operation, attributes={"gen_ai.request.model": "model-a"}),
        ):
            pass
        with observed_span(operation):
            pass
    spans = [span for span in summary.spans if span.operation_name == operation]
    assert [span.attributes.get("pr.gen_ai.purpose") for span in spans] == [
        None,
        "connection.check",
        None,
    ]
    assert spans[0].attributes["gen_ai.request.model"] == "model-a"
    assert spans[1].attributes["gen_ai.request.model"] == "model-a"
