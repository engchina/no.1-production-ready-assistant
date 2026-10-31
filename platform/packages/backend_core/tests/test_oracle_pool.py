"""共有の Oracle 接続 pool（#793）。fake の oracledb で、作成・再利用・作り直し・返却を確かめる。"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pr_backend_core.oracle_pool import (
    OraclePoolSize,
    SharedOraclePool,
    connect_kwargs_fingerprint,
)
from pr_backend_core.oracle_session import init_oracle_session


class _Connection:
    def __init__(self) -> None:
        self.closed = 0
        self.rollbacks = 0
        self.call_timeout = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class _Pool:
    def __init__(self, kwargs: dict[str, Any], *, busy: bool = False) -> None:
        self.kwargs = kwargs
        self.acquired: list[_Connection] = []
        self.closed: list[bool] = []
        self.busy = busy

    def acquire(self) -> _Connection:
        connection = _Connection()
        self.acquired.append(connection)
        return connection

    def close(self, force: bool = False) -> None:
        if self.busy and not force:
            raise RuntimeError("DPY-2014: pool has busy connections")
        self.closed.append(force)


class _FakeOracledb:
    POOL_GETMODE_TIMEDWAIT = 3

    def __init__(self) -> None:
        self.pools: list[_Pool] = []
        self.busy_next = False

    def create_pool(self, **kwargs: Any) -> _Pool:
        pool = _Pool(kwargs, busy=self.busy_next)
        self.pools.append(pool)
        return pool


def _pool(fake: _FakeOracledb, **kwargs: Any) -> SharedOraclePool:
    return SharedOraclePool(name="test", oracledb_loader=lambda: fake, **kwargs)


KWARGS = {"user": "APP", "password": "secret", "dsn": "db_high", "wallet_location": "/w"}


def test_pool_is_created_once_and_reused() -> None:
    fake = _FakeOracledb()
    pool = _pool(fake, size=OraclePoolSize(min=1, max=6))

    for _ in range(5):
        with pool.connection(KWARGS) as connection:
            assert isinstance(connection, _Connection)

    assert len(fake.pools) == 1
    created = fake.pools[0].kwargs
    assert created["user"] == "APP"
    assert created["wallet_location"] == "/w"
    assert (created["min"], created["max"], created["increment"]) == (1, 6, 1)
    assert created["getmode"] == _FakeOracledb.POOL_GETMODE_TIMEDWAIT
    assert created["wait_timeout"] > 0
    assert created["session_callback"] is init_oracle_session
    # 借りた接続はすべて pool に返している。
    assert [c.closed for c in fake.pools[0].acquired] == [1] * 5


def test_pool_is_recreated_when_connect_kwargs_change() -> None:
    fake = _FakeOracledb()
    pool = _pool(fake)

    with pool.connection(KWARGS):
        pass
    with pool.connection({**KWARGS, "password": "rotated"}):
        pass

    assert len(fake.pools) == 2
    assert fake.pools[0].closed == [False]
    assert fake.pools[1].closed == []


def test_busy_pool_is_closed_later_and_on_shutdown() -> None:
    fake = _FakeOracledb()
    fake.busy_next = True
    pool = _pool(fake)
    with pool.connection(KWARGS):
        pass
    fake.busy_next = False

    # 貸し出し中の接続がある古い pool は、処理を止めずに後で閉じる。
    with pool.connection({**KWARGS, "dsn": "other_high"}):
        pass
    assert fake.pools[0].closed == []

    pool.close()
    assert fake.pools[0].closed == [True]
    assert fake.pools[1].closed == [True]

    # 閉じた後は次に借りるときに作り直す。
    with pool.connection(KWARGS):
        pass
    assert len(fake.pools) == 3


def test_exception_rolls_back_and_releases() -> None:
    fake = _FakeOracledb()
    pool = _pool(fake)

    with pytest.raises(ValueError), pool.connection(KWARGS):
        raise ValueError("boom")

    connection = fake.pools[0].acquired[0]
    assert connection.rollbacks == 1
    assert connection.closed == 1


def test_on_acquire_is_applied_each_time() -> None:
    fake = _FakeOracledb()
    pool = _pool(fake)

    def set_timeout(connection: Any) -> None:
        connection.call_timeout = 5000

    with pool.connection(KWARGS, on_acquire=set_timeout) as connection:
        assert connection.call_timeout == 5000


def test_concurrent_first_use_creates_one_pool() -> None:
    fake = _FakeOracledb()
    pool = _pool(fake)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        with pool.connection(KWARGS):
            pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(fake.pools) == 1
    assert len(fake.pools[0].acquired) == 8


def test_resize_recreates_pool_with_new_size() -> None:
    fake = _FakeOracledb()
    pool = _pool(fake)
    with pool.connection(KWARGS):
        pass
    pool.resize(OraclePoolSize.of(2, 8))
    with pool.connection(KWARGS):
        pass

    assert [(p.kwargs["min"], p.kwargs["max"]) for p in fake.pools] == [(1, 4), (2, 8)]


def test_pool_size_is_clamped() -> None:
    assert OraclePoolSize.of(0, 0) == OraclePoolSize(min=1, max=1)
    assert OraclePoolSize.of(3, 2) == OraclePoolSize(min=3, max=3)
    assert OraclePoolSize.of("2", "x") == OraclePoolSize(min=2, max=4)


def test_fingerprint_does_not_contain_secret() -> None:
    fingerprint = connect_kwargs_fingerprint(KWARGS)
    assert "secret" not in fingerprint
    assert fingerprint == connect_kwargs_fingerprint(dict(reversed(list(KWARGS.items()))))
    assert fingerprint != connect_kwargs_fingerprint({**KWARGS, "password": "other"})


# --- DB の停止中に作った pool の回復（#820） -------------------------------------------


def _run_now(task: Any) -> None:
    task()


class _OracleError(Exception):
    """python-oracledb の例外の代わり（文字列に DPY / ORA のコードを持つ）。"""


class _DownDb:
    """DB の状態を切り替えられる fake の oracledb。停止中に作った pool は起動後も壊れたまま。"""

    POOL_GETMODE_TIMEDWAIT = 3

    def __init__(self) -> None:
        self.up = False
        # 停止中に作った pool は、接続を作る処理が止まったままになる（ユーザー環境で観測した状態）。
        self.break_new_pools = False
        self.pools: list[_DownDbPool] = []

    def create_pool(self, **kwargs: Any) -> _DownDbPool:
        broken = self.break_new_pools and not self.up
        pool = _DownDbPool(self, broken=broken, maximum=int(kwargs["max"]))
        self.pools.append(pool)
        return pool


class _DownDbPool:
    def __init__(self, db: _DownDb, *, broken: bool, maximum: int) -> None:
        self._db = db
        self.broken = broken
        self.max = maximum
        self.busy = 0
        self.closed: list[bool] = []
        self.acquires = 0

    def acquire(self) -> _Connection:
        self.acquires += 1
        if self.broken:
            raise _OracleError(
                "DPY-4005: timed out waiting for the connection pool to return a connection"
            )
        if not self._db.up:
            raise _OracleError("DPY-6005: cannot connect to database. ORA-12541: no listener")
        self.busy += 1
        return _Connection()

    def close(self, force: bool = False) -> None:
        self.closed.append(force)


def test_pool_created_while_db_down_recovers_after_db_starts() -> None:
    """停止中に作った pool が起動後も DPY-4005 を返しても、プロセスを再起動せずに借りられる。"""
    db = _DownDb()
    db.break_new_pools = True
    pool = SharedOraclePool(name="test", oracledb_loader=lambda: db, pool_closer=_run_now)
    pool._pool_for(KWARGS)  # 停止中に作られた pool（最初の要求・起動直後の処理など）

    db.up = True
    connection = pool.acquire(KWARGS)

    assert isinstance(connection, _Connection)
    assert len(db.pools) == 2
    assert db.pools[0].broken is True
    assert db.pools[0].closed  # 壊れた pool は閉じて、二度と使わない
    assert isinstance(pool.acquire(KWARGS), _Connection)
    assert len(db.pools) == 2


def test_acquire_while_db_down_then_after_start() -> None:
    """停止中の要求は失敗し、起動後の要求は新しい pool から借りられる。"""
    db = _DownDb()
    db.break_new_pools = True
    pool = SharedOraclePool(name="test", oracledb_loader=lambda: db, pool_closer=_run_now)

    with pytest.raises(_OracleError, match="DPY-4005"):
        pool.acquire(KWARGS)

    db.up = True
    assert isinstance(pool.acquire(KWARGS), _Connection)
    assert all(p.closed for p in db.pools[:-1])


def test_retry_failure_surfaces_underlying_connection_error() -> None:
    """やり直しも失敗したら、DPY-4005 ではなく新しい pool の本当の接続エラーを返す。"""
    db = _DownDb()
    db.up = True
    pool = SharedOraclePool(name="test", oracledb_loader=lambda: db, pool_closer=_run_now)
    pool.acquire(KWARGS)
    db.pools[0].broken = True
    db.up = False

    with pytest.raises(_OracleError, match="DPY-6005") as raised:
        pool.acquire(KWARGS)

    assert "DPY-4005" not in str(raised.value)
    assert len(db.pools) == 2
    # 次の要求は、また新しい pool から始める。
    db.up = True
    assert isinstance(pool.acquire(KWARGS), _Connection)
    assert len(db.pools) == 3


def test_connection_error_discards_pool_without_retry() -> None:
    """接続できないときは同じ要求の中でやり直さず、pool だけを捨てる（次は新しい pool）。"""
    db = _DownDb()
    db.up = True
    pool = SharedOraclePool(name="test", oracledb_loader=lambda: db, pool_closer=_run_now)
    pool.acquire(KWARGS)
    db.up = False

    with pytest.raises(_OracleError, match="DPY-6005"):
        pool.acquire(KWARGS)

    assert len(db.pools) == 1
    assert db.pools[0].closed
    db.up = True
    assert isinstance(pool.acquire(KWARGS), _Connection)
    assert len(db.pools) == 2


def test_exhausted_pool_is_not_recreated() -> None:
    """貸し出し中の接続が上限に達した本当の枯渇では、pool を作り直さない。"""
    db = _DownDb()
    db.up = True
    pool = SharedOraclePool(
        name="test", oracledb_loader=lambda: db, size=OraclePoolSize(1, 2), pool_closer=_run_now
    )
    pool.acquire(KWARGS)
    current = db.pools[0]
    current.busy = current.max
    current.broken = True

    with pytest.raises(_OracleError, match="DPY-4005"):
        pool.acquire(KWARGS)

    assert len(db.pools) == 1
    assert current.closed == []


def test_sql_error_keeps_pool() -> None:
    """接続と関係ない失敗では pool を捨てない。"""

    class _FailingPool(_Pool):
        def acquire(self) -> _Connection:
            raise _OracleError("ORA-00942: table or view does not exist")

    class _Db(_FakeOracledb):
        def create_pool(self, **kwargs: Any) -> _Pool:
            created = _FailingPool(kwargs)
            self.pools.append(created)
            return created

    fake = _Db()
    pool = _pool(fake)
    with pytest.raises(_OracleError, match="ORA-00942"):
        pool.acquire(KWARGS)
    assert len(fake.pools) == 1
    assert fake.pools[0].closed == []


def test_discarded_pool_is_closed_without_blocking_the_request() -> None:
    """壊れた pool を閉じる処理（接続の試行が終わるまで戻らない）を、要求のスレッドで待たない。"""
    release = threading.Event()
    closing = threading.Event()

    class _SlowClosePool(_DownDbPool):
        def close(self, force: bool = False) -> None:
            closing.set()
            release.wait(5)
            super().close(force)

    class _SlowDb(_DownDb):
        def create_pool(self, **kwargs: Any) -> _DownDbPool:
            pool = _SlowClosePool(self, broken=self.break_new_pools and not self.up, maximum=4)
            self.pools.append(pool)
            return pool

    slow = _SlowDb()
    slow.break_new_pools = True
    pool = SharedOraclePool(name="test", oracledb_loader=lambda: slow)
    pool._pool_for(KWARGS)
    slow.up = True

    assert isinstance(pool.acquire(KWARGS), _Connection)  # close の完了を待たずに返る
    assert closing.wait(5)
    release.set()
    pool.close()
