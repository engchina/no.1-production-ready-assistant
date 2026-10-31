"""Run の trace の木の形（処理の経過の親子。#3226・#3272・#3471）。

画面の処理の経過は span の木の投影なので、工程の中で起きた処理（モデル・ツール・MCP・呼び先の製品の
処理）がその工程の span の子になっていることを、主な経路で確かめる。

- モデルの呼び出し（`gen_ai.chat`）とツールの呼び出し（`gen_ai.execute_tool`）は実行の全体の span の
  下に順に並ぶ（ツールは求めたモデルの呼び出しの後に動くので兄弟）。
- MCP の呼び出し（`mcp.request`）はツールの span の子で、呼び先の製品の server の span
  （`mcp.server`）は送った `tools/call` の `mcp.request` の子（`params._meta` の traceparent）。
- 根拠の取得の後に Runtime が足す業務ガイドの照合（#1322）は、根拠の取得のツールの子（#3471）。
- NL2SQL のジョブの続きの取得（#848）は、ジョブを作ったツールの span の子。

組み込み Runtime は SDK の `ScriptedModel` で台本にし、RAG / NL2SQL は契約どおりの fake の MCP
（`mcp_support`）にする。fake の RAG は、受けた `params._meta` の traceparent で共通の
`mcp_server_trace` を始め（別の process と同じく、Agent の文脈を引き継がない）、span を同じ表に
書く。
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, _job, fake_product_mcp
from pr_backend_core.observability.mcp_trace import mcp_server_trace
from pr_backend_core.observability.trace_store import StoredTraceSpan, TraceSpanOwner
from pr_backend_core.observability.tracing import SpanDisplay, observed_span
from pydantic import BaseModel
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.trace_spans import trace_spans
from app.settings import get_settings

SKILL_ID = "test3471-skill"
AGENT_ID = "test3471-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
PROFILE = "bv-sales"

ROOT = "agent.execute"
CHAT = "gen_ai.chat"
TOOL = "gen_ai.execute_tool"
MCP = "mcp.request"
SERVER = "mcp.server"
RAG_WORK = "rag.test_retrieve"


class _TracedScriptedModel(ScriptedModel):
    """OCI の client と同じく、モデルの呼び出しを `gen_ai.chat` の span で測る台本のモデル。"""

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        with observed_span(CHAT, kind="client") as span:
            response = await super().get_response(*args, **kwargs)
            builtin_runtime._set_turn_kind(span, response)  # noqa: SLF001 - OCI の client と同じ印
            return response


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> None:
    model = _TracedScriptedModel(list(steps))
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


@contextlib.contextmanager
def _agent(tools: dict[str, list[str]]) -> Iterator[None]:
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="合成の指示。",
            mcp_requirements=[
                SkillMcpRequirement(server_id=server, tool_names=names)
                for server, names in tools.items()
            ],
        )
    )
    runtime_repository.create_agent(AgentProfile(id=AGENT_ID, name=AGENT_ID, skill_ids=[SKILL_ID]))
    try:
        yield
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _run(goal: str) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _rag_server(fake: FakeProductMcp, tool: str) -> Any:
    """受けた traceparent（`params._meta`）で RAG の server の span を始め、中で工程の span を開く。

    呼び先は別の process なので、Agent の文脈（今の span・記録）を引き継がない空の文脈で動かす。
    """

    def output(_argument: BaseModel) -> dict[str, Any]:
        request = next(
            item
            for item in reversed(fake.requests)
            if item["method"] == "tools/call" and item["body"]["params"]["name"] == tool
        )
        meta = request["body"]["params"].get("_meta") or {}
        owner = TraceSpanOwner(product="rag", operation_id=f"rag-{tool}", owner_user_uuid=USER_UUID)

        def serve() -> None:
            with (
                mcp_server_trace(
                    operation_id=owner.operation_id,
                    trusted=True,
                    traceparent=meta.get("traceparent"),
                    listener=lambda update: trace_spans.submit(update, owner),
                    method="tools/call",
                    tool_name=tool,
                ),
                observed_span(RAG_WORK, display=SpanDisplay(kind="retrieve")),
            ):
                pass

        contextvars.Context().run(serve)
        return deepcopy(DEFAULT_OUTPUTS[tool])

    return output


class _Tree:
    """Run の trace の span（表の行と同じ形）を親子で引く。"""

    def __init__(self, run: RunState) -> None:
        assert run.trace_id is not None
        self.spans = trace_spans.fetch(run.trace_id, viewer_user_uuid=None)
        self.by_id = {span.span_id: span for span in self.spans}

    def parent(self, span: StoredTraceSpan) -> StoredTraceSpan | None:
        return self.by_id.get(span.parent_span_id or "")

    def children(self, span: StoredTraceSpan, name: str | None = None) -> list[StoredTraceSpan]:
        return [
            item
            for item in self.spans
            if item.parent_span_id == span.span_id and (name is None or item.name == name)
        ]

    def one(self, name: str, **attributes: Any) -> StoredTraceSpan:
        [found] = self.all(name, **attributes)
        return found

    def all(self, name: str, **attributes: Any) -> list[StoredTraceSpan]:
        return [
            span
            for span in self.spans
            if span.name == name
            and all(span.attributes.get(key) == value for key, value in attributes.items())
        ]

    def tool(self, name: str) -> StoredTraceSpan:
        return self.one(TOOL, **{"gen_ai.tool.name": name})

    def tools_call(self, tool: str) -> list[StoredTraceSpan]:
        return self.all(MCP, **{"mcp.method.name": "tools/call", "gen_ai.tool.name": tool})


@pytest.fixture
def rag(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch)
    fake.outputs["rag_retrieve_evidence"] = _rag_server(fake, "rag_retrieve_evidence")
    with _agent({"rag": ["rag_retrieve_evidence", "rag_lookup_guides", "rag_validate_answer"]}):
        yield fake


def test_tool_call_with_mcp_to_rag_nests_each_step_under_its_owner(
    monkeypatch: MonkeyPatch, rag: FakeProductMcp
) -> None:
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_retrieve_evidence",
                {"query": "合成の語", "search_answer_profile_id": PROFILE},
                call_id="call-1",
            )
        ],
        [assistant_message("根拠付き回答です。")],
    )

    run = _run("合成の質問ですか？")

    assert run.status == RunStatus.COMPLETED
    tree = _Tree(run)
    root = tree.one(ROOT)
    assert root.root and root.product == "agent"
    # モデルの呼び出しとツールの呼び出しは、実行の全体の下に順に並ぶ。
    top = [
        (span.name, span.display_kind, span.attributes.get("gen_ai.tool.name"))
        for span in tree.children(root)
        if span.name != MCP
    ]
    assert top == [
        ("agent.attachment_evidence", None, None),
        (CHAT, "plan", None),
        (TOOL, "tool", "rag__rag_retrieve_evidence"),
        (CHAT, "respond", None),
        # 回答の最終の検証（#1246）は Runtime の独立の工程。
        (TOOL, "tool", "rag__rag_validate_answer"),
    ]
    evidence = tree.tool("rag__rag_retrieve_evidence")
    # 業務ガイドの照合（#1322）は根拠の取得の一部なので、根拠の取得のツールの子（#3471）。
    guides = tree.tool("rag__rag_lookup_guides")
    assert tree.parent(guides) == evidence
    assert guides.started_at >= evidence.started_at
    assert evidence.finished_at is not None and guides.finished_at is not None
    assert guides.finished_at <= evidence.finished_at
    # MCP の呼び出しは、それを送ったツールの span の子（ツールの thread でも）。
    [evidence_call] = tree.tools_call("rag_retrieve_evidence")
    assert tree.parent(evidence_call) == evidence
    [guides_call] = tree.tools_call("rag_lookup_guides")
    assert tree.parent(guides_call) == guides
    [validate_call] = tree.tools_call("rag_validate_answer")
    assert tree.parent(validate_call) == tree.tool("rag__rag_validate_answer")
    # 呼び先（RAG）の server の span は、送った `tools/call` の `mcp.request` の子で、RAG の工程は
    # その下（同じ trace）。
    [server] = tree.all(SERVER)
    assert server.product == "rag" and server.trace_id == root.trace_id
    assert tree.parent(server) == evidence_call
    [work] = tree.children(server, RAG_WORK)
    assert work.product == "rag" and work.display_kind == "retrieve"


def test_job_follow_up_calls_stay_under_the_tool_that_created_the_job(
    monkeypatch: MonkeyPatch,
) -> None:
    fake = fake_product_mcp(
        monkeypatch, outputs={"nl2sql_query": {**_job(), "status": "running", "job_id": "job-1"}}
    )
    _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_query",
                {"question": "合成の件数", "profile_id": "profile-sales"},
                call_id="call-1",
            )
        ],
        [assistant_message("3 件です。")],
    )
    with _agent({"nl2sql": ["nl2sql_query", "nl2sql_get_job"]}):
        run = _run("合成の件数は？")

    assert run.status == RunStatus.COMPLETED
    assert [call["name"] for call in fake.tool_calls] == ["nl2sql_query", "nl2sql_get_job"]
    tree = _Tree(run)
    query = tree.tool("nl2sql__nl2sql_query")
    assert tree.parent(query) == tree.one(ROOT)
    # ジョブの続きの取得（#848）はモデルの呼び出しを挟まず、ジョブを作ったツールの span の中。
    calls = [*tree.tools_call("nl2sql_query"), *tree.tools_call("nl2sql_get_job")]
    assert len(calls) == 2
    assert {call.parent_span_id for call in calls} == {query.span_id}
    assert not tree.all(TOOL, **{"gen_ai.tool.name": "nl2sql__nl2sql_get_job"})
