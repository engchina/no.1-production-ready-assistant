"""観測の時間・抽出・実行単位の集計（外部接続なし）。"""

from datetime import UTC, datetime
from unittest.mock import patch

from app import observability as obs


def test_completed_event_span_ends_at_completion() -> None:
    event = obs.TraceEvent(
        id="synthetic",
        event_type="tool.completed",
        run_id="run-a",
        attributes={"duration_ms": 1000},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload = obs._otlp_event_payload(event, service_name="test")
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert int(record["timeUnixNano"]) == obs._datetime_to_unix_nano(event.created_at)
    assert "resourceSpans" not in payload
    assert "spanId" not in record


def test_sampling_keeps_trace_events_together() -> None:
    for index in range(128):
        first = obs.TraceEvent(
            id="start",
            event_type="step.started",
            run_id="run-a",
            trace_id=f"{index + 1:032x}",
            attributes={},
            created_at=datetime.now(UTC),
        )
        last = first.model_copy(update={"id": "end", "event_type": "tool.completed"})
        assert obs._should_sample_trace_event(first, 0.5) == obs._should_sample_trace_event(
            last, 0.5
        )


def test_recoverable_tool_failure_is_not_a_failed_run() -> None:
    with patch.object(obs, "RUNS_TOTAL") as runs, patch.object(obs, "_append_trace_event"):
        obs.record_runtime_event("tool.failed", {"run_id": "run-a"})
        runs.labels.assert_not_called()
        obs.record_runtime_event("runtime.failed", {"run_id": "run-a"})
        runs.labels.assert_called_once_with(status="failed")
        runs.labels.return_value.inc.assert_called_once_with()
