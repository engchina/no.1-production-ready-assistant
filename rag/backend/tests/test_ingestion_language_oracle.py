"""実 Oracle でジョブの版固定と成功時だけの成果物切替を確かめる。"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from rag_parser_core.language import builtin_pack, resolve

from app.clients.language_pack_release_store import _insert
from app.clients.oracle import OracleClient, OracleConnectionProtocol, _execute
from app.rag.ingestion_language import ingestion_release_scope
from app.schemas.document import IngestionJob, IngestionJobStatus
from app.schemas.language_pack_release import LanguagePackIndexRelation, LanguagePackRelease


@pytest.mark.usefixtures("oracle_db")
async def test_pinned_outputs_filter_and_failed_build_keep_serving_version() -> None:
    client = OracleClient()
    suffix = uuid4().hex
    first_pack = builtin_pack()
    second_pack = resolve(first_pack, {"labels": {"default_document_type": "合成の新しい種類"}})
    releases = [
        LanguagePackRelease(
            id=f"test-{suffix}-{index}",
            version=int(suffix[:12], 16) + index,
            effective=pack.to_dict(),
            hash=pack.hash,
            ingest_hash=pack.ingest_hash,
            published_at=datetime.now(UTC),
        )
        for index, pack in enumerate((first_pack, second_pack), start=1)
    ]

    def seed(connection: OracleConnectionProtocol) -> None:
        for release in releases:
            _insert(connection, release)

    await client._run_transaction(seed)
    document_id = None
    try:
        detail = await client.create_document(
            file_name=f"synthetic-{suffix}.txt",
            object_storage_path=f"local://{suffix}.txt",
            content_type="text/plain",
        )
        document_id = detail.id
        recipe = await client.ensure_default_document_recipe(document_id)
        recipe_id = str(recipe["recipe_id"])
        old_set, new_set = f"old-{suffix}", f"new-{suffix}"
        with ingestion_release_scope(releases[0], "synthetic-job"):
            await client.upsert_chunk_set(
                chunk_set_id=old_set, document_id=document_id, recipe_id=recipe_id
            )
        await client.mark_chunk_set_indexed(chunk_set_id=old_set, chunk_count=0, vector_count=0)
        await client.activate_recipe_chunk_set(recipe_id=recipe_id, chunk_set_id=old_set)
        item = IngestionJob(
            id=f"job-{suffix}",
            document_id=document_id,
            recipe_id=recipe_id,
            recipe_revision=int(str(recipe.get("config_revision") or 1)),
            parser_profile="generic",
            status=IngestionJobStatus.QUEUED,
            queued_at=datetime.now(UTC),
            language_pack_release_id=releases[1].id,
        )
        await client.create_ingestion_job(item)
        assert (
            await client.pin_ingestion_job_language_pack(item.id, releases[0].id) == releases[1].id
        )
        saved_job = await client.get_ingestion_job(item.id)
        assert saved_job is not None and saved_job.language_pack_release_id == releases[1].id
        with ingestion_release_scope(releases[1], item.id):
            await client.upsert_chunk_set(
                chunk_set_id=new_set, document_id=document_id, recipe_id=recipe_id
            )
        uses = await client.document_language_pack_uses(
            [document_id], current_release_id=releases[1].id
        )
        [used] = uses[document_id].recipes
        assert used.release_id == releases[0].id
        assert used.target_release_id == releases[1].id
        assert used.relation == LanguagePackIndexRelation.DIFFERENT
        assert used.built_at is not None
        args: dict[str, Any] = dict(
            language_pack_relation=LanguagePackIndexRelation.DIFFERENT,
            current_release_id=releases[1].id,
        )
        ids, total = await client.list_document_ids(**args, limit=1000)
        assert document_id in ids and total >= 1
        rows = await client.list_documents(**args)
        assert document_id in [row.id for row in rows]
        assert await client.count_documents(**args) == total
        # 未完了の成果物を active にする操作は拒否し、旧版を引き続き表示する。
        with pytest.raises(ValueError):
            await client.activate_recipe_chunk_set(recipe_id=recipe_id, chunk_set_id=new_set)
        assert (
            await client.document_language_pack_uses(
                [document_id], current_release_id=releases[1].id
            )
        )[document_id].recipes[0].release_id == releases[0].id
        await client.mark_chunk_set_indexed(chunk_set_id=new_set, chunk_count=0, vector_count=0)
        await client.activate_recipe_chunk_set(recipe_id=recipe_id, chunk_set_id=new_set)
        now = (
            await client.document_language_pack_uses(
                [document_id], current_release_id=releases[1].id
            )
        )[document_id].recipes[0]
        assert now.release_id == releases[1].id and now.job_id == item.id
        assert now.relation == LanguagePackIndexRelation.CURRENT
        ids, _ = await client.list_document_ids(**args, limit=1000)
        assert document_id not in ids
    finally:
        if document_id:
            await client.delete_document(document_id)

        def cleanup(connection: OracleConnectionProtocol) -> None:
            for release in releases:
                _execute(
                    connection,
                    "DELETE FROM rag_language_pack_releases WHERE release_id=:id",
                    {"id": release.id},
                )

        await client._run_transaction(cleanup)
