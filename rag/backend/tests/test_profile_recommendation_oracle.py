"""検索・回答プロファイルの推薦（#1850）が読む SQL を、実 Oracle AI Database で確かめる。

未到達なら oracle_db fixture が skip し、作成行は cleanup_to_baseline で後始末する。
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.rag.request_context import (
    current_audit_request_context,
    reset_audit_request_context,
    set_audit_request_context,
)
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from tests.test_answer_scope_oracle import _chunk, _indexed_document


@pytest.mark.usefixtures("oracle_db")
async def test_profile_targets_and_document_knowledge_bases_on_real_oracle() -> None:
    client = OracleClient()
    token = uuid4().hex[:12]
    kb_a = await client.create_knowledge_base(name=f"rec-{token}-a", description="A")
    kb_b = await client.create_knowledge_base(name=f"rec-{token}-b", description="B")
    kb_archived = await client.create_knowledge_base(name=f"rec-{token}-x", description="X")
    kb_duplicate = await client.create_knowledge_base(name=f"rec-{token}-d", description="D")
    canonical = await _indexed_document(
        client,
        file_name=f"rec-{token}-server.pdf",
        chunks=[_chunk(0, "サーバー室の点検の準備")],
        knowledge_base_ids=[kb_a.id],
    )
    other = await _indexed_document(
        client,
        file_name=f"rec-{token}-expense.pdf",
        chunks=[_chunk(0, "経費の精算の期限")],
        knowledge_base_ids=[kb_b.id, kb_archived.id],
    )
    # 取込を省いた重複（自前の索引が無い）は、正本の chunk を自分の KB の検索に使う（#1381）。
    await client.create_document(
        file_name=f"rec-{token}-server-copy.pdf",
        object_storage_path=f"local://rec-{token}-server-copy.pdf",
        content_type="application/pdf",
        duplicate_of_document_id=canonical,
        knowledge_base_ids=[kb_duplicate.id],
    )
    await client.archive_knowledge_base(kb_archived.id)
    first = await client.create_search_answer_profile(
        name=f"rec-{token}-1",
        description="点検",
        config=SearchAnswerProfileConfig(knowledge_base_ids=[kb_a.id, kb_duplicate.id]),
    )
    second = await client.create_search_answer_profile(
        name=f"rec-{token}-2",
        description="経費",
        config=SearchAnswerProfileConfig(knowledge_base_ids=[kb_b.id, kb_archived.id]),
    )

    targets = await client.search_answer_profile_targets(
        search_answer_profile_ids=[first.id, second.id]
    )
    by_id = {target.id: target for target in targets}
    assert set(by_id) == {first.id, second.id}
    assert [kb.id for kb in by_id[first.id].knowledge_bases] == [kb_a.id, kb_duplicate.id]
    # アーカイブ済みの KB は検索されないので候補の KB に入れない。
    assert [kb.id for kb in by_id[second.id].knowledge_bases] == [kb_b.id]
    assert by_id[second.id].description == "経費"
    assert await client.search_answer_profile_targets(search_answer_profile_ids=[]) == []

    every_kb = [kb_a.id, kb_b.id, kb_archived.id, kb_duplicate.id]
    assert await client.knowledge_base_ids_by_retrieved_document([canonical, other], every_kb) == {
        canonical: {kb_a.id, kb_duplicate.id},
        other: {kb_b.id},
    }
    assert await client.knowledge_base_ids_by_retrieved_document([canonical], [kb_b.id]) == {}

    # 利用者が使える KB の外は数えない（検索の範囲と同じ）。
    scoped = set_audit_request_context(
        replace(current_audit_request_context(), allowed_knowledge_base_ids=frozenset({kb_a.id}))
    )
    try:
        assert await client.knowledge_base_ids_by_retrieved_document(
            [canonical, other], every_kb
        ) == {canonical: {kb_a.id}}
        [limited] = await client.search_answer_profile_targets(search_answer_profile_ids=[first.id])
        assert [kb.id for kb in limited.knowledge_bases] == [kb_a.id]
    finally:
        reset_audit_request_context(scoped)
