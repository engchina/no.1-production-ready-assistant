"""前後の文脈（同じ chunk group の sibling）を読む SQL（#1490・#3461）を実 Oracle で確かめる。

合成の文書（架空の手順書）を chunk group つきの chunk で保存し、起点ごとの sibling（同じ
文書・chunk_set・group の chunk、自分を除く、起点に近い順、上限まで）が返ること、同じ group の
id を持つ別の文書の chunk を返さないことを確かめる。未到達なら oracle_db fixture が skip し、
作成行は後始末する（AI は使わない）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.rag.chunking import Chunk
from app.schemas.document import FileStatus
from app.schemas.extraction import StructuredExtraction
from app.schemas.search import RetrievedChunk

_EMBEDDING = [0.1] * 1024
# chunk の位置 → chunk group(0〜4 は表 A、5〜7 は表 B)。
_GROUPS = ["grp-a"] * 5 + ["grp-b"] * 3


async def _indexed_document(
    client: OracleClient, *, file_name: str, knowledge_base_id: str
) -> tuple[str, str]:
    """検索対象（INDEXED・active な chunk_set）の文書を作り、(文書 ID, chunk_set ID)。"""
    detail = await client.create_document(
        file_name=file_name,
        object_storage_path=f"local://{file_name}",
        content_type="application/pdf",
        knowledge_base_ids=[knowledge_base_id],
    )
    document_id = detail.id
    recipes = await client.list_document_recipes(document_id)
    recipe_id = str(recipes[0]["recipe_id"])
    chunk_set_id = f"cs_ctx_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    chunks = [
        Chunk(
            text=f"表の行 {index} の本文。",
            index=index,
            start_offset=0,
            end_offset=10,
            metadata={"chunk_group_id": group},
        )
        for index, group in enumerate(_GROUPS)
    ]
    extraction = StructuredExtraction(raw_text="本文", confidence=0.9, document_type="手順書")
    await client.save_index(
        document_id,
        extraction,
        chunks,
        [_EMBEDDING] * len(chunks),
        chunk_set_id=chunk_set_id,
    )
    await client.mark_chunk_set_indexed(
        chunk_set_id=chunk_set_id, chunk_count=len(chunks), vector_count=len(chunks)
    )
    await client.activate_recipe_chunk_set(
        recipe_id=recipe_id,
        chunk_set_id=chunk_set_id,
        materialized_revision=int(str(recipes[0].get("config_revision") or 1)),
    )
    await client.update_document_status(document_id, FileStatus.INDEXED)
    return document_id, chunk_set_id


def _anchor(document_id: str, chunk_set_id: str, index: int, strategy: str) -> RetrievedChunk:
    return RetrievedChunk(
        document_id=document_id,
        chunk_id=f"{document_id}:{chunk_set_id}:{index}",
        text=f"表の行 {index} の本文。",
        score=0.5,
        metadata={
            "chunk_group_id": _GROUPS[index],
            "chunk_strategy": strategy,
            "chunk_index": index,
            "chunk_set_id": chunk_set_id,
        },
    )


@pytest.mark.usefixtures("oracle_db")
@pytest.mark.parametrize("strategy", ["small_to_big", "fixed_length"])
async def test_context_group_siblings_on_real_oracle(strategy: str) -> None:
    client = OracleClient()
    token = uuid4().hex[:8]
    kb = await client.create_knowledge_base(name=f"context-{token}")
    first, first_set = await _indexed_document(
        client, file_name=f"context-a-{token}.pdf", knowledge_base_id=kb.id
    )
    # 同じ group の id を持つ別の文書(前後の文脈に入らない)。
    other, other_set = await _indexed_document(
        client, file_name=f"context-b-{token}.pdf", knowledge_base_id=kb.id
    )
    anchors = [
        _anchor(first, first_set, 2, strategy),
        _anchor(first, first_set, 6, strategy),
        _anchor(other, other_set, 0, strategy),
        _anchor(first, first_set, 4, strategy),
    ]

    siblings = await client.context_group_siblings(anchors, max_chunks_per_group=2)

    def ids(document_id: str, chunk_set_id: str, *indexes: int) -> list[str]:
        return [f"{document_id}:{chunk_set_id}:{index}" for index in indexes]

    expected = [
        ids(first, first_set, 1, 3),
        ids(first, first_set, 5, 7),
        ids(other, other_set, 1, 2) if strategy == "small_to_big" else ids(other, other_set, 1),
        ids(first, first_set, 3, 2) if strategy == "small_to_big" else ids(first, first_set, 3, 5),
    ]
    assert [chunk.chunk_id for chunk in siblings] == [
        chunk_id for group in expected for chunk_id in group
    ]
    assert [chunk.metadata["context_anchor_chunk_id"] for chunk in siblings] == [
        anchor.chunk_id for anchor, group in zip(anchors, expected, strict=True) for _ in group
    ]
    assert all(
        chunk.metadata["context_group_purpose"]
        == ("parent_source_mapping" if strategy == "small_to_big" else "neighbor_context")
        for chunk in siblings
    )

    if strategy == "fixed_length":
        expanded = await client.context_group_siblings(
            [anchors[-1]], max_chunks_per_group=2, neighbor_child_count=3
        )
        assert [chunk.chunk_id for chunk in expanded] == ids(first, first_set, 3, 5, 2, 6, 1, 7)
