"""回答を上限で打ち切ったとき、回答の thread が LLM を呼び続けないことを検証する（#1422）。

遅い LLM はスタブ（`rag_engine.adapters.model_api` の client を差し替え、渡された timeout だけ待って
timeout の例外を出す。実際の HTTP の timeout と同じ振る舞い）。時間は短い上限で確かめる。

回答の thread が終わるまでの待ちは、壁時計の短い上限に頼らない（#1427）。thread の中の
処理（LLM の失敗の再試行の判定が初回に import する SDK など）は、CI の並列（`-n auto`）の
負荷の下では数秒かかることがある。import は先に済ませ（`_warm_retry_imports`）、待ちの上限は
「遅い LLM 1 回」より短い範囲で十分に長くとる（期限で縮まらなければ失敗する）。

打ち切りの試験は、壁時計の所要時間でも判定しない（#3297）。LLM を止めておき（`release` まで
返さない）、LLM を呼んだことを確かめてから打ち切り、打ち切りの後に LLM の呼び出しが増えないことと
thread が終わることを、呼び出しの回数と event で確かめる。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from rag_engine.adapters import model_api
from rag_engine.config import get_settings as engine_get_settings
from rag_engine.models.llm import TextOutput
from rag_engine.resources.deadline import (
    Deadline,
    DeadlineExceeded,
    bind_deadline,
    current_deadline,
)

from app.config import Settings
from app.rag import answer_threads
from app.rag.answer_engine import _run_on_loop
from app.rag.answer_threads import run_answer_in_thread
from app.rag.answer_timeout import AnswerTimeoutError, run_answer_with_timeout

_PROVIDER: Any = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="")
# 遅い LLM の 1 回の応答時間（打ち切りの上限より十分長い）。
_SLOW_LLM_SECONDS = 30.0
# 打ち切った後、回答の thread が終わるまで待つ上限（秒）。期限で縮めた LLM 1 回（1 秒未満）と
# 後片付けが負荷の下で遅れても足りる長さにし、遅い LLM 1 回（`_SLOW_LLM_SECONDS`）より短くする。
# 期限で 1 回の timeout が縮まなければ thread は `_SLOW_LLM_SECONDS` 待つので、この上限で失敗する
# （#1427）。
_THREAD_EXIT_WAIT_SECONDS = 10.0
assert _THREAD_EXIT_WAIT_SECONDS < _SLOW_LLM_SECONDS


class _SlowLlm:
    """遅い LLM。呼び出しごとに timeout と再試行の設定を記録し、timeout だけ待って失敗する。

    `release` を渡すと、試験が `release` を立てるまで返らない（1 回の timeout は hang の guard）。
    """

    def __init__(self, release: threading.Event | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        # 最初の呼び出しで立つ（回答の thread が LLM を待ち始めた）。
        self.called = threading.Event()
        self.release = release
        self._lock = threading.Lock()

    def client(self, options: dict[str, Any] | None = None) -> Any:
        owner = self

        class _Responses:
            def parse(self, **kwargs: Any) -> Any:
                with owner._lock:
                    owner.calls.append(dict(options or {}))
                owner.called.set()
                timeout = float((options or {}).get("timeout", _SLOW_LLM_SECONDS))
                if owner.release is None:
                    time.sleep(min(timeout, _SLOW_LLM_SECONDS))
                else:
                    owner.release.wait(min(timeout, _SLOW_LLM_SECONDS))
                raise TimeoutError("LLM の応答が timeout しました。")

        class _Client:
            responses = _Responses()

            def with_options(self, **new_options: Any) -> Any:
                return owner.client(new_options)

        return _Client()


def _engine_settings() -> Any:
    return replace(
        engine_get_settings(environ={}, dotenv_path=None),
        llm_request_timeout_seconds=600,
        llm_retries=3,
        llm_retry_initial_wait_seconds=0.0,
        llm_retry_max_wait_seconds=0.0,
    )


def _answer_flow(llm: _SlowLlm, finished: threading.Event, steps: int = 4) -> str:
    """回答フローの代わり。LLM を何度か呼び、失敗は握りつぶして次の工程へ進む。

    rag_engine の補助の工程（質問の拡張など）も、失敗を握りつぶして回答を続ける。
    """
    settings = _engine_settings()
    try:
        with mock.patch.object(model_api, "_openai_client_for_provider", return_value=llm.client()):
            for _ in range(steps):
                with contextlib.suppress(Exception):
                    model_api._parse_with_retry(
                        lambda: model_api._openai_parsed([], settings, TextOutput, _PROVIDER),
                        "Responses text",
                        settings,
                        retries=model_api._llm_transport_retries(settings),
                    )
        return "answer"
    finally:
        finished.set()


@pytest.fixture(autouse=True)
def _warm_retry_imports() -> None:
    """LLM の失敗の再試行の判定が初回に import する SDK を先に読む。

    `model_api._is_retryable_error` は例外の型を見るため、openai・httpx・requests を関数の中で
    import する。回答の thread の中で初めて import すると、その process の最初の 1 回だけ thread の
    終わりが遅れる（CI の並列の負荷の下では 3 秒を超えた。#1427）。テストが確かめるのは打ち切りの
    振る舞いなので、import の時間を thread の時間に含めない。
    """
    assert model_api._is_retryable_error(TimeoutError())


async def _wait_thread_exit(finished: threading.Event) -> bool:
    """回答の thread が終わる（`_answer_flow` の `finished`）まで、上限付きで待つ。"""
    return await asyncio.to_thread(finished.wait, _THREAD_EXIT_WAIT_SECONDS)


async def _wait_llm_called(llm: _SlowLlm) -> None:
    """回答の thread が LLM を呼ぶまで、event loop を止めずに待つ（上限は hang の guard）。"""
    give_up = time.monotonic() + _THREAD_EXIT_WAIT_SECONDS
    while not llm.called.is_set():
        assert time.monotonic() < give_up, "回答の thread が LLM を呼ばない"
        await asyncio.sleep(0.01)


def _record_answer_budgets(
    monkeypatch: pytest.MonkeyPatch, timeout_seconds: float
) -> list[asyncio.Timeout]:
    """`run_answer_with_timeout` の打ち切り（`asyncio.timeout`）を記録する。

    試験は記録した打ち切りの期限を今にして（`reschedule`）、壁時計の上限を待たずに打ち切る。
    同じ秒数の `asyncio.timeout` だけを記録する。
    """
    budgets: list[asyncio.Timeout] = []
    original = asyncio.timeout

    def timeout(delay: float | None) -> asyncio.Timeout:
        budget = original(delay)
        if delay == timeout_seconds:
            budgets.append(budget)
        return budget

    monkeypatch.setattr(asyncio, "timeout", timeout)
    return budgets


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answer_threads, "ANSWER_DEADLINE_GRACE_SECONDS", 0.05)


@pytest.fixture
async def small_default_executor() -> Any:
    """既定の executor を 1 本にする（DB・モデルの API の呼び出しの thread が足りない状態）。"""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-default")
    loop.set_default_executor(executor)
    yield executor
    executor.shutdown(wait=False)


async def test_timeout_stops_llm_calls_in_answer_thread(
    short_grace: None, small_default_executor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打ち切ると、待つ側は回答の thread を待たずに返り、thread は次の LLM を呼ばずに終わる。

    所要時間では判定しない（#3297）。打ち切りの上限は長くとり、LLM を呼んだことと既定の
    executor の処理が返ったことを確かめてから、打ち切りの期限を今にする。
    """
    timeout_seconds = 20.0
    budgets = _record_answer_budgets(monkeypatch, timeout_seconds)
    release = threading.Event()
    llm = _SlowLlm(release)
    finished = threading.Event()
    db_results: list[str] = []

    async def operation(_tracker: Any) -> str:
        task = asyncio.create_task(run_answer_in_thread(_answer_flow, llm, finished))
        await _wait_llm_called(llm)
        # 回答の thread が LLM を待っている間も、既定の executor の処理（DB の呼び出しの
        # 代わり。executor は 1 本）は返る。回答の thread が既定の executor を使っていれば、
        # LLM を止めている間は返らず、打ち切りの前にここを通らない。
        db_results.append(await asyncio.to_thread(lambda: "db"))
        [budget] = budgets
        budget.reschedule(asyncio.get_running_loop().time())
        return await task

    with pytest.raises(AnswerTimeoutError) as raised:
        await run_answer_with_timeout(operation, Settings(), timeout_seconds=timeout_seconds)
    assert raised.value.timeout_seconds == timeout_seconds
    assert db_results == ["db"]
    # 待つ側は LLM の応答（回答の thread）を待たずに返った。
    assert not finished.is_set()
    assert len(llm.calls) == 1

    # LLM が（timeout の失敗で）返った後、回答の thread は終わる。
    release.set()
    assert await _wait_thread_exit(finished)
    # 呼んだ LLM は 1 回だけで、残りの工程・再試行で LLM を呼ばない。1 回の timeout は
    # 期限の残り（LLM 1 回の timeout の設定の 600 秒ではない）。
    assert len(llm.calls) == 1
    [options] = llm.calls
    assert options["max_retries"] == 0
    assert options["timeout"] <= timeout_seconds + answer_threads.ANSWER_DEADLINE_GRACE_SECONDS


async def test_wait_for_cancel_stops_answer_thread(short_grace: None) -> None:
    llm = _SlowLlm()
    finished = threading.Event()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_answer_in_thread(_answer_flow, llm, finished, timeout_seconds=0.2), timeout=0.2
        )
    assert await _wait_thread_exit(finished)
    assert len(llm.calls) == 1


async def test_cancelled_task_stops_answer_thread() -> None:
    """チャットの停止（task の取り消し）でも、回答の thread は次の LLM を呼ばない。

    時刻の期限の無い文脈（取り消しだけを伝える期限）でも止まる。
    """
    release = threading.Event()
    llm = _SlowLlm(release)
    finished = threading.Event()

    task = asyncio.create_task(run_answer_in_thread(_answer_flow, llm, finished))
    await _wait_llm_called(llm)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not finished.is_set()
    # 期限が無いと LLM 1 回の timeout（600 秒）になるため、LLM の応答は試験が返す。
    release.set()
    assert await _wait_thread_exit(finished)
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_retries"] == 0


async def test_answer_thread_inherits_context() -> None:
    def read() -> float | None:
        deadline = current_deadline()
        assert deadline is not None
        return deadline.remaining()

    remaining = await run_answer_in_thread(read, timeout_seconds=30.0)
    assert remaining is not None
    assert 0 < remaining <= 30.0 + answer_threads.ANSWER_DEADLINE_GRACE_SECONDS


async def test_run_on_loop_cancels_io_after_deadline() -> None:
    loop = asyncio.get_running_loop()
    io_cancelled = asyncio.Event()

    async def slow_io() -> str:
        try:
            await asyncio.sleep(_SLOW_LLM_SECONDS)
        except asyncio.CancelledError:
            io_cancelled.set()
            raise
        return "io"

    def in_thread() -> None:
        with bind_deadline(Deadline(0.2)):
            _run_on_loop(slow_io, loop)

    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await asyncio.to_thread(in_thread)
    assert time.monotonic() - started < 2.0
    await asyncio.wait_for(io_cancelled.wait(), timeout=2.0)


async def test_run_on_loop_does_not_start_io_after_deadline() -> None:
    loop = asyncio.get_running_loop()
    started: list[bool] = []

    async def io() -> str:
        started.append(True)
        return "io"

    def in_thread() -> None:
        deadline = Deadline(30.0)
        deadline.cancel()
        with bind_deadline(deadline):
            _run_on_loop(io, loop)

    with pytest.raises(DeadlineExceeded):
        await asyncio.to_thread(in_thread)
    assert started == []


async def test_run_on_loop_returns_io_result_and_raises_io_timeout() -> None:
    loop = asyncio.get_running_loop()

    async def ok() -> str:
        return "io"

    async def failing() -> str:
        raise TimeoutError("DB の呼び出しが timeout しました。")

    assert await asyncio.to_thread(_run_on_loop, ok, loop) == "io"
    # I/O 自体の TimeoutError は、待ちの上限と取り違えずにそのまま送出する。
    with pytest.raises(TimeoutError, match="DB の呼び出し"):
        await asyncio.wait_for(asyncio.to_thread(_run_on_loop, failing, loop), timeout=2.0)
