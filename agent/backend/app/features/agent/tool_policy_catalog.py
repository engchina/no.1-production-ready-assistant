"""ツール権限の画面のツールの一覧（Control Plane のツールと MCP 接続のツール。#1814）。

ツール権限（`ToolPolicy`）は `<接続>__<ツール>` の名前で MCP 接続のツールにも効くが、MCP 接続の
ツールは `tool_registry` に登録せず Run ごとに `tools/list` で取るため、画面の一覧に出ていなかった。
ここでは Control Plane のツールに、登録した MCP 接続の `tools/list`（ログイン中の利用者として
取る）を足し、ポリシーで個別に決めていないときの判定（`default_decision`。Run と同じ
`ToolPolicy.decide`）を付ける。

- URL を設定していない接続は取らない（`configured=False`。失敗にしない）。
- 一覧を取れない接続は `connections[].error` に理由を出し、その接続のツールで保存済みのポリシーは
  `available=False` の行として残す（接続が止まっていても、保存した許可・承認・拒否を消さない）。
- 既定の判定は変えない（MCP のツールは `readOnlyHint=true` なら承認なし、それ以外は既定の動作）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from app.features.agent.config import McpConnectionConfig, runtime_config_store
from app.features.agent.tools import (
    MCP_TOOL_SEPARATOR,
    ExternalToolError,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicy,
    ToolPolicyDecision,
    list_mcp_connection_tools,
    mcp_tool_definition,
    tool_registry,
)

# Control Plane のツール（`tool_registry`）の出所（Skill の requirement の `server_id` と同じ値）。
CONTROL_PLANE_SOURCE = "control-plane"
# 接続の一覧を同時に取る数の上限（接続ごとの timeout は接続の設定）。
_MAX_PARALLEL_CONNECTIONS = 8


class ToolPolicyTool(BaseModel):
    name: str
    description: str = ""
    # `control-plane`（組み込みのツール）か、MCP 接続の ID。
    server_id: str
    server_label: str = ""
    permission_level: ToolPermissionLevel | None = None
    side_effects: bool | None = None
    # ポリシーで個別に決めていないときの判定（Run と同じ `ToolPolicy.decide`）。不明なら None。
    default_decision: ToolPolicyDecision | None = None
    # その判定の理由（#1832）: `standard`（標準の接続で Agent が承認なしと決めたツール）/
    # `read_only`（読み取り専用の宣言。MCP は信頼した接続の readOnlyHint）/
    # `untrusted_annotation`（readOnlyHint はあるが接続が注釈を信頼しない）/ `default_mode`。
    default_reason: str | None = None
    # False は一覧に無い（接続を取れない・ツールが無くなった）が、保存したポリシーにある名前。
    available: bool = True


class ToolPolicyConnection(BaseModel):
    server_id: str
    label: str
    # False は URL を設定していない（ローカルの開発・その製品を配備しない構成）。一覧は取らない。
    configured: bool = True
    # ツールの注釈（readOnlyHint）を既定の判定に使うか（#1832。標準の接続は常に True）。
    trust_tool_annotations: bool = False
    tool_count: int = 0
    # 一覧を取れなかった理由（取れた・URL が未設定なら None）。
    error: str | None = None


class ToolPolicyToolsData(BaseModel):
    tools: list[ToolPolicyTool] = Field(default_factory=list)
    connections: list[ToolPolicyConnection] = Field(default_factory=list)


def _row(
    definition: ToolDefinition, *, server_id: str, server_label: str, default_policy: ToolPolicy
) -> ToolPolicyTool:
    decision, reason = default_policy.explain(definition)
    return ToolPolicyTool(
        name=definition.name,
        description=definition.description,
        server_id=server_id,
        server_label=server_label,
        permission_level=definition.permission_level,
        side_effects=definition.side_effects,
        default_decision=decision,
        default_reason=reason,
    )


def _connection_tools(
    config: McpConnectionConfig, context: ToolInvocationContext
) -> tuple[list[ToolDefinition], str | None]:
    if not config.base_url:
        # URL を設定していない接続は失敗ではない（Run でも使えないので一覧に出すツールも無い）。
        return [], None
    try:
        listed = list_mcp_connection_tools(config.server_id, context=context)
    except KeyError:
        return [], "MCP 接続が見つかりません。"
    except ExternalToolError as exc:
        return [], exc.message
    return [mcp_tool_definition(config, tool) for tool in listed.tools], None


def tool_policy_catalog(context: ToolInvocationContext) -> ToolPolicyToolsData:
    """ツール権限の画面のツールの一覧（MCP 接続の `tools/list` は HTTP。スレッドで呼ぶ）。"""
    config = runtime_config_store.get_tool_policy()
    # 個別のポリシーを外した判定（「既定に従う」を選んだときの動き）。
    default_policy = ToolPolicy(default_mode=config.default_mode)
    tools = [
        _row(
            definition,
            server_id=CONTROL_PLANE_SOURCE,
            server_label="",
            default_policy=default_policy,
        )
        for definition in tool_registry.definitions()
    ]
    servers = runtime_config_store.list_mcp_servers()
    connections: list[ToolPolicyConnection] = []
    if servers:
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_CONNECTIONS, len(servers))) as pool:
            results = list(pool.map(lambda item: _connection_tools(item, context), servers))
        for server, (definitions, error) in zip(servers, results, strict=True):
            label = server.label or server.server_id
            connections.append(
                ToolPolicyConnection(
                    server_id=server.server_id,
                    label=label,
                    configured=bool(server.base_url),
                    trust_tool_annotations=server.annotations_trusted(),
                    tool_count=len(definitions),
                    error=error,
                )
            )
            tools.extend(
                _row(
                    definition,
                    server_id=server.server_id,
                    server_label=label,
                    default_policy=default_policy,
                )
                for definition in definitions
            )
    # 保存したポリシーにあって一覧に無い名前も残す（接続の停止中に保存し直して消さないため）。
    listed = {tool.name for tool in tools}
    labels = {connection.server_id: connection.label for connection in connections}
    for name in sorted((config.allow | config.ask | config.deny) - listed):
        server_id, separator, _tool = name.partition(MCP_TOOL_SEPARATOR)
        source = server_id if separator else CONTROL_PLANE_SOURCE
        tools.append(
            ToolPolicyTool(
                name=name,
                server_id=source,
                server_label=labels.get(source, ""),
                available=False,
            )
        )
    return ToolPolicyToolsData(tools=tools, connections=connections)
