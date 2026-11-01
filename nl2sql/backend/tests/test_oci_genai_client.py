"""設定画面のモデルのテストで使う埋め込み / 再ランクのクライアント（#8）。

OpenAI 互換の HTTP を fake の `post` で受け、送る body と URL・次元の検証を確かめる。
外部 API へは接続しない。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest
from pr_system_settings.model import EnterpriseAiConfiguredModel

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
                {"index": 1, "embedding": [0.2] * 1536},
                {"index": 0, "embedding": [0.1] * 1536},
            ]
        }
    )
    client = OciGenAiClient(settings=_settings(), post=post)

    vectors = await client.embed(["a", "b"], input_type="SEARCH_QUERY")

    assert post.calls == [
        (
            f"{ENDPOINT}/embeddings",
            {"model": "text-embedding-v4", "input": ["a", "b"], "dimensions": 1536},
        )
    ]
    assert [vector[0] for vector in vectors] == [0.1, 0.2]


async def test_embed_rejects_wrong_dimension() -> None:
    post = _FakePost({"data": [{"index": 0, "embedding": [0.1] * 1024}]})
    client = OciGenAiClient(settings=_settings(), post=post)

    with pytest.raises(
        ValueError, match=r"embedding の次元数が不正です。expected=1536, actual=1024"
    ):
        await client.embed(["a"])


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


BAILIAN = "https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _connection_settings(connection_id: str) -> Settings:
    """プライマリ = DeepSeek（LLM）、セカンダリ / ターシャリ = 百煉（埋め込み / 再ランク。#45）。"""
    return _settings(
        oci_enterprise_ai_endpoint="https://api.deepseek.com",
        oci_enterprise_ai_api_key="sk-deepseek",
        **{
            f"oci_enterprise_ai_{connection_id}_endpoint": BAILIAN,
            f"oci_enterprise_ai_{connection_id}_api_key": f"sk-{connection_id}",
        },
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="deepseek-flash"),
            EnterpriseAiConfiguredModel(
                model_id="text-embedding-v4", kind="embedding", connection_id=connection_id
            ),
            EnterpriseAiConfiguredModel(
                model_id="qwen3-rerank", kind="rerank", connection_id=connection_id
            ),
        ],
    )


@pytest.mark.parametrize("connection_id", ["secondary", "tertiary"])
async def test_embedding_and_rerank_use_model_connection_over_http(
    monkeypatch: pytest.MonkeyPatch, connection_id: str
) -> None:
    """選んだ接続の endpoint と key で HTTP を送り、再ランクは /compatible-api/ に読み替える。"""
    sent: list[tuple[str, str | None]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> _FakeResponse:
        sent.append((request.full_url, request.get_header("Authorization")))
        if request.full_url.endswith("/reranks"):
            return _FakeResponse({"results": [{"index": 0, "relevance_score": 0.7}]})
        return _FakeResponse({"data": [{"index": 0, "embedding": [0.1] * 1536}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OciGenAiClient(
        settings=_connection_settings(connection_id), sdk_call_runner=_run_inline
    )

    await client.embed(["a"])
    await client.rerank("q", ["d0", "d1"], top_n=2)

    assert sent == [
        (f"{BAILIAN}/embeddings", f"Bearer sk-{connection_id}"),
        (
            BAILIAN.replace("/compatible-mode/", "/compatible-api/") + "/reranks",
            f"Bearer sk-{connection_id}",
        ),
    ]


async def _run_inline(operation: Any) -> Any:
    return operation()


async def test_missing_base_url_is_config_error() -> None:
    client = OciGenAiClient(settings=_settings(oci_enterprise_ai_endpoint=""), post=_FakePost({}))

    with pytest.raises(ValueError, match="Base URL が未設定です"):
        await client.embed(["a"])
