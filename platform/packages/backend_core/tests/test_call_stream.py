"""受信済み prefix の保持と、delta の数に比例しない checkpoint。"""

import json

from pr_backend_core.observability import call_stream
from pr_backend_core.observability.call_details import call_content
from pr_backend_core.observability.call_stream import CallStreamCapture


def test_stream_coalesces_deltas_and_keeps_thinking_on_interruption(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(call_stream.time, "monotonic", lambda: clock[0])
    snapshots = []
    capture = CallStreamCapture(lambda value: snapshots.append(call_content(value)))
    for _ in range(10000):
        capture.feed({"type": "response.reasoning_text.delta", "delta": "a", "output_index": 0})
    assert len(snapshots) == 1
    clock[0] += 5.0
    capture.feed({"type": "response.output_text.delta", "delta": "合成の回答", "output_index": 1})
    assert len(snapshots) == 2
    capture.flush(interrupted=True)
    final = json.loads(snapshots[-1].text)
    assert final["status"] == "interrupted"
    assert final["output"][0]["content"][0]["text"] == "a" * 10000
    assert final["output"][1]["content"][0]["text"] == "合成の回答"


def test_terminal_response_wins_over_prefix_and_bounded_capture_is_explicit():
    snapshots = []
    capture = CallStreamCapture(snapshots.append)
    capture.feed({"type": "response.output_text.delta", "delta": "x" * 100000})
    capture.flush(interrupted=True)
    assert snapshots[-1]["capture_truncated"] is True
    assert len(snapshots[-1]["output"][0]["content"][0]["text"]) == 64000
    final = {"status": "completed", "output": [], "usage": {"output_tokens": 3}}
    capture.feed({"type": "response.completed", "response": final})
    capture.flush(interrupted=True)
    assert snapshots[-1] is final


def test_failed_terminal_without_output_keeps_the_received_prefix():
    snapshots = []
    capture = CallStreamCapture(snapshots.append)
    capture.feed({"type": "response.output_text.delta", "delta": "合成の途中の回答"})
    capture.feed({"type": "response.failed", "response": {"status": "failed", "output": []}})
    assert snapshots[-1]["status"] == "failed"
    assert snapshots[-1]["output"][0]["content"][0]["text"] == "合成の途中の回答"
    assert snapshots[-1]["capture_partial"] is True
