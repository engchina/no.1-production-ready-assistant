"""OCI Guardrails クライアントのテスト(SDK 非依存に挙動を検証)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.clients.oci_guardrails import (
    OciGuardrailsClient,
    OciGuardrailsUnavailableError,
    _parse_results,
)
from app.config import Settings


def test_is_configured_requires_backend_and_compartment() -> None:
    # backend=local は常に未設定扱い。
    assert (
        OciGuardrailsClient(
            Settings(rag_guardrails_input_enabled=False, rag_guardrails_output_enabled=False)
        ).is_configured()
        is False
    )
    # backend=oci_guardrails でも compartment が無ければ未設定。
    assert (
        OciGuardrailsClient(
            Settings(
                rag_guardrails_input_enabled=True,
                rag_guardrails_output_enabled=True,
                oci_compartment_id="",
            )
        ).is_configured()
        is False
    )
    # backend + compartment が揃えば設定済み。
    assert (
        OciGuardrailsClient(
            Settings(
                rag_guardrails_input_enabled=True,
                rag_guardrails_output_enabled=True,
                oci_compartment_id="ocid1.compartment",
            )
        ).is_configured()
        is True
    )


def test_inspect_text_raises_safe_error_when_unconfigured() -> None:
    client = OciGuardrailsClient(
        Settings(rag_guardrails_input_enabled=False, rag_guardrails_output_enabled=False)
    )
    with pytest.raises(OciGuardrailsUnavailableError):
        client.inspect_text("some text")


def test_inspect_text_empty_returns_none() -> None:
    client = OciGuardrailsClient(
        Settings(
            rag_guardrails_input_enabled=True,
            rag_guardrails_output_enabled=True,
            oci_compartment_id="ocid1.compartment",
        )
    )
    assert client.inspect_text("   ") is None


def _category(name: str, score: float, flagged_modalities: list[str] | None = None) -> object:
    """SDK の ``CategoryScore`` と同じ形(name / score / flagged_modalities)。"""
    return SimpleNamespace(name=name, score=score, flagged_modalities=flagged_modalities)


def test_parse_results_flags_injection_by_threshold() -> None:
    results = SimpleNamespace(
        content_moderation=SimpleNamespace(
            categories=[_category("OVERALL", 1.0), _category("BLOCKLIST", 0.0)]
        ),
        prompt_injection=SimpleNamespace(score=0.8, flagged_modalities=[]),
        personally_identifiable_information=[
            SimpleNamespace(label="EMAIL_ADDRESS", offset=4, length=16, text="should-not-be-kept"),
        ],
    )
    inspection = _parse_results(results, threshold=0.5)
    assert inspection.prompt_injection is True
    assert inspection.moderation_categories == ("OVERALL",)
    assert inspection.pii_labels == ("EMAIL_ADDRESS",)
    assert inspection.pii_spans[0].offset == 4
    assert inspection.pii_spans[0].length == 16
    assert "should-not-be-kept" not in repr(inspection)
    assert inspection.flagged is True


def test_parse_results_below_threshold_not_flagged() -> None:
    results = SimpleNamespace(
        content_moderation=SimpleNamespace(categories=[]),
        prompt_injection=SimpleNamespace(score=0.2, flagged_modalities=[]),
        personally_identifiable_information=[],
    )
    inspection = _parse_results(results, threshold=0.5)
    assert inspection.prompt_injection is False
    assert inspection.flagged is False


def test_parse_results_zero_moderation_scores_are_not_flagged() -> None:
    # ApplyGuardrails は検出が無くても OVERALL / BLOCKLIST を score 0 で返す(#2061)。
    results = SimpleNamespace(
        content_moderation=SimpleNamespace(
            categories=[_category("OVERALL", 0.0), _category("BLOCKLIST", 0.0)]
        ),
        prompt_injection=SimpleNamespace(score=0.0, flagged_modalities=None),
        personally_identifiable_information=None,
    )
    inspection = _parse_results(results, threshold=0.5)
    assert inspection.moderation_categories == ()
    assert inspection.flagged is False


def test_parse_results_moderation_flagged_modalities_count_as_detection() -> None:
    results = SimpleNamespace(
        content_moderation=SimpleNamespace(
            categories=[_category("OVERALL", 0.2, ["TEXT"]), _category("BLOCKLIST", 0.0)]
        ),
        prompt_injection=None,
        personally_identifiable_information=None,
    )
    assert _parse_results(results, threshold=0.5).moderation_categories == ("OVERALL",)


def test_parse_results_accepts_sdk_models() -> None:
    # 実の SDK の model で、検出なしの応答が flagged にならないことを確かめる。
    models = pytest.importorskip("oci.generative_ai_inference.models")
    clean = models.GuardrailsResults(
        content_moderation=models.ContentModerationResult(
            categories=[
                models.CategoryScore(name="OVERALL", score=0.0),
                models.CategoryScore(name="BLOCKLIST", score=0.0),
            ]
        ),
        prompt_injection=models.PromptInjectionProtectionResult(score=0.0),
        personally_identifiable_information=[],
    )
    assert _parse_results(clean, threshold=0.5).flagged is False
    flagged = models.GuardrailsResults(
        content_moderation=models.ContentModerationResult(
            categories=[
                models.CategoryScore(name="OVERALL", score=1.0),
                models.CategoryScore(name="BLOCKLIST", score=0.0),
            ]
        ),
        prompt_injection=models.PromptInjectionProtectionResult(score=0.0),
        personally_identifiable_information=[],
    )
    inspection = _parse_results(flagged, threshold=0.5)
    assert inspection.moderation_categories == ("OVERALL",)
    assert inspection.prompt_injection is False


def test_client_disables_sdk_retries_and_keeps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # SDK の既定の再試行を残すと、届かないときに縮退まで 1 分を超える(#2061)。
    import app.clients.oci_guardrails as module

    created: dict[str, object] = {}

    class _NoneRetryStrategy:
        pass

    class _NoCircuitBreakerStrategy:
        pass

    class _InferenceClient:
        def __init__(self, config: object, **kwargs: object) -> None:
            created["config"] = config
            created.update(kwargs)

    fake_modules = {
        "oci.config": SimpleNamespace(),
        "oci.generative_ai_inference": SimpleNamespace(
            GenerativeAiInferenceClient=_InferenceClient
        ),
        "oci.retry": SimpleNamespace(NoneRetryStrategy=_NoneRetryStrategy),
        "oci.circuit_breaker": SimpleNamespace(NoCircuitBreakerStrategy=_NoCircuitBreakerStrategy),
    }
    monkeypatch.setattr(
        "app.clients.oci_guardrails.importlib.import_module", lambda name: fake_modules[name]
    )
    monkeypatch.setattr(module, "load_oci_config_without_prompt", lambda *_args: {"region": "x"})
    client = OciGuardrailsClient(
        Settings(
            rag_guardrails_input_enabled=True,
            rag_guardrails_output_enabled=True,
            oci_compartment_id="ocid1.compartment",
            oci_guardrails_timeout_seconds=2.0,
        )
    )
    client._client()
    assert isinstance(created["retry_strategy"], _NoneRetryStrategy)
    assert isinstance(created["circuit_breaker_strategy"], _NoCircuitBreakerStrategy)  # #2211
    assert created["timeout"] == (2.0, 2.0)
