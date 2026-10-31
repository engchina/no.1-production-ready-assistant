"""設定画面の埋め込み / 再ランクのモデルのテストが OpenAI 互換の HTTP を呼ぶ（#8）。

`_post_openai_compatible` を fake に差し替え、URL・body（`dimensions=1024`）・次元の検証を確かめる。
外部 API へは接続しない。
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

ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _request(target: str, model_id: str, **generative: str) -> ModelSettingsTestRequest:
    payload = {
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": ENDPOINT,
                    "project_ocid": "",
                    "api_key": "sk-test",
                }
            ],
            "models": [{"model_id": "text-a"}],
            "default_text_model_id": "text-a",
            "default_vision_model_id": "",
            "text_response_path": "/output_text",
        },
        "generative_ai": {
            "embedding_model": "text-embedding-v4",
            "embedding_dim": 1024,
            "rerank_model": "qwen3-rerank",
            **generative,
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


def test_embedding_test_sends_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, details = _run(
        monkeypatch,
        _request("embedding", "text-embedding-v4"),
        {"data": [{"index": 0, "embedding": [0.1] * 1024}]},
    )

    assert calls == [
        (
            f"{ENDPOINT}/embeddings",
            {
                "model": "text-embedding-v4",
                "input": [MODEL_TEST_EMBEDDING_INPUT],
                "dimensions": 1024,
            },
            "sk-test",
        )
    ]
    assert details == {"vector_dim": 1024, "input_count": 1}


def test_embedding_test_rejects_wrong_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(
        ValueError, match=r"embedding の次元数が不正です。expected=1024, actual=1536"
    ):
        _run(
            monkeypatch,
            _request("embedding", "text-embedding-v4"),
            {"data": [{"index": 0, "embedding": [0.1] * 1536}]},
        )


def test_embedding_test_uses_embedding_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _run(
        monkeypatch,
        _request("embedding", "text-embedding-v4", embedding_base_url="https://e.example.test/v1"),
        {"data": [{"index": 0, "embedding": [0.1] * 1024}]},
    )

    assert calls[0][0] == "https://e.example.test/v1/embeddings"


def test_rerank_test_rewrites_compatible_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, details = _run(
        monkeypatch,
        _request("rerank", "qwen3-rerank"),
        {"results": [{"index": 1, "relevance_score": 0.8}]},
    )

    assert calls == [
        (
            "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
            {
                "model": "qwen3-rerank",
                "query": MODEL_TEST_RERANK_QUERY,
                "documents": list(MODEL_TEST_RERANK_DOCUMENTS),
                "top_n": 1,
            },
            "sk-test",
        )
    ]
    assert details == {"ranked_count": 1, "top_score": 0.8}


def test_rerank_test_uses_rerank_base_url_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _run(
        monkeypatch,
        _request(
            "rerank", "qwen3-rerank", rerank_base_url="https://r.example.test/compatible-mode/v1"
        ),
        {"results": []},
    )

    assert calls[0][0] == "https://r.example.test/compatible-mode/v1/reranks"


def test_embedding_dim_is_the_platform_constant() -> None:
    """Agent の設定の次元は 3 製品の正本（#46）。旧い既定（1536）は起動で拒否する。"""
    from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
    from pydantic import ValidationError

    from app.settings import Settings

    assert Settings().oci_genai_embedding_dim == EMBEDDING_DIMENSIONS
    with pytest.raises(ValidationError):
        Settings(oci_genai_embedding_dim=1536)
