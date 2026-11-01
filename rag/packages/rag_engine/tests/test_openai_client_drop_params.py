"""LLM の OpenAI 互換 client は、モデルが受け付けない引数を外して送り直す（#3539）。

回答・監査などの呼び出し（`responses.parse` / `responses.stream`）は `temperature` 0 を送る。サンプリングの
引数を受け付けない推論モデルの 400 では、その引数を外して 1 回だけ送り直し、以後そのモデルには送らない。
HTTP は httpx.MockTransport（決定論。合成の 400 の本文）で受け、OCI へは接続しない。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pr_backend_core.llm_request_params import reset_dropped_parameters
from pr_backend_core.llm_request_params_httpx import ParameterDroppingClient

from rag_engine.adapters import model_api

ENDPOINT = "https://enterprise-ai.example/openai/v1"
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
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
}


@pytest.fixture(autouse=True)
def _fresh_dropped_parameters() -> Iterator[None]:
    reset_dropped_parameters()
    yield
    reset_dropped_parameters()


def test_openai_client_drops_temperature_and_remembers(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(400, json=UNSUPPORTED_TEMPERATURE)
        return httpx.Response(200, json=_RESPONSE)

    monkeypatch.setattr(
        model_api,
        "ParameterDroppingClient",
        lambda **kwargs: ParameterDroppingClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    # provider ごとの cache を通さずに作る（テストの間で client を持ち越さない）。
    client = model_api._openai_client.__wrapped__("provider-a", ENDPOINT, "test-key", "", 30, 0)

    client.responses.create(model=REASONING_MODEL, input="x", temperature=model_api.LLM_TEMPERATURE)
    assert ["temperature" in body for body in bodies] == [True, False]

    client.responses.create(model=REASONING_MODEL, input="x", temperature=model_api.LLM_TEMPERATURE)
    assert len(bodies) == 3
    assert "temperature" not in bodies[2]
