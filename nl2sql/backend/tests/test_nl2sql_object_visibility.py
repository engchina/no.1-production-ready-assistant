"""系统对象不进入用户可见目录的回归测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaRefreshJob,
    SchemaRefreshTargetObject,
    SchemaTable,
    SchemaViewDependency,
)
from app.features.nl2sql.object_visibility import (
    filter_user_visible_catalog,
    is_user_visible_object_name,
    is_user_visible_schema_object,
    system_object_prefix_sql_filters,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings


def _table(name: str, *, table_type: str = "table", owner: str = "APP") -> SchemaTable:
    return SchemaTable(
        owner=owner,
        table_name=name,
        table_type=table_type,
        logical_name=name,
        columns=[
            SchemaColumn(
                column_name="ID",
                logical_name="ID",
                data_type="NUMBER",
                nullable=False,
            )
        ],
    )


def test_object_name_visibility_rejects_dollar_and_hash_markers() -> None:
    assert is_user_visible_object_name("ORDERS") is True
    assert is_user_visible_object_name("TD_NL2SQL_ORDERS") is True
    assert is_user_visible_object_name("NL2SQL_APP.ORDERS") is True
    assert is_user_visible_object_name("DBTOOLS$EXECUTION_HISTORY") is False
    assert is_user_visible_object_name("SYS#AUDIT") is False
    assert is_user_visible_object_name("NL2SQL_SCHEMA_OBJECTS") is False
    assert is_user_visible_object_name('"nl2sql_schema_objects"') is False
    assert is_user_visible_object_name("APP.NL2SQL_SCHEMA_OBJECTS") is False
    assert is_user_visible_schema_object("APP", "ORDERS") is True
    assert is_user_visible_schema_object("NL2SQL_APP", "ORDERS") is True
    assert is_user_visible_schema_object("APP", "TD_NL2SQL_ORDERS") is True
    assert is_user_visible_schema_object("APP", "NL2SQL_SCHEMA_OBJECTS") is False
    assert is_user_visible_schema_object("RMAN$CATALOG", "RC_BACKUP_ARCHIVELOG_DETAILS") is False
    assert is_user_visible_schema_object("SYS#CATALOG", "AUDIT_LOG") is False


def test_catalog_filter_removes_system_objects_and_dependencies() -> None:
    catalog = SchemaCatalog(
        refreshed_at="2026-07-22T00:00:00+00:00",
        tables=[
            _table("ORDERS"),
            _table("TD_NL2SQL_ORDERS"),
            _table("ORDERS", owner="NL2SQL_APP"),
            _table("NL2SQL_SCHEMA_OBJECTS"),
            _table("DBTOOLS$EXECUTION_HISTORY"),
            _table("SYS#AUDIT", table_type="view"),
            _table(
                "RC_BACKUP_ARCHIVELOG_DETAILS",
                owner="RMAN$CATALOG",
                table_type="view",
            ),
        ],
        view_dependencies=[
            SchemaViewDependency(
                owner="APP",
                view_name="ORDER_VIEW",
                referenced_owner="APP",
                referenced_name="ORDERS",
                referenced_type="TABLE",
            ),
            SchemaViewDependency(
                owner="APP",
                view_name="ORDER_VIEW",
                referenced_owner="APP",
                referenced_name="DBTOOLS$EXECUTION_HISTORY",
                referenced_type="TABLE",
            ),
            SchemaViewDependency(
                owner="APP",
                view_name="ORDER_VIEW",
                referenced_owner="APP",
                referenced_name="NL2SQL_SCHEMA_OBJECTS",
                referenced_type="TABLE",
            ),
            SchemaViewDependency(
                owner="APP",
                view_name="ORDER_VIEW",
                referenced_owner="RMAN$CATALOG",
                referenced_name="RC_BACKUP_ARCHIVELOG_DETAILS",
                referenced_type="VIEW",
            ),
            SchemaViewDependency(
                owner="RMAN$CATALOG",
                view_name="RC_BACKUP_ARCHIVELOG_DETAILS",
                referenced_owner="APP",
                referenced_name="ORDERS",
                referenced_type="TABLE",
            ),
        ],
    )

    visible = filter_user_visible_catalog(catalog)

    assert [(table.owner, table.table_name) for table in visible.tables] == [
        ("APP", "ORDERS"),
        ("APP", "TD_NL2SQL_ORDERS"),
        ("NL2SQL_APP", "ORDERS"),
    ]
    assert [item.referenced_name for item in visible.view_dependencies] == ["ORDERS"]


def test_memory_schema_pages_filter_before_counts_and_detail_lookup() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(
        refreshed_at="2026-07-22T00:00:00+00:00",
        tables=[
            _table("ORDERS"),
            _table("TD_NL2SQL_ORDERS"),
            _table("NL2SQL_SCHEMA_OBJECTS"),
            _table("DBTOOLS$EXECUTION_HISTORY"),
            _table("SYS#AUDIT", table_type="view"),
            _table("RC_ARCHIVED_LOG", owner="RMAN$CATALOG", table_type="view"),
        ],
    )
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest={
            ("APP", "ORDERS"): "v1",
            ("APP", "TD_NL2SQL_ORDERS"): "v1",
            ("APP", "NL2SQL_SCHEMA_OBJECTS"): "v1",
            ("APP", "DBTOOLS$EXECUTION_HISTORY"): "v1",
            ("APP", "SYS#AUDIT"): "v1",
            ("RMAN$CATALOG", "RC_ARCHIVED_LOG"): "v1",
        },
        changed_keys={
            ("APP", "ORDERS"),
            ("APP", "TD_NL2SQL_ORDERS"),
            ("APP", "NL2SQL_SCHEMA_OBJECTS"),
            ("APP", "DBTOOLS$EXECUTION_HISTORY"),
            ("APP", "SYS#AUDIT"),
            ("RMAN$CATALOG", "RC_ARCHIVED_LOG"),
        },
        deleted_keys=set(),
    )

    page = repository.search_schema_objects(
        cursor=None,
        limit=100,
        query="",
        owner="APP",
        object_type="",
        allowed_names=None,
        row_state="all",
    )

    assert [item.object_name for item in page.items] == ["ORDERS", "TD_NL2SQL_ORDERS"]
    assert (page.total, page.table_count, page.view_count) == (2, 2, 0)
    assert repository.get_catalog_head().object_count == 2
    assert repository.get_schema_object("APP", "NL2SQL_SCHEMA_OBJECTS") is None
    assert repository.get_schema_object("APP", "SYS#AUDIT") is None
    assert repository.get_schema_object("RMAN$CATALOG", "RC_ARCHIVED_LOG") is None


def test_oracle_admin_list_filters_both_markers_in_sql_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        executed = ""

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _binds: object | None = None) -> None:
            self.executed = sql

        def fetchall(self) -> list[tuple[object, ...]]:
            return [
                ("ORDERS", "APP", 1, "受注"),
                ("TD_NL2SQL_ORDERS", "APP", 1, "業務"),
                ("NL2SQL_SCHEMA_OBJECTS", "APP", 2, "NL2SQL system"),
                ("ORDERS", "NL2SQL_APP", 1, "受注"),
                ("DBTOOLS$EXECUTION_HISTORY", "APP", 4, "内部履歴"),
                ("SYS#AUDIT", "APP", 1, "内部監査"),
                ("RC_BACKUP_ARCHIVELOG_DETAILS", "RMAN$CATALOG", 2, "RMAN"),
            ]

    class Connection:
        def __init__(self, cursor: Cursor) -> None:
            self._cursor = cursor

        def cursor(self) -> Cursor:
            return self._cursor

    cursor = Cursor()

    @contextmanager
    def connection() -> Iterator[Connection]:
        yield Connection(cursor)

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", connection)

    items = adapter.list_db_admin_objects("table")

    assert [item["qualified_name"] for item in items] == [
        "APP.ORDERS",
        "APP.TD_NL2SQL_ORDERS",
        "NL2SQL_APP.ORDERS",
    ]
    for fragment in (
        "INSTR(t.owner, '$') = 0",
        "INSTR(t.owner, '#') = 0",
        "INSTR(t.table_name, '$') = 0",
        "INSTR(t.table_name, '#') = 0",
        *system_object_prefix_sql_filters("t.table_name"),
    ):
        assert fragment in cursor.executed
    # owner 名は接頭辞で除かない（`NL2SQL_APP` などの業務の schema を見せる）。
    assert "UPPER(T.OWNER) NOT LIKE" not in cursor.executed.upper()


def test_saved_profile_scope_does_not_expose_system_objects() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._profiles["business"] = Nl2SqlProfile(  # noqa: SLF001
        id="business",
        name="业务",
        object_scope_version=2,
        allowed_tables=[
            "APP.ORDERS",
            "APP.TD_NL2SQL_ORDERS",
            "APP.NL2SQL_SCHEMA_OBJECTS",
            "NL2SQL_APP.ORDERS",
            "APP.DBTOOLS$EXECUTION_HISTORY",
            "RMAN$CATALOG.RC_BACKUP_ARCHIVELOG_DETAILS",
        ],
        allowed_views=["APP.SYS#AUDIT_VIEW"],
    )

    profile = service.get_profile("business")

    assert profile.allowed_tables == [
        "APP.ORDERS",
        "APP.TD_NL2SQL_ORDERS",
        "NL2SQL_APP.ORDERS",
    ]
    assert profile.allowed_views == []
    assert service.profile_allowed_object_names(profile) == [
        "APP.ORDERS",
        "APP.TD_NL2SQL_ORDERS",
        "NL2SQL_APP.ORDERS",
    ]


def test_schema_refresh_targets_ignore_system_owners() -> None:
    job = SchemaRefreshJob(
        job_id="refresh-1",
        created_at="2026-07-22T00:00:00+00:00",
        target_objects=[
            SchemaRefreshTargetObject(owner="APP", object_name="ORDERS"),
            SchemaRefreshTargetObject(owner="APP", object_name="NL2SQL_SCHEMA_OBJECTS"),
            SchemaRefreshTargetObject(owner="NL2SQL_APP", object_name="ORDERS"),
            SchemaRefreshTargetObject(
                owner="RMAN$CATALOG",
                object_name="RC_BACKUP_ARCHIVELOG_DETAILS",
            ),
            SchemaRefreshTargetObject(owner="APP", object_name="DBTOOLS$EXECUTION_HISTORY"),
        ],
    )

    target_keys = Nl2SqlService._schema_refresh_target_keys(job)  # noqa: SLF001
    expected_state = Nl2SqlService._schema_refresh_expected_state_by_key(job)  # noqa: SLF001

    assert target_keys == {("APP", "ORDERS"), ("NL2SQL_APP", "ORDERS")}
    assert expected_state == {
        ("APP", "ORDERS"): "unknown",
        ("NL2SQL_APP", "ORDERS"): "unknown",
    }


@pytest.mark.parametrize(
    "name",
    ["PLATFORM_USERS", "APP.PLATFORM_AUTH_SESSIONS", "RAG_DOCUMENTS", "AGENT_RUNTIME_RUNS"],
)
def test_shared_platform_and_other_product_tables_are_hidden(name: str) -> None:
    """同じ schema を共有する認証テーブルや他製品のテーブルを業務一覧に出さない（#212）。"""
    assert is_user_visible_object_name(name) is False
    assert is_user_visible_schema_object("APP", name.split(".")[-1]) is False
    # 接頭辞が一致しない業務テーブルは従来どおり見える。
    assert is_user_visible_object_name("PLATFORMS") is True
    assert is_user_visible_object_name("APP.RAGE_EVENTS") is True


def test_system_object_prefix_sql_filters_match_python_visibility() -> None:
    """SQL の除外の条件は Python の可視性の判定と同じ接頭辞を、大文字小文字を区別せずに除く。

    #1708
    """

    filters = system_object_prefix_sql_filters("o.object_name")

    assert filters == (
        "UPPER(o.object_name) NOT LIKE 'NL2SQL\\_%' ESCAPE '\\'",
        "UPPER(o.object_name) NOT LIKE 'PLATFORM\\_%' ESCAPE '\\'",
        "UPPER(o.object_name) NOT LIKE 'RAG\\_%' ESCAPE '\\'",
        "UPPER(o.object_name) NOT LIKE 'AGENT\\_%' ESCAPE '\\'",
    )
    for name in ("NL2SQL_X", "platform_users", "Rag_Chunks", "AGENT_RUNS"):
        assert not is_user_visible_object_name(name)
    # `_` は LIKE の 1 文字の wildcard なので escape する（`RAGX…` のような業務の表を落とさない）。
    assert is_user_visible_object_name("RAGXDATA")
    assert all("\\_%" in item for item in filters)
