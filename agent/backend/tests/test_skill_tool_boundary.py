"""業務 Agent → スキル → ツールの一方向の依存と、宣言の閉包の外の呼び出しの決定論テスト（#3240）。

- 業務 Agent はツールを直接持たない: 作成・変更の API は直接の指定を 422 で拒み、保存済みの旧い値
  （`tool_names`）は読むときに無視し、Run に渡すツールはスキルの宣言からだけ決まる。
- ツールの層（`tools.py` とそこから import する module）はスキルを import しない。スキルの追加・
  変更・削除は、ツールの登録と MCP 接続の提供を変えない。
- 必須のツールが接続に無ければ、交差集合で黙って続けず、モデルを呼ぶ前に Run を失敗にする。
- 接続のすべてのツール（`tool_names` が空）の露出の棚卸し: 組み込みのスキルは明示の一覧だけを使い、
  接続のすべてを使うスキルでは、接続に足したツールも渡るが承認の要否は今のツール権限で決まる。
- モデルに渡していない名前の呼び出しは実行せず、理由つきで Run のイベントに残す。

モデルは SDK の `ScriptedModel`、ツールは合成の handler、RAG / NL2SQL は契約どおりの fake の MCP
（`mcp_support`）、保存先は memory。外部への通信はしない。
"""

from __future__ import annotations

import ast
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from mcp_support import Nl2SqlGetJobIn, fake_product_mcp
from pr_backend_core.mcp import McpTool
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import (
    UNDECLARED_CALL_DECLINED,
    UNDECLARED_CALL_NAME_CONFLICT,
    UNDECLARED_CALL_NAME_MISMATCH,
    UNDECLARED_CALL_NOT_DECLARED,
    UNDECLARED_CALL_POLICY_DENIED,
    UNDECLARED_CALL_PREVIOUS_CONTEXT,
    UNDECLARED_CALL_REASONS,
    UNDECLARED_CALL_UNKNOWN,
    ModelTarget,
    classify_undeclared_call,
)
from app.features.agent.config import runtime_config_store
from app.features.agent.confirmation import CONFIRMATION_TOOL
from app.features.agent.runtime import (
    DIRECT_TOOL_FIELDS,
    AgentProfile,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import (
    BUILTIN_SKILL_IDS,
    AgentSkillDefinition,
    SkillMcpRequirement,
    skill_registry,
)
from app.features.agent.tools import (
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicyDecision,
    tool_registry,
)
from app.main import app

READ = "test3240_read"
WRITE = "test3240_write"
SKILL_ID = "test3240-skill"
AGENT_ID = "test3240-agent"
USER_UUID = "11111111-2222-3333-4444-555555553240"
APP_DIR = Path(__file__).resolve().parents[1] / "app"

client = TestClient(app)


class _Calls:
    def __init__(self) -> None:
        self.names: list[str] = []

    def handler(self, name: str) -> Any:
        def handle(arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
            self.names.append(name)
            return {"name": name, "ok": True}

        return handle


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "additionalProperties": False,
    }


def _cleanup_runs(agent_id: str) -> None:
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - テストの後始末
        for run_id in [
            run_id for run_id, run in repository._runs.items() if run.agent_id == agent_id
        ]:
            run = repository._runs.pop(run_id)
            for approval in run.approvals:
                repository._approvals.pop(approval.id, None)
    with contextlib.suppress(KeyError, ValueError):
        runtime_repository.delete_agent(agent_id)


@pytest.fixture
def calls() -> Iterator[_Calls]:
    """スキルは READ だけを宣言する（WRITE は登録済みだが宣言しない Control Plane のツール）。"""
    recorded = _Calls()
    original = runtime_config_store.get_tool_policy()
    for name, level in ((READ, ToolPermissionLevel.READ), (WRITE, ToolPermissionLevel.WRITE)):
        tool_registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} の合成のツール",
                input_schema=_schema(),
                output_schema={"type": "object"},
                permission_level=level,
                side_effects=level == ToolPermissionLevel.WRITE,
            ),
            recorded.handler(name),
        )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="宣言のテストの Skill",
            instructions="件数を読む。",
            mcp_requirements=[SkillMcpRequirement(server_id="control-plane", tool_names=[READ])],
        )
    )
    # 書き込みのツールも自動実行にしておく（露出は権限ではなく宣言で決まることを確かめる）。
    runtime_config_store.patch_tool_policy(allow=[READ, WRITE], ask=[], deny=[])
    try:
        yield recorded
    finally:
        runtime_config_store.patch_tool_policy(
            default_mode=original.default_mode,
            allow=sorted(original.allow),
            ask=sorted(original.ask),
            deny=sorted(original.deny),
        )
        _cleanup_runs(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        for name in (READ, WRITE):
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


def _run(agent_id: str = AGENT_ID) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="件数を確かめて", agent_id=agent_id), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return run.id


def _declared_tools(model: ScriptedModel, call_index: int) -> list[str]:
    """モデルに渡したツールのうち、スキルの宣言から来たもの（実行環境が足す回答前の確認を除く）。"""
    return [tool.name for tool in model.calls[call_index].tools if tool.name != CONFIRMATION_TOOL]


def _undeclared(run_id: str) -> dict[str, str]:
    """Run のイベントに残した、渡していない名前の呼び出し（名前 → 理由）。"""
    return {
        event.payload["undeclared_tool_call"]["tool_name"]: event.payload["undeclared_tool_call"][
            "reason"
        ]
        for event in runtime_repository.get_run(run_id).events
        if "undeclared_tool_call" in event.payload
    }


# ---- 業務 Agent はツールを直接持たない ----------------------------------------------------------


@pytest.mark.parametrize("field", DIRECT_TOOL_FIELDS)
def test_agent_api_rejects_direct_tool_fields(field: str) -> None:
    """作成・変更の API はツールの直接の指定を黙って捨てずに 422 で拒む（未対応を成功にしない）。"""
    agent_id = f"test3240-api-{field.replace('_', '-')}"
    created = client.post(
        "/api/agents", json={"id": agent_id, "name": "直接の指定", field: ["echo"]}
    )
    assert created.status_code == 422, created.text
    assert "スキル" in str(created.json())
    assert agent_id not in {agent.id for agent in runtime_repository.list_agents()}

    try:
        created = client.post("/api/agents", json={"id": agent_id, "name": "宣言だけ"})
        assert created.status_code == 200
        patched = client.patch(f"/api/agents/{agent_id}", json={field: ["echo"]})
        assert patched.status_code == 422, patched.text
        [current] = [agent for agent in runtime_repository.list_agents() if agent.id == agent_id]
        assert field not in current.model_dump()
    finally:
        client.request("DELETE", f"/api/agents/{agent_id}")


def test_stored_tool_fields_are_ignored_on_read() -> None:
    """保存済みの旧い項目（`tool_names`・`migration_required`）は読むときに無視する（互換を持たない）。"""
    stored = {
        "id": "test3240-stored",
        "name": "保存済みの業務 Agent",
        "skill_ids": [SKILL_ID],
        "tool_names": [WRITE, "echo"],
        "migration_required": True,
        "enabled": True,
    }
    agent = AgentProfile.model_validate(stored)
    dumped = agent.model_dump()
    assert "tool_names" not in dumped
    assert "migration_required" not in dumped
    assert agent.skill_ids == [SKILL_ID]
    assert agent.enabled is True


def test_directly_listed_tool_is_not_exposed_to_the_run(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """保存済みの記録がツールを直接並べていても、Run に渡すのはスキルが宣言したツールだけ。

    モデルが宣言の外のツールを名前で呼んでも実行せず、理由（スキルが宣言していない）を Run に残す。
    """
    runtime_repository.create_agent(
        AgentProfile.model_validate(
            {
                "id": AGENT_ID,
                "name": "直接の指定の残った業務 Agent",
                "skill_ids": [SKILL_ID],
                "tool_names": [WRITE, "echo"],
            }
        )
    )
    model = _script(
        monkeypatch,
        [
            function_call(WRITE, {"count": 1}, call_id="call-write"),
            function_call("echo", {"value": 1}, call_id="call-echo"),
        ],
        [assistant_message("書き込みのツールは使えませんでした。")],
    )

    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert _declared_tools(model, 0) == [READ]
    assert calls.names == []
    assert run.steps == []
    assert _undeclared(run_id) == {
        WRITE: UNDECLARED_CALL_NOT_DECLARED,
        "echo": UNDECLARED_CALL_NOT_DECLARED,
    }
    # モデルには実行しなかったことと、呼べる名前を返す（読み替えて実行しない。#1303）。
    second = str(model.calls[1].input)
    assert f"ツール「{WRITE}」はありません" in second
    assert READ in second


# ---- ツールの層はスキルに依存しない --------------------------------------------------------------

# スキルの定義・一覧・参照文書・書き出し・業務 Agent を扱う module（ツールの層から import しない）。
_SKILL_SIDE_MODULES = frozenset(
    {
        "app.features.agent.skills",
        "app.features.agent.skills_loader",
        "app.features.agent.skill_references",
        "app.features.agent.skill_export",
        "app.features.agent.skill_updates",
        "app.features.agent.templates",
        "app.features.agent.drafting",
        "app.features.agent.builtin_runtime",
        "app.features.agent.runtime",
        "app.features.agent.router",
        "app.features.agent.mcp_server",
    }
)
# ツールの層の入口（ツールの登録・MCP 接続の呼び出し・ツール権限の判定）。
_TOOL_LAYER_ENTRY = "app.features.agent.tools"


def _module_path(module: str) -> Path | None:
    relative = Path(*module.split(".")[1:])
    for candidate in (APP_DIR / f"{relative}.py", APP_DIR / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _app_imports(module: str) -> set[str]:
    """module が import する `app.` の module（関数の中の import も含む）。"""
    path = _module_path(module)
    if path is None:
        return set()
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("app."))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            for alias in node.names:
                child = f"{node.module}.{alias.name}"
                found.add(child if _module_path(child) is not None else node.module)
    return found


def test_tool_layer_does_not_import_skills() -> None:
    """`tools.py` から辿れる module に、スキル・業務 Agent の側の module が無い（逆依存しない）。"""
    seen: set[str] = set()
    pending = [_TOOL_LAYER_ENTRY]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_app_imports(module) - seen)
    assert seen & _SKILL_SIDE_MODULES == set()
    # 走査が空振りしていない（ツールの層の module を実際に辿っている）。
    assert {"app.features.agent.config", "app.features.agent.tool_arguments"} <= seen


def test_skill_changes_do_not_change_tool_provision(calls: _Calls) -> None:
    """スキルの追加・変更・削除は、ツールの登録・MCP 接続の一覧・ツール権限を変えない。"""
    del calls
    before = (
        sorted(tool_registry.names()),
        [config.model_dump() for config in runtime_config_store.list_mcp_servers()],
        runtime_config_store.get_tool_policy().model_dump(),
    )
    extra = "test3240-extra-skill"
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=extra,
            name="追加のスキル",
            mcp_requirements=[SkillMcpRequirement(server_id="control-plane", tool_names=[WRITE])],
        )
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(id=extra, name="変えたスキル", mcp_requirements=[])
    )
    skill_registry.remove(extra)
    skill_registry.remove(SKILL_ID)
    after = (
        sorted(tool_registry.names()),
        [config.model_dump() for config in runtime_config_store.list_mcp_servers()],
        runtime_config_store.get_tool_policy().model_dump(),
    )
    assert after == before


# ---- 必須のツールの不足 --------------------------------------------------------------------------


def test_missing_required_tool_fails_the_run_instead_of_intersecting(
    monkeypatch: MonkeyPatch,
) -> None:
    """スキルが明示したツールが接続に無ければ、ある分だけで続けず、モデルを呼ぶ前に失敗にする。"""
    mcp = fake_product_mcp(monkeypatch)
    skill_id = "test3240-missing-skill"
    agent_id = "test3240-missing-agent"
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=skill_id,
            name="必須のツールの不足",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search", "rag_not_listed"])
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=agent_id, name="不足の業務 Agent", skill_ids=[skill_id])
    )
    model = _script(monkeypatch, [assistant_message("ある分だけで答えました。")])
    try:
        tools, warnings = builtin_runtime.discover_mcp_tools(
            [skill_id], context=ToolInvocationContext(user_uuid=USER_UUID)
        )
        run_id = _run(agent_id)
        run = runtime_repository.get_run(run_id)
    finally:
        _cleanup_runs(agent_id)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(skill_id)

    # 一覧の取得は、ある分を返しつつ不足を理由として返す（呼び出し側が失敗にする）。
    assert [definition.mcp_tool_name for definition, _handler in tools] == ["rag_search"]
    assert len(warnings) == 1 and "必須ツール" in warnings[0]
    assert run.status == RunStatus.FAILED
    assert run.events[-1].payload["error_code"] == "runtime.mcp_unavailable"
    assert not model.calls
    assert mcp.calls_of("rag_search") == []


# ---- 接続のすべてのツールの露出の棚卸し ---------------------------------------------------------


def test_builtin_skills_declare_explicit_tool_lists() -> None:
    """組み込みのスキルは、接続のすべて（`tool_names` が空）を使わず、明示の一覧だけを宣言する。

    接続のすべてを使うのは利用者が作ったスキルだけ（接続にツールを足すと露出が増える。docs の
    agent/docs/agents/runtime.md「接続のすべてのツールの露出」）。
    """
    for skill_id in BUILTIN_SKILL_IDS:
        skill = skill_registry.get(skill_id)
        assert skill is not None, skill_id
        assert skill.mcp_requirements, skill_id
        for requirement in skill.mcp_requirements:
            assert requirement.tool_names, (skill_id, requirement.server_id)


def test_whole_connection_exposes_added_tools_under_the_current_policy(
    monkeypatch: MonkeyPatch,
) -> None:
    """接続のすべてを使うスキルでは、接続に後から足したツールも渡るが、承認の要否は今のツール権限。

    書き込み（readOnlyHint=false）のツールは、標準の自動実行の一覧に無ければ承認が要る。スキルの
    宣言は承認を省く根拠にならない。
    """
    mcp = fake_product_mcp(monkeypatch)
    added = "nl2sql_test3240_write"
    mcp.servers["nl2sql"].tools[added] = McpTool(
        name=added,
        description="後から足した書き込みのツール",
        input_model=Nl2SqlGetJobIn,
        handler=lambda _argument: {"ok": True},
        read_only=False,
    )
    skill_id = "test3240-whole-skill"
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=skill_id,
            name="接続のすべて",
            mcp_requirements=[SkillMcpRequirement(server_id="nl2sql")],
        )
    )
    try:
        tools, warnings = builtin_runtime.discover_mcp_tools(
            [skill_id], context=ToolInvocationContext(user_uuid=USER_UUID)
        )
    finally:
        skill_registry.remove(skill_id)

    assert warnings == []
    definitions = {definition.mcp_tool_name: definition for definition, _handler in tools}
    assert added in definitions
    policy = builtin_runtime._active_policy()  # noqa: SLF001 - Run と同じツール権限の判定
    assert policy.decide(definitions[added]) == ToolPolicyDecision.ASK
    assert mcp.calls_of(added) == []


# ---- 宣言の閉包の外の呼び出しの理由別の監査 -----------------------------------------------------


@pytest.mark.parametrize(
    ("called", "kwargs", "reason"),
    [
        (
            "rag_search",
            {"exposed": ["rag__rag_search"]},
            UNDECLARED_CALL_NAME_MISMATCH,
        ),
        (
            "test_write",
            {"excluded": {"test_write": UNDECLARED_CALL_POLICY_DENIED}},
            UNDECLARED_CALL_POLICY_DENIED,
        ),
        (
            "nl2sql__nl2sql_query",
            {"excluded": {"nl2sql__nl2sql_query": UNDECLARED_CALL_DECLINED}},
            UNDECLARED_CALL_DECLINED,
        ),
        (
            "a__b__read",
            {"excluded": {"a__b__read": UNDECLARED_CALL_NAME_CONFLICT}},
            UNDECLARED_CALL_NAME_CONFLICT,
        ),
        (
            "rag__rag_read_source",
            {"previous_names": ["rag__rag_read_source"], "registered_connections": ["rag"]},
            UNDECLARED_CALL_PREVIOUS_CONTEXT,
        ),
        (
            "erp__lookup_order",
            {"registered_connections": ["erp"]},
            UNDECLARED_CALL_NOT_DECLARED,
        ),
        ("echo", {"registered_tools": ["echo"]}, UNDECLARED_CALL_NOT_DECLARED),
        ("other__delete_everything", {"registered_connections": ["rag"]}, UNDECLARED_CALL_UNKNOWN),
    ],
    ids=[
        "name_mismatch",
        "policy_denied",
        "declined",
        "name_conflict",
        "previous_context",
        "not_declared_connection",
        "not_declared_control_plane",
        "unknown",
    ],
)
def test_undeclared_call_reason(called: str, kwargs: dict[str, Any], reason: str) -> None:
    arguments: dict[str, Any] = {
        "exposed": [],
        "excluded": {},
        "previous_names": [],
        "registered_connections": [],
        "registered_tools": [],
        **kwargs,
    }
    exposed = arguments.pop("exposed")
    assert classify_undeclared_call(called, exposed, **arguments) == reason
    assert reason in UNDECLARED_CALL_REASONS


def test_undeclared_calls_are_recorded_with_reasons_and_not_run(
    monkeypatch: MonkeyPatch, calls: _Calls
) -> None:
    """1 つの応答の宣言の外の呼び出しを、どれも実行せず、理由ごとに Run のイベントに残す。"""
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="宣言のテストの Skill",
            instructions="件数を読んで書く。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="control-plane", tool_names=[READ, WRITE])
            ],
        )
    )
    runtime_config_store.patch_tool_policy(allow=[READ], ask=[], deny=[WRITE])
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="監査の業務 Agent", skill_ids=[SKILL_ID])
    )
    model = _script(
        monkeypatch,
        [
            function_call(WRITE, {"count": 1}, call_id="call-denied"),
            function_call(f"other__{READ}", {"count": 1}, call_id="call-mismatch"),
            function_call("echo", {"value": 1}, call_id="call-not-declared"),
            function_call("test3240_made_up", {}, call_id="call-unknown"),
        ],
        [assistant_message("使えるツールだけで確かめました。")],
    )

    run_id = _run()

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert _declared_tools(model, 0) == [READ]
    assert calls.names == []
    assert _undeclared(run_id) == {
        WRITE: UNDECLARED_CALL_POLICY_DENIED,
        f"other__{READ}": UNDECLARED_CALL_NAME_MISMATCH,
        "echo": UNDECLARED_CALL_NOT_DECLARED,
        "test3240_made_up": UNDECLARED_CALL_UNKNOWN,
    }
    events = [event for event in run.events if "undeclared_tool_call" in event.payload]
    assert {event.payload["undeclared_tool_call"]["call_id"] for event in events} == {
        "call-denied",
        "call-mismatch",
        "call-not-declared",
        "call-unknown",
    }
    assert all(event.payload["severity"] == "warning" for event in events)
    # 検知しても渡すツールは広げない（次のモデルの呼び出しも同じツールだけ）。
    assert _declared_tools(model, 1) == [READ]
