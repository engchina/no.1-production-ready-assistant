"""描画済み根拠の文字上限と、system・schema・出力予約を含む入力予算。"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from rag_engine.config import Settings, get_settings
from rag_engine.generation.section_expansion import estimate_tokens


class GenerationInputBudgetExceeded(RuntimeError):
    """固定の指示または必須の根拠が入力予算に入らず、生成を送信しない。"""

    def __init__(self, diagnostic: dict[str, Any]) -> None:
        super().__init__("generation_input_budget_exceeded")
        self.diagnostic = diagnostic


def model_input_measurement(
    settings: Settings | None, system: str, prompt: str, schema: Mapping[str, Any], *, provider_id: str | None = None
) -> dict[str, Any]:
    settings = settings or get_settings(environ={}, dotenv_path=None)
    provider = settings.llm_providers.get(provider_id or settings.default_answer_llm)
    reserve = provider.max_output_tokens if provider else settings.answer_max_tokens
    limit = max(0, settings.answer_max_input_tokens - reserve)
    encoded = json.dumps(
        {
            "input": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "text": {"format": {"type": "json_schema", "strict": True, "schema": schema}},
        },
        ensure_ascii=False,
    )
    tokens = estimate_tokens(encoded)
    return {
        "model_input_limit_tokens": settings.answer_max_input_tokens,
        "output_reserved_tokens": reserve,
        "available_input_tokens": limit,
        "estimated_input_tokens": tokens,
        "token_count_method": "estimated_ascii_quarter_non_ascii_one",
        "fits": tokens <= limit,
    }


def ensure_model_input(settings: Settings, system: str, prompt: str, schema: Mapping[str, Any], *, provider_id: str | None = None) -> None:
    diagnostic = model_input_measurement(settings, system, prompt, schema, provider_id=provider_id)
    if not diagnostic["fits"]:
        diagnostic["reason"] = "final_model_input_exceeds_limit"
        raise GenerationInputBudgetExceeded(diagnostic)


def fit_generation_evidence(
    spans: Sequence[dict[str, Any]],
    settings: Settings,
    *,
    render: Callable[[Sequence[dict[str, Any]]], str],
    evidence_text: Callable[[Sequence[dict[str, Any]]], str],
    system: str,
    schema: Mapping[str, Any],
    provider_id: str | None = None,
    protected_ids: Collection[str] = (),
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """任意の根拠を順位の後ろから外す。必須の本文や既存草稿の引用は途中で切らない。"""
    char_limit = settings.evidence_budget_chars
    protected = set(protected_ids)
    required = {i for i, span in enumerate(spans) if span.get("pinned") or span.get("evidence_id") in protected}
    optional = [i for i in range(len(spans)) if i not in required]

    def candidate(count: int) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        included = required | set(optional[:count])
        selected = [span for i, span in enumerate(spans) if i in included]
        prompt = render(selected)
        block_chars = len(evidence_text(selected))
        measurement = model_input_measurement(settings, system, prompt, schema, provider_id=provider_id)
        diagnostic = {
            "configured_chars": char_limit,
            "rendered_chars": block_chars,
            **measurement,
            "excluded_span_count": len(spans) - len(selected),
            "fits": block_chars <= char_limit and measurement["fits"],
        }
        return selected, prompt, diagnostic

    selected, prompt, diagnostic = candidate(len(optional))
    if diagnostic["fits"]:
        return selected, prompt, diagnostic
    low = 0
    high = len(optional)
    selected, prompt, diagnostic = candidate(0)
    if not diagnostic["fits"]:
        diagnostic["reason"] = "required_evidence_or_fixed_input_exceeds_limit"
        raise GenerationInputBudgetExceeded(diagnostic)
    # 任意の根拠は関連度順のprefixだけを残し、二分探索後に採用する表示を作り直す。
    while low < high:
        middle = (low + high + 1) // 2
        if candidate(middle)[2]["fits"]:
            low = middle
        else:
            high = middle - 1
    return candidate(low)
