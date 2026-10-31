"""監査入力の出典対応を展開するだけで、意味支持や充足を決めない。"""

from copy import deepcopy
from typing import Any

from rag_engine.generation.guide_context import audit_guide_candidates

STEPS: list[dict[str, Any]] = [
    {"step_id": "a", "evidence_ids": ["direct-a", "shared", "shared"]},
    {"step_id": "b", "evidence_ids": ["shared", "direct-b"]},
]
CONTRACTS: list[dict[str, Any]] = [
    {"step_id": "b", "requirements": [{"id": "b:0", "text": "手順Bの条件", "kind": "evidence"}]},
    {
        "step_id": "a",
        "requirements": [
            {"id": "a:1", "text": "手順Aの操作", "kind": "evidence"},
            {"id": "a:0", "text": "手順Aの完了", "kind": "completion"},
        ],
    },
]


def test_actual_citation_expands_only_its_related_steps_and_original_requirement_order() -> None:
    rows = audit_guide_candidates(STEPS, CONTRACTS, ["direct-a"])
    assert rows == [
        {
            "step_id": "a",
            "association": "source_only",
            "matched_evidence_ids": ["direct-a"],
            "candidate_requirements": CONTRACTS[1]["requirements"],
        }
    ]
    assert [row["id"] for row in rows[0]["candidate_requirements"]] == ["a:1", "a:0"]


def test_shared_citation_is_a_candidate_for_multiple_steps_without_exclusive_assignment() -> None:
    rows = audit_guide_candidates(STEPS, CONTRACTS, iter(["shared", "shared"]))
    assert [row["step_id"] for row in rows] == ["a", "b"]
    assert [row["matched_evidence_ids"] for row in rows] == [["shared"], ["shared"]]
    assert all(row["association"] == "source_only" for row in rows)
    assert all(set(row) == {"step_id", "association", "matched_evidence_ids", "candidate_requirements"} for row in rows)


def test_unreferenced_and_unknown_sources_do_not_become_candidates() -> None:
    assert audit_guide_candidates(STEPS, CONTRACTS, []) == []
    assert audit_guide_candidates(STEPS, CONTRACTS, ["unknown", "", "E1"]) == []
    assert audit_guide_candidates([], CONTRACTS, ["direct-a"]) == []
    assert audit_guide_candidates([{"step_id": "a", "evidence_ids": []}], CONTRACTS, ["direct-a"]) == []


def test_missing_contract_does_not_invent_requirements_or_infer_material_status() -> None:
    assert audit_guide_candidates(STEPS, [], ["direct-a"]) == [
        {
            "step_id": "a",
            "association": "source_only",
            "matched_evidence_ids": ["direct-a"],
            "candidate_requirements": [],
        }
    ]


def test_matching_uses_exact_ids_and_preserves_step_evidence_order() -> None:
    rows = audit_guide_candidates(STEPS, CONTRACTS, ["shared", "direct-a", "direct-a", "direct-b"])
    assert [row["step_id"] for row in rows] == ["a", "b"]
    assert rows[0]["matched_evidence_ids"] == ["direct-a", "shared"]
    assert audit_guide_candidates(STEPS, CONTRACTS, ["DIRECT-A", " direct-a "]) == []


def test_candidates_and_inputs_are_independent_without_mutating_original_contract() -> None:
    before = deepcopy((STEPS, CONTRACTS))
    result = audit_guide_candidates(STEPS, CONTRACTS, ["direct-a"])
    result[0]["candidate_requirements"][0]["text"] = "監査入力だけの変更"
    result[0]["matched_evidence_ids"].append("other")
    assert before == (STEPS, CONTRACTS)
