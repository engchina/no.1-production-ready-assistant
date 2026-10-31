"""worker / readiness 用の短時間 cache 付き system schema probe。"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from app.rag.system_schema import system_schema_manager

RuntimeSchemaState = Literal["ready", "setup_required", "unavailable"]


class SystemSchemaRuntime:
    """高頻度 worker poll から Oracle dictionary 参照を抑える。"""

    def __init__(self, *, ttl_seconds: float = 5.0) -> None:
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._state: RuntimeSchemaState | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._checked_at = 0.0
        self._state = None

    async def state(self) -> RuntimeSchemaState:
        now = time.monotonic()
        if self._state is not None and now - self._checked_at < self._ttl_seconds:
            return self._state
        async with self._lock:
            now = time.monotonic()
            if self._state is not None and now - self._checked_at < self._ttl_seconds:
                return self._state
            try:
                status = await asyncio.to_thread(system_schema_manager.status)
            except Exception:
                state: RuntimeSchemaState = "unavailable"
            else:
                state = (
                    "ready"
                    if status["status"] == "ready"
                    and status["operation_state"]["status"] != "running"
                    else "setup_required"
                )
            self._state = state
            self._checked_at = time.monotonic()
            return state

    async def is_ready(self) -> bool:
        return await self.state() == "ready"


system_schema_runtime = SystemSchemaRuntime()


__all__ = ["RuntimeSchemaState", "SystemSchemaRuntime", "system_schema_runtime"]
