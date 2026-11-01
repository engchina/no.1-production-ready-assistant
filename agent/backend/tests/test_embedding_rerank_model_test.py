"""設定画面の埋め込み / 再ランクのモデルのテストが OpenAI 互換の HTTP を呼ぶ（#8 / #45）。

`_post_openai_compatible` を fake に差し替え、URL・body（`dimensions` = 設定の次元）・API key・
次元の検証を確かめる。埋め込み / 再ランクのモデルは登録モデル（種類 embedding / rerank）で、
そのモデルの接続（ここではセカンダリ接続 = 百煉）の Endpoint URL と key で呼ぶ。外部 API へは
接続しない。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anyio
import pytest
from pr_system_settings.model import ModelSettingsTestRequest, model_test_candidate
from pr_system_settings.model_test_input import (
    MODEL_TEST_EMBEDDING_INPUT,
    MODEL_TEST_RERANK_DOCUMENTS,
    MODEL_TEST_RERANK_QUERY,
)

import app.features.agent.router as agent_router
from app.settings import Settings

BAILIAN = "https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
# 埋め込みの次元は製品の設定（Oracle の VECTOR 列に合わせた値）から作る（直書きしない）。
DIM = Settings(_env_file=None).genai_embedding_dim


def _request(
    target: str, model_id: str, *, endpoint: str = BAILIAN, connection: str = "secondary"
) -> ModelSettingsTestRequest:
    payload = {
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": "https://api.deepseek.com",
                    "api_key": "sk-deepseek",
                },
                {"connection_id": "secondary", "endpoint": endpoint, "api_key": "sk-aliyun"},
            ],
            "models": [
                {"model_id": "deepseek-flash"},
                {"model_id": "text-embedding-v4", "kind": "embedding", "connection_id": connection},
                {"model_id": "qwen3-rerank", "kind": "rerank", "connection_id": connection},
            ],
            "default_text_model_id": "deepseek-flash",
            "default_vision_model_id": "",
            "default_embedding_model_id": "text-embedding-v4",
            "default_rerank_model_id": "qwen3-rerank",
            "text_response_path": "/output_text",
        },
    }
    return ModelSettingsTestRequest.model_validate(
        {"settings": payload, "target_type": target, "model_id": model_id}
    )


def _run(
    monkeypatch: pytest.MonkeyPatch, request: ModelSettingsTestRequest, response: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any], str]], dict[str, Any]]:
    calls: list[tuple[str, dict[str, Any], str]] = []

    def fake_post(
        url: str, body: Mapping[str, object], api_key: str, _timeout: int
    ) -> dict[str, Any]:
        calls.append((url, dict(body), api_key))
        return response

    monkeypatch.setattr(agent_router, "_post_openai_compatible", fake_post)
    candidate = model_test_candidate(Settings(_env_file=None), request)
    details = anyio.run(agent_router._run_model_settings_test, candidate, request)  # noqa: SLF001
    return calls, details


def test_embedding_test_uses_model_connection_and_sends_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, details = _run(
        monkeypatch,
        _request("embedding", "text-embedding-v4"),
        {"data": [{"index": 0, "embedding": [0.1] * DIM}]},
    )

    assert calls == [
        (
            f"{BAILIAN}/embeddings",
            {
                "model": "text-embedding-v4",
                "input": [MODEL_TEST_EMBEDDING_INPUT],
                "dimensions": DIM,
            },
            "sk-aliyun",
        )
    ]
    assert details == {"vector_dim": DIM, "input_count": 1}


def test_embedding_test_rejects_wrong_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(
        ValueError, match=rf"埋め込みの次元が {DIM} ではありません（返った次元: {DIM - 1}）"
    ):
        _run(
            monkeypatch,
            _request("embedding", "text-embedding-v4"),
            {"data": [{"index": 0, "embedding": [0.1] * (DIM - 1)}]},
        )


def test_embedding_on_primary_connection_uses_primary_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _run(
        monkeypatch,
        _request("embedding", "text-embedding-v4", connection="primary"),
        {"data": [{"index": 0, "embedding": [0.1] * DIM}]},
    )

    assert calls[0][0] == "https://api.deepseek.com/embeddings"
    assert calls[0][2] == "sk-deepseek"


def test_rerank_test_rewrites_compatible_mode_and_returns_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, details = _run(
        monkeypatch,
        _request("rerank", "qwen3-rerank"),
        {
            "results": [
                {"index": 0, "relevance_score": 0.8},
                {"index": 1, "relevance_score": 0.1},
            ]
        },
    )

    assert calls == [
        (
            BAILIAN.replace("/compatible-mode/", "/compatible-api/") + "/reranks",
            {
                "model": "qwen3-rerank",
                "query": MODEL_TEST_RERANK_QUERY,
                "documents": list(MODEL_TEST_RERANK_DOCUMENTS),
                "top_n": 2,
            },
            "sk-aliyun",
        )
    ]
    assert details == {"ranked_count": 2, "ranked_order": "文書 1 → 文書 2", "top_score": 0.8}


def test_rerank_endpoint_without_compatible_mode_is_used_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _run(
        monkeypatch,
        _request("rerank", "qwen3-rerank", endpoint="https://r.example.test/v1"),
        {"results": [{"index": 1, "relevance_score": 0.5}]},
    )

    assert calls[0][0] == "https://r.example.test/v1/reranks"


def test_rerank_test_requires_index_and_score(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="index と relevance_score"):
        _run(monkeypatch, _request("rerank", "qwen3-rerank"), {"results": []})


def test_embedding_dim_is_the_platform_constant() -> None:
    """Agent の設定の次元は 3 製品の正本（#46）。旧い既定（1536）は起動で拒否する。"""
    from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
    from pydantic import ValidationError

    assert DIM == EMBEDDING_DIMENSIONS
    with pytest.raises(ValidationError):
        Settings(_env_file=None, genai_embedding_dim=1536)
