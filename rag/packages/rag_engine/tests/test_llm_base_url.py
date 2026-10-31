"""LLM の base URL は Endpoint URL を入力どおり使い、path を補わない(#1440)。

以前は path の無い Endpoint URL に `/openai/v1` を足し、OCI 以外の OpenAI 互換 API
(例 `https://api.deepseek.com`)で `…/openai/v1/responses` を呼んで 404 になっていた。
backend は platform の `enterprise_ai_base_url` で決めた値を渡す。NL2SQL・Agent・RAG backend の
test_enterprise_ai_endpoint_url.py と同じ表で、OpenAI SDK が呼ぶ URL を確かめる。
"""

import httpx
import pytest

from rag_engine.adapters import oci
from rag_engine.config import ENTERPRISE_AI_LLM_PROVIDER, get_settings

# (Endpoint URL, 呼ぶ URL)
ENDPOINT_CASES = [
    (
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1",
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1/responses",
    ),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/responses"),
    ("https://api.deepseek.com", "https://api.deepseek.com/responses"),
]
ENDPOINT_CASE_IDS = ["oci-openai-v1", "compatible-with-path", "compatible-without-path"]


def _settings(endpoint: str):
    return get_settings(
        environ={
            "OCI_ENTERPRISE_AI_ENDPOINT": endpoint,
            "OCI_ENTERPRISE_AI_API_KEY": "sk-test",
            "OCI_ENTERPRISE_AI_DEFAULT_MODEL": "vendor.text",
            "LLM_RETRIES": "0",
        },
        dotenv_path=None,
    )


@pytest.mark.parametrize(("endpoint", "expected"), ENDPOINT_CASES, ids=ENDPOINT_CASE_IDS)
def test_openai_client_calls_endpoint_as_is(endpoint, expected):
    settings = _settings(endpoint)
    provider = settings.llm_providers[ENTERPRISE_AI_LLM_PROVIDER]
    assert provider.base_url == endpoint
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"id": "resp-1", "object": "response", "output": []})

    client = oci._openai_client_for_provider(provider, settings).with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.responses.create(model="vendor.text", input="x")

    assert urls == [expected]


def test_base_url_trims_spaces_and_trailing_slash():
    provider = _settings("  https://api.deepseek.com/v1/  ").llm_providers[ENTERPRISE_AI_LLM_PROVIDER]
    assert provider.base_url == "https://api.deepseek.com/v1"


def test_missing_endpoint_is_reported():
    settings = _settings("")
    with pytest.raises(RuntimeError, match="ENDPOINT"):
        oci._validated_llm_provider(settings, ENTERPRISE_AI_LLM_PROVIDER, require_vision=False)
