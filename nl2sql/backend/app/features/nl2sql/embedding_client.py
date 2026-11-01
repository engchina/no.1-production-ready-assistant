"""埋め込みの呼び出しの境界（feedback の学習で使う vector index 用）。

LLM/VLM は Enterprise AI、埋め込みは専用の接続という AGENTS.md の役割分担を保つため、
埋め込みの呼び出しはここに閉じ込める。接続先は OpenAI 互換の API で、既定は Aliyun
百煉（DashScope）の `text-embedding-v4`（#8）。HTTP を叩くのはこのファイルだけ。

endpoint と API key は既定の埋め込みモデル（登録モデルの種類 embedding）の接続のもの（#45）。
CI / local は設定が無ければ `is_configured()` が False を返し、deterministic に動く。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Protocol

from pr_backend_core.bounded_calls import Attempt, BoundedCallAborted
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
from pr_system_settings.model import embedding_api_base_url, embedding_model_connection

from app.settings import Settings

from .ai_calls import sync_ai_caller

# 百煉の OpenAI 互換 API は 1 要求 10 件まで。超えると 400 が返る。
EMBED_BATCH_SIZE = 10
# Oracle のベクトル列と互換にするため `EMBEDDING_DIMENSIONS` に固定（#46）。
EMBED_DIMENSIONS = EMBEDDING_DIMENSIONS


class EmbeddingClientError(RuntimeError):
    """Embedding client の実行時エラー。"""


class FeedbackEmbeddingClient(Protocol):
    """Feedback learning 用 embedding client contract."""

    def is_configured(self) -> bool:
        """Return whether live embedding execution is configured."""

    def module_available(self) -> bool:
        """Return whether the provider SDK can be imported."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into `EMBED_DIMENSIONS`-dimensional vectors."""


class OpenAiCompatibleEmbeddingClient:
    """OpenAI 互換の `POST {base_url}/embeddings` を呼ぶ client（#8）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def base_url(self) -> str:
        """埋め込みの Base URL（既定の埋め込みモデルの接続の endpoint。#45）。"""
        return embedding_api_base_url(self.settings)

    def api_key(self) -> str:
        """埋め込みの API key（既定の埋め込みモデルの接続のもの。#45）。"""
        return embedding_model_connection(self.settings).api_key

    def is_configured(self) -> bool:
        return bool(
            self.settings.nl2sql_feedback_embedding_enabled
            and self.base_url()
            and self.api_key()
            and self.settings.oci_genai_embed_model_id
        )

    def module_available(self) -> bool:
        """HTTP は標準ライブラリで送るので、追加の SDK は要らない。"""
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """10 件ずつの要求を上限つきの並行（``NL2SQL_EMBEDDING_CONCURRENCY``。#2004）で送る。

        結果は入力の順。429・5xx・接続・時間切れの再試行と全体の待ちは共通の実行器が受け持つ。
        失敗は ``EmbeddingClientError``（元の例外は ``__cause__``）。
        """
        if not texts:
            return []
        if not self.is_configured():
            raise EmbeddingClientError("埋め込みの設定が不足しています。")
        url = f"{self.base_url()}/embeddings"
        key = self.api_key()
        model = self.settings.oci_genai_embed_model_id
        timeout = max(1, int(getattr(self.settings, "oci_enterprise_ai_timeout_seconds", 60) or 60))
        batches = [
            texts[offset : offset + EMBED_BATCH_SIZE]
            for offset in range(0, len(texts), EMBED_BATCH_SIZE)
        ]

        def embed_batch(batch: list[str], _attempt: Attempt) -> list[list[float]]:
            payload = _post_json(
                url,
                {"model": model, "input": batch, "dimensions": EMBED_DIMENSIONS},
                key,
                timeout,
            )
            return _vectors(payload, expected_count=len(batch))

        try:
            with sync_ai_caller(self.settings, "embedding") as bridge:
                outcomes = bridge.map(batches, embed_batch)
        except BoundedCallAborted as exc:
            raise EmbeddingClientError(f"埋め込みに失敗しました: {exc}") from exc
        vectors: list[list[float]] = []
        for batch, outcome in zip(batches, outcomes, strict=True):
            if outcome.error is not None:
                cause = outcome.error.__cause__ or outcome.error
                if isinstance(cause, EmbeddingClientError):
                    raise cause
                raise EmbeddingClientError(f"埋め込みに失敗しました: {cause}") from cause
            batch_vectors = outcome.value or []
            if len(batch_vectors) != len(batch):
                raise EmbeddingClientError("埋め込みの応答の件数が入力の件数と一致しません。")
            vectors.extend(batch_vectors)
        return vectors


def _post_json(url: str, body: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    """埋め込みの要求を 1 回送る。**HTTP を叩くのはここだけ。**"""
    request = urllib.request.Request(  # noqa: S310 — 設定の Base URL は https を期待する
        url,
        method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    # HTTPError はそのまま伝播させる。`status` を持つので共通の分類器が種類（429 の
    # 絞り・403 の権限など）を決め、再試行の可否を判断できる。
    # file:// などを開かないよう、scheme を http(s) に限定する（bandit B310）。
    if request.type not in ("http", "https"):
        raise ValueError(f"URL は http(s) にしてください: {request.type}://…")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise EmbeddingClientError("埋め込みの応答が object ではありません。")
    return payload


def _vectors(payload: dict[str, Any], *, expected_count: int) -> list[list[float]]:
    """応答から入力の順でベクトルを取り出す（`data[].index` が入力の位置）。"""
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise EmbeddingClientError("埋め込みの応答に data がありません。")
    if len(rows) != expected_count:
        raise EmbeddingClientError("埋め込みの応答の件数が入力の件数と一致しません。")

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
            raise EmbeddingClientError(
                f"埋め込みの応答の #{position + 1} に embedding がありません。"
            )
        vector = [float(value) for value in raw]
        if len(vector) != EMBED_DIMENSIONS:
            raise EmbeddingClientError(
                f"embedding 次元が {EMBED_DIMENSIONS} ではありません: {len(vector)}"
            )
        vectors.append(vector)
    return vectors
