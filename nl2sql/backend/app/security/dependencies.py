"""FastAPI 全 API の fail-closed 認証/RBAC dependency。

認可の流れは platform の `pr_system_settings.auth.dependencies.authorize_request`（#212）。
ここでは NL2SQL の権限 manifest・Cookie 名・local debug・actor の伝播を渡す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from pr_system_settings.auth import dependencies as platform_dependencies
from pr_system_settings.auth.domain import LOCAL_DEBUG_USER_UUID as LOCAL_DEBUG_USER_UUID
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_CODE

from app.api.concurrency import run_sync_io as run_in_threadpool
from app.settings import get_settings

from .domain import Principal
from .permissions import (
    ALL_PERMISSION_CODES,
    API_KEY_API_PATHS,
    AUTHENTICATED_WITHOUT_PERMISSION,
    MCP_AUDIENCE,
    OPEN_API_OPERATIONS,
    PUBLIC_API_PATHS,
    SERVICE_TOKEN_API_PATHS,
    UNCLASSIFIED_PERMISSION,
    permission_for_route,
)
from .request_actor import reset_actor_context, set_actor_context
from .service import get_security_service

permission_route_path = platform_dependencies.permission_route_path
request_context = platform_dependencies.request_context


def local_debug_principal() -> Principal:
    """DB session を作らない local debug 専用 SYSTEM_ADMIN identity。"""
    return Principal(
        user_uuid=LOCAL_DEBUG_USER_UUID,
        login_user_id="local-debug",
        display_name="ローカル DEBUG 管理者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE],
        permissions=set(ALL_PERMISSION_CODES),
        data_entitlements=[],
        allowed_profile_ids=set(),
        session_id="local-debug",
        csrf_token_hash="",  # nosec B106
        password_change_allowed=False,
    )


def _enter_actor(principal: Any) -> Any:
    return set_actor_context(principal.user_uuid, is_system_admin=principal.is_system_admin)


async def authorize_api_request(request: Request) -> AsyncIterator[None]:
    async with platform_dependencies.authorize_request(
        request,
        settings=get_settings(),
        service=get_security_service(),
        # テストが module の run_in_threadpool を差し替えるため、呼び出しのたびに引く。
        run_sync=lambda *args: run_in_threadpool(*args),
        permission_for_route=permission_for_route,
        public_paths=PUBLIC_API_PATHS,
        authenticated_without_permission=AUTHENTICATED_WITHOUT_PERMISSION,
        local_debug_principal=local_debug_principal,
        enter_actor=_enter_actor,
        exit_actor=reset_actor_context,
        unclassified_permission=UNCLASSIFIED_PERMISSION,
        # Agent は MCP を Run の利用者のサービストークンで呼ぶ（#230 / #231）。
        service_token_paths=SERVICE_TOKEN_API_PATHS,
        service_token_audience=MCP_AUDIENCE,
        open_operations=OPEN_API_OPERATIONS,
        # 外部の MCP クライアントは API キー（`Bearer prak_…`）で MCP だけを呼ぶ（#1805）。
        api_key_paths=API_KEY_API_PATHS,
    ):
        yield


def current_principal(request: Request) -> Principal:
    return platform_dependencies.current_principal(request, Principal)
