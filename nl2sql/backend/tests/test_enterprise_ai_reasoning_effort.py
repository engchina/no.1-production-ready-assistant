"""登録モデルごとの推論（思考）の量（#1425）が、NL2SQL の呼び出しの payload に入ること。

HTTP は httpx.MockTransport（決定論）で受ける（実際のモデルの API へは接続しない）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.features.nl2sql.enterprise_ai_client import HttpEnterpriseAiDirectClient
from app.settings import EnterpriseAiConfiguredModel, Settings

ENDPOINT = "https://primary.example/openai/v1"


def _settings(
    text_effort: str = "medium", vision_effort: str = "medium", **overrides: Any
) -> Settings:
    values: dict[str, Any] = {
        "enterprise_ai_endpoint": ENDPOINT,
        "enterprise_ai_api_key": "sk-primary",
        "enterprise_ai_models": [
            EnterpriseAiConfiguredModel.model_validate(
                {"model_id": "text-a", "reasoning_effort": text_effort}
            ),
            EnterpriseAiConfiguredModel.model_validate(
                {"model_id": "vision-b", "vision_enabled": True, "reasoning_effort": vision_effort}
            ),
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


def test_text_and_image_calls_send_each_model_effort(payloads: list[dict[str, Any]]) -> None:
    client = HttpEnterpriseAiDirectClient(_settings("high", "low"))

    client.generate(prompt="質問", context="文脈", system_prompt="指示")
    client.generate_from_image(b"\x89PNG", "読んで")

    assert [payload.get("reasoning") for payload in payloads] == [
        {"effort": "high"},
        {"effort": "low"},
    ]


def test_none_effort_sends_no_reasoning(payloads: list[dict[str, Any]]) -> None:
    """none は「送らない」（`reasoning` を入れない）。"""
    client = HttpEnterpriseAiDirectClient(_settings("none", "none"))

    client.generate(prompt="質問", context="文脈", system_prompt="指示")
    client.generate_from_image(b"\x89PNG", "読んで")

    assert all("reasoning" not in payload for payload in payloads)


def test_payload_template_is_left_to_template(payloads: list[dict[str, Any]]) -> None:
    """payload template の構成では、template が送る形をそのまま使う（`reasoning` を足さない）。"""
    template = json.dumps({"model": "{{model}}", "input": "{{user_message}}"})
    client = HttpEnterpriseAiDirectClient(
        _settings("high", enterprise_ai_llm_payload_template=template)
    )

    client.generate(prompt="質問", context="文脈", system_prompt="指示")

    assert "reasoning" not in payloads[0]


def test_default_effort_is_low(payloads: list[dict[str, Any]]) -> None:
    """値の無いモデル（#1425 より前の設定も）は既定の low を送る（#3004）。"""
    settings = Settings(
        _env_file=None,
        enterprise_ai_endpoint=ENDPOINT,
        enterprise_ai_api_key="sk-primary",
        enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id="text-a")],
        enterprise_ai_default_text_model="text-a",
        enterprise_ai_max_retries=0,
    )

    HttpEnterpriseAiDirectClient(settings).generate(
        prompt="質問", context="文脈", system_prompt="指示"
    )

    assert payloads[0]["reasoning"] == {"effort": "low"}
