"""Run 全体の実行予算（モデルの呼び出し・token・稼働時間。#68）。

1 回の SDK 実行の上限（`max_turns`）・ツールの回数（`SupportTaskBudget`）・通信の timeout とは別に、
Run の最初の実行・差し戻し（根拠・手順・出力の項目の確認）・承認や確認の後の再開を通した合計を
1 つの予算で数える。

- **予約と精算**: モデルを呼ぶ直前（`call_model_input_filter`）に 1 回を予約し、入力の見積もりの
  token を足しても上限を超えないかを確かめる。応答の後（`on_llm_end`）に usage の実測で精算する。
  プロバイダが usage を返さない呼び出しは「実測できなかった呼び出し」として数え、予約した見積もりを
  消費に足す（実測と見積もりは分けて残す）。token の上限は入力の見積もりで予約するため、最後の
  1 回の出力の分（モデルの出力の上限まで）は上限を超えうる。料金は扱わない（金額の上限は
  保証しない）。
- **稼働時間**: 実行（開始・再開）ごとに計算・通信に使った時間だけを足す。承認・確認で人を待つ
  時間は実行の外なので数えない。
- **再開で戻さない**: 消費は Run の metadata（`run_budget`）に残し、次の実行はそこから続ける。
- **止め方**: 上限に達したら、新しいツールの呼び出しを実行せず（未実行として残す）、次のモデルの
  呼び出しの前に `runtime.run_budget_exceeded` で Run を失敗にする。失敗の説明は決まった文で、
  そのためにモデルを呼ばない（後始末の予算は 0）。実行中の呼び出しは中断しない（その呼び出しの
  timeout に任せる）。結果の無い step は「結果不明」として報告し、取り消されたとはみなさない。
- RAG / NL2SQL が内部で使ったモデルの token は、この Run の usage に入らない（二重に数えない。
  取得できない消費として扱う）。

上限は管理者が基線（`run_budget` の消費の記録）を測って設定する。0 以下はその上限を数えない
（既定。消費の記録は常に残す）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

RUN_BUDGET_METADATA_KEY = "run_budget"
RUN_BUDGET_EXCEEDED_CODE = "runtime.run_budget_exceeded"

SCOPE_MODEL_CALLS = "model_calls"
SCOPE_TOTAL_TOKENS = "total_tokens"
SCOPE_ACTIVE_SECONDS = "active_seconds"

_SCOPE_LABELS = {
    SCOPE_MODEL_CALLS: "モデルの呼び出し",
    SCOPE_TOTAL_TOKENS: "token",
    SCOPE_ACTIVE_SECONDS: "稼働時間（秒）",
}


@dataclass(frozen=True)
class RunBudgetLimits:
    """Run 全体の上限（0 以下は数えない）。"""

    model_calls: int = 0
    total_tokens: int = 0
    active_seconds: float = 0.0

    @classmethod
    def from_settings(cls, settings: Any) -> RunBudgetLimits:
        return cls(
            model_calls=int(settings.agent_run_max_model_calls),
            total_tokens=int(settings.agent_run_max_total_tokens),
            active_seconds=float(settings.agent_run_max_active_seconds),
        )

    def as_record(self) -> dict[str, int | float | None]:
        return {
            SCOPE_MODEL_CALLS: self.model_calls if self.model_calls > 0 else None,
            SCOPE_TOTAL_TOKENS: self.total_tokens if self.total_tokens > 0 else None,
            SCOPE_ACTIVE_SECONDS: self.active_seconds if self.active_seconds > 0 else None,
        }


def monotonic() -> float:
    """稼働時間の時計（テストで差し替える）。"""
    return time.monotonic()


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float) and value > 0 else 0


def _float(value: object) -> float:
    return float(value) if isinstance(value, int | float) and value > 0 else 0.0


@dataclass
class RunBudget:
    """1 回の実行（開始・再開）のあいだ、Run 全体の予算を数える（前の実行の消費から続ける）。"""

    limits: RunBudgetLimits
    model_calls: int = 0
    measured_tokens: int = 0
    unmeasured_calls: int = 0
    estimated_tokens: int = 0
    active_seconds_before: float = 0.0
    executions: int = 1
    # 予算で実行しなかったツールの呼び出し（この Run の通し）。
    refused_tool_calls: int = 0
    exceeded: dict[str, Any] | None = None
    clock: Callable[[], float] = field(default_factory=lambda: monotonic)
    _started: float = field(default=0.0, init=False)
    _reserved_tokens: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    @classmethod
    def for_run(
        cls,
        metadata: Mapping[str, Any] | None,
        limits: RunBudgetLimits,
    ) -> RunBudget:
        """Run の metadata に残した消費から続ける（承認・確認の後の再開で 0 に戻さない）。"""
        saved = metadata.get(RUN_BUDGET_METADATA_KEY) if isinstance(metadata, Mapping) else None
        consumed = saved.get("consumed") if isinstance(saved, Mapping) else None
        consumed = consumed if isinstance(consumed, Mapping) else {}
        exceeded = saved.get("exceeded") if isinstance(saved, Mapping) else None
        return cls(
            limits=limits,
            model_calls=_int(consumed.get(SCOPE_MODEL_CALLS)),
            measured_tokens=_int(consumed.get("measured_tokens")),
            unmeasured_calls=_int(consumed.get("unmeasured_calls")),
            estimated_tokens=_int(consumed.get("estimated_tokens")),
            active_seconds_before=_float(consumed.get(SCOPE_ACTIVE_SECONDS)),
            executions=_int(consumed.get("executions")) + 1,
            refused_tool_calls=_int(consumed.get("refused_tool_calls")),
            exceeded=dict(exceeded) if isinstance(exceeded, Mapping) else None,
        )

    @property
    def total_tokens(self) -> int:
        """実測の token と、usage が無かった呼び出しの見積もりの合計。"""
        return self.measured_tokens + self.estimated_tokens

    @property
    def active_seconds(self) -> float:
        return self.active_seconds_before + max(self.clock() - self._started, 0.0)

    def _over(self, *, extra_tokens: int = 0, next_call: bool = False) -> dict[str, Any] | None:
        limits = self.limits
        calls = self.model_calls + (1 if next_call else 0)
        if 0 < limits.model_calls < calls:
            return {
                "scope": SCOPE_MODEL_CALLS,
                "limit": limits.model_calls,
                "used": self.model_calls,
            }
        tokens = self.total_tokens + extra_tokens
        if limits.total_tokens > 0 and (
            self.total_tokens >= limits.total_tokens or tokens > limits.total_tokens
        ):
            return {
                "scope": SCOPE_TOTAL_TOKENS,
                "limit": limits.total_tokens,
                "used": self.total_tokens,
                "reserved": extra_tokens,
            }
        seconds = round(self.active_seconds, 1)
        if 0 < limits.active_seconds <= seconds:
            return {"scope": SCOPE_ACTIVE_SECONDS, "limit": limits.active_seconds, "used": seconds}
        return None

    def _stop(self, found: dict[str, Any], at: str) -> dict[str, Any]:
        if self.exceeded is None:
            self.exceeded = {**found, "at": at}
        return self.exceeded

    def reserve_model_call(self, estimated_input_tokens: int = 0) -> dict[str, Any] | None:
        """モデルを 1 回呼ぶ前に予約する。超えるなら予約せず、超えた上限を返す。

        await を挟まずに確かめて数える（同じ Run の呼び出しが重なっても上限を超えない）。
        """
        if self.exceeded is not None:
            return self.exceeded
        estimate = max(int(estimated_input_tokens), 0)
        found = self._over(extra_tokens=estimate, next_call=True)
        if found is not None:
            return self._stop(found, "model_call")
        self.model_calls += 1
        self._reserved_tokens = estimate
        return None

    def settle(self, usage: object) -> None:
        """応答の usage で予約を精算する（usage が無ければ見積もりを消費にし、欠測を数える）。"""
        total = getattr(usage, "total_tokens", 0) if usage is not None else 0
        if not isinstance(total, int) or total <= 0:
            total = _int(getattr(usage, "input_tokens", 0)) + _int(
                getattr(usage, "output_tokens", 0)
            )
        if total > 0:
            self.measured_tokens += total
        else:
            self.unmeasured_calls += 1
            self.estimated_tokens += self._reserved_tokens
        self._reserved_tokens = 0

    def reserve_tool_call(self) -> dict[str, Any] | None:
        """外部の操作（ツールの呼び出し）を始める前に確かめる。予算が尽きていれば実行しない。

        結果を読むモデルの呼び出しが残っていない（モデルの呼び出しの上限まで使った）ときも
        実行しない（使えない結果のために外部の操作をしない）。
        """
        found = self.exceeded or self._over(next_call=True)
        if found is None:
            return None
        self.refused_tool_calls += 1
        return self._stop(found, "tool_call")

    def snapshot(self, *, unknown_steps: int = 0) -> dict[str, Any]:
        """metadata に残す・報告する値（上限・消費・残量・超過・未実行・結果不明）。"""
        limits = self.limits.as_record()
        seconds = round(self.active_seconds, 1)
        consumed: dict[str, Any] = {
            SCOPE_MODEL_CALLS: self.model_calls,
            SCOPE_TOTAL_TOKENS: self.total_tokens,
            "measured_tokens": self.measured_tokens,
            "estimated_tokens": self.estimated_tokens,
            "unmeasured_calls": self.unmeasured_calls,
            SCOPE_ACTIVE_SECONDS: seconds,
            "executions": self.executions,
            "refused_tool_calls": self.refused_tool_calls,
        }
        used = {
            SCOPE_MODEL_CALLS: self.model_calls,
            SCOPE_TOTAL_TOKENS: self.total_tokens,
            SCOPE_ACTIVE_SECONDS: seconds,
        }
        remaining = {
            scope: None if limit is None else max(round(limit - used[scope], 1), 0)
            for scope, limit in limits.items()
        }
        return {
            "limits": limits,
            "consumed": consumed,
            "remaining": remaining,
            "exceeded": self.exceeded,
            "operations": {
                "not_executed": self.refused_tool_calls,
                "unknown_result": unknown_steps,
            },
            # usage を返さなかった呼び出しがあれば、token は見積もりを含む（厳密ではない）。
            "tokens_exact": self.unmeasured_calls == 0,
        }


def exceeded_message(exceeded: Mapping[str, Any], budget: RunBudget) -> str:
    """予算で止めた Run の失敗の文（日本語。モデルを呼ばずに作る）。"""
    scope = str(exceeded.get("scope") or "")
    label = _SCOPE_LABELS.get(scope, scope)
    tokens = f"{budget.total_tokens:,}"
    if budget.unmeasured_calls:
        tokens += (
            f"（うち usage の無い {budget.unmeasured_calls} 回の見積もり "
            f"{budget.estimated_tokens:,}）"
        )
    message = (
        f"Run 全体の実行予算（{label}の上限 {exceeded.get('limit')}）に達したため止めました。"
        f"消費: モデルの呼び出し {budget.model_calls} 回・token {tokens}・"
        f"稼働 {round(budget.active_seconds, 1)} 秒（承認・確認の待ちを除く）。"
    )
    if budget.refused_tool_calls:
        message += (
            f"予算に達した後のツールの呼び出し {budget.refused_tool_calls} 件は実行していません。"
        )
    return message + "ここまでの結果は実行の経過で確認してください。"


def tool_refusal(exceeded: Mapping[str, Any]) -> dict[str, Any]:
    """予算に達した後のツールの呼び出しにモデルへ返す結果（呼び先へは送っていない）。"""
    return {
        "error": "Run 全体の実行予算に達したため、このツールは実行していません。",
        "error_code": RUN_BUDGET_EXCEEDED_CODE,
        "budget_scope": exceeded.get("scope"),
    }


__all__ = [
    "RUN_BUDGET_EXCEEDED_CODE",
    "RUN_BUDGET_METADATA_KEY",
    "RunBudget",
    "RunBudgetLimits",
    "exceeded_message",
    "tool_refusal",
]
