"""OCI Document Understanding 共有 core と service app factory の検証。

実 OCI に依らず、`OciDocumentUnderstandingConfig.from_env` の解決と
`create_service_parse_app` の HTTP 契約(/health, /parse + document_id)を決定論で確認する。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag_parser_core.extraction import StructuredExtraction
from rag_parser_core.oci_document_understanding import (
    OciDocumentUnderstandingConfig,
    document_understanding_result_to_payload,
)
from rag_parser_core.result import ParseResponse
from rag_parser_core.service import create_service_parse_app
from rag_parser_core.source import SourceModality, SourceProfile


def _polygon(x1: float, y1: float, x2: float, y2: float) -> dict[str, object]:
    """DU の normalizedVertices(0..1)形式の boundingPolygon を作る。"""
    return {
        "normalizedVertices": [
            {"x": x1, "y": y1},
            {"x": x2, "y": y1},
            {"x": x2, "y": y2},
            {"x": x1, "y": y2},
        ]
    }


def _du_result_json() -> dict[str, object]:
    return {
        "pages": [
            {
                "pageNumber": 1,
                "dimensions": {"width": 2480, "height": 3508},
                "lines": [
                    {"text": "請求書", "boundingPolygon": _polygon(0.1, 0.05, 0.4, 0.1)},
                    {"text": "合計 1,200 円", "boundingPolygon": _polygon(0.1, 0.2, 0.6, 0.25)},
                ],
                "words": [{"confidence": 0.9}],
                "tables": [
                    {
                        "headerRows": [
                            {
                                "cells": [
                                    {
                                        "text": "品目",
                                        "rowIndex": 0,
                                        "columnIndex": 0,
                                        "boundingPolygon": _polygon(0.1, 0.3, 0.3, 0.35),
                                    }
                                ]
                            }
                        ],
                        "bodyRows": [
                            {"cells": [{"text": "1,200", "rowIndex": 1, "columnIndex": 1}]}
                        ],
                    }
                ],
            }
        ],
        "detectedDocumentTypes": [{"documentType": "INVOICE"}],
    }


def test_config_from_env_resolves_fallbacks() -> None:
    env = {
        "PLATFORM_OCI_COMPARTMENT_ID": "ocid1.compartment.oc1..fallback",
        "PLATFORM_OCI_REGION": "us-chicago-1",
        "PLATFORM_OBJECT_STORAGE_REGION": "ap-osaka-1",
        "PLATFORM_OBJECT_STORAGE_NAMESPACE": "ns-default",
        "PLATFORM_OBJECT_STORAGE_BUCKET": "bucket-default",
        "RAG_OCI_DOCUMENT_UNDERSTANDING_LANGUAGE": "JPN",
        "RAG_OCI_DOCUMENT_UNDERSTANDING_FEATURES": '["DOCUMENT_TEXT_EXTRACTION"]',
        "RAG_OCI_DOCUMENT_UNDERSTANDING_POLL_INTERVAL_SECONDS": "0.5",
    }
    config = OciDocumentUnderstandingConfig.from_env(env)
    # 専用設定が空なら object_storage_* / compartment へ fallback する。
    assert config.resolve_compartment_id() == "ocid1.compartment.oc1..fallback"
    assert config.resolve_namespace() == "ns-default"
    assert config.resolve_input_bucket() == "bucket-default"
    assert config.resolve_output_bucket() == "bucket-default"
    assert config.is_configured() is True
    assert config.object_storage_region == "us-chicago-1"
    assert list(config.features) == ["DOCUMENT_TEXT_EXTRACTION"]
    assert config.poll_interval_seconds == 0.5


def test_config_from_env_allows_du_object_storage_region_override() -> None:
    config = OciDocumentUnderstandingConfig.from_env(
        {
            "PLATFORM_OCI_REGION": "us-chicago-1",
            "PLATFORM_OBJECT_STORAGE_REGION": "ap-osaka-1",
            "RAG_OCI_DOCUMENT_UNDERSTANDING_OBJECT_STORAGE_REGION": "eu-frankfurt-1",
        }
    )
    assert config.object_storage_region == "eu-frankfurt-1"


def test_config_from_env_unconfigured() -> None:
    assert OciDocumentUnderstandingConfig.from_env({}).is_configured() is False


def _service_app(*, configured: bool, captured: dict[str, object]) -> TestClient:
    async def _parse(
        source_bytes: bytes,
        content_type: str,
        source_profile: SourceProfile | None,
        document_id: str,
        prompt: str,
    ) -> ParseResponse:
        captured["bytes"] = source_bytes
        captured["content_type"] = content_type
        captured["document_id"] = document_id
        captured["prompt"] = prompt
        extraction = StructuredExtraction.model_validate(
            document_understanding_result_to_payload(_du_result_json())
        )
        return ParseResponse(
            extraction=extraction,
            parser_backend="oci_document_understanding",
            parser_version="oci_document_understanding",
            template="oci_document_understanding",
        )

    app = create_service_parse_app(
        backend="oci_document_understanding",
        parse=_parse,
        is_configured=lambda: configured,
        title="parser-oci-document-understanding",
    )
    return TestClient(app)


def test_service_health_reflects_configuration() -> None:
    assert _service_app(configured=True, captured={}).get("/health").json()["status"] == "ok"
    assert _service_app(configured=False, captured={}).get("/health").json()["status"] == "degraded"


def test_service_parse_roundtrips_and_forwards_document_id() -> None:
    captured: dict[str, object] = {}
    client = _service_app(configured=True, captured=captured)
    response = client.post(
        "/parse",
        files={"file": ("scan.pdf", b"%PDF", "application/pdf")},
        data={"content_type": "application/pdf", "document_id": "doc-123"},
    )
    assert response.status_code == 200
    parsed = ParseResponse.model_validate(response.json())
    assert parsed.parser_backend == "oci_document_understanding"
    assert parsed.extraction is not None
    assert "請求書" in parsed.extraction.raw_text
    # document_id がハンドラまで届くこと(OCI 入力 object 名の一意化に使う)
    assert captured["document_id"] == "doc-123"
    assert captured["content_type"] == "application/pdf"


def test_service_parse_falls_back_to_profile_sha_for_document_id() -> None:
    captured: dict[str, object] = {}
    client = _service_app(configured=True, captured=captured)
    profile = SourceProfile(
        original_file_name="a.pdf",
        sanitized_file_name="a.pdf",
        content_type="application/pdf",
        file_size_bytes=4,
        content_sha256="a" * 64,
        modality=SourceModality.PDF,
        parser_profile="pdf",
    )
    response = client.post(
        "/parse",
        files={"file": ("a.pdf", b"%PDF", "application/pdf")},
        data={"content_type": "application/pdf", "source_profile": profile.model_dump_json()},
    )
    assert response.status_code == 200
    # document_id 未指定時は source_profile.content_sha256 へフォールバックする。
    assert captured["document_id"] == "a" * 64


@pytest.mark.parametrize("payload", [_du_result_json()])
def test_remap_payload_validates_as_structured_extraction(payload: dict[str, object]) -> None:
    extraction = StructuredExtraction.model_validate(
        document_understanding_result_to_payload(payload)
    )
    assert extraction.document_type == "INVOICE"
    assert extraction.tables and extraction.tables[0].cells


def test_remap_maps_bounding_polygon_to_bbox() -> None:
    """DU の boundingPolygon を element / cell の bbox(xyxy・ratio)へ写す。"""
    extraction = StructuredExtraction.model_validate(
        document_understanding_result_to_payload(_du_result_json())
    )
    # ページ寸法は ExtractionPage に残る。
    assert extraction.pages[0].width == 2480
    assert extraction.pages[0].height == 3508
    # line → bbox 付き element(polygon→xyxy 集約、ratio)。
    bbox_elements = [element for element in extraction.elements if element.bbox]
    assert bbox_elements, "line bbox が element に載っていない"
    first = bbox_elements[0]
    assert first.bbox == [0.1, 0.05, 0.4, 0.1]
    assert first.metadata["bbox_coordinate_mode"] == "xyxy"
    assert first.metadata["bbox_unit"] == "ratio"
    assert first.metadata["bbox_source"] == "line"
    # table cell → bbox。
    cells_with_bbox = [cell for table in extraction.tables for cell in table.cells if cell.bbox]
    assert cells_with_bbox and cells_with_bbox[0].bbox == [0.1, 0.3, 0.3, 0.35]


def test_remap_omits_bbox_metadata_for_invalid_polygon() -> None:
    """有効な bbox に正規化できない polygon では bbox も lineage metadata も付けない。"""
    payload = {
        "pages": [
            {
                "pageNumber": 1,
                # 頂点 1 点 → 2 値しか無く xyxy へ正規化できない。
                "lines": [
                    {"text": "x", "boundingPolygon": {"normalizedVertices": [{"x": 0.1, "y": 0.1}]}}
                ],
            }
        ]
    }
    extraction = StructuredExtraction.model_validate(
        document_understanding_result_to_payload(payload)
    )
    element = extraction.elements[0]
    assert element.bbox is None
    assert "bbox_coordinate_mode" not in element.metadata
    assert "bbox_unit" not in element.metadata
