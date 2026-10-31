"""埋め込みの client の境界（#8。OpenAI 互換の API を呼ぶ）。"""

from typing import Any

import pytest

from app.features.nl2sql.embedding_client import (
    EMBED_BATCH_SIZE,
    EMBED_DIMENSIONS,
    EmbeddingClientError,
    OpenAiCompatibleEmbeddingClient,
)
from app.settings import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "nl2sql_feedback_embedding_enabled": True,
        "oci_genai_embed_model_id": "text-embedding-v4",
        "embedding_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "oci_enterprise_ai_endpoint": "https://enterprise.invalid/compatible-mode/v1",
        "oci_enterprise_ai_api_key": "sk-test",
        "nl2sql_embedding_concurrency": 2,
    }
    base.update(overrides)
    return Settings.model_construct(**base)


def test_base_url_prefers_embedding_setting() -> None:
    """Base URL は埋め込みの設定が優先。空なら Enterprise AI の接続の endpoint。"""
    client = OpenAiCompatibleEmbeddingClient(_settings())
    assert client.base_url() == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    fallback = OpenAiCompatibleEmbeddingClient(_settings(embedding_base_url=""))
    assert fallback.base_url() == "https://enterprise.invalid/compatible-mode/v1"


def test_api_key_comes_from_enterprise_ai_connection() -> None:
    """API key は Enterprise AI の接続のものを使う（百煉は chat と共通）。"""
    assert OpenAiCompatibleEmbeddingClient(_settings()).api_key() == "sk-test"


def test_is_configured_requires_enabled_url_key_and_model() -> None:
    """設定が欠けていれば実行しない（CI / local は deterministic に動く）。"""
    assert OpenAiCompatibleEmbeddingClient(_settings()).is_configured() is True
    for overrides in (
        {"nl2sql_feedback_embedding_enabled": False},
        {"embedding_base_url": "", "oci_enterprise_ai_endpoint": ""},
        {"oci_enterprise_ai_api_key": ""},
        {"oci_genai_embed_model_id": ""},
    ):
        assert OpenAiCompatibleEmbeddingClient(_settings(**overrides)).is_configured() is False


def test_module_available_needs_no_sdk() -> None:
    """HTTP は標準ライブラリで送るので、追加の SDK は要らない。"""
    assert OpenAiCompatibleEmbeddingClient(_settings()).module_available() is True


def test_embed_texts_splits_into_batches_and_keeps_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 件ずつに分けて送り、`data[].index` が順不同でも入力の順で返す。"""
    sent: list[dict[str, Any]] = []

    def fake_post(url: str, body: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
        assert url == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        assert api_key == "sk-test"
        assert body["model"] == "text-embedding-v4"
        assert body["dimensions"] == EMBED_DIMENSIONS
        sent.append(body)
        # 応答はわざと逆順で返す（index で並べ直せることを確かめる）。
        rows = [
            {
                "index": index,
                "embedding": [
                    float(int(text.rsplit("-", 1)[1])),
                    *([0.0] * (EMBED_DIMENSIONS - 1)),
                ],
            }
            for index, text in enumerate(body["input"])
        ]
        return {"data": list(reversed(rows))}

    monkeypatch.setattr("app.features.nl2sql.embedding_client._post_json", fake_post)
    client = OpenAiCompatibleEmbeddingClient(_settings())

    vectors = client.embed_texts([f"text-{index}" for index in range(25)])

    assert [vector[0] for vector in vectors] == [float(index) for index in range(25)]
    assert [len(body["input"]) for body in sent] == [EMBED_BATCH_SIZE, EMBED_BATCH_SIZE, 5]


def test_embed_texts_rejects_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """応答の件数が入力と違えば拒否する（候補を取りこぼしたまま進めない）。"""

    def fake_post(url: str, body: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
        return {"data": [{"index": 0, "embedding": [0.0] * EMBED_DIMENSIONS}]}

    monkeypatch.setattr("app.features.nl2sql.embedding_client._post_json", fake_post)
    client = OpenAiCompatibleEmbeddingClient(_settings())

    with pytest.raises(EmbeddingClientError, match="件数"):
        client.embed_texts(["a", "b"])


def test_embed_texts_rejects_wrong_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    """次元が違えば拒否する（Oracle の VECTOR 列に入らない）。"""

    def fake_post(url: str, body: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
        return {"data": [{"index": 0, "embedding": [0.0, 1.0]}]}

    monkeypatch.setattr("app.features.nl2sql.embedding_client._post_json", fake_post)
    client = OpenAiCompatibleEmbeddingClient(_settings())

    with pytest.raises(EmbeddingClientError, match="次元"):
        client.embed_texts(["a"])


def test_embed_texts_requires_configuration() -> None:
    """設定が足りないまま呼んだら、実行せずに失敗する。"""
    client = OpenAiCompatibleEmbeddingClient(_settings(oci_enterprise_ai_api_key=""))

    with pytest.raises(EmbeddingClientError, match="設定が不足"):
        client.embed_texts(["a"])


def test_embed_texts_returns_empty_without_input() -> None:
    """入力が無ければ呼び出さない。"""
    assert OpenAiCompatibleEmbeddingClient(_settings()).embed_texts([]) == []
