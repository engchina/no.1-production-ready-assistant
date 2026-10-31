"""python-oracledb の JSON CLOB を array fetch で文字列化する補助。"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any


def _clob_as_text_handler() -> Callable[[Any, Any], Any] | None:
    try:
        oracledb = import_module("oracledb")
    except Exception:  # pragma: no cover - optional driver boundary
        return None

    def output_type_handler(inner_cursor: Any, metadata: Any) -> Any:
        if metadata.type_code is not oracledb.DB_TYPE_CLOB:
            return None
        return inner_cursor.var(
            oracledb.DB_TYPE_LONG,
            arraysize=inner_cursor.arraysize,
        )

    return output_type_handler


def configure_clob_fetch_as_text(cursor: Any) -> None:
    """CLOB locator の行単位 read を避け、DB_TYPE_LONG として一括取得する。"""

    handler = _clob_as_text_handler()
    if handler is not None:
        cursor.outputtypehandler = handler


def configure_connection_clob_fetch_as_text(connection: Any) -> None:
    """接続のすべての query で、CLOB 列を文字列として fetch する（#904）。

    CLOB を LOB locator で受け取ると、`read()` のたびに DB との往復が 1 回増える（遅延の大きい
    ネットワークでは 1 行ごとに約 0.4 秒）。状態の保存先（`NL2SQL_*` の表の JSON CLOB）の接続に
    当て、行と一緒に本文を受け取る。OUT bind（`cursor.var(DB_TYPE_CLOB)`）には影響しない。
    """

    handler = _clob_as_text_handler()
    if handler is not None:
        connection.outputtypehandler = handler
