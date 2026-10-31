"""OCI Generative AI Guardrails クライアント。

OCI Generative AI の **ApplyGuardrails API**(`oci.generative_ai_inference`)を呼び、
テキストの content moderation / PII 検出 / prompt injection 検出を行う。回答生成は行わない
**検出専用 API** であり、AGENTS.md §1 の「chat 推論 API を LLM/VLM に使わない」には抵触しない
(ユーザ明示要望による OCI サービスの追加)。確定スタック(embedding/rerank=OCI GenAI、
回答 LLM/VLM=Enterprise AI、ベクトル DB=Oracle AI Database)は不変で、別 LLM provider・外部
ベクトル DB は導入しない。

未設定・SDK 失敗・呼び出し失敗時は非機密な例外へ変換し、呼び出し側が
失敗時は処理を止める。privacy: 検出結果は **PII の値そのものを保持せず、
offset / length / label のみ** を返す。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from app.clients.oci_auth import load_oci_config_without_prompt
from app.config import Settings

logger = logging.getLogger(__name__)

# content moderation のカテゴリを検出とみなす score(OCI の応答は 0.0 / 1.0)。
CONTENT_MODERATION_THRESHOLD = 0.5


class OciGuardrailsUnavailableError(RuntimeError):
    """OCI Guardrails が検査を完了できなかったことだけを表す非機密例外。"""


@dataclass(frozen=True)
class PiiSpan:
    """入力中の PII 位置。検出文字列そのものは保持しない。"""

    offset: int
    length: int
    label: str


@dataclass(frozen=True)
class GuardrailInspection:
    """ApplyGuardrails の非機密サマリ(PII の値は保持しない)。"""

    prompt_injection: bool = False
    prompt_injection_score: float = 0.0
    moderation_categories: tuple[str, ...] = ()
    pii_spans: tuple[PiiSpan, ...] = ()

    @property
    def pii_labels(self) -> tuple[str, ...]:
        """診断用の label 一覧。原値は含めない。"""
        return tuple(span.label for span in self.pii_spans)

    @property
    def flagged(self) -> bool:
        """いずれかの検出があったか。"""
        return bool(self.prompt_injection or self.moderation_categories or self.pii_labels)


class OciGuardrailsClient:
    """ApplyGuardrails を薄くラップし、失敗を非機密な単一例外へ変換する。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _compartment_id(self) -> str:
        return (
            str(getattr(self._settings, "oci_guardrails_compartment_id", "") or "").strip()
            or str(getattr(self._settings, "oci_compartment_id", "") or "").strip()
        )

    def is_configured(self) -> bool:
        """compartment が解決でき、かつ OCI Guardrails が有効のとき True。"""
        return (
            self._settings.rag_guardrails_input_enabled
            or self._settings.rag_guardrails_output_enabled
        ) and bool(self._compartment_id())

    def inspect_text(
        self,
        text: str,
        *,
        language_code: str = "ja",
        prompt_injection_threshold: float | None = None,
    ) -> GuardrailInspection | None:
        """text を ApplyGuardrails で検査する。失敗時は非機密な例外を送出する。"""
        if not text.strip():
            return None
        if not self.is_configured():
            raise OciGuardrailsUnavailableError("OCI Guardrails を利用できません。")
        try:
            return self._inspect(text, language_code, prompt_injection_threshold)
        except OciGuardrailsUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK 詳細を境界外へ出さない
            logger.warning(
                "oci_guardrails_inspect_failed",
                extra={"error": type(exc).__name__},
            )
            raise OciGuardrailsUnavailableError("OCI Guardrails を利用できません。") from exc

    def _client(self) -> object:
        # 既存 oci client と同じく importlib + oci_auth で SDK を遅延解決する
        # (mypy の import-untyped を避け、暗号化 PEM の対話プロンプトも防ぐ)。
        oci_config = importlib.import_module("oci.config")
        inference = importlib.import_module("oci.generative_ai_inference")
        config = load_oci_config_without_prompt(
            oci_config,
            getattr(self._settings, "oci_config_file", "~/.oci/config") or "~/.oci/config",
            getattr(self._settings, "oci_config_profile", "DEFAULT") or "DEFAULT",
        )
        endpoint = str(getattr(self._settings, "oci_guardrails_endpoint", "") or "").strip()
        kwargs: dict[str, object] = {}
        if endpoint:
            kwargs["service_endpoint"] = endpoint
        timeout = float(getattr(self._settings, "oci_guardrails_timeout_seconds", 5.0))
        kwargs["timeout"] = (min(3.0, timeout), timeout)
        # SDK の既定の再試行(DEFAULT_RETRY_STRATEGY)を切る。残すと届かないときに timeout を
        # 何度も待ち、検査失敗の通知まで 1 分を超える(#2061)。
        kwargs["retry_strategy"] = importlib.import_module("oci.retry").NoneRetryStrategy()
        # SDK の既定の circuit breaker も切る(失敗の扱いは呼び出し側のエラー処理が決める。#2211)。
        kwargs["circuit_breaker_strategy"] = importlib.import_module(
            "oci.circuit_breaker"
        ).NoCircuitBreakerStrategy()
        return inference.GenerativeAiInferenceClient(config, **kwargs)

    def _inspect(
        self, text: str, language_code: str, prompt_injection_threshold: float | None
    ) -> GuardrailInspection | None:
        models = importlib.import_module("oci.generative_ai_inference.models")

        threshold = (
            prompt_injection_threshold
            if prompt_injection_threshold is not None
            else float(getattr(self._settings, "oci_guardrails_prompt_injection_threshold", 0.5))
        )
        details = models.ApplyGuardrailsDetails(
            compartment_id=self._compartment_id(),
            input=models.GuardrailsTextInput(
                type="TEXT", content=text, language_code=language_code
            ),
            guardrail_configs=models.GuardrailConfigs(
                content_moderation_config=models.ContentModerationConfiguration(),
                personally_identifiable_information_config=(
                    models.PersonallyIdentifiableInformationConfiguration()
                ),
                prompt_injection_config=models.PromptInjectionConfiguration(),
            ),
        )
        response = self._client().apply_guardrails(details)  # type: ignore[attr-defined]
        results = getattr(getattr(response, "data", None), "results", None)
        if results is None:
            return None
        return _parse_results(results, threshold)


def _parse_results(results: object, threshold: float) -> GuardrailInspection:
    """ApplyGuardrails の results を非機密サマリへ変換する(PII の値は捨てる)。"""
    moderation = getattr(results, "content_moderation", None)
    categories = _flagged_moderation_categories(
        getattr(moderation, "categories", None) if moderation else None
    )

    injection_result = getattr(results, "prompt_injection", None)
    score = float(getattr(injection_result, "score", 0.0) or 0.0) if injection_result else 0.0
    flagged_modalities = (
        tuple(getattr(injection_result, "flagged_modalities", None) or ())
        if injection_result
        else ()
    )
    prompt_injection = bool(score >= threshold or flagged_modalities)

    pii_items = getattr(results, "personally_identifiable_information", None) or ()
    pii_spans = tuple(
        span
        for item in pii_items
        if item is not None
        for span in [_pii_span(item)]
        if span is not None
    )
    return GuardrailInspection(
        prompt_injection=prompt_injection,
        prompt_injection_score=score,
        moderation_categories=categories,
        pii_spans=pii_spans,
    )


def _flagged_moderation_categories(categories: Iterable[object] | None) -> tuple[str, ...]:
    """content moderation の ``CategoryScore`` から、検出したカテゴリの名前だけを返す。

    ApplyGuardrails は検出が無くても ``OVERALL`` / ``BLOCKLIST`` を ``score=0.0`` で返す。
    score が閾値以上か ``flagged_modalities`` があるカテゴリだけを検出とする(#2061)。
    """
    flagged: list[str] = []
    for category in categories or ():
        if category is None:
            continue
        score = float(getattr(category, "score", 0.0) or 0.0)
        modalities = getattr(category, "flagged_modalities", None) or ()
        if score < CONTENT_MODERATION_THRESHOLD and not modalities:
            continue
        name = str(getattr(category, "name", "") or "").strip()
        flagged.append(name or "UNKNOWN")
    return tuple(dict.fromkeys(flagged))


def _pii_span(item: object) -> PiiSpan | None:
    """SDK version 差を吸収して PII の位置情報だけを取り出す。"""
    offset = getattr(item, "offset", None)
    if offset is None:
        offset = getattr(item, "start_offset", None)
    length = getattr(item, "length", None)
    if length is None and offset is not None:
        end_offset = getattr(item, "end_offset", None)
        if end_offset is not None:
            length = int(end_offset) - int(offset)
    if offset is None or length is None or int(offset) < 0 or int(length) <= 0:
        return None
    return PiiSpan(
        offset=int(offset),
        length=int(length),
        label=str(getattr(item, "label", "") or "PII"),
    )
