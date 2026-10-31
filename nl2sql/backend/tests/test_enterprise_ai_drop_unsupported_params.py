"""モデルが受け付けない引数を外して送り直す（#3539）。

サンプリングの引数を受け付けない推論モデルは `temperature` を HTTP 400 で拒む。
Enterprise AI Direct はその引数を外して 1 回だけ送り直し、以後そのモデルには送らない。
受け付けるモデルには今までどおり送る。
HTTP は httpx.MockTransport（決定論。合成の 400 の本文）で受け、OCI へは接続しない。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pr_system_settings.model import ModelSettingsTestRequest, run_model_settings_test

from app.features.nl2sql import enterprise_ai_client
from app.features.nl2sql.enterprise_ai_client import (
    EnterpriseAiDirectError,
    OciEnterpriseAiDirectClient,
)
from app.features.settings import router as settings_router
from app.settings import EnterpriseAiConfiguredModel, Settings

ENDPOINT = "https://primary.example/openai/v1"
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


def _settings(default_model: str = REASONING_MODEL) -> Settings:
    return Settings(
        _env_file=None,
        oci_enterprise_ai_endpoint=ENDPOINT,
        oci_enterprise_ai_api_key="sk-primary",
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id=REASONING_MODEL),
            EnterpriseAiConfiguredModel(model_id=SAMPLING_MODEL),
        ],
        oci_enterprise_ai_default_text_model=default_model,
        oci_enterprise_ai_max_retries=0,
    )


@pytest.fixture
def payloads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """推論モデルだけ temperature を 400 で拒む合成の gateway。送った payload を記録する。"""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body.get("model") == REASONING_MODEL and "temperature" in body:
            return httpx.Response(400, json=UNSUPPORTED_TEMPERATURE)
        if "tools" in body:
            error = {"message": "Unsupported parameter: 'tools'.", "param": "tools"}
            return httpx.Response(400, json={"error": error})
        return httpx.Response(200, json={"output_text": json.dumps({"text": "ok"})})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return sent


def _generate(settings: Settings) -> str:
    return OciEnterpriseAiDirectClient(settings).generate(
        prompt="質問", context="文脈", system_prompt="指示"
    )


def test_drops_temperature_once_and_remembers(payloads: list[dict[str, Any]]) -> None:
    assert _generate(_settings())
    assert ["temperature" in payload for payload in payloads] == [True, False]

    # 2 回目は送り直さずに最初から外す
    assert _generate(_settings())
    assert len(payloads) == 3
    assert "temperature" not in payloads[2]
    assert payloads[2]["max_output_tokens"] > 0


def test_keeps_temperature_for_models_that_accept_it(payloads: list[dict[str, Any]]) -> None:
    _generate(_settings())
    _generate(_settings(SAMPLING_MODEL))
    assert payloads[-1]["model"] == SAMPLING_MODEL
    assert payloads[-1]["temperature"] == 0


def test_non_allow_listed_parameter_still_fails(
    payloads: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """許可の一覧に無い引数（tools）の 400 は今までどおり失敗にし、送り直さない。"""
    original = enterprise_ai_client._build_payload
    monkeypatch.setattr(
        enterprise_ai_client,
        "_build_payload",
        lambda **kwargs: {**original(**kwargs), "tools": []},
    )
    with pytest.raises(EnterpriseAiDirectError, match="HTTP 400"):
        _generate(_settings(SAMPLING_MODEL))
    assert len(payloads) == 1


def test_model_test_reports_dropped_parameter(payloads: list[dict[str, Any]]) -> None:
    request = ModelSettingsTestRequest.model_validate(
        {
            "target_type": "enterprise_text",
            "model_id": REASONING_MODEL,
            "settings": {
                "enterprise_ai": {
                    "endpoint": ENDPOINT,
                    "api_key": "sk-primary",
                    "models": [{"model_id": REASONING_MODEL, "display_name": "A"}],
                    "default_text_model_id": REASONING_MODEL,
                    "max_retries": 0,
                },
                "generative_ai": {
                    "embedding_model": "cohere.embed-v4.0",
                    "rerank_model": "cohere.rerank-v4.0-fast",
                },
            },
        }
    )
    result = asyncio.run(
        run_model_settings_test(_settings(), request, settings_router._run_model_settings_test)
    )
    assert result.status == "success", result.raw_error
    assert result.details["dropped_parameters"] == "temperature"
    assert "受け付けない引数（temperature）は外して送りました" in result.message
