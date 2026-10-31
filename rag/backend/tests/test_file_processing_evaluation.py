"""ファイル処理 golden set 用メトリクスのテスト。"""

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast

from pytest import CaptureFixture

from app.rag import file_processing_evaluation as evaluation_module
from app.rag import file_processing_golden_cli
from app.rag.chunking import Chunk, ChunkMetadata, chunk_extraction
from app.rag.file_processing_evaluation import (
    REQUIRED_ADAPTER_SCHEMA_REMAP_SOURCE_KINDS,
    REQUIRED_FILE_PROCESSING_METRICS,
    REQUIRED_FILE_PROCESSING_SCENARIOS,
    REQUIRED_FILE_PROCESSING_SOURCE_KINDS,
    PageHitCase,
    TableQaResult,
    bbox_citation_coverage,
    bbox_coordinate_validity_coverage,
    build_file_processing_staging_plan,
    citation_traceability_coverage,
    element_lineage_coverage,
    extraction_page_coverage,
    failed_segment_rate,
    ingestion_quality_report_completeness,
    low_confidence_document_rate,
    page_hit_accuracy,
    parser_fallback_rate,
    quality_report_metadata_violation,
    run_file_processing_contract_checks,
    table_qa_accuracy,
    validate_file_processing_fixture_assets,
    validate_file_processing_manifest,
)
from app.rag.parser_adapter_routing import normalize_source_kind
from app.schemas.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    IngestionQualityReport,
    StructuredExtraction,
)
from app.schemas.search import RetrievedChunk

REPO_ROOT = Path(__file__).resolve().parents[2]
# monorepo（#71）では GitHub workflow は suite root（rag/ の親）の .github に置く。
SUITE_ROOT = REPO_ROOT.parent


def test_parser_fallback_rate_reads_quality_report_and_artifacts() -> None:
    """fallback_used は quality_report / parser_artifacts の双方から読める。"""
    extractions = [
        {"quality_report": {"fallback_used": True}},
        {"parser_artifacts": {"fallback_used": False}},
        {"parser_artifacts": {"fallback_used": True}},
        {},
    ]

    assert parser_fallback_rate(extractions) == 0.5


def test_quality_rates_read_quality_report_without_raw_text() -> None:
    """抽取品質 metric は quality_report の非機密 metadata から集計する。"""
    extractions: list[Mapping[str, object]] = [
        {
            "quality_report": {
                "page_coverage": 1.0,
                "low_confidence_count": 0,
                "failed_segment_count": 0,
                "quality_warnings": [],
            }
        },
        {
            "quality_report": {
                "page_coverage": 0.5,
                "low_confidence_count": 2,
                "failed_segment_count": 1,
                "quality_warnings": ["low_confidence_elements", "failed_segments"],
            }
        },
        {"quality_report": {"page_coverage": "0.75"}},
        {"parser_artifacts": {"failed_segment_count": 1}},
    ]

    assert extraction_page_coverage(extractions) == 0.75
    assert low_confidence_document_rate(extractions) == 0.25
    assert failed_segment_rate(extractions) == 0.5


def test_ingestion_quality_report_completeness_requires_contract_metadata() -> None:
    """quality_report は parser/構造件数/安全 warning の完全性を要求する。"""
    extraction = StructuredExtraction(
        raw_text="本文",
        elements=[
            DocumentElement(
                kind="text",
                text="本文",
                element_id="el-1",
                page_number=1,
                source_parser="local_text_structure",
            )
        ],
    )
    report = IngestionQualityReport(
        parser_profile="local_text_structure",
        parser_backend="local_partition",
        parser_version="local-v1",
        fallback_used=False,
        risk_level="low",
        page_count=1,
        page_coverage=1.0,
        table_count=0,
        figure_count=0,
        formula_count=0,
        element_count=1,
        low_confidence_count=0,
        failed_segment_count=0,
        long_document=False,
        quality_warnings=[],
    )
    payload = extraction.model_copy(update={"quality_report": report}).to_document_payload()
    missing_version = {
        **payload,
        "quality_report": {
            key: value
            for key, value in cast(Mapping[str, object], payload["quality_report"]).items()
            if key != "parser_version"
        },
    }
    sensitive = {
        **payload,
        "quality_report": {
            **cast(dict[str, object], payload["quality_report"]),
            "raw_text": "本文",
        },
    }
    underreported = {
        **payload,
        "quality_report": {
            **cast(dict[str, object], payload["quality_report"]),
            "element_count": 0,
        },
    }

    assert (
        quality_report_metadata_violation(
            payload,
            expected_parser_profile="local_text_structure",
            expected_parser_backend="local_partition",
            expected_parser_version="local-v1",
            expected_fallback_used=False,
        )
        is None
    )
    assert quality_report_metadata_violation(missing_version) == (
        "quality_report_fields_missing:parser_version"
    )
    assert quality_report_metadata_violation(sensitive) == "quality_report_sensitive_key"
    assert quality_report_metadata_violation(underreported) == "quality_element_count_mismatch"
    assert (
        ingestion_quality_report_completeness([payload, missing_version, sensitive, underreported])
        == 0.25
    )


def test_table_qa_accuracy_matches_normalized_expected_answer() -> None:
    """表 QA は空白や大小差を正規化して期待値の包含を評価する。"""
    results = [
        TableQaResult(
            case_id="ok",
            expected_answer="1000 円",
            actual_answer="交通費は1000円です。",
        ),
        TableQaResult(case_id="miss", expected_answer="部門長", actual_answer="承認者は経理です。"),
    ]

    assert table_qa_accuracy(results) == 0.5


def test_page_hit_accuracy_uses_citation_page_range() -> None:
    """citation metadata の page range が期待 page と重なれば hit とする。"""
    cases = [
        PageHitCase(case_id="hit", expected_document_id="doc-1", expected_pages=(2,)),
        PageHitCase(case_id="miss", expected_document_id="doc-2", expected_pages=(4,)),
        PageHitCase(case_id="api-shape", expected_document_id="doc-3", expected_pages=(5,)),
    ]
    retrieved: Mapping[str, Sequence[RetrievedChunk | Mapping[str, object]]] = {
        "hit": [
            RetrievedChunk(
                document_id="doc-1",
                chunk_id="doc-1:0",
                text="根拠",
                score=1.0,
                metadata={"page_start": 1, "page_end": 2},
            )
        ],
        "miss": [
            {"document_id": "doc-2", "metadata": {"page_start": 2, "page_end": 3}},
        ],
        "api-shape": [
            {
                "document_id": "doc-3",
                "chunk_id": "doc-3:0",
                "page_start": 5,
                "page_end": 5,
                "metadata": {},
            },
        ],
    }

    assert page_hit_accuracy(cases, retrieved) == 2 / 3


def test_citation_traceability_coverage_requires_page_and_lineage() -> None:
    """traceable citation は page と element/bbox/section のいずれかを持つ。"""
    citations: list[RetrievedChunk | Mapping[str, object]] = [
        RetrievedChunk(
            document_id="doc-1",
            chunk_id="doc-1:0",
            text="根拠",
            score=1.0,
            metadata={
                "page_start": 2,
                "page_end": 2,
                "element_ids": "el-1,el-2",
                "bbox": "[0.1, 0.2, 0.4, 0.5]",
            },
        ),
        {
            "document_id": "doc-2",
            "chunk_id": "doc-2:0",
            "page_start": 1,
            "page_end": 1,
            "section_path": "第1章/概要",
            "metadata": {},
        },
        {
            "document_id": "doc-3",
            "chunk_id": "doc-3:0",
            "metadata": {"element_ids": "el-3"},
        },
        {
            "document_id": "doc-4",
            "chunk_id": "doc-4:0",
            "metadata": {"page_start": 4},
        },
    ]

    assert citation_traceability_coverage(citations) == 0.5


def test_local_required_checks_require_resolvable_element_lineage() -> None:
    """local contract でも orphan element_ids を traceable と見なさない。"""
    extraction = StructuredExtraction.model_validate(
        {
            "raw_text": "本文",
            "elements": [
                {
                    "kind": "text",
                    "text": "本文",
                    "element_id": "el-1",
                    "page_number": 1,
                }
            ],
        }
    )
    orphan_chunk = Chunk(
        text="本文",
        index=0,
        start_offset=0,
        end_offset=2,
        metadata={
            "element_ids": "missing-el",
            "page_start": 1,
            "chunk_group_id": "g1",
        },
    )
    page_lineage_extraction = StructuredExtraction.model_validate(
        {
            "raw_text": "本文",
            "elements": [{"kind": "text", "text": "本文", "page_number": 1}],
            "pages": [{"page_number": 1, "element_ids": ["page-el"]}],
        }
    )
    page_chunk = Chunk(
        text="本文",
        index=1,
        start_offset=0,
        end_offset=2,
        metadata={
            "element_ids": "page-el",
            "page_start": 1,
            "chunk_group_id": "g2",
        },
    )
    json_lineage_chunk = Chunk(
        text="本文",
        index=2,
        start_offset=0,
        end_offset=2,
        metadata={
            "element_ids": '["page-el"]',
            "page_start": 1,
            "chunk_group_id": "g3",
        },
    )

    assert evaluation_module._check_element_lineage(extraction, [orphan_chunk]) == (
        "failure",
        "element_ids_unresolved",
    )
    assert evaluation_module._check_chunk_traceability(extraction, [orphan_chunk]) == (
        "failure",
        "lineage_metadata_missing",
    )
    assert evaluation_module._check_element_lineage(
        page_lineage_extraction,
        [page_chunk],
    ) == ("passed", "ok")
    assert evaluation_module._check_chunk_traceability(
        page_lineage_extraction,
        [page_chunk],
    ) == ("passed", "ok")
    assert evaluation_module._check_element_lineage(
        page_lineage_extraction,
        [json_lineage_chunk],
    ) == ("passed", "ok")


def test_code_and_equation_contract_requires_block_metadata() -> None:
    """code/formula block は content_kind だけでなく block 固有 metadata も要求する。"""
    extraction = StructuredExtraction.model_validate(
        {
            "elements": [
                {
                    "kind": "code",
                    "text": "select 1 from dual;",
                    "content_kind": "code",
                    "metadata": {"code_language": "sql"},
                },
                {
                    "kind": "equation",
                    "text": "E = mc^2",
                    "content_kind": "equation",
                    "metadata": {"equation_delimiter": "$$"},
                },
            ]
        }
    )
    code_without_language = Chunk(
        text="select 1 from dual;",
        index=0,
        start_offset=0,
        end_offset=19,
        metadata={"content_kind": "code"},
    )
    code_with_language = Chunk(
        text="select 1 from dual;",
        index=0,
        start_offset=0,
        end_offset=19,
        metadata={"content_kind": "code", "code_language": "sql"},
    )
    equation_without_delimiter = Chunk(
        text="E = mc^2",
        index=1,
        start_offset=20,
        end_offset=28,
        metadata={"content_kind": "equation"},
    )
    equation_with_delimiter = Chunk(
        text="E = mc^2",
        index=1,
        start_offset=20,
        end_offset=28,
        metadata={"content_kind": "equation", "equation_delimiter": "$$"},
    )

    assert evaluation_module._check_content_kind_present(
        extraction,
        [code_without_language],
        expected_kind="code",
        required_metadata_key="code_language",
    ) == ("failure", "code_code_language_missing")
    assert evaluation_module._check_content_kind_present(
        extraction,
        [code_with_language],
        expected_kind="code",
        required_metadata_key="code_language",
    ) == ("passed", "ok")
    assert evaluation_module._check_content_kind_present(
        extraction,
        [equation_without_delimiter],
        expected_kind="equation",
        required_metadata_key="equation_delimiter",
    ) == ("failure", "equation_equation_delimiter_missing")
    assert evaluation_module._check_content_kind_present(
        extraction,
        [equation_with_delimiter],
        expected_kind="equation",
        required_metadata_key="equation_delimiter",
    ) == ("passed", "ok")


def test_table_preserve_rows_requires_header_repeat_for_split_chunks() -> None:
    """長表 row-group chunk は各 part に表頭と行範囲 metadata を要求する。"""
    table_shape: ChunkMetadata = {
        "table_id": "tbl-expenses",
        "table_row_count": 3,
        "table_column_count": 2,
    }
    chunks = [
        Chunk(
            text="|項目|金額|\n|---|---|\n|交通費|1000円|",
            index=0,
            start_offset=0,
            end_offset=30,
            metadata={
                "content_kind": "table",
                "chunk_template": "table_preserve_rows",
                **table_shape,
                "chunk_group_id": "grp-table",
                "chunk_part_index": 1,
                "chunk_part_count": 2,
                "table_data_row_start": 1,
                "table_data_row_end": 1,
                "table_header_repeated": False,
            },
        ),
        Chunk(
            text="|項目|金額|\n|---|---|\n|宿泊費|2000円|",
            index=1,
            start_offset=31,
            end_offset=61,
            metadata={
                "content_kind": "table",
                "chunk_template": "table_preserve_rows",
                **table_shape,
                "chunk_group_id": "grp-table",
                "chunk_part_index": 2,
                "chunk_part_count": 2,
                "table_data_row_start": 2,
                "table_data_row_end": 2,
                "table_header_repeated": True,
            },
        ),
    ]

    assert evaluation_module._check_table_preserve_rows(chunks) == ("passed", "ok")


def test_chunk_block_integrity_requires_dependency_for_figure_caption() -> None:
    """figure と caption を同一 chunk にする場合は dependency edge が必要。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="figure", text="図", element_id="fig-1"),
            DocumentElement(
                kind="figure_caption",
                text="図1",
                element_id="fig-1-caption",
                parent_id="fig-1",
            ),
        ]
    )
    base_metadata: ChunkMetadata = {
        "chunk_group_id": "grp-figure",
        "content_kind": "figure",
        "element_kinds": "figure,figure_caption",
        "element_ids": "fig-1,fig-1-caption",
        "parent_element_ids": "fig-1",
    }
    missing_dependency = Chunk(
        text="図\n図1",
        index=0,
        start_offset=0,
        end_offset=4,
        metadata=base_metadata,
    )
    with_dependency = Chunk(
        text="図\n図1",
        index=0,
        start_offset=0,
        end_offset=4,
        metadata={
            **base_metadata,
            "dependency_edges": '[{"child_id":"fig-1-caption","parent_id":"fig-1"}]',
        },
    )

    assert evaluation_module._check_chunk_block_integrity(
        extraction,
        [missing_dependency],
    ) == ("failure", "figure_caption_dependency_missing")
    assert evaluation_module._check_chunk_block_integrity(
        extraction,
        [with_dependency],
    ) == ("passed", "ok")


def test_chunk_block_integrity_rejects_mixed_code_and_text_blocks() -> None:
    """code chunk に本文 block が混ざると構造境界違反にする。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="code", text="print(1)", element_id="code-1"),
            DocumentElement(kind="text", text="説明", element_id="text-1"),
        ]
    )
    mixed_chunk = Chunk(
        text="print(1)\n説明",
        index=0,
        start_offset=0,
        end_offset=12,
        metadata={
            "chunk_group_id": "grp-code",
            "content_kind": "code",
            "element_kinds": "code,text",
            "element_ids": "code-1,text-1",
            "code_language": "python",
        },
    )

    assert evaluation_module._check_chunk_block_integrity(
        extraction,
        [mixed_chunk],
    ) == ("failure", "mixed_code_block")


def test_reading_order_rejects_page_and_raw_offset_regressions() -> None:
    """reading order は element order だけでなく page/raw offset の逆行も検出する。"""
    page_regression = StructuredExtraction(
        elements=[
            DocumentElement(kind="text", text="2ページ目", page_number=2),
            DocumentElement(kind="text", text="1ページ目", page_number=1),
        ]
    )
    raw_offset_regression = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="後ろ",
                page_number=1,
                metadata={"raw_start": 20, "raw_end": 24},
            ),
            DocumentElement(
                kind="text",
                text="前",
                page_number=1,
                metadata={"raw_start": 5, "raw_end": 7},
            ),
        ]
    )
    valid = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="前",
                page_number=1,
                metadata={"raw_start": 0, "raw_end": 2},
            ),
            DocumentElement(
                kind="text",
                text="後ろ",
                page_number=1,
                metadata={"raw_start": 3, "raw_end": 6},
            ),
        ]
    )

    assert evaluation_module._check_reading_order(page_regression, []) == (
        "failure",
        "element_page_order_not_monotonic",
    )
    assert evaluation_module._check_reading_order(raw_offset_regression, []) == (
        "failure",
        "element_raw_offset_not_monotonic",
    )
    assert evaluation_module._check_reading_order(valid, []) == ("passed", "ok")


def test_reading_order_rejects_chunk_group_part_regression() -> None:
    """分割 chunk の part/row 順序が戻る場合は reading order 違反にする。"""
    extraction = StructuredExtraction(
        elements=[DocumentElement(kind="table", text="表", element_id="tbl-1", page_number=1)]
    )
    chunks = [
        Chunk(
            text="|項目|金額|\n|---|---|\n|宿泊費|2000円|",
            index=0,
            start_offset=31,
            end_offset=61,
            metadata={
                "content_kind": "table",
                "element_ids": "tbl-1",
                "chunk_group_id": "grp-table",
                "page_start": 1,
                "chunk_part_index": 2,
                "table_data_row_start": 2,
            },
        ),
        Chunk(
            text="|項目|金額|\n|---|---|\n|交通費|1000円|",
            index=1,
            start_offset=0,
            end_offset=30,
            metadata={
                "content_kind": "table",
                "element_ids": "tbl-1",
                "chunk_group_id": "grp-table",
                "page_start": 1,
                "chunk_part_index": 1,
                "table_data_row_start": 1,
            },
        ),
    ]

    assert evaluation_module._check_reading_order(extraction, chunks) == (
        "failure",
        "chunk_group_part_order_not_monotonic",
    )


def test_table_structure_fidelity_requires_cells_and_chunk_lineage() -> None:
    """table chunk は tables[] cells と table_id/shape metadata で結び直せる必要がある。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|交通費|1000円|",
                element_id="tbl-1",
                content_kind="table",
                metadata={"table_id": "tbl-1", "row_count": 2, "column_count": 2},
            )
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="tbl-1",
                cells=[
                    ExtractionTableCell(row=0, col=0, text="項目"),
                    ExtractionTableCell(row=0, col=1, text="金額"),
                    ExtractionTableCell(row=1, col=0, text="交通費"),
                    ExtractionTableCell(row=1, col=1, text="1000円"),
                ],
            )
        ],
    )
    chunk = Chunk(
        text="|項目|金額|\n|交通費|1000円|",
        index=0,
        start_offset=0,
        end_offset=20,
        metadata={
            "content_kind": "table",
            "element_ids": "tbl-1",
            "chunk_group_id": "grp-table",
            "table_id": "tbl-1",
            "table_row_count": 2,
            "table_column_count": 2,
        },
    )

    assert evaluation_module._check_table_structure_fidelity(extraction, [chunk]) == (
        "passed",
        "ok",
    )


def test_table_structure_fidelity_rejects_missing_cells_and_unresolved_table_id() -> None:
    """tables[] cells がない、または chunk の table_id が未解決なら構造忠実度違反。"""
    missing_cells = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|",
                element_id="tbl-1",
                content_kind="table",
                metadata={"table_id": "tbl-1"},
            )
        ]
    )
    unresolved = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|",
                element_id="tbl-1",
                content_kind="table",
                metadata={"table_id": "tbl-1"},
            )
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="tbl-1",
                cells=[
                    ExtractionTableCell(row=0, col=0, text="項目"),
                    ExtractionTableCell(row=0, col=1, text="金額"),
                ],
            )
        ],
    )
    chunk = Chunk(
        text="|項目|金額|",
        index=0,
        start_offset=0,
        end_offset=8,
        metadata={
            "content_kind": "table",
            "element_ids": "tbl-1",
            "chunk_group_id": "grp-table",
            "table_id": "missing-table",
            "table_row_count": 1,
            "table_column_count": 2,
        },
    )

    assert evaluation_module._check_table_structure_fidelity(missing_cells, [chunk]) == (
        "failure",
        "table_cells_missing",
    )
    assert evaluation_module._check_table_structure_fidelity(unresolved, [chunk]) == (
        "failure",
        "table_chunk_id_unresolved",
    )


def test_table_cell_lineage_requires_formula_refs_to_resolve_to_cells() -> None:
    """table/chunk の formula_cell_refs は cell metadata まで辿れる必要がある。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|交通費|1000円|",
                element_id="tbl-1",
                content_kind="table",
                metadata={"table_id": "tbl-1"},
            )
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="tbl-1",
                metadata={"formula_cell_refs": "B2"},
                cells=[
                    ExtractionTableCell(row=0, col=0, text="項目"),
                    ExtractionTableCell(row=0, col=1, text="金額"),
                    ExtractionTableCell(row=1, col=0, text="交通費"),
                    ExtractionTableCell(
                        row=1,
                        col=1,
                        text="1000円",
                        metadata={
                            "formula_cell_ref": "B2",
                            "formula": "SUM(B1:B1)",
                            "equation_format": "excel_formula",
                        },
                    ),
                ],
            )
        ],
    )
    chunk = Chunk(
        text="|項目|金額|\n|交通費|1000円|",
        index=0,
        start_offset=0,
        end_offset=20,
        metadata={
            "content_kind": "table",
            "table_id": "tbl-1",
            "formula_cell_refs": "B2",
        },
    )
    unresolved = extraction.model_copy(
        update={
            "tables": [
                extraction.tables[0].model_copy(
                    update={
                        "cells": [
                            cell.model_copy(update={"metadata": {}})
                            for cell in extraction.tables[0].cells
                        ]
                    }
                )
            ]
        }
    )
    incomplete = extraction.model_copy(
        update={
            "tables": [
                extraction.tables[0].model_copy(
                    update={
                        "metadata": {},
                        "cells": [
                            (
                                cell.model_copy(update={"metadata": {"formula_cell_ref": "B2"}})
                                if cell.row == 1 and cell.col == 1
                                else cell
                            )
                            for cell in extraction.tables[0].cells
                        ],
                    }
                )
            ]
        }
    )

    assert evaluation_module._check_table_cell_lineage(extraction, [chunk]) == (
        "passed",
        "ok",
    )
    assert evaluation_module._check_table_cell_lineage(unresolved, [chunk]) == (
        "failure",
        "table_cell_metadata_missing",
    )
    assert evaluation_module._check_table_cell_lineage(incomplete, []) == (
        "failure",
        "table_cell_formula_detail_missing",
    )


def test_table_cell_lineage_accepts_generic_cell_refs() -> None:
    """普通の表セル ref も formula 以外の cell-level lineage として検証する。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|交通費|1000円|",
                element_id="tbl-1",
                content_kind="table",
                metadata={"table_id": "tbl-1"},
            )
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="tbl-1",
                cells=[
                    ExtractionTableCell(row=0, col=0, text="項目", metadata={"cell_ref": "A1"}),
                    ExtractionTableCell(row=0, col=1, text="金額", metadata={"cell_ref": "B1"}),
                    ExtractionTableCell(
                        row=1,
                        col=0,
                        text="交通費",
                        metadata={"cell_ref": "A2"},
                    ),
                    ExtractionTableCell(
                        row=1,
                        col=1,
                        text="1000円",
                        metadata={"cell_ref": "B2"},
                    ),
                ],
            )
        ],
    )
    chunk = Chunk(
        text="|項目|金額|\n|交通費|1000円|",
        index=0,
        start_offset=0,
        end_offset=20,
        metadata={
            "content_kind": "table",
            "table_id": "tbl-1",
            "table_cell_refs": "A2\nB2",
        },
    )
    unresolved = extraction.model_copy(
        update={
            "tables": [
                extraction.tables[0].model_copy(
                    update={
                        "cells": [
                            cell.model_copy(update={"metadata": {}})
                            for cell in extraction.tables[0].cells
                        ]
                    }
                )
            ]
        }
    )

    assert evaluation_module._check_table_cell_lineage(extraction, [chunk]) == (
        "passed",
        "ok",
    )
    assert evaluation_module._check_table_cell_lineage(unresolved, [chunk]) == (
        "failure",
        "table_cell_metadata_missing",
    )


def test_table_cell_lineage_accepts_structured_adapter_cell_refs() -> None:
    """adapter 由来の object / JSON 形式 cell refs も cell metadata に解決する。"""
    extraction = StructuredExtraction(
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                metadata={"formula_cell_refs": '[{"formula_cell_ref":"B2"}]'},
                cells=[
                    ExtractionTableCell(
                        row=1,
                        col=1,
                        text="1000円",
                        metadata={
                            "formula_cell_ref": "B2",
                            "formula": "SUM(B1:B1)",
                            "equation_format": "excel_formula",
                        },
                    )
                ],
            )
        ],
    )
    chunk = Chunk(
        text="1000円",
        index=0,
        start_offset=0,
        end_offset=4,
        metadata={
            "content_kind": "table",
            "formula_cell_refs": '[{"formula_cell_ref":"B2"}]',
        },
    )

    assert evaluation_module._check_table_cell_lineage(extraction, [chunk]) == (
        "passed",
        "ok",
    )


def test_table_row_tree_fidelity_recomputes_key_value_row_hashes() -> None:
    """table row-tree は chunk text から key-value row block として再検証できる。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|---|---|\n|交通費|1000円|\n|宿泊費|2000円|",
                element_id="tbl-1",
                page_number=1,
                metadata={"table_id": "tbl-1", "row_count": 3, "column_count": 2},
            )
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="tbl-1",
                page_number=1,
                cells=[
                    ExtractionTableCell(row=0, col=0, text="項目"),
                    ExtractionTableCell(row=0, col=1, text="金額"),
                    ExtractionTableCell(row=1, col=0, text="交通費"),
                    ExtractionTableCell(row=1, col=1, text="1000円"),
                    ExtractionTableCell(row=2, col=0, text="宿泊費"),
                    ExtractionTableCell(row=2, col=1, text="2000円"),
                ],
            )
        ],
    )
    chunks = chunk_extraction(extraction, chunk_size=32, overlap=0)
    table_chunks = [chunk for chunk in chunks if chunk.metadata["content_kind"] == "table"]
    damaged = table_chunks[0]
    damaged = Chunk(
        text=damaged.text,
        index=damaged.index,
        start_offset=damaged.start_offset,
        end_offset=damaged.end_offset,
        metadata={**damaged.metadata, "table_row_tree_kv_sha256": "0" * 64},
    )

    assert evaluation_module._check_table_row_tree_fidelity(extraction, table_chunks) == (
        "passed",
        "ok",
    )
    assert evaluation_module._check_table_row_tree_fidelity(extraction, [damaged]) == (
        "failure",
        "table_row_tree_kv_hash_mismatch",
    )


def test_visual_chunk_metadata_requires_preview_and_citation_lineage() -> None:
    """visual chunk には preview/citation 用の最小 metadata が揃っている必要がある。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="本文",
                element_id="el-1",
                source_parser="local_text_structure",
                page_number=1,
            )
        ]
    )
    chunk = Chunk(
        text="本文",
        index=0,
        start_offset=0,
        end_offset=2,
        metadata={
            "content_kind": "text",
            "source_parser": "local_text_structure",
            "chunk_template": "markdown_by_heading",
            "chunk_group_id": "grp-1",
            "chunk_group_kind": "element_group",
            "chunk_part_index": 1,
            "chunk_part_count": 1,
            "page_start": 1,
            "page_end": 1,
            "element_ids": "el-1",
        },
    )

    assert evaluation_module._check_visual_chunk_metadata(extraction, [chunk]) == (
        "passed",
        "ok",
    )


def test_visual_chunk_metadata_rejects_missing_source_parser_and_page_range() -> None:
    """source_parser/page range が欠けた chunk は citation-to-preview に出せない。"""
    extraction = StructuredExtraction(
        elements=[DocumentElement(kind="text", text="本文", element_id="el-1", page_number=1)]
    )
    missing_source_parser = Chunk(
        text="本文",
        index=0,
        start_offset=0,
        end_offset=2,
        metadata={
            "content_kind": "text",
            "chunk_template": "markdown_by_heading",
            "chunk_group_id": "grp-1",
            "chunk_group_kind": "element_group",
            "chunk_part_index": 1,
            "chunk_part_count": 1,
            "page_start": 1,
            "page_end": 1,
            "element_ids": "el-1",
        },
    )
    missing_page = Chunk(
        text="本文",
        index=0,
        start_offset=0,
        end_offset=2,
        metadata={
            "content_kind": "text",
            "source_parser": "local_text_structure",
            "chunk_template": "markdown_by_heading",
            "chunk_group_id": "grp-1",
            "chunk_group_kind": "element_group",
            "chunk_part_index": 1,
            "chunk_part_count": 1,
            "element_ids": "el-1",
        },
    )

    assert evaluation_module._check_visual_chunk_metadata(
        extraction,
        [missing_source_parser],
    ) == ("failure", "visual_source_parser_missing")
    assert evaluation_module._check_visual_chunk_metadata(extraction, [missing_page]) == (
        "failure",
        "visual_page_range_missing",
    )


def test_chunk_size_compliance_requires_hash_target_and_bounded_chunks() -> None:
    """chunk size compliance は空/過大 chunk と hash metadata 退化を検出する。"""
    text = "本文"
    valid = Chunk(
        text=text,
        index=0,
        start_offset=0,
        end_offset=len(text),
        metadata={
            "content_kind": "text",
            "text_chars": len(text),
            "text_sha256": sha256(text.encode()).hexdigest(),
            "chunk_size_target": 80,
            "chunk_size_limit": 80,
            "chunk_size_compliance": "within_limit",
        },
    )
    oversized = Chunk(
        text="x" * 81,
        index=1,
        start_offset=0,
        end_offset=81,
        metadata={
            "content_kind": "text",
            "text_chars": 81,
            "text_sha256": sha256(("x" * 81).encode()).hexdigest(),
            "chunk_size_target": 80,
            "chunk_size_limit": 80,
            "chunk_size_compliance": "within_limit",
        },
    )
    justified_table = Chunk(
        text="|長い列|\n|" + "x" * 120 + "|",
        index=2,
        start_offset=0,
        end_offset=130,
        metadata={
            "content_kind": "table",
            "text_chars": len("|長い列|\n|" + "x" * 120 + "|"),
            "text_sha256": sha256(("|長い列|\n|" + "x" * 120 + "|").encode()).hexdigest(),
            "chunk_size_target": 80,
            "chunk_size_limit": 80,
            "chunk_size_compliance": "overflow_justified",
            "chunk_size_overflow_reason": "atomic_block",
        },
    )
    delimiter_chunk = Chunk(
        text="長い本文" * 60,
        index=3,
        start_offset=0,
        end_offset=len("長い本文" * 60),
        metadata={
            "content_kind": "text",
            "chunk_fixed_delimiter": True,
            "text_chars": len("長い本文" * 60),
            "text_sha256": sha256(("長い本文" * 60).encode()).hexdigest(),
        },
    )

    assert evaluation_module._check_chunk_size_compliance(
        [valid, justified_table, delimiter_chunk]
    ) == ("passed", "ok")
    assert evaluation_module._check_chunk_size_compliance([oversized]) == (
        "failure",
        "chunk_size_limit_exceeded",
    )


def test_chunk_contextual_coherence_requires_split_parent_context() -> None:
    """split chunk は parent/section context を metadata と group で辿れる必要がある。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="部門長が承認します。",
                element_id="el-1",
                page_number=1,
                section_path=["経費申請", "承認"],
            ),
            DocumentElement(
                kind="text",
                text="経理部が確認します。",
                element_id="el-2",
                page_number=1,
                section_path=["経費申請", "承認"],
            ),
        ]
    )
    base_metadata: ChunkMetadata = {
        "content_kind": "text",
        "chunk_group_id": "grp-section",
        "chunk_group_kind": "section",
        "chunk_part_count": 2,
        "section_title": "承認",
        "section_path": "経費申請 > 承認",
    }
    first = Chunk(
        text="部門長が承認します。",
        index=0,
        start_offset=0,
        end_offset=10,
        metadata={**base_metadata, "chunk_part_index": 1, "element_ids": "el-1"},
    )
    second = Chunk(
        text="経理部が確認します。",
        index=1,
        start_offset=10,
        end_offset=20,
        metadata={**base_metadata, "chunk_part_index": 2, "element_ids": "el-2"},
    )
    missing_context = Chunk(
        text="経理部が確認します。",
        index=1,
        start_offset=10,
        end_offset=20,
        metadata={
            "content_kind": "text",
            "chunk_group_id": "grp-section",
            "chunk_group_kind": "section",
            "chunk_part_count": 2,
            "chunk_part_index": 2,
            "element_ids": "el-2",
        },
    )

    assert evaluation_module._check_chunk_contextual_coherence(
        extraction,
        [first, second],
    ) == ("passed", "ok")
    assert evaluation_module._check_chunk_contextual_coherence(
        extraction,
        [missing_context],
    ) == ("failure", "context_section_path_missing")


def test_chunk_contextual_coherence_requires_table_continuation_context() -> None:
    """分割表の continuation chunk は表頭と行範囲を保持する。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|A|100円|",
                element_id="tbl-1",
                page_number=1,
            )
        ]
    )
    first = Chunk(
        text="|項目|金額|\n|---|---|\n|A|100円|",
        index=0,
        start_offset=0,
        end_offset=20,
        metadata={
            "content_kind": "table",
            "chunk_group_id": "grp-table",
            "chunk_group_kind": "table",
            "chunk_part_count": 2,
            "chunk_part_index": 1,
            "element_ids": "tbl-1",
            "table_id": "tbl-1",
            "table_data_row_start": 1,
            "table_data_row_end": 1,
            "table_header_repeated": False,
        },
    )
    missing_header = Chunk(
        text="|A|100円|",
        index=1,
        start_offset=20,
        end_offset=30,
        metadata={
            "content_kind": "table",
            "chunk_group_id": "grp-table",
            "chunk_group_kind": "table",
            "chunk_part_count": 2,
            "chunk_part_index": 2,
            "element_ids": "tbl-1",
            "table_id": "tbl-1",
            "table_data_row_start": 2,
            "table_data_row_end": 2,
            "table_header_repeated": False,
        },
    )

    assert evaluation_module._check_chunk_contextual_coherence(
        extraction,
        [first, missing_header],
    ) == ("failure", "context_table_header_repeat_missing")


def test_structural_section_coverage_requires_expected_sections() -> None:
    """expected_sections は extraction/chunk section lineage で全て覆う必要がある。"""
    case: Mapping[str, object] = {
        "expected_sections": [
            "検索運用マニュアル > インデックス確認",
            "検索運用マニュアル > 引用確認",
        ]
    }
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="INDEXED状態を確認します。",
                element_id="el-1",
                page_number=1,
                section_path=["検索運用マニュアル", "インデックス確認"],
            ),
            DocumentElement(
                kind="text",
                text="根拠を確認します。",
                element_id="el-2",
                page_number=1,
                section_path=["検索運用マニュアル", "引用確認"],
            ),
        ]
    )
    chunks = [
        Chunk(
            text="INDEXED状態を確認します。",
            index=0,
            start_offset=0,
            end_offset=14,
            metadata={
                "section_path": "検索運用マニュアル > インデックス確認",
                "element_ids": "el-1",
            },
        ),
        Chunk(
            text="根拠を確認します。",
            index=1,
            start_offset=15,
            end_offset=24,
            metadata={
                "section_path": "検索運用マニュアル > 引用確認",
                "element_ids": "el-2",
            },
        ),
    ]

    assert evaluation_module._check_structural_section_coverage(case, extraction, chunks) == (
        "passed",
        "ok",
    )
    assert evaluation_module._check_structural_section_coverage(
        {"expected_sections": ["検索運用マニュアル > 未収録"]},
        extraction,
        chunks,
    ) == ("failure", "missing_sections:検索運用マニュアル > 未収録".casefold())


def test_parser_warning_taxonomy_accepts_known_unsupported_and_segment_codes() -> None:
    """unsupported / segment failure は既知の安定 warning code で表現する。"""

    class SegmentFailure:
        error_code = "office_segment_parse_failed"

    assert evaluation_module._check_parser_warning_taxonomy(
        ["unsupported_tiff_image"],
        unsupported_reason="tiff_image_not_supported",
        segment_failures=[],
    ) == ("passed", "ok")
    assert evaluation_module._check_parser_warning_taxonomy(
        [],
        unsupported_reason=None,
        segment_failures=[SegmentFailure()],
    ) == ("passed", "ok")


def test_parser_warning_taxonomy_rejects_free_text_and_unknown_codes() -> None:
    """raw exception 風の warning や未知 code は非機密 taxonomy 違反にする。"""
    assert evaluation_module._check_parser_warning_taxonomy(
        ["ValueError: /tmp/source.pdf failed"],
        unsupported_reason=None,
        segment_failures=[],
    ) == ("failure", "unsafe_warning_code")
    assert evaluation_module._check_parser_warning_taxonomy(
        ["some_new_warning"],
        unsupported_reason=None,
        segment_failures=[],
    ) == ("failure", "unknown_warning_code")
    assert evaluation_module._check_parser_warning_taxonomy(
        ["unsupported_outlook_msg"],
        unsupported_reason="tiff_image_not_supported",
        segment_failures=[],
    ) == ("failure", "unsupported_warning_missing")


def test_table_preserve_rows_requires_table_lineage_and_shape_metadata() -> None:
    """表 chunk は citation で使う table id と表サイズ metadata を持つ。"""
    missing_table_id = [
        Chunk(
            text="|項目|金額|\n|---|---|\n|交通費|1000円|",
            index=0,
            start_offset=0,
            end_offset=30,
            metadata={
                "content_kind": "table",
                "chunk_template": "table_preserve_rows",
                "table_row_count": 2,
                "table_column_count": 2,
            },
        )
    ]
    missing_shape = [
        Chunk(
            text="|項目|金額|\n|---|---|\n|交通費|1000円|",
            index=0,
            start_offset=0,
            end_offset=30,
            metadata={
                "content_kind": "table",
                "chunk_template": "table_preserve_rows",
                "table_id": "tbl-expenses",
                "table_row_count": 2,
            },
        )
    ]

    assert evaluation_module._check_table_preserve_rows(missing_table_id) == (
        "failure",
        "table_lineage_metadata_missing",
    )
    assert evaluation_module._check_table_preserve_rows(missing_shape) == (
        "failure",
        "table_shape_metadata_missing",
    )


def test_table_preserve_rows_rejects_split_chunk_without_header_context() -> None:
    """表 chunk が存在するだけでは長表 row-group の contract を満たさない。"""
    table_shape: ChunkMetadata = {
        "table_id": "tbl-expenses",
        "table_row_count": 3,
        "table_column_count": 2,
    }
    first_chunk = Chunk(
        text="|項目|金額|\n|---|---|\n|交通費|1000円|",
        index=0,
        start_offset=0,
        end_offset=30,
        metadata={
            "content_kind": "table",
            "chunk_template": "table_preserve_rows",
            **table_shape,
            "chunk_part_index": 1,
            "chunk_part_count": 2,
            "table_data_row_start": 1,
            "table_data_row_end": 1,
            "table_header_repeated": False,
        },
    )
    missing_header = [
        first_chunk,
        Chunk(
            text="|宿泊費|2000円|",
            index=1,
            start_offset=31,
            end_offset=45,
            metadata={
                "content_kind": "table",
                "chunk_template": "table_preserve_rows",
                **table_shape,
                "chunk_part_index": 2,
                "chunk_part_count": 2,
                "table_data_row_start": 2,
                "table_data_row_end": 2,
                "table_header_repeated": True,
            },
        ),
    ]
    missing_row_range = [
        first_chunk,
        Chunk(
            text="|項目|金額|\n|---|---|\n|宿泊費|2000円|",
            index=1,
            start_offset=31,
            end_offset=61,
            metadata={
                "content_kind": "table",
                "chunk_template": "table_preserve_rows",
                **table_shape,
                "chunk_part_index": 2,
                "chunk_part_count": 2,
                "table_header_repeated": True,
            },
        ),
    ]

    assert evaluation_module._check_table_preserve_rows(missing_header) == (
        "failure",
        "table_header_not_repeated",
    )
    assert evaluation_module._check_table_preserve_rows(missing_row_range) == (
        "failure",
        "table_row_group_metadata_missing",
    )


def test_bbox_and_element_lineage_coverage_accept_api_and_metadata_shapes() -> None:
    """bbox / element lineage は RetrievedChunk と API chunk view の両方から読む。"""
    citations: list[RetrievedChunk | Mapping[str, object]] = [
        RetrievedChunk(
            document_id="doc-1",
            chunk_id="doc-1:0",
            text="根拠",
            score=1.0,
            metadata={"bbox": "[0, 0, 50, 10]", "element_ids": '["el-1"]'},
        ),
        {
            "document_id": "doc-2",
            "chunk_id": "doc-2:0",
            "bbox": [0.1, 0.1, 0.3, 0.2],
            "element_ids": ["el-2", "el-3"],
            "metadata": {},
        },
        {
            "document_id": "doc-3",
            "chunk_id": "doc-3:0",
            "bbox": [0, 0, 0, 0],
            "element_ids": [],
            "metadata": {},
        },
        {
            "document_id": "doc-4",
            "chunk_id": "doc-4:0",
            "metadata": {"bbox": "not-json", "element_ids": ""},
        },
    ]

    assert bbox_citation_coverage(citations) == 0.5
    assert element_lineage_coverage(citations) == 0.5


def test_bbox_coordinate_validity_coverage_requires_unit_mode_and_bounds() -> None:
    """bbox overlay 用には座標値だけでなく mode/unit/range も必要。"""
    citations: list[RetrievedChunk | Mapping[str, object]] = [
        RetrievedChunk(
            document_id="doc-1",
            chunk_id="doc-1:0",
            text="根拠",
            score=1.0,
            metadata={
                "bbox": "[0.1, 0.2, 0.4, 0.5]",
                "bbox_coordinate_mode": "xyxy",
                "bbox_unit": "ratio",
            },
        ),
        {
            "document_id": "doc-2",
            "chunk_id": "doc-2:0",
            "bbox": [10, 20, 30, 40],
            "metadata": {
                "bbox_coordinate_mode": "xywh",
                "bbox_coordinate_unit": "percent",
            },
        },
        {
            "document_id": "doc-3",
            "chunk_id": "doc-3:0",
            "bbox": [10, 20, 30, 40],
            "metadata": {
                "bbox_coordinate_mode": "xywh",
                "bbox_unit": "absolute",
                "page_width": 200,
                "page_height": 100,
            },
        },
        {
            "document_id": "doc-4",
            "chunk_id": "doc-4:0",
            "bbox": [0.1, 0.2, 0.4, 0.5],
            "metadata": {"bbox_unit": "ratio"},
        },
        {
            "document_id": "doc-5",
            "chunk_id": "doc-5:0",
            "bbox": [10, 20, 30, 40],
            "metadata": {
                "bbox_coordinate_mode": "xywh",
                "bbox_unit": "absolute",
            },
        },
    ]

    assert bbox_coordinate_validity_coverage(citations) == 3 / 5


def test_preview_addressability_validates_extraction_bbox_targets() -> None:
    """preview jump は element/table cell/asset bbox を page context 付きで検証する。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="請求書",
                element_id="el-1",
                page_number=1,
                bbox=[10, 20, 80, 40],
                metadata={"bbox_unit": "absolute"},
            )
        ],
        pages=[ExtractionPage(page_number=1, width=200, height=100)],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                page_number=1,
                cells=[
                    ExtractionTableCell(
                        row=0,
                        col=0,
                        text="金額",
                        bbox=[0.1, 0.2, 0.3, 0.4],
                        metadata={"bbox_unit": "ratio"},
                    )
                ],
            )
        ],
        assets=[
            ExtractionAsset(
                asset_id="fig-1",
                page_number=1,
                bbox=[10, 10, 40, 40],
                metadata={"bbox_unit": "absolute"},
            )
        ],
    )
    missing_page = extraction.model_copy(
        update={
            "assets": [
                ExtractionAsset(
                    asset_id="fig-1",
                    bbox=[0.1, 0.2, 0.3, 0.4],
                    metadata={"bbox_unit": "ratio"},
                )
            ]
        }
    )
    missing_page_size = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="請求書",
                element_id="el-1",
                page_number=1,
                bbox=[10, 20, 180, 40],
                metadata={"bbox_unit": "absolute"},
            )
        ],
        pages=[ExtractionPage(page_number=1)],
    )

    assert evaluation_module._check_preview_addressability(extraction, []) == (
        "passed",
        "ok",
    )
    assert evaluation_module._check_preview_addressability(missing_page, []) == (
        "failure",
        "preview_bbox_page_missing",
    )
    assert evaluation_module._check_preview_addressability(missing_page_size, []) == (
        "failure",
        "preview_bbox_absolute_page_size_missing",
    )
    rotated = extraction.model_copy(
        update={"pages": [ExtractionPage(page_number=1, width=200, height=100, rotation=90)]}
    )
    rotation_mismatch = extraction.model_copy(
        update={
            "elements": [
                DocumentElement(
                    kind="text",
                    text="請求書",
                    element_id="el-1",
                    page_number=1,
                    bbox=[10, 20, 80, 40],
                    metadata={"bbox_unit": "absolute", "page_rotation": 0},
                )
            ],
            "pages": [ExtractionPage(page_number=1, width=200, height=100, rotation=90)],
        }
    )
    invalid_rotation = extraction.model_copy(
        update={"pages": [ExtractionPage(page_number=1, width=200, height=100, rotation=45)]}
    )
    assert evaluation_module._check_preview_addressability(rotated, []) == (
        "passed",
        "ok",
    )
    assert evaluation_module._check_preview_addressability(rotation_mismatch, []) == (
        "failure",
        "preview_bbox_page_rotation_mismatch",
    )
    assert evaluation_module._check_preview_addressability(invalid_rotation, []) == (
        "failure",
        "preview_bbox_page_rotation_invalid",
    )


def test_file_processing_golden_manifest_tracks_traceability_metrics() -> None:
    """file-processing golden set は機械検証できる契約を明示する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert validate_file_processing_manifest(manifest) == ()
    assert validate_file_processing_fixture_assets(manifest, manifest_path=manifest_path) == ()
    assert set(manifest["metrics"]) >= REQUIRED_FILE_PROCESSING_METRICS
    assert set(manifest["thresholds"]) >= REQUIRED_FILE_PROCESSING_METRICS
    assert manifest["thresholds"]["table_qa_accuracy"] == {"min": 1.0}
    assert manifest["thresholds"]["bbox_coordinate_validity_coverage"] == {"min": 0.9}
    assert manifest["thresholds"]["chunk_block_integrity"] == {"min": 1.0}
    assert manifest["thresholds"]["reading_order_consistency"] == {"min": 1.0}
    assert manifest["thresholds"]["structural_section_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["dependency_context_recall"] == {"min": 1.0}
    assert manifest["thresholds"]["table_structure_fidelity"] == {"min": 1.0}
    assert manifest["thresholds"]["table_cell_lineage_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["table_row_tree_fidelity"] == {"min": 1.0}
    assert manifest["thresholds"]["visual_chunk_metadata_completeness"] == {"min": 1.0}
    assert manifest["thresholds"]["chunk_size_compliance"] == {"min": 1.0}
    assert manifest["thresholds"]["chunk_contextual_coherence"] == {"min": 1.0}
    assert manifest["thresholds"]["cross_page_table_continuity_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["ingestion_quality_report_completeness"] == {"min": 1.0}
    assert manifest["thresholds"]["parser_warning_taxonomy_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["parser_routing_accuracy"] == {"min": 1.0}
    assert manifest["thresholds"]["source_kind_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["backend_source_kind_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["adapter_contract_coverage"] == {"min": 1.0}
    assert manifest["thresholds"]["parser_fallback_rate"] == {"max": 0.2}
    assert manifest["staging_policy"]["required_runtime_checks"] == [
        "extraction_artifact_cache_roundtrip"
    ]
    assert {case["scenario"] for case in manifest["cases"]} >= REQUIRED_FILE_PROCESSING_SCENARIOS
    manifest_source_kinds = {normalize_source_kind(case["modality"]) for case in manifest["cases"]}
    assert manifest_source_kinds >= REQUIRED_FILE_PROCESSING_SOURCE_KINDS
    remap_source_kinds = {
        normalize_source_kind(case["modality"])
        for case in manifest["cases"]
        if case.get("adapter_schema_remap") is True
    }
    assert remap_source_kinds >= REQUIRED_ADAPTER_SCHEMA_REMAP_SOURCE_KINDS
    fixture_root = (manifest_path.parent / manifest["fixture_root"]).resolve()
    duplicate_case = next(
        case for case in manifest["cases"] if case["id"] == "duplicate-file-canonical-kb"
    )
    assert (
        sha256((fixture_root / duplicate_case["fixture"]).read_bytes()).digest()
        == sha256((fixture_root / duplicate_case["duplicate_fixture"]).read_bytes()).digest()
    )
    assert any(case["id"] == "long-table-row-groups" for case in manifest["cases"])
    assert any(case["id"] == "long-table-tsv-row-groups" for case in manifest["cases"])
    assert any(case["id"] == "cross-page-table-continuity" for case in manifest["cases"])
    assert any(case["id"] == "two-column-pdf-reading-order" for case in manifest["cases"])
    assert any(case["id"] == "image-ocr-bbox" for case in manifest["cases"])
    assert any(case["id"] == "tiff-image-unsupported" for case in manifest["cases"])
    assert any(case["id"] == "markdown-code-formula-blocks" for case in manifest["cases"])
    assert any(case["id"] == "corrupted-file-partial-failure" for case in manifest["cases"])
    assert any(case["id"] == "legacy-office-unsupported" for case in manifest["cases"])


def test_file_processing_golden_manifest_requires_markdown_code_formula_case() -> None:
    """Markdown の code/formula block 保持は必須 golden scenario として扱う。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases"] = [
        case for case in manifest["cases"] if case.get("scenario") != "markdown_code_formula_blocks"
    ]

    errors = validate_file_processing_manifest(manifest)

    assert "missing_scenarios:markdown_code_formula_blocks" in errors


def test_file_processing_manifest_requires_scenario_specific_checks() -> None:
    """scenario 名だけでなく、各 scenario の重要 check も必須にする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    markdown_case = next(
        case for case in manifest["cases"] if case["scenario"] == "markdown_code_formula_blocks"
    )
    markdown_case["required_checks"] = ["heading_structure", "element_lineage"]

    errors = validate_file_processing_manifest(manifest)

    assert (
        "case[markdown-code-formula-blocks]:missing_required_checks:"
        "chunk_block_integrity,chunk_contextual_coherence,chunk_size_compliance,"
        "code_block,equation_block,quality_report_metadata,reading_order,"
        "visual_chunk_metadata"
    ) in errors


def test_file_processing_manifest_requires_table_structure_for_table_scenarios() -> None:
    """table scenario は tables[]/cells[] lineage の検証を必須にする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    table_case = next(case for case in manifest["cases"] if case["id"] == "long-table-row-groups")
    table_case["required_checks"] = [
        check for check in table_case["required_checks"] if check != "table_structure_fidelity"
    ]

    errors = validate_file_processing_manifest(manifest)

    assert (
        "case[long-table-row-groups]:missing_required_checks:table_structure_fidelity"
    ) in errors


def test_file_processing_manifest_requires_adapter_schema_remap_coverage() -> None:
    """strict adapter smoke 用 fixture 宣言を source kind ごとに必須にする。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        if normalize_source_kind(case.get("modality")) == "email":
            case.pop("adapter_schema_remap", None)

    errors = validate_file_processing_manifest(manifest)

    assert "adapter_schema_remap:missing_source_kinds:email" in errors


def test_file_processing_manifest_rejects_negative_adapter_schema_remap_case() -> None:
    """unsupported/corrupted case を adapter schema-remap 証跡に混ぜない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tiff_case = next(case for case in manifest["cases"] if case["id"] == "tiff-image-unsupported")
    tiff_case["adapter_schema_remap"] = True

    errors = validate_file_processing_manifest(manifest)

    assert ("case[tiff-image-unsupported]:adapter_schema_remap_not_positive_fixture") in errors


def test_file_processing_contract_runner_executes_local_parser_checks() -> None:
    """同梱 fixture は local parser/chunker contract と staging pending を分離する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    report = run_file_processing_contract_checks(manifest, manifest_path=manifest_path)

    assert report.passed is True
    assert report.case_count == 16
    assert report.failure_count == 0
    assert report.pending_staging_check_count > 0

    by_id = {result.case_id: result for result in report.case_results}
    assert {
        "table_preserve_rows",
        "table_qa_accuracy",
        "element_lineage",
        "chunk_block_integrity",
        "reading_order",
        "table_structure_fidelity",
        "table_cell_lineage",
        "table_row_tree_fidelity",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "quality_report_metadata",
    } <= set(by_id["long-table-row-groups"].passed_checks)
    assert {
        "table_preserve_rows",
        "table_qa_accuracy",
        "element_lineage",
        "chunk_block_integrity",
        "reading_order",
        "table_structure_fidelity",
        "table_cell_lineage",
        "table_row_tree_fidelity",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "quality_report_metadata",
    } <= set(by_id["long-table-tsv-row-groups"].passed_checks)
    assert {
        "table_preserve_rows",
        "element_lineage",
        "chunk_block_integrity",
        "reading_order",
        "table_row_tree_fidelity",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "cross_page_table_continuity",
        "quality_report_metadata",
    } <= set(by_id["cross-page-table-continuity"].passed_checks)
    assert {
        "sheet_segment",
        "table_preserve_rows",
        "element_lineage",
        "chunk_block_integrity",
        "reading_order",
        "table_structure_fidelity",
        "table_cell_lineage",
        "table_row_tree_fidelity",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "quality_report_metadata",
    } <= set(by_id["japanese-xlsx-sheets"].passed_checks)
    assert {
        "heading_structure",
        "section_path",
        "structural_section_coverage",
        "citation_traceability",
        "dependency_lineage",
        "chunk_block_integrity",
        "reading_order",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "quality_report_metadata",
    } <= set(by_id["html-semantic-blocks"].passed_checks)
    assert {
        "code_block",
        "equation_block",
        "element_lineage",
        "chunk_block_integrity",
        "reading_order",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "quality_report_metadata",
    } <= set(by_id["markdown-code-formula-blocks"].passed_checks)
    assert {
        "email_headers",
        "thread_body",
        "attachment_metadata",
        "chunk_block_integrity",
        "reading_order",
        "visual_chunk_metadata",
        "chunk_size_compliance",
        "chunk_contextual_coherence",
        "quality_report_metadata",
    } <= set(by_id["email-thread-headers"].passed_checks)
    assert "canonical_alias" in by_id["duplicate-file-canonical-kb"].passed_checks
    assert any(
        check.startswith("searchable_canonical:")
        for check in by_id["duplicate-file-canonical-kb"].pending_checks
    )
    assert {
        "failed_segment_status",
        "parser_warning_taxonomy",
        "safe_error",
    } <= set(by_id["corrupted-file-partial-failure"].passed_checks)
    assert {
        "expected_unsupported_reason",
        "expected_warning",
        "parser_warning_taxonomy",
        "safe_error",
        "unsupported_reason",
    } <= set(by_id["legacy-office-unsupported"].passed_checks)
    assert {
        "expected_unsupported_reason",
        "expected_warning",
        "parser_warning_taxonomy",
        "safe_error",
        "unsupported_reason",
    } <= set(by_id["tiff-image-unsupported"].passed_checks)
    assert any(
        check.startswith("ocr_text:") for check in by_id["scanned-pdf-ocr-ja"].pending_checks
    )
    staging_plan = build_file_processing_staging_plan(manifest, report)
    assert len(staging_plan) == report.pending_staging_check_count
    gates = {requirement.suggested_gate for requirement in staging_plan}
    assert {
        "enterprise_ai_file_extraction_gate",
        "duplicate_kb_membership_gate",
        "preview_bbox_citation_gate",
        "segment_artifact_reuse_gate",
        "table_qa_search_gate",
    } <= gates


def test_file_processing_contract_runner_reports_local_regression() -> None:
    """期待 template が実装とずれたら case failure として検出する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases"][2]["expected_chunk_template"] = "broken_template"

    report = run_file_processing_contract_checks(manifest, manifest_path=manifest_path)

    assert report.passed is False
    assert report.failure_count == 1
    assert any(
        failure.startswith("expected_chunk_template:")
        for failure in report.case_results[2].failures
    )


def test_file_processing_golden_cli_writes_non_sensitive_report(tmp_path: Path) -> None:
    """CLI は local contract report を JSON artifact として保存できる。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-report.json"

    exit_code = file_processing_golden_cli.main([str(manifest_path), "--output", str(output_path)])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["promotion_ready"] is False
    assert payload["case_count"] == 16
    assert payload["pending_staging_check_count"] > 0
    assert payload["staging_policy"] == {
        "required_for_promotion": True,
        "pending_checks_block_promotion": True,
        "required_runtime_checks": ["extraction_artifact_cache_roundtrip"],
    }
    assert set(payload["metric_summary"]) >= set(manifest_metrics())
    assert payload["metric_summary"]["retrieval_recall"]["status"] == "requires_staging"
    assert payload["metric_summary"]["parser_fallback_rate"]["value"] == 1 / 16
    assert payload["metric_summary"]["extraction_page_coverage"]["status"] == "requires_staging"
    assert payload["metric_summary"]["extraction_page_coverage"]["value"] is None
    assert payload["metric_summary"]["low_confidence_document_rate"]["value"] == 0.0
    assert payload["metric_summary"]["failed_segment_rate"]["value"] == 1 / 16
    assert payload["metric_summary"]["parser_routing_accuracy"]["status"] == "measured"
    assert payload["metric_summary"]["parser_routing_accuracy"]["value"] == 1.0
    assert payload["metric_summary"]["table_qa_accuracy"]["status"] == "measured"
    assert payload["metric_summary"]["table_qa_accuracy"]["value"] == 1.0
    assert payload["metric_summary"]["page_hit_accuracy"]["status"] == "requires_staging"
    assert payload["metric_summary"]["page_hit_accuracy"]["value"] is None
    assert payload["metric_summary"]["citation_traceability_coverage"]["status"] == "partial"
    assert payload["metric_summary"]["bbox_coordinate_validity_coverage"]["status"] == (
        "requires_staging"
    )
    assert payload["metric_summary"]["bbox_coordinate_validity_coverage"]["value"] is None
    assert payload["metric_summary"]["preview_addressability_coverage"]["status"] == (
        "requires_staging"
    )
    assert payload["metric_summary"]["element_lineage_coverage"]["value"] == 1.0
    assert payload["metric_summary"]["chunk_block_integrity"]["status"] == "measured"
    assert payload["metric_summary"]["chunk_block_integrity"]["value"] == 1.0
    assert payload["metric_summary"]["reading_order_consistency"]["status"] == "partial"
    assert payload["metric_summary"]["reading_order_consistency"]["value"] == 1.0
    assert payload["metric_summary"]["structural_section_coverage"]["status"] == "measured"
    assert payload["metric_summary"]["structural_section_coverage"]["value"] == 1.0
    assert payload["metric_summary"]["dependency_context_recall"]["status"] == ("requires_staging")
    assert payload["metric_summary"]["dependency_context_recall"]["value"] is None
    assert payload["metric_summary"]["table_structure_fidelity"]["status"] == "measured"
    assert payload["metric_summary"]["table_structure_fidelity"]["value"] == 1.0
    assert payload["metric_summary"]["table_cell_lineage_coverage"]["status"] == "measured"
    assert payload["metric_summary"]["table_cell_lineage_coverage"]["value"] == 1.0
    assert payload["metric_summary"]["table_row_tree_fidelity"]["status"] == "measured"
    assert payload["metric_summary"]["table_row_tree_fidelity"]["value"] == 1.0
    assert payload["metric_summary"]["visual_chunk_metadata_completeness"]["status"] == ("measured")
    assert payload["metric_summary"]["visual_chunk_metadata_completeness"]["value"] == 1.0
    assert payload["metric_summary"]["chunk_size_compliance"]["status"] == "measured"
    assert payload["metric_summary"]["chunk_size_compliance"]["value"] == 1.0
    assert payload["metric_summary"]["chunk_contextual_coherence"]["status"] == "measured"
    assert payload["metric_summary"]["chunk_contextual_coherence"]["value"] == 1.0
    assert payload["metric_summary"]["cross_page_table_continuity_coverage"]["status"] == (
        "measured"
    )
    assert payload["metric_summary"]["cross_page_table_continuity_coverage"]["value"] == 1.0
    assert payload["metric_summary"]["ingestion_quality_report_completeness"]["status"] == (
        "partial"
    )
    assert payload["metric_summary"]["ingestion_quality_report_completeness"]["value"] == 1.0
    assert payload["metric_summary"]["parser_warning_taxonomy_coverage"]["status"] == "measured"
    assert payload["metric_summary"]["parser_warning_taxonomy_coverage"]["value"] == 1.0
    backend_source_summary = payload["metric_summary"]["backend_source_kind_coverage"]
    assert backend_source_summary["status"] == "measured"
    assert backend_source_summary["value"] == 1.0
    assert set(backend_source_summary["covered_source_kinds"]) >= (
        REQUIRED_FILE_PROCESSING_SOURCE_KINDS
    )
    assert backend_source_summary["missing_source_kinds"] == []
    assert backend_source_summary["backend_source_kinds"]
    assert "raw_text" not in str(backend_source_summary)
    assert payload["metric_summary"]["adapter_contract_coverage"]["status"] == ("requires_staging")
    assert payload["metric_summary"]["groundedness"]["status"] == "requires_staging"
    assert payload["metric_summary"]["ingestion_p95_ms"]["status"] == "requires_staging"
    threshold_by_metric = {result["metric"]: result for result in payload["threshold_results"]}
    assert threshold_by_metric["table_qa_accuracy"]["status"] == "passed"
    assert threshold_by_metric["chunk_block_integrity"]["status"] == "passed"
    assert threshold_by_metric["reading_order_consistency"]["status"] == "passed"
    assert threshold_by_metric["structural_section_coverage"]["status"] == "passed"
    assert threshold_by_metric["dependency_context_recall"]["status"] == "pending"
    assert threshold_by_metric["table_structure_fidelity"]["status"] == "passed"
    assert threshold_by_metric["table_cell_lineage_coverage"]["status"] == "passed"
    assert threshold_by_metric["table_row_tree_fidelity"]["status"] == "passed"
    assert threshold_by_metric["visual_chunk_metadata_completeness"]["status"] == "passed"
    assert threshold_by_metric["chunk_size_compliance"]["status"] == "passed"
    assert threshold_by_metric["chunk_contextual_coherence"]["status"] == "passed"
    assert threshold_by_metric["cross_page_table_continuity_coverage"]["status"] == "passed"
    assert threshold_by_metric["ingestion_quality_report_completeness"]["status"] == "passed"
    assert threshold_by_metric["parser_warning_taxonomy_coverage"]["status"] == "passed"
    assert threshold_by_metric["adapter_contract_coverage"]["status"] == "pending"
    assert threshold_by_metric["parser_routing_accuracy"]["status"] == "passed"
    assert threshold_by_metric["parser_fallback_rate"]["status"] == "passed"
    assert threshold_by_metric["extraction_page_coverage"]["status"] == "pending"
    assert threshold_by_metric["low_confidence_document_rate"]["status"] == "passed"
    assert threshold_by_metric["failed_segment_rate"]["status"] == "passed"
    assert threshold_by_metric["bbox_coordinate_validity_coverage"]["status"] == "pending"
    assert threshold_by_metric["page_hit_accuracy"]["status"] == "pending"
    blocker_by_code = {blocker["code"]: blocker for blocker in payload["promotion_blockers"]}
    assert (
        blocker_by_code["pending_staging_checks"]["count"] == payload["pending_staging_check_count"]
    )
    assert any(blocker["code"] == "threshold_pending" for blocker in payload["promotion_blockers"])
    assert len(payload["staging_requirements"]) == payload["pending_staging_check_count"]
    assert {requirement["suggested_gate"] for requirement in payload["staging_requirements"]} >= {
        "enterprise_ai_file_extraction_gate",
        "duplicate_kb_membership_gate",
        "quality_report_metadata_gate",
    }
    assert "raw_text" not in output_path.read_text(encoding="utf-8")


def test_file_processing_golden_cli_can_fail_on_pending(tmp_path: Path) -> None:
    """staging gate では pending を明示的に失敗扱いへ切り替えられる。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-report.json"

    exit_code = file_processing_golden_cli.main(
        [str(manifest_path), "--output", str(output_path), "--fail-on-pending"]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["promotion_ready"] is False


def test_file_processing_golden_cli_writes_non_sensitive_trend_snapshot(
    tmp_path: Path,
) -> None:
    """file-processing trend は品質回帰用の aggregate だけを保存する。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    report_path = tmp_path / "nested" / "file-processing-report.json"
    trend_path = tmp_path / "nested" / "file-processing-trend.json"

    exit_code = file_processing_golden_cli.main(
        [
            str(manifest_path),
            "--output",
            str(report_path),
            "--trend-output",
            str(trend_path),
        ]
    )

    trend = json.loads(trend_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert trend["kind"] == "file_processing_golden"
    assert trend["manifest_version"] == 1
    assert len(trend["result_sha256"]) == 64
    assert trend["passed"] is True
    assert trend["promotion_ready"] is False
    assert trend["case_count"] == 16
    assert trend["pending_staging_check_count"] > 0
    assert trend["promotion_blocker_code_counts"]["pending_staging_checks"] == 1
    assert trend["threshold_status_counts"]["passed"] > 0
    assert trend["threshold_status_counts"]["pending"] > 0
    assert any(item["metric"] == "page_hit_accuracy" for item in trend["threshold_pending"])
    assert trend["metrics"]["parser_fallback_rate"]["value"] == 1 / 16
    assert trend["metrics"]["table_qa_accuracy"]["value"] == 1.0
    assert trend["metrics"]["page_hit_accuracy"]["status"] == "requires_staging"
    assert trend["metrics"]["bbox_coordinate_validity_coverage"]["status"] == ("requires_staging")
    assert trend["metrics"]["backend_source_kind_coverage"]["missing_source_kinds"] == []
    assert trend["staging_dataset_policy"]["configured"] is False
    assert trend["staging_dataset_policy"]["promotion_ready"] is True
    assert "case_results" not in trend
    assert "staging_requirements" not in trend
    trend_text = trend_path.read_text(encoding="utf-8")
    assert "raw_text" not in trend_text
    assert "chunk_text" not in trend_text


def test_file_processing_golden_cli_honors_non_blocking_staging_policy(
    tmp_path: Path,
) -> None:
    """manifest が明示的に許可した場合だけ staging pending は promotion を止めない。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fixture_root"] = str((manifest_path.parent / manifest["fixture_root"]).resolve())
    manifest["staging_policy"] = {
        "required_for_promotion": False,
        "pending_checks_block_promotion": False,
        "required_runtime_checks": [],
    }
    custom_manifest_path = tmp_path / "manifest.json"
    custom_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_path = tmp_path / "file-processing-report.json"

    exit_code = file_processing_golden_cli.main(
        [str(custom_manifest_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["promotion_ready"] is True
    assert payload["pending_staging_check_count"] > 0
    assert payload["staging_policy"] == manifest["staging_policy"]
    assert not any(
        blocker["code"] in {"pending_staging_checks", "threshold_pending"}
        for blocker in payload["promotion_blockers"]
    )
    assert any(result["status"] == "pending" for result in payload["threshold_results"])


def test_file_processing_golden_cli_emits_safe_github_annotation(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """CI annotation は promotion status だけを非機密に出す。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    output_path = tmp_path / "file-processing-report.json"

    exit_code = file_processing_golden_cli.main(
        [str(manifest_path), "--output", str(output_path), "--github-annotations"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "::warning::file-processing golden promotion_not_ready" in captured.out
    assert "promotion_ready=false" in captured.out
    assert "pending_staging_check_count=29" in captured.out
    assert "raw_text" not in captured.out


def test_file_processing_golden_cli_fails_when_local_threshold_regresses(
    tmp_path: Path,
) -> None:
    """local measured metric が manifest threshold を下回ったら gate を失敗させる。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fixture_root"] = str((manifest_path.parent / manifest["fixture_root"]).resolve())
    manifest["thresholds"]["parser_fallback_rate"] = {"max": 0.0}
    custom_manifest_path = tmp_path / "manifest.json"
    custom_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_path = tmp_path / "file-processing-report.json"

    exit_code = file_processing_golden_cli.main(
        [str(custom_manifest_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    threshold_by_metric = {result["metric"]: result for result in payload["threshold_results"]}
    assert exit_code == 1
    assert payload["promotion_ready"] is False
    assert any(
        blocker["code"] == "threshold_failed" and blocker["metric"] == "parser_fallback_rate"
        for blocker in payload["promotion_blockers"]
    )
    assert threshold_by_metric["parser_fallback_rate"]["status"] == "failed"
    assert threshold_by_metric["parser_fallback_rate"]["actual"] == 1 / 16


def test_file_processing_golden_cli_fails_when_parser_routing_regresses(
    tmp_path: Path,
) -> None:
    """parser profile / chunk template の分流退化も metric threshold で止める。"""
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fixture_root"] = str((manifest_path.parent / manifest["fixture_root"]).resolve())
    manifest["cases"][2]["expected_chunk_template"] = "wrong_template"
    custom_manifest_path = tmp_path / "manifest.json"
    custom_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_path = tmp_path / "file-processing-report.json"

    exit_code = file_processing_golden_cli.main(
        [str(custom_manifest_path), "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    threshold_by_metric = {result["metric"]: result for result in payload["threshold_results"]}
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["metric_summary"]["parser_routing_accuracy"]["value"] < 1.0
    assert threshold_by_metric["parser_routing_accuracy"]["status"] == "failed"
    assert any(
        blocker["code"] == "threshold_failed" and blocker["metric"] == "parser_routing_accuracy"
        for blocker in payload["promotion_blockers"]
    )


def test_nightly_workflow_runs_file_processing_gate_before_api_skip() -> None:
    """nightly workflow は API base URL がなくても parser/file-processing artifact を作る。"""
    workflow = (SUITE_ROOT / ".github/workflows/rag-evaluation-nightly.yml").read_text(
        encoding="utf-8"
    )

    assert "file_processing_manifest_path" in workflow
    assert "file_processing_trend_baseline_path" in workflow
    assert "file_processing_staging_trend_baseline_path" in workflow
    assert "run_file_processing_staging" in workflow
    assert "require_real_world_file_processing_manifest" in workflow
    assert "run_parser_adapter_contract" in workflow
    assert "parser_adapter_contract_strict" in workflow
    assert "adapter_contract_strict_enabled=false" in workflow
    assert "adapter_contract_strict_enabled=true" in workflow
    assert "parser_adapter_contract_source_kinds" in workflow
    assert "app.rag.parser_adapter_contract_cli" in workflow
    assert "parser_adapter_contract_args+=(--strict)" in workflow
    assert "staging_args+=(--parser-adapter-contract-strict)" in workflow
    assert "app.rag.file_processing_golden_cli" in workflow
    assert "app.rag.file_processing_staging_cli" in workflow
    assert "app.rag.file_processing_trend_cli" in workflow
    assert "parser-adapter-compatibility.json" in workflow
    assert "file-processing-report.json" in workflow
    assert "file-processing-trend.json" in workflow
    assert "file-processing-trend-regression.json" in workflow
    assert "--trend-output" in workflow
    assert "file-processing-staging-report.json" in workflow
    assert "file-processing-staging-trend.json" in workflow
    assert "file-processing-staging-trend-regression.json" in workflow
    assert "--require-real-world-policy" in workflow
    assert "--require-promotion-ready" in workflow
    assert "--github-annotations" in workflow
    assert workflow.index("app.rag.parser_adapter_contract_cli") < workflow.index(
        "app.rag.file_processing_golden_cli"
    )
    assert workflow.index("app.rag.file_processing_golden_cli") < workflow.index(
        'if [ -z "$api_base_url" ]; then'
    )
    assert workflow.index("parser adapter contract gate failed") < workflow.index(
        "file-processing gate failed"
    )


def test_file_processing_golden_manifest_reports_missing_contract_fields() -> None:
    """manifest の重要契約が欠けたら stable error code を返す。"""
    invalid_manifest: Mapping[str, object] = {
        "metrics": ["table_qa_accuracy"],
        "cases": [
            {
                "id": "broken-case",
                "fixture": "broken.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
                "expected_parser_profile": "enterprise_ai_pdf_layout",
            }
        ],
    }

    errors = validate_file_processing_manifest(invalid_manifest)

    assert any(error.startswith("missing_metrics:") for error in errors)
    assert any(error.startswith("missing_scenarios:") for error in errors)
    assert "thresholds:missing" in errors
    assert "case[broken-case]:missing_fields:expected_chunk_template,required_checks" in errors
    assert "case[broken-case]:required_checks_empty" in errors
    assert "case[broken-case]:assertion_missing" in errors


def test_file_processing_manifest_accepts_real_world_staging_dataset_policy() -> None:
    """実 staging 難文書 manifest は非機密 real-world 契約を宣言できる。"""
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
            "expected_pages": [1],
            "required_checks": [
                "ocr_text",
                "page_coverage",
                "citation_traceability",
                "quality_report_metadata",
            ],
        }
    )

    errors = validate_file_processing_manifest(manifest)

    assert not any(error.startswith("staging_dataset_policy:") for error in errors)
    assert not any(error.startswith("case[real-scanned-pdf-ocr-ja]:real_world") for error in errors)
    summary = evaluation_module.staging_dataset_policy_summary(manifest)
    assert summary["configured"] is True
    assert summary["promotion_ready"] is True
    assert summary["real_world_case_count"] == 1
    assert summary["compliant_real_world_case_count"] == 1
    assert summary["covered_source_kinds"] == ["pdf"]
    assert summary["missing_source_kinds"] == []
    assert summary["covered_scenarios"] == ["scanned_pdf_ocr"]
    assert summary["missing_scenarios"] == []
    assert summary["policy_error_count"] == 0
    assert "real-scanned-contract-ja.pdf" not in json.dumps(summary, ensure_ascii=False)


def test_file_processing_manifest_rejects_fake_real_world_staging_dataset() -> None:
    """real-world gate は synthetic case の看板替えを許可しない。"""
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
            "expected_pages": [1],
            "required_checks": [
                "ocr_text",
                "page_coverage",
                "citation_traceability",
                "quality_report_metadata",
            ],
        }
    )

    errors = validate_file_processing_manifest(manifest)

    assert "case[fake-real-pdf]:real_world_data_sensitivity_not_non_sensitive" in errors
    assert "case[fake-real-pdf]:real_world_review_required" in errors
    assert "case[fake-real-pdf]:real_world_fixture_prefix_mismatch:staging/" in errors
    assert "staging_dataset_policy:real_world_cases_insufficient:1/2" in errors
    assert "staging_dataset_policy:missing_source_kinds:office" in errors
    assert "staging_dataset_policy:missing_scenarios:japanese_docx_layout" in errors
    summary = evaluation_module.staging_dataset_policy_summary(manifest)
    assert summary["promotion_ready"] is False
    assert cast(int, summary["policy_error_count"]) >= 1
    assert summary["real_world_case_count"] == 1
    assert summary["compliant_real_world_case_count"] == 0
    assert summary["sensitivity_violation_count"] == 1
    assert summary["review_missing_count"] == 1
    assert summary["fixture_prefix_mismatch_count"] == 1
    assert summary["missing_source_kinds"] == ["office"]
    assert summary["missing_scenarios"] == ["japanese_docx_layout"]
    assert "fake-real-pdf" not in json.dumps(summary, ensure_ascii=False)


def test_file_processing_fixture_asset_validator_reports_missing_assets(
    tmp_path: Path,
) -> None:
    """fixture 参照が存在しない場合は manifest case 単位で検出する。"""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest: Mapping[str, object] = {
        "fixture_root": "fixtures",
        "cases": [
            {
                "id": "missing-pdf",
                "fixture": "missing.pdf",
                "modality": "pdf",
            },
            {
                "id": "bad-extension",
                "fixture": "manual.txt",
                "modality": "html",
            },
            {
                "id": "unsafe",
                "fixture": "../secret.pdf",
                "modality": "pdf",
            },
        ],
    }

    errors = validate_file_processing_fixture_assets(manifest, manifest_path=manifest_path)

    assert any(error.startswith("fixture_root:not_found:") for error in errors)
    assert "case[missing-pdf]:fixture_not_found:missing.pdf" in errors
    assert "case[bad-extension]:fixture_extension_mismatch:html:.txt" in errors
    assert "case[unsafe]:fixture_unsafe_path:../secret.pdf" in errors


def test_file_processing_fixture_asset_validator_accepts_tsv_assets(
    tmp_path: Path,
) -> None:
    """golden set は TSV の表 fixture も table QA 対象として表現できる。"""
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "long-table.tsv").write_text("name\tamount\nalpha\t1200\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest: Mapping[str, object] = {
        "fixture_root": "fixtures",
        "cases": [
            {
                "id": "long-table-tsv",
                "fixture": "long-table.tsv",
                "modality": "tsv",
            }
        ],
    }

    errors = validate_file_processing_fixture_assets(manifest, manifest_path=manifest_path)

    assert errors == ()


def manifest_metrics() -> Sequence[str]:
    manifest_path = REPO_ROOT / "docs/evaluation/file-processing-golden-set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return cast(Sequence[str], manifest["metrics"])


def test_spreadsheet_citations_are_traceable_by_sheet_and_row() -> None:
    """表計算の引用は頁の代わりにシート名と行・セル範囲・要素の ID でたどれる（#1456）。

    以前は page_start を必須にしていたので、Excel の 1 行 = 1 chunk の引用は位置を持っていても
    常にたどれないと数え、指標が「引用のうち PDF の割合」になっていた。
    """
    ledger_row = {
        "document_id": "ledger",
        "chunk_id": "ledger:23",
        "file_name": "system-ledger.xlsx",
        "metadata": {
            "sheet_name": "システム台帳",
            "row_start": 23,
            "row_end": 23,
            "section_path": "システム台帳",
            "element_ids": ["sheet-000-r23"],
        },
    }
    citations: list[RetrievedChunk | Mapping[str, object]] = [
        ledger_row,
        RetrievedChunk(
            document_id="ledger",
            chunk_id="ledger:24",
            text="システムID: SYS-120",
            score=1.0,
            metadata={"sheet_name": "システム台帳", "cell_range": "A24:F24"},
        ),
        {
            "document_id": "ledger",
            "chunk_id": "ledger:5",
            "metadata": {"sheet_name": "システム台帳", "element_ids": "sheet-000-r5"},
        },
        # シート名だけ（行・セル範囲・要素の ID が無い）はたどれない。
        {"document_id": "ledger", "chunk_id": "ledger:6", "metadata": {"sheet_name": "台帳"}},
        # 頁もシートの位置も無い引用はたどれない。
        {
            "document_id": "doc-5",
            "chunk_id": "doc-5:0",
            "metadata": {"section_path": "第1章", "element_ids": "el-1"},
        },
        # 頁のある引用（PDF）の条件は変えない（頁と要素・bbox・節が要る）。
        {"document_id": "doc-6", "chunk_id": "doc-6:0", "metadata": {"page_start": 1}},
    ]

    assert citation_traceability_coverage(citations) == 0.5
    assert citation_traceability_coverage([ledger_row]) == 1.0
    # 文書・chunk の ID が無ければシートの位置があってもたどれない。
    assert citation_traceability_coverage([{**ledger_row, "chunk_id": ""}]) == 0.0
