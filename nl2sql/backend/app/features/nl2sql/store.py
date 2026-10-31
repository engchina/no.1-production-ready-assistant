"""NL2SQL persistence store boundary.

local / CI は memory store に snapshot を保存する。production（oracle）は entity 単位の
repository（`incremental_store`）を使い、この snapshot の store は使わない（#1758）。旧の
Oracle の snapshot の表（`NL2SQL_STATE_STORE`）を読む store と移行の CLI は、未リリースで
移行するデータが無いため削除した（#1765）。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol


class Nl2SqlStore(Protocol):
    """NL2SQL state snapshot store contract."""

    mode: str

    def load_snapshot(self) -> dict[str, Any] | None:
        """Persisted snapshot を返す。未保存なら None。"""

    def save_snapshot(self, snapshot: dict[str, Any]) -> None:
        """最新 snapshot を保存する。"""

    def check(self) -> tuple[bool, str]:
        """診断用に store 可用性を返す。"""


class MemoryNl2SqlStore:
    """Process-local store used for deterministic local / CI runtime."""

    mode = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None

    def load_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return json.loads(json.dumps(self._snapshot)) if self._snapshot else None

    def save_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = json.loads(json.dumps(snapshot, ensure_ascii=False))

    def check(self) -> tuple[bool, str]:
        return True, "memory store を使用しています。"
