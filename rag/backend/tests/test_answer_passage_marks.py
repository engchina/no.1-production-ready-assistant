"""回答の記録・評価の段落の印（#2738。platform/contracts/answer-passages/passage-marks.json）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.rag.evaluation import score_case_answers, summarize_case_results
from app.rag.stored_answer import PASSAGE_MARK_RECORD_KEYS, passage_mark_record
from app.schemas.evaluation import EvaluationCase
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchResponse

CONTRACT: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "platform/contracts/answer-passages/passage-marks.json"
    ).read_text("utf-8")
)


def _example(example_id: str) -> dict[str, Any]:
    return next(item for item in CONTRACT["examples"] if item["id"] == example_id)


def test_record_keeps_the_contract_fields_only() -> None:
    """記録は契約の 3 つの一覧と項目だけを持つ。契約の例はそのまま通る。"""
    for example in CONTRACT["examples"]:
        envelope = {key: example[key] for key in PASSAGE_MARK_RECORD_KEYS}
        assert passage_mark_record(envelope) == envelope
    noisy = {
        "passage_marks": [
            {"text": "・説明", "state": "unverified", "reasons": [{"code": "unsupported", "x": 1}]},
            {"text": " ", "state": "warning", "reasons": []},
            "壊れた値",
        ],
        "removed_passages": None,
    }
    assert passage_mark_record(noisy) == {
        "passage_marks": [
            {
                "text": "・説明",
                "state": "unverified",
                "reasons": [{"code": "unsupported", "label": "", "detail": ""}],
            }
        ],
        "removed_passages": [],
        "answer_warnings": [],
    }
    # 保存済みの過去の回答（印の記録が無い）は空の一覧として読む。
    assert passage_mark_record(None) == {
        "passage_marks": [],
        "removed_passages": [],
        "answer_warnings": [],
    }


def _response(answer: str, details: dict[str, Any]) -> SearchResponse:
    return SearchResponse(
        answer=answer,
        citations=[RetrievedChunk(chunk_id="doc-1:0", document_id="doc-1", text=answer, score=1.0)],
        trace_id="trace-1",
        elapsed_ms=1.0,
        diagnostics=SearchDiagnostics(answer=details),
    )


def test_evaluation_records_the_marks_per_answer_and_averages_them() -> None:
    """段落の印は監査の診断に残し、独立した評価指標として集計しない。"""
    example = _example("keep-unverified-remove-contradicted")
    case = EvaluationCase(id="case-1", query="合成の質問", relevant_document_ids=["doc-1"])
    details = {"outcome": "conditional", **{key: example[key] for key in PASSAGE_MARK_RECORD_KEYS}}
    marked = score_case_answers(case, [(_response(example["published"], details), {})])
    old = score_case_answers(
        case.model_copy(update={"id": "case-2"}), [(_response("回答", {"outcome": "answered"}), {})]
    )
    assert marked.diagnostics is not None
    recorded = passage_mark_record(marked.diagnostics.answer)
    assert len(recorded["removed_passages"]) == 1
    assert len(recorded["passage_marks"]) == 1
    assert old.diagnostics is not None
    assert passage_mark_record(old.diagnostics.answer)["removed_passages"] == []
    metrics = summarize_case_results([marked, old])
    assert metrics.mrr == 1
    assert "removed_passages_per_answer" not in metrics.model_dump()
    assert "warning_passages_per_answer" not in metrics.model_dump()
    assert "removed_passages_per_answer" not in metrics.metric_case_counts


def test_faithfulness_counts_the_body_of_an_all_unverified_answer() -> None:
    """未確認の印は監査に保持し、忠実性は主張の支持の判定から求める。"""
    from app.schemas.evaluation import EvaluationAnswerJudgement

    case = EvaluationCase(
        id="case-1",
        query="合成の質問",
        relevant_document_ids=["doc-1"],
        expected_response_decision="hold",
    )
    details = {
        "outcome": "insufficient_evidence",
        "passage_marks": [
            {
                "text": "合成の主張",
                "state": "unverified",
                "reasons": [{"code": "unsupported", "label": "未確認", "detail": ""}],
            }
        ],
        "removed_passages": [],
        "answer_warnings": [],
    }
    result = score_case_answers(
        case,
        [(_response("合成の主張", details), {})],
        EvaluationAnswerJudgement(status="completed", claim_support_rate=0),
    )
    assert result.response_decision_accuracy == 1
    assert result.claim_support_rate == 0
    assert result.diagnostics is not None
    assert len(passage_mark_record(result.diagnostics.answer)["passage_marks"]) == 1
