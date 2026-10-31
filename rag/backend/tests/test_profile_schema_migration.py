"""旧 schema の ledger・作成時刻・FK を保持し、中断後も再開する。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.rag.search_answer_profile_migration import (
    COLUMN_RENAMES,
    CONSTRAINT_RENAMES,
    INDEX_RENAMES,
    RENAME_MIGRATION,
    TABLE_RENAMES,
    historical_sections,
)
from app.rag.system_schema import MIGRATIONS, SystemSchemaError, SystemSchemaManager
from tests.test_system_schema_manager import _FakeDatabase


def old_database() -> _FakeDatabase:
    db = _FakeDatabase()
    SystemSchemaManager(db.connection).initialize()
    for mapping, kind in [(TABLE_RENAMES, "TABLE"), (INDEX_RENAMES, "INDEX")]:
        for old, new in mapping.items():
            if (new, kind) in db.objects:
                db.objects[(old, kind)] = db.objects.pop((new, kind))
    inverse_tables = {v: k for k, v in TABLE_RENAMES.items()}
    inverse_constraints = {v: k for k, v in CONSTRAINT_RENAMES.items()}
    db.foreign_keys = {
        inverse_constraints.get(name, name): (
            replace(
                fk,
                name=inverse_constraints.get(name, name),
                table_name=inverse_tables.get(fk.table_name, fk.table_name),
                referenced_table_name=inverse_tables.get(
                    fk.referenced_table_name, fk.referenced_table_name
                ),
                columns=tuple(
                    "BUSINESS_VIEW_ID" if c == "SEARCH_ANSWER_PROFILE_ID" else c for c in fk.columns
                ),
                referenced_columns=tuple(
                    "BUSINESS_VIEW_ID" if c == "SEARCH_ANSWER_PROFILE_ID" else c
                    for c in fk.referenced_columns
                ),
            ),
            valid,
        )
        for name, (fk, valid) in db.foreign_keys.items()
    }
    db.migrations.pop(RENAME_MIGRATION)
    db.executed.clear()
    return db


def test_update_keeps_ledger_creation_times_and_foreign_keys() -> None:
    db = old_database()
    ledger = dict(db.migrations)
    created = db.objects[("RAG_BUSINESS_VIEWS", "TABLE")]
    manager = SystemSchemaManager(db.connection)
    assert manager.initialize(allow_destructive=True)["status"] == "ready"
    assert db.objects[("RAG_SEARCH_ANSWER_PROFILES", "TABLE")] == created
    assert all(db.migrations[key] == value for key, value in ledger.items())
    assert manager.initialize()["operation"] == "no_op"
    assert not any("DROP TABLE RAG_BUSINESS" in sql for sql in db.executed)
    assert not any("BUSINESS_VIEW_ID" in fk.columns for fk, _ in db.foreign_keys.values())


def test_partial_table_rename_resumes_without_recreating_old_tables() -> None:
    db = old_database()
    db.objects[("RAG_SEARCH_ANSWER_PROFILES", "TABLE")] = db.objects.pop(
        ("RAG_BUSINESS_VIEWS", "TABLE")
    )
    for key, (fk, valid) in list(db.foreign_keys.items()):
        if fk.referenced_table_name == "RAG_BUSINESS_VIEWS":
            db.foreign_keys[key] = (
                replace(fk, referenced_table_name="RAG_SEARCH_ANSWER_PROFILES"),
                valid,
            )
    assert (
        SystemSchemaManager(db.connection).initialize(allow_destructive=True)["status"] == "ready"
    )
    assert not any("CREATE TABLE RAG_BUSINESS_VIEWS" in sql for sql in db.executed)


def test_existing_new_table_collision_stops_before_old_data_is_dropped() -> None:
    db = old_database()
    db.objects[("RAG_SEARCH_ANSWER_PROFILES", "TABLE")] = datetime.now(UTC)
    with pytest.raises(SystemSchemaError) as error:
        SystemSchemaManager(db.connection).initialize(allow_destructive=True)
    assert error.value.code == "ORA-20060"
    assert ("RAG_BUSINESS_VIEWS", "TABLE") in db.objects
    assert RENAME_MIGRATION not in db.migrations


def test_historical_sql_checksums_stay_immutable_and_current_names_are_complete() -> None:
    import hashlib

    archived = historical_sections()["migrations"]
    assert len(archived) == 54
    # archive の後に足した migration（#860 の改名・#1175 など）は除いて照合する。
    for section, migration in zip(archived, MIGRATIONS[: len(archived)], strict=True):
        assert migration.name == section["name"]
        assert migration.checksum == hashlib.sha256(section["sql"].encode()).hexdigest()
    assert set(COLUMN_RENAMES) >= {
        "RAG_CONVERSATIONS",
        "RAG_ANSWER_RECORDS",
        "RAG_ROLE_SEARCH_ANSWER_PROFILES",
    }
