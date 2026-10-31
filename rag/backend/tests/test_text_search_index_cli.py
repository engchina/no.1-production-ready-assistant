"""既存の search_text を正規化する CLI（#1336）。実 DB は使わず、cursor の振る舞いを確かめる。"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag.text_search_index_cli import normalize_search_texts


class _Lob:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self._rows: list[tuple[str, object]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        self._connection.selects.append((sql, dict(binds)))
        table = sql.split(" FROM ", 1)[1].split()[0]
        rows = sorted(self._connection.tables[table].items())
        if "last_key" in binds:
            rows = [row for row in rows if row[0] > binds["last_key"]]
        self._rows = [(key, _Lob(value)) for key, value in rows[: binds["batch_size"]]]

    def fetchall(self) -> list[tuple[str, object]]:
        return self._rows

    def executemany(self, sql: str, rows: list[dict[str, str]]) -> None:
        table = sql.split("UPDATE ", 1)[1].split()[0]
        for row in rows:
            self._connection.tables[table][row["row_key"]] = row["search_text"]
        self._connection.updates.append((table, len(rows)))


class _FakeConnection:
    def __init__(self, tables: dict[str, dict[str, str]]) -> None:
        self.tables = tables
        self.selects: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[tuple[str, int]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def _connection() -> _FakeConnection:
    return _FakeConnection(
        {
            "rag_chunks": {
                "c1": "手順①で申請",
                "c2": "変更の無い本文",
                "c3": "第Ⅴ章",
            },
            "rag_feedback_details": {"f1": "㈱サンプル"},
        }
    )


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
def test_normalize_updates_only_changed_rows_in_batches(dry_run: bool) -> None:
    connection = _connection()

    results = normalize_search_texts(connection, dry_run=dry_run, batch_size=2)

    assert [(result.table, result.scanned, result.changed) for result in results] == [
        ("rag_chunks", 3, 2),
        ("rag_feedback_details", 1, 1),
    ]
    # Oracle は空文字を NULL として扱うため、最初の回は last_key の条件を付けない。
    first_sql, first_binds = connection.selects[0]
    assert "WHERE" not in first_sql
    assert "last_key" not in first_binds
    assert connection.selects[1][1]["last_key"] == "c2"
    if dry_run:
        assert connection.updates == []
        assert connection.tables["rag_chunks"]["c1"] == "手順①で申請"
    else:
        assert connection.tables["rag_chunks"] == {
            "c1": "手順1で申請",
            "c2": "変更の無い本文",
            "c3": "第V章",
        }
        assert connection.tables["rag_feedback_details"] == {"f1": "(株)サンプル"}
        # 変わった行のある回ごとに commit する（rag_chunks で 2 回、フィードバックで 1 回）。
        assert connection.commits == 3


def test_normalize_is_idempotent() -> None:
    connection = _connection()
    normalize_search_texts(connection, dry_run=False)

    again = normalize_search_texts(connection, dry_run=False)

    assert [result.changed for result in again] == [0, 0]
