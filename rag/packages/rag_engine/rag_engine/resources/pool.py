"""アプリが所有するモデルプール。共有 singleton を要求しない。"""

from collections.abc import Callable
from threading import RLock
from typing import Any


class ModelPool:
    """signature が同じモデルを再利用し、close 時に自分のモデルだけを解放する。"""

    def __init__(self):
        self._lock = RLock()
        self._entries: dict[str, tuple[object, Any, Callable[[], None] | None]] = {}

    def get(self, key, loader, *, signature=None, unloader=None):
        """モデルを一度だけ構築する。loader の失敗はキャッシュしない。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] == signature:
                return entry[1]
            self.unload(key)
            value = loader()
            self._entries[key] = (signature, value, unloader)
            return value

    def unload(self, key: str) -> bool:
        """指定モデルを解放する。unloader の失敗は呼び出し元へ通知する。"""
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return False
            if entry[2] is not None:
                entry[2]()
            return True

    def unload_all(self) -> list[str]:
        """すべてのモデルを解放する。複数の解放失敗は ExceptionGroup で返す。"""
        with self._lock:
            keys = list(self._entries)
            failures = []
            for key in keys:
                try:
                    self.unload(key)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise ExceptionGroup("model release failed", failures)
            return keys

    def loaded(self) -> list[str]:
        """現在このインスタンスが保持する key を返す。"""
        with self._lock:
            return list(self._entries)
