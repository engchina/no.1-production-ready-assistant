"""文書の削除の後始末の記録（#2452）を、実 Oracle AI Database で確かめる。

文書の行の削除と記録が同じ transaction で残ること、実行中の取込 job で削除が止まると記録も
残らないこと、残りの対象の書き戻しと完了で行が消えることを、合成の文書で確かめる。未到達なら
oracle_db fixture が skip し、作成した文書は cleanup_to_baseline、記録はテストの中で後始末する
（AI と保存先は使わない）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.clients.document_cleanup_store import (
    DOCUMENT_CLEANUP_TABLE,
    DocumentCleanupStore,
    DocumentCleanupTargets,
)
from app.clients.oracle import DocumentDeleteBlockedByRunningIngestionError, OracleClient
from app.schemas.document import IngestionJob, IngestionJobStatus


async def _delete_cleanup_rows(client: OracleClient, document_ids: list[str]) -> None:
    def operation(connection: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        for document_id in document_ids:
            cursor.execute(
                f"DELETE FROM {DOCUMENT_CLEANUP_TABLE} WHERE document_id = :document_id",
                {"document_id": document_id},
            )

    await client._run_transaction(operation)


@pytest.mark.usefixtures("oracle_db")
async def test_document_cleanup_record_is_written_with_document_delete() -> None:
    client = OracleClient()
    store = DocumentCleanupStore(client)
    token = uuid4().hex[:12]
    deleted = await client.create_document(
        file_name=f"cleanup-{token}.txt",
        object_storage_path=f"local://uploaded/{token}/deleted.txt",
        content_type="text/plain",
    )
    remaining = await client.create_document(
        file_name=f"cleanup-remaining-{token}.txt",
        object_storage_path=f"local://uploaded/{token}/remaining.txt",
        content_type="text/plain",
    )
    targets = DocumentCleanupTargets(
        source_uri=f"local://uploaded/{token}/deleted.txt",
        artifact_uris=(f"local://artifacts/extractions/{deleted.id}/full.json",),
        prefixes=(f"artifacts/extractions/{deleted.id}/",),
    )
    try:
        task = await store.delete_document_with_cleanup(deleted.id, deleted.file_name, targets)

        assert task is not None
        assert await client.get_document(deleted.id) is None
        found = await store.find_for_document(deleted.id)
        assert found is not None
        assert found.cleanup_id == task.cleanup_id
        assert found.file_name == deleted.file_name
        assert found.targets == targets
        assert task.cleanup_id in {
            item.cleanup_id for item in await store.list_due(limit=200, min_age_seconds=0)
        }
        # 新しい記録は、削除の要求の中の後始末と重ねないよう定期の後始末の対象にしない。
        assert task.cleanup_id not in {
            item.cleanup_id for item in await store.list_due(limit=200, min_age_seconds=3600)
        }
        assert await store.referenced_uris(
            [targets.source_uri or "", f"local://uploaded/{token}/remaining.txt"]
        ) == {f"local://uploaded/{token}/remaining.txt"}
        assert await store.document_exists(remaining.id)
        assert not await store.document_exists(deleted.id)
        # 消えた文書の再削除は None（記録は増えない）。
        assert await store.delete_document_with_cleanup(deleted.id, "x", targets) is None

        left = DocumentCleanupTargets(prefixes=targets.prefixes)
        await store.save_progress(task.cleanup_id, left, error_type="OSError")
        retried = await store.find_for_document(deleted.id)
        assert retried is not None
        assert retried.targets == left
        assert retried.attempt_count == 1

        await store.save_progress(task.cleanup_id, DocumentCleanupTargets(), error_type=None)
        assert await store.find_for_document(deleted.id) is None
    finally:
        await _delete_cleanup_rows(client, [deleted.id, remaining.id])


@pytest.mark.usefixtures("oracle_db")
async def test_document_cleanup_record_is_rolled_back_when_running_job_blocks_delete() -> None:
    client = OracleClient()
    store = DocumentCleanupStore(client)
    token = uuid4().hex[:12]
    detail = await client.create_document(
        file_name=f"cleanup-running-{token}.txt",
        object_storage_path=f"local://uploaded/{token}/running.txt",
        content_type="text/plain",
    )
    [recipe, *_] = await client.list_document_recipes(detail.id)
    job = await client.create_ingestion_job(
        IngestionJob(
            id=f"job_cleanup_{token}",
            document_id=detail.id,
            recipe_id=str(recipe["recipe_id"]),
            recipe_revision=int(str(recipe["config_revision"])),
            status=IngestionJobStatus.QUEUED,
            parser_profile="local_text_structure",
            queued_at=datetime.now(UTC),
        )
    )
    assert await client.claim_ingestion_job(job.id, started_at=datetime.now(UTC)) is not None
    try:
        with pytest.raises(DocumentDeleteBlockedByRunningIngestionError):
            await store.delete_document_with_cleanup(
                detail.id,
                detail.file_name,
                DocumentCleanupTargets(source_uri=f"local://uploaded/{token}/running.txt"),
            )

        assert await client.get_document(detail.id) is not None
        assert await store.find_for_document(detail.id) is None
    finally:
        await _delete_cleanup_rows(client, [detail.id])
