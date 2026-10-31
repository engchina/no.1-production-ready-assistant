"""ツール権限の画面のツールの一覧（Control Plane のツールと MCP 接続のツール。#1814）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp_support import fake_product_mcp
from pytest import MonkeyPatch
from security_support import client

from app.features.agent.config import McpConnectionConfig, runtime_config_store
from app.features.agent.control_plane_store import FileItemStore, set_control_plane_store
from app.features.agent.tools import (
    ExternalMcpToolInfo,
    ExternalToolError,
    ToolDefinition,
    ToolPolicy,
    ToolPolicyDecision,
    list_mcp_connection_tools,
    mcp_tool_definition,
)


@pytest.fixture
def restore_policy() -> Iterator[None]:
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


def _tools(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in data["tools"]}


def test_lists_mcp_connection_tools_with_the_default_decision(
    monkeypatch: MonkeyPatch, restore_policy: None
) -> None:
    """MCP 接続のツールも並べ、個別に決めていないときの判定（Run と同じ）を付ける。"""
    del restore_policy
    fake_product_mcp(monkeypatch)
    # 個別のポリシーは「既定に従う」の判定（default_decision）を変えない。
    runtime_config_store.patch_tool_policy(allow=["nl2sql__nl2sql_query"])

    response = client.get("/api/settings/tool-policy/tools")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    tools = _tools(data)
    # Control Plane のツール。
    assert tools["echo"]["server_id"] == "control-plane"
    assert tools["echo"]["default_decision"] == "allow"
    assert tools["echo"]["default_reason"] == "read_only"
    # NL2SQL の標準の接続: nl2sql_query は readOnlyHint=false でも既定で自動実行（#1832）。
    query = tools["nl2sql__nl2sql_query"]
    assert (query["server_id"], query["default_decision"], query["available"]) == (
        "nl2sql",
        "allow",
        True,
    )
    assert query["server_label"] == "データ問い合わせ（NL2SQL）"
    assert query["permission_level"] == "write" and query["side_effects"] is True
    assert query["default_reason"] == "standard"
    # それ以外の readOnlyHint=false のツールは既定で承認が要る。
    cancel = tools["nl2sql__nl2sql_cancel_requirement"]
    assert (cancel["default_decision"], cancel["default_reason"]) == ("ask", "default_mode")
    assert tools["nl2sql__nl2sql_get_job"]["default_decision"] == "allow"
    # RAG の標準の接続は読み取り専用（承認なし）。
    assert tools["rag__rag_search"]["default_decision"] == "allow"
    connections = {item["server_id"]: item for item in data["connections"]}
    assert connections["nl2sql"] == {
        "server_id": "nl2sql",
        "label": "データ問い合わせ（NL2SQL）",
        "configured": True,
        # 標準の接続はツールの注釈を常に使う（#1832）。
        "trust_tool_annotations": True,
        "tool_count": 6,
        "error": None,
    }


def test_default_mode_deny_changes_the_default_decision(
    monkeypatch: MonkeyPatch, restore_policy: None
) -> None:
    del restore_policy
    fake_product_mcp(monkeypatch)
    runtime_config_store.patch_tool_policy(default_mode="deny")

    tools = _tools(client.get("/api/settings/tool-policy/tools").json()["data"])

    assert tools["nl2sql__nl2sql_cancel_requirement"]["default_decision"] == "deny"
    assert tools["nl2sql__nl2sql_query"]["default_decision"] == "allow"
    assert tools["rag__rag_search"]["default_decision"] == "allow"


def test_unreachable_connection_reports_the_reason_and_keeps_saved_policies(
    monkeypatch: MonkeyPatch, restore_policy: None
) -> None:
    """一覧を取れない接続は理由を返し、その接続の保存したポリシーは一覧に無い行として残す。"""
    del restore_policy
    fake_product_mcp(monkeypatch)
    servers = {config.server_id: config for config in runtime_config_store.list_mcp_servers()}
    # 呼び先が応答しない外部の接続（一覧を取れない）と、URL を設定していない接続（失敗にしない）。
    servers["crm1814"] = servers["nl2sql"].model_copy(
        update={"server_id": "crm1814", "label": "CRM", "base_url": "https://crm.test/mcp"}
    )
    servers["blank1814"] = servers["nl2sql"].model_copy(
        update={"server_id": "blank1814", "label": "未設定", "base_url": None}
    )
    monkeypatch.setattr(runtime_config_store, "_mcp_servers", servers)
    original = list_mcp_connection_tools

    def list_tools(server_id: str, *, context: Any) -> Any:
        if server_id == "crm1814":
            raise ExternalToolError("mcp.http_error", "MCP 接続「CRM」に接続できません。")
        return original(server_id, context=context)

    monkeypatch.setattr(
        "app.features.agent.tool_policy_catalog.list_mcp_connection_tools", list_tools
    )
    runtime_config_store.patch_tool_policy(deny=["crm1814__delete_customer"])

    data = client.get("/api/settings/tool-policy/tools").json()["data"]

    connections = {item["server_id"]: item for item in data["connections"]}
    assert connections["crm1814"]["tool_count"] == 0
    assert connections["crm1814"]["error"] == "MCP 接続「CRM」に接続できません。"
    assert (connections["blank1814"]["configured"], connections["blank1814"]["error"]) == (
        False,
        None,
    )
    assert connections["rag"]["error"] is None
    stale = _tools(data)["crm1814__delete_customer"]
    assert stale == {
        "name": "crm1814__delete_customer",
        "description": "",
        "server_id": "crm1814",
        "server_label": "CRM",
        "permission_level": None,
        "side_effects": None,
        "default_decision": None,
        "default_reason": None,
        "available": False,
    }


def _external_tool(config: McpConnectionConfig) -> ToolDefinition:
    """読み取り専用の注釈（readOnlyHint=true）を返す外部の MCP のツール。"""
    return mcp_tool_definition(
        config,
        ExternalMcpToolInfo(
            name="read_wiki_structure",
            server_id=config.server_id,
            read_only=True,
            function_name=f"{config.server_id}__read_wiki_structure",
        ),
    )


@pytest.mark.parametrize(
    ("source", "trust", "decision", "reason"),
    [
        ("runtime", False, ToolPolicyDecision.ASK, "untrusted_annotation"),
        ("runtime", True, ToolPolicyDecision.ALLOW, "read_only"),
        ("env", False, ToolPolicyDecision.ASK, "untrusted_annotation"),
        # 標準の接続（RAG / NL2SQL）は注釈を常に使う（既定を変えない）。
        ("builtin", False, ToolPolicyDecision.ALLOW, "read_only"),
    ],
    ids=["external-untrusted", "external-trusted", "declared-untrusted", "builtin"],
)
def test_read_only_hint_is_used_only_for_trusted_connections(
    source: str, trust: bool, decision: ToolPolicyDecision, reason: str
) -> None:
    """注釈を信頼しない接続では readOnlyHint=true でも既定の動作にする（#1832）。"""
    config = McpConnectionConfig(server_id="wiki1832", source=source, trust_tool_annotations=trust)
    definition = _external_tool(config)

    assert ToolPolicy().explain(definition) == (decision, reason)
    # 既定の動作が拒否なら、信頼しない接続のツールは拒否（モデルに渡さない）。
    if decision == ToolPolicyDecision.ASK:
        assert ToolPolicy(default_mode="deny").decide(definition) == ToolPolicyDecision.DENY
    # ツール権限の個別の指定が優先する。
    assert ToolPolicy(allow={definition.name}).explain(definition) == (
        ToolPolicyDecision.ALLOW,
        "policy",
    )


def test_trust_tool_annotations_is_saved_per_connection(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """「ツールの注釈を信頼する」は外部の接続では既定でオフ、接続ごとにオンにでき、標準の接続は
    常にオンでオフにできない（#1832・#1875）。"""
    set_control_plane_store(FileItemStore(tmp_path / "items.json"))
    monkeypatch.setattr(
        runtime_config_store, "_mcp_servers", dict(runtime_config_store._mcp_servers)
    )  # noqa: SLF001 - 接続の一覧を test の後に戻す
    try:
        created = client.post(
            "/api/settings/mcp-connections",
            json={"server_id": "wiki1875", "base_url": "https://wiki.example.test/mcp"},
        )
        assert created.status_code == 200, created.text
        # 新しい外部の接続はオフで始める（#1875）。
        assert created.json()["data"]["trust_tool_annotations"] is False
        assert runtime_config_store.get_mcp("wiki1875").annotations_trusted() is False
        patched = client.patch(
            "/api/settings/mcp-connections/wiki1875", json={"trust_tool_annotations": True}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["trust_tool_annotations"] is True
        assert runtime_config_store.get_mcp("wiki1875").annotations_trusted() is True
        # ほかの項目だけの変更では、保存した信頼を変えない。
        relabeled = client.patch("/api/settings/mcp-connections/wiki1875", json={"label": "Wiki"})
        assert relabeled.json()["data"]["trust_tool_annotations"] is True
        turned_off = client.patch(
            "/api/settings/mcp-connections/wiki1875", json={"trust_tool_annotations": False}
        )
        assert turned_off.json()["data"]["trust_tool_annotations"] is False
        assert runtime_config_store.get_mcp("wiki1875").annotations_trusted() is False

        builtin = client.patch(
            "/api/settings/mcp-connections/nl2sql", json={"trust_tool_annotations": False}
        )
        assert builtin.status_code == 400
        listed = client.get("/api/settings/mcp-connections").json()["data"]["connections"]
        assert {item["server_id"]: item["trust_tool_annotations"] for item in listed}[
            "nl2sql"
        ] is True
    finally:
        set_control_plane_store(None)


@pytest.mark.parametrize(
    ("source", "trusted"),
    [("runtime", False), ("env", False), ("builtin", True)],
    ids=["external", "declared", "builtin"],
)
def test_stored_connection_without_trust_value_is_untrusted_unless_builtin(
    monkeypatch: MonkeyPatch, source: str, trusted: bool
) -> None:
    """値を保存していない既存の外部の接続はオフ、標準の接続は常にオン（#1875。互換は持たない）。"""
    servers = (
        {}
        if source == "runtime"
        else {"old1875": McpConnectionConfig(server_id="old1875", source=source)}
    )
    monkeypatch.setattr(runtime_config_store, "_mcp_servers", servers)  # noqa: SLF001 - 接続の一覧を差し替える
    # #1832 より前に保存した接続（`trust_tool_annotations` の無い文書）を復元する。
    runtime_config_store.restore_mcp_server(
        McpConnectionConfig.model_validate({"server_id": "old1875"})
    )

    restored = runtime_config_store.get_mcp("old1875")
    assert restored is not None
    assert restored.annotations_trusted() is trusted
    assert (
        ToolPolicy().explain(_external_tool(restored))[0] == ToolPolicyDecision.ALLOW
    ) is trusted


def test_connection_tools_show_the_default_decision(monkeypatch: MonkeyPatch) -> None:
    """接続のツールの一覧にも「既定に従う」の判定を付ける（nl2sql_query は自動実行。#1832）。"""
    fake_product_mcp(monkeypatch)

    response = client.get("/api/settings/mcp-connections/nl2sql/tools")

    assert response.status_code == 200, response.text
    tools = response.json()["data"]["tools"]
    decisions = {tool["name"]: tool["default_decision"] for tool in tools}
    assert decisions["nl2sql_query"] == "allow"
    assert decisions["nl2sql_cancel_requirement"] == "ask"
    assert decisions["nl2sql_get_job"] == "allow"
