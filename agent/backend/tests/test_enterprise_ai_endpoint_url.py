"""接続の Endpoint URL を入力どおり使い、`{endpoint}/responses` を呼ぶ（#1440）。

組み込み Runtime（OpenAI SDK の base URL）とモデルの接続テストの両方で、RAG・NL2SQL のテストと
同じ 3 種の Endpoint URL が同じ URL を呼ぶことを確かめる（path を補わない。OCI も `/openai/v1` まで
書く）。
OCI へは接続しない（HTTP は httpx.MockTransport）。
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import httpx2
import pytest
from openai import AsyncOpenAI
from pr_system_settings.model import (
    EnterpriseAiConfiguredModel,
    ModelSettingsTestRequest,
    model_test_candidate,
)

import app.features.agent.router as agent_router
from app.features.agent import builtin_runtime
from app.settings import Settings

# (Endpoint URL, 呼ぶ URL)。RAG・NL2SQL の test_enterprise_ai_endpoint_url.py と同じ表。
ENDPOINT_CASES = [
    (
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1",
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1/responses",
    ),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/responses"),
    ("https://api.deepseek.com", "https://api.deepseek.com/responses"),
]
ENDPOINT_CASE_IDS = ["oci-openai-v1", "compatible-with-path", "compatible-without-path"]

_RESPONSE: dict[str, Any] = {
    "id": "resp-1",
    "object": "response",
    "created_at": 0,
    "model": "text-a",
    "status": "completed",
    "output": [],
    "output_text": "ok",
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
}


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
def test_runtime_calls_endpoint_as_is(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, expected: str
) -> None:
    urls: list[str] = []

    # openai の SDK は httpx2 の client を受け取る（httpx の client は型が合わない）。
    def handler(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        return httpx2.Response(200, json=_RESPONSE)

    settings = Settings(
        _env_file=None,
        oci_enterprise_ai_endpoint=endpoint,
        oci_enterprise_ai_api_key="sk-test",
        oci_enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id="text-a")],
        oci_enterprise_ai_default_text_model="text-a",
    )
    monkeypatch.setattr(builtin_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(
        builtin_runtime,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **{
                **kwargs,
                "http_client": httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            }
        ),
    )

    target = builtin_runtime.resolve_model_target("")
    model: Any = builtin_runtime._default_model_factory(target)  # noqa: SLF001
    client: Any = model._get_client()  # noqa: SLF001 - SDK が使う client で呼ぶ

    anyio.run(lambda: client.responses.create(model="text-a", input="x"))

    assert urls == [expected]


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
def test_model_test_calls_endpoint_as_is(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, expected: str
) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"output_text": "ok"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    payload = {
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": endpoint,
                    "project_ocid": "",
                    "api_key": "sk-test",
                }
            ],
            "models": [{"model_id": "text-a"}],
            "default_text_model_id": "text-a",
            "default_vision_model_id": "",
            "text_response_path": "/output_text",
        },
        "generative_ai": {
            "embedding_model": "cohere.embed-v4.0",
            "embedding_dim": 1024,
            "rerank_model": "cohere.rerank-v4.0-fast",
        },
    }
    request = ModelSettingsTestRequest.model_validate(
        {"settings": payload, "target_type": "enterprise_text", "model_id": "text-a"}
    )
    candidate = model_test_candidate(Settings(_env_file=None), request)

    anyio.run(agent_router._run_model_settings_test, candidate, request)  # noqa: SLF001

    assert urls == [expected]
