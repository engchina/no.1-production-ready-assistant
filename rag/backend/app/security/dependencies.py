"""RAG の全 API の fail-closed 認証/RBAC dependency（#214）。

認可の流れは platform の `pr_system_settings.auth.dependencies.authorize_request`。
ここでは RAG の権限 manifest・Cookie 名・local の扱いと、認可後の監査 / 対象範囲の context
（`AuditRequestContext`）の組み立てを渡す。

- local（`PLATFORM_AUTH_MODE=local`）: 全権限・対象範囲の制限なしのローカル利用者。監査 context は
  従来どおり header（`X-User-ID` / `x-rag-*`）から作る。
- production: 利用者と対象範囲は認証済みの利用者から決め、client の header の範囲は使わない。
- MCP（`POST /api/mcp`。#232）: Cookie の代わりにサービストークン（`Authorization: Bearer`）の
  `sub` の利用者として認証する（audience `rag`）。外部の MCP クライアントは API キー
  （`Authorization: Bearer prak_…`。#1805）で、キーを作った利用者として認証する。キーは MCP の
  入口だけで使え、local でも検証する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import Token
from dataclasses import replace
from typing import Any

from fastapi import Request
from pr_system_settings.auth import dependencies as platform_dependencies
from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.visibility import can_view_all_users_data
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.rag.request_context import (
    AuditRequestContext,
    audit_request_context_for_principal,
    audit_request_context_from_headers,
    reset_audit_request_context,
    set_audit_request_context,
)

from .domain import LOCAL_DEBUG_USER_UUID, SYSTEM_ADMIN_ROLE_CODE, Principal, as_principal
from .permissions import (
    ALL_PERMISSION_CODES,
    API_KEY_API_PATHS,
    AUTHENTICATED_WITHOUT_PERMISSION,
    FEEDBACK_MANAGE,
    OPEN_API_OPERATIONS,
    PUBLIC_API_PATHS,
    SERVICE_TOKEN_API_PATHS,
    SERVICE_TOKEN_AUDIENCE,
    UNCLASSIFIED_PERMISSION,
    permission_for_route,
)
from .service import get_security_service

permission_route_path = platform_dependencies.permission_route_path
request_context = platform_dependencies.request_context

# local mode で X-User-ID がないときの利用者（会話・回答履歴の持ち主）。従来の値を保つ。
LOCAL_DEFAULT_USER_ID = "local-user"


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
        allowed_search_answer_profile_ids=None,
        allowed_knowledge_base_ids=None,
    )


def audit_context_for_request(
    request: Request, principal: PlatformPrincipal
) -> AuditRequestContext:
    """認可を通った利用者の監査 / 対象範囲の context。"""
    settings = get_settings()
    request_id = str(getattr(request.state, "request_id", "") or "")
    if settings.local_debug_enabled:
        # local の利用者は SYSTEM_ADMIN（全権限）なので、保存済みの回答（#304）と
        # 利用者フィードバック（#408）とチャットの会話（#1889）も全件を扱う。
        return replace(
            audit_request_context_from_headers(
                request.headers,
                request_id=request_id,
                settings=settings,
                default_user_id=LOCAL_DEFAULT_USER_ID,
                allow_user_header=True,
            ),
            answer_records_unrestricted=True,
            feedback_all_users=True,
            # 実行履歴（#1890）の実行した利用者。`X-User-ID` が無ければローカル利用者。
            user_uuid=(request.headers.get("x-user-id") or "").strip() or LOCAL_DEBUG_USER_UUID,
            all_users_visible=True,
        )
    rag_principal = as_principal(principal)
    # MCP（サービストークン。#232）では token の利用者に加えて、Agent の agent_id / run_id を
    # memory の分割キーに使う。
    claims = getattr(request.state, "service_token_claims", None)
    return audit_request_context_for_principal(
        request.headers,
        request_id=request_id,
        settings=settings,
        user_uuid=rag_principal.user_uuid,
        allowed_search_answer_profile_ids=rag_principal.allowed_search_answer_profile_ids,
        allowed_knowledge_base_ids=rag_principal.allowed_knowledge_base_ids,
        service_token_claims=claims if isinstance(claims, dict) else None,
        # 保存済みの回答は持ち主だけが扱う。SYSTEM_ADMIN と rag.feedback.manage は全件（#304）。
        answer_records_unrestricted=rag_principal.has_permission(FEEDBACK_MANAGE),
        # 利用者フィードバックは SYSTEM_ADMIN（構成管理者を含む）だけが全員の分を見る。ほかの
        # ロールは自分が送った分だけ（#408。NL2SQL の実行履歴と同じ規則）。
        feedback_all_users=rag_principal.is_system_admin,
        # 「AI 活用」のデータ（実行履歴。#1890）は system_admin だけが全員の分を見る（#1889）。
        all_users_visible=can_view_all_users_data(rag_principal),
    )


async def authorize_api_request(request: Request) -> AsyncIterator[None]:
    """`/api` の全 route の dependency。公開 path 以外はログインと manifest の権限を確認する。"""

    def enter_actor(principal: Any) -> Token[AuditRequestContext | None]:
        return set_audit_request_context(audit_context_for_request(request, principal))

    async with platform_dependencies.authorize_request(
        request,
        settings=get_settings(),
        service=get_security_service(),
        run_sync=lambda *args: run_in_threadpool(*args),
        permission_for_route=permission_for_route,
        public_paths=PUBLIC_API_PATHS,
        authenticated_without_permission=AUTHENTICATED_WITHOUT_PERMISSION,
        local_debug_principal=local_debug_principal,
        enter_actor=enter_actor,
        exit_actor=reset_audit_request_context,
        unclassified_permission=UNCLASSIFIED_PERMISSION,
        service_token_paths=SERVICE_TOKEN_API_PATHS,
        service_token_audience=SERVICE_TOKEN_AUDIENCE,
        open_operations=OPEN_API_OPERATIONS,
        api_key_paths=API_KEY_API_PATHS,
    ):
        yield


def current_principal(request: Request) -> Principal:
    return platform_dependencies.current_principal(request, Principal)
