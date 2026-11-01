"""同じ例外を既存の監査・CRAG・呼び出しログへ運ぶ。外部モデルは呼ばない。"""

import json
import logging
from collections.abc import Callable
from concurrent.futures import CancelledError
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ValidationError
from test_audit_failure_preserves_draft import run_failed_audit
from test_grounded_answer import context, record

from rag_engine.adapters import model_api
from rag_engine.config import LlmProviderSettings, get_settings
from rag_engine.generation import answering, execution_record
from rag_engine.generation.answer_models import CragRetrievalAttempt
from rag_engine.models.llm import CragRetrievalGradeOutput
from rag_engine.resources.deadline import DeadlineExceeded


class Schema(BaseModel):
    count: int


def synthetic_provider() -> LlmProviderSettings:
    return LlmProviderSettings(provider_id="test", label="test", region="", project_id="", api_key="", model="synthetic")


def invalid_json_output() -> Exception:
    try:
        json.loads('{"private-key":')
    except json.JSONDecodeError as cause:
        error = model_api.InvalidStructuredOutput("private-message")
        error.__cause__ = cause
        return error
    raise AssertionError("合成のJSONは閉じていない")


def invalid_output() -> Exception:
    try:
        Schema.model_validate({"count": "private-input"})
    except ValidationError as cause:
        error = model_api.InvalidStructuredOutput("private-message")
        error.__cause__ = cause
        return error
    raise AssertionError("合成の入力は型違反になる")


@pytest.mark.parametrize(
    "error", [invalid_output(), invalid_json_output(), model_api.IncompleteResponse("private-message", "max_output_tokens")]
)
def test_audit_failure_is_preserved_in_response_trace(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    current, _, calls = cast(Callable[..., Any], run_failed_audit)(monkeypatch, error)
    details = current.trace()["timing"]["audit_failure"]
    assert current.review_execution == "error"
    assert details["error_class"] == type(error).__name__
    assert details["category"] in {"schema_field", "invalid_json", "truncated_output"}
    assert len(calls) == 1
    assert "private" not in json.dumps(details)
    assert current.unanswered[0]["status"] == "unknown"
    assert current.unanswered[0]["reviewed"] is False


def test_failed_call_logger_keeps_safe_chain_as_extra(caplog: pytest.LogCaptureFixture) -> None:
    provider = synthetic_provider()
    with caplog.at_level(logging.INFO, logger=model_api.__name__):
        model_api._record_failed_call(invalid_output(), provider, Schema, 0.1)
    event = caplog.records[-1]
    assert event.__dict__["llm_failure"]["category"] == "schema_field"
    assert event.__dict__["llm_failure"]["cause_classes"] == ["ValidationError"]
    assert event.__dict__["llm_status"] == "failed"
    assert "private" not in json.dumps(event.__dict__["llm_failure"])


def run_crag(monkeypatch: pytest.MonkeyPatch, grade: object) -> tuple[Any, Mock]:
    ctx = cast(Callable[..., Any], context)(cast(Callable[..., Any], record)(0, "対象Aを確認します。", "対象A"))
    monkeypatch.setattr(answering, "build_adb_hybrid_answer_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(answering, "_crag_grade_skip_check", lambda *args, **kwargs: {"reason": "disabled"})
    monkeypatch.setattr(answering, "_crag_grade_skip", lambda *args, **kwargs: None)
    monkeypatch.setattr(answering, "CRAG_MAX_RETRIEVAL_ATTEMPTS", 1)
    mock = Mock(side_effect=grade) if isinstance(grade, BaseException) else Mock(return_value=grade)
    monkeypatch.setattr(answering, "_grade_crag_retrieval", mock)
    return answering.build_crag_answer_context("対象Aを確認する方法は？", None, (), get_settings(), rerank_enabled=False), mock


@pytest.mark.parametrize(
    "error", [invalid_output(), invalid_json_output(), model_api.IncompleteResponse("private-message", "max_output_tokens")]
)
def test_crag_failed_grade_has_details_without_missing_evidence(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    (_, _, attempts), mock = run_crag(monkeypatch, error)
    payload = attempts[0].to_payload()
    assert mock.call_count == 1
    assert payload["grade_execution"] == "error"
    assert payload["grade_failure"]["category"] in {"schema_field", "invalid_json", "truncated_output"}
    assert payload["required_aspect_coverage"] == "unknown"
    assert payload["missing_aspects"] == []
    assert "private" not in json.dumps(payload)


@pytest.mark.parametrize("error", [PermissionError("denied"), CancelledError(), DeadlineExceeded("deadline")])
def test_crag_does_not_downgrade_permission_cancel_or_deadline(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    with pytest.raises(type(error)):
        run_crag(monkeypatch, error)


@pytest.mark.parametrize("missing", [False, True])
def test_terminal_narrative_distinguishes_unverified_from_missing(monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    grade = CragRetrievalAttempt(
        attempt=1,
        query="対象A",
        retrieval_queries=("対象A",),
        sufficient=False,
        grade_execution="completed",
        required_aspect_coverage="missing" if missing else "unknown",
        missing_aspects=("operation",) if missing else (),
        recovery_action="stop",
    )
    lines: list[str] = []
    token = execution_record._execution_lines.set(lines)
    try:
        run_crag(monkeypatch, grade)
    finally:
        execution_record._execution_lines.reset(token)
    rendered = "\n".join(lines)
    if missing:
        assert "不足と判定されました" in rendered
    else:
        assert "充足を確認できなかった" in rendered
        assert "不足と判定されました" not in rendered
        assert "生成に使える材料なし" not in rendered


def test_grade_enum_failure_has_safe_enum_location() -> None:
    with pytest.raises(ValidationError) as caught:
        CragRetrievalGradeOutput.model_validate(
            {"candidate_verdicts": [{"chunk_id": "private", "relevant": True}], "recovery_action": "private"}
        )
    error = model_api.InvalidStructuredOutput("private")
    error.__cause__ = caught.value
    from rag_engine.resources.structured_failures import structured_failure_details

    details = structured_failure_details(error, CragRetrievalGradeOutput)
    assert details["category"] == "schema_enum"
    assert any(row["loc"] == ["recovery_action"] for row in details["validation_errors"])
    assert "private" not in json.dumps(details)


def test_real_structured_formatter_retains_failure_extra(caplog: pytest.LogCaptureFixture) -> None:
    from pr_backend_core.logging import StructuredFormatter

    with caplog.at_level(logging.INFO, logger=model_api.__name__):
        model_api._record_failed_call(invalid_output(), synthetic_provider(), Schema, 0.1)
    formatter = StructuredFormatter(
        service_name="test", service_version="test", environment="test", component="test", max_record_bytes=32768
    )
    rendered = json.loads(formatter.format(caplog.records[-1]))
    assert rendered["llm_failure"]["category"] == "schema_field"
    assert "private" not in json.dumps(rendered["llm_failure"])


def test_binding_failure_stays_separate_from_material_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_structured_failures import ContractFailure

    error = ContractFailure("citation_range_invalid")
    (_, _, attempts), mock = run_crag(monkeypatch, error)
    payload = attempts[0].to_payload()
    assert mock.call_count == 1
    assert payload["grade_failure"]["category"] == "evidence_binding"
    assert payload["required_aspect_coverage"] == "unknown"
    assert payload["missing_aspects"] == []
