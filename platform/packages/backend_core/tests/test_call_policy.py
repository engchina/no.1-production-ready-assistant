"""本文は保存前に止め、呼び出しの開始時の判断を保つ。"""

import json
from datetime import UTC, datetime

from pr_backend_core.chat_progress import ChatProgressStepDetails
from pr_backend_core.observability.call_policy import call_content_policy, capture_call_content
from pr_backend_core.observability.call_stream import CallStreamCapture
from pr_backend_core.observability.tracing import (
    SpanDisplay,
    observed_span,
    operation_trace,
    record_span,
)


def test_default_is_off_and_body_factories_are_never_called():
    def forbidden():
        raise AssertionError("本文を複製してはいけない")

    with (
        call_content_policy(lambda: False),
        operation_trace("run", operation_id="synthetic") as result,
    ):
        with observed_span("gen_ai.chat", attributes={"gen_ai.request.model": "synthetic"}) as span:
            span.set_call_input(forbidden)
            span.set_call_output(forbidden)
        record_span(
            "stage",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            display=SpanDisplay(details=ChatProgressStepDetails(lines=["private-synthetic"])),
        )
    call = next(item for item in result.spans if item.operation_name == "gen_ai.chat")
    assert call.display.details.call.capture_enabled is False
    assert call.attributes["gen_ai.request.model"] == "synthetic"
    assert "private-synthetic" not in json.dumps(
        [item.model_dump(mode="json") for item in result.spans]
    )


def test_policy_is_fixed_for_the_call_and_metadata_remains():
    enabled = [True]
    with (
        call_content_policy(lambda: enabled[0]),
        operation_trace("run", operation_id="synthetic") as result,
    ):
        with observed_span("gen_ai.chat") as span:
            span.set_call_input({"input": "合成の質問"})
            enabled[0] = False
            span.set_call_output({"output": "合成の結果"})
        with observed_span("gen_ai.chat") as disabled:
            disabled.set_call_input({"input": "private"})
    calls = [
        item.display.details.call for item in result.spans if item.operation_name == "gen_ai.chat"
    ]
    assert "合成の結果" in calls[0].output.text
    assert calls[1].capture_enabled is False and calls[1].input.text == ""


def test_stream_off_never_creates_a_checkpoint_and_policy_failure_is_closed():
    snapshots = []
    capture = CallStreamCapture(snapshots.append, enabled=False)
    for _ in range(100):
        capture.feed({"type": "response.output_text.delta", "delta": "private"})
    capture.flush(interrupted=True)
    assert not capture.has_content and not snapshots

    def fail():
        raise RuntimeError("private")

    with call_content_policy(fail):
        assert capture_call_content() is False
