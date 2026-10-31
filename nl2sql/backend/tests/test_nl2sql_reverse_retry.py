"""実測の自然言語応答と段階 retry・時間予算・失敗分類の回帰。"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from test_nl2sql_reverse_access import _service
from test_nl2sql_structure_roundtrip import StagedClient

from app.features.nl2sql import reverse_generation
from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.features.nl2sql.models import ReverseSqlRequest


class RawClient(StagedClient):
    def generate(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return cast(str, output)


def test_structured_question_response_is_success_without_retry() -> None:
    service = _service()
    client = RawClient(
        [
            '{"logical_structure":"physical"}',
            '{"logical_structure":"logical"}',
            '{"question":"すべての従業員情報を教えてください。"}',
        ]
    )
    cast(Any, service)._enterprise_ai_client = client
    result = service.reverse_sql_deep(
        ReverseSqlRequest(sql="SELECT ID FROM APP.INVOICES", profile_id="finance")
    )
    assert result.question == "すべての従業員情報を教えてください。"
    assert result.warnings == []
    assert result.source == "oci_enterprise_ai"
    assert len(client.calls) == 3
    assert "指定された JSON Schema に従い" in client.calls[-1]["system_prompt"]


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "すべての情報を教えてください。",
        '{"question":',
        '{"question":[]}',
        EnterpriseAiDirectError("timeout", code="timeout", retryable=True),
        EnterpriseAiDirectError("busy", code="unavailable", retryable=True),
    ],
)
def test_only_failed_question_stage_is_retried(
    failure: object, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service()
    client = RawClient(
        [
            '{"logical_structure":"physical"}',
            '{"logical_structure":"logical"}',
            failure,
            '{"question":"すべての請求情報を教えてください。"}',
        ]
    )
    cast(Any, service)._enterprise_ai_client = client
    result = service.reverse_sql_deep(
        ReverseSqlRequest(sql="SELECT ID FROM APP.INVOICES", profile_id="finance")
    )
    assert not result.warnings
    assert result.logical_structure == "logical"
    assert len(client.calls) == 4
    assert all(call["max_retries"] == 0 for call in client.calls)
    assert client.calls[2]["prompt"] == client.calls[3]["prompt"] == "logical"
    assert "reverse_sql_stage_failed" in caplog.text
    assert "SELECT ID" not in caplog.text


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_retry_exhaustion_keeps_completed_stages(stage: int) -> None:
    service = _service()
    outputs = ['{"logical_structure":"physical"}', '{"logical_structure":"logical"}'][:stage]
    client = RawClient([*outputs, " ", " ", " "])
    cast(Any, service)._enterprise_ai_client = client
    result = service.reverse_sql_deep(
        ReverseSqlRequest(sql="SELECT ID FROM APP.INVOICES", profile_id="finance")
    )
    assert len(client.calls) == stage + 3
    assert "試行 3 回" in result.warnings[0]
    assert "応答が空" in result.warnings[0]
    assert (
        reverse_generation.STAGE_LABELS[list(reverse_generation.STAGE_LABELS)[stage]]
        in result.warnings[0]
    )
    if stage == 2:
        assert result.logical_structure == "logical"
        assert "SQL 論理構造は生成済み" in result.warnings[0]
    else:
        assert "SELECT ID FROM APP.INVOICES" in result.logical_structure


def test_auth_failure_is_not_retried_or_exposed() -> None:
    service = _service()
    client = RawClient([EnterpriseAiDirectError("SECRET response", code="authentication")])
    cast(Any, service)._enterprise_ai_client = client
    result = service.reverse_sql_deep(
        ReverseSqlRequest(sql="SELECT ID FROM APP.INVOICES", profile_id="finance")
    )
    assert len(client.calls) == 1
    assert "認証" in result.warnings[0]
    assert "SECRET" not in result.warnings[0]


def test_deadline_prevents_further_calls() -> None:
    client = RawClient([])
    with pytest.raises(reverse_generation.ReverseStageError) as exc:
        reverse_generation.generate_stage(
            client=cast(Any, client),
            stage="business_question",
            prompt="synthetic",
            context="",
            system_prompt="",
            parse=str,
            timeout_seconds=600,
            max_retries=10,
            deadline=time.monotonic() - 1,
        )
    assert exc.value.reason == "deadline"
    assert client.calls == []


@pytest.mark.parametrize("max_retries, attempts", [(0, 1), (10, 3)])
def test_retry_count_is_bounded(max_retries: int, attempts: int) -> None:
    client = RawClient([""] * attempts)
    service = _service()
    with pytest.raises(reverse_generation.ReverseStageError) as exc:
        reverse_generation.generate_stage(
            client=cast(Any, client),
            stage="business_question",
            prompt="synthetic",
            context="",
            system_prompt="",
            parse=service._reverse_question_from_text,
            timeout_seconds=600,
            max_retries=max_retries,
            deadline=time.monotonic() + 10,
        )
    assert exc.value.attempts == attempts
    assert len(client.calls) == attempts
    assert all(0 < call["timeout_seconds"] <= 10 for call in client.calls)
