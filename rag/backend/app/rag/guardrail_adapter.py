"""独立した旧ステージの静的解決。RAG の安全チェックからは呼び出さない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rag_pipeline_core.guardrail import (
    DEFAULT_GROUNDING_MIN_OVERLAP,
    DEFAULT_GROUNDING_MIN_RATIO,
    GUARDRAIL_POLICIES,
    GUARDRAIL_SPECS,
    resolve_guardrail,
)
from rag_pipeline_core.guardrail import (
    normalize_guardrail_policy as _core_normalize,
)

from app.config import Settings

GuardrailStaticPolicy = Literal["standard", "strict", "lenient", "regulated"]
DEFAULT_GUARDRAIL_POLICY: GuardrailStaticPolicy = "standard"
GUARDRAIL_POLICY_ORDER: tuple[GuardrailStaticPolicy, ...] = GUARDRAIL_POLICIES  # type: ignore[assignment]


@dataclass(frozen=True)
class GuardrailAdapterParams:
    """ガードレール段へ渡す解決済み effective パラメータ。"""

    policy: GuardrailStaticPolicy
    block_prompt_injection: bool
    mask_sensitive_identifiers: bool
    max_query_chars: int
    grounding_min_overlap: int
    grounding_min_ratio: float
    audit_emphasis: bool


@dataclass(frozen=True)
class GuardrailStaticPolicyStatus:
    """1 安全ポリシーの選択状態と groundedness 厳格度。"""

    name: GuardrailStaticPolicy
    origin: str
    recommended_for: tuple[str, ...]
    selected: bool
    grounding_min_overlap: int
    grounding_min_ratio: float
    audit_emphasis: bool


@dataclass(frozen=True)
class GuardrailAdapterRuntimeSettings:
    """Guardrail アダプターの非機密 runtime snapshot。"""

    policy: GuardrailStaticPolicy
    block_prompt_injection: bool
    mask_sensitive_identifiers: bool
    max_query_chars: int
    grounding_min_overlap: int
    grounding_min_ratio: float
    audit_emphasis: bool
    policies: tuple[GuardrailStaticPolicyStatus, ...]


def normalize_guardrail_policy(value: object) -> GuardrailStaticPolicy:
    """未知のポリシー名は既定 standard へ寄せる。"""
    return _core_normalize(value)  # type: ignore[return-value]


def resolve_guardrail_adapter(settings: Settings) -> GuardrailAdapterParams:
    """Settings から Guardrail アダプターの effective パラメータを作る。

    静的 groundedness/audit は core で解決し、backend 設定由来の安全レバーを上乗せする。
    """
    policy = normalize_guardrail_policy(
        getattr(settings, "rag_guardrail_policy", DEFAULT_GUARDRAIL_POLICY)
    )
    overlap, ratio, audit = _resolve_static(policy)
    return GuardrailAdapterParams(
        policy=policy,
        block_prompt_injection=bool(getattr(settings, "guardrail_block_prompt_injection", True)),
        mask_sensitive_identifiers=bool(
            getattr(settings, "guardrail_mask_sensitive_identifiers", True)
        ),
        max_query_chars=int(getattr(settings, "guardrail_max_query_chars", 2000)),
        grounding_min_overlap=overlap,
        grounding_min_ratio=ratio,
        audit_emphasis=audit,
    )


# policy→静的閾値はプロセス内で不変(定数)。解決結果を policy 名でメモ化する
# (policy あたり最大 1 回)。
_STATIC_CACHE: dict[str, tuple[int, float, bool]] = {}


def reset_guardrail_static_cache() -> None:
    """静的解決メモ化をクリアする(テスト isolation 用)。"""
    _STATIC_CACHE.clear()


def _resolve_static(policy: str) -> tuple[int, float, bool]:
    """静的 (grounding_min_overlap, grounding_min_ratio, audit_emphasis) を解決する。"""
    cached = _STATIC_CACHE.get(policy)
    if cached is not None:
        return cached
    resolved = resolve_guardrail(policy)
    result = (resolved.grounding_min_overlap, resolved.grounding_min_ratio, resolved.audit_emphasis)
    _STATIC_CACHE[policy] = result
    return result


def guardrail_adapter_runtime_settings(settings: Settings) -> GuardrailAdapterRuntimeSettings:
    """Settings から Guardrail アダプター readiness snapshot を作る。"""
    params = resolve_guardrail_adapter(settings)
    statuses = tuple(
        GuardrailStaticPolicyStatus(
            name=spec.name,  # type: ignore[arg-type]
            origin=spec.origin,
            recommended_for=spec.recommended_for,
            selected=spec.name == params.policy,
            grounding_min_overlap=(
                spec.grounding_min_overlap
                if spec.grounding_min_overlap is not None
                else DEFAULT_GROUNDING_MIN_OVERLAP
            ),
            grounding_min_ratio=(
                spec.grounding_min_ratio
                if spec.grounding_min_ratio is not None
                else DEFAULT_GROUNDING_MIN_RATIO
            ),
            audit_emphasis=spec.audit_emphasis,
        )
        for spec in (GUARDRAIL_SPECS[name] for name in GUARDRAIL_POLICIES)
    )
    return GuardrailAdapterRuntimeSettings(
        policy=params.policy,
        block_prompt_injection=params.block_prompt_injection,
        mask_sensitive_identifiers=params.mask_sensitive_identifiers,
        max_query_chars=params.max_query_chars,
        grounding_min_overlap=params.grounding_min_overlap,
        grounding_min_ratio=params.grounding_min_ratio,
        audit_emphasis=params.audit_emphasis,
        policies=statuses,
    )
