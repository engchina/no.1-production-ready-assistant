"""モデルの呼び出しごとの文脈予算・入力の構成・指紋と cache の利用量の記録（#2583）。

モデルは OCI の client の層（`CompatResponsesModel` → `_CompatResponses`）を通し、呼び先だけを合成の
応答を返す fake にする（OCI へは接続しない）。モデルの呼び出しの span（`gen_ai.chat`）の属性を
確かめる: 指紋の安定と変化・入力の部分ごとの見積もり・予算と縮める前後・プロバイダが cache を
返さないときの不明（0 と区別）・呼び出しごとの usage と Run の累計（承認の再開・差し戻し・入力の
超過の送り直し）が二重に数えられないこと。本文（指示・引数・結果・質問）は属性に入らない。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from agents.testing import assistant_message, function_call
from openai._models import construct_type
from openai.types.responses import Response
from openai.types.responses.response_usage import ResponseUsage
from pr_backend_core.observability.tracing import ObservedSpan, observed_span, operation_trace
from pytest import MonkeyPatch
from test_agent_output_fields import AGENT_ID as OUTPUT_AGENT_ID
from test_agent_output_fields import LOOKUP as OUTPUT_LOOKUP
from test_agent_output_fields import agents as _agents_fixture  # noqa: F401 - fixture
from test_builtin_runtime import SKILL_ID, WRITE, _create_run
from test_builtin_runtime import _script as _script_model
from test_builtin_runtime import calls as _calls_fixture  # noqa: F401 - fixture
from test_context_window import EVIDENCE_TOOL, _evidence, _items, _overflow_error

from app.features.agent import builtin_runtime
from app.features.agent.context_window import ContextBudget, ContextLimits, compact_items
from app.features.agent.model_call_observation import (
    FINGERPRINT_CHARS,
    fingerprint,
    model_call_attributes,
)
from app.features.agent.output_fields import RECORD_OUTPUT_TOOL, output_retried
from app.features.agent.runtime import (
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)

CHAT = "gen_ai.chat"
CACHE = "gen_ai.usage.cache_read.input_tokens"
# 合成の指示・ツール・入力（業務の内容を含まない）。
STATIC = "合成の指示の本文。" * 20
TOOL_A = {
    "type": "function",
    "name": "synthetic_lookup",
    "description": "合成のツールの説明",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
}
TOOL_B = {
    "type": "function",
    "name": "synthetic_write",
    "description": "合成の書き込みの説明",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
}


def _request(dynamic: str = "", *, tools: list[Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "model": "test-model",
        "instructions": STATIC + (f"\n\n{dynamic}" if dynamic else ""),
        "tools": [TOOL_A, TOOL_B] if tools is None else tools,
        "input": [{"role": "user", "content": "合成の質問の文"}],
        **extra,
    }


# --------------------------------------------------------------------------------------------
# 指紋と入力の構成（単体）
# --------------------------------------------------------------------------------------------


def test_fingerprints_are_stable_for_the_same_configuration() -> None:
    first = model_call_attributes(_request("合成の状態 A"), static_instructions=STATIC)
    # 同じ構成（ツールの定義の key の並びだけ違う）なら同じ指紋。
    reordered = [dict(reversed(list(TOOL_A.items()))), dict(reversed(list(TOOL_B.items())))]
    second = model_call_attributes(
        _request("合成の状態 A", tools=reordered), static_instructions=STATIC
    )
    for key in (
        "pr.context.instructions.static_fingerprint",
        "pr.context.instructions.fingerprint",
        "pr.context.tools.fingerprint",
    ):
        assert first[key] == second[key]
        value = str(first[key])
        assert len(value) == FINGERPRINT_CHARS and int(value, 16) >= 0
    assert first["pr.context.tools.count"] == 2


def test_fingerprints_change_with_instructions_or_schema() -> None:
    base = model_call_attributes(_request("合成の状態 A"), static_instructions=STATIC)
    # Run の状態（指示の末尾）だけが変わる: 変わらない部分の指紋は同じで、全体の指紋は変わる。
    dynamic = model_call_attributes(_request("合成の状態 B は長い"), static_instructions=STATIC)
    assert (
        dynamic["pr.context.instructions.static_fingerprint"]
        == base["pr.context.instructions.static_fingerprint"]
    )
    assert (
        dynamic["pr.context.instructions.fingerprint"]
        != base["pr.context.instructions.fingerprint"]
    )
    assert dynamic["pr.context.tools.fingerprint"] == base["pr.context.tools.fingerprint"]
    assert int(dynamic["pr.context.estimate.dynamic_state_tokens"]) > int(
        base["pr.context.estimate.dynamic_state_tokens"]
    )
    assert (
        dynamic["pr.context.estimate.static_instructions_tokens"]
        == base["pr.context.estimate.static_instructions_tokens"]
    )

    # 業務 Agent の指示が変わる。
    changed = {**_request("合成の状態 A"), "instructions": "別の指示。\n\n合成の状態 A"}
    instructions = model_call_attributes(changed, static_instructions="別の指示。")
    assert (
        instructions["pr.context.instructions.static_fingerprint"]
        != base["pr.context.instructions.static_fingerprint"]
    )

    # ツールの schema が変わる（引数を 1 つ足す）・ツールが減る。
    schema = {
        **TOOL_B,
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "integer"}, "note": {"type": "string"}},
        },
    }
    widened = model_call_attributes(
        _request("合成の状態 A", tools=[TOOL_A, schema]), static_instructions=STATIC
    )
    fewer = model_call_attributes(
        _request("合成の状態 A", tools=[TOOL_A]), static_instructions=STATIC
    )
    assert widened["pr.context.tools.fingerprint"] != base["pr.context.tools.fingerprint"]
    assert fewer["pr.context.tools.fingerprint"] != base["pr.context.tools.fingerprint"]
    assert (
        widened["pr.context.instructions.static_fingerprint"]
        == base["pr.context.instructions.static_fingerprint"]
    )


def test_unknown_static_part_counts_the_whole_instructions_as_static() -> None:
    """変わらない部分が分からない（Run の外の呼び出し）・先頭に一致しないときは分けない。"""
    for static in (None, "先頭に無い指示"):
        values = model_call_attributes(_request("合成の状態 A"), static_instructions=static)
        assert values["pr.context.estimate.dynamic_state_tokens"] == 0
        assert (
            values["pr.context.instructions.static_fingerprint"]
            == values["pr.context.instructions.fingerprint"]
        )
    assert fingerprint(STATIC) == fingerprint(STATIC)


def test_composition_splits_history_and_tool_outputs_and_reports_the_budget() -> None:
    budget = ContextBudget(
        ContextLimits(model_id="test-model", max_input_tokens=200_000, max_output_tokens=4096)
    )
    items = _items(_evidence("a", 3))
    values = model_call_attributes(
        {**_request("合成の状態 A"), "input": items}, budget=budget, static_instructions=STATIC
    )
    parts = (
        "static_instructions_tokens",
        "dynamic_state_tokens",
        "tool_definitions_tokens",
        "history_tokens",
        "tool_outputs_tokens",
    )
    estimates = {part: int(values[f"pr.context.estimate.{part}"]) for part in parts}
    assert all(value > 0 for value in estimates.values())
    # ツールの結果（根拠 3 件）は、質問とツールの呼び出しより大きい。
    assert estimates["tool_outputs_tokens"] > estimates["history_tokens"]
    assert values["pr.context.estimate.input_tokens"] == sum(estimates.values())
    assert values["pr.context.input.items"] == len(items)
    assert values["pr.context.budget.input_tokens"] == budget.input_tokens()
    assert (
        values["pr.context.budget.remaining_tokens"]
        == budget.input_tokens() - values["pr.context.estimate.input_tokens"]
    )
    # 縮めた記録が無ければ縮める前後は付けない。
    assert "pr.context.compaction.tokens_before" not in values


def test_compaction_before_and_after_are_recorded() -> None:
    budget = ContextBudget(
        ContextLimits(model_id="test-model", max_input_tokens=24_000, max_output_tokens=4096)
    )
    items = _items(_evidence("a", 40), _evidence("b", 40))
    fixed = f"{STATIC}\n"
    result = compact_items(items, budget=budget, fixed_text=fixed)
    assert result.outputs and result.fits
    values = model_call_attributes(
        {**_request(), "tools": [], "input": result.items},
        budget=budget,
        static_instructions=STATIC,
        compaction=result,
    )
    assert values["pr.context.compaction.tokens_before"] == result.tokens_before
    assert values["pr.context.compaction.tokens_after"] == result.tokens_after
    assert values["pr.context.compaction.tokens_before"] > result.tokens_after
    assert values["pr.context.compaction.outputs"] == len(result.outputs)
    assert int(values["pr.context.budget.remaining_tokens"]) >= 0


def test_long_conversation_grows_history_but_not_the_static_prefix() -> None:
    def turns(count: int) -> list[dict[str, Any]]:
        return [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"合成の往復 {index}"}
            for index in range(count)
        ]

    short = model_call_attributes(
        {**_request("合成の状態 A"), "input": turns(2)}, static_instructions=STATIC
    )
    long = model_call_attributes(
        {**_request("合成の状態 A"), "input": turns(40)}, static_instructions=STATIC
    )
    assert long["pr.context.input.items"] == 40
    assert int(long["pr.context.estimate.history_tokens"]) > int(
        short["pr.context.estimate.history_tokens"]
    )
    for key in (
        "pr.context.instructions.static_fingerprint",
        "pr.context.tools.fingerprint",
        "pr.context.estimate.static_instructions_tokens",
        "pr.context.estimate.tool_definitions_tokens",
    ):
        assert long[key] == short[key]


def test_attributes_never_contain_the_text() -> None:
    request = {
        **_request("合成の状態の本文"),
        "input": [
            {"role": "user", "content": "合成の質問の本文"},
            {
                "type": "function_call",
                "call_id": "c",
                "name": "synthetic_lookup",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c", "output": "合成の結果の本文"},
        ],
    }
    dumped = json.dumps(
        model_call_attributes(request, static_instructions=STATIC), ensure_ascii=False
    )
    for text in (
        "合成の指示",
        "合成の状態",
        "合成の質問",
        "合成の結果",
        "合成のツール",
        "synthetic",
    ):
        assert text not in dumped


@pytest.mark.usefixtures("_calls_fixture")
def test_static_instructions_come_before_the_run_state(monkeypatch: MonkeyPatch) -> None:
    """Run の状態（支援タスク）を変えても、指示の変わらない部分は同じ（#2583）。"""
    run_id = _create_run("合成の質問")
    _script_model(monkeypatch)
    built = [
        builtin_runtime.build_sdk_agent(
            run_id,
            name="agent",
            instructions="合成の業務 Agent の指示。",
            skill_ids=[SKILL_ID],
            model_id="",
            support_task=state,
        )
        for state in ("", "合成の支援タスクの状態 A", "合成の支援タスクの状態 B")
    ]
    statics = [getattr(agent, builtin_runtime._STATIC_INSTRUCTIONS_ATTR) for agent in built]  # noqa: SLF001
    assert statics[0] == statics[1] == statics[2] == built[0].instructions
    for agent in built[1:]:
        assert isinstance(agent.instructions, str) and agent.instructions.startswith(statics[0])
    values = [
        model_call_attributes(
            {"instructions": agent.instructions, "tools": []}, static_instructions=static
        )
        for agent, static in zip(built, statics, strict=True)
    ]
    assert len({value["pr.context.instructions.static_fingerprint"] for value in values}) == 1
    assert len({value["pr.context.instructions.fingerprint"] for value in values}) == 3
    assert values[0]["pr.context.estimate.dynamic_state_tokens"] == 0
    assert int(values[1]["pr.context.estimate.dynamic_state_tokens"]) > 0


# --------------------------------------------------------------------------------------------
# OCI の client の層の span
# --------------------------------------------------------------------------------------------


def _usage(
    input_tokens: int, output_tokens: int, cached: int | None = None, *, details: bool = True
) -> ResponseUsage:
    data: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if details:
        # 呼び先の JSON から作る（openai の client と同じ。無い項目は None のまま）。
        data["input_tokens_details"] = {} if cached is None else {"cached_tokens": cached}
        data["output_tokens_details"] = {"reasoning_tokens": 0}
    return cast(ResponseUsage, construct_type(type_=ResponseUsage, value=data))


@contextmanager
def _chat_spans(monkeypatch: MonkeyPatch) -> Iterator[list[ObservedSpan]]:
    """組み込み Runtime のモデルの呼び出しの span を集める。"""
    spans: list[ObservedSpan] = []

    @contextmanager
    def capture(name: str, **kwargs: Any) -> Iterator[ObservedSpan]:
        with observed_span(name, **kwargs) as span:
            if name == CHAT:
                spans.append(span)
            yield span

    monkeypatch.setattr(builtin_runtime, "observed_span", capture)
    yield spans


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (_usage(10, 2, details=False), None),
        (_usage(10, 2, None), None),
        (_usage(10, 2, 0), 0),
        (_usage(10, 2, 7), 7),
        # 入力より多い cache は矛盾した値として記録しない（cached <= input）。
        (_usage(10, 2, 12), None),
    ],
    ids=["no-details", "details-without-cache", "zero", "hit", "over-input"],
)
def test_cache_usage_not_reported_is_unknown_not_zero(
    monkeypatch: MonkeyPatch, usage: ResponseUsage, expected: int | None
) -> None:
    class Inner:
        async def create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(status="completed", output=[], usage=usage, model="test-model")

    async def call() -> None:
        await builtin_runtime._CompatResponses(Inner()).create(**_request())  # noqa: SLF001

    with (
        _chat_spans(monkeypatch) as spans,
        operation_trace("agent.execute", operation_id="run-2583-cache"),
    ):
        anyio.run(call)
    [span] = spans
    assert span.attributes["gen_ai.usage.input_tokens"] == 10
    assert span.attributes.get(CACHE) == expected
    # Run の外の呼び出しは構成と指紋だけ（予算は無い）。
    assert "pr.context.tools.fingerprint" in span.attributes
    assert "pr.context.budget.input_tokens" not in span.attributes


def test_provider_overflow_resend_records_usage_only_on_the_sent_attempt(
    monkeypatch: MonkeyPatch,
) -> None:
    """入力の超過で送り直した呼び出し: 超過の試行は usage を持たず、送り直しの試行だけが持つ。"""

    class Inner:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise _overflow_error()
            return SimpleNamespace(
                status="completed", output=[], usage=_usage(900, 10, 300), model="test-model"
            )

    window = builtin_runtime._ContextWindow(  # noqa: SLF001
        "run-2583-overflow",
        ContextLimits(model_id="test-model", max_input_tokens=128_000, max_output_tokens=4096),
        [EVIDENCE_TOOL],
        static_instructions=STATIC,
    )
    items = _items(_evidence("a", 40), _evidence("b", 40))

    async def call() -> None:
        builtin_runtime._ACTIVE_CONTEXT_WINDOW.set(window)  # noqa: SLF001
        await builtin_runtime._CompatResponses(Inner()).create(  # noqa: SLF001
            model="test-model", input=items, instructions=STATIC, tools=[]
        )

    with (
        _chat_spans(monkeypatch) as spans,
        operation_trace("agent.execute", operation_id="run-2583-overflow"),
    ):
        anyio.run(call)
    first, second = spans
    assert (first.attributes["pr.attempt"], second.attributes["pr.attempt"]) == (1, 2)
    assert first.status == "failed"
    assert "gen_ai.usage.input_tokens" not in first.attributes
    assert (second.attributes["gen_ai.usage.input_tokens"], second.attributes[CACHE]) == (900, 300)
    # 送り直しは縮めた入力（縮める前後と縮めた数）で、ツールの結果の見積もりが小さい。
    assert int(second.attributes["pr.context.compaction.outputs"]) >= 1
    assert int(second.attributes["pr.context.estimate.tool_outputs_tokens"]) < int(
        first.attributes["pr.context.estimate.tool_outputs_tokens"]
    )
    # 指示とツールの定義は同じ（送り直しで指紋は変わらない）。
    for key in ("pr.context.instructions.static_fingerprint", "pr.context.tools.fingerprint"):
        assert first.attributes[key] == second.attributes[key]


# --------------------------------------------------------------------------------------------
# Run の中の呼び出しと累計（承認の再開・差し戻し）
# --------------------------------------------------------------------------------------------


class _FakeResponses:
    """`responses.create` に台本の応答（出力と usage）を返す呼び先。"""

    def __init__(self, steps: list[tuple[list[Any], ResponseUsage]]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        output, usage = self.steps.pop(0)
        return Response.model_construct(
            id=f"resp-{len(self.requests)}",
            created_at=0,
            model="test-model",
            object="response",
            output=output,
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
            status="completed",
            usage=usage,
        )


class _FakeClient:
    base_url = "https://llm.example/openai/v1/"

    def __init__(self, steps: list[tuple[list[Any], ResponseUsage]]) -> None:
        self.responses = _FakeResponses(steps)


def _route(monkeypatch: MonkeyPatch, *steps: tuple[list[Any], ResponseUsage]) -> _FakeResponses:
    """Run のモデルを、OCI の client の層を通す fake の呼び先にする。"""
    _script_model(monkeypatch)
    client = _FakeClient(list(steps))

    def factory(target: Any) -> Any:
        return builtin_runtime.CompatResponsesModel(
            model=target.model_id,
            openai_client=client,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(builtin_runtime, "model_factory", factory)
    return client.responses


def _assert_no_double_count(run_id: str, spans: list[ObservedSpan]) -> None:
    """呼び出しごとの usage の和が、Run の累計（SDK の `Usage`）と一致する。"""
    run = runtime_repository.get_run(run_id)
    assert run.usage is not None
    sent = [span for span in spans if span.status == "done"]
    assert run.usage.requests == len(sent)
    assert run.usage.input_tokens == sum(
        int(span.attributes["gen_ai.usage.input_tokens"]) for span in sent
    )
    assert run.usage.output_tokens == sum(
        int(span.attributes["gen_ai.usage.output_tokens"]) for span in sent
    )


@pytest.mark.usefixtures("_calls_fixture")
def test_usage_per_call_and_cumulative_are_not_double_counted_across_approval(
    monkeypatch: MonkeyPatch,
) -> None:
    _route(
        monkeypatch,
        ([function_call(WRITE, {"query": "登録"}, call_id="call-2583")], _usage(100, 20, 40)),
        ([assistant_message("登録しました。")], _usage(150, 30, details=False)),
    )
    run_id = _create_run("合成の登録をして")

    with _chat_spans(monkeypatch) as spans:
        anyio.run(builtin_runtime.execute_run, run_id)
        waiting = runtime_repository.get_run(run_id)
        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert len(spans) == 1
        _assert_no_double_count(run_id, spans)
        [approval] = waiting.approvals
        runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))
        anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert len(spans) == 2
    _assert_no_double_count(run_id, spans)
    assert (run.usage.requests, run.usage.input_tokens) == (2, 250)  # type: ignore[union-attr]
    first, second = (span.attributes for span in spans)
    # cache: 返した呼び出しは値、返さなかった呼び出しは不明（属性なし）。
    assert (first[CACHE], CACHE in second) == (40, False)
    # 承認の前後で、指示の変わらない部分とツールの定義の指紋は同じ。
    for key in ("pr.context.instructions.static_fingerprint", "pr.context.tools.fingerprint"):
        assert first[key] == second[key]
    # Run の入力の管理の予算が付く（SDK の入力の filter と同じ文脈で読める）。
    assert int(first["pr.context.budget.input_tokens"]) > 0
    assert int(second["pr.context.estimate.tool_outputs_tokens"]) > 0
    assert int(second["pr.context.input.items"]) > int(first["pr.context.input.items"])


@pytest.mark.usefixtures("_agents_fixture")
def test_usage_is_not_double_counted_when_the_answer_is_sent_back(
    monkeypatch: MonkeyPatch,
) -> None:
    """出力の項目を記録せずに答えた回答の差し戻し（追加の検証の続き）でも二重に数えない。"""
    responses = _route(
        monkeypatch,
        ([function_call(OUTPUT_LOOKUP, {"query": "A"}, call_id="call-l")], _usage(100, 10, 0)),
        ([assistant_message("A は一致しました。")], _usage(120, 12, 60)),
        (
            [
                function_call(
                    RECORD_OUTPUT_TOOL,
                    {"rows": [{"item": "A", "matched": True}]},
                    call_id="call-r",
                )
            ],
            _usage(140, 14, 80),
        ),
        ([assistant_message("A は業務データと一致しました。")], _usage(160, 16, 90)),
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の照合をして", agent_id=OUTPUT_AGENT_ID),
        created_by_user_uuid="00000000-0000-0000-0000-000000002583",
    )

    with _chat_spans(monkeypatch) as spans:
        anyio.run(builtin_runtime.execute_run, run.id)

    finished = runtime_repository.get_run(run.id)
    assert finished.status == RunStatus.COMPLETED
    assert output_retried(finished.metadata)
    assert not responses.steps
    assert len(spans) == 4
    _assert_no_double_count(run.id, spans)
    assert finished.usage is not None and finished.usage.input_tokens == 520
    assert [span.attributes[CACHE] for span in spans] == [0, 60, 80, 90]
    assert len({span.attributes["pr.context.tools.fingerprint"] for span in spans}) == 1
