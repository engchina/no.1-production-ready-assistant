"""同次監査の動的な要求ID対応を検証し、本文支持と材料不足から分離する。"""

import json

import pytest
from pydantic import ValidationError
from test_grounded_answer import FakeModel, audit, context, draft, ids, item, record

from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans
from rag_engine.generation.request_recovery import request_recovery_plan
from rag_engine.models.llm import GroundedAudit

QUESTION = "対象Aの画面を開く手順は？"
TEXT = "対象Aの画面を開きます。"


def run_review(requests):
    ctx = context(record(0, TEXT, "対象A"))
    reference = ids(ctx, QUESTION)[TEXT]
    seen = []

    def review(prompt):
        seen.append(json.loads(prompt)["task_contract"]["request_units"])
        return audit((0, "supported", "matched", ""), requests=requests)

    model = FakeModel([draft(item(reference, TEXT, TEXT, request_id="Q1"), summary="")], [review])
    current = grounded.run_round(
        QUESTION,
        _grounded_spans(QUESTION, ctx, (), None),
        None,
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=None,
    )
    assert len(model.prompts) == 2 and current.audit_calls == 1
    assert {row["id"] for row in seen[0]} == {"Q1"}
    entry = next(entry for entry in current.checked if entry.item.text == TEXT)
    assert entry.published and entry.confirmed and not entry.removed
    return current


@pytest.mark.parametrize(
    "requests,missing,invalid,duplicates,validation",
    [
        ([], ["Q1"], [], [], "partial"),
        ([("Q999", "addressed")], ["Q1"], ["Q999"], [], "error"),
        ([("Q1", "addressed"), ("Q999", "missing")], [], ["Q999"], [], "error"),
        ([("Q1", "addressed"), ("Q1", "missing")], [], [], ["Q1"], "error"),
        ([("Q999", "addressed"), ("Q999", "missing")], ["Q1"], ["Q999"], [], "error"),
    ],
)
def test_contract_failures_do_not_become_missing_material_or_remove_valid_body(requests, missing, invalid, duplicates, validation):
    current = run_review(requests)
    dimension = current.trace()["validation_dimensions"]["answer_completeness"]
    assert current.review_execution == "partial"
    assert dimension["execution"] == "partial"
    assert dimension["review_execution"] == "partial"
    assert dimension["contract_validation"] == {
        "status": validation,
        "expected_request_ids": ["Q1"],
        "returned_request_ids": [row[0] for row in requests],
        "missing_request_ids": missing,
        "invalid_request_ids": invalid,
        "duplicate_request_ids": duplicates,
    }
    assert all(row["request_id"] == "Q1" for row in dimension["requests"])
    unknown = [row for row in current.unanswered if not row.get("reviewed")]
    assert [row["request_id"] for row in unknown] == missing + duplicates
    assert all(row["status"] == "unknown" and row["evidence_status"] == "unknown" and row["evidence_ids"] == [] for row in unknown)
    assert all(row["action"] == "retain" for row in request_recovery_plan(current.unanswered, (), set()))


def test_valid_review_remains_completed():
    current = run_review([("Q1", "addressed")])
    assert current.review_execution == "completed"
    assert current.addressed == set() and current.unanswered[0]["status"] == "unknown"
    assert current.timing["semantic_audit_observation"]["audit"]["request_reviews"][0]["status"] == "addressed"
    assert current.trace()["validation_dimensions"]["answer_completeness"]["execution"] == "partial"


def test_valid_partial_review_keeps_its_material_unknown_without_inventing_failure():
    current = run_review([("Q1", "partial")])
    assert current.missing_request_ids == current.invalid_request_ids == current.duplicate_request_ids == ()
    assert current.unanswered[0]["status"] == "unknown"
    assert current.timing["semantic_audit_observation"]["audit"]["request_reviews"][0]["status"] == "partial"
    assert current.unanswered[0]["evidence_status"] == "unknown"


def test_invalid_status_enum_is_a_schema_error_not_a_missing_result():
    payload = audit((0, "supported", "matched", ""), requests=[("Q1", "not-a-status")])
    with pytest.raises(ValidationError) as error:
        GroundedAudit.model_validate(payload)
    assert any(row["type"] == "literal_error" and row["loc"] == ("request_reviews", 0, "status") for row in error.value.errors())


def test_contract_uses_exact_sent_request_rows_and_keeps_other_valid_partial_reviews():
    source = GroundedAudit.model_validate(
        audit((0, "supported", "matched", ""), requests=[("sent-B", "partial"), ("sent-A", "addressed"), ("unrequested", "missing")])
    )
    result, duplicates, missing, invalid = grounded._validate_request_review_contract(
        source, [{"id": "sent-A"}, {"id": "sent-B"}, {"id": "sent-C"}]
    )
    assert duplicates == () and missing == ("sent-C",) and invalid == ("unrequested",)
    assert [(row.request_id, row.status) for row in result.request_reviews] == [("sent-B", "partial"), ("sent-A", "addressed")]
    assert result.reviews == source.reviews
    assert len(source.request_reviews) == 3


def test_invalid_enum_preserves_unverified_body_and_reports_execution_error_not_missing():
    ctx = context(record(0, TEXT, "対象A"))
    reference = ids(ctx, QUESTION)[TEXT]
    model = FakeModel(
        [draft(item(reference, TEXT, TEXT, request_id="Q1"), summary="")],
        [audit((0, "supported", "matched", ""), requests=[("Q1", "invalid-status")])],
    )
    current = grounded.run_round(
        QUESTION,
        _grounded_spans(QUESTION, ctx, (), None),
        None,
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=None,
    )
    assert current.review_execution == "error" and current.audit is None
    assert current.timing["audit_error_type"] == "ValidationError"
    assert current.audit_calls == 1 and len(model.prompts) == 2
    [unverified] = current.unanswered
    assert unverified["status"] == "unknown" and unverified["reviewed"] is False
    entry = next(entry for entry in current.checked if entry.item.text == TEXT)
    assert entry.published and entry.confirmed and not entry.removed
    assert current.verified == 0  # confirmed は原文・基本検査のみ。


def test_invalid_generated_gap_is_retained_without_search_and_valid_body_survives():
    from test_grounded_answer import run

    ctx = context(record(0, TEXT, "対象A"))
    reference = ids(ctx, QUESTION)[TEXT]
    model = FakeModel(
        [draft(item(reference, TEXT, TEXT, request_id="Q1"), {"kind": "gap", "request_id": "Q999", "text": "未確認です。"}, summary="")],
        [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed"), ("Q999", "missing")])],
    )
    response = run(model, ctx, QUESTION).response
    decisions = response.generation_trace["rounds"][0]["request_recovery"]["decisions"]
    [invalid] = [row for row in decisions if row["request_id"] == "Q999"]
    assert invalid["cause"] == "invalid_request_id" and invalid["action"] == "retain"
    assert invalid["status"] == "unknown" and invalid["evidence_ids"] == []
    assert TEXT in response.answer_text
    assert len(model.prompts) == 2


def test_foreign_unknown_gap_does_not_block_a_valid_missing_material_search():
    reviews = [{"request_id": "Q1", "status": "missing", "reviewed": True, "evidence_status": "missing"}]
    plan = request_recovery_plan(reviews, ["Q999"], set(), expected_request_ids={"Q1"})
    assert [(row["request_id"], row["cause"], row["action"]) for row in plan] == [
        ("Q1", "evidence_missing", "retrieval"),
        ("Q999", "invalid_request_id", "retain"),
    ]


def test_explicit_unknown_uses_original_id_and_retains_body_without_remedy():
    current = run_review([("Q1", "unknown")])
    assert current.addressed == set()
    [row] = current.unanswered
    assert row["request_id"] == "Q1" and row["status"] == "unknown"
    assert row["evidence_status"] == "unknown"
    assert current.trace()["validation_dimensions"]["answer_completeness"]["execution"] == "partial"
    [decision] = request_recovery_plan(current.unanswered, (), set(), expected_request_ids={"Q1"})
    assert decision["action"] == "retain" and decision["cause"] == "review_incomplete"


def test_audit_prompt_explains_exact_id_and_unknown_without_duplicate_rule():
    prompt = grounded.audit_system_prompt(detailed=True)
    assert prompt.count("新規・改変・重複した id を作らない") == 1
    assert "元の id のまま unknown" in prompt


@pytest.mark.parametrize("reviews", [[], [("Q999", "addressed")], [("Q1", "addressed"), ("Q1", "missing")], [("Q1", "unknown")]])
def test_unverified_request_survives_final_envelope_with_supported_body(reviews):
    from test_grounded_answer import run

    ctx = context(record(0, TEXT, "対象A"))
    reference = ids(ctx, QUESTION)[TEXT]
    model = FakeModel(
        [draft(item(reference, TEXT, TEXT, request_id="Q1"), summary="")],
        [audit((0, "supported", "matched", ""), requests=reviews)],
    )
    response = run(model, ctx, QUESTION).response
    assert response.envelope["requests"] == [{"id": "Q1", "text": QUESTION, "status": "unknown"}]
    assert response.envelope["outcome"] == "conditional"
    assert "unverified_requests" in response.envelope["handoff_reasons"]
    assert "unanswered_requests" not in response.envelope["handoff_reasons"]
    assert TEXT in response.answer_text
    assert len(model.prompts) == 2
