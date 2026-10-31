"""RAPTOR の独自指示を失わず一度だけ統合する。"""

from app.rag.oracle_schema import oracle_schema_migration_sections
from app.rag.system_schema import SystemSchemaManager
from tests.test_system_schema_manager import _FakeDatabase


def test_raptor_prompt_migration_keeps_custom_content_and_runs_once() -> None:
    section = next(
        s
        for s in oracle_schema_migration_sections()
        if s.name == "20261027_001_raptor_prompt_consolidation"
    )
    assert "COALESCE" in section.sql
    assert "WHEN MATCHED THEN UPDATE SET target.content = source.content" in section.sql
    assert "raptor_summary_archived" in section.sql
    assert "DELETE" not in section.sql.upper()
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.migrations.pop(section.name)
    database.executed.clear()
    manager.initialize()
    needle = "MERGE INTO RAG_ANSWER_PROMPTS TARGET"
    count = sum(needle in sql for sql in database.executed)
    assert count == 1
    manager.initialize()
    assert sum(needle in sql for sql in database.executed) == count
