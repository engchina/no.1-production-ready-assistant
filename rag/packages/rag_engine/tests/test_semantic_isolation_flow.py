"""未校正の観察や失敗が全回答の自動是正へ戻らないことを確認する。"""

from dataclasses import replace
from typing import Any

import pytest
from test_uncalibrated_audit_diagnostics import BODY, OPEN, QUESTION, SAVE, Model

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.models.llm import GroundedAudit


@pytest.mark.parametrize("observation", ["contradicted", "not_applicable", "unknown", "error", "timed_out", "missing"])
def test_untrusted_semantic_outcome_keeps_body_and_does_not_regenerate(monkeypatch: pytest.MonkeyPatch, observation: str) -> None:
    model = Model(
        "contradicted" if observation == "contradicted" else "unknown",
        "not_applicable" if observation == "not_applicable" else "unknown",
        summary="確認済みの条件に沿う操作です。",
    )
    audit_attempts: list[str] = []
    if observation in {"error", "timed_out", "missing"}:

        def synthetic_audit(*args: Any, **kwargs: Any) -> GroundedAudit:
            audit_attempts.append(observation)
            if observation == "error":
                raise ValueError("synthetic invalid audit")
            if observation == "timed_out":
                raise TimeoutError("synthetic timeout")
            return GroundedAudit(reviews=[], request_reviews=[], summary_supported=False, goal_alignment="off_target")

        monkeypatch.setattr(grounded, "_parse_audit", synthetic_audit)

    record = AnswerRecord(
        id="source-a",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=BODY,
        source="synthetic.pdf",
        chunk_level="parent",
        chunk_id="source-a",
        source_run_id="run",
    )
    config = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="detailed", answer_max_correction_rounds=2)

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external I/O is not required")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = answering.synthesize_grounded_answer(
            QUESTION, AnswerContext(records=[record], text=BODY), config, image_prompt_mode="text_only"
        )
    trace = result.response.generation_trace
    assert result.response.answer_text.index(OPEN) < result.response.answer_text.index(SAVE)
    assert "承認済み" in result.response.answer_text
    assert len(trace["rounds"]) == 1
    assert trace["rounds"][0]["timing"]["generation_calls"] == 1
    assert result.response.envelope["outcome"] != "insufficient_evidence"
    assert not any(row.get("removed", False) for row in trace["rounds"][0]["items"])
    dimensions = trace["rounds"][0]["validation_dimensions"]
    assert all(row["status"] == "unknown" for row in dimensions["claim_support"]["items"])
    if observation in {"error", "timed_out", "missing"}:
        assert len(model.calls) == 1
        assert audit_attempts == [observation] * (2 if observation == "missing" else 1)
    else:
        assert len(model.calls) == 2 and len(model.audits) == 1
    expected_execution = {"error": "error", "timed_out": "timed_out", "missing": "error"}.get(observation, "completed")
    assert trace["rounds"][0]["review_execution"] == expected_execution
