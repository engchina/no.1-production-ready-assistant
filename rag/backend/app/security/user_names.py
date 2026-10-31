"""一覧に出す利用者の表示名（実行履歴 #1890・チャットの会話 #1889。Agent の `user_names` と同じ）。

画面の利用者は表示名で出し、UUID・ログイン ID は出さない（#1696）。構成のシステム管理者
（認証テーブルに無い固定の UUID）は、共通認証の `display_identities` が
表示名「システム管理者」で補う。
引けない利用者（削除済みなど）は省き、画面は「（削除済みのユーザー）」と出す。

チャットの会話は利用者を `user_id_hash`（salt + 利用者 ID の SHA-256）でしか持たないので、
`display_names_by_user_hash` は利用者の UUID を同じ hash にして引き当てる（利用者は少ない）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from pr_system_settings.auth.domain import CONFIGURED_SYSTEM_ADMIN_USER_UUID

from app.rag.request_context import user_id_hash_for
from app.security.dependencies import LOCAL_DEFAULT_USER_ID, local_debug_principal
from app.security.domain import LOCAL_DEBUG_USER_UUID
from app.security.service import get_security_service

logger = logging.getLogger(__name__)

_CONFIGURED_ADMIN_NAME = "システム管理者"


def user_display_names(user_uuids: list[str]) -> dict[str, str]:
    """利用者の UUID → 表示名（ローカル利用者・構成のシステム管理者を含む）。引けない人は省く。"""
    names: dict[str, str] = {}
    if LOCAL_DEBUG_USER_UUID in user_uuids:
        names[LOCAL_DEBUG_USER_UUID] = local_debug_principal().display_name
    remaining = [uuid for uuid in user_uuids if uuid and uuid not in names]
    if not remaining:
        return names
    try:
        identities = get_security_service().display_identities(remaining)
    except Exception:  # noqa: BLE001 - 名前は表示の補助。引けなくても一覧は返す
        logger.warning("rag_user_names_unavailable", exc_info=True)
        return names
    for uuid, identity in identities.items():
        names[uuid] = identity.display_name or identity.login_user_id
    return names


def display_names_by_user_hash(user_id_hashes: Collection[str]) -> dict[str, str]:
    """`user_id_hash` → 表示名（チャットの会話の利用者。#1889）。引けない hash は省く。"""
    wanted = {value for value in user_id_hashes if value}
    if not wanted:
        return {}
    local_name = local_debug_principal().display_name
    # local で `X-User-ID` が無いときの会話の持ち主（`LOCAL_DEFAULT_USER_ID`）とローカル利用者。
    candidates: dict[str, str] = {
        LOCAL_DEFAULT_USER_ID: local_name,
        LOCAL_DEBUG_USER_UUID: local_name,
        CONFIGURED_SYSTEM_ADMIN_USER_UUID: _CONFIGURED_ADMIN_NAME,
    }
    try:
        for user in get_security_service().list_users():
            candidates[user.user_uuid] = user.display_name or user.login_user_id
    except Exception:  # noqa: BLE001 - 名前は表示の補助。引けなくても一覧は返す
        logger.warning("rag_conversation_owner_names_unavailable", exc_info=True)
    names: dict[str, str] = {}
    for user_id, name in candidates.items():
        hashed = user_id_hash_for(user_id)
        if hashed is not None and hashed in wanted:
            names[hashed] = name
    return names
