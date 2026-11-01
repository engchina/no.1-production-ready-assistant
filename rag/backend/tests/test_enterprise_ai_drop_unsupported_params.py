"""モデルが受け付けない引数を外して送り直す（#3539）。

サンプリングの引数を受け付けない推論モデルは `temperature` を HTTP 400 で拒む。
共有の Enterprise AI の client（openai SDK の transport）とモデルの接続テストは、その引数を
外して 1 回だけ送り直し、以後そのモデルには送らない。受け付けるモデルには temperature 0 を
送り続ける。
OCI へは接続しない（HTTP は httpx.MockTransport。合成の 400 の本文）。
"""

import json
from typing import Any

import httpx
import pytest
from pr_backend_core.llm_request_params_httpx import AsyncParameterDroppingClient
from pr_system_settings.model import ModelSettingsTestRequest, run_model_settings_test
from rag_parser_core import enterprise_ai as shared_client
from rag_parser_core.enterprise_ai import EnterpriseAiClient, EnterpriseAiConfig

from app.api.routes import settings as settings_routes
from app.config import get_settings

ENDPOINT = "https://enterprise-ai.example/openai/v1"
REASONING_MODEL = "reasoning-a"
SAMPLING_MODEL = "sampling-b"
UNSUPPORTED_TEMPERATURE = {
    "error": {
        "message": "Unsupported parameter: 'temperature' is not supported with this model.",
        "type": "invalid_request_error",
        "param": "temperature",
        "code": None,
    }
}


@pytest.fixture
def bodies(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """推論モデルだけ temperature を 400 で拒む合成の gateway。送った本文を記録する。"""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body.get("model") == REASONING_MODEL and "temperature" in body:
            return httpx.Response(400, json=UNSUPPORTED_TEMPERATURE)
        if "tools" in body:
            error = {"message": "Unsupported parameter: 'tools'.", "param": "tools"}
            return httpx.Response(400, json={"error": error})
        return httpx.Response(200, json={"output_text": "回答です"})

    monkeypatch.setattr(
        shared_client,
        "AsyncParameterDroppingClient",
        lambda **kwargs: AsyncParameterDroppingClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    return sent


def _client(model_id: str) -> EnterpriseAiClient:
    return EnterpriseAiClient(
        EnterpriseAiConfig(
            enterprise_ai_endpoint=ENDPOINT,
            enterprise_ai_api_key="test-key",
            default_model_id=model_id,
            enterprise_ai_max_retries=0,
        )
    )


async def test_drops_temperature_once_and_remembers(bodies: list[dict[str, Any]]) -> None:
    assert await _client(REASONING_MODEL).generate("質問", "根拠") == "回答です"
    assert ["temperature" in body for body in bodies] == [True, False]

    # 2 回目は送り直さずに最初から外す
    assert await _client(REASONING_MODEL).generate("質問", "根拠") == "回答です"
    assert len(bodies) == 3
    assert "temperature" not in bodies[2]


async def test_keeps_temperature_for_models_that_accept_it(
    bodies: list[dict[str, Any]],
) -> None:
    await _client(REASONING_MODEL).generate("質問", "根拠")
    await _client(SAMPLING_MODEL).generate("質問", "根拠")
    assert bodies[-1]["model"] == SAMPLING_MODEL
    assert bodies[-1]["temperature"] == 0.0


async def test_non_allow_listed_parameter_still_fails(
    bodies: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """許可の一覧に無い引数（tools）の 400 は今までどおり失敗にし、送り直さない。"""
    original = shared_client._build_llm_payload
    monkeypatch.setattr(
        shared_client,
        "_build_llm_payload",
        lambda *args, **kwargs: {**original(*args, **kwargs), "tools": []},
    )
    with pytest.raises(httpx.HTTPStatusError, match="tools"):
        await _client(SAMPLING_MODEL).generate("質問", "根拠")
    assert len(bodies) == 1


async def test_model_test_reports_dropped_parameter(bodies: list[dict[str, Any]]) -> None:
    request = ModelSettingsTestRequest.model_validate(
        {
            "target_type": "enterprise_text",
            "model_id": REASONING_MODEL,
            "settings": {
                "enterprise_ai": {
                    "endpoint": ENDPOINT,
                    "api_key": "test-key",
                    "models": [{"model_id": REASONING_MODEL, "display_name": "A"}],
                    "default_text_model_id": REASONING_MODEL,
                    "max_retries": 0,
                },
            },
        }
    )
    result = await run_model_settings_test(
        get_settings(), request, settings_routes._run_model_settings_test
    )
    assert result.status == "success", result.raw_error
    assert ["temperature" in body for body in bodies] == [True, False]
    assert result.details["dropped_parameters"] == "temperature"
    assert "受け付けない引数（temperature）は外して送りました" in result.message
