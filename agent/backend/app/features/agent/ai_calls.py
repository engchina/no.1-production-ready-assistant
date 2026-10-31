"""一括の LLM の呼び出しの上限つきの並行の実行器を作る（#2004）。

共通の部品 ``pr_backend_core.bounded_calls`` に、Agent の設定（並行の上限）と、組み込み Runtime
の例外の分類を渡す。品質評価の判定（LLM-as-judge）は、job の中の呼び出しが 1 つの実行器を共有し、
429 を受けたら全体で待って並行の数を下げる（ばらばらに再試行して再び 429 にならない）。

SDK の再試行との分担: この実行器を通す呼び出しは openai SDK の再試行を切る
（``ModelTarget.sdk_max_retries=0``）。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import replace

from pr_backend_core.bounded_calls import (
    BoundedCaller,
    BoundedCallPolicy,
    ErrorKind,
    chain_classifiers,
)
from pr_system_settings.model import (
    enterprise_ai_retry_output_token_budget,
    lower_reasoning_effort,
)

from app.features.agent import builtin_runtime
from app.settings import get_settings

STAGE_LABELS: dict[str, str] = {
    "evaluation_judge": "品質評価の判定",
    "agent_drafting": "業務 Agent の下書きの提案",
}

# モデル・接続が設定されていない（設定の誤り。待っても直らない）。
_CONFIGURATION_CODES = frozenset(
    {"runtime.model_not_configured", "runtime.model_connection_not_configured"}
)


def agent_error_kind(exc: BaseException) -> ErrorKind | None:
    """組み込み Runtime の例外のうち、共通の分類では決まらないものの種類。"""
    if isinstance(exc, builtin_runtime.BuiltinRuntimeError):
        if exc.code == builtin_runtime.MODEL_OUTPUT_INCOMPLETE_CODE:
            return ErrorKind.TRUNCATED
        if exc.code in _CONFIGURATION_CODES:
            return ErrorKind.NOT_FOUND
    return None


def evaluation_concurrency() -> int:
    """品質評価で同時に実行するケースの数の上限（``AGENT_EVALUATION_CONCURRENCY``）。"""
    return max(1, int(get_settings().agent_evaluation_concurrency or 1))


async def _sleep(seconds: float) -> None:
    """再試行の待ち（テストは差し替えて実時間で待たない）。"""
    await asyncio.sleep(seconds)


def _random() -> float:
    """jitter の乱数（暗号の用途ではない）。"""
    return random.random()  # nosec B311 - backoff の jitter で、暗号の用途ではない


def make_ai_caller(stage: str, *, max_concurrency: int) -> BoundedCaller:
    """段の実行器を作る（1 つの job ごと）。"""
    return BoundedCaller(
        f"agent.{stage}",
        BoundedCallPolicy(max_concurrency=max(1, max_concurrency)),
        label=STAGE_LABELS.get(stage, stage),
        classifier=chain_classifiers(agent_error_kind),
        sleep=lambda seconds: _sleep(seconds),
        random=lambda: _random(),
    )


def retry_model_target(target: builtin_runtime.ModelTarget) -> builtin_runtime.ModelTarget:
    """出力の上限で止まった構造化出力を送り直すときのモデル（品質評価の判定・下書きの提案。#1845・#2023）。

    広げられれば出力の上限を広げ、広げられなければ推論の量を 1 段下げる。

    既定の上限はもうモデルの出力の上限のことが多く、そのときは同じ上限で送り直す（実環境では、
    出力が上限よりずっと少ないのに `max_output_tokens` で止まる応答があり、送り直すと判定できた。
    #1816 の評価の記録）。
    """
    widened = enterprise_ai_retry_output_token_budget(
        get_settings(), target.model_id, current=target.max_output_tokens
    )
    if widened:
        return replace(target, max_output_tokens=widened)
    # 広げられなければ推論の量を 1 段下げる（#2023。high → medium → low。low・none は下げない）。
    lower = lower_reasoning_effort(target.reasoning_effort)
    return replace(target, reasoning_effort=lower) if lower else target
