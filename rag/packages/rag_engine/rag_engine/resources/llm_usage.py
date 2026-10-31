"""LLM の呼び出しの token の数と秒（#1516）。

回答の工程（生成・監査・抽出）が遅い理由（入力・出力・推論の token の数）を工程の記録（execution_steps）と
log で確かめるため、LLM の応答の ``usage`` を累積する。実行記録は回答の開始で新しい累積（`llm_usage`）を
置き、工程ごとに開始と終了の差（`usage_delta`）を記録する。

抽出は thread で並列に呼ぶ（`contextvars.copy_context`）。累積の object は文脈から参照で引き継がれるので、
足すときは lock を取る。累積が無い文脈（取込・単体の CLI）では何もしない。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# 累積する値（usage の項目）。秒は呼び出しの往復の時間の和（並列の呼び出しは重なる）。
USAGE_KEYS = ("calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "seconds")
# 応答が最後まで返らなかった（`status=incomplete`）数と、出力の上限で未完了のため上限を広げて送り直した数（#1527）。
# 0 なら記録に出さない（多くの工程では起きない）。理由（`incomplete_details.reason`）ごとの数は ``incomplete_reasons``。
# 停滞（#1926）で打ち切って送り直した数（``stall_resends``）も同じく 0 なら出さない。
# 出力の上限を広げても未完了のため推論の量を下げて送り直した数（``effort_retries``。#2023）も同じ。
# 出力の暴走（同じ並びの繰り返し・空白）で打ち切って送り直した数（``runaway_resends``。#2966）も同じ。
RETRY_KEYS = ("incomplete_responses", "output_retries", "effort_retries", "stall_resends", "runaway_resends")
INCOMPLETE_REASONS_KEY = "incomplete_reasons"
# streaming で受けた呼び出しの時刻（#1926）。最初の event までの秒（待ち）と出力の速さ（delta の event/秒。停滞）を
# 呼び出しごとに残し、工程の記録には最大の待ち（``first_event_seconds_max``）と最も遅い速さ（``delta_rate_min``）を出す。
# 遅い呼び出しが、最初の event までの待ち（待ち行列）か、出力の途中の停滞かを記録から見分けるため。
STREAM_TIMINGS_KEY = "_stream_timings"


class LlmUsage:
    """1 回の回答の LLM の usage の累積（thread をまたいで共有する）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = dict.fromkeys(USAGE_KEYS, 0)
        self._reasons: dict[str, int] = {}
        self._stream: list[tuple[float | None, float | None]] = []

    def add(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            for key in (*USAGE_KEYS, *RETRY_KEYS):
                if key in USAGE_KEYS or values.get(key):
                    self._values[key] = self._values.get(key, 0) + float(values.get(key, 0) or 0)
            if reason := values.get("incomplete_reason"):
                self._reasons[str(reason)] = self._reasons.get(str(reason), 0) + 1
            if values.get("streamed"):
                self._stream.append((_optional_float(values.get("first_event_seconds")), _optional_float(values.get("delta_rate"))))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = dict(self._values)
            if self._reasons:
                result[INCOMPLETE_REASONS_KEY] = dict(self._reasons)
            if self._stream:
                result[STREAM_TIMINGS_KEY] = list(self._stream)
            return result


llm_usage: ContextVar[LlmUsage | None] = ContextVar("rag_engine_llm_usage", default=None)


@contextmanager
def bind_llm_usage(usage: LlmUsage | None = None) -> Iterator[LlmUsage]:
    """この文脈の LLM の usage を新しい累積（または ``usage``）にする。"""
    current = usage or LlmUsage()
    token = llm_usage.set(current)
    try:
        yield current
    finally:
        llm_usage.reset(token)


def _value(source: Any, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def response_usage(response: Any) -> dict[str, int]:
    """Responses API の応答の ``usage`` から、入力・cache に当たった入力・出力・推論の token の数を読む。"""
    usage = _value(response, "usage")
    return {
        "input_tokens": _int(_value(usage, "input_tokens")),
        "cached_tokens": _int(_value(_value(usage, "input_tokens_details"), "cached_tokens")),
        "output_tokens": _int(_value(usage, "output_tokens")),
        "reasoning_tokens": _int(_value(_value(usage, "output_tokens_details"), "reasoning_tokens")),
    }


def record_llm_usage(values: Mapping[str, Any]) -> None:
    """この文脈の累積に、1 回の呼び出しの usage（``response_usage`` と ``seconds``）を足す。

    応答が未完了なら ``incomplete_reason``（`incomplete_details.reason`）を渡す（未完了の数と理由を数える）。
    streaming で受けた呼び出しは ``streamed`` と ``first_event_seconds``・``delta_rate`` を渡す（#1926）。
    """
    usage = llm_usage.get()
    if usage is not None:
        usage.add({**values, "calls": 1, **({"incomplete_responses": 1} if values.get("incomplete_reason") else {})})


def record_llm_output_retry() -> None:
    """出力の上限で未完了になった呼び出しを、上限を広げて送り直したことを数える（#1527）。"""
    usage = llm_usage.get()
    if usage is not None:
        usage.add({"output_retries": 1})


def record_llm_effort_retry() -> None:
    """出力の上限で未完了になった呼び出しを、推論の量を下げて送り直したことを数える（#2023）。"""
    usage = llm_usage.get()
    if usage is not None:
        usage.add({"effort_retries": 1})


def record_llm_stall_resend() -> None:
    """停滞した呼び出しを打ち切って送り直したことを数える（#1926）。"""
    usage = llm_usage.get()
    if usage is not None:
        usage.add({"stall_resends": 1})


def record_llm_runaway_resend() -> None:
    """出力が暴走した呼び出しを打ち切って送り直したことを数える（#2966）。"""
    usage = llm_usage.get()
    if usage is not None:
        usage.add({"runaway_resends": 1})


def usage_snapshot() -> dict[str, Any]:
    """この文脈の累積（無ければ空）。"""
    usage = llm_usage.get()
    return usage.snapshot() if usage is not None else {}


def usage_delta(before: Mapping[str, Any]) -> dict[str, Any]:
    """``before``（`usage_snapshot`）からの増分。呼び出しが無ければ空。秒は小数 1 桁、他は整数。

    未完了の応答・送り直し（`RETRY_KEYS`）とその理由は、増えたときだけ入れる。
    """
    after = usage_snapshot()
    if not after or after.get("calls", 0) - before.get("calls", 0) <= 0:
        return {}
    delta: dict[str, Any] = {}
    for key in USAGE_KEYS:
        diff = after.get(key, 0) - before.get(key, 0)
        delta[key] = round(diff, 1) if key == "seconds" else int(diff)
    for key in RETRY_KEYS:
        if diff := int(after.get(key, 0) - before.get(key, 0)):
            delta[key] = diff
    before_reasons = before.get(INCOMPLETE_REASONS_KEY) or {}
    reasons = {
        reason: count - before_reasons.get(reason, 0)
        for reason, count in (after.get(INCOMPLETE_REASONS_KEY) or {}).items()
        if count - before_reasons.get(reason, 0) > 0
    }
    if reasons:
        delta[INCOMPLETE_REASONS_KEY] = reasons
    timings = (after.get(STREAM_TIMINGS_KEY) or [])[len(before.get(STREAM_TIMINGS_KEY) or []) :]
    if waits := [first for first, _rate in timings if first is not None]:
        delta["first_event_seconds_max"] = round(max(waits), 1)
    if rates := [rate for _first, rate in timings if rate is not None]:
        delta["delta_rate_min"] = round(min(rates), 1)
    return delta


def usage_line(delta: Mapping[str, Any]) -> str:
    """利用者向けの 1 行（例: 「入力 12,345 tokens・出力 2,345 tokens（推論 1,800）・120.5 秒」）。空なら空文字。"""
    if not delta:
        return ""
    text = f"入力 {int(delta.get('input_tokens', 0)):,} tokens"
    if delta.get("cached_tokens"):
        text += f"（cache {int(delta['cached_tokens']):,}）"
    text += f"・出力 {int(delta.get('output_tokens', 0)):,} tokens"
    if delta.get("reasoning_tokens"):
        text += f"（推論 {int(delta['reasoning_tokens']):,}）"
    text += f"・{float(delta.get('seconds', 0)):.1f} 秒"
    if delta.get("incomplete_responses"):
        reasons = "・".join(f"{reason} {count} 回" for reason, count in (delta.get(INCOMPLETE_REASONS_KEY) or {}).items())
        text += f"・未完了の応答 {int(delta['incomplete_responses'])} 回" + (f"（{reasons}）" if reasons else "")
    if delta.get("output_retries"):
        text += f"・出力の上限を広げた送り直し {int(delta['output_retries'])} 回"
    if delta.get("effort_retries"):
        text += f"・推論の量を下げた送り直し {int(delta['effort_retries'])} 回"
    if delta.get("stall_resends"):
        text += f"・停滞による送り直し {int(delta['stall_resends'])} 回"
    if delta.get("runaway_resends"):
        text += f"・出力の繰り返しを打ち切った送り直し {int(delta['runaway_resends'])} 回"
    return text
