"""ログインの試行の回数を 3 製品・全 worker で共有して数える（#1173）。

`PLATFORM_LOGIN_ATTEMPTS` を、SQL を解釈する fake の Oracle（commit した行だけが別の接続から
見える）で置き換え、別のプロセス相当の 2 つの `LoginThrottle` が同じ記録で数えることを確かめる。
時計は DB とアプリで同じものを差し替え、窓が過ぎるのをテストで待たない。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest
from test_auth import CONFIGURED_PASSWORD, SERVICE_SECRET, _ProductService, _service, _Settings

from pr_system_settings.auth.errors import SecurityApiError
from pr_system_settings.auth.login_throttle import (
    LOGIN_ATTEMPTS_TABLE,
    PURGE_BATCH_ROWS,
    LoginThrottle,
    LoginThrottleLimits,
    OracleLoginAttemptStore,
)
from pr_system_settings.auth.migrations import PLATFORM_AUTH_DDL
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES, InMemoryAuthStore, OracleAuthStore

WRONG_PASSWORD = "WrongPassword!123"  # nosec B105 - テスト用
IP = "203.0.113.10"
OTHER_IP = "198.51.100.20"
WINDOW_SECONDS = 15 * 60
RETRY_SECONDS = 30.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _FakeOracle:
    """`PLATFORM_LOGIN_ATTEMPTS` だけを持つ fake。INSERT は commit まで別の接続から見えない。"""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        # (KEY_HASH, ATTEMPT_ID, ATTEMPTED_AT)
        self.rows: list[tuple[str, str, float]] = []
        self.down = False
        self.missing_table = False
        self.connects = 0
        self.binds: list[Any] = []

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        self.connects += 1
        if self.down:
            raise ConnectionError("DPY-6005: cannot connect to database")
        connection = _FakeConnection(self)
        try:
            yield connection
        finally:
            connection.pending.clear()  # commit していない行は捨てる（rollback）


class _FakeConnection:
    def __init__(self, db: _FakeOracle) -> None:
        self.db = db
        self.pending: list[tuple[str, str, float]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.db.rows.extend(self.pending)
        self.pending.clear()


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.db = connection.db
        self.rowcount = 0
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def _check(self, sql: str) -> str:
        assert LOGIN_ATTEMPTS_TABLE in sql
        if self.db.missing_table:
            raise RuntimeError(
                f'ORA-00942: table or view "APP"."{LOGIN_ATTEMPTS_TABLE}" does not exist'
            )
        return " ".join(sql.split())

    def executemany(self, sql: str, rows: Sequence[dict[str, Any]]) -> None:
        statement = self._check(sql)
        assert statement.startswith(f"INSERT INTO {LOGIN_ATTEMPTS_TABLE}")
        assert "SYS_EXTRACT_UTC(SYSTIMESTAMP)" in statement
        self.db.binds.extend(rows)
        now = self.db.clock()
        self.connection.pending.extend((row["key_hash"], row["attempt_id"], now) for row in rows)

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        statement = self._check(sql)
        self.db.binds.append(binds)
        now = self.db.clock()
        keys = {value for name, value in binds.items() if name.startswith("key_")}
        if statement.startswith("SELECT KEY_HASH, ATTEMPT_ID"):
            visible = [*self.db.rows, *self.connection.pending]
            window = binds["window_seconds"]
            selected = [
                (key, attempt_id, now - at)
                for key, attempt_id, at in visible
                if key in keys and at > now - window
            ]
            self._result = sorted(selected, key=lambda row: (row[2], row[1]))
            return
        if "WHERE ATTEMPTED_AT <=" in statement:
            assert "ROWNUM <= :max_rows" in statement
            cutoff = now - binds["window_seconds"]
            expired = [row for row in self.db.rows if row[2] <= cutoff][: binds["max_rows"]]
            self._delete(expired)
            return
        if "WHERE KEY_HASH = :cleared_key_hash OR" in statement:
            self._delete(
                [
                    row
                    for row in self.db.rows
                    if row[0] == binds["cleared_key_hash"]
                    or (row[1] == binds["attempt_id"] and row[0] in keys)
                ]
            )
            return
        if "WHERE ATTEMPT_ID = :attempt_id AND KEY_HASH IN" in statement:
            self._delete(
                [row for row in self.db.rows if row[1] == binds["attempt_id"] and row[0] in keys]
            )
            return
        raise AssertionError(f"unexpected SQL: {statement}")

    def _delete(self, rows: list[tuple[str, str, float]]) -> None:
        for row in rows:
            self.db.rows.remove(row)
        self.rowcount = len(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def db(clock: _Clock) -> _FakeOracle:
    return _FakeOracle(clock)


def _throttle(clock: _Clock, db: _FakeOracle, *, secret: str = SERVICE_SECRET) -> LoginThrottle:
    return LoginThrottle(
        clock=clock,
        shared_store=OracleLoginAttemptStore(db.connection),
        key_secret=secret,
        shared_retry_seconds=RETRY_SECONDS,
    )


def _worker(clock: _Clock, db: _FakeOracle) -> _ProductService:
    """別のプロセス（別の worker・別の製品）相当の service。記録は同じ DB に置く。"""
    service, _ = _service()
    service.login_throttle = _throttle(clock, db)
    return service


def _fail(service: _ProductService, login_user_id: str = "system_admin", *, ip: str = IP) -> int:
    with pytest.raises(SecurityApiError) as exc:
        service.login(login_user_id, WRONG_PASSWORD, client_ip=ip)
    return exc.value.status_code


def _retry_after(service: _ProductService, *, ip: str = IP) -> int:
    with pytest.raises(SecurityApiError) as exc:
        service.login("system_admin", CONFIGURED_PASSWORD, client_ip=ip)
    assert exc.value.status_code == 429
    return int(exc.value.headers["Retry-After"])


def test_two_workers_count_failures_together_through_db(clock: _Clock, db: _FakeOracle) -> None:
    worker_a, worker_b = _worker(clock, db), _worker(clock, db)
    for service in (worker_a, worker_b, worker_a, worker_b, worker_a):
        assert _fail(service) == 401
        clock.now += 10

    # 上限（5 回）は 2 つの worker を合わせた回数。どちらの worker でも、正しいパスワードでも 429。
    assert _retry_after(worker_a) == WINDOW_SECONDS - 50
    assert _retry_after(worker_b) == WINDOW_SECONDS - 50
    # 拒否した試行は記録に残さない（ログイン ID＋IP と IP の 2 行 × 失敗 5 回だけ）。
    assert len(db.rows) == 10


def test_shared_limit_is_lifted_after_window(clock: _Clock, db: _FakeOracle) -> None:
    worker_a, worker_b = _worker(clock, db), _worker(clock, db)
    for _ in range(5):
        assert _fail(worker_a) == 401
    assert _retry_after(worker_b) == WINDOW_SECONDS

    clock.now += WINDOW_SECONDS - 1
    assert _retry_after(worker_b) == 1

    clock.now += 1
    principal, _, _ = worker_b.login("system_admin", CONFIGURED_PASSWORD, client_ip=IP)
    assert principal.is_system_admin


def test_success_on_other_worker_clears_login_failures(clock: _Clock, db: _FakeOracle) -> None:
    worker_a, worker_b = _worker(clock, db), _worker(clock, db)
    for _ in range(4):
        assert _fail(worker_a) == 401
    worker_b.login("system_admin", CONFIGURED_PASSWORD, client_ip=IP)

    # ログイン ID＋IP の行は消え、送信元 IP の失敗 4 回だけが残る（成功は数えない）。
    assert len(db.rows) == 4
    for _ in range(5):
        assert _fail(worker_a) == 401
    assert _retry_after(worker_b) == WINDOW_SECONDS


def test_concurrent_attempts_do_not_exceed_limit(clock: _Clock, db: _FakeOracle) -> None:
    """照合の前に行を確定してから数えるので、同時の試行でも許可される失敗は上限まで。"""
    limits = LoginThrottleLimits(per_login_and_ip=2, per_ip=0, window_seconds=WINDOW_SECONDS)
    throttles = [_throttle(clock, db) for _ in range(3)]
    # 3 つの worker が、どれも照合を終える前に同時に試す。
    attempts = [throttle.begin("system_admin", IP, limits) for throttle in throttles]
    admitted = [attempt for attempt in attempts if attempt.retry_after_seconds is None]
    assert len(admitted) == 2
    for throttle, attempt in zip(throttles, attempts, strict=True):
        if attempt.retry_after_seconds is None:
            throttle.record_failure(attempt)
    assert len(db.rows) == 2
    assert throttles[0].begin("system_admin", IP, limits).retry_after_seconds == WINDOW_SECONDS


def test_unexpected_error_does_not_count_as_failure(clock: _Clock, db: _FakeOracle) -> None:
    service = _worker(clock, db)

    def _broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("DPY-4011: the database or network closed the connection")

    service.store.get_user_by_login_user_id = _broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        service.login("someone", WRONG_PASSWORD, client_ip=IP)
    assert db.rows == []


def test_db_unavailable_falls_back_to_process_memory_and_still_limits(
    clock: _Clock, db: _FakeOracle, caplog: pytest.LogCaptureFixture
) -> None:
    service = _worker(clock, db)
    db.down = True
    with caplog.at_level(logging.WARNING, logger="pr_system_settings.auth.login_throttle"):
        for _ in range(5):
            assert _fail(service) == 401
        # fail open にしない: プロセス内で数えて 6 回目は 429。
        assert _retry_after(service) == WINDOW_SECONDS
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "auth_login_throttle_shared_store_unavailable"
    ]
    assert warnings, "DB に切り替えられないことを警告していない"
    assert warnings[0].fallback == "process_memory"  # type: ignore[attr-defined]
    assert warnings[0].error_code == "DPY-6005"  # type: ignore[attr-defined]
    # 停止中は試し直しの間隔まで DB に接続しない（ログインのたびに接続を待たない）。
    assert db.connects == 1
    # 構成管理者は DB の停止中も、別の送信元からログインできる（#1150）。
    principal, _, _ = service.login("system_admin", CONFIGURED_PASSWORD, client_ip=OTHER_IP)
    assert principal.is_system_admin


def test_returns_to_db_after_recovery_and_keeps_memory_failures(
    clock: _Clock, db: _FakeOracle, caplog: pytest.LogCaptureFixture
) -> None:
    service = _worker(clock, db)
    db.down = True
    for _ in range(2):
        assert _fail(service) == 401
    assert db.rows == []

    db.down = False
    clock.now += RETRY_SECONDS
    with caplog.at_level(logging.INFO, logger="pr_system_settings.auth.login_throttle"):
        for _ in range(3):
            assert _fail(service) == 401
    assert "auth_login_throttle_shared_store_restored" in caplog.messages
    # DB に戻った後の失敗は DB に記録し、停止中にプロセス内で数えた 2 回も合わせて上限に達する。
    assert len(db.rows) == 6
    assert _retry_after(service) == WINDOW_SECONDS - RETRY_SECONDS
    # 別の worker は DB の 3 回だけを数える。
    assert _fail(_worker(clock, db)) == 401


def test_slow_database_bounds_login_wait_and_keeps_memory_limit(
    clock: _Clock, db: _FakeOracle
) -> None:
    service = _worker(clock, db)
    entered, finish = threading.Event(), threading.Event()
    original = db.connection

    @contextmanager
    def slow_connection() -> Iterator[_FakeConnection]:
        entered.set()
        assert finish.wait(5)
        with original() as connection:
            yield connection

    service.login_throttle = LoginThrottle(
        clock=clock,
        shared_store=OracleLoginAttemptStore(slow_connection),
        key_secret=SERVICE_SECRET,
        shared_timeout_seconds=0.05,
    )
    try:
        started = time.monotonic()
        principal, _, _ = service.login("system_admin", CONFIGURED_PASSWORD, client_ip=OTHER_IP)
        assert principal.is_system_admin
        assert entered.is_set()
        assert time.monotonic() - started < 1
        for _ in range(5):
            assert _fail(service) == 401
        assert _retry_after(service) == WINDOW_SECONDS
        # 再試行の時刻を過ぎても、止まった処理を増やさない。
        clock.now += RETRY_SECONDS
        assert _retry_after(service) == WINDOW_SECONDS - RETRY_SECONDS
        assert db.connects == 0
    finally:
        finish.set()
        assert service.login_throttle._shared_operation_lock.acquire(timeout=2)
        service.login_throttle._shared_operation_lock.release()


@pytest.mark.parametrize("operation", ["clear", "purge", "release"])
def test_shared_cleanup_also_has_wait_limit(
    clock: _Clock, db: _FakeOracle, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    throttle = _throttle(clock, db)
    throttle._shared_timeout_seconds = 0.05
    attempt = throttle.begin("system_admin", IP, LoginThrottleLimits(5, 20, WINDOW_SECONDS))
    finish = threading.Event()

    def slow(*args: object) -> None:
        assert finish.wait(5)

    store = throttle.shared_store
    assert store is not None
    monkeypatch.setattr(store, "purge_expired" if operation == "purge" else operation, slow)
    try:
        started = time.monotonic()
        if operation == "clear":
            throttle.record_success(attempt)
        elif operation == "purge":
            throttle.record_failure(attempt)
        else:
            throttle.cancel(attempt)
        assert time.monotonic() - started < 1
        assert throttle._available_shared_store() is None
    finally:
        finish.set()
        assert throttle._shared_operation_lock.acquire(timeout=2)
        throttle._shared_operation_lock.release()


def test_missing_table_falls_back_with_migration_hint(
    clock: _Clock, db: _FakeOracle, caplog: pytest.LogCaptureFixture
) -> None:
    service = _worker(clock, db)
    db.missing_table = True
    with caplog.at_level(logging.WARNING, logger="pr_system_settings.auth.login_throttle"):
        for _ in range(5):
            assert _fail(service) == 401
        assert _retry_after(service) == WINDOW_SECONDS
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "auth_login_throttle_shared_store_unavailable"
    )
    assert record.error_code == "ORA-00942"  # type: ignore[attr-defined]
    assert LOGIN_ATTEMPTS_TABLE in record.hint  # type: ignore[attr-defined]


def test_expired_rows_are_purged_when_failure_is_recorded(clock: _Clock, db: _FakeOracle) -> None:
    service = _worker(clock, db)
    for index in range(3):
        assert _fail(service, f"user{index}", ip=f"192.0.2.{index}") == 401
    assert len(db.rows) == 6

    clock.now += WINDOW_SECONDS
    assert _fail(service, "user9", ip=OTHER_IP) == 401
    # 窓を過ぎた行は、失敗を記録するついでに消える。
    assert len(db.rows) == 2

    # 1 回に消す件数には上限がある（DELETE を短くする）。残りは次の失敗で消える。
    db.rows.extend((f"{index:064x}", f"old-{index}", 0.0) for index in range(PURGE_BATCH_ROWS + 5))
    assert _fail(service, "user9", ip=OTHER_IP) == 401
    assert len(db.rows) == 4 + 5
    assert _fail(service, "user9", ip=OTHER_IP) == 401
    assert len(db.rows) == 6


def test_db_keeps_only_hmac_of_login_id_and_ip(clock: _Clock, db: _FakeOracle) -> None:
    service = _worker(clock, db)
    assert _fail(service, " System_Admin ") == 401
    assert _fail(_worker(clock, db), "system_admin") == 401
    stored = [value for row in db.rows for value in row[:2]] + [
        str(value) for binds in db.binds for value in binds.values()
    ]
    assert not any("admin" in value.casefold() or IP in value for value in stored)
    assert all(re.fullmatch("[0-9a-f]{64}", key) for key, _, _ in db.rows)
    # 同じ secret の worker は同じ鍵になる（大文字・小文字と前後の空白は区別しない）。
    assert len({key for key, _, _ in db.rows}) == 2

    # secret が違えば鍵も違う（secret が無ければ行から戻せない）。
    other = _FakeOracle(clock)
    _throttle(clock, other, secret="y" * 40).begin(
        "system_admin", IP, LoginThrottleLimits(5, 20, WINDOW_SECONDS)
    )
    assert {key for key, _, _ in other.rows}.isdisjoint({key for key, _, _ in db.rows})


def test_without_shared_secret_counts_in_process_only(
    clock: _Clock, db: _FakeOracle, caplog: pytest.LogCaptureFixture
) -> None:
    service, _ = _service()
    with caplog.at_level(logging.WARNING, logger="pr_system_settings.auth.login_throttle"):
        service.login_throttle = _throttle(clock, db, secret="")
    assert "auth_login_throttle_shared_store_disabled" in caplog.messages
    assert service.login_throttle.shared_store is None
    for _ in range(5):
        assert _fail(service) == 401
    assert _retry_after(service) == WINDOW_SECONDS
    assert db.connects == 0


def test_auth_service_shares_counts_only_with_oracle_store(db: _FakeOracle) -> None:
    oracle_service = _ProductService(OracleAuthStore(db.connection), _Settings())
    assert isinstance(oracle_service.login_throttle.shared_store, OracleLoginAttemptStore)
    memory_service = _ProductService(InMemoryAuthStore(), _Settings())
    assert memory_service.login_throttle.shared_store is None


def test_login_attempts_table_ddl_uses_platform_names() -> None:
    joined = "\n".join(PLATFORM_AUTH_DDL)
    assert f"CREATE TABLE {LOGIN_ATTEMPTS_TABLE} (" in joined
    assert "CONSTRAINT PK_PLATFORM_LOGIN_ATTEMPTS PRIMARY KEY (KEY_HASH, ATTEMPT_ID)" in joined
    assert "CREATE INDEX IX_PLATFORM_LOGIN_ATTEMPTS_AT ON PLATFORM_LOGIN_ATTEMPTS" in joined
    assert LOGIN_ATTEMPTS_TABLE in PLATFORM_AUTH_TABLES
