"""実行 endpoint までの Oracle 引用/コメント互換と安全境界。"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.features.nl2sql import router
from app.features.nl2sql.models import ExecuteRequest, QueryResults
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService, is_select_only, normalize_executable_sql
from app.features.nl2sql.sql_lexing import prepare_oracle_query
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT ID FROM APP.ORDERS;;", "SELECT ID FROM APP.ORDERS"),
        ("SELECT ID FROM APP.ORDERS; ; ;", "SELECT ID FROM APP.ORDERS"),
        (
            "SELECT ID FROM APP.ORDERS; /* note */; -- tail\n;",
            "SELECT ID FROM APP.ORDERS /* note */ -- tail",
        ),
        (
            "SELECT q'[it''s; valid]' AS NOTE FROM APP.ORDERS;;",
            "SELECT q'[it''s; valid]' AS NOTE FROM APP.ORDERS",
        ),
        ("SELECT ID FROM APP.ORDERS; -- explanation", "SELECT ID FROM APP.ORDERS -- explanation"),
        ("SELECT ID FROM APP.ORDERS; /* note */", "SELECT ID FROM APP.ORDERS /* note */"),
        (
            "SELECT q'[it's valid]' AS NOTE FROM APP.ORDERS;",
            "SELECT q'[it's valid]' AS NOTE FROM APP.ORDERS",
        ),
        (
            "SELECT q'{delete; 'update'}' AS NOTE FROM APP.ORDERS",
            "SELECT q'{delete; 'update'}' AS NOTE FROM APP.ORDERS",
        ),
        (
            "SELECT q'!a;b!' AS NOTE FROM APP.ORDERS; -- q'[comment]'",
            "SELECT q'!a;b!' AS NOTE FROM APP.ORDERS -- q'[comment]'",
        ),
        (
            "SELECT 'q''[literal]'';' AS NOTE FROM APP.ORDERS;",
            "SELECT 'q''[literal]'';' AS NOTE FROM APP.ORDERS",
        ),
        (
            'SELECT ID AS "q\'[quoted]" FROM APP.ORDERS;',
            'SELECT ID AS "q\'[quoted]" FROM APP.ORDERS',
        ),
    ],
)
def test_execute_preserves_literals_and_removes_only_statement_terminator(
    monkeypatch: pytest.MonkeyPatch, sql: str, expected: str
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    execute = Mock(return_value=QueryResults(columns=[], rows=[], total=0))
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service._oracle_adapter, "execute_select", execute)
    monkeypatch.setattr(router, "nl2sql_service", service)
    assert is_select_only(sql)
    router.execute(
        ExecuteRequest(sql=sql, row_limit=100),
        SimpleNamespace(state=SimpleNamespace(principal=None)),  # type: ignore[arg-type]
    )
    execute.assert_called_once_with(expected, 100)
    assert normalize_executable_sql(expected) == expected
    parsed = prepare_oracle_query(expected)
    assert prepare_oracle_query(parsed) == parsed


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT q'[ok]' FROM APP.ORDERS; DELETE FROM APP.ORDERS",
        "SELECT 1 FROM APP.ORDERS;; DELETE FROM APP.ORDERS;;",
        "SELECT 1 FROM APP.ORDERS; /* between */; SELECT 2 FROM APP.ORDERS;;",
        "SELECT 1 FROM APP.ORDERS; /* note */ SELECT 2 FROM APP.ORDERS",
        "SELECT q'[unterminated' FROM APP.ORDERS",
        "SELECT 1 FROM APP.ORDERS; /* unterminated",
        "SELECT UTL_HTTP.REQUEST(q'[http://example.invalid/it's]') FROM APP.ORDERS",
        "SELECT DBMS_XMLGEN.GETXML(q'[SELECT * FROM SALARY]') FROM APP.ORDERS",
        "SELECT q'[ok]' FROM APP.ORDERS FOR UPDATE",
    ],
)
def test_execute_compatibility_does_not_weaken_safety(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    execute = Mock()
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service._oracle_adapter, "execute_select", execute)
    monkeypatch.setattr(router, "nl2sql_service", service)
    with pytest.raises(HTTPException) as denied:
        router.execute(
            ExecuteRequest(sql=sql, row_limit=100),
            SimpleNamespace(state=SimpleNamespace(principal=None)),  # type: ignore[arg-type]
        )
    assert denied.value.status_code == 400
    execute.assert_not_called()


def test_semantic_graph_retains_q_quoted_predicate_value() -> None:
    sql = "SELECT ID FROM APP.ORDERS WHERE NOTE = q'[it's valid; -- not a comment]'"
    graph = parse_oracle_sql(sql).graph
    assert graph is not None
    assert "it''s valid; -- not a comment" in graph.filters[0].expression_sql
    assert (
        prepare_oracle_query("SELECT nq'[日本語's]' FROM DUAL") == "SELECT N'日本語''s' FROM DUAL"
    )


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["ID", "ID"], ["ID", "ID_2"]),
        (["ID", "ID", "ID_2", "ID"], ["ID", "ID_3", "ID_2", "ID_4"]),
        (["ID", "NAME"], ["ID", "NAME"]),
    ],
)
def test_oracle_results_preserve_every_column(names: list[str], expected: list[str]) -> None:
    values = tuple(range(1, len(names) + 1))
    cursor = Mock()
    cursor.description = [(name,) for name in names]
    cursor.fetchmany.return_value = [values]

    @contextmanager
    def cursor_context() -> Iterator[Mock]:
        yield cursor

    @contextmanager
    def connection(**_kwargs: object) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(cursor=cursor_context)

    adapter = OracleNl2SqlAdapter(get_settings())
    adapter.user_data_connection = connection  # type: ignore[method-assign]
    result = adapter.execute_select("SELECT 1 AS ID, 2 AS ID FROM DUAL", 100)
    assert result.columns == expected
    assert result.rows == [dict(zip(expected, values, strict=True))]
