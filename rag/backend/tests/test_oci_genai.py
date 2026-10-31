"""埋め込み / 再ランクの adapter の境界のテスト（#8。OpenAI 互換の API）。"""

import json
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pr_backend_core.observability.model_purpose import model_call_purpose
from pr_system_settings.model import EnterpriseAiConfiguredModel

from app.clients.oci_genai import (
    EMBEDDING_INPUT_MAX_CHARS,
    EMBEDDING_REQUEST_MAX_CHARS,
    EMBEDDING_REQUEST_MAX_INPUTS,
    OciGenAiClient,
    OciGenAiConfigError,
)
from app.config import Settings


def test_embed_requires_base_url(tmp_path: Path) -> None:
    """Base URL が無いときは、利用者が直せる設定エラーとして返す（#8）。"""
    _ = tmp_path
    client = OciGenAiClient(
        settings=_oci_settings().model_copy(update={"oci_enterprise_ai_endpoint": ""})
    )

    with pytest.raises(OciGenAiConfigError, match="Base URL が未設定です"):
        client._base_url(rerank=False)


def test_embed_requires_api_key() -> None:
    """API key が無いときは、利用者が直せる設定エラーとして返す（#8）。"""
    client = OciGenAiClient(
        settings=_oci_settings().model_copy(update={"oci_enterprise_ai_api_key": ""})
    )

    with pytest.raises(OciGenAiConfigError, match="API key がありません"):
        client._api_key(rerank=False)


def test_rerank_base_url_falls_back_to_compatible_api() -> None:
    """再ランクの Base URL は `/compatible-mode/` を `/compatible-api/` に読み替える（#8）。

    百煉は再ランクだけ `/compatible-api/v1` 配下で、`/compatible-mode/v1` に投げると 404。
    """
    client = OciGenAiClient(
        settings=_oci_settings().model_copy(
            update={
                "oci_enterprise_ai_endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            }
        )
    )

    assert client._base_url(rerank=False) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert client._base_url(rerank=True) == "https://dashscope.aliyuncs.com/compatible-api/v1"


BAILIAN = "https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@pytest.mark.parametrize("connection_id", ["secondary", "tertiary"])
async def test_embedding_and_rerank_use_model_connection_over_http(
    monkeypatch: pytest.MonkeyPatch, connection_id: str
) -> None:
    """埋め込み / 再ランクは、そのモデルの接続の endpoint と key で HTTP を送る（#45）。

    プライマリ接続 = DeepSeek（LLM）、セカンダリ / ターシャリ接続 = 百煉（埋め込み / 再ランク）。
    再ランクは `/compatible-mode/` を `/compatible-api/` に読み替える。
    """
    sent: list[tuple[str, str | None]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> _FakeHttpResponse:
        sent.append((request.full_url, request.get_header("Authorization")))
        if request.full_url.endswith("/reranks"):
            return _FakeHttpResponse({"results": [{"index": 0, "relevance_score": 0.7}]})
        return _FakeHttpResponse({"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    async def inline(operation: Callable[[], Any]) -> Any:
        return operation()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    settings = _oci_settings().model_copy(
        update={
            "oci_enterprise_ai_endpoint": "https://api.deepseek.com",
            "oci_enterprise_ai_api_key": "sk-deepseek",
            f"oci_enterprise_ai_{connection_id}_endpoint": BAILIAN,
            f"oci_enterprise_ai_{connection_id}_api_key": f"sk-{connection_id}",
            "oci_enterprise_ai_models": [
                EnterpriseAiConfiguredModel(model_id="deepseek-flash"),
                EnterpriseAiConfiguredModel(
                    model_id="text-embedding-v4", kind="embedding", connection_id=connection_id
                ),
                EnterpriseAiConfiguredModel(
                    model_id="qwen3-rerank", kind="rerank", connection_id=connection_id
                ),
            ],
            "rag_embedding_cache_enabled": False,
            "rag_rerank_cache_enabled": False,
        }
    )
    client = OciGenAiClient(settings=settings, sdk_call_runner=inline)

    await client.embed(["a"])
    await client.rerank("q", ["d0", "d1"], top_n=2)

    assert sent == [
        (f"{BAILIAN}/embeddings", f"Bearer sk-{connection_id}"),
        (
            BAILIAN.replace("/compatible-mode/", "/compatible-api/") + "/reranks",
            f"Bearer sk-{connection_id}",
        ),
    ]


async def test_embed_validates_oci_embedding_count() -> None:
    """OCI embedding adapter の返却件数が入力件数と違う場合は拒否する。"""
    client = StubEmbeddingClient(vectors=[[1.0, 0.0, 0.0]])

    with pytest.raises(ValueError, match="embedding の件数が一致しません"):
        await client.embed(["a", "b"])


async def test_embed_validates_oci_embedding_dimension() -> None:
    """OCI embedding adapter の次元数が Oracle VECTOR 幅と違う場合は拒否する。"""
    client = StubEmbeddingClient(vectors=[[1.0, 0.0]])

    with pytest.raises(ValueError, match=r"embedding\[0\] の次元数が不正です"):
        await client.embed(["a"])


async def test_embed_accepts_expected_dimension() -> None:
    """期待次元の embedding はそのまま返す。"""
    client = StubEmbeddingClient(vectors=[[1.0, 0.0, 0.0]])

    assert await client.embed(["a"]) == [[1.0, 0.0, 0.0]]


async def test_embed_rejects_too_long_input_before_oci_call() -> None:
    """異常長 input は OCI embedding 呼び出し前に拒否する。"""
    client = CountingEmbeddingClient()
    long_text = "あ" * (EMBEDDING_INPUT_MAX_CHARS + 1)

    with pytest.raises(
        ValueError,
        match=(rf"index=1, chars={len(long_text)}, max_chars={EMBEDDING_INPUT_MAX_CHARS}"),
    ):
        await client.embed(["短い本文", long_text])

    assert client.calls == 0


async def test_embed_cache_reuses_hits_and_batches_only_misses() -> None:
    """embedding cache は hit を再利用し、miss だけ OCI へまとめて送る。"""
    client = CountingEmbeddingClient()

    first = await client.embed(["承認", "監査"])
    second = await client.embed(["監査", "証跡", "監査"])

    assert client.calls == 2
    assert client.batches == [["承認", "監査"], ["証跡"]]
    assert second[0] == first[1]
    assert second[2] == first[1]
    assert second[1] == _vector_for_text("証跡")


async def test_embed_cache_batches_misses_with_configured_batch_size() -> None:
    """cache miss の embedding は設定した batch size で分割する。"""
    client = CountingEmbeddingClient(
        settings=_oci_settings().model_copy(update={"rag_embedding_batch_size": 2})
    )

    vectors = await client.embed(["承認", "監査", "証跡", "監査", "規程"])

    assert client.calls == 2
    assert client.batches == [["承認", "監査"], ["証跡", "規程"]]
    assert vectors == [
        _vector_for_text("承認"),
        _vector_for_text("監査"),
        _vector_for_text("証跡"),
        _vector_for_text("監査"),
        _vector_for_text("規程"),
    ]


async def test_embed_batches_by_total_character_budget() -> None:
    """件数上限内でも 1 request の総文字数上限を超える前に分割する。"""
    client = CountingEmbeddingClient()
    texts = [
        "a" * 40_000,
        "b" * 40_000,
        "c" * 40_000,
    ]

    vectors = await client.embed(texts)

    assert client.batches == [texts[:2], texts[2:]]
    assert all(sum(map(len, batch)) <= EMBEDDING_REQUEST_MAX_CHARS for batch in client.batches)
    assert vectors == [_vector_for_text(text) for text in texts]


async def test_embed_batches_by_maximum_input_count() -> None:
    """設定 object が検証を迂回しても OCI request は 96 入力を超えない。"""
    client = CountingEmbeddingClient(
        settings=_oci_settings().model_copy(update={"rag_embedding_batch_size": 1024})
    )
    texts = [f"text-{index}" for index in range(EMBEDDING_REQUEST_MAX_INPUTS + 1)]

    vectors = await client.embed(texts)

    assert [len(batch) for batch in client.batches] == [EMBEDDING_REQUEST_MAX_INPUTS, 1]
    assert vectors == [_vector_for_text(text) for text in texts]


async def test_embed_cache_can_be_disabled() -> None:
    """設定で embedding cache を無効化できる。"""
    client = CountingEmbeddingClient(
        settings=_oci_settings().model_copy(update={"rag_embedding_cache_enabled": False})
    )

    await client.embed(["承認"])
    await client.embed(["承認"])

    assert client.calls == 2
    assert client.batches == [["承認"], ["承認"]]


async def test_embed_batches_oci_calls_when_cache_is_disabled() -> None:
    """cache 無効時も OCI embedding request は batch size で分割する。"""
    client = CountingEmbeddingClient(
        settings=_oci_settings().model_copy(
            update={
                "rag_embedding_cache_enabled": False,
                "rag_embedding_batch_size": 2,
            }
        )
    )

    vectors = await client.embed(["a", "bb", "ccc"])

    assert client.calls == 2
    assert client.batches == [["a", "bb"], ["ccc"]]
    assert vectors == [_vector_for_text("a"), _vector_for_text("bb"), _vector_for_text("ccc")]


async def test_embed_uses_openai_compatible_embeddings_request() -> None:
    """埋め込みは `POST {base_url}/embeddings` に model・input・dimensions を渡す（#8）。

    `input_type` は観測の purpose にだけ使い、API へは送らない。
    """
    post = FakePostJson(embed_response={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})
    client = OciGenAiClient(
        settings=_oci_settings(),
        post=post,
        sdk_call_runner=_run_inline,
    )

    vectors = await client.embed(["検索 query"], input_type="SEARCH_QUERY")

    assert vectors == [[1.0, 0.0, 0.0]]
    assert post.urls("/embeddings") == [
        "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    ]
    assert post.requests("/embeddings") == [
        {"model": "text-embedding-v4", "input": ["検索 query"], "dimensions": 3}
    ]


async def test_embed_rejects_invalid_input_type() -> None:
    """OCI/local 共通で embedding input_type の誤指定を fail fast する。"""
    client = OciGenAiClient(
        settings=_oci_settings(),
        post=FakePostJson(),
        sdk_call_runner=_run_inline,
    )

    with pytest.raises(ValueError, match="embedding input_type が不正です"):
        await client.embed(["a"], input_type="BAD_TYPE")  # type: ignore[arg-type]


async def test_rerank_accepts_and_sorts_valid_oci_results() -> None:
    """OCI rerank adapter の正しい結果は score 降順に正規化する。"""
    client = StubRerankClient(results=[(1, 0.25), (0, 0.75)])

    assert await client.rerank("社内規程", ["本文 A", "本文 B"], top_n=2) == [
        (0, 0.75),
        (1, 0.25),
    ]


async def test_rerank_cache_reuses_same_query_documents_and_top_n() -> None:
    """rerank cache は query/document/top_n が同一のときだけ結果を再利用する。"""
    client = CountingRerankClient()

    first = await client.rerank("承認条件", ["本文 A", "本文 B"], top_n=2)
    second = await client.rerank("承認条件", ["本文 A", "本文 B"], top_n=2)
    third = await client.rerank("承認条件", ["本文 A", "本文 B"], top_n=1)

    assert first == [(0, 0.9), (1, 0.8)]
    assert second == first
    assert third == [(0, 0.9)]
    assert client.calls == 2
    assert client.calls_args == [
        ("承認条件", ["本文 A", "本文 B"], 2),
        ("承認条件", ["本文 A", "本文 B"], 1),
    ]


async def test_rerank_cache_can_be_disabled() -> None:
    """設定で rerank cache を無効化できる。"""
    client = CountingRerankClient(
        settings=_oci_settings().model_copy(update={"rag_rerank_cache_enabled": False})
    )

    await client.rerank("承認条件", ["本文 A"], top_n=1)
    await client.rerank("承認条件", ["本文 A"], top_n=1)

    assert client.calls == 2


async def test_rerank_uses_openai_compatible_reranks_request() -> None:
    """再ランクは `POST {base_url}/reranks`（複数形）に候補をまとめて渡す（#8）。

    Base URL は埋め込みの `/compatible-mode/` を `/compatible-api/` に読み替える
    （百煉は再ランクだけそちらの配下で、`/compatible-mode/v1` に投げると 404）。
    """
    post = FakePostJson(
        rerank_response={
            "results": [
                {"index": 1, "relevance_score": 0.25},
                {"index": 0, "relevance_score": 0.75},
            ]
        }
    )
    client = OciGenAiClient(
        settings=_oci_settings(),
        post=post,
        sdk_call_runner=_run_inline,
    )

    results = await client.rerank("承認条件", ["本文 A", "本文 B"], top_n=2)

    assert results == [(0, 0.75), (1, 0.25)]
    assert post.urls("/reranks") == ["https://dashscope.aliyuncs.com/compatible-api/v1/reranks"]
    assert post.requests("/reranks") == [
        {
            "model": "qwen3-rerank",
            "query": "承認条件",
            "documents": ["本文 A", "本文 B"],
            "top_n": 2,
        }
    ]


async def test_rerank_returns_empty_without_oci_call_when_documents_are_empty() -> None:
    """候補文書が空なら OCI rerank 呼び出しを行わない。"""
    client = StubRerankClient(results=[(0, 0.75)])

    assert await client.rerank("社内規程", [], top_n=1) == []
    assert client.calls == 0


async def test_rerank_rejects_invalid_top_n() -> None:
    """top_n は 1 以上に制限する。"""
    client = StubRerankClient(results=[])

    with pytest.raises(ValueError, match="rerank top_n は 1 以上"):
        await client.rerank("社内規程", ["本文"], top_n=0)


async def test_rerank_rejects_too_many_results() -> None:
    """OCI rerank adapter が top_n を超える件数を返した場合は拒否する。"""
    client = StubRerankClient(results=[(0, 0.75), (1, 0.25)])

    with pytest.raises(ValueError, match="rerank の返却件数が不正です"):
        await client.rerank("社内規程", ["本文 A", "本文 B"], top_n=1)


async def test_rerank_rejects_duplicate_index() -> None:
    """OCI rerank adapter が同じ候補 index を重複返却した場合は拒否する。"""
    client = StubRerankClient(results=[(0, 0.75), (0, 0.25)])

    with pytest.raises(ValueError, match="rerank index が重複しています"):
        await client.rerank("社内規程", ["本文 A", "本文 B"], top_n=2)


async def test_rerank_rejects_out_of_range_index() -> None:
    """OCI rerank adapter が候補数外の index を返した場合は拒否する。"""
    client = StubRerankClient(results=[(2, 0.75)])

    with pytest.raises(ValueError, match="rerank index が範囲外です"):
        await client.rerank("社内規程", ["本文 A", "本文 B"], top_n=2)


async def test_rerank_rejects_non_finite_score() -> None:
    """OCI rerank adapter が NaN/Infinity score を返した場合は拒否する。"""
    client = StubRerankClient(results=[(0, float("nan"))])

    with pytest.raises(ValueError, match="rerank score が不正です"):
        await client.rerank("社内規程", ["本文"], top_n=1)


class StubEmbeddingClient(OciGenAiClient):
    """OCI embedding の戻り値だけを差し替えるテスト用 client。"""

    def __init__(self, vectors: list[list[float]]) -> None:
        settings = Settings.model_construct(
            oci_genai_embedding_dim=3,
        )
        super().__init__(settings=settings)
        self._vectors = vectors

    async def _embed_with_oci(
        self,
        texts: list[str],
        *,
        input_type: str,
    ) -> list[list[float]]:
        return self._vectors


class StubRerankClient(OciGenAiClient):
    """OCI rerank の戻り値だけを差し替えるテスト用 client。"""

    def __init__(self, results: list[tuple[int, float]]) -> None:
        settings = Settings.model_construct(
            oci_genai_embedding_dim=3,
        )
        super().__init__(settings=settings)
        self._results = results
        self.calls = 0

    async def _rerank_with_oci(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        self.calls += 1
        return self._results


class CountingEmbeddingClient(OciGenAiClient):
    """cache 挙動確認用の deterministic embedding client。"""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings=settings or _oci_settings())
        self.calls = 0
        self.batches: list[list[str]] = []

    async def _embed_with_oci(
        self,
        texts: list[str],
        *,
        input_type: str,
    ) -> list[list[float]]:
        _ = input_type
        self.calls += 1
        self.batches.append(list(texts))
        return [_vector_for_text(text) for text in texts]


class CountingRerankClient(OciGenAiClient):
    """cache 挙動確認用の deterministic rerank client。"""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings=settings or _oci_settings())
        self.calls = 0
        self.calls_args: list[tuple[str, list[str], int]] = []

    async def _rerank_with_oci(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        self.calls += 1
        self.calls_args.append((query, list(documents), top_n))
        return [(index, 0.9 - (index * 0.1)) for index in range(min(len(documents), top_n))]


class FakePostJson:
    """OpenAI 互換の API の最小 fake（#8。HTTP は送らない）。"""

    def __init__(
        self,
        embed_response: dict[str, Any] | None = None,
        rerank_response: dict[str, Any] | None = None,
    ) -> None:
        self._embed_response = embed_response or {"data": []}
        self._rerank_response = rerank_response or {"results": []}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, body))
        return self._rerank_response if url.endswith("/reranks") else self._embed_response

    def requests(self, suffix: str) -> list[dict[str, Any]]:
        return [body for url, body in self.calls if url.endswith(suffix)]

    def urls(self, suffix: str) -> list[str]:
        return [url for url, _ in self.calls if url.endswith(suffix)]


def _oci_settings() -> Settings:
    return Settings.model_construct(
        oci_genai_embedding_model="text-embedding-v4",
        oci_genai_embedding_dim=3,
        oci_genai_rerank_model="qwen3-rerank",
        oci_enterprise_ai_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        oci_enterprise_ai_api_key="sk-test",
        oci_enterprise_ai_timeout_seconds=60,
    )


def _vector_for_text(text: str) -> list[float]:
    return [float(len(text.encode("utf-8"))), 0.0, 0.0]


async def _run_inline(operation: Callable[[], Any]) -> Any:
    """テストでは同期 fake を同一 thread で実行する。"""
    return operation()


@pytest.mark.parametrize(
    "input_type,purpose",
    [
        ("SEARCH_QUERY", "query.embed"),
        ("SEARCH_DOCUMENT", "document.embed"),
        ("CLASSIFICATION", None),
        ("CLUSTERING", None),
    ],
)
@pytest.mark.parametrize("probe", [False, True])
async def test_embedding_purpose_uses_declared_input_type_and_preserves_probe(
    monkeypatch: pytest.MonkeyPatch,
    input_type: str,
    purpose: str | None,
    probe: bool,
) -> None:
    from app.clients import oci_genai

    captured: list[dict[str, object]] = []

    @contextmanager
    def capture(
        name: str, *, kind: str, attributes: dict[str, object]
    ) -> Iterator[SimpleNamespace]:
        assert name == "gen_ai.embeddings" and kind == "client"
        captured.append(attributes)
        yield SimpleNamespace(
            set_call_input=lambda *args, **kwargs: None,
            set_call_output=lambda *args, **kwargs: None,
        )

    monkeypatch.setattr(oci_genai, "observed_span", capture)
    post = FakePostJson(embed_response={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})
    client = OciGenAiClient(settings=_oci_settings(), post=post, sdk_call_runner=_run_inline)
    with model_call_purpose("connection.check" if probe else None):
        result = await client.embed(["合成の入力"], input_type=input_type)  # type: ignore[arg-type]
    assert result == [[1.0, 0.0, 0.0]]
    assert captured[0].get("pr.gen_ai.purpose") == ("connection.check" if probe else purpose)
    assert captured[0]["gen_ai.request.model"] == "text-embedding-v4"
    # input_type は purpose にだけ使い、API へは送らない（#8）。
    assert post.requests("/embeddings") == [
        {"model": "text-embedding-v4", "input": ["合成の入力"], "dimensions": 3}
    ]


async def test_rerank_cache_normalizes_only_query_presentation() -> None:
    """原文送信を保持し、同じ質問の表記差だけをキャッシュで吸収する。"""
    client = CountingRerankClient()
    original = "ＡＢＣ  設定"
    first = await client.rerank(original, ["本文 A", "本文 B"], top_n=2)
    assert await client.rerank("ABC\t設定", ["本文 A", "本文 B"], top_n=2) == first
    assert client.calls_args == [(original, ["本文 A", "本文 B"], 2)]

    for query in ["ABC 設定 追加条件", "ABC設定", "abc 設定"]:
        await client.rerank(query, ["本文 A", "本文 B"], top_n=2)
    await client.rerank(original, ["本文 B", "本文 A"], top_n=2)
    await client.rerank(original, ["本文 A", "本文 C"], top_n=2)
    await client.rerank(original, ["本文 A", "本文 B"], top_n=1)
    assert client.calls == 7


def test_rerank_cache_key_keeps_model_boundary() -> None:
    from app.clients.oci_genai import _rerank_cache_key

    assert _rerank_cache_key(
        query="ABC 設定", documents=["本文 A"], top_n=1, model_id="model-a"
    ) != _rerank_cache_key(query="ABC 設定", documents=["本文 A"], top_n=1, model_id="model-b")
