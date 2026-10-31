"""既定 Enterprise AI transport が openai SDK 経由で呼ぶことの契約テスト。"""

import json
from collections.abc import Callable

import httpx
import openai
import pytest
from rag_parser_core.oci_enterprise_ai import (
    EnterpriseAiTimeoutError,
    OciEnterpriseAiClient,
    OciEnterpriseAiConfig,
    _DefaultEnterpriseAiTransport,
)

ENDPOINT = "https://enterprise-ai.example/openai/v1"


def _config(max_retries: int = 0) -> OciEnterpriseAiConfig:
    return OciEnterpriseAiConfig(
        oci_enterprise_ai_endpoint=ENDPOINT,
        oci_enterprise_ai_api_key="test-key",
        oci_enterprise_ai_project_ocid="ocid1.project.test",
        default_model_id="test-llm",
        oci_enterprise_ai_max_retries=max_retries,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 0,
) -> OciEnterpriseAiClient:
    """実 transport(openai SDK)を使い、HTTP 層だけ MockTransport に差し替える。"""

    def _sdk_client(self: _DefaultEnterpriseAiTransport, timeout: float) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key="test-key",
            base_url=ENDPOINT,
            max_retries=max_retries,
            timeout=timeout,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(_DefaultEnterpriseAiTransport, "_client", _sdk_client)
    return OciEnterpriseAiClient(_config(max_retries))


async def test_generate_posts_responses_via_openai_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"output_text": "回答です"})

    answer = await _client(monkeypatch, handler).generate("質問", "根拠")

    assert answer == "回答です"
    request = requests[0]
    assert str(request.url) == f"{ENDPOINT}/responses"
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["openai-project"] == "ocid1.project.test"
    assert json.loads(request.content)["model"] == "test-llm"
    # SDK が付けるヘッダーを重ねて送らない(重複を連結する gateway は 400。#3527)
    assert request.headers.get_list("content-type") == ["application/json"]
    assert request.headers.get_list("accept") == ["application/json"]


async def test_generate_retries_retryable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        headers = {"retry-after": "0"}
        if status == 200:
            return httpx.Response(200, json={"output_text": "ok"}, headers=headers)
        return httpx.Response(status, json={"error": "busy"}, headers=headers)

    answer = await _client(monkeypatch, handler, max_retries=1).generate("q", "c")

    assert answer == "ok"


async def test_http_error_keeps_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    with pytest.raises(httpx.HTTPStatusError, match="model not found"):
        await _client(monkeypatch, handler).generate("q", "c")


async def test_timeout_maps_to_enterprise_ai_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EnterpriseAiTimeoutError):
        await _client(monkeypatch, handler).generate("q", "c")


async def test_upload_file_sends_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "file-1"})

    transport = _DefaultEnterpriseAiTransport(_config())
    _client(monkeypatch, handler)  # _client の差し替えだけを使う
    response = await transport.upload_file(
        f"{ENDPOINT}/files",
        "input.pdf",
        b"%PDF",
        mime_type="application/pdf",
        purpose="user_data",
        headers={"accept": "application/json", "Authorization": "Bearer test-key"},
        timeout=5,
    )

    assert response == {"id": "file-1"}
    request = requests[0]
    (content_type,) = request.headers.get_list("content-type")
    assert content_type.startswith("multipart/form-data; boundary=")
    assert request.headers.get_list("accept") == ["application/json"]
    assert b'name="purpose"' in request.content
    assert b"user_data" in request.content
    assert b'filename="input.pdf"' in request.content
