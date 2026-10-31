"""モデルの接続テストが、テストするモデルの接続で呼ぶこと（#533 / #786）。

HTTP は httpx.MockTransport（決定論）で受け、URL・API key・Project のヘッダーを確かめる。
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest
from pr_system_settings.model import ModelSettingsTestRequest, model_test_candidate

import app.features.agent.router as agent_router
from app.settings import Settings

PRIMARY = "https://primary.example/openai/v1"
SECONDARY = "https://secondary.example/openai/v1"
# ターシャリ接続（#786）は OpenAI / OpenAI 互換 API 向けで、Project OCID を入れない。
TERTIARY = "https://api.openai.example/v1"


def _payload() -> dict[str, Any]:
    return {
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": PRIMARY,
                    "project_ocid": "ocid1.project.primary",
                    "api_key": "sk-primary",
                },
                {
                    "connection_id": "secondary",
                    "endpoint": SECONDARY,
                    "project_ocid": "ocid1.project.secondary",
                    "api_key": "",
                    "has_api_key": True,
                },
                {
                    "connection_id": "tertiary",
                    "endpoint": TERTIARY,
                    "project_ocid": "",
                    "api_key": "sk-tertiary",
                },
            ],
            "models": [
                {"model_id": "text-a"},
                {"model_id": "vision-b", "vision_enabled": True, "connection_id": "secondary"},
                {"model_id": "gpt-c", "connection_id": "tertiary"},
            ],
            "default_text_model_id": "text-a",
            "default_vision_model_id": "vision-b",
            "text_response_path": "/output_text",
            "vision_response_path": "/output_text",
        },
        "generative_ai": {
            "embedding_model": "cohere.embed-v4.0",
            "embedding_dim": 1536,
            "rerank_model": "cohere.rerank-v4.0-fast",
        },
    }


@pytest.mark.parametrize(
    ("target_type", "model_id", "expected"),
    [
        (
            "enterprise_text",
            "text-a",
            (PRIMARY, "Bearer sk-primary", "ocid1.project.primary"),
        ),
        (
            "enterprise_vision",
            "vision-b",
            (SECONDARY, "Bearer sk-saved-secondary", "ocid1.project.secondary"),
        ),
        # Project OCID のないターシャリ接続は OpenAI-Project ヘッダーを送らない（#786）。
        ("enterprise_text", "gpt-c", (TERTIARY, "Bearer sk-tertiary", "")),
    ],
    ids=["text-primary", "vision-secondary", "text-tertiary"],
)
def test_model_test_uses_connection_of_tested_model(
    monkeypatch: pytest.MonkeyPatch,
    target_type: str,
    model_id: str,
    expected: tuple[str, str, str],
) -> None:
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        base = next(item for item in (PRIMARY, SECONDARY, TERTIARY) if url.startswith(item))
        assert url == f"{base}/responses"
        calls.append(
            (base, request.headers["Authorization"], request.headers.get("OpenAI-Project", ""))
        )
        return httpx.Response(200, json={"output_text": "ok"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    # 保存済みのセカンダリ接続の key（画面では空欄 = 保持）を使う。
    saved = Settings(_env_file=None, oci_enterprise_ai_secondary_api_key="sk-saved-secondary")
    request = ModelSettingsTestRequest.model_validate(
        {"settings": _payload(), "target_type": target_type, "model_id": model_id}
    )
    candidate = model_test_candidate(saved, request)

    details = anyio.run(agent_router._run_model_settings_test, candidate, request)

    assert details["response_chars"] == 2
    assert calls == [expected]
