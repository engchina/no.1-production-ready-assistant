"""自然言語質問だけを入力にした SQL 生成と認可・安全境界。"""

from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError
from test_nl2sql_reverse_access import _principal, _request, _service
from test_nl2sql_structure_roundtrip import StagedClient

from app.features.nl2sql import router
from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.features.nl2sql.models import QuestionToSqlRequest
from app.security.permissions import permission_for_route


def test_question_is_the_only_generation_input_with_schema_and_without_glossary() -> None:
    service = _service()
    sql = "SELECT * FROM APP.INVOICES WHERE AMOUNT >= 100 ORDER BY ID"
    client = StagedClient([{"sql": sql, "explanation": "請求情報を取得します。"}])
    cast(Any, service)._enterprise_ai_client = client
    question = "金額が100以上の請求情報をすべてID順に教えてください。"
    result = service.question_to_sql(QuestionToSqlRequest(question=question, profile_id="finance"))
    assert result.sql == sql
    assert result.source == "oci_enterprise_ai"
    assert len(client.calls) == 1
    assert client.calls[0]["prompt"] == question
    assert "table APP.INVOICES logical=請求" in client.calls[0]["context"]
    assert "table APP.ORDERS" not in client.calls[0]["context"]
    assert "- 請求: INVOICES" not in client.calls[0]["context"]
    assert "自然言語の質問" in client.calls[0]["system_prompt"]


@pytest.mark.parametrize("field", ["sql", "logical_structure", "use_glossary"])
def test_request_rejects_unowned_inputs(field: str) -> None:
    with pytest.raises(ValidationError):
        QuestionToSqlRequest.model_validate({"question": "請求情報", field: "unexpected"})


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM APP.INVOICES",
        "SELECT * FROM APP.INVOICES; DELETE FROM APP.INVOICES",
        "SELECT * FROM APP.ORDERS",
        "SELECT UTL_HTTP.REQUEST('https://example.com') FROM APP.INVOICES",
        "not sql",
    ],
)
def test_question_generation_cannot_bypass_sql_safety_or_profile(sql: str) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient([{"sql": sql}])
    with pytest.raises(ValueError):
        service.question_to_sql(QuestionToSqlRequest(question="請求情報", profile_id="finance"))


@pytest.mark.parametrize("output", [{}, {"sql": []}, {"sql": None}, "not JSON"])
def test_bad_provider_response_is_retryable_502(
    output: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient([output])
    monkeypatch.setattr(router, "nl2sql_service", service)
    with pytest.raises(HTTPException) as exc:
        router.question_to_sql(
            QuestionToSqlRequest(question="請求情報", profile_id="finance"),
            cast(Request, _request(_principal({"finance"}))),
        )
    assert exc.value.status_code == 502
    assert "再試行" in exc.value.detail


def test_endpoint_requires_profile_access_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    client = StagedClient([{"sql": "SELECT * FROM APP.INVOICES"}])
    cast(Any, service)._enterprise_ai_client = client
    monkeypatch.setattr(router, "nl2sql_service", service)
    req = QuestionToSqlRequest(question="請求情報", profile_id="finance")
    with pytest.raises(HTTPException) as exc:
        router.question_to_sql(req, cast(Request, _request(_principal({"sales"}))))
    assert exc.value.status_code == 403
    assert client.calls == []
    result = router.question_to_sql(req, cast(Request, _request(_principal({"finance"}))))
    assert result.data is not None
    assert result.data.sql == "SELECT * FROM APP.INVOICES"
    assert permission_for_route("POST", "/nl2sql/reverse/question-sql") == frozenset(
        {"menu.sql_to_question"}
    )


def test_unconfigured_or_blank_question_never_fabricates_sql() -> None:
    service = _service()
    client = StagedClient([], configured=False)
    cast(Any, service)._enterprise_ai_client = client
    for question in [" ", "請求情報"]:
        with pytest.raises(ValueError):
            service.question_to_sql(QuestionToSqlRequest(question=question, profile_id="finance"))
    assert client.calls == []


def test_empty_sql_returns_actionable_reason_and_provider_failure_is_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient(
        [
            {"sql": "", "explanation": "質問に取得する項目を指定してください。"},
            EnterpriseAiDirectError("unavailable"),
        ]
    )
    monkeypatch.setattr(router, "nl2sql_service", service)
    req = QuestionToSqlRequest(question="請求情報", profile_id="finance")
    for status, message in [(400, "質問に取得する項目"), (502, "再試行")]:
        with pytest.raises(HTTPException) as exc:
            router.question_to_sql(req, cast(Request, _request(_principal({"finance"}))))
        assert exc.value.status_code == status
        assert message in exc.value.detail
