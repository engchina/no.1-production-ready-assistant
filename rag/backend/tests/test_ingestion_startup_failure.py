"""claim 前の起動失敗、待ち時間と後続 job の進行の回帰。"""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

import pytest

from app.clients.oracle import OracleClient
from app.config import get_settings
from app.rag import ingestion_worker as iw
from app.schemas.document import IngestionJob, IngestionJobPhase, IngestionJobStatus
from tests.test_oracle_adapter import FakeOracleCursor, FakeOraclePool, _llm_settings, _run_inline


def _job(job_id: str, *, attempts: int = 0, recipe_id: str | None = None) -> IngestionJob:
    return IngestionJob(
        id=job_id,
        document_id=f"doc-{job_id}",
        recipe_id=recipe_id,
        status=IngestionJobStatus.QUEUED,
        phase=IngestionJobPhase.PREPROCESS,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
        attempt_count=attempts,
        max_attempts=3,
    )


class _Queue:
    def __init__(self, jobs: list[IngestionJob]) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.record_error = False
        self.exclusions: list[set[str]] = []

    async def get_ingestion_job(self, job_id: str) -> IngestionJob:
        return self.jobs[job_id]

    async def record_ingestion_startup_failure(
        self, job: IngestionJob, *, expected_attempt_count: int
    ) -> bool:
        if self.record_error:
            raise RuntimeError("synthetic record failure")
        current = self.jobs[job.id]
        if (
            current.status != IngestionJobStatus.QUEUED
            or current.attempt_count != expected_attempt_count
        ):
            return False
        attempts = current.attempt_count + 1
        self.jobs[job.id] = current.model_copy(
            update={
                "attempt_count": attempts,
                "status": IngestionJobStatus.FAILED
                if attempts >= current.max_attempts
                else IngestionJobStatus.QUEUED,
            }
        )
        return True

    async def list_dispatchable_ingestion_jobs(
        self,
        *,
        limit: int,
        exclude_job_ids: Sequence[str],
        exclude_phases: Collection[IngestionJobPhase],
    ) -> list[IngestionJob]:
        self.exclusions.append(set(exclude_job_ids))
        return [
            job
            for job in self.jobs.values()
            if job.status == IngestionJobStatus.QUEUED
            and job.id not in exclude_job_ids
            and job.phase not in exclude_phases
        ][:limit]


async def _drain(worker: iw.IngestionQueueWorker) -> int:
    count = await worker._dispatch_available()
    await asyncio.gather(*worker._tasks)
    return count


async def test_preclaim_failure_backs_off_counts_attempts_and_allows_following_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """route import に依存せず失敗を記録し、待ち時間の間は次の job を実行する。"""
    queue = _Queue([_job("bad"), _job("good")])
    monkeypatch.setattr(iw, "OracleClient", lambda: queue)
    now = [0.0]
    calls: list[str] = []

    async def runner(job_id: str) -> None:
        calls.append(job_id)
        if job_id == "bad":
            raise iw.IngestionJobSubprocessError("synthetic preclaim failure")
        queue.jobs[job_id] = queue.jobs[job_id].model_copy(
            update={"status": IngestionJobStatus.SUCCEEDED}
        )

    worker = iw.IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        concurrency=1,
        poll_interval_seconds=2,
        monotonic_clock=lambda: now[0],
    )
    original_import = builtins.__import__

    def no_routes(name: str, *args: object, **kwargs: object) -> object:
        if name == "app.api.routes.documents":
            raise ModuleNotFoundError("synthetic unavailable route dependency")
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", no_routes)
        assert await _drain(worker) == 1
        assert queue.jobs["bad"].attempt_count == 1
        assert await _drain(worker) == 1
        assert calls == ["bad", "good"]
        assert "bad" in queue.exclusions[-1]
        assert await _drain(worker) == 0
        now[0] = 2
        assert await _drain(worker) == 1
        assert queue.jobs["bad"].attempt_count == 2
        now[0] = 5
        assert await _drain(worker) == 0
        now[0] = 6
        assert await _drain(worker) == 1
        assert queue.jobs["bad"].status == IngestionJobStatus.FAILED
        now[0] = 60
        assert await _drain(worker) == 0
    assert calls == ["bad", "good", "bad", "bad"]


async def test_record_failure_also_triggers_global_cooldown_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DB の失敗記録が壊れていても、別 job への切替で全体の休止を迂回しない。"""
    queue = _Queue([_job(f"bad-{i}") for i in range(6)])
    queue.record_error = True
    monkeypatch.setattr(iw, "OracleClient", lambda: queue)
    now = [0.0]
    calls: list[str] = []
    broken = [True]

    async def runner(job_id: str) -> None:
        calls.append(job_id)
        if broken[0]:
            raise iw.IngestionJobSubprocessError("synthetic startup failure")
        queue.jobs[job_id] = queue.jobs[job_id].model_copy(
            update={"status": IngestionJobStatus.SUCCEEDED}
        )

    worker = iw.IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        concurrency=1,
        poll_interval_seconds=2,
        monotonic_clock=lambda: now[0],
    )
    for _ in range(3):
        assert await _drain(worker) == 1
    # finally が WAKEUP を立てても dispatch の休止判定は残る。
    assert iw._WAKEUP.is_set()
    for _ in range(10):
        assert await _drain(worker) == 0
    assert len(calls) == 3
    assert "ingestion_worker_startup_paused" in caplog.text
    assert "synthetic record failure" not in caplog.text
    now[0] = 2
    broken[0] = False
    assert await _drain(worker) == 1
    assert worker._startup_failure_streak == 0
    assert await _drain(worker) == 1


@pytest.mark.parametrize("recipe_id", [None, "recipe-a"], ids=["document", "recipe"])
@pytest.mark.parametrize("attempts", [0, 2], ids=["retry", "terminal"])
async def test_startup_failure_sql_is_conditional_and_atomic(
    monkeypatch: pytest.MonkeyPatch,
    recipe_id: str | None,
    attempts: int,
) -> None:
    """親行 lock と条件付き UPDATE の後だけ対象へ失敗を書き、active 成果物は保持する。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    job = _job("a", attempts=attempts, recipe_id=recipe_id)
    pool = FakeOraclePool(execute_results=[[{"document_id": job.document_id}]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    assert await client.record_ingestion_startup_failure(job, expected_attempt_count=attempts)
    calls = pool.connection.calls
    assert "FOR UPDATE" in calls[0].statement
    update = calls[1]
    assert "status = 'QUEUED' AND lease_owner IS NULL" in update.statement
    assert "attempt_count = :expected_attempt_count" in update.statement
    assert "active_job.status = 'RUNNING'" in update.statement
    assert update.parameters["attempt_count"] == attempts + 1
    assert update.parameters["status"] == ("FAILED" if attempts == 2 else "QUEUED")
    assert len(calls) == (3 if attempts == 2 else 2)
    if attempts == 2:
        assert ("UPDATE rag_document_recipes" if recipe_id else "UPDATE rag_documents") in calls[
            2
        ].statement
        assert "active_chunk_set" not in calls[2].statement
    assert pool.connection.commits == 1


async def test_startup_failure_does_not_update_target_after_losing_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消・完了・別 worker の claim・試行回数変更で UPDATE が不成立なら対象を触らない。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 0, raising=False)
    job = _job("a", attempts=2)
    pool = FakeOraclePool(execute_results=[[{"document_id": job.document_id}]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    assert not await client.record_ingestion_startup_failure(job, expected_attempt_count=2)
    assert len(pool.connection.calls) == 2


async def test_startup_failure_transaction_rolls_back_on_target_update_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """対象の失敗記録が壊れたら job の終端遷移も rollback する。"""
    from app.clients import oracle as oracle_module

    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    job = _job("a", attempts=2)
    pool = FakeOraclePool(execute_results=[[{"document_id": job.document_id}]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    original = oracle_module._execute

    def fail_target(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic target update failure")

    monkeypatch.setattr(oracle_module, "_execute", fail_target)
    with pytest.raises(RuntimeError, match="synthetic target update failure"):
        await client.record_ingestion_startup_failure(job, expected_attempt_count=2)
    assert pool.connection.rollbacks == 1
    assert pool.connection.commits == 0
    monkeypatch.setattr(oracle_module, "_execute", original)


async def test_restart_preserves_failure_limit_and_run_loop_progresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker の再生成後も残る試行回数で終端にし、実際の loop で後続へ進む。"""
    from unittest.mock import AsyncMock

    queue = _Queue([_job("bad", attempts=2), _job("good")])
    monkeypatch.setattr(iw, "OracleClient", lambda: queue)
    stop = asyncio.Event()
    calls: list[str] = []

    async def runner(job_id: str) -> None:
        calls.append(job_id)
        if job_id == "bad":
            raise iw.IngestionJobSubprocessError("synthetic preclaim failure")
        queue.jobs[job_id] = queue.jobs[job_id].model_copy(
            update={"status": IngestionJobStatus.SUCCEEDED}
        )
        stop.set()

    worker = iw.IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        concurrency=1,
        schema_ready=AsyncMock(return_value=True),
        recover_stale=AsyncMock(return_value=[]),
        sweep_document_cleanups=AsyncMock(return_value=0),
        poll_interval_seconds=60,
    )
    await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=2)
    assert calls == ["bad", "good"]
    assert queue.jobs["bad"].status == IngestionJobStatus.FAILED
    assert queue.jobs["bad"].attempt_count == 3


async def test_missing_parent_is_not_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    """文書の削除・アクセス範囲の変更後は job の失敗も書き込まない。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    pool = FakeOraclePool(execute_results=[[]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    assert not await client.record_ingestion_startup_failure(_job("a"), expected_attempt_count=0)
    assert len(pool.connection.calls) == 1
