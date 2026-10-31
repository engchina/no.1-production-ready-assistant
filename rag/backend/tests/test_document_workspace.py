"""文書プレビュー（原本配信）と抽出本文表示用 API のテスト。"""

import hashlib
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import anyio
import pytest

from app.api.routes import documents as documents_route
from app.clients.document_cleanup_store import DocumentCleanupTargets, DocumentCleanupTask
from app.clients.object_storage import ObjectStorageClient
from app.clients.oracle import DocumentReviewEditConflictError, ReviewedExtractionWrite
from app.config import Settings, get_settings
from app.main import app
from app.rag.document_cleanup import retry_due_document_cleanups
from app.rag.request_context import current_audit_request_context
from app.rag.source_profile import (
    AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE,
    EPUB_UPLOAD_REJECTED_MESSAGE,
    HEIC_UPLOAD_REJECTED_MESSAGE,
)
from app.schemas.document import (
    DocumentChunkView,
    DocumentDetail,
    DocumentExtractionExportFormat,
    DocumentPreprocessArtifact,
    DocumentProcessingConfig,
    DocumentSummary,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
    IngestionSegment,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from app.schemas.knowledge_base import KnowledgeBaseRef
from tests.language_release_support import install_language_release_stub
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def test_structured_table_html_preserves_formula_cell_metadata() -> None:
    """review HTML は table cell formula lineage を data 属性として安全に出す。"""
    table = ExtractionTable(
        table_id="tbl-1",
        cells=[
            ExtractionTableCell(
                row=1,
                col=1,
                text="<INDEXED>",
                metadata={
                    "formula_cell_ref": "B2",
                    "equation_format": "excel_formula",
                    "formula": 'IF(A2="状態","<INDEXED>","ERROR")',
                    "formula_value": "<INDEXED>",
                },
            )
        ],
    )

    html = documents_route._structured_table_html(table, "")

    assert 'data-formula-ref="B2"' in html
    assert 'data-formula-format="excel_formula"' in html
    assert 'data-formula-value="&lt;INDEXED&gt;"' in html
    assert "&lt;INDEXED&gt;" in html
    assert "<INDEXED>" not in html


def test_ingestion_job_error_message_preserves_safe_errors() -> None:
    """job 表示では利用者が直せる設定エラーだけ原因を表示する。"""

    class SafeConfigError(RuntimeError):
        safe_for_user = True

    assert (
        documents_route._safe_ingestion_job_error_message(
            SafeConfigError("OCI 認証設定の fingerprint が不正です。")
        )
        == "OCI 認証設定の fingerprint が不正です。"
    )
    assert (
        documents_route._safe_ingestion_job_error_message(RuntimeError("secret detail"))
        == "取込処理に失敗しました。"
    )


class FakeWorkspaceOracle:
    """文書 workspace API テスト用のインメモリ Oracle fake。"""

    def __init__(self) -> None:
        self.documents: dict[str, DocumentDetail] = {}
        self.chunks: dict[str, list[DocumentChunkView]] = {}
        self.ingestion_jobs: dict[str, IngestionJob] = {}
        self.ingestion_segments: dict[str, list[IngestionSegment]] = {}
        self.knowledge_base_assignments: set[tuple[str, str]] = set()
        self.processing_configs: dict[str, DocumentProcessingConfig] = {}
        self.recipes: dict[str, dict[str, object]] = {}
        self.extraction_artifacts: dict[str, dict[str, object]] = {}
        self.chunk_set_chunks: dict[str, list[DocumentChunkView]] = {}
        # 文書の削除の後始末の記録（#2452）。
        self.cleanup_store = FakeDocumentCleanupStore(self)

    async def find_document_by_content_hash(self, content_sha256: str) -> DocumentSummary | None:
        for detail in self.documents.values():
            if detail.content_sha256 == content_sha256:
                return DocumentSummary.model_validate(detail.model_dump())
        return None

    async def create_document(
        self,
        *,
        file_name: str,
        object_storage_path: str,
        content_type: str | None,
        file_size_bytes: int | None,
        content_sha256: str | None,
        duplicate_of_document_id: str | None,
        knowledge_base_ids: list[str] | None = None,
    ) -> DocumentDetail:
        document_id = uuid4().hex
        knowledge_bases = [
            KnowledgeBaseRef(id=knowledge_base_id, name=f"KB {knowledge_base_id}")
            for knowledge_base_id in knowledge_base_ids or ["kb-default"]
        ]
        detail = DocumentDetail(
            id=document_id,
            file_name=file_name,
            status=FileStatus.UPLOADED,
            object_storage_path=object_storage_path,
            content_type=content_type,
            file_size_bytes=file_size_bytes,
            content_sha256=content_sha256,
            duplicate_of_document_id=duplicate_of_document_id,
            uploaded_at=datetime.now(UTC),
            knowledge_bases=knowledge_bases,
        )
        self.documents[document_id] = detail
        for knowledge_base in knowledge_bases:
            self.knowledge_base_assignments.add((knowledge_base.id, document_id))
        return detail

    async def assign_documents_to_knowledge_base(
        self,
        knowledge_base_id: str,
        document_ids: list[str],
    ) -> object:
        for document_id in document_ids:
            detail = self.documents[document_id]
            if not any(ref.id == knowledge_base_id for ref in detail.knowledge_bases):
                detail = detail.model_copy(
                    update={
                        "knowledge_bases": [
                            *detail.knowledge_bases,
                            KnowledgeBaseRef(
                                id=knowledge_base_id,
                                name=f"KB {knowledge_base_id}",
                            ),
                        ]
                    }
                )
                self.documents[document_id] = detail
            self.knowledge_base_assignments.add((knowledge_base_id, document_id))
        return object()

    async def list_documents(
        self,
        status: FileStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
    ) -> list[DocumentSummary]:
        if queued_phase is not None:
            # fake は取込の job の順番待ちを持たない。
            return []
        documents = list(self.documents.values())
        if status is not None:
            documents = [document for document in documents if document.status == status]
        if query:
            normalized_query = query.casefold()
            documents = [
                document
                for document in documents
                if normalized_query in document.file_name.casefold()
                or (
                    document.category_name is not None
                    and normalized_query in document.category_name.casefold()
                )
            ]
        if knowledge_base_id:
            documents = [
                document
                for document in documents
                if any(ref.id == knowledge_base_id for ref in document.knowledge_bases)
            ]
        documents = sorted(documents, key=lambda document: document.uploaded_at, reverse=True)
        sliced = documents[offset : offset + limit] if limit is not None else documents[offset:]
        return [DocumentSummary.model_validate(document.model_dump()) for document in sliced]

    async def count_documents(
        self,
        status: FileStatus | None = None,
        query: str | None = None,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
    ) -> int:
        return len(
            await self.list_documents(
                status=status,
                queued_phase=queued_phase,
                query=query,
                knowledge_base_id=knowledge_base_id,
            )
        )

    async def list_document_chunks(self, document_id: str) -> list[DocumentChunkView]:
        return list(self.chunks.get(document_id, []))

    async def count_document_chunks(self, document_id: str) -> int:
        return len(self.chunks.get(document_id, []))

    async def list_document_knowledge_bases(self, document_id: str) -> list[KnowledgeBaseRef]:
        return list(self.documents[document_id].knowledge_bases)

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        return list(self.ingestion_segments.get(document_id, []))

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        return self.documents.get(document_id)

    async def document_exists(self, document_id: str) -> bool:
        return document_id in self.documents

    async def get_document_summary(self, document_id: str) -> DocumentDetail | None:
        # 実装は JSON 列を読まない DocumentSummary を返す。fake は詳細(その上位型)で代用する。
        return self.documents.get(document_id)

    async def get_document_processing_config(self, document_id: str) -> DocumentProcessingConfig:
        return self.processing_configs.get(document_id, DocumentProcessingConfig())

    async def update_document_processing_config(
        self, document_id: str, config: DocumentProcessingConfig
    ) -> DocumentProcessingConfig:
        self.processing_configs[document_id] = config
        return config

    async def get_document_serving_chunk_set_id(self, document_id: str) -> None:
        _ = document_id
        return None

    async def list_document_knowledge_base_configs(
        self, document_id: str
    ) -> list[tuple[str, object]]:
        # 所属 KB 無し → plan None で従来の単一 materialization 経路へ縮退する。
        _ = document_id
        return []

    async def list_duplicate_documents(self, document_ids: list[str]) -> list[DocumentSummary]:
        return [
            DocumentSummary.model_validate(detail.model_dump())
            for detail in self.documents.values()
            if detail.duplicate_of_document_id in document_ids
        ]

    async def delete_document(self, document_id: str) -> bool:
        if document_id not in self.documents:
            return False
        del self.documents[document_id]
        self.ingestion_segments.pop(document_id, None)
        self.ingestion_jobs = {
            job_id: job
            for job_id, job in self.ingestion_jobs.items()
            if job.document_id != document_id
        }
        for duplicate_id, detail in list(self.documents.items()):
            if detail.duplicate_of_document_id == document_id:
                self.documents[duplicate_id] = detail.model_copy(
                    update={"duplicate_of_document_id": None}
                )
        return True

    async def update_document_status(
        self,
        document_id: str,
        status: FileStatus,
        error_message: str | None = None,
    ) -> DocumentDetail:
        detail = self.documents[document_id]
        indexed_at = datetime.now(UTC) if status == FileStatus.INDEXED else detail.indexed_at
        updated = detail.model_copy(
            update={
                "status": status,
                "indexed_at": indexed_at,
                "error_message": error_message,
            }
        )
        self.documents[document_id] = updated
        return updated

    async def reset_document_ingestion_outputs(
        self,
        document_id: str,
        *,
        status: FileStatus = FileStatus.UPLOADED,
        error_message: str | None = None,
        clear_preprocess_artifact: bool = False,
    ) -> DocumentDetail:
        detail = self.documents[document_id]
        updated = detail.model_copy(
            update={
                "status": status,
                "preprocess_artifact": (
                    None if clear_preprocess_artifact else detail.preprocess_artifact
                ),
                "extraction": {},
                "error_message": error_message,
                "indexed_at": None,
            }
        )
        self.documents[document_id] = updated
        self.chunks.pop(document_id, None)
        self.ingestion_segments.pop(document_id, None)
        return updated

    async def save_preprocess_artifact(
        self,
        document_id: str,
        artifact: DocumentPreprocessArtifact | None,
    ) -> DocumentDetail:
        detail = self.documents[document_id]
        updated = detail.model_copy(update={"preprocess_artifact": artifact})
        self.documents[document_id] = updated
        return updated

    async def reset_document_chunk_outputs(
        self,
        document_id: str,
        *,
        status: FileStatus = FileStatus.REVIEW,
        error_message: str | None = None,
    ) -> DocumentDetail:
        detail = self.documents[document_id]
        updated = detail.model_copy(
            update={
                "status": status,
                "error_message": error_message,
                "indexed_at": None,
            }
        )
        self.documents[document_id] = updated
        self.chunks.pop(document_id, None)
        return updated

    async def reset_document_index_outputs(
        self,
        document_id: str,
        *,
        status: FileStatus = FileStatus.CHUNKED,
        error_message: str | None = None,
    ) -> DocumentDetail:
        detail = self.documents[document_id]
        updated = detail.model_copy(
            update={
                "status": status,
                "error_message": error_message,
                "indexed_at": None,
            }
        )
        self.documents[document_id] = updated
        return updated

    async def create_ingestion_job(self, job: IngestionJob) -> IngestionJob:
        self.ingestion_jobs[job.id] = job
        return job

    async def pin_ingestion_job_language_pack(self, job_id: str, release_id: str) -> str:
        job = self.ingestion_jobs[job_id]
        identity = job.language_pack_release_id or release_id
        self.ingestion_jobs[job_id] = job.model_copy(update={"language_pack_release_id": identity})
        return identity

    async def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        return self.ingestion_jobs.get(job_id)

    async def list_ingestion_jobs(
        self,
        *,
        status: IngestionJobStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[IngestionJob]:
        jobs = [
            job for job in self.ingestion_jobs.values() if status is None or job.status == status
        ]
        sorted_jobs = sorted(
            jobs,
            key=lambda job: job.queued_at,
            reverse=True,
        )
        return sorted_jobs[offset : offset + limit] if limit is not None else sorted_jobs[offset:]

    async def list_document_ingestion_jobs(
        self,
        document_id: str,
        *,
        status: IngestionJobStatus | None = None,
    ) -> list[IngestionJob]:
        return [
            job
            for job in self.ingestion_jobs.values()
            if job.document_id == document_id and (status is None or job.status == status)
        ]

    async def count_ingestion_jobs(
        self,
        *,
        status: IngestionJobStatus | None = None,
    ) -> int:
        return len(
            [job for job in self.ingestion_jobs.values() if status is None or job.status == status]
        )

    async def recover_stale_ingestion_jobs(
        self,
        *,
        stale_before: datetime,
        limit: int,
    ) -> list[IngestionJob]:
        stale_jobs = [
            job
            for job in self.ingestion_jobs.values()
            if job.status == IngestionJobStatus.RUNNING
            and (job.started_at or job.queued_at) < stale_before
        ][:limit]
        for job in stale_jobs:
            status = (
                IngestionJobStatus.FAILED
                if job.attempt_count >= job.max_attempts
                else IngestionJobStatus.QUEUED
            )
            self.ingestion_jobs[job.id] = job.model_copy(
                update={
                    "status": status,
                    "started_at": None if status == IngestionJobStatus.QUEUED else job.started_at,
                    "finished_at": (
                        datetime.now(UTC) if status == IngestionJobStatus.FAILED else None
                    ),
                }
            )
        return stale_jobs

    async def claim_ingestion_job(
        self,
        job_id: str,
        *,
        started_at: datetime,
        lease_owner: str | None = None,
    ) -> IngestionJob | None:
        job = self.ingestion_jobs.get(job_id)
        if job is None or job.status != IngestionJobStatus.QUEUED:
            return None
        claimed = job.model_copy(
            update={
                "status": IngestionJobStatus.RUNNING,
                "attempt_count": job.attempt_count + 1,
                "started_at": started_at,
                "error_message": None,
                "finished_at": None,
            }
        )
        self.ingestion_jobs[job_id] = claimed
        return claimed

    async def transition_ingestion_job(
        self,
        job_id: str,
        *,
        from_statuses: Collection[IngestionJobStatus],
        to_status: IngestionJobStatus,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        lease_owner: str | None = None,
    ) -> IngestionJob | None:
        # lease を持たない実行(lease_owner なし)の経路だけを扱う(lease の条件は #359 のテスト)。
        assert lease_owner is None
        # 実 Oracle の `WHERE status IN (...)` と同じく、遷移元の状態のときだけ書く。
        job = self.ingestion_jobs.get(job_id)
        if job is None or job.status not in from_statuses:
            return None
        updated = job.model_copy(
            update={
                "status": to_status,
                "error_message": error_message,
                "finished_at": finished_at,
            }
        )
        self.ingestion_jobs[job_id] = updated
        return updated

    async def get_document_recipe(
        self, document_id: str, recipe_id: str
    ) -> dict[str, object] | None:
        row = self.recipes.get(recipe_id)
        if row is None or row.get("document_id") != document_id:
            return None
        return dict(row)

    async def update_document_recipe_status(
        self, *, recipe_id: str, status: FileStatus, **kwargs: object
    ) -> None:
        row = self.recipes.setdefault(recipe_id, {"recipe_id": recipe_id})
        row["status"] = status.value
        for key, value in kwargs.items():
            if value is not None:
                row[key] = value

    async def upsert_chunk_set(self, **kwargs: object) -> None:
        _ = kwargs

    async def mark_chunk_set_indexed(self, **kwargs: object) -> None:
        _ = kwargs

    async def delete_stale_document_chunk_sets(self, **kwargs: object) -> None:
        _ = kwargs

    async def set_document_serving_chunk_set(self, **kwargs: object) -> None:
        _ = kwargs

    async def upsert_document_extraction_artifact(self, **kwargs: object) -> None:
        extraction_recipe_id = kwargs.get("extraction_recipe_id")
        if isinstance(extraction_recipe_id, str) and "extraction" in kwargs:
            self.extraction_artifacts[extraction_recipe_id] = {
                "extraction_json": kwargs["extraction"],
                "recipe_subset": kwargs.get("recipe_subset"),
                "status": kwargs.get("status"),
            }

    async def get_document_extraction_artifact(
        self, *, document_id: str, extraction_recipe_id: str
    ) -> dict[str, object] | None:
        _ = document_id
        artifact = self.extraction_artifacts.get(extraction_recipe_id)
        return dict(artifact) if artifact is not None else None

    async def save_document_recipe_review_extraction(
        self,
        *,
        document_id: str,
        recipe_id: str,
        build: Callable[[Mapping[str, object], Mapping[str, object]], ReviewedExtractionWrite],
    ) -> str:
        """実 Oracle がレシピの行ロックの下で行う、読み・修正・書きをまとめた fake(#2616)。"""
        row = self.recipes.get(recipe_id)
        if row is None or row.get("document_id") != document_id:
            raise KeyError("レシピが見つかりません。")
        if row.get("status") != FileStatus.REVIEW.value:
            raise DocumentReviewEditConflictError("確認待ちのレシピのみ修正できます。")
        if any(
            job.recipe_id == recipe_id
            and job.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING}
            for job in self.ingestion_jobs.values()
        ):
            raise DocumentReviewEditConflictError("このレシピは処理中または待機中です。")
        source_id = row.get("active_extraction_recipe_id")
        artifact = self.extraction_artifacts.get(str(source_id)) if source_id else None
        if artifact is None or not artifact.get("extraction_json"):
            raise DocumentReviewEditConflictError("修正対象の抽出結果がありません。")
        write = build(dict(row), dict(artifact))
        self.extraction_artifacts[write.extraction_recipe_id] = {
            "extraction_json": dict(write.extraction),
            "recipe_subset": write.recipe_subset,
            "status": write.status,
        }
        row["active_extraction_recipe_id"] = write.extraction_recipe_id
        return write.extraction_recipe_id

    async def list_chunk_set_chunks(self, chunk_set_id: str) -> list[DocumentChunkView]:
        return list(self.chunk_set_chunks.get(chunk_set_id, []))


class FakeWorkspaceIngestionPipeline:
    """取込 API テストで外部 AI/embedding を呼ばずに抽出結果を保存する fake。"""

    last_settings: Settings | None = None

    def __init__(self, *, oracle: FakeWorkspaceOracle, settings: Settings | None = None) -> None:
        self._oracle = oracle
        self._settings = settings
        FakeWorkspaceIngestionPipeline.last_settings = settings

    async def ingest(
        self,
        document_id: str,
        image_bytes: bytes,
        prompt: str,
        *,
        content_type: str = "application/octet-stream",
        source_profile: object | None = None,
        chunk_set_id: str | None = None,
        original_object_storage_path: str | None = None,
        prepared_artifact: DocumentPreprocessArtifact | None = None,
        cancel_checker: object | None = None,
    ) -> DocumentDetail:
        _ = prompt, source_profile, chunk_set_id, cancel_checker
        detail = await self._oracle.get_document(document_id)
        assert detail is not None
        if prepared_artifact is None:
            await self._oracle.save_preprocess_artifact(
                document_id,
                DocumentPreprocessArtifact(
                    derivation_id="fake-passthrough",
                    profile="none",
                    converted=False,
                    source_content_type=detail.content_type,
                    source_sha256=detail.content_sha256,
                    object_storage_path=original_object_storage_path,
                    content_type=content_type,
                    sha256=hashlib.sha256(image_bytes).hexdigest(),
                    file_name=f"{detail.file_name.rsplit('.', 1)[0]}__prepared.txt",
                ),
            )
            detail = await self._oracle.get_document(document_id)
            assert detail is not None
        raw_text = image_bytes.decode("utf-8", errors="replace")
        self._oracle.documents[document_id] = detail.model_copy(
            update={
                "extraction": {
                    "document_type": "社内規程",
                    "raw_text": raw_text,
                    "confidence": 0.9,
                }
            }
        )
        return await self._oracle.update_document_status(document_id, FileStatus.INDEXED)


class FakeDocumentCleanupStore:
    """文書の削除の後始末の記録（#2452）の fake。文書の行の削除と記録を 1 回で行う。"""

    def __init__(self, oracle: FakeWorkspaceOracle) -> None:
        self.oracle = oracle
        self.tasks: dict[str, DocumentCleanupTask] = {}
        self.owners: dict[str, str | None] = {}

    def pending_for(self, document_id: str) -> list[DocumentCleanupTask]:
        return [task for task in self.tasks.values() if task.document_id == document_id]

    async def delete_document_with_cleanup(
        self, document_id: str, file_name: str, targets: DocumentCleanupTargets
    ) -> DocumentCleanupTask | None:
        if not await self.oracle.delete_document(document_id):
            return None
        task = DocumentCleanupTask(
            cleanup_id=uuid4().hex, document_id=document_id, file_name=file_name, targets=targets
        )
        if not targets.is_empty:
            self.tasks[task.cleanup_id] = task
            self.owners[task.cleanup_id] = current_audit_request_context().user_id_hash
        return task

    async def find_for_document(self, document_id: str) -> DocumentCleanupTask | None:
        context = current_audit_request_context()
        for task in self.pending_for(document_id):
            if context.all_users_visible or self.owners[task.cleanup_id] == context.user_id_hash:
                return task
        return None

    async def list_due(self, *, limit: int, min_age_seconds: float) -> list[DocumentCleanupTask]:
        _ = min_age_seconds
        return list(self.tasks.values())[:limit]

    async def save_progress(
        self, cleanup_id: str, remaining: DocumentCleanupTargets, *, error_type: str | None
    ) -> None:
        _ = error_type
        task = self.tasks.get(cleanup_id)
        if task is None:
            return
        if remaining.is_empty:
            del self.tasks[cleanup_id]
            return
        self.tasks[cleanup_id] = DocumentCleanupTask(
            cleanup_id=cleanup_id,
            document_id=task.document_id,
            file_name=task.file_name,
            targets=remaining,
            attempt_count=task.attempt_count + 1,
        )

    async def referenced_uris(self, uris: Sequence[str]) -> set[str]:
        referenced: set[str] = set()
        for detail in self.oracle.documents.values():
            referenced.add(detail.object_storage_path or "")
            if detail.preprocess_artifact is not None:
                referenced.add(detail.preprocess_artifact.object_storage_path or "")
        for segments in self.oracle.ingestion_segments.values():
            referenced.update(segment.artifact_path or "" for segment in segments)
        return {uri for uri in uris if uri in referenced}

    async def document_exists(self, document_id: str) -> bool:
        return document_id in self.oracle.documents


@pytest.fixture
def fake_cleanup_store(fake_document_dependencies: FakeWorkspaceOracle) -> FakeDocumentCleanupStore:
    return fake_document_dependencies.cleanup_store


@pytest.fixture(autouse=True)
def fake_document_dependencies(monkeypatch: pytest.MonkeyPatch) -> FakeWorkspaceOracle:
    install_language_release_stub(monkeypatch)
    fake_oracle = FakeWorkspaceOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake_oracle)
    monkeypatch.setattr(
        documents_route, "DocumentCleanupStore", lambda oracle: fake_oracle.cleanup_store
    )
    monkeypatch.setattr(documents_route, "IngestionPipeline", FakeWorkspaceIngestionPipeline)
    FakeWorkspaceIngestionPipeline.last_settings = None
    return fake_oracle


def _seed_recipe(
    fake: FakeWorkspaceOracle,
    document_id: str,
    *,
    status: FileStatus,
    extraction: Mapping[str, object] | None = None,
    chunks: list[DocumentChunkView] | None = None,
    preprocess_artifact: DocumentPreprocessArtifact | None = None,
) -> str:
    """文書にレシピ 1 を用意し、抽出結果(レシピの extraction artifact)と chunk を紐づける。"""
    recipe_id = "recipe-1"
    now = datetime.now(UTC)
    row: dict[str, object] = {
        "recipe_id": recipe_id,
        "document_id": document_id,
        "slot_no": 1,
        "status": status.value,
        "config_revision": 1,
        "processing_config": {},
        "created_at": now,
        "updated_at": now,
    }
    if extraction is not None:
        row["active_extraction_recipe_id"] = "er-recipe-1"
        fake.extraction_artifacts["er-recipe-1"] = {
            "extraction_json": dict(extraction),
            "recipe_subset": {},
            "status": "materialized",
        }
    if chunks is not None:
        row["active_chunk_set_id"] = "cs-recipe-1"
        fake.chunk_set_chunks["cs-recipe-1"] = chunks
    if preprocess_artifact is not None:
        row["preprocess_artifact"] = preprocess_artifact.model_dump(mode="json")
    fake.recipes[recipe_id] = row
    return recipe_id


def _upload(file_name: str, body: bytes, content_type: str) -> str:
    resp = client.post(
        "/api/documents/upload",
        files={"file": (file_name, body, content_type)},
    )
    assert resp.status_code == 200
    return str(resp.json()["data"]["id"])


def _review_extraction_payload() -> dict[str, object]:
    return StructuredExtraction(
        raw_text="経費申請",
        elements=[
            DocumentElement(
                kind="title",
                text="経費申請",
                order=0,
                element_id="el-0000",
                page_number=1,
            )
        ],
    ).to_document_payload()


def test_recipe_review_edits_patch_saves_without_creating_chunks(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """PATCH はレシピを REVIEW のまま抽出結果だけ更新し、Chunk job は作らない。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction=_review_extraction_payload(),
    )

    response = client.patch(
        f"/api/documents/{document_id}/recipes/{recipe_id}/review-edits",
        json={
            "element_edits": [
                {
                    "element_id": "el-0000",
                    "text": "経費申請（レビュー済み）",
                    "base_text": "経費申請",
                }
            ]
        },
    )

    assert response.status_code == 200
    saved = response.json()["data"]
    assert saved["status"] == "REVIEW"
    # 共有の抽出結果は変えず、レシピ固有の抽出結果へ copy-on-write で保存する。
    edited_id = saved["active_extraction_recipe_id"]
    assert edited_id != "er-recipe-1"
    edited = fake_document_dependencies.extraction_artifacts[edited_id]["extraction_json"]
    assert isinstance(edited, dict)
    assert edited["raw_text"] == "経費申請（レビュー済み）"
    assert fake_document_dependencies.ingestion_jobs == {}


def test_recipe_review_edits_patch_rejects_unknown_element(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """存在しない要素 ID の修正は 400 にする。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction=_review_extraction_payload(),
    )

    response = client.patch(
        f"/api/documents/{document_id}/recipes/{recipe_id}/review-edits",
        json={"element_edits": [{"element_id": "missing", "text": "x", "base_text": ""}]},
    )

    assert response.status_code == 400


def test_recipe_review_edits_patch_requires_review_status(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """REVIEW でないレシピへは修正を保存しない(409)。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.UPLOADED,
        extraction=_review_extraction_payload(),
    )

    response = client.patch(
        f"/api/documents/{document_id}/recipes/{recipe_id}/review-edits",
        json={"element_edits": []},
    )

    assert response.status_code == 409


def test_recipe_review_edits_patch_rejects_stale_base_text(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """読み込んだ後にほかの保存で変わった要素への修正は 409 にし、成果を変えない(#2616)。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction=_review_extraction_payload(),
    )
    before = dict(fake_document_dependencies.extraction_artifacts)

    response = client.patch(
        f"/api/documents/{document_id}/recipes/{recipe_id}/review-edits",
        json={
            "element_edits": [{"element_id": "el-0000", "text": "新しい文", "base_text": "古い文"}]
        },
    )

    assert response.status_code == 409
    assert response.json()["error_messages"] == [
        "読み込んだ後に、ほかの操作で同じ箇所が保存されました。"
        "変更を破棄して読み込み直してから直してください。"
    ]
    assert fake_document_dependencies.extraction_artifacts == before
    assert fake_document_dependencies.recipes[recipe_id]["active_extraction_recipe_id"] == (
        "er-recipe-1"
    )


def test_recipe_review_edits_patch_after_approval_is_rejected(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """承認で CHUNK job を投入した後の下書き保存は 409 にする(承認した内容を後から変えない)。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction=_review_extraction_payload(),
    )
    approved = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")
    assert approved.status_code == 200

    response = client.patch(
        f"/api/documents/{document_id}/recipes/{recipe_id}/review-edits",
        json={
            "element_edits": [
                {"element_id": "el-0000", "text": "承認後の修正", "base_text": "経費申請"}
            ]
        },
    )

    assert response.status_code == 409
    assert response.json()["error_messages"] == ["このレシピは処理中または待機中です。"]
    assert fake_document_dependencies.recipes[recipe_id]["active_extraction_recipe_id"] == (
        "er-recipe-1"
    )


def test_document_upload_returns_assigned_knowledge_bases() -> None:
    """upload 時に指定した KB 所属はレスポンスへ返す。"""
    resp = client.post(
        "/api/documents/upload",
        data={"knowledge_base_ids": "kb-1,kb-2"},
        files={"file": ("policy.txt", b"sample", "text/plain")},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["knowledge_bases"] == [
        {"id": "kb-1", "name": "KB kb-1"},
        {"id": "kb-2", "name": "KB kb-2"},
    ]


def test_duplicate_upload_keeps_direct_knowledge_base_memberships(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """重複 upload は重複元 document の KB 所属を増やさない。"""
    body = b"%PDF-1.7\nsame manager profile"
    original = client.post(
        "/api/documents/upload",
        data={"knowledge_base_ids": "kb-2"},
        files={"file": ("私の上司.pdf", body, "application/pdf")},
    )
    duplicate = client.post(
        "/api/documents/upload",
        data={"knowledge_base_ids": "kb-1"},
        files={"file": ("私の上司.pdf", body, "application/pdf")},
    )

    assert original.status_code == 200
    assert duplicate.status_code == 200
    original_id = original.json()["data"]["id"]
    duplicate_id = duplicate.json()["data"]["id"]

    original_detail = client.get(f"/api/documents/{original_id}").json()["data"]
    duplicate_detail = client.get(f"/api/documents/{duplicate_id}").json()["data"]
    assert [ref["id"] for ref in original_detail["knowledge_bases"]] == ["kb-2"]
    assert [ref["id"] for ref in duplicate_detail["knowledge_bases"]] == ["kb-1"]

    listed = client.get("/api/documents").json()["data"]["items"]
    listed_by_id = {document["id"]: document for document in listed}
    assert [ref["id"] for ref in listed_by_id[original_id]["knowledge_bases"]] == ["kb-2"]
    assert [ref["id"] for ref in listed_by_id[duplicate_id]["knowledge_bases"]] == ["kb-1"]
    assert ("kb-1", original_id) not in fake_document_dependencies.knowledge_base_assignments


def test_document_upload_returns_source_profile() -> None:
    """upload レスポンスは原本品質と処理方針を source profile として返す。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": ("policy.txt", "本文".encode(), "text/plain")},
    )

    assert resp.status_code == 200
    profile = resp.json()["data"]["source_profile"]
    assert profile["original_file_name"] == "policy.txt"
    assert profile["sanitized_file_name"] == "policy.txt"
    assert profile["extension"] == ".txt"
    assert profile["content_type"] == "text/plain"
    assert profile["modality"] == "text"
    assert profile["parser_profile"] == "local_text_structure"
    assert profile["text_charset"] == "utf-8"
    assert profile["quality_status"] == "ready"
    assert profile["quality_warnings"] == []


def test_document_upload_accepts_tsv_as_local_text_table_source() -> None:
    """TSV upload は unknown 扱いにせず local table parser へ渡す。"""
    resp = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "metrics.tsv",
                b"name\tamount\nalpha\t1200\n",
                "text/tab-separated-values",
            )
        },
    )

    assert resp.status_code == 200
    profile = resp.json()["data"]["source_profile"]
    assert profile["extension"] == ".tsv"
    assert profile["content_type"] == "text/tab-separated-values"
    assert profile["modality"] == "text"
    assert profile["parser_profile"] == "local_text_structure"
    assert profile["parser_backend"] == "local_partition"
    assert profile["preview_kind"] == "text"
    assert profile["unsupported_reason"] is None


def test_document_upload_keeps_ingestion_manual_until_explicit_enqueue() -> None:
    """upload 後は保存だけ行い、明示操作で取込 job をキュー投入する。"""
    resp = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "policy.txt",
                "社内規程: 経費申請\n部門長が承認します。".encode(),
                "text/plain",
            )
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert "ingestion_job" not in body

    detail = client.get(f"/api/documents/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"

    enqueue = client.post(f"/api/documents/{body['id']}/ingestion-jobs")
    assert enqueue.status_code == 200
    job = enqueue.json()["data"]
    assert job["status"] == "QUEUED"
    assert job["parser_profile"] == "local_text_structure"

    job_detail = client.get(f"/api/documents/ingestion-jobs/{job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "QUEUED"

    anyio.run(documents_route._run_ingestion_job, job["id"])
    detail = client.get(f"/api/documents/{body['id']}")
    assert detail.status_code == 200
    indexed = detail.json()["data"]
    assert indexed["status"] == "INDEXED"
    assert "部門長が承認" in indexed["extraction"]["raw_text"]

    job_detail = client.get(f"/api/documents/ingestion-jobs/{job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "SUCCEEDED"
    assert job_detail.json()["data"]["attempt_count"] == 1


def test_document_processing_config_is_used_by_ingestion_pipeline() -> None:
    """レシピの上書きは KB を介さず、取込の Settings へ渡る（#488。レシピの job と同じ合成）。"""
    settings, effective = documents_route._merge_document_processing_config(
        DocumentProcessingConfig(
            parser_adapter_backend="unstructured", chunking_strategy="page_level"
        )
    )

    assert settings.rag_parser_adapter_backend == "unstructured"
    assert settings.rag_chunking_strategy == "page_level"
    assert effective.parser_adapter_backend == "unstructured"
    assert effective.chunking_strategy == "page_level"


def test_document_upload_has_no_ingestion_mode_and_never_enqueues() -> None:
    """upload は取込開始方針を受け取らない。旧 ``ingestion_mode`` を送っても job は作らない。"""
    resp = client.post(
        "/api/documents/upload",
        data={"ingestion_mode": "auto"},
        files={"file": ("policy.txt", b"body", "text/plain")},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert "ingestion_job" not in body
    jobs = client.get("/api/documents/ingestion-jobs")
    assert jobs.json()["data"]["items"] == []


def test_document_upload_rejects_unknown_octet_stream_before_storage() -> None:
    """application/octet-stream でも拡張子から未知の binary は保存前に 415 にする。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": ("payload.bin", b"\x00\x01\x02", "application/octet-stream")},
    )

    assert resp.status_code == 415
    assert resp.json()["error_messages"] == ["対応していないファイル形式です。"]


def test_document_upload_reports_unsupported_outlook_msg_without_enqueue() -> None:
    """Outlook MSG は未対応 email として source profile を返す。"""
    resp = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "approval.msg",
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1outlook msg",
                "application/vnd.ms-outlook",
            )
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert body["source_profile"]["modality"] == "email"
    assert body["source_profile"]["parser_profile"] == "unsupported_outlook_msg"
    assert body["source_profile"]["parser_backend"] == "unsupported"
    assert body["source_profile"]["preview_kind"] == "unsupported"
    assert body["source_profile"]["unsupported_reason"] == "outlook_msg_not_supported"
    assert "unsupported_outlook_msg" in body["source_profile"]["quality_warnings"]
    assert "ingestion_job" not in body

    detail = client.get(f"/api/documents/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_upload_reports_unsupported_tiff_image_without_enqueue() -> None:
    """TIFF 画像は upload 時に VLM へ送らず source profile を返す。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": ("scan.tiff", b"II*\x00tiff", "image/tiff")},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert body["source_profile"]["modality"] == "image"
    assert body["source_profile"]["parser_profile"] == "unsupported_tiff_image"
    assert body["source_profile"]["parser_backend"] == "unsupported"
    assert body["source_profile"]["preview_kind"] == "unsupported"
    assert body["source_profile"]["unsupported_reason"] == "tiff_image_not_supported"
    assert "unsupported_tiff_image" in body["source_profile"]["quality_warnings"]
    assert "ingestion_job" not in body

    detail = client.get(f"/api/documents/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_upload_reports_unsupported_legacy_office_without_enqueue() -> None:
    """旧バイナリ Office は upload 時に VLM へ送らず source profile を返す。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": ("legacy.doc", b"legacy office", "application/msword")},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert body["source_profile"]["modality"] == "office"
    assert body["source_profile"]["parser_profile"] == "unsupported_legacy_office_binary"
    assert body["source_profile"]["parser_backend"] == "unsupported"
    assert body["source_profile"]["preview_kind"] == "unsupported"
    assert body["source_profile"]["unsupported_reason"] == "legacy_office_binary_not_supported"
    assert "unsupported_legacy_office_binary" in body["source_profile"]["quality_warnings"]
    assert "ingestion_job" not in body

    detail = client.get(f"/api/documents/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_upload_duplicate_source_does_not_enqueue() -> None:
    """重複原本は upload 時に job を作らず、手動確認に残す。"""
    body = "同じ本文".encode()
    original = client.post(
        "/api/documents/upload",
        files={"file": ("original.txt", body, "text/plain")},
    )
    assert original.status_code == 200

    duplicate = client.post(
        "/api/documents/upload",
        files={"file": ("duplicate.txt", body, "text/plain")},
    )

    assert duplicate.status_code == 200
    payload = duplicate.json()["data"]
    assert payload["ingestion_started"] is False
    assert "ingestion_job" not in payload
    assert payload["duplicate_of_document_id"] == original.json()["data"]["id"]
    assert "duplicate_content" in payload["source_profile"]["quality_warnings"]

    enqueue = client.post(f"/api/documents/{payload['id']}/ingestion-jobs")
    assert enqueue.status_code == 200
    job = enqueue.json()["data"]
    assert job["status"] == "SKIPPED"
    assert job["skip_reason"] == "duplicate_content"

    detail = client.get(f"/api/documents/{payload['id']}")
    assert detail.status_code == 200
    detail_payload = detail.json()["data"]
    assert detail_payload["status"] == "UPLOADED"
    assert detail_payload["duplicate_source"]["id"] == original.json()["data"]["id"]
    assert detail_payload["duplicate_source"]["file_name"] == "original.txt"


def test_batch_upload_saves_documents_without_ingestion_jobs() -> None:
    """batch-upload は複数ファイルを保存し、取込 job は明示操作まで作らない。"""
    resp = client.post(
        "/api/documents/batch-upload",
        data={"knowledge_base_ids": "kb-1"},
        files=[
            ("files", ("policy-a.txt", "A 規程".encode(), "text/plain")),
            ("files", ("policy-b.txt", "B 規程".encode(), "text/plain")),
        ],
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_count"] == 2
    assert data["uploaded_count"] == 2
    # 取込 job を作らないため、job の状態を数える指標は返さない（#306）。
    assert "queued_count" not in data
    assert "skipped_count" not in data
    assert [item["file_name"] for item in data["items"]] == ["policy-a.txt", "policy-b.txt"]
    assert all(item["ingestion_started"] is False for item in data["items"])
    assert all("ingestion_job" not in item for item in data["items"])

    jobs = client.get("/api/documents/ingestion-jobs")
    assert jobs.status_code == 200
    listed = jobs.json()["data"]["items"]
    assert listed == []

    for item in data["items"]:
        detail = client.get(f"/api/documents/{item['id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["status"] == "UPLOADED"


def test_batch_upload_failed_item_includes_source_profile() -> None:
    """batch-upload の失敗 item は判定できた source profile を返す。"""
    resp = client.post(
        "/api/documents/batch-upload",
        files=[
            ("files", ("policy-ok.txt", b"OK", "text/plain")),
            ("files", ("policy.exe", b"MZ", "application/x-msdownload")),
        ],
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_count"] == 2
    assert data["uploaded_count"] == 1
    assert data["failed_count"] == 1
    assert data["items"][0]["file_name"] == "policy-ok.txt"
    failed = data["failed_items"][0]
    assert failed["file_name"] == "policy.exe"
    assert failed["status_code"] == 415
    assert failed["source_profile"]["modality"] == "unknown"
    assert failed["source_profile"]["parser_profile"] == "enterprise_ai_generic"
    assert failed["source_profile"]["preview_kind"] == "unsupported"
    assert failed["source_profile"]["unsupported_reason"] == "unknown_file_type"
    assert "unknown_modality" in failed["source_profile"]["quality_warnings"]


def test_document_ingestion_job_endpoint_queues_existing_document() -> None:
    """保存済み文書は文書単位で取込 job へ投入できる。"""
    document_id = _upload(
        "manual-policy.txt",
        "手動キュー投入対象".encode(),
        "text/plain",
    )

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["document_id"] == document_id
    assert job["status"] == "QUEUED"
    assert job["parser_profile"] == "local_text_structure"

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"

    job_detail = client.get(f"/api/documents/ingestion-jobs/{job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "QUEUED"

    anyio.run(documents_route._run_ingestion_job, job["id"])
    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "INDEXED"

    job_detail = client.get(f"/api/documents/ingestion-jobs/{job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "SUCCEEDED"


def test_external_ocr_failure_marks_job_failed_and_document_error(
    monkeypatch: pytest.MonkeyPatch,
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    message = "選択した文書解析サービス（Unstructured）の応答がタイムアウトしました。"

    class UnstructuredFailure(RuntimeError):
        safe_for_user = True

    class FailingUnstructuredPipeline(FakeWorkspaceIngestionPipeline):
        async def ingest(
            self,
            document_id: str,
            *_args: object,
            **_kwargs: object,
        ) -> DocumentDetail:
            await self._oracle.update_document_status(document_id, FileStatus.ERROR, message)
            raise UnstructuredFailure(message)

    monkeypatch.setattr(documents_route, "IngestionPipeline", FailingUnstructuredPipeline)
    document_id = _upload("scan.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")
    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")
    assert resp.status_code == 200
    job = resp.json()["data"]

    anyio.run(documents_route._run_ingestion_job, job["id"])

    job_detail = client.get(f"/api/documents/ingestion-jobs/{job['id']}")
    assert job_detail.status_code == 200
    failed_job = job_detail.json()["data"]
    assert failed_job["status"] == "FAILED"
    assert failed_job["error_message"] == message
    stored = fake_document_dependencies.documents[document_id]
    assert stored.status == FileStatus.ERROR
    assert stored.error_message == message


def test_document_ingestion_job_endpoint_skips_duplicate_document() -> None:
    """重複文書の手動 job 投入も重複索引を作らず skipped にする。"""
    body = "重複本文".encode()
    original = client.post(
        "/api/documents/upload",
        files={"file": ("original.txt", body, "text/plain")},
    )
    assert original.status_code == 200
    duplicate = client.post(
        "/api/documents/upload",
        files={"file": ("duplicate.txt", body, "text/plain")},
    )
    assert duplicate.status_code == 200
    document_id = duplicate.json()["data"]["id"]

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] == "SKIPPED"
    assert job["skip_reason"] == "duplicate_content"
    assert "duplicate_content" in job["quality_warnings"]

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_ingestion_job_endpoint_force_queues_duplicate_document() -> None:
    """force=true なら重複文書も明示的な再取込としてキュー投入する。"""
    body = "重複でも取り込む本文".encode()
    original = client.post(
        "/api/documents/upload",
        files={"file": ("original.txt", body, "text/plain")},
    )
    assert original.status_code == 200
    duplicate = client.post(
        "/api/documents/upload",
        files={"file": ("duplicate.txt", body, "text/plain")},
    )
    assert duplicate.status_code == 200
    document_id = duplicate.json()["data"]["id"]

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs?force=true")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] == "QUEUED"
    assert job["skip_reason"] is None


def test_document_ingestion_job_endpoint_skips_indexed_without_force() -> None:
    """既に索引済みの文書は force なしなら再取込せず job 履歴だけ残す。"""
    document_id = _upload(
        "indexed-policy.txt",
        "索引済み本文".encode(),
        "text/plain",
    )
    first = client.post(f"/api/documents/{document_id}/ingestion-jobs")
    assert first.status_code == 200
    anyio.run(documents_route._run_ingestion_job, first.json()["data"]["id"])

    second = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert second.status_code == 200
    job = second.json()["data"]
    assert job["status"] == "SKIPPED"
    assert job["skip_reason"] == "already_indexed"


def test_recipe_approve_review_queues_chunk_phase(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """REVIEW のレシピの承認は INDEX ではなく CHUNK job を投入する。"""
    document_id = _upload("review-policy.txt", b"review text", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction={"document_type": "text", "raw_text": "review text"},
    )

    resp = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "CHUNK"
    assert job["status"] == "QUEUED"
    assert job["recipe_id"] == recipe_id
    assert fake_document_dependencies.recipes[recipe_id]["status"] == "REVIEW"


def test_recipe_approve_review_with_edits_saves_before_chunk_phase(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """承認時のテキスト修正は、CHUNK job の投入前にレシピの抽出結果へ保存する。"""
    document_id = _upload("review-policy.txt", b"review text", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction=_review_extraction_payload(),
    )

    resp = client.post(
        f"/api/documents/{document_id}/recipes/{recipe_id}/approve",
        json={
            "element_edits": [
                {"element_id": "el-0000", "text": "経費申請（承認時修正）", "base_text": "経費申請"}
            ]
        },
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["phase"] == "CHUNK"
    edited_id = fake_document_dependencies.recipes[recipe_id]["active_extraction_recipe_id"]
    assert isinstance(edited_id, str) and edited_id != "er-recipe-1"
    edited = fake_document_dependencies.extraction_artifacts[edited_id]["extraction_json"]
    assert isinstance(edited, dict)
    assert edited["raw_text"] == "経費申請（承認時修正）"


def test_recipe_approve_review_with_unknown_element_is_rejected(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """承認時の修正に存在しない要素 ID があれば 400 にし、job を投入しない。"""
    document_id = _upload("review-policy.txt", b"review text", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.REVIEW,
        extraction=_review_extraction_payload(),
    )

    resp = client.post(
        f"/api/documents/{document_id}/recipes/{recipe_id}/approve",
        json={"element_edits": [{"element_id": "does-not-exist", "text": "x", "base_text": ""}]},
    )

    assert resp.status_code == 400
    assert fake_document_dependencies.ingestion_jobs == {}


def test_recipe_approve_chunked_queues_index_phase(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """CHUNKED のレシピの承認は embedding/index 専用の INDEX job を投入する。"""
    document_id = _upload("chunked-policy.txt", b"chunked text", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.CHUNKED,
        extraction={"document_type": "text", "raw_text": "chunked text"},
        chunks=[
            DocumentChunkView(
                document_id=document_id,
                chunk_id=f"{document_id}:chunk-0",
                chunk_index=0,
                text="chunked text",
            )
        ],
    )

    resp = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "INDEX"
    assert job["status"] == "QUEUED"
    assert fake_document_dependencies.recipes[recipe_id]["status"] == "CHUNKED"


def test_recipe_approve_preprocessed_queues_extract_phase(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """PREPROCESSED のレシピの承認は保存済み準備ファイルから EXTRACT 専用 job を投入する。"""
    document_id = _upload("preprocessed-policy.txt", b"prepared text", "text/plain")
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.PREPROCESSED,
        preprocess_artifact=DocumentPreprocessArtifact(
            derivation_id="der-1",
            profile="passthrough",
            file_name="preprocessed-policy.txt",
            object_storage_path="local://prepared/preprocessed-policy.txt",
            content_type="text/plain",
        ),
    )

    resp = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "EXTRACT"
    assert job["status"] == "QUEUED"


def test_recipe_approve_preprocessed_duplicate_still_queues_extract_phase(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """重複文書でも、レシピが PREPROCESSED まで進んでいれば承認で EXTRACT を投入する。

    重複スキップは初回 PREPROCESS の入口だけ。ここで skip すると重複文書は承認しても
    解析中へ進めず PREPROCESSED で詰まる(回帰防止)。
    """
    document_id = _upload("dup-preprocessed.txt", b"prepared dup text", "text/plain")
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={"duplicate_of_document_id": "doc-original"}
    )
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.PREPROCESSED,
        preprocess_artifact=DocumentPreprocessArtifact(
            derivation_id="der-1",
            profile="passthrough",
            file_name="dup-preprocessed.txt",
            object_storage_path="local://prepared/dup-preprocessed.txt",
            content_type="text/plain",
        ),
    )

    resp = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "EXTRACT"
    assert job["status"] == "QUEUED"
    assert job["skip_reason"] is None


@pytest.mark.parametrize("status", [FileStatus.UPLOADED, FileStatus.INDEXED])
def test_recipe_approve_requires_confirmation_status(
    fake_document_dependencies: FakeWorkspaceOracle,
    status: FileStatus,
) -> None:
    """確認待ち(PREPROCESSED / REVIEW / CHUNKED)でないレシピの承認は 409。"""
    document_id = _upload("policy.txt", b"policy", "text/plain")
    recipe_id = _seed_recipe(fake_document_dependencies, document_id, status=status)

    resp = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")

    assert resp.status_code == 409
    assert fake_document_dependencies.ingestion_jobs == {}


def test_recipe_approve_returns_404_for_unknown_recipe() -> None:
    document_id = _upload("policy.txt", b"policy", "text/plain")

    resp = client.post(f"/api/documents/{document_id}/recipes/missing/approve")

    assert resp.status_code == 404


def test_document_processing_config_resolves_preprocess_profile_and_auto_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピの有効な設定は、ファイル準備方式と自動進行を global 既定から解決する（#488）。"""
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_preprocess_profile="passthrough"),
    )

    _, effective = documents_route._merge_document_processing_config(DocumentProcessingConfig())

    assert effective.preprocess_profile == "passthrough"
    assert effective.auto_parse_after_preprocess_enabled is True
    assert effective.auto_chunk_after_extract_enabled is True
    assert effective.auto_index_after_chunk_enabled is True


def test_preprocess_auto_advance_waits_for_manual_review_when_disabled(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自動進行が無効なら PREPROCESS 完了後は REVIEW に留まり、Chunk は投入しない。"""
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=False),
    )
    document_id = _upload("manual-review.txt", b"review text", "text/plain")
    detail = fake_document_dependencies.documents[document_id].model_copy(
        update={
            "status": FileStatus.REVIEW,
            "extraction": {"document_type": "text", "raw_text": "review text"},
        }
    )
    fake_document_dependencies.documents[document_id] = detail
    job = IngestionJob(
        id="job-preprocess",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        phase=IngestionJobPhase.PREPROCESS,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )

    anyio.run(documents_route._enqueue_auto_advance_job, job, detail)

    assert fake_document_dependencies.ingestion_jobs == {}


def test_preprocess_auto_advance_queues_chunk_when_enabled(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定有効時だけ PREPROCESS 完了後に CHUNK job を自動投入する。"""
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=True),
    )
    document_id = _upload("auto-review.txt", b"review text", "text/plain")
    detail = fake_document_dependencies.documents[document_id].model_copy(
        update={
            "status": FileStatus.REVIEW,
            "extraction": {"document_type": "text", "raw_text": "review text"},
        }
    )
    fake_document_dependencies.documents[document_id] = detail
    job = IngestionJob(
        id="job-preprocess",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        phase=IngestionJobPhase.PREPROCESS,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )

    anyio.run(documents_route._enqueue_auto_advance_job, job, detail)

    jobs = list(fake_document_dependencies.ingestion_jobs.values())
    assert len(jobs) == 1
    assert jobs[0].phase == IngestionJobPhase.CHUNK
    assert jobs[0].status == IngestionJobStatus.QUEUED


def test_document_chunk_reprocess_resets_downstream_only(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """CHUNK から再処理すると抽出は保持し、chunk 以降だけ削除する。"""
    document_id = _upload("chunk-reprocess.txt", b"chunk source", "text/plain")
    extraction = {"document_type": "text", "raw_text": "抽出済み"}
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.INDEXED,
            "extraction": extraction,
            "indexed_at": datetime.now(UTC),
        }
    )
    fake_document_dependencies.chunks[document_id] = [
        DocumentChunkView(
            document_id=document_id,
            chunk_id=f"{document_id}:old",
            chunk_index=0,
            text="古い chunk",
        )
    ]

    resp = client.post(
        f"/api/documents/{document_id}/ingestion-jobs",
        params={"phase": "CHUNK", "force": "true"},
    )

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "CHUNK"
    reset = fake_document_dependencies.documents[document_id]
    assert reset.status == FileStatus.REVIEW
    assert reset.extraction == extraction
    assert reset.indexed_at is None
    assert fake_document_dependencies.chunks.get(document_id) is None


def test_document_index_reprocess_keeps_chunks(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """INDEX から再処理すると chunk は保持し、索引状態だけ戻す。"""
    document_id = _upload("index-reprocess.txt", b"index source", "text/plain")
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.INDEXED,
            "extraction": {"document_type": "text", "raw_text": "chunk 済み"},
            "indexed_at": datetime.now(UTC),
        }
    )
    chunks = [
        DocumentChunkView(
            document_id=document_id,
            chunk_id=f"{document_id}:chunk-0",
            chunk_index=0,
            text="chunk 済み",
        )
    ]
    fake_document_dependencies.chunks[document_id] = chunks

    resp = client.post(
        f"/api/documents/{document_id}/ingestion-jobs",
        params={"phase": "INDEX", "force": "true"},
    )

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "INDEX"
    reset = fake_document_dependencies.documents[document_id]
    assert reset.status == FileStatus.CHUNKED
    assert reset.indexed_at is None
    assert fake_document_dependencies.chunks[document_id] == chunks


def test_document_ingestion_job_endpoint_clears_previous_outputs_on_force_reingest(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """force 再取込は旧 extraction/chunk/checkpoint を入隊時点で消す。"""
    document_id = _upload(
        "reingest-policy.pdf",
        b"%PDF-1.4\nold processing state",
        "application/pdf",
    )
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.INDEXED,
            "extraction": {
                "raw_text": "前回の抽出本文",
                "quality_report": {
                    "parser_backend": "enterprise_ai",
                    "parser_profile": "enterprise_ai_pdf_layout",
                },
            },
            "indexed_at": datetime.now(UTC),
            "error_message": "前回の状態",
        }
    )
    fake_document_dependencies.chunks[document_id] = [
        DocumentChunkView(
            document_id=document_id,
            chunk_id=f"{document_id}:old",
            chunk_index=0,
            text="前回の chunk",
        )
    ]
    fake_document_dependencies.ingestion_segments[document_id] = [
        IngestionSegment(
            segment_id=f"{document_id}:old-p1-3",
            document_id=document_id,
            status="SUCCEEDED",
            parser_backend="enterprise_ai",
            parser_profile="enterprise_ai_pdf_layout",
            page_start=1,
            page_end=3,
        )
    ]

    resp = client.post(
        f"/api/documents/{document_id}/ingestion-jobs",
        params={"force": "true"},
    )

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] == "QUEUED"
    cleared = fake_document_dependencies.documents[document_id]
    assert cleared.status == FileStatus.UPLOADED
    assert cleared.extraction == {}
    assert cleared.indexed_at is None
    assert cleared.error_message is None
    assert fake_document_dependencies.chunks.get(document_id) is None
    assert fake_document_dependencies.ingestion_segments.get(document_id) is None

    segments = client.get(f"/api/documents/{document_id}/ingestion-segments")
    assert segments.status_code == 200
    assert [item["segment_id"] for item in segments.json()["data"]] == [f"{document_id}:source"]
    assert segments.json()["data"][0]["status"] == "QUEUED"
    assert segments.json()["data"][0]["progress_unit"] == "source"


def test_document_ingestion_job_endpoint_skips_unsupported_outlook_msg() -> None:
    """未対応 Outlook MSG の手動 job 投入も skipped 履歴だけ残す。"""
    document_id = _upload(
        "approval.msg",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1outlook msg",
        "application/octet-stream",
    )

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] == "SKIPPED"
    assert job["parser_profile"] == "unsupported_outlook_msg"
    assert job["skip_reason"] == "outlook_msg_not_supported"
    assert "unsupported_outlook_msg" in job["quality_warnings"]

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_ingestion_job_endpoint_skips_unsupported_tiff_image() -> None:
    """TIFF 画像の手動 job 投入も skipped 履歴だけ残す。"""
    document_id = _upload("scan.tiff", b"II*\x00tiff", "image/tiff")

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] == "SKIPPED"
    assert job["parser_profile"] == "unsupported_tiff_image"
    assert job["skip_reason"] == "tiff_image_not_supported"
    assert "unsupported_tiff_image" in job["quality_warnings"]

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_ingestion_job_endpoint_skips_unsupported_legacy_office() -> None:
    """旧バイナリ Office の手動 job 投入も skipped 履歴だけ残す。"""
    document_id = _upload("legacy.doc", b"legacy office", "application/msword")

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] == "SKIPPED"
    assert job["parser_profile"] == "unsupported_legacy_office_binary"
    assert job["skip_reason"] == "legacy_office_binary_not_supported"
    assert "unsupported_legacy_office_binary" in job["quality_warnings"]

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"


def test_document_ingestion_job_endpoint_runs_legacy_office_with_office_to_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定レシピのファイル準備が変換できる(Office→PDF)なら、旧形式を飛ばさない(#2470)。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rag_preprocess_enabled", True)
    monkeypatch.setattr(settings, "rag_preprocess_profile", "office_to_pdf")
    document_id = _upload("legacy.doc", b"legacy office", "application/msword")

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["status"] != "SKIPPED"
    assert job["skip_reason"] is None


def test_document_upload_accepts_macro_enabled_office() -> None:
    """マクロ付きの Office はアップロードでき、変換が要る形式として案内する(#2470)。"""
    resp = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "macro.docm",
                b"PK\x03\x04macro",
                "application/vnd.ms-word.document.macroEnabled.12",
            )
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert body["source_profile"]["modality"] == "office"
    assert body["source_profile"]["unsupported_reason"] == "macro_enabled_office_not_supported"
    assert body["parser_notice"]["code"] == "office_conversion_required"


@pytest.mark.parametrize(
    ("file_name", "content_type", "expected"),
    [
        ("photo.heic", "image/heic", HEIC_UPLOAD_REJECTED_MESSAGE),
        ("photo.HEIF", "application/octet-stream", HEIC_UPLOAD_REJECTED_MESSAGE),
        ("photo.bin", "image/heif-sequence", HEIC_UPLOAD_REJECTED_MESSAGE),
        ("book.epub", "application/epub+zip", EPUB_UPLOAD_REJECTED_MESSAGE),
        ("voice.mp3", "audio/mpeg", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("meeting.m4a", "application/octet-stream", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("record.ogg", "application/ogg", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("clip.mp4", "video/mp4", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("clip.bin", "video/quicktime", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
    ],
    ids=[
        "heic",
        "heif-octet-stream",
        "heif-mime",
        "epub",
        "mp3",
        "m4a-octet-stream",
        "ogg-application",
        "mp4-video",
        "video-mime",
    ],
)
def test_document_upload_rejects_unsupported_formats_with_guide(
    fake_document_dependencies: FakeWorkspaceOracle,
    file_name: str,
    content_type: str,
    expected: str,
) -> None:
    """HEIC / HEIF・EPUB(#3351)と音声・動画(#3369)は、保存せずに 415 で変換の方法を返す。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": (file_name, b"\x00\x00\x00\x18ftypheic", content_type)},
    )

    assert resp.status_code == 415
    assert resp.json()["error_messages"] == [expected]
    assert fake_document_dependencies.documents == {}


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [
        ("memo.odt", "application/vnd.oasis.opendocument.text"),
        ("memo.rtf", "text/rtf"),
        ("memo.rtf", "application/rtf"),
    ],
    ids=["odt", "rtf-text", "rtf-application"],
)
def test_document_upload_accepts_odt_rtf_as_office_conversion(
    file_name: str, content_type: str
) -> None:
    """ODT・RTF はアップロードでき、Office→PDF で変換する形式として案内する(#3351)。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": (file_name, b"{\\rtf1 synthetic}", content_type)},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ingestion_started"] is False
    assert body["source_profile"]["modality"] == "office"
    assert body["source_profile"]["unsupported_reason"] == "odt_rtf_not_supported"
    assert body["parser_notice"]["code"] == "office_conversion_required"
    assert "「Office→PDF」" in body["parser_notice"]["message"]


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [
        ("guide.rst", "text/x-rst"),
        ("guide.rst", "application/octet-stream"),
        ("notes.org", "text/org"),
        ("notes.org", "text/plain"),
    ],
    ids=["rst-mime", "rst-octet-stream", "org-mime", "org-text"],
)
def test_document_upload_accepts_rst_org_as_text(file_name: str, content_type: str) -> None:
    """reStructuredText・Org はプレーンテキストとして受け付ける(pandoc を通さない。#3351)。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": (file_name, b"Title\n=====\n\nBody.\n", content_type)},
    )

    assert resp.status_code == 200
    profile = resp.json()["data"]["source_profile"]
    assert profile["modality"] == "text"
    assert profile["unsupported_reason"] is None
    assert profile["preview_kind"] == "text"


def test_document_upload_accepts_text_xml() -> None:
    """.xml は text/xml でも受け付ける(#3360)。"""
    resp = client.post(
        "/api/documents/upload",
        files={"file": ("data.xml", b"<root><item>a</item></root>", "text/xml")},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["source_profile"]["modality"] == "text"


def test_drain_queued_ingestion_jobs_runs_persisted_jobs(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """永続化済み QUEUED job は drain API からワーカーへ通知できる。"""
    document_id = _upload(
        "queued-policy.txt",
        "再起動後の queued job".encode(),
        "text/plain",
    )
    queued_job = IngestionJob(
        id="job-queued",
        document_id=document_id,
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[queued_job.id] = queued_job

    resp = client.post("/api/documents/ingestion-jobs/drain")

    assert resp.status_code == 200
    jobs = resp.json()["data"]
    assert [job["id"] for job in jobs] == ["job-queued"]
    assert jobs[0]["status"] == "QUEUED"

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "UPLOADED"

    job_detail = client.get("/api/documents/ingestion-jobs/job-queued")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "QUEUED"

    anyio.run(documents_route._run_ingestion_job, "job-queued")
    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "INDEXED"
    job_detail = client.get("/api/documents/ingestion-jobs/job-queued")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "SUCCEEDED"
    assert job_detail.json()["data"]["attempt_count"] == 1


def test_retry_ingestion_job_creates_new_job_for_failed_document(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """失敗済み job の retry は新しい job として対象文書を再投入する。"""
    document_id = _upload(
        "failed-policy.txt",
        "retry 対象本文".encode(),
        "text/plain",
    )
    failed_job = IngestionJob(
        id="job-failed",
        document_id=document_id,
        status=IngestionJobStatus.FAILED,
        parser_profile="local_text_structure",
        error_message="前回失敗",
        queued_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[failed_job.id] = failed_job

    resp = client.post("/api/documents/ingestion-jobs/job-failed/retry")

    assert resp.status_code == 200
    retry_job = resp.json()["data"]
    assert retry_job["id"] != "job-failed"
    assert retry_job["document_id"] == document_id
    assert retry_job["status"] == "QUEUED"

    job_detail = client.get(f"/api/documents/ingestion-jobs/{retry_job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "QUEUED"

    anyio.run(documents_route._run_ingestion_job, retry_job["id"])
    job_detail = client.get(f"/api/documents/ingestion-jobs/{retry_job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "SUCCEEDED"

    original_job = client.get("/api/documents/ingestion-jobs/job-failed")
    assert original_job.status_code == 200
    assert original_job.json()["data"]["status"] == "FAILED"


def test_preprocess_reprocess_clears_prepared_artifact_and_outputs(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """PREPROCESS 再処理は処理後ファイル情報と抽出/Chunk を削除して原本からやり直す。"""
    document_id = _upload("policy.txt", b"original-body", "text/plain")
    prepared_path = anyio.run(
        ObjectStorageClient().put,
        f"artifacts/canonical/{document_id}/old-prepared.txt",
        b"old-prepared",
        "text/plain",
    )
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.INDEXED,
            "preprocess_artifact": DocumentPreprocessArtifact(
                derivation_id="old",
                profile="text_normalize",
                converted=True,
                object_storage_path=prepared_path,
                content_type="text/plain",
                file_name="policy__prepared.txt",
            ),
            "extraction": {"raw_text": "old extraction"},
            "indexed_at": datetime.now(UTC),
        }
    )
    fake_document_dependencies.chunks[document_id] = [
        DocumentChunkView(
            document_id=document_id,
            chunk_id="chunk-old",
            chunk_index=0,
            text="old",
        )
    ]

    resp = client.post(
        f"/api/documents/{document_id}/ingestion-jobs",
        params={"force": "true", "phase": "PREPROCESS"},
    )

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "PREPROCESS"
    assert fake_document_dependencies.documents[document_id].preprocess_artifact is None
    assert fake_document_dependencies.documents[document_id].extraction == {}
    assert document_id not in fake_document_dependencies.chunks

    anyio.run(documents_route._run_ingestion_job, job["id"])

    updated = fake_document_dependencies.documents[document_id]
    assert updated.status == FileStatus.INDEXED
    assert updated.preprocess_artifact is not None
    assert updated.preprocess_artifact.object_storage_path == updated.object_storage_path
    assert updated.extraction["raw_text"] == "original-body"


def test_extract_reprocess_uses_existing_prepared_artifact(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """EXTRACT 再処理は原本へ戻らず、保存済みの処理後ファイルを抽出入力にする。"""
    document_id = _upload("policy.txt", b"original-body", "text/plain")
    prepared_body = b"prepared-body"
    prepared_path = anyio.run(
        ObjectStorageClient().put,
        f"artifacts/canonical/{document_id}/prepared.txt",
        prepared_body,
        "text/plain",
    )
    detail = fake_document_dependencies.documents[document_id]
    artifact = DocumentPreprocessArtifact(
        derivation_id="prepared",
        profile="text_normalize",
        converted=True,
        object_storage_path=prepared_path,
        content_type="text/plain",
        sha256=hashlib.sha256(prepared_body).hexdigest(),
        file_name="policy__prepared.txt",
    )
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.INDEXED,
            "preprocess_artifact": artifact,
            "extraction": {"raw_text": "old extraction"},
            "indexed_at": datetime.now(UTC),
        }
    )

    resp = client.post(
        f"/api/documents/{document_id}/ingestion-jobs",
        params={"force": "true", "phase": "EXTRACT"},
    )

    assert resp.status_code == 200
    job = resp.json()["data"]
    assert job["phase"] == "EXTRACT"
    assert fake_document_dependencies.documents[document_id].preprocess_artifact == artifact

    anyio.run(documents_route._run_ingestion_job, job["id"])

    updated = fake_document_dependencies.documents[document_id]
    assert updated.preprocess_artifact == artifact
    assert updated.extraction["raw_text"] == "prepared-body"


def test_extract_reprocess_requires_prepared_artifact() -> None:
    """処理後ファイルが欠けている EXTRACT 再処理は原本へ黙って戻らない。"""
    document_id = _upload("policy.txt", b"original-body", "text/plain")

    resp = client.post(
        f"/api/documents/{document_id}/ingestion-jobs",
        params={"force": "true", "phase": "EXTRACT"},
    )

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == [
        "処理後ファイルが見つかりません。ファイル準備から再処理してください。"
    ]


def test_retry_failed_chunk_job_requeues_from_error(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """FAILED の CHUNK job は ERROR 文書からでも同じ stage を再投入できる。"""
    document_id = _upload("failed-chunk.txt", b"chunk retry", "text/plain")
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.ERROR,
            "extraction": {"document_type": "text", "raw_text": "抽出済み"},
            "error_message": "前回 chunk 失敗",
        }
    )
    failed_job = IngestionJob(
        id="job-failed-chunk",
        document_id=document_id,
        status=IngestionJobStatus.FAILED,
        phase=IngestionJobPhase.CHUNK,
        parser_profile="local_text_structure",
        error_message="前回 chunk 失敗",
        queued_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[failed_job.id] = failed_job

    resp = client.post("/api/documents/ingestion-jobs/job-failed-chunk/retry")

    assert resp.status_code == 200
    retry_job = resp.json()["data"]
    assert retry_job["phase"] == "CHUNK"
    assert retry_job["status"] == "QUEUED"
    assert fake_document_dependencies.documents[document_id].status == FileStatus.REVIEW


def test_retry_ingestion_job_rejects_running_job(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """実行中 job の retry は二重実行になるため拒否する。"""
    document_id = _upload(
        "running-policy.txt",
        "実行中本文".encode(),
        "text/plain",
    )
    running_job = IngestionJob(
        id="job-running",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[running_job.id] = running_job

    resp = client.post("/api/documents/ingestion-jobs/job-running/retry")

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["この取込ジョブはまだ実行中です。"]


def test_retry_failed_ingestion_segments_creates_retry_job(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """FAILED checkpoint がある場合だけ segment retry job を投入する。"""
    document_id = _upload(
        "segment-failed.pdf",
        b"%PDF-1.4\nsegment retry target",
        "application/pdf",
    )
    fake_document_dependencies.ingestion_segments[document_id] = [
        IngestionSegment(
            segment_id=f"{document_id}:p1-2",
            document_id=document_id,
            status="SUCCEEDED",
            parser_backend="enterprise_ai",
            parser_profile="enterprise_ai_pdf_layout",
            page_start=1,
            page_end=2,
            artifact_path=f"oci://namespace/bucket/artifacts/extractions/{document_id}/p1-2.json",
        ),
        IngestionSegment(
            segment_id=f"{document_id}:p3-4",
            document_id=document_id,
            status="FAILED",
            parser_backend="enterprise_ai",
            parser_profile="enterprise_ai_pdf_layout",
            page_start=3,
            page_end=4,
            attempt_count=1,
            error_code="enterprise_ai_response_validation_error",
            error_message="前回の segment 抽出に失敗しました。",
        ),
    ]

    resp = client.post(f"/api/documents/{document_id}/ingestion-segments/retry")

    assert resp.status_code == 200
    retry_job = resp.json()["data"]
    assert retry_job["document_id"] == document_id
    assert retry_job["status"] == "QUEUED"
    job_detail = client.get(f"/api/documents/ingestion-jobs/{retry_job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "QUEUED"

    anyio.run(documents_route._run_ingestion_job, retry_job["id"])
    job_detail = client.get(f"/api/documents/ingestion-jobs/{retry_job['id']}")
    assert job_detail.status_code == 200
    assert job_detail.json()["data"]["status"] == "SUCCEEDED"


def test_retry_failed_ingestion_segments_requires_failed_checkpoint(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """FAILED checkpoint がない文書は segment retry として受け付けない。"""
    document_id = _upload(
        "segment-ok.pdf",
        b"%PDF-1.4\nno failed segment",
        "application/pdf",
    )
    fake_document_dependencies.ingestion_segments[document_id] = [
        IngestionSegment(
            segment_id=f"{document_id}:p1-2",
            document_id=document_id,
            status="SUCCEEDED",
            parser_backend="enterprise_ai",
            parser_profile="enterprise_ai_pdf_layout",
            page_start=1,
            page_end=2,
            artifact_path=f"oci://namespace/bucket/artifacts/extractions/{document_id}/p1-2.json",
        )
    ]

    resp = client.post(f"/api/documents/{document_id}/ingestion-segments/retry")

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["再試行対象の失敗 segment がありません。"]


def test_cancel_queued_ingestion_job(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """待機中 job は CANCELLED にでき、drain 対象から外れる。"""
    document_id = _upload(
        "cancel-queued-policy.txt",
        "cancel queued 本文".encode(),
        "text/plain",
    )
    queued_job = IngestionJob(
        id="job-cancel-queued",
        document_id=document_id,
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[queued_job.id] = queued_job

    resp = client.post("/api/documents/ingestion-jobs/job-cancel-queued/cancel")

    assert resp.status_code == 200
    cancelled = resp.json()["data"]
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["error_message"] == documents_route.INGESTION_JOB_CANCELLED_MESSAGE
    assert cancelled["finished_at"] is not None

    drain = client.post("/api/documents/ingestion-jobs/drain")
    assert drain.status_code == 200
    assert drain.json()["data"] == []


def test_cancel_running_ingestion_job(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """実行中 job は CANCELLED にでき、文書の status は API では戻さない(#305)。

    実行中の worker がまだ処理を続けているため、文書を戻すのは cancel を検知した worker に任せる。
    API が先に戻すと、同じ文書を再投入できてしまい、古い worker と新しい job が競合する。
    """
    document_id = _upload(
        "cancel-running-policy.txt",
        "cancel running 本文".encode(),
        "text/plain",
    )
    running_job = IngestionJob(
        id="job-cancel-running",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[running_job.id] = running_job
    fake_document_dependencies.documents[document_id] = fake_document_dependencies.documents[
        document_id
    ].model_copy(update={"status": FileStatus.INGESTING})

    resp = client.post("/api/documents/ingestion-jobs/job-cancel-running/cancel")

    assert resp.status_code == 200
    cancelled = resp.json()["data"]
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["error_message"] == documents_route.INGESTION_JOB_CANCELLED_MESSAGE
    assert fake_document_dependencies.documents[document_id].status == FileStatus.INGESTING


def test_cancel_ingestion_job_rejects_terminal_status(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """完了済み job の cancel は履歴破壊を避けて拒否する。"""
    document_id = _upload(
        "cancel-terminal-policy.txt",
        "cancel terminal 本文".encode(),
        "text/plain",
    )
    succeeded_job = IngestionJob(
        id="job-cancel-terminal",
        document_id=document_id,
        status=IngestionJobStatus.SUCCEEDED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[succeeded_job.id] = succeeded_job

    resp = client.post("/api/documents/ingestion-jobs/job-cancel-terminal/cancel")

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["この取込ジョブはキャンセルできません。"]
    assert fake_document_dependencies.ingestion_jobs[succeeded_job.id].status == (
        IngestionJobStatus.SUCCEEDED
    )


def test_running_ingestion_job_does_not_overwrite_cancelled_status(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 終了時に、途中で cancel された job を SUCCEEDED で上書きしない。"""
    document_id = _upload(
        "cancel-race-policy.txt",
        "cancel race 本文".encode(),
        "text/plain",
    )
    queued_job = IngestionJob(
        id="job-cancel-race",
        document_id=document_id,
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[queued_job.id] = queued_job

    async def cancel_during_ingest(
        document_id: str,
        *,
        force: bool = False,
        use_prepared_artifact: bool = False,
        cancel_checker: object | None = None,
    ) -> DocumentDetail:
        _ = force, use_prepared_artifact, cancel_checker
        job = fake_document_dependencies.ingestion_jobs[queued_job.id]
        fake_document_dependencies.ingestion_jobs[queued_job.id] = job.model_copy(
            update={
                "status": IngestionJobStatus.CANCELLED,
                "error_message": documents_route.INGESTION_JOB_CANCELLED_MESSAGE,
                "finished_at": datetime.now(UTC),
            }
        )
        detail = await fake_document_dependencies.get_document(document_id)
        assert detail is not None
        return detail

    monkeypatch.setattr(documents_route, "_ingest_existing_document", cancel_during_ingest)

    anyio.run(documents_route._run_ingestion_job, queued_job.id)

    assert fake_document_dependencies.ingestion_jobs[queued_job.id].status == (
        IngestionJobStatus.CANCELLED
    )
    # cancel を検知した worker が文書の status を戻す。
    assert fake_document_dependencies.documents[document_id].status == FileStatus.UPLOADED


def _interleave_between_job_read_and_write(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeWorkspaceOracle,
    job_id: str,
    action: Callable[[], None],
) -> dict[str, bool]:
    """job の状態を読んでから書くまでの間に、別の処理(action)を 1 回だけ割り込ませる。

    get は値を返した後に、transition は書く前に action を呼ぶ。どちらも「読んでから書くまで」の
    間に相当する(#305 の TOCTOU)。``armed["on"]`` を True にした後の最初のアクセスで発火する。
    """
    armed = {"on": False}
    original_get = fake.get_ingestion_job
    original_transition = fake.transition_ingestion_job

    def fire(target_id: str) -> None:
        if armed["on"] and target_id == job_id:
            armed["on"] = False
            action()

    async def get_then_interleave(target_id: str) -> IngestionJob | None:
        current = await original_get(target_id)
        fire(target_id)
        return current

    async def interleave_then_transition(target_id: str, **kwargs: Any) -> IngestionJob | None:
        fire(target_id)
        return await original_transition(target_id, **kwargs)

    monkeypatch.setattr(fake, "get_ingestion_job", get_then_interleave)
    monkeypatch.setattr(fake, "transition_ingestion_job", interleave_then_transition)
    return armed


def _cancel_job_in_store(fake: FakeWorkspaceOracle, job_id: str) -> None:
    """cancel API が job を CANCELLED にした状態を直接作る。"""
    job = fake.ingestion_jobs[job_id]
    if job.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING}:
        fake.ingestion_jobs[job_id] = job.model_copy(
            update={
                "status": IngestionJobStatus.CANCELLED,
                "error_message": documents_route.INGESTION_JOB_CANCELLED_MESSAGE,
                "finished_at": datetime.now(UTC),
            }
        )


def test_cancel_after_job_succeeded_does_not_overwrite_succeeded(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel API が RUNNING を読んだ直後に job が SUCCEEDED になったら、上書きせず 409 にする。"""
    document_id = _upload("succeeded-race.txt", "競合本文".encode(), "text/plain")
    fake_document_dependencies.documents[document_id] = fake_document_dependencies.documents[
        document_id
    ].model_copy(update={"status": FileStatus.INDEXED})
    running_job = IngestionJob(
        id="job-succeeded-race",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[running_job.id] = running_job

    def worker_finishes() -> None:
        job = fake_document_dependencies.ingestion_jobs[running_job.id]
        fake_document_dependencies.ingestion_jobs[running_job.id] = job.model_copy(
            update={"status": IngestionJobStatus.SUCCEEDED, "finished_at": datetime.now(UTC)}
        )

    armed = _interleave_between_job_read_and_write(
        monkeypatch, fake_document_dependencies, running_job.id, worker_finishes
    )
    armed["on"] = True

    resp = client.post(f"/api/documents/ingestion-jobs/{running_job.id}/cancel")

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["この取込ジョブはキャンセルできません。"]
    assert fake_document_dependencies.ingestion_jobs[running_job.id].status == (
        IngestionJobStatus.SUCCEEDED
    )
    assert fake_document_dependencies.documents[document_id].status == FileStatus.INDEXED


def test_cancel_during_finish_keeps_cancelled_and_skips_auto_advance(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完了処理が RUNNING を確かめた直後に cancel されたら、SUCCEEDED で上書きせず自動進行しない。

    #305 の TOCTOU(確認と書き込みの間の割り込み)を再現する。
    """
    monkeypatch.setattr(get_settings(), "rag_auto_chunk_after_extract_enabled", True)
    document_id = _upload("finish-race.txt", "完了競合本文".encode(), "text/plain")
    queued_job = IngestionJob(
        id="job-finish-race",
        document_id=document_id,
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[queued_job.id] = queued_job
    armed = _interleave_between_job_read_and_write(
        monkeypatch,
        fake_document_dependencies,
        queued_job.id,
        lambda: _cancel_job_in_store(fake_document_dependencies, queued_job.id),
    )

    async def ingest_to_review(
        document_id: str,
        *,
        force: bool = False,
        use_prepared_artifact: bool = False,
        cancel_checker: object | None = None,
    ) -> DocumentDetail:
        _ = force, use_prepared_artifact, cancel_checker
        detail = await fake_document_dependencies.update_document_status(
            document_id, FileStatus.REVIEW
        )
        # pipeline は REVIEW まで書き終えた。この後の完了処理の途中で cancel が割り込む。
        armed["on"] = True
        return detail

    monkeypatch.setattr(documents_route, "_ingest_existing_document", ingest_to_review)

    anyio.run(documents_route._run_ingestion_job, queued_job.id)

    assert fake_document_dependencies.ingestion_jobs[queued_job.id].status == (
        IngestionJobStatus.CANCELLED
    )
    # 取り消した job の次工程(CHUNK)は投入しない。
    assert [
        job.id
        for job in fake_document_dependencies.ingestion_jobs.values()
        if job.phase == IngestionJobPhase.CHUNK
    ] == []
    # job は CANCELLED なので、文書も取り消し後の状態へ戻す(REVIEW のまま残さない)。
    assert fake_document_dependencies.documents[document_id].status == FileStatus.UPLOADED


def test_cancel_recipe_job_does_not_change_document_status(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """レシピの job の cancel は、文書(全レシピの集約)の status を変えない。"""
    document_id = _upload("recipe-cancel.txt", "レシピ本文".encode(), "text/plain")
    fake_document_dependencies.documents[document_id] = fake_document_dependencies.documents[
        document_id
    ].model_copy(update={"status": FileStatus.INDEXED})
    fake_document_dependencies.recipes["recipe-2"] = {
        "document_id": document_id,
        "recipe_id": "recipe-2",
        "status": FileStatus.INGESTING.value,
        "config_revision": 1,
        "processing_config": {},
    }
    running_job = IngestionJob(
        id="job-recipe-cancel",
        document_id=document_id,
        recipe_id="recipe-2",
        recipe_revision=1,
        status=IngestionJobStatus.RUNNING,
        phase=IngestionJobPhase.PREPROCESS,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[running_job.id] = running_job

    resp = client.post(f"/api/documents/ingestion-jobs/{running_job.id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "CANCELLED"
    assert fake_document_dependencies.documents[document_id].status == FileStatus.INDEXED


def test_retry_recipe_job_requeues_same_recipe_without_resetting_document(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """レシピの job の再試行は同じレシピの job を投入し、文書全体(全レシピ)の出力を消さない。"""
    document_id = _upload("recipe-retry.txt", "レシピ再試行本文".encode(), "text/plain")
    fake_document_dependencies.documents[document_id] = fake_document_dependencies.documents[
        document_id
    ].model_copy(
        update={
            "status": FileStatus.INDEXED,
            "extraction": {"raw_text": "レシピ 1 の抽出"},
            "indexed_at": datetime.now(UTC),
        }
    )
    fake_document_dependencies.chunks[document_id] = [
        DocumentChunkView(
            document_id=document_id,
            chunk_id="chunk-recipe-1",
            chunk_index=0,
            text="レシピ 1 の chunk",
        )
    ]
    fake_document_dependencies.recipes["recipe-2"] = {
        "document_id": document_id,
        "recipe_id": "recipe-2",
        "status": FileStatus.ERROR.value,
        "config_revision": 3,
        "processing_config": {},
    }
    failed_job = IngestionJob(
        id="job-recipe-failed",
        document_id=document_id,
        recipe_id="recipe-2",
        recipe_revision=2,
        status=IngestionJobStatus.FAILED,
        phase=IngestionJobPhase.PREPROCESS,
        parser_profile="local_text_structure",
        error_message="前回失敗",
        queued_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[failed_job.id] = failed_job

    resp = client.post(f"/api/documents/ingestion-jobs/{failed_job.id}/retry")

    assert resp.status_code == 200
    retry_job = resp.json()["data"]
    assert retry_job["id"] != failed_job.id
    assert retry_job["recipe_id"] == "recipe-2"
    assert retry_job["recipe_revision"] == 3
    assert retry_job["phase"] == "PREPROCESS"
    assert retry_job["status"] == "QUEUED"
    # 他のレシピの出力(文書の chunk / 抽出)と文書の status はそのまま。
    detail = fake_document_dependencies.documents[document_id]
    assert detail.status == FileStatus.INDEXED
    assert detail.extraction == {"raw_text": "レシピ 1 の抽出"}
    assert [chunk.chunk_id for chunk in fake_document_dependencies.chunks[document_id]] == [
        "chunk-recipe-1"
    ]


def test_document_content_returns_original_bytes() -> None:
    """原本配信は保存した bytes と保存済み content-type を返す。"""
    body = "社内規程 経費申請 承認フロー".encode()
    document_id = _upload("policy.txt", body, "text/plain")

    resp = client.get(f"/api/documents/{document_id}/content")

    assert resp.status_code == 200
    assert resp.content == body
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''policy.txt" in resp.headers["content-disposition"]


def test_document_content_prefers_stored_content_type_without_extension() -> None:
    """拡張子がなくても upload 時に保存した content-type で配信する。"""
    body = "拡張子なしテキスト".encode()
    document_id = _upload("policy", body, "text/plain")

    resp = client.get(f"/api/documents/{document_id}/content")

    assert resp.status_code == 200
    assert resp.content == body
    assert resp.headers["content-type"].startswith("text/plain")
    assert "filename*=UTF-8''policy" in resp.headers["content-disposition"]


def test_document_content_returns_prepared_artifact(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """処理後ファイル配信は preprocess_artifact の path / filename / content-type を使う。"""
    document_id = _upload("私の上司.docx", b"office-body", "application/octet-stream")
    prepared_body = b"%PDF-1.7\nprepared"
    prepared_path = anyio.run(
        ObjectStorageClient().put,
        f"artifacts/canonical/{document_id}/prepared.pdf",
        prepared_body,
        "application/pdf",
    )
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "preprocess_artifact": DocumentPreprocessArtifact(
                derivation_id="der-1",
                profile="office_pdf",
                converted=True,
                object_storage_path=prepared_path,
                content_type="application/pdf",
                sha256="dummy",
                file_name="私の上司__prepared.pdf",
            )
        }
    )

    resp = client.get(
        f"/api/documents/{document_id}/content?variant=prepared&disposition=attachment"
    )

    assert resp.status_code == 200
    assert resp.content == prepared_body
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert (
        "%E7%A7%81%E3%81%AE%E4%B8%8A%E5%8F%B8__prepared.pdf" in resp.headers["content-disposition"]
    )


def test_document_content_prepared_missing_returns_404() -> None:
    """処理後ファイルが無い場合、原本へ黙って戻さない。"""
    document_id = _upload("policy.txt", b"body", "text/plain")

    resp = client.get(f"/api/documents/{document_id}/content?variant=prepared")

    assert resp.status_code == 404
    assert resp.json()["error_messages"] == [
        "処理後ファイルが見つかりません。ファイル準備から再処理してください。"
    ]


def test_document_content_sets_utf8_charset_for_text() -> None:
    """UTF-8 テキストは charset=utf-8 を付与して配信する。"""
    body = "社内規程 経費申請 承認フロー".encode()
    document_id = _upload("policy.txt", body, "text/plain")

    resp = client.get(f"/api/documents/{document_id}/content")

    assert resp.status_code == 200
    assert resp.content == body
    assert "charset=utf-8" in resp.headers["content-type"].lower()


# WHATWG (TextDecoder) が受理する代表的なラベル
_WHATWG_VALID_LABELS = {"utf-8", "shift_jis", "euc-jp", "gbk", "gb18030", "euc-kr", "big5"}


def test_document_content_detects_non_utf8_charset() -> None:
    """非 UTF-8 テキスト(Shift_JIS)は文字コードを検出して charset を付与する。"""
    plain = (
        "となりのトトロは宮崎駿監督の長編アニメーション映画である。"
        "昭和三十年代の日本の農村を舞台に、姉妹とトトロの交流を描いた作品。"
    )
    body = (plain * 8).encode("shift_jis")
    document_id = _upload("totoro.txt", body, "text/plain")

    resp = client.get(f"/api/documents/{document_id}/content")

    assert resp.status_code == 200
    # 原本 bytes は無改変で配信する
    assert resp.content == body
    content_type = resp.headers["content-type"].lower()
    assert "charset=" in content_type
    charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
    # 非 UTF-8 を検出し、ブラウザ TextDecoder 互換ラベルで配信する
    assert charset != "utf-8"
    assert charset in _WHATWG_VALID_LABELS
    # 配信した charset でデコードすると元のテキストへ戻る
    assert resp.content.decode(charset) == plain * 8


def test_document_content_missing_original_returns_reason_and_next_action(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """保存先に原本が無いとき、プレビュー・ページ一覧は理由と対処を返す(#1210)。"""
    document_id = _upload("policy.txt", b"body", "text/plain")
    path = fake_document_dependencies.documents[document_id].object_storage_path
    assert path is not None
    assert anyio.run(ObjectStorageClient().delete, path)

    for url in (
        f"/api/documents/{document_id}/content",
        f"/api/documents/{document_id}/preview-pages",
    ):
        resp = client.get(url)

        assert resp.status_code == 404
        assert resp.json()["error_messages"] == [
            "原本ファイルが保存先にありません。文書をアップロードし直してください。"
        ]
        # 画面はこの code のときだけ iframe に戻さず理由を出す。
        assert resp.json()["error_code"] == "RAG_DOCUMENT_FILE_MISSING"
        assert resp.headers["x-request-id"]


def test_document_content_returns_404_for_unknown_document() -> None:
    """存在しないドキュメントの原本配信は 404。"""
    resp = client.get("/api/documents/unknown/content")

    assert resp.status_code == 404
    assert resp.json()["error_messages"] == ["ドキュメントが見つかりません。"]


def test_ingestion_segments_fallback_uses_table_and_asset_pages(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """checkpoint 未永続化でも table/asset lineage から page range を返す。"""
    document_id = _upload("layout.pdf", b"%PDF-1.7\nsample", "application/pdf")
    artifact_path = f"artifacts/extractions/{document_id}/full.json"
    detail = fake_document_dependencies.documents[document_id]
    fake_document_dependencies.documents[document_id] = detail.model_copy(
        update={
            "status": FileStatus.INDEXED,
            "indexed_at": datetime.now(UTC),
            "extraction": {
                "raw_text": "売上表と説明図を含む PDF",
                "elements": [],
                "tables": [
                    {
                        "table_id": "tbl-1",
                        "page_number": 3,
                        "caption": "四半期売上",
                    }
                ],
                "assets": [
                    {
                        "asset_id": "fig-1",
                        "kind": "figure",
                        "page_number": 5,
                    }
                ],
                "quality_report": {
                    "parser_backend": "docling",
                    "parser_profile": "enterprise_ai_pdf_layout",
                },
                "parser_artifacts": {
                    "extraction_artifact_path": artifact_path,
                },
            },
        }
    )

    resp = client.get(f"/api/documents/{document_id}/ingestion-segments")

    assert resp.status_code == 200
    segments = resp.json()["data"]
    assert len(segments) == 1
    assert segments[0]["page_start"] == 3
    assert segments[0]["page_end"] == 5
    assert segments[0]["progress_unit"] == "page"
    assert segments[0]["progress_start"] == 3
    assert segments[0]["progress_end"] == 5
    assert segments[0]["parser_backend"] == "docling"
    assert segments[0]["artifact_path"] == artifact_path


def test_ingestion_segments_progress_unit_for_persisted_office_segments(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """既存 checkpoint row でも segment_id から slide/sheet 進捗単位を補う。"""
    pptx_id = _upload(
        "deck.pptx",
        b"pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    xlsx_id = _upload(
        "book.xlsx",
        b"xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    fake_document_dependencies.ingestion_segments[pptx_id] = [
        IngestionSegment(
            segment_id=f"{pptx_id}:slide2",
            document_id=pptx_id,
            status="RUNNING",
            page_start=2,
            page_end=2,
        )
    ]
    fake_document_dependencies.ingestion_segments[xlsx_id] = [
        IngestionSegment(
            segment_id=f"{xlsx_id}:sheet3",
            document_id=xlsx_id,
            status="QUEUED",
            page_start=3,
            page_end=3,
        )
    ]

    pptx_resp = client.get(f"/api/documents/{pptx_id}/ingestion-segments")
    xlsx_resp = client.get(f"/api/documents/{xlsx_id}/ingestion-segments")

    assert pptx_resp.status_code == 200
    assert xlsx_resp.status_code == 200
    assert pptx_resp.json()["data"][0]["progress_unit"] == "slide"
    assert pptx_resp.json()["data"][0]["progress_start"] == 2
    assert xlsx_resp.json()["data"][0]["progress_unit"] == "sheet"
    assert xlsx_resp.json()["data"][0]["progress_start"] == 3


def test_ingestion_segments_fallback_uses_planned_parser_for_running_extract_job(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkpoint 作成前の取込中表示は upload 時 profile ではなく現行 parser 設定を使う。"""
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
    )
    document_id = _upload("scan.pdf", b"%PDF-1.7\nsample", "application/pdf")
    job = IngestionJob(
        id="job-unstructured",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        parser_profile="enterprise_ai_pdf_layout",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_jobs[job.id] = job

    resp = client.get(f"/api/documents/{document_id}/ingestion-segments")

    assert resp.status_code == 200
    segments = resp.json()["data"]
    assert len(segments) == 1
    assert segments[0]["status"] == "RUNNING"
    assert segments[0]["parser_backend"] == "unstructured"
    assert segments[0]["parser_profile"] == "unstructured"


def test_ingestion_segments_fallback_uses_document_parser_before_ingest(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """未取込表示は upload 時 profile ではなく文書の現行 parser 設定を使う。"""
    document_id = _upload("scan-before-ingest.pdf", b"%PDF-1.7\nsample", "application/pdf")
    fake_document_dependencies.processing_configs[document_id] = DocumentProcessingConfig(
        parser_adapter_backend="unstructured"
    )

    resp = client.get(f"/api/documents/{document_id}/ingestion-segments")

    assert resp.status_code == 200
    segments = resp.json()["data"]
    assert len(segments) == 1
    assert segments[0]["status"] == "UPLOADED"
    assert segments[0]["parser_backend"] == "unstructured"
    assert segments[0]["parser_profile"] == "unstructured"


def test_delete_document_removes_record_and_original_file(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """document 削除 API は DB record とアップロード原本を削除する。"""
    body = b"sample policy"
    document_id = _upload("policy.txt", body, "text/plain")
    detail = fake_document_dependencies.documents[document_id]
    assert detail.object_storage_path is not None
    assert anyio.run(ObjectStorageClient().get, detail.object_storage_path) == body

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == document_id
    assert data["file_name"] == "policy.txt"
    assert data["object_deleted"] is True
    assert document_id not in fake_document_dependencies.documents
    with pytest.raises(FileNotFoundError):
        anyio.run(ObjectStorageClient().get, detail.object_storage_path)
    assert client.get(f"/api/documents/{document_id}").status_code == 404


def test_delete_document_removes_extraction_artifacts(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """document 削除 API は抽出 artifact cache と segment artifact も best-effort で削除する。"""
    document_id = _upload("policy.txt", b"sample policy", "text/plain")
    full_artifact_path = anyio.run(
        ObjectStorageClient().put,
        f"artifacts/extractions/{document_id}/full.json",
        b'{"raw_text":"redacted"}',
        "application/json",
    )
    segment_artifact_path = anyio.run(
        ObjectStorageClient().put,
        f"artifacts/extractions/{document_id}/segments/p1.json",
        b'{"raw_text":"redacted segment"}',
        "application/json",
    )
    prepared_artifact_path = anyio.run(
        ObjectStorageClient().put,
        f"artifacts/canonical/{document_id}/prepared.pdf",
        b"%PDF-1.7\nprepared",
        "application/pdf",
    )
    duplicate_segment_artifact_path = full_artifact_path
    original_path = fake_document_dependencies.documents[document_id].object_storage_path
    assert original_path is not None
    fake_document_dependencies.documents[document_id] = fake_document_dependencies.documents[
        document_id
    ].model_copy(
        update={
            "status": FileStatus.INDEXED,
            "preprocess_artifact": DocumentPreprocessArtifact(
                derivation_id="der-delete",
                profile="office_pdf",
                converted=True,
                object_storage_path=prepared_artifact_path,
                content_type="application/pdf",
                file_name="policy__prepared.pdf",
            ),
            "extraction": {"parser_artifacts": {"extraction_artifact_path": full_artifact_path}},
        }
    )
    fake_document_dependencies.ingestion_segments[document_id] = [
        IngestionSegment(
            segment_id=f"{document_id}:p1",
            document_id=document_id,
            status="SUCCEEDED",
            artifact_path=segment_artifact_path,
        ),
        IngestionSegment(
            segment_id=f"{document_id}:full",
            document_id=document_id,
            status="SUCCEEDED",
            artifact_path=duplicate_segment_artifact_path,
        ),
        IngestionSegment(
            segment_id=f"{document_id}:source",
            document_id=document_id,
            status="SUCCEEDED",
            artifact_path=original_path,
        ),
    ]

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["object_deleted"] is True
    assert data["artifact_deleted_count"] == 3
    assert data["artifact_delete_failed_count"] == 0
    with pytest.raises(FileNotFoundError):
        anyio.run(ObjectStorageClient().get, full_artifact_path)
    with pytest.raises(FileNotFoundError):
        anyio.run(ObjectStorageClient().get, segment_artifact_path)
    with pytest.raises(FileNotFoundError):
        anyio.run(ObjectStorageClient().get, prepared_artifact_path)
    with pytest.raises(FileNotFoundError):
        anyio.run(ObjectStorageClient().get, original_path)


def test_delete_document_removes_queued_ingestion_state(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """待機中の投入 job / segment は document 削除と同時に消す。"""
    document_id = _upload("queued-policy.txt", b"queued sample", "text/plain")
    fake_document_dependencies.ingestion_jobs["job-queued-delete"] = IngestionJob(
        id="job-queued-delete",
        document_id=document_id,
        status=IngestionJobStatus.QUEUED,
        parser_profile="enterprise_ai_generic",
        queued_at=datetime.now(UTC),
    )
    fake_document_dependencies.ingestion_segments[document_id] = [
        IngestionSegment(
            segment_id=f"{document_id}:p1",
            document_id=document_id,
            status="QUEUED",
            page_start=1,
            page_end=1,
        )
    ]

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 200
    assert document_id not in fake_document_dependencies.documents
    assert "job-queued-delete" not in fake_document_dependencies.ingestion_jobs
    assert document_id not in fake_document_dependencies.ingestion_segments


def test_delete_document_blocks_running_ingestion_job(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """実行中の取込 job がある document は worker 競合を避けて削除を止める。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")
    fake_document_dependencies.ingestion_jobs["job-running"] = IngestionJob(
        id="job-running",
        document_id=document_id,
        status=IngestionJobStatus.RUNNING,
        parser_profile="enterprise_ai_generic",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 409
    assert "先にキャンセルしてください" in resp.json()["error_messages"][0]
    assert document_id in fake_document_dependencies.documents


def test_delete_document_clears_duplicate_references(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """重複元 document を削除すると、残る重複 document の参照を外す。"""
    original_id = _upload("original.txt", b"same body", "text/plain")
    duplicate_id = _upload("duplicate.txt", b"same body", "text/plain")
    assert (
        fake_document_dependencies.documents[duplicate_id].duplicate_of_document_id == original_id
    )

    resp = client.delete(f"/api/documents/{original_id}")

    assert resp.status_code == 200
    assert fake_document_dependencies.documents[duplicate_id].duplicate_of_document_id is None


def test_delete_document_removes_past_ingestion_artifacts_by_prefix(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """参照が残っていない過去の取込の成果物とレシピの preprocess_artifact も消す（#303）。"""
    document_id = _upload("policy.txt", b"sample policy", "text/plain")
    other_id = _upload("other.txt", b"other policy", "text/plain")
    storage = ObjectStorageClient()
    past_paths = [
        anyio.run(
            storage.put,
            f"artifacts/extractions/{document_id}/old-trace.json",
            b'{"raw_text":"old"}',
            "application/json",
        ),
        anyio.run(
            storage.put,
            f"artifacts/extractions/{document_id}/old-trace/segments/p1.json",
            b'{"raw_text":"old segment"}',
            "application/json",
        ),
        anyio.run(
            storage.put,
            f"artifacts/canonical/{document_id}/old-trace/canonical.pdf",
            b"%PDF-1.7\nold",
            "application/pdf",
        ),
    ]
    # prefix の設定を変える前に作ったレシピの成果物（文書の prefix の外）も、行の参照から消す。
    recipe_artifact_path = anyio.run(
        storage.put,
        f"legacy-canonical/{document_id}/recipe-2/prepared.pdf",
        b"%PDF-1.7\nrecipe",
        "application/pdf",
    )
    other_path = anyio.run(
        storage.put,
        f"artifacts/extractions/{other_id}/trace.json",
        b'{"raw_text":"other"}',
        "application/json",
    )

    async def list_document_recipes(requested_id: str) -> list[dict[str, object]]:
        assert requested_id == document_id
        return [
            {"recipe_id": "recipe-1", "preprocess_artifact": None},
            {
                "recipe_id": "recipe-2",
                "preprocess_artifact": {"object_storage_path": recipe_artifact_path},
            },
        ]

    monkeypatch.setattr(
        fake_document_dependencies, "list_document_recipes", list_document_recipes, raising=False
    )

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["artifact_deleted_count"] == 4
    assert body["data"]["artifact_delete_failed_count"] == 0
    assert body["warning_messages"] == []
    for path in [*past_paths, recipe_artifact_path]:
        with pytest.raises(FileNotFoundError):
            anyio.run(storage.get, path)
    assert anyio.run(storage.get, other_path) == b'{"raw_text":"other"}'


def test_delete_document_keeps_deleting_when_prefix_cleanup_fails(
    fake_document_dependencies: FakeWorkspaceOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成果物の後始末に失敗しても文書の削除は止めず、warning で知らせる（#303）。"""
    document_id = _upload("policy.txt", b"sample policy", "text/plain")

    async def failing_delete_prefix(
        self: ObjectStorageClient, prefix: str, **kwargs: object
    ) -> object:
        raise RuntimeError(f"list failed: {prefix}")

    monkeypatch.setattr(ObjectStorageClient, "delete_prefix", failing_delete_prefix)

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["object_deleted"] is True
    assert body["data"]["artifact_delete_failed_count"] == 2
    assert any("artifact" in message for message in body["warning_messages"])
    assert document_id not in fake_document_dependencies.documents


def _fail_storage_delete_for(
    monkeypatch: pytest.MonkeyPatch, failing: set[str], calls: list[str]
) -> None:
    """``failing`` の URI の削除だけを一時的な保存先の障害（OSError）にする。"""
    original_delete = ObjectStorageClient.delete

    async def flaky_delete(self: ObjectStorageClient, key: str) -> bool:
        calls.append(key)
        if key in failing:
            raise OSError("temporary storage failure")
        return await original_delete(self, key)

    monkeypatch.setattr(ObjectStorageClient, "delete", flaky_delete)


def test_delete_document_retries_source_cleanup_after_storage_recovers(
    fake_cleanup_store: FakeDocumentCleanupStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原本の削除に失敗しても、保存先の復旧後に同じ文書の削除で原本を消し直せる（#2452）。"""
    document_id = _upload("policy.txt", b"sample policy", "text/plain")
    other_id = _upload("other.txt", b"other policy", "text/plain")
    storage = ObjectStorageClient()
    source_path = str(fake_cleanup_store.oracle.documents[document_id].object_storage_path)
    other_path = str(fake_cleanup_store.oracle.documents[other_id].object_storage_path)
    failing = {source_path}
    calls: list[str] = []
    _fail_storage_delete_for(monkeypatch, failing, calls)

    first = client.delete(f"/api/documents/{document_id}")

    assert first.status_code == 200
    body = first.json()
    assert body["data"]["object_deleted"] is False
    assert any("原本ファイルの削除に失敗しました" in m for m in body["warning_messages"])
    assert any("やり直します" in m for m in body["warning_messages"])
    assert anyio.run(storage.get, source_path) == b"sample policy"
    [pending] = fake_cleanup_store.pending_for(document_id)
    assert pending.targets.source_uri == source_path
    assert pending.targets.prefixes == ()

    failing.clear()
    retried = client.delete(f"/api/documents/{document_id}")

    assert retried.status_code == 200
    data = retried.json()["data"]
    assert data["id"] == document_id
    assert data["file_name"] == "policy.txt"
    assert data["object_deleted"] is True
    assert retried.json()["warning_messages"] == []
    with pytest.raises(FileNotFoundError):
        anyio.run(storage.get, source_path)
    assert calls.count(source_path) == 2
    assert fake_cleanup_store.pending_for(document_id) == []
    # 後始末が済んだ後の再試行は、これまでどおり 404。別の文書の原本は消さない。
    assert client.delete(f"/api/documents/{document_id}").status_code == 404
    assert anyio.run(storage.get, other_path) == b"other policy"
    assert other_path not in calls


def test_delete_document_retries_artifact_and_prefix_cleanup_in_worker_sweep(
    fake_cleanup_store: FakeDocumentCleanupStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成果物・prefix の削除の失敗も記録に残り、取込ワーカーの定期の後始末で消える（#2452）。"""
    document_id = _upload("policy.txt", b"sample policy", "text/plain")
    other_id = _upload("other.txt", b"other policy", "text/plain")
    storage = ObjectStorageClient()
    artifact_path = anyio.run(
        storage.put,
        f"legacy-artifacts/{document_id}/full.json",
        b'{"raw_text":"redacted"}',
        "application/json",
    )
    past_path = anyio.run(
        storage.put,
        f"artifacts/extractions/{document_id}/old-trace.json",
        b'{"raw_text":"old"}',
        "application/json",
    )
    other_artifact = anyio.run(
        storage.put,
        f"artifacts/extractions/{other_id}/trace.json",
        b'{"raw_text":"other"}',
        "application/json",
    )
    documents = fake_cleanup_store.oracle.documents
    documents[document_id] = documents[document_id].model_copy(
        update={"extraction": {"parser_artifacts": {"extraction_artifact_path": artifact_path}}}
    )
    failing = {artifact_path}
    calls: list[str] = []
    _fail_storage_delete_for(monkeypatch, failing, calls)
    original_delete_prefix = ObjectStorageClient.delete_prefix
    prefix_failing = [True]

    async def flaky_delete_prefix(self: ObjectStorageClient, prefix: str, **kwargs: bool) -> object:
        if prefix_failing[0]:
            raise OSError(f"list failed: {prefix}")
        return await original_delete_prefix(self, prefix, **kwargs)

    monkeypatch.setattr(ObjectStorageClient, "delete_prefix", flaky_delete_prefix)

    resp = client.delete(f"/api/documents/{document_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["object_deleted"] is True
    assert body["data"]["artifact_delete_failed_count"] == 3
    assert any("artifact" in message for message in body["warning_messages"])
    [pending] = fake_cleanup_store.pending_for(document_id)
    assert pending.targets.source_uri is None
    assert pending.targets.artifact_uris == (artifact_path,)
    assert len(pending.targets.prefixes) == 2
    assert anyio.run(storage.get, past_path) == b'{"raw_text":"old"}'

    failing.clear()
    prefix_failing[0] = False
    completed = anyio.run(
        lambda: retry_due_document_cleanups(
            limit=10, min_age_seconds=0, store=fake_cleanup_store, storage=storage
        )
    )

    assert completed == 1
    assert fake_cleanup_store.pending_for(document_id) == []
    for path in (artifact_path, past_path):
        with pytest.raises(FileNotFoundError):
            anyio.run(storage.get, path)
    assert anyio.run(storage.get, other_artifact) == b'{"raw_text":"other"}'
    assert other_id in documents


def test_document_cleanup_record_survives_interruption_after_db_delete(
    fake_cleanup_store: FakeDocumentCleanupStore,
) -> None:
    """文書の行を消した直後に止まっても、記録から原本・成果物を消せる。繰り返しても成功する。"""
    document_id = _upload("policy.txt", b"sample policy", "text/plain")
    storage = ObjectStorageClient()
    source_path = str(fake_cleanup_store.oracle.documents[document_id].object_storage_path)
    past_path = anyio.run(
        storage.put,
        f"artifacts/canonical/{document_id}/old-trace/canonical.pdf",
        b"%PDF-1.7\nold",
        "application/pdf",
    )
    targets = DocumentCleanupTargets(
        source_uri=source_path,
        prefixes=(f"artifacts/canonical/{document_id}/", f"artifacts/extractions/{document_id}/"),
    )

    # DB の削除（と記録）だけが済み、保存先の後始末の前にプロセスが止まった状態。
    task = anyio.run(
        fake_cleanup_store.delete_document_with_cleanup, document_id, "policy.txt", targets
    )

    assert task is not None
    assert document_id not in fake_cleanup_store.oracle.documents
    [pending] = fake_cleanup_store.pending_for(document_id)
    assert pending.targets == targets
    # 原本が既に無くても（別の経路で消えた）、後始末は正常に終わる。
    anyio.run(storage.delete, source_path)

    def sweep() -> int:
        return anyio.run(
            lambda: retry_due_document_cleanups(
                limit=10, min_age_seconds=0, store=fake_cleanup_store, storage=storage
            )
        )

    assert sweep() == 1
    assert fake_cleanup_store.pending_for(document_id) == []
    with pytest.raises(FileNotFoundError):
        anyio.run(storage.get, past_path)
    assert sweep() == 0
    # 同じ記録をもう一度実行しても（消せたものは無いものとして）成功する。
    fake_cleanup_store.tasks[task.cleanup_id] = task
    fake_cleanup_store.owners[task.cleanup_id] = None
    assert sweep() == 1


def test_document_cleanup_does_not_delete_files_of_remaining_documents(
    fake_cleanup_store: FakeDocumentCleanupStore,
) -> None:
    """記録の URI を別の文書が参照していれば、その URI と同じ ID の文書の prefix は消さない。"""
    remaining_id = _upload("remaining.txt", b"remaining policy", "text/plain")
    storage = ObjectStorageClient()
    remaining_source = str(fake_cleanup_store.oracle.documents[remaining_id].object_storage_path)
    remaining_artifact = anyio.run(
        storage.put,
        f"artifacts/extractions/{remaining_id}/trace.json",
        b'{"raw_text":"remaining"}',
        "application/json",
    )
    fake_cleanup_store.oracle.ingestion_segments[remaining_id] = [
        IngestionSegment(
            segment_id=f"{remaining_id}:p1",
            document_id=remaining_id,
            status="SUCCEEDED",
            artifact_path=remaining_artifact,
        )
    ]
    task = DocumentCleanupTask(
        cleanup_id="cleanup-collision",
        document_id=remaining_id,
        file_name="deleted.txt",
        targets=DocumentCleanupTargets(
            source_uri=remaining_source,
            artifact_uris=(remaining_artifact,),
            prefixes=(f"artifacts/extractions/{remaining_id}/",),
        ),
    )
    fake_cleanup_store.tasks[task.cleanup_id] = task
    fake_cleanup_store.owners[task.cleanup_id] = None

    completed = anyio.run(
        lambda: retry_due_document_cleanups(
            limit=10, min_age_seconds=0, store=fake_cleanup_store, storage=storage
        )
    )

    assert completed == 1
    assert fake_cleanup_store.tasks == {}
    assert anyio.run(storage.get, remaining_source) == b"remaining policy"
    assert anyio.run(storage.get, remaining_artifact) == b'{"raw_text":"remaining"}'


def test_bulk_delete_keeps_partial_failures_independent(
    fake_document_dependencies: FakeWorkspaceOracle,
    fake_cleanup_store: FakeDocumentCleanupStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括削除（文書ごとの DELETE）は、実行中の job の文書だけ 409 にする。

    文書ごとの結果は互いに影響しない（部分失敗の扱いは変えない）。
    """
    deleted_id = _upload("a.txt", b"body a", "text/plain")
    running_id = _upload("b.txt", b"body b", "text/plain")
    flaky_id = _upload("c.txt", b"body c", "text/plain")
    fake_document_dependencies.ingestion_jobs["job-running"] = IngestionJob(
        id="job-running",
        document_id=running_id,
        status=IngestionJobStatus.RUNNING,
        parser_profile="enterprise_ai_generic",
        queued_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    flaky_source = str(fake_cleanup_store.oracle.documents[flaky_id].object_storage_path)
    _fail_storage_delete_for(monkeypatch, {flaky_source}, [])

    statuses = [
        client.delete(f"/api/documents/{document_id}").status_code
        for document_id in (deleted_id, running_id, flaky_id)
    ]

    assert statuses == [200, 409, 200]
    assert running_id in fake_document_dependencies.documents
    assert deleted_id not in fake_document_dependencies.documents
    assert flaky_id not in fake_document_dependencies.documents
    assert fake_cleanup_store.pending_for(deleted_id) == []
    assert fake_cleanup_store.pending_for(running_id) == []
    assert [task.targets.source_uri for task in fake_cleanup_store.pending_for(flaky_id)] == [
        flaky_source
    ]


def test_delete_impact_counts_duplicates_that_rely_on_the_source(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """正本の削除の前に、正本を参照する重複文書の件数と所属 KB を返す（#303）。"""
    source_id = _upload("original.txt", b"same body", "text/plain")
    duplicate_id = _upload("duplicate.txt", b"same body", "text/plain")
    indexed_duplicate_id = _upload("duplicate-indexed.txt", b"same body", "text/plain")
    co_deleted_duplicate_id = _upload("duplicate-selected.txt", b"same body", "text/plain")
    standalone_id = _upload("standalone.txt", b"other body", "text/plain")
    anyio.run(
        fake_document_dependencies.assign_documents_to_knowledge_base,
        "kb-hr",
        [duplicate_id],
    )
    anyio.run(
        fake_document_dependencies.assign_documents_to_knowledge_base,
        "kb-indexed",
        [indexed_duplicate_id],
    )
    anyio.run(
        fake_document_dependencies.assign_documents_to_knowledge_base,
        "kb-selected",
        [co_deleted_duplicate_id],
    )
    # 自前の索引を持つ重複文書は、正本を消しても検索対象が消えない。
    fake_document_dependencies.documents[indexed_duplicate_id] = (
        fake_document_dependencies.documents[indexed_duplicate_id].model_copy(
            update={"status": FileStatus.INDEXED}
        )
    )

    resp = client.get(
        "/api/documents/delete-impact",
        params=[
            ("document_id", source_id),
            ("document_id", standalone_id),
            ("document_id", co_deleted_duplicate_id),
        ],
    )

    assert resp.status_code == 200
    impacts = {impact["document_id"]: impact for impact in resp.json()["data"]}
    assert impacts[source_id]["duplicate_count"] == 1
    assert [kb["id"] for kb in impacts[source_id]["knowledge_bases"]] == ["kb-default", "kb-hr"]
    assert impacts[standalone_id] == {
        "document_id": standalone_id,
        "duplicate_count": 0,
        "knowledge_bases": [],
    }
    assert impacts[co_deleted_duplicate_id]["duplicate_count"] == 0


def test_delete_impact_requires_document_ids() -> None:
    """対象の文書を指定しない問い合わせは 422 にする。"""
    assert client.get("/api/documents/delete-impact").status_code == 422
    assert (
        client.get("/api/documents/delete-impact", params={"document_id": " "}).status_code == 422
    )


def test_document_detail_returns_extraction_after_ingest() -> None:
    """取込 job 実行後の詳細 API は抽出本文とメタデータを返す。"""
    document_id = _upload(
        "policy.txt",
        "社内規程: 経費申請\n部門長が承認します。".encode(),
        "text/plain",
    )
    ingest = client.post(f"/api/documents/{document_id}/ingestion-jobs")
    assert ingest.status_code == 200
    job = ingest.json()["data"]
    assert job["document_id"] == document_id
    assert job["status"] == "QUEUED"
    anyio.run(documents_route._run_ingestion_job, job["id"])

    detail = client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    extraction = detail.json()["data"]["extraction"]
    assert extraction["document_type"] == "社内規程"
    assert "部門長が承認" in extraction["raw_text"]
    assert "fields" not in extraction

    jobs = client.get("/api/documents/ingestion-jobs", params={"status": "SUCCEEDED"})
    assert jobs.status_code == 200
    assert any(job["document_id"] == document_id for job in jobs.json()["data"]["items"])


def test_document_extraction_export_returns_markdown_view(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """保存済み StructuredExtraction を Markdown で監査できる。"""
    document_id = _upload("manual.html", "<h1>検索運用</h1>".encode(), "text/html")
    extraction = {
        "raw_text": "検索運用\nインデックスを確認します。",
        "document_type": "運用マニュアル",
        "confidence": 0.91,
        "elements": [
            {
                "kind": "title",
                "text": "検索運用",
                "order": 0,
                "element_id": "h1",
                "page_number": 1,
                "section_path": ["検索運用"],
            },
            {
                "kind": "code",
                "text": "ragctl reindex",
                "order": 1,
                "element_id": "code-1",
                "page_number": 1,
                "section_path": ["検索運用"],
                "metadata": {"code_language": "bash"},
            },
            {
                "kind": "table",
                "text": "|項目|値|\n|-|-|\n|状態|INDEXED|",
                "order": 2,
                "element_id": "tbl-1",
                "page_number": 2,
                "section_path": ["検索運用", "確認表"],
            },
        ],
        "assets": [
            {
                "asset_id": "fig-1",
                "kind": "figure",
                "page_number": 2,
                "bbox": {"x": 10, "y": 20, "w": 30, "h": 40},
                "alt_text": "検索フロー図",
            }
        ],
        "quality_report": {
            "parser_backend": "local_partition",
            "parser_profile": "local_html_semantic",
            "page_count": 2,
            "page_coverage": 1.0,
            "element_count": 3,
        },
    }
    recipe_id = _seed_recipe(
        fake_document_dependencies, document_id, status=FileStatus.INDEXED, extraction=extraction
    )

    resp = client.get(
        f"/api/documents/{document_id}/recipes/{recipe_id}/extraction-export",
        params={"format": DocumentExtractionExportFormat.MARKDOWN.value},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["format"] == "markdown"
    assert data["content_type"].startswith("text/markdown")
    assert data["parser_backend"] == "local_partition"
    assert data["parser_profile"] == "local_html_semantic"
    assert data["page_count"] == 2
    assert data["element_count"] == 3
    assert data["asset_count"] == 1
    assert "<!-- page: 1 -->" in data["content"]
    assert "# 検索運用" in data["content"]
    assert "```bash\nragctl reindex\n```" in data["content"]
    assert "|状態|INDEXED|" in data["content"]
    assert "> Asset: figure `fig-1`" in data["content"]
    assert "> bbox: 10,20,40,60" in data["content"]
    assert "> alt: 検索フロー図" in data["content"]
    assert data["payload"] == {}


def test_document_extraction_export_returns_html_view(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """HTML export は保存済み extraction から escaped review HTML を返す。"""
    document_id = _upload("manual.html", b"<h1>html</h1>", "text/html")
    extraction = {
        "raw_text": "検索運用\n<script>alert(1)</script>",
        "document_type": "運用マニュアル",
        "elements": [
            {
                "kind": "title",
                "text": "検索運用",
                "order": 0,
                "element_id": "title-1",
                "page_number": 1,
                "bbox": [0, 0, 100, 20],
            },
            {
                "kind": "text",
                "text": "<script>alert(1)</script>",
                "order": 1,
                "element_id": "txt-1",
                "page_number": 1,
                "content_kind": "text",
            },
            {
                "kind": "code",
                "text": "ragctl reindex",
                "order": 2,
                "element_id": "code-1",
                "page_number": 1,
                "metadata": {"code_language": "bash"},
            },
            {
                "kind": "table",
                "text": "|項目|値|\n|-|-|\n|状態|INDEXED|",
                "order": 3,
                "element_id": "tbl-1",
                "page_number": 2,
            },
        ],
        "tables": [
            {
                "table_id": "tbl-1",
                "element_id": "tbl-1",
                "page_number": 2,
                "caption": "検索状態表",
                "cells": [
                    {"row": 0, "col": 0, "text": "項目"},
                    {"row": 0, "col": 1, "text": "値"},
                    {
                        "row": 1,
                        "col": 0,
                        "text": "状態",
                        "bbox": {
                            "left": 10,
                            "top": 20,
                            "right": 30,
                            "bottom": 40,
                        },
                    },
                    {
                        "row": 1,
                        "col": 1,
                        "text": "<INDEXED>",
                        "metadata": {
                            "formula_cell_ref": "B2",
                            "equation_format": "excel_formula",
                            "formula": 'IF(A2="状態","<INDEXED>","ERROR")',
                            "formula_value": "<INDEXED>",
                        },
                    },
                ],
            }
        ],
        "assets": [
            {
                "asset_id": "fig-1",
                "kind": "figure",
                "object_path": "oci://namespace/bucket/internal/figure.png",
                "page_number": 2,
                "bbox": {"x": 5, "y": 10, "w": 25, "h": 35},
                "alt_text": "<画像プレビュー>",
            }
        ],
        "quality_report": {
            "parser_backend": "local_partition",
            "parser_profile": "local_html_semantic",
            "page_count": 2,
            "page_coverage": 1.0,
            "element_count": 4,
        },
    }
    recipe_id = _seed_recipe(
        fake_document_dependencies, document_id, status=FileStatus.INDEXED, extraction=extraction
    )

    resp = client.get(
        f"/api/documents/{document_id}/recipes/{recipe_id}/extraction-export",
        params={"format": DocumentExtractionExportFormat.HTML.value},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["format"] == "html"
    assert data["content_type"].startswith("text/html")
    assert data["payload"] == {}
    assert '<article data-document-type="運用マニュアル">' in data["content"]
    assert '<p class="page-marker" data-page="1">page 1</p>' in data["content"]
    assert '<h1 data-element-id="title-1"' in data["content"]
    assert 'data-bbox="0,0,100,20"' in data["content"]
    assert ">検索運用</h1>" in data["content"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in data["content"]
    assert "<script>alert(1)</script>" not in data["content"]
    assert '<code class="language-bash">ragctl reindex</code>' in data["content"]
    assert '<p class="table-caption" data-table-id="tbl-1">検索状態表</p>' in data["content"]
    assert '<table data-element-id="tbl-1"' in data["content"]
    assert 'class="table-block" data-table-id="tbl-1"' in data["content"]
    assert '<th data-table-id="tbl-1" data-row="0" data-col="0">項目</th>' in data["content"]
    assert 'data-bbox="10,20,30,40"' in data["content"]
    assert 'data-formula-ref="B2"' in data["content"]
    assert 'data-formula-format="excel_formula"' in data["content"]
    escaped_formula = (
        'data-formula="IF(A2=&quot;状態&quot;,&quot;&lt;INDEXED&gt;&quot;,&quot;ERROR&quot;)"'
    )
    assert escaped_formula in data["content"]
    assert 'data-formula-value="&lt;INDEXED&gt;"' in data["content"]
    assert 'class="asset-block"' in data["content"]
    assert 'data-asset-id="fig-1"' in data["content"]
    assert 'data-kind="figure"' in data["content"]
    assert 'data-page="2"' in data["content"]
    assert 'data-bbox="5,10,30,45"' in data["content"]
    assert "&lt;画像プレビュー&gt;" in data["content"]
    assert "oci://namespace/bucket/internal/figure.png" not in data["content"]
    assert "&lt;INDEXED&gt;" in data["content"]
    assert "<INDEXED>" not in data["content"]


def test_document_extraction_export_returns_json_payload(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """JSON export は保存済み extraction payload を機械可読に返す。"""
    document_id = _upload("policy.txt", b"policy", "text/plain")
    extraction = {
        "raw_text": "部門長が承認します。",
        "document_type": "社内規程",
        "confidence": 0.9,
        "elements": [
            {
                "kind": "text",
                "text": "部門長が承認します。",
                "order": 0,
                "element_id": "el-1",
            }
        ],
        "parser_artifacts": {"parser_backend": "local_partition"},
    }
    recipe_id = _seed_recipe(
        fake_document_dependencies, document_id, status=FileStatus.INDEXED, extraction=extraction
    )

    resp = client.get(
        f"/api/documents/{document_id}/recipes/{recipe_id}/extraction-export",
        params={"format": DocumentExtractionExportFormat.JSON.value},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["format"] == "json"
    assert data["content_type"].startswith("application/json")
    assert data["payload"]["document_type"] == "社内規程"
    assert data["payload"]["elements"][0]["element_id"] == "el-1"
    assert '"document_type": "社内規程"' in data["content"]


def test_document_extraction_export_returns_chunk_view_without_embeddings(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """chunks export は embedding なしの可視化 metadata だけを返す。"""
    document_id = _upload("policy.txt", b"policy", "text/plain")
    extraction = {"raw_text": "承認条件", "document_type": "社内規程"}
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.INDEXED,
        extraction=extraction,
        chunks=[
            DocumentChunkView(
                document_id=document_id,
                chunk_id=f"{document_id}:0",
                chunk_index=0,
                text="承認条件: 部門長",
                page_start=1,
                page_end=1,
                section_path="経費申請 > 承認",
                content_kind="text",
                element_ids=["el-1"],
                metadata={"chunk_template": "markdown_by_heading"},
            )
        ],
    )

    resp = client.get(
        f"/api/documents/{document_id}/recipes/{recipe_id}/extraction-export",
        params={"format": DocumentExtractionExportFormat.CHUNKS.value},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["format"] == "chunks"
    assert data["content_type"].startswith("application/json")
    assert data["chunks"][0]["chunk_id"] == f"{document_id}:0"
    assert data["chunks"][0]["element_ids"] == ["el-1"]
    assert "embedding" not in data["content"].casefold()


@pytest.mark.parametrize(
    ("export_format", "suffix", "content_type"),
    [
        (DocumentExtractionExportFormat.MARKDOWN, ".md", "text/markdown"),
        (DocumentExtractionExportFormat.HTML, ".html", "text/html"),
        (DocumentExtractionExportFormat.JSON, ".json", "application/json"),
        (DocumentExtractionExportFormat.CHUNKS, "_chunks.json", "application/json"),
    ],
    ids=["markdown", "html", "json", "chunks"],
)
def test_document_extraction_export_download_returns_attachment(
    fake_document_dependencies: FakeWorkspaceOracle,
    export_format: DocumentExtractionExportFormat,
    suffix: str,
    content_type: str,
) -> None:
    """download=true は同じ内容を、文書名とレシピの分かるファイル名の添付で返す(#561)。"""
    document_id = _upload("経費規程.txt", b"policy", "text/plain")
    extraction = {
        "raw_text": "承認条件",
        "document_type": "社内規程",
        "elements": [{"kind": "text", "text": "承認条件", "order": 0, "element_id": "el-1"}],
    }
    recipe_id = _seed_recipe(
        fake_document_dependencies,
        document_id,
        status=FileStatus.INDEXED,
        extraction=extraction,
        chunks=[
            DocumentChunkView(
                document_id=document_id,
                chunk_id=f"{document_id}:0",
                chunk_index=0,
                text="承認条件",
                element_ids=["el-1"],
            )
        ],
    )
    url = f"/api/documents/{document_id}/recipes/{recipe_id}/extraction-export"

    view = client.get(url, params={"format": export_format.value})
    resp = client.get(url, params={"format": export_format.value, "download": "true"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(content_type)
    expected_name = quote(f"経費規程_レシピ1{suffix}")
    assert resp.headers["content-disposition"] == f"attachment; filename*=UTF-8''{expected_name}"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "private, no-store"
    # 画面の表示(JSON の envelope)と同じ本文をファイルにする。
    assert resp.content.decode("utf-8") == view.json()["data"]["content"]
    if export_format == DocumentExtractionExportFormat.HTML:
        assert resp.headers["content-security-policy"] == "sandbox"
    else:
        assert "content-security-policy" not in resp.headers


def test_document_extraction_export_download_without_extraction_returns_404(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """抽出結果が無いレシピは、ダウンロードでもファイルを返さず 404 にする。"""
    document_id = _upload("policy.txt", b"policy", "text/plain")
    recipe_id = _seed_recipe(fake_document_dependencies, document_id, status=FileStatus.UPLOADED)

    resp = client.get(
        f"/api/documents/{document_id}/recipes/{recipe_id}/extraction-export",
        params={"format": "markdown", "download": "true"},
    )

    assert resp.status_code == 404
    assert "content-disposition" not in resp.headers


def test_fields_edit_endpoint_is_not_available() -> None:
    """帳票向けの抽出フィールド編集 endpoint は提供しない。"""
    document_id = _upload("policy.txt", b"sample", "text/plain")

    resp = client.patch(
        f"/api/documents/{document_id}/fields",
        json={"fields": {"document_number": "DOC-001"}},
    )

    assert resp.status_code == 404


def test_document_ingestion_job_endpoint_rejects_second_enqueue_while_queued(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """#281: 待機中の job がある文書へ再投入しても、出力を初期化せず 409 を返す。

    投入直後は worker が拾うまで文書は UPLOADED のままなので、一覧の「ファイル準備を実行」や
    一括投入から二重に押せる。以前は Oracle のレシピ単位の拒否(ValueError)が 500 になり、
    しかもその前に出力の初期化が走っていた。
    """
    document_id = _upload("queued-twice.txt", b"queued twice", "text/plain")
    first = client.post(f"/api/documents/{document_id}/ingestion-jobs")
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "QUEUED"
    reset_calls: list[str] = []
    original_reset = fake_document_dependencies.reset_document_ingestion_outputs

    async def tracking_reset(document_id: str, **kwargs: object) -> DocumentDetail:
        reset_calls.append(document_id)
        return await original_reset(document_id, **kwargs)  # type: ignore[arg-type]

    fake_document_dependencies.reset_document_ingestion_outputs = tracking_reset  # type: ignore[method-assign]

    second = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert second.status_code == 409
    assert second.json()["error_messages"] == [
        "このドキュメントは取込待ちまたは取込中です。完了してから再実行してください。"
    ]
    assert reset_calls == []
    jobs = [
        job
        for job in fake_document_dependencies.ingestion_jobs.values()
        if job.document_id == document_id
    ]
    assert len(jobs) == 1


def test_document_ingestion_job_endpoint_allows_enqueue_while_other_recipe_job_queued(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """別レシピ(2・3 件目)の待機中 job は、文書単位(既定レシピ)の投入を妨げない。"""
    document_id = _upload("other-recipe.txt", b"other recipe", "text/plain")
    fake_document_dependencies.ingestion_jobs["job-other-recipe"] = IngestionJob(
        id="job-other-recipe",
        document_id=document_id,
        recipe_id="recipe-2",
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "QUEUED"


def test_document_ingestion_job_endpoint_maps_recipe_conflict_to_409(
    fake_document_dependencies: FakeWorkspaceOracle,
) -> None:
    """Oracle の job 作成が同じレシピの競合(ValueError)を返したら 500 ではなく 409 にする。"""
    document_id = _upload("race.txt", b"race", "text/plain")

    async def conflicting_create(job: IngestionJob) -> IngestionJob:
        raise ValueError("このレシピは処理中または待機中です。")

    fake_document_dependencies.create_ingestion_job = conflicting_create  # type: ignore[method-assign]

    resp = client.post(f"/api/documents/{document_id}/ingestion-jobs")

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["このレシピは処理中または待機中です。"]


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [
        ("page.html", "text/html"),
        ("page.htm", "text/html"),
    ],
)
def test_document_content_sandboxes_scriptable_types(file_name: str, content_type: str) -> None:
    """#281: アップロードした HTML を原本配信で開いてもスクリプトを動かさない。"""
    body = b"<html><body><script>alert(1)</script></body></html>"
    document_id = _upload(file_name, body, content_type)

    resp = client.get(f"/api/documents/{document_id}/content")

    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == "sandbox"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_document_content_does_not_sandbox_plain_text() -> None:
    """テキスト・PDF などは従来どおり(CSP sandbox は PDF viewer を妨げるため付けない)。"""
    document_id = _upload("plain.txt", b"plain", "text/plain")

    resp = client.get(f"/api/documents/{document_id}/content")

    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers


def test_record_boundaries_are_hidden_in_review_and_exports() -> None:
    from rag_parser_core.structured_records import (
        JSONL_RECORD_BOUNDARY,
        STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY,
        join_record_boundary_text,
    )

    original = ["id: row-a\nvalue: 10", "id: row-b\nvalue: 20"]
    raw = join_record_boundary_text(original)
    extraction = StructuredExtraction(
        raw_text=raw,
        parser_artifacts={STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY: JSONL_RECORD_BOUNDARY},
    )
    assert extraction.to_document_payload()["display_text"] == "\n".join(original)
    assert extraction.raw_text == raw
    # 要素が無い場合の表示も同じ復元を使う。
    extraction.elements = []
    assert JSONL_RECORD_BOUNDARY not in documents_route._extraction_markdown(extraction)
    assert JSONL_RECORD_BOUNDARY not in documents_route._extraction_html(extraction)
    literal = StructuredExtraction(raw_text=f"原文の記号: {JSONL_RECORD_BOUNDARY}")
    assert JSONL_RECORD_BOUNDARY in str(literal.to_document_payload()["display_text"])
