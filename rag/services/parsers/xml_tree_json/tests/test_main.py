"""XML ツリー JSON の parser サービスの HTTP 契約(#2024)。

合成の XML ツリー JSON を ``POST /parse`` に渡し、段落を 1 要素にした
``StructuredExtraction`` が返ること、対応しない JSON は fallback の warning になることを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "rag_parser_core"
    / "tests"
    / "fixtures"
    / "xml_tree_sample.json"
)


def test_health_reports_backend() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "xml_tree_json"
    assert body["status"] == "ok"


def test_parse_returns_paragraph_elements_with_citation_labels() -> None:
    response = TestClient(app).post(
        "/parse",
        files={"file": ("sample.json", FIXTURE.read_bytes(), "application/json")},
        data={"content_type": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["parser_backend"] == "xml_tree_json"
    assert not body["fallback_used"]
    extraction = body["extraction"]
    labels = [
        element["metadata"]["xml_tree_citation_label_arabic"] for element in extraction["elements"]
    ]
    assert "サンプル規程 第1条" in labels
    assert "サンプル規程 附則（制定時）第3条" in labels
    document = extraction["parser_artifacts"]["xml_tree_document"]
    assert document["document_id"] == "SAMPLE0000000001"


def test_parse_rejects_json_that_is_not_supported_tree() -> None:
    response = TestClient(app).post(
        "/parse",
        files={"file": ("data.json", json.dumps({"items": [1]}).encode(), "application/json")},
        data={"content_type": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["extraction"] is None
    assert body["warnings"] == ["xml_tree_json_unsupported_tree"]
