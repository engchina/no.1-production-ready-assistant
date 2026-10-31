"""ログインの試行の回数の制限（#1087。3 製品・全 worker で共有する。#1173）。

構成管理者（`system_admin`）は DB のユーザーではないため、DB ユーザーのロック
（`FAILED_LOGIN_COUNT` / `LOCKED_UNTIL`）を使えない。代わりに、共通のログインの入口
（`AuthService.login`）で、失敗の回数を次の 2 つの単位で数え、上限に達したら一定時間 429 を返す。

- ログイン ID と送信元 IP の組: 1 つの端末から 1 つのログイン ID のパスワードを
  何回も試すのを止める。
  ログイン ID だけで数えると、攻撃者が別の IP から失敗を重ねて構成管理者（緊急用の全権限の管理者）を
  常に締め出せるため、送信元 IP と組にする（Issue #1087 の案 1 の副作用を避ける）。
- 送信元 IP: 1 つの端末から多くのログイン ID を試す（password spraying）のを止める。

ユーザーの有無・状態は数え方と応答に影響しない（存在しないユーザーも同じ。#1105 と同じ考え方）。
成功したら、そのログイン ID と送信元 IP の組の回数を消す（送信元 IP の回数は残す）。

失敗は直近の窓（`window_seconds`）の中の時刻で数える（sliding log）。上限に達したら、窓の中で
上限から数えて最も古い失敗が窓から出るまで拒否する。

記録の置き場所（#1173）:

- 3 製品は同じ Oracle schema を共有するので、記録を共通のテーブル `PLATFORM_LOGIN_ATTEMPTS`
  に置き、3 製品・gunicorn の全 worker・再起動をまたいで同じ回数を数える
  （`OracleLoginAttemptStore`）。時刻は DB の時計（UTC）で比べ、ホストの時計のずれに依らない。
- ログイン ID・送信元 IP はそのまま保存しない。共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`
  から導いた鍵の HMAC-SHA256（16 進 64 文字）だけを保存し、テーブルを読めても利用者の ID や
  IP は分からない（鍵が無ければ総当たりで戻せない）。鍵が無い（空・32 文字未満）ときは
  DB に記録せず、プロセス内だけで数える（読める形を残さない。Agent の `secret_box` と同じ考え方）。
- DB が使えない（接続できない・テーブルが無い）ときは、プロセス内の記録
  （`MemoryLoginAttemptStore`）に自動で切り替えて制限を続け、警告をログに出す（fail open に
  しない。構成管理者は DB の停止中もログインできる。#1150）。`shared_retry_seconds` の後に
  DB を試し直し、戻れば DB に戻る。DB に戻った後も、停止中にプロセス内で数えた失敗は
  窓の中なら数える。

同時の試行（2 worker・2 製品が同時に数える）で上限を超えないよう、照合の前に試行の行を足して
commit してから、窓の中の行を数える（予約）。自分以外の行が上限以上なら、自分の行を消して拒否する。
照合に失敗したら行を残し（失敗の記録になる）、成功・照合以外のエラーでは行を消す。
許可された試行のうち、最後に commit したものは、それより前に commit した許可された行をすべて
数えるので、許可される失敗は上限を超えない（READ COMMITTED のままでよく、表の lock は要らない）。
同時の試行どうしが互いの行を数えて両方とも拒否されることはある（安全側）。

送信元 IP は `request.client.host` で、Nginx の後ろでは uvicorn の proxy headers（gunicorn の
`forwarded_allow_ips` の既定は 127.0.0.1 / ::1）が `X-Forwarded-For` の右端の信頼しない host に
置き換える。アプリは `X-Forwarded-For` を直接読まない。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Protocol, TypeVar
from uuid import uuid4

logger = logging.getLogger(__name__)

LOGIN_ATTEMPTS_TABLE = "PLATFORM_LOGIN_ATTEMPTS"
# 共有の鍵の最短の長さ（サービストークンの署名鍵と同じ）。
LOGIN_THROTTLE_MIN_SECRET_LENGTH = 32
# DB が使えなかった後、プロセス内で数え続ける秒数。これを過ぎたら DB を試し直す。
DEFAULT_SHARED_RETRY_SECONDS = 30.0
# 接続・再試行・SQL・後処理を含む共有ストアの 1 操作の待機上限。
DEFAULT_SHARED_TIMEOUT_SECONDS = 2.0
_Result = TypeVar("_Result")
# 失敗を記録するついでに消す、期限切れの行の上限（1 回の DELETE を短くする）。
PURGE_BATCH_ROWS = 1000
# 1 回の確認で読む行の上限（鍵ごとの行は上限＋同時の試行の数しか残らない）。
MAX_ROWS_PER_CHECK = 1000
# プロセス内の鍵の数がこれを超えたら、窓を過ぎた鍵を掃除する（多くの IP からの試行で増え続けない）。
_PRUNE_THRESHOLD = 10_000
# 用途ごとの鍵を共有の secret から導くときのラベル（鍵を変えると数え直しになる）。
_KEY_LABEL = b"platform-login-throttle/v1"
_ERROR_CODE_PATTERN = re.compile(r"\b(?:ORA|DPY)-\d{4,5}\b")


@dataclass(frozen=True)
class LoginThrottleLimits:
    """上限。0 以下の上限はその単位の制限を無効にする。"""

    per_login_and_ip: int
    per_ip: int
    window_seconds: float


@dataclass(frozen=True)
class AttemptRow:
    """窓の中の 1 回の試行。`age_seconds` は記録してからの秒数（記録した側の時計）。"""

    attempt_id: str
    age_seconds: float


AttemptRows = dict[str, list[AttemptRow]]


class LoginAttemptStore(Protocol):
    """試行の記録の置き場所。鍵（`key_hash`）は呼び出し側が作る不透明な文字列。"""

    def reserve(
        self, attempt_id: str, key_hashes: Sequence[str], window_seconds: float
    ) -> AttemptRows:
        """鍵ごとに試行の行を足して確定し、窓の中の行（自分を含む）を新しい順に返す。"""
        ...

    def recent(self, key_hashes: Sequence[str], window_seconds: float) -> AttemptRows:
        """窓の中の行を新しい順に返す（足さない）。"""
        ...

    def release(self, attempt_id: str, key_hashes: Sequence[str]) -> None:
        """自分の試行の行を消す（拒否・照合以外のエラー）。"""
        ...

    def clear(self, attempt_id: str, key_hashes: Sequence[str], cleared_key_hash: str) -> None:
        """成功: 自分の試行の行と、`cleared_key_hash` のすべての行を消す。"""
        ...

    def purge_expired(self, window_seconds: float) -> int:
        """窓を過ぎた行を、`PURGE_BATCH_ROWS` 件まで消す。消した件数を返す。"""
        ...


class MemoryLoginAttemptStore:
    """プロセスのメモリの記録（DB が使えないとき・共有の鍵が無いとき）。時計は差し替えられる。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # 鍵 → [(記録した時刻, attempt_id)]（古い順）。
        self._rows: dict[str, list[tuple[float, str]]] = {}
        self._lock = threading.Lock()

    def reserve(
        self, attempt_id: str, key_hashes: Sequence[str], window_seconds: float
    ) -> AttemptRows:
        now = self._clock()
        with self._lock:
            if len(self._rows) >= _PRUNE_THRESHOLD:
                self._prune(now, window_seconds)
            for key in key_hashes:
                self._rows.setdefault(key, []).append((now, attempt_id))
            return self._recent(key_hashes, now, window_seconds)

    def recent(self, key_hashes: Sequence[str], window_seconds: float) -> AttemptRows:
        now = self._clock()
        with self._lock:
            return self._recent(key_hashes, now, window_seconds)

    def release(self, attempt_id: str, key_hashes: Sequence[str]) -> None:
        with self._lock:
            for key in key_hashes:
                self._drop(key, lambda row: row[1] == attempt_id)

    def clear(self, attempt_id: str, key_hashes: Sequence[str], cleared_key_hash: str) -> None:
        with self._lock:
            self._rows.pop(cleared_key_hash, None)
            for key in key_hashes:
                self._drop(key, lambda row: row[1] == attempt_id)

    def purge_expired(self, window_seconds: float) -> int:
        now = self._clock()
        with self._lock:
            return self._prune(now, window_seconds)

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()

    def is_empty(self) -> bool:
        with self._lock:
            return not self._rows

    def _recent(self, key_hashes: Sequence[str], now: float, window_seconds: float) -> AttemptRows:
        result: AttemptRows = {}
        for key in key_hashes:
            self._drop(key, lambda row: row[0] + window_seconds <= now)
            rows = self._rows.get(key, [])
            result[key] = [AttemptRow(attempt_id, now - at) for at, attempt_id in reversed(rows)]
        return result

    def _drop(self, key: str, predicate: Callable[[tuple[float, str]], bool]) -> int:
        rows = self._rows.get(key)
        if rows is None:
            return 0
        kept = [row for row in rows if not predicate(row)]
        dropped = len(rows) - len(kept)
        if kept:
            self._rows[key] = kept
        else:
            del self._rows[key]
        return dropped

    def _prune(self, now: float, window_seconds: float) -> int:
        return sum(
            self._drop(key, lambda row: row[0] + window_seconds <= now) for key in list(self._rows)
        )


ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# 記録してからの秒数（DB の時計）。TIMESTAMP どうしの差は INTERVAL DAY TO SECOND。
_AGE_SECONDS_SQL = (
    "EXTRACT(DAY FROM AGE) * 86400 + EXTRACT(HOUR FROM AGE) * 3600"
    " + EXTRACT(MINUTE FROM AGE) * 60 + EXTRACT(SECOND FROM AGE)"
)
_NOW_UTC_SQL = "SYS_EXTRACT_UTC(SYSTIMESTAMP)"


def _key_binds(key_hashes: Sequence[str]) -> tuple[str, dict[str, str]]:
    """`IN (...)` の placeholder と bind。鍵は 2 つまで（ログイン ID＋IP・IP）。"""
    binds = {f"key_{index}": key for index, key in enumerate(key_hashes)}
    return ", ".join(f":{name}" for name in binds), binds


class OracleLoginAttemptStore:
    """`PLATFORM_LOGIN_ATTEMPTS` の記録（3 製品・全 worker で共有）。

    `connection_factory` は、未コミットの変更を持たない接続の context manager を返すこと
    （`OracleAuthStore` と同じ）。時刻はすべて DB の `SYS_EXTRACT_UTC(SYSTIMESTAMP)` で決める。
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def reserve(
        self, attempt_id: str, key_hashes: Sequence[str], window_seconds: float
    ) -> AttemptRows:
        with self._connection_factory() as connection, connection.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO {LOGIN_ATTEMPTS_TABLE} (KEY_HASH, ATTEMPT_ID, ATTEMPTED_AT)
                VALUES (:key_hash, :attempt_id, {_NOW_UTC_SQL})
                """,  # nosec B608 - 表名と式は固定。値はすべて bind
                [{"key_hash": key, "attempt_id": attempt_id} for key in key_hashes],
            )
            # 数える前に確定し、同時に数える別の worker・製品から見えるようにする。
            connection.commit()
            return self._select_recent(cursor, key_hashes, window_seconds)

    def recent(self, key_hashes: Sequence[str], window_seconds: float) -> AttemptRows:
        with self._connection_factory() as _connection, _connection.cursor() as cursor:
            return self._select_recent(cursor, key_hashes, window_seconds)

    def release(self, attempt_id: str, key_hashes: Sequence[str]) -> None:
        placeholders, binds = _key_binds(key_hashes)
        with self._connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {LOGIN_ATTEMPTS_TABLE} "  # nosec B608 - 固定の表名と bind 名だけ
                f"WHERE ATTEMPT_ID = :attempt_id AND KEY_HASH IN ({placeholders})",
                {"attempt_id": attempt_id, **binds},
            )
            connection.commit()

    def clear(self, attempt_id: str, key_hashes: Sequence[str], cleared_key_hash: str) -> None:
        placeholders, binds = _key_binds(key_hashes)
        with self._connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {LOGIN_ATTEMPTS_TABLE} "  # nosec B608 - 固定の表名と bind 名だけ
                "WHERE KEY_HASH = :cleared_key_hash "
                f"OR (ATTEMPT_ID = :attempt_id AND KEY_HASH IN ({placeholders}))",
                {"cleared_key_hash": cleared_key_hash, "attempt_id": attempt_id, **binds},
            )
            connection.commit()

    def purge_expired(self, window_seconds: float) -> int:
        with self._connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {LOGIN_ATTEMPTS_TABLE} "  # nosec B608 - 表名と式は固定
                f"WHERE ATTEMPTED_AT <= {_NOW_UTC_SQL} "
                "- NUMTODSINTERVAL(:window_seconds, 'SECOND') AND ROWNUM <= :max_rows",
                {"window_seconds": float(window_seconds), "max_rows": PURGE_BATCH_ROWS},
            )
            purged = int(cursor.rowcount or 0)
            connection.commit()
            return purged

    @staticmethod
    def _select_recent(
        cursor: Any, key_hashes: Sequence[str], window_seconds: float
    ) -> AttemptRows:
        placeholders, binds = _key_binds(key_hashes)
        cursor.execute(
            f"""
            SELECT KEY_HASH, ATTEMPT_ID, {_AGE_SECONDS_SQL} AS AGE_SECONDS
              FROM (
                SELECT KEY_HASH, ATTEMPT_ID, {_NOW_UTC_SQL} - ATTEMPTED_AT AS AGE
                  FROM {LOGIN_ATTEMPTS_TABLE}
                 WHERE KEY_HASH IN ({placeholders})
                   AND ATTEMPTED_AT > {_NOW_UTC_SQL} - NUMTODSINTERVAL(:window_seconds, 'SECOND')
              )
             ORDER BY AGE_SECONDS, ATTEMPT_ID
             FETCH FIRST {MAX_ROWS_PER_CHECK} ROWS ONLY
            """,  # nosec B608 - 表名・式・件数は固定。値はすべて bind
            {"window_seconds": float(window_seconds), **binds},
        )
        result: AttemptRows = {key: [] for key in key_hashes}
        for key_hash, attempt_id, age_seconds in cursor.fetchall():
            result.setdefault(str(key_hash), []).append(
                AttemptRow(str(attempt_id), max(0.0, float(age_seconds)))
            )
        return result


@dataclass
class LoginAttempt:
    """1 回のログインの試行。`begin` が返し、結果を `record_*` / `cancel` に渡す。"""

    attempt_id: str
    # 上限を持つ単位の鍵（ログイン ID＋IP・IP）と上限。
    limited_keys: tuple[tuple[str, int], ...]
    login_key_hash: str
    window_seconds: float
    # 拒否するなら次に試せるまでの秒数（1 以上）。
    retry_after_seconds: int | None = None
    # 行を足した置き場所（拒否・上限なしのときは None）。
    store: LoginAttemptStore | None = field(default=None, repr=False)

    @property
    def key_hashes(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.limited_keys)


def derive_login_throttle_key(secret: str) -> bytes | None:
    """共有の secret から、試行の記録の鍵を導く。secret が短ければ None（DB に記録しない）。"""
    secret = secret.strip()
    if len(secret) < LOGIN_THROTTLE_MIN_SECRET_LENGTH:
        return None
    return hmac.new(secret.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()


class LoginThrottle:
    """ログインの失敗の回数を数える。

    `shared_store`（`OracleLoginAttemptStore`）と `key_secret`（共通の secret）があれば DB で、
    無い・DB が使えないときはプロセスのメモリで数える。時計は差し替えられる（テスト用）。
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        shared_store: LoginAttemptStore | None = None,
        key_secret: str = "",
        shared_retry_seconds: float = DEFAULT_SHARED_RETRY_SECONDS,
        shared_timeout_seconds: float = DEFAULT_SHARED_TIMEOUT_SECONDS,
    ) -> None:
        self._clock = clock
        self.memory = MemoryLoginAttemptStore(clock)
        key = derive_login_throttle_key(key_secret)
        if shared_store is not None and key is None:
            logger.warning(
                "auth_login_throttle_shared_store_disabled",
                extra={
                    "reason": "service_token_secret_missing",
                    "hint": "共通 .env の PLATFORM_SERVICE_TOKEN_SECRET（32 文字以上）を"
                    "設定すると、ログインの試行の回数を 3 製品・全 worker で共有して数えます。",
                },
            )
            shared_store = None
        self.shared_store = shared_store
        # 鍵が無いときはプロセスごとの乱数の鍵（プロセスの外に出ないので共有しなくてよい）。
        self._key = key or secrets.token_bytes(32)
        self._shared_retry_seconds = shared_retry_seconds
        self._shared_timeout_seconds = shared_timeout_seconds
        # 時間切れでも DB 処理を強制終了できないため、同時に残す処理を 1 つに制限する。
        self._shared_operation_lock = threading.Lock()
        self._shared_unavailable_until: float | None = None
        self._state_lock = threading.Lock()

    # ---- 鍵 ----

    def _hash(self, *parts: str) -> str:
        message = "\x00".join(parts).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def _limited_keys(
        self, login_user_id: str, client_ip: str, limits: LoginThrottleLimits
    ) -> tuple[tuple[tuple[str, int], ...], str]:
        normalized = login_user_id.strip().casefold()
        login_key = self._hash("login", normalized, client_ip)
        ip_key = self._hash("ip", client_ip)
        limited = tuple(
            (key, limit)
            for key, limit in ((login_key, limits.per_login_and_ip), (ip_key, limits.per_ip))
            if limit > 0
        )
        return limited, login_key

    # ---- 試行 ----

    def begin(
        self, login_user_id: str, client_ip: str, limits: LoginThrottleLimits
    ) -> LoginAttempt:
        """照合の前に呼ぶ。拒否なら `retry_after_seconds` を持つ（行は残さない）。"""
        limited, login_key = self._limited_keys(login_user_id, client_ip, limits)
        attempt = LoginAttempt(
            attempt_id=str(uuid4()),
            limited_keys=limited,
            login_key_hash=login_key,
            window_seconds=float(limits.window_seconds),
        )
        if not limited:
            return attempt
        shared = self._available_shared_store()
        if shared is not None:
            try:
                shared_rows = self._call_shared(
                    lambda: shared.reserve(
                        attempt.attempt_id, attempt.key_hashes, attempt.window_seconds
                    )
                )
            except Exception as exc:
                self._mark_shared_unavailable(exc, "reserve")
            else:
                self._mark_shared_available()
                # DB の停止中にプロセス内で数えた失敗も、窓の中なら合わせて数える。
                memory_rows = self.memory.recent(attempt.key_hashes, attempt.window_seconds)
                rows = {
                    key: [*shared_rows.get(key, []), *memory_rows.get(key, [])]
                    for key in attempt.key_hashes
                }
                wait = self._wait(rows, attempt)
                if wait is None:
                    attempt.store = shared
                    return attempt
                self._release_quietly(shared, attempt)
                attempt.retry_after_seconds = wait
                return attempt
        memory_rows = self.memory.reserve(
            attempt.attempt_id, attempt.key_hashes, attempt.window_seconds
        )
        wait = self._wait(memory_rows, attempt)
        if wait is None:
            attempt.store = self.memory
            return attempt
        self.memory.release(attempt.attempt_id, attempt.key_hashes)
        attempt.retry_after_seconds = wait
        return attempt

    def record_failure(self, attempt: LoginAttempt) -> None:
        """照合に失敗した。足した行を失敗の記録として残し、期限切れの行を掃除する。"""
        store = attempt.store
        # プロセス内の記録は、鍵の数が増えたときに `reserve` が掃除する。
        if store is None or store is self.memory:
            return
        try:
            self._call_shared(lambda: store.purge_expired(attempt.window_seconds))
        except Exception as exc:
            self._mark_shared_unavailable(exc, "purge")

    def record_success(self, attempt: LoginAttempt) -> None:
        """成功したログイン ID と送信元 IP の組の回数を消す（送信元 IP の回数は残す）。"""
        # DB の停止中にプロセス内で数えた失敗も消す。
        self.memory.clear(attempt.attempt_id, attempt.key_hashes, attempt.login_key_hash)
        store = attempt.store
        if store is None or store is self.memory:
            return
        try:
            self._call_shared(
                lambda: store.clear(attempt.attempt_id, attempt.key_hashes, attempt.login_key_hash)
            )
        except Exception as exc:
            self._mark_shared_unavailable(exc, "clear")

    def cancel(self, attempt: LoginAttempt) -> None:
        """照合が成功・失敗のどちらにもならなかった（DB のエラーなど）。足した行を消す。"""
        store = attempt.store
        if store is None:
            return
        if store is self.memory:
            self.memory.release(attempt.attempt_id, attempt.key_hashes)
            return
        self._release_quietly(store, attempt)

    def reset(self) -> None:
        """プロセス内の記録を消す（テスト用）。DB の記録は消さない。"""
        self.memory.reset()

    # ---- 判定 ----

    @staticmethod
    def _wait(rows: AttemptRows, attempt: LoginAttempt) -> int | None:
        """自分以外の窓の中の試行が上限以上なら、次に試せるまでの秒数。"""
        waits: list[float] = []
        for key, limit in attempt.limited_keys:
            others = [row for row in rows.get(key, []) if row.attempt_id != attempt.attempt_id]
            if len(others) >= limit:
                others.sort(key=lambda row: row.age_seconds)
                # 上限から数えて最も古い試行が窓から出れば、もう 1 回試せる。
                waits.append(attempt.window_seconds - others[limit - 1].age_seconds)
        if not waits:
            return None
        return max(1, ceil(max(waits)))

    # ---- 共有の記録の切り替え ----

    def _call_shared(self, operation: Callable[[], _Result]) -> _Result:
        """共有の処理を時間内だけ待つ。遅れて確定した予約も失敗として残す（安全側）。"""
        deadline = time.monotonic() + self._shared_timeout_seconds
        if not self._shared_operation_lock.acquire(timeout=self._shared_timeout_seconds):
            raise TimeoutError("Shared login attempt store is busy")
        result: Future[_Result] = Future()

        def run() -> None:
            try:
                result.set_result(operation())
            except BaseException as exc:
                result.set_exception(exc)
            finally:
                self._shared_operation_lock.release()

        worker = threading.Thread(target=run, name="login-throttle-store", daemon=True)
        try:
            worker.start()
        except BaseException:
            self._shared_operation_lock.release()
            raise
        return result.result(timeout=max(0.0, deadline - time.monotonic()))

    def _available_shared_store(self) -> LoginAttemptStore | None:
        if self.shared_store is None:
            return None
        with self._state_lock:
            until = self._shared_unavailable_until
        if until is not None and self._clock() < until:
            return None
        return self.shared_store

    def _mark_shared_unavailable(self, exc: Exception, operation: str) -> None:
        with self._state_lock:
            already = self._shared_unavailable_until is not None
            self._shared_unavailable_until = self._clock() + self._shared_retry_seconds
        message = str(exc).upper()
        missing_table = "ORA-00942" in message
        logger.warning(
            "auth_login_throttle_shared_store_unavailable",
            extra={
                "operation": operation,
                "error_type": type(exc).__name__,
                "error_code": _oracle_error_code(message),
                "fallback": "process_memory",
                "retry_after_seconds": self._shared_retry_seconds,
                "already_unavailable": already,
                "hint": (
                    f"{LOGIN_ATTEMPTS_TABLE} がありません。システムテーブルの作成・更新を"
                    "実行してください。"
                    if missing_table
                    else "データベースに接続できるようになると、共有の記録に戻ります。"
                ),
            },
        )

    def _mark_shared_available(self) -> None:
        with self._state_lock:
            was_unavailable = self._shared_unavailable_until is not None
            self._shared_unavailable_until = None
        if was_unavailable:
            logger.info("auth_login_throttle_shared_store_restored")

    def _release_quietly(self, store: LoginAttemptStore, attempt: LoginAttempt) -> None:
        try:
            self._call_shared(lambda: store.release(attempt.attempt_id, attempt.key_hashes))
        except Exception as exc:
            # 消せなかった行は失敗 1 回として窓の間だけ数える（安全側）。
            self._mark_shared_unavailable(exc, "release")


def _oracle_error_code(message: str) -> str:
    """ログに出すエラーコード（ORA-xxxxx / DPY-xxxx）。文そのものは出さない。"""
    match = _ERROR_CODE_PATTERN.search(message)
    return match.group(0) if match else ""
