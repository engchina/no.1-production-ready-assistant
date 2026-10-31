"""SELECT-only 安全判定の回帰テスト。

危険語・`;` の判定は SQL の構造だけを見るべきで、業務データの値やコメントに
含まれる語で正当な SELECT を弾いてはならない(Issue: is_select_only の誤ブロック)。
"""

from __future__ import annotations

import pytest

from app.features.nl2sql.models import AllowedObjects
from app.features.nl2sql.service import (
    Nl2SqlService,
    _mask_sql_literals_and_comments,
    is_select_only,
)
from app.features.nl2sql.store import MemoryNl2SqlStore


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM T WHERE MEMO = 'please delete this row'",
        "SELECT * FROM T WHERE ACTION = 'update'",
        "SELECT * FROM T WHERE NOTE = 'it''s an update'",
        "-- 売上集計\nSELECT * FROM T",
        "/* 集計 */ SELECT * FROM T",
        "-- 1 行目\n/* 2 行目 */\nSELECT * FROM T",
        "SELECT ';' AS X FROM DUAL",
        'SELECT "DELETE FLAG" FROM T',
        "SELECT * FROM T WHERE NOTE = q'[drop; create]'",
        "SELECT ID FROM T -- ; DELETE FROM T",
        "SELECT ID FROM T /* truncate table */ WHERE ID = 1",
        "(SELECT 1 FROM DUAL)",
        "( SELECT 1 FROM DUAL )",
        "WITH x AS (SELECT 1 AS v FROM DUAL) SELECT v FROM x",
        "SELECT CREATE_DATE, UPDATED_BY FROM T",
        "SELECT CALL FROM APP.ORDERS",
        "SELECT MERGE, TRUNCATE, BEGIN, DECLARE FROM APP.ORDERS",
        "SELECT ID AS CALL FROM APP.ORDERS",
        "SELECT * FROM T;",
        "SELECT * FROM T;  -- 末尾コメント",
    ],
)
def test_is_select_only_accepts_read_only_sql_with_literals_and_comments(sql: str) -> None:
    assert is_select_only(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "-- only a comment",
        "/* x */ DELETE FROM T",
        "-- 説明\nUPDATE T SET A = 1",
        "SELECT 1 FROM DUAL; DELETE FROM T",
        "SELECT 1 FROM DUAL; SELECT 2 FROM DUAL",
        "BEGIN NULL; END;",
        "DECLARE v NUMBER; BEGIN NULL; END;",
        "CALL P()",
        "SELECT * FROM T WHERE ID = (DELETE FROM T)",
        "(DELETE FROM T)",
        "SELECT 'unterminated FROM T; DELETE FROM T",
        "drop table t",
        "merge into t using d on (t.id = d.id) when matched then update set t.a = d.a",
    ],
)
def test_is_select_only_rejects_mutations_and_multi_statements(sql: str) -> None:
    assert is_select_only(sql) is False


def test_mask_keeps_length_and_blanks_only_literal_and_comment_bodies() -> None:
    sql = "SELECT 'a;b' AS x, \"Q\" FROM T -- c\n/* d */ WHERE y = q'[z]'"
    masked = _mask_sql_literals_and_comments(sql)

    assert len(masked) == len(sql)
    assert "a;b" not in masked
    assert "-- c" not in masked
    assert "/* d */" not in masked
    assert "q'[z]'" not in masked
    assert masked.split() == ["SELECT", "AS", "x,", "FROM", "T", "WHERE", "y", "="]


def test_analyze_sql_keeps_leading_comment_select_executable() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    sql = "-- 注文一覧\nSELECT ID FROM APP.ORDERS WHERE STATUS = 'update'"

    analysis = service.analyze_sql(sql, AllowedObjects(), 100)

    assert analysis.safety.is_select_only is True
    assert analysis.safety.blocked_reason == ""
    assert analysis.safety.is_safe is True
    assert analysis.executable_sql == sql


@pytest.mark.parametrize(
    ("sql", "function_name"),
    [
        (
            "SELECT DBMS_XMLGEN.GETXML('SELECT * FROM APP.SALARY') "
            "FROM APP.ORDERS WHERE ROWNUM = 1",
            "DBMS_XMLGEN.GETXML",
        ),
        (
            "SELECT DBMS_XMLQUERY.GETXML('SELECT * FROM APP.SALARY') FROM APP.ORDERS",
            "DBMS_XMLQUERY.GETXML",
        ),
        (
            "SELECT DBMS_XMLGEN.GETXML('SELECT PASSWORD_HASH FROM PLATFORM_USERS') FROM DUAL",
            "DBMS_XMLGEN.GETXML",
        ),
        (
            "SELECT UTL_INADDR.GET_HOST_ADDRESS('attacker.example') FROM APP.ORDERS",
            "UTL_INADDR.GET_HOST_ADDRESS",
        ),
        (
            "SELECT HTTPURITYPE('http://169.254.169.254/').GETCLOB() FROM APP.ORDERS",
            "HTTPURITYPE.GETCLOB",
        ),
        (
            "SELECT UTL_HTTP.REQUEST('http://169.254.169.254/') FROM APP.ORDERS",
            "UTL_HTTP.REQUEST",
        ),
        (
            "SELECT DBMS_METADATA.GET_DDL('TABLE', 'ORDERS') FROM APP.ORDERS",
            "DBMS_METADATA.GET_DDL",
        ),
    ],
)
def test_analyze_sql_blocks_dangerous_oracle_functions(
    sql: str,
    function_name: str,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    analysis = service.analyze_sql(
        sql,
        AllowedObjects(table_names=["APP.ORDERS", "DUAL"]),
        100,
    )

    assert analysis.safety.is_select_only is True
    assert analysis.safety.is_safe is False
    assert function_name in analysis.safety.blocked_reason
    assert "危険な Oracle 関数" in analysis.safety.blocked_reason
    assert "許可されていない表" not in analysis.safety.blocked_reason
