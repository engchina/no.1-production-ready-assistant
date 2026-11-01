"""RAG の Enterprise AI の呼び出しは seed を送らない（#3529）。

seed を受け付けないモデルでは未知の引数として HTTP 400 になるため、標準の payload・回答フローの
`responses.parse`・parser のサービスへ渡す env のどれにも seed を入れない。
HTTP は決定論の fake transport で受ける（モデルの API へは接続しない）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

from rag_engine.adapters import model_api as engine_api
from rag_engine.config import get_settings as engine_get_settings
from rag_engine.models.llm import TextOutput
from rag_parser_core.enterprise_ai import EnterpriseAiConfig

from app.clients.enterprise_ai import EnterpriseAiClient, config_from_settings
from app.services.control import SERVICE_RUNTIME_ENV_KEYS, service_runtime_env
from tests.test_enterprise_ai import FakeEnterpriseAiTransport, _llm_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


async def test_text_and_image_calls_do_not_send_seed() -> None:
    transport = FakeEnterpriseAiTransport({"output_text": "回答"})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    await client.generate("質問", "根拠")
    await client.generate_from_image(PNG, "説明して", mime_type="image/png")
    await client.generate_from_images([PNG], "説明して", mime_type="image/png")

    assert len(transport.calls) == 3
    assert all("seed" not in call["payload"] for call in transport.calls)


def test_config_and_parser_env_have_no_seed() -> None:
    """共有 client の config と、parser（VLM。env から読む）の env に seed が無い。"""
    assert not hasattr(config_from_settings(_llm_settings()), "seed")
    assert not hasattr(EnterpriseAiConfig.from_env({}), "seed")
    env = service_runtime_env(_llm_settings())
    assert not any("SEED" in key for key in env)
    assert not any("SEED" in key for key in SERVICE_RUNTIME_ENV_KEYS)


class _Responses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(status="completed", output_parsed=TextOutput(text="ok"))


def test_engine_parse_does_not_send_seed() -> None:
    """rag_engine の Responses API（`responses.parse`）は seed を送らない。"""
    responses = _Responses()
    client = SimpleNamespace(responses=responses)
    provider = SimpleNamespace(model="m", label="m", reasoning_effort="")
    settings = engine_get_settings(environ={}, dotenv_path=None)

    with mock.patch.object(engine_api, "_openai_client_for_provider", return_value=client):
        engine_api._openai_parsed([], settings, TextOutput, provider)  # type: ignore[arg-type]

    assert "seed" not in responses.calls[0]
    assert "seed" not in responses.calls[0].get("extra_body", {})
