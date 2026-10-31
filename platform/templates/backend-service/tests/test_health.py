"""health / 業務 ping の疎通テスト（Oracle 不要）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ok"


def test_ready() -> None:
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ok"


def test_example_ping() -> None:
    resp = client.get("/api/example/ping")
    assert resp.status_code == 200
    assert resp.json()["data"]["message"] == "pong"
