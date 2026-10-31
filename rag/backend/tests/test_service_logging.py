"""独立 service factory が DB / OCR なしで共通の access 契約を使う。"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rag_parser_core.preprocess import ConvertHealth, ConvertOutcome
from rag_parser_core.preprocess_service import create_preprocess_app
from rag_parser_core.service import create_parse_app
from rag_parser_core.source import SourceProfile


def _convert(
    _data: bytes, _filename: str, _profile: str, _source: SourceProfile | None
) -> ConvertOutcome:
    raise AssertionError("呼ばれない")


@pytest.mark.parametrize("kind", ["parser", "preprocess"])
def test_service_factory_uses_common_wire_contract(kind: str, capsys: Any) -> None:
    app = (
        create_parse_app(backend="stub", import_name="json", title="parser-stub")
        if kind == "parser"
        else create_preprocess_app(
            converter=_convert, health_probe=ConvertHealth, title="preprocess-stub"
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/health?question=PRIVATE_SENTINEL",
            headers={"X-Request-ID": "service-request", "X-Correlation-Run-ID": "run-1"},
        )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "service-request"
    wire = capsys.readouterr().err
    assert "PRIVATE_SENTINEL" not in wire
    records = [json.loads(line) for line in wire.splitlines()]
    summaries = [record for record in records if record["event"] == "http_access"]
    assert len(summaries) == 1
    assert summaries[0]["service_name"] == app.title
    assert summaries[0]["component"] == "microservice"
    assert summaries[0]["timestamp"].endswith("+09:00")
    assert summaries[0]["run_id"] == "run-1"
