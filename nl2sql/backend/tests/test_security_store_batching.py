"""共通認証の store の接続と SQL の往復（#793）。

- 接続は要求ごとに張らず、共通認証用の pool から借りる
- ロールの権限・業務プロファイル・Data Grant は、ロールの数によらず表ごとに 1 回で読む
"""

from __future__ import annotations

from typing import Any

import pytest

from app.clients.oracle_runtime import close_oracle_pools
from app.features.nl2sql import oracle_adapter
from app.security.domain import RoleRecord
from app.security.store import OracleSecurityStore
from app.settings import Settings


def _settings() -> Settings:
    return Settings(
        oracle_user="APP",
        oracle_password="AppPass!123",  # nosec B106 - テスト用
        oracle_dsn="adb.example.oraclecloud.com:1522/app_high",
        oracle_connection_security="walletless_tls",
        oracle_driver_mode="thin",
        nl2sql_oracle_call_timeout_seconds=7,
    )


class _Cursor:
    def __init__(self, rows: dict[str, list[tuple[Any, ...]]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._last = ""

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        self._last = " ".join(sql.split())
        self.executed.append((self._last, dict(binds or {})))

    def fetchall(self) -> list[tuple[Any, ...]]:
        for table, rows in self.rows.items():
            if table in self._last:
                return rows
        return []


def _role(index: int) -> RoleRecord:
    return RoleRecord(
        role_id=f"role-{index}",
        role_code=f"R{index:02d}",
        display_name=f"R{index}",
        description="",
        is_built_in=False,
        archived=False,
        version=1,
    )


def _entitlement_row(role_id: str, entitlement_id: str) -> tuple[Any, ...]:
    return (
        role_id,
        entitlement_id,
        "SALES",
        "ALL",
        "READ",
        "APP",
        "ORDERS",
        "TABLE",
        '["ID"]',
        "ALL",
        None,
        None,
        None,
        None,
        "APPLIED",
        None,
        None,
        None,
    )


def test_role_details_are_read_once_per_table() -> None:
    store = OracleSecurityStore(_settings())
    roles = [_role(index) for index in range(40)]
    cursor = _Cursor(
        {
            "NL2SQL_APP_ROLE_PERMISSIONS": [("role-1", "menu.query"), ("role-1", "menu.history")],
            "NL2SQL_APP_ROLE_PROFILES": [("role-2", "profile-a")],
            "NL2SQL_APP_DATA_ENTITLEMENTS": [
                _entitlement_row("role-3", "e-1"),
                _entitlement_row("role-3", "e-2"),
                _entitlement_row("role-4", "e-3"),
            ],
        }
    )

    loaded = store._roles_details(cursor, roles)  # noqa: SLF001

    assert len(cursor.executed) == 3
    assert [role.role_id for role in loaded] == [role.role_id for role in roles]
    by_id = {role.role_id: role for role in loaded if isinstance(role, RoleRecord)}
    assert by_id["role-1"].permissions == {"menu.query", "menu.history"}
    assert by_id["role-2"].allowed_profile_ids == {"profile-a"}
    assert [item.entitlement_id for item in by_id["role-3"].entitlements] == ["e-1", "e-2"]
    assert by_id["role-3"].entitlements[0].role_id == "role-3"
    assert by_id["role-3"].entitlements[0].column_names == ["ID"]
    assert [item.entitlement_id for item in by_id["role-4"].entitlements] == ["e-3"]
    assert by_id["role-0"].permissions == set()
    assert by_id["role-0"].entitlements == []


def test_single_role_details_use_the_same_queries() -> None:
    store = OracleSecurityStore(_settings())
    cursor = _Cursor({"NL2SQL_APP_ROLE_PERMISSIONS": [("role-1", "menu.query")]})

    role = store._role_details(cursor, _role(1))  # noqa: SLF001

    assert role.permissions == {"menu.query"}
    assert all(binds == {"role_0": "role-1"} for _, binds in cursor.executed)


class _PooledConnection:
    def __init__(self) -> None:
        self.call_timeout = 0
        self.closed = 0
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class _Pool:
    def __init__(self) -> None:
        self.acquired: list[_PooledConnection] = []
        self.closed = False

    def acquire(self) -> _PooledConnection:
        connection = _PooledConnection()
        self.acquired.append(connection)
        return connection

    def close(self, force: bool = False) -> None:
        self.closed = True


class _FakeOracledb:
    POOL_GETMODE_TIMEDWAIT = 3

    def __init__(self) -> None:
        self.pools: list[_Pool] = []

    def create_pool(self, **_kwargs: Any) -> _Pool:
        pool = _Pool()
        self.pools.append(pool)
        return pool

    @staticmethod
    def connect(**_kwargs: Any) -> Any:
        raise AssertionError("共通認証の store が pool を使わずに接続している")


def test_auth_store_borrows_from_one_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOracledb()
    pool = oracle_adapter._AUTH_CONNECTION_POOL  # noqa: SLF001
    pool.close()
    monkeypatch.setattr(pool, "_oracledb_loader", lambda: fake)
    store = OracleSecurityStore(_settings())
    monkeypatch.setattr(store._adapter, "_oracledb", fake)  # noqa: SLF001
    try:
        for _ in range(3):
            with store.connection() as connection:
                assert connection.call_timeout == 7000
        with pytest.raises(RuntimeError), store.connection():
            raise RuntimeError("boom")
        assert len(fake.pools) == 1
        acquired = fake.pools[0].acquired
        assert len(acquired) == 4
        assert all(connection.closed == 1 for connection in acquired)
        assert acquired[-1].rollbacks == 1
        # DB 設定の保存時・終了時（close_oracle_pools）に閉じる。
        close_oracle_pools()
        assert fake.pools[0].closed
    finally:
        pool.close()
