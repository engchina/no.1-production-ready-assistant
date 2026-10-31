"""会話の添付の保存（`PLATFORM_CHAT_ATTACHMENTS`。#2517）。

原本は BLOB、解析の結果は CLOB の JSON（`PARSED_JSON` は本文、`OUTLINE_JSON` は索引と解析の
状態）。一覧は索引だけを読み、本文は 1 件ずつ読む。DDL は `auth.migrations` の共通の DDL。

解析の状態（#2652）は `OUTLINE_JSON` の `parse_state` に置き、`replace_state` が
`parse_state.revision` を比べて書き換える（列を足さない。同時の解析・古い試行の遅い完了・削除や
期限の後の完了で、状態を壊さず行を復活させない）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from .errors import schema_required
from .models import (
    AttachmentOutline,
    ChatAttachmentKind,
    ChatAttachmentProduct,
    ChatAttachmentRecord,
    ParsedAttachment,
)

CHAT_ATTACHMENTS_TABLE = "PLATFORM_CHAT_ATTACHMENTS"
# 会話の添付の共通の表（製品のシステムテーブルの全再作成で消さない表に足す）。
CHAT_ATTACHMENT_TABLES: tuple[str, ...] = (
    CHAT_ATTACHMENTS_TABLE,
    "PLATFORM_CHAT_ATTACHMENT_SETTINGS",
)


class ChatAttachmentStore(Protocol):
    def insert(
        self, record: ChatAttachmentRecord, content: bytes, parsed: ParsedAttachment
    ) -> None: ...

    def get(self, attachment_id: str) -> ChatAttachmentRecord | None: ...

    def parsed(self, attachment_id: str) -> ParsedAttachment | None: ...

    def content(self, attachment_id: str) -> bytes | None: ...

    def replace_state(
        self,
        attachment_id: str,
        outline: AttachmentOutline,
        *,
        expected_revision: int,
        now: datetime,
        parsed: ParsedAttachment | None = None,
    ) -> bool:
        """`OUTLINE_JSON`（と `parsed` があれば `PARSED_JSON`）を置き換える（#2652）。

        今の `parse_state.revision`（無い行は 0）が `expected_revision` で、期限内の行だけを
        書き換え、書き換えたかを返す（消えた・期限の切れた・ほかの試行が書き換えた行は False）。
        """
        ...

    def list_for_conversation(
        self, product: ChatAttachmentProduct, conversation_id: str, *, now: datetime
    ) -> list[ChatAttachmentRecord]: ...

    def list_pending(
        self, product: ChatAttachmentProduct, uploaded_by_user_uuid: str, *, now: datetime
    ) -> list[ChatAttachmentRecord]: ...

    def bind(
        self,
        attachment_ids: Sequence[str],
        *,
        conversation_id: str,
        owner_user_uuid: str,
        expires_at: datetime,
    ) -> None: ...

    def touch_conversation(
        self, product: ChatAttachmentProduct, conversation_id: str, *, expires_at: datetime
    ) -> None: ...

    def delete(self, attachment_id: str) -> bool: ...

    def delete_conversation(self, product: ChatAttachmentProduct, conversation_id: str) -> int: ...

    def purge_expired(self, *, now: datetime, limit: int) -> int: ...


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class InMemoryChatAttachmentStore:
    """プロセスの中だけの保存（テスト・DB を使わない構成）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ChatAttachmentRecord] = {}
        self._parsed: dict[str, ParsedAttachment] = {}
        self._content: dict[str, bytes] = {}

    def insert(
        self, record: ChatAttachmentRecord, content: bytes, parsed: ParsedAttachment
    ) -> None:
        with self._lock:
            self._records[record.attachment_id] = record
            self._parsed[record.attachment_id] = parsed
            self._content[record.attachment_id] = content

    def get(self, attachment_id: str) -> ChatAttachmentRecord | None:
        with self._lock:
            return self._records.get(attachment_id)

    def parsed(self, attachment_id: str) -> ParsedAttachment | None:
        with self._lock:
            return self._parsed.get(attachment_id)

    def content(self, attachment_id: str) -> bytes | None:
        with self._lock:
            return self._content.get(attachment_id)

    def replace_state(
        self,
        attachment_id: str,
        outline: AttachmentOutline,
        *,
        expected_revision: int,
        now: datetime,
        parsed: ParsedAttachment | None = None,
    ) -> bool:
        with self._lock:
            record = self._records.get(attachment_id)
            if (
                record is None
                or _utc(record.expires_at) <= _utc(now)
                or record.outline.revision != expected_revision
            ):
                return False
            if parsed is not None:
                self._parsed[attachment_id] = parsed
            self._records[attachment_id] = record.model_copy(update={"outline": outline})
            return True

    def _select(self, predicate: Any) -> list[ChatAttachmentRecord]:
        with self._lock:
            records = [record for record in self._records.values() if predicate(record)]
        return sorted(records, key=lambda record: (record.created_at, record.attachment_id))

    def list_for_conversation(
        self, product: ChatAttachmentProduct, conversation_id: str, *, now: datetime
    ) -> list[ChatAttachmentRecord]:
        return self._select(
            lambda record: (
                record.product == product
                and record.conversation_id == conversation_id
                and _utc(record.expires_at) > _utc(now)
            )
        )

    def list_pending(
        self, product: ChatAttachmentProduct, uploaded_by_user_uuid: str, *, now: datetime
    ) -> list[ChatAttachmentRecord]:
        return self._select(
            lambda record: (
                record.product == product
                and record.conversation_id is None
                and record.uploaded_by_user_uuid == uploaded_by_user_uuid
                and _utc(record.expires_at) > _utc(now)
            )
        )

    def bind(
        self,
        attachment_ids: Sequence[str],
        *,
        conversation_id: str,
        owner_user_uuid: str,
        expires_at: datetime,
    ) -> None:
        with self._lock:
            for attachment_id in attachment_ids:
                record = self._records.get(attachment_id)
                if record is None:
                    continue
                self._records[attachment_id] = record.model_copy(
                    update={
                        "conversation_id": conversation_id,
                        "owner_user_uuid": owner_user_uuid,
                        "expires_at": expires_at,
                    }
                )

    def touch_conversation(
        self, product: ChatAttachmentProduct, conversation_id: str, *, expires_at: datetime
    ) -> None:
        with self._lock:
            for attachment_id, record in list(self._records.items()):
                if record.product == product and record.conversation_id == conversation_id:
                    self._records[attachment_id] = record.model_copy(
                        update={"expires_at": expires_at}
                    )

    def _drop(self, attachment_id: str) -> None:
        self._records.pop(attachment_id, None)
        self._parsed.pop(attachment_id, None)
        self._content.pop(attachment_id, None)

    def delete(self, attachment_id: str) -> bool:
        with self._lock:
            found = attachment_id in self._records
            self._drop(attachment_id)
        return found

    def delete_conversation(self, product: ChatAttachmentProduct, conversation_id: str) -> int:
        with self._lock:
            ids = [
                attachment_id
                for attachment_id, record in self._records.items()
                if record.product == product and record.conversation_id == conversation_id
            ]
            for attachment_id in ids:
                self._drop(attachment_id)
        return len(ids)

    def purge_expired(self, *, now: datetime, limit: int) -> int:
        with self._lock:
            ids = [
                attachment_id
                for attachment_id, record in self._records.items()
                if _utc(record.expires_at) <= _utc(now)
            ][:limit]
            for attachment_id in ids:
                self._drop(attachment_id)
        return len(ids)


_COLUMNS = (
    "ATTACHMENT_ID, PRODUCT, OWNER_USER_UUID, UPLOADED_BY_USER_UUID, CONVERSATION_ID, FILE_NAME, "
    "MEDIA_TYPE, FILE_KIND, SIZE_BYTES, CONTENT_SHA256, OUTLINE_JSON, CREATED_AT, EXPIRES_AT"
)


def _read_lob(value: Any) -> Any:
    if value is not None and hasattr(value, "read"):
        return value.read()
    return value


def _json_column[M: BaseModel](model: type[M], value: Any) -> M:
    """`IS JSON` の CLOB の列を読む（#2548）。

    python-oracledb は `IS JSON` の制約のある列を dict / list で返す（制約を知らない経路では
    文字列・LOB のまま）。どちらでも同じ model にする。
    """
    raw = _read_lob(value)
    if not raw:
        return model()
    if isinstance(raw, (str, bytes, bytearray)):
        return model.model_validate_json(raw)
    return model.model_validate(raw)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    raise TypeError("timestamp column is not a datetime")


def _record_from_row(row: Sequence[Any]) -> ChatAttachmentRecord:
    outline = _json_column(AttachmentOutline, row[10])
    kind: ChatAttachmentKind = row[7]
    product: ChatAttachmentProduct = row[1]
    return ChatAttachmentRecord(
        attachment_id=str(row[0]),
        product=product,
        owner_user_uuid=str(row[2]),
        uploaded_by_user_uuid=str(row[3]),
        conversation_id=str(row[4]) if row[4] is not None else None,
        file_name=str(row[5]),
        media_type=str(row[6]),
        kind=kind,
        size_bytes=int(row[8]),
        content_sha256=str(row[9]),
        outline=outline,
        created_at=_timestamp(row[11]),
        expires_at=_timestamp(row[12]),
    )


class OracleChatAttachmentStore:
    """`PLATFORM_CHAT_ATTACHMENTS` の保存。接続は共通認証の store と同じものを使う。"""

    def __init__(self, connection_factory: Any) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                yield connection
        except Exception as exc:
            # この store の SQL は PLATFORM_CHAT_ATTACHMENTS だけを読むので、ORA-00942（表が無い）・
            # ORA-00904（列が無い）は共通の DDL が未適用とみなす。
            message = str(exc).upper()
            if "ORA-00942" in message or "ORA-00904" in message:
                raise schema_required() from exc
            raise

    def insert(
        self, record: ChatAttachmentRecord, content: bytes, parsed: ParsedAttachment
    ) -> None:
        # LOB は bytes / str のまま bind する（python-oracledb は 1 GB まで LOB の列へ渡せる）。
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO PLATFORM_CHAT_ATTACHMENTS ("
                "ATTACHMENT_ID, PRODUCT, OWNER_USER_UUID, UPLOADED_BY_USER_UUID, CONVERSATION_ID, "
                "FILE_NAME, MEDIA_TYPE, FILE_KIND, SIZE_BYTES, CONTENT_SHA256, CONTENT, "
                "PARSED_JSON, OUTLINE_JSON, CREATED_AT, EXPIRES_AT) VALUES ("
                ":attachment_id, :product, :owner_user_uuid, :uploaded_by_user_uuid, "
                ":conversation_id, :file_name, :media_type, :file_kind, :size_bytes, "
                ":content_sha256, :content, :parsed_json, :outline_json, :created_at, :expires_at)",
                {
                    "attachment_id": record.attachment_id,
                    "product": record.product,
                    "owner_user_uuid": record.owner_user_uuid,
                    "uploaded_by_user_uuid": record.uploaded_by_user_uuid,
                    "conversation_id": record.conversation_id,
                    "file_name": record.file_name,
                    "media_type": record.media_type,
                    "file_kind": record.kind,
                    "size_bytes": record.size_bytes,
                    "content_sha256": record.content_sha256,
                    "content": content,
                    "parsed_json": parsed.model_dump_json(),
                    "outline_json": record.outline.model_dump_json(),
                    "created_at": record.created_at,
                    "expires_at": record.expires_at,
                },
            )
            conn.commit()

    def get(self, attachment_id: str) -> ChatAttachmentRecord | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM PLATFORM_CHAT_ATTACHMENTS "  # nosec B608 - 固定の列名
                "WHERE ATTACHMENT_ID = :id",
                {"id": attachment_id},
            )
            row = cursor.fetchone()
            return _record_from_row(row) if row is not None else None

    def parsed(self, attachment_id: str) -> ParsedAttachment | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT PARSED_JSON FROM PLATFORM_CHAT_ATTACHMENTS WHERE ATTACHMENT_ID = :id",
                {"id": attachment_id},
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _json_column(ParsedAttachment, row[0])

    def content(self, attachment_id: str) -> bytes | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT CONTENT FROM PLATFORM_CHAT_ATTACHMENTS WHERE ATTACHMENT_ID = :id",
                {"id": attachment_id},
            )
            row = cursor.fetchone()
            if row is None:
                return None
            value = _read_lob(row[0])
        return bytes(value) if value is not None else None

    def replace_state(
        self,
        attachment_id: str,
        outline: AttachmentOutline,
        *,
        expected_revision: int,
        now: datetime,
        parsed: ParsedAttachment | None = None,
    ) -> bool:
        """解析の状態（と結果）を、今の版と期限を確かめて置き換える（#2652）。"""
        binds: dict[str, Any] = {
            "outline_json": outline.model_dump_json(),
            "id": attachment_id,
            "now": now,
            "revision": expected_revision,
        }
        assignments = "OUTLINE_JSON = :outline_json"
        if parsed is not None:
            assignments += ", PARSED_JSON = :parsed_json"
            binds["parsed_json"] = parsed.model_dump_json()
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                # 列は固定の 2 つだけ（値はすべて bind）。
                f"UPDATE PLATFORM_CHAT_ATTACHMENTS SET {assignments} "  # nosec B608
                "WHERE ATTACHMENT_ID = :id AND EXPIRES_AT > :now "
                "AND NVL(JSON_VALUE(OUTLINE_JSON, '$.parse_state.revision' RETURNING NUMBER), 0) "
                "= :revision",
                binds,
            )
            updated = int(cursor.rowcount or 0)
            conn.commit()
        return updated == 1

    def _list(self, where: str, binds: dict[str, Any]) -> list[ChatAttachmentRecord]:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                # 列と条件はこの module の固定の文（値はすべて bind）。
                f"SELECT {_COLUMNS} FROM PLATFORM_CHAT_ATTACHMENTS WHERE {where} "  # nosec B608
                "ORDER BY CREATED_AT, ATTACHMENT_ID",
                binds,
            )
            return [_record_from_row(row) for row in cursor.fetchall()]

    def list_for_conversation(
        self, product: ChatAttachmentProduct, conversation_id: str, *, now: datetime
    ) -> list[ChatAttachmentRecord]:
        return self._list(
            "PRODUCT = :product AND CONVERSATION_ID = :conversation_id AND EXPIRES_AT > :now",
            {"product": product, "conversation_id": conversation_id, "now": now},
        )

    def list_pending(
        self, product: ChatAttachmentProduct, uploaded_by_user_uuid: str, *, now: datetime
    ) -> list[ChatAttachmentRecord]:
        return self._list(
            "PRODUCT = :product AND CONVERSATION_ID IS NULL "
            "AND UPLOADED_BY_USER_UUID = :uploaded_by AND EXPIRES_AT > :now",
            {"product": product, "uploaded_by": uploaded_by_user_uuid, "now": now},
        )

    def bind(
        self,
        attachment_ids: Sequence[str],
        *,
        conversation_id: str,
        owner_user_uuid: str,
        expires_at: datetime,
    ) -> None:
        if not attachment_ids:
            return
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.executemany(
                "UPDATE PLATFORM_CHAT_ATTACHMENTS SET CONVERSATION_ID = :conversation_id, "
                "OWNER_USER_UUID = :owner_user_uuid, EXPIRES_AT = :expires_at "
                "WHERE ATTACHMENT_ID = :attachment_id",
                [
                    {
                        "conversation_id": conversation_id,
                        "owner_user_uuid": owner_user_uuid,
                        "expires_at": expires_at,
                        "attachment_id": attachment_id,
                    }
                    for attachment_id in attachment_ids
                ],
            )
            conn.commit()

    def touch_conversation(
        self, product: ChatAttachmentProduct, conversation_id: str, *, expires_at: datetime
    ) -> None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE PLATFORM_CHAT_ATTACHMENTS SET EXPIRES_AT = :expires_at "
                "WHERE PRODUCT = :product AND CONVERSATION_ID = :conversation_id "
                "AND EXPIRES_AT < :expires_at",
                {"expires_at": expires_at, "product": product, "conversation_id": conversation_id},
            )
            conn.commit()

    def delete(self, attachment_id: str) -> bool:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM PLATFORM_CHAT_ATTACHMENTS WHERE ATTACHMENT_ID = :id",
                {"id": attachment_id},
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
        return deleted > 0

    def delete_conversation(self, product: ChatAttachmentProduct, conversation_id: str) -> int:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM PLATFORM_CHAT_ATTACHMENTS "
                "WHERE PRODUCT = :product AND CONVERSATION_ID = :conversation_id",
                {"product": product, "conversation_id": conversation_id},
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
        return deleted

    def purge_expired(self, *, now: datetime, limit: int) -> int:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM PLATFORM_CHAT_ATTACHMENTS WHERE ATTACHMENT_ID IN ("
                "SELECT ATTACHMENT_ID FROM PLATFORM_CHAT_ATTACHMENTS WHERE EXPIRES_AT <= :now "
                "FETCH FIRST :limit ROWS ONLY)",
                {"now": now, "limit": limit},
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
        return deleted
