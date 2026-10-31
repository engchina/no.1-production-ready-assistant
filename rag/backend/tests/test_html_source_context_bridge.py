"""取込 metadata が backend の記録変換を経て実際の生成入力へ届くこと。"""

import json

import pytest
from rag_engine.generation.answer_models import AnswerContext
from rag_engine.generation.answer_records import _stored_chunk_answer_record
from rag_engine.generation.answering import _grounded_prompt, _grounded_spans

from app.rag.answer_engine import _stored_child
from app.schemas.search import RetrievedChunk


@pytest.mark.parametrize(
    "engine_metadata", [None, "not-json", json.dumps({"schema_version": 4, "section_path": ["節"]})]
)
@pytest.mark.parametrize("region", ["header", "footer", "caption"])
def test_stored_html_structure_reaches_generation_input(
    engine_metadata: str | None, region: str
) -> None:
    text = "合成の領域の原文です。"
    chunk = RetrievedChunk(
        document_id="synthetic-doc",
        chunk_id="synthetic-chunk",
        text=text,
        score=1.0,
        file_name="synthetic.html",
        metadata={
            "engine_metadata_json": engine_metadata,
            "html_region_role": region,
            "table_caption": "合成の表題",
            "section_path": "上位の節",
            "chunk_strategy": "small_to_big",
        },
    )
    stored = _stored_child(chunk, rrf_score=1.0)
    record = _stored_chunk_answer_record(stored)
    context = AnswerContext(records=[record], text=text)
    question = "原文の領域と表題を教えてください。"
    spans = _grounded_spans(question, context, (), None)
    prompt = _grounded_prompt(question, spans, context, (), None, None, None, None, None)
    assert f'"role": "{region}"' in prompt
    assert '"table_caption": "合成の表題"' in prompt
    assert spans[0]["text"] == text
    assert chunk.text == text
    assert stored.metadata["html_region_role"] == region
