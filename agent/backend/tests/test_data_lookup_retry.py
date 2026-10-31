"""業務データを引かずに「資料からは確認できない」と答えた回答の差し戻し（#1979）の決定論テスト。

資料（RAG）と業務データ（NL2SQL）の両方を持つ業務 Agent で、モデルが資料だけを引いて「モニターの
単価は資料には記載がありません」と答えたら、`nl2sql_query` で引くよう 1 回だけ差し戻す（略号・別名の
差し戻し #1958 と同じ 1 回の枠）。モデルは SDK の `ScriptedModel`、RAG / NL2SQL は契約どおりの
fake の MCP（`mcp_support`）にする。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStep,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import DATA_LOOKUP_HINT, missing_data_lookup
from app.features.agent.tool_grounding import METADATA_KEY
from app.features.agent.tools import ToolCall, ToolPolicy, ToolResult
from app.settings import get_settings

SKILL_ID = "test1979-skill"
AGENT_ID = "test1979-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
EVIDENCE = "rag__rag_retrieve_evidence"
QUERY = "nl2sql__nl2sql_query"
GOAL = "経費精算規程の会食費の 1 人あたりの上限は、モニターの単価の何分の 1 ですか？"
ABSENT = "会食費の上限は 1 人あたり 10,000 円です。\n\nモニターの単価は資料には記載がありません。"
TOOLS = [EVIDENCE, QUERY, "rag__rag_search"]


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    # ツール権限は保存先の状態に依らず、承認なしにする（並列・直列のどちらでも同じ結果にする）。
    monkeypatch.setattr(builtin_runtime, "_active_policy", lambda: ToolPolicy())
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="資料と業務データで調べて答える。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_retrieve_evidence"]),
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_query"]),
            ],
        )
    )
    runtime_repository.create_agent(AgentProfile(id=AGENT_ID, name=AGENT_ID, skill_ids=[SKILL_ID]))
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


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run() -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=GOAL, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _step(name: str) -> RunStep:
    return RunStep(
        id=f"step-{name}",
        run_id="run-1979",
        title=name,
        tool_call=ToolCall(name=name, arguments={"query": "会食費 上限"}, trace_id=name),
        tool_result=ToolResult(name=name, success=True, output={"evidence": []}),
    )


def _grounding_events(run: RunState) -> list[dict[str, Any]]:
    return [
        event.payload["tool_grounding"] for event in run.events if "tool_grounding" in event.payload
    ]


def test_missing_data_lookup_needs_an_absent_value_without_the_data_query() -> None:
    steps = [_step(EVIDENCE)]
    # 資料だけを引き、資料に無いとした値がある（業務データのツールを持つ）。
    assert missing_data_lookup(steps, ABSENT, TOOLS) is True
    # 業務データでの確認を利用者に求めた（引いていない）。
    deferred = (
        "5 万円未満なら所属長です。該当する受注があるかは、業務データで確認する必要があります。"
    )
    assert missing_data_lookup(steps, deferred, TOOLS) is True
    # 業務データを引いた・業務データのツールを持たない・資料を引いていない・資料に無いと述べない。
    assert missing_data_lookup([*steps, _step(QUERY)], ABSENT, TOOLS) is False
    assert missing_data_lookup(steps, ABSENT, [EVIDENCE]) is False
    assert missing_data_lookup([], ABSENT, TOOLS) is False
    assert missing_data_lookup(steps, "上限は 10,000 円で、単価の 3 分の 1 です。", TOOLS) is False


def test_answer_without_the_data_query_is_sent_back_once(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        [function_call(EVIDENCE, {"query": "経費精算規程 会食費 上限"}, call_id="c1")],
        [assistant_message(ABSENT)],
        [
            function_call(
                QUERY, {"question": "モニターの単価", "profile_id": "profile-sales"}, call_id="c2"
            )
        ],
        [assistant_message("上限 10,000 円は、モニターの単価 30,000 円の 3 分の 1 です。")],
    )

    run = _run()

    assert len(model.calls) == 4
    users = [item for item in model.calls[2].input if isinstance(item, dict)]
    retry = [item for item in users if item.get("role") == "user"][-1]["content"]
    assert retry.startswith(DATA_LOOKUP_HINT)
    assert ABSENT in retry
    assert len(mcp.calls_of("nl2sql_query")) == 1
    answer = [item for item in run.artifacts if item.kind == "answer"][-1].content["text"]
    assert answer == "上限 10,000 円は、モニターの単価 30,000 円の 3 分の 1 です。"
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1, "data_retry": True}
    assert _grounding_events(run) == [{"action": "data_retry", "original_answer": ABSENT}]


def test_data_lookup_retry_is_not_repeated(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    model = _script(
        monkeypatch,
        [function_call(EVIDENCE, {"query": "経費精算規程 会食費 上限"}, call_id="c1")],
        [assistant_message(ABSENT)],
        [assistant_message(ABSENT)],
    )

    run = _run()

    # 差し戻しは 1 回だけ。差し戻しの後も引かなければ、そのまま出す（拒答にはしない）。
    assert len(model.calls) == 3
    assert mcp.calls_of("nl2sql_query") == []
    answer = [item for item in run.artifacts if item.kind == "answer"][-1].content["text"]
    assert answer == ABSENT
    assert [event["action"] for event in _grounding_events(run)] == ["data_retry"]


def test_answer_with_the_data_query_is_not_sent_back(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        [function_call(EVIDENCE, {"query": "経費精算規程 会食費 上限"}, call_id="c1")],
        [
            function_call(
                QUERY, {"question": "モニターの単価", "profile_id": "profile-sales"}, call_id="c2"
            )
        ],
        [assistant_message(ABSENT)],
    )

    run = _run()

    assert len(model.calls) == 3
    assert _grounding_events(run) == []
