"""埋め込み / 再ランクのモデルを登録モデル（種類 embedding / rerank）として扱う（#45）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from test_model import FakeSettings, make_client, make_store

from pr_system_settings.model import (
    ENTERPRISE_AI_API_KEY_ENV,
    ENTERPRISE_AI_SECONDARY_API_KEY_ENV,
    ENTERPRISE_AI_TERTIARY_API_KEY_ENV,
    ModelSettingsTestRequest,
    embedding_api_base_url,
    embedding_model_connection,
    enterprise_ai_model_catalog,
    model_test_candidate,
    rerank_base_url,
)

BAILIAN = "https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@pytest.fixture(autouse=True)
def _no_process_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENTERPRISE_AI_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ENTERPRISE_AI_SECONDARY_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ENTERPRISE_AI_TERTIARY_API_KEY_ENV, raising=False)


def payload(**enterprise: Any) -> dict[str, Any]:
    """プライマリ = DeepSeek（LLM）、セカンダリ = 百煉（埋め込み / 再ランク）。"""
    return {
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": "https://api.deepseek.com",
                    "api_key": "sk-ds",
                },
                {"connection_id": "secondary", "endpoint": BAILIAN, "api_key": "sk-ali"},
            ],
            "models": [
                {"model_id": "deepseek-flash", "vision_enabled": True},
                {
                    "model_id": "text-embedding-v4",
                    "kind": "embedding",
                    "connection_id": "secondary",
                },
                {"model_id": "qwen3-rerank", "kind": "rerank", "connection_id": "secondary"},
            ],
            "default_text_model_id": "deepseek-flash",
            "default_vision_model_id": "deepseek-flash",
            "default_embedding_model_id": "text-embedding-v4",
            "default_rerank_model_id": "qwen3-rerank",
            **enterprise,
        }
    }


def test_embedding_and_rerank_use_their_model_connection(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)

    response = client.patch("/api/settings/model", json=payload())

    assert response.status_code == 200, response.text
    assert settings.genai_embedding_model == "text-embedding-v4"
    assert settings.genai_rerank_model == "qwen3-rerank"
    assert embedding_model_connection(settings).api_key == "sk-ali"
    assert embedding_model_connection(settings, rerank=True).api_key == "sk-ali"
    assert embedding_api_base_url(settings) == BAILIAN
    assert embedding_api_base_url(settings, rerank=True) == BAILIAN.replace(
        "/compatible-mode/", "/compatible-api/"
    )
    # LLM の選択肢・接続は種類 text だけ（埋め込み / 再ランクは混ざらない）。
    assert [model.model_id for model in enterprise_ai_model_catalog(settings)] == ["deepseek-flash"]
    data = client.get("/api/settings/model").json()["data"]["settings"]["enterprise_ai"]
    assert [model["kind"] for model in data["models"]] == ["text", "embedding", "rerank"]
    assert data["default_embedding_model_id"] == "text-embedding-v4"
    assert data["default_rerank_model_id"] == "qwen3-rerank"
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert "generative_ai" not in document
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_SECONDARY_API_KEY_ENV] == "sk-ali"


def test_rerank_url_without_compatible_mode_is_kept() -> None:
    assert rerank_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"


def test_unregistered_embedding_model_uses_primary_connection() -> None:
    settings = FakeSettings(
        enterprise_ai_endpoint="https://primary.example/v1",
        enterprise_ai_api_key="sk-primary",
    )
    assert embedding_model_connection(settings).connection_id == "primary"
    assert embedding_api_base_url(settings) == "https://primary.example/v1"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("default_embedding_model_id", "", "既定の埋め込みモデルを選択"),
        ("default_embedding_model_id", "qwen3-rerank", "既定の埋め込みモデル「qwen3-rerank」"),
        (
            "default_rerank_model_id",
            "text-embedding-v4",
            "既定の再ランクモデル「text-embedding-v4」",
        ),
        ("default_text_model_id", "text-embedding-v4", "既定のテキストモデル「text-embedding-v4」"),
    ],
)
def test_default_models_must_match_kind(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch(
        "/api/settings/model", json=payload(**{field: value})
    )

    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_embedding_model_must_point_to_existing_connection(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    body = payload()
    body["enterprise_ai"]["models"][1]["connection_id"] = "tertiary"

    response = make_client(settings, store).patch("/api/settings/model", json=body)

    assert response.status_code == 422
    assert "接続がありません" in response.json()["detail"]


def test_kind_other_than_text_is_not_vision() -> None:
    from pr_system_settings.model import EnterpriseAiConfiguredModel

    model = EnterpriseAiConfiguredModel(model_id="e", kind="embedding", vision_enabled=True)
    assert model.vision_enabled is False
    assert EnterpriseAiConfiguredModel(model_id="t", kind=None).kind == "text"  # type: ignore[arg-type]


def test_legacy_generative_ai_section_moves_to_registered_models(tmp_path: Path) -> None:
    """#45 より前の JSON の `generative_ai` は、プライマリ接続の登録モデルと既定のモデルへ移す。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 6,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "vision_enabled": True}],
                    "default_text_model_id": "llm-a",
                    "default_vision_model_id": "llm-a",
                },
                "generative_ai": {
                    "embedding_model": "text-embedding-v4",
                    "embedding_dim": 1536,
                    "embedding_base_url": "https://ignored.example/v1",
                    "rerank_model": "qwen3-rerank",
                    "rerank_base_url": "",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    assert settings.genai_embedding_model == "text-embedding-v4"
    assert settings.genai_rerank_model == "qwen3-rerank"
    data = make_client(settings, store).get("/api/settings/model").json()["data"]["settings"]
    models = data["enterprise_ai"]["models"]
    assert [(model["model_id"], model["kind"], model["connection_id"]) for model in models] == [
        ("llm-a", "text", "primary"),
        ("text-embedding-v4", "embedding", "primary"),
        ("qwen3-rerank", "rerank", "primary"),
    ]
    assert "generative_ai" not in data
    # 移した状態はそのまま保存できる。
    response = make_client(settings, store).patch("/api/settings/model", json=data)
    assert response.status_code == 200, response.text


def test_model_test_candidate_uses_row_connection(tmp_path: Path) -> None:
    settings = FakeSettings()
    request = ModelSettingsTestRequest.model_validate(
        {"settings": payload(), "target_type": "rerank", "model_id": "qwen3-rerank"}
    )
    candidate = model_test_candidate(settings, request)

    assert embedding_model_connection(candidate, rerank=True).api_key == "sk-ali"
    assert embedding_api_base_url(candidate, rerank=True).endswith("/compatible-api/v1")


def test_legacy_cohere_models_are_not_carried_over(tmp_path: Path) -> None:
    """旧 OCI Generative AI（Cohere）の ID は移さず、既定の埋め込み / 再ランクは未設定にする。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 6,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "vision_enabled": True}],
                    "default_text_model_id": "llm-a",
                    "default_vision_model_id": "llm-a",
                },
                "generative_ai": {
                    "embedding_model": "cohere.embed-v4.0",
                    "rerank_model": "cohere.rerank-v4.0-fast",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    data = make_client(settings, store).get("/api/settings/model").json()["data"]["settings"]
    enterprise = data["enterprise_ai"]
    assert [model["model_id"] for model in enterprise["models"]] == ["llm-a"]
    assert enterprise["default_embedding_model_id"] == ""
    assert enterprise["default_rerank_model_id"] == ""
