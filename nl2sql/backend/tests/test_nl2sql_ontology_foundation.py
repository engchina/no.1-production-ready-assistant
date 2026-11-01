from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.features.nl2sql.models import (
    AllowedObjects,
    Nl2SqlEngine,
    Nl2SqlProfile,
    QueryResults,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
    SchemaViewDependency,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError, OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import Settings, get_settings


class _RowsCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed = ""

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed = " ".join(sql.split())

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class _ExplainCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.current_rows: list[tuple[Any, ...]] = []
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    def __enter__(self) -> _ExplainCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        self.current_rows = self.rows if "FROM plan_table" in normalized else []

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.current_rows)


class _ExplainConnection:
    def __init__(self, cursor: _ExplainCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> _ExplainCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def test_oracle_constraint_metadata_preserves_composite_fk_order() -> None:
    cursor = _RowsCursor(
        [
            (
                "ORDER_LINES",
                "FK_ORDER_LINES_ORDER",
                "R",
                "TENANT_ID, ORDER_ID",
                "SALES",
                "SALES",
                "ORDERS",
                "TENANT_ID, ORDER_ID",
                "CASCADE",
                "ENABLED",
                "NOT DEFERRABLE",
            )
        ]
    )
    table = SchemaTable(
        table_name="ORDER_LINES",
        logical_name="注文明細",
        owner="SALES",
    )
    adapter = OracleNl2SqlAdapter(get_settings())

    adapter._load_constraints(cursor, {"SALES.ORDER_LINES": table})

    detail = table.constraint_details[0]
    assert detail.columns == ["TENANT_ID", "ORDER_ID"]
    assert detail.referenced_table == "ORDERS"
    assert detail.referenced_columns == ["TENANT_ID", "ORDER_ID"]
    assert detail.delete_rule == "CASCADE"
    assert "FROM all_constraints uc" in cursor.executed
    assert "ruc.owner = uc.r_owner" in cursor.executed
    assert "rucc.owner = ruc.owner" in cursor.executed
    assert "rucc.position = ucc.position" in cursor.executed


def test_schema_fingerprint_changes_with_relationship_or_lineage() -> None:
    adapter = OracleNl2SqlAdapter(get_settings())
    base = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="ORDERS",
                logical_name="注文",
                owner="SALES",
                columns=[
                    SchemaColumn(
                        column_name="ORDER_ID",
                        logical_name="注文ID",
                        data_type="NUMBER",
                    )
                ],
            )
        ],
    )
    changed = base.model_copy(
        deep=True,
        update={
            "view_dependencies": [
                SchemaViewDependency(
                    owner="SALES",
                    view_name="ORDER_SUMMARY",
                    referenced_owner="SALES",
                    referenced_name="ORDERS",
                )
            ]
        },
    )

    assert adapter._schema_fingerprint(base) == adapter._schema_fingerprint(base)
    assert adapter._schema_fingerprint(base) != adapter._schema_fingerprint(changed)


def test_request_scope_is_intersection_of_profile_view() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="sales",
            name="営業",
            allowed_tables=["ORDERS", "CUSTOMERS"],
        )
    )

    resolved = service._resolve_allowed_objects(
        "sales",
        AllowedObjects(
            table_names=["ORDERS", "SECRETS"],
            columns={"ORDERS": ["ORDER_ID"], "SECRETS": ["TOKEN"]},
        ),
    )

    assert resolved.table_names == ["APP.ORDERS"]
    assert resolved.columns == {"APP.ORDERS": ["ORDER_ID"]}


def test_unknown_profile_never_falls_back_to_default() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    with pytest.raises(ValueError, match="業務プロファイル"):
        service.get_profile("does-not-exist")


def test_oracle_explain_plan_uses_safe_literal_and_summarizes_full_scan(
    monkeypatch: MonkeyPatch,
) -> None:
    cursor = _ExplainCursor(
        [
            ("SELECT STATEMENT", None, None, None, 42, 1200, 4096),
            ("TABLE ACCESS", "FULL", "SALES", "ORDERS", 40, 1200, 4096),
        ]
    )
    connection = _ExplainConnection(cursor)

    @contextmanager
    def fake_connection() -> Iterator[_ExplainConnection]:
        yield connection

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)

    result = adapter.explain_select("SELECT ORDER_ID FROM SALES.ORDERS;")

    assert result.available is True
    assert result.total_cost == 42
    assert result.estimated_cardinality == 1200
    assert result.full_table_scans == ["ORDERS"]
    explain_sql, explain_params = next(
        item for item in cursor.executed if item[0].startswith("EXPLAIN PLAN")
    )
    assert "SET STATEMENT_ID = 'NL2SQL_" in explain_sql
    assert explain_sql.endswith("SELECT ORDER_ID FROM SALES.ORDERS")
    assert explain_params is None
    assert connection.committed is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ORDER_ID INTO ORDER_ARCHIVE FROM ORDERS",
        "SELECT ORDER_ID FROM ORDERS FOR UPDATE",
    ],
)
def test_sql_ast_blocks_select_side_effects(sql: str) -> None:
    analysis = parse_oracle_sql(sql)

    assert analysis.graph is None
    assert analysis.validation.is_valid is False
    assert analysis.validation.findings[0].code == "SQL_QUERY_HAS_SIDE_EFFECT"


def test_query_session_history_keeps_trace_and_is_idempotent() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    trace = {
        "ontology_revision_id": "revision-1",
        "intent_version": 2,
        "sql_hash": "sql-hash",
        "validation_hash": "validation-hash",
    }

    first = service.record_ontology_history(
        session_id="session-1",
        question="受注件数を表示",
        rewritten_question="受注の件数を表示",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        generated_sql="SELECT COUNT(*) FROM ORDERS",
        executable_sql="SELECT COUNT(*) FROM ORDERS FETCH FIRST 100 ROWS ONLY",
        profile_id="default",
        result=QueryResults(columns=["COUNT"], rows=[{"COUNT": 3}], total=1),
        ontology_trace_summary=trace,
        elapsed_ms=12,
    )
    second = service.record_ontology_history(
        session_id="session-1",
        question="差し替えを許可しない",
        rewritten_question="差し替えを許可しない",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        generated_sql="SELECT 1 FROM DUAL",
        executable_sql="SELECT 1 FROM DUAL",
        profile_id="default",
        result=QueryResults(columns=["X"], rows=[], total=0),
        ontology_trace_summary={},
    )

    assert second.id == first.id
    history = service.list_history().items
    assert len(history) == 1
    assert history[0].session_id == "session-1"
    assert history[0].ontology_trace_summary == trace
    assert history[0].result_columns == ["COUNT"]


def test_connection_wraps_connect_failure_as_oracle_adapter_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """oracledb.connect() の生の失敗を OracleAdapterError へ統一変換する。

    接続確立の失敗が素通りすると呼び出し側の
    `except OracleAdapterError` で捕まらず 500 になる回帰の防止。
    """

    class _FailingOracledb:
        def connect(self, **_kwargs: Any) -> Any:
            raise RuntimeError("ORA-12541: TNS:no listener")

    # CI には .env / Wallet が無いため、接続情報は明示して wallet 不要モードで組み立てる。
    adapter = OracleNl2SqlAdapter(
        Settings(
            oracle_user="APP",
            oracle_password="password",
            oracle_dsn="localhost/FREEPDB1",
            oracle_connection_security="walletless_tls",
        )
    )
    monkeypatch.setattr(adapter, "_load_oracledb", lambda: _FailingOracledb())
    monkeypatch.setattr(adapter, "_init_client", lambda _oracledb: None)
    monkeypatch.setattr(adapter, "is_configured", lambda: True)

    with pytest.raises(OracleAdapterError, match="Oracle 接続に失敗しました"), adapter.connection():
        pass
