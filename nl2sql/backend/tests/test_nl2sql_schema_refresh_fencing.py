"""Schema refresh の lease 接管後に旧 worker の書込みを拒否する。"""

import json
import threading
from typing import Any

import pytest
from test_nl2sql_incremental_state import _incremental_service
from test_nl2sql_state_document_fencing import Database

from app.features.nl2sql.incremental_store import (
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
    SchemaRefreshExecutionLost,
)
from app.features.nl2sql.models import SchemaCatalog, SchemaRefreshJob


def claimed_pair() -> tuple[MemoryIncrementalNl2SqlRepository, SchemaRefreshJob, SchemaRefreshJob]:
    repo = MemoryIncrementalNl2SqlRepository()
    repo.save_refresh_job(SchemaRefreshJob(job_id="refresh", created_at="2026-09-15T00:00:00Z"))
    old = repo.claim_refresh_job(worker_id="old", lease_seconds=300)
    assert old is not None
    repo.save_refresh_job(old.model_copy(update={"lease_expires_at": "2000-01-01T00:00:00Z"}))
    current = repo.claim_refresh_job(worker_id="new", lease_seconds=300)
    assert current is not None
    return repo, old, current


@pytest.mark.parametrize("action", ["heartbeat", "progress", "done", "error", "catalog"])
def test_old_worker_cannot_change_reclaimed_job_or_catalog(action: str) -> None:
    repo, old, current = claimed_pair()
    before = repo.load_catalog(), repo.get_catalog_head(), repo.schema_manifest()
    with pytest.raises(SchemaRefreshExecutionLost):
        if action == "heartbeat":
            _incremental_service(repo)._heartbeat_schema_refresh_job(repo, old)
        elif action == "catalog":
            repo.apply_schema_refresh(
                catalog=SchemaCatalog(refreshed_at="late", tables=[]),
                manifest={},
                changed_keys=set(),
                deleted_keys=set(),
                execution=old,
            )
        else:
            repo.save_refresh_job(
                old.model_copy(
                    update={"phase": "persisting"} if action == "progress" else {"status": action}
                ),
                expected_owner=old,
            )
    assert repo.get_refresh_job(old.job_id) == current
    assert (repo.load_catalog(), repo.get_catalog_head(), repo.schema_manifest()) == before
    renewed = _incremental_service(repo)._heartbeat_schema_refresh_job(repo, current)
    assert renewed.worker_id == "new" and renewed.attempt == 2


@pytest.mark.parametrize("late_error", [False, True])
def test_late_manifest_cannot_advance_or_fail_new_worker(
    monkeypatch: pytest.MonkeyPatch, late_error: bool
) -> None:
    repo = MemoryIncrementalNl2SqlRepository()
    service = _incremental_service(repo)
    entered, release = threading.Event(), threading.Event()

    class Adapter:
        def fetch_schema_manifest(self, _keys: Any) -> dict[tuple[str, str], str]:
            entered.set()
            assert release.wait(5)
            if late_error:
                raise OSError("late manifest failure")
            return {}

    monkeypatch.setattr(service, "_oracle_adapter", Adapter())
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    pending = service.start_schema_refresh_job(dispatch=False)
    thread = threading.Thread(target=service._run_schema_refresh_job, args=(pending.job_id,))
    thread.start()
    try:
        assert entered.wait(5)
        old = repo.get_refresh_job(pending.job_id)
        assert old is not None
        repo.save_refresh_job(old.model_copy(update={"lease_expires_at": "2000-01-01T00:00:00Z"}))
        current = repo.claim_refresh_job(worker_id="new", lease_seconds=300)
        assert current is not None
        before = repo.load_catalog(), repo.get_catalog_head()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert repo.get_refresh_job(pending.job_id) == current
    assert (repo.load_catalog(), repo.get_catalog_head()) == before


class SchemaDatabase(Database):
    def fetchone(self) -> Any:
        return (json.dumps(self.payload),)


@pytest.mark.parametrize("action", ["save", "catalog"])
@pytest.mark.parametrize("stale", ["worker", "attempt", "lease"])
def test_oracle_checks_owner_before_any_mutation(action: str, stale: str) -> None:
    _, old, current = claimed_pair()
    payload = old.model_dump(mode="json")
    payload.update(
        {"worker_id": current.worker_id}
        if stale == "worker"
        else (
            {"attempt": current.attempt}
            if stale == "attempt"
            else {"lease_expires_at": "2000-01-01T00:00:00Z"}
        )
    )
    db = SchemaDatabase(payload)
    repo = OracleIncrementalNl2SqlRepository(connection_factory=db.connection)
    with pytest.raises(SchemaRefreshExecutionLost):
        if action == "save":
            repo.save_refresh_job(old, expected_owner=old)
        else:
            repo.apply_schema_refresh(
                catalog=SchemaCatalog(refreshed_at="late", tables=[]),
                manifest={},
                changed_keys=set(),
                deleted_keys=set(),
                execution=old,
            )
    assert len(db.calls) == 1
    assert "NL2SQL_SCHEMA_REFRESH_JOBS" in db.calls[0][0]
    assert "FOR UPDATE" in db.calls[0][0]
    assert db.commits == 0 and db.rollbacks == 1


def test_oracle_valid_owner_saves_after_lock_in_one_transaction() -> None:
    _, old, _ = claimed_pair()
    db = SchemaDatabase(old.model_dump(mode="json"))
    repo = OracleIncrementalNl2SqlRepository(connection_factory=db.connection)
    assert repo.save_refresh_job(old, expected_owner=old) == old
    assert "FOR UPDATE" in db.calls[0][0]
    assert db.calls[1][0].startswith("MERGE INTO NL2SQL_SCHEMA_REFRESH_JOBS")
    assert db.commits == 1 and db.rollbacks == 0
