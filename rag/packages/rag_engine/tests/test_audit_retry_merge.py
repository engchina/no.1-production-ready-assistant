"""再監査の部分的な形式失敗で、有効な局所観察を失わない。通信0。"""

import json

import pytest
from audit_fixtures import legacy_audit_to_wire_fixture

from rag_engine.generation.audit_recovery import merge_audit_retry
from rag_engine.models.audit_wire import audit_wire_schema, decode_audit_wire
from rag_engine.models.llm import GroundedAudit


def audit(*, support="supported", request_state="partial", requirement_state="not_mentioned"):
    return GroundedAudit.model_validate(
        {
            "reviews": [{"index": 0, "support": support, "applicability": "matched", "reason": "合成の意味観察"}],
            "application_reviews": [{"index": 0, "valid": False, "reason": "合成の適用観察"}],
            "summary_supported": False,
            "goal_alignment": "partial",
            "summary_basis": [],
            "request_reviews": [{"request_id": "Q1", "status": request_state, "reason": "合成の要求観察"}],
            "guide_requirement_reviews": [
                {
                    "requirement_id": "g:0",
                    "answer_state": requirement_state,
                    "item_indexes": [],
                    "answer_quote": "",
                    "reason": "合成の要件観察",
                }
            ],
        }
    )


def wire(value, **changes):
    schema = audit_wire_schema(GroundedAudit, ("Q1",), ("g:0",))
    payload = legacy_audit_to_wire_fixture(value, schema)
    payload.update(changes)
    result = decode_audit_wire(json.dumps(payload), schema)
    result.audit._wire_validation = result.validation
    return result.audit


@pytest.mark.parametrize("support", ["supported", "contradicted"])
def test_partial_retry_preserves_valid_first_claim_and_application(support):
    first = wire(audit(support=support))
    second = wire(audit(), reviews=[{"index": 0, "support": "invalid-status"}], application_reviews=[])
    merged = merge_audit_retry(first, second)
    assert merged.reviews[0].support == support
    assert merged.application_reviews[0].valid is False
    assert merged.request_reviews[0].status == "partial"
    assert merged._wire_validation["status"] == "error"
    assert merged._wire_validation["strict_wire_validation"] == "failed"
    history = merged._wire_validation["retry_merge"]
    assert history["row_sources"]["reviews"] == [{"key": 0, "attempt": 1}]
    assert history["attempts"][0]["validation"]["strict_wire_validation"] == "passed"
    assert history["attempts"][1]["validation"]["strict_wire_validation"] == "failed"
    assert not first._wire_validation.get("retry_merge")
    from rag_engine.generation import grounded
    from rag_engine.models.llm import GroundedCitation, GroundedItem

    body = "対象画面を開きます。"
    entry = grounded.CheckedItem(
        GroundedItem(kind="operation", text=body, applies="matched", citations=[GroundedCitation(evidence_id="e1", quote=body)]),
        span={"evidence_id": "e1", "text": body},
    )
    grounded.apply_audit([entry], merged)
    assert entry.removed is (support == "contradicted")
    assert entry.item.text == body


def test_valid_second_observations_replace_only_their_own_keys():
    first = wire(audit(support="contradicted"))
    repaired = audit(support="supported", request_state="addressed", requirement_state="explained")
    repaired.guide_requirement_reviews[0].item_indexes = [0]
    repaired.guide_requirement_reviews[0].answer_quote = "合成の実本文"
    second = wire(repaired)
    merged = merge_audit_retry(first, second)
    assert merged.reviews[0].support == "supported"
    assert merged.request_reviews[0].status == "addressed"
    assert merged.guide_requirement_reviews[0].answer_quote == "合成の実本文"
    assert merged._wire_validation["retry_merge"]["row_sources"]["guide_requirement_reviews"] == [{"key": "g:0", "attempt": 2}]


def test_missing_or_invalid_bound_row_keeps_first_and_does_not_invent_a_result():
    first = wire(audit())
    second = wire(audit(), request_reviews={}, guide_requirement_reviews={"g:0": {"answer_state": "invalid-status"}})
    merged = merge_audit_retry(first, second)
    assert merged.request_reviews[0].request_id == "Q1"
    assert merged.guide_requirement_reviews[0].answer_state == "not_mentioned"
    effective = merged._wire_validation["retry_merge"]["effective_request_binding"]
    assert effective == {"expected_ids": ["Q1"], "adopted_ids": ["Q1"], "states": {"Q1": "valid"}}
    assert merged._wire_validation["request_binding"]["states"] == {"Q1": "missing"}
    empty = merge_audit_retry(wire(audit(), guide_requirement_reviews={}), second)
    assert empty.guide_requirement_reviews == []
    assert empty._wire_validation["retry_merge"]["effective_requirement_binding"]["states"] == {"g:0": "unknown"}


def test_unverified_scalar_cannot_replace_valid_first_values():
    first = wire(audit())
    second = wire(audit(), summary_supported="invalid", goal_alignment="invalid", summary_basis="invalid")
    assert second._wire_validation["fields"]["summary_supported"] == "unverified"
    merged = merge_audit_retry(first, second)
    assert merged.summary_supported is False and merged.goal_alignment == "partial" and merged.summary_basis == []
    assert merged._wire_validation["retry_merge"]["field_sources"]["summary_supported"] == {"attempt": 1, "status": "valid"}
    assert merged._wire_validation["fields"]["summary_supported"] == "unverified"


def test_duplicate_second_index_does_not_override_a_valid_first_review():
    first = wire(audit(support="contradicted"))
    duplicated = audit(support="supported")
    duplicated.reviews.append(duplicated.reviews[0].model_copy())
    second = wire(duplicated)
    merged = merge_audit_retry(first, second)
    assert len(merged.reviews) == 1 and merged.reviews[0].support == "contradicted"


def test_existing_row_limit_is_preserved_without_dropping_first_observations():
    prior = audit()
    prior.reviews = [prior.reviews[0].model_copy(update={"index": index}) for index in range(40)]
    later = audit()
    later.reviews = [later.reviews[0].model_copy(update={"index": 40})]
    first, second = wire(prior), wire(later)
    merged = merge_audit_retry(first, second)
    assert [row.index for row in merged.reviews] == list(range(40))
    assert merged._wire_validation["retry_merge"]["limited_rows"] == {"reviews": [40]}


def test_two_unverified_scalars_keep_unknown_provenance_and_safe_attempt_snapshots():
    first = wire(audit(), summary_supported="invalid")
    second = wire(audit(), summary_supported="invalid")
    first._wire_validation["raw_text"] = "private raw response"
    first._wire_validation["retry_merge"] = {"attempts": ["old nested snapshot"]}
    merged = merge_audit_retry(first, second)
    assert merged.summary_supported is False
    metadata = merged._wire_validation["retry_merge"]
    assert metadata["field_sources"]["summary_supported"] == {"attempt": None, "status": "unverified"}
    assert len(metadata["attempts"]) == 2
    assert all("raw_text" not in row["validation"] and "retry_merge" not in row["validation"] for row in metadata["attempts"])
