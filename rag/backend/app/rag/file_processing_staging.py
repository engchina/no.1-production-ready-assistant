"""file-processing golden set を staging 環境で実行する runner。"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from app.clients.file_storage import FileStorageClient
from app.clients.oracle import OracleClient, close_oracle_pool
from app.config import Settings, get_settings
from app.rag.file_processing_evaluation import (
    REQUIRED_FILE_PROCESSING_SOURCE_KINDS,
    FileProcessingContractReport,
    FileProcessingMetricThresholdResult,
    FileProcessingStagingRequirement,
    build_file_processing_staging_plan,
    evaluate_file_processing_metric_thresholds,
    quality_report_metadata_violation,
    run_file_processing_contract_checks,
)
from app.rag.grounding_metrics import GroundednessEvaluation, evaluate_groundedness
from app.rag.ingestion import IngestionPipeline
from app.rag.parser_adapter_routing import normalize_source_kind
from app.rag.pipeline import RagPipeline
from app.rag.source_profile import build_source_profile
from app.schemas.document import (
    DocumentChunkView,
    DocumentDetail,
    FileStatus,
    IngestionSegment,
    SourceProfile,
)
from app.schemas.knowledge_base import KnowledgeBaseDetail
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest, SearchResponse

FILE_PROCESSING_STAGING_PROMPT = (
    "文書をページ、表、見出し、bbox、要素IDを保持して日本語で構造化抽出してください。"
)
ARTIFACT_CACHE_PROBE_PAYLOAD = (
    b'{"probe":"file_processing_staging_artifact_cache","contains_document_text":false}\n'
)
ARTIFACT_CACHE_PROBE_CONTENT_TYPE = "application/json"


class StagingOracleProtocol(Protocol):
    """file-processing staging runner が使う Oracle client の最小契約。"""

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
        default_search_mode: SearchMode = SearchMode.HYBRID,
        retrieval_config: Mapping[str, object] | None = None,
    ) -> KnowledgeBaseDetail:
        """一時的な staging KB を作成する。"""

    async def archive_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail:
        """一時的な staging KB をアーカイブする。"""

    async def assign_documents_to_knowledge_base(
        self,
        knowledge_base_id: str,
        document_ids: Sequence[str],
    ) -> KnowledgeBaseDetail:
        """既存文書を KB へ追加する。"""

    async def create_document(
        self,
        file_name: str,
        object_storage_path: str,
        content_type: str | None,
        file_size_bytes: int | None = None,
        content_sha256: str | None = None,
        duplicate_of_document_id: str | None = None,
        knowledge_base_ids: Sequence[str] | None = None,
    ) -> DocumentDetail:
        """document row を作成する。"""

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        """document detail を返す。"""

    async def delete_document(self, document_id: str) -> bool:
        """document/chunk/index を削除する。"""

    async def count_document_chunks(self, document_id: str) -> int:
        """document の chunk 数を返す。"""

    async def list_document_chunks(self, document_id: str) -> list[DocumentChunkView]:
        """document の chunk metadata を返す。"""

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        """document の segment checkpoint を返す。"""


class StagingObjectStorageProtocol(Protocol):
    """file-processing staging runner が使う 保存先の最小契約。"""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """object を保存し URI を返す。"""

    async def get(self, key: str) -> bytes:
        """object を取得する。"""

    async def delete(self, key: str) -> bool:
        """object を削除する。"""


class StagingIngestionProtocol(Protocol):
    """file-processing staging runner が使う ingestion pipeline 契約。"""

    async def ingest(
        self,
        document_id: str,
        image_bytes: bytes,
        prompt: str,
        *,
        content_type: str = "application/octet-stream",
        source_profile: SourceProfile | None = None,
    ) -> DocumentDetail:
        """document を取込む。"""


class StagingSearchProtocol(Protocol):
    """file-processing staging runner が使う search pipeline 契約。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        """検索を実行する。"""


@dataclass(frozen=True)
class FileProcessingStagingGateResult:
    """staging で 1 pending check を閉じた結果。"""

    case_id: str
    scenario: str
    check: str
    suggested_gate: str
    passed: bool
    failure_code: str | None = None
    evidence: Mapping[str, object] | None = None


@dataclass(frozen=True)
class FileProcessingStagingRuntimeCheckResult:
    """staging case 実行前に外部契約を検証した結果。"""

    check: str
    status: str
    failure_code: str | None = None
    evidence: Mapping[str, object] | None = None

    @property
    def passed(self) -> bool:
        return self.status in {"ok", "skipped"}


@dataclass(frozen=True)
class FileProcessingStagingCaseResult:
    """staging で 1 manifest case を実行した結果。"""

    case_id: str
    scenario: str
    fixture: str
    document_id: str | None
    status: str
    chunk_count: int
    segment_count: int
    gate_results: tuple[FileProcessingStagingGateResult, ...]
    cleanup: Mapping[str, str]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.gate_results)


@dataclass(frozen=True)
class FileProcessingStagingReport:
    """file-processing staging runner の非機密結果。"""

    run_id: str
    knowledge_base_id: str | None
    case_results: tuple[FileProcessingStagingCaseResult, ...]
    runtime_checks: tuple[FileProcessingStagingRuntimeCheckResult, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    metric_evidence: Mapping[str, object] = field(default_factory=dict)
    threshold_results: tuple[FileProcessingMetricThresholdResult, ...] = ()
    local_manifest_errors: tuple[str, ...] = ()
    cleanup: Mapping[str, str] | None = None

    @property
    def passed(self) -> bool:
        return (
            not self.local_manifest_errors
            and all(check.passed for check in self.runtime_checks)
            and all(result.passed for result in self.case_results)
            and all(result.passed for result in self.threshold_results)
        )

    @property
    def case_count(self) -> int:
        return len(self.case_results)

    @property
    def gate_count(self) -> int:
        return sum(len(result.gate_results) for result in self.case_results)

    @property
    def failure_count(self) -> int:
        gate_failures = sum(
            1
            for case_result in self.case_results
            for gate_result in case_result.gate_results
            if not gate_result.passed
        )
        threshold_failures = sum(1 for result in self.threshold_results if not result.passed)
        runtime_failures = sum(1 for check in self.runtime_checks if not check.passed)
        return (
            len(self.local_manifest_errors) + runtime_failures + gate_failures + threshold_failures
        )


async def run_file_processing_staging_checks(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path,
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol,
    ingestion: StagingIngestionProtocol,
    search: StagingSearchProtocol,
    cleanup: bool = False,
    run_id: str | None = None,
    artifact_cache_enabled: bool = True,
    artifact_cache_prefix: str = "artifacts/extractions",
) -> FileProcessingStagingReport:
    """manifest の staging pending checks を実環境で検証する。"""
    resolved_run_id = run_id or uuid4().hex
    local_report = run_file_processing_contract_checks(manifest, manifest_path=manifest_path)
    if not local_report.passed:
        return FileProcessingStagingReport(
            run_id=resolved_run_id,
            knowledge_base_id=None,
            case_results=(),
            local_manifest_errors=(
                *local_report.manifest_errors,
                *(failure for result in local_report.case_results for failure in result.failures),
            ),
            threshold_results=evaluate_file_processing_metric_thresholds(
                {},
                _mapping(manifest.get("thresholds")),
            ),
        )

    requirements = build_file_processing_staging_plan(manifest, local_report)
    if not requirements:
        metrics: Mapping[str, float] = {}
        return FileProcessingStagingReport(
            run_id=resolved_run_id,
            knowledge_base_id=None,
            case_results=(),
            metrics=metrics,
            threshold_results=evaluate_file_processing_metric_thresholds(
                metrics,
                _mapping(manifest.get("thresholds")),
            ),
        )

    fixture_root = _fixture_root(manifest, manifest_path)
    case_by_id = _case_manifest_by_id(manifest)
    requirements_by_case = _requirements_by_case(requirements)
    runtime_checks = (
        await _verify_extraction_artifact_cache_roundtrip(
            storage=storage,
            run_id=resolved_run_id,
            enabled=artifact_cache_enabled,
            artifact_cache_prefix=artifact_cache_prefix,
        ),
    )
    if any(not check.passed for check in runtime_checks):
        metrics = _staging_metrics(())
        return FileProcessingStagingReport(
            run_id=resolved_run_id,
            knowledge_base_id=None,
            case_results=(),
            runtime_checks=runtime_checks,
            metrics=metrics,
            threshold_results=(),
        )
    kb = await oracle.create_knowledge_base(
        name=f"file-processing-golden-{resolved_run_id}",
        description="file-processing golden staging run",
        default_search_mode=SearchMode.HYBRID,
    )
    case_results: list[FileProcessingStagingCaseResult] = []
    created_documents: list[str] = []
    created_objects: list[str] = []
    cleanup_status: dict[str, str] = {"knowledge_base": "pending" if cleanup else "skipped"}
    try:
        for case_id, case_requirements in requirements_by_case.items():
            case = case_by_id[case_id]
            result, document_ids, object_uris = await _run_staging_case(
                case,
                case_requirements=case_requirements,
                fixture_root=fixture_root,
                run_id=resolved_run_id,
                knowledge_base_id=kb.id,
                oracle=oracle,
                storage=storage,
                ingestion=ingestion,
                search=search,
            )
            case_results.append(result)
            created_documents.extend(document_ids)
            created_objects.extend(object_uris)
    finally:
        if cleanup:
            cleanup_status = await _cleanup_staging_resources(
                oracle=oracle,
                storage=storage,
                knowledge_base_id=kb.id,
                document_ids=created_documents,
                object_uris=created_objects,
            )
    metrics = _merge_local_contract_metrics(_staging_metrics(case_results), local_report)
    metric_evidence = {
        **_local_metric_evidence(local_report),
        **_staging_metric_evidence(case_results),
    }
    return FileProcessingStagingReport(
        run_id=resolved_run_id,
        knowledge_base_id=kb.id,
        case_results=tuple(case_results),
        runtime_checks=runtime_checks,
        metrics=metrics,
        metric_evidence=metric_evidence,
        threshold_results=evaluate_file_processing_metric_thresholds(
            metrics,
            _mapping(manifest.get("thresholds")),
        ),
        cleanup=cleanup_status,
    )


async def run_file_processing_staging_checks_with_real_clients(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path,
    cleanup: bool = False,
    settings: Settings | None = None,
) -> FileProcessingStagingReport:
    """実際のモデルの API・Oracle・保存先の client を使って staging checks を実行する。"""
    resolved_settings = settings or get_settings()
    oracle = OracleClient(settings=resolved_settings)
    storage = FileStorageClient(settings=resolved_settings)
    try:
        return await run_file_processing_staging_checks(
            manifest,
            manifest_path=manifest_path,
            oracle=oracle,
            storage=storage,
            ingestion=IngestionPipeline(
                oracle=oracle,
                object_storage=storage,
                settings=resolved_settings,
            ),
            search=RagPipeline(oracle=oracle, settings=resolved_settings),
            cleanup=cleanup,
            artifact_cache_enabled=resolved_settings.rag_extraction_artifact_cache_enabled,
            artifact_cache_prefix=resolved_settings.rag_extraction_artifact_prefix,
        )
    finally:
        close_oracle_pool()


async def _verify_extraction_artifact_cache_roundtrip(
    *,
    storage: StagingObjectStorageProtocol,
    run_id: str,
    enabled: bool,
    artifact_cache_prefix: str,
) -> FileProcessingStagingRuntimeCheckResult:
    """extraction artifact cache の put/get/delete 契約を非機密 payload で検証する。"""
    check = "extraction_artifact_cache_roundtrip"
    if not enabled:
        return FileProcessingStagingRuntimeCheckResult(
            check=check,
            status="skipped",
            evidence={"artifact_cache_enabled": False},
        )

    prefix = _artifact_cache_probe_prefix(artifact_cache_prefix)
    key = f"{prefix}/staging-preflight/{run_id}/{uuid4().hex}.json"
    object_uri: str | None = None
    try:
        object_uri = await storage.put(
            key,
            ARTIFACT_CACHE_PROBE_PAYLOAD,
            ARTIFACT_CACHE_PROBE_CONTENT_TYPE,
        )
        fetched = await storage.get(object_uri)
        if fetched != ARTIFACT_CACHE_PROBE_PAYLOAD:
            cleanup_status = await _delete_artifact_cache_probe(storage, object_uri)
            return FileProcessingStagingRuntimeCheckResult(
                check=check,
                status="failed",
                failure_code="artifact_cache_probe_readback_mismatch",
                evidence={
                    "object_ref_hash": _hash_label(object_uri),
                    "object_uri_scheme": _storage_uri_scheme(object_uri),
                    "payload_bytes": len(ARTIFACT_CACHE_PROBE_PAYLOAD),
                    "cleanup": cleanup_status,
                },
            )
        deleted = await storage.delete(object_uri)
    except Exception as exc:
        cleanup_status = (
            await _delete_artifact_cache_probe(storage, object_uri)
            if object_uri is not None
            else "not_created"
        )
        evidence: dict[str, object] = {
            "error_type": type(exc).__name__,
            "payload_bytes": len(ARTIFACT_CACHE_PROBE_PAYLOAD),
            "cleanup": cleanup_status,
        }
        if object_uri is not None:
            evidence["object_ref_hash"] = _hash_label(object_uri)
            evidence["object_uri_scheme"] = _storage_uri_scheme(object_uri)
        return FileProcessingStagingRuntimeCheckResult(
            check=check,
            status="failed",
            failure_code="artifact_cache_roundtrip_failed",
            evidence=evidence,
        )

    cleanup_status = "deleted" if deleted else "missing"
    if not deleted:
        return FileProcessingStagingRuntimeCheckResult(
            check=check,
            status="failed",
            failure_code="artifact_cache_probe_cleanup_missing",
            evidence={
                "object_ref_hash": _hash_label(object_uri),
                "object_uri_scheme": _storage_uri_scheme(object_uri),
                "payload_bytes": len(ARTIFACT_CACHE_PROBE_PAYLOAD),
                "cleanup": cleanup_status,
            },
        )
    return FileProcessingStagingRuntimeCheckResult(
        check=check,
        status="ok",
        evidence={
            "object_ref_hash": _hash_label(object_uri),
            "object_uri_scheme": _storage_uri_scheme(object_uri),
            "payload_bytes": len(ARTIFACT_CACHE_PROBE_PAYLOAD),
            "cleanup": cleanup_status,
        },
    )


async def _delete_artifact_cache_probe(
    storage: StagingObjectStorageProtocol,
    object_uri: str,
) -> str:
    try:
        return "deleted" if await storage.delete(object_uri) else "missing"
    except Exception:
        return "error"


def _artifact_cache_probe_prefix(value: str) -> str:
    parts = [
        part
        for part in value.strip().replace("\\", "/").strip("/").split("/")
        if part not in ("", ".", "..")
    ]
    return "/".join(parts) or "artifacts/extractions"


async def _run_staging_case(
    case: Mapping[str, object],
    *,
    case_requirements: Sequence[FileProcessingStagingRequirement],
    fixture_root: Path,
    run_id: str,
    knowledge_base_id: str,
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol,
    ingestion: StagingIngestionProtocol,
    search: StagingSearchProtocol,
) -> tuple[FileProcessingStagingCaseResult, list[str], list[str]]:
    case_id = _case_id(case)
    if case_id == "duplicate-file-canonical-kb":
        return await _run_duplicate_staging_case(
            case,
            case_requirements=case_requirements,
            fixture_root=fixture_root,
            run_id=run_id,
            knowledge_base_id=knowledge_base_id,
            oracle=oracle,
            storage=storage,
            ingestion=ingestion,
            search=search,
        )
    evidence, document_ids, object_uris = await _ingest_fixture_for_staging(
        case,
        fixture_name=str(case["fixture"]),
        fixture_root=fixture_root,
        run_id=run_id,
        knowledge_base_id=knowledge_base_id,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
    )
    if _case_id(case) == "corrupted-file-partial-failure":
        second_pass = await _retry_existing_document(
            case,
            evidence=evidence,
            fixture_root=fixture_root,
            ingestion=ingestion,
            oracle=oracle,
            storage=storage,
        )
        evidence = second_pass
    elif _should_search_case(case):
        evidence = await _collect_search_evidence(
            case,
            evidence=evidence,
            knowledge_base_id=knowledge_base_id,
            search=search,
            document_filter=True,
        )
    gate_results = tuple(
        _evaluate_staging_requirement(requirement, case=case, evidence=evidence)
        for requirement in case_requirements
    )
    object_uris = _unique_nonempty_strings([*object_uris, *_artifact_paths_from_evidence(evidence)])
    return (
        _case_result_from_evidence(case, evidence, gate_results),
        document_ids,
        object_uris,
    )


async def _run_duplicate_staging_case(
    case: Mapping[str, object],
    *,
    case_requirements: Sequence[FileProcessingStagingRequirement],
    fixture_root: Path,
    run_id: str,
    knowledge_base_id: str,
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol,
    ingestion: StagingIngestionProtocol,
    search: StagingSearchProtocol,
) -> tuple[FileProcessingStagingCaseResult, list[str], list[str]]:
    canonical_evidence, document_ids, object_uris = await _ingest_fixture_for_staging(
        case,
        fixture_name=str(case["fixture"]),
        fixture_root=fixture_root,
        run_id=run_id,
        knowledge_base_id=None,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
    )
    duplicate_fixture = str(case["duplicate_fixture"])
    duplicate_data = (fixture_root / duplicate_fixture).read_bytes()
    duplicate_sha = hashlib.sha256(duplicate_data).hexdigest()
    duplicate_uri = await storage.put(
        f"file-processing-golden/{run_id}/{duplicate_fixture}",
        duplicate_data,
        _content_type(duplicate_fixture),
    )
    duplicate_detail = await oracle.create_document(
        file_name=duplicate_fixture,
        object_storage_path=duplicate_uri,
        content_type=_content_type(duplicate_fixture),
        file_size_bytes=len(duplicate_data),
        content_sha256=duplicate_sha,
        duplicate_of_document_id=canonical_evidence.document_id,
        knowledge_base_ids=[knowledge_base_id],
    )
    document_ids.append(duplicate_detail.id)
    object_uris.append(duplicate_uri)
    search_response = await search.run(
        SearchRequest(
            query=_staging_query(case),
            top_k=5,
            filters={"knowledge_base_id": knowledge_base_id},
            knowledge_base_ids=[knowledge_base_id],
        )
    )
    groundedness = _search_groundedness(search_response)
    canonical_citations = [
        chunk
        for chunk in search_response.citations
        if chunk.document_id == canonical_evidence.document_id
    ]
    evidence = replace(
        canonical_evidence,
        duplicate_document_id=duplicate_detail.id,
        duplicate_of_document_id=duplicate_detail.duplicate_of_document_id,
        knowledge_base_search_hit=bool(canonical_citations),
        knowledge_base_search_traceable=any(
            _retrieved_chunk_traceable(chunk) for chunk in canonical_citations
        ),
        retrieval_hit=bool(canonical_citations),
        retrieval_traceable=any(_retrieved_chunk_traceable(chunk) for chunk in canonical_citations),
        search_executed=True,
        search_page_hit=_search_hits_expected_page(case, search_response, canonical_evidence),
        search_page_traceable=_search_hits_expected_page_with_traceability(
            case,
            search_response,
            canonical_evidence,
        ),
        search_citation_count=len(search_response.citations),
        search_elapsed_ms=search_response.elapsed_ms,
        groundedness_passed=groundedness.grounded,
        groundedness_score=groundedness.score,
    )
    gate_results = tuple(
        _evaluate_staging_requirement(requirement, case=case, evidence=evidence)
        for requirement in case_requirements
    )
    object_uris = _unique_nonempty_strings([*object_uris, *_artifact_paths_from_evidence(evidence)])
    return (
        _case_result_from_evidence(case, evidence, gate_results),
        document_ids,
        object_uris,
    )


@dataclass(frozen=True)
class _StagingEvidence:
    document_id: str
    status: str
    chunks: tuple[DocumentChunkView, ...]
    segments: tuple[IngestionSegment, ...]
    extraction: Mapping[str, object]
    ingestion_error_type: str | None = None
    duplicate_document_id: str | None = None
    duplicate_of_document_id: str | None = None
    knowledge_base_search_hit: bool = False
    knowledge_base_search_traceable: bool = False
    retrieval_hit: bool = False
    retrieval_traceable: bool = False
    search_executed: bool = False
    search_page_hit: bool = False
    search_page_traceable: bool = False
    table_qa_answer_hit: bool = False
    table_qa_traceable: bool = False
    table_qa_cell_refs_traceable: bool = False
    table_qa_cell_refs_resolvable: bool = False
    table_qa_cell_refs_expected_count: int = 0
    table_qa_cell_refs_resolved_count: int = 0
    table_qa_cell_refs_covered_count: int = 0
    dependency_lineage_traceable: bool = False
    dependency_context_traceable: bool = False
    dependency_context_expected_count: int = 0
    dependency_context_covered_count: int = 0
    structural_section_traceable: bool = False
    structural_section_expected_count: int = 0
    structural_section_covered_count: int = 0
    search_citation_count: int = 0
    search_elapsed_ms: float | None = None
    groundedness_passed: bool | None = None
    groundedness_score: float | None = None
    ingestion_elapsed_ms: float | None = None
    artifact_integrity: Mapping[str, object] = field(default_factory=dict)
    initial_retry_segments: tuple[IngestionSegment, ...] = ()
    retry_segments: tuple[IngestionSegment, ...] = ()


async def _ingest_fixture_for_staging(
    case: Mapping[str, object],
    *,
    fixture_name: str,
    fixture_root: Path,
    run_id: str,
    knowledge_base_id: str | None,
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol,
    ingestion: StagingIngestionProtocol,
) -> tuple[_StagingEvidence, list[str], list[str]]:
    data = (fixture_root / fixture_name).read_bytes()
    content_type = _content_type(fixture_name)
    digest = hashlib.sha256(data).hexdigest()
    object_uri = await storage.put(
        f"file-processing-golden/{run_id}/{fixture_name}",
        data,
        content_type,
    )
    detail = await oracle.create_document(
        file_name=fixture_name,
        object_storage_path=object_uri,
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256=digest,
        knowledge_base_ids=[knowledge_base_id] if knowledge_base_id else None,
    )
    source_profile = build_source_profile(
        original_file_name=fixture_name,
        sanitized_file_name=fixture_name,
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256=digest,
        data=data,
    )
    error_type: str | None = None
    started = perf_counter()
    try:
        await ingestion.ingest(
            detail.id,
            data,
            FILE_PROCESSING_STAGING_PROMPT,
            content_type=content_type,
            source_profile=source_profile,
        )
    except Exception as exc:
        if _case_id(case) != "corrupted-file-partial-failure":
            raise
        error_type = type(exc).__name__
    ingestion_elapsed_ms = (perf_counter() - started) * 1000
    evidence = await _collect_staging_evidence(
        oracle,
        storage,
        detail.id,
        ingestion_error_type=error_type,
    )
    evidence = replace(evidence, ingestion_elapsed_ms=ingestion_elapsed_ms)
    return evidence, [detail.id], [object_uri]


async def _retry_existing_document(
    case: Mapping[str, object],
    *,
    evidence: _StagingEvidence,
    fixture_root: Path,
    ingestion: StagingIngestionProtocol,
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol,
) -> _StagingEvidence:
    fixture_name = str(case["fixture"])
    data = (fixture_root / fixture_name).read_bytes()
    content_type = _content_type(fixture_name)
    source_profile = build_source_profile(
        original_file_name=fixture_name,
        sanitized_file_name=fixture_name,
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )
    with suppress(Exception):
        await ingestion.ingest(
            evidence.document_id,
            data,
            FILE_PROCESSING_STAGING_PROMPT,
            content_type=content_type,
            source_profile=source_profile,
        )
    retry_evidence = await _collect_staging_evidence(
        oracle,
        storage,
        evidence.document_id,
        ingestion_error_type=evidence.ingestion_error_type,
    )
    return replace(
        retry_evidence,
        initial_retry_segments=evidence.segments,
        retry_segments=retry_evidence.segments,
        ingestion_elapsed_ms=evidence.ingestion_elapsed_ms,
    )


async def _collect_search_evidence(
    case: Mapping[str, object],
    *,
    evidence: _StagingEvidence,
    knowledge_base_id: str,
    search: StagingSearchProtocol,
    document_filter: bool,
) -> _StagingEvidence:
    filters = {"knowledge_base_id": knowledge_base_id}
    if document_filter:
        filters["document_id"] = evidence.document_id
    search_response = await search.run(
        SearchRequest(
            query=_staging_query(case),
            top_k=5,
            filters=filters,
            knowledge_base_ids=[knowledge_base_id],
        )
    )
    groundedness = _search_groundedness(search_response)
    document_citations = [
        chunk for chunk in search_response.citations if chunk.document_id == evidence.document_id
    ]
    expected_dependency_edges = _extraction_dependency_pairs(evidence.extraction)
    covered_dependency_context_edges = _search_dependency_context_edges(
        search_response,
        evidence,
    )
    expected_sections = _section_set(case.get("expected_sections"))
    covered_sections = _search_covered_sections(search_response, evidence)
    expected_table_cell_refs = _table_cell_ref_set(case.get("expected_table_cell_refs"))
    extraction_table_cell_refs = _extraction_table_cell_refs(evidence.extraction)
    covered_table_cell_refs = _search_covered_table_cell_refs(search_response, evidence)
    return replace(
        evidence,
        search_executed=True,
        retrieval_hit=bool(document_citations),
        retrieval_traceable=any(_retrieved_chunk_traceable(chunk) for chunk in document_citations),
        search_page_hit=_search_hits_expected_page(case, search_response, evidence),
        search_page_traceable=_search_hits_expected_page_with_traceability(
            case,
            search_response,
            evidence,
        ),
        table_qa_answer_hit=_search_answer_contains_expected(case, search_response),
        table_qa_traceable=_search_has_traceable_table_citation(search_response, evidence),
        table_qa_cell_refs_traceable=bool(
            expected_table_cell_refs
            and expected_table_cell_refs <= extraction_table_cell_refs
            and expected_table_cell_refs <= covered_table_cell_refs
        ),
        table_qa_cell_refs_resolvable=bool(
            expected_table_cell_refs and expected_table_cell_refs <= extraction_table_cell_refs
        ),
        table_qa_cell_refs_expected_count=len(expected_table_cell_refs),
        table_qa_cell_refs_resolved_count=len(
            expected_table_cell_refs & extraction_table_cell_refs
        ),
        table_qa_cell_refs_covered_count=len(expected_table_cell_refs & covered_table_cell_refs),
        dependency_lineage_traceable=_search_has_dependency_lineage_citation(
            search_response,
            evidence,
        ),
        dependency_context_traceable=bool(
            expected_dependency_edges
            and expected_dependency_edges <= covered_dependency_context_edges
        ),
        dependency_context_expected_count=len(expected_dependency_edges),
        dependency_context_covered_count=len(
            expected_dependency_edges & covered_dependency_context_edges
        ),
        structural_section_traceable=bool(
            expected_sections and expected_sections <= covered_sections
        ),
        structural_section_expected_count=len(expected_sections),
        structural_section_covered_count=len(expected_sections & covered_sections),
        search_citation_count=len(search_response.citations),
        search_elapsed_ms=search_response.elapsed_ms,
        groundedness_passed=groundedness.grounded,
        groundedness_score=groundedness.score,
    )


async def _collect_staging_evidence(
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol | None,
    document_id: str,
    *,
    ingestion_error_type: str | None = None,
) -> _StagingEvidence:
    detail = await oracle.get_document(document_id)
    chunks = tuple(await oracle.list_document_chunks(document_id))
    segments = tuple(await oracle.list_ingestion_segments(document_id))
    status = detail.status.value if detail is not None else "UNKNOWN"
    extraction = detail.extraction if detail is not None else {}
    artifact_integrity = (
        await _verify_artifact_integrity(
            storage=storage,
            document_id=document_id,
            extraction=extraction,
            segments=segments,
        )
        if storage is not None
        else {}
    )
    return _StagingEvidence(
        document_id=document_id,
        status=status,
        chunks=chunks,
        segments=segments,
        extraction=extraction,
        ingestion_error_type=ingestion_error_type,
        artifact_integrity=artifact_integrity,
    )


async def _verify_artifact_integrity(
    *,
    storage: StagingObjectStorageProtocol,
    document_id: str,
    extraction: Mapping[str, object],
    segments: Sequence[IngestionSegment],
) -> Mapping[str, object]:
    """保存先の artifact を読み戻し、identity metadata だけを非機密化する。"""
    parser_artifacts = _mapping(extraction.get("parser_artifacts"))
    full_path = _optional_str(parser_artifacts.get("extraction_artifact_path"))
    full_uri_scheme = _storage_uri_scheme(full_path)
    full_readable = False
    full_identity_verified = False
    full_payload_bytes = 0
    error_count = 0
    if full_path:
        payload, payload_bytes = await _read_artifact_payload(storage, full_path)
        full_payload_bytes = payload_bytes
        full_readable = payload is not None
        full_identity_verified = payload is not None and _full_artifact_payload_matches(
            payload, document_id
        )
        if not full_identity_verified:
            error_count += 1

    segment_expected = 0
    segment_readable = 0
    segment_identity_verified = 0
    segment_payload_bytes = 0
    segment_local_uri_count = 0
    segment_non_local_uri_count = 0
    for segment in _segments_with_unique_artifact_path(segments):
        segment_expected += 1
        if _storage_uri_scheme(segment.artifact_path) == "local":
            segment_local_uri_count += 1
        else:
            segment_non_local_uri_count += 1
        payload, payload_bytes = await _read_artifact_payload(storage, segment.artifact_path or "")
        segment_payload_bytes += payload_bytes
        if payload is None:
            error_count += 1
            continue
        segment_readable += 1
        if _segment_artifact_payload_matches(payload, segment):
            segment_identity_verified += 1
        else:
            error_count += 1

    return {
        "artifact_full_present": bool(full_path),
        "artifact_full_uri_scheme": full_uri_scheme,
        "artifact_full_local_uri": full_uri_scheme == "local",
        "artifact_full_readable": full_readable,
        "artifact_full_identity_verified": full_identity_verified,
        "artifact_full_payload_bytes": full_payload_bytes,
        "artifact_segment_expected_count": segment_expected,
        "artifact_segment_local_uri_count": segment_local_uri_count,
        "artifact_segment_non_local_uri_count": segment_non_local_uri_count,
        "artifact_segment_readable_count": segment_readable,
        "artifact_segment_identity_verified_count": segment_identity_verified,
        "artifact_segment_payload_bytes": segment_payload_bytes,
        "artifact_integrity_error_count": error_count,
    }


async def _read_artifact_payload(
    storage: StagingObjectStorageProtocol,
    artifact_path: str,
) -> tuple[Mapping[str, object] | None, int]:
    if not artifact_path:
        return None, 0
    try:
        data = await storage.get(artifact_path)
    except Exception:
        return None, 0
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, len(data)
    return (_mapping(payload) or None), len(data)


def _full_artifact_payload_matches(
    payload: Mapping[str, object],
    document_id: str,
) -> bool:
    parser_artifacts = _mapping(payload.get("parser_artifacts"))
    return (
        _positive_int(parser_artifacts.get("extraction_artifact_schema_version")) > 0
        and _optional_str(parser_artifacts.get("extraction_artifact_kind")) == "full"
        and _optional_str(parser_artifacts.get("extraction_artifact_document_id")) == document_id
    )


def _segment_artifact_payload_matches(
    payload: Mapping[str, object],
    segment: IngestionSegment,
) -> bool:
    parser_artifacts = _mapping(payload.get("parser_artifacts"))
    return (
        _positive_int(parser_artifacts.get("extraction_artifact_schema_version")) > 0
        and _optional_str(parser_artifacts.get("extraction_artifact_kind")) == "segment"
        and _optional_str(parser_artifacts.get("extraction_artifact_document_id"))
        == segment.document_id
        and _optional_str(parser_artifacts.get("extraction_artifact_segment_id"))
        == segment.segment_id
        and _positive_int(parser_artifacts.get("extraction_artifact_page_start"))
        == _positive_int(segment.page_start)
        and _positive_int(parser_artifacts.get("extraction_artifact_page_end"))
        == _positive_int(segment.page_end)
    )


def _segments_with_unique_artifact_path(
    segments: Sequence[IngestionSegment],
) -> tuple[IngestionSegment, ...]:
    result: list[IngestionSegment] = []
    seen_paths: set[str] = set()
    for segment in segments:
        if segment.status != "SUCCEEDED" or not segment.artifact_path:
            continue
        if segment.artifact_path in seen_paths:
            continue
        seen_paths.add(segment.artifact_path)
        result.append(segment)
    return tuple(result)


def _evaluate_staging_requirement(
    requirement: FileProcessingStagingRequirement,
    *,
    case: Mapping[str, object],
    evidence: _StagingEvidence,
) -> FileProcessingStagingGateResult:
    check = requirement.check
    passed = False
    failure_code: str | None = None
    if requirement.suggested_gate == "enterprise_ai_file_extraction_gate":
        passed, failure_code = _enterprise_ai_file_extraction_passed(case, evidence)
    elif requirement.suggested_gate == "preview_bbox_citation_gate":
        passed, failure_code = _preview_bbox_citation_passed(evidence)
    elif requirement.suggested_gate == "file_processing_page_hit_gate":
        passed, failure_code = _page_hit_passed(case, evidence)
    elif requirement.suggested_gate == "table_qa_search_gate":
        passed, failure_code = _table_qa_search_passed(case, evidence)
    elif requirement.suggested_gate == "dependency_lineage_search_gate":
        passed, failure_code = _dependency_lineage_search_passed(evidence)
    elif requirement.suggested_gate == "dependency_context_recall_gate":
        passed, failure_code = _dependency_context_recall_passed(evidence)
    elif requirement.suggested_gate == "structural_section_search_gate":
        passed, failure_code = _structural_section_search_passed(case, evidence)
    elif requirement.suggested_gate == "quality_report_metadata_gate":
        passed, failure_code = _quality_report_metadata_passed(evidence)
    elif requirement.suggested_gate == "duplicate_kb_membership_gate":
        passed, failure_code = _duplicate_kb_membership_passed(evidence)
    elif requirement.suggested_gate == "segment_artifact_reuse_gate":
        passed, failure_code = _segment_artifact_reuse_passed(evidence)
    else:
        passed, failure_code = _generic_traceability_passed(evidence)
    return FileProcessingStagingGateResult(
        case_id=requirement.case_id,
        scenario=requirement.scenario,
        check=check,
        suggested_gate=requirement.suggested_gate,
        passed=passed,
        failure_code=None if passed else failure_code,
        evidence=_safe_evidence_summary(evidence, case=case),
    )


def _enterprise_ai_file_extraction_passed(
    case: Mapping[str, object],
    evidence: _StagingEvidence,
) -> tuple[bool, str | None]:
    if evidence.status != FileStatus.INDEXED.value:
        return False, "document_not_indexed"
    if not evidence.chunks:
        return False, "chunks_missing"
    expected_kind = _optional_str(case.get("expected_content_kind"))
    if expected_kind and not _has_content_kind(evidence.chunks, expected_kind):
        return False, "expected_content_kind_missing"
    expected_pages = _int_set(case.get("expected_pages"))
    if expected_pages and not expected_pages <= _chunk_pages(evidence.chunks):
        return False, "expected_pages_missing"
    if not _has_traceable_chunk(evidence.chunks):
        return False, "traceable_chunk_missing"
    return True, None


def _preview_bbox_citation_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    extraction_violation = _extraction_preview_addressability_violation(evidence)
    if extraction_violation is None and any(
        _chunk_preview_addressable(chunk, evidence) for chunk in evidence.chunks
    ):
        return True, None
    if extraction_violation is not None:
        return False, f"preview_extraction_{extraction_violation}"
    if any(_chunk_has_valid_bbox(chunk) for chunk in evidence.chunks):
        if any(_chunk_has_invalid_bbox_unit(chunk) for chunk in evidence.chunks):
            return False, "bbox_unit_invalid"
        if any(_chunk_has_ambiguous_bbox_without_mode(chunk) for chunk in evidence.chunks):
            return False, "bbox_coordinate_mode_missing"
        for chunk in evidence.chunks:
            rotation_violation = _chunk_bbox_rotation_violation(chunk, evidence)
            if rotation_violation is not None:
                return False, rotation_violation
        return False, "preview_address_metadata_missing"
    return False, "bbox_missing"


def _page_hit_passed(
    case: Mapping[str, object],
    evidence: _StagingEvidence,
) -> tuple[bool, str | None]:
    expected_pages = _int_set(case.get("expected_pages"))
    if not expected_pages:
        return False, "expected_pages_missing"
    if not evidence.search_executed:
        return False, "search_not_executed"
    if not evidence.search_page_hit:
        return False, "expected_page_not_retrieved"
    if not evidence.search_page_traceable:
        return False, "search_citation_traceability_missing"
    return True, None


def _table_qa_search_passed(
    case: Mapping[str, object],
    evidence: _StagingEvidence,
) -> tuple[bool, str | None]:
    if not _optional_str(case.get("expected_answer")):
        return False, "expected_answer_missing"
    if not evidence.search_executed:
        return False, "search_not_executed"
    if not evidence.table_qa_answer_hit:
        return False, "expected_answer_not_in_search_answer"
    if not evidence.table_qa_traceable:
        return False, "table_citation_traceability_missing"
    if _table_cell_ref_set(case.get("expected_table_cell_refs")) and (
        not evidence.table_qa_cell_refs_resolvable
    ):
        return False, "table_cell_extraction_ref_missing"
    if _table_cell_ref_set(case.get("expected_table_cell_refs")) and (
        not evidence.table_qa_cell_refs_traceable
    ):
        return False, "table_cell_citation_missing"
    return True, None


def _dependency_lineage_search_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    if not evidence.search_executed:
        return False, "search_not_executed"
    if not _extraction_dependency_pairs(evidence.extraction):
        return False, "extraction_dependency_lineage_missing"
    if not evidence.dependency_lineage_traceable:
        return False, "dependency_lineage_citation_missing"
    return True, None


def _dependency_context_recall_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    if not evidence.search_executed:
        return False, "search_not_executed"
    if not _extraction_dependency_pairs(evidence.extraction):
        return False, "extraction_dependency_lineage_missing"
    if not evidence.dependency_context_traceable:
        return False, "dependency_context_not_recalled"
    return True, None


def _structural_section_search_passed(
    case: Mapping[str, object],
    evidence: _StagingEvidence,
) -> tuple[bool, str | None]:
    if not _section_set(case.get("expected_sections")):
        return False, "expected_sections_missing"
    if not evidence.search_executed:
        return False, "search_not_executed"
    if not evidence.structural_section_traceable:
        return False, "expected_sections_not_retrieved"
    return True, None


def _quality_report_metadata_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    if evidence.status != FileStatus.INDEXED.value:
        return False, "document_not_indexed"
    violation = quality_report_metadata_violation(evidence.extraction)
    if violation is not None:
        return False, violation
    return True, None


def _duplicate_kb_membership_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    if not evidence.duplicate_document_id:
        return False, "duplicate_document_missing"
    if evidence.duplicate_of_document_id != evidence.document_id:
        return False, "duplicate_alias_missing"
    if not evidence.knowledge_base_search_hit:
        return False, "duplicate_alias_not_searchable_in_kb"
    if not evidence.knowledge_base_search_traceable:
        return False, "duplicate_alias_citation_traceability_missing"
    return True, None


def _segment_artifact_reuse_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    before_segments = evidence.initial_retry_segments or evidence.segments
    after_segments = evidence.retry_segments or evidence.segments
    if not evidence.retry_segments:
        return False, "retry_segments_missing"
    failed_before = _segments_by_status(before_segments, "FAILED")
    succeeded_before = {
        segment_id: segment
        for segment_id, segment in _segments_by_status(before_segments, "SUCCEEDED").items()
        if segment.artifact_path
    }
    if not failed_before:
        return False, "failed_segment_missing"
    if not succeeded_before:
        return False, "successful_segment_artifact_missing"
    after_by_id = {segment.segment_id: segment for segment in after_segments}
    for segment_id, segment in succeeded_before.items():
        after_segment = after_by_id.get(segment_id)
        if after_segment is None or after_segment.status != "SUCCEEDED":
            return False, "successful_segment_not_retained"
        if after_segment.artifact_path != segment.artifact_path:
            return False, "successful_segment_artifact_rewritten"
        if _segment_attempt_count(after_segment) != _segment_attempt_count(segment):
            return False, "successful_segment_reprocessed"
    for segment_id, segment in failed_before.items():
        after_segment = after_by_id.get(segment_id)
        if after_segment is None:
            return False, "failed_segment_missing_after_retry"
        if _segment_attempt_count(after_segment) <= _segment_attempt_count(segment):
            return False, "failed_segment_not_retried"
    return True, None


def _segment_artifact_reuse_summary(evidence: _StagingEvidence) -> dict[str, object]:
    """segment retry / artifact reuse の非機密 summary を作る。"""
    before_segments = evidence.initial_retry_segments or evidence.segments
    after_segments = evidence.retry_segments or evidence.segments
    after_by_id = {segment.segment_id: segment for segment in after_segments}
    failed_before = _segments_by_status(before_segments, "FAILED")
    succeeded_before = _segments_by_status(before_segments, "SUCCEEDED")
    succeeded_with_artifact = {
        segment_id: segment
        for segment_id, segment in succeeded_before.items()
        if segment.artifact_path
    }
    retained_successful_artifacts = 0
    rewritten_successful_artifacts = 0
    reprocessed_successful_segments = 0
    for segment_id, segment in succeeded_with_artifact.items():
        after_segment = after_by_id.get(segment_id)
        if after_segment is None or after_segment.status != "SUCCEEDED":
            continue
        if after_segment.artifact_path == segment.artifact_path:
            retained_successful_artifacts += 1
        else:
            rewritten_successful_artifacts += 1
        if _segment_attempt_count(after_segment) != _segment_attempt_count(segment):
            reprocessed_successful_segments += 1
    failed_retried_count = 0
    failed_succeeded_count = 0
    for segment_id, segment in failed_before.items():
        after_segment = after_by_id.get(segment_id)
        if after_segment is None:
            continue
        if _segment_attempt_count(after_segment) > _segment_attempt_count(segment):
            failed_retried_count += 1
        if after_segment.status == "SUCCEEDED":
            failed_succeeded_count += 1
    parser_artifacts = _mapping(evidence.extraction.get("parser_artifacts"))
    warnings = _string_set(evidence.extraction.get("warnings"))
    segment_cache_miss_count = _positive_int(
        parser_artifacts.get("segment_extraction_artifact_cache_miss_count")
    )
    return {
        "retry_initial_failed_segment_count": len(failed_before),
        "retry_initial_successful_segment_count": len(succeeded_before),
        "retry_initial_successful_segment_artifact_count": len(succeeded_with_artifact),
        "retry_retained_successful_segment_artifact_count": retained_successful_artifacts,
        "retry_rewritten_successful_segment_artifact_count": rewritten_successful_artifacts,
        "retry_reprocessed_successful_segment_count": reprocessed_successful_segments,
        "retry_failed_segment_retried_count": failed_retried_count,
        "retry_failed_segment_succeeded_count": failed_succeeded_count,
        "segment_cache_miss_count": segment_cache_miss_count,
        "segment_cache_miss_warning": "segment_extraction_artifact_cache_miss" in warnings,
        "full_artifact_cached": bool(parser_artifacts.get("extraction_artifact_path")),
        "full_artifact_reused": bool(parser_artifacts.get("extraction_artifact_reused")),
        "full_artifact_identity_present": _full_artifact_identity_present(parser_artifacts),
    }


def _full_artifact_identity_present(parser_artifacts: Mapping[str, object]) -> bool:
    return (
        _positive_int(parser_artifacts.get("extraction_artifact_schema_version")) > 0
        and _optional_str(parser_artifacts.get("extraction_artifact_kind")) == "full"
        and _optional_str(parser_artifacts.get("extraction_artifact_document_id")) is not None
    )


def _segments_by_status(
    segments: Sequence[IngestionSegment],
    status: str,
) -> dict[str, IngestionSegment]:
    return {
        segment.segment_id: segment
        for segment in segments
        if segment.status == status and segment.segment_id
    }


def _segment_attempt_count(segment: IngestionSegment) -> int:
    return segment.attempt_count if segment.attempt_count is not None else 0


def _generic_traceability_passed(evidence: _StagingEvidence) -> tuple[bool, str | None]:
    if _has_traceable_chunk(evidence.chunks):
        return True, None
    return False, "traceable_chunk_missing"


def _case_result_from_evidence(
    case: Mapping[str, object],
    evidence: _StagingEvidence,
    gate_results: Sequence[FileProcessingStagingGateResult],
) -> FileProcessingStagingCaseResult:
    return FileProcessingStagingCaseResult(
        case_id=_case_id(case),
        scenario=str(case.get("scenario", "")),
        fixture=str(case.get("fixture", "")),
        document_id=evidence.document_id,
        status=evidence.status,
        chunk_count=len(evidence.chunks),
        segment_count=len(evidence.segments),
        gate_results=tuple(gate_results),
        cleanup={"document": "pending"},
    )


def _safe_evidence_summary(
    evidence: _StagingEvidence,
    *,
    case: Mapping[str, object],
) -> Mapping[str, object]:
    chunk_templates = sorted(_chunk_template_labels(evidence.chunks))
    return {
        "source_kind": normalize_source_kind(case.get("modality")),
        "parser_backend": _extraction_parser_backend(evidence.extraction),
        "parser_profile": _extraction_parser_profile(evidence.extraction),
        "status": evidence.status,
        "chunk_count": len(evidence.chunks),
        "chunk_templates": chunk_templates,
        "chunk_template_chunk_count": sum(
            1 for chunk in evidence.chunks if _chunk_template_labels_for_chunk(chunk)
        ),
        "segment_count": len(evidence.segments),
        "bbox_chunk_count": sum(1 for chunk in evidence.chunks if _chunk_has_valid_bbox(chunk)),
        "preview_addressable_chunk_count": sum(
            1 for chunk in evidence.chunks if _chunk_preview_addressable(chunk, evidence)
        ),
        "extraction_bbox_target_count": _extraction_preview_target_count(evidence),
        "extraction_preview_addressable_target_count": (
            _extraction_preview_addressable_target_count(evidence)
        ),
        "element_lineage_chunk_count": sum(
            1 for chunk in evidence.chunks if _chunk_has_resolvable_element_lineage(chunk, evidence)
        ),
        "traceable_chunk_count": sum(1 for chunk in evidence.chunks if _chunk_traceable(chunk)),
        "artifact_segment_count": sum(1 for segment in evidence.segments if segment.artifact_path),
        **dict(evidence.artifact_integrity),
        "initial_retry_segment_count": len(evidence.initial_retry_segments),
        "retry_segment_count": len(evidence.retry_segments),
        **_segment_artifact_reuse_summary(evidence),
        "failed_segment_count": sum(
            1 for segment in evidence.segments if segment.status == "FAILED"
        ),
        "parser_fallback_used": _parser_fallback_used(evidence.extraction),
        "extraction_page_coverage": _extraction_page_coverage(evidence.extraction),
        "low_confidence_count": _extraction_low_confidence_count(evidence.extraction),
        "quality_report_complete": quality_report_metadata_violation(evidence.extraction) is None,
        "ingestion_elapsed_ms": _rounded_optional_float(evidence.ingestion_elapsed_ms),
        "retrieval_hit": evidence.retrieval_hit,
        "retrieval_traceable": evidence.retrieval_traceable,
        "search_executed": evidence.search_executed,
        "search_page_hit": evidence.search_page_hit,
        "search_page_traceable": evidence.search_page_traceable,
        "table_qa_answer_hit": evidence.table_qa_answer_hit,
        "table_qa_traceable": evidence.table_qa_traceable,
        "table_qa_cell_refs_traceable": evidence.table_qa_cell_refs_traceable,
        "table_qa_cell_refs_resolvable": evidence.table_qa_cell_refs_resolvable,
        "table_qa_cell_refs_expected_count": evidence.table_qa_cell_refs_expected_count,
        "table_qa_cell_refs_resolved_count": evidence.table_qa_cell_refs_resolved_count,
        "table_qa_cell_refs_covered_count": evidence.table_qa_cell_refs_covered_count,
        "dependency_lineage_traceable": evidence.dependency_lineage_traceable,
        "dependency_context_traceable": evidence.dependency_context_traceable,
        "dependency_context_expected_count": evidence.dependency_context_expected_count,
        "dependency_context_covered_count": evidence.dependency_context_covered_count,
        "structural_section_traceable": evidence.structural_section_traceable,
        "structural_section_expected_count": evidence.structural_section_expected_count,
        "structural_section_covered_count": evidence.structural_section_covered_count,
        "search_citation_count": evidence.search_citation_count,
        "search_elapsed_ms": _rounded_optional_float(evidence.search_elapsed_ms),
        "groundedness_passed": evidence.groundedness_passed,
        "groundedness_score": _rounded_optional_float(evidence.groundedness_score),
        "knowledge_base_search_hit": evidence.knowledge_base_search_hit,
        "knowledge_base_search_traceable": evidence.knowledge_base_search_traceable,
        "ingestion_error_type": evidence.ingestion_error_type,
    }


CHUNK_TEMPLATE_METADATA_KEYS = (
    "chunk_template",
    "source_chunk_template",
    "chunk_templates",
    "source_chunk_templates",
    "chunking_template",
    "template",
)


def _chunk_template_labels(chunks: Sequence[DocumentChunkView]) -> set[str]:
    """chunk metadata から非機密 template label だけを集める。"""
    labels: set[str] = set()
    for chunk in chunks:
        labels.update(_chunk_template_labels_for_chunk(chunk))
    return labels


def _chunk_template_labels_for_chunk(chunk: DocumentChunkView) -> set[str]:
    labels: set[str] = set()
    for key in CHUNK_TEMPLATE_METADATA_KEYS:
        labels.update(_safe_template_label_set(chunk.metadata.get(key)))
    return labels


def _safe_template_label_set(value: object) -> set[str]:
    raw_values: list[object]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return set()
        if text.startswith("[") or text.startswith("{"):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if decoded is not None:
                return _safe_template_label_set(decoded)
        raw_values = re.split(r"[\n,;\t]+", text)
    elif isinstance(value, Mapping):
        raw_values = [
            value.get(key)
            for key in ("chunk_template", "source_chunk_template", "template", "id", "name")
        ]
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        raw_values = list(value)
    else:
        return set()
    labels: set[str] = set()
    for raw_value in raw_values:
        if raw_value is None or isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, Mapping | Sequence) and not isinstance(
            raw_value,
            str | bytes | bytearray,
        ):
            labels.update(_safe_template_label_set(raw_value))
            continue
        label = str(raw_value).strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", label):
            labels.add(label)
    return labels


def _extraction_parser_backend(extraction: Mapping[str, object]) -> str:
    quality_report = _mapping(extraction.get("quality_report"))
    backend = _optional_str(quality_report.get("parser_backend"))
    if backend:
        return backend
    artifacts = _mapping(extraction.get("parser_artifacts"))
    return _optional_str(artifacts.get("parser_backend")) or ""


def _extraction_parser_profile(extraction: Mapping[str, object]) -> str:
    quality_report = _mapping(extraction.get("quality_report"))
    profile = _optional_str(quality_report.get("parser_profile"))
    if profile:
        return profile
    artifacts = _mapping(extraction.get("parser_artifacts"))
    return _optional_str(artifacts.get("source_parser")) or ""


def _staging_metrics(
    case_results: Sequence[FileProcessingStagingCaseResult],
) -> Mapping[str, float]:
    """staging gate の非機密 aggregate metrics を作る。"""
    gate_results = [gate for case in case_results for gate in case.gate_results]
    total_chunks = _evidence_sum(gate_results, "chunk_count")
    preview_gate_results = [
        gate for gate in gate_results if gate.suggested_gate == "preview_bbox_citation_gate"
    ]
    preview_total_chunks = _evidence_sum(preview_gate_results, "chunk_count")
    preview_bbox_target_count = _evidence_sum(preview_gate_results, "extraction_bbox_target_count")
    metrics: dict[str, float] = {
        "gate_pass_rate": _safe_ratio(
            sum(1 for gate in gate_results if gate.passed),
            len(gate_results),
        ),
        "parser_fallback_rate": _safe_ratio(
            _evidence_true_count(gate_results, "parser_fallback_used"),
            len(case_results),
        ),
        "low_confidence_document_rate": _safe_ratio(
            _evidence_positive_count(gate_results, "low_confidence_count"),
            len(case_results),
        ),
        "failed_segment_rate": _safe_ratio(
            _evidence_positive_count(gate_results, "failed_segment_count"),
            len(case_results),
        ),
        "citation_traceability_coverage": _safe_ratio(
            _evidence_sum(gate_results, "traceable_chunk_count"),
            total_chunks,
        ),
        "bbox_citation_coverage": _safe_ratio(
            _evidence_sum(preview_gate_results, "bbox_chunk_count"),
            preview_total_chunks,
        ),
        "preview_addressability_coverage": _safe_ratio(
            _evidence_sum(preview_gate_results, "preview_addressable_chunk_count")
            + _evidence_sum(
                preview_gate_results,
                "extraction_preview_addressable_target_count",
            ),
            preview_total_chunks + preview_bbox_target_count,
        ),
        "element_lineage_coverage": _safe_ratio(
            _evidence_sum(gate_results, "element_lineage_chunk_count"),
            total_chunks,
        ),
        "retrieval_recall": _evidence_bool_ratio(
            gate_results,
            numerator_key="retrieval_traceable",
            denominator_key="search_executed",
        ),
        "groundedness": _evidence_bool_ratio(
            gate_results,
            numerator_key="groundedness_passed",
            denominator_key="search_executed",
        ),
    }
    ingestion_elapsed_values = _evidence_float_values(gate_results, "ingestion_elapsed_ms")
    if ingestion_elapsed_values:
        metrics["ingestion_p95_ms"] = _percentile(ingestion_elapsed_values, 0.95)
    page_coverage_values = _evidence_float_values(gate_results, "extraction_page_coverage")
    if page_coverage_values:
        metrics["extraction_page_coverage"] = round(
            sum(page_coverage_values) / len(page_coverage_values),
            4,
        )
    _add_gate_metric(
        metrics,
        key="page_hit_accuracy",
        gate_results=gate_results,
        suggested_gate="file_processing_page_hit_gate",
    )
    _add_check_metric(
        metrics,
        key="table_qa_accuracy",
        gate_results=gate_results,
        check="table_qa_accuracy",
    )
    table_cell_expected_count = _evidence_sum(
        gate_results,
        "table_qa_cell_refs_expected_count",
    )
    if table_cell_expected_count:
        table_cell_lineage_count = sum(
            min(
                _positive_int(result.evidence.get("table_qa_cell_refs_resolved_count")),
                _positive_int(result.evidence.get("table_qa_cell_refs_covered_count")),
            )
            for result in gate_results
            if result.evidence is not None
        )
        metrics["table_cell_lineage_coverage"] = _safe_ratio(
            table_cell_lineage_count,
            table_cell_expected_count,
        )
    _add_check_metric(
        metrics,
        key="bbox_coordinate_validity_coverage",
        gate_results=gate_results,
        check="bbox_coordinate_validity",
    )
    _add_check_metric(
        metrics,
        key="structural_section_coverage",
        gate_results=gate_results,
        check="structural_section_coverage",
    )
    _add_check_metric(
        metrics,
        key="dependency_context_recall",
        gate_results=gate_results,
        check="dependency_context_recall",
    )
    _add_check_metric(
        metrics,
        key="ingestion_quality_report_completeness",
        gate_results=gate_results,
        check="quality_report_metadata",
    )
    return metrics


def _merge_local_contract_metrics(
    staging_metrics: Mapping[str, float],
    local_report: FileProcessingContractReport,
) -> Mapping[str, float]:
    """staging gate では発生しない local measured metrics を report に補う。"""
    metrics = dict(staging_metrics)
    for key, value in _local_contract_metrics(local_report).items():
        metrics.setdefault(key, value)
    metrics["adapter_contract_coverage"] = _adapter_contract_coverage(metrics)
    return metrics


ADAPTER_CONTRACT_COMPONENT_METRICS = (
    "parser_routing_accuracy",
    "source_kind_coverage",
    "backend_source_kind_coverage",
    "extraction_page_coverage",
    "preview_addressability_coverage",
    "element_lineage_coverage",
    "table_structure_fidelity",
    "table_cell_lineage_coverage",
    "visual_chunk_metadata_completeness",
    "ingestion_quality_report_completeness",
    "parser_warning_taxonomy_coverage",
)


def _adapter_contract_coverage(metrics: Mapping[str, float]) -> float:
    """adapter/schema/chunk/citation の重要構造契約を 1 つの gate に畳み込む。"""
    values = [_metric_ratio(metrics.get(metric)) for metric in ADAPTER_CONTRACT_COMPONENT_METRICS]
    return round(sum(values) / len(values), 4)


def _metric_ratio(value: object) -> float:
    if isinstance(value, int | float) and math.isfinite(value):
        return max(0.0, min(1.0, float(value)))
    return 0.0


def _local_contract_metrics(report: FileProcessingContractReport) -> dict[str, float]:
    metrics: dict[str, float] = {
        "parser_fallback_rate": _safe_ratio(
            sum(1 for result in report.case_results if result.fallback_used),
            report.case_count,
        ),
        "low_confidence_document_rate": _safe_ratio(
            sum(1 for result in report.case_results if result.low_confidence_count > 0),
            report.case_count,
        ),
        "failed_segment_rate": _safe_ratio(
            sum(1 for result in report.case_results if result.failed_segment_count > 0),
            report.case_count,
        ),
    }
    page_coverages = [
        result.page_coverage for result in report.case_results if result.page_coverage is not None
    ]
    if page_coverages:
        metrics["extraction_page_coverage"] = round(
            sum(page_coverages) / len(page_coverages),
            4,
        )
    check_metrics = {
        "table_qa_accuracy": "table_qa_accuracy",
        "citation_traceability_coverage": "citation_traceability",
        "bbox_citation_coverage": "bbox_citation",
        "bbox_coordinate_validity_coverage": "bbox_coordinate_validity",
        "preview_addressability_coverage": "preview_jump",
        "element_lineage_coverage": "element_lineage",
        "chunk_block_integrity": "chunk_block_integrity",
        "reading_order_consistency": "reading_order",
        "structural_section_coverage": "structural_section_coverage",
        "table_structure_fidelity": "table_structure_fidelity",
        "table_cell_lineage_coverage": "table_cell_lineage",
        "table_row_tree_fidelity": "table_row_tree_fidelity",
        "visual_chunk_metadata_completeness": "visual_chunk_metadata",
        "chunk_size_compliance": "chunk_size_compliance",
        "chunk_contextual_coherence": "chunk_contextual_coherence",
        "cross_page_table_continuity_coverage": "cross_page_table_continuity",
        "ingestion_quality_report_completeness": "quality_report_metadata",
        "parser_warning_taxonomy_coverage": "parser_warning_taxonomy",
    }
    for metric, check in check_metrics.items():
        value = _local_check_metric_value(report, check)
        if value is not None:
            metrics[metric] = value
    parser_routing_accuracy = _local_parser_routing_accuracy(report)
    if parser_routing_accuracy is not None:
        metrics["parser_routing_accuracy"] = parser_routing_accuracy
    source_kind_coverage = _local_source_kind_coverage(report)
    if source_kind_coverage is not None:
        metrics["source_kind_coverage"] = source_kind_coverage
    backend_source_kind_coverage = _local_backend_source_kind_coverage(report)
    if backend_source_kind_coverage is not None:
        metrics["backend_source_kind_coverage"] = backend_source_kind_coverage
    return metrics


def _local_metric_evidence(report: FileProcessingContractReport) -> dict[str, object]:
    return {"backend_source_kind_coverage": _local_backend_source_kind_coverage_evidence(report)}


def _staging_metric_evidence(
    case_results: Sequence[FileProcessingStagingCaseResult],
) -> dict[str, object]:
    summaries: dict[str, Mapping[str, object]] = {}
    evidence_by_case: dict[str, tuple[Mapping[str, object], ...]] = {}
    for case_result in case_results:
        if not case_result.gate_results:
            continue
        gate_evidence = tuple(
            _mapping(gate_result.evidence)
            for gate_result in case_result.gate_results
            if gate_result.evidence is not None
        )
        if not gate_evidence:
            continue
        summaries[case_result.case_id] = gate_evidence[0]
        evidence_by_case[case_result.case_id] = gate_evidence
    return {
        "segment_artifact_reuse": _segment_artifact_reuse_metric_evidence(summaries),
        "table_cell_lineage": _table_cell_lineage_metric_evidence(evidence_by_case),
        "preview_addressability": _preview_addressability_metric_evidence(case_results),
    }


def _table_cell_lineage_metric_evidence(
    evidence_by_case: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    case_count = len(evidence_by_case)
    expected_case_count = 0
    expected_ref_count = 0
    resolved_ref_count = 0
    covered_ref_count = 0
    lineage_ref_count = 0
    expected_case_refs: set[str] = set()
    resolved_case_refs: set[str] = set()
    covered_case_refs: set[str] = set()
    lineage_case_refs: set[str] = set()
    unresolved_case_refs: set[str] = set()
    uncovered_case_refs: set[str] = set()
    for case_id, gate_evidence in evidence_by_case.items():
        expected = max(
            _positive_int(evidence.get("table_qa_cell_refs_expected_count"))
            for evidence in gate_evidence
        )
        resolved = max(
            _positive_int(evidence.get("table_qa_cell_refs_resolved_count"))
            for evidence in gate_evidence
        )
        covered = max(
            _positive_int(evidence.get("table_qa_cell_refs_covered_count"))
            for evidence in gate_evidence
        )
        if expected <= 0:
            continue
        case_ref = _case_ref_label(case_id)
        expected_case_refs.add(case_ref)
        if resolved >= expected:
            resolved_case_refs.add(case_ref)
        else:
            unresolved_case_refs.add(case_ref)
        if covered >= expected:
            covered_case_refs.add(case_ref)
        lineage = min(resolved, covered)
        if lineage >= expected:
            lineage_case_refs.add(case_ref)
        else:
            uncovered_case_refs.add(case_ref)
        expected_case_count += 1
        expected_ref_count += expected
        resolved_ref_count += resolved
        covered_ref_count += covered
        lineage_ref_count += lineage
    unresolved_ref_count = max(expected_ref_count - resolved_ref_count, 0)
    uncovered_ref_count = max(expected_ref_count - lineage_ref_count, 0)
    coverage = _safe_ratio(lineage_ref_count, expected_ref_count) if expected_ref_count else None
    return {
        "case_count": case_count,
        "expected_case_count": expected_case_count,
        "expected_ref_count": expected_ref_count,
        "resolved_ref_count": resolved_ref_count,
        "covered_ref_count": covered_ref_count,
        "lineage_ref_count": lineage_ref_count,
        "unresolved_ref_count": unresolved_ref_count,
        "uncovered_ref_count": uncovered_ref_count,
        "expected_case_refs": sorted(expected_case_refs),
        "resolved_case_refs": sorted(resolved_case_refs),
        "covered_case_refs": sorted(covered_case_refs),
        "lineage_case_refs": sorted(lineage_case_refs),
        "unresolved_case_refs": sorted(unresolved_case_refs),
        "uncovered_case_refs": sorted(uncovered_case_refs),
        "all_expected_refs_resolved": bool(expected_ref_count) and unresolved_ref_count == 0,
        "all_expected_refs_covered": bool(expected_ref_count) and uncovered_ref_count == 0,
        "coverage": coverage,
    }


def _preview_addressability_metric_evidence(
    case_results: Sequence[FileProcessingStagingCaseResult],
) -> dict[str, object]:
    preview_gate_evidence = [
        _mapping(gate_result.evidence)
        for case_result in case_results
        for gate_result in case_result.gate_results
        if gate_result.suggested_gate == "preview_bbox_citation_gate"
        and gate_result.evidence is not None
    ]
    chunk_target_count = sum(
        _positive_int(evidence.get("chunk_count")) for evidence in preview_gate_evidence
    )
    chunk_bbox_count = sum(
        _positive_int(evidence.get("bbox_chunk_count")) for evidence in preview_gate_evidence
    )
    chunk_addressable_count = sum(
        _positive_int(evidence.get("preview_addressable_chunk_count"))
        for evidence in preview_gate_evidence
    )
    extraction_bbox_target_count = sum(
        _positive_int(evidence.get("extraction_bbox_target_count"))
        for evidence in preview_gate_evidence
    )
    extraction_addressable_target_count = sum(
        _positive_int(evidence.get("extraction_preview_addressable_target_count"))
        for evidence in preview_gate_evidence
    )
    preview_gate_case_refs: set[str] = set()
    addressable_case_refs: set[str] = set()
    unaddressable_case_refs: set[str] = set()
    chunk_bbox_case_refs: set[str] = set()
    chunk_missing_bbox_case_refs: set[str] = set()
    for case_result in case_results:
        gate_evidence = [
            _mapping(gate_result.evidence)
            for gate_result in case_result.gate_results
            if gate_result.suggested_gate == "preview_bbox_citation_gate"
            and gate_result.evidence is not None
        ]
        if not gate_evidence:
            continue
        case_ref = _case_ref_label(case_result.case_id)
        case_chunk_target_count = sum(
            _positive_int(evidence.get("chunk_count")) for evidence in gate_evidence
        )
        case_chunk_bbox_count = sum(
            _positive_int(evidence.get("bbox_chunk_count")) for evidence in gate_evidence
        )
        case_chunk_addressable_count = sum(
            _positive_int(evidence.get("preview_addressable_chunk_count"))
            for evidence in gate_evidence
        )
        case_extraction_bbox_target_count = sum(
            _positive_int(evidence.get("extraction_bbox_target_count"))
            for evidence in gate_evidence
        )
        case_extraction_addressable_target_count = sum(
            _positive_int(evidence.get("extraction_preview_addressable_target_count"))
            for evidence in gate_evidence
        )
        case_target_count = case_chunk_target_count + case_extraction_bbox_target_count
        case_addressable_count = (
            case_chunk_addressable_count + case_extraction_addressable_target_count
        )
        if case_target_count <= 0:
            continue
        preview_gate_case_refs.add(case_ref)
        if case_addressable_count >= case_target_count:
            addressable_case_refs.add(case_ref)
        else:
            unaddressable_case_refs.add(case_ref)
        if case_chunk_target_count > 0 and case_chunk_bbox_count >= case_chunk_target_count:
            chunk_bbox_case_refs.add(case_ref)
        elif case_chunk_target_count > 0:
            chunk_missing_bbox_case_refs.add(case_ref)
    target_count = chunk_target_count + extraction_bbox_target_count
    addressable_target_count = chunk_addressable_count + extraction_addressable_target_count
    unaddressable_target_count = max(target_count - addressable_target_count, 0)
    coverage = _safe_ratio(addressable_target_count, target_count) if target_count else None
    return {
        "case_count": len(case_results),
        "preview_gate_case_count": len(preview_gate_evidence),
        "chunk_target_count": chunk_target_count,
        "chunk_bbox_count": chunk_bbox_count,
        "chunk_addressable_count": chunk_addressable_count,
        "extraction_bbox_target_count": extraction_bbox_target_count,
        "extraction_addressable_target_count": extraction_addressable_target_count,
        "target_count": target_count,
        "addressable_target_count": addressable_target_count,
        "unaddressable_target_count": unaddressable_target_count,
        "preview_gate_case_refs": sorted(preview_gate_case_refs),
        "addressable_case_refs": sorted(addressable_case_refs),
        "unaddressable_case_refs": sorted(unaddressable_case_refs),
        "chunk_bbox_case_refs": sorted(chunk_bbox_case_refs),
        "chunk_missing_bbox_case_refs": sorted(chunk_missing_bbox_case_refs),
        "chunk_bbox_coverage": (
            _safe_ratio(chunk_bbox_count, chunk_target_count) if chunk_target_count else None
        ),
        "coverage": coverage,
        "all_targets_addressable": bool(target_count) and unaddressable_target_count == 0,
        "all_chunks_have_bbox": bool(chunk_target_count) and chunk_bbox_count == chunk_target_count,
    }


def _segment_artifact_reuse_metric_evidence(
    summaries: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    retry_cases = {
        case_id: summary
        for case_id, summary in summaries.items()
        if _positive_int(summary.get("retry_segment_count")) > 0
    }
    full_artifact_cached_cases = _case_refs_for_summary_flag(
        summaries,
        "full_artifact_cached",
    )
    full_artifact_identity_verified_cases = _case_refs_for_summary_flag(
        summaries,
        "artifact_full_identity_verified",
    )
    retained_successful_segment_artifact_cases = _case_refs_for_positive_summary_metric(
        retry_cases,
        "retry_retained_successful_segment_artifact_count",
    )
    successful_segment_rewrite_cases = _case_refs_for_positive_summary_metric(
        retry_cases,
        "retry_rewritten_successful_segment_artifact_count",
    )
    successful_segment_reprocess_cases = _case_refs_for_positive_summary_metric(
        retry_cases,
        "retry_reprocessed_successful_segment_count",
    )
    segment_cache_miss_cases = _case_refs_for_positive_summary_metric(
        summaries,
        "segment_cache_miss_count",
    )
    artifact_integrity_error_cases = _case_refs_for_positive_summary_metric(
        summaries,
        "artifact_integrity_error_count",
    )
    return {
        "case_count": len(summaries),
        "retry_case_count": len(retry_cases),
        "case_refs": sorted(_case_ref_label(case_id) for case_id in summaries),
        "retry_case_refs": sorted(_case_ref_label(case_id) for case_id in retry_cases),
        "initial_failed_segment_count": sum(
            _positive_int(summary.get("retry_initial_failed_segment_count"))
            for summary in retry_cases.values()
        ),
        "initial_successful_segment_artifact_count": sum(
            _positive_int(summary.get("retry_initial_successful_segment_artifact_count"))
            for summary in retry_cases.values()
        ),
        "retained_successful_segment_artifact_count": sum(
            _positive_int(summary.get("retry_retained_successful_segment_artifact_count"))
            for summary in retry_cases.values()
        ),
        "rewritten_successful_segment_artifact_count": sum(
            _positive_int(summary.get("retry_rewritten_successful_segment_artifact_count"))
            for summary in retry_cases.values()
        ),
        "reprocessed_successful_segment_count": sum(
            _positive_int(summary.get("retry_reprocessed_successful_segment_count"))
            for summary in retry_cases.values()
        ),
        "failed_segment_retried_count": sum(
            _positive_int(summary.get("retry_failed_segment_retried_count"))
            for summary in retry_cases.values()
        ),
        "failed_segment_succeeded_count": sum(
            _positive_int(summary.get("retry_failed_segment_succeeded_count"))
            for summary in retry_cases.values()
        ),
        "segment_cache_miss_count": sum(
            _positive_int(summary.get("segment_cache_miss_count")) for summary in summaries.values()
        ),
        "segment_cache_miss_case_count": sum(
            1
            for summary in summaries.values()
            if _positive_int(summary.get("segment_cache_miss_count")) > 0
        ),
        "full_artifact_cached_case_count": sum(
            1 for summary in summaries.values() if summary.get("full_artifact_cached") is True
        ),
        "full_artifact_cached_case_refs": full_artifact_cached_cases,
        "full_artifact_identity_present_case_count": sum(
            1
            for summary in summaries.values()
            if summary.get("full_artifact_identity_present") is True
        ),
        "full_artifact_local_case_count": sum(
            1 for summary in summaries.values() if summary.get("artifact_full_local_uri") is True
        ),
        "full_artifact_readable_case_count": sum(
            1 for summary in summaries.values() if summary.get("artifact_full_readable") is True
        ),
        "full_artifact_identity_verified_case_count": sum(
            1
            for summary in summaries.values()
            if summary.get("artifact_full_identity_verified") is True
        ),
        "full_artifact_identity_verified_case_refs": (full_artifact_identity_verified_cases),
        "retained_successful_segment_artifact_case_refs": (
            retained_successful_segment_artifact_cases
        ),
        "successful_segment_rewrite_case_refs": successful_segment_rewrite_cases,
        "successful_segment_reprocess_case_refs": successful_segment_reprocess_cases,
        "segment_cache_miss_case_refs": segment_cache_miss_cases,
        "artifact_integrity_error_case_refs": artifact_integrity_error_cases,
        "segment_artifact_expected_count": sum(
            _positive_int(summary.get("artifact_segment_expected_count"))
            for summary in summaries.values()
        ),
        "segment_artifact_local_uri_count": sum(
            _positive_int(summary.get("artifact_segment_local_uri_count"))
            for summary in summaries.values()
        ),
        "segment_artifact_non_local_uri_count": sum(
            _positive_int(summary.get("artifact_segment_non_local_uri_count"))
            for summary in summaries.values()
        ),
        "segment_artifact_readable_count": sum(
            _positive_int(summary.get("artifact_segment_readable_count"))
            for summary in summaries.values()
        ),
        "segment_artifact_identity_verified_count": sum(
            _positive_int(summary.get("artifact_segment_identity_verified_count"))
            for summary in summaries.values()
        ),
        "artifact_integrity_error_count": sum(
            _positive_int(summary.get("artifact_integrity_error_count"))
            for summary in summaries.values()
        ),
    }


def _case_ref_label(case_id: str) -> str:
    return f"case:{_hash_label(case_id)}"


def _case_refs_for_summary_flag(
    summaries: Mapping[str, Mapping[str, object]],
    key: str,
) -> list[str]:
    return sorted(
        _case_ref_label(case_id)
        for case_id, summary in summaries.items()
        if summary.get(key) is True
    )


def _case_refs_for_positive_summary_metric(
    summaries: Mapping[str, Mapping[str, object]],
    key: str,
) -> list[str]:
    return sorted(
        _case_ref_label(case_id)
        for case_id, summary in summaries.items()
        if _positive_int(summary.get(key)) > 0
    )


def _local_check_metric_value(
    report: FileProcessingContractReport,
    check: str,
) -> float | None:
    passed_count = 0
    failed_count = 0
    for result in report.case_results:
        if check in result.passed_checks:
            passed_count += 1
        if any(failure.startswith(f"{check}:") for failure in result.failures):
            failed_count += 1
    measured_count = passed_count + failed_count
    if measured_count == 0:
        return None
    return _safe_ratio(passed_count, measured_count)


def _local_parser_routing_accuracy(
    report: FileProcessingContractReport,
) -> float | None:
    """SourceProfile / parser registry / chunk template の local 分流成功率。"""
    passed_count = 0
    failed_count = 0
    for result in report.case_results:
        routing_failed = any(
            failure.startswith(
                (
                    "expected_parser_profile:",
                    "expected_chunk_template:",
                    "expected_unsupported_reason:",
                )
            )
            for failure in result.failures
        )
        if routing_failed:
            failed_count += 1
            continue
        if {
            "expected_parser_profile",
            "expected_chunk_template",
        } <= set(result.passed_checks):
            passed_count += 1
    measured_count = passed_count + failed_count
    if measured_count == 0:
        return None
    return _safe_ratio(passed_count, measured_count)


def _local_source_kind_coverage(
    report: FileProcessingContractReport,
) -> float | None:
    """golden set が要求 source kind を偏りなく local contract で覆っているか。"""
    if not report.case_results:
        return None
    covered_source_kinds = {
        result.source_kind
        for result in report.case_results
        if result.source_kind in REQUIRED_FILE_PROCESSING_SOURCE_KINDS and not result.failures
    }
    return _safe_ratio(
        len(covered_source_kinds),
        len(REQUIRED_FILE_PROCESSING_SOURCE_KINDS),
    )


def _local_backend_source_kind_coverage(
    report: FileProcessingContractReport,
) -> float | None:
    """source kind ごとに成功 parser backend の帰属があるか。"""
    if not report.case_results:
        return None
    raw_covered_source_kinds = _local_backend_source_kind_coverage_evidence(report).get(
        "covered_source_kinds"
    )
    if not isinstance(raw_covered_source_kinds, Sequence) or isinstance(
        raw_covered_source_kinds,
        str | bytes | bytearray,
    ):
        return None
    covered_source_kinds = {
        source_kind for source_kind in raw_covered_source_kinds if isinstance(source_kind, str)
    }
    return _safe_ratio(
        len(covered_source_kinds),
        len(REQUIRED_FILE_PROCESSING_SOURCE_KINDS),
    )


def _local_backend_source_kind_coverage_evidence(
    report: FileProcessingContractReport,
) -> dict[str, object]:
    """backend-source coverage の非機密 matrix evidence。"""
    backend_source_kinds: dict[str, set[str]] = {}
    backend_case_ids: dict[str, list[str]] = {}
    for result in report.case_results:
        if result.failures:
            continue
        if result.source_kind not in REQUIRED_FILE_PROCESSING_SOURCE_KINDS:
            continue
        if not result.parser_backend:
            continue
        backend_source_kinds.setdefault(result.parser_backend, set()).add(result.source_kind)
        backend_case_ids.setdefault(result.parser_backend, []).append(result.case_id)
    covered_source_kinds = {
        source_kind
        for source_kinds in backend_source_kinds.values()
        for source_kind in source_kinds
    }
    return {
        "value": _safe_ratio(
            len(covered_source_kinds),
            len(REQUIRED_FILE_PROCESSING_SOURCE_KINDS),
        ),
        "required_source_kinds": sorted(REQUIRED_FILE_PROCESSING_SOURCE_KINDS),
        "covered_source_kinds": sorted(covered_source_kinds),
        "missing_source_kinds": sorted(
            REQUIRED_FILE_PROCESSING_SOURCE_KINDS - covered_source_kinds
        ),
        "backend_source_kinds": {
            backend: sorted(source_kinds)
            for backend, source_kinds in sorted(backend_source_kinds.items())
        },
        "backend_case_ids": {
            backend: sorted(case_ids) for backend, case_ids in sorted(backend_case_ids.items())
        },
    }


def _add_gate_metric(
    metrics: dict[str, float],
    *,
    key: str,
    gate_results: Sequence[FileProcessingStagingGateResult],
    suggested_gate: str,
) -> None:
    matching = [gate for gate in gate_results if gate.suggested_gate == suggested_gate]
    if matching:
        metrics[key] = _safe_ratio(sum(1 for gate in matching if gate.passed), len(matching))


def _add_check_metric(
    metrics: dict[str, float],
    *,
    key: str,
    gate_results: Sequence[FileProcessingStagingGateResult],
    check: str,
) -> None:
    matching = [gate for gate in gate_results if gate.check == check]
    if matching:
        metrics[key] = _safe_ratio(sum(1 for gate in matching if gate.passed), len(matching))


def _evidence_sum(
    gate_results: Sequence[FileProcessingStagingGateResult],
    key: str,
) -> int:
    values: dict[str, int] = {}
    for gate in gate_results:
        evidence = _mapping(gate.evidence)
        value = evidence.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        values[gate.case_id] = max(values.get(gate.case_id, 0), value)
    return sum(values.values())


def _evidence_true_count(
    gate_results: Sequence[FileProcessingStagingGateResult],
    key: str,
) -> int:
    values: dict[str, bool] = {}
    for gate in gate_results:
        evidence = _mapping(gate.evidence)
        value = evidence.get(key)
        if isinstance(value, bool):
            values[gate.case_id] = values.get(gate.case_id, False) or value
    return sum(1 for value in values.values() if value)


def _evidence_positive_count(
    gate_results: Sequence[FileProcessingStagingGateResult],
    key: str,
) -> int:
    values: dict[str, int] = {}
    for gate in gate_results:
        evidence = _mapping(gate.evidence)
        value = evidence.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        values[gate.case_id] = max(values.get(gate.case_id, 0), int(value))
    return sum(1 for value in values.values() if value > 0)


def _evidence_bool_ratio(
    gate_results: Sequence[FileProcessingStagingGateResult],
    *,
    numerator_key: str,
    denominator_key: str,
) -> float:
    numerator_values: dict[str, bool] = {}
    denominator_values: dict[str, bool] = {}
    for gate in gate_results:
        evidence = _mapping(gate.evidence)
        denominator = evidence.get(denominator_key)
        if isinstance(denominator, bool):
            denominator_values[gate.case_id] = (
                denominator_values.get(gate.case_id, False) or denominator
            )
        numerator = evidence.get(numerator_key)
        if isinstance(numerator, bool):
            numerator_values[gate.case_id] = numerator_values.get(gate.case_id, False) or numerator
    denominator_count = sum(1 for value in denominator_values.values() if value)
    numerator_count = sum(
        1
        for case_id, value in numerator_values.items()
        if value and denominator_values.get(case_id)
    )
    return _safe_ratio(numerator_count, denominator_count)


def _evidence_float_values(
    gate_results: Sequence[FileProcessingStagingGateResult],
    key: str,
) -> tuple[float, ...]:
    values_by_case: dict[str, float] = {}
    for gate in gate_results:
        evidence = _mapping(gate.evidence)
        value = evidence.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        values_by_case[gate.case_id] = float(value)
    return tuple(values_by_case.values())


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _rounded_optional_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _parser_fallback_used(extraction: Mapping[str, object]) -> bool:
    quality = _mapping(extraction.get("quality_report"))
    artifacts = _mapping(extraction.get("parser_artifacts"))
    return bool(quality.get("fallback_used")) or bool(artifacts.get("fallback_used"))


def _extraction_page_coverage(extraction: Mapping[str, object]) -> float | None:
    quality = _mapping(extraction.get("quality_report"))
    value = _optional_float(quality.get("page_coverage"))
    return round(value, 4) if value is not None else None


def _extraction_low_confidence_count(extraction: Mapping[str, object]) -> int:
    quality = _mapping(extraction.get("quality_report"))
    return _positive_int(quality.get("low_confidence_count"))


def _should_search_case(case: Mapping[str, object]) -> bool:
    return any(
        isinstance(case.get(key), str) and str(case.get(key)).strip()
        for key in ("staging_query", "expected_answer")
    )


def _search_groundedness(search_response: SearchResponse) -> GroundednessEvaluation:
    context = "\n".join(chunk.text for chunk in search_response.citations)
    return evaluate_groundedness(search_response.answer, context)


def _search_hits_expected_page(
    case: Mapping[str, object],
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> bool:
    expected_pages = _int_set(case.get("expected_pages"))
    if not expected_pages:
        return any(chunk.document_id == evidence.document_id for chunk in search_response.citations)
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        page_start = _chunk_page_int(chunk, "page_start")
        page_end = _chunk_page_int(chunk, "page_end") or page_start
        if page_start is None or page_end is None:
            continue
        if expected_pages & set(range(page_start, page_end + 1)):
            return True
    return False


def _search_hits_expected_page_with_traceability(
    case: Mapping[str, object],
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> bool:
    """期待 page に命中した citation が preview/監査に使える lineage を持つか。"""
    expected_pages = _int_set(case.get("expected_pages"))
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        if expected_pages:
            page_start = _chunk_page_int(chunk, "page_start")
            page_end = _chunk_page_int(chunk, "page_end") or page_start
            if page_start is None or page_end is None:
                continue
            if not expected_pages & set(range(page_start, page_end + 1)):
                continue
        if _retrieved_chunk_traceable(chunk):
            return True
    return False


def _search_answer_contains_expected(
    case: Mapping[str, object],
    search_response: SearchResponse,
) -> bool:
    expected = _optional_str(case.get("expected_answer"))
    if not expected:
        return False
    return _normalize_answer(expected) in _normalize_answer(search_response.answer)


def _search_has_traceable_table_citation(
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> bool:
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        if _retrieved_chunk_content_kind(chunk) not in {"table", "sheet"}:
            continue
        if _retrieved_chunk_traceable(chunk):
            return True
    return False


def _search_covered_table_cell_refs(
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> set[str]:
    refs: set[str] = set()
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        if _retrieved_chunk_content_kind(chunk) not in {"table", "sheet"}:
            continue
        if not _retrieved_chunk_traceable(chunk):
            continue
        refs.update(_retrieved_chunk_table_cell_refs(chunk))
    return refs


def _retrieved_chunk_table_cell_refs(chunk: RetrievedChunk) -> set[str]:
    refs: set[str] = set()
    for key in (
        "table_cell_refs",
        "cell_refs",
        "cell_ref",
        "cell_address",
        "formula_cell_refs",
        "formula_cell_ref",
    ):
        refs.update(_table_cell_ref_set(chunk.metadata.get(key)))
    return refs


TABLE_CELL_REF_KEYS = (
    "formula_cell_refs",
    "formula_cell_ref",
    "table_cell_refs",
    "table_cell_ref",
    "cell_refs",
    "cell_ref",
    "cell_address",
    "address",
    "ref",
    "reference",
)


def _extraction_table_cell_refs(extraction: Mapping[str, object]) -> set[str]:
    refs: set[str] = set()
    raw_tables = extraction.get("tables")
    if not isinstance(raw_tables, Sequence) or isinstance(raw_tables, str | bytes | bytearray):
        return refs
    for raw_table in raw_tables:
        table = _mapping(raw_table)
        refs.update(_table_cell_ref_set(table.get("metadata")))
        raw_cells = table.get("cells")
        if not isinstance(raw_cells, Sequence) or isinstance(raw_cells, str | bytes | bytearray):
            continue
        for raw_cell in raw_cells:
            cell = _mapping(raw_cell)
            refs.update(_table_cell_ref_set(cell.get("metadata")))
            refs.update(_table_cell_ref_set(cell))
    return refs


def _table_cell_ref_set(value: object) -> set[str]:
    raw_values: list[object]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return set()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                decoded = None
            if decoded is not None:
                return _table_cell_ref_set(decoded)
            raw_values = [stripped]
        else:
            raw_values = re.split(r"[\n,;\t]+", stripped)
    elif isinstance(value, Mapping):
        mapping_refs: set[str] = set()
        for key in TABLE_CELL_REF_KEYS:
            mapping_refs.update(_table_cell_ref_set(value.get(key)))
        metadata = value.get("metadata")
        if metadata is not value:
            mapping_refs.update(_table_cell_ref_set(metadata))
        return mapping_refs
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        raw_values = list(value)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        raw_values = [str(value)]
    else:
        return set()
    refs: set[str] = set()
    for raw_value in raw_values:
        if raw_value is None or isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, Mapping | Sequence) and not isinstance(
            raw_value,
            str | bytes | bytearray,
        ):
            refs.update(_table_cell_ref_set(raw_value))
            continue
        cleaned = str(raw_value).strip().strip("'\"")
        if cleaned:
            refs.add(cleaned[:80].casefold())
    return refs


def _search_has_dependency_lineage_citation(
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> bool:
    expected_edges = _extraction_dependency_pairs(evidence.extraction)
    if not expected_edges:
        return False
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        if not _retrieved_chunk_traceable(chunk):
            continue
        if not _metadata_text(chunk.metadata.get("parent_element_ids")):
            continue
        if _retrieved_chunk_dependency_edges(chunk) & expected_edges:
            return True
    return False


def _search_dependency_context_edges(
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> set[tuple[str, str]]:
    expected_edges = _extraction_dependency_pairs(evidence.extraction)
    if not expected_edges:
        return set()
    covered: set[tuple[str, str]] = set()
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        if not _metadata_bool(chunk.metadata.get("context_dependency_promoted")):
            continue
        if not _retrieved_chunk_traceable(chunk):
            continue
        covered.update(_retrieved_chunk_dependency_edges(chunk) & expected_edges)
        covered.update(_retrieved_chunk_parent_child_edges(chunk) & expected_edges)
    return covered


def _retrieved_chunk_parent_child_edges(chunk: RetrievedChunk) -> set[tuple[str, str]]:
    parent_ids = _id_set(chunk.metadata.get("parent_element_ids"))
    element_ids = _id_set(chunk.metadata.get("element_ids"))
    return {
        (parent_id, element_id)
        for parent_id in parent_ids
        for element_id in element_ids
        if parent_id != element_id
    }


def _search_covered_sections(
    search_response: SearchResponse,
    evidence: _StagingEvidence,
) -> set[str]:
    sections: set[str] = set()
    for chunk in search_response.citations:
        if chunk.document_id != evidence.document_id:
            continue
        for key in ("section_path", "section_title"):
            section = _normalize_section_label(chunk.metadata.get(key))
            if section:
                sections.add(section)
    return sections


def _extraction_dependency_pairs(extraction: Mapping[str, object]) -> set[tuple[str, str]]:
    elements = extraction.get("elements")
    if not isinstance(elements, Sequence) or isinstance(elements, str | bytes | bytearray):
        return set()
    pairs: set[tuple[str, str]] = set()
    for raw_element in elements:
        element = _mapping(raw_element)
        parent_id = _optional_str(element.get("parent_id"))
        element_id = _optional_str(element.get("element_id"))
        if not element_id:
            element_id = _optional_str(_mapping(element.get("metadata")).get("element_id"))
        if parent_id and element_id:
            pairs.add((parent_id, element_id))
    return pairs


def _retrieved_chunk_dependency_edges(chunk: RetrievedChunk) -> set[tuple[str, str]]:
    value = chunk.metadata.get("dependency_edges")
    if value is None:
        return set()
    if isinstance(value, str):
        try:
            raw_edges = json.loads(value)
        except json.JSONDecodeError:
            return set()
    else:
        raw_edges = value
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, str | bytes | bytearray):
        return set()
    edges: set[tuple[str, str]] = set()
    for raw_edge in raw_edges:
        edge = _mapping(raw_edge)
        parent_id = _optional_str(edge.get("parent_id"))
        child_id = _optional_str(edge.get("child_id"))
        if parent_id and child_id:
            edges.add((parent_id, child_id))
    return edges


def _retrieved_chunk_traceable(chunk: RetrievedChunk) -> bool:
    """Search citation が document/chunk/page と element/bbox/section lineage を持つか。"""
    if not chunk.document_id or not chunk.chunk_id:
        return False
    page_start = _chunk_page_int(chunk, "page_start")
    if page_start is None:
        return False
    metadata = chunk.metadata
    return bool(
        _metadata_text(metadata.get("element_ids"))
        or _retrieved_chunk_has_bbox(chunk)
        or _metadata_text(metadata.get("section_path"))
    )


def _metadata_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0
    return False


def _id_set(value: object) -> set[str]:
    text = _metadata_text(value)
    if not text:
        return set()
    return {
        item.strip().strip("'\"")
        for item in text.replace("[", "").replace("]", "").split(",")
        if item.strip().strip("'\"")
    }


def _retrieved_chunk_has_bbox(chunk: RetrievedChunk) -> bool:
    value = chunk.metadata.get("bbox")
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip() and value.strip().casefold() != "null")
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return len(value) >= 4
    return False


def _retrieved_chunk_content_kind(chunk: RetrievedChunk) -> str:
    value = chunk.metadata.get("content_kind")
    return value.strip().casefold() if isinstance(value, str) else ""


def _metadata_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _metadata_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _chunk_page_int(chunk: RetrievedChunk, key: str) -> int | None:
    """Search citation の page 値を metadata/top-level 両対応で読む。"""
    value = getattr(chunk, key, None)
    if value is None:
        value = chunk.metadata.get(key)
    return _metadata_int(value)


async def _cleanup_staging_resources(
    *,
    oracle: StagingOracleProtocol,
    storage: StagingObjectStorageProtocol,
    knowledge_base_id: str,
    document_ids: Sequence[str],
    object_uris: Sequence[str],
) -> dict[str, str]:
    status: dict[str, str] = {}
    for document_id in _unique_nonempty_strings(document_ids):
        try:
            status[f"document:{document_id}"] = (
                "deleted" if await oracle.delete_document(document_id) else "missing"
            )
        except Exception:
            status[f"document:{document_id}"] = "error"
    for object_uri in _unique_nonempty_strings(object_uris):
        try:
            status[f"object:{_hash_label(object_uri)}"] = (
                "deleted" if await storage.delete(object_uri) else "missing"
            )
        except Exception:
            status[f"object:{_hash_label(object_uri)}"] = "error"
    try:
        await oracle.archive_knowledge_base(knowledge_base_id)
    except Exception:
        status["knowledge_base"] = "error"
    else:
        status["knowledge_base"] = "archived"
    return status


def _artifact_paths_from_evidence(evidence: _StagingEvidence) -> tuple[str, ...]:
    """staging ingestion が生成した full/segment artifact path を cleanup 対象にする。"""
    paths: list[str] = []
    parser_artifacts = _mapping(evidence.extraction.get("parser_artifacts"))
    artifact_path = parser_artifacts.get("extraction_artifact_path")
    if isinstance(artifact_path, str):
        paths.append(artifact_path)
    for segment in (*evidence.segments, *evidence.retry_segments):
        if segment.artifact_path:
            paths.append(segment.artifact_path)
    return tuple(_unique_nonempty_strings(paths))


def _unique_nonempty_strings(values: Sequence[str]) -> list[str]:
    """順序を保って空文字と重複を除く。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        result.append(stripped)
    return result


def _requirements_by_case(
    requirements: Sequence[FileProcessingStagingRequirement],
) -> dict[str, list[FileProcessingStagingRequirement]]:
    grouped: dict[str, list[FileProcessingStagingRequirement]] = defaultdict(list)
    for requirement in requirements:
        grouped[requirement.case_id].append(requirement)
    return dict(grouped)


def _case_manifest_by_id(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, str | bytes | bytearray):
        return {}
    return {
        _case_id(case): case for case in raw_cases if isinstance(case, Mapping) and _case_id(case)
    }


def _fixture_root(manifest: Mapping[str, object], manifest_path: Path) -> Path:
    root = str(manifest.get("fixture_root") or ".")
    return (manifest_path.parent / root).resolve()


def _content_type(file_name: str) -> str:
    return mimetypes.guess_type(file_name)[0] or "application/octet-stream"


def _case_id(case: Mapping[str, object]) -> str:
    return str(case.get("id") or "")


def _staging_query(case: Mapping[str, object]) -> str:
    for key in ("staging_query", "expected_answer", "notes"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(case.get("id") or "file-processing golden")


def _has_content_kind(chunks: Sequence[DocumentChunkView], expected_kind: str) -> bool:
    if expected_kind == "text":
        return bool(chunks)
    return any(chunk.content_kind == expected_kind for chunk in chunks)


def _chunk_pages(chunks: Sequence[DocumentChunkView]) -> set[int]:
    pages: set[int] = set()
    for chunk in chunks:
        if chunk.page_start is None:
            continue
        page_end = chunk.page_end or chunk.page_start
        pages.update(range(chunk.page_start, page_end + 1))
    return pages


def _has_traceable_chunk(chunks: Sequence[DocumentChunkView]) -> bool:
    return any(_chunk_traceable(chunk) for chunk in chunks)


def _chunk_traceable(chunk: DocumentChunkView) -> bool:
    return bool(
        chunk.chunk_id
        and chunk.document_id
        and (chunk.element_ids or _chunk_has_valid_bbox(chunk) or chunk.section_path)
        and chunk.page_start is not None
    )


def _chunk_preview_addressable(chunk: DocumentChunkView, evidence: _StagingEvidence) -> bool:
    return bool(
        chunk.chunk_id
        and chunk.document_id
        and chunk.page_start is not None
        and _chunk_has_valid_bbox(chunk)
        and not _chunk_has_invalid_bbox_unit(chunk)
        and not _chunk_has_ambiguous_bbox_without_mode(chunk)
        and _chunk_bbox_rotation_violation(chunk, evidence) is None
        and _chunk_bbox_has_preview_scale(chunk, evidence)
        and _chunk_has_resolvable_element_lineage(chunk, evidence)
    )


def _chunk_has_valid_bbox(chunk: DocumentChunkView) -> bool:
    bbox = chunk.bbox
    if not bbox or len(bbox) < 4:
        return False
    x, y, right_or_width, bottom_or_height = bbox[:4]
    if not all(math.isfinite(value) for value in (x, y, right_or_width, bottom_or_height)):
        return False
    if x < 0 or y < 0:
        return False
    return not (right_or_width <= 0 or bottom_or_height <= 0)


def _chunk_bbox_has_preview_scale(chunk: DocumentChunkView, evidence: _StagingEvidence) -> bool:
    """frontend preview と同じく absolute bbox は page size が必要。"""
    if not chunk.bbox:
        return False
    unit = _chunk_bbox_unit(chunk)
    if unit in {"ratio", "percent"}:
        return True
    if unit is None and max(chunk.bbox[:4]) <= 100:
        return True
    if chunk.page_start is None:
        return False
    return _extraction_page_has_size(evidence.extraction, chunk.page_start)


def _chunk_has_ambiguous_bbox_without_mode(chunk: DocumentChunkView) -> bool:
    """非原点 bbox は xyxy / xywh の解釈差が出るため mode metadata を要求する。"""
    if not chunk.bbox or not _chunk_has_valid_bbox(chunk):
        return False
    if _chunk_bbox_coordinate_mode(chunk) is not None:
        return False
    x, y, right_or_width, bottom_or_height = chunk.bbox[:4]
    return bool((x > 0 or y > 0) and right_or_width > x and bottom_or_height > y)


def _chunk_bbox_coordinate_mode(chunk: DocumentChunkView) -> str | None:
    value = chunk.metadata.get("bbox_coordinate_mode")
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(",", "_")
    normalized = "_".join(part for part in normalized.split("_") if part)
    if normalized in {"xyxy", "x1_y1_x2_y2"}:
        return "xyxy"
    if normalized in {"xywh", "x_y_width_height", "left_top_width_height"}:
        return "xywh"
    return None


_BBOX_UNIT_METADATA_KEYS = (
    "bbox_unit",
    "bbox_coordinate_unit",
    "coordinate_unit",
    "unit",
)


def _chunk_bbox_unit(chunk: DocumentChunkView) -> str | None:
    value = _chunk_bbox_unit_value(chunk)
    if not isinstance(value, str):
        return None
    normalized = _normalize_bbox_unit_value(value)
    if normalized in {"ratio", "normalized", "relative", "fraction"}:
        return "ratio"
    if normalized in {"percent", "percentage", "%"}:
        return "percent"
    if normalized in {"absolute", "pixel", "pixels", "px", "point", "points", "pt"}:
        return "absolute"
    return None


def _chunk_has_invalid_bbox_unit(chunk: DocumentChunkView) -> bool:
    value = _chunk_bbox_unit_value(chunk)
    if not isinstance(value, str) or not value.strip():
        return False
    return _chunk_bbox_unit(chunk) is None


def _chunk_bbox_rotation_violation(
    chunk: DocumentChunkView,
    evidence: _StagingEvidence,
) -> str | None:
    if not chunk.bbox or chunk.page_start is None:
        return None
    page_rotation = _extraction_page_rotation(evidence.extraction, chunk.page_start)
    if page_rotation is not None and page_rotation < 0:
        return "bbox_page_rotation_invalid"
    metadata_rotation = _bbox_rotation_from_metadata(chunk.metadata)
    if metadata_rotation is not None and metadata_rotation < 0:
        return "bbox_page_rotation_invalid"
    if (
        page_rotation is not None
        and metadata_rotation is not None
        and metadata_rotation != page_rotation
    ):
        return "bbox_page_rotation_mismatch"
    return None


def _chunk_bbox_unit_value(chunk: DocumentChunkView) -> object:
    for key in _BBOX_UNIT_METADATA_KEYS:
        if key in chunk.metadata:
            return chunk.metadata[key]
    return None


def _normalize_bbox_unit_value(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized == "%":
        return normalized
    return "_".join(part for part in normalized.replace(" ", "_").split("_") if part)


def _normalize_answer(value: str) -> str:
    return "".join(value.casefold().split())


def _extraction_page_has_size(extraction: Mapping[str, object], page_number: int) -> bool:
    pages = extraction.get("pages")
    if not isinstance(pages, Sequence) or isinstance(pages, str | bytes | bytearray):
        return False
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        if _metadata_int(page.get("page_number")) != page_number:
            continue
        width = _metadata_float(page.get("width"))
        height = _metadata_float(page.get("height"))
        return width is not None and height is not None and width > 0 and height > 0
    return False


def _extraction_page_rotation(
    extraction: Mapping[str, object],
    page_number: int,
) -> int | None:
    pages = extraction.get("pages")
    if not isinstance(pages, Sequence) or isinstance(pages, str | bytes | bytearray):
        return None
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        if _metadata_int(page.get("page_number")) != page_number:
            continue
        if "rotation" not in page:
            return None
        return _normalize_page_rotation(page.get("rotation"))
    return None


def _bbox_rotation_from_metadata(metadata: Mapping[str, object]) -> int | None:
    for key in (
        "page_rotation",
        "bbox_page_rotation",
        "source_page_rotation",
        "rotation",
    ):
        if key in metadata:
            return _normalize_page_rotation(metadata.get(key))
    return None


def _normalize_page_rotation(value: object) -> int:
    number = _metadata_float(value)
    if number is None or not number.is_integer():
        return -1
    normalized = int(number) % 360
    return normalized if normalized in {0, 90, 180, 270} else -1


def _extraction_preview_addressability_violation(
    evidence: _StagingEvidence,
) -> str | None:
    """実 ingestion の element/table cell/asset bbox も preview へ定位できるか検証する。"""
    for target in _extraction_preview_bbox_targets(evidence.extraction):
        violation = _extraction_preview_target_violation(target, evidence)
        if violation is not None:
            return violation
    return None


def _extraction_preview_target_count(evidence: _StagingEvidence) -> int:
    return len(_extraction_preview_bbox_targets(evidence.extraction))


def _extraction_preview_addressable_target_count(evidence: _StagingEvidence) -> int:
    return sum(
        1
        for target in _extraction_preview_bbox_targets(evidence.extraction)
        if _extraction_preview_target_violation(target, evidence) is None
    )


def _extraction_preview_bbox_targets(
    extraction: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    targets: list[Mapping[str, object]] = []
    elements = extraction.get("elements")
    if isinstance(elements, Sequence) and not isinstance(elements, str | bytes | bytearray):
        for element in elements:
            element_map = _mapping(element)
            bbox = _bbox_numbers(element_map.get("bbox"))
            if bbox is None:
                continue
            targets.append(
                {
                    "kind": "element",
                    "page_number": element_map.get("page_number"),
                    "bbox": bbox,
                    "metadata": _mapping(element_map.get("metadata")),
                }
            )
    tables = extraction.get("tables")
    if isinstance(tables, Sequence) and not isinstance(tables, str | bytes | bytearray):
        for table in tables:
            table_map = _mapping(table)
            table_page_number = table_map.get("page_number")
            cells = table_map.get("cells")
            if not isinstance(cells, Sequence) or isinstance(cells, str | bytes | bytearray):
                continue
            for cell in cells:
                cell_map = _mapping(cell)
                bbox = _bbox_numbers(cell_map.get("bbox"))
                if bbox is None:
                    continue
                targets.append(
                    {
                        "kind": "table_cell",
                        "page_number": cell_map.get("page_number", table_page_number),
                        "bbox": bbox,
                        "metadata": _mapping(cell_map.get("metadata")),
                    }
                )
    assets = extraction.get("assets")
    if isinstance(assets, Sequence) and not isinstance(assets, str | bytes | bytearray):
        for asset in assets:
            asset_map = _mapping(asset)
            bbox = _bbox_numbers(asset_map.get("bbox"))
            if bbox is None:
                continue
            targets.append(
                {
                    "kind": "asset",
                    "page_number": asset_map.get("page_number"),
                    "bbox": bbox,
                    "metadata": _mapping(asset_map.get("metadata")),
                }
            )
    return tuple(targets)


def _extraction_preview_target_violation(
    target: Mapping[str, object],
    evidence: _StagingEvidence,
) -> str | None:
    page_number = _metadata_int(target.get("page_number"))
    if page_number is None or page_number <= 0:
        return "bbox_page_missing"
    explicit_pages = _extraction_page_numbers(evidence.extraction)
    if explicit_pages and page_number not in explicit_pages:
        return "bbox_page_unresolved"
    page_rotation = _extraction_page_rotation(evidence.extraction, page_number)
    if page_rotation is not None and page_rotation < 0:
        return "bbox_page_rotation_invalid"
    metadata_rotation = _bbox_rotation_from_metadata(_mapping(target.get("metadata")))
    if metadata_rotation is not None and metadata_rotation < 0:
        return "bbox_page_rotation_invalid"
    if (
        page_rotation is not None
        and metadata_rotation is not None
        and metadata_rotation != page_rotation
    ):
        return "bbox_page_rotation_mismatch"
    bbox = _bbox_numbers(target.get("bbox"))
    if bbox is None:
        return "bbox_missing"
    if not _bbox_values_valid(bbox):
        return "bbox_invalid"
    unit = _bbox_unit_from_metadata(_mapping(target.get("metadata")))
    if unit is None:
        unit = _inferred_bbox_unit(bbox)
    elif unit == "invalid":
        return "bbox_unit_invalid"
    if unit == "absolute" and not _extraction_page_has_size(evidence.extraction, page_number):
        return "bbox_absolute_page_size_missing"
    return None


def _extraction_page_numbers(extraction: Mapping[str, object]) -> set[int]:
    pages = extraction.get("pages")
    if not isinstance(pages, Sequence) or isinstance(pages, str | bytes | bytearray):
        return set()
    page_numbers: set[int] = set()
    for page in pages:
        page_number = _metadata_int(_mapping(page).get("page_number"))
        if page_number is not None and page_number > 0:
            page_numbers.add(page_number)
    return page_numbers


def _bbox_numbers(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return None
    if len(value) < 4:
        return None
    numbers = tuple(_metadata_float(item) for item in value[:4])
    if any(number is None for number in numbers):
        return None
    x, y, right_or_width, bottom_or_height = numbers
    if x is None or y is None or right_or_width is None or bottom_or_height is None:
        return None
    return (x, y, right_or_width, bottom_or_height)


def _bbox_values_valid(bbox: Sequence[float]) -> bool:
    if len(bbox) < 4:
        return False
    x, y, right_or_width, bottom_or_height = bbox[:4]
    if not all(math.isfinite(value) for value in (x, y, right_or_width, bottom_or_height)):
        return False
    if x < 0 or y < 0:
        return False
    return not (right_or_width <= 0 or bottom_or_height <= 0)


def _bbox_unit_from_metadata(metadata: Mapping[str, object]) -> str | None:
    for key in _BBOX_UNIT_METADATA_KEYS:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _normalize_bbox_unit_value(value)
        if normalized in {"ratio", "normalized", "relative", "fraction"}:
            return "ratio"
        if normalized in {"percent", "percentage", "%"}:
            return "percent"
        if normalized in {"absolute", "pixel", "pixels", "px", "point", "points", "pt"}:
            return "absolute"
        return "invalid"
    return None


def _inferred_bbox_unit(value: Sequence[float]) -> str:
    max_value = max(abs(number) for number in value[:4])
    if max_value <= 1:
        return "ratio"
    if max_value <= 100:
        return "percent"
    return "absolute"


def _chunk_has_resolvable_element_lineage(
    chunk: DocumentChunkView,
    evidence: _StagingEvidence,
) -> bool:
    chunk_element_ids = _chunk_element_ids(chunk)
    if not chunk_element_ids:
        return False
    extraction_element_ids = _extraction_element_ids(evidence.extraction)
    return bool(extraction_element_ids and (set(chunk_element_ids) & extraction_element_ids))


def _chunk_element_ids(chunk: DocumentChunkView) -> tuple[str, ...]:
    return tuple(item.strip() for item in chunk.element_ids if item.strip())


def _extraction_element_ids(extraction: Mapping[str, object]) -> set[str]:
    element_ids: set[str] = set()
    elements = extraction.get("elements")
    if isinstance(elements, Sequence) and not isinstance(elements, str | bytes | bytearray):
        for index, element in enumerate(elements):
            if not isinstance(element, Mapping):
                continue
            element_id = _optional_str(element.get("element_id"))
            if not element_id:
                element_id = _optional_str(_mapping(element.get("metadata")).get("element_id"))
            if not element_id:
                order = _metadata_int(element.get("order"))
                element_id = f"el-{order:04d}" if order is not None else f"el-{index:04d}"
            element_ids.add(element_id)
    pages = extraction.get("pages")
    if isinstance(pages, Sequence) and not isinstance(pages, str | bytes | bytearray):
        for page in pages:
            if not isinstance(page, Mapping):
                continue
            for element_id in _string_sequence(page.get("element_ids")):
                element_ids.add(element_id)
    return element_ids


def _string_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _int_set(value: object) -> set[int]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return set()
    result: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            result.add(item)
        elif isinstance(item, str) and item.strip().isdigit():
            result.add(int(item.strip()))
    return result


def _section_set(value: object) -> set[str]:
    if isinstance(value, str):
        section = _normalize_section_label(value)
        return {section} if section else set()
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
        return set()
    return {section for item in value if (section := _normalize_section_label(item))}


def _string_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
        return set()
    return {item for item in value if isinstance(item, str)}


def _normalize_section_label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s*(?:>|/|›|»)\s*", " > ", value.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().casefold()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float) and value.is_integer():
        parsed = int(value)
        return parsed if parsed > 0 else 0
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _hash_label(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _storage_uri_scheme(value: object) -> str:
    if not isinstance(value, str):
        return "missing"
    stripped = value.strip()
    if not stripped:
        return "missing"
    if "://" not in stripped:
        return "key"
    return stripped.split("://", 1)[0].casefold() or "unknown"
