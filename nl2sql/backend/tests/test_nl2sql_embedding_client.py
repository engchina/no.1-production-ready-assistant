from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.features.nl2sql.embedding_client import OciGenAiEmbeddingClient
from app.settings import Settings


class _FakeConfig:
    @staticmethod
    def from_file(config_file: str, profile: str) -> dict[str, str]:
        return {"config_file": config_file, "profile": profile, "region": "us-chicago-1"}


class _FakeInference:
    calls: list[dict[str, Any]] = []

    class GenerativeAiInferenceClient:
        def __init__(self, **kwargs: Any) -> None:
            _FakeInference.calls.append(kwargs)


class _FakeEmbedTextDetails:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.compartment_id = ""
        self.serving_mode: object | None = None
        self.truncate = ""
        self.input_type = ""


class _FakeOnDemandServingMode:
    def __init__(self, *, model_id: str) -> None:
        self.model_id = model_id


class _FakeEmbedModels:
    EmbedTextDetails = _FakeEmbedTextDetails
    OnDemandServingMode = _FakeOnDemandServingMode


class _FakeEmbedResponse:
    def __init__(self, embeddings: list[list[float]]) -> None:
        self.data = SimpleNamespace(embeddings=embeddings)


class _FakeEmbedClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_text(self, details: _FakeEmbedTextDetails) -> _FakeEmbedResponse:
        batch = list(details.inputs)
        self.calls.append(batch)
        embeddings: list[list[float]] = []
        for text in batch:
            index = int(text.rsplit("-", 1)[1])
            embeddings.append([float(index), *([0.0] * 1535)])
        return _FakeEmbedResponse(embeddings)


def _settings(*, auth_mode: str = "config_file") -> Settings:
    return Settings(
        oci_region="us-chicago-1",
        oci_compartment_id="ocid1.compartment.oc1..example",
        oci_config_file="/tmp/oci-config",
        oci_config_profile="DEFAULT",
        oci_auth_mode=auth_mode,
        oci_genai_endpoint="https://inference.generativeai.us-chicago-1.oci.oraclecloud.com",
        oci_genai_embed_model_id="cohere.embed-v4.0",
        nl2sql_feedback_embedding_enabled=True,
    )


def test_oci_genai_embedding_client_config_auth_does_not_pass_none_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_oci = SimpleNamespace(config=_FakeConfig())
    _FakeInference.calls = []

    def fake_import_module(name: str) -> object:
        if name == "oci":
            return fake_oci
        if name == "oci.generative_ai_inference":
            return _FakeInference
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    OciGenAiEmbeddingClient(_settings())._get_client()

    assert _FakeInference.calls
    assert _FakeInference.calls[0]["config"] == {
        "config_file": "/tmp/oci-config",
        "profile": "DEFAULT",
        "region": "us-chicago-1",
    }
    assert "signer" not in _FakeInference.calls[0]


def test_oci_genai_embedding_client_principal_auth_passes_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = object()
    fake_oci = SimpleNamespace(
        config=_FakeConfig(),
        auth=SimpleNamespace(
            signers=SimpleNamespace(
                InstancePrincipalsSecurityTokenSigner=lambda: signer,
            )
        ),
    )
    _FakeInference.calls = []

    def fake_import_module(name: str) -> object:
        if name == "oci":
            return fake_oci
        if name == "oci.generative_ai_inference":
            return _FakeInference
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    OciGenAiEmbeddingClient(_settings(auth_mode="instance_principal"))._get_client()

    assert _FakeInference.calls
    assert _FakeInference.calls[0]["config"] == {}
    assert _FakeInference.calls[0]["signer"] is signer


def test_oci_genai_embedding_client_batches_oci_requests_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OciGenAiEmbeddingClient(_settings())
    fake_client = _FakeEmbedClient()
    monkeypatch.setattr(client, "_get_client", lambda: fake_client)
    monkeypatch.setattr(client, "_load_models", lambda: _FakeEmbedModels)

    vectors = client.embed_texts([f"text-{index}" for index in range(200)])

    assert [len(batch) for batch in fake_client.calls] == [96, 96, 8]
    assert all(len(batch) <= 96 for batch in fake_client.calls)
    assert len(vectors) == 200
    assert [vectors[index][0] for index in range(200)] == [float(index) for index in range(200)]
    assert all(len(vector) == 1536 for vector in vectors)


def test_oci_genai_embedding_client_is_rebuilt_when_oci_auth_settings_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """別の製品が OCI 認証（設定・OCI config のファイル）を保存したら作り直す（#2102）。"""
    fake_oci = SimpleNamespace(config=_FakeConfig())
    _FakeInference.calls = []

    def fake_import_module(name: str) -> object:
        if name == "oci":
            return fake_oci
        if name == "oci.generative_ai_inference":
            return _FakeInference
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    config_file = tmp_path / "oci-config"
    config_file.write_text("[DEFAULT]\nregion=us-chicago-1\n", encoding="utf-8")
    settings = _settings()
    settings.oci_config_file = str(config_file)
    client = OciGenAiEmbeddingClient(settings)

    first = client._get_client()
    assert client._get_client() is first
    assert len(_FakeInference.calls) == 1

    # `get_settings()` が読み直すと、同じ Settings の値が書き換わる。
    settings.oci_config_profile = "OTHER"
    second = client._get_client()
    assert second is not first
    assert _FakeInference.calls[1]["config"]["profile"] == "OTHER"

    # OCI 認証の画面は、同じパスの OCI config を書き換える（鍵・fingerprint の更新）。
    config_file.write_text("[OTHER]\nregion=us-chicago-1\nfingerprint=changed\n", encoding="utf-8")
    assert client._get_client() is not second
    assert len(_FakeInference.calls) == 3


def test_oci_genai_embedding_client_disables_sdk_retry_and_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 再試行・circuit breaker は共通の実行器が受け持つ。
    # SDK の breaker が先に開くと 429 で失敗する（#2211）。
    none_retry, no_breaker = object(), object()
    fake_oci = SimpleNamespace(
        config=_FakeConfig(),
        retry=SimpleNamespace(NoneRetryStrategy=lambda: none_retry),
        circuit_breaker=SimpleNamespace(NoCircuitBreakerStrategy=lambda: no_breaker),
    )
    _FakeInference.calls = []

    def fake_import_module(name: str) -> object:
        if name == "oci":
            return fake_oci
        if name == "oci.generative_ai_inference":
            return _FakeInference
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    OciGenAiEmbeddingClient(_settings())._get_client()

    assert _FakeInference.calls[0]["retry_strategy"] is none_retry
    assert _FakeInference.calls[0]["circuit_breaker_strategy"] is no_breaker
