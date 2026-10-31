"""埋め込み / 再ランクの API key の決め方（#45）。

backend は既定の埋め込み / 再ランクのモデルの接続の key を `EMBEDDING_API_KEY` / `RERANK_API_KEY` で
渡す（LLM と別の接続にできる）。渡されなければ LLM の既定の接続の key を使う。
"""

from __future__ import annotations

from rag_engine.adapters import oci
from rag_engine.config import get_settings

BASE_ENV = {
    "OCI_ENTERPRISE_AI_ENDPOINT": "https://api.deepseek.com",
    "OCI_ENTERPRISE_AI_API_KEY": "sk-deepseek",
}


def test_embedding_and_rerank_keys_come_from_backend_env() -> None:
    settings = get_settings(
        environ={
            **BASE_ENV,
            "EMBEDDING_BASE_URL": "https://bailian.example/compatible-mode/v1",
            "EMBEDDING_API_KEY": "sk-aliyun-embed",
            "RERANK_BASE_URL": "https://bailian.example/compatible-api/v1",
            "RERANK_API_KEY": "sk-aliyun-rerank",
        }
    )

    assert oci._embedding_api_key(settings) == "sk-aliyun-embed"
    assert oci._embedding_api_key(settings, rerank=True) == "sk-aliyun-rerank"
    assert oci._embedding_base_url(settings) == "https://bailian.example/compatible-mode/v1"
    assert oci._rerank_base_url(settings) == "https://bailian.example/compatible-api/v1"


def test_keys_fall_back_to_llm_connection() -> None:
    settings = get_settings(environ=dict(BASE_ENV))

    assert oci._embedding_api_key(settings) == "sk-deepseek"
    assert oci._embedding_api_key(settings, rerank=True) == "sk-deepseek"
