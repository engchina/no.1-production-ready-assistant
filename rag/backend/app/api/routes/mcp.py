"""MCP の入口（`POST /api/mcp`。#232）。

Agent がサービストークン（`Authorization: Bearer`、audience `rag`）で呼ぶ。認証は
`authorize_api_request` が行い（token の `sub` の利用者。local は全権限のローカル利用者）、
ツールごとの権限はここで利用者の権限から判定する。ツールは `app.mcp.tools`。

ツールの呼び出し（`tools/call`）は server の span（`mcp.server`）の中で処理し、共通の span の表に
書く（#3226）。サービストークンの要求だけ Agent の traceparent を親にして Agent の木につなげる（
`app.rag.trace_store.mcp_request_trace`）。
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pr_backend_core.mcp import HasAnyPermission, McpServer, mcp_http_response

from app.mcp.tools import build_rag_mcp_server
from app.rag.trace_store import mcp_request_trace

router = APIRouter()


class TracedMcpServer(McpServer):
    """`tools/call` を server の span の中で処理する MCP サーバー（ツールは元のサーバーのもの）。

    進み具合を SSE で送る呼び出しは、共通の入口がツールの実行を別の task で動かすので、span はその
    task の中（`handle`）で開いて閉じる。
    """

    def __init__(self, server: McpServer, request: Request) -> None:
        super().__init__(
            name=server.name,
            version=server.version,
            tools=server.tools.values(),
            instructions=server.instructions,
        )
        self._request = request

    async def handle(
        self, message: Any, *, has_any_permission: HasAnyPermission
    ) -> dict[str, Any] | None:
        if not (
            isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message
        ):
            return await super().handle(message, has_any_permission=has_any_permission)
        with mcp_request_trace(self._request, message):
            return await super().handle(message, has_any_permission=has_any_permission)


@router.post("", response_class=Response)
async def mcp_endpoint(request: Request) -> Response:
    """MCP の JSON-RPC（`initialize` / `ping` / `tools/list` / `tools/call`）を処理する。"""
    principal = getattr(request.state, "principal", None)

    def has_any_permission(permissions: frozenset[str]) -> bool:
        return principal is not None and bool(principal.has_any_permission(set(permissions)))

    return await mcp_http_response(
        request,
        TracedMcpServer(build_rag_mcp_server(request), request),
        has_any_permission=has_any_permission,
    )
