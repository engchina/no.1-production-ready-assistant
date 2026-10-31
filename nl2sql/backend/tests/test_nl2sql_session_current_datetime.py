"""生成した SQL の「今」を共通の設定の時間帯にそろえる（#3265）。

ADB の `SYSDATE` / `SYSTIMESTAMP` は DB のサーバーの時刻（UTC）で、session の時間帯に従わない。
生成した SQL は session の時間帯の `CURRENT_DATE` / `CURRENT_TIMESTAMP` にし、業務データの
SELECT の session の時間帯を設定の時間帯にする（`OracleNl2SqlAdapter.execute_select`）。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.features.nl2sql.models import Nl2SqlEngine
from app.features.nl2sql.service import GeneratedSql, Nl2SqlService
from app.features.nl2sql.sql_lexing import use_session_current_datetime
from app.features.nl2sql.store import MemoryNl2SqlStore


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT ID FROM APP.ORDERS WHERE DUE_DATE < TRUNC(SYSDATE)",
            "SELECT ID FROM APP.ORDERS WHERE DUE_DATE < TRUNC(CURRENT_DATE)",
        ),
        (
            "select id from app.orders where created_at >= systimestamp - interval '1' day",
            "select id from app.orders where created_at >= CURRENT_TIMESTAMP - interval '1' day",
        ),
        (
            "SELECT MONTHS_BETWEEN(SysDate, BIRTH_DATE) FROM APP.PEOPLE",
            "SELECT MONTHS_BETWEEN(CURRENT_DATE, BIRTH_DATE) FROM APP.PEOPLE",
        ),
        (
            "SELECT TRUNC(SYSDATE,'MM'),SYSDATE-1 FROM DUAL",
            "SELECT TRUNC(CURRENT_DATE,'MM'),CURRENT_DATE-1 FROM DUAL",
        ),
    ],
    ids=["trunc", "timestamp-lowercase", "mixed-case", "no-spaces"],
)
def test_db_clock_functions_become_session_time_zone_functions(sql: str, expected: str) -> None:
    assert use_session_current_datetime(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'SYSDATE' AS LABEL FROM DUAL",
        "SELECT q'[SYSDATE; SYSTIMESTAMP]' FROM DUAL",
        'SELECT "SYSDATE" FROM APP.T',
        "SELECT T.SYSDATE_FLAG, SYSDATE_X, X_SYSDATE FROM APP.T",
        "SELECT ID FROM APP.T -- SYSDATE in a comment\n",
        "SELECT ID /* SYSTIMESTAMP */ FROM APP.T",
        "SELECT CURRENT_DATE FROM DUAL",
    ],
    ids=[
        "string",
        "q-quote",
        "quoted-identifier",
        "identifier-part",
        "line-comment",
        "block-comment",
        "already-session",
    ],
)
def test_quotes_comments_and_identifiers_are_kept(sql: str) -> None:
    assert use_session_current_datetime(sql) == sql


def test_rewrite_is_idempotent() -> None:
    once = use_session_current_datetime("SELECT SYSDATE, SYSTIMESTAMP FROM DUAL")
    assert once == "SELECT CURRENT_DATE, CURRENT_TIMESTAMP FROM DUAL"
    assert use_session_current_datetime(once) == once


# 生成の本体は差し替えるため、業務プロファイル・対象は使わない。
_UNUSED = cast(Any, None)


def _generated(sql: str) -> GeneratedSql:
    return GeneratedSql(
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        generated_sql=sql,
        explanation="",
        engine_meta={"provider": "stub"},
    )


def test_generated_sql_uses_session_current_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    def generate(*_args: Any, **_kwargs: Any) -> GeneratedSql:
        return _generated("SELECT ID FROM APP.ORDERS WHERE DUE_DATE < TRUNC(SYSDATE)")

    monkeypatch.setattr(service, "_generate_sql_with_engine", generate)
    generated = service._generate_sql(  # noqa: SLF001
        Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        "q",
        _UNUSED,
        _UNUSED,
        None,
        [],
    )

    # 画面・履歴・実行の SQL は同じ（置き換えた後の SQL）。
    assert generated.generated_sql == (
        "SELECT ID FROM APP.ORDERS WHERE DUE_DATE < TRUNC(CURRENT_DATE)"
    )
    assert generated.engine_meta == {"provider": "stub", "session_current_datetime": True}


def test_generated_sql_without_db_clock_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    original = _generated("SELECT ID FROM APP.ORDERS WHERE ORDER_DATE >= DATE '2026-10-01'")
    monkeypatch.setattr(service, "_generate_sql_with_engine", lambda *_a, **_k: original)

    generated = service._generate_sql(  # noqa: SLF001
        Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        "q",
        _UNUSED,
        _UNUSED,
        None,
        [],
    )

    assert generated.generated_sql == original.generated_sql
    assert generated.engine_meta == {"provider": "stub"}
