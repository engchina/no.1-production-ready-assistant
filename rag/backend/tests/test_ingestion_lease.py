"""取込 job の lease / heartbeat と、worker の停止時の後始末のテスト(#357 / #359)。

`rag_ingestion_jobs` の lease の意味(claim・heartbeat・stale の回復・停止時の戻し・
dispatch の除外・lease の条件付きの完了/失敗)を Oracle の SQL と同じ条件で持つ in-memory の
store と、進め方を制御できる時計で、worker の挙動を決定論的に再現する。heartbeat と lease の TTL は
Oracle と同じく store の時計(DB の時計)で判定する。SQL そのものは tests/test_oracle_adapter.py で
検査する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from app.api.routes import documents
from app.config import BACKEND_ROOT, Settings, get_settings
from app.rag import ingestion_job_runner, ingestion_worker
from app.rag.ingestion import IngestionCancelledError
from app.rag.ingestion_worker import IngestionQueueWorker
from app.rag.request_context import (
    NO_ACCESS_AUDIT_REQUEST_CONTEXT,
    AuditRequestContext,
    current_audit_request_context,
)
from app.schemas.document import (
    DocumentDetail,
    FileStatus,
    IngestionJob,
    IngestionJobLease,
    IngestionJobPhase,
    IngestionJobStatus,
)
from tests.language_release_support import install_language_release_stub

_ACTIVE_DOCUMENT_STATUSES = {
    FileStatus.PREPROCESSING,
    FileStatus.INGESTING,
    FileStatus.CHUNKING,
    FileStatus.INDEXING,
}


class _Clock:
    """テストが進める時計(DB の時計と、worker の時計の両方に使う)。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class _Row:
    job: IngestionJob
    lease_owner: str | None = None
    heartbeat_at: datetime | None = None


@dataclass
class _LeaseQueueStore:
    """`rag_ingestion_jobs` の lease の意味を Oracle の SQL と同じ条件で持つ in-memory の store。

    ``clock`` は DB の時計(``SYSTIMESTAMP``)。heartbeat の時刻と lease の TTL の判定に使う(#359)。
    """

    clock: _Clock = field(default_factory=_Clock)
    rows: dict[str, _Row] = field(default_factory=dict)
    documents: dict[str, FileStatus] = field(default_factory=dict)
    claims: list[tuple[str, str | None]] = field(default_factory=list)

    def add(
        self,
        job_id: str,
        *,
        document_id: str,
        queued_at: datetime,
        status: IngestionJobStatus = IngestionJobStatus.QUEUED,
        lease_owner: str | None = None,
        heartbeat_at: datetime | None = None,
        started_at: datetime | None = None,
        attempt_count: int = 0,
    ) -> None:
        self.rows[job_id] = _Row(
            job=IngestionJob(
                id=job_id,
                document_id=document_id,
                status=status,
                parser_profile="local_text_structure",
                attempt_count=attempt_count,
                queued_at=queued_at,
                started_at=started_at,
            ),
            lease_owner=lease_owner,
            heartbeat_at=heartbeat_at,
        )
        self.documents.setdefault(document_id, FileStatus.UPLOADED)

    async def pin_ingestion_job_language_pack(self, job_id: str, release_id: str) -> str:
        row = self.rows[job_id]
        identity = row.job.language_pack_release_id or release_id
        row.job = row.job.model_copy(update={"language_pack_release_id": identity})
        return identity

    def job(self, job_id: str) -> IngestionJob:
        return self.rows[job_id].job

    def _running_documents(self) -> set[str]:
        return {
            row.job.document_id
            for row in self.rows.values()
            if row.job.status == IngestionJobStatus.RUNNING
        }

    def _requeue(self, row: _Row, *, attempt_count: int) -> None:
        row.job = row.job.model_copy(
            update={
                "status": IngestionJobStatus.QUEUED,
                "attempt_count": attempt_count,
                "error_message": None,
                "started_at": None,
                "finished_at": None,
            }
        )
        row.lease_owner = None
        row.heartbeat_at = None
        # 文書単位の PREPROCESS job は、工程の前の状態(UPLOADED)へ戻す。
        if self.documents.get(row.job.document_id) in _ACTIVE_DOCUMENT_STATUSES:
            self.documents[row.job.document_id] = FileStatus.UPLOADED

    # --- OracleClient と同じ名前の操作 ---

    async def list_dispatchable_ingestion_jobs(
        self,
        *,
        limit: int,
        exclude_job_ids: Sequence[str] = (),
        exclude_phases: Collection[IngestionJobPhase] = (),
    ) -> list[IngestionJob]:
        running_documents = self._running_documents()
        candidates = sorted(
            (
                row.job
                for row in self.rows.values()
                if row.job.status == IngestionJobStatus.QUEUED
                and row.job.document_id not in running_documents
                and row.job.id not in exclude_job_ids
                and row.job.phase not in exclude_phases
            ),
            key=lambda job: (job.queued_at, job.id),
        )
        return candidates[:limit]

    async def list_ingestion_jobs(
        self,
        *,
        status: IngestionJobStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> list[IngestionJob]:
        # 修正前の worker の dispatch(QUEUED を古い順に取るだけ)。
        jobs = sorted(
            (row.job for row in self.rows.values() if status is None or row.job.status == status),
            key=lambda job: (job.queued_at, job.id),
            reverse=not oldest_first,
        )
        return jobs[offset : None if limit is None else offset + limit]

    async def claim_ingestion_job(
        self, job_id: str, *, started_at: datetime, lease_owner: str | None = None
    ) -> IngestionJob | None:
        row = self.rows.get(job_id)
        if row is None or row.job.status != IngestionJobStatus.QUEUED:
            return None
        if row.job.document_id in self._running_documents():
            return None
        row.job = row.job.model_copy(
            update={
                "status": IngestionJobStatus.RUNNING,
                "attempt_count": row.job.attempt_count + 1,
                "started_at": started_at,
                "error_message": None,
                "finished_at": None,
            }
        )
        row.lease_owner = lease_owner
        row.heartbeat_at = self.clock() if lease_owner is not None else None
        self.claims.append((job_id, lease_owner))
        # pipeline が文書の status を工程中にする。
        self.documents[row.job.document_id] = FileStatus.INGESTING
        return row.job

    async def heartbeat_ingestion_jobs(self, *, lease_owner: str) -> int:
        count = 0
        for row in self.rows.values():
            if row.lease_owner == lease_owner and row.job.status == IngestionJobStatus.RUNNING:
                row.heartbeat_at = self.clock()
                count += 1
        return count

    async def recover_stale_ingestion_jobs(
        self,
        *,
        stale_before: datetime,
        limit: int,
        lease_ttl_seconds: float | None = None,
        exclude_lease_owner: str | None = None,
    ) -> list[IngestionJob]:
        heartbeat_cutoff = self.clock() - timedelta(seconds=lease_ttl_seconds or 90.0)
        recovered: list[IngestionJob] = []
        for row in list(self.rows.values()):
            job = row.job
            if job.status != IngestionJobStatus.RUNNING:
                continue
            if row.heartbeat_at is None:
                stale = (job.started_at or job.queued_at) < stale_before
            else:
                stale = row.heartbeat_at < heartbeat_cutoff
            if not stale:
                continue
            if exclude_lease_owner is not None and row.lease_owner == exclude_lease_owner:
                continue
            recovered.append(job)
            if job.attempt_count >= job.max_attempts:
                row.job = job.model_copy(update={"status": IngestionJobStatus.FAILED})
            else:
                self._requeue(row, attempt_count=job.attempt_count)
            if len(recovered) >= limit:
                break
        return recovered

    async def requeue_leased_ingestion_jobs(self, *, lease_owner: str) -> list[IngestionJob]:
        requeued: list[IngestionJob] = []
        for row in self.rows.values():
            if row.lease_owner == lease_owner and row.job.status == IngestionJobStatus.RUNNING:
                self._requeue(row, attempt_count=max(row.job.attempt_count - 1, 0))
                requeued.append(row.job)
        return requeued

    async def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        row = self.rows.get(job_id)
        return None if row is None else row.job

    async def get_ingestion_job_lease(self, job_id: str) -> IngestionJobLease | None:
        row = self.rows.get(job_id)
        if row is None:
            return None
        return IngestionJobLease(status=row.job.status, lease_owner=row.lease_owner)

    async def list_document_ingestion_jobs(
        self, document_id: str, *, status: IngestionJobStatus | None = None
    ) -> list[IngestionJob]:
        return [
            row.job
            for row in self.rows.values()
            if row.job.document_id == document_id and (status is None or row.job.status == status)
        ]

    async def transition_ingestion_job(
        self,
        job_id: str,
        *,
        from_statuses: Collection[IngestionJobStatus],
        to_status: IngestionJobStatus,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        lease_owner: str | None = None,
    ) -> IngestionJob | None:
        row = self.rows.get(job_id)
        if row is None or row.job.status not in from_statuses:
            return None
        # lease を持つ実行は、自分の lease の行だけを遷移する(#359)。
        if lease_owner is not None and row.lease_owner != lease_owner:
            return None
        row.job = row.job.model_copy(
            update={
                "status": to_status,
                "error_message": error_message,
                "finished_at": finished_at,
            }
        )
        return row.job

    async def update_document_status(
        self, document_id: str, status: FileStatus, error_message: str | None = None
    ) -> None:
        _ = error_message
        self.documents[document_id] = status


class _JobRunner:
    """子プロセスの代わり: claim して、テストが release するまで実行中のままにする。"""

    def __init__(self, store: _LeaseQueueStore, clock: _Clock) -> None:
        self.store = store
        self.clock = clock
        self.lease_owner: str | None = None
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.cancelled: list[str] = []

    def started_event(self, job_id: str) -> asyncio.Event:
        return self.started.setdefault(job_id, asyncio.Event())

    def release_event(self, job_id: str) -> asyncio.Event:
        return self.release.setdefault(job_id, asyncio.Event())

    async def __call__(self, job_id: str) -> None:
        claimed = await self.store.claim_ingestion_job(
            job_id, started_at=self.clock(), lease_owner=self.lease_owner
        )
        if claimed is None:
            return
        self.started_event(job_id).set()
        try:
            await self.release_event(job_id).wait()
        except asyncio.CancelledError:
            # 子プロセスは SIGTERM で止まり、job の状態は書かない。
            self.cancelled.append(job_id)
            raise
        await self.store.transition_ingestion_job(
            job_id,
            from_statuses=(IngestionJobStatus.RUNNING,),
            to_status=IngestionJobStatus.SUCCEEDED,
            finished_at=self.clock(),
        )


async def _schema_ready() -> bool:
    return True


def _settings() -> Settings:
    return get_settings().model_copy(
        update={
            "ingestion_queue_stale_running_seconds": 300.0,
            "ingestion_queue_heartbeat_interval_seconds": 15.0,
            "ingestion_queue_lease_ttl_seconds": 90.0,
            "ingestion_queue_startup_drain_limit": 50,
        }
    )


def _worker(
    worker_id: str,
    store: _LeaseQueueStore,
    clock: _Clock,
    *,
    concurrency: int = 1,
    shutdown_grace_seconds: float = 0.0,
    heartbeat_interval_seconds: float | None = None,
) -> tuple[IngestionQueueWorker, _JobRunner]:
    runner = _JobRunner(store, clock)
    worker = IngestionQueueWorker(
        settings=_settings(),
        job_runner=runner,
        schema_ready=_schema_ready,
        concurrency=concurrency,
        heavy_phase_concurrency=concurrency,
        poll_interval_seconds=0.01,
        worker_id=worker_id,
        shutdown_grace_seconds=shutdown_grace_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        clock=clock,
    )
    runner.lease_owner = worker.worker_id
    return worker, runner


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> Iterator[_LeaseQueueStore]:
    store = _LeaseQueueStore(clock=clock)
    monkeypatch.setattr(ingestion_worker, "OracleClient", lambda: store)
    ingestion_worker._WAKEUP.clear()
    yield store
    ingestion_worker._WAKEUP.clear()


async def _finish(worker: IngestionQueueWorker, runner: _JobRunner) -> None:
    for event in runner.release.values():
        event.set()
    for task in list(worker._tasks):
        await asyncio.wait_for(task, timeout=5)


async def test_long_running_job_is_not_recovered_while_heartbeating(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """heartbeat が続く限り、開始から stale の閾値(300 秒)を超えた job も回復しない。

    修正前は開始からの経過時間だけで判定し、正常に実行中の長い job を QUEUED に戻していた。
    """
    store.add("job-long", document_id="doc-long", queued_at=clock())
    worker_a, runner_a = _worker("worker-a", store, clock)
    worker_b, _ = _worker("worker-b", store, clock)

    assert await worker_a._dispatch_available() == 1
    await asyncio.wait_for(runner_a.started_event("job-long").wait(), timeout=5)

    # 20 分(subprocess の timeout と同じ長さ)、15 秒ごとに heartbeat を打ちながら、
    # 自分と別の worker が回復を試みる。
    for _ in range(80):
        clock.advance(15)
        await worker_a._heartbeat_safely()
        await worker_a._recover_stale_safely()
        await worker_b._recover_stale_safely()

    job = store.job("job-long")
    assert job.status == IngestionJobStatus.RUNNING
    assert job.attempt_count == 1
    assert store.rows["job-long"].lease_owner == "worker-a"
    assert store.claims == [("job-long", "worker-a")]
    await _finish(worker_a, runner_a)
    assert store.job("job-long").status == IngestionJobStatus.SUCCEEDED


async def test_own_leased_job_is_not_recovered_even_if_heartbeat_lags(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """heartbeat が遅れても、自分の lease の実行中 job は自分の回復では戻さない。"""
    store.add("job-own", document_id="doc-own", queued_at=clock())
    worker, runner = _worker("worker-a", store, clock)
    await worker._dispatch_available()
    await asyncio.wait_for(runner.started_event("job-own").wait(), timeout=5)

    clock.advance(600)
    await worker._recover_stale_safely()

    assert store.job("job-own").status == IngestionJobStatus.RUNNING
    await _finish(worker, runner)


async def test_job_with_stopped_heartbeat_is_recovered_after_ttl(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """worker が止まって heartbeat が途絶えた job は、TTL を過ぎてから別の worker が回復する。"""
    store.add("job-orphan", document_id="doc-orphan", queued_at=clock())
    worker_a, runner_a = _worker("worker-a", store, clock)
    worker_b, runner_b = _worker("worker-b", store, clock)
    await worker_a._dispatch_available()
    await asyncio.wait_for(runner_a.started_event("job-orphan").wait(), timeout=5)
    # worker-a はここで止まった(heartbeat を打たない)。

    clock.advance(89)
    await worker_b._recover_stale_safely()
    assert store.job("job-orphan").status == IngestionJobStatus.RUNNING

    clock.advance(2)
    await worker_b._recover_stale_safely()
    recovered = store.job("job-orphan")
    assert recovered.status == IngestionJobStatus.QUEUED
    assert store.rows["job-orphan"].lease_owner is None
    assert store.documents["doc-orphan"] == FileStatus.UPLOADED

    # 回復した job は別の worker が claim し直す(試行回数は 2 回目)。
    assert await worker_b._dispatch_available() == 1
    await asyncio.wait_for(runner_b.started_event("job-orphan").wait(), timeout=5)
    assert store.job("job-orphan").attempt_count == 2
    assert store.rows["job-orphan"].lease_owner == "worker-b"

    for task in list(worker_a._tasks):
        task.cancel()
    await asyncio.gather(*worker_a._tasks, return_exceptions=True)
    await _finish(worker_b, runner_b)


async def test_worker_clock_skew_does_not_recover_heartbeating_job(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """heartbeat と lease の TTL は DB の時計で判定し、worker の時計のずれに左右されない(#359)。

    修正前は heartbeat の時刻と TTL の基準を worker の時計で渡していたため、時計が進んだ別の host の
    worker が、heartbeat の続く job を stale として回復していた。
    """
    store.add("job-skew", document_id="doc-skew", queued_at=clock())
    worker_a, runner_a = _worker("worker-a", store, clock)
    skewed = _Clock()
    skewed.now = clock.now + timedelta(minutes=10)
    worker_b, _ = _worker("worker-b", store, skewed)

    await worker_a._dispatch_available()
    await asyncio.wait_for(runner_a.started_event("job-skew").wait(), timeout=5)
    clock.advance(15)
    skewed.advance(15)
    await worker_a._heartbeat_safely()
    await worker_b._recover_stale_safely()

    assert store.job("job-skew").status == IngestionJobStatus.RUNNING
    assert store.rows["job-skew"].lease_owner == "worker-a"
    await _finish(worker_a, runner_a)


async def test_legacy_running_row_without_heartbeat_uses_elapsed_time(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """lease 導入前の RUNNING 行(heartbeat なし)は、従来どおり開始からの経過時間で回復する。"""
    store.add(
        "job-legacy",
        document_id="doc-legacy",
        queued_at=clock(),
        status=IngestionJobStatus.RUNNING,
        started_at=clock(),
        attempt_count=1,
    )
    store.documents["doc-legacy"] = FileStatus.INGESTING
    worker, _ = _worker("worker-a", store, clock)

    clock.advance(299)
    await worker._recover_stale_safely()
    assert store.job("job-legacy").status == IngestionJobStatus.RUNNING

    clock.advance(2)
    await worker._recover_stale_safely()
    assert store.job("job-legacy").status == IngestionJobStatus.QUEUED


async def test_dispatch_is_not_blocked_by_job_of_running_document(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """先頭の QUEUED job が claim できなくても(同じ文書の job が実行中)、後ろの job を流す。

    修正前は ``_fetch_queued(free)`` が毎回先頭の claim できない job だけを取り、free の枠を埋めて
    いたため、後ろの QUEUED job が流れなかった。
    """
    store.add(
        "job-running",
        document_id="doc-busy",
        queued_at=clock(),
        status=IngestionJobStatus.RUNNING,
        started_at=clock(),
        lease_owner="worker-other",
        heartbeat_at=clock(),
        attempt_count=1,
    )
    clock.advance(1)
    store.add("job-blocked", document_id="doc-busy", queued_at=clock())
    clock.advance(1)
    store.add("job-next", document_id="doc-free", queued_at=clock())
    worker, runner = _worker("worker-a", store, clock)

    assert await worker._dispatch_available() == 1
    await asyncio.wait_for(runner.started_event("job-next").wait(), timeout=5)

    assert store.job("job-next").status == IngestionJobStatus.RUNNING
    assert store.job("job-blocked").status == IngestionJobStatus.QUEUED
    assert store.claims == [("job-next", "worker-a")]
    await _finish(worker, runner)


async def test_dispatch_skips_own_inflight_queued_job(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """自分が dispatch 済みでまだ QUEUED に見える job は取り直さず、次の job を取る。"""
    store.add("job-first", document_id="doc-1", queued_at=clock())
    clock.advance(1)
    store.add("job-second", document_id="doc-2", queued_at=clock())
    worker, runner = _worker("worker-a", store, clock, concurrency=2)
    # job-first は dispatch 済みで、子がまだ claim していない(QUEUED のまま)。
    worker._inflight.add("job-first")

    assert await worker._dispatch_available() == 1
    await asyncio.wait_for(runner.started_event("job-second").wait(), timeout=5)
    assert store.claims == [("job-second", "worker-a")]
    worker._inflight.discard("job-first")
    await _finish(worker, runner)


async def test_two_workers_do_not_double_run_a_long_job(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """worker が 2 つでも、実行中の長い job を別の worker が回復・claim しない。

    修正前は 300 秒で stale として QUEUED に戻り、別の worker が claim して二重実行になり、
    claim のたびに attempt が増えて規定回数を超えて停止になりえた。
    """
    store.add("job-long", document_id="doc-long", queued_at=clock())
    worker_a, runner_a = _worker("worker-a", store, clock)
    worker_b, runner_b = _worker("worker-b", store, clock)

    await asyncio.gather(worker_a._dispatch_available(), worker_b._dispatch_available())
    await asyncio.sleep(0)
    for _ in range(80):
        clock.advance(15)
        await worker_a._heartbeat_safely()
        await worker_b._recover_stale_safely()
        await worker_b._dispatch_available()
        await asyncio.sleep(0)

    assert len(store.claims) == 1
    assert store.job("job-long").attempt_count == 1
    assert store.job("job-long").status == IngestionJobStatus.RUNNING
    await _finish(worker_a, runner_a)
    await _finish(worker_b, runner_b)
    assert store.job("job-long").status == IngestionJobStatus.SUCCEEDED


async def test_shutdown_requeues_own_leased_job_without_consuming_attempt(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """停止で grace を過ぎた job は子を止め、自分の lease の job を QUEUED に戻す(attempt は戻す)。

    修正前は子を止めるだけで job を RUNNING のまま残し、stale の閾値まで同じ文書の job も
    claim されなかった。
    """
    store.add("job-slow", document_id="doc-slow", queued_at=clock())
    store.add(
        "job-other-worker",
        document_id="doc-other",
        queued_at=clock(),
        status=IngestionJobStatus.RUNNING,
        started_at=clock(),
        lease_owner="worker-other",
        heartbeat_at=clock(),
        attempt_count=1,
    )
    store.documents["doc-other"] = FileStatus.INGESTING
    worker, runner = _worker("worker-a", store, clock, shutdown_grace_seconds=0.05)
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    await asyncio.wait_for(runner.started_event("job-slow").wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert runner.cancelled == ["job-slow"]
    requeued = store.job("job-slow")
    assert requeued.status == IngestionJobStatus.QUEUED
    assert requeued.attempt_count == 0
    assert requeued.started_at is None
    assert store.rows["job-slow"].lease_owner is None
    assert store.documents["doc-slow"] == FileStatus.UPLOADED
    # 他の worker の lease の job には触れない。
    assert store.job("job-other-worker").status == IngestionJobStatus.RUNNING
    assert store.documents["doc-other"] == FileStatus.INGESTING


async def test_worker_and_job_runner_run_with_system_access(
    store: _LeaseQueueStore, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取込は要求の外の信頼した処理なので、範囲の制限の無い context で動く（#3250）。

    文脈の無い処理は何も読めない（`NO_ACCESS_AUDIT_REQUEST_CONTEXT`）ので、worker の loop と
    job を 1 件だけ実行する子（`ingestion_job_runner._run`）が明示して入る。
    """
    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT
    store.add("job-scope", document_id="doc-scope", queued_at=clock())
    worker, runner = _worker("worker-a", store, clock)
    seen: list[AuditRequestContext] = []
    run_job = runner.__call__

    async def capture(job_id: str) -> None:
        seen.append(current_audit_request_context())
        runner.release_event(job_id).set()
        await run_job(job_id)

    worker._job_runner = capture
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    await asyncio.wait_for(runner.started_event("job-scope").wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert seen == [AuditRequestContext()]

    child: list[AuditRequestContext] = []

    async def fake_run_ingestion_job(job_id: str, *, lease_owner: str | None = None) -> None:
        child.append(current_audit_request_context())

    monkeypatch.setattr(documents, "_run_ingestion_job", fake_run_ingestion_job)
    await ingestion_job_runner._run("job-child")
    assert child == [AuditRequestContext()]
    assert current_audit_request_context() is NO_ACCESS_AUDIT_REQUEST_CONTEXT


async def test_shutdown_waits_for_job_within_grace(store: _LeaseQueueStore, clock: _Clock) -> None:
    """grace のうちに終わる job は止めずに完了させ、QUEUED に戻さない。"""
    store.add("job-quick", document_id="doc-quick", queued_at=clock())
    worker, runner = _worker("worker-a", store, clock, shutdown_grace_seconds=5.0)
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    await asyncio.wait_for(runner.started_event("job-quick").wait(), timeout=5)
    stop.set()
    await asyncio.sleep(0.05)
    assert not task.done()  # grace の間は job の完了を待つ。
    runner.release_event("job-quick").set()
    await asyncio.wait_for(task, timeout=5)

    assert runner.cancelled == []
    assert store.job("job-quick").status == IngestionJobStatus.SUCCEEDED


async def test_shutdown_does_not_fail_job_interrupted_by_signal(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """停止中に子が signal で止まって異常終了しても FAILED にせず、QUEUED に戻す。"""
    store.add("job-interrupted", document_id="doc-int", queued_at=clock())
    started = asyncio.Event()
    stop = asyncio.Event()

    async def runner(job_id: str) -> None:
        await store.claim_ingestion_job(job_id, started_at=clock(), lease_owner="worker-a")
        started.set()
        await stop.wait()
        # 端末の Ctrl+C などで子にも signal が届き、終了コード 143 で終わった。
        raise ingestion_worker.IngestionJobSubprocessError(
            "ingestion job subprocess exited with code 143"
        )

    worker = IngestionQueueWorker(
        settings=_settings(),
        job_runner=runner,
        schema_ready=_schema_ready,
        concurrency=1,
        poll_interval_seconds=0.01,
        worker_id="worker-a",
        shutdown_grace_seconds=5.0,
        clock=clock,
    )
    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    await asyncio.wait_for(started.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    job = store.job("job-interrupted")
    assert job.status == IngestionJobStatus.QUEUED
    assert job.attempt_count == 0


async def test_heartbeat_loop_extends_lease_only_while_jobs_run(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """heartbeat は実行中の job がある間だけ、heartbeat_interval ごとに打つ。"""
    beats: list[datetime] = []
    enough = asyncio.Event()

    async def heartbeat() -> int:
        beats.append(clock())
        if len(beats) >= 3:
            enough.set()
        return 1

    store.add("job-hb", document_id="doc-hb", queued_at=clock())
    runner = _JobRunner(store, clock)
    runner.lease_owner = "worker-a"
    worker = IngestionQueueWorker(
        settings=_settings(),
        job_runner=runner,
        schema_ready=_schema_ready,
        concurrency=1,
        poll_interval_seconds=0.01,
        worker_id="worker-a",
        heartbeat=heartbeat,
        heartbeat_interval_seconds=0.01,
        shutdown_grace_seconds=5.0,
        clock=clock,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    await asyncio.wait_for(enough.wait(), timeout=5)
    runner.release_event("job-hb").set()
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    count_after_finish = len(beats)

    # 実行中の job が無いアイドルの worker は heartbeat を打たない。
    idle = IngestionQueueWorker(
        settings=_settings(),
        job_runner=runner,
        fetch_queued=lambda _limit, _exclude_phases: _no_jobs(),
        recover_stale=_no_jobs,
        schema_ready=_schema_ready,
        poll_interval_seconds=0.01,
        heartbeat=heartbeat,
        heartbeat_interval_seconds=0.01,
        clock=clock,
    )
    idle_stop = asyncio.Event()
    idle_task = asyncio.create_task(idle.run_forever(stop_event=idle_stop))
    await asyncio.sleep(0.1)
    idle_stop.set()
    await asyncio.wait_for(idle_task, timeout=5)

    assert count_after_finish >= 3
    assert len(beats) == count_after_finish
    assert store.job("job-hb").status == IngestionJobStatus.SUCCEEDED


async def _no_jobs() -> Sequence[IngestionJob]:
    return []


def test_worker_ids_are_unique_and_fit_lease_owner_column() -> None:
    """lease_owner(VARCHAR2(128))に収まり、同じプロセスの worker でも重ならない。"""
    first = ingestion_worker.new_worker_id()
    second = ingestion_worker.new_worker_id()

    assert first != second
    assert len(first) <= 128
    assert f":{os.getpid()}:" in first


def test_worker_unit_stop_timeout_covers_shutdown_grace() -> None:
    """systemd の worker unit は、grace と子の停止待ちの後に job を QUEUED に戻す時間を残す。"""
    init_script = (BACKEND_ROOT.parent / "init_script.sh").read_text(encoding="utf-8")
    grace_match = re.search(r"^WORKER_SHUTDOWN_GRACE_SECONDS=(\d+)$", init_script, re.M)
    stop_timeout_match = re.search(r"^WORKER_TIMEOUT_STOP_SEC=(\d+)$", init_script, re.M)
    assert grace_match is not None
    assert stop_timeout_match is not None
    grace = int(grace_match[1])
    stop_timeout = int(stop_timeout_match[1])

    assert grace == Settings.model_fields["ingestion_queue_shutdown_grace_seconds"].default
    grace_env = "Environment=RAG_INGESTION_QUEUE_SHUTDOWN_GRACE_SECONDS="
    assert f"{grace_env}${{WORKER_SHUTDOWN_GRACE_SECONDS}}" in init_script
    assert "TimeoutStopSec=${WORKER_TIMEOUT_STOP_SEC}" in init_script
    assert stop_timeout >= grace + ingestion_worker.CHILD_TERMINATE_TIMEOUT_SECONDS + 10


def test_lease_ttl_must_cover_several_heartbeats() -> None:
    """TTL が heartbeat 間隔の 3 倍未満の設定は起動時に拒否する。"""
    with pytest.raises(ValueError, match="3 倍以上"):
        Settings(
            ingestion_queue_heartbeat_interval_seconds=30.0,
            ingestion_queue_lease_ttl_seconds=60.0,
        )


async def test_subprocess_runner_passes_lease_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """process isolation の子は、親の worker の lease で claim する。"""
    captured: list[tuple[str, ...]] = []

    class _Process:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_create_subprocess_exec(*cmd: str) -> _Process:
        captured.append(cmd)
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    settings = get_settings().model_copy(update={"ingestion_queue_process_isolation_enabled": True})

    runner = ingestion_worker._job_runner_for_settings(settings, lease_owner="host:1:abc")
    await runner("job-leased")

    assert captured == [
        (
            sys.executable,
            "-m",
            "app.rag.ingestion_job_runner",
            "job-leased",
            "--lease-owner",
            "host:1:abc",
        )
    ]


async def test_inline_runner_passes_lease_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """process isolation を使わない worker も、自分の lease で claim する。"""
    from app.api.routes import documents

    captured: dict[str, object] = {}

    async def fake_run(job_id: str, *, lease_owner: str | None = None) -> None:
        captured.update(job_id=job_id, lease_owner=lease_owner)

    monkeypatch.setattr(documents, "_run_ingestion_job", fake_run)
    settings = get_settings().model_copy(
        update={"ingestion_queue_process_isolation_enabled": False}
    )

    await ingestion_worker._job_runner_for_settings(settings, lease_owner="w-1")("job-inline")

    assert captured == {"job_id": "job-inline", "lease_owner": "w-1"}


def test_job_runner_stops_on_sigterm_without_writing_job_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子の runner は SIGTERM で job の実行を取り消し、後始末をして 143 で終わる。"""
    from app.api.routes import documents

    captured: dict[str, object] = {}
    closed: list[bool] = []

    async def fake_run(job_id: str, *, lease_owner: str | None = None) -> None:
        captured.update(job_id=job_id, lease_owner=lease_owner)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(30)

    monkeypatch.setattr(documents, "_run_ingestion_job", fake_run)
    monkeypatch.setattr(ingestion_job_runner, "close_oracle_pool", lambda: closed.append(True))

    exit_code = ingestion_job_runner.main(["job-term", "--lease-owner", "w-2"])

    assert exit_code == 128 + signal.SIGTERM
    assert captured == {"job_id": "job-term", "lease_owner": "w-2"}
    assert closed == [True]


# --- 完了・失敗の遷移の lease の条件(#359) ---


class _Pipeline:
    """``_ingest_existing_document`` の代わり: release まで待ち、成功するか ``error`` を投げる。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.error: Exception | None = None
        self.check_cancel = False
        self.cancel_results: list[bool] = []
        self.auto_advanced: list[str] = []
        self.dispatched: list[str] = []

    async def __call__(
        self,
        document_id: str,
        *,
        force: bool,
        use_prepared_artifact: bool,
        cancel_checker: object = None,
    ) -> DocumentDetail:
        _ = document_id, force, use_prepared_artifact
        self.started.set()
        await self.release.wait()
        if self.check_cancel and callable(cancel_checker):
            cancelled = bool(await cancel_checker())
            self.cancel_results.append(cancelled)
            if cancelled:
                raise IngestionCancelledError("取り消されました。")
        if self.error is not None:
            raise self.error
        # 自動進行(次工程の投入)は差し替えるため、中身は使わない。
        return cast(DocumentDetail, object())

    async def auto_advance(self, job: IngestionJob, detail: DocumentDetail) -> None:
        _ = detail
        self.auto_advanced.append(job.id)

    def dispatch(self, job_id: str, *, force: bool = False) -> None:
        _ = force
        self.dispatched.append(job_id)


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch, store: _LeaseQueueStore) -> _Pipeline:
    install_language_release_stub(monkeypatch)
    pipeline = _Pipeline()

    async def reset_outputs(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(documents, "OracleClient", lambda: store)
    monkeypatch.setattr(documents, "_ingest_existing_document", pipeline)
    monkeypatch.setattr(documents, "_reset_document_outputs_for_extract", reset_outputs)
    monkeypatch.setattr(documents, "_enqueue_auto_advance_job", pipeline.auto_advance)
    monkeypatch.setattr(documents, "_dispatch_ingestion_job", pipeline.dispatch)
    return pipeline


async def _expire_lease_and_reclaim(
    store: _LeaseQueueStore, clock: _Clock, job_id: str, *, new_owner: str = "worker-b"
) -> None:
    """worker-a の heartbeat が TTL を超えて途絶え、別の worker が回復して claim し直す。"""
    other, _ = _worker(new_owner, store, clock)
    clock.advance(91)
    await other._recover_stale_safely()
    assert store.job(job_id).status == IngestionJobStatus.QUEUED
    reclaimed = await store.claim_ingestion_job(job_id, started_at=clock(), lease_owner=new_owner)
    assert reclaimed is not None


def _stale_result_discarded(
    caplog: pytest.LogCaptureFixture, *, job_id: str, lease_owner: str
) -> bool:
    return any(
        record.getMessage() == "ingestion_job_stale_result_discarded"
        and getattr(record, "job_id", None) == job_id
        and getattr(record, "lease_owner", None) == lease_owner
        for record in caplog.records
    )


def _assert_reclaimed_job_untouched(store: _LeaseQueueStore, job_id: str, document_id: str) -> None:
    job = store.job(job_id)
    assert job.status == IngestionJobStatus.RUNNING
    assert job.attempt_count == 2
    assert job.finished_at is None
    assert store.rows[job_id].lease_owner == "worker-b"
    # 新しい実行が工程中にした文書の status も変えない。
    assert store.documents[document_id] == FileStatus.INGESTING


@pytest.mark.parametrize("outcome", ["success", "failure", "transient"])
async def test_stale_run_result_does_not_overwrite_reclaimed_job(
    store: _LeaseQueueStore,
    clock: _Clock,
    pipeline: _Pipeline,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """heartbeat が途絶えて別の worker が再 claim した後の、古い実行の完了・失敗・再キューは捨てる。

    修正前は遷移の条件が ``status = 'RUNNING'`` だけで、古い実行の SUCCEEDED / FAILED / QUEUED が
    新しい実行の RUNNING を上書きし、文書の status も戻していた。
    """
    store.add("job-stale", document_id="doc-stale", queued_at=clock())
    if outcome == "failure":
        pipeline.error = RuntimeError("parser down")
    elif outcome == "transient":
        pipeline.error = RuntimeError("DPY-4011: connection closed")
        monkeypatch.setattr(documents, "is_transient_oracle_error", lambda _exc: True)
    run_a = asyncio.create_task(documents._run_ingestion_job("job-stale", lease_owner="worker-a"))
    await asyncio.wait_for(pipeline.started.wait(), timeout=5)

    await _expire_lease_and_reclaim(store, clock, "job-stale")
    with caplog.at_level(logging.INFO):
        pipeline.release.set()
        await asyncio.wait_for(run_a, timeout=5)

    _assert_reclaimed_job_untouched(store, "job-stale", "doc-stale")
    assert pipeline.auto_advanced == []
    assert pipeline.dispatched == []
    assert _stale_result_discarded(caplog, job_id="job-stale", lease_owner="worker-a")


async def test_stale_run_stops_at_cancel_check_after_lease_is_lost(
    store: _LeaseQueueStore,
    clock: _Clock,
    pipeline: _Pipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """lease を失った実行は、工程の途中の取り消しの確認で止まり、以後の書き込みをしない。"""
    store.add("job-stale", document_id="doc-stale", queued_at=clock())
    pipeline.check_cancel = True
    run_a = asyncio.create_task(documents._run_ingestion_job("job-stale", lease_owner="worker-a"))
    await asyncio.wait_for(pipeline.started.wait(), timeout=5)

    await _expire_lease_and_reclaim(store, clock, "job-stale")
    with caplog.at_level(logging.INFO):
        pipeline.release.set()
        await asyncio.wait_for(run_a, timeout=5)

    assert pipeline.cancel_results == [True]
    _assert_reclaimed_job_untouched(store, "job-stale", "doc-stale")
    assert _stale_result_discarded(caplog, job_id="job-stale", lease_owner="worker-a")


async def test_stale_run_does_not_restore_statuses_when_new_run_is_cancelled(
    store: _LeaseQueueStore,
    clock: _Clock,
    pipeline: _Pipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """再 claim された実行が取り消されても、古い実行は取り消しの後始末(status の戻し)をしない。

    後始末は取り消された実行(lease の持ち主)が行う。
    """
    store.add("job-stale", document_id="doc-stale", queued_at=clock())
    pipeline.check_cancel = True
    run_a = asyncio.create_task(documents._run_ingestion_job("job-stale", lease_owner="worker-a"))
    await asyncio.wait_for(pipeline.started.wait(), timeout=5)
    await _expire_lease_and_reclaim(store, clock, "job-stale")
    await store.transition_ingestion_job(
        "job-stale",
        from_statuses=(IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING),
        to_status=IngestionJobStatus.CANCELLED,
    )

    with caplog.at_level(logging.INFO):
        pipeline.release.set()
        await asyncio.wait_for(run_a, timeout=5)

    assert store.job("job-stale").status == IngestionJobStatus.CANCELLED
    assert store.documents["doc-stale"] == FileStatus.INGESTING
    assert _stale_result_discarded(caplog, job_id="job-stale", lease_owner="worker-a")


async def test_worker_does_not_fail_job_reclaimed_after_child_crash(
    store: _LeaseQueueStore, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """子が異常終了したとき、job が既に別の worker に再 claim されていれば FAILED にしない。"""
    store.add("job-crash", document_id="doc-crash", queued_at=clock())
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(job_id: str) -> None:
        await store.claim_ingestion_job(job_id, started_at=clock(), lease_owner="worker-a")
        started.set()
        await release.wait()
        raise ingestion_worker.IngestionJobSubprocessError("ingestion job subprocess timed out")

    worker = IngestionQueueWorker(
        settings=_settings(),
        job_runner=runner,
        schema_ready=_schema_ready,
        concurrency=1,
        poll_interval_seconds=0.01,
        worker_id="worker-a",
        clock=clock,
    )
    assert await worker._dispatch_available() == 1
    await asyncio.wait_for(started.wait(), timeout=5)
    await _expire_lease_and_reclaim(store, clock, "job-crash")

    with caplog.at_level(logging.INFO):
        release.set()
        for task in list(worker._tasks):
            await asyncio.wait_for(task, timeout=5)

    _assert_reclaimed_job_untouched(store, "job-crash", "doc-crash")
    assert _stale_result_discarded(caplog, job_id="job-crash", lease_owner="worker-a")


async def test_worker_fails_own_leased_job_after_child_crash(
    store: _LeaseQueueStore, clock: _Clock
) -> None:
    """自分の lease の job の子が異常終了したら、従来どおり FAILED にし文書を ERROR にする。"""
    store.add("job-crash", document_id="doc-crash", queued_at=clock())

    async def runner(job_id: str) -> None:
        await store.claim_ingestion_job(job_id, started_at=clock(), lease_owner="worker-a")
        raise ingestion_worker.IngestionJobSubprocessError("child died")

    worker = IngestionQueueWorker(
        settings=_settings(),
        job_runner=runner,
        schema_ready=_schema_ready,
        concurrency=1,
        poll_interval_seconds=0.01,
        worker_id="worker-a",
        clock=clock,
    )
    assert await worker._dispatch_available() == 1
    for task in list(worker._tasks):
        await asyncio.wait_for(task, timeout=5)

    assert store.job("job-crash").status == IngestionJobStatus.FAILED
    assert store.documents["doc-crash"] == FileStatus.ERROR


@pytest.mark.parametrize("lease_owner", [None, "worker-a"])
async def test_run_finishes_job_it_still_holds(
    store: _LeaseQueueStore,
    clock: _Clock,
    pipeline: _Pipeline,
    lease_owner: str | None,
) -> None:
    """lease を持ち続けた実行と、lease を持たない実行(従来の経路)は、従来どおり完了を書く。"""
    store.add("job-ok", document_id="doc-ok", queued_at=clock())
    run = asyncio.create_task(documents._run_ingestion_job("job-ok", lease_owner=lease_owner))
    await asyncio.wait_for(pipeline.started.wait(), timeout=5)
    clock.advance(30)
    pipeline.release.set()
    await asyncio.wait_for(run, timeout=5)

    assert store.job("job-ok").status == IngestionJobStatus.SUCCEEDED
    assert pipeline.auto_advanced == ["job-ok"]


async def test_run_without_lease_fails_job_as_before(
    store: _LeaseQueueStore, clock: _Clock, pipeline: _Pipeline
) -> None:
    """lease を持たない実行(lease 導入前の経路)は、従来どおり lease を条件にせず FAILED を書く。"""
    store.add("job-legacy", document_id="doc-legacy", queued_at=clock())
    pipeline.error = RuntimeError("parser down")
    run = asyncio.create_task(documents._run_ingestion_job("job-legacy"))
    await asyncio.wait_for(pipeline.started.wait(), timeout=5)
    pipeline.release.set()
    await asyncio.wait_for(run, timeout=5)

    job = store.job("job-legacy")
    assert job.status == IngestionJobStatus.FAILED
    assert store.rows["job-legacy"].heartbeat_at is None


async def test_own_run_restores_statuses_when_cancelled(
    store: _LeaseQueueStore,
    clock: _Clock,
    pipeline: _Pipeline,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """自分の lease の実行が取り消されたら、従来どおり文書の status を工程の前へ戻す。"""
    store.add("job-cancel", document_id="doc-cancel", queued_at=clock())
    pipeline.check_cancel = True
    run = asyncio.create_task(documents._run_ingestion_job("job-cancel", lease_owner="worker-a"))
    await asyncio.wait_for(pipeline.started.wait(), timeout=5)
    await store.transition_ingestion_job(
        "job-cancel",
        from_statuses=(IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING),
        to_status=IngestionJobStatus.CANCELLED,
    )

    with caplog.at_level(logging.INFO):
        pipeline.release.set()
        await asyncio.wait_for(run, timeout=5)

    assert store.job("job-cancel").status == IngestionJobStatus.CANCELLED
    assert store.documents["doc-cancel"] == FileStatus.UPLOADED
    assert not _stale_result_discarded(caplog, job_id="job-cancel", lease_owner="worker-a")
