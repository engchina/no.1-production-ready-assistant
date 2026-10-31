from __future__ import annotations

from prometheus_client import REGISTRY

from app.features.nl2sql.ontology_observability import record_source_extraction


def _source_extraction_sample(*, file_format: str, status: str) -> float:
    value = REGISTRY.get_sample_value(
        "nl2sql_ontology_source_extractions_total",
        {"format": file_format, "status": status},
    )
    return float(value or 0)


def test_record_source_extraction_keeps_xls_as_controlled_format() -> None:
    xls_before = _source_extraction_sample(file_format="xls", status="extracted")
    unknown_before = _source_extraction_sample(file_format="unknown", status="extracted")

    record_source_extraction(file_format=".xls", status="extracted")

    assert _source_extraction_sample(file_format="xls", status="extracted") == xls_before + 1
    assert _source_extraction_sample(file_format="unknown", status="extracted") == unknown_before


def test_record_source_extraction_still_buckets_unknown_formats() -> None:
    unknown_before = _source_extraction_sample(file_format="unknown", status="extracted")

    record_source_extraction(file_format=".tsv", status="extracted")

    assert (
        _source_extraction_sample(file_format="unknown", status="extracted") == unknown_before + 1
    )
