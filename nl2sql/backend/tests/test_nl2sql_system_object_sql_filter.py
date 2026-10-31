"""DB の object の一覧・件数・ページングで、システムの object を SQL の段階で除く（#1710）。

各一覧の SQL を SQLite で実際に評価する。システムテーブル（NL2SQL_ / PLATFORM_ / RAG_ / AGENT_）と
Oracle の内部 object（`DR$…`・`VECTOR$…` など名前に $ / # を含む）が業務の object より
多い schema で、
1 ページ目が上限まで業務の object で埋まり、件数が選べる件数と一致することを確かめる。
取った後に Python で落とすと、1 ページが数件になり、件数もずれる。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from app.clients.oracle_runtime import OraclePoolManager
from app.features.nl2sql.incremental_store import OracleIncrementalNl2SqlRepository
from app.features.nl2sql.object_visibility import (
    is_user_visible_schema_object,
    user_visible_object_sql_filters,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.deepsec import DeepSecService
from app.security.service import SecurityService
from app.security.store import InMemorySecurityStore
from app.settings import Settings, get_settings

OWNER = "APP"

# 業務の object。名前の順で、システムの object の間に散らばる。
BUSINESS_OBJECTS: tuple[tuple[str, str], ...] = (
    ("DEPARTMENT", "TABLE"),
    ("EMPLOYEE", "TABLE"),
    ("PROJECT", "TABLE"),
    # `_` を LIKE の wildcard のまま使うと落ちる名前（`RAG_` と 1 文字違い）。
    ("RAGXDATA", "TABLE"),
    ("V_DEPT_PROJECT", "VIEW"),
    ("V_EMP_DEPT", "VIEW"),
)


def _system_objects() -> list[tuple[str, str]]:
    names = [
        f"{prefix}{index:02d}"
        for prefix in ("AGENT_RUNS_", "NL2SQL_JOBS_", "PLATFORM_USERS_", "RAG_CHUNKS_")
        for index in range(15)
    ]
    # Oracle Text・vector 索引の内部表（4 つの接頭辞で始まらないが ALL_TABLES に出る）。
    names += [
        "DR$RAG_CHUNKS_TEXT_IDX$B",
        "DR$RAG_CHUNKS_TEXT_IDX$I",
        "VECTOR$RAG_CHUNKS_EMBEDDING_HNSW_IDX$1$HNSW_CHECKPOINTS",
        "VECTOR$RAG_CHUNKS_EMBEDDING_HNSW_IDX$1$HNSW_ROWID_VID_MAP",
        "SYS#AUDIT",
        # 引用付きで作った小文字の名前も大文字小文字を区別せずに除く。
        "rag_lower_case",
    ]
    return [(name, "TABLE") for name in names] + [("PLATFORM_V_ROLES", "VIEW")]


SYSTEM_OBJECTS = tuple(_system_objects())
ALL_OBJECTS = tuple(sorted((*BUSINESS_OBJECTS, *SYSTEM_OBJECTS)))
BUSINESS_TABLES = sum(kind == "TABLE" for _name, kind in BUSINESS_OBJECTS)
BUSINESS_VIEWS = len(BUSINESS_OBJECTS) - BUSINESS_TABLES
PAGE_LIMIT = 4


def test_fixture_has_more_system_objects_than_business_objects() -> None:
    assert len(SYSTEM_OBJECTS) > 10 * len(BUSINESS_OBJECTS)
    assert all(not is_user_visible_schema_object(OWNER, name) for name, _ in SYSTEM_OBJECTS)
    assert all(is_user_visible_schema_object(OWNER, name) for name, _ in BUSINESS_OBJECTS)


def _oracle_to_sqlite(sql: str) -> str:
    sql = re.sub(r"FETCH FIRST (:\w+) ROWS ONLY", r"LIMIT \1", sql)
    return re.sub(r"WHERE ROWNUM <= (:\w+)", r"LIMIT \1", sql)


class _SqliteCursor:
    def __init__(
        self, connection: sqlite3.Connection, executed: list[str], fetched: list[int]
    ) -> None:
        self._cursor = connection.cursor()
        self._executed = executed
        self._fetched = fetched

    def __enter__(self) -> _SqliteCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        self._executed.append(sql)
        self._cursor.execute(_oracle_to_sqlite(sql), binds or {})

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._cursor.fetchall()
        self._fetched.append(len(rows))
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return cast(tuple[Any, ...] | None, self._cursor.fetchone())

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self.fetchall())


class _SqliteConnection:
    def __init__(
        self, connection: sqlite3.Connection, executed: list[str], fetched: list[int]
    ) -> None:
        self._connection = connection
        self._executed = executed
        self._fetched = fetched

    def __enter__(self) -> _SqliteConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _SqliteCursor:
        return _SqliteCursor(self._connection, self._executed, self._fetched)


class _Database:
    """Oracle の辞書ビューと NL2SQL の schema snapshot を SQLite に作る。"""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        # Oracle の LIKE と同じく大文字小文字を区別する。
        self.connection.execute("PRAGMA case_sensitive_like = ON")
        self.connection.create_function("NVL", 2, lambda value, default: value or default)
        self.connection.create_function("SYS_CONTEXT", 2, lambda _namespace, _key: OWNER)
        self.executed: list[str] = []
        self.fetched: list[int] = []
        self._create()

    def _create(self) -> None:
        db = self.connection
        db.execute("CREATE TABLE all_users (username TEXT, oracle_maintained TEXT)")
        db.execute("INSERT INTO all_users VALUES (?, 'N')", (OWNER,))
        db.execute(
            "CREATE TABLE all_objects (owner TEXT, object_name TEXT, object_type TEXT, "
            "status TEXT, last_ddl_time TEXT, object_id INTEGER)"
        )
        db.execute("CREATE TABLE all_tables (owner TEXT, table_name TEXT, num_rows INTEGER)")
        db.execute("CREATE TABLE all_views (owner TEXT, view_name TEXT)")
        db.execute("CREATE TABLE all_tab_comments (owner TEXT, table_name TEXT, comments TEXT)")
        db.execute(
            "CREATE TABLE NL2SQL_SCHEMA_OBJECTS (OWNER_NAME TEXT, OBJECT_NAME TEXT, "
            "OBJECT_TYPE TEXT, LOGICAL_NAME TEXT, COMMENTS TEXT, ROW_COUNT INTEGER, "
            "COLUMN_COUNT INTEGER, LAST_DDL_AT TEXT)"
        )
        db.execute(
            "CREATE TABLE NL2SQL_SCHEMA_COLUMNS (OWNER_NAME TEXT, OBJECT_NAME TEXT, "
            "COLUMN_NAME TEXT, LOGICAL_NAME TEXT)"
        )
        db.execute(
            "CREATE TABLE NL2SQL_SCHEMA_CATALOG_HEAD (HEAD_KEY TEXT, CATALOG_VERSION INTEGER, "
            "REFRESHED_AT TEXT)"
        )
        db.execute("INSERT INTO NL2SQL_SCHEMA_CATALOG_HEAD VALUES ('active', 7, '2026-10-13')")
        for object_id, (name, kind) in enumerate(ALL_OBJECTS, start=1):
            db.execute(
                "INSERT INTO all_objects VALUES (?, ?, ?, 'VALID', '2026-10-13', ?)",
                (OWNER, name, kind, object_id),
            )
            if kind == "TABLE":
                db.execute("INSERT INTO all_tables VALUES (?, ?, 1)", (OWNER, name))
            else:
                db.execute("INSERT INTO all_views VALUES (?, ?)", (OWNER, name))
            db.execute("INSERT INTO all_tab_comments VALUES (?, ?, '')", (OWNER, name))
            # 古い snapshot に残ったシステムの object も含める（refresh の後からの除外より前の行）。
            db.execute(
                "INSERT INTO NL2SQL_SCHEMA_OBJECTS VALUES (?, ?, ?, ?, '', 1, 1, '')",
                (OWNER, name, kind, name),
            )

    @contextmanager
    def connect(self, **_kwargs: object) -> Iterator[_SqliteConnection]:
        yield _SqliteConnection(self.connection, self.executed, self.fetched)

    # DeepSecService の pool の代わり。
    control_connection = connect


def _business_names() -> list[str]:
    return [name for name, _kind in BUSINESS_OBJECTS]


def _assert_system_filters_in(sql: str, *, object_column: str, owner_column: str) -> None:
    for fragment in user_visible_object_sql_filters(
        object_column=object_column, owner_column=owner_column
    ):
        assert fragment in sql


def _collect_pages(fetch: Any) -> tuple[list[list[str]], Any]:
    pages: list[list[str]] = []
    cursor: str | None = None
    first: Any = None
    for _ in range(len(ALL_OBJECTS)):
        page = fetch(cursor)
        first = first or page
        pages.append(page[0])
        cursor = page[1]
        if not cursor:
            break
    return pages, first


def test_schema_object_search_fills_first_page_and_counts_only_business_objects() -> None:
    """`GET /api/schema/objects`（業務プロファイル・ワークベンチ・SQL→質問の対象の選択）。"""

    database = _Database()
    repository = OracleIncrementalNl2SqlRepository(connection_factory=database.connect)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - SQL の結合を確かめる

    def fetch(cursor: str | None) -> tuple[list[str], str | None, Any]:
        page = service.search_schema_objects(
            cursor=cursor,
            limit=PAGE_LIMIT,
            query="",
            owner="",
            object_type="",
            profile_id=None,
            include_counts=cursor is None,
        )
        return [item.object_name for item in page.items], page.next_cursor, page

    pages, first = _collect_pages(fetch)

    assert pages[0] == _business_names()[:PAGE_LIMIT]
    assert [name for page in pages for name in page] == _business_names()
    first_page = first[2]
    assert first_page.total == len(BUSINESS_OBJECTS)
    assert first_page.table_count == BUSINESS_TABLES
    assert first_page.view_count == BUSINESS_VIEWS
    page_sql = next(sql for sql in database.executed if "FETCH FIRST" in sql)
    count_sql = next(sql for sql in database.executed if "COUNT(*) TOTAL_COUNT" in sql)
    for sql in (page_sql, count_sql):
        _assert_system_filters_in(sql, object_column="o.OBJECT_NAME", owner_column="o.OWNER_NAME")


def test_schema_object_search_views_only_reaches_business_views() -> None:
    """種類で絞っても、システムの view に埋もれずに業務の view が 1 ページ目に出る。"""

    database = _Database()
    repository = OracleIncrementalNl2SqlRepository(connection_factory=database.connect)

    page = repository.search_schema_objects(
        cursor=None,
        limit=PAGE_LIMIT,
        query="",
        owner="",
        object_type="VIEW",
        allowed_names=None,
    )

    assert [item.object_name for item in page.items] == ["V_DEPT_PROJECT", "V_EMP_DEPT"]
    assert page.total == BUSINESS_VIEWS
    assert page.next_cursor is None


def test_db_admin_object_page_fills_first_page_and_counts_only_business_objects() -> None:
    """`GET /api/nl2sql/db-admin/objects`。

    データ管理・表 / view の管理・メタデータ SQL・合成データの対象の選択が使う。
    """

    database = _Database()
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = OracleIncrementalNl2SqlRepository(  # noqa: SLF001
        connection_factory=database.connect
    )

    def fetch(cursor: str | None) -> tuple[list[str], str | None, Any]:
        page = service.list_db_admin_objects_page(
            cursor=cursor,
            limit=PAGE_LIMIT,
            query="",
            object_type="all",
            row_state="all",
            include_counts=cursor is None,
        )
        return [item.name for item in page.items], page.next_cursor, page

    pages, first = _collect_pages(fetch)

    assert pages[0] == _business_names()[:PAGE_LIMIT]
    assert [name for page in pages for name in page] == _business_names()
    assert first[2].total == len(BUSINESS_OBJECTS)
    assert first[2].table_count == BUSINESS_TABLES
    assert first[2].view_count == BUSINESS_VIEWS


def test_schema_owner_counts_only_business_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/schema/owners` の表 / view の件数にシステムの object を数えない。"""

    database = _Database()
    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", database.connect)

    owners = adapter.fetch_schema_owners()

    assert [(item.owner, item.table_count, item.view_count) for item in owners.owners] == [
        (OWNER, BUSINESS_TABLES, BUSINESS_VIEWS)
    ]
    _assert_system_filters_in(
        database.executed[-1], object_column="o.object_name", owner_column="o.owner"
    )


@pytest.mark.parametrize(
    ("object_type", "expected"),
    [
        ("table", [name for name, kind in BUSINESS_OBJECTS if kind == "TABLE"]),
        ("view", [name for name, kind in BUSINESS_OBJECTS if kind == "VIEW"]),
    ],
    ids=["table", "view"],
)
def test_db_admin_table_and_view_lists_select_only_business_objects(
    monkeypatch: pytest.MonkeyPatch, object_type: str, expected: list[str]
) -> None:
    """`GET /api/nl2sql/db-admin/tables`・`/views`（旧 API）も SQL で除く。"""

    database = _Database()
    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", database.connect)

    items = adapter.list_db_admin_objects(object_type)

    assert [item["name"] for item in items] == expected
    # SQL が返した行がすべて業務の object（Python の後からの除外に頼らない）。
    assert database.fetched[-1] == len(expected)
    prefix = "v" if object_type == "view" else "t"
    name_column = f"{prefix}.view_name" if object_type == "view" else f"{prefix}.table_name"
    _assert_system_filters_in(
        database.executed[-1], object_column=name_column, owner_column=f"{prefix}.owner"
    )


def test_schema_manifest_selects_only_business_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema の refresh の変更検出（ALL_OBJECTS）もシステムの object を取らない。"""

    database = _Database()
    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", database.connect)
    monkeypatch.setattr(adapter, "_schema_owner_filter", lambda _column: ("1 = 1", {}))

    manifest = adapter.fetch_schema_manifest()

    assert sorted(name for _owner, name in manifest) == sorted(_business_names())
    assert database.fetched[-1] == len(BUSINESS_OBJECTS)
    _assert_system_filters_in(
        database.executed[-1], object_column="o.object_name", owner_column="o.owner"
    )


def test_deepsec_target_objects_fill_first_page_and_count_only_business_objects() -> None:
    """`GET /api/security/deepsec/target-objects`（Data Grant の対象。#1708 と同じ helper）。"""

    database = _Database()
    settings = Settings.model_construct(
        oracle_user=OWNER,
        nl2sql_schema_owner_allowlist=[],
        nl2sql_oracle_deepsec_enabled=True,
    )
    security = SecurityService(InMemorySecurityStore(), settings)
    service = DeepSecService(settings, security, cast(OraclePoolManager, database))

    def fetch(cursor: str | None) -> tuple[list[str], str | None, dict[str, object]]:
        page = service.target_objects(
            cursor=cursor, limit=PAGE_LIMIT, include_counts=cursor is None
        )
        items = cast(list[dict[str, object]], page["items"])
        return [str(item["name"]) for item in items], cast(str | None, page["next_cursor"]), page

    pages, first = _collect_pages(fetch)

    assert pages[0] == _business_names()[:PAGE_LIMIT]
    assert [name for page in pages for name in page] == _business_names()
    assert first[2]["total"] == len(BUSINESS_OBJECTS)
