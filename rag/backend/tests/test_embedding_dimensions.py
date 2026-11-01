"""埋め込みの次元の正本と、RAG の各層の一致（#46）。"""

from __future__ import annotations

from pr_system_settings.embedding import EMBEDDING_DIMENSIONS, EMBEDDING_VECTOR_COLUMN_TYPE
from rag_engine.adapters.oracle.store import ADB_EMBEDDING_DIMENSIONS
from rag_engine.config import DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS

from app.clients.oracle import oracle_vector_schema_sql
from app.config import Settings
from app.rag import oracle_schema
from app.rag.system_schema import pending_destructive_migrations


def test_rag_engine_dimensions_match_platform_constant() -> None:
    """rag_engine は platform を import できないため同じ値を持つ。値の一致をここで確かめる。"""
    assert DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS == EMBEDDING_DIMENSIONS
    assert ADB_EMBEDDING_DIMENSIONS == EMBEDDING_DIMENSIONS
    assert Settings().genai_embedding_dim == EMBEDDING_DIMENSIONS


def test_chunk_ddl_and_manifest_use_platform_vector_type() -> None:
    assert f"embedding       {EMBEDDING_VECTOR_COLUMN_TYPE}," in oracle_vector_schema_sql()
    assert oracle_schema.VECTOR_CONTRACT == EMBEDDING_VECTOR_COLUMN_TYPE
    assert oracle_schema.oracle_schema_manifest()["vector_contract"] == "VECTOR(1024, FLOAT32)"


def test_embedding_dimensions_migration_rebuilds_only_mismatched_column() -> None:
    section = {item.name: item for item in oracle_schema.oracle_schema_migration_sections()}[
        oracle_schema.EMBEDDING_DIMENSIONS_MIGRATION
    ]

    assert section.table_name == "rag_chunks"
    # 保存済みのベクトルを消すので、作成・更新は承認を求める。
    assert section.destructive
    assert "再埋め込み" in section.destructive_note
    sql = section.sql
    assert "vector_info" in sql
    assert f"NOT LIKE 'VECTOR({EMBEDDING_DIMENSIONS},FLOAT32%'" in sql
    assert "DROP INDEX rag_chunks_embedding_hnsw_idx" in sql
    assert "ALTER TABLE rag_chunks DROP COLUMN embedding" in sql
    assert f"ALTER TABLE rag_chunks ADD (embedding {EMBEDDING_VECTOR_COLUMN_TYPE})" in sql
    assert "'CREATE VECTOR INDEX rag_chunks_embedding_hnsw_idx '" in sql
    # 文書・chunk の本文は消さない。
    assert "DROP TABLE" not in sql and "DELETE" not in sql
    # 1 つの PL/SQL の block（SQLcl でも 1 文で当たる）。
    assert len(oracle_schema.split_sql_statements(sql)) == 1


def test_embedding_dimensions_migration_requires_approval_on_existing_schema() -> None:
    name = oracle_schema.EMBEDDING_DIMENSIONS_MIGRATION
    pending = pending_destructive_migrations("outdated", [name])
    assert [item["name"] for item in pending] == [name]
    # 未初期化の DB は migration を記録だけするので、承認は要らない。
    assert pending_destructive_migrations("missing", [name]) == []
