"""3 製品共通の軌跡の本文記録。未設定・読み出し失敗は無効。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, params
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel, ConfigDict

from .auth.errors import SecurityApiError

logger = logging.getLogger(__name__)
TRACE_RECORDING_TABLE = "PLATFORM_TRACE_RECORDING_SETTINGS"
CACHE_SECONDS = 5.0


class TraceRecordingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    updated_at: datetime | None = None


class TraceRecordingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class TraceRecordingStore(Protocol):
    def load(self) -> TraceRecordingSettings | None: ...
    def save(self, enabled: bool) -> TraceRecordingSettings: ...


class InMemoryTraceRecordingStore:
    def __init__(self) -> None:
        self.record = TraceRecordingSettings()

    def load(self) -> TraceRecordingSettings:
        return self.record

    def save(self, enabled: bool) -> TraceRecordingSettings:
        self.record = TraceRecordingSettings(enabled=enabled, updated_at=datetime.now(UTC))
        return self.record


class OracleTraceRecordingStore:
    def __init__(self, connection_factory: Any) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                yield connection
        except SecurityApiError:
            raise
        except Exception as exc:
            if "ORA-00942" in str(exc).upper():
                raise SecurityApiError(
                    409,
                    "共通のシステムテーブルを初期化してください。",
                    code="SECURITY_SCHEMA_MIGRATION_REQUIRED",
                ) from exc
            raise SecurityApiError(
                503,
                "軌跡の記録設定を読み書きできません。データベースの状態を確認してください。",
                code="TRACE_RECORDING_SETTINGS_UNAVAILABLE",
                retryable=True,
            ) from exc

    def load(self) -> TraceRecordingSettings | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT ENABLED, UPDATED_AT FROM PLATFORM_TRACE_RECORDING_SETTINGS WHERE ID = 1"
            )
            row = cursor.fetchone()
        if row is None:
            return None
        updated = row[1]
        if isinstance(updated, datetime) and updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return TraceRecordingSettings(enabled=bool(row[0]), updated_at=updated)

    def save(self, enabled: bool) -> TraceRecordingSettings:
        now = datetime.now(UTC)
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """MERGE INTO PLATFORM_TRACE_RECORDING_SETTINGS target
                USING (SELECT 1 AS ID FROM DUAL) source ON (target.ID = source.ID)
                WHEN MATCHED THEN UPDATE SET ENABLED = :enabled, UPDATED_AT = :updated_at
                WHEN NOT MATCHED THEN INSERT (ID, ENABLED, UPDATED_AT)
                  VALUES (1, :enabled, :updated_at)""",
                {"enabled": int(enabled), "updated_at": now},
            )
            conn.commit()
        return TraceRecordingSettings(enabled=enabled, updated_at=now)


class TraceRecordingProvider:
    def __init__(
        self, store: TraceRecordingStore, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, bool] | None = None

    def enabled(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
            try:
                value = self.load().enabled
            except Exception as exc:
                logger.warning(
                    "trace_recording_settings_unavailable", extra={"error_type": type(exc).__name__}
                )
                value = False
            self._cached = (now, value)
            return value

    def load(self) -> TraceRecordingSettings:
        return self.store.load() or TraceRecordingSettings()

    def save(self, enabled: bool) -> TraceRecordingSettings:
        saved = self.store.save(enabled)
        with self._lock:
            self._cached = (self._clock(), saved.enabled)
        return saved


def build_trace_recording_router(
    *,
    provider: Callable[[], TraceRecordingProvider],
    write_dependencies: list[params.Depends] | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/trace-recording", response_model=ApiResponse[TraceRecordingSettings])
    def get_settings() -> ApiResponse[TraceRecordingSettings]:
        return ApiResponse(data=provider().load())

    @router.put(
        "/trace-recording",
        response_model=ApiResponse[TraceRecordingSettings],
        dependencies=write_dependencies or [],
    )
    def update_settings(payload: TraceRecordingUpdate) -> ApiResponse[TraceRecordingSettings]:
        return ApiResponse(data=provider().save(payload.enabled))

    return router
