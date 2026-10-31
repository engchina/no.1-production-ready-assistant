"""State document の所有権付き atomic 更新の memory / Oracle 境界。"""

import json
from contextlib import contextmanager
from typing import Any

import pytest

from app.features.nl2sql.incremental_store import (
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
)


def owned() -> dict[str, Any]:
    return {
        "job_id": "job",
        "status": "running",
        "worker_id": "worker",
        "attempt": 1,
        "lease_expires_at": "2099-01-01T00:00:00+00:00",
        "deadline_at": "2099-01-01T00:00:00+00:00",
    }


class Database:
    def __init__(
        self, payload: dict[str, Any], *, active: bool = False, fail: bool = False
    ) -> None:
        self.payload, self.active, self.fail = payload, active, fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = self.rollbacks = 0

    @contextmanager
    def connection(self) -> Any:
        yield self

    @contextmanager
    def cursor(self) -> Any:
        yield self

    def setinputsizes(self, **_kwargs: Any) -> None:
        pass

    def execute(self, sql: str, binds: Any = None) -> None:
        values = dict(binds or {})
        self.calls.append((sql, values))
        if (
            self.fail
            and sql.startswith("MERGE INTO NL2SQL_STATE_DOCUMENTS")
            and values.get("entity_id") == "new"
        ):
            raise OSError("write failed")

    def fetchone(self) -> Any:
        return json.dumps(self.payload), "", self.payload["status"]

    def fetchall(self) -> Any:
        return [(json.dumps(self.payload),)] if self.active else []

    def fetchmany(self, _size: int) -> Any:
        return [("job", json.dumps(self.payload), "")]

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.parametrize("backend", ["memory", "oracle"])
@pytest.mark.parametrize("mismatch", ["worker", "attempt", "deadline", "lease", "status"])
def test_stale_owner_cannot_mutate_any_document(backend: str, mismatch: str) -> None:
    payload = owned()
    if mismatch == "worker":
        payload["worker_id"] = "other"
    elif mismatch == "attempt":
        payload["attempt"] = 2
    elif mismatch in {"deadline", "lease"}:
        payload["deadline_at" if mismatch == "deadline" else "lease_expires_at"] = (
            "2000-01-01T00:00:00+00:00"
        )
    else:
        payload["status"] = "done"
    db = Database(payload)
    repository: Any = (
        OracleIncrementalNl2SqlRepository(connection_factory=db.connection)
        if backend == "oracle"
        else MemoryIncrementalNl2SqlRepository()
    )
    if backend == "memory":
        repository.put_document("jobs", "job", payload, status=payload["status"])
        repository.put_document("cache", "old", {"value": "keep"})
    saved = repository.patch_document_if_current(
        "jobs",
        "job",
        {"status": "done"},
        expected={"status": "running", "worker_id": "worker", "attempt": 1},
        status="done",
        require_live_lease=True,
        upserts=[("cache", "new", {"value": "new"}, "", "ready")],
        deletes=[("cache", "old")],
    )
    assert saved is None
    if backend == "memory":
        assert repository.get_document("cache", "old") == {"value": "keep"}
        assert repository.get_document("cache", "new") is None
        assert repository.get_document("jobs", "job") == payload
    else:
        assert db.commits == 0 and db.rollbacks == 1
        assert len(db.calls) == 1 and "FOR UPDATE" in db.calls[0][0]


@pytest.mark.parametrize("fail", [False, True])
def test_oracle_cache_and_job_share_one_transaction(fail: bool) -> None:
    db = Database(owned(), fail=fail)
    repository = OracleIncrementalNl2SqlRepository(connection_factory=db.connection)

    def commit() -> Any:
        return repository.patch_document_if_current(
            "jobs",
            "job",
            {"status": "done"},
            expected={"worker_id": "worker", "attempt": 1},
            status="done",
            require_live_lease=True,
            upserts=[("cache", "new", {"value": "new"}, "", "ready")],
            deletes=[("cache", "old")],
        )

    if fail:
        with pytest.raises(OSError):
            commit()
        assert db.commits == 0 and db.rollbacks == 1
    else:
        assert commit()["status"] == "done"
        assert db.commits == 1 and db.rollbacks == 0
    assert "FOR UPDATE" in db.calls[0][0]
    assert any(sql.startswith("DELETE") for sql, _binds in db.calls)


@pytest.mark.parametrize("active", [False, True])
def test_exclusive_oracle_claim_checks_other_live_jobs(active: bool) -> None:
    payload = owned() if active else {"job_id": "job", "status": "pending"}
    db = Database(payload, active=active)
    repository = OracleIncrementalNl2SqlRepository(connection_factory=db.connection)
    claimed = repository.claim_document(
        "profile_jobs", worker_id="worker", lease_seconds=600, entity_id="job", exclusive=True
    )
    assert db.calls[0][0] == "LOCK TABLE NL2SQL_STATE_DOCUMENTS IN EXCLUSIVE MODE"
    if active:
        assert claimed is None
        assert db.rollbacks == 1
        assert not any(sql.startswith("UPDATE") for sql, _binds in db.calls)
    else:
        assert claimed and claimed["worker_id"] == "worker"
        assert db.commits == 1
