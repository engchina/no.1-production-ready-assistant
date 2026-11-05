"""確認の項目の規則へ渡す backend の材料（#1492）。

旧版の文書の印（document_superseded）を rag_engine の根拠へ渡す。LLM・モデルの API へは接続しない。
"""

from __future__ import annotations

from app.rag.answer_engine import _stored_child
from app.schemas.search import RetrievedChunk


def _chunk(*, superseded: bool) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id="chunk-1",
        text="手順",
        score=0.5,
        file_name="manual-v1.pdf",
        metadata={"document_superseded": True} if superseded else {},
    )


def test_stored_child_marks_superseded_documents_for_the_engine() -> None:
    superseded = _stored_child(_chunk(superseded=True), rrf_score=0.1).metadata
    current = _stored_child(_chunk(superseded=False), rrf_score=0.1).metadata
    assert superseded["document_superseded"] is True
    assert "document_superseded" not in current
