"""API ドキュメント（/docs・/redoc・/openapi.json）を公開しないことの回帰テスト（#748）。

RAG の backend と、parser / 前処理のサービスの app factory が対象。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from rag_parser_core.preprocess import ConvertHealth, ConvertOutcome
from rag_parser_core.preprocess_service import create_preprocess_app
from rag_parser_core.result import ParseResponse
from rag_parser_core.service import create_parse_app, create_service_parse_app
from rag_parser_core.source import SourceProfile

from app.main import app as backend_app
from tests.support import AsgiTestClient

DOC_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


def _convert(
    _data: bytes, _filename: str, _profile: str, _source: SourceProfile | None
) -> ConvertOutcome:
    raise AssertionError("呼ばれない")


async def _service_parse(
    _data: bytes, _filename: str, _source: SourceProfile | None, _document_id: str, _prompt: str
) -> ParseResponse:
    raise AssertionError("呼ばれない")


def _service_apps() -> list[FastAPI]:
    return [
        create_parse_app(backend="docling", import_name="docling"),
        create_service_parse_app(backend="unstructured", parse=_service_parse),
        create_preprocess_app(converter=_convert, health_probe=ConvertHealth),
    ]


@pytest.mark.parametrize("path", DOC_PATHS)
def test_backend_does_not_expose_api_docs(path: str) -> None:
    response = AsgiTestClient(backend_app).get(path)

    assert response.status_code == 404
    # schema はテストなどから app.openapi() で得られる。
    assert "/api/health" in backend_app.openapi()["paths"]


def test_service_apps_do_not_expose_api_docs() -> None:
    for service_app in _service_apps():
        client = AsgiTestClient(service_app)
        for path in DOC_PATHS:
            assert client.get(path).status_code == 404, (service_app.title, path)
        assert client.get("/health").status_code == 200, service_app.title
