"""進展の無いツールの呼び出しを見つけて Run を終える（#2580）。

組み込み Runtime の呼び出しの境界（`builtin_runtime._ToolRecorder`）と、モデルの呼び出しの前
（`_run_config` の `call_model_input_filter`）から使う。SDK の Agent loop はそのまま使い、
ここでは Run の step から判定だけを行う。

数え方:

- 数えるのは Run の step（保存先にある。承認の後の再開・再起動で数え直さない）。モデルが呼んだ
  ツールの step だけを、step を作った順（呼び出しを始めた順）に見る。結果がまだ無い step（実行中・
  承認待ち）と、取消・未実行の step は数えない。並列の呼び出しも始めた順で数えるので、完了の順で
  結果は変わらない。
- Control Plane が自分で呼んだ step（回答の検証・業務ガイドの照合。監査の `authorization` が
  `control_plane`）と、回答前の確認（`confirmation`）は数えない。
- 承認待ち・承認が要るための未実行・ツール権限の拒否・予算の超過は数えない（それぞれの仕組みで
  止まる）。

終える条件（ツールごと。別のツールの失敗は足さない）:

- 同じ呼び出しの反復（`runtime.tool_call_repeated`）: 同じツールを同じ引数で呼び、同じ結果が
  `REPEAT_LIMIT` 回続いた。結果が変わる呼び出し（polling）、引数が変わる呼び出し（ページング・
  別の条件の検索）は数えない。
- 実行の失敗の連続（`runtime.tool_failures_repeated`）: 同じツールの実行（呼び先へ送った呼び出し。
  一時的な HTTP の障害を含む）が、成功を挟まずに `FAILURE_LIMIT` 回失敗した。
- 引数の誤りの連続（`runtime.tool_argument_errors_repeated`）: 同じツールの呼び出しが、引数の誤り
  （schema・呼び先の検証・データの範囲）で `ARGUMENT_ERROR_LIMIT` 回続けて拒否された。実行に
  至った呼び出し（成功・実行の失敗）で数え直す。

上限の 1 回前の結果には、次も同じなら終えることをモデルへの結果（`loop_notice`）に足す（差し戻し）。
上限に達したら、同じ応答の残りの呼び出しを実行せず、次のモデルの呼び出しの前に Run を失敗にする。
判定に使う引数・結果の照合は Run の中だけで行い、値・指紋をイベント・ログ・metric に出さない。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from app.features.agent.data_scope import DATA_SCOPE_VIOLATION_CODE
from app.features.agent.support_task import BUDGET_EXCEEDED_CODE
from app.features.agent.tool_arguments import INVALID_ARGUMENTS_CODE
from app.features.agent.tools import ToolPolicyDecision, ToolResult

# 同じツール・同じ引数で同じ結果が続いた回数の上限。
REPEAT_LIMIT = 3
# 同じツールの実行が成功を挟まずに失敗した回数の上限（呼び先の HTTP の再試行の後の失敗を
# 1 回と数える）。
FAILURE_LIMIT = 3
# 同じツールの呼び出しが引数の誤りで続けて拒否された回数の上限（直しながら呼び直す分の余裕を
# 持たせる）。
ARGUMENT_ERROR_LIMIT = 4

TOOL_CALL_REPEATED_CODE = "runtime.tool_call_repeated"
TOOL_FAILURES_REPEATED_CODE = "runtime.tool_failures_repeated"
TOOL_ARGUMENT_ERRORS_REPEATED_CODE = "runtime.tool_argument_errors_repeated"

LoopKind = Literal["repeated_call", "repeated_failure", "repeated_argument_error"]

_CODES: dict[LoopKind, str] = {
    "repeated_call": TOOL_CALL_REPEATED_CODE,
    "repeated_failure": TOOL_FAILURES_REPEATED_CODE,
    "repeated_argument_error": TOOL_ARGUMENT_ERRORS_REPEATED_CODE,
}
_LIMITS: dict[LoopKind, int] = {
    "repeated_call": REPEAT_LIMIT,
    "repeated_failure": FAILURE_LIMIT,
    "repeated_argument_error": ARGUMENT_ERROR_LIMIT,
}
# 数える step の状態（結果が出たもの）。
_FINISHED_STATUSES = frozenset({"completed", "failed"})
# Control Plane が自分で呼んだ・確認の往復の step（監査の `authorization`）。
_NOT_MODEL_CALLS = frozenset({"control_plane", "confirmation"})


@dataclass(frozen=True)
class LoopFinding:
    """ツールの 1 つの数え方の今の回数。"""

    kind: LoopKind
    tool_name: str
    count: int

    @property
    def code(self) -> str:
        return _CODES[self.kind]

    @property
    def limit(self) -> int:
        return _LIMITS[self.kind]

    @property
    def stopped(self) -> bool:
        return self.count >= self.limit

    @property
    def warns(self) -> bool:
        """次も同じなら終える（上限の 1 回前）。"""
        return self.count == self.limit - 1 and self.count > 0

    def notice(self) -> str:
        """モデルへの結果に足す案内（`loop_notice`）。"""
        if self.kind == "repeated_call":
            return (
                f"このツールを同じ引数で {self.count} 回呼び、同じ結果が続きました。"
                "同じ引数でもう一度呼んで同じ結果なら、実行を終えます。"
                "引数を変えるか、ここまでの結果で回答してください。"
            )
        if self.kind == "repeated_failure":
            return (
                f"このツールの実行が続けて {self.count} 回失敗しました。次も失敗すると実行を"
                "終えます。同じ呼び出しを繰り返さず、別のツールで調べるか、ここまでの結果で"
                "回答してください。"
            )
        return (
            f"このツールの引数の誤りが続けて {self.count} 回ありました。次も誤りなら実行を"
            "終えます。誤りの内容に従って引数を直してください。"
        )

    def refusal(self) -> dict[str, Any]:
        """上限に達した後の呼び出しへの結果（実行していない）。"""
        return {
            "error": (
                "進展の無いツールの呼び出しが続いたため実行を終えます。"
                "このツールは実行していません。"
            ),
            "error_code": self.code,
        }

    def run_message(self) -> str:
        """Run の失敗の文（画面の既存の失敗の表示に出る）。"""
        if self.kind == "repeated_call":
            return (
                f"ツール {self.tool_name} を同じ引数で {self.count} 回呼び、同じ結果が続いたため、"
                "実行を終えました。質問の対象や条件を具体的にして、もう一度実行してください。"
            )
        if self.kind == "repeated_failure":
            return (
                f"ツール {self.tool_name} の実行が続けて {self.count} 回失敗したため、実行を"
                "終えました。接続先のサービスの状態を確かめてから、もう一度実行してください。"
            )
        return (
            f"ツール {self.tool_name} の引数の誤りが続けて {self.count} 回あったため、実行を"
            "終えました。もう一度実行し、解決しなければツールの引数の定義を確かめてください。"
        )


@dataclass
class _ToolCounters:
    argument_errors: int = 0
    failures: int = 0


@dataclass
class _Scan:
    """step を順に見た結果（ツールごとの連続の数と、呼び出しごとの同じ結果の数）。"""

    tools: dict[str, _ToolCounters]
    repeats: dict[str, tuple[str, int]]
    # step ID → その step で更新した数え方（モデルへの案内に使う）。
    by_step: dict[str, LoopFinding]


def failure_kind(result: ToolResult) -> Literal["argument", "execution"] | None:
    """失敗した結果の数え方（数えない失敗は None）。"""
    if result.success:
        return None
    if result.approval_required or result.policy_decision == ToolPolicyDecision.DENY:
        return None
    if result.error_code == BUDGET_EXCEEDED_CODE:
        return None
    if result.error_code in {INVALID_ARGUMENTS_CODE, DATA_SCOPE_VIOLATION_CODE}:
        return "argument"
    inner = result.error_details.get("details") if isinstance(result.error_details, dict) else None
    errors = inner.get("errors") if isinstance(inner, dict) else None
    if isinstance(errors, list) and errors:
        # 呼び先が引数（値）の検証の誤りを返した（実行はしていない）。
        return "argument"
    return "execution"


def _fingerprint(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scan(steps: Iterable[Any]) -> _Scan:
    scan = _Scan(tools={}, repeats={}, by_step={})
    for step in steps:
        call, result = getattr(step, "tool_call", None), getattr(step, "tool_result", None)
        if call is None or result is None or str(step.status) not in _FINISHED_STATUSES:
            continue
        if result.audit_metadata.get("authorization") in _NOT_MODEL_CALLS:
            continue
        name = call.name
        counters = scan.tools.setdefault(name, _ToolCounters())
        if result.success:
            counters.argument_errors = 0
            counters.failures = 0
            key = _fingerprint([name, call.arguments])
            output = _fingerprint(result.output)
            previous, count = scan.repeats.get(key, ("", 0))
            count = count + 1 if previous == output else 1
            scan.repeats[key] = (output, count)
            scan.by_step[step.id] = LoopFinding("repeated_call", name, count)
            continue
        kind = failure_kind(result)
        if kind == "argument":
            counters.argument_errors += 1
            scan.by_step[step.id] = LoopFinding(
                "repeated_argument_error", name, counters.argument_errors
            )
        elif kind == "execution":
            counters.argument_errors = 0
            counters.failures += 1
            scan.by_step[step.id] = LoopFinding("repeated_failure", name, counters.failures)
    return scan


def stop_finding(steps: Iterable[Any]) -> LoopFinding | None:
    """上限に達した数え方（無ければ None）。最初に見つかった 1 つ。"""
    scan = _scan(steps)
    for name, counters in scan.tools.items():
        if counters.argument_errors >= ARGUMENT_ERROR_LIMIT:
            return LoopFinding("repeated_argument_error", name, counters.argument_errors)
        if counters.failures >= FAILURE_LIMIT:
            return LoopFinding("repeated_failure", name, counters.failures)
    for finding in scan.by_step.values():
        if finding.kind == "repeated_call" and finding.stopped:
            return finding
    return None


def step_finding(steps: Iterable[Any], step_id: str) -> LoopFinding | None:
    """その step の結果で更新した数え方（数えない step は None）。"""
    return _scan(steps).by_step.get(step_id)
