"""ファイル処理 golden set 向けの軽量評価指標。"""

import hashlib
import json
import math
import mimetypes
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from pydantic import ValidationError
from rag_parser_core.parsed_table import html_table_layout
from rag_parser_core.source import template_for_source_profile

from app.rag.chunking import Chunk, chunk_extraction
from app.rag.ingestion_quality import (
    HIGH_RISK_WARNING_CODES,
    MEDIUM_RISK_WARNING_CODES,
    build_ingestion_quality_report,
)
from app.rag.parser_adapter_routing import normalize_source_kind
from app.rag.source_profile import build_source_profile
from app.schemas.extraction import DocumentElement, IngestionQualityReport, StructuredExtraction
from app.schemas.search import RetrievedChunk

REQUIRED_FILE_PROCESSING_METRICS = frozenset(
    {
        "retrieval_recall",
        "table_qa_accuracy",
        "page_hit_accuracy",
        "citation_traceability_coverage",
        "bbox_citation_coverage",
        "bbox_coordinate_validity_coverage",
        "preview_addressability_coverage",
        "element_lineage_coverage",
        "chunk_block_integrity",
        "reading_order_consistency",
        "structural_section_coverage",
        "dependency_context_recall",
        "table_structure_fidelity",
        "table_cell_lineage_coverage",
        "table_row_tree_fidelity",
        "visual_chunk_metadata_completeness",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "cross_page_table_continuity_coverage",
        "ingestion_quality_report_completeness",
        "parser_warning_taxonomy_coverage",
        "parser_routing_accuracy",
        "source_kind_coverage",
        "backend_source_kind_coverage",
        "adapter_contract_coverage",
        "extraction_page_coverage",
        "low_confidence_document_rate",
        "failed_segment_rate",
        "groundedness",
        "parser_fallback_rate",
        "ingestion_p95_ms",
    }
)
FILE_PROCESSING_THRESHOLD_DIRECTIONS = {
    "retrieval_recall": "min",
    "table_qa_accuracy": "min",
    "page_hit_accuracy": "min",
    "citation_traceability_coverage": "min",
    "bbox_citation_coverage": "min",
    "bbox_coordinate_validity_coverage": "min",
    "preview_addressability_coverage": "min",
    "element_lineage_coverage": "min",
    "chunk_block_integrity": "min",
    "reading_order_consistency": "min",
    "structural_section_coverage": "min",
    "dependency_context_recall": "min",
    "table_structure_fidelity": "min",
    "table_cell_lineage_coverage": "min",
    "table_row_tree_fidelity": "min",
    "visual_chunk_metadata_completeness": "min",
    "chunk_size_compliance": "min",
    "chunk_contextual_coherence": "min",
    "cross_page_table_continuity_coverage": "min",
    "ingestion_quality_report_completeness": "min",
    "parser_warning_taxonomy_coverage": "min",
    "parser_routing_accuracy": "min",
    "source_kind_coverage": "min",
    "backend_source_kind_coverage": "min",
    "adapter_contract_coverage": "min",
    "extraction_page_coverage": "min",
    "low_confidence_document_rate": "max",
    "failed_segment_rate": "max",
    "groundedness": "min",
    "parser_fallback_rate": "max",
    "ingestion_p95_ms": "max",
}

REQUIRED_FILE_PROCESSING_SCENARIOS = frozenset(
    {
        "scanned_pdf_ocr",
        "two_column_pdf_reading_order",
        "long_table_row_groups",
        "cross_page_table_continuity",
        "japanese_docx_layout",
        "japanese_pptx_slides",
        "japanese_xlsx_sheets",
        "html_semantic_blocks",
        "markdown_code_formula_blocks",
        "email_thread_headers",
        "image_ocr_bbox",
        "duplicate_file_canonical_kb",
        "corrupted_file_partial_failure",
        "legacy_office_unsupported",
        "tiff_image_unsupported",
    }
)
REQUIRED_FILE_PROCESSING_SOURCE_KINDS = frozenset(
    {
        "pdf",
        "image",
        "office",
        "html",
        "email",
        "text",
    }
)
REQUIRED_ADAPTER_SCHEMA_REMAP_SOURCE_KINDS = frozenset({"pdf", "image", "office", "html", "email"})
REQUIRED_FILE_PROCESSING_SCENARIO_CHECKS: Mapping[str, frozenset[str]] = {
    "scanned_pdf_ocr": frozenset(
        {"ocr_text", "page_coverage", "citation_traceability", "quality_report_metadata"}
    ),
    "two_column_pdf_reading_order": frozenset(
        {
            "reading_order",
            "page_hit_accuracy",
            "citation_traceability",
            "quality_report_metadata",
        }
    ),
    "long_table_row_groups": frozenset(
        {
            "reading_order",
            "table_preserve_rows",
            "table_qa_accuracy",
            "element_lineage",
            "chunk_block_integrity",
            "table_structure_fidelity",
            "table_cell_lineage",
            "table_row_tree_fidelity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "cross_page_table_continuity": frozenset(
        {
            "reading_order",
            "table_preserve_rows",
            "element_lineage",
            "chunk_block_integrity",
            "visual_chunk_metadata",
            "table_row_tree_fidelity",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "cross_page_table_continuity",
            "quality_report_metadata",
        }
    ),
    "japanese_docx_layout": frozenset(
        {
            "heading_structure",
            "paragraph_order",
            "reading_order",
            "element_lineage",
            "chunk_block_integrity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "japanese_pptx_slides": frozenset(
        {
            "slide_segment",
            "reading_order",
            "citation_traceability",
            "element_lineage",
            "chunk_block_integrity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "japanese_xlsx_sheets": frozenset(
        {
            "sheet_segment",
            "reading_order",
            "table_preserve_rows",
            "element_lineage",
            "chunk_block_integrity",
            "table_structure_fidelity",
            "table_cell_lineage",
            "table_row_tree_fidelity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "html_semantic_blocks": frozenset(
        {
            "heading_structure",
            "reading_order",
            "section_path",
            "structural_section_coverage",
            "citation_traceability",
            "dependency_lineage",
            "dependency_context_recall",
            "chunk_block_integrity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "markdown_code_formula_blocks": frozenset(
        {
            "heading_structure",
            "reading_order",
            "code_block",
            "equation_block",
            "element_lineage",
            "chunk_block_integrity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "email_thread_headers": frozenset(
        {
            "email_headers",
            "thread_body",
            "attachment_metadata",
            "reading_order",
            "chunk_block_integrity",
            "visual_chunk_metadata",
            "chunk_size_compliance",
            "chunk_contextual_coherence",
            "quality_report_metadata",
        }
    ),
    "image_ocr_bbox": frozenset(
        {
            "ocr_text",
            "bbox_citation",
            "bbox_coordinate_validity",
            "preview_jump",
            "quality_report_metadata",
        }
    ),
    "tiff_image_unsupported": frozenset(
        {"unsupported_reason", "safe_error", "parser_warning_taxonomy"}
    ),
    "duplicate_file_canonical_kb": frozenset(
        {
            "canonical_alias",
            "knowledge_base_membership",
            "searchable_canonical",
            "quality_report_metadata",
        }
    ),
    "corrupted_file_partial_failure": frozenset(
        {
            "failed_segment_status",
            "artifact_reuse",
            "safe_error",
            "parser_warning_taxonomy",
        }
    ),
    "legacy_office_unsupported": frozenset(
        {"unsupported_reason", "safe_error", "parser_warning_taxonomy"}
    ),
}

_REQUIRED_CASE_FIELDS = frozenset(
    {
        "id",
        "fixture",
        "modality",
        "scenario",
        "expected_parser_profile",
        "expected_chunk_template",
        "required_checks",
    }
)
_CASE_ASSERTION_FIELDS = frozenset(
    {
        "expected_content_kind",
        "expected_warning",
        "expected_answer",
        "expected_pages",
        "expected_sections",
        "expected_unsupported_reason",
    }
)
_SUPPORTED_MODALITIES = frozenset(
    {
        "pdf",
        "image",
        "text",
        "markdown",
        "json",
        "csv",
        "tsv",
        "html",
        "email",
        "office",
    }
)
_OPTIONAL_FIXTURE_FIELDS = ("duplicate_fixture",)
_REAL_WORLD_FIXTURE_KIND = "real_world"
_DEFAULT_REAL_WORLD_FIXTURE_PREFIX = "staging/"
_ADAPTER_SCHEMA_REMAP_FIELD = "adapter_schema_remap"
_FIXTURE_EXTENSIONS_BY_MODALITY = {
    "pdf": frozenset({".pdf"}),
    "image": frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}),
    "text": frozenset({".txt"}),
    "markdown": frozenset({".md", ".markdown"}),
    "json": frozenset({".json"}),
    "csv": frozenset({".csv"}),
    "tsv": frozenset({".tsv"}),
    "html": frozenset({".html", ".htm", ".xhtml"}),
    "email": frozenset({".eml", ".msg"}),
    "office": frozenset({".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"}),
}
_STAGING_ONLY_CHECKS = frozenset(
    {
        "artifact_reuse",
        "knowledge_base_membership",
        "page_hit_accuracy",
        "dependency_context_recall",
        "searchable_canonical",
    }
)
_WARNING_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_KNOWN_FILE_PROCESSING_WARNING_CODES = frozenset(
    {
        *HIGH_RISK_WARNING_CODES,
        *MEDIUM_RISK_WARNING_CODES,
        "duplicate_content",
        "low_confidence_elements",
        "office_local_parse_failed",
        "office_local_parse_empty",
        "office_segment_parse_failed",
    }
)
_WARNING_CODE_BY_UNSUPPORTED_REASON = {
    "outlook_msg_not_supported": "unsupported_outlook_msg",
    "tiff_image_not_supported": "unsupported_tiff_image",
    "legacy_office_binary_not_supported": "unsupported_legacy_office_binary",
    "odt_rtf_not_supported": "unsupported_odt_rtf",
}
_QUALITY_REPORT_REQUIRED_FIELDS = frozenset(
    {
        "parser_profile",
        "parser_backend",
        "parser_version",
        "fallback_used",
        "risk_level",
        "page_count",
        "page_coverage",
        "table_count",
        "figure_count",
        "formula_count",
        "element_count",
        "low_confidence_count",
        "failed_segment_count",
        "long_document",
        "quality_warnings",
    }
)
_QUALITY_REPORT_COUNT_FIELDS = (
    "page_count",
    "table_count",
    "figure_count",
    "formula_count",
    "element_count",
    "low_confidence_count",
    "failed_segment_count",
)
_QUALITY_REPORT_SENSITIVE_KEYS = frozenset(
    {"raw_text", "text", "content", "document_text", "ocr_text", "page_text"}
)
_FIGURE_ASSET_KINDS = frozenset({"figure", "image", "picture", "chart", "diagram"})


@dataclass(frozen=True)
class TableQaResult:
    """表 QA の期待値と実回答。"""

    case_id: str
    expected_answer: str
    actual_answer: str


@dataclass(frozen=True)
class PageHitCase:
    """citation が当てるべき document/page。"""

    case_id: str
    expected_document_id: str
    expected_pages: tuple[int, ...]


@dataclass(frozen=True)
class FileProcessingContractCaseResult:
    """file-processing manifest の 1 case に対する local contract 結果。"""

    case_id: str
    fixture: str
    source_kind: str = ""
    parser_profile: str = ""
    parser_backend: str = ""
    chunk_template: str = ""
    fallback_used: bool = False
    page_coverage: float | None = None
    low_confidence_count: int = 0
    failed_segment_count: int = 0
    passed_checks: tuple[str, ...] = ()
    pending_checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileProcessingContractReport:
    """file-processing manifest の local contract 集計結果。"""

    manifest_errors: tuple[str, ...] = ()
    case_results: tuple[FileProcessingContractCaseResult, ...] = ()

    @property
    def passed(self) -> bool:
        """local contract と manifest/asset 契約に失敗がないか。"""
        return not self.manifest_errors and not any(result.failures for result in self.case_results)

    @property
    def case_count(self) -> int:
        return len(self.case_results)

    @property
    def failure_count(self) -> int:
        return len(self.manifest_errors) + sum(len(result.failures) for result in self.case_results)

    @property
    def pending_staging_check_count(self) -> int:
        return sum(len(result.pending_checks) for result in self.case_results)


@dataclass(frozen=True)
class FileProcessingStagingRequirement:
    """staging で閉じるべき file-processing pending check。"""

    case_id: str
    scenario: str
    fixture: str
    check: str
    reason: str
    required_evidence: tuple[str, ...]
    suggested_gate: str


@dataclass(frozen=True)
class FileProcessingMetricThresholdResult:
    """file-processing metric threshold の評価結果。"""

    metric: str
    direction: str
    threshold: float
    actual: float | None
    status: str
    passed: bool
    reason: str | None = None


def validate_file_processing_manifest(
    manifest: Mapping[str, object],
) -> tuple[str, ...]:
    """file-processing golden set manifest の最低契約を検証する。"""
    errors: list[str] = []

    metrics = _string_set(manifest.get("metrics"))
    missing_metrics = REQUIRED_FILE_PROCESSING_METRICS - metrics
    if missing_metrics:
        errors.append("missing_metrics:" + ",".join(sorted(missing_metrics)))
    errors.extend(_validate_metric_threshold_contract(manifest.get("thresholds")))

    raw_cases = manifest.get("cases")
    if not _is_sequence(raw_cases):
        errors.append("cases:not_sequence")
        return tuple(errors)

    scenarios: set[str] = set()
    source_kinds: set[str] = set()
    seen_case_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            errors.append(f"case[{index}]:not_mapping")
            continue
        case = _mapping(raw_case)
        case_id = _string_value(case.get("id"))
        case_label = case_id or str(index)
        if case_id in seen_case_ids:
            errors.append(f"case[{case_label}]:duplicate_id")
        if case_id:
            seen_case_ids.add(case_id)

        missing_fields = _missing_required_case_fields(case)
        if missing_fields:
            errors.append(f"case[{case_label}]:missing_fields:{','.join(missing_fields)}")

        scenario = _string_value(case.get("scenario"))
        if scenario:
            scenarios.add(scenario)

        modality = _string_value(case.get("modality"))
        if modality and modality not in _SUPPORTED_MODALITIES:
            errors.append(f"case[{case_label}]:unsupported_modality:{modality}")
        if modality:
            source_kinds.add(normalize_source_kind(modality))

        required_checks = _string_set(case.get("required_checks"))
        if not required_checks:
            errors.append(f"case[{case_label}]:required_checks_empty")
        scenario_required_checks = REQUIRED_FILE_PROCESSING_SCENARIO_CHECKS.get(scenario)
        if scenario_required_checks:
            missing_required_checks = scenario_required_checks - required_checks
            if missing_required_checks:
                errors.append(
                    f"case[{case_label}]:missing_required_checks:"
                    + ",".join(sorted(missing_required_checks))
                )

        if not any(_has_case_assertion(case, field) for field in _CASE_ASSERTION_FIELDS):
            errors.append(f"case[{case_label}]:assertion_missing")

    errors.extend(_validate_staging_dataset_policy(manifest, raw_cases))
    errors.extend(_validate_adapter_schema_remap_contract(raw_cases))

    missing_scenarios = REQUIRED_FILE_PROCESSING_SCENARIOS - scenarios
    if missing_scenarios:
        errors.append("missing_scenarios:" + ",".join(sorted(missing_scenarios)))
    missing_source_kinds = REQUIRED_FILE_PROCESSING_SOURCE_KINDS - source_kinds
    if missing_source_kinds:
        errors.append("missing_source_kinds:" + ",".join(sorted(missing_source_kinds)))

    return tuple(errors)


def _validate_adapter_schema_remap_contract(
    raw_cases: Sequence[object],
) -> tuple[str, ...]:
    """strict adapter smoke が使う schema-remap fixture 宣言を検証する。"""
    errors: list[str] = []
    declared_source_kinds: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            continue
        case = _mapping(raw_case)
        if case.get(_ADAPTER_SCHEMA_REMAP_FIELD) is not True:
            continue
        case_id = _string_value(case.get("id")) or str(index)
        source_kind = normalize_source_kind(case.get("modality"))
        if source_kind not in REQUIRED_ADAPTER_SCHEMA_REMAP_SOURCE_KINDS:
            errors.append(f"case[{case_id}]:adapter_schema_remap_unsupported_source:{source_kind}")
            continue
        declared_source_kinds.add(source_kind)
        if not _case_requires_adapter_schema_remap_smoke(case):
            errors.append(f"case[{case_id}]:adapter_schema_remap_not_positive_fixture")
        if not _string_value(case.get("fixture")):
            errors.append(f"case[{case_id}]:adapter_schema_remap_fixture_missing")
        if not _string_value(case.get("scenario")):
            errors.append(f"case[{case_id}]:adapter_schema_remap_scenario_missing")

    missing_source_kinds = REQUIRED_ADAPTER_SCHEMA_REMAP_SOURCE_KINDS - declared_source_kinds
    if missing_source_kinds:
        errors.append(
            "adapter_schema_remap:missing_source_kinds:" + ",".join(sorted(missing_source_kinds))
        )
    return tuple(errors)


def _case_requires_adapter_schema_remap_smoke(case: Mapping[str, object]) -> bool:
    """negative/unsupported staging cases を adapter schema-remap smoke から外す。"""
    if _string_value(case.get("expected_warning")):
        return False
    if _string_value(case.get("expected_unsupported_reason")):
        return False
    if _string_value(case.get("expected_parser_profile")).startswith("unsupported_"):
        return False
    return not _string_value(case.get("expected_chunk_template")).startswith("unsupported_")


def evaluate_file_processing_metric_thresholds(
    metric_summary: Mapping[str, object],
    thresholds: Mapping[str, object] | None,
) -> tuple[FileProcessingMetricThresholdResult, ...]:
    """manifest thresholds を local/staging の metric summary に適用する。"""
    if not thresholds:
        return ()
    results: list[FileProcessingMetricThresholdResult] = []
    for metric, threshold_config in thresholds.items():
        direction = FILE_PROCESSING_THRESHOLD_DIRECTIONS.get(metric)
        if direction is None:
            continue
        threshold = _threshold_value(threshold_config, direction)
        if threshold is None:
            continue
        summary = _mapping(metric_summary.get(metric))
        actual = _optional_float(summary.get("value"))
        if actual is None:
            actual = _optional_float(metric_summary.get(metric))
        if actual is None:
            results.append(
                FileProcessingMetricThresholdResult(
                    metric=metric,
                    direction=direction,
                    threshold=threshold,
                    actual=None,
                    status="pending",
                    passed=True,
                    reason="metric_value_unavailable",
                )
            )
            continue
        passed = actual >= threshold if direction == "min" else actual <= threshold
        status = "passed" if passed else "failed"
        results.append(
            FileProcessingMetricThresholdResult(
                metric=metric,
                direction=direction,
                threshold=threshold,
                actual=actual,
                status=status,
                passed=passed,
            )
        )
    return tuple(results)


def _validate_staging_dataset_policy(
    manifest: Mapping[str, object],
    raw_cases: Sequence[object],
) -> tuple[str, ...]:
    """real-world staging fixture の最低限の非機密契約を検証する。"""
    raw_policy = manifest.get("staging_dataset_policy")
    if raw_policy is None:
        return ()
    policy = _mapping(raw_policy)
    if not policy:
        return ("staging_dataset_policy:not_mapping",)

    errors: list[str] = []
    min_real_world_cases = _optional_int(policy.get("min_real_world_cases"))
    if min_real_world_cases is None:
        min_real_world_cases = 1 if policy.get("required_for_promotion") is True else 0
    if min_real_world_cases < 0:
        errors.append("staging_dataset_policy:min_real_world_cases_invalid")
        min_real_world_cases = 0

    required_source_kinds = {
        normalize_source_kind(item) for item in _string_set(policy.get("required_source_kinds"))
    }
    unknown_source_kinds = required_source_kinds - REQUIRED_FILE_PROCESSING_SOURCE_KINDS
    if unknown_source_kinds:
        errors.append(
            "staging_dataset_policy:unknown_source_kinds:" + ",".join(sorted(unknown_source_kinds))
        )

    required_scenarios = _string_set(policy.get("required_scenarios"))
    unknown_scenarios = required_scenarios - REQUIRED_FILE_PROCESSING_SCENARIOS
    if unknown_scenarios:
        errors.append(
            "staging_dataset_policy:unknown_scenarios:" + ",".join(sorted(unknown_scenarios))
        )

    required_fixture_prefix = (
        _string_value(policy.get("required_fixture_prefix")) or _DEFAULT_REAL_WORLD_FIXTURE_PREFIX
    )
    real_world_cases: list[Mapping[str, object]] = []
    real_world_source_kinds: set[str] = set()
    real_world_scenarios: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            continue
        case = _mapping(raw_case)
        if not _is_real_world_case(case):
            continue
        real_world_cases.append(case)
        case_id = _string_value(case.get("id")) or str(index)
        real_world_source_kinds.add(normalize_source_kind(case.get("modality")))
        scenario = _string_value(case.get("scenario"))
        if scenario:
            real_world_scenarios.add(scenario)
        if _string_value(case.get("data_sensitivity")) != "non_sensitive":
            errors.append(f"case[{case_id}]:real_world_data_sensitivity_not_non_sensitive")
        if case.get("reviewed_for_public_ci") is not True:
            errors.append(f"case[{case_id}]:real_world_review_required")
        fixture = _string_value(case.get("fixture"))
        if required_fixture_prefix and not fixture.startswith(required_fixture_prefix):
            errors.append(
                f"case[{case_id}]:real_world_fixture_prefix_mismatch:{required_fixture_prefix}"
            )

    if len(real_world_cases) < min_real_world_cases:
        errors.append(
            "staging_dataset_policy:real_world_cases_insufficient:"
            f"{len(real_world_cases)}/{min_real_world_cases}"
        )

    missing_source_kinds = required_source_kinds - real_world_source_kinds
    if missing_source_kinds:
        errors.append(
            "staging_dataset_policy:missing_source_kinds:" + ",".join(sorted(missing_source_kinds))
        )
    missing_scenarios = required_scenarios - real_world_scenarios
    if missing_scenarios:
        errors.append(
            "staging_dataset_policy:missing_scenarios:" + ",".join(sorted(missing_scenarios))
        )
    return tuple(errors)


def staging_dataset_policy_summary(manifest: Mapping[str, object]) -> dict[str, object]:
    """real-world staging dataset policy の非機密 summary を返す。

    fixture path / case id / OCR/chunk text は出さず、promotion gate が見るべき
    coverage と review 状態だけを artifact に残す。
    """
    raw_policy = manifest.get("staging_dataset_policy")
    raw_cases = manifest.get("cases")
    cases = (
        [case for case in raw_cases if isinstance(case, Mapping)]
        if isinstance(raw_cases, list)
        else []
    )
    if raw_policy is None:
        return {
            "configured": False,
            "required_for_promotion": False,
            "promotion_ready": True,
            "min_real_world_cases": 0,
            "real_world_case_count": sum(1 for case in cases if _is_real_world_case(case)),
            "compliant_real_world_case_count": 0,
            "required_source_kinds": [],
            "covered_source_kinds": [],
            "missing_source_kinds": [],
            "required_scenarios": [],
            "covered_scenarios": [],
            "missing_scenarios": [],
            "policy_error_count": 0,
        }

    policy = _mapping(raw_policy)
    if not policy:
        return {
            "configured": True,
            "required_for_promotion": False,
            "promotion_ready": False,
            "min_real_world_cases": 0,
            "real_world_case_count": 0,
            "compliant_real_world_case_count": 0,
            "required_source_kinds": [],
            "covered_source_kinds": [],
            "missing_source_kinds": [],
            "required_scenarios": [],
            "covered_scenarios": [],
            "missing_scenarios": [],
            "policy_error_count": 1,
        }

    min_real_world_cases = _optional_int(policy.get("min_real_world_cases"))
    if min_real_world_cases is None:
        min_real_world_cases = 1 if policy.get("required_for_promotion") is True else 0
    min_real_world_cases = max(0, min_real_world_cases)
    required_source_kinds = {
        normalize_source_kind(item) for item in _string_set(policy.get("required_source_kinds"))
    }
    valid_required_source_kinds = required_source_kinds & REQUIRED_FILE_PROCESSING_SOURCE_KINDS
    required_scenarios = _string_set(policy.get("required_scenarios"))
    valid_required_scenarios = required_scenarios & REQUIRED_FILE_PROCESSING_SCENARIOS
    required_fixture_prefix = (
        _string_value(policy.get("required_fixture_prefix")) or _DEFAULT_REAL_WORLD_FIXTURE_PREFIX
    )

    real_world_case_count = 0
    compliant_real_world_case_count = 0
    covered_source_kinds: set[str] = set()
    covered_scenarios: set[str] = set()
    non_sensitive_reviewed_case_count = 0
    sensitivity_violation_count = 0
    review_missing_count = 0
    fixture_prefix_mismatch_count = 0
    for raw_case in cases:
        case = _mapping(raw_case)
        if not _is_real_world_case(case):
            continue
        real_world_case_count += 1
        source_kind = normalize_source_kind(case.get("modality"))
        if source_kind in REQUIRED_FILE_PROCESSING_SOURCE_KINDS:
            covered_source_kinds.add(source_kind)
        scenario = _string_value(case.get("scenario"))
        if scenario in REQUIRED_FILE_PROCESSING_SCENARIOS:
            covered_scenarios.add(scenario)
        sensitivity_ok = _string_value(case.get("data_sensitivity")) == "non_sensitive"
        review_ok = case.get("reviewed_for_public_ci") is True
        fixture = _string_value(case.get("fixture"))
        prefix_ok = not required_fixture_prefix or fixture.startswith(required_fixture_prefix)
        if sensitivity_ok and review_ok:
            non_sensitive_reviewed_case_count += 1
        if not sensitivity_ok:
            sensitivity_violation_count += 1
        if not review_ok:
            review_missing_count += 1
        if not prefix_ok:
            fixture_prefix_mismatch_count += 1
        if sensitivity_ok and review_ok and prefix_ok:
            compliant_real_world_case_count += 1

    missing_source_kinds = valid_required_source_kinds - covered_source_kinds
    missing_scenarios = valid_required_scenarios - covered_scenarios
    policy_error_count = len(_validate_staging_dataset_policy(manifest, cases))
    return {
        "configured": True,
        "required_for_promotion": bool(policy.get("required_for_promotion", False)),
        "promotion_ready": policy_error_count == 0,
        "min_real_world_cases": min_real_world_cases,
        "real_world_case_count": real_world_case_count,
        "compliant_real_world_case_count": compliant_real_world_case_count,
        "non_sensitive_reviewed_case_count": non_sensitive_reviewed_case_count,
        "sensitivity_violation_count": sensitivity_violation_count,
        "review_missing_count": review_missing_count,
        "fixture_prefix_mismatch_count": fixture_prefix_mismatch_count,
        "required_fixture_prefix": required_fixture_prefix,
        "required_source_kinds": sorted(valid_required_source_kinds),
        "covered_source_kinds": sorted(covered_source_kinds),
        "missing_source_kinds": sorted(missing_source_kinds),
        "unknown_source_kind_count": len(
            required_source_kinds - REQUIRED_FILE_PROCESSING_SOURCE_KINDS
        ),
        "required_scenarios": sorted(valid_required_scenarios),
        "covered_scenarios": sorted(covered_scenarios),
        "missing_scenarios": sorted(missing_scenarios),
        "unknown_scenario_count": len(required_scenarios - REQUIRED_FILE_PROCESSING_SCENARIOS),
        "policy_error_count": policy_error_count,
    }


def _is_real_world_case(case: Mapping[str, object]) -> bool:
    return (
        _string_value(case.get("fixture_kind")) == _REAL_WORLD_FIXTURE_KIND
        or case.get("real_world") is True
    )


def build_file_processing_staging_plan(
    manifest: Mapping[str, object],
    report: FileProcessingContractReport,
) -> tuple[FileProcessingStagingRequirement, ...]:
    """local contract で pending になった項目を staging 実行計画へ変換する。"""
    raw_cases = manifest.get("cases")
    if not _is_sequence(raw_cases):
        return ()
    case_by_id = {
        _string_value(raw_case.get("id")): _mapping(raw_case)
        for raw_case in raw_cases
        if isinstance(raw_case, Mapping)
    }
    requirements: list[FileProcessingStagingRequirement] = []
    emitted: set[tuple[str, str]] = set()
    for result in report.case_results:
        case = case_by_id.get(result.case_id, {})
        scenario = _string_value(case.get("scenario"))
        for pending in result.pending_checks:
            check, reason = _split_pending_check(pending)
            emitted.add((result.case_id, check))
            requirements.append(
                FileProcessingStagingRequirement(
                    case_id=result.case_id,
                    scenario=scenario,
                    fixture=result.fixture,
                    check=check,
                    reason=reason,
                    required_evidence=_staging_required_evidence(check, reason),
                    suggested_gate=_staging_suggested_gate(check, reason),
                )
            )
    for case_id, case in case_by_id.items():
        if not _table_qa_requires_staging(case):
            continue
        key = (case_id, "table_qa_accuracy")
        if key in emitted:
            continue
        reason = "requires_staging_search_qa"
        requirements.append(
            FileProcessingStagingRequirement(
                case_id=case_id,
                scenario=_string_value(case.get("scenario")),
                fixture=_string_value(case.get("fixture")),
                check="table_qa_accuracy",
                reason=reason,
                required_evidence=_staging_required_evidence("table_qa_accuracy", reason),
                suggested_gate=_staging_suggested_gate("table_qa_accuracy", reason),
            )
        )
    for case_id, case in case_by_id.items():
        if not _structural_section_requires_staging(case):
            continue
        key = (case_id, "structural_section_coverage")
        if key in emitted:
            continue
        reason = "requires_staging_section_search"
        requirements.append(
            FileProcessingStagingRequirement(
                case_id=case_id,
                scenario=_string_value(case.get("scenario")),
                fixture=_string_value(case.get("fixture")),
                check="structural_section_coverage",
                reason=reason,
                required_evidence=_staging_required_evidence(
                    "structural_section_coverage",
                    reason,
                ),
                suggested_gate=_staging_suggested_gate(
                    "structural_section_coverage",
                    reason,
                ),
            )
        )
    return tuple(requirements)


def run_file_processing_contract_checks(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path,
) -> FileProcessingContractReport:
    """同梱 fixture を local parser/chunker で検証する。

    モデルの API、保存先、Oracle AI Database が必要な品質 check は失敗扱いにせず、
    ``pending_checks`` として返す。CI では local contract の退化を検出し、staging/nightly
    では pending check を実データで閉じる。
    """
    manifest_errors = (
        *validate_file_processing_manifest(manifest),
        *validate_file_processing_fixture_assets(manifest, manifest_path=manifest_path),
    )
    if manifest_errors:
        return FileProcessingContractReport(manifest_errors=tuple(manifest_errors))
    raw_cases = manifest.get("cases")
    if not _is_sequence(raw_cases):
        return FileProcessingContractReport(manifest_errors=("cases:not_sequence",))
    fixture_root = _resolve_fixture_root(manifest, manifest_path=manifest_path)
    case_results = [
        _run_file_processing_case_contract(_mapping(raw_case), fixture_root=fixture_root)
        for raw_case in raw_cases
        if isinstance(raw_case, Mapping)
    ]
    return FileProcessingContractReport(case_results=tuple(case_results))


def validate_file_processing_fixture_assets(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path,
) -> tuple[str, ...]:
    """manifest が参照する fixture 実体の存在・基本整合性を検証する。"""
    errors: list[str] = []
    fixture_root = _resolve_fixture_root(manifest, manifest_path=manifest_path)
    if not fixture_root.exists():
        errors.append(f"fixture_root:not_found:{fixture_root}")
    elif not fixture_root.is_dir():
        errors.append(f"fixture_root:not_directory:{fixture_root}")

    raw_cases = manifest.get("cases")
    if not _is_sequence(raw_cases):
        errors.append("cases:not_sequence")
        return tuple(errors)

    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            errors.append(f"case[{index}]:not_mapping")
            continue
        case = _mapping(raw_case)
        case_id = _string_value(case.get("id")) or str(index)
        _validate_fixture_reference(
            case,
            field="fixture",
            fixture_root=fixture_root,
            case_id=case_id,
            errors=errors,
        )
        for field in _OPTIONAL_FIXTURE_FIELDS:
            if _string_value(case.get(field)):
                _validate_fixture_reference(
                    case,
                    field=field,
                    fixture_root=fixture_root,
                    case_id=case_id,
                    errors=errors,
                )

    return tuple(errors)


def _validate_metric_threshold_contract(value: object) -> list[str]:
    errors: list[str] = []
    thresholds = _mapping(value)
    if not thresholds:
        errors.append("thresholds:missing")
        return errors
    missing = set(REQUIRED_FILE_PROCESSING_METRICS) - set(thresholds)
    if missing:
        errors.append("thresholds:missing_metrics:" + ",".join(sorted(missing)))
    for metric, threshold_config in thresholds.items():
        direction = FILE_PROCESSING_THRESHOLD_DIRECTIONS.get(metric)
        if direction is None:
            errors.append(f"thresholds:{metric}:unknown_metric")
            continue
        config = _mapping(threshold_config)
        if not config:
            errors.append(f"thresholds:{metric}:not_mapping")
            continue
        unexpected_keys = sorted(set(config) - {"min", "max"})
        if unexpected_keys:
            errors.append(f"thresholds:{metric}:unknown_keys:{','.join(unexpected_keys)}")
        wrong_direction = "max" if direction == "min" else "min"
        if wrong_direction in config:
            errors.append(f"thresholds:{metric}:wrong_direction:{wrong_direction}")
        threshold = _threshold_value(config, direction)
        if threshold is None:
            errors.append(f"thresholds:{metric}:{direction}_missing_or_invalid")
        elif threshold < 0:
            errors.append(f"thresholds:{metric}:{direction}_negative")
    return errors


def _run_file_processing_case_contract(
    case: Mapping[str, object],
    *,
    fixture_root: Path,
) -> FileProcessingContractCaseResult:
    case_id = _string_value(case.get("id"))
    fixture_name = _string_value(case.get("fixture"))
    fixture_path = fixture_root / fixture_name
    data = fixture_path.read_bytes()
    content_type = _fixture_content_type(fixture_path)
    digest = hashlib.sha256(data).hexdigest()
    source_profile = build_source_profile(
        original_file_name=fixture_name,
        sanitized_file_name=fixture_name,
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256=digest,
        data=data,
    )
    # registry(解析実体)は評価実行時のみ遅延 import する(module import で app.main へ
    # registry を持ち込まないため。backend は通常経路で解析コードを load しない)。
    from rag_parser_core.registry import (
        parse_openxml_office_segment_extractions,
        parse_with_registry,
    )

    parser_result = parse_with_registry(
        data,
        source_profile=source_profile,
        content_type=content_type,
    )
    extraction = parser_result.extraction
    chunks = chunk_extraction(extraction, chunk_size=200, overlap=0) if extraction else []
    segment_result = parse_openxml_office_segment_extractions(
        data,
        source_profile=source_profile,
    )
    quality_report = (
        build_ingestion_quality_report(
            extraction,
            source_profile=source_profile,
            parser_profile=source_profile.parser_profile,
            parser_backend=parser_result.parser_backend,
            parser_version=parser_result.parser_version,
            fallback_used=parser_result.fallback_used,
            failed_segment_count=len(segment_result.failures),
        )
        if extraction is not None
        else None
    )
    failed_segment_count = max(
        quality_report.failed_segment_count if quality_report is not None else 0,
        len(segment_result.failures),
    )
    effective_template = (
        parser_result.template
        if parser_result.unsupported_reason
        or (extraction is not None and parser_result.template != "enterprise_ai_fallback")
        else template_for_source_profile(source_profile)
    )

    passed: list[str] = []
    pending: list[str] = []
    failures: list[str] = []
    _check_expected_parser_profile(case, source_profile.parser_profile, passed, failures)
    _check_expected_chunk_template(case, effective_template, passed, failures)
    _check_expected_content_kind(case, extraction, chunks, passed, pending, failures)
    _check_expected_answer(case, extraction, chunks, passed, pending, failures)
    _check_expected_warning(case, parser_result.warnings, segment_result.failures, passed, failures)
    _check_expected_unsupported_reason(case, parser_result.unsupported_reason, passed, failures)
    _check_expected_pages(case, extraction, passed, pending, failures)
    _check_duplicate_fixture(case, fixture_root, digest, passed, failures)
    for check in _string_set(case.get("required_checks")):
        status, detail = _evaluate_required_file_processing_check(
            check,
            case=case,
            extraction=extraction,
            chunks=chunks,
            warnings=parser_result.warnings,
            unsupported_reason=parser_result.unsupported_reason,
            segment_failures=segment_result.failures,
            duplicate_sha256=digest,
            fixture_root=fixture_root,
            quality_report=quality_report,
            parser_profile=source_profile.parser_profile,
            parser_backend=parser_result.parser_backend,
            parser_version=parser_result.parser_version,
            fallback_used=parser_result.fallback_used,
        )
        if status == "passed":
            passed.append(check)
        elif status == "pending":
            pending.append(f"{check}:{detail}")
        else:
            failures.append(f"{check}:{detail}")
    if _table_qa_requires_staging(case) and "table_qa_accuracy" in passed:
        pending.append("table_qa_accuracy:requires_staging_search_qa")
    if _dependency_lineage_requires_staging(case) and "dependency_lineage" in passed:
        pending.append("dependency_lineage:requires_staging_search_citation")
    if _structural_section_requires_staging(case) and "structural_section_coverage" in passed:
        pending.append("structural_section_coverage:requires_staging_section_search")
    return FileProcessingContractCaseResult(
        case_id=case_id,
        fixture=fixture_name,
        source_kind=normalize_source_kind(case.get("modality")),
        parser_profile=source_profile.parser_profile,
        parser_backend=parser_result.parser_backend,
        chunk_template=effective_template,
        fallback_used=parser_result.fallback_used,
        page_coverage=(
            quality_report.page_coverage
            if quality_report is not None and _is_page_coverage_metric_case(case)
            else None
        ),
        low_confidence_count=(
            quality_report.low_confidence_count if quality_report is not None else 0
        ),
        failed_segment_count=failed_segment_count,
        passed_checks=tuple(_unique_sorted(passed)),
        pending_checks=tuple(_unique_sorted(pending)),
        failures=tuple(_unique_sorted(failures)),
    )


def _fixture_content_type(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(path.name)
    return content_type or "application/octet-stream"


def _is_page_coverage_metric_case(case: Mapping[str, object]) -> bool:
    return bool(_int_set(case.get("expected_pages"))) or "page_coverage" in _string_set(
        case.get("required_checks")
    )


def _check_expected_parser_profile(
    case: Mapping[str, object],
    parser_profile: str,
    passed: list[str],
    failures: list[str],
) -> None:
    expected = _string_value(case.get("expected_parser_profile"))
    if not expected:
        return
    if parser_profile == expected:
        passed.append("expected_parser_profile")
        return
    failures.append(f"expected_parser_profile:{parser_profile}!={expected}")


def _check_expected_chunk_template(
    case: Mapping[str, object],
    chunk_template: str,
    passed: list[str],
    failures: list[str],
) -> None:
    expected = _string_value(case.get("expected_chunk_template"))
    if not expected:
        return
    if chunk_template == expected:
        passed.append("expected_chunk_template")
        return
    failures.append(f"expected_chunk_template:{chunk_template}!={expected}")


def _check_expected_content_kind(
    case: Mapping[str, object],
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
    passed: list[str],
    pending: list[str],
    failures: list[str],
) -> None:
    expected = _string_value(case.get("expected_content_kind"))
    if not expected:
        return
    if extraction is None:
        pending.append("expected_content_kind:requires_enterprise_ai_extraction")
        return
    content_kinds = _content_kinds(extraction, chunks)
    if expected in content_kinds:
        passed.append("expected_content_kind")
        return
    failures.append(f"expected_content_kind:{expected}:not_found")


def _check_expected_answer(
    case: Mapping[str, object],
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
    passed: list[str],
    pending: list[str],
    failures: list[str],
) -> None:
    expected = _string_value(case.get("expected_answer"))
    if not expected:
        return
    if extraction is None:
        pending.append("expected_answer:requires_extraction")
        return
    haystack = _normalized_extraction_and_chunk_text(extraction, chunks)
    if _normalize_answer(expected) in haystack:
        passed.append("expected_answer")
        return
    failures.append("expected_answer:not_found")


def _check_expected_warning(
    case: Mapping[str, object],
    warnings: Sequence[str],
    segment_failures: Sequence[object],
    passed: list[str],
    failures: list[str],
) -> None:
    expected = _string_value(case.get("expected_warning"))
    if not expected:
        return
    failure_codes = {
        _string_value(getattr(failure, "error_code", "")) for failure in segment_failures
    }
    if expected in set(warnings) or expected in failure_codes:
        passed.append("expected_warning")
        return
    failures.append(f"expected_warning:{expected}:not_found")


def _check_expected_unsupported_reason(
    case: Mapping[str, object],
    unsupported_reason: str | None,
    passed: list[str],
    failures: list[str],
) -> None:
    expected = _string_value(case.get("expected_unsupported_reason"))
    if not expected:
        return
    if unsupported_reason == expected:
        passed.append("expected_unsupported_reason")
        return
    failures.append(f"expected_unsupported_reason:{unsupported_reason or '<none>'}!={expected}")


def _check_expected_pages(
    case: Mapping[str, object],
    extraction: StructuredExtraction | None,
    passed: list[str],
    pending: list[str],
    failures: list[str],
) -> None:
    expected_pages = _int_set(case.get("expected_pages"))
    if not expected_pages:
        return
    if extraction is None:
        pending.append("expected_pages:requires_enterprise_ai_extraction")
        return
    actual_pages = {page.page_number for page in extraction.pages} | {
        element.page_number for element in extraction.elements if element.page_number is not None
    }
    if expected_pages <= actual_pages:
        passed.append("expected_pages")
        return
    missing_pages = ",".join(str(page) for page in sorted(expected_pages - actual_pages))
    failures.append(f"expected_pages:missing:{missing_pages}")


def _check_duplicate_fixture(
    case: Mapping[str, object],
    fixture_root: Path,
    digest: str,
    passed: list[str],
    failures: list[str],
) -> None:
    duplicate_fixture = _string_value(case.get("duplicate_fixture"))
    if not duplicate_fixture:
        return
    duplicate_digest = hashlib.sha256((fixture_root / duplicate_fixture).read_bytes()).hexdigest()
    if duplicate_digest == digest:
        passed.append("duplicate_fixture_sha256")
        return
    failures.append("duplicate_fixture_sha256:mismatch")


def _evaluate_required_file_processing_check(
    check: str,
    *,
    case: Mapping[str, object],
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
    warnings: Sequence[str],
    unsupported_reason: str | None,
    segment_failures: Sequence[object],
    duplicate_sha256: str,
    fixture_root: Path,
    quality_report: IngestionQualityReport | None,
    parser_profile: str,
    parser_backend: str,
    parser_version: str,
    fallback_used: bool,
) -> tuple[str, str]:
    if check in _STAGING_ONLY_CHECKS:
        return "pending", "requires_staging_pipeline"
    if check == "ocr_text":
        return _check_non_empty_extraction(extraction, requires="enterprise_ai")
    if check == "reading_order":
        return _check_reading_order(extraction, chunks)
    if check == "page_coverage":
        expected_pages = _int_set(case.get("expected_pages"))
        if extraction is None:
            return "pending", "requires_enterprise_ai_extraction"
        actual_pages = {page.page_number for page in extraction.pages}
        return ("passed", "ok") if expected_pages <= actual_pages else ("failure", "missing_pages")
    if check == "citation_traceability":
        return _check_chunk_traceability(extraction, chunks)
    if check == "bbox_citation":
        return _check_bbox_citation(chunks)
    if check == "bbox_coordinate_validity":
        return _check_bbox_coordinate_validity(chunks)
    if check == "preview_jump":
        return _check_preview_addressability(extraction, chunks)
    if check == "table_preserve_rows":
        return _check_table_preserve_rows(chunks)
    if check == "table_qa_accuracy":
        return _check_expected_answer_as_required(case, extraction, chunks)
    if check == "code_block":
        return _check_content_kind_present(
            extraction,
            chunks,
            expected_kind="code",
            required_metadata_key="code_language",
        )
    if check == "equation_block":
        return _check_content_kind_present(
            extraction,
            chunks,
            expected_kind="equation",
            required_metadata_key="equation_delimiter",
        )
    if check == "element_lineage":
        return _check_element_lineage(extraction, chunks)
    if check == "chunk_block_integrity":
        return _check_chunk_block_integrity(extraction, chunks)
    if check == "table_structure_fidelity":
        return _check_table_structure_fidelity(extraction, chunks)
    if check == "table_cell_lineage":
        return _check_table_cell_lineage(extraction, chunks)
    if check == "table_row_tree_fidelity":
        return _check_table_row_tree_fidelity(extraction, chunks)
    if check == "visual_chunk_metadata":
        return _check_visual_chunk_metadata(extraction, chunks)
    if check == "chunk_size_compliance":
        return _check_chunk_size_compliance(chunks)
    if check == "chunk_contextual_coherence":
        return _check_chunk_contextual_coherence(extraction, chunks)
    if check == "cross_page_table_continuity":
        return _check_cross_page_table_continuity(extraction, chunks)
    if check == "quality_report_metadata":
        return _check_quality_report_metadata(
            quality_report,
            extraction,
            parser_profile=parser_profile,
            parser_backend=parser_backend,
            parser_version=parser_version,
            fallback_used=fallback_used,
        )
    if check == "heading_structure":
        return _check_heading_structure(extraction, chunks)
    if check == "paragraph_order":
        return _check_paragraph_order(extraction)
    if check == "slide_segment":
        return _check_segment_success(segment_failures, extraction, expected_kind="slide")
    if check == "sheet_segment":
        return _check_segment_success(segment_failures, extraction, expected_kind="sheet")
    if check == "section_path":
        return _check_section_path(extraction, chunks)
    if check == "structural_section_coverage":
        return _check_structural_section_coverage(case, extraction, chunks)
    if check == "dependency_lineage":
        return _check_dependency_lineage(extraction, chunks)
    if check == "email_headers":
        return _check_email_headers(extraction)
    if check == "thread_body":
        return _check_thread_body(extraction)
    if check == "attachment_metadata":
        return _check_attachment_metadata(extraction)
    if check == "canonical_alias":
        duplicate_fixture = _string_value(case.get("duplicate_fixture"))
        if not duplicate_fixture:
            return "failure", "duplicate_fixture_missing"
        duplicate_digest = hashlib.sha256(
            (fixture_root / duplicate_fixture).read_bytes()
        ).hexdigest()
        if duplicate_digest == duplicate_sha256:
            return "passed", "ok"
        return "failure", "sha_mismatch"
    if check == "failed_segment_status":
        return ("passed", "ok") if segment_failures else ("failure", "segment_failure_missing")
    if check == "unsupported_reason":
        expected = _string_value(case.get("expected_unsupported_reason"))
        if not expected:
            return "failure", "expected_unsupported_reason_missing"
        if unsupported_reason == expected:
            return "passed", "ok"
        return "failure", "reason_mismatch"
    if check == "safe_error":
        if warnings or segment_failures:
            return "passed", "ok"
        return "failure", "safe_warning_missing"
    if check == "parser_warning_taxonomy":
        return _check_parser_warning_taxonomy(
            warnings,
            unsupported_reason=unsupported_reason,
            segment_failures=segment_failures,
        )
    return "failure", "unknown_required_check"


def _check_non_empty_extraction(
    extraction: StructuredExtraction | None,
    *,
    requires: str,
) -> tuple[str, str]:
    if extraction is None:
        return "pending", f"requires_{requires}"
    return ("passed", "ok") if extraction.raw_text.strip() else ("failure", "empty_extraction")


def _check_parser_warning_taxonomy(
    warnings: Sequence[str],
    *,
    unsupported_reason: str | None,
    segment_failures: Sequence[object],
) -> tuple[str, str]:
    """parser fallback / unsupported の警告が安定 code だけで表現されているか。"""
    warning_codes = [_string_value(warning) for warning in warnings if _string_value(warning)]
    warning_codes.extend(
        code
        for failure in segment_failures
        if (code := _string_value(getattr(failure, "error_code", "")))
    )
    if not warning_codes:
        return "failure", "warning_code_missing"
    for code in warning_codes:
        if _WARNING_CODE_PATTERN.fullmatch(code) is None:
            return "failure", "unsafe_warning_code"
        if code not in _KNOWN_FILE_PROCESSING_WARNING_CODES:
            return "failure", "unknown_warning_code"
    if unsupported_reason:
        expected_warning = _WARNING_CODE_BY_UNSUPPORTED_REASON.get(unsupported_reason)
        if expected_warning is None:
            return "failure", "unknown_unsupported_reason"
        if expected_warning not in set(warning_codes):
            return "failure", "unsupported_warning_missing"
    return "passed", "ok"


def _check_chunk_traceability(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if not chunks:
        return "pending", "requires_extraction"
    if any(
        _chunk_has_resolvable_element_lineage(chunk, extraction)
        and chunk.metadata.get("page_start") is not None
        and chunk.metadata.get("chunk_group_id")
        for chunk in chunks
    ):
        return "passed", "ok"
    return "failure", "lineage_metadata_missing"


def _check_bbox_citation(
    chunks: Sequence[Chunk],
    *,
    pending_reason: str = "requires_enterprise_ai_bbox",
) -> tuple[str, str]:
    if not chunks:
        return "pending", pending_reason
    if any(chunk.metadata.get("bbox") for chunk in chunks):
        return "passed", "ok"
    return "pending", pending_reason


def _check_bbox_coordinate_validity(
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if not chunks:
        return "pending", "requires_enterprise_ai_bbox"
    bbox_chunks = [chunk for chunk in chunks if chunk.metadata.get("bbox")]
    if not bbox_chunks:
        return "pending", "requires_enterprise_ai_bbox"
    for chunk in bbox_chunks:
        violation = _bbox_coordinate_violation(
            chunk.metadata.get("bbox"),
            metadata=chunk.metadata,
        )
        if violation is not None:
            return "failure", violation
    return "passed", "ok"


def _check_preview_addressability(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """preview/citation jump が bbox-bearing extraction object まで到達できるかを見る。"""
    if extraction is None:
        return _check_bbox_citation(chunks, pending_reason="requires_preview_bbox")
    targets = _preview_bbox_targets(extraction)
    if not targets:
        return _check_bbox_citation(chunks, pending_reason="requires_preview_bbox")
    explicit_pages = {page.page_number for page in extraction.pages}
    page_dimensions = _page_dimensions(extraction)
    page_rotations = _page_rotations(extraction)
    for target in targets:
        page_number = _optional_int(target.get("page_number"))
        if page_number is None or page_number <= 0:
            return "failure", "preview_bbox_page_missing"
        if explicit_pages and page_number not in explicit_pages:
            return "failure", "preview_bbox_page_unresolved"
        metadata = _preview_bbox_metadata(
            target,
            page_dimensions=page_dimensions,
            page_rotations=page_rotations,
        )
        rotation_violation = _preview_bbox_rotation_violation(
            target,
            page_rotations=page_rotations,
        )
        if rotation_violation is not None:
            return "failure", rotation_violation
        violation = _bbox_coordinate_violation(target.get("bbox"), metadata=metadata)
        if violation is not None:
            return "failure", f"preview_{violation}"
    return "passed", "ok"


def _preview_bbox_targets(extraction: StructuredExtraction) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for element in extraction.elements:
        if element.bbox:
            targets.append(
                {
                    "kind": "element",
                    "page_number": element.page_number,
                    "bbox": element.bbox,
                    "metadata": element.metadata,
                }
            )
    for table in extraction.tables:
        for cell in table.cells:
            if cell.bbox:
                targets.append(
                    {
                        "kind": "table_cell",
                        "page_number": table.page_number,
                        "bbox": cell.bbox,
                        "metadata": cell.metadata,
                    }
                )
    for asset in extraction.assets:
        if asset.bbox:
            targets.append(
                {
                    "kind": "asset",
                    "page_number": asset.page_number,
                    "bbox": asset.bbox,
                    "metadata": asset.metadata,
                }
            )
    return targets


def _preview_bbox_metadata(
    target: Mapping[str, object],
    *,
    page_dimensions: Mapping[int, tuple[float, float]],
    page_rotations: Mapping[int, int],
) -> dict[str, object]:
    metadata = dict(_mapping(target.get("metadata")))
    metadata.setdefault("bbox_coordinate_mode", "xyxy")
    bbox_value = target.get("bbox")
    if not _bbox_coordinate_unit(metadata):
        inferred_unit = _inferred_bbox_unit(bbox_value)
        if inferred_unit is not None:
            metadata["bbox_unit"] = inferred_unit
    page_number = _optional_int(target.get("page_number"))
    if page_number is not None and page_number in page_dimensions:
        width, height = page_dimensions[page_number]
        metadata.setdefault("page_width", width)
        metadata.setdefault("page_height", height)
    if page_number is not None and page_number in page_rotations:
        metadata.setdefault("page_rotation", page_rotations[page_number])
    return metadata


def _preview_bbox_rotation_violation(
    target: Mapping[str, object],
    *,
    page_rotations: Mapping[int, int],
) -> str | None:
    page_number = _optional_int(target.get("page_number"))
    if page_number is None or page_number not in page_rotations:
        return None
    expected_rotation = page_rotations[page_number]
    if expected_rotation < 0:
        return "preview_bbox_page_rotation_invalid"
    metadata_rotation = _bbox_page_rotation(_mapping(target.get("metadata")))
    if metadata_rotation is None:
        return None
    if metadata_rotation < 0:
        return "preview_bbox_page_rotation_invalid"
    if metadata_rotation != expected_rotation:
        return "preview_bbox_page_rotation_mismatch"
    return None


def _page_dimensions(extraction: StructuredExtraction) -> dict[int, tuple[float, float]]:
    dimensions: dict[int, tuple[float, float]] = {}
    for page in extraction.pages:
        if page.width is not None and page.height is not None:
            dimensions[page.page_number] = (page.width, page.height)
    return dimensions


def _page_rotations(extraction: StructuredExtraction) -> dict[int, int]:
    rotations: dict[int, int] = {}
    for page in extraction.pages:
        if page.rotation is not None:
            rotations[page.page_number] = _normalize_page_rotation(page.rotation)
    return rotations


def _inferred_bbox_unit(value: object) -> str | None:
    bbox = _bbox_tuple(value)
    if bbox is None:
        return None
    max_value = max(abs(number) for number in bbox[:4])
    if max_value <= 1:
        return "ratio"
    if max_value <= 100:
        return "percent"
    return "absolute"


def _check_table_preserve_rows(chunks: Sequence[Chunk]) -> tuple[str, str]:
    table_chunks = [
        chunk
        for chunk in chunks
        if chunk.metadata.get("content_kind") == "table"
        and chunk.metadata.get("chunk_template") == "table_preserve_rows"
    ]
    if not table_chunks:
        return "failure", "table_preserve_rows_chunk_missing"
    if not all(_string_value(chunk.metadata.get("table_id")) for chunk in table_chunks):
        return "failure", "table_lineage_metadata_missing"
    if not all(
        _positive_int(chunk.metadata.get("table_row_count")) > 0
        and _positive_int(chunk.metadata.get("table_column_count")) > 0
        for chunk in table_chunks
    ):
        return "failure", "table_shape_metadata_missing"
    split_chunks = [
        chunk
        for chunk in table_chunks
        if _optional_int(chunk.metadata.get("chunk_part_count")) is not None
        and (_optional_int(chunk.metadata.get("chunk_part_count")) or 0) > 1
    ]
    if not split_chunks:
        return "passed", "ok"
    if not all(
        _optional_int(chunk.metadata.get("table_data_row_start")) is not None
        and _optional_int(chunk.metadata.get("table_data_row_end")) is not None
        for chunk in split_chunks
    ):
        return "failure", "table_row_group_metadata_missing"
    ordered = sorted(
        split_chunks,
        key=lambda chunk: (
            _optional_int(chunk.metadata.get("chunk_part_index")) or 0,
            chunk.index,
        ),
    )
    if any(_optional_int(chunk.metadata.get("chunk_part_index")) is None for chunk in ordered):
        return "failure", "table_part_index_missing"
    first_header = _table_header_signature(ordered[0].text)
    if not first_header:
        return "failure", "table_header_missing"
    for chunk in ordered[1:]:
        if chunk.metadata.get("table_header_repeated") is not True:
            return "failure", "table_header_repeat_metadata_missing"
        if _table_header_signature(chunk.text) != first_header:
            return "failure", "table_header_not_repeated"
    if any(
        (_optional_int(chunk.metadata.get("table_data_row_start")) or 0)
        > (_optional_int(chunk.metadata.get("table_data_row_end")) or 0)
        for chunk in split_chunks
    ):
        return "failure", "table_row_group_range_invalid"
    return "passed", "ok"


def _check_cross_page_table_continuity(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """同一 table_id の跨ページ table が同じ parent group で辿れるかを見る。"""
    if extraction is None:
        return "pending", "requires_extraction"
    table_pages = _cross_page_table_pages(extraction)
    if not table_pages:
        return "failure", "cross_page_table_missing"
    for table_id, pages in table_pages.items():
        table_chunks = [
            chunk
            for chunk in chunks
            if chunk.metadata.get("content_kind") == "table"
            and _string_value(chunk.metadata.get("table_id")) == table_id
        ]
        if not table_chunks:
            return "failure", "cross_page_table_chunk_missing"
        violation = _cross_page_table_chunk_violation(table_chunks, pages=pages)
        if violation is not None:
            return "failure", violation
    return "passed", "ok"


def _cross_page_table_pages(extraction: StructuredExtraction) -> dict[str, set[int]]:
    pages_by_table: dict[str, set[int]] = {}
    for element in extraction.elements:
        if element.kind != "table" and element.content_kind != "table":
            continue
        table_id = _string_value(element.metadata.get("table_id")) or _string_value(
            element.element_id
        )
        if not table_id or element.page_number is None:
            continue
        pages_by_table.setdefault(table_id, set()).add(element.page_number)
    return {table_id: pages for table_id, pages in pages_by_table.items() if len(pages) > 1}


def _cross_page_table_chunk_violation(
    chunks: Sequence[Chunk],
    *,
    pages: set[int],
) -> str | None:
    ordered = sorted(
        chunks,
        key=lambda chunk: (
            _optional_int(chunk.metadata.get("chunk_part_index")) or 0,
            chunk.index,
        ),
    )
    group_ids = {
        _string_value(chunk.metadata.get("chunk_group_id"))
        for chunk in ordered
        if _string_value(chunk.metadata.get("chunk_group_id"))
    }
    continuity_ids = {
        _string_value(chunk.metadata.get("table_continuity_group_id"))
        for chunk in ordered
        if _string_value(chunk.metadata.get("table_continuity_group_id"))
    }
    if len(group_ids) != 1 or len(continuity_ids) != 1 or group_ids != continuity_ids:
        return "cross_page_table_group_missing"
    if any(chunk.metadata.get("chunk_group_kind") != "table_continuity" for chunk in ordered):
        return "cross_page_table_group_kind_invalid"
    if any(chunk.metadata.get("table_cross_page") is not True for chunk in ordered):
        return "cross_page_table_flag_missing"
    if any(
        _optional_int(chunk.metadata.get("table_page_start")) != min(pages) for chunk in ordered
    ):
        return "cross_page_table_page_start_mismatch"
    if any(_optional_int(chunk.metadata.get("table_page_end")) != max(pages) for chunk in ordered):
        return "cross_page_table_page_end_mismatch"
    expected_indexes = list(range(1, len(ordered) + 1))
    actual_indexes = [
        _optional_int(chunk.metadata.get("table_continuation_index")) for chunk in ordered
    ]
    if actual_indexes != expected_indexes:
        return "cross_page_table_continuation_order_invalid"
    if any(
        _optional_int(chunk.metadata.get("table_continuation_count")) != len(ordered)
        for chunk in ordered
    ):
        return "cross_page_table_continuation_count_invalid"
    if any(chunk.metadata.get("table_header_repeated") is not True for chunk in ordered[1:]):
        return "cross_page_table_header_not_repeated"
    row_ranges: list[tuple[int, int]] = []
    for chunk in ordered:
        row_start = _optional_int(chunk.metadata.get("table_data_row_start"))
        row_end = _optional_int(chunk.metadata.get("table_data_row_end"))
        if row_start is None or row_end is None:
            return "cross_page_table_row_range_missing"
        if row_start <= 0 or row_end < row_start:
            return "cross_page_table_row_range_invalid"
        row_ranges.append((row_start, row_end))
    if [row_start for row_start, _row_end in row_ranges] != sorted(
        row_start for row_start, _row_end in row_ranges
    ):
        return "cross_page_table_row_order_invalid"
    previous_end = 0
    for row_start, row_end in row_ranges:
        if row_start <= previous_end:
            return "cross_page_table_row_overlap"
        previous_end = row_end
    chunk_pages = {page for chunk in ordered for page in _chunk_pages_from_metadata(chunk.metadata)}
    if not pages <= chunk_pages:
        return "cross_page_table_chunk_pages_missing"
    return None


def _chunk_pages_from_metadata(metadata: Mapping[str, object]) -> set[int]:
    page_start = _optional_int(metadata.get("page_start"))
    page_end = _optional_int(metadata.get("page_end")) or page_start
    if page_start is None or page_end is None or page_end < page_start:
        return set()
    return set(range(page_start, page_end + 1))


def _table_header_signature(text: str) -> tuple[str, ...]:
    """表 chunk 先頭の header 行を比較用に正規化する。

    HTML の表は先頭の表頭の行（``tr``。#3547 で HTML の表も行の境目で分けて表頭を繰り返す）。
    """
    if (layout := html_table_layout(text)) is not None:
        return tuple(row.strip() for row in layout.rows[: layout.header_rows])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not _looks_like_table_row(lines[0]):
        return ()
    if len(lines) >= 2 and _looks_like_markdown_separator(lines[1]):
        return (lines[0], lines[1])
    return (lines[0],)


def _looks_like_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _looks_like_markdown_separator(line: str) -> bool:
    if not _looks_like_table_row(line):
        return False
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _check_expected_answer_as_required(
    case: Mapping[str, object],
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    expected = _string_value(case.get("expected_answer"))
    if not expected:
        return "failure", "expected_answer_missing"
    if extraction is None:
        return "pending", "requires_extraction"
    haystack = _normalized_extraction_and_chunk_text(extraction, chunks)
    if _normalize_answer(expected) in haystack:
        return "passed", "ok"
    return "failure", "answer_missing"


def _check_content_kind_present(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
    *,
    expected_kind: str,
    required_metadata_key: str | None = None,
) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    content_kinds = _content_kinds(extraction, chunks)
    if expected_kind not in content_kinds:
        return "failure", f"{expected_kind}_missing"
    if required_metadata_key is None:
        return "passed", "ok"
    if any(
        chunk.metadata.get("content_kind") == expected_kind
        and _string_value(chunk.metadata.get(required_metadata_key))
        for chunk in chunks
    ):
        return "passed", "ok"
    return "failure", f"{expected_kind}_{required_metadata_key}_missing"


def _check_element_lineage(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if not chunks:
        return "pending", "requires_extraction"
    if any(_chunk_has_resolvable_element_lineage(chunk, extraction) for chunk in chunks):
        return "passed", "ok"
    if any(_chunk_element_ids(chunk) for chunk in chunks):
        return "failure", "element_ids_unresolved"
    return "failure", "element_ids_missing"


def _check_chunk_block_integrity(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if extraction is None or not chunks:
        return "pending", "requires_extraction"
    for chunk in chunks:
        if not _string_value(chunk.metadata.get("chunk_group_id")):
            return "failure", "chunk_group_missing"
        if not _chunk_has_resolvable_element_lineage(chunk, extraction):
            return "failure", "chunk_element_lineage_unresolved"
        violation = _chunk_block_integrity_violation(chunk)
        if violation is not None:
            return "failure", violation
    return "passed", "ok"


def _chunk_block_integrity_violation(chunk: Chunk) -> str | None:
    kinds = set(_id_tuple(chunk.metadata.get("element_kinds")))
    if not kinds:
        return "element_kinds_missing"
    content_kind = _string_value(chunk.metadata.get("content_kind"))
    if not content_kind:
        return "content_kind_missing"
    if content_kind == "figure":
        if not kinds <= {"figure", "figure_caption"}:
            return "mixed_figure_block"
        if "figure_caption" in kinds and "figure" in kinds and not _chunk_dependency_edges(chunk):
            return "figure_caption_dependency_missing"
        return None
    if content_kind == "table":
        if not kinds <= {"table", "table_caption"}:
            return "mixed_table_block"
        if not _string_value(chunk.metadata.get("table_id")):
            return "table_id_missing"
        return None
    if content_kind == "code" and kinds != {"code"}:
        return "mixed_code_block"
    if content_kind == "equation" and kinds != {"equation"}:
        return "mixed_equation_block"
    if content_kind == "email" and not kinds <= {"email", "text", "list"}:
        return "mixed_email_block"
    if content_kind in {"slide", "sheet"}:
        if not _string_value(chunk.metadata.get("chunk_template")):
            return f"{content_kind}_template_missing"
        page_start = _optional_int(chunk.metadata.get("page_start"))
        page_end = _optional_int(chunk.metadata.get("page_end"))
        if page_start is None or page_end is None or page_start != page_end:
            return f"{content_kind}_segment_boundary_missing"
        if not kinds <= {"title", "text", "list", "table"}:
            return f"mixed_{content_kind}_block"
        return None
    if content_kind in {"text", "list"}:
        allowed_text_kinds = {"title", "text", "list"}
        if not kinds <= allowed_text_kinds:
            return "mixed_text_block"
    return None


def _check_table_structure_fidelity(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """table chunk が tables[] / cells[] の構造情報へ回収できるかを見る。"""
    if extraction is None:
        return "pending", "requires_extraction"
    table_chunks = [chunk for chunk in chunks if chunk.metadata.get("content_kind") == "table"]
    if not table_chunks:
        return "failure", "table_chunk_missing"
    table_shapes = _extraction_table_shapes(extraction)
    if not table_shapes:
        return "failure", "table_cells_missing"
    for shape in table_shapes.values():
        violation = _table_shape_violation(shape)
        if violation is not None:
            return "failure", violation
    table_element_ids = _table_element_ids(extraction)
    if table_element_ids and not table_element_ids <= set(table_shapes):
        return "failure", "table_element_lineage_unresolved"
    for chunk in table_chunks:
        violation = _table_chunk_structure_violation(chunk, table_shapes)
        if violation is not None:
            return "failure", violation
    group_violation = _table_chunk_group_structure_violation(table_chunks)
    if group_violation is not None:
        return "failure", group_violation
    return "passed", "ok"


def _extraction_table_shapes(
    extraction: StructuredExtraction,
) -> dict[str, tuple[int, int, int]]:
    shapes: dict[str, tuple[int, int, int]] = {}
    for table in extraction.tables:
        table_id = _string_value(table.table_id)
        if not table_id or not table.cells:
            continue
        row_indexes = {cell.row for cell in table.cells}
        column_indexes = {cell.col for cell in table.cells}
        shapes[table_id] = (
            max(row_indexes) + 1 if row_indexes else 0,
            max(column_indexes) + 1 if column_indexes else 0,
            len({(cell.row, cell.col) for cell in table.cells}),
        )
    return shapes


def _table_shape_violation(shape: tuple[int, int, int]) -> str | None:
    row_count, column_count, cell_count = shape
    if row_count <= 0 or column_count <= 0:
        return "table_shape_invalid"
    if cell_count < row_count:
        return "table_cell_grid_sparse"
    return None


def _table_element_ids(extraction: StructuredExtraction) -> set[str]:
    table_ids: set[str] = set()
    for element in extraction.elements:
        if element.content_kind != "table" and element.kind != "table":
            continue
        table_id = _string_value(element.metadata.get("table_id")) or _string_value(
            element.element_id
        )
        if table_id:
            table_ids.add(table_id)
    return table_ids


def _table_chunk_structure_violation(
    chunk: Chunk,
    table_shapes: Mapping[str, tuple[int, int, int]],
) -> str | None:
    table_id = _string_value(chunk.metadata.get("table_id"))
    if not table_id:
        return "table_chunk_id_missing"
    shape = table_shapes.get(table_id)
    if shape is None:
        return "table_chunk_id_unresolved"
    row_count, column_count, _cell_count = shape
    chunk_row_count = _positive_int(chunk.metadata.get("table_row_count"))
    chunk_column_count = _positive_int(chunk.metadata.get("table_column_count"))
    if chunk_row_count and chunk_row_count != row_count:
        return "table_chunk_row_count_mismatch"
    if chunk_column_count and chunk_column_count != column_count:
        return "table_chunk_column_count_mismatch"
    parsed_column_count = _table_chunk_column_count(chunk.text)
    if parsed_column_count is not None and parsed_column_count != column_count:
        return "table_chunk_text_column_mismatch"
    row_start = _optional_int(chunk.metadata.get("table_data_row_start"))
    row_end = _optional_int(chunk.metadata.get("table_data_row_end"))
    if row_start is None and row_end is None:
        return None
    if row_start is None or row_end is None:
        return "table_chunk_row_range_partial"
    if row_start <= 0 or row_end < row_start:
        return "table_chunk_row_range_invalid"
    body_row_count = max(0, row_count - 1)
    if body_row_count and row_end > body_row_count:
        return "table_chunk_row_range_out_of_bounds"
    return None


def _table_chunk_group_structure_violation(chunks: Sequence[Chunk]) -> str | None:
    by_group: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        group_id = _string_value(chunk.metadata.get("chunk_group_id"))
        if group_id:
            by_group.setdefault(group_id, []).append(chunk)
    for group_chunks in by_group.values():
        ranges: list[tuple[int, int]] = []
        for chunk in group_chunks:
            row_start = _optional_int(chunk.metadata.get("table_data_row_start"))
            row_end = _optional_int(chunk.metadata.get("table_data_row_end"))
            if row_start is not None and row_end is not None:
                ranges.append((row_start, row_end))
        if len(ranges) < 2:
            continue
        ordered = sorted(ranges)
        previous_end = 0
        for row_start, row_end in ordered:
            if row_start <= previous_end:
                return "table_chunk_row_range_overlap"
            previous_end = row_end
    return None


def _table_chunk_column_count(text: str) -> int | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not _looks_like_table_row(stripped):
            continue
        if _looks_like_markdown_separator(stripped):
            continue
        return len([cell for cell in stripped.strip("|").split("|")])
    return None


def _check_table_cell_lineage(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """table/chunk が主張する cell-level lineage を cells[].metadata で解決できるか見る。"""
    if extraction is None:
        return "pending", "requires_extraction"
    if not extraction.tables:
        return "failure", "table_cells_missing"
    table_refs = _table_cell_refs(extraction)
    chunk_refs = _chunk_table_cell_refs(chunks)
    claimed_refs = table_refs | chunk_refs
    cell_refs = _cell_metadata_refs(extraction)
    if claimed_refs and not cell_refs:
        return "failure", "table_cell_metadata_missing"
    missing_refs = claimed_refs - cell_refs
    if missing_refs:
        return "failure", "table_cell_ref_unresolved"
    incomplete_ref = _cell_formula_metadata_incomplete_ref(extraction)
    if incomplete_ref:
        return "failure", "table_cell_formula_detail_missing"
    return "passed", "ok"


def _table_cell_refs(extraction: StructuredExtraction) -> set[str]:
    refs: set[str] = set()
    for table in extraction.tables:
        refs.update(_formula_ref_set(table.metadata.get("table_cell_refs")))
        refs.update(_formula_ref_set(table.metadata.get("cell_refs")))
        refs.update(_formula_ref_set(table.metadata.get("formula_cell_refs")))
        refs.update(_formula_ref_set(table.metadata.get("formula_cell_ref")))
    return refs


def _chunk_table_cell_refs(chunks: Sequence[Chunk]) -> set[str]:
    refs: set[str] = set()
    for chunk in chunks:
        refs.update(_formula_ref_set(chunk.metadata.get("table_cell_refs")))
        refs.update(_formula_ref_set(chunk.metadata.get("cell_refs")))
        refs.update(_formula_ref_set(chunk.metadata.get("cell_ref")))
        refs.update(_formula_ref_set(chunk.metadata.get("formula_cell_refs")))
        refs.update(_formula_ref_set(chunk.metadata.get("formula_cell_ref")))
    return refs


def _cell_metadata_refs(extraction: StructuredExtraction) -> set[str]:
    refs: set[str] = set()
    for table in extraction.tables:
        for cell in table.cells:
            refs.update(_formula_ref_set(cell.metadata.get("cell_ref")))
            refs.update(_formula_ref_set(cell.metadata.get("cell_address")))
            refs.update(_formula_ref_set(cell.metadata.get("address")))
            refs.update(_formula_ref_set(cell.metadata.get("formula_cell_ref")))
    return refs


def _cell_formula_metadata_incomplete_ref(extraction: StructuredExtraction) -> str | None:
    for table in extraction.tables:
        for cell in table.cells:
            refs = _formula_ref_set(cell.metadata.get("formula_cell_ref"))
            if not refs:
                continue
            has_formula_detail = any(
                _string_value(cell.metadata.get(key))
                for key in ("formula", "formula_value", "equation_format")
            )
            if not has_formula_detail:
                return sorted(refs)[0]
    return None


def _formula_ref_set(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return refs
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                decoded = None
            if decoded is not None:
                return _formula_ref_set(decoded)
        candidates = re.split(r"[\n,;\t]+", stripped)
    elif isinstance(value, Mapping):
        for key in (
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
        ):
            refs.update(_formula_ref_set(value.get(key)))
        metadata = value.get("metadata")
        if metadata is not value:
            refs.update(_formula_ref_set(metadata))
        return refs
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            refs.update(_formula_ref_set(item))
        return refs
    elif isinstance(value, int | float) and not isinstance(value, bool):
        candidates = [str(value)]
    else:
        return refs
    for candidate in candidates:
        cleaned = candidate.strip()
        if cleaned:
            refs.add(cleaned[:80])
    return refs


def _check_table_row_tree_fidelity(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """table chunk が row-level key-value block として復元できるかを見る。"""
    if extraction is None:
        return "pending", "requires_extraction"
    table_chunks = [chunk for chunk in chunks if chunk.metadata.get("content_kind") == "table"]
    if not table_chunks:
        return "failure", "table_chunk_missing"
    table_shapes = _extraction_table_shapes(extraction)
    for chunk in table_chunks:
        violation = _table_row_tree_chunk_violation(chunk, table_shapes)
        if violation is not None:
            return "failure", violation
    return "passed", "ok"


def _table_row_tree_chunk_violation(
    chunk: Chunk,
    table_shapes: Mapping[str, tuple[int, int, int]],
) -> str | None:
    metadata = chunk.metadata
    if _string_value(metadata.get("table_row_tree_version")) != "row_tree_v1":
        return "table_row_tree_version_missing"
    if _string_value(metadata.get("table_row_tree_format")) != "key_value_rows":
        return "table_row_tree_format_missing"
    column_keys = _json_string_list(metadata.get("table_row_tree_column_keys"))
    if not column_keys:
        return "table_row_tree_column_keys_missing"
    column_count = _positive_int(metadata.get("table_row_tree_column_count"))
    if column_count != len(column_keys):
        return "table_row_tree_column_count_mismatch"
    table_id = _string_value(metadata.get("table_id"))
    shape = table_shapes.get(table_id)
    if shape is not None and shape[1] != column_count:
        return "table_row_tree_extraction_column_mismatch"

    row_blocks = _table_row_tree_blocks_from_chunk(chunk, column_keys=column_keys)
    if not row_blocks:
        return "table_row_tree_rows_missing"
    row_count = _positive_int(metadata.get("table_row_tree_row_count"))
    if row_count != len(row_blocks):
        return "table_row_tree_row_count_mismatch"
    row_start = _positive_int(metadata.get("table_row_tree_row_start"))
    row_end = _positive_int(metadata.get("table_row_tree_row_end"))
    if row_start <= 0 or row_end < row_start or row_end - row_start + 1 != row_count:
        return "table_row_tree_row_range_invalid"
    if _string_value(metadata.get("table_row_tree_header_sha256")) != _header_sha256(column_keys):
        return "table_row_tree_header_hash_mismatch"
    row_hashes = _json_string_list(metadata.get("table_row_tree_row_hashes"))
    expected_hashes = [
        _stable_json_sha256({"columns": column_keys, "row": row_block}) for row_block in row_blocks
    ]
    if row_hashes != expected_hashes:
        return "table_row_tree_row_hash_mismatch"
    if _string_value(metadata.get("table_row_tree_kv_sha256")) != _stable_json_sha256(
        {"columns": column_keys, "rows": row_blocks}
    ):
        return "table_row_tree_kv_hash_mismatch"
    return None


def _table_row_tree_blocks_from_chunk(
    chunk: Chunk,
    *,
    column_keys: Sequence[str],
) -> list[dict[str, str]]:
    rows = _table_rows_without_separator(chunk.text)
    if len(rows) < 2:
        return []
    expected_keys = _table_column_keys(rows[0], column_count=len(column_keys))
    if list(column_keys) != expected_keys:
        return []
    return _table_key_value_rows(list(column_keys), rows[1:])


def _table_rows_without_separator(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not _looks_like_table_row(stripped) or _looks_like_markdown_separator(stripped):
            continue
        cells = [
            _clean_table_cell(cell.replace("\\|", "|")) for cell in _split_table_cells(stripped)
        ]
        if cells:
            rows.append(cells)
    return rows


def _split_table_cells(line: str) -> list[str]:
    body = line.strip().strip("|")
    return re.split(r"(?<!\\)\|", body)


def _clean_table_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _table_column_keys(header_cells: Sequence[str], *, column_count: int) -> list[str]:
    raw_keys = [
        _clean_table_column_key(header_cells[index] if index < len(header_cells) else "")
        for index in range(column_count)
    ]
    keys: list[str] = []
    seen: dict[str, int] = {}
    for index, key in enumerate(raw_keys, start=1):
        base = key or f"column_{index}"
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return keys


def _clean_table_column_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:80]


def _table_key_value_rows(
    column_keys: list[str],
    data_rows: Sequence[Sequence[str]],
) -> list[dict[str, str]]:
    row_blocks: list[dict[str, str]] = []
    for row in data_rows:
        values = [*row, *([""] * max(0, len(column_keys) - len(row)))]
        row_blocks.append(
            {
                column_key: values[index] if index < len(values) else ""
                for index, column_key in enumerate(column_keys)
            }
        )
    return row_blocks


def _json_string_list(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _header_sha256(column_keys: Sequence[str]) -> str:
    header_json = json.dumps(column_keys, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(header_json.encode("utf-8")).hexdigest()


def _stable_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_visual_chunk_metadata(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """preview workspace / citation jump に必要な chunk metadata の完全性を見る。"""
    if extraction is None or not chunks:
        return "pending", "requires_extraction"
    valid_pages = _structured_extraction_pages(extraction)
    for chunk in chunks:
        violation = _visual_chunk_metadata_violation(chunk, extraction, valid_pages=valid_pages)
        if violation is not None:
            return "failure", violation
    return "passed", "ok"


def _check_chunk_size_compliance(chunks: Sequence[Chunk]) -> tuple[str, str]:
    """chunk が空でなく、サイズ契約とハッシュ metadata を満たすかを見る。"""
    if not chunks:
        return "pending", "requires_extraction"
    for chunk in chunks:
        violation = _chunk_size_compliance_violation(chunk)
        if violation is not None:
            return "failure", violation
    return "passed", "ok"


def _chunk_size_compliance_violation(chunk: Chunk) -> str | None:
    text = chunk.text
    if not text.strip():
        return "chunk_text_empty"
    text_chars = _optional_int(chunk.metadata.get("text_chars"))
    if text_chars != len(text):
        return "chunk_text_chars_mismatch"
    text_sha256 = _string_value(chunk.metadata.get("text_sha256"))
    if len(text_sha256) != 64 or text_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest():
        return "chunk_text_sha256_mismatch"
    if chunk.metadata.get("chunk_fixed_delimiter") is True:
        return None
    target = _positive_int(chunk.metadata.get("chunk_size_target"))
    limit = _positive_int(chunk.metadata.get("chunk_size_limit"))
    if target <= 0 or limit < target:
        return "chunk_size_target_missing"
    compliance = _string_value(chunk.metadata.get("chunk_size_compliance"))
    if compliance == "within_limit":
        return None if len(text) <= limit else "chunk_size_limit_exceeded"
    if compliance == "overflow_justified":
        if len(text) <= limit:
            return "chunk_size_overflow_not_needed"
        reason = _string_value(chunk.metadata.get("chunk_size_overflow_reason"))
        if reason != "atomic_block":
            return "chunk_size_overflow_reason_invalid"
        if _string_value(chunk.metadata.get("content_kind")) not in {
            "table",
            "code",
            "equation",
            "figure",
        }:
            return "chunk_size_overflow_content_kind_invalid"
        return None
    if compliance == "overflow":
        return "chunk_size_limit_exceeded"
    return "chunk_size_compliance_missing"


def _check_chunk_contextual_coherence(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """split chunk が親 context を保持し、同一 group として復元できるかを見る。"""
    if extraction is None or not chunks:
        return "pending", "requires_extraction"
    for chunk in chunks:
        violation = _chunk_contextual_coherence_violation(chunk, extraction)
        if violation is not None:
            return "failure", violation
    group_violation = _chunk_group_contextual_coherence_violation(chunks)
    if group_violation is not None:
        return "failure", group_violation
    return "passed", "ok"


def _chunk_contextual_coherence_violation(
    chunk: Chunk,
    extraction: StructuredExtraction,
) -> str | None:
    metadata = chunk.metadata
    content_kind = _string_value(metadata.get("content_kind"))
    if not content_kind:
        return "context_content_kind_missing"
    if not _string_value(metadata.get("chunk_group_id")):
        return "context_chunk_group_id_missing"
    if not _string_value(metadata.get("chunk_group_kind")):
        return "context_chunk_group_kind_missing"
    part_index = _positive_int(metadata.get("chunk_part_index"))
    part_count = _positive_int(metadata.get("chunk_part_count"))
    if part_index <= 0 or part_count <= 0 or part_index > part_count:
        return "context_chunk_part_metadata_invalid"
    if not _chunk_has_resolvable_element_lineage(chunk, extraction):
        return "context_element_lineage_unresolved"

    if (
        content_kind in {"text", "list"}
        and _chunk_references_section_path(chunk, extraction)
        and not (
            _string_value(metadata.get("section_path"))
            or _string_value(metadata.get("section_title"))
        )
    ):
        return "context_section_path_missing"
    if content_kind == "table":
        if not _string_value(metadata.get("table_id")):
            return "context_table_id_missing"
        if part_count > 1:
            if (
                _optional_int(metadata.get("table_data_row_start")) is None
                or _optional_int(metadata.get("table_data_row_end")) is None
            ):
                return "context_table_row_range_missing"
            if part_index > 1 and metadata.get("table_header_repeated") is not True:
                return "context_table_header_repeat_missing"
    if content_kind == "code" and not _string_value(metadata.get("code_language")):
        return "context_code_language_missing"
    if content_kind == "equation" and not _string_value(metadata.get("equation_delimiter")):
        return "context_equation_delimiter_missing"
    if content_kind in {"slide", "sheet"}:
        if not _string_value(metadata.get("chunk_template")):
            return f"context_{content_kind}_template_missing"
        page_start = _optional_int(metadata.get("page_start"))
        page_end = _optional_int(metadata.get("page_end")) or page_start
        if page_start is None or page_end is None or page_start != page_end:
            return f"context_{content_kind}_page_boundary_missing"
    if content_kind == "email" and _string_value(metadata.get("chunk_template")) != "email_thread":
        return "context_email_template_missing"
    return None


def _chunk_group_contextual_coherence_violation(chunks: Sequence[Chunk]) -> str | None:
    by_group: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        group_id = _string_value(chunk.metadata.get("chunk_group_id"))
        if group_id:
            by_group.setdefault(group_id, []).append(chunk)
    for group_chunks in by_group.values():
        violation = _single_chunk_group_contextual_coherence_violation(group_chunks)
        if violation is not None:
            return violation
    return None


def _single_chunk_group_contextual_coherence_violation(
    group_chunks: Sequence[Chunk],
) -> str | None:
    part_counts = {_positive_int(chunk.metadata.get("chunk_part_count")) for chunk in group_chunks}
    if len(part_counts) != 1:
        return "context_group_part_count_mismatch"
    part_count = next(iter(part_counts))
    part_indexes = sorted(
        _positive_int(chunk.metadata.get("chunk_part_index")) for chunk in group_chunks
    )
    if part_indexes != list(range(1, part_count + 1)):
        return "context_group_part_indexes_not_contiguous"
    group_kinds = {_string_value(chunk.metadata.get("chunk_group_kind")) for chunk in group_chunks}
    if len(group_kinds) != 1:
        return "context_group_kind_mismatch"
    if part_count <= 1:
        return None

    section_paths = {
        _string_value(chunk.metadata.get("section_path"))
        for chunk in group_chunks
        if _string_value(chunk.metadata.get("section_path"))
    }
    if len(section_paths) > 1:
        return "context_group_section_path_mismatch"
    table_ids = {
        _string_value(chunk.metadata.get("table_id"))
        for chunk in group_chunks
        if _string_value(chunk.metadata.get("table_id"))
    }
    if len(table_ids) > 1:
        return "context_group_table_id_mismatch"
    code_languages = {
        _string_value(chunk.metadata.get("code_language"))
        for chunk in group_chunks
        if _string_value(chunk.metadata.get("code_language"))
    }
    if len(code_languages) > 1:
        return "context_group_code_language_mismatch"
    equation_delimiters = {
        _string_value(chunk.metadata.get("equation_delimiter"))
        for chunk in group_chunks
        if _string_value(chunk.metadata.get("equation_delimiter"))
    }
    if len(equation_delimiters) > 1:
        return "context_group_equation_delimiter_mismatch"
    return None


def _chunk_references_section_path(
    chunk: Chunk,
    extraction: StructuredExtraction,
) -> bool:
    chunk_ids = set(_chunk_element_ids(chunk))
    if not chunk_ids:
        return False
    return any(
        element.element_id in chunk_ids and bool(element.section_path)
        for element in extraction.elements
    )


def _visual_chunk_metadata_violation(
    chunk: Chunk,
    extraction: StructuredExtraction,
    *,
    valid_pages: set[int],
) -> str | None:
    metadata = chunk.metadata
    content_kind = _string_value(metadata.get("content_kind"))
    if not content_kind:
        return "visual_content_kind_missing"
    if not _string_value(metadata.get("source_parser")):
        return "visual_source_parser_missing"
    if not _string_value(metadata.get("chunk_template")):
        return "visual_chunk_template_missing"
    if not _string_value(metadata.get("chunk_group_id")):
        return "visual_chunk_group_id_missing"
    if not _string_value(metadata.get("chunk_group_kind")):
        return "visual_chunk_group_kind_missing"
    part_index = _positive_int(metadata.get("chunk_part_index"))
    part_count = _positive_int(metadata.get("chunk_part_count"))
    if part_index <= 0 or part_count <= 0 or part_index > part_count:
        return "visual_chunk_part_metadata_invalid"
    page_start = _optional_int(metadata.get("page_start"))
    page_end = _optional_int(metadata.get("page_end")) or page_start
    if page_start is None or page_end is None:
        return "visual_page_range_missing"
    if page_start <= 0 or page_start > page_end:
        return "visual_page_range_invalid"
    if valid_pages and not set(range(page_start, page_end + 1)) & valid_pages:
        return "visual_page_range_unresolved"
    if not _chunk_has_resolvable_element_lineage(chunk, extraction):
        return "visual_element_lineage_unresolved"
    if content_kind == "table" and not _string_value(metadata.get("table_id")):
        return "visual_table_id_missing"
    if content_kind == "code" and not _string_value(metadata.get("code_language")):
        return "visual_code_language_missing"
    if content_kind == "equation" and not _string_value(metadata.get("equation_delimiter")):
        return "visual_equation_delimiter_missing"
    return None


def _check_quality_report_metadata(
    quality_report: IngestionQualityReport | None,
    extraction: StructuredExtraction | None,
    *,
    parser_profile: str,
    parser_backend: str,
    parser_version: str,
    fallback_used: bool,
) -> tuple[str, str]:
    """quality_report が parser/extraction と矛盾しない非機密 metadata かを見る。"""
    if extraction is None:
        return "pending", "requires_extraction_quality_report"
    if quality_report is None:
        return "failure", "quality_report_missing"
    payload = extraction.to_document_payload()
    payload["quality_report"] = quality_report.model_dump(exclude_none=True)
    violation = quality_report_metadata_violation(
        payload,
        expected_parser_profile=parser_profile,
        expected_parser_backend=parser_backend,
        expected_parser_version=parser_version,
        expected_fallback_used=fallback_used,
    )
    return ("passed", "ok") if violation is None else ("failure", violation)


def ingestion_quality_report_completeness(
    extractions: Sequence[Mapping[str, object]],
) -> float:
    """保存済み extraction payload の quality_report 完整率を返す。"""
    if not extractions:
        return 0.0
    complete = sum(
        1 for extraction in extractions if quality_report_metadata_violation(extraction) is None
    )
    return complete / len(extractions)


def quality_report_metadata_violation(
    extraction_payload: Mapping[str, object],
    *,
    expected_parser_profile: str | None = None,
    expected_parser_backend: str | None = None,
    expected_parser_version: str | None = None,
    expected_fallback_used: bool | None = None,
) -> str | None:
    """quality_report payload が production gate に必要な項目を満たすか検証する。"""
    report_payload = _mapping(extraction_payload.get("quality_report"))
    if not report_payload:
        return "quality_report_missing"
    missing_fields = sorted(_QUALITY_REPORT_REQUIRED_FIELDS - set(report_payload))
    if missing_fields:
        return "quality_report_fields_missing:" + ",".join(missing_fields)
    if any(key in report_payload for key in _QUALITY_REPORT_SENSITIVE_KEYS):
        return "quality_report_sensitive_key"
    try:
        report = IngestionQualityReport.model_validate(report_payload)
    except ValidationError:
        return "quality_report_invalid"

    violation = _quality_report_base_violation(report)
    if violation is not None:
        return violation
    if expected_parser_profile and report.parser_profile != expected_parser_profile:
        return "quality_parser_profile_mismatch"
    if expected_parser_backend and report.parser_backend != expected_parser_backend:
        return "quality_parser_backend_mismatch"
    if expected_parser_version and report.parser_version != expected_parser_version:
        return "quality_parser_version_mismatch"
    if expected_fallback_used is not None and report.fallback_used != expected_fallback_used:
        return "quality_fallback_used_mismatch"
    if not _has_structured_payload_for_quality_report(extraction_payload):
        return None
    try:
        extraction = StructuredExtraction.model_validate(extraction_payload)
    except ValidationError:
        return "quality_extraction_invalid"
    return _quality_report_extraction_violation(report, extraction)


def _quality_report_base_violation(report: IngestionQualityReport) -> str | None:
    if not report.parser_profile:
        return "quality_parser_profile_missing"
    if not report.parser_backend:
        return "quality_parser_backend_missing"
    if not report.parser_version:
        return "quality_parser_version_missing"
    if report.risk_level not in {"low", "medium", "high"}:
        return "quality_risk_level_invalid"
    if any(getattr(report, field) < 0 for field in _QUALITY_REPORT_COUNT_FIELDS):
        return "quality_count_negative"
    warning_codes = set(report.quality_warnings)
    for code in warning_codes:
        if _WARNING_CODE_PATTERN.fullmatch(code) is None:
            return "quality_warning_code_unsafe"
        if code not in _KNOWN_FILE_PROCESSING_WARNING_CODES:
            return "quality_warning_code_unknown"
    expected_risk = _quality_report_expected_risk_level(warning_codes)
    if report.risk_level != expected_risk:
        return "quality_risk_level_mismatch"
    if report.fallback_used and "parser_fallback_used" not in warning_codes:
        return "quality_fallback_warning_missing"
    if report.table_count > 0 and "table_structure_review" not in warning_codes:
        return "quality_table_warning_missing"
    if report.figure_count > 0 and "figure_ocr_review" not in warning_codes:
        return "quality_figure_warning_missing"
    if report.formula_count > 0 and "formula_review" not in warning_codes:
        return "quality_formula_warning_missing"
    if report.low_confidence_count > 0 and not warning_codes.intersection(
        {"low_confidence_elements", "low_extraction_confidence"}
    ):
        return "quality_low_confidence_warning_missing"
    if report.failed_segment_count > 0 and "failed_segments" not in warning_codes:
        return "quality_failed_segment_warning_missing"
    return None


def _quality_report_extraction_violation(
    report: IngestionQualityReport,
    extraction: StructuredExtraction,
) -> str | None:
    minimum_page_count = _quality_report_minimum_page_count(extraction)
    if report.page_count < minimum_page_count:
        return "quality_page_count_underreported"
    if minimum_page_count > 0 and report.page_coverage <= 0:
        return "quality_page_coverage_missing"
    if report.element_count != len(extraction.elements):
        return "quality_element_count_mismatch"
    if report.table_count < _quality_report_table_count(extraction):
        return "quality_table_count_underreported"
    if report.figure_count < _quality_report_figure_count(extraction):
        return "quality_figure_count_underreported"
    if report.formula_count < _quality_report_formula_count(extraction):
        return "quality_formula_count_underreported"
    if report.low_confidence_count < _quality_report_low_confidence_count(extraction):
        return "quality_low_confidence_count_underreported"
    return None


def _quality_report_expected_risk_level(warning_codes: set[str]) -> str:
    if warning_codes.intersection(HIGH_RISK_WARNING_CODES):
        return "high"
    if warning_codes.intersection(MEDIUM_RISK_WARNING_CODES):
        return "medium"
    return "low"


def _has_structured_payload_for_quality_report(payload: Mapping[str, object]) -> bool:
    return any(
        key in payload
        for key in ("raw_text", "elements", "pages", "tables", "assets", "parser_artifacts")
    )


def _quality_report_minimum_page_count(extraction: StructuredExtraction) -> int:
    page_numbers = _structured_extraction_pages(extraction)
    return max(len(extraction.pages), len(page_numbers))


def _quality_report_table_count(extraction: StructuredExtraction) -> int:
    return max(
        len(extraction.tables),
        sum(
            1
            for element in extraction.elements
            if element.kind == "table" or element.content_kind == "table"
        ),
    )


def _quality_report_figure_count(extraction: StructuredExtraction) -> int:
    return max(
        sum(
            1
            for element in extraction.elements
            if element.kind in {"figure", "figure_caption"} or element.content_kind == "figure"
        ),
        sum(1 for asset in extraction.assets if asset.kind.casefold() in _FIGURE_ASSET_KINDS),
    )


def _quality_report_formula_count(extraction: StructuredExtraction) -> int:
    return sum(
        1
        for element in extraction.elements
        if element.kind in {"formula", "equation"} or element.content_kind == "equation"
    )


def _quality_report_low_confidence_count(extraction: StructuredExtraction) -> int:
    count = sum(
        1
        for element in extraction.elements
        if element.confidence is not None and element.confidence < 0.65
    )
    for table in extraction.tables:
        count += sum(
            1 for cell in table.cells if cell.confidence is not None and cell.confidence < 0.65
        )
    return count


def _structured_extraction_pages(extraction: StructuredExtraction) -> set[int]:
    return {page.page_number for page in extraction.pages if page.page_number is not None} | {
        element.page_number for element in extraction.elements if element.page_number is not None
    }


def _chunk_has_resolvable_element_lineage(
    chunk: Chunk,
    extraction: StructuredExtraction | None,
) -> bool:
    chunk_element_ids = _chunk_element_ids(chunk)
    if not chunk_element_ids:
        return False
    extraction_element_ids = _structured_extraction_element_ids(extraction)
    return bool(extraction_element_ids and (set(chunk_element_ids) & extraction_element_ids))


def _chunk_element_ids(chunk: Chunk) -> tuple[str, ...]:
    return _id_tuple(chunk.metadata.get("element_ids"))


def _structured_extraction_element_ids(
    extraction: StructuredExtraction | None,
) -> set[str]:
    if extraction is None:
        return set()
    element_ids = {
        element.element_id
        for element in extraction.elements
        if element.element_id is not None and element.element_id.strip()
    }
    for page in extraction.pages:
        element_ids.update(element_id for element_id in page.element_ids if element_id.strip())
    return element_ids


def _check_heading_structure(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    if any(element.kind == "title" for element in extraction.elements):
        return "passed", "ok"
    if any(chunk.metadata.get("section_title") for chunk in chunks):
        return "passed", "ok"
    return "failure", "heading_missing"


def _check_paragraph_order(extraction: StructuredExtraction | None) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    orders = [element.order for element in extraction.elements]
    if orders == sorted(orders):
        return "passed", "ok"
    return "failure", "element_order_not_monotonic"


def _check_reading_order(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    """layout parser の reading order と chunk 順序が一貫しているかを見る。"""
    if extraction is None:
        return "pending", "requires_enterprise_ai_extraction"
    elements = [element for element in extraction.elements if element.text.strip()]
    if not elements:
        return "failure", "reading_order_elements_missing"
    element_violation = _reading_order_element_violation(elements)
    if element_violation is not None:
        return "failure", element_violation
    chunk_violation = _reading_order_chunk_violation(chunks)
    if chunk_violation is not None:
        return "failure", chunk_violation
    return "passed", "ok"


def _reading_order_element_violation(
    elements: Sequence[DocumentElement],
) -> str | None:
    orders = [element.order for element in elements]
    if orders != sorted(orders):
        return "element_order_not_monotonic"

    pages = [
        page
        for element in elements
        if (page := _optional_int(getattr(element, "page_number", None))) is not None
    ]
    if pages and pages != sorted(pages):
        return "element_page_order_not_monotonic"

    raw_ranges: list[tuple[int, int]] = []
    for element in elements:
        metadata = element.metadata
        if not isinstance(metadata, Mapping):
            continue
        raw_start = _optional_int(metadata.get("raw_start"))
        raw_end = _optional_int(metadata.get("raw_end"))
        if raw_start is None or raw_end is None:
            continue
        if raw_start > raw_end:
            return "element_raw_range_invalid"
        raw_ranges.append((raw_start, raw_end))
    if len(raw_ranges) >= 2 and [item[0] for item in raw_ranges] != sorted(
        item[0] for item in raw_ranges
    ):
        return "element_raw_offset_not_monotonic"
    return None


def _reading_order_chunk_violation(chunks: Sequence[Chunk]) -> str | None:
    if not chunks:
        return None
    chunk_indexes = [chunk.index for chunk in chunks]
    if chunk_indexes != sorted(chunk_indexes):
        return "chunk_index_not_monotonic"

    pages = [
        page
        for chunk in chunks
        if (page := _optional_int(chunk.metadata.get("page_start"))) is not None
    ]
    if pages and pages != sorted(pages):
        return "chunk_page_order_not_monotonic"

    by_group: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        group_id = _string_value(chunk.metadata.get("chunk_group_id"))
        if group_id:
            by_group.setdefault(group_id, []).append(chunk)
    for group_chunks in by_group.values():
        part_indexes = [
            part_index
            for chunk in group_chunks
            if (part_index := _optional_int(chunk.metadata.get("chunk_part_index"))) is not None
        ]
        if len(part_indexes) >= 2 and part_indexes != sorted(part_indexes):
            return "chunk_group_part_order_not_monotonic"
        row_starts = [
            row_start
            for chunk in group_chunks
            if (row_start := _optional_int(chunk.metadata.get("table_data_row_start"))) is not None
        ]
        if len(row_starts) >= 2 and row_starts != sorted(row_starts):
            return "table_row_group_order_not_monotonic"
    return None


def _check_segment_success(
    segment_failures: Sequence[object],
    extraction: StructuredExtraction | None,
    *,
    expected_kind: str,
) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    if segment_failures:
        return "failure", "unexpected_segment_failure"
    content_kinds = _content_kinds(extraction, [])
    if expected_kind in content_kinds:
        return "passed", "ok"
    return "failure", f"{expected_kind}_missing"


def _check_section_path(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    if any(element.section_path for element in extraction.elements):
        return "passed", "ok"
    if any(chunk.metadata.get("section_path") for chunk in chunks):
        return "passed", "ok"
    return "failure", "section_path_missing"


def _check_structural_section_coverage(
    case: Mapping[str, object],
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    expected_sections = _section_set(case.get("expected_sections"))
    if not expected_sections:
        return "failure", "expected_sections_missing"
    if extraction is None or not chunks:
        return "pending", "requires_extraction"
    covered_sections = _extraction_section_set(extraction) | _chunk_section_set(chunks)
    missing = expected_sections - covered_sections
    if missing:
        return "failure", "missing_sections:" + ",".join(sorted(missing))
    return "passed", "ok"


def _extraction_section_set(extraction: StructuredExtraction) -> set[str]:
    sections: set[str] = set()
    for element in extraction.elements:
        if element.section_path:
            sections.add(_normalize_section_label(" > ".join(element.section_path)))
        if element.kind == "title" and element.text:
            sections.add(_normalize_section_label(element.text))
    return {section for section in sections if section}


def _chunk_section_set(chunks: Sequence[Chunk]) -> set[str]:
    sections: set[str] = set()
    for chunk in chunks:
        for key in ("section_path", "section_title"):
            section = _normalize_section_label(chunk.metadata.get(key))
            if section:
                sections.add(section)
    return sections


def _check_dependency_lineage(
    extraction: StructuredExtraction | None,
    chunks: Sequence[Chunk],
) -> tuple[str, str]:
    if extraction is None or not chunks:
        return "pending", "requires_extraction"
    parent_child_pairs = {
        (element.parent_id, element.element_id)
        for element in extraction.elements
        if element.parent_id and element.element_id
    }
    if not parent_child_pairs:
        return "failure", "parent_child_elements_missing"
    for chunk in chunks:
        if not _chunk_has_resolvable_element_lineage(chunk, extraction):
            continue
        if not _string_value(chunk.metadata.get("parent_element_ids")):
            continue
        if _chunk_dependency_edges(chunk) & parent_child_pairs:
            return "passed", "ok"
    return "failure", "dependency_metadata_missing"


def _chunk_dependency_edges(chunk: Chunk) -> set[tuple[str, str]]:
    value = chunk.metadata.get("dependency_edges")
    if not isinstance(value, str) or not value.strip():
        return set()
    try:
        raw_edges = json.loads(value)
    except json.JSONDecodeError:
        return set()
    if not isinstance(raw_edges, list):
        return set()
    edges: set[tuple[str, str]] = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, Mapping):
            continue
        parent_id = _string_value(raw_edge.get("parent_id"))
        child_id = _string_value(raw_edge.get("child_id"))
        if parent_id and child_id:
            edges.add((parent_id, child_id))
    return edges


def _check_email_headers(extraction: StructuredExtraction | None) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    raw_text = extraction.raw_text
    if all(label in raw_text for label in ("Subject:", "From:", "To:")):
        return "passed", "ok"
    return "failure", "email_headers_missing"


def _check_thread_body(extraction: StructuredExtraction | None) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    return ("passed", "ok") if "承認" in extraction.raw_text else ("failure", "thread_body_missing")


def _check_attachment_metadata(
    extraction: StructuredExtraction | None,
) -> tuple[str, str]:
    if extraction is None:
        return "pending", "requires_extraction"
    if "attachment_count" in extraction.parser_artifacts:
        return "passed", "ok"
    return "failure", "attachment_count_missing"


def _content_kinds(extraction: StructuredExtraction, chunks: Sequence[Chunk]) -> set[str]:
    kinds: set[str] = set()
    chunk_template = extraction.parser_artifacts.get("chunk_template")
    if chunk_template == "office_sheet":
        kinds.add("sheet")
    elif chunk_template == "office_slide":
        kinds.add("slide")
    for element in extraction.elements:
        if element.content_kind:
            kinds.add(element.content_kind)
        if element.kind == "table":
            kinds.add("table")
        elif element.kind in {"list", "equation", "code"}:
            kinds.add(element.kind)
        elif element.kind in {"text", "title"}:
            kinds.add("text")
    for chunk in chunks:
        content_kind = chunk.metadata.get("content_kind")
        if isinstance(content_kind, str) and content_kind:
            kinds.add(content_kind)
    return kinds


def _normalized_extraction_and_chunk_text(
    extraction: StructuredExtraction,
    chunks: Sequence[Chunk],
) -> str:
    return _normalize_answer(extraction.raw_text + "\n" + "\n".join(chunk.text for chunk in chunks))


def _int_set(value: object) -> set[int]:
    if not _is_sequence(value):
        return set()
    values: set[int] = set()
    for item in value:
        number = _optional_int(item)
        if number is not None:
            values.add(number)
    return values


def _section_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {_normalize_section_label(value)} if _normalize_section_label(value) else set()
    if not _is_sequence(value):
        return set()
    return {section for item in value if (section := _normalize_section_label(item))}


def _normalize_section_label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s*(?:>|/|›|»)\s*", " > ", value.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().casefold()


def _unique_sorted(values: Sequence[str]) -> list[str]:
    return sorted(set(values))


def _split_pending_check(value: str) -> tuple[str, str]:
    check, separator, reason = value.partition(":")
    return check, reason if separator else "pending"


def _staging_required_evidence(check: str, reason: str) -> tuple[str, ...]:
    if check in {"ocr_text", "reading_order", "page_coverage"} or reason.startswith(
        "requires_enterprise_ai"
    ):
        return (
            "Model API extraction completed for fixture",
            "document status is INDEXED",
            "extraction pages/elements are stored without raw text leakage in artifacts",
            "chunk citation metadata includes document_id/chunk_id/page/element lineage",
        )
    if check in {"bbox_citation", "preview_jump", "bbox_coordinate_validity"} or reason == (
        "requires_preview_bbox"
    ):
        return (
            "GET /api/documents/{document_id}/chunks returns bbox metadata",
            "bbox metadata includes bbox_coordinate_mode and bbox_unit",
            "absolute bbox coordinates include page width/height metadata",
            "GET /api/documents/{document_id} preview payload keeps page numbers",
            "DocumentPreviewWorkspace can jump from citation/chunk to page bbox",
        )
    if check == "page_hit_accuracy":
        return (
            "evaluation case retrieves the expected document",
            "top citation overlaps expected page range",
            "page_hit_accuracy threshold passes in staging report",
        )
    if check == "table_qa_accuracy":
        return (
            "staging search answer includes the expected table value",
            "answer citation points to a table/sheet chunk with lineage",
            "table_qa_accuracy threshold passes in staging report",
        )
    if check == "dependency_lineage":
        return (
            "staging search returns the target document citation",
            "citation metadata includes parent_element_ids and dependency_edges",
            "dependency_edges match extraction parent-child element lineage",
        )
    if check == "dependency_context_recall":
        return (
            "staging search final citations include dependency-promoted context",
            "promoted citation metadata covers expected parent-child element ids",
            "dependency_context_recall threshold passes in staging report",
        )
    if check == "structural_section_coverage":
        return (
            "manifest expected_sections are present in extraction/chunk metadata",
            "staging search returns citations from every expected section",
            "citations include section_path or section_title lineage metadata",
        )
    if check == "quality_report_metadata":
        return (
            "DocumentDetail.extraction.quality_report includes parser profile/backend/version",
            "quality_report includes fallback/page/table/figure/formula/segment count metadata",
            "quality_report contains stable warning codes and no OCR/source text",
            "ingestion_quality_report_completeness threshold passes in staging report",
        )
    if check in {"knowledge_base_membership", "searchable_canonical"}:
        return (
            "duplicate KB membership routes to canonical chunks",
            "duplicate upload records duplicate/alias metadata",
            "search scoped to target knowledge base returns canonical chunks",
        )
    if check == "artifact_reuse":
        return (
            "successful segment extraction artifact is stored in the artifact storage",
            "retry only reprocesses failed segment ids",
            "ingestion-segments API shows successful segment artifact path retained",
        )
    return ("staging evidence is required for this pending check",)


def _staging_suggested_gate(check: str, reason: str) -> str:
    if check in {"knowledge_base_membership", "searchable_canonical"}:
        return "duplicate_kb_membership_gate"
    if check in {"bbox_citation", "preview_jump", "bbox_coordinate_validity"} or reason == (
        "requires_preview_bbox"
    ):
        return "preview_bbox_citation_gate"
    if check == "artifact_reuse":
        return "segment_artifact_reuse_gate"
    if check == "page_hit_accuracy":
        return "file_processing_page_hit_gate"
    if check == "table_qa_accuracy":
        return "table_qa_search_gate"
    if check == "dependency_lineage":
        return "dependency_lineage_search_gate"
    if check == "dependency_context_recall":
        return "dependency_context_recall_gate"
    if check == "structural_section_coverage":
        return "structural_section_search_gate"
    if check == "quality_report_metadata":
        return "quality_report_metadata_gate"
    if reason.startswith("requires_enterprise_ai"):
        return "enterprise_ai_file_extraction_gate"
    return "file_processing_staging_gate"


def _table_qa_requires_staging(case: Mapping[str, object]) -> bool:
    return "table_qa_accuracy" in _string_set(case.get("required_checks")) and bool(
        _string_value(case.get("expected_answer"))
    )


def _dependency_lineage_requires_staging(case: Mapping[str, object]) -> bool:
    return "dependency_lineage" in _string_set(case.get("required_checks")) and bool(
        _string_value(case.get("staging_query"))
    )


def _structural_section_requires_staging(case: Mapping[str, object]) -> bool:
    return (
        "structural_section_coverage" in _string_set(case.get("required_checks"))
        and bool(_section_set(case.get("expected_sections")))
        and bool(_string_value(case.get("staging_query")))
    )


def parser_fallback_rate(extractions: Sequence[Mapping[str, object]]) -> float:
    """extraction payload 群から parser fallback 率を返す。"""
    if not extractions:
        return 0.0
    fallback_count = 0
    for extraction in extractions:
        quality = _mapping(extraction.get("quality_report"))
        artifacts = _mapping(extraction.get("parser_artifacts"))
        if bool(quality.get("fallback_used")) or bool(artifacts.get("fallback_used")):
            fallback_count += 1
    return fallback_count / len(extractions)


def extraction_page_coverage(extractions: Sequence[Mapping[str, object]]) -> float:
    """quality_report の page coverage 平均を返す。"""
    coverages: list[float] = []
    for extraction in extractions:
        quality = _mapping(extraction.get("quality_report"))
        value = _optional_float(quality.get("page_coverage"))
        if value is not None:
            coverages.append(value)
    if not coverages:
        return 0.0
    return sum(coverages) / len(coverages)


def low_confidence_document_rate(extractions: Sequence[Mapping[str, object]]) -> float:
    """低信頼 element を含む extraction の割合を返す。"""
    if not extractions:
        return 0.0
    low_confidence_count = 0
    for extraction in extractions:
        quality = _mapping(extraction.get("quality_report"))
        warnings = quality.get("quality_warnings")
        warning_items = warnings if isinstance(warnings, list) else []
        if _positive_int(quality.get("low_confidence_count")) > 0 or any(
            str(warning) in {"low_confidence_elements", "low_extraction_confidence"}
            for warning in warning_items
        ):
            low_confidence_count += 1
    return low_confidence_count / len(extractions)


def failed_segment_rate(extractions: Sequence[Mapping[str, object]]) -> float:
    """失敗 segment を持つ extraction の割合を返す。"""
    if not extractions:
        return 0.0
    failed_count = 0
    for extraction in extractions:
        quality = _mapping(extraction.get("quality_report"))
        artifacts = _mapping(extraction.get("parser_artifacts"))
        if (
            _positive_int(quality.get("failed_segment_count")) > 0
            or _positive_int(artifacts.get("failed_segment_count")) > 0
            or _positive_int(artifacts.get("failed_segments")) > 0
        ):
            failed_count += 1
    return failed_count / len(extractions)


def table_qa_accuracy(results: Sequence[TableQaResult]) -> float:
    """表 QA の実回答が期待値を含む割合を返す。"""
    if not results:
        return 0.0
    hits = 0
    for result in results:
        expected = _normalize_answer(result.expected_answer)
        actual = _normalize_answer(result.actual_answer)
        if expected and expected in actual:
            hits += 1
    return hits / len(results)


def page_hit_accuracy(
    cases: Sequence[PageHitCase],
    retrieved_by_case: Mapping[str, Sequence[RetrievedChunk | Mapping[str, object]]],
) -> float:
    """期待 document/page range に citation が命中した割合を返す。"""
    if not cases:
        return 0.0
    hits = 0
    for case in cases:
        retrieved = retrieved_by_case.get(case.case_id, [])
        if any(_retrieved_hits_expected_page(item, case) for item in retrieved):
            hits += 1
    return hits / len(cases)


def citation_traceability_coverage(
    citations: Sequence[RetrievedChunk | Mapping[str, object]],
) -> float:
    """citation が document/chunk/page/element lineage を持つ割合を返す。"""
    if not citations:
        return 0.0
    traceable = sum(1 for citation in citations if _has_traceable_citation_metadata(citation))
    return traceable / len(citations)


def bbox_citation_coverage(
    citations: Sequence[RetrievedChunk | Mapping[str, object]],
) -> float:
    """citation が preview へ位置決め可能な bbox を持つ割合を返す。"""
    if not citations:
        return 0.0
    with_bbox = sum(1 for citation in citations if _citation_bbox(citation) is not None)
    return with_bbox / len(citations)


def bbox_coordinate_validity_coverage(
    citations: Sequence[RetrievedChunk | Mapping[str, object]],
) -> float:
    """bbox 付き citation のうち overlay 可能な coordinate metadata を持つ割合。"""
    bbox_citations = [citation for citation in citations if _citation_bbox(citation) is not None]
    if not bbox_citations:
        return 0.0
    valid = sum(1 for citation in bbox_citations if _citation_bbox_coordinate_valid(citation))
    return valid / len(bbox_citations)


def element_lineage_coverage(
    citations: Sequence[RetrievedChunk | Mapping[str, object]],
) -> float:
    """citation が element_ids を保持している割合を返す。"""
    if not citations:
        return 0.0
    with_lineage = sum(1 for citation in citations if _citation_element_ids(citation))
    return with_lineage / len(citations)


def _retrieved_hits_expected_page(
    item: RetrievedChunk | Mapping[str, object],
    case: PageHitCase,
) -> bool:
    document_id = (
        item.document_id if isinstance(item, RetrievedChunk) else str(item.get("document_id"))
    )
    if document_id != case.expected_document_id:
        return False
    metadata = _citation_metadata(item)
    page_start = _optional_int(_citation_value(item, metadata, "page_start"))
    page_end = _optional_int(_citation_value(item, metadata, "page_end")) or page_start
    if page_start is None or page_end is None:
        return False
    retrieved_pages = set(range(page_start, page_end + 1))
    return bool(retrieved_pages & set(case.expected_pages))


def _has_traceable_citation_metadata(
    item: RetrievedChunk | Mapping[str, object],
) -> bool:
    """引用を原文の位置へたどれるか（文書・chunk と、頁か表計算のシートの位置）。

    頁のある引用（PDF・画像など）は頁と、要素の ID・bbox・節のどれかがあればたどれる。
    表計算（Excel の行の記録。#1221）の引用は頁を持たないので、シート名と行（``row_start``）・
    セル範囲・要素の ID のどれかがあればたどれる（#1456）。

    D の評価の、MCP の読み取りのツール（``rag_read_document`` / ``rag_read_section``）で読んだ
    chunk の引用は、要素の ID の代わりに要素の定位子（``element_locator``。``…/el:…`` を含むもの）
    を持ち、節を見出しの列（``section_path`` の配列）で持つので、どちらも位置として数える（#1471）。
    """
    document_id = _citation_document_id(item)
    chunk_id = _citation_chunk_id(item)
    if not document_id or not chunk_id:
        return False
    metadata = _citation_metadata(item)
    page_start = _optional_int(_citation_value(item, metadata, "page_start"))
    page_end = _optional_int(_citation_value(item, metadata, "page_end")) or page_start
    element = bool(_citation_element_ids(item) or _citation_element_locator(item, metadata))
    if page_start is None or page_end is None:
        return bool(
            _string_value(_citation_value(item, metadata, "sheet_name"))
            and (
                _optional_int(_citation_value(item, metadata, "row_start")) is not None
                or _string_value(_citation_value(item, metadata, "cell_range"))
                or element
            )
        )
    return bool(
        element
        or _citation_bbox(item) is not None
        or _section_path_value(_citation_value(item, metadata, "section_path"))
    )


def _citation_element_locator(
    item: RetrievedChunk | Mapping[str, object], metadata: Mapping[str, object]
) -> bool:
    """要素の定位子（``doc:…/ext:…/page:…/el:…``。#1330）を持つか（要素を指さない定位子は数えない）。"""
    value = _string_value(_citation_value(item, metadata, "element_locator"))
    return "/el:" in value


def _section_path_value(value: object) -> bool:
    """節の位置（文字列か、空でない見出しの列。#1471）を持つか。"""
    if isinstance(value, list | tuple):
        return any(_string_value(part) for part in value)
    return bool(_string_value(value))


def _citation_document_id(item: RetrievedChunk | Mapping[str, object]) -> str:
    if isinstance(item, RetrievedChunk):
        return item.document_id
    value = item.get("document_id")
    return str(value).strip() if value is not None else ""


def _citation_chunk_id(item: RetrievedChunk | Mapping[str, object]) -> str:
    if isinstance(item, RetrievedChunk):
        return item.chunk_id
    value = item.get("chunk_id")
    return str(value).strip() if value is not None else ""


def _citation_metadata(
    item: RetrievedChunk | Mapping[str, object],
) -> Mapping[str, object]:
    if isinstance(item, RetrievedChunk):
        return item.metadata
    return _mapping(item.get("metadata"))


def _citation_value(
    item: RetrievedChunk | Mapping[str, object],
    metadata: Mapping[str, object],
    key: str,
) -> object:
    if not isinstance(item, RetrievedChunk) and key in item:
        return item[key]
    return metadata.get(key)


def _citation_element_ids(
    item: RetrievedChunk | Mapping[str, object],
) -> tuple[str, ...]:
    if not isinstance(item, RetrievedChunk) and "element_ids" in item:
        return _id_tuple(item.get("element_ids"))
    return _id_tuple(_citation_metadata(item).get("element_ids"))


def _citation_bbox(
    item: RetrievedChunk | Mapping[str, object],
) -> tuple[float, ...] | None:
    if not isinstance(item, RetrievedChunk) and "bbox" in item:
        return _bbox_tuple(item.get("bbox"))
    return _bbox_tuple(_citation_metadata(item).get("bbox"))


def _citation_bbox_coordinate_valid(item: RetrievedChunk | Mapping[str, object]) -> bool:
    metadata = _citation_metadata(item)
    bbox_value = _citation_value(item, metadata, "bbox")
    return _bbox_coordinate_violation(bbox_value, metadata=metadata) is None


def _bbox_coordinate_violation(
    bbox_value: object,
    *,
    metadata: Mapping[str, object],
) -> str | None:
    bbox = _bbox_tuple(bbox_value)
    if bbox is None:
        return "bbox_invalid"
    mode = _bbox_coordinate_mode(metadata)
    if mode is None:
        return "bbox_coordinate_mode_missing"
    unit = _bbox_coordinate_unit(metadata)
    if unit is None:
        return "bbox_unit_missing"
    page_rotation = _bbox_page_rotation(metadata)
    if page_rotation is not None and page_rotation < 0:
        return "bbox_page_rotation_invalid"
    if unit in {"ratio", "percent"}:
        max_value = 1.0 if unit == "ratio" else 100.0
        if any(value < 0 or value > max_value for value in bbox[:4]):
            return "bbox_coordinate_out_of_range"
        if mode == "xyxy" and not (bbox[2] > bbox[0] and bbox[3] > bbox[1]):
            return "bbox_xyxy_area_invalid"
        if mode == "xywh" and not (bbox[2] > 0 and bbox[3] > 0):
            return "bbox_xywh_area_invalid"
        if mode == "xywh" and (bbox[0] + bbox[2] > max_value or bbox[1] + bbox[3] > max_value):
            return "bbox_xywh_out_of_bounds"
    elif unit == "absolute":
        width = _optional_float(
            metadata.get("page_width") or metadata.get("width") or metadata.get("page_w")
        )
        height = _optional_float(
            metadata.get("page_height") or metadata.get("height") or metadata.get("page_h")
        )
        if width is None or height is None or width <= 0 or height <= 0:
            return "bbox_absolute_page_size_missing"
        if mode == "xyxy" and not (
            0 <= bbox[0] < bbox[2] <= width and 0 <= bbox[1] < bbox[3] <= height
        ):
            return "bbox_absolute_xyxy_out_of_bounds"
        if mode == "xywh" and not (
            bbox[0] >= 0
            and bbox[1] >= 0
            and bbox[2] > 0
            and bbox[3] > 0
            and bbox[0] + bbox[2] <= width
            and bbox[1] + bbox[3] <= height
        ):
            return "bbox_absolute_xywh_out_of_bounds"
    else:
        return "bbox_unit_unknown"
    return None


def _bbox_coordinate_mode(metadata: Mapping[str, object]) -> str | None:
    for key in ("bbox_coordinate_mode", "bbox_mode", "bbox_format", "coordinate_mode"):
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        if normalized in {"xyxy", "x1_y1_x2_y2"}:
            return "xyxy"
        if normalized in {"xywh", "x_y_width_height", "left_top_width_height"}:
            return "xywh"
    return None


def _bbox_coordinate_unit(metadata: Mapping[str, object]) -> str | None:
    for key in ("bbox_unit", "bbox_coordinate_unit", "coordinate_unit", "unit"):
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"[^a-z0-9%]+", "_", value.casefold()).strip("_")
        if normalized in {"ratio", "relative", "normalized", "fraction"}:
            return "ratio"
        if normalized in {"percent", "percentage", "%"}:
            return "percent"
        if normalized in {"absolute", "pixel", "pixels", "px", "point", "points", "pt"}:
            return "absolute"
    return None


def _bbox_page_rotation(metadata: Mapping[str, object]) -> int | None:
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
    number = _optional_float(value)
    if number is None or not number.is_integer():
        return -1
    normalized = int(number) % 360
    return normalized if normalized in {0, 90, 180, 270} else -1


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _string_set(value: object) -> set[str]:
    if not _is_sequence(value):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


def _missing_required_case_fields(case: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            field
            for field in _REQUIRED_CASE_FIELDS
            if not _has_non_empty_manifest_field(case, field)
        )
    )


def _has_non_empty_manifest_field(case: Mapping[str, object], field: str) -> bool:
    value = case.get(field)
    if field == "required_checks":
        return bool(_string_set(value))
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _resolve_fixture_root(manifest: Mapping[str, object], *, manifest_path: Path) -> Path:
    root = _string_value(manifest.get("fixture_root")) or "."
    return (manifest_path.parent / root).resolve()


def _validate_fixture_extension(
    case: Mapping[str, object],
    *,
    case_id: str,
    fixture_relative: Path,
    errors: list[str],
) -> None:
    modality = _string_value(case.get("modality"))
    expected_extensions = _FIXTURE_EXTENSIONS_BY_MODALITY.get(modality)
    if expected_extensions is None:
        return
    extension = fixture_relative.suffix.casefold()
    if extension not in expected_extensions:
        errors.append(
            f"case[{case_id}]:fixture_extension_mismatch:{modality}:{extension or '<none>'}"
        )


def _validate_fixture_reference(
    case: Mapping[str, object],
    *,
    field: str,
    fixture_root: Path,
    case_id: str,
    errors: list[str],
) -> None:
    fixture_name = _string_value(case.get(field))
    if not fixture_name:
        errors.append(f"case[{case_id}]:{field}_missing_name")
        return
    fixture_relative = Path(fixture_name)
    if fixture_relative.is_absolute() or ".." in fixture_relative.parts:
        errors.append(f"case[{case_id}]:{field}_unsafe_path:{fixture_name}")
        return
    _validate_fixture_extension(
        case,
        case_id=case_id,
        fixture_relative=fixture_relative,
        errors=errors,
    )
    fixture_path = fixture_root / fixture_relative
    try:
        stat = fixture_path.stat()
    except FileNotFoundError:
        errors.append(f"case[{case_id}]:{field}_not_found:{fixture_name}")
        return
    if not fixture_path.is_file():
        errors.append(f"case[{case_id}]:{field}_not_file:{fixture_name}")
        return
    if stat.st_size <= 0:
        errors.append(f"case[{case_id}]:{field}_empty:{fixture_name}")


def _has_case_assertion(case: Mapping[str, object], field: str) -> bool:
    value = case.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    if _is_sequence(value):
        return bool(value)
    return value is not None


def _id_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.startswith("["):
            try:
                decoded = json.loads(cleaned)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                return tuple(
                    str(item).strip()
                    for item in decoded
                    if isinstance(item, str | int) and str(item).strip()
                )
        cleaned = cleaned.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
        return tuple(item.strip() for item in cleaned.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return tuple(
            str(item).strip() for item in value if isinstance(item, str | int) and str(item).strip()
        )
    return ()


def _bbox_tuple(value: object) -> tuple[float, ...] | None:
    if value is None:
        return None
    raw_value = value
    if isinstance(value, str):
        try:
            raw_value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, bytes | bytearray | str):
        return None
    values: list[float] = []
    for item in raw_value:
        number = _optional_float(item)
        if number is None:
            return None
        values.append(number)
    if len(values) < 4:
        return None
    bbox = tuple(values[:4])
    return bbox if _bbox_has_positive_area(bbox) else None


def _bbox_has_positive_area(bbox: Sequence[float]) -> bool:
    x, y, right_or_width, bottom_or_height = bbox[:4]
    if not all(math.isfinite(value) for value in (x, y, right_or_width, bottom_or_height)):
        return False
    if x < 0 or y < 0:
        return False
    return not (right_or_width <= 0 or bottom_or_height <= 0)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _positive_int(value: object) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else 0


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


def _threshold_value(value: object, direction: str) -> float | None:
    config = _mapping(value)
    return _optional_float(config.get(direction))


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_answer(value: str) -> str:
    return "".join(value.casefold().split())
