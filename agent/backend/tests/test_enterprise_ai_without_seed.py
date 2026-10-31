"""Agent の LLM の呼び出しは seed を送らない（#3529）。

組み込み Runtime の SDK の ModelSettings と、モデルの接続テストの payload に seed を入れない。
OCI へは接続しない（モデルは差し替え、HTTP は httpx.MockTransport）。
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest
from agents.testing import ScriptedModel
from pr_system_settings.model import ModelSettingsTestRequest, model_test_candidate

import app.features.agent.router as agent_router
from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.settings import Settings

ENDPOINT = "https://primary.example/openai/v1"


def _target(*, effort: str = "medium") -> ModelTarget:
    return ModelTarget(
        model_id="m",
        endpoint=ENDPOINT,
        project_ocid="",
        api_key="k",
        reasoning_effort=effort,
    )


@pytest.mark.parametrize("effort", ["medium", "none"], ids=["with-reasoning", "no-reasoning"])
def test_model_settings_do_not_send_seed(effort: str) -> None:
    """推論の量の有無にかかわらず、SDK の extra_body（top-level の `seed`）を付けない。"""
    settings = builtin_runtime.model_settings_for(_target(effort=effort))
    assert not (settings.extra_body or {})


def test_sdk_agent_does_not_send_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin_runtime, "resolve_model_target", lambda _model_id="": _target())
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: ScriptedModel([]))

    agent = builtin_runtime.build_sdk_agent(
        "run-a", name="a", instructions="指示", skill_ids=[], model_id="m"
    )

    assert not (agent.model_settings.extra_body or {})


def _payload() -> dict[str, Any]:
    enterprise: dict[str, Any] = {
        "connections": [{"connection_id": "primary", "endpoint": ENDPOINT, "api_key": "sk"}],
        "models": [
            {"model_id": "text-a"},
            {"model_id": "vision-b", "vision_enabled": True},
        ],
        "default_text_model_id": "text-a",
        "default_vision_model_id": "vision-b",
        "text_response_path": "/output_text",
        "vision_response_path": "/output_text",
        # 廃止した項目は読み捨てる。
        "seed": 7,
    }
    return {"enterprise_ai": enterprise}


@pytest.mark.parametrize(
    ("target_type", "model_id"),
    [("enterprise_text", "text-a"), ("enterprise_vision", "vision-b")],
    ids=["text", "vision"],
)
def test_model_test_does_not_send_seed(
    monkeypatch: pytest.MonkeyPatch, target_type: str, model_id: str
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": "ok"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    request = ModelSettingsTestRequest.model_validate(
        {"settings": _payload(), "target_type": target_type, "model_id": model_id}
    )
    candidate = model_test_candidate(Settings(_env_file=None), request)

    anyio.run(agent_router._run_model_settings_test, candidate, request)

    assert sent[0]["model"] == model_id
    assert "seed" not in sent[0]
