"""品質評価のケースの上限つきの並行と、429 の全体での扱い（#2004）。

回答の pipeline と判定はスタブにし、再試行の待ちは実時間で待たない（``ai_calls._sleep``）。
"""

import asyncio
from collections.abc import Mapping, Sequence
from time import perf_counter

import pytest
from pr_backend_core.bounded_calls import BoundedCallAborted
from pytest import MonkeyPatch

import app.rag.evaluation as evaluation
from app.clients import ai_calls
from app.rag.evaluation import EvaluationRunner
from app.rag.evaluation_jobs import evaluation_job_failed_message
from app.rag.pipeline import SearchStageProgressCallback
from app.schemas.evaluation import EvaluationCase
from app.schemas.search import RetrievedChunk, SearchRequest, SearchResponse
from tests.test_evaluation import EVALUATION_INPUT, _approval_response, _completed_judgement


class _HttpError(Exception):
    """openai・httpx と同じく ``status_code`` を持つ HTTP の失敗のスタブ。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status


class _ThrottledPipeline:
    """最初の ``errors`` を順に出し、少し待ってから答える。同時に動いた数を数える。"""

    def __init__(self, errors: list[Exception] | None = None, *, delay: float = 0.02) -> None:
        self.errors = list(errors or [])
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.queries: list[str] = []
        self.trace_ids: list[str] = []

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        assert trace_id
        self.queries.append(request.query)
        self.trace_ids.append(trace_id)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.errors:
                raise self.errors.pop(0)
            return _approval_response(request, trace_id, evaluation_input=EVALUATION_INPUT)
        finally:
            self.in_flight -= 1


class _ThrottledJudge:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(
        self,
        *,
        trace_id: str,
        evaluation_input: Mapping[str, object],
        standard_answer: str,
        citations: Sequence[RetrievedChunk],
        timeout_seconds: float,
        judge_model_id: str,
    ) -> Mapping[str, object]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _completed_judgement()


@pytest.fixture
def concurrency(monkeypatch: MonkeyPatch) -> list[int]:
    """ケースの並行の上限（既定のテストは 1 件ずつ。ここで差し替える）と、待ちを実時間にしない。"""
    value = [2]
    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda _settings: value[0])
    monkeypatch.setattr(ai_calls, "_sleep", lambda _seconds: asyncio.sleep(0))
    return value


def _cases(count: int, *, standard_answer: str | None = None) -> list[EvaluationCase]:
    return [
        EvaluationCase(
            id=f"case-{index}",
            query=f"承認条件 {index}",
            relevant_document_ids=["doc-1"],
            required_answer_points=["120000"],
            standard_answer=standard_answer,
        )
        for index in range(1, count + 1)
    ]


async def test_cases_run_with_bounded_concurrency_and_429_is_retried(
    concurrency: list[int],
) -> None:
    """並行の上限を超えず、429 は全体で待って送り直し、結果を失わずケースの順に残す。"""
    pipeline = _ThrottledPipeline([_HttpError(429), _HttpError(429)])
    runner = EvaluationRunner(pipeline=pipeline)

    metrics = await runner.run(cases=_cases(4), top_k=5)

    assert [result.case_id for result in metrics.case_results] == [
        "case-1",
        "case-2",
        "case-3",
        "case-4",
    ]
    assert [result.status for result in metrics.case_results] == ["success"] * 4
    assert metrics.error_count == 0
    assert pipeline.peak == 2
    # 4 ケースと、429 の 2 回の送り直し（新しい trace id で記録する）。
    assert len(pipeline.queries) == 6
    assert len(set(pipeline.trace_ids)) == 6
    assert {result.trace_id for result in metrics.case_results} <= set(pipeline.trace_ids[2:])


async def test_judge_429_is_retried_with_the_same_caller(concurrency: list[int]) -> None:
    judge = _ThrottledJudge([_HttpError(429)])
    runner = EvaluationRunner(pipeline=_ThrottledPipeline(), answer_judge=judge)

    cases = _cases(2, standard_answer="承認条件は 120000 円です。")
    metrics = await runner.run(cases=cases, top_k=5)

    assert judge.calls == 3
    judgements = [result.answer_evaluation for result in metrics.case_results]
    assert all(item is not None and item.status == "completed" for item in judgements)


async def test_timeout_is_not_retried(concurrency: list[int], monkeypatch: MonkeyPatch) -> None:
    """回答の上限の時間切れは送り直さず、ケースの失敗のまま（今までと同じ）。"""
    pipeline = _ThrottledPipeline([TimeoutError("slow")])
    runner = EvaluationRunner(pipeline=pipeline)

    metrics = await runner.run(cases=_cases(1), top_k=5)

    assert metrics.error_count == 1
    assert len(pipeline.queries) == 1


async def test_permission_error_stops_remaining_cases(concurrency: list[int]) -> None:
    """権限の誤り（403）は送り直さず、残りのケースを始めずに評価を止める。"""
    concurrency[0] = 1
    pipeline = _ThrottledPipeline([_HttpError(403)])
    runner = EvaluationRunner(pipeline=pipeline)

    with pytest.raises(BoundedCallAborted) as exc_info:
        await runner.run(cases=_cases(3), top_k=5)

    assert len(pipeline.queries) == 1
    message = evaluation_job_failed_message(exc_info.value)
    assert "権限" in message
    assert "詳細" not in message


async def test_concurrency_shortens_total_time(concurrency: list[int]) -> None:
    """スタブの遅延で、並行の数に応じて全体の時間が短くなる（順番は変わらない）。"""
    elapsed: dict[int, float] = {}
    for value in (1, 4):
        concurrency[0] = value
        pipeline = _ThrottledPipeline(delay=0.05)
        started = perf_counter()
        metrics = await EvaluationRunner(pipeline=pipeline).run(cases=_cases(8), top_k=5)
        elapsed[value] = perf_counter() - started
        assert pipeline.peak == value
        assert [result.case_id for result in metrics.case_results] == [
            f"case-{index}" for index in range(1, 9)
        ]
    assert elapsed[4] < elapsed[1] * 0.6
