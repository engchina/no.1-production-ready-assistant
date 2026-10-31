"""Run の claim と実行の世代（fencing token）の契約（#2606）。

queued の Run の lease が切れて別の worker が claim し直した後に、旧所有者が開始・書き戻しをできない
ことを、差し替えた時計と memory の repository（1 プロセス）・偽の Oracle の表（2 プロセス）で、
待ちも実サービスも使わずに確かめる。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from checkpoint_support import CheckpointTable
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime, run_claim, runtime, runtime_dispatcher
from app.features.agent import runtime_checkpoint_store as checkpoint_store
from app.features.agent.run_claim import RunClaim
from app.features.agent.runtime import (
    AgentRuntimeOracleCheckpointRepository,
    AgentRuntimeRepository,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunEventType,
    RunState,
    RunStatus,
    RunUsage,
)
from app.features.agent.tools import ToolCall

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
LEASE_SECONDS = 1


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: MonkeyPatch) -> _Clock:
    value = _Clock()
    monkeypatch.setattr(runtime, "_now", value)
    return value


def _queued(repository: Any, goal: str = "合成の質問") -> RunState:
    run: RunState = repository.create_builtin_run(RunCreateRequest(goal=goal, agent_id="default"))
    assert run.status == RunStatus.QUEUED
    return run


def _claim(repository: Any, worker_id: str) -> RunClaim:
    run = repository.claim_control_plane_run(worker_id, lease_seconds=LEASE_SECONDS)
    assert run is not None
    claim = run_claim.claim_of(run.id, run.metadata)
    assert claim is not None
    return claim


def _started_events(run: RunState) -> int:
    return sum(
        1
        for event in run.events
        if event.type == RunEventType.RUN_STATUS_CHANGED
        and event.payload.get("status") == "running"
    )


def _execution(run: RunState) -> dict[str, Any]:
    value = run.metadata.get(run_claim.EXECUTION_METADATA_KEY)
    assert isinstance(value, dict)
    return value


# ---- 開始（memory。1 プロセスの中の 2 つの worker） ------------------------------------------


def test_stale_owner_cannot_start_after_the_lease_is_reclaimed(clock: _Clock) -> None:
    """A の claim → 期限切れ → B の claim → A の復帰: A は開始できず、B だけが開始できる。"""
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    claim_a = _claim(repository, "worker-a")
    clock.advance(seconds=2)
    claim_b = _claim(repository, "worker-b")
    assert claim_b.run_id == claim_a.run_id == run.id
    assert claim_b.generation == claim_a.generation + 1
    assert claim_b.token != claim_a.token

    assert repository.begin_builtin_run(run.id, claim=claim_a) is None
    assert repository.begin_builtin_resume(run.id, claim=claim_a) is None
    # A の拒否は Run を変えない（B の lease が残る）。
    current = repository.get_run(run.id)
    assert current.status == RunStatus.QUEUED
    assert run_claim.claim_of(run.id, current.metadata) == claim_b

    started = repository.begin_builtin_run(run.id, claim=claim_b)
    assert started is not None
    running = repository.get_run(run.id)
    assert running.status == RunStatus.RUNNING
    assert run_claim.LEASE_METADATA_KEY not in running.metadata
    assert _execution(running)["token"] == claim_b.token
    assert _execution(running)["worker_id"] == "worker-b"
    assert _execution(running)["generation"] == claim_b.generation
    assert _started_events(running) == 1


def test_same_claim_is_started_only_once(clock: _Clock) -> None:
    """同じ claim の 2 回目の開始・別の入口（in_process・再開）の開始を受け付けない。"""
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    claim = _claim(repository, "worker-a")

    assert repository.begin_builtin_run(run.id, claim=claim) is not None
    assert repository.begin_builtin_run(run.id, claim=claim) is None
    assert repository.begin_builtin_run(run.id) is None
    assert repository.begin_builtin_resume(run.id, claim=claim) is None
    assert repository.begin_builtin_resume(run.id) is None

    running = repository.get_run(run.id)
    assert running.status == RunStatus.RUNNING
    assert _started_events(running) == 1
    # running の Run は claim し直さない。
    clock.advance(seconds=10)
    assert repository.claim_control_plane_run("worker-b", lease_seconds=LEASE_SECONDS) is None


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [(timedelta(milliseconds=-1), True), (timedelta(0), False), (timedelta(seconds=1), False)],
    ids=["just-before-expiry", "at-expiry", "after-expiry"],
)
def test_start_near_the_lease_expiry(clock: _Clock, offset: timedelta, accepted: bool) -> None:
    """期限の直前は開始でき、期限ちょうど・後は開始できない（claim し直せる時刻と同じ境界）。"""
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    claim = _claim(repository, "worker-a")
    clock.advance(seconds=LEASE_SECONDS)
    clock.now += offset

    started = repository.begin_builtin_run(run.id, claim=claim)

    assert (started is not None) is accepted
    status = repository.get_run(run.id).status
    assert status == (RunStatus.RUNNING if accepted else RunStatus.QUEUED)
    # 開始しなかった Run は、別の worker が claim し直せる。
    reclaimed = repository.claim_control_plane_run("worker-b", lease_seconds=LEASE_SECONDS)
    assert (reclaimed is None) is accepted


def test_cancelled_claimed_run_is_not_started(clock: _Clock) -> None:
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    claim = _claim(repository, "worker-a")
    repository.cancel_run(run.id)

    assert repository.begin_builtin_run(run.id, claim=claim) is None

    assert repository.get_run(run.id).status == RunStatus.CANCELLED


def test_in_process_start_does_not_take_over_an_active_lease(clock: _Clock) -> None:
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    _claim(repository, "worker-a")

    assert repository.begin_builtin_run(run.id) is None

    clock.advance(seconds=2)
    assert repository.begin_builtin_run(run.id) is not None
    running = repository.get_run(run.id)
    assert running.status == RunStatus.RUNNING
    assert _execution(running)["worker_id"] is None
    # in_process の開始も、claim と同じ列の次の世代になる。
    assert _execution(running)["generation"] == 2


def test_rejected_start_is_logged_with_the_reason(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    claim_a = _claim(repository, "worker-a")
    clock.advance(seconds=2)
    _claim(repository, "worker-b")

    with caplog.at_level(logging.WARNING, logger=runtime.logger.name):
        assert repository.begin_builtin_run(run.id, claim=claim_a) is None

    records = [r for r in caplog.records if r.getMessage() == "agent_runtime_begin_rejected"]
    assert [getattr(r, "reason", None) for r in records] == [run_claim.REJECT_LEASE_MISMATCH]


# ---- 書き戻し（実行の世代） --------------------------------------------------------------------


def _approval_call() -> ToolCall:
    return ToolCall(name="synthetic_write", arguments={"value": 1}, trace_id="call-1")


def test_resume_after_approval_rejects_the_old_owner_and_its_late_writes(clock: _Clock) -> None:
    """承認待ちからの再開: 旧所有者の再開・遅れた完了 / 失敗 / 利用量は新しい実行を上書きしない。"""
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    claim_a = _claim(repository, "worker-a")
    started = repository.begin_builtin_run(run.id, claim=claim_a)
    assert started is not None
    first_run = started[0]

    with run_claim.execution_fence(run.id, first_run.metadata):
        repository.request_builtin_approvals(run.id, [_approval_call()], state="{}")
        # 自分の実行が承認待ちで区切った後の遅れた失敗は、承認待ちを壊さない。
        repository.fail_builtin_run(run.id, code="runtime.model_failed", detail="遅れた失敗")
    waiting = repository.get_run(run.id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    # 承認待ちは claim も開始もしない。
    assert repository.claim_control_plane_run("worker-b", lease_seconds=LEASE_SECONDS) is None
    assert repository.begin_builtin_resume(run.id) is None

    repository.decide_approval(waiting.approvals[0].id, ApprovalDecisionRequest(approved=True))
    assert repository.get_run(run.id).status == RunStatus.QUEUED
    claim_b = _claim(repository, "worker-b")
    assert claim_b.generation == claim_a.generation + 1
    assert repository.begin_builtin_resume(run.id, claim=claim_a) is None
    resumed = repository.begin_builtin_resume(run.id, claim=claim_b)
    assert resumed is not None
    second_run = resumed[0]
    repository.record_builtin_usage(run.id, RunUsage(model="m", requests=2, total_tokens=20))

    # 旧所有者（A）の遅れた書き戻しは、running の新しい実行（B）を変えない。
    with run_claim.execution_fence(run.id, first_run.metadata):
        repository.complete_builtin_run(run.id, "古い回答")
        repository.fail_builtin_run(run.id, code="runtime.model_failed", detail="古い失敗")
        repository.record_builtin_usage(run.id, RunUsage(model="m", requests=1, total_tokens=5))
        repository.request_builtin_approvals(run.id, [_approval_call()], state="{}")
    current = repository.get_run(run.id)
    assert current.status == RunStatus.RUNNING
    assert current.usage is not None and current.usage.total_tokens == 20
    assert not current.artifacts
    assert len(current.approvals) == 1

    # 新しい実行（B）の書き戻しは受け付ける。
    with run_claim.execution_fence(run.id, second_run.metadata):
        completed = repository.complete_builtin_run(run.id, "新しい回答")
    assert completed.status == RunStatus.COMPLETED
    assert [a.content.get("text") for a in completed.artifacts if a.kind == "answer"] == [
        "新しい回答"
    ]


def test_writes_outside_an_execution_are_not_fenced(clock: _Clock) -> None:
    """実行の外（API の操作・保守の呼び出し）の書き戻しは、今までどおり受け付ける。"""
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    assert repository.begin_builtin_run(run.id) is not None

    failed = repository.fail_builtin_run(run.id, code="runtime.model_failed", detail="失敗")

    assert failed.status == RunStatus.FAILED


def test_cancelled_run_keeps_the_usage_of_its_execution(clock: _Clock) -> None:
    """実行中に取り消した Run も、その実行の利用量は残す（終了の判定は既存のまま）。"""
    repository = AgentRuntimeRepository()
    run = _queued(repository)
    started = repository.begin_builtin_run(run.id)
    assert started is not None
    repository.cancel_run(run.id)

    with run_claim.execution_fence(run.id, started[0].metadata):
        repository.record_builtin_usage(run.id, RunUsage(model="m", requests=1, total_tokens=7))
        completed = repository.complete_builtin_run(run.id, "取消の後の回答")

    assert completed.status == RunStatus.CANCELLED
    assert completed.usage is not None and completed.usage.total_tokens == 7


# ---- 組み込み Runtime と dispatcher の入口 ---------------------------------------------------


@pytest.fixture
def memory_repository() -> Iterator[AgentRuntimeRepository]:
    repository = AgentRuntimeRepository()
    runtime.set_runtime_repository(repository)
    try:
        yield repository
    finally:
        runtime.set_runtime_repository(None)


def test_execute_run_with_a_stale_claim_does_not_build_the_agent(
    clock: _Clock, memory_repository: AgentRuntimeRepository, monkeypatch: MonkeyPatch
) -> None:
    """旧所有者の `execute_run` はモデルもツールも使わず、新しい所有者の実行だけが動く。"""
    built: list[str] = []

    def build_sdk_agent(run_id: str, **_kwargs: object) -> object:
        built.append(run_id)
        raise builtin_runtime.BuiltinRuntimeError("runtime.synthetic", "合成の失敗")

    monkeypatch.setattr(builtin_runtime, "build_sdk_agent", build_sdk_agent)
    run = _queued(memory_repository)
    claim_a = _claim(memory_repository, "worker-a")
    clock.advance(seconds=2)
    claim_b = _claim(memory_repository, "worker-b")

    anyio.run(lambda: builtin_runtime.execute_run(run.id, claim=claim_a))
    assert built == []
    assert memory_repository.get_run(run.id).status == RunStatus.QUEUED

    anyio.run(lambda: builtin_runtime.execute_run(run.id, claim=claim_b))
    assert built == [run.id]
    # 実行の中の失敗の書き戻し（asyncio.to_thread の先を含む）は、自分の実行として受け付ける。
    failed = memory_repository.get_run(run.id)
    assert failed.status == RunStatus.FAILED
    assert failed.events[-1].payload["error_code"] == "runtime.synthetic"


def test_dispatcher_passes_its_claim_to_the_start(
    clock: _Clock, memory_repository: AgentRuntimeRepository, monkeypatch: MonkeyPatch
) -> None:
    received: list[tuple[str, RunClaim | None]] = []

    async def execute_run(run_id: str, *, claim: RunClaim | None = None) -> None:
        received.append((run_id, claim))

    monkeypatch.setattr(builtin_runtime, "execute_run", execute_run)
    run = _queued(memory_repository)

    assert anyio.run(runtime_dispatcher.dispatch_once, "worker-a") is True

    lease = run_claim.claim_of(run.id, memory_repository.get_run(run.id).metadata)
    assert lease is not None and lease.worker_id == "worker-a"
    assert received == [(run.id, lease)]


# ---- 2 つのプロセス（偽の Oracle の表） --------------------------------------------------------


def _process(table: CheckpointTable) -> AgentRuntimeOracleCheckpointRepository:
    return AgentRuntimeOracleCheckpointRepository(connect_factory=table.connect)


def _stored(table: CheckpointTable, run_id: str) -> RunState:
    return next(run for run in table.snapshot().runs if run.id == run_id)


def test_stale_owner_in_another_process_cannot_start(clock: _Clock) -> None:
    """別のプロセスの B が claim し直した Run を、メモリの古いプロセス A は開始できない。"""
    table = CheckpointTable()
    api, process_a, process_b = _process(table), _process(table), _process(table)
    run = _queued(api)
    claim_a = _claim(process_a, "worker-a")
    clock.advance(seconds=2)
    claim_b = _claim(process_b, "worker-b")
    version = table.version()

    # A のメモリは B の claim を知らないが、開始は保存先の今の claim で判定する。
    assert process_a.begin_builtin_run(run.id, claim=claim_a) is None
    assert table.version() == version
    stored = _stored(table, run.id)
    assert stored.status == RunStatus.QUEUED
    assert run_claim.claim_of(run.id, stored.metadata) == claim_b

    assert process_b.begin_builtin_run(run.id, claim=claim_b) is not None
    stored = _stored(table, run.id)
    assert stored.status == RunStatus.RUNNING
    assert _execution(stored)["token"] == claim_b.token
    # B の開始は条件付きの遷移として、その場で保存先に書く（同じ claim の 2 回目は受け付けない）。
    assert process_b.begin_builtin_run(run.id, claim=claim_b) is None
    assert process_a.begin_builtin_resume(run.id, claim=claim_a) is None
    assert _started_events(_stored(table, run.id)) == 1


def test_start_in_one_process_blocks_a_later_claim_in_another(clock: _Clock) -> None:
    """期限の直前に A が開始すれば、期限の後の B の claim は running の Run を取らない。"""
    table = CheckpointTable()
    process_a, process_b = _process(table), _process(table)
    run = _queued(process_a)
    claim_a = _claim(process_a, "worker-a")
    clock.advance(seconds=LEASE_SECONDS, milliseconds=-1)
    assert process_a.begin_builtin_run(run.id, claim=claim_a) is not None
    clock.advance(seconds=2)

    assert process_b.claim_control_plane_run("worker-b", lease_seconds=LEASE_SECONDS) is None
    assert _stored(table, run.id).status == RunStatus.RUNNING


def test_failed_start_write_does_not_leave_a_running_run(
    clock: _Clock, monkeypatch: MonkeyPatch
) -> None:
    """開始を保存先に書けなければ、メモリも queued に戻し、同じ claim で開始し直せる。"""
    table = CheckpointTable()
    process_a = _process(table)
    run = _queued(process_a)
    claim_a = _claim(process_a, "worker-a")
    original = checkpoint_store.write_entities
    failures = [RuntimeError("ORA-03113: end-of-file on communication channel")]

    def write_entities(*args: Any, **kwargs: Any) -> None:
        if failures:
            raise failures.pop()
        original(*args, **kwargs)

    monkeypatch.setattr(checkpoint_store, "write_entities", write_entities)

    with pytest.raises(RuntimeError, match="ORA-03113"):
        process_a.begin_builtin_run(run.id, claim=claim_a)
    assert _stored(table, run.id).status == RunStatus.QUEUED

    # 次の遷移で保存先の全体を読み直し、書けなかった開始をメモリから戻す。
    assert process_a.claim_control_plane_run("worker-a", lease_seconds=LEASE_SECONDS) is None
    assert process_a.get_run(run.id).status == RunStatus.QUEUED
    assert process_a.begin_builtin_run(run.id, claim=claim_a) is not None
    assert _stored(table, run.id).status == RunStatus.RUNNING
