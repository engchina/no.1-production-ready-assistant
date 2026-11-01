"""RAG 評価ランナーのテスト(#591 の 9 指標)。"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from pydantic import ValidationError
from pytest import LogCaptureFixture, MonkeyPatch

from app.config import ENTERPRISE_AI_TIMEOUT_MAX_SECONDS, Settings, get_settings
from app.main import app
from app.rag.evaluation import (
    EVALUATION_CASE_ERROR_MESSAGE,
    EvaluationRunner,
    evaluation_settings,
    is_abstained,
    summarize_answer_judgement,
)
from app.rag.evaluation_adapter import resolve_evaluation_suite
from app.rag.pipeline import (
    SearchStageProgress,
    SearchStageProgressCallback,
)
from app.schemas.evaluation import (
    EVALUATION_METRIC_NAMES,
    EvaluationCase,
    EvaluationEvidence,
    EvaluationMetrics,
    EvaluationRagOverrides,
    EvaluationThresholds,
)
from app.schemas.search import (
    RetrievedChunk,
    SearchDiagnostics,
    SearchRequest,
    SearchResponse,
)
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

EVALUATION_INPUT = {"question": "承認条件", "answer_text": "承認条件は 120000 円です。"}


def _approval_response(
    request: SearchRequest,
    trace_id: str,
    *,
    evaluation_input: Mapping[str, object] | None = None,
) -> SearchResponse:
    return SearchResponse(
        answer="承認条件は 120000 円です。",
        citations=[
            RetrievedChunk(
                document_id="doc-1",
                chunk_id="doc-1:0",
                text="承認条件: 120000",
                score=1.0,
                metadata={
                    "page_start": 2,
                    "page_end": 2,
                    "element_ids": "el-approval",
                    "evidence_model_used": True,
                },
            )
        ],
        trace_id=trace_id,
        elapsed_ms=1.0,
        diagnostics=SearchDiagnostics(knowledge_base_count=len(request.knowledge_base_ids)),
    ).with_evaluation_input(evaluation_input)


class StubPipeline:
    """評価ランナー用の固定レスポンス pipeline(回答の記録の評価入力つき)。"""

    def __init__(self) -> None:
        self.requests: list[SearchRequest] = []
        self.trace_ids: list[str] = []

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        assert trace_id
        self.requests.append(request)
        self.trace_ids.append(trace_id)
        return _approval_response(request, trace_id, evaluation_input=EVALUATION_INPUT)


class FakeJudge:
    """標準回答による評価(evaluate_answer_record)の決定論スタブ。"""

    def __init__(self, result: Mapping[str, object] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "trace_id": trace_id,
                "evaluation_input": dict(evaluation_input),
                "standard_answer": standard_answer,
                "citations": list(citations),
                "timeout_seconds": timeout_seconds,
                "judge_model_id": judge_model_id,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _completed_judgement(
    *,
    passed: bool = True,
    claim_status: str = "supported",
    requirement_coverage: float = 1.0,
    coverage_statuses: tuple[str, ...] = ("addressed",),
) -> dict[str, object]:
    return {
        "status": "completed",
        "passed": passed,
        "answer_correctness": 1.0 if passed else 0.0,
        "claim_support_rate": 1.0 if claim_status == "supported" else 0.0,
        "claim_checks": [{"status": claim_status}],
        "coverage_checks": [{"status": status} for status in coverage_statuses],
        "requirement_coverage": requirement_coverage,
        "message": "",
    }


async def test_evaluation_runner_computes_metrics_by_perspective() -> None:
    """検索・根拠・回答の指標を、測れるケースだけで求める。"""
    pipeline = StubPipeline()
    runner = EvaluationRunner(pipeline=pipeline, answer_judge=FakeJudge(_completed_judgement()))
    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="case-1",
                query="承認条件",
                relevant_document_ids=["doc-1"],
                required_evidence=[EvaluationEvidence(id="e1", document_id="doc-1", text="120000")],
                expected_response_decision="answer",
                standard_answer="合成の標準回答",
            )
        ],
        top_k=5,
        filters={"status": "indexed"},
    )
    assert metrics.required_evidence_recall == 1.0
    assert metrics.mrr == 1.0
    assert metrics.answer_correctness == 1.0
    assert metrics.claim_support_rate == 1.0
    assert metrics.requirement_coverage == 1.0
    assert metrics.response_decision_accuracy == 1.0
    assert metrics.metric_case_counts == {
        "required_evidence_recall": 1,
        "mrr": 1,
        "claim_support_rate": 1,
        "answer_correctness": 1,
        "requirement_coverage": 1,
        "response_decision_accuracy": 1,
    }
    assert set(metrics.metric_case_counts) == set(EVALUATION_METRIC_NAMES)
    assert metrics.reasoning_type_breakdown == {}
    assert metrics.hops_breakdown == {}
    assert metrics.split_breakdown == {}
    assert metrics.passed is True
    assert metrics.threshold_failures == []
    assert metrics.failure_reason_counts == {}
    result = metrics.case_results[0]
    assert result.case_id == "case-1"
    assert result.trace_id == pipeline.trace_ids[0]
    assert result.status == "success"
    assert result.retrieved_document_ids == ["doc-1"]
    assert result.hit_document_ids == ["doc-1"]
    assert result.required_evidence_recall == 1.0
    assert result.mrr == 1.0
    assert result.answer_correctness == 1.0
    assert result.response_decision_accuracy == 1.0
    assert result.answer_evaluation is not None
    assert result.failure_reasons == []
    assert pipeline.requests[0].top_k == 5
    assert pipeline.requests[0].filters == {"status": "INDEXED"}


async def test_evaluation_runner_passes_knowledge_base_scope_to_search_request() -> None:
    """評価 runner は KB スコープを SearchRequest へ伝播する。"""
    pipeline = StubPipeline()
    runner = EvaluationRunner(pipeline=pipeline)

    await runner.run(
        cases=[EvaluationCase(id="case-1", query="承認条件")],
        top_k=5,
        knowledge_base_ids=["kb-1", "kb-2"],
    )

    request = pipeline.requests[0]
    assert request.knowledge_base_ids == ["kb-1", "kb-2"]
    assert request.filters["knowledge_base_id"] == "kb-1,kb-2"


async def test_evaluation_runner_includes_superseded_documents_only_for_marked_cases() -> None:
    """旧版も検索するケース（版の比較。#1366）だけ include_superseded=true を足す。"""
    pipeline = StubPipeline()
    runner = EvaluationRunner(pipeline=pipeline)

    await runner.run(
        cases=[
            EvaluationCase(id="current", query="今の承認の期限"),
            EvaluationCase(id="versions", query="旧版との違い", include_superseded=True),
        ],
        top_k=5,
        filters={"status": "indexed"},
    )

    assert [request.filters for request in pipeline.requests] == [
        {"status": "INDEXED"},
        {"status": "INDEXED", "include_superseded": "true"},
    ]


async def test_metrics_skip_cases_without_expectations() -> None:
    runner = EvaluationRunner(pipeline=StubPipeline())
    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="with-evidence",
                query="合成の質問",
                relevant_document_ids=["doc-1"],
                required_evidence=[EvaluationEvidence(id="e1", document_id="doc-1", text="120000")],
            ),
            EvaluationCase(id="document-only", query="合成の質問", relevant_document_ids=["doc-2"]),
        ],
        top_k=5,
    )
    assert metrics.metric_case_counts["required_evidence_recall"] == 1
    assert metrics.required_evidence_recall == 1
    assert metrics.metric_case_counts["mrr"] == 2
    assert metrics.mrr == 0.5
    assert metrics.case_results[1].required_evidence_recall is None
    assert metrics.case_results[1].failure_reasons == ["retrieval_miss"]
    assert metrics.response_decision_accuracy is None


class DuplicateChunkPipeline:
    """同じ document の複数 chunk を返す pipeline。"""

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        return SearchResponse(
            answer="A 文書の承認条件が関連します。",
            citations=[
                RetrievedChunk(
                    document_id=document_id,
                    chunk_id=chunk_id,
                    text=text,
                    score=score,
                )
                for document_id, chunk_id, text, score in (
                    ("doc-b", "doc-b:0", "B 文書の検索候補です。", 0.9),
                    ("doc-a", "doc-a:0", "A 文書には承認条件が記載されています。", 0.8),
                    ("doc-a", "doc-a:1", "A 文書の補足説明です。", 0.7),
                )
            ],
            trace_id=trace_id or "trace",
            elapsed_ms=1.0,
        )


async def test_retrieval_metrics_are_document_level_not_chunk_level() -> None:
    runner = EvaluationRunner(pipeline=DuplicateChunkPipeline())

    metrics = await runner.run(
        cases=[EvaluationCase(id="case-duplicate", query="A", relevant_document_ids=["doc-a"])],
        top_k=3,
    )

    assert metrics.mrr is not None
    # 正解の文書は 2 番目の文書(chunk の数では数えない)。
    assert metrics.mrr == 0.5
    assert metrics.case_results[0].retrieved_document_ids == ["doc-b", "doc-a"]
    assert metrics.case_results[0].hit_document_ids == ["doc-a"]


class MissPipeline:
    """関連 document を返さない応答を返す pipeline。"""

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        return SearchResponse(
            answer="関連しない回答です。",
            citations=[
                RetrievedChunk(
                    document_id="doc-x",
                    chunk_id="doc-x:0",
                    text="支払条件は月末締め翌月末払いです。",
                    score=0.8,
                ),
            ],
            trace_id=trace_id or "trace-miss",
            elapsed_ms=12.5,
        )


async def test_evaluation_case_result_exposes_miss_diagnostics() -> None:
    runner = EvaluationRunner(pipeline=MissPipeline())
    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="case-miss",
                query="合成の質問",
                relevant_document_ids=["doc-a"],
                required_evidence=[
                    EvaluationEvidence(id="e1", document_id="doc-a", text="必要な事実")
                ],
            )
        ],
        top_k=5,
    )
    assert metrics.required_evidence_recall == metrics.mrr == 0
    assert metrics.answer_correctness is None
    assert metrics.claim_support_rate is None
    result = metrics.case_results[0]
    assert result.retrieved_document_ids == ["doc-x"]
    assert result.hit_document_ids == []
    assert result.missing_evidence == ["e1"]
    assert result.failure_reasons == ["evidence_miss", "retrieval_miss"]
    assert metrics.failure_reason_counts == {"evidence_miss": 1, "retrieval_miss": 1}
    assert result.elapsed_ms == 12.5


async def test_evaluation_runner_reports_threshold_failures() -> None:
    runner = EvaluationRunner(pipeline=MissPipeline())
    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="case-fail",
                query="合成の質問",
                relevant_document_ids=["doc-a"],
                required_evidence=[
                    EvaluationEvidence(id="e1", document_id="doc-a", text="必要な事実")
                ],
            )
        ],
        top_k=5,
        thresholds=resolve_evaluation_suite("standard"),
    )
    assert metrics.passed is False
    assert [
        (failure.metric, failure.actual, failure.threshold)
        for failure in metrics.threshold_failures
    ] == [("required_evidence_recall", 0, 0.9), ("mrr", 0, 0.6)]
    assert metrics.claim_support_rate is None
    assert metrics.metric_case_counts["claim_support_rate"] == 0


NO_RESULTS_ANSWER = "資料の中に、この質問に答えられる根拠が見つかりませんでした。"


class NoResultsPipeline:
    """根拠が見つからず、引用なしで「答えられない」と返す pipeline。"""

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        return SearchResponse(
            answer=NO_RESULTS_ANSWER,
            citations=[],
            trace_id=trace_id or "trace-no-results",
            elapsed_ms=3.0,
        )


@pytest.mark.parametrize("suite", ["standard", "strict"])
async def test_expected_refusal_case_passes_every_suite(suite: str) -> None:
    runner = EvaluationRunner(pipeline=NoResultsPipeline())
    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="case-negative", query="合成の質問", expected_response_decision="hold"
            )
        ],
        top_k=5,
        thresholds=EvaluationThresholds(
            response_decision_accuracy=resolve_evaluation_suite(suite).response_decision_accuracy
        ),
    )
    assert metrics.threshold_failures == []
    assert metrics.passed is True
    assert metrics.response_decision_accuracy == 1
    assert metrics.required_evidence_recall is None
    assert metrics.mrr is None
    assert metrics.claim_support_rate is None
    assert metrics.failure_reason_counts == {}
    result = metrics.case_results[0]
    assert result.observed_outcome == "insufficient_evidence"
    assert result.response_decision_accuracy == 1
    assert result.failure_reasons == []


async def test_answerable_case_with_no_results_counts_unexpected_refusal() -> None:
    metrics = await EvaluationRunner(pipeline=NoResultsPipeline()).run(
        cases=[
            EvaluationCase(
                id="positive",
                query="合成の質問",
                relevant_document_ids=["doc-a"],
                expected_response_decision="answer",
            )
        ],
        top_k=5,
    )
    assert metrics.mrr == 0
    assert metrics.response_decision_accuracy == 0
    assert metrics.case_results[0].failure_reasons == ["retrieval_miss", "unexpected_handling"]


async def test_unanswerable_case_answered_counts_unexpected_answer() -> None:
    metrics = await EvaluationRunner(pipeline=StubPipeline()).run(
        cases=[
            EvaluationCase(id="negative", query="合成の質問", expected_response_decision="hold")
        ],
        top_k=5,
    )
    assert metrics.response_decision_accuracy == 0
    assert metrics.case_results[0].failure_reasons == ["unexpected_handling"]


async def test_category_breakdown_summarizes_results_per_category() -> None:
    metrics = await EvaluationRunner(pipeline=StubPipeline()).run(
        cases=[
            EvaluationCase(
                id="answer",
                query="合成の質問",
                category="document_answerable",
                expected_response_decision="answer",
            ),
            EvaluationCase(
                id="hold",
                query="合成の質問",
                category="knowledge_missing",
                expected_response_decision="hold",
            ),
            EvaluationCase(id="none", query="合成の質問"),
        ],
        top_k=5,
    )
    assert [result.category for result in metrics.case_results] == [
        "document_answerable",
        "knowledge_missing",
        None,
    ]
    assert metrics.category_breakdown["document_answerable"].case_count == 1
    assert (
        metrics.category_breakdown["document_answerable"].metrics["response_decision_accuracy"] == 1
    )
    assert (
        metrics.category_breakdown["knowledge_missing"].metrics["response_decision_accuracy"] == 0
    )
    assert metrics.category_breakdown["uncategorized"].metrics["response_decision_accuracy"] is None
    assert set(metrics.category_breakdown["uncategorized"].metrics) == set(EVALUATION_METRIC_NAMES)


async def test_category_breakdown_is_empty_without_categories() -> None:
    runner = EvaluationRunner(pipeline=StubPipeline())
    metrics = await runner.run(cases=[EvaluationCase(id="c", query="承認条件")], top_k=5)
    assert metrics.category_breakdown == {}
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate({"id": "c", "query": "q", "category": "unknown"})


def test_is_abstained_uses_answer_record_insufficient_reason() -> None:
    """不足の理由を返し、モデルが使った根拠が無い回答は拒答とみなす。"""
    request = SearchRequest(query="承認条件")
    answered = _approval_response(request, "trace-1")
    refused = answered.model_copy(
        update={
            "citations": [
                chunk.model_copy(update={"metadata": {"evidence_model_used": False}})
                for chunk in answered.citations
            ],
            "diagnostics": SearchDiagnostics(answer={"insufficient_reason": "資料に記載がない"}),
        }
    )
    partial = answered.model_copy(
        update={"diagnostics": SearchDiagnostics(answer={"insufficient_reason": "一部不足"})}
    )

    assert is_abstained(answered) is False
    assert is_abstained(refused) is True
    # 一部だけ不足でも、モデルが根拠を使って答えた回答は拒答ではない。
    assert is_abstained(partial) is False


def test_expects_answer_is_inferred_from_expectations() -> None:
    assert EvaluationCase(id="a", query="q").expects_answer is False
    assert EvaluationCase(id="b", query="q", required_answer_points=["x"]).expects_answer
    assert EvaluationCase(id="c", query="q", standard_answer="答え").expects_answer
    assert not EvaluationCase(id="d", query="q", standard_answer="   ").expects_answer
    assert not EvaluationCase(
        id="e", query="q", relevant_document_ids=["doc"], answerable=False
    ).expects_answer


async def test_standard_answer_case_is_judged_with_answer_record() -> None:
    judge = FakeJudge(_completed_judgement())
    pipeline = StubPipeline()
    metrics = await EvaluationRunner(pipeline=pipeline, answer_judge=judge).run(
        cases=[
            EvaluationCase(
                id="standard",
                query="合成の質問",
                relevant_document_ids=["doc-1"],
                standard_answer="合成の標準回答",
            ),
            EvaluationCase(id="no-standard", query="合成の質問", relevant_document_ids=["doc-1"]),
        ],
        top_k=5,
        thresholds=resolve_evaluation_suite("strict"),
    )
    assert len(judge.calls) == 2
    call = judge.calls[0]
    assert call["trace_id"] == pipeline.trace_ids[0]
    assert call["evaluation_input"] == EVALUATION_INPUT
    assert call["standard_answer"] == "合成の標準回答"
    assert call["timeout_seconds"] == ENTERPRISE_AI_TIMEOUT_MAX_SECONDS
    assert call["citations"]
    assert metrics.claim_support_rate == 1
    assert metrics.answer_correctness == metrics.requirement_coverage == 1
    assert metrics.metric_case_counts["answer_correctness"] == 1
    assert metrics.metric_case_counts["claim_support_rate"] == 2
    judgement = metrics.case_results[0].answer_evaluation
    assert judgement is not None and judgement.status == "completed"
    assert judgement.claims_supported and judgement.missing_content is False
    assert metrics.case_results[1].answer_correctness is None
    assert metrics.case_results[1].claim_support_rate == 1


async def test_judgement_failures_are_reported_per_axis() -> None:
    """根拠のない主張・必要な項目の欠落・不合格を、それぞれの失敗理由として残す。"""
    judge = FakeJudge(
        _completed_judgement(
            passed=False,
            claim_status="contradicted",
            requirement_coverage=0.5,
            coverage_statuses=("addressed", "missing"),
        )
    )
    runner = EvaluationRunner(pipeline=StubPipeline(), answer_judge=judge)

    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="case-judged",
                query="承認条件",
                relevant_document_ids=["doc-1"],
                standard_answer="承認者は部長です。",
            )
        ],
        top_k=5,
        thresholds=resolve_evaluation_suite("standard"),
    )

    assert metrics.claim_support_rate == 0.0
    assert metrics.requirement_coverage == 0.5
    assert metrics.answer_correctness == 0.0
    assert set(metrics.case_results[0].failure_reasons) == {
        "unsupported_claim",
        "missing_content",
        "answer_failed",
    }
    assert {failure.metric for failure in metrics.threshold_failures} >= {
        "claim_support_rate",
        "requirement_coverage",
        "answer_correctness",
    }
    assert metrics.passed is False


@pytest.mark.parametrize(
    ("judge_result", "status"),
    [
        (TimeoutError(), "timeout"),
        (RuntimeError("secret detail"), "error"),
        ({"status": "input_too_large", "message": "入力上限を超えています。"}, "input_too_large"),
    ],
    ids=["timeout", "exception", "input_too_large"],
)
async def test_incomplete_judgement_is_not_counted_and_fails_the_gate(
    judge_result: Mapping[str, object] | Exception, status: str
) -> None:
    """標準回答で評価できなかったケースは指標に入れず、評価を合格にしない。"""
    runner = EvaluationRunner(pipeline=StubPipeline(), answer_judge=FakeJudge(judge_result))

    metrics = await runner.run(
        cases=[
            EvaluationCase(
                id="case-judged",
                query="承認条件",
                relevant_document_ids=["doc-1"],
                standard_answer="承認者は部長です。",
            )
        ],
        top_k=5,
    )

    judgement = metrics.case_results[0].answer_evaluation
    assert judgement is not None
    assert judgement.status == status
    assert "secret detail" not in str(judgement.model_dump())
    assert metrics.answer_correctness is None
    assert metrics.case_results[0].failure_reasons == ["answer_evaluation_error"]
    assert metrics.error_count == 0
    assert metrics.passed is False


async def test_judgement_is_unavailable_without_answer_record_input() -> None:
    judge = FakeJudge(_completed_judgement())
    metrics = await EvaluationRunner(pipeline=MissPipeline(), answer_judge=judge).run(
        cases=[
            EvaluationCase(id="synthetic", query="合成の質問", standard_answer="合成の標準回答")
        ],
        top_k=5,
    )
    judgement = metrics.case_results[0].answer_evaluation
    assert judgement is not None and judgement.status == "completed"
    assert len(judge.calls) == 1
    evaluation_input = judge.calls[0]["evaluation_input"]
    assert isinstance(evaluation_input, dict) and evaluation_input["evidence_items"]
    assert metrics.metric_case_counts["claim_support_rate"] == 1


def test_summarize_answer_judgement_normalizes_coverage() -> None:
    judgement = summarize_answer_judgement(
        _completed_judgement(requirement_coverage=0.75, coverage_statuses=("partial",))
    )
    assert judgement.requirement_coverage == 0.75
    assert judgement.missing_content is False


def test_saved_legacy_metrics_still_validate() -> None:
    """新規の結果 schema は六指標だけを出力する。"""
    from app.schemas.evaluation import EVALUATION_METRIC_NAMES

    metrics = EvaluationMetrics(case_count=1, mrr=1.0)
    payload = metrics.model_dump()
    assert payload["metrics_version"] == "six_metrics_v1"
    assert set(EVALUATION_METRIC_NAMES) <= payload.keys()
    assert payload["answer_correctness"] is None
    assert payload["response_decision_accuracy"] is None
    assert not {"context_recall", "faithfulness", "answer_keyword_hit_rate"} & payload.keys()
    assert metrics.case_results == []
    assert metrics.threshold_failures == []


class PartiallyFailingPipeline:
    """一部 case だけ失敗する評価 runner 用 pipeline。"""

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        if "失敗" in request.query:
            raise RuntimeError("raw secret detail: INV-SECRET")
        return _approval_response(request, trace_id or "trace-ok")


async def test_evaluation_runner_isolates_case_errors(caplog: LogCaptureFixture) -> None:
    """1 case の検索失敗は batch 全体を中断せず、失敗 case として返す。"""
    runner = EvaluationRunner(pipeline=PartiallyFailingPipeline())

    with caplog.at_level(logging.INFO, logger="app.audit"):
        metrics = await runner.run(
            cases=[
                EvaluationCase(
                    id="case-ok",
                    query="承認条件",
                    relevant_document_ids=["doc-1"],
                    required_answer_points=["120000"],
                ),
                EvaluationCase(
                    id="case-error",
                    query="INV-SECRET の失敗ケース",
                    relevant_document_ids=["doc-2"],
                    required_answer_points=["999"],
                ),
            ],
            top_k=5,
        )

    assert metrics.case_count == 2
    assert metrics.error_count == 1
    assert metrics.passed is False
    # 失敗したケースは指標の平均に入れない(error_count で数える)。
    assert metrics.mrr is not None
    assert metrics.metric_case_counts["mrr"] == 1

    ok_result, error_result = metrics.case_results
    assert ok_result.status == "success"
    assert error_result.case_id == "case-error"
    assert error_result.status == "error"
    assert error_result.error_type == "RuntimeError"
    assert error_result.error_message == EVALUATION_CASE_ERROR_MESSAGE
    assert error_result.relevant_document_ids == ["doc-2"]
    assert error_result.mrr is None
    assert error_result.failure_reasons == ["case_error"]
    assert metrics.failure_reason_counts == {"case_error": 1}
    assert "INV-SECRET" not in str(error_result.model_dump(mode="json"))
    assert "raw secret detail" not in str(error_result.model_dump(mode="json"))

    audit_record = next(record for record in caplog.records if record.message == "rag_search_audit")
    audit_event = cast(Any, audit_record).audit_event
    assert audit_event["trace_id"] == error_result.trace_id
    assert audit_event["outcome"] == "error"
    assert audit_event["error_stage"] == "evaluation"
    assert audit_event["error_type"] == "RuntimeError"
    assert "INV-SECRET" not in str(audit_event)


async def test_evaluation_runner_records_case_metrics(monkeypatch: MonkeyPatch) -> None:
    """評価 case ごとの成功/失敗を低 cardinality metrics に残す。"""
    observed: list[tuple[str, str, float]] = []
    monkeypatch.setattr(
        "app.rag.evaluation.record_evaluation_case",
        lambda mode, status, seconds: observed.append((mode, status, seconds)),
    )
    runner = EvaluationRunner(pipeline=PartiallyFailingPipeline())

    await runner.run(
        cases=[
            EvaluationCase(id="case-ok", query="承認条件", relevant_document_ids=["doc-1"]),
            EvaluationCase(id="case-error", query="失敗ケース", relevant_document_ids=["doc-2"]),
        ],
        top_k=5,
    )

    assert [(mode, status) for mode, status, _ in observed] == [
        ("hybrid", "success"),
        ("hybrid", "error"),
    ]
    assert all(seconds >= 0 for _, _, seconds in observed)


class SlowPipeline:
    """評価 case timeout を再現する pipeline。"""

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        assert trace_id
        await asyncio.sleep(1)
        raise AssertionError("timeout 前に完了しない")


class StagedSlowPipeline:
    """「遅い」を含む質問だけ、回答フロー（answer）の工程で止まる pipeline。"""

    def __init__(self, *, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds
        self.queries: list[str] = []

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        self.queries.append(request.query)
        if "遅い" in request.query:
            if progress_callback is not None:
                await progress_callback(
                    SearchStageProgress(
                        trace_id=trace_id or "trace",
                        stage="answer",
                        outcome="started",
                        elapsed_ms=0.0,
                        attributes={},
                    )
                )
            await asyncio.sleep(self._sleep_seconds)
        return _approval_response(request, trace_id or "trace-ok")


async def test_evaluation_runner_records_timeout_audit(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    """評価 case timeout は error result と脱敏済み RAG 監査ログに残す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_timeout_seconds", 0.001)
    runner = EvaluationRunner(pipeline=SlowPipeline(), settings=settings)

    with caplog.at_level(logging.INFO, logger="app.audit"):
        metrics = await runner.run(
            cases=[
                EvaluationCase(
                    id="case-timeout",
                    query="INV-SECRET の timeout ケース",
                    relevant_document_ids=["doc-timeout"],
                )
            ],
            top_k=5,
        )

    assert metrics.error_count == 1
    assert metrics.passed is False
    result = metrics.case_results[0]
    assert result.status == "error"
    assert result.error_type == "TimeoutError"
    assert result.error_message is not None
    assert "時間切れになった工程: 検索の準備" in result.error_message
    assert "INV-SECRET" not in result.error_message

    audit_record = next(record for record in caplog.records if record.message == "rag_search_audit")
    audit_event = cast(Any, audit_record).audit_event
    assert audit_event["trace_id"] == result.trace_id
    assert audit_event["error_stage"] == "timeout"
    assert "INV-SECRET" not in str(audit_event)


def _timeout_cases() -> list[EvaluationCase]:
    return [
        EvaluationCase(id="case-slow", query="遅い: 承認条件は？", relevant_document_ids=["doc-1"]),
        EvaluationCase(id="case-fast", query="承認条件は？", relevant_document_ids=["doc-1"]),
    ]


async def test_evaluation_case_is_limited_by_answer_timeout() -> None:
    """評価の 1 ケースは、回答生成の上限（rag_answer_timeout_seconds）で打ち切る（#383）。"""
    settings = Settings(rag_answer_timeout_seconds=0.05)
    pipeline = StagedSlowPipeline(sleep_seconds=1.0)
    runner = EvaluationRunner(pipeline=pipeline, settings=settings)

    metrics = await runner.run(cases=_timeout_cases(), top_k=5)

    slow, fast = metrics.case_results
    assert slow.status == "error"
    assert slow.error_type == "TimeoutError"
    assert slow.failure_reasons == ["case_error"]
    assert slow.error_stage == "answer"
    assert slow.error_message is not None
    assert "上限の 1 秒以内に終わりませんでした" in slow.error_message
    assert "時間切れになった工程: 質問の条件を整理して回答を作る" in slow.error_message
    assert "遅い" not in slow.error_message
    assert fast.status == "success"
    assert pipeline.queries == ["遅い: 承認条件は？", "承認条件は？"]
    assert metrics.error_count == 1
    assert metrics.passed is False


async def test_evaluation_time_budget_stops_remaining_cases() -> None:
    """評価全体の上限に達したら、実行中のケースを打ち切り、残りのケースは実行せずに記録する。"""
    settings = Settings(rag_answer_timeout_seconds=5.0)
    pipeline = StagedSlowPipeline(sleep_seconds=1.0)
    runner = EvaluationRunner(pipeline=pipeline, settings=settings)

    metrics = await runner.run(cases=_timeout_cases(), top_k=5, time_budget_seconds=0.05)

    slow, skipped = metrics.case_results
    assert slow.status == "error"
    assert slow.error_message is not None
    assert "評価全体の時間の上限に達したため" in slow.error_message
    assert skipped.error_type == "EvaluationTimeBudgetExceeded"
    assert skipped.failure_reasons == ["case_error"]
    assert pipeline.queries == ["遅い: 承認条件は？"]
    assert metrics.error_count == 2


def test_evaluation_settings_map_overrides() -> None:
    """評価は全体の既定のまま動かし、評価の上書き（rag_overrides）だけを一時適用する。"""
    base = Settings(
        rag_query_strategy="auto_routing",
        rag_answer_flow="crag",
        rag_neighbor_child_count=3,
        rag_rerank_enabled=True,
        rag_rrf_k=60,
        rag_context_group_max_chunks=4,
        oracle_vector_target_accuracy=95,
    )

    default = evaluation_settings(base, None)
    overridden = evaluation_settings(
        base,
        EvaluationRagOverrides(
            query_strategy="rag_fusion",
            answer_flow="standard_rag",
            neighbor_child_count=1,
            rerank_enabled=False,
            rrf_k=30,
            context_group_max_chunks=2,
            oracle_vector_target_accuracy=90,
        ),
    )

    assert default.rag_query_strategy == "auto_routing"
    assert overridden.rag_query_strategy == "rag_fusion"
    assert overridden.rag_answer_flow == "standard_rag"
    assert overridden.rag_neighbor_child_count == 1
    assert overridden.rag_rerank_enabled is False
    assert overridden.rag_rrf_k == 30
    assert overridden.rag_context_group_max_chunks == 2
    assert overridden.oracle_vector_target_accuracy == 90
    # 元の Settings は変えない。
    assert base.rag_query_strategy == "auto_routing"


def test_evaluation_settings_map_llm_latency_overrides() -> None:
    """回答の LLM の時間の短縮(#1499)の設定を評価の上書きで指定できる。"""
    base = Settings()
    overridden = evaluation_settings(
        base,
        EvaluationRagOverrides(
            strategy_reasoning_effort="high",
            judge_reasoning_effort="low",
            generation_reasoning_effort="low",
            crag_skip_on_strong_signals=True,
            crag_skip_min_rerank_score=0.7,
            query_expansion_max_queries=2,
        ),
    )

    assert overridden.rag_strategy_reasoning_effort == "high"
    assert overridden.rag_judge_reasoning_effort == "low"
    assert overridden.rag_generation_reasoning_effort == "low"
    assert overridden.rag_crag_skip_on_strong_signals is True
    assert overridden.rag_crag_skip_min_rerank_score == 0.7
    assert overridden.rag_query_expansion_max_queries == 2
    # 既定は「戦略はモデルの登録値・根拠の評価 medium(#1550)・生成はモデルの登録値」、
    # CRAG の省略は off、検索文の上限は従来の 5。
    assert (
        base.rag_strategy_reasoning_effort,
        base.rag_judge_reasoning_effort,
        base.rag_generation_reasoning_effort,
    ) == ("model", "medium", "model")
    assert base.rag_crag_skip_on_strong_signals is False
    assert base.rag_query_expansion_max_queries == 5


def test_evaluation_settings_map_judge_latency_overrides() -> None:
    """監査の推論の量・CRAG の評価の入力の圧縮・評価し直しの省略(#1528)を比べられる。"""
    base = Settings()
    overridden = evaluation_settings(
        base,
        EvaluationRagOverrides(
            audit_reasoning_effort="low",
            crag_compact_input=not base.rag_crag_compact_input,
            section_skip_regrade_at_scope=not base.rag_section_skip_regrade_at_scope,
        ),
    )

    assert overridden.rag_audit_reasoning_effort == "low"
    assert overridden.rag_crag_compact_input is (not base.rag_crag_compact_input)
    skip_regrade = base.rag_section_skip_regrade_at_scope
    assert overridden.rag_section_skip_regrade_at_scope is (not skip_regrade)
    # 上書きの無い項目は既定のまま。
    untouched = evaluation_settings(base, EvaluationRagOverrides(judge_reasoning_effort="low"))
    assert untouched.rag_audit_reasoning_effort == base.rag_audit_reasoning_effort
    assert untouched.rag_crag_compact_input is base.rag_crag_compact_input
    # CRAG の評価の候補の短い ID(#1864)も比べられる。
    short = evaluation_settings(base, EvaluationRagOverrides(crag_short_candidate_ids=False))
    assert short.rag_crag_short_candidate_ids is False
    assert untouched.rag_crag_short_candidate_ids is base.rag_crag_short_candidate_ids
    # 監査の通った判定の reason を空にする・是正の組の回数の上限(#1912)も比べられる。
    rounds = evaluation_settings(
        base,
        EvaluationRagOverrides(answer_audit_compact_reasons=True, answer_max_correction_rounds=0),
    )
    assert rounds.rag_answer_audit_compact_reasons is True
    assert rounds.rag_answer_max_correction_rounds == 0
    assert untouched.rag_answer_max_correction_rounds == base.rag_answer_max_correction_rounds


async def test_evaluation_run_applies_rag_overrides(
    monkeypatch: MonkeyPatch,
) -> None:
    """評価の上書き（rag_overrides）を一時 Settings として pipeline へ渡す。"""
    observed_settings: list[Settings] = []

    class CapturingRagPipeline:
        def __init__(self, settings: Settings) -> None:
            observed_settings.append(settings)

        async def run(
            self,
            request: SearchRequest,
            trace_id: str | None = None,
            progress_callback: SearchStageProgressCallback | None = None,
        ) -> SearchResponse:
            return _approval_response(request, trace_id or "trace")

    monkeypatch.setattr("app.rag.evaluation.RagPipeline", CapturingRagPipeline)
    runner = EvaluationRunner(
        settings=Settings(rag_answer_timeout_seconds=30.0, rag_rrf_k=60),
        answer_judge=FakeJudge(_completed_judgement()),
    )

    cases = [EvaluationCase(id="case", query="承認条件", relevant_document_ids=["doc-1"])]
    await runner.run(cases=cases, top_k=3)
    await runner.run(
        cases=cases,
        top_k=3,
        rag_overrides=EvaluationRagOverrides(query_strategy="rag_fusion", rrf_k=10),
    )

    assert [settings.rag_rrf_k for settings in observed_settings] == [60, 10]
    assert observed_settings[1].rag_query_strategy == "rag_fusion"


def test_evaluation_api_rejects_empty_cases() -> None:
    response = client.post("/api/evaluation/run", json={"cases": [], "top_k": 5})

    assert response.status_code == 422
    body = response.json()
    assert body["data"] is None
    assert body["error_messages"]


@pytest.mark.parametrize(
    "thresholds",
    [{"context_recall": 1.1}, {"precision_at_k": 0.5}, {"groundedness_pass_rate": 0.9}],
    ids=["out-of-range", "removed-precision", "removed-groundedness"],
)
def test_evaluation_api_rejects_invalid_thresholds(thresholds: dict[str, float]) -> None:
    """範囲外の値と、削除した指標の閾値は受け付けない(gate が効かないまま通さない)。"""
    response = client.post(
        "/api/evaluation/run",
        json={"cases": [{"id": "c", "query": "承認条件"}], "thresholds": thresholds},
    )

    assert response.status_code == 422
    assert response.json()["error_messages"]


def test_evaluation_api_ignores_removed_run_fields(monkeypatch: MonkeyPatch) -> None:
    """以前の golden set の mode / rerank_top_n / 削除した期待値の欄は無視して評価する。"""
    captured: dict[str, Any] = {}

    async def fake_run(self: EvaluationRunner, **kwargs: Any) -> EvaluationMetrics:
        captured.update(kwargs)
        return EvaluationMetrics(case_count=1)

    class NoopOracleClient:
        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            return "eval-1"

    monkeypatch.setattr(EvaluationRunner, "run", fake_run)
    monkeypatch.setattr("app.api.routes.evaluation.OracleClient", NoopOracleClient)

    response = client.post(
        "/api/evaluation/run",
        json={
            "cases": [
                {
                    "id": "c",
                    "query": "承認条件",
                    "expected_content_kind": "table",
                    "expected_section_paths": ["経費 > 承認"],
                }
            ],
            "mode": "keyword",
            "rerank_top_n": 3,
        },
    )

    assert response.status_code == 200
    assert "mode" not in captured
    # 省略時はチャット・RAG 検索と同じ既定（#3512）。
    assert captured["top_k"] == 50


@pytest.mark.parametrize(
    "path",
    ["/api/evaluation/jobs/run", "/api/evaluation/run"],
    ids=["jobs-run", "run"],
)
@pytest.mark.parametrize(
    ("cases", "expected"),
    [
        (
            [{"id": "same", "query": "A"}, {"id": " same ", "query": "B"}],
            "評価ケースの id が重複しています: same",
        ),
        ([{"id": "  ", "query": "A"}], "評価ケースの id を入力してください。"),
        ([{"id": "x" * 201, "query": "A"}], "cases[0].id: 200 文字以内で入力してください。"),
    ],
    ids=["duplicate", "blank", "too-long"],
)
def test_evaluation_api_rejects_invalid_case_ids(
    path: str, cases: list[dict[str, str]], expected: str
) -> None:
    """結果のケースを id で区別できるよう、重複・空・長すぎる id を投入前に拒否する（#977）。"""
    response = client.post(path, json={"cases": cases})

    assert response.status_code == 422
    assert any(expected in message for message in response.json()["error_messages"])


@pytest.mark.parametrize(
    "rag_overrides",
    [
        {"context_window_chars": 4096},
        {"query_strategy": "unknown"},
        {"neighbor_child_count": 21},
    ],
    ids=["removed-override", "bad-strategy", "bad-neighbors"],
)
def test_evaluation_api_rejects_invalid_rag_overrides(rag_overrides: dict[str, Any]) -> None:
    """削除した上書きのキーと、範囲外・未知の値は受け付けない。"""
    response = client.post(
        "/api/evaluation/jobs/run",
        json={"cases": [{"id": "case-1", "query": "承認条件"}], "rag_overrides": rag_overrides},
    )

    assert response.status_code == 422
    assert response.json()["error_messages"]


def test_evaluation_api_rejects_blank_case_query() -> None:
    response = client.post(
        "/api/evaluation/run",
        json={"cases": [{"id": "blank-query", "query": "   "}], "top_k": 5},
    )

    assert response.status_code == 422
    assert response.json()["error_messages"]


def test_evaluation_api_persists_redacted_artifact(monkeypatch: MonkeyPatch) -> None:
    """評価 API は query・期待語・標準回答の原文を除いた artifact summary を保存する。"""
    artifacts: list[dict[str, Any]] = []

    class FakeEvaluationRunner:
        async def run(self, **kwargs: object) -> EvaluationMetrics:
            del kwargs
            return EvaluationMetrics(case_count=1, passed=True)

    class FakeOracleClient:
        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            artifacts.append(artifact)
            return "eval-1"

    monkeypatch.setattr("app.api.routes.evaluation.EvaluationRunner", FakeEvaluationRunner)
    monkeypatch.setattr("app.api.routes.evaluation.OracleClient", FakeOracleClient)

    response = client.post(
        "/api/evaluation/run",
        json={
            "cases": [
                {
                    "id": "secret-case",
                    "query": "社外秘キーワード ABC-123 の承認条件",
                    "relevant_document_ids": ["doc-1"],
                    "required_answer_points": ["ABC-123"],
                    "standard_answer": "承認者は XYZ-999 部長です。",
                }
            ],
            "top_k": 1,
            "knowledge_base_ids": ["kb-1"],
        },
    )

    assert response.status_code == 200
    assert len(artifacts) == 1
    artifact_text = str(artifacts[0])
    assert "社外秘キーワード" not in artifact_text
    assert "ABC-123" not in artifact_text
    assert "XYZ-999" not in artifact_text
    case_summary = artifacts[0]["request_summary"]["cases"][0]
    assert case_summary["query_hash"]
    assert case_summary["standard_answer_hash"]
    assert artifacts[0]["knowledge_base_ids"] == ["kb-1"]


def test_evaluation_api_limits_whole_run_by_time_budget(monkeypatch: MonkeyPatch) -> None:
    """評価 API は、評価全体を画面・Nginx の待ちより短い上限（600 秒）で打ち切らせる（#383）。"""
    from app.api.routes import evaluation as evaluation_route

    observed: dict[str, object] = {}

    class CapturingEvaluationRunner:
        async def run(self, **kwargs: object) -> EvaluationMetrics:
            observed["run"] = kwargs.get("time_budget_seconds")
            return EvaluationMetrics(case_count=0)

    class NoopOracleClient:
        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            del artifact
            return "eval-1"

    monkeypatch.setattr("app.api.routes.evaluation.EvaluationRunner", CapturingEvaluationRunner)
    monkeypatch.setattr("app.api.routes.evaluation.OracleClient", NoopOracleClient)
    case = {"id": "case-1", "query": "承認条件は？", "relevant_document_ids": ["doc-1"]}

    run_response = client.post("/api/evaluation/run", json={"cases": [case]})

    assert run_response.status_code == 200
    assert evaluation_route.EVALUATION_RUN_TIMEOUT_SECONDS == ENTERPRISE_AI_TIMEOUT_MAX_SECONDS
    assert observed == {"run": evaluation_route.EVALUATION_RUN_TIMEOUT_SECONDS}


@pytest.mark.usefixtures("oracle_db")
def test_evaluation_api_runs_against_local_pipeline(monkeypatch: MonkeyPatch) -> None:
    """API 経由でも、回答エンジンの回答の記録から指標を返す(実 Oracle。LLM はスタブ)。"""
    from app.rag.answer_engine import AnswerEngine, AnswerOutcome

    async def refuse(
        self: AnswerEngine, request: SearchRequest, *, step_callback: object = None
    ) -> AnswerOutcome:
        del self, request, step_callback
        return AnswerOutcome(
            answer="資料からは確認できませんでした。",
            citations=[],
            diagnostics={"insufficient_reason": "該当する資料がありません。"},
            evaluation_input=None,
        )

    monkeypatch.setattr(AnswerEngine, "run", refuse)
    response = client.post(
        "/api/evaluation/run",
        json={
            "cases": [
                {"id": "empty-store", "query": "合成の質問", "expected_response_decision": "hold"}
            ],
            "thresholds": {"response_decision_accuracy": 1},
            "top_k": 5,
            "knowledge_base_ids": ["kb-eval-empty-isolated"],
            "suite": "strict",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["case_count"] == 1
    assert data["evaluation_suite"] == "strict"
    # 答えるべきでない質問に答えなかったので合格(#301)。
    assert data["response_decision_accuracy"] == 1.0
    assert data["required_evidence_recall"] is None
    assert data["passed"] is True
    assert data["threshold_failures"] == []
    result = data["case_results"][0]
    assert result["observed_outcome"] == "insufficient_evidence"
    assert result["failure_reasons"] == []


def _eval_run_body() -> dict[str, Any]:
    return {
        "cases": [
            {
                "id": "c1",
                "query": "承認条件は?",
                "relevant_document_ids": ["doc-1"],
                "required_answer_points": ["承認"],
            }
        ]
    }


def test_run_evaluation_applies_suite_thresholds_when_request_omits(
    monkeypatch: MonkeyPatch,
) -> None:
    """request に thresholds 未指定なら設定の基準の閾値を適用し、基準を stamp する。"""
    captured: dict[str, Any] = {}

    async def fake_run(self: EvaluationRunner, **kwargs: Any) -> EvaluationMetrics:
        captured["thresholds"] = kwargs.get("thresholds")
        return EvaluationMetrics(case_count=1)

    monkeypatch.setattr(EvaluationRunner, "run", fake_run)
    monkeypatch.setattr(get_settings(), "rag_evaluation_suite", "strict")

    resp = client.post("/api/evaluation/run", json=_eval_run_body())

    assert resp.status_code == 200
    assert resp.json()["data"]["evaluation_suite"] == "strict"
    assert captured["thresholds"].response_decision_accuracy == 1.0


def test_run_evaluation_request_thresholds_take_precedence_over_suite(
    monkeypatch: MonkeyPatch,
) -> None:
    """request の thresholds は基準より優先される(空の thresholds で判定を行わない)。"""
    captured: dict[str, Any] = {}

    async def fake_run(self: EvaluationRunner, **kwargs: Any) -> EvaluationMetrics:
        captured["thresholds"] = kwargs.get("thresholds")
        return EvaluationMetrics(case_count=1)

    monkeypatch.setattr(EvaluationRunner, "run", fake_run)

    body = {**_eval_run_body(), "thresholds": {"mrr": 0.42}, "suite": "strict"}
    resp = client.post("/api/evaluation/run", json=body)

    assert resp.status_code == 200
    assert resp.json()["data"]["evaluation_suite"] == "strict"
    assert captured["thresholds"].mrr == 0.42
    assert captured["thresholds"].response_decision_accuracy is None

    resp = client.post("/api/evaluation/run", json={**_eval_run_body(), "thresholds": {}})
    assert resp.status_code == 200
    assert captured["thresholds"].model_dump(exclude_none=True) == {}


def test_run_evaluation_rejects_removed_suite_name() -> None:
    """削除した基準の名前は request では受け付けない(設定値は後継へ寄せる)。"""
    resp = client.post("/api/evaluation/run", json={**_eval_run_body(), "suite": "strict_ci"})

    assert resp.status_code == 422


def test_run_evaluation_ignores_kb_legacy_evaluation_suite_when_request_omits(
    monkeypatch: MonkeyPatch,
) -> None:
    """単一 KB 指定時も KB legacy evaluation_suite は使わずグローバル既定が効く。"""
    from app.api.routes import evaluation as evaluation_route

    async def fake_run(self: EvaluationRunner, **kwargs: Any) -> EvaluationMetrics:
        return EvaluationMetrics(case_count=1)

    class FakeOracleClient:
        async def get_knowledge_base(self, knowledge_base_id: str) -> None:
            raise AssertionError("KB legacy query は評価の基準の解決で参照しない")

        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            return "eval-1"

    monkeypatch.setattr(EvaluationRunner, "run", fake_run)
    monkeypatch.setattr(evaluation_route, "OracleClient", FakeOracleClient)
    monkeypatch.setattr(get_settings(), "rag_evaluation_suite", "standard")

    body = {**_eval_run_body(), "knowledge_base_ids": ["kb-1"]}
    resp = client.post("/api/evaluation/run", json=body)

    assert resp.status_code == 200
    assert resp.json()["data"]["evaluation_suite"] == "standard"


def test_threshold_skips_unmeasured_metrics() -> None:
    """測れなかった指標(None)の閾値は判定しない。"""
    from app.rag.evaluation import _threshold_failures

    failures = _threshold_failures(
        EvaluationThresholds(answer_correctness=0.8, mrr=0.5),
        {"answer_correctness": None, "mrr": 0.4},
    )

    assert [(failure.metric, failure.actual) for failure in failures] == [("mrr", 0.4)]


# ---- 業務支援の採点（#1231） ------------------------------------------------------------


def test_handling_helpers_score_outcome_steps_forbidden_and_conditions() -> None:
    from app.rag.evaluation_handling import (
        condition_coverage,
        forbidden_hits,
        observed_outcome,
        step_order_score,
    )

    assert observed_outcome({"outcome": "needs_human"}, abstained=True).source == "explicit"
    assert observed_outcome({}, abstained=True).outcome == "insufficient_evidence"
    assert observed_outcome({"external_data_required": True}, abstained=False).outcome == (
        "needs_environment_data"
    )
    assert observed_outcome({"needs_human_review": True}, abstained=False).outcome == "conditional"
    assert observed_outcome(None, abstained=False).outcome == "answered"
    # 未知の対応の値は推定に戻す。
    assert observed_outcome({"outcome": "other"}, abstained=False).source == "inferred"

    answer = "1. アカウントを登録する。2. 権限を付与する。3. 通知を設定する。"
    assert step_order_score(answer, ["登録", "権限", "通知"]) == (1.0, [])
    score, missing = step_order_score(answer, ["通知", "登録", "承認"])
    assert (round(score, 4), missing) == (0.3333, ["承認"])
    assert step_order_score(answer, []) == (1.0, [])
    assert forbidden_hits("グループ　に付与してください", ["グループに付与", "削除"]) == [
        "グループに付与"
    ]
    assert condition_coverage(answer, ["権限", "対象の利用者"]) == (0.5, ["対象の利用者"])


async def test_business_support_cases_score_handling_metrics_and_reasons() -> None:
    cases = [
        EvaluationCase(
            id="answer",
            query="合成の質問",
            category="document_answerable",
            expected_response_decision="answer",
            required_answer_points=["合成の要点"],
            required_conditions=["合成の条件"],
        ),
        EvaluationCase(
            id="clarify",
            query="合成の質問",
            category="clarification_required",
            expected_response_decision="clarify",
        ),
    ]
    judge = FakeJudge(
        _completed_judgement(requirement_coverage=0.5, coverage_statuses=("missing",))
    )
    metrics = await EvaluationRunner(pipeline=StubPipeline(), answer_judge=judge).run(
        cases=cases, top_k=5
    )
    first, second = metrics.case_results
    assert (first.observed_outcome, first.outcome_source) == ("answered", "inferred")
    assert first.response_decision_accuracy == 1
    assert first.requirement_coverage == 0.5
    assert "missing_content" in first.failure_reasons
    assert second.response_decision_accuracy == 0
    assert "unexpected_handling" in second.failure_reasons
    assert second.requirement_coverage is None
    assert metrics.response_decision_accuracy == 0.5
    assert metrics.metric_case_counts["requirement_coverage"] == 1
    assert (
        metrics.category_breakdown["clarification_required"].metrics["response_decision_accuracy"]
        == 0
    )
    assert "合成の条件" in str(judge.calls[0]["standard_answer"])


# ---- 検索・回答プロファイルを指定した評価（#1249） ------------------------------------------


async def test_profile_evaluation_resolves_each_case_through_the_profile() -> None:
    seen: list[tuple[str | None, str]] = []

    async def resolver(request: Any, settings: Any, guide_context: Any) -> tuple[Any, Any]:
        seen.append((request.search_answer_profile_id, request.query))
        return request.model_copy(update={"knowledge_base_ids": ["kb-profile"]}), settings

    pipeline = StubPipeline()
    runner = EvaluationRunner(pipeline=pipeline, profile_resolver=resolver)
    cases = [EvaluationCase(id="a", query="承認条件"), EvaluationCase(id="b", query="期限")]
    metrics = await runner.run(cases=cases, top_k=5, search_answer_profile_id="bv-1")
    assert seen == [("bv-1", "承認条件"), ("bv-1", "期限")]
    assert metrics.error_count == 0

    # 指定しなければ解決しない（全体の既定。#301）。
    seen.clear()
    await runner.run(cases=cases, top_k=5)
    assert seen == []


async def test_profile_resolution_failure_fails_only_that_case() -> None:
    from fastapi import HTTPException

    async def resolver(request: Any, settings: Any, guide_context: Any) -> tuple[Any, Any]:
        raise HTTPException(status_code=409, detail="アーカイブ済み")

    runner = EvaluationRunner(pipeline=StubPipeline(), profile_resolver=resolver)
    metrics = await runner.run(
        cases=[EvaluationCase(id="a", query="承認条件")], top_k=5, search_answer_profile_id="bv-x"
    )
    assert metrics.error_count == 1
    assert metrics.case_results[0].status == "error"


# ---- 確認の質問・引き継ぎは拒答・検索の取りこぼしではない（#1259） ---------------------------


def _response_with_outcome(outcome: str | None, *, citations: bool) -> Any:
    from app.schemas.search import SearchDiagnostics, SearchResponse

    answer: dict[str, Any] = {"insufficient_reason": ""}
    if outcome is not None:
        answer["outcome"] = outcome
    return SearchResponse(
        answer="権限は個別の利用者とグループのどちらに付けますか？",
        citations=[RetrievedChunk(document_id="doc-1", chunk_id="doc-1:0", text="本文", score=1.0)]
        if citations
        else [],
        trace_id="t",
        elapsed_ms=1.0,
        diagnostics=SearchDiagnostics(answer=answer),
    )


def test_clarification_is_not_a_refusal_or_retrieval_miss() -> None:
    from app.rag.evaluation import _case_result, is_abstained

    case = EvaluationCase(id="c", query="権限を付与したい", relevant_document_ids=["doc-1"])
    clarify = _response_with_outcome("needs_clarification", citations=False)
    assert is_abstained(clarify) is False
    result = _case_result(case, clarify, None)
    assert result.required_evidence_recall is None
    assert result.response_decision_accuracy is None
    assert "retrieval_miss" not in result.failure_reasons
    assert "unexpected_refusal" not in result.failure_reasons

    assert is_abstained(_response_with_outcome("insufficient_evidence", citations=False)) is True
    assert is_abstained(_response_with_outcome("answered", citations=True)) is False
    # 対応を持たない古い記録は、引用が無ければ拒答（今までどおり）。
    assert is_abstained(_response_with_outcome(None, citations=False)) is True


def test_equivalent_documents_count_as_the_relevant_document() -> None:
    """同じ名前の文書（同じ文書とみなす文書。#2086）の引用は、正解の文書・必要な根拠に一致する。"""
    from app.rag.evaluation import _case_result

    case = EvaluationCase(
        id="c",
        query="承認条件は？",
        relevant_document_ids=["doc-new"],
        equivalent_document_ids={"doc-new": ["doc-1"]},
        required_evidence=[
            EvaluationEvidence(id="e1", document_id="doc-new", page_start=2, text="120000")
        ],
    )
    request = SearchRequest(query="承認条件は？")

    result = _case_result(case, _approval_response(request, "t"), None)

    assert result.retrieved_document_ids == ["doc-new"]
    assert result.mrr == 1.0
    assert result.required_evidence_recall == 1.0
    assert result.missing_evidence == []


# ---- 既知の条件・往復・別解・区分・必要な根拠（#1284） --------------------------------------


def test_case_schema_accepts_conditions_turns_alternatives_split_and_evidence() -> None:
    case = EvaluationCase.model_validate(
        {
            "id": "mt",
            "query": "アクセス権限を付与したい",
            "split": "holdout",
            "conditions": {" target ": " 個別 ", "empty": " "},
            "required_answer_points": ["詳細画面", "付与"],
            "expected_response_decision": "answer",
            "required_evidence": [
                {"id": "e1", "document_id": "file:manual.pdf", "text": " 個別の利用者 "},
                {"id": "e2", "document_id": " ", "text": "付与"},
            ],
            "turns": [
                {
                    "reply": "  個別の利用者です ",
                    "conditions": {"approved": "はい"},
                    "expected_outcomes": ["answered"],
                }
            ],
        }
    )
    assert case.split == "holdout"
    assert case.conditions == {"target": "個別"}
    assert case.required_answer_points == ["詳細画面", "付与"]
    assert case.expected_response_decision == "answer"
    assert case.turns[0].reply == "個別の利用者です"
    assert case.required_evidence[0].text == "個別の利用者"
    assert case.required_evidence[1].document_id is None
    assert case.all_condition_ids() == {"target", "approved"}
    # 根拠だけのケースも答えるべき質問とみなす。
    assert EvaluationCase(
        id="e", query="q", required_evidence=[{"id": "e1", "text": "x"}]
    ).expects_answer
    # 既存の評価セット（新しい欄なし）はそのまま読める。
    legacy = EvaluationCase(id="old", query="q")
    assert (legacy.split, legacy.conditions, legacy.turns, legacy.required_evidence) == (
        None,
        {},
        [],
        [],
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"split": "train"},
        {"expected_response_decision": "invalid"},
        {"turns": [{"reply": "はい", "unknown": 1}]},
        {"turns": [{"reply": " "}]},
        {"turns": [{"reply": "はい"}] * 6},
        {"required_evidence": [{"id": "e", "text": "a"}, {"id": "e", "text": "b"}]},
        {
            "conditions": {f"c{i}": "v" for i in range(20)},
            "turns": [{"reply": "r", "conditions": {f"t{i}": "v" for i in range(11)}}],
        },
    ],
    ids=[
        "unknown-split",
        "invalid-response-decision",
        "turn-extra-field",
        "blank-reply",
        "too-many-turns",
        "duplicate-evidence",
        "too-many-conditions",
    ],
)
def test_case_schema_rejects_invalid_contract(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate({"id": "x", "query": "q", **payload})


def test_nfkc_matching_ignores_width_case_and_whitespace() -> None:
    from app.rag.evaluation_handling import (
        best_step_order_score,
        contains_normalized,
        forbidden_hits,
    )

    answer = "ＡＵＴＨ．ＬＯＧ を確認し、\n すぐに　削除して ください。"
    assert forbidden_hits(answer, ["すぐに削除してください", "auth.log", "承認は不要"]) == [
        "すぐに削除してください",
        "auth.log",
    ]
    assert contains_normalized("最長 ３０ 日です", "最長30日")
    assert not contains_normalized("本文", " ")
    # 別解のうち、いちばん点の高い列で採点する（同点なら期待する手順）。
    text = "再ログインしてください。"
    assert best_step_order_score(text, [["ログアウト", "ログインし直"], ["再ログイン"]]) == (
        1.0,
        [],
    )
    assert best_step_order_score(text, [["ログアウト"], ["サインアウト"]]) == (0.0, ["ログアウト"])
    assert best_step_order_score(text, []) == (1.0, [])


def _turn_response(
    request: SearchRequest,
    trace_id: str,
    *,
    answer: str,
    outcome: str,
    citations: Sequence[RetrievedChunk] = (),
    clarifications: Sequence[str] = (),
) -> SearchResponse:
    details: dict[str, Any] = {"outcome": outcome, "insufficient_reason": ""}
    if clarifications:
        details["envelope"] = {
            "clarifications": [{"condition_id": item} for item in clarifications]
        }
    return SearchResponse(
        answer=answer,
        citations=list(citations),
        trace_id=trace_id,
        elapsed_ms=10.0,
        diagnostics=SearchDiagnostics(answer=details),
    )


class ConversationPipeline:
    """最初の質問には確認の質問、返答には根拠付きの回答を返す（会話の履歴と条件を記録する）。"""

    def __init__(self, *, reask: bool = False, fail_on_turn: int | None = None) -> None:
        self.reask = reask
        self.fail_on_turn = fail_on_turn
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
        *,
        history: Sequence[Any] | None = None,
    ) -> SearchResponse:
        assert trace_id
        turn = len(history or []) // 2
        self.calls.append(
            {
                "query": request.query,
                "conditions": dict(request.conditions),
                "history": [(item.role, item.content) for item in history or []],
                "trace_id": trace_id,
            }
        )
        if turn == self.fail_on_turn:
            raise RuntimeError("boom")
        if turn == 0:
            return _turn_response(
                request,
                trace_id,
                answer="権限は個別の利用者とグループのどちらに付けますか？",
                outcome="needs_clarification",
                clarifications=["target"],
            )
        return _turn_response(
            request,
            trace_id,
            answer=(
                "どちらに付けますか？"
                if self.reask
                else "利用者の詳細画面の「権限」タブで付与します。"
            ),
            outcome="needs_clarification" if self.reask else "answered",
            clarifications=["target"] if self.reask else (),
            citations=[
                RetrievedChunk(
                    document_id="doc-manual",
                    chunk_id="doc-manual:3",
                    text="個別の利用者に付与する場合: 利用者の詳細画面の「権限」タブ",
                    score=1.0,
                )
            ],
        )


def _multi_turn_case(**overrides: Any) -> EvaluationCase:
    payload: dict[str, Any] = {
        "id": "mt-grant",
        "query": "アクセス権限を付与したい",
        "category": "clarification_required",
        "split": "dev",
        "relevant_document_ids": ["doc-manual"],
        "conditions": {"scope": "本番"},
        "expected_outcomes": ["needs_clarification"],
        "required_conditions": ["個別", "グループ"],
        "required_evidence": [
            {"id": "grant-individual", "document_id": "doc-manual", "text": "個別の利用者に付与"},
            {"id": "other-doc", "document_id": "doc-other", "text": "個別の利用者"},
        ],
        "turns": [
            {
                "reply": "個別の利用者に付けます",
                "conditions": {"target": "個別"},
                "expected_outcomes": ["answered", "conditional"],
                "required_answer_points": ["権限タブ", "付与"],
                "expected_response_decision": "answer",
            }
        ],
    }
    payload.update(overrides)
    return EvaluationCase.model_validate(payload)


async def test_runner_sends_conditions_and_runs_turns_in_order_with_history() -> None:
    pipeline = ConversationPipeline()
    metrics = await EvaluationRunner(pipeline=pipeline).run(cases=[_multi_turn_case()], top_k=5)

    first, second = pipeline.calls
    # 最初の質問は既知の条件だけ、返答は前の往復の履歴と、足した条件で送る。
    assert (first["query"], first["conditions"], first["history"]) == (
        "アクセス権限を付与したい",
        {"scope": "本番"},
        [],
    )
    assert second["query"] == "個別の利用者に付けます"
    assert second["conditions"] == {"scope": "本番", "target": "個別"}
    assert second["history"] == [
        ("USER", "アクセス権限を付与したい"),
        ("ASSISTANT", "権限は個別の利用者とグループのどちらに付けますか？"),
    ]
    assert first["trace_id"] != second["trace_id"]

    result = metrics.case_results[0]
    assert result.status == "success"
    assert [turn.turn for turn in result.turn_results] == [0, 1]
    clarify, answer = result.turn_results
    assert (clarify.observed_outcome, clarify.response_decision_accuracy) == (
        "needs_clarification",
        1.0,
    )
    assert clarify.expected_decisions == ["clarify"]
    # 最初の質問で聞いた target は、まだ渡していない条件なので聞き直しではない。
    assert clarify.turn == 0
    # 「権限タブ」は「「権限」タブ」と一致しないが、別解（詳細画面 → 付与）で満点になる。
    assert answer.response_decision_accuracy == 1.0
    assert answer.expected_decisions == ["answer"]
    # ケースの採点: 対応はすべての往復で正しい、検索・根拠は最後の回答で測る。
    assert result.observed_outcome == "answered"
    assert result.response_decision_accuracy == 1.0
    assert result.trace_id == second["trace_id"]
    assert result.mrr == 1.0
    assert result.elapsed_ms == 20.0
    assert (result.required_evidence_recall, result.missing_evidence) == (0.5, ["other-doc"])
    assert result.failure_reasons == ["evidence_miss"]
    assert metrics.required_evidence_recall == 0.5
    assert metrics.metric_case_counts["required_evidence_recall"] == 1
    assert metrics.response_decision_accuracy == 1.0


async def test_runner_flags_reasked_known_condition() -> None:
    metrics = await EvaluationRunner(pipeline=ConversationPipeline(reask=True)).run(
        cases=[_multi_turn_case()], top_k=5
    )
    result = metrics.case_results[0]
    assert result.turn_results[1].observed_outcome == "needs_clarification"
    assert result.turn_results[1].response_decision_accuracy == 0.0
    assert result.response_decision_accuracy == 0.0
    assert "unexpected_handling" in result.failure_reasons


async def test_runner_turn_failure_fails_the_case() -> None:
    metrics = await EvaluationRunner(pipeline=ConversationPipeline(fail_on_turn=1)).run(
        cases=[_multi_turn_case()], top_k=5
    )
    assert metrics.error_count == 1
    result = metrics.case_results[0]
    assert (result.status, result.split, result.failure_reasons) == ("error", "dev", ["case_error"])


async def test_profile_evaluation_passes_prior_questions_as_guide_context() -> None:
    seen: list[tuple[str, list[str], dict[str, str]]] = []

    async def resolver(request: Any, settings: Any, guide_context: Any) -> tuple[Any, Any]:
        seen.append((request.query, list(guide_context), dict(request.conditions)))
        return request, settings

    runner = EvaluationRunner(pipeline=ConversationPipeline(), profile_resolver=resolver)
    await runner.run(cases=[_multi_turn_case()], top_k=5, search_answer_profile_id="bv-1")
    assert seen == [
        ("アクセス権限を付与したい", [], {"scope": "本番"}),
        (
            "個別の利用者に付けます",
            ["アクセス権限を付与したい"],
            {"scope": "本番", "target": "個別"},
        ),
    ]


async def test_split_breakdown_reports_metrics_per_split() -> None:
    cases = [
        EvaluationCase(
            id=identifier, query="合成の質問", split=split, expected_response_decision=decision
        )
        for identifier, split, decision in [
            ("d1", "dev", "answer"),
            ("d2", "dev", "hold"),
            ("h1", "holdout", "answer"),
            ("u1", None, "hold"),
        ]
    ]
    metrics = await EvaluationRunner(pipeline=StubPipeline()).run(cases=cases, top_k=5)
    assert set(metrics.split_breakdown) == {"dev", "holdout", "unassigned"}
    dev = metrics.split_breakdown["dev"]
    assert (dev.case_count, dev.error_count) == (2, 0)
    assert dev.metrics["response_decision_accuracy"] == 0.5
    assert dev.metric_case_counts["response_decision_accuracy"] == 2
    assert dev.failure_reason_counts == {"unexpected_handling": 1}
    assert metrics.split_breakdown["holdout"].metrics["response_decision_accuracy"] == 1
    assert metrics.split_breakdown["unassigned"].metrics["response_decision_accuracy"] == 0
    assert [result.split for result in metrics.case_results] == ["dev", "dev", "holdout", None]
    plain = await EvaluationRunner(pipeline=StubPipeline()).run(
        cases=[EvaluationCase(id="p", query="合成の質問")], top_k=5
    )
    assert plain.split_breakdown == {}


def test_trend_includes_split_breakdown_and_evidence_recall() -> None:
    from app.rag.evaluation_cli import _metrics_trend
    from app.schemas.evaluation import EvaluationSplitSummary

    trend = _metrics_trend(
        EvaluationMetrics(
            case_count=1,
            required_evidence_recall=0.5,
            split_breakdown={"holdout": EvaluationSplitSummary(case_count=1, metrics={"mrr": 1.0})},
        )
    )
    assert trend["required_evidence_recall"] == 0.5
    assert trend["split_breakdown"]["holdout"]["metrics"] == {"mrr": 1.0}


async def test_evaluation_runner_shares_one_standard_scope_record_across_cases() -> None:
    """1 回の評価のケースは、標準回答の比較範囲の記録を共有する（#3570）。"""
    from rag_engine.evaluation.answer_eval import StandardScopeCache, standard_scope_cache

    seen: list[StandardScopeCache] = []

    class RecordingPipeline(StubPipeline):
        async def run(
            self,
            request: SearchRequest,
            trace_id: str | None = None,
            progress_callback: SearchStageProgressCallback | None = None,
        ) -> SearchResponse:
            # 共有の中なら、その記録が返る。
            with standard_scope_cache() as cache:
                seen.append(cache)
            return await super().run(request, trace_id, progress_callback)

    runner = EvaluationRunner(pipeline=RecordingPipeline())
    cases = [
        EvaluationCase(id="case-1", query="承認条件"),
        EvaluationCase(id="case-2", query="申請の期限"),
    ]
    await runner.run(cases=cases, top_k=5)
    await runner.run(cases=cases[:1], top_k=5)

    assert len(seen) == 3
    assert seen[0] is seen[1]
    # 別の評価（job）は記録を共有しない。
    assert seen[2] is not seen[0]


def test_summarize_answer_judgement_keeps_judge_diagnostics() -> None:
    """判定の診断（比較範囲の使い回し・外した項目の数・再試行の回数）は、採点できなかったケースにも残す（#3570）。"""
    diagnostics: dict[str, object] = {
        "standard_scope_reused": True,
        "standard_scope_retries": 0,
        "standard_scope_dropped": 1,
        "judge_retries": 1,
        # 判定に使ったモデル（#3574）。
        "judge_model_id": "model-judge",
        "retry_reasons": ["coverage:InvalidStructuredOutput"],
        # 段落ごとの主張の判定の呼び出しの数・失敗・出典の無い段落の数（#3576）。
        "judge_calls": 6,
        "claim_calls": 4,
        "claim_failures": 1,
        "uncited_claims": 2,
    }
    completed = summarize_answer_judgement(
        {**_completed_judgement(), "judge_diagnostics": diagnostics}
    )
    assert completed.judge_diagnostics is not None
    assert completed.judge_diagnostics.model_dump() == diagnostics

    failed = summarize_answer_judgement(
        {
            "status": "error",
            "message": "",
            "judge_diagnostics": {**diagnostics, "standard_scope_retries": 1},
        }
    )
    assert failed.status == "error" and failed.judge_diagnostics is not None
    assert failed.judge_diagnostics.standard_scope_retries == 1

    assert summarize_answer_judgement({"status": "timeout"}).judge_diagnostics is None
    broken = summarize_answer_judgement({"status": "error", "judge_diagnostics": "壊れた値"})
    assert broken.judge_diagnostics is None
