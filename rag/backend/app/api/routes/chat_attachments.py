"""チャットの添付の API（`/api/chat-attachments`。3 製品で同じ形。#2517）。

実体は platform の `pr_system_settings.chat_attachments.build_chat_attachment_router`。RAG は
会話の持ち主の判定（見えない会話は 404）とサービスだけを渡す。
"""

from __future__ import annotations

from pr_system_settings.chat_attachments import build_chat_attachment_router

from app.rag.chat_attachments import (
    chat_attachment_service,
    conversation_owner_for_attachments,
)
from app.security.dependencies import current_principal

router = build_chat_attachment_router(
    get_service=lambda: chat_attachment_service(),
    current_principal=current_principal,
    conversation_owner=conversation_owner_for_attachments,
    tags=["chat"],
)
