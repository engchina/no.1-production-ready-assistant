"""呼び出し元が検索・回答を取り消したら、次の工程と LLM の呼び出しを始めない（#2691）。

MCP の `rag_search` は、client（Agent の Run の取消・外部の client の中断）が接続を切ると、共通の
MCP の入口（`pr_backend_core.mcp`）が検索の task を取り消す。ここでは、その取り消しが回答の thread
に伝わり、次の工程・LLM の呼び出しを始めないこと、成功ではなく取消として監査に残ることを確かめる。

回答フローは決定論のスタブ（工程ごとに LLM の代わりの関数を 1 回呼び、呼び出しの回数を数える）。
外部への通信はしない。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, cast

import pytest
from pytest import LogCaptureFixture, MonkeyPatch
from rag_engine.generation.execution_record import _execution_step
from rag_engine.resources.deadline import DeadlineExceeded, check_deadline

from app.api.routes import search as search_route
from app.rag.answer_threads import run_answer_in_thread
from app.rag.pipeline import SearchStageProgress
from app.schemas.search import SearchRequest

# スタブの LLM の 1 回が、取り消しの後に戻るまで待つ上限（秒）。
WAIT_SECONDS = 10.0


class _Flow:
    """回答フローの代わり。工程ごとに LLM の代わりを 1 回呼ぶ（呼び出しを数える）。"""

    def __init__(self) -> None:
        self.llm_calls = 0
        self.steps: list[str] = []
        self.first_call = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.stopped_by: type[BaseException] | None = None

    def _llm(self) -> None:
        check_deadline()  # LLM の adapter と同じく、呼ぶ前に期限を確かめる
        self.llm_calls += 1
        self.first_call.set()
        # 実行中の LLM の呼び出し（取り消しの後に戻る）。
        self.release.wait(WAIT_SECONDS)

    def run(self) -> str:
        try:
            for name in ("質問の理解", "文書検索", "回答文の生成と根拠確認"):
                with _execution_step(name):
                    self.steps.append(name)
                    self._llm()
            return "answer"
        except DeadlineExceeded as exc:
            self.stopped_by = type(exc)
            raise
        finally:
            self.finished.set()


def _pipeline(flow: _Flow) -> type:
    class Pipeline:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(
            self,
            request: SearchRequest,
            *,
            trace_id: str,
            progress_callback: Any,
            trace: Any = None,
        ) -> Any:
            await progress_callback(
                SearchStageProgress(
                    trace_id=trace_id,
                    stage="answer",
                    outcome="started",
                    elapsed_ms=0.0,
                    attributes={},
                )
            )
            return await run_answer_in_thread(flow.run)

    return Pipeline


@pytest.fixture
def audits(monkeypatch: MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    async def resolve(request: SearchRequest, settings: Any, **_kwargs: Any) -> Any:
        return request, settings, None, None

    monkeypatch.setattr(search_route, "_resolve_query_context", resolve)
    monkeypatch.setattr(
        search_route, "record_rag_search_audit", lambda **kwargs: recorded.append(kwargs)
    )
    return recorded


async def test_cancelled_search_starts_no_further_step_or_llm_call(
    monkeypatch: MonkeyPatch, audits: list[dict[str, Any]], caplog: LogCaptureFixture
) -> None:
    flow = _Flow()
    monkeypatch.setattr(search_route, "RagPipeline", _pipeline(flow))
    caplog.set_level(logging.INFO, logger=search_route.logger.name)

    task = asyncio.create_task(
        search_route._run_search_with_timeout(SearchRequest(query="合成の質問"))
    )
    assert await asyncio.to_thread(flow.first_call.wait, WAIT_SECONDS)
    # client の切断（MCP の入口が検索の task を取り消す）。
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 実行中だった LLM の呼び出しが戻っても、次の工程・LLM の呼び出しは始めない。
    flow.release.set()
    assert await asyncio.to_thread(flow.finished.wait, WAIT_SECONDS)

    assert flow.llm_calls == 1
    assert flow.steps == ["質問の理解"]
    assert flow.stopped_by is DeadlineExceeded
    # 成功としては残さず、取消を監査に残す（止まった工程と秒。質問の本文は残さない）。
    [audit] = audits
    assert audit["outcome"] == "error"
    assert audit["error_stage"] == search_route.SEARCH_CANCELLED_OUTCOME
    assert isinstance(audit["error"], search_route.SearchCancelledError)
    record = next(r for r in caplog.records if r.getMessage() == "rag_search_cancelled")
    assert record.__dict__["stage"] == "answer"
    assert isinstance(record.__dict__["elapsed_ms"], int)
    assert "合成の質問" not in caplog.text


async def test_search_that_finishes_is_not_recorded_as_cancelled(
    monkeypatch: MonkeyPatch, audits: list[dict[str, Any]]
) -> None:
    flow = _Flow()
    flow.release.set()
    monkeypatch.setattr(search_route, "RagPipeline", _pipeline(flow))

    result = await search_route._run_search_with_timeout(SearchRequest(query="合成の質問"))

    assert cast(Any, result) == "answer"
    assert flow.llm_calls == 3
    assert audits == []
