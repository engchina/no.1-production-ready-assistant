"""OCI Enterprise AI の接続をモデルごとに選ぶ（#533 / #786）。呼び出しが対象モデルの接続を使うこと。

HTTP は決定論の fake transport で受け、URL・API key・Project のヘッダーを確かめる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from rag_parser_core import oci_enterprise_ai as shared_client

from app.clients.oci_enterprise_ai import OciEnterpriseAiClient, config_from_settings
from app.config import EnterpriseAiConfiguredModel, Settings
from app.readiness import READINESS_MISSING_CREDENTIALS, READINESS_OK, _enterprise_ai_check
from app.services.control import service_runtime_env

PRIMARY = "https://primary.example/openai/v1"
SECONDARY = "https://secondary.example/openai/v1"
# ターシャリ接続（#786）は OpenAI / OpenAI 互換 API 向けで、Project OCID を入れない。
TERTIARY = "https://api.openai.example/v1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "oci_compartment_id": "ocid1.compartment.oc1..example",
        "oci_enterprise_ai_endpoint": PRIMARY,
        "oci_enterprise_ai_project_ocid": "ocid1.project.primary",
        "oci_enterprise_ai_api_key": "sk-primary",
        "oci_enterprise_ai_secondary_endpoint": SECONDARY,
        "oci_enterprise_ai_secondary_project_ocid": "ocid1.project.secondary",
        "oci_enterprise_ai_secondary_api_key": "sk-secondary",
        "oci_enterprise_ai_tertiary_endpoint": TERTIARY,
        "oci_enterprise_ai_tertiary_project_ocid": "",
        "oci_enterprise_ai_tertiary_api_key": "sk-tertiary",
        "oci_enterprise_ai_models": [
            EnterpriseAiConfiguredModel(model_id="text-a", display_name="A"),
            EnterpriseAiConfiguredModel(
                model_id="vision-b",
                display_name="B",
                vision_enabled=True,
                connection_id="secondary",
            ),
            EnterpriseAiConfiguredModel(
                model_id="text-c", display_name="C", connection_id="secondary"
            ),
            EnterpriseAiConfiguredModel(
                model_id="gpt-d", display_name="D", connection_id="tertiary"
            ),
        ],
        "oci_enterprise_ai_default_text_model": "text-a",
        "oci_enterprise_ai_default_vision_model": "vision-b",
        "oci_enterprise_ai_llm_path": "/responses",
        "oci_enterprise_ai_vlm_path": "/responses",
        "oci_enterprise_ai_vlm_input_mode": "files_api",
        "oci_enterprise_ai_timeout_seconds": 5.0,
        "oci_enterprise_ai_max_retries": 0,
    }
    values.update(overrides)
    return Settings.model_construct(**values)


class RecordingTransport:
    """Enterprise AI の HTTP を記録する fake（応答は固定）。"""

    def __init__(self, response: Mapping[str, Any] | None = None) -> None:
        self._response = response or {"output_text": "ok"}
        self.calls: list[dict[str, Any]] = []

    def _record(self, kind: str, url: str, headers: Mapping[str, str]) -> None:
        self.calls.append({"kind": kind, "url": url, "headers": dict(headers)})

    async def post_json(
        self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str], timeout: float
    ) -> Mapping[str, Any]:
        self._record("post", url, headers)
        return self._response

    async def stream_json(
        self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str], timeout: float
    ) -> AsyncIterator[str]:
        self._record("stream", url, headers)
        yield 'data: {"type": "response.output_text.delta", "delta": "ok"}'

    async def upload_file(
        self,
        url: str,
        file_name: str,
        content: bytes,
        *,
        mime_type: str,
        purpose: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        self._record("upload", url, headers)
        return {"id": "file-1"}

    async def delete(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> Mapping[str, Any]:
        self._record("delete", url, headers)
        return {"deleted": True}


def _connection_of(call: Mapping[str, Any]) -> tuple[str, str, str]:
    headers = call["headers"]
    base = next(item for item in (PRIMARY, SECONDARY, TERTIARY) if call["url"].startswith(item))
    return base, headers["Authorization"], headers.get("OpenAI-Project", "")


PRIMARY_CONNECTION = (PRIMARY, "Bearer sk-primary", "ocid1.project.primary")
SECONDARY_CONNECTION = (SECONDARY, "Bearer sk-secondary", "ocid1.project.secondary")
# Project OCID がないので OpenAI-Project ヘッダーは送らない。
TERTIARY_CONNECTION = (TERTIARY, "Bearer sk-tertiary", "")


def test_config_uses_text_and_vision_connections() -> None:
    config = config_from_settings(_settings())
    assert (config.oci_enterprise_ai_endpoint, config.oci_enterprise_ai_api_key) == (
        PRIMARY,
        "sk-primary",
    )
    vision = config.for_vision()
    assert (vision.oci_enterprise_ai_endpoint, vision.oci_enterprise_ai_api_key) == (
        SECONDARY,
        "sk-secondary",
    )
    assert vision.oci_enterprise_ai_project_ocid == "ocid1.project.secondary"
    assert vision.vision_model_id == "vision-b"


def test_config_same_connection_keeps_single_config() -> None:
    settings = _settings(oci_enterprise_ai_default_vision_model="vision-b")
    settings.oci_enterprise_ai_models = [
        EnterpriseAiConfiguredModel(model_id="text-a"),
        EnterpriseAiConfiguredModel(model_id="vision-b", vision_enabled=True),
    ]
    config = config_from_settings(settings)
    assert config.vision_oci_enterprise_ai_endpoint is None
    assert config.for_vision() is config


async def test_text_and_vision_calls_use_each_model_connection() -> None:
    transport = RecordingTransport()
    client = OciEnterpriseAiClient(settings=_settings(), http_transport=transport)

    await client.generate("質問", "根拠")
    await client.generate_from_images([b"png"], "説明してください")
    await client.generate_from_image(b"%PDF", "説明", mime_type="application/pdf")

    connections = [(call["kind"], _connection_of(call)) for call in transport.calls]
    assert connections == [
        ("post", PRIMARY_CONNECTION),
        ("post", SECONDARY_CONNECTION),
        ("upload", SECONDARY_CONNECTION),
        ("post", SECONDARY_CONNECTION),
        ("delete", SECONDARY_CONNECTION),
    ]


async def test_vlm_extraction_uses_vision_connection() -> None:
    transport = RecordingTransport(
        {"data": {"raw_text": "本文", "document_type": "文書", "confidence": 0.9, "warnings": []}}
    )
    client = OciEnterpriseAiClient(settings=_settings(), http_transport=transport)

    await client.extract_with_vlm(b"%PDF", "抽出", mime_type="application/pdf")

    assert {_connection_of(call) for call in transport.calls} == {SECONDARY_CONNECTION}
    preview = client.preview_vlm_request(b"%PDF", "抽出", mime_type="application/pdf")
    assert preview.url.startswith(SECONDARY)


async def test_compare_model_override_uses_that_model_connection() -> None:
    """マルチモデル比較で選んだモデル（セカンダリ接続）は、セカンダリ接続で呼ぶ。"""
    transport = RecordingTransport()
    client = OciEnterpriseAiClient(
        settings=_settings(), http_transport=transport, model_id="text-c"
    )

    await client.generate("質問", "根拠")

    assert _connection_of(transport.calls[0]) == SECONDARY_CONNECTION
    assert client.preview_llm_request("質問", "根拠").url.startswith(SECONDARY)


async def test_tertiary_model_uses_responses_api_without_project_header() -> None:
    """ターシャリ接続（Project OCID なし）のモデルは、OpenAI-Project ヘッダーなしで呼ぶ。"""
    transport = RecordingTransport()
    client = OciEnterpriseAiClient(settings=_settings(), http_transport=transport, model_id="gpt-d")

    await client.generate("質問", "根拠")

    assert _connection_of(transport.calls[0]) == TERTIARY_CONNECTION
    assert "OpenAI-Project" not in transport.calls[0]["headers"]
    assert transport.calls[0]["url"] == f"{TERTIARY}/responses"


def test_default_transport_is_built_per_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []

    class Recording:
        def __init__(self, config: shared_client.OciEnterpriseAiConfig) -> None:
            built.append(config.oci_enterprise_ai_endpoint)

    monkeypatch.setattr(shared_client, "_DefaultEnterpriseAiTransport", Recording)
    OciEnterpriseAiClient(settings=_settings())
    assert built == [PRIMARY, SECONDARY]


def test_parser_service_env_uses_vision_model_connection() -> None:
    """OCI parser は VLM 抽出だけなので、既定の Vision モデルの接続を渡す。"""
    env = service_runtime_env(_settings())
    assert env["PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT"] == SECONDARY
    assert env["PLATFORM_OCI_ENTERPRISE_AI_API_KEY"] == "sk-secondary"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_PROJECT_OCID"] == "ocid1.project.secondary"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL"] == "vision-b"


def test_engine_settings_use_answer_and_vision_connections(tmp_path: Path) -> None:
    from rag_engine.config import ENTERPRISE_AI_LLM_PROVIDER, ENTERPRISE_AI_VISION_LLM_PROVIDER

    from app.rag.answer_engine import build_engine_settings

    engine_settings = build_engine_settings(_settings(), output_dir=tmp_path)
    answer = engine_settings.llm_providers[ENTERPRISE_AI_LLM_PROVIDER]
    vision = engine_settings.llm_providers[ENTERPRISE_AI_VISION_LLM_PROVIDER]
    assert (answer.endpoint, answer.api_key, answer.project_id, answer.model) == (
        PRIMARY,
        "sk-primary",
        "ocid1.project.primary",
        "text-a",
    )
    assert (vision.endpoint, vision.api_key, vision.project_id, vision.model) == (
        SECONDARY,
        "sk-secondary",
        "ocid1.project.secondary",
        "vision-b",
    )


def test_readiness_checks_connection_of_each_default_model() -> None:
    assert _enterprise_ai_check(_settings()) == READINESS_OK
    assert (
        _enterprise_ai_check(_settings(oci_enterprise_ai_secondary_api_key=""))
        == READINESS_MISSING_CREDENTIALS
    )


def _two_connection_payload() -> dict[str, Any]:
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
                    # #533 の表示名が残った payload も受け付ける（#542 で廃止。無視する）。
                    "display_name": "シカゴ",
                    "endpoint": SECONDARY,
                    "project_ocid": "ocid1.project.secondary",
                    "api_key": "sk-secondary",
                },
                {
                    "connection_id": "tertiary",
                    "endpoint": TERTIARY,
                    "project_ocid": "",
                    "api_key": "sk-tertiary",
                },
            ],
            "models": [
                {"model_id": "text-a", "display_name": "A"},
                {
                    "model_id": "vision-b",
                    "display_name": "B",
                    "vision_enabled": True,
                    "connection_id": "secondary",
                },
                {"model_id": "gpt-d", "display_name": "D", "connection_id": "tertiary"},
            ],
            "default_text_model_id": "text-a",
            "default_vision_model_id": "vision-b",
            "api_path": "/responses",
            "vlm_input_mode": "files_api",
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
        ("enterprise_text", "text-a", PRIMARY_CONNECTION),
        ("enterprise_vision", "vision-b", SECONDARY_CONNECTION),
        ("enterprise_text", "gpt-d", TERTIARY_CONNECTION),
    ],
    ids=["text-primary", "vision-secondary", "text-tertiary"],
)
def test_model_settings_test_calls_connection_of_tested_model(
    monkeypatch: pytest.MonkeyPatch,
    target_type: str,
    model_id: str,
    expected: tuple[str, str, str],
) -> None:
    """接続テストは、保存前の入力値のうち、テストするモデルの接続で呼ぶ。"""
    from app.main import app
    from tests.support import AsgiTestClient

    transport = RecordingTransport({"output_text": "ok"})
    monkeypatch.setattr(shared_client, "_DefaultEnterpriseAiTransport", lambda _config: transport)

    response = AsgiTestClient(app).post(
        "/api/settings/model/test",
        json={
            "settings": _two_connection_payload(),
            "target_type": target_type,
            "model_id": model_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "success", response.text
    assert [_connection_of(call) for call in transport.calls] == [expected]
    assert "sk-primary" not in response.text and "sk-secondary" not in response.text
    assert "sk-tertiary" not in response.text
