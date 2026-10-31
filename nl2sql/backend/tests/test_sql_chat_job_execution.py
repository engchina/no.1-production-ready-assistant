"""チャットのターンも SQL 生成の画面と同じく、生成した SQL を同じジョブで実行する（#1176）。

- 実行は SQL 生成のジョブの実行の段階と同じ経路（安全検査・`execute_sql`・actor の明示・実行履歴）。
- 上限はチャットの上限（#1154）。結果の行は会話（ジョブの文書）に残さず、画面が 1 回だけ受け取る。
- 実行の権限が無い利用者は生成だけ。DML は安全検査で遮断し、実行しない（SQL 生成のジョブと同じ）。

接続の種類（system_admin / 非 system_admin・DeepSec）は `test_nl2sql_db_roundtrips.py` の
`test_business_sql_connection_follows_job_actor`（チャットのターンを含む）が確かめる。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from test_nl2sql_job_runtime import _repository, _request, _worker
from test_sql_chat import ChatFixture, RecordingClient

from app.features.nl2sql.models import (
    AllowedObjects,
    AnalyzeData,
    JobCreateRequest,
    JobData,
    JobStatus,
    JobStepStatus,
    QueryResults,
    SafetyReport,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError
from app.features.nl2sql.service import Nl2SqlService
from app.security.request_actor import current_actor_context
from app.settings import get_settings


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> ChatFixture:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    client = RecordingClient()
    return _worker(repository, client), repository, client


def chat_turn(question: str = "注文一覧", previous: str | None = None) -> JobCreateRequest:
    """今の画面が送るチャットのターン（生成と実行。#1176）。"""
    return _request().model_copy(
        update={"question": question, "chat": True, "previous_job_id": previous}
    )


def run(
    service: Nl2SqlService, req: JobCreateRequest, actor: str = "user-1", admin: bool = False
) -> JobData:
    created = service.start_job(req, actor_user_uuid=actor, actor_is_system_admin=admin)
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid=actor)
    assert job is not None
    return job


def _step(job: JobData, stage: str) -> JobStepStatus:
    return next(step.status for step in job.steps if step.stage == stage)


def test_chat_turn_executes_in_the_job_and_rows_are_received_once(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _ = chat
    calls: list[tuple[str, int | None, Any]] = []
    execute = service.execute_sql

    def record(
        sql: str,
        allowed: AllowedObjects,
        row_limit: int | None,
        *,
        analysis: AnalyzeData | None = None,
    ) -> tuple[SafetyReport, str, QueryResults]:
        calls.append((sql, row_limit, current_actor_context()))
        return execute(sql, allowed, row_limit, analysis=analysis)

    monkeypatch.setattr(service, "execute_sql", record)
    turn = run(service, chat_turn(), admin=True)

    # SQL 生成のジョブと同じく、生成・安全性の確認・実行・結果の整形まで同じジョブで行う。
    assert turn.status == JobStatus.DONE, turn.error_message
    assert turn.chat is True
    assert [step.status for step in turn.steps] == [JobStepStatus.DONE] * 5
    # 生成 SQL を、チャットの行数の上限で、ジョブの actor（system_admin か）で実行する（#904）。
    assert [(sql, limit) for sql, limit, _ in calls] == [
        ("SELECT ID FROM APP.ORDERS", get_settings().nl2sql_chat_result_max_rows)
    ]
    actor = calls[0][2]
    assert (actor.user_uuid, actor.is_system_admin) == ("user-1", True)

    # ターンには要約だけを残す（行は会話に残さない。#1154）。
    summary = turn.last_execution
    assert summary is not None
    assert summary.status == "done"
    assert summary.row_count > 0
    assert summary.result_expires_at
    assert turn.result is not None and turn.result.results.rows == []
    document = repository.get_document("jobs", turn.job_id)
    assert document is not None
    assert document["result"]["results"]["rows"] == []
    # 実行履歴（監査）は実行ありの 1 件（SQL 生成のジョブと同じ）。
    history = repository.get_document("history", summary.history_id)
    assert history is not None
    assert history["generation_only"] is False
    assert history["session_id"] == turn.conversation_id
    assert history["result_row_count"] == summary.row_count

    # 行は画面が 1 回だけ受け取る（別の worker からでも受け取れる）。受け取ったら消す。
    observer = _worker(repository)
    with pytest.raises(PermissionError):
        observer.take_chat_execution_result(turn.job_id, actor_user_uuid="other")
    received = observer.take_chat_execution_result(turn.job_id, actor_user_uuid="user-1")
    assert received.status == "done"
    assert received.results.total == len(received.results.rows) == summary.row_count
    assert received.history_id == summary.history_id
    assert received.row_limit == get_settings().nl2sql_chat_result_max_rows
    assert repository.get_document("chat_results", turn.job_id) is None
    with pytest.raises(LookupError):
        observer.take_chat_execution_result(turn.job_id, actor_user_uuid="user-1")

    # 会話を開き直したときは要約（「もう一度実行」）。会話の継続もできる。
    reopened = observer.get_sql_chat(turn.conversation_id, actor="user-1")
    assert reopened is not None and reopened.turns[0].last_execution is not None
    second = run(service, chat_turn("多い順にして", turn.job_id))
    assert second.status == JobStatus.DONE
    assert second.conversation_id == turn.conversation_id
    assert [
        item.id for item in observer.list_sql_chats(actor="user-1", profile_ids=None).items
    ] == [turn.job_id]


def test_chat_turn_result_expires_and_is_swept(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _ = chat
    monkeypatch.setattr(get_settings(), "nl2sql_chat_result_retention_seconds", 30)
    first = run(service, chat_turn())
    stored = repository.get_document("chat_results", first.job_id)
    assert stored is not None
    # 期限を過ぎた結果は受け取れない（画面は要約と「もう一度実行」を出す）。
    repository.put_document(
        "chat_results", first.job_id, {**stored, "expires_at": "2000-01-01T00:00:00+00:00"}
    )
    with pytest.raises(LookupError):
        service.take_chat_execution_result(first.job_id, actor_user_uuid="user-1")
    assert repository.get_document("chat_results", first.job_id) is None

    # 受け取られずに期限を過ぎた行は、次のチャットの実行で消す。
    second = run(service, chat_turn("別の質問"))
    assert repository.get_document("chat_results", second.job_id) is not None
    swept: list[int] = []
    monkeypatch.setattr(
        repository,
        "delete_documents_older_than",
        lambda collection, *, seconds: (
            swept.append(seconds) if collection == "chat_results" else None
        ),
    )
    run(service, chat_turn("三つ目"))
    assert swept == [30]


def test_chat_turn_reexecute_drops_unreceived_rows(chat: ChatFixture) -> None:
    service, repository, _ = chat
    turn = run(service, chat_turn())
    assert repository.get_document("chat_results", turn.job_id) is not None
    again = service.execute_chat_turn(
        turn.job_id, actor_user_uuid="user-1", actor_is_system_admin=False
    )
    assert again.status == "done"
    # もう一度実行した応答で新しい行を返す。ジョブの中の実行の行は残さない。
    assert repository.get_document("chat_results", turn.job_id) is None
    reopened = service.get_job(turn.job_id, actor_user_uuid="user-1")
    assert reopened is not None and reopened.last_execution is not None
    assert reopened.last_execution.result_expires_at is None
    assert reopened.last_execution.history_id == again.history_id


def test_chat_turn_blocks_dml_without_executing(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DML は SQL 生成のジョブと同じく安全検査で遮断し、自動では実行しない。"""

    service, repository, client = chat
    client.text = '{"sql":"DELETE FROM APP.ORDERS","explanation":"削除"}'
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("安全でない SQL を実行")
    )
    turn = run(service, chat_turn())
    workbench = run(
        service,
        _request().model_copy(update={"question": "注文を削除"}),
    )
    assert turn.status == workbench.status == JobStatus.ERROR
    assert turn.error_code == workbench.error_code == "SQL_BLOCKED"
    assert _step(turn, "safety_check") == JobStepStatus.ERROR
    assert _step(turn, "execute_sql") == JobStepStatus.SKIPPED
    assert turn.last_execution is None
    assert repository.get_document("chat_results", turn.job_id) is None


def test_chat_turn_execution_failure_keeps_generated_sql(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle の失敗は SQL 生成のジョブと同じ文・コード・詳細。ターンは生成した SQL を残す。"""

    service, _, _ = chat

    def fail(*_args: object, **_kwargs: object) -> tuple[SafetyReport, str, QueryResults]:
        raise OracleAdapterError(
            "SELECT の実行に失敗しました: ORA-00942: table or view does not exist"
        )

    monkeypatch.setattr(service, "execute_sql", fail)
    turn = run(service, chat_turn())
    workbench = run(service, _request())

    # 生成した SQL は残り、「もう一度実行」できる（ジョブは完了。実行の段階は失敗）。
    assert turn.status == JobStatus.DONE
    assert _step(turn, "execute_sql") == JobStepStatus.ERROR
    assert turn.result is not None and turn.result.generated_sql == "SELECT ID FROM APP.ORDERS"
    assert turn.last_execution is not None and turn.last_execution.status == "error"
    received = service.take_chat_execution_result(turn.job_id, actor_user_uuid="user-1")
    assert (received.error_message, received.error_code, received.error_detail) == (
        workbench.error_message,
        workbench.error_code,
        workbench.error_detail,
    )
    assert received.error_code == "ORA-00942"


def test_old_generation_only_chat_turns_still_continue(chat: ChatFixture) -> None:
    """#1176 より前のチャット（generation_only だけ）も会話として続けられる。"""

    service, _, _ = chat
    old = run(service, _request().model_copy(update={"generation_only": True}))
    assert old.chat is True
    assert _step(old, "execute_sql") == JobStepStatus.SKIPPED
    follow = run(service, chat_turn("続き", old.job_id))
    assert follow.conversation_id == old.conversation_id
    assert _step(follow, "execute_sql") == JobStepStatus.DONE


def test_chat_route_without_execute_permission_generates_only(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_nl2sql_operation_profile_access import _request as api_request

    from app.features.nl2sql import router
    from app.security.permissions import (
        QUERY_GENERATE_PERMISSION,
        ROUTE_PERMISSIONS,
        SQL_EXECUTE_PERMISSION,
    )

    assert ROUTE_PERMISSIONS[("POST", "/nl2sql/jobs/{job_id}/execution-result")] == frozenset(
        {SQL_EXECUTE_PERMISSION}
    )
    service, _, _ = chat
    monkeypatch.setattr(router, "nl2sql_service", service)
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("権限が無いのに実行")
    )

    # 実行の権限が無い利用者のチャットは生成だけ（拒否しない）。
    # SQL 生成の画面のジョブは今までどおり 403。
    generate_only = api_request({"orders-profile"}, permissions={QUERY_GENERATE_PERMISSION})
    created = router.create_job(chat_turn(), generate_only).data
    assert created is not None
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=created.job_id)
    job = router.get_job(created.job_id, generate_only).data
    assert job is not None
    assert job.status == JobStatus.DONE
    assert job.generation_only is True and job.chat is True
    assert _step(job, "execute_sql") == JobStepStatus.SKIPPED
    assert job.last_execution is None
    with pytest.raises(Exception) as forbidden:
        router.create_job(_request(), generate_only)
    assert getattr(forbidden.value, "status_code", None) == 403


def test_execution_result_route_checks_owner_and_profile_access(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_nl2sql_operation_profile_access import _request as api_request

    from app.features.nl2sql import router
    from app.security.permissions import QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION

    service, _, _ = chat
    monkeypatch.setattr(router, "nl2sql_service", service)
    permissions = {QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION}
    actor_request = api_request({"orders-profile"}, permissions=permissions)
    created = router.create_job(chat_turn(), actor_request).data
    assert created is not None
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=created.job_id)
    # 業務プロファイルの利用権限が外れたら受け取れない（行は残る）。
    with pytest.raises(HTTPException) as denied:
        router.take_chat_execution_result(
            created.job_id, api_request(set(), permissions=permissions)
        )
    assert denied.value.status_code == 403
    received = router.take_chat_execution_result(created.job_id, actor_request).data
    assert received is not None and received.status == "done"
    with pytest.raises(HTTPException) as gone:
        router.take_chat_execution_result(created.job_id, actor_request)
    assert gone.value.status_code == 404
