"""テストの共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）。

実 DB は使わない。DB-API の形の fake の接続（platform の `test_trace_store.py` の FakeDb と
同じ形）で、書き手の `MERGE` を行の upsert、読み出しの `SELECT` を trace id と持ち主の
絞り込みとして扱う。書き手の thread は動かさず、テストが `flush` を呼んでから読む。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from app.features.nl2sql.trace_spans import (
    TraceSpanStore,
    install_trace_span_store,
    reset_trace_span_store,
)

# `fetch_trace_spans` の列の順（`_SELECT_COLUMNS`）。
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
# MERGE の WHEN MATCHED で変わる列（開始時刻・持ち主・名前は最初の書き込みのまま）。
_UPDATED = (
    "parent_span_id",
    "status",
    "finished_at",
    "display_kind",
    "display_params_json",
    "attributes_json",
)


def _aware(value: object) -> object:
    return value.replace(tzinfo=UTC) if isinstance(value, datetime) else value


class FakeTraceSpanDb:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes = 0
        self.selects: list[dict[str, Any]] = []
        self.fail_reads = False

    @contextmanager
    def connect(self) -> Iterator[_FakeConnection]:
        yield _FakeConnection(self)

    def spans(self, **match: object) -> list[dict[str, Any]]:
        """書かれた行（列の値で絞る）。"""
        return [
            row
            for row in self.rows.values()
            if all(row.get(key) == value for key, value in match.items())
        ]


class _FakeConnection:
    def __init__(self, db: FakeTraceSpanDb) -> None:
        self.db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)

    def commit(self) -> None:
        return None


class _FakeCursor:
    def __init__(self, db: FakeTraceSpanDb) -> None:
        self.db = db
        self.rowcount = 0
        self._result: list[tuple[Any, ...]] = []

    def setinputsizes(self, **_sizes: Any) -> None:
        pass

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        assert sql.lstrip().startswith("MERGE INTO PLATFORM_TRACE_SPANS")
        self.db.writes += 1
        for row in rows:
            key = (row["trace_id"], row["span_id"])
            current = self.db.rows.get(key)
            if current is None:
                self.db.rows[key] = dict(row)
            else:
                current.update({column: row[column] for column in _UPDATED})

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        assert "FROM PLATFORM_TRACE_SPANS" in sql
        if self.db.fail_reads:
            raise RuntimeError("ORA-03113")
        self.db.selects.append(dict(binds))
        rows = [
            row
            for row in self.db.rows.values()
            if row["trace_id"] == binds["trace_id"]
            and ("viewer" not in binds or row["owner_user_uuid"] == binds["viewer"])
        ]
        rows.sort(key=lambda row: (row["started_at"], row["span_id"]))
        self._result = [
            tuple(_aware(row[column]) for column in _COLUMNS) for row in rows[: binds["limit"]]
        ]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


@dataclass
class TraceTable:
    store: TraceSpanStore
    db: FakeTraceSpanDb

    def flush(self) -> None:
        self.store.writer.flush()


@pytest.fixture
def trace_table() -> Iterator[TraceTable]:
    """fake の span の表を入れる（書き手の thread は動かさない）。"""
    db = FakeTraceSpanDb()
    store = install_trace_span_store(db.connect, start=False)
    try:
        yield TraceTable(store=store, db=db)
    finally:
        reset_trace_span_store()
