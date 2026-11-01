"""登録モデルごとの推論（思考）の量（#1425）が、組み込み Runtime と接続テストに渡ること。

モデルの API へは接続しない（モデルは差し替え、HTTP は httpx.MockTransport）。
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest
from agents.testing import ScriptedModel
from pr_system_settings.model import (
    EnterpriseAiConfiguredModel,
    ModelSettingsTestRequest,
    model_test_candidate,
)

import app.features.agent.router as agent_router
from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.settings import Settings

ENDPOINT = "https://primary.example/openai/v1"


def _target(effort: str) -> ModelTarget:
    return ModelTarget(
        model_id="m", endpoint=ENDPOINT, project_id="", api_key="k", reasoning_effort=effort
    )


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("low", "low"), ("medium", "medium"), ("none", None)],
    ids=["low", "medium", "none"],
)
def test_model_settings_send_effort_only_when_set(effort: str, expected: str | None) -> None:
    settings = builtin_runtime.model_settings_for(_target(effort))

    assert settings.store is False
    assert (settings.reasoning.effort if settings.reasoning else None) == expected


def test_resolve_model_target_reads_effort_of_agent_model(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        enterprise_ai_endpoint=ENDPOINT,
        enterprise_ai_api_key="sk-test",
        enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="text-a", reasoning_effort="none"),
            EnterpriseAiConfiguredModel(model_id="text-b"),
        ],
        enterprise_ai_default_text_model="text-a",
    )
    monkeypatch.setattr(builtin_runtime, "get_settings", lambda: settings)

    assert builtin_runtime.resolve_model_target("").reasoning_effort == "none"
    # 値を書かないモデルは既定の low。
    assert builtin_runtime.resolve_model_target("text-b").reasoning_effort == "low"


def test_sdk_agent_uses_model_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        builtin_runtime, "resolve_model_target", lambda _model_id="": _target("high")
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: ScriptedModel([]))

    agent = builtin_runtime.build_sdk_agent(
        "run-reasoning", name="a", instructions="指示", skill_ids=[], model_id="m"
    )

    assert agent.model_settings.reasoning is not None
    assert agent.model_settings.reasoning.effort == "high"


def _payload(effort: str) -> dict[str, Any]:
    return {
        "enterprise_ai": {
            "connections": [{"connection_id": "primary", "endpoint": ENDPOINT, "api_key": "sk"}],
            "models": [
                {"model_id": "text-a", "reasoning_effort": effort},
                {"model_id": "vision-b", "vision_enabled": True, "reasoning_effort": "low"},
            ],
            "default_text_model_id": "text-a",
            "default_vision_model_id": "vision-b",
            "text_response_path": "/output_text",
            "vision_response_path": "/output_text",
        },
    }


@pytest.mark.parametrize(
    ("target_type", "model_id", "effort", "expected"),
    [
        ("enterprise_text", "text-a", "high", {"effort": "high"}),
        ("enterprise_text", "text-a", "none", None),
        ("enterprise_vision", "vision-b", "none", {"effort": "low"}),
    ],
    ids=["text-high", "text-none", "vision-low"],
)
def test_model_test_sends_unsaved_effort(
    monkeypatch: pytest.MonkeyPatch,
    target_type: str,
    model_id: str,
    effort: str,
    expected: dict[str, str] | None,
) -> None:
    """「テスト」は保存前に選んだ推論の量で呼ぶ（モデルが受け付けるかを保存の前に確かめる）。"""
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
        {"settings": _payload(effort), "target_type": target_type, "model_id": model_id}
    )
    candidate = model_test_candidate(Settings(_env_file=None), request)

    anyio.run(agent_router._run_model_settings_test, candidate, request)

    assert sent[0]["model"] == model_id
    assert sent[0].get("reasoning") == expected


def test_model_test_shows_rejection_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """受け付けない値の 400 は、本文（理由）を結果の詳細に出す。"""
    reason = "unknown variant `none`, expected one of `minimal`, `low`, `medium`, `high`"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": reason}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    request = ModelSettingsTestRequest.model_validate(
        {"settings": _payload("low"), "target_type": "enterprise_text", "model_id": "text-a"}
    )
    candidate = model_test_candidate(Settings(_env_file=None), request)

    with pytest.raises(ValueError, match="HTTP 400") as raised:
        anyio.run(agent_router._run_model_settings_test, candidate, request)
    assert reason in str(raised.value)


def test_rag_mcp_timeout_default_outlasts_rag_answer_timeout() -> None:
    """`rag_search` は RAG の回答生成（既定 600 秒）を通しで待つので、既定はそれより長い。"""
    assert Settings(_env_file=None).agent_external_rag_timeout_seconds == 630.0
