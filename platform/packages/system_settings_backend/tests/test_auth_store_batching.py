"""共通認証の store の SQL の往復がユーザー数・ロール数に比例しないこと（#793）。

fake の接続・cursor で、接続の回数と SQL の実行回数を数える。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from pr_system_settings.auth.domain import RoleRecord, SessionRecord, UserRecord
from pr_system_settings.auth.service import AuthService
from pr_system_settings.auth.store import (
    IN_LIST_CHUNK_SIZE,
    InMemoryAuthStore,
    OracleAuthStore,
    in_list_binds,
    values_by_role_id,
)


class _Db:
    """PLATFORM_USERS / PLATFORM_ROLES / PLATFORM_USER_ROLES だけを持つ fake。"""

    def __init__(self, users: int, roles: int) -> None:
        self.roles = [(f"role-{index:04d}", f"ROLE_{index:04d}") for index in range(roles)]
        self.users = [f"user-{index:04d}" for index in range(users)]
        self.user_roles = [
            (user, self.roles[(index + offset) % roles][0])
            for index, user in enumerate(self.users)
            for offset in range(min(2, roles))
        ]
        self.connects = 0
        self.executed: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        self.connects += 1
        yield _Connection(self)


class _Connection:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def cursor(self) -> _Cursor:
        return _Cursor(self._db)

    def commit(self) -> None:
        return None


class _Cursor:
    def __init__(self, db: _Db) -> None:
        self._db = db
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        binds = binds or {}
        self._db.executed.append((sql, binds))
        normalized = " ".join(sql.split())
        values = set(binds.values())
        if normalized.startswith("SELECT USER_UUID, LOGIN_USER_ID, DISPLAY_NAME, PASSWORD_HASH"):
            users = self._db.users
            if "WHERE USER_UUID = :user_uuid" in normalized:
                users = [user for user in users if user == binds["user_uuid"]]
            self._rows = [(user, user, user, "hash", "ACTIVE", 0, 0, None, 1, 0) for user in users]
        elif normalized.startswith("SELECT USER_UUID, ROLE_ID FROM PLATFORM_USER_ROLES"):
            self._rows = sorted(self._db.user_roles)
        elif normalized.startswith("SELECT ROLE_ID FROM PLATFORM_USER_ROLES"):
            self._rows = sorted(
                (role,) for user, role in self._db.user_roles if user == binds["user_uuid"]
            )
        elif normalized.startswith("SELECT ROLE_ID, ROLE_CODE"):
            roles = self._db.roles
            if "WHERE ROLE_ID IN" in normalized:
                roles = [role for role in roles if role[0] in values]
            elif "WHERE ROLE_ID = :role_id" in normalized:
                roles = [role for role in roles if role[0] == binds["role_id"]]
            self._rows = [(role_id, code, code, None, 0, 0, 1) for role_id, code in roles]
        else:  # pragma: no cover - 想定外の SQL はテストの誤り
            raise AssertionError(normalized)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _BatchingStore(OracleAuthStore):
    """製品の一括の hook（`_roles_details`）の例。権限を 1 回の IN で読んだことにする。"""

    def __init__(self, db: _Db) -> None:
        super().__init__(db.connection)
        self.batches: list[list[str]] = []

    def _roles_details(self, cursor: Any, roles: Sequence[RoleRecord]) -> list[RoleRecord]:
        self.batches.append([role.role_id for role in roles])
        return list(roles)


@pytest.mark.parametrize(("users", "roles"), [(1, 1), (30, 12), (250, 40)])
def test_list_users_reads_role_assignments_once(users: int, roles: int) -> None:
    db = _Db(users=users, roles=roles)
    store = OracleAuthStore(db.connection)

    listed = store.list_users()

    assert len(listed) == users
    assert db.connects == 1
    assert len(db.executed) == 2
    assert listed[0].role_ids == sorted(role for user, role in db.user_roles if user == "user-0000")


def test_list_users_without_users_reads_once() -> None:
    db = _Db(users=0, roles=1)

    assert OracleAuthStore(db.connection).list_users() == []
    assert len(db.executed) == 1


@pytest.mark.parametrize("roles", [1, 15, 60])
def test_list_roles_uses_the_batch_hook_once(roles: int) -> None:
    db = _Db(users=1, roles=roles)
    store = _BatchingStore(db)

    listed = store.list_roles(include_archived=True)

    assert [role.role_id for role in listed] == [role_id for role_id, _ in db.roles]
    assert db.connects == 1
    assert len(db.executed) == 1
    assert store.batches == [[role_id for role_id, _ in db.roles]]


def test_default_batch_hook_falls_back_to_single_hook() -> None:
    """一括の hook を上書きしていない製品は、今までどおり 1 件ずつの hook で読む（後方互換）。"""
    db = _Db(users=1, roles=3)
    seen: list[str] = []

    class _Legacy(OracleAuthStore):
        def _role_details(self, cursor: Any, role: RoleRecord) -> RoleRecord:
            seen.append(role.role_id)
            return role

    listed = _Legacy(db.connection).list_roles()

    assert [role.role_id for role in listed] == seen == [role_id for role_id, _ in db.roles]


def test_get_roles_keeps_order_skips_missing_and_batches() -> None:
    db = _Db(users=1, roles=5)
    store = _BatchingStore(db)

    roles = store.get_roles(["role-0003", "missing", "role-0001", "role-0003", ""])

    assert [role.role_id for role in roles] == ["role-0003", "role-0001"]
    assert db.connects == 1
    assert len(db.executed) == 1
    assert store.batches == [["role-0003", "role-0001"]]
    assert store.get_roles([]) == []
    assert db.connects == 1


def test_get_roles_chunks_in_lists() -> None:
    db = _Db(users=1, roles=IN_LIST_CHUNK_SIZE + 5)
    store = _BatchingStore(db)

    roles = store.get_roles([role_id for role_id, _ in db.roles])

    assert len(roles) == IN_LIST_CHUNK_SIZE + 5
    assert [len(binds) for _, binds in db.executed] == [IN_LIST_CHUNK_SIZE, 5]
    assert store.batches == [[role_id for role_id, _ in db.roles]]


def test_in_list_binds_deduplicates_and_chunks() -> None:
    chunks = list(in_list_binds("r_", ["a", "b", "a", "c"], chunk_size=2))

    assert chunks == [(":r_0, :r_1", {"r_0": "a", "r_1": "b"}), (":r_0", {"r_0": "c"})]
    assert list(in_list_binds("r_", [])) == []


def test_in_memory_get_roles() -> None:
    store = InMemoryAuthStore()
    store.roles = {
        "a": RoleRecord("a", "A", "A", "", False, False, 1),
        "b": RoleRecord("b", "B", "B", "", False, True, 1),
    }

    assert [role.role_id for role in store.get_roles(["b", "x", "a", "b"])] == ["b", "a"]


def test_principal_reads_roles_in_one_batch() -> None:
    """ログイン済みの要求ごとの principal の組み立てが、ロールの数だけ接続しない。"""
    db = _Db(users=1, roles=8)
    db.user_roles = [("user-0000", role_id) for role_id, _ in db.roles]
    store = _BatchingStore(db)
    service = AuthService(store, SimpleNamespace())  # type: ignore[arg-type]
    user = UserRecord(
        user_uuid="user-0000",
        login_user_id="user-0000",
        display_name="user",
        password_hash="hash",
        status="ACTIVE",
        force_password_change=False,
        failed_login_count=0,
        locked_until=None,
        version=1,
        role_ids=[role_id for role_id, _ in db.roles],
    )
    now = datetime.now(UTC)
    session = SessionRecord(
        session_id="s",
        user_uuid="user-0000",
        token_hash="t",  # nosec B106 - テスト用
        csrf_token_hash="c",  # nosec B106 - テスト用
        idle_expires_at=now + timedelta(minutes=5),
        absolute_expires_at=now + timedelta(hours=1),
        last_seen_at=now,
    )

    principal = service._principal_for(user, session)  # noqa: SLF001

    assert principal.role_ids == [role_id for role_id, _ in db.roles]
    assert db.connects == 1
    assert len(db.executed) == 1


def test_values_by_role_id_groups_rows_and_chunks() -> None:
    executed: list[tuple[str, dict[str, Any]]] = []

    class _RowsCursor:
        def execute(self, sql: str, binds: dict[str, Any]) -> None:
            executed.append((sql, binds))

        def fetchall(self) -> list[tuple[str, str]]:
            return [(value, f"code-{value}") for value in executed[-1][1].values()]

    role_ids = [f"r{index}" for index in range(IN_LIST_CHUNK_SIZE + 1)]
    values = values_by_role_id(_RowsCursor(), "SELECT ROLE_ID, CODE FROM T", role_ids)

    assert len(executed) == 2
    assert executed[0][0].startswith("SELECT ROLE_ID, CODE FROM T WHERE ROLE_ID IN (:role_0, ")
    assert values["r0"] == {"code-r0"}
    assert len(values) == IN_LIST_CHUNK_SIZE + 1
    assert values_by_role_id(_RowsCursor(), "SELECT ROLE_ID, CODE FROM T", []) == {}
