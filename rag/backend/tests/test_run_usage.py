"""実行のモデルの利用量（実行履歴の概要。#3419）の集計のテスト。

本物の `operation_trace` と回答エンジンの観測（`_observe_engine_call`）で span を作り、
`AnswerTrace` の span の知らせから集めた値を確かめる。OCI は呼ばない（usage は合成の値）。
"""

from __future__ import annotations

import pytest
from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.model_usage import RunModelUsage, parse_run_usage
from pr_backend_core.observability.tracing import observed_span
from rag_engine.resources.tracing import observe_call

from app.rag.trace_store import AnswerTrace
from tests.span_progress_support import answer_step, answer_trace


def _call(model: str, usage: dict[str, object] | None) -> None:
    """回答エンジンの LLM の呼び出し（`gen_ai.chat` の span）を 1 回分作る。"""
    with observe_call("chat", model) as observation:
        if usage is not None:
            observation.usage(usage)


def _usage(input_tokens: int, output_tokens: int, **details: int) -> dict[str, object]:
    value: dict[str, object] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if "cached" in details:
        value["input_tokens_details"] = {"cached_tokens": details["cached"]}
    if "reasoning" in details:
        value["output_tokens_details"] = {"reasoning_tokens": details["reasoning"]}
    return value


def test_usage_sums_llm_calls_without_double_counting_stages() -> None:
    """工程の span・embedding・rerank は数えず、LLM の呼び出しだけを足す（合計 = 入力 + 出力）。

    質問の書き換え（backend の client）・質問の理解・回答の生成・検証・送り直しの 1 回ずつを数える。
    cached は入力の、reasoning は出力の内数で、合計に足さない。
    """
    trace = AnswerTrace("op-usage", owner_user_uuid=None)
    with answer_trace(trace, trace_id="run-usage"):
        # 会話の履歴による質問の書き換え（backend の OciEnterpriseAiClient と同じ span）。
        with observed_span(
            "gen_ai.chat", kind="client", attributes={"gen_ai.request.model": "model-rewrite"}
        ) as span:
            record_genai_usage(span, _usage(50, 10))
        with answer_step("質問の理解"):
            _call("model-answer", _usage(100, 20, cached=40))
        with answer_step("回答文の生成と根拠確認"):
            _call("model-answer", _usage(300, 80, reasoning=30))
            # 出力の上限で未完了の送り直し（同じ工程の 2 回目の呼び出し）。
            _call("model-answer", _usage(300, 120, reasoning=50))
        # embedding / rerank は LLM の token に入れない。
        with observe_call("embeddings", "embed-model") as observation:
            observation.usage(_usage(999, 0))
        with observe_call("rerank", "rerank-model"):
            pass

    usage = trace.model_usage(primary_model="model-answer")
    assert usage == RunModelUsage(
        models=["model-answer", "model-rewrite"],
        requests=4,
        input_tokens=750,
        output_tokens=230,
        total_tokens=980,
        cached_input_tokens=40,
        reasoning_output_tokens=80,
        measurement="complete",
        measured_requests=4,
    )


def test_nested_llm_call_spans_count_only_the_inner_call() -> None:
    """`gen_ai.chat` の中の `gen_ai.chat`（共有 client の内側の呼び出し）は内側だけを数える。"""
    trace = AnswerTrace("op-nested", owner_user_uuid=None)
    with (
        answer_trace(trace, trace_id="run-nested"),
        observed_span(
            "gen_ai.chat", kind="client", attributes={"gen_ai.request.model": "model-a"}
        ) as outer,
    ):
        with answer_step("回答"):
            _call("model-a", _usage(10, 5))
        record_genai_usage(outer, _usage(10, 5))

    usage = trace.model_usage()
    assert usage is not None
    assert (usage.requests, usage.total_tokens, usage.measurement) == (1, 15, "complete")


def test_calls_without_usage_are_partial_and_failed_calls_still_count() -> None:
    """利用量の無い呼び出し・失敗した呼び出しは数え、利用量は 0 にせず一部計測にする。"""
    trace = AnswerTrace("op-partial", owner_user_uuid=None)
    with answer_trace(trace, trace_id="run-partial"):
        _call("model-a", _usage(100, 10))
        # provider が usage を返さない。
        _call("model-a", None)
        # 応答が返らなかった呼び出し（timeout）。
        with pytest.raises(TimeoutError), observe_call("chat", "model-a"):
            raise TimeoutError

    usage = trace.model_usage()
    assert usage is not None
    assert usage.requests == 3
    assert usage.measured_requests == 1
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 10, 110)
    assert usage.measurement == "partial"
    # provider が返さなかった内数は None（0 にしない）。
    assert usage.cached_input_tokens is None
    assert usage.reasoning_output_tokens is None


def test_unfinished_and_truncated_traces_are_partial() -> None:
    """終わっていない呼び出し（取消で残った thread など）と、上限で切った trace は一部計測。"""
    trace = AnswerTrace("op-running", owner_user_uuid=None)
    with answer_trace(trace, trace_id="run-running"):
        _call("model-a", _usage(10, 1))
        with observe_call("chat", "model-a"):
            running = trace.model_usage()
    assert running is not None
    assert (running.requests, running.measured_requests, running.measurement) == (2, 1, "partial")

    finished = trace.model_usage()
    assert finished is not None
    assert finished.measurement == "partial"  # 2 回目は利用量が無い
    complete = AnswerTrace("op-truncated", owner_user_uuid=None)
    with answer_trace(complete, trace_id="run-truncated"):
        _call("model-a", _usage(10, 1))
    assert complete.model_usage() is not None
    assert complete.model_usage(truncated=True).measurement == "partial"  # type: ignore[union-attr]


def test_runs_without_llm_calls_are_a_measured_zero_and_unstarted_traces_are_unknown() -> None:
    """LLM を呼ばなかった実行は実測の 0、trace の始まる前の実行は記録しない（未計測）。"""
    trace = AnswerTrace("op-zero", owner_user_uuid=None)
    with (
        answer_trace(trace, trace_id="run-zero", name="rag.search"),
        observe_call("embeddings", "embed-model"),
    ):
        pass

    assert trace.model_usage() == RunModelUsage(
        requests=0, input_tokens=0, output_tokens=0, total_tokens=0, measurement="complete"
    )
    assert AnswerTrace("op-unstarted", owner_user_uuid=None).model_usage() is None


def test_separate_traces_do_not_mix() -> None:
    """比較の列は列ごとに trace が分かれ、利用量が混ざらない。"""
    first = AnswerTrace("op-column-a", owner_user_uuid=None)
    second = AnswerTrace("op-column-b", owner_user_uuid=None)
    with answer_trace(first, trace_id="run-column-a"):
        _call("model-a", _usage(10, 1))
        with answer_trace(second, trace_id="run-column-b"):
            _call("model-b", _usage(20, 2))
            _call("model-b", _usage(20, 2))
        _call("model-a", _usage(10, 1))

    a = first.model_usage(primary_model="model-a")
    b = second.model_usage(primary_model="model-b")
    assert a is not None and b is not None
    assert (a.models, a.requests, a.total_tokens) == (["model-a"], 2, 22)
    assert (b.models, b.requests, b.total_tokens) == (["model-b"], 2, 44)


@pytest.mark.parametrize(
    ("value", "expected_requests"),
    [
        ({"models": ["m"], "requests": 2, "input_tokens": 3, "output_tokens": 4}, 2),
        (None, None),
        ("broken", None),
        ({"requests": -1}, None),
    ],
    ids=["saved", "legacy", "broken", "invalid"],
)
def test_parse_run_usage(value: object, expected_requests: int | None) -> None:
    usage = parse_run_usage(value)
    assert (usage.requests if usage is not None else None) == expected_requests
