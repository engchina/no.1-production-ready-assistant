"""直接根拠の不足による早期終了でも、解決済みガイドの案内を失わない。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_crag_partial_generation import OVERVIEW, QUESTION, grade, run_answer

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.generation import crag_generation as c
from rag_engine.generation import grounded
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext

NOTICE = "条件Aの場合は担当窓口に処理を依頼してください。"


def view():
    return {
        "generation_usable": False,
        "generation_decision": c.GATE_NO_ANSWERABLE_PART,
        "aspect_checks": [{"aspect": "procedure", "status": "unknown"}],
    }


def test_confirmed_notice_survives_zero_generation_refusal_as_guide_information():
    knowledge = SimpleNamespace(guide_title="合成ガイド", guide_notices=(NOTICE,))
    result = c.refusal_response("合成の操作方法は？", view(), runtime_knowledge=knowledge)
    assert NOTICE in result.answer_text
    assert grounded.answer_labels().guide_notes_section in result.answer_text
    assert grounded.answer_labels().guide_notes_note("合成ガイド") in result.answer_text
    assert not result.cited_evidence
    assert result.generation_trace["crag_generation_gate"]["generation_calls"] == 0
    assert result.generation_trace["crag_generation_gate"]["generation_usable"] is False


def test_no_notice_is_not_invented():
    current = c.refusal_response("合成の操作方法は？", view())
    result = c.refusal_response("合成の操作方法は？", view(), runtime_knowledge=None)
    assert result.answer_text == current.answer_text
    assert grounded.answer_labels().guide_notes_section not in result.answer_text


def test_unresolved_branch_does_not_supply_a_notice():
    unknown = SimpleNamespace(
        guide_title="合成ガイド",
        guide_notices=(),
        unknown_conditions=("条件A",),
        unresolved_branch_notes=("条件Aなら別窓口へ依頼してください。",),
    )
    result = c.refusal_response("合成の操作方法は？", view(), runtime_knowledge=unknown)
    assert "別窓口" not in result.answer_text
    assert grounded.answer_labels().guide_notes_section not in result.answer_text


@pytest.mark.parametrize("caller", ["synthesize", "dispatch"])
def test_both_early_return_callers_forward_resolved_guide_notices(caller):
    knowledge = RuntimeKnowledgeContext(
        original_question=QUESTION, expanded_question=QUESTION, path=Path("unused.json"), guide_title="合成ガイド", guide_notices=(NOTICE,)
    )
    if caller == "synthesize":
        with patch.object(a, "parse_text_response", side_effect=AssertionError("生成は不要")):
            result = a.synthesize_grounded_answer(
                QUESTION,
                a.AnswerContext(records=[], text=""),
                get_settings(environ={}, dotenv_path=None),
                runtime_knowledge=knowledge,
                crag_assessment=view(),
                image_prompt_mode="text_only",
            ).response
    else:
        with patch.object(a, "build_runtime_knowledge_context", return_value=knowledge):
            result, calls = run_answer(OVERVIEW, grade(OVERVIEW, partial=False), lambda context: pytest.fail("生成は不要"))
        assert all(schema not in {"GroundedDraft", "GroundedDraftWire"} for schema, _ in calls)
    assert NOTICE in result.answer_text
    assert result.generation_trace["crag_generation_gate"]["generation_calls"] == 0
    assert result.generation_trace["crag_assessment"]["input_stage"] == "prepared_for_generation"


@pytest.mark.parametrize(
    "source,quote,reason,usable",
    [
        ("missing", "直接の原文", "source_not_in_generation_context", False),
        ("alias", "欠けた原文", "quote_not_in_generation_source", False),
        ("alias", "直接の原文", "matched", True),
    ],
)
def test_generation_binding_diagnostics_distinguish_missing_source_and_quote(source, quote, reason, usable):
    evidence = {"source_id": source, "quote": quote}
    assessment = {"answerable_parts": [{"status": "supported", "evidence": [evidence]}], "aspect_checks": []}
    spans = [{"evidence_id": "E1", "label": "E1", "source_id": "canonical", "source_aliases": ["alias"], "text": "直接の原文"}]
    result = c.generation_view(assessment, spans, a.AnswerContext(records=[], text=""), budget_chars=42)
    assert result["generation_usable"] is usable
    proof = result["answerable_parts"][0]["evidence"][0]
    assert proof["binding_reason"] == reason
    assert proof["source_candidate_ids"] == ([] if source == "missing" else ["E1"])
    assert result["input_budget_chars"] == 42
    assert result["input_stage"] == "prepared_for_generation"
    assert result["generation_context_spans"] == [
        {"evidence_id": "E1", "source_id": "canonical", "source_aliases": ["alias"], "text_chars": len("直接の原文")}
    ]
    prompt = c.prompt_context(result)
    assert "binding_reason" not in prompt and "generation_context_spans" not in prompt and "input_budget_chars" not in prompt
