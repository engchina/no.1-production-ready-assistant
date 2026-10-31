"""Oracle JSON CLOB の array fetch 設定テスト。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import app.features.nl2sql.oracle_lob as oracle_lob


class _Cursor:
    arraysize = 128

    def __init__(self) -> None:
        self.outputtypehandler: Any = None
        self.variables: list[tuple[Any, int]] = []

    def var(self, db_type: Any, *, arraysize: int) -> tuple[Any, int]:
        variable = (db_type, arraysize)
        self.variables.append(variable)
        return variable


def test_clob_fetch_handler_maps_only_clob_to_long(
    monkeypatch: Any,
) -> None:
    clob_type = object()
    long_type = object()
    driver = SimpleNamespace(DB_TYPE_CLOB=clob_type, DB_TYPE_LONG=long_type)
    monkeypatch.setattr(oracle_lob, "import_module", lambda _name: driver)
    cursor = _Cursor()

    oracle_lob.configure_clob_fetch_as_text(cursor)

    clob_variable = cursor.outputtypehandler(
        cursor,
        SimpleNamespace(type_code=clob_type),
    )
    other_variable = cursor.outputtypehandler(
        cursor,
        SimpleNamespace(type_code=object()),
    )
    assert clob_variable == (long_type, cursor.arraysize)
    assert other_variable is None
    assert cursor.variables == [(long_type, cursor.arraysize)]
