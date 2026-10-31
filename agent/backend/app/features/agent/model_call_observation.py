"""モデルの呼び出しごとの入力の構成・指紋・予算を、モデルの呼び出しの span の属性にする（#2583）。

何が入力を占めたか、どの変更で入力の先頭（指示・ツールの定義）が変わったかを、本文を含めずに
調べられるようにする。値は既存のモデルの呼び出しの span（`gen_ai.chat`。送り直しは `pr.attempt`
の別の span）に付け、属性は platform の allowlist（`pr_backend_core.observability.tracing`）の名前
だけにする。

- **指紋**: 指示の変わらない部分（業務 Agent の指示・スキル・データの範囲の案内）・指示の全体・
  ツールの定義の集合の sha256 の先頭 16 桁。同じ構成なら同じ値で、指示・schema が変われば変わる。
  指紋の一致はプロバイダの cache の hit の証明ではない（cache の実測は usage の
  `gen_ai.usage.cache_read.input_tokens`。プロバイダが返さなければ属性を付けない＝不明で、0 と
  区別する。`pr_backend_core.observability.genai`）。
- **構成**: 入力の部分ごとの token の見積もり（`context_window.input_composition`。予算と同じ
  見積もり）。見積もりと実測（`gen_ai.usage.input_tokens`）の差は同じ span で比べる。
- **予算と縮め方**: Run の入力の予算（`_ContextWindow`）・残り・縮める前後の見積もり・縮めた
  ツールの結果の数。Run の外の呼び出し（評価・下書きの作成）は構成と指紋だけ。

呼び出しごとの usage は span に、Run の累計は SDK の `Usage`（`RunUsage`。承認の再開・差し戻しも
SDK の累計を引き継いで上書きする）にだけ持ち、span の値を累計に足さない（二重に数えない）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.features.agent.context_window import (
    CompactionResult,
    ContextBudget,
    ContextLimits,
    input_composition,
    split_instructions,
)

# 指紋の桁数（sha256 の 16 進の先頭。64 bit）。
FINGERPRINT_CHARS = 16
# Run の外の呼び出しの見積もり（係数 1。予算は無い）。
_PLAIN_BUDGET = ContextBudget(ContextLimits(model_id="", max_input_tokens=0, max_output_tokens=0))


def fingerprint(value: object) -> str:
    """値の指紋（文字列はそのまま、ほかは key を並べた JSON の sha256 の先頭）。"""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value


def model_call_attributes(
    request: Mapping[str, Any],
    *,
    budget: ContextBudget | None = None,
    static_instructions: str | None = None,
    provider_limit: int | None = None,
    compaction: CompactionResult | None = None,
) -> dict[str, int | str]:
    """モデルに送る要求（`responses.create` の引数）の、span に付ける属性（本文は含めない）。

    `budget`・`provider_limit`・`compaction` は Run の入力の管理（`_ContextWindow`）の値、
    `static_instructions` は指示の変わらない部分（`build_sdk_agent` が作る）。
    """
    raw_instructions = request.get("instructions")
    instructions = raw_instructions if isinstance(raw_instructions, str) else ""
    raw_tools = request.get("tools")
    tools = [_plain(tool) for tool in raw_tools] if isinstance(raw_tools, list | tuple) else []
    static, _dynamic = split_instructions(instructions, static_instructions)
    composition = input_composition(
        request.get("input"),
        budget=budget or _PLAIN_BUDGET,
        instructions=instructions,
        static_instructions=static_instructions,
        tools=tools,
    )
    values: dict[str, int | str] = {
        "pr.context.instructions.static_fingerprint": fingerprint(static),
        "pr.context.instructions.fingerprint": fingerprint(instructions),
        "pr.context.tools.fingerprint": fingerprint(tools),
        "pr.context.tools.count": len(tools),
        "pr.context.input.items": composition.items,
        "pr.context.estimate.static_instructions_tokens": composition.static_instructions_tokens,
        "pr.context.estimate.dynamic_state_tokens": composition.dynamic_state_tokens,
        "pr.context.estimate.tool_definitions_tokens": composition.tool_definitions_tokens,
        "pr.context.estimate.history_tokens": composition.history_tokens,
        "pr.context.estimate.tool_outputs_tokens": composition.tool_outputs_tokens,
        "pr.context.estimate.input_tokens": composition.total_tokens,
    }
    if budget is not None:
        available = budget.input_tokens(provider_limit)
        values["pr.context.budget.input_tokens"] = available
        values["pr.context.budget.remaining_tokens"] = available - composition.total_tokens
    if compaction is not None:
        values["pr.context.compaction.tokens_before"] = compaction.tokens_before
        values["pr.context.compaction.tokens_after"] = compaction.tokens_after
        values["pr.context.compaction.outputs"] = len(compaction.outputs)
    return values


__all__ = ["FINGERPRINT_CHARS", "fingerprint", "model_call_attributes"]
