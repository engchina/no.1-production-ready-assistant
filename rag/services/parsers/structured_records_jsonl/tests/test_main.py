"""構造化レコード JSONL の parser サービスの HTTP 契約(#2457)。

合成の構造化レコードの JSONL を ``POST /parse`` に渡し、記録を 1 要素にして検索用テキストを
境界のマーカーで区切った ``StructuredExtraction`` が返ること、契約に合わない入力は fallback の
warning になることを確かめる。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from rag_parser_core.structured_records import (
    JSONL_RECORD_BOUNDARY,
    STRUCTURED_RECORDS_CONTENT_TYPE,
    DocumentLine,
    RecordField,
    RecordLocation,
    RowLine,
    dump_structured_records,
)

from app.main import app


def _jsonl() -> bytes:
    return dump_structured_records(
        [
            DocumentLine(
                record_id="document",
                source_type="csv",
                converter="csv_to_jsonl",
                converter_version="v2",
                header_row=1,
            ),
            RowLine(
                record_id="row-000001",
                source_type="csv",
                location=RecordLocation(row_start=2, row_end=2),
                fields=[RecordField(name="項目", position=1, value="値 A")],
            ),
            RowLine(
                record_id="row-000002",
                source_type="csv",
                location=RecordLocation(row_start=3, row_end=3),
                fields=[RecordField(name="項目", position=1, value="値 B")],
            ),
        ]
    )


def test_health_reports_backend() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "structured_records_jsonl"
    assert body["status"] == "ok"


def test_parse_returns_records_separated_by_boundaries() -> None:
    response = TestClient(app).post(
        "/parse",
        files={"file": ("records.jsonl", _jsonl(), STRUCTURED_RECORDS_CONTENT_TYPE)},
        data={"content_type": STRUCTURED_RECORDS_CONTENT_TYPE},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["parser_backend"] == "structured_records_jsonl"
    assert not body["fallback_used"]
    extraction = body["extraction"]
    assert [element["element_id"] for element in extraction["elements"]] == [
        "row-000001",
        "row-000002",
    ]
    assert extraction["raw_text"] == f"項目: 値 A\n{JSONL_RECORD_BOUNDARY}\n項目: 値 B"
    assert extraction["parser_artifacts"]["structured_records_record_boundary_count"] == 1


def test_parse_rejects_jsonl_outside_the_contract() -> None:
    response = TestClient(app).post(
        "/parse",
        files={"file": ("data.jsonl", json.dumps({"items": [1]}).encode() + b"\n", "text/plain")},
        data={"content_type": "text/plain"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["extraction"] is None
    assert body["warnings"] == ["structured_records_invalid:unknown_schema@line1"]
