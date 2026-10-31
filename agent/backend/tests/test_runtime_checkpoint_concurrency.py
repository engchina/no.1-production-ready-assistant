"""Run・業務 Agent を 1 件 = 1 行で保存し、2 つのプロセスが書いても互いの更新を消さない。

#1916: 同じ保存先に 2 つのプロセス（worktree で主の `.env` のまま起動した 2 つ目の backend・
runtime-dispatcher）が書いても、片方の書き込みがもう片方の更新を消さない。
#1939: 変えた Run・業務 Agent の行だけを書く（以前は全体の snapshot を 1 行で書き戻し、時間が Run の
総数に比例した）。書き込み（I/O）は repository の lock の外で行い、event loop の上の変更は待たない。

2 つのプロセスは、同じ偽の表（`CheckpointTable`）を読み書きする 2 つの repository で表す。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import pytest
from checkpoint_support import KEY, CheckpointConnection, CheckpointTable
from pytest import MonkeyPatch

from app.features.agent import runtime
from app.features.agent.runtime import (
    AgentProfile,
    AgentRuntimeCheckpointCorruptError,
    AgentRuntimeMigrationRequiredError,
    AgentRuntimeOracleCheckpointRepository,
    AgentRuntimeSnapshot,
    RunState,
    RunStatus,
)
from app.features.agent.runtime_checkpoint_store import WRITER_ID


def _run(
    run_id: str,
    status: RunStatus = RunStatus.RUNNING,
    *,
    thread_id: str | None = None,
    user: str | None = None,
) -> RunState:
    return RunState(
        id=run_id,
        goal="規程を調べて",
        agent_id="default",
        runtime_id="builtin",
        status=status,
        thread_id=thread_id,
        created_by_user_uuid=user,
    )


def _table(*runs: RunState, agents: list[AgentProfile] | None = None) -> CheckpointTable:
    table = CheckpointTable()
    table.seed(runs, agents or [])
    return table


def _process(table: CheckpointTable) -> AgentRuntimeOracleCheckpointRepository:
    """同じ保存先を読み込んだ 1 つのプロセス（backend）。"""
    return AgentRuntimeOracleCheckpointRepository(connect_factory=table.connect)


def _stored_status(table: CheckpointTable) -> dict[str, RunStatus]:
    return {run.id: run.status for run in table.snapshot().runs}


def _merge_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage() == "agent_runtime_checkpoint_concurrent_write"
    ]


# ---- 変えたものだけを書く（#1939） ----------------------------------------------------------


def test_small_change_writes_only_the_changed_runs() -> None:
    """Run が多くても、会話の削除は会話の Run の行と head だけを書く（全体を書き戻さない）。"""
    runs = [_run(f"run_{index:04d}", RunStatus.COMPLETED) for index in range(1000)]
    runs.append(_run("run_t1", RunStatus.COMPLETED, thread_id="thread_x", user="u1"))
    runs.append(_run("run_t2", RunStatus.COMPLETED, thread_id="thread_x", user="u1"))
    table = _table(*runs)
    repository = _process(table)
    table.calls.clear()

    repository.delete_thread("thread_x", user_uuid="u1")

    assert table.calls.count("entity_merge") == 2
    assert table.calls.count("head_update") == 1
    stored = {run.id: run for run in table.snapshot().runs}
    assert stored["run_t1"].thread_id is None
    assert stored["run_t1"].metadata["deleted_thread_id"] == "thread_x"
    assert len(stored) == 1002
    assert table.version() == 1


def test_delete_thread_time_does_not_grow_with_the_number_of_runs(
    monkeypatch: MonkeyPatch,
) -> None:
    """Run が数千件でも、会話の削除・名前の変更・取り消しの保存は Run の数に比例しない。"""
    # 印の付け忘れの検出（テストだけ）は lock のたびに全件を比べるので、時間の計測では止める。
    monkeypatch.setattr(runtime, "VERIFY_CHANGE_TRACKING", False)

    def elapsed(count: int) -> float:
        runs = [_run(f"run_{index:05d}", RunStatus.COMPLETED) for index in range(count)]
        runs.append(_run("run_t", RunStatus.COMPLETED, thread_id="thread_x", user="u1"))
        repository = _process(_table(*runs))
        started = time.perf_counter()
        repository.rename_thread("thread_x", user_uuid="u1", title="名前")
        repository.delete_thread("thread_x", user_uuid="u1")
        repository.cancel_run("run_00001")
        return time.perf_counter() - started

    small, large = elapsed(10), elapsed(3000)
    # 全体を書き戻す実装は 3,000 件で数秒かかる（#1939 の計測）。行だけなら件数に依らない。
    assert large < 0.5
    assert large < small * 20 + 0.05


def test_writes_without_conflict_do_not_merge(caplog: pytest.LogCaptureFixture) -> None:
    """1 つのプロセスだけが書くときは、版を進めるだけで別のプロセスの行を読まない。"""
    table = _table(_run("run_a"), _run("run_b"))
    only = _process(table)
    table.calls.clear()

    with caplog.at_level(logging.INFO):
        only.cancel_run("run_a")
        only.cancel_run("run_b")

    assert table.version() == 2
    assert table.writer() == WRITER_ID
    assert "entity_select" not in table.calls
    assert _merge_logs(caplog) == []


# ---- 2 つのプロセス（#1916） ------------------------------------------------------------


def test_second_process_does_not_erase_the_first_process_updates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """古いメモリのプロセスが書いても、別のプロセスが先に書いた Run の更新は残る。"""
    table = _table(_run("run_a"), _run("run_b"))
    first = _process(table)
    second = _process(table)

    first.cancel_run("run_a")
    with caplog.at_level(logging.WARNING):
        second.cancel_run("run_b")

    assert _stored_status(table) == {
        "run_a": RunStatus.CANCELLED,
        "run_b": RunStatus.CANCELLED,
    }
    assert table.version() == 2
    # 書いたプロセスのメモリも、保存先の更新を取り込む（画面に古い状態を出し続けない）。
    assert second.get_run("run_a").status == RunStatus.CANCELLED
    [record] = _merge_logs(caplog)
    assert record.levelno == logging.WARNING
    extra: Any = record
    assert extra.other_writer_id == WRITER_ID
    assert extra.synced_version == 0
    assert extra.current_version == 1
    assert extra.runs_from_storage == 1
    assert extra.overlapping == []
    assert "2 つ目の Agent の backend" in extra.reason

    # 先に書いたプロセスも、次の保存で相手の更新を消さず、取り込む。
    first.create_agent(AgentProfile(id="agent_first", name="一つ目"))
    assert _stored_status(table)["run_b"] == RunStatus.CANCELLED
    assert first.get_run("run_b").status == RunStatus.CANCELLED
    assert table.version() == 3


def test_same_run_changed_by_both_keeps_the_later_writer_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """両方が同じ Run を変えたときだけ、後から書いたプロセスの値にする（ログに残す）。"""
    table = _table(_run("run_a"), _run("run_b"))
    first = _process(table)
    second = _process(table)

    first.cancel_run("run_a")
    second.cancel_run("run_a")

    assert _stored_status(table)["run_a"] == RunStatus.CANCELLED
    assert _stored_status(table)["run_b"] == RunStatus.RUNNING
    [record] = _merge_logs(caplog)
    extra: Any = record
    assert extra.overlapping == ["run:run_a"]


def test_agent_deleted_by_another_process_is_not_restored() -> None:
    """別のプロセスが消した業務 Agent を、古いメモリのプロセスが書き戻さない。"""
    custom = AgentProfile(id="agent_custom", name="経費の確認")
    table = _table(_run("run_a"), agents=[custom])
    first = _process(table)
    second = _process(table)

    first.delete_agent("agent_custom")
    second.cancel_run("run_a")

    assert "agent_custom" not in {agent.id for agent in table.snapshot().agents}
    assert "agent_custom" not in {agent.id for agent in second.list_agents()}
    assert _stored_status(table)["run_a"] == RunStatus.CANCELLED
    # 削除は行を消さずに印を残す（別のプロセスが削除を知る）。
    assert table.entities[(KEY, "agent", "agent_custom")].text is None


def test_first_writes_to_an_empty_table_do_not_overwrite_each_other() -> None:
    """行が無い保存先に 2 つのプロセスが続けて書いても、後の方は前の方の行を上書きしない。"""
    table = CheckpointTable()
    first = _process(table)
    second = _process(table)

    first.create_agent(AgentProfile(id="agent_first", name="一つ目"))
    second.create_agent(AgentProfile(id="agent_second", name="二つ目"))

    stored_agents = {agent.id for agent in table.snapshot().agents}
    assert {"agent_first", "agent_second"} <= stored_agents
    assert "agent_first" in {agent.id for agent in second.list_agents()}
    assert table.version() == 2


def test_unreadable_row_written_by_another_process_is_quarantined() -> None:
    """別のプロセスが書いた行が読めなければ、メモリに入れずに退避する（元の文字列は残す）。"""
    table = _table(_run("run_a"), _run("run_b"))
    process = _process(table)
    table.entities[(KEY, "run", "run_b")].text = "{broken"
    table.entities[(KEY, "run", "run_b")].change_version = 5
    table.heads[KEY].version = 5

    process.cancel_run("run_a")
    # 退避の記録と行の削除は、次の保存で書く。
    process.create_agent(AgentProfile(id="agent_x", name="x"))

    assert "run_b" not in {run.id for run in process.list_runs()}
    quarantined = table.header()["quarantined"]
    assert [item["id"] for item in quarantined] == ["run_b"]
    assert quarantined[0]["raw"] == "{broken"
    assert table.entities[(KEY, "run", "run_b")].text is None


def test_unreadable_head_is_not_overwritten() -> None:
    """head が読めなければ、上書きせずに止める。"""
    table = _table(_run("run_a"))
    process = _process(table)
    table.heads[KEY].text = "{broken"
    table.heads[KEY].version = 7

    with pytest.raises(AgentRuntimeCheckpointCorruptError, match="上書きせずに止めました"):
        process.cancel_run("run_a")

    assert table.heads[KEY].text == "{broken"


# ---- 移す前の形（migration 016 の前） --------------------------------------------------------


def test_legacy_snapshot_requires_the_migration() -> None:
    """全体の snapshot の 1 行のままなら、システムテーブルの更新へ案内する（読み書きしない）。"""
    table = CheckpointTable()
    legacy = AgentRuntimeSnapshot(runs=[_run("run_a")]).model_dump_json()
    table.set_legacy(legacy)

    with pytest.raises(AgentRuntimeMigrationRequiredError, match="20261015_016"):
        _process(table)

    assert table.heads[KEY].text == legacy
    assert table.entities == {}


@pytest.mark.parametrize(
    ("versioned", "entities_table"),
    [(False, True), (True, False)],
    ids=["no-version-column", "no-entities-table"],
)
def test_tables_before_the_migration_require_it(versioned: bool, entities_table: bool) -> None:
    table = _table(_run("run_a"))
    table.versioned = versioned
    table.entities_table = entities_table

    with pytest.raises(AgentRuntimeMigrationRequiredError):
        _process(table)


# ---- 書き込みは lock の外（#1939） ----------------------------------------------------------


class _SlowTable(CheckpointTable):
    """書き込み（head の lock）を止められる保存先（遅い保存の再現）。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.writing = threading.Event()
        self.fail_next = False

    def connect(self) -> CheckpointConnection:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("ORA-03113: end-of-file on communication channel")
        return super().connect()

    def execute(self, statement: str, params: dict[str, Any]) -> Any:
        if "FOR UPDATE" in statement.upper():
            self.writing.set()
            assert self.gate.wait(timeout=10)
        return super().execute(statement, params)


def test_slow_write_does_not_block_reads() -> None:
    """保存の書き込みの間も、一覧・回答の stream（メモリの読み取り）は待たない。"""
    table = _SlowTable()
    table.seed([_run("run_t", RunStatus.COMPLETED, thread_id="thread_x", user="u1")])
    repository = _process(table)
    table.gate.clear()

    writer = threading.Thread(
        target=repository.delete_thread, args=("thread_x",), kwargs={"user_uuid": "u1"}
    )
    writer.start()
    try:
        assert table.writing.wait(timeout=5)
        started = time.perf_counter()
        summaries = repository.list_run_summaries()
        events = list(repository.iter_events("run_t"))
        assert time.perf_counter() - started < 1
        assert [item.id for item in summaries] == ["run_t"]
        assert events == []
        # メモリは書き込みを待たずに変わっている。
        assert repository.get_run("run_t").thread_id is None
    finally:
        table.gate.set()
        writer.join(timeout=10)
    assert table.snapshot().runs[0].thread_id is None


def test_changes_on_the_event_loop_are_written_in_the_background(
    monkeypatch: MonkeyPatch,
) -> None:
    """event loop の上の変更（組み込み Runtime の記録）は、書き込みを待たずに戻る。"""
    monkeypatch.setattr(runtime, "BACKGROUND_WRITES", True)
    table = _SlowTable()
    table.seed([_run("run_a"), _run("run_b")])
    repository = _process(table)
    table.gate.clear()

    async def change_on_loop() -> float:
        started = time.perf_counter()
        repository.cancel_run("run_a")
        return time.perf_counter() - started

    try:
        assert asyncio.run(change_on_loop()) < 1
        assert table.writing.wait(timeout=5)
        assert _stored_status(table)["run_a"] == RunStatus.RUNNING
    finally:
        table.gate.set()
    repository.flush_writes()
    assert _stored_status(table)["run_a"] == RunStatus.CANCELLED
    assert repository.pending_writes() == 0


def test_failed_write_is_kept_and_written_by_the_next_save() -> None:
    """書けなかった保存は予定に残り、次の書き込みで後の変更と重ねて書く。"""
    table = _SlowTable()
    table.seed([_run("run_a"), _run("run_b")])
    repository = _process(table)

    table.fail_next = True
    with pytest.raises(RuntimeError, match="ORA-03113"):
        repository.cancel_run("run_a")
    assert repository.get_run("run_a").status == RunStatus.CANCELLED
    assert repository.pending_writes() == 1

    repository.cancel_run("run_b")

    assert _stored_status(table) == {"run_a": RunStatus.CANCELLED, "run_b": RunStatus.CANCELLED}
    assert repository.pending_writes() == 0
    assert table.version() == 1
