"""NL2SQL は seed を送らない（#3529）。

- Enterprise AI Direct: テキストと画像の呼び出しの payload に `seed` を入れない
- 保存済みの業務プロファイル・PATCH の `seed` は読み捨てる

HTTP は httpx.MockTransport（決定論）で受け、Oracle へは接続しない。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.features.nl2sql.enterprise_ai_client import HttpEnterpriseAiDirectClient
from app.features.nl2sql.models import Nl2SqlProfile, ProfilePatchRequest
from app.settings import EnterpriseAiConfiguredModel, Settings

ENDPOINT = "https://primary.example/openai/v1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "enterprise_ai_endpoint": ENDPOINT,
        "enterprise_ai_api_key": "sk-primary",
        "enterprise_ai_models": [
            EnterpriseAiConfiguredModel(model_id="text-a"),
            EnterpriseAiConfiguredModel(model_id="vision-b", vision_enabled=True),
        ],
        "enterprise_ai_default_text_model": "text-a",
        "enterprise_ai_default_vision_model": "vision-b",
        "enterprise_ai_max_retries": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def payloads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """同期の httpx.Client を MockTransport に差し替え、送った payload を記録する。"""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": json.dumps({"text": "ok"})})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return sent


def test_text_and_image_calls_do_not_send_seed(payloads: list[dict[str, Any]]) -> None:
    client = HttpEnterpriseAiDirectClient(_settings())

    client.generate(prompt="質問", context="文脈", system_prompt="指示")
    client.generate_from_image(b"\x89PNG", "読んで")

    assert len(payloads) == 2
    assert all("seed" not in payload for payload in payloads)


def test_stored_seed_is_ignored() -> None:
    """保存済みの業務プロファイル・PATCH の `seed` は読み捨てる。"""
    profile = Nl2SqlProfile.model_validate({"id": "profile-a", "name": "A", "seed": 42})
    assert "seed" not in profile.model_dump()
    patch = ProfilePatchRequest.model_validate({"seed": 7})
    assert "seed" not in patch.model_dump()
