"""OCI Generative AI クライアント（埋め込み / リランク）。

埋め込み: Cohere Embed v4（1536 次元）。
リランク: Cohere Rerank v4 fast。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable, Sequence
from numbers import Real
from typing import Any, Literal, Protocol

from app.clients.oci_auth import load_oci_config_without_prompt
from app.settings import Settings, get_settings

EmbeddingInputType = Literal["SEARCH_DOCUMENT", "SEARCH_QUERY", "CLASSIFICATION", "CLUSTERING"]
EMBEDDING_INPUT_TYPES = frozenset(
    {"SEARCH_DOCUMENT", "SEARCH_QUERY", "CLASSIFICATION", "CLUSTERING"}
)
type SdkCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]


class GenerativeAiInferenceClientProtocol(Protocol):
    """OCI Generative AI Inference client の最小インターフェース。"""

    def embed_text(self, embed_text_details: object) -> Any:
        """OCI Generative AI embed_text を呼び出す。"""

    def rerank_text(self, rerank_text_details: object) -> Any:
        """OCI Generative AI rerank_text を呼び出す。"""


class OciGenAiClient:
    """OCI Generative AI による埋め込み / リランククライアント。"""

    def __init__(
        self,
        settings: Settings | None = None,
        inference_client: GenerativeAiInferenceClientProtocol | None = None,
        sdk_call_runner: SdkCallRunner | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._inference_client = inference_client
        self._sdk_call_runner = sdk_call_runner or _run_sdk_call_in_thread

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        """テキストを 1536 次元ベクトルに埋め込む（Cohere Embed v4）。"""
        if input_type not in EMBEDDING_INPUT_TYPES:
            raise ValueError(f"embedding input_type が不正です。input_type={input_type}")
        if not texts:
            return []
        vectors = await self._embed_with_oci(texts, input_type=input_type)
        _validate_embedding_batch(
            vectors,
            expected_count=len(texts),
            expected_dim=self._settings.oci_genai_embedding_dim,
        )
        return vectors

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """Cohere Rerank v4 fast で再ランク付けし、(index, score) を返す。"""
        if top_n < 1:
            raise ValueError(f"rerank top_n は 1 以上である必要があります。actual={top_n}")
        if not documents:
            return []
        results = await self._rerank_with_oci(query, documents, top_n)
        return _validate_rerank_results(results, document_count=len(documents), top_n=top_n)

    async def _embed_with_oci(
        self,
        texts: list[str],
        *,
        input_type: EmbeddingInputType,
    ) -> list[list[float]]:
        """OCI Generative AI Embedding 呼び出し。"""
        models = importlib.import_module("oci.generative_ai_inference.models")
        details = _model_details(
            models.EmbedTextDetails,
            inputs=texts,
            serving_mode=models.OnDemandServingMode(
                model_id=self._settings.oci_genai_embedding_model
            ),
            compartment_id=self._settings.oci_compartment_id,
            input_type=input_type,
            output_dimensions=self._settings.oci_genai_embedding_dim,
        )
        response = await self._sdk_call_runner(lambda: self._client().embed_text(details))
        embeddings = getattr(getattr(response, "data", response), "embeddings", None)
        if not isinstance(embeddings, list):
            raise ValueError("OCI embedding response に embeddings がありません。")
        return [[float(value) for value in vector] for vector in embeddings]

    async def _rerank_with_oci(
        self, query: str, documents: list[str], top_n: int
    ) -> Sequence[tuple[object, object]]:
        """OCI Generative AI Rerank 呼び出し。"""
        models = importlib.import_module("oci.generative_ai_inference.models")
        details = _model_details(
            models.RerankTextDetails,
            input=query,
            documents=documents,
            serving_mode=models.OnDemandServingMode(model_id=self._settings.oci_genai_rerank_model),
            compartment_id=self._settings.oci_compartment_id,
            top_n=top_n,
        )
        response = await self._sdk_call_runner(lambda: self._client().rerank_text(details))
        document_ranks = getattr(getattr(response, "data", response), "document_ranks", None)
        if not isinstance(document_ranks, list):
            raise ValueError("OCI rerank response に document_ranks がありません。")
        return [
            (getattr(rank, "index", None), getattr(rank, "relevance_score", None))
            for rank in document_ranks
        ]

    def _client(self) -> GenerativeAiInferenceClientProtocol:
        """OCI Generative AI Inference client を遅延初期化する。"""
        if self._inference_client is not None:
            return self._inference_client

        oci_config = importlib.import_module("oci.config")
        genai = importlib.import_module("oci.generative_ai_inference")
        config = load_oci_config_without_prompt(
            oci_config,
            self._settings.oci_config_file,
            self._settings.resolved_oci_config_profile,
        )
        endpoint = self._settings.oci_genai_endpoint.strip()
        if endpoint:
            self._inference_client = genai.GenerativeAiInferenceClient(
                config, service_endpoint=endpoint
            )
        else:
            self._inference_client = genai.GenerativeAiInferenceClient(config)
        return self._inference_client


async def _run_sdk_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期 OCI SDK 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)


def _model_details(model_cls: type[object], **attrs: object) -> object:
    """OCI SDK model の constructor 差異を吸収して属性を設定する。"""
    try:
        return model_cls(**attrs)
    except TypeError:
        details = model_cls()
        for key, value in attrs.items():
            setattr(details, key, value)
        return details


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
                f"embedding 次元が不正です。index={index}, expected={expected_dim}, "
                f"actual={len(values)}"
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
        raise ValueError("OCI rerank response に有効な rank がありません。")
    return sorted(validated, key=lambda item: item[1], reverse=True)[:top_n]


def _coerce_float(value: object) -> float:
    """SDK 応答値を score/vector 用 float へ変換する。"""
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"数値へ変換できない値です: {type(value).__name__}")
