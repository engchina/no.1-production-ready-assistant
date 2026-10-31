import pytest

from pr_backend_core.oracle_session import (
    SESSION_INIT_SQL,
    SESSION_UTC_SQL,
    init_oracle_session,
    local_session_time_zone,
)


class _Cursor:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self._executed.append(statement)


class _Connection:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.executed)


def test_init_oracle_session_disables_result_cache_and_pins_utc() -> None:
    connection = _Connection()

    init_oracle_session(connection, "any-tag")

    # 1 回の ALTER SESSION で、result cache を使わず（#333）、
    # session の時間帯を UTC に固定する（#3255）。
    assert connection.executed == [SESSION_INIT_SQL]
    assert SESSION_INIT_SQL == "ALTER SESSION SET RESULT_CACHE_MODE = MANUAL TIME_ZONE = '+00:00'"


def test_session_time_zone_is_an_offset_not_a_region_name() -> None:
    # 名前の時間帯（'UTC'・'Asia/Tokyo'）の値は python-oracledb の thin mode で読めない
    # （DPY-3022。#3241）。
    time_zone = SESSION_INIT_SQL.split("TIME_ZONE = ", 1)[1]
    assert time_zone == "'+00:00'"


def test_local_session_time_zone_sets_offset_and_restores_utc() -> None:
    connection = _Connection()

    with local_session_time_zone(connection, "+09:00"):
        connection.executed.append("SELECT CURRENT_DATE FROM DUAL")

    # 業務データの SQL の間だけ設定の時間帯にし、抜けると UTC に戻す（#3265）。
    assert connection.executed == [
        "ALTER SESSION SET TIME_ZONE = '+09:00'",
        "SELECT CURRENT_DATE FROM DUAL",
        SESSION_UTC_SQL,
    ]
    assert SESSION_UTC_SQL == "ALTER SESSION SET TIME_ZONE = '+00:00'"


def test_local_session_time_zone_restores_utc_when_the_sql_fails() -> None:
    connection = _Connection()

    with pytest.raises(RuntimeError), local_session_time_zone(connection, "-05:00"):
        raise RuntimeError("ORA-00942")

    assert connection.executed == ["ALTER SESSION SET TIME_ZONE = '-05:00'", SESSION_UTC_SQL]


def test_local_session_time_zone_skips_round_trips_for_utc() -> None:
    connection = _Connection()

    with local_session_time_zone(connection, "+00:00"):
        pass

    assert connection.executed == []


@pytest.mark.parametrize("offset", ["Asia/Tokyo", "+9:00", "+09:00'; DROP", ""], ids=str)
def test_local_session_time_zone_rejects_values_that_are_not_offsets(offset: str) -> None:
    connection = _Connection()
    with pytest.raises(ValueError, match=r"\+HH:MM"), local_session_time_zone(connection, offset):
        pass
    assert connection.executed == []
