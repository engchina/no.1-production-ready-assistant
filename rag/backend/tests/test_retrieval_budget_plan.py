"""比較の準備は実行せず、既存の品質評価の入力契約を保つ。"""

import json
from pathlib import Path

import pytest

from app.rag.retrieval_budget_plan import main, prepare_plan
from app.schemas.evaluation import EvaluationRunRequest


def request() -> EvaluationRunRequest:
    return EvaluationRunRequest.model_validate(
        {
            "cases": [
                {
                    "id": "case-a",
                    "query": "合成資料の設定を確認する",
                    "relevant_document_ids": ["doc-a"],
                }
            ],
            "search_answer_profile_id": "profile-a",
            "top_k": 50,
            "filters": {
                "page_ranges": json.dumps(
                    [{"document_id": "doc-a", "page_start": 2, "page_end": 4}]
                )
            },
            "rag_overrides": {"rerank_enabled": True, "reference_expansion_enabled": True},
        }
    )


def test_variants_preserve_cases_scope_and_unrelated_overrides() -> None:
    baseline = request()
    plan = prepare_plan(baseline, total_caps=[100, 200, 100])
    assert not plan["executed"] and plan["requires_effective_settings_snapshot"]
    assert plan["unavailable_controls"] == ["document_selection_disabled"]
    for variant in plan["variants"]:
        parsed = EvaluationRunRequest.model_validate(variant["request"])
        assert parsed.cases == baseline.cases and parsed.filters == baseline.filters
        assert parsed.search_answer_profile_id == baseline.search_answer_profile_id
        assert parsed.rag_overrides is not None and parsed.rag_overrides.rerank_enabled
        if variant["id"] == "without-reference":
            assert parsed.rag_overrides.reference_expansion_enabled is False
        if variant["id"] == "without-diversity":
            assert variant["environment_overlay"] == {"RAG_CANDIDATE_DIVERSITY_MAX_EXTRA": "0"}
    assert sum(value["id"].startswith("total-cap-") for value in plan["variants"]) == 2
    assert baseline == request()


def test_cli_generates_valid_inputs_and_refuses_to_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "request.json"
    source.write_text(request().model_dump_json(), encoding="utf-8")
    output = tmp_path / "prepared"
    assert main([str(source), "--output-dir", str(output), "--total-cap", "123"]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    for variant in manifest["variants"]:
        prepared = EvaluationRunRequest.model_validate_json(
            (output / f"{variant['id']}.json").read_text()
        )
        assert prepared.cases == request().cases
    with pytest.raises(FileExistsError):
        main([str(source), "--output-dir", str(output)])


@pytest.mark.parametrize("cap", [0, -1, 2001])
def test_invalid_cap_never_prepares_requests(cap: int) -> None:
    with pytest.raises(ValueError):
        prepare_plan(request(), total_caps=[cap])


def test_recovery_matrix_preserves_scope_and_prepares_only_four_conditions() -> None:
    original = request()
    plan = prepare_plan(original, recovery_matrix=True)
    assert len(plan["variants"]) == 4
    assert not plan["executed"]
    observed = set()
    for variant in plan["variants"]:
        parsed = EvaluationRunRequest.model_validate(variant["request"])
        assert parsed.cases == original.cases and parsed.filters == original.filters
        assert parsed.search_answer_profile_id == original.search_answer_profile_id
        assert parsed.rag_overrides is not None
        assert parsed.rag_overrides.rerank_enabled is True
        assert parsed.rag_overrides.reference_expansion_enabled is True
        observed.add(
            (
                parsed.rag_overrides.answer_flow,
                variant["environment_overlay"]["RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED"],
            )
        )
    assert observed == {
        ("standard_rag", "false"),
        ("standard_rag", "true"),
        ("crag", "false"),
        ("crag", "true"),
    }
    assert original == request()
    with pytest.raises(ValueError):
        prepare_plan(original, recovery_matrix=True, total_caps=[100])
