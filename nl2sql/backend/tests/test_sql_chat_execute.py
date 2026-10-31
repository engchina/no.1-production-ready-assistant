"""チャットのターンの SQL の実行（#1154）。

SQL 生成のジョブの実行の段階と同じ経路（業務プロファイルの範囲・安全検査・`execute_sql`・
失敗の扱い・実行履歴）で実行すること、本人・権限・業務プロファイルの境界、上限、結果の行を保存しないことを確かめる。
接続の種類（system_admin / 非 system_admin）は `test_nl2sql_db_roundtrips.py` の
`test_chat_execution_connection_follows_requesting_actor` が確かめる。
"""

from __future__ import annotations

from typing import Any

import pytest
from test_nl2sql_job_runtime import _repository, _request, _worker
from test_sql_chat import ChatFixture, RecordingClient, request, run

from app.features.nl2sql.models import (
    AllowedObjects,
    AnalyzeData,
    JobStatus,
    QueryResults,
    SafetyReport,
    SqlChatExecuteData,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError
from app.features.nl2sql.service import (
    Nl2SqlService,
    OthersContentForbiddenError,
    _cap_chat_results,
)
from app.security.request_actor import current_actor_context
from app.settings import get_settings


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> ChatFixture:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    client = RecordingClient()
    return _worker(repository, client), repository, client


def _execute(
    service: Nl2SqlService, job_id: str, *, actor: str = "user-1", admin: bool = False
) -> SqlChatExecuteData:
    return service.execute_chat_turn(job_id, actor_user_uuid=actor, actor_is_system_admin=admin)


def test_chat_execute_runs_stored_sql_and_keeps_only_summary(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _ = chat
    turn = run(service, request())
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
    data = _execute(service, turn.job_id, admin=True)

    assert data.status == "done", data.error_message
    # 画面から SQL を受け取らず、ターンに保存した生成 SQL を、設定の行数の上限で実行する。
    assert [(sql, limit) for sql, limit, _ in calls] == [
        ("SELECT ID FROM APP.ORDERS", get_settings().nl2sql_chat_result_max_rows)
    ]
    # 実行の actor は要求の利用者（system_admin か）を明示する（接続の分離。#904）。
    actor = calls[0][2]
    assert (actor.user_uuid, actor.is_system_admin) == ("user-1", True)
    assert data.results.total == len(data.results.rows) > 0
    assert data.row_limit == get_settings().nl2sql_chat_result_max_rows

    # 実行履歴（監査）を SQL 生成の実行と同じ形で残す。
    history = repository.get_document("history", data.history_id)
    assert history is not None
    assert history["generation_only"] is False
    assert history["session_id"] == turn.conversation_id
    assert history["actor_user_uuid"] == "user-1"
    assert history["result_row_count"] == data.results.total
    assert history["generated_sql"] == "SELECT ID FROM APP.ORDERS"
    # SQL を実行するだけでモデルを呼ばないので、利用量は実測の 0（#3543）。
    assert history["usage"]["requests"] == history["usage"]["total_tokens"] == 0
    assert history["usage"]["measurement"] == "complete"

    # ターンのジョブには要約だけを残し、行は保存しない。別の worker で開き直しても要約が出る。
    document = repository.get_document("jobs", turn.job_id)
    assert document is not None
    assert document["last_execution"]["row_count"] == data.results.total
    assert document["result"]["results"]["rows"] == []
    reopened = _worker(repository).get_sql_chat(turn.conversation_id, actor="user-1")
    assert reopened is not None
    summary = reopened.turns[0].last_execution
    assert summary is not None
    assert summary.status == "done"
    assert summary.row_count == data.results.total
    assert summary.history_id == data.history_id
    assert reopened.turns[0].result is not None
    assert reopened.turns[0].result.results.rows == []


def test_chat_execute_rejects_other_users_and_non_chat_jobs(chat: ChatFixture) -> None:
    service, _, _ = chat
    turn = run(service, request())
    # 実行は会話の持ち主だけ（#3244）。見えない一般の利用者は PermissionError（404）、見える
    # system_admin は OthersContentForbiddenError（403）で、どちらも SQL を実行しない。
    with pytest.raises(PermissionError):
        _execute(service, turn.job_id, actor="other", admin=False)
    with pytest.raises(OthersContentForbiddenError):
        _execute(service, turn.job_id, actor="admin", admin=True)
    assert service.get_job(turn.job_id, actor_user_uuid="user-1").last_execution is None  # type: ignore[union-attr]
    with pytest.raises(PermissionError):
        _execute(service, turn.job_id, actor="")
    with pytest.raises(LookupError):
        _execute(service, "missing")
    ordinary = run(service, _request())
    with pytest.raises(ValueError, match="チャット"):
        _execute(service, ordinary.job_id)
    pending = service.start_job(request("続き", turn.job_id), actor_user_uuid="user-1")
    with pytest.raises(ValueError, match="完了していない"):
        _execute(service, pending.job_id)


def test_chat_execute_rejects_unsafe_generated_sql(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, client = chat
    client.text = '{"sql":"DELETE FROM APP.ORDERS","explanation":"削除"}'
    turn = run(service, request())
    assert turn.status == JobStatus.ERROR
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("安全でない SQL を実行")
    )
    with pytest.raises(ValueError, match="安全検査"):
        _execute(service, turn.job_id)


def test_chat_execute_rechecks_profile_scope_like_generation_jobs(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成の後に業務プロファイルの範囲から表が外れたら実行しない（SQL 生成のジョブと同じ）。"""

    service, _, _ = chat
    turn = run(service, request())
    resolved: list[str | None] = []

    def narrowed(profile_id: str | None, _requested: AllowedObjects) -> AllowedObjects:
        resolved.append(profile_id)
        return AllowedObjects(table_names=["APP.CUSTOMERS"], enforce_table_scope=True)

    monkeypatch.setattr(service, "_resolve_allowed_objects", narrowed)
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("範囲外の SQL を実行")
    )
    data = _execute(service, turn.job_id)
    assert data.status == "error"
    assert data.error_code == "SQL_BLOCKED"
    assert data.error_message
    assert data.results.rows == []
    reopened = service.get_sql_chat(turn.conversation_id, actor="user-1")
    assert reopened is not None and reopened.turns[0].last_execution is not None
    assert reopened.turns[0].last_execution.error_code == "SQL_BLOCKED"
    # 範囲はターンの業務プロファイルで解決する（SQL 生成のジョブと同じ）。
    assert resolved == ["orders-profile"]


def test_chat_execute_failure_matches_generation_job_failure(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle のエラーは SQL 生成のジョブと同じ利用者向けの文・コード・詳細に分ける。"""

    service, repository, _ = chat
    turn = run(service, request())

    def fail(*_args: object, **_kwargs: object) -> tuple[SafetyReport, str, QueryResults]:
        raise OracleAdapterError(
            "SELECT の実行に失敗しました: ORA-00942: table or view does not exist"
        )

    monkeypatch.setattr(service, "execute_sql", fail)
    data = _execute(service, turn.job_id)
    job = run(service, _request())

    assert data.status == "error"
    assert job.status == JobStatus.ERROR
    assert (data.error_message, data.error_code, data.error_detail) == (
        job.error_message,
        job.error_code,
        job.error_detail,
    )
    assert data.error_code == "ORA-00942"
    assert data.error_detail is not None and "ORA-00942" in data.error_detail
    assert "ORA-" not in (data.error_message or "")
    history = repository.get_document("history", data.history_id)
    assert history is not None and history["result_row_count"] == 0


def test_chat_results_are_capped_by_cells_and_bytes() -> None:
    results = QueryResults(
        columns=["ID", "NOTE"],
        rows=[{"ID": index, "NOTE": "あ" * 300} for index in range(100)],
        total=100,
    )
    capped, cells_truncated = _cap_chat_results(results, max_cell_chars=100, max_bytes=10_000)
    assert cells_truncated
    assert all(row["NOTE"] == "あ" * 100 + "…" for row in capped.rows)
    assert 0 < len(capped.rows) < 100
    assert capped.total == capped.returned_count == len(capped.rows)
    assert capped.has_more and capped.truncated

    small = QueryResults(columns=["ID"], rows=[{"ID": 1}, {"ID": None}], total=2)
    same, truncated = _cap_chat_results(small, max_cell_chars=100, max_bytes=10_000)
    assert not truncated
    assert same.rows == small.rows
    assert not same.has_more

    # 取得の上限で打ち切った結果（adapter の has_more）は打ち切りのまま返す。
    partial = small.model_copy(update={"has_more": True, "truncated": True})
    kept, _ = _cap_chat_results(partial, max_cell_chars=100, max_bytes=10_000)
    assert kept.has_more and kept.truncated


def test_chat_execute_route_requires_execute_permission_and_profile_access(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException
    from test_nl2sql_operation_profile_access import _request as api_request

    from app.features.nl2sql import router
    from app.security.permissions import (
        QUERY_GENERATE_PERMISSION,
        ROUTE_PERMISSIONS,
        SQL_EXECUTE_PERMISSION,
    )

    # 実行は SQL 生成のジョブ・SELECT SQL の実行と同じ実行の権限（menu.chat だけでは実行できない）。
    assert ROUTE_PERMISSIONS[("POST", "/nl2sql/jobs/{job_id}/execute")] == frozenset(
        {SQL_EXECUTE_PERMISSION}
    )
    service, _, _ = chat
    monkeypatch.setattr(router, "nl2sql_service", service)
    permissions = {QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION}
    actor_request = api_request({"orders-profile"}, permissions=permissions)
    created = router.create_job(request(), actor_request).data
    assert created is not None
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=created.job_id)
    executed = router.execute_chat_turn(created.job_id, actor_request).data
    assert executed is not None and executed.status == "done"
    # 業務プロファイルの利用権限が外れたら実行しない。
    with pytest.raises(HTTPException) as denied:
        router.execute_chat_turn(created.job_id, api_request(set(), permissions=permissions))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        router.execute_chat_turn("missing", actor_request)
    assert missing.value.status_code == 404
