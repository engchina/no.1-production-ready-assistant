"""Agent の全 API の fail-closed 認証/RBAC dependency（#215 / #750）。

認可の流れは platform の `pr_system_settings.auth.dependencies.authorize_request`（RAG / NL2SQL と
同じ）。ここでは Agent の権限 manifest・Cookie 名・local の扱いを渡す。

判定順（`/api` の HTTP）:

1. local（`PLATFORM_AUTH_MODE=local`）: 全権限のローカル利用者（`request.state.principal`）。
2. production で公開 path（`/health`・`/ready`・`/ready/database`・`/auth/login`）: そのまま通す。
3. production: 共通認証（DB ユーザー・構成管理者）の session Cookie を検証し、更新系は CSRF を
   照合し、manifest の権限を確認する。Cookie が無ければ 401。router は利用者から `ActorPolicy`
   を作る。
4. `POST /mcp`（#1874）: 外部の MCP クライアントの API キー（`Authorization: Bearer prak_…`）を
   local でも検証し、キーを発行した利用者として認証する（共通の `api_key_paths`。RAG / NL2SQL と
   同じ）。キーの業務 Agent の範囲は `request.state.api_key` から MCP のツールが絞る。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from pr_system_settings.auth import dependencies as platform_dependencies
from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from starlette.concurrency import run_in_threadpool

import app.settings as app_settings

from .domain import LOCAL_DEBUG_USER_UUID, SYSTEM_ADMIN_ROLE_CODE, Principal
from .permissions import (
    ALL_PERMISSION_CODES,
    API_KEY_API_PATHS,
    AUTHENTICATED_WITHOUT_PERMISSION,
    OPEN_API_OPERATIONS,
    PUBLIC_API_PATHS,
    UNCLASSIFIED_PERMISSION,
    permission_for_route,
    roles_for_permissions,
)
from .service import get_security_service

permission_route_path = platform_dependencies.permission_route_path
request_context = platform_dependencies.request_context


def local_debug_principal() -> Principal:
    """DB セッションを作らない local 専用の利用者（全権限・対象範囲の制限なし）。"""
    return Principal(
        user_uuid=LOCAL_DEBUG_USER_UUID,
        login_user_id="local",
        display_name="ローカル利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE],
        permissions=set(ALL_PERMISSION_CODES),
        session_id="local-debug",
        csrf_token_hash="",  # nosec B106 - local はブラウザのセッションを作らない
        password_change_allowed=False,
        allowed_agent_ids=None,
    )


def _enter_actor(_principal: Any) -> None:
    return None


def _exit_actor(_token: Any) -> None:
    return None


async def authorize_api_request(request: Request) -> AsyncIterator[None]:
    """`/api` の全 route の dependency。公開 path 以外はログインと manifest の権限を確認する。

    `/api` は HTTP の route だけを持つ（Run のイベントの push は SSE、操作は REST。#2260）。
    """
    async with platform_dependencies.authorize_request(
        request,
        settings=app_settings.get_settings(),
        service=get_security_service(),
        run_sync=lambda *args: run_in_threadpool(*args),
        permission_for_route=permission_for_route,
        public_paths=PUBLIC_API_PATHS,
        authenticated_without_permission=AUTHENTICATED_WITHOUT_PERMISSION,
        local_debug_principal=local_debug_principal,
        enter_actor=_enter_actor,
        exit_actor=_exit_actor,
        unclassified_permission=UNCLASSIFIED_PERMISSION,
        open_operations=OPEN_API_OPERATIONS,
        api_key_paths=API_KEY_API_PATHS,
    ):
        yield


def current_principal(request: Request) -> Principal:
    return platform_dependencies.current_principal(request, Principal)


def actor_roles_for_principal(principal: PlatformPrincipal) -> set[str]:
    """利用者の capability → 従来のロール名（SYSTEM_ADMIN は admin）。"""
    roles = roles_for_permissions(principal.permissions)
    if principal.is_system_admin:
        roles.add("admin")
    return roles
