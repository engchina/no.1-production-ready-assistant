"""署名つきの URL（図 #1311・原文のプレビュー #2389）の読み取りの共通の手順。

URL は Cookie もサービストークンも使わず、path のトークンだけで読む（権限 manifest では公開
path。認証は各 route が行う）。読むたびに、トークンの利用者（``sub``）の今の状態と権限を確かめ、
その利用者の監査・対象範囲の context で読む。
"""

from __future__ import annotations

from contextvars import Token
from typing import Any

from fastapi import HTTPException, Request
from pr_system_settings.auth.errors import SecurityApiError
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.rag.request_context import AuditRequestContext, set_audit_request_context
from app.security.dependencies import audit_context_for_request, local_debug_principal
from app.security.service import get_security_service

# 応答とエラーの応答の両方に付ける（URL を共有の cache・Referer に残さない）。
SIGNED_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def signed_error(status_code: int, message: str) -> HTTPException:
    """署名つきの URL の読み取りの失敗（cache・Referer に残さない header つき）。"""
    return HTTPException(status_code=status_code, detail=message, headers=SIGNED_RESPONSE_HEADERS)


async def enter_token_user(
    request: Request, subject: str, *, forbidden_message: str
) -> Token[Any] | None:
    """トークンの利用者の今の権限で、監査・対象範囲の context を作る（local は依存関係が作る）。

    無効な利用者・検索の権限が無い利用者は 403。戻り値は ``reset_audit_request_context`` に渡す。
    """
    settings = get_settings()
    if settings.local_debug_enabled:
        if subject != local_debug_principal().user_uuid:
            raise signed_error(403, forbidden_message)
        return None
    try:
        principal = await run_in_threadpool(get_security_service().principal_for_worker, subject)
    except SecurityApiError as exc:
        if exc.status_code >= 500:
            raise
        raise signed_error(403, forbidden_message) from None
    if not principal.has_any_permission(set(mcp_tools.SEARCH_PERMISSIONS)):
        raise signed_error(403, forbidden_message)
    request.state.principal = principal
    context: AuditRequestContext = audit_context_for_request(request, principal)
    return set_audit_request_context(context)
