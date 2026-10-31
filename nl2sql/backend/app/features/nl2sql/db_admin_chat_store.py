"""DB 管理チャットの会話の履歴の保存先（#2319）。DDL は migration 024。

会話（`NL2SQL_DB_ADMIN_CHATS`。持ち主・名前）とターン（`NL2SQL_DB_ADMIN_CHAT_TURNS`。
再表示に要るものを JSON で持つ）を残す。チャットの会話は SQL 生成のジョブ（実行履歴・
フィードバック・業務プロファイルの範囲と結び付く）なので流用せず、専用の表に置く。
実行の監査は `NL2SQL_ADMIN_AUDIT_LOG`（migration 023）のまま。
Oracle の永続化を使わない構成（テスト・ローカルの memory）は、同じ契約の memory に残す。

だれの会話を扱えるか（system_admin は全員・それ以外は本人だけ）はサービスが決める。ここは持ち主で
絞る条件（`owner_user_uuid`。None は絞らない）を受け取るだけ。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pr_backend_core.chat_history_window import chat_history_cutoff, within_chat_history

from .models import DbAdminChatSummary, DbAdminChatTurn

# 会話の名前の列の長さ（migration 024 の VARCHAR2(400 CHAR)）。
TITLE_MAX_CHARS = 400


@dataclass(slots=True)
class _MemoryConversation:
    summary: DbAdminChatSummary
    turns: list[DbAdminChatTurn] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    return str(value or "")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value.read() if hasattr(value, "read") else value)


def _turn_json(turn: DbAdminChatTurn) -> str:
    # 表示名は読むときに引く（保存しない）。ID と送り手は列に持つ。
    payload = turn.model_dump(
        mode="json", exclude={"turn_id", "sender_user_uuid", "sender_display_name"}
    )
    return json.dumps(payload, ensure_ascii=False)


def _decode_turn(turn_id: Any, sender: Any, payload: Any) -> DbAdminChatTurn:
    # IS JSON の制約の CLOB は、python-oracledb が dict で返すことがある（文字列なら読み直す）。
    data = payload if isinstance(payload, dict) else json.loads(_text(payload) or "{}")
    if not isinstance(data, dict):
        data = {}
    data["turn_id"] = _text(turn_id)
    data["sender_user_uuid"] = _text(sender) or None
    return DbAdminChatTurn.model_validate(data)


class DbAdminChatStore:
    """Oracle の表（migration 024）か memory に、DB 管理チャットの会話とターンを残す。"""

    def __init__(self, connection: Callable[[], AbstractContextManager[Any]] | None = None):
        self.connection = connection
        self._lock = threading.RLock()
        self._conversations: dict[str, _MemoryConversation] = {}

    # ---- 会話 ----

    def create_conversation(
        self, conversation_id: str, *, owner_user_uuid: str, title: str
    ) -> DbAdminChatSummary:
        clipped = title.strip()[:TITLE_MAX_CHARS]
        if self.connection is None:
            now = _now_iso()
            summary = DbAdminChatSummary(
                id=conversation_id,
                title=clipped,
                created_at=now,
                updated_at=now,
                actor_user_uuid=owner_user_uuid,
            )
            with self._lock:
                self._conversations[conversation_id] = _MemoryConversation(summary=summary)
            return summary.model_copy(deep=True)
        # 時刻は DB の SYSTIMESTAMP（列の既定）で残す（python-oracledb は datetime を秒までの
        # DATE で渡すので、同じ秒の会話の並びが崩れる）。
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO NL2SQL_DB_ADMIN_CHATS (CONVERSATION_ID, OWNER_USER_UUID, TITLE) "
                "VALUES (:conversation_id, :owner, :title)",
                {"conversation_id": conversation_id, "owner": owner_user_uuid, "title": clipped},
            )
            conn.commit()
        created = self.get_conversation(conversation_id)
        if created is None:  # pragma: no cover - 直前に作った行が読めないのは DB の異常
            raise LookupError(conversation_id)
        return created

    def get_conversation(self, conversation_id: str) -> DbAdminChatSummary | None:
        if self.connection is None:
            with self._lock:
                stored = self._conversations.get(conversation_id)
                return stored.summary.model_copy(deep=True) if stored else None
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT CONVERSATION_ID, OWNER_USER_UUID, TITLE, CREATED_AT, UPDATED_AT "
                "FROM NL2SQL_DB_ADMIN_CHATS WHERE CONVERSATION_ID = :conversation_id",
                {"conversation_id": conversation_id},
            )
            row = cursor.fetchone()
        return self._decode_summary(row) if row else None

    def list_conversations(
        self,
        *,
        owner_user_uuid: str | None,
        offset: int,
        limit: int,
        updated_within_days: int = 0,
    ) -> tuple[list[DbAdminChatSummary], int]:
        """新しい順（始めた時刻）の 1 ページと総数。`owner_user_uuid` が None なら全員の会話。

        `updated_within_days` は会話の履歴の期間（最後の更新〔ターンを足す・名前を変えると進む〕が
        直近 N 日の会話だけを、総数と 1 ページの両方で返す。0 は絞らない。#2340）。
        """

        if self.connection is None:
            updated_since = chat_history_cutoff(updated_within_days)
            with self._lock:
                matched = sorted(
                    (
                        item.summary
                        for item in self._conversations.values()
                        if (
                            owner_user_uuid is None
                            or item.summary.actor_user_uuid == owner_user_uuid
                        )
                        and within_chat_history(item.summary.updated_at, updated_since)
                    ),
                    key=lambda summary: (summary.created_at, summary.id),
                    reverse=True,
                )
                return (
                    [item.model_copy(deep=True) for item in matched[offset : offset + limit]],
                    len(matched),
                )
        clauses: list[str] = []
        binds: dict[str, Any] = {}
        if owner_user_uuid is not None:
            clauses.append("OWNER_USER_UUID = :owner")
            binds["owner"] = owner_user_uuid
        if updated_within_days > 0:
            # 時刻は DB の時刻で比べる（UPDATED_AT は SYSTIMESTAMP で書く）。
            clauses.append("UPDATED_AT >= SYSTIMESTAMP - NUMTODSINTERVAL(:days, 'DAY')")
            binds["days"] = updated_within_days
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM NL2SQL_DB_ADMIN_CHATS{where}", binds)  # nosec B608
            total_row = cursor.fetchone()
            cursor.execute(
                "SELECT CONVERSATION_ID, OWNER_USER_UUID, TITLE, CREATED_AT, UPDATED_AT "  # nosec B608
                f"FROM NL2SQL_DB_ADMIN_CHATS{where} "
                "ORDER BY CREATED_AT DESC, CONVERSATION_ID DESC "
                "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
                {**binds, "offset": offset, "limit": limit},
            )
            rows = cursor.fetchall()
        total = int(total_row[0]) if total_row else 0
        return [self._decode_summary(row) for row in rows], total

    def rename_conversation(self, conversation_id: str, title: str) -> DbAdminChatSummary | None:
        clipped = title.strip()[:TITLE_MAX_CHARS]
        if self.connection is None:
            with self._lock:
                stored = self._conversations.get(conversation_id)
                if stored is None:
                    return None
                stored.summary = stored.summary.model_copy(
                    update={"title": clipped, "updated_at": _now_iso()}
                )
                return stored.summary.model_copy(deep=True)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE NL2SQL_DB_ADMIN_CHATS SET TITLE = :title, UPDATED_AT = SYSTIMESTAMP "
                "WHERE CONVERSATION_ID = :conversation_id",
                {"title": clipped, "conversation_id": conversation_id},
            )
            conn.commit()
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        """会話とターンを消す（ターンは FK の ON DELETE CASCADE）。"""

        if self.connection is None:
            with self._lock:
                return self._conversations.pop(conversation_id, None) is not None
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM NL2SQL_DB_ADMIN_CHATS WHERE CONVERSATION_ID = :conversation_id",
                {"conversation_id": conversation_id},
            )
            deleted = int(getattr(cursor, "rowcount", 0) or 0)
            conn.commit()
        return deleted > 0

    # ---- ターン ----

    def list_turns(self, conversation_id: str) -> list[DbAdminChatTurn]:
        """会話のターン（送った順）。"""

        if self.connection is None:
            with self._lock:
                stored = self._conversations.get(conversation_id)
                return [turn.model_copy(deep=True) for turn in stored.turns] if stored else []
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT TURN_ID, SENDER_USER_UUID, TURN_JSON FROM NL2SQL_DB_ADMIN_CHAT_TURNS "
                "WHERE CONVERSATION_ID = :conversation_id ORDER BY TURN_NO, CREATED_AT, TURN_ID",
                {"conversation_id": conversation_id},
            )
            rows = cursor.fetchall()
        return [_decode_turn(row[0], row[1], row[2]) for row in rows]

    def get_turn(self, conversation_id: str, turn_id: str) -> DbAdminChatTurn | None:
        if self.connection is None:
            with self._lock:
                stored = self._conversations.get(conversation_id)
                for turn in stored.turns if stored else []:
                    if turn.turn_id == turn_id:
                        return turn.model_copy(deep=True)
                return None
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT TURN_ID, SENDER_USER_UUID, TURN_JSON FROM NL2SQL_DB_ADMIN_CHAT_TURNS "
                "WHERE CONVERSATION_ID = :conversation_id AND TURN_ID = :turn_id",
                {"conversation_id": conversation_id, "turn_id": turn_id},
            )
            row = cursor.fetchone()
        return _decode_turn(row[0], row[1], row[2]) if row else None

    def append_turn(self, conversation_id: str, turn: DbAdminChatTurn) -> None:
        """会話の末尾にターンを足し、会話の更新時刻を進める。"""

        if self.connection is None:
            with self._lock:
                stored = self._conversations.get(conversation_id)
                if stored is None:
                    raise LookupError(conversation_id)
                stored.turns.append(turn.model_copy(deep=True))
                stored.summary = stored.summary.model_copy(update={"updated_at": _now_iso()})
            return
        with self.connection() as conn, conn.cursor() as cursor:
            if hasattr(cursor, "setinputsizes"):
                cursor.setinputsizes(turn_json=_clob_type())
            cursor.execute(
                "INSERT INTO NL2SQL_DB_ADMIN_CHAT_TURNS "
                "(TURN_ID, CONVERSATION_ID, TURN_NO, SENDER_USER_UUID, TURN_JSON) "
                "SELECT :turn_id, :conversation_id, NVL(MAX(TURN_NO), 0) + 1, :sender, :turn_json "
                "FROM NL2SQL_DB_ADMIN_CHAT_TURNS WHERE CONVERSATION_ID = :conversation_id",
                {
                    "turn_id": turn.turn_id,
                    "conversation_id": conversation_id,
                    "sender": turn.sender_user_uuid,
                    "turn_json": _turn_json(turn),
                },
            )
            cursor.execute(
                "UPDATE NL2SQL_DB_ADMIN_CHATS SET UPDATED_AT = SYSTIMESTAMP "
                "WHERE CONVERSATION_ID = :conversation_id",
                {"conversation_id": conversation_id},
            )
            conn.commit()

    def update_turn(self, conversation_id: str, turn: DbAdminChatTurn) -> None:
        """ターンの内容（SQL の置き換え・最後の実行の要約）を書き換える。"""

        if self.connection is None:
            with self._lock:
                stored = self._conversations.get(conversation_id)
                if stored is None:
                    raise LookupError(conversation_id)
                for index, current in enumerate(stored.turns):
                    if current.turn_id == turn.turn_id:
                        stored.turns[index] = turn.model_copy(
                            deep=True, update={"sender_user_uuid": current.sender_user_uuid}
                        )
                        return
                raise LookupError(turn.turn_id)
        with self.connection() as conn, conn.cursor() as cursor:
            if hasattr(cursor, "setinputsizes"):
                cursor.setinputsizes(turn_json=_clob_type())
            cursor.execute(
                "UPDATE NL2SQL_DB_ADMIN_CHAT_TURNS SET TURN_JSON = :turn_json, "
                "UPDATED_AT = SYSTIMESTAMP "
                "WHERE CONVERSATION_ID = :conversation_id AND TURN_ID = :turn_id",
                {
                    "turn_json": _turn_json(turn),
                    "conversation_id": conversation_id,
                    "turn_id": turn.turn_id,
                },
            )
            updated = int(getattr(cursor, "rowcount", 0) or 0)
            conn.commit()
        if updated == 0:
            raise LookupError(turn.turn_id)

    @staticmethod
    def _decode_summary(row: Any) -> DbAdminChatSummary:
        return DbAdminChatSummary(
            id=_text(row[0]),
            actor_user_uuid=_text(row[1]),
            title=_text(row[2]),
            created_at=_iso(row[3]),
            updated_at=_iso(row[4]),
        )


def _clob_type() -> Any:
    """CLOB の bind の型（python-oracledb の `DB_TYPE_CLOB`）。"""

    import oracledb

    return oracledb.DB_TYPE_CLOB
