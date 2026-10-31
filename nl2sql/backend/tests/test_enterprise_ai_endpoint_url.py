"""接続の Endpoint URL を入力どおり使い、`{endpoint}/responses` を呼ぶ（#1440）。

3 製品は platform の `enterprise_ai_request_url` で URL を決める。RAG・Agent のテストと同じ 3 種の
Endpoint URL で、同じ URL を呼ぶことを確かめる（path を補わない。OCI も `/openai/v1` まで書く）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.features.nl2sql.enterprise_ai_client import OciEnterpriseAiDirectClient
from app.settings import EnterpriseAiConfiguredModel, Settings

# (Endpoint URL, 呼ぶ URL)。RAG・Agent の test_enterprise_ai_endpoint_url.py と同じ表。
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
        "oci_enterprise_ai_endpoint": endpoint,
        "oci_enterprise_ai_project_ocid": "",
        "oci_enterprise_ai_api_key": "sk-test",
        "oci_enterprise_ai_models": [
            EnterpriseAiConfiguredModel(model_id="text-a"),
            EnterpriseAiConfiguredModel(model_id="vision-b", vision_enabled=True),
        ],
        "oci_enterprise_ai_default_text_model": "text-a",
        "oci_enterprise_ai_default_vision_model": "vision-b",
        "oci_enterprise_ai_max_retries": 0,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def recorded_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"output_text": json.dumps({"text": "ok"})})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return urls


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
def test_text_and_vision_call_endpoint_as_is(
    recorded_urls: list[str], endpoint: str, expected: str
) -> None:
    client = OciEnterpriseAiDirectClient(_settings(endpoint))

    client.generate(prompt="質問", context="", system_prompt="日本語で答える")
    client.generate_from_image(b"jpeg", "説明してください")

    assert recorded_urls == [expected, expected]
