"""チャットの添付の API（`/api/chat-attachments`。3 製品で同じ形。#2517）。

実体は platform の `pr_system_settings.chat_attachments.build_chat_attachment_router`。NL2SQL は
会話の持ち主の判定（見えない会話は 404）とサービスだけを渡す。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pr_system_settings.auth.visibility import can_view_all_users_data
from pr_system_settings.chat_attachments import build_chat_attachment_router

from app.features.nl2sql.chat_attachments import chat_attachment_service
from app.security.dependencies import current_principal


def _conversation_owner(principal: Any, conversation_id: str) -> str:
    """会話（SQL チャット）の持ち主の UUID。見えない会話は 404（「AI 活用」の見え方。#1889）。

    持ち主の分からない会話は空文字（platform の添付の API が添付を 403 にする。一覧は閲覧なので
    返す。#3244）。
    """
    from app.features.nl2sql.service import nl2sql_service

    actor = str(getattr(principal, "user_uuid", ""))
    try:
        chat = nl2sql_service.get_sql_chat(
            conversation_id, actor=actor, all_users=can_view_all_users_data(principal)
        )
    except (LookupError, PermissionError):
        chat = None
    if chat is None:
        raise HTTPException(status_code=404, detail="会話が見つかりません。")
    return (chat.conversation.actor_user_uuid or "").strip()


router = build_chat_attachment_router(
    get_service=lambda: chat_attachment_service(),
    current_principal=current_principal,
    conversation_owner=_conversation_owner,
    tags=["nl2sql"],
)
