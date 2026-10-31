"""埋め込み / 再ランクのクライアント（設定画面のモデルのテスト用。#8）。

接続先は OpenAI 互換の API。既定は Aliyun 百煉（DashScope）の `text-embedding-v4`
（Oracle のベクトル列に合わせ `dimensions=EMBEDDING_DIMENSIONS` を必ず送る。#46）と `qwen3-rerank`。
既定の埋め込み / 再ランクのモデル（登録モデルの種類 embedding / rerank。#45）の接続の endpoint と
API key で呼び、再ランクは `/compatible-mode/` を `/compatible-api/` に読み替える。
LLM/VLM には使わない。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from numbers import Real
from typing import Any, Literal, Protocol

from pr_system_settings.model import embedding_api_base_url, embedding_model_connection

from app.settings import Settings, get_settings

EmbeddingInputType = Literal["SEARCH_DOCUMENT", "SEARCH_QUERY", "CLASSIFICATION", "CLUSTERING"]
EMBEDDING_INPUT_TYPES = frozenset(
    {"SEARCH_DOCUMENT", "SEARCH_QUERY", "CLASSIFICATION", "CLUSTERING"}
)
type SdkCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]


class PostJson(Protocol):
    """`POST {url}` に JSON を送り、JSON を返す（テストの差し替え口。#8）。"""

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]: ...


class OciGenAiClient:
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

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        """テキストを `oci_genai_embedding_dim`（`EMBEDDING_DIMENSIONS`）次元のベクトルに埋め込む。

        `input_type` は呼び出し側の互換のために受け取るだけ（OpenAI 互換の `/embeddings`
        に query / document の区別は無い）。
        """
        if input_type not in EMBEDDING_INPUT_TYPES:
            raise ValueError(f"embedding input_type が不正です。input_type={input_type}")
        if not texts:
            return []
        url = f"{self._base_url(rerank=False)}/embeddings"
        body: dict[str, Any] = {
            "model": self._settings.oci_genai_embedding_model,
            "input": texts,
            "dimensions": self._settings.oci_genai_embedding_dim,
        }
        payload = await self._sdk_call_runner(lambda: self._post(url, body, rerank=False))
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError("embedding の応答に data がありません。")
        return _validate_embedding_batch(
            _embedding_rows_in_input_order(rows, expected_count=len(texts)),
            expected_count=len(texts),
            expected_dim=self._settings.oci_genai_embedding_dim,
        )

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """`POST {base_url}/reranks` で再ランク付けし、(index, score) を返す。"""
        if top_n < 1:
            raise ValueError(f"rerank top_n は 1 以上である必要があります。actual={top_n}")
        if not documents:
            return []
        url = f"{self._base_url(rerank=True)}/reranks"
        body: dict[str, Any] = {
            "model": self._settings.oci_genai_rerank_model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        payload = await self._sdk_call_runner(lambda: self._post(url, body, rerank=True))
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("rerank の応答に results がありません。")
        ranks = [
            (result.get("index"), result.get("relevance_score"))
            for result in results
            if isinstance(result, dict)
        ]
        return _validate_rerank_results(ranks, document_count=len(documents), top_n=top_n)

    def _base_url(self, *, rerank: bool) -> str:
        """埋め込み / 再ランクの OpenAI 互換 Base URL（#8 / #45。`embedding_api_base_url`）。

        既定の埋め込み / 再ランクのモデルの接続の endpoint を使う。再ランクは
        `/compatible-mode/` を `/compatible-api/` に読み替える（百煉は再ランクだけ
        `/compatible-api/v1` 配下で、`/compatible-mode/v1` に投げると 404）。
        """
        base = embedding_api_base_url(self._settings, rerank=rerank)
        if not base:
            raise ValueError(
                "埋め込み / 再ランクの Base URL が未設定です。"
                "埋め込み / 再ランクのモデルの接続の Endpoint URL を入力してください。"
            )
        return base

    def _api_key(self, *, rerank: bool) -> str:
        """API key（#45。既定の埋め込み / 再ランクのモデルの接続のものを使う）。"""
        key = embedding_model_connection(self._settings, rerank=rerank).api_key
        if not key:
            raise ValueError(
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
        timeout = max(1, int(self._settings.oci_enterprise_ai_timeout_seconds or 60))
        # HTTPError はそのまま伝播させる（`status` を持つので呼び出し側が種類を判断できる）。
        # file:// などを開かないよう、scheme を http(s) に限定する（bandit B310）。
        if request.type not in ("http", "https"):
            raise ValueError(f"URL は http(s) にしてください: {request.type}://…")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{url} の応答が object ではありません。")
        return payload


async def _run_sdk_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期の HTTP 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)


def _embedding_rows_in_input_order(rows: list[Any], *, expected_count: int) -> list[list[object]]:
    """`/embeddings` の応答を入力の順に並べ直す（`data[].index` が入力の位置）。"""
    if len(rows) != expected_count:
        raise ValueError(
            f"embedding response 件数が不正です。expected={expected_count}, actual={len(rows)}"
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
    vectors: list[list[object]] = []
    for position, row in enumerate(ordered):
        raw = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(raw, list):
            raise ValueError(f"embedding の応答の #{position + 1} に embedding がありません。")
        vectors.append(raw)
    return vectors


def _validate_embedding_batch(
    vectors: Sequence[Sequence[object]],
    *,
    expected_count: int,
    expected_dim: int,
) -> list[list[float]]:
    """embedding response の件数と次元を検証する。"""
    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding response 件数が不正です。expected={expected_count}, actual={len(vectors)}"
        )
    validated: list[list[float]] = []
    for index, vector in enumerate(vectors):
        values = [_coerce_float(value) for value in vector]
        if len(values) != expected_dim:
            raise ValueError(
                f"embedding の次元数が不正です。expected={expected_dim}, "
                f"actual={len(values)}, index={index}"
            )
        validated.append(values)
    return validated


def _validate_rerank_results(
    results: Sequence[tuple[object, object]],
    *,
    document_count: int,
    top_n: int,
) -> list[tuple[int, float]]:
    """rerank response を検証し、score 降順で返す。"""
    validated: list[tuple[int, float]] = []
    for index, score in results:
        if not isinstance(index, int) or index < 0 or index >= document_count:
            continue
        validated.append((index, _coerce_float(score)))
    if not validated:
        raise ValueError("rerank の応答に有効な rank がありません。")
    return sorted(validated, key=lambda item: item[1], reverse=True)[:top_n]


def _coerce_float(value: object) -> float:
    """応答値を score/vector 用 float へ変換する。"""
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"数値へ変換できない値です: {type(value).__name__}")
