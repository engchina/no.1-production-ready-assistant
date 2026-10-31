"""file-processing trend regression gate のテスト。"""

import json
from pathlib import Path

from pytest import CaptureFixture

from app.rag import file_processing_trend_cli


def test_file_processing_trend_cli_passes_without_regression(tmp_path: Path) -> None:
    """許容範囲内の揺れは regression にしない。"""
    baseline = _trend(
        metrics={
            "retrieval_recall": 0.95,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "parser_fallback_rate": 0.05,
            "ingestion_p95_ms": 100_000.0,
        },
        result_sha256="baseline",
    )
    current = _trend(
        metrics={
            "retrieval_recall": 0.94,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "parser_fallback_rate": 0.06,
            "ingestion_p95_ms": 110_000.0,
        },
        result_sha256="current",
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["regression_count"] == 0
    assert set(payload["metrics_compared"]) >= {
        "retrieval_recall",
        "table_qa_accuracy",
        "page_hit_accuracy",
        "parser_fallback_rate",
        "ingestion_p95_ms",
    }
    assert "raw_text" not in output_path.read_text(encoding="utf-8")
    assert "case_results" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_core_metric_regression(
    tmp_path: Path,
) -> None:
    """中核 metric の退化は regression として失敗させる。"""
    baseline = _trend(
        metrics={
            "table_qa_accuracy": 1.0,
            "parser_fallback_rate": 0.05,
        },
        result_sha256="baseline",
    )
    current = _trend(
        metrics={
            "table_qa_accuracy": 0.99,
            "parser_fallback_rate": 0.08,
        },
        result_sha256="current",
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert {regression["metric"] for regression in payload["regressions"]} == {
        "table_qa_accuracy",
        "parser_fallback_rate",
    }
    table_regression = next(
        regression
        for regression in payload["regressions"]
        if regression["metric"] == "table_qa_accuracy"
    )
    assert table_regression["allowed_delta"] == 0.0
    assert table_regression["reason"] == "metric_decreased"


def test_file_processing_trend_cli_zero_drop_for_dependency_chunk_metrics(
    tmp_path: Path,
) -> None:
    """dependency/adaptive chunk 指標は既定の許容低下幅でも落とせない。"""
    baseline = _trend(
        metrics={
            "dependency_context_recall": 1.0,
            "chunk_contextual_coherence": 1.0,
            "reading_order_consistency": 1.0,
            "retrieval_recall": 0.95,
        },
        result_sha256="baseline",
    )
    current = _trend(
        metrics={
            "dependency_context_recall": 0.99,
            "chunk_contextual_coherence": 0.99,
            "reading_order_consistency": 0.99,
            "retrieval_recall": 0.94,
        },
        result_sha256="current",
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    regressions = {regression["metric"]: regression for regression in payload["regressions"]}
    assert set(regressions) == {
        "chunk_contextual_coherence",
        "dependency_context_recall",
        "reading_order_consistency",
    }
    assert all(regression["allowed_delta"] == 0.0 for regression in regressions.values())
    assert "retrieval_recall" not in regressions
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_handles_metric_summary_shape(
    tmp_path: Path,
) -> None:
    """local / staging trend の metric summary dict 形式も比較できる。"""
    baseline = _trend(
        metrics={
            "table_qa_accuracy": {"status": "measured", "value": 1.0},
            "page_hit_accuracy": {"status": "requires_staging", "value": None},
            "retrieval_recall": {"status": "partial", "value": 0.9},
        },
        result_sha256="baseline",
    )
    current = _trend(
        metrics={
            "table_qa_accuracy": {"status": "measured", "value": 1.0},
            "page_hit_accuracy": {"status": "requires_staging", "value": None},
            "retrieval_recall": {"status": "partial", "value": 0.9},
        },
        result_sha256="current",
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["metrics_compared"] == ["retrieval_recall", "table_qa_accuracy"]
    assert "page_hit_accuracy" not in payload["metrics_compared"]


def test_file_processing_trend_cli_fails_when_comparable_metric_is_removed(
    tmp_path: Path,
) -> None:
    """baseline にある比較可能 metric を current から消した場合は退化として止める。"""
    baseline = _trend(
        metrics={
            "retrieval_recall": 1.0,
            "table_qa_accuracy": 1.0,
            "adapter_contract_coverage": 1.0,
        },
        result_sha256="baseline",
    )
    current = _trend(
        metrics={
            "retrieval_recall": 1.0,
        },
        result_sha256="current",
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["metrics_compared"] == ["retrieval_recall"]
    assert payload["metrics_missing"] == [
        "adapter_contract_coverage",
        "table_qa_accuracy",
    ]
    regressions = {
        regression["metric"]: regression["reason"] for regression in payload["regressions"]
    }
    assert regressions["adapter_contract_coverage"] == "metric_missing_from_current"
    assert regressions["table_qa_accuracy"] == "metric_missing_from_current"
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_requires_promotion_ready_when_requested(
    tmp_path: Path,
) -> None:
    """staging 昇格では promotion_ready の退化も明示的に止められる。"""
    baseline = _trend(metrics={"retrieval_recall": 1.0}, result_sha256="baseline")
    current = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="current",
        promotion_ready=False,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [
            str(current_path),
            "--baseline",
            str(baseline_path),
            "--output",
            str(output_path),
            "--require-promotion-ready",
        ]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["regressions"][-1]["metric"] == "promotion_ready"
    assert payload["regressions"][-1]["reason"] == "promotion_ready_regressed"


def test_file_processing_trend_cli_fails_on_trend_identity_regression(
    tmp_path: Path,
) -> None:
    """比較対象の kind / case / gate 面積が変わった場合は metric が同じでも止める。"""
    baseline = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="baseline",
        kind="file_processing_staging",
        case_count=10,
        gate_count=42,
    )
    current = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="current",
        kind="file_processing",
        case_count=8,
        gate_count=39,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "trend_kind_changed" in reasons
    assert "case_count_decreased" in reasons
    assert "gate_count_decreased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_real_world_policy_regression(
    tmp_path: Path,
) -> None:
    """staging real-world dataset 証跡の退化は metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="baseline",
        staging_dataset_policy={
            "configured": True,
            "promotion_ready": True,
            "real_world_case_count": 6,
            "executed_real_world_case_count": 6,
            "executed_compliant_real_world_case_count": 6,
            "executed_source_kinds": ["email", "html", "image", "office", "pdf"],
            "executed_scenarios": [
                "email_thread_attachment",
                "html_semantic_blocks",
                "image_ocr_receipt",
                "japanese_docx_layout",
                "scanned_pdf_ocr",
            ],
            "policy_error_count": 0,
            "execution_error_count": 0,
            "missing_source_kinds": [],
            "missing_scenarios": [],
            "missing_executed_source_kinds": [],
            "missing_executed_scenarios": [],
        },
    )
    current = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="current",
        staging_dataset_policy={
            "configured": True,
            "promotion_ready": False,
            "real_world_case_count": 5,
            "executed_real_world_case_count": 4,
            "executed_compliant_real_world_case_count": 3,
            "executed_source_kinds": ["email", "html", "pdf"],
            "executed_scenarios": [
                "email_thread_attachment",
                "html_semantic_blocks",
                "scanned_pdf_ocr",
            ],
            "policy_error_count": 2,
            "execution_error_count": 1,
            "missing_source_kinds": ["office"],
            "missing_scenarios": ["japanese_docx_layout"],
            "missing_executed_source_kinds": ["image", "office"],
            "missing_executed_scenarios": [
                "image_ocr_receipt",
                "japanese_docx_layout",
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "staging_dataset_policy_promotion_ready_regressed" in reasons
    assert "real_world_case_count_decreased" in reasons
    assert "executed_real_world_case_count_decreased" in reasons
    assert "executed_compliant_real_world_case_count_decreased" in reasons
    assert "staging_dataset_executed_source_kind_count_decreased" in reasons
    assert "staging_dataset_executed_scenario_count_decreased" in reasons
    assert "staging_dataset_policy_error_count_increased" in reasons
    assert "staging_dataset_execution_error_count_increased" in reasons
    assert "staging_dataset_missing_source_kind_count_increased" in reasons
    assert "staging_dataset_missing_scenario_count_increased" in reasons
    assert "staging_dataset_missing_executed_source_kind_count_increased" in reasons
    assert "staging_dataset_missing_executed_scenario_count_increased" in reasons
    assert "japanese_docx_layout" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_real_world_policy_set_replacement(
    tmp_path: Path,
) -> None:
    """real-world policy の実行/欠落集合は同数で入れ替わっても退化として止める。"""
    baseline = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="baseline",
        passed=False,
        staging_dataset_policy={
            "configured": True,
            "promotion_ready": False,
            "executed_source_kinds": ["pdf", "office"],
            "executed_scenarios": ["scanned_pdf_ocr", "japanese_docx_layout"],
            "missing_source_kinds": ["image"],
            "missing_scenarios": ["image_ocr_receipt"],
            "missing_executed_source_kinds": ["email"],
            "missing_executed_scenarios": ["email_thread_headers"],
        },
    )
    current = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="current",
        passed=False,
        staging_dataset_policy={
            "configured": True,
            "promotion_ready": False,
            "executed_source_kinds": ["pdf", "email"],
            "executed_scenarios": ["scanned_pdf_ocr", "email_thread_headers"],
            "missing_source_kinds": ["office"],
            "missing_scenarios": ["japanese_docx_layout"],
            "missing_executed_source_kinds": ["office"],
            "missing_executed_scenarios": ["japanese_docx_layout"],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert reasons == {
        "staging_dataset_executed_source_kinds_removed",
        "staging_dataset_executed_scenarios_removed",
        "staging_dataset_missing_source_kinds_added",
        "staging_dataset_missing_scenarios_added",
        "staging_dataset_missing_executed_source_kinds_added",
        "staging_dataset_missing_executed_scenarios_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "japanese_docx_layout" not in output_text
    assert "email_thread_headers" not in output_text


def test_file_processing_trend_cli_fails_on_parser_adapter_contract_regression(
    tmp_path: Path,
) -> None:
    """strict adapter remap 証跡の退化は core metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract={
            "passed": True,
            "case_count": 6,
            "blocking_failure_count": 0,
            "scenarios": [
                "html_semantic_blocks",
                "scanned_pdf_ocr",
                "two_column_pdf_reading_order",
            ],
            "passed_scenarios": [
                "html_semantic_blocks",
                "scanned_pdf_ocr",
                "two_column_pdf_reading_order",
            ],
            "missing_scenarios": [],
            "blocking_failure_scenarios": [],
            "backend_passed_source_kinds": {
                "docling": ["html", "office", "pdf"],
                "mineru": ["image", "pdf"],
                "unstructured": ["email", "html", "image", "office", "pdf"],
            },
            "adapter_package_version_pairs": [
                "docling|docling|2.103.0",
                "mineru|mineru_file_parse|3.4.0",
                "unstructured|unstructured|0.18.32",
            ],
            "missing_source_kinds": [],
            "blocking_failure_source_kinds": [],
            "blocking_failure_backends": [],
        },
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="runtime",
        parser_adapter_contract={
            "passed": False,
            "case_count": 6,
            "blocking_failure_count": 2,
            "scenarios": [
                "html_semantic_blocks",
                "scanned_pdf_ocr",
                "simple_pdf_text",
            ],
            "passed_scenarios": ["html_semantic_blocks", "scanned_pdf_ocr"],
            "missing_scenarios": ["simple_pdf_text"],
            "blocking_failure_scenarios": ["simple_pdf_text"],
            "backend_passed_source_kinds": {
                "docling": ["html", "office", "pdf"],
                "mineru": ["image"],
                "unstructured": ["email", "html", "image", "office", "pdf"],
            },
            "adapter_package_version_pairs": [
                "docling|docling|2.104.0",
                "unstructured|unstructured|0.18.32",
            ],
            "missing_source_kinds": ["office"],
            "blocking_failure_source_kinds": ["pdf"],
            "blocking_failure_backends": ["docling"],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "parser_adapter_contract_strict_mode_removed" in reasons
    assert "parser_adapter_contract_passed_regressed" in reasons
    assert "parser_adapter_contract_scenarios_removed" in reasons
    assert "parser_adapter_contract_passed_scenario_count_decreased" in reasons
    assert "parser_adapter_contract_backend_source_pairs_removed" in reasons
    assert "parser_adapter_contract_backend_source_pair_count_decreased" in reasons
    assert "parser_adapter_contract_package_version_pairs_removed" in reasons
    assert "parser_adapter_contract_package_version_pair_count_decreased" in reasons
    assert "parser_adapter_contract_missing_scenario_count_increased" in reasons
    assert "parser_adapter_contract_blocking_scenario_count_increased" in reasons
    assert "parser_adapter_contract_blocking_failure_count_increased" in reasons
    assert "parser_adapter_contract_missing_source_kind_count_increased" in reasons
    assert "parser_adapter_contract_blocking_source_kind_count_increased" in reasons
    assert "parser_adapter_contract_blocking_backend_count_increased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_parser_adapter_contract_bad_set_replacement(
    tmp_path: Path,
) -> None:
    """adapter contract の bad set は同数で入れ替わっても止める。"""
    baseline_contract = {
        "passed": False,
        "case_count": 6,
        "blocking_failure_count": 2,
        "source_kinds": ["pdf", "office", "image"],
        "backends": ["docling", "mineru", "unstructured"],
        "scenarios": ["image_ocr_receipt", "japanese_docx_layout"],
        "passed_scenarios": [],
        "passed_source_kinds": ["pdf"],
        "missing_source_kinds": ["image"],
        "missing_scenarios": ["image_ocr_receipt"],
        "blocking_failure_source_kinds": ["image"],
        "blocking_failure_scenarios": ["image_ocr_receipt"],
        "blocking_failure_backends": ["mineru"],
        "backend_passed_source_kinds": {"docling": ["pdf"]},
        "backend_passed_scenarios": {},
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 1}}},
        "warning_code_counts": {},
        "blocking_failure_reason_counts": {},
    }
    current_contract = {
        **baseline_contract,
        "missing_source_kinds": ["office"],
        "missing_scenarios": ["japanese_docx_layout"],
        "blocking_failure_source_kinds": ["office"],
        "blocking_failure_scenarios": ["japanese_docx_layout"],
        "blocking_failure_backends": ["docling"],
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        passed=False,
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        passed=False,
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "parser_adapter_contract_missing_source_kinds_added",
        "parser_adapter_contract_missing_scenarios_added",
        "parser_adapter_contract_blocking_source_kinds_added",
        "parser_adapter_contract_blocking_scenarios_added",
        "parser_adapter_contract_blocking_backends_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "japanese_docx_layout" not in output_text
    assert "docling" not in output_text


def test_file_processing_trend_cli_fails_on_parser_adapter_contract_evidence_set_removal(
    tmp_path: Path,
) -> None:
    """contract の source/scenario/backend evidence は同数置換でも維持を要求する。"""
    baseline_contract = {
        "passed": True,
        "case_count": 6,
        "blocking_failure_count": 0,
        "source_kinds": ["pdf", "office"],
        "backends": ["docling", "mineru"],
        "scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
        "passed_scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
        "passed_source_kinds": ["pdf", "office"],
        "missing_scenarios": [],
        "blocking_failure_scenarios": [],
        "missing_source_kinds": [],
        "blocking_failure_source_kinds": [],
        "blocking_failure_backends": [],
        "backend_passed_source_kinds": {"docling": ["pdf", "office"]},
        "backend_passed_scenarios": {
            "docling": ["scanned_pdf_ocr", "two_column_pdf_reading_order"]
        },
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 1}}},
        "warning_code_counts": {},
        "blocking_failure_reason_counts": {},
    }
    current_contract = {
        **baseline_contract,
        "source_kinds": ["pdf", "html"],
        "backends": ["docling", "unstructured"],
        "scenarios": ["scanned_pdf_ocr", "html_semantic_blocks"],
        "passed_scenarios": ["scanned_pdf_ocr", "html_semantic_blocks"],
        "passed_source_kinds": ["pdf", "html"],
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "parser_adapter_contract_scenarios_removed",
        "parser_adapter_contract_source_kinds_removed",
        "parser_adapter_contract_backends_removed",
        "parser_adapter_contract_passed_scenarios_removed",
        "parser_adapter_contract_passed_source_kinds_removed",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "two_column_pdf_reading_order" not in output_text
    assert "mineru" not in output_text


def test_file_processing_trend_cli_fails_on_parser_adapter_contract_case_ref_replacement(
    tmp_path: Path,
) -> None:
    """adapter contract は同数でも実 manifest case hash の入れ替えを止める。"""
    baseline_contract = {
        "passed": True,
        "case_count": 2,
        "blocking_failure_count": 0,
        "source_kinds": ["pdf"],
        "backends": ["docling"],
        "scenarios": ["scanned_pdf_ocr"],
        "passed_scenarios": ["scanned_pdf_ocr"],
        "passed_source_kinds": ["pdf"],
        "passed_case_refs": ["case:adapter-old"],
        "backend_passed_case_refs": {"docling": ["case:adapter-old"]},
        "blocking_failure_case_refs": [],
        "missing_scenarios": [],
        "blocking_failure_scenarios": [],
        "missing_source_kinds": [],
        "blocking_failure_source_kinds": [],
        "blocking_failure_backends": [],
        "backend_passed_source_kinds": {"docling": ["pdf"]},
        "backend_passed_scenarios": {"docling": ["scanned_pdf_ocr"]},
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 1}}},
        "warning_code_counts": {},
        "blocking_failure_reason_counts": {},
    }
    current_contract = {
        **baseline_contract,
        "passed_case_refs": ["case:adapter-new"],
        "backend_passed_case_refs": {"docling": ["case:adapter-new"]},
        "blocking_failure_case_refs": ["case:adapter-bad"],
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "parser_adapter_contract_passed_case_refs_removed",
        "parser_adapter_contract_backend_passed_case_refs_removed",
        "parser_adapter_contract_blocking_failure_case_refs_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "case:adapter-old" not in output_text
    assert "case:adapter-new" not in output_text
    assert "case:adapter-bad" not in output_text


def test_file_processing_trend_cli_fails_on_backend_scenario_pair_regression(
    tmp_path: Path,
) -> None:
    """全局 scenario が残っても backend/scenario 証跡の退化は失敗させる。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract={
            "passed": True,
            "case_count": 6,
            "blocking_failure_count": 0,
            "scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
            "passed_scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
            "missing_scenarios": [],
            "blocking_failure_scenarios": [],
            "backend_passed_source_kinds": {
                "docling": ["pdf"],
                "mineru": ["pdf"],
            },
            "backend_passed_scenarios": {
                "docling": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
                "mineru": ["scanned_pdf_ocr"],
            },
            "missing_source_kinds": [],
            "blocking_failure_source_kinds": [],
            "blocking_failure_backends": [],
        },
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract={
            "passed": True,
            "case_count": 6,
            "blocking_failure_count": 0,
            "scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
            "passed_scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
            "missing_scenarios": [],
            "blocking_failure_scenarios": [],
            "backend_passed_source_kinds": {
                "docling": ["pdf"],
                "mineru": ["pdf"],
            },
            "backend_passed_scenarios": {
                "docling": ["scanned_pdf_ocr"],
                "mineru": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
            },
            "missing_source_kinds": [],
            "blocking_failure_source_kinds": [],
            "blocking_failure_backends": [],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert exit_code == 1
    assert reasons == {"parser_adapter_contract_backend_scenario_pairs_removed"}
    assert "two_column_pdf_reading_order" not in output_path.read_text(encoding="utf-8")
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_backend_source_bad_status_count_regression(
    tmp_path: Path,
) -> None:
    """backend/source の bad status 増加は代表 status が同じでも失敗させる。"""
    baseline_contract = {
        "passed": True,
        "case_count": 6,
        "blocking_failure_count": 0,
        "scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
        "passed_scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
        "missing_scenarios": [],
        "blocking_failure_scenarios": [],
        "backend_passed_source_kinds": {"docling": ["pdf"]},
        "backend_passed_scenarios": {
            "docling": ["scanned_pdf_ocr", "two_column_pdf_reading_order"]
        },
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 2, "fallback": 0}}},
        "missing_source_kinds": [],
        "blocking_failure_source_kinds": [],
        "blocking_failure_backends": [],
    }
    current_contract = {
        **baseline_contract,
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 2, "fallback": 1}}},
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["regressions"] == [
        {
            "metric": ("parser_adapter_contract_backend_source_status_count:docling:pdf:fallback"),
            "direction": "max",
            "baseline": 0.0,
            "current": 1.0,
            "allowed_delta": 0.0,
            "delta": 1.0,
            "reason": ("parser_adapter_contract_backend_source_bad_status_count_increased"),
        }
    ]
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_backend_source_passed_count_decrease(
    tmp_path: Path,
) -> None:
    """backend/source の passed 件数低下は pair が残っていても失敗させる。"""
    baseline_contract = {
        "passed": True,
        "case_count": 6,
        "blocking_failure_count": 0,
        "scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
        "passed_scenarios": ["scanned_pdf_ocr", "two_column_pdf_reading_order"],
        "missing_scenarios": [],
        "blocking_failure_scenarios": [],
        "backend_passed_source_kinds": {"docling": ["pdf"]},
        "backend_passed_scenarios": {
            "docling": ["scanned_pdf_ocr", "two_column_pdf_reading_order"]
        },
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 3}}},
        "missing_source_kinds": [],
        "blocking_failure_source_kinds": [],
        "blocking_failure_backends": [],
    }
    current_contract = {
        **baseline_contract,
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 2}}},
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["regressions"] == [
        {
            "metric": ("parser_adapter_contract_backend_source_status_count:docling:pdf:passed"),
            "direction": "min",
            "baseline": 3.0,
            "current": 2.0,
            "allowed_delta": 0.0,
            "delta": -1.0,
            "reason": ("parser_adapter_contract_backend_source_passed_status_count_decreased"),
        }
    ]
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_adapter_warning_code_increase(
    tmp_path: Path,
) -> None:
    """adapter warning taxonomy の増加は coverage が同じでも失敗させる。"""
    baseline_contract = {
        "passed": True,
        "case_count": 6,
        "blocking_failure_count": 0,
        "scenarios": ["scanned_pdf_ocr"],
        "passed_scenarios": ["scanned_pdf_ocr"],
        "missing_scenarios": [],
        "blocking_failure_scenarios": [],
        "backend_passed_source_kinds": {"docling": ["pdf"]},
        "backend_passed_scenarios": {"docling": ["scanned_pdf_ocr"]},
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 3}}},
        "warning_code_counts": {},
        "blocking_failure_reason_counts": {},
        "missing_source_kinds": [],
        "blocking_failure_source_kinds": [],
        "blocking_failure_backends": [],
    }
    current_contract = {
        **baseline_contract,
        "warning_code_counts": {"docling_adapter_layout_warning": 1},
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["regressions"] == [
        {
            "metric": ("parser_adapter_contract_warning_code_count:docling_adapter_layout_warning"),
            "direction": "max",
            "baseline": 0.0,
            "current": 1.0,
            "allowed_delta": 0.0,
            "delta": 1.0,
            "reason": "parser_adapter_contract_warning_code_count_increased",
        }
    ]
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_blocking_reason_count_increase(
    tmp_path: Path,
) -> None:
    """blocking failure reason の増加は総数が同じでも理由単位で失敗させる。"""
    baseline_contract = {
        "passed": False,
        "case_count": 6,
        "blocking_failure_count": 2,
        "scenarios": ["scanned_pdf_ocr"],
        "passed_scenarios": ["scanned_pdf_ocr"],
        "missing_scenarios": [],
        "blocking_failure_scenarios": [],
        "backend_passed_source_kinds": {"docling": ["pdf"]},
        "backend_passed_scenarios": {"docling": ["scanned_pdf_ocr"]},
        "backend_source_status_counts": {"docling": {"pdf": {"passed": 3}}},
        "warning_code_counts": {},
        "blocking_failure_reason_counts": {
            "adapter_fallback_used": 2,
            "schema_remap_page_lineage_missing": 0,
        },
        "missing_source_kinds": [],
        "blocking_failure_source_kinds": [],
        "blocking_failure_backends": [],
    }
    current_contract = {
        **baseline_contract,
        "blocking_failure_reason_counts": {
            "adapter_fallback_used": 1,
            "schema_remap_page_lineage_missing": 1,
        },
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        passed=False,
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=baseline_contract,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        passed=False,
        parser_adapter_contract_mode="strict",
        parser_adapter_contract=current_contract,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["regressions"] == [
        {
            "metric": (
                "parser_adapter_contract_blocking_failure_reason_count:"
                "schema_remap_page_lineage_missing"
            ),
            "direction": "max",
            "baseline": 0.0,
            "current": 1.0,
            "allowed_delta": 0.0,
            "delta": 1.0,
            "reason": ("parser_adapter_contract_blocking_failure_reason_count_increased"),
        }
    ]
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_adapter_golden_gate_regression(
    tmp_path: Path,
) -> None:
    """同一 golden set adapter gate の退化は metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        adapter_golden_gate={
            "passed": True,
            "mode": "strict",
            "metrics_source": "file_processing_staging",
            "selected_backend": "docling",
            "recommended_backend": "docling",
            "metrics_applied_to": "docling",
            "required_source_kinds": ["pdf", "office", "html", "email", "image"],
            "manifest_source_kinds": ["pdf", "office", "html", "email", "image"],
            "covered_source_kinds": ["pdf", "office", "html", "email", "image"],
            "missing_manifest_source_kinds": [],
            "missing_source_kinds": [],
            "missing_metric_names": [],
            "failed_metric_count": 0,
            "contract_passed": True,
            "contract_case_count": 8,
            "contract_blocking_failure_count": 0,
            "contract_missing_source_kinds": [],
            "source_route_contract_gap_source_kinds": [],
            "blocker_codes": [],
        },
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        adapter_golden_gate={
            "passed": False,
            "mode": "runtime",
            "metrics_source": "runtime",
            "selected_backend": "local",
            "recommended_backend": "local",
            "metrics_applied_to": "local",
            "required_source_kinds": ["pdf", "office", "html", "email"],
            "manifest_source_kinds": ["pdf", "html", "email"],
            "covered_source_kinds": ["pdf", "html", "email"],
            "missing_manifest_source_kinds": ["office", "image"],
            "missing_source_kinds": ["office", "image"],
            "missing_metric_names": ["page_hit_accuracy"],
            "failed_metric_count": 1,
            "contract_passed": False,
            "contract_case_count": 6,
            "contract_blocking_failure_count": 2,
            "contract_missing_source_kinds": ["office"],
            "source_route_contract_gap_source_kinds": ["office"],
            "blocker_codes": [
                "adapter_golden_gate_source_kind_not_measured",
                "adapter_golden_gate_source_route_contract_missing",
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "adapter_golden_gate_passed_regressed" in reasons
    assert "adapter_golden_gate_mode_changed" in reasons
    assert "adapter_golden_gate_metrics_source_changed" in reasons
    assert "adapter_golden_gate_selected_backend_changed" in reasons
    assert "adapter_golden_gate_recommended_backend_changed" in reasons
    assert "adapter_golden_gate_metrics_applied_to_changed" in reasons
    assert "adapter_golden_gate_contract_regressed" in reasons
    assert "adapter_golden_gate_required_source_kind_count_decreased" in reasons
    assert "adapter_golden_gate_required_source_kinds_removed" in reasons
    assert "adapter_golden_gate_manifest_source_kind_count_decreased" in reasons
    assert "adapter_golden_gate_manifest_source_kinds_removed" in reasons
    assert "adapter_golden_gate_covered_source_kind_count_decreased" in reasons
    assert "adapter_golden_gate_covered_source_kinds_removed" in reasons
    assert "adapter_golden_gate_contract_case_count_decreased" in reasons
    assert "adapter_golden_gate_missing_source_kind_count_increased" in reasons
    assert "adapter_golden_gate_missing_manifest_source_kind_count_increased" in reasons
    assert "adapter_golden_gate_contract_missing_source_kind_count_increased" in reasons
    assert "adapter_golden_gate_source_route_contract_gap_source_kind_count_increased" in reasons
    assert "adapter_golden_gate_missing_metric_count_increased" in reasons
    assert "adapter_golden_gate_failed_metric_count_increased" in reasons
    assert "adapter_golden_gate_contract_blocking_failure_count_increased" in reasons
    assert "adapter_golden_gate_blocker_code_count_increased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_adapter_golden_gate_bad_set_additions(
    tmp_path: Path,
) -> None:
    """bad source/code が同数で入れ替わった場合も新規悪化として失敗させる。"""
    baseline_gate = {
        "passed": False,
        "mode": "strict",
        "metrics_source": "file_processing_staging",
        "selected_backend": "docling",
        "recommended_backend": "docling",
        "metrics_applied_to": "docling",
        "missing_source_kinds": ["office"],
        "missing_manifest_source_kinds": ["image"],
        "contract_missing_source_kinds": ["pdf"],
        "source_route_contract_gap_source_kinds": ["html"],
        "missing_metric_names": ["page_hit_accuracy"],
        "blocker_codes": ["adapter_golden_gate_metric_missing"],
    }
    current_gate = {
        **baseline_gate,
        "missing_source_kinds": ["email"],
        "missing_manifest_source_kinds": ["office"],
        "contract_missing_source_kinds": ["email"],
        "source_route_contract_gap_source_kinds": ["image"],
        "missing_metric_names": ["table_qa_accuracy"],
        "blocker_codes": ["adapter_golden_gate_contract_failed"],
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        passed=False,
        adapter_golden_gate=baseline_gate,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        passed=False,
        adapter_golden_gate=current_gate,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert reasons == {
        "adapter_golden_gate_missing_source_kinds_added",
        "adapter_golden_gate_missing_manifest_source_kinds_added",
        "adapter_golden_gate_contract_missing_source_kinds_added",
        "adapter_golden_gate_source_route_contract_gap_source_kinds_added",
        "adapter_golden_gate_missing_metric_names_added",
        "adapter_golden_gate_blocker_codes_added",
    }
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_adapter_golden_gate_case_ref_replacement(
    tmp_path: Path,
) -> None:
    """adapter golden gate は contract case hash 証跡の入れ替えも止める。"""
    baseline_gate = {
        "passed": True,
        "mode": "strict",
        "metrics_source": "file_processing_staging",
        "selected_backend": "docling",
        "recommended_backend": "docling",
        "metrics_applied_to": "docling",
        "contract_passed": True,
        "contract_case_count": 2,
        "contract_blocking_failure_count": 0,
        "contract_passed_case_refs": ["case:adapter-gate-old"],
        "contract_backend_passed_case_refs": {"docling": ["case:adapter-gate-old"]},
        "contract_blocking_failure_case_refs": [],
    }
    current_gate = {
        **baseline_gate,
        "contract_passed_case_refs": ["case:adapter-gate-new"],
        "contract_backend_passed_case_refs": {"docling": ["case:adapter-gate-new"]},
        "contract_blocking_failure_case_refs": ["case:adapter-gate-bad"],
    }
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        adapter_golden_gate=baseline_gate,
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        adapter_golden_gate=current_gate,
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "adapter_golden_gate_contract_passed_case_refs_removed",
        "adapter_golden_gate_contract_backend_passed_case_refs_removed",
        "adapter_golden_gate_contract_blocking_failure_case_refs_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "case:adapter-gate-old" not in output_text
    assert "case:adapter-gate-new" not in output_text


def test_file_processing_trend_cli_fails_on_parser_adapter_scorecard_regression(
    tmp_path: Path,
) -> None:
    """adapter scorecard の推奨・entry 証跡退化は metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_scorecard={
            "selected_backend": "docling",
            "recommended_backend": "docling",
            "metrics_source": "file_processing_staging",
            "metrics_applied_to": "docling",
            "entries": [
                _parser_adapter_scorecard_entry(
                    "docling",
                    rank=1,
                    score=96.0,
                    status="recommended",
                    recommended=True,
                    executable=True,
                    selected=True,
                    enabled=True,
                    installed=True,
                    metric_count=12,
                    reason_codes=["adapter_metrics_complete"],
                    warning_codes=[],
                ),
                _parser_adapter_scorecard_entry(
                    "mineru",
                    rank=2,
                    score=82.0,
                    status="eligible",
                    recommended=False,
                    executable=True,
                    selected=False,
                    enabled=True,
                    installed=True,
                    metric_count=8,
                ),
            ],
        },
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_scorecard={
            "selected_backend": "local",
            "recommended_backend": "local",
            "metrics_source": "runtime",
            "metrics_applied_to": "local",
            "entries": [
                _parser_adapter_scorecard_entry(
                    "docling",
                    rank=3,
                    score=64.0,
                    status="missing",
                    recommended=False,
                    executable=False,
                    selected=False,
                    enabled=False,
                    installed=False,
                    metric_count=5,
                    reason_codes=[
                        "adapter_metrics_complete",
                        "adapter_metric_evidence_incomplete",
                    ],
                    warning_codes=["adapter_package_missing"],
                ),
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "parser_adapter_scorecard_selected_backend_changed" in reasons
    assert "parser_adapter_scorecard_recommended_backend_changed" in reasons
    assert "parser_adapter_scorecard_metrics_source_changed" in reasons
    assert "parser_adapter_scorecard_metrics_applied_to_changed" in reasons
    assert "parser_adapter_scorecard_entries_removed" in reasons
    assert "parser_adapter_scorecard_entry_count_decreased" in reasons
    assert "parser_adapter_scorecard_status_regressed" in reasons
    assert "parser_adapter_scorecard_recommended_flag_regressed" in reasons
    assert "parser_adapter_scorecard_executable_flag_regressed" in reasons
    assert "parser_adapter_scorecard_enabled_flag_regressed" in reasons
    assert "parser_adapter_scorecard_installed_flag_regressed" in reasons
    assert "parser_adapter_scorecard_rank_regressed" in reasons
    assert "parser_adapter_scorecard_score_decreased" in reasons
    assert "parser_adapter_scorecard_metric_count_decreased" in reasons
    assert "parser_adapter_scorecard_reason_code_count_increased" in reasons
    assert "parser_adapter_scorecard_warning_code_count_increased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_parser_adapter_scorecard_code_replacement(
    tmp_path: Path,
) -> None:
    """adapter scorecard の reason/warning は同数で入れ替わっても止める。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_scorecard={
            "selected_backend": "docling",
            "recommended_backend": "docling",
            "metrics_source": "file_processing_staging",
            "metrics_applied_to": "docling",
            "entries": [
                _parser_adapter_scorecard_entry(
                    "docling",
                    rank=1,
                    score=96.0,
                    status="recommended",
                    recommended=True,
                    executable=True,
                    selected=True,
                    enabled=True,
                    installed=True,
                    metric_count=12,
                    reason_codes=["adapter_metrics_complete"],
                    warning_codes=["adapter_latency_warning"],
                ),
            ],
        },
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_scorecard={
            "selected_backend": "docling",
            "recommended_backend": "docling",
            "metrics_source": "file_processing_staging",
            "metrics_applied_to": "docling",
            "entries": [
                _parser_adapter_scorecard_entry(
                    "docling",
                    rank=1,
                    score=96.0,
                    status="recommended",
                    recommended=True,
                    executable=True,
                    selected=True,
                    enabled=True,
                    installed=True,
                    metric_count=12,
                    reason_codes=["adapter_metric_evidence_incomplete"],
                    warning_codes=["adapter_package_missing"],
                ),
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "parser_adapter_scorecard_reason_codes_added",
        "parser_adapter_scorecard_warning_codes_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "adapter_metric_evidence_incomplete" not in output_text
    assert "adapter_package_missing" not in output_text


def test_file_processing_trend_cli_fails_on_parser_adapter_source_route_regression(
    tmp_path: Path,
) -> None:
    """source kind 別 parser route の退化は adapter score が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_source_routes=[
            {
                "source_kind": "pdf",
                "candidate_order": ["docling", "mineru", "unstructured"],
                "attempted_order": ["docling"],
                "active_order": ["docling", "mineru"],
                "selected_backend": "docling",
                "reason_codes": ["contract_aware_source_route"],
                "warning_codes": [],
            },
            {
                "source_kind": "office",
                "candidate_order": ["docling", "unstructured"],
                "attempted_order": ["docling"],
                "active_order": ["docling"],
                "selected_backend": "docling",
                "reason_codes": [],
                "warning_codes": [],
            },
        ],
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_source_routes=[
            {
                "source_kind": "pdf",
                "candidate_order": ["mineru"],
                "attempted_order": [],
                "active_order": [],
                "selected_backend": "local",
                "reason_codes": [
                    "contract_aware_source_route",
                    "local_fallback_due_to_contract_gap",
                ],
                "warning_codes": ["docling_adapter_contract_unverified_for_source"],
            },
        ],
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "parser_adapter_source_routes_removed" in reasons
    assert "parser_adapter_source_route_count_decreased" in reasons
    assert "parser_adapter_source_route_selected_backend_changed" in reasons
    assert "parser_adapter_source_route_candidate_order_count_decreased" in reasons
    assert "parser_adapter_source_route_attempted_order_count_decreased" in reasons
    assert "parser_adapter_source_route_active_order_count_decreased" in reasons
    assert "parser_adapter_source_route_reason_code_count_increased" in reasons
    assert "parser_adapter_source_route_warning_code_count_increased" in reasons
    assert "parser_adapter_source_route_contract_gap_warning_count_increased" in reasons
    assert "parser_adapter_source_route_candidates_removed" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_parser_adapter_source_route_code_replacement(
    tmp_path: Path,
) -> None:
    """source route の reason/warning は同数で入れ替わっても止める。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_source_routes=[
            {
                "source_kind": "pdf",
                "candidate_order": ["docling", "mineru", "unstructured"],
                "attempted_order": ["docling"],
                "active_order": ["docling", "mineru"],
                "selected_backend": "docling",
                "reason_codes": ["contract_aware_source_route"],
                "warning_codes": ["mineru_adapter_contract_unverified_for_source"],
            },
        ],
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_source_routes=[
            {
                "source_kind": "pdf",
                "candidate_order": ["docling", "mineru", "unstructured"],
                "attempted_order": ["docling"],
                "active_order": ["docling", "mineru"],
                "selected_backend": "docling",
                "reason_codes": ["local_fallback_due_to_contract_gap"],
                "warning_codes": ["docling_adapter_contract_unverified_for_source"],
            },
        ],
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "parser_adapter_source_route_reason_codes_added",
        "parser_adapter_source_route_warning_codes_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "local_fallback_due_to_contract_gap" not in output_text
    assert "docling_adapter_contract_unverified_for_source" not in output_text


def test_file_processing_trend_cli_fails_on_parser_adapter_source_route_evidence_set_replacement(
    tmp_path: Path,
) -> None:
    """route の candidate/attempted/active backend は同数で入れ替わっても止める。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="baseline",
        parser_adapter_source_routes=[
            {
                "source_kind": "pdf",
                "candidate_order": ["docling", "mineru"],
                "attempted_order": ["docling", "mineru"],
                "active_order": ["docling", "mineru"],
                "selected_backend": "docling",
                "reason_codes": [],
                "warning_codes": [],
            },
        ],
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0},
        result_sha256="current",
        parser_adapter_source_routes=[
            {
                "source_kind": "pdf",
                "candidate_order": ["docling", "unstructured"],
                "attempted_order": ["docling", "unstructured"],
                "active_order": ["docling", "unstructured"],
                "selected_backend": "docling",
                "reason_codes": [],
                "warning_codes": [],
            },
        ],
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "parser_adapter_source_route_candidates_removed",
        "parser_adapter_source_route_attempted_backends_removed",
        "parser_adapter_source_route_active_backends_removed",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "mineru" not in output_text
    assert "unstructured" not in output_text


def test_file_processing_trend_cli_fails_on_runtime_gate_status_regression(
    tmp_path: Path,
) -> None:
    """runtime smoke / promotion blocker / threshold status は総合 metric が同じでも止める。"""
    baseline = _trend(
        metrics={"adapter_contract_coverage": 1.0, "table_qa_accuracy": 1.0},
        result_sha256="baseline",
        promotion_blocker_code_counts={},
        runtime_check_status_counts={"ok": 2},
        threshold_status_counts={"passed": 8, "failed": 0, "pending": 0},
        threshold_failures=[],
    )
    current = _trend(
        metrics={"adapter_contract_coverage": 1.0, "table_qa_accuracy": 1.0},
        result_sha256="current",
        promotion_blocker_code_counts={
            "parser_adapter_contract_failed": 1,
            "required_runtime_check_not_ok": 1,
        },
        runtime_check_status_counts={"ok": 1, "failed": 1, "skipped": 1},
        threshold_status_counts={"passed": 7, "failed": 1, "pending": 1},
        threshold_failures=[
            {
                "metric": "adapter_contract_coverage",
                "actual": 0.0,
                "required": 1.0,
                "reason": "parser_adapter_contract_failed",
            }
        ],
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "runtime_check_ok_status_count_decreased" in reasons
    assert "runtime_check_bad_status_count_increased" in reasons
    assert "promotion_blocker_code_count_increased" in reasons
    assert "threshold_passed_status_count_decreased" in reasons
    assert "threshold_failed_status_count_increased" in reasons
    assert "threshold_bad_status_count_increased" in reasons
    assert "threshold_failure_metrics_added" in reasons
    output_text = output_path.read_text(encoding="utf-8")
    assert "adapter_contract_coverage" in output_text
    assert "raw_text" not in output_text
    assert "case_results" not in output_text


def test_file_processing_trend_cli_fails_on_backend_source_kind_matrix_regression(
    tmp_path: Path,
) -> None:
    """backend/source kind matrix の証跡退化は coverage metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"backend_source_kind_coverage": 1.0},
        result_sha256="baseline",
        backend_source_kind_matrix={
            "value": 1.0,
            "required_source_kinds": ["pdf", "office", "html", "email", "image"],
            "covered_source_kinds": ["pdf", "office", "html", "email", "image"],
            "missing_source_kinds": [],
            "backend_source_kinds": {
                "docling": ["pdf", "office", "html"],
                "unstructured": ["email", "image"],
            },
        },
    )
    current = _trend(
        metrics={"backend_source_kind_coverage": 1.0},
        result_sha256="current",
        backend_source_kind_matrix={
            "value": 0.8,
            "required_source_kinds": ["pdf", "office", "html", "email"],
            "covered_source_kinds": ["pdf", "html", "email", "image"],
            "missing_source_kinds": ["office"],
            "backend_source_kinds": {
                "docling": ["pdf", "html"],
                "unstructured": ["email", "image"],
            },
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "backend_source_kind_matrix_required_source_kind_count_decreased" in reasons
    assert "backend_source_kind_matrix_covered_source_kind_count_decreased" in reasons
    assert "backend_source_kind_matrix_missing_source_kind_count_increased" in reasons
    assert "backend_source_kind_matrix_missing_source_kinds_added" in reasons
    assert "backend_source_kind_matrix_required_source_kinds_removed" in reasons
    assert "backend_source_kind_matrix_covered_source_kinds_removed" in reasons
    assert "backend_source_kind_matrix_backend_source_pairs_removed" in reasons
    assert "backend_source_kind_matrix_backend_source_pair_count_decreased" in reasons
    assert "backend_source_kind_matrix_value_decreased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_backend_source_kind_missing_set_replacement(
    tmp_path: Path,
) -> None:
    """backend/source matrix の missing source は同数で入れ替わっても止める。"""
    baseline = _trend(
        metrics={"backend_source_kind_coverage": 1.0},
        result_sha256="baseline",
        passed=False,
        backend_source_kind_matrix={
            "value": 0.8,
            "required_source_kinds": ["pdf", "office", "html", "email", "image"],
            "covered_source_kinds": ["pdf", "office", "html", "email"],
            "missing_source_kinds": ["image"],
            "backend_source_kinds": {"docling": ["pdf", "office", "html", "email"]},
        },
    )
    current = _trend(
        metrics={"backend_source_kind_coverage": 1.0},
        result_sha256="current",
        passed=False,
        backend_source_kind_matrix={
            "value": 0.8,
            "required_source_kinds": ["pdf", "office", "html", "email", "image"],
            "covered_source_kinds": ["pdf", "office", "html", "email"],
            "missing_source_kinds": ["office"],
            "backend_source_kinds": {"docling": ["pdf", "office", "html", "email"]},
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "backend_source_kind_matrix_missing_source_kinds_added"
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "office" not in output_text
    assert "image" not in output_text


def test_file_processing_trend_cli_fails_on_object_storage_artifact_chain_regression(
    tmp_path: Path,
) -> None:
    """Object Storage artifact chain の退化は metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="baseline",
        object_storage_artifact_chain={
            "passed": True,
            "roundtrip_check": "ok",
            "roundtrip_object_uri_scheme": "oci",
            "full_artifact_cached_case_count": 3,
            "full_artifact_oci_case_count": 3,
            "full_artifact_identity_present_case_count": 3,
            "full_artifact_readable_case_count": 3,
            "full_artifact_identity_verified_case_count": 3,
            "segment_artifact_expected_count": 6,
            "segment_artifact_oci_uri_count": 6,
            "segment_artifact_non_oci_uri_count": 0,
            "segment_artifact_readable_count": 6,
            "segment_artifact_identity_verified_count": 6,
            "artifact_integrity_error_count": 0,
            "retry_case_count": 2,
            "retained_successful_segment_artifact_count": 2,
            "segment_cache_miss_count": 0,
            "rewritten_successful_segment_artifact_count": 0,
            "audit_payload_redaction_enforced": True,
        },
    )
    current = _trend(
        metrics={"retrieval_recall": 1.0},
        result_sha256="current",
        object_storage_artifact_chain={
            "passed": False,
            "roundtrip_check": "failed",
            "roundtrip_object_uri_scheme": "local",
            "full_artifact_cached_case_count": 2,
            "full_artifact_oci_case_count": 1,
            "full_artifact_identity_present_case_count": 2,
            "full_artifact_readable_case_count": 1,
            "full_artifact_identity_verified_case_count": 1,
            "segment_artifact_expected_count": 5,
            "segment_artifact_oci_uri_count": 4,
            "segment_artifact_non_oci_uri_count": 1,
            "segment_artifact_readable_count": 4,
            "segment_artifact_identity_verified_count": 3,
            "artifact_integrity_error_count": 1,
            "retry_case_count": 1,
            "retained_successful_segment_artifact_count": 1,
            "segment_cache_miss_count": 1,
            "rewritten_successful_segment_artifact_count": 1,
            "audit_payload_redaction_enforced": False,
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "object_storage_artifact_chain_passed_regressed" in reasons
    assert "object_storage_artifact_roundtrip_check_regressed" in reasons
    assert "object_storage_artifact_roundtrip_uri_scheme_regressed" in reasons
    assert "object_storage_audit_payload_redaction_regressed" in reasons
    assert "object_storage_full_artifact_cached_case_count_decreased" in reasons
    assert "object_storage_full_artifact_oci_case_count_decreased" in reasons
    assert "object_storage_full_artifact_identity_count_decreased" in reasons
    assert "object_storage_full_artifact_readable_count_decreased" in reasons
    assert "object_storage_full_artifact_identity_verified_count_decreased" in reasons
    assert "object_storage_segment_artifact_expected_count_decreased" in reasons
    assert "object_storage_segment_artifact_oci_uri_count_decreased" in reasons
    assert "object_storage_segment_artifact_non_oci_uri_count_increased" in reasons
    assert "object_storage_segment_artifact_readable_count_decreased" in reasons
    assert "object_storage_segment_artifact_identity_verified_count_decreased" in reasons
    assert "object_storage_artifact_integrity_error_count_increased" in reasons
    assert "object_storage_retry_case_count_decreased" in reasons
    assert "object_storage_retained_successful_segment_artifact_count_decreased" in reasons
    assert "object_storage_segment_cache_miss_count_increased" in reasons
    assert "object_storage_rewritten_successful_segment_artifact_count_increased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_segment_artifact_reuse_regression(
    tmp_path: Path,
) -> None:
    """segment retry/reuse evidence は object chain とは別に縮小と再処理増加を止める。"""
    baseline = _trend(
        metrics={"failed_segment_rate": 0.0},
        result_sha256="baseline",
        segment_artifact_reuse={
            "case_count": 8,
            "retry_case_count": 2,
            "initial_failed_segment_count": 2,
            "initial_successful_segment_artifact_count": 3,
            "retained_successful_segment_artifact_count": 3,
            "rewritten_successful_segment_artifact_count": 0,
            "reprocessed_successful_segment_count": 0,
            "failed_segment_retried_count": 2,
            "failed_segment_succeeded_count": 2,
            "segment_cache_miss_count": 0,
            "segment_cache_miss_case_count": 0,
            "full_artifact_cached_case_count": 3,
            "full_artifact_identity_present_case_count": 3,
            "segment_artifact_expected_count": 5,
            "segment_artifact_readable_count": 5,
            "segment_artifact_identity_verified_count": 5,
            "segment_artifact_non_oci_uri_count": 0,
            "artifact_integrity_error_count": 0,
        },
    )
    current = _trend(
        metrics={"failed_segment_rate": 0.0},
        result_sha256="current",
        segment_artifact_reuse={
            "case_count": 7,
            "retry_case_count": 1,
            "initial_failed_segment_count": 1,
            "initial_successful_segment_artifact_count": 2,
            "retained_successful_segment_artifact_count": 1,
            "rewritten_successful_segment_artifact_count": 1,
            "reprocessed_successful_segment_count": 1,
            "failed_segment_retried_count": 1,
            "failed_segment_succeeded_count": 1,
            "segment_cache_miss_count": 1,
            "segment_cache_miss_case_count": 1,
            "full_artifact_cached_case_count": 2,
            "full_artifact_identity_present_case_count": 2,
            "segment_artifact_expected_count": 4,
            "segment_artifact_readable_count": 3,
            "segment_artifact_identity_verified_count": 3,
            "segment_artifact_non_oci_uri_count": 1,
            "artifact_integrity_error_count": 1,
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "segment_artifact_reuse_case_count_decreased" in reasons
    assert "segment_artifact_reuse_retry_case_count_decreased" in reasons
    assert "segment_artifact_reuse_initial_failed_segment_count_decreased" in reasons
    assert "segment_artifact_reuse_initial_successful_artifact_count_decreased" in reasons
    assert "segment_artifact_reuse_retained_successful_artifact_count_decreased" in reasons
    assert "segment_artifact_reuse_failed_segment_retried_count_decreased" in reasons
    assert "segment_artifact_reuse_failed_segment_succeeded_count_decreased" in reasons
    assert "segment_artifact_reuse_full_artifact_cached_case_count_decreased" in reasons
    assert "segment_artifact_reuse_full_artifact_identity_count_decreased" in reasons
    assert "segment_artifact_reuse_segment_artifact_expected_count_decreased" in reasons
    assert "segment_artifact_reuse_segment_artifact_readable_count_decreased" in reasons
    assert "segment_artifact_reuse_segment_artifact_identity_count_decreased" in reasons
    assert "segment_artifact_reuse_rewritten_successful_artifact_count_increased" in reasons
    assert "segment_artifact_reuse_reprocessed_successful_segment_count_increased" in reasons
    assert "segment_artifact_reuse_cache_miss_count_increased" in reasons
    assert "segment_artifact_reuse_cache_miss_case_count_increased" in reasons
    assert "segment_artifact_reuse_non_oci_uri_count_increased" in reasons
    assert "segment_artifact_reuse_integrity_error_count_increased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_artifact_case_ref_replacement(
    tmp_path: Path,
) -> None:
    """artifact/retry case hash evidence は同数で入れ替わっても止める。"""
    baseline_object_chain = {
        "passed": True,
        "roundtrip_check": "ok",
        "roundtrip_object_uri_scheme": "oci",
        "full_artifact_cached_case_count": 1,
        "full_artifact_cached_case_refs": ["case:artifact-old"],
        "full_artifact_oci_case_count": 1,
        "full_artifact_identity_present_case_count": 1,
        "full_artifact_readable_case_count": 1,
        "full_artifact_identity_verified_case_count": 1,
        "full_artifact_identity_verified_case_refs": ["case:artifact-old"],
        "segment_artifact_expected_count": 1,
        "segment_artifact_oci_uri_count": 1,
        "segment_artifact_non_oci_uri_count": 0,
        "segment_artifact_readable_count": 1,
        "segment_artifact_identity_verified_count": 1,
        "artifact_integrity_error_count": 0,
        "retry_case_count": 1,
        "retry_case_refs": ["case:retry-old"],
        "retained_successful_segment_artifact_count": 1,
        "retained_successful_segment_artifact_case_refs": ["case:retry-old"],
        "segment_cache_miss_count": 0,
        "rewritten_successful_segment_artifact_count": 0,
        "audit_payload_redaction_enforced": True,
    }
    baseline_segment_reuse = {
        "case_count": 1,
        "case_refs": ["case:artifact-old"],
        "retry_case_count": 1,
        "retry_case_refs": ["case:retry-old"],
        "initial_failed_segment_count": 1,
        "initial_successful_segment_artifact_count": 1,
        "retained_successful_segment_artifact_count": 1,
        "retained_successful_segment_artifact_case_refs": ["case:retry-old"],
        "rewritten_successful_segment_artifact_count": 0,
        "reprocessed_successful_segment_count": 0,
        "failed_segment_retried_count": 1,
        "failed_segment_succeeded_count": 1,
        "segment_cache_miss_count": 0,
        "segment_cache_miss_case_count": 0,
        "full_artifact_cached_case_count": 1,
        "full_artifact_cached_case_refs": ["case:artifact-old"],
        "full_artifact_identity_present_case_count": 1,
        "full_artifact_identity_verified_case_refs": ["case:artifact-old"],
        "segment_artifact_expected_count": 1,
        "segment_artifact_readable_count": 1,
        "segment_artifact_identity_verified_count": 1,
        "segment_artifact_non_oci_uri_count": 0,
        "artifact_integrity_error_count": 0,
    }
    baseline = _trend(
        metrics={"failed_segment_rate": 0.0},
        result_sha256="baseline",
        object_storage_artifact_chain=baseline_object_chain,
        segment_artifact_reuse=baseline_segment_reuse,
    )
    current = _trend(
        metrics={"failed_segment_rate": 0.0},
        result_sha256="current",
        object_storage_artifact_chain={
            **baseline_object_chain,
            "full_artifact_cached_case_refs": ["case:artifact-new"],
            "full_artifact_identity_verified_case_refs": ["case:artifact-new"],
            "retry_case_refs": ["case:retry-new"],
            "retained_successful_segment_artifact_case_refs": ["case:retry-new"],
        },
        segment_artifact_reuse={
            **baseline_segment_reuse,
            "case_refs": ["case:artifact-new"],
            "retry_case_refs": ["case:retry-new"],
            "full_artifact_cached_case_refs": ["case:artifact-new"],
            "full_artifact_identity_verified_case_refs": ["case:artifact-new"],
            "retained_successful_segment_artifact_case_refs": ["case:retry-new"],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "object_storage_full_artifact_case_refs_removed",
        "object_storage_full_artifact_identity_case_refs_removed",
        "object_storage_retry_case_refs_removed",
        "object_storage_retained_segment_artifact_case_refs_removed",
        "segment_artifact_reuse_case_refs_removed",
        "segment_artifact_reuse_retry_case_refs_removed",
        "segment_artifact_reuse_full_artifact_case_refs_removed",
        "segment_artifact_reuse_full_artifact_identity_case_refs_removed",
        "segment_artifact_reuse_retained_segment_artifact_case_refs_removed",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "case:artifact-old" not in output_text
    assert "case:artifact-new" not in output_text


def test_file_processing_trend_cli_fails_on_table_cell_lineage_case_ref_replacement(
    tmp_path: Path,
) -> None:
    """cell lineage は件数が同じでも case hash 証跡の入れ替えを止める。"""
    baseline_table_cell_lineage = {
        "case_count": 2,
        "expected_case_count": 1,
        "expected_ref_count": 4,
        "resolved_ref_count": 4,
        "covered_ref_count": 4,
        "lineage_ref_count": 4,
        "unresolved_ref_count": 0,
        "uncovered_ref_count": 0,
        "expected_case_refs": ["case:table-old"],
        "resolved_case_refs": ["case:table-old"],
        "covered_case_refs": ["case:table-old"],
        "lineage_case_refs": ["case:table-old"],
        "unresolved_case_refs": [],
        "uncovered_case_refs": [],
        "all_expected_refs_resolved": True,
        "all_expected_refs_covered": True,
        "coverage": 1.0,
    }
    baseline = _trend(
        metrics={"table_cell_lineage_coverage": 1.0},
        result_sha256="baseline",
        table_cell_lineage=baseline_table_cell_lineage,
    )
    current = _trend(
        metrics={"table_cell_lineage_coverage": 1.0},
        result_sha256="current",
        table_cell_lineage={
            **baseline_table_cell_lineage,
            "expected_case_refs": ["case:table-new"],
            "resolved_case_refs": ["case:table-new"],
            "covered_case_refs": ["case:table-new"],
            "lineage_case_refs": ["case:table-new"],
            "unresolved_case_refs": ["case:table-bad"],
            "uncovered_case_refs": ["case:table-bad"],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "table_cell_lineage_expected_case_refs_removed",
        "table_cell_lineage_resolved_case_refs_removed",
        "table_cell_lineage_covered_case_refs_removed",
        "table_cell_lineage_lineage_case_refs_removed",
        "table_cell_lineage_unresolved_case_refs_added",
        "table_cell_lineage_uncovered_case_refs_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "case:table-old" not in output_text
    assert "case:table-new" not in output_text
    assert "case:table-bad" not in output_text


def test_file_processing_trend_cli_fails_on_preview_addressability_case_ref_replacement(
    tmp_path: Path,
) -> None:
    """preview addressability は同じ coverage でも fixture 証跡の入れ替えを止める。"""
    baseline_preview_addressability = {
        "case_count": 2,
        "preview_gate_case_count": 1,
        "chunk_target_count": 3,
        "chunk_bbox_count": 3,
        "chunk_addressable_count": 3,
        "extraction_bbox_target_count": 2,
        "extraction_addressable_target_count": 2,
        "target_count": 5,
        "addressable_target_count": 5,
        "unaddressable_target_count": 0,
        "preview_gate_case_refs": ["case:preview-old"],
        "addressable_case_refs": ["case:preview-old"],
        "unaddressable_case_refs": [],
        "chunk_bbox_case_refs": ["case:preview-old"],
        "chunk_missing_bbox_case_refs": [],
        "chunk_bbox_coverage": 1.0,
        "coverage": 1.0,
        "all_targets_addressable": True,
        "all_chunks_have_bbox": True,
    }
    baseline = _trend(
        metrics={"preview_addressability_coverage": 1.0},
        result_sha256="baseline",
        preview_addressability=baseline_preview_addressability,
    )
    current = _trend(
        metrics={"preview_addressability_coverage": 1.0},
        result_sha256="current",
        preview_addressability={
            **baseline_preview_addressability,
            "preview_gate_case_refs": ["case:preview-new"],
            "addressable_case_refs": ["case:preview-new"],
            "unaddressable_case_refs": ["case:preview-bad"],
            "chunk_bbox_case_refs": ["case:preview-new"],
            "chunk_missing_bbox_case_refs": ["case:preview-bad"],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "preview_addressability_gate_case_refs_removed",
        "preview_addressability_addressable_case_refs_removed",
        "preview_addressability_chunk_bbox_case_refs_removed",
        "preview_addressability_unaddressable_case_refs_added",
        "preview_addressability_chunk_missing_bbox_case_refs_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "case:preview-old" not in output_text
    assert "case:preview-new" not in output_text
    assert "case:preview-bad" not in output_text


def test_file_processing_trend_cli_fails_on_table_cell_lineage_evidence_regression(
    tmp_path: Path,
) -> None:
    """cell-level citation evidence は coverage metric が同じでも縮小を止める。"""
    baseline = _trend(
        metrics={"table_cell_lineage_coverage": 1.0},
        result_sha256="baseline",
        table_cell_lineage={
            "case_count": 8,
            "expected_case_count": 2,
            "expected_ref_count": 3,
            "resolved_ref_count": 3,
            "covered_ref_count": 3,
            "lineage_ref_count": 3,
            "unresolved_ref_count": 0,
            "uncovered_ref_count": 0,
            "all_expected_refs_resolved": True,
            "all_expected_refs_covered": True,
            "coverage": 1.0,
        },
    )
    current = _trend(
        metrics={"table_cell_lineage_coverage": 1.0},
        result_sha256="current",
        table_cell_lineage={
            "case_count": 8,
            "expected_case_count": 1,
            "expected_ref_count": 2,
            "resolved_ref_count": 1,
            "covered_ref_count": 1,
            "lineage_ref_count": 1,
            "unresolved_ref_count": 1,
            "uncovered_ref_count": 1,
            "all_expected_refs_resolved": False,
            "all_expected_refs_covered": False,
            "coverage": 0.5,
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "table_cell_lineage_expected_case_count_decreased" in reasons
    assert "table_cell_lineage_expected_ref_count_decreased" in reasons
    assert "table_cell_lineage_resolved_ref_count_decreased" in reasons
    assert "table_cell_lineage_covered_ref_count_decreased" in reasons
    assert "table_cell_lineage_lineage_ref_count_decreased" in reasons
    assert "table_cell_lineage_unresolved_ref_count_increased" in reasons
    assert "table_cell_lineage_uncovered_ref_count_increased" in reasons
    assert "table_cell_lineage_evidence_coverage_decreased" in reasons
    assert "table_cell_lineage_all_expected_refs_resolved_regressed" in reasons
    assert "table_cell_lineage_all_expected_refs_covered_regressed" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_preview_addressability_evidence_regression(
    tmp_path: Path,
) -> None:
    """preview bbox evidence は coverage metric が同じでも縮小を止める。"""
    baseline = _trend(
        metrics={"preview_addressability_coverage": 1.0},
        result_sha256="baseline",
        preview_addressability={
            "case_count": 8,
            "preview_gate_case_count": 2,
            "chunk_target_count": 3,
            "chunk_bbox_count": 3,
            "chunk_addressable_count": 3,
            "extraction_bbox_target_count": 2,
            "extraction_addressable_target_count": 2,
            "target_count": 5,
            "addressable_target_count": 5,
            "unaddressable_target_count": 0,
            "chunk_bbox_coverage": 1.0,
            "coverage": 1.0,
            "all_targets_addressable": True,
            "all_chunks_have_bbox": True,
        },
    )
    current = _trend(
        metrics={"preview_addressability_coverage": 1.0},
        result_sha256="current",
        preview_addressability={
            "case_count": 8,
            "preview_gate_case_count": 1,
            "chunk_target_count": 2,
            "chunk_bbox_count": 1,
            "chunk_addressable_count": 1,
            "extraction_bbox_target_count": 1,
            "extraction_addressable_target_count": 1,
            "target_count": 3,
            "addressable_target_count": 2,
            "unaddressable_target_count": 1,
            "chunk_bbox_coverage": 0.5,
            "coverage": 0.67,
            "all_targets_addressable": False,
            "all_chunks_have_bbox": False,
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "preview_addressability_gate_case_count_decreased" in reasons
    assert "preview_addressability_chunk_target_count_decreased" in reasons
    assert "preview_addressability_chunk_bbox_count_decreased" in reasons
    assert "preview_addressability_chunk_addressable_count_decreased" in reasons
    assert "preview_addressability_extraction_bbox_target_count_decreased" in reasons
    assert "preview_addressability_extraction_addressable_target_count_decreased" in reasons
    assert "preview_addressability_target_count_decreased" in reasons
    assert "preview_addressability_addressable_target_count_decreased" in reasons
    assert "preview_addressability_unaddressable_target_count_increased" in reasons
    assert "preview_addressability_evidence_coverage_decreased" in reasons
    assert "preview_addressability_chunk_bbox_coverage_decreased" in reasons
    assert "preview_addressability_all_targets_addressable_regressed" in reasons
    assert "preview_addressability_all_chunks_have_bbox_regressed" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_chunk_template_scorecard_regression(
    tmp_path: Path,
) -> None:
    """template 別 scorecard evidence の退化は総合 metric が同じでも失敗させる。"""
    baseline = _trend(
        metrics={"chunk_block_integrity": 1.0},
        result_sha256="baseline",
        chunk_template_scorecard={
            "recommended_template": "pdf_layout",
            "metrics_source": "file_processing_staging",
            "promotion_blocking": False,
            "observed_templates": ["pdf_layout", "html_semantic"],
            "entries": [
                _chunk_template_entry("pdf_layout", status="recommended", score=100.0),
                _chunk_template_entry(
                    "html_semantic",
                    status="healthy",
                    score=100.0,
                    expected_case_count=2,
                    measured_case_count=2,
                    covered_source_kinds=["html", "office"],
                    covered_scenarios=["html_semantic_blocks", "japanese_docx_layout"],
                    missing_source_kinds=[],
                    missing_scenarios=[],
                    reason_codes=["adaptive_chunking_metrics_applied"],
                ),
            ],
        },
    )
    current = _trend(
        metrics={"chunk_block_integrity": 1.0},
        result_sha256="current",
        chunk_template_scorecard={
            "recommended_template": "pdf_layout",
            "metrics_source": "file_processing_staging",
            "promotion_blocking": True,
            "observed_templates": ["pdf_layout", "html_semantic"],
            "entries": [
                _chunk_template_entry("pdf_layout", status="recommended", score=100.0),
                _chunk_template_entry(
                    "html_semantic",
                    status="blocked",
                    score=72.0,
                    expected_case_count=1,
                    measured_case_count=1,
                    covered_source_kinds=["html"],
                    covered_scenarios=["html_semantic_blocks"],
                    missing_source_kinds=["office"],
                    missing_scenarios=["japanese_docx_layout"],
                    reason_codes=[
                        "adaptive_chunking_metrics_applied",
                        "chunk_template_source_kind_evidence_missing",
                        "chunk_template_scenario_evidence_missing",
                    ],
                ),
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reasons = {regression["reason"] for regression in payload["regressions"]}
    assert "chunk_template_scorecard_promotion_blocking_regressed" in reasons
    assert "chunk_template_status_regressed" in reasons
    assert "chunk_template_promotion_blocking_regressed" in reasons
    assert "chunk_template_score_decreased" in reasons
    assert "chunk_template_expected_case_count_decreased" in reasons
    assert "chunk_template_measured_case_count_decreased" in reasons
    assert "chunk_template_covered_source_kind_count_decreased" in reasons
    assert "chunk_template_covered_scenario_count_decreased" in reasons
    assert "chunk_template_missing_source_kind_count_increased" in reasons
    assert "chunk_template_missing_scenario_count_increased" in reasons
    assert "chunk_template_reason_code_count_increased" in reasons
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_trend_cli_fails_on_chunk_template_bad_set_replacement(
    tmp_path: Path,
) -> None:
    """template の missing/reason は同数で入れ替わっても止める。"""
    baseline = _trend(
        metrics={"chunk_block_integrity": 1.0},
        result_sha256="baseline",
        passed=False,
        chunk_template_scorecard={
            "recommended_template": "html_semantic",
            "metrics_source": "file_processing_staging",
            "promotion_blocking": True,
            "observed_templates": ["html_semantic"],
            "entries": [
                _chunk_template_entry(
                    "html_semantic",
                    status="blocked",
                    score=72.0,
                    expected_case_count=2,
                    measured_case_count=1,
                    covered_source_kinds=["html"],
                    covered_scenarios=["html_semantic_blocks"],
                    missing_source_kinds=["office"],
                    missing_scenarios=["japanese_docx_layout"],
                    reason_codes=["chunk_template_source_kind_evidence_missing"],
                ),
            ],
        },
    )
    current = _trend(
        metrics={"chunk_block_integrity": 1.0},
        result_sha256="current",
        passed=False,
        chunk_template_scorecard={
            "recommended_template": "html_semantic",
            "metrics_source": "file_processing_staging",
            "promotion_blocking": True,
            "observed_templates": ["html_semantic"],
            "entries": [
                _chunk_template_entry(
                    "html_semantic",
                    status="blocked",
                    score=72.0,
                    expected_case_count=2,
                    measured_case_count=1,
                    covered_source_kinds=["html"],
                    covered_scenarios=["html_semantic_blocks"],
                    missing_source_kinds=["email"],
                    missing_scenarios=["email_thread_headers"],
                    reason_codes=["chunk_template_scenario_evidence_missing"],
                ),
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "chunk_template_missing_source_kinds_added",
        "chunk_template_missing_scenarios_added",
        "chunk_template_reason_codes_added",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "email_thread_headers" not in output_text
    assert "chunk_template_scenario_evidence_missing" not in output_text


def test_file_processing_trend_cli_fails_on_chunk_template_covered_set_replacement(
    tmp_path: Path,
) -> None:
    """template の covered source/scenario は同数で入れ替わっても止める。"""
    baseline = _trend(
        metrics={"chunk_block_integrity": 1.0},
        result_sha256="baseline",
        chunk_template_scorecard={
            "recommended_template": "html_semantic",
            "metrics_source": "file_processing_staging",
            "promotion_blocking": False,
            "observed_templates": ["html_semantic"],
            "entries": [
                _chunk_template_entry(
                    "html_semantic",
                    status="healthy",
                    score=100.0,
                    expected_case_count=2,
                    measured_case_count=2,
                    covered_source_kinds=["html", "office"],
                    covered_scenarios=["html_semantic_blocks", "japanese_docx_layout"],
                    missing_source_kinds=[],
                    missing_scenarios=[],
                    reason_codes=["adaptive_chunking_metrics_applied"],
                ),
            ],
        },
    )
    current = _trend(
        metrics={"chunk_block_integrity": 1.0},
        result_sha256="current",
        chunk_template_scorecard={
            "recommended_template": "html_semantic",
            "metrics_source": "file_processing_staging",
            "promotion_blocking": False,
            "observed_templates": ["html_semantic"],
            "entries": [
                _chunk_template_entry(
                    "html_semantic",
                    status="healthy",
                    score=100.0,
                    expected_case_count=2,
                    measured_case_count=2,
                    covered_source_kinds=["html", "email"],
                    covered_scenarios=["html_semantic_blocks", "email_thread_headers"],
                    missing_source_kinds=[],
                    missing_scenarios=[],
                    reason_codes=["adaptive_chunking_metrics_applied"],
                ),
            ],
        },
    )
    baseline_path = _write_json(tmp_path / "baseline.json", baseline)
    current_path = _write_json(tmp_path / "current.json", current)
    output_path = tmp_path / "trend-regression.json"

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path), "--output", str(output_path)]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {regression["reason"] for regression in payload["regressions"]} == {
        "chunk_template_covered_source_kinds_removed",
        "chunk_template_covered_scenarios_removed",
    }
    output_text = output_path.read_text(encoding="utf-8")
    assert "japanese_docx_layout" not in output_text
    assert "email_thread_headers" not in output_text


def test_file_processing_trend_cli_rejects_invalid_payload(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """入力不備は安全な error として exit 2 を返す。"""
    baseline_path = _write_json(tmp_path / "baseline.json", _trend(metrics={}))
    current_path = _write_json(tmp_path / "current.json", {"kind": "file_processing"})

    exit_code = file_processing_trend_cli.main(
        [str(current_path), "--baseline", str(baseline_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "metrics object" in captured.err
    assert "raw_text" not in captured.err


def _trend(
    *,
    metrics: dict[str, object],
    result_sha256: str = "trend",
    kind: str = "file_processing_staging",
    passed: bool = True,
    case_count: int | None = None,
    gate_count: int | None = None,
    failure_count: int = 0,
    promotion_blocker_count: int = 0,
    promotion_ready: bool = True,
    promotion_blocker_code_counts: dict[str, object] | None = None,
    runtime_check_status_counts: dict[str, object] | None = None,
    threshold_status_counts: dict[str, object] | None = None,
    threshold_failures: list[dict[str, object]] | None = None,
    staging_dataset_policy: dict[str, object] | None = None,
    parser_adapter_contract_mode: str | None = None,
    parser_adapter_contract: dict[str, object] | None = None,
    adapter_golden_gate: dict[str, object] | None = None,
    parser_adapter_source_routes: list[dict[str, object]] | None = None,
    parser_adapter_scorecard: dict[str, object] | None = None,
    backend_source_kind_matrix: dict[str, object] | None = None,
    object_storage_artifact_chain: dict[str, object] | None = None,
    segment_artifact_reuse: dict[str, object] | None = None,
    chunk_template_scorecard: dict[str, object] | None = None,
    table_cell_lineage: dict[str, object] | None = None,
    preview_addressability: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": kind,
        "passed": passed,
        "promotion_ready": promotion_ready,
        "failure_count": failure_count,
        "promotion_blocker_count": promotion_blocker_count,
        "result_sha256": result_sha256,
        "metrics": metrics,
    }
    if case_count is not None:
        payload["case_count"] = case_count
    if gate_count is not None:
        payload["gate_count"] = gate_count
    if promotion_blocker_code_counts is not None:
        payload["promotion_blocker_code_counts"] = promotion_blocker_code_counts
    if runtime_check_status_counts is not None:
        payload["runtime_check_status_counts"] = runtime_check_status_counts
    if threshold_status_counts is not None:
        payload["threshold_status_counts"] = threshold_status_counts
    if threshold_failures is not None:
        payload["threshold_failures"] = threshold_failures
    if staging_dataset_policy is not None:
        payload["staging_dataset_policy"] = staging_dataset_policy
    if parser_adapter_contract_mode is not None:
        payload["parser_adapter_contract_mode"] = parser_adapter_contract_mode
    if parser_adapter_contract is not None:
        payload["parser_adapter_contract"] = parser_adapter_contract
    if adapter_golden_gate is not None:
        payload["adapter_golden_gate"] = adapter_golden_gate
    if parser_adapter_source_routes is not None:
        payload["parser_adapter_source_routes"] = parser_adapter_source_routes
    if parser_adapter_scorecard is not None:
        payload["parser_adapter_scorecard"] = parser_adapter_scorecard
    if backend_source_kind_matrix is not None:
        payload["backend_source_kind_matrix"] = backend_source_kind_matrix
    if object_storage_artifact_chain is not None:
        payload["object_storage_artifact_chain"] = object_storage_artifact_chain
    if segment_artifact_reuse is not None:
        payload["segment_artifact_reuse"] = segment_artifact_reuse
    if chunk_template_scorecard is not None:
        payload["chunk_template_scorecard"] = chunk_template_scorecard
    if table_cell_lineage is not None:
        payload["table_cell_lineage"] = table_cell_lineage
    if preview_addressability is not None:
        payload["preview_addressability"] = preview_addressability
    return payload


def _parser_adapter_scorecard_entry(
    backend: str,
    *,
    rank: int,
    score: float,
    status: str,
    recommended: bool,
    executable: bool,
    selected: bool,
    enabled: bool,
    installed: bool,
    metric_count: int,
    reason_codes: list[str] | None = None,
    warning_codes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "backend": backend,
        "rank": rank,
        "score": score,
        "status": status,
        "recommended": recommended,
        "executable": executable,
        "selected": selected,
        "enabled": enabled,
        "installed": installed,
        "metric_count": metric_count,
        "reason_codes": reason_codes or [],
        "warning_codes": warning_codes or [],
    }


def _chunk_template_entry(
    template: str,
    *,
    status: str,
    score: float,
    expected_case_count: int = 1,
    measured_case_count: int = 1,
    covered_source_kinds: list[str] | None = None,
    covered_scenarios: list[str] | None = None,
    missing_source_kinds: list[str] | None = None,
    missing_scenarios: list[str] | None = None,
    reason_codes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "template": template,
        "status": status,
        "score": score,
        "promotion_blocking": status == "blocked",
        "metric_count": 3,
        "expected_case_count": expected_case_count,
        "measured_case_count": measured_case_count,
        "covered_source_kinds": covered_source_kinds or ["pdf"],
        "covered_scenarios": covered_scenarios or ["scanned_pdf_ocr"],
        "missing_source_kinds": missing_source_kinds or [],
        "missing_scenarios": missing_scenarios or [],
        "reason_codes": reason_codes or ["adaptive_chunking_metrics_applied"],
    }


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path
