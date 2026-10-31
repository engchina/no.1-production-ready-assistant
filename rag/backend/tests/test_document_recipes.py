"""1文書1〜3レシピの境界・工程状態・検索対象契約。"""

from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from rag_parser_core.language import builtin_pack

from app.api.routes import documents as documents_route
from app.api.routes.documents import (
    _apply_recipe_review_text_edits,
    _candidate_chunking_settings,
    _enqueue_failed_segment_retry_job_for_document,
    _mark_layers_rebuild_required,
    _materialize_experiment_candidate,
    _recipe_steps,
)
from app.clients.object_storage import ObjectStorageClient
from app.clients.oracle import (
    ReviewedExtractionWrite,
    oracle_chunk_set_schema_sql,
    oracle_document_recipe_schema_sql,
    oracle_ingestion_job_schema_sql,
)
from app.config import Settings, get_settings, resolve_ingestion_queue_concurrency
from app.rag.chunking_small_to_big import SMALL_TO_BIG_STRATEGY
from app.rag.extraction_field_adapter import (
    FIELD_SCHEMA_FILE_ENV,
    FieldDefinition,
    save_field_schema,
)
from app.rag.ingestion import IngestionCancelledError
from app.rag.layer_fingerprint import (
    FIELD_SCHEMA_HASH_ARTIFACT_KEY,
    chunk_metadata_contract_hash,
    field_schema_hash,
)
from app.rag.variant_keys import (
    compute_graph_layer_id,
    compute_metadata_layer_id,
    compute_nav_layer_id,
)
from app.schemas.document import (
    INGESTION_HEAVY_PHASES,
    DocumentChunkPreviewRequest,
    DocumentDetail,
    DocumentLayerStatusName,
    DocumentPreprocessArtifact,
    DocumentProcessingConfig,
    DocumentRecipeStepStatus,
    DocumentReviewEditsRequest,
    DocumentSummary,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
    IngestionSegment,
)
from app.schemas.extraction import StructuredExtraction
from tests.language_release_support import install_language_release_stub


def test_document_recipe_schema_enforces_one_slot_per_document_and_max_three() -> None:
    sql = oracle_document_recipe_schema_sql()
    assert "CHECK (slot_no BETWEEN 1 AND 3)" in sql
    assert "UNIQUE (document_id, slot_no)" in sql
    assert "config_revision" in sql and "NUMBER(10) DEFAULT 1 NOT NULL" in sql
    assert "materialized_revision" in sql


def test_chunk_set_schema_enforces_one_active_output_per_recipe() -> None:
    sql = oracle_chunk_set_schema_sql()
    assert "recipe_id       VARCHAR2(64)" in sql
    assert "is_active       NUMBER(1) DEFAULT 0 NOT NULL" in sql
    assert "CASE WHEN is_active = 1 THEN recipe_id END" in sql


def test_ingestion_job_schema_snapshots_recipe_revision() -> None:
    sql = oracle_ingestion_job_schema_sql()
    assert "recipe_id        VARCHAR2(64)" in sql
    assert "recipe_revision  NUMBER(10)" in sql
    assert "(recipe_id, status, queued_at DESC)" in sql


def test_recipe_steps_keep_failure_isolated_to_its_phase() -> None:
    now = datetime.now(UTC)
    failed = IngestionJob(
        id="job-1",
        document_id="doc-1",
        recipe_id="recipe-2",
        recipe_revision=3,
        status=IngestionJobStatus.FAILED,
        phase=IngestionJobPhase.EXTRACT,
        parser_profile="docling",
        queued_at=now,
        error_message="抽出に失敗しました。",
    )
    steps = _recipe_steps(
        {
            "status": FileStatus.ERROR.value,
            "failed_phase": IngestionJobPhase.EXTRACT.value,
        },
        [failed],
    )
    by_phase = {step.phase: step for step in steps}
    assert by_phase[IngestionJobPhase.EXTRACT].status == DocumentRecipeStepStatus.FAILED
    assert by_phase[IngestionJobPhase.EXTRACT].error_message == "抽出に失敗しました。"
    assert by_phase[IngestionJobPhase.CHUNK].status == DocumentRecipeStepStatus.PENDING
    assert by_phase[IngestionJobPhase.INDEX].status == DocumentRecipeStepStatus.PENDING


def test_indexed_recipe_reports_all_four_steps_succeeded_without_jobs() -> None:
    steps = _recipe_steps({"status": FileStatus.INDEXED.value}, [])
    assert [step.status for step in steps] == [DocumentRecipeStepStatus.SUCCEEDED] * 4


def _recipe_job(
    phase: IngestionJobPhase,
    status: IngestionJobStatus,
    *,
    job_id: str = "job-1",
    queued_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_message: str | None = None,
) -> IngestionJob:
    return IngestionJob(
        id=job_id,
        document_id="doc-1",
        recipe_id="recipe-1",
        recipe_revision=1,
        status=status,
        phase=phase,
        parser_profile="docling",
        queued_at=queued_at or datetime.now(UTC),
        finished_at=finished_at,
        error_message=error_message,
    )


_P = DocumentRecipeStepStatus.PENDING
_R = DocumentRecipeStepStatus.RUNNING
_S = DocumentRecipeStepStatus.SUCCEEDED
_NR = DocumentRecipeStepStatus.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (FileStatus.UPLOADED, [_P, _P, _P, _P]),
        (FileStatus.PREPROCESSING, [_R, _P, _P, _P]),
        (FileStatus.PREPROCESSED, [_S, _P, _P, _P]),
        (FileStatus.INGESTING, [_S, _R, _P, _P]),
        (FileStatus.REVIEW, [_S, _NR, _P, _P]),
        (FileStatus.CHUNKING, [_S, _S, _R, _P]),
        (FileStatus.CHUNKED, [_S, _S, _NR, _P]),
        (FileStatus.INDEXING, [_S, _S, _S, _R]),
        (FileStatus.INDEXED, [_S, _S, _S, _S]),
    ],
)
def test_recipe_steps_follow_recipe_status_matrix(
    status: FileStatus, expected: list[DocumentRecipeStepStatus]
) -> None:
    """レシピ行 status が4工程表示の単一状態源。"""
    steps = _recipe_steps({"status": status.value}, [])
    assert [step.status for step in steps] == expected


def test_recipe_steps_show_single_running_step_during_full_run() -> None:
    """通しジョブ(phase=PREPROCESS)が抽出まで進んでも処理中表示は1工程だけ。"""
    running = _recipe_job(IngestionJobPhase.PREPROCESS, IngestionJobStatus.RUNNING)
    steps = _recipe_steps({"status": FileStatus.INGESTING.value}, [running])
    assert [step.status for step in steps] == [_S, _R, _P, _P]


@pytest.mark.parametrize(
    ("auto_chunk", "finished_ago", "expected"),
    [
        # 自動進行の猶予の間は、抽出を完了・Chunk 作成を順番待ちにする(確認待ちにしない。#2026)。
        (True, timedelta(seconds=1), [_S, _S, DocumentRecipeStepStatus.QUEUED, _P]),
        (False, timedelta(seconds=1), [_S, _NR, _P, _P]),
        (True, timedelta(minutes=5), [_S, _NR, _P, _P]),
    ],
)
def test_recipe_steps_queue_chunk_during_auto_advance_handoff(
    auto_chunk: bool, finished_ago: timedelta, expected: list[DocumentRecipeStepStatus]
) -> None:
    """抽出の job の SUCCEEDED から CHUNK の job の投入までも Chunk 作成を QUEUED にする(#733)。"""
    finished = _recipe_job(
        IngestionJobPhase.PREPROCESS,
        IngestionJobStatus.SUCCEEDED,
        finished_at=datetime.now(UTC) - finished_ago,
    )
    steps = _recipe_steps(
        {"status": FileStatus.REVIEW.value}, [finished], auto_chunk_after_extract=auto_chunk
    )
    assert [step.status for step in steps] == expected
    queued = [step for step in steps if step.status == DocumentRecipeStepStatus.QUEUED]
    if queued:
        # 投入前の猶予の間は、待っている job が無いため前の件数を出さない。
        [step] = queued
        assert step.queue_wait is not None
        assert step.queue_wait.phase == IngestionJobPhase.CHUNK
        assert step.queue_wait.job_id is None
        assert step.queue_wait.jobs_ahead is None


def test_recipe_steps_attribute_full_run_failure_to_failed_phase() -> None:
    """通しジョブの失敗は failed_phase の工程に出し、メッセージも引き継ぐ。"""
    failed = _recipe_job(
        IngestionJobPhase.PREPROCESS,
        IngestionJobStatus.FAILED,
        error_message="選択した文書解析サービス(MinerU)に接続できません。",
    )
    steps = _recipe_steps(
        {
            "status": FileStatus.ERROR.value,
            "failed_phase": IngestionJobPhase.EXTRACT.value,
        },
        [failed],
    )
    assert [step.status for step in steps] == [_S, DocumentRecipeStepStatus.FAILED, _P, _P]
    assert steps[1].error_message == "選択した文書解析サービス(MinerU)に接続できません。"
    assert steps[0].error_message is None


def test_recipe_steps_ignore_stale_jobs_from_previous_run() -> None:
    """再処理中は前回実行のジョブ行(後続工程の SUCCEEDED 等)を表示しない。"""
    now = datetime.now(UTC)
    old = now - timedelta(hours=1)
    jobs = [
        _recipe_job(
            IngestionJobPhase.PREPROCESS,
            IngestionJobStatus.RUNNING,
            job_id="job-new",
            queued_at=now,
        ),
        _recipe_job(
            IngestionJobPhase.EXTRACT,
            IngestionJobStatus.SUCCEEDED,
            job_id="job-old-1",
            queued_at=old,
        ),
        _recipe_job(
            IngestionJobPhase.CHUNK,
            IngestionJobStatus.SUCCEEDED,
            job_id="job-old-2",
            queued_at=old,
        ),
        _recipe_job(
            IngestionJobPhase.INDEX,
            IngestionJobStatus.FAILED,
            job_id="job-old-3",
            queued_at=old,
        ),
    ]
    steps = _recipe_steps({"status": FileStatus.PREPROCESSING.value}, jobs)
    assert [step.status for step in steps] == [_R, _P, _P, _P]


def test_recipe_steps_show_queued_overlay_for_newest_job() -> None:
    """失敗した工程から再試行を投入した直後(claim 前)はその工程を QUEUED 表示する。"""
    now = datetime.now(UTC)
    jobs = [
        _recipe_job(
            IngestionJobPhase.EXTRACT,
            IngestionJobStatus.QUEUED,
            job_id="job-retry",
            queued_at=now,
        ),
        _recipe_job(
            IngestionJobPhase.PREPROCESS,
            IngestionJobStatus.FAILED,
            job_id="job-old",
            queued_at=now - timedelta(minutes=5),
        ),
    ]
    steps = _recipe_steps(
        {
            "status": FileStatus.ERROR.value,
            "failed_phase": IngestionJobPhase.EXTRACT.value,
        },
        jobs,
    )
    assert [step.status for step in steps] == [_S, DocumentRecipeStepStatus.QUEUED, _P, _P]


@pytest.mark.parametrize(
    ("failed_phase", "queued_phase", "expected"),
    [
        # 失敗した工程そのものを再実行する。
        (
            IngestionJobPhase.INDEX,
            IngestionJobPhase.INDEX,
            [_S, _S, _S, DocumentRecipeStepStatus.QUEUED],
        ),
        # 失敗した工程より前から再処理する。後ろの工程の前回の失敗は残さない(#2687)。
        (
            IngestionJobPhase.INDEX,
            IngestionJobPhase.CHUNK,
            [_S, _S, DocumentRecipeStepStatus.QUEUED, _P],
        ),
        (
            IngestionJobPhase.CHUNK,
            IngestionJobPhase.PREPROCESS,
            [DocumentRecipeStepStatus.QUEUED, _S, _P, _P],
        ),
    ],
)
def test_recipe_steps_hide_previous_failure_while_reprocess_is_queued(
    failed_phase: IngestionJobPhase,
    queued_phase: IngestionJobPhase,
    expected: list[DocumentRecipeStepStatus],
) -> None:
    """再処理の job の claim 前は、レシピ行が ERROR でも前回の FAILED の工程を出さない(#2687)。"""
    now = datetime.now(UTC)
    jobs = [
        _recipe_job(queued_phase, IngestionJobStatus.QUEUED, job_id="job-new", queued_at=now),
        _recipe_job(
            failed_phase,
            IngestionJobStatus.FAILED,
            job_id="job-old",
            queued_at=now - timedelta(minutes=5),
            error_message="前回の失敗の本文",
        ),
    ]
    steps = _recipe_steps(
        {"status": FileStatus.ERROR.value, "failed_phase": failed_phase.value},
        jobs,
    )
    assert [step.status for step in steps] == expected
    assert all(step.error_message is None for step in steps)


def test_recipe_steps_show_new_failure_after_reprocess_fails() -> None:
    """再処理の job が失敗すれば、新しい失敗の工程と本文を出す(#2687)。"""
    now = datetime.now(UTC)
    jobs = [
        _recipe_job(
            IngestionJobPhase.CHUNK,
            IngestionJobStatus.FAILED,
            job_id="job-new",
            queued_at=now,
            error_message="新しい失敗の本文",
        ),
        _recipe_job(
            IngestionJobPhase.INDEX,
            IngestionJobStatus.FAILED,
            job_id="job-old",
            queued_at=now - timedelta(minutes=5),
            error_message="前回の失敗の本文",
        ),
    ]
    steps = _recipe_steps(
        {"status": FileStatus.ERROR.value, "failed_phase": IngestionJobPhase.CHUNK.value},
        jobs,
    )
    assert [step.status for step in steps] == [_S, _S, DocumentRecipeStepStatus.FAILED, _P]
    assert steps[2].error_message == "新しい失敗の本文"


_Q = DocumentRecipeStepStatus.QUEUED


@pytest.mark.parametrize(
    ("recipe_status", "queued_phase", "expected"),
    [
        # 自動進行(または利用者の続行)で Chunk 作成の job が worker の空きを待っている。
        (FileStatus.REVIEW, IngestionJobPhase.CHUNK, [_S, _S, _Q, _P]),
        # Chunk の確認の後に索引の job が待っている。
        (FileStatus.CHUNKED, IngestionJobPhase.INDEX, [_S, _S, _S, _Q]),
        # ファイル準備の確認の後に抽出の job が待っている。
        (FileStatus.PREPROCESSED, IngestionJobPhase.EXTRACT, [_S, _Q, _P, _P]),
        # 投入直後(claim 前)は文書の状態が前のまま。
        (FileStatus.UPLOADED, IngestionJobPhase.PREPROCESS, [_Q, _P, _P, _P]),
    ],
)
def test_recipe_steps_show_queue_wait_instead_of_review(
    recipe_status: FileStatus,
    queued_phase: IngestionJobPhase,
    expected: list[DocumentRecipeStepStatus],
) -> None:
    """次の工程の job が QUEUED なら、前の工程は確認待ちでなく完了、待つ工程は順番待ち(#2026)。"""
    queued_at = datetime.now(UTC) - timedelta(seconds=90)
    queued = _recipe_job(
        queued_phase, IngestionJobStatus.QUEUED, job_id="job-q", queued_at=queued_at
    )
    done = _recipe_job(
        IngestionJobPhase.PREPROCESS,
        IngestionJobStatus.SUCCEEDED,
        job_id="job-done",
        queued_at=queued_at - timedelta(minutes=5),
    )

    steps = _recipe_steps(
        {"status": recipe_status.value},
        [queued, done],
        auto_chunk_after_extract=True,
        queue_positions={"job-q": 3},
        queue_concurrency=resolve_ingestion_queue_concurrency(4, 2, vcpu=8),
    )

    assert [step.status for step in steps] == expected
    waits = [step.queue_wait for step in steps if step.queue_wait is not None]
    assert len(waits) == 1
    [wait] = waits
    assert wait.phase == queued_phase
    assert wait.job_id == "job-q"
    assert wait.queued_at == queued_at
    assert wait.jobs_ahead == 3
    # 待っている段の枠(重い段は重い段の上限、軽い段は全体の枠。#2027)。
    assert wait.concurrency == (2 if queued_phase in INGESTION_HEAVY_PHASES else 4)


def test_recipe_steps_queue_wait_without_position() -> None:
    """順番を読めなかったとき(queue_positions に無い)は前の件数を出さない。"""
    queued = _recipe_job(IngestionJobPhase.CHUNK, IngestionJobStatus.QUEUED, job_id="job-q")

    steps = _recipe_steps({"status": FileStatus.REVIEW.value}, [queued], queue_positions={})

    assert steps[2].queue_wait is not None
    assert steps[2].queue_wait.jobs_ahead is None
    assert steps[2].queue_wait.concurrency == 1


def test_recipe_steps_keep_review_when_nothing_is_queued() -> None:
    """自動進行が無効で確認が要るとき(待っている job が無い)は、確認待ちのまま。"""
    finished = _recipe_job(
        IngestionJobPhase.PREPROCESS,
        IngestionJobStatus.SUCCEEDED,
        finished_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    steps = _recipe_steps({"status": FileStatus.REVIEW.value}, [finished], queue_positions={})

    assert [step.status for step in steps] == [_S, _NR, _P, _P]
    assert all(step.queue_wait is None for step in steps)


class _RecipeStatusOracle:
    """_mark_recipe_job_failed 用: 現在 status を返し、更新引数を記録する。"""

    def __init__(self, status: FileStatus) -> None:
        self._status = status
        self.recorded: dict[str, object] = {}

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
        return {"document_id": document_id, "recipe_id": recipe_id, "status": self._status.value}

    async def update_document_recipe_status(self, **kwargs: object) -> None:
        self.recorded = kwargs


@pytest.mark.parametrize(
    ("job_phase", "recipe_status", "expected"),
    [
        # 通しジョブが抽出まで進んで失敗 → EXTRACT に帰属(MinerU 接続失敗など)。
        (IngestionJobPhase.PREPROCESS, FileStatus.INGESTING, IngestionJobPhase.EXTRACT),
        # 前処理中の失敗はそのまま。
        (IngestionJobPhase.PREPROCESS, FileStatus.PREPROCESSING, IngestionJobPhase.PREPROCESS),
        # CHUNK ジョブが索引まで進んで失敗 → INDEX に帰属。
        (IngestionJobPhase.CHUNK, FileStatus.INDEXING, IngestionJobPhase.INDEX),
        # 非実行 status(ゲート停止等)は job.phase へフォールバック。
        (IngestionJobPhase.CHUNK, FileStatus.REVIEW, IngestionJobPhase.CHUNK),
        # ジョブ開始工程より前へは戻さない。
        (IngestionJobPhase.CHUNK, FileStatus.PREPROCESSING, IngestionJobPhase.CHUNK),
    ],
)
async def test_mark_recipe_job_failed_attributes_actual_stage(
    job_phase: IngestionJobPhase,
    recipe_status: FileStatus,
    expected: IngestionJobPhase,
) -> None:
    """失敗工程はレシピ行の現在 status(工程ごとに更新)から導出する。"""
    oracle = _RecipeStatusOracle(recipe_status)
    job = _recipe_job(job_phase, IngestionJobStatus.RUNNING)
    await documents_route._mark_recipe_job_failed(oracle, job, "boom")  # type: ignore[arg-type]
    assert oracle.recorded["status"] == FileStatus.ERROR
    assert oracle.recorded["failed_phase"] == expected
    assert oracle.recorded["error_message"] == "boom"


async def test_recipe_segment_retry_creates_extract_job_for_same_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recipe 指定 retry は同 recipe の FAILED segment と revision を使う。"""

    class FakeOracle:
        created: IngestionJob | None = None

        async def get_document(self, document_id: str) -> DocumentDetail:
            return DocumentDetail(
                id=document_id,
                file_name="policy.pdf",
                status=FileStatus.ERROR,
                object_storage_path="local://policy.pdf",
                content_sha256="a" * 64,
                uploaded_at=datetime.now(UTC),
            )

        async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
            return {
                "document_id": document_id,
                "recipe_id": recipe_id,
                "config_revision": 7,
                "processing_config": {},
                "preprocess_artifact": {
                    "derivation_id": "prepared-1",
                    "profile": "passthrough",
                    "file_name": "policy.pdf",
                    "object_storage_path": "local://prepared/policy.pdf",
                },
            }

        async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
            return [
                IngestionSegment(
                    segment_id=f"{document_id}:recipe-2:p1-2",
                    document_id=document_id,
                    recipe_id="recipe-2",
                    status="FAILED",
                    parser_backend="enterprise_ai",
                    parser_profile="enterprise_ai_pdf_layout",
                ),
                IngestionSegment(
                    segment_id=f"{document_id}:recipe-1:p3-4",
                    document_id=document_id,
                    recipe_id="recipe-1",
                    status="FAILED",
                    parser_backend="enterprise_ai",
                    parser_profile="enterprise_ai_pdf_layout",
                ),
            ]

        async def create_ingestion_job(self, job: IngestionJob) -> IngestionJob:
            self.created = job
            return job

    fake = FakeOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda _job_id: None)

    job = await _enqueue_failed_segment_retry_job_for_document("doc-1", recipe_id="recipe-1")

    assert job.recipe_id == "recipe-1"
    assert job.recipe_revision == 7
    assert job.phase == IngestionJobPhase.EXTRACT
    assert fake.created == job


async def test_recipe_segment_retry_ignores_other_recipe_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 recipe の FAILED segment だけでは再試行を受け付けない。"""

    class FakeOracle:
        async def get_document(self, document_id: str) -> DocumentDetail:
            return DocumentDetail(
                id=document_id,
                file_name="policy.pdf",
                status=FileStatus.ERROR,
                object_storage_path="local://policy.pdf",
                uploaded_at=datetime.now(UTC),
            )

        async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
            return {
                "document_id": document_id,
                "recipe_id": recipe_id,
                "preprocess_artifact": {
                    "derivation_id": "prepared-1",
                    "profile": "passthrough",
                    "file_name": "policy.pdf",
                    "object_storage_path": "local://prepared/policy.pdf",
                },
            }

        async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
            return [
                IngestionSegment(
                    segment_id=f"{document_id}:recipe-2:p1-2",
                    document_id=document_id,
                    recipe_id="recipe-2",
                    status="FAILED",
                    parser_backend="enterprise_ai",
                    parser_profile="enterprise_ai_pdf_layout",
                )
            ]

    monkeypatch.setattr(documents_route, "OracleClient", FakeOracle)

    with pytest.raises(HTTPException) as exc_info:
        await _enqueue_failed_segment_retry_job_for_document("doc-1", recipe_id="recipe-1")

    assert exc_info.value.status_code == 409


async def test_recipe_review_edit_copies_shared_extraction_before_pointer_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存共有 artifact は変更せず recipe 固有 ID へ copy-on-write する。"""

    class FakeOracle:
        write: ReviewedExtractionWrite | None = None

        async def get_document(self, document_id: str) -> DocumentDetail:
            return DocumentDetail(
                id=document_id,
                file_name="policy.pdf",
                status=FileStatus.REVIEW,
                content_sha256="a" * 64,
                uploaded_at=datetime.now(UTC),
            )

        async def save_document_recipe_review_extraction(
            self,
            *,
            document_id: str,
            recipe_id: str,
            build: Callable[[Mapping[str, object], Mapping[str, object]], ReviewedExtractionWrite],
        ) -> str:
            recipe: dict[str, object] = {
                "document_id": document_id,
                "recipe_id": recipe_id,
                "status": "REVIEW",
                "config_revision": 3,
                "processing_config": {},
                "active_extraction_recipe_id": "er_shared",
            }
            artifact: dict[str, object] = {
                "extraction_json": StructuredExtraction(raw_text="共有本文").to_document_payload(),
                "recipe_subset": {},
                "status": "materialized",
            }
            self.write = build(recipe, artifact)
            return self.write.extraction_recipe_id

    fake = FakeOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)

    await _apply_recipe_review_text_edits("doc-1", "recipe-1", DocumentReviewEditsRequest())

    assert fake.write is not None
    assert fake.write.extraction_recipe_id != "er_shared"
    assert fake.write.source_sha256 == "a" * 64


async def test_list_document_recipes_fetches_jobs_once_for_all_recipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数レシピでも取込 job 一覧は1回だけ取得する(N+1 回避)。"""

    def _row(recipe_id: str) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "document_id": "doc-1",
            "recipe_id": recipe_id,
            "slot_no": 1,
            "status": FileStatus.INDEXED.value,
            "processing_config": {},
            "config_revision": 1,
            "created_at": now,
            "updated_at": now,
        }

    class FakeOracle:
        job_list_calls = 0

        async def list_document_recipes(self, document_id: str) -> list[dict[str, object]]:
            return [_row("recipe-1"), _row("recipe-2"), _row("recipe-3")]

        async def list_document_ingestion_jobs(
            self, document_id: str, *, status: IngestionJobStatus | None = None
        ) -> list[IngestionJob]:
            self.job_list_calls += 1
            return []

    fake = FakeOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)

    result = await documents_route.list_document_recipes("doc-1")

    assert result.data is not None
    assert len(result.data) == 3
    assert fake.job_list_calls == 1


async def test_document_recipe_view_exposes_pending_chunk_set_after_index_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding / 索引で失敗したレシピは、保存済みの CHUNKED の chunk_set を画面へ返す(#2467)。

    画面はこれで「Embedding / 索引を再実行」(保存済みの Chunk から再開)を主の操作にする。
    """
    now = datetime.now(UTC)

    class FakeOracle:
        async def list_document_recipes(self, document_id: str) -> list[dict[str, object]]:
            return [
                {
                    "document_id": document_id,
                    "recipe_id": "recipe-1",
                    "slot_no": 1,
                    "status": FileStatus.ERROR.value,
                    "failed_phase": IngestionJobPhase.INDEX.value,
                    "active_extraction_recipe_id": "er-1",
                    "active_chunk_set_id": None,
                    "pending_chunk_set_id": "cs-pending",
                    "processing_config": {},
                    "config_revision": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            ]

        async def list_document_ingestion_jobs(
            self, document_id: str, *, status: IngestionJobStatus | None = None
        ) -> list[IngestionJob]:
            return []

    monkeypatch.setattr(documents_route, "OracleClient", lambda: FakeOracle())

    result = await documents_route.list_document_recipes("doc-1")

    assert result.data is not None
    [recipe] = result.data
    assert recipe.active_chunk_set_id is None
    assert recipe.pending_chunk_set_id == "cs-pending"
    assert recipe.failed_phase == IngestionJobPhase.INDEX


@pytest.fixture(autouse=True)
def _language_release_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    install_language_release_stub(monkeypatch)


class _FakeRecipeJobOracle:
    """EXTRACT job の materialize と job ライフサイクルを支える fake。

    ``create_ingestion_job`` に実 Oracle と同じレシピ行ロックのガード(同一レシピに
    QUEUED/RUNNING が居れば拒否)を再現し、自動進行の投入タイミングを検証できるようにする。
    """

    def __init__(self) -> None:
        self.recipe_status = FileStatus.INGESTING
        self.jobs: dict[str, IngestionJob] = {}
        self.config_revision = 1

    async def pin_ingestion_job_language_pack(self, job_id: str, release_id: str) -> str:
        item = self.jobs[job_id]
        pinned = item.language_pack_release_id or release_id
        self.jobs[job_id] = item.model_copy(update={"language_pack_release_id": pinned})
        return pinned

    async def get_document(self, document_id: str) -> DocumentDetail:
        return DocumentDetail(
            id=document_id,
            file_name="policy.pdf",
            status=FileStatus.INGESTING,
            content_sha256="a" * 64,
            content_type="application/pdf",
            object_storage_path="local://policy.pdf",
            uploaded_at=datetime.now(UTC),
        )

    async def get_document_serving_chunk_set_id(self, document_id: str) -> str | None:
        return None

    async def get_document_processing_config(self, document_id: str) -> DocumentProcessingConfig:
        return DocumentProcessingConfig()

    async def update_document_recipe_status(
        self, *, recipe_id: str, status: FileStatus, **_kwargs: object
    ) -> None:
        self.recipe_status = status

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
        return {
            "document_id": document_id,
            "recipe_id": recipe_id,
            "status": self.recipe_status.value,
            "config_revision": self.config_revision,
            "processing_config": {},
            "preprocess_artifact": DocumentPreprocessArtifact(
                derivation_id="prepared-1",
                profile="passthrough",
                file_name="policy.pdf",
                object_storage_path="local://prepared/policy.pdf",
                content_type="application/pdf",
            ).model_dump(mode="json"),
        }

    async def claim_ingestion_job(
        self, job_id: str, *, started_at: datetime, lease_owner: str | None = None
    ) -> IngestionJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.status != IngestionJobStatus.QUEUED:
            return None
        claimed = job.model_copy(
            update={
                "status": IngestionJobStatus.RUNNING,
                "attempt_count": job.attempt_count + 1,
                "started_at": started_at,
            }
        )
        self.jobs[job_id] = claimed
        return claimed

    async def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        return self.jobs.get(job_id)

    async def create_ingestion_job(self, job: IngestionJob) -> IngestionJob:
        if job.recipe_id is not None and any(
            existing.recipe_id == job.recipe_id
            and existing.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING}
            for existing in self.jobs.values()
        ):
            raise ValueError("このレシピは処理中または待機中です。")
        self.jobs[job.id] = job
        return job

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
        job = self.jobs.get(job_id)
        if job is None or job.status not in from_statuses:
            return None
        updated = job.model_copy(
            update={
                "status": to_status,
                "error_message": error_message,
                "finished_at": finished_at,
            }
        )
        self.jobs[job_id] = updated
        return updated

    async def list_document_ingestion_jobs(
        self, document_id: str, *, status: IngestionJobStatus | None = None
    ) -> list[IngestionJob]:
        return [
            job
            for job in self.jobs.values()
            if job.document_id == document_id and (status is None or job.status == status)
        ]


class _FakeRecipeJobPipeline:
    """extraction を行わず、ingest 実行でレシピを REVIEW へ遷移させる fake pipeline。"""

    def __init__(self, *, oracle: _FakeRecipeJobOracle, **_kwargs: object) -> None:
        self._oracle = oracle

    async def ingest(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self._oracle.recipe_status = FileStatus.REVIEW


def _extract_job(job_id: str = "job-extract-1") -> IngestionJob:
    return IngestionJob(
        id=job_id,
        document_id="doc-1",
        recipe_id="recipe-1",
        recipe_revision=1,
        status=IngestionJobStatus.QUEUED,
        phase=IngestionJobPhase.EXTRACT,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        settings_overrides={"processing_config": {}},
    )


async def _decide_next_phase(
    monkeypatch: pytest.MonkeyPatch, *, auto_chunk_enabled: bool
) -> IngestionJobPhase | None:
    """EXTRACT 完了直後の materialize が返す「次フェーズ」の決定だけを取り出す。"""
    fake = _FakeRecipeJobOracle()
    await ObjectStorageClient().put("prepared/policy.pdf", b"prepared pdf bytes", "application/pdf")
    monkeypatch.setattr(documents_route, "IngestionPipeline", _FakeRecipeJobPipeline)
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=auto_chunk_enabled),
    )
    result = await _materialize_experiment_candidate(fake, _extract_job())  # type: ignore[arg-type]
    # 索引まで進まない EXTRACT は、記録する成功の監査を持たない。
    assert result.success_outcome is None
    return result.next_phase


async def test_recipe_extract_returns_chunk_phase_when_auto_chunk_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXTRACT 完了で REVIEW かつ auto_chunk 有効なら次フェーズ CHUNK を返す(決定のみ)。"""
    next_phase = await _decide_next_phase(monkeypatch, auto_chunk_enabled=True)
    assert next_phase == IngestionJobPhase.CHUNK


async def test_recipe_extract_returns_none_when_auto_chunk_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto_chunk 無効時は次フェーズを返さず REVIEW に留める。"""
    next_phase = await _decide_next_phase(monkeypatch, auto_chunk_enabled=False)
    assert next_phase is None


async def test_recipe_extract_job_enqueues_chunk_after_current_job_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto_chunk 有効時、EXTRACT job 完了(SUCCEEDED)後に同一レシピの CHUNK job を投入する。

    投入が現在ジョブ RUNNING 中に走るとレシピ行ロックのガードで弾かれるため、この統合的な
    経路テストが本来の不具合(自動進行が「このレシピは処理中または待機中です」で失敗)を捕捉する。
    """
    fake = _FakeRecipeJobOracle()
    fake.jobs["job-extract-1"] = _extract_job()
    await ObjectStorageClient().put("prepared/policy.pdf", b"prepared pdf bytes", "application/pdf")
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "IngestionPipeline", _FakeRecipeJobPipeline)
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=True),
    )
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *a, **k: None)

    await documents_route._run_ingestion_job("job-extract-1", propagate_errors=True)

    # 現在の EXTRACT job は完了済み。
    assert fake.jobs["job-extract-1"].status == IngestionJobStatus.SUCCEEDED
    # 完了後にガードを通過して CHUNK job が 1 件だけ投入される。
    chunk_jobs = [
        job
        for job in fake.jobs.values()
        if job.phase == IngestionJobPhase.CHUNK and job.recipe_id == "recipe-1"
    ]
    assert len(chunk_jobs) == 1
    assert chunk_jobs[0].status == IngestionJobStatus.QUEUED


async def test_recipe_extract_job_stays_in_review_when_auto_chunk_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto_chunk 無効時は EXTRACT job 完了後も CHUNK job を投入しない。"""
    fake = _FakeRecipeJobOracle()
    fake.jobs["job-extract-1"] = _extract_job()
    await ObjectStorageClient().put("prepared/policy.pdf", b"prepared pdf bytes", "application/pdf")
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "IngestionPipeline", _FakeRecipeJobPipeline)
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=False),
    )
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *a, **k: None)

    await documents_route._run_ingestion_job("job-extract-1", propagate_errors=True)

    assert fake.jobs["job-extract-1"].status == IngestionJobStatus.SUCCEEDED
    assert fake.recipe_status == FileStatus.REVIEW
    chunk_jobs = [job for job in fake.jobs.values() if job.phase == IngestionJobPhase.CHUNK]
    assert chunk_jobs == []


def _cancel_recipe_job(fake: _FakeRecipeJobOracle, job_id: str) -> None:
    job = fake.jobs[job_id]
    fake.jobs[job_id] = job.model_copy(
        update={
            "status": IngestionJobStatus.CANCELLED,
            "error_message": documents_route.INGESTION_JOB_CANCELLED_MESSAGE,
            "finished_at": datetime.now(UTC),
        }
    )


async def test_recipe_job_cancelled_during_finish_does_not_enqueue_next_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完了処理が RUNNING を読んだ直後に cancel されたら、SUCCEEDED にせず次工程も入れない。"""
    fake = _FakeRecipeJobOracle()
    fake.jobs["job-extract-1"] = _extract_job()
    await ObjectStorageClient().put("prepared/policy.pdf", b"prepared pdf bytes", "application/pdf")
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "IngestionPipeline", _FakeRecipeJobPipeline)
    monkeypatch.setattr(
        documents_route,
        "get_settings",
        lambda: Settings(rag_auto_chunk_after_extract_enabled=True),
    )
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *a, **k: None)
    armed = {"on": False}
    original_get = fake.get_ingestion_job
    original_transition = fake.transition_ingestion_job
    original_ingest = _FakeRecipeJobPipeline.ingest

    def fire(job_id: str) -> None:
        if armed["on"]:
            armed["on"] = False
            _cancel_recipe_job(fake, job_id)

    async def get_then_cancel(job_id: str) -> IngestionJob | None:
        current = await original_get(job_id)
        fire(job_id)
        return current

    async def cancel_then_transition(job_id: str, **kwargs: Any) -> IngestionJob | None:
        fire(job_id)
        return await original_transition(job_id, **kwargs)

    async def ingest_then_arm(
        self: _FakeRecipeJobPipeline, *args: object, **kwargs: object
    ) -> None:
        await original_ingest(self, *args, **kwargs)
        armed["on"] = True

    monkeypatch.setattr(fake, "get_ingestion_job", get_then_cancel)
    monkeypatch.setattr(fake, "transition_ingestion_job", cancel_then_transition)
    monkeypatch.setattr(_FakeRecipeJobPipeline, "ingest", ingest_then_arm)

    await documents_route._run_ingestion_job("job-extract-1")

    assert fake.jobs["job-extract-1"].status == IngestionJobStatus.CANCELLED
    assert [job for job in fake.jobs.values() if job.phase == IngestionJobPhase.CHUNK] == []
    # 取り消しを検知した worker がレシピを EXTRACT の前の状態(ファイル準備済み)へ戻す。
    assert fake.recipe_status == FileStatus.PREPROCESSED


class _FakeRecipeIndexPipeline:
    """INDEX 工程の fake。索引の途中で cancel が割り込んだ状態を作る。"""

    cancel_during_index: dict[str, bool] = {"on": False}

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def index_chunked(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        _FakeRecipeIndexPipeline.cancel_during_index["on"] = True


async def test_recipe_materialize_checks_cancel_before_activate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引後に cancel されていたら、新しい chunk_set を active にしない(#305)。"""

    class IndexOracle(_FakeRecipeJobOracle):
        def __init__(self) -> None:
            super().__init__()
            self.activated: list[str] = []

        async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
            row = await super().get_document_recipe(document_id, recipe_id)
            return {**row, "active_extraction_recipe_id": "extraction-1"}

        async def get_latest_recipe_chunk_set(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            _ = args, kwargs
            return {
                "chunk_set_id": "chunk-set-pending",
                "language_pack_ingest_hash": builtin_pack().ingest_hash,
                "language_pack_release_id": "synthetic-release",
            }

        async def count_chunk_set_chunks(self, chunk_set_id: str) -> int:
            _ = chunk_set_id
            return 3

        async def upsert_chunk_set(self, **kwargs: object) -> None:
            _ = kwargs

        async def mark_chunk_set_indexed(self, **kwargs: object) -> None:
            _ = kwargs

        async def activate_recipe_chunk_set(self, *, chunk_set_id: str, **kwargs: object) -> None:
            _ = kwargs
            self.activated.append(chunk_set_id)

        async def update_document_status(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("取り消した job で文書の status を INDEXED にしない")

    fake = IndexOracle()
    _FakeRecipeIndexPipeline.cancel_during_index["on"] = False
    monkeypatch.setattr(documents_route, "IngestionPipeline", _FakeRecipeIndexPipeline)
    job = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )

    async def cancel_checker() -> bool:
        return _FakeRecipeIndexPipeline.cancel_during_index["on"]

    with pytest.raises(IngestionCancelledError):
        await _materialize_experiment_candidate(
            fake,  # type: ignore[arg-type]
            job,
            cancel_checker=cancel_checker,
        )

    assert fake.activated == []


@pytest.mark.parametrize(
    ("phase", "active_chunk_set_id", "expected"),
    [
        (IngestionJobPhase.PREPROCESS, None, FileStatus.UPLOADED),
        (IngestionJobPhase.EXTRACT, None, FileStatus.PREPROCESSED),
        (IngestionJobPhase.CHUNK, None, FileStatus.REVIEW),
        (IngestionJobPhase.INDEX, None, FileStatus.CHUNKED),
        # 旧 active の出力があれば検索対象のまま。
        (IngestionJobPhase.CHUNK, "chunk-set-old", FileStatus.INDEXED),
    ],
)
async def test_restore_recipe_status_after_cancel_returns_to_state_before_phase(
    phase: IngestionJobPhase,
    active_chunk_set_id: str | None,
    expected: FileStatus,
) -> None:
    """取り消し後のレシピは、旧 active があれば INDEXED、無ければ工程を始める前の状態へ戻す。"""
    recorded: dict[str, object] = {}

    class RestoreOracle:
        async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
            return {
                "document_id": document_id,
                "recipe_id": recipe_id,
                "active_chunk_set_id": active_chunk_set_id,
            }

        async def update_document_recipe_status(self, **kwargs: object) -> None:
            recorded.update(kwargs)

    job = _recipe_job(phase, IngestionJobStatus.CANCELLED)
    await documents_route._restore_recipe_status_after_cancel(
        RestoreOracle(),  # type: ignore[arg-type]
        job,
    )

    assert recorded["recipe_id"] == "recipe-1"
    assert recorded["status"] == expected


def test_chunk_preview_settings_reject_overlap_not_smaller_than_size() -> None:
    """分割プレビューの一時上書きも overlap < chunk_size の相互制約を検証する(422)。"""
    request = DocumentChunkPreviewRequest(chunk_size=300, chunk_overlap=300)
    with pytest.raises(HTTPException) as exc:
        _candidate_chunking_settings(get_settings(), request.settings_overrides())
    assert exc.value.status_code == 422


async def test_legacy_experiment_job_without_recipe_is_rejected() -> None:
    """移行前の旧実験 API の job（recipe_id なし）は、利用者向けのエラーで止める（#486）。"""
    from types import SimpleNamespace
    from typing import Any, cast

    from app.api.routes import documents as documents_route
    from app.rag.ingestion import IngestionUserError

    legacy_job = SimpleNamespace(
        recipe_id=None,
        document_id="doc-legacy",
        settings_overrides={"rag_parser_adapter_backend": "docling"},
    )
    with pytest.raises(IngestionUserError, match="処理レシピから再実行"):
        await documents_route._materialize_experiment_candidate(
            cast(Any, object()), cast(Any, legacy_job)
        )


_NAVIGATION_EXTRACTION = StructuredExtraction(
    raw_text=(
        "# 第1章 概要\n\n"
        "社内規程の概要を説明します。\n\n"
        "## 1.1 経費申請\n\n"
        "部門長の承認後、経理部が確認します。\n"
    ),
).to_document_payload()


class _CompletingRecipeOracle(_FakeRecipeJobOracle):
    """レシピの job が索引を終えて chunk_set を active にするまでを支える fake。

    記録した派生情報レイヤーは ``/chunk-sets`` の API からも読めるようにする。
    """

    def __init__(self) -> None:
        super().__init__()
        self.activated: list[str] = []
        self.chunk_set_rows: dict[str, dict[str, object]] = {}
        self.layers: dict[str, dict[str, object]] = {}

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
        row = await super().get_document_recipe(document_id, recipe_id)
        return {**row, "active_extraction_recipe_id": "extraction-1"}

    async def list_document_recipes(self, document_id: str) -> list[dict[str, object]]:
        return [await self.get_document_recipe(document_id, "recipe-1")]

    async def get_latest_recipe_chunk_set(
        self, *args: object, **kwargs: object
    ) -> dict[str, object] | None:
        _ = args, kwargs
        return {
            "chunk_set_id": "chunk-set-pending",
            "language_pack_ingest_hash": builtin_pack().ingest_hash,
            "language_pack_release_id": "synthetic-release",
        }

    async def graph_entity_counts_for_chunk_sets(self, chunk_set_ids: list[str]) -> dict[str, int]:
        return {chunk_set_id: 4 for chunk_set_id in chunk_set_ids}

    async def count_chunk_set_chunks(self, chunk_set_id: str) -> int:
        _ = chunk_set_id
        return 3

    async def upsert_chunk_set(self, *, chunk_set_id: str, **kwargs: object) -> None:
        self.chunk_set_rows[chunk_set_id] = {"chunk_set_id": chunk_set_id, **kwargs}

    async def mark_chunk_set_chunked(self, **kwargs: object) -> None:
        _ = kwargs

    async def mark_chunk_set_indexed(self, **kwargs: object) -> None:
        _ = kwargs

    async def activate_recipe_chunk_set(self, *, chunk_set_id: str, **kwargs: object) -> None:
        _ = kwargs
        self.activated.append(chunk_set_id)

    async def update_document_status(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs

    async def get_document_extraction_artifact(
        self, *, document_id: str, extraction_recipe_id: str
    ) -> dict[str, object]:
        return {
            "document_id": document_id,
            "extraction_recipe_id": extraction_recipe_id,
            "status": "materialized",
            "reason": None,
            "extraction_json": {
                **_NAVIGATION_EXTRACTION,
                "parser_artifacts": {
                    "language_pack_ingest_hash": builtin_pack().ingest_hash,
                    "language_pack_release_id": "synthetic-release",
                },
            },
        }

    async def list_document_extraction_field_sets(self, document_id: str) -> list[object]:
        return []

    async def upsert_artifact_layer(self, *, layer_id: str, **kwargs: object) -> None:
        self.layers[layer_id] = {"layer_id": layer_id, **kwargs}

    # --- GET /chunk-sets 用 ---
    async def get_document_summary(self, document_id: str) -> DocumentDetail:
        return await self.get_document(document_id)

    async def list_document_chunk_sets(self, document_id: str) -> list[dict[str, object]]:
        _ = document_id
        return [
            {
                "chunk_set_id": chunk_set_id,
                "recipe_id": "recipe-1",
                "extraction_recipe_id": "extraction-1",
                "status": "INDEXED",
                "chunk_count": 3,
                "vector_count": 3,
                "is_serving": True,
                "knowledge_base_ids": ["kb-1"],
            }
            for chunk_set_id in self.activated
        ]

    async def list_document_knowledge_base_configs(self, document_id: str) -> list[object]:
        _ = document_id
        return []

    async def list_artifact_layers_for_chunk_sets(
        self, chunk_set_ids: Collection[str]
    ) -> dict[str, dict[str, object]]:
        return {
            layer_id: layer
            for layer_id, layer in self.layers.items()
            if layer["parent_chunk_set_id"] in chunk_set_ids
        }


class _RecordingRecipePipeline:
    """呼ばれた工程と ``record_outcome`` を記録する fake pipeline。

    索引まで行う呼び出し(``index_chunked`` / ``ingest``)は、実物と同じく記録していない成功の
    内容を ``deferred_success_outcome`` に残す。成功の記録は ``events`` に "success" を積む。
    """

    calls: list[tuple[str, object]] = []
    events: list[str] = []

    def __init__(self, **_kwargs: object) -> None:
        self.deferred_success_outcome: object | None = None

    def _defer_success(self) -> None:
        self.deferred_success_outcome = _RecordingOutcome()

    async def ingest(self, *args: object, **kwargs: object) -> None:
        _ = args
        _RecordingRecipePipeline.calls.append(("ingest", kwargs.get("record_outcome")))
        _RecordingRecipePipeline.calls.append(
            ("ingest_source", (kwargs.get("source_sha256"), kwargs.get("source_size")))
        )
        self._defer_success()

    async def chunk_reviewed(self, *args: object, **kwargs: object) -> None:
        _ = args
        _RecordingRecipePipeline.calls.append(("chunk_reviewed", kwargs.get("record_outcome")))

    async def index_chunked(self, *args: object, **kwargs: object) -> None:
        _ = args
        _RecordingRecipePipeline.calls.append(("index_chunked", kwargs.get("record_outcome")))
        self._defer_success()


class _RecordingOutcome:
    def record(self) -> None:
        _RecordingRecipePipeline.events.append("success")


class _FailingOutcome:
    def record(self) -> None:
        _RecordingRecipePipeline.events.append("success_failed")
        raise RuntimeError("audit sink unavailable")


class _EventRecipeOracle(_CompletingRecipeOracle):
    """active への切り替え・文書の status の更新・job の完了を、成功の記録と同じ列
    (``events``)に積む fake。"""

    async def get_document(self, document_id: str) -> DocumentDetail:
        detail = await super().get_document(document_id)
        return detail.model_copy(update={"file_size_bytes": 2048})

    async def activate_recipe_chunk_set(self, *, chunk_set_id: str, **kwargs: object) -> None:
        await super().activate_recipe_chunk_set(chunk_set_id=chunk_set_id, **kwargs)
        _RecordingRecipePipeline.events.append("activate")

    async def update_document_status(self, *args: object, **kwargs: object) -> None:
        await super().update_document_status(*args, **kwargs)
        _RecordingRecipePipeline.events.append("document_indexed")

    async def transition_ingestion_job(self, job_id: str, **kwargs: Any) -> IngestionJob | None:
        updated = await super().transition_ingestion_job(job_id, **kwargs)
        if updated is not None:
            _RecordingRecipePipeline.events.append(f"job_{updated.status.value.lower()}")
        return updated


def _prepare_recipe_run(
    monkeypatch: pytest.MonkeyPatch, fake: _EventRecipeOracle, phase: IngestionJobPhase
) -> str:
    """``_run_ingestion_job`` でレシピの job を実行する準備をし、job ID を返す。"""
    _RecordingRecipePipeline.calls = []
    _RecordingRecipePipeline.events = []
    monkeypatch.setattr(documents_route, "IngestionPipeline", _RecordingRecipePipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: Settings())
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *a, **k: None)
    job_id = "job-recipe-1"
    fake.jobs[job_id] = _extract_job(job_id).model_copy(update={"phase": phase})
    return job_id


@pytest.mark.parametrize(
    ("phase", "expected_calls"),
    [
        (IngestionJobPhase.INDEX, [("index_chunked", False)]),
        (IngestionJobPhase.CHUNK, [("chunk_reviewed", False), ("index_chunked", False)]),
        # 確認待ちのゲートなし: 抽出の job が索引まで進む。監査の原本は文書の hash とサイズ。
        (
            IngestionJobPhase.EXTRACT,
            [("ingest", False), ("ingest_source", ("a" * 64, 2048))],
        ),
    ],
    ids=["index", "chunk_then_auto_index", "extract_single_pass"],
)
async def test_recipe_job_records_success_outcome_once_after_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    phase: IngestionJobPhase,
    expected_calls: list[tuple[str, object]],
) -> None:
    """レシピの job は、成功の監査・metric を job を SUCCEEDED にした後に 1 回だけ記録する(#514)。

    pipeline の工程の中では記録させず(``record_outcome=False``)、残された成功の内容を
    ``_materialize_experiment_candidate`` が返し、``_run_ingestion_job`` が SUCCEEDED を書けた後に
    記録する。
    """
    fake = _EventRecipeOracle()
    await ObjectStorageClient().put("prepared/policy.pdf", b"prepared pdf bytes", "application/pdf")
    job_id = _prepare_recipe_run(monkeypatch, fake, phase)

    await documents_route._run_ingestion_job(job_id, propagate_errors=True)

    assert fake.jobs[job_id].status == IngestionJobStatus.SUCCEEDED
    assert _RecordingRecipePipeline.calls == expected_calls
    assert _RecordingRecipePipeline.events == [
        "activate",
        "document_indexed",
        "job_succeeded",
        "success",
    ]
    assert len(fake.activated) == 1


async def test_recipe_job_rebuilds_the_section_map_after_it_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分割と embedding が終わって job が成功したら、章節と chunk の対応を作る（#1438）。"""
    fake = _EventRecipeOracle()
    job_id = _prepare_recipe_run(monkeypatch, fake, IngestionJobPhase.INDEX)

    async def record(document_id: str, *, oracle: object | None = None, reason: str) -> bool:
        _RecordingRecipePipeline.events.append(f"section_map:{reason}")
        return True

    monkeypatch.setattr(documents_route, "refresh_document_section_map", record)

    await documents_route._run_ingestion_job(job_id, propagate_errors=True)

    assert _RecordingRecipePipeline.events == [
        "activate",
        "document_indexed",
        "job_succeeded",
        "section_map:ingestion",
        "success",
    ]


async def test_materialize_returns_success_outcome_without_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_materialize_experiment_candidate`` は成功の監査を記録せず、呼び出し側へ返す(#514)。"""
    fake = _EventRecipeOracle()
    _RecordingRecipePipeline.calls = []
    _RecordingRecipePipeline.events = []
    monkeypatch.setattr(documents_route, "IngestionPipeline", _RecordingRecipePipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: Settings())
    job = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )

    result = await _materialize_experiment_candidate(fake, job)  # type: ignore[arg-type]

    assert result.next_phase is None
    assert isinstance(result.success_outcome, _RecordingOutcome)
    assert _RecordingRecipePipeline.events == ["activate", "document_indexed"]


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("cancelled_before_activation", IngestionJobStatus.CANCELLED),
        ("activation_failed", IngestionJobStatus.FAILED),
        ("document_status_update_failed", IngestionJobStatus.FAILED),
        ("cancelled_before_succeeded", IngestionJobStatus.CANCELLED),
    ],
)
async def test_recipe_job_leaves_no_success_outcome_when_not_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_status: IngestionJobStatus,
) -> None:
    """SUCCEEDED にならなかったレシピの job は、成功の監査・metric を記録しない(#504 / #514)。

    切り替えの前の取り消し・切り替えの失敗に加え、切り替えの後の文書の status の更新の失敗と、
    SUCCEEDED を書く直前の取り消しでも、成功を残さない。
    """
    fake = _EventRecipeOracle()
    job_id = _prepare_recipe_run(monkeypatch, fake, IngestionJobPhase.INDEX)
    original_mark_indexed = fake.mark_chunk_set_indexed
    original_update_status = fake.update_document_status

    async def mark_indexed_then_cancel(**kwargs: object) -> None:
        # 索引を終えた後(切り替えの直前の取り消しの確認より前)に取り消す。
        await original_mark_indexed(**kwargs)
        _cancel_recipe_job(fake, job_id)

    async def failing_activate(**kwargs: Any) -> None:
        _ = kwargs
        raise RuntimeError("activate failed")

    async def failing_update_status(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise RuntimeError("update document status failed")

    async def update_status_then_cancel(*args: object, **kwargs: object) -> None:
        # 工程をすべて終えた後、SUCCEEDED を書く直前に取り消す。
        await original_update_status(*args, **kwargs)
        _cancel_recipe_job(fake, job_id)

    if failure == "cancelled_before_activation":
        monkeypatch.setattr(fake, "mark_chunk_set_indexed", mark_indexed_then_cancel)
    elif failure == "activation_failed":
        monkeypatch.setattr(fake, "activate_recipe_chunk_set", failing_activate)
    elif failure == "document_status_update_failed":
        monkeypatch.setattr(fake, "update_document_status", failing_update_status)
    else:
        monkeypatch.setattr(fake, "update_document_status", update_status_then_cancel)

    await documents_route._run_ingestion_job(job_id)

    assert fake.jobs[job_id].status == expected_status
    assert _RecordingRecipePipeline.calls == [("index_chunked", False)]
    assert "success" not in _RecordingRecipePipeline.events
    assert "job_succeeded" not in _RecordingRecipePipeline.events


async def test_recipe_job_stays_succeeded_when_success_outcome_recording_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """成功の監査の記録に失敗しても、job の成功は取り消さずログに残す(#514)。"""

    class FailingOutcomePipeline(_RecordingRecipePipeline):
        def _defer_success(self) -> None:
            self.deferred_success_outcome = _FailingOutcome()

    fake = _EventRecipeOracle()
    job_id = _prepare_recipe_run(monkeypatch, fake, IngestionJobPhase.INDEX)
    monkeypatch.setattr(documents_route, "IngestionPipeline", FailingOutcomePipeline)

    with caplog.at_level("WARNING", logger=documents_route.logger.name):
        await documents_route._run_ingestion_job(job_id, propagate_errors=True)

    assert fake.jobs[job_id].status == IngestionJobStatus.SUCCEEDED
    assert _RecordingRecipePipeline.events == [
        "activate",
        "document_indexed",
        "job_succeeded",
        "success_failed",
    ]
    assert fake.activated == ["chunk-set-pending"]
    assert any(
        record.getMessage() == "recipe_ingestion_success_outcome_record_failed"
        for record in caplog.records
    )


class _SourceFailureRecipeOracle(_FakeRecipeJobOracle):
    """原本の取得に失敗する PREPROCESS の job 用の fake。文書の status の更新を記録する。"""

    def __init__(self, object_storage_path: str) -> None:
        super().__init__()
        self.object_storage_path = object_storage_path
        self.document_status_updates: list[tuple[object, ...]] = []
        self.recipe_updates: list[dict[str, object]] = []

    async def get_document(self, document_id: str) -> DocumentDetail:
        detail = await super().get_document(document_id)
        return detail.model_copy(
            update={
                "status": FileStatus.UPLOADED,
                "object_storage_path": self.object_storage_path,
                "file_size_bytes": 10,
            }
        )

    async def update_document_recipe_status(
        self, *, recipe_id: str, status: FileStatus, **kwargs: object
    ) -> None:
        await super().update_document_recipe_status(recipe_id=recipe_id, status=status)
        self.recipe_updates.append({"status": status, **kwargs})

    async def update_document_status(self, *args: object, **kwargs: object) -> None:
        self.document_status_updates.append((*args, *kwargs.values()))


@pytest.mark.parametrize(
    ("stored_bytes", "expected_error"),
    [
        (None, "原本ファイルが見つかりません。"),
        (b"0123456789", "原本ファイルの SHA-256 がアップロード時と一致しません。"),
    ],
    ids=["missing", "hash_mismatch"],
)
async def test_recipe_job_source_fetch_failure_marks_only_recipe_error(
    monkeypatch: pytest.MonkeyPatch,
    stored_bytes: bytes | None,
    expected_error: str,
) -> None:
    """原本の取得に失敗したレシピの job も、レシピの行だけを ERROR にする(#504)。

    文書の status(全レシピの集約)は、他の原因で失敗したときと同じく変えない。
    """
    object_path = "local://uploaded/source-failure.pdf"
    if stored_bytes is not None:
        object_path = await ObjectStorageClient().put(
            "uploaded/source-failure.pdf", stored_bytes, "application/pdf"
        )
    fake = _SourceFailureRecipeOracle(object_path)
    fake.jobs["job-preprocess-1"] = _extract_job("job-preprocess-1").model_copy(
        update={"phase": IngestionJobPhase.PREPROCESS}
    )
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "get_settings", lambda: Settings())

    await documents_route._run_ingestion_job("job-preprocess-1")

    job = fake.jobs["job-preprocess-1"]
    assert job.status == IngestionJobStatus.FAILED
    assert job.error_message == expected_error
    assert fake.recipe_status == FileStatus.ERROR
    assert fake.recipe_updates[-1]["error_message"] == expected_error
    assert fake.recipe_updates[-1]["failed_phase"] == IngestionJobPhase.PREPROCESS
    assert fake.document_status_updates == []


def _layer_settings(*, enabled: bool) -> Settings:
    if enabled:
        return Settings(
            rag_graph_profile="entities",
            rag_field_extraction_enabled=True,
            rag_navigation_summary_enabled=True,
        )
    return Settings(
        rag_graph_profile="off",
        rag_field_extraction_enabled=False,
        rag_navigation_summary_enabled=False,
        rag_raptor_enabled=False,
    )


async def test_recipe_job_records_layers_and_chunk_sets_endpoint_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピの job の完了で派生情報レイヤーを記録し、/chunk-sets がその状態を返す(#494)。

    レシピの chunk_set ID は planner の ID と一致しないため、レシピの設定から層 ID を作る。
    """
    fake = _CompletingRecipeOracle()
    settings = _layer_settings(enabled=True)
    _RecordingRecipePipeline.calls = []
    monkeypatch.setattr(documents_route, "IngestionPipeline", _RecordingRecipePipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: settings)
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    job = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )

    await _materialize_experiment_candidate(fake, job)  # type: ignore[arg-type]

    assert fake.activated == ["chunk-set-pending"]
    expected_ids = {
        "metadata": compute_metadata_layer_id("chunk-set-pending", settings),
        "graph": compute_graph_layer_id("chunk-set-pending", settings),
        "navigation": compute_nav_layer_id("chunk-set-pending", settings),
    }
    assert {
        str(layer["layer_kind"]): layer_id for layer_id, layer in fake.layers.items()
    } == expected_ids
    assert all(
        layer["parent_chunk_set_id"] == "chunk-set-pending" for layer in fake.layers.values()
    )
    assert fake.layers[expected_ids["navigation"]]["status"] == "materialized"

    response = await documents_route.list_document_chunk_sets("doc-1")

    assert response.data is not None
    assert len(response.data) == 1
    statuses = response.data[0].layer_statuses
    by_layer = {
        "metadata": statuses.metadata,
        "graph": statuses.graph,
        "navigation": statuses.navigation,
    }
    assert all(status.requested for status in by_layer.values())
    assert {name: status.layer_id for name, status in by_layer.items()} == expected_ids
    assert statuses.navigation.status == DocumentLayerStatusName.MATERIALIZED
    assert statuses.graph.status == DocumentLayerStatusName.MATERIALIZED
    assert "4 件" in (statuses.graph.reason or "")


async def test_recipe_chunk_set_reports_not_requested_layers_without_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピの設定で使わないレイヤーは記録せず、/chunk-sets では「使用しません」を返す。"""
    fake = _CompletingRecipeOracle()
    _RecordingRecipePipeline.calls = []
    monkeypatch.setattr(documents_route, "IngestionPipeline", _RecordingRecipePipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: _layer_settings(enabled=False))
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    job = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )

    await _materialize_experiment_candidate(fake, job)  # type: ignore[arg-type]
    response = await documents_route.list_document_chunk_sets("doc-1")

    assert fake.layers == {}
    assert response.data is not None
    statuses = response.data[0].layer_statuses
    for status in (statuses.metadata, statuses.graph, statuses.navigation):
        assert status.requested is False
        assert status.status == DocumentLayerStatusName.NOT_REQUESTED


# --- 作り直しが必要の判定(#550) ---

_FIELDS_V1 = [FieldDefinition(name="請求書番号", description="invoice no")]
_FIELDS_V2 = [
    FieldDefinition(name="請求書番号", description="invoice no"),
    FieldDefinition(name="合計金額", value_type="number"),
]


class _FingerprintRecipeOracle(_CompletingRecipeOracle):
    """抽出の工程で項目の定義の hash を刻んだ抽出結果を返す fake。"""

    def __init__(self, stamped_fields: list[FieldDefinition]) -> None:
        super().__init__()
        self.extraction_json: dict[str, object] = {
            **_NAVIGATION_EXTRACTION,
            "fields": [
                {"name": "請求書番号", "value": "INV-1", "value_type": "string", "confidence": 0.9}
            ],
            "parser_artifacts": {FIELD_SCHEMA_HASH_ARTIFACT_KEY: field_schema_hash(stamped_fields)},
        }

    async def get_document_extraction_artifact(
        self, *, document_id: str, extraction_recipe_id: str
    ) -> dict[str, object]:
        artifact = await super().get_document_extraction_artifact(
            document_id=document_id, extraction_recipe_id=extraction_recipe_id
        )
        return {**artifact, "extraction_json": self.extraction_json}


async def _record_fingerprinted_layers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, settings: Settings
) -> _FingerprintRecipeOracle:
    """項目の定義 V1 で抽出・索引したレシピの job を完了させ、レイヤーを記録する。"""
    monkeypatch.setenv(FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))
    save_field_schema(_FIELDS_V1)
    fake = _FingerprintRecipeOracle(_FIELDS_V1)
    _RecordingRecipePipeline.calls = []
    monkeypatch.setattr(documents_route, "IngestionPipeline", _RecordingRecipePipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: settings)
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    job = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )
    await _materialize_experiment_candidate(fake, job)  # type: ignore[arg-type]
    return fake


async def _layer_statuses(document_id: str = "doc-1") -> Any:
    response = await documents_route.list_document_chunk_sets(document_id)
    assert response.data is not None
    return response.data[0].layer_statuses


async def test_layer_needs_rebuild_only_when_recorded_fingerprint_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """記録した指紋と今の入力が同じなら出さず、項目の定義が変わると「作り直しが必要」を出す。

    状態(status)は変えず、定義を元に戻すと印も消える(保存しない派生の値)。
    """
    settings = _layer_settings(enabled=True).model_copy(
        update={"rag_chunking_strategy": "structure_aware"}
    )
    fake = await _record_fingerprinted_layers(monkeypatch, tmp_path, settings)
    metadata_id = compute_metadata_layer_id("chunk-set-pending", settings)
    assert fake.layers[metadata_id]["input_fingerprint"] == {
        "field_schema_hash": field_schema_hash(_FIELDS_V1)
    }
    # ナビ要約の上限数の刻みが無い抽出結果からは、ナビの指紋を作らない(不明)。
    nav_id = compute_nav_layer_id("chunk-set-pending", settings)
    assert fake.layers[nav_id]["input_fingerprint"] is None

    statuses = await _layer_statuses()
    assert statuses.metadata.status == DocumentLayerStatusName.MATERIALIZED
    assert statuses.metadata.rebuild_required is False
    assert statuses.metadata.rebuild_inputs == []

    save_field_schema(_FIELDS_V2)
    statuses = await _layer_statuses()
    assert statuses.metadata.status == DocumentLayerStatusName.MATERIALIZED
    assert statuses.metadata.rebuild_required is True
    assert statuses.metadata.rebuild_inputs == ["field_schema_hash"]
    assert statuses.navigation.rebuild_required is False

    save_field_schema(_FIELDS_V1)
    statuses = await _layer_statuses()
    assert statuses.metadata.rebuild_required is False


async def test_layer_without_fingerprint_is_unknown_and_not_flagged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """指紋の無い既存の行(migration 前に作ったレイヤー)は、定義が変わっても警告しない。"""
    settings = _layer_settings(enabled=True)
    fake = await _record_fingerprinted_layers(monkeypatch, tmp_path, settings)
    for layer in fake.layers.values():
        layer["input_fingerprint"] = None
    save_field_schema(_FIELDS_V2)

    statuses = await _layer_statuses()

    for status in (statuses.metadata, statuses.graph, statuses.navigation):
        assert status.rebuild_required is False
        assert status.rebuild_inputs == []


async def test_chunk_metadata_contract_is_part_of_metadata_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """親子階層の分割では chunk metadata の契約を指紋に入れ、契約が上がると作り直しを出す。"""
    settings = _layer_settings(enabled=True).model_copy(
        update={"rag_chunking_strategy": SMALL_TO_BIG_STRATEGY}
    )
    fake = await _record_fingerprinted_layers(monkeypatch, tmp_path, settings)
    metadata_id = compute_metadata_layer_id("chunk-set-pending", settings)
    recorded = fake.layers[metadata_id]["input_fingerprint"]
    assert isinstance(recorded, dict)
    assert recorded["chunk_metadata_contract"] == chunk_metadata_contract_hash()

    fake.layers[metadata_id]["input_fingerprint"] = {
        **recorded,
        "chunk_metadata_contract": "older-contract",
    }
    statuses = await _layer_statuses()

    assert statuses.metadata.rebuild_required is True
    assert statuses.metadata.rebuild_inputs == ["chunk_metadata_contract"]


class _ListFingerprintOracle:
    def __init__(
        self,
        rows: list[dict[str, object]] | Exception,
        field_sets: dict[str, list[list[FieldDefinition] | None]] | None = None,
    ) -> None:
        self.rows = rows
        self.requested: list[str] = []
        self.field_sets = field_sets or {}
        self.field_set_requests: list[list[str]] = []

    async def list_documents_extraction_field_sets(
        self, document_ids: Collection[str]
    ) -> dict[str, list[list[FieldDefinition] | None]]:
        self.field_set_requests.append(list(document_ids))
        return {key: value for key, value in self.field_sets.items() if key in document_ids}

    async def list_serving_artifact_layer_fingerprints(
        self, document_ids: Collection[str]
    ) -> list[dict[str, object]]:
        self.requested = list(document_ids)
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


def _summary(document_id: str) -> DocumentSummary:
    return DocumentSummary(
        id=document_id,
        file_name=f"{document_id}.pdf",
        status=FileStatus.INDEXED,
        uploaded_at=datetime.now(UTC),
    )


def _layer_row(
    document_id: str,
    settings: Settings,
    fingerprint: Mapping[str, object],
    *,
    layer_id: str | None = None,
) -> dict[str, object]:
    chunk_set_id = f"cs-{document_id}"
    return {
        "document_id": document_id,
        "layer_id": layer_id or compute_metadata_layer_id(chunk_set_id, settings),
        "layer_kind": "metadata",
        "parent_chunk_set_id": chunk_set_id,
        "recipe_id": f"recipe-{document_id}",
        "recipe_processing_config": {},
        "document_processing_config": {},
        "input_fingerprint": fingerprint,
    }


async def test_documents_list_marks_documents_whose_layers_need_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """一覧は検索対象の層の指紋を 1 回で読み、今も要求されている層の変化だけを印にする。"""
    monkeypatch.setenv(FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))
    save_field_schema(_FIELDS_V2)
    settings = _layer_settings(enabled=True)
    stale = {"field_schema_hash": field_schema_hash(_FIELDS_V1)}
    current = {"field_schema_hash": field_schema_hash(_FIELDS_V2)}
    oracle = _ListFingerprintOracle(
        [
            _layer_row("doc-stale", settings, stale),
            _layer_row("doc-current", settings, current),
            # 設定が変わって今は要求されていない層(ID が違う)の古い行は印にしない。
            _layer_row("doc-orphan", settings, stale, layer_id="md_orphan"),
        ]
    )
    documents = [_summary(name) for name in ("doc-stale", "doc-current", "doc-orphan", "doc-new")]

    await _mark_layers_rebuild_required(oracle, documents, settings)  # type: ignore[arg-type]

    assert oracle.requested == ["doc-stale", "doc-current", "doc-orphan", "doc-new"]
    assert {document.id: document.layers_rebuild_required for document in documents} == {
        "doc-stale": True,
        "doc-current": False,
        "doc-orphan": False,
        "doc-new": False,
    }


async def test_documents_list_keeps_rows_when_fingerprint_lookup_fails() -> None:
    """指紋の読み出しに失敗しても、一覧は印を付けずにそのまま返す。"""
    documents = [_summary("doc-1")]

    await _mark_layers_rebuild_required(
        _ListFingerprintOracle(RuntimeError("db down")),  # type: ignore[arg-type]
        documents,
        _layer_settings(enabled=True),
    )

    assert documents[0].layers_rebuild_required is False


async def test_documents_list_compares_with_each_documents_knowledge_base_definitions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """一覧は文書ごとに、属する KB の今の定義(無ければ全体の既定)と比べる(#548)。"""
    monkeypatch.setenv(FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))
    save_field_schema(_FIELDS_V2)
    settings = _layer_settings(enabled=True)
    kb_fields = [FieldDefinition(name="契約日", value_type="date")]
    kb_hash = {"field_schema_hash": field_schema_hash(kb_fields)}
    default_hash = {"field_schema_hash": field_schema_hash(_FIELDS_V2)}
    oracle = _ListFingerprintOracle(
        [
            # KB の定義で抽出した層。全体の既定とは違うが、KB の定義は変わっていない。
            _layer_row("doc-kb", settings, kb_hash),
            # KB に定義を足す前に、全体の既定で作った層。
            _layer_row("doc-kb-stale", settings, default_hash),
            # KB の定義を持たない文書は全体の既定と比べる。
            _layer_row("doc-default", settings, default_hash),
        ],
        field_sets={"doc-kb": [kb_fields], "doc-kb-stale": [kb_fields], "doc-default": [None]},
    )
    documents = [_summary(name) for name in ("doc-kb", "doc-kb-stale", "doc-default")]

    await _mark_layers_rebuild_required(oracle, documents, settings)  # type: ignore[arg-type]

    assert oracle.field_set_requests == [["doc-kb", "doc-kb-stale", "doc-default"]]
    assert {document.id: document.layers_rebuild_required for document in documents} == {
        "doc-kb": False,
        "doc-kb-stale": True,
        "doc-default": False,
    }


@pytest.mark.parametrize("fails", [False, True])
async def test_index_reprocess_clones_active_chunks_and_activates_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    class ReindexOracle(_CompletingRecipeOracle):
        def __init__(self) -> None:
            super().__init__()
            self.cloned: list[dict[str, str]] = []

        async def get_latest_recipe_chunk_set(
            self, *args: object, **kwargs: object
        ) -> dict[str, object] | None:
            return {"chunk_set_id": "active-set"} if kwargs.get("active") is True else None

        async def clone_recipe_chunk_set_chunks(self, **kwargs: str) -> None:
            self.cloned.append(kwargs)

    class ReindexPipeline(_RecordingRecipePipeline):
        async def index_chunked(self, *args: object, **kwargs: object) -> None:
            if fails:
                raise RuntimeError("合成の索引エラー")
            await super().index_chunked(*args, **kwargs)

    fake = ReindexOracle()
    monkeypatch.setattr(documents_route, "IngestionPipeline", ReindexPipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: _layer_settings(enabled=False))
    job = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )
    if fails:
        with pytest.raises(RuntimeError, match="合成の索引エラー"):
            await _materialize_experiment_candidate(fake, job)  # type: ignore[arg-type]
    else:
        await _materialize_experiment_candidate(fake, job)  # type: ignore[arg-type]
    assert len(fake.cloned) == 1
    candidate = fake.cloned[0]["target_chunk_set_id"]
    assert candidate != "active-set"
    assert fake.cloned[0]["source_chunk_set_id"] == "active-set"
    assert fake.activated == ([] if fails else [candidate])


@pytest.mark.parametrize(
    ("chunk_status", "count", "expected"),
    [
        ("INDEXED", 0, DocumentLayerStatusName.MATERIALIZED),
        ("INDEXED", 4, DocumentLayerStatusName.MATERIALIZED),
        ("ERROR", 4, DocumentLayerStatusName.ERROR),
        ("CHUNKED", 0, DocumentLayerStatusName.PLANNED_ONLY),
    ],
)
async def test_graph_layer_uses_materialization_result(
    monkeypatch: pytest.MonkeyPatch,
    chunk_status: str,
    count: int,
    expected: DocumentLayerStatusName,
) -> None:
    class GraphOracle(_CompletingRecipeOracle):
        async def graph_entity_counts_for_chunk_sets(
            self, chunk_set_ids: list[str]
        ) -> dict[str, int]:
            return {chunk_set_id: count for chunk_set_id in chunk_set_ids}

        async def list_document_chunk_sets(self, document_id: str) -> list[dict[str, object]]:
            rows = await super().list_document_chunk_sets(document_id)
            return [{**row, "status": chunk_status} for row in rows]

    fake = GraphOracle()
    fake.activated = ["chunk-set-test"]
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "get_settings", lambda: _layer_settings(enabled=True))
    result = await documents_route.list_document_chunk_sets("doc-1")
    assert result.data is not None
    graph = result.data[0].layer_statuses.graph
    assert graph.status == expected
    if chunk_status == "INDEXED":
        assert f"{count} 件" in (graph.reason or "")


@pytest.mark.parametrize("fails", [False, True])
async def test_pending_index_cross_release_clones_before_build_and_preserves_old_set(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    """索引だけに影響する公開版の変更でも、旧 pending 集合を直接更新しない。"""
    from rag_parser_core.language import resolve

    from app.rag.ingestion_language import ingestion_release_scope

    before = install_language_release_stub(monkeypatch)
    pack = resolve(builtin_pack(), {"entity_cues": {"alias_column_words": ["合成の別名"]}})
    after = before.model_copy(
        update={
            "id": "synthetic-new-release",
            "version": 2,
            "effective": pack.to_dict(),
            "hash": pack.hash,
            "ingest_hash": pack.ingest_hash,
        }
    )

    class PendingOracle(_CompletingRecipeOracle):
        def __init__(self) -> None:
            super().__init__()
            self.cloned: list[dict[str, str]] = []

        async def clone_recipe_chunk_set_chunks(self, **kwargs: str) -> None:
            self.cloned.append(kwargs)

    class PendingPipeline(_RecordingRecipePipeline):
        async def index_chunked(self, *args: object, **kwargs: object) -> None:
            assert kwargs["chunk_set_id"] != "chunk-set-pending"
            if fails:
                raise RuntimeError("合成の索引エラー")
            await super().index_chunked(*args, **kwargs)

    fake = PendingOracle()
    monkeypatch.setattr(documents_route, "IngestionPipeline", PendingPipeline)
    monkeypatch.setattr(documents_route, "get_settings", lambda: _layer_settings(enabled=False))
    item = _extract_job().model_copy(
        update={"phase": IngestionJobPhase.INDEX, "status": IngestionJobStatus.RUNNING}
    )
    with ingestion_release_scope(after, item.id):
        if fails:
            with pytest.raises(RuntimeError, match="合成の索引エラー"):
                await _materialize_experiment_candidate(fake, item)  # type: ignore[arg-type]
        else:
            await _materialize_experiment_candidate(fake, item)  # type: ignore[arg-type]
    [cloned] = fake.cloned
    assert cloned["source_chunk_set_id"] == "chunk-set-pending"
    candidate = cloned["target_chunk_set_id"]
    assert candidate != "chunk-set-pending"
    assert "chunk-set-pending" not in fake.chunk_set_rows
    assert fake.activated == ([] if fails else [candidate])
