"""チャットの処理の段階のイベントの記録・組み立て・配信（#1359）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Header, Query
from fastapi.testclient import TestClient

from pr_backend_core.chat_progress import (
    CHAT_PROGRESS_SSE_EVENT,
    CHAT_PROGRESS_SSE_HEARTBEAT_EVENT,
    UNCHANGED,
    ChatProgressPage,
    ChatProgressRecorder,
    ChatProgressStepEvent,
    ChatProgressTerminalEvent,
    chat_progress_cursor,
    chat_progress_page,
    chat_progress_sse_response,
    chat_progress_sse_stream,
    chat_progress_sse_timing,
    configure_chat_progress_sse,
    dump_chat_progress_events,
    fold_chat_progress_events,
    parse_chat_progress_events,
    restore_chat_progress_sse,
)

T0 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=UTC)


def _clock() -> Callable[[], datetime]:
    ticks = iter(range(10_000))
    return lambda: T0 + timedelta(seconds=next(ticks))


def _recorder(**kwargs: object) -> ChatProgressRecorder:
    return ChatProgressRecorder("job-1", clock=_clock(), **kwargs)  # type: ignore[arg-type]


def _statuses(recorder: ChatProgressRecorder) -> dict[str, str]:
    return {step.step_id: step.status for step in recorder.steps()}


def test_recorder_numbers_events_and_orders_steps_by_declaration() -> None:
    sink: list[object] = []
    recorder = _recorder(sink=sink.append)
    recorder.declare("queue", "generate", "execute")
    recorder.start("queue")
    recorder.start("generate", exclusive=True)
    recorder.finish("generate", params={"engine": "select_ai"})
    recorder.start("execute")
    recorder.complete("done")

    events = recorder.events
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert sink == events
    assert [step.step_id for step in recorder.steps()] == ["queue", "generate", "execute"]
    assert _statuses(recorder) == {"queue": "done", "generate": "done", "execute": "done"}
    assert isinstance(events[-1], ChatProgressTerminalEvent)
    assert recorder.terminal == "done"
    generate = recorder.step("generate")
    assert generate is not None and generate.params == {"engine": "select_ai"}
    assert generate.started_at is not None and generate.finished_at is not None


def test_recorder_never_moves_a_step_backwards() -> None:
    recorder = _recorder()
    recorder.start("retrieve")
    recorder.finish("retrieve")
    before = recorder.last_seq
    # 回答フローが前の段階の工程へ戻っても（#1358）、完了した段階を実行中に戻さない。
    assert recorder.start("retrieve") is None
    assert recorder.last_seq == before
    assert _statuses(recorder) == {"retrieve": "done"}
    # 同じ段階の間の確定（完了 → 失敗）は受け付ける。
    assert recorder.finish("retrieve", "failed") is not None
    assert _statuses(recorder) == {"retrieve": "failed"}


def test_recorder_skips_unchanged_updates() -> None:
    recorder = _recorder()
    recorder.start("plan")
    seq = recorder.last_seq
    assert recorder.start("plan") is None
    assert recorder.update("plan") is None
    assert recorder.update("missing", detail="x") is None
    assert recorder.last_seq == seq
    assert recorder.update("plan", detail="2 回目") is not None


def test_complete_finishes_running_and_skips_pending() -> None:
    recorder = _recorder()
    recorder.declare("a", "b", "c")
    recorder.start("a")
    recorder.complete("failed")
    assert _statuses(recorder) == {"a": "failed", "b": "skipped", "c": "skipped"}
    skipped = recorder.step("b")
    assert skipped is not None and skipped.finished_at is None
    # 終端の後は記録しない。
    assert recorder.start("c") is None
    assert recorder.complete("done") is None


def test_cancel_marks_running_steps_skipped() -> None:
    recorder = _recorder()
    recorder.start("a")
    recorder.complete("cancelled")
    step = recorder.step("a")
    assert step is not None and step.status == "skipped" and step.finished_at is not None


def test_recorder_resumes_from_saved_events() -> None:
    first = _recorder()
    first.declare("a", "b")
    first.start("a")
    saved = parse_chat_progress_events(
        json.loads(json.dumps(dump_chat_progress_events(first.events)))
    )

    resumed = ChatProgressRecorder("job-1", events=saved, clock=_clock())
    assert resumed.last_seq == first.last_seq
    assert _statuses(resumed) == {"a": "running", "b": "pending"}
    event = resumed.start("b", exclusive=True)
    assert event is not None and event.seq == first.last_seq + 2  # a の完了の後


def test_new_attempt_resets_steps_and_keeps_numbering() -> None:
    recorder = _recorder()
    recorder.declare("a", "b")
    recorder.start("a")
    seq = recorder.last_seq
    recorder.new_attempt(2)
    assert recorder.steps() == []
    recorder.declare("a", "b")
    assert recorder.events[-1].seq == seq + 2
    assert recorder.events[-1].attempt == 2
    fold = fold_chat_progress_events(recorder.events)
    assert fold.attempt == 2
    assert {step.step_id: step.status for step in fold.step_list()} == {
        "a": "pending",
        "b": "pending",
    }
    # 古い試行の番号は受け付けない。
    recorder.new_attempt(1)
    assert recorder.attempt == 2


def test_recorder_with_larger_attempt_than_saved_starts_a_new_attempt() -> None:
    first = _recorder()
    first.start("a")
    resumed = ChatProgressRecorder("job-1", attempt=3, events=first.events, clock=_clock())
    assert resumed.steps() == []
    resumed.start("a")
    assert resumed.events[-1].attempt == 3


def _step(seq: int, step_id: str, status: str, *, attempt: int = 0) -> ChatProgressStepEvent:
    return ChatProgressStepEvent.model_validate(
        {
            "seq": seq,
            "target_id": "job-1",
            "attempt": attempt,
            "emitted_at": T0.isoformat(),
            "step_id": step_id,
            "status": status,
        }
    )


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        pytest.param(
            [_step(2, "a", "done"), _step(1, "a", "running")], {"a": "done"}, id="out-of-order"
        ),
        pytest.param(
            [_step(1, "a", "running"), _step(1, "a", "running"), _step(2, "b", "running")],
            {"a": "running", "b": "running"},
            id="duplicate",
        ),
        pytest.param(
            [_step(1, "a", "done"), _step(2, "a", "running")], {"a": "done"}, id="backwards"
        ),
        pytest.param(
            [_step(1, "a", "done"), _step(2, "a", "running", attempt=1)],
            {"a": "running"},
            id="new-attempt",
        ),
        pytest.param(
            [_step(1, "a", "running", attempt=1), _step(2, "b", "running", attempt=0)],
            {"a": "running"},
            id="old-attempt",
        ),
    ],
)
def test_fold_drops_old_duplicate_and_backward_events(
    events: list[ChatProgressStepEvent], expected: dict[str, str]
) -> None:
    fold = fold_chat_progress_events(events)
    assert {step.step_id: step.status for step in fold.step_list()} == expected


def test_parse_drops_malformed_items() -> None:
    raw = [
        {"seq": 1, "target_id": "t", "emitted_at": T0.isoformat(), "type": "x"},
        {
            "seq": 0,
            "target_id": "t",
            "emitted_at": T0.isoformat(),
            "step_id": "a",
            "status": "running",
        },
        "broken",
        _step(1, "a", "running").model_dump(mode="json"),
    ]
    assert [event.seq for event in parse_chat_progress_events(raw)] == [1]
    assert parse_chat_progress_events({"steps": []}) == []


def test_page_returns_events_after_since() -> None:
    recorder = _recorder()
    recorder.declare("a", "b")
    recorder.start("a")
    page = chat_progress_page("job-1", recorder.events, since=2)
    assert [event.seq for event in page.events] == [3]
    assert page.last_seq == 3 and not page.terminal
    recorder.complete()
    assert recorder.page(since=99).terminal is True
    assert recorder.page(since=99).events == []


@pytest.mark.parametrize(
    ("since", "header", "expected"),
    [
        pytest.param(None, None, 0, id="none"),
        pytest.param(3, None, 3, id="since"),
        pytest.param(3, "7", 7, id="header-newer"),
        pytest.param(9, "7", 9, id="since-newer"),
        pytest.param(None, "x", 0, id="broken-header"),
        pytest.param(-1, None, 0, id="negative"),
    ],
)
def test_cursor_takes_the_newer_position(
    since: int | None, header: str | None, expected: int
) -> None:
    assert chat_progress_cursor(since=since, last_event_id=header) == expected


def _parse_sse(text: str) -> list[dict[str, str]]:
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            key, _, value = line.partition(": ")
            frame[key] = value
        frames.append(frame)
    return frames


async def test_sse_stream_sends_new_events_heartbeats_and_closes_on_terminal() -> None:
    recorder = _recorder()
    recorder.declare("a")
    pages = 0

    async def fetch(cursor: int) -> ChatProgressPage:
        nonlocal pages
        pages += 1
        if pages == 3:
            recorder.start("a")
        if pages == 5:
            recorder.complete()
        return recorder.page(since=cursor)

    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    chunks = [
        chunk
        async for chunk in chat_progress_sse_stream(
            fetch,
            since=0,
            poll_seconds=5,
            heartbeat_seconds=4,
            sleep=sleep,
            monotonic=lambda: now[0],
        )
    ]
    frames = _parse_sse("".join(chunks))
    assert frames[0] == {"retry": "2000"}
    progress = [frame for frame in frames if frame.get("event") == CHAT_PROGRESS_SSE_EVENT]
    assert [frame["id"] for frame in progress] == [str(event.seq) for event in recorder.events]
    assert json.loads(progress[-1]["data"])["type"] == "terminal"
    heartbeats = [f for f in frames if f.get("event") == CHAT_PROGRESS_SSE_HEARTBEAT_EVENT]
    assert heartbeats and json.loads(heartbeats[0]["data"]) == {"last_seq": 1}


async def test_sse_stream_resumes_after_cursor_and_ends_when_target_is_gone() -> None:
    recorder = _recorder()
    recorder.declare("a", "b", "c")
    calls = 0

    async def fetch(cursor: int) -> ChatProgressPage | None:
        nonlocal calls
        calls += 1
        return recorder.page(since=cursor) if calls == 1 else None

    async def sleep(_: float) -> None:
        return None

    chunks = [chunk async for chunk in chat_progress_sse_stream(fetch, since=1, sleep=sleep)]
    ids = [f["id"] for f in _parse_sse("".join(chunks)) if "id" in f]
    assert ids == ["2", "3"]


async def test_sse_stream_closes_after_max_seconds() -> None:
    recorder = _recorder()
    recorder.start("a")
    now = [0.0]

    async def fetch(cursor: int) -> ChatProgressPage:
        return recorder.page(since=cursor)

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    chunks = [
        chunk
        async for chunk in chat_progress_sse_stream(
            fetch, poll_seconds=1, max_seconds=3, sleep=sleep, monotonic=lambda: now[0]
        )
    ]
    assert len([c for c in chunks if c.startswith("id: ")]) == 1


def _app(recorder: ChatProgressRecorder) -> FastAPI:
    app = FastAPI()

    async def fetch(cursor: int) -> ChatProgressPage:
        return recorder.page(since=cursor)

    @app.get("/progress/stream")
    async def stream(
        since: int | None = Query(default=None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> object:
        return await chat_progress_sse_response(
            fetch, since=since, last_event_id=last_event_id, poll_seconds=0.01
        )

    return app


def test_sse_response_streams_and_returns_204_when_finished() -> None:
    recorder = _recorder()
    recorder.declare("a")
    recorder.start("a")
    recorder.complete()
    client = TestClient(_app(recorder))

    response = client.get("/progress/stream", params={"since": 1})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    ids = [f["id"] for f in _parse_sse(response.text) if "id" in f]
    assert ids == [str(seq) for seq in range(2, recorder.last_seq + 1)]

    # 張り直し（`Last-Event-ID` が終端の番号）は 204。ブラウザの EventSource は張り直しをやめる。
    finished = client.get("/progress/stream", headers={"Last-Event-ID": str(recorder.last_seq)})
    assert finished.status_code == 204


def test_unchanged_keeps_values_and_none_clears_them() -> None:
    recorder = _recorder()
    recorder.start("a", detail="1 回目", params={"attempt": 1})
    recorder.update("a", detail=UNCHANGED, params={"attempt": 2})
    step = recorder.step("a")
    assert step is not None and step.detail == "1 回目" and step.params == {"attempt": 2}
    recorder.finish("a", detail=None)
    step = recorder.step("a")
    assert step is not None and step.detail is None and step.params == {"attempt": 2}


def test_serialized_events_and_pages_omit_absent_fields() -> None:
    recorder = _recorder()
    recorder.declare("a")
    event = recorder.events[0].model_dump(mode="json")
    assert "detail" not in event and "started_at" not in event and "kind" not in event
    page = json.loads(recorder.page().model_dump_json())
    assert all(None not in item.values() for item in page["events"])
    assert page["events"][0]["step_id"] == "a"


def test_configure_sse_timing_changes_defaults_and_restores() -> None:
    with pytest.raises(ValueError):
        configure_chat_progress_sse(poll_seconds=0)
    previous = configure_chat_progress_sse(poll_seconds=0.01, heartbeat_seconds=0.02)
    try:
        timing = chat_progress_sse_timing()
        assert (timing.poll_seconds, timing.heartbeat_seconds) == (0.01, 0.02)
        assert timing.max_seconds == previous.max_seconds
    finally:
        restore_chat_progress_sse(previous)
    assert chat_progress_sse_timing() == previous


async def test_sse_stream_uses_configured_timing() -> None:
    recorder = _recorder()
    recorder.start("a")
    now = [0.0]
    slept: list[float] = []

    async def fetch(cursor: int) -> ChatProgressPage:
        return recorder.page(since=cursor)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    previous = configure_chat_progress_sse(poll_seconds=2, max_seconds=5)
    try:
        chunks = [
            chunk
            async for chunk in chat_progress_sse_stream(
                fetch, sleep=sleep, monotonic=lambda: now[0]
            )
        ]
    finally:
        restore_chat_progress_sse(previous)
    assert slept and set(slept) == {2}
    assert len([c for c in chunks if c.startswith("id: ")]) == 1


# ---------------------------------------------------------------------------
# 段の詳細（#2294）
# ---------------------------------------------------------------------------


def test_step_details_are_recorded_folded_and_round_trip() -> None:
    from pr_backend_core.chat_progress import chat_progress_details

    recorder = ChatProgressRecorder("job-d", clock=_clock())
    recorder.start("generate_sql")
    details = chat_progress_details(
        fields=[("結果", "安全"), ("参照した表", "T1, T2", True), ("無い値", None)],
        lines=["警告の行"],
        code=[("生成した SQL", "SELECT 1 FROM DUAL", "sql")],
    )
    assert details is not None
    event = recorder.finish("generate_sql", details=details)
    assert event is not None
    dumped = dump_chat_progress_events(recorder.events)
    assert dumped[-1]["details"] == {
        "fields": [
            {"label": "結果", "value": "安全"},
            {"label": "参照した表", "value": "T1, T2", "mono": True},
        ],
        "lines": ["警告の行"],
        "code": [{"label": "生成した SQL", "language": "sql", "text": "SELECT 1 FROM DUAL"}],
    }
    restored = parse_chat_progress_events(dumped)
    assert fold_chat_progress_events(restored).steps["generate_sql"].details == details
    # 同じ詳細の更新は記録しない。詳細だけの変化は記録する（状態は変えない）。
    assert recorder.update("generate_sql", details=details) is None
    changed = recorder.update("generate_sql", details=chat_progress_details(lines=["別の行"]))
    assert changed is not None and changed.status == "done"


def test_step_details_are_bounded_and_secrets_are_masked() -> None:
    from pr_backend_core.chat_progress import (
        DETAILS_CODE_MAX,
        DETAILS_MAX_LINES,
        ChatProgressStepDetails,
        chat_progress_details,
    )

    details = chat_progress_details(
        lines=[f"行 {index}" for index in range(DETAILS_MAX_LINES + 5)],
        code=[("JSON", '{"api_key": "abc", "password": "p@ss", "n": 1}', "json")],
    )
    assert details is not None
    assert len(details.lines) == DETAILS_MAX_LINES
    assert details.truncated is True
    assert details.code[0].text == '{"api_key": "***", "password": "***", "n": 1}'

    masked = chat_progress_details(
        lines=[
            "token=abc123",
            "Authorization: Bearer xyz.abc",
            "key prak_ABC_def",
            "https://u:p@h/x",
        ]
    )
    assert masked is not None
    assert masked.lines == [
        "token=***",
        "Authorization: Bearer ***",
        "key ***",
        "https://***@h/x",
    ]
    assert masked.truncated is None

    # 保存済みの長すぎる記録も捨てずに切って読む（イベントが壊れて段が消えない）。
    long_code = ChatProgressStepDetails.model_validate(
        {"code": [{"text": "x" * (DETAILS_CODE_MAX + 100)}]}
    )
    assert len(long_code.code[0].text) == DETAILS_CODE_MAX + 1
    assert long_code.truncated is True
    assert chat_progress_details() is None


def test_record_span_turns_trace_into_progress_events() -> None:
    """span の開始・変更・終了を段のイベントにし、終端まで同じ記録で流す（#3226）。"""
    import asyncio

    import pytest

    from pr_backend_core.chat_progress import ChatProgressRecorder, chat_progress_details
    from pr_backend_core.observability.tracing import observed_span, operation_trace

    recorder = ChatProgressRecorder("target-a")
    from pr_backend_core.observability.call_policy import call_content_policy

    with (
        call_content_policy(lambda: True),
        operation_trace("task", operation_id="target-a", listener=recorder.record_span) as summary,
    ):
        with observed_span("stage") as stage:
            stage.set_display(
                kind="first", params={"count": 1}, details=chat_progress_details(lines=["行"])
            )
        with pytest.raises(asyncio.CancelledError), observed_span("stopped"):
            raise asyncio.CancelledError
    recorder.complete("done")
    steps = {step.step_id: step for step in recorder.steps()}
    root = steps[summary.root_span_id or ""]
    assert root.span is not None and root.span["root"] is True and root.status == "done"
    first = next(step for step in steps.values() if step.span and step.span["name"] == "stage")
    assert first.kind == "first" and first.params == {"count": 1}
    assert first.details is not None and first.details.lines == ["行"]
    assert first.span is not None and first.span["parent_id"] == summary.root_span_id
    stopped = next(step for step in steps.values() if step.span and step.span["name"] == "stopped")
    assert stopped.status == "skipped" and stopped.span is not None
    assert stopped.span["cancelled"] is True
    # 保存した JSON から読み直しても span の項目が残る。
    from pr_backend_core.chat_progress import dump_chat_progress_events, parse_chat_progress_events

    events = parse_chat_progress_events(dump_chat_progress_events(recorder.events))
    assert any(getattr(event, "parent_id", None) == summary.root_span_id for event in events)
    assert events[-1].type == "terminal"


def test_complete_cancelled_marks_running_spans_as_cancelled() -> None:
    """停止の終端は、開いていた span の段を「停止」（skipped と cancelled）で閉じる（#3226）。"""
    from pr_backend_core.chat_progress import ChatProgressRecorder
    from pr_backend_core.observability.tracing import observed_span, operation_trace

    recorder = ChatProgressRecorder("target-b")
    with (
        operation_trace("task", operation_id="target-b", listener=recorder.record_span),
        observed_span("stage", finish_on_exit=False),
    ):
        recorder.complete("cancelled")
    stage = next(step for step in recorder.steps() if step.span and step.span["name"] == "stage")
    assert stage.status == "skipped"
    assert stage.span is not None and stage.span["cancelled"] is True
    assert recorder.terminal == "cancelled"
