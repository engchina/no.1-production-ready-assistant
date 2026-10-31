"""会話の添付のエラー（利用者に見せてよい理由。#2517）。

製品の `SecurityApiError` の handler（problem 形式）がそのまま応答にする。
"""

from __future__ import annotations

from ..auth.errors import SecurityApiError

# 共通の DDL（`PLATFORM_CHAT_ATTACHMENTS`）が未適用。
CHAT_ATTACHMENT_SCHEMA_REQUIRED_CODE = "CHAT_ATTACHMENT_SCHEMA_REQUIRED"


class ChatAttachmentError(SecurityApiError):
    """添付の受け付け・読み取りのエラー（413 大きすぎる・415 種類・409 上限など）。"""


def attachment_not_found() -> ChatAttachmentError:
    # 他人の添付・期限の切れた添付・無い添付を区別しない（存在を漏らさない）。
    return ChatAttachmentError(
        404,
        "添付ファイルが見つかりません。期限が切れたか、削除された可能性があります。",
        code="CHAT_ATTACHMENT_NOT_FOUND",
    )


def schema_required() -> ChatAttachmentError:
    return ChatAttachmentError(
        409,
        "添付ファイルの表がありません。システムテーブルを作成・更新してから、もう一度お試しください。",
        code=CHAT_ATTACHMENT_SCHEMA_REQUIRED_CODE,
    )


def settings_unavailable() -> ChatAttachmentError:
    # 設定を読めない（DB に接続できないなど。#2555）。管理者の設定（無効）とは区別する。
    return ChatAttachmentError(
        503,
        "チャットの添付の設定を読めません。しばらくしてからもう一度お試しください。",
        code="CHAT_ATTACHMENT_SETTINGS_UNAVAILABLE",
        retryable=True,
    )
