"""Oracle の接続 pool の共有（3 製品共通。#793）。

python-oracledb に直接依存しない（`oracledb` は使うときに import する。テストは loader で
差し替える）。

- 接続引数（DSN・ユーザー・パスワード・Wallet / TLS の引数を含む）の指紋ごとに pool を 1 つだけ
  持ち、最初に借りるときに作る。引数が変わったら（DB の設定を保存したとき等）作り直す
- 新しい接続ごとに `init_oracle_session`（session_callback）を当てる
- 借りた接続は、例外のときは rollback してから返す。返すと python-oracledb が未コミットの変更を
  rollback する（呼び出し側は今までどおり自分で commit する）
- 終了時と DB の設定の保存時に `close()` で閉じる
- DB の停止中に作った pool が DB の起動後も接続を返さない（`DPY-4005` が続く）ときは、その pool を
  捨てて新しい pool で 1 回だけやり直す。接続できない（`DPY-6005`・`ORA-125xx` など）ときは pool を
  捨てて（次の要求は新しい pool から始める）その例外を返す（#820。`recover_pool_error`）

接続のたびに Wallet / mTLS の handshake と認証をやり直さないためのもの。DB が未設定かどうかの判定は
呼び出し側（製品）が持ち、未設定なら pool を作らない。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal

from .oracle_errors import (
    is_oracle_connection_error,
    is_oracle_pool_wait_timeout,
    oracle_error_codes,
)
from .oracle_session import init_oracle_session

logger = logging.getLogger(__name__)

# 既定の大きさ。共通認証・システムテーブル・DB の状態確認のような短い処理を前提にする。
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 4
# pool が埋まっているときに待つ上限（ミリ秒）。無期限に待たない。
DEFAULT_WAIT_TIMEOUT_MS = 30_000
# 使われていない接続（min を超える分）を閉じるまでの秒数。
DEFAULT_IDLE_TIMEOUT_SECONDS = 300
# 借りるとき、この秒数より長く使っていない接続だけを ping で確かめる（python-oracledb の既定）。
DEFAULT_PING_INTERVAL_SECONDS = 60

OracledbLoader = Callable[[], Any]
SessionCallback = Callable[[Any, str | None], None]
# 捨てた pool を閉じる処理を走らせる（既定は daemon thread。テストは同期で走らせる）。
PoolCloser = Callable[[Callable[[], None]], None]


def close_in_background(task: Callable[[], None]) -> None:
    """捨てた pool を別のスレッドで閉じる（#820）。

    python-oracledb の `pool.close()` は、pool が試している接続（停止中の DB への接続など）が
    終わるまで戻らないことがある（`tcp_connect_timeout` の間。手元で 16 秒を観測）。要求のスレッドと
    pool の lock を、その間止めない。
    """
    threading.Thread(target=task, name="oracle-pool-close", daemon=True).start()


def _load_oracledb() -> Any:
    return importlib.import_module("oracledb")


def connect_kwargs_fingerprint(connect_kwargs: Mapping[str, object]) -> str:
    """接続引数の指紋（資格情報を含むため、生値は保持・出力しない）。"""
    payload = json.dumps(
        sorted((str(key), repr(value)) for key, value in connect_kwargs.items()),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OraclePoolSize:
    """pool の大きさ（min ≤ max、どちらも 1 以上に丸める）。"""

    min: int = DEFAULT_POOL_MIN
    max: int = DEFAULT_POOL_MAX

    @classmethod
    def of(cls, minimum: object, maximum: object) -> OraclePoolSize:
        low = max(1, _as_int(minimum, DEFAULT_POOL_MIN))
        high = max(low, _as_int(maximum, DEFAULT_POOL_MAX))
        return cls(min=low, max=high)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


class SharedOraclePool:
    """接続引数ごとに 1 つの python-oracledb の pool を遅延作成して共有する（スレッド安全）。"""

    def __init__(
        self,
        *,
        name: str,
        size: OraclePoolSize | None = None,
        wait_timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
        ping_interval_seconds: int = DEFAULT_PING_INTERVAL_SECONDS,
        session_callback: SessionCallback | None = init_oracle_session,
        oracledb_loader: OracledbLoader = _load_oracledb,
        pool_closer: PoolCloser = close_in_background,
    ) -> None:
        self.name = name
        self._size = size or OraclePoolSize()
        self._wait_timeout_ms = max(0, int(wait_timeout_ms))
        self._idle_timeout_seconds = max(0, int(idle_timeout_seconds))
        self._ping_interval_seconds = int(ping_interval_seconds)
        self._session_callback = session_callback
        self._oracledb_loader = oracledb_loader
        self._lock = threading.Lock()
        self._pool: Any | None = None
        self._fingerprint: str | None = None
        # 設定の変更で入れ替えた pool のうち、貸し出し中の接続があって閉じられなかったもの。
        self._retired: list[Any] = []
        self._pool_closer = pool_closer

    @property
    def size(self) -> OraclePoolSize:
        return self._size

    def resize(self, size: OraclePoolSize) -> None:
        """大きさを変える。今の pool は閉じ、次に借りるときに新しい大きさで作る。"""
        with self._lock:
            if size == self._size:
                return
            self._size = size
            self._retire_current_locked()

    @contextmanager
    def connection(
        self,
        connect_kwargs: Mapping[str, object],
        *,
        on_acquire: Callable[[Any], None] | None = None,
    ) -> Iterator[Any]:
        """pool から接続を借りる。`with` を抜けると返す（例外のときは rollback してから返す）。"""
        connection = self.acquire(connect_kwargs)
        try:
            if on_acquire is not None:
                on_acquire(connection)
            yield connection
        except BaseException:
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - 切れた接続の rollback の失敗で元の例外を隠さない
                logger.debug("oracle_pool_rollback_failed", extra={"pool": self.name})
            finally:
                _release(connection)
            raise
        else:
            _release(connection)

    def acquire(self, connect_kwargs: Mapping[str, object]) -> Any:
        """pool から接続を借りる（返すのは呼び出し側。`connection.close()` で pool に戻る）。

        pool が壊れている（DB の停止中に作られ、DB の起動後も接続を返さない）ときは、新しい pool で
        1 回だけやり直す（#820）。やり直しも失敗したら、新しい pool の例外（本当の接続エラー）を
        返す。
        """
        pool = self._pool_for(connect_kwargs)
        try:
            return pool.acquire()
        except Exception as exc:
            action = recover_pool_error(pool, exc)
            if action == "raise":
                raise
            self._discard(pool, reason=action, exc=exc)
            if action == "discard":
                raise
        fresh = self._pool_for(connect_kwargs)
        try:
            return fresh.acquire()
        except Exception as exc:
            if recover_pool_error(fresh, exc) != "raise":
                # 次の要求は、また新しい pool から始める（壊れた pool を使い続けない）。
                self._discard(fresh, reason="retry_failed", exc=exc)
            raise

    def close(self) -> None:
        """pool を閉じる（終了時・DB の設定の保存時）。次に借りるときに作り直す。"""
        with self._lock:
            pools = [*self._retired, *([self._pool] if self._pool is not None else [])]
            self._retired = []
            self._pool = None
            self._fingerprint = None
        for pool in pools:
            with suppress(Exception):
                pool.close(force=True)

    def _discard(self, pool: Any, *, reason: str, exc: BaseException) -> None:
        """今の pool が `pool` なら捨てる（別のスレッドが作り直していれば何もしない）。

        閉じるのは別のスレッド（`pool_closer`）。貸し出し中の接続があって閉じられなければ、今までどおり
        後で閉じる（その接続を使っている処理は止めない）。
        """
        target: Any = pool
        with self._lock:
            if id(self._pool) != id(target):
                return
            self._pool = None
            self._fingerprint = None

        def close() -> None:
            try:
                target.close()
            except Exception:  # noqa: BLE001 - 貸し出し中。次の作成時・終了時に閉じ直す
                with self._lock:
                    self._retired.append(target)

        self._pool_closer(close)
        logger.warning(
            "oracle_pool_discarded",
            extra={
                "pool": self.name,
                "reason": reason,
                "exception_type": type(exc).__name__,
                "error_codes": ",".join(oracle_error_codes(exc)) or None,
            },
        )

    def _pool_for(self, connect_kwargs: Mapping[str, object]) -> Any:
        fingerprint = connect_kwargs_fingerprint(connect_kwargs)
        with self._lock:
            if self._pool is not None and self._fingerprint == fingerprint:
                return self._pool
            self._retire_current_locked()
            self._close_retired_locked()
            pool = self._create_pool(connect_kwargs)
            self._pool = pool
            self._fingerprint = fingerprint
            logger.info(
                "oracle_pool_created",
                extra={"pool": self.name, "pool_min": self._size.min, "pool_max": self._size.max},
            )
            return pool

    def _create_pool(self, connect_kwargs: Mapping[str, object]) -> Any:
        oracledb = self._oracledb_loader()
        kwargs: dict[str, Any] = dict(connect_kwargs)
        kwargs.update(
            min=self._size.min,
            max=self._size.max,
            increment=1,
            getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
            wait_timeout=self._wait_timeout_ms,
            timeout=self._idle_timeout_seconds,
            ping_interval=self._ping_interval_seconds,
        )
        if self._session_callback is not None:
            kwargs["session_callback"] = self._session_callback
        return oracledb.create_pool(**kwargs)

    def _retire_current_locked(self) -> None:
        if self._pool is None:
            return
        pool = self._pool
        self._pool = None
        self._fingerprint = None
        try:
            # 貸し出し中の接続があれば失敗する。その接続を使っている処理は止めない。
            pool.close()
        except Exception:  # noqa: BLE001 - 後で閉じ直す
            self._retired.append(pool)

    def _close_retired_locked(self) -> None:
        remaining: list[Any] = []
        for pool in self._retired:
            try:
                pool.close()
            except Exception:  # noqa: BLE001 - まだ貸し出し中
                remaining.append(pool)
        self._retired = remaining


PoolRecovery = Literal["retry", "discard", "raise"]


def pool_exhausted(pool: Any) -> bool:
    """貸し出し中の接続が上限に達しているか（本当の枯渇。分からなければ False）。"""
    try:
        busy = int(pool.busy)
        maximum = int(pool.max)
    except Exception:  # noqa: BLE001 - fake の pool や閉じた pool は「分からない」
        return False
    return maximum > 0 and busy >= maximum


def recover_pool_error(pool: Any, exc: BaseException) -> PoolRecovery:
    """`pool.acquire()` の失敗をどう扱うか（#820）。

    - `retry`：pool の待ちの timeout（`DPY-4005`）で、貸し出し中の接続は上限に達していない。pool が
      接続を作れない状態のまま止まっているので、捨てて新しい pool でやり直す
    - `discard`：接続できない（停止中・起動中・ネットワーク・資格情報）。python-oracledb が新しい
      接続を試した結果なので、同じ要求の中ではやり直さず、pool だけを捨てて例外を返す
    - `raise`：それ以外（本当の枯渇・SQL のエラーなど）。pool はそのまま使う
    """
    if is_oracle_pool_wait_timeout(exc):
        return "raise" if pool_exhausted(pool) else "retry"
    if is_oracle_connection_error(exc):
        return "discard"
    return "raise"


def _release(connection: Any) -> None:
    """pool に返す（切れた接続は python-oracledb が捨てる）。"""
    with suppress(Exception):
        connection.close()


__all__ = [
    "DEFAULT_POOL_MAX",
    "PoolCloser",
    "close_in_background",
    "DEFAULT_POOL_MIN",
    "OraclePoolSize",
    "PoolRecovery",
    "SharedOraclePool",
    "connect_kwargs_fingerprint",
    "pool_exhausted",
    "recover_pool_error",
]
