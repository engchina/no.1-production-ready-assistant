"""列展開（`*` / `別名.*` / `owner.表.*`）と `COUNT(*)` の接地の扱い（#1787）。

生成 SQL の接地確認（画面の `sqlGrounding.ts`）は、SQL の semantic graph の
`columns[].name == "*"` と `projections[].contains_wildcard` で列展開を見分け、
列名 `*` の列ではなく「その表の全列」として表の接地に数える。ここではその前提の
graph の形と、オントロジー構築の Q/A の SQL の解決が列展開を未解決にしないことを確かめる。
"""

from __future__ import annotations

import pytest

from app.features.nl2sql.ontology_build import _qa_pair_context_payload
from app.features.nl2sql.ontology_models import QaPair
from app.features.nl2sql.sql_semantics import parse_oracle_sql

SCHEMA_CONTEXT = {
    "objects": [
        {
            "owner": "ADB_APP_USER",
            "object_name": "DEPARTMENT",
            "object_type": "TABLE",
            "columns": [
                {"column": "DEPARTMENT_ID", "data_type": "NUMBER"},
                {"column": "DEPARTMENT_NAME", "data_type": "VARCHAR2"},
            ],
        }
    ]
}


@pytest.mark.parametrize(
    ("sql", "table", "owner"),
    [
        ('SELECT dept.* FROM "ADB_APP_USER"."DEPARTMENT" dept', "dept", ""),
        (
            "SELECT ADB_APP_USER.DEPARTMENT.* FROM ADB_APP_USER.DEPARTMENT",
            "DEPARTMENT",
            "ADB_APP_USER",
        ),
    ],
    ids=["alias-star", "owner-table-star"],
)
def test_qualified_star_is_marked_as_wildcard_column(sql: str, table: str, owner: str) -> None:
    graph = parse_oracle_sql(sql).graph
    assert graph is not None

    assert [(column.owner, column.table, column.name) for column in graph.columns] == [
        (owner, table, "*")
    ]
    assert [projection.contains_wildcard for projection in graph.projections] == [True]
    assert [table.name for table in graph.tables] == ["DEPARTMENT"]


def test_unqualified_star_is_a_wildcard_projection_without_column() -> None:
    graph = parse_oracle_sql('SELECT * FROM "ADB_APP_USER"."DEPARTMENT" dept').graph
    assert graph is not None

    assert graph.columns == []
    assert [projection.contains_wildcard for projection in graph.projections] == [True]


def test_count_star_is_neither_a_column_nor_a_wildcard_projection() -> None:
    graph = parse_oracle_sql('SELECT COUNT(*) FROM "ADB_APP_USER"."DEPARTMENT" dept').graph
    assert graph is not None

    assert graph.columns == []
    assert [projection.contains_wildcard for projection in graph.projections] == [False]
    assert [aggregate.referenced_columns for aggregate in graph.aggregates] == [[]]


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT dept.* FROM "ADB_APP_USER"."DEPARTMENT" dept',
        "SELECT ADB_APP_USER.DEPARTMENT.* FROM ADB_APP_USER.DEPARTMENT",
        "SELECT * FROM ADB_APP_USER.DEPARTMENT",
        "SELECT COUNT(*) FROM ADB_APP_USER.DEPARTMENT d",
    ],
    ids=["alias-star", "owner-table-star", "star", "count-star"],
)
def test_qa_context_does_not_report_star_as_unresolved_column(sql: str) -> None:
    payload = _qa_pair_context_payload(SCHEMA_CONTEXT, QaPair(question="部門の全列", sql=sql))

    assert payload["schema_unresolved_references"] == []
    assert [item["object"] for item in payload["schema_resolved_objects"]] == [
        "ADB_APP_USER.DEPARTMENT"
    ]
    assert payload["schema_resolved_columns"] == []


def test_qa_context_reports_star_on_unknown_table_only_as_the_table() -> None:
    payload = _qa_pair_context_payload(
        SCHEMA_CONTEXT,
        QaPair(question="不明な表の全列", sql="SELECT u.* FROM ADB_APP_USER.UNKNOWN_TABLE u"),
    )

    assert payload["schema_unresolved_references"] == ["ADB_APP_USER.UNKNOWN_TABLE u"]
