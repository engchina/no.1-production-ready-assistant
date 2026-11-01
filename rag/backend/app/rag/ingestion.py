"""取込: VLM 抽出 -> チャンク分割 -> 埋め込み -> Oracle AI Database へ索引。"""

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pr_backend_core.bounded_calls import (
    Attempt,
    BoundedCallAborted,
    BoundedCaller,
    BoundedCallError,
    BoundedCallFailed,
)
from pr_backend_core.logging import safe_exception_fields
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pydantic import ValidationError
from rag_parser_core.capabilities import ADAPTER_CAPABILITIES, adapter_supports_source
from rag_parser_core.enterprise_ai import LLM_SYSTEM_PROMPT, sdk_retries_disabled
from rag_parser_core.language import LANGUAGE_PACK_HASH_KEY, current_pack
from rag_parser_core.preprocess import ConvertOutcome, SourceDerivation
from rag_parser_core.result import (
    EXTERNAL_ADAPTER_PACKAGES,
    ParserRegistryResult,
)
from rag_parser_core.source import template_for_source_profile
from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_CONTENT_TYPE,
    STRUCTURED_RECORDS_FILE_EXTENSION,
    RecordBoundaryError,
    record_boundary_plain_text,
)
from rag_pipeline_core.raptor import build_raptor_summaries

from app.clients.ai_calls import attach_ingestion_progress, ingestion_ai_calls, make_ai_caller
from app.clients.enterprise_ai import (
    EnterpriseAiClient,
    EnterpriseAiIncompleteResponseError,
    EnterpriseAiTimeoutError,
    EnterpriseAiValidationError,
)
from app.clients.file_storage import FileStorageClient
from app.clients.genai import GenAiClient
from app.clients.oracle import OracleClient
from app.clients.parser_service import (
    ParserServiceClient,
    ParserServiceUnavailableError,
    parse_progress_id,
    supported_formats_label,
)
from app.clients.preprocess_service import PreprocessServiceClient
from app.config import (
    DEFAULT_PARSER_ADAPTER_BACKEND,
    Settings,
    enterprise_ai_default_model_id,
    enterprise_ai_max_input_tokens,
    get_settings,
)
from app.rag.audit import record_rag_ingestion_audit
from app.rag.chunking import Chunk, chunk_extraction_with_strategy
from app.rag.chunking_small_to_big import (
    SMALL_TO_BIG_STRATEGY,
    build_parent_child_chunks,
    engine_search_text,
    small_to_big_parent_source,
)
from app.rag.chunking_strategy import resolve_chunking_params
from app.rag.cross_references import annotate_cross_references
from app.rag.entity_index import EntityIndex, EntityIndexOptions, build_entity_index
from app.rag.extraction_field_adapter import (
    FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY,
    FIELD_EXTRACTION_RESPONSE_SCHEMA,
    FieldDefinition,
    FieldExtractionFailed,
    UnitExtraction,
    extract_fields_from_extraction,
    field_definitions_prompt,
    load_field_schema,
    parse_extraction_candidates,
    resolve_field_definitions,
    with_field_extraction_outcome,
)
from app.rag.field_extraction_units import (
    MIN_UNIT_TEXT_TOKENS,
    FieldExtractionUnit,
    FieldValueCandidate,
    field_extraction_text_budget,
)
from app.rag.graph_adapter import resolve_graph_adapter
from app.rag.graph_index import GraphIndex, build_graph_index
from app.rag.heading_levels import (
    HEADING_LEVELS_RESPONSE_SCHEMA,
    HeadingLevelOptions,
    apply_heading_levels,
    parse_heading_levels,
    plan_heading_level_inference,
    with_heading_level_report,
)
from app.rag.ingestion_progress import (
    IngestionProgressReporter,
    VisionSegmentsProgress,
    active_ingestion_progress,
    parse_progress_from_service,
    vision_progress_from_ledger,
)
from app.rag.ingestion_prompts import (
    FIELD_EXTRACTION_PROMPT,
    HEADING_LEVEL_PROMPT,
    NAVIGATION_SUMMARY_PROMPT,
    RAPTOR_SUMMARY_SYSTEM_PROMPT,
)
from app.rag.ingestion_quality import build_ingestion_quality_report, requires_review
from app.rag.ingestion_slots import release_heavy_ingestion_slot
from app.rag.ingestion_strategy import extraction_strategy_for_source
from app.rag.layer_fingerprint import (
    FIELD_SCHEMA_HASH_ARTIFACT_KEY,
    NAVIGATION_SUMMARY_MAX_NODES_ARTIFACT_KEY,
    field_schema_hash,
)
from app.rag.navigation import (
    build_navigation_tree,
    navigation_summary_elements,
    summarize_navigation_nodes,
)
from app.rag.observability import (
    TraceOutcome,
    elapsed_ms,
    new_trace_id,
    now,
    record_ingestion,
    record_ingestion_stage,
    record_trace_span,
)
from app.rag.page_labels import (
    PAGE_LABELS_ARTIFACT_KEY,
    apply_page_labels,
    labels_from_artifacts,
    page_labels_artifact,
    pdf_page_labels,
)
from app.rag.parser_source_guard import converted_source_profile
from app.rag.pdf_segment_merge import (
    ContinuationGeometry,
    consolidate_document_extraction,
    extraction_with_page_offset,
    merge_segment_extractions,
)
from app.rag.pdf_segments import PdfPageSegment, split_pdf_page_segments
from app.rag.preprocess_strategy import preprocess_options, resolve_preprocess_profile
from app.rag.variant_keys import (
    compute_document_recipe_extraction_id,
    compute_extraction_recipe_id,
    extraction_recipe_subset,
)
from app.rag.vision import (
    EnterpriseAiPictureDescriber,
    read_figures_with_vision,
    vision_failure,
)
from app.schemas.document import (
    DocumentChunkView,
    DocumentDetail,
    DocumentPreprocessArtifact,
    FileStatus,
    IngestionParseProgress,
    IngestionParseSegmentProgress,
    IngestionParseSegmentStatus,
    IngestionSegment,
    IngestionVisionProgress,
    SourceProfile,
)
from app.schemas.extraction import (
    ExtractionArtifactValue,
    ExtractionField,
    IngestionQualityReport,
    StructuredExtraction,
)

INGESTION_INTERNAL_ERROR_MESSAGE = "取込処理に失敗しました。時間をおいて再実行してください。"
# 解析中のページの進捗を解析サービスから読む間隔(秒。#1999)。
_PARSE_PROGRESS_POLL_SECONDS = 3.0
INGESTION_JOB_CANCELLED_MESSAGE = "利用者によりキャンセルされました。"
EXTRACTION_ARTIFACT_SCHEMA_VERSION = 1
# 観測用に正規化する知識文書カテゴリ。特定業務(帳票等)には固定しない。
OBSERVABILITY_DOCUMENT_TYPES = frozenset(
    {
        "ドキュメント",
        "文書",
        "社内規程",
        "マニュアル",
        "FAQ",
        "議事録",
        "報告書",
        "技術文書",
        "仕様書",
        "手順書",
        "ナレッジ",
        "policy",
        "manual",
        "faq",
        "meeting_notes",
        "report",
        "guide",
        "specification",
        "procedure",
        "knowledge_base",
    }
)
STRUCTURE_TABLE_COUNT_KEYS = ("table_count", "adapter_table_count")
STRUCTURE_FIGURE_COUNT_KEYS = (
    "figure_count",
    "image_count",
    "picture_count",
    "chart_count",
    "asset_count",
)
STRUCTURE_FORMULA_COUNT_KEYS = ("formula_count", "equation_count")
STRUCTURE_PAGE_COUNT_KEYS = ("page_count", "page_total")
STRUCTURE_ASSET_COUNT_KEYS = ("asset_count", "picture_count", "image_count")
FIGURE_ASSET_KINDS = {"figure", "image", "picture", "chart", "diagram"}
logger = logging.getLogger(__name__)


class IngestionUserError(ValueError):
    """利用者が入力や設定を直せる取込エラー。"""

    safe_for_user = True


class IngestionTimeoutError(IngestionUserError):
    """上流 AI 処理の timeout により再実行または設定変更が必要な取込エラー。"""


class IngestionCancelledError(IngestionUserError):
    """cooperative cancellation により取込を中断した。"""


@dataclass(frozen=True)
class _SegmentExtraction:
    """PDF segment とその抽出結果。"""

    segment: PdfPageSegment
    extraction: StructuredExtraction


@dataclass(frozen=True)
class _CachedSegmentLoadResult:
    """成功済み segment artifact の読み戻し結果。"""

    cached: list[_SegmentExtraction]
    missing_ranges: set[tuple[int, int]]


class _VisionJobState:
    """1 回の取込の中で、Vision の作業ディレクトリと実行器を共有する(#2002)。

    PDF の段ごとの解析と並べて先に読んだ図・表の応答(checkpoint)を、すべての段の後の全体の
    読み取りが同じ作業ディレクトリから使う。実行器も共有し、429 の待ちと並行の数の調整を
    先読みと全体の読み取りで同じにする。取込の終わりに作業ディレクトリを消す。
    """

    def __init__(self) -> None:
        self._work_dir: Path | None = None
        self._caller: BoundedCaller | None = None
        self._finish_progress: Callable[[], None] | None = None
        self.segments_progress: VisionSegmentsProgress | None = None

    def work_dir(self) -> Path:
        if self._work_dir is None:
            self._work_dir = Path(tempfile.mkdtemp(prefix="rag-vision-job-"))
        return self._work_dir

    def vision_caller(self, settings: object) -> BoundedCaller:
        if self._caller is None:
            self._caller = make_ai_caller(settings, "vision")
            self._finish_progress = attach_ingestion_progress(self._caller, "vision")
        return self._caller

    def close(self) -> None:
        if self._finish_progress is not None:
            self._finish_progress()
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)


_VISION_JOB: ContextVar[_VisionJobState | None] = ContextVar("rag_vision_job", default=None)


@contextlib.asynccontextmanager
async def _vision_job_scope() -> AsyncIterator[_VisionJobState]:
    """取込の 1 回の間、Vision の作業ディレクトリと実行器を context に置く(#2002)。"""
    state = _VisionJobState()
    token = _VISION_JOB.set(state)
    try:
        yield state
    finally:
        _VISION_JOB.reset(token)
        await asyncio.to_thread(state.close)


class _VisionPrefetcher:
    """段の解析が終わった順に、その段の図・表の Vision を先に読む(流水線。#2002)。

    解析(解析サービスの CPU)と Vision(VLM の応答待ち)を重ねる。段は 1 つずつ読み(段の中の
    図は実行器が並べる)、次の段の解析と並べる。先読みの失敗は全体の読み取りに任せる。止める
    失敗(権限・設定の誤り・取り消し)は、``finish`` で出す。
    """

    def __init__(
        self,
        run: Callable[[IngestionSegment, StructuredExtraction, threading.Event], Awaitable[None]],
    ) -> None:
        self._run = run
        self._queue: asyncio.Queue[tuple[IngestionSegment, StructuredExtraction] | None] = (
            asyncio.Queue()
        )
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._task = asyncio.create_task(self._worker())

    def submit(self, segment: IngestionSegment, extraction: StructuredExtraction) -> None:
        if not self._stop.is_set():
            self._queue.put_nowait((segment, extraction))

    async def _worker(self) -> None:
        while (item := await self._queue.get()) is not None:
            if self._error is not None or self._stop.is_set():
                continue
            segment, extraction = item
            try:
                await self._run(segment, extraction, self._stop)
            except IngestionUserError as exc:
                if not self._stop.is_set():
                    self._error = exc
            except Exception:  # noqa: BLE001 - 先読みは補助。全体の読み取りが読み直す。
                logger.warning(
                    "vision_prefetch_failed",
                    extra={"segment_id": segment.segment_id},
                    exc_info=True,
                )

    async def finish(self) -> None:
        """残りの段の先読みを待つ。止める失敗があれば出す。"""
        self._queue.put_nowait(None)
        await self._task
        if self._error is not None:
            raise self._error

    async def stop(self) -> None:
        """先読みを止める(解析の失敗・取り消し)。読み取り中の図は次の対象の前で止まる。"""
        self._stop.set()
        self._queue.put_nowait(None)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


_VISION_CHECKPOINT_FILE = re.compile(r"^[a-z0-9_.-]{1,64}/vision/checkpoints/[0-9a-f]{64}\.json$")


def _vision_layout_records(extraction: StructuredExtraction) -> Mapping[str, object] | None:
    layout = extraction.parser_artifacts.get("layout_records")
    if isinstance(layout, Mapping) and layout.get("records"):
        return layout
    return None


def _collect_vision_checkpoint_files(
    work_dir: Path, *, page_start: int, page_end: int
) -> bytes | None:
    """作業ディレクトリの checkpoint のうち、段のページの再利用できる応答をまとめる。"""
    files: dict[str, str] = {}
    for path in sorted(work_dir.glob("*/vision/checkpoints/*.json")):
        relative = path.relative_to(work_dir).as_posix()
        if not _VISION_CHECKPOINT_FILE.match(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            saved = json.loads(text)
        except (OSError, ValueError):
            continue
        record = saved.get("record") if isinstance(saved, dict) else None
        page = record.get("page") if isinstance(record, dict) else None
        if not (saved.get("reusable") and isinstance(page, int)):
            continue
        if page_start <= page <= page_end:
            files[relative] = text
    if not files:
        return None
    return json.dumps({"version": 1, "files": files}, ensure_ascii=False).encode("utf-8")


def _write_vision_checkpoint_files(work_dir: Path, data: bytes) -> int:
    """段の artifact の checkpoint を作業ディレクトリへ書く(既にある応答は上書きしない)。"""
    try:
        payload = json.loads(data)
    except ValueError:
        return 0
    files = payload.get("files") if isinstance(payload, dict) else None
    if payload.get("version") != 1 or not isinstance(files, dict):
        return 0
    written = 0
    for relative, text in files.items():
        if not (isinstance(relative, str) and isinstance(text, str)):
            continue
        if not _VISION_CHECKPOINT_FILE.match(relative):
            continue
        path = work_dir / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written += 1
    return written


class _SegmentParseTracker:
    """段ごとの解析の状態を job の進捗(``IngestionParseProgress.segments``)へ写す(#2001)。

    段の状態は抽出セグメントの行と同じ時に書き換え、画面は段の帯・「抽出: N / M ページ」・解析の
    bar をこの 1 つの記録から作る。job の外(``progress`` が None)では何もしない。
    """

    def __init__(
        self,
        progress: IngestionProgressReporter | None,
        *,
        engine: str,
        page_total: int,
        segments: Sequence[IngestionSegment],
    ) -> None:
        self._progress = progress
        self._engine = engine
        self._page_total = page_total
        self._segments: dict[str, IngestionParseSegmentProgress] = {}
        for segment in segments:
            if segment.page_start is None or segment.page_end is None:
                continue
            self._segments[segment.segment_id] = IngestionParseSegmentProgress(
                segment_id=segment.segment_id,
                page_start=segment.page_start,
                page_end=segment.page_end,
                status=_parse_segment_status(segment.status),
            )
        self._publish(finished=False)

    def mark(
        self, segment_id: str, status: IngestionParseSegmentStatus, *, reused: bool = False
    ) -> None:
        current = self._segments.get(segment_id)
        if current is None:
            return
        span = current.page_end - current.page_start + 1
        self._segments[segment_id] = current.model_copy(
            update={
                "status": status,
                "completed": span if status == "SUCCEEDED" else 0,
                "reused": reused or (current.reused and status == "SUCCEEDED"),
            }
        )
        self._publish(finished=False)

    def update_running(self, segment_id: str, completed: int) -> None:
        current = self._segments.get(segment_id)
        if current is None or current.status != "RUNNING":
            return
        span = current.page_end - current.page_start + 1
        self._segments[segment_id] = current.model_copy(
            update={"completed": max(0, min(completed, span))}
        )
        self._publish(finished=False)

    def finish(self) -> None:
        self._publish(finished=True)

    def _publish(self, *, finished: bool) -> None:
        if self._progress is None:
            return
        segments = sorted(self._segments.values(), key=lambda item: item.page_start)
        if finished:
            completed = total = self._page_total
        else:
            running = [item for item in segments if item.status == "RUNNING"]
            completed = sum(item.completed for item in running)
            total = sum(item.page_end - item.page_start + 1 for item in running)
        self._progress.update_parse(
            IngestionParseProgress(
                engine=self._engine,
                completed=completed,
                total=total if (finished or total) else None,
                finished=finished,
                segments=segments,
            )
        )


def _parse_segment_status(status: str) -> IngestionParseSegmentStatus:
    if status in {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}:
        return status  # type: ignore[return-value]
    return "QUEUED"


# 段の artifact の自描述の key。まとめた結果(原本全体の抽出)には持ち込まない。
_SEGMENT_ARTIFACT_KEY_PREFIXES = ("extraction_artifact_", "segment_parser_")


def _without_segment_artifact_keys(extraction: StructuredExtraction) -> StructuredExtraction:
    parser_artifacts = {
        key: value
        for key, value in extraction.parser_artifacts.items()
        if not key.startswith(_SEGMENT_ARTIFACT_KEY_PREFIXES)
    }
    return extraction.model_copy(update={"parser_artifacts": parser_artifacts})


@dataclass(frozen=True)
class IngestionSuccessOutcome:
    """索引まで終えた取込の、まだ記録していない成功の監査・metric の内容(#504)。

    本文(抽出テキスト・chunk)は持たない。原本は ``source_sha256`` / ``source_size`` が
    あればそれを、なければ ``source_bytes`` の hash と長さを監査に残す(記録時に計算する)。
    レシピの job は、job を SUCCEEDED にできた後に :meth:`record` を 1 回だけ呼ぶ(#514)。
    """

    trace_id: str
    document_id: str
    started_at: float
    source_bytes: bytes
    source_sha256: str | None
    source_size: int | None
    document_type: str | None
    extraction_confidence: float | None
    parser_backend: str | None
    parser_profile: str | None
    segment_count: int
    fallback_count: int
    failed_segment_count: int
    chunk_count: int
    vector_count: int

    def record(self) -> None:
        """成功の metric と監査を記録する。経過時間は取込の開始から記録時までとする。"""
        record_ingestion("success", self.chunk_count)
        record_rag_ingestion_audit(
            trace_id=self.trace_id,
            document_id=self.document_id,
            outcome="success",
            source_bytes=self.source_bytes,
            document_type=self.document_type,
            extraction_confidence=self.extraction_confidence,
            parser_backend=self.parser_backend,
            parser_profile=self.parser_profile,
            segment_count=self.segment_count,
            fallback_count=self.fallback_count,
            failed_segment_count=self.failed_segment_count,
            chunk_count=self.chunk_count,
            vector_count=self.vector_count,
            elapsed_ms=elapsed_ms(self.started_at),
            source_sha256=self.source_sha256,
            source_size=self.source_size,
        )


class IngestionPipeline:
    """ドキュメント取込パイプライン。"""

    def __init__(
        self,
        vlm: EnterpriseAiClient | None = None,
        genai: GenAiClient | None = None,
        oracle: OracleClient | None = None,
        object_storage: FileStorageClient | None = None,
        settings: Settings | None = None,
        recipe_id: str | None = None,
        recipe_revision: int | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._recipe_id = recipe_id
        self._recipe_revision = recipe_revision
        # ``record_outcome=False`` で索引まで終えた取込の、記録していない成功の内容(#504)。
        # 公開の工程(ingest / index_reviewed / chunk_reviewed / index_chunked)の開始で空に戻す。
        self.deferred_success_outcome: IngestionSuccessOutcome | None = None
        self._vlm = vlm or EnterpriseAiClient(settings=self._settings)
        self._genai = genai or GenAiClient(settings=self._settings)
        self._oracle = oracle or OracleClient(settings=self._settings)
        self._object_storage = object_storage or FileStorageClient(settings=self._settings)
        # 外部 adapter は同一プロセスで import せず parser マイクロサービスへ HTTP 委譲する。
        self._parser_service = ParserServiceClient(settings=self._settings)
        # 前処理(parse 前の原本変換)。軽量正規化は in-process、重い変換はサービスへ委譲。
        self._preprocess = PreprocessServiceClient(self._settings)

    def _selected_parser_backend(self) -> str:
        """設定の解析エンジン(小文字)。廃止済みの local / local_partition は既定のエンジン。"""
        backend = (
            str(
                getattr(
                    self._settings,
                    "rag_parser_adapter_backend",
                    DEFAULT_PARSER_ADAPTER_BACKEND,
                )
            )
            .strip()
            .casefold()
            or DEFAULT_PARSER_ADAPTER_BACKEND
        )
        # 廃止済み baseline 値 local / local_partition は既定エンジンのサービスへマップする
        # (in-process 解析は実行しない)。診断側は設定値 local をそのまま baseline として扱う。
        if backend in {"local", "local_partition"}:
            return DEFAULT_PARSER_ADAPTER_BACKEND
        return backend

    def _partition_source(
        self,
        *,
        parse_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
    ) -> ParserRegistryResult:
        """選択 parser backend を parser マイクロサービスへ HTTP 委譲する。

        in-process 解析・local fallback は一切持たない。
        - 非対応形式(outlook .msg / TIFF / 旧 Office バイナリ)は profiling 済みの
          ``source_profile.unsupported_reason`` を sentinel として返す。
        - それ以外(既定 ``docling`` 含む)は runner を fail-fast で実行する。
          抽出が空なら ``ParserServiceUnavailableError`` を送出し、別経路へ縮退しない。
        """
        unsupported_reason = (
            source_profile.unsupported_reason if source_profile is not None else None
        )
        if unsupported_reason:
            return ParserRegistryResult(
                extraction=None,
                parser_backend="unsupported",
                template=f"unsupported_{unsupported_reason}",
                warnings=(unsupported_reason,),
                unsupported_reason=unsupported_reason,
            )
        backend = self._selected_parser_backend()
        if backend not in ADAPTER_CAPABILITIES:
            raise ParserServiceUnavailableError(
                backend,
                "engine_removed",
                warning_code="parser_engine_removed",
            )
        # pre-flight: 宣言 matrix で非対応形式なら microservice・モデルの API を呼ぶ前に停止する
        # (preprocess 変換後の content_type で判定するため、office_to_pdf 済み等は通る)。
        if not adapter_supports_source(
            backend, source_profile=source_profile, content_type=content_type
        ):
            raise ParserServiceUnavailableError(
                backend,
                "adapter_source_unsupported",
                warning_code=f"{backend}_adapter_source_unsupported",
            )
        return self._parser_service.runner(
            backend,
            parse_bytes,
            source_profile,
            content_type,
            fail_fast=True,
        )

    async def _poll_parse_progress(
        self,
        progress_id: str,
        on_progress: Callable[[IngestionParseProgress], None],
    ) -> None:
        """解析サービスの呼び出しの間、ページの進捗を間隔ごとに読んで渡す(#1999)。

        読み取りの失敗は無視する(進捗は補助の表示で、解析の結果を変えない)。
        """
        while True:
            await asyncio.sleep(_PARSE_PROGRESS_POLL_SECONDS)
            payload = await asyncio.to_thread(self._parser_service.fetch_progress, progress_id)
            parsed = parse_progress_from_service(payload) if payload is not None else None
            if parsed is not None:
                on_progress(parsed)

    async def _run_partition_with_progress(
        self,
        *,
        trace_id: str,
        parse_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        attributes: Mapping[str, object],
        on_progress: Callable[[IngestionParseProgress], None] | None,
    ) -> ParserRegistryResult:
        """解析サービスを 1 回呼ぶ。``on_progress`` があれば、その間のページの進捗を渡す。"""
        progress_id = uuid4().hex if on_progress is not None else None
        progress_poll = (
            asyncio.create_task(self._poll_parse_progress(progress_id, on_progress))
            if on_progress is not None and progress_id is not None
            else None
        )
        try:
            with parse_progress_id(progress_id):
                return await _observe_cpu_ingestion_stage(
                    trace_id,
                    "source_partition",
                    lambda: self._partition_source(
                        parse_bytes=parse_bytes,
                        source_profile=source_profile,
                        content_type=content_type,
                    ),
                    attributes=attributes,
                    result_attributes=_parser_result_attributes,
                )
        except ParserServiceUnavailableError as exc:
            raise IngestionUserError(str(exc)) from exc
        finally:
            if progress_poll is not None:
                progress_poll.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_poll

    async def _partition_whole_source(
        self,
        *,
        trace_id: str,
        parse_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        attributes: Mapping[str, object],
        progress: IngestionProgressReporter | None,
    ) -> ParserRegistryResult:
        """原本全体を 1 回で解析する(PDF 以外・段が 1 つ・段ごとに解析しない解析エンジン)。

        解析の間のページの進捗を解析サービスから読み、job の進捗へ写す(#1999)。最初に段の
        始まり(ページ数は未確定)を記録し、解析サービスが進捗を返さない間もその段を出す。
        """
        if progress is not None:
            progress.update_parse(IngestionParseProgress())
        return await self._run_partition_with_progress(
            trace_id=trace_id,
            parse_bytes=parse_bytes,
            source_profile=source_profile,
            content_type=content_type,
            attributes=attributes,
            on_progress=progress.update_parse if progress is not None else None,
        )

    async def _pdf_segment_parse_plan(
        self,
        *,
        parse_bytes: bytes,
        content_type: str,
        source_profile: SourceProfile | None,
    ) -> tuple[str, list[PdfPageSegment]] | None:
        """段ごとに解析する PDF なら (解析エンジン, 段) を返す(#2001)。それ以外は None。

        対象は解析サービス(Docling・Unstructured など)・外部の解析 API で解析する 2 段以上の PDF。
        段ごとの解析に対応しない解析エンジンと、形式・設定で解析できない原本は
        原本全体の経路に任せる(そちらが利用者向けの理由で止める)。
        """
        settings = self._settings
        if not (
            getattr(settings, "rag_pdf_segmentation_enabled", True)
            and getattr(settings, "rag_segment_checkpoint_enabled", True)
        ):
            return None
        if _observability_content_type(content_type) != "application/pdf":
            return None
        if source_profile is not None and source_profile.unsupported_reason:
            return None
        backend = self._selected_parser_backend()
        if backend not in ADAPTER_CAPABILITIES:
            return None
        if not adapter_supports_source(
            backend, source_profile=source_profile, content_type=content_type
        ):
            return None
        segments = await asyncio.to_thread(
            _pdf_segments_for_ingestion,
            parse_bytes,
            content_type=content_type,
            max_pages_per_segment=settings.rag_pdf_max_pages_per_segment,
            max_segments=settings.rag_pdf_max_segments,
            enabled=True,
        )
        if len(segments) < 2:
            return None
        return backend, segments

    async def _prepare_pdf_parse_segments(
        self,
        *,
        document_id: str,
        parser_backend: str,
        pdf_segments: Sequence[PdfPageSegment],
        progress: IngestionProgressReporter | None,
    ) -> list[IngestionSegment]:
        """段の記録を解析の前に用意する(#2001)。

        前の試行(中断・失敗・backend の再起動)の段の記録が同じ段の分け方なら行を残し、成功した段は
        そのまま(抽出結果の読み戻しは ``_parse_pdf_segments``)、ほかの段(待機中・処理中のまま
        残ったもの・失敗・取消)は待機中から始める。段の分け方・解析エンジンが変わったら作り直す。
        """
        desired = [
            IngestionSegment(
                segment_id=_pdf_checkpoint_segment_id(
                    document_id, segment, recipe_id=self._recipe_id
                ),
                document_id=document_id,
                recipe_id=self._recipe_id,
                status="QUEUED",
                parser_backend=parser_backend,
                parser_profile=parser_backend,
                page_start=segment.page_start,
                page_end=segment.page_end,
                progress_unit="page",
                progress_start=segment.page_start,
                progress_end=segment.page_end,
            )
            for segment in pdf_segments
        ]
        existing = await self._safe_list_ingestion_segments(document_id)
        existing_by_id = {segment.segment_id: segment for segment in existing}
        same_plan = set(existing_by_id) == {segment.segment_id for segment in desired} and all(
            existing_by_id[segment.segment_id].parser_backend == parser_backend
            for segment in desired
        )
        if not same_plan:
            await self._safe_replace_ingestion_segments(document_id, desired)
            segments = desired
        else:
            segments = []
            for segment in desired:
                previous = existing_by_id[segment.segment_id]
                if previous.status == "SUCCEEDED":
                    segments.append(previous)
                    continue
                await self._safe_update_ingestion_segment(
                    segment.segment_id, status="QUEUED", error_code="", error_message=""
                )
                segments.append(
                    previous.model_copy(
                        update={"status": "QUEUED", "error_code": None, "error_message": None}
                    )
                )
        return segments

    async def _parse_pdf_segments(
        self,
        *,
        trace_id: str,
        document_id: str,
        parser_backend: str,
        pdf_segments: Sequence[PdfPageSegment],
        checkpoint_segments: Sequence[IngestionSegment],
        source_profile: SourceProfile | None,
        content_type: str,
        attributes: Mapping[str, object],
        progress: IngestionProgressReporter | None,
        cancel_checker: Callable[[], Awaitable[bool]] | None,
        on_segment_parsed: Callable[[IngestionSegment, StructuredExtraction], None] | None = None,
    ) -> tuple[ParserRegistryResult, list[IngestionSegment]]:
        """PDF を段ごとに解析し、原本全体の 1 つの解析結果にまとめる(#2001)。

        ``on_segment_parsed`` は、段の抽出結果(原本のページにそろえたもの)ができるたびに呼ぶ
        (前の試行の段の artifact を使う段は最初にページの順に。#2002 の Vision の先読み)。

        - 成功した段の抽出結果(前の試行の段の artifact を含む)はそのまま使い、ほかの段だけを
          解析サービスへ送る。段ごとに 待機中 → 処理中 → 完了 / 失敗 を書き、抽出結果を段の
          artifact として保存する(再開に使う)。
        - 同時に解析する段の数は ``rag_pdf_segment_parse_concurrency``(既定 1 = 順番)。
        - 段が失敗したら、解析中の段の終わりを待ち、まだ始めていない段は取消にして止める
          (成功した段は次の試行で使う)。
        - すべての段が成功したら、ページの順にまとめる(``pdf_segment_merge``)。文書全体の整理・
          Vision・項目抽出・章節の木・chunk は、まとめた結果から作る(``ingest``)。
        """
        page_total = max(segment.page_end for segment in pdf_segments)
        tracker = _SegmentParseTracker(
            progress,
            engine=parser_backend,
            page_total=page_total,
            segments=checkpoint_segments,
        )
        ranges = {(segment.page_start, segment.page_end) for segment in pdf_segments}
        parse_keys = {
            (segment.page_start, segment.page_end): self._segment_parse_key(segment.content)
            for segment in pdf_segments
        }
        cached = await self._load_cached_segment_extractions(
            checkpoint_segments,
            target_segments=[],
            available_ranges=ranges,
            parse_keys=parse_keys,
        )
        results: dict[tuple[int, int], tuple[StructuredExtraction, ParserRegistryResult | None]] = {
            (item.segment.page_start, item.segment.page_end): (item.extraction, None)
            for item in cached.cached
        }
        checkpoints_by_range = {
            (segment.page_start, segment.page_end): segment
            for segment in checkpoint_segments
            if segment.page_start is not None and segment.page_end is not None
        }
        for page_range in sorted(results):
            tracker.mark(checkpoints_by_range[page_range].segment_id, "SUCCEEDED", reused=True)
            if on_segment_parsed is not None:
                on_segment_parsed(checkpoints_by_range[page_range], results[page_range][0])
        # 成功の記録はあるが artifact を読み戻せない段は、待機中に戻して解析し直す。
        for page_range in cached.missing_ranges:
            checkpoint = checkpoints_by_range.get(page_range)
            if checkpoint is not None:
                await self._safe_update_ingestion_segment(checkpoint.segment_id, status="QUEUED")
                tracker.mark(checkpoint.segment_id, "QUEUED")
        pending = [
            segment
            for segment in pdf_segments
            if (segment.page_start, segment.page_end) not in results
        ]
        errors: list[BaseException] = []
        queue = list(pending)
        concurrency = max(1, int(getattr(self._settings, "rag_pdf_segment_parse_concurrency", 1)))

        async def parse_one(segment: PdfPageSegment) -> None:
            page_range = (segment.page_start, segment.page_end)
            checkpoint = checkpoints_by_range[page_range]
            await self._safe_update_ingestion_segment(
                checkpoint.segment_id,
                status="RUNNING",
                attempt_count=checkpoint.attempt_count + 1,
                error_code="",
                error_message="",
            )
            tracker.mark(checkpoint.segment_id, "RUNNING")
            try:
                result = await self._run_partition_with_progress(
                    trace_id=trace_id,
                    parse_bytes=segment.content,
                    source_profile=source_profile,
                    content_type=content_type,
                    attributes={
                        **attributes,
                        "segment_page_start": segment.page_start,
                        "segment_page_end": segment.page_end,
                    },
                    on_progress=(
                        (
                            lambda parsed: tracker.update_running(
                                checkpoint.segment_id, parsed.completed
                            )
                        )
                        if progress is not None
                        else None
                    ),
                )
                extraction = self._segment_extraction_from_result(result)
                extraction = extraction_with_page_offset(
                    extraction, page_start=segment.page_start, page_end=segment.page_end
                )
                artifact_path = await self._cache_segment_extraction_artifact(
                    document_id=document_id,
                    trace_id=trace_id,
                    segment=checkpoint,
                    extraction=extraction,
                    parse_key=parse_keys[page_range],
                    parser_result=result,
                )
            except asyncio.CancelledError:
                # job の task の取り消し(lease の喪失など)。行は処理中のまま残り、次の試行が
                # 待機中から始める(``_prepare_pdf_parse_segments``)。
                tracker.mark(checkpoint.segment_id, "CANCELLED")
                raise
            except Exception as exc:
                await self._mark_segment_failed(checkpoint, error=exc)
                tracker.mark(checkpoint.segment_id, "FAILED")
                raise
            await self._mark_segment_succeeded(checkpoint, artifact_path=artifact_path)
            tracker.mark(checkpoint.segment_id, "SUCCEEDED")
            results[page_range] = (extraction, result)
            if on_segment_parsed is not None:
                # 解析の終わった段の図・表の Vision を、次の段の解析と並べて始める(#2002)。
                on_segment_parsed(checkpoint, extraction)

        async def worker() -> None:
            while queue and not errors:
                try:
                    # 取り消しは段を始める前に確かめる(始めていない段は queue に残して取消にする)。
                    await _raise_if_cancelled(cancel_checker)
                except IngestionCancelledError as exc:
                    errors.append(exc)
                    return
                segment = queue.pop(0)
                try:
                    await parse_one(segment)
                except Exception as exc:  # noqa: BLE001 - 解析中の段の終わりを待ってから送出する
                    errors.append(exc)

        await asyncio.gather(*(worker() for _ in range(min(concurrency, len(queue)) or 1)))
        if errors:
            for segment in queue:
                checkpoint = checkpoints_by_range[(segment.page_start, segment.page_end)]
                await self._mark_segment_failed(
                    checkpoint,
                    status="CANCELLED",
                    error_code="segment_not_started",
                    error_message=(
                        "前の段の解析が失敗・取消になったため、この段は解析していません。"
                        "再試行すると、この段から解析します。"
                    ),
                )
                tracker.mark(checkpoint.segment_id, "CANCELLED")
            cancelled = next(
                (error for error in errors if isinstance(error, IngestionCancelledError)), None
            )
            raise cancelled or errors[0]

        merged = merge_segment_extractions(
            [
                (page_range[0], _without_segment_artifact_keys(extraction))
                for page_range, (extraction, _result) in results.items()
            ]
        )
        tracker.finish()
        fresh = [result for _extraction, result in results.values() if result is not None]
        first_extraction = results[min(results)][0]
        parser_result = ParserRegistryResult(
            extraction=merged,
            parser_backend=parser_backend,
            parser_version=(
                fresh[0].parser_version
                if fresh
                else str(
                    first_extraction.parser_artifacts.get("segment_parser_version")
                    or parser_backend
                )
            ),
            template=(
                fresh[0].template
                if fresh
                else str(
                    first_extraction.parser_artifacts.get("segment_parser_template")
                    or f"{parser_backend}_layout"
                )
            ),
            warnings=tuple(_dedupe_text([w for result in fresh for w in result.warnings])),
        )
        latest = await self._segments_with_latest_status(checkpoint_segments)
        return parser_result, latest

    def _segment_extraction_from_result(self, result: ParserRegistryResult) -> StructuredExtraction:
        """段の解析の結果から抽出を取り出す。使えない結果は原本全体の解析と同じ理由で止める。"""
        if result.unsupported_reason:
            raise IngestionUserError(_unsupported_parser_message(result))
        self._raise_if_selected_parser_was_not_used(result)
        if result.extraction is None:
            raise IngestionUserError(
                "選択した解析エンジンで抽出できませんでした。"
                "別経路へ縮退せず取込を停止しました。"
                "解析サービスの稼働と設定を確認してから再実行してください。"
            )
        return result.extraction

    def _segment_parse_key(self, segment_bytes: bytes) -> str:
        """段の抽出結果を使い回してよいかの鍵(段の PDF の hash と、解析・前処理の設定)。"""
        segment_sha = hashlib.sha256(segment_bytes).hexdigest()
        key = compute_extraction_recipe_id(segment_sha, self._settings)
        if self._recipe_id is not None and self._recipe_revision is not None:
            key = compute_document_recipe_extraction_id(key, self._recipe_id, self._recipe_revision)
        return key

    async def _cache_segment_extraction_artifact(
        self,
        *,
        document_id: str,
        trace_id: str,
        segment: IngestionSegment,
        extraction: StructuredExtraction,
        parse_key: str,
        parser_result: ParserRegistryResult,
    ) -> str | None:
        """段の抽出結果(原本のページにそろえたもの)を段の artifact として保存する(#2001)。

        保存の失敗は取込を止めない(その段は次の試行で解析し直す)。
        """
        if not getattr(self._settings, "rag_extraction_artifact_cache_enabled", True):
            return None
        key_prefix = _safe_artifact_prefix(
            getattr(self._settings, "rag_extraction_artifact_prefix", "artifacts/extractions")
        )
        key = (
            f"{key_prefix}/{_safe_artifact_key_part(document_id)}/segments/"
            f"{_safe_artifact_key_part(segment.segment_id)}.json"
        )
        cache_extraction = _extraction_with_artifact_cache_metadata(
            extraction,
            artifact_kind="segment",
            document_id=document_id,
            trace_id=trace_id,
            segment=segment,
        )
        cache_extraction = cache_extraction.model_copy(
            update={
                "parser_artifacts": {
                    **cache_extraction.parser_artifacts,
                    "extraction_artifact_parse_key": parse_key,
                    "segment_parser_version": parser_result.parser_version,
                    "segment_parser_template": parser_result.template,
                }
            }
        )
        data = json.dumps(
            cache_extraction.to_document_payload(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        try:
            return await self._object_storage.put(key, data, content_type="application/json")
        except Exception as exc:  # noqa: BLE001 - 段の artifact は再開の補助。取込は続ける。
            logger.warning(
                "segment_extraction_artifact_cache_failed",
                extra={"segment_id": segment.segment_id, "error_type": type(exc).__name__},
            )
            return None

    async def _mark_unfinished_segments_running(self, document_id: str) -> None:
        """原本全体を解析する間、前の試行の終わっていない段の記録を処理中にする(#2001)。

        段の記録は解析の後に作り直す(``_prepare_segment_checkpoints``)。その前に前の試行の
        待機中のまま残った記録が出ると、解析の bar が進むのに段は待機中のままに見える。
        """
        for segment in await self._safe_list_ingestion_segments(document_id):
            if segment.status in {"QUEUED", "CANCELLED"}:
                await self._safe_update_ingestion_segment(segment.segment_id, status="RUNNING")

    async def _image_retrieval_prompt(self) -> str | None:
        """画面で編集した画像検索のプロンプト(Vision 用)。未編集・読込失敗は None(既定値)。"""
        from rag_engine.knowledge.prompt_files import IMAGE_RETRIEVAL_PROMPT_KEY

        return await self._saved_prompt(IMAGE_RETRIEVAL_PROMPT_KEY)

    async def _saved_prompt_snapshot(self) -> dict[str, str]:
        """保存済み指示を一度に読み、同じ図の処理で新旧の設定を混ぜない。"""
        if not hasattr(self._oracle, "answer_prompt_overrides"):
            return {}
        try:
            return await self._oracle.answer_prompt_overrides()
        except Exception:  # noqa: BLE001 - 編集したプロンプトは補助。既定値で解析を続ける。
            logger.warning("ingestion prompt load failed", exc_info=True)
            return {}

    async def _saved_prompt(self, key: str, fallback: str | None = None) -> str | None:
        """各段の保存済み指示を読む。未編集・読込失敗は既定を使う。"""
        return (await self._saved_prompt_snapshot()).get(key, fallback)

    async def _stage_prompt(self, key: str, fallback: str) -> str:
        return await self._saved_prompt(key, fallback) or fallback

    def _raise_if_selected_parser_was_not_used(
        self,
        parser_result: ParserRegistryResult,
    ) -> None:
        """明示選択された外部 parser が抽出に失敗した場合は別経路へ進めない。"""
        selected_backend = (
            str(getattr(self._settings, "rag_parser_adapter_backend", "local")).strip().casefold()
        )
        if selected_backend not in EXTERNAL_ADAPTER_PACKAGES:
            return
        if (
            parser_result.parser_backend == selected_backend
            and parser_result.extraction is not None
            and not parser_result.fallback_used
        ):
            return
        raise IngestionUserError(_selected_parser_failure_message(selected_backend, parser_result))

    async def ingest(
        self,
        document_id: str,
        image_bytes: bytes,
        prompt: str,
        *,
        content_type: str = "application/octet-stream",
        source_profile: SourceProfile | None = None,
        chunk_set_id: str | None = None,
        record_outcome: bool = True,
        original_object_storage_path: str | None = None,
        prepared_artifact: DocumentPreprocessArtifact | None = None,
        manage_document_state: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
        source_sha256: str | None = None,
        source_size: int | None = None,
    ) -> DocumentDetail:
        """1 ドキュメントを取込し、ベクトル索引まで行う(``_ingest``)。

        取込の 1 回の間、Vision の作業ディレクトリと実行器を共有する(#2002)。
        """
        async with _vision_job_scope():
            return await self._ingest(
                document_id,
                image_bytes,
                prompt,
                content_type=content_type,
                source_profile=source_profile,
                chunk_set_id=chunk_set_id,
                record_outcome=record_outcome,
                original_object_storage_path=original_object_storage_path,
                prepared_artifact=prepared_artifact,
                manage_document_state=manage_document_state,
                cancel_checker=cancel_checker,
                source_sha256=source_sha256,
                source_size=source_size,
            )

    async def _ingest(
        self,
        document_id: str,
        image_bytes: bytes,
        prompt: str,
        *,
        content_type: str = "application/octet-stream",
        source_profile: SourceProfile | None = None,
        chunk_set_id: str | None = None,
        record_outcome: bool = True,
        original_object_storage_path: str | None = None,
        prepared_artifact: DocumentPreprocessArtifact | None = None,
        manage_document_state: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
        source_sha256: str | None = None,
        source_size: int | None = None,
    ) -> DocumentDetail:
        """1 ドキュメントを取込し、ベクトル索引まで行う。

        ``manage_document_state=False``(candidate モード)では文書の status / preprocess
        artifact / 配信を一切更新せず、失敗しても文書を ERROR にしない。レシピ実験で配信中の
        文書を乱さず候補 chunk_set だけ materialize するために使う(REVIEW ゲートも無視して
        必ず索引まで進める)。chunk_set の行確定・配信は呼び出し側が行う。
        ``source_sha256`` / ``source_size`` を渡すと、成功・失敗の監査の原本の hash とサイズに
        ``image_bytes`` ではなくそれを使う(処理後ファイルを読むレシピの job。#504)。
        """
        self.deferred_success_outcome = None
        started_at = now()
        trace_id = new_trace_id()
        strategy = extraction_strategy_for_source(
            source_profile=source_profile,
            base_prompt=prompt,
        )
        checkpoint_segments: list[IngestionSegment] = []
        try:
            # status は cancel を確かめてから書く。取り消し後に中間の status を書き戻さない(#305)。
            await _raise_if_cancelled(cancel_checker)
            if manage_document_state:
                if prepared_artifact is None:
                    await self._oracle.update_document_status(document_id, FileStatus.PREPROCESSING)
                else:
                    await self._oracle.update_document_status(document_id, FileStatus.INGESTING)
            if prepared_artifact is None:
                # 前処理(Preprocess): parse の前に原本を一度だけ canonical な中間物へ変換し、
                # 派生系譜(SourceDerivation)を残す。passthrough(既定)は原本そのまま。
                parse_bytes, parse_content_type, source_derivation = await self._preprocess_source(
                    trace_id=trace_id,
                    document_id=document_id,
                    source_bytes=image_bytes,
                    content_type=content_type,
                    source_profile=source_profile,
                )
                # ファイル準備中に取り消されたら、artifact と後続の status を書かない。
                await _raise_if_cancelled(cancel_checker)
                if manage_document_state or self._recipe_id is not None:
                    # 文書レシピは legacy 文書列を上書きせず、レシピ固有 artifact を保存する。
                    await self._save_preprocess_artifact(
                        document_id=document_id,
                        source_derivation=source_derivation,
                        original_file_name=(
                            source_profile.original_file_name
                            if source_profile is not None
                            else "document"
                        ),
                        original_object_storage_path=original_object_storage_path,
                        fallback_content_type=parse_content_type,
                    )
                if (
                    manage_document_state or self._recipe_id is not None
                ) and not self._settings.rag_auto_parse_after_preprocess_enabled:
                    # ファイル準備ゲート: PREPROCESSED で停止し、人の承認(または KB/全体
                    # 設定で自動進行)で EXTRACT ジョブを別途投入して parse へ進む。
                    # candidate モードはゲートを無視して必ず索引まで進める。
                    if self._recipe_id is not None:
                        await self._oracle.update_document_recipe_status(
                            recipe_id=self._recipe_id,
                            status=FileStatus.PREPROCESSED,
                        )
                        detail = await self._oracle.get_document(document_id)
                        if detail is None:
                            raise IngestionUserError("ドキュメントが見つかりません。")
                    else:
                        detail = await self._oracle.update_document_status(
                            document_id, FileStatus.PREPROCESSED
                        )
                    record_ingestion("preprocessed", 0)
                    return detail
                await _raise_if_cancelled(cancel_checker)
                if self._recipe_id is not None:
                    await self._oracle.update_document_recipe_status(
                        recipe_id=self._recipe_id,
                        status=FileStatus.INGESTING,
                    )
                elif manage_document_state:
                    await self._oracle.update_document_status(document_id, FileStatus.INGESTING)
            else:
                parse_bytes = image_bytes
                parse_content_type = prepared_artifact.content_type or content_type
                source_derivation = _source_derivation_from_preprocess_artifact(prepared_artifact)
            # 旧形式・マクロ付きの Office をファイル準備で変換したら、派生物を原本の形式で
            # 止めない(#2470)。
            source_profile = converted_source_profile(
                source_profile, converted=source_derivation.converted
            )
            await _raise_if_cancelled(cancel_checker)
            progress = active_ingestion_progress()
            partition_attributes: dict[str, object] = {
                "content_type": _observability_content_type(parse_content_type),
                "source_modality": (
                    source_profile.modality.value if source_profile is not None else "unknown"
                ),
                "parser_profile": strategy.parser_profile,
                "preprocess_profile": source_derivation.preprocess_profile,
                "preprocess_converted": source_derivation.converted,
            }
            # PDF は段(既定 10 ページ)ごとに解析サービスを呼び、段ごとに状態・進捗・抽出結果を
            # 残す。失敗・中断の後は成功した段を使い、ほかの段だけを解析し直す(#2001)。
            segment_plan = await self._pdf_segment_parse_plan(
                parse_bytes=parse_bytes,
                content_type=parse_content_type,
                source_profile=source_profile,
            )
            segmented_parse = segment_plan is not None
            if segment_plan is not None:
                segment_backend, pdf_segments = segment_plan
                checkpoint_segments = await self._prepare_pdf_parse_segments(
                    document_id=document_id,
                    parser_backend=segment_backend,
                    pdf_segments=pdf_segments,
                    progress=progress,
                )
                prefetcher = await self._start_vision_prefetch(
                    trace_id=trace_id,
                    document_id=document_id,
                    checkpoint_segments=checkpoint_segments,
                    source_bytes=parse_bytes,
                    content_type=parse_content_type,
                    file_name=(
                        source_profile.sanitized_file_name if source_profile is not None else ""
                    ),
                    parser_backend=segment_backend,
                    progress=progress,
                    cancel_checker=cancel_checker,
                )
                try:
                    parser_result, checkpoint_segments = await self._parse_pdf_segments(
                        trace_id=trace_id,
                        document_id=document_id,
                        parser_backend=segment_backend,
                        pdf_segments=pdf_segments,
                        checkpoint_segments=checkpoint_segments,
                        source_profile=source_profile,
                        content_type=parse_content_type,
                        attributes=partition_attributes,
                        progress=progress,
                        cancel_checker=cancel_checker,
                        on_segment_parsed=prefetcher.submit if prefetcher is not None else None,
                    )
                except BaseException:
                    if prefetcher is not None:
                        await prefetcher.stop()
                    raise
                if prefetcher is not None:
                    # 解析の後に残った段の先読みを待つ(全体の読み取りがその応答を使う)。
                    await prefetcher.finish()
            else:
                # 前の試行の段の記録(待機中のまま残ったもの)は、原本全体の解析の間は処理中に
                # する(解析の bar と段の状態を食い違わせない。#2001)。
                await self._mark_unfinished_segments_running(document_id)
                parser_result = await self._partition_whole_source(
                    trace_id=trace_id,
                    parse_bytes=parse_bytes,
                    source_profile=source_profile,
                    content_type=parse_content_type,
                    attributes=partition_attributes,
                    progress=progress,
                )
            if parser_result.unsupported_reason:
                raise IngestionUserError(_unsupported_parser_message(parser_result))
            self._raise_if_selected_parser_was_not_used(parser_result)
            effective_parser_profile = _checkpoint_parser_profile(
                parser_result,
                fallback=strategy.parser_profile,
            )
            if not segmented_parse:
                checkpoint_segments = await self._prepare_segment_checkpoints(
                    document_id=document_id,
                    source_bytes=parse_bytes,
                    content_type=parse_content_type,
                    source_profile=source_profile,
                    parser_backend=_checkpoint_parser_backend(parser_result, source_profile),
                    parser_profile=effective_parser_profile,
                    recipe_id=self._recipe_id,
                )
            await _raise_if_cancelled(cancel_checker)
            extraction: StructuredExtraction | None = None
            try:
                if not segmented_parse:
                    # 後段失敗からの再開: cached full extraction artifact があれば再利用して復旧する
                    # (local fallback ではなく resume 最適化)。段ごとの解析(#2001)は段の artifact を
                    # 解析の前に読み戻すため、ここは原本全体を 1 回で解析する経路だけ。
                    # ponytail: 再利用でも解析サービスは _partition_source で先に呼ばれる。再開時の
                    # 再解析を完全に避けたい場合は cache 判定を parse 前へ移す。V1 では許容する。
                    cached_extraction = await self._load_cached_full_extraction(
                        checkpoint_segments, parse_key=self._segment_parse_key(parse_bytes)
                    )
                    if cached_extraction is not None:
                        extraction = cached_extraction
                if extraction is not None:
                    pass
                elif parser_result.extraction is not None:
                    # 既定 docling を含む parser マイクロサービスの抽出をそのまま使う。
                    extraction = parser_result.extraction
                else:
                    # local fallback / Office セグメント / 暗黙 VLM-OCR は持たない。
                    # 防御的に fail-fast する。
                    raise IngestionUserError(
                        _unsupported_parser_message(parser_result)
                        if parser_result.unsupported_reason
                        else (
                            "選択した解析エンジンで抽出できませんでした。"
                            "別経路へ縮退せず取込を停止しました。"
                            "解析サービスの稼働と設定を確認してから再実行してください。"
                        )
                    )
            except EnterpriseAiTimeoutError as exc:
                raise IngestionTimeoutError(str(exc)) from exc
            except EnterpriseAiIncompleteResponseError as exc:
                raise IngestionUserError(str(exc)) from exc
            # 上の分岐はいずれかで必ず extraction を確定させる。
            assert extraction is not None
            if progress is not None:
                # 解析の段の完了。ページ数は解析の結果で補う(進捗を返さない解析経路でも出す)。
                progress.finish_parse(
                    engine=parser_result.parser_backend,
                    page_count=len(extraction.pages) or None,
                )
            await _raise_if_cancelled(cancel_checker)
            extraction = _extraction_with_parser_context(
                extraction,
                parser_result=parser_result,
                fallback_template=template_for_source_profile(source_profile),
                source_parser=effective_parser_profile,
            )
            # 派生系譜(溯源)を抽出 metadata へ刻む。artifact cache / document payload 経由で永続。
            extraction = _extraction_with_source_derivation(extraction, source_derivation)
            # PDF の印刷の頁番号（ページラベル）を残し、chunk の場所に付ける（#1244）。
            # PDFium の呼び出しは lock の中で同期に行うため、event loop を止めないよう
            # 別のスレッドで読む（#3362）。
            extraction = await asyncio.to_thread(
                _extraction_with_page_labels, extraction, parse_bytes, parse_content_type
            )
            # 図・画像の読み取り(Vision)は解析エンジンに関係なくここで行う(#497)。抽出の cache より
            # 前に行い、後段の失敗から再開しても読み取り済みの図を読み直さない。
            extraction = await self._attach_vision(
                trace_id,
                extraction,
                source_bytes=parse_bytes,
                content_type=parse_content_type,
                file_name=(
                    source_profile.sanitized_file_name if source_profile is not None else ""
                ),
                parser_backend=parser_result.parser_backend,
                cancel_checker=cancel_checker,
            )
            # 文書全体の整理(しおりの見出しの深さ・ページの境目で切れた段落・表の続き。#2001)。
            # 段ごとの解析をまとめた結果にも原本全体の解析の結果にも同じ規則で当てる。Vision の
            # 書き戻し(表の拡張 HTML)の後に当て、Vision が表の続きの印を消さないようにする。
            if _observability_content_type(parse_content_type) == "application/pdf":
                extraction = await asyncio.to_thread(
                    consolidate_document_extraction,
                    extraction,
                    source_bytes=parse_bytes,
                    geometry=ContinuationGeometry.from_settings(self._settings),
                )
            # 解析の区間(解析サービス・図表の読み取り・文書全体の整理)はここまで。この後の見出しの
            # 階層の推定・項目抽出は LLM の応答待ちが主のため、worker に重い段の枠を返し、全体の枠
            # だけで待つ(LLM の並行は段ごとの実行器の上限で絞る。#2702)。
            release_heavy_ingestion_slot()
            # 見出しの階層の推定(任意。#2277)。原本全体の整理の後、章節の木・項目抽出・chunk の前。
            extraction = await self._attach_heading_levels(trace_id, extraction)
            quality_report = build_ingestion_quality_report(
                extraction,
                source_profile=source_profile,
                parser_profile=effective_parser_profile,
                parser_backend=parser_result.parser_backend,
                parser_version=parser_result.parser_version,
                fallback_used=parser_result.fallback_used,
            )
            extraction = extraction.model_copy(update={"quality_report": quality_report})
            await _raise_if_cancelled(cancel_checker)
            extraction, extraction_artifact_path = await self._cache_extraction_artifact(
                document_id=document_id,
                trace_id=trace_id,
                extraction=extraction,
                parse_key=self._segment_parse_key(parse_bytes),
            )
            quality_report = build_ingestion_quality_report(
                extraction,
                source_profile=source_profile,
                parser_profile=effective_parser_profile,
                parser_backend=parser_result.parser_backend,
                parser_version=parser_result.parser_version,
                fallback_used=parser_result.fallback_used,
            )
            extraction = extraction.model_copy(update={"quality_report": quality_report})
            extraction = await self._attach_extraction_fields(
                trace_id, extraction, document_id=document_id
            )
            extraction = await self._attach_navigation_tree(
                trace_id, extraction, document_id=document_id
            )
            checkpoint_segments = await self._mark_segments_succeeded(
                checkpoint_segments,
                artifact_path=extraction_artifact_path,
            )
            text = _text_for_chunking(extraction)
            if not text:
                raise IngestionUserError("抽出可能なテキストが見つかりませんでした。")
            # 抽出の正本の保存(レシピは抽出 pointer も切り替える)と REVIEW の前に確かめる。
            await _raise_if_cancelled(cancel_checker)
            # extraction 層(rag_document_extractions)へ正本として書く(両ゲート共通)。
            extraction_recipe_id = await self._persist_extraction_layer(
                document_id, source_profile, extraction
            )
            if (manage_document_state or self._recipe_id is not None) and (
                self._settings.rag_review_gate_enabled or requires_review(quality_report)
            ):
                # REVIEW で停止する前に抽出本文を永続化し、プレビュー・後段 CHUNK で再利用する。
                # candidate モードは REVIEW で止めず索引まで進める。
                await _raise_if_cancelled(cancel_checker)
                if self._recipe_id is not None:
                    await self._oracle.update_document_recipe_status(
                        recipe_id=self._recipe_id,
                        status=FileStatus.REVIEW,
                        active_extraction_recipe_id=extraction_recipe_id,
                    )
                    detail = await self._oracle.get_document(document_id)
                    if detail is None:
                        raise IngestionUserError("ドキュメントが見つかりません。")
                else:
                    await self._oracle.save_extraction(document_id, extraction)
                    detail = await self._oracle.update_document_status(
                        document_id, FileStatus.REVIEW
                    )
                record_ingestion("review", 0)
                return detail
            return await self._run_index_phase(
                trace_id=trace_id,
                document_id=document_id,
                extraction=extraction,
                quality_report=quality_report,
                parser_profile=strategy.parser_profile,
                checkpoint_segments=checkpoint_segments,
                source_bytes=image_bytes,
                started_at=started_at,
                chunk_set_id=chunk_set_id,
                record_outcome=record_outcome,
                manage_document_state=manage_document_state,
                cancel_checker=cancel_checker,
                document_title=(
                    source_profile.original_file_name if source_profile is not None else ""
                ),
                source_sha256=source_sha256,
                source_size=source_size,
            )
        except IngestionCancelledError as exc:
            await self._mark_segments_cancelled(checkpoint_segments)
            record_ingestion("cancelled", 0)
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=image_bytes,
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise
        except Exception as exc:
            record_ingestion("error", 0)
            await self._mark_segments_failed(checkpoint_segments, error=exc)
            if manage_document_state:
                # candidate モードは失敗しても配信中文書を ERROR にしない(ジョブ側で失敗記録)。
                await self._oracle.update_document_status(
                    document_id,
                    FileStatus.ERROR,
                    _safe_persistent_error_message(exc),
                )
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=image_bytes,
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise

    async def _persist_extraction_layer(
        self,
        document_id: str,
        source_profile: SourceProfile | None,
        extraction: StructuredExtraction,
    ) -> str | None:
        """抽出を extraction 層(rag_document_extractions)へ正本として upsert する。

        extraction_recipe_id = hash(source_sha256, preprocess, parser)。chunk_set の親キーで、
        index 時は同じキーで読み戻す(単一 materialization では owning 抽出と一致)。
        recipe job はレシピ固有 ID に保存してから pointer を切り替える。legacy 経路だけは
        書込失敗時に従来どおり取込を継続する。
        """
        source_sha256 = source_profile.content_sha256 if source_profile is not None else None
        if not source_sha256:
            if self._recipe_id is not None:
                raise IngestionUserError("抽出成果物のソースハッシュが未確定です。")
            return None
        try:
            preprocess_profile = _source_derivation_profile(extraction)
            recipe_settings = (
                self._settings.model_copy(update={"rag_preprocess_profile": preprocess_profile})
                if preprocess_profile
                else self._settings
            )
            extraction_recipe_id = compute_extraction_recipe_id(source_sha256, recipe_settings)
            if self._recipe_id is not None:
                if self._recipe_revision is None:
                    raise IngestionUserError("レシピ revision が未確定です。")
                extraction_recipe_id = compute_document_recipe_extraction_id(
                    extraction_recipe_id,
                    self._recipe_id,
                    self._recipe_revision,
                )
            recipe = extraction_recipe_subset(recipe_settings)
            await self._oracle.upsert_document_extraction_artifact(
                document_id=document_id,
                extraction_recipe_id=extraction_recipe_id,
                source_sha256=source_sha256,
                recipe_subset=recipe,
                extraction=extraction.to_document_payload(),
                status="materialized",
            )
            if self._recipe_id is not None:
                await self._oracle.update_document_recipe_status(
                    recipe_id=self._recipe_id,
                    status=FileStatus.INGESTING,
                    active_extraction_recipe_id=extraction_recipe_id,
                )
            return extraction_recipe_id
        except Exception:
            if self._recipe_id is not None:
                raise
            logger.warning(
                "extraction 層への書込に失敗(取込は継続)。document_id=%s",
                document_id,
                exc_info=True,
            )
            return None

    async def _load_reviewed_extraction(self, detail: DocumentDetail) -> StructuredExtraction:
        """承認済み文書の抽出を extraction 層(正本)から読む。無ければ legacy 列へ縮退。

        index 時の設定から extraction_recipe_id を再計算して新表を引く。単一 materialization では
        extract 時の owning 抽出と一致。読み失敗・未登録は detail.extraction(legacy)へ縮退。
        """
        if self._recipe_id is not None:
            recipe = await self._oracle.get_document_recipe(detail.id, self._recipe_id)
            extraction_recipe_id = (
                str(recipe.get("active_extraction_recipe_id"))
                if recipe is not None and recipe.get("active_extraction_recipe_id")
                else None
            )
            if extraction_recipe_id is None:
                raise IngestionUserError("索引対象の抽出結果が見つかりません。")
            row = await self._oracle.get_document_extraction_artifact(
                document_id=detail.id,
                extraction_recipe_id=extraction_recipe_id,
            )
            payload = _coerce_extraction_payload(row.get("extraction_json")) if row else None
            if payload is None:
                raise IngestionUserError("索引対象の抽出結果が見つかりません。")
            return _validate_structured_extraction_payload(payload)

        source_sha256 = detail.content_sha256
        if source_sha256:
            try:
                extraction_recipe_id = compute_extraction_recipe_id(source_sha256, self._settings)
                row = await self._oracle.get_document_extraction_artifact(
                    document_id=detail.id,
                    extraction_recipe_id=extraction_recipe_id,
                )
            except Exception:  # noqa: BLE001 — 縮退して legacy 列から読む。
                logger.warning("extraction 層の読込に失敗、legacy へ縮退。", exc_info=True)
                row = None
            payload = (
                _coerce_extraction_payload(row.get("extraction_json")) if row else None
            ) or _coerce_extraction_payload(detail.extraction)
            if payload:
                return _validate_structured_extraction_payload(payload)
        if not detail.extraction:
            raise IngestionUserError("索引対象の抽出結果が見つかりません。")
        return _validate_structured_extraction_payload(detail.extraction)

    async def index_reviewed(
        self,
        document_id: str,
        *,
        chunk_set_id: str | None = None,
        record_outcome: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> DocumentDetail:
        """保存済み抽出から chunk→embed→index を一括実行する legacy 後段。

        前段(parse/抽出)は再実行せず、保存済み抽出本文を再利用する。
        review gate 無効時や旧互換の複数 chunk_set materialization で使う。
        """
        self.deferred_success_outcome = None
        started_at = now()
        trace_id = new_trace_id()
        detail = await self._oracle.get_document(document_id)
        if detail is None:
            raise IngestionUserError("ドキュメントが見つかりません。")
        # 原本 bytes は読まないため、監査には文書に記録済みの原本の hash とサイズを使う(#504)。
        source_sha256 = detail.content_sha256
        source_size = detail.file_size_bytes
        extraction = await self._load_reviewed_extraction(detail)
        quality_report = extraction.quality_report or build_ingestion_quality_report(extraction)
        # 開始時の status も cancel を確かめてから書く(#305)。
        await _raise_if_cancelled(cancel_checker)
        if self._recipe_id is not None:
            await self._oracle.update_document_recipe_status(
                recipe_id=self._recipe_id,
                status=FileStatus.INDEXING,
            )
        else:
            await self._oracle.update_document_status(document_id, FileStatus.INDEXING)
        checkpoint_segments = await self._safe_list_ingestion_segments(document_id)
        try:
            return await self._run_index_phase(
                trace_id=trace_id,
                document_id=document_id,
                extraction=extraction,
                quality_report=quality_report,
                parser_profile=quality_report.parser_profile,
                checkpoint_segments=checkpoint_segments,
                source_bytes=b"",
                started_at=started_at,
                chunk_set_id=chunk_set_id,
                record_outcome=record_outcome,
                manage_document_state=self._recipe_id is None,
                cancel_checker=cancel_checker,
                document_title=detail.file_name,
                source_sha256=source_sha256,
                source_size=source_size,
            )
        except IngestionCancelledError as exc:
            record_ingestion("cancelled", 0)
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=b"",
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise
        except Exception as exc:
            record_ingestion("error", 0)
            if self._recipe_id is None:
                await self._oracle.update_document_status(
                    document_id,
                    FileStatus.ERROR,
                    _safe_persistent_error_message(exc),
                )
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=b"",
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise

    async def chunk_reviewed(
        self,
        document_id: str,
        *,
        chunk_set_id: str | None = None,
        record_outcome: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> DocumentDetail:
        """REVIEW で承認済みの抽出から chunk だけを作り、CHUNKED で停止する。"""
        self.deferred_success_outcome = None
        started_at = now()
        trace_id = new_trace_id()
        detail = await self._oracle.get_document(document_id)
        if detail is None:
            raise IngestionUserError("ドキュメントが見つかりません。")
        # 原本 bytes は読まないため、監査には文書に記録済みの原本の hash とサイズを使う(#504)。
        source_sha256 = detail.content_sha256
        source_size = detail.file_size_bytes
        extraction = await self._load_reviewed_extraction(detail)
        quality_report = extraction.quality_report or build_ingestion_quality_report(extraction)
        # 開始時の status も cancel を確かめてから書く(#305)。
        await _raise_if_cancelled(cancel_checker)
        if self._recipe_id is not None:
            await self._oracle.update_document_recipe_status(
                recipe_id=self._recipe_id,
                status=FileStatus.CHUNKING,
            )
        else:
            await self._oracle.update_document_status(document_id, FileStatus.CHUNKING)
        checkpoint_segments = await self._safe_list_ingestion_segments(document_id)
        try:
            chunks = await self._build_chunks_for_extraction(
                trace_id=trace_id,
                extraction=extraction,
                quality_report=quality_report,
                parser_profile=quality_report.parser_profile,
                cancel_checker=cancel_checker,
                source_name=detail.file_name,
            )
            chunks = _chunks_with_context_headers(
                chunks,
                detail.file_name,
                enabled=self._settings.rag_chunk_context_header_enabled,
            )
            await _raise_if_cancelled(cancel_checker)
            await _observe_ingestion_stage(
                trace_id,
                "chunk_save",
                self._oracle.save_chunk_preview(
                    document_id,
                    extraction,
                    chunks,
                    chunk_set_id=chunk_set_id,
                ),
                attributes={"chunk_count": len(chunks)},
            )
            await _raise_if_cancelled(cancel_checker)
            if self._recipe_id is not None:
                await self._oracle.update_document_recipe_status(
                    recipe_id=self._recipe_id,
                    status=FileStatus.CHUNKED,
                )
                detail = await self._oracle.get_document(document_id)
                if detail is None:
                    raise IngestionUserError("ドキュメントが見つかりません。")
            else:
                detail = await self._oracle.update_document_status(document_id, FileStatus.CHUNKED)
            if record_outcome:
                record_ingestion("review", len(chunks))
                record_rag_ingestion_audit(
                    trace_id=trace_id,
                    document_id=document_id,
                    outcome="success",
                    source_bytes=b"",
                    source_sha256=source_sha256,
                    source_size=source_size,
                    document_type=_observability_document_type(extraction.document_type),
                    extraction_confidence=extraction.confidence,
                    parser_backend=quality_report.parser_backend,
                    parser_profile=quality_report.parser_profile,
                    segment_count=len(checkpoint_segments),
                    fallback_count=1 if quality_report.fallback_used else 0,
                    failed_segment_count=quality_report.failed_segment_count,
                    chunk_count=len(chunks),
                    vector_count=0,
                    elapsed_ms=elapsed_ms(started_at),
                )
            return detail
        except IngestionCancelledError as exc:
            record_ingestion("cancelled", 0)
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=b"",
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise
        except Exception as exc:
            record_ingestion("error", 0)
            if self._recipe_id is None:
                await self._oracle.update_document_status(
                    document_id,
                    FileStatus.ERROR,
                    _safe_persistent_error_message(exc),
                )
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=b"",
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise

    async def index_chunked(
        self,
        document_id: str,
        *,
        chunk_set_id: str,
        record_outcome: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> DocumentDetail:
        """CHUNKED の chunk_set から embedding→index だけを実行する。"""
        self.deferred_success_outcome = None
        started_at = now()
        trace_id = new_trace_id()
        detail = await self._oracle.get_document(document_id)
        if detail is None:
            raise IngestionUserError("ドキュメントが見つかりません。")
        # 原本 bytes は読まないため、監査には文書に記録済みの原本の hash とサイズを使う(#504)。
        source_sha256 = detail.content_sha256
        source_size = detail.file_size_bytes
        extraction = await self._load_reviewed_extraction(detail)
        quality_report = extraction.quality_report or build_ingestion_quality_report(extraction)
        # 開始時の status も cancel を確かめてから書く(#305)。
        await _raise_if_cancelled(cancel_checker)
        if self._recipe_id is not None:
            await self._oracle.update_document_recipe_status(
                recipe_id=self._recipe_id,
                status=FileStatus.INDEXING,
            )
        else:
            await self._oracle.update_document_status(document_id, FileStatus.INDEXING)
        checkpoint_segments = await self._safe_list_ingestion_segments(document_id)
        try:
            chunk_views = await self._oracle.list_chunk_set_chunks(chunk_set_id)
            chunks = [_chunk_view_to_chunk(view) for view in chunk_views]
            if not chunks:
                raise IngestionUserError("索引対象のチャンクが見つかりません。")
            chunks = _chunks_with_context_headers(
                chunks,
                detail.file_name,
                enabled=self._settings.rag_chunk_context_header_enabled,
            )
            return await self._run_embedding_index_phase(
                trace_id=trace_id,
                document_id=document_id,
                extraction=extraction,
                quality_report=quality_report,
                chunks=chunks,
                checkpoint_segments=checkpoint_segments,
                source_bytes=b"",
                started_at=started_at,
                chunk_set_id=chunk_set_id,
                record_outcome=record_outcome,
                manage_document_state=self._recipe_id is None,
                cancel_checker=cancel_checker,
                reuse_saved_chunks=True,
                source_sha256=source_sha256,
                source_size=source_size,
            )
        except IngestionCancelledError as exc:
            record_ingestion("cancelled", 0)
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=b"",
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise
        except Exception as exc:
            record_ingestion("error", 0)
            if self._recipe_id is None:
                await self._oracle.update_document_status(
                    document_id,
                    FileStatus.ERROR,
                    _safe_persistent_error_message(exc),
                )
            record_rag_ingestion_audit(
                trace_id=trace_id,
                document_id=document_id,
                outcome="error",
                source_bytes=b"",
                segment_count=len(checkpoint_segments),
                failed_segment_count=_failed_checkpoint_count(checkpoint_segments),
                elapsed_ms=elapsed_ms(started_at),
                error=exc,
                source_sha256=source_sha256,
                source_size=source_size,
            )
            raise

    async def _build_chunks_for_extraction(
        self,
        *,
        trace_id: str,
        extraction: StructuredExtraction,
        quality_report: IngestionQualityReport,
        parser_profile: str,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
        source_name: str = "",
    ) -> list[Chunk]:
        """抽出結果から chunk を作る。embedding/index は行わない。"""
        text = _text_for_chunking(extraction)
        if not text:
            raise IngestionUserError("抽出可能なテキストが見つかりませんでした。")
        await _raise_if_cancelled(cancel_checker)
        chunking_params = resolve_chunking_params(self._settings)
        # 親子階層（small-to-big）は、どの解析の結果でも親子に分割する(#3377)。親子を作った入力
        # (Docling のレイアウト・行の記録・抽出の構造)は trace に残す。
        is_small_to_big = chunking_params.strategy == SMALL_TO_BIG_STRATEGY

        def _run_chunking() -> list[Chunk]:
            diagnostics: dict[str, int] = {}
            try:
                if is_small_to_big:
                    result = build_parent_child_chunks(
                        extraction,
                        source_name=source_name,
                        params=chunking_params.small_to_big,
                        diagnostics=diagnostics,
                    )
                else:
                    result = chunk_extraction_with_strategy(
                        extraction,
                        strategy=chunking_params.strategy,
                        chunk_size=chunking_params.chunk_size,
                        overlap=chunking_params.overlap,
                        min_chars=chunking_params.min_chars,
                        delimiter=chunking_params.delimiter,
                    )
            except RecordBoundaryError as exc:
                raise IngestionUserError(record_boundary_failure_message(exc)) from exc
            if not result:
                # 空の結果は分割段階の失敗として記録し、本文を含めず除去理由の件数を示す。
                boilerplate_count = diagnostics.get("excluded_page_boilerplate_count", 0)
                details = (
                    f"ページ定型文の除外: {boilerplate_count} 件、"
                    f"装飾の除外: {diagnostics.get('excluded_decorative_count', 0)} 件、"
                    f"空の要素: {diagnostics.get('empty_record_count', 0)} 件。"
                    if diagnostics
                    else "分割と除外処理の後に索引対象が残りませんでした。"
                )
                raise IngestionUserError(
                    f"索引用チャンクを作成できませんでした（抽出文字数: {len(text)}）。"
                    + details
                    + "解析結果と文書の分割設定を確認してください。"
                )
            return result

        small_to_big_attributes: dict[str, int] = (
            {
                "chunk_child_target_chars": chunking_params.small_to_big.child_target_chars,
                "chunk_table_child_target_chars": (
                    chunking_params.small_to_big.table_child_target_chars
                ),
                "chunk_parent_target_chars": chunking_params.small_to_big.parent_target_chars,
                "chunk_parent_max_pages": chunking_params.small_to_big.parent_max_pages,
                "chunk_parent_max_children": chunking_params.small_to_big.parent_max_children,
            }
            if is_small_to_big
            else {}
        )
        parent_source_attributes: dict[str, str] = (
            {"chunk_parent_source": small_to_big_parent_source(extraction)}
            if is_small_to_big
            else {}
        )
        chunks = await _observe_cpu_ingestion_stage(
            trace_id,
            "chunking",
            _run_chunking,
            attributes={
                "chunk_profile": "structure_v1",
                "chunk_strategy": chunking_params.strategy,
                "effective_chunk_strategy": chunking_params.strategy,
                **parent_source_attributes,
                "chunk_size": chunking_params.chunk_size,
                "chunk_overlap": chunking_params.overlap,
                "chunk_min_chars": chunking_params.min_chars,
                **small_to_big_attributes,
                "input_chars": len(text),
                "chunk_count": 0,
                "parser_profile": parser_profile,
                "parser_backend": quality_report.parser_backend,
                "fallback_used": quality_report.fallback_used,
                "quality_risk_level": quality_report.risk_level,
                **_extraction_structure_attributes(extraction),
            },
            result_attributes=lambda result: {"chunk_count": len(result)},
        )
        # RAPTOR 再帰要約索引(opt-in)。leaf に summary node を足して索引する。要約失敗は leaf のみ。
        chunks = await self._augment_with_raptor(trace_id, chunks, cancel_checker)
        apply_page_labels(chunks, labels_from_artifacts(extraction.parser_artifacts))
        # 本文の交差参照(「第3章を参照」など)を同じ文書の節に解決して metadata に残す(#1280)。
        annotate_cross_references(chunks, document_title=source_name)
        # 分割に使った言語資源の取込の項目の hash(#2181)。言語資源を変えたあと、分割し直しが
        # 要る文書を数える。
        language_hash = current_pack().ingest_hash
        for chunk in chunks:
            chunk.metadata[LANGUAGE_PACK_HASH_KEY] = language_hash
        return _chunks_with_source_derivation(chunks, _source_derivation_id(extraction))

    async def _run_index_phase(
        self,
        *,
        trace_id: str,
        document_id: str,
        extraction: StructuredExtraction,
        quality_report: IngestionQualityReport,
        parser_profile: str,
        checkpoint_segments: list[IngestionSegment],
        source_bytes: bytes,
        started_at: float,
        chunk_set_id: str | None = None,
        record_outcome: bool = True,
        manage_document_state: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
        document_title: str = "",
        source_sha256: str | None = None,
        source_size: int | None = None,
    ) -> DocumentDetail:
        """抽出結果から chunk→embed→index を実行し INDEXED まで進める後段。

        ``record_outcome=False`` のときは成功 metric / audit を出さない(複数 chunk_set を
        materialize する loop で、1 文書 1 論理取込として記録を 1 回に集約するため)。
        記録しなかった成功の内容は ``deferred_success_outcome`` に残る。
        ``manage_document_state=False``(candidate モード)では文書 status を INDEXED へ
        遷移させない(配信中文書を乱さない)。

        例外はそのまま呼び出し側(ingest / index_reviewed)の except へ伝播させる。
        """
        chunks = await self._build_chunks_for_extraction(
            trace_id=trace_id,
            extraction=extraction,
            quality_report=quality_report,
            parser_profile=parser_profile,
            cancel_checker=cancel_checker,
            source_name=document_title,
        )
        chunks = _chunks_with_context_headers(
            chunks,
            document_title,
            enabled=self._settings.rag_chunk_context_header_enabled,
        )
        return await self._run_embedding_index_phase(
            trace_id=trace_id,
            document_id=document_id,
            extraction=extraction,
            quality_report=quality_report,
            chunks=chunks,
            checkpoint_segments=checkpoint_segments,
            source_bytes=source_bytes,
            started_at=started_at,
            chunk_set_id=chunk_set_id,
            record_outcome=record_outcome,
            manage_document_state=manage_document_state,
            cancel_checker=cancel_checker,
            reuse_saved_chunks=False,
            source_sha256=source_sha256,
            source_size=source_size,
        )

    def _chunk_embedding_inputs(self, chunks: list[Chunk]) -> list[str]:
        """embedding へ渡す入力テキストを作る(Contextual chunk header の決定論版)。

        有効時は「文書名 > section_path」を各 chunk の先頭へ前置して検索文脈を持たせる。
        保存される chunk 本文・引用表示は変えず、chunk metadata の context_header を使う。
        """
        if not self._settings.rag_chunk_context_header_enabled:
            return [
                engine_search_text(chunk.metadata, include_context_header=False) or chunk.text
                for chunk in chunks
            ]
        return [
            engine_search_text(chunk.metadata) or _embedding_input_with_context_header(chunk)
            for chunk in chunks
        ]

    async def _run_embedding_index_phase(
        self,
        *,
        trace_id: str,
        document_id: str,
        extraction: StructuredExtraction,
        quality_report: IngestionQualityReport,
        chunks: list[Chunk],
        checkpoint_segments: list[IngestionSegment],
        source_bytes: bytes,
        started_at: float,
        chunk_set_id: str | None = None,
        record_outcome: bool = True,
        manage_document_state: bool = True,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
        reuse_saved_chunks: bool,
        source_sha256: str | None = None,
        source_size: int | None = None,
    ) -> DocumentDetail:
        """chunk を embedding し、Oracle index として検索可能にする。

        ``source_sha256`` / ``source_size`` は、``source_bytes`` を持たない工程が成功の監査に
        原本の hash とサイズを残すために渡す。
        ``record_outcome=False`` のときは成功の metric / 監査を記録せず、その内容を
        ``deferred_success_outcome`` に残す。レシピの job は active への切り替えまで終え、
        job を SUCCEEDED にできた後にそれを記録する(#504 / #514)。
        """
        await _raise_if_cancelled(cancel_checker)
        embed_inputs = self._chunk_embedding_inputs(chunks)
        vectors = await _observe_ingestion_stage(
            trace_id,
            "embedding",
            self._genai.embed(embed_inputs),
            attributes={
                "model": self._settings.genai_embedding_model,
                "input_count": len(chunks),
                "context_header_enabled": self._settings.rag_chunk_context_header_enabled,
            },
            result_attributes=lambda result: {"vector_count": len(result)},
        )
        await _raise_if_cancelled(cancel_checker)
        if reuse_saved_chunks and chunk_set_id is not None:
            await _observe_ingestion_stage(
                trace_id,
                "indexing",
                self._save_embeddings_for_chunk_set(
                    trace_id,
                    document_id,
                    extraction,
                    chunks,
                    vectors,
                    chunk_set_id=chunk_set_id,
                    cancel_checker=cancel_checker,
                ),
                attributes={
                    "chunk_count": len(chunks),
                    "vector_count": len(vectors),
                },
            )
        else:
            await _observe_ingestion_stage(
                trace_id,
                "indexing",
                self._save_index(
                    trace_id,
                    document_id,
                    extraction,
                    chunks,
                    vectors,
                    chunk_set_id=chunk_set_id,
                    cancel_checker=cancel_checker,
                ),
                attributes={
                    "chunk_count": len(chunks),
                    "vector_count": len(vectors),
                },
            )
        await _raise_if_cancelled(cancel_checker)
        if manage_document_state:
            detail = await self._oracle.update_document_status(document_id, FileStatus.INDEXED)
        else:
            # candidate モード: 配信中文書の status を変えない。現在の detail をそのまま返す。
            current = await self._oracle.get_document(document_id)
            if current is None:
                raise IngestionUserError("ドキュメントが見つかりません。")
            detail = current
        outcome = IngestionSuccessOutcome(
            trace_id=trace_id,
            document_id=document_id,
            started_at=started_at,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            source_size=source_size,
            document_type=_observability_document_type(extraction.document_type),
            extraction_confidence=extraction.confidence,
            parser_backend=quality_report.parser_backend,
            parser_profile=quality_report.parser_profile,
            segment_count=len(checkpoint_segments),
            fallback_count=1 if quality_report.fallback_used else 0,
            failed_segment_count=quality_report.failed_segment_count,
            chunk_count=len(chunks),
            vector_count=len(vectors),
        )
        if record_outcome:
            outcome.record()
        else:
            self.deferred_success_outcome = outcome
        return detail

    async def _preprocess_source(
        self,
        *,
        trace_id: str,
        document_id: str,
        source_bytes: bytes,
        content_type: str,
        source_profile: SourceProfile | None,
    ) -> tuple[bytes, str, SourceDerivation]:
        """parse の前に原本を canonical な中間物へ変換し、(canonical bytes, content_type,
        派生系譜)を返す。

        passthrough(既定)は変換せず原本をそのまま返す(現行挙動と一致)。それ以外は
        in-process(text_normalize)または前処理マイクロサービスへ委譲し、変換物を
        保存先へ保存して派生系譜(SourceDerivation)に object path / sha を残す。
        選択済みの前処理マイクロサービスが失敗した場合は別経路へ切り替えない。
        """
        profile = resolve_preprocess_profile(self._settings)
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        if profile == "passthrough":
            return (
                source_bytes,
                content_type,
                _passthrough_derivation(
                    profile=profile, source_sha=source_sha, content_type=content_type
                ),
            )
        outcome: ConvertOutcome = await _observe_cpu_ingestion_stage(
            trace_id,
            "preprocess",
            lambda: self._preprocess.convert(
                source_bytes,
                content_type=content_type,
                source_profile=source_profile,
                profile=profile,
                options=preprocess_options(self._settings, profile),
            ),
            attributes={
                "preprocess_profile": profile,
                "content_type": _observability_content_type(content_type),
                "source_bytes": len(source_bytes),
            },
            result_attributes=lambda result: {
                "preprocess_converted": result.converted,
                "preprocess_converter": result.converter_name,
            },
        )
        if not outcome.converted or outcome.derived_bytes is None:
            return (
                source_bytes,
                content_type,
                _passthrough_derivation(
                    profile=profile,
                    source_sha=source_sha,
                    content_type=content_type,
                    converter_name=outcome.converter_name,
                    warnings=list(outcome.warnings),
                ),
            )
        derived_bytes = outcome.derived_bytes
        derived_content_type = outcome.derived_content_type or content_type
        derived_sha = hashlib.sha256(derived_bytes).hexdigest()
        derived_path = await self._cache_canonical_artifact(
            document_id=document_id,
            trace_id=trace_id,
            derived_bytes=derived_bytes,
            content_type=derived_content_type,
        )
        if derived_path is None:
            # 変換は成功したが処理後ファイルを保存できなかった。silent に path 欠落の
            # PREPROCESSED を作ると「処理後」プレビューも EXTRACT(承認して解析へ)も壊れるため、
            # ここで失敗を表面化して ERROR にし、人に再処理を促す(原因は warning ログ参照)。
            raise IngestionUserError(
                "ファイル準備の処理後ファイルを保存できませんでした。"
                "ストレージ設定を確認し、ファイル準備から再処理してください。"
            )
        derivation = SourceDerivation(
            derivation_id=uuid4().hex,
            preprocess_profile=profile,
            converted=True,
            converter_name=outcome.converter_name,
            converter_version=outcome.converter_version,
            source_content_type=content_type,
            source_sha256=source_sha,
            derived_object_path=derived_path,
            derived_content_type=derived_content_type,
            derived_sha256=derived_sha,
            page_map={str(key): int(value) for key, value in outcome.page_map.items()},
            warnings=list(outcome.warnings),
        )
        return derived_bytes, derived_content_type, derivation

    async def _save_preprocess_artifact(
        self,
        *,
        document_id: str,
        source_derivation: SourceDerivation,
        original_file_name: str,
        original_object_storage_path: str | None,
        fallback_content_type: str,
    ) -> DocumentPreprocessArtifact:
        content_type = (
            source_derivation.derived_content_type
            or source_derivation.source_content_type
            or fallback_content_type
            or "application/octet-stream"
        )
        artifact = DocumentPreprocessArtifact(
            derivation_id=source_derivation.derivation_id,
            profile=source_derivation.preprocess_profile,
            converted=source_derivation.converted,
            converter_name=source_derivation.converter_name,
            converter_version=source_derivation.converter_version,
            source_content_type=source_derivation.source_content_type,
            source_sha256=source_derivation.source_sha256,
            object_storage_path=(
                source_derivation.derived_object_path
                if source_derivation.converted
                else original_object_storage_path
            ),
            content_type=content_type,
            sha256=source_derivation.derived_sha256 or source_derivation.source_sha256,
            file_name=_prepared_artifact_file_name(original_file_name, content_type),
            page_map={str(key): int(value) for key, value in source_derivation.page_map.items()},
            warnings=list(source_derivation.warnings),
        )
        if self._recipe_id is not None:
            await self._oracle.update_document_recipe_status(
                recipe_id=self._recipe_id,
                status=FileStatus.PREPROCESSED,
                preprocess_artifact=artifact,
            )
            return artifact
        save_artifact = getattr(self._oracle, "save_preprocess_artifact", None)
        if not callable(save_artifact):
            return artifact
        await save_artifact(document_id, artifact)
        if artifact.object_storage_path:
            # 保存検証: ファイル準備 artifact(JSON 列)の書き込みが永続化したか読み戻して確認する。
            # 稀に commit 成功でも値が残らない事象があり、silent に path 欠落の PREPROCESSED を
            # 作ると「処理後」も EXTRACT も壊れる。残っていなければ失敗を表面化し再処理を促す。
            get_document = getattr(self._oracle, "get_document", None)
            if callable(get_document):
                saved = await get_document(document_id)
                if (
                    saved is None
                    or saved.preprocess_artifact is None
                    or not saved.preprocess_artifact.object_storage_path
                ):
                    logger.warning(
                        "preprocess_artifact_not_persisted",
                        extra={"document_id": document_id},
                    )
                    raise IngestionUserError(
                        "ファイル準備の処理後ファイル情報を保存できませんでした。"
                        "時間をおいてファイル準備から再処理してください。"
                    )
        return artifact

    async def _cache_canonical_artifact(
        self,
        *,
        document_id: str,
        trace_id: str,
        derived_bytes: bytes,
        content_type: str,
    ) -> str | None:
        """前処理で生成した正規化原本(canonical source)を 保存先へ保存する。

        変換物(処理後ファイル)は下流の抽出/プレビューに必須なため、抽出 JSON 用の
        rag_extraction_artifact_cache_enabled では握らず常に保存する。保存失敗は呼び出し側で
        致命扱いにし(silent な path 欠落の PREPROCESSED を作らない)、原因を warning に残す。
        """
        key_prefix = _safe_artifact_prefix(
            getattr(self._settings, "rag_canonical_artifact_prefix", "artifacts/canonical")
        )
        document_key = _safe_artifact_key_part(document_id)
        trace_key = _safe_artifact_key_part(trace_id)
        extension = _canonical_artifact_extension(content_type)
        key = f"{key_prefix}/{document_key}/{trace_key}/canonical{extension}"
        try:
            return await self._object_storage.put(
                key,
                derived_bytes,
                content_type=content_type or "application/octet-stream",
            )
        except Exception as exc:  # noqa: BLE001 - 失敗は呼び出し側で致命扱い。原因を残して None を返す
            logger.warning(
                "canonical_artifact_cache_failed",
                extra={
                    "document_id": document_id,
                    "key": key,
                    "error_type": type(exc).__name__,
                    **safe_exception_fields(exc),
                },
            )
            return None

    async def _prepare_segment_checkpoints(
        self,
        *,
        document_id: str,
        source_bytes: bytes,
        content_type: str,
        source_profile: SourceProfile | None,
        parser_backend: str,
        parser_profile: str,
        recipe_id: str | None = None,
    ) -> list[IngestionSegment]:
        """segment checkpoint を作成し、既存 failed segment があれば再試行対象にする。"""
        if not getattr(self._settings, "rag_segment_checkpoint_enabled", True):
            return []
        existing = await self._safe_list_ingestion_segments(document_id)
        failed = [segment for segment in existing if segment.status == "FAILED"]
        if failed:
            failed_ids = {segment.segment_id for segment in failed}
            for segment in failed:
                await self._safe_update_ingestion_segment(
                    segment.segment_id,
                    status="RUNNING",
                    error_code="retry_failed_segment",
                    error_message="失敗 segment のみ再試行します。",
                )
            return [
                (
                    segment.model_copy(
                        update={
                            "status": "RUNNING",
                            "error_code": "retry_failed_segment",
                        }
                    )
                    if segment.segment_id in failed_ids
                    else segment
                )
                for segment in existing
            ]
        if existing and all(segment.status == "SUCCEEDED" for segment in existing):
            return existing
        segments = await asyncio.to_thread(
            _checkpoint_segments_for_source,
            document_id=document_id,
            source_bytes=source_bytes,
            content_type=content_type,
            source_profile=source_profile,
            parser_backend=parser_backend,
            parser_profile=parser_profile,
            recipe_id=recipe_id,
            max_pages_per_segment=self._settings.rag_pdf_max_pages_per_segment,
            max_segments=self._settings.rag_pdf_max_segments,
            segmentation_enabled=self._settings.rag_pdf_segmentation_enabled,
        )
        # segment は解析サービスの応答の後に作る(通しジョブ内で原本全体を 1 回で解析する)。
        # 作った時点で解析は済み、残りの抽出(図・表の説明など)を全体で実行中のため、待機中では
        # なく処理中にする(#1999)。完了・失敗・取消は抽出の終わりにまとめて書く。
        segments = [segment.model_copy(update={"status": "RUNNING"}) for segment in segments]
        await self._safe_replace_ingestion_segments(document_id, segments)
        return segments

    async def _attach_heading_levels(
        self,
        trace_id: str,
        extraction: StructuredExtraction,
    ) -> StructuredExtraction:
        """しおりも番号も無い文書の見出しの階層をモデルの API で推定する(#2277)。

        `rag_heading_level_inference_enabled` が真で、対象の条件(`heading_levels`)に合う文書だけ
        LLM を 1 回呼ぶ。応答の形が合わない・呼び出しが失敗したときは今の階層のまま続ける
        (見出しの階層は補助で、取込は止めない)。
        """
        if not getattr(self._settings, "rag_heading_level_inference_enabled", False):
            return extraction
        plan = plan_heading_level_inference(
            extraction, HeadingLevelOptions.from_settings(self._settings)
        )
        if not plan.planned:
            return with_heading_level_report(extraction, plan, "skipped")

        prompt = await self._stage_prompt("heading_levels", HEADING_LEVEL_PROMPT)

        async def once(_: Attempt) -> str:
            with sdk_retries_disabled():
                return await async_call_with_model_purpose(
                    "document.infer_headings",
                    self._vlm.generate,
                    prompt,
                    plan.payload,
                    response_schema=HEADING_LEVELS_RESPONSE_SCHEMA,
                    response_schema_name="heading_levels",
                )

        try:
            with ingestion_ai_calls(self._settings, "heading_levels", total=1) as caller:
                raw = await caller.call(once, label="heading_levels")
        except Exception:  # noqa: BLE001 - 見出しの階層は補助。失敗は今の階層のまま続ける。
            logger.warning("heading_level_inference_failed", extra={"trace_id": trace_id})
            return with_heading_level_report(extraction, plan, "failed")
        levels = parse_heading_levels(raw, len(plan.record_ids))
        if levels is None:
            logger.warning("heading_level_inference_invalid", extra={"trace_id": trace_id})
            return with_heading_level_report(extraction, plan, "invalid_response")
        return apply_heading_levels(extraction, plan, levels)

    async def _attach_extraction_fields(
        self,
        trace_id: str,
        extraction: StructuredExtraction,
        *,
        document_id: str,
    ) -> StructuredExtraction:
        """`rag_field_extraction_enabled` が真なら field schema に従い named field を抽出する。

        定義済み field をモデルの API の structured output で抽出し、`ExtractionField`
        へ Pydantic 検証して保存する（既定 OFF）。項目は文書が属する KB の定義（無ければ全体の
        既定。複数の KB に属するときは和集合）を使う（#548）。実行結果(完了と件数・定義なし)を
        刻み、抽出の失敗(不正な応答を含む)は 0 件にせず取込を止める（#2481）。

        本文は既定のテキストモデルの入力の予算に収まる単位に分けて 1 単位ずつ呼び、候補を統合する
        （#2720。`app.rag.field_extraction_units`）。単位の結果は抽出の成果物の置き場所に保存し、
        失敗の後の再実行では、入力・モデル・項目の定義・方式の版が同じ単位を呼び直さない。
        失敗は分類（入力の上限の超過・出力の打ち切り・応答の形・要求の誤り・一時的な障害）ごとの
        文で止め、途中の結果は抽出結果に入れない（呼び出し側は旧 active の成果物を保つ）。
        """
        if not getattr(self._settings, "rag_field_extraction_enabled", False):
            return extraction
        field_defs = resolve_field_definitions(
            await self._oracle.list_document_extraction_field_sets(document_id),
            load_field_schema().fields,
        )
        if not field_defs:
            return with_field_extraction_outcome(extraction, "no_definitions")
        prompt = (
            await self._stage_prompt("field_extraction", FIELD_EXTRACTION_PROMPT)
            + f"抽出する field 定義: {field_definitions_prompt(field_defs)}"
        )
        model_id = enterprise_ai_default_model_id(self._settings)
        schema_json = json.dumps(FIELD_EXTRACTION_RESPONSE_SCHEMA, ensure_ascii=False)
        text_budget = field_extraction_text_budget(
            max_input_tokens=enterprise_ai_max_input_tokens(self._settings, model_id),
            percent=int(getattr(self._settings, "rag_field_extraction_input_token_percent", 40)),
            fixed_texts=(LLM_SYSTEM_PROMPT, prompt, schema_json),
        )
        if text_budget < MIN_UNIT_TEXT_TOKENS:
            logger.warning(
                "field_extraction_budget_insufficient",
                extra={
                    "trace_id": trace_id,
                    "text_budget_tokens": text_budget,
                    "field_count": len(field_defs),
                },
            )
            raise IngestionUserError(
                "項目の定義（項目名・説明）が長く、項目抽出の 1 回の入力の予算に本文を入れる余地が"
                "ありません。別経路には切り替えずに取込を停止しました。項目の数・説明を減らすか、"
                "システム設定 › モデル の既定のテキストモデルの「入力の上限（tokens）」と"
                " RAG_FIELD_EXTRACTION_INPUT_TOKEN_PERCENT を確認してから再実行してください。"
            )
        scope = _field_checkpoint_scope(model_id, field_defs, prompt, schema_json)

        async def _extract_unit(unit: FieldExtractionUnit) -> UnitExtraction:
            key = self._field_checkpoint_key(document_id, scope, unit.text)
            if key is not None:
                saved = _read_field_checkpoint(await self._read_optional_artifact(key))
                if saved is not None:
                    return UnitExtraction(saved, reused=True)

            async def once(_: Attempt) -> list[FieldValueCandidate]:
                with sdk_retries_disabled():
                    raw = await async_call_with_model_purpose(
                        "document.extract",
                        self._vlm.generate,
                        prompt,
                        unit.text,
                        response_schema=FIELD_EXTRACTION_RESPONSE_SCHEMA,
                        response_schema_name="field_extraction",
                    )
                # 応答の形の誤りは「出力の形の誤り」として、実行器が回数を絞って呼び直す。
                return parse_extraction_candidates(raw, field_defs)

            label = f"fields:{unit.label}"
            try:
                try:
                    candidates = await caller.call(once, label=label)
                except BoundedCallFailed as exc:
                    if not exc.final_retry:
                        raise
                    # 再試行を使い切った一時的な失敗は、間隔をあけてもう 1 回だけ呼ぶ(#2002)。
                    candidates = await caller.call(once, label=label, final=True)
            except BoundedCallFailed as exc:
                logger.warning(
                    "field_extraction_unit_failed",
                    extra={
                        "trace_id": trace_id,
                        "unit": unit.label,
                        "unit_total": unit.total,
                        "kind": exc.kind.value,
                        "status": exc.status,
                        "attempts": exc.attempts,
                    },
                )
                raise
            if key is not None:
                await self._save_field_checkpoint(key, candidates)
            return UnitExtraction(candidates)

        try:
            with ingestion_ai_calls(self._settings, "field_extraction") as caller:
                extracted = await extract_fields_from_extraction(
                    extraction, field_defs, _extract_unit, text_budget_tokens=text_budget
                )
        except BoundedCallAborted as exc:
            logger.warning(
                "field_extraction_aborted",
                extra={"trace_id": trace_id, "kind": exc.kind.value, "status": exc.status},
            )
            raise IngestionUserError(
                f"項目抽出を止めました。{exc} 別経路には切り替えずに取込を停止しました。"
            ) from exc
        except FieldExtractionFailed as exc:
            logger.warning(
                "field_extraction_failed",
                extra={
                    "trace_id": trace_id,
                    "category": exc.category,
                    "kind": exc.kind.value,
                    "status": exc.status,
                    **exc.run.as_artifact(),
                },
            )
            raise IngestionUserError(
                _field_extraction_failure_message(
                    exc, checkpoints=self._field_checkpoints_enabled()
                )
            ) from exc
        except Exception as exc:
            logger.warning(
                "field_extraction_failed",
                extra={"trace_id": trace_id, "category": "unknown", **safe_exception_fields(exc)},
            )
            raise IngestionUserError(
                "項目抽出に失敗しました。別経路には切り替えずに取込を停止しました。"
                "項目抽出設定、field schema、Enterprise AI の応答形式を確認してから"
                "再実行してください。"
            ) from exc
        outcome = extracted.parser_artifacts.get(FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY)
        if isinstance(outcome, Mapping):
            logger.info(
                "field_extraction_completed",
                extra={
                    "trace_id": trace_id,
                    **{
                        key: value
                        for key, value in outcome.items()
                        if isinstance(value, int) and not isinstance(value, bool)
                    },
                    "conflict_field_count": (
                        len(conflicts)
                        if isinstance(conflicts := outcome.get("conflict_fields"), list)
                        else 0
                    ),
                },
            )
        # 実際に使った項目の定義を刻む。レイヤーの記録はこれを指紋にする(#550)。
        return extracted.model_copy(
            update={
                "parser_artifacts": {
                    **extracted.parser_artifacts,
                    FIELD_SCHEMA_HASH_ARTIFACT_KEY: field_schema_hash(field_defs),
                }
            }
        )

    async def _attach_vision(
        self,
        trace_id: str,
        extraction: StructuredExtraction,
        *,
        source_bytes: bytes,
        content_type: str,
        file_name: str,
        parser_backend: str,
        cancel_checker: Callable[[], Awaitable[bool]] | None,
        segment: IngestionSegment | None = None,
        stop: threading.Event | None = None,
    ) -> StructuredExtraction:
        """`rag_vision_enabled` が真なら、図・画像を既定の Vision モデルで読み取る(#497)。

        Docling を含む全ての解析エンジンで同じ共通の段(``app.rag.vision``)を使う。1 件ごとの
        失敗は要素の metadata と warning に残して取込を続ける。段の全体が失敗したときも元の
        解析結果のまま続け、warning(``vision_failed``)とログを残す。取り消しは止める。

        取込の job の中(``_vision_job``)では、作業ディレクトリと実行器を job の中で共有する
        (#2002)。``segment`` を渡すと、PDF の段ごとの解析が終わった段の図・表を先に読む(流水線)。
        その応答は作業ディレクトリの checkpoint に残り、全体の読み取り(``segment`` なし)はそれを
        使うため、同じ図の VLM を呼び直さない。``stop`` は先読みを途中で止める印。
        """
        if not self._settings.rag_vision_enabled:
            return extraction
        from rag_engine.knowledge.prompt_files import IMAGE_RETRIEVAL_PROMPT_KEY
        from rag_engine.parsing.vision_prompt_rules import refine_image_retrieval_prompt
        from rag_engine.resources.runtime import bind_prompt_overrides

        from app.clients.enterprise_ai import config_from_settings
        from app.rag.answer_prompts import default_prompt

        saved = await self._saved_prompt_snapshot()
        vision_overrides = {
            key: value for key, value in saved.items() if key.startswith("picture_description.")
        }
        job = _VISION_JOB.get()
        override = saved.get(IMAGE_RETRIEVAL_PROMPT_KEY)
        template = refine_image_retrieval_prompt(
            override or default_prompt(IMAGE_RETRIEVAL_PROMPT_KEY),
            rules=vision_overrides.get("picture_description.rules"),
        ).strip()
        loop = asyncio.get_running_loop()
        # 図の読み取りは既定の Vision モデルとその接続で行う(#533)。
        config = config_from_settings(self._settings).for_vision()

        def cancel_check() -> None:
            if stop is not None and stop.is_set():
                raise IngestionCancelledError("図・表の説明の先読みを止めました。")
            if cancel_checker is not None:
                asyncio.run_coroutine_threadsafe(_raise_if_cancelled(cancel_checker), loop).result()

        # 図・表の説明の段の件数を job の進捗へ写す(#1999)。対象の検出(ページの描画)の間も
        # 段を出し、Vision モデルの応答を待っている対象(ページ・種類)を画面に示す。段ごとの
        # 先読みは、説明の始まった段の件数を足して出す(#2002)。
        progress = active_ingestion_progress()
        progress_callback: Callable[[Mapping[str, object]], None] | None = None
        segments_progress = job.segments_progress if job is not None else None
        if segment is not None:
            page_start, page_end = segment.page_start or 1, segment.page_end or 1
            if segments_progress is not None:
                progress_callback = functools.partial(
                    segments_progress.update, page_start, page_end
                )
        elif progress is not None:
            progress.update_vision(IngestionVisionProgress(phase="preparing"))

            def progress_callback(payload: Mapping[str, object]) -> None:
                progress.update_vision(vision_progress_from_ledger(payload))

        # VLM の呼び出しは上限つきの並行の実行器を通す(#2002。上限は rag_vision_concurrency)。
        # job の中では先読みと全体の読み取りで 1 つの実行器を使う(429 の待ちを共有する)。
        caller_scope: contextlib.AbstractContextManager[BoundedCaller] = (
            contextlib.nullcontext(job.vision_caller(self._settings))
            if job is not None
            else ingestion_ai_calls(self._settings, "vision")
        )
        with bind_prompt_overrides(vision_overrides), caller_scope as caller:
            describer = EnterpriseAiPictureDescriber(
                self._vlm,
                loop,
                model_id=config.vision_model_id,
                endpoint=config.enterprise_ai_endpoint,
                project_id=config.enterprise_ai_project_id,
                caller=caller,
            )
            try:
                result = await asyncio.to_thread(
                    read_figures_with_vision,
                    extraction,
                    source_bytes=source_bytes,
                    content_type=content_type,
                    file_name=file_name,
                    parser_backend=parser_backend,
                    describer=describer,
                    prompt_template=template,
                    cancel_check=(
                        cancel_check if cancel_checker is not None or stop is not None else None
                    ),
                    progress_callback=progress_callback,
                    work_dir=job.work_dir() if job is not None else None,
                )
            except IngestionCancelledError:
                raise
            except BoundedCallAborted as exc:
                # 権限・設定の誤り・割り当ての使い切り・回復しない失敗。残りを呼ばずに止める。
                logger.warning(
                    "vision_aborted",
                    extra={"trace_id": trace_id, "kind": exc.kind.value, "status": exc.status},
                )
                raise IngestionUserError(str(exc)) from exc
            except Exception:
                if segment is not None:
                    raise
                logger.warning("vision_failed", extra={"trace_id": trace_id}, exc_info=True)
                result = vision_failure(extraction, parser_backend)
        if segment is not None:
            if segments_progress is not None:
                segments_progress.finish_segment(page_start, page_end)
            return result
        if progress is not None:
            progress.finish_vision()
        _raise_if_item_failures_fail_job(self._settings, result)
        return result

    async def _start_vision_prefetch(
        self,
        *,
        trace_id: str,
        document_id: str,
        checkpoint_segments: Sequence[IngestionSegment],
        source_bytes: bytes,
        content_type: str,
        file_name: str,
        parser_backend: str,
        progress: IngestionProgressReporter | None,
        cancel_checker: Callable[[], Awaitable[bool]] | None,
    ) -> _VisionPrefetcher | None:
        """段ごとの解析と並べる Vision の先読みを始める(#2002)。Vision が無効なら None。

        前の試行で段ごとに残した応答を作業ディレクトリへ戻してから始める(済んだ図を呼び直さない)。
        """
        job = _VISION_JOB.get()
        if job is None or not self._settings.rag_vision_enabled:
            return None
        await self._restore_vision_checkpoints(document_id, checkpoint_segments)
        if progress is not None:
            job.segments_progress = VisionSegmentsProgress(
                progress, segments_total=len(checkpoint_segments)
            )

        async def run(
            segment: IngestionSegment, extraction: StructuredExtraction, stop: threading.Event
        ) -> None:
            await self._prefetch_segment_vision(
                trace_id=trace_id,
                document_id=document_id,
                segment=segment,
                extraction=extraction,
                source_bytes=source_bytes,
                content_type=content_type,
                file_name=file_name,
                parser_backend=parser_backend,
                cancel_checker=cancel_checker,
                stop=stop,
            )

        return _VisionPrefetcher(run)

    async def _prefetch_segment_vision(
        self,
        *,
        trace_id: str,
        document_id: str,
        segment: IngestionSegment,
        extraction: StructuredExtraction,
        source_bytes: bytes,
        content_type: str,
        file_name: str,
        parser_backend: str,
        cancel_checker: Callable[[], Awaitable[bool]] | None,
        stop: threading.Event,
    ) -> None:
        """解析の終わった段の図・表を先に読み、応答を段の artifact に残す(#2002)。

        layout_records(Docling の解析の結果)の無い段は読まない(全体の読み取りが読む)。先読みの
        失敗は警告にとどめる(全体の読み取りが読み直す)。止める失敗(権限・設定の誤り)は出す。
        """
        if _vision_layout_records(extraction) is None:
            return
        job = _VISION_JOB.get()
        await self._attach_vision(
            trace_id,
            extraction,
            source_bytes=source_bytes,
            content_type=content_type,
            file_name=file_name,
            parser_backend=parser_backend,
            cancel_checker=cancel_checker,
            segment=segment,
            stop=stop,
        )
        if job is not None:
            await self._save_vision_checkpoints(document_id, segment, job)

    async def _restore_vision_checkpoints(
        self, document_id: str, segments: Sequence[IngestionSegment]
    ) -> None:
        """前の試行で段ごとに残した Vision の応答を作業ディレクトリへ戻す(再開。#2002)。

        応答は入力(画像・prompt・モデル)の hash の鍵で使い回すため、入力が変わった図は使わない
        (``VisionCheckpoints``)。読めない artifact は飛ばす。
        """
        job = _VISION_JOB.get()
        if job is None or not self._settings.rag_vision_enabled:
            return
        restored = 0
        for segment in segments:
            key = self._vision_checkpoint_artifact_key(document_id, segment)
            if key is None:
                return
            data = await self._read_optional_artifact(key)
            if data is None:
                continue
            restored += await asyncio.to_thread(
                _write_vision_checkpoint_files, job.work_dir(), data
            )
        if restored:
            logger.info(
                "vision_checkpoints_restored",
                extra={"document_id": document_id, "count": restored},
            )

    def _field_checkpoints_enabled(self) -> bool:
        return bool(getattr(self._settings, "rag_extraction_artifact_cache_enabled", True))

    def _field_checkpoint_key(self, document_id: str, scope: str, unit_text: str) -> str | None:
        """項目抽出の単位の結果の置き場所(抽出の成果物の prefix の下。文書の削除で消える)。

        名前は方式の版・モデル・項目の定義・指示(``scope``)と単位の本文の hash で、どれかが
        変われば別の名前になる(違う入力の結果を使い直さない。#2720)。
        """
        if not self._field_checkpoints_enabled():
            return None
        key_prefix = _safe_artifact_prefix(
            getattr(self._settings, "rag_extraction_artifact_prefix", "artifacts/extractions")
        )
        digest = hashlib.sha256(f"{scope}\n{unit_text}".encode()).hexdigest()
        return f"{key_prefix}/{_safe_artifact_key_part(document_id)}/fields/{digest}.json"

    async def _save_field_checkpoint(
        self, key: str, candidates: Sequence[FieldValueCandidate]
    ) -> None:
        """単位の結果を保存する(再実行で使い直す補助。保存できなくても取込は続ける)。"""
        payload = {
            "version": _FIELD_CHECKPOINT_VERSION,
            "candidates": [
                {"field": item.field.model_dump(mode="json"), "raw_value": item.raw_value}
                for item in candidates
            ],
        }
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            await self._object_storage.put(key, data, content_type="application/json")
        except Exception as exc:  # noqa: BLE001 - 再開の補助。取込は続ける。
            logger.warning(
                "field_extraction_checkpoint_save_failed",
                extra={"error_type": type(exc).__name__},
            )

    async def _read_optional_artifact(self, key: str) -> bytes | None:
        """無い・読めない artifact は None(使わずに読み直す)。"""
        try:
            return await self._object_storage.get(key)
        except Exception:  # noqa: BLE001 - 再開の補助。読めなければ読み直す。
            return None

    async def _save_vision_checkpoints(
        self, document_id: str, segment: IngestionSegment, job: _VisionJobState
    ) -> None:
        """段の図・表の Vision の応答(checkpoint)を段の artifact に残す(再開に使う。#2002)。"""
        key = self._vision_checkpoint_artifact_key(document_id, segment)
        if key is None or segment.page_start is None or segment.page_end is None:
            return
        data = await asyncio.to_thread(
            _collect_vision_checkpoint_files,
            job.work_dir(),
            page_start=segment.page_start,
            page_end=segment.page_end,
        )
        if data is None:
            return
        try:
            await self._object_storage.put(key, data, content_type="application/json")
        except Exception as exc:  # noqa: BLE001 - 再開の補助。取込は続ける。
            logger.warning(
                "vision_checkpoint_artifact_save_failed",
                extra={"segment_id": segment.segment_id, "error_type": type(exc).__name__},
            )

    def _vision_checkpoint_artifact_key(
        self, document_id: str, segment: IngestionSegment
    ) -> str | None:
        if not getattr(self._settings, "rag_extraction_artifact_cache_enabled", True):
            return None
        key_prefix = _safe_artifact_prefix(
            getattr(self._settings, "rag_extraction_artifact_prefix", "artifacts/extractions")
        )
        return (
            f"{key_prefix}/{_safe_artifact_key_part(document_id)}/vision/"
            f"{_safe_artifact_key_part(segment.segment_id)}.json"
        )

    async def _attach_navigation_tree(
        self,
        trace_id: str,
        extraction: StructuredExtraction,
        *,
        document_id: str | None = None,
    ) -> StructuredExtraction:
        """章節 navigation tree を決定論的に構築し、任意で node 要約を付与する。

        tree 構築は LLM 不要で常に実行する。node 要約は
        `rag_navigation_summary_enabled` が真のときだけ LLM で行う。
        """
        from rag_parser_core.extraction import NAV_SUMMARY_KEY, SECTION_SUMMARY_CONTENT_KIND

        from app.rag.section_rules import (
            NAVIGATION_RULES_FINGERPRINT_ARTIFACT,
            NAVIGATION_RULES_MODE_ARTIFACT,
            PARSER_NAVIGATION_ARTIFACT,
            build_rule_navigation,
            load_section_rules,
            rules_for_mode,
            section_rules_fingerprint,
        )
        from app.schemas.document import DocumentProcessingConfig

        # 再実行の前の要約を除き、派生要素が要約の入力や章節の候補に戻らないようにする。
        extraction = extraction.model_copy(
            update={
                "elements": [
                    element
                    for element in extraction.elements
                    if element.content_kind != SECTION_SUMMARY_CONTENT_KIND
                    and not element.metadata.get(NAV_SUMMARY_KEY)
                ]
            }
        )
        store = load_section_rules()
        mode = store.mode
        if document_id is not None and self._recipe_id is not None:
            row = await self._oracle.get_document_recipe(document_id, self._recipe_id)
            config = DocumentProcessingConfig.model_validate(
                (row or {}).get("processing_config") or {}
            )
            mode = config.section_rules_mode or mode
        parser_nodes = build_navigation_tree(extraction)
        rules = rules_for_mode(mode, store)
        nodes = parser_nodes if rules is None else build_rule_navigation(extraction, rules)
        parser_artifacts = {
            **extraction.parser_artifacts,
            NAVIGATION_RULES_MODE_ARTIFACT: mode,
            NAVIGATION_RULES_FINGERPRINT_ARTIFACT: section_rules_fingerprint(mode, store),
        }
        if rules is not None:
            parser_artifacts[PARSER_NAVIGATION_ARTIFACT] = [
                node.model_dump(mode="json") for node in parser_nodes
            ]
        else:
            parser_artifacts.pop(PARSER_NAVIGATION_ARTIFACT, None)
        if getattr(self._settings, "rag_navigation_summary_enabled", False):
            max_nodes = int(getattr(self._settings, "rag_navigation_summary_max_nodes", 24))
            prompt = await self._stage_prompt("navigation_summary", NAVIGATION_SUMMARY_PROMPT)

            async def _summarize_one(text: str, attempt: Attempt) -> str:
                # 大きすぎる(context の長さの超過)ときは入力を短くしてやり直す。
                source = (
                    text
                    if attempt.adaptation == 0
                    else text[: max(200, len(text) >> attempt.adaptation)]
                )
                with sdk_retries_disabled():
                    return await async_call_with_model_purpose(
                        "document.summarize", self._vlm.generate, prompt, source
                    )

            try:
                # 章節の要約は上限つきの並行で呼ぶ(#2002。rag_navigation_summary_concurrency)。
                with ingestion_ai_calls(self._settings, "navigation_summary") as caller:

                    async def _summarize_many(texts: Sequence[str]) -> list[str | None]:
                        outcomes = await caller.map(list(texts), _summarize_one, adaptive=True)
                        return [outcome.value if outcome.ok else None for outcome in outcomes]

                    nodes = await summarize_navigation_nodes(
                        nodes,
                        extraction,
                        None,
                        summarize_many=_summarize_many,
                        max_nodes=max_nodes,
                    )
            except BoundedCallAborted as exc:
                logger.warning("navigation_summary_aborted", extra={"trace_id": trace_id})
                raise IngestionUserError(
                    f"章節の要約を止めました。{exc} 別経路には切り替えずに取込を停止しました。"
                ) from exc
            except Exception as exc:
                logger.warning("navigation_summary_failed", extra={"trace_id": trace_id})
                raise IngestionUserError(
                    "章節の要約に失敗しました。別経路には切り替えずに取込を停止しました。"
                    "章節の要約設定と Enterprise AI の応答を確認してから"
                    "再実行してください。"
                ) from exc
            # 実際に使った要約の上限数を刻む。レイヤーの記録はこれを指紋にする(#550)。
            parser_artifacts = {
                **parser_artifacts,
                NAVIGATION_SUMMARY_MAX_NODES_ARTIFACT_KEY: max_nodes,
            }
        # summary がある node は検索可能な section_summary element にして、
        # Knowhere の Navigate / progressive disclosure を hybrid retrieval へつなぐ。
        next_order = max((element.order for element in extraction.elements), default=0) + 1
        summary_elements = navigation_summary_elements(nodes, start_order=next_order)
        updates: dict[str, object] = {"navigation": nodes, "parser_artifacts": parser_artifacts}
        if summary_elements:
            updates["elements"] = [*extraction.elements, *summary_elements]
        return extraction.model_copy(update=updates)

    async def _cache_extraction_artifact(
        self,
        *,
        document_id: str,
        trace_id: str,
        extraction: StructuredExtraction,
        parse_key: str | None = None,
    ) -> tuple[StructuredExtraction, str | None]:
        """抽出 JSON artifact を 保存先に保存し、path を metadata へ戻す。

        ``parse_key`` は解析に渡した内容と設定の鍵(``_segment_parse_key``)。再開で読み戻すときに
        今回の鍵と一致するかを確かめる(#2076)。
        """
        if not getattr(self._settings, "rag_extraction_artifact_cache_enabled", True):
            return extraction, None
        key_prefix = _safe_artifact_prefix(
            getattr(
                self._settings,
                "rag_extraction_artifact_prefix",
                "artifacts/extractions",
            )
        )
        document_key = _safe_artifact_key_part(document_id)
        trace_key = _safe_artifact_key_part(trace_id)
        key = f"{key_prefix}/{document_key}/{trace_key}.json"
        cache_extraction = _extraction_with_artifact_cache_metadata(
            extraction,
            artifact_kind="full",
            document_id=document_id,
            trace_id=trace_id,
            parse_key=parse_key,
        )
        payload = cache_extraction.to_document_payload()
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            artifact_path = await self._object_storage.put(
                key,
                data,
                content_type="application/json",
            )
        except Exception as exc:
            logger.warning(
                "extraction_artifact_cache_failed",
                extra={"document_id": document_id, "error_type": type(exc).__name__},
            )
            parser_artifacts = {
                **extraction.parser_artifacts,
                "extraction_artifact_cache_failed": True,
            }
            warnings = _dedupe_text([*extraction.warnings, "extraction_artifact_cache_failed"])
            return (
                extraction.model_copy(
                    update={"parser_artifacts": parser_artifacts, "warnings": warnings}
                ),
                None,
            )
        parser_artifacts = {
            **cache_extraction.parser_artifacts,
            "extraction_artifact_path": artifact_path,
            "extraction_artifact_content_type": "application/json",
        }
        return (
            cache_extraction.model_copy(update={"parser_artifacts": parser_artifacts}),
            artifact_path,
        )

    async def _load_cached_segment_extractions(
        self,
        checkpoint_segments: Sequence[IngestionSegment],
        *,
        target_segments: Sequence[PdfPageSegment],
        available_ranges: set[tuple[int, int]] | None = None,
        parse_keys: Mapping[tuple[int, int], str] | None = None,
    ) -> _CachedSegmentLoadResult:
        """成功済み segment artifact を読み戻し、欠落分は再処理へ回す。

        ``parse_keys`` を渡すと、段の PDF と解析・前処理の設定から作った鍵が artifact と同じ段だけを
        使う(原本・設定が変わった段の結果を使い回さない。#2001)。
        """
        target_ranges = {(segment.page_start, segment.page_end) for segment in target_segments}
        cached: list[_SegmentExtraction] = []
        missing_ranges: set[tuple[int, int]] = set()
        for checkpoint in checkpoint_segments:
            if checkpoint.status != "SUCCEEDED":
                continue
            if checkpoint.page_start is None or checkpoint.page_end is None:
                continue
            checkpoint_range = (checkpoint.page_start, checkpoint.page_end)
            if available_ranges is not None and checkpoint_range not in available_ranges:
                continue
            if checkpoint_range in target_ranges:
                continue
            if not checkpoint.artifact_path:
                missing_ranges.add(checkpoint_range)
                continue
            try:
                data = await self._object_storage.get(checkpoint.artifact_path)
                extraction = StructuredExtraction.model_validate_json(data)
            except Exception as exc:
                logger.info(
                    "segment_extraction_artifact_load_skipped",
                    extra={
                        "segment_id": checkpoint.segment_id,
                        "error_type": type(exc).__name__,
                    },
                )
                missing_ranges.add(checkpoint_range)
                continue
            if not _cached_segment_artifact_matches(
                extraction,
                checkpoint,
                parse_key=parse_keys.get(checkpoint_range) if parse_keys is not None else None,
            ):
                logger.info(
                    "segment_extraction_artifact_identity_mismatch",
                    extra={"segment_id": checkpoint.segment_id},
                )
                missing_ranges.add(checkpoint_range)
                continue
            cached.append(
                _SegmentExtraction(
                    segment=PdfPageSegment(
                        index=len(cached),
                        page_start=checkpoint.page_start,
                        page_end=checkpoint.page_end,
                        content=b"",
                    ),
                    extraction=extraction,
                )
            )
        return _CachedSegmentLoadResult(cached=cached, missing_ranges=missing_ranges)

    async def _load_cached_full_extraction(
        self,
        checkpoint_segments: Sequence[IngestionSegment],
        *,
        parse_key: str,
    ) -> StructuredExtraction | None:
        """全 segment が同じ full artifact を指し、解析の入力と設定が同じなら抽出結果を再利用する。

        segment は文書ごとで、解析に渡した内容(ファイル準備の後の bytes)・設定・レシピの revision を
        持たない。artifact に保存した鍵(``parse_key``)が今回と違えば再利用しない。ファイル準備や
        解析エンジンを変えて取り込み直したのに前回の解析の結果を使うと、新しい設定が chunk に
        反映されない(#2076)。
        """
        if not checkpoint_segments:
            return None
        if any(
            segment.status != "SUCCEEDED" or not segment.artifact_path
            for segment in checkpoint_segments
        ):
            return None
        paths = {segment.artifact_path for segment in checkpoint_segments if segment.artifact_path}
        if len(paths) != 1:
            return None
        artifact_path = next(iter(paths))
        try:
            data = await self._object_storage.get(artifact_path)
            extraction = StructuredExtraction.model_validate_json(data)
        except Exception as exc:
            logger.info(
                "full_extraction_artifact_load_skipped",
                extra={"error_type": type(exc).__name__},
            )
            return None
        document_id = checkpoint_segments[0].document_id
        if not _cached_full_artifact_matches(extraction, document_id, parse_key=parse_key):
            logger.info(
                "full_extraction_artifact_identity_mismatch",
                extra={"document_id": document_id},
            )
            return None
        parser_artifacts = {
            **extraction.parser_artifacts,
            "extraction_artifact_reused": True,
            "extraction_artifact_path": artifact_path,
        }
        return extraction.model_copy(update={"parser_artifacts": parser_artifacts})

    async def _mark_segments_succeeded(
        self,
        segments: Sequence[IngestionSegment],
        *,
        artifact_path: str | None,
    ) -> list[IngestionSegment]:
        updated_segments: list[IngestionSegment] = []
        latest_segments = await self._segments_with_latest_status(segments)
        latest_by_id = {segment.segment_id: segment for segment in latest_segments}
        for segment in segments:
            latest = latest_by_id.get(segment.segment_id, segment)
            checkpoint_artifact_path = (
                latest.artifact_path or segment.artifact_path or artifact_path
            )
            await self._mark_segment_succeeded(latest, artifact_path=checkpoint_artifact_path)
            updated_segments.append(
                latest.model_copy(
                    update={
                        "status": "SUCCEEDED",
                        "artifact_path": checkpoint_artifact_path,
                    }
                )
            )
        return updated_segments

    async def _mark_segments_cancelled(
        self,
        segments: Sequence[IngestionSegment],
    ) -> None:
        for segment in await self._segments_with_latest_status(segments):
            if segment.status == "SUCCEEDED":
                continue
            await self._safe_update_ingestion_segment(segment.segment_id, status="CANCELLED")

    async def _mark_segments_failed(
        self,
        segments: Sequence[IngestionSegment],
        *,
        error: Exception,
    ) -> None:
        for segment in await self._segments_with_latest_status(segments):
            # 段ごとの解析(#2001)は、失敗した段と始めていない段(取消)を先に書いている。
            if segment.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                continue
            await self._mark_segment_failed(segment, error=error)

    async def _segments_with_latest_status(
        self,
        segments: Sequence[IngestionSegment],
    ) -> list[IngestionSegment]:
        """DB 上の最新 segment 状態を反映し、成功済み checkpoint を保護する。"""
        if not segments:
            return []
        document_id = segments[0].document_id
        latest = await self._safe_list_ingestion_segments(document_id)
        if not latest:
            return list(segments)
        latest_by_id = {segment.segment_id: segment for segment in latest}
        return [latest_by_id.get(segment.segment_id, segment) for segment in segments]

    async def _mark_segment_succeeded(
        self,
        segment: IngestionSegment | None,
        *,
        artifact_path: str | None = None,
    ) -> None:
        if segment is None:
            return
        await self._safe_update_ingestion_segment(
            segment.segment_id,
            status="SUCCEEDED",
            artifact_path=artifact_path,
            error_code="",
            error_message="",
        )

    async def _mark_segment_failed(
        self,
        segment: IngestionSegment | None,
        *,
        error: Exception | None = None,
        status: str = "FAILED",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if segment is None:
            return
        await self._safe_update_ingestion_segment(
            segment.segment_id,
            status=status,
            error_code=error_code or _segment_error_code(error),
            error_message=(error_message or _safe_segment_error_message(error))[:500],
        )

    async def _safe_list_ingestion_segments(
        self,
        document_id: str,
    ) -> list[IngestionSegment]:
        list_segments = getattr(self._oracle, "list_ingestion_segments", None)
        if not callable(list_segments):
            return []
        try:
            segments = list(await list_segments(document_id))
            if self._recipe_id is not None:
                return [segment for segment in segments if segment.recipe_id == self._recipe_id]
            return segments
        except Exception as exc:
            logger.info(
                "ingestion_segment_checkpoint_list_skipped",
                extra={"document_id": document_id, "error_type": type(exc).__name__},
            )
            return []

    async def _safe_replace_ingestion_segments(
        self,
        document_id: str,
        segments: Sequence[IngestionSegment],
    ) -> None:
        replace_segments = getattr(self._oracle, "replace_ingestion_segments", None)
        if not callable(replace_segments):
            return
        try:
            await replace_segments(document_id, segments)
        except Exception as exc:
            logger.info(
                "ingestion_segment_checkpoint_replace_skipped",
                extra={"document_id": document_id, "error_type": type(exc).__name__},
            )

    async def _safe_update_ingestion_segment(
        self,
        segment_id: str,
        **updates: object,
    ) -> None:
        update_segment = getattr(self._oracle, "update_ingestion_segment", None)
        if not callable(update_segment):
            return
        cleaned_updates = {key: value for key, value in updates.items() if value is not None}
        try:
            await update_segment(segment_id, **cleaned_updates)
        except Exception as exc:
            logger.info(
                "ingestion_segment_checkpoint_update_skipped",
                extra={"segment_id": segment_id, "error_type": type(exc).__name__},
            )

    async def _augment_with_raptor(
        self,
        trace_id: str,
        chunks: list[Chunk],
        cancel_checker: Callable[[], Awaitable[bool]] | None,
    ) -> list[Chunk]:
        """RAPTOR 再帰要約索引(opt-in)。leaf に summary node を加えて返す。

        要約はモデルの API で行う。OFF(既定)のときは leaf chunk をそのまま返す。
        1 つの階層の cluster は取込のほかの段と同じ上限つきの並行の実行器で呼ぶ(#2067。
        ``rag_raptor_concurrency``)。429 は段の全体で待ち、再試行のあとも要約できない cluster は
        飛ばす(leaf は残る)。権限・モデルの設定の誤り・割り当ての使い切りは取込を止める。
        """
        if not getattr(self._settings, "rag_raptor_enabled", False):
            return chunks
        await _raise_if_cancelled(cancel_checker)

        system_prompt = await self._stage_prompt(
            "raptor_summary_system", RAPTOR_SUMMARY_SYSTEM_PROMPT
        )

        async def _summarize_one(text: str, attempt: Attempt) -> str:
            # 大きすぎる(context の長さの超過)ときは入力を短くしてやり直す。
            source = (
                text
                if attempt.adaptation == 0
                else text[: max(200, len(text) >> attempt.adaptation)]
            )
            with sdk_retries_disabled():
                return await async_call_with_model_purpose(
                    "document.summarize",
                    self._vlm.generate,
                    "",
                    source,
                    system_prompt=system_prompt,
                )

        try:
            with ingestion_ai_calls(self._settings, "raptor_summary") as caller:

                async def _summarize_many(texts: list[str]) -> list[str | None]:
                    # 階層の間で取消を確かめる(階層は前の階層の要約を入力にするため順に作る)。
                    await _raise_if_cancelled(cancel_checker)
                    outcomes = await caller.map(texts, _summarize_one, adaptive=True)
                    return [outcome.value if outcome.ok else None for outcome in outcomes]

                return await build_raptor_summaries(
                    chunks,
                    summarize_many=_summarize_many,
                    cluster_size=int(getattr(self._settings, "rag_raptor_cluster_size", 5)),
                    max_levels=int(getattr(self._settings, "rag_raptor_max_levels", 2)),
                )
        except IngestionCancelledError:
            raise
        except BoundedCallError as exc:
            # 権限・設定の誤り(BoundedCallAborted)と、件の失敗で job を失敗にする設定
            # (rag_ai_call_fail_job_on_item_error)の件の失敗。
            logger.warning("raptor_summary_aborted", extra={"trace_id": trace_id})
            raise IngestionUserError(
                f"RAPTOR の階層の要約を止めました。{exc} 別経路には切り替えずに取込を停止しました。"
            ) from exc
        except Exception as exc:
            logger.warning("raptor_summary_failed", extra={"trace_id": trace_id})
            raise IngestionUserError(
                "RAPTOR の階層の要約に失敗しました。別経路には切り替えずに取込を停止しました。"
                "RAPTOR 設定と Enterprise AI の応答を確認してから再実行してください。"
            ) from exc

    async def _save_index(
        self,
        trace_id: str,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        vectors: list[list[float]],
        *,
        chunk_set_id: str | None = None,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """抽出本文とチャンクを一貫した indexing stage として保存する。

        chunk_set_id を渡すと、その chunk_set だけを置換・タグ付けする(複数 chunk_set 共存)。
        None は文書の全 chunk を置換し未タグ保存(現行挙動)。
        """
        await _raise_if_cancelled(cancel_checker)
        await self._oracle.save_index(
            document_id, extraction, chunks, vectors, chunk_set_id=chunk_set_id
        )
        await _raise_if_cancelled(cancel_checker)
        await self._save_entity_index_if_enabled(
            trace_id, document_id, extraction, chunks, chunk_set_id=chunk_set_id
        )
        if not resolve_graph_adapter(self._settings).enabled:
            return
        try:
            await _observe_ingestion_stage(
                trace_id,
                "graph_indexing",
                self._save_graph_index(
                    document_id,
                    extraction,
                    chunks,
                    chunk_set_id=chunk_set_id,
                ),
                attributes={
                    "chunk_count": len(chunks),
                    "element_count": len(extraction.elements),
                },
                result_attributes=_graph_index_result_attributes,
            )
        except Exception as exc:
            logger.info(
                "graph_indexing_failed",
                extra={"document_id": document_id, "error_type": type(exc).__name__},
            )
            raise IngestionUserError(
                "文書構造索引の構築に失敗しました。別経路には切り替えずに取込を停止しました。"
                "文書構造索引の構築の設定、抽出結果、データベース保存先を確認してから再実行してください。"
            ) from exc

    async def _save_embeddings_for_chunk_set(
        self,
        trace_id: str,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        vectors: list[list[float]],
        *,
        chunk_set_id: str,
        cancel_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """保存済み chunk_set の chunk 行へ embedding と派生 index を保存する。"""
        await _raise_if_cancelled(cancel_checker)
        await self._oracle.update_chunk_set_embeddings(
            chunk_set_id=chunk_set_id,
            chunks=chunks,
            embeddings=vectors,
        )
        await _raise_if_cancelled(cancel_checker)
        await self._save_entity_index_if_enabled(
            trace_id, document_id, extraction, chunks, chunk_set_id=chunk_set_id
        )
        if not resolve_graph_adapter(self._settings).enabled:
            return
        try:
            await _observe_ingestion_stage(
                trace_id,
                "graph_indexing",
                self._save_graph_index(
                    document_id,
                    extraction,
                    chunks,
                    chunk_set_id=chunk_set_id,
                ),
                attributes={
                    "chunk_count": len(chunks),
                    "element_count": len(extraction.elements),
                },
                result_attributes=_graph_index_result_attributes,
            )
        except Exception as exc:
            logger.info(
                "graph_indexing_failed",
                extra={"document_id": document_id, "error_type": type(exc).__name__},
            )
            raise IngestionUserError(
                "文書構造索引の構築に失敗しました。別経路には切り替えずに取込を停止しました。"
                "文書構造索引の構築の設定、抽出結果、データベース保存先を確認してから再実行してください。"
            ) from exc

    async def _save_entity_index_if_enabled(
        self,
        trace_id: str,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        *,
        chunk_set_id: str | None,
    ) -> None:
        """実体・別名索引の構築が有効なとき、実体と chunk の関連を保存する。

        文書レシピで無効にした文書は何もしない(SQL も実行しない)。実体は chunk_set ごとに
        置き換える。
        chunk_set の無い旧い保存(未タグの chunk)は対象にしない。
        """
        if not self._settings.rag_entity_index_enabled or chunk_set_id is None:
            return
        try:
            await _observe_ingestion_stage(
                trace_id,
                "entity_indexing",
                self._save_entity_index(document_id, extraction, chunks, chunk_set_id=chunk_set_id),
                attributes={"chunk_count": len(chunks)},
                result_attributes=_entity_index_result_attributes,
            )
        except Exception as exc:
            logger.info(
                "entity_indexing_failed",
                extra={"document_id": document_id, "error_type": type(exc).__name__},
            )
            raise IngestionUserError(
                "実体・別名索引の保存に失敗しました。別経路には切り替えずに取込を停止しました。"
                "システムテーブル(実体の層の表)が作成済みか、データベース保存先を確認してから"
                "再実行してください。"
            ) from exc

    async def _save_entity_index(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        *,
        chunk_set_id: str,
    ) -> EntityIndex:
        """chunk から実体を決定的に抜き出し、Oracle の実体の表へ保存する(#1362)。"""
        from app.clients.entity_store import EntityStore

        title = "\n".join(element.text for element in extraction.elements[:3])
        index = await asyncio.to_thread(
            build_entity_index,
            document_id=document_id,
            chunks=chunks,
            chunk_set_id=chunk_set_id,
            document_title=title,
            options=EntityIndexOptions(
                name_columns=tuple(self._settings.rag_entity_name_columns),
                attribute_columns=tuple(self._settings.rag_entity_attribute_columns),
            ),
        )
        await EntityStore(self._oracle).replace_chunk_set_entity_index(
            document_id, chunk_set_id, index
        )
        return index

    async def _save_graph_index(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        *,
        chunk_set_id: str | None = None,
    ) -> GraphIndex:
        """構造化抽出から関係情報(文書と章・節の見出しのつながり)を作り Oracle へ保存する。"""
        knowledge_bases = await self._oracle.list_document_knowledge_bases(document_id)
        document = await self._oracle.get_document(document_id)
        graph_index = await asyncio.to_thread(
            build_graph_index,
            document_id=document_id,
            knowledge_base_ids=[knowledge_base.id for knowledge_base in knowledge_bases],
            extraction=extraction,
            chunks=chunks,
            chunk_set_id=chunk_set_id,
            document_name=document.file_name if document is not None else None,
        )
        await self._oracle.replace_document_graph_index(
            document_id,
            graph_index,
            chunk_set_id=chunk_set_id,
        )
        return graph_index


def _safe_persistent_error_message(error: Exception) -> str:
    """document table に保存してよい短いエラーメッセージを返す。"""
    if getattr(error, "safe_for_user", False):
        return str(error)[:200]
    return INGESTION_INTERNAL_ERROR_MESSAGE


def _segment_error_code(error: Exception | None) -> str:
    """segment checkpoint に保存する低 cardinality の error code を返す。"""
    if error is None:
        return "error"
    code = getattr(error, "error_code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:80]
    if isinstance(error, ValidationError):
        return "structured_extraction_validation_error"
    return type(error).__name__


def _safe_segment_error_message(error: Exception | None) -> str:
    """segment UI に出してよい原因メッセージへ正規化する。"""
    if error is None:
        return ""
    if isinstance(error, ValidationError):
        return _validation_error_message(error)
    if getattr(error, "safe_for_user", False):
        return str(error)
    return INGESTION_INTERNAL_ERROR_MESSAGE


def _coerce_extraction_payload(value: object) -> Mapping[str, object] | None:
    """extraction 層の extraction_json(JSON 列)を Mapping へ正規化する。

    Oracle JSON 列はドライバ設定によって dict / str いずれでも返りうるため両対応する。
    """
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _validate_structured_extraction_payload(
    payload: Mapping[str, object],
) -> StructuredExtraction:
    """抽出 payload を検証し、ValidationError を利用者向けに変換する。"""
    try:
        return StructuredExtraction.model_validate(payload)
    except ValidationError as exc:
        raise EnterpriseAiValidationError(_validation_error_message(exc)) from exc


def _validation_error_message(error: ValidationError) -> str:
    """Pydantic validation error から非機密な原因摘要を作る。"""
    details: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False)[:3]:
        location = ".".join(str(part) for part in item.get("loc", ()) if part != "__root__")
        error_type = str(item.get("type") or "validation_error")
        message = str(item.get("msg") or "schema validation failed")
        details.append(f"{location}: {error_type} ({message})" if location else message)
    if error.error_count() > len(details):
        details.append(f"ほか {error.error_count() - len(details)} 件")
    joined = " / ".join(details) if details else "StructuredExtraction schema validation failed"
    return (
        "Enterprise AI の抽出結果が保存用 schema と一致しません。"
        f"失敗項目: {joined}。"
        "VLM response path / payload template と raw_text・confidence・elements の形式を"
        "確認して再実行してください。"
    )


def _raise_if_item_failures_fail_job(settings: object, extraction: StructuredExtraction) -> None:
    """``rag_ai_call_fail_job_on_item_error`` なら、説明できなかった図がある取込を止める(#2002)。"""
    if not getattr(settings, "rag_ai_call_fail_job_on_item_error", False):
        return
    summary = extraction.parser_artifacts.get("vision")
    failed = summary.get("failed") if isinstance(summary, Mapping) else None
    if isinstance(failed, int) and failed > 0:
        raise IngestionUserError(
            f"図・表の説明（Vision）で、再試行しても説明できなかった図・表が {failed} 件"
            "ありました。"
            "RAG_AI_CALL_FAIL_JOB_ON_ITEM_ERROR が有効なため取込を止めました。"
            "文書の詳細で図・表の説明の失敗の理由を確かめてから再実行してください。"
        )


async def _raise_if_cancelled(
    cancel_checker: Callable[[], Awaitable[bool]] | None,
) -> None:
    """job が cancel 済みなら以降の stage を止める。"""
    if cancel_checker is None:
        return
    if await cancel_checker():
        raise IngestionCancelledError(INGESTION_JOB_CANCELLED_MESSAGE)


def _unsupported_parser_message(result: ParserRegistryResult) -> str:
    """parser registry の未対応理由を利用者向け文言へ寄せる。"""
    if result.unsupported_reason == "tiff_image_not_supported":
        return (
            "TIFF 画像の取込は現在未対応です。"
            "PNG/JPEG/WEBP へ変換してから再アップロードしてください。"
        )
    if result.unsupported_reason in {
        "legacy_office_binary_not_supported",
        "macro_enabled_office_not_supported",
    }:
        return (
            "旧形式の Office・マクロ付きの Office は原本のままでは解析できません。"
            "文書レシピのファイル準備で変換(Word・PowerPoint は「Office→PDF」、"
            "Word は「Word→JSONL」も可、Excel は「Excel→JSONL」)を選んでから取り込んでください"
            "(マクロは実行しません)。"
        )
    if result.unsupported_reason == "odt_rtf_not_supported":
        return (
            "ODT・RTF は原本のままでは解析できません。"
            "文書レシピのファイル準備で「Office→PDF」を選んでから取り込んでください。"
        )
    return "このファイル形式は取込に対応していません。"


def _selected_parser_failure_message(
    selected_backend: str,
    result: ParserRegistryResult,
) -> str:
    """明示選択 parser が使えなかった理由を利用者向け文言へ寄せる。"""
    label = _parser_backend_label(selected_backend)
    warning = result.warnings[0] if result.warnings else None
    warning_suffix = f" エラーコード: {warning}" if warning else ""
    if warning and warning.startswith("structured_records_invalid:"):
        return (
            f"文書解析エンジン（{label}）は、ファイル準備の「CSV→JSONL」「Excel→JSONL」「Word→JSONL」が"
            "作った構造化レコードの JSONL を読めませんでした。このファイルはその形式に合わないか、"
            "1 件の記録が大きすぎるため、別の解析エンジンには切り替えずに取込を停止しました。"
            "この文書の「処理レシピ」でファイル準備と文書解析の組み合わせを確認してから"
            f"再実行してください。{warning_suffix}"
        )
    if warning and warning.endswith("_adapter_package_missing"):
        return (
            f"選択した文書解析エンジン（{label}）の実行に必要なパッケージが"
            "parser サービス内に見つかりません。別の解析エンジンには切り替えずに"
            f"取込を停止しました。サービス管理画面で parser-{selected_backend.replace('_', '-')} "
            f"のイメージと依存関係を確認してから再実行してください。{warning_suffix}"
        )
    if warning and warning.endswith("_adapter_feature_flag_disabled"):
        return (
            f"選択した文書解析エンジン（{label}）が無効になっています。"
            "ナレッジベースまたはシステム設定でこの解析エンジンを有効にしてから"
            f"再実行してください。{warning_suffix}"
        )
    if warning and warning.endswith("_adapter_source_unsupported"):
        formats = supported_formats_label(selected_backend)
        formats_suffix = f"（対応形式: {formats}）" if formats else ""
        return (
            f"選択した文書解析エンジン（{label}）はこのファイル形式を処理できません"
            f"{formats_suffix}。"
            "別の解析エンジンには切り替えずに取込を停止しました。対応形式に変換するか、"
            f"設定を変更してから再実行してください。{warning_suffix}"
        )
    if warning and warning.endswith("_adapter_invalid_input"):
        return (
            f"選択した文書解析エンジン（{label}）でファイルを読み取れませんでした。"
            "ファイルが破損しているか、この形式として読み取れません。"
            "元ファイルが正しく開けるか確認し、開ける場合は別の解析エンジンを試すか、"
            f"ファイルを再作成・再変換してから再アップロードしてください。{warning_suffix}"
        )
    if warning and (
        warning.endswith("_adapter_service_unconfigured")
        or warning.endswith("_adapter_service_unreachable")
        or warning.endswith("_adapter_service_invalid_response")
    ):
        return (
            f"選択した文書解析エンジン（{label}）の parser サービスを実行できません。"
            "Enterprise AI など別経路には切り替えずに取込を停止しました。"
            f"サービス管理画面で parser-{selected_backend.replace('_', '-')} の状態とログを"
            f"確認してから再実行してください。{warning_suffix}"
        )
    if result.parser_backend != selected_backend:
        return (
            f"選択した文書解析エンジン（{label}）ではなく "
            f"{_parser_backend_label(result.parser_backend)} の結果になったため、"
            "別経路には切り替えずに取込を停止しました。"
            f"{warning_suffix}"
        )
    return (
        f"選択した文書解析エンジン（{label}）で解析結果を作成できませんでした。"
        "Enterprise AI など別経路には切り替えずに取込を停止しました。"
        f"サービス管理画面で parser-{selected_backend.replace('_', '-')} のログを確認し、"
        f"原因を修正してから再実行してください。{warning_suffix}"
    )


def record_boundary_failure_message(error: RecordBoundaryError) -> str:
    """構造化レコードの記録の境界で分割できなかった理由を利用者向け文言へ寄せる(#2457)。"""
    if error.code == "record_too_large":
        reason = "1 件の記録が chunk の上限を超えています"
    elif error.code == "boundary_count_mismatch":
        reason = "検索用テキストの記録の境界の数が記録の数と合いません"
    else:
        reason = "検索用テキストの記録の境界が壊れています"
    return (
        f"構造化レコードの抽出を記録の単位で分割できませんでした（{reason}）。"
        "記録を結合・切り捨てずに取込を停止しました。抽出結果の確認で本文を編集した場合は、"
        "記録の境界の行を消さずに保存し直すか、文書を再処理してください。"
        f" エラーコード: structured_records_boundary_invalid:{error.code}"
    )


def _parser_backend_label(backend: str) -> str:
    labels = {
        "docling": "Docling",
        "unstructured": "Unstructured",
    }
    return labels.get(backend, backend)


def _parser_result_attributes(result: ParserRegistryResult) -> Mapping[str, object]:
    """parser registry 結果から非機密な trace attribute を作る。"""
    extraction = result.extraction
    return {
        "parser_backend": result.parser_backend,
        "parser_version": result.parser_version,
        "parser_fallback_used": result.fallback_used,
        "chunk_template": result.template,
        "parser_warning_count": len(result.warnings),
        "parser_unsupported": result.unsupported_reason is not None,
        "element_count": len(extraction.elements) if extraction is not None else 0,
        "page_count": len(extraction.pages) if extraction is not None else 0,
        "table_count": len(extraction.tables) if extraction is not None else 0,
    }


def _checkpoint_parser_backend(
    parser_result: ParserRegistryResult,
    source_profile: SourceProfile | None,
) -> str:
    """checkpoint には実際に使う予定の parser backend を残す。"""
    if _uses_external_adapter_extraction(parser_result):
        return parser_result.parser_backend
    if source_profile is not None and source_profile.parser_backend == "local_partition":
        return source_profile.parser_backend
    return parser_result.parser_backend


def _checkpoint_parser_profile(
    parser_result: ParserRegistryResult,
    *,
    fallback: str,
) -> str:
    """checkpoint/quality には user が選んだ実 parser を優先して残す。"""
    extraction = parser_result.extraction
    if extraction is not None and extraction.quality_report is not None:
        report_profile = extraction.quality_report.parser_profile
        if report_profile.strip():
            return report_profile.strip()[:80]
    if extraction is not None:
        for key in ("parser_profile", "source_parser", "external_adapter"):
            artifact_value = extraction.parser_artifacts.get(key)
            if isinstance(artifact_value, str) and artifact_value.strip():
                return artifact_value.strip()[:80]
    if _uses_external_adapter_extraction(parser_result):
        return parser_result.parser_backend[:80]
    return fallback[:80]


def _uses_external_adapter_extraction(parser_result: ParserRegistryResult) -> bool:
    """任意 external adapter が実際に extraction を返したかを判定する。"""
    return parser_result.extraction is not None and _is_external_adapter_backend(
        parser_result.parser_backend
    )


def _is_external_adapter_backend(parser_backend: str) -> bool:
    return parser_backend in {"docling", "unstructured"}


def _extraction_with_parser_context(
    extraction: StructuredExtraction,
    *,
    parser_result: ParserRegistryResult,
    fallback_template: str,
    source_parser: str,
) -> StructuredExtraction:
    """抽出結果へ parser/chunk template lineage を付与する。"""
    chunk_template = (
        parser_result.template
        if parser_result.template != "enterprise_ai_fallback"
        else fallback_template
    )
    effective_source_parser = _source_parser_name(
        extraction,
        parser_result=parser_result,
        fallback=source_parser,
    )
    elements = [
        element.model_copy(
            update={
                "source_parser": element.source_parser or effective_source_parser,
                "metadata": {
                    **element.metadata,
                    "source_parser": element.source_parser or effective_source_parser,
                    "chunk_template": element.metadata.get("chunk_template") or chunk_template,
                },
            }
        )
        for element in extraction.elements
    ]
    parser_artifacts = {
        **extraction.parser_artifacts,
        "parser_backend": parser_result.parser_backend,
        "parser_version": parser_result.parser_version,
        "fallback_used": parser_result.fallback_used,
        "chunk_template": chunk_template,
        "source_parser": effective_source_parser,
    }
    return extraction.model_copy(
        update={
            "warnings": _dedupe_text([*extraction.warnings, *parser_result.warnings]),
            "elements": elements,
            "parser_artifacts": parser_artifacts,
        }
    )


def _passthrough_derivation(
    *,
    profile: str,
    source_sha: str,
    content_type: str,
    converter_name: str = "passthrough",
    warnings: list[str] | None = None,
) -> SourceDerivation:
    """変換しない(passthrough / no-op / 縮退)場合の派生系譜。derived は原本と同一を指す。"""
    return SourceDerivation(
        derivation_id=uuid4().hex,
        preprocess_profile=profile,
        converted=False,
        converter_name=converter_name,
        converter_version="v1",
        source_content_type=content_type,
        source_sha256=source_sha,
        derived_content_type=content_type,
        derived_sha256=source_sha,
        warnings=warnings or [],
    )


def _extraction_with_source_derivation(
    extraction: StructuredExtraction,
    derivation: SourceDerivation,
) -> StructuredExtraction:
    """派生系譜(溯源)を抽出 metadata(parser_artifacts)へ刻む。"""
    parser_artifacts = {
        **extraction.parser_artifacts,
        "source_derivation": derivation.model_dump(mode="json"),
    }
    return extraction.model_copy(update={"parser_artifacts": parser_artifacts})


def _extraction_with_page_labels(
    extraction: StructuredExtraction, source_bytes: bytes, content_type: str
) -> StructuredExtraction:
    """解析した PDF のページラベルを parser_artifacts に残す（物理頁と違うときだけ。#1244）。"""
    if not source_bytes.startswith(b"%PDF") and "pdf" not in (content_type or "").casefold():
        return extraction
    artifact = page_labels_artifact(pdf_page_labels(source_bytes))
    if artifact is None:
        return extraction
    parser_artifacts = {**extraction.parser_artifacts, PAGE_LABELS_ARTIFACT_KEY: artifact}
    return extraction.model_copy(update={"parser_artifacts": parser_artifacts})


def _source_derivation_id(extraction: StructuredExtraction) -> str | None:
    """抽出 metadata から派生系譜 ID を取り出す(無ければ None)。"""
    derivation = extraction.parser_artifacts.get("source_derivation")
    if isinstance(derivation, Mapping):
        value = derivation.get("derivation_id")
        if isinstance(value, str) and value:
            return value
    return None


def _source_derivation_profile(extraction: StructuredExtraction) -> str | None:
    """抽出 metadata からファイル準備 profile を取り出す。"""
    derivation = extraction.parser_artifacts.get("source_derivation")
    if isinstance(derivation, Mapping):
        value = derivation.get("preprocess_profile")
        if isinstance(value, str) and value:
            return value
    return None


def _chunks_with_source_derivation(chunks: list[Chunk], derivation_id: str | None) -> list[Chunk]:
    """各 chunk metadata へ派生系譜 ID を付与する(chunk → 派生原本 → 原本の追跡)。"""
    if not derivation_id:
        return chunks
    for chunk in chunks:
        chunk.metadata["source_derivation_id"] = derivation_id
    return chunks


def _chunk_view_to_chunk(view: DocumentChunkView) -> Chunk:
    """保存済み chunk view を embedding/index 用 Chunk へ戻す。"""
    metadata = {
        key: value
        for key, value in view.metadata.items()
        if isinstance(value, str | int | float | bool) or value is None
    }
    metadata["document_id"] = view.document_id
    metadata["chunk_id"] = view.chunk_id
    start = _stored_chunk_int(metadata.get("start_offset"), default=0)
    end = _stored_chunk_int(metadata.get("end_offset"), default=len(view.text))
    return Chunk(
        text=view.text,
        index=view.chunk_index,
        start_offset=start,
        end_offset=end,
        metadata=metadata,
    )


def _chunks_with_context_headers(
    chunks: list[Chunk],
    document_title: str,
    *,
    enabled: bool,
) -> list[Chunk]:
    """表示本文を変えず、索引用の決定論ヘッダだけ metadata へ付ける。"""
    contextualized: list[Chunk] = []
    for chunk in chunks:
        metadata = dict(chunk.metadata)
        metadata["context_header_enabled"] = enabled
        section_path = str(metadata.get("section_path") or "").strip()
        header = " > ".join(part for part in (document_title.strip(), section_path) if part)
        if enabled and header:
            metadata["context_header"] = header
        else:
            metadata.pop("context_header", None)
        contextualized.append(
            Chunk(
                text=chunk.text,
                index=chunk.index,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                metadata=metadata,
            )
        )
    return contextualized


def _embedding_input_with_context_header(chunk: Chunk, document_title: str = "") -> str:
    """chunk の embedding 入力へ「文書名 > section_path」の文脈ヘッダを前置する。"""
    header = str(chunk.metadata.get("context_header") or "").strip()
    if not header:
        section_path = str(chunk.metadata.get("section_path") or "").strip()
        header = " > ".join(part for part in (document_title.strip(), section_path) if part)
    if not header:
        return chunk.text
    return f"{header}\n{chunk.text}"


def _stored_chunk_int(value: object, *, default: int) -> int:
    """保存済み chunk metadata の整数値を安全に読む。"""
    if isinstance(value, bool) or value is None:
        return default
    if not isinstance(value, str | int | float):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_artifact_extension(content_type: str) -> str:
    """canonical artifact の保存拡張子を content_type から決める(表示・取り回し用)。"""
    normalized = (content_type or "").split(";", 1)[0].strip().casefold()
    mapping = {
        "application/pdf": ".pdf",
        "application/json": ".json",
        # 前処理（csv_to_jsonl / excel_to_jsonl / word_to_jsonl）の構造化レコードの JSONL（#2457）。
        STRUCTURED_RECORDS_CONTENT_TYPE: STRUCTURED_RECORDS_FILE_EXTENSION,
        "application/csv": ".csv",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "text/markdown": ".md",
        "text/html": ".html",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "application/zip": ".zip",
    }
    return mapping.get(normalized, ".bin")


def _prepared_artifact_file_name(original_file_name: str, content_type: str) -> str:
    raw_name = (original_file_name or "document").replace("\\", "/").rsplit("/", 1)[-1]
    raw_name = re.sub(r"[\x00-\x1f\x7f]+", "_", raw_name).strip(" .") or "document"
    base = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    base = (base[:220]).rstrip(" .") or "document"
    return f"{base}__prepared{_canonical_artifact_extension(content_type)}"


def _source_derivation_from_preprocess_artifact(
    artifact: DocumentPreprocessArtifact,
) -> SourceDerivation:
    return SourceDerivation(
        derivation_id=artifact.derivation_id,
        preprocess_profile=artifact.profile,
        converted=artifact.converted,
        converter_name=artifact.converter_name
        or ("preprocess" if artifact.converted else "passthrough"),
        converter_version=artifact.converter_version or "v1",
        source_content_type=artifact.source_content_type,
        source_sha256=artifact.source_sha256,
        derived_object_path=(artifact.object_storage_path if artifact.converted else None),
        derived_content_type=artifact.content_type,
        derived_sha256=artifact.sha256,
        page_map={str(key): int(value) for key, value in artifact.page_map.items()},
        warnings=list(artifact.warnings),
    )


def _source_parser_name(
    extraction: StructuredExtraction,
    *,
    parser_result: ParserRegistryResult,
    fallback: str,
) -> str:
    """抽出 payload / parser result から source parser 名を決める。"""
    value = extraction.parser_artifacts.get("source_parser")
    if isinstance(value, str) and value.strip():
        return value.strip()[:80]
    if _uses_external_adapter_extraction(parser_result):
        return parser_result.parser_backend[:80]
    if parser_result.parser_backend == "local_partition":
        return parser_result.template[:80]
    return fallback[:80]


def _text_for_chunking(extraction: StructuredExtraction) -> str:
    """抽出結果から索引用テキストを作る(構造化レコードの記録の境界のマーカーは除く。#2457)。"""
    return record_boundary_plain_text(extraction).strip()


def _pdf_segments_for_ingestion(
    source_bytes: bytes,
    *,
    content_type: str,
    max_pages_per_segment: int,
    max_segments: int,
    enabled: bool,
) -> list[PdfPageSegment]:
    """取込対象 PDF を segment 化する。PDF でなければ空 list。"""
    if not enabled or _observability_content_type(content_type) != "application/pdf":
        return []
    segments = split_pdf_page_segments(
        source_bytes,
        max_pages_per_segment=max_pages_per_segment,
    )
    if len(segments) > max_segments:
        raise IngestionUserError(
            "PDF のページ分割数が上限を超えています。"
            f"max_segments={max_segments}, actual={len(segments)}"
        )
    return segments


def _checkpoint_segments_for_source(
    *,
    document_id: str,
    source_bytes: bytes,
    content_type: str,
    source_profile: SourceProfile | None,
    parser_backend: str,
    parser_profile: str,
    recipe_id: str | None,
    max_pages_per_segment: int,
    max_segments: int,
    segmentation_enabled: bool,
) -> list[IngestionSegment]:
    """source bytes から永続 checkpoint の初期 segment を作る。"""
    pdf_segments = _pdf_segments_for_ingestion(
        source_bytes,
        content_type=content_type,
        max_pages_per_segment=max_pages_per_segment,
        max_segments=max_segments,
        enabled=segmentation_enabled,
    )
    if pdf_segments and not (len(pdf_segments) == 1 and pdf_segments[0].page_count <= 1):
        return [
            IngestionSegment(
                segment_id=_pdf_checkpoint_segment_id(document_id, segment, recipe_id=recipe_id),
                document_id=document_id,
                recipe_id=recipe_id,
                status="QUEUED",
                parser_backend=parser_backend,
                parser_profile=parser_profile,
                page_start=segment.page_start,
                page_end=segment.page_end,
                progress_unit="page",
                progress_start=segment.page_start,
                progress_end=segment.page_end,
            )
            for segment in pdf_segments
        ]
    office_segments = _office_checkpoint_segments_for_source(
        document_id=document_id,
        source_bytes=source_bytes,
        content_type=content_type,
        source_profile=source_profile,
        parser_backend=parser_backend,
        parser_profile=parser_profile,
        recipe_id=recipe_id,
        max_segments=max_segments,
    )
    if office_segments:
        return office_segments
    single_pdf_page = bool(pdf_segments and pdf_segments[0].page_count <= 1)
    source_is_page = single_pdf_page or _observability_content_type(content_type).startswith(
        "image/"
    )
    return [
        IngestionSegment(
            segment_id=f"{_segment_scope(document_id, recipe_id)}:source",
            document_id=document_id,
            recipe_id=recipe_id,
            status="QUEUED",
            parser_backend=parser_backend,
            parser_profile=parser_profile,
            page_start=1 if source_is_page else None,
            page_end=1 if source_is_page else None,
            progress_unit="page" if source_is_page else "source",
            progress_start=1 if source_is_page else None,
            progress_end=1 if source_is_page else None,
        )
    ]


def _office_checkpoint_segments_for_source(
    *,
    document_id: str,
    source_bytes: bytes,
    content_type: str,
    source_profile: SourceProfile | None,
    parser_backend: str,
    parser_profile: str,
    recipe_id: str | None,
    max_segments: int,
) -> list[IngestionSegment]:
    """OpenXML Office を slide/sheet 単位の checkpoint にする。"""
    office_kind = _office_checkpoint_kind(
        content_type=content_type,
        source_profile=source_profile,
    )
    if office_kind is None:
        return []
    if office_kind == "slide":
        numbers = _openxml_member_numbers(source_bytes, r"ppt/slides/slide(\d+)\.xml")
    else:
        numbers = _openxml_member_numbers(source_bytes, r"xl/worksheets/sheet(\d+)\.xml")
    if not numbers:
        return []
    if len(numbers) > max_segments:
        raise IngestionUserError(
            "Office の論理 segment 数が上限を超えています。"
            f"max_segments={max_segments}, actual={len(numbers)}"
        )
    return [
        IngestionSegment(
            segment_id=f"{_segment_scope(document_id, recipe_id)}:{office_kind}{number}",
            document_id=document_id,
            recipe_id=recipe_id,
            status="QUEUED",
            parser_backend=parser_backend,
            parser_profile=parser_profile,
            page_start=number,
            page_end=number,
            progress_unit=office_kind,
            progress_start=number,
            progress_end=number,
        )
        for number in numbers
    ]


def _office_checkpoint_kind(
    *,
    content_type: str,
    source_profile: SourceProfile | None,
) -> str | None:
    extension = (source_profile.extension if source_profile is not None else None) or ""
    normalized = _observability_content_type(content_type)
    if extension == ".pptx" or normalized.endswith("officedocument.presentationml.presentation"):
        return "slide"
    if extension == ".xlsx" or normalized.endswith("officedocument.spreadsheetml.sheet"):
        return "sheet"
    return None


def _openxml_member_numbers(source_bytes: bytes, pattern: str) -> list[int]:
    regex = re.compile(pattern)
    try:
        with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
            numbers = []
            for name in archive.namelist():
                match = regex.fullmatch(name)
                if match:
                    numbers.append(int(match.group(1)))
    except (zipfile.BadZipFile, ValueError):
        return []
    return sorted(set(numbers))


def _failed_checkpoint_count(checkpoint_segments: Sequence[IngestionSegment]) -> int:
    return sum(
        1
        for segment in checkpoint_segments
        if segment.status == "FAILED" or segment.error_code == "retry_failed_segment"
    )


def _extraction_with_artifact_cache_metadata(
    extraction: StructuredExtraction,
    *,
    artifact_kind: str,
    document_id: str,
    trace_id: str,
    segment: IngestionSegment | None = None,
    parse_key: str | None = None,
) -> StructuredExtraction:
    """保存先の cache payload を後方互換な parser_artifacts で自描述にする。"""
    parser_artifacts: dict[str, ExtractionArtifactValue] = {
        **extraction.parser_artifacts,
        "extraction_artifact_schema_version": EXTRACTION_ARTIFACT_SCHEMA_VERSION,
        "extraction_artifact_kind": artifact_kind,
        "extraction_artifact_document_id": document_id,
        "extraction_artifact_trace_id": trace_id,
    }
    if parse_key is not None:
        parser_artifacts["extraction_artifact_parse_key"] = parse_key
    if segment is not None:
        parser_artifacts["extraction_artifact_segment_id"] = segment.segment_id
        if segment.page_start is not None:
            parser_artifacts["extraction_artifact_page_start"] = segment.page_start
        if segment.page_end is not None:
            parser_artifacts["extraction_artifact_page_end"] = segment.page_end
    return extraction.model_copy(update={"parser_artifacts": parser_artifacts})


def _cached_segment_artifact_matches(
    extraction: StructuredExtraction,
    segment: IngestionSegment,
    *,
    parse_key: str | None = None,
) -> bool:
    """保存先の segment artifact が現在の checkpoint と一致するか確認する。

    ``parse_key`` を渡すと、段の PDF と解析・前処理の設定の鍵も一致する artifact だけを使う。
    """
    artifacts = extraction.parser_artifacts
    if parse_key is not None and (
        _artifact_string(artifacts.get("extraction_artifact_parse_key")) != parse_key
    ):
        return False
    return (
        _artifact_int(artifacts.get("extraction_artifact_schema_version"))
        == EXTRACTION_ARTIFACT_SCHEMA_VERSION
        and _artifact_string(artifacts.get("extraction_artifact_kind")) == "segment"
        and _artifact_string(artifacts.get("extraction_artifact_document_id"))
        == segment.document_id
        and _artifact_string(artifacts.get("extraction_artifact_segment_id")) == segment.segment_id
        and _artifact_int(artifacts.get("extraction_artifact_page_start")) == segment.page_start
        and _artifact_int(artifacts.get("extraction_artifact_page_end")) == segment.page_end
    )


def _cached_full_artifact_matches(
    extraction: StructuredExtraction,
    document_id: str,
    *,
    parse_key: str,
) -> bool:
    """保存先の full extraction artifact が現在文書の、同じ入力と設定のものか確認する。

    鍵を持たない(#2076 の前に保存した)artifact は再利用しない(もう一度解析する)。
    """
    artifacts = extraction.parser_artifacts
    return (
        _artifact_int(artifacts.get("extraction_artifact_schema_version"))
        == EXTRACTION_ARTIFACT_SCHEMA_VERSION
        and _artifact_string(artifacts.get("extraction_artifact_kind")) == "full"
        and _artifact_string(artifacts.get("extraction_artifact_document_id")) == document_id
        and _artifact_string(artifacts.get("extraction_artifact_parse_key")) == parse_key
    )


def _artifact_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _artifact_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _segment_scope(document_id: str, recipe_id: str | None) -> str:
    return document_id if recipe_id is None else f"{document_id}:{recipe_id[:12]}"


def _pdf_checkpoint_segment_id(
    document_id: str,
    segment: PdfPageSegment,
    *,
    recipe_id: str | None = None,
) -> str:
    return f"{_segment_scope(document_id, recipe_id)}:p{segment.page_start}-{segment.page_end}"


# 項目抽出の単位の結果の保存の形と、方式の版(#2720)。単位の分け方・頭書き・応答の形・統合の
# 規則を変えたら版を上げ、前の方式の結果を使い直さない。
_FIELD_CHECKPOINT_VERSION = 1
FIELD_EXTRACTION_METHOD_VERSION = "units-1"

# 項目抽出の失敗の分類ごとの文(何が起きたか / どう直すか)。本文・値・応答の本文は入れない。
_FIELD_FAILURE_REASONS: dict[str, str] = {
    "input_too_large": (
        "項目抽出の入力がモデルの入力の上限を超え、本文を分け直しても収まりませんでした"
    ),
    "truncated": "項目抽出の応答がモデルの出力の上限で途中で終わりました",
    "invalid_response": "項目抽出の応答を項目の一覧として読めませんでした",
    "request_rejected": (
        "項目抽出の要求をモデルが受け付けませんでした（入力の上限の超過とは判定できない要求の誤り）"
    ),
    "safety": "項目抽出の要求がモデルの安全のための確認で拒否されました",
    "transient": (
        "Enterprise AI の一時的な障害（流量の制限・サービスの不可用・時間切れ・接続）が、"
        "再試行の後も続きました"
    ),
    "unknown": "項目抽出に失敗しました（分類できない誤り）",
}
_FIELD_FAILURE_HINTS: dict[str, str] = {
    "input_too_large": (
        "システム設定 › モデル の既定のテキストモデルの「入力の上限（tokens）」が実際のモデルの"
        "上限を超えていないかと、RAG_FIELD_EXTRACTION_INPUT_TOKEN_PERCENT を確認してから"
        "再実行してください。"
    ),
    "truncated": (
        "システム設定 › モデル の既定のテキストモデルの「出力の上限（tokens）」を確認するか、"
        "項目の数を減らしてから再実行してください。"
    ),
    "invalid_response": (
        "Enterprise AI の応答形式（モデルの設定）を確認してから再実行してください。"
    ),
    "request_rejected": (
        "システム設定 › モデル の既定のテキストモデルの設定（payload のテンプレート・"
        "応答形式）と項目の定義を確認してから再実行してください。"
    ),
    "safety": "項目の定義と文書の内容を確認してから再実行してください。",
    "transient": "時間をおいて再実行してください。",
    "unknown": (
        "項目抽出設定、field schema、Enterprise AI の応答形式を確認してから再実行してください。"
    ),
}


def _field_extraction_failure_message(exc: FieldExtractionFailed, *, checkpoints: bool) -> str:
    """項目抽出の失敗の、利用者向けの文(分類・HTTP の状態・単位の数だけで作る)。"""
    run = exc.run
    status = f"（HTTP {exc.status}）" if exc.status else ""
    counts = f"{run.unit_count} 部分のうち {run.failed} 部分が失敗"
    if run.skipped:
        counts += f"、{run.skipped} 部分は未実行"
    parts = [
        f"{_FIELD_FAILURE_REASONS[exc.category]}{status}（{counts}）。",
        "別経路には切り替えずに取込を停止しました。",
        _FIELD_FAILURE_HINTS[exc.category],
    ]
    if checkpoints and run.succeeded:
        parts.append(f"成功した {run.succeeded} 部分の結果は、再実行のときに使い直します。")
    return "".join(parts)


def _field_checkpoint_scope(
    model_id: str, field_defs: Sequence[FieldDefinition], prompt: str, schema_json: str
) -> str:
    """単位の結果を使い直してよい範囲(方式の版・モデル・項目の定義・指示・応答の形)。"""
    payload = json.dumps(
        [
            FIELD_EXTRACTION_METHOD_VERSION,
            model_id.strip(),
            field_schema_hash(field_defs),
            prompt,
            schema_json,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_field_checkpoint(data: bytes | None) -> list[FieldValueCandidate] | None:
    """保存した単位の結果。無い・形が合わないときは None(呼び直す)。"""
    if data is None:
        return None
    try:
        payload = json.loads(data)
        if payload.get("version") != _FIELD_CHECKPOINT_VERSION:
            return None
        return [
            FieldValueCandidate(
                field=ExtractionField.model_validate(item["field"]),
                raw_value=str(item["raw_value"]),
            )
            for item in payload["candidates"]
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _safe_artifact_key_part(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    if cleaned in {".", ".."}:
        return "segment"
    return cleaned[:160] or "segment"


def document_artifact_prefixes(settings: object, document_id: str) -> list[str]:
    """文書の取込の成果物（正規化原本・抽出 artifact・segment artifact）を置く prefix を返す。

    成果物の key は `{prefix}/{document_id}/{trace_id}...` なので、この prefix を消せば
    参照が残っていない過去の取込の成果物も含めて後始末できる（文書の削除。#303）。
    """
    document_key = _safe_artifact_key_part(document_id)
    prefixes = [
        _safe_artifact_prefix(
            getattr(settings, "rag_canonical_artifact_prefix", "artifacts/canonical")
        ),
        _safe_artifact_prefix(
            getattr(settings, "rag_extraction_artifact_prefix", "artifacts/extractions")
        ),
    ]
    return list(dict.fromkeys(f"{prefix}/{document_key}/" for prefix in prefixes))


def _safe_artifact_prefix(value: object) -> str:
    """保存先の artifact prefix を監査しやすい key path へ正規化する。"""
    raw = str(value or "").replace("\\", "/")
    parts = [
        _safe_artifact_key_part(part)
        for part in raw.strip().strip("/").split("/")
        if part.strip() not in {"", ".", ".."}
    ]
    return "/".join(parts) or "artifacts/extractions"


def _dedupe_text(values: Sequence[str]) -> list[str]:
    """空文字を落として順序を保ったまま重複排除する。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


async def _observe_ingestion_stage[T](
    trace_id: str,
    stage: str,
    operation: Awaitable[T],
    *,
    attributes: Mapping[str, object] | None = None,
    result_attributes: Callable[[T], Mapping[str, object]] | None = None,
) -> T:
    """非同期の取込 stage を metrics / trace span に記録する。"""
    started_at = perf_counter()
    base_attributes = dict(attributes or {})
    try:
        result = await operation
    except asyncio.CancelledError as exc:
        elapsed = perf_counter() - started_at
        _record_ingestion_stage(trace_id, stage, "cancelled", elapsed, base_attributes, exc)
        raise
    except Exception as exc:
        elapsed = perf_counter() - started_at
        _record_ingestion_stage(trace_id, stage, "error", elapsed, base_attributes, exc)
        raise
    elapsed = perf_counter() - started_at
    if result_attributes is not None:
        base_attributes.update(result_attributes(result))
    _record_ingestion_stage(trace_id, stage, "success", elapsed, base_attributes)
    return result


async def _observe_cpu_ingestion_stage[T](
    trace_id: str,
    stage: str,
    operation: Callable[[], T],
    *,
    attributes: Mapping[str, object] | None = None,
    result_attributes: Callable[[T], Mapping[str, object]] | None = None,
) -> T:
    """CPU バウンドな同期 stage を thread へ退避し、event loop を塞がずに記録する。"""
    return await _observe_ingestion_stage(
        trace_id,
        stage,
        asyncio.to_thread(operation),
        attributes=attributes,
        result_attributes=result_attributes,
    )


def _record_ingestion_stage(
    trace_id: str,
    stage: str,
    outcome: TraceOutcome,
    elapsed: float,
    attributes: Mapping[str, object],
    error: BaseException | None = None,
) -> None:
    """低 cardinality の stage metric と安全な trace span を記録する。"""
    record_ingestion_stage(stage, outcome, elapsed)
    record_trace_span(
        trace_id=trace_id,
        span_name=stage,
        outcome=outcome,
        seconds=elapsed,
        attributes=dict(attributes),
        error=error,
    )


def _entity_index_result_attributes(index: EntityIndex) -> Mapping[str, object]:
    return {
        "entity_count": len(index.entities),
        "entity_alias_count": index.alias_count,
        "entity_chunk_link_count": len(index.links),
    }


def _graph_index_result_attributes(graph_index: GraphIndex) -> Mapping[str, object]:
    """文書構造索引の構築結果から件数だけを trace attribute にする。"""
    return {
        "graph_entity_count": len(graph_index.entities),
        "graph_relationship_count": len(graph_index.relationships),
        "graph_entity_chunk_link_count": len(graph_index.entity_chunk_links),
    }


def _extraction_structure_attributes(
    extraction: StructuredExtraction,
) -> Mapping[str, object]:
    """構造化抽出から非機密な件数だけを trace attribute にする。"""
    quality = extraction.quality_report or build_ingestion_quality_report(extraction)
    return {
        "element_count": len(extraction.elements),
        "table_count": quality.table_count,
        "figure_count": quality.figure_count,
        "formula_count": quality.formula_count,
        "page_count": quality.page_count,
        "asset_count": max(
            len(extraction.assets),
            _raw_artifact_count(extraction.parser_artifacts, STRUCTURE_ASSET_COUNT_KEYS),
        ),
        "low_confidence_count": quality.low_confidence_count,
        "failed_segment_count": quality.failed_segment_count,
    }


def _raw_extraction_structure_attributes(
    extracted: Mapping[str, object],
) -> Mapping[str, object]:
    """VLM 生 payload から本文を見ずに構造件数だけを読む。"""
    elements = _raw_mapping_items(extracted.get("elements"))
    pages_payload = _raw_mapping_items(extracted.get("pages"))
    tables = _raw_mapping_items(extracted.get("tables"))
    assets = _raw_mapping_items(extracted.get("assets"))
    artifacts = _raw_mapping(extracted.get("parser_artifacts"))
    pages: set[int] = set()
    table_count = sum(1 for item in elements if _raw_mapping_label(item.get("kind")) == "table")
    figure_count = sum(
        1
        for item in elements
        if _raw_mapping_label(item.get("kind")) in {"figure", "figure_caption"}
    )
    formula_count = sum(
        1
        for item in elements
        if _raw_mapping_label(item.get("kind")) in {"formula", "equation"}
        or _raw_mapping_label(item.get("content_kind")) == "equation"
    )
    for item in elements:
        if page_number := _raw_page_number(item.get("page_number")):
            pages.add(page_number)
    for page_payload in pages_payload:
        if _raw_string_items(page_payload.get("element_ids")):
            page_number = _raw_page_number(page_payload.get("page_number"))
            if page_number is not None:
                pages.add(page_number)
    for container in (*tables, *assets):
        page_number = _raw_page_number(container.get("page_number"))
        if page_number is not None:
            pages.add(page_number)
    declared_page_count = max(
        len(pages_payload),
        _raw_artifact_count(artifacts, STRUCTURE_PAGE_COUNT_KEYS),
    )
    return {
        "element_count": len(elements),
        "table_count": max(
            table_count,
            len(tables),
            _raw_artifact_count(artifacts, STRUCTURE_TABLE_COUNT_KEYS),
        ),
        "figure_count": max(
            figure_count,
            sum(1 for asset in assets if _raw_is_figure_asset_kind(asset.get("kind"))),
            _raw_artifact_count(artifacts, STRUCTURE_FIGURE_COUNT_KEYS),
        ),
        "formula_count": max(
            formula_count,
            _raw_artifact_count(artifacts, STRUCTURE_FORMULA_COUNT_KEYS),
        ),
        "page_count": max(declared_page_count, len(pages)),
        "asset_count": max(
            len(assets),
            _raw_artifact_count(artifacts, STRUCTURE_ASSET_COUNT_KEYS),
        ),
    }


def _raw_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _raw_mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    items: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            items.append(item)
    return items


def _raw_string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _raw_mapping_label(value: object) -> str:
    return str(value).strip().casefold() if value is not None else ""


def _raw_page_number(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _raw_artifact_count(artifacts: Mapping[str, object], keys: Sequence[str]) -> int:
    return max((_raw_positive_int(artifacts.get(key)) for key in keys), default=0)


def _raw_positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else 0
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _raw_is_figure_asset_kind(kind: object) -> bool:
    return _raw_mapping_label(kind) in FIGURE_ASSET_KINDS


def _observability_document_type(value: object) -> str | None:
    """ログ・trace 用 document type を既知カテゴリに正規化する。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized in OBSERVABILITY_DOCUMENT_TYPES else "other"


def _observability_content_type(value: str) -> str:
    """ログ・trace 用 MIME type を低 cardinality に正規化する。"""
    normalized = value.split(";", maxsplit=1)[0].strip().lower()
    if normalized in {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "text/plain",
        "application/octet-stream",
    }:
        return normalized
    if normalized.startswith("image/"):
        return "image/other"
    if normalized.startswith("text/"):
        return "text/other"
    if normalized:
        return "other"
    return "application/octet-stream"
