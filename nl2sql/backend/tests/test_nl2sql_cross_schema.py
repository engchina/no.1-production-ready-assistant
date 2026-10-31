from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from app.features.nl2sql.models import (
    AllowedObjects,
    AssetRefreshData,
    Nl2SqlEngine,
    Nl2SqlProfile,
    SampleDataStep,
    SchemaCatalog,
    SchemaTable,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError, OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings


def _table(owner: str, name: str, table_type: str = "TABLE") -> SchemaTable:
    return SchemaTable(
        owner=owner,
        table_name=name,
        logical_name=f"{owner} {name}",
        table_type=table_type,
    )


def _service(*tables: SchemaTable, current_owner: str = "APP") -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(  # noqa: SLF001 - scope resolver unit boundary
        refreshed_at="2026-07-19T00:00:00+00:00",
        current_owner=current_owner,
        tables=list(tables),
    )
    return service


SAMPLE_OBJECTS = ["DEPARTMENT", "EMPLOYEE", "PROJECT", "V_EMP_DEPT", "V_DEPT_PROJECT"]


def _sample_tables(owner: str) -> list[SchemaTable]:
    # 導入判定は種類と列構成で行うため、サンプル DDL と同じ列を持たせる。
    ddl = Nl2SqlService(store=MemoryNl2SqlStore())
    return [
        item.model_copy(update={"owner": owner, "logical_name": f"{owner} {item.table_name}"})
        for item in (*ddl._sample_tables_from_ddl(), *ddl._sample_views_from_ddl())  # noqa: SLF001
    ]


def test_schema_table_exposes_canonical_qualified_name() -> None:
    assert _table("SH", "ORDERS").model_dump()["qualified_name"] == "SH.ORDERS"
    assert _table("APP", "lower").model_dump()["qualified_name"] == 'APP."lower"'


def test_empty_owner_allowlist_discovers_non_maintained_visible_scope() -> None:
    settings = get_settings().model_copy(
        update={"nl2sql_schema_owner_allowlist": [], "oracle_user": "APP"}
    )
    adapter = OracleNl2SqlAdapter(settings)

    owner_filter, binds = adapter._schema_owner_filter("c.owner")  # noqa: SLF001

    assert "all_users" in owner_filter
    assert "oracle_maintained" in owner_filter
    assert "CURRENT_SCHEMA" not in owner_filter
    assert binds == {}


def test_explicit_owner_allowlist_is_an_upper_bound() -> None:
    settings = get_settings().model_copy(update={"nl2sql_schema_owner_allowlist": ["SH", "SSB"]})
    adapter = OracleNl2SqlAdapter(settings)

    owner_filter, binds = adapter._schema_owner_filter("c.owner")  # noqa: SLF001

    assert "oracle_maintained" in owner_filter
    assert "c.owner IN" in owner_filter
    assert set(binds.values()) == {"SH", "SSB"}


def test_schema_owner_discovery_excludes_maintained_and_intersects_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        ("APP", "N", 1, 4, 1),
        ("SH", "N", 0, 8, 2),
        ("SSB", "N", 0, 6, 0),
        ("RMAN$CATALOG", "N", 0, 123, 2),
        ("SYS", "Y", 0, 100, 50),
    ]

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

        def __iter__(self) -> Iterator[tuple[Any, ...]]:
            return iter(rows)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    @contextmanager
    def connection() -> Iterator[Connection]:
        yield Connection()

    settings = get_settings().model_copy(
        update={"nl2sql_schema_owner_allowlist": ["SH", "RMAN$CATALOG"], "oracle_user": "APP"}
    )
    adapter = OracleNl2SqlAdapter(settings)
    monkeypatch.setattr(adapter, "connection", connection)

    discovered = adapter.fetch_schema_owners()

    assert discovered.current_owner == "APP"
    assert [item.owner for item in discovered.owners] == ["SH"]
    assert discovered.owners[0].table_count == 8
    assert discovered.owners[0].view_count == 2
    assert discovered.excluded_oracle_maintained_count == 1


def test_legacy_bare_name_prefers_current_owner_and_keeps_duplicate_external_object() -> None:
    service = _service(_table("APP", "ORDERS"), _table("SH", "ORDERS"))

    profile = service.create_profile(
        Nl2SqlProfile(id="orders", name="受注", allowed_tables=["ORDERS", "SH.ORDERS"])
    )

    assert profile.allowed_tables == ["APP.ORDERS", "SH.ORDERS"]
    assert profile.object_scope_version == 2
    assert service.build_select_ai_profile_attributes(profile)["object_list"] == [
        {"owner": "APP", "name": "ORDERS"},
        {"owner": "SH", "name": "ORDERS"},
    ]


def test_legacy_bare_name_uses_unique_external_match_and_blocks_ambiguous_match() -> None:
    unique = _service(_table("SH", "SALES"), current_owner="APP")
    profile = unique.create_profile(
        Nl2SqlProfile(id="sales", name="売上", allowed_tables=["SALES"])
    )
    assert profile.allowed_tables == ["SH.SALES"]

    ambiguous = _service(
        _table("SH", "ORDERS"),
        _table("SSB", "ORDERS"),
        current_owner="APP",
    )
    with pytest.raises(ValueError, match="複数 schema"):
        ambiguous.create_profile(Nl2SqlProfile(id="orders", name="受注", allowed_tables=["ORDERS"]))


def test_owner_aware_semantic_scope_rejects_unqualified_external_table() -> None:
    service = _service(
        _table("APP", "ORDERS"),
        _table("SH", "ORDERS"),
        _table("SSB", "CUSTOMERS"),
    )
    allowed = AllowedObjects(
        table_names=["SH.ORDERS", "SSB.CUSTOMERS"],
        enforce_table_scope=True,
    )

    unqualified = service.analyze_sql("SELECT * FROM ORDERS", allowed, None)
    qualified = service.analyze_sql("SELECT * FROM SH.ORDERS", allowed, None)
    joined = service.analyze_sql(
        "SELECT o.ORDER_ID FROM SH.ORDERS o JOIN SSB.CUSTOMERS c ON c.CUSTOMER_ID = o.CUSTOMER_ID",
        allowed,
        None,
    )

    assert unqualified.safety.is_safe is False
    assert unqualified.safety.referenced_tables == ["APP.ORDERS"]
    assert qualified.safety.is_safe is True
    assert qualified.safety.referenced_tables == ["SH.ORDERS"]
    assert joined.safety.is_safe is True
    assert joined.safety.referenced_tables == ["SH.ORDERS", "SSB.CUSTOMERS"]


def test_legacy_empty_scope_snapshots_current_owner_only() -> None:
    service = _service(
        _table("APP", "LOCAL_TABLE"),
        _table("SH", "EXTERNAL_TABLE"),
    )
    legacy = Nl2SqlProfile(id="legacy", name="旧 Profile")

    migrated = service._profile_scope_for_read(legacy)  # noqa: SLF001

    assert migrated.allowed_tables == ["APP.LOCAL_TABLE"]
    assert migrated.object_scope_version == 2


def test_sample_imported_status_is_scoped_to_current_owner() -> None:
    service = _service(*_sample_tables("SH"), current_owner="APP")

    assert service.sample_data_info().imported_objects == []

    service._catalog.current_owner = "SH"  # noqa: SLF001 - owner scoped sample status

    assert service.sample_data_info().imported_objects == SAMPLE_OBJECTS


def test_sample_import_catalog_state_keeps_other_owner_duplicates() -> None:
    service = _service(_table("SH", "DEPARTMENT"), current_owner="APP")

    service._apply_sample_import_to_catalog(SampleDataStep.ALL)  # noqa: SLF001

    remaining = {(table.owner, table.table_name) for table in service._catalog.tables}  # noqa: SLF001
    assert ("SH", "DEPARTMENT") in remaining
    assert ("APP", "DEPARTMENT") in remaining
    assert service.sample_data_info().imported_objects == SAMPLE_OBJECTS


def test_sample_delete_removes_current_owner_only() -> None:
    service = _service(*_sample_tables("APP"), *_sample_tables("SH"), current_owner="APP")

    service._remove_sample_from_state()  # noqa: SLF001 - regression for owner scoped deletion

    remaining = {(table.owner, table.table_name) for table in service._catalog.tables}  # noqa: SLF001
    assert not {("APP", name) for name in SAMPLE_OBJECTS} & remaining
    assert {("SH", name) for name in SAMPLE_OBJECTS} <= remaining
    assert service.sample_data_info().imported_objects == []

    service._catalog.current_owner = "SH"  # noqa: SLF001 - verify other owner remains imported

    assert service.sample_data_info().imported_objects == SAMPLE_OBJECTS


def test_legacy_empty_scope_migration_is_persisted_as_a_snapshot() -> None:
    service = _service(
        _table("APP", "LOCAL_TABLE"),
        _table("SH", "EXTERNAL_TABLE"),
    )
    service._profiles["legacy"] = Nl2SqlProfile(  # noqa: SLF001
        id="legacy",
        name="旧 Profile",
    )

    first = service.get_profile("legacy")
    service._catalog.tables.append(_table("APP", "ADDED_LATER"))  # noqa: SLF001
    second = service.get_profile("legacy")

    assert first.allowed_tables == ["APP.LOCAL_TABLE"]
    assert second.allowed_tables == ["APP.LOCAL_TABLE"]
    assert service._profiles["legacy"].object_scope_version == 2  # noqa: SLF001


def test_owner_qualified_column_restriction_does_not_confuse_duplicate_tables() -> None:
    service = _service(_table("APP", "ORDERS"), _table("SH", "ORDERS"))
    allowed = AllowedObjects(
        table_names=["SH.ORDERS"],
        columns={"SH.ORDERS": ["ORDER_ID"]},
        enforce_table_scope=True,
    )

    blocked = service.analyze_sql(
        "SELECT SH.ORDERS.SECRET_VALUE FROM SH.ORDERS",
        allowed,
        None,
    )
    accepted = service.analyze_sql(
        "SELECT SH.ORDERS.ORDER_ID FROM SH.ORDERS",
        allowed,
        None,
    )

    assert blocked.safety.is_safe is False
    assert blocked.safety.blocked_reason == "許可されていない列を参照しています。"
    assert accepted.safety.is_safe is True


def test_unsynchronized_select_ai_scope_blocks_agent_use() -> None:
    service = _service(_table("SH", "ORDERS"))
    profile = Nl2SqlProfile(
        id="sales",
        name="売上",
        allowed_tables=["SH.ORDERS"],
        object_scope_version=2,
    )
    profile_name = service._select_ai_profile_name(profile)  # noqa: SLF001
    service._asset_meta[Nl2SqlEngine.SELECT_AI] = AssetRefreshData(  # noqa: SLF001
        engine=Nl2SqlEngine.SELECT_AI,
        refreshed=False,
        status="error",
        profile_name=profile_name,
        warning="scope mismatch",
    )

    with pytest.raises(OracleAdapterError, match="object scope が未同期"):
        service._assert_select_ai_scope_ready(profile)  # noqa: SLF001


def test_select_ai_scope_failure_remains_profile_specific_after_other_sync() -> None:
    service = _service(_table("SH", "ORDERS"), _table("SSB", "CUSTOMERS"))
    failed = Nl2SqlProfile(
        id="sales",
        name="売上",
        allowed_tables=["SH.ORDERS"],
        object_scope_version=2,
    )
    ready = Nl2SqlProfile(
        id="customers",
        name="顧客",
        allowed_tables=["SSB.CUSTOMERS"],
        object_scope_version=2,
    )
    failed_name = service._select_ai_profile_name(failed)  # noqa: SLF001
    ready_name = service._select_ai_profile_name(ready)  # noqa: SLF001

    service._record_select_ai_scope_state(  # noqa: SLF001
        profile_name=failed_name,
        expected_scope={"SH.ORDERS"},
        actual_scope=set(),
        warning="scope mismatch",
    )
    service._record_select_ai_scope_state(  # noqa: SLF001
        profile_name=ready_name,
        expected_scope={"SSB.CUSTOMERS"},
        actual_scope={"SSB.CUSTOMERS"},
    )

    with pytest.raises(OracleAdapterError, match="object scope が未同期"):
        service._assert_select_ai_scope_ready(failed)  # noqa: SLF001
    service._assert_select_ai_scope_ready(ready)  # noqa: SLF001
