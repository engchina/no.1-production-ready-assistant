"""チャットの添付を使うかの設定（製品ごと。#2517）。

各製品の「運用設定 › チャットの添付」で管理者が切り替える。既定は無効（管理者が有効にするまで、
チャットの入力欄に添付の操作を出さず、添付の API は 403）。上限（大きさ・件数・期限）は共通の
`.env`（`limits`）で、この画面では変えない。

- 保存先は共通の表 `PLATFORM_CHAT_ATTACHMENT_SETTINGS`（製品ごとに 1 行。キーは
  `chat_attachment_setting_key`。DDL は `auth.migrations` の共通の DDL）。保存の後に再起動しないで
  効かせるため `.env` には置かない。
- 読むときは短い cache（`CACHE_SECONDS`）を持つ。読めないとき（表が未作成・DB の停止中）は無効として
  扱う（添付を受け付けない側に倒す）。読めなかった理由は `failure` で返し、添付の受け付け・送信は
  「管理者が無効にしている」ではなくその理由のエラーにする（#2555）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, params
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel

from .errors import (
    CHAT_ATTACHMENT_SCHEMA_REQUIRED_CODE,
    ChatAttachmentError,
    schema_required,
    settings_unavailable,
)
from .limits import chat_attachment_limits

logger = logging.getLogger(__name__)

CHAT_ATTACHMENT_SETTINGS_TABLE = "PLATFORM_CHAT_ATTACHMENT_SETTINGS"
CHAT_ATTACHMENT_SETTING_KEY_PREFIX = "chat_attachments"
CACHE_SECONDS = 10.0


def chat_attachment_setting_key(product_key: str) -> str:
    """製品の行のキー（`AuthService.product_key` から決める）。"""
    return f"{CHAT_ATTACHMENT_SETTING_KEY_PREFIX}:{product_key}"


class ChatAttachmentSettingsRecord(BaseModel):
    enabled: bool = False
    updated_at: datetime | None = None


class ChatAttachmentSettingsStore(Protocol):
    def load(self) -> ChatAttachmentSettingsRecord | None: ...
    def save(self, record: ChatAttachmentSettingsRecord) -> ChatAttachmentSettingsRecord: ...


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryChatAttachmentSettingsStore:
    """プロセスの中だけの保存（テスト・DB を使わない構成）。"""

    def __init__(self, record: ChatAttachmentSettingsRecord | None = None) -> None:
        self._lock = threading.Lock()
        self._record = record

    def load(self) -> ChatAttachmentSettingsRecord | None:
        with self._lock:
            return self._record

    def save(self, record: ChatAttachmentSettingsRecord) -> ChatAttachmentSettingsRecord:
        saved = ChatAttachmentSettingsRecord(enabled=record.enabled, updated_at=_now())
        with self._lock:
            self._record = saved
        return saved


class OracleChatAttachmentSettingsStore:
    """`PLATFORM_CHAT_ATTACHMENT_SETTINGS` の 1 製品の行。接続は共通認証の store と同じ。"""

    def __init__(self, connection_factory: Any, setting_key: str) -> None:
        self._connection_factory = connection_factory
        self._setting_key = setting_key

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                yield connection
        except Exception as exc:
            # この store の SQL はこの表だけを読むので、ORA-00942 は共通の DDL が未適用とみなす。
            if "ORA-00942" in str(exc).upper():
                raise schema_required() from exc
            raise

    def load(self) -> ChatAttachmentSettingsRecord | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT ENABLED, UPDATED_AT FROM PLATFORM_CHAT_ATTACHMENT_SETTINGS "
                "WHERE SETTING_KEY = :setting_key",
                {"setting_key": self._setting_key},
            )
            row = cursor.fetchone()
        if row is None:
            return None
        updated_at = row[1]
        if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return ChatAttachmentSettingsRecord(
            enabled=bool(int(row[0] or 0)),
            updated_at=updated_at if isinstance(updated_at, datetime) else None,
        )

    def save(self, record: ChatAttachmentSettingsRecord) -> ChatAttachmentSettingsRecord:
        now = _now()
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                MERGE INTO PLATFORM_CHAT_ATTACHMENT_SETTINGS target
                USING (SELECT :setting_key AS SETTING_KEY FROM DUAL) source
                   ON (target.SETTING_KEY = source.SETTING_KEY)
                 WHEN MATCHED THEN UPDATE SET ENABLED = :enabled, UPDATED_AT = :updated_at
                 WHEN NOT MATCHED THEN INSERT (SETTING_KEY, ENABLED, UPDATED_AT)
                      VALUES (:setting_key, :enabled, :updated_at)
                """,
                {
                    "setting_key": self._setting_key,
                    "enabled": 1 if record.enabled else 0,
                    "updated_at": now,
                },
            )
            conn.commit()
        return ChatAttachmentSettingsRecord(enabled=record.enabled, updated_at=now)


class ChatAttachmentSettingsProvider:
    """添付を使うか（短い cache 付き）。読めないときは無効。"""

    def __init__(
        self,
        store: ChatAttachmentSettingsStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, bool] | None = None
        # 最後に読めなかった理由（読めたら None）。エラーは呼ぶたびに作る。
        self._failure: Callable[[], ChatAttachmentError] | None = None

    def enabled(self) -> bool:
        now = self._clock()
        with self._lock:
            if self._cached is not None and now - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
        failure: Callable[[], ChatAttachmentError] | None = None
        try:
            record = self.store.load()
            value = bool(record.enabled) if record is not None else False
        except Exception as exc:  # 表が未作成・DB の停止中は受け付けない側に倒す
            logger.warning(
                "chat_attachment_settings_unavailable", extra={"error_type": type(exc).__name__}
            )
            value = False
            schema_missing = (
                isinstance(exc, ChatAttachmentError)
                and exc.code == CHAT_ATTACHMENT_SCHEMA_REQUIRED_CODE
            )
            failure = schema_required if schema_missing else settings_unavailable
        with self._lock:
            self._cached = (now, value)
            self._failure = failure
        return value

    def failure(self) -> ChatAttachmentError | None:
        """最後に読んだときに設定を読めなかった理由（読めたら None）。`enabled` の後に呼ぶ。"""
        with self._lock:
            failure = self._failure
        return failure() if failure is not None else None

    def load(self) -> ChatAttachmentSettingsRecord:
        return self.store.load() or ChatAttachmentSettingsRecord()

    def save(self, record: ChatAttachmentSettingsRecord) -> ChatAttachmentSettingsRecord:
        saved = self.store.save(record)
        with self._lock:
            self._cached = (self._clock(), saved.enabled)
            self._failure = None
        return saved


class ChatAttachmentSettingsData(BaseModel):
    """「チャットの添付」の画面の値（上限は共通の `.env` の値で、この画面では変えない）。"""

    enabled: bool
    updated_at: datetime | None = None
    max_bytes: int
    max_files: int
    ttl_hours: int


class ChatAttachmentSettingsUpdate(BaseModel):
    enabled: bool


def build_chat_attachment_settings_router(
    *,
    load: Callable[[], ChatAttachmentSettingsRecord],
    save: Callable[[ChatAttachmentSettingsRecord], ChatAttachmentSettingsRecord],
    get_settings: Callable[[], object],
    write_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """「チャットの添付」の設定 API（`/chat-attachments`。製品は `/settings` の下に置く）。

    `load` / `save` は製品の共通認証のサービスの `chat_attachment_settings`（provider）を渡す。
    表が未作成なら 409（システムテーブルの作成・更新が必要）。権限は製品の API の manifest が
    判定する。
    """
    router = APIRouter()

    def data(record: ChatAttachmentSettingsRecord) -> ChatAttachmentSettingsData:
        limits = chat_attachment_limits(get_settings())
        return ChatAttachmentSettingsData(
            enabled=record.enabled,
            updated_at=record.updated_at,
            max_bytes=limits.max_bytes,
            max_files=limits.max_files,
            ttl_hours=limits.ttl_hours,
        )

    @router.get("/chat-attachments", response_model=ApiResponse[ChatAttachmentSettingsData])
    def get_chat_attachment_settings() -> ApiResponse[ChatAttachmentSettingsData]:
        """チャットの添付を使うか（未設定なら無効）と、共通の上限。"""
        return ApiResponse(data=data(load()))

    @router.put(
        "/chat-attachments",
        response_model=ApiResponse[ChatAttachmentSettingsData],
        dependencies=list(write_dependencies),
    )
    def update_chat_attachment_settings(
        payload: ChatAttachmentSettingsUpdate,
    ) -> ApiResponse[ChatAttachmentSettingsData]:
        """チャットの添付を使うかを保存する。

        同じプロセスはすぐ、ほかのプロセスは cache の期限の後に効く。
        """
        return ApiResponse(data=data(save(ChatAttachmentSettingsRecord(enabled=payload.enabled))))

    return router
