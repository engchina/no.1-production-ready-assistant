"""独立した合成の表で RAPTOR 指示の統合を実 Oracle に照合する。"""

from typing import Any
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient, _execute, _fetch_all
from app.rag.oracle_schema import raptor_prompt_consolidation_sql, split_sql_statements


@pytest.mark.usefixtures("oracle_db")
@pytest.mark.parametrize(
    "system,instruction",
    [
        ("独自の共通指示", "独自の階層指示"),
        ("独自の共通指示", None),
        (None, "独自の階層指示"),
        (None, None),
    ],
)
async def test_saved_raptor_instructions_are_merged_without_losing_either(
    system: str | None, instruction: str | None
) -> None:
    oracle = OracleClient()
    table = "RAG_PROMPT_TEST_" + uuid4().hex[:16].upper()

    def verify(connection: Any) -> None:
        _execute(
            connection,
            f"CREATE TABLE {table} (prompt_key VARCHAR2(64) PRIMARY KEY, "
            "content CLOB, updated_at TIMESTAMP)",
            {},
        )
        try:
            for key, content in (
                ("raptor_summary_system", system),
                ("raptor_summary", instruction),
            ):
                if content is not None:
                    _execute(
                        connection,
                        f"INSERT INTO {table} (prompt_key, content) VALUES (:key, :content)",
                        {"key": key, "content": content},
                    )
            for statement in split_sql_statements(
                raptor_prompt_consolidation_sql().replace("rag_answer_prompts", table)
            ):
                _execute(connection, statement, {})
            rows = {
                str(row["prompt_key"]): str(row["content"])
                for row in _fetch_all(connection, f"SELECT prompt_key, content FROM {table}", {})
            }
            assert "raptor_summary" not in rows
            if system is None and instruction is None:
                assert rows == {}
            else:
                merged = rows["raptor_summary_system"]
                assert (system or "あなたは文書を階層的に要約するアシスタントです。") in merged
                assert (
                    instruction or "次のテキスト群の要点を簡潔な日本語で要約してください。"
                ) in merged
                if instruction is not None:
                    assert rows["raptor_summary_archived"] == instruction
        finally:
            _execute(connection, f"DROP TABLE {table} PURGE", {})

    await oracle._run_transaction(verify)
