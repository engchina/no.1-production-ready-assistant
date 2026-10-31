"""1 回の実行のモデルの利用量（実行履歴の「モデル」「token」「モデルの呼び出し」。#3419・#3543）。

RAG と NL2SQL の実行履歴が同じ規則で数える。集計の元は、その実行の trace（`operation_trace`）の
span の知らせ（`SpanUpdate`）だけで、別の計測は持たない。数えるのは LLM の呼び出しの span
（`gen_ai.chat`）で、利用量は provider が返した値（`record_genai_usage` の `gen_ai.usage.*`）。

- 工程の span は数えない（子の呼び出しと二重に数えない）。`gen_ai.chat` の中に `gen_ai.chat`
  があれば、内側（実際の呼び出し）だけを数える。
- embedding / rerank（`gen_ai.embeddings` / `gen_ai.rerank`）は LLM の token に入れない。
- 合計は入力 + 出力。cached は入力の内数、reasoning は出力の内数で、合計に足さない。
- 利用量の無い呼び出し（失敗・取消・provider が返さない）・終わっていない呼び出し・記録の上限
  で切った trace は「一部計測」（`measurement="partial"`）。LLM を呼ばなかった実行は実測の 0
  （`complete`）。
- アプリが利用量を受け取れない呼び出し（DB の中でモデルを呼ぶ Select AI など）は、その span に
  `UNMEASURED_SOURCE_ATTRIBUTE`（出どころの名前）を付ける。数は作らず、出どころを
  `unmeasured_sources` に残す: ほかに LLM の呼び出しが無ければ「未計測」（`unmeasured`）、
  あれば「一部計測」。
- 記録の無い実行（この機能より前の記録・trace の始まる前に失敗した実行）は利用量を持たない
  （画面は「未計測」）。

別の trace（比較の列・MCP の呼び出し元の業務 Agent）の利用量は混ざらない。

実行の trace の外で呼んだモデルの利用量（NL2SQL の回答前の確認の要求。#3552）は
`collect_model_usage` で同じ規則で集め、`add_run_usage` で実行の利用量に足す。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, Field

from .tracing import SpanUpdate, observe_spans

LLM_CALL_SPAN = "gen_ai.chat"
# アプリが利用量を受け取れないモデルの呼び出しを含む span の属性（値は出どころの名前。例: Select AI
# の `select_ai`）。属性は `tracing._SAFE_ATTRIBUTES` に載せている。
UNMEASURED_SOURCE_ATTRIBUTE = "pr.gen_ai.unmeasured_source"
_TOKEN_ATTRIBUTES = {
    "input": "gen_ai.usage.input_tokens",
    "output": "gen_ai.usage.output_tokens",
    "cached": "gen_ai.usage.cache_read.input_tokens",
    "reasoning": "gen_ai.usage.reasoning.output_tokens",
}
_FINISHED = {"done", "failed", "cancelled", "skipped"}

# complete: すべての呼び出しの入力・出力を計測した。partial: 一部の呼び出しの利用量が無い。
# unmeasured: 計測できない呼び出し（`unmeasured_sources`）だけで、数を持たない。
type UsageMeasurement = Literal["complete", "partial", "unmeasured"]
USAGE_MEASUREMENTS: frozenset[str] = frozenset({"complete", "partial", "unmeasured"})


class RunModelUsage(BaseModel):
    """1 回の実行のモデルの利用量（RAG の `stages_json.usage`・NL2SQL の実行履歴の `usage`）。"""

    # 呼び出したモデルの ID（最初に呼んだ順。回答のモデルを先頭にする）。
    models: list[str] = Field(default_factory=list)
    # LLM の呼び出しの回数（失敗・取消の呼び出しも 1 回）。計測できない呼び出しは数えない。
    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    # 入力 + 出力（cached・reasoning は内数で足さない）。
    total_tokens: int = Field(default=0, ge=0)
    # 入力のうちキャッシュから読んだ token（provider が返した呼び出しだけの和。どれも返さなければ
    # None）。
    cached_input_tokens: int | None = Field(default=None, ge=0)
    # 出力のうち推論の token（同上）。
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    measurement: UsageMeasurement = "complete"
    # 利用量（入力と出力）を計測できた呼び出しの数。
    measured_requests: int = Field(default=0, ge=0)
    # 利用量を計測できない呼び出しの出どころ（Select AI など。最初に呼んだ順。回数は数えない）。
    unmeasured_sources: list[str] = Field(default_factory=list)


class _Call:
    __slots__ = ("model", "parent", "status", "tokens")

    def __init__(self) -> None:
        self.model = ""
        self.parent: str | None = None
        self.status = "running"
        self.tokens: dict[str, int] = {}


def _token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class ModelUsageCollector:
    """trace の span の知らせから、LLM の呼び出しの回数と利用量を集める。

    実行の thread のほか、生成の中の thread からも呼ばれる。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._parents: dict[str, str | None] = {}
        self._calls: dict[str, _Call] = {}
        self._unmeasured: list[str] = []

    def observe(self, update: SpanUpdate) -> None:
        with self._lock:
            if update.root:
                self._started = True
            self._parents[update.span_id] = update.parent_span_id
            source = update.attributes.get(UNMEASURED_SOURCE_ATTRIBUTE)
            if isinstance(source, str) and source and source not in self._unmeasured:
                self._unmeasured.append(source)
            if update.operation_name != LLM_CALL_SPAN:
                return
            call = self._calls.setdefault(update.span_id, _Call())
            call.parent = update.parent_span_id
            call.status = update.status
            attributes = update.attributes
            model = attributes.get("gen_ai.response.model") or attributes.get(
                "gen_ai.request.model"
            )
            if isinstance(model, str) and model:
                call.model = model
            for key, name in _TOKEN_ATTRIBUTES.items():
                value = _token(attributes.get(name))
                if value is not None:
                    call.tokens[key] = value

    def summary(
        self, *, primary_model: str | None = None, truncated: bool = False
    ) -> RunModelUsage | None:
        """集めた利用量。trace が始まっていなければ None（未計測）。

        ``primary_model`` は回答のモデル（呼んでいれば `models` の先頭にする）。``truncated`` は
        記録の上限で span を切った trace（呼び出しを数えきれていないので一部計測）。
        """
        with self._lock:
            if not self._started and not self._calls and not self._unmeasured:
                return None
            calls = {span_id: call for span_id, call in self._calls.items()}
            parents = dict(self._parents)
            unmeasured = list(self._unmeasured)
        # `gen_ai.chat` の中の `gen_ai.chat` は内側だけを数える（外側は子の呼び出しの和と重なる）。
        outer: set[str] = set()
        for span_id in calls:
            seen: set[str] = {span_id}
            parent = parents.get(span_id)
            while parent is not None and parent not in seen:
                seen.add(parent)
                if parent in calls:
                    outer.add(parent)
                parent = parents.get(parent)
        leaves = [call for span_id, call in calls.items() if span_id not in outer]
        models: list[str] = []
        for call in leaves:
            if call.model and call.model not in models:
                models.append(call.model)
        if primary_model and primary_model in models:
            models.remove(primary_model)
            models.insert(0, primary_model)
        measured = [
            call
            for call in leaves
            if call.status in _FINISHED and "input" in call.tokens and "output" in call.tokens
        ]
        input_tokens = sum(call.tokens.get("input", 0) for call in leaves)
        output_tokens = sum(call.tokens.get("output", 0) for call in leaves)

        def subtotal(key: str) -> int | None:
            values = [call.tokens[key] for call in leaves if key in call.tokens]
            return sum(values) if values else None

        measurement: UsageMeasurement
        if unmeasured and not leaves:
            # 計測できない呼び出しだけ（Select AI だけの実行）。数を作らない。
            measurement = "unmeasured"
        elif unmeasured or truncated or len(measured) < len(leaves):
            measurement = "partial"
        else:
            measurement = "complete"
        return RunModelUsage(
            models=models,
            requests=len(leaves),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_input_tokens=subtotal("cached"),
            reasoning_output_tokens=subtotal("reasoning"),
            measurement=measurement,
            measured_requests=len(measured),
            unmeasured_sources=unmeasured,
        )


@contextmanager
def collect_model_usage() -> Iterator[ModelUsageCollector]:
    """範囲の中のモデルの呼び出しを集める（実行の trace の外の要求でも使える。#3552）。

    `operation_trace` の記録とは別に span の知らせを受ける（記録の中なら、記録の listener にも
    今どおり知らせる）。範囲を出た後に `summary()` で読む。モデルを呼ばなければ None。
    """
    collector = ModelUsageCollector()
    with observe_spans(collector.observe):
        yield collector


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def add_run_usage(total: RunModelUsage | None, usage: RunModelUsage | None) -> RunModelUsage | None:
    """2 つの利用量の和（同じ実行の別の要求の呼び出しを足す。#3552）。

    None（記録の無い側）は足すものが無いとして、もう一方をそのまま返す。モデル・計測できない
    呼び出しの出どころは `total` の順を先にする。計測の状態は `ModelUsageCollector` と同じ規則:
    計測できない呼び出しだけなら未計測、どちらかが一部計測・未計測なら一部計測。
    """
    if total is None or usage is None:
        return total if usage is None else usage
    models = [*total.models, *(model for model in usage.models if model not in total.models)]
    unmeasured = [
        *total.unmeasured_sources,
        *(source for source in usage.unmeasured_sources if source not in total.unmeasured_sources),
    ]
    requests = total.requests + usage.requests
    measurement: UsageMeasurement
    if unmeasured and not requests:
        measurement = "unmeasured"
    elif total.measurement != "complete" or usage.measurement != "complete":
        measurement = "partial"
    else:
        measurement = "complete"
    input_tokens = total.input_tokens + usage.input_tokens
    output_tokens = total.output_tokens + usage.output_tokens
    return RunModelUsage(
        models=models,
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_input_tokens=_sum_optional(total.cached_input_tokens, usage.cached_input_tokens),
        reasoning_output_tokens=_sum_optional(
            total.reasoning_output_tokens, usage.reasoning_output_tokens
        ),
        measurement=measurement,
        measured_requests=total.measured_requests + usage.measured_requests,
        unmeasured_sources=unmeasured,
    )


def parse_run_usage(value: object) -> RunModelUsage | None:
    """保存した利用量を読む。無い・形の違う記録（この機能より前の実行）は None（未計測）。"""
    if not isinstance(value, dict):
        return None
    try:
        return RunModelUsage.model_validate(value)
    except ValueError:
        return None


def run_usage_totals(
    total_tokens: object, measurement: object
) -> tuple[int | None, UsageMeasurement | None]:
    """一覧の行の利用量（合計の token と計測の状態。#3466）。

    一覧は利用量の全体を読まず、2 つの値だけを使う。計測の状態が無い・形の違う記録（この機能より
    前の実行・trace の始まる前に失敗した実行）は未計測（どちらも None。0 にしない）。計測できない
    呼び出しだけの実行（`unmeasured`）は数を持たない（合計は None）。計測した記録の合計が無ければ 0
    （`RunModelUsage` の既定と同じ。LLM を呼ばなかった実行は実測の 0）。
    """
    if measurement == "unmeasured":
        return None, "unmeasured"
    if measurement != "complete" and measurement != "partial":
        return None, None
    if isinstance(total_tokens, bool) or not isinstance(total_tokens, int | float):
        return 0, measurement
    if total_tokens < 0 or not float(total_tokens).is_integer():
        return 0, measurement
    return int(total_tokens), measurement


def run_usage_summary_fields(
    usage: RunModelUsage | None,
) -> tuple[int | None, UsageMeasurement | None]:
    """利用量の全体から、一覧と同じ 2 つの値（合計の token と計測の状態）を決める。"""
    if usage is None:
        return None, None
    return run_usage_totals(usage.total_tokens, usage.measurement)


__all__ = [
    "LLM_CALL_SPAN",
    "UNMEASURED_SOURCE_ATTRIBUTE",
    "USAGE_MEASUREMENTS",
    "ModelUsageCollector",
    "RunModelUsage",
    "UsageMeasurement",
    "add_run_usage",
    "collect_model_usage",
    "parse_run_usage",
    "run_usage_summary_fields",
    "run_usage_totals",
]
