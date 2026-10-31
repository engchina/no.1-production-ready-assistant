"""OCI Generative AI embedding client boundary for NL2SQL feedback learning.

LLM/VLM は Enterprise AI、embedding は OCI Generative AI という AGENTS.md の
役割分担を保つため、feedback vector index 用の embedding 呼び出しはここに閉じ込める。
CI/local は optional import のまま deterministic に動作する。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Protocol

from pr_backend_core.bounded_calls import Attempt, BoundedCallAborted

from app.settings import Settings

from .ai_calls import sync_ai_caller

OCI_GENAI_EMBED_BATCH_SIZE = 96


class EmbeddingClientError(RuntimeError):
    """Embedding client の実行時エラー。"""


class FeedbackEmbeddingClient(Protocol):
    """Feedback learning 用 embedding client contract."""

    def is_configured(self) -> bool:
        """Return whether live embedding execution is configured."""

    def module_available(self) -> bool:
        """Return whether the provider SDK can be imported."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into 1536-dimensional vectors."""


class OciGenAiEmbeddingClient:
    """OCI Generative AI Cohere Embed v4 client.

    The exact OCI SDK objects are imported lazily to keep tests and local development
    independent from live OCI credentials.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._oci: Any | None = None
        self._client: Any | None = None
        # client を作ったときの OCI 認証の設定（#2102）。別の製品・別の worker が OCI 認証を保存し、
        # `get_settings()` が読み直したら（または OCI config のファイルが書き換わったら）作り直す。
        self._client_key: tuple[object, ...] | None = None

    def is_configured(self) -> bool:
        return bool(
            self.settings.nl2sql_feedback_embedding_enabled
            and self.settings.oci_region
            and self.settings.oci_compartment_id
            and self.settings.oci_genai_embed_model_id
        )

    def module_available(self) -> bool:
        try:
            self._load_oci()
        except EmbeddingClientError:
            return False
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """96 件ずつの要求を上限つきの並行（``NL2SQL_EMBEDDING_CONCURRENCY``。#2004）で送る。

        結果は入力の順。429・5xx・接続・時間切れの再試行と全体の待ちは共通の実行器が受け持つ
        （OCI SDK の再試行は切る）。失敗は ``EmbeddingClientError``（元の例外は ``__cause__``）。
        """
        if not texts:
            return []
        if not self.is_configured():
            raise EmbeddingClientError("OCI GenAI embedding 設定が不足しています。")
        client = self._get_client()
        models = self._load_models()
        batches = [
            texts[offset : offset + OCI_GENAI_EMBED_BATCH_SIZE]
            for offset in range(0, len(texts), OCI_GENAI_EMBED_BATCH_SIZE)
        ]

        def embed_batch(batch: list[str], _attempt: Attempt) -> list[list[float]]:
            details = models.EmbedTextDetails()
            details.inputs = batch
            details.compartment_id = self.settings.oci_compartment_id
            details.serving_mode = models.OnDemandServingMode(
                model_id=self.settings.oci_genai_embed_model_id
            )
            if hasattr(details, "truncate"):
                details.truncate = "END"
            if hasattr(details, "input_type"):
                details.input_type = "SEARCH_DOCUMENT"
            response = client.embed_text(details)
            embeddings = getattr(response.data, "embeddings", None)
            if embeddings is None:
                raise EmbeddingClientError("OCI GenAI embedding 応答に embeddings がありません。")
            return [[float(value) for value in vector] for vector in embeddings]

        try:
            with sync_ai_caller(self.settings, "embedding") as bridge:
                outcomes = bridge.map(batches, embed_batch)
        except BoundedCallAborted as exc:
            raise EmbeddingClientError(f"OCI GenAI embedding に失敗しました: {exc}") from exc
        vectors: list[list[float]] = []
        for batch, outcome in zip(batches, outcomes, strict=True):
            if outcome.error is not None:
                cause = outcome.error.__cause__ or outcome.error
                if isinstance(cause, EmbeddingClientError):
                    raise cause
                message = f"OCI GenAI embedding に失敗しました: {cause}"
                raise EmbeddingClientError(message) from cause
            batch_vectors = outcome.value or []
            if len(batch_vectors) != len(batch):
                raise EmbeddingClientError("OCI GenAI embedding 応答件数が入力件数と一致しません。")
            for vector in batch_vectors:
                if len(vector) != 1536:
                    raise EmbeddingClientError(
                        f"embedding 次元が 1536 ではありません: {len(vector)}"
                    )
            vectors.extend(batch_vectors)
        return vectors

    def _load_oci(self) -> Any:
        if self._oci is not None:
            return self._oci
        try:
            self._oci = importlib.import_module("oci")
        except ModuleNotFoundError as exc:
            raise EmbeddingClientError("OCI SDK がインストールされていません。") from exc
        return self._oci

    def _load_models(self) -> Any:
        try:
            return importlib.import_module("oci.generative_ai_inference.models")
        except ModuleNotFoundError as exc:
            raise EmbeddingClientError(
                "OCI Generative AI Inference models が見つかりません。"
            ) from exc

    def _client_settings_key(self) -> tuple[object, ...]:
        """client の作り方を決める OCI 認証の設定と、OCI config のファイルの状態。"""
        settings = self.settings
        config_file = settings.oci_config_file or str(Path.home() / ".oci" / "config")
        try:
            stat = Path(config_file).expanduser().stat()
            config_state: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            config_state = None
        return (
            settings.oci_auth_mode.strip().lower(),
            config_file,
            settings.resolved_oci_config_profile,
            settings.oci_region,
            settings.oci_genai_endpoint,
            config_state,
        )

    def _get_client(self) -> Any:
        key = self._client_settings_key()
        if self._client is not None and self._client_key == key:
            return self._client
        oci = self._load_oci()
        inference = importlib.import_module("oci.generative_ai_inference")
        endpoint = self.settings.oci_genai_endpoint or (
            f"https://inference.generativeai.{self.settings.oci_region}.oci.oraclecloud.com"
        )
        auth_mode = self.settings.oci_auth_mode.strip().lower()
        signer = None
        config: dict[str, Any] = {}
        if auth_mode == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        elif auth_mode == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
        else:
            config_file = self.settings.oci_config_file or str(Path.home() / ".oci" / "config")
            config = oci.config.from_file(
                config_file,
                self.settings.resolved_oci_config_profile,
            )
        # 再試行は共通の上限つきの並行の実行器が受け持つ（#2004）。SDK の既定の再試行
        # （DEFAULT_RETRY_STRATEGY）を残すと、全体の待ち・並行の数の調整を通らずに再試行する。
        client_kwargs: dict[str, Any] = {"config": config, "service_endpoint": endpoint}
        retry = getattr(oci, "retry", None)
        if retry is not None:
            client_kwargs["retry_strategy"] = retry.NoneRetryStrategy()
        # SDK の既定の circuit breaker も切る。429 が続くと先に開き、
        # 実行器の知らない例外で失敗する（#2211）。
        circuit_breaker = getattr(oci, "circuit_breaker", None)
        if circuit_breaker is not None:
            client_kwargs["circuit_breaker_strategy"] = circuit_breaker.NoCircuitBreakerStrategy()
        if signer is not None:
            client_kwargs["signer"] = signer
        self._client = inference.GenerativeAiInferenceClient(**client_kwargs)
        self._client_key = key
        return self._client
