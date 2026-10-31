"""進捗取得と claim の競合、長い同期呼出し中の lease 維持を検証する。"""

import threading
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from test_nl2sql_job_runtime import _repository, _request, _worker

from app.features.nl2sql import incremental_store
from app.features.nl2sql import service as service_module
from app.settings import get_settings


@pytest.fixture(autouse=True)
def external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")


@pytest.mark.parametrize("cached", [False, True])
def test_poll_snapshot_cannot_replace_job_claimed_during_read(
    monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    repo = _repository()
    service = _worker(repo)
    job = service.start_job(_request())
    if not cached:
        service._jobs.pop(job.job_id)
    reached, release = threading.Event(), threading.Event()
    original = repo.get_document
    observed: list[Any] = []

    def read(collection: str, identity: str) -> Any:
        result = original(collection, identity)
        if collection == "jobs" and threading.current_thread().name == "poller":
            reached.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(repo, "get_document", read)
    poller = threading.Thread(
        target=lambda: observed.append(service._load_job_record(job.job_id)), name="poller"
    )
    poller.start()
    try:
        assert reached.wait(5)
        claimed = service._claim_nl2sql_job(worker_id="worker", job_id=job.job_id)
        assert claimed is not None
    finally:
        release.set()
        poller.join(5)
    assert not poller.is_alive()
    assert observed[0].status.value == "pending"
    assert service._jobs[job.job_id] is claimed
    assert claimed.execution_owner == ("worker", 1)
    service._run_job_safely(job.job_id)
    completed = repo.get_document("jobs", job.job_id)
    assert completed is not None and completed["status"] == "done"
    assert len(repo.list_documents("history", limit=100)) == 1


@pytest.mark.parametrize("outcome", ["done", "error", "cancel"])
def test_long_generation_renews_lease_and_stops_heartbeat_after_completion(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "Clock":
            return cls.fromtimestamp(cls.current.timestamp(), tz)

    monkeypatch.setattr(service_module, "datetime", Clock)
    monkeypatch.setattr(incremental_store, "datetime", Clock)
    monkeypatch.setattr(get_settings(), "nl2sql_job_lease_seconds", 30.0)
    repo = _repository()
    service, other = _worker(repo), _worker(repo)
    monkeypatch.setattr(service, "_job_heartbeat_interval_seconds", lambda: 0.01)
    entered, release = threading.Event(), threading.Event()
    condition = threading.Condition()
    ticks: list[tuple[datetime, bool]] = []
    original_generate = service._generate_selected_engine
    original_heartbeat = service._heartbeat_job

    def generate(**kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        if outcome == "error":
            raise RuntimeError("generation failed")
        return original_generate(**kwargs)

    def heartbeat(job_id: str, owner: tuple[str, int]) -> bool:
        # 仮想時計の更新と tick の記録を同期し、実際の30秒待機なしで複数 lease を跨ぐ。
        with condition:
            saved = original_heartbeat(job_id, owner)
            ticks.append((Clock.current, saved))
            condition.notify_all()
            return saved

    monkeypatch.setattr(service, "_generate_selected_engine", generate)
    monkeypatch.setattr(service, "_heartbeat_job", heartbeat)
    job = service.start_job(_request())
    runner = threading.Thread(target=service.run_next_nl2sql_job, kwargs={"job_id": job.job_id})
    runner.start()
    try:
        assert entered.wait(5)
        initial = repo.get_document("jobs", job.job_id)
        assert initial is not None
        for _ in range(3):
            with condition:
                Clock.current += timedelta(seconds=20)
                assert condition.wait_for(lambda: (Clock.current, True) in ticks, timeout=2)
            current = repo.get_document("jobs", job.job_id)
            assert current is not None
            assert datetime.fromisoformat(current["lease_expires_at"]) > Clock.current
            assert current["steps"] == initial["steps"]
            assert other._claim_nl2sql_job(worker_id="other", job_id=job.job_id) is None
        assert Clock.current > datetime.fromisoformat(initial["lease_expires_at"])
        if outcome == "cancel":
            service.request_job_cancel(job.job_id)
    finally:
        release.set()
        runner.join(5)
    assert not runner.is_alive()
    assert not any(
        thread.name == f"nl2sql-job-heartbeat-{job.job_id}" for thread in threading.enumerate()
    )
    completed = repo.get_document("jobs", job.job_id)
    assert completed is not None
    assert completed["status"] == ("done" if outcome == "done" else "error")
    if outcome == "cancel":
        assert completed["error_code"] == service_module.JOB_CANCELLED_ERROR_CODE
    assert completed["attempt"] == 1
    # 完了と停止（#3348。停止の行）は実行履歴に残し、失敗は残さない。
    history = repo.list_documents("history", limit=100)
    assert len(history) == (0 if outcome == "error" else 1)
    if outcome == "cancel":
        assert history[0]["run_status"] == "cancelled"


@pytest.mark.parametrize("state", ["expired", "reclaimed", "done", "error"])
def test_heartbeat_cannot_revive_expired_or_replaced_execution(state: str) -> None:
    repo = _repository()
    old, new = _worker(repo), _worker(repo)
    job = old.start_job(_request())
    assert old._claim_nl2sql_job(worker_id="old", job_id=job.job_id)
    if state in {"expired", "reclaimed"}:
        repo.patch_document("jobs", job.job_id, {"lease_expires_at": "2000-01-01T00:00:00Z"})
        if state == "reclaimed":
            assert new._claim_nl2sql_job(worker_id="new", job_id=job.job_id)
            # 新所有者の cache に置換されても heartbeat は元の claim に固定される。
            old._jobs[job.job_id] = new._jobs[job.job_id]
    else:
        repo.patch_document("jobs", job.job_id, {"status": state})
    before = repo.get_document("jobs", job.job_id)
    assert not old._heartbeat_job(job.job_id, ("old", 1))
    assert repo.get_document("jobs", job.job_id) == before


def test_heartbeat_retries_transient_store_failure_and_stops_on_ownership_loss(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _repository()
    service = _worker(repo)
    job = service.start_job(_request())
    assert service._claim_nl2sql_job(worker_id="worker", job_id=job.job_id)
    monkeypatch.setattr(service, "_job_heartbeat_interval_seconds", lambda: 0.01)
    original = service._heartbeat_job
    recovered, stopped = threading.Event(), threading.Event()
    attempts = 0

    def heartbeat(job_id: str, owner: tuple[str, int]) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary repository failure")
        if attempts == 2:
            assert original(job_id, owner)
            recovered.set()
            return True
        repo.patch_document("jobs", job_id, {"lease_expires_at": "2000-01-01T00:00:00Z"})
        assert not original(job_id, owner)
        stopped.set()
        return False

    monkeypatch.setattr(service, "_heartbeat_job", heartbeat)
    with service._keep_job_lease_alive(job.job_id):
        assert recovered.wait(5)
        assert stopped.wait(5)
    assert attempts == 3
    assert "nl2sql_job_heartbeat_failed" in caplog.text
    assert not any(
        thread.name == f"nl2sql-job-heartbeat-{job.job_id}" for thread in threading.enumerate()
    )
