"""OCI Enterprise AI direct client の payload 契約テスト。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.features.nl2sql.enterprise_ai_client import _build_payload
from app.settings import Settings


def test_enterprise_ai_payload_includes_responses_structured_output_format() -> None:
    response_format = {
        "type": "json_schema",
        "name": "question_intent_graph_v1",
        "schema": {"type": "object", "additionalProperties": False},
        "strict": True,
    }

    payload = _build_payload(
        settings=Settings(),
        model_id="enterprise-intent",
        prompt="受注件数を表示",
        context="{}",
        system_prompt="JSON だけを返してください。",
        response_format=response_format,
    )

    assert payload["text"] == {"format": response_format}


def test_enterprise_ai_payload_template_receives_response_format_object() -> None:
    response_format = {
        "type": "json_schema",
        "name": "question_intent_graph_v1",
        "schema": {"type": "object", "additionalProperties": False},
        "strict": True,
    }
    settings = Settings(
        oci_enterprise_ai_llm_payload_template=(
            '{"model":"${model}","format":"${response_format}","input":"${prompt}"}'
        )
    )

    payload = _build_payload(
        settings=settings,
        model_id="enterprise-intent",
        prompt="受注件数を表示",
        context="{}",
        system_prompt="JSON だけを返してください。",
        response_format=response_format,
    )

    assert payload["format"] == response_format


def test_reverse_retry_uses_typed_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.features.nl2sql import enterprise_ai_client as module

    settings = Settings(
        oci_enterprise_ai_endpoint="https://enterprise.example.test",
        oci_enterprise_ai_api_key="synthetic-test-key",
    )
    real_client = httpx.Client
    for status, code, retryable in [
        (401, "authentication", False),
        # OCI の 403 / 404 は API key の認可の失敗（#1684）。
        (403, "authorization", False),
        (404, "authorization", False),
        (400, "request", False),
        (429, "rate_limit", True),
        (503, "unavailable", True),
    ]:
        transport = httpx.MockTransport(
            lambda request, status=status: httpx.Response(status, json={"error": "synthetic"})
        )
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda transport=transport, **kwargs: real_client(transport=transport, **kwargs),
        )
        with pytest.raises(module.EnterpriseAiDirectError) as error:
            client = module.OciEnterpriseAiDirectClient(settings)
            client._post_json(
                {}, connection=client.connection(""), path="/responses", max_retries=0
            )
        assert error.value.code == code
        assert error.value.retryable is retryable


# OCI の実測の応答の本文（2026-10-13。#1684）。
_OCI_404_BODY = (
    '{ "code": "404", "message": "Authorization failed or requested resource not found." }'
)
_OCI_MODEL_NOT_FOUND_BODY = (
    '{"error":{"code":null,"message":"Entity with key xai.grok-4.6 not found",'
    '"param":null,"type":"invalid_request_error","valid":true}}'
)


def _direct_settings() -> Settings:
    return Settings(
        oci_enterprise_ai_endpoint=(
            "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1"
        ),
        oci_enterprise_ai_api_key="synthetic-test-key",
        oci_enterprise_ai_project_ocid="ocid1.generativeaiproject.oc1.us-chicago-1.synthetic",
        oci_enterprise_ai_default_text_model="openai.gpt-oss-120b",
    )


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    import httpx

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs)
    )


@pytest.mark.parametrize(
    ("status", "body", "kind", "error_code", "configuration_error"),
    [
        (404, _OCI_404_BODY, "authorization", "ENTERPRISE_AI_AUTHORIZATION_FAILED", True),
        (403, '{"code":"403"}', "authorization", "ENTERPRISE_AI_AUTHORIZATION_FAILED", True),
        (401, '{"code":"401"}', "authentication", "ENTERPRISE_AI_AUTHENTICATION_FAILED", True),
        (400, _OCI_MODEL_NOT_FOUND_BODY, "model_not_found", "ENTERPRISE_AI_MODEL_NOT_FOUND", True),
        (
            400,
            '{"error":{"message":"bad reasoning"}}',
            "request",
            "ENTERPRISE_AI_REQUEST_REJECTED",
            False,
        ),
    ],
    ids=["oci-404", "403", "401", "model-not-found", "other-400"],
)
def test_configuration_failures_are_classified_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: str,
    kind: str,
    error_code: str,
    configuration_error: bool,
) -> None:
    """設定の誤り（#1684）は再試行せず、呼んだモデル・接続のホストを持つ。"""
    import httpx

    from app.features.nl2sql import enterprise_ai_client as module

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text=body)

    _mock_http(monkeypatch, handler)
    client = module.OciEnterpriseAiDirectClient(_direct_settings())
    with pytest.raises(module.EnterpriseAiDirectError) as raised:
        client.generate(prompt="社員一覧", context="", system_prompt="SQL", max_retries=3)

    error = raised.value
    assert len(calls) == 1
    assert error.code == kind
    assert error.retryable is False
    assert error.configuration_error is configuration_error
    assert error.http_failure is not None and error.http_failure.error_code == error_code
    assert error.model_id == "openai.gpt-oss-120b"
    assert error.endpoint_host == "inference.generativeai.us-chicago-1.oci.oraclecloud.com"
    actions = " ".join(error.failure_actions())
    if kind in {"authorization", "model_not_found"}:
        assert "openai.gpt-oss-120b" in actions
    if configuration_error:
        assert "inference.generativeai.us-chicago-1.oci.oraclecloud.com" in actions
    assert "synthetic-test-key" not in actions


def test_unavailable_is_still_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.features.nl2sql import enterprise_ai_client as module

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="busy")

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    client = module.OciEnterpriseAiDirectClient(_direct_settings())
    with pytest.raises(module.EnterpriseAiDirectError) as raised:
        client.generate(prompt="社員一覧", context="", system_prompt="SQL", max_retries=2)

    assert len(calls) == 3
    assert raised.value.code == "unavailable"
    assert raised.value.retryable is True
    assert raised.value.configuration_error is False


def test_direct_call_uses_system_default_text_model_and_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """アプリが直接呼ぶ Enterprise AI は、既定のテキストモデルとその接続を使う（#1684）。"""
    import json

    import httpx

    from app.features.nl2sql import enterprise_ai_client as module

    seen: list[tuple[str, dict[str, object], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                json.loads(request.content),
                request.headers.get("OpenAI-Project", ""),
            )
        )
        return httpx.Response(200, json={"output_text": '{"text":"ok"}'})

    _mock_http(monkeypatch, handler)
    client = module.OciEnterpriseAiDirectClient(_direct_settings())
    client.generate(prompt="社員一覧", context="", system_prompt="SQL")

    url, payload, project = seen[0]
    assert url == (
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1/responses"
    )
    assert payload["model"] == "openai.gpt-oss-120b"
    assert project == "ocid1.generativeaiproject.oc1.us-chicago-1.synthetic"


@pytest.mark.usefixtures("trace_body_enabled")
def test_model_call_snapshot_includes_schema_and_returned_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from pr_backend_core.observability.tracing import operation_trace

    from app.features.nl2sql import enterprise_ai_client as module

    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"output_text": '{"text":"synthetic-result"}'}),
    )
    client = module.OciEnterpriseAiDirectClient(_direct_settings())
    with operation_trace("synthetic", operation_id="a") as trace:
        client.generate(prompt="synthetic-input", context="", system_prompt="synthetic-system")
    span = next(s for s in trace.spans if s.operation_name == "gen_ai.chat")
    assert span.display and span.display.details and span.display.details.call
    assert "synthetic-input" in span.display.details.call.input.text
    assert "synthetic-result" in span.display.details.call.output.text


@pytest.fixture
def trace_body_enabled() -> Iterator[None]:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
