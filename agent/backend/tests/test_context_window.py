"""モデルに渡す入力を、モデルの入力の上限に収める（#2383）の決定論テスト。

縮め方（`context_window`）の単体と、組み込み Runtime に組み込んだ動き（SDK の `ScriptedModel` と
契約どおりの fake の MCP）を確かめる。ツールの結果は合成の巨大な根拠の一覧で、業務の内容は含まない。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pydantic import BaseModel
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import (
    CONTEXT_OVERFLOW_CODE,
    BuiltinRuntimeError,
    ModelTarget,
)
from app.features.agent.context_window import (
    COMPACTION_KEY,
    ContextBudget,
    ContextLimits,
    ProviderOverflow,
    compact_items,
    compact_output,
    provider_overflow,
)
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.observability import CONTEXT_COMPACTIONS_TOTAL
from app.settings import get_settings

SKILL_ID = "test2383-skill"
AGENT_ID = "test2383-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
EVIDENCE_TOOL = "rag__rag_retrieve_evidence"
READ_TOOL = "rag__rag_read_source"
# 合成の本文（業務の内容を含まない）。1 件の抜粋は MCP の上限と同じ 1000 文字。
_EXCERPT = ("合成の本文の文。" * 200)[:1000]


def _evidence(group: str, count: int) -> dict[str, Any]:
    """根拠 `count` 件の rag_retrieve_evidence の結果。

    ID は `doc-<group>-<n>` / `chunk-<group>-<n>`。
    """
    template = DEFAULT_OUTPUTS["rag_retrieve_evidence"]
    item = template["evidence"][0]
    return {
        **deepcopy(template),
        "trace_id": f"trace-{group}",
        "evidence": [
            {
                **deepcopy(item),
                "evidence_id": f"chunk-{group}-{index}",
                "document_id": f"doc-{group}-{index}",
                "chunk_id": f"chunk-{group}-{index}",
                "file_name": f"文書 {group.upper()}-{index}",
                "excerpt": _EXCERPT,
                "truncated": False,
                "text_length": len(_EXCERPT),
                "used_in_answer": False,
            }
            for index in range(count)
        ],
    }


def _ids(output: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["document_id"], item["chunk_id"]) for item in output["evidence"]]


def _budget(max_input: int, max_output: int = 1024, margin: float = 0.0) -> ContextBudget:
    return ContextBudget(
        ContextLimits(model_id="m", max_input_tokens=max_input, max_output_tokens=max_output),
        margin_ratio=margin,
    )


def _items(*outputs: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [{"role": "user", "content": "質問の文"}]
    for index, output in enumerate(outputs):
        call_id = f"call-{index}"
        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": EVIDENCE_TOOL,
                "arguments": "{}",
            }
        )
        items.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output, ensure_ascii=False),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 縮め方（単体）


def test_budget_subtracts_output_and_margin_from_input_limit() -> None:
    budget = _budget(128_000, 32_768, 0.05)
    assert budget.input_tokens() == 128_000 - 32_768 - 6_400
    # プロバイダの上限が設定より小さければ、そちらを使う。
    assert budget.input_tokens(100_000) == 100_000 - 32_768 - 6_400
    # 出力の上限が入力の上限と同じ小さいモデルでも、予算が 0 にならない（出力は半分まで引く）。
    assert _budget(8_192, 8_192).input_tokens() == 4_096


@pytest.mark.parametrize("level", [1, 2, 3, 4], ids=lambda level: f"level-{level}")
def test_compacted_output_keeps_every_evidence_id(level: int) -> None:
    output = _evidence("a", 30)
    compacted = json.loads(
        compact_output(json.dumps(output, ensure_ascii=False), level, read_tool=READ_TOOL)
    )

    assert _ids(compacted) == _ids(output)
    assert [item["evidence_id"] for item in compacted["evidence"]] == [
        item["evidence_id"] for item in output["evidence"]
    ]
    note = compacted[COMPACTION_KEY]
    assert note["original_chars"] == len(json.dumps(output, ensure_ascii=False))
    assert READ_TOOL in note["next_step"]
    # 縮めた結果を縮め直しても、元の文字数を残す。
    again = json.loads(compact_output(json.dumps(compacted, ensure_ascii=False), 4))
    assert again[COMPACTION_KEY]["original_chars"] == note["original_chars"]
    assert _ids(again) == _ids(output)


def test_level_one_shortens_excerpts_and_marks_them_truncated() -> None:
    output = _evidence("a", 3)
    compacted = json.loads(compact_output(json.dumps(output, ensure_ascii=False), 1))

    for item in compacted["evidence"]:
        assert item["truncated"] is True
        assert len(item["excerpt"]) < len(_EXCERPT)
        assert _EXCERPT.startswith(item["excerpt"].rstrip("…"))
    # 一覧の外の項目はそのまま。
    assert compacted["trace_id"] == "trace-a"
    # 呼び直しても副作用の無いツールと分からなければ、呼び直しを案内しない（#2632）。
    assert "呼び直して" not in compacted[COMPACTION_KEY]["next_step"]
    # 読み直すツールが無い信頼できる読み取りのツールは、絞って呼び直す案内にする。
    rerun = json.loads(compact_output(json.dumps(output, ensure_ascii=False), 1, rerunnable=True))
    assert "呼び直して" in rerun[COMPACTION_KEY]["next_step"]


# 合成の書き込みのツール（#2632）。
WRITE_TOOL = "synthetic__synthetic_write"
TRUSTED_READ_TOOL = "synthetic__synthetic_read"


def _write_items(name: str = WRITE_TOOL) -> list[dict[str, Any]]:
    """書き込みのツールの長い結果（`operation_id` と約 25,000 文字の `rows[].detail`）。"""
    output = {
        "operation_id": "op-2632",
        "rows": [{"row_id": f"row-{index}", "detail": "合成の詳細。" * 125} for index in range(33)],
    }
    return [
        {"role": "user", "content": "合成の質問"},
        {"type": "function_call", "call_id": "call-w", "name": name, "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "call-w",
            "output": json.dumps(output, ensure_ascii=False),
        },
        {"type": "function_call", "call_id": "call-r", "name": EVIDENCE_TOOL, "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-r", "output": "{}"},
    ]


def _compact_write(rerunnable: set[str]) -> dict[str, Any]:
    items = _write_items()
    result = compact_items(
        items,
        budget=_budget(2_400, 400),
        fixed_text="指示",
        read_tool_for=lambda _name: None,
        rerunnable=lambda name: name in rerunnable,
    )
    assert result.fits
    assert [item.tool_name for item in result.outputs] == [WRITE_TOOL]
    compacted: dict[str, Any] = json.loads(result.items[2]["output"])
    return compacted


def test_compacted_write_results_never_ask_to_rerun_the_operation() -> None:
    """書き込みの結果を縮めても、同じツールの呼び直し（同じ操作の再実行）を案内しない。"""
    compacted = _compact_write({TRUSTED_READ_TOOL})

    note = compacted[COMPACTION_KEY]
    assert "呼び直して" not in note["next_step"]
    assert "読み直せません" in note["next_step"]
    # ID は残す（結果と操作の対応を保つ）。
    assert compacted["operation_id"] == "op-2632"
    assert [row["row_id"] for row in compacted["rows"]][:3] == ["row-0", "row-1", "row-2"]
    # 縮め直しても制約を保つ。
    again = json.loads(compact_output(json.dumps(compacted, ensure_ascii=False), 4))
    assert "呼び直して" not in again[COMPACTION_KEY]["next_step"]
    assert again[COMPACTION_KEY]["original_chars"] == note["original_chars"]


def test_trusted_read_results_keep_the_rerun_hint() -> None:
    items = _write_items(TRUSTED_READ_TOOL)
    result = compact_items(
        items,
        budget=_budget(2_400, 400),
        fixed_text="指示",
        rerunnable=lambda name: name == TRUSTED_READ_TOOL,
    )
    compacted = json.loads(result.items[2]["output"])
    assert "呼び直して" in compacted[COMPACTION_KEY]["next_step"]


def test_items_within_budget_are_not_changed() -> None:
    items = _items(_evidence("a", 2))
    result = compact_items(items, budget=_budget(128_000), fixed_text="指示")

    assert result.items == items
    assert result.outputs == []
    assert result.fits


def test_older_tool_outputs_are_compacted_first_and_latest_is_kept() -> None:
    items = _items(_evidence("a", 40), _evidence("b", 40))
    total = compact_items(items, budget=_budget(10**6), fixed_text="").tokens_before
    # 2 件の結果の片方を縮めれば収まる予算。
    budget = _budget(int(total * 0.75) + 1024, 1024)

    result = compact_items(items, budget=budget, fixed_text="")

    assert result.fits
    assert [(item.call_id, item.latest) for item in result.outputs] == [("call-0", False)]
    # 最新の結果・利用者の質問・ツールの呼び出しはそのまま。
    assert result.items[0] == items[0]
    assert result.items[3:] == items[3:]
    older = json.loads(result.items[2]["output"])
    assert COMPACTION_KEY in older
    assert _ids(older) == _ids(_evidence("a", 40))
    # 元の入力は変えない。
    assert COMPACTION_KEY not in json.loads(items[2]["output"])


def test_latest_output_is_compacted_only_when_older_are_not_enough() -> None:
    items = _items(_evidence("a", 40), _evidence("b", 40))
    total = compact_items(items, budget=_budget(10**6), fixed_text="").tokens_before

    result = compact_items(items, budget=_budget(int(total * 0.35) + 1024, 1024), fixed_text="")

    assert result.fits
    levels = {item.call_id: (item.level, item.latest) for item in result.outputs}
    assert levels["call-0"][1] is False
    assert levels["call-1"][1] is True
    # 最新の結果は ID だけ（段階 4）にはしない。
    assert levels["call-1"][0] <= 3
    for index, group in ((2, "a"), (4, "b")):
        assert _ids(json.loads(result.items[index]["output"])) == _ids(_evidence(group, 40))


def test_items_that_cannot_fit_report_overflow() -> None:
    items = [{"role": "user", "content": "質問の文" * 5000}]
    result = compact_items(items, budget=_budget(8_192, 1_024), fixed_text="指示")

    assert not result.fits
    assert not result.fixed_exceeds
    assert result.items == items


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (
            "400",
            "The engine prompt length 160066 exceeds the max_model_len 131072. "
            "Please reduce prompt.",
            ProviderOverflow(prompt_tokens=160066, limit_tokens=131072),
        ),
        (
            "context_length_exceeded",
            "This model's maximum context length is 131072 tokens. However, your messages "
            "resulted in 160066 tokens.",
            ProviderOverflow(prompt_tokens=160066, limit_tokens=131072),
        ),
        ("context_length_exceeded", "too long", ProviderOverflow()),
        ("invalid_value", "reasoning_text is not supported by model m.", None),
        ("model_output_invalid", "Please retry.", None),
    ],
    ids=["vllm", "openai", "code-only", "other-400", "output-invalid"],
)
def test_provider_overflow_is_detected_from_the_error(
    code: str, message: str, expected: ProviderOverflow | None
) -> None:
    assert provider_overflow(code, message) == expected


# ---------------------------------------------------------------------------
# 組み込み Runtime


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    """RAG の根拠の収集と本文の読み取りを使う業務 Agent。"""
    fake = fake_product_mcp(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="入力の上限の Skill",
            instructions="RAG で調べて答える。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag", tool_names=["rag_retrieve_evidence", "rag_read_source"]
                )
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="入力の上限の業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield fake
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _script(
    monkeypatch: MonkeyPatch, *steps: Any, max_input: int, max_output: int = 2048
) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model-2383",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
            max_input_tokens=max_input,
            max_output_tokens=max_output,
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _serve_evidence(mcp: FakeProductMcp, count: int) -> None:
    """query ごとに別の根拠の一覧を返す（query の末尾の 1 文字を ID の印にする）。"""

    def output(argument: BaseModel) -> dict[str, Any]:
        query = str(getattr(argument, "query", ""))
        return _evidence(query[-1], count)

    mcp.outputs["rag_retrieve_evidence"] = output


def _validate_cited(argument: BaseModel) -> dict[str, Any]:
    """渡された根拠のうち、回答が書いた chunk の id を裏付けとして返す検証の結果。"""
    answer = str(getattr(argument, "answer", ""))
    evidence = list(getattr(argument, "evidence", []) or [])
    claims = [
        {
            "answer_quote": answer,
            "status": "supported",
            "chunk_id": ref.chunk_id,
            "reason": "合成の根拠に書かれている。",
        }
        for ref in evidence
        if ref.chunk_id in answer
    ]
    return {
        **deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"]),
        "counts": {"supported": len(claims), "unsupported": 0, "contradicted": 0},
        "claims": claims,
    }


def _tool_outputs(call_input: Any) -> dict[str, str]:
    return {
        item["call_id"]: item["output"]
        for item in call_input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    }


def _compactions(run: RunState) -> list[dict[str, Any]]:
    return [
        event.payload["context_compaction"]
        for event in run.events
        if "context_compaction" in event.payload
    ]


def _metric(model: str, trigger: str, outcome: str) -> float:
    value: float = CONTEXT_COMPACTIONS_TOTAL.labels(
        model=model, trigger=trigger, outcome=outcome
    )._value.get()
    return value


def test_run_compacts_older_evidence_and_keeps_validation_working(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _serve_evidence(mcp, 40)
    mcp.outputs["rag_validate_answer"] = _validate_cited
    answer = "合成の本文の文が書かれています（chunk-a-3、chunk-b-7）。"
    model = _script(
        monkeypatch,
        [function_call(EVIDENCE_TOOL, {"query": "根拠 a"}, call_id="call-1")],
        [function_call(EVIDENCE_TOOL, {"query": "根拠 b"}, call_id="call-2")],
        [assistant_message(answer)],
        # 根拠 1 件の一覧（約 40k tokens）を 2 つ全文では渡せず、1 つなら渡せる上限。
        max_input=64_000,
    )
    before = _metric("test-model-2383", "budget", "compacted")
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )

    anyio.run(builtin_runtime.execute_run, run.id)
    run = runtime_repository.get_run(run.id)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 3
    # 2 回目の呼び出し（根拠 1 件の一覧）は縮めない。
    assert all(
        COMPACTION_KEY not in output for output in _tool_outputs(model.calls[1].input).values()
    )
    # 3 回目: 古い結果だけを縮め、最新の結果・利用者の質問はそのまま渡す。
    final_input = model.calls[2].input
    outputs = _tool_outputs(final_input)
    older, latest = json.loads(outputs["call-1"]), json.loads(outputs["call-2"])
    assert COMPACTION_KEY in older
    assert READ_TOOL in older[COMPACTION_KEY]["next_step"]
    assert COMPACTION_KEY not in latest
    assert latest["evidence"][0]["excerpt"] == _EXCERPT
    assert any(
        item.get("content") == "合成の質問" for item in final_input if isinstance(item, dict)
    )
    # 縮めても根拠の ID はすべて残る。
    assert _ids(older) == _ids(_evidence("a", 40))
    assert _ids(latest) == _ids(_evidence("b", 40))
    # Run の step のツールの結果は全文のまま。
    step_outputs = [
        step.tool_result.output
        for step in run.steps
        if step.tool_call is not None
        and step.tool_call.name == EVIDENCE_TOOL
        and step.tool_result is not None
    ]
    assert [(output or {})["evidence"][0]["excerpt"] for output in step_outputs] == [
        _EXCERPT,
        _EXCERPT,
    ]
    # 縮めたことを Run のイベントと metric に残す（同じ縮め方は 1 回だけ）。
    [record] = _compactions(run)
    assert record["trigger"] == "budget"
    assert record["model"] == "test-model-2383"
    assert record["max_input_tokens"] == 64_000
    assert record["estimated_tokens_after"] <= record["budget_tokens"]
    assert record["estimated_tokens_before"] > record["budget_tokens"]
    assert [(item["call_id"], item["latest"]) for item in record["outputs"]] == [("call-1", False)]
    assert _metric("test-model-2383", "budget", "compacted") == before + 1
    # 回答の検証は step の根拠（全文）で、回答が書いた根拠の ID と本文の対応のまま動く。
    [validation_call] = mcp.calls_of("rag_validate_answer")
    sent = {
        (ref["document_id"], ref["chunk_id"]) for ref in validation_call["arguments"]["evidence"]
    }
    assert {("doc-a-3", "chunk-a-3"), ("doc-b-7", "chunk-b-7")} <= sent
    [validation] = [item for item in run.artifacts if item.kind == "answer_validation"]
    assert validation.content["status"] == "completed"
    assert validation.content["valid"] is True


def test_input_that_cannot_fit_fails_before_calling_the_model(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    del mcp
    model = _script(monkeypatch, [assistant_message("回答")], max_input=4_096, max_output=1_024)
    before = _metric("test-model-2383", "budget", "failed")
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問の文。" * 2000, agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, run.id)
    run = runtime_repository.get_run(run.id)

    assert run.status == RunStatus.FAILED
    assert len(model.calls) == 0
    [failed] = [event for event in run.events if event.payload.get("error_code")]
    assert failed.payload["error_code"] == CONTEXT_OVERFLOW_CODE
    assert "入力の上限" in failed.message
    assert "絞って" in failed.message
    assert _metric("test-model-2383", "budget", "failed") == before + 1


def test_compaction_can_be_disabled(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    del mcp
    monkeypatch.setattr(get_settings(), "agent_context_compaction_enabled", False)
    model = _script(monkeypatch, [assistant_message("回答")], max_input=4_096, max_output=1_024)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問の文。" * 2000, agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, run.id)

    # 縮めず・止めずにモデルを呼ぶ。
    assert len(model.calls) >= 1
    assert not _compactions(runtime_repository.get_run(run.id))


# ---------------------------------------------------------------------------
# プロバイダの入力の超過（400）の送り直し


def _overflow_error(prompt: int = 160066, limit: int = 131072) -> Any:
    # openai は httpx2 の Response を受け取る（httpx の Response は型が合わない）。
    import httpx2
    from openai import BadRequestError

    body = {
        "code": "400",
        "message": f"The engine prompt length {prompt} exceeds the max_model_len {limit}. "
        "Please reduce prompt.",
        "param": None,
        "type": "BadRequestError",
    }
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://llm.example/responses"))
    return BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


class _Inner:
    def __init__(self, failures: list[Exception]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._failures = list(failures)

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self._failures:
            raise self._failures.pop(0)

        class _Done:
            status = "completed"

        return _Done()


def _window(run_id: str, max_input: int = 128_000) -> Any:
    return builtin_runtime._ContextWindow(  # noqa: SLF001 - 送り直しの単体
        run_id,
        ContextLimits(
            model_id="test-model-2383", max_input_tokens=max_input, max_output_tokens=4096
        ),
        [EVIDENCE_TOOL, READ_TOOL],
    )


def _builtin_run(mcp: FakeProductMcp) -> str:
    del mcp
    return runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    ).id


def test_provider_overflow_is_compacted_and_resent_once(mcp: FakeProductMcp) -> None:
    run_id = _builtin_run(mcp)
    window = _window(run_id)
    items = _items(_evidence("a", 40), _evidence("b", 40))
    inner = _Inner([_overflow_error()])
    before = _metric("test-model-2383", "provider_limit", "compacted")

    async def call() -> Any:
        builtin_runtime._ACTIVE_CONTEXT_WINDOW.set(window)  # noqa: SLF001
        return await builtin_runtime._ModelApiResponses(inner).create(  # noqa: SLF001
            model="test-model-2383", input=items, instructions="指示", tools=[]
        )

    response = anyio.run(call)

    assert response.status == "completed"
    assert len(inner.requests) == 2
    resent = _tool_outputs(inner.requests[1]["input"])
    assert COMPACTION_KEY in json.loads(resent["call-0"])
    for call_id, group in (("call-0", "a"), ("call-1", "b")):
        assert _ids(json.loads(resent[call_id])) == _ids(_evidence(group, 40))
    # プロバイダが数えた token 数で、Run の残りの見積もりを補正する。
    assert window.budget.estimate_scale > 1.0
    [record] = _compactions(runtime_repository.get_run(run_id))
    assert record["trigger"] == "provider_limit"
    assert record["provider_limit_tokens"] == 131072
    assert _metric("test-model-2383", "provider_limit", "compacted") == before + 1


def test_second_provider_overflow_fails_with_a_clear_reason(mcp: FakeProductMcp) -> None:
    window = _window(_builtin_run(mcp))
    items = _items(_evidence("a", 40), _evidence("b", 40))
    inner = _Inner([_overflow_error(), _overflow_error(140000)])

    async def call() -> Any:
        builtin_runtime._ACTIVE_CONTEXT_WINDOW.set(window)  # noqa: SLF001
        return await builtin_runtime._ModelApiResponses(inner).create(  # noqa: SLF001
            model="test-model-2383", input=items, instructions="指示", tools=[]
        )

    with pytest.raises(BuiltinRuntimeError) as raised:
        anyio.run(call)

    assert raised.value.code == CONTEXT_OVERFLOW_CODE
    assert "入力の上限" in raised.value.message
    assert len(inner.requests) == 2


def test_provider_overflow_without_a_run_window_is_raised_as_is() -> None:
    from openai import BadRequestError

    inner = _Inner([_overflow_error()])

    async def call() -> Any:
        builtin_runtime._ACTIVE_CONTEXT_WINDOW.set(None)  # noqa: SLF001
        return await builtin_runtime._ModelApiResponses(inner).create(  # noqa: SLF001
            model="m", input=[], instructions="指示", tools=[]
        )

    with pytest.raises(BadRequestError):
        anyio.run(call)
    assert len(inner.requests) == 1


def test_only_trusted_read_only_tools_are_offered_for_rerun() -> None:
    """Run が呼び直しを案内するのは、信頼できる読み取り専用のツールだけ（#2632）。

    書き込みのツールと、注釈を信頼しない接続が読み取り専用と申告したツールは対象にしない。縮めても
    ツールは実行しない（副作用の handler の実行数は増えない）。
    """
    from app.features.agent.config import McpConnectionConfig
    from app.features.agent.tools import (
        ExternalMcpToolInfo,
        ToolInvocationContext,
        mcp_tool_definition,
    )

    executed: list[str] = []

    def handler(name: str) -> Any:
        def handle(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
            executed.append(name)
            return {}

        return handle

    trusted = McpConnectionConfig(
        server_id="synthetic", source="runtime", trust_tool_annotations=True
    )
    untrusted = McpConnectionConfig(server_id="untrusted", source="runtime")
    entries = [
        (
            mcp_tool_definition(
                config, ExternalMcpToolInfo(name=name, server_id=config.server_id, read_only=ro)
            ),
            handler(name),
        )
        for config, name, ro in (
            (trusted, "synthetic_write", False),
            (trusted, "synthetic_read", True),
            (untrusted, "synthetic_read", True),
        )
    ]
    tools = builtin_runtime.build_function_tools("run_2632", [], entries)

    rerunnable = builtin_runtime._rerunnable_tool_names(tools)  # noqa: SLF001

    assert rerunnable == {TRUSTED_READ_TOOL}
    window = builtin_runtime._ContextWindow(  # noqa: SLF001
        "run_2632",
        ContextLimits(model_id="test-model-2632", max_input_tokens=2_400, max_output_tokens=400),
        [tool.name for tool in tools],
        rerunnable,
    )
    for name in (WRITE_TOOL, "untrusted__synthetic_read"):
        result = compact_items(
            _write_items(name),
            budget=_budget(2_400, 400),
            fixed_text="指示",
            read_tool_for=window.read_tool_for,
            rerunnable=window.rerunnable,
        )
        note = json.loads(result.items[2]["output"])[COMPACTION_KEY]
        assert "呼び直して" not in note["next_step"], name
    assert executed == []
