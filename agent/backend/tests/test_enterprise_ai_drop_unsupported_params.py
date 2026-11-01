"""モデルが受け付けない引数を外して送り直す（#3539）。

サンプリングの引数を受け付けない推論モデルは `temperature` を HTTP 400 で拒む。
組み込み Runtime の SDK の client とモデルの接続テストは、その引数を外して 1 回だけ送り直し、
以後そのモデルには送らない。
モデルの API へは接続しない（HTTP は httpx / httpx2 の MockTransport。合成の 400 の本文）。
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import httpx2
import pytest
from pr_system_settings.model import (
    EnterpriseAiConfiguredModel,
    ModelSettingsTestRequest,
    run_model_settings_test,
)

import app.features.agent.router as agent_router
from app.features.agent import builtin_runtime
from app.settings import Settings

ENDPOINT = "https://gateway.example.test/v1"
REASONING_MODEL = "reasoning-a"
UNSUPPORTED_TEMPERATURE = {
    "error": {
        "message": "Unsupported parameter: 'temperature' is not supported with this model.",
        "type": "invalid_request_error",
        "param": "temperature",
        "code": None,
    }
}
_RESPONSE: dict[str, Any] = {
    "id": "resp-1",
    "object": "response",
    "created_at": 0,
    "model": REASONING_MODEL,
    "status": "completed",
    "output": [],
    "output_text": "ok",
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
}


def _reply(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if body.get("model") == REASONING_MODEL and "temperature" in body:
        return 400, UNSUPPORTED_TEMPERATURE
    return 200, _RESPONSE


def test_runtime_client_drops_temperature_and_remembers(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        status, payload = _reply(body)
        return httpx2.Response(status, json=payload)

    settings = Settings(
        _env_file=None,
        enterprise_ai_endpoint=ENDPOINT,
        enterprise_ai_api_key="sk-test",
        enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id=REASONING_MODEL)],
        enterprise_ai_default_text_model=REASONING_MODEL,
    )
    monkeypatch.setattr(builtin_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(
        builtin_runtime,
        "enterprise_ai_http_client",
        lambda timeout=None: builtin_runtime._ParameterDroppingHttpClient(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        ),
    )

    target = builtin_runtime.resolve_model_target("")
    model: Any = builtin_runtime._default_model_factory(target)  # noqa: SLF001
    client: Any = model._get_client()  # noqa: SLF001 - SDK が使う client で呼ぶ

    async def call() -> None:
        await client.responses.create(model=REASONING_MODEL, input="x", temperature=0)

    anyio.run(call)
    assert ["temperature" in body for body in bodies] == [True, False]

    # 2 回目は送り直さずに最初から外す
    anyio.run(call)
    assert len(bodies) == 3
    assert "temperature" not in bodies[2]


def test_model_test_reports_dropped_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        status, payload = _reply(body)
        return httpx.Response(status, json=payload)

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
                    "endpoint": ENDPOINT,
                    "project_id": "",
                    "api_key": "sk-test",
                }
            ],
            "models": [{"model_id": REASONING_MODEL}],
            "default_text_model_id": REASONING_MODEL,
            "default_vision_model_id": "",
            "text_response_path": "/output_text",
        },
    }
    request = ModelSettingsTestRequest.model_validate(
        {"settings": payload, "target_type": "enterprise_text", "model_id": REASONING_MODEL}
    )

    result = anyio.run(
        run_model_settings_test,
        Settings(_env_file=None),
        request,
        agent_router._run_model_settings_test,  # noqa: SLF001
    )

    assert result.status == "success", result.raw_error
    assert ["temperature" in body for body in bodies] == [True, False]
    assert result.details["dropped_parameters"] == "temperature"
    assert "受け付けない引数（temperature）は外して送りました" in result.message
