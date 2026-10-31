"""Run の取消で、実行中のツールの呼び出し（MCP の HTTP）を打ち切る（#2691）。

ツールの handler は worker thread で同期の httpx を使って呼び先へ送るため、asyncio の task を
取り消しても thread と接続は残り、呼び先（RAG の検索など）は取消を知らずに検索・LLM の呼び出しを
続ける。そこで実行中の 1 回のツールの呼び出しごとに打ち切りの印（`CallAbort`）を作り、

- 実行の文脈（`ContextVar`。`asyncio.to_thread` が thread に引き継ぐ）に置いて、MCP の送信
  （`tools._post_mcp_message`）が送る前に確かめる（打ち切った後は送らない・再試行しない）。
- 送信中の接続の socket を登録し、打ち切るときに `shutdown` する（読みを待っている thread が
  すぐに戻り、呼び先は切断を検知する）。打ち切るのは状態を変えない手順と読み取り専用のツール
  だけで、書き込みのツールは送った後は結果を待って事実のまま記録する（#2609・#2601）。
- 同じプロセスの取消（`runtime._cancel_run_locked`）は `abort_run_calls` ですぐに打ち切る。別の
  プロセスの取消は、実行している側が保存先を定期的に読み直して知る（`builtin_runtime`）。
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)

# 打ち切った呼び出しの結果の error_code（step は失敗ではなく取消として記録する）。
TOOL_CALL_ABORTED_CODE = "runtime.tool_call_aborted"
TOOL_CALL_ABORTED_MESSAGE = "実行の取消で、ツールの呼び出しを打ち切りました（結果は使いません）。"

# 打ち切りの理由（ログ・step の詳細の `reason`）。
REASON_RUN_CANCELLED = "run_cancelled"
REASON_RUN_ENDED = "run_ended"
REASON_TASK_CANCELLED = "task_cancelled"


class CallAbort:
    """1 回のツールの呼び出しの打ち切りの印（thread をまたいで共有する）。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._aborted_at: float | None = None
        self._interrupted = False
        self._sockets: set[socket.socket] = set()

    @property
    def aborted(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def interrupted(self) -> bool:
        """打ち切りで、送信を止めた・送信中の接続を切ったか（結果を取消として記録する）。"""
        return self._interrupted

    def seconds_since_abort(self) -> float | None:
        aborted_at = self._aborted_at
        return None if aborted_at is None else max(0.0, monotonic() - aborted_at)

    def abort(self, reason: str) -> bool:
        """打ち切る（最初の 1 回だけ True）。登録した接続はすぐに切る。"""
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            self._aborted_at = monotonic()
            sockets = list(self._sockets)
            self._sockets.clear()
        for sock in sockets:
            _shutdown(sock)
        return True

    def note_interrupted(self) -> None:
        """送信を止めた・接続を切った（MCP の送信が打ち切りの例外を送出するときに呼ぶ）。"""
        self._interrupted = True

    def watch_socket(self, sock: socket.socket) -> None:
        """送信中の接続を登録する（打ち切り済みならすぐに切る）。"""
        with self._lock:
            if self._reason is None:
                self._sockets.add(sock)
                return
        _shutdown(sock)

    def forget_socket(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)

    def httpcore_trace(self) -> Any:
        """httpx の request の `trace` 拡張（接続を作ったときに socket を登録する）。"""

        def trace(event_name: str, info: dict[str, Any]) -> None:
            if event_name not in _CONNECT_EVENTS:
                return
            stream = info.get("return_value")
            get_extra_info = getattr(stream, "get_extra_info", None)
            sock = get_extra_info("socket") if callable(get_extra_info) else None
            if isinstance(sock, socket.socket):
                self.watch_socket(sock)

        return trace


# httpcore の trace の、接続を作り終えた event（TCP と unix socket）。
_CONNECT_EVENTS = frozenset(
    {"connection.connect_tcp.complete", "connection.connect_unix_socket.complete"}
)


def _shutdown(sock: socket.socket) -> None:
    # close ではなく shutdown にする（読みを待っている別の thread が戻る）。
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


_current: ContextVar[CallAbort | None] = ContextVar("agent_tool_call_abort", default=None)
_registry_lock = threading.Lock()
_registry: dict[str, set[CallAbort]] = {}


def current_call_abort() -> CallAbort | None:
    """この文脈（実行中のツールの呼び出し）の打ち切りの印。Run の外の呼び出しは None。"""
    return _current.get()


@contextmanager
def bind_call_abort(run_id: str) -> Iterator[CallAbort]:
    """ツールの呼び出しの打ち切りの印を作り、この文脈と Run の一覧に置く（抜けるときに外す）。"""
    abort = CallAbort(run_id)
    with _registry_lock:
        _registry.setdefault(run_id, set()).add(abort)
    token = _current.set(abort)
    try:
        yield abort
    finally:
        _current.reset(token)
        with _registry_lock:
            calls = _registry.get(run_id)
            if calls is not None:
                calls.discard(abort)
                if not calls:
                    _registry.pop(run_id, None)


def abort_run_calls(run_id: str, reason: str = REASON_RUN_CANCELLED) -> int:
    """このプロセスで実行中の、この Run のツールの呼び出しをすべて打ち切る（打ち切った数）。"""
    with _registry_lock:
        calls = list(_registry.get(run_id, ()))
    count = sum(1 for abort in calls if abort.abort(reason))
    if count:
        logger.info(
            "agent_tool_calls_aborted",
            extra={"run_id": run_id, "reason": reason, "count": count},
        )
    return count
