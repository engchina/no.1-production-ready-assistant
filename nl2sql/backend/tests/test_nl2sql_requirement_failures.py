"""回答前の確認の LLM の失敗の理由と「詳細」、最低限の要件だけを確かめる方針（#1715・#1727）。

モデルの API は httpx.MockTransport（決定論）で受ける。応答の形は 2026-10-13 の実測
（`xai.grok-4.3` の HTTP 500 `internal_error`、`openai.gpt-oss-120b` の `status=incomplete`・
`incomplete_details.reason=max_output_tokens`）に合わせる。実サービスの確認は PR に書く。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.features.nl2sql.enterprise_ai_client import HttpEnterpriseAiDirectClient
from app.features.nl2sql.ontology_router import _raise_domain_error
from app.features.nl2sql.sql_requirement import (
    RequirementAiUnavailableError,
    RequirementContext,
    call_requirement_llm,
)
from app.main import http_exception_problem_handler
from app.settings import EnterpriseAiConfiguredModel, Settings

ENDPOINT = "https://llm.example/v1"
API_KEY = "sk-requirement-secret-key-0001"
MODEL = "xai.grok-4.3"
QUESTION = "部門ごとの社員の平均給与"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "enterprise_ai_endpoint": ENDPOINT,
        "enterprise_ai_api_key": API_KEY,
        "enterprise_ai_models": [EnterpriseAiConfiguredModel(model_id=MODEL)],
        "enterprise_ai_default_text_model": MODEL,
        "enterprise_ai_max_retries": 3,
        "enterprise_ai_llm_max_output_tokens": 1200,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _context() -> RequirementContext:
    return RequirementContext(
        question=QUESTION,
        tables={},
        relationships=[],
        profile_context={},
        ontology_concepts=[],
        evidence_corpus="",
        fiscal_year_start=None,
        today=date(2026, 10, 13),
    )


def _valid_output() -> dict[str, Any]:
    return {
        "clarified_question_ja": QUESTION,
        "items": [],
        "answerable": True,
        "unanswerable_reason_ja": "",
    }


class _Transport:
    """送った payload を記録し、応答を順に返す MockTransport。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *responses: Any) -> None:
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []
        self.timeouts: list[Any] = []
        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            self.payloads.append(json.loads(request.content))
            response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            if isinstance(response, Exception):
                raise response
            assert isinstance(response, httpx.Response)
            return response

        def client(**kwargs: Any) -> httpx.Client:
            self.timeouts.append(kwargs.get("timeout"))
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "Client", client)


def _call(settings: Settings, **options: Any) -> Any:
    return call_requirement_llm(
        HttpEnterpriseAiDirectClient(settings),
        question=QUESTION,
        context=_context(),
        mode="draft",
        **options,
    )


def _failure(settings: Settings, **options: Any) -> RequirementAiUnavailableError:
    with pytest.raises(RequirementAiUnavailableError) as raised:
        _call(settings, **options)
    return raised.value


def _json(status: int, body: dict[str, Any], **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def _assert_no_secret(error: RequirementAiUnavailableError) -> None:
    text = json.dumps({"message": error.message_ja, "details": error.details}, ensure_ascii=False)
    assert API_KEY not in text
    assert "Bearer" not in text
    assert QUESTION not in text  # prompt の全文を出さない


# --- 1. 失敗の種類ごとの理由と「詳細」 -------------------------------------------------


def test_server_error_names_status_attempts_model_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        monkeypatch,
        _json(
            500,
            {
                "error": {
                    "message": "Internal server error",
                    "type": "server_error",
                    "code": "internal_error",
                }
            },
            **{"opc-request-id": "opc-req-500"},
        ),
    )
    error = _failure(_settings(), max_retries=1, timeout_seconds=90.0)

    assert error.code == "REQUIREMENT_AI_FAILED"
    assert "サーバーのエラー" in error.message_ja
    assert "HTTP 500、2 回試行" in error.message_ja
    # 次の操作（もう一度確認・確認せずに生成）は画面が出す。
    assert "確認せずに" not in error.message_ja
    assert len(transport.payloads) == 2  # 既定の 3 回ではなく、回答前の確認の再試行 1 回
    details = error.details
    assert details["kind"] == "unavailable"
    assert details["http_status"] == 500
    assert details["model_id"] == MODEL
    assert details["attempts"] == 2
    assert details["upstream_request_id"] == "opc-req-500"
    assert details["provider_message"] == "Internal server error (internal_error)"
    assert details["error_code"] == "ENTERPRISE_AI_UNAVAILABLE"
    assert details["mode"] == "draft"
    assert isinstance(details["elapsed_ms"], int)
    _assert_no_secret(error)


def test_rate_limit_asks_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    _Transport(monkeypatch, _json(429, {"error": {"message": "Too many requests"}}))
    error = _failure(_settings(), max_retries=0)

    assert "呼び出しの上限" in error.message_ja
    assert "HTTP 429" in error.message_ja
    assert "少し待ってから" in error.message_ja
    assert error.details["kind"] == "rate_limit"
    assert error.details["retryable"] is True


def test_bad_request_shows_provider_reason_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Transport(
        monkeypatch,
        _json(
            400,
            {
                "error": {
                    "message": f"text.format is not supported (key {API_KEY})",
                    "code": "invalid_value",
                }
            },
        ),
    )
    error = _failure(_settings(), max_retries=0)

    assert "受け付けませんでした（HTTP 400）" in error.message_ja
    assert MODEL in error.message_ja
    assert error.details["kind"] == "request"
    assert "text.format is not supported" in error.details["provider_message"]
    _assert_no_secret(error)


def test_timeout_names_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _Transport(monkeypatch, httpx.ReadTimeout("timed out"))
    error = _failure(_settings(), max_retries=0, timeout_seconds=90.0)

    assert f"回答前の確認の LLM（{MODEL}）が 90 秒以内に応答しませんでした" in error.message_ja
    assert error.details["kind"] == "timeout"
    assert error.details["timeout_seconds"] == 90.0
    assert transport.timeouts == [90.0]


def test_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _Transport(monkeypatch, httpx.ConnectError("refused"))
    error = _failure(_settings(), max_retries=0)

    assert "接続できませんでした" in error.message_ja
    assert error.details["kind"] == "connection"


def test_incomplete_output_names_the_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """推論の token で出力の上限を使い切った応答（openai.gpt-oss-120b の実測の形）。"""
    _Transport(
        monkeypatch,
        _json(
            200,
            {
                "id": "resp_incomplete_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        ),
    )
    error = _failure(_settings(), max_retries=0, max_output_tokens=16384)

    # 回答前の確認の上限（16384）はそのまま送り、広げて送り直さない（#2310）。推論の量は既定の
    # low で、下げて送り直せない。
    assert "出力の上限（16384 token）に達して" in error.message_ja
    assert "NL2SQL_REQUIREMENT_MAX_OUTPUT_TOKENS" in error.message_ja
    assert "「出力の上限（tokens）」" in error.message_ja
    assert error.details["kind"] == "incomplete"
    assert error.details["max_output_tokens"] == 16384
    assert error.details["upstream_request_id"] == "resp_incomplete_1"


def test_schema_mismatch_is_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    _Transport(monkeypatch, _json(200, {"output_text": '{"items": "not-a-list"}'}))
    error = _failure(_settings(), max_retries=0)

    assert "指定の JSON の形式ではありませんでした" in error.message_ja
    assert error.details["kind"] == "response_format"
    _assert_no_secret(error)


def test_configuration_error_does_not_ask_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _Transport(
        monkeypatch,
        _json(
            404, {"code": "404", "message": "Authorization failed or requested resource not found."}
        ),
    )
    error = _failure(_settings(), max_retries=3)

    assert "時間をおいても直りません" in error.message_ja
    assert "もう一度確認" not in error.message_ja
    assert error.details["kind"] == "authorization"


# --- 2. 回答前の確認の上限（共通の LLM の上限 1200 を使わない） -------------------------------


def test_requirement_uses_its_own_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _Transport(monkeypatch, _json(200, {"output_text": json.dumps(_valid_output())}))
    output, model = _call(_settings(), max_output_tokens=16384, timeout_seconds=90.0, max_retries=1)

    assert model == MODEL
    assert output.clarified_question_ja == QUESTION
    # 回答前の確認の上限は、ほかの呼び出しの既定（32768。#1719）へ引き上げずに送る（#2310）。
    assert transport.payloads[0]["max_output_tokens"] == 16384
    assert transport.timeouts == [90.0]


def test_requirement_output_limit_is_capped_by_the_model_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答前の確認の上限より、モデルの出力の上限が小さければモデルの上限で送る。"""
    transport = _Transport(monkeypatch, _json(200, {"output_text": json.dumps(_valid_output())}))
    settings = _settings(
        enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id=MODEL, max_output_tokens=12_000)]
    )
    _call(settings, max_output_tokens=16384, max_retries=0)

    assert transport.payloads[0]["max_output_tokens"] == 12_000


def test_requirement_output_limit_retries_with_lower_effort_without_widening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """出力の上限で未完了なら、上限を広げずに推論を下げて 1 回だけ送り直す（#2310）。

    推論以外の出力が止まらずに上限まで書き続けた回（検証環境の実測の形）を、広げた上限で
    もう一度待たない。モデルの出力の上限（131072）まで広げられる構成でも広げない。
    """
    incomplete = _json(
        200,
        {
            "id": "resp_incomplete_2",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {
                "output_tokens": 16384,
                "output_tokens_details": {"reasoning_tokens": 1200},
            },
        },
    )
    transport = _Transport(
        monkeypatch,
        incomplete,
        _json(200, {"output_text": json.dumps(_valid_output())}),
    )
    settings = _settings(
        enterprise_ai_models=[
            EnterpriseAiConfiguredModel(
                model_id=MODEL,
                reasoning_effort="medium",
                max_input_tokens=262_144,
                max_output_tokens=131_072,
            )
        ]
    )
    output, _model = _call(settings, max_output_tokens=16384, max_retries=0)

    assert output.clarified_question_ja == QUESTION
    assert [payload["max_output_tokens"] for payload in transport.payloads] == [16384, 16384]
    assert [payload["reasoning"] for payload in transport.payloads] == [
        {"effort": "medium"},
        {"effort": "low"},
    ]


def test_router_passes_requirement_settings() -> None:
    from app.features.nl2sql.ontology_router import OntologyApiRuntime
    from app.settings import get_settings

    settings = get_settings()
    options = OntologyApiRuntime._requirement_llm_options(object.__new__(OntologyApiRuntime))
    assert options == {
        "timeout_seconds": float(settings.nl2sql_requirement_timeout_seconds),
        "max_retries": int(settings.nl2sql_requirement_max_retries),
        "max_output_tokens": int(settings.nl2sql_requirement_max_output_tokens),
    }
    # 回答前の確認の上限は 16384（#2310。ユーザーの判断。ほかの呼び出しの 32768 の例外）。
    field = Settings.model_fields["nl2sql_requirement_max_output_tokens"]
    assert field.default == 16384
    assert settings.nl2sql_requirement_max_output_tokens >= 8192
    # 応答の遅い回（2026-10-17 の実測 208 秒）が収まり、試行の全部（最悪）が Agent の
    # AGENT_EXTERNAL_NL2SQL_TIMEOUT_SECONDS（600 秒）の内訳の 480 秒に収まる（#2240）。
    assert settings.nl2sql_requirement_timeout_seconds >= 210
    assert (
        settings.nl2sql_requirement_timeout_seconds * (settings.nl2sql_requirement_max_retries + 1)
        <= 480
    )


def test_requirement_output_limit_setting_bounds() -> None:
    """上限は設定で変えられ、実測の要る量の約 2 倍（8192）より小さい値は受けない（#2310）。"""
    from pydantic import ValidationError

    assert (
        Settings(
            _env_file=None, nl2sql_requirement_max_output_tokens=32768
        ).nl2sql_requirement_max_output_tokens
        == 32768
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, nl2sql_requirement_max_output_tokens=4096)


# --- 3. API の応答（problem と error_details） ----------------------------------------


async def test_api_returns_reason_and_details() -> None:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_problem_handler)  # type: ignore[arg-type]

    @app.post("/fail")
    def fail() -> None:
        _raise_domain_error(
            RequirementAiUnavailableError(
                "REQUIREMENT_AI_FAILED",
                "回答前の確認でモデルの API がサーバーのエラーを返しました（HTTP 500）。",
                details={"kind": "unavailable", "http_status": 500, "model_id": MODEL},
            )
        )

    @app.post("/not-configured")
    def not_configured() -> None:
        _raise_domain_error(
            RequirementAiUnavailableError("REQUIREMENT_AI_NOT_CONFIGURED", "未設定です。")
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/fail")
        legacy = await client.post("/not-configured")

    assert response.status_code == 503
    body = response.json()
    assert body["error_messages"] == [
        "回答前の確認でモデルの API がサーバーのエラーを返しました（HTTP 500）。"
    ]
    assert body["error_code"] == "REQUIREMENT_AI_FAILED"
    assert body["problem"]["code"] == "REQUIREMENT_AI_FAILED"
    assert body["error_details"] == {"kind": "unavailable", "http_status": 500, "model_id": MODEL}
    # 未設定（画面が確認せずに SQL を作る判定に使う）は、これまでどおり文に code を含める。
    assert legacy.status_code == 503
    assert "REQUIREMENT_AI_NOT_CONFIGURED" in legacy.json()["error_messages"][0]
    assert "error_details" not in legacy.json()


# --- 4. 最低限の要件だけを確かめる方針（#1715） ---------------------------------------


def test_system_prompt_asks_only_for_the_minimum() -> None:
    from app.features.nl2sql.sql_requirement import SYSTEM_PROMPT

    assert "最低限" in SYSTEM_PROMPT
    assert "指定なし（使わない）" in SYSTEM_PROMPT
    # 細部まで確かめさせる旧い指示（欄の例に限らずすべて項目にする）を持たない。
    assert "すべて項目にしてください" not in SYSTEM_PROMPT
    import re

    assert re.findall(r"(?m)^## Step (\d+):", SYSTEM_PROMPT) == ["1", "2", "3", "4", "5"]
    # 規則 12 の件数は、プログラムの上限（MAX_OPEN_QUESTIONS）と同じ。
    from app.features.nl2sql.sql_requirement import MAX_OPEN_QUESTIONS

    assert f"多くても {MAX_OPEN_QUESTIONS} 件" in SYSTEM_PROMPT
