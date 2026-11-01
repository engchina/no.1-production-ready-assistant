"""組み込み Runtime（OpenAI Agents SDK + モデルの API。#754）の決定論テスト。

モデルは SDK の `ScriptedModel`（台本どおりに応答する）に差し替え、モデルの API へは接続しない。
ツールはテスト用に登録し、ポリシー・承認・step・成果物・再開を確かめる。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import anyio
import pytest
from agents import FunctionTool, Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from mcp_support import fake_product_mcp
from pr_backend_core.chat_history import CHAT_HISTORY_UNTRUSTED_NOTE, ChatHistoryTurn
from pr_system_settings.answer_notice import DEFAULT_ANSWER_NOTICE_TEXT
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.config import runtime_config_store
from app.features.agent.run_facts import fact_from_run
from app.features.agent.runtime import (
    BUILTIN_RUNTIME_ID,
    DELETED_THREAD_METADATA_KEY,
    MCP_APPROVAL_SKIPPED_CODE,
    AgentProfile,
    AgentProfilePatch,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunStatus,
    ThreadBusyError,
    ThreadNotFoundError,
    builtin_resume_pending,
    get_runtime_repository,
    mcp_approval_message,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import (
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    tool_registry,
)
from app.features.agent.trace_spans import trace_spans
from app.features.agent.usage import build_usage_report
from app.settings import get_settings

LOOKUP = "test754_lookup"
WRITE = "test754_write"
SKILL_ID = "test754-skill"
AGENT_ID = "test754-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"


class _Calls:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any], ToolInvocationContext]] = []

    def handler(self, name: str) -> Any:
        def handle(arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
            self.items.append((name, dict(arguments), context))
            return {"name": name, "rows": 3}

        return handle


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }


@pytest.fixture
def calls() -> Iterator[_Calls]:
    recorded = _Calls()
    for name, level, side_effects in (
        (LOOKUP, ToolPermissionLevel.READ, False),
        (WRITE, ToolPermissionLevel.WRITE, True),
    ):
        tool_registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} のテスト用ツール",
                input_schema=_schema(),
                output_schema={"type": "object"},
                permission_level=level,
                side_effects=side_effects,
            ),
            recorded.handler(name),
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="テストの Skill",
            instructions="売上は test754_lookup で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="control-plane", tool_names=[LOOKUP, WRITE])
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name="テストの業務 Agent",
            instructions="経理の質問に答える。",
            skill_ids=[SKILL_ID],
        )
    )
    try:
        yield recorded
    finally:
        # このテストの Run も消す（削除した Agent を参照する Run が残ると、同じ worker の後の
        # snapshot の検証が失敗する）。
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        for name in (LOOKUP, WRITE):
            tool_registry._definitions.pop(name, None)  # noqa: SLF001 - テスト用の登録を戻す
            tool_registry._handlers.pop(name, None)  # noqa: SLF001


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _create_run(goal: str = "今月の売上を教えて") -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    assert run.runtime_id == BUILTIN_RUNTIME_ID
    assert run.status == RunStatus.QUEUED
    return run.id


def test_instructions_and_tools_come_from_agent_and_skills(calls: _Calls) -> None:
    del calls
    instructions = builtin_runtime.compose_instructions("経理の質問に答える。", [SKILL_ID])
    assert "経理の質問に答える。" in instructions
    assert "# Skill: テストの Skill" in instructions
    assert builtin_runtime.agent_tool_names([SKILL_ID]) == [LOOKUP, WRITE]
    tools = {
        tool.name: tool for tool in builtin_runtime.build_function_tools("run_x", [LOOKUP, WRITE])
    }
    # 読み取りのツールは承認なし、書き込み（副作用あり）のツールは既定の policy で承認が必要。
    # 承認が要るかは呼び出しごとに最新のツール権限で決める（#2610）。
    assert _needs_approval(tools[LOOKUP], {"query": "売上"}) is False
    assert _needs_approval(tools[WRITE], {"query": "登録"}) is True
    assert tools[LOOKUP].strict_json_schema is True


def _needs_approval(tool: FunctionTool, arguments: dict[str, Any]) -> bool:
    """function tool の `needs_approval`（呼び出しごとの判定。#2610）をその時点で評価する。"""
    gate = tool.needs_approval
    assert callable(gate)
    decided = anyio.run(gate, cast(Any, None), arguments, "call-check")
    assert isinstance(decided, bool)
    return decided


def test_run_calls_tool_and_records_answer(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    model = _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "売上"}, call_id="call-1")],
        [assistant_message("今月の売上は 3 件です。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert [(name, args) for name, args, _ in calls.items] == [(LOOKUP, {"query": "売上"})]
    # ツールは Run の利用者として呼ぶ（RAG / NL2SQL のサービストークンの sub。#233）。
    assert calls.items[0][2].user_uuid == USER_UUID
    assert calls.items[0][2].run_id == run_id
    [step] = run.steps
    assert step.status == "completed"
    assert step.tool_call is not None and step.tool_call.trace_id == "call-1"
    answer = next(item for item in run.artifacts if item.kind == "answer")
    # 回答の対応（#1305）。Control Plane のツールは現場のデータの道具に数えない。
    assert answer.content == {
        "text": "今月の売上は 3 件です。",
        "outcome": {
            "schema_version": 1,
            "value": "answered",
            "basis": "answer_passages",
            "rag_outcome": None,
            "signals": [],
        },
        # 回答の注意書き（#2405。本文とは別の項目）。
        "notice": DEFAULT_ANSWER_NOTICE_TEXT,
    }
    # モデルには Agent と Skill の指示と、ツールの結果を渡している。
    first, second = model.calls
    assert "経理の質問に答える。" in str(first.system_instructions)
    assert '"rows": 3' in str(second.input)


@pytest.fixture
def trace_provider() -> Iterator[None]:
    from pr_backend_core.config.tracing import TracingSettings
    from pr_backend_core.observability.tracing import configure_tracing, shutdown_tracing

    configure_tracing(TracingSettings(), service_name="synthetic")
    try:
        yield
    finally:
        shutdown_tracing()


def test_tool_requiring_approval_waits_and_resumes(
    monkeypatch: MonkeyPatch, calls: _Calls, trace_provider: None
) -> None:
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-w")],
        [assistant_message("登録しました。")],
    )
    run_id = _create_run("登録して")

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    assert calls.items == []
    [approval] = waiting.approvals
    assert approval.tool_call.name == WRITE
    assert approval.tool_call.arguments == {"query": "登録"}

    decided = runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by="approver1")
    )
    assert decided.status == RunStatus.QUEUED
    assert builtin_resume_pending(decided)

    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    # 実行と再開の区間は共通の span の表の 1 つの trace（#3226 の案 A）。再開の区間は実行の
    # 区間の子。
    assert run.trace_id is not None
    spans = trace_spans.fetch(run.trace_id, viewer_user_uuid=None)
    first, resumed = [span for span in spans if span.root]
    assert (first.name, resumed.name) == ("agent.execute", "agent.resume")
    assert resumed.parent_span_id == first.span_id
    assert {span.trace_id for span in spans} == {run.trace_id}
    assert {span.owner_user_uuid for span in spans} == {USER_UUID}
    assert all(
        span.finished_at is not None and span.finished_at >= span.started_at for span in spans
    )

    assert [name for name, _, _ in calls.items] == [WRITE]
    # 承認後のツールも承認者ではなく Run の利用者として呼ぶ（MCP のサービストークンの sub。#233）。
    [(_, _, context)] = calls.items
    assert context.user_uuid == USER_UUID
    assert context.approval_id == approval.id
    # 承認済みの step を実行して完了にする（step を重複して作らない）。
    assert [step.status for step in run.steps] == ["completed"]
    assert run.steps[0].approval_id == approval.id
    assert "_builtin_sdk_state" not in run.metadata


@pytest.fixture
def restore_tool_policy() -> Iterator[None]:
    original = runtime_config_store.get_tool_policy()
    try:
        yield
    finally:
        runtime_config_store.patch_tool_policy(
            default_mode=original.default_mode,
            allow=sorted(original.allow),
            ask=sorted(original.ask),
            deny=sorted(original.deny),
        )


def test_approve_and_always_allow_runs_the_next_call_without_approval(
    monkeypatch: MonkeyPatch, calls: _Calls, restore_tool_policy: None
) -> None:
    """承認して「今後このツールは自動で実行」: ツール権限に allow が入り、次の呼び出しは承認待ちに
    ならない（#1832）。監査（Run のイベント）に残す。"""
    from security_support import client

    del restore_tool_policy
    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda _run: None)
    runtime_config_store.patch_tool_policy(ask=[WRITE])
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録 1"}, call_id="call-a1")],
        [function_call(WRITE, {"query": "登録 2"}, call_id="call-a2")],
        [assistant_message("2 件を登録しました。")],
    )
    run_id = _create_run("2 件を登録して")
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals

    decided = client.post(
        f"/api/approvals/{approval.id}/decision", json={"approved": True, "always_allow": True}
    )

    assert decided.status_code == 200, decided.text
    policy = runtime_config_store.get_tool_policy()
    assert WRITE in policy.allow and WRITE not in policy.ask
    anyio.run(builtin_runtime.resume_run, run_id)
    run = runtime_repository.get_run(run_id)
    # 同じ Run の 2 回目の呼び出しは承認待ちにならずに実行する。
    assert run.status == RunStatus.COMPLETED
    assert len(run.approvals) == 1
    assert [args["query"] for name, args, _ in calls.items if name == WRITE] == ["登録 1", "登録 2"]
    decided_event = next(
        event
        for event in run.events
        if event.type == "approval.decided" and event.payload.get("approval_id") == approval.id
    )
    assert decided_event.payload["always_allow"] is True
    assert "今後このツールは自動で実行" in decided_event.message


def test_always_allow_requires_agent_admin(
    monkeypatch: MonkeyPatch, calls: _Calls, restore_tool_policy: None
) -> None:
    """承認だけができる利用者は「今後は自動で実行」を選べない（403。承認もツール権限も変えない）。"""
    from security_support import client, enable_production_auth, login

    from app.security.permissions import APPROVALS_DECIDE, MENU_APPROVALS, RUNS_VIEW
    from app.security.service import set_security_service

    del calls, restore_tool_policy
    _script(monkeypatch, [function_call(WRITE, {"query": "登録"}, call_id="call-b1")])
    auth = enable_production_auth(monkeypatch)
    try:
        approver = auth.user_with_permissions(
            "approver-1832", [MENU_APPROVALS, RUNS_VIEW, APPROVALS_DECIDE], agent_ids=[AGENT_ID]
        )
        # 承認は自分の Run の承認だけ（#1889）なので、承認する利用者の Run にする。
        run_id = runtime_repository.create_builtin_run(
            RunCreateRequest(goal="登録して", agent_id=AGENT_ID),
            created_by_user_uuid=approver.user_uuid,
        ).id
        anyio.run(builtin_runtime.execute_run, run_id)
        [approval] = runtime_repository.get_run(run_id).approvals
        refused = client.post(
            f"/api/approvals/{approval.id}/decision",
            json={"approved": True, "always_allow": True},
            headers=login("approver-1832"),
        )
    finally:
        set_security_service(None)

    assert refused.status_code == 403
    assert WRITE not in runtime_config_store.get_tool_policy().allow
    assert runtime_repository.get_run(run_id).approvals[0].status == "pending"


def test_rejected_tool_is_not_executed(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-r")],
        [assistant_message("承認されなかったため登録していません。")],
    )
    run_id = _create_run("登録して")
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals

    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=False))
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert calls.items == []
    assert [step.status for step in run.steps] == ["cancelled"]


def test_mcp_run_does_not_wait_for_approval(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    """外部の MCP クライアントの Run も無人の実行と同じく承認の要るツールを呼ばない（#1874）。"""
    model = _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-mcp")],
        [assistant_message("確かめられなかった点: 登録はできませんでした。")],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="登録して", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
        extra_metadata={"source": "mcp", "api_key_id": "0123456789abcdef"},
    )

    anyio.run(builtin_runtime.execute_run, run.id)

    finished = runtime_repository.get_run(run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.approvals == []
    assert calls.items == []
    [skipped] = finished.steps
    assert skipped.tool_result is not None
    assert skipped.tool_result.error_code == MCP_APPROVAL_SKIPPED_CODE
    assert any(
        event.payload.get("error_code") == MCP_APPROVAL_SKIPPED_CODE
        and "外部の MCP クライアント" in event.message
        for event in finished.events
    )
    assert mcp_approval_message(WRITE) in str(model.calls[1].input)


def test_chat_run_still_waits_for_approval_of_the_same_tool(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """無人でない Run（画面のチャット）は今どおり承認待ちにする（#1813）。"""
    _script(monkeypatch, [function_call(WRITE, {"query": "登録"}, call_id="call-chat")])
    run_id = _create_run("登録して")

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    assert calls.items == []
    assert not any(
        step.tool_result and step.tool_result.error_code == MCP_APPROVAL_SKIPPED_CODE
        for step in waiting.steps
    )


def _usage(requests: int, input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def test_usage_is_recorded_and_accumulates_across_approval(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """モデルの利用量を Run に残し、承認待ちから再開しても累計にする（#772）。"""
    del calls
    monkeypatch.setattr(
        builtin_runtime, "enterprise_ai_default_model_id", lambda _settings: "default-model"
    )
    _script(
        monkeypatch,
        ModelStep(
            output=[function_call(WRITE, {"query": "登録"}, call_id="call-u")],
            usage=_usage(1, 100, 20),
        ),
        ModelStep(output=[assistant_message("登録しました。")], usage=_usage(1, 150, 30)),
    )
    run_id = _create_run("登録して")

    anyio.run(builtin_runtime.execute_run, run_id)
    waiting = runtime_repository.get_run(run_id)
    assert waiting.usage is not None
    assert (waiting.usage.requests, waiting.usage.total_tokens) == (1, 120)
    # Agent がモデルを指定しないときは、その時点の既定のテキストモデルの名前を残す。
    assert waiting.usage.model == "default-model"

    [approval] = waiting.approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.usage is not None
    assert run.usage.model_dump() == {
        "model": "default-model",
        "requests": 2,
        "input_tokens": 250,
        "output_tokens": 50,
        "total_tokens": 300,
    }


def test_usage_is_recorded_when_the_run_fails_after_model_calls(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """ツール呼び出しの上限で止まった Run も、それまでの利用量を残す（#772）。"""
    del calls
    monkeypatch.setattr(builtin_runtime, "_max_turns", lambda: 2)
    _script(
        monkeypatch,
        *[
            ModelStep(
                output=[function_call(LOOKUP, {"query": "売上"}, call_id=f"call-{index}")],
                usage=_usage(1, 10, 5),
            )
            for index in range(2)
        ],
    )
    # Run は作成時の版を使う（#770）。モデルを変えた下書きで実行する。
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(model_id="explicit-model"))
    run_id = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="ずっと調べる", agent_id=AGENT_ID, draft=True),
        created_by_user_uuid=USER_UUID,
    ).id

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert run.events[-1].payload["error_code"] == "runtime.max_turns_exceeded"
    assert run.usage is not None
    assert (run.usage.model, run.usage.requests, run.usage.total_tokens) == (
        "explicit-model",
        2,
        30,
    )


def test_missing_model_settings_fail_the_run(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    del calls

    def missing(_model_id: str = "") -> ModelTarget:
        raise builtin_runtime.BuiltinRuntimeError("runtime.model_not_configured", "モデル未設定")

    monkeypatch.setattr(builtin_runtime, "resolve_model_target", missing)
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert run.events[-1].payload["error_code"] == "runtime.model_not_configured"


def test_unregistered_model_fails_without_switching_connections(monkeypatch: MonkeyPatch) -> None:
    """登録に無いモデル（登録を消したなど）は、プライマリ接続へ切り替えずに失敗する（#3240）。

    空のモデル（業務 Agent が既定を使う）は、既定のテキストモデルとその接続に決まる。
    """
    from pr_system_settings.model import EnterpriseAiConfiguredModel

    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "enterprise_ai_endpoint", "https://primary.example/v1")
    monkeypatch.setattr(settings, "enterprise_ai_api_key", "k-primary")
    monkeypatch.setattr(
        settings,
        "enterprise_ai_models",
        [EnterpriseAiConfiguredModel(model_id="registered-3240", display_name="R")],
    )
    monkeypatch.setattr(settings, "enterprise_ai_default_text_model", "registered-3240")
    monkeypatch.setattr(settings, "enterprise_ai_default_vision_model", "")

    with pytest.raises(builtin_runtime.BuiltinRuntimeError) as raised:
        builtin_runtime.resolve_model_target("removed-3240")
    assert raised.value.code == "runtime.model_not_registered"
    assert "removed-3240" in raised.value.message

    assert builtin_runtime.resolve_model_target("").model_id == "registered-3240"
    assert builtin_runtime.resolve_model_target("registered-3240").endpoint.startswith(
        "https://primary.example"
    )


def test_cancelled_run_is_not_started(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    model = _script(monkeypatch, [assistant_message("実行されない")])
    run_id = _create_run()
    runtime_repository.cancel_run(run_id)

    anyio.run(builtin_runtime.execute_run, run_id)

    assert runtime_repository.get_run(run_id).status == RunStatus.CANCELLED
    assert model.calls == ()
    assert calls.items == []


def _dispatch_until_settled(run_id: str) -> None:
    """runtime-dispatcher で、この Run が承認待ちか終了になるまで claim と実行を繰り返す。"""
    from app.features.agent.runtime_dispatcher import dispatch_once

    for _ in range(20):
        status = runtime_repository.get_run(run_id).status
        if status in {RunStatus.WAITING_APPROVAL, RunStatus.COMPLETED, RunStatus.FAILED}:
            return
        anyio.run(dispatch_once, "worker-754")
    raise AssertionError(f"run did not settle: {runtime_repository.get_run(run_id).status}")


def test_api_run_is_executed_and_resumed_by_dispatcher(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """API は Run を queued で作り、dispatcher（本番の別プロセス）が実行・承認後の再開をする。"""
    from security_support import client

    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", "dispatcher")
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-api")],
        [assistant_message("登録しました。")],
    )

    created = client.post("/api/runs", json={"goal": "登録して", "agent_id": AGENT_ID})
    assert created.status_code == 200, created.text
    run_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "queued"
    assert created.json()["data"]["runtime_id"] == BUILTIN_RUNTIME_ID

    _dispatch_until_settled(run_id)
    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    approval_id = waiting.approvals[0].id

    decided = client.post(f"/api/approvals/{approval_id}/decision", json={"approved": True})
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "queued"
    # 決定者はログイン中の利用者（local はローカル利用者）。
    assert decided.json()["data"]["approvals"][0]["decided_by"] == "local"

    _dispatch_until_settled(run_id)
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert [name for name, _, _ in calls.items] == [WRITE]


def test_runtime_status_reports_model_and_missing_settings(monkeypatch: MonkeyPatch) -> None:
    from security_support import client

    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda _model_id="": ModelTarget(
            model_id="xai.grok-4", endpoint="https://llm.example", project_id="", api_key="secret"
        ),
    )
    ready = client.get("/api/runtime/status").json()["data"]
    assert ready["ready"] is True
    assert ready["model_id"] == "xai.grok-4"
    assert ready["sdk"] == "openai-agents"
    assert "secret" not in str(ready)

    def missing(_model_id: str = "") -> ModelTarget:
        raise builtin_runtime.BuiltinRuntimeError("runtime.model_not_configured", "モデル未設定")

    monkeypatch.setattr(builtin_runtime, "resolve_model_target", missing)
    blocked = client.get("/api/runtime/status").json()["data"]
    assert blocked["ready"] is False
    assert blocked["error_code"] == "runtime.model_not_configured"


def test_tracing_is_disabled() -> None:
    """業務データを含む trace を外部（OpenAI）へ送らない。"""
    from agents.tracing import get_trace_provider

    provider = get_trace_provider()
    assert getattr(provider, "_disabled", True) is True


def test_model_api_omits_empty_tools_for_xai() -> None:
    """xAI のモデルは空の `tools` を 400 で拒否するため、送る前に外す（実環境で確認）。"""

    class _Responses:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {}

    class _Client:
        def __init__(self) -> None:
            self.responses = _Responses()
            self.base_url = "https://llm.example/openai/v1"

    client = _Client()
    model = builtin_runtime.ModelApiResponsesModel(model="xai.grok-4.3", openai_client=client)  # type: ignore[arg-type]
    wrapped: Any = model._get_client()  # noqa: SLF001 - SDK の内部の差し替えを確かめる
    assert wrapped.base_url == "https://llm.example/openai/v1"

    anyio.run(lambda: wrapped.responses.create(model="m", input="x", tools=[], tool_choice="auto"))
    anyio.run(lambda: wrapped.responses.create(model="m", input="x", tools=[{"type": "function"}]))

    assert client.responses.calls[0] == {"model": "m", "input": "x"}
    assert client.responses.calls[1]["tools"] == [{"type": "function"}]


def _bad_request(code: str, message: str) -> Any:
    # openai は httpx2 の Response を受け取る（httpx の Response は型が合わない）。
    import httpx2
    from openai import BadRequestError

    body = {"code": code, "message": message, "param": None, "type": "invalid_request_error"}
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://llm.example/responses"))
    return BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


class _RecordingResponses:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._failures = list(failures or [])

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._failures:
            raise self._failures.pop(0)
        return {}


class _RecordingClient:
    def __init__(self, responses: _RecordingResponses) -> None:
        self.responses = responses


def _model_api_client(responses: _RecordingResponses) -> Any:
    model = builtin_runtime.ModelApiResponsesModel(
        model="openai.gpt-oss-120b",
        openai_client=_RecordingClient(responses),  # type: ignore[arg-type]
    )
    return model._get_client()  # noqa: SLF001 - SDK の内部の差し替えを確かめる


def test_model_api_omits_reasoning_text_from_the_input() -> None:
    """gpt-oss は input の reasoning_text を 400 で拒否する（#1215。実環境で確認）。"""
    from openai.types.responses import ResponseReasoningItem

    responses = _RecordingResponses()
    wrapped = _model_api_client(responses)
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "規程を検索する"}],
    }
    model_reasoning = ResponseReasoningItem.model_validate({**reasoning, "id": "rs_2"})
    call = {"type": "function_call", "call_id": "c1", "name": "rag__rag_search", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "c1", "output": "{}"}
    items = [{"role": "user", "content": "質問"}, reasoning, model_reasoning, call, output]

    anyio.run(lambda: wrapped.responses.create(model="m", input=items))

    sent = responses.calls[0]["input"]
    assert sent[1] == {"id": "rs_1", "type": "reasoning", "summary": []}
    assert sent[2] == {"id": "rs_2", "type": "reasoning", "summary": []}
    assert sent[0] is items[0] and sent[3] is call and sent[4] is output
    # 渡された input は変えない（SDK の状態に影響させない）。
    assert reasoning["content"]

    anyio.run(lambda: wrapped.responses.create(model="m", input="x"))
    assert responses.calls[1]["input"] == "x"


def test_model_api_retries_model_output_invalid_only() -> None:
    """モデルの API が再試行を求める `model_output_invalid` だけ、上限まで同じ要求で再試行する。

    #1215。
    """
    invalid = _bad_request("model_output_invalid", "The selected model could not produce ...")
    responses = _RecordingResponses([invalid, invalid])
    wrapped = _model_api_client(responses)

    assert anyio.run(lambda: wrapped.responses.create(model="m", input="x")) == {}
    assert len(responses.calls) == 3

    exhausted = _RecordingResponses([invalid] * 3)
    with pytest.raises(Exception, match="model_output_invalid"):
        anyio.run(lambda: _model_api_client(exhausted).responses.create(model="m", input="x"))
    assert len(exhausted.calls) == 1 + builtin_runtime.MODEL_OUTPUT_INVALID_RETRIES

    other = _RecordingResponses([_bad_request("invalid_value", "reasoning_text is not supported")])
    with pytest.raises(Exception, match="invalid_value"):
        anyio.run(lambda: _model_api_client(other).responses.create(model="m", input="x"))
    assert len(other.calls) == 1


def test_model_failure_records_the_api_error_code(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    """型名（BadRequestError）だけでは原因が分からないため、API の code まで残す（#1215）。"""
    del calls
    error = _bad_request("invalid_value", "reasoning_text is not supported by model m.")

    class _FailingModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            raise error

    _script(monkeypatch)  # モデルの接続の設定（CI には model-settings.json が無い）
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: _FailingModel([]))
    run_id = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="規程を調べて", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    ).id

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    failed = run.events[-1]
    assert failed.payload["error_code"] == "runtime.model_failed"
    assert failed.message == "モデルの呼び出しに失敗しました（BadRequestError: invalid_value）。"


# ---------------------------------------------------------------------------
# MCP 接続のツール（RAG / NL2SQL。#757）
# ---------------------------------------------------------------------------

MCP_SKILL_ID = "test757-skill"
MCP_AGENT_ID = "test757-agent"


@pytest.fixture
def mcp_agent(monkeypatch: MonkeyPatch) -> Iterator[Any]:
    """RAG の rag_search と、NL2SQL のすべてのツールを使う Agent（fake の MCP サーバー）。"""
    mcp = fake_product_mcp(monkeypatch)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=MCP_SKILL_ID,
            name="MCP の Skill",
            instructions="RAG と NL2SQL で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
                SkillMcpRequirement(server_id="nl2sql"),
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=MCP_AGENT_ID, name="MCP の Agent", skill_ids=[MCP_SKILL_ID])
    )
    try:
        yield mcp
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == MCP_AGENT_ID
            ]:
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(MCP_AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(MCP_SKILL_ID)


def _create_mcp_run(goal: str) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=MCP_AGENT_ID), created_by_user_uuid=USER_UUID
    )
    return run.id


def test_mcp_tool_after_approval_uses_the_run_creator_token(
    monkeypatch: MonkeyPatch, mcp_agent: Any
) -> None:
    """承認後の再実行の MCP のトークンの `sub` は、承認者ではなく Run の利用者（#233・#3251）。"""
    approver_uuid = "99999999-3251-0000-0000-000000000009"
    _script(
        monkeypatch,
        [
            function_call(
                "nl2sql__nl2sql_cancel_requirement",
                {"requirement_session_id": "qs-1"},
                call_id="call-cancel",
            )
        ],
        [assistant_message("確認を取り消しました。")],
    )
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    run_id = _create_mcp_run("確認を取り消して")

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL, waiting.events[-1].message
    assert mcp_agent.calls_of("nl2sql_cancel_requirement") == []
    [approval] = waiting.approvals
    runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by=approver_uuid)
    )
    anyio.run(builtin_runtime.resume_run, run_id)

    # 承認したツールが呼び先に届いたこと（その後の回答の確かめはこのテストの対象外）。
    run = runtime_repository.get_run(run_id)
    [step] = [item for item in run.steps if item.approval_id == approval.id]
    assert step.status == "completed"
    [call] = mcp_agent.calls_of("nl2sql_cancel_requirement")
    assert call["claims"]["sub"] == USER_UUID
    assert call["claims"]["sub"] != approver_uuid
    assert call["claims"]["run_id"] == run_id


def test_mcp_connection_tools_are_given_to_the_model(
    monkeypatch: MonkeyPatch, mcp_agent: Any
) -> None:
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "契約の更新条件"}, call_id="call-rag")],
        [assistant_message("更新条件は契約書の第 5 条です。")],
    )
    run_id = _create_mcp_run("契約の更新条件を調べて")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # Skill が許可したツールだけを渡す（rag は rag_search だけ、nl2sql はすべて）。
    tools = {tool.name: tool for tool in model.calls[0].tools if isinstance(tool, FunctionTool)}
    assert "rag__rag_search" in tools
    assert "rag__rag_list_search_answer_profiles" not in tools
    assert {"nl2sql__nl2sql_query", "nl2sql__nl2sql_get_job"} <= set(tools)
    # readOnlyHint の無いツールは承認が必要、読み取り専用のツールは承認なし。nl2sql_query は
    # readOnlyHint=false でも標準の接続の既定で承認なし（#1832）。
    assert (
        _needs_approval(
            tools["nl2sql__nl2sql_cancel_requirement"], {"requirement_session_id": "qs-1"}
        )
        is True
    )
    assert _needs_approval(tools["nl2sql__nl2sql_query"], {"question": "売上"}) is False
    assert _needs_approval(tools["rag__rag_search"], {"query": "更新条件"}) is False
    # ツールは Run の利用者のサービストークン（aud = 接続）で呼ぶ。
    [call] = mcp_agent.calls_of("rag_search")
    assert call["claims"]["sub"] == USER_UUID
    assert call["claims"]["aud"] == "rag"
    assert call["claims"]["run_id"] == run_id
    # 利用者の元の質問は Control Plane が付ける（#2261）。
    assert call["arguments"] == {
        "query": "契約の更新条件",
        "original_question": "契約の更新条件を調べて",
    }
    # 最後の step は、Control Plane が RAG の根拠で回答を確かめる検証（既定 on。#1277）。
    [step, validation] = run.steps
    assert step.tool_call is not None and step.tool_call.name == "rag__rag_search"
    assert step.status == "completed"
    assert validation.tool_call is not None
    assert validation.tool_call.name == "rag__rag_validate_answer"
    # RAG の結果は根拠の成果物として残る。
    evidence = next(item for item in run.artifacts if item.kind == "rag_evidence")
    assert evidence.content["evidence"][0]["file_name"] == "契約書.pdf"


def test_partially_unavailable_required_mcp_stops_before_the_model(
    monkeypatch: MonkeyPatch, mcp_agent: Any
) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    runtime_config_store.upsert_mcp_server("nl2sql", base_url="")
    model = _script(monkeypatch, [assistant_message("RAG だけで回答しました。")])
    run_id = _create_mcp_run("売上を調べて")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert not model.calls
    assert run.events[-1].payload["error_code"] == "runtime.mcp_unavailable"
    warning = next(event for event in run.events if event.type == "runtime.event")
    assert "NL2SQL" in warning.message
    assert warning.payload == {"severity": "warning"}


def test_unreachable_mcp_service_is_a_tool_error_and_the_run_continues(
    monkeypatch: MonkeyPatch, mcp_agent: Any
) -> None:
    """RAG が止まっても Run は例外で落ちず、ツールの失敗をモデルへ返す（#854）。

    根拠のツールが 1 つも結果を返さないまま確定した回答は出さず、ツールの失敗の理由で Run を
    失敗にする（資料に根拠が無いとは読ませない。#2475）。
    """
    _script(
        monkeypatch,
        ModelStep(
            output=[
                function_call("rag__rag_search", {"query": "確認用の条件"}, call_id="call-rag")
            ],
            usage=_usage(1, 100, 20),
        ),
        ModelStep(
            output=[assistant_message("接続できないため確認できません。")], usage=_usage(1, 150, 30)
        ),
    )
    run_id = _create_mcp_run("契約の更新条件を調べて")
    # ツールの一覧の取得の後に RAG が止まった（tools/call の前の initialize から接続できない）。
    real_list = builtin_runtime.discover_mcp_tools

    def discover_then_stop(*args: Any, **kwargs: Any) -> Any:
        found = real_list(*args, **kwargs)
        mcp_agent.method_failures["initialize"] = ["connect"] * 20
        return found

    monkeypatch.setattr(builtin_runtime, "discover_mcp_tools", discover_then_stop)

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert run.events[-1].payload["error_code"] == "runtime.grounding_tool_failed"
    assert "RAG のサービスが起動しているか" in run.events[-1].message
    [step] = run.steps
    assert step.status == "failed"
    assert step.tool_result is not None
    assert step.tool_result.error_code == "mcp.unreachable"
    assert "RAG のサービスが起動しているか" in (step.tool_result.error or "")
    assert mcp_agent.calls_of("rag_search") == []
    _assert_failed_usage(run_id)


def _assert_failed_usage(run_id: str) -> None:
    run = runtime_repository.get_run(run_id)
    assert run.usage is not None
    assert (
        run.usage.requests,
        run.usage.input_tokens,
        run.usage.output_tokens,
        run.usage.total_tokens,
    ) == (2, 250, 50, 300)
    report = build_usage_report(
        [fact_from_run(run)],
        days=7,
        now=datetime.now(UTC),
        tz=ZoneInfo("UTC"),
        agent_names={},
        user_names=lambda ids: {},
    )
    assert (
        report.totals.runs,
        report.totals.runs_with_usage,
        report.totals.requests,
        report.totals.total_tokens,
    ) == (1, 1, 2, 300)
    assert report.by_model[0].total_tokens == 300


@pytest.mark.parametrize(
    "check", ["_raise_on_grounding_failure", "_require_hops", "_require_output"]
)
def test_usage_is_retained_when_postcheck_fails_after_resume(
    monkeypatch: MonkeyPatch,
    calls: _Calls,
    check: str,
) -> None:
    del calls
    _script(
        monkeypatch,
        ModelStep(
            output=[function_call(WRITE, {"query": "確認用"}, call_id="call-usage")],
            usage=_usage(1, 100, 20),
        ),
        ModelStep(output=[assistant_message("確認しました。")], usage=_usage(1, 150, 30)),
    )
    run_id = _create_run("確認用の質問")
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise builtin_runtime.BuiltinRuntimeError("runtime.grounding_tool_failed", "確認用の失敗")

    async def async_fail(*args: Any, **kwargs: Any) -> Any:
        return fail()

    monkeypatch.setattr(
        builtin_runtime, check, fail if check == "_raise_on_grounding_failure" else async_fail
    )
    anyio.run(builtin_runtime.resume_run, run_id)
    assert runtime_repository.get_run(run_id).status == RunStatus.FAILED
    _assert_failed_usage(run_id)


# ---------------------------------------------------------------------------
# 指示のツールの名前と、無いツールの呼び出し（#1303）
# ---------------------------------------------------------------------------

BUSINESS_AGENT_ID = "test1303-business-agent"
_BARE_RAG_TOOL = re.compile(
    r"(?<![A-Za-z0-9_-])rag_(search|retrieve_evidence|lookup_guides|read_source"
    r"|list_search_answer_profiles)(?![A-Za-z0-9_-])"
)


@pytest.fixture
def business_agent(monkeypatch: MonkeyPatch) -> Iterator[Any]:
    """組み込みのスキル business-rag-research を持つ業務 Agent（fake の RAG の MCP）。"""
    mcp = fake_product_mcp(monkeypatch)
    runtime_repository.create_agent(
        AgentProfile(
            id=BUSINESS_AGENT_ID,
            name="業務支援の Agent",
            instructions="rag_retrieve_evidence と rag_lookup_guides には profile を必ず渡す。",
            skill_ids=["business-rag-research"],
        )
    )
    try:
        yield mcp
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id == BUSINESS_AGENT_ID
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(BUSINESS_AGENT_ID)


def test_skill_instructions_name_the_tools_given_to_the_model(
    monkeypatch: MonkeyPatch, business_agent: Any
) -> None:
    """スキルの指示の素のツールの名前は、モデルに渡す `<接続>__<ツール>` に書き直す（#1303）。"""
    model = _script(monkeypatch, [assistant_message("資料に根拠が見つかりませんでした。")])
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="経費の精算の期限は？", agent_id=BUSINESS_AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, run.id)

    tools = {tool.name for tool in model.calls[0].tools}
    assert {
        "rag__rag_retrieve_evidence",
        "rag__rag_lookup_guides",
        "rag__rag_read_source",
        "rag__rag_list_search_answer_profiles",
    } <= tools
    instructions = str(model.calls[0].system_instructions)
    # Skill の指示も業務の指示も、モデルが呼べる名前で書いてある（素の名前は残らない）。
    # Markdown の `code` の中の素の名前も書き直す（#1795）。
    assert "`rag__rag_retrieve_evidence` で集め" in instructions
    assert "rag__rag_retrieve_evidence と rag__rag_lookup_guides には profile" in instructions
    assert "rag__rag_lookup_guides" in instructions
    assert _BARE_RAG_TOOL.search(instructions) is None, instructions
    # 定義の指示は素の名前のまま（Run のときだけ書き直す）。
    skill = skill_registry.get("business-rag-research")
    assert skill is not None and "`rag_retrieve_evidence` で集め" in skill.instructions


def test_bare_tool_name_call_does_not_fail_the_run(
    monkeypatch: MonkeyPatch, business_agent: Any
) -> None:
    """モデルが素の名前でツールを呼んでも Run は落ちず、呼ぶ名前を返して呼び直させる（#1303）。"""
    model = _script(
        monkeypatch,
        [function_call("rag_retrieve_evidence", {"query": "精算の期限"}, call_id="call-bare")],
        [
            function_call(
                "rag__rag_retrieve_evidence", {"query": "精算の期限"}, call_id="call-prefixed"
            )
        ],
        [assistant_message("精算の期限は翌月 10 日です。")],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="経費の精算の期限は？", agent_id=BUSINESS_AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, run.id)

    finished = runtime_repository.get_run(run.id)
    assert finished.status == RunStatus.COMPLETED, finished.events[-1].message
    # 素の名前の呼び出しは実行せず、モデルへ呼ぶ名前を返した。
    hint = str(model.calls[1].input)
    assert "ツール「rag_retrieve_evidence」はありません" in hint
    assert "呼ぶときの名前は「rag__rag_retrieve_evidence」" in hint
    # 呼び先へ届いたのは、モデルが呼び直した 1 回だけ（Control Plane は読み替えて実行しない）。
    assert len(business_agent.calls_of("rag_retrieve_evidence")) == 1
    called = [step.tool_call.name for step in finished.steps if step.tool_call is not None]
    assert called[0] == "rag__rag_retrieve_evidence"
    assert "rag_retrieve_evidence" not in called


def test_ambiguous_bare_tool_name_is_not_rewritten_or_resolved() -> None:
    """同じ素の名前が複数の接続にあるときは、指示を書き換えず、案内で候補を並べる（#1303）。"""
    skill_id = "test1303-ambiguous"
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=skill_id,
            name="2 つの RAG",
            instructions="rag_search で調べ、rag_read_source で本文を読む。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search", "rag_read_source"]),
                SkillMcpRequirement(server_id="rag_hr", tool_names=["rag_search"]),
            ],
        )
    )
    exposed = ["rag__rag_search", "rag__rag_read_source", "rag_hr__rag_search"]
    try:
        assert builtin_runtime.instruction_tool_names([skill_id], exposed) == {
            "rag_read_source": "rag__rag_read_source"
        }
        instructions = builtin_runtime.compose_instructions("", [skill_id], exposed)
        assert "rag_search で調べ、rag__rag_read_source で本文を読む。" in instructions
    finally:
        skill_registry.remove(skill_id)

    message = builtin_runtime.tool_not_found_message("rag_search", exposed)
    assert "呼ぶときの名前は" not in message
    assert "rag__rag_search、rag_hr__rag_search" in message
    # 1 つに決まるものは呼ぶ名前を、決まらない名前は使えるツールを返す。
    assert "「rag__rag_read_source」" in builtin_runtime.tool_not_found_message(
        "rag_read_source", exposed
    )
    unknown = builtin_runtime.tool_not_found_message("web_search", exposed)
    assert "使えるツールの名前: rag__rag_read_source、rag__rag_search" in unknown


def test_render_tool_names_replaces_whole_names_only() -> None:
    names = {"rag_search": "rag__rag_search"}
    text = "rag_search と `rag_search`、rag_searchで。rag_search_x・rag__rag_search・my-rag_search"
    assert builtin_runtime.render_tool_names(text, names) == (
        "rag__rag_search と `rag__rag_search`、rag__rag_searchで。"
        "rag_search_x・rag__rag_search・my-rag_search"
    )


# ---------------------------------------------------------------------------
# 会話（スレッド。#768）
# ---------------------------------------------------------------------------


def test_thread_passes_previous_turns_to_the_model(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    model = _script(
        monkeypatch,
        [assistant_message("今月の売上は 120 万円です。")],
        [assistant_message("先月は 100 万円なので 20 万円増えました。")],
    )
    first_id = _create_run("今月の売上は？")
    anyio.run(builtin_runtime.execute_run, first_id)
    first = runtime_repository.get_run(first_id)
    assert first.thread_id is not None and first.thread_id.startswith("thread_")

    second = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="先月と比べると？", agent_id=AGENT_ID, thread_id=first.thread_id),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, second.id)

    assert runtime_repository.get_run(second.id).status == RunStatus.COMPLETED
    # 1 回目は質問だけ、2 回目は前の質問と回答を未信頼のデータとして付けてモデルへ渡す（#1818）。
    assert model.calls[0].input == [{"role": "user", "content": "今月の売上は？"}]
    assert model.calls[1].input == [
        {
            "role": "user",
            "content": (
                f"{CHAT_HISTORY_UNTRUSTED_NOTE}\n"
                '<conversation_history untrusted="true">\n<turn>\n'
                "<question>今月の売上は？</question>\n"
                "<answer>今月の売上は 120 万円です。</answer>\n"
                "</turn>\n</conversation_history>"
            ),
        },
        {"role": "user", "content": "先月と比べると？"},
    ]

    thread = runtime_repository.get_thread(first.thread_id, user_uuid=USER_UUID)
    assert [run.id for run in thread.runs] == [first_id, second.id]
    [summary] = [
        item
        for item in runtime_repository.list_threads(user_uuid=USER_UUID, agent_id=AGENT_ID)
        if item.thread_id == first.thread_id
    ]
    assert (summary.title, summary.run_count, summary.last_status) == (
        "今月の売上は？",
        2,
        RunStatus.COMPLETED,
    )


def test_conversation_input_keeps_recent_turns_within_shared_budget(
    monkeypatch: MonkeyPatch,
) -> None:
    """前の往復は共通の予算で新しい順に詰め、古い往復を落とし、0 なら付けない（#1818）。"""
    from types import SimpleNamespace

    from app.features.agent import runtime as runtime_module

    turns = [
        ChatHistoryTurn.of(("question", f"質問{index}"), ("answer", "回" * 900))
        for index in range(1, 6)
    ]
    monkeypatch.setattr(
        runtime_module.runtime_repository, "thread_history", lambda run_id: list(turns)
    )
    budget = SimpleNamespace(
        chat_history_max_turns=8, chat_history_max_chars_per_turn=500, chat_history_max_chars=1000
    )
    monkeypatch.setattr(builtin_runtime, "get_settings", lambda: budget)
    items = builtin_runtime.conversation_input("run_x", "次の質問")
    assert isinstance(items, list)
    history = items[0]["content"]
    # 1 往復を 500 文字に切り詰め、合計 1,000 文字に入る新しい 2 往復だけを渡す。
    assert "質問5" in history and "質問4" in history and "質問3" not in history
    assert "…（以下省略）" in history
    assert items[1] == {"role": "user", "content": "次の質問"}

    budget.chat_history_max_turns = 0
    assert builtin_runtime.conversation_input("run_x", "次の質問") == "次の質問"


def test_run_history_turn_includes_tool_names_not_results() -> None:
    """前の Run の往復は、使ったツールの名前だけを持ち、結果を持たない（#1818）。"""
    from app.features.agent.runtime import RunState, RunStep, run_history_turn
    from app.features.agent.tools import ToolCall

    run = RunState(
        id="run_x",
        goal="売上は？",
        agent_id=AGENT_ID,
        status=RunStatus.COMPLETED,
        steps=[
            RunStep(run_id="run_x", tool_call=ToolCall(name="rag__rag_search", arguments={})),
            RunStep(run_id="run_x", tool_call=ToolCall(name="nl2sql__nl2sql_query", arguments={})),
            RunStep(run_id="run_x", tool_call=ToolCall(name="rag__rag_search", arguments={})),
        ],
    )
    turn = run_history_turn(run, "120 万円です。")
    assert turn.parts == (
        ("question", "売上は？"),
        ("answer", "120 万円です。"),
        ("tools", "rag__rag_search、nl2sql__nl2sql_query"),
    )


def test_thread_belongs_to_its_user_and_agent(calls: _Calls) -> None:
    first_id = _create_run("自分の会話")
    thread_id = runtime_repository.get_run(first_id).thread_id
    assert thread_id is not None

    # 別の利用者は続けられず、一覧にも出ない。別の Agent でも続けられない。
    with pytest.raises(ThreadNotFoundError):
        runtime_repository.create_builtin_run(
            RunCreateRequest(goal="横取り", agent_id=AGENT_ID, thread_id=thread_id),
            created_by_user_uuid="99999999-0000-0000-0000-000000000000",
        )
    with pytest.raises(ThreadNotFoundError):
        runtime_repository.create_builtin_run(
            RunCreateRequest(goal="別の Agent", agent_id="default", thread_id=thread_id),
            created_by_user_uuid=USER_UUID,
        )
    with pytest.raises(ThreadNotFoundError):
        runtime_repository.get_thread(thread_id, user_uuid="99999999-0000-0000-0000-000000000000")
    others = runtime_repository.list_threads(user_uuid="99999999-0000-0000-0000-000000000000")
    assert thread_id not in {item.thread_id for item in others}


def test_thread_rename_and_delete_keep_runs(calls: _Calls) -> None:
    """会話の名前の変更・削除は作った利用者だけ。削除しても Run は残す（#1817）。"""
    del calls
    other = "99999999-0000-0000-0000-000000000000"
    run_id = _create_run("最初の質問")
    thread_id = runtime_repository.get_run(run_id).thread_id
    assert thread_id is not None

    summary = runtime_repository.rename_thread(thread_id, user_uuid=USER_UUID, title="売上の確認")
    assert summary.title == "売上の確認"
    [listed] = [
        item
        for item in runtime_repository.list_threads(user_uuid=USER_UUID)
        if item.thread_id == thread_id
    ]
    assert listed.title == "売上の確認"
    with pytest.raises(ThreadNotFoundError):
        runtime_repository.rename_thread(thread_id, user_uuid=other, title="横取り")

    # 回答の作成中の会話は消さない。
    with pytest.raises(ThreadBusyError):
        runtime_repository.delete_thread(thread_id, user_uuid=USER_UUID)
    runtime_repository.cancel_run(run_id)
    with pytest.raises(ThreadNotFoundError):
        runtime_repository.delete_thread(thread_id, user_uuid=other)
    runtime_repository.delete_thread(thread_id, user_uuid=USER_UUID)

    with pytest.raises(ThreadNotFoundError):
        runtime_repository.get_thread(thread_id, user_uuid=USER_UUID)
    assert thread_id not in {
        item.thread_id for item in runtime_repository.list_threads(user_uuid=USER_UUID)
    }
    with pytest.raises(ThreadNotFoundError):
        runtime_repository.create_builtin_run(
            RunCreateRequest(goal="続き", agent_id=AGENT_ID, thread_id=thread_id),
            created_by_user_uuid=USER_UUID,
        )
    # Run は監査・フィードバック・評価のために残る（会話からの参照だけを外す）。
    kept = runtime_repository.get_run(run_id)
    assert kept.thread_id is None
    assert kept.metadata[DELETED_THREAD_METADATA_KEY] == thread_id


def test_thread_api_renames_and_deletes(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    """`PATCH` / `DELETE /api/threads/{thread_id}`（#1817）。空の名前は 422、作成中は 409。"""
    del calls
    from security_support import client

    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda run: None)
    created = client.post("/api/runs", json={"goal": "最初の質問", "agent_id": AGENT_ID})
    assert created.status_code == 200, created.text
    run_id = created.json()["data"]["id"]
    thread_id = created.json()["data"]["thread_id"]

    renamed = client.patch(f"/api/threads/{thread_id}", json={"title": "  売上の確認  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["title"] == "売上の確認"
    assert client.patch(f"/api/threads/{thread_id}", json={"title": "   "}).status_code == 422
    assert client.patch(f"/api/threads/{thread_id}", json={"title": "あ" * 81}).status_code == 422
    missing = "thread_" + "0" * 32
    assert client.patch(f"/api/threads/{missing}", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/threads/{missing}").status_code == 404

    busy = client.delete(f"/api/threads/{thread_id}")
    assert busy.status_code == 409
    assert busy.json()["error_messages"] == [
        "回答の作成中・承認待ちの会話は削除できません。"
        "停止するか、回答が終わってから削除してください。"
    ]
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    deleted = client.delete(f"/api/threads/{thread_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/threads/{thread_id}").status_code == 404
    listed = client.get(f"/api/threads?agent_id={AGENT_ID}").json()["data"]["items"]
    assert thread_id not in {item["thread_id"] for item in listed}


def test_thread_api_writes_storage_off_the_event_loop(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """会話の名前の変更・削除は、保存先への書き込みを threadpool で待つ（#1935）。

    Oracle の checkpoint の書き込みは数秒〜十数秒かかることがあり、event loop の上で待つと
    ほかの要求（作成中の回答の stream など）も止まる。
    """
    del calls
    from security_support import client

    repository = get_runtime_repository()
    where: list[tuple[str, bool]] = []

    def record(name: str) -> None:
        original = getattr(repository, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            where.append((name, on_loop))
            return original(*args, **kwargs)

        monkeypatch.setattr(repository, name, wrapper)

    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda run: None)
    created = client.post("/api/runs", json={"goal": "最初の質問", "agent_id": AGENT_ID})
    assert created.status_code == 200, created.text
    run_id = created.json()["data"]["id"]
    thread_id = created.json()["data"]["thread_id"]
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    for name in ("get_thread", "rename_thread", "delete_thread"):
        record(name)

    renamed = client.patch(f"/api/threads/{thread_id}", json={"title": "売上の確認"})
    assert renamed.status_code == 200, renamed.text
    assert client.delete(f"/api/threads/{thread_id}").status_code == 200

    assert {name for name, _ in where} == {"get_thread", "rename_thread", "delete_thread"}
    assert [name for name, on_loop in where if on_loop] == []


def test_thread_api_pages_conversations(monkeypatch: MonkeyPatch, calls: _Calls) -> None:
    """会話の履歴は新しい順に limit / offset でページングし、全件数を返す（#1265）。"""
    from security_support import client

    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda run: None)
    before = client.get(f"/api/threads?agent_id={AGENT_ID}").json()["data"]["total"]
    created = []
    for index in range(27):
        response = client.post("/api/runs", json={"goal": f"質問 {index}", "agent_id": AGENT_ID})
        assert response.status_code == 200, response.text
        created.append(response.json()["data"]["thread_id"])

    first = client.get(f"/api/threads?agent_id={AGENT_ID}").json()["data"]
    assert (first["total"], first["limit"], first["offset"]) == (before + 27, 25, 0)
    assert [item["thread_id"] for item in first["items"]] == created[::-1][:25]
    assert first["has_next"] is True
    second = client.get(f"/api/threads?agent_id={AGENT_ID}&limit=25&offset=25").json()["data"]
    assert [item["thread_id"] for item in second["items"]][:2] == created[1::-1]
    assert second["total"] == before + 27
    assert client.get("/api/threads?limit=0").status_code == 422
    assert client.get("/api/threads?offset=-1").status_code == 422


def test_thread_list_only_within_history_days(monkeypatch: MonkeyPatch) -> None:
    """会話の履歴は最後の更新が直近 N 日の会話だけ（件数も同じ条件。#2340）。0 は絞らない。"""
    from security_support import client

    from app.features.agent.runtime import RunStatus, ThreadSummary, runtime_repository
    from app.settings import get_settings

    now = datetime.now(UTC)

    def thread(thread_id: str, updated_at: datetime) -> ThreadSummary:
        return ThreadSummary(
            thread_id=thread_id,
            agent_id=AGENT_ID,
            title=thread_id,
            run_count=1,
            last_status=RunStatus.COMPLETED,
            created_at=updated_at,
            updated_at=updated_at,
        )

    threads = [thread("thread-recent", now), thread("thread-old", now - timedelta(days=91))]
    monkeypatch.setattr(runtime_repository, "list_threads", lambda **_kwargs: list(threads))
    monkeypatch.setattr(get_settings(), "chat_history_days", 90)

    listed = client.get(f"/api/threads?agent_id={AGENT_ID}").json()["data"]
    assert [item["thread_id"] for item in listed["items"]] == ["thread-recent"]
    assert listed["total"] == 1
    assert listed["history_days"] == 90

    monkeypatch.setattr(get_settings(), "chat_history_days", 0)
    listed = client.get(f"/api/threads?agent_id={AGENT_ID}").json()["data"]
    assert listed["total"] == 2
    assert listed["history_days"] == 0


def test_thread_api_lists_and_continues_conversation(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    from pr_system_settings.auth.domain import LOCAL_DEBUG_USER_UUID
    from security_support import client

    scheduled: list[str] = []
    monkeypatch.setattr(
        "app.features.agent.router._schedule_builtin_run", lambda run: scheduled.append(run.id)
    )

    created = client.post("/api/runs", json={"goal": "最初の質問", "agent_id": AGENT_ID})
    assert created.status_code == 200, created.text
    thread_id = created.json()["data"]["thread_id"]
    continued = client.post(
        "/api/runs",
        json={"goal": "続きの質問", "agent_id": AGENT_ID, "thread_id": thread_id},
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["data"]["thread_id"] == thread_id
    assert continued.json()["data"]["created_by_user_uuid"] == LOCAL_DEBUG_USER_UUID

    listed = client.get(f"/api/threads?agent_id={AGENT_ID}")
    assert listed.status_code == 200
    [thread] = [item for item in listed.json()["data"]["items"] if item["thread_id"] == thread_id]
    assert thread["title"] == "最初の質問"
    assert thread["run_count"] == 2

    assert listed.json()["data"]["total"] >= 1
    assert listed.json()["data"]["limit"] == 25
    assert listed.json()["data"]["offset"] == 0

    detail = client.get(f"/api/threads/{thread_id}")
    assert detail.status_code == 200
    assert [run["goal"] for run in detail.json()["data"]["runs"]] == ["最初の質問", "続きの質問"]

    missing = "thread_" + "0" * 32
    assert client.get(f"/api/threads/{missing}").status_code == 404
    unknown = client.post(
        "/api/runs", json={"goal": "x", "agent_id": AGENT_ID, "thread_id": missing}
    )
    assert unknown.status_code == 404
    assert unknown.json()["error_messages"] == ["会話が見つかりません。"]
    # チャットの送信・停止の失敗にそのまま出る理由は日本語（英語・内部の ID を出さない）。
    no_agent = client.post("/api/runs", json={"goal": "x", "agent_id": "agent-missing"})
    assert no_agent.status_code == 404
    assert no_agent.json()["error_messages"] == ["業務 Agent が見つかりません。"]
    no_run = client.post("/api/runs/run-missing/cancel")
    assert no_run.status_code == 404
    assert no_run.json()["error_messages"] == ["実行が見つかりません。"]
    assert (
        client.post(
            "/api/runs", json={"goal": "x", "agent_id": AGENT_ID, "thread_id": "bad"}
        ).status_code
        == 422
    )
    assert len(scheduled) == 2


@pytest.mark.parametrize("permitted", [True, False])
def test_skill_reads_only_its_own_reference(
    monkeypatch: MonkeyPatch, calls: _Calls, permitted: bool
) -> None:
    """スキルの参照文書（references/）は索引だけを指示に入れ、割り当てた分だけ読ませる（#1803）。"""
    from app.features.agent.skill_references import REFERENCE_TOOL_NAME
    from app.features.agent.skills import SkillReference

    del calls
    path = "references/guide.md"
    skill = skill_registry.get(SKILL_ID)
    assert skill is not None
    skill_registry.upsert_custom(
        skill.model_copy(
            update={
                "references": [
                    SkillReference(path=path, characters=11, content="対象の根拠を確認する。")
                ]
            }
        )
    )
    model = _script(
        monkeypatch,
        [
            function_call(
                REFERENCE_TOOL_NAME,
                {
                    "skill_id": SKILL_ID,
                    "path": path if permitted else "references/unassigned.md",
                    "offset": 0,
                },
                call_id="read-reference",
            )
        ],
        [assistant_message("確認しました。")],
    )
    run_id = _create_run()
    anyio.run(builtin_runtime.execute_run, run_id)
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert path in str(model.calls[0].system_instructions)
    assert "対象の根拠を確認する。" not in str(model.calls[0].system_instructions)
    if permitted:
        assert "対象の根拠を確認する。" in str(model.calls[1].input)
        assert run.steps[0].status == "completed"
    else:
        assert "割り当てられていない" in str(model.calls[1].input)
        assert run.steps[0].status == "failed"


@pytest.mark.parametrize("same_actor", [False, True])
def test_losing_always_allow_keeps_policy(
    monkeypatch: MonkeyPatch, calls: _Calls, restore_tool_policy: None, same_actor: bool
) -> None:
    from security_support import client

    from app.features.agent import control_plane_store, router

    monkeypatch.setattr(router, "_schedule_builtin_run", lambda _run: None)
    runtime_config_store.patch_tool_policy(ask=[WRITE], allow=[])
    _script(monkeypatch, [function_call(WRITE, {"query": "合成の操作"}, call_id="race")])
    run_id = _create_run("合成の操作")
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    repository = get_runtime_repository()
    original = repository.decide_approval
    saves: list[object] = []
    monkeypatch.setattr(control_plane_store, "save_tool_policy", saves.append)

    def competing(approval_id: str, request: ApprovalDecisionRequest, **kwargs: Any) -> Any:
        original(
            approval_id,
            ApprovalDecisionRequest(
                approved=True, decided_by=request.decided_by if same_actor else "other-actor"
            ),
        )
        return original(approval_id, request, **kwargs)

    monkeypatch.setattr(repository, "decide_approval", competing)
    response = client.post(
        f"/api/approvals/{approval.id}/decision", json={"approved": True, "always_allow": True}
    )
    assert response.status_code == 200
    assert not saves
    assert WRITE not in runtime_config_store.get_tool_policy().allow
    assert WRITE in runtime_config_store.get_tool_policy().ask
    events = [e for e in runtime_repository.get_run(run_id).events if e.type == "approval.decided"]
    assert len(events) == 1 and not events[0].payload["always_allow"]


@pytest.mark.parametrize("mode", ["in_process", "dispatcher"])
def test_always_allow_failure_is_retryable(
    monkeypatch: MonkeyPatch, calls: _Calls, restore_tool_policy: None, mode: str
) -> None:
    from security_support import client

    from app.features.agent import control_plane_store, router
    from app.features.agent.control_plane_store import ControlPlaneStoreError

    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", mode)
    scheduled: list[str] = []
    monkeypatch.setattr(router, "_schedule_builtin_run", lambda run: scheduled.append(run.id))
    runtime_config_store.patch_tool_policy(ask=[WRITE], allow=[])
    _script(monkeypatch, [function_call(WRITE, {"query": "合成の操作"}, call_id="failure")])
    run_id = _create_run("合成の操作")
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    before = runtime_config_store.get_tool_policy()

    def fail(_policy: object) -> None:
        raise ControlPlaneStoreError("合成の保存失敗")

    monkeypatch.setattr(control_plane_store, "save_tool_policy", fail)
    url = f"/api/approvals/{approval.id}/decision"
    assert client.post(url, json={"approved": True, "always_allow": True}).status_code == 503
    run = runtime_repository.get_run(run_id)
    assert run.approvals[0].status == "pending"
    assert run.status == RunStatus.WAITING_APPROVAL
    assert runtime_config_store.get_tool_policy() == before
    assert not scheduled
    assert not any(e.type == "approval.decided" for e in run.events)
    saves: list[object] = []
    monkeypatch.setattr(control_plane_store, "save_tool_policy", saves.append)
    assert client.post(url, json={"approved": True, "always_allow": True}).status_code == 200
    assert len(saves) == 1
    assert runtime_repository.get_run(run_id).status == RunStatus.QUEUED
    assert WRITE in runtime_config_store.get_tool_policy().allow
    assert client.post(url, json={"approved": True, "always_allow": True}).status_code == 200
    assert len(saves) == 1


def test_required_mcp_discovery_failure_stops_before_the_model(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    model = _script(monkeypatch, [assistant_message('{"command":"lookup","parameters":{}}')])
    monkeypatch.setattr(
        builtin_runtime,
        "discover_mcp_tools",
        lambda *args, **kwargs: (
            [],
            ["MCP 接続 A のツールを取得できません。接続を確認してください。 "],
        ),
    )
    run_id = _create_run("資料の値を確認してください。")
    anyio.run(builtin_runtime.execute_run, run_id)
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.FAILED
    assert not model.calls
    assert not calls.items
    assert "MCP" in run.events[-1].message
    assert fact_from_run(run).tool_failures == 1
    assert not any(item.kind == "answer" for item in run.artifacts)


def test_rejected_approval_does_not_ask_for_execution_again(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    model = _script(
        monkeypatch,
        [function_call(WRITE, {"query": "処理 A"}, call_id="call-reject")],
        [assistant_message("同じ処理を実行してよろしいですか？")],
    )
    run_id = _create_run("処理 A を実行してください。")
    anyio.run(builtin_runtime.execute_run, run_id)
    [approval] = runtime_repository.get_run(run_id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=False))
    anyio.run(builtin_runtime.resume_run, run_id)
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert not calls.items
    assert len(model.calls) == 1
    answer = next(item.content["text"] for item in run.artifacts if item.kind == "answer")
    assert "却下" in answer and "実行していません" in answer
    assert "よろしい" not in answer


def test_mixed_approval_executes_only_approved_calls_without_another_model_turn(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    model = _script(
        monkeypatch,
        [
            function_call(WRITE, {"query": "処理 A"}, call_id="call-a"),
            function_call(WRITE, {"query": "処理 B"}, call_id="call-b"),
        ],
        [assistant_message("却下した処理をもう一度実行しますか？")],
    )
    run_id = _create_run("処理 A と処理 B を実行してください。")
    anyio.run(builtin_runtime.execute_run, run_id)
    approvals = runtime_repository.get_run(run_id).approvals
    assert len(approvals) == 2
    for approval in approvals:
        runtime_repository.decide_approval(
            approval.id,
            ApprovalDecisionRequest(approved=approval.tool_call.arguments["query"] == "処理 A"),
        )
    anyio.run(builtin_runtime.resume_run, run_id)
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert [args["query"] for _, args, _ in calls.items] == ["処理 A"]
    assert len(model.calls) == 1
    assert not run.confirmations


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_transition_counts_once_even_when_repeated(
    monkeypatch: MonkeyPatch, calls: _Calls, status: str
) -> None:
    from unittest.mock import Mock

    from app import observability

    del calls
    counter = Mock()
    monkeypatch.setattr(observability, "RUNS_TOTAL", counter)
    run_id = _create_run("合成の実行")
    assert runtime_repository.begin_builtin_run(run_id) is not None
    for _ in range(2):
        if status == "completed":
            runtime_repository.complete_builtin_run(run_id, "合成の回答")
        elif status == "failed":
            runtime_repository.fail_builtin_run(
                run_id, code="synthetic_failure", detail="合成の失敗"
            )
        else:
            runtime_repository.cancel_run(run_id)
    counter.labels.assert_called_once_with(status=status)
    counter.labels.return_value.inc.assert_called_once_with()
