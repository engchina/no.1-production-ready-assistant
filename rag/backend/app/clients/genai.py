"""埋め込み / 再ランクのクライアント（#8）。

接続先は OpenAI 互換の API。既定は Aliyun 百煉（DashScope）の `text-embedding-v4`
（`EMBEDDING_DIMENSIONS` 次元。#46）と `qwen3-rerank`。LLM/VLM には使わない。
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import urllib.request
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from numbers import Real
from typing import Any, Literal, Protocol

from pr_backend_core.bounded_calls import KIND_LABELS, Attempt, BoundedCallFailed, ErrorKind
from pr_backend_core.chat_progress import chat_progress_details
from pr_backend_core.observability.model_purpose import model_purpose_attributes
from pr_backend_core.observability.tracing import observed_span
from pr_system_settings.model import embedding_api_base_url, embedding_model_connection
from rag_engine.resources.rerank_diagnostics import (
    RerankCallReceipt,
    bind_rerank_attempt,
    current_rerank_attempt,
    current_rerank_receipt,
    use_rerank_receipt,
)
from rag_engine.retrieval.query_identity import normalized_query_key

from app.clients.ai_calls import embedding_rejection_reason, ingestion_ai_calls
from app.config import Settings, get_settings

TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[ぁ-んァ-ン一-龯々ー]+", re.IGNORECASE)
EmbeddingInputType = Literal["SEARCH_DOCUMENT", "SEARCH_QUERY", "CLASSIFICATION", "CLUSTERING"]
EMBEDDING_INPUT_TYPES = frozenset(
    {"SEARCH_DOCUMENT", "SEARCH_QUERY", "CLASSIFICATION", "CLUSTERING"}
)
# ponytail: Cohere Embed v4 128k tokens への保守的な文字数ガード。
# tokenizer は実際に必要になるまで入れない。
EMBEDDING_INPUT_MAX_CHARS = 100_000
# 埋め込みの API の 1 request 内の全入力の上限（Cohere Embed 4 では 128k token）。
# tokenizer 依存を増やさず保守的に抑える。
EMBEDDING_REQUEST_MAX_CHARS = 100_000
EMBEDDING_REQUEST_MAX_INPUTS = 96
# 1 件の入力の上限(Embed 4 が inputs[i] ごとに検証する。#2467 で実 API の応答から確認)。
EMBEDDING_INPUT_MAX_TOKENS = 128_000
# 再試行でバッチを分けるときの最大の分割数(1 回の呼び出しの中で順に送る)。
EMBEDDING_RETRY_MAX_SPLITS = 8
type SdkCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]


class PostJson(Protocol):
    """`POST {url}` に JSON を送り、JSON を返す（テストの差し替え口。#8）。"""

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]: ...


logger = logging.getLogger(__name__)


class GenAiConfigError(ValueError):
    """利用者が埋め込み・再ランクのモデルの設定を直せる設定エラー。"""

    safe_for_user = True


class EmbeddingRequestError(RuntimeError):
    """Embedding の要求の失敗を、理由とバッチの位置・大きさで示す(#2467)。

    サービスの応答本文・入力の本文・認証情報は含めない。``diagnostics`` は構造化ログと同じ値。
    """

    safe_for_user = True

    def __init__(self, message: str, *, diagnostics: dict[str, object]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class GenAiClient:
    """OpenAI 互換の API による埋め込み / 再ランクのクライアント（#8）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        post: PostJson | None = None,
        sdk_call_runner: SdkCallRunner | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # `post` はテストの差し替え口（本番は `_post`。HTTP を叩くのはそこだけ）。
        self._post_json = post
        self._sdk_call_runner = sdk_call_runner or _run_sdk_call_in_thread
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._rerank_cache: OrderedDict[str, list[tuple[int, float]]] = OrderedDict()

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        """テキストを `genai_embedding_dim`（`EMBEDDING_DIMENSIONS`）次元に埋め込む。"""
        if input_type not in EMBEDDING_INPUT_TYPES:
            raise ValueError(f"embedding input_type が不正です。input_type={input_type}")
        if not texts:
            return []
        _validate_embedding_input_lengths(texts)
        _raise_if_empty_embedding_input(texts, model_id=self._settings.genai_embedding_model)
        if self._embedding_cache_disabled():
            vectors = await self._embed_batches(texts, input_type=input_type)
            _validate_embedding_batch(
                vectors,
                expected_count=len(texts),
                expected_dim=self._settings.genai_embedding_dim,
            )
            return vectors

        vectors = await self._embed_with_cache(texts, input_type=input_type)
        _validate_embedding_batch(
            vectors,
            expected_count=len(texts),
            expected_dim=self._settings.genai_embedding_dim,
        )
        return vectors

    async def _embed_with_cache(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        """cache hit を再利用し、miss だけをまとめて 埋め込みモデル に出す。"""
        vectors: list[list[float] | None] = [None] * len(texts)
        miss_texts: list[str] = []
        miss_keys: list[str] = []
        miss_positions: dict[str, list[int]] = {}
        for position, text in enumerate(texts):
            key = _embedding_cache_key(
                text=text,
                input_type=input_type,
                model_id=self._settings.genai_embedding_model,
                dimension=self._settings.genai_embedding_dim,
            )
            cached = self._get_cached_embedding(key)
            if cached is not None:
                vectors[position] = cached
                continue
            positions = miss_positions.setdefault(key, [])
            positions.append(position)
            if len(positions) == 1:
                miss_keys.append(key)
                miss_texts.append(text)

        if miss_texts:
            missing_vectors = await self._embed_batches(
                miss_texts,
                input_type=input_type,
            )
            _validate_embedding_batch(
                missing_vectors,
                expected_count=len(miss_texts),
                expected_dim=self._settings.genai_embedding_dim,
            )
            for key, vector in zip(miss_keys, missing_vectors, strict=True):
                self._store_embedding(key, vector)
                for position in miss_positions[key]:
                    vectors[position] = list(vector)

        return [_require_cached_vector(vector, index) for index, vector in enumerate(vectors)]

    async def _embed_batches(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        """埋め込みの request を設定値で分割し、入力順に結合して返す。

        batch は上限つきの並行で送る(#2002。上限は ``rag_embedding_concurrency``)。429・5xx・
        接続・timeout の再試行と全体の待ちは共通の実行器が受け持つ(SDK の再試行は切っている)。
        結果は入力の順に結合する。最後の再試行でも失敗した batch があれば、その失敗を出す。
        """
        batch_size = min(
            EMBEDDING_REQUEST_MAX_INPUTS,
            max(1, getattr(self._settings, "rag_embedding_batch_size", 96)),
        )
        batches = _embedding_request_batches(texts, max_count=batch_size)
        if not batches:
            return []

        async def embed_batch(batch: list[str], attempt: Attempt) -> list[list[float]]:
            # サービスの高負荷(HTTP 400 でも返る。#2467)のあとは、バッチを分けて小さい要求で送る。
            batch_vectors: list[list[float]] = []
            for part in _split_batch(batch, _retry_split_count(attempt, len(batch))):
                batch_vectors.extend(await self._embed_with_api(part, input_type=input_type))
            _validate_embedding_batch(
                batch_vectors,
                expected_count=len(batch),
                expected_dim=self._settings.genai_embedding_dim,
            )
            return batch_vectors

        with ingestion_ai_calls(self._settings, "embedding", total=len(batches)) as caller:
            outcomes = await _unwrapped_item_failure(caller.map(batches, embed_batch))
        vectors: list[list[float]] = []
        for index, outcome in enumerate(outcomes):
            if outcome.error is not None:
                cause = outcome.error.__cause__
                if outcome.error.status is None:
                    # 応答の検証の失敗など、サービスの HTTP の拒否でないものは元の例外で出す。
                    raise cause or outcome.error
                raise _embedding_request_error(
                    outcome.error,
                    cause if isinstance(cause, BaseException) else None,
                    batch=batches[index],
                    batch_index=index,
                    batch_total=len(batches),
                    total_inputs=len(texts),
                    model_id=self._settings.genai_embedding_model,
                ) from (cause or outcome.error)
            vectors.extend(outcome.value or [])
        return vectors

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """Cohere Rerank v4 fast で再ランク付けし、(index, score) を返す。"""
        receipt = current_rerank_receipt() or RerankCallReceipt()
        receipt.request(query, documents, model=self._settings.genai_rerank_model, top_n=top_n)
        with (
            use_rerank_receipt(receipt),
            observed_span(
                "rerank.request", kind="internal", attributes=receipt.attributes()
            ) as span,
        ):
            try:
                if top_n < 1:
                    raise ValueError(f"rerank top_n は 1 以上である必要があります。actual={top_n}")
                if not documents:
                    receipt.status = "skipped"
                    return []
                cache_key: str | None = None
                receipt.cache_state = "disabled" if self._rerank_cache_disabled() else "miss"
                if receipt.cache_state != "disabled":
                    cache_key = _rerank_cache_key(
                        query=query,
                        documents=documents,
                        top_n=top_n,
                        model_id=self._settings.genai_rerank_model,
                    )
                    cached = self._get_cached_rerank(cache_key)
                    if cached is not None:
                        receipt.cache_state = "hit"
                        receipt.status = "completed"
                        return cached

                async def rerank_once(attempt: Attempt) -> Sequence[tuple[object, object]]:
                    with bind_rerank_attempt(attempt.number):
                        return await self._rerank_with_api(query, documents, top_n)

                # SDK の再試行は切り、既存の共通実行器だけで再試行する。
                with ingestion_ai_calls(self._settings, "rerank", total=1) as caller:
                    results = await _unwrapped_item_failure(
                        caller.call(rerank_once, label="rerank")
                    )
                validated = _validate_rerank_results(
                    results, document_count=len(documents), top_n=top_n
                )
                if cache_key is not None:
                    self._store_rerank(cache_key, validated)
                receipt.status = "completed"
                return validated
            except asyncio.CancelledError:
                receipt.status = "cancelled"
                span.set_status("cancelled")
                raise
            except Exception as exc:
                receipt.status, receipt.error_type = "error", type(exc).__name__
                span.set_status("failed")
                raise
            finally:
                span.set_attributes(receipt.attributes())
                span.set_display(
                    details=chat_progress_details(
                        code=[("rerank", json.dumps(receipt.to_dict(), ensure_ascii=False), "json")]
                    )
                )

    def _base_url(self, *, rerank: bool) -> str:
        """埋め込み / 再ランクの OpenAI 互換 Base URL（#8 / #45。`embedding_api_base_url`）。

        既定の埋め込み / 再ランクのモデルの接続の endpoint を使う。再ランクは
        `/compatible-mode/` を `/compatible-api/` に読み替える（百煉は再ランクだけ
        `/compatible-api/v1` 配下で、`/compatible-mode/v1` に投げると 404）。
        """
        base = embedding_api_base_url(self._settings, rerank=rerank)
        if not base:
            raise GenAiConfigError(
                "埋め込み / 再ランクの Base URL が未設定です。"
                "埋め込み / 再ランクのモデルの接続の Endpoint URL を入力してください。"
            )
        return base

    def _api_key(self, *, rerank: bool) -> str:
        """API key（#45。既定の埋め込み / 再ランクのモデルの接続のものを使う）。"""
        key = embedding_model_connection(self._settings, rerank=rerank).api_key
        if not key:
            raise GenAiConfigError(
                "埋め込み / 再ランクの API key がありません"
                "（埋め込み / 再ランクのモデルの接続の API key を使います）。"
            )
        return key

    def _post(self, url: str, body: dict[str, Any], *, rerank: bool) -> dict[str, Any]:
        """OpenAI 互換の API を 1 回呼ぶ（#8）。**HTTP を叩くのはここだけ。**"""
        if self._post_json is not None:
            return self._post_json(url, body)
        request = urllib.request.Request(  # noqa: S310 — 設定の Base URL は https を期待する
            url,
            method="POST",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key(rerank=rerank)}",
            },
        )
        timeout = max(1, int(self._settings.enterprise_ai_timeout_seconds or 60))
        # HTTPError はそのまま伝播させる。`status` を持つので共通の分類器が種類
        # （429 の絞り・403 の権限など）を決め、再試行の可否を判断できる。
        # file:// などを開かないよう、scheme を http(s) に限定する（bandit B310）。
        if request.type not in ("http", "https"):
            raise ValueError(f"URL は http(s) にしてください: {request.type}://…")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{url} の応答が object ではありません。")
        return payload

    async def _embed_with_api(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        """OpenAI 互換の `POST {base_url}/embeddings` を呼ぶ（#8）。

        `input_type` は観測の purpose にだけ使う（OpenAI 互換の `/embeddings` に
        query / document の区別は無い）。LLM/VLM には使わず、埋め込み専用に限定する。
        """
        model = self._settings.genai_embedding_model
        url = f"{self._base_url(rerank=False)}/embeddings"
        body: dict[str, Any] = {
            "model": model,
            "input": texts,
            "dimensions": self._settings.genai_embedding_dim,
        }
        with observed_span(
            "gen_ai.embeddings",
            kind="client",
            attributes={
                "gen_ai.operation.name": "embeddings",
                "gen_ai.provider.name": "openai_compatible",
                "gen_ai.request.model": model,
                **model_purpose_attributes(
                    default={
                        "SEARCH_QUERY": "query.embed",
                        "SEARCH_DOCUMENT": "document.embed",
                    }.get(input_type)
                ),
            },
        ) as span:
            span.set_call_input(lambda: {"url": url, **body}, restricted=True)
            payload = await self._sdk_call_runner(lambda: self._post(url, body, rerank=False))
            span.set_call_output(lambda: payload)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError("embedding の応答に data がありません。")
        return _embedding_vectors_in_input_order(rows, expected_count=len(texts))

    async def _rerank_with_api(
        self, query: str, documents: list[str], top_n: int
    ) -> Sequence[tuple[object, object]]:
        """OpenAI 互換の `POST {base_url}/reranks` を呼ぶ（#8。複数形。`/rerank` は 404）。"""
        model = self._settings.genai_rerank_model
        url = f"{self._base_url(rerank=True)}/reranks"
        body: dict[str, Any] = {
            "model": model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        receipt = current_rerank_receipt()
        attempt_number = current_rerank_attempt()
        attempt: dict[str, Any] | None = None

        def invoke() -> dict[str, Any]:
            nonlocal attempt
            attempt = receipt.begin_sdk(attempt_number) if receipt is not None else None
            try:
                payload = self._post(url, body, rerank=True)
            except BaseException as exc:
                if receipt is not None and attempt is not None:
                    receipt.finish_sdk(
                        attempt,
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                        exc,
                    )
                raise
            if receipt is not None and attempt is not None:
                receipt.finish_sdk(attempt, "completed")
            return payload

        with observed_span(
            "gen_ai.rerank",
            kind="client",
            attributes={
                "gen_ai.operation.name": "rerank",
                "gen_ai.provider.name": "openai_compatible",
                "gen_ai.request.model": model,
                "rerank.attempt_number": attempt_number,
                "rerank.retry_number": max(0, attempt_number - 1),
                **(receipt.attributes() if receipt is not None else {}),
                **model_purpose_attributes(),
            },
        ) as span:
            span.set_call_input(lambda: {"url": url, **body}, restricted=True)
            try:
                payload = await self._sdk_call_runner(invoke)
                span.set_call_output(lambda: payload)
            finally:
                if receipt is not None:
                    span.set_attributes(receipt.attributes())
                    span.set_attributes(
                        {
                            "rerank.status_scope": "sdk_attempt",
                            "rerank.status": str(attempt["status"])
                            if attempt is not None
                            else "not_run",
                        }
                    )
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("rerank の応答に results がありません。")
        return [
            (result.get("index"), result.get("relevance_score"))
            for result in results
            if isinstance(result, dict)
        ]

    def _embedding_cache_disabled(self) -> bool:
        return not getattr(self._settings, "rag_embedding_cache_enabled", True) or (
            getattr(self._settings, "rag_embedding_cache_max_entries", 4096) <= 0
        )

    def _rerank_cache_disabled(self) -> bool:
        return not getattr(self._settings, "rag_rerank_cache_enabled", True) or (
            getattr(self._settings, "rag_rerank_cache_max_entries", 1024) <= 0
        )

    def _get_cached_embedding(self, key: str) -> list[float] | None:
        cached = self._embedding_cache.pop(key, None)
        if cached is None:
            return None
        self._embedding_cache[key] = cached
        return list(cached)

    def _store_embedding(self, key: str, vector: Sequence[float]) -> None:
        self._embedding_cache[key] = [float(value) for value in vector]
        self._trim_embedding_cache()

    def _trim_embedding_cache(self) -> None:
        max_entries = getattr(self._settings, "rag_embedding_cache_max_entries", 4096)
        while len(self._embedding_cache) > max_entries:
            self._embedding_cache.popitem(last=False)

    def _get_cached_rerank(self, key: str) -> list[tuple[int, float]] | None:
        cached = self._rerank_cache.pop(key, None)
        if cached is None:
            return None
        self._rerank_cache[key] = cached
        return list(cached)

    def _store_rerank(self, key: str, results: Sequence[tuple[int, float]]) -> None:
        self._rerank_cache[key] = [(int(index), float(score)) for index, score in results]
        self._trim_rerank_cache()

    def _trim_rerank_cache(self) -> None:
        max_entries = getattr(self._settings, "rag_rerank_cache_max_entries", 1024)
        while len(self._rerank_cache) > max_entries:
            self._rerank_cache.popitem(last=False)


async def _unwrapped_item_failure[T](awaitable: Awaitable[T]) -> T:
    """件の失敗は元の例外(SDK の例外・検証の失敗)で出す(呼び出し側の扱いを変えない)。

    job を止める失敗(``BoundedCallAborted``。権限・設定の誤りなど)は、直す所の案内の文のまま出す。
    """
    try:
        return await awaitable
    except BoundedCallFailed as exc:
        cause = exc.__cause__
        if isinstance(cause, Exception):
            raise cause from None
        raise


async def _run_sdk_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期の SDK 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)


def _embedding_vectors_in_input_order(rows: list[Any], *, expected_count: int) -> list[list[float]]:
    """`/embeddings` の応答を入力の順に並べ直す（#8。`data[].index` が入力の位置）。"""
    if len(rows) != expected_count:
        raise ValueError(
            f"embedding の応答の件数が入力と違います: expected {expected_count}, got {len(rows)}"
        )
    ordered: list[Any] = [None] * expected_count
    for position, row in enumerate(rows):
        index = position
        if isinstance(row, dict):
            try:
                index = int(row["index"])
            except (KeyError, TypeError, ValueError):
                index = position
        if not 0 <= index < expected_count or ordered[index] is not None:
            index = position
        ordered[index] = row
    vectors: list[list[float]] = []
    for position, row in enumerate(ordered):
        raw = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(raw, list):
            raise ValueError(f"embedding の応答の #{position + 1} に embedding がありません。")
        vectors.append([float(value) for value in raw])
    return vectors


def _embedding_cache_key(
    *,
    text: str,
    input_type: EmbeddingInputType,
    model_id: str,
    dimension: int,
) -> str:
    """embedding cache key を原文なしの安定 hash で作る。"""
    return _cache_key(
        {
            "kind": "embedding",
            "model_id": model_id,
            "dimension": dimension,
            "input_type": input_type,
            "text_sha256": _sha256_text(text),
        }
    )


def _rerank_cache_key(
    *,
    query: str,
    documents: list[str],
    top_n: int,
    model_id: str,
) -> str:
    """rerank cache key を query/document 原文なしの安定 hash で作る。"""
    return _cache_key(
        {
            "kind": "rerank",
            "model_id": model_id,
            "top_n": top_n,
            "query_sha256": _sha256_text(normalized_query_key(query)),
            "document_sha256": [_sha256_text(document) for document in documents],
        }
    )


def _cache_key(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_cached_vector(vector: list[float] | None, index: int) -> list[float]:
    if vector is None:
        raise ValueError(f"embedding[{index}] の cache 復元に失敗しました。")
    return list(vector)


def _validate_embedding_input_lengths(texts: Sequence[str]) -> None:
    for index, text in enumerate(texts):
        chars = len(text)
        if chars > EMBEDDING_INPUT_MAX_CHARS:
            raise ValueError(
                "embedding input が長すぎます。"
                f"index={index}, chars={chars}, max_chars={EMBEDDING_INPUT_MAX_CHARS}"
            )


def _raise_if_empty_embedding_input(texts: Sequence[str], *, model_id: str) -> None:
    """空の入力はサービスが HTTP 400 で拒否するため、送る前に止める(#2467)。"""
    empty = [index for index, text in enumerate(texts) if text == ""]
    if not empty:
        return
    diagnostics: dict[str, object] = {
        "reason": "input_empty",
        "total_inputs": len(texts),
        "empty_inputs": len(empty),
        "first_empty_index": empty[0],
        "model": model_id,
    }
    logger.warning("embedding_request_failed", extra=diagnostics)
    raise EmbeddingRequestError(
        f"Embedding の入力に空の Chunk が {len(empty)} 件あります（最初は {empty[0] + 1} 件目）。"
        "Chunk 作成から再実行してください。",
        diagnostics=diagnostics,
    )


def _retry_split_count(attempt: Attempt, size: int) -> int:
    """再試行のときにバッチを何個に分けて送るか(一時的な不可用・大きすぎるの後だけ分ける)。"""
    if size <= 1:
        return 1
    if attempt.final:
        return min(size, EMBEDDING_RETRY_MAX_SPLITS)
    if attempt.last_error not in {ErrorKind.UNAVAILABLE, ErrorKind.TOO_LARGE}:
        return 1
    splits: int = 2 ** max(0, attempt.number - 1)
    return min(size, EMBEDDING_RETRY_MAX_SPLITS, splits)


def _split_batch(batch: list[str], parts: int) -> list[list[str]]:
    """入力の順を保ったまま、件数がほぼ等しい ``parts`` 個に分ける。"""
    if parts <= 1:
        return [batch]
    size, extra = divmod(len(batch), parts)
    result: list[list[str]] = []
    start = 0
    for index in range(parts):
        end = start + size + (1 if index < extra else 0)
        if end > start:
            result.append(batch[start:end])
        start = end
    return result


_EMBEDDING_REASON_LABELS: dict[str, str] = {
    "service_overloaded": "サービスの高負荷",
    "input_too_long": "1 件の入力がトークンの上限を超えた",
    "too_many_inputs": "入力の件数が上限を超えた",
    "input_empty": "空の入力",
    "invalid_request": "要求の誤り",
}
_EMBEDDING_REASON_HINTS: dict[str, str] = {
    "service_overloaded": (
        "再試行しても回復しませんでした。時間をおいて、保存済みの Chunk から "
        "Embedding / 索引を再実行してください。"
    ),
    "input_too_long": (
        f"1 件の Chunk が Embedding の上限（{EMBEDDING_INPUT_MAX_TOKENS:,} トークン）を"
        "超えています。Chunk のサイズの設定を小さくして、Chunk 作成から再実行してください。"
    ),
    "too_many_inputs": "Embedding のバッチの件数の設定（96 件以下）を確かめてください。",
    "input_empty": "Chunk 作成から再実行してください。",
}
_DEFAULT_EMBEDDING_HINT = (
    "時間をおいて、保存済みの Chunk から Embedding / 索引を再実行してください。"
)


def _embedding_request_error(
    failure: BoundedCallFailed,
    cause: BaseException | None,
    *,
    batch: Sequence[str],
    batch_index: int,
    batch_total: int,
    total_inputs: int,
    model_id: str,
) -> EmbeddingRequestError:
    """失敗したバッチの診断(本文・入力を含めない)を構造化ログに出し、利用者向けの例外にする。"""
    status = failure.status
    service_code = getattr(cause, "code", None) if cause is not None else None
    reason = (embedding_rejection_reason(cause) if cause is not None else None) or (
        failure.kind.value
    )
    batch_chars = sum(len(text) for text in batch)
    # byte 単位の BPE の token は 1 byte 以上なので、UTF-8 の byte 数は token 数の上限になる
    # (tokenizer は持たない)。
    batch_utf8_bytes = sum(len(text.encode("utf-8")) for text in batch)
    diagnostics: dict[str, object] = {
        "reason": reason,
        "kind": failure.kind.value,
        "status": status,
        "service_code": str(service_code) if service_code is not None else None,
        "opc_request_id": getattr(cause, "request_id", None) if cause is not None else None,
        "attempts": failure.attempts,
        "batch_index": batch_index + 1,
        "batch_total": batch_total,
        "batch_inputs": len(batch),
        "batch_chars": batch_chars,
        "batch_max_input_chars": max((len(text) for text in batch), default=0),
        "batch_token_upper_bound": batch_utf8_bytes,
        "total_inputs": total_inputs,
        "model": model_id,
    }
    logger.warning("embedding_request_failed", extra=diagnostics)
    label = _EMBEDDING_REASON_LABELS.get(reason) or KIND_LABELS.get(failure.kind, "失敗")
    status_text = f"HTTP {status}・" if status is not None else ""
    hint = _EMBEDDING_REASON_HINTS.get(reason, _DEFAULT_EMBEDDING_HINT)
    return EmbeddingRequestError(
        f"Embedding の要求が失敗しました（{status_text}{label}）。"
        f"バッチ {batch_index + 1}/{batch_total}（{len(batch)} 件・{batch_chars:,} 文字）。{hint}",
        diagnostics=diagnostics,
    )


def _embedding_request_batches(texts: Sequence[str], *, max_count: int) -> list[list[str]]:
    """入力順を保ったまま件数と保守的な総文字数の両方で request を分割する。"""
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_chars = 0
    for text in texts:
        if batch and (
            len(batch) >= max_count or batch_chars + len(text) > EMBEDDING_REQUEST_MAX_CHARS
        ):
            batches.append(batch)
            batch = []
            batch_chars = 0
        batch.append(text)
        batch_chars += len(text)
    if batch:
        batches.append(batch)
    return batches


def _validate_embedding_batch(
    vectors: list[list[float]],
    expected_count: int,
    expected_dim: int,
) -> None:
    """Embedding adapter の返却件数と次元数を検証する。"""
    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding の件数が一致しません。expected={expected_count}, actual={len(vectors)}"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != expected_dim:
            raise ValueError(
                f"embedding[{index}] の次元数が不正です。"
                f"expected={expected_dim}, actual={len(vector)}"
            )


def _validate_rerank_results(
    results: Sequence[tuple[object, object]],
    *,
    document_count: int,
    top_n: int,
) -> list[tuple[int, float]]:
    """Rerank adapter の index/score 契約を検証し、score 降順に正規化する。"""
    expected_max = min(document_count, top_n)
    if len(results) > expected_max:
        message = f"rerank の返却件数が不正です。expected_max={expected_max}, actual={len(results)}"
        raise ValueError(message)

    seen_indexes: set[int] = set()
    validated: list[tuple[int, float]] = []
    for position, (index, score) in enumerate(results):
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f"rerank index の型が不正です。position={position}, index={index!r}")
        if index in seen_indexes:
            raise ValueError(f"rerank index が重複しています。index={index}")
        if index < 0 or index >= document_count:
            raise ValueError(
                f"rerank index が範囲外です。index={index}, document_count={document_count}"
            )
        if not isinstance(score, Real) or isinstance(score, bool):
            message = f"rerank score が不正です。index={index}, score={score!r}"
            raise ValueError(message)
        normalized_score = float(score)
        if not math.isfinite(normalized_score):
            message = f"rerank score が不正です。index={index}, score={score!r}"
            raise ValueError(message)
        seen_indexes.add(index)
        validated.append((index, normalized_score))

    return sorted(validated, key=lambda item: item[1], reverse=True)


def _tokens(text: str) -> list[str]:
    """日本語・英数字の簡易トークン化。"""
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]
