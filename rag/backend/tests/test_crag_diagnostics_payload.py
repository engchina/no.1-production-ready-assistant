"""CRAG の生成可否・観点・引用の診断を回答 API まで保持する。"""

from rag_engine.generation.answer_models import AnswerQuestionResult

from app.rag.answer_engine import _outcome_from_result, _SearchState


def test_crag_diagnostics_preserve_separate_coverage_and_evidence() -> None:
    attempt = {
        "sufficient": True,
        "generation_usable": True,
        "required_aspect_coverage": "partial",
        "missing_aspects": ["procedure"],
        "unknown_aspects": ["applicability"],
        "aspect_checks": [
            {
                "aspect": "business_object",
                "status": "supported",
                "evidence": [{"source_id": "e1", "quote": "対象 A の説明"}],
            }
        ],
    }
    result = AnswerQuestionResult(
        answer="回答",
        question_display="質問",
        original_question="質問",
        selected_strategy="simple",
        effective_strategy="simple",
        crag_attempts=(attempt,),
    )
    diagnostics = _outcome_from_result(result, _SearchState()).diagnostics
    assert diagnostics["crag_attempt_count"] == 1
    assert diagnostics["crag_attempts"] == [attempt]


def test_final_crag_gate_view_survives_outcome_conversion() -> None:
    assessment = {
        "generation_usable": False,
        "context_changed": True,
        "input_budget_chars": 42,
        "generation_context_spans": [
            {
                "evidence_id": "E1",
                "source_id": "canonical",
                "source_aliases": ["alias"],
                "text_chars": 10,
            }
        ],
        "answerable_parts": [
            {
                "evidence": [
                    {
                        "source_id": "alias",
                        "source_candidate_ids": ["E1"],
                        "evidence_ids": [],
                        "binding_reason": "quote_not_in_generation_source",
                    }
                ]
            }
        ],
    }
    gate = {
        "generation_usable": False,
        "generation_calls": 0,
        "reason": "no_verified_answerable_part",
    }
    result = AnswerQuestionResult(
        answer="回答",
        question_display="質問",
        original_question="質問",
        selected_strategy="simple",
        effective_strategy="simple",
        generation_trace={"crag_assessment": assessment, "crag_generation_gate": gate},
    )
    diagnostics = _outcome_from_result(result, _SearchState()).diagnostics
    assert diagnostics["crag_assessment"] == assessment
    assert diagnostics["crag_generation_gate"] == gate
