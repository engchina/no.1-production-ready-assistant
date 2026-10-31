"""チャットの処理の経過（3 製品共通の契約。#1359）を、Run の trace（span の木。#3226）から
作るテスト。span は共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）に書く。

組み込み Runtime は SDK の `ScriptedModel` で台本にし（OCI へは接続しない）、モデルの呼び出しの
span は OCI の client と同じ形（`gen_ai.chat` に工程の種類を付ける）で作る。記録の位置（ツール
なし・ツール・実行中・並列・承認・却下・承認の 2 回目・引数の誤り・失敗・停止・実行中の停止）、
Run に残すもの（trace id・工程の詳細）の保存先（file / Oracle の checkpoint・normalized）、表への
書き込み（Oracle の書き手。fake の DB）、読み出しの API（`GET /api/runs/{run_id}/trace`）と権限を
確かめる。テストの保存先は memory なので、span は process の中の表（`trace_spans.memory`）にある。
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pr_backend_core.chat_progress import (
    ChatProgressStepEvent,
    ChatProgressTerminalEvent,
    chat_progress_details,
    fold_chat_progress_events,
)
from pr_backend_core.observability.trace_store import TraceSpanOwner, TraceSpanWriter
from pr_backend_core.observability.tracing import (
    SpanDisplay,
    SpanUpdate,
    attach_trace_context,
    observed_span,
    operation_trace,
)
from pr_system_settings.auth.domain import LOCAL_DEBUG_USER_UUID
from pytest import MonkeyPatch
from security_support import (
    ProductionAuth,
    client,
    enable_production_auth,
    login,
)
from test_builtin_runtime import AGENT_ID, LOOKUP, WRITE, _create_run
from test_builtin_runtime import _script as _script_model
from test_builtin_runtime import calls as _calls_fixture  # noqa: F401 - pytestmark の fixture
from test_health import _FakeOracleConnection, _FakeOracleStore
from trace_support import run_progress_events

from app.features.agent import builtin_runtime
from app.features.agent import tools as tools_module
from app.features.agent.chat_progress import RUN_STEP_PARAM, model_turn_kind, tool_display
from app.features.agent.runtime import (
    AgentRuntimeOracleCheckpointRepository,
    AgentRuntimeOracleNormalizedRepository,
    AgentRuntimeRepository,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunState,
    runtime_repository,
)
from app.features.agent.tools import (
    ToolCall,
    ToolInvocationContext,
    mcp_base_tool_name,
    tool_registry,
)
from app.features.agent.trace_spans import run_trace_owner, trace_spans
from app.security.service import set_security_service

# 組み込み Runtime のテスト用のツール・Skill・業務 Agent（test_builtin_runtime の fixture）。
pytestmark = pytest.mark.usefixtures("_calls_fixture")

CHAT = "gen_ai.chat"
TOOL = "gen_ai.execute_tool"
APPROVAL = "agent.approval_wait"

Row = tuple[str | None, str | None, str, dict[str, Any] | None]


class _TracedScriptedModel(ScriptedModel):
    """OCI の client と同じく、モデルの呼び出しを `gen_ai.chat` の span で測る台本のモデル。"""

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        with observed_span(
            CHAT,
            kind="client",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "oracle",
                "gen_ai.request.model": "test-model",
            },
        ) as span:
            response = await super().get_response(*args, **kwargs)
            builtin_runtime._set_turn_kind(span, response)  # noqa: SLF001 - OCI の client と同じ印
            return response


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = _TracedScriptedModel(list(steps))
    _script_model(monkeypatch)
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _rows(run: RunState) -> list[Row]:
    """組み立てた行（実行の全体の span を除く。span の名前・工程の種類・状態・params）。

    params の Run の step の id は値が毎回変わるので省く（`_run_step_of` で確かめる）。
    """
    rows: list[Row] = []
    for step in fold_chat_progress_events(run_progress_events(run)).step_list():
        span = step.span or {}
        if span.get("root") or span.get("name") == "agent.attachment_evidence":
            continue
        params = {key: value for key, value in (step.params or {}).items() if key != RUN_STEP_PARAM}
        kind = step.kind if step.kind != step.step_id else None
        rows.append((span.get("name"), kind, step.status, params or None))
    return rows


def _terminal(run: RunState) -> str | None:
    return fold_chat_progress_events(run_progress_events(run)).terminal


def _roots(run: RunState) -> list[Any]:
    return [
        step
        for step in fold_chat_progress_events(run_progress_events(run)).step_list()
        if (step.span or {}).get("root")
    ]


def _assert_contiguous(run: RunState) -> None:
    """番号は 1 から連続し、どのイベントも Run の id を対象にする。"""
    events = run_progress_events(run)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert {event.target_id for event in events} == {run.id}


def _approve(run_id: str, *, approved: bool = True) -> None:
    waiting = runtime_repository.get_run(run_id)
    for approval in waiting.approvals:
        if approval.status == "pending":
            runtime_repository.decide_approval(
                approval.id, ApprovalDecisionRequest(approved=approved, decided_by="approver1")
            )


# --------------------------------------------------------------------------------------------
# 工程の種類
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        ([{"type": "reasoning"}, {"type": "function_call"}], "plan"),
        ([{"type": "message"}], "respond"),
        ([{"type": "reasoning"}], None),
        (None, None),
    ],
    ids=["tool", "answer", "reasoning-only", "none"],
)
def test_model_turn_kind_follows_the_response_output(
    output: list[dict[str, str]] | None, kind: str | None
) -> None:
    assert model_turn_kind(output) == kind


# --------------------------------------------------------------------------------------------
# 記録の位置
# --------------------------------------------------------------------------------------------


def test_queued_run_has_no_progress_yet() -> None:
    """受け付けただけの Run には span が無い（実行の区間が始まってから記録する）。"""
    run = runtime_repository.get_run(_create_run())

    assert run.trace_store is True
    assert run.trace_id is None
    assert run_progress_events(run) == []
    assert _terminal(run) is None


def test_answer_without_tools_records_one_model_call(monkeypatch: MonkeyPatch) -> None:
    _script(monkeypatch, [assistant_message("こんにちは。")])
    run_id = _create_run("挨拶して")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run) == [(CHAT, "respond", "done", None)]
    assert _terminal(run) == "done"
    _assert_contiguous(run)
    # 実行の全体の span（root）も記録し（行にはしない）、モデルの呼び出しはその子。
    [root] = _roots(run)
    assert root.span is not None and root.span["name"] == "agent.execute"
    fold = fold_chat_progress_events(run_progress_events(run))
    [attachment] = [
        step
        for step in fold.step_list()
        if (step.span or {}).get("name") == "agent.attachment_evidence"
    ]
    assert attachment.status == "skipped"
    [chat] = [step for step in fold.step_list() if (step.span or {}).get("name") == CHAT]
    assert chat.span is not None
    assert chat.span["parent_id"] == root.step_id
    assert chat.span["trace_id"] == root.span["trace_id"]
    assert chat.span["attributes"]["gen_ai.request.model"] == "test-model"
    assert chat.started_at is not None and chat.finished_at is not None


def test_tool_call_records_plan_tool_and_respond(monkeypatch: MonkeyPatch) -> None:
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "合成の語"}, call_id="call-1")],
        [assistant_message("3 件です。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run) == [
        (CHAT, "plan", "done", None),
        (TOOL, "tool", "done", {"tool": LOOKUP}),
        (CHAT, "respond", "done", None),
    ]
    assert _terminal(run) == "done"
    _assert_contiguous(run)
    # ツールの span は Run の step と結ぶ（画面がデータの範囲・表の結果を足す）。
    [step] = run.steps
    finished = [
        event
        for event in run_progress_events(run)
        if isinstance(event, ChatProgressStepEvent)
        and event.name == TOOL
        and event.status == "done"
    ]
    assert len(finished) == 1
    assert finished[0].params is not None
    assert finished[0].params[RUN_STEP_PARAM] == step.id
    assert finished[0].attributes is not None
    assert finished[0].attributes["gen_ai.tool.name"] == LOOKUP
    # 終わったツールの詳細（内部名・引数・結果）は、その span の終わりのイベントで送る（#2373）。
    details = finished[0].details
    assert details is not None
    assert [(field.label, field.value) for field in details.fields][0] == ("ツール", LOOKUP)
    labels = [block.label for block in details.code]
    assert labels[0] == "引数"
    assert "合成の語" in details.code[0].text
    assert "結果" in labels


def test_running_tool_is_visible_while_it_runs(monkeypatch: MonkeyPatch) -> None:
    """実行中のツールは、終わる前から実行中の span として記録され、呼んだ引数を開いて見られる。"""
    seen: list[list[Row]] = []
    details: list[Any] = []

    def lookup(_arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        current = runtime_repository.get_run(context.run_id or "")
        seen.append(_rows(current))
        details.extend(
            event.details
            for event in run_progress_events(current)
            if isinstance(event, ChatProgressStepEvent) and event.name == TOOL
        )
        return {"rows": 1}

    definition = tool_registry.get(LOOKUP)
    assert definition is not None
    tool_registry.register(definition, lookup)
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "合成の語"}, call_id="call-1")],
        [assistant_message("1 件です。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    assert seen == [[(CHAT, "plan", "done", None), (TOOL, "tool", "running", {"tool": LOOKUP})]]
    assert details and details[0] is not None
    assert "合成の語" in details[0].code[0].text


def test_parallel_tools_in_one_turn(monkeypatch: MonkeyPatch) -> None:
    # 即時に終わる fake は次のツールの開始より先に完了し得るため、両方の開始を待つ。
    started = Barrier(2)

    def parallel_lookup(
        _arguments: dict[str, Any], _context: ToolInvocationContext
    ) -> dict[str, Any]:
        started.wait(timeout=5)
        return {"name": LOOKUP, "rows": 3}

    definition = tool_registry.get(LOOKUP)
    assert definition is not None
    tool_registry.register(definition, parallel_lookup)
    _script(
        monkeypatch,
        [
            function_call(LOOKUP, {"query": "今月"}, call_id="call-1"),
            function_call(LOOKUP, {"query": "先月"}, call_id="call-2"),
        ],
        [assistant_message("比べました。")],
    )
    run_id = _create_run("今月と先月を比べて")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert [(name, kind) for name, kind, _status, _params in _rows(run)] == [
        (CHAT, "plan"),
        (TOOL, "tool"),
        (TOOL, "tool"),
        (CHAT, "respond"),
    ]
    assert {status for _, _, status, _ in _rows(run)} == {"done"}


def test_span_from_a_tool_thread_is_nested_under_the_tool(monkeypatch: MonkeyPatch) -> None:
    """ツールのスレッドで開いた span（MCP の呼び出しなど）も、ツールの span の子として記録する。"""

    def lookup(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        with observed_span("mcp.request", kind="client", attributes={"pr.attempt": 1}):
            return {"rows": 1}

    definition = tool_registry.get(LOOKUP)
    assert definition is not None
    tool_registry.register(definition, lookup)
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "x"}, call_id="call-1")],
        [assistant_message("1 件です。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    steps = fold_chat_progress_events(run_progress_events(run)).step_list()
    [tool] = [step for step in steps if (step.span or {}).get("name") == TOOL]
    [mcp] = [step for step in steps if (step.span or {}).get("name") == "mcp.request"]
    assert mcp.span is not None and mcp.span["parent_id"] == tool.step_id
    assert mcp.status == "done"


def test_mcp_request_under_a_tool_carries_the_tool_title(monkeypatch: MonkeyPatch) -> None:
    """ツールの中の MCP の `tools/call` の span は、ツールの表示名（MCP の `title`）を
    `pr.tool.title` に持つ（画面の行は「MCP の呼び出し · 〈表示名〉」。#3468）。別のツールの
    呼び出しには付けない。"""

    def lookup(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        for name in (mcp_base_tool_name(LOOKUP), "other_tool"):
            with observed_span(
                "mcp.request",
                kind="client",
                attributes=tools_module._mcp_request_attributes(
                    {"method": "tools/call", "params": {"name": name}}
                ),
            ):
                pass
        return {"rows": 1}

    definition = tool_registry.get(LOOKUP)
    assert definition is not None
    tool_registry.register(definition, lookup)
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "x"}, call_id="call-1")],
        [assistant_message("1 件です。")],
    )
    run_id = _create_run()
    runtime_repository.note_builtin_tool_titles(run_id, {LOOKUP: "合成の照会"})

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    steps = fold_chat_progress_events(run_progress_events(run)).step_list()
    [tool] = [step for step in steps if (step.span or {}).get("name") == TOOL]
    calls = [step for step in steps if (step.span or {}).get("name") == "mcp.request"]
    assert all(step.span is not None and step.span["parent_id"] == tool.step_id for step in calls)
    assert [((step.span or {}).get("attributes") or {}).get("pr.tool.title") for step in calls] == [
        "合成の照会",
        None,
    ]


def test_approval_wait_is_recorded_when_the_run_resumes(monkeypatch: MonkeyPatch) -> None:
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-w")],
        [assistant_message("登録しました。")],
    )
    run_id = _create_run("登録して")
    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    # 待っている間は待ちの行を出さない（見出しの状態と承認のカードで示す）。実行の区間は閉じる。
    assert _rows(waiting) == [(CHAT, "plan", "done", None)]
    assert _terminal(waiting) is None
    assert [root.status for root in _roots(waiting)] == ["done"]

    _approve(run_id)
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run) == [
        (CHAT, "plan", "done", None),
        (APPROVAL, "approval", "done", {"tools": WRITE, "round": 1}),
        (TOOL, "tool", "done", {"tool": WRITE}),
        (CHAT, "respond", "done", None),
    ]
    assert _terminal(run) == "done"
    _assert_contiguous(run)
    # 再開の区間は同じ trace の、実行の区間の子（画面は 2 つの root を行にせず、1 本の木にする）。
    execute, resume = _roots(run)
    assert (execute.span["name"], resume.span["name"]) == ("agent.execute", "agent.resume")
    assert resume.span["parent_id"] == execute.step_id
    assert resume.span["trace_id"] == execute.span["trace_id"]
    approval = run.approvals[0]
    fold = fold_chat_progress_events(run_progress_events(run))
    [wait] = [step for step in fold.step_list() if (step.span or {}).get("name") == APPROVAL]
    assert wait.span is not None and wait.span["parent_id"] == resume.step_id
    assert (wait.started_at, wait.finished_at) == (approval.created_at, approval.decided_at)


_INCOMING_TRACE = "1" * 32
_INCOMING_TRACEPARENT = f"00-{_INCOMING_TRACE}-{'2' * 16}-01"


def test_run_from_an_external_request_starts_a_new_trace(monkeypatch: MonkeyPatch) -> None:
    """画面・API キーの外部の MCP クライアントの要求から作った Run は、来た traceparent を親に
    せず新しい trace を始め、要求の span は span link にだけ残す（#3272。RAG / NL2SQL と同じ
    `mcp_trace` の規則）。root の span は親を持たない。"""
    _script(monkeypatch, [assistant_message("合成の回答。")])
    links: list[Any] = []
    original = operation_trace

    @contextmanager
    def recording(name: str, **kwargs: Any) -> Iterator[Any]:
        links.append((name, list(kwargs.get("links", ()))))
        with original(name, **kwargs) as summary:
            yield summary

    monkeypatch.setattr(builtin_runtime, "operation_trace", recording)
    with attach_trace_context({"traceparent": _INCOMING_TRACEPARENT}):
        run_id = _create_run("合成の質問")

    created = runtime_repository.get_run(run_id)
    assert created.trace_context == {}
    assert created.trace_link_context == {"traceparent": _INCOMING_TRACEPARENT}

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    [root] = _roots(run)
    assert root.span is not None
    assert "parent_id" not in root.span
    assert root.span["trace_id"] != _INCOMING_TRACE
    assert run.trace_id == root.span["trace_id"]
    [(name, [link])] = links
    assert name == "agent.execute"
    assert f"{link.context.trace_id:032x}" == _INCOMING_TRACE


def test_run_without_a_request_span_has_no_link() -> None:
    """要求の span の外（dispatcher・テスト）で作った Run は親も span link も持たない。"""
    run = runtime_repository.get_run(_create_run("合成の質問"))
    assert (run.trace_context, run.trace_link_context) == ({}, {})


def test_tool_titles_are_in_tool_and_approval_params(monkeypatch: MonkeyPatch) -> None:
    """ツールの表示名（MCP の `title`。Run の metadata）があれば、ツールの span の params に
    `title`、承認の待ちの補足に表示名を出す（内部名の `tool` は残す）。"""
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-t")],
        [assistant_message("登録しました。")],
    )
    run_id = _create_run("登録して")
    runtime_repository.note_builtin_tool_titles(run_id, {WRITE: "記録の登録"})
    anyio.run(builtin_runtime.execute_run, run_id)
    _approve(run_id)
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run)[1:3] == [
        (APPROVAL, "approval", "done", {"tools": "記録の登録", "round": 1}),
        (TOOL, "tool", "done", {"tool": WRITE, "title": "記録の登録"}),
    ]
    # 表示名は足すだけ（空は記録しない）。
    runtime_repository.note_builtin_tool_titles(run_id, {})
    assert runtime_repository.get_run(run_id).metadata["tool_titles"] == {WRITE: "記録の登録"}


def test_rejected_tool_is_a_skipped_tool_span(monkeypatch: MonkeyPatch) -> None:
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-r")],
        [assistant_message("登録していません。")],
    )
    run_id = _create_run("登録して")
    anyio.run(builtin_runtime.execute_run, run_id)
    _approve(run_id, approved=False)
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run) == [
        (CHAT, "plan", "done", None),
        (APPROVAL, "approval", "done", {"tools": WRITE, "round": 1}),
        (TOOL, "tool", "skipped", {"tool": WRITE, "rejected": True}),
    ]
    assert _terminal(run) == "done"


def test_second_approval_round_is_its_own_wait(monkeypatch: MonkeyPatch) -> None:
    """承認の後に別の承認を求めたら、承認の待ちを回ごとに出す（同じ待ちを重ねない。#1358）。"""
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "1 件目"}, call_id="call-1")],
        [function_call(WRITE, {"query": "2 件目"}, call_id="call-2")],
        [assistant_message("2 件登録しました。")],
    )
    run_id = _create_run("2 件登録して")
    anyio.run(builtin_runtime.execute_run, run_id)
    _approve(run_id)
    anyio.run(builtin_runtime.resume_run, run_id)

    second = runtime_repository.get_run(run_id)
    assert second.status == "waiting_approval"
    assert _rows(second) == [
        (CHAT, "plan", "done", None),
        (APPROVAL, "approval", "done", {"tools": WRITE, "round": 1}),
        (TOOL, "tool", "done", {"tool": WRITE}),
        (CHAT, "plan", "done", None),
    ]

    _approve(run_id)
    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run) == [
        (CHAT, "plan", "done", None),
        (APPROVAL, "approval", "done", {"tools": WRITE, "round": 1}),
        (TOOL, "tool", "done", {"tool": WRITE}),
        (CHAT, "plan", "done", None),
        (APPROVAL, "approval", "done", {"tools": WRITE, "round": 2}),
        (TOOL, "tool", "done", {"tool": WRITE}),
        (CHAT, "respond", "done", None),
    ]
    assert _terminal(run) == "done"
    _assert_contiguous(run)
    # 3 つの区間（実行・再開・再開）は 1 つの trace。
    assert len({root.span["trace_id"] for root in _roots(run)}) == 1


def test_invalid_arguments_are_a_skipped_tool_span(monkeypatch: MonkeyPatch) -> None:
    """引数の誤りで呼び先へ送らなかった呼び出しも、実行しなかったツールとして残す（#3226）。"""
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"other": "x"}, call_id="call-bad")],
        [assistant_message("調べられませんでした。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    rows = _rows(run)
    assert rows[0] == (CHAT, "plan", "done", None)
    assert rows[1] == (TOOL, "tool", "skipped", {"tool": LOOKUP})
    skipped = [
        event
        for event in run_progress_events(run)
        if isinstance(event, ChatProgressStepEvent) and event.name == TOOL
    ]
    details = skipped[-1].details
    assert details is not None
    assert any(field.label == "エラー" for field in details.fields)


def test_unattended_run_keeps_the_tool_it_did_not_run(monkeypatch: MonkeyPatch) -> None:
    """承認の要るツールを呼ばない Run（外部の MCP クライアント）も、呼ばなかったツールを残す。"""
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-mcp")],
        [assistant_message("登録はできませんでした。")],
    )
    run_id = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="登録して", agent_id=AGENT_ID),
        created_by_user_uuid=LOCAL_DEBUG_USER_UUID,
        extra_metadata={"source": "mcp", "api_key_id": "0123456789abcdef"},
    ).id

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert _rows(run) == [
        (CHAT, "plan", "done", None),
        (TOOL, "tool", "skipped", {"tool": WRITE}),
        (CHAT, "respond", "done", None),
    ]
    [step] = run.steps
    skipped = [
        event
        for event in run_progress_events(run)
        if isinstance(event, ChatProgressStepEvent) and event.name == TOOL
    ]
    assert skipped[-1].params is not None and skipped[-1].params[RUN_STEP_PARAM] == step.id


def test_failed_run_records_failed_terminal(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(builtin_runtime, "_max_turns", lambda: 1)
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "売上"}, call_id="call-1")],
        [function_call(LOOKUP, {"query": "売上"}, call_id="call-2")],
    )
    run_id = _create_run("ずっと調べる")

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == "failed"
    assert _terminal(run) == "failed"
    rows = _rows(run)
    assert rows[0][:3] == (CHAT, "plan", "done")
    assert not any(status == "running" for _, _, status, _ in rows)


def test_run_cancelled_before_it_started_has_no_progress() -> None:
    """開始の前に停止した Run は span が無く、処理の経過は空（古い形式の記録ではない）。"""
    run_id = _create_run()

    cancelled = runtime_repository.cancel_run(run_id)

    assert cancelled.trace_id is None and cancelled.trace_store is True
    assert run_progress_events(cancelled) == []


def test_cancel_while_waiting_for_approval_ends_the_progress(monkeypatch: MonkeyPatch) -> None:
    _script(monkeypatch, [function_call(WRITE, {"query": "登録"}, call_id="call-c")])
    run_id = _create_run("登録して")
    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.cancel_run(run_id)

    assert _rows(run) == [(CHAT, "plan", "done", None)]
    assert _terminal(run) == "cancelled"


def test_cancel_from_another_context_while_a_tool_runs(monkeypatch: MonkeyPatch) -> None:
    """実行中のツールの間に別の文脈（画面の操作）で停止したら、終端は停止で、開いていた span は
    スキップで閉じる。停止の後に終わった span は表に書かない（表では実行中のまま残り、Run の終端
    に合わせて閉じる。終端の後に行を増やさない）。"""

    def lookup(_arguments: dict[str, Any], context: ToolInvocationContext) -> dict[str, Any]:
        # 実行の文脈（trace の記録）を持たない、新しい文脈で停止する。
        contextvars.Context().run(runtime_repository.cancel_run, context.run_id or "")
        return {"rows": 1}

    definition = tool_registry.get(LOOKUP)
    assert definition is not None
    tool_registry.register(definition, lookup)
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "x"}, call_id="call-1")],
        [assistant_message("1 件です。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == "cancelled"
    assert _terminal(run) == "cancelled"
    assert isinstance(run_progress_events(run)[-1], ChatProgressTerminalEvent)
    assert _rows(run) == [
        (CHAT, "plan", "done", None),
        (TOOL, "tool", "skipped", {"tool": LOOKUP}),
    ]
    assert all(root.status == "skipped" for root in _roots(run))
    _assert_contiguous(run)


# --------------------------------------------------------------------------------------------
# 保存と読み込み
# --------------------------------------------------------------------------------------------


def _seed_completed_run(repository: AgentRuntimeRepository) -> RunState:
    """trace id と工程の詳細（ツールの引数と結果）を Run に残して完了した Run。"""
    from app.features.agent.tools import ToolResult

    run = repository.create_builtin_run(
        RunCreateRequest(goal="保存", agent_id="default"),
        created_by_user_uuid=LOCAL_DEBUG_USER_UUID,
    )
    assert repository.begin_builtin_run(run.id) is not None
    owner = run_trace_owner(run.id, LOCAL_DEBUG_USER_UUID)
    assert owner is not None

    def listen(update: SpanUpdate) -> None:
        if repository.record_builtin_span(run.id, update):
            trace_spans.submit(update, owner)

    call = ToolCall(name="echo", arguments={"text": "x"}, trace_id="call-1")
    result = ToolResult(name="echo", success=True, output={"text": "x"})
    with operation_trace("agent.execute", operation_id=run.id, listener=listen):
        with observed_span("gen_ai.execute_tool", attributes={"gen_ai.tool.name": "echo"}) as span:
            step_id, _context = repository.start_builtin_tool_step(run.id, call)
            repository.finish_builtin_tool_step(run.id, step_id, result)
            display = tool_display(call, {}, result=result, run_step=step_id)
            span.set_display(kind=display.kind, params=display.params, details=display.details)
        return repository.complete_builtin_run(run.id, "回答しました。")


def test_trace_id_and_step_details_survive_the_file_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    repository = AgentRuntimeRepository(snapshot_path=path)
    completed = _seed_completed_run(repository)
    assert completed.trace_id is not None
    assert _terminal(completed) == "done"
    assert [row[0] for row in _rows(completed)] == ["gen_ai.execute_tool"]
    [details] = completed.span_details.values()
    assert details.fields[0].value == "echo"

    reloaded = AgentRuntimeRepository(snapshot_path=path).get_run(completed.id)

    assert reloaded.trace_id == completed.trace_id
    assert reloaded.span_details == completed.span_details
    assert reloaded.trace_store is True
    # 保存の JSON には処理の経過のイベント・trace の要約を書かない（記録は表 1 つ）。工程の詳細は
    # 値の無い項目を出さない。
    saved = json.loads(path.read_text(encoding="utf-8"))
    [saved_run] = [item for item in saved["runs"] if item["id"] == completed.id]
    assert not {"progress_events", "trace_summary", "trace_segments"} & set(saved_run)
    assert all("truncated" not in item for item in saved_run["span_details"].values())


def test_span_details_are_saved_together_in_the_background(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """span の知らせで Run に残すもの（工程の詳細）はメモリの Run にすぐ入れ、保存先へは少し待って
    まとめて書く（知らせのたびに Run の JSON を書かない）。"""
    from app.features.agent import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "BACKGROUND_WRITES", True)
    monkeypatch.setattr(runtime_module, "SPAN_PERSIST_DELAY_SECONDS", 0.05)
    path = tmp_path / "runtime.json"
    repository = AgentRuntimeRepository(snapshot_path=path)
    run = repository.create_builtin_run(RunCreateRequest(goal="保存", agent_id="default"))
    assert repository.begin_builtin_run(run.id) is not None
    repository.flush_writes()
    staged: list[int] = []
    original = repository._persist_locked  # noqa: SLF001 - 保存の予定の回数を数える

    def counting(*, background: bool = False) -> bool:
        staged.append(1)
        return original(background=background)

    monkeypatch.setattr(repository, "_persist_locked", counting)

    def listener(update: SpanUpdate) -> None:
        repository.record_builtin_span(run.id, update)

    with operation_trace("agent.execute", operation_id=run.id, listener=listener):
        for index in range(5):
            details = chat_progress_details(fields=[("ツール", f"synthetic_{index}")])
            with observed_span("gen_ai.execute_tool", display=SpanDisplay(details=details)):
                pass
        # メモリの Run にはすぐ入る。
        assert len(repository.get_run(run.id).span_details) == 5

    def saved_details() -> int:
        saved = json.loads(path.read_text(encoding="utf-8"))
        [item] = [entry for entry in saved["runs"] if entry["id"] == run.id]
        return len(item.get("span_details", {}))

    for _ in range(100):
        if repository.pending_writes() == 0 and saved_details() == 5:
            break
        anyio.run(anyio.sleep, 0.05)
    # 5 つの詳細と trace id が保存され、保存の予定は知らせの数（開始と終了の 12）より少ない。
    assert saved_details() == 5
    assert 0 < len(staged) < 12


@pytest.mark.parametrize(
    "repository_class",
    [AgentRuntimeOracleCheckpointRepository, AgentRuntimeOracleNormalizedRepository],
    ids=["checkpoint", "normalized"],
)
def test_trace_id_and_step_details_survive_the_oracle_checkpoint(repository_class: Any) -> None:
    store = _FakeOracleStore()

    def connect() -> _FakeOracleConnection:
        return _FakeOracleConnection(store)

    repository = repository_class(connect_factory=connect)
    completed = _seed_completed_run(repository)

    reloaded = repository_class(connect_factory=connect).get_run(completed.id)

    assert reloaded.trace_id == completed.trace_id
    assert reloaded.span_details == completed.span_details
    assert _terminal(reloaded) == "done"
    assert [row[0] for row in _rows(reloaded)] == ["gen_ai.execute_tool"]


def test_old_progress_records_are_not_read_and_broken_details_are_dropped() -> None:
    """古い処理の経過のイベント・trace の要約は読まない（#3226 の案 A。後方互換は持たない）。
    形の違う工程の詳細は捨てる（壊れた記録で Run の読み込みを止めない）。"""
    run = RunState.model_validate(
        {
            "id": "run_old_progress",
            "goal": "x",
            "agent_id": "default",
            "status": "completed",
            "progress_events": [{"type": "terminal", "seq": 1, "status": "done"}],
            "trace_summary": {"schema_version": 1},
            "trace_segments": [],
            "span_details": {
                "a000000000000001": {"fields": [{"label": "ツール", "value": "echo"}]},
                "a000000000000002": "broken",
            },
        }
    )

    assert run.trace_store is False
    assert run.trace_id is None
    assert list(run.span_details) == ["a000000000000001"]
    dumped = run.model_dump(mode="json")
    assert not {"progress_events", "trace_summary", "trace_segments"} & set(dumped)
    # trace の無い Run は表を読まない（処理の経過は空。画面は古い形式の案内を出す）。
    assert run_progress_events(run) == []


# --------------------------------------------------------------------------------------------
# 共通の span の表への書き込み（Oracle の書き手。fake の DB）
# --------------------------------------------------------------------------------------------


class _FakeSpanCursor:
    def __init__(self, db: _FakeSpanDb) -> None:
        self.db = db
        self._result: list[tuple[Any, ...]] = []

    def setinputsizes(self, **_sizes: Any) -> None:
        pass

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        assert "MERGE INTO PLATFORM_TRACE_SPANS" in sql
        for row in rows:
            self.db.rows[(row["trace_id"], row["span_id"])] = dict(row)

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        assert sql.startswith("SELECT") and "FROM PLATFORM_TRACE_SPANS" in sql
        rows = [
            row
            for row in self.db.rows.values()
            if row["trace_id"] == binds["trace_id"]
            and ("viewer" not in binds or row["owner_user_uuid"] == binds["viewer"])
        ]
        rows.sort(key=lambda row: (row["started_at"], row["span_id"]))
        self._result = [tuple(row[column] for column in _SPAN_COLUMNS) for row in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


_SPAN_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "product",
    "operation_id",
    "attempt",
    "is_root",
    "owner_user_uuid",
    "name",
    "span_kind",
    "status",
    "started_at",
    "finished_at",
    "display_kind",
    "display_params_json",
    "attributes_json",
)


class _FakeSpanConnection:
    def __init__(self, db: _FakeSpanDb) -> None:
        self.db = db

    def cursor(self) -> _FakeSpanCursor:
        return _FakeSpanCursor(self.db)

    def commit(self) -> None:
        self.db.commits += 1


class _FakeSpanDb:
    """共通の span の表の fake（主キーで MERGE・trace id と持ち主で SELECT）。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.commits = 0

    @contextmanager
    def connect(self, _settings: Any = None) -> Iterator[_FakeSpanConnection]:
        yield _FakeSpanConnection(self)


@pytest.fixture
def span_table(monkeypatch: MonkeyPatch) -> Iterator[_FakeSpanDb]:
    """Oracle の保存先と同じく、共通の書き手で表（fake）に書いて表から読む。"""
    import app.oracle_connection as oracle_connection

    db = _FakeSpanDb()
    monkeypatch.setattr(oracle_connection, "platform_oracle_connection", db.connect)
    writer = TraceSpanWriter(connection_factory=db.connect, flush_seconds=60)
    trace_spans.use_writer(writer)
    try:
        yield db
    finally:
        trace_spans.use_writer(None)


def test_oracle_writer_writes_the_run_spans_without_step_details(
    monkeypatch: MonkeyPatch, span_table: _FakeSpanDb
) -> None:
    """Run の span は共通の書き手で表に書く（製品・Run の id・実行した利用者が持ち主）。工程の詳細
    （ツールの引数と結果）は表に入れず Run に残し、読み出しの API が Run から足す。"""
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "合成の語"}, call_id="call-1")],
        [assistant_message("3 件です。")],
    )
    run_id = _create_run()

    anyio.run(builtin_runtime.execute_run, run_id)
    assert trace_spans.uses_table
    # 主の処理は queue に積むだけ（書き手の間隔の前は表に無い）。
    assert span_table.rows == {}
    assert trace_spans._writer is not None  # noqa: SLF001 - テストで待たずに書く
    trace_spans._writer.flush()  # noqa: SLF001

    run = runtime_repository.get_run(run_id)
    rows = list(span_table.rows.values())
    assert {row["trace_id"] for row in rows} == {run.trace_id}
    assert {(row["product"], row["operation_id"], row["owner_user_uuid"]) for row in rows} == {
        ("agent", run_id, run.created_by_user_uuid)
    }
    assert sum(row["is_root"] for row in rows) == 1
    assert {row["name"] for row in rows} >= {"agent.execute", CHAT, TOOL}
    assert all(row["status"] != "running" for row in rows)
    # 工程の詳細（検索の語を含む引数）は表に入れない。
    assert not any("合成の語" in json.dumps(row, default=str, ensure_ascii=False) for row in rows)
    assert any(
        "合成の語" in json.dumps(item.model_dump(), ensure_ascii=False)
        for item in run.span_details.values()
    )
    assert run.trace_dropped_spans == 0
    # 読み出しの API は表を読み、工程の詳細を Run から足す。
    response = client.get(f"/api/runs/{run_id}/trace")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    [tool] = [
        event for event in data["events"] if event.get("name") == TOOL and event["type"] == "step"
    ]
    assert "合成の語" in json.dumps(tool["details"], ensure_ascii=False)
    assert data["events"][-1]["type"] == "terminal"


def test_dropped_spans_are_kept_on_the_run(monkeypatch: MonkeyPatch) -> None:
    """書けなかった span の数は、実行の区切りを確定するときに Run に残す（画面は一部だけの
    案内）。"""
    monkeypatch.setattr(trace_spans, "dropped", lambda _trace_id: 3)
    _script(monkeypatch, [assistant_message("こんにちは。")])
    run_id = _create_run("挨拶して")

    anyio.run(builtin_runtime.execute_run, run_id)

    assert runtime_repository.get_run(run_id).trace_dropped_spans == 3
    data = client.get(f"/api/runs/{run_id}/trace").json()["data"]
    assert data["dropped_spans"] == 3


# --------------------------------------------------------------------------------------------
# API（`GET /api/runs/{run_id}/trace`・権限）
# --------------------------------------------------------------------------------------------


def _completed_api_run(monkeypatch: MonkeyPatch, owner: str = LOCAL_DEBUG_USER_UUID) -> str:
    _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "売上"}, call_id="call-1")],
        [assistant_message("3 件です。")],
    )
    # 会話は作った利用者だけが読む（local はローカル利用者）。
    run_id = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="今月の売上を教えて", agent_id=AGENT_ID),
        created_by_user_uuid=owner,
    ).id
    anyio.run(builtin_runtime.execute_run, run_id)
    return run_id


def test_run_trace_returns_the_span_snapshot(monkeypatch: MonkeyPatch) -> None:
    run_id = _completed_api_run(monkeypatch)
    run = runtime_repository.get_run(run_id)

    response = client.get(f"/api/runs/{run_id}/trace")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["target_id"], data["trace_id"], data["dropped_spans"]) == (
        run_id,
        run.trace_id,
        0,
    )
    events = data["events"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1] == {
        "schema_version": 1,
        "seq": len(events),
        "target_id": run_id,
        "attempt": 0,
        "emitted_at": events[-1]["emitted_at"],
        "type": "terminal",
        "status": "done",
    }
    # span のイベントは span の名前・親・trace id を持つ（画面が木にする）。
    spans = [event for event in events if event["type"] == "step"]
    assert spans and all(event.get("name") and event["trace_id"] == run.trace_id for event in spans)
    assert {event["name"] for event in spans} >= {"agent.execute", CHAT, TOOL}
    # Run の応答には処理の経過のイベントを入れない（記録は表 1 つ。画面は trace の API で読む）。
    detail = client.get(f"/api/runs/{run_id}").json()["data"]
    assert "progress_events" not in detail
    assert (detail["trace_store"], detail["trace_id"]) == (True, run.trace_id)
    assert client.get("/api/runs/run-missing/trace").status_code == 404


def test_run_trace_read_failure_is_503(monkeypatch: MonkeyPatch) -> None:
    run_id = _completed_api_run(monkeypatch)

    def unavailable(_trace_id: str, *, viewer_user_uuid: str | None) -> list[Any]:
        raise RuntimeError("ORA-00942")

    monkeypatch.setattr(trace_spans, "fetch", unavailable)

    response = client.get(f"/api/runs/{run_id}/trace")

    assert response.status_code == 503
    assert "ORA-" not in response.text


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Any:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_run_trace_follows_the_run_scope(monkeypatch: MonkeyPatch, auth: ProductionAuth) -> None:
    owner = auth.user_with_permissions(
        "progress-chat", ["agent.runs.operate"], agent_ids=[AGENT_ID]
    )
    run_id = _completed_api_run(monkeypatch, owner=owner.user_uuid)
    path = f"/api/runs/{run_id}/trace"

    # チャットの利用者（業務 Agent の実行）は、対象範囲の Agent の自分の Run を読める。
    assert client.get(path, headers=login("progress-chat")).status_code == 200

    # 同じ範囲でも、ほかの利用者の Run は 404（存在を漏らさない。#1889）。
    auth.user_with_permissions("progress-peer", ["agent.runs.operate"], agent_ids=[AGENT_ID])
    assert client.get(path, headers=login("progress-peer")).status_code == 404

    # 対象範囲の外の Agent の、ほかの利用者の Run も 404。
    auth.user_with_permissions("progress-other", ["agent.runs.operate"], agent_ids=["other"])
    assert client.get(path, headers=login("progress-other")).status_code == 404

    # メニューも capability も無い利用者は経路で 403。
    auth.user_with_permissions("progress-none", [], agent_ids=[AGENT_ID])
    assert client.get(path, headers=login("progress-none")).status_code == 403
    # ログインしていなければ 401。
    assert client.get(path).status_code == 401


def test_run_trace_shows_only_the_run_users_spans_except_to_system_admin(
    monkeypatch: MonkeyPatch, auth: ProductionAuth
) -> None:
    """同じ trace にほかの利用者が書いた span は、system_admin だけに見える（表の持ち主の判定）。

    呼び先の MCP の span はサービストークンの `sub`（Run の利用者）が持ち主なので、Run の利用者に
    見える。
    """
    owner = auth.user_with_permissions("trace-owner", ["agent.runs.operate"], agent_ids=[AGENT_ID])
    run_id = _completed_api_run(monkeypatch, owner=owner.user_uuid)
    run = runtime_repository.get_run(run_id)
    assert run.trace_id is not None
    [root] = [span for span in trace_spans.fetch(run.trace_id, viewer_user_uuid=None) if span.root]
    now = datetime.now(UTC)

    def called(span_id: str, user_uuid: str) -> None:
        trace_spans.submit(
            SpanUpdate(
                span_id=span_id,
                parent_span_id=root.span_id,
                trace_id=run.trace_id or "",
                operation_name="mcp.server",
                status="done",
                started_at=now,
                finished_at=now,
                root=True,
                span_kind="server",
            ),
            TraceSpanOwner(product="rag", operation_id="synthetic-call", owner_user_uuid=user_uuid),
        )

    called("b000000000000001", owner.user_uuid)
    called("b000000000000002", "synthetic-other-user")

    def server_spans(headers: dict[str, str]) -> set[str]:
        response = client.get(f"/api/runs/{run_id}/trace", headers=headers)
        assert response.status_code == 200, response.text
        return {
            event["step_id"]
            for event in response.json()["data"]["events"]
            if event.get("name") == "mcp.server"
        }

    assert server_spans(login("trace-owner")) == {"b000000000000001"}
    auth.create_user("trace-admin", system_admin=True)
    assert server_spans(login("trace-admin")) == {"b000000000000001", "b000000000000002"}


def test_created_run_has_no_trace_yet(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda run: None)

    created = client.post("/api/runs", json={"goal": "質問", "agent_id": "default"})

    assert created.status_code == 200, created.text
    data = created.json()["data"]
    # 処理の経過は実行の区間の span から記録する（受け付けただけの Run は trace が無く空）。
    assert (data["trace_store"], data["trace_id"]) == (True, None)
    trace = client.get(f"/api/runs/{data['id']}/trace").json()["data"]
    assert (trace["trace_id"], trace["events"]) == (None, [])


@pytest.fixture(autouse=True)
def enable_trace_content_for_existing_detail_tests() -> Iterator[None]:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield


def test_body_off_keeps_the_normal_run_and_its_timing(monkeypatch: MonkeyPatch) -> None:
    from pr_backend_core.observability.call_policy import call_content_policy

    _script(monkeypatch, [assistant_message("合成の回答")])
    with call_content_policy(lambda: False):
        run_id = _create_run("合成の質問")
        anyio.run(builtin_runtime.execute_run, run_id)
    run = runtime_repository.get_run(run_id)
    assert _terminal(run) == "done"
    assert _rows(run) == [(CHAT, "respond", "done", None)]
    fold = fold_chat_progress_events(run_progress_events(run))
    [chat] = [step for step in fold.step_list() if (step.span or {}).get("name") == CHAT]
    assert chat.started_at is not None and chat.finished_at is not None
    assert not chat.details or not chat.details.code
