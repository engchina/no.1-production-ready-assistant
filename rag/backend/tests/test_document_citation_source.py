"""根拠を ID で読む画面 API は、検索と同じ閲覧範囲を変えずに使う。"""

import pytest
from fastapi import HTTPException

from app.api.routes import documents
from app.schemas.search import RetrievedChunk
from app.security.permissions import ROUTE_PERMISSIONS


@pytest.mark.asyncio
async def test_source_preserves_original_ids_and_position(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    source = RetrievedChunk(
        document_id="document-a",
        chunk_id="document-a:set-a:7",
        text="合成の原文",
        score=0,
        metadata={"page_number": 3, "bbox": [10, 20, 30, 40], "recipe_id": "recipe-a"},
    )

    class Oracle:
        async def retrievable_chunk(self, document_id: str, chunk_id: str) -> RetrievedChunk:
            calls.append((document_id, chunk_id))
            return source

    monkeypatch.setattr(documents, "OracleClient", Oracle)
    response = await documents.get_document_citation_chunk("document-a", "document-a:set-a:7")
    assert calls == [("document-a", "document-a:set-a:7")]
    assert response.data == source


@pytest.mark.asyncio
async def test_source_does_not_substitute_a_hidden_or_obsolete_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Oracle:
        async def retrievable_chunk(self, document_id: str, chunk_id: str) -> None:
            return None

    monkeypatch.setattr(documents, "OracleClient", Oracle)
    with pytest.raises(HTTPException) as error:
        await documents.get_document_citation_chunk("document-a", "old-set:7")
    assert error.value.status_code == 404


def test_source_requires_same_document_view_permission() -> None:
    assert (
        ROUTE_PERMISSIONS[("GET", "/documents/{document_id}/chunks/{chunk_id}")]
        == ROUTE_PERMISSIONS[("GET", "/documents/{document_id}")]
    )
