"""取込ジョブの一時的な DB エラーの再キューと、ポーリングの API の軽い経路(#341)。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.routes import documents as documents_route
from app.clients.file_storage import FileStorageClient
from app.config import Settings
from app.schemas.document import (
    DocumentDetail,
    DocumentSummary,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
    IngestionSegment,
)
from app.schemas.language_pack_release import LanguagePackRelease
from tests.language_release_support import install_language_release_stub
from tests.test_document_recipes import _extract_job, _FakeRecipeJobOracle


@pytest.fixture(autouse=True)
def published_release(monkeypatch: pytest.MonkeyPatch) -> LanguagePackRelease:
    return install_language_release_stub(monkeypatch)


def _dead_session_error() -> RuntimeError:
    return RuntimeError(
        SimpleNamespace(
            is_session_dead=True,
            isrecoverable=True,
            full_code="DPY-4011",
            code=0,
        )
    )


class _TransientFailPipeline:
    """ingest の途中で DB の接続断(DPY-4011)を起こす fake pipeline。"""

    def __init__(self, *, oracle: _FakeRecipeJobOracle, **_kwargs: object) -> None:
        self._oracle = oracle

    async def ingest(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise _dead_session_error()


async def _prepare_recipe_job(
    monkeypatch: pytest.MonkeyPatch, *, attempt_count: int, max_attempts: int = 3
) -> _FakeRecipeJobOracle:
    fake = _FakeRecipeJobOracle()
    fake.jobs["job-extract-1"] = _extract_job().model_copy(
        update={"attempt_count": attempt_count, "max_attempts": max_attempts}
    )
    await FileStorageClient().put("prepared/policy.pdf", b"prepared pdf bytes", "application/pdf")
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "IngestionPipeline", _TransientFailPipeline)
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=False),
    )
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *a, **k: None)
    return fake


async def test_recipe_job_transient_db_error_requeues_job_and_restores_recipe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一時的な DB エラーで、試行回数が残っていれば QUEUED に戻し、レシピも工程の前に戻す。"""
    fake = await _prepare_recipe_job(monkeypatch, attempt_count=0)

    with caplog.at_level(logging.WARNING, logger="app.api.routes.documents"):
        await documents_route._run_ingestion_job("job-extract-1")

    job = fake.jobs["job-extract-1"]
    assert job.status == IngestionJobStatus.QUEUED
    assert job.attempt_count == 1
    assert job.finished_at is None
    # EXTRACT の前の状態(ファイル準備済み)へ戻す。ERROR にはしない。
    assert fake.recipe_status == FileStatus.PREPROCESSED
    record = next(
        item
        for item in caplog.records
        if item.message == "ingestion_job_requeued_transient_db_error"
    )
    assert record.__dict__["full_code"] == "DPY-4011"
    assert record.__dict__["oracle_error_code"] == 0
    assert record.__dict__["attempt_count"] == 1


async def test_recipe_job_transient_db_error_fails_when_attempts_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """試行回数を使い切ったら今までどおり FAILED にし、ログに Oracle のエラーコードを出す。"""
    fake = await _prepare_recipe_job(monkeypatch, attempt_count=2, max_attempts=3)

    with caplog.at_level(logging.ERROR, logger="app.api.routes.documents"):
        await documents_route._run_ingestion_job("job-extract-1")

    assert fake.jobs["job-extract-1"].status == IngestionJobStatus.FAILED
    assert fake.recipe_status == FileStatus.ERROR
    record = next(item for item in caplog.records if item.message == "ingestion_job_failed")
    assert record.__dict__["full_code"] == "DPY-4011"
    assert record.__dict__["oracle_error_code"] == 0


async def test_recipe_job_non_transient_error_fails_immediately(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一時的でない例外は試行回数が残っていても FAILED(ログのキーは常に出す)。"""
    fake = await _prepare_recipe_job(monkeypatch, attempt_count=0)

    async def ingest(self: object, *args: object, **kwargs: object) -> None:
        _ = self, args, kwargs
        raise RuntimeError("壊れた入力")

    monkeypatch.setattr(_TransientFailPipeline, "ingest", ingest)

    with caplog.at_level(logging.ERROR, logger="app.api.routes.documents"):
        await documents_route._run_ingestion_job("job-extract-1")

    assert fake.jobs["job-extract-1"].status == IngestionJobStatus.FAILED
    record = next(item for item in caplog.records if item.message == "ingestion_job_failed")
    assert "oracle_error_code" in record.__dict__
    assert "full_code" in record.__dict__
    assert record.__dict__["full_code"] is None


async def test_recipe_job_transient_error_after_cancel_does_not_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """再キューの直前に取り消されていたら QUEUED に戻さない(CANCELLED を上書きしない)。"""
    fake = await _prepare_recipe_job(monkeypatch, attempt_count=0)
    original_transition = fake.transition_ingestion_job

    async def cancel_then_transition(job_id: str, **kwargs: Any) -> IngestionJob | None:
        job = fake.jobs[job_id]
        fake.jobs[job_id] = job.model_copy(update={"status": IngestionJobStatus.CANCELLED})
        return await original_transition(job_id, **kwargs)

    monkeypatch.setattr(fake, "transition_ingestion_job", cancel_then_transition)

    await documents_route._run_ingestion_job("job-extract-1")

    assert fake.jobs["job-extract-1"].status == IngestionJobStatus.CANCELLED
    assert fake.recipe_status == FileStatus.PREPROCESSED


class _DocumentJobOracle(_FakeRecipeJobOracle):
    """文書単位の job の fake。文書の status の書き込みを記録する。"""

    def __init__(self, release: LanguagePackRelease) -> None:
        super().__init__()
        self.release = release
        self.document_statuses: list[FileStatus] = []

    async def get_document(self, document_id: str) -> DocumentDetail:
        detail = await super().get_document(document_id)
        return detail.model_copy(
            update={
                "extraction": {
                    "parser_artifacts": {
                        "language_pack_release_id": self.release.id,
                        "language_pack_ingest_hash": self.release.ingest_hash,
                    }
                }
            }
        )

    async def update_document_status(
        self, document_id: str, status: FileStatus, error_message: str | None = None
    ) -> None:
        _ = document_id, error_message
        self.document_statuses.append(status)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (IngestionJobPhase.PREPROCESS, FileStatus.UPLOADED),
        (IngestionJobPhase.CHUNK, FileStatus.REVIEW),
        (IngestionJobPhase.INDEX, FileStatus.CHUNKED),
    ],
)
async def test_document_job_transient_db_error_requeues_and_restores_document(
    monkeypatch: pytest.MonkeyPatch,
    phase: IngestionJobPhase,
    expected: FileStatus,
    published_release: LanguagePackRelease,
) -> None:
    """文書単位の job も、一時的な DB エラーなら QUEUED に戻し、文書を工程の前の状態に戻す。"""
    fake = _DocumentJobOracle(published_release)
    fake.jobs["job-doc-1"] = IngestionJob(
        id="job-doc-1",
        document_id="doc-1",
        status=IngestionJobStatus.QUEUED,
        phase=phase,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *a, **k: None)

    async def fail(*args: object, **kwargs: object) -> DocumentDetail:
        _ = args, kwargs
        raise _dead_session_error()

    async def noop(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(documents_route, "_ingest_existing_document", fail)
    monkeypatch.setattr(documents_route, "_chunk_reviewed_document", fail)
    monkeypatch.setattr(documents_route, "_index_reviewed_document", fail)
    monkeypatch.setattr(documents_route, "_reset_document_outputs_for_extract", noop)

    await documents_route._run_ingestion_job("job-doc-1")

    assert fake.jobs["job-doc-1"].status == IngestionJobStatus.QUEUED
    assert fake.document_statuses == [expected]


# ---------------------------------------------------------------------------
# ポーリングの API は JSON 列を読む get_document を呼ばない
# ---------------------------------------------------------------------------


def _summary(document_id: str = "doc-1") -> DocumentSummary:
    return DocumentSummary(
        id=document_id,
        file_name="policy.pdf",
        status=FileStatus.INGESTING,
        content_type="application/pdf",
        content_sha256="a" * 64,
        uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _LightOracle:
    """get_document(JSON 列を読む)を呼んだら失敗する fake。"""

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        raise AssertionError(f"get_document を呼ばない: {document_id}")

    async def document_exists(self, document_id: str) -> bool:
        return document_id == "doc-1"

    async def get_document_summary(self, document_id: str) -> DocumentSummary | None:
        return _summary(document_id) if document_id == "doc-1" else None

    async def list_document_ingestion_jobs(
        self, document_id: str, *, status: IngestionJobStatus | None = None
    ) -> list[IngestionJob]:
        _ = document_id, status
        return []

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        return [
            IngestionSegment(
                segment_id=f"{document_id}:p1-2",
                document_id=document_id,
                status="RUNNING",
                parser_backend="local",
                parser_profile="local_text_structure",
                page_start=1,
                page_end=2,
            )
        ]

    async def list_document_knowledge_bases(self, document_id: str) -> list[object]:
        _ = document_id
        return []

    async def list_document_chunk_sets(self, document_id: str) -> list[dict[str, object]]:
        _ = document_id
        return []

    async def list_document_knowledge_base_configs(self, document_id: str) -> dict[str, object]:
        _ = document_id
        return {}

    async def list_artifact_layers_for_chunk_sets(self, chunk_set_ids: list[str]) -> dict[str, Any]:
        _ = chunk_set_ids
        return {}

    async def list_document_extraction_field_sets(self, document_id: str) -> list[Any]:
        # KB の項目の定義(#548)は rag_knowledge_bases を読み、文書の JSON 列は読まない。
        _ = document_id
        return []


@pytest.mark.parametrize(
    "call",
    [
        lambda: documents_route.list_document_ingestion_jobs("doc-1"),
        lambda: documents_route.list_document_ingestion_segments("doc-1"),
        lambda: documents_route.list_document_knowledge_bases("doc-1"),
    ],
    ids=["ingestion-jobs", "ingestion-segments", "knowledge-bases"],
)
async def test_polling_routes_do_not_read_document_json_columns(
    monkeypatch: pytest.MonkeyPatch,
    call: Any,
) -> None:
    monkeypatch.setattr(documents_route, "OracleClient", lambda: _LightOracle())

    result = await call()

    assert result.data is not None


async def test_chunk_sets_route_does_not_read_document_json_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _LightOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: oracle)

    async def resolve(*args: object, **kwargs: object) -> tuple[Settings, object]:
        _ = args, kwargs
        return Settings(), None

    monkeypatch.setattr(documents_route, "_resolve_ingestion_settings", resolve)

    result = await documents_route.list_document_chunk_sets("doc-1")

    assert result.data == []


@pytest.mark.parametrize(
    "call",
    [
        lambda: documents_route.list_document_ingestion_jobs("doc-missing"),
        lambda: documents_route.list_document_ingestion_segments("doc-missing"),
        lambda: documents_route.list_document_chunk_sets("doc-missing"),
    ],
)
async def test_polling_routes_return_404_for_missing_document(
    monkeypatch: pytest.MonkeyPatch,
    call: Any,
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(documents_route, "OracleClient", lambda: _LightOracle())

    with pytest.raises(HTTPException) as exc_info:
        await call()

    assert exc_info.value.status_code == 404
