"""Issue #471: 引用識別子・派生投影の系譜を検証から公開コンテキストまで保持する。"""

import pytest
from test_nl2sql_ontology_scope_recovery import inspect, metric
from test_nl2sql_ontology_workspace import model, no_auth  # noqa: F401

from app.features.nl2sql.ontology_definition_validation import checked_expression
from app.features.nl2sql.ontology_published_context import published_context
from app.features.nl2sql.ontology_sql_validation import expression_in_query


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "o".ID, SUM("o".AMOUNT) FROM APP.ORDERS "o" WHERE "o".AMOUNT > 0 GROUP BY "o".ID',
        'SELECT "Mixed Case".ID, SUM("Mixed Case".AMOUNT) FROM APP.ORDERS "Mixed Case" '
        'WHERE "Mixed Case".AMOUNT > 0 GROUP BY "Mixed Case".ID',
        "WITH src AS (SELECT ID, AMOUNT FROM APP.ORDERS) "
        "SELECT src.ID, SUM(src.AMOUNT) FROM src WHERE src.AMOUNT > 0 GROUP BY src.ID",
        "SELECT src.ID, SUM(src.AMOUNT) FROM (SELECT ID, AMOUNT FROM APP.ORDERS) src "
        "WHERE src.AMOUNT > 0 GROUP BY src.ID",
        'WITH "src" AS (SELECT ID AS "key", AMOUNT AS "value" FROM APP.ORDERS), '
        'next_src AS (SELECT "key", "value" AS "amount" FROM "src") '
        'SELECT n."key", SUM(n."amount") FROM next_src n '
        'WHERE n."amount" > 0 GROUP BY n."key"',
        'WITH src("key", "value") AS (SELECT ID, AMOUNT FROM APP.ORDERS) '
        'SELECT src."key", SUM(src."value") FROM src '
        'WHERE src."value" > 0 GROUP BY src."key"',
        'WITH src AS (SELECT ID, AMOUNT AS "a", CUSTOMER_ID AS "A" FROM APP.ORDERS) '
        'SELECT src.ID, SUM(src."a") FROM src WHERE src."a" > 0 GROUP BY src.ID',
    ],
)
def test_metric_lineage_validates_publishes_and_remains_in_query_context(sql: str) -> None:
    definition = metric("APP.ORDERS.AMOUNT > 0")
    definition.expression_sql = sql
    svc, bundle, errors = inspect([*model(), definition])
    assert not errors
    bundle = svc.validate("sales", bundle.id, bundle.etag, None)
    bundle = svc.review("sales", bundle.id, bundle.etag, [d.id for d in bundle.definitions], None)
    release = svc.publish("sales", bundle.id, bundle.etag, "", "publish", None)
    context = published_context(svc.runtime, "sales", release["id"])
    # LLM には定義から作る業務の知識（指標の式・条件）を渡す（#1720）。
    assert "- 売上（" in context
    assert f"測定値: {sql}" in context
    assert "絞り込みの条件: APP.ORDERS.AMOUNT > 0" in context
    narrowed = published_context(
        svc.runtime,
        "sales",
        release["id"],
        {"APP.ORDERS": ["ID", "CUSTOMER_ID"], "APP.CUSTOMERS": ["ID", "AMOUNT"]},
    )
    assert "- 売上（" not in narrowed


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(o.AMOUNT) FROM APP.ORDERS o JOIN APP.ORDERS p ON o.ID=p.ID",
        "WITH src AS (SELECT ID FROM APP.ORDERS) SELECT ID FROM src",
        "WITH src AS (SELECT AMOUNT * 2 AS AMOUNT FROM APP.ORDERS) SELECT AMOUNT FROM src",
        "WITH src AS (SELECT AMOUNT AS a, AMOUNT AS b FROM APP.ORDERS) SELECT a,b FROM src",
        "WITH src AS (SELECT AMOUNT FROM APP.CUSTOMERS) SELECT AMOUNT FROM src",
    ],
)
def test_filter_rejects_ambiguous_hidden_computed_or_different_source(sql: str) -> None:
    query = checked_expression(sql)
    original = query.sql()
    with pytest.raises(ValueError, match="一意に解決"):
        expression_in_query(
            checked_expression("APP.ORDERS.AMOUNT > 0"),
            query,
            {"APP.ORDERS": {"ID", "AMOUNT"}, "APP.CUSTOMERS": {"ID", "AMOUNT"}},
        )
    assert query.sql() == original


def test_derived_filter_still_requires_the_exact_business_condition() -> None:
    definition = metric("APP.ORDERS.AMOUNT > 0")
    definition.expression_sql = (
        "WITH src AS (SELECT ID, AMOUNT FROM APP.ORDERS) "
        "SELECT ID, SUM(AMOUNT) FROM src WHERE AMOUNT > 1 GROUP BY ID"
    )
    _, _, errors = inspect([*model(), definition])
    assert "METRIC_FILTER_MISMATCH" in errors
