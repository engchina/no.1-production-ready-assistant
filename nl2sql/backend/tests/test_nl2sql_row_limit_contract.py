"""取得件数上限の API 境界と Oracle への値の受け渡しを検証する。"""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app.features.nl2sql import router


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router._require_persistence] = lambda: None
    service = MagicMock()
    monkeypatch.setattr(router, "nl2sql_service", service)
    return app, service


@pytest.mark.parametrize("limit", [0, -1, 1.5, 100001])
@pytest.mark.parametrize(
    ("path", "field", "payload"),
    [
        ("/db-admin/execute", "row_limit", {"sql": "SELECT * FROM T"}),
        ("/db-admin/preview-data", "limit", {"object_name": "T"}),
        ("/db-admin/preview-data/export.xlsx", "limit", {"object_name": "T"}),
        ("/execute", "row_limit", {"sql": "SELECT * FROM T"}),
    ],
)
async def test_invalid_limits_never_reach_service(
    api: tuple[FastAPI, MagicMock],
    limit: float,
    path: str,
    field: str,
    payload: dict[str, Any],
) -> None:
    app, service = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/nl2sql" + path, json={**payload, field: limit})
    assert response.status_code == 422
    assert service.mock_calls == []
