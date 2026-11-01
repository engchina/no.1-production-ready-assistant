"""知識源の同期(#77)を、製品の取込先(``ProductSyncTarget``)と実 Oracle で確かめる。

合成の manifest(架空の社内規程)を同期し、取込 worker の代わりに job と文書の状態を進めて、
追加・同じ内容の再通知・内容の更新(取込が済むまで前の版が公開のまま)・権限の撤回(文書を
消す)・取込中の文書の撤回(job を取り消してから消す)を確かめる。未到達なら oracle_db fixture が
skip し、作成行は後始末する(AI は使わない)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.clients.file_storage import FileStorageClient
from app.clients.oracle import OracleClient
from app.config import get_settings
from app.rag.source_sync import ManifestSource, SyncReport, SyncStateStore, run_sync
from app.rag.source_sync_cli import ProductSyncTarget
from app.schemas.document import FileStatus, IngestionJobStatus


def _write_manifest(directory: Path, files: dict[str, tuple[str, bool]]) -> Path:
    items = []
    for external_id, (text, visible) in files.items():
        name = f"{external_id}.txt"
        (directory / name).write_text(text, encoding="utf-8")
        items.append({"external_id": external_id, "path": name, "visible": visible})
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({"items": items}), encoding="utf-8")
    return manifest


async def _finish_ingestion(client: OracleClient, document_id: str) -> None:
    """取込 worker の代わりに、待機中の job を成功にして文書を INDEXED にする。"""
    for job in await client.list_document_ingestion_jobs(
        document_id, status=IngestionJobStatus.QUEUED
    ):
        await client.transition_ingestion_job(
            job.id,
            from_statuses=(IngestionJobStatus.QUEUED,),
            to_status=IngestionJobStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
        )
    await client.update_document_status(document_id, FileStatus.INDEXED)


@pytest.mark.usefixtures("oracle_db")
async def test_manifest_sync_through_the_product_ingestion_path(tmp_path: Path) -> None:
    client = OracleClient()
    settings = get_settings()
    kb = await client.create_knowledge_base(name=f"kb-sync-{uuid4().hex[:8]}")
    target = ProductSyncTarget(
        client, FileStorageClient(settings), knowledge_base_ids=[kb.id], settings=settings
    )
    store = SyncStateStore(tmp_path / "state.json")
    source_id = f"sync-{uuid4().hex[:8]}"

    async def sync(files: dict[str, tuple[str, bool]]) -> SyncReport:
        manifest = _write_manifest(tmp_path, files)
        return await run_sync(
            source_id=source_id,
            source=ManifestSource(manifest),
            target=target,
            store=store,
            config_sha256="config",
        )

    files = {"kinmu": ("勤務規程 2023 年度版", True), "keihi": ("経費精算の手順", True)}
    report = await sync(files)
    assert (report.added, report.published, report.unreflected) == (2, 0, 2)
    state = store.load(source_id)
    kinmu_v1 = state.items["kinmu"].pending_document_id
    keihi = state.items["keihi"].pending_document_id
    assert kinmu_v1 and keihi
    jobs = await client.list_document_ingestion_jobs(kinmu_v1)
    assert [job.status for job in jobs] == [IngestionJobStatus.QUEUED]
    assert {ref.id for ref in (await client.get_document(kinmu_v1)).knowledge_bases} == {kb.id}  # type: ignore[union-attr]

    # 取込が済んだら公開する。同じ内容の再通知では文書を増やさない。
    await _finish_ingestion(client, kinmu_v1)
    await _finish_ingestion(client, keihi)
    report = await sync(files)
    assert (report.published, report.added, report.unchanged) == (2, 0, 2)
    report = await sync(files)
    assert (report.added, report.updated, report.published) == (0, 0, 0)

    # 内容の更新: 取込が済むまで前の版が公開のまま、済んだら前の版を置き換え済みにする。
    files["kinmu"] = ("勤務規程 2025 年度版", True)
    report = await sync(files)
    kinmu_v2 = store.load(source_id).items["kinmu"].pending_document_id
    assert report.updated == 1 and kinmu_v2 and kinmu_v2 != kinmu_v1
    old = await client.get_document(kinmu_v1)
    assert old is not None and old.superseded_by_document_id is None
    await _finish_ingestion(client, kinmu_v2)
    report = await sync(files)
    assert report.published == 1
    old = await client.get_document(kinmu_v1)
    assert old is not None and old.superseded_by_document_id == kinmu_v2

    # 権限の撤回: 公開中の版と前の版を消す。
    files["kinmu"] = ("勤務規程 2025 年度版", False)
    report = await sync(files)
    assert report.revoked == 1
    assert await client.get_document(kinmu_v2) is None
    assert await client.get_document(kinmu_v1) is None

    # 取込の待機中の文書を撤回する: job を取り消し、次の同期で消す。
    files["keihi"] = ("経費精算の手順 改訂", True)
    await sync(files)
    keihi_v2 = store.load(source_id).items["keihi"].pending_document_id
    assert keihi_v2
    files["keihi"] = ("経費精算の手順 改訂", False)
    report = await sync(files)
    assert report.status == "partial"
    assert await client.get_document(keihi) is None
    assert [job.status for job in await client.list_document_ingestion_jobs(keihi_v2)] == [
        IngestionJobStatus.CANCELLED
    ]
    report = await sync(files)
    assert await client.get_document(keihi_v2) is None
    assert store.load(source_id).items["keihi"].status == "revoked"
    assert report.unreflected == 0
