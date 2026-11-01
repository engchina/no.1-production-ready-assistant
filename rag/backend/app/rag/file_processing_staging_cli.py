"""file-processing golden set の staging gate CLI。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pr_backend_core.logging import configure_cli_logging

from app.config import Settings, get_settings
from app.rag.chunk_template_scorecard import (
    ChunkTemplateScorecard,
    build_chunk_template_scorecard,
)
from app.rag.file_processing_evaluation import staging_dataset_policy_summary
from app.rag.file_processing_staging import (
    FileProcessingStagingCaseResult,
    FileProcessingStagingReport,
    run_file_processing_staging_checks_with_real_clients,
)
from app.rag.parser_adapter_contract import (
    BLOCKING_FAILURE_STATUSES,
    ParserAdapterCompatibilityMatrix,
    parser_adapter_contract_artifact_payload,
    parser_adapter_contract_summary,
    parser_adapter_fixture_root_from_manifest,
    parser_adapter_fixture_specs_from_manifest,
    parser_service_probe_settings,
    run_parser_adapter_compatibility_matrix,
    strict_parser_adapter_settings,
)
from app.rag.parser_adapter_readiness import (
    ParserAdapterRuntimeSettings,
    parser_adapter_runtime_settings,
)
from app.rag.parser_adapter_routing import normalize_source_kind
from app.rag.parser_adapter_scorecard import (
    ParserAdapterScoreBackend,
    ParserAdapterScorecard,
    ParserAdapterSourceRoute,
    build_parser_adapter_scorecard,
    build_parser_adapter_source_routes,
)
from app.rag.request_context import with_system_access
from app.rag.staging_smoke import SmokePreflightResult, staging_smoke_preflight

PROMOTION_THRESHOLD_GUARDS: Mapping[str, tuple[str, float]] = {
    "retrieval_recall": ("min", 0.9),
    "table_qa_accuracy": ("min", 1.0),
    "page_hit_accuracy": ("min", 0.9),
    "citation_traceability_coverage": ("min", 0.8),
    "bbox_citation_coverage": ("min", 0.8),
    "bbox_coordinate_validity_coverage": ("min", 0.9),
    "preview_addressability_coverage": ("min", 0.8),
    "element_lineage_coverage": ("min", 0.9),
    "chunk_block_integrity": ("min", 1.0),
    "reading_order_consistency": ("min", 1.0),
    "structural_section_coverage": ("min", 1.0),
    "dependency_context_recall": ("min", 1.0),
    "table_structure_fidelity": ("min", 1.0),
    "table_cell_lineage_coverage": ("min", 1.0),
    "table_row_tree_fidelity": ("min", 1.0),
    "visual_chunk_metadata_completeness": ("min", 1.0),
    "chunk_size_compliance": ("min", 1.0),
    "chunk_contextual_coherence": ("min", 1.0),
    "cross_page_table_continuity_coverage": ("min", 1.0),
    "ingestion_quality_report_completeness": ("min", 1.0),
    "parser_warning_taxonomy_coverage": ("min", 1.0),
    "parser_routing_accuracy": ("min", 1.0),
    "source_kind_coverage": ("min", 1.0),
    "backend_source_kind_coverage": ("min", 1.0),
    "adapter_contract_coverage": ("min", 1.0),
    "parser_fallback_rate": ("max", 0.2),
    "failed_segment_rate": ("max", 0.25),
}

ADAPTER_GOLDEN_GATE_SOURCE_KINDS = ("pdf", "office", "html", "email", "image")
ADAPTER_GOLDEN_GATE_METRICS = (
    "table_qa_accuracy",
    "page_hit_accuracy",
    "parser_fallback_rate",
    "bbox_citation_coverage",
    "bbox_coordinate_validity_coverage",
    "preview_addressability_coverage",
    "adapter_contract_coverage",
    "backend_source_kind_coverage",
)
PROMOTION_POLICY_REQUIRED_RUNTIME_CHECKS = ("extraction_artifact_cache_roundtrip",)

STAGING_GATE_EVIDENCE_ALLOWLIST = frozenset(
    {
        "source_kind",
        "parser_backend",
        "parser_profile",
        "status",
        "chunk_count",
        "segment_count",
        "bbox_chunk_count",
        "preview_addressable_chunk_count",
        "extraction_bbox_target_count",
        "extraction_preview_addressable_target_count",
        "element_lineage_chunk_count",
        "traceable_chunk_count",
        "artifact_segment_count",
        "artifact_full_present",
        "artifact_full_uri_scheme",
        "artifact_full_local_uri",
        "artifact_full_readable",
        "artifact_full_identity_verified",
        "artifact_full_payload_bytes",
        "artifact_segment_expected_count",
        "artifact_segment_local_uri_count",
        "artifact_segment_non_local_uri_count",
        "artifact_segment_readable_count",
        "artifact_segment_identity_verified_count",
        "artifact_segment_payload_bytes",
        "artifact_integrity_error_count",
        "initial_retry_segment_count",
        "retry_segment_count",
        "retry_initial_failed_segment_count",
        "retry_initial_successful_segment_count",
        "retry_initial_successful_segment_artifact_count",
        "retry_retained_successful_segment_artifact_count",
        "retry_rewritten_successful_segment_artifact_count",
        "retry_reprocessed_successful_segment_count",
        "retry_failed_segment_retried_count",
        "retry_failed_segment_succeeded_count",
        "segment_cache_miss_count",
        "segment_cache_miss_warning",
        "full_artifact_cached",
        "full_artifact_reused",
        "full_artifact_identity_present",
        "failed_segment_count",
        "parser_fallback_used",
        "extraction_page_coverage",
        "low_confidence_count",
        "quality_report_complete",
        "ingestion_elapsed_ms",
        "retrieval_hit",
        "retrieval_traceable",
        "search_executed",
        "search_page_hit",
        "search_page_traceable",
        "table_qa_answer_hit",
        "table_qa_traceable",
        "table_qa_cell_refs_traceable",
        "table_qa_cell_refs_resolvable",
        "table_qa_cell_refs_expected_count",
        "table_qa_cell_refs_resolved_count",
        "table_qa_cell_refs_covered_count",
        "dependency_lineage_traceable",
        "dependency_context_traceable",
        "dependency_context_expected_count",
        "dependency_context_covered_count",
        "structural_section_traceable",
        "structural_section_expected_count",
        "structural_section_covered_count",
        "search_citation_count",
        "search_elapsed_ms",
        "groundedness_passed",
        "groundedness_score",
        "knowledge_base_search_hit",
        "knowledge_base_search_traceable",
        "ingestion_error_type",
    }
)
STAGING_RUNTIME_EVIDENCE_ALLOWLIST = frozenset(
    {
        "artifact_cache_enabled",
        "cleanup",
        "error_type",
        "object_ref_hash",
        "object_uri_scheme",
        "payload_bytes",
    }
)
SENSITIVE_STAGING_PAYLOAD_KEYS = frozenset(
    {
        "answer",
        "content",
        "element_text",
        "extracted_text",
        "html",
        "markdown",
        "ocr_text",
        "prompt",
        "query",
        "raw_text",
        "table_html",
        "table_text",
        "text",
    }
)


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    configure_cli_logging("rag")
    parser = argparse.ArgumentParser(
        prog="rag-file-processing-staging",
        description=(
            "file-processing golden manifest の pending checks を staging 実環境で検証します。"
        ),
    )
    parser.add_argument("manifest", type=Path, help="file-processing golden manifest JSON")
    parser.add_argument("--output", type=Path, help="結果 JSON の保存先。未指定なら stdout。")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "実行後に作成した staging document/object/KB を best-effort で削除または "
            "archive します。"
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "外部依存へ接続せず、file-processing staging 実行前の設定チェックだけを JSON 出力する。"
        ),
    )
    parser.add_argument(
        "--parser-adapter-contract-strict",
        action="store_true",
        help=(
            "Docling / Unstructured の feature flag を staging smoke 用に "
            "有効化し、preflight / 実 staging / adapter contract artifact を同じ "
            "strict 設定で検証する。"
        ),
    )
    parser.add_argument(
        "--require-real-world-policy",
        action="store_true",
        help=(
            "production promotion 用に staging_dataset_policy の設定と合規を必須にする。"
            "synthetic manifest だけの staging 実行を preflight で止めます。"
        ),
    )
    parser.add_argument(
        "--trend-output",
        type=Path,
        help=(
            "nightly trend 用の非機密サマリ JSON 保存先。"
            "case_results、gate evidence、OCR 原文、chunk 本文は含めません。"
        ),
    )
    args = parser.parse_args(argv)
    try:
        manifest = _load_manifest(args.manifest)
        # adapter の可用性は、parse を委譲する parser サービスの /health で判定する。
        # 手で実行するときも RAG_PARSER_READINESS_PROBE_ENABLED の指定に依らない(#343 / #366)。
        settings = parser_service_probe_settings(get_settings())
        effective_settings = (
            strict_parser_adapter_settings(settings)
            if args.parser_adapter_contract_strict
            else settings
        )
        preflight = staging_smoke_preflight(settings=settings)
        preflight_payload = _preflight_payload(
            preflight,
            effective_settings,
            manifest=manifest,
            manifest_path=args.manifest,
            parser_adapter_contract_strict=args.parser_adapter_contract_strict,
            require_real_world_policy=args.require_real_world_policy,
        )
        if args.preflight_only:
            _write_payload(preflight_payload, args.output)
            _write_trend_payload(
                preflight_payload,
                args.trend_output,
                kind="file_processing_staging_preflight",
            )
            return 0 if preflight_payload["passed"] else 1
        if not preflight_payload["passed"]:
            _write_payload(preflight_payload, args.output)
            _write_trend_payload(
                preflight_payload,
                args.trend_output,
                kind="file_processing_staging_preflight",
            )
            return 1
        report = asyncio.run(
            run_file_processing_staging_checks_with_real_clients(
                manifest,
                manifest_path=args.manifest,
                cleanup=args.cleanup,
                settings=effective_settings,
            )
        )
        payload = _report_payload(
            report,
            manifest=manifest,
            manifest_path=args.manifest,
            settings=effective_settings,
            parser_adapter_contract_strict=args.parser_adapter_contract_strict,
            require_real_world_policy=args.require_real_world_policy,
        )
        _write_payload(payload, args.output)
        _write_trend_payload(payload, args.trend_output, kind="file_processing_staging")
    except FileProcessingStagingCliError as exc:
        print(f"file-processing staging エラー: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        payload = {"passed": False, "error_type": type(exc).__name__}
        _write_payload(payload, args.output)
        _write_trend_payload(payload, args.trend_output, kind="file_processing_staging_error")
        return 3
    return 0 if report.passed and bool(payload["promotion_ready"]) else 1


class FileProcessingStagingCliError(RuntimeError):
    """CLI 利用者へ返す安全なエラー。"""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileProcessingStagingCliError(f"manifest が見つかりません: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FileProcessingStagingCliError(
            f"manifest が JSON として読めません: line={exc.lineno}, column={exc.colno}"
        ) from exc
    if not isinstance(raw, dict):
        raise FileProcessingStagingCliError("manifest root は JSON object にしてください。")
    return raw


def _report_payload(
    report: FileProcessingStagingReport,
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path | None = None,
    settings: Settings,
    parser_adapter_contract_strict: bool = False,
    require_real_world_policy: bool = False,
) -> dict[str, Any]:
    settings = (
        strict_parser_adapter_settings(settings) if parser_adapter_contract_strict else settings
    )
    payload = asdict(report)
    parser_adapters = parser_adapter_runtime_settings(settings)
    parser_scorecard = build_parser_adapter_scorecard(
        parser_adapters,
        metrics=report.metrics,
        metrics_source="file_processing_staging",
    )
    parser_adapter_fixture_specs = parser_adapter_fixture_specs_from_manifest(
        manifest,
        require_declared_schema_remap=parser_adapter_contract_strict,
    )
    parser_adapter_fixture_root = parser_adapter_fixture_root_from_manifest(
        manifest,
        manifest_path=manifest_path,
    )
    parser_adapter_contract = run_parser_adapter_compatibility_matrix(
        settings,
        fixture_root=parser_adapter_fixture_root,
        source_kinds=_source_kinds_from_manifest(manifest),
        fixture_specs=parser_adapter_fixture_specs,
        require_backend_evidence=parser_adapter_contract_strict,
    )
    parser_source_routes = _contract_aware_source_routes(
        build_parser_adapter_source_routes(
            parser_adapters,
            source_kinds=_source_kinds_from_manifest(manifest),
        ),
        parser_adapter_contract,
    )
    backend_source_kind_matrix = _backend_source_kind_matrix(report)
    observed_chunk_templates = _observed_chunk_templates(manifest)
    chunk_scorecard = build_chunk_template_scorecard(
        metrics=report.metrics,
        observed_templates=observed_chunk_templates,
        metrics_source="file_processing_staging",
        template_evidence=(
            _chunk_template_manifest_evidence(manifest, report) if report.case_results else None
        ),
    )
    adapter_golden_gate = _adapter_golden_gate(
        report,
        manifest=manifest,
        parser_scorecard=parser_scorecard,
        parser_adapter_contract=parser_adapter_contract,
        backend_source_kind_matrix=backend_source_kind_matrix,
        parser_source_routes=parser_source_routes,
        parser_adapter_contract_strict=parser_adapter_contract_strict,
    )
    object_storage_artifact_chain = _object_storage_artifact_chain(report)
    staging_dataset_policy = _staging_dataset_policy_summary(manifest, report)
    promotion_blockers = _promotion_blockers(
        report,
        manifest=manifest,
        staging_dataset_policy=staging_dataset_policy,
        require_real_world_policy=require_real_world_policy,
        parser_scorecard=parser_scorecard,
        chunk_scorecard=chunk_scorecard,
        parser_adapter_contract=parser_adapter_contract,
        adapter_golden_gate=adapter_golden_gate,
        object_storage_artifact_chain=object_storage_artifact_chain,
    )
    payload.update(
        {
            "passed": report.passed,
            "promotion_ready": not promotion_blockers,
            "promotion_blockers": promotion_blockers,
            "parser_adapters": asdict(parser_adapters),
            "parser_adapter_scorecard": asdict(parser_scorecard),
            "parser_adapter_source_routes": [asdict(route) for route in parser_source_routes],
            "parser_adapter_contract": parser_adapter_contract_artifact_payload(
                parser_adapter_contract
            ),
            "parser_adapter_contract_mode": (
                "strict" if parser_adapter_contract_strict else "runtime"
            ),
            "adapter_contract_matrix_summary": _parser_adapter_contract_artifact_summary(
                parser_adapter_contract
            ),
            "backend_source_kind_matrix": backend_source_kind_matrix,
            "adapter_golden_gate": adapter_golden_gate,
            "object_storage_artifact_chain": object_storage_artifact_chain,
            "chunk_template_scorecard": asdict(chunk_scorecard),
            "staging_policy": _staging_policy(manifest),
            "staging_dataset_policy": staging_dataset_policy,
            "case_count": report.case_count,
            "gate_count": report.gate_count,
            "failure_count": report.failure_count,
        }
    )
    _apply_parser_adapter_contract_metric(payload, parser_adapter_contract)
    return _sanitize_staging_payload(payload)


def _backend_source_kind_matrix(report: FileProcessingStagingReport) -> dict[str, object]:
    evidence = _mapping(report.metric_evidence.get("backend_source_kind_coverage"))
    if not evidence:
        return {
            "value": report.metrics.get("backend_source_kind_coverage"),
            "required_source_kinds": [],
            "covered_source_kinds": [],
            "missing_source_kinds": [],
            "backend_source_kinds": {},
            "backend_case_ids": {},
        }
    return dict(evidence)


def _contract_aware_source_routes(
    routes: Sequence[ParserAdapterSourceRoute],
    matrix: ParserAdapterCompatibilityMatrix,
) -> tuple[ParserAdapterSourceRoute, ...]:
    """staging source route を real schema-remap contract の証跡で補正する。"""
    passed_pairs: set[tuple[str, str]] = {
        (str(case.backend), str(case.source_kind))
        for case in matrix.cases
        if case.status == "passed"
    }
    if not passed_pairs:
        return tuple(routes)
    return tuple(_contract_aware_source_route(route, passed_pairs=passed_pairs) for route in routes)


def _contract_aware_source_route(
    route: ParserAdapterSourceRoute,
    *,
    passed_pairs: set[tuple[str, str]],
) -> ParserAdapterSourceRoute:
    selected = route.selected_backend
    if selected == "local" or (selected, route.source_kind) in passed_pairs:
        return route
    verified_backend: ParserAdapterScoreBackend = next(
        (backend for backend in route.active_order if (backend, route.source_kind) in passed_pairs),
        "local",
    )
    reason_codes = [
        *route.reason_codes,
        "contract_aware_source_route",
        (
            "contract_verified_alternative_selected"
            if verified_backend != "local"
            else "local_fallback_due_to_contract_gap"
        ),
    ]
    warning_codes = [
        *route.warning_codes,
        f"{selected}_adapter_contract_unverified_for_source",
    ]
    return ParserAdapterSourceRoute(
        source_kind=route.source_kind,
        candidate_order=route.candidate_order,
        attempted_order=route.attempted_order,
        active_order=route.active_order,
        selected_backend=verified_backend,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        warning_codes=tuple(dict.fromkeys(warning_codes)),
    )


def _apply_parser_adapter_contract_metric(
    payload: dict[str, Any],
    matrix: ParserAdapterCompatibilityMatrix,
) -> None:
    """runtime adapter contract の合否を staging artifact の metric に反映する。"""
    value = 1.0 if matrix.passed else 0.0
    summary = parser_adapter_contract_summary(matrix)
    metrics = _mapping(payload.get("metrics"))
    metrics["adapter_contract_coverage"] = value
    payload["metrics"] = metrics
    metric_evidence = _mapping(payload.get("metric_evidence"))
    metric_evidence["adapter_contract_coverage"] = {
        "source": "parser_adapter_contract",
        "passed": matrix.passed,
        "case_count": matrix.case_count,
        "blocking_failure_count": matrix.blocking_failure_count,
        "missing_source_kinds": summary["missing_source_kinds"],
        "blocking_failure_source_kinds": summary["blocking_failure_source_kinds"],
        "blocking_failure_backends": summary["blocking_failure_backends"],
        "reason_code_counts": summary["reason_code_counts"],
        "warning_code_counts": summary["warning_code_counts"],
        "blocking_failure_reason_counts": summary["blocking_failure_reason_counts"],
    }
    payload["metric_evidence"] = metric_evidence
    if not matrix.passed:
        payload["passed"] = False
    threshold_results = payload.get("threshold_results")
    if not isinstance(threshold_results, list | tuple):
        return
    updated_threshold_results = list(threshold_results)
    payload["threshold_results"] = updated_threshold_results
    for result in updated_threshold_results:
        if not isinstance(result, dict) or result.get("metric") != "adapter_contract_coverage":
            continue
        result["actual"] = value
        result["status"] = "passed" if matrix.passed else "failed"
        result["passed"] = matrix.passed
        if matrix.passed:
            result.pop("reason", None)
        else:
            result["reason"] = "parser_adapter_contract_failed"
        break


def _sanitize_staging_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """staging artifact から OCR/検索/抽出の原文が漏れないよう evidence を allowlist 化する。"""
    sanitized = _redact_sensitive_payload(payload)
    if not isinstance(sanitized, dict):
        return {"passed": False, "error_type": "staging_payload_sanitization_failed"}
    for case_result in sanitized.get("case_results", []):
        if not isinstance(case_result, dict):
            continue
        for gate_result in case_result.get("gate_results", []):
            if not isinstance(gate_result, dict):
                continue
            gate_result["evidence"] = _safe_evidence_mapping(
                gate_result.get("evidence"),
                allowed_keys=STAGING_GATE_EVIDENCE_ALLOWLIST,
            )
    for runtime_check in sanitized.get("runtime_checks", []):
        if not isinstance(runtime_check, dict):
            continue
        runtime_check["evidence"] = _safe_evidence_mapping(
            runtime_check.get("evidence"),
            allowed_keys=STAGING_RUNTIME_EVIDENCE_ALLOWLIST,
        )
    return sanitized


def _safe_evidence_mapping(
    value: object,
    *,
    allowed_keys: frozenset[str],
) -> dict[str, object]:
    evidence = _mapping(value)
    return {
        key: sanitized_value
        for key, raw_value in evidence.items()
        if key in allowed_keys
        if (sanitized_value := _safe_evidence_value(raw_value)) is not None
    }


def _safe_evidence_value(value: object) -> object | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if len(normalized) <= 128 and "\n" not in normalized and "\r" not in normalized:
            return normalized
    return None


def _redact_sensitive_payload(value: object) -> object:
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in SENSITIVE_STAGING_PAYLOAD_KEYS:
                sanitized[key_text] = "[redacted]"
                continue
            sanitized[key_text] = _redact_sensitive_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_redact_sensitive_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_payload(item) for item in value)
    return value


def _contains_sensitive_payload_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in SENSITIVE_STAGING_PAYLOAD_KEYS:
                return True
            if _contains_sensitive_payload_key(item):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_contains_sensitive_payload_key(item) for item in value)
    return False


def _promotion_blockers(
    report: FileProcessingStagingReport,
    *,
    manifest: Mapping[str, Any],
    staging_dataset_policy: Mapping[str, object],
    require_real_world_policy: bool = False,
    parser_scorecard: ParserAdapterScorecard | None = None,
    chunk_scorecard: ChunkTemplateScorecard | None = None,
    parser_adapter_contract: ParserAdapterCompatibilityMatrix | None = None,
    adapter_golden_gate: Mapping[str, Any] | None = None,
    object_storage_artifact_chain: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """本番昇格を止める staging blocker を機械可読に返す。"""
    blockers: list[dict[str, Any]] = []
    blockers.extend(_promotion_policy_blockers(manifest))
    blockers.extend(
        _required_real_world_policy_blockers(
            staging_dataset_policy,
            require_real_world_policy=require_real_world_policy,
        )
    )
    blockers.extend(_staging_dataset_policy_promotion_blockers(staging_dataset_policy))
    if report.local_manifest_errors:
        blockers.append(
            {
                "code": "local_contract_failed",
                "count": len(report.local_manifest_errors),
            }
        )
    failed_runtime_checks = [check for check in report.runtime_checks if not check.passed]
    blockers.extend(
        {
            "code": "runtime_check_failed",
            "check": check.check,
            "failure_code": check.failure_code,
        }
        for check in failed_runtime_checks
    )
    failed_gate_count = sum(
        1
        for case_result in report.case_results
        for gate_result in case_result.gate_results
        if not gate_result.passed
    )
    if failed_gate_count:
        blockers.append({"code": "staging_gate_failed", "count": failed_gate_count})
    blockers.extend(
        {
            "code": "threshold_failed",
            "metric": threshold.metric,
            "reason": threshold.reason,
        }
        for threshold in report.threshold_results
        if not threshold.passed
    )
    runtime_status_by_check = {check.check: check.status for check in report.runtime_checks}
    for check_name in _required_runtime_checks(manifest):
        status = runtime_status_by_check.get(check_name)
        if status == "ok":
            continue
        blockers.append(
            {
                "code": "required_runtime_check_not_ok",
                "check": check_name,
                "status": status or "missing",
            }
        )
    if parser_scorecard is not None:
        blockers.extend(_parser_scorecard_promotion_blockers(parser_scorecard))
    if chunk_scorecard is not None:
        blockers.extend(_chunk_scorecard_promotion_blockers(chunk_scorecard))
    if parser_adapter_contract is not None:
        blockers.extend(_parser_adapter_contract_promotion_blockers(parser_adapter_contract))
    if adapter_golden_gate is not None and bool(
        _staging_policy(manifest)["required_for_promotion"]
    ):
        blockers.extend(_adapter_golden_gate_promotion_blockers(adapter_golden_gate))
    if object_storage_artifact_chain is not None and bool(
        _staging_policy(manifest)["required_for_promotion"]
    ):
        blockers.extend(
            _object_storage_artifact_chain_promotion_blockers(object_storage_artifact_chain)
        )
    blockers.extend(_promotion_threshold_policy_blockers(manifest))
    return blockers


def _required_real_world_policy_blockers(
    summary: Mapping[str, object],
    *,
    require_real_world_policy: bool,
) -> list[dict[str, Any]]:
    """production promotion で synthetic-only staging manifest を禁止する。"""
    if not require_real_world_policy or bool(summary.get("configured")):
        return []
    return [
        {
            "code": "staging_dataset_policy_missing",
            "required_for_promotion": True,
        }
    ]


def _staging_dataset_policy_promotion_blockers(
    summary: Mapping[str, object],
) -> list[dict[str, Any]]:
    """real-world staging dataset policy の迂回や未達を promotion blocker にする。"""
    if not bool(summary.get("configured")):
        return []
    blockers: list[dict[str, Any]] = []
    if not bool(summary.get("required_for_promotion")):
        blockers.append(
            {
                "code": "staging_dataset_policy_not_required",
                "real_world_case_count": _int_value(summary.get("real_world_case_count")),
                "compliant_real_world_case_count": _int_value(
                    summary.get("compliant_real_world_case_count")
                ),
            }
        )
    if not bool(summary.get("promotion_ready")):
        blockers.append(
            {
                "code": "staging_dataset_policy_failed",
                "policy_error_count": _int_value(summary.get("policy_error_count")),
                "min_real_world_cases": _int_value(summary.get("min_real_world_cases")),
                "real_world_case_count": _int_value(summary.get("real_world_case_count")),
                "compliant_real_world_case_count": _int_value(
                    summary.get("compliant_real_world_case_count")
                ),
                "missing_source_kinds": _string_list(summary.get("missing_source_kinds")),
                "missing_scenarios": _string_list(summary.get("missing_scenarios")),
                "sensitivity_violation_count": _int_value(
                    summary.get("sensitivity_violation_count")
                ),
                "review_missing_count": _int_value(summary.get("review_missing_count")),
                "fixture_prefix_mismatch_count": _int_value(
                    summary.get("fixture_prefix_mismatch_count")
                ),
                "executed_real_world_case_count": _int_value(
                    summary.get("executed_real_world_case_count")
                ),
                "executed_compliant_real_world_case_count": _int_value(
                    summary.get("executed_compliant_real_world_case_count")
                ),
                "missing_executed_source_kinds": _string_list(
                    summary.get("missing_executed_source_kinds")
                ),
                "missing_executed_scenarios": _string_list(
                    summary.get("missing_executed_scenarios")
                ),
                "execution_error_count": _int_value(summary.get("execution_error_count")),
            }
        )
    return blockers


def _staging_dataset_policy_summary(
    manifest: Mapping[str, Any],
    report: FileProcessingStagingReport,
) -> dict[str, object]:
    """real-world staging policy に本実行の case coverage evidence を足す。"""
    summary = dict(staging_dataset_policy_summary(manifest))
    if not bool(summary.get("configured")):
        return summary

    required_for_promotion = bool(summary.get("required_for_promotion"))
    executed_case_ids = {
        case_result.case_id for case_result in report.case_results if case_result.gate_results
    }
    required_fixture_prefix = _string_value(summary.get("required_fixture_prefix"))
    executed_real_world_case_count = 0
    executed_compliant_real_world_case_count = 0
    executed_source_kinds: set[str] = set()
    executed_scenarios: set[str] = set()
    raw_cases = manifest.get("cases")
    cases = raw_cases if isinstance(raw_cases, list) else []
    for raw_case in cases:
        case = _mapping(raw_case)
        if not _is_real_world_staging_case(case):
            continue
        case_id = _manifest_case_id(case)
        if case_id not in executed_case_ids:
            continue
        executed_real_world_case_count += 1
        if not _is_compliant_real_world_staging_case(
            case,
            required_fixture_prefix=required_fixture_prefix,
        ):
            continue
        executed_compliant_real_world_case_count += 1
        source_kind = normalize_source_kind(case.get("modality"))
        if source_kind:
            executed_source_kinds.add(source_kind)
        scenario = _string_value(case.get("scenario"))
        if scenario:
            executed_scenarios.add(scenario)

    required_source_kinds = set(_string_list(summary.get("required_source_kinds")))
    required_scenarios = set(_string_list(summary.get("required_scenarios")))
    missing_executed_source_kinds = required_source_kinds - executed_source_kinds
    missing_executed_scenarios = required_scenarios - executed_scenarios
    execution_error_codes: list[str] = []
    if required_for_promotion:
        min_real_world_cases = _int_value(summary.get("min_real_world_cases"))
        if executed_compliant_real_world_case_count < min_real_world_cases:
            execution_error_codes.append("real_world_executed_cases_insufficient")
        if missing_executed_source_kinds:
            execution_error_codes.append("real_world_executed_source_kinds_missing")
        if missing_executed_scenarios:
            execution_error_codes.append("real_world_executed_scenarios_missing")

    execution_error_count = len(execution_error_codes)
    summary.update(
        {
            "executed_real_world_case_count": executed_real_world_case_count,
            "executed_compliant_real_world_case_count": (executed_compliant_real_world_case_count),
            "executed_source_kinds": sorted(executed_source_kinds),
            "missing_executed_source_kinds": sorted(missing_executed_source_kinds),
            "executed_scenarios": sorted(executed_scenarios),
            "missing_executed_scenarios": sorted(missing_executed_scenarios),
            "execution_error_count": execution_error_count,
            "execution_error_codes": execution_error_codes,
            "promotion_ready": bool(summary.get("promotion_ready")) and execution_error_count == 0,
        }
    )
    return summary


def _is_real_world_staging_case(case: Mapping[str, object]) -> bool:
    return _string_value(case.get("fixture_kind")) == "real_world" or case.get("real_world") is True


def _is_compliant_real_world_staging_case(
    case: Mapping[str, object],
    *,
    required_fixture_prefix: str,
) -> bool:
    fixture = _string_value(case.get("fixture"))
    return (
        _string_value(case.get("data_sensitivity")) == "non_sensitive"
        and case.get("reviewed_for_public_ci") is True
        and (not required_fixture_prefix or fixture.startswith(required_fixture_prefix))
    )


def _promotion_policy_blockers(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """promotion policy 自体を緩めて gate を迂回する変更を止める。"""
    policy = _staging_policy(manifest)
    blockers: list[dict[str, Any]] = []
    if not bool(policy["required_for_promotion"]):
        blockers.append({"code": "promotion_policy_not_required"})
    if not bool(policy["pending_checks_block_promotion"]):
        blockers.append({"code": "promotion_policy_pending_checks_not_blocking"})
    required_runtime_checks = set(_string_list(policy.get("required_runtime_checks")))
    missing_runtime_checks = sorted(
        set(PROMOTION_POLICY_REQUIRED_RUNTIME_CHECKS) - required_runtime_checks
    )
    if missing_runtime_checks:
        blockers.append(
            {
                "code": "promotion_policy_required_runtime_check_missing",
                "checks": missing_runtime_checks,
            }
        )
    return blockers


def _parser_scorecard_promotion_blockers(
    scorecard: ParserAdapterScorecard,
) -> list[dict[str, Any]]:
    """scorecard が明示 parser backend の不適合を示す場合は昇格を止める。"""
    if scorecard.metrics_applied_to is None:
        return []
    selected = scorecard.selected_backend
    if selected not in {"docling", "unstructured"}:
        return []
    if scorecard.recommended_backend == selected:
        return []
    return [
        {
            "code": "parser_adapter_scorecard_mismatch",
            "selected_backend": selected,
            "recommended_backend": scorecard.recommended_backend,
            "metrics_source": scorecard.metrics_source,
        }
    ]


def _chunk_scorecard_promotion_blockers(
    scorecard: ChunkTemplateScorecard,
) -> list[dict[str, Any]]:
    """chunk template scorecard の promotion blocker を返す。"""
    return [
        {
            "code": "chunk_template_scorecard_blocked",
            "template": entry.template,
            "score": entry.score,
            "metrics_source": scorecard.metrics_source,
        }
        for entry in scorecard.entries
        if entry.promotion_blocking
    ]


def _parser_adapter_contract_promotion_blockers(
    matrix: ParserAdapterCompatibilityMatrix,
) -> list[dict[str, Any]]:
    """active adapter の real remap smoke が失敗した場合は昇格を止める。"""
    if matrix.passed:
        return []
    failed_cases = [
        case for case in matrix.cases if case.blocking and case.status in BLOCKING_FAILURE_STATUSES
    ]
    if not failed_cases:
        return []
    return [
        {
            "code": "parser_adapter_contract_failed",
            "count": len(failed_cases),
            "backends": sorted({case.backend for case in failed_cases}),
            "source_kinds": sorted({case.source_kind for case in failed_cases}),
        }
    ]


def _adapter_golden_gate(
    report: FileProcessingStagingReport,
    *,
    manifest: Mapping[str, Any],
    parser_scorecard: ParserAdapterScorecard,
    parser_adapter_contract: ParserAdapterCompatibilityMatrix,
    backend_source_kind_matrix: Mapping[str, object],
    parser_source_routes: Sequence[ParserAdapterSourceRoute],
    parser_adapter_contract_strict: bool,
) -> dict[str, Any]:
    """同一 golden/staging set で adapter の実力を測る非機密 gate summary。"""
    manifest_source_kinds = set(_source_kinds_from_manifest(manifest))
    required_source_kinds = set(ADAPTER_GOLDEN_GATE_SOURCE_KINDS)
    covered_source_kinds = {
        source_kind
        for source_kind in _string_list(backend_source_kind_matrix.get("covered_source_kinds"))
        if source_kind in required_source_kinds
    }
    contract_summary = parser_adapter_contract_summary(parser_adapter_contract)
    metrics = dict(report.metrics)
    metrics["adapter_contract_coverage"] = 1.0 if parser_adapter_contract.passed else 0.0
    metric_values = {
        metric: float(metrics[metric])
        for metric in ADAPTER_GOLDEN_GATE_METRICS
        if isinstance(metrics.get(metric), int | float)
        and not isinstance(metrics.get(metric), bool)
    }
    missing_metric_names = sorted(set(ADAPTER_GOLDEN_GATE_METRICS) - set(metric_values))
    failed_metric_checks = _adapter_golden_gate_failed_metrics(metric_values)
    blocker_codes: list[str] = []
    missing_manifest_source_kinds = sorted(required_source_kinds - manifest_source_kinds)
    missing_covered_source_kinds = sorted(required_source_kinds - covered_source_kinds)
    if missing_manifest_source_kinds:
        blocker_codes.append("adapter_golden_gate_manifest_source_kind_missing")
    if missing_covered_source_kinds:
        blocker_codes.append("adapter_golden_gate_source_kind_not_measured")
    if missing_metric_names:
        blocker_codes.append("adapter_golden_gate_metric_missing")
    if failed_metric_checks:
        blocker_codes.append("adapter_golden_gate_metric_failed")
    if not parser_adapter_contract.passed:
        blocker_codes.append("adapter_golden_gate_contract_failed")
    route_contract_gap_source_kinds = _source_route_contract_gap_source_kinds(parser_source_routes)
    if route_contract_gap_source_kinds:
        blocker_codes.append("adapter_golden_gate_source_route_contract_missing")
    if (
        parser_scorecard.selected_backend in {"docling", "unstructured"}
        and parser_scorecard.metrics_applied_to != parser_scorecard.selected_backend
    ):
        blocker_codes.append("adapter_golden_gate_selected_adapter_not_measured")
    return {
        "passed": not blocker_codes,
        "mode": "strict" if parser_adapter_contract_strict else "runtime",
        "metrics_source": "file_processing_staging",
        "selected_backend": parser_scorecard.selected_backend,
        "recommended_backend": parser_scorecard.recommended_backend,
        "metrics_applied_to": parser_scorecard.metrics_applied_to,
        "required_source_kinds": sorted(required_source_kinds),
        "manifest_source_kinds": sorted(manifest_source_kinds),
        "covered_source_kinds": sorted(covered_source_kinds),
        "missing_manifest_source_kinds": missing_manifest_source_kinds,
        "missing_source_kinds": missing_covered_source_kinds,
        "metric_values": dict(sorted(metric_values.items())),
        "missing_metric_names": missing_metric_names,
        "failed_metric_checks": failed_metric_checks,
        "contract_passed": parser_adapter_contract.passed,
        "contract_case_count": parser_adapter_contract.case_count,
        "contract_blocking_failure_count": parser_adapter_contract.blocking_failure_count,
        "contract_missing_source_kinds": contract_summary.get("missing_source_kinds", []),
        "contract_passed_case_refs": contract_summary.get("passed_case_refs", []),
        "contract_backend_passed_case_refs": contract_summary.get(
            "backend_passed_case_refs",
            {},
        ),
        "contract_blocking_failure_case_refs": contract_summary.get(
            "blocking_failure_case_refs",
            [],
        ),
        "source_route_contract_gap_source_kinds": route_contract_gap_source_kinds,
        "blocker_codes": blocker_codes,
    }


def _source_route_contract_gap_source_kinds(
    routes: Sequence[ParserAdapterSourceRoute],
) -> list[str]:
    """source route が contract gap で fallback した source kind を返す。"""
    return sorted(
        {
            route.source_kind
            for route in routes
            if any(
                warning.endswith("_adapter_contract_unverified_for_source")
                for warning in route.warning_codes
            )
        }
    )


def _adapter_golden_gate_failed_metrics(
    metric_values: Mapping[str, float],
) -> list[dict[str, object]]:
    failed: list[dict[str, object]] = []
    for metric in ADAPTER_GOLDEN_GATE_METRICS:
        if metric not in metric_values:
            continue
        direction, required = PROMOTION_THRESHOLD_GUARDS[metric]
        actual = metric_values[metric]
        passed = actual >= required if direction == "min" else actual <= required
        if passed:
            continue
        failed.append(
            {
                "metric": metric,
                "direction": direction,
                "required": required,
                "actual": actual,
            }
        )
    return failed


def _adapter_golden_gate_promotion_blockers(
    adapter_golden_gate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """adapter golden gate の失敗を promotion blocker へ畳み込む。"""
    blocker_codes = _string_list(adapter_golden_gate.get("blocker_codes"))
    if not blocker_codes:
        return []
    return [
        {
            "code": "adapter_golden_gate_failed",
            "blocker_codes": blocker_codes,
            "selected_backend": adapter_golden_gate.get("selected_backend"),
            "missing_source_kinds": _string_list(adapter_golden_gate.get("missing_source_kinds")),
            "source_route_contract_gap_source_kinds": _string_list(
                adapter_golden_gate.get("source_route_contract_gap_source_kinds")
            ),
            "missing_metric_names": _string_list(adapter_golden_gate.get("missing_metric_names")),
        }
    ]


def _object_storage_artifact_chain(
    report: FileProcessingStagingReport,
) -> dict[str, Any]:
    """保存先の artifact の復旧/再利用 chain を非機密 gate summary にする。"""
    runtime_checks = {check.check: check for check in report.runtime_checks}
    runtime_status = {check_name: check.status for check_name, check in runtime_checks.items()}
    roundtrip_check = runtime_checks.get("extraction_artifact_cache_roundtrip")
    roundtrip_evidence = _mapping(roundtrip_check.evidence if roundtrip_check else {})
    evidence = _mapping(report.metric_evidence.get("segment_artifact_reuse"))
    blocker_codes: list[str] = []
    roundtrip_status = runtime_status.get("extraction_artifact_cache_roundtrip")
    roundtrip_object_uri_scheme = _string_value(roundtrip_evidence.get("object_uri_scheme"))
    if roundtrip_status != "ok":
        blocker_codes.append("object_storage_artifact_roundtrip_not_ok")
    if roundtrip_status == "ok" and roundtrip_object_uri_scheme != "local":
        blocker_codes.append("object_storage_artifact_roundtrip_not_local")
    full_artifact_cached_case_count = _int_value(evidence.get("full_artifact_cached_case_count"))
    full_artifact_local_case_count = _int_value(evidence.get("full_artifact_local_case_count"))
    full_artifact_identity_present_case_count = _int_value(
        evidence.get("full_artifact_identity_present_case_count")
    )
    full_artifact_readable_case_count = _int_value(
        evidence.get("full_artifact_readable_case_count")
    )
    full_artifact_identity_verified_case_count = _int_value(
        evidence.get("full_artifact_identity_verified_case_count")
    )
    segment_artifact_expected_count = _int_value(evidence.get("segment_artifact_expected_count"))
    segment_artifact_local_uri_count = _int_value(evidence.get("segment_artifact_local_uri_count"))
    segment_artifact_non_local_uri_count = _int_value(
        evidence.get("segment_artifact_non_local_uri_count")
    )
    segment_artifact_readable_count = _int_value(evidence.get("segment_artifact_readable_count"))
    segment_artifact_identity_verified_count = _int_value(
        evidence.get("segment_artifact_identity_verified_count")
    )
    artifact_integrity_error_count = _int_value(evidence.get("artifact_integrity_error_count"))
    if full_artifact_cached_case_count <= 0:
        blocker_codes.append("object_storage_full_artifact_not_cached")
    if full_artifact_local_case_count < full_artifact_cached_case_count:
        blocker_codes.append("object_storage_full_artifact_not_local")
    if full_artifact_identity_present_case_count < full_artifact_cached_case_count:
        blocker_codes.append("object_storage_full_artifact_identity_missing")
    if full_artifact_readable_case_count < full_artifact_cached_case_count:
        blocker_codes.append("object_storage_full_artifact_unreadable")
    if full_artifact_identity_verified_case_count < full_artifact_cached_case_count:
        blocker_codes.append("object_storage_full_artifact_identity_unverified")
    if segment_artifact_readable_count < segment_artifact_expected_count:
        blocker_codes.append("object_storage_segment_artifact_unreadable")
    if (
        segment_artifact_local_uri_count < segment_artifact_expected_count
        or segment_artifact_non_local_uri_count > 0
    ):
        blocker_codes.append("object_storage_segment_artifact_not_local")
    if segment_artifact_identity_verified_count < segment_artifact_expected_count:
        blocker_codes.append("object_storage_segment_artifact_identity_unverified")
    if artifact_integrity_error_count > 0:
        blocker_codes.append("object_storage_artifact_integrity_error")
    if _int_value(evidence.get("segment_cache_miss_count")) > 0:
        blocker_codes.append("object_storage_segment_artifact_cache_miss")
    if _int_value(evidence.get("rewritten_successful_segment_artifact_count")) > 0:
        blocker_codes.append("object_storage_successful_segment_artifact_rewritten")
    sensitive_evidence_key_detected = _report_contains_sensitive_evidence_key(report)
    audit_payload_redaction_enforced = not sensitive_evidence_key_detected
    if not audit_payload_redaction_enforced:
        blocker_codes.append("object_storage_audit_payload_not_redacted")
    return {
        "passed": not blocker_codes,
        "roundtrip_check": roundtrip_status or "missing",
        "roundtrip_object_uri_scheme": roundtrip_object_uri_scheme or "missing",
        "full_artifact_cached_case_count": full_artifact_cached_case_count,
        "full_artifact_cached_case_refs": _string_list(
            evidence.get("full_artifact_cached_case_refs")
        ),
        "full_artifact_local_case_count": full_artifact_local_case_count,
        "full_artifact_identity_present_case_count": (full_artifact_identity_present_case_count),
        "full_artifact_readable_case_count": full_artifact_readable_case_count,
        "full_artifact_identity_verified_case_count": (full_artifact_identity_verified_case_count),
        "full_artifact_identity_verified_case_refs": _string_list(
            evidence.get("full_artifact_identity_verified_case_refs")
        ),
        "segment_artifact_expected_count": segment_artifact_expected_count,
        "segment_artifact_local_uri_count": segment_artifact_local_uri_count,
        "segment_artifact_non_local_uri_count": segment_artifact_non_local_uri_count,
        "segment_artifact_readable_count": segment_artifact_readable_count,
        "segment_artifact_identity_verified_count": segment_artifact_identity_verified_count,
        "artifact_integrity_error_count": artifact_integrity_error_count,
        "retry_case_count": _int_value(evidence.get("retry_case_count")),
        "retry_case_refs": _string_list(evidence.get("retry_case_refs")),
        "retained_successful_segment_artifact_count": _int_value(
            evidence.get("retained_successful_segment_artifact_count")
        ),
        "retained_successful_segment_artifact_case_refs": _string_list(
            evidence.get("retained_successful_segment_artifact_case_refs")
        ),
        "rewritten_successful_segment_artifact_count": _int_value(
            evidence.get("rewritten_successful_segment_artifact_count")
        ),
        "successful_segment_rewrite_case_refs": _string_list(
            evidence.get("successful_segment_rewrite_case_refs")
        ),
        "segment_cache_miss_count": _int_value(evidence.get("segment_cache_miss_count")),
        "segment_cache_miss_case_refs": _string_list(evidence.get("segment_cache_miss_case_refs")),
        "artifact_integrity_error_case_refs": _string_list(
            evidence.get("artifact_integrity_error_case_refs")
        ),
        "audit_payload_redaction_enforced": audit_payload_redaction_enforced,
        "sensitive_evidence_key_detected": sensitive_evidence_key_detected,
        "blocker_codes": blocker_codes,
    }


def _report_contains_sensitive_evidence_key(report: FileProcessingStagingReport) -> bool:
    """audit/staging evidence に OCR・検索・抽出本文系 key が混入したかを検出する。"""
    if _contains_sensitive_payload_key(report.metric_evidence):
        return True
    for runtime_check in report.runtime_checks:
        if _contains_sensitive_payload_key(runtime_check.evidence or {}):
            return True
    return any(
        _contains_sensitive_payload_key(gate_result.evidence or {})
        for case_result in report.case_results
        for gate_result in case_result.gate_results
    )


def _object_storage_artifact_chain_promotion_blockers(
    chain: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blocker_codes = _string_list(chain.get("blocker_codes"))
    if not blocker_codes:
        return []
    return [
        {
            "code": "object_storage_artifact_chain_failed",
            "blocker_codes": blocker_codes,
            "roundtrip_check": chain.get("roundtrip_check"),
        }
    ]


def _promotion_threshold_policy_blockers(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """promotion 用の中核 file-processing 閾値が緩められていないか確認する。"""
    if not bool(_staging_policy(manifest)["required_for_promotion"]):
        return []
    thresholds = _mapping(manifest.get("thresholds"))
    blockers: list[dict[str, Any]] = []
    for metric, (direction, required_value) in PROMOTION_THRESHOLD_GUARDS.items():
        actual_value = _threshold_value(thresholds.get(metric), direction)
        if actual_value is None:
            blockers.append(
                {
                    "code": "promotion_threshold_missing",
                    "metric": metric,
                    "direction": direction,
                    "required": required_value,
                }
            )
            continue
        too_loose = (
            actual_value < required_value if direction == "min" else actual_value > required_value
        )
        if too_loose:
            blockers.append(
                {
                    "code": "promotion_threshold_too_loose",
                    "metric": metric,
                    "direction": direction,
                    "required": required_value,
                    "actual": actual_value,
                }
            )
    return blockers


def _threshold_value(raw_threshold: object, direction: str) -> float | None:
    threshold = _mapping(raw_threshold)
    raw_value = threshold.get(direction)
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        return None
    return float(raw_value)


def _preflight_payload(
    preflight: SmokePreflightResult,
    settings: Settings,
    *,
    manifest: Mapping[str, Any] | None = None,
    manifest_path: Path | None = None,
    parser_adapter_contract_strict: bool = False,
    require_real_world_policy: bool = False,
) -> dict[str, Any]:
    settings = (
        strict_parser_adapter_settings(settings) if parser_adapter_contract_strict else settings
    )
    parser_adapters = parser_adapter_runtime_settings(settings)
    parser_adapter_preflight = _parser_adapter_preflight(parser_adapters)
    parser_scorecard = build_parser_adapter_scorecard(parser_adapters)
    parser_source_routes = build_parser_adapter_source_routes(
        parser_adapters,
        source_kinds=_source_kinds_from_manifest(manifest or {}),
    )
    parser_adapter_contract = (
        _run_preflight_parser_adapter_contract(
            settings,
            manifest=manifest or {},
            manifest_path=manifest_path,
        )
        if parser_adapter_contract_strict
        else None
    )
    staging_dataset_policy = staging_dataset_policy_summary(manifest or {})
    real_world_policy_preflight = _real_world_policy_preflight(
        staging_dataset_policy,
        require_real_world_policy=require_real_world_policy,
    )
    passed = (
        preflight.ok
        and bool(parser_adapter_preflight["ok"])
        and bool(real_world_policy_preflight["ok"])
        and (parser_adapter_contract is None or parser_adapter_contract.passed)
    )
    payload: dict[str, Any] = {
        "passed": passed,
        "preflight": asdict(preflight),
        "parser_adapters": asdict(parser_adapters),
        "parser_adapter_contract_mode": ("strict" if parser_adapter_contract_strict else "runtime"),
        "parser_adapter_preflight": parser_adapter_preflight,
        "real_world_policy_preflight": real_world_policy_preflight,
        "parser_adapter_scorecard": asdict(parser_scorecard),
        "parser_adapter_source_routes": [asdict(route) for route in parser_source_routes],
        "staging_dataset_policy": staging_dataset_policy,
        "case_count": 0,
        "gate_count": 0,
        "failure_count": _preflight_failure_count(
            smoke_preflight_ok=preflight.ok,
            parser_adapter_preflight=parser_adapter_preflight,
            parser_adapter_contract=parser_adapter_contract,
            real_world_policy_preflight=real_world_policy_preflight,
        ),
    }
    if parser_adapter_contract is not None:
        payload["parser_adapter_contract"] = parser_adapter_contract_artifact_payload(
            parser_adapter_contract
        )
        payload["adapter_contract_matrix_summary"] = _parser_adapter_contract_artifact_summary(
            parser_adapter_contract
        )
    return payload


def _parser_adapter_contract_artifact_summary(
    matrix: ParserAdapterCompatibilityMatrix,
) -> dict[str, object]:
    summary = parser_adapter_contract_artifact_payload(matrix).get("summary")
    return summary if isinstance(summary, dict) else {}


def _real_world_policy_preflight(
    summary: Mapping[str, object],
    *,
    require_real_world_policy: bool,
) -> dict[str, object]:
    """real-world staging policy の設定不足を実 client 作成前に止める。"""
    if not require_real_world_policy:
        return {"ok": True, "message": "real-world policy not required", "failures": []}
    if not bool(summary.get("configured")):
        return {
            "ok": False,
            "message": "staging_dataset_policy is required for promotion",
            "failures": [{"code": "staging_dataset_policy_missing"}],
        }
    if not bool(summary.get("required_for_promotion")):
        return {
            "ok": False,
            "message": "staging_dataset_policy must be required for promotion",
            "failures": [{"code": "staging_dataset_policy_not_required"}],
        }
    if not bool(summary.get("promotion_ready")):
        return {
            "ok": False,
            "message": "staging_dataset_policy is not promotion ready",
            "failures": [{"code": "staging_dataset_policy_failed"}],
        }
    return {"ok": True, "message": "real-world policy preflight ok", "failures": []}


def _run_preflight_parser_adapter_contract(
    settings: Settings,
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path | None,
) -> ParserAdapterCompatibilityMatrix:
    """strict preflight でも実 fixture を adapter schema-remap smoke に通す。"""
    fixture_specs = parser_adapter_fixture_specs_from_manifest(
        manifest,
        require_declared_schema_remap=True,
    )
    fixture_root = parser_adapter_fixture_root_from_manifest(
        manifest,
        manifest_path=manifest_path,
    )
    return run_parser_adapter_compatibility_matrix(
        settings,
        fixture_root=fixture_root,
        source_kinds=_source_kinds_from_manifest(manifest),
        fixture_specs=fixture_specs,
        require_backend_evidence=True,
    )


def _staging_policy(manifest: Mapping[str, Any]) -> dict[str, Any]:
    raw_policy = _mapping(manifest.get("staging_policy"))
    return {
        "required_for_promotion": bool(raw_policy.get("required_for_promotion", False)),
        "pending_checks_block_promotion": bool(
            raw_policy.get("pending_checks_block_promotion", False)
        ),
        "required_runtime_checks": _required_runtime_checks(manifest),
    }


def _required_runtime_checks(manifest: Mapping[str, Any]) -> list[str]:
    raw_policy = _mapping(manifest.get("staging_policy"))
    raw_checks = raw_policy.get("required_runtime_checks")
    if not isinstance(raw_checks, list):
        return []
    return [check for check in raw_checks if isinstance(check, str)]


def _observed_chunk_templates(manifest: Mapping[str, Any]) -> list[str]:
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list):
        return []
    templates: list[str] = []
    for raw_case in raw_cases:
        case = _mapping(raw_case)
        template = case.get("expected_chunk_template")
        if isinstance(template, str) and template and not template.startswith("unsupported_"):
            templates.append(template)
    return templates


def _chunk_template_manifest_evidence(
    manifest: Mapping[str, Any],
    report: FileProcessingStagingReport,
) -> dict[str, dict[str, object]]:
    """manifest template が staging case で実測された source/scenario 証跡を返す。"""
    case_results = {case.case_id: case for case in report.case_results}
    staged_case_ids = set(case_results)
    evidence: dict[str, dict[str, object]] = {}
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list):
        return evidence
    for raw_case in raw_cases:
        case = _mapping(raw_case)
        case_id = _manifest_case_id(case)
        if case_id not in staged_case_ids:
            continue
        case_result = case_results.get(case_id)
        if case_result is None or case_result.status != "INDEXED":
            continue
        template = _string_value(case.get("expected_chunk_template"))
        if not template or template.startswith("unsupported_"):
            continue
        entry = evidence.setdefault(
            template,
            {
                "expected_case_count": 0,
                "measured_case_count": 0,
                "expected_source_kinds": set(),
                "covered_source_kinds": set(),
                "expected_scenarios": set(),
                "covered_scenarios": set(),
                "observed_chunk_templates": set(),
            },
        )
        source_kind = normalize_source_kind(case.get("modality"))
        scenario = _string_value(case.get("scenario"))
        entry["expected_case_count"] = _int_value(entry.get("expected_case_count")) + 1
        _evidence_set(entry, "expected_source_kinds").add(source_kind)
        if scenario:
            _evidence_set(entry, "expected_scenarios").add(scenario)
        if not case_result.gate_results:
            continue
        actual_templates = _case_observed_chunk_templates(case_result)
        _evidence_set(entry, "observed_chunk_templates").update(actual_templates)
        if template not in actual_templates:
            continue
        entry["measured_case_count"] = _int_value(entry.get("measured_case_count")) + 1
        _evidence_set(entry, "covered_source_kinds").add(source_kind)
        if scenario:
            _evidence_set(entry, "covered_scenarios").add(scenario)
    return {
        template: {
            "expected_case_count": _int_value(values.get("expected_case_count")),
            "measured_case_count": _int_value(values.get("measured_case_count")),
            "expected_source_kinds": sorted(_as_set(values["expected_source_kinds"])),
            "covered_source_kinds": sorted(_as_set(values["covered_source_kinds"])),
            "expected_scenarios": sorted(_as_set(values["expected_scenarios"])),
            "covered_scenarios": sorted(_as_set(values["covered_scenarios"])),
            "observed_chunk_templates": sorted(_as_set(values["observed_chunk_templates"])),
        }
        for template, values in sorted(evidence.items())
    }


def _case_observed_chunk_templates(
    case_result: FileProcessingStagingCaseResult,
) -> set[str]:
    templates: set[str] = set()
    for gate_result in case_result.gate_results:
        evidence = _mapping(gate_result.evidence)
        templates.update(_string_list(evidence.get("chunk_templates")))
    return templates


def _manifest_case_id(case: Mapping[str, object]) -> str:
    return _string_value(case.get("id")) or _string_value(case.get("fixture")) or "case"


def _as_set(value: object) -> set[str]:
    if isinstance(value, set):
        return {item for item in value if isinstance(item, str)}
    if isinstance(value, list | tuple):
        return {item for item in value if isinstance(item, str)}
    return set()


def _evidence_set(entry: dict[str, object], key: str) -> set[str]:
    value = entry.get(key)
    if isinstance(value, set):
        return value
    values: set[str] = set()
    entry[key] = values
    return values


def _source_kinds_from_manifest(manifest: Mapping[str, Any]) -> list[str]:
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list):
        return []
    source_kinds: list[str] = []
    for raw_case in raw_cases:
        case = _mapping(raw_case)
        modality = case.get("modality")
        source_kinds.append(normalize_source_kind(modality))
    return list(dict.fromkeys(source_kinds))


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parser_adapter_preflight(runtime: ParserAdapterRuntimeSettings) -> dict[str, Any]:
    failures = [
        {
            "backend": adapter.backend,
            "status": adapter.status,
            "warning_code": adapter.warning_code,
        }
        for adapter in runtime.adapters
        if adapter.selected and adapter.status in {"disabled", "missing"}
    ]
    failure_statuses = {failure["status"] for failure in failures}
    return {
        "ok": not failures,
        "message": (
            "parser adapter preflight ok"
            if not failures
            else (
                "selected parser adapter feature flag is disabled"
                if failure_statuses == {"disabled"}
                else "selected parser adapter is not ready"
            )
        ),
        "failures": failures,
    }


def _preflight_failure_count(
    *,
    smoke_preflight_ok: bool,
    parser_adapter_preflight: dict[str, Any],
    parser_adapter_contract: ParserAdapterCompatibilityMatrix | None = None,
    real_world_policy_preflight: Mapping[str, object] | None = None,
) -> int:
    count = 0 if smoke_preflight_ok else 1
    failures = parser_adapter_preflight.get("failures")
    if isinstance(failures, list):
        count += len(failures)
    if real_world_policy_preflight is not None and not bool(real_world_policy_preflight.get("ok")):
        policy_failures = real_world_policy_preflight.get("failures")
        count += len(policy_failures) if isinstance(policy_failures, list) else 1
    if parser_adapter_contract is not None and not parser_adapter_contract.passed:
        count += max(1, parser_adapter_contract.blocking_failure_count)
    return count


STAGING_TREND_METRICS = (
    "gate_pass_rate",
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
)


def _write_trend_payload(payload: Mapping[str, Any], output: Path | None, *, kind: str) -> None:
    if output is None:
        return
    _write_payload(_trend_payload(payload, kind=kind), output)


def _trend_payload(payload: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """staging report から case detail を除いた非機密 trend snapshot を作る。"""
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    threshold_results = _mapping_list(payload.get("threshold_results"))
    promotion_blockers = _mapping_list(payload.get("promotion_blockers"))
    return {
        "kind": kind,
        "result_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        "passed": bool(payload.get("passed")),
        "promotion_ready": bool(payload.get("promotion_ready")),
        "case_count": _int_value(payload.get("case_count")),
        "gate_count": _int_value(payload.get("gate_count")),
        "failure_count": _int_value(payload.get("failure_count")),
        "promotion_blocker_count": len(promotion_blockers),
        "promotion_blocker_code_counts": _code_counts(promotion_blockers),
        "runtime_check_status_counts": _runtime_check_status_counts(
            _mapping_list(payload.get("runtime_checks"))
        ),
        "staging_dataset_policy": _mapping(payload.get("staging_dataset_policy")),
        "threshold_status_counts": _threshold_status_counts(threshold_results),
        "threshold_failures": _threshold_trend_items(threshold_results, status="failed"),
        "metrics": _trend_metrics(_mapping(payload.get("metrics"))),
        "parser_adapter_contract_mode": payload.get("parser_adapter_contract_mode"),
        "parser_adapter_contract": _parser_adapter_contract_trend(payload),
        "adapter_golden_gate": _adapter_golden_gate_trend(payload),
        "parser_adapter_source_routes": _parser_adapter_source_routes_trend(payload),
        "object_storage_artifact_chain": _object_storage_artifact_chain_trend(payload),
        "parser_adapter_scorecard": _parser_adapter_scorecard_trend(payload),
        "chunk_template_scorecard": _chunk_template_scorecard_trend(payload),
        "backend_source_kind_matrix": _backend_source_kind_matrix_trend(payload),
        "segment_artifact_reuse": _segment_artifact_reuse_trend(payload),
        "table_cell_lineage": _table_cell_lineage_trend(payload),
        "preview_addressability": _preview_addressability_trend(payload),
    }


def _trend_metrics(metrics: Mapping[str, object]) -> dict[str, float]:
    return {
        metric: float(value)
        for metric in STAGING_TREND_METRICS
        if isinstance((value := metrics.get(metric)), int | float) and not isinstance(value, bool)
    }


def _parser_adapter_contract_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    summary = _mapping(payload.get("adapter_contract_matrix_summary"))
    contract = _mapping(payload.get("parser_adapter_contract"))
    return {
        key: value
        for key, value in {
            "passed": summary.get("passed"),
            "case_count": summary.get("case_count"),
            "blocking_failure_count": summary.get("blocking_failure_count"),
            "missing_source_kinds": summary.get("missing_source_kinds"),
            "passed_case_refs": summary.get("passed_case_refs"),
            "backend_passed_case_refs": summary.get("backend_passed_case_refs"),
            "blocking_failure_case_refs": summary.get("blocking_failure_case_refs"),
            "backend_passed_source_kinds": summary.get("backend_passed_source_kinds"),
            "backend_passed_scenarios": summary.get("backend_passed_scenarios"),
            "backend_source_status_counts": summary.get("backend_source_status_counts"),
            "scenarios": summary.get("scenarios"),
            "passed_scenarios": summary.get("passed_scenarios"),
            "missing_scenarios": summary.get("missing_scenarios"),
            "blocking_failure_scenarios": summary.get("blocking_failure_scenarios"),
            "blocking_failure_source_kinds": summary.get("blocking_failure_source_kinds"),
            "blocking_failure_backends": summary.get("blocking_failure_backends"),
            "reason_code_counts": summary.get("reason_code_counts"),
            "warning_code_counts": summary.get("warning_code_counts"),
            "blocking_failure_reason_counts": summary.get("blocking_failure_reason_counts"),
            "adapter_package_version_pairs": _adapter_package_version_pairs(contract.get("cases")),
        }.items()
        if value is not None and value != []
    }


def _adapter_package_version_pairs(value: object) -> list[str]:
    """adapter contract cases から非機密 package/version 証跡を trend 用に抽出する。"""
    pairs: set[str] = set()
    for case in _mapping_list(value):
        backend = _string_value(case.get("backend"))
        package_name = _string_value(case.get("adapter_distribution_name")) or _string_value(
            case.get("adapter_import_name")
        )
        version = _string_value(case.get("adapter_package_version"))
        if not backend or not package_name or not version:
            continue
        pairs.add(f"{backend}|{package_name}|{version}")
    return sorted(pairs)


def _adapter_golden_gate_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    gate = _mapping(payload.get("adapter_golden_gate"))
    return {
        key: value
        for key, value in {
            "passed": gate.get("passed"),
            "mode": gate.get("mode"),
            "metrics_source": gate.get("metrics_source"),
            "selected_backend": gate.get("selected_backend"),
            "recommended_backend": gate.get("recommended_backend"),
            "metrics_applied_to": gate.get("metrics_applied_to"),
            "required_source_kinds": gate.get("required_source_kinds"),
            "manifest_source_kinds": gate.get("manifest_source_kinds"),
            "covered_source_kinds": gate.get("covered_source_kinds"),
            "missing_manifest_source_kinds": gate.get("missing_manifest_source_kinds"),
            "missing_source_kinds": gate.get("missing_source_kinds"),
            "missing_metric_names": gate.get("missing_metric_names"),
            "failed_metric_count": len(_mapping_list(gate.get("failed_metric_checks"))),
            "contract_passed": gate.get("contract_passed"),
            "contract_case_count": gate.get("contract_case_count"),
            "contract_blocking_failure_count": gate.get("contract_blocking_failure_count"),
            "contract_missing_source_kinds": gate.get("contract_missing_source_kinds"),
            "contract_passed_case_refs": _string_list(gate.get("contract_passed_case_refs")),
            "contract_backend_passed_case_refs": gate.get("contract_backend_passed_case_refs"),
            "contract_blocking_failure_case_refs": _string_list(
                gate.get("contract_blocking_failure_case_refs")
            ),
            "source_route_contract_gap_source_kinds": gate.get(
                "source_route_contract_gap_source_kinds"
            ),
            "blocker_codes": gate.get("blocker_codes"),
        }.items()
        if value is not None
    }


def _parser_adapter_source_routes_trend(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    for route in _mapping_list(payload.get("parser_adapter_source_routes")):
        source_kind = _string_value(route.get("source_kind"))
        if not source_kind:
            continue
        safe_route = {
            "source_kind": source_kind,
            "candidate_order": _string_list(route.get("candidate_order")),
            "attempted_order": _string_list(route.get("attempted_order")),
            "active_order": _string_list(route.get("active_order")),
            "selected_backend": _string_value(route.get("selected_backend")),
            "reason_codes": _string_list(route.get("reason_codes")),
            "warning_codes": _string_list(route.get("warning_codes")),
        }
        routes.append({key: value for key, value in safe_route.items() if value is not None})
    return routes


def _object_storage_artifact_chain_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    chain = _mapping(payload.get("object_storage_artifact_chain"))
    return {
        key: value
        for key, value in {
            "passed": chain.get("passed"),
            "roundtrip_check": chain.get("roundtrip_check"),
            "roundtrip_object_uri_scheme": chain.get("roundtrip_object_uri_scheme"),
            "full_artifact_cached_case_count": chain.get("full_artifact_cached_case_count"),
            "full_artifact_cached_case_refs": _string_list(
                chain.get("full_artifact_cached_case_refs")
            ),
            "full_artifact_local_case_count": chain.get("full_artifact_local_case_count"),
            "full_artifact_identity_present_case_count": chain.get(
                "full_artifact_identity_present_case_count"
            ),
            "full_artifact_readable_case_count": chain.get("full_artifact_readable_case_count"),
            "full_artifact_identity_verified_case_count": chain.get(
                "full_artifact_identity_verified_case_count"
            ),
            "full_artifact_identity_verified_case_refs": _string_list(
                chain.get("full_artifact_identity_verified_case_refs")
            ),
            "segment_artifact_expected_count": chain.get("segment_artifact_expected_count"),
            "segment_artifact_local_uri_count": chain.get("segment_artifact_local_uri_count"),
            "segment_artifact_non_local_uri_count": chain.get(
                "segment_artifact_non_local_uri_count"
            ),
            "segment_artifact_readable_count": chain.get("segment_artifact_readable_count"),
            "segment_artifact_identity_verified_count": chain.get(
                "segment_artifact_identity_verified_count"
            ),
            "artifact_integrity_error_count": chain.get("artifact_integrity_error_count"),
            "retry_case_count": chain.get("retry_case_count"),
            "retry_case_refs": _string_list(chain.get("retry_case_refs")),
            "retained_successful_segment_artifact_count": chain.get(
                "retained_successful_segment_artifact_count"
            ),
            "retained_successful_segment_artifact_case_refs": _string_list(
                chain.get("retained_successful_segment_artifact_case_refs")
            ),
            "rewritten_successful_segment_artifact_count": chain.get(
                "rewritten_successful_segment_artifact_count"
            ),
            "successful_segment_rewrite_case_refs": _string_list(
                chain.get("successful_segment_rewrite_case_refs")
            ),
            "segment_cache_miss_count": chain.get("segment_cache_miss_count"),
            "segment_cache_miss_case_refs": _string_list(chain.get("segment_cache_miss_case_refs")),
            "artifact_integrity_error_case_refs": _string_list(
                chain.get("artifact_integrity_error_case_refs")
            ),
            "audit_payload_redaction_enforced": chain.get("audit_payload_redaction_enforced"),
            "blocker_codes": chain.get("blocker_codes"),
        }.items()
        if value is not None
    }


def _parser_adapter_scorecard_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    scorecard = _mapping(payload.get("parser_adapter_scorecard"))
    return {
        key: value
        for key, value in {
            "selected_backend": scorecard.get("selected_backend"),
            "recommended_backend": scorecard.get("recommended_backend"),
            "metrics_source": scorecard.get("metrics_source"),
            "metrics_applied_to": scorecard.get("metrics_applied_to"),
            "entries": _parser_adapter_scorecard_entry_trends(scorecard.get("entries")),
        }.items()
        if value is not None and value != []
    }


def _parser_adapter_scorecard_entry_trends(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    entries: list[dict[str, object]] = []
    for item in value:
        entry = _mapping(item)
        backend = _string_value(entry.get("backend"))
        if not backend:
            continue
        safe_entry = {
            "backend": backend,
            "rank": entry.get("rank") if isinstance(entry.get("rank"), int) else None,
            "score": entry.get("score") if isinstance(entry.get("score"), int | float) else None,
            "status": _string_value(entry.get("status")),
            "recommended": entry.get("recommended"),
            "executable": entry.get("executable"),
            "selected": entry.get("selected"),
            "enabled": entry.get("enabled"),
            "installed": entry.get("installed"),
            "metric_count": entry.get("metric_count"),
            "reason_codes": _string_list(entry.get("reason_codes")),
            "warning_codes": _string_list(entry.get("warning_codes")),
        }
        entries.append(
            {key: safe_value for key, safe_value in safe_entry.items() if safe_value is not None}
        )
    return entries


def _chunk_template_scorecard_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    scorecard = _mapping(payload.get("chunk_template_scorecard"))
    return {
        key: value
        for key, value in {
            "recommended_template": scorecard.get("recommended_template"),
            "metrics_source": scorecard.get("metrics_source"),
            "promotion_blocking": scorecard.get("promotion_blocking"),
            "observed_templates": _string_list(scorecard.get("observed_templates")),
            "entries": _chunk_template_scorecard_entry_trends(scorecard.get("entries")),
        }.items()
        if value is not None and value != []
    }


def _chunk_template_scorecard_entry_trends(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    entries: list[dict[str, object]] = []
    for item in value:
        entry = _mapping(item)
        template = _string_value(entry.get("template"))
        if not template:
            continue
        safe_entry = {
            "template": template,
            "status": _string_value(entry.get("status")),
            "score": entry.get("score") if isinstance(entry.get("score"), int | float) else None,
            "promotion_blocking": entry.get("promotion_blocking"),
            "metric_count": entry.get("metric_count"),
            "expected_case_count": entry.get("expected_case_count"),
            "measured_case_count": entry.get("measured_case_count"),
            "expected_source_kinds": _string_list(entry.get("expected_source_kinds")),
            "covered_source_kinds": _string_list(entry.get("covered_source_kinds")),
            "missing_source_kinds": _string_list(entry.get("missing_source_kinds")),
            "expected_scenarios": _string_list(entry.get("expected_scenarios")),
            "covered_scenarios": _string_list(entry.get("covered_scenarios")),
            "missing_scenarios": _string_list(entry.get("missing_scenarios")),
            "reason_codes": _string_list(entry.get("reason_codes")),
        }
        entries.append(
            {key: safe_value for key, safe_value in safe_entry.items() if safe_value is not None}
        )
    return entries


def _backend_source_kind_matrix_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    matrix = _mapping(payload.get("backend_source_kind_matrix"))
    return {
        key: value
        for key, value in {
            "value": matrix.get("value"),
            "required_source_kinds": matrix.get("required_source_kinds"),
            "covered_source_kinds": matrix.get("covered_source_kinds"),
            "missing_source_kinds": matrix.get("missing_source_kinds"),
            "backend_source_kinds": matrix.get("backend_source_kinds"),
        }.items()
        if value is not None
    }


def _segment_artifact_reuse_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    metric_evidence = _mapping(payload.get("metric_evidence"))
    evidence = _mapping(metric_evidence.get("segment_artifact_reuse"))
    return {key: value for key, value in evidence.items() if _trend_safe_evidence_value(value)}


def _table_cell_lineage_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    metric_evidence = _mapping(payload.get("metric_evidence"))
    evidence = _mapping(metric_evidence.get("table_cell_lineage"))
    return {key: value for key, value in evidence.items() if _trend_safe_evidence_value(value)}


def _preview_addressability_trend(payload: Mapping[str, Any]) -> dict[str, object]:
    metric_evidence = _mapping(payload.get("metric_evidence"))
    evidence = _mapping(metric_evidence.get("preview_addressability"))
    return {key: value for key, value in evidence.items() if _trend_safe_evidence_value(value)}


def _trend_safe_evidence_value(value: object) -> bool:
    if isinstance(value, bool | int | float | str) or value is None:
        return True
    if isinstance(value, list):
        return all(isinstance(item, str) and item.startswith("case:") for item in value)
    return False


def _runtime_check_status_counts(runtime_checks: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for runtime_check in runtime_checks:
        status = runtime_check.get("status")
        if not isinstance(status, str):
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


def _threshold_status_counts(threshold_results: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for threshold in threshold_results:
        status = threshold.get("status")
        if not isinstance(status, str):
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


def _threshold_trend_items(
    threshold_results: Sequence[Mapping[str, object]],
    *,
    status: str,
) -> list[dict[str, object]]:
    return [
        {
            key: value
            for key, value in {
                "metric": threshold.get("metric"),
                "actual": threshold.get("actual"),
                "required": threshold.get("required"),
                "reason": threshold.get("reason"),
            }.items()
            if value is not None
        }
        for threshold in threshold_results
        if threshold.get("status") == status
    ]


def _code_counts(items: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        code = item.get("code")
        if not isinstance(code, str):
            continue
        counts[code] = counts.get(code, 0) + 1
    return counts


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        return []
    return sorted(item for item in value if isinstance(item, str))


def _int_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _write_payload(payload: dict[str, Any], output: Path | None) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        print(encoded)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
