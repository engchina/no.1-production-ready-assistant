"""取込キューワーカーのテスト。"""

import asyncio
import sys
from collections.abc import Collection, Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.rag import ingestion_worker
from app.rag.ingestion_worker import (
    IngestionJobStalledError,
    IngestionJobSubprocessError,
    IngestionJobTimeoutError,
    IngestionQueueWorker,
)
from app.schemas.document import (
    INGESTION_HEAVY_PHASES,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobProgress,
    IngestionJobStatus,
    IngestionParseProgress,
    IngestionParseSegmentProgress,
    IngestionVisionProgress,
)


def _job(job_id: str) -> IngestionJob:
    return IngestionJob(
        id=job_id,
        document_id=f"doc-{job_id}",
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )


async def _schema_ready() -> bool:
    return True


@pytest.fixture(autouse=True)
def _reset_wakeup() -> Iterator[None]:
    ingestion_worker._WAKEUP.clear()
    yield
    ingestion_worker._WAKEUP.clear()


async def test_worker_runs_all_queued_jobs() -> None:
    """QUEUED ジョブを取り出して全件 job_runner に渡す。"""
    queued = [_job(f"j{i}") for i in range(3)]
    executed: list[str] = []
    stop = asyncio.Event()

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        batch = queued[:limit]
        del queued[:limit]
        return batch

    async def runner(job_id: str) -> None:
        executed.append(job_id)
        if len(executed) == 3:
            stop.set()

    async def recover() -> Sequence[IngestionJob]:
        return []

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        concurrency=2,
        # fetch が返した job を queue から消すので、全部を起動できる重い段の上限にする(auto は
        # vCPU で変わる)。
        heavy_phase_concurrency=2,
        poll_interval_seconds=0.05,
    )
    await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=5)

    assert sorted(executed) == ["j0", "j1", "j2"]


async def test_worker_pauses_until_system_schema_is_ready(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未初期化時は queue table を照会せず、ready 遷移後に自動再開する。"""
    stop = asyncio.Event()
    checks = 0
    fetch_calls = 0

    async def schema_ready() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    async def fetch(
        _limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        nonlocal fetch_calls
        fetch_calls += 1
        stop.set()
        return []

    async def runner(_job_id: str) -> None:  # pragma: no cover - job はない
        raise AssertionError("no queued job expected")

    async def recover() -> Sequence[IngestionJob]:
        return []

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=schema_ready,
        poll_interval_seconds=0.01,
    )
    with caplog.at_level("INFO", logger="app.rag.ingestion_worker"):
        await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=5)

    assert fetch_calls == 1
    assert [record.message for record in caplog.records].count(
        "ingestion_worker_schema_setup_required"
    ) == 1
    assert [record.message for record in caplog.records].count("ingestion_worker_schema_ready") == 1


async def test_worker_respects_concurrency_limit() -> None:
    """同時実行数は concurrency を超えない。"""
    queued = [_job(f"j{i}") for i in range(5)]
    stop = asyncio.Event()
    current = 0
    observed_max = 0
    completed = 0

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        batch = queued[:limit]
        del queued[:limit]
        return batch

    async def runner(job_id: str) -> None:
        nonlocal current, observed_max, completed
        current += 1
        observed_max = max(observed_max, current)
        await asyncio.sleep(0.05)
        current -= 1
        completed += 1
        if completed == 5:
            stop.set()

    async def recover() -> Sequence[IngestionJob]:
        return []

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        concurrency=2,
        heavy_phase_concurrency=2,
        poll_interval_seconds=0.05,
    )
    await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=5)

    assert completed == 5
    assert observed_max == 2


async def test_worker_does_not_redispatch_inflight_job() -> None:
    """同じ QUEUED ジョブが続けて返っても二重起動しない。"""
    job = _job("dup")
    starts: list[str] = []
    release = asyncio.Event()
    stop = asyncio.Event()

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        # claim 前の窓で同じジョブが見え続ける状況を模す。
        return [job]

    async def runner(job_id: str) -> None:
        starts.append(job_id)
        await release.wait()

    async def recover() -> Sequence[IngestionJob]:
        return []

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        concurrency=2,
        poll_interval_seconds=0.01,
    )
    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    await asyncio.sleep(0.1)
    release.set()
    stop.set()
    ingestion_worker.request_ingestion_worker_wakeup()
    await asyncio.wait_for(task, timeout=5)

    # 実行中（in-flight）の間は同一 id を再ディスパッチしない。
    assert starts.count("dup") == 1


def test_dispatch_uses_worker_wakeup(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 入口では取込を実行せず、ワーカーへ起床通知のみ行う。"""
    from app.api.routes import documents

    dedicated = get_settings().model_copy(update={"ingestion_queue_dedicated_worker_enabled": True})
    monkeypatch.setattr(documents, "get_settings", lambda: dedicated)
    ingestion_worker._WAKEUP.clear()

    documents._dispatch_ingestion_job("job-1", force=False)

    assert ingestion_worker._WAKEUP.is_set()


def test_dispatch_still_only_wakes_worker_when_dedicated_mode_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """legacy 設定でも FastAPI BackgroundTasks へ戻さない。"""
    from app.api.routes import documents

    inline = get_settings().model_copy(update={"ingestion_queue_dedicated_worker_enabled": False})
    monkeypatch.setattr(documents, "get_settings", lambda: inline)
    ingestion_worker._WAKEUP.clear()

    documents._dispatch_ingestion_job("job-2", force=True)

    assert ingestion_worker._WAKEUP.is_set()


class _FakeProcess:
    def __init__(self, return_code: int) -> None:
        self.returncode: int | None = None
        self._return_code = return_code
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _HangingProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__(-15)
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        self.returncode = self._return_code
        return self._return_code

    def terminate(self) -> None:
        super().terminate()
        self._done.set()

    def kill(self) -> None:
        super().kill()
        self._return_code = -9
        self._done.set()


async def test_subprocess_runner_invokes_single_job_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process isolation は job id を単発 runner へ渡す。"""
    captured: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*cmd: str) -> _FakeProcess:
        captured.append(cmd)
        return _FakeProcess(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await ingestion_worker.run_ingestion_job_subprocess("job-subprocess")

    assert captured == [
        (
            sys.executable,
            "-m",
            "app.rag.ingestion_job_runner",
            "job-subprocess",
        )
    ]


async def test_subprocess_runner_raises_without_finishing_job_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child process 異常終了時は worker 側で成功扱いしない。"""

    async def fake_create_subprocess_exec(*cmd: str) -> _FakeProcess:
        _ = cmd
        return _FakeProcess(7)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(IngestionJobSubprocessError):
        await ingestion_worker.run_ingestion_job_subprocess("job-failed-child")


async def test_subprocess_runner_times_out_and_terminates_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parser 子プロセスが固着したら job runner を待ち続けない。"""
    process = _HangingProcess()

    async def fake_create_subprocess_exec(*cmd: str) -> _HangingProcess:
        _ = cmd
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(IngestionJobTimeoutError, match="全体の上限"):
        await ingestion_worker.run_ingestion_job_subprocess(
            "job-timeout",
            timeout_seconds=0.01,
        )

    assert process.terminated is True


class _FinishableProcess(_HangingProcess):
    """テストが ``finish`` を呼ぶまで終わらない子(正常終了)。"""

    def finish(self) -> None:
        self._return_code = 0
        self._done.set()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _running_job(progress: IngestionJobProgress | None) -> IngestionJob:
    return _job("job-progress").model_copy(
        update={
            "status": IngestionJobStatus.RUNNING,
            "phase": IngestionJobPhase.EXTRACT,
            "progress": progress,
        }
    )


def _vision_progress(done: int, *, finished: bool = False) -> IngestionJobProgress:
    return IngestionJobProgress(
        stage="vision",
        parse=IngestionParseProgress(engine="docling", completed=82, total=82, finished=True),
        vision=IngestionVisionProgress(
            succeeded=done,
            running=0 if finished else 1,
            total_known=232,
            total_final=True,
            finished=finished,
        ),
        updated_at=datetime(2026, 10, 15, tzinfo=UTC) + timedelta(seconds=done),
    )


def _patch_child(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> None:
    async def fake_create_subprocess_exec(*cmd: str) -> _FakeProcess:
        _ = cmd
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


# 停滞と全体の上限は差し替えた時計で判定する。1 回の読み出しで時計を 60 秒進め、実時間では
# 待たない(読み出しの間隔だけ 1 ミリ秒)。
_READ_STEP_SECONDS = 60.0
_POLL_SECONDS = 0.001


async def test_subprocess_runner_keeps_progressing_job_beyond_stall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """進んでいる job は、停滞の時間を何度超える長さでも止めない(#2003)。"""
    process = _FinishableProcess()
    _patch_child(monkeypatch, process)
    clock = _FakeClock()
    reads = 0

    async def read_job(job_id: str) -> IngestionJob | None:
        nonlocal reads
        assert job_id == "job-progress"
        reads += 1
        clock.now += _READ_STEP_SECONDS
        if reads == 100:
            process.finish()
        return _running_job(_vision_progress(reads))

    await ingestion_worker.run_ingestion_job_subprocess(
        "job-progress",
        timeout_seconds=86400.0,
        stall_timeout_seconds=100.0,
        progress_poll_interval_seconds=_POLL_SECONDS,
        read_job=read_job,
        clock=clock,
    )

    assert process.terminated is False
    # 停滞の時間(100 秒)の 60 倍の長さを走らせた。
    assert clock.now == pytest.approx(100 * _READ_STEP_SECONDS)


async def test_subprocess_runner_stops_job_whose_progress_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """進捗を出す段の途中で進捗が変わらなければ、停滞の時間で止めて理由を出す(#2003)。"""
    process = _HangingProcess()
    _patch_child(monkeypatch, process)
    clock = _FakeClock()

    async def read_job(job_id: str) -> IngestionJob | None:
        _ = job_id
        clock.now += _READ_STEP_SECONDS
        return _running_job(_vision_progress(120))

    with pytest.raises(IngestionJobStalledError) as raised:
        await ingestion_worker.run_ingestion_job_subprocess(
            "job-progress",
            timeout_seconds=86400.0,
            stall_timeout_seconds=1800.0,
            progress_poll_interval_seconds=_POLL_SECONDS,
            read_job=read_job,
            clock=clock,
        )

    assert process.terminated is True
    # 最初に読んだ時刻(60 秒)から 1800 秒。
    assert clock.now == pytest.approx(60.0 + 1800.0)
    message = str(raised.value)
    assert "1800 秒（30 分）進まなかった" in message
    assert "最後に進んだ段: 図・表の説明 120 / 232 件" in message
    assert "RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS" in message
    assert len(message) <= 500


async def test_subprocess_runner_stops_progressing_job_at_overall_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """進んでいても、全体の上限を超えたら止め、停滞とは別の理由を出す(#2003)。"""
    process = _HangingProcess()
    _patch_child(monkeypatch, process)
    clock = _FakeClock()
    reads = 0

    async def read_job(job_id: str) -> IngestionJob | None:
        nonlocal reads
        _ = job_id
        reads += 1
        clock.now += _READ_STEP_SECONDS
        return _running_job(_vision_progress(reads))

    with pytest.raises(IngestionJobTimeoutError) as raised:
        await ingestion_worker.run_ingestion_job_subprocess(
            "job-progress",
            timeout_seconds=3600.0,
            stall_timeout_seconds=1800.0,
            progress_poll_interval_seconds=_POLL_SECONDS,
            read_job=read_job,
            clock=clock,
        )

    assert process.terminated is True
    assert clock.now == pytest.approx(3600.0)
    message = str(raised.value)
    assert "全体の上限（3600 秒（1 時間））" in message
    assert "最後に進んだ段: 図・表の説明 60 / 232 件" in message
    assert "RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS" in message
    assert not isinstance(raised.value, IngestionJobStalledError)


@pytest.mark.parametrize(
    ("progress", "read_fails"),
    [
        (None, False),
        (_vision_progress(232, finished=True), False),
        (None, True),
    ],
    ids=["no-progress-stage", "after-extraction", "read-failure"],
)
async def test_subprocess_runner_does_not_stall_outside_progress_stages(
    monkeypatch: pytest.MonkeyPatch,
    progress: IngestionJobProgress | None,
    read_fails: bool,
) -> None:
    """進捗を出さない段(抽出の前後)と、進捗を読めないときは停滞で止めない(#2003)。"""
    process = _FinishableProcess()
    _patch_child(monkeypatch, process)
    clock = _FakeClock()
    reads = 0

    async def read_job(job_id: str) -> IngestionJob | None:
        nonlocal reads
        _ = job_id
        reads += 1
        clock.now += _READ_STEP_SECONDS
        if reads == 50:
            process.finish()
        if read_fails:
            raise RuntimeError("database unavailable")
        return _running_job(progress)

    await ingestion_worker.run_ingestion_job_subprocess(
        "job-progress",
        timeout_seconds=86400.0,
        stall_timeout_seconds=100.0,
        progress_poll_interval_seconds=_POLL_SECONDS,
        read_job=read_job,
        clock=clock,
    )

    assert process.terminated is False


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (None, "進捗の記録なし"),
        (
            IngestionJobProgress(
                stage="parse",
                parse=IngestionParseProgress(completed=12, total=82),
                updated_at=datetime(2026, 10, 15, tzinfo=UTC),
            ),
            "解析 12 / 82 ページ",
        ),
        (
            IngestionJobProgress(
                stage="parse",
                parse=IngestionParseProgress(completed=3),
                updated_at=datetime(2026, 10, 15, tzinfo=UTC),
            ),
            "解析 3 ページ（総ページ数は未確定）",
        ),
        (
            IngestionJobProgress(
                stage="vision",
                vision=IngestionVisionProgress(succeeded=4, failed=1, total_known=10),
                updated_at=datetime(2026, 10, 15, tzinfo=UTC),
            ),
            "図・表の説明 5 / 10 件以上",
        ),
        (_vision_progress(232, finished=True), "図・表の説明の完了の後"),
        (
            IngestionJobProgress(
                stage="parse",
                parse=IngestionParseProgress(
                    completed=4,
                    total=10,
                    segments=[
                        IngestionParseSegmentProgress(
                            segment_id="d:p1-10", page_start=1, page_end=10, status="SUCCEEDED"
                        ),
                        IngestionParseSegmentProgress(
                            segment_id="d:p11-20",
                            page_start=11,
                            page_end=20,
                            status="RUNNING",
                            completed=4,
                        ),
                        IngestionParseSegmentProgress(
                            segment_id="d:p21-23", page_start=21, page_end=23, status="QUEUED"
                        ),
                    ],
                ),
                updated_at=datetime(2026, 10, 15, tzinfo=UTC),
            ),
            "解析 完了 10 / 23 ページ（解析中: p.11-20 の 4 / 10 ページ）",
        ),
    ],
    ids=[
        "none",
        "parse",
        "parse-total-unknown",
        "vision-total-open",
        "vision-finished",
        "parse-segments",
    ],
)
def test_describe_ingestion_progress(progress: IngestionJobProgress | None, expected: str) -> None:
    """失敗の文言に出す最後の段と件数(#2003)。"""
    assert ingestion_worker.describe_ingestion_progress(progress).startswith(expected)


async def test_process_isolation_runner_uses_job_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process isolation は parser HTTP timeout ではなく job 全体 timeout を使う。"""
    captured: dict[str, float | None] = {}

    async def fake_run(
        job_id: str,
        *,
        timeout_seconds: float | None = None,
        stall_timeout_seconds: float | None = None,
        progress_poll_interval_seconds: float | None = None,
        lease_owner: str | None = None,
    ) -> None:
        assert job_id == "job-subprocess-timeout"
        assert lease_owner is None
        captured["timeout_seconds"] = timeout_seconds
        captured["stall_timeout_seconds"] = stall_timeout_seconds
        captured["progress_poll_interval_seconds"] = progress_poll_interval_seconds

    monkeypatch.setattr(ingestion_worker, "run_ingestion_job_subprocess", fake_run)
    settings = get_settings().model_copy(
        update={
            "ingestion_queue_process_isolation_enabled": True,
            "ingestion_job_subprocess_timeout_seconds": 42.0,
            "ingestion_job_stall_timeout_seconds": 7.0,
            "ingestion_queue_heartbeat_interval_seconds": 3.0,
            "rag_parser_service_timeout_seconds": 1.0,
        }
    )

    runner = ingestion_worker._job_runner_for_settings(settings)
    await runner("job-subprocess-timeout")

    assert captured == {
        "timeout_seconds": 42.0,
        "stall_timeout_seconds": 7.0,
        "progress_poll_interval_seconds": 3.0,
    }


async def test_worker_marks_running_job_failed_when_runner_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子プロセス異常終了後に RUNNING job を放置しない。"""
    queued = [_job("crash")]
    running = queued[0].model_copy(update={"status": IngestionJobStatus.RUNNING})
    updates: dict[str, object] = {}
    stop = asyncio.Event()

    class FakeOracle:
        async def get_ingestion_job(self, job_id: str) -> IngestionJob:
            assert job_id == "crash"
            return running

        async def transition_ingestion_job(self, job_id: str, **kwargs: object) -> IngestionJob:
            assert job_id == "crash"
            # 完了・失敗の書き込みは RUNNING のときだけに限る(#305)。
            assert kwargs.pop("from_statuses") == (IngestionJobStatus.RUNNING,)
            updates["status"] = kwargs.pop("to_status")
            updates.update(kwargs)
            stop.set()
            return running.model_copy(update={"status": updates["status"], **kwargs})

        async def update_document_status(
            self,
            document_id: str,
            status: FileStatus,
            error_message: str | None = None,
        ) -> None:
            updates["document_id"] = document_id
            updates["document_status"] = status
            updates["document_error"] = error_message

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        _ = limit
        batch = queued[:]
        queued.clear()
        return batch

    async def runner(job_id: str) -> None:
        assert job_id == "crash"
        raise IngestionJobSubprocessError("child died")

    async def recover() -> Sequence[IngestionJob]:
        return []

    monkeypatch.setattr(ingestion_worker, "OracleClient", FakeOracle)
    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        concurrency=1,
        poll_interval_seconds=0.01,
    )
    await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=5)

    assert updates["status"] is IngestionJobStatus.FAILED
    assert updates["error_message"] == "child died"
    assert updates["document_status"] is FileStatus.ERROR
    # FAILED は自分(この worker)の lease の job のときだけ書く(#359)。
    assert updates["lease_owner"] == worker.worker_id


class _CrashedJobOracle:
    """子プロセスの異常終了後の後始末(_mark_running_job_failed)用の fake。"""

    def __init__(self, job: IngestionJob) -> None:
        self.job = job
        self.document_updates: list[tuple[str, FileStatus]] = []
        self.recipe_updates: list[dict[str, object]] = []
        self.recipe_status = FileStatus.INGESTING

    async def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        return self.job if job_id == self.job.id else None

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
        # lease を持たない実行(lease_owner なし)の経路だけを扱う(lease の条件は #359 のテスト)。
        assert lease_owner is None
        if job_id != self.job.id or self.job.status not in from_statuses:
            return None
        self.job = self.job.model_copy(
            update={
                "status": to_status,
                "error_message": error_message,
                "finished_at": finished_at,
            }
        )
        return self.job

    async def list_document_ingestion_jobs(
        self, document_id: str, *, status: IngestionJobStatus | None = None
    ) -> list[IngestionJob]:
        jobs = [self.job] if self.job.document_id == document_id else []
        return [job for job in jobs if status is None or job.status == status]

    async def update_document_status(
        self, document_id: str, status: FileStatus, error_message: str | None = None
    ) -> None:
        _ = error_message
        self.document_updates.append((document_id, status))

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, object]:
        return {
            "document_id": document_id,
            "recipe_id": recipe_id,
            "status": self.recipe_status.value,
        }

    async def update_document_recipe_status(self, **kwargs: object) -> None:
        self.recipe_updates.append(kwargs)
        status = kwargs["status"]
        assert isinstance(status, FileStatus)
        self.recipe_status = status


def _recipe_job(status: IngestionJobStatus) -> IngestionJob:
    return IngestionJob(
        id="job-recipe",
        document_id="doc-recipe",
        recipe_id="recipe-2",
        recipe_revision=1,
        status=status,
        phase=IngestionJobPhase.PREPROCESS,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )


async def test_mark_running_recipe_job_failed_changes_only_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピの job が異常終了したら、レシピ行だけ ERROR にし文書の status は変えない(#305)。"""
    fake = _CrashedJobOracle(_recipe_job(IngestionJobStatus.RUNNING))
    monkeypatch.setattr(ingestion_worker, "OracleClient", lambda: fake)

    await ingestion_worker._mark_running_job_failed(
        "job-recipe", error=IngestionJobSubprocessError("child died")
    )

    assert fake.job.status == IngestionJobStatus.FAILED
    assert fake.document_updates == []
    assert fake.recipe_updates[-1]["recipe_id"] == "recipe-2"
    assert fake.recipe_updates[-1]["status"] == FileStatus.ERROR
    # 失敗した工程はレシピ行の現在 status(INGESTING = 解析)から決める。
    assert fake.recipe_updates[-1]["failed_phase"] == IngestionJobPhase.EXTRACT


async def test_mark_running_job_failed_restores_cancelled_recipe_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取り消し済みの job の子が異常終了したら、FAILED にせず取り消し後の状態へ戻す(#305)。"""
    fake = _CrashedJobOracle(_recipe_job(IngestionJobStatus.CANCELLED))
    monkeypatch.setattr(ingestion_worker, "OracleClient", lambda: fake)

    await ingestion_worker._mark_running_job_failed(
        "job-recipe", error=IngestionJobSubprocessError("child died")
    )

    assert fake.job.status == IngestionJobStatus.CANCELLED
    assert fake.document_updates == []
    assert fake.recipe_status == FileStatus.UPLOADED


async def test_default_fetch_excludes_inflight_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """キュー消費は claim できる QUEUED job を取り、自分が実行中の job は除く(#357)。"""
    captured: dict[str, object] = {}

    async def fake_list(self: object, **kwargs: object) -> Sequence[IngestionJob]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "app.clients.oracle.OracleClient.list_dispatchable_ingestion_jobs",
        fake_list,
    )
    worker = IngestionQueueWorker(settings=get_settings(), schema_ready=_schema_ready)
    worker._inflight.update({"job-b", "job-a"})
    result = await worker._default_fetch_queued(5)

    assert result == []
    assert captured == {"limit": 5, "exclude_job_ids": ["job-a", "job-b"], "exclude_phases": ()}


async def test_worker_recovers_stale_jobs_before_consuming() -> None:
    """起動時に stale RUNNING ジョブの回復を試みる。"""
    recovered_calls = 0
    stop = asyncio.Event()

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        stop.set()
        return []

    async def runner(job_id: str) -> None:  # pragma: no cover - 呼ばれない
        raise AssertionError("no queued job expected")

    async def recover() -> Sequence[IngestionJob]:
        nonlocal recovered_calls
        recovered_calls += 1
        return [_job("stale")]

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        concurrency=2,
        poll_interval_seconds=0.01,
    )
    await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=5)

    assert recovered_calls == 1


async def test_worker_recovers_stale_jobs_periodically_when_idle() -> None:
    """アイドル中は recovery interval ごとに固着回復を再実行する。"""
    recovered_calls = 0
    fetch_calls = 0
    stop = asyncio.Event()

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls >= 3:
            stop.set()
        return []

    async def runner(job_id: str) -> None:  # pragma: no cover - 呼ばれない
        raise AssertionError("no queued job expected")

    async def recover() -> Sequence[IngestionJob]:
        nonlocal recovered_calls
        recovered_calls += 1
        return []

    settings = get_settings().model_copy(update={"ingestion_queue_recovery_interval_seconds": 0.0})
    worker = IngestionQueueWorker(
        settings=settings,
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        concurrency=2,
        poll_interval_seconds=0.01,
    )
    await asyncio.wait_for(worker.run_forever(stop_event=stop), timeout=5)

    # 起動時 1 回 + アイドルサイクルでの定期回復で 2 回以上呼ばれる。
    assert recovered_calls >= 2


def _phase_job(job_id: str, phase: IngestionJobPhase) -> IngestionJob:
    return _job(job_id).model_copy(update={"phase": phase})


async def test_worker_caps_heavy_phases_and_fills_free_slots_with_light_phases() -> None:
    """重い段(ファイル準備・抽出)は上限まで。残りの枠は後ろの軽い段で埋める(#2027)。

    段の枠が無いと、取る順番の先頭の重い段が全体の枠を埋め、軽い段(Chunk 作成・索引)が
    後ろで待ち続ける。
    """
    queue = [
        _phase_job("pre-1", IngestionJobPhase.PREPROCESS),
        _phase_job("pre-2", IngestionJobPhase.PREPROCESS),
        _phase_job("ext-3", IngestionJobPhase.EXTRACT),
        _phase_job("chunk-1", IngestionJobPhase.CHUNK),
        _phase_job("index-2", IngestionJobPhase.INDEX),
    ]
    fetches: list[tuple[int, frozenset[IngestionJobPhase]]] = []
    started: list[str] = []
    release = asyncio.Event()

    async def fetch(
        limit: int, exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        fetches.append((limit, frozenset(exclude_phases)))
        return [
            job
            for job in queue
            if job.id not in worker._inflight and job.phase not in exclude_phases
        ][:limit]

    async def runner(job_id: str) -> None:
        started.append(job_id)
        await release.wait()

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        schema_ready=_schema_ready,
        concurrency=4,
        heavy_phase_concurrency=2,
    )

    # 1 回目: 先頭の 4 件を取り、重い段は 2 件まで起動し、3 件目の重い段を飛ばして軽い段を起動。
    assert await worker._dispatch_available() == 3
    # 2 回目: 重い段は上限なので除いて取り、残りの枠を軽い段で埋める。
    assert await worker._dispatch_available() == 1
    # 枠が埋まったら取らない。
    assert await worker._dispatch_available() == 0
    await asyncio.sleep(0)

    assert sorted(started) == ["chunk-1", "index-2", "pre-1", "pre-2"]
    assert fetches == [(4, frozenset()), (1, INGESTION_HEAVY_PHASES)]
    release.set()
    await asyncio.gather(*worker._tasks)
    assert worker._inflight == set()
    assert worker._inflight_heavy == set()


def test_worker_heavy_phase_concurrency_never_exceeds_total() -> None:
    """重い段の上限は全体の枠を超えない(全体を下げたときは全体で頭打ち)。"""
    worker = IngestionQueueWorker(
        settings=get_settings(),
        schema_ready=_schema_ready,
        concurrency=2,
        heavy_phase_concurrency=5,
    )
    assert worker._heavy_phase_concurrency == 2


async def test_worker_sweeps_document_cleanups_without_blocking_dispatch() -> None:
    """文書の削除の後始末(#2452)は別の task で動き、遅くても失敗しても job を止めない。"""
    starts: list[str] = []
    sweeps: list[str] = []
    sweep_release = asyncio.Event()
    stop = asyncio.Event()
    jobs = [_job("after-sweep")]

    async def fetch(
        limit: int, _exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        return jobs[:limit]

    async def runner(job_id: str) -> None:
        starts.append(job_id)
        jobs.clear()

    async def recover() -> Sequence[IngestionJob]:
        return []

    async def sweep() -> int:
        sweeps.append("start")
        await sweep_release.wait()
        raise RuntimeError("storage unavailable")

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=runner,
        fetch_queued=fetch,
        recover_stale=recover,
        schema_ready=_schema_ready,
        poll_interval_seconds=0.01,
        sweep_document_cleanups=sweep,
    )
    task = asyncio.create_task(worker.run_forever(stop_event=stop))
    for _ in range(100):
        if starts:
            break
        await asyncio.sleep(0.01)

    # 後始末が終わる前に job が始まる。
    assert starts == ["after-sweep"]
    assert sweeps == ["start"]
    sweep_release.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    ingestion_worker.request_ingestion_worker_wakeup()
    await asyncio.wait_for(task, timeout=5)
