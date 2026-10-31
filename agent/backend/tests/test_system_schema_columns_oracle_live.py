"""実 Oracle AI Database で、台帳が適用済みでも欠けた列をシステムテーブルの状態に出すか（#2458）。

実 Oracle の結合テスト（`AGENT_RUN_ORACLE_INTEGRATION=1`。接続は環境変数の `PLATFORM_ORACLE_*`）
だけで実行する。初期化済みのテスト用の schema で、migration 014 が足す列を一時的に別名に変えて
「列が欠けた表」を再現し、finally で必ず元の名前に戻す（データは消さない）。
"""

from __future__ import annotations

import os

import pytest

from app.oracle_connection import connect_platform_oracle
from app.system_schema import SystemSchemaError, SystemSchemaManager

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_RUN_ORACLE_INTEGRATION") != "1",
    reason="実 Oracle の結合テスト（AGENT_RUN_ORACLE_INTEGRATION=1）だけで実行する",
)

_TABLE = "AGENT_RUN_FACTS"
_COLUMN = "DATA_TOOL_CALLS"
_RENAMED = "T2458_DATA_TOOL_CALLS"


def _rename_column(old: str, new: str) -> None:
    with connect_platform_oracle() as connection, connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {_TABLE} RENAME COLUMN {old} TO {new}")  # nosec B608


def test_status_reports_missing_column_on_oracle() -> None:
    manager = SystemSchemaManager()
    before = manager.status()
    assert before["status"] == "ready", before["missing_columns"]
    assert before["missing_columns"] == []

    _rename_column(_COLUMN, _RENAMED)
    try:
        status = manager.status()
        assert status["status"] == "outdated"
        assert status["pending_versions"] == []
        assert status["missing_objects"] == []
        assert status["missing_columns"] == [{"table_name": _TABLE, "column_name": _COLUMN}]
        # 列を足す migration は台帳で適用済みなので、更新では直らない。列の名前を出して止める。
        with pytest.raises(SystemSchemaError) as raised:
            manager.initialize()
        assert raised.value.code == "SCHEMA_POSTCONDITION_FAILED"
        assert f"{_TABLE}.{_COLUMN}" in raised.value.public_message
    finally:
        _rename_column(_RENAMED, _COLUMN)

    # 列が戻れば ready。直前の失敗の記録は、次の作成・更新（何もしない）で消える。
    after = manager.initialize()
    assert after["status"] == "ready"
    assert after["operation"] == "no_op"
    assert after["missing_columns"] == []
