"""取込の job が解析の区間の後に重い段の枠を返すテスト(#2702)。

重い段(ファイル準備・抽出)の枠は解析サービスの CPU・メモリを守るためにある。項目抽出は解析の
後に LLM の応答を待つだけなので、その間は重い段の枠を持たず、全体の枠だけで並べる。
LLM・子プロセスは決定論のスタブで置き換える。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from app.config import get_settings
from app.rag import ingestion_worker
from app.rag.ingestion_slots import heavy_slot_releaser, release_heavy_ingestion_slot
from app.rag.ingestion_worker import IngestionJobSubprocessError, IngestionQueueWorker
from app.schemas.document import IngestionJob, IngestionJobPhase, IngestionJobStatus

Fetch = Callable[[int, Collection[IngestionJobPhase]], Awaitable[Sequence[IngestionJob]]]


@pytest.fixture(autouse=True)
def _reset_wakeup() -> Iterator[None]:
    ingestion_worker._WAKEUP.clear()
    yield
    ingestion_worker._WAKEUP.clear()


def _phase_job(job_id: str, phase: IngestionJobPhase) -> IngestionJob:
    return IngestionJob(
        id=job_id,
        document_id=f"doc-{job_id}",
        status=IngestionJobStatus.QUEUED,
        phase=phase,
        parser_profile="local_text_structure",
        queued_at=datetime.now(UTC),
    )


def _heavy_queue(count: int) -> list[IngestionJob]:
    return [_phase_job(f"pre-{index}", IngestionJobPhase.PREPROCESS) for index in range(count)]


def _fetch_from(queue: list[IngestionJob], worker_ref: list[IngestionQueueWorker]) -> Fetch:
    """取る順に、実行中でなく除く段でもない QUEUED の job を返す(queue から除くのは claim の時)。"""

    async def fetch(
        limit: int, exclude_phases: Collection[IngestionJobPhase] = ()
    ) -> Sequence[IngestionJob]:
        worker = worker_ref[0]
        return [
            job
            for job in queue
            if job.id not in worker._inflight and job.phase not in exclude_phases
        ][:limit]

    return fetch


async def _schema_ready() -> bool:
    return True


async def _no_recover() -> Sequence[IngestionJob]:
    return []


def _worker(
    queue: list[IngestionJob],
    runner: Callable[[str], Awaitable[None]],
    *,
    concurrency: int,
    heavy: int,
    **kwargs: object,
) -> IngestionQueueWorker:
    worker_ref: list[IngestionQueueWorker] = []

    async def claim_and_run(job_id: str) -> None:
        # 実際の job は子の claim で QUEUED から外れる。
        queue[:] = [job for job in queue if job.id != job_id]
        await runner(job_id)

    worker = IngestionQueueWorker(
        settings=get_settings(),
        job_runner=claim_and_run,
        fetch_queued=_fetch_from(queue, worker_ref),
        recover_stale=_no_recover,
        schema_ready=_schema_ready,
        concurrency=concurrency,
        heavy_phase_concurrency=heavy,
        poll_interval_seconds=0.05,
        **kwargs,  # type: ignore[arg-type]
    )
    worker_ref.append(worker)
    return worker


async def _until(condition: Callable[[], bool], *, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


async def test_llm_wait_after_parse_does_not_hold_heavy_slot() -> None:
    """解析の後の LLM の応答待ちの間に、別の文書の重い段(解析)が始まる。

    重い段の上限 2・全体 6 で、項目抽出の応答待ちが重い段の枠を持ち続けると、同時に進むのは
    2 件だけになる。解析の区間の後に枠を返せば、応答待ちは全体の枠(6)まで並び、解析の同時
    実行は 2 を超えない。
    """
    queue = _heavy_queue(10)
    llm_gate = asyncio.Event()
    stop = asyncio.Event()
    parsing: set[str] = set()
    running: set[str] = set()
    waiting_llm: set[str] = set()
    observed = {"parsing": 0, "running": 0, "waiting_llm": 0}
    completed: list[str] = []

    async def runner(job_id: str) -> None:
        running.add(job_id)
        parsing.add(job_id)
        observed["running"] = max(observed["running"], len(running))
        observed["parsing"] = max(observed["parsing"], len(parsing))
        await asyncio.sleep(0.01)  # 解析(CPU)の区間
        parsing.discard(job_id)
        release_heavy_ingestion_slot()
        waiting_llm.add(job_id)
        observed["waiting_llm"] = max(observed["waiting_llm"], len(waiting_llm))
        await llm_gate.wait()  # 項目抽出の LLM の応答待ち
        waiting_llm.discard(job_id)
        running.discard(job_id)
        completed.append(job_id)
        if len(completed) == 10:
            stop.set()

    worker = _worker(queue, runner, concurrency=6, heavy=2)
    run = asyncio.create_task(worker.run_forever(stop_event=stop))

    # 応答待ちが重い段の枠を持たないので、全体の枠(6)まで応答待ちが並ぶ。
    await _until(lambda: len(waiting_llm) == 6)
    assert len(worker._inflight) == 6
    assert worker._inflight_heavy == set()
    # 全体の枠が埋まっている間は、次の文書を始めない。
    await asyncio.sleep(0.1)
    assert len(running) == 6
    assert len(queue) == 4

    llm_gate.set()
    await asyncio.wait_for(run, timeout=5)

    assert sorted(completed) == sorted(f"pre-{index}" for index in range(10))
    assert observed == {"parsing": 2, "running": 6, "waiting_llm": 6}
    assert worker._inflight == set()
    assert worker._inflight_heavy == set()


async def test_heavy_slot_is_held_until_parse_ends() -> None:
    """解析の区間の途中は重い段の枠を持ち、上限を超えて解析を始めない。"""
    queue = _heavy_queue(4)
    parse_gate = asyncio.Event()
    started: list[str] = []

    async def runner(job_id: str) -> None:
        started.append(job_id)
        await parse_gate.wait()
        release_heavy_ingestion_slot()

    worker = _worker(queue, runner, concurrency=6, heavy=2)

    assert await worker._dispatch_available() == 2
    await asyncio.sleep(0)
    # 重い段の上限に達しているので重い段を除いて取る(軽い段が無ければ何も起動しない)。
    assert await worker._dispatch_available() == 0
    assert sorted(started) == ["pre-0", "pre-1"]
    assert worker._inflight_heavy == {"pre-0", "pre-1"}

    parse_gate.set()
    await _until(lambda: not worker._inflight_heavy)
    # 枠が返ったら、次の重い段を取る。
    assert await worker._dispatch_available() == 2
    await asyncio.gather(*worker._tasks)
    assert worker._inflight == set()
    assert worker._inflight_heavy == set()


async def test_release_in_light_phase_job_does_not_touch_heavy_slots() -> None:
    """軽い段の job の中で返しても、重い段の枠の数は変わらない(返し方を置かない)。"""
    queue = [
        _phase_job("pre-0", IngestionJobPhase.PREPROCESS),
        _phase_job("chunk-0", IngestionJobPhase.CHUNK),
    ]
    heavy_gate = asyncio.Event()
    seen_heavy: list[set[str]] = []

    async def runner(job_id: str) -> None:
        if job_id == "chunk-0":
            release_heavy_ingestion_slot()
            seen_heavy.append(set(worker._inflight_heavy))
            return
        await heavy_gate.wait()

    worker = _worker(queue, runner, concurrency=4, heavy=1)

    assert await worker._dispatch_available() == 2
    await _until(lambda: bool(seen_heavy))
    assert seen_heavy == [{"pre-0"}]
    heavy_gate.set()
    await asyncio.gather(*worker._tasks)
    assert worker._inflight_heavy == set()


@pytest.mark.parametrize("released", [True, False], ids=["after-release", "during-parse"])
async def test_failed_job_is_marked_failed_and_frees_slots(
    monkeypatch: pytest.MonkeyPatch, released: bool
) -> None:
    """枠を返した後・返す前のどちらで失敗しても、失敗の扱い(FAILED にする)は変わらない。"""
    failed: list[tuple[str, str, str | None]] = []

    async def fake_mark_failed(
        job_id: str,
        *,
        error: Exception,
        lease_owner: str | None = None,
        expected_attempt_count: int = 0,
    ) -> None:
        failed.append((job_id, str(error), lease_owner))

    monkeypatch.setattr(ingestion_worker, "_mark_running_job_failed", fake_mark_failed)

    async def runner(job_id: str) -> None:
        if released:
            release_heavy_ingestion_slot()
            # 二度目は何もしない(1 回だけ返す)。
            release_heavy_ingestion_slot()
        raise IngestionJobSubprocessError("child died")

    worker = _worker(_heavy_queue(1), runner, concurrency=2, heavy=1, worker_id="worker-a")

    assert await worker._dispatch_available() == 1
    await asyncio.gather(*worker._tasks)

    assert failed == [("pre-0", "child died", "worker-a")]
    assert worker._inflight == set()
    assert worker._inflight_heavy == set()


async def test_cancelled_job_after_release_frees_total_slot() -> None:
    """停止で取り消した job(枠を返した後の応答待ち)は全体の枠も返す。"""
    released = asyncio.Event()

    async def runner(job_id: str) -> None:
        release_heavy_ingestion_slot()
        released.set()
        await asyncio.Event().wait()

    worker = _worker(_heavy_queue(1), runner, concurrency=2, heavy=1)

    assert await worker._dispatch_available() == 1
    await asyncio.wait_for(released.wait(), timeout=5)
    assert worker._inflight == {"pre-0"}
    assert worker._inflight_heavy == set()
    for task in list(worker._tasks):
        task.cancel()
    await asyncio.gather(*worker._tasks, return_exceptions=True)
    assert worker._inflight == set()


class _ReleasingProcess:
    """子の代わり: 受け継いだ fd に 1 byte を書いて(または書かずに)、後で終わる。"""

    def __init__(self, fd: int, *, write: bool) -> None:
        self.returncode: int | None = None
        self._fd = fd
        self._write = write
        self._done = asyncio.Event()

    def finish_parse(self) -> None:
        if self._write:
            os.write(self._fd, b"1")
        os.close(self._fd)

    def exit(self) -> None:
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self._done.set()

    def kill(self) -> None:
        self._done.set()


@pytest.mark.parametrize("write", [True, False], ids=["released", "exited-without-release"])
async def test_subprocess_runner_releases_heavy_slot_when_child_signals(
    monkeypatch: pytest.MonkeyPatch, write: bool
) -> None:
    """子が解析の区間を終えて pipe に書いたら、子の終わりを待たずに重い段の枠を返す。"""
    captured: list[tuple[tuple[str, ...], dict[str, object]]] = []
    processes: list[_ReleasingProcess] = []

    async def fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> _ReleasingProcess:
        captured.append((cmd, kwargs))
        pass_fds = cast(tuple[int, ...], kwargs["pass_fds"])
        # 子は fd を受け継ぐ(親は起動の後に自分の書き込み側を閉じる)。
        process = _ReleasingProcess(os.dup(pass_fds[0]), write=write)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    releases: list[str] = []

    async def read_job(_job_id: str) -> IngestionJob | None:
        return None

    with heavy_slot_releaser(lambda: releases.append("released")):
        run = asyncio.create_task(
            ingestion_worker.run_ingestion_job_subprocess(
                "job-pipe",
                timeout_seconds=30,
                stall_timeout_seconds=30,
                progress_poll_interval_seconds=0.05,
                read_job=read_job,
            )
        )
        await _until(lambda: bool(processes))
        processes[0].finish_parse()
        if write:
            await _until(lambda: bool(releases))
        else:
            await asyncio.sleep(0.1)
        # 子はまだ終わっていない(項目抽出の応答待ち)。
        assert not run.done()
        assert releases == (["released"] if write else [])
        processes[0].exit()
        await asyncio.wait_for(run, timeout=5)

    cmd, kwargs = captured[0]
    fd_index = cmd.index("--heavy-slot-release-fd")
    assert int(cmd[fd_index + 1]) == cast(tuple[int, ...], kwargs["pass_fds"])[0]
    assert releases == (["released"] if write else [])


async def test_job_runner_child_writes_release_to_parent_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子の runner は、job の中で枠を返すと親の pipe に 1 byte を書く(2 回目は書かない)。"""
    from app.api.routes import documents
    from app.rag import ingestion_job_runner

    calls: list[str] = []

    async def fake_run_ingestion_job(job_id: str, *, lease_owner: str | None = None) -> None:
        calls.append(job_id)
        release_heavy_ingestion_slot()
        release_heavy_ingestion_slot()

    monkeypatch.setattr(documents, "_run_ingestion_job", fake_run_ingestion_job)
    read_fd, write_fd = os.pipe()
    try:
        await ingestion_job_runner._run("job-child", heavy_slot_release_fd=write_fd)
        # 1 回だけ書いて閉じる(その後は EOF)。
        assert os.read(read_fd, 8) == b"1"
        assert os.read(read_fd, 8) == b""
    finally:
        os.close(read_fd)
    assert calls == ["job-child"]


def test_release_outside_worker_is_noop() -> None:
    """worker の外(同期の取込・評価)では何もしない。"""
    release_heavy_ingestion_slot()
