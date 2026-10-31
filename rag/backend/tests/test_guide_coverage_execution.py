"""本文充足の未実行と、完了した不足の結果を分けて公開する。"""

from typing import Any

import pytest
from pytest import MonkeyPatch

from app.rag import guide_step_validation as validation
from app.rag.answer_checks import AnswerFinding
from app.schemas.support_guide import SupportGuideContent


@pytest.mark.parametrize(
    "execution", ["not_run", "error", "timed_out", "partial", "unknown", "completed"]
)
def test_coverage_execution_controls_only_unexplained_findings(
    monkeypatch: MonkeyPatch, execution: str
) -> None:
    step: dict[str, Any] = {
        "step_id": "open",
        "title": "合成手順",
        "required": True,
        "status": "unused",
        "answer_quote": "",
        "validation": {"body_coverage": {"execution": execution, "status": "missing"}},
    }
    monkeypatch.setattr(
        validation,
        "validate_guide_steps",
        lambda *args, **kwargs: {"steps": [step], "embedding": None},
    )
    deterministic = AnswerFinding("guide_steps", "step_wrong_branch", "error", "合成の分岐矛盾")
    monkeypatch.setattr(validation, "check_guide_steps", lambda *args, **kwargs: [deterministic])
    findings, steps, embedding = validation.guide_step_findings(
        "説明",
        SupportGuideContent.model_validate(
            {
                "title": "合成ガイド",
                "goal": {"expected_result": "合成結果"},
                "steps": [{"id": "open", "title": "合成手順"}],
            }
        ),
        {},
        [],
        [],
        embed=None,
    )
    assert steps[0]["validation"] == step["validation"]
    assert findings[0] == deterministic
    assert [finding.code for finding in findings[1:]] == (
        ["guide_step_unexplained"] if execution == "completed" else []
    )
    assert embedding is None
