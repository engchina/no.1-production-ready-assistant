"""JSON 列を書く executemany は CLOB で bind する（#2043。32KB を超える行で ORA-40441）。"""

import inspect

import oracledb

from app.clients import oracle as oracle_module
from app.clients.oracle import _executemany


class _Cursor:
    def __init__(self) -> None:
        self.input_sizes: dict[str, object] = {}
        self.rows: list[object] = []

    def setinputsizes(self, **sizes: object) -> None:
        self.input_sizes.update(sizes)

    def executemany(self, statement: str, rows: list[object]) -> None:
        self.rows = rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.cursors: list[_Cursor] = []

    def cursor(self) -> _Cursor:
        cursor = _Cursor()
        self.cursors.append(cursor)
        return cursor


def test_executemany_binds_json_columns_as_clob() -> None:
    connection = _Connection()
    rows = [{"metadata_json": "{}"}, {"metadata_json": '{"text":"' + "あ" * 12000 + '"}'}]

    _executemany(
        connection,  # type: ignore[arg-type]
        "INSERT INTO rag_chunks (metadata_json) VALUES (:metadata_json)",
        rows,
        input_sizes={"metadata_json": oracledb.DB_TYPE_CLOB},
    )

    assert connection.cursors[0].input_sizes == {"metadata_json": oracledb.DB_TYPE_CLOB}
    assert connection.cursors[0].rows == rows


def test_json_column_writes_pass_clob_input_sizes() -> None:
    """JSON 型の列（metadata_json・関係情報の source_document_ids）を書く executemany。"""
    source = inspect.getsource(oracle_module)
    assert source.count('input_sizes=_json_input_sizes("metadata_json")') >= 2  # INSERT と UPDATE
    assert source.count('input_sizes=_json_input_sizes("source_document_ids")') >= 2
