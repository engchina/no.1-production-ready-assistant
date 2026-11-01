"""知識源(manifest)を、製品の文書の取込へ差分で同期する CLI(#77)。

同期の契約は ``app.rag.source_sync``。この CLI は取込先を製品の書き込みの経路にする:

- 取込: 原本を保存先に置き(``source-sync/<key>/<ファイル名>``。同じ key は同じ path)、
  文書を作って取込 job を待機に入れる(一括の取込と同じ ``accept_bulk_ingestion``)。job は
  取込 worker が処理する。同じ内容の文書が既にあれば、製品の重複の扱い(正本の索引を使う)になる。
- 公開: 取込の済んだ新しい版で前の版を置き換える(``set_document_superseded_by``。前の版は
  検索から外れ、版の履歴として残る)。
- 削除・撤回: 文書の削除と同じ後始末(``DELETE /api/documents/{id}`` と同じ関数)。取込の
  実行中なら job を取り消して、次の同期で消し直す。

使い方(rag/backend で。設定の JSON は利用者が編集し、状態の JSON は CLI だけが書く):

  uv run python -m app.rag.source_sync_cli --config /path/to/manuals.sync.json

設定の例::

  {"source_id": "manuals", "manifest": "manifest.json",
   "knowledge_base_ids": ["<KB の ID>"], "page_size": 50, "max_attempts": 3}

結果(件数・状態だけ。本文・ファイルの中身は出さない)を JSON で標準出力に出す。終了コードは
``succeeded`` で 0、``partial`` で 1、``listing_failed`` で 2。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.api.routes import documents as documents_route
from app.clients.file_storage import FileStorageClient
from app.clients.oracle import OracleClient
from app.clients.oracle_bulk_ingestion_store import OracleBulkIngestionStore
from app.config import Settings, get_settings
from app.rag.bulk_ingestion import REASON_DUPLICATE_CONTENT, accept_bulk_ingestion
from app.rag.request_context import with_system_access
from app.rag.source_sync import (
    DocumentState,
    ManifestSource,
    SourceItem,
    SyncAlreadyRunningError,
    SyncItemRejected,
    SyncStateStore,
    run_sync,
)
from app.schemas.document import (
    BulkIngestionOutcome,
    DocumentProcessingConfig,
    FileStatus,
    IngestionJob,
    IngestionJobStatus,
)

_EXIT_CODES = {"succeeded": 0, "partial": 1, "listing_failed": 2}
_ACTIVE_JOB_STATUSES = (IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING)
_CANCELLED_MESSAGE = "知識源の同期で削除・撤回したため取り消しました。"


class SyncConfig(BaseModel):
    """利用者が編集する同期の設定(実行の状態とは別のファイル)。"""

    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    manifest: Path
    knowledge_base_ids: list[str] = Field(default_factory=list)
    page_size: int = Field(default=50, ge=1, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=20)


def _safe_file_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
    return cleaned.strip("._") or "document.txt"


def _resolve_settings(config: DocumentProcessingConfig) -> Settings:
    return documents_route._merge_document_processing_config(config)[0]


async def _not_fresh(document_id: str) -> IngestionJob:
    raise RuntimeError(f"同期で作った文書は新しい文書のはず: {document_id}")


class ProductSyncTarget:
    """``SyncTarget`` を製品の文書・取込・版・削除の経路で実装する。"""

    def __init__(
        self,
        oracle: OracleClient,
        storage: FileStorageClient,
        *,
        knowledge_base_ids: Sequence[str],
        settings: Settings,
    ) -> None:
        self._oracle = oracle
        self._storage = storage
        self._knowledge_base_ids = list(knowledge_base_ids)
        self._settings = settings

    async def stage(self, *, key: str, item: SourceItem, data: bytes) -> str:
        file_name = _safe_file_name(item.name)
        object_path = await self._storage.put(
            key=f"source-sync/{key}/{file_name}", data=data, content_type=item.content_type
        )
        existing = await self._oracle.find_document_id_by_object_storage_path(object_path)
        if existing is not None:
            return existing  # 取込に出した直後に止まった同期の再開
        content_sha256 = hashlib.sha256(data).hexdigest()
        duplicate = await self._oracle.find_document_by_content_hash(content_sha256)
        detail = await self._oracle.create_document(
            file_name=file_name,
            object_storage_path=object_path,
            content_type=item.content_type,
            file_size_bytes=len(data),
            content_sha256=content_sha256,
            duplicate_of_document_id=duplicate.id if duplicate is not None else None,
            knowledge_base_ids=self._knowledge_base_ids or None,
        )
        result = await accept_bulk_ingestion(
            [detail.id],
            store=OracleBulkIngestionStore(self._oracle),
            resolve_settings=_resolve_settings,
            enqueue_single=_not_fresh,
            max_attempts=self._settings.ingestion_job_max_attempts,
        )
        decision = result.items[0]
        if decision.outcome == BulkIngestionOutcome.ACCEPTED or (
            decision.outcome == BulkIngestionOutcome.SKIPPED
            and decision.reason == REASON_DUPLICATE_CONTENT
        ):
            return detail.id
        # 受け付けられなかった文書は残さない(次の同期で同じ key から作り直す)。
        await self.remove(detail.id)
        raise SyncItemRejected(decision.reason or decision.outcome.value)

    async def state(self, document_id: str) -> DocumentState:
        detail = await self._oracle.get_document(document_id)
        if detail is None:
            return "missing"
        if detail.status == FileStatus.INDEXED:
            return "indexed"
        if detail.status == FileStatus.ERROR:
            return "failed"
        if detail.duplicate_of_document_id is not None:
            canonical = await self._oracle.get_document(detail.duplicate_of_document_id)
            if canonical is not None and canonical.status == FileStatus.INDEXED:
                return "indexed"
        jobs = await self._oracle.list_document_ingestion_jobs(document_id)
        if jobs and all(job.status == IngestionJobStatus.CANCELLED for job in jobs):
            return "failed"
        return "pending"

    async def publish(self, *, previous: str | None, document_id: str) -> None:
        if previous is not None:
            await self._oracle.set_document_superseded_by(previous, document_id)

    async def remove(self, document_id: str) -> bool:
        active = [
            job
            for status in _ACTIVE_JOB_STATUSES
            for job in await self._oracle.list_document_ingestion_jobs(document_id, status=status)
        ]
        if active:
            for job in active:
                await self._oracle.transition_ingestion_job(
                    job.id,
                    from_statuses=_ACTIVE_JOB_STATUSES,
                    to_status=IngestionJobStatus.CANCELLED,
                    error_message=_CANCELLED_MESSAGE,
                    finished_at=datetime.now(UTC),
                )
            return False
        try:
            await documents_route.delete_document(document_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return True
            if exc.status_code == 409:
                return False
            raise
        return True


def load_config(path: Path) -> tuple[SyncConfig, str]:
    """設定と、その内容の sha256(設定が変わったら途中の一覧を捨てる)。"""
    text = path.read_text(encoding="utf-8")
    config = SyncConfig.model_validate_json(text)
    if not config.manifest.is_absolute():
        config.manifest = (path.parent / config.manifest).resolve()
    return config, hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _run(config_path: Path, state_path: Path | None) -> int:
    config, config_sha256 = load_config(config_path)
    settings = get_settings()
    store = SyncStateStore(
        state_path or config_path.with_name(f"{config.source_id}.sync-state.json")
    )
    target = ProductSyncTarget(
        OracleClient(settings),
        FileStorageClient(settings),
        knowledge_base_ids=config.knowledge_base_ids,
        settings=settings,
    )
    report = await run_sync(
        source_id=config.source_id,
        source=ManifestSource(config.manifest, page_size=config.page_size),
        target=target,
        store=store,
        config_sha256=config_sha256,
        max_attempts=config.max_attempts,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return _EXIT_CODES[report.status]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="同期の設定(JSON)")
    parser.add_argument(
        "--state", type=Path, help="実行の状態(JSON。既定は設定の隣の <source_id>.sync-state.json)"
    )
    return parser


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args.config, args.state))
    except SyncAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (OSError, ValidationError) as exc:
        print(f"設定を読めません: {type(exc).__name__}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
