"""取込品質レポートの構造カウントを検証する。"""

from app.rag.ingestion_quality import (
    build_ingestion_quality_report,
    summarize_ingestion_quality,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)


def test_quality_report_counts_first_class_structure_and_parser_artifacts() -> None:
    """Docling 系の first-class metadata から品質指標を落とさず集計する。"""
    extraction = StructuredExtraction(
        raw_text="本文\n表\n図",
        confidence=0.92,
        elements=[
            DocumentElement(kind="text", text="本文", page_number=1, confidence=0.6),
        ],
        pages=[
            ExtractionPage(page_number=1, element_ids=["el-0000"]),
            ExtractionPage(page_number=2),
            ExtractionPage(page_number=3, element_ids=["tbl-main"]),
            ExtractionPage(page_number=4, element_ids=["fig-1"]),
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-main",
                element_id="tbl-main",
                page_number=3,
                cells=[ExtractionTableCell(row=0, col=0, text="金額", confidence=0.4)],
            )
        ],
        assets=[ExtractionAsset(asset_id="fig-1", kind="image", page_number=4, alt_text="構成図")],
        parser_artifacts={
            "page_count": 5,
            "table_count": 2,
            "equation_count": 2,
            "asset_count": 3,
            "low_confidence_count": 4,
            "failed_segment_count": 1,
        },
    )

    report = build_ingestion_quality_report(
        extraction,
        parser_profile="external_layout",
        parser_backend="docling",
        parser_version="2.0.0",
    )

    assert report.parser_backend == "docling"
    assert report.parser_version == "2.0.0"
    assert report.page_count == 5
    assert report.page_coverage == 0.6
    assert report.table_count == 2
    assert report.figure_count == 3
    assert report.formula_count == 2
    assert report.low_confidence_count == 4
    assert report.failed_segment_count == 1
    assert report.risk_level == "medium"
    assert report.quality_warnings == [
        "table_structure_review",
        "figure_ocr_review",
        "formula_review",
        "low_confidence_elements",
        "failed_segments",
    ]


def test_legacy_quality_summary_infers_first_class_tables_assets_and_artifacts() -> None:
    """quality_report が無い旧 extraction JSON も構造 metadata から集計する。"""
    summary = summarize_ingestion_quality(
        [
            {
                "elements": [
                    {
                        "kind": "text",
                        "text": "本文",
                        "page_number": 1,
                        "confidence": 0.6,
                    }
                ],
                "pages": [
                    {"page_number": 1, "element_ids": ["el-1"]},
                    {"page_number": 2, "element_ids": []},
                ],
                "tables": [
                    {
                        "table_id": "tbl-1",
                        "page_number": 2,
                        "cells": [{"row": 0, "col": 0, "text": "A", "confidence": 0.4}],
                    }
                ],
                "assets": [{"asset_id": "chart-1", "kind": "chart", "page_number": 2}],
                "parser_artifacts": {
                    "page_count": "4",
                    "adapter_table_count": 2,
                    "formula_count": 1,
                    "failed_segment_count": 1,
                },
            }
        ]
    )

    assert summary["document_count"] == 1
    assert summary["table_document_count"] == 1
    assert summary["figure_document_count"] == 1
    assert summary["formula_document_count"] == 1
    assert summary["low_confidence_document_count"] == 1
    assert summary["failed_segment_document_count"] == 1
    assert summary["average_page_coverage"] == 0.5
    assert summary["warning_counts"] == {
        "table_structure_review": 1,
        "figure_ocr_review": 1,
        "formula_review": 1,
        "low_confidence_elements": 1,
        "failed_segments": 1,
    }
    assert summary["risk_counts"] == {"low": 0, "medium": 1, "high": 0}
    assert summary["parser_profile_counts"] == {"legacy": 1}


def test_risk_level_ignores_exception_type_suffix_on_warning_codes() -> None:
    """``<code>: <例外の型名>`` の warning も code で判定する(#310)。"""
    from app.rag.ingestion_quality import _risk_level

    assert _risk_level(["docling_adapter_failed"]) == "medium"
    assert _risk_level(["docling_adapter_failed: ImportError"]) == "medium"
    assert _risk_level(["unknown_modality: detail"]) == "high"
