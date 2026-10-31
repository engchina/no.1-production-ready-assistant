"""埋め込みの次元の正本と、NL2SQL の各層の一致（#46）。"""

from __future__ import annotations

from typing import Any

import pytest
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS, EMBEDDING_VECTOR_COLUMN_TYPE
from pr_system_settings.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    SystemSchemaError,
)
from pydantic import ValidationError

from app.features.nl2sql.embedding_client import EMBED_DIMENSIONS
from app.features.nl2sql.ontology_store import ONTOLOGY_TABLE_DDL
from app.features.settings.system_schema import (
    MIGRATIONS,
    SystemSchemaManager,
    pending_destructive_migrations,
    split_migration_sql,
)
from app.settings import Settings

_MIGRATIONS = {migration.version: migration for migration in MIGRATIONS}


def test_embedding_client_and_settings_use_platform_constant() -> None:
    assert EMBED_DIMENSIONS == EMBEDDING_DIMENSIONS
    assert Settings().oci_genai_embedding_dim == EMBEDDING_DIMENSIONS
    # #46 より前の既定（1536）が `.env` に残っていたら、起動で止める（列と合わない）。
    with pytest.raises(ValidationError):
        Settings(oci_genai_embedding_dim=1536)


def test_ontology_ddl_uses_platform_vector_type() -> None:
    """SQL のファイルは platform を import できないため、同じ値を書き、一致をここで確かめる。"""
    base = _MIGRATIONS[1].path.read_text(encoding="utf-8")
    assert f"EMBEDDING {EMBEDDING_VECTOR_COLUMN_TYPE}," in base
    assert f"EMBEDDING {EMBEDDING_VECTOR_COLUMN_TYPE}," in ONTOLOGY_TABLE_DDL["nodes"]


def test_embedding_dimensions_migration_rebuilds_only_mismatched_columns() -> None:
    migration = _MIGRATIONS[25]
    sql = migration.path.read_text(encoding="utf-8")

    assert migration.destructive
    assert "NL2SQL_ONTOLOGY_NODES" in migration.destructive_note
    assert f"NOT LIKE 'VECTOR({EMBEDDING_DIMENSIONS},FLOAT32%'" in sql
    assert "'DROP INDEX IX_NL2SQL_ONT_NODE_EMBED'" in sql
    assert "'ALTER TABLE NL2SQL_ONTOLOGY_NODES DROP COLUMN EMBEDDING'" in sql
    assert f"ADD (EMBEDDING {EMBEDDING_VECTOR_COLUMN_TYPE})" in sql
    assert "'CREATE VECTOR INDEX IX_NL2SQL_ONT_NODE_EMBED '" in sql
    assert "DROP TABLE NL2SQL_FEEDBACK_VECTORS" in sql
    # 1 つの PL/SQL の block。
    assert len(split_migration_sql(sql)) == 1


def test_pending_destructive_migrations_skip_uninitialized_schema() -> None:
    assert pending_destructive_migrations("partial", [24, 25]) == [
        {
            "name": "025_embedding_dimensions_1024",
            "description": _MIGRATIONS[25].destructive_note,
        }
    ]
    assert pending_destructive_migrations("missing", [24, 25]) == []
    assert pending_destructive_migrations("ready", []) == []


class _PendingDestructiveManager(SystemSchemaManager):
    def __init__(self) -> None:
        super().__init__(connection_factory=lambda: None, ddl_lock_timeout_seconds=1)  # type: ignore[arg-type,return-value]
        self.touched = False

    def status(self) -> dict[str, Any]:
        return {"pending_destructive_migrations": pending_destructive_migrations("partial", [25])}

    def _ensure_control_schema(self) -> None:
        self.touched = True
        raise AssertionError("承認の前に DB に触った")


def test_initialize_requires_approval_before_touching_database() -> None:
    manager = _PendingDestructiveManager()

    with pytest.raises(SystemSchemaError) as caught:
        manager.initialize()

    assert caught.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert caught.value.status_code == 409
    assert not manager.touched
    # 承認すると先へ進む（ここでは制御テーブルの用意に入ったことだけを確かめる）。
    with pytest.raises(AssertionError):
        manager.initialize(allow_destructive=True)
    assert manager.touched


def test_cli_initialize_passes_approval_and_explains_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from app.cli import nl2sql_system_schema

    calls: list[bool] = []

    class _Manager:
        def initialize(
            self, *, recreate: bool, confirmation: str | None, allow_destructive: bool
        ) -> dict[str, Any]:
            calls.append(allow_destructive)
            if not allow_destructive:
                raise SystemSchemaError(
                    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED, "承認が要る", status_code=409
                )
            return {"operation_state": {"schema_epoch": 1}}

    monkeypatch.setattr(nl2sql_system_schema, "system_schema_manager", _Manager())
    monkeypatch.setattr(nl2sql_system_schema, "reset_system_schema_runtime", lambda **_: None)

    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", "--initialize"])
    assert nl2sql_system_schema.main() == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert "--allow-destructive" in error["hint"]

    monkeypatch.setattr("sys.argv", ["nl2sql_system_schema", "--initialize", "--allow-destructive"])
    assert nl2sql_system_schema.main() == 0
    assert calls == [False, True]
