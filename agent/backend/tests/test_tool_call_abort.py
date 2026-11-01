"""Run の取消で、実行中の MCP の呼び出し（HTTP）を打ち切る（#2691）の決定論テスト。

- 取消で、読み取りのツールの送信中の接続を切る（呼び先は切断を検知する）。step は失敗ではなく
  取消で、以後のツール・モデルの呼び出しは始まらない。
- 別のプロセスの取消は、実行中の呼び出しの間の定期の確認（保存先を読み直す）で知って打ち切る。
- 打ち切った後は送らない。書き込みのツールは送った後は切らず、届いた結果を事実のまま返す。

呼び先は 127.0.0.1 の合成の HTTP サーバー（応答を返さずに切断を待つ）。モデルは SDK の
`ScriptedModel`、保存先は memory。外部への通信はしない。
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from typing import Any

import anyio
import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import MonkeyPatch
from security_support import client

from app.features.agent import builtin_runtime, call_abort, tools
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.call_abort import TOOL_CALL_ABORTED_CODE
from app.features.agent.config import runtime_config_store
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import (
    ExternalToolError,
    McpRetryKind,
    McpSession,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    tool_registry,
)

READ = "test2691_read"
WRITE = "test2691_write"
SKILL_ID = "test2691-skill"
AGENT_ID = "test2691-agent"
USER_UUID = "11111111-2222-3333-4444-555555552691"
# 合成のサーバーが切断を待つ上限（秒）。打ち切れなければテストはこの秒の後に失敗する。
WAIT_SECONDS = 10.0


class _SlowMcp:
    """`tools/call` を受けたら応答を返さずに、client の切断（か `release`）を待つ合成の MCP。"""

    def __init__(self) -> None:
        self.received = threading.Event()
        self.disconnected = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # テストの出力を汚さない
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                owner.calls += 1
                owner.received.set()
                if owner._wait_disconnect(self.connection):
                    owner.disconnected.set()
                    return
                payload = {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"structuredContent": {"ok": True}, "isError": False},
                }
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/api/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _wait_disconnect(self, connection: socket.socket) -> bool:
        """client が切断したら True、`release` されたら False（応答を返す）。"""
        connection.settimeout(0.05)
        end = monotonic() + WAIT_SECONDS
        while monotonic() < end and not self.release.is_set():
            try:
                if connection.recv(1, socket.MSG_PEEK) == b"":
                    return True
            except TimeoutError:
                continue
            except OSError:
                return True
        connection.settimeout(None)
        return False

    def __enter__(self) -> _SlowMcp:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


def _session(url: str) -> McpSession:
    # 固定の session id で initialize を省く（送るのは tools/call の 1 回だけ）。
    return McpSession(
        url=url,
        headers={},
        timeout_seconds=WAIT_SECONDS + 5,
        max_retries=0,
        service_code="mcp",
        service_label="合成の MCP",
        session_id="session-2691",
    )


def _count_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }


@pytest.fixture
def slow_mcp() -> Iterator[_SlowMcp]:
    with _SlowMcp() as server:
        yield server


@pytest.fixture
def agent_tools(slow_mcp: _SlowMcp) -> Iterator[list[str]]:
    """読み取り（合成の MCP を呼ぶ）と書き込み（呼ばれた回数を数える）の 2 つのツール。"""
    writes: list[str] = []

    def read(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        response = _session(slow_mcp.url).request(
            "tools/call", {"name": "slow", "arguments": {}}, request_id="r-1", idempotent=True
        )
        return dict(response.result or {})

    def write(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        writes.append("write")
        return {"ok": True}

    original = runtime_config_store.get_tool_policy()
    for name, level, handler in (
        (READ, ToolPermissionLevel.READ, read),
        (WRITE, ToolPermissionLevel.WRITE, write),
    ):
        tool_registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} の合成のツール",
                input_schema=_count_schema(),
                output_schema={"type": "object"},
                permission_level=level,
                side_effects=level == ToolPermissionLevel.WRITE,
            ),
            handler,
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="打ち切りのテストの Skill",
            instructions="読んでから書く。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="control-plane", tool_names=[READ, WRITE])
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="打ち切りのテストの業務 Agent", skill_ids=[SKILL_ID])
    )
    runtime_config_store.patch_tool_policy(allow=[READ, WRITE], ask=[], deny=[])
    try:
        yield writes
    finally:
        runtime_config_store.patch_tool_policy(
            default_mode=original.default_mode,
            allow=sorted(original.allow),
            ask=sorted(original.ask),
            deny=sorted(original.deny),
        )
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
        for name in (READ, WRITE):
            tool_registry._definitions.pop(name, None)  # noqa: SLF001 - テスト用の登録を戻す
            tool_registry._handlers.pop(name, None)  # noqa: SLF001


def _script(monkeypatch: MonkeyPatch) -> ScriptedModel:
    """読み取り → 書き込み → 回答、の台本のモデル。"""
    model = ScriptedModel(
        [
            [function_call(READ, {"count": 1}, call_id="call-read")],
            [function_call(WRITE, {"count": 1}, call_id="call-write")],
            [assistant_message("書きました。")],
        ]
    )
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


def _create_run() -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="読んで書いて", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    return run.id


def _assert_aborted(run_id: str, model: ScriptedModel, writes: list[str], reason: str) -> None:
    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    # 取消の後は書き込みのツールもモデルも呼ばない。
    assert writes == []
    assert len(model.calls) == 1
    [step] = run.steps
    assert step.status == "cancelled"
    assert step.tool_result is not None
    assert step.tool_result.success is False
    assert step.tool_result.error_code == TOOL_CALL_ABORTED_CODE
    assert step.tool_result.error_details["reason"] == reason
    assert step.tool_result.error_details["transport_error_code"] == "mcp.call_aborted"
    # 失敗ではなく取消のまま。
    assert not [event for event in run.events if event.type == "runtime.failed"]


def test_cancel_cuts_the_in_flight_mcp_call(
    monkeypatch: MonkeyPatch, slow_mcp: _SlowMcp, agent_tools: list[str]
) -> None:
    """同じプロセスの取消で、送信中の読み取りの接続をすぐに切る（呼び先は切断を検知する）。"""
    model = _script(monkeypatch)
    run_id = _create_run()

    def cancel_when_sent() -> None:
        assert slow_mcp.received.wait(WAIT_SECONDS)
        runtime_repository.cancel_run(run_id, cancelled_by="user-a")

    canceller = threading.Thread(target=cancel_when_sent)
    canceller.start()
    started = monotonic()
    anyio.run(builtin_runtime.execute_run, run_id)
    canceller.join(WAIT_SECONDS)

    assert slow_mcp.disconnected.wait(WAIT_SECONDS)
    # 呼び先の応答（WAIT_SECONDS 秒の待ち）を待たずに戻る。
    assert monotonic() - started < WAIT_SECONDS / 2
    assert slow_mcp.calls == 1
    _assert_aborted(run_id, model, agent_tools, call_abort.REASON_RUN_CANCELLED)


def test_mcp_agent_cancel_run_cuts_the_in_flight_mcp_call(
    monkeypatch: MonkeyPatch, slow_mcp: _SlowMcp, agent_tools: list[str]
) -> None:
    """外部の MCP の取消（agent_cancel_run。#3240）も、画面の取消と同じく送信中の読み取りを切る。"""
    model = _script(monkeypatch)
    run_id = _create_run()
    results: list[dict[str, Any]] = []

    def cancel_when_sent() -> None:
        assert slow_mcp.received.wait(WAIT_SECONDS)
        response = client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "agent_cancel_run", "arguments": {"run_id": run_id}},
            },
        )
        results.append(response.json()["result"]["structuredContent"])

    canceller = threading.Thread(target=cancel_when_sent)
    canceller.start()
    started = monotonic()
    anyio.run(builtin_runtime.execute_run, run_id)
    canceller.join(WAIT_SECONDS)

    assert slow_mcp.disconnected.wait(WAIT_SECONDS)
    assert monotonic() - started < WAIT_SECONDS / 2
    assert [item["result"] for item in results] == ["cancelled"]
    _assert_aborted(run_id, model, agent_tools, call_abort.REASON_RUN_CANCELLED)


def test_cancel_by_another_process_is_seen_while_the_call_is_in_flight(
    monkeypatch: MonkeyPatch, slow_mcp: _SlowMcp, agent_tools: list[str]
) -> None:
    """別のプロセスの取消（このプロセスへの通知は無い）を、実行中の呼び出しの間の確認で知る。"""
    model = _script(monkeypatch)
    monkeypatch.setattr(builtin_runtime, "TOOL_CALL_CANCEL_POLL_SECONDS", 0.05)
    # 同じプロセスの通知を止め、保存先の確認だけで知る形にする。
    monkeypatch.setattr(call_abort, "abort_run_calls", lambda *_args, **_kwargs: 0)
    run_id = _create_run()

    def cancel_when_sent() -> None:
        assert slow_mcp.received.wait(WAIT_SECONDS)
        runtime_repository.cancel_run(run_id)

    canceller = threading.Thread(target=cancel_when_sent)
    canceller.start()
    anyio.run(builtin_runtime.execute_run, run_id)
    canceller.join(WAIT_SECONDS)

    assert slow_mcp.disconnected.wait(WAIT_SECONDS)
    _assert_aborted(run_id, model, agent_tools, call_abort.REASON_RUN_ENDED)


def test_aborted_call_is_not_sent(monkeypatch: MonkeyPatch) -> None:
    """打ち切った後の送信は始めない（呼び先へは 1 回も送らない）。"""
    sent: list[httpx.Request] = []
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("app.features.agent.tools.httpx.Client", client)
    with call_abort.bind_call_abort("run-2691-unit") as abort:
        abort.abort(call_abort.REASON_RUN_CANCELLED)
        with pytest.raises(ExternalToolError) as raised:
            _session("http://rag.test/api/mcp").request(
                "tools/call", {"name": "slow", "arguments": {}}, request_id="r-3"
            )

    assert raised.value.code == "mcp.call_aborted"
    assert raised.value.details["retryable"] is False
    assert abort.interrupted
    assert sent == []


def test_abort_of_another_run_does_not_cut_the_call(slow_mcp: _SlowMcp) -> None:
    """取消は同じ Run の呼び出しだけを打ち切る（別の Run の取消では切らない）。"""
    outcome: dict[str, Any] = {}

    def post() -> None:
        outcome["data"] = _session(slow_mcp.url).request(
            "tools/call", {"name": "slow", "arguments": {}}, request_id="r-4", idempotent=True
        )

    with call_abort.bind_call_abort("run-2691-mine"):
        thread = threading.Thread(target=contextvars.copy_context().run, args=(post,))
        thread.start()
        assert slow_mcp.received.wait(WAIT_SECONDS)

        assert call_abort.abort_run_calls("run-2691-other") == 0
        assert not slow_mcp.disconnected.wait(0.3)
        slow_mcp.release.set()
        thread.join(WAIT_SECONDS)

    assert outcome["data"].result == {"structuredContent": {"ok": True}, "isError": False}


def test_write_call_in_flight_is_not_cut_and_its_result_is_kept(slow_mcp: _SlowMcp) -> None:
    """書き込み（unsafe）のツールは、送った後に打ち切っても接続を切らず、届いた結果を返す。"""
    outcome: dict[str, Any] = {}

    def post() -> None:
        outcome["data"] = tools._post_mcp_message(  # noqa: SLF001
            service_code="mcp",
            service_label="合成の MCP",
            url=slow_mcp.url,
            payload={"jsonrpc": "2.0", "id": "r-5", "method": "tools/call", "params": {}},
            headers={},
            timeout_seconds=WAIT_SECONDS + 5,
            max_retries=0,
            kind=McpRetryKind.UNSAFE,
        )

    with call_abort.bind_call_abort("run-2691-write") as abort:
        thread = threading.Thread(target=contextvars.copy_context().run, args=(post,))
        thread.start()
        assert slow_mcp.received.wait(WAIT_SECONDS)

        assert call_abort.abort_run_calls("run-2691-write") == 1
        assert not slow_mcp.disconnected.wait(0.3)
        slow_mcp.release.set()
        thread.join(WAIT_SECONDS)

    data, _headers = outcome["data"]
    assert data["result"]["structuredContent"] == {"ok": True}
    assert not abort.interrupted
