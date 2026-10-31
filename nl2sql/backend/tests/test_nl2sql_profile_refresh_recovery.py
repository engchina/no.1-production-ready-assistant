"""DB Profile 一覧更新の多重実行・遅着・atomic 保存。"""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_nl2sql_db_admin_management import _MutableSelectAiProfileAdapter
from test_nl2sql_incremental_state import _incremental_service

from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.service import (
    _SELECT_AI_DB_PROFILE_COLLECTION as PROFILES,
)
from app.features.nl2sql.service import (
    _SELECT_AI_DB_PROFILE_REFRESH_JOB_COLLECTION as JOBS,
)
from app.features.nl2sql.service import (
    _SELECT_AI_DB_PROFILE_REFRESH_META_COLLECTION as META,
)


def setup_pair(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Any]:
    repository = MemoryIncrementalNl2SqlRepository()
    adapter = _MutableSelectAiProfileAdapter({"FRESH": {}})
    first, second = _incremental_service(repository), _incremental_service(repository)
    for service in (first, second):
        monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
        monkeypatch.setattr(service, "_oracle_adapter", adapter)
    repository.put_document(
        PROFILES,
        "OLD",
        {"name": "OLD", "status": "available", "attributes": {}},
        status="available",
    )
    return repository, adapter, first, second


@pytest.mark.parametrize("same_job", [True, False])
def test_only_one_profile_refresh_executes_across_services(
    monkeypatch: pytest.MonkeyPatch, same_job: bool
) -> None:
    repo, adapter, first, second = setup_pair(monkeypatch)
    started, release = threading.Event(), threading.Event()
    original = adapter.fetch_select_ai_profile_names

    def blocked(names: Any = None) -> set[str]:
        result = original(names)
        started.set()
        assert release.wait(5)
        return set(result)

    monkeypatch.setattr(adapter, "fetch_select_ai_profile_names", blocked)
    job = first.start_select_ai_db_profile_refresh_job(dispatch=False)
    other_id = (
        job.job_id
        if same_job
        else second.start_select_ai_db_profile_refresh_job(dispatch=False).job_id
    )
    thread = threading.Thread(
        target=first._run_select_ai_db_profile_refresh_job, args=(job.job_id,)
    )
    thread.start()
    try:
        assert started.wait(5)
        assert not second._run_select_ai_db_profile_refresh_job(other_id)
        assert len(adapter.name_fetch_calls) == 1
    finally:
        release.set()
        thread.join(5)
    assert repo.get_document(JOBS, job.job_id)["status"] == "done"
    assert repo.get_document(PROFILES, "FRESH")
    assert repo.get_document(PROFILES, "OLD") is None
    if not same_job:
        assert second._run_select_ai_db_profile_refresh_job(other_id)


@pytest.mark.parametrize("late_failure", [False, True])
def test_late_refresh_cannot_overwrite_new_cache(
    monkeypatch: pytest.MonkeyPatch, late_failure: bool
) -> None:
    repo, adapter, first, second = setup_pair(monkeypatch)
    reached, release = threading.Event(), threading.Event()
    original = adapter.get_select_ai_profile_detail

    def blocked(**kwargs: Any) -> dict[str, Any]:
        detail = original(**kwargs)
        reached.set()
        assert release.wait(5)
        if late_failure:
            raise OSError("late result failure")
        return dict(detail)

    monkeypatch.setattr(adapter, "get_select_ai_profile_detail", blocked)
    job = first.start_select_ai_db_profile_refresh_job(dispatch=False)
    thread = threading.Thread(
        target=first._run_select_ai_db_profile_refresh_job, args=(job.job_id,)
    )
    thread.start()
    try:
        assert reached.wait(5)
        repo.patch_document(JOBS, job.job_id, {"deadline_at": "2000-01-01T00:00:00+00:00"})
        assert (
            second.get_select_ai_db_profile_refresh_job(job.job_id).error_code
            == "profile_list_refresh_timeout"
        )
        new_adapter = _MutableSelectAiProfileAdapter({"NEWEST": {}})
        monkeypatch.setattr(second, "_oracle_adapter", new_adapter)
        fresh = second.start_select_ai_db_profile_refresh_job(dispatch=False)
        assert second._run_select_ai_db_profile_refresh_job(fresh.job_id)
        cache = repo.list_documents(PROFILES, limit=100)
        head = repo.get_document(META, "head")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert repo.list_documents(PROFILES, limit=100) == cache
    assert repo.get_document(META, "head") == head
    assert repo.get_document(JOBS, job.job_id)["status"] == "error"
    assert repo.get_document(JOBS, fresh.job_id)["status"] == "done"


def test_commit_failure_keeps_previous_cache_and_finishes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _adapter, first, _second = setup_pair(monkeypatch)
    previous = repo.list_documents(PROFILES, limit=100)
    job = first.start_select_ai_db_profile_refresh_job(dispatch=False)
    put = repo.put_document

    def failing(collection: str, *args: Any, **kwargs: Any) -> None:
        if collection == PROFILES:
            raise OSError("atomic write failure")
        put(collection, *args, **kwargs)

    monkeypatch.setattr(repo, "put_document", failing)
    assert not first._run_select_ai_db_profile_refresh_job(job.job_id)
    assert repo.get_document(JOBS, job.job_id)["status"] == "error"
    assert repo.list_documents(PROFILES, limit=100) == previous
    assert repo.get_document(META, "head") is None


@pytest.mark.parametrize("status", ["pending", "running"])
def test_legacy_expired_job_recovers_without_fetch(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    repo, adapter, first, second = setup_pair(monkeypatch)
    job = first.start_select_ai_db_profile_refresh_job(dispatch=False)
    old = repo.get_document(JOBS, job.job_id)
    for key in ("deadline_at", "worker_id", "attempt", "lease_expires_at"):
        old.pop(key)
    old.update(status=status, created_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    repo.put_document(JOBS, job.job_id, old, status=status)
    assert second.get_select_ai_db_profile_refresh_job(job.job_id).status.value == "error"
    assert not adapter.name_fetch_calls
    assert repo.get_document(PROFILES, "OLD")


def test_shutdown_does_not_interrupt_another_services_live_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, adapter, first, second = setup_pair(monkeypatch)
    reached, release = threading.Event(), threading.Event()
    original = adapter.fetch_select_ai_profile_names

    def blocked(names: Any = None) -> set[str]:
        reached.set()
        assert release.wait(5)
        return set(original(names))

    monkeypatch.setattr(adapter, "fetch_select_ai_profile_names", blocked)
    job = first.start_select_ai_db_profile_refresh_job(dispatch=True)
    try:
        assert reached.wait(5)
        second.shutdown_select_ai_db_profile_refresh_jobs()
        assert repo.get_document(JOBS, job.job_id)["status"] == "running"
        first.shutdown_select_ai_db_profile_refresh_jobs()
        assert (
            repo.get_document(JOBS, job.job_id)["error_code"] == "profile_list_refresh_interrupted"
        )
    finally:
        release.set()
        for _ in range(500):
            if job.job_id not in first._profile_list_refresh_dispatching_job_ids:
                break
            time.sleep(0.01)
    assert job.job_id not in first._profile_list_refresh_dispatching_job_ids
    assert repo.get_document(PROFILES, "OLD")
