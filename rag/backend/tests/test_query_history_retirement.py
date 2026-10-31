"""質問候補専用の保存・API・表を退役し、通常の回答記録を維持する。"""

from app.main import app
from app.rag.oracle_schema import (
    oracle_schema_migration_sections,
    oracle_schema_sql,
    retire_query_history_sql,
    split_sql_statements,
)
from tests import _oracle_test_db


def test_retired_question_candidate_routes_are_absent() -> None:
    paths = app.openapi()["paths"]
    assert "/api/settings/query-history" not in paths
    assert "/api/search-answer-profiles/{search_answer_profile_id}/query-suggestions" not in paths
    assert "/api/chat/conversations" in paths
    assert "/api/search/answers/{trace_id}" in paths


def test_retirement_is_explicit_and_does_not_create_the_table_again() -> None:
    assert "CREATE TABLE rag_query_history" not in oracle_schema_sql()
    section = next(
        item
        for item in oracle_schema_migration_sections()
        if item.name == "20261029_001_retire_query_history"
    )
    assert section.destructive
    assert "RAG_QUERY_HISTORY" in section.destructive_note
    sql = section.sql.upper()
    assert "DROP TABLE RAG_QUERY_HISTORY PURGE" in sql
    assert "CASCADE" not in sql
    assert "SQLCODE != -942" in sql


def test_retirement_removes_only_the_candidate_table_on_real_oracle(
    oracle_schema_locked: None,
) -> None:
    statement = split_sql_statements(retire_query_history_sql())[0]
    with _oracle_test_db._schema_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'RAG_QUERY_HISTORY'")
        assert cursor.fetchone()[0] == 0
        preserved_tables = (
            "RAG_CONVERSATIONS",
            "RAG_MESSAGES",
            "RAG_ANSWER_RECORDS",
            "RAG_RUN_HISTORY",
        )
        before_counts = {}
        for table in preserved_tables:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")  # 固定の表の一覧だけを使う。
            before_counts[table] = cursor.fetchone()[0]
        cursor.execute("CREATE TABLE RAG_QUERY_HISTORY (query_id NUMBER PRIMARY KEY)")
        try:
            cursor.execute("INSERT INTO RAG_QUERY_HISTORY (query_id) VALUES (1)")
            connection.commit()
            cursor.execute("SELECT COUNT(*) FROM RAG_QUERY_HISTORY")
            assert cursor.fetchone()[0] == 1
            cursor.execute(statement)
            cursor.execute(statement)
            cursor.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name = 'RAG_QUERY_HISTORY'"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name IN "
                "('RAG_CONVERSATIONS', 'RAG_MESSAGES', 'RAG_ANSWER_RECORDS', 'RAG_RUN_HISTORY')"
            )
            assert cursor.fetchone()[0] == 4
            for table in preserved_tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")  # 固定の表の一覧だけを使う。
                assert cursor.fetchone()[0] == before_counts[table]
        finally:
            cursor.execute(statement)
