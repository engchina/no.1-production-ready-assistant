"""変換エラー既定値を含む SELECT の分類と、式を隠さない安全境界。"""

import pytest
from test_nl2sql_db_admin_chat import _catalog_service, _FakeAdapter, _FakeEnterpriseAi

from app.features.nl2sql.service import (
    _dangerous_oracle_function_names,
    _unapproved_function_names,
    is_select_only,
    normalize_executable_sql,
)
from app.features.nl2sql.sql_semantics import parse_oracle_sql


@pytest.mark.parametrize("fallback", ["NULL", "'2026-01-01'", ":fallback", "COALESCE(T.B, T.C)"])
def test_conversion_default_is_preserved_and_read_only(fallback: str) -> None:
    sql = f"SELECT TO_DATE(T.A DEFAULT {fallback} ON CONVERSION ERROR, 'YYYY-MM-DD') AS D FROM T"
    assert is_select_only(sql)
    plan = _catalog_service(_FakeAdapter(), _FakeEnterpriseAi([]))._db_admin_chat_plan(sql)
    assert plan.read_only
    assert not plan.statements[0].requires_confirmation
    graph = parse_oracle_sql(sql).graph
    assert graph is not None
    assert "DEFAULT" in graph.projections[0].expression_sql
    assert "ON CONVERSION ERROR" in graph.projections[0].expression_sql
    assert normalize_executable_sql(sql) == sql
    if "T.B" in fallback:
        assert {c.name for c in graph.columns} >= {"A", "B", "C"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT TO_DATE('bad' DEFAULT ON CONVERSION ERROR) FROM DUAL",
        "SELECT TO_DATE('bad' DEFAULT NULL ON ERROR) FROM DUAL",
        "SELECT TO_DATE('bad' DEFAULT NULL ON CONVERSION ERROR, ) FROM DUAL",
        "SELECT TO_DATE('bad' DEFAULT NULL ON CONVERSION ERROR, 'a', 'b', 'c') FROM DUAL",
        "SELECT TO_DATE('bad' DEFAULT NULL ON CONVERSION ERROR) FROM DUAL FOR UPDATE",
        "SELECT TO_DATE('bad' DEFAULT NULL ON CONVERSION ERROR) INTO T FROM DUAL",
        "SELECT TO_DATE('bad' DEFAULT NULL ON CONVERSION ERROR) FROM DUAL; DELETE FROM T",
    ],
)
def test_conversion_syntax_does_not_relax_read_only_boundary(sql: str) -> None:
    assert not is_select_only(sql)


def test_default_expression_functions_are_still_inspected() -> None:
    sql = "SELECT TO_DATE('bad' DEFAULT UTL_HTTP.REQUEST('x') ON CONVERSION ERROR) FROM DUAL"
    assert _dangerous_oracle_function_names(sql) == ["UTL_HTTP.REQUEST"]
    assert _unapproved_function_names(sql, []) == ["UTL_HTTP.REQUEST"]
    sql = "SELECT TO_DATE('bad' DEFAULT APP.FN() ON CONVERSION ERROR) FROM DUAL"
    assert _unapproved_function_names(sql, []) == ["APP.FN"]
