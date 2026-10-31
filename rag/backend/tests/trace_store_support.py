"""共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）のテストの補助。

実 DB は使わない。DB-API の形の fake（platform の `test_trace_store.py` の FakeDb と同じ形）に、
本物の書き手（`TraceSpanWriter`。thread は始めず、読む前に `flush` する）が書き、読み出し
（`fetch_trace_spans` と実行の id での引き当て）は書いた行から返す。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pr_backend_core.observability.trace_store import TraceSpanWriter

from app.rag import trace_store
from app.rag.span_display import span_display_name

_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "product",
    "operation_id",
    "attempt",
    "is_root",
    "owner_user_uuid",
    "name",
    "span_kind",
    "status",
    "started_at",
    "finished_at",
    "display_kind",
    "display_params_json",
    "attributes_json",
)


class FakeTraceCursor:
    def __init__(self, db: FakeTraceDb) -> None:
        self.db = db
        self._result: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def setinputsizes(self, **_sizes: Any) -> None:
        pass

    def executemany(self, _sql: str, rows: list[dict[str, Any]]) -> None:
        if self.db.fail_writes:
            raise RuntimeError("ORA-03113")
        for row in rows:
            self.db.rows[(row["trace_id"], row["span_id"])] = dict(row)

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        self.db.queries.append((sql, dict(binds)))
        rows = list(self.db.rows.values())
        viewer = binds.get("viewer")
        if viewer is not None:
            rows = [row for row in rows if row["owner_user_uuid"] == viewer]
        if sql.startswith("SELECT TRACE_ID FROM"):
            matched = [
                row
                for row in rows
                if row["product"] == binds["product"]
                and row["operation_id"] == binds["operation_id"]
                and row["is_root"]
            ]
            self._result = [(row["trace_id"],) for row in matched[:1]]
            return
        rows = sorted(
            (row for row in rows if row["trace_id"] == binds["trace_id"]),
            key=lambda row: (row["started_at"], row["span_id"]),
        )
        self._result = [tuple(row[column] for column in _COLUMNS) for row in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class FakeTraceConnection:
    def __init__(self, db: FakeTraceDb) -> None:
        self.db = db

    def cursor(self) -> FakeTraceCursor:
        return FakeTraceCursor(self.db)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeTraceDb:
    """書いた span の行（主キー → bind）と、読み出しの SQL。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.fail_writes = False
        # 工程の名前は本番の書き手（`start_trace_span_writer`）と同じ関数で書く（#3272）。
        self.writer = TraceSpanWriter(
            self.connect,
            retries=1,
            retry_backoff_seconds=0.0,
            display_name=span_display_name,
        )

    @contextmanager
    def connect(self) -> Iterator[FakeTraceConnection]:
        yield FakeTraceConnection(self)

    @contextmanager
    def read(self) -> Iterator[FakeTraceConnection]:
        """読み出しの接続（読む前に待っている span を書く）。"""
        self.writer.flush()
        yield FakeTraceConnection(self)

    def flush(self) -> None:
        self.writer.flush()

    def spans(self) -> list[dict[str, Any]]:
        self.writer.flush()
        return sorted(self.rows.values(), key=lambda row: (row["started_at"], row["span_id"]))


def install_trace_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTraceDb]:
    """このテストの span の表（書き手と読み出しの接続を fake にする。fixture `trace_db`）。"""
    db = FakeTraceDb()
    monkeypatch.setattr(trace_store, "_trace_connection", db.read)
    previous = trace_store.set_trace_span_writer(db.writer)
    try:
        yield db
    finally:
        trace_store.set_trace_span_writer(previous)
