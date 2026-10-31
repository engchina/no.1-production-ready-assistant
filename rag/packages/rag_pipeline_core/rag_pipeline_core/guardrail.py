"""Guardrail ポリシーの決定論解決(backend の中で使う)。

policy → groundedness 厳格度(min_overlap / min_ratio)+ audit_emphasis を決定論で解決する。
NeMo Guardrails 的な strict/regulated は「より厳しい groundedness + 監査強調」へ再マップ済み。
block_prompt_injection / PII マスク / max_query_chars は backend 設定由来のため backend 側で
上乗せする（OCI Generative AI Guardrails は #11 で削除した）。
Settings 非依存(policy 名のみ)。外部安全 SaaS は導入しない。
"""

from __future__ import annotations

from dataclasses import dataclass

GUARDRAIL_POLICIES: tuple[str, ...] = ("standard", "strict", "lenient", "regulated")
DEFAULT_GUARDRAIL_POLICY = "standard"

# standard(settings 既定値)を表す sentinel。
DEFAULT_GROUNDING_MIN_OVERLAP = 3
DEFAULT_GROUNDING_MIN_RATIO = 0.12


@dataclass(frozen=True)
class GuardrailSpec:
    name: str
    origin: str
    recommended_for: tuple[str, ...]
    grounding_min_overlap: int | None  # None は standard(settings 既定値)
    grounding_min_ratio: float | None
    audit_emphasis: bool = False


GUARDRAIL_SPECS: dict[str, GuardrailSpec] = {
    "standard": GuardrailSpec("standard", "reference_policy", ("general", "balanced"), None, None),
    "strict": GuardrailSpec(
        "strict", "nemo_guardrails_strict", ("low_hallucination", "sensitive"), 5, 0.30
    ),
    "lenient": GuardrailSpec("lenient", "recall_first", ("exploratory", "internal"), 2, 0.05),
    "regulated": GuardrailSpec(
        "regulated",
        "compliance_audit",
        ("compliance", "regulated"),
        5,
        0.30,
        audit_emphasis=True,
    ),
}


@dataclass(frozen=True)
class GuardrailResolved:
    policy: str
    grounding_min_overlap: int
    grounding_min_ratio: float
    audit_emphasis: bool


def normalize_guardrail_policy(value: object) -> str:
    normalized = str(value).casefold()
    return normalized if normalized in GUARDRAIL_SPECS else DEFAULT_GUARDRAIL_POLICY


def resolve_guardrail(policy: object) -> GuardrailResolved:
    """policy から groundedness 厳格度 + audit_emphasis を解決する(standard は既定値)。"""
    name = normalize_guardrail_policy(policy)
    spec = GUARDRAIL_SPECS[name]
    return GuardrailResolved(
        policy=name,
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
