"""ドキュメント API。アップロード・一覧・取込(抽出→索引)。"""

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import PurePath
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import uuid4

from charset_normalizer import from_bytes
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
)
from pr_backend_core.api import OffsetParams, empty_page, offset_params, paginate
from pr_backend_core.oracle_errors import oracle_error_codes
from rag_parser_core.structured_records import RecordBoundaryError, record_boundary_plain_text

from app.api.errors import DocumentFileMissingError
from app.clients.document_cleanup_store import DocumentCleanupStore, DocumentCleanupTargets
from app.clients.object_storage import ObjectStorageClient, describe_upload_storage_error
from app.clients.oci_genai import EMBEDDING_INPUT_MAX_CHARS
from app.clients.oracle import (
    DocumentDeleteBlockedByRunningIngestionError,
    DocumentReviewEditConflictError,
    DocumentSectionsConflictError,
    DocumentSupersessionError,
    OracleClient,
    ReviewedExtractionWrite,
    is_transient_oracle_error,
    oracle_error_log_fields,
)
from app.clients.oracle_bulk_ingestion_store import OracleBulkIngestionStore
from app.config import (
    CHUNKING_STRATEGIES_WITH_MIN_CHARS,
    FIXED_DELIMITER_STRATEGY,
    LEGACY_CHUNKING_STRATEGY_ALIASES,
    PREVIOUS_DEFAULT_CHUNK_DELIMITER,
    IngestionQueueConcurrency,
    Settings,
    get_settings,
)
from app.db_degradation import load_or_degrade
from app.rag.bulk_ingestion import BulkIngestionStore, accept_bulk_ingestion
from app.rag.chunking import Chunk, chunk_extraction_with_strategy
from app.rag.chunking_small_to_big import SMALL_TO_BIG_STRATEGY, build_parent_child_chunks
from app.rag.chunking_strategy import resolve_small_to_big_params
from app.rag.document_cleanup import DocumentCleanupOutcome, run_document_cleanup
from app.rag.document_crop import (
    DocumentSourceNotFoundError,
    crop_png,
    load_parsed_source,
    page_sizes,
    render_page_png,
)
from app.rag.document_sections import removed_source_ids, section_errors
from app.rag.document_sections_service import (
    document_sections,
    sections_extraction,
)
from app.rag.extraction_field_adapter import field_extraction_outcome, load_field_schema
from app.rag.ingestion import (
    IngestionCancelledError,
    IngestionPipeline,
    IngestionSuccessOutcome,
    IngestionTimeoutError,
    IngestionUserError,
    document_artifact_prefixes,
    record_boundary_failure_message,
)
from app.rag.ingestion_language import artifact_language_compatible, ingestion_release_scope
from app.rag.ingestion_progress import track_ingestion_progress
from app.rag.ingestion_worker import request_ingestion_worker_wakeup
from app.rag.kb_adapter_config import (
    KbAdapterConfigError,
    KnowledgeBaseAdapterConfig,
    resolve_effective_adapter_config,
    resolve_effective_settings,
)
from app.rag.language_pack_releases import active_release, pack_from_release
from app.rag.layer_fingerprint import (
    changed_layer_inputs,
    current_field_definitions,
    current_layer_inputs,
    recorded_layer_fingerprint,
)
from app.rag.navigation import build_navigation_tree
from app.rag.parser_source_guard import check_parser_source, settings_convert_source
from app.rag.published_language import initialized_release_store
from app.rag.rate_limit import enforce_rate_limit
from app.rag.request_context import current_audit_request_context
from app.rag.search_answer_profile_config import MAX_SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES
from app.rag.section_map_service import refresh_document_section_map
from app.rag.section_rules import (
    extraction_sections,
    load_section_rules,
)
from app.rag.source_profile import build_source_profile, rejected_upload_format_message
from app.rag.variant_keys import (
    compute_chunk_set_id,
    compute_document_recipe_extraction_id,
    compute_extraction_recipe_id,
    compute_graph_layer_id,
    compute_metadata_layer_id,
    compute_nav_layer_id,
    extraction_recipe_subset,
)
from app.rag.variant_planner import MaterializationPlan, plan_document_materializations
from app.schemas.common import ApiResponse, Page
from app.schemas.document import (
    BULK_INGESTION_MAX_DOCUMENTS,
    INGESTION_HEAVY_PHASES,
    BatchUploadFailedItem,
    BatchUploadResult,
    BulkIngestionRequest,
    BulkIngestionResult,
    DocumentApproveRequest,
    DocumentChunkPreviewRequest,
    DocumentChunkPreviewResponse,
    DocumentChunkPreviewStats,
    DocumentChunkSet,
    DocumentChunkSetLayerStatuses,
    DocumentChunkView,
    DocumentClassification,
    DocumentDeleteImpact,
    DocumentDeleteResult,
    DocumentDetail,
    DocumentExtractionExport,
    DocumentExtractionExportFormat,
    DocumentIdList,
    DocumentLayerStatusName,
    DocumentMaterializationLayerStatus,
    DocumentPreprocessArtifact,
    DocumentPreviewPage,
    DocumentPreviewPages,
    DocumentProcessingConfig,
    DocumentRecipeCreateRequest,
    DocumentRecipeDeleteResult,
    DocumentRecipeStep,
    DocumentRecipeStepStatus,
    DocumentRecipeView,
    DocumentReviewEditsRequest,
    DocumentSectionsData,
    DocumentSectionsSaveRequest,
    DocumentSummary,
    DocumentSupersededByRequest,
    DocumentTableCellTextEdit,
    DuplicateDocumentRef,
    FileStatus,
    IngestionJob,
    IngestionJobLease,
    IngestionJobPhase,
    IngestionJobProgress,
    IngestionJobStatus,
    IngestionQueueWait,
    IngestionSegment,
    ParserSourceNotice,
    SourceProfile,
    UploadResult,
)
from app.schemas.extraction import (
    MARKDOWN_HEADING,
    SEARCHABLE_ELEMENT_KINDS,
    DocumentElement,
    DocumentNavigationNode,
    ExtractionAsset,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
    numbered_heading,
)
from app.schemas.knowledge_base import (
    DocumentKnowledgeBaseReplaceRequest,
    KnowledgeBaseRef,
)
from app.schemas.language_pack_release import LanguagePackIndexRelation
from app.schemas.search import RetrievedChunk, normalize_search_id_list

router = APIRouter()
logger = logging.getLogger(__name__)
SOURCE_SIZE_MISMATCH_MESSAGE = "原本ファイルのサイズがアップロード時と一致しません。"
SOURCE_HASH_MISMATCH_MESSAGE = "原本ファイルの SHA-256 がアップロード時と一致しません。"
INGESTION_JOB_CANCELLED_MESSAGE = "利用者によりキャンセルされました。"
UPLOAD_STORAGE_FAILED_MESSAGE = (
    "原本を保存先に保存できませんでした。システム設定 > アップロード保存先 の設定を確認してから、"
    "もう一度アップロードしてください。"
)
# rag_documents.file_name は VARCHAR2(512)（BYTE 長）。日本語の長いファイル名が DB の INSERT で
# 失敗しないよう、表示・保存用のファイル名は文字数と UTF-8 のバイト数の両方で切り詰める（#280）。
MAX_UPLOAD_FILE_NAME_CHARS = 255
MAX_UPLOAD_FILE_NAME_BYTES = 512
_MAX_PRESERVED_SUFFIX_CHARS = 16
CHUNK_SET_PUBLISH_ERROR_MESSAGE = "索引の公開設定に失敗しました。時間をおいて再実行してください。"
DELETE_BLOCKING_INGESTION_STATUSES = frozenset({IngestionJobStatus.RUNNING})
# cancel API が CANCELLED へ遷移できる状態。
_CANCELLABLE_INGESTION_JOB_STATUSES = (IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING)
# lease を持つ実行が、自分の実行として結果を書ける・後始末できる job の状態(#359)。RUNNING は
# 実行中、CANCELLED は実行中に利用者が取り消した job(取り消しの API は lease を変えない)。
# stale の回復は job を QUEUED(lease を外す)か FAILED に戻すため、どちらも自分の実行ではない。
_LEASE_HELD_INGESTION_JOB_STATUSES = frozenset(
    {IngestionJobStatus.RUNNING, IngestionJobStatus.CANCELLED}
)
# ブラウザが開くとスクリプトを実行しうる形式。原本配信では CSP sandbox を付ける。
SCRIPTABLE_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
    }
)
# 親子階層（small-to-big）の分割パラメータ(文書レシピの項目名)。分割方式が small_to_big の
# ときだけ分割結果に効くので、差分(drift)の判定もそのときだけ比べる。
SMALL_TO_BIG_PROCESSING_CONFIG_FIELDS: tuple[str, ...] = (
    "chunk_child_target_chars",
    "chunk_table_child_target_chars",
    "chunk_parent_target_chars",
    "chunk_parent_max_pages",
    "chunk_parent_max_children",
)
DOCUMENT_PROCESSING_OUTPUT_GROUPS: dict[str, tuple[str, ...]] = {
    # 前処理 excel_to_jsonl の選択肢(#1221)も前処理の出力を変える。
    "preprocess_profile": ("preprocess_profile", "excel_options"),
    "parser_adapter_backend": (
        "parser_adapter_backend",
        "parser_docling_enabled",
        "parser_unstructured_enabled",
        "parser_structured_records_jsonl_enabled",
        "parser_xml_tree_json_enabled",
        # Vision は解析の後の共通の段で、解析結果(図の要素の本文)を変える(#497)。
        "vision_enabled",
        # 見出しの階層の推定(#2277)も解析結果(見出しの階層)を変える。
        "heading_level_inference_enabled",
    ),
    "chunking_strategy": (
        "chunking_strategy",
        "chunk_size",
        "chunk_overlap",
        "chunk_min_chars",
        "chunk_delimiter",
        *SMALL_TO_BIG_PROCESSING_CONFIG_FIELDS,
    ),
    "graph_profile": ("graph_profile",),
    # 実体の層（#1362）。索引の保存の後に作るため、変えると索引からやり直す。
    "entity_index_enabled": (
        "entity_index_enabled",
        "entity_name_columns",
        "entity_attribute_columns",
    ),
    "field_extraction_enabled": ("field_extraction_enabled",),
    "navigation_summary_enabled": ("navigation_summary_enabled",),
}


@router.post("/upload", response_model=ApiResponse[UploadResult])
async def upload_document(
    http_request: Request,
    file: Annotated[UploadFile, File(...)],
    knowledge_base_ids: Annotated[list[str] | None, Form()] = None,
) -> ApiResponse[UploadResult]:
    """ドキュメントファイルをアップロードし、Object Storage へ保管する。

    原本の保存と文書行の登録までを行い、取込 job は作らない（取込は文書ごとに明示して始める）。
    """
    enforce_rate_limit("upload", http_request)
    result = await _store_uploaded_document(file, knowledge_base_ids)
    return ApiResponse(data=result)


@router.post("/batch-upload", response_model=ApiResponse[BatchUploadResult])
async def batch_upload_documents(
    http_request: Request,
    files: Annotated[list[UploadFile], File(...)],
    knowledge_base_ids: Annotated[list[str] | None, Form()] = None,
) -> ApiResponse[BatchUploadResult]:
    """複数ドキュメントをまとめてアップロードし、Object Storage へ保管する。"""
    enforce_rate_limit("upload", http_request)
    if not files:
        raise HTTPException(status_code=400, detail="アップロード対象ファイルを選択してください。")
    items: list[UploadResult] = []
    failed_items: list[BatchUploadFailedItem] = []
    for file in files:
        try:
            result = await _store_uploaded_document(file, knowledge_base_ids)
            items.append(result)
        except HTTPException as exc:
            source_profile = await _failed_upload_source_profile(file)
            failed_items.append(
                BatchUploadFailedItem(
                    file_name=_safe_display_filename(file.filename),
                    status_code=exc.status_code,
                    message=str(exc.detail),
                    source_profile=source_profile,
                )
            )
        except Exception:
            logger.exception(
                "batch_upload_item_failed",
                extra={"file_name": _safe_display_filename(file.filename)},
            )
            source_profile = await _failed_upload_source_profile(file)
            failed_items.append(
                BatchUploadFailedItem(
                    file_name=_safe_display_filename(file.filename),
                    status_code=500,
                    message="アップロード処理に失敗しました。",
                    source_profile=source_profile,
                )
            )
    return ApiResponse(
        data=BatchUploadResult(
            items=items,
            failed_items=failed_items,
            total_count=len(files),
            uploaded_count=len(items),
            failed_count=len(failed_items),
        )
    )


async def _failed_upload_source_profile(file: UploadFile) -> SourceProfile | None:
    """batch upload の失敗 item に返す source profile を best-effort で作る。"""
    settings = get_settings()
    original_file_name = file.filename or "document.bin"
    file_name = _safe_display_filename(original_file_name)
    content_type = _normalized_content_type(file.content_type)
    data: bytes | None = None
    file_size_bytes = _upload_file_size_hint(file)
    content_sha256 = ""
    try:
        if file_size_bytes is None or file_size_bytes <= settings.max_upload_bytes:
            await file.seek(0)
            data = await file.read(settings.max_upload_bytes + 1)
            await file.seek(0)
            file_size_bytes = len(data)
            if len(data) <= settings.max_upload_bytes:
                content_sha256 = _sha256_hex(data)
            else:
                data = None
    except Exception:
        data = None
    try:
        return build_source_profile(
            original_file_name=original_file_name,
            sanitized_file_name=file_name,
            content_type=content_type,
            file_size_bytes=file_size_bytes or 0,
            content_sha256=content_sha256,
            duplicate_of_document_id=None,
            data=data,
        )
    except Exception:
        return None


def _upload_file_size_hint(file: UploadFile) -> int | None:
    """Starlette UploadFile の size hint を安全に読む。"""
    size = getattr(file, "size", None)
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    return max(size, 0)


def _is_allowed_upload_content_type(
    content_type: str,
    *,
    sanitized_file_name: str,
    allowed_content_types: list[str],
) -> bool:
    """MIME whitelist と拡張子 profile を組み合わせて upload 可否を判定する。"""
    normalized_allowed = {_normalized_content_type(allowed) for allowed in allowed_content_types}
    if content_type not in normalized_allowed:
        return False
    if content_type != "application/octet-stream":
        return True
    profile = build_source_profile(
        original_file_name=sanitized_file_name,
        sanitized_file_name=sanitized_file_name,
        content_type=content_type,
        file_size_bytes=0,
        content_sha256="",
        data=None,
    )
    return profile.unsupported_reason != "unknown_file_type"


async def _store_uploaded_document(
    file: UploadFile,
    knowledge_base_ids: list[str] | None,
) -> UploadResult:
    """単一 UploadFile を保存し、取込前の upload result を返す。"""
    settings = get_settings()
    selected_knowledge_base_ids = _normalize_upload_knowledge_base_ids(knowledge_base_ids)
    if (
        not selected_knowledge_base_ids
        and current_audit_request_context().allowed_knowledge_base_ids is not None
    ):
        # 利用できる KB が制限された利用者は、KB を指定しないと DEFAULT KB に入り、
        # 自分では見えない文書になる（#214）。
        raise HTTPException(
            status_code=400,
            detail="アップロード先のナレッジベースを指定してください。",
        )
    content_type = _normalized_content_type(file.content_type)
    original_file_name = file.filename or "document.bin"
    file_name = _safe_display_filename(original_file_name)
    rejected_message = rejected_upload_format_message(file_name, content_type)
    if rejected_message is not None:
        # HEIC / HEIF・EPUB(解析の依存を入れない。#3351)と音声・動画(取り込まない。#3369)は、
        # 保存せずに変換の方法を返す。
        raise HTTPException(status_code=415, detail=rejected_message)
    if not _is_allowed_upload_content_type(
        content_type,
        sanitized_file_name=file_name,
        allowed_content_types=settings.allowed_upload_content_types,
    ):
        raise HTTPException(status_code=415, detail="対応していないファイル形式です。")

    data = await _read_upload_file(file, settings.max_upload_bytes)
    if not data:
        raise HTTPException(status_code=400, detail="空のファイルはアップロードできません。")

    storage = ObjectStorageClient()
    oracle = OracleClient()
    content_sha256 = _sha256_hex(data)
    duplicate = await oracle.find_document_by_content_hash(content_sha256)
    source_profile = build_source_profile(
        original_file_name=original_file_name,
        sanitized_file_name=file_name,
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256=content_sha256,
        duplicate_of_document_id=duplicate.id if duplicate is not None else None,
        data=data,
    )
    key = f"uploaded/{uuid4().hex}/{file_name}"
    try:
        object_path = await storage.put(
            key=key,
            data=data,
            content_type=content_type,
        )
    except Exception as exc:
        # 保存先の未設定・認証切れ・容量不足などは、原因の分かる 503 にする（#280）。
        logger.exception(
            "upload_storage_put_failed",
            extra={"file_name": file_name, "exception_type": type(exc).__name__},
        )
        # OCI の bucket が無い・権限が無い・届かないときは、直す場所の分かる文言にする（#2093）。
        message = describe_upload_storage_error(exc) or UPLOAD_STORAGE_FAILED_MESSAGE
        raise HTTPException(status_code=503, detail=message) from exc
    try:
        detail = await oracle.create_document(
            file_name=file_name,
            object_storage_path=object_path,
            content_type=content_type,
            file_size_bytes=len(data),
            content_sha256=content_sha256,
            duplicate_of_document_id=duplicate.id if duplicate is not None else None,
            knowledge_base_ids=selected_knowledge_base_ids or None,
        )
    except Exception as exc:
        # 文書行を作れなかった原本は、どの文書からも参照されず保存先に残り続ける。
        # 範囲外・アーカイブ済みの KB や DB の失敗でも、保存した原本を消してから返す（#280）。
        await _delete_orphan_upload_object(storage, object_path)
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。") from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    return UploadResult(
        id=detail.id,
        file_name=detail.file_name,
        status=detail.status,
        file_size_bytes=detail.file_size_bytes or len(data),
        content_sha256=content_sha256,
        duplicate_of_document_id=detail.duplicate_of_document_id,
        knowledge_bases=detail.knowledge_bases,
        source_profile=source_profile,
        parser_notice=await _parser_notice(settings, source_profile),
    )


async def _parser_notice(
    settings: Settings, source_profile: SourceProfile
) -> ParserSourceNotice | None:
    """既定の文書解析エンジンで扱えない形式なら、取込を始める前の案内を返す(#286)。"""
    block = await check_parser_source(settings, source_profile)
    if block is None:
        return None
    return ParserSourceNotice(
        code=block.code,
        backend=block.backend,
        file_format=block.file_format,
        suggested_backend=block.suggested_backend,
        message=block.message,
    )


async def _raise_if_parser_source_blocked(
    settings: Settings, source_profile: SourceProfile, phase: IngestionJobPhase
) -> None:
    """解析を含む工程(準備・抽出)を、選んだ解析エンジンで扱えない形式なら始めずに 409 にする。"""
    if phase not in {IngestionJobPhase.PREPROCESS, IngestionJobPhase.EXTRACT}:
        return
    block = await check_parser_source(settings, source_profile)
    if block is not None:
        raise HTTPException(status_code=409, detail=block.message)


async def _default_recipe_settings(oracle: OracleClient, document_id: str) -> Settings:
    """文書単位の取込(既定レシピ)で使う実効設定。"""
    settings, _ = await _resolve_ingestion_settings(oracle, document_id)
    return settings


async def _delete_orphan_upload_object(storage: ObjectStorageClient, object_path: str) -> None:
    """文書行を作れなかったアップロードの原本を best-effort で削除する。"""
    try:
        await storage.delete(object_path)
    except Exception:
        # 後始末の失敗で元のエラーを隠さない。残った原本はログから追えるようにする。
        logger.warning(
            "upload_orphan_object_cleanup_failed",
            extra={"object_storage_path": object_path},
            exc_info=True,
        )


# 文書の一覧の knowledge_base_id の長さの上限。KB の ID（32 文字の hex）とカンマを、
# プロファイルの KB の上限の数だけ（URL を Nginx の要求行の上限 8 KiB に収める）。
_KNOWLEDGE_BASE_FILTER_MAX_LENGTH = 33 * MAX_SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES


async def _language_pack_document_filter(
    oracle: OracleClient,
    relation: LanguagePackIndexRelation | None,
    current_release_id: str | None,
) -> dict[str, Any]:
    if relation is None and current_release_id is None:
        return {}
    current = await (await initialized_release_store(oracle)).current()
    if current_release_id and current_release_id != current.id:
        raise HTTPException(
            status_code=409, detail="言語資源の現在版が変わりました。条件を再確認してください。"
        )
    return {"language_pack_relation": relation, "current_release_id": current.id}


@router.get("", response_model=ApiResponse[Page[DocumentSummary]])
async def list_documents(
    paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))],
    status: FileStatus | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    # カンマ区切りで複数を受ける（どれかに所属する文書）。業務ガイドの資料の選択は、
    # 検索・回答プロファイルのナレッジベース（上限 200 件）をまとめて渡す(#2749)。
    knowledge_base_id: str | None = Query(
        default=None, min_length=1, max_length=_KNOWLEDGE_BASE_FILTER_MAX_LENGTH
    ),
    queued_phase: IngestionJobPhase | None = None,
    language_pack_relation: LanguagePackIndexRelation | None = None,
    current_release_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> ApiResponse[Page[DocumentSummary]]:
    """取込対象ドキュメントの一覧を返す。DB 停止時は空一覧 + warning で縮退する。

    状態の絞り込みは一覧の行に出る状態の 1 つを選ぶ(#2677): ``status`` は順番待ちでない文書の
    状態(レシピ1の状態)、``queued_phase`` はレシピ1の取込の job が順番待ちの段(行に
    「〈工程〉の順番待ち」と出る)。両方は同時に受け付けない。
    """
    if status is not None and queued_phase is not None:
        raise HTTPException(
            status_code=422,
            detail="status と queued_phase は同時に指定できません。",
        )
    oracle = OracleClient()
    settings = get_settings()
    release_filter = await _language_pack_document_filter(
        oracle, language_pack_relation, current_release_id
    )

    async def _load() -> Page[DocumentSummary]:
        documents = await oracle.list_documents(
            status=status,
            query=q,
            limit=paging.limit,
            offset=paging.offset,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            **release_filter,
        )
        await _mark_layers_rebuild_required(oracle, documents, settings)
        documents = await _attach_queue_waits(oracle, documents, settings)
        total = await oracle.count_documents(
            status=status,
            query=q,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            **release_filter,
        )
        return paginate(documents, total=total, limit=paging.limit, offset=paging.offset)

    fallback: Page[DocumentSummary] = empty_page(paging)
    page, degraded = await load_or_degrade(
        _load,
        timeout_seconds=settings.db_read_timeout_seconds,
        fallback=fallback,
        log_label="documents_list",
    )
    return ApiResponse(
        data=page,
        warning_messages=[degraded.message] if degraded else [],
    )


@router.get("/ids", response_model=ApiResponse[DocumentIdList])
async def list_document_ids(
    status: FileStatus | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    knowledge_base_id: str | None = Query(default=None, min_length=1, max_length=128),
    queued_phase: IngestionJobPhase | None = None,
    language_pack_relation: LanguagePackIndexRelation | None = None,
    current_release_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=BULK_INGESTION_MAX_DOCUMENTS, ge=1, le=BULK_INGESTION_MAX_DOCUMENTS),
) -> ApiResponse[DocumentIdList]:
    """一覧と同じ条件の文書の ID(ファイル名の自然順に最大 ``limit`` 件)と件数を返す(#2674)。

    一覧の「条件に一致する文書をすべて選ぶ」から一括の投入へ渡す。要約・JSON 列は読まない。
    """
    if status is not None and queued_phase is not None:
        raise HTTPException(
            status_code=422,
            detail="status と queued_phase は同時に指定できません。",
        )
    oracle = OracleClient()
    release_filter = await _language_pack_document_filter(
        oracle, language_pack_relation, current_release_id
    )
    ids, total = await oracle.list_document_ids(
        status=status,
        query=q,
        knowledge_base_id=knowledge_base_id,
        queued_phase=queued_phase,
        **release_filter,
        limit=limit,
    )
    return ApiResponse(data=DocumentIdList(ids=ids, total=total, limit=limit))


def _queue_wait_concurrency(
    phase: IngestionJobPhase, plan: IngestionQueueConcurrency | None
) -> int:
    """順番待ちの job が待っている段の枠(重い段は重い段の上限、軽い段は全体の枠。#2027)。"""
    if plan is None:
        return 1
    return max(1, plan.heavy_phase if phase in INGESTION_HEAVY_PHASES else plan.total)


async def _attach_queue_waits(
    oracle: OracleClient,
    documents: list[DocumentSummary],
    settings: Settings,
) -> list[DocumentSummary]:
    """一覧の文書に、レシピ1の取込の job の順番待ちの情報を付ける(#2026)。

    一覧の状態はレシピ1の状態のため、レシピ1(``slot_no`` の最も小さいレシピ。レシピの行が無い
    文書は文書の job)の QUEUED の job だけを見る。前の件数は詳細と同じ問い合わせで数える。
    表示の補助なので、読めないときは印を付けずに一覧を返す(一覧そのものを縮退させない)。
    1 回の問い合わせで一覧の全文書を読む。
    """
    if not documents:
        return documents
    try:
        rows = await oracle.list_ingestion_queue_positions(
            document_ids=[document.id for document in documents]
        )
    except Exception:
        logger.warning("documents_list_queue_positions_failed", exc_info=True)
        return documents
    concurrency_plan = settings.ingestion_queue_concurrency()
    waits: dict[str, IngestionQueueWait] = {}
    for row in rows:
        document_id = str(row.get("document_id"))
        if document_id in waits or row.get("recipe_slot_no") != row.get("first_slot_no"):
            continue
        raw_ahead = row.get("jobs_ahead")
        queued_at = row.get("queued_at")
        phase = IngestionJobPhase(str(row.get("phase") or IngestionJobPhase.PREPROCESS.value))
        waits[document_id] = IngestionQueueWait(
            phase=phase,
            job_id=str(row["job_id"]) if row.get("job_id") is not None else None,
            queued_at=queued_at if isinstance(queued_at, datetime) else None,
            jobs_ahead=max(0, int(str(raw_ahead))) if raw_ahead is not None else None,
            concurrency=_queue_wait_concurrency(phase, concurrency_plan),
        )
    if not waits:
        return documents
    return [
        document.model_copy(update={"queue_wait": waits[document.id]})
        if document.id in waits
        else document
        for document in documents
    ]


async def _mark_layers_rebuild_required(
    oracle: OracleClient,
    documents: Sequence[DocumentSummary],
    global_settings: Settings,
) -> None:
    """一覧の文書に、検索対象の派生情報レイヤーの作り直しが必要かの印を付ける(#550)。

    判定は文書詳細(``/chunk-sets``)と同じで、レシピの設定で層 ID を作り直し、今も要求されて
    いる層の指紋だけを今の入力と比べる。表示の補助なので、読めないときは印を付けずに一覧を返す
    (一覧そのものを縮退させない)。1 回の問い合わせで一覧の全文書を読む。
    """
    if not documents:
        return
    try:
        rows = await oracle.list_serving_artifact_layer_fingerprints(
            [document.id for document in documents]
        )
    except Exception:
        logger.warning("documents_list_layer_fingerprints_failed", exc_info=True)
        return
    if not rows:
        return
    try:
        # 項目の定義は文書が属する KB で変わるため、層のある文書の定義を 1 回で読む(#548)。
        field_sets = await oracle.list_documents_extraction_field_sets(
            list(dict.fromkeys(str(row.get("document_id")) for row in rows))
        )
    except Exception:
        logger.warning("documents_list_layer_fingerprints_failed", exc_info=True)
        return
    default_fields = load_field_schema().fields
    inputs_by_document: dict[str, dict[str, object]] = {}
    flagged: set[str] = set()
    for row in rows:
        document_id = str(row.get("document_id"))
        if document_id in flagged:
            continue
        if document_id not in inputs_by_document:
            inputs_by_document[document_id] = current_layer_inputs(
                global_settings,
                current_field_definitions(field_sets.get(document_id, []), default_fields),
            )
        current_inputs = inputs_by_document[document_id]
        raw_config = (
            row.get("recipe_processing_config")
            if row.get("recipe_id") is not None
            else row.get("document_processing_config")
        )
        try:
            settings, _effective = _merge_document_processing_config(
                DocumentProcessingConfig.model_validate(raw_config or {}), global_settings
            )
        except (ValueError, KbAdapterConfigError):
            continue
        layer = str(row.get("layer_kind"))
        chunk_set_id = str(row.get("parent_chunk_set_id"))
        if not _layer_requested(layer, settings) or row.get("layer_id") != _recipe_layer_id(
            chunk_set_id, settings, layer
        ):
            continue
        if changed_layer_inputs(row.get("input_fingerprint"), current_inputs):
            flagged.add(document_id)
    for document in documents:
        document.layers_rebuild_required = document.id in flagged


DELETE_IMPACT_MAX_DOCUMENTS = 100


@router.get("/delete-impact", response_model=ApiResponse[list[DocumentDeleteImpact]])
async def document_delete_impact(
    document_id: Annotated[
        list[str],
        Query(min_length=1, max_length=DELETE_IMPACT_MAX_DOCUMENTS),
    ],
) -> ApiResponse[list[DocumentDeleteImpact]]:
    """削除の前に、正本を参照する重複文書の件数と所属 KB を返す（#303）。

    重複文書は chunk を持たず正本の chunk を使うため、正本を消すとその KB の検索対象から
    内容が消える。自前の索引を持つ（INDEXED の）重複文書と、同時に削除する文書は数えない。
    """
    requested = list(dict.fromkeys(value.strip() for value in document_id if value.strip()))
    if not requested:
        raise HTTPException(status_code=422, detail="document_id を指定してください。")
    deleting = set(requested)
    duplicates = await OracleClient().list_duplicate_documents(requested)
    impacts: dict[str, DocumentDeleteImpact] = {
        requested_id: DocumentDeleteImpact(document_id=requested_id) for requested_id in requested
    }
    for duplicate in duplicates:
        source_id = duplicate.duplicate_of_document_id
        if source_id not in impacts or duplicate.id in deleting:
            continue
        if duplicate.status == FileStatus.INDEXED:
            continue
        impact = impacts[source_id]
        known = {knowledge_base.id for knowledge_base in impact.knowledge_bases}
        impact.duplicate_count += 1
        impact.knowledge_bases.extend(
            knowledge_base
            for knowledge_base in duplicate.knowledge_bases
            if knowledge_base.id not in known
        )
    return ApiResponse(data=list(impacts.values()))


@router.get("/ingestion-jobs", response_model=ApiResponse[Page[IngestionJob]])
async def list_ingestion_jobs(
    paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))],
    status: Annotated[IngestionJobStatus | None, Query()] = None,
) -> ApiResponse[Page[IngestionJob]]:
    """直近の取込 job 一覧を返す。DB 停止時は空一覧 + warning で縮退する。"""
    oracle = OracleClient()
    settings = get_settings()

    async def _load() -> Page[IngestionJob]:
        page_items = await oracle.list_ingestion_jobs(
            status=status, limit=paging.limit, offset=paging.offset
        )
        total = await oracle.count_ingestion_jobs(status=status)
        return paginate(page_items, total=total, limit=paging.limit, offset=paging.offset)

    fallback: Page[IngestionJob] = empty_page(paging)
    page, degraded = await load_or_degrade(
        _load,
        timeout_seconds=settings.db_read_timeout_seconds,
        fallback=fallback,
        log_label="ingestion_jobs_list",
    )
    return ApiResponse(
        data=page,
        warning_messages=[degraded.message] if degraded else [],
    )


@router.post("/ingestion-jobs/drain", response_model=ApiResponse[list[IngestionJob]])
async def drain_queued_ingestion_jobs(
    http_request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiResponse[list[IngestionJob]]:
    """永続化済み QUEUED job をバックグラウンド実行へ戻す。"""
    enforce_rate_limit("ingest", http_request)
    jobs = await OracleClient().list_ingestion_jobs(
        status=IngestionJobStatus.QUEUED,
        limit=limit,
        offset=0,
    )
    for job in jobs:
        _dispatch_ingestion_job(job.id)
    return ApiResponse(data=jobs)


@router.post("/ingestion-jobs/{job_id}/retry", response_model=ApiResponse[IngestionJob])
async def retry_ingestion_job(
    http_request: Request,
    job_id: str,
    force: bool = Query(default=False),
) -> ApiResponse[IngestionJob]:
    """完了済みまたは失敗済み job の対象を新しい job として再投入する。

    レシピの job(``recipe_id`` あり)は同じレシピの job として投入し、文書全体(全レシピ)の
    出力は初期化しない(#305)。``force`` は文書単位の job の重複・未対応の skip 判定にだけ使う。
    """
    enforce_rate_limit("ingest", http_request)
    job = await OracleClient().get_ingestion_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="取込ジョブが見つかりません。")
    if job.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING}:
        raise HTTPException(status_code=409, detail="この取込ジョブはまだ実行中です。")
    if job.recipe_id is not None:
        try:
            recipe_job = await _enqueue_ingestion_job_for_recipe(
                job.document_id, job.recipe_id, phase=job.phase
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="レシピが見つかりません。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ApiResponse(data=recipe_job)
    retry_job = await _enqueue_ingestion_job_for_document(
        job.document_id,
        force=force or job.status == IngestionJobStatus.FAILED,
        phase=job.phase,
    )
    return ApiResponse(data=retry_job)


@router.post("/ingestion-jobs/{job_id}/cancel", response_model=ApiResponse[IngestionJob])
async def cancel_ingestion_job(
    http_request: Request,
    job_id: str,
) -> ApiResponse[IngestionJob]:
    """待機中または実行中の取込 job をキャンセル済みにする。

    job の状態だけを QUEUED / RUNNING → CANCELLED に条件付きで変える(#305)。文書・レシピの
    status は戻さない。

    - RUNNING の job: 処理を続けている worker が cancel を検知し、文書単位の job なら文書を、
      レシピの job ならレシピ行だけを戻す(`_restore_statuses_after_cancel`)。
    - QUEUED の job: worker がまだ何も書いておらず、投入時の文書・レシピは安定した状態のため、
      戻すものはない。
    """
    enforce_rate_limit("ingest", http_request)
    oracle = OracleClient()
    job = await oracle.get_ingestion_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="取込ジョブが見つかりません。")
    if job.status not in _CANCELLABLE_INGESTION_JOB_STATUSES:
        raise HTTPException(status_code=409, detail="この取込ジョブはキャンセルできません。")
    cancelled = await oracle.transition_ingestion_job(
        job_id,
        from_statuses=_CANCELLABLE_INGESTION_JOB_STATUSES,
        to_status=IngestionJobStatus.CANCELLED,
        error_message=INGESTION_JOB_CANCELLED_MESSAGE,
        finished_at=datetime.now(UTC),
    )
    if cancelled is None:
        # 確かめた後に完了・失敗した。その最終状態を CANCELLED で上書きしない。
        raise HTTPException(status_code=409, detail="この取込ジョブはキャンセルできません。")
    return ApiResponse(data=cancelled)


@router.get("/ingestion-jobs/{job_id}", response_model=ApiResponse[IngestionJob])
async def get_ingestion_job(job_id: str) -> ApiResponse[IngestionJob]:
    """指定した取込 job の現在状態を返す。"""
    job = await OracleClient().get_ingestion_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="取込ジョブが見つかりません。")
    return ApiResponse(data=job)


def _bulk_ingestion_store() -> BulkIngestionStore:
    """一括の投入の保存先(テストは偽の保存先に差し替える)。"""
    return OracleBulkIngestionStore(OracleClient())


@router.post("/ingestion-jobs/bulk", response_model=ApiResponse[BulkIngestionResult])
async def enqueue_bulk_ingestion_jobs(
    http_request: Request,
    request: BulkIngestionRequest,
) -> ApiResponse[BulkIngestionResult]:
    """選んだ文書(最大 1 万件)のファイル準備をまとめて受け付ける(#2674)。

    受け付けた文書の取込 job(QUEUED)を永続化してから、文書ごとの結果(受付済み・既に処理中・
    スキップ・受付不可と理由)を返す。流量制限は要求 1 回として数え、一括の中の各文書は単件の
    流量制限を通らない(高コストの処理の並行数は取込 worker が制御する)。同じ文書レシピに待機中・
    実行中の job があれば作らないため、通信断の後に同じ要求を再送しても二重に登録しない。
    """
    enforce_rate_limit("ingest", http_request)
    settings = get_settings()
    result = await accept_bulk_ingestion(
        request.document_ids,
        store=_bulk_ingestion_store(),
        resolve_settings=lambda config: _merge_document_processing_config(config)[0],
        enqueue_single=lambda document_id: _enqueue_ingestion_job_for_document(
            document_id, force=False
        ),
        max_attempts=settings.ingestion_job_max_attempts,
    )
    if result.accepted_count:
        request_ingestion_worker_wakeup()
    logger.info(
        "bulk_ingestion_accepted",
        extra={
            "requested_count": result.requested_count,
            "unique_count": result.unique_count,
            "accepted_count": result.accepted_count,
            "already_active_count": result.already_active_count,
            "skipped_count": result.skipped_count,
            "rejected_count": result.rejected_count,
        },
    )
    return ApiResponse(data=result)


@router.post("/{document_id}/ingestion-jobs", response_model=ApiResponse[IngestionJob])
async def enqueue_document_ingestion_job(
    http_request: Request,
    document_id: str,
    force: bool = Query(default=False),
    phase: Annotated[IngestionJobPhase, Query()] = IngestionJobPhase.PREPROCESS,
) -> ApiResponse[IngestionJob]:
    """保存済みドキュメントを取込 job としてキュー投入する。"""
    enforce_rate_limit("ingest", http_request)
    job = await _enqueue_ingestion_job_for_document(document_id, force=force, phase=phase)
    return ApiResponse(data=job)


@router.get("/{document_id}/ingestion-jobs", response_model=ApiResponse[list[IngestionJob]])
async def list_document_ingestion_jobs(
    document_id: str,
) -> ApiResponse[list[IngestionJob]]:
    """文書 workspace 用に、この文書の取込 job 履歴を新しい順で返す。"""
    oracle = OracleClient()
    if not await oracle.document_exists(document_id):
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    return ApiResponse(data=await oracle.list_document_ingestion_jobs(document_id))


@router.post(
    "/{document_id}/ingestion-segments/retry",
    response_model=ApiResponse[IngestionJob],
)
async def retry_failed_document_ingestion_segments(
    http_request: Request,
    document_id: str,
    recipe_id: str | None = Query(default=None),
) -> ApiResponse[IngestionJob]:
    """FAILED checkpoint がある文書だけ、失敗 segment 再試行 job として再投入する。"""
    enforce_rate_limit("ingest", http_request)
    job = await _enqueue_failed_segment_retry_job_for_document(
        document_id,
        recipe_id=recipe_id,
    )
    return ApiResponse(data=job)


_RECIPE_PHASES = (
    IngestionJobPhase.PREPROCESS,
    IngestionJobPhase.EXTRACT,
    IngestionJobPhase.CHUNK,
    IngestionJobPhase.INDEX,
)

_PHASE_TO_RUNNING_STATUS = {
    IngestionJobPhase.PREPROCESS: FileStatus.PREPROCESSING,
    IngestionJobPhase.EXTRACT: FileStatus.INGESTING,
    IngestionJobPhase.CHUNK: FileStatus.CHUNKING,
    IngestionJobPhase.INDEX: FileStatus.INDEXING,
}
_RUNNING_STATUS_TO_PHASE = {status: phase for phase, status in _PHASE_TO_RUNNING_STATUS.items()}

_STEP_PENDING = DocumentRecipeStepStatus.PENDING
_STEP_RUNNING = DocumentRecipeStepStatus.RUNNING
_STEP_SUCCEEDED = DocumentRecipeStepStatus.SUCCEEDED
_STEP_NEEDS_REVIEW = DocumentRecipeStepStatus.NEEDS_REVIEW

# レシピ行 status を単一状態源として 4 工程の表示状態を導出する行列。
# 1 本のジョブが複数工程を通し実行するため、ジョブ行の phase/status からは
# 「いまどの工程か」を判定できない(pipeline が工程ごとにレシピ status を更新する)。
_RECIPE_STEP_MATRIX: dict[FileStatus, tuple[DocumentRecipeStepStatus, ...]] = {
    FileStatus.UPLOADED: (_STEP_PENDING, _STEP_PENDING, _STEP_PENDING, _STEP_PENDING),
    FileStatus.PREPROCESSING: (_STEP_RUNNING, _STEP_PENDING, _STEP_PENDING, _STEP_PENDING),
    FileStatus.PREPROCESSED: (_STEP_SUCCEEDED, _STEP_PENDING, _STEP_PENDING, _STEP_PENDING),
    FileStatus.INGESTING: (_STEP_SUCCEEDED, _STEP_RUNNING, _STEP_PENDING, _STEP_PENDING),
    FileStatus.REVIEW: (_STEP_SUCCEEDED, _STEP_NEEDS_REVIEW, _STEP_PENDING, _STEP_PENDING),
    FileStatus.CHUNKING: (_STEP_SUCCEEDED, _STEP_SUCCEEDED, _STEP_RUNNING, _STEP_PENDING),
    FileStatus.CHUNKED: (_STEP_SUCCEEDED, _STEP_SUCCEEDED, _STEP_NEEDS_REVIEW, _STEP_PENDING),
    FileStatus.INDEXING: (_STEP_SUCCEEDED, _STEP_SUCCEEDED, _STEP_SUCCEEDED, _STEP_RUNNING),
    FileStatus.INDEXED: (_STEP_SUCCEEDED, _STEP_SUCCEEDED, _STEP_SUCCEEDED, _STEP_SUCCEEDED),
}


# 抽出の job が SUCCEEDED になってから、自動進行の CHUNK の job を投入するまでの猶予(#733)。
# 固定の猶予。投入が猶予より遅れるなら、SUCCEEDED と投入を 1 トランザクションにする。
_AUTO_ADVANCE_HANDOFF_GRACE = timedelta(seconds=30)


def _recipe_steps(
    row: Mapping[str, object],
    jobs: list[IngestionJob],
    *,
    auto_chunk_after_extract: bool = False,
    queue_positions: Mapping[str, int] | None = None,
    queue_concurrency: IngestionQueueConcurrency | None = None,
) -> list[DocumentRecipeStep]:
    """レシピの 4 工程の表示状態を返す。

    次の工程の job が QUEUED(と自動進行の投入までの猶予の間。#733)のときは、その工程を
    QUEUED(順番待ち)にし、前の工程の「確認待ち」(NEEDS_REVIEW)を完了にする(#2026)。確認は
    済んでいる(自動進行・利用者の続行)ため、利用者がすることは無く、システムが worker の空きを
    待っている。QUEUED の工程には順番待ちの情報(前の job の数・同時に処理できる数)を付ける。
    """
    recipe_status = FileStatus(str(row.get("status") or FileStatus.UPLOADED.value))
    raw_failed_phase = row.get("failed_phase")
    failed_phase = IngestionJobPhase(str(raw_failed_phase)) if raw_failed_phase else None
    latest_by_phase: dict[IngestionJobPhase, IngestionJob] = {}
    for job in jobs:
        latest_by_phase.setdefault(job.phase, job)
    latest_failed = next((job for job in jobs if job.status == IngestionJobStatus.FAILED), None)
    if recipe_status == FileStatus.ERROR:
        failed = failed_phase or (
            latest_failed.phase if latest_failed is not None else IngestionJobPhase.PREPROCESS
        )
        failed_index = _RECIPE_PHASES.index(failed)
        statuses = tuple(
            (
                _STEP_SUCCEEDED
                if i < failed_index
                else DocumentRecipeStepStatus.FAILED
                if i == failed_index
                else _STEP_PENDING
            )
            for i in range(len(_RECIPE_PHASES))
        )
    else:
        statuses = _RECIPE_STEP_MATRIX.get(recipe_status, (_STEP_PENDING,) * len(_RECIPE_PHASES))
    newest = jobs[0] if jobs else None
    # 抽出の job を SUCCEEDED にしてから CHUNK の job を投入するまでの間は、レシピが REVIEW で
    # QUEUED の job も無い。ここで Chunk 作成を QUEUED にし、画面のポーリングを止めない(#733)。
    # 投入に失敗した場合は猶予を過ぎると「確認待ち」に戻り、手動で続行できる。
    handoff_phase: IngestionJobPhase | None = None
    if (
        auto_chunk_after_extract
        and recipe_status == FileStatus.REVIEW
        and newest is not None
        and newest.status == IngestionJobStatus.SUCCEEDED
        and newest.phase in {IngestionJobPhase.PREPROCESS, IngestionJobPhase.EXTRACT}
        and newest.finished_at is not None
        and datetime.now(UTC) - newest.finished_at < _AUTO_ADVANCE_HANDOFF_GRACE
    ):
        handoff_phase = IngestionJobPhase.CHUNK
    # enqueue→claim 間はレシピ status がまだ前値のため、最新ジョブでだけ補正する。
    queued_job = (
        newest if newest is not None and newest.status == IngestionJobStatus.QUEUED else None
    )
    queued_phase = queued_job.phase if queued_job is not None else handoff_phase
    queued_index = _RECIPE_PHASES.index(queued_phase) if queued_phase is not None else None
    result: list[DocumentRecipeStep] = []
    for index, (phase, status) in enumerate(zip(_RECIPE_PHASES, statuses, strict=True)):
        latest_job = latest_by_phase.get(phase)
        queue_wait: IngestionQueueWait | None = None
        if queued_index is not None and index == queued_index:
            status = DocumentRecipeStepStatus.QUEUED
            queue_wait = IngestionQueueWait(
                phase=phase,
                job_id=queued_job.id if queued_job is not None else None,
                queued_at=queued_job.queued_at if queued_job is not None else None,
                jobs_ahead=(
                    queue_positions.get(queued_job.id)
                    if queued_job is not None and queue_positions is not None
                    else None
                ),
                concurrency=_queue_wait_concurrency(phase, queue_concurrency),
            )
        elif (
            queued_index is not None
            and index < queued_index
            and status == DocumentRecipeStepStatus.NEEDS_REVIEW
        ):
            # 次の工程が順番待ちなら、この工程の確認は済んでいる(#2026)。
            status = _STEP_SUCCEEDED
        elif (
            queued_index is not None
            and index > queued_index
            and status == DocumentRecipeStepStatus.FAILED
        ):
            # 失敗した工程より前の工程から再処理を投入した。後ろの工程は作り直すため、claim まで
            # レシピ行に残る前回の失敗を出さない(#2687)。
            status = _STEP_PENDING
        error_message: str | None = None
        if status == DocumentRecipeStepStatus.FAILED:
            # 通しジョブの失敗では失敗工程にジョブ行が無いことがあるため、
            # 最新 FAILED ジョブのメッセージへフォールバックする。
            error_message = (latest_job.error_message if latest_job is not None else None) or (
                latest_failed.error_message if latest_failed is not None else None
            )
        result.append(
            DocumentRecipeStep(
                phase=phase,
                status=status,
                started_at=latest_job.started_at if latest_job is not None else None,
                finished_at=latest_job.finished_at if latest_job is not None else None,
                error_message=error_message,
                queue_wait=queue_wait,
            )
        )
    return result


async def _ingestion_queue_positions(
    oracle: OracleClient, jobs: Sequence[IngestionJob]
) -> dict[str, int]:
    """QUEUED の job の、FIFO で前にある claim できる job の数(#2026)。

    表示の補助なので、読めないときは空(前の件数を出さない)にし、画面の取得を失敗させない。
    """
    queued_ids = [job.id for job in jobs if job.status == IngestionJobStatus.QUEUED]
    if not queued_ids:
        return {}
    try:
        rows = await oracle.list_ingestion_queue_positions(job_ids=queued_ids)
    except Exception:
        logger.warning("ingestion_queue_positions_failed", exc_info=True)
        return {}
    return _queue_positions_by_job(rows)


def _queue_positions_by_job(rows: Sequence[Mapping[str, object]]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for row in rows:
        raw = row.get("jobs_ahead")
        if row.get("job_id") is None or raw is None:
            continue
        positions[str(row["job_id"])] = max(0, int(str(raw)))
    return positions


async def _document_recipe_view(
    oracle: OracleClient,
    row: Mapping[str, object],
    *,
    document_jobs: Sequence[IngestionJob] | None = None,
    queue_positions: Mapping[str, int] | None = None,
) -> DocumentRecipeView:
    config = DocumentProcessingConfig.model_validate(row.get("processing_config") or {})
    recipe_settings, effective = _merge_document_processing_config(config)
    recipe_id = str(row["recipe_id"])
    all_jobs = (
        document_jobs
        if document_jobs is not None
        else await oracle.list_document_ingestion_jobs(str(row["document_id"]))
    )
    jobs = [job for job in all_jobs if job.recipe_id == recipe_id]
    if queue_positions is None:
        queue_positions = await _ingestion_queue_positions(oracle, jobs[:1])
    active_chunk_set_id = (
        str(row["active_chunk_set_id"]) if row.get("active_chunk_set_id") is not None else None
    )
    config_revision = int(str(row.get("config_revision") or 1))
    materialized_revision = (
        int(str(row["materialized_revision"]))
        if row.get("materialized_revision") is not None
        else None
    )
    return DocumentRecipeView(
        recipe_id=recipe_id,
        document_id=str(row["document_id"]),
        slot_no=int(str(row["slot_no"])),
        status=FileStatus(str(row.get("status") or FileStatus.UPLOADED.value)),
        failed_phase=(
            IngestionJobPhase(str(row["failed_phase"]))
            if row.get("failed_phase") is not None
            else None
        ),
        processing_config=config,
        effective_processing_config=effective,
        preprocess_artifact=(
            DocumentPreprocessArtifact.model_validate(row["preprocess_artifact"])
            if row.get("preprocess_artifact")
            else None
        ),
        active_extraction_recipe_id=(
            str(row["active_extraction_recipe_id"])
            if row.get("active_extraction_recipe_id") is not None
            else None
        ),
        active_chunk_set_id=active_chunk_set_id,
        pending_chunk_set_id=(
            str(row["pending_chunk_set_id"]) if row.get("pending_chunk_set_id") else None
        ),
        chunk_count=int(str(row.get("chunk_count") or 0)),
        vector_count=int(str(row.get("vector_count") or 0)),
        config_revision=config_revision,
        materialized_revision=materialized_revision,
        searchable=(
            active_chunk_set_id is not None and str(row.get("chunk_set_status")) == "INDEXED"
        ),
        needs_reprocessing=(
            materialized_revision is not None and config_revision != materialized_revision
        ),
        error_message=(str(row["error_message"]) if row.get("error_message") else None),
        steps=_recipe_steps(
            row,
            jobs,
            auto_chunk_after_extract=recipe_settings.rag_auto_chunk_after_extract_enabled,
            queue_positions=queue_positions,
            queue_concurrency=get_settings().ingestion_queue_concurrency(),
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
    )


@router.get("/{document_id}/recipes", response_model=ApiResponse[list[DocumentRecipeView]])
async def list_document_recipes(document_id: str) -> ApiResponse[list[DocumentRecipeView]]:
    """文書の 1〜3 件の独立レシピを返す。"""
    oracle = OracleClient()
    try:
        rows = await oracle.list_document_recipes(document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。") from exc
    jobs = await oracle.list_document_ingestion_jobs(document_id)
    # レシピごとに問い合わせず、文書の QUEUED の job の順番を 1 回で読む(#2026)。
    queue_positions = await _ingestion_queue_positions(oracle, jobs)
    return ApiResponse(
        data=[
            await _document_recipe_view(
                oracle, row, document_jobs=jobs, queue_positions=queue_positions
            )
            for row in rows
        ]
    )


@router.post("/{document_id}/recipes", response_model=ApiResponse[DocumentRecipeView])
async def create_document_recipe(
    document_id: str, request: DocumentRecipeCreateRequest
) -> ApiResponse[DocumentRecipeView]:
    """空き slot にレシピを追加する。"""
    oracle = OracleClient()
    try:
        row = await oracle.create_document_recipe(
            document_id, copy_from_recipe_id=request.copy_from_recipe_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=await _document_recipe_view(oracle, row))


@router.put("/{document_id}/recipes/{recipe_id}", response_model=ApiResponse[DocumentRecipeView])
async def update_document_recipe(
    document_id: str, recipe_id: str, request: DocumentProcessingConfig
) -> ApiResponse[DocumentRecipeView]:
    """選択レシピの明示設定を保存する。"""
    try:
        _merge_document_processing_config(request)
    except KbAdapterConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    oracle = OracleClient()
    try:
        await oracle.update_document_recipe_config(document_id, recipe_id, request)
        row = await oracle.get_document_recipe(document_id, recipe_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    # 章節の抽出規則の方式（section_rules_mode）は取込をやり直さずに章節を変えるため、
    # 章節と chunk の対応だけを作り直す（#1438）。
    await refresh_document_section_map(document_id, oracle=oracle, reason="recipe_config")
    return ApiResponse(data=await _document_recipe_view(oracle, row))


@router.delete(
    "/{document_id}/recipes/{recipe_id}",
    response_model=ApiResponse[DocumentRecipeDeleteResult],
)
async def delete_document_recipe(
    document_id: str, recipe_id: str
) -> ApiResponse[DocumentRecipeDeleteResult]:
    """最後の1件を保護してレシピと固有索引を削除する。"""
    oracle = OracleClient()
    try:
        removed = await oracle.delete_document_recipe(document_id, recipe_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await refresh_document_section_map(document_id, oracle=oracle, reason="recipe_delete")
    return ApiResponse(
        data=DocumentRecipeDeleteResult(
            recipe_id=recipe_id,
            document_id=document_id,
            removed_chunk_set_count=removed,
        )
    )


@router.post(
    "/{document_id}/recipes/{recipe_id}/ingestion-jobs",
    response_model=ApiResponse[IngestionJob],
)
async def enqueue_document_recipe_job(
    document_id: str,
    recipe_id: str,
    phase: IngestionJobPhase = IngestionJobPhase.PREPROCESS,
    expected_language_pack_release_id: Annotated[
        str | None, Query(min_length=1, max_length=64)
    ] = None,
    expected_config_revision: Annotated[int | None, Query(ge=1)] = None,
) -> ApiResponse[IngestionJob]:
    """レシピ設定の snapshot を持つ独立 job を投入する。文書内実行は worker が直列化する。"""
    try:
        job = await _enqueue_ingestion_job_for_recipe(
            document_id,
            recipe_id,
            phase=phase,
            expected_language_pack_release_id=expected_language_pack_release_id,
            expected_config_revision=expected_config_revision,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=job)


async def _enqueue_ingestion_job_for_recipe(
    document_id: str,
    recipe_id: str,
    *,
    phase: IngestionJobPhase,
    expected_language_pack_release_id: str | None = None,
    expected_config_revision: int | None = None,
) -> IngestionJob:
    """snapshot を作り、Oracle 側の行ロック検証後に recipe job を投入する。"""
    oracle = OracleClient()
    row = await oracle.get_document_recipe(document_id, recipe_id)
    detail = await oracle.get_document(document_id)
    if row is None or detail is None:
        raise KeyError("レシピが見つかりません。")
    if (
        expected_config_revision is not None
        and int(str(row.get("config_revision") or 1)) != expected_config_revision
    ):
        raise ValueError("レシピの改訂が変わりました。対象を確認し直してください。")
    config = DocumentProcessingConfig.model_validate(row.get("processing_config") or {})
    effective_settings, _ = _merge_document_processing_config(config)
    source_profile = _source_profile_for_detail(detail)
    # レシピの解析エンジン(既定は Docling)で扱えない形式は、job を作る前に止める(#286)。
    await _raise_if_parser_source_blocked(effective_settings, source_profile, phase)
    job = await _create_ingestion_job_record(
        oracle=oracle,
        document_id=document_id,
        recipe_id=recipe_id,
        recipe_revision=int(str(row.get("config_revision") or 1)),
        parser_profile=source_profile.parser_profile,
        quality_warnings=source_profile.quality_warnings,
        phase=phase,
        expected_language_pack_release_id=expected_language_pack_release_id,
        settings_overrides={
            "processing_config": config.model_dump(mode="json", exclude_none=True),
            "rag_preprocess_profile": effective_settings.rag_preprocess_profile,
            "rag_parser_adapter_backend": effective_settings.rag_parser_adapter_backend,
        },
    )
    _dispatch_ingestion_job(job.id)
    return job


@router.get(
    "/{document_id}/recipes/{recipe_id}/chunks",
    response_model=ApiResponse[list[DocumentChunkView]],
)
async def list_document_recipe_chunks(
    document_id: str, recipe_id: str
) -> ApiResponse[list[DocumentChunkView]]:
    oracle = OracleClient()
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    chunk_set_id = row.get("active_chunk_set_id")
    return ApiResponse(
        data=(await oracle.list_chunk_set_chunks(str(chunk_set_id)) if chunk_set_id else [])
    )


@router.post(
    "/{document_id}/recipes/{recipe_id}/chunk-preview",
    response_model=ApiResponse[DocumentChunkPreviewResponse],
)
async def preview_document_recipe_chunks(
    http_request: Request,
    document_id: str,
    recipe_id: str,
    request: DocumentChunkPreviewRequest | None = None,
) -> ApiResponse[DocumentChunkPreviewResponse]:
    """保存済み抽出を一時設定で分割し、DB・job・工程状態を変更せず返す。"""
    enforce_rate_limit("ingest", http_request)
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if detail is None or row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    status = FileStatus(str(row.get("status")))
    if status not in {FileStatus.REVIEW, FileStatus.CHUNKED}:
        raise HTTPException(
            status_code=409,
            detail="確認待ちまたは分割確認待ちのレシピのみプレビューできます。",
        )
    extraction_recipe_id = row.get("active_extraction_recipe_id")
    artifact = (
        await oracle.get_document_extraction_artifact(
            document_id=document_id,
            extraction_recipe_id=str(extraction_recipe_id),
        )
        if extraction_recipe_id
        else None
    )
    if artifact is None or not artifact.get("extraction_json"):
        raise HTTPException(status_code=409, detail="再利用できる抽出結果がありません。")

    config = DocumentProcessingConfig.model_validate(row.get("processing_config") or {})
    settings, _ = _merge_document_processing_config(config)
    candidate = _candidate_chunking_settings(
        settings,
        (request or DocumentChunkPreviewRequest()).settings_overrides(),
    )
    extraction = StructuredExtraction.model_validate(artifact["extraction_json"])
    try:
        # 親子階層（small-to-big）は、どの解析の結果でも親子に分割する(#3377)。
        if candidate.rag_chunking_strategy == SMALL_TO_BIG_STRATEGY:
            chunks = build_parent_child_chunks(
                extraction,
                source_name=detail.file_name,
                params=resolve_small_to_big_params(candidate),
            )
        else:
            chunks = chunk_extraction_with_strategy(
                extraction,
                strategy=candidate.rag_chunking_strategy,
                chunk_size=candidate.rag_chunk_size,
                overlap=candidate.rag_chunk_overlap,
                min_chars=candidate.rag_chunk_min_chars,
                delimiter=candidate.rag_chunk_delimiter,
            )
    except RecordBoundaryError as exc:
        raise HTTPException(status_code=422, detail=record_boundary_failure_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    views: list[DocumentChunkView] = []
    search_lengths: list[int] = []
    for chunk in chunks:
        metadata = dict(chunk.metadata)
        section_path = str(metadata.get("section_path") or "").strip()
        header = " > ".join(part for part in (detail.file_name.strip(), section_path) if part)
        if candidate.rag_chunk_context_header_enabled and header:
            metadata["context_header"] = header
        else:
            metadata.pop("context_header", None)
        search_lengths.append(
            len(f"{header}\n{chunk.text}")
            if candidate.rag_chunk_context_header_enabled and header
            else len(chunk.text)
        )
        views.append(_preview_chunk_view(document_id, recipe_id, chunk, metadata))

    lengths = [len(chunk.text) for chunk in chunks]
    overflow_count = sum(
        1
        for chunk in chunks
        if chunk.metadata.get("chunk_size_compliance") in {"overflow", "overflow_justified"}
    )
    embedding_overflow_count = sum(
        1 for length in search_lengths if length > EMBEDDING_INPUT_MAX_CHARS
    )
    warnings: list[str] = []
    if overflow_count:
        warnings.append(f"設定サイズを超える chunk が {overflow_count} 件あります。")
    if embedding_overflow_count:
        warnings.append(
            f"embedding 入力上限を超える chunk が {embedding_overflow_count} 件あります。"
        )
    return ApiResponse(
        data=DocumentChunkPreviewResponse(
            chunks=views,
            stats=DocumentChunkPreviewStats(
                chunk_count=len(chunks),
                min_chars=min(lengths, default=0),
                average_chars=(round(sum(lengths) / len(lengths), 1) if lengths else 0),
                max_chars=max(lengths, default=0),
                overflow_count=overflow_count,
                embedding_overflow_count=embedding_overflow_count,
            ),
            warnings=warnings,
        )
    )


def _preview_chunk_view(
    document_id: str,
    recipe_id: str,
    chunk: Chunk,
    metadata: dict[str, str | int | float | bool | None],
) -> DocumentChunkView:
    """永続化前の Chunk を既存 UI view へ写す。"""

    def optional_int(value: object) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float, str)):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    bbox: list[float] | None = None
    raw_bbox = metadata.get("bbox")
    try:
        parsed_bbox = json.loads(raw_bbox) if isinstance(raw_bbox, str) else raw_bbox
        if isinstance(parsed_bbox, list) and len(parsed_bbox) == 4:
            bbox = [float(value) for value in parsed_bbox]
    except (TypeError, ValueError, json.JSONDecodeError):
        bbox = None
    element_ids = [
        value.strip()
        for value in str(metadata.get("element_ids") or "").split(",")
        if value.strip()
    ]
    page_start = optional_int(metadata.get("page_start")) or optional_int(
        metadata.get("page_number")
    )
    return DocumentChunkView(
        document_id=document_id,
        chunk_id=f"preview:{recipe_id}:{chunk.index}",
        chunk_index=chunk.index,
        text=chunk.text,
        page_start=page_start,
        page_end=optional_int(metadata.get("page_end")) or page_start,
        bbox=bbox,
        section_path=str(metadata["section_path"]) if metadata.get("section_path") else None,
        content_kind=str(metadata["content_kind"]) if metadata.get("content_kind") else None,
        chunk_group_id=(
            str(metadata["chunk_group_id"]) if metadata.get("chunk_group_id") else None
        ),
        source_parser=(str(metadata["source_parser"]) if metadata.get("source_parser") else None),
        element_ids=element_ids,
        metadata=metadata,
    )


@router.get("/{document_id}/recipes/{recipe_id}/content")
async def document_recipe_content(
    document_id: str,
    recipe_id: str,
    variant: Annotated[Literal["original", "prepared"], Query()] = "original",
    disposition: Annotated[Literal["inline", "attachment"], Query()] = "inline",
) -> Response:
    """選択レシピの原本または固有のファイル準備 artifact を返す。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if detail is None or row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    artifact = (
        DocumentPreprocessArtifact.model_validate(row["preprocess_artifact"])
        if row.get("preprocess_artifact")
        else None
    )
    return await document_content_response(
        detail,
        variant=variant,
        disposition=disposition,
        preprocess_artifact=artifact,
    )


async def _recipe_detail_and_artifact(
    document_id: str, recipe_id: str
) -> tuple[DocumentDetail, DocumentPreprocessArtifact | None]:
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if detail is None or row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    artifact = (
        DocumentPreprocessArtifact.model_validate(row["preprocess_artifact"])
        if row.get("preprocess_artifact")
        else None
    )
    return detail, artifact


@router.get(
    "/{document_id}/recipes/{recipe_id}/preview-pages",
    response_model=ApiResponse[DocumentPreviewPages],
)
async def document_recipe_preview_pages(
    document_id: str,
    recipe_id: str,
    variant: Annotated[Literal["original", "prepared"], Query()] = "original",
) -> ApiResponse[DocumentPreviewPages]:
    """選択レシピの原本 / 処理後ファイルのページ一覧(ページ画像のプレビュー用)。"""
    detail, artifact = await _recipe_detail_and_artifact(document_id, recipe_id)
    return await _document_preview_pages_response(
        detail, variant=variant, preprocess_artifact=artifact
    )


@router.get("/{document_id}/recipes/{recipe_id}/preview-pages/{page_number}")
async def document_recipe_preview_page_image(
    document_id: str,
    recipe_id: str,
    page_number: Annotated[int, Path(ge=1, le=10000)],
    variant: Annotated[Literal["original", "prepared"], Query()] = "original",
    dpi: Annotated[int, Query(ge=48, le=288)] = 144,
) -> Response:
    """選択レシピの原本 / 処理後ファイルの 1 ページを PNG で返す(bbox の強調を重ねる)。"""
    detail, artifact = await _recipe_detail_and_artifact(document_id, recipe_id)
    return await _document_preview_page_image_response(
        detail, page_number=page_number, variant=variant, dpi=dpi, preprocess_artifact=artifact
    )


async def _document_sections(
    oracle: OracleClient, document_id: str, recipe_id: str | None
) -> DocumentSectionsData:
    """人の修正があればそれを、無ければ抽出結果の章節を返す(#713)。"""
    resolved = await document_sections(oracle, document_id, recipe_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    return resolved[0]


@router.get("/{document_id}/sections", response_model=ApiResponse[DocumentSectionsData])
async def get_document_sections(
    document_id: str,
    recipe_id: Annotated[str | None, Query(max_length=128)] = None,
) -> ApiResponse[DocumentSectionsData]:
    """文書の章節(章節ナビゲーション。#713)。"""
    return ApiResponse(data=await _document_sections(OracleClient(), document_id, recipe_id))


@router.put("/{document_id}/sections", response_model=ApiResponse[DocumentSectionsData])
async def save_document_sections(
    document_id: str, request: DocumentSectionsSaveRequest
) -> ApiResponse[DocumentSectionsData]:
    """人が修正した章節を保存する(すべての処理レシピで共有。#713)。"""
    oracle = OracleClient()
    resolved = await document_sections(oracle, document_id, request.recipe_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    current, previous_removed = resolved
    errors = section_errors(request.sections, current.page_count)
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors[:5]))
    try:
        # 保存した後は「新しく抽出」の印を外す。人が消した抽出の章節は覚えて、足し直さない(#721)。
        extracted = extraction_sections(
            *await sections_extraction(oracle, document_id, request.recipe_id)
        )
        await oracle.save_document_sections(
            document_id,
            {
                "sections": [
                    section.model_copy(update={"added_from_extraction": False}).model_dump()
                    for section in request.sections
                ],
                "removed_source_ids": removed_source_ids(
                    previous_removed, extracted, request.sections
                ),
            },
            base_revision=request.base_revision,
        )
    except DocumentSectionsConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="読み込んだ後に、ほかの操作で章節が保存されました。読み込み直してから直してください。",
        ) from exc
    # 章節を直しても chunk・embedding は作り直さず、章節と chunk の対応だけを作り直す（#1438）。
    await refresh_document_section_map(document_id, oracle=oracle, reason="sections_saved")
    return ApiResponse(data=await _document_sections(oracle, document_id, request.recipe_id))


@router.delete("/{document_id}/sections", response_model=ApiResponse[DocumentSectionsData])
async def reset_document_sections(
    document_id: str,
    recipe_id: Annotated[str | None, Query(max_length=128)] = None,
) -> ApiResponse[DocumentSectionsData]:
    """人の修正を捨て、抽出結果の章節に戻す(#713)。"""
    oracle = OracleClient()
    if not await oracle.document_exists(document_id):
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    await oracle.delete_document_sections(document_id)
    await refresh_document_section_map(document_id, oracle=oracle, reason="sections_reset")
    return ApiResponse(data=await _document_sections(oracle, document_id, recipe_id))


@router.get(
    "/{document_id}/recipes/{recipe_id}/extraction-export",
    response_model=ApiResponse[DocumentExtractionExport],
)
async def export_document_recipe_extraction(
    document_id: str,
    recipe_id: str,
    format: Annotated[DocumentExtractionExportFormat, Query()] = (
        DocumentExtractionExportFormat.MARKDOWN
    ),
    download: Annotated[bool, Query()] = False,
) -> ApiResponse[DocumentExtractionExport] | Response:
    """選択レシピの抽出・active chunks を返す。

    `download=true` のときは、同じ内容をファイルとして返す(`Content-Disposition: attachment`。
    文書の詳細の「抽出エクスポート」「Chunk / Citation」のダウンロード。#561)。
    """
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if detail is None or row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    extraction_recipe_id = row.get("active_extraction_recipe_id")
    artifact = (
        await oracle.get_document_extraction_artifact(
            document_id=document_id,
            extraction_recipe_id=str(extraction_recipe_id),
        )
        if extraction_recipe_id
        else None
    )
    if artifact is None or not artifact.get("extraction_json"):
        raise HTTPException(status_code=404, detail="抽出結果が見つかりません。")
    extraction = StructuredExtraction.model_validate(artifact["extraction_json"])
    payload = extraction.to_document_payload()
    chunks: list[DocumentChunkView] = []
    if format == DocumentExtractionExportFormat.CHUNKS:
        chunk_set_id = row.get("active_chunk_set_id")
        chunks = await oracle.list_chunk_set_chunks(str(chunk_set_id)) if chunk_set_id else []
        payload = {"chunks": [chunk.model_dump(mode="json") for chunk in chunks]}
    content = _document_extraction_export_content(format, extraction, payload)
    if download:
        return _document_extraction_export_download_response(
            format,
            content,
            file_name=detail.file_name,
            slot_no=row.get("slot_no"),
        )
    return ApiResponse(
        data=DocumentExtractionExport(
            document_id=document_id,
            file_name=detail.file_name,
            format=format,
            content_type=_document_extraction_export_content_type(format),
            content=content,
            payload=(
                payload
                if format
                not in {
                    DocumentExtractionExportFormat.MARKDOWN,
                    DocumentExtractionExportFormat.HTML,
                }
                else {}
            ),
            chunks=chunks,
            parser_backend=_extraction_parser_backend(extraction),
            parser_profile=_extraction_parser_profile(extraction),
            page_count=len(extraction.pages),
            element_count=len(extraction.elements),
            table_count=len(extraction.tables),
            asset_count=len(extraction.assets),
        )
    )


@router.post(
    "/{document_id}/recipes/{recipe_id}/approve",
    response_model=ApiResponse[IngestionJob],
)
async def approve_document_recipe(
    http_request: Request,
    document_id: str,
    recipe_id: str,
    body: DocumentApproveRequest | None = None,
) -> ApiResponse[IngestionJob]:
    """選択レシピの確認待ち工程を承認して次工程を投入する。"""
    enforce_rate_limit("ingest", http_request)
    oracle = OracleClient()
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    status = FileStatus(str(row.get("status") or FileStatus.UPLOADED.value))
    if status == FileStatus.PREPROCESSED:
        phase = IngestionJobPhase.EXTRACT
    elif status == FileStatus.REVIEW:
        if body is not None and (
            body.element_edits or body.table_cell_edits or body.raw_text is not None
        ):
            await _apply_recipe_review_text_edits(document_id, recipe_id, body)
        phase = IngestionJobPhase.CHUNK
    elif status == FileStatus.CHUNKED:
        phase = IngestionJobPhase.INDEX
    else:
        raise HTTPException(status_code=409, detail="確認待ちのレシピのみ承認できます。")
    return await enqueue_document_recipe_job(document_id, recipe_id, phase)


@router.patch(
    "/{document_id}/recipes/{recipe_id}/review-edits",
    response_model=ApiResponse[DocumentRecipeView],
)
async def save_document_recipe_review_edits(
    http_request: Request,
    document_id: str,
    recipe_id: str,
    body: DocumentReviewEditsRequest,
) -> ApiResponse[DocumentRecipeView]:
    enforce_rate_limit("ingest", http_request)
    await _apply_recipe_review_text_edits(document_id, recipe_id, body)
    oracle = OracleClient()
    row = await oracle.get_document_recipe(document_id, recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    return ApiResponse(data=await _document_recipe_view(oracle, row))


def _candidate_chunking_settings(base: Settings, overrides: Mapping[str, object]) -> Settings:
    """global 設定に chunking 上書きを重ねた候補レシピ設定を返す(cross-field 検証込み)。

    model_copy は Settings の model_validator を再実行しないため、chunking の相互制約だけ
    ここで明示検証する(不正なら 422)。parser/前処理は変えない=既存抽出を再利用できる。
    """
    candidate = base.model_copy(update=dict(overrides))
    if candidate.rag_chunk_overlap >= candidate.rag_chunk_size:
        raise HTTPException(
            status_code=422, detail="overlap は chunk_size より小さくしてください。"
        )
    if (
        candidate.rag_chunking_strategy in CHUNKING_STRATEGIES_WITH_MIN_CHARS
        and candidate.rag_chunk_min_chars >= candidate.rag_chunk_size
    ):
        raise HTTPException(
            status_code=422, detail="min_chars は chunk_size より小さくしてください。"
        )
    return candidate


@router.get("/{document_id}/chunk-sets", response_model=ApiResponse[list[DocumentChunkSet]])
async def list_document_chunk_sets(document_id: str) -> ApiResponse[list[DocumentChunkSet]]:
    """文書の chunk_set(variant)一覧を返す。KB 詳細での variant 可視化に使う。"""
    oracle = OracleClient()
    # 状態・ハッシュだけを使う。抽出結果などの JSON 列は読まない(#341)。
    detail = await oracle.get_document_summary(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    rows = await oracle.list_document_chunk_sets(document_id)
    plan, configs = await _materialization_plan_for_document(oracle, detail)
    effective_settings, _config = await _resolve_ingestion_settings(oracle, document_id)
    effective_by_kb = _effective_ingestion_settings_by_kb(effective_settings, configs)
    persisted_layers = await oracle.list_artifact_layers_for_chunk_sets(
        [str(row.get("chunk_set_id")) for row in rows if row.get("chunk_set_id") is not None]
    )
    # レシピの chunk_set はレシピの設定でレイヤーを判定する(#494)。
    recipe_settings = (
        await _recipe_settings_by_id(oracle, document_id)
        if any(row.get("recipe_id") is not None for row in rows)
        else {}
    )
    # 作り直しの判定に使う今の入力(項目の定義は文書が属する KB の定義か全体の既定。#548 / #550)。
    current_inputs = current_layer_inputs(
        effective_settings,
        current_field_definitions(await oracle.list_document_extraction_field_sets(document_id)),
    )
    graph_counts = (
        await oracle.graph_entity_counts_for_chunk_sets([str(row["chunk_set_id"]) for row in rows])
        if rows
        else {}
    )
    chunk_sets: list[DocumentChunkSet] = []
    for row in rows:
        chunk_set = DocumentChunkSet.model_validate(row)
        if chunk_set.extraction_recipe_id:
            extraction = await oracle.get_document_extraction_artifact(
                document_id=document_id,
                extraction_recipe_id=chunk_set.extraction_recipe_id,
            )
            if extraction is not None:
                chunk_set.extraction_status = DocumentLayerStatusName(
                    str(extraction.get("status") or DocumentLayerStatusName.PLANNED_ONLY.value)
                )
                chunk_set.extraction_reason = (
                    str(extraction["reason"]) if extraction.get("reason") is not None else None
                )
        settings_for_recipe = (
            recipe_settings.get(str(row["recipe_id"])) if row.get("recipe_id") is not None else None
        )
        if settings_for_recipe is not None:
            chunk_set.layer_statuses = _recipe_layer_statuses_for_chunk_set(
                chunk_set.chunk_set_id,
                settings_for_recipe,
                persisted_layers,
                current_inputs=current_inputs,
            )
        elif plan is not None:
            chunk_set.layer_statuses = _layer_statuses_for_chunk_set(
                chunk_set.chunk_set_id,
                plan,
                effective_by_kb,
                persisted_layers,
                current_inputs=current_inputs,
            )
        if chunk_set.layer_statuses is not None and chunk_set.layer_statuses.graph.requested:
            count = graph_counts.get(chunk_set.chunk_set_id, 0)
            if chunk_set.status in {"ERROR", "FAILED"}:
                chunk_set.layer_statuses.graph = chunk_set.layer_statuses.graph.model_copy(
                    update={
                        "status": DocumentLayerStatusName.ERROR,
                        "reason": (
                            "索引の処理に失敗したため、文書構造索引の構築は完了していません。"
                        ),
                    }
                )
            elif count > 0 or chunk_set.status == "INDEXED":
                chunk_set.layer_statuses.graph = chunk_set.layer_statuses.graph.model_copy(
                    update={
                        "status": DocumentLayerStatusName.MATERIALIZED,
                        "reason": f"文書構造索引を構築済みです（{count} 件）。"
                        if count
                        else (
                            "文書構造索引の構築は完了しましたが、対象の見出しがありません（0 件）。"
                        ),
                    }
                )
        chunk_sets.append(chunk_set)
    return ApiResponse(data=chunk_sets)


@dataclass(frozen=True)
class _RecipeJobResult:
    """レシピの job の工程の結果。どちらも呼び出し側が job を SUCCEEDED にできた後に使う。

    ``next_phase`` は現在ジョブ完了後に自動投入すべき次フェーズ(不要なら ``None``)。
    ``success_outcome`` は active への切り替えまで終えた job の、まだ記録していない成功の
    監査・metric(索引まで進まなかった job は ``None``。#504 / #514)。
    """

    next_phase: IngestionJobPhase | None = None
    success_outcome: IngestionSuccessOutcome | None = None


async def _materialize_experiment_candidate(
    oracle: OracleClient,
    job: IngestionJob,
    *,
    cancel_checker: Callable[[], Awaitable[bool]] | None = None,
) -> _RecipeJobResult:
    """文書レシピの job で、設定 snapshot から新しい chunk_set を隔離構築する。

    既存 active 出力を構築中に変更しない。成功時だけ active を原子的に差し替える。
    ``recipe_id`` の無い job（移行前の旧実験 API の job）は実行せず、
    利用者向けのエラーで止める（#486）。

    戻り値の ``next_phase`` は「現在ジョブ完了後に自動投入すべき次フェーズ」。
    現在ジョブが RUNNING のまま新ジョブを作るとレシピ行ロックのガードで弾かれるため、
    投入自体は呼び出し側(``_run_ingestion_job``)が現在ジョブ SUCCEEDED 後に行う。
    成功の監査・metric(``success_outcome``)もここでは記録せず、呼び出し側が job を
    SUCCEEDED にできた後に 1 回だけ記録する(#514)。
    """
    recipe_id = job.recipe_id
    if recipe_id is None:
        raise IngestionUserError(
            "旧いレシピ実験の job は実行できません。文書の処理レシピから再実行してください。"
        )
    detail = await oracle.get_document(job.document_id)
    if detail is None:
        raise IngestionUserError("ドキュメントが見つかりません。")
    if not detail.content_sha256:
        raise IngestionUserError("文書のソースハッシュが未確定です。")
    raw_config = (job.settings_overrides or {}).get("processing_config")
    if isinstance(raw_config, Mapping):
        candidate_config = DocumentProcessingConfig.model_validate(raw_config)
    else:
        _base_settings, candidate_config = await _resolve_ingestion_settings(
            oracle, job.document_id, recipe_id
        )
    candidate_settings, effective_candidate_config = _merge_document_processing_config(
        candidate_config
    )
    base_chunk_set_id = compute_chunk_set_id(detail.content_sha256, candidate_settings)
    candidate_chunk_set_id = hashlib.sha256(
        f"{base_chunk_set_id}:{recipe_id}:{job.recipe_revision}:{job.id}".encode()
    ).hexdigest()
    await _raise_if_job_cancelled(cancel_checker)
    await oracle.update_document_recipe_status(
        recipe_id=recipe_id,
        status=_PHASE_TO_RUNNING_STATUS[job.phase],
    )
    pipeline = IngestionPipeline(
        oracle=oracle,
        settings=candidate_settings,
        recipe_id=recipe_id,
        recipe_revision=job.recipe_revision,
    )
    extraction_recipe_id = compute_extraction_recipe_id(detail.content_sha256, candidate_settings)
    if job.phase in {
        IngestionJobPhase.CHUNK,
        IngestionJobPhase.INDEX,
    }:
        recipe_row = await oracle.get_document_recipe(job.document_id, recipe_id)
        active_extraction_recipe_id = (
            recipe_row.get("active_extraction_recipe_id") if recipe_row is not None else None
        )
        if active_extraction_recipe_id is None:
            raise IngestionUserError("索引対象の抽出結果が見つかりません。")
        extraction_recipe_id = str(active_extraction_recipe_id)
        if release := active_release():
            artifact = await oracle.get_document_extraction_artifact(
                document_id=job.document_id, extraction_recipe_id=extraction_recipe_id
            )
            payload = (artifact or {}).get("extraction_json") or {}
            artifacts = payload.get("parser_artifacts", {}) if isinstance(payload, dict) else {}
            if not await artifact_language_compatible(oracle, artifacts, "CHUNK"):
                raise IngestionUserError(
                    "抽出結果の言語資源を再利用できません。文書解析から再処理してください。"
                )

    if job.phase == IngestionJobPhase.INDEX:
        pending = await oracle.get_latest_recipe_chunk_set(
            recipe_id,
            status="CHUNKED",
            active=False,
        )
        if pending is None:
            active = await oracle.get_latest_recipe_chunk_set(
                recipe_id, status="INDEXED", active=True
            )
            if active is None:
                raise IngestionUserError(
                    "索引対象の Chunk が見つかりません。Chunk 作成から再開してください。"
                )
            if active_release() and not await artifact_language_compatible(oracle, active, "INDEX"):
                raise IngestionUserError(
                    "有効な Chunk の言語資源が対象版と一致しません。"
                    "文書解析または Chunk 作成から再処理してください。"
                )
            await oracle.upsert_chunk_set(
                chunk_set_id=candidate_chunk_set_id,
                document_id=job.document_id,
                recipe_id=recipe_id,
                extraction_recipe_id=extraction_recipe_id,
                recipe_subset=_processing_recipe_snapshot(
                    candidate_config, effective_candidate_config
                ),
                status="CHUNKED",
            )
            await oracle.clone_recipe_chunk_set_chunks(
                recipe_id=recipe_id,
                source_chunk_set_id=str(active["chunk_set_id"]),
                target_chunk_set_id=candidate_chunk_set_id,
            )
        else:
            if active_release() and not await artifact_language_compatible(
                oracle, pending, "INDEX"
            ):
                raise IngestionUserError(
                    "保存した分割結果の言語資源を再利用できません。"
                    "文書解析または Chunk 作成から再処理してください。"
                )
            if (release := active_release()) and pending.get(
                "language_pack_release_id"
            ) != release.id:
                # 旧版の途中成果物は書き換えず、新版の索引を別集合として作る。
                await oracle.upsert_chunk_set(
                    chunk_set_id=candidate_chunk_set_id,
                    document_id=job.document_id,
                    recipe_id=recipe_id,
                    extraction_recipe_id=extraction_recipe_id,
                    recipe_subset=_processing_recipe_snapshot(
                        candidate_config, effective_candidate_config
                    ),
                    status="CHUNKED",
                )
                await oracle.clone_recipe_chunk_set_chunks(
                    recipe_id=recipe_id,
                    source_chunk_set_id=str(pending["chunk_set_id"]),
                    target_chunk_set_id=candidate_chunk_set_id,
                )
            else:
                candidate_chunk_set_id = str(pending["chunk_set_id"])
        # 成功の監査・metric は工程の中では記録せず、active への切り替えの後に 1 回だけ記録する
        # (#493 / #504)。
        await pipeline.index_chunked(
            job.document_id,
            chunk_set_id=candidate_chunk_set_id,
            record_outcome=False,
            cancel_checker=cancel_checker,
        )
    elif job.phase == IngestionJobPhase.CHUNK:
        # Chunk 作成は途中の工程のため、成功の監査を出さない。
        await pipeline.chunk_reviewed(
            job.document_id,
            chunk_set_id=candidate_chunk_set_id,
            record_outcome=False,
            cancel_checker=cancel_checker,
        )
        chunk_count = await oracle.count_chunk_set_chunks(candidate_chunk_set_id)
        await oracle.upsert_chunk_set(
            chunk_set_id=candidate_chunk_set_id,
            document_id=job.document_id,
            recipe_id=recipe_id,
            extraction_recipe_id=extraction_recipe_id,
            recipe_subset=_processing_recipe_snapshot(candidate_config, effective_candidate_config),
            status="CHUNKED",
        )
        await oracle.mark_chunk_set_chunked(
            chunk_set_id=candidate_chunk_set_id,
            chunk_count=chunk_count,
        )
        if not candidate_settings.rag_auto_index_after_chunk_enabled:
            return _RecipeJobResult()
        await pipeline.index_chunked(
            job.document_id,
            chunk_set_id=candidate_chunk_set_id,
            record_outcome=False,
            cancel_checker=cancel_checker,
        )
    else:
        prepared_artifact: DocumentPreprocessArtifact | None = None
        if job.phase == IngestionJobPhase.EXTRACT:
            recipe_row = await oracle.get_document_recipe(job.document_id, recipe_id)
            if recipe_row is None or not recipe_row.get("preprocess_artifact"):
                raise IngestionUserError(
                    "処理後ファイルが見つかりません。ファイル準備から再処理してください。"
                )
            prepared_artifact = DocumentPreprocessArtifact.model_validate(
                recipe_row["preprocess_artifact"]
            )
            if not prepared_artifact.object_storage_path:
                raise IngestionUserError(
                    "処理後ファイルが見つかりません。ファイル準備から再処理してください。"
                )
            try:
                data = await ObjectStorageClient().get(prepared_artifact.object_storage_path)
            except (FileNotFoundError, ValueError) as exc:
                raise IngestionUserError("処理後ファイルを読み込めませんでした。") from exc
            source_profile = _source_profile_for_detail(detail)
        else:
            # レシピの job の失敗はレシピの行だけを ERROR にする(文書の status は変えない。#504)。
            data, source_profile = await _load_source_bytes(
                oracle, job.document_id, detail, mark_document_error=False
            )
        await pipeline.ingest(
            document_id=job.document_id,
            image_bytes=data,
            prompt="ドキュメントを日本語で OCR し、本文テキストを抽出してください。",
            content_type=(
                prepared_artifact.content_type
                if prepared_artifact is not None and prepared_artifact.content_type
                else detail.content_type or "application/octet-stream"
            ),
            source_profile=source_profile,
            chunk_set_id=candidate_chunk_set_id,
            # 成功の監査は active への切り替えの後に記録する(REVIEW / PREPROCESSED で止まる場合は
            # 索引まで進まないため、記録するものが無い)。
            record_outcome=False,
            original_object_storage_path=detail.object_storage_path,
            prepared_artifact=prepared_artifact,
            manage_document_state=False,
            cancel_checker=cancel_checker,
            # 処理後ファイルを読む工程でも、監査には文書に記録済みの原本の hash とサイズを使う。
            source_sha256=detail.content_sha256,
            source_size=detail.file_size_bytes,
        )
        recipe_row = await oracle.get_document_recipe(job.document_id, recipe_id)
        recipe_status = (
            FileStatus(str(recipe_row.get("status")))
            if recipe_row is not None
            else FileStatus.ERROR
        )
        if (
            recipe_status == FileStatus.REVIEW
            and candidate_settings.rag_auto_chunk_after_extract_enabled
        ):
            # 現在の EXTRACT ジョブがまだ RUNNING のため、ここで CHUNK ジョブを作ると
            # レシピ行ロックのガード(同一レシピの QUEUED/RUNNING 拒否)で弾かれる。
            # 投入は呼び出し側が現在ジョブ SUCCEEDED 後に行うので、決定だけ返す。
            return _RecipeJobResult(next_phase=IngestionJobPhase.CHUNK)
        if recipe_status in {FileStatus.PREPROCESSED, FileStatus.REVIEW}:
            return _RecipeJobResult()
        active_extraction_recipe_id = (
            recipe_row.get("active_extraction_recipe_id") if recipe_row is not None else None
        )
        if active_extraction_recipe_id is None:
            raise IngestionUserError("索引対象の抽出結果が見つかりません。")
        extraction_recipe_id = str(active_extraction_recipe_id)

    chunk_count = await oracle.count_chunk_set_chunks(candidate_chunk_set_id)
    await oracle.upsert_chunk_set(
        chunk_set_id=candidate_chunk_set_id,
        document_id=job.document_id,
        recipe_id=recipe_id,
        extraction_recipe_id=extraction_recipe_id,
        recipe_subset=_processing_recipe_snapshot(candidate_config, effective_candidate_config),
    )
    await oracle.mark_chunk_set_indexed(
        chunk_set_id=candidate_chunk_set_id, chunk_count=chunk_count, vector_count=chunk_count
    )
    # active の切り替えは検索対象を変えるため、取り消された job では行わない(#305)。
    await _raise_if_job_cancelled(cancel_checker)
    # 派生情報レイヤーの実体化状態を、active にする chunk_set について記録する(#494)。
    await _record_recipe_artifact_layers(
        oracle,
        document_id=job.document_id,
        chunk_set_id=candidate_chunk_set_id,
        extraction_recipe_id=extraction_recipe_id,
        settings=candidate_settings,
    )
    await oracle.activate_recipe_chunk_set(
        recipe_id=recipe_id,
        chunk_set_id=candidate_chunk_set_id,
        extraction_recipe_id=extraction_recipe_id,
        materialized_revision=job.recipe_revision,
    )
    # 文書一覧の legacy 集約状態。少なくとも1レシピが検索可能なら INDEXED とする。
    await oracle.update_document_status(job.document_id, FileStatus.INDEXED)
    # INDEX まで到達した経路は自動進行の追加投入不要(CHUNK→INDEX はここで完結)。
    # 成功の監査・metric はここでは記録せず、呼び出し側が job を SUCCEEDED にできた後に記録する。
    # この後の status の更新の失敗や SUCCEEDED を書く直前の取り消しで、成功しなかった job の
    # 成功の監査が残らないようにする(#504 / #514)。
    return _RecipeJobResult(success_outcome=pipeline.deferred_success_outcome)


async def _materialization_plan_for_document(
    oracle: OracleClient,
    detail: DocumentSummary,
    *,
    global_settings: Settings | None = None,
) -> tuple[MaterializationPlan | None, dict[str, KnowledgeBaseAdapterConfig]]:
    """文書の有効レシピと所属 KB scope から materialization plan を復元する。"""
    configs = dict(await oracle.list_document_knowledge_base_configs(detail.id))
    if not detail.content_sha256 or not configs:
        return None, configs
    settings = global_settings
    if settings is None:
        settings, _config = await _resolve_ingestion_settings(oracle, detail.id)
    return plan_document_materializations(detail.content_sha256, settings, configs), configs


def _effective_ingestion_settings_by_kb(
    document_settings: Settings,
    configs: Mapping[str, KnowledgeBaseAdapterConfig],
) -> dict[str, Settings]:
    """KB ごとの有効な構築設定を返す(3 層モデル: レシピは文書で KB 共通)。

    レイヤー状態表示を文書の単一レシピに揃え、materialization と一致させる。
    KB 別取込上書きは使わない。
    """
    return {knowledge_base_id: document_settings for knowledge_base_id in configs}


def _layer_statuses_for_chunk_set(
    chunk_set_id: str,
    plan: MaterializationPlan,
    effective_by_kb: Mapping[str, Settings],
    persisted_layers: Mapping[str, Mapping[str, object]] | None = None,
    *,
    current_inputs: Mapping[str, object] | None = None,
) -> DocumentChunkSetLayerStatuses:
    """派生情報レイヤーの現在状態を chunk_set 単位で作る。"""
    return DocumentChunkSetLayerStatuses(
        metadata=_layer_status_for_chunk_set(
            chunk_set_id,
            plan,
            effective_by_kb,
            persisted_layers or {},
            layer="metadata",
            user_label="項目抽出",
            current_inputs=current_inputs,
        ),
        graph=_layer_status_for_chunk_set(
            chunk_set_id,
            plan,
            effective_by_kb,
            persisted_layers or {},
            layer="graph",
            user_label="文書構造索引",
            current_inputs=current_inputs,
        ),
        navigation=_layer_status_for_chunk_set(
            chunk_set_id,
            plan,
            effective_by_kb,
            persisted_layers or {},
            layer="navigation",
            user_label="ナビゲーション",
            current_inputs=current_inputs,
        ),
    )


def _layer_status_for_chunk_set(
    chunk_set_id: str,
    plan: MaterializationPlan,
    effective_by_kb: Mapping[str, Settings],
    persisted_layers: Mapping[str, Mapping[str, object]],
    *,
    layer: str,
    user_label: str,
    current_inputs: Mapping[str, object] | None = None,
) -> DocumentMaterializationLayerStatus:
    requested_ids = _requested_layer_ids_for_chunk_set(
        chunk_set_id,
        plan,
        effective_by_kb,
        layer=layer,
    )
    if not requested_ids:
        return _not_requested_layer_status(user_label)
    if len(requested_ids) > 1:
        return DocumentMaterializationLayerStatus(
            requested=True,
            status=DocumentLayerStatusName.PLANNED_ONLY,
            reason=(
                f"{user_label}は複数の方針がこのチャンク構成を共有しています。"
                "現時点では計画だけを表示しています。"
            ),
        )
    return _requested_layer_status(
        requested_ids[0],
        persisted_layers,
        user_label=user_label,
        current_inputs=current_inputs,
    )


def _not_requested_layer_status(user_label: str) -> DocumentMaterializationLayerStatus:
    return DocumentMaterializationLayerStatus(
        requested=False,
        status=DocumentLayerStatusName.NOT_REQUESTED,
        reason=f"現在の構築設定では{user_label}を使用しません。",
    )


def _requested_layer_status(
    layer_id: str,
    persisted_layers: Mapping[str, Mapping[str, object]],
    *,
    user_label: str,
    current_inputs: Mapping[str, object] | None = None,
) -> DocumentMaterializationLayerStatus:
    """要求された派生層の状態。実体化の記録があればそれを、無ければ計画だけとして返す。

    ``current_inputs`` を渡すと、記録した指紋と比べて「作り直しが必要」を付ける(#550)。
    """
    persisted = persisted_layers.get(layer_id)
    if persisted is not None:
        changed = (
            changed_layer_inputs(persisted.get("input_fingerprint"), current_inputs)
            if current_inputs is not None
            else []
        )
        return DocumentMaterializationLayerStatus(
            layer_id=layer_id,
            requested=bool(persisted.get("requested", True)),
            status=DocumentLayerStatusName(
                str(persisted.get("status") or DocumentLayerStatusName.PLANNED_ONLY.value)
            ),
            reason=str(persisted["reason"]) if persisted.get("reason") is not None else None,
            rebuild_required=bool(changed),
            rebuild_inputs=changed,
        )
    return DocumentMaterializationLayerStatus(
        layer_id=layer_id,
        requested=True,
        status=DocumentLayerStatusName.PLANNED_ONLY,
        reason=f"{user_label}は構築計画に含まれていますが、まだ実体化していません。",
    )


# 派生情報レイヤー(chunk_set に重なる層)と利用者向けの名前。
_ARTIFACT_LAYER_LABELS: tuple[tuple[str, str], ...] = (
    ("metadata", "項目抽出"),
    ("graph", "文書構造索引"),
    ("navigation", "ナビゲーション"),
)


def _recipe_layer_id(chunk_set_id: str, settings: Settings, layer: str) -> str:
    """レシピの chunk_set に重なる派生層の ID(planner と同じ規則で chunk_set_id から作る)。"""
    if layer == "metadata":
        return compute_metadata_layer_id(chunk_set_id, settings)
    if layer == "graph":
        return compute_graph_layer_id(chunk_set_id, settings)
    return compute_nav_layer_id(chunk_set_id, settings)


def _recipe_layer_statuses_for_chunk_set(
    chunk_set_id: str,
    settings: Settings,
    persisted_layers: Mapping[str, Mapping[str, object]],
    *,
    current_inputs: Mapping[str, object] | None = None,
) -> DocumentChunkSetLayerStatuses:
    """レシピの chunk_set の派生情報レイヤーの状態を、そのレシピの構築設定から作る(#494)。

    レシピの job の chunk_set ID は recipe / revision / job ごとの hash で、planner の
    chunk_set ID(content hash + 設定)とは一致しない。そのため plan ではなく、レシピの
    設定と chunk_set ID から層 ID を作り、job の完了時に記録した実体化の状態を引く。
    """
    statuses = {
        layer: (
            _requested_layer_status(
                _recipe_layer_id(chunk_set_id, settings, layer),
                persisted_layers,
                user_label=user_label,
                current_inputs=current_inputs,
            )
            if _layer_requested(layer, settings)
            else _not_requested_layer_status(user_label)
        )
        for layer, user_label in _ARTIFACT_LAYER_LABELS
    }
    return DocumentChunkSetLayerStatuses(
        metadata=statuses["metadata"],
        graph=statuses["graph"],
        navigation=statuses["navigation"],
    )


async def _recipe_settings_by_id(oracle: OracleClient, document_id: str) -> dict[str, Settings]:
    """文書のレシピごとの有効な構築設定(global 既定にレシピの上書きを重ねたもの)。"""
    result: dict[str, Settings] = {}
    for row in await oracle.list_document_recipes(document_id):
        config = DocumentProcessingConfig.model_validate(row.get("processing_config") or {})
        result[str(row["recipe_id"])], _effective = _merge_document_processing_config(config)
    return result


async def _record_recipe_artifact_layers(
    oracle: OracleClient,
    *,
    document_id: str,
    chunk_set_id: str,
    extraction_recipe_id: str,
    settings: Settings,
) -> None:
    """レシピの job が索引を終えた chunk_set に、要求された派生層の実体化状態を記録する(#494)。

    判定はレシピの抽出結果(extraction 層)で行う。レシピの job は文書の legacy の抽出列を
    更新しないため、文書の抽出列では判定しない。
    """
    requested = [
        (layer, user_label)
        for layer, user_label in _ARTIFACT_LAYER_LABELS
        if _layer_requested(layer, settings)
    ]
    if not requested:
        return
    artifact = await oracle.get_document_extraction_artifact(
        document_id=document_id,
        extraction_recipe_id=extraction_recipe_id,
    )
    raw_extraction = artifact.get("extraction_json") if artifact is not None else None
    extraction = raw_extraction if isinstance(raw_extraction, Mapping) else None
    fields_configured = await _document_has_field_definitions(oracle, document_id)
    for layer, user_label in requested:
        status, reason = _materialized_layer_state(
            layer=layer,
            user_label=user_label,
            extraction=extraction,
            settings=settings,
            field_definitions_configured=fields_configured,
        )
        if layer == "graph":
            counts = await oracle.graph_entity_counts_for_chunk_sets([chunk_set_id])
            count = counts.get(chunk_set_id, 0)
            status = DocumentLayerStatusName.MATERIALIZED
            reason = (
                f"文書構造索引を構築済みです（{count} 件）。"
                if count
                else "文書構造索引の構築は完了しましたが、対象の見出しがありません（0 件）。"
            )
        await oracle.upsert_artifact_layer(
            layer_id=_recipe_layer_id(chunk_set_id, settings, layer),
            layer_kind=layer,
            parent_chunk_set_id=chunk_set_id,
            document_id=document_id,
            requested=True,
            status=status.value,
            reason=reason,
            metrics=_layer_metrics(layer, extraction),
            input_fingerprint=recorded_layer_fingerprint(layer, extraction, settings),
        )


def _requested_layer_ids_for_chunk_set(
    chunk_set_id: str,
    plan: MaterializationPlan,
    effective_by_kb: Mapping[str, Settings],
    *,
    layer: str,
) -> tuple[str, ...]:
    knowledge_base_ids = plan.chunk_sets.get(chunk_set_id, frozenset())
    layer_map = {
        "metadata": plan.metadata_layers,
        "graph": plan.graph_layers,
        "navigation": plan.nav_layers,
    }.get(layer)
    if not knowledge_base_ids or layer_map is None:
        return ()
    requested: list[str] = []
    for layer_id, owners in layer_map.items():
        relevant_owners = owners & knowledge_base_ids
        if any(
            _layer_requested(layer, effective_by_kb[knowledge_base_id])
            for knowledge_base_id in relevant_owners
            if knowledge_base_id in effective_by_kb
        ):
            requested.append(layer_id)
    return tuple(sorted(requested))


def _layer_requested(layer: str, settings: Settings) -> bool:
    if layer == "metadata":
        return bool(settings.rag_field_extraction_enabled)
    if layer == "graph":
        return settings.rag_graph_profile != "off"
    if layer == "navigation":
        return bool(settings.rag_navigation_summary_enabled or settings.rag_raptor_enabled)
    return False


def _merge_document_processing_config(
    config: DocumentProcessingConfig,
    global_settings: Settings | None = None,
) -> tuple[Settings, DocumentProcessingConfig]:
    """global 既定へ文書の明示上書きだけを重ねる。KB 設定は参照しない。"""
    base = global_settings or get_settings()
    adapter = KnowledgeBaseAdapterConfig(ingestion=config)
    effective_settings = resolve_effective_settings(base, adapter, scope="ingestion")
    # 外部 parser 選択時に自動注入される feature flag も含め、実際の Settings を
    # スナップショットへ投影する。これにより runtime と drift 判定が同じ値を見る。
    effective = resolve_effective_adapter_config(
        effective_settings, KnowledgeBaseAdapterConfig()
    ).ingestion
    return effective_settings, DocumentProcessingConfig.model_validate(
        {
            **effective.model_dump(),
            # 章節の抽出規則の方式は Settings ではなく全体の既定のファイルが持つ(#715)。
            "section_rules_mode": config.section_rules_mode or load_section_rules().mode,
        }
    )


def global_document_processing_config(
    global_settings: Settings | None = None,
) -> DocumentProcessingConfig:
    """レシピで何も上書きしないときの実効値（「グローバル設定に従う」の値。#528）。

    設定の概要の画面が、レシピと同じ解決で全体の既定を表示するために使う。
    """
    return _merge_document_processing_config(DocumentProcessingConfig(), global_settings)[1]


def _processing_recipe_snapshot(
    config: DocumentProcessingConfig,
    effective: DocumentProcessingConfig,
) -> dict[str, object]:
    """chunk_set に刻む文書レシピ。昇格時に継承/上書き状態も復元できる。"""
    return {
        "processing_config": config.model_dump(mode="json", exclude_none=True),
        "effective_processing_config": effective.model_dump(mode="json"),
    }


def _processing_config_drift_groups(
    observed: Mapping[str, object],
    effective: Mapping[str, object],
) -> list[str]:
    """配信中レシピの snapshot と現在の有効設定を比べ、出力が変わる設定群を返す。

    親子階層（small-to-big）の分割パラメータは、その方式を使うときだけ比べる。追加前の snapshot は
    値を持たないため、そのときは rag_poc の既定値(Settings の既定)で分割したものとして扱う。
    """
    small_to_big_in_use = effective.get("chunking_strategy") == SMALL_TO_BIG_STRATEGY
    fixed_delimiter_in_use = effective.get("chunking_strategy") == FIXED_DELIMITER_STRATEGY

    def _observed_value(field: str) -> object:
        value = observed.get(field)
        if value is None and field in SMALL_TO_BIG_PROCESSING_CONFIG_FIELDS:
            return Settings.model_fields[f"rag_{field}"].default
        if value is None and field == "chunk_delimiter":
            # 分割符をレシピに持つ前(#2457)の snapshot は、旧い既定の分割符で分割したものとする。
            return PREVIOUS_DEFAULT_CHUNK_DELIMITER
        return value

    def _compared_fields(fields: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            field
            for field in fields
            if (small_to_big_in_use or field not in SMALL_TO_BIG_PROCESSING_CONFIG_FIELDS)
            and (fixed_delimiter_in_use or field != "chunk_delimiter")
        )

    return [
        group
        for group, fields in DOCUMENT_PROCESSING_OUTPUT_GROUPS.items()
        if any(_observed_value(field) != effective.get(field) for field in _compared_fields(fields))
    ]


def _snapshot_used_removed_chunking_strategy(row: Mapping[str, object] | None) -> bool:
    """chunk_set の snapshot が削除した分割方式(親子階層など)で作られたかを返す。"""
    raw = row.get("recipe_subset") if row else None
    effective = raw.get("effective_processing_config") if isinstance(raw, Mapping) else None
    strategy = effective.get("chunking_strategy") if isinstance(effective, Mapping) else None
    return (
        isinstance(strategy, str)
        and strategy.strip().casefold() in LEGACY_CHUNKING_STRATEGY_ALIASES
    )


@router.get(
    "/{document_id}/ingestion-segments",
    response_model=ApiResponse[list[IngestionSegment]],
)
async def list_document_ingestion_segments(
    document_id: str,
) -> ApiResponse[list[IngestionSegment]]:
    """文書 preview workspace 用に取込 segment/checkpoint 状態を返す。"""
    oracle = OracleClient()
    # 取込中にポーリングされるため、保存済みの segment があれば JSON 列を読まない(#341)。
    summary = await oracle.get_document_summary(document_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    try:
        persisted_segments = await oracle.list_ingestion_segments(document_id)
    except Exception:
        persisted_segments = []
    if persisted_segments:
        return ApiResponse(
            data=[
                _segment_with_progress_defaults(segment, summary) for segment in persisted_segments
            ]
        )
    # 保存済みの segment が無い旧データだけ、抽出結果のページ範囲から segment を組み立てる。
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    effective_settings, _owning = await _resolve_ingestion_settings(oracle, document_id)
    jobs = await oracle.list_document_ingestion_jobs(document_id)
    return ApiResponse(data=_document_ingestion_segments(detail, jobs, effective_settings))


def _segment_with_progress_defaults(
    segment: IngestionSegment,
    detail: DocumentSummary,
) -> IngestionSegment:
    """旧 checkpoint row に progress 表示用の単位を補う。"""
    if segment.progress_unit != "source":
        return segment
    suffix = segment.segment_id.rsplit(":", 1)[-1]
    if suffix.startswith("slide"):
        unit = "slide"
    elif suffix.startswith("sheet"):
        unit = "sheet"
    elif segment.page_start is not None and segment.page_end is not None:
        content_type = (detail.content_type or "").lower()
        unit = (
            "page"
            if content_type == "application/pdf" or content_type.startswith("image/")
            else "source"
        )
    else:
        unit = "source"
    return segment.model_copy(
        update={
            "progress_unit": unit,
            "progress_start": segment.page_start if unit != "source" else None,
            "progress_end": segment.page_end if unit != "source" else None,
        }
    )


@router.get("/{document_id}", response_model=ApiResponse[DocumentDetail])
async def get_document(document_id: str) -> ApiResponse[DocumentDetail]:
    """ドキュメント詳細（抽出本文含む）を返す。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    detail = await _attach_duplicate_source(detail, oracle)
    return ApiResponse(data=detail)


async def _attach_duplicate_source(
    detail: DocumentDetail,
    oracle: OracleClient,
) -> DocumentDetail:
    """重複 skip の理由を画面で説明できるよう、参照元の最小摘要を付ける。"""
    duplicate_id = detail.duplicate_of_document_id
    if duplicate_id is None:
        return detail
    # 表示用の摘要だけを使うため、重複元の JSON 列は読まない(#341)。
    duplicate = await oracle.get_document_summary(duplicate_id)
    if duplicate is None:
        return detail
    return detail.model_copy(
        update={
            "duplicate_source": DuplicateDocumentRef(
                id=duplicate.id,
                file_name=duplicate.file_name,
                status=duplicate.status,
                uploaded_at=duplicate.uploaded_at,
                indexed_at=duplicate.indexed_at,
            )
        }
    )


@router.delete("/{document_id}", response_model=ApiResponse[DocumentDeleteResult])
async def delete_document(document_id: str) -> ApiResponse[DocumentDeleteResult]:
    """ドキュメント本体、検索 index、投入関連行、原本ファイル参照を削除する。

    保存先の原本・成果物は、文書の行の削除と同じ transaction で残した後始末の記録から消す
    （#2452）。消せなかった対象は記録に残り、同じ文書の削除の再試行と取込ワーカーの定期の
    後始末でやり直す。文書の行が無くても、扱える後始末の記録があればやり直す。
    """
    oracle = OracleClient()
    cleanup_store = DocumentCleanupStore(oracle)
    detail = await oracle.get_document(document_id)
    if detail is None:
        return await _retry_document_cleanup(cleanup_store, document_id)
    blocking_jobs = await _list_delete_blocking_ingestion_jobs(oracle, document_id)
    if blocking_jobs:
        raise HTTPException(
            status_code=409,
            detail="取込ジョブが実行中のため削除できません。先にキャンセルしてください。",
        )
    artifact_paths = await _document_artifact_paths(oracle, detail)
    has_oci_reference = any(
        path.strip().startswith("oci://")
        for path in (detail.object_storage_path or "", *artifact_paths)
    )
    targets = DocumentCleanupTargets(
        source_uri=detail.object_storage_path or None,
        artifact_uris=tuple(artifact_paths),
        prefixes=tuple(document_artifact_prefixes(get_settings(), document_id)),
        include_oci=has_oci_reference,
    )

    try:
        task = await cleanup_store.delete_document_with_cleanup(
            document_id, detail.file_name, targets
        )
    except DocumentDeleteBlockedByRunningIngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        if "ORA-00060" not in oracle_error_codes(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail="文書の更新と競合したため削除できませんでした。最新情報を取得してから再実行してください。",
        ) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")

    outcome = await run_document_cleanup(cleanup_store, ObjectStorageClient(), task)
    return _document_delete_response(
        document_id=detail.id,
        file_name=detail.file_name,
        object_storage_path=detail.object_storage_path,
        outcome=outcome,
    )


async def _retry_document_cleanup(
    cleanup_store: DocumentCleanupStore, document_id: str
) -> ApiResponse[DocumentDeleteResult]:
    """文書の行が無いときの削除の再試行。後始末の記録が残っていればやり直す（#2452）。"""
    task = await cleanup_store.find_for_document(document_id)
    if task is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    outcome = await run_document_cleanup(cleanup_store, ObjectStorageClient(), task)
    return _document_delete_response(
        document_id=task.document_id,
        file_name=task.file_name,
        object_storage_path=task.targets.source_uri,
        outcome=outcome,
    )


def _document_delete_response(
    *,
    document_id: str,
    file_name: str,
    object_storage_path: str | None,
    outcome: DocumentCleanupOutcome,
) -> ApiResponse[DocumentDeleteResult]:
    return ApiResponse(
        data=DocumentDeleteResult(
            id=document_id,
            file_name=file_name,
            object_storage_path=object_storage_path,
            object_deleted=outcome.object_deleted,
            artifact_deleted_count=outcome.artifact_deleted_count,
            artifact_delete_failed_count=outcome.artifact_delete_failed_count,
        ),
        warning_messages=outcome.warning_messages,
    )


@router.get("/{document_id}/knowledge-bases", response_model=ApiResponse[list[KnowledgeBaseRef]])
async def list_document_knowledge_bases(
    document_id: str,
) -> ApiResponse[list[KnowledgeBaseRef]]:
    """ドキュメントの所属ナレッジベース一覧を返す。"""
    oracle = OracleClient()
    if not await oracle.document_exists(document_id):
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    return ApiResponse(data=await oracle.list_document_knowledge_bases(document_id))


@router.put("/{document_id}/knowledge-bases", response_model=ApiResponse[list[KnowledgeBaseRef]])
async def replace_document_knowledge_bases(
    document_id: str,
    request: DocumentKnowledgeBaseReplaceRequest,
) -> ApiResponse[list[KnowledgeBaseRef]]:
    """ドキュメントの所属ナレッジベースを指定リストへ置換する。"""
    try:
        refs = await OracleClient().replace_document_knowledge_bases(
            document_id,
            request.knowledge_base_ids,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="ドキュメントまたはナレッジベースが見つかりません。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=refs)


@router.put("/{document_id}/classification", response_model=ApiResponse[DocumentDetail])
async def save_document_classification(
    document_id: str,
    body: DocumentClassification,
) -> ApiResponse[DocumentDetail]:
    """文書の分類と有効期間を保存する。検索の分類フィルタと基準日の絞り込みに使う。"""
    try:
        detail = await OracleClient().save_document_classification(document_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。") from exc
    return ApiResponse(data=detail)


@router.put("/{document_id}/superseded-by", response_model=ApiResponse[DocumentDetail])
async def save_document_superseded_by(
    document_id: str,
    body: DocumentSupersededByRequest,
) -> ApiResponse[DocumentDetail]:
    """文書を置き換えた新しい版を設定・解除する(#1248)。

    新しい版を設定した文書(旧版)の chunk は、既定では回答の検索の対象から外す
    (filters の ``include_superseded=true`` で含める)。null で解除する。
    自分自身・循環・見えない新しい版は 422。
    """
    try:
        detail = await OracleClient().set_document_superseded_by(
            document_id, body.superseded_by_document_id
        )
    except DocumentSupersessionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。") from exc
    return ApiResponse(data=detail)


async def _apply_recipe_review_text_edits(
    document_id: str,
    recipe_id: str,
    edits: DocumentReviewEditsRequest | DocumentApproveRequest,
) -> None:
    """選択レシピの extraction artifact だけを構造保持で修正する。

    修正は要素・表セルの差分として、レシピの行ロックの下で読んだ最新の抽出成果へ当てる
    (#2616)。別の要素・表セルへの並行の修正は両方残し、読み込んだ後に同じ要素・表セルが
    ほかの保存で変わっていたら 409 にする(``base_text`` で照合する)。初回の保存は共有の
    抽出成果を書き換えず、レシピ固有の ID へ copy-on-write する。
    """
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。")
    if not detail.content_sha256:
        raise HTTPException(status_code=409, detail="文書のソースハッシュが未確定です。")
    content_sha256 = detail.content_sha256

    def build(
        recipe: Mapping[str, object], artifact: Mapping[str, object]
    ) -> ReviewedExtractionWrite:
        extraction = _reviewed_extraction_with_edits(
            StructuredExtraction.model_validate(artifact["extraction_json"]),
            edits,
        )
        raw_recipe_subset = artifact.get("recipe_subset")
        recipe_subset = (
            {str(key): value for key, value in raw_recipe_subset.items()}
            if isinstance(raw_recipe_subset, Mapping)
            else None
        )
        config = DocumentProcessingConfig.model_validate(recipe.get("processing_config") or {})
        recipe_settings, _ = _merge_document_processing_config(config)
        if recipe_subset and isinstance(recipe_subset.get("rag_preprocess_profile"), str):
            recipe_settings = recipe_settings.model_copy(
                update={"rag_preprocess_profile": recipe_subset["rag_preprocess_profile"]}
            )
        base_extraction_recipe_id = compute_extraction_recipe_id(content_sha256, recipe_settings)
        return ReviewedExtractionWrite(
            extraction_recipe_id=compute_document_recipe_extraction_id(
                base_extraction_recipe_id,
                recipe_id,
                int(str(recipe.get("config_revision") or 1)),
            ),
            source_sha256=content_sha256,
            extraction=extraction.to_document_payload(),
            recipe_subset=recipe_subset,
            status=str(artifact.get("status") or "materialized"),
        )

    try:
        await oracle.save_document_recipe_review_extraction(
            document_id=document_id,
            recipe_id=recipe_id,
            build=build,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="レシピが見つかりません。") from exc
    except DocumentReviewEditConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _reviewed_extraction_with_edits(
    extraction: StructuredExtraction,
    edits: DocumentReviewEditsRequest | DocumentApproveRequest,
) -> StructuredExtraction:
    """構造・bbox を維持し、許可されたテキストだけを差し替える。

    読み込んだときの文(``base_text``)と今の文が違い、修正後の文とも違う要素・表セルは、
    ほかの保存が先に変えていたため ``DocumentReviewEditConflictError`` にする(#2616)。
    """
    text_by_element_id = {edit.element_id: edit.text for edit in edits.element_edits}
    current_text_by_element_id = {
        element.element_id: element.text for element in extraction.elements if element.element_id
    }
    unknown_ids = sorted(text_by_element_id.keys() - current_text_by_element_id.keys())
    if unknown_ids:
        raise HTTPException(
            status_code=400,
            detail="存在しない要素 ID が含まれています。",
        )
    if any(
        _review_edit_conflicts(
            edit.base_text, edit.text, current_text_by_element_id[edit.element_id]
        )
        for edit in edits.element_edits
    ):
        raise DocumentReviewEditConflictError(_REVIEW_EDIT_CONFLICT_MESSAGE)
    if text_by_element_id:
        updated_elements = [
            (
                element.model_copy(
                    update={
                        "text": text_by_element_id[element.element_id],
                        "metadata": {**element.metadata, "reviewed_text": True},
                    }
                )
                if element.element_id in text_by_element_id
                else element
            )
            for element in extraction.elements
        ]
        extraction = extraction.model_copy(update={"elements": updated_elements})
    if edits.table_cell_edits:
        extraction = _apply_table_cell_edits(extraction, edits.table_cell_edits)
    normalized = _canonicalize_reviewed_extraction(extraction)
    # 旧クライアントの approve(raw_text) は受理を継続する。新しい保存 API は構造編集のみ。
    if isinstance(edits, DocumentApproveRequest) and edits.raw_text is not None:
        normalized = StructuredExtraction.model_validate(
            normalized.model_copy(update={"raw_text": edits.raw_text}).model_dump()
        )
    return normalized


def _canonicalize_reviewed_extraction(
    extraction: StructuredExtraction,
) -> StructuredExtraction:
    """構造化要素を正本として表・章節・offset・raw_text を再同期する。"""
    table_by_key: dict[str, ExtractionTable] = {}
    for extraction_table in extraction.tables:
        table_by_key[extraction_table.table_id] = extraction_table
        if extraction_table.element_id:
            table_by_key[extraction_table.element_id] = extraction_table
    table_elements = [element for element in extraction.elements if element.kind == "table"]
    fallback_table = (
        extraction.tables[0] if len(extraction.tables) == len(table_elements) == 1 else None
    )

    matched_table_ids: set[str] = set()
    source_elements: list[DocumentElement] = []
    for element in extraction.elements:
        if element.kind != "table":
            source_elements.append(element)
            continue
        table_key = _review_table_key(element)
        table = table_by_key.get(table_key) if table_key else fallback_table
        if table is None:
            source_elements.append(element)
            continue
        matched_table_ids.add(table.table_id)
        source_elements.append(element.model_copy(update={"text": _review_table_text(table)}))

    for extraction_table in extraction.tables:
        if extraction_table.table_id in matched_table_ids:
            continue
        row_count, column_count = _review_table_shape(extraction_table)
        source_elements.append(
            DocumentElement(
                kind="table",
                text=_review_table_text(extraction_table),
                order=len(source_elements),
                element_id=extraction_table.element_id or extraction_table.table_id,
                content_kind="table",
                page_number=extraction_table.page_number,
                bbox=_review_table_bbox(extraction_table),
                metadata={
                    "table_id": extraction_table.table_id,
                    "row_count": row_count,
                    "column_count": column_count,
                },
            )
        )

    path_by_level: dict[int, str] = {}
    current_path: list[str] = []
    raw_parts: list[str] = []
    cursor = 0
    elements: list[DocumentElement] = []
    for element in sorted(source_elements, key=lambda item: item.order):
        metadata = dict(element.metadata)
        text = element.text.strip()
        if element.kind == "title":
            level, title = _review_heading(element, text)
            path_by_level = {
                existing_level: existing_title
                for existing_level, existing_title in path_by_level.items()
                if existing_level < level
            }
            path_by_level[level] = title
            current_path = [path_by_level[key] for key in sorted(path_by_level)]
            metadata["section_level"] = level
        elif not current_path and element.section_path:
            current_path = list(element.section_path)

        metadata.pop("raw_start", None)
        metadata.pop("raw_end", None)
        if element.kind in SEARCHABLE_ELEMENT_KINDS and text:
            if raw_parts:
                cursor += 1
            metadata["raw_start"] = cursor
            cursor += len(text)
            metadata["raw_end"] = cursor
            raw_parts.append(text)

        elements.append(
            element.model_copy(
                update={
                    "text": text,
                    "section_path": list(current_path),
                    "metadata": metadata,
                }
            )
        )

    element_page = {
        element.element_id: element.page_number
        for element in elements
        if element.element_id and element.page_number is not None
    }
    pages = [
        page.model_copy(
            update={
                "element_ids": [
                    *page.element_ids,
                    *[
                        element_id
                        for element_id, page_number in element_page.items()
                        if page_number == page.page_number and element_id not in page.element_ids
                    ],
                ]
            }
        )
        for page in extraction.pages
    ]
    normalized = StructuredExtraction.model_validate(
        extraction.model_copy(
            update={
                "elements": elements,
                "pages": pages,
                "raw_text": "\n".join(raw_parts),
                "navigation": [],
            }
        ).model_dump()
    )
    return normalized.model_copy(
        update={
            "navigation": build_navigation_tree(normalized),
            "parser_artifacts": _reviewed_layout_artifacts(normalized),
        }
    )


def _reviewed_layout_artifacts(extraction: StructuredExtraction) -> dict[str, Any]:
    """確認した本文・表・見出しを親子分割の入力へ同期し、元の解析情報を残す。"""
    artifacts = dict(extraction.parser_artifacts)
    layout = artifacts.get("layout_records")
    if not isinstance(layout, Mapping):
        return artifacts
    source_records = layout.get("records")
    if not isinstance(source_records, list):
        return artifacts
    elements = {
        element.element_id: element for element in extraction.elements if element.element_id
    }
    tables = {
        key: table
        for table in extraction.tables
        for key in (table.table_id, table.element_id)
        if key
    }
    records: list[Any] = []
    for source_record in source_records:
        if not isinstance(source_record, Mapping):
            records.append(source_record)
            continue
        element = elements.get(str(source_record.get("id") or ""))
        if element is None:
            records.append(source_record)
            continue
        table = tables.get(_review_table_key(element) or "") if element.kind == "table" else None
        reviewed = (
            table.metadata.get("reviewed_text") if table else element.metadata.get("reviewed_text")
        )
        if not reviewed:
            records.append(source_record)
            continue
        record = dict(source_record)
        raw_value = record.get("raw")
        raw = dict(raw_value) if isinstance(raw_value, Mapping) else {}
        text = element.text
        if table is not None:
            text = _structured_table_html(table, "")
            if previous_html := raw.get("table_enhanced_html"):
                raw.setdefault("review_original_table_enhanced_html", previous_html)
            raw["table_enhanced_html"] = text
        if text != str(record.get("text") or ""):
            raw.setdefault("review_original_text", record.get("text") or "")
        record["text"] = text
        if element.kind == "title":
            if "heading_level" in raw:
                raw.setdefault("review_original_heading_level", raw["heading_level"])
            raw["heading_level"] = element.metadata["section_level"]
        record["raw"] = raw
        records.append(record)
    artifacts["layout_records"] = {**layout, "records": records}
    return artifacts


def _review_table_key(element: DocumentElement) -> str | None:
    value = element.metadata.get("table_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return element.element_id


def _review_table_text(table: ExtractionTable) -> str:
    if not table.cells:
        return table.caption or ""
    row_count, column_count = _review_table_shape(table)
    rows = [["" for _ in range(column_count)] for _ in range(row_count)]
    for cell in table.cells:
        rows[cell.row][cell.col] = cell.text
    markdown = "\n".join(
        "| " + " | ".join(value.replace("|", "\\|").strip() for value in row) + " |"
        for row in rows
        if any(value.strip() for value in row)
    )
    return "\n".join(part for part in (table.caption, markdown) if part).strip()


def _review_table_shape(table: ExtractionTable) -> tuple[int, int]:
    if not table.cells:
        return 0, 0
    return (
        max(cell.row + cell.row_span for cell in table.cells),
        max(cell.col + cell.col_span for cell in table.cells),
    )


def _review_table_bbox(table: ExtractionTable) -> list[float] | None:
    boxes = [cell.bbox for cell in table.cells if cell.bbox and len(cell.bbox) >= 4]
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _review_heading(element: DocumentElement, text: str) -> tuple[int, str]:
    level_value = element.metadata.get("section_level")
    level = (
        int(level_value)
        if isinstance(level_value, int) and not isinstance(level_value, bool) and level_value > 0
        else max(1, len(element.section_path))
    )
    title = text
    if match := MARKDOWN_HEADING.match(text):
        level = len(match.group("marks"))
        title = match.group("title")
    elif match := numbered_heading().match(text):
        title = match.group("title")
    return min(6, level), re.sub(r"\s+", " ", title).strip().strip("#")[:80]


_REVIEW_EDIT_CONFLICT_MESSAGE = (
    "読み込んだ後に、ほかの操作で同じ箇所が保存されました。"
    "変更を破棄して読み込み直してから直してください。"
)


def _review_edit_conflicts(base_text: str, text: str, current_text: str) -> bool:
    """読み込んだ後にほかの保存が同じ箇所を別の文へ変えていたか(前後の空白は比べない)。"""
    current = current_text.strip()
    return base_text.strip() != current and text.strip() != current


def _apply_table_cell_edits(
    extraction: StructuredExtraction,
    cell_edits: list[DocumentTableCellTextEdit],
) -> StructuredExtraction:
    """表セルのテキストのみを差し替える(row/col/span・bbox・構造は保持)。"""
    text_by_cell_key = {(edit.table_id, edit.row, edit.col): edit.text for edit in cell_edits}
    current_text_by_cell_key = {
        (table.table_id, cell.row, cell.col): cell.text
        for table in extraction.tables
        for cell in table.cells
    }
    unknown_cells = sorted(
        f"{table_id}:{row},{col}"
        for (table_id, row, col) in text_by_cell_key.keys() - current_text_by_cell_key.keys()
    )
    if unknown_cells:
        raise HTTPException(
            status_code=400,
            detail="存在しない表セルが含まれています。",
        )
    if any(
        _review_edit_conflicts(
            edit.base_text,
            edit.text,
            current_text_by_cell_key[(edit.table_id, edit.row, edit.col)],
        )
        for edit in cell_edits
    ):
        raise DocumentReviewEditConflictError(_REVIEW_EDIT_CONFLICT_MESSAGE)
    updated_tables = [
        table.model_copy(
            update={
                "metadata": (
                    {**table.metadata, "reviewed_text": True}
                    if any(key[0] == table.table_id for key in text_by_cell_key)
                    else table.metadata
                ),
                "cells": [
                    (
                        cell.model_copy(
                            update={"text": text_by_cell_key[(table.table_id, cell.row, cell.col)]}
                        )
                        if (table.table_id, cell.row, cell.col) in text_by_cell_key
                        else cell
                    )
                    for cell in table.cells
                ],
            }
        )
        for table in extraction.tables
    ]
    return extraction.model_copy(update={"tables": updated_tables})


async def _load_source_bytes(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
    *,
    mark_document_error: bool = True,
) -> tuple[bytes, SourceProfile]:
    """保存済み原本を取得し、整合性検証して source_profile を組む(失敗は HTTPException)。

    取込(extract)経路と、案 A の承認後 非 owning parser 再抽出で共有する。
    ``mark_document_error=False``(レシピの job)では、失敗しても文書の status を変えない。
    失敗は呼び出し側の job の失敗の処理がレシピの行だけに記録する(#504)。
    """
    if detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")

    async def mark_error(message: str) -> None:
        if mark_document_error:
            await oracle.update_document_status(document_id, FileStatus.ERROR, message)

    try:
        data = await ObjectStorageClient().get(detail.object_storage_path)
    except FileNotFoundError as exc:
        await mark_error("原本ファイルが見つかりません。")
        raise HTTPException(status_code=409, detail="原本ファイルが見つかりません。") from exc
    except ValueError as exc:
        await mark_error(str(exc))
        raise HTTPException(status_code=400, detail="原本ファイルの参照パスが不正です。") from exc

    if integrity_error := _source_integrity_error(data, detail):
        await mark_error(integrity_error)
        raise HTTPException(status_code=409, detail=integrity_error)

    source_profile = build_source_profile(
        original_file_name=(
            detail.source_profile.original_file_name
            if detail.source_profile is not None
            else detail.file_name
        ),
        sanitized_file_name=detail.file_name,
        content_type=detail.content_type,
        file_size_bytes=detail.file_size_bytes,
        content_sha256=detail.content_sha256,
        duplicate_of_document_id=detail.duplicate_of_document_id,
        data=data,
    )
    return data, source_profile


async def _load_prepared_source_bytes(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
) -> tuple[bytes, SourceProfile]:
    """保存済みファイル準備 artifact を取得する。欠落時は原本へ戻さない。"""
    artifact = detail.preprocess_artifact
    if artifact is None or not artifact.object_storage_path:
        raise HTTPException(
            status_code=409,
            detail="処理後ファイルが見つかりません。ファイル準備から再処理してください。",
        )
    try:
        data = await ObjectStorageClient().get(artifact.object_storage_path)
    except FileNotFoundError as exc:
        await oracle.update_document_status(
            document_id,
            FileStatus.ERROR,
            "処理後ファイルが見つかりません。ファイル準備から再処理してください。",
        )
        raise HTTPException(
            status_code=409,
            detail="処理後ファイルが見つかりません。ファイル準備から再処理してください。",
        ) from exc
    except ValueError as exc:
        await oracle.update_document_status(document_id, FileStatus.ERROR, str(exc))
        raise HTTPException(status_code=400, detail="処理後ファイルの参照パスが不正です。") from exc

    if artifact.sha256 and _sha256_hex(data) != artifact.sha256:
        message = "処理後ファイルの SHA-256 がファイル準備時と一致しません。"
        await oracle.update_document_status(document_id, FileStatus.ERROR, message)
        raise HTTPException(status_code=409, detail=message)

    source_profile = _source_profile_for_detail(detail)
    return data, source_profile


async def _ingest_existing_document(
    document_id: str,
    *,
    force: bool = False,
    use_prepared_artifact: bool = False,
    cancel_checker: Callable[[], Awaitable[bool]] | None = None,
) -> DocumentDetail:
    """保存済み原本を検証して取込パイプラインへ渡す。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None or detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if detail.status in (
        FileStatus.PREPROCESSING,
        FileStatus.INGESTING,
        FileStatus.CHUNKING,
        FileStatus.INDEXING,
    ):
        raise HTTPException(status_code=409, detail="このドキュメントは現在取込中です。")
    if detail.status == FileStatus.INDEXED and not force:
        return detail
    if use_prepared_artifact:
        data, source_profile = await _load_prepared_source_bytes(oracle, document_id, detail)
        ingest_content_type = (
            detail.preprocess_artifact.content_type
            if detail.preprocess_artifact is not None and detail.preprocess_artifact.content_type
            else "application/octet-stream"
        )
        prepared_artifact = detail.preprocess_artifact
    else:
        data, source_profile = await _load_source_bytes(oracle, document_id, detail)
        ingest_content_type = detail.content_type or "application/octet-stream"
        prepared_artifact = None
    effective_settings, processing_config = await _resolve_ingestion_settings(oracle, document_id)
    plan, _configs = await _materialization_plan_for_document(
        oracle,
        detail,
        global_settings=effective_settings,
    )
    ingest_prompt = "ドキュメントを日本語で OCR し、本文テキストを抽出してください。"
    if plan is None or not plan.chunk_sets:
        chunk_set_id = _document_chunk_set_id(detail, effective_settings)
        pipeline = IngestionPipeline(oracle=oracle, settings=effective_settings)
        result = await pipeline.ingest(
            document_id=document_id,
            image_bytes=data,
            prompt=ingest_prompt,
            content_type=ingest_content_type,
            source_profile=source_profile,
            chunk_set_id=chunk_set_id,
            original_object_storage_path=detail.object_storage_path,
            prepared_artifact=prepared_artifact,
            cancel_checker=cancel_checker,
        )
        await _reconcile_document_chunk_sets(
            oracle,
            document_id,
            result,
            chunk_set_id,
            effective_settings,
            processing_config,
        )
        return result
    # plan 実体化: 抽出グループ(parser×preprocess)ごとに extract 1 回 → 各 chunking で index。
    result = detail
    recipe_groups = plan.chunk_sets_by_extraction_recipe()
    total_chunk_sets = sum(len(chunk_set_ids) for chunk_set_ids in recipe_groups.values())
    processed_chunk_sets = 0
    for _recipe_id, chunk_set_ids in recipe_groups.items():
        for index, chunk_set_id in enumerate(chunk_set_ids):
            pipeline = IngestionPipeline(oracle=oracle, settings=effective_settings)
            processed_chunk_sets += 1
            # 成功 metric/audit は最後の chunk_set でのみ出し、1 文書 1 論理取込に集約する。
            record_outcome = processed_chunk_sets == total_chunk_sets
            if index == 0:
                result = await pipeline.ingest(
                    document_id=document_id,
                    image_bytes=data,
                    prompt=ingest_prompt,
                    content_type=ingest_content_type,
                    source_profile=source_profile,
                    chunk_set_id=chunk_set_id,
                    record_outcome=record_outcome,
                    original_object_storage_path=detail.object_storage_path,
                    prepared_artifact=prepared_artifact,
                    cancel_checker=cancel_checker,
                )
                if result.status == FileStatus.REVIEW:
                    # REVIEW ゲート ON: 抽出は REVIEW で停止。残りは承認後に CHUNK→INDEX する。
                    return result
            else:
                # 同抽出(同 parser/前処理)の chunking 変種: 抽出を再利用して re-chunk。
                result = await pipeline.index_reviewed(
                    document_id,
                    chunk_set_id=chunk_set_id,
                    record_outcome=record_outcome,
                    cancel_checker=cancel_checker,
                )
    await _reconcile_plan_chunk_sets(
        oracle, document_id, result, plan, effective_settings, processing_config
    )
    return result


async def _chunk_reviewed_document(
    document_id: str,
    *,
    cancel_checker: Callable[[], Awaitable[bool]] | None = None,
) -> DocumentDetail:
    """REVIEW で承認済みの文書を CHUNK だけ実行する。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if detail.status not in (FileStatus.REVIEW, FileStatus.CHUNKING):
        raise HTTPException(
            status_code=409,
            detail="プレビュー確認待ちの文書のみ Chunk 作成できます。",
        )
    effective_settings, processing_config = await _resolve_ingestion_settings(oracle, document_id)
    plan, _configs = await _materialization_plan_for_document(
        oracle,
        detail,
        global_settings=effective_settings,
    )
    if plan is None or not plan.chunk_sets:
        chunk_set_id = _document_chunk_set_id(detail, effective_settings)
        pipeline = IngestionPipeline(oracle=oracle, settings=effective_settings)
        result = await pipeline.chunk_reviewed(
            document_id, chunk_set_id=chunk_set_id, cancel_checker=cancel_checker
        )
        await _reconcile_document_chunk_sets_chunked(
            oracle,
            document_id,
            result,
            chunk_set_id,
            effective_settings,
            processing_config,
        )
        return result
    # 3 層モデル: plan は常に単一 extraction recipe。保存済み extraction から chunk 化する。
    result = detail
    chunk_set_ids = sorted(plan.chunk_sets)
    for index, chunk_set_id in enumerate(chunk_set_ids):
        pipeline = IngestionPipeline(oracle=oracle, settings=effective_settings)
        # 成功 metric/audit は最後の chunk_set でのみ出し、1 文書 1 論理取込に集約する。
        result = await pipeline.chunk_reviewed(
            document_id,
            chunk_set_id=chunk_set_id,
            record_outcome=index == len(chunk_set_ids) - 1,
            cancel_checker=cancel_checker,
        )
    await _reconcile_plan_chunk_sets_chunked(
        oracle, document_id, result, plan, effective_settings, processing_config
    )
    return result


async def _index_reviewed_document(
    document_id: str,
    *,
    cancel_checker: Callable[[], Awaitable[bool]] | None = None,
) -> DocumentDetail:
    """CHUNKED の文書を後段(embedding/index)だけ実行する。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if detail.status not in (FileStatus.CHUNKED, FileStatus.INDEXING):
        raise HTTPException(
            status_code=409,
            detail="Chunk 確認済みの文書のみ索引できます。",
        )
    effective_settings, processing_config = await _resolve_ingestion_settings(oracle, document_id)
    plan, _configs = await _materialization_plan_for_document(
        oracle,
        detail,
        global_settings=effective_settings,
    )
    if plan is None or not plan.chunk_sets:
        chunk_set_id = _document_chunk_set_id(detail, effective_settings)
        if chunk_set_id is None:
            raise HTTPException(status_code=409, detail="索引対象の chunk_set がありません。")
        pipeline = IngestionPipeline(oracle=oracle, settings=effective_settings)
        result = await pipeline.index_chunked(
            document_id, chunk_set_id=chunk_set_id, cancel_checker=cancel_checker
        )
        await _reconcile_document_chunk_sets(
            oracle,
            document_id,
            result,
            chunk_set_id,
            effective_settings,
            processing_config,
        )
        return result
    result = detail
    chunk_set_ids = sorted(plan.chunk_sets)
    for index, chunk_set_id in enumerate(chunk_set_ids):
        # 3 層モデル: レシピは文書単位(global)。
        pipeline = IngestionPipeline(oracle=oracle, settings=effective_settings)
        result = await pipeline.index_chunked(
            document_id,
            chunk_set_id=chunk_set_id,
            record_outcome=index == len(chunk_set_ids) - 1,
            cancel_checker=cancel_checker,
        )
    await _reconcile_plan_chunk_sets(
        oracle, document_id, result, plan, effective_settings, processing_config
    )
    return result


async def _reconcile_plan_chunk_sets(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
    plan: MaterializationPlan,
    effective_settings: Settings,
    processing_config: DocumentProcessingConfig,
) -> None:
    """plan の各 chunk_set を永続化し、文書の serving を確定、plan に無い chunk_set を GC する。

    chunk は save_index で挿入時タグ付け済み。serving 設定 / extraction artifact まで揃って
    初めて検索可能な INDEXED とみなすため、失敗時は ERROR に戻す。
    """
    if detail.status != FileStatus.INDEXED:
        return
    _, effective_config = _merge_document_processing_config(processing_config)
    recipe_snapshot = _processing_recipe_snapshot(processing_config, effective_config)
    try:
        for chunk_set_id in plan.chunk_sets:
            chunk_count = await oracle.count_chunk_set_chunks(chunk_set_id)
            extraction_recipe_id = plan.extraction_recipe_for_chunk_set(chunk_set_id)
            await oracle.upsert_chunk_set(
                chunk_set_id=chunk_set_id,
                document_id=document_id,
                extraction_recipe_id=extraction_recipe_id,
                recipe_subset=recipe_snapshot,
            )
            await oracle.mark_chunk_set_indexed(
                chunk_set_id=chunk_set_id, chunk_count=chunk_count, vector_count=chunk_count
            )
            if extraction_recipe_id is not None:
                await _record_document_extraction_artifact(
                    oracle,
                    detail,
                    extraction_recipe_id=extraction_recipe_id,
                    settings=effective_settings,
                )
        # 3 層モデル: 文書の serving chunk_set を設定(単一レシピなので plan の chunk_set)。
        serving_chunk_sets = sorted(plan.chunk_sets)
        if serving_chunk_sets:
            await oracle.set_document_serving_chunk_set(
                document_id=document_id, chunk_set_id=serving_chunk_sets[0]
            )
        await _reconcile_plan_artifact_layers(oracle, document_id, detail, plan, effective_settings)
        await oracle.delete_document_chunk_sets_except(
            document_id=document_id, keep_chunk_set_ids=list(plan.chunk_sets)
        )
        await oracle.delete_document_extractions_except(
            document_id=document_id, keep_extraction_ids=list(plan.extraction_recipes)
        )
        if plan.truncated_extractions:
            logger.warning(
                "抽出数が上限を超え %d 件を打ち切りました(document_id=%s)。",
                len(plan.truncated_extractions),
                document_id,
            )
    except Exception as exc:
        logger.warning(
            "chunk_set plan reconcile に失敗しました。document_id=%s",
            document_id,
            exc_info=True,
        )
        await oracle.update_document_status(
            document_id,
            FileStatus.ERROR,
            CHUNK_SET_PUBLISH_ERROR_MESSAGE,
        )
        raise IngestionUserError(CHUNK_SET_PUBLISH_ERROR_MESSAGE) from exc


async def _reconcile_plan_chunk_sets_chunked(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
    plan: MaterializationPlan,
    effective_settings: Settings,
    processing_config: DocumentProcessingConfig,
) -> None:
    """plan の各 chunk_set を CHUNKED として永続化する。KB binding は INDEX 後に作る。"""
    if detail.status != FileStatus.CHUNKED:
        return
    _, effective_config = _merge_document_processing_config(processing_config)
    recipe_snapshot = _processing_recipe_snapshot(processing_config, effective_config)
    try:
        for chunk_set_id in plan.chunk_sets:
            chunk_count = await oracle.count_chunk_set_chunks(chunk_set_id)
            extraction_recipe_id = plan.extraction_recipe_for_chunk_set(chunk_set_id)
            await oracle.upsert_chunk_set(
                chunk_set_id=chunk_set_id,
                document_id=document_id,
                extraction_recipe_id=extraction_recipe_id,
                recipe_subset=recipe_snapshot,
                status="CHUNKED",
            )
            await oracle.mark_chunk_set_chunked(chunk_set_id=chunk_set_id, chunk_count=chunk_count)
            if extraction_recipe_id is not None:
                await _record_document_extraction_artifact(
                    oracle,
                    detail,
                    extraction_recipe_id=extraction_recipe_id,
                    settings=effective_settings,
                )
        await oracle.delete_document_chunk_sets_except(
            document_id=document_id, keep_chunk_set_ids=list(plan.chunk_sets)
        )
        await oracle.delete_document_extractions_except(
            document_id=document_id, keep_extraction_ids=list(plan.extraction_recipes)
        )
    except Exception as exc:
        logger.warning(
            "chunk_set chunk plan reconcile に失敗しました。document_id=%s",
            document_id,
            exc_info=True,
        )
        await oracle.update_document_status(
            document_id,
            FileStatus.ERROR,
            CHUNK_SET_PUBLISH_ERROR_MESSAGE,
        )
        raise IngestionUserError(CHUNK_SET_PUBLISH_ERROR_MESSAGE) from exc


async def _reconcile_plan_artifact_layers(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
    plan: MaterializationPlan,
    effective_settings: Settings,
) -> None:
    """plan に含まれる派生 layer の状態を永続化する。"""
    configs = dict(await oracle.list_document_knowledge_base_configs(document_id))
    effective_by_kb = _effective_ingestion_settings_by_kb(effective_settings, configs)
    fields_configured = await _document_has_field_definitions(oracle, document_id)
    for chunk_set_id in plan.chunk_sets:
        for layer, user_label in _ARTIFACT_LAYER_LABELS:
            requested_ids = _requested_layer_ids_for_chunk_set(
                chunk_set_id,
                plan,
                effective_by_kb,
                layer=layer,
            )
            for layer_id in requested_ids:
                status, reason = _materialized_layer_state(
                    layer=layer,
                    user_label=user_label,
                    extraction=detail.extraction,
                    settings=effective_settings,
                    field_definitions_configured=fields_configured,
                )
                await oracle.upsert_artifact_layer(
                    layer_id=layer_id,
                    layer_kind=layer,
                    parent_chunk_set_id=chunk_set_id,
                    document_id=document_id,
                    requested=True,
                    status=status.value,
                    reason=reason,
                    metrics=_layer_metrics(layer, detail.extraction),
                    input_fingerprint=recorded_layer_fingerprint(
                        layer, detail.extraction, effective_settings
                    ),
                )


async def _document_has_field_definitions(oracle: OracleClient, document_id: str) -> bool:
    """文書に効く項目抽出の定義(所属 KB の定義か全体の既定。#548)が 1 件以上あるか。"""
    return bool(
        current_field_definitions(await oracle.list_document_extraction_field_sets(document_id))
    )


def _materialized_layer_state(
    *,
    layer: str,
    user_label: str,
    extraction: Mapping[str, object] | None,
    settings: Settings,
    field_definitions_configured: bool,
) -> tuple[DocumentLayerStatusName, str]:
    if not extraction:
        return (
            DocumentLayerStatusName.NEEDS_REINGEST,
            (
                f"{user_label}の作成に必要な抽出 artifact がありません。"
                "現在の構築設定で再取込してください。"
            ),
        )
    if layer == "metadata":
        return _metadata_layer_state(user_label, extraction, settings, field_definitions_configured)
    if layer == "navigation":
        node_count = _navigation_node_count(extraction)
        if node_count > 0:
            return (
                DocumentLayerStatusName.MATERIALIZED,
                (
                    f"{user_label}は保存済み抽出 artifact から "
                    f"{node_count} 件の章節として実体化済みです。"
                ),
            )
        return (
            DocumentLayerStatusName.PLANNED_ONLY,
            f"{user_label}は要求されていますが、章節構造を抽出できていません。",
        )
    return (
        DocumentLayerStatusName.PLANNED_ONLY,
        f"{user_label}は構築計画に含まれていますが、まだ実体化していません。",
    )


def _metadata_layer_state(
    user_label: str,
    extraction: Mapping[str, object],
    settings: Settings,
    field_definitions_configured: bool,
) -> tuple[DocumentLayerStatusName, str]:
    """項目抽出の成果物があれば実体化とする(図の要約は Vision の解析結果に含まれる。#497)。

    `field_definitions_configured` は、文書に効く項目の定義(所属 KB の定義か全体の既定)が
    あるか(#548)。
    """
    field_enabled = bool(getattr(settings, "rag_field_extraction_enabled", False))
    reasons: list[str] = []
    if field_enabled and not _fields_materialized(extraction):
        # 取込が刻んだ実行結果(#2481)。抽出の失敗は取込を止めるため、ここには来ない。
        outcome = field_extraction_outcome(extraction)
        if outcome is not None and outcome[0] == "completed":
            return (
                DocumentLayerStatusName.MATERIALIZED,
                (
                    f"{user_label}は実行済みですが、項目の定義に当てはまる値は"
                    "見つかりませんでした(0 件)。"
                ),
            )
        if not field_definitions_configured:
            reasons.append(
                # 未保存の環境は標準の項目を使うため(#556)、ここに来るのは定義を 0 件で
                # 保存したとき(全体の既定か、文書が属する KB の定義)だけ。
                "項目抽出は有効ですが、この文書に使う項目の定義が 0 件のため実行されません。"
                "ナレッジベースの「抽出する項目」か、文書解析の設定の項目の定義"
                "(「標準の項目に戻す」も使えます)で項目を登録してから再取込してください"
            )
        elif outcome is not None and outcome[0] == "no_definitions":
            reasons.append(
                "取込の時点では、この文書に使う項目の定義が 0 件だったため項目抽出を"
                "実行していません。再取込すると今の項目の定義で抽出します"
            )
        elif outcome is not None and outcome[0] == "no_text":
            reasons.append("抽出した本文が無いため、項目抽出を実行していません")
        else:
            reasons.append("項目抽出の成果物がまだありません")
    if reasons:
        return (DocumentLayerStatusName.PLANNED_ONLY, "。".join(reasons) + "。")
    if field_enabled:
        return (
            DocumentLayerStatusName.MATERIALIZED,
            f"{user_label}は保存済み抽出 artifact から実体化済みです。",
        )
    # 無効なのに layer が要求された場合は旧来の payload 有無で判定する。
    if _fields_materialized(extraction):
        return (
            DocumentLayerStatusName.MATERIALIZED,
            f"{user_label}は保存済み抽出 artifact から実体化済みです。",
        )
    return (
        DocumentLayerStatusName.PLANNED_ONLY,
        f"{user_label}は構築計画に含まれていますが、まだ実体化していません。",
    )


def _fields_materialized(extraction: Mapping[str, object]) -> bool:
    return bool(extraction.get("fields"))


def _layer_metrics(layer: str, extraction: Mapping[str, object] | None) -> dict[str, object]:
    if not extraction:
        return {}
    if layer == "navigation":
        return {"navigation_node_count": _navigation_node_count(extraction)}
    if layer != "metadata":
        return {}
    return {
        "field_count": _metadata_item_count(extraction.get("fields")),
        "asset_count": _metadata_item_count(extraction.get("assets")),
    }


def _navigation_node_count(extraction: Mapping[str, object]) -> int:
    """保存済み extraction から決定論的 navigation node 数を数える。"""
    nodes = _navigation_nodes_from_extraction(extraction)
    return len(nodes)


def _navigation_nodes_from_extraction(
    extraction: Mapping[str, object],
) -> list[DocumentNavigationNode]:
    if not extraction:
        return []
    try:
        structured = StructuredExtraction.model_validate(dict(extraction))
    except Exception:
        return []
    return list(structured.navigation or build_navigation_tree(structured))


def _metadata_item_count(value: object) -> int:
    return len(value) if isinstance(value, list | tuple) else 0


def _document_chunk_set_id(detail: DocumentDetail, settings: Settings) -> str | None:
    """文書の content_sha256 と effective 取込設定から chunk_set_id を求める(無ければ None)。"""
    if not detail.content_sha256:
        return None
    return compute_chunk_set_id(detail.content_sha256, settings)


def _document_extraction_recipe_id(detail: DocumentDetail, settings: Settings) -> str | None:
    """文書の content_sha256 と effective 解析設定から extraction_recipe_id を求める。"""
    if not detail.content_sha256:
        return None
    return compute_extraction_recipe_id(detail.content_sha256, settings)


def _extraction_recipe_subset(settings: Settings) -> dict[str, object]:
    """extraction recipe の人が読める snapshot。正規 ID は variant_keys 側の hash を正とする。"""
    return extraction_recipe_subset(settings)


async def _record_document_extraction_artifact(
    oracle: OracleClient,
    detail: DocumentDetail,
    *,
    extraction_recipe_id: str | None,
    settings: Settings,
    status: DocumentLayerStatusName = DocumentLayerStatusName.MATERIALIZED,
    reason: str | None = None,
) -> None:
    """extraction recipe 単位の抽出 artifact 状態を保存する。"""
    if extraction_recipe_id is None:
        return
    await oracle.upsert_document_extraction_artifact(
        document_id=detail.id,
        extraction_recipe_id=extraction_recipe_id,
        source_sha256=detail.content_sha256,
        recipe_subset=_extraction_recipe_subset(settings),
        status=status.value,
        reason=reason,
        metrics=_extraction_metrics(detail.extraction),
    )


def _extraction_metrics(extraction: Mapping[str, object] | None) -> dict[str, object]:
    if not extraction:
        return {}
    return {
        "element_count": _metadata_item_count(extraction.get("elements")),
        "table_count": _metadata_item_count(extraction.get("tables")),
        "asset_count": _metadata_item_count(extraction.get("assets")),
        "field_count": _metadata_item_count(extraction.get("fields")),
    }


async def _reconcile_document_chunk_sets(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
    chunk_set_id: str | None,
    effective_settings: Settings,
    processing_config: DocumentProcessingConfig,
) -> None:
    """取込後、materialize した chunk_set を記録し文書の serving を確定する(planner 駆動の基盤)。

    chunk は save_index で**挿入時に chunk_set_id タグ付け済み**。本関数は chunk_set 行の永続化・
    serving 設定・旧 chunk_set(とその chunk、未タグ chunk)の GC を行う。serving 確定まで揃って
    初めて検索可能な INDEXED とみなすため、失敗時は ERROR に戻す。
    """
    if detail.status != FileStatus.INDEXED or chunk_set_id is None:
        return
    try:
        chunk_count = await oracle.count_document_chunks(document_id)
        extraction_recipe_id = _document_extraction_recipe_id(detail, effective_settings)
        _, effective_config = _merge_document_processing_config(processing_config)
        await oracle.upsert_chunk_set(
            chunk_set_id=chunk_set_id,
            document_id=document_id,
            extraction_recipe_id=extraction_recipe_id,
            recipe_subset=_processing_recipe_snapshot(processing_config, effective_config),
        )
        await oracle.mark_chunk_set_indexed(
            chunk_set_id=chunk_set_id, chunk_count=chunk_count, vector_count=chunk_count
        )
        await _record_document_extraction_artifact(
            oracle,
            detail,
            extraction_recipe_id=extraction_recipe_id,
            settings=effective_settings,
        )
        # 取込設定変更で生じた旧 chunk_set とその chunk(+未タグ chunk)を削除し、keep だけ残す。
        await oracle.delete_stale_document_chunk_sets(
            document_id=document_id, keep_chunk_set_id=chunk_set_id
        )
        # 3 層モデル: この単一 chunk_set を文書の serving にする(retrieval はこれを検索対象)。
        # 所属 KB は membership(rag_document_knowledge_bases)が正本で、別表 binding は持たない。
        await oracle.set_document_serving_chunk_set(
            document_id=document_id, chunk_set_id=chunk_set_id
        )
    except Exception as exc:
        logger.warning(
            "chunk_set reconcile に失敗しました。document_id=%s",
            document_id,
            exc_info=True,
        )
        await oracle.update_document_status(
            document_id,
            FileStatus.ERROR,
            CHUNK_SET_PUBLISH_ERROR_MESSAGE,
        )
        raise IngestionUserError(CHUNK_SET_PUBLISH_ERROR_MESSAGE) from exc


async def _reconcile_document_chunk_sets_chunked(
    oracle: OracleClient,
    document_id: str,
    detail: DocumentDetail,
    chunk_set_id: str | None,
    effective_settings: Settings,
    processing_config: DocumentProcessingConfig,
) -> None:
    """CHUNK 後、chunk_set 行だけを記録する。KB binding は INDEX 完了まで作らない。"""
    if detail.status != FileStatus.CHUNKED or chunk_set_id is None:
        return
    try:
        chunk_count = await oracle.count_chunk_set_chunks(chunk_set_id)
        extraction_recipe_id = _document_extraction_recipe_id(detail, effective_settings)
        _, effective_config = _merge_document_processing_config(processing_config)
        await oracle.upsert_chunk_set(
            chunk_set_id=chunk_set_id,
            document_id=document_id,
            extraction_recipe_id=extraction_recipe_id,
            recipe_subset=_processing_recipe_snapshot(processing_config, effective_config),
            status="CHUNKED",
        )
        await oracle.mark_chunk_set_chunked(chunk_set_id=chunk_set_id, chunk_count=chunk_count)
        await _record_document_extraction_artifact(
            oracle,
            detail,
            extraction_recipe_id=extraction_recipe_id,
            settings=effective_settings,
        )
        await oracle.delete_stale_document_chunk_sets(
            document_id=document_id, keep_chunk_set_id=chunk_set_id
        )
    except Exception as exc:
        logger.warning(
            "chunk_set chunk reconcile に失敗しました。document_id=%s",
            document_id,
            exc_info=True,
        )
        await oracle.update_document_status(
            document_id,
            FileStatus.ERROR,
            CHUNK_SET_PUBLISH_ERROR_MESSAGE,
        )
        raise IngestionUserError(CHUNK_SET_PUBLISH_ERROR_MESSAGE) from exc


async def _resolve_ingestion_settings(
    oracle: OracleClient,
    document_id: str,
    recipe_id: str | None = None,
) -> tuple[Settings, DocumentProcessingConfig]:
    """レシピ > global 既定で有効な処理設定を解決する。KB は参照しない。

    レシピは ``recipe_id``、無ければ文書の既定レシピ。処理設定はレシピに保存するので、文書行の
    設定はレシピを読めないときだけ使う(#697)。
    """
    config: DocumentProcessingConfig | None = None
    ensure_recipe = getattr(oracle, "ensure_default_document_recipe", None)
    get_recipe = getattr(oracle, "get_document_recipe", None)
    if recipe_id is None and callable(ensure_recipe):
        recipe_id = str((await ensure_recipe(document_id))["recipe_id"])
    if recipe_id is not None and callable(get_recipe):
        row = await get_recipe(document_id, recipe_id)
        if row is not None:
            config = DocumentProcessingConfig.model_validate(row.get("processing_config") or {})
    if config is None:
        config = await oracle.get_document_processing_config(document_id)
    effective, _resolved = _merge_document_processing_config(config)
    return effective, config


def _dispatch_ingestion_job(
    job_id: str,
    *,
    force: bool = False,
) -> None:
    """QUEUED ジョブの消費をワーカーへ通知する。HTTP 内では実行しない。"""
    _ = (job_id, force)
    request_ingestion_worker_wakeup()


async def _raise_if_job_cancelled(
    cancel_checker: Callable[[], Awaitable[bool]] | None,
) -> None:
    """文書・レシピの status や出力を書き換える前に、job が取り消されていないかを確かめる。"""
    if cancel_checker is not None and await cancel_checker():
        raise IngestionCancelledError(INGESTION_JOB_CANCELLED_MESSAGE)


def _restore_status_for_cancelled_phase(phase: IngestionJobPhase) -> FileStatus:
    if phase == IngestionJobPhase.INDEX:
        return FileStatus.CHUNKED
    if phase == IngestionJobPhase.CHUNK:
        return FileStatus.REVIEW
    return FileStatus.UPLOADED


def _restore_recipe_status_for_cancelled_phase(phase: IngestionJobPhase) -> FileStatus:
    """レシピの job を取り消した後の、レシピ行の status(工程を始める前の状態)。"""
    if phase == IngestionJobPhase.EXTRACT:
        # EXTRACT はファイル準備の成果物(処理後ファイル)がある状態から始まる。
        return FileStatus.PREPROCESSED
    return _restore_status_for_cancelled_phase(phase)


async def _enqueue_ingestion_job_for_document(
    document_id: str,
    *,
    force: bool,
    phase: IngestionJobPhase = IngestionJobPhase.PREPROCESS,
) -> IngestionJob:
    """既存ドキュメントを job 化し、必要ならバックグラウンド実行へ渡す。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None or detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if detail.status in (
        FileStatus.PREPROCESSING,
        FileStatus.INGESTING,
        FileStatus.CHUNKING,
        FileStatus.INDEXING,
    ):
        raise HTTPException(status_code=409, detail="このドキュメントは現在取込中です。")
    # 投入直後の job は worker が拾うまで QUEUED のまま文書は UPLOADED 等に見えるため、
    # 状態だけでは二重投入を止められない。出力の初期化より前に、同じ(既定)レシピの
    # 待機中・実行中 job を確かめる(#281)。
    await _raise_if_default_recipe_job_active(oracle, document_id)

    source_profile = _source_profile_for_detail(detail)
    # 重複スキップは初回取込(PREPROCESS)の入口だけに適用する。PREPROCESSED 以降の段階進行
    # (EXTRACT/承認して解析へ 等)は、既に重複を承知で取込を確定した文書の続きなので skip しない
    # (さもないと重複文書は承認しても解析中へ進めず PREPROCESSED で詰まる)。
    if (
        detail.duplicate_of_document_id is not None
        and not force
        and phase == IngestionJobPhase.PREPROCESS
    ):
        return await _create_ingestion_job_record(
            oracle=oracle,
            document_id=document_id,
            parser_profile=source_profile.parser_profile,
            quality_warnings=source_profile.quality_warnings,
            status=IngestionJobStatus.SKIPPED,
            skip_reason="duplicate_content",
            phase=phase,
        )
    recipe_settings = await _default_recipe_settings(oracle, document_id)
    # 旧形式・マクロ付きの Office は、既定レシピのファイル準備が変換できる(Office→PDF など)なら
    # 原本の形式で飛ばさない(#2470)。
    if (
        source_profile.unsupported_reason
        and not force
        and not settings_convert_source(recipe_settings, source_profile)
    ):
        return await _create_ingestion_job_record(
            oracle=oracle,
            document_id=document_id,
            parser_profile=source_profile.parser_profile,
            quality_warnings=source_profile.quality_warnings,
            status=IngestionJobStatus.SKIPPED,
            skip_reason=source_profile.unsupported_reason,
            phase=phase,
        )
    if phase == IngestionJobPhase.CHUNK:
        return await _enqueue_chunk_phase_job_for_document(document_id, force=force)
    if phase == IngestionJobPhase.INDEX:
        return await _enqueue_index_phase_job_for_document(document_id, force=force)
    # 既定レシピの解析エンジン(既定は Docling)で扱えない形式は、出力を初期化する前に止める(#286)。
    await _raise_if_parser_source_blocked(recipe_settings, source_profile, phase)
    if detail.status == FileStatus.INDEXED and not force:
        return await _create_ingestion_job_record(
            oracle=oracle,
            document_id=document_id,
            parser_profile=source_profile.parser_profile,
            quality_warnings=source_profile.quality_warnings,
            status=IngestionJobStatus.SKIPPED,
            skip_reason="already_indexed",
            phase=phase,
        )
    if phase == IngestionJobPhase.EXTRACT and (
        detail.preprocess_artifact is None or not detail.preprocess_artifact.object_storage_path
    ):
        raise HTTPException(
            status_code=409,
            detail="処理後ファイルが見つかりません。ファイル準備から再処理してください。",
        )
    if phase == IngestionJobPhase.PREPROCESS:
        await _reset_document_outputs_for_extract(
            oracle,
            document_id,
            clear_preprocess_artifact=True,
        )
    else:
        await _reset_document_outputs_for_extract(oracle, document_id)

    job = await _create_ingestion_job_record(
        oracle=oracle,
        document_id=document_id,
        parser_profile=source_profile.parser_profile,
        quality_warnings=source_profile.quality_warnings,
        phase=phase,
    )
    _dispatch_ingestion_job(job.id, force=force)
    return job


async def _raise_if_default_recipe_job_active(oracle: OracleClient, document_id: str) -> None:
    """文書単位の投入先(既定レシピ)に待機中・実行中の job があれば 409 にする。

    Oracle の job 作成も同じレシピの QUEUED / RUNNING を拒否するが、それより前に行う出力の初期化
    (`_reset_document_outputs_for_extract`)で待機中 job の前提を壊さないよう、ここで先に止める。
    """
    default_recipe_id: str | None = None
    ensure_recipe = getattr(oracle, "ensure_default_document_recipe", None)
    if callable(ensure_recipe):
        recipe = await ensure_recipe(document_id)
        default_recipe_id = str(recipe["recipe_id"])
    jobs = await oracle.list_document_ingestion_jobs(document_id)
    if any(
        job.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING}
        and job.recipe_id in {None, default_recipe_id}
        for job in jobs
    ):
        raise HTTPException(
            status_code=409,
            detail="このドキュメントは取込待ちまたは取込中です。完了してから再実行してください。",
        )


async def _enqueue_index_phase_job_for_document(
    document_id: str,
    *,
    force: bool = False,
) -> IngestionJob:
    """CHUNKED 文書に対し INDEX フェーズ job を投入する。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if detail.status == FileStatus.ERROR and force:
        pass
    elif detail.status not in (FileStatus.CHUNKED, FileStatus.INDEXED):
        raise HTTPException(
            status_code=409,
            detail="Chunk 確認済みの文書のみ索引できます。",
        )
    if detail.status == FileStatus.INDEXED and not force:
        source_profile = _source_profile_for_detail(detail)
        return await _create_ingestion_job_record(
            oracle=oracle,
            document_id=document_id,
            parser_profile=source_profile.parser_profile,
            quality_warnings=source_profile.quality_warnings,
            status=IngestionJobStatus.SKIPPED,
            skip_reason="already_indexed",
            phase=IngestionJobPhase.INDEX,
        )
    if force:
        await oracle.reset_document_index_outputs(document_id, status=FileStatus.CHUNKED)
    source_profile = _source_profile_for_detail(detail)
    job = await _create_ingestion_job_record(
        oracle=oracle,
        document_id=document_id,
        parser_profile=source_profile.parser_profile,
        quality_warnings=source_profile.quality_warnings,
        phase=IngestionJobPhase.INDEX,
    )
    _dispatch_ingestion_job(job.id)
    return job


async def _enqueue_chunk_phase_job_for_document(
    document_id: str,
    *,
    force: bool = False,
) -> IngestionJob:
    """REVIEW 文書に対し CHUNK フェーズ job を投入する。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if detail.status == FileStatus.ERROR and force:
        pass
    elif detail.status not in (FileStatus.REVIEW, FileStatus.CHUNKED, FileStatus.INDEXED):
        raise HTTPException(
            status_code=409,
            detail="抽出確認済みの文書のみ Chunk 作成できます。",
        )
    if force or detail.status != FileStatus.REVIEW:
        await oracle.reset_document_chunk_outputs(document_id, status=FileStatus.REVIEW)
    source_profile = _source_profile_for_detail(detail)
    job = await _create_ingestion_job_record(
        oracle=oracle,
        document_id=document_id,
        parser_profile=source_profile.parser_profile,
        quality_warnings=source_profile.quality_warnings,
        phase=IngestionJobPhase.CHUNK,
    )
    _dispatch_ingestion_job(job.id)
    return job


async def _enqueue_failed_segment_retry_job_for_document(
    document_id: str,
    *,
    recipe_id: str | None = None,
) -> IngestionJob:
    """FAILED segment checkpoint のみを対象にした再試行 job を投入する。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None or detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if recipe_id is not None:
        recipe = await oracle.get_document_recipe(document_id, recipe_id)
        if recipe is None:
            raise HTTPException(status_code=404, detail="レシピが見つかりません。")
        raw_artifact = recipe.get("preprocess_artifact")
        artifact = DocumentPreprocessArtifact.model_validate(raw_artifact) if raw_artifact else None
        if artifact is None or not artifact.object_storage_path:
            raise HTTPException(
                status_code=409,
                detail="処理後ファイルが見つかりません。ファイル準備から再処理してください。",
            )
        segments = await oracle.list_ingestion_segments(document_id)
        if not any(
            segment.recipe_id == recipe_id and segment.status == "FAILED" for segment in segments
        ):
            raise HTTPException(
                status_code=409,
                detail="再試行対象の失敗 segment がありません。",
            )
        try:
            return await _enqueue_ingestion_job_for_recipe(
                document_id,
                recipe_id,
                phase=IngestionJobPhase.EXTRACT,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if detail.status in (
        FileStatus.PREPROCESSING,
        FileStatus.INGESTING,
        FileStatus.CHUNKING,
        FileStatus.INDEXING,
    ):
        raise HTTPException(status_code=409, detail="このドキュメントは現在取込中です。")
    segments = await oracle.list_ingestion_segments(document_id)
    if not any(segment.status == "FAILED" for segment in segments):
        raise HTTPException(
            status_code=409,
            detail="再試行対象の失敗 segment がありません。",
        )
    return await _enqueue_ingestion_job_for_document(
        document_id,
        force=True,
    )


async def _list_delete_blocking_ingestion_jobs(
    oracle: OracleClient,
    document_id: str,
) -> list[IngestionJob]:
    """削除を止めるべき実行中の取込 job を返す。"""
    jobs: list[IngestionJob] = []
    for status in DELETE_BLOCKING_INGESTION_STATUSES:
        jobs.extend(await oracle.list_document_ingestion_jobs(document_id, status=status))
    return jobs


async def _create_ingestion_job_record(
    *,
    oracle: OracleClient,
    document_id: str,
    parser_profile: str,
    quality_warnings: list[str],
    status: IngestionJobStatus = IngestionJobStatus.QUEUED,
    phase: IngestionJobPhase = IngestionJobPhase.PREPROCESS,
    skip_reason: str | None = None,
    settings_overrides: dict[str, object] | None = None,
    recipe_id: str | None = None,
    recipe_revision: int | None = None,
    expected_language_pack_release_id: str | None = None,
) -> IngestionJob:
    """取込 job を永続化する。``settings_overrides`` 付きはレシピ実験(Phase 3b)ジョブ。"""
    if recipe_id is None:
        ensure_recipe = getattr(oracle, "ensure_default_document_recipe", None)
        if callable(ensure_recipe):
            recipe = await ensure_recipe(document_id)
            recipe_id = str(recipe["recipe_id"])
            recipe_revision = int(str(recipe.get("config_revision") or 1))
    release = active_release()
    if release is None:
        release = await (await initialized_release_store(oracle)).current()
        if expected_language_pack_release_id and expected_language_pack_release_id != release.id:
            raise HTTPException(
                status_code=409, detail="言語資源の現在版が変わりました。差分を再確認してください。"
            )
    elif expected_language_pack_release_id and expected_language_pack_release_id != release.id:
        raise HTTPException(status_code=409, detail="処理対象の言語資源の版が一致しません。")
    queued_at = datetime.now(UTC)
    settings = get_settings()
    job = IngestionJob(
        id=uuid4().hex,
        document_id=document_id,
        language_pack_release_id=release.id,
        status=status,
        phase=phase,
        parser_profile=parser_profile,
        quality_warnings=quality_warnings,
        settings_overrides=settings_overrides,
        recipe_id=recipe_id,
        recipe_revision=recipe_revision,
        skip_reason=skip_reason,
        max_attempts=settings.ingestion_job_max_attempts,
        queued_at=queued_at,
        finished_at=queued_at if status == IngestionJobStatus.SKIPPED else None,
    )
    try:
        if expected_language_pack_release_id:
            return await oracle.create_ingestion_job(
                job, expected_current_release_id=expected_language_pack_release_id
            )
        return await oracle.create_ingestion_job(job)
    except ValueError as exc:
        # 同じレシピの待機中・実行中 job がある / レシピ設定が更新された、は競合(409)として返す。
        # 以前は文書単位の投入経路で 500 になっていた(#281)。
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _reset_document_outputs_for_extract(
    oracle: OracleClient,
    document_id: str,
    *,
    clear_preprocess_artifact: bool = False,
) -> None:
    """EXTRACT 再投入前に旧抽出・checkpoint・派生結果を初期化する。"""
    reset_outputs = getattr(oracle, "reset_document_ingestion_outputs", None)
    if callable(reset_outputs):
        await reset_outputs(
            document_id,
            status=FileStatus.UPLOADED,
            clear_preprocess_artifact=clear_preprocess_artifact,
        )
        return
    await oracle.update_document_status(document_id, FileStatus.UPLOADED)


def _source_profile_for_detail(detail: DocumentDetail) -> SourceProfile:
    """保存済み DocumentDetail から parser profile を復元する。"""
    if detail.source_profile is not None:
        return detail.source_profile
    return build_source_profile(
        original_file_name=detail.file_name,
        sanitized_file_name=detail.file_name,
        content_type=detail.content_type,
        file_size_bytes=detail.file_size_bytes,
        content_sha256=detail.content_sha256,
        duplicate_of_document_id=detail.duplicate_of_document_id,
        data=None,
    )


def _document_ingestion_segments(
    detail: DocumentDetail,
    jobs: list[IngestionJob],
    effective_settings: Settings | None = None,
) -> list[IngestionSegment]:
    """保存済み extraction/job から segment view を推定する。"""
    source_profile = _source_profile_for_detail(detail)
    page_start, page_end = _document_page_range(detail.extraction)
    latest_job = max(jobs, key=lambda job: job.queued_at, default=None)
    attempt_count = latest_job.attempt_count if latest_job is not None else 0
    status = _segment_status_from_detail(detail, latest_job)
    error_message = detail.error_message or (latest_job.error_message if latest_job else None)
    parser_backend = _parser_backend_from_extraction(detail.extraction, source_profile)
    parser_profile = _parser_profile_from_extraction(detail.extraction, source_profile)
    progress_unit = "page" if page_start is not None and page_end is not None else "source"
    planned_parser = _planned_parser_backend_for_unmaterialized_extract(
        detail,
        latest_job,
        effective_settings,
    )
    if planned_parser is not None:
        parser_backend = planned_parser
        parser_profile = planned_parser
    return [
        IngestionSegment(
            segment_id=f"{detail.id}:source",
            document_id=detail.id,
            status=status,
            parser_backend=parser_backend,
            parser_profile=parser_profile,
            page_start=page_start,
            page_end=page_end,
            progress_unit=progress_unit,
            progress_start=page_start,
            progress_end=page_end,
            attempt_count=attempt_count,
            artifact_path=(
                _extraction_artifact_path(detail.extraction) or detail.object_storage_path
            ),
            error_code="ingestion_error" if error_message else None,
            error_message=error_message,
        )
    ]


def _planned_parser_backend_for_unmaterialized_extract(
    detail: DocumentDetail,
    latest_job: IngestionJob | None,
    effective_settings: Settings | None,
) -> str | None:
    """未実体化の抽出では upload 時判定ではなく現在の明示 parser を表示に使う。"""
    if effective_settings is None or _extraction_has_parser_context(detail.extraction):
        return None
    planned = _selected_parser_backend(effective_settings)
    if planned is None:
        return None
    if (
        latest_job is not None
        and latest_job.phase in {IngestionJobPhase.PREPROCESS, IngestionJobPhase.EXTRACT}
        and latest_job.status
        in {
            IngestionJobStatus.QUEUED,
            IngestionJobStatus.RUNNING,
            IngestionJobStatus.FAILED,
        }
    ):
        return planned
    if detail.status in {
        FileStatus.UPLOADED,
        FileStatus.PREPROCESSING,
        FileStatus.INGESTING,
        FileStatus.ERROR,
    }:
        return planned
    return None


def _selected_parser_backend(settings: Settings) -> str | None:
    """Settings の明示 parser backend を表示用に返す。local は未選択として扱う。"""
    selected = str(getattr(settings, "rag_parser_adapter_backend", "local")).strip()
    if not selected or selected == "local":
        return None
    return selected[:80]


def _extraction_has_parser_context(extraction: Mapping[str, object]) -> bool:
    """保存済み extraction に実 parser 情報があるか判定する。"""
    for container_name in ("quality_report", "parser_artifacts"):
        container = extraction.get(container_name)
        if not isinstance(container, Mapping):
            continue
        for key in ("parser_backend", "parser_profile", "source_parser", "external_adapter"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _document_page_range(extraction: Mapping[str, object]) -> tuple[int | None, int | None]:
    """extraction payload からページ範囲を推定する。"""
    pages: set[int] = set()
    raw_pages = extraction.get("pages")
    if isinstance(raw_pages, list):
        for page in raw_pages:
            if isinstance(page, Mapping):
                page_number = page.get("page_number")
                if isinstance(page_number, int) and page_number >= 1:
                    pages.add(page_number)
    raw_elements = extraction.get("elements")
    if isinstance(raw_elements, list):
        for element in raw_elements:
            if isinstance(element, Mapping):
                page_number = element.get("page_number")
                if isinstance(page_number, int) and page_number >= 1:
                    pages.add(page_number)
    raw_tables = extraction.get("tables")
    if isinstance(raw_tables, list):
        pages.update(_page_numbers_from_mappings(raw_tables))
    raw_assets = extraction.get("assets")
    if isinstance(raw_assets, list):
        pages.update(_page_numbers_from_mappings(raw_assets))
    if not pages:
        return None, None
    return min(pages), max(pages)


def _page_numbers_from_mappings(items: list[object]) -> set[int]:
    """tables/assets の first-class metadata からページ番号を集める。"""
    pages: set[int] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        page_number = item.get("page_number")
        if isinstance(page_number, int) and not isinstance(page_number, bool) and page_number >= 1:
            pages.add(page_number)
    return pages


def _parser_backend_from_extraction(
    extraction: Mapping[str, object],
    source_profile: SourceProfile,
) -> str:
    """extraction quality/parser artifacts から parser backend を読む。"""
    for container_name in ("quality_report", "parser_artifacts"):
        container = extraction.get(container_name)
        if isinstance(container, Mapping):
            value = container.get("parser_backend")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return source_profile.parser_backend


def _parser_profile_from_extraction(
    extraction: Mapping[str, object],
    source_profile: SourceProfile,
) -> str:
    """extraction quality/parser artifacts から parser profile を読む。"""
    for container_name in ("quality_report", "parser_artifacts"):
        container = extraction.get(container_name)
        if isinstance(container, Mapping):
            value = container.get("parser_profile")
            if isinstance(value, str) and value.strip():
                return value.strip()
            source_parser = container.get("source_parser")
            if isinstance(source_parser, str) and source_parser.strip():
                return source_parser.strip()
            external_adapter = container.get("external_adapter")
            if isinstance(external_adapter, str) and external_adapter.strip():
                return external_adapter.strip()
    return source_profile.parser_profile


def _extraction_artifact_path(extraction: Mapping[str, object]) -> str | None:
    """extraction payload から artifact cache path を読む。"""
    artifacts = extraction.get("parser_artifacts")
    if isinstance(artifacts, Mapping):
        value = artifacts.get("extraction_artifact_path")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _document_extraction_export_content(
    export_format: DocumentExtractionExportFormat,
    extraction: StructuredExtraction,
    payload: Mapping[str, object],
) -> str:
    """export format に応じた文字列表現を返す。"""
    if export_format in {
        DocumentExtractionExportFormat.JSON,
        DocumentExtractionExportFormat.CHUNKS,
    }:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if export_format == DocumentExtractionExportFormat.HTML:
        return _extraction_html(extraction)
    return _extraction_markdown(extraction)


def _document_extraction_export_content_type(
    export_format: DocumentExtractionExportFormat,
) -> str:
    """export payload の media type を返す。"""
    if export_format == DocumentExtractionExportFormat.MARKDOWN:
        return "text/markdown; charset=utf-8"
    if export_format == DocumentExtractionExportFormat.HTML:
        return "text/html; charset=utf-8"
    return "application/json; charset=utf-8"


_EXTRACTION_EXPORT_FILE_SUFFIXES: dict[DocumentExtractionExportFormat, str] = {
    DocumentExtractionExportFormat.MARKDOWN: ".md",
    DocumentExtractionExportFormat.HTML: ".html",
    DocumentExtractionExportFormat.JSON: ".json",
    DocumentExtractionExportFormat.CHUNKS: "_chunks.json",
}


def _document_extraction_export_file_name(
    export_format: DocumentExtractionExportFormat,
    *,
    file_name: str | None,
    slot_no: object,
) -> str:
    """ダウンロードのファイル名(元の文書名の拡張子を除いた部分 + レシピ + 形式の拡張子)。

    例: `契約書.pdf` のレシピ1の Markdown は `契約書_レシピ1.md`、
    chunk は `契約書_レシピ1_chunks.json`。
    """
    safe_name = _safe_display_filename(file_name)
    stem = PurePath(safe_name).stem.strip(" .") or "document"
    slot = slot_no if isinstance(slot_no, int) and slot_no > 0 else 1
    return _truncate_file_name(
        f"{stem}_レシピ{slot}{_EXTRACTION_EXPORT_FILE_SUFFIXES[export_format]}"
    )


def _document_extraction_export_download_response(
    export_format: DocumentExtractionExportFormat,
    content: str,
    *,
    file_name: str | None,
    slot_no: object,
) -> Response:
    """抽出エクスポートをファイルとして返す(原本のダウンロードと同じヘッダーにそろえる)。"""
    download_name = _document_extraction_export_file_name(
        export_format, file_name=file_name, slot_no=slot_no
    )
    headers = {
        # 非 ASCII ファイル名は RFC 5987 でエンコードする
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
        # MIME sniffing による取り違えを防ぐ
        "X-Content-Type-Options": "nosniff",
        # 抽出のやり直しで内容が変わるため、ブラウザに残さない
        "Cache-Control": "private, no-store",
    }
    if export_format == DocumentExtractionExportFormat.HTML:
        # 同じ origin で開かれてもスクリプトを動かさない(#281 と同じ扱い)。
        headers["Content-Security-Policy"] = "sandbox"
    return Response(
        content=content.encode("utf-8"),
        media_type=_document_extraction_export_content_type(export_format),
        headers=headers,
    )


def _extraction_parser_backend(extraction: StructuredExtraction) -> str | None:
    """quality_report / parser_artifacts から parser backend を読む。"""
    if extraction.quality_report is not None and extraction.quality_report.parser_backend:
        return extraction.quality_report.parser_backend
    value = extraction.parser_artifacts.get("parser_backend")
    return value if isinstance(value, str) and value.strip() else None


def _extraction_parser_profile(extraction: StructuredExtraction) -> str | None:
    """quality_report / parser_artifacts から parser profile を読む。"""
    if extraction.quality_report is not None and extraction.quality_report.parser_profile:
        return extraction.quality_report.parser_profile
    value = extraction.parser_artifacts.get("parser_profile")
    return value if isinstance(value, str) and value.strip() else None


def _extraction_markdown(extraction: StructuredExtraction) -> str:
    """StructuredExtraction を human review しやすい Markdown へ変換する。"""
    lines: list[str] = []
    current_page: int | None = None
    if not extraction.elements and extraction.raw_text:
        lines.append(record_boundary_plain_text(extraction))
    for element in sorted(extraction.elements, key=lambda item: item.order):
        if element.page_number is not None and element.page_number != current_page:
            current_page = element.page_number
            if lines:
                lines.append("")
            lines.append(f"<!-- page: {current_page} -->")
        rendered = _element_markdown(element)
        if rendered:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(rendered)
    for asset in sorted(extraction.assets, key=_asset_sort_key):
        if asset.page_number is not None and asset.page_number != current_page:
            current_page = asset.page_number
            if lines:
                lines.append("")
            lines.append(f"<!-- page: {current_page} -->")
        rendered = _asset_markdown(asset)
        if rendered:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(rendered)
    return "\n".join(lines).strip() or record_boundary_plain_text(extraction)


def _element_markdown(element: DocumentElement) -> str:
    """1 element を Markdown block へ変換する。"""
    text = element.text.strip()
    if not text:
        return ""
    if element.kind == "title":
        if text.startswith("#"):
            return text
        level = _markdown_heading_level(element)
        return f"{'#' * level} {text}"
    if element.kind == "code":
        language = _metadata_str(element.metadata.get("code_language"))
        return f"```{language}\n{text}\n```"
    if element.kind == "equation":
        return f"$$\n{text}\n$$"
    if element.kind == "figure":
        return f"> 図: {text}"
    if element.kind == "figure_caption":
        return f"> 図注: {text}"
    if element.kind == "table_caption":
        return f"> 表注: {text}"
    return text


def _asset_markdown(asset: ExtractionAsset) -> str:
    """first-class asset を Markdown 監査行として返す。"""
    labels = [f"> Asset: {asset.kind} `{asset.asset_id}`"]
    if asset.page_number is not None:
        labels.append(f"> page: {asset.page_number}")
    if asset.bbox:
        labels.append(f"> bbox: {','.join(f'{value:g}' for value in asset.bbox)}")
    if asset.alt_text:
        labels.append(f"> alt: {asset.alt_text.strip()}")
    return "\n".join(labels)


def _extraction_html(extraction: StructuredExtraction) -> str:
    """StructuredExtraction を安全に escaped HTML へ変換する。"""
    title = escape(extraction.document_type or "ドキュメント")
    tables_by_element_id = _tables_by_element_id(extraction)
    if not extraction.elements and not extraction.assets:
        body = _html_text_block(record_boundary_plain_text(extraction))
        return f'<article data-document-type="{title}">\n{body}\n</article>'
    lines = [f'<article data-document-type="{title}">']
    current_page: int | None = None
    if not extraction.elements and extraction.raw_text:
        lines.append(_html_text_block(record_boundary_plain_text(extraction)))
    for element in sorted(extraction.elements, key=lambda item: item.order):
        if element.page_number is not None and element.page_number != current_page:
            current_page = element.page_number
            lines.append(
                f'  <p class="page-marker" data-page="{current_page}">page {current_page}</p>'
            )
        rendered = _element_html(element, tables_by_element_id=tables_by_element_id)
        if rendered:
            lines.append(rendered)
    for asset in sorted(extraction.assets, key=_asset_sort_key):
        if asset.page_number is not None and asset.page_number != current_page:
            current_page = asset.page_number
            lines.append(
                f'  <p class="page-marker" data-page="{current_page}">page {current_page}</p>'
            )
        lines.append(_asset_html(asset))
    lines.append("</article>")
    return "\n".join(lines)


def _asset_html(asset: ExtractionAsset) -> str:
    """first-class asset を実体埋め込みなしの安全な HTML へ変換する。"""
    attrs = _asset_html_attrs(asset)
    label = asset.alt_text.strip() if asset.alt_text else asset.kind
    return f'  <aside{attrs} class="asset-block">{_html_inline(label)}</aside>'


def _element_html(
    element: DocumentElement,
    *,
    tables_by_element_id: Mapping[str, ExtractionTable],
) -> str:
    """1 element を HTML block へ変換する。source text は必ず escape する。"""
    text = element.text.strip()
    attrs = _element_html_attrs(element)
    if element.kind == "table":
        table = tables_by_element_id.get(element.element_id or "")
        if table is not None and table.cells:
            return _structured_table_html(table, attrs)
    if not text:
        return ""
    if element.kind == "title":
        level = _markdown_heading_level(element)
        return f"  <h{level}{attrs}>{_html_inline(text)}</h{level}>"
    if element.kind == "code":
        language = _metadata_str(element.metadata.get("code_language"))
        class_attr = f' class="language-{escape(language, quote=True)}"' if language else ""
        return f"  <pre{attrs}><code{class_attr}>{escape(text)}</code></pre>"
    if element.kind == "equation":
        return f'  <div{attrs} class="equation">{escape(text)}</div>'
    if element.kind == "figure":
        return f"  <figure{attrs}><figcaption>{_html_inline(text)}</figcaption></figure>"
    if element.kind == "figure_caption":
        return f'  <p{attrs} class="figure-caption">{_html_inline(text)}</p>'
    if element.kind == "table_caption":
        return f'  <p{attrs} class="table-caption">{_html_inline(text)}</p>'
    if element.kind == "table":
        return f'  <pre{attrs} class="table-block">{escape(text)}</pre>'
    if element.kind == "list":
        return f'  <div{attrs} class="list-block">{_html_text_lines(text)}</div>'
    return f"  <p{attrs}>{_html_text_lines(text)}</p>"


def _asset_html_attrs(asset: ExtractionAsset) -> str:
    attrs: list[tuple[str, str]] = [
        ("data-asset-id", asset.asset_id),
        ("data-kind", asset.kind),
    ]
    if asset.page_number is not None:
        attrs.append(("data-page", str(asset.page_number)))
    if asset.bbox:
        attrs.append(("data-bbox", ",".join(f"{value:g}" for value in asset.bbox)))
    return "".join(f' {name}="{escape(value, quote=True)}"' for name, value in attrs)


def _asset_sort_key(asset: ExtractionAsset) -> tuple[int, str]:
    page_number = asset.page_number if asset.page_number is not None else 1_000_000
    return page_number, asset.asset_id


def _tables_by_element_id(extraction: StructuredExtraction) -> dict[str, ExtractionTable]:
    """element_id から first-class table metadata を参照できるようにする。"""
    tables: dict[str, ExtractionTable] = {}
    for table in extraction.tables:
        if table.element_id:
            tables.setdefault(table.element_id, table)
        tables.setdefault(table.table_id, table)
    return tables


def _structured_table_html(
    table: ExtractionTable,
    element_attrs: str,
) -> str:
    """ExtractionTable.cells を実 table として安全に HTML 化する。"""
    table_id_attr = escape(table.table_id, quote=True)
    table_attrs = f'{element_attrs} class="table-block" data-table-id="{table_id_attr}"'
    lines: list[str] = []
    if table.caption:
        lines.append(
            f'  <p class="table-caption" data-table-id="{table_id_attr}">'
            f"{_html_inline(table.caption)}</p>"
        )
    lines.append(f"  <table{table_attrs}>")
    lines.append("    <tbody>")
    for row_index, cells in _table_cells_by_row(table).items():
        lines.append(f'      <tr data-row="{row_index}">')
        for cell in cells:
            tag = "th" if row_index == 0 else "td"
            cell_attrs = _table_cell_html_attrs(table, cell)
            lines.append(f"        <{tag}{cell_attrs}>{_html_text_lines(cell.text)}</{tag}>")
        lines.append("      </tr>")
    lines.append("    </tbody>")
    lines.append("  </table>")
    return "\n".join(lines)


def _table_cells_by_row(table: ExtractionTable) -> dict[int, list[ExtractionTableCell]]:
    rows: dict[int, list[ExtractionTableCell]] = {}
    for cell in sorted(table.cells, key=lambda item: (item.row, item.col)):
        rows.setdefault(cell.row, []).append(cell)
    return rows


def _table_cell_html_attrs(table: ExtractionTable, cell: ExtractionTableCell) -> str:
    row = cell.row
    col = cell.col
    row_span = cell.row_span
    col_span = cell.col_span
    bbox = cell.bbox
    attrs = [
        ("data-table-id", table.table_id),
        ("data-row", str(row)),
        ("data-col", str(col)),
    ]
    if row_span != 1:
        attrs.append(("rowspan", str(row_span)))
    if col_span != 1:
        attrs.append(("colspan", str(col_span)))
    if isinstance(bbox, list) and bbox:
        attrs.append(("data-bbox", ",".join(f"{value:g}" for value in bbox)))
    if formula_ref := _table_cell_metadata_label(cell, "formula_cell_ref"):
        attrs.append(("data-formula-ref", formula_ref))
    if formula_format := _table_cell_metadata_label(cell, "equation_format"):
        attrs.append(("data-formula-format", formula_format))
    if formula := _table_cell_metadata_label(cell, "formula"):
        attrs.append(("data-formula", formula))
    if formula_value := _table_cell_metadata_label(cell, "formula_value"):
        attrs.append(("data-formula-value", formula_value))
    return "".join(f' {name}="{escape(value, quote=True)}"' for name, value in attrs)


def _table_cell_metadata_label(cell: ExtractionTableCell, key: str) -> str | None:
    value = cell.metadata.get(key)
    if isinstance(value, str | int | float):
        cleaned = str(value).strip()
        return cleaned[:1000] if cleaned else None
    return None


def _element_html_attrs(element: DocumentElement) -> str:
    """レビュー時に lineage を追える最小限の data 属性を作る。"""
    attrs: list[tuple[str, str]] = []
    if element.element_id:
        attrs.append(("data-element-id", element.element_id))
    if element.parent_id:
        attrs.append(("data-parent-id", element.parent_id))
    if element.content_kind:
        attrs.append(("data-content-kind", element.content_kind))
    elif element.kind:
        attrs.append(("data-content-kind", element.kind))
    if element.source_parser:
        attrs.append(("data-source-parser", element.source_parser))
    if element.page_number is not None:
        attrs.append(("data-page", str(element.page_number)))
    if element.bbox:
        attrs.append(("data-bbox", ",".join(f"{value:g}" for value in element.bbox)))
    if not attrs:
        return ""
    return "".join(f' {name}="{escape(value, quote=True)}"' for name, value in attrs)


def _html_text_block(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    return f"  <p>{_html_text_lines(cleaned)}</p>"


def _html_text_lines(text: str) -> str:
    return "<br>\n".join(escape(line) for line in text.splitlines())


def _html_inline(text: str) -> str:
    return escape(" ".join(line.strip() for line in text.splitlines() if line.strip()))


def _markdown_heading_level(element: DocumentElement) -> int:
    """element metadata / section_path から Markdown heading level を決める。"""
    metadata_level = _metadata_int(element.metadata.get("section_level"))
    if metadata_level is not None:
        return max(1, min(metadata_level, 6))
    if element.section_path:
        return max(1, min(len(element.section_path), 6))
    return 1


def _metadata_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _metadata_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


async def _document_artifact_paths(
    oracle: OracleClient,
    detail: DocumentDetail,
) -> list[str]:
    """削除対象 document に紐づく抽出 artifact cache path を重複排除して返す。"""
    paths: list[str] = []
    if extraction_artifact_path := _extraction_artifact_path(detail.extraction):
        paths.append(extraction_artifact_path)
    if detail.preprocess_artifact is not None and detail.preprocess_artifact.object_storage_path:
        paths.append(detail.preprocess_artifact.object_storage_path)
    try:
        segments = await oracle.list_ingestion_segments(detail.id)
    except Exception:
        segments = []
    for segment in segments:
        if segment.artifact_path:
            paths.append(segment.artifact_path)
    # レシピ行の `preprocess_artifact`（レシピごとのファイル準備の成果物。#303）。
    try:
        recipes = await oracle.list_document_recipes(detail.id)
    except Exception:
        recipes = []
    for recipe in recipes:
        artifact = recipe.get("preprocess_artifact")
        if isinstance(artifact, Mapping):
            recipe_artifact_path = artifact.get("object_storage_path")
            if isinstance(recipe_artifact_path, str):
                paths.append(recipe_artifact_path)
    original_path = detail.object_storage_path
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = path.strip()
        if not normalized or normalized == original_path or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _segment_status_from_detail(
    detail: DocumentDetail,
    latest_job: IngestionJob | None,
) -> str:
    """document/job status を segment status に寄せる。"""
    if latest_job is not None and latest_job.status in {
        IngestionJobStatus.QUEUED,
        IngestionJobStatus.RUNNING,
        IngestionJobStatus.CANCELLED,
    }:
        return latest_job.status.value
    if detail.status == FileStatus.ERROR:
        return "FAILED"
    if detail.status == FileStatus.INDEXED:
        return "SUCCEEDED"
    return detail.status.value


async def _run_ingestion_job(
    job_id: str,
    *,
    propagate_errors: bool = False,
    lease_owner: str | None = None,
) -> None:
    """キュー投入済み取込 job を実行する。

    job の完了・失敗は RUNNING のときだけ書く(`transition_ingestion_job`)。cancel を検知したら
    (pipeline の途中でも、完了・失敗を書く直前でも)、文書・レシピの status を取り消し後の
    状態へ戻し、次工程は投入しない(#305)。
    ``lease_owner`` は job を実行する取込 worker の識別子。claim で lease を取り、worker が
    heartbeat を打つ(#357)。lease を持つ実行は、完了・失敗・再キュー・取り消しの後始末を
    自分の lease のときだけ行う。heartbeat が途絶えて別の worker が claim し直した後の古い実行の
    結果は捨て、新しい実行の状態・文書・レシピを変えない。工程の途中の取り消しの確認でも lease を
    確かめ、lease を失った実行はそこで止める(#359)。
    """
    oracle = OracleClient()
    job = await oracle.claim_ingestion_job(
        job_id, started_at=datetime.now(UTC), lease_owner=lease_owner
    )
    if job is None:
        return

    try:
        store = await initialized_release_store(oracle)
        release_id = job.language_pack_release_id
        if release_id is None:
            current = await store.current()
            release_id = await oracle.pin_ingestion_job_language_pack(job.id, current.id)
            job = job.model_copy(update={"language_pack_release_id": release_id})
        release = await store.get(release_id)
        if release is None:
            raise IngestionUserError("固定した言語資源の公開版が見つかりません。")
        pack_from_release(release)
    except Exception:
        await _fail_ingestion_job(
            oracle, job, "固定した言語資源の公開版を読み込めませんでした。", lease_owner=lease_owner
        )
        if propagate_errors:
            raise
        return
    with ingestion_release_scope(release, job.id):
        await _run_pinned_ingestion_job(
            oracle, job, propagate_errors=propagate_errors, lease_owner=lease_owner
        )


async def _validate_legacy_language_input(oracle: OracleClient, job: IngestionJob) -> None:
    release = active_release()
    detail = await oracle.get_document(job.document_id)
    extraction = detail.extraction if detail else None
    artifacts = extraction.get("parser_artifacts", {}) if extraction else {}
    if release and not await artifact_language_compatible(oracle, artifacts, job.phase.value):
        raise IngestionUserError(
            "既存の抽出結果の言語資源を再利用できません。文書解析から再処理してください。"
        )


async def _run_pinned_ingestion_job(
    oracle: OracleClient,
    job: IngestionJob,
    *,
    propagate_errors: bool = False,
    lease_owner: str | None = None,
) -> None:
    job_id = job.id

    async def is_cancelled() -> bool:
        if lease_owner is not None:
            lease = await oracle.get_ingestion_job_lease(job_id)
            # lease を失った実行も取り消しと同じく止め、後始末で結果を捨てる(#359)。
            return lease is None or (
                not _holds_ingestion_lease(lease, lease_owner)
                or lease.status == IngestionJobStatus.CANCELLED
            )
        current = await oracle.get_ingestion_job(job_id)
        return current is not None and current.status == IngestionJobStatus.CANCELLED

    async def write_progress(progress: IngestionJobProgress | None) -> None:
        # 抽出の段（解析・Vision）の進捗を job の行へ書く（#1999）。lease を失った実行は書かない。
        await oracle.update_ingestion_job_progress(job_id, progress, lease_owner=lease_owner)

    # レシピ経路で「現在ジョブ完了後に自動投入すべき次フェーズ」を受け取る。投入は現在ジョブが
    # SUCCEEDED になった後(レシピ行ロックのガードを通過できる状態)に行う。
    next_recipe_phase: IngestionJobPhase | None = None
    # レシピ経路で、job を SUCCEEDED にできた後に記録する成功の監査・metric(#514)。
    recipe_success_outcome: IngestionSuccessOutcome | None = None
    # 文書単位の job の実行結果。自動進行(次工程の投入)は SUCCEEDED を書けた後に判定する。
    finished_detail: DocumentDetail | None = None
    try:
        async with track_ingestion_progress(write_progress):
            if job.recipe_id is not None:
                await _raise_if_job_cancelled(is_cancelled)
                await oracle.update_document_recipe_status(
                    recipe_id=job.recipe_id,
                    status=_PHASE_TO_RUNNING_STATUS[job.phase],
                )
            if job.recipe_id is not None or job.settings_overrides is not None:
                # 正式レシピは旧 active を維持した隔離 materialize。recipe_id の無い旧実験の job は
                # 利用者向けのエラーで止める（#486）。
                recipe_result = await _materialize_experiment_candidate(
                    oracle, job, cancel_checker=is_cancelled
                )
                next_recipe_phase = recipe_result.next_phase
                recipe_success_outcome = recipe_result.success_outcome
            elif job.phase == IngestionJobPhase.CHUNK:
                await _validate_legacy_language_input(oracle, job)
                finished_detail = await _chunk_reviewed_document(
                    job.document_id,
                    cancel_checker=is_cancelled,
                )
            elif job.phase == IngestionJobPhase.INDEX:
                await _validate_legacy_language_input(oracle, job)
                finished_detail = await _index_reviewed_document(
                    job.document_id,
                    cancel_checker=is_cancelled,
                )
            elif job.phase == IngestionJobPhase.EXTRACT:
                current_detail = await oracle.get_document(job.document_id)
                if (
                    current_detail is None
                    or current_detail.preprocess_artifact is None
                    or not current_detail.preprocess_artifact.object_storage_path
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="処理後ファイルが見つかりません。ファイル準備から再処理してください。",
                    )
                await _raise_if_job_cancelled(is_cancelled)
                await _reset_document_outputs_for_extract(oracle, job.document_id)
                finished_detail = await _ingest_existing_document(
                    job.document_id,
                    force=True,
                    use_prepared_artifact=True,
                    cancel_checker=is_cancelled,
                )
            else:
                await _raise_if_job_cancelled(is_cancelled)
                await _reset_document_outputs_for_extract(
                    oracle,
                    job.document_id,
                    clear_preprocess_artifact=True,
                )
                finished_detail = await _ingest_existing_document(
                    job.document_id,
                    force=True,
                    use_prepared_artifact=False,
                    cancel_checker=is_cancelled,
                )
    except HTTPException as exc:
        await _fail_ingestion_job(oracle, job, str(exc.detail), lease_owner=lease_owner)
        logger.info(
            "ingestion_job_user_error",
            extra={
                "job_id": job_id,
                "document_id": job.document_id,
                "status_code": exc.status_code,
            },
        )
        if propagate_errors:
            raise
    except IngestionCancelledError:
        await _restore_statuses_after_cancel(oracle, job, lease_owner=lease_owner)
        logger.info(
            "ingestion_job_cancelled",
            extra={"job_id": job_id, "document_id": job.document_id},
        )
        if propagate_errors:
            raise
    except IngestionTimeoutError as exc:
        await _fail_ingestion_job(oracle, job, str(exc), lease_owner=lease_owner)
        logger.info(
            "ingestion_job_timeout",
            extra={"job_id": job_id, "document_id": job.document_id},
        )
        if propagate_errors:
            raise
    except IngestionUserError as exc:
        await _fail_ingestion_job(oracle, job, str(exc), lease_owner=lease_owner)
        logger.info(
            "ingestion_job_validation_error",
            extra={"job_id": job_id, "document_id": job.document_id},
        )
        if propagate_errors:
            raise
    except Exception as exc:
        if (
            is_transient_oracle_error(exc)
            and job.attempt_count < job.max_attempts
            and await _requeue_ingestion_job_after_transient_error(
                oracle, job, exc, lease_owner=lease_owner
            )
        ):
            if propagate_errors:
                raise
            return
        safe_error = _safe_ingestion_job_error_message(exc)
        await _fail_ingestion_job(oracle, job, safe_error, lease_owner=lease_owner)
        logger.exception(
            "ingestion_job_failed",
            extra={
                "job_id": job_id,
                "document_id": job.document_id,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                **oracle_error_log_fields(exc),
            },
        )
        if propagate_errors:
            raise
    else:
        succeeded = await _finish_ingestion_job_unless_cancelled(
            oracle,
            job_id,
            status=IngestionJobStatus.SUCCEEDED,
            lease_owner=lease_owner,
        )
        if succeeded is None:
            # SUCCEEDED を書く直前に取り消された。取り消しを検知した側として status を戻し、
            # 次工程は投入しない。lease を失っていたら(別の worker が claim し直した)何も戻さない。
            await _restore_statuses_after_cancel(
                oracle,
                job,
                lease_owner=lease_owner,
                discarded_status=IngestionJobStatus.SUCCEEDED,
            )
            return
        # 取込（分割と embedding）が終わったら、章節と chunk の対応を作る（#1438。失敗しても job は
        # 成功のまま。MCP で章節を読むときに無ければ作り直す）。
        await refresh_document_section_map(job.document_id, oracle=oracle, reason="ingestion")
        if recipe_success_outcome is not None:
            _record_recipe_success_outcome(job, recipe_success_outcome)
        if finished_detail is not None:
            await _enqueue_auto_advance_job(job, finished_detail)
        if next_recipe_phase is not None and job.recipe_id is not None:
            # 現在ジョブは SUCCEEDED になったので、同一レシピの次フェーズ job を投入できる
            # (レシピ行ロックのガードを通過する)。抽出は既に成功しているため、投入失敗は
            # 握りつぶして warning に留め、人手の「承認して Chunk 作成」で続行可能にする。
            try:
                await _enqueue_ingestion_job_for_recipe(
                    job.document_id, job.recipe_id, phase=next_recipe_phase
                )
            except Exception:
                logger.warning(
                    "recipe_auto_advance_enqueue_failed",
                    extra={
                        "document_id": job.document_id,
                        "recipe_id": job.recipe_id,
                        "phase": next_recipe_phase.value,
                    },
                    exc_info=True,
                )


def _record_recipe_success_outcome(job: IngestionJob, outcome: IngestionSuccessOutcome) -> None:
    """SUCCEEDED にできたレシピの job の成功の監査・metric を 1 回だけ記録する(#514)。

    job は既に成功しているため、記録に失敗しても job の状態は戻さず、ログに残すだけにする。
    """
    try:
        outcome.record()
    except Exception:
        logger.warning(
            "recipe_ingestion_success_outcome_record_failed",
            extra={
                "job_id": job.id,
                "document_id": job.document_id,
                "recipe_id": job.recipe_id,
            },
            exc_info=True,
        )


async def _requeue_ingestion_job_after_transient_error(
    oracle: OracleClient,
    job: IngestionJob,
    error: Exception,
    *,
    lease_owner: str | None = None,
) -> bool:
    """一時的な DB エラー(接続断)の job を QUEUED に戻す。戻せたら True(#341)。

    試行回数(``attempt_count < max_attempts``)が残っている場合だけ呼ぶ。文書・レシピの status を
    工程を始める前の状態へ戻してから、job を RUNNING のときだけ QUEUED に戻す(条件付きの状態遷移)。
    先に job を戻すと、別の worker が claim して書いた status を後から上書きしうるため、
    この順にする。
    戻す途中でまた DB が失敗したら False を返し、呼び出し側は今までどおり FAILED にする
    (FAILED も書けなければ、stale の回復が拾う)。
    lease を持つ実行は、lease を失っていたら(別の worker が claim し直した)文書・レシピを戻さず、
    job も戻さずに True を返す(結果を捨てる。#359)。
    """
    try:
        if await _discard_if_ingestion_lease_lost(
            oracle, job, lease_owner=lease_owner, discarded_status=IngestionJobStatus.QUEUED
        ):
            return True
        await _restore_statuses_before_job_phase(oracle, job)
        requeued = await oracle.transition_ingestion_job(
            job.id,
            from_statuses=(IngestionJobStatus.RUNNING,),
            to_status=IngestionJobStatus.QUEUED,
            error_message=None,
            finished_at=None,
            lease_owner=lease_owner,
        )
    except Exception as requeue_error:
        logger.warning(
            "ingestion_job_requeue_failed",
            extra={
                "job_id": job.id,
                "document_id": job.document_id,
                **oracle_error_log_fields(requeue_error),
            },
        )
        return False
    if requeued is None:
        # 戻す前に取り消された(または stale の回復が戻した)。取り消しとして扱う。
        await _restore_statuses_after_cancel(
            oracle, job, lease_owner=lease_owner, discarded_status=IngestionJobStatus.QUEUED
        )
        return True
    logger.warning(
        "ingestion_job_requeued_transient_db_error",
        extra={
            "job_id": job.id,
            "document_id": job.document_id,
            "recipe_id": job.recipe_id,
            "phase": job.phase.value,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            **oracle_error_log_fields(error),
        },
    )
    _dispatch_ingestion_job(job.id)
    return True


async def _restore_statuses_before_job_phase(
    oracle: OracleClient,
    job: IngestionJob,
) -> None:
    """再実行の前に、文書(文書単位の job)またはレシピ行(レシピの job)を工程の前の状態へ戻す。"""
    if job.recipe_id is not None:
        await _restore_recipe_status_after_cancel(oracle, job)
        return
    await oracle.update_document_status(
        job.document_id, _restore_status_for_cancelled_phase(job.phase)
    )


async def _fail_ingestion_job(
    oracle: OracleClient,
    job: IngestionJob,
    error_message: str,
    *,
    lease_owner: str | None = None,
) -> bool:
    """RUNNING の job を FAILED にし、レシピの job はレシピ行だけを ERROR にする。

    文書単位の job の文書は pipeline(または異常終了を検知した worker)が ERROR にする。
    FAILED を書く前に取り消されていたら、取り消しとして status を戻す。FAILED を書けたら True。
    ``lease_owner`` を渡すと自分の lease のときだけ書き、lease を失っていたら何も変えない(#359)。
    """
    failed = await _finish_ingestion_job_unless_cancelled(
        oracle,
        job.id,
        status=IngestionJobStatus.FAILED,
        error_message=error_message,
        lease_owner=lease_owner,
    )
    if failed is None:
        await _restore_statuses_after_cancel(
            oracle, job, lease_owner=lease_owner, discarded_status=IngestionJobStatus.FAILED
        )
        return False
    await _mark_recipe_job_failed(oracle, job, error_message)
    return True


async def _restore_statuses_after_cancel(
    oracle: OracleClient,
    job: IngestionJob,
    *,
    lease_owner: str | None = None,
    discarded_status: IngestionJobStatus = IngestionJobStatus.CANCELLED,
) -> None:
    """取り消しを検知した側(worker)が、job の対象の status を取り消し後の状態へ戻す。

    - レシピの job(``recipe_id`` あり): レシピ行だけを戻す。文書(全レシピの集約)には触れない。
    - 文書単位の job: 文書の status を工程に応じて戻す。

    job が CANCELLED でない(stale の回復で QUEUED に戻された等)場合と、同じ文書の別の job が
    RUNNING の場合(その job が status を書いている)は戻さない。
    lease を持つ実行(``lease_owner``)は、lease を失っていたら(別の worker が claim し直した)
    戻さず、書けなかった結果(``discarded_status``)を捨てたことをログに残す(#359)。
    取り消しの後始末は、取り消された実行(lease の持ち主)が行う。
    """
    if await _discard_if_ingestion_lease_lost(
        oracle, job, lease_owner=lease_owner, discarded_status=discarded_status
    ):
        return
    current = await oracle.get_ingestion_job(job.id)
    if current is None or current.status != IngestionJobStatus.CANCELLED:
        logger.info(
            "ingestion_job_cancel_restore_skipped",
            extra={
                "job_id": job.id,
                "status": current.status.value if current is not None else None,
            },
        )
        return
    running_jobs = await oracle.list_document_ingestion_jobs(
        job.document_id, status=IngestionJobStatus.RUNNING
    )
    if any(other.id != job.id for other in running_jobs):
        logger.info(
            "ingestion_job_cancel_restore_skipped_other_running",
            extra={"job_id": job.id, "document_id": job.document_id},
        )
        return
    if job.recipe_id is not None:
        await _restore_recipe_status_after_cancel(oracle, job)
        return
    await oracle.update_document_status(
        job.document_id, _restore_status_for_cancelled_phase(job.phase)
    )


def _holds_ingestion_lease(lease: IngestionJobLease | None, lease_owner: str) -> bool:
    """job がまだ ``lease_owner`` の実行(RUNNING か、実行中に取り消された)なら True(#359)。"""
    return (
        lease is not None
        and lease.lease_owner == lease_owner
        and lease.status in _LEASE_HELD_INGESTION_JOB_STATUSES
    )


async def _discard_if_ingestion_lease_lost(
    oracle: OracleClient,
    job: IngestionJob,
    *,
    lease_owner: str | None,
    discarded_status: IngestionJobStatus,
) -> bool:
    """lease を持つ実行が lease を失っていたら、結果を捨てたことをログに残して True を返す(#359)。

    heartbeat が TTL を超えて途絶えると、別の worker の stale の回復が job を QUEUED(lease を外す)
    か FAILED に戻し、別の worker が claim し直す(lease の持ち主が変わる)。その後の古い実行の
    完了・失敗・再キュー・取り消しの後始末は、新しい実行の状態・文書・レシピを上書きしうるため
    捨てる。lease を持たない実行(``lease_owner`` なし)は従来どおり False を返す。
    """
    if lease_owner is None:
        return False
    lease = await oracle.get_ingestion_job_lease(job.id)
    if _holds_ingestion_lease(lease, lease_owner):
        return False
    logger.warning(
        "ingestion_job_stale_result_discarded",
        extra={
            "job_id": job.id,
            "document_id": job.document_id,
            "recipe_id": job.recipe_id,
            "phase": job.phase.value,
            "lease_owner": lease_owner,
            "discarded_status": discarded_status.value,
            "current_status": lease.status.value if lease is not None else None,
            "current_lease_owner": lease.lease_owner if lease is not None else None,
        },
    )
    return True


async def _mark_recipe_job_failed(
    oracle: OracleClient,
    job: IngestionJob,
    error_message: str,
) -> None:
    """レシピ失敗だけを記録する。既存 active chunk_set は変更しない。

    1 本のジョブが複数工程を通し実行するため、失敗工程は job.phase ではなく
    レシピ行の現在 status(pipeline が工程ごとに更新)から導出する。
    ジョブ開始工程より前へは戻さず、ゲート停止など非実行 status は job.phase に従う。
    """
    if job.recipe_id is None:
        return
    failed_phase = job.phase
    try:
        row = await oracle.get_document_recipe(job.document_id, job.recipe_id)
    except Exception:
        row = None
    if row is not None and row.get("status"):
        current_phase = _RUNNING_STATUS_TO_PHASE.get(FileStatus(str(row["status"])))
        if current_phase is not None and _RECIPE_PHASES.index(current_phase) > _RECIPE_PHASES.index(
            failed_phase
        ):
            failed_phase = current_phase
    await oracle.update_document_recipe_status(
        recipe_id=job.recipe_id,
        status=FileStatus.ERROR,
        failed_phase=failed_phase,
        error_message=error_message[:2000],
    )


async def _restore_recipe_status_after_cancel(
    oracle: OracleClient,
    job: IngestionJob,
) -> None:
    """取消後は旧 active があれば検索対象、無ければ job の工程を始める前の状態へ戻す。

    工程の前の状態は stale の回復(`_restore_recipe_status_for_job_phase`)と同じ対応にする。
    以前は一律に未処理へ戻していたため、Chunk 作成の取消で抽出の確認済み(REVIEW)が失われていた。
    """
    if job.recipe_id is None:
        return
    row = await oracle.get_document_recipe(job.document_id, job.recipe_id)
    await oracle.update_document_recipe_status(
        recipe_id=job.recipe_id,
        status=(
            FileStatus.INDEXED
            if row is not None and row.get("active_chunk_set_id") is not None
            else _restore_recipe_status_for_cancelled_phase(job.phase)
        ),
    )


def _safe_ingestion_job_error_message(error: Exception) -> str:
    if getattr(error, "safe_for_user", False):
        message = str(error).replace("\n", " ").strip()
        if message:
            return message[:2000]
    return "取込処理に失敗しました。"


async def _enqueue_auto_advance_job(job: IngestionJob, detail: DocumentDetail) -> None:
    """文書の有効な処理レシピに従い次 stage の job を投入する。"""
    try:
        settings, _config = await _resolve_ingestion_settings(OracleClient(), job.document_id)
        if (
            job.phase in {IngestionJobPhase.PREPROCESS, IngestionJobPhase.EXTRACT}
            and detail.status == FileStatus.REVIEW
            and settings.rag_auto_chunk_after_extract_enabled
        ):
            await _enqueue_chunk_phase_job_for_document(job.document_id)
        elif (
            job.phase == IngestionJobPhase.CHUNK
            and detail.status == FileStatus.CHUNKED
            and settings.rag_auto_index_after_chunk_enabled
        ):
            await _enqueue_index_phase_job_for_document(job.document_id)
    except Exception:
        logger.warning(
            "auto advance job enqueue failed. document_id=%s phase=%s",
            job.document_id,
            job.phase,
            exc_info=True,
        )


async def _finish_ingestion_job_unless_cancelled(
    oracle: OracleClient,
    job_id: str,
    *,
    status: IngestionJobStatus,
    error_message: str | None = None,
    lease_owner: str | None = None,
) -> IngestionJob | None:
    """RUNNING の job だけを完了・失敗にする。取り消し済みなどで書けなければ None を返す。

    状態の確認と書き込みは 1 回の条件付き UPDATE で行い、確認と書き込みの間に cancel API が
    割り込んでも CANCELLED を上書きしない(#305)。``lease_owner`` を渡すと自分の lease の行だけを
    書き、別の worker が claim し直した job を上書きしない(#359)。
    """
    finished = await oracle.transition_ingestion_job(
        job_id,
        from_statuses=(IngestionJobStatus.RUNNING,),
        to_status=status,
        error_message=error_message,
        finished_at=datetime.now(UTC),
        lease_owner=lease_owner,
    )
    if finished is None:
        logger.info(
            "ingestion_job_finish_skipped_not_running",
            extra={"job_id": job_id, "final_status": status.value},
        )
    return finished


@router.get("/{document_id}/content")
async def document_content(
    document_id: str,
    variant: Annotated[Literal["original", "prepared"], Query()] = "original",
    disposition: Annotated[Literal["inline", "attachment"], Query()] = "inline",
) -> Response:
    """原本またはファイル準備後 artifact を返す（文書プレビュー/ダウンロード用）。"""
    oracle = OracleClient()
    detail = await oracle.get_document(document_id)
    if detail is None or detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    return await document_content_response(
        detail,
        variant=variant,
        disposition=disposition,
        preprocess_artifact=detail.preprocess_artifact,
    )


@router.get("/{document_id}/preview-pages", response_model=ApiResponse[DocumentPreviewPages])
async def document_preview_pages(
    document_id: str,
    variant: Annotated[Literal["original", "prepared"], Query()] = "original",
) -> ApiResponse[DocumentPreviewPages]:
    """原本 / 処理後ファイルのページ一覧(PDF をページ画像で表示し、bbox の強調を重ねる。#349)。"""
    detail = await OracleClient().get_document(document_id)
    if detail is None or detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    return await _document_preview_pages_response(
        detail, variant=variant, preprocess_artifact=detail.preprocess_artifact
    )


@router.get("/{document_id}/preview-pages/{page_number}")
async def document_preview_page_image(
    document_id: str,
    page_number: Annotated[int, Path(ge=1, le=10000)],
    variant: Annotated[Literal["original", "prepared"], Query()] = "original",
    dpi: Annotated[int, Query(ge=48, le=288)] = 144,
) -> Response:
    """原本 / 処理後ファイルの 1 ページを PNG で返す(ページの /Rotate を反映した向き)。"""
    detail = await OracleClient().get_document(document_id)
    if detail is None or detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    return await _document_preview_page_image_response(
        detail,
        page_number=page_number,
        variant=variant,
        dpi=dpi,
        preprocess_artifact=detail.preprocess_artifact,
    )


async def _document_preview_pages_response(
    detail: DocumentDetail,
    *,
    variant: Literal["original", "prepared"],
    preprocess_artifact: DocumentPreprocessArtifact | None,
) -> ApiResponse[DocumentPreviewPages]:
    data, _file_name, _content_type = await load_document_content(
        detail, variant=variant, preprocess_artifact=preprocess_artifact
    )
    try:
        sizes = await asyncio.to_thread(page_sizes, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(
        data=DocumentPreviewPages(
            page_count=len(sizes),
            pages=[
                DocumentPreviewPage(page_number=index + 1, width=width, height=height)
                for index, (width, height) in enumerate(sizes)
            ],
        )
    )


async def _document_preview_page_image_response(
    detail: DocumentDetail,
    *,
    page_number: int,
    variant: Literal["original", "prepared"],
    dpi: int,
    preprocess_artifact: DocumentPreprocessArtifact | None,
) -> Response:
    data, _file_name, _content_type = await load_document_content(
        detail, variant=variant, preprocess_artifact=preprocess_artifact
    )
    try:
        png = await asyncio.to_thread(render_page_png, data, page_number, dpi)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{document_id}/crop")
async def document_crop(
    document_id: str,
    page: Annotated[int, Query(ge=1, le=10000)],
    x0: Annotated[float, Query(ge=0)],
    y0: Annotated[float, Query(ge=0)],
    x1: Annotated[float, Query(gt=0)],
    y1: Annotated[float, Query(gt=0)],
    page_width: Annotated[float, Query(gt=0)],
    page_height: Annotated[float, Query(gt=0)],
    dpi: Annotated[int, Query(ge=36, le=300)] = 150,
) -> Response:
    """解析に使ったファイルから bbox の領域を PNG で切り出す(解析結果プレビュー用)。

    bbox は解析結果のページ画像 px 座標(page_width / page_height 基準)。ファイル準備後の
    artifact があればそれ(解析対象)を、無ければ原本を開く。画像ファイルは 1 ページとして扱う。
    """
    if x1 <= x0 or y1 <= y0 or x1 > page_width * 1.001 or y1 > page_height * 1.001:
        raise HTTPException(status_code=422, detail="切り出し範囲が不正です。")
    try:
        data = await load_parsed_source(OracleClient(), document_id)
    except DocumentSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="原本ファイルの参照パスが不正です。") from exc
    try:
        png = await asyncio.to_thread(
            crop_png, data, page, (x0, y0, x1, y1), (page_width, page_height), dpi
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


async def document_content_response(
    detail: DocumentDetail,
    *,
    variant: Literal["original", "prepared"],
    disposition: Literal["inline", "attachment"],
    preprocess_artifact: DocumentPreprocessArtifact | None,
) -> Response:
    data, file_name, content_type = await load_document_content(
        detail, variant=variant, preprocess_artifact=preprocess_artifact
    )
    media_type = _content_type_header(content_type, data)
    headers = {
        # 非 ASCII ファイル名は RFC 5987 でエンコードする
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file_name)}",
        # MIME sniffing による取り違えを防ぐ
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=60",
    }
    if media_type.split(";", 1)[0].strip().lower() in SCRIPTABLE_CONTENT_TYPES:
        # 利用者がアップロードした HTML / SVG を同じ origin で開いてもスクリプトを動かさない(#281)。
        # 画面のプレビューは本文をテキストとして取得するので、表示には影響しない。
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=data, media_type=media_type, headers=headers)


async def load_document_content(
    detail: DocumentDetail,
    *,
    variant: Literal["original", "prepared"],
    preprocess_artifact: DocumentPreprocessArtifact | None,
) -> tuple[bytes, str, str]:
    """原本またはファイル準備後 artifact の (中身, ファイル名, content type) を返す。"""
    if detail.object_storage_path is None:
        raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
    if variant == "prepared":
        artifact = preprocess_artifact
        if artifact is None or not artifact.object_storage_path:
            raise HTTPException(
                status_code=404,
                detail="処理後ファイルが見つかりません。ファイル準備から再処理してください。",
            )
        path = artifact.object_storage_path
        file_name = artifact.file_name
        content_type = artifact.content_type or _document_media_type(detail)
        not_found_message = "処理後ファイルが見つかりません。ファイル準備から再処理してください。"
        bad_path_message = "処理後ファイルの参照パスが不正です。"
    else:
        path = detail.object_storage_path
        file_name = detail.file_name
        content_type = _document_media_type(detail)
        # 保存先の設定が変わった・ファイルが消えたなど、DB の参照先にファイルが無い（#1210）。
        not_found_message = "原本ファイルが保存先にありません。文書をアップロードし直してください。"
        bad_path_message = "原本ファイルの参照パスが不正です。"
    try:
        data = await ObjectStorageClient().get(path)
    except FileNotFoundError as exc:
        raise DocumentFileMissingError(not_found_message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=bad_path_message) from exc
    return data, file_name, content_type


async def _read_upload_file(file: UploadFile, max_bytes: int) -> bytes:
    """アップロードを上限付きで読み込む。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="ファイルサイズが上限を超えています。")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_display_filename(file_name: str | None) -> str:
    """表示・保存用のファイル名を安全な basename にする。

    長すぎる名前は拡張子を残したまま、文字数（255）と UTF-8 のバイト数（512。
    `rag_documents.file_name` の列長）の両方に収まるよう末尾側を切り詰める。
    """
    name = PurePath((file_name or "document.bin").replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name).strip(" .")
    if not name:
        return "document.bin"
    return _truncate_file_name(name)


def _truncate_file_name(name: str) -> str:
    """拡張子を残して、ファイル名を文字数とバイト数の上限に収める。"""
    if (
        len(name) <= MAX_UPLOAD_FILE_NAME_CHARS
        and len(name.encode("utf-8")) <= MAX_UPLOAD_FILE_NAME_BYTES
    ):
        return name
    suffix = PurePath(name).suffix
    if not suffix or len(suffix) > _MAX_PRESERVED_SUFFIX_CHARS:
        suffix = ""
    stem = name[: len(name) - len(suffix)] if suffix else name
    max_stem_chars = MAX_UPLOAD_FILE_NAME_CHARS - len(suffix)
    max_stem_bytes = MAX_UPLOAD_FILE_NAME_BYTES - len(suffix.encode("utf-8"))
    stem = stem[:max_stem_chars]
    while stem and len(stem.encode("utf-8")) > max_stem_bytes:
        stem = stem[:-1]
    stem = stem.rstrip(" .")
    return f"{stem}{suffix}" if stem else f"document{suffix}"


def _normalized_content_type(content_type: str | None) -> str:
    """MIME type のパラメータと大小差を正規化する。"""
    if not content_type:
        return "application/octet-stream"
    return content_type.split(";", maxsplit=1)[0].strip().lower() or "application/octet-stream"


def _sha256_hex(data: bytes) -> str:
    """アップロード原本の内容 hash を返す。"""
    return hashlib.sha256(data).hexdigest()


def _source_integrity_error(data: bytes, detail: DocumentDetail) -> str | None:
    """保存済みメタデータと取得した原本 bytes の整合性を検証する。"""
    if detail.file_size_bytes is not None and len(data) != detail.file_size_bytes:
        return SOURCE_SIZE_MISMATCH_MESSAGE
    if detail.content_sha256 is not None and _sha256_hex(data) != detail.content_sha256:
        return SOURCE_HASH_MISMATCH_MESSAGE
    return None


def _document_media_type(detail: DocumentDetail) -> str:
    """原本配信用 MIME type は保存済み metadata を優先する。"""
    if detail.content_type:
        return _normalized_content_type(detail.content_type)
    media_type, _ = mimetypes.guess_type(detail.file_name)
    return media_type or "application/octet-stream"


# プレビューでテキスト扱いする MIME type（text/* に加えて）
_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "application/csv",
    "application/x-ndjson",
}


def _is_text_media_type(media_type: str) -> bool:
    """テキストとしてデコード/プレビューする MIME type かどうか。"""
    return media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES


# python codec 名 → WHATWG (TextDecoder) ラベルの対応。
# ブラウザ TextDecoder は限られたラベルしか受け付けないため、検出結果を寄せる。
_WHATWG_LABELS = {
    "cp932": "shift_jis",
    "ms932": "shift_jis",
    "shift-jis": "shift_jis",
    "sjis": "shift_jis",
    "euc-jp": "euc-jp",
    "eucjp": "euc-jp",
    "euc-jis-2004": "euc-jp",
    "euc-jisx0213": "euc-jp",
    "cp936": "gbk",
    "gbk": "gbk",
    "gb2312": "gbk",
    "cp949": "euc-kr",
    "euc-kr": "euc-kr",
    "cp950": "big5",
    "big5hkscs": "big5",
}


def _detect_text_charset(data: bytes) -> str:
    """テキスト原本の文字コードを検出する（WHATWG TextDecoder 互換ラベルで返す）。"""
    if not data:
        return "utf-8"
    # UTF-8 として妥当ならそのまま採用（検出器の誤判定を避ける）
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    match = from_bytes(data).best()
    if match is None or not match.encoding:
        return "utf-8"
    # python codec 名（例: cp932 / euc_jis_2004）を WHATWG ラベルへ寄せる
    label = match.encoding.replace("_", "-")
    return _WHATWG_LABELS.get(label, label)


def _content_type_header(media_type: str, data: bytes) -> str:
    """テキスト系は文字コードを検出し charset を付与する（非 UTF-8 の文字化け対策）。"""
    if not _is_text_media_type(media_type):
        return media_type
    return f"{media_type}; charset={_detect_text_charset(data)}"


def _normalize_upload_knowledge_base_ids(values: list[str] | None) -> list[str]:
    """multipart form の KB ID 指定を API 内部のリストへ正規化する。"""
    if not values:
        return []
    expanded: list[str] = []
    for value in values:
        expanded.extend(value.split(","))
    return normalize_search_id_list(expanded)


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ApiResponse[RetrievedChunk])
async def get_document_citation_chunk(
    document_id: str, chunk_id: str
) -> ApiResponse[RetrievedChunk]:
    """閲覧可能な現行の根拠だけを ID で読み、原文の位置を返す。検索は行わない。"""
    chunk = await OracleClient().retrievable_chunk(document_id, chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="この根拠は現在の閲覧範囲では表示できません。")
    return ApiResponse(data=chunk)
