"""実 Oracle AI Database で、台帳が適用済みでも欠けた列をシステムテーブルの状態に出すか（#2458）。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、初期化済みのテスト用の
schema で、migration 006 が足す列を一時的に別名に変えて「列が欠けた表」を再現し、finally で必ず
元の名前に戻す（データは消さない）。LLM は呼ばない。資格情報は出力しない。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.settings.system_schema import SystemSchemaError, SystemSchemaManager
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)

_TABLE = "NL2SQL_SCHEMA_REFRESH_JOBS"
_COLUMN = "WORKER_ID"
_RENAMED = "T2458_WORKER_ID"


@pytest.fixture
def live_settings() -> Iterator[Any]:
    """conftest が PLATFORM_ORACLE_USER=APP を強制するため、共通 .env の接続ユーザーへ戻す。"""
    settings = get_settings()
    env_user = str(
        dotenv_values(Path(__file__).resolve().parents[3] / "platform" / ".env").get(
            "PLATFORM_ORACLE_USER"
        )
        or os.environ.get("PLATFORM_ORACLE_USER", "")
    )
    original_user = settings.oracle_user
    if env_user:
        settings.oracle_user = env_user
    try:
        yield settings
    finally:
        settings.oracle_user = original_user


def _rename_column(adapter: OracleNl2SqlAdapter, old: str, new: str) -> None:
    with adapter.connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {_TABLE} RENAME COLUMN {old} TO {new}")  # nosec B608


def test_status_reports_missing_column_on_oracle(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    manager = SystemSchemaManager(adapter.connection, ddl_lock_timeout_seconds=5)
    before = manager.status()
    assert before["status"] == "ready", before["missing_columns"]
    assert before["missing_columns"] == []

    _rename_column(adapter, _COLUMN, _RENAMED)
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
        _rename_column(adapter, _RENAMED, _COLUMN)

    # 列が戻れば ready。直前の失敗の記録は、次の作成・更新（何もしない）で消える。
    after = manager.initialize()
    assert after["status"] == "ready"
    assert after["operation"] == "no_op"
    assert after["missing_columns"] == []
