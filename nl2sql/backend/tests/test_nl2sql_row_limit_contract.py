"""取得件数上限の API 境界と Oracle への値の受け渡しを検証する。"""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app.features.nl2sql import router, synthetic_router
from app.features.nl2sql.models import QueryResults, SyntheticDataResultsData


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, MagicMock, MagicMock]:
    app = FastAPI()
    app.include_router(router.router)
    app.include_router(synthetic_router.router, prefix="/nl2sql")
    app.dependency_overrides[router._require_persistence] = lambda: None
    service = MagicMock()
    service.synthetic_data_results.return_value = SyntheticDataResultsData(
        table_name="APP.T", runtime="oracle", results=QueryResults(columns=[], rows=[], total=0)
    )
    monkeypatch.setattr(router, "nl2sql_service", service)
    synthetic = MagicMock()
    synthetic.get.return_value.preview = False
    synthetic.get.return_value.targets = [MagicMock(table_name="APP.T")]
    synthetic.get.return_value.request = {"_object_ids": {"APP.T": 7}}
    synthetic.adapter._db_admin_identity.return_value = MagicMock(owner="APP", object_name="T")
    cursor = synthetic.adapter.connection.return_value.__enter__.return_value.cursor
    cursor.return_value.__enter__.return_value.fetchone.return_value = (7,)
    synthetic.adapter.execute_select.return_value = QueryResults(columns=[], rows=[], total=0)
    synthetic.get.return_value.targets[0].model_dump.return_value = {}
    synthetic.get.return_value.status = "completed"
    synthetic.get.return_value.finished_at = None
    monkeypatch.setattr(synthetic_router, "get_synthetic_service", lambda: synthetic)
    return app, service, synthetic


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
    api: tuple[FastAPI, MagicMock, MagicMock],
    limit: float,
    path: str,
    field: str,
    payload: dict[str, Any],
) -> None:
    app, service, _ = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/nl2sql" + path, json={**payload, field: limit})
    assert response.status_code == 422
    assert service.mock_calls == []


@pytest.mark.parametrize("path", ["/synthetic-data/results", "/synthetic-data/runs/one/results"])
@pytest.mark.parametrize("limit", [0, -1, 1.5, 100001])
async def test_synthetic_invalid_limits_do_not_fetch(
    api: tuple[FastAPI, MagicMock, MagicMock], path: str, limit: float
) -> None:
    app, service, synthetic = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/nl2sql" + path, params={"table_name": "APP.T", "limit": limit}
        )
    assert response.status_code == 422
    service.synthetic_data_results.assert_not_called()
    synthetic.get.assert_not_called()


@pytest.mark.parametrize("limit", [1, 100000])
async def test_synthetic_boundaries_are_not_silently_clamped(
    api: tuple[FastAPI, MagicMock, MagicMock], limit: int
) -> None:
    app, service, synthetic = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ["/synthetic-data/results", "/synthetic-data/runs/one/results"]:
            response = await client.get(
                "/nl2sql" + path, params={"table_name": "APP.T", "limit": limit}
            )
            assert response.status_code == 200, response.text
    service.synthetic_data_results.assert_called_once_with(table_name="APP.T", limit=limit)
    synthetic.adapter.execute_select.assert_called_once_with('SELECT * FROM "APP"."T"', limit)
