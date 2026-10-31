"""NL2SQL 非同期 job の複数 worker / 再起動耐性テスト。

gunicorn の複数 worker では、job を実行するプロセスと `GET /jobs/{id}` /
`POST /jobs/{id}/cancel` を受けるプロセスが異なる。1 つの repository を共有する
2 つの service を「別 worker」に見立てて、状態収束・キャンセル伝搬・孤児 job の
正規化を検証する。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    FeedbackRating,
    JobCreateRequest,
    JobStatus,
    JobStepStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError
from app.features.nl2sql.service import (
    _NL2SQL_JOB_STAGES,
    JOB_CANCELLED_ERROR_CODE,
    JOB_FAILED_ERROR_CODE,
    PROFILE_REVALIDATION_REQUIRED_ERROR_CODE,
    SQL_BLOCKED_ERROR_CODE,
    SQL_EXECUTION_FAILED_ERROR_CODE,
    HistoryNotRatableError,
    Nl2SqlService,
    StoredJob,
    _new_job_steps,
)
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings

_PROFILE_ID = "orders-profile"
_GENERATED = '{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID を取得します。"}'


class _FakeEnterpriseAiClient:
    def __init__(self, text: str) -> None:
        self.text = text

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "enterprise-nl2sql-model"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: object | None = None,
    ) -> str:
        del prompt, context, system_prompt, timeout_seconds, max_output_tokens, max_retries
        return self.text


class _BlockingEnterpriseAiClient(_FakeEnterpriseAiClient):
    """generate_sql 段階で止まり、テスト側が release するまで戻らない。"""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: object | None = None,
    ) -> str:
        self.entered.set()
        del timeout_seconds, max_output_tokens, max_retries
        assert self.release.wait(timeout=10), "テストが release しないまま timeout"
        return super().generate(prompt=prompt, context=context, system_prompt=system_prompt)


def _repository() -> MemoryIncrementalNl2SqlRepository:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(
        refreshed_at="2026-09-01T00:00:00+00:00",
        schema_fingerprint="job-runtime-v1",
        current_owner="APP",
        tables=[
            SchemaTable(
                owner="APP",
                table_name="ORDERS",
                logical_name="注文",
                columns=[
                    SchemaColumn(
                        column_name="ID", logical_name="ID", data_type="NUMBER", nullable=False
                    ),
                    SchemaColumn(
                        column_name="ORDER_NAME", logical_name="注文名", data_type="VARCHAR2"
                    ),
                    SchemaColumn(column_name="AMOUNT", logical_name="金額", data_type="NUMBER"),
                    SchemaColumn(column_name="CREATED_AT", logical_name="作成日", data_type="DATE"),
                ],
            )
        ],
    )
    manifest = {
        (table.owner.upper(), table.table_name.upper()): catalog.refreshed_at
        for table in catalog.tables
    }
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )
    repository.save_profile(
        Nl2SqlProfile(id=_PROFILE_ID, name="注文管理", allowed_tables=["APP.ORDERS"]),
        expected_etag=None,
    )
    return repository


def _worker(
    repository: MemoryIncrementalNl2SqlRepository,
    client: _FakeEnterpriseAiClient | None = None,
) -> Nl2SqlService:
    """1 つの repository を共有する「別 worker プロセス」相当の service。"""

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    service._catalog = repository.load_catalog()  # noqa: SLF001
    service._enterprise_ai_client = client or _FakeEnterpriseAiClient(_GENERATED)  # noqa: SLF001
    return service


def _request() -> JobCreateRequest:
    return JobCreateRequest(
        question="注文一覧を確認したい",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        profile_id=_PROFILE_ID,
    )


def _wait_terminal(service: Nl2SqlService, job_id: str) -> Any:
    job = None
    for _ in range(200):
        job = service.get_job(job_id)
        if job is not None and job.status in {JobStatus.DONE, JobStatus.ERROR}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job が terminal に達しない: {job}")


def test_observer_worker_rereads_in_flight_job_until_owner_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)
    observer = _worker(repository)
    # owner 側の実行を止め、pending のまま永続化された状態を作る。
    monkeypatch.setattr(owner, "_run_job_safely", lambda _job_id: None)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)

    first = observer.get_job(created.job_id)
    assert first is not None
    assert first.status == JobStatus.PENDING

    assert owner._claim_nl2sql_job(worker_id="owner", job_id=created.job_id) is not None
    with owner._lock:  # noqa: SLF001
        stored = owner._jobs[created.job_id]  # noqa: SLF001
        stored.status = JobStatus.DONE
        stored.finished_at = datetime.now(UTC).isoformat()
    owner._persist_job(created.job_id)  # noqa: SLF001

    # 旧実装は observer 側 cache を再読込せず、永遠に pending を返していた。
    second = observer.get_job(created.job_id)
    assert second is not None
    assert second.status == JobStatus.DONE


def test_owner_worker_returns_persisted_state_after_execution_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)
    monkeypatch.setattr(owner, "_run_job_safely", lambda _job_id: None)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    assert owner.run_next_nl2sql_job(job_id=created.job_id, worker_id="worker-owner") is True

    # 別 worker の終端状態を、実行元の process も保存済みの正本から返す。
    document = repository.get_document("jobs", created.job_id)
    assert document is not None
    repository.put_document("jobs", created.job_id, {**document, "status": "error"})

    job = owner.get_job(created.job_id)
    assert job is not None
    assert job.status == JobStatus.ERROR


def test_external_worker_mode_enqueues_without_inprocess_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)
    dispatched = False

    def dispatch_should_not_run(*, settings: Any | None = None) -> bool:
        nonlocal dispatched
        del settings
        dispatched = True
        return False

    monkeypatch.setattr(owner, "_dispatch_nl2sql_queue_worker", dispatch_should_not_run)

    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    fetched = owner.get_job(created.job_id)
    document = repository.get_document("jobs", created.job_id)

    assert dispatched is False
    assert fetched is not None
    assert fetched.status == JobStatus.PENDING
    assert document is not None
    assert document["status"] == "pending"
    assert document["worker_id"] == ""


def test_run_next_nl2sql_job_claims_pending_document_with_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)
    monkeypatch.setattr(owner, "_run_job_safely", lambda _job_id: None)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)

    assert owner.run_next_nl2sql_job(job_id=created.job_id, worker_id="worker-claim") is True

    document = repository.get_document("jobs", created.job_id)
    assert document is not None
    assert document["status"] == "running"
    assert document["worker_id"] == "worker-claim"
    assert document["heartbeat_at"]
    assert document["lease_expires_at"]
    assert document["attempt"] == 1
    with owner._lock:  # noqa: SLF001
        local = owner._jobs[created.job_id]  # noqa: SLF001
    assert local.owned is True
    assert local.worker_id == "worker-claim"


def test_cancel_from_other_worker_reaches_owner_at_stage_boundary() -> None:
    repository = _repository()
    blocking = _BlockingEnterpriseAiClient(_GENERATED)
    owner = _worker(repository, blocking)
    observer = _worker(repository)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    assert blocking.entered.wait(timeout=10)

    # 旧実装: observer は job を知らず None(404)、知っていても別プロセスの複製に
    # フラグを立てるだけで owner は止まらなかった。
    accepted = observer.request_job_cancel(created.job_id)
    assert accepted is not None
    assert accepted.status == JobStatus.RUNNING
    assert repository.get_document("job_cancel_requests", created.job_id) is not None

    blocking.release.set()
    finished = _wait_terminal(owner, created.job_id)
    assert finished.status == JobStatus.ERROR
    assert finished.error_code == JOB_CANCELLED_ERROR_CODE
    assert "キャンセル" in (finished.error_message or "")

    # error_code も snapshot 経由で他 worker に見える。
    seen = _wait_terminal(observer, created.job_id)
    assert seen is not None
    assert seen.status == JobStatus.ERROR
    assert seen.error_code == JOB_CANCELLED_ERROR_CODE


def test_cancel_job_records_first_requester_and_reports_repeated_requests() -> None:
    """MCP の取消（#3318）: 要求のドキュメントに操作した人と入口を残し、2 回目は上書きしない。

    2 回目の要求は別のプロセス（job の文書を読み直した手元の job は印を持たない）でも、
    要求のドキュメントから「要求済み」と分かる。
    """
    repository = _repository()
    blocking = _BlockingEnterpriseAiClient(_GENERATED)
    owner = _worker(repository, blocking)
    observer = _worker(repository)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    assert blocking.entered.wait(timeout=10)

    first = observer.cancel_job(
        created.job_id,
        actor_user_uuid="user-1",
        via={"via": "mcp", "agent_run_id": "run-1", "ignored": "x"},
    )
    assert first is not None
    assert (first.job.status, first.already_requested) == (JobStatus.RUNNING, False)
    document = repository.get_document("job_cancel_requests", created.job_id)
    assert document is not None
    assert (document["actor_user_uuid"], document["via"], document["agent_run_id"]) == (
        "user-1",
        "mcp",
        "run-1",
    )
    assert "ignored" not in document

    second = _worker(repository).cancel_job(
        created.job_id, actor_user_uuid="admin-1", actor_can_manage=True, via={"via": "mcp"}
    )
    assert second is not None
    assert second.already_requested is True
    kept = repository.get_document("job_cancel_requests", created.job_id)
    assert kept is not None
    assert kept["actor_user_uuid"] == "user-1"

    blocking.release.set()
    finished = _wait_terminal(owner, created.job_id)
    assert (finished.status, finished.error_code) == (JobStatus.ERROR, JOB_CANCELLED_ERROR_CODE)

    # 停止も実行履歴に行を残し、別のプロセスが受けた最初の要求の記録（取り消した人と入口）を写す
    # （#3348）。SQL は持たない。
    [row] = owner.list_history(status="cancelled").items
    assert (row.job_id, row.run_status, row.question, row.generated_sql) == (
        created.job_id,
        "cancelled",
        _request().question,
        "",
    )
    assert (
        row.cancelled_by_user_uuid,
        row.cancel_via,
        row.cancel_agent_run_id,
        row.cancel_api_key_id,
    ) == ("user-1", "mcp", "run-1", "")
    assert row.actor_user_uuid == "user-1"
    assert repository.get_document("history", row.id) is not None
    # 完了・ブロック（安全性の判定）とフィードバックの一覧には出さない。
    assert owner.list_history(status="succeeded").items == []
    assert owner.list_history(safety="safe").items == []
    assert [item.id for item in owner.list_history().items] == [row.id]
    assert owner.list_feedback(limit=25, rating="all", profile_id="", query="").items == []
    with pytest.raises(HistoryNotRatableError):
        owner.save_feedback(row.id, FeedbackRating.GOOD, actor_user_uuid="user-1")


def test_cancelled_job_history_records_the_screen_cancel_of_the_same_process() -> None:
    """取消の要求を受けたプロセスで終わった停止は、手元の記録から画面の取消を写す（#3348）。"""
    blocking = _BlockingEnterpriseAiClient(_GENERATED)
    service = _worker(_repository(), blocking)
    created = service.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    assert blocking.entered.wait(timeout=10)
    service.cancel_job(created.job_id, actor_user_uuid="admin-1", actor_can_manage=True)
    blocking.release.set()
    _wait_terminal(service, created.job_id)

    [row] = service.list_history(status="cancelled").items
    assert (row.actor_user_uuid, row.cancelled_by_user_uuid, row.cancel_via) == (
        "user-1",
        "admin-1",
        "",
    )
    assert row.elapsed_ms is not None and row.elapsed_ms >= 0


def test_cancel_of_unknown_job_returns_none() -> None:
    service = _worker(_repository())

    assert service.request_job_cancel("missing-job") is None


def test_cancel_of_terminal_job_is_noop_without_cancel_document() -> None:
    repository = _repository()
    owner = _worker(repository)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    finished = _wait_terminal(owner, created.job_id)
    assert finished.status == JobStatus.DONE

    result = owner.request_job_cancel(created.job_id)
    assert result is not None
    assert result.status == JobStatus.DONE
    assert repository.get_document("job_cancel_requests", created.job_id) is None


def _put_in_flight_snapshot(
    service: Nl2SqlService,
    repository: MemoryIncrementalNl2SqlRepository,
    *,
    job_id: str,
    updated_at: str,
    lease_expires_at: str | None = None,
) -> None:
    steps = _new_job_steps()
    steps[0] = steps[0].model_copy(update={"status": JobStepStatus.DONE, "elapsed_ms": 5})
    steps[1] = steps[1].model_copy(update={"status": JobStepStatus.RUNNING})
    stored = StoredJob(
        job_id=job_id,
        request=_request(),
        actor_user_uuid="user-1",
        actor_is_system_admin=True,
        status=JobStatus.RUNNING,
        created_at=updated_at,
        started_at=updated_at,
        steps=steps,
        worker_id="worker-old",
        heartbeat_at=updated_at,
        lease_expires_at=lease_expires_at,
        attempt=1,
    )
    snapshot = {**service._job_to_snapshot(stored), "updated_at": updated_at}  # noqa: SLF001
    repository.put_document("jobs", job_id, snapshot, status="running")


def test_expired_running_snapshot_is_reclaimed_and_completed() -> None:
    repository = _repository()
    service = _worker(repository)
    two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _put_in_flight_snapshot(service, repository, job_id="job-orphan", updated_at=two_hours_ago)

    assert service.run_next_nl2sql_job(worker_id="worker-retry") is True
    job = service.get_job("job-orphan")

    assert job is not None
    assert job.status == JobStatus.DONE
    assert not job.error_code
    assert job.finished_at
    assert all(step.status == JobStepStatus.DONE for step in job.steps)
    persisted = repository.get_document("jobs", "job-orphan")
    assert persisted is not None
    assert persisted["status"] == "done"
    assert persisted["worker_id"] == ""
    assert persisted["attempt"] == 2


def test_result_snapshot_restores_terminal_status_and_step_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)

    assert owner.run_next_nl2sql_job(job_id=created.job_id, worker_id="worker-owner") is True
    finished = owner.get_job(created.job_id)
    assert finished is not None
    assert finished.status == JobStatus.DONE

    snapshot = repository.get_document("jobs", created.job_id)
    assert snapshot is not None
    inconsistent_steps = [
        {
            **step,
            **(
                {"status": JobStepStatus.RUNNING.value, "elapsed_ms": None}
                if step["stage"] == "execute_sql"
                else {}
            ),
        }
        for step in snapshot["steps"]
    ]
    repository.put_document(
        "jobs",
        created.job_id,
        {
            **snapshot,
            "status": JobStatus.RUNNING.value,
            "steps": inconsistent_steps,
            "worker_id": "",
            "lease_expires_at": None,
        },
        status="running",
    )

    observer = _worker(repository)
    restored = observer.get_job(created.job_id)

    assert restored is not None
    assert restored.status == JobStatus.DONE
    assert [step.stage for step in restored.steps] == list(_NL2SQL_JOB_STAGES)
    assert [step.status for step in restored.steps] == [JobStepStatus.DONE] * 5


def test_fresh_in_flight_snapshot_from_other_worker_stays_running() -> None:
    repository = _repository()
    service = _worker(repository)
    now = datetime.now(UTC).isoformat()
    lease_expires_at = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    _put_in_flight_snapshot(
        service,
        repository,
        job_id="job-live",
        updated_at=now,
        lease_expires_at=lease_expires_at,
    )

    job = service.get_job("job-live")

    assert job is not None
    assert job.status == JobStatus.RUNNING
    persisted = repository.get_document("jobs", "job-live")
    assert persisted is not None
    assert persisted["status"] == "running"
    assert persisted["lease_expires_at"] == lease_expires_at


def test_get_job_wakes_expired_running_job_inprocess() -> None:
    repository = _repository()
    service = _worker(repository)
    three_minutes_ago = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
    expired_lease = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    _put_in_flight_snapshot(
        service,
        repository,
        job_id="job-expired-lease",
        updated_at=three_minutes_ago,
        lease_expires_at=expired_lease,
    )

    job = _wait_terminal(service, "job-expired-lease")

    assert job is not None
    assert job.status == JobStatus.DONE
    assert job.error_code is None


def test_run_job_safely_tolerates_missing_job_record() -> None:
    service = _worker(_repository())

    # 旧実装は self._jobs[job_id] の KeyError で worker スレッドごと落ちていた。
    service._run_job_safely("missing-job")  # noqa: SLF001


def test_execute_sql_oracle_error_keeps_generated_sql_result_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 生成 SQL の実行時エラー（ORA-00904 等）でも生成 SQL・safety・履歴を失わない。
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)

    def failing_execute(*_args: object, **_kwargs: object) -> None:
        raise OracleAdapterError('ORA-00904: "ORDER_NAME2": invalid identifier')

    monkeypatch.setattr(owner, "execute_sql", failing_execute)
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)

    assert owner.run_next_nl2sql_job(job_id=created.job_id, worker_id="worker-owner") is True
    job = owner.get_job(created.job_id)

    assert job is not None
    assert job.status == JobStatus.ERROR
    # 1 文目は利用者の言葉にし、Oracle のエラーの元の文は詳細に分ける（messaging.md §10.3。#1072）。
    assert job.error_message is not None and job.error_message.startswith(
        "生成した SQL の実行に失敗しました。"
    )
    assert "ORA-" not in job.error_message
    assert job.error_detail == 'ORA-00904: "ORDER_NAME2": invalid identifier'
    assert job.result is not None
    assert job.result.generated_sql == "SELECT ID FROM APP.ORDERS"
    assert job.result.safety.is_safe is True
    assert job.result.results.total == 0
    steps = {step.stage: step.status for step in job.steps}
    assert steps["safety_check"] == JobStepStatus.DONE
    assert steps["execute_sql"] == JobStepStatus.ERROR
    assert steps["format_results"] == JobStepStatus.DONE
    history = owner._history_by_id(job.result.history_id)  # noqa: SLF001
    assert history is not None
    assert history.generated_sql == "SELECT ID FROM APP.ORDERS"
    assert history.safety_is_safe is True
    # 失敗の分類は Oracle のコード（#847）。永続化した snapshot にも残る。
    assert job.error_code == "ORA-00904"
    persisted = repository.get_document("jobs", created.job_id)
    assert persisted is not None
    assert persisted["error_code"] == "ORA-00904"
    assert persisted["error_detail"] == 'ORA-00904: "ORDER_NAME2": invalid identifier'


def _run_owned_job(monkeypatch: pytest.MonkeyPatch, owner: Nl2SqlService) -> Any:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    created = owner.start_job(_request(), actor_user_uuid="user-1", actor_is_system_admin=True)
    assert owner.run_next_nl2sql_job(job_id=created.job_id, worker_id="worker-owner") is True
    job = owner.get_job(created.job_id)
    assert job is not None
    return job


def test_execute_sql_error_without_oracle_code_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _worker(_repository())

    def failing_execute(*_args: object, **_kwargs: object) -> None:
        raise OracleAdapterError("SQL の実行が打ち切られました。")

    monkeypatch.setattr(owner, "execute_sql", failing_execute)
    job = _run_owned_job(monkeypatch, owner)

    assert job.status == JobStatus.ERROR
    assert job.error_code == SQL_EXECUTION_FAILED_ERROR_CODE


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            OracleAdapterError(
                "Select AI の生成に失敗しました: ORA-04027: self-deadlock during automatic "
                "validation for object DBMS_CLOUD_AI"
            ),
            "ORA-04027",
        ),
        (RuntimeError("想定外の失敗"), JOB_FAILED_ERROR_CODE),
    ],
    ids=["oracle-code", "no-code"],
)
def test_failed_job_always_has_error_code(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    """例外で失敗したジョブも error_code を null にしない（#847。MCP の呼び出し側が分類する）。"""
    owner = _worker(_repository())

    def failing_generate(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(owner, "_generate_selected_engine", failing_generate)
    job = _run_owned_job(monkeypatch, owner)

    assert job.status == JobStatus.ERROR
    assert job.error_code == expected
    # 例外の文（英語・ORA-…）は 1 文目に出さず、詳細に分ける（#1072）。
    assert job.error_message is not None and job.error_message.startswith(
        "SQL の生成に失敗しました。"
    )
    assert str(error) not in job.error_message
    assert job.error_detail == str(error)
    steps = {step.stage: step.status for step in job.steps}
    assert steps["generate_sql"] == JobStepStatus.ERROR


def test_long_failure_detail_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _worker(_repository())

    def failing_generate(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(owner, "_generate_selected_engine", failing_generate)
    job = _run_owned_job(monkeypatch, owner)

    assert job.error_detail == "x" * 4000 + "…"


def test_blocked_sql_job_has_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _worker(
        _repository(),
        _FakeEnterpriseAiClient('{"sql":"DELETE FROM APP.ORDERS","explanation":"削除します。"}'),
    )
    job = _run_owned_job(monkeypatch, owner)

    assert job.status == JobStatus.ERROR
    assert job.result is not None
    assert job.result.safety.is_safe is False
    assert job.error_code == SQL_BLOCKED_ERROR_CODE


def _scope_changed_error() -> Exception:
    from app.features.nl2sql.ontology_service import (
        BUSINESS_SCOPE_CHANGED_CODE,
        BUSINESS_SCOPE_CHANGED_MESSAGE_JA,
        OntologyGateBlockedError,
    )

    return OntologyGateBlockedError(BUSINESS_SCOPE_CHANGED_CODE, BUSINESS_SCOPE_CHANGED_MESSAGE_JA)


def _wrapped_scope_changed_error() -> Exception:
    try:
        raise _scope_changed_error()
    except Exception as exc:
        try:
            raise RuntimeError("業務定義の準備に失敗しました。") from exc
        except RuntimeError as wrapped:
            return wrapped


@pytest.mark.parametrize(
    "make_error",
    [_scope_changed_error, _wrapped_scope_changed_error],
    ids=["direct", "cause-chain"],
)
def test_business_scope_changed_does_not_suggest_retry(
    monkeypatch: pytest.MonkeyPatch, make_error: Any
) -> None:
    """公開の後に業務プロファイル・Schema が変わった業務定義（BUSINESS_SCOPE_CHANGED）は、
    「時間をおいて」と案内せず、再検証の要る失敗と分かる error_code にする（#1827）。"""
    from app.features.mcp.tools import _job_result

    owner = _worker(_repository())

    def stale_ontology(*_args: object, **_kwargs: object) -> None:
        raise make_error()

    monkeypatch.setattr(owner, "_job_published_ontology_markdown", stale_ontology)
    job = _run_owned_job(monkeypatch, owner)

    assert job.status == JobStatus.ERROR
    assert job.error_code == PROFILE_REVALIDATION_REQUIRED_ERROR_CODE
    message = job.error_message or ""
    assert "時間をおいてもう一度" not in message
    assert "時間をおいても直りません" in message
    assert message.startswith("この業務プロファイルの業務定義（公開版のオントロジー）は、")
    assert "「オントロジー構築」" in message
    # ゲートの文とコードは「詳細」に残す（管理者が原因を確かめる）。
    assert job.error_detail is not None
    assert "BUSINESS_SCOPE_CHANGED" in job.error_detail
    steps = {step.stage: step.status for step in job.steps}
    assert steps["prepare_context"] == JobStepStatus.ERROR

    # MCP（nl2sql_query / nl2sql_get_job）も同じコードと、再試行を案内しない文を返す。
    output = _job_result(job)
    assert output.error_code == PROFILE_REVALIDATION_REQUIRED_ERROR_CODE
    assert output.error_message is not None
    assert output.error_message.startswith(message)
    assert "時間をおいてもう一度" not in output.error_message


class _FailingEnterpriseAiClient(_FakeEnterpriseAiClient):
    """OCI Enterprise AI の HTTP の失敗を返す client（#1684）。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__("")
        self.status = status
        self.body = body
        self.calls = 0

    def model_id(self) -> str:
        return "openai.gpt-oss-120b"

    def generate(self, **_kwargs: Any) -> str:
        from pr_system_settings.enterprise_ai_errors import classify_enterprise_ai_http_failure

        from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError

        self.calls += 1
        failure = classify_enterprise_ai_http_failure(self.status, self.body)
        raise EnterpriseAiDirectError(
            f"OCI Enterprise AI HTTP {self.status}: {self.body}",
            code=failure.kind,
            retryable=failure.retryable,
            http_failure=failure,
            model_id=self.model_id(),
            endpoint_host="inference.generativeai.us-chicago-1.oci.oraclecloud.com",
        )


_OCI_404_BODY = (
    '{ "code": "404", "message": "Authorization failed or requested resource not found." }'
)


def test_enterprise_ai_authorization_failure_explains_settings_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enterprise AI Direct の 404（認可の失敗）は「時間をおいて」と案内しない（#1684）。"""
    client = _FailingEnterpriseAiClient(404, _OCI_404_BODY)
    owner = _worker(_repository(), client)
    # 本番（oracle runtime）と同じく、生成の失敗をテンプレート SQL で埋めない。
    monkeypatch.setattr(owner, "_use_oracle_runtime", lambda: True)
    job = _run_owned_job(monkeypatch, owner)

    assert job.status == JobStatus.ERROR
    assert client.calls == 1
    assert job.error_code == "ENTERPRISE_AI_AUTHORIZATION_FAILED"
    message = job.error_message or ""
    assert "時間をおいてもう一度" not in message
    assert "時間をおいても直りません" in message
    assert "openai.gpt-oss-120b" in message
    assert "inference.generativeai.us-chicago-1.oci.oraclecloud.com" in message
    assert "HTTP 404" in message
    assert "IAM" in message
    assert "既定のテキストモデル" in message
    assert "業務プロファイルの Region・LLM Model は Select AI・Select AI Agent 用" in message
    assert job.error_detail is not None and "Authorization failed" in job.error_detail
    steps = {step.stage: step.status for step in job.steps}
    assert steps["generate_sql"] == JobStepStatus.ERROR


def test_enterprise_ai_temporary_failure_keeps_retry_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一時的な失敗（5xx）は今までどおり「時間をおいて」と案内する。"""
    owner = _worker(_repository(), _FailingEnterpriseAiClient(503, "busy"))
    monkeypatch.setattr(owner, "_use_oracle_runtime", lambda: True)
    job = _run_owned_job(monkeypatch, owner)

    assert job.status == JobStatus.ERROR
    assert job.error_code == "ENTERPRISE_AI_UNAVAILABLE"
    assert job.error_message is not None and job.error_message.startswith(
        "SQL の生成に失敗しました。時間をおいてもう一度実行してください。"
    )


def test_worker_trace_survives_snapshot_and_does_not_invent_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker の span は共通の span の表に入り、画面の要求の trace を親にしない（#3226 の案 A）。"""

    from pr_backend_core.config.tracing import TracingSettings
    from pr_backend_core.observability.tracing import (
        configure_tracing,
        observed_span,
        shutdown_tracing,
    )
    from trace_span_fake import FakeTraceSpanDb

    from app.features.nl2sql.trace_spans import install_trace_span_store, reset_trace_span_store

    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    owner = _worker(repository)
    configure_tracing(TracingSettings(), service_name="synthetic")
    db = FakeTraceSpanDb()
    store = install_trace_span_store(db.connect, start=False)
    try:
        with observed_span("http.request", kind="server") as http:
            request_trace_id = f"{http.span.get_span_context().trace_id:032x}"
            created = owner.start_job(
                _request().model_copy(update={"generation_only": True}),
                actor_user_uuid="user-1",
                actor_is_system_admin=True,
            )
        assert owner.run_next_nl2sql_job(job_id=created.job_id, worker_id="synthetic")
        store.writer.flush()
        observer = _worker(repository)
        job = observer.get_job(created.job_id, actor_user_uuid="user-1")
        assert job is not None and job.status == JobStatus.DONE and job.trace_id
        rows = db.spans(trace_id=job.trace_id)
        names = [row["name"] for row in rows]
        assert "nl2sql.generate_sql" in names
        # 実行しない SQL の実行の段は、未実行（所要時間 0）の span で残す（#3226）。
        [execute] = [row for row in rows if row["name"] == "nl2sql.execute_sql"]
        assert execute["status"] == "skipped" and execute["started_at"] == execute["finished_at"]
        # 画面の要求（ブラウザの traceparent）は親にせず、新しい trace にする（span link だけ）。
        [root] = [row for row in rows if row["is_root"]]
        assert root["parent_span_id"] is None and job.trace_id != request_trace_id
        assert all(row["parent_span_id"] == root["span_id"] for row in rows if row is not root)
        with pytest.raises(PermissionError):
            observer.get_job(created.job_id, actor_user_uuid="user-2")
    finally:
        reset_trace_span_store()
        shutdown_tracing()
