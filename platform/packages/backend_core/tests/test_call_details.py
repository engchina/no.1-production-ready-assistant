"""呼び出しの snapshot・失敗・機密除去と外部 telemetry の境界。"""

import asyncio
import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_backend_core.chat_progress import ChatProgressStepDetails
from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.call_details import (
    call_content,
    call_input,
    withhold_call_details,
)
from pr_backend_core.observability.tracing import (
    configure_tracing,
    observed_span,
    operation_trace,
    shutdown_tracing,
)


def test_snapshot_is_copied_bounded_and_redacted_before_clipping() -> None:
    request = {
        "input": "synthetic-input",
        "api_key": "private-key",
        "headers": {"Authorization": "private-auth"},
        "image_url": "data:image/png;base64,private-image",
        "text": {"format": {"type": "json_schema"}},
    }
    snapshot = call_input(request)
    request["input"] = "changed"
    dumped = snapshot.model_dump()
    assert "schema" in dumped and "schema_" not in dumped
    assert all(
        secret not in json.dumps(dumped)
        for secret in ["private-key", "private-auth", "private-image", "changed"]
    )
    assert "synthetic-input" in snapshot.input.text
    assert snapshot.output.status == "not_recorded"
    long = call_content({"input": "x" * 100000, "password": "private-password"})
    assert long.status == "truncated" and len(long.text) <= 64000
    assert json.loads(long.text)["input"].startswith("xxx")
    assert "private-password" not in long.text
    serialized = call_content(json.dumps({"output": "x" * 100000}))
    assert serialized.status == "truncated"
    assert json.loads(serialized.text)["output"].startswith("xxx")
    value = ChatProgressStepDetails(call=snapshot).model_dump()
    assert ChatProgressStepDetails.model_validate(value).model_dump() == value


def test_call_payload_is_in_product_details_but_never_exported() -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    try:
        with (
            operation_trace("run", operation_id="a") as result,
            observed_span("gen_ai.chat") as span,
        ):
            span.set_call_input({"input": "private-synthetic-content"})
            span.set_call_output({"output": "synthetic-answer"})
        call = next(s for s in result.spans if s.operation_name == "gen_ai.chat")
        assert call.display and call.display.details and call.display.details.call
        assert "synthetic-answer" in call.display.details.call.output.text
    finally:
        shutdown_tracing()
    assert all(
        "private-synthetic-content" not in str(s.attributes) and not s.events
        for s in exporter.get_finished_spans()
    )


@pytest.mark.asyncio
async def test_failure_and_parallel_calls_preserve_their_own_inputs() -> None:
    configure_tracing(TracingSettings(), service_name="synthetic")

    async def call(value: str) -> None:
        with observed_span("gen_ai.chat") as span:
            span.set_call_input({"input": value})
            await asyncio.sleep(0)
            if value == "left":
                raise asyncio.CancelledError()
            span.set_call_output("right-output")

    try:
        with operation_trace("run", operation_id="a") as result:
            await asyncio.gather(call("left"), call("right"), return_exceptions=True)
        calls = [s for s in result.spans if s.operation_name == "gen_ai.chat"]
        assert {s.status for s in calls} == {"cancelled", "done"}
        for s in calls:
            assert s.display and s.display.details and s.display.details.call
            c = s.display.details.call
            assert ("CancelledError" in c.output.text) == ("left" in c.input.text)
    finally:
        shutdown_tracing()


def test_restricted_snapshots_are_withheld_without_mutating_saved_record() -> None:
    call = call_input({"input": "private-synthetic-content"}).model_copy(
        update={"visibility": "system_admin"}
    )
    saved = {"span": ChatProgressStepDetails(call=call).model_dump()}
    hidden = withhold_call_details(saved, allow_restricted=False)
    assert "private-synthetic-content" not in json.dumps(hidden)
    assert hidden["span"]["call"]["input"]["status"] == "withheld"
    assert withhold_call_details(saved, allow_restricted=True) == saved
    assert "private-synthetic-content" in json.dumps(saved)


def test_media_is_removed_from_mcp_blocks_and_serialized_tool_results() -> None:
    block = {"content": [{"type": "image", "data": "synthetic-base64", "mimeType": "image/png"}]}
    snapshot = call_content({"result": block, "output": json.dumps(block)})
    assert "synthetic-base64" not in snapshot.text
    assert "image/png" in snapshot.text
    assert "[media omitted]" in snapshot.text


@pytest.fixture(autouse=True)
def enable_trace_content_for_snapshot_tests():
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
