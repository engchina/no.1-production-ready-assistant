"""保存済み評価入力の引用キー移行を実 Oracle で検証する。"""

from typing import Any
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient, _execute
from app.rag.oracle_schema import cited_evidence_migration_sql, split_sql_statements


@pytest.mark.usefixtures("oracle_db")
async def test_cited_evidence_migration_preserves_order_and_image_metadata() -> None:
    oracle = OracleClient()
    trace_id = f"pytest-cited-{uuid4().hex}"
    payload = {
        "answer_text": "合成の回答",
        "used_images": [
            {"image_id": "child-text", "answer_use": "confirmed"},
            {"image_id": "picture-1", "answer_use": "reference"},
        ],
        "evidence_items": [{"metadata": {"image_evidence": [{"image_id": "picture-1"}]}}],
    }
    try:
        await oracle.save_answer_record(
            {
                "trace_id": trace_id,
                "surface": "search",
                "answer_engine": "grounded",
                "question": "合成の質問",
                "answer": "合成の回答",
                "citations": [],
                "diagnostics": {},
                "evaluation_input": payload,
            }
        )

        def migrate(connection: Any) -> None:
            for statement in split_sql_statements(cited_evidence_migration_sql()):
                _execute(connection, statement, {})

        await oracle._run_transaction(migrate)
        row = await oracle.get_answer_record(trace_id)
        assert row is not None
        result = row["evaluation_input_json"]
        assert isinstance(result, dict)
        assert result["cited_evidence"] == [
            {"evidence_id": "child-text", "answer_use": "confirmed"},
            {"evidence_id": "picture-1", "answer_use": "reference"},
        ]
        assert result["evidence_items"] == payload["evidence_items"]
        assert "used_images" not in result
        await oracle._run_transaction(migrate)
        again = await oracle.get_answer_record(trace_id)
        assert again is not None and again["evaluation_input_json"] == result
    finally:
        await oracle._run_transaction(
            lambda connection: _execute(
                connection,
                "DELETE FROM rag_answer_records WHERE trace_id = :trace_id",
                {"trace_id": trace_id},
            )
        )
