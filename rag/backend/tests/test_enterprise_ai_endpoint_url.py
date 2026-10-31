"""接続の Endpoint URL を入力どおり使い、`{endpoint}/responses` を呼ぶ（#1440）。

RAG の backend は platform の `enterprise_ai_base_url` で決めた base URL を、回答（`rag_engine`）・
接続テストと Vision（`rag_parser_core`）・parser の環境変数に渡す。NL2SQL・Agent のテストと同じ
3 種の Endpoint URL で、どの経路も同じ URL を呼ぶことを確かめる（path を補わない）。
以前の `rag_engine` は path の無い URL に `/openai/v1` を足し、`https://api.deepseek.com` で
`…/openai/v1/responses` を呼んで 404 になっていた。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.clients.oci_enterprise_ai import EnterpriseAiHttpTransport, OciEnterpriseAiClient
from app.config import EnterpriseAiConfiguredModel, Settings
from app.services.control import service_runtime_env

# (Endpoint URL, 呼ぶ URL)。NL2SQL・Agent の test_enterprise_ai_endpoint_url.py と同じ表。
ENDPOINT_CASES = [
    (
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1",
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1/responses",
    ),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/responses"),
    ("https://api.deepseek.com", "https://api.deepseek.com/responses"),
]
ENDPOINT_CASE_IDS = ["oci-openai-v1", "compatible-with-path", "compatible-without-path"]


def _settings(endpoint: str) -> Settings:
    values: dict[str, Any] = {
        "oci_compartment_id": "ocid1.compartment.oc1..example",
        "oci_enterprise_ai_endpoint": endpoint,
        "oci_enterprise_ai_project_ocid": "",
        "oci_enterprise_ai_api_key": "sk-test",
        "oci_enterprise_ai_models": [
            EnterpriseAiConfiguredModel(model_id="text-a"),
            EnterpriseAiConfiguredModel(model_id="vision-b", vision_enabled=True),
        ],
        "oci_enterprise_ai_default_text_model": "text-a",
        "oci_enterprise_ai_default_vision_model": "vision-b",
        "oci_enterprise_ai_llm_path": "/responses",
        "oci_enterprise_ai_vlm_path": "/responses",
        "oci_enterprise_ai_vlm_input_mode": "files_api",
        "oci_enterprise_ai_timeout_seconds": 5.0,
        "oci_enterprise_ai_max_retries": 0,
    }
    return Settings.model_construct(**values)


class RecordingTransport:
    """Enterprise AI の HTTP の URL を記録する fake（応答は固定）。"""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def post_json(
        self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str], timeout: float
    ) -> Mapping[str, Any]:
        self.urls.append(url)
        return {"output_text": "ok"}


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
async def test_model_test_client_calls_endpoint_as_is(endpoint: str, expected: str) -> None:
    """接続テスト・Vision が使う共有 core（rag_parser_core）の URL。"""
    transport = RecordingTransport()
    # テキストの生成は post_json だけを使う（ほかの method は呼ばない）。
    client = OciEnterpriseAiClient(
        settings=_settings(endpoint),
        http_transport=cast(EnterpriseAiHttpTransport, transport),
    )

    await client.generate("質問", "根拠")

    assert transport.urls == [expected]
    assert client.preview_llm_request("質問", "根拠").url == expected


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
def test_answer_engine_calls_endpoint_as_is(tmp_path: Path, endpoint: str, expected: str) -> None:
    """回答・評価・`rag_validate_answer` が使う rag_engine の OpenAI SDK の URL。"""
    from rag_engine.adapters import oci
    from rag_engine.config import ENTERPRISE_AI_LLM_PROVIDER, ENTERPRISE_AI_VISION_LLM_PROVIDER

    from app.rag.answer_engine import build_engine_settings

    engine_settings = build_engine_settings(_settings(endpoint), output_dir=tmp_path)
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"id": "resp-1", "object": "response", "output": []})

    for provider_id in (ENTERPRISE_AI_LLM_PROVIDER, ENTERPRISE_AI_VISION_LLM_PROVIDER):
        provider = engine_settings.llm_providers[provider_id]
        client = oci._openai_client_for_provider(provider, engine_settings).with_options(  # noqa: SLF001
            http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        client.responses.create(model=provider.model, input="x")

    assert urls == [expected, expected]


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
def test_parser_service_env_calls_endpoint_as_is(endpoint: str, expected: str) -> None:
    """OCI parser（microservice）は backend が渡す env の Endpoint URL で呼ぶ。"""
    from rag_parser_core.oci_enterprise_ai import OciEnterpriseAiClient as ParserClient
    from rag_parser_core.oci_enterprise_ai import OciEnterpriseAiConfig

    env = service_runtime_env(_settings(endpoint))
    config = OciEnterpriseAiConfig.from_env(env)

    assert ParserClient(config).preview_llm_request("質問", "根拠").url == expected
