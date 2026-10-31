"""NL2SQL の MCP エンドポイント（`POST /api/mcp`、#231）。

Streamable HTTP の JSON 応答だけを扱う（`pr_backend_core.mcp`）。認証は `authorize_api_request`
がサービストークン（`Authorization: Bearer`、audience `nl2sql`）で行い、route manifest では
「認証済みなら通す」。ツールごとの権限はツール側で判定する。

ツールの呼び出し（`tools/call`）は server の span（`mcp.server`）の中で処理し、共通の span の表に
書く（#3226 の案 A）。サービストークン（Agent の Run）の要求だけ、来た traceparent（JSON-RPC の
`params._meta` を優先し、無ければ HTTP header）を親にし、Agent の `mcp.request` の下に木を
つなげる。API キーの外部の MCP クライアントの traceparent は親にせず、新しい trace にする
（span link にだけ残す）。ツールが作るジョブは、その span の下で worker が実行する（同じ trace）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request, Response
from pr_backend_core.mcp import HasAnyPermission, McpServer, mcp_http_response
from pr_backend_core.observability.mcp_trace import incoming_mcp_traceparent, mcp_server_trace
from pr_backend_core.observability.request_context import generate_request_id, request_id_var

from app.features.nl2sql.trace_spans import trace_span_listener

from .tools import build_mcp_server, has_any_permission_for

router = APIRouter(tags=["mcp"])


def _trusted_service_token(request: Request) -> bool:
    """署名を確かめたサービストークン（Agent の Run）の要求か（API キー・local の認証は False）。"""

    return getattr(request.state, "api_key", None) is None and isinstance(
        getattr(request.state, "service_token_claims", None), Mapping
    )


def _request_owner(request: Request) -> str:
    """要求の利用者（サービストークンの `sub` / API キーの持ち主）。認証が無効なら空。"""

    principal = getattr(request.state, "principal", None)
    return str(getattr(principal, "user_uuid", "") or "")


def traced_mcp_server(request: Request, server: McpServer) -> McpServer:
    """`tools/call` を server の span の中で処理する server にする。

    ほかの method（initialize・tools/list など）は span を作らない。

    SSE の進み具合を送る要求も、ツールの実行（`McpServer.handle`）の間だけを span にする。
    """

    handle = server.handle

    async def traced(
        message: Any, *, has_any_permission: HasAnyPermission
    ) -> dict[str, Any] | None:
        if not (
            isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message
        ):
            return await handle(message, has_any_permission=has_any_permission)
        params = message.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        tool_name = params.get("name") if isinstance(params, dict) else None
        operation_id = request_id_var.get() or generate_request_id(None)
        with mcp_server_trace(
            operation_id=operation_id,
            trusted=_trusted_service_token(request),
            traceparent=incoming_mcp_traceparent(
                meta if isinstance(meta, Mapping) else None, request.headers.get("traceparent")
            ),
            listener=trace_span_listener(
                operation_id=operation_id, owner_user_uuid=_request_owner(request)
            ),
            method="tools/call",
            tool_name=tool_name if isinstance(tool_name, str) else None,
        ):
            return await handle(message, has_any_permission=has_any_permission)

    server.handle = traced  # type: ignore[method-assign]
    return server


@router.post("/mcp", response_class=Response)
async def mcp_endpoint(request: Request) -> Response:
    """MCP の JSON-RPC（initialize / ping / tools/list / tools/call）を処理する。"""
    return await mcp_http_response(
        request,
        traced_mcp_server(request, build_mcp_server(request)),
        has_any_permission=has_any_permission_for(request),
    )
