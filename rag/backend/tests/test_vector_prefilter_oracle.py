"""回答の vector 検索が、索引を使う計画でも active でない chunk に k 件を取られないこと(#74)。

合成の文書(架空の手順書)で、問に近い chunk を再取込の途中の chunk_set(active でない)に置き、
vector の索引を使わせる hint を付けた近似の検索が、exact の検索と同じ行を k 件返すことを確かめる。
条件の書き換え(``_ACTIVE_CHUNK_PREFILTER_SQL``)が、従来の EXISTS の条件と同じ行を選ぶこと
(レシピの無い文書の未タグの chunk は使い、レシピのある文書に残った未タグの chunk は使わない)も
同じ DB で比べる。未到達なら oracle_db fixture が skip し、作成行は後始末する(AI は使わない)。
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from uuid import uuid4

import pytest

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    _oracle_retrieval_where,
    _to_vector_bind,
    vector_search_statement,
)
from app.config import get_settings
from app.rag.chunking import Chunk
from app.rag.vector_recall_cli import (
    _run_statement,
    force_vector_index_hint,
    vector_plan_operations,
)
from app.schemas.document import FileStatus
from app.schemas.extraction import StructuredExtraction

_DIMENSIONS = 1024
_TOP_K = 5


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def _near(rng: random.Random, center: list[float], spread: float) -> list[float]:
    noise = _unit([rng.gauss(0.0, 1.0) for _ in range(_DIMENSIONS)])
    return _unit([c + spread * n for c, n in zip(center, noise, strict=True)])


def _chunks(count: int, label: str) -> list[Chunk]:
    return [
        Chunk(text=f"{label} の手順 {index}。", index=index, start_offset=0, end_offset=8)
        for index in range(count)
    ]


_EXTRACTION = StructuredExtraction(raw_text="本文", confidence=0.9, document_type="手順書")


async def _document(client: OracleClient, knowledge_base_id: str, label: str) -> tuple[str, str]:
    detail = await client.create_document(
        file_name=f"{label}-{uuid4().hex[:8]}.pdf",
        object_storage_path=f"local://{label}.pdf",
        content_type="application/pdf",
        knowledge_base_ids=[knowledge_base_id],
    )
    recipes = await client.list_document_recipes(detail.id)
    return detail.id, str(recipes[0]["recipe_id"])


async def _chunk_set(
    client: OracleClient,
    document_id: str,
    recipe_id: str,
    vectors: list[list[float]],
    *,
    activate: bool,
) -> str:
    chunk_set_id = f"cs_pf_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    await client.save_index(
        document_id,
        _EXTRACTION,
        _chunks(len(vectors), chunk_set_id),
        vectors,
        chunk_set_id=chunk_set_id,
    )
    await client.mark_chunk_set_indexed(
        chunk_set_id=chunk_set_id, chunk_count=len(vectors), vector_count=len(vectors)
    )
    if activate:
        await client.activate_recipe_chunk_set(recipe_id=recipe_id, chunk_set_id=chunk_set_id)
    return chunk_set_id


def _drop_recipes(document_id: str) -> Callable[[OracleConnectionProtocol], None]:
    def operation(connection: OracleConnectionProtocol) -> None:
        _execute(
            connection,
            "DELETE FROM rag_chunk_sets WHERE document_id = :document_id",
            {"document_id": document_id},
        )
        _execute(
            connection,
            "DELETE FROM rag_document_recipes WHERE document_id = :document_id",
            {"document_id": document_id},
        )

    return operation


async def _ids(client: OracleClient, statement: str, binds: dict[str, object]) -> list[str]:
    return [str(row["chunk_id"]) for row in await client._fetch_all(statement, binds)]


@pytest.mark.usefixtures("oracle_db")
async def test_vector_search_with_the_index_is_not_starved_by_inactive_chunk_sets() -> None:
    client = OracleClient()
    settings = get_settings()
    rng = random.Random(74)
    query = _unit([rng.gauss(0.0, 1.0) for _ in range(_DIMENSIONS)])
    kb = await client.create_knowledge_base(name=f"kb-prefilter-{uuid4().hex[:8]}")

    # 検索する chunk(active)は問から少し遠く、再取込の途中の chunk(active でない)は問に近い。
    current_id, current_recipe = await _document(client, kb.id, "current")
    active_vectors = [_near(rng, query, 1.2) for _ in range(8)]
    await _chunk_set(client, current_id, current_recipe, active_vectors, activate=True)
    await _chunk_set(
        client,
        current_id,
        current_recipe,
        [_near(rng, query, 0.3) for _ in range(30)],
        activate=False,
    )
    await client.update_document_status(current_id, FileStatus.INDEXED)

    # レシピの無い文書の未タグの chunk(旧 schema)は検索に使う。
    legacy_id, _legacy_recipe = await _document(client, kb.id, "legacy")
    await client._run_transaction(_drop_recipes(legacy_id))
    await client.save_index(
        legacy_id, _EXTRACTION, _chunks(2, "legacy"), [_near(rng, query, 1.0) for _ in range(2)]
    )
    await client.update_document_status(legacy_id, FileStatus.INDEXED)

    # レシピのある文書に残った未タグの chunk は使わない(従来の条件と同じ)。
    residue_id, residue_recipe = await _document(client, kb.id, "residue")
    await client.save_index(
        residue_id, _EXTRACTION, _chunks(3, "residue"), [_near(rng, query, 0.2) for _ in range(3)]
    )
    await _chunk_set(
        client,
        residue_id,
        residue_recipe,
        [_near(rng, query, 1.3) for _ in range(3)],
        activate=True,
    )
    await client.update_document_status(residue_id, FileStatus.INDEXED)

    filters = {"knowledge_base_id": kb.id}
    bind = _to_vector_bind(query)
    approx_sql, binds = vector_search_statement(settings, bind, top_k=_TOP_K, filters=filters)
    exact_sql, _ = vector_search_statement(
        settings, bind, top_k=_TOP_K, filters=filters, exact=True
    )

    # 従来の EXISTS の条件で、同じ範囲の近い順の k 件(基準)。
    where_sql, reference_binds = _oracle_retrieval_where(filters)
    reference_binds["embedding"] = bind
    reference = await _ids(
        client,
        f"""
        SELECT c.chunk_id FROM rag_chunks c
        JOIN rag_documents d ON d.document_id = c.document_id
        WHERE {where_sql}
        ORDER BY VECTOR_DISTANCE(c.embedding, :embedding, COSINE), c.chunk_id
        FETCH EXACT FIRST {_TOP_K} ROWS ONLY
        """,
        reference_binds,
    )
    assert len(reference) == _TOP_K

    exact = await _ids(client, exact_sql, binds)
    approx = await _ids(client, approx_sql, binds)
    forced_sql = force_vector_index_hint(approx_sql)
    forced = await _ids(client, forced_sql, binds)
    _rows, _elapsed, plan = await client._run_transaction(
        _run_statement(forced_sql, binds, with_plan=True)
    )
    assert set(exact) == set(reference)
    assert set(approx) == set(reference)
    assert set(forced) == set(reference)
    # 索引を使う計画でも、active でない chunk に k 件を取られない(従来の条件では 2 件だった)。
    # 従来の条件と同じく、レシピの無い文書の未タグの chunk は使う。
    rows = await client._fetch_all(
        exact_sql.replace(f"FIRST {_TOP_K} ROWS", "FIRST 50 ROWS"), binds
    )
    documents = {str(row["document_id"]) for row in rows}
    assert legacy_id in documents
    chunk_sets = {row["chunk_set_id"] for row in rows if str(row["document_id"]) == residue_id}
    assert None not in chunk_sets
    # hint を付けても optimizer が HNSW を選ばない回があり(CI の Oracle Free で観測)、その回は
    # 結果の一致だけを確かめ、索引の計画の前提が満たされないことを skip で明示する。
    if not any("VECTOR INDEX HNSW SCAN" in op for op in vector_plan_operations(plan)):
        pytest.skip("optimizer が HNSW の索引を使わなかった:\n" + "\n".join(plan))
