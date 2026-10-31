"""回答漏れ・材料不足・未検証の補救経路を決定論的に確かめる。"""

import pytest

from rag_engine.generation.request_recovery import request_recovery_plan


@pytest.mark.parametrize(
    ("evidence_status", "refs", "expected"),
    [
        ("available", ["E1"], ("answer_omission", "correction")),
        ("available", [], ("unverified_evidence", "retain")),
        ("available", ["E1", "unknown"], ("unverified_evidence", "retain")),
        ("missing", [], ("evidence_missing", "retrieval")),
        ("unknown", [], ("unverified_evidence", "retain")),
    ],
)
def test_reviewed_material_status_selects_the_remedy(evidence_status, refs, expected):
    rows = [{"request_id": "Q1", "status": "partial", "reviewed": True, "evidence_status": evidence_status, "evidence_ids": refs}]
    plan = request_recovery_plan(rows, (), {"E1"})
    assert len(plan) == 1
    assert (plan[0]["cause"], plan[0]["action"]) == expected
    assert plan[0]["evidence_ids"] == (refs if expected[1] == "correction" else [])


def test_unexecuted_review_is_not_a_missing_material_verdict():
    assert request_recovery_plan([{"request_id": "Q1", "status": "missing", "reviewed": False}], (), set()) == []


def test_explicit_generated_gap_can_retrieve_without_an_audit():
    plan = request_recovery_plan([], ["Q1", "Q1"], set())
    assert [(row["request_id"], row["action"]) for row in plan] == [("Q1", "retrieval")]


def test_available_material_overrides_generated_gap_without_searching_again():
    rows = [{"request_id": "Q1", "status": "missing", "reviewed": True, "evidence_status": "available", "evidence_ids": ["E1", "E1"]}]
    plan = request_recovery_plan(rows, ["Q1"], {"E1"})
    assert plan[0]["action"] == "correction"
    assert plan[0]["evidence_ids"] == ["E1"]


@pytest.mark.parametrize("status", ["partial", "missing"])
def test_reviewed_unknown_is_not_overridden_by_a_generated_gap(status):
    row = {"request_id": "Q1", "status": status, "reviewed": True, "evidence_status": "unknown", "evidence_ids": []}
    [decision] = request_recovery_plan([row], ["Q1"], {"E1"})
    assert decision["cause"] == "unverified_evidence" and decision["action"] == "retain"


def test_reviewed_missing_can_still_search_with_a_generated_gap():
    row = {"request_id": "Q1", "status": "partial", "reviewed": True, "evidence_status": "missing", "evidence_ids": []}
    [decision] = request_recovery_plan([row], ["Q1"], set())
    assert decision["cause"] == "evidence_missing" and decision["action"] == "retrieval"


def test_an_unexecuted_review_alone_does_not_trigger_recovery():
    row = {"request_id": "Q1", "status": "missing", "reviewed": False, "evidence_status": "missing"}
    assert request_recovery_plan([row], (), set()) == []


@pytest.mark.parametrize("reviewed", [False, True])
def test_duplicate_review_blocks_a_generated_gap_instead_of_becoming_missing_material(reviewed):
    row = {"request_id": "Q1", "status": "missing", "reviewed": reviewed, "evidence_status": "missing"}
    [decision] = request_recovery_plan([row], ["Q1"], set(), blocked_request_ids=["Q1"])
    assert decision["cause"] == "review_incomplete" and decision["action"] == "retain"
    assert decision["evidence_ids"] == []


def test_duplicate_without_a_verdict_keeps_an_unknown_status():
    [decision] = request_recovery_plan([], ["Q1"], set(), blocked_request_ids=["Q1"])
    assert decision["status"] == "unknown" and decision["action"] == "retain"
