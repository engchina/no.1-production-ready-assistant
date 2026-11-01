"""未校正の意味監査を後処理で自動裁決へ戻さない。外部サービスは使わない。"""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.rag.answer_engine import AnswerEngine, AnswerOutcome, _outcome_from_result, _SearchState
from app.rag.answer_validation import AnswerValidation, EvidenceRef, audited_answer_lines
from app.rag.run_history import answer_validation_execution, answer_verification
from app.schemas.search import SearchRequest
from tests.test_answer_use import _EngineResult

POLICY = {"mode": "diagnostic_only", "calibration": "not_calibrated"}


def outcome() -> AnswerOutcome:
    return AnswerOutcome(
        answer="合成の説明を保持します。",
        citations=[],
        diagnostics={
            "semantic_audit_policy": dict(POLICY),
            "outcome": "answered",
            "envelope": {"outcome": "answered"},
            "confidence": "high",
            "insufficient_reason": "既存の案内",
        },
    )


def engine(mode: str = "standard") -> AnswerEngine:
    return AnswerEngine(
        settings=Settings(rag_answer_audit_mode=cast(Any, mode)),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
    )


@pytest.mark.parametrize("mode", ["standard", "detailed"])
@pytest.mark.parametrize("valid", [False, True])
async def test_raw_semantic_verdict_cannot_modify_the_final_answer(
    monkeypatch: pytest.MonkeyPatch, mode: str, valid: bool
) -> None:
    result = outcome()
    checks = [
        {
            "status": "supported" if valid else "contradicted",
            "answer_quote": result.answer,
            "reason": "合成の意味判定",
            "source_id": "chunk-a",
        }
    ]
    counts = {"supported" if valid else "contradicted": 1}
    validate = AsyncMock(
        return_value=AnswerValidation(
            status="completed",
            valid=valid,
            claim_checks=checks,
            counts=counts,
            llm_usage={"calls": 1},
        )
    )
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validate)
    await engine(mode)._validate_outcome(SearchRequest(query="合成の質問"), result)
    validate.assert_awaited_once()
    assert result.answer == "合成の説明を保持します。"
    assert result.diagnostics["outcome"] == result.diagnostics["envelope"]["outcome"] == "answered"
    assert result.diagnostics["confidence"] == "high"
    assert result.diagnostics["insufficient_reason"] == "既存の案内"
    assert result.diagnostics["needs_human_review"] is True
    diagnostic = result.diagnostics["answer_validation"]
    assert diagnostic["status"] == "completed" and diagnostic["raw_valid"] is valid
    assert diagnostic["valid"] is None and diagnostic["semantic_status"] == "unknown"
    assert diagnostic["automatic_actions"] is False
    assert diagnostic["claim_checks"] == checks and diagnostic["counts"] == counts
    assert result.diagnostics["execution_steps"][0]["llm_calls"] == 1
    assert answer_validation_execution(result.diagnostics) == "unknown"
    assert answer_verification(result.diagnostics) == "unverified"


@pytest.mark.parametrize("kind", ["missing_evidence", "stale_evidence"])
async def test_deterministically_unavailable_sources_still_require_review(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    result = outcome()
    values: dict[str, Any] = {kind: [EvidenceRef("doc-a", "chunk-a")]}
    validation = AnswerValidation(status="completed", valid=False, **values)
    monkeypatch.setattr(
        "app.rag.answer_validation.validate_answer", AsyncMock(return_value=validation)
    )
    await engine()._validate_outcome(SearchRequest(query="合成の質問"), result)
    assert result.answer == "合成の説明を保持します。"
    assert result.diagnostics["confidence"] == "low"
    assert result.diagnostics["outcome"] == "conditional"
    assert result.diagnostics["envelope"]["outcome"] == "conditional"
    assert result.diagnostics["answer_validation"][kind + "_count"] == 1
    assert result.diagnostics["answer_validation"]["semantic_status"] == "unknown"


async def test_none_keeps_zero_final_validator_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    result = outcome()
    validate = AsyncMock()
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validate)
    await engine("none")._validate_outcome(SearchRequest(query="合成の質問"), result)
    validate.assert_not_awaited()
    assert result.diagnostics["answer_validation"]["reason"] == "audit_disabled"
    assert result.diagnostics["answer_validation"]["valid"] is None
    assert (
        result.diagnostics["outcome"] == "answered" and result.diagnostics["confidence"] == "high"
    )
    assert answer_validation_execution(result.diagnostics) == "disabled"


@pytest.mark.parametrize(
    "exc,status", [(TimeoutError(), "timed_out"), (RuntimeError("synthetic"), "error")]
)
async def test_execution_failure_is_distinct_from_negative_semantics(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, status: str
) -> None:
    result = outcome()
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", AsyncMock(side_effect=exc))
    await engine()._validate_outcome(SearchRequest(query="合成の質問"), result)
    assert result.diagnostics["answer_validation"]["status"] == status
    assert result.diagnostics["answer_validation"]["valid"] is None
    assert result.diagnostics["answer_validation"]["raw_valid"] is False
    assert (
        result.diagnostics["outcome"] == "answered" and result.diagnostics["confidence"] == "high"
    )
    assert answer_validation_execution(result.diagnostics) == status


def test_engine_policy_reaches_backend_diagnostics() -> None:
    result = _EngineResult([])
    result.generation_trace = {"semantic_audit_policy": dict(POLICY)}
    projected = _outcome_from_result(result, _SearchState())
    assert projected.diagnostics["semantic_audit_policy"] == POLICY
    assert (
        projected.diagnostics["semantic_audit_policy"]
        is not result.generation_trace["semantic_audit_policy"]
    )


@pytest.mark.parametrize("diagnostic_only", [False, True])
async def test_saved_raw_positive_cannot_restore_supported_reuse(
    monkeypatch: pytest.MonkeyPatch, diagnostic_only: bool
) -> None:
    from app.rag.answer_engine import ANSWER_CITATION_LINES_KEY

    diagnostics: dict[str, Any] = {
        "answer_validation": {
            "status": "completed",
            "valid": True,
            "reuse_context": "same",
            "counts": {"supported": 1},
            "claim_checks": [
                {"status": "supported", "answer_quote": "合成の回答", "source_id": "chunk-a"}
            ],
        },
        "envelope": {"items": []},
    }
    if diagnostic_only:
        diagnostics["semantic_audit_policy"] = dict(POLICY)

    class Oracle:
        async def get_answer_record(self, trace_id: str) -> dict[str, Any]:
            return {
                "answer": "合成の回答",
                "diagnostics_json": diagnostics,
                "citations_json": [
                    {
                        "chunk_id": "chunk-a",
                        "text": "合成の根拠",
                        "metadata": {ANSWER_CITATION_LINES_KEY: [1]},
                    }
                ],
            }

    monkeypatch.setattr(
        "rag_engine.evaluation.answer_validation.audited_answer_items",
        lambda *args: {"合成の回答": ["chunk-a"]},
    )
    reused = await audited_answer_lines(
        cast(Any, Oracle()), ["trace-a"], {"chunk-a": {"text": "合成の根拠"}}, reuse_context="same"
    )
    assert reused == ({} if diagnostic_only else {"合成の回答": ["chunk-a"]})
    assert answer_verification(diagnostics) == ("unverified" if diagnostic_only else "verified")
    assert answer_validation_execution(diagnostics) == ("unknown" if diagnostic_only else "passed")


@pytest.mark.parametrize("alignment", ["aligned", "off_target", "partial"])
def test_raw_goal_alignment_is_not_adopted_under_diagnostic_policy(alignment: str) -> None:
    from app.rag.answer_engine import adopted_goal_alignment

    trace: dict[str, Any] = {"rounds": [{"accepted": True, "audit": {"goal_alignment": alignment}}]}
    assert adopted_goal_alignment(trace) == alignment
    trace["semantic_audit_policy"] = dict(POLICY)
    assert adopted_goal_alignment(trace) is None
    assert trace["rounds"][0]["audit"]["goal_alignment"] == alignment
