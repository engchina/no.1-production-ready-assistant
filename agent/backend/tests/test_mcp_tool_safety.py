"""MCP 接続のツールの名前と再送の安全の決定論テスト。

- #2596: 文字の置き換えで同じ名前になる別のツール（`read.item` と `read_item`）を、黙って片方だけに
  しない。名前は一覧の順に依らず決まり、どちらも元のツールに届く。別の出自が同じ名前になったら
  どれも渡さずに警告を残す。
- #2595: 注釈（readOnlyHint）を信頼しない接続では、申告だけで送信後の失敗（502 / 504・通信の途中の
  切断）を再送しない。送信前の失敗と 429 / 503 の再試行、信頼した接続の読み取りの再試行は保つ。

MCP は `httpx.MockTransport` の合成のサーバー、モデルは SDK の `ScriptedModel`、保存先は memory。
外部への通信はしない。ツールの名前・内容は合成で、業務の内容は含まない。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import anyio
import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import LogCaptureFixture, MonkeyPatch

import app.features.agent.tools as tools_module
from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.config import McpConnectionConfig, runtime_config_store
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import (
    ExternalMcpToolInfo,
    ExternalToolError,
    JsonRpcResponse,
    ToolCall,
    ToolDefinition,
    ToolHandler,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicy,
    mcp_function_name,
    mcp_tool_definition,
    mcp_tool_handler,
    retry_after_delivery_allowed,
    tool_registry,
)
from app.features.agent.tools import _mcp_tools_from_result as _mcp_tools_from_result

USER_UUID = "11111111-2222-3333-4444-555555552596"
# モデルに渡す function tool の名前の規則（英数字・`_`・`-`、64 文字以内）。
_FUNCTION_NAME_RULE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _listing(*names: str, read_only: bool = True) -> JsonRpcResponse:
    return JsonRpcResponse(
        result={
            "tools": [
                {
                    "name": name,
                    "description": f"合成のツール {index}",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": read_only},
                }
                for index, name in enumerate(names)
            ]
        }
    )


def _names(server_id: str, *names: str) -> dict[str, str]:
    listed = _mcp_tools_from_result(server_id, _listing(*names))
    return {tool.name: str(tool.function_name) for tool in listed.tools}


class _SyntheticMcp:
    """合成の MCP サーバー（`httpx.MockTransport`）。`tools/call` の失敗を順に返す。"""

    def __init__(self, tools: list[str], *, read_only: bool = True) -> None:
        self.tools = tools
        self.read_only = read_only
        self.calls: list[str] = []
        # `tools/call` に順に返す失敗（int は HTTP status、例外はそのまま送出）。
        self.failures: list[int | Exception] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            return self._result(body, {"capabilities": {}})
        if method == "tools/list":
            listing = _listing(*self.tools, read_only=self.read_only).result
            return self._result(body, listing or {})
        assert method == "tools/call"
        name = body["params"]["name"]
        failure = self.failures.pop(0) if self.failures else None
        if isinstance(failure, httpx.ConnectError):
            # 送信前の失敗（呼び先に届いていない）。
            raise failure
        self.calls.append(name)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return httpx.Response(failure, text="合成の失敗")
        return self._result(body, {"structuredContent": {"tool": name, "ok": True}})

    @staticmethod
    def _result(body: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def synthetic(monkeypatch: MonkeyPatch) -> Iterator[_SyntheticMcp]:
    server = _SyntheticMcp(["read.item", "read_item"])
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(server.handle), timeout=kwargs["timeout"])

    monkeypatch.setattr("app.features.agent.tools.httpx.Client", client)
    # 再試行の待ちは記録だけ（待たない）。
    monkeypatch.setattr(tools_module, "_retry_sleep", lambda _seconds: None)
    yield server


@contextlib.contextmanager
def _connection(server_id: str, *, trusted: bool) -> Iterator[McpConnectionConfig]:
    config = runtime_config_store.upsert_mcp_server(
        server_id,
        base_url=f"https://{server_id}.example.test/mcp",
        auth_mode="none",
        trust_tool_annotations=trusted,
    )
    try:
        yield config
    finally:
        with contextlib.suppress(KeyError):
            runtime_config_store.remove_mcp_server(server_id)


# ---- #2596: 名前の衝突 ------------------------------------------------------------------------


def test_names_that_collide_after_replacement_are_kept_apart() -> None:
    names = _names("synthetic", "read.item", "read_item")

    # 置き換えの要らない名前は変えない（ツール権限・指示・保存済みの承認の名前を保つ）。
    assert names["read_item"] == "synthetic__read_item"
    # 置き換えた側だけに元の名前の hash を付ける。
    assert names["read.item"] == mcp_function_name("synthetic", "read.item", disambiguate=True)
    assert names["read.item"].startswith("synthetic__read_item_")
    assert names["read.item"] != names["read_item"]
    assert all(_FUNCTION_NAME_RULE.match(name) for name in names.values())
    # 一覧の順に依らない。
    assert _names("synthetic", "read_item", "read.item") == names
    # 衝突しなければ、置き換えた名前もそのまま（既存の名前を変えない）。
    assert _names("synthetic", "read.item") == {"read.item": "synthetic__read_item"}


@pytest.mark.parametrize(
    ("server_id", "tools"),
    [
        ("synthetic", ["x" * 70 + ".a", "x" * 70 + "_a"]),
        ("syn.thetic", ["read.item", "read_item", "read/item"]),
        ("syn__thetic", ["read.item", "read_item"]),
    ],
    ids=["long-names", "replaced-server-id", "separator-in-server-id"],
)
def test_disambiguated_names_follow_the_model_name_rules(server_id: str, tools: list[str]) -> None:
    names = _names(server_id, *tools)

    assert len(set(names.values())) == len(tools)
    assert all(_FUNCTION_NAME_RULE.match(name) for name in names.values())
    assert _names(server_id, *reversed(tools)) == names


def _definition(name: str, server_id: str | None, tool_name: str | None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="合成のツール",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        permission_level=ToolPermissionLevel.READ,
        mcp_server_id=server_id,
        mcp_tool_name=tool_name,
    )


def _handler(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
    return {"ok": True}


def test_conflicting_origins_are_not_exposed_and_are_reported(caplog: LogCaptureFixture) -> None:
    """別の出自のツールが同じ名前になったら、一覧の順でどちらかを選ばず、どれも渡さない。"""
    entries: list[tuple[ToolDefinition, ToolHandler]] = [
        (_definition("a__b__c", "a", "b__c"), _handler),
        (_definition("a__b__c", "a__b", "c"), _handler),
        (_definition("a__other", "a", "other"), _handler),
        # 同じ出自の再列挙は 1 つにまとめる（衝突ではない）。
        (_definition("a__other", "a", "other"), _handler),
    ]

    with caplog.at_level(logging.WARNING, logger="app.features.agent.builtin_runtime"):
        forward = builtin_runtime.build_function_tools("run_2596", [], entries)
        backward = builtin_runtime.build_function_tools("run_2596", [], entries[::-1])

    assert [tool.name for tool in forward] == ["a__other"]
    assert [tool.name for tool in backward] == ["a__other"]
    conflicts = [
        record for record in caplog.records if record.msg == "builtin_runtime_tool_name_conflict"
    ]
    assert conflicts and conflicts[0].__dict__["tool_name"] == "a__b__c"


@pytest.fixture
def skill_for() -> Iterator[Any]:
    created: list[str] = []

    def create(skill_id: str, server_id: str, tool_names: list[str]) -> str:
        skill_registry.upsert_custom(
            AgentSkillDefinition(
                id=skill_id,
                name="合成の Skill",
                instructions="read.item で読み、read_item で確かめる。",
                mcp_requirements=[SkillMcpRequirement(server_id=server_id, tool_names=tool_names)],
            )
        )
        created.append(skill_id)
        return skill_id

    yield create
    for skill_id in created:
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(skill_id)


def test_skill_allowlist_is_not_widened_by_a_colliding_name(
    synthetic: _SyntheticMcp, skill_for: Any
) -> None:
    """Skill が許可したツールだけを、元の名前で選ぶ（衝突する名前の別のツールは渡さない）。"""
    with _connection("test2596-allow", trusted=True):
        skill_id = skill_for("test2596-allow-skill", "test2596-allow", ["read.item"])
        tools, warnings = builtin_runtime.discover_mcp_tools(
            [skill_id], context=ToolInvocationContext(user_uuid=USER_UUID)
        )

    assert warnings == []
    assert [(item.mcp_tool_name, item.name) for item, _handler in tools] == [
        ("read.item", mcp_function_name("test2596-allow", "read.item", disambiguate=True))
    ]


def test_both_colliding_tools_reach_their_own_tool_in_a_run(
    monkeypatch: MonkeyPatch, synthetic: _SyntheticMcp, skill_for: Any
) -> None:
    """`read.item` と `read_item` の両方をモデルに渡し、呼んだ名前の元のツールに届く。"""
    server_id = "test2596-run"
    agent_id = "test2596-run-agent"
    dotted = mcp_function_name(server_id, "read.item", disambiguate=True)
    plain = f"{server_id}__read_item"
    model = ScriptedModel(
        [
            [function_call(dotted, "{}", call_id="call-dotted")],
            [function_call(plain, "{}", call_id="call-plain")],
            [assistant_message("確かめました。")],
        ]
    )
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
    with _connection(server_id, trusted=True):
        skill_id = skill_for("test2596-run-skill", server_id, ["read.item", "read_item"])
        runtime_repository.create_agent(
            AgentProfile(id=agent_id, name="合成の業務 Agent", skill_ids=[skill_id])
        )
        try:
            run = runtime_repository.create_builtin_run(
                RunCreateRequest(goal="合成の質問", agent_id=agent_id),
                created_by_user_uuid=USER_UUID,
            )
            anyio.run(builtin_runtime.execute_run, run.id)
            finished = runtime_repository.get_run(run.id)
        finally:
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)

    assert finished.status == RunStatus.COMPLETED, finished.events[-1].message
    exposed = {tool.name for tool in model.calls[0].tools}
    assert {dotted, plain} <= exposed
    # 呼んだ名前の元のツールに 1 回ずつ届いた。
    assert synthetic.calls == ["read.item", "read_item"]
    # 指示の素の名前は、それぞれの元のツールのモデル用の名前に書き直す。
    instructions = str(model.calls[0].system_instructions)
    assert f"{dotted} で読み、{plain} で確かめる。" in instructions


# ---- #2595: 注釈を信頼しない接続の再送 --------------------------------------------------------


def _read_only_tool(server_id: str) -> ExternalMcpToolInfo:
    return ExternalMcpToolInfo(
        name="read_item",
        server_id=server_id,
        read_only=True,
        function_name=mcp_function_name(server_id, "read_item"),
    )


def _call(config: McpConnectionConfig, tool: ExternalMcpToolInfo) -> dict[str, Any]:
    handler = mcp_tool_handler(config, tool)
    result = handler({}, ToolInvocationContext(user_uuid=USER_UUID))
    # 画像の無い結果は文字の結果（JSON）のまま返る（#2650）。
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize(
    "failure",
    [502, 504, "read_error"],
    ids=["502", "504", "disconnect-after-send"],
)
def test_untrusted_read_only_hint_does_not_resend_after_delivery(
    synthetic: _SyntheticMcp, failure: int | str
) -> None:
    synthetic.failures.append(
        httpx.ReadError("合成の切断") if failure == "read_error" else int(failure)
    )
    with _connection("test2595-untrusted", trusted=False) as config:
        tool = _read_only_tool(config.server_id)
        assert config.annotations_trusted() is False
        assert retry_after_delivery_allowed(config, tool) is False
        with pytest.raises(ExternalToolError) as raised:
            _call(config, tool)

    assert synthetic.calls == ["read_item"]
    assert raised.value.details["attempts"] == 1
    assert raised.value.details["retryable"] is False


def test_explicit_allow_does_not_make_an_untrusted_tool_resendable(
    synthetic: _SyntheticMcp,
) -> None:
    """ツール権限で許可しても、承認の記録があっても、送信後の失敗は再送しない。"""
    synthetic.failures.append(502)
    with _connection("test2595-allowed", trusted=False) as config:
        tool = _read_only_tool(config.server_id)
        definition = mcp_tool_definition(config, tool)
        result = tool_registry.invoke(
            ToolCall(name=definition.name, arguments={}),
            policy=ToolPolicy(allow={definition.name}),
            context=ToolInvocationContext(user_uuid=USER_UUID, approval_id="approval-2595"),
            definition=definition,
            handler=mcp_tool_handler(config, tool),
        )

    assert result.success is False
    assert result.error_code == "mcp.http_error"
    assert synthetic.calls == ["read_item"]


@pytest.mark.parametrize(
    "failure",
    [503, 429, "connect"],
    ids=["503", "429", "connect-before-send"],
)
def test_untrusted_tools_still_retry_failures_before_acceptance(
    synthetic: _SyntheticMcp, failure: int | str
) -> None:
    """送っていない・受け付ける前に断られた失敗は、注釈の信頼に依らず再試行する。"""
    synthetic.failures.append(
        httpx.ConnectError("合成の接続の失敗") if failure == "connect" else int(failure)
    )
    with _connection("test2595-before", trusted=False) as config:
        output = _call(config, _read_only_tool(config.server_id))

    assert output["ok"] is True
    # 送信前の失敗は呼び先に届いていない（サーバーの記録は届いた分だけ）。
    expected = ["read_item"] if failure == "connect" else ["read_item", "read_item"]
    assert synthetic.calls == expected


@pytest.mark.parametrize("failure", [502, 504], ids=["502", "504"])
def test_trusted_read_only_tools_keep_retrying_gateway_errors(
    synthetic: _SyntheticMcp, failure: int
) -> None:
    synthetic.failures.append(failure)
    with _connection("test2595-trusted", trusted=True) as config:
        tool = _read_only_tool(config.server_id)
        assert retry_after_delivery_allowed(config, tool) is True
        output = _call(config, tool)

    assert output["ok"] is True
    assert synthetic.calls == ["read_item", "read_item"]


def test_write_tools_are_not_resent_even_on_a_trusted_connection(
    synthetic: _SyntheticMcp,
) -> None:
    synthetic.failures.append(502)
    with _connection("test2595-write", trusted=True) as config:
        tool = _read_only_tool(config.server_id).model_copy(update={"read_only": False})
        with pytest.raises(ExternalToolError):
            _call(config, tool)

    assert synthetic.calls == ["read_item"]
