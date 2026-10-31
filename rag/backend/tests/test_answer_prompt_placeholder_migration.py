"""回答の指示を保持し、回答用の placeholder だけを一度だけ改名する。"""

from app.rag.oracle_schema import oracle_schema_migration_sections
from app.rag.system_schema import SystemSchemaManager
from tests.test_system_schema_manager import _FakeDatabase


def test_placeholder_migration_is_registered_and_runs_once() -> None:
    section = next(
        s
        for s in oracle_schema_migration_sections()
        if s.name == "20261021_001_answer_prompt_placeholders"
    )
    assert "WHERE PROMPT_KEY = 'vlm_answer'" in section.sql
    assert "'{{images}}', '{{answer_context}}'" in section.sql
    assert "'{{image_metadata}}', '{{evidence_metadata}}'" in section.sql
    assert not section.destructive_note
    database = _FakeDatabase()
    manager = SystemSchemaManager(database.connection)
    manager.initialize()
    database.migrations.pop(section.name)
    database.executed.clear()
    manager.initialize()
    needle = " ".join(section.sql.upper().split())
    count = sum(needle in sql for sql in database.executed)
    assert count == 1
    manager.initialize()
    assert sum(needle in sql for sql in database.executed) == count
