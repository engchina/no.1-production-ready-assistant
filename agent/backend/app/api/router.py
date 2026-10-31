"""業務ルーターの集約（/api 配下に include される）。

`/api` の全 route は `authorize_api_request`（fail-closed の認証/RBAC。#215）を通る。
公開 path（`/health`・`/ready`・`/ready/database`・`/auth/login`）
以外は、production ではログイン（Cookie のセッション）と権限 manifest
（`app.security.permissions`）の権限が必要。

認証の前に `require_agent_storage` が、Run・定義の保存先の読み込みを待つ（#1212。DB に接続
できなければ 503。DB の状態・システム設定・認証・ユーザーとロールの API は待たない）。
"""

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from app.features.agent import storage_bootstrap
from app.features.agent.mcp_server import router as mcp_router
from app.features.agent.router import router as agent_router
from app.features.settings.system_tables import router as system_tables_router
from app.security.dependencies import authorize_api_request
from app.security.router import router as security_router


async def require_agent_storage(request: Request) -> None:
    """業務の API の前に、Run の repository と定義を読み込む（済んでいれば何もしない。#1212）。"""
    if storage_bootstrap.is_ready():
        return
    if not storage_bootstrap.requires_storage(request.url.path):
        return
    await run_in_threadpool(storage_bootstrap.ensure_ready)


api_router = APIRouter(
    dependencies=[Depends(require_agent_storage), Depends(authorize_api_request)]
)
api_router.include_router(security_router)
api_router.include_router(agent_router)
# 外部の MCP クライアントに業務 Agent へのチャットを出す（#1874。API キーで認証）。
api_router.include_router(mcp_router)
api_router.include_router(system_tables_router)
