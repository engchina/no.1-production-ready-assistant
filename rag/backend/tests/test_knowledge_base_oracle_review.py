"""ナレッジベースの Oracle 操作の回帰テスト（#282）。

- 名前の重複を 409 用の例外にする（作成・改名・同時作成の一意制約違反）。
- アーカイブ済み KB でも所属文書の一覧に文書を返す（検索だけが ACTIVE で絞る）。
- 最後の所属を外すと DEFAULT へ移し、未所属の文書を作らない。
"""

import pytest

from app.clients.oracle import (
    KnowledgeBaseNameConflictError,
    OracleClient,
    _knowledge_base_name_conflict_guard,
    _oracle_document_where,
    _oracle_retrieval_where,
)
from tests.test_oracle_adapter import (
    FakeOraclePool,
    _oci_settings,
    _oracle_document_row,
    _oracle_knowledge_base_row,
    _run_inline,
)


def _knowledge_base_row(knowledge_base_id: str, name: str) -> dict[str, object]:
    row = _oracle_knowledge_base_row(name=name)
    row["knowledge_base_id"] = knowledge_base_id
    return row


async def test_create_knowledge_base_rejects_duplicate_name() -> None:
    """同じ名前（大文字小文字を区別しない）の KB があると INSERT せず 409 用の例外にする。"""
    pool = FakeOraclePool(execute_results=[[_knowledge_base_row("kb-1", "社内規程")]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(KnowledgeBaseNameConflictError, match="同じ名前"):
        await client.create_knowledge_base(name="社内規程")

    lookup = pool.connection.calls[0]
    assert "LOWER(name) = :knowledge_base_name" in lookup.statement
    assert lookup.parameters["knowledge_base_name"] == "社内規程"
    assert not any(
        "INSERT INTO rag_knowledge_bases" in call.statement for call in pool.connection.calls
    )
    assert pool.connection.commits == 0


async def test_rename_knowledge_base_rejects_name_of_another_knowledge_base() -> None:
    """改名先が別の KB の名前なら UPDATE しない。"""
    pool = FakeOraclePool(
        execute_results=[
            [_knowledge_base_row("kb-1", "社内規程")],
            [_knowledge_base_row("kb-2", "設計資料")],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(KnowledgeBaseNameConflictError):
        await client.update_knowledge_base("kb-1", name="設計資料", update_fields={"name"})

    assert not any("UPDATE rag_knowledge_bases" in call.statement for call in pool.connection.calls)


async def test_rename_knowledge_base_allows_case_change_of_own_name() -> None:
    """自分の名前の大文字小文字だけを変える改名は重複扱いしない（名前の検索もしない）。"""
    pool = FakeOraclePool(execute_results=[[_knowledge_base_row("kb-1", "faq")]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.update_knowledge_base("kb-1", name="FAQ", update_fields={"name"})

    assert updated.name == "FAQ"
    assert any("UPDATE rag_knowledge_bases" in call.statement for call in pool.connection.calls)


def test_unique_constraint_violation_is_converted_to_name_conflict() -> None:
    """同時作成で一意制約違反（ORA-00001）になったときも 409 用の例外へ読み替える。"""
    with pytest.raises(KnowledgeBaseNameConflictError), _knowledge_base_name_conflict_guard():
        raise RuntimeError(
            "ORA-00001: unique constraint (RAG.RAG_KNOWLEDGE_BASES_TENANT_NAME_UIDX) violated"
        )

    with pytest.raises(RuntimeError, match="ORA-12541"), _knowledge_base_name_conflict_guard():
        raise RuntimeError("ORA-12541: TNS:no listener")


def test_document_list_by_knowledge_base_keeps_archived_membership() -> None:
    """所属文書の一覧（KB での絞り込み）は KB の状態で絞らない。検索だけが ACTIVE で絞る。"""
    where_sql, binds = _oracle_document_where(knowledge_base_id="kb-archived")

    assert "rag_document_knowledge_bases dkb" in where_sql
    assert "kb.status = 'ACTIVE'" not in where_sql
    assert binds["filter_knowledge_base_id_0"] == "kb-archived"

    retrieval_sql, _retrieval_binds = _oracle_retrieval_where({"knowledge_base_id": "kb-archived"})
    assert "kb.status = 'ACTIVE'" in retrieval_sql


async def test_remove_last_membership_moves_document_to_default() -> None:
    """最後の所属を外すと DEFAULT へ所属させてから外す（未所属の文書を作らない）。"""
    pool = FakeOraclePool(
        execute_results=[
            [_knowledge_base_row("kb-1", "社内規程")],
            [_oracle_document_row()],
            [{"knowledge_base_id": "kb-1"}],
            [_knowledge_base_row("kb-default", "DEFAULT")],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.remove_document_from_knowledge_base("kb-1", "doc-1")

    assert detail.id == "kb-1"
    membership_lookup = pool.connection.calls[2]
    assert "FROM rag_document_knowledge_bases" in membership_lookup.statement
    assert "FOR UPDATE" in membership_lookup.statement
    assert len(pool.connection.many_calls) == 1
    inserted = pool.connection.many_calls[0]
    assert "INSERT INTO rag_document_knowledge_bases" in inserted.statement
    assert inserted.rows[0]["knowledge_base_id"] == "kb-default"
    assert inserted.rows[0]["document_id"] == "doc-1"
    delete = pool.connection.calls[-1]
    assert "DELETE FROM rag_document_knowledge_bases" in delete.statement
    assert delete.parameters["knowledge_base_id"] == "kb-1"
    assert pool.connection.commits == 1


async def test_remove_membership_with_other_memberships_only_deletes() -> None:
    """ほかの KB にも所属していれば、所属の行を消すだけにする。"""
    pool = FakeOraclePool(
        execute_results=[
            [_knowledge_base_row("kb-1", "社内規程")],
            [_oracle_document_row()],
            [{"knowledge_base_id": "kb-1"}, {"knowledge_base_id": "kb-2"}],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    await client.remove_document_from_knowledge_base("kb-1", "doc-1")

    assert not pool.connection.many_calls
    assert "DELETE FROM rag_document_knowledge_bases" in pool.connection.calls[-1].statement


async def test_remove_default_only_membership_is_rejected() -> None:
    """DEFAULT にだけ所属する文書は外せない（外すと未所属になる）。"""
    pool = FakeOraclePool(
        execute_results=[
            [_knowledge_base_row("kb-default", "DEFAULT")],
            [_oracle_document_row()],
            [{"knowledge_base_id": "kb-default"}],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(ValueError, match="DEFAULT にだけ所属する文書は外せません"):
        await client.remove_document_from_knowledge_base("kb-default", "doc-1")

    assert not pool.connection.many_calls
    assert not any(
        "DELETE FROM rag_document_knowledge_bases" in call.statement
        for call in pool.connection.calls
    )
    assert pool.connection.commits == 0


def _archived_knowledge_base_row(knowledge_base_id: str, name: str) -> dict[str, object]:
    row = _oracle_knowledge_base_row(name=name, status="ARCHIVED")
    row["knowledge_base_id"] = knowledge_base_id
    return row


async def test_remove_membership_from_archived_knowledge_base_is_rejected() -> None:
    """アーカイブ済みの KB からは所属を外さない（追加と同じく 409 用の例外。#961）。"""
    pool = FakeOraclePool(execute_results=[[_archived_knowledge_base_row("kb-old", "旧規程")]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(ValueError, match="アーカイブ済み"):
        await client.remove_document_from_knowledge_base("kb-old", "doc-1")

    assert not pool.connection.many_calls
    assert not any(
        "DELETE FROM rag_document_knowledge_bases" in call.statement
        for call in pool.connection.calls
    )
    assert pool.connection.commits == 0


async def test_update_archived_knowledge_base_is_rejected() -> None:
    """アーカイブ済みの KB は名前・説明を変えない（画面の読み取り専用と同じ。#961）。"""
    pool = FakeOraclePool(execute_results=[[_archived_knowledge_base_row("kb-old", "旧規程")]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(ValueError, match="アーカイブ済み"):
        await client.update_knowledge_base(
            "kb-old", description="新しい説明", update_fields={"description"}
        )

    assert not any("UPDATE rag_knowledge_bases" in call.statement for call in pool.connection.calls)
    assert pool.connection.commits == 0
