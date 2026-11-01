"""埋め込みの client の境界（#8。OpenAI 互換の API を呼ぶ）。

埋め込みは既定の埋め込みモデル（登録モデルの種類 embedding。#45）の接続の endpoint と key で呼ぶ。
ここではプライマリ接続 = DeepSeek（LLM）、セカンダリ接続 = 百煉（埋め込み）。
"""

from typing import Any

import pytest
from pr_system_settings.model import EnterpriseAiConfiguredModel

from app.features.nl2sql.embedding_client import (
    EMBED_BATCH_SIZE,
    EMBED_DIMENSIONS,
    EmbeddingClientError,
    OpenAiCompatibleEmbeddingClient,
)
from app.settings import Settings

BAILIAN = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "nl2sql_feedback_embedding_enabled": True,
        "genai_embedding_model": "text-embedding-v4",
        "genai_embed_model_id": "text-embedding-v4",
        "enterprise_ai_endpoint": "https://api.deepseek.com",
        "enterprise_ai_api_key": "sk-deepseek",
        "enterprise_ai_secondary_endpoint": BAILIAN,
        "enterprise_ai_secondary_api_key": "sk-test",
        "enterprise_ai_models": [
            EnterpriseAiConfiguredModel(model_id="deepseek-flash"),
            EnterpriseAiConfiguredModel(
                model_id="text-embedding-v4", kind="embedding", connection_id="secondary"
            ),
        ],
        "nl2sql_embedding_concurrency": 2,
    }
    base.update(overrides)
    return Settings.model_construct(**base)


def test_base_url_and_key_come_from_embedding_model_connection() -> None:
    """endpoint と key は既定の埋め込みモデルの接続（セカンダリ接続）のもの（#45）。"""
    client = OpenAiCompatibleEmbeddingClient(_settings())
    assert client.base_url() == BAILIAN
    assert client.api_key() == "sk-test"


def test_unregistered_embedding_model_uses_primary_connection() -> None:
    """登録に無い埋め込みモデル（.env だけの設定）はプライマリ接続で呼ぶ。"""
    client = OpenAiCompatibleEmbeddingClient(_settings(enterprise_ai_models=[]))
    assert client.base_url() == "https://api.deepseek.com"
    assert client.api_key() == "sk-deepseek"


def test_is_configured_requires_enabled_url_key_and_model() -> None:
    """設定が欠けていれば実行しない（CI / local は deterministic に動く）。"""
    assert OpenAiCompatibleEmbeddingClient(_settings()).is_configured() is True
    for overrides in (
        {"nl2sql_feedback_embedding_enabled": False},
        {"enterprise_ai_secondary_endpoint": "", "enterprise_ai_endpoint": ""},
        {"enterprise_ai_secondary_api_key": ""},
        {"genai_embed_model_id": ""},
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
        assert url == f"{BAILIAN}/embeddings"
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
    client = OpenAiCompatibleEmbeddingClient(_settings(enterprise_ai_secondary_api_key=""))

    with pytest.raises(EmbeddingClientError, match="設定が不足"):
        client.embed_texts(["a"])


def test_embed_texts_returns_empty_without_input() -> None:
    """入力が無ければ呼び出さない。"""
    assert OpenAiCompatibleEmbeddingClient(_settings()).embed_texts([]) == []
