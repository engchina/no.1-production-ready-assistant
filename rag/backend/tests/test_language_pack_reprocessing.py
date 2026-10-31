"""再処理は明示したレシピ・工程・公開版・改訂だけを受け付ける。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from rag_parser_core.language import builtin_pack

from app.api.routes import language_pack_reprocessing as routes
from app.clients.oracle import OracleClient
from app.main import app
from app.schemas.language_pack_release import LanguagePackRelease
from tests.support import AsgiTestClient
from tests.test_language_pack_releases import release

client = AsgiTestClient(app)
PREFIX = "/api/documents/language-pack/"


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> LanguagePackRelease:
    value = release(builtin_pack())
    store = SimpleNamespace(current=AsyncMock(return_value=value))
    monkeypatch.setattr(routes, "initialized_release_store", AsyncMock(return_value=store))
    return value


def target(document: str = "document-a", recipe: str = "recipe-a") -> dict[str, object]:
    return {
        "document_id": document,
        "recipe_id": recipe,
        "phase": "CHUNK",
        "expected_config_revision": 3,
    }


def test_plan_deduplicates_documents_and_returns_real_recipe_targets(
    monkeypatch: pytest.MonkeyPatch, published: LanguagePackRelease
) -> None:
    lookup = AsyncMock(
        return_value=[
            {
                "document_id": "document-a",
                "file_name": "synthetic.pdf",
                "recipe_id": "recipe-b",
                "slot_no": 2,
                "config_revision": 3,
            }
        ]
    )
    monkeypatch.setattr(
        routes, "OracleClient", lambda: SimpleNamespace(list_language_reprocessing_targets=lookup)
    )
    response = client.post(
        PREFIX + "reprocessing-targets",
        json={
            "document_ids": ["document-a", "document-a", "unavailable"],
            "expected_release_id": published.id,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["unavailable_count"] == 1
    assert data["targets"][0]["recipe_id"] == "recipe-b"
    lookup.assert_awaited_once_with(["document-a", "unavailable"])


def test_stale_release_creates_no_job(
    monkeypatch: pytest.MonkeyPatch, published: LanguagePackRelease
) -> None:
    enqueue = AsyncMock()
    monkeypatch.setattr(routes, "_enqueue_ingestion_job_for_recipe", enqueue)
    response = client.post(
        PREFIX + "reprocessing-jobs",
        json={
            "expected_release_id": "previous",
            "targets": [target()],
        },
    )
    assert response.status_code == 409
    enqueue.assert_not_awaited()


def test_jobs_preserve_phase_revision_and_partial_results(
    monkeypatch: pytest.MonkeyPatch, published: LanguagePackRelease
) -> None:
    enqueue = AsyncMock(
        side_effect=[
            SimpleNamespace(id="job-a"),
            KeyError("private"),
            ValueError("private"),
            HTTPException(422, "private"),
            RuntimeError("private"),
        ]
    )
    monkeypatch.setattr(routes, "_enqueue_ingestion_job_for_recipe", enqueue)
    response = client.post(
        PREFIX + "reprocessing-jobs",
        json={
            "expected_release_id": published.id,
            "targets": [target(recipe=f"recipe-{i}") for i in range(5)],
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["accepted_count"], data["rejected_count"]) == (1, 4)
    assert data["items"][0]["job_id"] == "job-a"
    assert "private" not in response.text
    for call in enqueue.await_args_list:
        assert call.kwargs == {
            "phase": "CHUNK",
            "expected_language_pack_release_id": published.id,
            "expected_config_revision": 3,
        }


def test_duplicate_recipe_is_rejected_before_any_job(
    monkeypatch: pytest.MonkeyPatch, published: LanguagePackRelease
) -> None:
    enqueue = AsyncMock()
    monkeypatch.setattr(routes, "_enqueue_ingestion_job_for_recipe", enqueue)
    response = client.post(
        PREFIX + "reprocessing-jobs",
        json={
            "expected_release_id": published.id,
            "targets": [target(), target()],
        },
    )
    assert response.status_code == 422
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("field", ["phase", "expected_config_revision"])
def test_jobs_require_explicit_phase_and_revision(
    field: str, published: LanguagePackRelease
) -> None:
    item = target()
    item.pop(field)
    response = client.post(
        PREFIX + "reprocessing-jobs",
        json={
            "expected_release_id": published.id,
            "targets": [item],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_plan_uses_one_authorized_batch_query(monkeypatch: pytest.MonkeyPatch) -> None:
    query = AsyncMock(return_value=[])
    oracle = OracleClient()
    monkeypatch.setattr(oracle, "_fetch_all", query)
    assert await oracle.list_language_reprocessing_targets(["a", "b", "a"]) == []
    query.assert_awaited_once()
    assert query.await_args is not None
    sql, binds = query.await_args.args
    assert "JSON_TABLE" in sql
    assert "rag_document_recipes" in sql
    # 認可文脈の無い直接呼出しでは、全件を読まずに拒否する。
    assert "1 = 0" in sql
    assert binds["selected_ids"] == '["a", "b"]'
