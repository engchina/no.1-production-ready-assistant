"""削除する旧テーブルの書き出し CLI（#596）の決定論テスト。"""

from __future__ import annotations

import array
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.rag import legacy_export


class _FakeLob:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self) -> str:
        return self.value


class _FakeCursor:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self.description: list[tuple[str]] = []
        self._rows: list[tuple[Any, ...]] = []
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: dict[str, Any] | None = None) -> None:
        self.database.executed.append(statement)
        if statement.startswith("SELECT COUNT(*) FROM user_tables"):
            assert parameters is not None
            exists = parameters["table_name"] in self.database.tables
            self._one = (1 if exists else 0,)
            return
        self.description = [(name,) for name in self.database.columns]
        self._rows = list(self.database.rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch


class _FakeDatabase:
    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.columns: list[str] = []
        self.rows: list[tuple[Any, ...]] = []
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    @contextmanager
    def connection(self) -> Iterator[_FakeDatabase]:
        yield self


def _memory_database() -> _FakeDatabase:
    database = _FakeDatabase()
    database.tables.add("RAG_AGENT_MEMORIES")
    database.columns = [
        "MEMORY_ID",
        "USER_ID_HASH",
        "MEMORY_TEXT",
        "METADATA_JSON",
        "EMBEDDING",
        "USEFULNESS_SCORE",
        "EVAL_COUNT",
        "CREATED_AT",
    ]
    created_at = datetime(2026, 9, 1, 9, 30, tzinfo=UTC)
    database.rows = [
        (
            f"mem-{index:03d}",
            "a" * 64,
            _FakeLob(f"記憶 {index}"),
            {"source": "chat", "tags": ["a", "b"]},
            array.array("f", [0.5, -0.25]),
            Decimal("0.75"),
            Decimal("3"),
            created_at,
        )
        for index in range(250)
    ]
    return database


def test_export_writes_all_rows_as_json_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _memory_database()
    output = tmp_path / "export" / "rag_agent_memories.jsonl"

    exit_code = legacy_export.main(
        ["--table", "rag_agent_memories", "--output", str(output)],
        connection_factory=database.connection,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "table": "rag_agent_memories",
        "status": "exported",
        "row_count": 250,
        "output": str(output),
    }
    lines = output.read_text(encoding="utf-8").splitlines()
    # fetchmany の 1 回分（200 行）を超えても、全行を書き出す。
    assert len(lines) == 250
    assert json.loads(lines[0]) == {
        "memory_id": "mem-000",
        "user_id_hash": "a" * 64,
        "memory_text": "記憶 0",
        "metadata_json": {"source": "chat", "tags": ["a", "b"]},
        "embedding": [0.5, -0.25],
        "usefulness_score": 0.75,
        "eval_count": 3,
        "created_at": "2026-09-01T09:30:00+00:00",
    }
    assert "記憶 0" in lines[0]  # 日本語は escape しない
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not output.with_name(f"{output.name}.partial").exists()
    assert "SELECT * FROM rag_agent_memories ORDER BY memory_id" in database.executed


def test_export_does_nothing_when_table_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _FakeDatabase()
    output = tmp_path / "rag_agent_memories.jsonl"

    exit_code = legacy_export.main(
        ["--table", "rag_agent_memories", "--output", str(output)],
        connection_factory=database.connection,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "table": "rag_agent_memories",
        "status": "table_missing",
        "row_count": 0,
    }
    assert not output.exists()
    assert len(database.executed) == 1


def test_export_rejects_tables_outside_allowlist(tmp_path: Path) -> None:
    database = _FakeDatabase()

    with pytest.raises(SystemExit) as exited:
        legacy_export.main(
            ["--table", "rag_documents", "--output", str(tmp_path / "out.jsonl")],
            connection_factory=database.connection,
        )

    assert exited.value.code == 2
    assert database.executed == []


def test_export_removes_partial_file_on_failure(tmp_path: Path) -> None:
    database = _memory_database()
    database.rows.append(("broken",))  # 列数が合わない行で書き出しを失敗させる
    output = tmp_path / "rag_agent_memories.jsonl"

    with pytest.raises(ValueError):
        legacy_export.export_table(database, "rag_agent_memories", output)

    assert not output.exists()
    assert not output.with_name(f"{output.name}.partial").exists()


def test_json_value_converts_oracle_types() -> None:
    assert legacy_export._json_value(Decimal("2")) == 2
    assert legacy_export._json_value(Decimal("0.125")) == 0.125
    assert legacy_export._json_value(b"\x00\x01") == "AAE="
    assert legacy_export._json_value((1, Decimal("1.5"))) == [1, 1.5]
    assert legacy_export._json_value(None) is None
