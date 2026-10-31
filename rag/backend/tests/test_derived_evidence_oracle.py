"""派生の chunk の原文を読む SQL（#2641）を、実 Oracle AI Database で確かめる。

合成の文書（架空の手順書）を、原文の chunk と派生の chunk（章節の要約・項目抽出の値・RAPTOR の
要約）で保存し、派生の chunk が指す原文の chunk が検索と同じ条件で返ること、範囲の外（別の
ナレッジベース）の文書を返さないことを確かめる。未到達なら oracle_db fixture が skip し、作成行は
後始末する（AI は使わない）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.rag.chunking import Chunk
from app.rag.derived_evidence import derived_source_chunks
from app.schemas.document import FileStatus
from app.schemas.extraction import StructuredExtraction
from app.schemas.search import RetrievedChunk

_EMBEDDING = [0.1] * 1536
_SECTION = "手順書 > 第 2 章 作業の準備"


def _chunk(index: int, text: str, **metadata: str | int | float | None) -> Chunk:
    return Chunk(
        text=text, index=index, start_offset=0, end_offset=len(text), metadata=dict(metadata)
    )


def _chunks() -> list[Chunk]:
    return [
        _chunk(0, "第 1 章の本文。", section_path="手順書 > 第 1 章 目的", element_ids="p1-1"),
        _chunk(1, "準備の本文 1。", section_path=_SECTION, element_ids="p1-2"),
        _chunk(2, "準備の本文 2。", section_path=_SECTION, element_ids="p1-10,p1-11"),
        _chunk(
            3,
            "第 2 章 作業の準備: 要約の文。",
            section_path=_SECTION,
            content_kind="section_summary",
            nav_summary=True,
        ),
        _chunk(
            4,
            "項目 A: 値 A",
            content_kind="field",
            extracted_field=True,
            field_source_element_id="p1-1",
            element_ids="field-0-a",
        ),
        _chunk(
            5,
            "全体の要約。",
            content_kind="summary",
            raptor_summary=True,
            raptor_level=1,
            raptor_child_indices="0,1,2",
        ),
    ]


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
    chunk_set_id = f"cs_derived_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    chunks = _chunks()
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


def _hit(document_id: str, chunk_set_id: str, index: int) -> RetrievedChunk:
    """検索で当たった派生の chunk（保存した metadata と同じ値）。"""
    chunk = _chunks()[index]
    return RetrievedChunk(
        document_id=document_id,
        chunk_id=f"{document_id}:{chunk_set_id}:{index}",
        text=chunk.text,
        score=1.0,
        metadata={**chunk.metadata, "chunk_set_id": chunk_set_id, "chunk_index": index},
    )


@pytest.mark.usefixtures("oracle_db")
async def test_derived_sources_read_original_chunks_on_real_oracle() -> None:
    client = OracleClient()
    token = uuid4().hex[:8]
    kb = await client.create_knowledge_base(name=f"derived-{token}")
    other_kb = await client.create_knowledge_base(name=f"derived-other-{token}")
    document_id, chunk_set_id = await _indexed_document(
        client, file_name=f"derived-{token}.pdf", knowledge_base_id=kb.id
    )
    ids = [f"{document_id}:{chunk_set_id}:{index}" for index in range(6)]
    scope = {"knowledge_base_id": kb.id, "content_kind": "section_summary"}

    async def sources(index: int, filters: dict[str, str]) -> list[str]:
        found = await derived_source_chunks(
            client, _hit(document_id, chunk_set_id, index), filters=filters
        )
        return [chunk.chunk_id for chunk in found]

    # 章節の要約 → 同じ章節の原文（要約そのものは返さない）。
    assert await sources(3, scope) == [ids[1], ids[2]]
    # 項目抽出の値 → 元の要素を含む原文（p1-1 と p1-10 を取り違えない）。
    assert await sources(4, scope) == [ids[0]]
    # RAPTOR の要約 → 要約した子の原文。
    assert await sources(5, scope) == [ids[0], ids[1], ids[2]]
    # 範囲の外のナレッジベースからは読まない。
    assert await sources(3, {"knowledge_base_id": other_kb.id}) == []
    assert await sources(5, {"knowledge_base_id": other_kb.id}) == []

    rows = await client.retrieval_chunk_set_chunks(
        {"knowledge_base_id": kb.id},
        document_id=document_id,
        chunk_set_id=chunk_set_id,
        chunk_indices=[5, 1],
        limit=10,
    )
    assert [row.chunk_id for row in rows] == [ids[1], ids[5]]
    assert rows[1].metadata["raptor_summary"] is True
    # 有効でない版（別の chunk_set）は読まない。
    assert (
        await client.retrieval_chunk_set_chunks(
            {"knowledge_base_id": kb.id},
            document_id=document_id,
            chunk_set_id=f"cs_missing_{token}",
            chunk_indices=[1],
            limit=10,
        )
        == []
    )
