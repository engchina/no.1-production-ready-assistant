"""候補対応や未実行を、確認した不足へ変換しない。外部モデルは呼ばない。"""

from typing import Any

import pytest
from test_guide_step_validation import ANSWER, _combined, _result, _step

from app.features.agent.answer_outcome import SIGNAL_GUIDE_STEPS, answer_outcome
from app.features.agent.answer_validation import answer_warnings, error_findings, merge_guide_steps


@pytest.mark.parametrize(
    "status", ["unknown", "partial", "not_searched", "unused", "reference_only", "no_evidence"]
)
@pytest.mark.parametrize("execution", [None, "not_run", "error", "timed_out", "partial", "unknown"])
def test_unverified_coverage_keeps_diagnostics_without_global_problem(
    status: str, execution: str | None
) -> None:
    step = _step("s1", "合成手順", status)
    if execution is None:
        step.pop("validation")
    else:
        step["validation"] = {"body_coverage": {"execution": execution, "status": "unknown"}}
    result = _result([step])
    content, published = _combined(result)
    assert published == ANSWER
    assert content["answer_warnings"] == []
    assert content["passage_marks"] == []
    assert content["result"]["guide_steps"] == [step]
    assert content["result"]["findings"] == result["findings"]
    assert SIGNAL_GUIDE_STEPS not in answer_outcome(ANSWER, steps=[], validation=content)["signals"]


def test_unknown_coverage_does_not_become_an_error_even_if_legacy_severity_is_error() -> None:
    step = _step("s1", "合成手順", "unknown")
    step.pop("validation")
    result = _result([step])
    result["findings"][0]["severity"] = "error"
    assert error_findings(result) == []
    assert answer_warnings(result) == []


@pytest.mark.parametrize("status", ["unknown", "partial", "not_searched"])
def test_multiple_connections_preserve_all_public_coverage_states(status: str) -> None:
    step = _step("s1", "合成手順", status)
    step["validation"] = {"body_coverage": {"execution": "not_run", "status": "unknown"}}
    assert merge_guide_steps([{"guide_steps": [step]}, {"guide_steps": []}]) == [step]


@pytest.mark.parametrize("code", ["step_order", "step_wrong_branch", "step_dependency_missing"])
def test_deterministic_guide_errors_still_warn(code: str) -> None:
    result: dict[str, Any] = {
        "findings": [
            {
                "check": "guide_steps",
                "code": code,
                "severity": "error",
                "message": "合成の検査で誤りを確認",
            }
        ]
    }
    assert len(error_findings(result)) == 1
    assert answer_warnings(result)[0]["code"] == "guide_check"


def test_confirmed_missing_and_contradicted_claims_are_not_suppressed() -> None:
    result = _result([_step("s1", "合成手順", "unused")])
    result["claims"][0]["status"] = "contradicted"
    result["counts"] = {"contradicted": 1, "supported": 1}
    content, published = _combined(result)
    assert content["answer_warnings"][0]["code"] == "guide_check"
    assert content["removed_passages"]
    assert ANSWER.splitlines()[0] not in published.split("**")[0]
