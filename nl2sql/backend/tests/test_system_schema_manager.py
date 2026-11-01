from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from pr_system_settings.auth.errors import ROUTE_FORBIDDEN_CODE
from starlette.responses import JSONResponse

from app.features.settings import router as settings_router
from app.features.settings import system_schema_runtime
from app.features.settings.system_schema import (
    CONTROL_TABLE,
    MANAGED_COLUMNS,
    MANAGED_FOREIGN_KEYS,
    MANAGED_INDEXES,
    MANAGED_OBJECTS,
    MANAGED_PACKAGES,
    MANAGED_SEQUENCES,
    MANAGED_TABLES,
    MIGRATIONS,
    PRESERVED_TABLES,
    SystemSchemaActiveJobsError,
    SystemSchemaBusyError,
    SystemSchemaError,
    SystemSchemaManager,
    classify_system_schema_status,
    split_migration_sql,
)
from app.schemas.settings import SystemTablesInitializeRequest, SystemTablesStatusData
from app.security import dependencies as security_dependencies
from app.security.domain import Principal
from app.security.permissions import permission_for_route
from app.security.service import SecurityApiError
from app.settings import get_settings


def _created_objects() -> dict[str, set[str]]:
    created: dict[str, set[str]] = {
        "TABLE": set(),
        "INDEX": set(),
        "SEQUENCE": set(),
    }
    pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+|VECTOR\s+)?" r"(TABLE|INDEX|SEQUENCE)\s+([A-Z0-9_]+)",
        flags=re.IGNORECASE,
    )
    for migration in MIGRATIONS:
        for object_type, name in pattern.findall(migration.path.read_text(encoding="utf-8")):
            created[object_type.upper()].add(name.upper())
    return created


def _ready_operation_state(epoch: int = 1) -> dict[str, Any]:
    return {
        "status": "idle",
        "operation_kind": None,
        "lease_expires_at": None,
        "last_error_code": None,
        "schema_epoch": epoch,
        "updated_at": "2026-07-19T00:00:00+00:00",
    }


def _status_payload(status: str, *, existing: int | None = None) -> dict[str, Any]:
    expected = len(MANAGED_OBJECTS)
    versions = [migration.version for migration in MIGRATIONS]
    return {
        "status": status,
        "schema_head": versions[-1],
        "applied_versions": versions if status == "ready" else [],
        "pending_versions": [] if status == "ready" else versions,
        "expected_object_count": expected,
        "existing_object_count": expected if existing is None else existing,
        "expected_table_count": len(MANAGED_TABLES),
        "existing_table_count": len(MANAGED_TABLES) if status == "ready" else 0,
        "missing_objects": [],
        "tables": [],
        "objects": [],
        "operation_state": _ready_operation_state(),
    }


def test_manifest_covers_every_core_create_and_excludes_preserved_tables() -> None:
    created = _created_objects()

    assert created["TABLE"] == set(MANAGED_TABLES)
    assert created["INDEX"] == set(MANAGED_INDEXES)
    assert created["SEQUENCE"] == set(MANAGED_SEQUENCES)
    assert len(MANAGED_TABLES) == 32
    assert len(MANAGED_INDEXES) == 30
    assert len(MANAGED_SEQUENCES) == 1
    assert len(MANAGED_OBJECTS) == 65
    assert [migration.version for migration in MIGRATIONS] == [
        0,
        1,
        2,
        3,
        5,
        6,
        7,
        8,
        9,
        15,
        17,
        18,
        19,
        21,
        22,
        23,
        24,
        25,
    ]
    assert all("security" not in migration.filename for migration in MIGRATIONS)
    assert set(MANAGED_TABLES).isdisjoint(PRESERVED_TABLES)
    assert "NL2SQL_ONTOLOGY_RDF_DATA" not in MANAGED_TABLES
    assert "NL2SQL_ONTOLOGY_RDF_SEQ" not in MANAGED_SEQUENCES
    assert "NL2SQL_FEEDBACK_VECTORS" not in MANAGED_TABLES
    assert "NL2SQL_STATE_STORE" not in MANAGED_TABLES
    assert "USER_BUSINESS_SENTINEL" not in MANAGED_TABLES


def test_admin_audit_migration_creates_named_table_and_indexes() -> None:
    migration = next(item for item in MIGRATIONS if item.version == 23)
    assert migration.created_objects == frozenset(
        {
            ("NL2SQL_ADMIN_AUDIT_LOG", "TABLE"),
            ("IX_NL2SQL_ADMIN_AUDIT_TIME", "INDEX"),
            ("IX_NL2SQL_ADMIN_AUDIT_ACTOR", "INDEX"),
        }
    )
    statements = split_migration_sql(migration.path.read_text())
    assert len(statements) == 3
    assert "CONSTRAINT PK_NL2SQL_ADMIN_AUDIT_LOG PRIMARY KEY" in statements[0]


def test_db_admin_chat_migration_creates_conversation_tables_and_indexes() -> None:
    migration = next(item for item in MIGRATIONS if item.version == 24)
    assert migration.created_objects == frozenset(
        {
            ("NL2SQL_DB_ADMIN_CHATS", "TABLE"),
            ("NL2SQL_DB_ADMIN_CHAT_TURNS", "TABLE"),
            ("IX_NL2SQL_DB_ADMIN_CHAT_OWNER", "INDEX"),
            ("IX_NL2SQL_DB_ADMIN_CHAT_TIME", "INDEX"),
            ("IX_NL2SQL_DB_ADMIN_CHAT_TURN_CONV", "INDEX"),
        }
    )
    statements = split_migration_sql(migration.path.read_text())
    assert len(statements) == 5
    # ターンは会話を消すと一緒に消える（FK の ON DELETE CASCADE）。
    assert "ON DELETE CASCADE" in statements[1]
    # 会話の表はターンの表より先に作り、DROP は manifest の逆順（ターンが先）。
    assert MANAGED_TABLES.index("NL2SQL_DB_ADMIN_CHATS") < MANAGED_TABLES.index(
        "NL2SQL_DB_ADMIN_CHAT_TURNS"
    )


def test_object_metadata_covers_manifest_order_and_marks_missing_objects() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    created_at = datetime(2026, 8, 29, tzinfo=UTC)
    existing_objects = {
        (MANAGED_TABLES[0], "TABLE"): created_at,
        (MANAGED_INDEXES[0], "INDEX"): created_at,
    }
    table_metadata = [
        {
            "name": MANAGED_TABLES[0],
            "exists": True,
            "estimated_rows": 12,
            "created_at": created_at.isoformat(),
            "last_analyzed_at": "2026-08-29T01:00:00+00:00",
        }
    ]

    metadata = manager._build_object_metadata(existing_objects, table_metadata, owner="NL2SQL_APP")

    assert [(item["name"], item["object_type"]) for item in metadata] == list(MANAGED_OBJECTS)
    assert sum(item["object_type"] == "TABLE" for item in metadata) == 32
    assert sum(item["object_type"] == "INDEX" for item in metadata) == 30
    assert sum(item["object_type"] == "SEQUENCE" for item in metadata) == 1
    assert metadata[0] == {
        "name": MANAGED_TABLES[0],
        "owner": "NL2SQL_APP",
        "qualified_name": f"NL2SQL_APP.{MANAGED_TABLES[0]}",
        "object_type": "TABLE",
        "exists": True,
        "estimated_rows": 12,
        "created_at": created_at.isoformat(),
        "last_analyzed_at": "2026-08-29T01:00:00+00:00",
    }
    first_index = metadata[len(MANAGED_TABLES)]
    assert first_index["name"] == MANAGED_INDEXES[0]
    assert first_index["exists"] is True
    assert first_index["estimated_rows"] is None
    assert first_index["last_analyzed_at"] is None
    assert metadata[-3] == {
        "name": MANAGED_SEQUENCES[0],
        "owner": "NL2SQL_APP",
        "qualified_name": f"NL2SQL_APP.{MANAGED_SEQUENCES[0]}",
        "object_type": "SEQUENCE",
        "exists": False,
        "estimated_rows": None,
        "created_at": None,
        "last_analyzed_at": None,
    }


def test_object_metadata_leaves_owner_empty_when_schema_owner_is_unknown() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)

    metadata = manager._build_object_metadata({}, [])

    assert metadata[0]["name"] == MANAGED_TABLES[0]
    assert metadata[0]["owner"] == ""
    assert metadata[0]["qualified_name"] == ""


class _SchemaOwnerCursor:
    def __init__(self, statements: list[str], row: tuple[Any, ...] | None) -> None:
        self.statements = statements
        self.row = row

    def __enter__(self) -> _SchemaOwnerCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        self.statements.append(sql)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class _SchemaOwnerConnection:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.statements: list[str] = []
        self.row = row

    def cursor(self) -> _SchemaOwnerCursor:
        return _SchemaOwnerCursor(self.statements, self.row)


def test_schema_owner_is_loaded_from_connected_user() -> None:
    connection = _SchemaOwnerConnection(("nl2sql_app",))

    assert SystemSchemaManager._load_schema_owner(connection) == "NL2SQL_APP"
    assert connection.statements == ["SELECT USER FROM DUAL"]
    assert SystemSchemaManager._load_schema_owner(_SchemaOwnerConnection(None)) == ""


def test_manifest_checksums_are_actual_sha256_values() -> None:
    for migration in MIGRATIONS:
        assert migration.path.is_file()
        assert re.fullmatch(r"[0-9a-f]{64}", migration.checksum)


def test_system_schema_status_has_four_deterministic_states() -> None:
    expected = set(MANAGED_OBJECTS)
    checksums = {migration.version: migration.checksum for migration in MIGRATIONS}
    one_domain_table = next(
        item
        for item in expected
        if item[1] == "TABLE" and item[0] not in {CONTROL_TABLE, "NL2SQL_SCHEMA_MIGRATIONS"}
    )

    assert classify_system_schema_status(set(), {}) == "missing"
    assert classify_system_schema_status({one_domain_table}, {}) == "partial"
    assert classify_system_schema_status(expected, {**checksums, 6: "stale"}) == "outdated"
    assert classify_system_schema_status(expected, checksums) == "ready"


def test_splitter_ignores_comments_and_keeps_multiline_statements() -> None:
    sql = "-- header\nCREATE TABLE EXAMPLE (\n ID NUMBER\n);\n-- trailing\n"
    assert split_migration_sql(sql) == ["CREATE TABLE EXAMPLE (\n ID NUMBER\n)"]


def test_splitter_keeps_single_line_plsql_cleanup_blocks() -> None:
    migration = next(item for item in MIGRATIONS if item.version == 15)
    statements = split_migration_sql(migration.path.read_text(encoding="utf-8"))

    assert len(statements) == 2
    assert "DROP TABLE NL2SQL_ONTOLOGY_RDF_DATA" in statements[0]
    assert "SQLCODE != -942" in statements[0]
    assert statements[0].endswith("END;")
    assert "DROP SEQUENCE NL2SQL_ONTOLOGY_RDF_SEQ" in statements[1]
    assert "SQLCODE != -2289" in statements[1]
    assert statements[1].endswith("END;")


def test_splitter_keeps_multiline_plsql_until_top_level_end_and_ignores_slash() -> None:
    sql = """
-- SQL*Plus style anonymous block.
BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE NL2SQL_TEMP';
    IF SQLCODE != -942 THEN
        RAISE;
    END IF;
END;
/
CREATE TABLE NL2SQL_TEMP (
    ID NUMBER
);
"""

    assert split_migration_sql(sql) == [
        "\n".join(
            [
                "BEGIN",
                "    EXECUTE IMMEDIATE 'DROP TABLE NL2SQL_TEMP';",
                "    IF SQLCODE != -942 THEN",
                "        RAISE;",
                "    END IF;",
                "END;",
            ]
        ),
        "\n".join(
            [
                "CREATE TABLE NL2SQL_TEMP (",
                "    ID NUMBER",
                ")",
            ]
        ),
    ]


def test_rdf_cleanup_migration_updates_stale_v2_and_drops_retired_objects() -> None:
    manager = SystemSchemaManager()
    status = _partial_status(
        applied_versions=[0, 1, 3, 5, 6, 7, 8, 9],
        pending_versions=[2, 15],
        missing_objects=[],
    )

    planned = manager._plan_migrations(status)

    assert [migration.version for migration in planned] == [2, 15]
    cleanup = planned[-1]
    assert cleanup.created_objects == frozenset()
    statements = split_migration_sql(cleanup.path.read_text(encoding="utf-8"))
    assert "DROP TABLE NL2SQL_ONTOLOGY_RDF_DATA" in statements[0]
    assert "DROP SEQUENCE NL2SQL_ONTOLOGY_RDF_SEQ" in statements[1]


class _LeaseState:
    status = "IDLE"
    owner: str | None = None
    expires_at: datetime | None = None


class _LeaseCursor:
    def __init__(self, state: _LeaseState) -> None:
        self.state = state
        self.rowcount = 0

    def __enter__(self) -> _LeaseCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        assert "UPDATE NL2SQL_SCHEMA_OPERATIONS" in sql
        now = datetime.now(UTC)
        can_claim = (
            self.state.status != "RUNNING"
            or self.state.expires_at is None
            or self.state.expires_at < now
        )
        if can_claim:
            self.state.status = "RUNNING"
            self.state.owner = str(binds["lease_owner"])
            self.state.expires_at = now + timedelta(seconds=int(binds["lease_seconds"]))
            self.rowcount = 1
        else:
            self.rowcount = 0


class _LeaseConnection:
    def __init__(self, state: _LeaseState) -> None:
        self.state = state

    def cursor(self) -> _LeaseCursor:
        return _LeaseCursor(self.state)

    def commit(self) -> None:
        return None


def test_database_lease_rejects_concurrency_and_allows_expired_takeover() -> None:
    state = _LeaseState()

    @contextmanager
    def connection() -> Any:
        yield _LeaseConnection(state)

    manager = SystemSchemaManager(connection, lease_seconds=30)
    manager._claim_lease("first", "initialize")
    with pytest.raises(SystemSchemaBusyError):
        manager._claim_lease("second", "recreate")

    state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    manager._claim_lease("second", "recreate")
    assert state.owner == "second"


def test_recreate_requires_exact_confirmation_before_database_access() -> None:
    manager = SystemSchemaManager(lambda: pytest.fail("database must not be accessed"))

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize(recreate=True, confirmation="wrong")

    assert error.value.code == "SCHEMA_RECREATE_CONFIRMATION_REQUIRED"
    assert error.value.status_code == 422


def test_recreate_drop_statements_only_use_manifest_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SystemSchemaManager()
    objects = set(MANAGED_OBJECTS) | {("USER_BUSINESS_SENTINEL", "TABLE")}
    statements: list[str] = []

    def record_drop(_connection: Any, statement: str) -> int:
        statements.append(statement)
        return 1

    monkeypatch.setattr(manager, "_load_objects", lambda _connection: dict.fromkeys(objects))
    monkeypatch.setattr(manager, "_heartbeat", lambda _connection, _owner: None)
    monkeypatch.setattr(
        manager,
        "_execute_drop",
        record_drop,
    )

    dropped = manager._drop_managed_objects(object(), "owner")

    assert dropped == len(MANAGED_OBJECTS) - 1 - len(MANAGED_PACKAGES)
    assert f"DROP PACKAGE {MANAGED_PACKAGES[0]}" in statements
    assert all("USER_BUSINESS_SENTINEL" not in statement for statement in statements)
    assert all("NL2SQL_AUTH_" not in statement for statement in statements)
    assert all("NL2SQL_FEEDBACK_VECTORS" not in statement for statement in statements)
    assert all(f"DROP TABLE {CONTROL_TABLE}" not in statement for statement in statements)


class _WorkflowManager(SystemSchemaManager):
    def __init__(
        self,
        initial_status: str,
        *,
        fail_once: bool = False,
        failure_message: str = "password=secret ORA-00600 full sql must not leak",
    ) -> None:
        super().__init__(
            lambda: self._connection(),
            ddl_lock_timeout_seconds=1,
        )
        self.initial_status = initial_status
        self.ready = initial_status == "ready"
        self.fail_once = fail_once
        self.failure_message = failure_message
        self.failures: list[str] = []
        self.applied_migrations: list[int] = []

    @contextmanager
    def _connection(self) -> Any:
        yield object()

    def _ensure_control_schema(self) -> None:
        return None

    def _claim_lease(self, owner: str, operation_kind: str) -> None:
        return None

    def _status_on(self, connection: Any) -> dict[str, Any]:
        return _status_payload("ready" if self.ready else self.initial_status, existing=0)

    def _configure_ddl_lock_timeout(self, connection: Any) -> None:
        return None

    def _apply_migration(self, connection: Any, migration: Any) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError(self.failure_message)
        self.applied_migrations.append(migration.version)
        if migration.version == MIGRATIONS[-1].version:
            self.ready = True

    def _heartbeat(self, connection: Any, owner: str) -> None:
        return None

    def _finish_operation(
        self,
        connection: Any,
        owner: str,
        *,
        increment_epoch: bool,
    ) -> None:
        return None

    def _record_failure(self, owner: str, error_code: str) -> None:
        self.failures.append(error_code)


def test_initialize_is_idempotent_and_failure_is_safe_and_retryable() -> None:
    ready = _WorkflowManager("ready")
    no_op = ready.initialize()
    assert no_op["operation"] == "no_op"
    assert no_op["applied_versions"] == [migration.version for migration in MIGRATIONS]
    assert ready.applied_migrations == []

    manager = _WorkflowManager("missing", fail_once=True)
    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == "ORA-00600"
    assert "secret" not in error.value.public_message
    assert manager.failures == ["ORA-00600"]

    retried = manager.initialize()
    assert retried["operation"] == "initialized"
    assert retried["status"] == "ready"
    assert retried["applied_versions"] == [migration.version for migration in MIGRATIONS]


def _partial_status(
    *,
    applied_versions: list[int],
    missing_objects: list[tuple[str, str]],
    pending_versions: list[int] | None = None,
) -> dict[str, Any]:
    return {
        **_status_payload("partial", existing=len(MANAGED_OBJECTS) - len(missing_objects)),
        "applied_versions": applied_versions,
        "pending_versions": (
            pending_versions
            if pending_versions is not None
            else [
                migration.version
                for migration in MIGRATIONS
                if migration.version not in applied_versions
            ]
        ),
        "missing_objects": [
            {"name": name, "object_type": object_type} for name, object_type in missing_objects
        ],
    }


def test_selective_plan_only_applies_versions_for_missing_evaluation_objects() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    status = _partial_status(
        applied_versions=[0, 1, 2, 3, 5, 6],
        pending_versions=[7, 8],
        missing_objects=[
            ("NL2SQL_EVALUATION_JOBS", "TABLE"),
            ("NL2SQL_EVALUATION_RESULTS", "TABLE"),
            ("IX_NL2SQL_EVAL_JOB_STATE", "INDEX"),
            ("IX_NL2SQL_EVAL_JOB_LEASE", "INDEX"),
        ],
    )

    assert [migration.version for migration in manager._plan_migrations(status)] == [7, 8]


def test_selective_plan_replays_only_owner_of_one_missing_object() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    status = _partial_status(
        applied_versions=[migration.version for migration in MIGRATIONS],
        pending_versions=[],
        missing_objects=[("IX_NL2SQL_EVAL_JOB_LEASE", "INDEX")],
    )

    assert [migration.version for migration in manager._plan_migrations(status)] == [8]


def test_selective_plan_replays_only_checksum_mismatch() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    status = _partial_status(
        applied_versions=[migration.version for migration in MIGRATIONS if migration.version != 5],
        pending_versions=[5],
        missing_objects=[],
    )

    assert [migration.version for migration in manager._plan_migrations(status)] == [5]


class _IncrementalWorkflowManager(_WorkflowManager):
    def __init__(self) -> None:
        super().__init__("partial")
        self.before = _partial_status(
            applied_versions=[0, 1, 2, 3, 5, 6],
            pending_versions=[7, 8, 9, 15, 17, 18, 19, 21, 22, 23, 24, 25],
            missing_objects=[
                ("NL2SQL_EVALUATION_JOBS", "TABLE"),
                ("NL2SQL_EVALUATION_RESULTS", "TABLE"),
                ("IX_NL2SQL_EVAL_JOB_STATE", "INDEX"),
                ("IX_NL2SQL_EVAL_JOB_LEASE", "INDEX"),
                ("UX_NL2SQL_PROFILES_NAME", "INDEX"),
            ],
        )

    def _status_on(self, connection: Any) -> dict[str, Any]:
        return _status_payload("ready") if self.ready else self.before


def test_incremental_update_reaches_ready_without_replaying_old_migrations() -> None:
    manager = _IncrementalWorkflowManager()

    result = manager.initialize()

    assert manager.applied_migrations == [7, 8, 9, 15, 17, 18, 19, 21, 22, 23, 24, 25]
    assert result["operation"] == "migrated"
    assert result["status"] == "ready"
    assert result["existing_object_count"] == len(MANAGED_OBJECTS)
    assert result["applied_versions"] == [migration.version for migration in MIGRATIONS]


class _RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        sql: str,
        binds: dict[str, Any] | None = None,
    ) -> None:
        self.statements.append(sql)


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.statements)

    def commit(self) -> None:
        self.commits += 1


class _ForeignKeyStateManager(SystemSchemaManager):
    def __init__(self, states: dict[str, str]) -> None:
        super().__init__(ddl_lock_timeout_seconds=1)
        self.states = states

    def _foreign_key_state(self, connection: Any, expected: Any) -> Any:
        return self.states[expected.name]


def test_apply_migration_sends_plsql_blocks_with_terminating_semicolon() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    connection = _RecordingConnection()

    manager._apply_migration(connection, next(item for item in MIGRATIONS if item.version == 15))

    plsql_statements = [
        statement
        for statement in connection.statements
        if statement.lstrip().upper().startswith("BEGIN")
    ]
    assert len(plsql_statements) == 2
    assert all(statement.rstrip().endswith("END;") for statement in plsql_statements)
    assert "/" not in connection.statements
    assert "MERGE INTO NL2SQL_SCHEMA_MIGRATIONS" in connection.statements[-1]


def test_v7_resume_skips_matching_first_foreign_key_and_creates_second() -> None:
    first, second = MANAGED_FOREIGN_KEYS[:2]
    manager = _ForeignKeyStateManager(
        {
            first.name: "matching",
            second.name: "missing",
        }
    )
    connection = _RecordingConnection()

    manager._apply_migration(connection, next(item for item in MIGRATIONS if item.version == 7))

    executed = "\n".join(connection.statements)
    assert f"ADD CONSTRAINT\n    {first.name}" not in executed
    assert f"ADD CONSTRAINT\n    {second.name}" in executed
    assert "MERGE INTO NL2SQL_SCHEMA_MIGRATIONS" in executed
    assert connection.commits == 1


def test_v7_resume_rejects_same_name_with_wrong_foreign_key_definition() -> None:
    first, second = MANAGED_FOREIGN_KEYS[:2]
    manager = _ForeignKeyStateManager(
        {
            first.name: "matching",
            second.name: "mismatch",
        }
    )

    with pytest.raises(SystemSchemaError) as error:
        manager._apply_migration(
            _RecordingConnection(),
            next(item for item in MIGRATIONS if item.version == 7),
        )

    assert error.value.code == "SCHEMA_CONSTRAINT_MISMATCH"
    assert second.name in error.value.public_message


class _ForeignKeyDictionaryCursor:
    def __init__(self, connection: _ForeignKeyDictionaryConnection) -> None:
        self.connection = connection
        self.query_kind = ""

    def __enter__(self) -> _ForeignKeyDictionaryCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        if "SELECT child.TABLE_NAME" in sql:
            self.query_kind = "constraint"
        elif "SELECT referenced_column.COLUMN_NAME" in sql:
            self.query_kind = "referenced_columns"
        else:
            self.query_kind = "columns"

    def fetchone(self) -> tuple[str, ...]:
        return (
            "NL2SQL_ONTOLOGY_PROFILE_VIEWS",
            "R",
            "CASCADE",
            "NL2SQL_PROFILES",
            self.connection.status,
            "VALIDATED",
            "NOT DEFERRABLE",
            "IMMEDIATE",
        )

    def fetchall(self) -> list[tuple[str]]:
        return [("PROFILE_ID",)]


class _ForeignKeyDictionaryConnection:
    def __init__(self, *, status: str = "ENABLED") -> None:
        self.status = status

    def cursor(self) -> _ForeignKeyDictionaryCursor:
        return _ForeignKeyDictionaryCursor(self)


def test_foreign_key_dictionary_validation_checks_enforcement_state() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    expected = MANAGED_FOREIGN_KEYS[1]

    assert manager._foreign_key_state(_ForeignKeyDictionaryConnection(), expected) == "matching"
    assert (
        manager._foreign_key_state(
            _ForeignKeyDictionaryConnection(status="DISABLED"),
            expected,
        )
        == "mismatch"
    )


def test_schema_connection_sets_bounded_ddl_lock_timeout() -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=30)
    connection = _RecordingConnection()

    manager._configure_ddl_lock_timeout(connection)

    assert connection.statements == ["ALTER SESSION SET DDL_LOCK_TIMEOUT = 30"]


def test_ora_00054_is_retryable_conflict_with_recovery_message() -> None:
    manager = _WorkflowManager(
        "missing",
        fail_once=True,
        failure_message="ORA-00054: resource busy and acquire with NOWAIT specified",
    )

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()

    assert error.value.code == "ORA-00054"
    assert error.value.status_code == 409
    assert "1 秒以内" in error.value.public_message
    assert "SQL生成評価 job" in error.value.public_message
    assert manager.failures == ["ORA-00054"]

    result = manager.initialize()
    assert result["status"] == "ready"


class _ActiveJobCursor(_RecordingCursor):
    def __init__(self, statements: list[str], active_table: str) -> None:
        super().__init__(statements)
        self.active_table = active_table
        self.current_sql = ""

    def execute(
        self,
        sql: str,
        binds: dict[str, Any] | None = None,
    ) -> None:
        super().execute(sql, binds)
        self.current_sql = sql

    def fetchone(self) -> tuple[int]:
        return (1,) if self.active_table in self.current_sql else (0,)


class _ActiveJobConnection(_RecordingConnection):
    def __init__(self, active_table: str) -> None:
        super().__init__()
        self.active_table = active_table

    def cursor(self) -> _ActiveJobCursor:
        return _ActiveJobCursor(self.statements, self.active_table)


@pytest.mark.parametrize(
    "active_table",
    [
        "NL2SQL_SCHEMA_REFRESH_JOBS",
        "NL2SQL_ONTOLOGY_JOBS",
        "NL2SQL_EVALUATION_JOBS",
        "NL2SQL_SYNTHETIC_RUNS",
    ],
)
def test_recreate_rejects_each_active_persistent_job_type(
    monkeypatch: pytest.MonkeyPatch,
    active_table: str,
) -> None:
    manager = SystemSchemaManager(ddl_lock_timeout_seconds=1)
    connection = _ActiveJobConnection(active_table)
    existing_job_tables = {
        (name, "TABLE"): None
        for name in (
            "NL2SQL_SCHEMA_REFRESH_JOBS",
            "NL2SQL_ONTOLOGY_JOBS",
            "NL2SQL_EVALUATION_JOBS",
            "NL2SQL_SYNTHETIC_RUNS",
        )
    }
    monkeypatch.setattr(manager, "_load_objects", lambda _connection: existing_job_tables)

    with pytest.raises(SystemSchemaActiveJobsError):
        manager._assert_no_active_jobs(connection)


class _ApiManager:
    def status(self) -> dict[str, Any]:
        return _status_payload("partial", existing=4)

    def initialize(
        self, *, recreate: bool, confirmation: str | None, allow_destructive: bool = False
    ) -> dict[str, Any]:
        result = _status_payload("ready")
        return {
            **result,
            "operation": "recreated" if recreate else "migrated",
            "applied_versions": [migration.version for migration in MIGRATIONS],
            "dropped_object_count": 42 if recreate else 0,
            "created_object_count": 48 if recreate else 44,
        }


class _LockedApiManager(_ApiManager):
    def initialize(
        self, *, recreate: bool, confirmation: str | None, allow_destructive: bool = False
    ) -> dict[str, Any]:
        raise SystemSchemaError(
            "ORA-00054",
            "Oracle の対象オブジェクトのロックが待機時間内に解放されませんでした。",
            status_code=409,
        )


def _system_tables_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/settings/database/system-tables/initialize",
            "headers": [],
        }
    )


def test_system_tables_api_contract_and_strict_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_router, "system_schema_manager", _ApiManager())
    monkeypatch.setattr(
        settings_router,
        "reset_system_schema_runtime",
        lambda *, schema_epoch: None,
    )

    status = settings_router.get_system_tables_status()
    initialized = settings_router.initialize_system_tables(
        _system_tables_request(), SystemTablesInitializeRequest(recreate=False)
    )

    assert status.data is not None
    assert status.data.status == "partial"
    assert not isinstance(initialized, JSONResponse)
    assert initialized.data is not None
    assert initialized.data.operation == "migrated"
    assert permission_for_route("GET", "/settings/database/system-tables") == frozenset(
        {"menu.settings_system_tables"}
    )
    assert permission_for_route("POST", "/settings/database/system-tables/initialize") == frozenset(
        {"menu.settings_system_tables"}
    )


def test_system_tables_api_exposes_retryable_ora_00054_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_router, "system_schema_manager", _LockedApiManager())

    response = settings_router.initialize_system_tables(
        _system_tables_request(), SystemTablesInitializeRequest(recreate=False)
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "5"
    body = json.loads(bytes(response.body))
    assert body["data"] is None
    assert body["error_messages"] == [
        "Oracle の対象オブジェクトのロックが待機時間内に解放されませんでした。"
    ]
    assert body["warning_messages"] == []
    assert body["error_code"] == "ORA-00054"
    assert body["problem"]["code"] == "ORA-00054"
    assert body["problem"]["type"] == "urn:nl2sql:problem:ora-00054"
    assert body["problem"]["request_id"]
    assert response.headers["X-Request-ID"] == body["problem"]["request_id"]
    assert body["problem"]["retryable"] is True


def test_schema_epoch_change_resets_each_runtime_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeManager:
        epoch = 10

        def current_epoch(self) -> int:
            return self.epoch

    class FakeNl2Sql:
        uses_incremental_store = True

        def reset_after_system_schema_change(self) -> None:
            events.append("nl2sql-reset")

        def recover_persistence(self) -> None:
            events.append("persistence-recovered")

    class FakeOntology:
        def reset_after_system_schema_change(self) -> None:
            events.append("ontology-reset")

    manager = FakeManager()
    monkeypatch.setattr(system_schema_runtime, "system_schema_manager", manager)
    monkeypatch.setattr(system_schema_runtime, "nl2sql_service", FakeNl2Sql())
    monkeypatch.setattr(system_schema_runtime, "ontology_runtime", FakeOntology())
    system_schema_runtime.reset_observed_system_schema_epoch()

    assert system_schema_runtime.observe_system_schema_epoch() is False
    manager.epoch = 11
    assert system_schema_runtime.observe_system_schema_epoch() is True
    assert system_schema_runtime.observe_system_schema_epoch() is False
    assert events == ["nl2sql-reset", "ontology-reset", "persistence-recovered"]


@pytest.mark.asyncio
async def test_system_table_post_requires_csrf_and_sql_execute_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_auth_session_cookie_name", "pr_session")
    monkeypatch.setattr(settings, "app_auth_csrf_cookie_name", "pr_csrf")

    async def inline_threadpool(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    class FakeSecurityService:
        def __init__(self, principal: Principal) -> None:
            self.principal = principal

        def authenticate_session(self, token: str) -> Principal:
            assert token == "session-token"
            return self.principal

        def verify_csrf(
            self,
            principal: Principal,
            cookie_csrf: str,
            header_csrf: str,
        ) -> None:
            if cookie_csrf != "csrf-token" or header_csrf != cookie_csrf:
                raise SecurityApiError(403, "CSRF token を確認してください。")

    def principal(permissions: set[str]) -> Principal:
        return Principal(
            user_uuid="user-1",
            login_user_id="viewer",
            display_name="閲覧者",
            status="ACTIVE",
            force_password_change=False,
            role_codes=["DB_VIEWER"],
            permissions=permissions,
            allowed_profile_ids=set(),
            session_id="session-1",
            csrf_token_hash="hash",
        )

    def request(*, csrf: bool) -> Request:
        headers = [(b"cookie", b"pr_session=session-token; pr_csrf=csrf-token")]
        if csrf:
            headers.append((b"x-csrf-token", b"csrf-token"))
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/settings/database/system-tables/initialize",
            "headers": headers,
            "route": SimpleNamespace(path="/api/settings/database/system-tables/initialize"),
        }
        return Request(scope)

    monkeypatch.setattr(security_dependencies, "run_in_threadpool", inline_threadpool)

    viewer_service = FakeSecurityService(principal({"menu.settings_database"}))
    monkeypatch.setattr(
        security_dependencies,
        "get_security_service",
        lambda: viewer_service,
    )
    with pytest.raises(SecurityApiError) as no_csrf:
        await anext(security_dependencies.authorize_api_request(request(csrf=False)))
    assert no_csrf.value.status_code == 403

    with pytest.raises(SecurityApiError) as no_execute:
        await anext(security_dependencies.authorize_api_request(request(csrf=True)))
    assert no_execute.value.status_code == 403
    assert no_execute.value.code == ROUTE_FORBIDDEN_CODE

    executor_service = FakeSecurityService(principal({"menu.settings_system_tables"}))
    monkeypatch.setattr(
        security_dependencies,
        "get_security_service",
        lambda: executor_service,
    )
    dependency = cast(
        AsyncGenerator[None, None],
        security_dependencies.authorize_api_request(request(csrf=True)),
    )
    await anext(dependency)
    await dependency.aclose()


def test_action_package_migration_preserves_complete_plsql_and_transaction() -> None:
    migration = next(item for item in MIGRATIONS if item.version == 22)
    assert migration.created_objects == frozenset(
        {("NL2SQL_ONT_ACTION_TX", "PACKAGE"), ("NL2SQL_ONT_ACTION_TX", "PACKAGE BODY")}
    )
    statements = split_migration_sql(migration.path.read_text())
    assert len(statements) == 3
    assert statements[0].startswith("CREATE OR REPLACE PACKAGE ")
    assert statements[1].startswith("CREATE OR REPLACE PACKAGE BODY ")
    assert statements[1].endswith("END NL2SQL_ONT_ACTION_TX;")
    assert "COMMIT;" not in statements[1] and "AUTONOMOUS_TRANSACTION" not in statements[1]
    assert "FOR UPDATE WAIT 5" in statements[1]
    assert "CLIENT_IDENTIFIER" in statements[1]


def test_cli_check_requires_system_and_security_tables(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配備の確認（#1648）: system table が ready で、認証・権限の表がそろうときだけ 0。"""
    import contextlib

    import app.cli.app_security_migrate as security_migrate
    import app.clients.oracle_runtime as oracle_runtime
    from app.cli import nl2sql_system_schema

    missing: list[str] = []
    state: dict[str, Any] = {"status": "ready", "operation_state": {"status": "idle"}}

    class _Pool:
        @contextlib.contextmanager
        def control_connection(self) -> Any:
            yield object()

    class _Manager:
        def status(self) -> dict[str, Any]:
            if isinstance(state.get("error"), Exception):
                raise state["error"]
            return state

    monkeypatch.setattr(nl2sql_system_schema, "system_schema_manager", _Manager())
    monkeypatch.setattr(oracle_runtime, "get_oracle_pool_manager", lambda: _Pool())
    monkeypatch.setattr(security_migrate, "missing_security_tables", lambda _connection: missing)
    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", "--check"])

    assert nl2sql_system_schema.main() == 0
    assert capsys.readouterr().out.strip() == "status=ready"

    missing.append("NL2SQL_APP_ROLE_PROFILES")
    assert nl2sql_system_schema.main() == 1
    output = capsys.readouterr().out
    assert "認証・権限の表が不足 1 件（NL2SQL_APP_ROLE_PROFILES" in output
    assert "app_security_migrate --apply" in output

    state["error"] = RuntimeError("ORA-01031: insufficient privileges password=secret")
    assert nl2sql_system_schema.main() == 1
    output = capsys.readouterr().out
    assert "ORA-01031" in output
    assert "secret" not in output

    # ORA でない失敗（設定の読み込みなど）は、値を伏せた手がかりを出す（#1701）。
    state["error"] = ValueError("設定ファイルを読み込めません: /srv/x.json password=secret")
    assert nl2sql_system_schema.main() == 1
    output = capsys.readouterr().out
    assert "設定ファイルを読み込めません: /srv/x.json" in output
    assert "secret" not in output

    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", "--status"])
    assert nl2sql_system_schema.main() == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "SCHEMA_OPERATION_FAILED"
    assert error["hint"] == "ValueError: 設定ファイルを読み込めません: /srv/x.json password=***"

    state["error"] = RuntimeError("ORA-01031: insufficient privileges")
    assert nl2sql_system_schema.main() == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "ORA-01031"
    assert "hint" not in error


def test_cli_reports_real_cause_of_pool_timeout_without_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pool の時間切れ（DPY-4005）は、直接 1 回だけ接続して本当の原因を出す（#1744）。"""
    from pr_backend_core import oracle_connect_diagnosis

    from app.cli import nl2sql_system_schema

    calls: list[dict[str, Any]] = []

    class _Oracledb:
        def connect(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise RuntimeError("DPY-6005: cannot connect to database (CONNECTION_ID=x).\ntimed out")

    class _Manager:
        def status(self) -> dict[str, Any]:
            raise RuntimeError("DPY-4005: timed out waiting for the connection pool")

    monkeypatch.setattr(oracle_connect_diagnosis, "_default_oracledb_loader", lambda: _Oracledb())
    monkeypatch.setattr(
        nl2sql_system_schema,
        "_connect_kwargs",
        lambda: {"user": "APP", "password": "Pa55-word-x", "dsn": "db_high"},
    )
    monkeypatch.setattr(nl2sql_system_schema, "system_schema_manager", _Manager())

    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", "--status"])
    assert nl2sql_system_schema.main() == 1
    output = capsys.readouterr().out
    error = json.loads(output)["error"]
    assert error["code"] == "DPY-6005"
    assert error["cause"] == "unreachable"
    assert error["hint"].startswith("ADB が停止しているか、この接続元から DB に届きません。")
    assert "起動" in error["hint"]
    assert "Pa55-word-x" not in output
    assert len(calls) == 1 and calls[0]["retry_count"] == 0

    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", "--check"])
    assert nl2sql_system_schema.main() == 1
    output = capsys.readouterr().out
    assert output.startswith(
        "状態を読めません（SCHEMA_STATUS_UNAVAILABLE: DPY-6005: ADB が停止しているか"
    )
    assert "Pa55-word-x" not in output


@pytest.mark.parametrize("flag", ["--status", "--check"], ids=["status", "check"])
def test_cli_connects_without_tns_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """CLI は TNS の再試行を外した接続で DB に繋ぐ（DB の停止中に長く待たない。#1779）。"""
    from pr_backend_core.oracle_dsn import cli_connect_mode_enabled

    from app.cli import nl2sql_system_schema

    seen: list[bool] = []

    class _Manager:
        def status(self) -> dict[str, Any]:
            seen.append(cli_connect_mode_enabled())
            raise RuntimeError("ORA-01031: insufficient privileges")

    assert not cli_connect_mode_enabled()
    monkeypatch.setattr(nl2sql_system_schema, "system_schema_manager", _Manager())
    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", flag])
    assert nl2sql_system_schema.main() == 1
    capsys.readouterr()
    assert seen == [True]


@pytest.mark.parametrize("cli", [False, True], ids=["server", "cli"])
def test_control_pool_strips_descriptor_retry_only_in_cli(
    monkeypatch: pytest.MonkeyPatch, cli: bool
) -> None:
    """常駐の server の control の pool は接続引数を変えない（#1779）。

    Wallet の別名の再試行は #1214 で外し済みで、記述子の DSN はそのまま。CLI だけ、記述子の
    再試行も外し、短い tcp_connect_timeout にする。
    """
    from pr_backend_core.oracle_dsn import CLI_TCP_CONNECT_TIMEOUT_SECONDS, enable_cli_connect_mode

    from app.clients import oracle_runtime
    from app.settings import get_settings

    descriptor = (
        "(description= (retry_count=20)(retry_delay=3)"
        "(address=(protocol=tcps)(port=1522)(host=adb.example.com))"
        "(connect_data=(service_name=x_nl2sql_high.adb.oraclecloud.com)))"
    )
    created: list[dict[str, Any]] = []

    class _Oracledb:
        POOL_GETMODE_TIMEDWAIT = 3

        def create_pool(self, **kwargs: Any) -> Any:
            created.append(kwargs)
            return object()

    base_kwargs = {"user": "APP", "dsn": descriptor, "tcp_connect_timeout": 10}
    monkeypatch.setattr(
        oracle_runtime, "oracle_connect_kwargs", lambda *_a, **_k: dict(base_kwargs)
    )
    manager = oracle_runtime.OraclePoolManager(get_settings())
    monkeypatch.setattr(manager, "_load_oracledb", lambda: _Oracledb())
    monkeypatch.setattr(manager, "_initialize_driver", lambda _oracledb: None)
    if cli:
        enable_cli_connect_mode()

    manager._get_pool()

    kwargs = created[0]
    assert kwargs["getmode"] == 3 and kwargs["max"] == 4
    if cli:
        assert kwargs["dsn"] == descriptor.replace("(retry_count=20)(retry_delay=3)", "")
        assert (kwargs["retry_count"], kwargs["retry_delay"]) == (0, 0)
        assert kwargs["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS
    else:
        assert kwargs["dsn"] == descriptor
        assert "retry_count" not in kwargs
        assert kwargs["tcp_connect_timeout"] == 10


# ---- 台帳が適用済みでも欠けた列（#2458） ---------------------------------------------


class _ColumnsCursor:
    """状態の取得に要る dictionary・台帳だけを返す fake（表は `removed` の列だけを欠く）。"""

    def __init__(self, removed: dict[str, set[str]], executed: list[str]) -> None:
        self._removed = removed
        self._executed = executed
        self._rows: list[tuple[Any, ...]] = []
        self._row: tuple[Any, ...] | None = None
        self.rowcount = 0

    def __enter__(self) -> _ColumnsCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        upper = " ".join(sql.split()).upper()
        self._executed.append(upper)
        self._rows, self._row, self.rowcount = [], None, 0
        if upper.startswith("SELECT OBJECT_NAME, OBJECT_TYPE, CREATED FROM USER_OBJECTS"):
            self._rows = [(name, object_type, None) for name, object_type in MANAGED_OBJECTS]
        elif upper.startswith("SELECT VERSION_NO, CHECKSUM FROM NL2SQL_SCHEMA_MIGRATIONS"):
            self._rows = [(migration.version, migration.checksum) for migration in MIGRATIONS]
        elif upper.startswith("SELECT TABLE_NAME, COLUMN_NAME FROM USER_TAB_COLUMNS"):
            names = sorted(str(value).upper() for value in (binds or {}).values())
            self._rows = [
                (name, column)
                for name in names
                for column in MANAGED_COLUMNS.get(name, ())
                if column not in self._removed.get(name, set())
            ]
        elif upper.startswith("SELECT USER FROM DUAL"):
            self._row = ("APP_USER",)
        elif upper.startswith("SELECT STATUS, OPERATION_KIND"):
            self._row = ("IDLE", None, None, None, 1, None)
        elif upper.startswith(f"UPDATE {CONTROL_TABLE}"):
            self.rowcount = 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _ColumnsConnection:
    def __init__(self, removed: dict[str, set[str]] | None = None) -> None:
        self.removed = removed or {}
        self.executed: list[str] = []

    def cursor(self) -> _ColumnsCursor:
        return _ColumnsCursor(self.removed, self.executed)

    def commit(self) -> None:
        return None


def _columns_manager() -> SystemSchemaManager:
    return SystemSchemaManager(lambda: cast(Any, None), ddl_lock_timeout_seconds=0)


def test_managed_columns_include_columns_added_by_later_migrations() -> None:
    """版ごとの migration を順に当てた後の列（後の版の ALTER TABLE ADD を含む）。"""

    assert set(MANAGED_COLUMNS) == set(MANAGED_TABLES)
    # 021 が PL/SQL の EXECUTE IMMEDIATE で足す列と、006 が ALTER TABLE ADD (...) で足す列。
    assert "PROFILE_ID" in MANAGED_COLUMNS["NL2SQL_ONTOLOGY_REVISIONS"]
    assert {"WORKER_ID", "HEARTBEAT_AT", "LEASE_EXPIRES_AT", "ATTEMPT_NO"} <= set(
        MANAGED_COLUMNS["NL2SQL_SCHEMA_REFRESH_JOBS"]
    )
    assert not any(
        column in {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}
        for columns in MANAGED_COLUMNS.values()
        for column in columns
    )


def test_status_is_ready_when_every_managed_column_exists() -> None:
    status = _columns_manager()._status_on(_ColumnsConnection())

    assert status["status"] == "ready"
    assert status["missing_columns"] == []
    assert SystemTablesStatusData.model_validate(status).missing_columns == []


def test_status_detects_missing_columns_even_when_ledger_is_current() -> None:
    """台帳がすべて適用済みでも、正本の列が欠けた表があれば ready にしない（#2458）。"""

    connection = _ColumnsConnection({"NL2SQL_ONTOLOGY_REVISIONS": {"PROFILE_ID"}})

    status = _columns_manager()._status_on(connection)

    assert status["status"] == "outdated"
    assert status["pending_versions"] == []
    assert status["missing_objects"] == []
    assert status["missing_columns"] == [
        {"table_name": "NL2SQL_ONTOLOGY_REVISIONS", "column_name": "PROFILE_ID"}
    ]
    payload = SystemTablesStatusData.model_validate(status).model_dump()
    assert payload["missing_columns"] == status["missing_columns"]


def test_initialize_stops_with_column_names_when_columns_stay_missing() -> None:
    """列を足す migration が台帳で適用済みなら更新では直らない。列の名前を出して止める。"""

    connection = _ColumnsConnection({"NL2SQL_SCHEMA_REFRESH_JOBS": {"WORKER_ID"}})

    with pytest.raises(SystemSchemaError) as raised:
        _columns_manager()._initialize_on(connection, "owner-1", recreate=False)

    assert raised.value.code == "SCHEMA_POSTCONDITION_FAILED"
    assert "NL2SQL_SCHEMA_REFRESH_JOBS.WORKER_ID" in raised.value.public_message
    # 適用する migration は無い（DDL を流していない）。
    assert not any(
        statement.startswith(("CREATE ", "ALTER TABLE")) for statement in connection.executed
    )
