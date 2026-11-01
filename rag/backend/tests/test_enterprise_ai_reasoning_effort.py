"""登録モデルごとの推論（思考）の量（#1425）が、RAG の各呼び出しの payload に入ること。

HTTP は決定論の fake transport で受ける（モデルの API へは接続しない）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from rag_engine.adapters import model_api as engine_api
from rag_engine.config import get_settings as engine_get_settings
from rag_engine.models.llm import TextOutput
from rag_parser_core.enterprise_ai import EnterpriseAiConfig

from app.clients.enterprise_ai import EnterpriseAiClient, config_from_settings
from app.config import EnterpriseAiConfiguredModel, Settings
from app.rag.answer_engine import build_engine_settings
from app.services.control import SERVICE_RUNTIME_ENV_KEYS, service_runtime_env
from tests.test_enterprise_ai import FakeEnterpriseAiTransport, _llm_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _settings(text_effort: str = "medium", vision_effort: str = "medium") -> Settings:
    settings = _llm_settings()
    settings.enterprise_ai_models = [
        EnterpriseAiConfiguredModel.model_validate(
            {"model_id": "enterprise-llm", "reasoning_effort": text_effort}
        ),
        EnterpriseAiConfiguredModel.model_validate(
            {
                "model_id": "enterprise-vlm",
                "vision_enabled": True,
                "reasoning_effort": vision_effort,
            }
        ),
    ]
    return settings


async def test_generate_sends_reasoning_of_text_model() -> None:
    transport = FakeEnterpriseAiTransport({"output_text": "回答"})
    client = EnterpriseAiClient(settings=_settings("low", "high"), http_transport=transport)

    await client.generate("質問", "根拠")

    assert transport.calls[0]["payload"]["reasoning"] == {"effort": "low"}


async def test_generate_default_sends_low() -> None:
    """値の無いモデル（#1425 より前の設定も）は既定の low を送る（#3004）。"""
    transport = FakeEnterpriseAiTransport({"output_text": "回答"})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    await client.generate("質問", "根拠")

    assert transport.calls[0]["payload"]["reasoning"] == {"effort": "low"}


async def test_generate_with_none_sends_no_reasoning() -> None:
    """none は「送らない」（`reasoning` を入れない）。"""
    transport = FakeEnterpriseAiTransport({"output_text": "回答"})
    client = EnterpriseAiClient(settings=_settings("none", "high"), http_transport=transport)

    await client.generate("質問", "根拠")

    assert "reasoning" not in transport.calls[0]["payload"]


async def test_compared_model_uses_its_own_effort() -> None:
    """チャットのモデル比較（`model_id`）は、そのモデルの値を使う。"""
    settings = _settings("none", "none")
    settings.enterprise_ai_models.append(
        EnterpriseAiConfiguredModel(model_id="other-llm", reasoning_effort="low")
    )
    transport = FakeEnterpriseAiTransport({"output_text": "回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport, model_id="other-llm")

    await client.generate("質問", "根拠")

    assert transport.calls[0]["payload"]["reasoning"] == {"effort": "low"}


async def test_image_calls_send_reasoning_of_vision_model() -> None:
    transport = FakeEnterpriseAiTransport({"output_text": "画像の説明"})
    client = EnterpriseAiClient(settings=_settings("none", "high"), http_transport=transport)

    await client.generate_from_image(PNG, "説明して", mime_type="image/png")
    await client.generate_from_images([PNG], "説明して", mime_type="image/png")

    assert [call["payload"]["reasoning"] for call in transport.calls] == [
        {"effort": "high"},
        {"effort": "high"},
    ]


def test_config_carries_effort_of_each_model() -> None:
    config = config_from_settings(_settings("low", "medium"))

    assert (config.text_reasoning_effort, config.vision_reasoning_effort) == ("low", "medium")
    assert config.for_vision().vision_reasoning_effort == "medium"
    # none は送る値が空（送らない）。
    assert config_from_settings(_settings("none", "none")).text_reasoning_effort == ""


def test_parser_service_env_carries_effort() -> None:
    """parser（VLM）は env から読むので、backend が値を書く。"""
    env = service_runtime_env(_settings("low", "high"))

    assert env["PLATFORM_ENTERPRISE_AI_TEXT_REASONING_EFFORT"] == "low"
    assert env["PLATFORM_ENTERPRISE_AI_VISION_REASONING_EFFORT"] == "high"
    assert set(env) <= set(SERVICE_RUNTIME_ENV_KEYS)
    config = EnterpriseAiConfig.from_env(env)
    assert (config.text_reasoning_effort, config.vision_reasoning_effort) == ("low", "high")


@pytest.mark.parametrize(
    ("text_effort", "vision_effort", "expected"),
    [("low", "high", ("low", "high")), ("none", "none", ("", ""))],
    ids=["set", "none"],
)
def test_answer_engine_provider_carries_effort(
    tmp_path: Path, text_effort: str, vision_effort: str, expected: tuple[str, str]
) -> None:
    engine = build_engine_settings(_settings(text_effort, vision_effort), output_dir=tmp_path)

    providers = engine.llm_providers
    assert (
        providers[engine.default_answer_llm].reasoning_effort,
        providers[engine.default_vision_llm].reasoning_effort,
    ) == expected


class _Responses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(status="completed", output_parsed=TextOutput(text="ok"))


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("low", {"effort": "low"}), ("", None)],
    ids=["set", "empty"],
)
def test_engine_parse_sends_reasoning(effort: str, expected: dict[str, str] | None) -> None:
    """rag_engine の Responses API（`responses.parse`）にも、モデルの値を渡す。"""
    responses = _Responses()
    client = SimpleNamespace(responses=responses)
    provider = SimpleNamespace(model="m", label="m", reasoning_effort=effort)
    settings = engine_get_settings(environ={}, dotenv_path=None)

    with mock.patch.object(engine_api, "_openai_client_for_provider", return_value=client):
        engine_api._openai_parsed([], settings, TextOutput, provider)  # type: ignore[arg-type]

    assert responses.calls[0].get("reasoning") == expected
