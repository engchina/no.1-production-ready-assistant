"""chunk template scorecard のテスト。"""

from app.rag.chunk_template_scorecard import (
    ChunkTemplateScorecard,
    ChunkTemplateScorecardEntry,
    build_chunk_template_scorecard,
)


def test_chunk_template_scorecard_recommends_healthy_template() -> None:
    """core chunk metrics が良好なら observed template を recommended にする。"""
    scorecard = build_chunk_template_scorecard(
        metrics={
            "chunk_block_integrity": 1.0,
            "chunk_contextual_coherence": 1.0,
            "chunk_size_compliance": 1.0,
            "element_lineage_coverage": 1.0,
            "page_hit_accuracy": 1.0,
        },
        observed_templates=["pdf_layout", "table_preserve_rows"],
        metrics_source="file_processing_staging",
    )

    assert scorecard.promotion_blocking is False
    assert scorecard.recommended_template == "pdf_layout"
    pdf = _entry(scorecard, "pdf_layout")
    assert pdf.status == "recommended"
    assert pdf.score == 100.0
    assert "adaptive_chunking_metrics_applied" in pdf.reason_codes


def test_chunk_template_scorecard_blocks_poor_core_metrics() -> None:
    """block integrity / coherence / size が悪ければ promotion blocker にする。"""
    scorecard = build_chunk_template_scorecard(
        metrics={
            "chunk_block_integrity": 0.0,
            "chunk_contextual_coherence": 0.2,
            "chunk_size_compliance": 0.5,
            "element_lineage_coverage": 1.0,
        },
        observed_templates=["html_semantic"],
        metrics_source="file_processing_staging",
    )

    assert scorecard.promotion_blocking is True
    assert scorecard.recommended_template is None
    html = _entry(scorecard, "html_semantic")
    assert html.status == "blocked"
    assert html.promotion_blocking is True
    assert "chunk_template_score_below_promotion_threshold" in html.reason_codes


def test_chunk_template_scorecard_does_not_block_unmeasured_preflight() -> None:
    """metrics がなければ未測定として扱い、preflight 的な段階では block しない。"""
    scorecard = build_chunk_template_scorecard(
        metrics={},
        observed_templates=["office_slide"],
        metrics_source="runtime",
    )

    assert scorecard.promotion_blocking is False
    assert scorecard.recommended_template is None
    slide = _entry(scorecard, "office_slide")
    assert slide.status == "unmeasured"
    assert slide.metric_source == "none"


def test_chunk_template_scorecard_blocks_missing_manifest_evidence() -> None:
    """template 別の source/scenario evidence が欠ける場合は global metric だけで通さない。"""
    scorecard = build_chunk_template_scorecard(
        metrics={
            "chunk_block_integrity": 1.0,
            "chunk_contextual_coherence": 1.0,
            "chunk_size_compliance": 1.0,
            "structural_section_coverage": 1.0,
            "dependency_context_recall": 1.0,
        },
        observed_templates=["html_semantic"],
        metrics_source="file_processing_staging",
        template_evidence={
            "html_semantic": {
                "expected_case_count": 2,
                "measured_case_count": 1,
                "expected_source_kinds": ["html", "office"],
                "covered_source_kinds": ["html"],
                "expected_scenarios": ["html_semantic_blocks", "japanese_docx_layout"],
                "covered_scenarios": ["html_semantic_blocks"],
            }
        },
    )

    entry = _entry(scorecard, "html_semantic")
    assert scorecard.promotion_blocking is True
    assert entry.status == "blocked"
    assert entry.score == 100.0
    assert entry.expected_case_count == 2
    assert entry.measured_case_count == 1
    assert entry.missing_source_kinds == ("office",)
    assert entry.missing_scenarios == ("japanese_docx_layout",)
    assert "chunk_template_case_evidence_missing" in entry.reason_codes
    assert "chunk_template_source_kind_evidence_missing" in entry.reason_codes
    assert "chunk_template_scenario_evidence_missing" in entry.reason_codes


def _entry(
    scorecard: ChunkTemplateScorecard,
    template: str,
) -> ChunkTemplateScorecardEntry:
    entries = scorecard.entries
    return next(entry for entry in entries if entry.template == template)
