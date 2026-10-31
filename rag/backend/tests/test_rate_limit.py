"""高コスト API の rate limit テスト。"""

import pytest
from pytest import MonkeyPatch

from app.config import get_settings
from app.main import app
from app.rag.rate_limit import RATE_LIMIT_MESSAGE
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

# 検索・アップロードエンドポイント経由のため実 Oracle + 決定論 AI スタブを使う。
pytestmark = pytest.mark.usefixtures("oracle_db")


def test_search_rate_limit_returns_429_with_retry_headers(monkeypatch: MonkeyPatch) -> None:
    """検索 API は同一主体の上限超過を 429 と retry header で返す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_search_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60.0)

    first = client.post("/api/search", json={"query": "存在しない社内規程"})
    second = client.post("/api/search", json={"query": "存在しない社内規程"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"]
    assert second.headers["x-ratelimit-limit"] == "1"
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert second.json()["error_messages"] == [RATE_LIMIT_MESSAGE]


def test_search_rate_limit_is_scoped_by_tenant_hash(monkeypatch: MonkeyPatch) -> None:
    """tenant が異なる検索は同一 client host でも別 bucket として扱う。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_search_requests", 1)

    tenant_a = client.post(
        "/api/search",
        json={"query": "存在しない社内規程"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    tenant_b = client.post(
        "/api/search",
        json={"query": "存在しない社内規程"},
        headers={"X-Tenant-ID": "tenant-b"},
    )
    tenant_a_again = client.post(
        "/api/search",
        json={"query": "存在しない社内規程"},
        headers={"X-Tenant-ID": "tenant-a"},
    )

    assert tenant_a.status_code == 200
    assert tenant_b.status_code == 200
    assert tenant_a_again.status_code == 429
    assert "tenant-a" not in tenant_a_again.text


def test_rate_limit_can_be_disabled(monkeypatch: MonkeyPatch) -> None:
    """緊急時や外部 limiter 併用時はアプリ内 limiter を無効化できる。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_search_requests", 1)

    first = client.post("/api/search", json={"query": "存在しない社内規程"})
    second = client.post("/api/search", json={"query": "存在しない社内規程"})

    assert first.status_code == 200
    assert second.status_code == 200


def test_evaluation_rate_limit_protects_golden_set_runs(monkeypatch: MonkeyPatch) -> None:
    """評価 API は golden set の連続実行を制限する。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_evaluation_runs", 1)
    payload = {
        "cases": [
            {
                "id": "case-1",
                "query": "存在しない社内規程",
                "relevant_document_ids": [],
            }
        ],
        "top_k": 1,
    }

    first = client.post("/api/evaluation/run", json=payload)
    second = client.post("/api/evaluation/run", json=payload)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error_messages"] == [RATE_LIMIT_MESSAGE]


def test_upload_rate_limit_blocks_second_file(monkeypatch: MonkeyPatch) -> None:
    """upload API は同一主体の連続 upload を制限する。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_uploads", 1)

    first = client.post(
        "/api/documents/upload",
        files={"file": ("policy-a.txt", "社内規程A".encode(), "text/plain")},
    )
    second = client.post(
        "/api/documents/upload",
        files={"file": ("policy-b.txt", "社内規程B".encode(), "text/plain")},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error_messages"] == [RATE_LIMIT_MESSAGE]
