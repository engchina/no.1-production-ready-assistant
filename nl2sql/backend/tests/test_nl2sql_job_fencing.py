"""SQL job の旧 worker は接管後の SQL 実行・結果・履歴・失敗を書けない。"""

import threading
from dataclasses import replace
from typing import Any

import pytest
from test_nl2sql_job_runtime import _repository, _request, _worker

from app.features.nl2sql.models import HistoryItem, JobStatus
from app.features.nl2sql.service import JobExecutionLost, StoredJob
from app.settings import get_settings


def expire(repo: Any, job_id: str) -> None:
    repo.patch_document("jobs", job_id, {"lease_expires_at": "2000-01-01T00:00:00Z"})


@pytest.mark.parametrize("reclaim", [False, True])
@pytest.mark.parametrize("operation", ["heartbeat", "stage", "done", "error"])
def test_stale_sql_job_write_is_rejected(
    monkeypatch: pytest.MonkeyPatch, reclaim: bool, operation: str
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repo = _repository()
    old, new = _worker(repo), _worker(repo)
    job = old.start_job(_request())
    claimed = old._claim_nl2sql_job(worker_id="old", job_id=job.job_id)
    assert claimed is not None
    expire(repo, job.job_id)
    if reclaim:
        assert new._claim_nl2sql_job(worker_id="new", job_id=job.job_id) is not None
    before = repo.get_document("jobs", job.job_id)
    with pytest.raises(JobExecutionLost):
        if operation == "stage":
            old._transition_job_steps(job.job_id, running_stage="execute_sql")
        elif operation == "heartbeat":
            old._renew_job_lease_locked(claimed)
            old._persist_job(job.job_id)
        else:
            terminal = replace(
                claimed, status=JobStatus(operation), worker_id="", lease_expires_at=None
            )
            old._persist_job_snapshot(terminal)
    assert repo.get_document("jobs", job.job_id) == before
    assert not repo.list_documents("history", limit=100)


@pytest.mark.parametrize("same_service", [False, True])
@pytest.mark.parametrize("late_failure", [False, True])
def test_reclaimed_sql_job_discards_late_generation_and_returns_new_state(
    monkeypatch: pytest.MonkeyPatch, same_service: bool, late_failure: bool
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repo = _repository()
    old = _worker(repo)
    new = old if same_service else _worker(repo)
    reached, release = threading.Event(), threading.Event()
    original = old._generate_selected_engine
    calls: list[str] = []

    def blocked(**kwargs: Any) -> Any:
        if threading.current_thread().name == "old-worker":
            reached.set()
            assert release.wait(5)
            if late_failure:
                raise RuntimeError("late generation error")
        return original(**kwargs)

    original_execute = old.execute_sql

    def execute(*args: Any, **kwargs: Any) -> Any:
        calls.append(threading.current_thread().name)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(old, "_generate_selected_engine", blocked)
    monkeypatch.setattr(old, "execute_sql", execute)
    job = old.start_job(_request())
    thread = threading.Thread(
        target=old.run_next_nl2sql_job,
        kwargs={"job_id": job.job_id, "worker_id": "old"},
        name="old-worker",
    )
    thread.start()
    try:
        assert reached.wait(5)
        expire(repo, job.job_id)
        assert new.run_next_nl2sql_job(job_id=job.job_id, worker_id="new")
        completed = repo.get_document("jobs", job.job_id)
        assert completed is not None and completed["status"] == "done" and completed["attempt"] == 2
        visible = old.get_job(job.job_id)
        assert visible is not None and visible.status == JobStatus.DONE
        history = repo.list_documents("history", limit=100)
        assert len(history) == 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert "old-worker" not in calls
    assert repo.get_document("jobs", job.job_id) == completed
    assert repo.list_documents("history", limit=100) == history
    visible = old.get_job(job.job_id)
    assert visible is not None and visible.status == JobStatus.DONE


def test_reclaimed_sql_job_restarts_steps_from_the_beginning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1358: 引き継いだ実行は前の実行の段階を残さず、段階を初めから進める。

    前の実行の段階（生成は完了、安全性の確認は実行中）を残すと、引き継いだ実行が準備から
    やり直す間、後の段階が実行中のまま残り、チャットの段階の一覧が前後していた。
    """
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repo = _repository()
    old, new = _worker(repo), _worker(repo)
    job = old.start_job(_request())
    assert old._claim_nl2sql_job(worker_id="old", job_id=job.job_id) is not None
    old._transition_job_steps(job.job_id, running_stage="prepare_context")
    old._transition_job_steps(
        job.job_id, completed_stage="prepare_context", running_stage="generate_sql"
    )
    old._transition_job_steps(
        job.job_id, completed_stage="generate_sql", running_stage="safety_check"
    )
    expire(repo, job.job_id)

    original = new._generate_selected_engine
    seen: dict[str, Any] = {}

    def generate(**kwargs: Any) -> Any:
        seen.update(repo.get_document("jobs", job.job_id) or {})
        return original(**kwargs)

    monkeypatch.setattr(new, "_generate_selected_engine", generate)
    assert new.run_next_nl2sql_job(job_id=job.job_id, worker_id="new")
    assert seen["attempt"] == 2
    assert [(step["stage"], step["status"]) for step in seen["steps"]] == [
        ("prepare_context", "done"),
        ("generate_sql", "running"),
        ("safety_check", "pending"),
        ("execute_sql", "pending"),
        ("format_results", "pending"),
    ]
    visible = new.get_job(job.job_id)
    assert visible is not None and visible.attempt == 2


def test_reclaim_between_final_check_and_commit_cannot_add_old_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repo = _repository()
    old, new = _worker(repo), _worker(repo)
    job = old.start_job(_request())
    original = old._persist_job_snapshot
    completed: dict[str, Any] = {}

    def persist(snapshot: StoredJob, history: HistoryItem | None = None, **kwargs: Any) -> None:
        if history is not None:
            expire(repo, job.job_id)
            assert new.run_next_nl2sql_job(job_id=job.job_id, worker_id="new")
            completed.update(repo.get_document("jobs", job.job_id) or {})
        original(snapshot, history, **kwargs)

    monkeypatch.setattr(old, "_persist_job_snapshot", persist)
    assert old.run_next_nl2sql_job(job_id=job.job_id, worker_id="old")
    assert completed["status"] == "done"
    assert repo.get_document("jobs", job.job_id) == completed
    history = repo.list_documents("history", limit=100)
    assert len(history) == 1 and history[0]["id"] == completed["result"]["history_id"]


def test_history_write_failure_rolls_back_job_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repo = _repository()
    original = repo.put_document

    def fail_history(collection: str, identity: str, payload: Any, **kwargs: Any) -> None:
        if collection == "history":
            raise OSError("history write failure")
        original(collection, identity, payload, **kwargs)

    monkeypatch.setattr(repo, "put_document", fail_history)
    service = _worker(repo)
    job = service.start_job(_request())
    assert service.run_next_nl2sql_job(job_id=job.job_id)
    stored = repo.get_document("jobs", job.job_id)
    assert stored is not None and stored["status"] == "running"
    assert not repo.list_documents("history", limit=100)
    visible = service.get_job(job.job_id)
    assert visible is not None and visible.status == JobStatus.DONE and visible.warning_message
