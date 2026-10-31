"""文書の一括の投入(#2674)の Oracle の保存先のテスト。

SQL の形(IN の分割・lock・待機中の job の確かめ直し・executemany)は fake の pool で、
実際の登録(欠落 0・重複 0・再送の冪等)は実 Oracle で確かめる(未到達なら oracle_db fixture が
skip し、作成行は cleanup_to_baseline で後始末する)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.api.routes import documents as documents_route
from app.clients.oracle import OracleClient, _execute, default_document_recipe_id
from app.clients.oracle_bulk_ingestion_store import OracleBulkIngestionStore
from app.config import Settings
from app.rag.bulk_ingestion import BulkJobDraft, accept_bulk_ingestion
from app.schemas.document import (
    BulkIngestionOutcome,
    DocumentProcessingConfig,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
)
from tests.test_oracle_adapter import FakeOraclePool, _oci_settings, _run_inline

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _client(pool: FakeOraclePool) -> OracleClient:
    return OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)


def _document_row(document_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": document_id,
        "file_name": f"{document_id}.txt",
        "document_status": "UPLOADED",
        "tenant_id_hash": None,
        "content_type": "text/plain",
        "file_size_bytes": 10,
        "content_sha256": "a" * 64,
        "duplicate_of_document_id": None,
        "processing_config": None,
        "error_message": None,
        "uploaded_at": NOW,
        "indexed_at": None,
        "has_source": 1,
        "has_outputs": 0,
        "list_status": "UPLOADED",
        "default_recipe_id": default_document_recipe_id(document_id),
        "recipe_processing_config": '{"chunk_size": 800}',
    }
    row.update(overrides)
    return row


async def test_load_states_reads_documents_and_jobs_in_chunks() -> None:
    """文書と job は IN の条件でまとめて読み、500 件を超えると分ける(1 文書ずつ往復しない)。"""
    ids = [f"doc-{index:03d}" for index in range(501)]
    pool = FakeOraclePool(
        execute_results=[
            [
                _document_row("doc-000"),
                _document_row(
                    "doc-001",
                    list_status="ERROR",
                    has_outputs=1,
                    default_recipe_id=None,
                    processing_config='{"chunk_size": 600}',
                ),
            ],
            [
                # doc-000: 別のレシピ(レシピ2)の QUEUED は、レシピ1の待機中には数えない。
                {"document_id": "doc-000", "recipe_id": "other-recipe", "active": 1},
                {"document_id": "doc-001", "recipe_id": None, "active": 1},
            ],
            [_document_row("doc-500")],
            [],
        ]
    )

    states = await OracleBulkIngestionStore(_client(pool)).load_states(ids)

    selects = pool.connection.calls
    assert len(selects) == 4
    document_query, job_query = selects[0], selects[1]
    assert "FROM rag_documents d LEFT JOIN rag_document_recipes r" in document_query.statement
    assert "r.slot_no = 1" in document_query.statement
    assert "KEEP (DENSE_RANK FIRST ORDER BY s.slot_no)" in document_query.statement
    assert "d.document_id IN (:bulk_document_id_0," in document_query.statement
    assert len([key for key in document_query.parameters if key.startswith("bulk_document")]) == 500
    assert "GROUP BY j.document_id, j.recipe_id" in job_query.statement
    assert set(job_query.parameters) == {"bulk_job_document_id_0", "bulk_job_document_id_1"}
    assert "bulk_document_id_0" in selects[2].parameters
    assert selects[2].parameters["bulk_document_id_0"] == "doc-500"

    assert set(states) == {"doc-000", "doc-001", "doc-500"}
    fresh = states["doc-000"]
    # 別のレシピの job があるので、一度も取り込んでいない文書には数えない。
    assert fresh.has_job_history is True
    assert fresh.fresh is False
    assert fresh.has_active_job is False
    assert fresh.processing_config == {"chunk_size": 800}
    retry = states["doc-001"]
    assert retry.status == FileStatus.ERROR
    assert retry.has_active_job is True
    assert retry.has_outputs is True
    assert retry.has_default_recipe is False
    # レシピ1が無い文書は文書の処理設定を使う(1 件の投入の ensure と同じ値)。
    assert retry.processing_config == {"chunk_size": 600}
    assert states["doc-500"].fresh is True


async def test_insert_queued_jobs_locks_recipes_and_rechecks_active_jobs() -> None:
    """1 つの transaction でレシピ1を用意し、lock し、待機中の job を確かめ直してから登録する。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                {"document_id": "doc-a", "recipe_id": "recipe-a", "config_revision": 2},
                {"document_id": "doc-b", "recipe_id": "recipe-b", "config_revision": 1},
                {"document_id": "doc-c", "recipe_id": "recipe-c", "config_revision": 1},
            ],
            [
                {"document_id": "doc-b", "recipe_id": "recipe-b"},
                # レシピ1以外の待機中の job は止めない。
                {"document_id": "doc-c", "recipe_id": "recipe-other"},
            ],
        ]
    )

    def draft(document_id: str, *, has_recipe: bool = True) -> BulkJobDraft:
        return BulkJobDraft(
            document_id=document_id,
            parser_profile="local_text_structure",
            quality_warnings=("w1",),
            has_default_recipe=has_recipe,
            recipe_seed={"document_id": document_id, "status": "UPLOADED", "uploaded_at": NOW},
        )

    result = await OracleBulkIngestionStore(_client(pool)).insert_queued_jobs(
        [draft("doc-a", has_recipe=False), draft("doc-b"), draft("doc-c"), draft("doc-gone")],
        queued_at=NOW,
        max_attempts=3,
    )

    assert set(result.created) == {"doc-a", "doc-c"}
    assert result.active == frozenset({"doc-b"})
    assert result.missing == frozenset({"doc-gone"})
    assert pool.connection.commits == 1
    merge, insert = pool.connection.many_calls
    assert merge.statement.startswith("MERGE INTO rag_document_recipes")
    assert [row["document_id"] for row in merge.rows] == ["doc-a"]
    assert merge.rows[0]["recipe_id"] == default_document_recipe_id("doc-a")
    lock, active = pool.connection.calls
    assert "FOR UPDATE OF r.recipe_id" in lock.statement
    assert "r.slot_no = 1" in lock.statement
    assert "status IN ('QUEUED', 'RUNNING')" in active.statement
    assert insert.statement.startswith("INSERT INTO rag_ingestion_jobs")
    rows = {row["document_id"]: row for row in insert.rows}
    assert set(rows) == {"doc-a", "doc-c"}
    assert rows["doc-a"]["recipe_id"] == "recipe-a"
    assert rows["doc-a"]["recipe_revision"] == 2
    assert rows["doc-a"]["status"] == "QUEUED"
    assert rows["doc-a"]["phase"] == "PREPROCESS"
    assert rows["doc-a"]["max_attempts"] == 3
    assert rows["doc-a"]["job_id"] == result.created["doc-a"]


async def test_insert_queued_jobs_refuses_batches_over_the_in_limit() -> None:
    pool = FakeOraclePool()
    drafts = [
        BulkJobDraft(
            document_id=f"doc-{index}",
            parser_profile="p",
            quality_warnings=(),
            has_default_recipe=True,
            recipe_seed={},
        )
        for index in range(501)
    ]
    with pytest.raises(ValueError):
        await OracleBulkIngestionStore(_client(pool)).insert_queued_jobs(
            drafts, queued_at=NOW, max_attempts=3
        )
    assert pool.connection.calls == []


# ---------------------------------------------------------------------------
# 実 Oracle
# ---------------------------------------------------------------------------

# 1 回の SQL の IN(500 件)と 1 回の transaction(500 件)を超える数。
_REAL_COUNT = 510


def _resolve(config: DocumentProcessingConfig) -> Settings:
    return documents_route._merge_document_processing_config(config)[0]


async def _no_single(document_id: str) -> IngestionJob:
    raise AssertionError(f"新しい文書は 1 件の投入を通らない: {document_id}")


async def _queued_jobs_by_document(client: OracleClient, document_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for start in range(0, len(document_ids), 500):
        chunk = document_ids[start : start + 500]
        binds = {f"d{index}": value for index, value in enumerate(chunk)}
        rows = await client._fetch_all(
            "SELECT document_id, COUNT(*) AS job_count FROM rag_ingestion_jobs "
            f"WHERE status = 'QUEUED' AND document_id IN ({', '.join(':' + key for key in binds)}) "
            "GROUP BY document_id",
            binds,
        )
        counts.update({str(row["document_id"]): int(str(row["job_count"])) for row in rows})
    return counts


@pytest.mark.usefixtures("oracle_db")
async def test_bulk_ingestion_registers_each_document_once_in_oracle() -> None:
    client = OracleClient()
    tag = uuid4().hex[:10]
    document_ids: list[str] = []
    for index in range(_REAL_COUNT):
        file_name = f"bulk-{tag}-{index:04d}.txt"
        detail = await client.create_document(
            file_name=file_name,
            object_storage_path=f"local://uploaded/{file_name}",
            content_type="text/plain",
            file_size_bytes=10,
            content_sha256=uuid4().hex + uuid4().hex,
        )
        document_ids.append(detail.id)
    # レシピ1の行が無い文書(migration 前の文書)は、登録の transaction で作る。
    no_recipe = document_ids[0]
    await client._run_transaction(
        lambda connection: _execute(
            connection,
            "DELETE FROM rag_document_recipes WHERE document_id = :document_id",
            {"document_id": no_recipe},
        )
    )
    # レシピ1に実行中の job がある文書は、既に処理中にする。
    running = document_ids[1]
    recipe = await client.ensure_default_document_recipe(running)
    await client.create_ingestion_job(
        IngestionJob(
            id=uuid4().hex,
            document_id=running,
            recipe_id=str(recipe["recipe_id"]),
            recipe_revision=int(str(recipe.get("config_revision") or 1)),
            status=IngestionJobStatus.RUNNING,
            phase=IngestionJobPhase.PREPROCESS,
            parser_profile="local_text_structure",
            queued_at=NOW,
        )
    )
    store = OracleBulkIngestionStore(client)
    request = [*document_ids, f"missing-{tag}"]

    result = await accept_bulk_ingestion(
        request,
        store=store,
        resolve_settings=_resolve,
        enqueue_single=_no_single,
        max_attempts=3,
    )

    outcomes = {item.document_id: item for item in result.items}
    assert result.accepted_count == _REAL_COUNT - 1
    assert outcomes[running].outcome == BulkIngestionOutcome.ALREADY_ACTIVE
    assert outcomes[f"missing-{tag}"].reason == "not_found"
    assert outcomes[no_recipe].outcome == BulkIngestionOutcome.ACCEPTED
    queued = await _queued_jobs_by_document(client, document_ids)
    accepted = [
        document_id
        for document_id in document_ids
        if outcomes[document_id].outcome == BulkIngestionOutcome.ACCEPTED
    ]
    # 欠落 0・重複 0: 受付済みの文書ごとに QUEUED の job が 1 件だけ。
    assert queued == dict.fromkeys(accepted, 1)
    jobs = await client.list_document_ingestion_jobs(no_recipe)
    assert [job.id for job in jobs] == [outcomes[no_recipe].job_id]
    assert jobs[0].recipe_id == default_document_recipe_id(no_recipe)

    # 再送しても二重に登録しない。
    again = await accept_bulk_ingestion(
        request,
        store=store,
        resolve_settings=_resolve,
        enqueue_single=_no_single,
        max_attempts=3,
    )
    assert again.accepted_count == 0
    assert again.already_active_count == _REAL_COUNT
    assert await _queued_jobs_by_document(client, document_ids) == queued

    ids, total = await client.list_document_ids(query=f"bulk-{tag}", limit=_REAL_COUNT)
    assert total == _REAL_COUNT
    assert set(ids) == set(document_ids)
