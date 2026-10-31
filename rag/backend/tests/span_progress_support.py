"""処理の経過（trace の span のイベント。#3226）のテストの補助。

偽の pipeline が本物と同じ span（実行の全体・パイプラインの段・回答フローの工程）を作れるように、
`operation_trace` と工程の観測（`_observe_engine_call`）を束ね、工程の span を開く関数を持つ。
`RecordingTrace` は span の知らせを処理の経過のイベント（画面の span の段と同じ形）にも集める
（共通の span の表を読まずに、span の名前・種類・詳細・親子を確かめる）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from threading import Lock
from typing import Any

from pr_backend_core.chat_progress import (
    ChatProgressRecorder,
    ChatProgressStepEvent,
    fold_chat_progress_events,
)
from pr_backend_core.observability.tracing import SpanListener, SpanUpdate, operation_trace
from rag_engine.generation.execution_record import _execution_step
from rag_engine.resources.tracing import bind_trace_observer

from app.rag.pipeline import _observe_engine_call, _observe_stage
from app.rag.trace_store import AnswerTrace

__all__ = [
    "RecordingTrace",
    "answer_step",
    "answer_trace",
    "pipeline_stage",
    "span_rows",
    "span_steps",
]


class RecordingTrace(AnswerTrace):
    """`AnswerTrace` に、span の知らせを処理の経過のイベントとして集める機能を足す（テスト）。"""

    def __init__(
        self, operation_id: str = "a" * 32, *, owner_user_uuid: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(operation_id, owner_user_uuid=owner_user_uuid, **kwargs)
        self._recorder = ChatProgressRecorder(operation_id)
        self._events_lock = Lock()
        self.updates: list[SpanUpdate] = []

    def listener(self) -> SpanListener:
        base = super().listener()

        def listen(update: SpanUpdate) -> None:
            base(update)
            with self._events_lock:
                self.updates.append(update)
                self._recorder.record_span(update)

        return listen

    @property
    def events(self) -> list[Any]:
        with self._events_lock:
            return list(self._recorder.events)


@contextmanager
def answer_trace(
    trace: AnswerTrace | None,
    *,
    trace_id: str = "trace",
    name: str = "rag.answer",
) -> Iterator[None]:
    """本物の pipeline と同じ実行の全体の span と工程の観測を開く。"""
    with (
        operation_trace(
            name, operation_id=trace_id, listener=trace.listener() if trace is not None else None
        ),
        bind_trace_observer(_observe_engine_call),
    ):
        yield


@contextmanager
def answer_step(name: str) -> Iterator[Any]:
    """回答フローの工程（`rag_engine` の `_execution_step`）を 1 つ開く。"""
    with _execution_step(name) as step:
        yield step


async def pipeline_stage(
    stage: str,
    work: Awaitable[object] | None = None,
    *,
    trace_id: str = "trace",
    progress_callback: Any = None,
    result: object = None,
    error: BaseException | None = None,
    result_details: Any = None,
) -> object:
    """パイプラインの段（`_observe_stage`）を 1 つ実行する。

    `work` を渡すと段の中で待つ（中の工程は段の子になる）。`error` を渡すと失敗にする。
    """

    async def operation() -> object:
        if work is not None:
            await work
        if error is not None:
            raise error
        return result

    return await _observe_stage(
        trace_id,
        stage,
        operation(),
        progress_callback=progress_callback,
        result_details=result_details,
    )


def span_steps(events: Sequence[object]) -> list[ChatProgressStepEvent]:
    """記録の最後の状態の段（span）のイベント（記録の順。root を含む）。"""
    latest: dict[str, ChatProgressStepEvent] = {}
    for event in events:
        if isinstance(event, ChatProgressStepEvent):
            latest[event.step_id] = event
    return list(latest.values())


def span_rows(events: Sequence[object]) -> list[tuple[str, str, object, str]]:
    """root を除く段の一覧（span の名前・種類・工程名・状態）。種類の無い span の種類は空。"""
    rows: list[tuple[str, str, object, str]] = []
    fold = fold_chat_progress_events(
        [event for event in events if isinstance(event, ChatProgressStepEvent)]
    )
    steps = {step.step_id: step for step in fold.step_list()}
    for event in span_steps(events):
        if event.root:
            continue
        step = steps[event.step_id]
        kind = "" if step.kind == step.step_id else str(step.kind)
        params: Mapping[str, object] = step.params or {}
        rows.append((str(event.name), kind, params.get("name"), step.status))
    return rows
