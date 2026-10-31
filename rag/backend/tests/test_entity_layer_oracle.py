"""実体の層（#1362）の表と検索の SQL を、実 Oracle AI Database で確かめる。

評価セットの資料（台帳・組織規程・サンプル物流社の組織規程）を chunk として保存して実体の表を作り、
質問から台帳の行（名寄せ）と組織規程の略号の表・承認者（1 段）が SQL の join で返ることを確かめる。
未到達なら oracle_db fixture が skip し、作成行は cleanup_to_baseline で後始末する（AI は
使わない）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.clients.entity_store import EntityStore
from app.clients.oracle import OracleClient
from app.rag.chunking import Chunk
from app.rag.entity_expansion import plan_entity_expansion
from app.rag.entity_index import build_entity_index
from app.schemas.document import FileStatus
from app.schemas.extraction import StructuredExtraction
from app.schemas.search import RetrievedChunk
from tests.entity_layer_support import business_support_corpus, multi_hop_corpus

_EMBEDDING = [0.1] * 1536


async def _indexed_document(
    client: OracleClient, *, file_name: str, chunks: list[Chunk], knowledge_base_id: str
) -> tuple[str, str]:
    """検索対象（INDEXED・active な chunk_set）の文書を作り、(文書 ID, chunk_set ID) を返す。"""
    detail = await client.create_document(
        file_name=file_name,
        object_storage_path=f"local://{file_name}",
        content_type="application/pdf",
        knowledge_base_ids=[knowledge_base_id],
    )
    document_id = detail.id
    recipes = await client.list_document_recipes(document_id)
    recipe_id = str(recipes[0]["recipe_id"])
    chunk_set_id = f"cs_entity_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    await client.save_index(
        document_id,
        StructuredExtraction(raw_text="本文", confidence=0.9),
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


async def _count(client: OracleClient, sql: str, binds: dict[str, object]) -> int:
    row = await client._fetch_one(sql, binds)
    assert row is not None
    return int(str(row["n"]))


@pytest.mark.usefixtures("oracle_db")
async def test_entity_expansion_joins_on_real_oracle() -> None:
    client = OracleClient()
    store = EntityStore(client)
    token = uuid4().hex[:8]
    kb = await client.create_knowledge_base(name=f"entity-{token}")
    other_kb = await client.create_knowledge_base(name=f"entity-other-{token}")
    corpus = {document.file_name: document for document in multi_hop_corpus()}
    saved: dict[str, tuple[str, str]] = {}
    for name, knowledge_base_id in (
        ("system-ledger.xlsx", kb.id),
        ("organization-rules.pdf", kb.id),
        ("logistics-organization-rules.pdf", kb.id),
        ("approval-rules.pdf", kb.id),
        # 別のナレッジベースの台帳（範囲の外）。
        ("logistics-system-ledger.xlsx", other_kb.id),
    ):
        document = corpus[name]
        document_id, chunk_set_id = await _indexed_document(
            client,
            file_name=f"entity-{token}-{name}",
            chunks=document.chunks,
            knowledge_base_id=knowledge_base_id,
        )
        index = build_entity_index(
            document_id=document_id,
            chunks=document.chunks,
            chunk_set_id=chunk_set_id,
            document_title=document.chunks[0].text,
        )
        await store.replace_chunk_set_entity_index(document_id, chunk_set_id, index)
        saved[name] = (document_id, chunk_set_id)

    ledger_id, ledger_cs = saved["system-ledger.xlsx"]
    org_id, org_cs = saved["organization-rules.pdf"]
    plan = await plan_entity_expansion(
        store,
        {"knowledge_base_id": kb.id},
        question="経費精算ポータルの変更の申請は、誰が承認しますか？",
        seed_chunks=[],
        exclude_chunk_ids=set(),
        max_chunks=6,
    )

    ids = [item.chunk.chunk_id for item in plan]
    assert ids[:3] == [f"{ledger_id}:{ledger_cs}:1", f"{org_id}:{org_cs}:2", f"{org_id}:{org_cs}:3"]
    assert plan[1].info["match"] == "経"
    assert plan[1].info["from_chunk_id"] == f"{ledger_id}:{ledger_cs}:1"
    assert "略号「経」: 経理部" in plan[1].chunk.text
    # サンプル物流社の組織規程（同じ略号「経」）と、範囲の外の台帳は足さない。
    other_ids = {
        saved["logistics-organization-rules.pdf"][0],
        saved["logistics-system-ledger.xlsx"][0],
    }
    assert not {item.chunk.document_id for item in plan} & other_ids
    # 別のナレッジベースの範囲では、その範囲の台帳（物流社の「経費精算システム」）だけを使い、
    # 範囲の外のサンプル社の台帳の行・組織規程は名寄せの途中にも使わない。
    other_plan = await plan_entity_expansion(
        store,
        {"knowledge_base_id": other_kb.id},
        question="経費精算ポータルの承認者は？",
        seed_chunks=[],
        exclude_chunk_ids=set(),
        max_chunks=6,
    )
    assert {item.chunk.document_id for item in other_plan} == {
        saved["logistics-system-ledger.xlsx"][0]
    }
    # 検索・回答プロファイルの案内（#1388）: 実体の索引を持つ文書を、ナレッジベースの範囲で数える。
    # 承認規程（approval-rules.pdf）は実体を持たない（台帳・組織規程の 3 文書だけ）。
    assert await store.count_entity_documents([kb.id]) == 3
    assert await store.count_entity_documents([other_kb.id]) == 1
    assert await store.count_entity_documents([kb.id, other_kb.id]) == 4

    # 再索引（chunk の作り直し）で関連が消え、文書の削除で実体と別名も消える（ON DELETE CASCADE）。
    links_sql = (
        "SELECT COUNT(*) AS n FROM rag_entity_chunks ec "
        "JOIN rag_entities e ON e.entity_id = ec.entity_id WHERE e.chunk_set_id = :cs"
    )
    assert await _count(client, links_sql, {"cs": org_cs}) > 0
    await client.save_index(
        org_id,
        StructuredExtraction(raw_text="本文", confidence=0.9),
        corpus["organization-rules.pdf"].chunks,
        [_EMBEDDING] * len(corpus["organization-rules.pdf"].chunks),
        chunk_set_id=org_cs,
    )
    assert await _count(client, links_sql, {"cs": org_cs}) == 0
    # chunk_set の行を作る前に chunk を保存する取込の経路でも保存できる（chunk_set に FK が無い）。
    early_cs = f"cs_entity_early_{uuid4().hex[:12]}"
    early_chunks = corpus["organization-rules.pdf"].chunks
    await client.save_index(
        org_id,
        StructuredExtraction(raw_text="本文", confidence=0.9),
        early_chunks,
        [_EMBEDDING] * len(early_chunks),
        chunk_set_id=early_cs,
    )
    await store.replace_chunk_set_entity_index(
        org_id,
        early_cs,
        build_entity_index(document_id=org_id, chunks=early_chunks, chunk_set_id=early_cs),
    )
    # 関連の消えた古い chunk_set の実体は、同じ文書の次の保存で消える。
    assert (
        await _count(
            client,
            "SELECT COUNT(*) AS n FROM rag_entities WHERE chunk_set_id = :cs",
            {"cs": org_cs},
        )
        == 0
    )
    assert await _count(client, links_sql, {"cs": early_cs}) > 0
    entities_sql = "SELECT COUNT(*) AS n FROM rag_entities WHERE document_id = :d"
    aliases_sql = (
        "SELECT COUNT(*) AS n FROM rag_entity_aliases a "
        "JOIN rag_entities e ON e.entity_id = a.entity_id WHERE e.document_id = :d"
    )
    assert await _count(client, entities_sql, {"d": ledger_id}) > 0
    assert await client.delete_document(ledger_id)
    assert await _count(client, entities_sql, {"d": ledger_id}) == 0
    assert await _count(client, aliases_sql, {"d": ledger_id}) == 0


@pytest.mark.usefixtures("oracle_db")
async def test_entity_labels_need_another_document_on_real_oracle() -> None:
    """本文の見出しのラベル（「原因 A」）は拡張に使わず、台帳と同じ「重要度 C」は使う（#1393）。"""
    client = OracleClient()
    store = EntityStore(client)
    token = uuid4().hex[:8]
    kb = await client.create_knowledge_base(name=f"entity-label-{token}")
    corpus = {
        document.file_name: document
        for document in [*multi_hop_corpus(), *business_support_corpus()]
    }
    saved: dict[str, tuple[str, str]] = {}
    for name in ("system-ledger.xlsx", "incident-contact-rules.pdf", "error-e1023-notes.pdf"):
        document = corpus[name]
        document_id, chunk_set_id = await _indexed_document(
            client,
            file_name=f"entity-label-{token}-{name}",
            chunks=document.chunks,
            knowledge_base_id=kb.id,
        )
        await store.replace_chunk_set_entity_index(
            document_id,
            chunk_set_id,
            build_entity_index(
                document_id=document_id,
                chunks=document.chunks,
                chunk_set_id=chunk_set_id,
                document_title=document.chunks[0].text,
            ),
        )
        saved[name] = (document_id, chunk_set_id)

    # 障害メモの「対処」の章（「原因 A の場合: …」）が上位に入っても、「原因 A」で章を足さない。
    notes_id, notes_cs = saved["error-e1023-notes.pdf"]
    remedy = RetrievedChunk(
        document_id=notes_id,
        chunk_id=f"{notes_id}:{notes_cs}:3",
        text=corpus["error-e1023-notes.pdf"].chunks[3].text,
        score=0.0,
        file_name="error-e1023-notes.pdf",
    )
    unrelated = await plan_entity_expansion(
        store,
        {"knowledge_base_id": kb.id},
        question="アカウントを削除するにはどうしますか？",
        seed_chunks=[remedy],
        exclude_chunk_ids={remedy.chunk_id},
        max_chunks=6,
    )
    assert unrelated == []

    # 台帳の「重要度: C」と同じ別名の、障害連絡規程の「重要度 C: …」は名寄せで足す。
    incident_id, incident_cs = saved["incident-contact-rules.pdf"]
    named = await plan_entity_expansion(
        store,
        {"knowledge_base_id": kb.id},
        question="重要度 C のシステムで障害が起きたら、いつまでにどこへ報告しますか？",
        seed_chunks=[],
        exclude_chunk_ids=set(),
        max_chunks=6,
    )
    assert [item.chunk.chunk_id for item in named] == [f"{incident_id}:{incident_cs}:1"]
