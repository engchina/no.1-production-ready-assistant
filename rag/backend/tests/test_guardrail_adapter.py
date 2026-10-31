"""Guardrail アダプター(安全ポリシー)のテスト。"""

import pytest
from rag_pipeline_core.guardrail import (
    DEFAULT_GROUNDING_MIN_OVERLAP,
    DEFAULT_GROUNDING_MIN_RATIO,
)

from app.config import Settings
from app.rag.guardrail_adapter import (
    GUARDRAIL_POLICY_ORDER,
    guardrail_adapter_runtime_settings,
    normalize_guardrail_policy,
    reset_guardrail_static_cache,
    resolve_guardrail_adapter,
)


class StageSettings(Settings):
    """RAG では使わない独立ステージの合成入力。"""

    rag_guardrail_policy: str = "standard"
    guardrail_block_prompt_injection: bool = True
    guardrail_mask_sensitive_identifiers: bool = True
    guardrail_max_query_chars: int = 2000


def test_standard_policy_reproduces_current_thresholds_and_flags() -> None:
    """standard は既存フラグと現行 groundedness 閾値を再現する(後方互換)。"""
    params = resolve_guardrail_adapter(
        StageSettings(
            rag_guardrail_policy="standard",
            guardrail_block_prompt_injection=True,
            guardrail_mask_sensitive_identifiers=True,
            guardrail_max_query_chars=2000,
        )
    )
    assert params.policy == "standard"
    assert params.block_prompt_injection is True
    assert params.mask_sensitive_identifiers is True
    assert params.max_query_chars == 2000
    assert params.grounding_min_overlap == DEFAULT_GROUNDING_MIN_OVERLAP
    assert params.grounding_min_ratio == DEFAULT_GROUNDING_MIN_RATIO
    assert params.audit_emphasis is False


def test_strict_raises_groundedness_threshold() -> None:
    params = resolve_guardrail_adapter(StageSettings(rag_guardrail_policy="strict"))
    assert params.grounding_min_overlap > DEFAULT_GROUNDING_MIN_OVERLAP
    assert params.grounding_min_ratio > DEFAULT_GROUNDING_MIN_RATIO
    assert params.audit_emphasis is False


def test_lenient_lowers_groundedness_threshold() -> None:
    params = resolve_guardrail_adapter(StageSettings(rag_guardrail_policy="lenient"))
    assert params.grounding_min_overlap < DEFAULT_GROUNDING_MIN_OVERLAP
    assert params.grounding_min_ratio < DEFAULT_GROUNDING_MIN_RATIO


def test_regulated_is_strict_with_audit_emphasis() -> None:
    params = resolve_guardrail_adapter(StageSettings(rag_guardrail_policy="regulated"))
    assert params.grounding_min_overlap > DEFAULT_GROUNDING_MIN_OVERLAP
    assert params.audit_emphasis is True


def test_runtime_settings_orders_and_marks_selected() -> None:
    runtime = guardrail_adapter_runtime_settings(StageSettings(rag_guardrail_policy="strict"))
    assert tuple(status.name for status in runtime.policies) == GUARDRAIL_POLICY_ORDER
    selected = [status.name for status in runtime.policies if status.selected]
    assert selected == ["strict"]


def test_normalize_guardrail_policy_defaults() -> None:
    assert normalize_guardrail_policy("nope") == "standard"
    assert normalize_guardrail_policy("regulated") == "regulated"


def test_static_resolution_is_cached_per_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """静的閾値の解決は backend の中で、policy あたり 1 回だけ行う(毎リクエスト回避。#3218)。"""
    from rag_pipeline_core.guardrail import resolve_guardrail

    reset_guardrail_static_cache()
    calls = {"n": 0}

    def counting(policy: object) -> object:
        calls["n"] += 1
        return resolve_guardrail(policy)

    monkeypatch.setattr("app.rag.guardrail_adapter.resolve_guardrail", counting)

    settings = StageSettings(rag_guardrail_policy="strict")
    first = resolve_guardrail_adapter(settings)
    second = resolve_guardrail_adapter(settings)

    assert calls["n"] == 1
    assert first.grounding_min_overlap == second.grounding_min_overlap == 5
