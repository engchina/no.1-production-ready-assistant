"""六指標の評価情報・独立性・応答判断を合成データで検証する。"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rag.evaluation import EvaluationRunner, score_case_answers, summarize_case_results
from app.schemas.evaluation import (
    EVALUATION_METRIC_NAMES,
    EvaluationAnswerJudgement,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationEvidence,
    EvaluationMetricName,
    EvaluationThresholds,
)
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchResponse


def response(outcome: str = "answered") -> SearchResponse:
    return SearchResponse(
        answer="合成の回答",
        trace_id="synthetic-trace",
        elapsed_ms=1,
        citations=[
            RetrievedChunk(document_id=doc, chunk_id=f"{doc}:c1", text="合成の必要な事実", score=1)
            for doc in ("other", "correct", "correct")
        ],
        diagnostics=SearchDiagnostics(answer={"outcome": outcome}),
    )


def test_required_fragments_and_document_ranks_are_independent() -> None:
    case = EvaluationCase(
        id="synthetic",
        query="合成の質問",
        relevant_document_ids=["correct"],
        required_evidence=[
            EvaluationEvidence(id="e1", document_id="correct", text="必要な事実"),
            EvaluationEvidence(id="e2", document_id="correct", text="見つからない事実"),
        ],
    )
    scored = score_case_answers(case, [(response(), {})])
    assert scored.required_evidence_recall == scored.mrr == 0.5
    assert scored.missing_evidence == ["e2"]
    metrics = summarize_case_results([scored])
    assert set(metrics.metric_case_counts) == set(EVALUATION_METRIC_NAMES)
    assert len(EVALUATION_METRIC_NAMES) == 6


@pytest.mark.parametrize("expected", ["answer", "clarify", "hold"])
@pytest.mark.parametrize(
    "actual,outcome",
    [("answer", "answered"), ("clarify", "needs_clarification"), ("hold", "insufficient_evidence")],
)
def test_three_way_decision(expected: Any, actual: str, outcome: str) -> None:
    case = EvaluationCase(id="synthetic", query="合成の質問", expected_response_decision=expected)
    scored = score_case_answers(case, [(response(outcome), {})])
    assert scored.response_decision_accuracy == float(expected == actual)
    metrics = summarize_case_results([scored])
    assert (
        metrics.decision_breakdown[expected].metric_case_counts["response_decision_accuracy"] == 1
    )


def test_missing_materials_are_not_zero_or_passed() -> None:
    scored = score_case_answers(
        EvaluationCase(id="synthetic", query="合成の質問"), [(response(), {})]
    )
    metrics = summarize_case_results(
        [scored], thresholds=EvaluationThresholds(answer_correctness=1)
    )
    assert all(getattr(metrics, name) is None for name in EVALUATION_METRIC_NAMES)
    assert metrics.passed is None
    assert all(count == 0 for count in metrics.metric_case_counts.values())


def test_correctness_does_not_follow_faithfulness_or_coverage() -> None:
    judgement = EvaluationAnswerJudgement(
        status="completed", answer_correctness=0, requirement_coverage=1, claim_support_rate=1
    )
    scored = score_case_answers(
        EvaluationCase(id="synthetic", query="合成の質問"), [(response(), {})], judgement
    )
    metrics = summarize_case_results(
        [scored], thresholds=EvaluationThresholds(answer_correctness=0.9)
    )
    assert metrics.answer_correctness == 0
    assert metrics.requirement_coverage == metrics.claim_support_rate == 1
    assert metrics.passed is False
    assert [item.metric for item in metrics.threshold_failures] == ["answer_correctness"]


async def test_faithfulness_without_standard_or_saved_record() -> None:
    captured = []

    class Pipeline:
        async def run(
            self, request: Any, trace_id: Any = None, progress_callback: Any = None
        ) -> SearchResponse:
            return response()

    async def judge(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {
            "status": "completed",
            "claim_support_rate": 1,
            "requirement_coverage": None,
            "answer_correctness": None,
        }

    result = await EvaluationRunner(
        pipeline=Pipeline(), answer_judge=judge, settings=Settings()
    ).run([EvaluationCase(id="synthetic", query="合成の質問")], top_k=3)
    assert captured[0]["standard_answer"] == ""
    assert captured[0]["evaluation_input"]["evidence_items"]
    assert result.claim_support_rate == 1
    assert result.answer_correctness is None
    assert result.metric_case_counts["claim_support_rate"] == 1
    assert result.metric_case_counts["answer_correctness"] == 0


@pytest.mark.parametrize(
    "name",
    [
        "context_recall",
        "faithfulness",
        "answer_pass_rate",
        "handling_accuracy",
        "step_order_score",
        "safe_answer_rate",
    ],
)
def test_removed_thresholds_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        EvaluationThresholds.model_validate({name: 1})


def test_failed_case_stays_in_expected_decision_breakdown() -> None:
    from app.rag.evaluation import _case_error_result

    case = EvaluationCase(id="failed", query="合成の質問", expected_response_decision="hold")
    failed = _case_error_result(
        case=case,
        trace_id="synthetic",
        elapsed=1,
        error=TimeoutError("合成の失敗"),
    )
    result = summarize_case_results([failed])
    breakdown = result.decision_breakdown["hold"]
    assert (breakdown.case_count, breakdown.error_count) == (1, 1)
    assert breakdown.metric_case_counts["response_decision_accuracy"] == 0
    assert result.response_decision_accuracy is None
    assert result.passed is False


def test_unassessed_completed_judgement_keeps_null_verdict() -> None:
    from app.rag.evaluation import summarize_answer_judgement

    summary = summarize_answer_judgement({"status": "completed", "passed": None})
    assert summary.passed is None
    assert summary.answer_correctness is None
    assert summary.claim_support_rate is None
    assert summary.requirement_coverage is None


@pytest.mark.parametrize("suite", ["standard", "strict"])
@pytest.mark.parametrize("metric", EVALUATION_METRIC_NAMES)
@pytest.mark.parametrize("offset", [-0.00001, 0.0, 0.00001])
def test_thresholds_use_unrounded_scores(
    suite: str, metric: EvaluationMetricName, offset: float
) -> None:
    from app.rag.evaluation_adapter import resolve_evaluation_suite

    thresholds = resolve_evaluation_suite(suite)
    target = getattr(thresholds, metric)
    value = min(1.0, target + offset)
    values = {name: 1.0 for name in EVALUATION_METRIC_NAMES}
    values[metric] = value
    judgement = EvaluationAnswerJudgement(
        status="completed",
        **{
            name: values[name]
            for name in ("answer_correctness", "requirement_coverage", "claim_support_rate")
        },
    )
    case = EvaluationCaseResult(
        case_id="synthetic-boundary",
        trace_id="synthetic",
        elapsed_ms=1,
        answer_evaluation=judgement,
        **values,
    )
    result = summarize_case_results([case], thresholds=thresholds)
    assert result.passed is (offset >= 0)
    assert [failure.metric for failure in result.threshold_failures] == (
        [metric] if offset < 0 else []
    )
    assert getattr(result, metric) == value


def test_thresholds_use_unrounded_mean_of_multiple_cases() -> None:
    cases = [
        EvaluationCaseResult(
            case_id=str(index), trace_id="synthetic", elapsed_ms=1, answer_correctness=value
        )
        for index, value in enumerate([0.9, 0.89998])
    ]
    result = summarize_case_results(cases, thresholds=EvaluationThresholds(answer_correctness=0.9))
    assert result.passed is False
    assert result.answer_correctness == pytest.approx(0.89999)
    assert result.threshold_failures[0].actual < 0.9


def test_retrieval_scores_preserve_precision_before_aggregation() -> None:
    answer = response()
    answer.citations.insert(
        1,
        RetrievedChunk(
            document_id="other-two",
            chunk_id="other-two:c1",
            text="合成の別の本文",
            score=0.5,
        ),
    )
    case = EvaluationCase(
        id="synthetic-precision",
        query="合成の質問",
        relevant_document_ids=["correct"],
        required_evidence=[
            EvaluationEvidence(id=str(index), document_id="correct", text=text)
            for index, text in enumerate(["必要な事実", "合成の不足 A", "合成の不足 B"])
        ],
    )
    result = score_case_answers(case, [(answer, {})])
    assert result.mrr == 1 / 3
    assert result.required_evidence_recall == 1 / 3


def test_result_preserves_applied_thresholds() -> None:
    result = summarize_case_results(
        [], thresholds=EvaluationThresholds(mrr=0.6, answer_correctness=0.9)
    )
    assert result.applied_thresholds == {"mrr": 0.6, "answer_correctness": 0.9}
