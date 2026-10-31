"""NL2SQL は seed を送らない（#3529）。

- Enterprise AI Direct: テキストと画像の呼び出しの payload に `seed` を入れない
- Select AI / Select AI Agent: AI profile に `seed` 属性を入れない（保存済みの業務プロファイルの
  `seed` は読み捨てる）

HTTP は httpx.MockTransport（決定論）で受け、Oracle へは接続しない。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.features.nl2sql.enterprise_ai_client import OciEnterpriseAiDirectClient
from app.features.nl2sql.models import Nl2SqlProfile, ProfilePatchRequest, ProfileSelectAiConfig
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import EnterpriseAiConfiguredModel, Settings

ENDPOINT = "https://primary.example/openai/v1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "oci_enterprise_ai_endpoint": ENDPOINT,
        "oci_enterprise_ai_api_key": "sk-primary",
        "oci_enterprise_ai_models": [
            EnterpriseAiConfiguredModel(model_id="text-a"),
            EnterpriseAiConfiguredModel(model_id="vision-b", vision_enabled=True),
        ],
        "oci_enterprise_ai_default_text_model": "text-a",
        "oci_enterprise_ai_default_vision_model": "vision-b",
        "oci_enterprise_ai_max_retries": 0,
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
    client = OciEnterpriseAiDirectClient(_settings())

    client.generate(prompt="質問", context="文脈", system_prompt="指示")
    client.generate_from_image(b"\x89PNG", "読んで")

    assert len(payloads) == 2
    assert all("seed" not in payload for payload in payloads)


def test_select_ai_profile_attributes_have_no_seed() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    profile = Nl2SqlProfile(id="profile-a", name="プロファイル A")

    assert "seed" not in service.build_select_ai_profile_attributes(profile)


def test_adapter_profile_attributes_have_no_seed() -> None:
    """Select AI Agent の assets の profile の属性（adapter が組み立てる）にも seed が無い。"""
    adapter = OracleNl2SqlAdapter(_settings())

    attributes = adapter._select_ai_profile_attributes(  # noqa: SLF001
        allowed_tables=["SALES"], row_limit=None, description=""
    )

    assert "seed" not in attributes


def test_stored_seed_is_ignored() -> None:
    """保存済みの業務プロファイル・PATCH の `seed` は読み捨てる。"""
    config = ProfileSelectAiConfig.model_validate({"seed": 42})
    assert "seed" not in config.model_dump()
    patch = ProfilePatchRequest.model_validate({"select_ai_config": {"seed": 7}})
    assert patch.select_ai_config is not None
    assert "seed" not in patch.select_ai_config.model_dump()
