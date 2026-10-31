"""file-processing staging runner のテスト。"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from app.config import Settings
from app.rag import file_processing_staging_cli, parser_adapter_readiness
from app.rag.file_processing_evaluation import (
    REQUIRED_FILE_PROCESSING_SOURCE_KINDS,
    FileProcessingMetricThresholdResult,
)
from app.rag.file_processing_staging import (
    FileProcessingStagingCaseResult,
    FileProcessingStagingGateResult,
    FileProcessingStagingReport,
    FileProcessingStagingRuntimeCheckResult,
    run_file_processing_staging_checks,
)
from app.rag.parser_adapter_contract import (
    ParserAdapterCompatibilityCase,
    ParserAdapterCompatibilityMatrix,
    ParserAdapterFixtureSpec,
)
from app.rag.staging_smoke import SmokePreflightResult
from app.schemas.document import (
    DocumentChunkView,
    DocumentDetail,
    FileStatus,
    IngestionSegment,
    SourceProfile,
)
from app.schemas.knowledge_base import KnowledgeBaseDetail, KnowledgeBaseStatus
from app.schemas.search import (
    RetrievedChunk,
    SearchDiagnostics,
    SearchMode,
    SearchRequest,
    SearchResponse,
)
from tests.support import shared_thin_wallet_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _parser_service_health_follows_package_info(monkeypatch: MonkeyPatch) -> None:
    """parser サービスの /health を、各テストが差し替える `_package_info` の結果で代用する。

    staging CLI は adapter の可用性を parser サービスの /health で判定する(#366)。テストは
    実サービス(127.0.0.1:18020 など)へ問い合わせず、`_package_info` の差し替えを
    「サービスの応答」として使う。
    """

    def probe(
        _settings: Settings,
        backend: parser_adapter_readiness.ParserAdapterName,
    ) -> tuple[bool, str | None, str | None]:
        spec = parser_adapter_readiness.ADAPTER_PACKAGES[backend]
        return parser_adapter_readiness._package_info(spec.import_name, spec.distribution_names)

    monkeypatch.setattr(parser_adapter_readiness, "_probe_service_health", probe)


async def test_file_processing_staging_runner_closes_pending_gates_with_evidence() -> None:
    """staging runner は pending requirements を実 evidence で gate 判定する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        cleanup=True,
        run_id="run-1",
    )

    assert report.passed is True
    assert report.case_count == 8
    assert report.gate_count == 29
    assert report.failure_count == 0
    assert report.runtime_checks[0].check == "extraction_artifact_cache_roundtrip"
    assert report.runtime_checks[0].status == "ok"
    assert report.runtime_checks[0].evidence is not None
    assert report.runtime_checks[0].evidence["cleanup"] == "deleted"
    assert report.runtime_checks[0].evidence["object_uri_scheme"] == "oci"
    assert "contains_document_text" not in str(report.runtime_checks[0].evidence)
    assert report.metrics["gate_pass_rate"] == 1.0
    assert report.metrics["parser_fallback_rate"] == 0.0
    assert report.metrics["extraction_page_coverage"] >= 0.8
    assert report.metrics["low_confidence_document_rate"] == 0.0
    assert report.metrics["failed_segment_rate"] <= 0.25
    assert report.metrics["citation_traceability_coverage"] == 1.0
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["bbox_coordinate_validity_coverage"] == 1.0
    assert report.metrics["chunk_block_integrity"] == 1.0
    assert report.metrics["reading_order_consistency"] == 1.0
    assert report.metrics["structural_section_coverage"] == 1.0
    assert report.metrics["dependency_context_recall"] == 1.0
    assert report.metrics["table_structure_fidelity"] == 1.0
    assert report.metrics["table_cell_lineage_coverage"] == 1.0
    assert report.metrics["table_row_tree_fidelity"] == 1.0
    assert report.metrics["visual_chunk_metadata_completeness"] == 1.0
    assert report.metrics["chunk_size_compliance"] == 1.0
    assert report.metrics["chunk_contextual_coherence"] == 1.0
    assert report.metrics["cross_page_table_continuity_coverage"] == 1.0
    assert report.metrics["ingestion_quality_report_completeness"] == 1.0
    assert report.metrics["parser_warning_taxonomy_coverage"] == 1.0
    assert report.metrics["parser_routing_accuracy"] == 1.0
    assert report.metrics["source_kind_coverage"] == 1.0
    assert report.metrics["backend_source_kind_coverage"] == 1.0
    assert report.metrics["adapter_contract_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] == 1.0
    assert report.metrics["element_lineage_coverage"] == 1.0
    assert report.metrics["retrieval_recall"] == 1.0
    assert report.metrics["groundedness"] == 1.0
    assert report.metrics["ingestion_p95_ms"] >= 0.0
    assert report.metrics["page_hit_accuracy"] == 1.0
    assert report.metrics["table_qa_accuracy"] == 1.0
    artifact_reuse_evidence = report.metric_evidence["segment_artifact_reuse"]
    assert isinstance(artifact_reuse_evidence, Mapping)
    assert artifact_reuse_evidence["retry_case_count"] == 1
    assert artifact_reuse_evidence["initial_failed_segment_count"] == 1
    assert artifact_reuse_evidence["initial_successful_segment_artifact_count"] == 1
    assert artifact_reuse_evidence["retained_successful_segment_artifact_count"] == 1
    assert artifact_reuse_evidence["reprocessed_successful_segment_count"] == 0
    assert artifact_reuse_evidence["failed_segment_retried_count"] == 1
    assert artifact_reuse_evidence["full_artifact_identity_present_case_count"] >= 1
    table_cell_lineage_evidence = report.metric_evidence["table_cell_lineage"]
    assert isinstance(table_cell_lineage_evidence, Mapping)
    assert table_cell_lineage_evidence["expected_case_count"] >= 1
    assert table_cell_lineage_evidence["expected_ref_count"] >= 1
    assert (
        table_cell_lineage_evidence["resolved_ref_count"]
        == (table_cell_lineage_evidence["expected_ref_count"])
    )
    assert (
        table_cell_lineage_evidence["covered_ref_count"]
        == (table_cell_lineage_evidence["expected_ref_count"])
    )
    assert (
        table_cell_lineage_evidence["lineage_ref_count"]
        == (table_cell_lineage_evidence["expected_ref_count"])
    )
    assert table_cell_lineage_evidence["unresolved_ref_count"] == 0
    assert table_cell_lineage_evidence["uncovered_ref_count"] == 0
    assert table_cell_lineage_evidence["all_expected_refs_resolved"] is True
    assert table_cell_lineage_evidence["all_expected_refs_covered"] is True
    assert table_cell_lineage_evidence["coverage"] == 1.0
    preview_addressability_evidence = report.metric_evidence["preview_addressability"]
    assert isinstance(preview_addressability_evidence, Mapping)
    assert preview_addressability_evidence["preview_gate_case_count"] >= 1
    assert preview_addressability_evidence["target_count"] >= 1
    assert (
        preview_addressability_evidence["addressable_target_count"]
        == (preview_addressability_evidence["target_count"])
    )
    assert (
        preview_addressability_evidence["chunk_bbox_count"]
        == (preview_addressability_evidence["chunk_target_count"])
    )
    assert preview_addressability_evidence["unaddressable_target_count"] == 0
    assert preview_addressability_evidence["all_targets_addressable"] is True
    assert preview_addressability_evidence["all_chunks_have_bbox"] is True
    assert preview_addressability_evidence["coverage"] == 1.0
    threshold_by_metric = {result.metric: result for result in report.threshold_results}
    assert threshold_by_metric["retrieval_recall"].status == "passed"
    assert threshold_by_metric["table_qa_accuracy"].status == "passed"
    assert threshold_by_metric["page_hit_accuracy"].status == "passed"
    assert threshold_by_metric["parser_fallback_rate"].status == "passed"
    assert threshold_by_metric["extraction_page_coverage"].status == "passed"
    assert threshold_by_metric["low_confidence_document_rate"].status == "passed"
    assert threshold_by_metric["failed_segment_rate"].status == "passed"
    assert threshold_by_metric["bbox_coordinate_validity_coverage"].status == "passed"
    assert threshold_by_metric["reading_order_consistency"].status == "passed"
    assert threshold_by_metric["structural_section_coverage"].status == "passed"
    assert threshold_by_metric["dependency_context_recall"].status == "passed"
    assert threshold_by_metric["table_structure_fidelity"].status == "passed"
    assert threshold_by_metric["table_cell_lineage_coverage"].status == "passed"
    assert threshold_by_metric["table_row_tree_fidelity"].status == "passed"
    assert threshold_by_metric["visual_chunk_metadata_completeness"].status == "passed"
    assert threshold_by_metric["chunk_size_compliance"].status == "passed"
    assert threshold_by_metric["chunk_contextual_coherence"].status == "passed"
    assert threshold_by_metric["cross_page_table_continuity_coverage"].status == "passed"
    assert threshold_by_metric["ingestion_quality_report_completeness"].status == "passed"
    assert threshold_by_metric["parser_warning_taxonomy_coverage"].status == "passed"
    assert threshold_by_metric["parser_routing_accuracy"].status == "passed"
    assert threshold_by_metric["source_kind_coverage"].status == "passed"
    assert threshold_by_metric["backend_source_kind_coverage"].status == "passed"
    assert threshold_by_metric["adapter_contract_coverage"].status == "passed"
    assert report.cleanup is not None
    assert report.cleanup["knowledge_base"] == "archived"
    assert all(result.passed for result in report.case_results)
    assert all(
        "raw_text" not in str(gate.evidence)
        for case_result in report.case_results
        for gate in case_result.gate_results
    )
    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )
    assert payload["promotion_ready"] is True
    assert payload["promotion_blockers"] == []
    assert payload["parser_adapter_scorecard"]["recommended_backend"] == "local"
    assert payload["parser_adapter_scorecard"]["metrics_source"] == "file_processing_staging"
    assert payload["parser_adapter_scorecard"]["metrics_applied_to"] == "local"
    assert payload["parser_adapter_contract"]["passed"] is True
    assert payload["parser_adapter_contract"]["case_count"] > 0
    contract_text = json.dumps(payload["parser_adapter_contract"], ensure_ascii=False)
    assert "raw_text" not in contract_text
    assert "policy-ja.pdf" not in contract_text
    assert "file-processing-fixtures" not in contract_text
    adapter_golden_gate = payload["adapter_golden_gate"]
    assert adapter_golden_gate["passed"] is True
    assert adapter_golden_gate["metrics_source"] == "file_processing_staging"
    assert adapter_golden_gate["selected_backend"] == "local"
    assert set(adapter_golden_gate["required_source_kinds"]) == {
        "pdf",
        "office",
        "html",
        "email",
        "image",
    }
    assert adapter_golden_gate["missing_source_kinds"] == []
    assert adapter_golden_gate["missing_metric_names"] == []
    assert adapter_golden_gate["failed_metric_checks"] == []
    assert adapter_golden_gate["metric_values"]["table_qa_accuracy"] == 1.0
    assert adapter_golden_gate["metric_values"]["page_hit_accuracy"] == 1.0
    assert adapter_golden_gate["metric_values"]["parser_fallback_rate"] == 0.0
    assert adapter_golden_gate["contract_passed"] is True
    assert "raw_text" not in str(adapter_golden_gate)
    contract_summary = payload["adapter_contract_matrix_summary"]
    assert contract_summary["passed"] is True
    assert contract_summary["case_count"] == payload["parser_adapter_contract"]["case_count"]
    assert contract_summary["backend_source_status"]
    assert adapter_golden_gate["contract_passed_case_refs"] == contract_summary["passed_case_refs"]
    assert (
        adapter_golden_gate["contract_backend_passed_case_refs"]
        == contract_summary["backend_passed_case_refs"]
    )
    assert "raw_text" not in str(contract_summary)
    backend_source_matrix = payload["backend_source_kind_matrix"]
    assert backend_source_matrix["value"] == 1.0
    assert set(backend_source_matrix["covered_source_kinds"]) >= (
        REQUIRED_FILE_PROCESSING_SOURCE_KINDS
    )
    assert backend_source_matrix["missing_source_kinds"] == []
    assert backend_source_matrix["backend_source_kinds"]
    assert "raw_text" not in str(backend_source_matrix)
    route_by_kind = {
        route["source_kind"]: route for route in payload["parser_adapter_source_routes"]
    }
    assert route_by_kind["pdf"]["candidate_order"] == ("docling", "unstructured")
    assert route_by_kind["pdf"]["attempted_order"] == ()
    assert route_by_kind["pdf"]["selected_backend"] == "local"
    assert route_by_kind["email"]["candidate_order"] == ("unstructured",)
    assert "audio" not in route_by_kind
    assert route_by_kind["text"]["candidate_order"] == ()
    assert "local_parser_preferred_for_source" in route_by_kind["text"]["reason_codes"]
    assert payload["chunk_template_scorecard"]["promotion_blocking"] is False
    assert payload["chunk_template_scorecard"]["recommended_template"] is not None
    assert payload["staging_policy"]["required_runtime_checks"] == [
        "extraction_artifact_cache_roundtrip"
    ]
    assert payload["staging_dataset_policy"]["configured"] is False
    assert payload["staging_dataset_policy"]["promotion_ready"] is True
    payload_artifact_reuse_evidence = payload["metric_evidence"]["segment_artifact_reuse"]
    assert payload_artifact_reuse_evidence["retry_case_count"] == 1
    assert payload_artifact_reuse_evidence["retained_successful_segment_artifact_count"] == 1
    payload_table_cell_lineage = payload["metric_evidence"]["table_cell_lineage"]
    assert payload_table_cell_lineage["expected_ref_count"] >= 1
    assert (
        payload_table_cell_lineage["resolved_ref_count"]
        == (payload_table_cell_lineage["expected_ref_count"])
    )
    assert (
        payload_table_cell_lineage["covered_ref_count"]
        == (payload_table_cell_lineage["expected_ref_count"])
    )
    payload_preview_addressability = payload["metric_evidence"]["preview_addressability"]
    assert payload_preview_addressability["target_count"] >= 1
    assert (
        payload_preview_addressability["addressable_target_count"]
        == (payload_preview_addressability["target_count"])
    )
    assert payload_preview_addressability["unaddressable_target_count"] == 0
    artifact_chain = payload["object_storage_artifact_chain"]
    assert artifact_chain["passed"] is True
    assert artifact_chain["roundtrip_check"] == "ok"
    assert artifact_chain["roundtrip_object_uri_scheme"] == "oci"
    assert artifact_chain["full_artifact_cached_case_count"] >= 1
    assert (
        artifact_chain["full_artifact_oci_case_count"]
        == (artifact_chain["full_artifact_cached_case_count"])
    )
    assert artifact_chain["full_artifact_identity_present_case_count"] >= 1
    assert artifact_chain["full_artifact_readable_case_count"] >= 1
    assert artifact_chain["full_artifact_identity_verified_case_count"] >= 1
    assert artifact_chain["segment_artifact_expected_count"] >= 1
    assert (
        artifact_chain["segment_artifact_oci_uri_count"]
        == (artifact_chain["segment_artifact_expected_count"])
    )
    assert artifact_chain["segment_artifact_non_oci_uri_count"] == 0
    assert (
        artifact_chain["segment_artifact_readable_count"]
        == (artifact_chain["segment_artifact_expected_count"])
    )
    assert (
        artifact_chain["segment_artifact_identity_verified_count"]
        == (artifact_chain["segment_artifact_expected_count"])
    )
    assert artifact_chain["artifact_integrity_error_count"] == 0
    assert artifact_chain["retained_successful_segment_artifact_count"] == 1
    assert artifact_chain["rewritten_successful_segment_artifact_count"] == 0
    assert artifact_chain["segment_cache_miss_count"] == 0
    assert artifact_chain["audit_payload_redaction_enforced"] is True
    trend = file_processing_staging_cli._trend_payload(
        payload,
        kind="file_processing_staging",
    )
    assert trend["kind"] == "file_processing_staging"
    assert len(trend["result_sha256"]) == 64
    assert trend["passed"] is True
    assert trend["promotion_ready"] is True
    assert trend["case_count"] == 8
    assert trend["gate_count"] == 29
    assert trend["failure_count"] == 0
    assert trend["runtime_check_status_counts"] == {"ok": 1}
    assert trend["threshold_status_counts"]["passed"] > 0
    assert trend["metrics"]["retrieval_recall"] == 1.0
    assert trend["metrics"]["table_qa_accuracy"] == 1.0
    assert trend["metrics"]["page_hit_accuracy"] == 1.0
    assert trend["metrics"]["bbox_coordinate_validity_coverage"] == 1.0
    assert trend["metrics"]["preview_addressability_coverage"] == 1.0
    assert trend["parser_adapter_contract"]["passed"] is True
    assert trend["adapter_golden_gate"]["passed"] is True
    assert trend["adapter_golden_gate"]["missing_source_kinds"] == []
    assert trend["adapter_golden_gate"]["failed_metric_count"] == 0
    assert trend["adapter_golden_gate"]["contract_passed_case_refs"] == trend[
        "parser_adapter_contract"
    ].get("passed_case_refs", [])
    assert trend["adapter_golden_gate"]["contract_backend_passed_case_refs"] == trend[
        "parser_adapter_contract"
    ].get("backend_passed_case_refs", {})
    trend_route_by_kind = {
        route["source_kind"]: route for route in trend["parser_adapter_source_routes"]
    }
    assert trend_route_by_kind["pdf"]["candidate_order"] == ["docling", "unstructured"]
    assert trend_route_by_kind["pdf"]["selected_backend"] == "local"
    assert trend_route_by_kind["email"]["candidate_order"] == ["unstructured"]
    assert trend["parser_adapter_scorecard"]["recommended_backend"] == "local"
    assert trend["backend_source_kind_matrix"]["missing_source_kinds"] == []
    assert trend["staging_dataset_policy"]["configured"] is False
    assert trend["object_storage_artifact_chain"]["passed"] is True
    assert trend["object_storage_artifact_chain"]["roundtrip_check"] == "ok"
    assert trend["object_storage_artifact_chain"]["full_artifact_identity_present_case_count"] >= 1
    assert trend["object_storage_artifact_chain"]["full_artifact_identity_verified_case_count"] >= 1
    assert trend["object_storage_artifact_chain"]["artifact_integrity_error_count"] == 0
    assert trend["segment_artifact_reuse"]["retry_case_count"] == 1
    assert trend["segment_artifact_reuse"]["retained_successful_segment_artifact_count"] == 1
    assert trend["segment_artifact_reuse"]["artifact_integrity_error_count"] == 0
    assert trend["table_cell_lineage"]["expected_ref_count"] >= 1
    assert (
        trend["table_cell_lineage"]["resolved_ref_count"]
        == (trend["table_cell_lineage"]["expected_ref_count"])
    )
    assert (
        trend["table_cell_lineage"]["covered_ref_count"]
        == (trend["table_cell_lineage"]["expected_ref_count"])
    )
    assert trend["table_cell_lineage"]["unresolved_ref_count"] == 0
    assert trend["table_cell_lineage"]["coverage"] == 1.0
    assert trend["preview_addressability"]["target_count"] >= 1
    assert (
        trend["preview_addressability"]["addressable_target_count"]
        == (trend["preview_addressability"]["target_count"])
    )
    assert trend["preview_addressability"]["unaddressable_target_count"] == 0
    assert trend["preview_addressability"]["coverage"] == 1.0
    assert "case_results" not in trend
    assert "gate_results" not in json.dumps(trend, ensure_ascii=False)
    assert "raw_text" not in json.dumps(trend, ensure_ascii=False)

    by_case = {result.case_id: result for result in report.case_results}
    duplicate = by_case["duplicate-file-canonical-kb"]
    assert any(
        gate.suggested_gate == "duplicate_kb_membership_gate" for gate in duplicate.gate_results
    )
    corrupted = by_case["corrupted-file-partial-failure"]
    segment_reuse_gate = next(
        gate
        for gate in corrupted.gate_results
        if gate.suggested_gate == "segment_artifact_reuse_gate"
    )
    assert segment_reuse_gate.evidence is not None
    assert segment_reuse_gate.evidence["retry_initial_failed_segment_count"] == 1
    assert segment_reuse_gate.evidence["retry_initial_successful_segment_artifact_count"] == 1
    assert segment_reuse_gate.evidence["retry_retained_successful_segment_artifact_count"] == 1
    assert segment_reuse_gate.evidence["retry_reprocessed_successful_segment_count"] == 0
    assert segment_reuse_gate.evidence["retry_failed_segment_retried_count"] == 1
    payload_corrupted = next(
        result
        for result in payload["case_results"]
        if result["case_id"] == "corrupted-file-partial-failure"
    )
    payload_segment_gate = next(
        gate
        for gate in payload_corrupted["gate_results"]
        if gate["suggested_gate"] == "segment_artifact_reuse_gate"
    )
    assert payload_segment_gate["evidence"]["retry_failed_segment_retried_count"] == 1
    html = by_case["html-semantic-blocks"]
    assert any(
        gate.suggested_gate == "dependency_lineage_search_gate" for gate in html.gate_results
    )
    assert any(
        gate.suggested_gate == "dependency_context_recall_gate" for gate in html.gate_results
    )
    assert any(
        gate.suggested_gate == "structural_section_search_gate" for gate in html.gate_results
    )
    assert any(
        deleted.startswith("oci://namespace/bucket/artifacts/extractions/staging-preflight/run-1/")
        for deleted in storage.deleted
    )
    assert any(
        deleted.startswith("oci://namespace/bucket/artifacts/extractions/doc-")
        and "/full.json" in deleted
        for deleted in storage.deleted
    )
    assert any(
        deleted.startswith("oci://namespace/bucket/artifacts/extractions/doc-")
        and "/segments/" in deleted
        for deleted in storage.deleted
    )
    assert any("/artifacts/extractions/doc-" in fetched for fetched in storage.gets)


def test_file_processing_staging_payload_redacts_raw_document_evidence() -> None:
    """staging artifact は gate/runtime evidence に混入した OCR 原文を出力しない。"""
    secret = "社外秘 OCR 原文 ABC-123"
    report = FileProcessingStagingReport(
        run_id="redaction-check",
        knowledge_base_id="kb-1",
        runtime_checks=(
            FileProcessingStagingRuntimeCheckResult(
                check="extraction_artifact_cache_roundtrip",
                status="ok",
                evidence={
                    "object_ref_hash": "artifact:abc123",
                    "payload_bytes": 48,
                    "raw_text": secret,
                },
            ),
        ),
        case_results=(
            FileProcessingStagingCaseResult(
                case_id="sensitive-pdf",
                scenario="scanned_pdf",
                fixture="sensitive.pdf",
                document_id="doc-1",
                status="INDEXED",
                chunk_count=1,
                segment_count=1,
                cleanup={"document": "pending"},
                gate_results=(
                    FileProcessingStagingGateResult(
                        case_id="sensitive-pdf",
                        scenario="scanned_pdf",
                        check="bbox_citation",
                        suggested_gate="preview_bbox_citation_gate",
                        passed=True,
                        evidence={
                            "source_kind": "pdf",
                            "chunk_count": 1,
                            "raw_text": secret,
                            "ocr_text": secret,
                            "answer": secret,
                            "leak": secret,
                        },
                    ),
                ),
            ),
        ),
        metric_evidence={
            "segment_artifact_reuse": {
                "full_artifact_cached_case_count": 1,
                "full_artifact_identity_present_case_count": 1,
                "full_artifact_readable_case_count": 1,
                "full_artifact_identity_verified_case_count": 1,
                "segment_artifact_expected_count": 0,
                "segment_artifact_readable_count": 0,
                "segment_artifact_identity_verified_count": 0,
                "artifact_integrity_error_count": 0,
                "segment_cache_miss_count": 0,
                "rewritten_successful_segment_artifact_count": 0,
            }
        },
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest={"cases": []},
        settings=Settings(),
    )
    payload_text = json.dumps(payload, ensure_ascii=False)

    assert secret not in payload_text
    assert "raw_text" not in payload_text
    assert "ocr_text" not in payload_text
    assert "answer" not in payload_text
    assert payload["case_results"][0]["gate_results"][0]["evidence"] == {
        "source_kind": "pdf",
        "chunk_count": 1,
    }
    assert payload["runtime_checks"][0]["evidence"] == {
        "object_ref_hash": "artifact:abc123",
        "payload_bytes": 48,
    }
    assert payload["object_storage_artifact_chain"]["passed"] is False
    assert payload["object_storage_artifact_chain"]["audit_payload_redaction_enforced"] is False
    assert payload["object_storage_artifact_chain"]["sensitive_evidence_key_detected"] is True
    assert (
        "object_storage_audit_payload_not_redacted"
        in payload["object_storage_artifact_chain"]["blocker_codes"]
    )


def test_file_processing_staging_trend_keeps_adapter_package_version_evidence() -> None:
    """staging trend は adapter package/version drift を比較できる証跡を残す。"""
    trend = file_processing_staging_cli._trend_payload(
        {
            "passed": True,
            "promotion_ready": True,
            "case_count": 1,
            "gate_count": 1,
            "failure_count": 0,
            "metrics": {"adapter_contract_coverage": 1.0},
            "threshold_results": [],
            "promotion_blockers": [],
            "runtime_checks": [],
            "parser_adapter_contract_mode": "strict",
            "adapter_contract_matrix_summary": {
                "passed": True,
                "case_count": 2,
                "blocking_failure_count": 0,
                "passed_case_refs": ["case:contract-docling", "case:contract-unstructured"],
                "backend_passed_case_refs": {
                    "docling": ["case:contract-docling"],
                    "unstructured": ["case:contract-unstructured"],
                },
                "blocking_failure_case_refs": [],
            },
            "parser_adapter_contract": {
                "cases": [
                    {
                        "backend": "docling",
                        "adapter_import_name": "docling",
                        "adapter_distribution_name": "docling",
                        "adapter_package_version": "2.103.0",
                    },
                    {
                        "backend": "unstructured",
                        "adapter_import_name": "unstructured",
                        "adapter_distribution_name": "unstructured",
                        "adapter_package_version": "0.27.8",
                    },
                ]
            },
        },
        kind="file_processing_staging",
    )

    assert trend["parser_adapter_contract"]["adapter_package_version_pairs"] == [
        "docling|docling|2.103.0",
        "unstructured|unstructured|0.27.8",
    ]
    assert trend["parser_adapter_contract"]["passed_case_refs"] == [
        "case:contract-docling",
        "case:contract-unstructured",
    ]
    assert trend["parser_adapter_contract"]["backend_passed_case_refs"] == {
        "docling": ["case:contract-docling"],
        "unstructured": ["case:contract-unstructured"],
    }
    assert "raw_text" not in json.dumps(trend, ensure_ascii=False)


async def test_staging_blocks_promotion_when_required_artifact_cache_is_skipped() -> None:
    """artifact cache を無効にした staging は実行できても production promotion では止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="artifact-cache-disabled",
        artifact_cache_enabled=False,
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(),
    )

    assert report.passed is True
    assert report.runtime_checks[0].check == "extraction_artifact_cache_roundtrip"
    assert report.runtime_checks[0].status == "skipped"
    assert payload["promotion_ready"] is False
    assert {
        "code": "required_runtime_check_not_ok",
        "check": "extraction_artifact_cache_roundtrip",
        "status": "skipped",
    } in payload["promotion_blockers"]
    assert payload["object_storage_artifact_chain"]["passed"] is False
    assert (
        "object_storage_artifact_roundtrip_not_ok"
        in (payload["object_storage_artifact_chain"]["blocker_codes"])
    )
    assert any(
        blocker["code"] == "object_storage_artifact_chain_failed"
        for blocker in payload["promotion_blockers"]
    )


async def test_file_processing_staging_runner_fails_on_unreadable_artifact_cache() -> None:
    """artifact cache が読めない staging 環境では golden case 実行前に止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeMismatchedArtifactStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="artifact-readback-fail",
    )

    assert report.passed is False
    assert report.case_count == 0
    assert report.gate_count == 0
    assert report.failure_count == 1
    assert report.knowledge_base_id is None
    assert len(report.runtime_checks) == 1
    assert report.runtime_checks[0].status == "failed"
    assert report.runtime_checks[0].failure_code == "artifact_cache_probe_readback_mismatch"
    assert report.runtime_checks[0].evidence is not None
    assert report.runtime_checks[0].evidence["cleanup"] == "deleted"
    assert "POLICY" not in str(report.runtime_checks[0].evidence)


async def test_staging_blocks_promotion_when_extraction_artifact_is_unreadable() -> None:
    """ingestion 後 artifact が Object Storage から読めない場合は promotion を止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeUnreadableArtifactStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="artifact-unreadable",
    )
    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        manifest_path=manifest_path,
        settings=Settings(),
    )

    assert report.passed is True
    assert payload["promotion_ready"] is False
    artifact_chain = payload["object_storage_artifact_chain"]
    assert artifact_chain["passed"] is False
    assert artifact_chain["full_artifact_cached_case_count"] >= 1
    assert artifact_chain["full_artifact_readable_case_count"] == 0
    assert artifact_chain["artifact_integrity_error_count"] > 0
    assert "object_storage_full_artifact_unreadable" in artifact_chain["blocker_codes"]
    assert "object_storage_artifact_integrity_error" in artifact_chain["blocker_codes"]
    assert any(
        blocker["code"] == "object_storage_artifact_chain_failed"
        for blocker in payload["promotion_blockers"]
    )
    assert any("/artifacts/extractions/doc-" in fetched for fetched in storage.gets)
    assert "raw_text" not in json.dumps(payload["object_storage_artifact_chain"])


def test_staging_blocks_promotion_when_artifact_chain_is_not_oci() -> None:
    """promotion では local/mock URI の artifact chain を OCI Object Storage 証跡にしない。"""
    report = _promotion_ready_staging_report("non-oci-artifact-chain")
    segment_artifact_evidence = cast(
        Mapping[str, object],
        report.metric_evidence["segment_artifact_reuse"],
    )
    report = FileProcessingStagingReport(
        run_id=report.run_id,
        knowledge_base_id=report.knowledge_base_id,
        runtime_checks=(
            FileProcessingStagingRuntimeCheckResult(
                check="extraction_artifact_cache_roundtrip",
                status="ok",
                evidence={
                    "object_ref_hash": "artifact:local",
                    "object_uri_scheme": "local",
                    "payload_bytes": 68,
                    "cleanup": "deleted",
                },
            ),
        ),
        case_results=report.case_results,
        metrics=report.metrics,
        metric_evidence={
            **dict(report.metric_evidence),
            "segment_artifact_reuse": {
                **dict(segment_artifact_evidence),
                "full_artifact_oci_case_count": 0,
                "segment_artifact_oci_uri_count": 0,
                "segment_artifact_non_oci_uri_count": 1,
            },
        },
        threshold_results=report.threshold_results,
    )
    manifest = {
        "staging_policy": {
            "required_for_promotion": True,
            "pending_checks_block_promotion": True,
            "required_runtime_checks": ["extraction_artifact_cache_roundtrip"],
        },
        "cases": [],
    }

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    artifact_chain = payload["object_storage_artifact_chain"]
    assert payload["promotion_ready"] is False
    assert artifact_chain["passed"] is False
    assert artifact_chain["roundtrip_object_uri_scheme"] == "local"
    assert "object_storage_artifact_roundtrip_not_oci" in artifact_chain["blocker_codes"]
    assert "object_storage_full_artifact_not_oci" in artifact_chain["blocker_codes"]
    assert "object_storage_segment_artifact_not_oci" in artifact_chain["blocker_codes"]
    assert any(
        blocker["code"] == "object_storage_artifact_chain_failed"
        for blocker in payload["promotion_blockers"]
    )


async def test_file_processing_staging_runner_reports_missing_bbox_gate() -> None:
    """bbox が欠けると preview bbox gate は失敗として返る。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle, image_has_bbox=False)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-2",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] < 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    threshold_by_metric = {result.metric: result for result in report.threshold_results}
    assert threshold_by_metric["bbox_citation_coverage"].status == "failed"
    assert threshold_by_metric["preview_addressability_coverage"].status == "failed"
    assert any(gate.failure_code == "bbox_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_rejects_invalid_bbox_gate() -> None:
    """ゼロ面積 bbox は preview へ位置決めできないため欠落扱いにする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle, image_bbox=[0, 0, 0, 0])
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-invalid-bbox",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] < 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(gate.failure_code == "bbox_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_requires_bbox_mode_for_ambiguous_bbox() -> None:
    """非原点 bbox は xyxy / xywh の判別 metadata がないと preview gate を通さない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(
        oracle,
        image_bbox=[25, 10, 50, 40],
        image_bbox_coordinate_mode=None,
    )
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-ambiguous-bbox-no-mode",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(gate.failure_code == "bbox_coordinate_mode_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_rejects_invalid_bbox_unit() -> None:
    """未知の bbox unit metadata は frontend preview と同じく address 不可にする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(
        oracle,
        image_bbox=[0.1, 0.1, 0.3, 0.2],
        image_bbox_unit="screen-space",
    )
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-invalid-bbox-unit",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(gate.failure_code == "bbox_unit_invalid" for gate in failed_gates)


async def test_file_processing_staging_runner_requires_preview_address_metadata() -> None:
    """bbox だけでは不十分で、preview jump には page と element lineage も必要。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle, image_has_element_ids=False)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-3",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    threshold_by_metric = {result.metric: result for result in report.threshold_results}
    assert threshold_by_metric["bbox_citation_coverage"].status == "passed"
    assert threshold_by_metric["preview_addressability_coverage"].status == "failed"
    assert any(gate.failure_code == "preview_address_metadata_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_rejects_orphan_element_lineage() -> None:
    """chunk の element_ids が extraction tree に存在しない場合は lineage 不足にする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(
        oracle,
        image_chunk_element_ids=["missing-el"],
        image_extraction_element_ids=["el-1"],
    )
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-orphan-element-lineage",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["element_lineage_coverage"] < 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(gate.failure_code == "preview_address_metadata_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_requires_page_size_for_absolute_bbox() -> None:
    """absolute bbox は extraction.pages の page size がないと preview へ定位できない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle, image_bbox=[153, 198, 306, 396])
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-absolute-bbox-no-page-size",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(gate.failure_code == "preview_address_metadata_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_accepts_absolute_bbox_with_page_size() -> None:
    """absolute bbox は page size があれば preview addressable として扱える。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(
        oracle,
        image_bbox=[153, 198, 306, 396],
        image_bbox_unit="px",
        image_page_size={"width": 612, "height": 792},
    )
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-absolute-bbox-with-page-size",
    )

    assert report.passed is True
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] == 1.0


async def test_file_processing_staging_runner_rejects_invalid_page_rotation_for_bbox() -> None:
    """rotated page の bbox は page rotation が解決できないと preview へ定位できない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(
        oracle,
        image_bbox=[153, 198, 306, 396],
        image_bbox_unit="px",
        image_page_size={"width": 612, "height": 792, "rotation": 45},
    )
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-absolute-bbox-invalid-page-rotation",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(gate.failure_code == "bbox_page_rotation_invalid" for gate in failed_gates)


async def test_file_processing_staging_runner_rejects_unaddressable_table_cell_bbox() -> None:
    """chunk bbox が正常でも table cell bbox が定位不可なら preview gate を落とす。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(
        oracle,
        image_table_cell_bbox=[153, 198, 306, 396],
        image_table_cell_bbox_unit="absolute",
    )
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="run-table-cell-bbox-no-page-size",
    )

    image_result = next(
        result for result in report.case_results if result.case_id == "image-ocr-bbox"
    )
    failed_gates = [gate for gate in image_result.gate_results if not gate.passed]

    assert report.passed is False
    assert report.metrics["bbox_citation_coverage"] == 1.0
    assert report.metrics["preview_addressability_coverage"] < 1.0
    assert any(
        gate.failure_code == "preview_extraction_bbox_absolute_page_size_missing"
        for gate in failed_gates
    )
    assert any(
        gate.evidence is not None
        and gate.evidence["extraction_bbox_target_count"] == 1
        and gate.evidence["extraction_preview_addressable_target_count"] == 0
        for gate in failed_gates
    )
    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(),
    )
    payload_image_result = next(
        result for result in payload["case_results"] if result["case_id"] == "image-ocr-bbox"
    )
    payload_failed_gate = next(
        gate for gate in payload_image_result["gate_results"] if not gate["passed"]
    )
    assert payload_failed_gate["evidence"]["extraction_bbox_target_count"] == 1
    assert payload_failed_gate["evidence"]["extraction_preview_addressable_target_count"] == 0
    assert "raw_text" not in str(payload_failed_gate["evidence"])


async def test_file_processing_staging_runner_requires_search_for_page_hit() -> None:
    """page hit は抽出ページではなく検索 citation のページ命中で判定する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    two_column_case = next(
        case for case in manifest["cases"] if case["id"] == "two-column-pdf-reading-order"
    )
    two_column_case.pop("staging_query")
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="page-hit-no-search",
    )

    two_column_result = next(
        result for result in report.case_results if result.case_id == "two-column-pdf-reading-order"
    )
    failed_gates = [gate for gate in two_column_result.gate_results if not gate.passed]
    threshold_by_metric = {result.metric: result for result in report.threshold_results}

    assert report.passed is False
    assert report.metrics["page_hit_accuracy"] == 0.0
    assert threshold_by_metric["page_hit_accuracy"].status == "failed"
    assert any(gate.failure_code == "search_not_executed" for gate in failed_gates)


async def test_file_processing_staging_runner_requires_traceable_search_citations() -> None:
    """検索が正しい文書/ページを返しても citation lineage がなければ失敗にする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeUntraceableSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="search-citation-untraceable",
    )

    two_column_result = next(
        result for result in report.case_results if result.case_id == "two-column-pdf-reading-order"
    )
    duplicate_result = next(
        result for result in report.case_results if result.case_id == "duplicate-file-canonical-kb"
    )
    failed_gates = [
        gate
        for result in (two_column_result, duplicate_result)
        for gate in result.gate_results
        if not gate.passed
    ]
    threshold_by_metric = {result.metric: result for result in report.threshold_results}

    assert report.passed is False
    assert report.metrics["retrieval_recall"] < 1.0
    assert report.metrics["page_hit_accuracy"] == 0.0
    assert threshold_by_metric["retrieval_recall"].status == "failed"
    assert threshold_by_metric["page_hit_accuracy"].status == "failed"
    assert any(gate.failure_code == "search_citation_traceability_missing" for gate in failed_gates)
    assert any(
        gate.failure_code == "duplicate_alias_citation_traceability_missing"
        for gate in failed_gates
    )


async def test_file_processing_staging_runner_requires_table_qa_search_answer() -> None:
    """table QA は local chunk だけでなく staging search answer でも期待値を要求する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeTableQaMissSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="table-qa-answer-miss",
    )

    table_result = next(
        result for result in report.case_results if result.case_id == "long-table-tsv-row-groups"
    )
    failed_gates = [gate for gate in table_result.gate_results if not gate.passed]
    threshold_by_metric = {result.metric: result for result in report.threshold_results}

    assert report.passed is False
    assert report.metrics["table_qa_accuracy"] < 1.0
    assert threshold_by_metric["table_qa_accuracy"].status == "failed"
    assert any(gate.failure_code == "expected_answer_not_in_search_answer" for gate in failed_gates)


async def test_file_processing_staging_runner_requires_table_cell_citation() -> None:
    """table QA は answer だけでなく期待 cell ref まで citation metadata で要求する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeTableCellRefMissSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="table-cell-citation-miss",
    )

    table_result = next(
        result for result in report.case_results if result.case_id == "long-table-tsv-row-groups"
    )
    failed_gates = [gate for gate in table_result.gate_results if not gate.passed]
    threshold_by_metric = {result.metric: result for result in report.threshold_results}

    assert report.passed is False
    assert report.metrics["table_qa_accuracy"] < 1.0
    assert report.metrics["table_cell_lineage_coverage"] < 1.0
    assert threshold_by_metric["table_qa_accuracy"].status == "failed"
    assert threshold_by_metric["table_cell_lineage_coverage"].status == "failed"
    assert any(gate.failure_code == "table_cell_citation_missing" for gate in failed_gates)
    assert any(
        gate.evidence is not None
        and gate.evidence["table_qa_cell_refs_expected_count"] == 1
        and gate.evidence["table_qa_cell_refs_resolved_count"] == 1
        and gate.evidence["table_qa_cell_refs_covered_count"] == 0
        for gate in failed_gates
    )


async def test_file_processing_staging_accepts_structured_table_cell_citation() -> None:
    """object / JSON 形式の adapter cell refs も table QA citation として扱う。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeStructuredTableCellRefSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="structured-table-cell-citation",
    )

    table_result = next(
        result for result in report.case_results if result.case_id == "long-table-tsv-row-groups"
    )
    table_gate = next(
        gate for gate in table_result.gate_results if gate.check == "table_qa_accuracy"
    )

    assert table_gate.passed is True
    assert table_gate.evidence is not None
    assert table_gate.evidence["table_qa_cell_refs_expected_count"] == 1
    assert table_gate.evidence["table_qa_cell_refs_resolved_count"] == 1
    assert table_gate.evidence["table_qa_cell_refs_covered_count"] == 1


async def test_file_processing_staging_requires_table_cell_refs_to_resolve_to_extraction() -> None:
    """citation metadata だけに cell ref があっても extraction cell に解決できなければ失敗。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeMissingTableCellExtractionIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="table-cell-extraction-ref-miss",
    )

    table_result = next(
        result for result in report.case_results if result.case_id == "long-table-tsv-row-groups"
    )
    failed_gates = [gate for gate in table_result.gate_results if not gate.passed]

    assert report.passed is False
    assert any(gate.failure_code == "table_cell_extraction_ref_missing" for gate in failed_gates)
    assert any(
        gate.evidence is not None
        and gate.evidence["table_qa_cell_refs_expected_count"] == 1
        and gate.evidence["table_qa_cell_refs_resolved_count"] == 0
        and gate.evidence["table_qa_cell_refs_covered_count"] == 1
        for gate in failed_gates
    )


async def test_file_processing_staging_runner_requires_dependency_lineage_citation() -> None:
    """dependency lineage は extraction だけでなく search citation metadata でも要求する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeDependencyLineageMissSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="dependency-lineage-miss",
    )

    html_result = next(
        result for result in report.case_results if result.case_id == "html-semantic-blocks"
    )
    failed_gates = [gate for gate in html_result.gate_results if not gate.passed]

    assert report.passed is False
    assert any(gate.failure_code == "dependency_lineage_citation_missing" for gate in failed_gates)


async def test_file_processing_staging_runner_requires_dependency_context_recall() -> None:
    """dependency context は final citation で promoted context を要求する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeDependencyContextMissSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="dependency-context-miss",
    )

    html_result = next(
        result for result in report.case_results if result.case_id == "html-semantic-blocks"
    )
    failed_gates = [gate for gate in html_result.gate_results if not gate.passed]

    assert report.passed is False
    assert any(
        gate.suggested_gate == "dependency_context_recall_gate"
        and gate.failure_code == "dependency_context_not_recalled"
        for gate in failed_gates
    )


async def test_file_processing_staging_runner_requires_structural_section_coverage() -> None:
    """multi-section case は search citations が期待 section を全て覆う必要がある。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeStructuralSectionMissSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="structural-section-miss",
    )

    html_result = next(
        result for result in report.case_results if result.case_id == "html-semantic-blocks"
    )
    failed_gates = [gate for gate in html_result.gate_results if not gate.passed]

    assert report.passed is False
    assert any(
        gate.suggested_gate == "structural_section_search_gate"
        and gate.failure_code == "expected_sections_not_retrieved"
        for gate in failed_gates
    )


async def test_file_processing_staging_runner_rejects_reprocessed_successful_segment() -> None:
    """retry 時に成功済み segment まで再処理したら artifact reuse gate は失敗する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    oracle = FakeReprocessingStagingOracle()
    storage = FakeObjectStorage()
    ingestion = FakeIngestion(oracle)
    search = FakeSearch(oracle)

    report = await run_file_processing_staging_checks(
        manifest,
        manifest_path=manifest_path,
        oracle=oracle,
        storage=storage,
        ingestion=ingestion,
        search=search,
        run_id="segment-success-reprocessed",
    )

    corrupted_result = next(
        result
        for result in report.case_results
        if result.case_id == "corrupted-file-partial-failure"
    )
    failed_gates = [gate for gate in corrupted_result.gate_results if not gate.passed]

    assert report.passed is False
    assert any(gate.failure_code == "successful_segment_reprocessed" for gate in failed_gates)


def test_file_processing_staging_cli_preflight_only_reports_safe_config_gap(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """preflight-only は外部接続せず安全な設定不足だけを artifact 化する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-preflight.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: Settings(
            oci_compartment_id="",
            oci_enterprise_ai_endpoint="",
            oci_enterprise_ai_project_ocid="",
            oci_enterprise_ai_api_key="",
            oci_enterprise_ai_models=[],
            oci_enterprise_ai_default_text_model="",
            oracle_user="",
            oracle_dsn="",
            object_storage_namespace="",
            object_storage_bucket="",
            upload_storage_backend="oci",
            oracle_password="super-secret-password",
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )

    exit_code = file_processing_staging_cli.main(
        [str(manifest_path), "--preflight-only", "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["preflight"]["ok"] is False
    assert payload["preflight"]["checks"]["oracle"] == "missing"
    assert payload["preflight"]["checks"]["object_storage"] == "missing"
    assert payload["parser_adapters"]["adapter_backend"] == "local"
    assert payload["parser_adapters"]["effective_order"] == []
    assert payload["parser_adapter_preflight"]["ok"] is True
    assert payload["parser_adapter_scorecard"]["recommended_backend"] == "local"
    assert payload["parser_adapter_scorecard"]["metrics_applied_to"] is None
    route_by_kind = {
        route["source_kind"]: route for route in payload["parser_adapter_source_routes"]
    }
    assert route_by_kind["pdf"]["candidate_order"] == ["docling", "unstructured"]
    assert route_by_kind["email"]["candidate_order"] == ["unstructured"]
    assert "super-secret-password" not in output_path.read_text(encoding="utf-8")


def test_parser_adapter_contract_failure_blocks_promotion() -> None:
    """active adapter の schema remap smoke 失敗は promotion blocker にする。"""
    matrix = ParserAdapterCompatibilityMatrix(
        passed=False,
        fixture_root="/tmp/fixtures",
        source_kinds=("pdf",),
        backends=("docling",),
        case_count=1,
        blocking_failure_count=1,
        cases=(
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="policy-ja.pdf",
                content_type="application/pdf",
                status="fallback",
                blocking=True,
                parser_backend="docling",
                warning_codes=("docling_adapter_failed",),
            ),
        ),
    )

    blockers = file_processing_staging_cli._parser_adapter_contract_promotion_blockers(matrix)

    assert blockers == [
        {
            "code": "parser_adapter_contract_failed",
            "count": 1,
            "backends": ["docling"],
            "source_kinds": ["pdf"],
        }
    ]


def test_report_payload_strict_contract_uses_explicit_adapter_settings(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """strict staging は外部 adapter 全選択かつ manifest fixture で contract を走らせる。"""
    captured_settings: dict[str, object] = {}
    captured_contract_args: dict[str, object] = {}

    def fake_contract_matrix(
        settings: Settings,
        **kwargs: object,
    ) -> ParserAdapterCompatibilityMatrix:
        captured_settings["backend"] = settings.rag_parser_adapter_backend
        captured_settings["docling_enabled"] = settings.rag_parser_docling_enabled
        captured_settings["unstructured_enabled"] = settings.rag_parser_unstructured_enabled
        captured_contract_args.update(kwargs)
        return ParserAdapterCompatibilityMatrix(
            passed=True,
            fixture_root=str(kwargs["fixture_root"]),
            source_kinds=("pdf", "html"),
            backends=("docling", "unstructured"),
            case_count=2,
            blocking_failure_count=0,
            cases=(
                _contract_passed_case(
                    "docling",
                    "pdf",
                    case_id="scanned-pdf-ocr-ja",
                    scenario="scanned_pdf_ocr",
                ),
                _contract_passed_case(
                    "unstructured",
                    "html",
                    case_id="html-semantic-blocks",
                    scenario="html_semantic_blocks",
                ),
            ),
        )

    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_parser_adapter_compatibility_matrix",
        fake_contract_matrix,
    )
    report = FileProcessingStagingReport(
        run_id="run-strict-contract",
        knowledge_base_id=None,
        case_results=(),
        metrics={"adapter_contract_coverage": 1.0},
        metric_evidence={},
    )
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "scanned-contract-ja.pdf").write_bytes(b"%PDF-1.7")
    (fixture_root / "manual.html").write_text("<h1>検索運用</h1>", encoding="utf-8")
    manifest_path = tmp_path / "manifests" / "file-processing-golden-set.json"
    manifest_path.parent.mkdir()
    manifest = {
        "fixture_root": "../fixtures",
        "cases": [
            {
                "id": "scanned-pdf-ocr-ja",
                "fixture": "scanned-contract-ja.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
                "adapter_schema_remap": True,
            },
            {
                "id": "html-semantic-blocks",
                "fixture": "manual.html",
                "modality": "html",
                "scenario": "html_semantic_blocks",
                "adapter_schema_remap": True,
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        manifest_path=manifest_path,
        settings=Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
        parser_adapter_contract_strict=True,
    )

    assert captured_settings == {
        "backend": "docling",
        "docling_enabled": True,
        "unstructured_enabled": True,
    }
    assert payload["parser_adapter_contract_mode"] == "strict"
    assert payload["parser_adapters"]["adapter_backend"] == "docling"
    assert captured_contract_args["fixture_root"] == fixture_root
    assert captured_contract_args["require_backend_evidence"] is True
    fixture_specs = cast(
        tuple[ParserAdapterFixtureSpec, ...],
        captured_contract_args["fixture_specs"],
    )
    assert [spec.file_name for spec in fixture_specs] == [
        "scanned-contract-ja.pdf",
        "manual.html",
    ]
    assert [spec.case_id for spec in fixture_specs] == [
        "scanned-pdf-ocr-ja",
        "html-semantic-blocks",
    ]
    assert captured_contract_args["source_kinds"] == ["pdf", "html"]
    assert payload["adapter_golden_gate"]["contract_passed_case_refs"]
    assert payload["adapter_golden_gate"]["contract_backend_passed_case_refs"]
    assert payload["adapter_golden_gate"]["contract_blocking_failure_case_refs"] == []


def test_report_payload_reflects_parser_adapter_contract_metric_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """runtime adapter contract failure は metric / threshold artifact にも反映する。"""
    matrix = ParserAdapterCompatibilityMatrix(
        passed=False,
        fixture_root="/tmp/fixtures",
        source_kinds=("pdf",),
        backends=("docling",),
        case_count=1,
        blocking_failure_count=1,
        cases=(
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="policy-ja.pdf",
                content_type="application/pdf",
                status="failed",
                blocking=True,
                case_id="secret-contract-case",
                reason_codes=("schema_remap_page_lineage_missing",),
            ),
        ),
    )
    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_parser_adapter_compatibility_matrix",
        lambda *_args, **_kwargs: matrix,
    )
    report = FileProcessingStagingReport(
        run_id="run-contract-failure",
        knowledge_base_id=None,
        case_results=(),
        metrics={"adapter_contract_coverage": 1.0},
        metric_evidence={},
        threshold_results=(
            FileProcessingMetricThresholdResult(
                metric="adapter_contract_coverage",
                direction="min",
                threshold=1.0,
                actual=1.0,
                status="passed",
                passed=True,
            ),
        ),
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest={"thresholds": {"adapter_contract_coverage": {"min": 1.0}}, "cases": []},
        settings=Settings(rag_parser_adapter_backend="docling", rag_parser_docling_enabled=True),
    )

    threshold = {result["metric"]: result for result in payload["threshold_results"]}[
        "adapter_contract_coverage"
    ]
    assert payload["passed"] is False
    assert payload["promotion_ready"] is False
    assert payload["metrics"]["adapter_contract_coverage"] == 0.0
    assert payload["metric_evidence"]["adapter_contract_coverage"] == {
        "source": "parser_adapter_contract",
        "passed": False,
        "case_count": 1,
        "blocking_failure_count": 1,
        "missing_source_kinds": ["pdf"],
        "blocking_failure_source_kinds": ["pdf"],
        "blocking_failure_backends": ["docling"],
        "reason_code_counts": {"schema_remap_page_lineage_missing": 1},
        "warning_code_counts": {},
        "blocking_failure_reason_counts": {"schema_remap_page_lineage_missing": 1},
    }
    assert threshold["actual"] == 0.0
    assert threshold["status"] == "failed"
    assert threshold["passed"] is False
    assert threshold["reason"] == "parser_adapter_contract_failed"
    summary_text = json.dumps(
        payload["adapter_contract_matrix_summary"],
        ensure_ascii=False,
    )
    assert "secret-contract-case" not in summary_text
    assert "policy-ja.pdf" not in summary_text
    assert payload["adapter_contract_matrix_summary"]["blocking_failures"][0]["case_ref_hash"]
    assert {
        "code": "parser_adapter_contract_failed",
        "count": 1,
        "backends": ["docling"],
        "source_kinds": ["pdf"],
    } in payload["promotion_blockers"]


def test_report_payload_source_routes_are_contract_aware(
    monkeypatch: MonkeyPatch,
) -> None:
    """staging route は固定順ではなく source/backend の remap 証跡で補正する。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (True, "1.0.0", import_name),
    )
    matrix = ParserAdapterCompatibilityMatrix(
        passed=True,
        fixture_root="/tmp/fixtures",
        source_kinds=("pdf", "office", "html", "email", "image"),
        backends=("docling", "unstructured"),
        case_count=5,
        blocking_failure_count=0,
        cases=(
            _contract_passed_case("docling", "pdf"),
            _contract_passed_case("unstructured", "office"),
            _contract_passed_case("unstructured", "html"),
            _contract_passed_case("unstructured", "email"),
            _contract_passed_case("unstructured", "image"),
        ),
    )
    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_parser_adapter_compatibility_matrix",
        lambda *_args, **_kwargs: matrix,
    )
    manifest = {
        "staging_policy": {
            "required_for_promotion": True,
            "pending_checks_block_promotion": True,
            "required_runtime_checks": ["extraction_artifact_cache_roundtrip"],
        },
        "cases": [
            {"id": "pdf", "fixture": "a.pdf", "modality": "pdf"},
            {"id": "office", "fixture": "a.xlsx", "modality": "office"},
            {"id": "html", "fixture": "a.html", "modality": "html"},
            {"id": "email", "fixture": "a.eml", "modality": "email"},
            {"id": "image", "fixture": "a.png", "modality": "image"},
        ],
    }

    payload = file_processing_staging_cli._report_payload(
        _promotion_ready_staging_report("contract-aware-route"),
        manifest=manifest,
        settings=Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
            rag_parser_unstructured_enabled=True,
        ),
    )

    route_by_kind = {
        route["source_kind"]: route for route in payload["parser_adapter_source_routes"]
    }
    assert route_by_kind["pdf"]["candidate_order"] == ("docling", "unstructured")
    assert route_by_kind["pdf"]["selected_backend"] == "docling"
    assert "selected_adapter_supported_for_source" in route_by_kind["pdf"]["reason_codes"]
    # Docling は PDF と画像だけを解析するため、Docling を選んでも office は
    # Docling に回らない(#366)。取込では parser_source_guard が取込前に止めて案内する
    # (自動で Unstructured へ振り分けない)。
    assert route_by_kind["office"]["candidate_order"] == ("unstructured",)
    assert route_by_kind["office"]["selected_backend"] == "local"
    assert "selected_adapter_unsupported_for_source" in route_by_kind["office"]["reason_codes"]
    assert "docling_adapter_source_unsupported" in route_by_kind["office"]["warning_codes"]
    assert route_by_kind["email"]["selected_backend"] == "local"
    assert payload["adapter_golden_gate"]["source_route_contract_gap_source_kinds"] == ["image"]
    assert (
        "adapter_golden_gate_source_route_contract_missing"
        in payload["adapter_golden_gate"]["blocker_codes"]
    )
    blocker = next(
        item
        for item in payload["promotion_blockers"]
        if item["code"] == "adapter_golden_gate_failed"
    )
    assert blocker["source_route_contract_gap_source_kinds"] == ["image"]


def test_file_processing_staging_cli_strict_preflight_requires_selected_adapter(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """strict staging preflight は local runtime でも選択 adapter package 不足を止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-strict-preflight.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: _complete_oci_settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    exit_code = file_processing_staging_cli.main(
        [
            str(manifest_path),
            "--preflight-only",
            "--parser-adapter-contract-strict",
            "--output",
            str(output_path),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    failures = payload["parser_adapter_preflight"]["failures"]
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["parser_adapter_contract_mode"] == "strict"
    assert payload["parser_adapters"]["adapter_backend"] == "docling"
    assert {failure["backend"] for failure in failures} == {"docling"}
    assert {failure["status"] for failure in failures} == {"missing"}
    assert payload["parser_adapter_contract"]["passed"] is False


def test_preflight_payload_strict_runs_manifest_adapter_contract(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """strict preflight でも manifest fixture を実 adapter contract runner へ渡す。"""
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "scanned-contract-ja.pdf").write_bytes(b"%PDF-1.7")
    (fixture_root / "manual.html").write_text("<h1>検索運用</h1>", encoding="utf-8")
    manifest_path = tmp_path / "manifests" / "file-processing-golden-set.json"
    manifest_path.parent.mkdir()
    manifest = {
        "fixture_root": "../fixtures",
        "cases": [
            {
                "id": "scanned-pdf-ocr-ja",
                "fixture": "scanned-contract-ja.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
                "adapter_schema_remap": True,
            },
            {
                "id": "html-semantic-blocks",
                "fixture": "manual.html",
                "modality": "html",
                "scenario": "html_semantic_blocks",
                "adapter_schema_remap": True,
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    captured_contract_args: dict[str, object] = {}
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name in {"docling", "unstructured"},
            "1.0.0",
            import_name,
        ),
    )

    def fake_contract_matrix(
        settings: Settings,
        **kwargs: object,
    ) -> ParserAdapterCompatibilityMatrix:
        captured_contract_args["backend"] = settings.rag_parser_adapter_backend
        captured_contract_args["docling_enabled"] = settings.rag_parser_docling_enabled
        captured_contract_args["unstructured_enabled"] = settings.rag_parser_unstructured_enabled
        captured_contract_args.update(kwargs)
        return ParserAdapterCompatibilityMatrix(
            passed=True,
            fixture_root=str(kwargs["fixture_root"]),
            source_kinds=("pdf", "html"),
            backends=("docling", "unstructured"),
            case_count=2,
            blocking_failure_count=0,
            cases=(
                _contract_passed_case("docling", "pdf"),
                _contract_passed_case("unstructured", "html"),
            ),
        )

    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_parser_adapter_compatibility_matrix",
        fake_contract_matrix,
    )

    payload = file_processing_staging_cli._preflight_payload(
        SmokePreflightResult(ok=True, checks={}, message="ok"),
        Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
        manifest=manifest,
        manifest_path=manifest_path,
        parser_adapter_contract_strict=True,
    )

    fixture_specs = cast(
        tuple[ParserAdapterFixtureSpec, ...],
        captured_contract_args["fixture_specs"],
    )
    assert payload["passed"] is True
    assert payload["failure_count"] == 0
    assert payload["parser_adapter_contract"]["passed"] is True
    assert payload["adapter_contract_matrix_summary"]["passed"] is True
    contract_text = json.dumps(payload["parser_adapter_contract"], ensure_ascii=False)
    assert str(fixture_root) not in contract_text
    assert "scanned-contract-ja.pdf" not in contract_text
    assert "manual.html" not in contract_text
    assert captured_contract_args["backend"] == "docling"
    assert captured_contract_args["docling_enabled"] is True
    assert captured_contract_args["unstructured_enabled"] is True
    assert captured_contract_args["fixture_root"] == fixture_root
    assert captured_contract_args["require_backend_evidence"] is True
    assert [spec.file_name for spec in fixture_specs] == [
        "scanned-contract-ja.pdf",
        "manual.html",
    ]
    assert [spec.case_id for spec in fixture_specs] == [
        "scanned-pdf-ocr-ja",
        "html-semantic-blocks",
    ]


def test_preflight_payload_strict_blocks_schema_remap_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """package が active でも schema remap 証跡が失敗すれば preflight は通さない。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name in {"docling", "unstructured"},
            "1.0.0",
            import_name,
        ),
    )
    matrix = ParserAdapterCompatibilityMatrix(
        passed=False,
        fixture_root="/tmp/fixtures",
        source_kinds=("pdf",),
        backends=("docling",),
        case_count=1,
        blocking_failure_count=1,
        cases=(
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="scanned-contract-ja.pdf",
                content_type="application/pdf",
                status="failed",
                blocking=True,
                case_id="secret-real-world-case",
                reason_codes=("schema_remap_page_lineage_missing",),
            ),
        ),
    )
    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_parser_adapter_compatibility_matrix",
        lambda *_args, **_kwargs: matrix,
    )

    payload = file_processing_staging_cli._preflight_payload(
        SmokePreflightResult(ok=True, checks={}, message="ok"),
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
            rag_parser_unstructured_enabled=True,
        ),
        manifest={
            "cases": [
                {
                    "id": "scanned-pdf-ocr-ja",
                    "fixture": "scanned-contract-ja.pdf",
                    "modality": "pdf",
                    "scenario": "scanned_pdf_ocr",
                    "adapter_schema_remap": True,
                }
            ]
        },
        parser_adapter_contract_strict=True,
    )

    assert payload["passed"] is False
    assert payload["failure_count"] == 1
    assert payload["parser_adapter_preflight"]["ok"] is True
    assert payload["parser_adapter_contract"]["passed"] is False
    assert payload["adapter_contract_matrix_summary"]["blocking_failure_reason_counts"] == {
        "schema_remap_page_lineage_missing": 1
    }
    summary_text = json.dumps(
        payload["adapter_contract_matrix_summary"],
        ensure_ascii=False,
    )
    assert "secret-real-world-case" not in summary_text
    assert "scanned-contract-ja.pdf" not in summary_text
    assert payload["adapter_contract_matrix_summary"]["blocking_failures"][0]["case_ref_hash"]


def test_file_processing_staging_cli_stops_before_clients_when_preflight_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """通常実行でも preflight 失敗時は実 staging client を作らない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-preflight.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )

    async def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("real staging runner must not be called")

    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_file_processing_staging_checks_with_real_clients",
        fail_if_called,
    )

    exit_code = file_processing_staging_cli.main([str(manifest_path), "--output", str(output_path)])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["preflight"]["ok"] is False
    assert payload["parser_adapters"]["adapter_backend"] == "local"


def test_file_processing_staging_cli_fails_when_report_is_not_promotion_ready(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """staging CLI は report.passed だけでなく promotion_ready=false も失敗扱いにする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-report.json"
    trend_path = tmp_path / "file-processing-staging-trend.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: _complete_oci_settings(rag_parser_adapter_backend="local"),
    )
    monkeypatch.setattr(
        file_processing_staging_cli,
        "staging_smoke_preflight",
        lambda *, settings: SmokePreflightResult(
            ok=True,
            checks={"oracle": "configured", "object_storage": "configured"},
            message="ok",
        ),
    )

    async def fake_runner(
        manifest: Mapping[str, object],
        *,
        manifest_path: Path,
        cleanup: bool = False,
        settings: Settings | None = None,
    ) -> object:
        del cleanup, settings
        oracle = FakeStagingOracle()
        return await run_file_processing_staging_checks(
            manifest,
            manifest_path=manifest_path,
            oracle=oracle,
            storage=FakeObjectStorage(),
            ingestion=FakeIngestion(oracle),
            search=FakeSearch(oracle),
            run_id="cli-promotion-not-ready",
            artifact_cache_enabled=False,
        )

    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_file_processing_staging_checks_with_real_clients",
        fake_runner,
    )

    exit_code = file_processing_staging_cli.main(
        [
            str(manifest_path),
            "--output",
            str(output_path),
            "--trend-output",
            str(trend_path),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    trend = json.loads(trend_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is True
    assert payload["promotion_ready"] is False
    assert trend["kind"] == "file_processing_staging"
    assert trend["passed"] is True
    assert trend["promotion_ready"] is False
    assert trend["promotion_blocker_code_counts"]["required_runtime_check_not_ok"] == 1
    assert trend["metrics"]["retrieval_recall"] == 1.0
    assert "case_results" not in trend
    assert "raw_text" not in trend_path.read_text(encoding="utf-8")
    assert {
        "code": "required_runtime_check_not_ok",
        "check": "extraction_artifact_cache_roundtrip",
        "status": "skipped",
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_underperforming_selected_adapter(
    monkeypatch: MonkeyPatch,
) -> None:
    """明示 parser adapter が staging 指標で local fallback 未満なら昇格を止める。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (True, "1.0.0", "docling"),
    )
    settings = _complete_oci_settings(
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    report = FileProcessingStagingReport(
        run_id="parser-scorecard-poor-docling",
        knowledge_base_id=None,
        case_results=(),
        metrics={
            "retrieval_recall": 0.0,
            "table_qa_accuracy": 0.0,
            "page_hit_accuracy": 0.0,
            "parser_fallback_rate": 1.0,
            "failed_segment_rate": 1.0,
            "ingestion_p95_ms": 60_000.0,
        },
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest={},
        settings=settings,
    )

    assert payload["promotion_ready"] is False
    assert payload["parser_adapter_scorecard"]["recommended_backend"] == "local"
    assert {
        "code": "parser_adapter_scorecard_mismatch",
        "selected_backend": "docling",
        "recommended_backend": "local",
        "metrics_source": "file_processing_staging",
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_underperforming_chunk_template() -> None:
    """Adaptive chunking 指標が悪い template は promotion blocker にする。"""
    report = FileProcessingStagingReport(
        run_id="chunk-template-poor-pdf-layout",
        knowledge_base_id=None,
        case_results=(),
        metrics={
            "chunk_block_integrity": 0.0,
            "chunk_contextual_coherence": 0.3,
            "chunk_size_compliance": 0.5,
            "element_lineage_coverage": 1.0,
            "page_hit_accuracy": 1.0,
        },
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest={"cases": [{"expected_chunk_template": "pdf_layout"}]},
        settings=Settings(),
    )

    assert payload["promotion_ready"] is False
    assert payload["chunk_template_scorecard"]["promotion_blocking"] is True
    assert {
        "code": "chunk_template_scorecard_blocked",
        "template": "pdf_layout",
        "score": payload["chunk_template_scorecard"]["entries"][0]["score"],
        "metrics_source": "file_processing_staging",
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_chunk_template_evidence_gap() -> None:
    """template 別の staging source/scenario 証跡が欠ける場合は global metrics で通さない。"""
    report = FileProcessingStagingReport(
        run_id="chunk-template-evidence-gap",
        knowledge_base_id=None,
        case_results=(
            FileProcessingStagingCaseResult(
                case_id="scanned-pdf-ocr-ja",
                scenario="scanned_pdf_ocr",
                fixture="scanned-contract-ja.pdf",
                document_id="doc-pdf",
                status="INDEXED",
                chunk_count=2,
                segment_count=1,
                gate_results=(
                    FileProcessingStagingGateResult(
                        case_id="scanned-pdf-ocr-ja",
                        scenario="scanned_pdf_ocr",
                        check="chunk_block_integrity",
                        suggested_gate="chunk_block_integrity_gate",
                        passed=True,
                        evidence={"source_kind": "pdf", "chunk_templates": ["pdf_layout"]},
                    ),
                ),
                cleanup={},
            ),
            FileProcessingStagingCaseResult(
                case_id="html-semantic-blocks",
                scenario="html_semantic_blocks",
                fixture="manual.html",
                document_id="doc-html",
                status="INDEXED",
                chunk_count=1,
                segment_count=1,
                gate_results=(),
                cleanup={},
            ),
        ),
        metrics={
            "chunk_block_integrity": 1.0,
            "chunk_contextual_coherence": 1.0,
            "chunk_size_compliance": 1.0,
            "element_lineage_coverage": 1.0,
            "structural_section_coverage": 1.0,
            "dependency_context_recall": 1.0,
            "page_hit_accuracy": 1.0,
        },
    )
    manifest = {
        "cases": [
            {
                "id": "scanned-pdf-ocr-ja",
                "fixture": "scanned-contract-ja.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
                "expected_chunk_template": "pdf_layout",
            },
            {
                "id": "html-semantic-blocks",
                "fixture": "manual.html",
                "modality": "html",
                "scenario": "html_semantic_blocks",
                "expected_chunk_template": "html_semantic",
            },
        ]
    }

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(),
    )

    entries = {entry["template"]: entry for entry in payload["chunk_template_scorecard"]["entries"]}
    assert payload["promotion_ready"] is False
    assert entries["pdf_layout"]["promotion_blocking"] is False
    assert entries["html_semantic"]["promotion_blocking"] is True
    assert entries["html_semantic"]["expected_case_count"] == 1
    assert entries["html_semantic"]["measured_case_count"] == 0
    assert entries["html_semantic"]["missing_source_kinds"] == ("html",)
    assert entries["html_semantic"]["missing_scenarios"] == ("html_semantic_blocks",)
    assert "chunk_template_case_evidence_missing" in entries["html_semantic"]["reason_codes"]
    assert {
        "code": "chunk_template_scorecard_blocked",
        "template": "html_semantic",
        "score": entries["html_semantic"]["score"],
        "metrics_source": "file_processing_staging",
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_chunk_template_runtime_metadata_gap() -> None:
    """gate が通っても chunk metadata に期待 template がなければ実測扱いにしない。"""
    report = FileProcessingStagingReport(
        run_id="chunk-template-runtime-gap",
        knowledge_base_id=None,
        case_results=(
            FileProcessingStagingCaseResult(
                case_id="scanned-pdf-ocr-ja",
                scenario="scanned_pdf_ocr",
                fixture="scanned-contract-ja.pdf",
                document_id="doc-pdf",
                status="INDEXED",
                chunk_count=2,
                segment_count=1,
                gate_results=(
                    FileProcessingStagingGateResult(
                        case_id="scanned-pdf-ocr-ja",
                        scenario="scanned_pdf_ocr",
                        check="chunk_block_integrity",
                        suggested_gate="chunk_block_integrity_gate",
                        passed=True,
                        evidence={
                            "source_kind": "pdf",
                            "chunk_templates": ["text_blocks"],
                        },
                    ),
                ),
                cleanup={},
            ),
        ),
        metrics={
            "chunk_block_integrity": 1.0,
            "chunk_contextual_coherence": 1.0,
            "chunk_size_compliance": 1.0,
            "element_lineage_coverage": 1.0,
            "page_hit_accuracy": 1.0,
        },
    )
    manifest = {
        "cases": [
            {
                "id": "scanned-pdf-ocr-ja",
                "fixture": "scanned-contract-ja.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
                "expected_chunk_template": "pdf_layout",
            }
        ]
    }

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(),
    )

    entry = payload["chunk_template_scorecard"]["entries"][0]
    assert payload["promotion_ready"] is False
    assert entry["template"] == "pdf_layout"
    assert entry["promotion_blocking"] is True
    assert entry["expected_case_count"] == 1
    assert entry["measured_case_count"] == 0
    assert entry["observed_chunk_templates"] == ("text_blocks",)
    assert entry["missing_source_kinds"] == ("pdf",)
    assert "chunk_template_case_evidence_missing" in entry["reason_codes"]
    assert {
        "code": "chunk_template_scorecard_blocked",
        "template": "pdf_layout",
        "score": entry["score"],
        "metrics_source": "file_processing_staging",
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_incomplete_adapter_golden_gate() -> None:
    """promotion staging では同一 golden set の adapter 指標不足を許可しない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = FileProcessingStagingReport(
        run_id="adapter-golden-incomplete",
        knowledge_base_id=None,
        case_results=(),
        metrics={
            "table_qa_accuracy": 1.0,
            "parser_fallback_rate": 0.0,
        },
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    gate = payload["adapter_golden_gate"]
    blocker = next(
        item
        for item in payload["promotion_blockers"]
        if item["code"] == "adapter_golden_gate_failed"
    )
    assert gate["passed"] is False
    assert set(gate["missing_source_kinds"]) == {"pdf", "office", "html", "email", "image"}
    assert set(gate["missing_metric_names"]) >= {
        "page_hit_accuracy",
        "bbox_citation_coverage",
        "bbox_coordinate_validity_coverage",
        "preview_addressability_coverage",
        "backend_source_kind_coverage",
    }
    assert "adapter_golden_gate_source_kind_not_measured" in blocker["blocker_codes"]
    assert "adapter_golden_gate_metric_missing" in blocker["blocker_codes"]
    assert blocker["selected_backend"] == "local"


def test_file_processing_staging_cli_blocks_loosened_promotion_thresholds() -> None:
    """promotion manifest の中核閾値が緩い場合は report が passed でも止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["thresholds"]["table_qa_accuracy"] = {"min": 0.5}
    manifest["thresholds"]["preview_addressability_coverage"] = {"min": 0.5}
    manifest["thresholds"]["table_cell_lineage_coverage"] = {"min": 0.5}
    manifest["thresholds"]["visual_chunk_metadata_completeness"] = {"min": 0.5}
    manifest["thresholds"]["ingestion_quality_report_completeness"] = {"min": 0.5}
    manifest["thresholds"]["parser_warning_taxonomy_coverage"] = {"min": 0.5}
    manifest["thresholds"]["parser_routing_accuracy"] = {"min": 0.5}
    manifest["thresholds"]["source_kind_coverage"] = {"min": 0.5}
    manifest["thresholds"]["backend_source_kind_coverage"] = {"min": 0.5}
    manifest["thresholds"]["parser_fallback_rate"] = {"max": 0.9}
    report = FileProcessingStagingReport(
        run_id="loosened-promotion-thresholds",
        knowledge_base_id=None,
        case_results=(),
        metrics={
            "retrieval_recall": 1.0,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "preview_addressability_coverage": 1.0,
            "table_cell_lineage_coverage": 1.0,
            "visual_chunk_metadata_completeness": 1.0,
            "ingestion_quality_report_completeness": 1.0,
            "parser_warning_taxonomy_coverage": 1.0,
            "parser_routing_accuracy": 1.0,
            "source_kind_coverage": 1.0,
            "backend_source_kind_coverage": 1.0,
            "parser_fallback_rate": 0.0,
        },
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    assert payload["passed"] is True
    assert payload["promotion_ready"] is False
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "table_qa_accuracy",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "preview_addressability_coverage",
        "direction": "min",
        "required": 0.8,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "table_cell_lineage_coverage",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "visual_chunk_metadata_completeness",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "ingestion_quality_report_completeness",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "parser_warning_taxonomy_coverage",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "parser_routing_accuracy",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "parser_fallback_rate",
        "direction": "max",
        "required": 0.2,
        "actual": 0.9,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "source_kind_coverage",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]
    assert {
        "code": "promotion_threshold_too_loose",
        "metric": "backend_source_kind_coverage",
        "direction": "min",
        "required": 1.0,
        "actual": 0.5,
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_invalid_real_world_dataset_policy() -> None:
    """real-world staging policy の未達は専用 blocker として非機密 summary に残す。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["staging_dataset_policy"] = {
        "required_for_promotion": True,
        "min_real_world_cases": 2,
        "required_source_kinds": ["pdf", "office"],
        "required_scenarios": ["scanned_pdf_ocr", "japanese_docx_layout"],
        "required_fixture_prefix": "staging/",
    }
    manifest["cases"].append(
        {
            "id": "fake-real-pdf",
            "fixture": "scanned-contract-ja.pdf",
            "fixture_kind": "real_world",
            "modality": "pdf",
            "scenario": "scanned_pdf_ocr",
            "expected_parser_profile": "enterprise_ai_pdf_layout",
            "expected_chunk_template": "pdf_layout",
            "expected_content_kind": "text",
            "required_checks": [
                "ocr_text",
                "page_coverage",
                "citation_traceability",
                "quality_report_metadata",
            ],
        }
    )
    report = _promotion_ready_staging_report("invalid-real-world-policy")

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    blocker = next(
        item
        for item in payload["promotion_blockers"]
        if item["code"] == "staging_dataset_policy_failed"
    )
    assert payload["promotion_ready"] is False
    assert blocker["policy_error_count"] >= 1
    assert blocker["min_real_world_cases"] == 2
    assert blocker["real_world_case_count"] == 1
    assert blocker["compliant_real_world_case_count"] == 0
    assert blocker["missing_source_kinds"] == ["office"]
    assert blocker["missing_scenarios"] == ["japanese_docx_layout"]
    assert blocker["sensitivity_violation_count"] == 1
    assert blocker["review_missing_count"] == 1
    assert blocker["fixture_prefix_mismatch_count"] == 1
    assert "fake-real-pdf" not in json.dumps(blocker, ensure_ascii=False)
    assert "scanned-contract-ja.pdf" not in json.dumps(blocker, ensure_ascii=False)


def test_file_processing_staging_preflight_requires_real_world_policy() -> None:
    """production 用 staging は real-world policy 不在を実 client 作成前に止める。"""
    payload = file_processing_staging_cli._preflight_payload(
        SmokePreflightResult(ok=True, checks={}, message="ok"),
        Settings(rag_parser_adapter_backend="local"),
        manifest={"cases": []},
        require_real_world_policy=True,
    )

    assert payload["passed"] is False
    assert payload["failure_count"] == 1
    assert payload["real_world_policy_preflight"] == {
        "ok": False,
        "message": "staging_dataset_policy is required for promotion",
        "failures": [{"code": "staging_dataset_policy_missing"}],
    }


def test_file_processing_staging_cli_blocks_missing_real_world_policy_when_required() -> None:
    """require_real_world_policy=true では synthetic-only manifest の promotion を止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = _promotion_ready_staging_report("missing-real-world-policy")

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
        require_real_world_policy=True,
    )

    assert payload["promotion_ready"] is False
    assert {
        "code": "staging_dataset_policy_missing",
        "required_for_promotion": True,
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_unexecuted_real_world_policy_case() -> None:
    """real-world policy は manifest 合規だけでなく本 staging 実行 coverage を要求する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["staging_dataset_policy"] = {
        "required_for_promotion": True,
        "min_real_world_cases": 1,
        "required_source_kinds": ["pdf"],
        "required_scenarios": ["scanned_pdf_ocr"],
        "required_fixture_prefix": "staging/",
    }
    manifest["cases"].append(
        {
            "id": "real-scanned-pdf-ocr-ja",
            "fixture": "staging/real-scanned-contract-ja.pdf",
            "fixture_kind": "real_world",
            "data_sensitivity": "non_sensitive",
            "reviewed_for_public_ci": True,
            "modality": "pdf",
            "scenario": "scanned_pdf_ocr",
            "expected_parser_profile": "enterprise_ai_pdf_layout",
            "expected_chunk_template": "pdf_layout",
            "expected_content_kind": "text",
            "required_checks": [
                "ocr_text",
                "page_coverage",
                "citation_traceability",
                "quality_report_metadata",
            ],
        }
    )
    report = _promotion_ready_staging_report("unexecuted-real-world-policy")

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    summary = payload["staging_dataset_policy"]
    blocker = next(
        item
        for item in payload["promotion_blockers"]
        if item["code"] == "staging_dataset_policy_failed"
    )
    assert payload["promotion_ready"] is False
    assert summary["real_world_case_count"] == 1
    assert summary["compliant_real_world_case_count"] == 1
    assert summary["executed_real_world_case_count"] == 0
    assert summary["executed_compliant_real_world_case_count"] == 0
    assert summary["missing_executed_source_kinds"] == ["pdf"]
    assert summary["missing_executed_scenarios"] == ["scanned_pdf_ocr"]
    assert summary["execution_error_codes"] == [
        "real_world_executed_cases_insufficient",
        "real_world_executed_source_kinds_missing",
        "real_world_executed_scenarios_missing",
    ]
    assert blocker["executed_real_world_case_count"] == 0
    assert blocker["executed_compliant_real_world_case_count"] == 0
    assert blocker["missing_executed_source_kinds"] == ["pdf"]
    assert blocker["missing_executed_scenarios"] == ["scanned_pdf_ocr"]
    blocker_text = json.dumps(blocker, ensure_ascii=False)
    assert "real-scanned-pdf-ocr-ja" not in blocker_text
    assert "real-scanned-contract-ja.pdf" not in blocker_text


def test_file_processing_staging_cli_blocks_non_required_real_world_policy() -> None:
    """real-world policy を設定しても promotion 必須でなければ gate 弱体化として止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["staging_dataset_policy"] = {
        "required_for_promotion": False,
        "min_real_world_cases": 0,
        "required_source_kinds": [],
        "required_scenarios": [],
        "required_fixture_prefix": "staging/",
    }
    report = _promotion_ready_staging_report("non-required-real-world-policy")

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    assert payload["promotion_ready"] is False
    assert {
        "code": "staging_dataset_policy_not_required",
        "real_world_case_count": 0,
        "compliant_real_world_case_count": 0,
    } in payload["promotion_blockers"]


def test_file_processing_staging_cli_blocks_weakened_promotion_policy() -> None:
    """promotion policy 自体を緩めても production promotion は通さない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["staging_policy"] = {
        "required_for_promotion": False,
        "pending_checks_block_promotion": False,
        "required_runtime_checks": [],
    }
    report = FileProcessingStagingReport(
        run_id="weakened-promotion-policy",
        knowledge_base_id=None,
        runtime_checks=(
            FileProcessingStagingRuntimeCheckResult(
                check="extraction_artifact_cache_roundtrip",
                status="ok",
                evidence={
                    "object_ref_hash": "artifact:oci",
                    "object_uri_scheme": "oci",
                    "payload_bytes": 68,
                    "cleanup": "deleted",
                },
            ),
        ),
        case_results=(),
        metrics={
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "parser_fallback_rate": 0.0,
            "bbox_citation_coverage": 1.0,
            "bbox_coordinate_validity_coverage": 1.0,
            "preview_addressability_coverage": 1.0,
            "backend_source_kind_coverage": 1.0,
        },
        metric_evidence={
            "backend_source_kind_coverage": {
                "covered_source_kinds": ["pdf", "office", "html", "email", "image"],
            },
            "segment_artifact_reuse": {
                "full_artifact_cached_case_count": 1,
                "full_artifact_identity_present_case_count": 1,
                "segment_cache_miss_count": 0,
                "rewritten_successful_segment_artifact_count": 0,
            },
        },
    )

    payload = file_processing_staging_cli._report_payload(
        report,
        manifest=manifest,
        settings=Settings(rag_parser_adapter_backend="local"),
    )

    blocker_codes = {blocker["code"] for blocker in payload["promotion_blockers"]}
    assert payload["passed"] is True
    assert payload["promotion_ready"] is False
    assert "promotion_policy_not_required" in blocker_codes
    assert "promotion_policy_pending_checks_not_blocking" in blocker_codes
    assert "promotion_policy_required_runtime_check_missing" in blocker_codes


def test_file_processing_staging_cli_fails_preflight_when_selected_adapter_missing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """明示選択された parser adapter が未導入なら実 staging 前に失敗する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-preflight.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: _complete_oci_settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    async def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("real staging runner must not be called")

    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_file_processing_staging_checks_with_real_clients",
        fail_if_called,
    )

    exit_code = file_processing_staging_cli.main([str(manifest_path), "--output", str(output_path)])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["preflight"]["ok"] is True
    assert payload["failure_count"] == 1
    assert payload["parser_adapters"]["adapter_backend"] == "docling"
    assert payload["parser_adapter_scorecard"]["recommended_backend"] == "local"
    assert payload["parser_adapter_preflight"] == {
        "ok": False,
        "message": "selected parser adapter is not ready",
        "failures": [
            {
                "backend": "docling",
                "status": "missing",
                "warning_code": "adapter_package_missing",
            }
        ],
    }


def test_file_processing_staging_cli_fails_preflight_when_selected_adapter_flag_disabled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """adapter backend を明示しても feature flag が false なら staging 前に失敗する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-preflight.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: _complete_oci_settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (True, "1.0.0", "docling"),
    )

    async def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("real staging runner must not be called")

    monkeypatch.setattr(
        file_processing_staging_cli,
        "run_file_processing_staging_checks_with_real_clients",
        fail_if_called,
    )

    exit_code = file_processing_staging_cli.main([str(manifest_path), "--output", str(output_path)])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["preflight"]["ok"] is True
    assert payload["failure_count"] == 1
    assert payload["parser_adapters"]["adapter_backend"] == "docling"
    assert payload["parser_adapters"]["effective_order"] == []
    assert payload["parser_adapter_preflight"] == {
        "ok": False,
        "message": "selected parser adapter feature flag is disabled",
        "failures": [
            {
                "backend": "docling",
                "status": "disabled",
                "warning_code": "adapter_feature_flag_disabled",
            }
        ],
    }


def test_file_processing_staging_cli_allows_available_selected_adapter(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """明示選択 adapter が導入済みなら parser adapter preflight は通す。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-staging-preflight.json"
    monkeypatch.setattr(
        file_processing_staging_cli,
        "get_settings",
        lambda: _complete_oci_settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (True, "1.0.0", "docling"),
    )

    exit_code = file_processing_staging_cli.main(
        [str(manifest_path), "--preflight-only", "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["parser_adapters"]["adapters"][0]["status"] == "active"
    assert payload["parser_adapter_scorecard"]["recommended_backend"] == "docling"
    assert payload["parser_adapter_preflight"] == {
        "ok": True,
        "message": "parser adapter preflight ok",
        "failures": [],
    }


def _promotion_ready_staging_report(run_id: str) -> FileProcessingStagingReport:
    """real-world policy tests が他 gate の不足に引きずられない staging report。"""
    return FileProcessingStagingReport(
        run_id=run_id,
        knowledge_base_id=None,
        runtime_checks=(
            FileProcessingStagingRuntimeCheckResult(
                check="extraction_artifact_cache_roundtrip",
                status="ok",
            ),
        ),
        case_results=(),
        metrics={
            "retrieval_recall": 1.0,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "citation_traceability_coverage": 1.0,
            "bbox_citation_coverage": 1.0,
            "bbox_coordinate_validity_coverage": 1.0,
            "preview_addressability_coverage": 1.0,
            "element_lineage_coverage": 1.0,
            "chunk_block_integrity": 1.0,
            "reading_order_consistency": 1.0,
            "structural_section_coverage": 1.0,
            "dependency_context_recall": 1.0,
            "table_structure_fidelity": 1.0,
            "table_cell_lineage_coverage": 1.0,
            "table_row_tree_fidelity": 1.0,
            "visual_chunk_metadata_completeness": 1.0,
            "chunk_size_compliance": 1.0,
            "chunk_contextual_coherence": 1.0,
            "cross_page_table_continuity_coverage": 1.0,
            "ingestion_quality_report_completeness": 1.0,
            "parser_warning_taxonomy_coverage": 1.0,
            "parser_routing_accuracy": 1.0,
            "source_kind_coverage": 1.0,
            "backend_source_kind_coverage": 1.0,
            "extraction_page_coverage": 1.0,
            "low_confidence_document_rate": 0.0,
            "failed_segment_rate": 0.0,
            "groundedness": 1.0,
            "parser_fallback_rate": 0.0,
            "ingestion_p95_ms": 1000.0,
        },
        metric_evidence={
            "backend_source_kind_coverage": {
                "covered_source_kinds": ["pdf", "office", "html", "email", "image"],
            },
            "segment_artifact_reuse": {
                "full_artifact_cached_case_count": 1,
                "full_artifact_oci_case_count": 1,
                "full_artifact_identity_present_case_count": 1,
                "full_artifact_readable_case_count": 1,
                "full_artifact_identity_verified_case_count": 1,
                "segment_artifact_expected_count": 1,
                "segment_artifact_oci_uri_count": 1,
                "segment_artifact_non_oci_uri_count": 0,
                "segment_artifact_readable_count": 1,
                "segment_artifact_identity_verified_count": 1,
                "artifact_integrity_error_count": 0,
                "retry_case_count": 1,
                "retained_successful_segment_artifact_count": 1,
                "rewritten_successful_segment_artifact_count": 0,
                "segment_cache_miss_count": 0,
            },
        },
    )


def _contract_passed_case(
    backend: Any,
    source_kind: Any,
    *,
    case_id: str | None = None,
    scenario: str | None = None,
) -> ParserAdapterCompatibilityCase:
    """source/backend の schema remap 成功 case を簡潔に作る。"""
    return ParserAdapterCompatibilityCase(
        backend=backend,
        source_kind=source_kind,
        fixture_name=f"{source_kind}-fixture",
        content_type="application/octet-stream",
        status="passed",
        blocking=False,
        case_id=case_id,
        scenario=scenario,
        parser_backend=backend,
        element_count=1,
        page_count=1,
        reason_codes=("schema_remap_contract_ok",),
    )


def _complete_oci_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "oci_region": "ap-osaka-1",
        "oci_compartment_id": "ocid1.compartment.oc1..example",
        "oci_enterprise_ai_endpoint": "https://enterprise-ai.example.com",
        "oci_enterprise_ai_project_ocid": "ocid1.generativeaiproject.oc1..example",
        "oci_enterprise_ai_api_key": "sk-test-secret",
        "oci_enterprise_ai_default_text_model": "llm-deployment",
        "oci_enterprise_ai_default_vision_model": "vlm-deployment",
        "oracle_user": "rag_user",
        "oracle_password": "oracle-password",
        "oracle_dsn": "adb.example.com/rag",
        # Wallet mTLS の Wallet がそろっていないと Oracle の readiness は ok にならない（#325）。
        "oracle_wallet_dir": shared_thin_wallet_dir(),
        "object_storage_region": "ap-osaka-1",
        "object_storage_namespace": "namespace",
        "object_storage_bucket": "bucket",
        "upload_storage_backend": "oci",
    }
    values.update(overrides)
    return Settings(**values)


class FakeObjectStorage:
    """Object Storage fake。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.gets: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        assert content_type
        uri = f"oci://namespace/bucket/{key}"
        self.objects[uri] = data
        return uri

    async def get(self, key: str) -> bytes:
        self.gets.append(key)
        if key in self.objects:
            return self.objects[key]
        payload = _fake_extraction_artifact_payload(key)
        if payload is None:
            return self.objects[key]
        self.objects[key] = payload
        return payload

    async def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self.objects.pop(key, None) is not None


class FakeMismatchedArtifactStorage(FakeObjectStorage):
    """artifact cache probe の readback だけを壊す fake。"""

    async def get(self, key: str) -> bytes:
        self.gets.append(key)
        if "/staging-preflight/" in key:
            return b'{"probe":"mismatch"}\n'
        if key in self.objects:
            return self.objects[key]
        payload = _fake_extraction_artifact_payload(key)
        if payload is None:
            return self.objects[key]
        self.objects[key] = payload
        return payload


class FakeUnreadableArtifactStorage(FakeObjectStorage):
    """preflight は通すが ingestion artifact の get だけ失敗させる fake。"""

    async def get(self, key: str) -> bytes:
        self.gets.append(key)
        if "/staging-preflight/" not in key and "/artifacts/" in key:
            raise KeyError(key)
        return self.objects[key]


def _fake_extraction_artifact_payload(key: str) -> bytes | None:
    if "/artifacts/" not in key:
        return None
    match = re.search(r"(doc-\d+)", key)
    if match is None:
        return None
    document_id = match.group(1)
    parser_artifacts: dict[str, object]
    if "/segments/" in key or key.endswith("/sheet1.json"):
        segment_name = key.rsplit("/", 1)[-1].removesuffix(".json")
        if segment_name == "source":
            segment_id = f"{document_id}:source"
            page_start = None
            page_end = None
        elif segment_name == "sheet1":
            segment_id = f"{document_id}:sheet1"
            page_start = 1
            page_end = 1
        else:
            segment_id = (
                segment_name
                if segment_name.startswith(document_id)
                else (f"{document_id}:{segment_name}")
            )
            page_start = None
            page_end = None
        parser_artifacts = {
            "extraction_artifact_schema_version": 1,
            "extraction_artifact_kind": "segment",
            "extraction_artifact_document_id": document_id,
            "extraction_artifact_trace_id": "fake-staging-trace",
            "extraction_artifact_segment_id": segment_id,
        }
        if page_start is not None:
            parser_artifacts["extraction_artifact_page_start"] = page_start
        if page_end is not None:
            parser_artifacts["extraction_artifact_page_end"] = page_end
    else:
        parser_artifacts = {
            "extraction_artifact_schema_version": 1,
            "extraction_artifact_kind": "full",
            "extraction_artifact_document_id": document_id,
            "extraction_artifact_trace_id": "fake-staging-trace",
        }
    return json.dumps(
        {
            "raw_text": "redacted",
            "elements": [],
            "parser_artifacts": parser_artifacts,
        },
        ensure_ascii=False,
    ).encode("utf-8")


class FakeStagingOracle:
    """Oracle fake。"""

    def __init__(self) -> None:
        self.documents: dict[str, DocumentDetail] = {}
        self.chunks: dict[str, list[DocumentChunkView]] = {}
        self.segments: dict[str, list[IngestionSegment]] = {}
        self.knowledge_base_documents: dict[str, set[str]] = {}
        self.counter = 0

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
        default_search_mode: SearchMode = SearchMode.HYBRID,
        retrieval_config: Mapping[str, object] | None = None,
    ) -> KnowledgeBaseDetail:
        del retrieval_config
        now = datetime.now(UTC)
        return KnowledgeBaseDetail(
            id="kb-1",
            name=name,
            description=description,
            status=KnowledgeBaseStatus.ACTIVE,
            default_search_mode=default_search_mode,
            created_at=now,
            updated_at=now,
        )

    async def archive_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail:
        now = datetime.now(UTC)
        return KnowledgeBaseDetail(
            id=knowledge_base_id,
            name="archived",
            status=KnowledgeBaseStatus.ARCHIVED,
            created_at=now,
            updated_at=now,
            archived_at=now,
        )

    async def assign_documents_to_knowledge_base(
        self,
        knowledge_base_id: str,
        document_ids: Sequence[str],
    ) -> KnowledgeBaseDetail:
        self.knowledge_base_documents.setdefault(knowledge_base_id, set()).update(document_ids)
        return await self.archive_knowledge_base(knowledge_base_id)

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
        self.counter += 1
        document = DocumentDetail(
            id=f"doc-{self.counter}",
            file_name=file_name,
            status=FileStatus.UPLOADED,
            content_type=content_type,
            file_size_bytes=file_size_bytes,
            content_sha256=content_sha256,
            duplicate_of_document_id=duplicate_of_document_id,
            uploaded_at=datetime.now(UTC),
            object_storage_path=object_storage_path,
        )
        self.documents[document.id] = document
        for knowledge_base_id in knowledge_base_ids or []:
            self.knowledge_base_documents.setdefault(knowledge_base_id, set()).add(document.id)
        return document

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        return self.documents.get(document_id)

    async def delete_document(self, document_id: str) -> bool:
        self.documents.pop(document_id, None)
        self.chunks.pop(document_id, None)
        self.segments.pop(document_id, None)
        return True

    async def count_document_chunks(self, document_id: str) -> int:
        return len(self.chunks.get(document_id, []))

    async def list_document_chunks(self, document_id: str) -> list[DocumentChunkView]:
        return list(self.chunks.get(document_id, []))

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        return list(self.segments.get(document_id, []))

    def set_indexed(
        self,
        document_id: str,
        *,
        chunks: list[DocumentChunkView],
        segments: list[IngestionSegment] | None = None,
        pages: list[dict[str, object]] | None = None,
        elements: list[dict[str, object]] | None = None,
        tables: list[dict[str, object]] | None = None,
        assets: list[dict[str, object]] | None = None,
    ) -> None:
        document = self.documents[document_id]
        self.documents[document_id] = document.model_copy(
            update={
                "status": FileStatus.INDEXED,
                "extraction": {
                    "pages": pages or [],
                    "elements": elements or [],
                    "tables": tables or [],
                    "assets": assets or [],
                    "parser_artifacts": {
                        "staging_fake": True,
                        "extraction_artifact_schema_version": 1,
                        "extraction_artifact_kind": "full",
                        "extraction_artifact_document_id": document_id,
                        "extraction_artifact_trace_id": "fake-staging-trace",
                        "extraction_artifact_path": (
                            f"oci://namespace/bucket/artifacts/extractions/{document_id}/full.json"
                        ),
                    },
                    "quality_report": {
                        "parser_profile": "enterprise_ai_pdf_layout",
                        "parser_backend": "enterprise_ai",
                        "parser_version": "v1",
                        "fallback_used": False,
                        "risk_level": "low",
                        "page_count": 1,
                        "page_coverage": 1.0,
                        "table_count": len(tables or []),
                        "figure_count": len(assets or []),
                        "formula_count": 0,
                        "element_count": len(elements or []),
                        "low_confidence_count": 0,
                        "failed_segment_count": 0,
                        "long_document": False,
                        "quality_warnings": [],
                    },
                },
            }
        )
        self.chunks[document_id] = chunks
        self.segments[document_id] = segments or []

    def set_error_segments(self, document_id: str) -> None:
        document = self.documents[document_id]
        self.documents[document_id] = document.model_copy(update={"status": FileStatus.ERROR})
        previous = {segment.segment_id: segment for segment in self.segments.get(document_id, [])}
        succeeded_id = f"{document_id}:sheet1"
        failed_id = f"{document_id}:sheet2"
        previous_succeeded = previous.get(succeeded_id)
        previous_failed = previous.get(failed_id)
        succeeded_attempt = previous_succeeded.attempt_count if previous_succeeded else 1
        failed_attempt = previous_failed.attempt_count if previous_failed else 1
        self.segments[document_id] = [
            IngestionSegment(
                segment_id=succeeded_id,
                document_id=document_id,
                status="SUCCEEDED",
                parser_backend="local_partition",
                parser_profile="local_office_structure",
                page_start=1,
                page_end=1,
                attempt_count=succeeded_attempt,
                artifact_path=f"oci://namespace/bucket/artifacts/{document_id}/sheet1.json",
            ),
            IngestionSegment(
                segment_id=failed_id,
                document_id=document_id,
                status="FAILED",
                parser_backend="local_partition",
                parser_profile="local_office_structure",
                page_start=2,
                page_end=2,
                attempt_count=(failed_attempt or 0) + 1,
                error_code="office_segment_parse_failed",
            ),
        ]


class FakeReprocessingStagingOracle(FakeStagingOracle):
    """retry 時に成功済み segment も再処理してしまう fake。"""

    def set_error_segments(self, document_id: str) -> None:
        had_previous_segments = bool(self.segments.get(document_id))
        super().set_error_segments(document_id)
        if not had_previous_segments:
            return
        self.segments[document_id] = [
            (
                segment.model_copy(update={"attempt_count": (segment.attempt_count or 0) + 1})
                if segment.status == "SUCCEEDED"
                else segment
            )
            for segment in self.segments[document_id]
        ]


class FakeIngestion:
    """Ingestion fake。"""

    def __init__(
        self,
        oracle: FakeStagingOracle,
        *,
        image_has_bbox: bool = True,
        image_has_element_ids: bool = True,
        image_bbox: list[float] | None = None,
        image_bbox_coordinate_mode: str | None = "xyxy",
        image_bbox_unit: str | None = None,
        image_page_size: Mapping[str, object] | None = None,
        image_chunk_element_ids: list[str] | None = None,
        image_extraction_element_ids: list[str] | None = None,
        image_table_cell_bbox: list[float] | None = None,
        image_table_cell_bbox_unit: str | None = None,
    ) -> None:
        self.oracle = oracle
        self.image_has_bbox = image_has_bbox
        self.image_has_element_ids = image_has_element_ids
        self.image_bbox = image_bbox or [0.1, 0.1, 0.3, 0.2]
        self.image_bbox_coordinate_mode = image_bbox_coordinate_mode
        self.image_bbox_unit = image_bbox_unit
        self.image_page_size = image_page_size
        self.image_chunk_element_ids = image_chunk_element_ids
        self.image_extraction_element_ids = image_extraction_element_ids
        self.image_table_cell_bbox = image_table_cell_bbox
        self.image_table_cell_bbox_unit = image_table_cell_bbox_unit

    async def ingest(
        self,
        document_id: str,
        image_bytes: bytes,
        prompt: str,
        *,
        content_type: str = "application/octet-stream",
        source_profile: SourceProfile | None = None,
        chunk_set_id: str | None = None,
    ) -> DocumentDetail:
        del image_bytes, prompt, source_profile, chunk_set_id
        document = self.oracle.documents[document_id]
        if document.file_name == "broken.xlsx":
            self.oracle.set_error_segments(document_id)
            raise RuntimeError("safe fake parse failure")
        chunks = self._chunks(document_id, document.file_name)
        self.oracle.set_indexed(
            document_id,
            chunks=chunks,
            segments=[
                IngestionSegment(
                    segment_id=f"{document_id}:source",
                    document_id=document_id,
                    status="SUCCEEDED",
                    artifact_path=(
                        "oci://namespace/bucket/artifacts/extractions/"
                        f"{document_id}/segments/source.json"
                    ),
                )
            ],
            pages=self._pages(document.file_name),
            elements=self._elements(document.file_name),
            tables=self._tables(document.file_name),
        )
        return self.oracle.documents[document_id]

    def _has_bbox(self, file_name: str) -> bool:
        if file_name == "receipt-ja.png":
            return self.image_has_bbox
        return file_name.endswith(".pdf")

    def _chunks(self, document_id: str, file_name: str) -> list[DocumentChunkView]:
        if file_name == "manual.html":
            return [
                _chunk(
                    document_id,
                    chunk_index=0,
                    content_kind="text",
                    page_start=1,
                    bbox=None,
                    section_path="検索運用マニュアル > インデックス確認",
                    metadata={
                        "section_path": "検索運用マニュアル > インデックス確認",
                        "section_title": "インデックス確認",
                    },
                    element_ids=["html-index"],
                ),
                _chunk(
                    document_id,
                    chunk_index=1,
                    content_kind="text",
                    page_start=1,
                    bbox=None,
                    section_path="検索運用マニュアル > 引用確認",
                    metadata={
                        **self._chunk_metadata(file_name),
                        "section_path": "検索運用マニュアル > 引用確認",
                        "section_title": "引用確認",
                    },
                    element_ids=self._chunk_element_ids(file_name),
                ),
            ]
        return [
            _chunk(
                document_id,
                content_kind=_content_kind(file_name),
                page_start=_page_start(file_name),
                bbox=self._bbox(file_name),
                metadata=self._chunk_metadata(file_name),
                element_ids=self._chunk_element_ids(file_name),
            )
        ]

    def _bbox(self, file_name: str) -> list[float] | None:
        if not self._has_bbox(file_name):
            return None
        if file_name == "receipt-ja.png":
            return self.image_bbox
        return [0.1, 0.1, 0.3, 0.2]

    def _chunk_metadata(self, file_name: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        chunk_template = _fake_chunk_template(file_name)
        if chunk_template:
            metadata["chunk_template"] = chunk_template
        if file_name == "manual.html":
            metadata["parent_element_ids"] = "fig-1"
            metadata["dependency_edges"] = '[{"child_id":"fig-1-caption","parent_id":"fig-1"}]'
            metadata["context_dependency_promoted"] = "true"
            metadata["context_dependency_reason"] = "child_of_anchor"
            metadata["context_dependency_shared_element_ids"] = "fig-1"
            return metadata
        if file_name in {"long-table-expenses.xlsx", "long-table-expenses.tsv"}:
            metadata["table_cell_refs"] = "D4"
            metadata["table_cell_ref_format"] = "a1"
            metadata["table_id"] = "expenses-table-1"
            return metadata
        if not self._has_bbox(file_name):
            return metadata
        if file_name == "receipt-ja.png":
            if self.image_bbox_coordinate_mode is not None:
                metadata["bbox_coordinate_mode"] = self.image_bbox_coordinate_mode
            metadata["bbox_unit"] = self.image_bbox_unit or _bbox_unit_for_fake(self.image_bbox)
            return metadata
        metadata["bbox_coordinate_mode"] = "xyxy"
        metadata["bbox_unit"] = "ratio"
        return metadata

    def _pages(self, file_name: str) -> list[dict[str, object]]:
        if file_name == "receipt-ja.png" and self.image_page_size is not None:
            return [
                {
                    "page_number": 1,
                    "element_ids": self._extraction_element_ids(file_name),
                    **dict(self.image_page_size),
                }
            ]
        return []

    def _has_element_ids(self, file_name: str) -> bool:
        if file_name == "receipt-ja.png":
            return self.image_has_element_ids
        return True

    def _chunk_element_ids(self, file_name: str) -> list[str]:
        if not self._has_element_ids(file_name):
            return []
        if file_name == "manual.html":
            return ["fig-1", "fig-1-caption"]
        if file_name == "receipt-ja.png" and self.image_chunk_element_ids is not None:
            return self.image_chunk_element_ids
        return ["el-1"]

    def _elements(self, file_name: str) -> list[dict[str, object]]:
        if file_name == "manual.html":
            return [
                {
                    "element_id": "html-index",
                    "kind": "text",
                    "text": "redacted",
                    "order": 0,
                    "page_number": 1,
                    "section_path": ["検索運用マニュアル", "インデックス確認"],
                },
                {
                    "element_id": "fig-1",
                    "kind": "figure",
                    "text": "redacted",
                    "order": 1,
                    "page_number": 1,
                    "section_path": ["検索運用マニュアル", "引用確認"],
                },
                {
                    "element_id": "fig-1-caption",
                    "parent_id": "fig-1",
                    "kind": "figure_caption",
                    "text": "redacted",
                    "order": 2,
                    "page_number": 1,
                    "section_path": ["検索運用マニュアル", "引用確認"],
                },
            ]
        page_number = _page_start(file_name)
        return [
            {
                "element_id": element_id,
                "kind": "text",
                "text": "redacted",
                "order": index,
                "page_number": page_number,
            }
            for index, element_id in enumerate(self._extraction_element_ids(file_name))
        ]

    def _tables(self, file_name: str) -> list[dict[str, object]]:
        if file_name in {"long-table-expenses.xlsx", "long-table-expenses.tsv"}:
            return [
                {
                    "table_id": "expenses-table-1",
                    "page_number": _page_start(file_name),
                    "metadata": {"table_cell_refs": [{"cell_ref": "D4"}]},
                    "cells": [
                        {
                            "row": 3,
                            "col": 3,
                            "text": "redacted",
                            "metadata": {"cell_ref": "D4"},
                        }
                    ],
                }
            ]
        if file_name != "receipt-ja.png" or self.image_table_cell_bbox is None:
            return []
        metadata: dict[str, str] = {}
        if self.image_table_cell_bbox_unit is not None:
            metadata["bbox_unit"] = self.image_table_cell_bbox_unit
        return [
            {
                "table_id": "receipt-table-1",
                "page_number": 1,
                "cells": [
                    {
                        "row": 0,
                        "col": 0,
                        "text": "redacted",
                        "bbox": self.image_table_cell_bbox,
                        "metadata": metadata,
                    }
                ],
            }
        ]

    def _extraction_element_ids(self, file_name: str) -> list[str]:
        if file_name == "receipt-ja.png" and self.image_extraction_element_ids is not None:
            return self.image_extraction_element_ids
        return ["el-1"]


class FakeMissingTableCellExtractionIngestion(FakeIngestion):
    """table cell citation は残るが extraction 側の cell refs だけ欠落する fake。"""

    def _tables(self, file_name: str) -> list[dict[str, object]]:
        tables = super()._tables(file_name)
        if file_name not in {"long-table-expenses.xlsx", "long-table-expenses.tsv"}:
            return tables

        def cells_without_metadata(table: Mapping[str, object]) -> list[dict[str, object]]:
            raw_cells = table.get("cells")
            if not isinstance(raw_cells, list):
                return []
            return [{**dict(cell), "metadata": {}} for cell in raw_cells if isinstance(cell, dict)]

        return [
            {
                **table,
                "metadata": {},
                "cells": cells_without_metadata(table),
            }
            for table in tables
        ]


class FakeSearch:
    """Search fake。"""

    def __init__(self, oracle: FakeStagingOracle) -> None:
        self.oracle = oracle

    async def run(self, request: SearchRequest) -> SearchResponse:
        knowledge_base_id = request.knowledge_base_ids[0]
        document_ids = set(self.oracle.knowledge_base_documents.get(knowledge_base_id, set()))
        for document_id in list(document_ids):
            document = self.oracle.documents.get(document_id)
            if document is not None and document.duplicate_of_document_id is not None:
                document_ids.add(document.duplicate_of_document_id)
        document_filter = request.filters.get("document_id")
        if document_filter:
            document_ids = {document_filter} & document_ids
        ordered_document_ids = sorted(document_ids)
        if "POLICY" in request.query.upper():
            ordered_document_ids = sorted(
                document_ids,
                key=lambda document_id: (
                    not self.oracle.documents[document_id].file_name.startswith("policy-ja.pdf"),
                    document_id,
                ),
            )
        elif "PAGE 2" in request.query.upper():
            ordered_document_ids = sorted(
                document_ids,
                key=lambda document_id: (
                    "two-column-report-ja.pdf" not in self.oracle.documents[document_id].file_name,
                    document_id,
                ),
            )
        elif "TOTAL" in request.query.upper():
            ordered_document_ids = sorted(
                document_ids,
                key=lambda document_id: (
                    "receipt-ja.png" not in self.oracle.documents[document_id].file_name,
                    document_id,
                ),
            )
        if "構造化 BLOCK CITATION" in request.query.upper():
            citations = [
                _retrieved_chunk_from_view(chunk)
                for document_id in ordered_document_ids
                if document_id in self.oracle.chunks
                and self.oracle.documents[document_id].file_name == "manual.html"
                for chunk in self.oracle.chunks[document_id]
            ]
        else:
            citations = [
                _retrieved_chunk_from_view(self.oracle.chunks[document_id][0])
                for document_id in ordered_document_ids
                if document_id in self.oracle.chunks
            ][:1]
        answer = citations[0].text if citations and "1000" in request.query else "redacted"
        return SearchResponse(
            answer=answer,
            citations=citations,
            trace_id="trace-1",
            elapsed_ms=1.0,
            diagnostics=SearchDiagnostics(citation_count=len(citations)),
        )


class FakeUntraceableSearch(FakeSearch):
    """page だけを返し、element/bbox/section lineage を落とす search fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        citations = [
            RetrievedChunk(
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                score=chunk.score,
                metadata={
                    "page_start": chunk.metadata.get("page_start"),
                    "page_end": chunk.metadata.get("page_end"),
                },
            )
            for chunk in response.citations
        ]
        return response.model_copy(update={"citations": citations})


class FakeTableQaMissSearch(FakeSearch):
    """table citation は返すが回答に期待値を含めない search fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        if "1000" not in request.query:
            return response
        return response.model_copy(update={"answer": "redacted"})


class FakeTableCellRefMissSearch(FakeSearch):
    """table citation と answer は返すが cell-level refs だけを落とす search fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        citations = []
        for chunk in response.citations:
            metadata = dict(chunk.metadata)
            metadata.pop("table_cell_refs", None)
            metadata.pop("cell_refs", None)
            metadata.pop("cell_ref", None)
            metadata.pop("formula_cell_refs", None)
            metadata.pop("formula_cell_ref", None)
            citations.append(chunk.model_copy(update={"metadata": metadata}))
        return response.model_copy(update={"citations": citations})


class FakeStructuredTableCellRefSearch(FakeSearch):
    """adapter 由来の object / JSON 形式 cell refs を citation metadata に返す fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        citations = []
        for chunk in response.citations:
            metadata = dict(chunk.metadata)
            metadata["table_cell_refs"] = [{"cell_ref": "D4"}]
            metadata["formula_cell_refs"] = '[{"formula_cell_ref":"D4"}]'
            citations.append(chunk.model_copy(update={"metadata": metadata}))
        return response.model_copy(update={"citations": citations})


class FakeDependencyLineageMissSearch(FakeSearch):
    """dependency lineage だけを citation metadata から落とす search fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        citations = []
        for chunk in response.citations:
            metadata = dict(chunk.metadata)
            metadata.pop("parent_element_ids", None)
            metadata.pop("dependency_edges", None)
            citations.append(chunk.model_copy(update={"metadata": metadata}))
        return response.model_copy(update={"citations": citations})


class FakeDependencyContextMissSearch(FakeSearch):
    """dependency lineage は残し、context promotion marker だけを落とす search fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        citations = []
        for chunk in response.citations:
            metadata = dict(chunk.metadata)
            metadata.pop("context_dependency_promoted", None)
            metadata.pop("context_dependency_reason", None)
            metadata.pop("context_dependency_shared_element_ids", None)
            citations.append(chunk.model_copy(update={"metadata": metadata}))
        return response.model_copy(update={"citations": citations})


class FakeStructuralSectionMissSearch(FakeSearch):
    """期待 section の一部しか返さない search fake。"""

    async def run(self, request: SearchRequest) -> SearchResponse:
        response = await super().run(request)
        if "構造化 BLOCK CITATION" not in request.query.upper():
            return response
        return response.model_copy(update={"citations": response.citations[:1]})


def _chunk(
    document_id: str,
    *,
    chunk_index: int = 0,
    content_kind: str,
    page_start: int,
    bbox: list[float] | None,
    section_path: str = "section",
    metadata: Mapping[str, str] | None = None,
    element_ids: list[str] | None = None,
) -> DocumentChunkView:
    text = "交通費 1000円" if content_kind in {"table", "sheet"} else "redacted"
    return DocumentChunkView(
        document_id=document_id,
        chunk_id=f"{document_id}:{chunk_index}",
        chunk_index=chunk_index,
        text=text,
        page_start=page_start,
        page_end=page_start,
        bbox=bbox,
        section_path=section_path,
        content_kind=content_kind,
        chunk_group_id=f"{document_id}:group",
        source_parser="staging_fake",
        element_ids=element_ids or [],
        metadata=dict(metadata or {}),
    )


def _fake_chunk_template(file_name: str) -> str | None:
    if file_name.endswith(".pdf"):
        return "pdf_layout"
    if file_name == "receipt-ja.png":
        return "ocr_page"
    if file_name == "manual.html":
        return "html_semantic"
    if file_name.endswith(".eml"):
        return "email_thread"
    if file_name.endswith(".pptx"):
        return "office_slide"
    if file_name.endswith(".docx"):
        return "office_document"
    if file_name.endswith(".xlsx"):
        return "office_sheet"
    if file_name.endswith(".tsv"):
        return "table_preserve_rows"
    if file_name.endswith(".md"):
        return "markdown_by_heading"
    return None


def _bbox_unit_for_fake(bbox: Sequence[float] | None) -> str:
    if not bbox:
        return "ratio"
    max_value = max(abs(float(value)) for value in bbox[:4])
    if max_value <= 1:
        return "ratio"
    if max_value <= 100:
        return "percent"
    return "absolute"


def _retrieved_chunk_from_view(chunk: DocumentChunkView) -> RetrievedChunk:
    return RetrievedChunk(
        document_id=chunk.document_id,
        chunk_id=chunk.chunk_id,
        text=chunk.text,
        score=1.0,
        metadata={
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "bbox": json.dumps(chunk.bbox) if chunk.bbox else None,
            "element_ids": ",".join(chunk.element_ids),
            "content_kind": chunk.content_kind,
            "section_path": chunk.section_path,
            **chunk.metadata,
        },
    )


def _content_kind(file_name: str) -> str:
    if file_name.endswith(".xlsx"):
        return "sheet"
    if file_name.endswith((".csv", ".tsv")):
        return "table"
    if file_name.endswith(".pptx"):
        return "slide"
    return "text"


def _page_start(file_name: str) -> int:
    if file_name == "two-column-report-ja.pdf":
        return 2
    return 1
