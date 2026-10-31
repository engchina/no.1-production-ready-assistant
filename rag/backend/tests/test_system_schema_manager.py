"""RAG Oracle system schema manager の決定論テスト。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pr_system_settings.system_schema import ForeignKeySpec, foreign_keys_from_create_table

from app.clients.oracle import oracle_vector_index_create_sql
from app.rag import oracle_schema
from app.rag.oracle_schema import oracle_schema_migration_sections, split_sql_statements
from app.rag.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    DOMAIN_TABLES,
    MANAGED_COLUMNS,
    MANAGED_FOREIGN_KEYS,
    MANAGED_INDEXES,
    MANAGED_OBJECTS,
    MANAGED_TABLES,
    MIGRATIONS,
    RECREATE_CONFIRMATION,
    RETIRED_MANAGED_OBJECTS,
    RETIRED_TABLES_APPROVAL,
    SystemSchemaActiveJobsError,
    SystemSchemaBusyError,
    SystemSchemaError,
    SystemSchemaManager,
    classify_system_schema_status,
    managed_manifest_from_schema,
)


class _FakeCursor:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self.rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        sql = re.sub(r"\s+", " ", statement).strip()
        upper = sql.upper()
        params = parameters or {}
        self.rows = []
        self.rowcount = 0
        self.database.executed.append(upper)

        if "EXECUTE IMMEDIATE 'DROP TABLE RAG_QUERY_HISTORY PURGE'" in upper:
            self.database.objects.pop(("RAG_QUERY_HISTORY", "TABLE"), None)
            self.database.objects.pop(("RAG_QUERY_HISTORY_PROFILE_IDX", "INDEX"), None)
            return
        if upper.startswith("DECLARE"):
            # 列を無ければ足す migration（`ALTER TABLE <表> ADD (<列> ...)`）を fake で適用する。
            for table, columns in self.database.removed_columns.items():
                columns.difference_update(
                    {
                        column
                        for column in columns
                        if f"'ALTER TABLE {table} ADD ({column} " in upper
                    }
                )
        if "PROCEDURE RENAME_TABLE" in upper:
            # dictionary 検査付きの migration 呼び出しを fake Oracle で適用する。
            for kind, old, new in re.findall(
                r"RENAME_(TABLE|INDEX|CONSTRAINT)\('([^']+)', '([^']+)'\);", upper
            ):
                if kind in {"TABLE", "INDEX"}:
                    old_key, new_key = (old, kind), (new, kind)
                    if old_key in self.database.objects and new_key in self.database.objects:
                        raise RuntimeError("ORA-20060: PROFILE_RENAME_CONFLICT")
                    if old_key in self.database.objects:
                        self.database.objects[new_key] = self.database.objects.pop(old_key)
                if kind == "TABLE":
                    self.database.foreign_keys = {
                        key: (
                            replace(
                                fk,
                                table_name=new if fk.table_name == old else fk.table_name,
                                referenced_table_name=new
                                if fk.referenced_table_name == old
                                else fk.referenced_table_name,
                            ),
                            valid,
                        )
                        for key, (fk, valid) in self.database.foreign_keys.items()
                    }
                if kind == "CONSTRAINT" and old in self.database.foreign_keys:
                    fk, valid = self.database.foreign_keys.pop(old)
                    self.database.foreign_keys[new] = (replace(fk, name=new), valid)
            for table, old, new in re.findall(
                r"RENAME_COLUMN\('([^']+)', '([^']+)', '([^']+)'\);", upper
            ):
                self.database.foreign_keys = {
                    key: (
                        replace(
                            fk,
                            columns=tuple(
                                new if c == old and fk.table_name == table else c
                                for c in fk.columns
                            ),
                            referenced_columns=tuple(
                                new if c == old and fk.referenced_table_name == table else c
                                for c in fk.referenced_columns
                            ),
                        ),
                        valid,
                    )
                    for key, (fk, valid) in self.database.foreign_keys.items()
                }
            return
        if upper.startswith("SELECT OBJECT_NAME, OBJECT_TYPE, CREATED FROM USER_OBJECTS"):
            names = {str(value).upper() for value in params.values()}
            self.rows = [
                (name, object_type, created_at)
                for (name, object_type), created_at in sorted(self.database.objects.items())
                if name in names and object_type in {"TABLE", "INDEX"}
            ]
            return
        if upper.startswith("SELECT PRE_NAME FROM CTX_USER_PREFERENCES"):
            if ("RAG_TEXT_WORLD_LEXER", "TEXT_PREFERENCE") in self.database.objects:
                self.rows = [("RAG_TEXT_WORLD_LEXER",)]
            return
        if upper.startswith("SELECT SPL_NAME FROM CTX_USER_STOPLISTS"):
            if ("RAG_TEXT_STOPLIST", "TEXT_STOPLIST") in self.database.objects:
                self.rows = [("RAG_TEXT_STOPLIST",)]
            return
        if upper.startswith("SELECT MIGRATION_NAME, CHECKSUM FROM RAG_SCHEMA_MIGRATIONS"):
            self.rows = sorted(self.database.migrations.items())
            return
        if upper.startswith("SELECT TABLE_NAME, NUM_ROWS, LAST_ANALYZED FROM USER_TABLES"):
            names = {str(value).upper() for value in params.values()}
            self.rows = [
                (name, 0, None)
                for name in sorted(names)
                if (name, "TABLE") in self.database.objects
            ]
            return
        if upper.startswith("SELECT STATUS, OPERATION_KIND"):
            operation = self.database.operation
            if operation is not None:
                self.rows = [
                    (
                        operation["status"],
                        operation["operation_kind"],
                        operation["lease_expires_at"],
                        operation["last_error_code"],
                        operation["schema_epoch"],
                        operation["updated_at"],
                    )
                ]
            return
        if upper.startswith("SELECT COUNT(*) FROM RAG_INGESTION_JOBS"):
            self.rows = [(self.database.active_jobs,)]
            return
        if upper.startswith("SELECT TABLE_NAME FROM USER_TABLES"):
            names = {str(value).upper() for value in params.values()}
            self.rows = [
                (name,) for name in sorted(names) if (name, "TABLE") in self.database.objects
            ]
            return
        if upper.startswith("SELECT TABLE_NAME, COLUMN_NAME FROM USER_TAB_COLUMNS"):
            # 表は正本の列を持つ（`removed_columns` の列だけを欠く）。
            names = {str(value).upper() for value in params.values()}
            self.rows = [
                (name, column)
                for name in sorted(names)
                if (name, "TABLE") in self.database.objects
                for column in MANAGED_COLUMNS.get(name, ())
                if column not in self.database.removed_columns.get(name, set())
            ]
            return
        if upper.startswith("SELECT CHILD.CONSTRAINT_NAME"):
            names = {str(value).upper() for value in params.values()}
            self.rows = [
                (
                    fk.name,
                    fk.table_name,
                    fk.referenced_table_name,
                    fk.delete_rule,
                    "DISABLED" if fk.name in self.database.disabled_foreign_keys else "ENABLED",
                    "VALIDATED" if validated else "NOT VALIDATED",
                    column,
                    referenced,
                )
                for fk, validated in sorted(
                    self.database.foreign_keys.values(), key=lambda item: item[0].name
                )
                if fk.table_name in names
                for column, referenced in zip(fk.columns, fk.referenced_columns, strict=True)
            ]
            return
        orphan_match = re.match(r"SELECT COUNT\(\*\) FROM ([A-Z0-9_$#]+) CHILD WHERE", upper)
        if orphan_match is not None:
            self.rows = [(self.database.orphans.get(orphan_match.group(1), 0),)]
            return
        if upper.startswith("ALTER TABLE") and " FOREIGN KEY " in upper:
            self.database.foreign_key_ddl.append(sql)
            self._add_foreign_key(upper)
            return
        if upper.startswith("ALTER TABLE") and " DROP CONSTRAINT " in upper:
            self.database.foreign_key_ddl.append(sql)
            name = upper.split()[-1]
            if name not in self.database.foreign_keys:
                raise RuntimeError("ORA-02443: Cannot drop constraint - nonexistent constraint")
            self.database.foreign_keys.pop(name)
            self.database.disabled_foreign_keys.discard(name)
            return
        modify_match = re.match(r"ALTER TABLE (\S+) MODIFY CONSTRAINT (\S+) (.+)$", upper)
        if modify_match is not None:
            self.database.foreign_key_ddl.append(sql)
            table, name, state = modify_match.groups()
            spec, _validated = self.database.foreign_keys[name]
            validate = state in {"ENABLE VALIDATE", "VALIDATE"}
            if validate and self.database.orphans.get(table, 0):
                raise RuntimeError("ORA-02298: cannot validate - parent keys not found")
            self.database.disabled_foreign_keys.discard(name)
            self.database.foreign_keys[name] = (spec, validate)
            return
        delete_match = re.match(r"DELETE FROM ([A-Z0-9_$#]+) CHILD WHERE", upper)
        if delete_match is not None:
            table = delete_match.group(1)
            self.rowcount = self.database.orphans.get(table, 0)
            self.database.orphans[table] = 0
            return

        if upper.startswith("ALTER SESSION SET DDL_LOCK_TIMEOUT"):
            return
        if upper.startswith("CREATE TABLE"):
            name = upper.split()[2]
            if (name, "TABLE") in self.database.objects:
                raise RuntimeError("ORA-00955: name is already used by an existing object")
            self.database.objects[(name, "TABLE")] = datetime.now(UTC)
            for foreign_key in foreign_keys_from_create_table(statement):
                self.database.foreign_keys[foreign_key.name] = (foreign_key, True)
            return
        index_match = re.match(
            r"CREATE (?:UNIQUE |VECTOR )?INDEX ([A-Z][A-Z0-9_$#]*)",
            upper,
        )
        if index_match is not None:
            name = index_match.group(1)
            if self.database.fail_drop_or_create_with_lock:
                self.database.fail_drop_or_create_with_lock = False
                raise RuntimeError("ORA-00054: resource busy")
            if (name, "INDEX") in self.database.objects:
                raise RuntimeError("ORA-00955: name is already used by an existing object")
            self.database.objects[(name, "INDEX")] = datetime.now(UTC)
            return
        if "CTX_DDL.CREATE_PREFERENCE" in upper:
            self.database.objects[("RAG_TEXT_WORLD_LEXER", "TEXT_PREFERENCE")] = None
        if "CTX_DDL.CREATE_STOPLIST" in upper:
            self.database.objects[("RAG_TEXT_STOPLIST", "TEXT_STOPLIST")] = None
        if "CTX_DDL.DROP_PREFERENCE" in upper:
            self.database.objects.pop(("RAG_TEXT_WORLD_LEXER", "TEXT_PREFERENCE"), None)
            return
        if "CTX_DDL.DROP_STOPLIST" in upper:
            self.database.objects.pop(("RAG_TEXT_STOPLIST", "TEXT_STOPLIST"), None)
            return
        if upper.startswith("DROP INDEX"):
            name = upper.split()[2]
            self.database.objects.pop((name, "INDEX"), None)
            return
        if upper.startswith("DROP TABLE"):
            name = upper.split()[2]
            self.database.objects.pop((name, "TABLE"), None)
            self.database.foreign_keys = {
                key: value
                for key, value in self.database.foreign_keys.items()
                if value[0].table_name != name
            }
            if name == "RAG_SCHEMA_MIGRATIONS":
                self.database.migrations.clear()
            return
        if upper.startswith("MERGE INTO RAG_SCHEMA_OPERATIONS"):
            if self.database.operation is None:
                self.database.operation = self.database.idle_operation()
            return
        if upper.startswith("MERGE INTO RAG_SCHEMA_MIGRATIONS"):
            self.database.migrations[str(params["migration_name"])] = str(params["checksum"])
            return
        if upper.startswith("UPDATE RAG_SCHEMA_OPERATIONS"):
            self._update_operation(upper, params)
            return

    def _add_foreign_key(self, upper: str) -> None:
        """`ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY` を Oracle と同じ規則で受ける。"""

        match = re.match(
            r"ALTER TABLE (\S+) ADD CONSTRAINT (\S+) FOREIGN KEY \(([^)]*)\) "
            r"REFERENCES (\S+) \(([^)]*)\)(?: ON DELETE (CASCADE|SET NULL))?( ENABLE NOVALIDATE)?$",
            upper,
        )
        assert match is not None, upper
        table, name, columns, referenced, referenced_columns, delete_rule, novalidate = (
            match.groups()
        )
        spec = ForeignKeySpec(
            name=name,
            table_name=table,
            columns=tuple(item.strip() for item in columns.split(",")),
            referenced_table_name=referenced,
            referenced_columns=tuple(item.strip() for item in referenced_columns.split(",")),
            delete_rule=delete_rule or "NO ACTION",
        )
        if any(fk.signature == spec.signature for fk, _ in self.database.foreign_keys.values()):
            raise RuntimeError("ORA-02275: such a referential constraint already exists")
        if name in self.database.foreign_keys:
            raise RuntimeError("ORA-02264: name already used by an existing constraint")
        if not novalidate and self.database.orphans.get(table, 0):
            raise RuntimeError("ORA-02298: cannot validate - parent keys not found")
        self.database.foreign_keys[name] = (spec, not novalidate)

    def _update_operation(self, upper: str, params: dict[str, Any]) -> None:
        operation = self.database.operation
        if operation is None:
            return
        now = datetime.now(UTC)
        owner = str(params.get("lease_owner") or "")
        if "SET STATUS = 'RUNNING'" in upper:
            if (
                operation["status"] == "RUNNING"
                and operation["lease_expires_at"] is not None
                and operation["lease_expires_at"] >= now
            ):
                return
            operation.update(
                {
                    "status": "RUNNING",
                    "operation_kind": params["operation_kind"],
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(seconds=int(params["lease_seconds"])),
                    "last_error_code": None,
                    "updated_at": now,
                }
            )
            self.database.claimed_operation_kinds.append(str(params["operation_kind"]))
            self.rowcount = 1
            return
        if operation.get("lease_owner") != owner:
            return
        if "SET STATUS = 'IDLE'" in upper:
            if "SCHEMA_EPOCH = SCHEMA_EPOCH + 1" in upper:
                operation["schema_epoch"] = int(operation["schema_epoch"]) + 1
            operation.update(
                {
                    "status": "IDLE",
                    "operation_kind": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                    "updated_at": now,
                }
            )
            self.rowcount = 1
            return
        if "SET STATUS = 'FAILED'" in upper:
            operation.update(
                {
                    "status": "FAILED",
                    "operation_kind": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": params["error_code"],
                    "updated_at": now,
                }
            )
            self.rowcount = 1
            return
        operation["lease_expires_at"] = now + timedelta(seconds=int(params["lease_seconds"]))
        operation["updated_at"] = now
        self.rowcount = 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None


class _FakeConnection:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.database)

    def commit(self) -> None:
        return None


class _FakeDatabase:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], datetime | None] = {}
        self.migrations: dict[str, str] = {}
        self.operation: dict[str, Any] | None = None
        self.active_jobs = 0
        self.fail_drop_or_create_with_lock = False
        # 制約名 → (定義, 既存の行を検査済みか)。CREATE TABLE と ALTER TABLE で増える。
        self.foreign_keys: dict[str, tuple[ForeignKeySpec, bool]] = {}
        # 子の表 → 参照先の無い行の件数。
        self.orphans: dict[str, int] = {}
        # 無効化されている FK の名前（#511）。
        self.disabled_foreign_keys: set[str] = set()
        # lease を取った操作の種類（OPERATION_KIND の CHECK 制約に合う値か）。
        self.claimed_operation_kinds: list[str] = []
        self.foreign_key_ddl: list[str] = []
        self.executed: list[str] = []
        # 表 → 正本にあるが欠けている列（取り消した migration が改名・削除した列など。#2450）。
        self.removed_columns: dict[str, set[str]] = {}

    @staticmethod
    def idle_operation() -> dict[str, Any]:
        return {
            "status": "IDLE",
            "operation_kind": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error_code": None,
            "schema_epoch": 0,
            "updated_at": datetime.now(UTC),
        }

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield _FakeConnection(self)


def test_schema_manifest_matches_canonical_ddl() -> None:
    assert managed_manifest_from_schema() == set(MANAGED_OBJECTS)
    assert len(MANAGED_TABLES) == len(set(MANAGED_TABLES))
    assert len(MANAGED_INDEXES) == len(set(MANAGED_INDEXES))


def test_schema_status_classifies_all_four_states() -> None:
    ready_objects = set(MANAGED_OBJECTS)
    checksums = {migration.name: migration.checksum for migration in MIGRATIONS}
    assert classify_system_schema_status(set(), {}) == "missing"
    assert (
        classify_system_schema_status(
            {("RAG_DOCUMENTS", "TABLE")},
            {},
        )
        == "partial"
    )
    assert (
        classify_system_schema_status(
            ready_objects,
            {**checksums, MIGRATIONS[-1].name: "changed"},
        )
        == "outdated"
    )
    assert classify_system_schema_status(ready_objects, checksums) == "ready"


def test_index_build_of_create_ddl_does_not_make_existing_environment_outdated() -> None:
    """作成の DDL の索引のビルドを変えても、既存の環境の状態は「更新が要る」にならない(#1990)。

    migration の checksum は版で固定した archive の SQL から作り、作成の DDL(既定の検索精度の
    推奨ビルド)は入らない。管理する索引は名前だけで判定する。
    """
    archive = json.loads(
        (
            Path(oracle_schema.__file__).parent / "schema_migrations/pre_profile_rename.json"
        ).read_text()
    )
    archived = {
        item["name"]: hashlib.sha256(item["sql"].encode("utf-8")).hexdigest()
        for item in archive["migrations"]
    }
    by_name = {migration.name: migration.checksum for migration in MIGRATIONS}
    assert {name: by_name[name] for name in archived} == archived
    create_sql = oracle_vector_index_create_sql()
    assert "NEIGHBORS 48" in create_sql
    assert all(create_sql not in migration.sql for migration in MIGRATIONS)
    # 以前の作成の DDL(32/500)で作った既存の環境の台帳と object は、そのまま ready。
    assert "RAG_CHUNKS_EMBEDDING_HNSW_IDX" in MANAGED_INDEXES
    assert classify_system_schema_status(set(MANAGED_OBJECTS), by_name) == "ready"


def test_initialize_is_idempotent_and_repairs_missing_index() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)

    initialized = manager.initialize()
    assert initialized["status"] == "ready"
    assert initialized["operation"] == "initialized"

    no_op = manager.initialize()
    assert no_op["operation"] == "no_op"

    database.objects.pop((MANAGED_INDEXES[-1], "INDEX"))
    repaired = manager.initialize()
    assert repaired["status"] == "ready"
    assert repaired["operation"] == "migrated"
    assert (MANAGED_INDEXES[-1], "INDEX") in database.objects


_RESTORE_CLASSIFICATION_MIGRATION = "20261019_001_restore_document_classification"


def test_status_detects_missing_columns_even_when_ledger_is_current() -> None:
    """台帳がすべて適用済みでも、正本の列が欠けた表があれば ready にしない（#2450）。

    取り消した migration（20261016_002）が分類の列を改名・削除した DB は、台帳だけを見ると最新に
    見える。列の欠落を状態に出し、更新で直せないときは列の名前を出して止める。
    """
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    assert manager.status()["missing_columns"] == []
    database.removed_columns = {"RAG_DOCUMENTS": {"CLASSIFICATION"}}

    status = manager.status()

    assert status["status"] == "outdated"
    assert status["pending_versions"] == []
    assert status["missing_columns"] == [
        {"table_name": "RAG_DOCUMENTS", "column_name": "CLASSIFICATION"}
    ]
    with pytest.raises(SystemSchemaError) as raised:
        manager.initialize()
    assert raised.value.code == "SCHEMA_POSTCONDITION_FAILED"
    assert "RAG_DOCUMENTS.CLASSIFICATION" in raised.value.public_message


def test_update_restores_columns_removed_by_retracted_migration() -> None:
    """取り消した migration を当てた DB は、追加の migration で分類の列が戻る（#2450）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.removed_columns = {
        "RAG_DOCUMENTS": {"CLASSIFICATION"},
    }
    database.migrations.pop(_RESTORE_CLASSIFICATION_MIGRATION)

    before = manager.status()
    assert before["status"] == "outdated"
    assert before["pending_versions"] == [_RESTORE_CLASSIFICATION_MIGRATION]
    # データを消す migration ではないので、承認は要らない。
    assert before["pending_destructive_migrations"] == []

    result = manager.initialize()

    assert result["status"] == "ready"
    assert result["missing_columns"] == []
    assert database.removed_columns == {"RAG_DOCUMENTS": set()}


def test_managed_columns_cover_every_managed_table() -> None:
    assert set(MANAGED_COLUMNS) == set(MANAGED_TABLES)
    assert "CLASSIFICATION" in MANAGED_COLUMNS["RAG_DOCUMENTS"]
    assert "RAG_QUERY_HISTORY" not in MANAGED_COLUMNS
    # 表制約の行は列に数えない。
    assert not any(
        column in {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}
        for columns in MANAGED_COLUMNS.values()
        for column in columns
    )


def test_initialize_replaces_only_explicit_retired_index() -> None:
    database = _FakeDatabase()
    retired_index = ("RAG_INGESTION_SEGMENTS_RECIPE_IDX", "INDEX")
    unmanaged_index = ("CUSTOM_RECIPE_STATUS_IDX", "INDEX")
    database.objects[retired_index] = datetime.now(UTC)
    database.objects[unmanaged_index] = datetime.now(UTC)

    result = SystemSchemaManager(database.connection).initialize()

    assert result["status"] == "ready"
    assert retired_index in RETIRED_MANAGED_OBJECTS
    assert retired_index not in database.objects
    assert unmanaged_index in database.objects
    assert ("RAG_INGESTION_SEGMENTS_RECIPE_STATUS_IDX", "INDEX") in database.objects


_RETIRED_STANDARD_ENGINE_TABLES = (
    "RAG_PROMPT_VERSIONS",
    "RAG_GENERATION_SETTINGS",
    "RAG_AGENT_MEMORIES",
)
_RETIRE_STANDARD_ENGINE_MIGRATION = "20260930_005_retire_standard_engine_objects"


def test_update_retires_standard_engine_tables_and_runs_migration() -> None:
    """旧 standard の回答エンジンの表が残る既存の DB は outdated になり、更新で消える（#596）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    assert not any((name, "TABLE") in database.objects for name in _RETIRED_STANDARD_ENGINE_TABLES)
    for name in _RETIRED_STANDARD_ENGINE_TABLES:
        database.objects[(name, "TABLE")] = datetime.now(UTC)
    database.migrations.pop(_RETIRE_STANDARD_ENGINE_MIGRATION)

    status = manager.status()
    assert status["status"] == "outdated"
    assert _RETIRE_STANDARD_ENGINE_MIGRATION in status["pending_versions"]
    assert {item["name"] for item in status["retired_objects"]} == set(
        _RETIRED_STANDARD_ENGINE_TABLES
    )

    # データを消す migration なので、承認（allow_destructive）が要る（#619）。
    result = manager.initialize(allow_destructive=True)

    assert result["status"] == "ready"
    assert _RETIRE_STANDARD_ENGINE_MIGRATION in database.migrations
    assert any("MENU.SETTINGS_GROUNDING" in statement for statement in database.executed)
    assert not any((name, "TABLE") in database.objects for name in _RETIRED_STANDARD_ENGINE_TABLES)
    # 回答の記録（answer_engine の列）と知識グラフの表は残す。
    assert ("RAG_ANSWER_RECORDS", "TABLE") in database.objects
    assert ("RAG_GRAPH_ENTITIES", "TABLE") in database.objects


_RETIRED_GRAPH_TABLES = ("RAG_GRAPH_CLAIMS", "RAG_GRAPH_COMMUNITY_SUMMARIES")
_RETIRE_GRAPH_CLAIMS_MIGRATION = "20260930_007_retire_graph_claims_community"


def test_update_retires_graph_claims_community_tables_with_approval() -> None:
    """関係情報の claims / community summary の表が残る DB は、承認付きの更新で消える（#621）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    assert not any((name, "TABLE") in database.objects for name in _RETIRED_GRAPH_TABLES)
    for name in _RETIRED_GRAPH_TABLES:
        database.objects[(name, "TABLE")] = datetime.now(UTC)
    database.migrations.pop(_RETIRE_GRAPH_CLAIMS_MIGRATION)

    status = manager.status()
    assert status["status"] == "outdated"
    assert [item["name"] for item in status["pending_destructive_migrations"]] == [
        _RETIRE_GRAPH_CLAIMS_MIGRATION
    ]
    assert {item["name"] for item in status["retired_objects"]} == set(_RETIRED_GRAPH_TABLES)
    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert all((name, "TABLE") in database.objects for name in _RETIRED_GRAPH_TABLES)

    result = manager.initialize(allow_destructive=True)

    assert result["status"] == "ready"
    assert _RETIRE_GRAPH_CLAIMS_MIGRATION in database.migrations
    assert not any((name, "TABLE") in database.objects for name in _RETIRED_GRAPH_TABLES)
    # 文書構造グラフが読む表は残す。
    for name in ("RAG_GRAPH_ENTITIES", "RAG_GRAPH_RELATIONSHIPS", "RAG_GRAPH_ENTITY_CHUNKS"):
        assert (name, "TABLE") in database.objects


# データを消す SQL（テーブルの DROP・行の DELETE・TRUNCATE）。`ON DELETE CASCADE` は含めない。
_DATA_LOSS_SQL = re.compile(r"\b(DROP\s+TABLE|DELETE\s+FROM|TRUNCATE\s+TABLE)\b", re.IGNORECASE)


def test_migrations_that_drop_or_delete_data_are_marked_destructive() -> None:
    """DROP TABLE・DELETE を含む migration は、すべて「破壊的」の印と説明を持つ（#619）。

    新しい migration でデータを消すときに印を付け忘れると、テストの fixture や「作成・更新」が
    確認なしに当ててしまうため、SQL から検出して照合する。
    """
    sections = oracle_schema_migration_sections()
    marked = {section.name for section in sections if section.destructive}
    dropping = {
        section.name
        for section in sections
        if any(_DATA_LOSS_SQL.search(statement) for statement in split_sql_statements(section.sql))
    }
    assert marked == dropping
    assert {
        "20260629_002_drop_kb_chunk_set_bindings",
        "20260928_001_retire_dashboard_permission",
        _RETIRE_STANDARD_ENGINE_MIGRATION,
        _RETIRE_GRAPH_CLAIMS_MIGRATION,
        "20261003_001_search_answer_profiles",
        "20261016_001_retire_evaluation_compare",
        "20261029_001_retire_query_history",
    } == marked
    assert all(migration.destructive_note for migration in MIGRATIONS if migration.destructive)
    # 印は checksum（SQL だけ）に影響しない（適用済みの DB を outdated にしない）。
    for section, migration in zip(sections, MIGRATIONS, strict=True):
        assert migration.destructive_note == section.destructive_note


def test_initialize_stops_before_destructive_migration_without_approval() -> None:
    """未適用の破壊的な migration は、承認が無ければ DB を変えずに止める（#619）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    for name in _RETIRED_STANDARD_ENGINE_TABLES:
        database.objects[(name, "TABLE")] = datetime.now(UTC)
    database.migrations.pop(_RETIRE_STANDARD_ENGINE_MIGRATION)
    claimed = list(database.claimed_operation_kinds)
    executed = len(database.executed)

    status = manager.status()
    assert [item["name"] for item in status["pending_destructive_migrations"]] == [
        _RETIRE_STANDARD_ENGINE_MIGRATION
    ]
    assert "rag_agent_memories" in status["pending_destructive_migrations"][0]["description"]

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()

    assert error.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert error.value.status_code == 409
    assert _RETIRE_STANDARD_ENGINE_MIGRATION in error.value.public_message
    # lease を取らず、DDL も DELETE も実行しない。表と台帳はそのまま。
    assert database.claimed_operation_kinds == claimed
    assert all(
        statement.lstrip().upper().startswith("SELECT")
        for statement in database.executed[executed:]
    )
    assert all((name, "TABLE") in database.objects for name in _RETIRED_STANDARD_ENGINE_TABLES)
    assert _RETIRE_STANDARD_ENGINE_MIGRATION not in database.migrations
    assert database.operation is not None
    assert database.operation["status"] == "IDLE"

    result = manager.initialize(allow_destructive=True)
    assert result["status"] == "ready"
    assert result["pending_destructive_migrations"] == []
    assert not any((name, "TABLE") in database.objects for name in _RETIRED_STANDARD_ENGINE_TABLES)


def test_non_destructive_pending_migration_is_applied_without_approval() -> None:
    """破壊的でない migration だけが未適用なら、今までどおり承認なしで当てる（#619）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    non_destructive = next(migration for migration in MIGRATIONS if not migration.destructive)
    database.migrations.pop(non_destructive.name)

    assert manager.status()["pending_destructive_migrations"] == []
    result = manager.initialize()

    assert result["operation"] == "migrated"
    assert database.migrations[non_destructive.name] == non_destructive.checksum


def test_fresh_database_reports_no_destructive_migrations() -> None:
    """未初期化の DB は migration を実行せずに記録するので、承認を求めない（#619）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)

    status = manager.status()
    assert status["status"] == "missing"
    assert status["pending_destructive_migrations"] == []
    assert manager.initialize()["operation"] == "initialized"
    # 適用済み（005 まで記録済み）の DB は何も変わらない。
    no_op = manager.initialize()
    assert no_op["operation"] == "no_op"
    assert no_op["pending_destructive_migrations"] == []


def test_retired_tables_without_ledger_are_not_dropped_without_approval() -> None:
    """台帳に記録の無い schema の廃止した表は、承認が無ければ消さない（missing でも。#1648）。

    既存の ADB の schema に、旧版の単独の製品・別のアプリケーションの同じ名前の表があるとき。
    """
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    database.objects[("RAG_PROMPT_VERSIONS", "TABLE")] = datetime.now(UTC)
    claimed = list(database.claimed_operation_kinds)

    status = manager.status()
    assert status["status"] == "missing"
    approvals = status["pending_destructive_migrations"]
    assert [item["name"] for item in approvals] == [RETIRED_TABLES_APPROVAL]
    assert "RAG_PROMPT_VERSIONS" in approvals[0]["description"]
    assert "専用の schema" in approvals[0]["description"]

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert ("RAG_PROMPT_VERSIONS", "TABLE") in database.objects
    assert database.claimed_operation_kinds == claimed
    assert not any(statement.upper().startswith("DROP") for statement in database.executed)

    result = manager.initialize(allow_destructive=True)
    assert result["status"] == "ready"
    assert ("RAG_PROMPT_VERSIONS", "TABLE") not in database.objects
    assert result["pending_destructive_migrations"] == []


def test_retired_tables_with_ledger_are_dropped_as_rag_owned() -> None:
    """台帳に記録があれば RAG が初期化した schema で、廃止した表は承認なしに消す（#1648）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.objects[("RAG_PROMPT_VERSIONS", "TABLE")] = datetime.now(UTC)

    status = manager.status()
    assert status["status"] == "outdated"
    assert status["pending_destructive_migrations"] == []

    result = manager.initialize()
    assert result["status"] == "ready"
    assert ("RAG_PROMPT_VERSIONS", "TABLE") not in database.objects


def test_recreate_drops_retired_standard_engine_tables() -> None:
    """全再作成は migration を記録だけするので、退役した表は退役一覧から消す（#596）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    for name in _RETIRED_STANDARD_ENGINE_TABLES:
        database.objects[(name, "TABLE")] = datetime.now(UTC)

    result = manager.initialize(recreate=True, confirmation=RECREATE_CONFIRMATION)

    assert result["status"] == "ready"
    assert not any((name, "TABLE") in database.objects for name in _RETIRED_STANDARD_ENGINE_TABLES)


def test_checksum_mismatch_reapplies_only_pending_migration() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.migrations[MIGRATIONS[0].name] = "mismatch"

    result = manager.initialize()

    assert result["status"] == "ready"
    assert database.migrations[MIGRATIONS[0].name] == MIGRATIONS[0].checksum


def test_recreate_requires_exact_confirmation_and_preserves_unmanaged_objects() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.objects[("CUSTOM_APPLICATION_TABLE", "TABLE")] = datetime.now(UTC)

    with pytest.raises(SystemSchemaError) as missing_confirmation:
        manager.initialize(recreate=True, confirmation="wrong")
    assert missing_confirmation.value.status_code == 422

    result = manager.initialize(
        recreate=True,
        confirmation=RECREATE_CONFIRMATION,
    )

    assert result["operation"] == "recreated"
    assert result["status"] == "ready"
    assert ("CUSTOM_APPLICATION_TABLE", "TABLE") in database.objects
    assert all((name, "TABLE") in database.objects for name in DOMAIN_TABLES)


def test_recreate_rejects_active_jobs_and_concurrent_lease() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.active_jobs = 1
    with pytest.raises(SystemSchemaActiveJobsError):
        manager.initialize(
            recreate=True,
            confirmation=RECREATE_CONFIRMATION,
        )

    database.active_jobs = 0
    assert database.operation is not None
    database.operation.update(
        {
            "status": "RUNNING",
            "lease_owner": "other",
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
    )
    with pytest.raises(SystemSchemaBusyError):
        manager.initialize()


def test_ora_00054_is_retryable_and_records_failure() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection, ddl_lock_timeout_seconds=1)
    manager.initialize()
    database.fail_drop_or_create_with_lock = True

    with pytest.raises(SystemSchemaError) as locked:
        manager.initialize(
            recreate=True,
            confirmation=RECREATE_CONFIRMATION,
        )

    assert locked.value.code == "ORA-00054"
    assert locked.value.status_code == 409
    assert database.operation is not None
    assert database.operation["status"] == "FAILED"


# ---- 外部キーの差分（#505） ---------------------------------------------------

# 開発 DB で足りなかった、rag_documents への ON DELETE CASCADE の FK（#485 の調査）。
_DOCUMENT_CASCADE_FOREIGN_KEYS = {
    "RAG_CHUNK_SETS_DOCUMENT_FK": "RAG_CHUNK_SETS",
    "RAG_DOCUMENT_RECIPES_DOCUMENT_FK": "RAG_DOCUMENT_RECIPES",
    "RAG_DOC_EXT_DOCUMENT_FK": "RAG_DOCUMENT_EXTRACTIONS",
    "RAG_DOCUMENT_KNOWLEDGE_BASES_DOC_FK": "RAG_DOCUMENT_KNOWLEDGE_BASES",
    "RAG_ARTIFACT_LAYERS_DOCUMENT_FK": "RAG_ARTIFACT_LAYERS",
}


def _drop_foreign_keys(database: _FakeDatabase, *names: str) -> None:
    """古い版で作った表（FK が無い）を再現する。"""

    for name in names:
        database.foreign_keys.pop(name)


def test_manifest_foreign_keys_come_from_canonical_create_table() -> None:
    by_name = {foreign_key.name: foreign_key for foreign_key in MANAGED_FOREIGN_KEYS}

    assert len(by_name) == len(MANAGED_FOREIGN_KEYS)
    for name, table_name in _DOCUMENT_CASCADE_FOREIGN_KEYS.items():
        foreign_key = by_name[name]
        assert foreign_key.table_name == table_name
        assert foreign_key.columns == ("DOCUMENT_ID",)
        assert foreign_key.referenced_table_name == "RAG_DOCUMENTS"
        assert foreign_key.referenced_columns == ("DOCUMENT_ID",)
        assert foreign_key.delete_rule == "CASCADE"
    # 削除規則の無い FK は NO ACTION として比べる。
    assert by_name["RAG_DOCUMENTS_DUPLICATE_FK"].delete_rule == "NO ACTION"
    # 子の表は RAG の管理対象。参照先は RAG の表か共通認証の PLATFORM_ROLES。
    assert {foreign_key.table_name for foreign_key in MANAGED_FOREIGN_KEYS} <= set(MANAGED_TABLES)
    assert {foreign_key.referenced_table_name for foreign_key in MANAGED_FOREIGN_KEYS} <= {
        *MANAGED_TABLES,
        "PLATFORM_ROLES",
    }


def test_fresh_initialize_creates_tables_with_all_foreign_keys() -> None:
    database = _FakeDatabase()

    result = SystemSchemaManager(database.connection).initialize()

    assert result["status"] == "ready"
    assert result["missing_foreign_keys"] == []
    assert result["orphaned_foreign_keys"] == []
    # 表の作成で FK ごと作られるので、既存の表に足す DDL は出ない。
    assert database.foreign_key_ddl == []
    assert set(database.foreign_keys) >= {fk.name for fk in MANAGED_FOREIGN_KEYS}


def test_status_reports_missing_foreign_keys_and_initialize_adds_them() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    _drop_foreign_keys(database, *_DOCUMENT_CASCADE_FOREIGN_KEYS)

    status = manager.status()

    assert status["status"] == "outdated"
    assert status["pending_versions"] == []
    assert status["missing_objects"] == []
    assert {item["name"] for item in status["missing_foreign_keys"]} == set(
        _DOCUMENT_CASCADE_FOREIGN_KEYS
    )
    chunk_sets = next(
        item
        for item in status["missing_foreign_keys"]
        if item["name"] == "RAG_CHUNK_SETS_DOCUMENT_FK"
    )
    assert chunk_sets == {
        "name": "RAG_CHUNK_SETS_DOCUMENT_FK",
        "table_name": "RAG_CHUNK_SETS",
        "columns": ["DOCUMENT_ID"],
        "referenced_table_name": "RAG_DOCUMENTS",
        "referenced_columns": ["DOCUMENT_ID"],
        "delete_rule": "CASCADE",
        "orphan_rows": 0,
    }
    assert manager.is_ready() is False

    result = manager.initialize()

    assert result["operation"] == "migrated"
    assert result["status"] == "ready"
    assert result["missing_foreign_keys"] == []
    assert result["orphaned_foreign_keys"] == []
    assert (
        "ALTER TABLE RAG_CHUNK_SETS ADD CONSTRAINT RAG_CHUNK_SETS_DOCUMENT_FK "
        "FOREIGN KEY (DOCUMENT_ID) REFERENCES RAG_DOCUMENTS (DOCUMENT_ID) ON DELETE CASCADE"
    ) in database.foreign_key_ddl
    assert all("NOVALIDATE" not in statement for statement in database.foreign_key_ddl)
    assert all(database.foreign_keys[name][1] for name in _DOCUMENT_CASCADE_FOREIGN_KEYS)
    assert manager.initialize()["operation"] == "no_op"


def test_orphan_rows_add_foreign_key_novalidate_and_are_reported_without_deleting() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    _drop_foreign_keys(database, "RAG_CHUNK_SETS_DOCUMENT_FK", "RAG_DOC_EXT_DOCUMENT_FK")
    database.orphans["RAG_CHUNK_SETS"] = 240

    before = manager.status()
    orphan_counts = {item["name"]: item["orphan_rows"] for item in before["missing_foreign_keys"]}
    assert orphan_counts == {"RAG_CHUNK_SETS_DOCUMENT_FK": 240, "RAG_DOC_EXT_DOCUMENT_FK": 0}

    result = manager.initialize()

    assert result["status"] == "ready"
    assert result["missing_foreign_keys"] == []
    # 孤立した行がある FK は、既存の行を検査しない NOVALIDATE で足し、件数を警告として出す。
    assert [(item["name"], item["orphan_rows"]) for item in result["orphaned_foreign_keys"]] == [
        ("RAG_CHUNK_SETS_DOCUMENT_FK", 240)
    ]
    assert database.foreign_keys["RAG_CHUNK_SETS_DOCUMENT_FK"][1] is False
    assert database.foreign_keys["RAG_DOC_EXT_DOCUMENT_FK"][1] is True
    assert any(
        statement.startswith("ALTER TABLE RAG_CHUNK_SETS ")
        and statement.endswith(" ENABLE NOVALIDATE")
        for statement in database.foreign_key_ddl
    )
    # 既存の行は利用者の操作なしに消さない。
    assert not any(statement.startswith(("DELETE", "TRUNCATE")) for statement in database.executed)

    # 利用者が孤立した行を片付けたら警告は消える（状態は ready のまま）。
    database.orphans["RAG_CHUNK_SETS"] = 0
    cleaned = manager.status()
    assert cleaned["status"] == "ready"
    assert cleaned["orphaned_foreign_keys"] == []


def test_existing_foreign_key_with_another_name_is_not_added_again() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    # 旧版の migration が別名で作った、同じ定義の FK。
    spec, validated = database.foreign_keys.pop("RAG_DOC_EXT_DOCUMENT_FK")
    renamed = ForeignKeySpec(
        name="RAG_DOCUMENT_EXTRACTIONS_DOCUMENT_FK",
        table_name=spec.table_name,
        columns=spec.columns,
        referenced_table_name=spec.referenced_table_name,
        referenced_columns=spec.referenced_columns,
        delete_rule=spec.delete_rule,
    )
    database.foreign_keys[renamed.name] = (renamed, validated)

    assert manager.status()["status"] == "ready"
    assert manager.initialize()["operation"] == "no_op"
    assert database.foreign_key_ddl == []


def test_missing_table_is_reported_as_partial_not_as_missing_foreign_key() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.objects.pop(("RAG_CHUNK_SETS", "TABLE"))
    database.foreign_keys = {
        key: value
        for key, value in database.foreign_keys.items()
        if value[0].table_name != "RAG_CHUNK_SETS"
    }

    status = manager.status()

    assert status["status"] == "partial"
    assert status["missing_foreign_keys"] == []


def _replace_delete_rule(database: _FakeDatabase, name: str, delete_rule: str) -> None:
    spec, validated = database.foreign_keys[name]
    database.foreign_keys[name] = (
        ForeignKeySpec(
            name=spec.name,
            table_name=spec.table_name,
            columns=spec.columns,
            referenced_table_name=spec.referenced_table_name,
            referenced_columns=spec.referenced_columns,
            delete_rule=delete_rule,
        ),
        validated,
    )


def test_delete_rule_mismatch_and_disabled_foreign_keys_are_outdated_and_repaired() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    # 旧版で CASCADE なしで作られた FK と、無効化された FK（#511）。
    _replace_delete_rule(database, "RAG_CHUNK_SETS_DOCUMENT_FK", "NO ACTION")
    database.disabled_foreign_keys.add("RAG_DOC_EXT_DOCUMENT_FK")
    database.foreign_keys["RAG_DOC_EXT_DOCUMENT_FK"] = (
        database.foreign_keys["RAG_DOC_EXT_DOCUMENT_FK"][0],
        False,
    )
    database.orphans["RAG_DOCUMENT_EXTRACTIONS"] = 7

    status = manager.status()

    assert status["status"] == "outdated"
    assert status["missing_foreign_keys"] == []
    assert [
        (item["name"], item["delete_rule"], item["current_delete_rule"])
        for item in status["mismatched_foreign_keys"]
    ] == [("RAG_CHUNK_SETS_DOCUMENT_FK", "CASCADE", "NO ACTION")]
    assert [(item["name"], item["orphan_rows"]) for item in status["disabled_foreign_keys"]] == [
        ("RAG_DOC_EXT_DOCUMENT_FK", 7)
    ]
    assert manager.is_ready() is False

    result = manager.initialize()

    assert result["operation"] == "migrated"
    assert result["status"] == "ready"
    assert result["mismatched_foreign_keys"] == []
    assert result["disabled_foreign_keys"] == []
    assert database.foreign_keys["RAG_CHUNK_SETS_DOCUMENT_FK"][0].delete_rule == "CASCADE"
    assert database.foreign_keys["RAG_CHUNK_SETS_DOCUMENT_FK"][1] is True
    # 参照先のない行がある無効化された FK は、既存の行を検査せずに有効にし、警告に出す。
    assert "RAG_DOC_EXT_DOCUMENT_FK" not in database.disabled_foreign_keys
    assert [(item["name"], item["orphan_rows"]) for item in result["orphaned_foreign_keys"]] == [
        ("RAG_DOC_EXT_DOCUMENT_FK", 7)
    ]
    assert database.foreign_key_ddl == [
        "ALTER TABLE RAG_CHUNK_SETS DROP CONSTRAINT RAG_CHUNK_SETS_DOCUMENT_FK",
        "ALTER TABLE RAG_CHUNK_SETS ADD CONSTRAINT RAG_CHUNK_SETS_DOCUMENT_FK "
        "FOREIGN KEY (DOCUMENT_ID) REFERENCES RAG_DOCUMENTS (DOCUMENT_ID) ON DELETE CASCADE",
        "ALTER TABLE RAG_DOCUMENT_EXTRACTIONS MODIFY CONSTRAINT RAG_DOC_EXT_DOCUMENT_FK "
        "ENABLE NOVALIDATE",
    ]
    # 既存の行は利用者の操作なしに消さない。
    assert not any(statement.startswith(("DELETE", "TRUNCATE")) for statement in database.executed)
    assert manager.initialize()["operation"] == "no_op"


def test_delete_orphaned_rows_after_confirmation_validates_foreign_key() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    _drop_foreign_keys(database, "RAG_CHUNK_SETS_DOCUMENT_FK")
    database.orphans["RAG_CHUNK_SETS"] = 240
    manager.initialize()
    assert [item["name"] for item in manager.status()["orphaned_foreign_keys"]] == [
        "RAG_CHUNK_SETS_DOCUMENT_FK"
    ]
    epoch = manager.status()["operation_state"]["schema_epoch"]

    # 確認した件数より多いときは削除しない（lease は返し、失敗として記録しない）。
    with pytest.raises(SystemSchemaError) as error:
        manager.delete_orphaned_rows(
            constraint_name="RAG_CHUNK_SETS_DOCUMENT_FK", expected_orphan_rows=10
        )
    assert (error.value.code, error.value.status_code) == ("SCHEMA_ORPHAN_ROWS_CHANGED", 409)
    assert database.orphans["RAG_CHUNK_SETS"] == 240
    assert manager.status()["operation_state"]["status"] == "idle"

    result = manager.delete_orphaned_rows(
        constraint_name="RAG_CHUNK_SETS_DOCUMENT_FK", expected_orphan_rows=240
    )

    assert result["operation"] == "orphans_deleted"
    assert result["deleted_row_count"] == 240
    assert result["foreign_key"]["table_name"] == "RAG_CHUNK_SETS"
    assert result["orphaned_foreign_keys"] == []
    assert result["status"] == "ready"
    assert result["operation_state"]["schema_epoch"] == epoch + 1
    assert database.foreign_keys["RAG_CHUNK_SETS_DOCUMENT_FK"][1] is True
    assert database.foreign_key_ddl[-1] == (
        "ALTER TABLE RAG_CHUNK_SETS MODIFY CONSTRAINT RAG_CHUNK_SETS_DOCUMENT_FK VALIDATE"
    )
    # lease の種類は作成・更新と同じ（RAG_SCHEMA_OPERATIONS の CHECK 制約に合わせる）。
    assert database.claimed_operation_kinds[-1] == "INITIALIZE"


def test_delete_orphaned_rows_rejects_unknown_and_busy() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()

    with pytest.raises(SystemSchemaError) as error:
        manager.delete_orphaned_rows(constraint_name="RAG_UNKNOWN_FK", expected_orphan_rows=0)
    assert (error.value.code, error.value.status_code) == ("SCHEMA_FOREIGN_KEY_NOT_FOUND", 404)
    assert manager.status()["operation_state"]["status"] == "idle"

    assert database.operation is not None
    database.operation.update(
        status="RUNNING",
        lease_owner="another",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(SystemSchemaBusyError):
        manager.delete_orphaned_rows(
            constraint_name="RAG_CHUNK_SETS_DOCUMENT_FK", expected_orphan_rows=0
        )


def test_status_lists_every_managed_object_with_the_summary_counts() -> None:
    """詳細の一覧（objects）は全管理 object で、概要の object の件数と一致する（#658）。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()

    status = manager.status()
    objects = status["objects"]

    assert [(item["name"], item["object_type"]) for item in objects] == list(MANAGED_OBJECTS)
    assert len(objects) == status["expected_object_count"]
    assert sum(item["exists"] for item in objects) == status["existing_object_count"]
    # テーブル以外（索引・Oracle Text の設定）は行数・統計日時を持たない。
    assert all(
        item["estimated_rows"] is None and item["last_analyzed_at"] is None
        for item in objects
        if item["object_type"] != "TABLE"
    )
    assert {item["object_type"] for item in objects} >= {"TABLE", "INDEX", "TEXT_PREFERENCE"}


def test_language_release_fresh_schema_and_incremental_upgrade_share_complete_contract() -> None:
    """新規作成と非破壊の更新の両方で、公開版・成果物の必須列を満たす。"""
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    fresh = manager.initialize()
    assert fresh["status"] == "ready"
    assert ("RAG_LANGUAGE_PACK_RELEASES", "TABLE") in database.objects
    assert ("RAG_LANGUAGE_PACK_STATE", "TABLE") in database.objects
    assert "LANGUAGE_PACK_RELEASE_ID" in MANAGED_COLUMNS["RAG_INGESTION_JOBS"]
    assert {
        "LANGUAGE_PACK_RELEASE_ID",
        "LANGUAGE_PACK_INGEST_HASH",
        "LANGUAGE_PACK_JOB_ID",
        "LANGUAGE_PACK_BUILT_AT",
    } <= set(MANAGED_COLUMNS["RAG_CHUNK_SETS"])
    assert manager.initialize()["operation"] == "no_op"

    for name in ("20261028_001_language_pack_releases", "20261028_002_language_pack_artifacts"):
        database.migrations.pop(name)
    database.removed_columns["RAG_INGESTION_JOBS"] = {"LANGUAGE_PACK_RELEASE_ID"}
    database.removed_columns["RAG_CHUNK_SETS"] = {
        "LANGUAGE_PACK_RELEASE_ID",
        "LANGUAGE_PACK_INGEST_HASH",
        "LANGUAGE_PACK_JOB_ID",
        "LANGUAGE_PACK_BUILT_AT",
    }
    before = manager.status()
    assert before["status"] == "outdated"
    assert before["pending_destructive_migrations"] == []
    start = len(database.executed)
    upgraded = manager.initialize()
    assert upgraded["status"] == "ready"
    assert upgraded["missing_columns"] == []
    assert not any(statement.startswith("DROP") for statement in database.executed[start:])


def test_retired_query_history_requires_explicit_migration_and_keeps_chat_records() -> None:
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    name = "20261029_001_retire_query_history"
    database.migrations.pop(name)
    database.objects[("RAG_QUERY_HISTORY", "TABLE")] = datetime.now(UTC)
    database.objects[("RAG_QUERY_HISTORY_PROFILE_IDX", "INDEX")] = datetime.now(UTC)
    before = manager.status()
    assert [item["name"] for item in before["pending_destructive_migrations"]] == [name]
    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert ("RAG_QUERY_HISTORY", "TABLE") in database.objects
    manager.initialize(allow_destructive=True)
    assert ("RAG_QUERY_HISTORY", "TABLE") not in database.objects
    assert ("RAG_QUERY_HISTORY_PROFILE_IDX", "INDEX") not in database.objects
    for table in ("RAG_CONVERSATIONS", "RAG_MESSAGES", "RAG_ANSWER_RECORDS", "RAG_RUN_HISTORY"):
        assert (table, "TABLE") in database.objects
    assert manager.initialize()["status"] == "ready"
