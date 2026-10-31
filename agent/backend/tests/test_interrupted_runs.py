"""backend の再起動で中断した Run を失敗にする（#1894）。

in_process の組み込み Runtime は Run を backend のプロセスの asyncio の task で実行する。再起動で
task が消えると Run は `running` / `queued` のまま終わらず、チャットは待ち続ける。起動で保存先を
読み込んだ後に、このプロセスの起動より前の `running` / `queued` の Run を失敗にする。
承認待ち・確認待ちは保存した SDK の状態から再開できるので残し、external の dispatcher では触らない。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.features.agent import control_plane_store, runtime, storage_bootstrap
from app.features.agent.chat_progress import run_progress_terminal
from app.features.agent.runtime import (
    INTERRUPTED_ERROR_CODE,
    INTERRUPTED_MESSAGE,
    AgentRuntimeRepository,
    AgentRuntimeSnapshot,
    ApprovalRequest,
    ApprovalStatus,
    RunEventType,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
)
from app.features.agent.tools import ToolCall
from app.settings import get_settings

# 前のプロセスが Run を最後に変えた時刻と、このプロセスの起動の時刻。
_BEFORE_RESTART = datetime(2026, 10, 14, 19, 18, 21, tzinfo=UTC)
_STARTED_AT = datetime(2026, 10, 14, 19, 18, 28, tzinfo=UTC)


def _run(run_id: str, status: RunStatus, *, updated_at: datetime = _BEFORE_RESTART) -> RunState:
    call = ToolCall(name="rag__rag_search", arguments={"query": "規程"}, trace_id=f"c_{run_id}")
    steps: list[RunStep] = []
    approvals: list[ApprovalRequest] = []
    metadata: dict[str, object] = {}
    if status == RunStatus.RUNNING:
        steps.append(
            RunStep(
                id=f"step_{run_id}",
                run_id=run_id,
                status=StepStatus.RUNNING,
                tool_call=call,
                started_at=updated_at,
            )
        )
    if status == RunStatus.WAITING_APPROVAL:
        steps.append(
            RunStep(
                id=f"step_{run_id}",
                run_id=run_id,
                status=StepStatus.WAITING_APPROVAL,
                tool_call=call,
                approval_id=f"approval_{run_id}",
            )
        )
        approvals.append(
            ApprovalRequest(
                id=f"approval_{run_id}",
                run_id=run_id,
                step_id=f"step_{run_id}",
                tool_call=call,
                status=ApprovalStatus.PENDING,
                reason="承認が必要です",
            )
        )
        metadata["_builtin_sdk_state"] = "{}"
    return RunState(
        id=run_id,
        goal="規程を調べて",
        agent_id="default",
        runtime_id="builtin",
        status=status,
        steps=steps,
        approvals=approvals,
        metadata=metadata,
        created_at=updated_at,
        updated_at=updated_at,
    )


def _saved_repository(path: Path, runs: list[RunState]) -> AgentRuntimeRepository:
    """前のプロセスが保存した snapshot を、次のプロセスとして読み込む。"""
    path.write_text(AgentRuntimeSnapshot(runs=runs).model_dump_json(), encoding="utf-8")
    return AgentRuntimeRepository(snapshot_path=path)


def _failure_event_payload(run: RunState) -> dict[str, object]:
    events = [event for event in run.events if event.type == RunEventType.RUNTIME_FAILED]
    assert len(events) == 1
    assert events[0].message == INTERRUPTED_MESSAGE
    return dict(events[0].payload)


def test_running_and_queued_runs_before_the_start_fail_and_are_saved(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    repository = _saved_repository(
        path,
        [
            _run("run_running", RunStatus.RUNNING),
            _run("run_queued", RunStatus.QUEUED),
            _run("run_waiting", RunStatus.WAITING_APPROVAL),
            _run("run_done", RunStatus.COMPLETED),
            # このプロセスの起動の後に作った Run（このプロセスか別のプロセスが動かしている）。
            _run("run_new", RunStatus.RUNNING, updated_at=_STARTED_AT + timedelta(seconds=1)),
        ],
    )

    failed = repository.fail_interrupted_runs(updated_before=_STARTED_AT)

    assert sorted(failed) == ["run_queued", "run_running"]
    # 次の起動で読んでも失敗のまま（保存した）。
    reloaded = AgentRuntimeRepository(snapshot_path=path)
    statuses = {run.id: run.status for run in reloaded.list_runs()}
    assert statuses == {
        "run_running": RunStatus.FAILED,
        "run_queued": RunStatus.FAILED,
        "run_waiting": RunStatus.WAITING_APPROVAL,
        "run_done": RunStatus.COMPLETED,
        "run_new": RunStatus.RUNNING,
    }
    running = reloaded.get_run("run_running")
    assert _failure_event_payload(running) == {
        "error_code": INTERRUPTED_ERROR_CODE,
        "runtime_id": "builtin",
        "previous_status": "running",
        "source": "startup",
    }
    # 途中の step も終わらせる（画面に実行中の step を残さない）。
    assert [step.status for step in running.steps] == [StepStatus.FAILED]
    assert running.steps[0].completed_at is not None
    # チャットの処理の経過も失敗で終わる（終端は Run の状態が正本。#1359・#3226）。
    assert run_progress_terminal(running) == "failed"
    assert _failure_event_payload(reloaded.get_run("run_queued"))["previous_status"] == "queued"
    # 承認待ちは保存した SDK の状態から再開できるので残す。
    waiting = reloaded.get_run("run_waiting")
    assert waiting.metadata["_builtin_sdk_state"] == "{}"
    assert waiting.approvals[0].status == ApprovalStatus.PENDING


def test_nothing_to_fail_does_not_save(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    repository = _saved_repository(path, [_run("run_done", RunStatus.COMPLETED)])
    saved = path.read_text(encoding="utf-8")

    assert repository.fail_interrupted_runs(updated_before=_STARTED_AT) == []
    assert path.read_text(encoding="utf-8") == saved


@pytest.fixture
def restarted(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[AgentRuntimeRepository]:
    """前のプロセスの Run を持つ保存先を、起動の読み込みの前の状態にする。"""
    repository = _saved_repository(
        tmp_path / "runtime.json",
        [_run("run_running", RunStatus.RUNNING), _run("run_waiting", RunStatus.WAITING_APPROVAL)],
    )
    previous = runtime._repository
    runtime.set_runtime_repository(repository)
    monkeypatch.setattr(storage_bootstrap, "_process_started_at", _STARTED_AT)
    monkeypatch.setattr(control_plane_store, "restore_control_plane", lambda: {})
    monkeypatch.setattr(storage_bootstrap, "_prepare_history", lambda: None)
    storage_bootstrap.reset()
    yield repository
    runtime.set_runtime_repository(previous)


def test_in_process_startup_fails_the_interrupted_runs_once(
    restarted: AgentRuntimeRepository, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", "in_process")

    storage_bootstrap.ensure_ready()

    assert restarted.get_run("run_running").status == RunStatus.FAILED
    assert restarted.get_run("run_waiting").status == RunStatus.WAITING_APPROVAL
    # 読み込みは 1 回だけ（済んだ後に別の Run が古い時刻で残っていても、もう触らない）。
    calls: list[datetime] = []
    monkeypatch.setattr(
        restarted, "fail_interrupted_runs", lambda *, updated_before: calls.append(updated_before)
    )
    storage_bootstrap.ensure_ready()
    assert calls == []


def test_external_dispatcher_keeps_the_runs(
    restarted: AgentRuntimeRepository, monkeypatch: MonkeyPatch
) -> None:
    # dispatcher が lease で claim する（API のプロセスは Run を実行していない）。
    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", "dispatcher")

    storage_bootstrap.ensure_ready()

    assert restarted.get_run("run_running").status == RunStatus.RUNNING


def test_failure_to_save_does_not_block_the_load(
    restarted: AgentRuntimeRepository, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", "in_process")

    def broken(*, updated_before: datetime) -> list[str]:
        raise RuntimeError("保存できません")

    monkeypatch.setattr(restarted, "fail_interrupted_runs", broken)

    storage_bootstrap.ensure_ready()

    assert storage_bootstrap.is_ready() is True
