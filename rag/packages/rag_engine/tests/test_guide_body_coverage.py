"""引用の候補対応と、実際の本文の意味的な全充足を混同しない。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.answer_envelope import guide_step_requests
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedItem

FIRST = "パネルAを開いて設定値を入力します。"
SECOND = "プレビューを選び内容を確認して反映ボタンを押します。"
SPAN = {"source_id": "sample", "evidence_id": "e1", "label": "E1", "text": FIRST + SECOND}
STEPS = (("s1", FIRST, ("sample",)), ("s2", SECOND, ("sample",)))
CONTRACTS = [
    {"step_id": "s1", "requirements": [{"id": "r1", "text": FIRST, "kind": "completion"}]},
    {"step_id": "s2", "depends_on": ["s1"], "requirements": [{"id": "r2", "text": SECOND, "kind": "completion"}]},
]


def item(text, quote=FIRST + SECOND):
    return grounded.CheckedItem(
        GroundedItem(kind="operation", text=text, applies="matched", citations=[GroundedCitation(evidence_id="e1", quote=quote)]), span=SPAN
    )


def observation(requirement, indexes, text, state="explained"):
    return {"requirement_id": requirement, "item_indexes": indexes, "answer_quote": text, "answer_state": state, "reason": "合成の同次観察"}


def audit(*observations):
    return GroundedAudit(reviews=[], guide_requirement_reviews=list(observations), summary_supported=True, goal_alignment="aligned")


def coverage(items):
    return grounded.guide_step_coverage(
        STEPS, items, [SPAN], step_terms={"s1": ("パネルA", "設定値", "入力"), "s2": ("プレビュー", "内容", "確認", "反映ボタン")}
    )


@pytest.mark.parametrize("text", [FIRST, FIRST + SECOND, "操作を完了します。"])
def test_source_or_quote_anchors_do_not_prove_unreviewed_body_completeness(text):
    result = coverage([item(text)])
    assert [row["status"] for row in result] == ["unknown", "unknown"]
    assert all(row["evidence_flow"]["context_evidence_ids"] == ["e1"] for row in result)
    assert all(row["validation"]["body_coverage"]["execution"] == "not_run" for row in result)
    assert [row["status"] for row in guide_step_requests(result)] == ["unknown", "unknown"]


def test_body_proofs_can_share_one_item_and_preserve_correct_operation_order():
    items = [item(FIRST + SECOND)]
    reviews = audit(observation("r1", [0], FIRST), observation("r2", [0], SECOND))
    result = grounded.apply_guide_contracts(coverage(items), items, CONTRACTS, reviews)
    assert [row["status"] for row in result] == ["explained", "explained"]
    assert result[0]["requirements"][0]["body_spans"][0]["start"] == 0
    assert result[1]["requirements"][0]["body_spans"][0]["start"] == len(FIRST)
    assert items[0].item.text == FIRST + SECOND


def test_source_only_requirement_quote_does_not_complete_omitted_body():
    items = [item(FIRST)]
    result = grounded.apply_guide_contracts(
        coverage(items), items, CONTRACTS, audit(observation("r1", [0], FIRST), observation("r2", [0], SECOND))
    )
    assert result[0]["status"] == "explained"
    assert result[1]["status"] == "unknown"
    assert result[1]["requirements"][0]["body_spans"] == []
    assert items[0].item.text == FIRST


def test_same_item_reversed_operations_are_not_complete():
    items = [item(SECOND + FIRST)]
    result = grounded.apply_guide_contracts(
        coverage(items), items, CONTRACTS, audit(observation("r1", [0], FIRST), observation("r2", [0], SECOND))
    )
    assert result[0]["status"] == "explained"
    assert result[1]["status"] == "partial" and result[1]["order_errors"] == ["s1"]
    assert items[0].item.text == SECOND + FIRST


def test_ambiguous_body_positions_do_not_claim_dependency_order_verified():
    items = [item(FIRST + SECOND + FIRST)]
    result = grounded.apply_guide_contracts(
        coverage(items), items, CONTRACTS, audit(observation("r1", [0], FIRST), observation("r2", [0], SECOND))
    )
    assert result[1]["status"] == "unknown"
    assert result[1]["validation"]["body_coverage"]["reason"] == "operation_order_unverified"


def test_multiple_items_jointly_prove_one_step_without_exclusive_assignment():
    left, right = "パネルAを開いて", "設定値を入力します。"
    items = [item(left, left), item(right, right), item(SECOND, SECOND)]
    result = grounded.apply_guide_contracts(
        coverage(items), items, CONTRACTS, audit(observation("r1", [0, 1], left + "\n" + right), observation("r2", [2], SECOND))
    )
    assert [row["status"] for row in result] == ["explained", "explained"]
    assert {row["item_index"] for row in result[0]["requirements"][0]["body_spans"]} == {0, 1}


@pytest.mark.parametrize("execution", ["not_run", "error", "timed_out"])
def test_unperformed_or_failed_semantic_review_preserves_body_and_known_sources(execution):
    items = [item(FIRST + SECOND)]
    result = grounded.apply_guide_contracts(coverage(items), items, CONTRACTS, None, review_execution=execution)
    assert [row["status"] for row in result] == ["unknown", "unknown"]
    assert all(row["validation"]["body_coverage"]["execution"] == execution for row in result)
    assert items[0].published and items[0].item.text == FIRST + SECOND
    assert all(row["status"] == "unknown" for row in guide_step_requests(result))


@pytest.mark.parametrize("mode", ["none", "detailed"])
def test_final_envelope_keeps_body_and_distinguishes_execution_from_guide_completion(mode):
    from dataclasses import replace
    from pathlib import Path

    from test_grounded_answer import FakeModel, context, draft, record
    from test_grounded_answer import audit as draft_audit
    from test_grounded_answer import item as draft_item

    from rag_engine.config import get_settings
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation.answering import _grounded_spans, synthesize_grounded_answer
    from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext

    question = "対象Aの変更手順を教えてください。"
    ctx = context(replace(record(0, FIRST + SECOND, "合成の操作", source="sample.pdf"), chunk_uid="sample"))
    knowledge = RuntimeKnowledgeContext(
        original_question=question,
        expanded_question=question,
        path=Path("synthetic.json"),
        guide_steps=STEPS,
        guide_step_contracts=tuple(CONTRACTS),
    )
    settings = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode=mode)
    evidence_id = _grounded_spans(question, ctx, (), knowledge)[0]["evidence_id"]
    assessment = draft_audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])
    assessment["guide_requirement_reviews"] = [observation("r1", [0], FIRST), observation("r2", [1], SECOND)]
    model = FakeModel(
        [
            draft(
                draft_item(evidence_id, FIRST, FIRST, request_id="Q1"), draft_item(evidence_id, SECOND, SECOND, request_id="Q1"), summary=""
            )
        ],
        [assessment],
    )

    def unused(*args, **kwargs):
        pytest.fail("追加の外部呼び出しは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        response = synthesize_grounded_answer(question, ctx, settings, image_prompt_mode="text_only", runtime_knowledge=knowledge).response
    assert FIRST in response.answer_text and SECOND in response.answer_text
    guide_requests = {row["id"]: row["status"] for row in response.envelope["requests"] if row["id"].startswith("guide:")}
    if mode == "none":
        assert [schema for schema, _prompt in model.prompts] == ["GroundedDraftWire"]
        assert guide_requests == {"guide:s1": "unknown", "guide:s2": "unknown"}
        assert response.envelope["outcome"] == "conditional"
        assert grounded.answer_labels().marked_section in response.answer_text
        assert grounded.answer_labels().gaps_section not in response.answer_text
        assert "unverified_requests" in response.envelope["handoff_reasons"]
        assert all(row["validation"]["body_coverage"]["execution"] == "not_run" for row in response.generation_trace["guide_step_coverage"])
    else:
        assert [schema for schema, _prompt in model.prompts] == ["GroundedDraftWire", "GroundedAudit"]
        assert guide_requests == {"guide:s1": "unknown", "guide:s2": "unknown"}
        assert response.envelope["outcome"] == "conditional"
        assert all(row["status"] == "unknown" for row in response.generation_trace["guide_step_coverage"])
        assert response.generation_trace["semantic_audit_observation"]["adopted"] is False


def test_predecessor_must_finish_before_the_next_step_even_across_multiple_items():
    left, right = "パネルAを開いて", "設定値を入力します。"
    items = [item(left, left), item(SECOND, SECOND), item(right, right)]
    result = grounded.apply_guide_contracts(
        coverage(items), items, CONTRACTS, audit(observation("r1", [0, 2], left + "\n" + right), observation("r2", [1], SECOND))
    )
    assert result[0]["status"] == "explained"
    assert result[1]["status"] == "partial" and result[1]["order_errors"] == ["s1"]
    assert [entry.item.text for entry in items] == [left, SECOND, right]


@pytest.mark.parametrize("shared_proof", [FIRST + SECOND, "反映は入力の後に行います。"])
def test_broad_or_relative_order_description_is_unknown_instead_of_text_position_contradiction(shared_proof):
    items = [item(shared_proof)]
    result = grounded.apply_guide_contracts(
        coverage(items), items, CONTRACTS, audit(observation("r1", [0], shared_proof), observation("r2", [0], shared_proof))
    )
    assert result[1]["status"] == "unknown"
    assert result[1]["order_unverified"] == ["s1"]
    assert "order_errors" not in result[1]
    assert items[0].item.text == shared_proof
