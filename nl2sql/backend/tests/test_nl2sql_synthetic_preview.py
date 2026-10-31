"""確認・再送・認可・破棄の境界。Oracle transaction は live test でも検証する。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from test_nl2sql_synthetic_runs import actor, request
from test_nl2sql_synthetic_runs import service as service_fixture

from app.features.nl2sql.synthetic_models import SyntheticRun
from app.features.nl2sql.synthetic_preview import ordered_tables

service = service_fixture


def ready(service: Any, **updates: Any) -> SyntheticRun:
    run: SyntheticRun = service.create(request(), actor())
    run.status, run.review_status = "completed", "ready"
    run.finished_at = datetime.now(UTC).isoformat()
    run.staging = {"APP.T": {"name": "APP.NL2SQL_SP_FIXTURE", "checksum": "digest", "count": 1}}
    for key, value in updates.items():
        setattr(run, key, value)
    assert service.store.save(run)
    return run


def test_new_runs_cannot_opt_out_of_preview(service: Any) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        request(preview=False)
    run: SyntheticRun = service.create(request(), actor())
    assert run.preview and run.review_status == "pending"
    assert "staging" not in run.public()


def test_generation_only_uses_persisted_staging_targets(service: Any) -> None:
    run: SyntheticRun = service.create(request(), actor())
    run.status = "running"
    service.store.save(run)
    service.execute(run.run_id)
    called = service.adapter.generate_synthetic_data.call_args.kwargs
    assert called["staging"] is True
    assert called["table_name"].startswith("APP.NL2SQL_SP_")
    assert called["table_name"] == service.store.get(run.run_id).staging["APP.T"]["name"]
    assert called["table_name"] != "APP.T"


def test_apply_requires_preview_confirmation_and_is_idempotent(
    service: Any, monkeypatch: Any
) -> None:
    run = ready(service)
    insert = Mock()
    monkeypatch.setattr(service.preview, "apply", insert)
    for confirmation, previews in [
        ("wrong", {"APP.T": "digest"}),
        ("APP.T", {}),
        ("APP.T", {"APP.T": "old"}),
    ]:
        with pytest.raises(HTTPException):
            service.review(run.run_id, actor(), confirmation=confirmation, previews=previews)
        insert.assert_not_called()
    first = service.review(run.run_id, actor(), confirmation="APP.T", previews={"APP.T": "digest"})
    again = service.review(run.run_id, actor(), confirmation="APP.T", previews={"APP.T": "digest"})
    assert first.review_status == again.review_status == "applied"
    assert first.applied_at == again.applied_at
    insert.assert_called_once()
    service.adapter.generate_synthetic_data.assert_not_called()
    with pytest.raises(HTTPException):
        service.review(run.run_id, actor(), discard=True)


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "running"},
        {"status": "partial"},
        {"review_status": "discarded"},
        {"preview": False},
        {"finished_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()},
    ],
)
def test_invalid_states_never_apply(service: Any, monkeypatch: Any, updates: Any) -> None:
    run = ready(service, **updates)
    insert = Mock()
    monkeypatch.setattr(service.preview, "apply", insert)
    with pytest.raises(HTTPException):
        service.review(run.run_id, actor(), confirmation="APP.T", previews={"APP.T": "digest"})
    insert.assert_not_called()


def test_review_reauthorizes_owner_and_profile(service: Any, monkeypatch: Any) -> None:
    run = ready(service)
    insert = Mock()
    monkeypatch.setattr(service.preview, "apply", insert)
    with pytest.raises(HTTPException) as error:
        service.review(
            run.run_id, actor("another"), confirmation="APP.T", previews={"APP.T": "digest"}
        )
    assert error.value.status_code == 404
    current = service.store.get(run.run_id)
    current.request["profile_id"] = "revoked"
    service.store.save(current)
    with pytest.raises(HTTPException) as error:
        service.review(run.run_id, actor(), confirmation="APP.T", previews={"APP.T": "digest"})
    assert error.value.status_code == 403
    insert.assert_not_called()


def test_apply_failure_keeps_ready_receipt(service: Any, monkeypatch: Any) -> None:
    run = ready(service)
    monkeypatch.setattr(service.preview, "apply", Mock(side_effect=RuntimeError("constraint")))
    with pytest.raises(HTTPException):
        service.review(run.run_id, actor(), confirmation="APP.T", previews={"APP.T": "digest"})
    assert service.store.get(run.run_id).review_status == "ready"
    assert service.store.get(run.run_id).applied_at is None


def test_discard_retries_cleanup_without_purging_receipt(service: Any, monkeypatch: Any) -> None:
    run = ready(service)
    cleanup = Mock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(service.preview, "cleanup", cleanup)
    assert service.review(run.run_id, actor(), discard=True).review_status == "discarded"
    assert service.store.get(run.run_id).staging
    old = service.store.get(run.run_id)
    old.finished_at = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    service.store.save(old)
    cleanup.side_effect = None
    service.tick()
    # 一時表だけを片付け、生成の記録は残す（#3235）。
    kept = service.store.get(run.run_id)
    assert kept is not None and kept.staging == {} and kept.review_status == "discarded"
    assert service.store.list_cleanup_candidates(service.context) == []
    service.adapter.generate_synthetic_data.assert_not_called()


def test_review_window_expiry_keeps_record_read_only(service: Any, monkeypatch: Any) -> None:
    """確認用データの期間（24 時間）を過ぎた記録は残し、適用・破棄だけを断る（#3235）。"""
    run = ready(service, finished_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    insert = Mock()
    monkeypatch.setattr(service.preview, "apply", insert)
    for kwargs in (
        {"confirmation": "APP.T", "previews": {"APP.T": "digest"}},
        {"discard": True},
    ):
        with pytest.raises(HTTPException) as error:
            service.review(run.run_id, actor(), **kwargs)
        assert error.value.status_code == 409
        assert "24時間" in error.value.detail
    insert.assert_not_called()
    kept = service.get(run.run_id, actor())
    assert kept.public()["review_expired"] is True
    assert [item.run_id for item in service.list(actor())] == [run.run_id]


def test_fk_order_and_cycle_rejection() -> None:
    plan: dict[str, dict[str, Any]] = {
        "APP.CHILD": {"metadata": {"constraints": [{"kind": "R", "reference": "APP.PARENT"}]}},
        "APP.PARENT": {"metadata": {"constraints": []}},
    }
    assert ordered_tables(plan) == ["APP.PARENT", "APP.CHILD"]
    plan["APP.PARENT"]["metadata"]["constraints"] = [{"kind": "R", "reference": "APP.CHILD"}]
    with pytest.raises(HTTPException):
        ordered_tables(plan)


@pytest.mark.asyncio
async def test_review_api_checks_preview_ownership_and_confirmation(
    service: Any, monkeypatch: Any
) -> None:
    import httpx
    from fastapi import FastAPI, Request

    from app.features.nl2sql import synthetic_router

    app = FastAPI()
    current = actor()

    @app.middleware("http")
    async def authenticate(req: Request, call_next: Any) -> Any:
        req.state.principal = current
        return await call_next(req)

    app.include_router(synthetic_router.router, prefix="/api/nl2sql")
    monkeypatch.setattr(synthetic_router, "get_synthetic_service", lambda: service)
    run = ready(service)
    monkeypatch.setattr(service.preview, "apply", Mock())
    monkeypatch.setattr(
        service.preview,
        "results",
        Mock(
            return_value={
                "preview_digest": "digest",
                "run_id": run.run_id,
                "table_name": "APP.T",
                "results": {"columns": ["ID"], "rows": [{"ID": 1}], "total": 1},
            }
        ),
    )
    path = f"/api/nl2sql/synthetic-data/runs/{run.run_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(path + "/apply", json={"confirmation": "APP.T", "previews": {}})
        ).status_code == 409
        result = await client.get(path + "/results?table_name=APP.T")
        assert result.status_code == 200
        checksum = result.json()["data"]["preview_digest"]
        payload = {"confirmation": "APP.T", "previews": {"APP.T": checksum}}
        current = actor("other")
        assert (await client.get(path + "/results?table_name=APP.T")).status_code == 404
        assert (await client.post(path + "/apply", json=payload)).status_code == 404
        assert (await client.post(path + "/discard")).status_code == 404
        current = actor()
        assert (await client.post(path + "/apply", json=payload)).json()["data"][
            "review_status"
        ] == "applied"
        assert (await client.post(path + "/apply", json=payload)).status_code == 200
        assert (await client.post(path + "/discard")).status_code == 409
        assert "staging" not in (await client.get(path)).json()["data"]
    service.preview.apply.assert_called_once()


@pytest.mark.parametrize("stopped", [False, True])
def test_recovery_seals_only_after_original_oracle_execution_stops(
    service: Any, monkeypatch: Any, stopped: bool
) -> None:
    from app.features.nl2sql import synthetic_service

    run = service.create(request(), actor())
    run.status = "unknown"
    run.session = {"sid": 7, "serial": 9}
    assert service.store.save(run)

    def inspect(_: Any, value: SyntheticRun) -> SyntheticRun:
        value.status = "completed"
        return value

    monkeypatch.setattr(synthetic_service, "inspect_operation", inspect)
    monkeypatch.setattr(service.preview, "execution_stopped", lambda _: stopped)
    seal = Mock()
    monkeypatch.setattr(service.preview, "seal", seal)
    service.reconcile(service.store.get(run.run_id))
    latest = service.store.get(run.run_id)
    assert latest.status == ("completed" if stopped else "unknown")
    assert seal.call_count == int(stopped)
