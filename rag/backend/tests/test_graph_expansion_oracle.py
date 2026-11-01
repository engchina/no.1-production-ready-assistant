"""関係情報で検索の候補を足す SQL（#2055）を、実 Oracle AI Database で確かめる。

合成の文書（架空の手順書）を chunk として保存し、取込と同じ ``build_graph_index`` で関係情報の表を
作り、当たった chunk と同じ狭い章節の chunk・質問に見出しの語が含まれる章節の chunk が SQL の join
で返ること、範囲の外（別のナレッジベース）の文書を返さないことを確かめる。未到達なら oracle_db
fixture が skip し、作成行は後始末する（AI は使わない）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.clients.oracle_graph_store import OracleGraphStore
from app.rag.chunking import Chunk
from app.rag.graph_expansion import GRAPH_KIND_NAME, GRAPH_KIND_NEIGHBOR, plan_graph_expansion
from app.rag.graph_index import GraphIndex, build_graph_index
from app.schemas.document import FileStatus
from app.schemas.extraction import StructuredExtraction
from app.schemas.search import RetrievedChunk

_EMBEDDING = [0.1] * 1024


def _chunks(sections: list[tuple[str, int]]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path, count in sections:
        for part in range(count):
            text = f"{path} の本文 {part + 1}"
            chunks.append(
                Chunk(
                    text=text,
                    index=len(chunks),
                    start_offset=0,
                    end_offset=len(text),
                    metadata={"section_path": path},
                )
            )
    return chunks


async def _indexed_document(
    client: OracleClient,
    *,
    file_name: str,
    chunks: list[Chunk],
    knowledge_base_id: str,
    activate: bool = True,
) -> tuple[str, str]:
    """文書と関係情報を作り、文書 ID と chunk_set ID を返す。activate=False は切替前。"""
    detail = await client.create_document(
        file_name=file_name,
        object_storage_path=f"local://{file_name}",
        content_type="application/pdf",
        knowledge_base_ids=[knowledge_base_id],
    )
    document_id = detail.id
    recipes = await client.list_document_recipes(document_id)
    recipe_id = str(recipes[0]["recipe_id"])
    chunk_set_id = f"cs_graph_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
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
    if activate:
        await client.activate_recipe_chunk_set(
            recipe_id=recipe_id,
            chunk_set_id=chunk_set_id,
            materialized_revision=int(str(recipes[0].get("config_revision") or 1)),
        )
    await client.update_document_status(document_id, FileStatus.INDEXED)
    graph = build_graph_index(
        document_id=document_id,
        knowledge_base_ids=[knowledge_base_id],
        extraction=extraction,
        chunks=chunks,
        chunk_set_id=chunk_set_id,
        document_name=file_name,
    )
    await client.replace_document_graph_index(document_id, graph, chunk_set_id=chunk_set_id)
    return document_id, chunk_set_id


@pytest.mark.usefixtures("oracle_db")
async def test_graph_expansion_joins_on_real_oracle() -> None:
    client = OracleClient()
    store = OracleGraphStore(client)
    token = uuid4().hex[:8]
    kb = await client.create_knowledge_base(name=f"graph-{token}")
    other_kb = await client.create_knowledge_base(name=f"graph-other-{token}")
    sections = [
        ("手順書 > 第 1 章 目的", 1),
        ("手順書 > 第 2 章 作業の準備", 3),
        ("手順書 > 第 3 章 作業の記録の保管", 2),
    ]
    saved = [
        await _indexed_document(
            client,
            file_name=f"graph-{token}-a.pdf",
            chunks=_chunks(sections),
            knowledge_base_id=kb.id,
        ),
        # 別のナレッジベースの同じ見出しの文書（範囲の外）。
        await _indexed_document(
            client,
            file_name=f"graph-{token}-b.pdf",
            chunks=_chunks(sections),
            knowledge_base_id=other_kb.id,
        ),
    ]
    try:
        document_id, chunk_set_id = saved[0]
        ids = [f"{document_id}:{chunk_set_id}:{index}" for index in range(6)]
        seed_chunk = RetrievedChunk(
            document_id=document_id,
            chunk_id=ids[2],
            text="手順書 > 第 2 章 作業の準備 の本文 2",
            score=1.0,
            metadata={"chunk_index": 2},
        )

        plan = await plan_graph_expansion(
            store,
            {"knowledge_base_id": kb.id},
            question="作業の記録の保管の期間は？",
            seed_chunks=[seed_chunk],
            exclude_chunk_ids={ids[2]},
            max_chunks=6,
        )

        # 名前の一致（第 3 章）と、当たった chunk と同じ第 2 章の chunk（近い順）。
        assert [item.chunk.chunk_id for item in plan] == [ids[4], ids[5], ids[1], ids[3]]
        assert [item.info["kind"] for item in plan] == [GRAPH_KIND_NAME] * 2 + [
            GRAPH_KIND_NEIGHBOR
        ] * 2
        assert plan[0].chunk.text == "手順書 > 第 3 章 作業の記録の保管 の本文 1"
        # 範囲の外のナレッジベースの文書は返さない。
        assert {item.chunk.document_id for item in plan} == {document_id}
    finally:
        for document_id, chunk_set_id in saved:
            await client.replace_document_graph_index(
                document_id, GraphIndex(), chunk_set_id=chunk_set_id
            )


@pytest.mark.usefixtures("oracle_db")
async def test_subgraph_hides_pending_graph_until_activation() -> None:
    """グラフを保存しても有効化までは表示せず、切替後に node と edge を表示する。"""
    client = OracleClient()
    token = uuid4().hex[:8]
    kb = await client.create_knowledge_base(name=f"graph-activation-{token}")
    document_id, chunk_set_id = await _indexed_document(
        client,
        file_name=f"graph-activation-{token}.pdf",
        chunks=_chunks([("section-a", 1)]),
        knowledge_base_id=kb.id,
        activate=False,
    )
    try:
        nodes, edges = await client.fetch_knowledge_base_subgraph(kb.id, limit=100)
        assert nodes == []
        assert edges == []

        recipes = await client.list_document_recipes(document_id)
        await client.activate_recipe_chunk_set(
            recipe_id=str(recipes[0]["recipe_id"]), chunk_set_id=chunk_set_id
        )
        nodes, edges = await client.fetch_knowledge_base_subgraph(kb.id, limit=100)
        assert nodes
        assert edges
        node_ids = {node["id"] for node in nodes}
        assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in edges)
    finally:
        await client.delete_document(document_id)
