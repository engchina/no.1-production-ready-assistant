"""設定画面のモデルのテストで使う埋め込み / 再ランクのクライアント（#8）。

OpenAI 互換の HTTP を fake の `post` で受け、送る body と URL・次元の検証を確かめる。
外部 API へは接続しない。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.clients.oci_genai import OciGenAiClient
from app.settings import Settings

ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class _FakePost:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, body))
        return self.payload


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "oci_enterprise_ai_endpoint": ENDPOINT,
        "oci_enterprise_ai_api_key": "sk-test",
        "oci_genai_embedding_model": "text-embedding-v4",
        "oci_genai_rerank_model": "qwen3-rerank",
        **overrides,
    }
    return Settings(_env_file=None, **values)


async def test_embed_sends_dimensions_and_orders_by_index() -> None:
    post = _FakePost(
        {
            "data": [
                {"index": 1, "embedding": [0.2] * 1024},
                {"index": 0, "embedding": [0.1] * 1024},
            ]
        }
    )
    client = OciGenAiClient(settings=_settings(), post=post)

    vectors = await client.embed(["a", "b"], input_type="SEARCH_QUERY")

    assert post.calls == [
        (
            f"{ENDPOINT}/embeddings",
            {"model": "text-embedding-v4", "input": ["a", "b"], "dimensions": 1024},
        )
    ]
    assert [vector[0] for vector in vectors] == [0.1, 0.2]


async def test_embed_rejects_wrong_dimension() -> None:
    post = _FakePost({"data": [{"index": 0, "embedding": [0.1] * 1536}]})
    client = OciGenAiClient(settings=_settings(), post=post)

    with pytest.raises(
        ValueError, match=r"embedding の次元数が不正です。expected=1024, actual=1536"
    ):
        await client.embed(["a"])


async def test_embed_uses_embedding_base_url_when_set() -> None:
    post = _FakePost({"data": [{"index": 0, "embedding": [0.1] * 1024}]})
    client = OciGenAiClient(
        settings=_settings(embedding_base_url="https://embed.example.test/v1/"), post=post
    )

    await client.embed(["a"])

    assert post.calls[0][0] == "https://embed.example.test/v1/embeddings"


async def test_rerank_rewrites_compatible_mode_to_compatible_api() -> None:
    post = _FakePost(
        {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]}
    )
    client = OciGenAiClient(settings=_settings(), post=post)

    ranks = await client.rerank("q", ["d0", "d1"], top_n=1)

    assert post.calls == [
        (
            "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            {"model": "qwen3-rerank", "query": "q", "documents": ["d0", "d1"], "top_n": 1},
        )
    ]
    assert ranks == [(1, 0.9)]


async def test_rerank_base_url_is_used_as_is() -> None:
    post = _FakePost({"results": [{"index": 0, "relevance_score": 0.5}]})
    client = OciGenAiClient(
        settings=_settings(rerank_base_url="https://rerank.example.test/compatible-mode/v1"),
        post=post,
    )

    await client.rerank("q", ["d0"], top_n=1)

    assert post.calls[0][0] == "https://rerank.example.test/compatible-mode/v1/reranks"


async def test_missing_base_url_is_config_error() -> None:
    client = OciGenAiClient(settings=_settings(oci_enterprise_ai_endpoint=""), post=_FakePost({}))

    with pytest.raises(ValueError, match="Base URL が未設定です"):
        await client.embed(["a"])
