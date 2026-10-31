"""根拠の選別だけを合成入力で検査する。実モデル・検索は呼ばない。"""

from copy import deepcopy
from typing import Any

import pytest

from rag_engine.generation.evidence_refinement import select_refinement_evidence


def evidence(number: int) -> dict[str, Any]:
    return {
        "evidence_id": f"stable-{number}",
        "label": f"E{number}",
        "source_id": f"source-{number}",
        "source": "synthetic.pdf",
        "section_path": [f"合成機能{number}"],
        "text": f"合成操作{number}を実行します。",
        "start": 100 * number,
        "end": 100 * number + 15,
        "page": number,
        "page_end": number,
        "function": ("synthetic.pdf", f"合成機能{number}"),
        "source_aliases": [f"alias-{number}"],
        "source_context": {
            "document_name": "synthetic.pdf",
            "search_context_header": "別の根拠の補助情報 stable-3",
            "children": [{"source_id": "source-3", "search_context_header": "除外する補助文"}],
        },
        "answer_context": "source-3の別機能を参照します。",
    }


def observed(number: int, **changes: Any) -> dict[str, Any]:
    return {"evidence_id": f"stable-{number}", "citation_validity": "valid", **changes}


def test_default_legacy_keeps_all_inputs_and_does_not_change_metadata() -> None:
    inputs = [evidence(1), evidence(2)]
    before = deepcopy(inputs)
    result = select_refinement_evidence(inputs, [observed(1)])
    assert result.spans == tuple(before)
    assert result.diagnostics["reason"] == "legacy"
    assert inputs == before


def test_cited_only_preserves_original_labels_positions_text_and_input_order() -> None:
    inputs = [evidence(3), evidence(1), evidence(2)]
    before = deepcopy(inputs)
    result = select_refinement_evidence(inputs, [observed(1), observed(3)], strategy="cited_only")
    assert [span["label"] for span in result.spans] == ["E3", "E1"]
    for span in result.spans:
        original = next(row for row in before if row["evidence_id"] == span["evidence_id"])
        for key in ("text", "start", "end", "page", "page_end", "source", "evidence_id", "source_id", "label"):
            assert span[key] == original[key]
    assert inputs == before
    assert result.diagnostics["semantic_support_adopted"] is False


def test_cited_required_keeps_uncited_dependency_completion_exception_and_unknown_sources() -> None:
    inputs = [evidence(number) for number in range(1, 6)]
    result = select_refinement_evidence(
        inputs,
        [observed(1)],
        strategy="cited_required",
        required_source_groups=[("source-2",), ("alias-3",)],
        uncertain_source_groups=[("source-4",)],
    )
    assert [row["evidence_id"] for row in result.spans] == ["stable-1", "stable-2", "stable-3", "stable-4"]
    assert result.diagnostics["candidate_missing_required_groups"] == []
    assert result.diagnostics["candidate_missing_uncertain_groups"] == []


def test_cited_only_reports_missing_protection_without_turning_into_required_strategy() -> None:
    result = select_refinement_evidence(
        [evidence(1), evidence(2)],
        [observed(1)],
        strategy="cited_only",
        required_source_groups=[("source-2",)],
        uncertain_source_groups=[("unlocated",)],
        unresolved_requirement_ids=["requirement-unknown"],
    )
    assert [row["evidence_id"] for row in result.spans] == ["stable-1"]
    assert result.diagnostics["candidate_missing_required_groups"] == [["source-2"]]
    assert result.diagnostics["candidate_missing_uncertain_groups"] == [["unlocated"]]
    assert result.diagnostics["unresolved_requirement_ids"] == ["requirement-unknown"]


@pytest.mark.parametrize(
    "protection",
    [{"required_source_groups": [("unknown",)]}, {"uncertain_source_groups": [()]}, {"unresolved_requirement_ids": ["required-unknown"]}],
)
def test_required_strategy_falls_back_when_preservation_cannot_be_resolved(protection: dict[str, Any]) -> None:
    inputs = [evidence(1), evidence(2)]
    result = select_refinement_evidence(inputs, [observed(1)], strategy="cited_required", **protection)
    assert result.spans == tuple(inputs)
    assert result.diagnostics["reason"] == "unresolved_preservation_scope"


@pytest.mark.parametrize(
    "observations",
    [
        [],
        [observed(1, citation_validity="invalid")],
        [observed(1, content_usability="unusable")],
        [observed(9)],
        [observed(1, citation_validity="unknown", claim_support="supported")],
    ],
)
def test_no_valid_citations_keeps_all_evidence(observations: list[dict[str, Any]]) -> None:
    inputs = [evidence(1), evidence(2)]
    result = select_refinement_evidence(inputs, observations, strategy="cited_only")
    assert result.spans == tuple(inputs)
    assert result.diagnostics["reason"] == "no_valid_citations"


def test_raw_semantic_unsupported_or_wrong_target_does_not_drop_a_real_citation() -> None:
    result = select_refinement_evidence(
        [evidence(1), evidence(2)],
        [observed(1, claim_support="unsupported", applicability="not_applicable")],
        strategy="cited_only",
    )
    assert [row["evidence_id"] for row in result.spans] == ["stable-1"]
    assert result.diagnostics["citation_observations"][0]["claim_support"] == "unsupported"
    assert result.diagnostics["semantic_support_adopted"] is False


def test_multiple_citations_and_all_matching_required_sources_are_not_forced_to_one_source() -> None:
    inputs = [evidence(number) for number in range(1, 5)]
    inputs[2]["source_aliases"].append("shared-parent")
    inputs[3]["source_aliases"].append("shared-parent")
    result = select_refinement_evidence(
        inputs, [observed(1), observed(2)], strategy="cited_required", required_source_groups=[("shared-parent",)]
    )
    assert len(result.spans) == 4


def test_removed_source_context_cannot_reenter_through_metadata() -> None:
    inputs = [evidence(1), evidence(2), evidence(3)]
    inputs[0].update(
        source_aliases=["alias-1", "source-3", "alias-3"],
        scope_source_ids=["source-1", "source-3"],
        child_ranges=[{"id": "source-1", "start": 100, "end": 115}, {"id": "source-3", "start": 999, "end": 1000}],
        metadata={"arbitrary_context": "stable-3"},
        document_texts=["stable-3の本文"],
        function_texts=["除外した機能の原文"],
        adjacent_texts=["除外した隣接頁の原文"],
        screen_texts=["除外した画面の説明"],
        limits=["除外した機能の条件"],
    )
    result = select_refinement_evidence(inputs, [observed(1)], strategy="cited_only")
    kept = result.spans[0]
    assert not {"answer_context", "metadata", "document_texts", "function_texts", "adjacent_texts", "screen_texts", "limits"} & kept.keys()
    assert kept["source_context"] == {"document_name": "synthetic.pdf", "section_path": ["合成機能1"]}
    assert kept["source_aliases"] == ["alias-1"] and kept["scope_source_ids"] == ["source-1"]
    assert kept["child_ranges"] == [{"id": "source-1", "start": 100, "end": 115}]
    assert inputs[0]["source_aliases"] == ["alias-1", "source-3", "alias-3"]


def test_failed_or_ambiguous_selection_keeps_original_collection() -> None:
    inputs = [evidence(1), evidence(1)]
    result = select_refinement_evidence(inputs, [observed(1)], strategy="cited_only")
    assert result.spans == tuple(inputs)
    assert result.diagnostics["reason"] == "ambiguous_evidence_identity"
    inputs = [evidence(1), {"text": "識別子の無い合成根拠"}]
    failed = select_refinement_evidence(inputs, [observed(1)], strategy="cited_required")
    assert failed.spans == tuple(inputs) and failed.diagnostics["reason"] == "selection_failed"
