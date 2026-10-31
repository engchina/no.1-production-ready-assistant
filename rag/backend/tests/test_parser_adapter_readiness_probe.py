"""readiness probe(parser サービス /health 問い合わせ)の検証。

probe ON 時に各 adapter の version/可用性を /health から解決し、未達時は missing へ
縮退することを確認する。probe OFF(既定)の挙動は test_parser_adapter_* 側で担保する。
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.rag.parser_adapter_readiness import parser_adapter_runtime_settings


def _install_health(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    # readiness は httpx を遅延 import するため、グローバルの httpx.Client を差し替える。
    original = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("timeout", None)
        return original(transport=handler)

    monkeypatch.setattr(httpx, "Client", factory)


def _settings() -> Settings:
    return Settings(
        rag_parser_readiness_probe_enabled=True,
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
        rag_http_service_retry_attempts=1,
    )


def test_probe_reports_remote_version_when_service_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "backend": "docling",
                "package_name": "docling",
                "package_version": "2.103.0",
            },
        )

    _install_health(monkeypatch, httpx.MockTransport(handle))
    runtime = parser_adapter_runtime_settings(_settings())
    docling = next(a for a in runtime.adapters if a.backend == "docling")
    assert docling.installed is True
    assert docling.version == "2.103.0"
    assert docling.status == "active"


def test_probe_marks_missing_when_service_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _install_health(monkeypatch, httpx.MockTransport(handle))
    runtime = parser_adapter_runtime_settings(_settings())
    docling = next(a for a in runtime.adapters if a.backend == "docling")
    assert docling.installed is False
    assert docling.version is None
    assert docling.status == "missing"
