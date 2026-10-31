"""backend_core の app factory / health / error envelope / metrics の疎通テスト。"""

from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

from pr_backend_core import create_app


def _build_client(readiness: dict[str, str] | None = None) -> TestClient:
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("kaboom")

    @router.get("/missing")
    async def missing() -> dict[str, str]:
        raise HTTPException(status_code=404)

    app = create_app(
        service_name="test-service",
        version="9.9.9",
        cors_origins=["http://localhost:3000"],
        api_router=router,
        readiness_checks_getter=(lambda: readiness) if readiness is not None else None,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_health_ok() -> None:
    client = _build_client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["version"] == "9.9.9"
    assert body["error_messages"] == []


def test_ready_degraded_when_check_fails() -> None:
    client = _build_client(readiness={"oracle": "missing"})
    resp = client.get("/api/ready")
    assert resp.status_code == 503
    assert resp.json()["data"]["status"] == "degraded"


def test_ready_ok_when_all_checks_pass() -> None:
    client = _build_client(readiness={"oracle": "ok"})
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ok"


def test_request_id_header_generated_and_preserved() -> None:
    client = _build_client()
    generated = client.get("/api/health")
    assert generated.headers["x-request-id"]
    preserved = client.get("/api/health", headers={"X-Request-ID": "abc-123"})
    assert preserved.headers["x-request-id"] == "abc-123"


def test_http_exception_envelope() -> None:
    client = _build_client()
    resp = client.get("/api/missing")
    assert resp.status_code == 404
    body = resp.json()
    assert body["data"] is None
    assert body["error_messages"] == ["リソースが見つかりません。"]


def test_unhandled_exception_is_masked() -> None:
    client = _build_client()
    resp = client.get("/api/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["data"] is None
    assert "kaboom" not in body["error_messages"][0]


def test_metrics_endpoint_exposed() -> None:
    client = _build_client()
    client.get("/api/health")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text


def test_api_docs_are_not_exposed() -> None:
    """API ドキュメント（/docs・/redoc・/openapi.json）は公開しない（#748）。"""
    client = _build_client()
    for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    # schema はテストなどから app.openapi() で得られる。
    assert "/api/health" in client.app.openapi()["paths"]  # type: ignore[attr-defined]
