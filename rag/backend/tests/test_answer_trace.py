"""回答の処理の経過（共通の span の表。#3226 の案 A）の記録のテスト。

回答の pipeline と同じ span（実行の全体・パイプラインの段・回答フローの工程）の知らせが、処理の
経過の段（親子・表示用のデータ・状態・工程の詳細）になること、`AnswerTrace` が表の書き手へ渡し、
製品の記録に置く値（trace id・工程の詳細・書けなかった数）を集めること、回答の thread からの知らせ
をループで知らせることを確かめる。実 DB は使わない。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Iterator

import pytest
from pr_backend_core.chat_progress import ChatProgressStepEvent
from pr_backend_core.observability.trace_store import TraceSpanWriter

from app.rag import trace_store
from app.rag.span_display import ANSWER_STEP_KIND
from app.rag.trace_store import AnswerTrace
from tests.span_progress_support import (
    RecordingTrace,
    answer_step,
    answer_trace,
    pipeline_stage,
    span_rows,
    span_steps,
)

pytestmark = pytest.mark.usefixtures("trace_body_enabled")

MESSAGE_ID = "a" * 32


async def test_records_root_stage_and_answer_steps_as_a_span_tree() -> None:
    """実行の全体・パイプラインの段・回答フローの工程を、親子つきの段として記録する。"""
    tracker = RecordingTrace(MESSAGE_ID, model_id="m1")
    with answer_trace(tracker):
        await pipeline_stage("history_rewrite")

        async def answer() -> None:
            with answer_step("質問の理解"):
                pass
            with answer_step("文書検索"), answer_step("Rerank"):
                pass

        await pipeline_stage("answer", answer())
        await pipeline_stage("answer_record_save")

    assert span_rows(tracker.events) == [
        # 始まった順（包む段の後に中の工程）。
        ("rag.history_rewrite", "history_rewrite", None, "done"),
        ("rag.answer", "", None, "done"),
        ("rag.engine.stage", ANSWER_STEP_KIND, "質問の理解", "done"),
        ("rag.engine.stage", ANSWER_STEP_KIND, "文書検索", "done"),
        ("rag.engine.stage", ANSWER_STEP_KIND, "Rerank", "done"),
        ("rag.answer_record_save", "answer_record_save", None, "done"),
    ]
    steps = {event.step_id: event for event in span_steps(tracker.events)}
    root = next(event for event in steps.values() if event.root)
    assert root.name == "rag.answer"
    assert root.trace_id is not None
    by_name = {(event.params or {}).get("name") or event.name: event for event in steps.values()}
    # 入れ子は span の親子（Rerank は文書検索の子、段は実行の全体の子）。
    assert by_name["Rerank"].parent_id == by_name["文書検索"].step_id
    assert by_name["rag.history_rewrite"].parent_id == root.step_id
    # 段の値に深さ（depth）を入れない（入れ子は親子で決まる）。
    assert "depth" not in (by_name["Rerank"].params or {})
    # 製品の記録に置く trace id は root の span の trace id。
    assert tracker.trace_id == root.trace_id


async def test_span_start_is_running_until_it_finishes() -> None:
    """開始した span は実行中の段になり、終わると所要時間つきの完了になる（実行中も届く）。"""
    tracker = RecordingTrace(MESSAGE_ID)

    def seen() -> list[tuple[str, str]]:
        return [
            (str((event.params or {}).get("name") or event.name), event.status)
            for event in span_steps(tracker.events)
            if isinstance(event, ChatProgressStepEvent) and not event.root
        ]

    with answer_trace(tracker), answer_step("文書検索"):
        assert seen() == [("文書検索", "running")]
    assert seen()[-1] == ("文書検索", "done")
    event = span_steps(tracker.events)[-1]
    assert event.started_at is not None and event.finished_at is not None


async def test_answer_step_display_has_llm_calls_note_and_details() -> None:
    """回答フローの工程の span に、LLM の回数・結果の説明・工程の詳細を付ける。"""
    tracker = RecordingTrace(MESSAGE_ID)
    with answer_trace(tracker), answer_step("根拠確認") as step:
        step.result("該当なし")
        step.status = "該当なし"
        step.field("検索文", "合成の検索文")
    event = next(e for e in span_steps(tracker.events) if not e.root)
    assert event.kind == ANSWER_STEP_KIND
    assert event.params == {"name": "根拠確認", "note": "該当なし"}
    assert event.details is not None
    assert [(field.label, field.value) for field in event.details.fields] == [
        ("結果", "該当なし"),
        ("検索文", "合成の検索文"),
    ]


async def test_pipeline_step_details_and_failure_summary_are_attached() -> None:
    """パイプラインの段の成功の詳細と、失敗の要約・エラーの種類を span の詳細にする。"""
    tracker = RecordingTrace(MESSAGE_ID)
    with answer_trace(tracker):
        await pipeline_stage(
            "history_rewrite",
            result="書き換えた質問",
            result_details=lambda value: {"fields": [{"label": "書き換えた質問", "value": value}]},
        )
        with pytest.raises(RuntimeError):
            await pipeline_stage("answer_record_save", error=RuntimeError("保存の失敗"))
    events = [event for event in span_steps(tracker.events) if not event.root]
    rewrite, save = events
    assert rewrite.details is not None
    assert [(field.label, field.value) for field in rewrite.details.fields] == [
        ("書き換えた質問", "書き換えた質問")
    ]
    assert save.status == "failed"
    assert save.details is not None
    assert save.details.lines[0].startswith("回答の記録をデータベースに保存できませんでした")
    assert "エラーの種類: RuntimeError" in save.details.lines
    # 例外の本文は詳細に入れない。
    assert all("保存の失敗" not in line for line in save.details.lines)


async def test_failed_answer_step_is_failed_with_summary_line() -> None:
    """例外で止まった回答フローの工程は失敗の段になり、詳細の先頭に要約を足す。"""
    tracker = RecordingTrace(MESSAGE_ID)
    with (
        answer_trace(tracker),
        pytest.raises(ValueError),
        answer_step("文書検索"),
    ):
        raise ValueError("合成の失敗")
    step = next(e for e in span_steps(tracker.events) if not e.root)
    assert step.status == "failed"
    assert step.details is not None
    assert step.details.lines[0] == "この工程でエラーが発生しました。"


async def test_thread_spans_notify_changes_on_the_loop() -> None:
    """回答の thread の span の知らせは、ループの中で変更を知らせる（作成中の記録の保存の予約）。"""
    notified_on: list[int] = []
    tracker = AnswerTrace(MESSAGE_ID, owner_user_uuid=None)
    tracker.attach(on_change=lambda: notified_on.append(threading.get_ident()))
    loop_thread = threading.get_ident()

    def work() -> None:
        with answer_step("根拠確認") as step:
            step.field("検索文", "合成の検索文")

    with answer_trace(tracker):
        await asyncio.to_thread(work)
        await asyncio.sleep(0)
    # root の trace id が分かった（ループの thread）・工程の詳細が増えた（回答の thread → ループ）。
    assert notified_on and set(notified_on) == {loop_thread}
    record = tracker.record()
    assert record["trace_id"] == tracker.trace_id
    assert len(record["span_details"]) == 1
    (details,) = record["span_details"].values()
    assert {"label": "検索文", "value": "合成の検索文"} in details["fields"]
    assert record["trace_dropped_spans"] == 0


class _FakeConnection:
    def __init__(self, rows: dict[tuple[str, str], dict[str, object]]) -> None:
        self.rows = rows

    def cursor(self) -> _FakeConnection:
        return self

    def setinputsizes(self, **_sizes: object) -> None:
        pass

    def executemany(self, _sql: str, rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.rows[(str(row["trace_id"]), str(row["span_id"]))] = dict(row)

    def commit(self) -> None:
        pass


async def test_writes_spans_to_shared_table_without_step_details() -> None:
    """span は実行した利用者を持ち主にして表に書き、工程の詳細（検索文など）は表に入れない。"""
    rows: dict[tuple[str, str], dict[str, object]] = {}

    @contextlib.contextmanager
    def connect() -> Iterator[_FakeConnection]:
        yield _FakeConnection(rows)

    writer = TraceSpanWriter(connect)
    previous = trace_store.set_trace_span_writer(writer)
    try:
        tracker = AnswerTrace(MESSAGE_ID, owner_user_uuid="user-a")
        with answer_trace(tracker), answer_step("根拠確認") as step:
            step.field("検索文", "合成の検索文")
        writer.flush()
    finally:
        trace_store.set_trace_span_writer(previous)
    assert {row["owner_user_uuid"] for row in rows.values()} == {"user-a"}
    assert {row["operation_id"] for row in rows.values()} == {MESSAGE_ID}
    assert {row["product"] for row in rows.values()} == {"rag"}
    assert sum(1 for row in rows.values() if row["is_root"]) == 1
    assert all("合成の検索文" not in str(row) for row in rows.values())
    # 詳細は製品の記録に置く。
    assert "合成の検索文" in str(tracker.record()["span_details"])


async def test_no_owner_does_not_write_to_table() -> None:
    """利用者の分からない実行（品質評価など）は表に書かない（持ち主の列は必須）。"""
    rows: dict[tuple[str, str], dict[str, object]] = {}

    @contextlib.contextmanager
    def connect() -> Iterator[_FakeConnection]:
        yield _FakeConnection(rows)

    writer = TraceSpanWriter(connect)
    previous = trace_store.set_trace_span_writer(writer)
    try:
        with answer_trace(AnswerTrace(MESSAGE_ID, owner_user_uuid=None)), answer_step("文書検索"):
            pass
        writer.flush()
    finally:
        trace_store.set_trace_span_writer(previous)
    assert rows == {}


async def test_logs_end_of_product_steps_with_duration_and_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """工程の span の終了を INFO で残す（request_id・trace_id・回答の id・所要時間）。"""
    from pr_backend_core.observability import request_id_var

    token = request_id_var.set("req-3226")
    try:
        with caplog.at_level(logging.INFO, logger="app.rag.trace_store"):
            tracker = RecordingTrace(MESSAGE_ID, model_id="m1")
            tracker.run_id = "trace-1"
            with answer_trace(tracker), answer_step("文書検索"):
                pass
    finally:
        request_id_var.reset(token)
    records = [record for record in caplog.records if record.name == "app.rag.trace_store"]
    # root（実行の全体）は出さない。
    assert len(records) == 1
    finished = records[0]
    assert finished.__dict__["step_kind"] == ANSWER_STEP_KIND
    assert finished.__dict__["step_name"] == "文書検索"
    assert finished.__dict__["status"] == "done"
    assert finished.__dict__["request_id"] == "req-3226"
    assert finished.__dict__["trace_id"] == "trace-1"
    assert finished.__dict__["message_id"] == MESSAGE_ID
    assert isinstance(finished.__dict__["duration_ms"], float)
