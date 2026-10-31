from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from app.features.nl2sql.ontology_store import (
    ONTOLOGY_COLLECTIONS,
    ONTOLOGY_DDL_STATEMENTS,
    ONTOLOGY_TABLE_DDL,
    InMemoryOntologyStore,
    OntologyVersionConflict,
    OracleOntologyStore,
    canonical_json,
    next_versioned_document,
    schema_fingerprint,
    stable_ontology_id,
    stable_physical_id,
)
from app.settings import get_settings


@dataclass(frozen=True)
class _JsonFixture:
    name: str
    created_at: datetime


class _SchemaCursor:
    def __init__(self, database: _SchemaDatabase) -> None:
        self.database = database
        self.input_sizes: dict[str, Any] = {}

    def __enter__(self) -> _SchemaCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _binds: dict[str, Any] | None = None) -> None:
        self._assert_input_sizes_match(sql)
        normalized = " ".join(sql.split())
        self.database.executed.append(normalized)
        if self.database.raise_row_lock_timeout and " FOR UPDATE WAIT " in normalized:
            raise RuntimeError("ORA-30006: resource busy; acquire with WAIT timeout expired")

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        self._assert_input_sizes_match(sql)
        self.database.executed_many.append((" ".join(sql.split()), rows))

    def setinputsizes(self, **input_sizes: Any) -> None:
        self.input_sizes = input_sizes

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def _assert_input_sizes_match(self, sql: str) -> None:
        for bind_name in self.input_sizes:
            if f":{bind_name}" not in sql:
                raise RuntimeError(f"unrecognized bind variable {bind_name}")


class _SchemaConnection:
    def __init__(self, database: _SchemaDatabase) -> None:
        self.database = database
        self.call_timeout = 0
        self.database.connections.append(self)

    def __enter__(self) -> _SchemaConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _SchemaCursor:
        return _SchemaCursor(self.database)

    def commit(self) -> None:
        self.database.commits += 1

    def rollback(self) -> None:
        self.database.rollbacks += 1


class _SchemaDatabase:
    def __init__(self) -> None:
        self.factory_calls = 0
        self.connections: list[_SchemaConnection] = []
        self.executed: list[str] = []
        self.executed_many: list[tuple[str, list[dict[str, Any]]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.raise_row_lock_timeout = False

    @contextmanager
    def connection(self) -> Iterator[_SchemaConnection]:
        self.factory_calls += 1
        yield _SchemaConnection(self)


def test_canonical_json_is_unicode_safe_and_deterministic() -> None:
    first = {
        "fixture": _JsonFixture("顧客", datetime(2026, 7, 11, 1, 2, tzinfo=UTC)),
        "tags": {"請求", "顧客"},
    }
    second = {
        "tags": {"顧客", "請求"},
        "fixture": _JsonFixture("顧客", datetime(2026, 7, 11, 1, 2, tzinfo=UTC)),
    }

    assert canonical_json(first) == canonical_json(second)
    assert "顧客" in canonical_json(first)
    assert "\\u9867" not in canonical_json(first)


def test_stable_ids_normalize_oracle_identifiers_without_cross_kind_collisions() -> None:
    table_id = stable_physical_id("table", " app ", "orders")
    same_table_id = stable_physical_id("table", "APP", "ORDERS")
    column_id = stable_physical_id("column", "APP", "ORDERS", "ORDER_ID")

    assert table_id == same_table_id
    assert table_id.startswith("physical_")
    assert column_id != table_id
    assert stable_ontology_id("Business Entity", "顧客") == stable_ontology_id(
        "Business Entity", "顧客"
    )


def test_schema_fingerprint_ignores_catalog_row_order_but_keeps_composite_key_order() -> None:
    first: dict[str, Any] = {
        "tables": [
            {
                "owner": "APP",
                "table_name": "ORDERS",
                "columns": [
                    {"column_name": "TENANT_ID", "data_type": "NUMBER"},
                    {"column_name": "ORDER_ID", "data_type": "NUMBER"},
                ],
            },
            {"owner": "APP", "table_name": "CUSTOMERS", "columns": []},
        ],
        "foreign_keys": [
            {
                "name": "FK_ORDER_CUSTOMER",
                "source_columns": ["TENANT_ID", "CUSTOMER_ID"],
                "target_columns": ["TENANT_ID", "CUSTOMER_ID"],
            }
        ],
    }
    reordered = {
        "foreign_keys": list(reversed(first["foreign_keys"])),
        "tables": [
            first["tables"][1],
            {
                **first["tables"][0],
                "columns": list(reversed(first["tables"][0]["columns"])),
            },
        ],
    }
    changed_composite_order = {
        **first,
        "foreign_keys": [
            {
                **first["foreign_keys"][0],
                "source_columns": ["CUSTOMER_ID", "TENANT_ID"],
            }
        ],
    }

    assert schema_fingerprint(first) == schema_fingerprint(reordered)
    assert schema_fingerprint(first) != schema_fingerprint(changed_composite_order)


def test_next_versioned_document_requires_matching_etag_for_updates() -> None:
    created = next_versioned_document(
        {"revision_id": "rev-1", "status": "draft"},
        current=None,
        expected_etag=None,
    )
    updated = next_versioned_document(
        {"revision_id": "rev-1", "status": "published"},
        current=created,
        expected_etag=created["etag"],
    )

    assert created["version"] == 1
    assert updated["version"] == 2
    assert updated["etag"] != created["etag"]

    with pytest.raises(OntologyVersionConflict) as missing_match:
        next_versioned_document(
            {"revision_id": "rev-1", "status": "archived"},
            current=updated,
            expected_etag=None,
        )
    assert missing_match.value.current_etag == updated["etag"]
    assert missing_match.value.current_version == 2

    with pytest.raises(OntologyVersionConflict):
        next_versioned_document(
            {"revision_id": "rev-1", "status": "archived"},
            current=updated,
            expected_etag=created["etag"],
        )


def test_memory_store_round_trips_graph_and_returns_detached_values() -> None:
    store = InMemoryOntologyStore()
    revision = store.save_revision(
        {
            "revision_id": "rev-1",
            "status": "draft",
            "schema_fingerprint": "f" * 64,
        }
    )
    node = store.save_node(
        {
            "revision_id": "rev-1",
            "node_id": "node-1",
            "node_type": "business_entity",
            "review_status": "approved",
            "physical_id": "physical-1",
            "embedding": [0.25, 0.5],
            "business_name": "顧客",
        }
    )
    store.save_edge(
        {
            "revision_id": "rev-1",
            "edge_id": "edge-1",
            "source_node_id": "node-1",
            "target_node_id": "node-2",
            "review_status": "approved",
        }
    )

    restored = store.get_node("rev-1", "node-1")
    assert restored == node
    assert store.list_nodes("rev-1") == [node]
    assert store.list_edges("rev-1")[0]["edge_id"] == "edge-1"
    assert store.get_revision("rev-1") == revision

    assert restored is not None
    restored["business_name"] = "改変"
    restored["embedding"].append(1.0)
    unchanged = store.get_node("rev-1", "node-1")
    assert unchanged is not None
    assert unchanged["business_name"] == "顧客"
    assert unchanged["embedding"] == [0.25, 0.5]


def test_oracle_object_node_lookup_filters_indexes_without_selecting_embedding() -> None:
    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)

    documents = store.list_documents(
        "nodes",
        {
            "revision_id": "revision-published",
            "node_type": "column",
            "physical_id": stable_physical_id("table", "APP", "ORDERS"),
        },
        include_embedding=False,
    )

    assert documents == []
    sql = database.executed[-1]
    assert "SELECT PAYLOAD_JSON, VERSION_NO, ETAG FROM" in sql
    assert "EMBEDDING" not in sql
    assert "REVISION_ID = :filter_revision_id" in sql
    assert "NODE_TYPE = :filter_node_type" in sql
    assert "PHYSICAL_ID = :filter_physical_id" in sql


def test_oracle_store_applies_round_trip_timeout_to_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_oracle_call_timeout_seconds", 7.5)
    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)

    store.ensure_schema()
    store.get_document("revisions", {"revision_id": "missing"})
    store.list_documents("revisions")
    store.save_document(
        "revisions",
        {
            "revision_id": "rev-1",
            "status": "draft",
            "schema_fingerprint": "a" * 64,
        },
    )
    store.save_documents_atomic(
        "edges",
        [
            (
                {
                    "revision_id": "rev-1",
                    "edge_id": "edge-1",
                    "source_node_id": "node-1",
                    "target_node_id": "node-2",
                    "review_status": "approved",
                },
                None,
            )
        ],
    )

    assert database.connections
    assert {connection.call_timeout for connection in database.connections} == {7500}
    assert any("FOR UPDATE WAIT 7" in sql for sql in database.executed)


def test_oracle_store_row_lock_wait_is_bounded_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_oracle_call_timeout_seconds", 120.0)
    database = _SchemaDatabase()
    database.raise_row_lock_timeout = True
    store = OracleOntologyStore(connection_factory=database.connection)

    with pytest.raises(TimeoutError, match="行ロック待ちが 30 秒を超えました"):
        store.save_document(
            "revisions",
            {
                "revision_id": "rev-locked",
                "status": "draft",
                "schema_fingerprint": "b" * 64,
            },
        )

    assert any("FOR UPDATE WAIT 30" in sql for sql in database.executed)


def test_memory_atomic_save_rolls_back_entire_revision_switch_on_conflict() -> None:
    store = InMemoryOntologyStore()
    first = store.save_revision(
        {"revision_id": "rev-1", "status": "published", "schema_fingerprint": "a" * 64}
    )
    second = store.save_revision(
        {"revision_id": "rev-2", "status": "draft", "schema_fingerprint": "b" * 64}
    )

    with pytest.raises(OntologyVersionConflict):
        store.save_documents_atomic(
            "revisions",
            [
                (
                    {
                        "revision_id": "rev-1",
                        "status": "archived",
                        "schema_fingerprint": "a" * 64,
                    },
                    first["etag"],
                ),
                (
                    {
                        "revision_id": "rev-2",
                        "status": "published",
                        "schema_fingerprint": "b" * 64,
                    },
                    "stale-etag",
                ),
            ],
        )

    assert store.get_revision("rev-1") == first
    assert store.get_revision("rev-2") == second


def test_oracle_atomic_create_uses_one_array_bound_insert() -> None:
    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)

    created = store.save_documents_atomic(
        "edges",
        [
            (
                {
                    "revision_id": "rev-1",
                    "edge_id": "edge-1",
                    "source_node_id": "node-1",
                    "target_node_id": "node-2",
                    "review_status": "approved",
                },
                None,
            ),
            (
                {
                    "revision_id": "rev-1",
                    "edge_id": "edge-2",
                    "source_node_id": "node-2",
                    "target_node_id": "node-3",
                    "review_status": "approved",
                },
                None,
            ),
        ],
    )

    assert [item["version"] for item in created] == [1, 1]
    assert database.factory_calls == 1
    assert database.commits == 1
    assert database.rollbacks == 0
    assert database.executed == []
    assert len(database.executed_many) == 1
    sql, rows = database.executed_many[0]
    assert "INSERT INTO NL2SQL_ONTOLOGY_EDGES" in sql
    assert [row["edge_id"] for row in rows] == ["edge-1", "edge-2"]


def test_oracle_atomic_create_array_binds_node_vectors() -> None:
    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)

    created = store.save_documents_atomic(
        "nodes",
        [
            (
                {
                    "revision_id": "rev-1",
                    "node_id": "node-1",
                    "node_type": "table",
                    "review_status": "approved",
                    "physical_id": "physical-1",
                    "embedding": [0.1, 0.2],
                },
                None,
            ),
            (
                {
                    "revision_id": "rev-1",
                    "node_id": "node-2",
                    "node_type": "table",
                    "review_status": "approved",
                    "physical_id": "physical-2",
                    "embedding": [0.3, 0.4],
                },
                None,
            ),
        ],
    )

    assert [item["version"] for item in created] == [1, 1]
    assert database.commits == 1
    assert database.rollbacks == 0
    assert database.executed == []
    assert len(database.executed_many) == 1
    sql, rows = database.executed_many[0]
    assert "INSERT INTO NL2SQL_ONTOLOGY_NODES" in sql
    assert [row["embedding"].typecode for row in rows] == ["f", "f"]


def test_memory_store_covers_query_trace_and_governance_collections() -> None:
    store = InMemoryOntologyStore()
    profile_view = store.save_profile_view(
        {"profile_id": "sales", "revision_id": "rev-1", "selected_node_ids": ["node-1"]}
    )
    session = store.save_query_session(
        {
            "session_id": "session-1",
            "ontology_revision_id": "rev-1",
            "profile_id": "sales",
            "status": "awaiting_intent_confirmation",
            "intent_version": 1,
            "sql_version": 0,
        }
    )
    artifact = store.save_artifact(
        {
            "artifact_id": "artifact-1",
            "session_id": "session-1",
            "artifact_type": "question_intent",
            "content_hash": "a" * 64,
        }
    )
    proposal = store.save_proposal(
        {
            "proposal_id": "proposal-1",
            "session_id": "session-1",
            "ontology_revision_id": "rev-1",
            "status": "draft",
        }
    )

    assert store.get_profile_view("sales") == profile_view
    assert store.get_query_session("session-1") == session
    assert store.list_artifacts("session-1") == [artifact]
    assert store.list_proposals("session-1") == [proposal]

    updated = store.save_query_session(
        {**session, "status": "awaiting_sql_confirmation", "sql_version": 1},
        expected_etag=session["etag"],
    )
    assert updated["version"] == 2
    assert updated["status"] == "awaiting_sql_confirmation"


def test_memory_store_rejects_partial_identity_and_unknown_filters() -> None:
    store = InMemoryOntologyStore()

    with pytest.raises(ValueError, match="identity must contain exactly"):
        store.get_document("nodes", {"node_id": "node-1"})
    with pytest.raises(ValueError, match="Unsupported nodes filters"):
        store.list_documents("nodes", {"business_name": "顧客"})


def test_oracle_ddl_covers_all_documents_and_vector_dimension() -> None:
    assert tuple(ONTOLOGY_TABLE_DDL) == ONTOLOGY_COLLECTIONS
    assert len(ONTOLOGY_DDL_STATEMENTS) > len(ONTOLOGY_COLLECTIONS)
    assert "VECTOR(1536, FLOAT32)" in ONTOLOGY_TABLE_DDL["nodes"]
    assert all("PAYLOAD_JSON CLOB" in ddl for ddl in ONTOLOGY_TABLE_DDL.values())
    assert all("VERSION_NO" in ddl and "ETAG" in ddl for ddl in ONTOLOGY_TABLE_DDL.values())


def test_oracle_store_does_not_run_ddl_until_ensure_schema_is_called() -> None:
    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)

    assert database.factory_calls == 0
    store.ensure_schema()
    store.ensure_schema()

    assert database.factory_calls == 1
    assert database.commits == 0
    assert database.rollbacks == 0
    assert database.executed == ["SELECT 1 FROM NL2SQL_ONTOLOGY_REVISIONS WHERE 1 = 0"]


def test_oracle_bulk_json_uses_string_binds_and_preserves_large_japanese_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oracledb

    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)
    bindings: list[Any] = []
    original = _SchemaCursor.executemany

    def capture(cursor: _SchemaCursor, sql: str, rows: list[dict[str, Any]]) -> None:
        bindings.append(cursor.input_sizes["payload_json"])
        original(cursor, sql, rows)

    monkeypatch.setattr(_SchemaCursor, "executemany", capture)
    documents = [
        ({"artifact_id": f"artifact-{i}", "content": "日" * (12000 if i == 256 else 20)}, None)
        for i in range(513)
    ]
    stored = store.save_documents_atomic("artifacts", documents)
    assert [len(rows) for _sql, rows in database.executed_many] == [256, 256, 1]
    assert isinstance(bindings[0], int)
    assert bindings[1] == oracledb.DB_TYPE_CLOB
    assert isinstance(bindings[2], int)
    assert [item["content"] for item in stored] == [doc["content"] for doc, _ in documents]
    assert database.commits == 1
    assert database.rollbacks == 0


def test_oracle_bulk_json_rolls_back_all_batches_when_later_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _SchemaDatabase()
    store = OracleOntologyStore(connection_factory=database.connection)
    original = _SchemaCursor.executemany

    def fail_second(cursor: _SchemaCursor, sql: str, rows: list[dict[str, Any]]) -> None:
        if database.executed_many:
            raise RuntimeError("second batch failed")
        original(cursor, sql, rows)

    monkeypatch.setattr(_SchemaCursor, "executemany", fail_second)
    with pytest.raises(RuntimeError, match="second batch failed"):
        store.save_documents_atomic(
            "artifacts", [({"artifact_id": f"artifact-{i}"}, None) for i in range(257)]
        )
    assert database.commits == 0
    assert database.rollbacks == 1
