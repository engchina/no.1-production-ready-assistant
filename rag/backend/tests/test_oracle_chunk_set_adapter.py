"""chunk_set 永続化の実 Oracle 統合テスト(3 層モデル: 文書単位 serving)。

実 Oracle AI Database を使い、文書単位 serving(is_serving)の確定/付け替え、所属 KB の
membership 由来導出、save_index の chunk_set スコープ、抽出 artifact の永続化を検証する。
未到達なら oracle_db fixture が skip し、作成行は cleanup_to_baseline で後始末する。
"""

import json
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.rag.chunking import Chunk
from app.rag.chunking_small_to_big import FIRST_PAGE_CONTEXT_KEY
from app.rag.ingestion import _coerce_extraction_payload, _validate_structured_extraction_payload
from app.schemas.document import DocumentProcessingConfig
from app.schemas.extraction import StructuredExtraction

_EMBEDDING = [0.1] * 1536


def _chunks(prefix: str, count: int) -> list[Chunk]:
    return [
        Chunk(index=i, text=f"{prefix}{i}", start_offset=i * 4, end_offset=i * 4 + 2)
        for i in range(count)
    ]


async def _stored_chunk_count(client: OracleClient, document_id: str) -> int:
    """文書に保存された chunk 行の数(検索対象かどうかに関係なく chunk_set をまたいで数える)。"""
    return len(await client.list_document_chunks(document_id))


def _unique_id(prefix: str) -> str:
    """実行ごとに一意な ID。前回の実行の行と衝突させない。"""
    return f"{prefix}_{uuid4().hex[:16]}"


async def _new_document(client: OracleClient) -> str:
    detail = await client.create_document(
        file_name="variant-doc.txt",
        object_storage_path="oci://bucket/variant-doc.txt",
        content_type="text/plain",
    )
    return detail.id


@pytest.mark.usefixtures("oracle_db")
async def test_document_processing_config_round_trip() -> None:
    """文書の処理レシピ上書き JSON は null を保存せず復元できる。"""
    client = OracleClient()
    document_id = await _new_document(client)
    expected = DocumentProcessingConfig(
        parser_adapter_backend="unstructured",
        chunking_strategy="page_level",
        chunk_size=900,
    )

    await client.update_document_processing_config(document_id, expected)

    assert await client.get_document_processing_config(document_id) == expected


@pytest.mark.usefixtures("oracle_db")
async def test_set_document_serving_chunk_set_marks_exactly_one_serving() -> None:
    """3 層モデル: set_document_serving_chunk_set は指定 1 つだけ is_serving=1、他は 0。"""
    client = OracleClient()
    document_id = await _new_document(client)
    cs_a = _unique_id("cs_serving")
    cs_b = _unique_id("cs_serving")
    await client.upsert_chunk_set(chunk_set_id=cs_a, document_id=document_id)
    await client.upsert_chunk_set(chunk_set_id=cs_b, document_id=document_id)

    # 既定は両方 serving(DEFAULT 1)。cs_a を serving に確定すると cs_b は 0 になる。
    await client.set_document_serving_chunk_set(document_id=document_id, chunk_set_id=cs_a)
    got_a = await client.get_chunk_set(cs_a)
    got_b = await client.get_chunk_set(cs_b)
    assert got_a is not None and int(str(got_a["is_serving"])) == 1
    assert got_b is not None and int(str(got_b["is_serving"])) == 0

    # serving を cs_b へ付け替える(Phase 3 の昇格相当)と入れ替わる。
    await client.set_document_serving_chunk_set(document_id=document_id, chunk_set_id=cs_b)
    swapped_a = await client.get_chunk_set(cs_a)
    swapped_b = await client.get_chunk_set(cs_b)
    assert swapped_a is not None and int(str(swapped_a["is_serving"])) == 0
    assert swapped_b is not None and int(str(swapped_b["is_serving"])) == 1


@pytest.mark.usefixtures("oracle_db")
async def test_list_document_chunk_sets_derives_membership_and_serving() -> None:
    """3 層モデル: 所属 KB は文書 membership、配信は cs.is_serving から導出する(binding 非依存)。"""
    client = OracleClient()
    document_id = await _new_document(client)
    cs_a = _unique_id("cs_list")
    cs_b = _unique_id("cs_list")
    await client.upsert_chunk_set(chunk_set_id=cs_a, document_id=document_id)
    await client.upsert_chunk_set(chunk_set_id=cs_b, document_id=document_id)

    kb_1 = await client.create_knowledge_base(name="一覧KB-1")
    kb_2 = await client.create_knowledge_base(name="一覧KB-2")
    await client.assign_documents_to_knowledge_base(kb_1.id, [document_id])
    await client.assign_documents_to_knowledge_base(kb_2.id, [document_id])

    # cs_a を serving に確定(cs_b は demote)。
    await client.set_document_serving_chunk_set(document_id=document_id, chunk_set_id=cs_a)

    rows = {
        str(row["chunk_set_id"]): row for row in await client.list_document_chunk_sets(document_id)
    }
    member_ids = {kb_1.id, kb_2.id}

    def _ids(value: object) -> set[str]:
        assert isinstance(value, list)
        return {str(item) for item in value}

    # 所属 KB は全 chunk_set 共通(文書 membership。既定 KB を含み得る)。
    a_members = _ids(rows[cs_a]["knowledge_base_ids"])
    b_members = _ids(rows[cs_b]["knowledge_base_ids"])
    assert member_ids <= a_members
    assert a_members == b_members
    # 配信中の chunk_set だけ全 membership が serving、非配信は空。
    assert _ids(rows[cs_a]["serving_knowledge_base_ids"]) == a_members
    assert _ids(rows[cs_b]["serving_knowledge_base_ids"]) == set()
    # is_serving フラグは membership に依らず serving 判別できる(KB 未所属でも有効)。
    assert rows[cs_a]["is_serving"] is True
    assert rows[cs_b]["is_serving"] is False
    # 作成日時は API(診断行の chunk_set 作成表示)向けに全行で返す。
    assert all(row["created_at"] is not None for row in rows.values())


@pytest.mark.usefixtures("oracle_db")
async def test_upsert_chunk_set_is_idempotent_and_mark_indexed() -> None:
    """upsert は冪等(重複行を作らない)、mark_chunk_set_indexed が status/件数を更新する。"""
    client = OracleClient()
    document_id = await _new_document(client)
    cs = _unique_id("cs_test")

    await client.upsert_chunk_set(chunk_set_id=cs, document_id=document_id)
    await client.upsert_chunk_set(chunk_set_id=cs, document_id=document_id)
    rows = await client.list_document_chunk_sets(document_id)
    assert [str(row["chunk_set_id"]) for row in rows] == [cs]

    before = await client.get_chunk_set(cs)
    assert before is not None
    assert before["status"] == "INGESTING"

    await client.mark_chunk_set_indexed(chunk_set_id=cs, chunk_count=12, vector_count=12)
    after = await client.get_chunk_set(cs)
    assert after is not None
    assert after["status"] == "INDEXED"
    assert after["chunk_count"] == 12
    assert after["vector_count"] == 12


@pytest.mark.usefixtures("oracle_db")
async def test_save_index_chunk_set_scope_keeps_other_chunk_sets() -> None:
    """save_index(chunk_set_id=...) はその chunk_set だけ置換し、他 chunk_set の chunk を残す。"""
    client = OracleClient()
    document_id = await _new_document(client)
    extraction = StructuredExtraction(raw_text="本文", confidence=0.9)
    cs_a = _unique_id("cs_scope")
    cs_b = _unique_id("cs_scope")

    # chunk_set A: 2 chunk、chunk_set B: 3 chunk を共存させる。
    await client.save_index(
        document_id, extraction, _chunks("A", 2), [_EMBEDDING] * 2, chunk_set_id=cs_a
    )
    await client.save_index(
        document_id, extraction, _chunks("B", 3), [_EMBEDDING] * 3, chunk_set_id=cs_b
    )
    # B 保存で A は消えていない(scoped delete)= 2 + 3 = 5。
    assert await _stored_chunk_count(client, document_id) == 5

    # A を 1 chunk で置換 → A の 2 は消え 1 追加、B の 3 は不変 = 4。
    await client.save_index(
        document_id, extraction, _chunks("A", 1), [_EMBEDDING], chunk_set_id=cs_a
    )
    assert await _stored_chunk_count(client, document_id) == 4


@pytest.mark.usefixtures("oracle_db")
async def test_first_page_context_is_saved_once_per_chunk_set_and_read_back() -> None:
    """1 ページ目の本文は chunk set に 1 つ保存し、回答のときに chunk set ごとに読める(#557)。

    取込は chunk を保存してから chunk set の行を作るので、保存で行を作り、後の upsert / mark が
    状態を書く。保存済み chunk を再利用する索引(値なし)で消さない。chunk の行には入れない。
    """
    client = OracleClient()
    document_id = await _new_document(client)
    extraction = StructuredExtraction(raw_text="本文", confidence=0.9)
    cs = _unique_id("cs_first_page")
    first_page = {
        "page": 1,
        "status": "available",
        "text": "受注管理規程 第3版 営業本部",
        "engine": "docling",
        "record_ids": ["docling-p1-1"],
        "truncated": False,
    }
    chunks = _chunks("P", 2)
    chunks[0].metadata[FIRST_PAGE_CONTEXT_KEY] = json.dumps(first_page, ensure_ascii=False)

    await client.save_chunk_preview(document_id, extraction, chunks, chunk_set_id=cs)
    await client.upsert_chunk_set(chunk_set_id=cs, document_id=document_id, status="CHUNKED")
    await client.mark_chunk_set_chunked(chunk_set_id=cs, chunk_count=2)

    chunk_set = await client.get_chunk_set(cs)
    assert chunk_set is not None and chunk_set["status"] == "CHUNKED"
    assert await client.chunk_set_first_page_contexts([cs, _unique_id("cs_missing")]) == {
        cs: first_page
    }
    views = await client.list_chunk_set_chunks(cs)
    assert len(views) == 2
    assert all(FIRST_PAGE_CONTEXT_KEY not in view.metadata for view in views)

    await client.save_index(
        document_id, extraction, _chunks("P", 2), [_EMBEDDING] * 2, chunk_set_id=cs
    )
    assert await client.chunk_set_first_page_contexts([cs]) == {cs: first_page}


@pytest.mark.usefixtures("oracle_db")
async def test_save_index_without_chunk_set_replaces_all_chunks() -> None:
    """chunk_set_id 未指定(現行挙動)は文書の全 chunk を置換する(後方互換)。"""
    client = OracleClient()
    document_id = await _new_document(client)
    extraction = StructuredExtraction(raw_text="本文", confidence=0.9)

    await client.save_index(document_id, extraction, _chunks("X", 3), [_EMBEDDING] * 3)
    assert await _stored_chunk_count(client, document_id) == 3
    # 再保存は全置換(2 件)= 2(加算されない)。
    await client.save_index(document_id, extraction, _chunks("Y", 2), [_EMBEDDING] * 2)
    assert await _stored_chunk_count(client, document_id) == 2


@pytest.mark.usefixtures("oracle_db")
async def test_document_extraction_artifact_round_trip_and_gc() -> None:
    """抽出 artifact の永続化: upsert / get / 状態更新 / GC が実 Oracle で動く。

    1 文書が複数の抽出 recipe(前処理 × 解析)を持てること、抽出 payload が JSON 列を往復して
    索引段階の読み込み(coerce → validate)で復元できることを確かめる。
    """
    client = OracleClient()
    document_id = await _new_document(client)
    payload = StructuredExtraction(raw_text="抽出本文の一文目です。").to_document_payload()

    ex_a = _unique_id("ex_a")
    ex_b = _unique_id("ex_b")
    await client.upsert_document_extraction_artifact(
        document_id=document_id,
        extraction_recipe_id=ex_a,
        source_sha256="a" * 64,
        recipe_subset={"preprocess": "none", "parser": "docling"},
        extraction=payload,
        status="materialized",
    )
    await client.upsert_document_extraction_artifact(
        document_id=document_id,
        extraction_recipe_id=ex_b,
        source_sha256="a" * 64,
        recipe_subset={"preprocess": "none", "parser": "unstructured"},
        extraction=payload,
        status="materialized",
    )

    got = await client.get_document_extraction_artifact(
        document_id=document_id, extraction_recipe_id=ex_a
    )
    assert got is not None
    assert got["status"] == "materialized"
    assert got["document_id"] == document_id
    assert got["recipe_subset"] == {"preprocess": "none", "parser": "docling"}
    # 索引段階の読み込み(get → coerce → validate)が実 Oracle の JSON 列を往復できる。
    coerced = _coerce_extraction_payload(got["extraction_json"])
    assert coerced is not None
    assert _validate_structured_extraction_payload(coerced).raw_text == "抽出本文の一文目です。"

    # 状態だけの更新(extraction なし)は保存済みの payload を消さない。
    await client.upsert_document_extraction_artifact(
        document_id=document_id,
        extraction_recipe_id=ex_a,
        source_sha256="a" * 64,
        recipe_subset={"preprocess": "none", "parser": "docling"},
        status="error",
        reason="解析に失敗しました。",
    )
    reloaded = await client.get_document_extraction_artifact(
        document_id=document_id, extraction_recipe_id=ex_a
    )
    assert reloaded is not None
    assert reloaded["status"] == "error"
    assert reloaded["extraction_json"] == got["extraction_json"]

    # ex_b 以外を残す GC → ex_a が消える。
    removed = await client.delete_document_extractions_except(
        document_id=document_id, keep_extraction_ids=[ex_b]
    )
    assert removed == [ex_a]
    assert (
        await client.get_document_extraction_artifact(
            document_id=document_id, extraction_recipe_id=ex_a
        )
        is None
    )
    assert (
        await client.get_document_extraction_artifact(
            document_id=document_id, extraction_recipe_id=ex_b
        )
        is not None
    )


@pytest.mark.usefixtures("oracle_db")
async def test_saved_embedding_commit_updates_search_text_and_preserves_original_in_oracle() -> (
    None
):
    """保存済み Chunk の向量・検索本文・metadata を commit し、再保存も索引へ反映する。"""
    client = OracleClient()
    document_id = await _new_document(client)
    chunk_set_id = _unique_id("cs_embed_commit")
    await client.upsert_chunk_set(chunk_set_id=chunk_set_id, document_id=document_id)
    original = "合成の本文を引用します。"
    await client.save_chunk_preview(
        document_id,
        StructuredExtraction(raw_text=original),
        [
            Chunk(
                index=0,
                text=original,
                start_offset=0,
                end_offset=len(original),
                metadata={"context_header": "oldkeyword"},
            )
        ],
        chunk_set_id=chunk_set_id,
    )
    [view] = await client.list_chunk_set_chunks(chunk_set_id)
    for header in ("newkeyword", "newkeyword"):
        chunk = Chunk(
            index=0,
            text=original,
            start_offset=0,
            end_offset=len(original),
            metadata={
                "document_id": document_id,
                "chunk_id": view.chunk_id,
                "context_header": header,
            },
        )
        await client.update_chunk_set_embeddings(
            chunk_set_id=chunk_set_id,
            chunks=[chunk],
            embeddings=[_EMBEDDING],
        )
        # 別 transaction で読むため、commit 前の COUNT だけを成功の証拠にしない。
        counts = await client._fetch_one(
            """
            SELECT COUNT(*) AS vector_count FROM rag_chunks
            WHERE chunk_set_id = :chunk_set_id AND embedding IS NOT NULL
            """,
            {"chunk_set_id": chunk_set_id},
        )
        assert counts is not None and int(str(counts["vector_count"])) == 1
        matches = await client._fetch_one(
            """
            SELECT COUNT(*) AS match_count FROM rag_chunks
            WHERE chunk_set_id = :chunk_set_id AND CONTAINS(search_text, 'newkeyword') > 0
            """,
            {"chunk_set_id": chunk_set_id},
        )
        assert matches is not None and int(str(matches["match_count"])) == 1
    [saved] = await client.list_chunk_set_chunks(chunk_set_id)
    assert saved.text == original
    assert saved.metadata["context_header"] == "newkeyword"
