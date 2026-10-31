"""SQL チャットの永続化、文脈、生成だけのモード、所有者の境界。"""

from __future__ import annotations

from uuid import UUID

import pytest
from pr_backend_core.api import OffsetParams
from pydantic import ValidationError
from test_nl2sql_job_runtime import _FakeEnterpriseAiClient, _repository, _request, _worker

from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    AllowedObjects,
    AnalyzeData,
    JobCreateRequest,
    JobData,
    JobStatus,
    JobStepStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    QueryResults,
    SafetyReport,
)
from app.features.nl2sql.service import Nl2SqlService, OthersContentForbiddenError
from app.settings import get_settings


class RecordingClient(_FakeEnterpriseAiClient):
    def __init__(self) -> None:
        super().__init__('{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID"}')
        self.prompts: list[str] = []

    def generate(self, **kwargs: object) -> str:
        self.prompts.append(str(kwargs["prompt"]))
        return self.text


type ChatFixture = tuple[Nl2SqlService, MemoryIncrementalNl2SqlRepository, RecordingClient]


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> ChatFixture:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    client = RecordingClient()
    service = _worker(repository, client)
    return service, repository, client


def request(question: str = "注文一覧", previous: str | None = None) -> JobCreateRequest:
    return _request().model_copy(
        update={
            "question": question,
            "generation_only": True,
            "previous_job_id": previous,
        }
    )


def run(service: Nl2SqlService, req: JobCreateRequest, actor: str = "user-1") -> JobData:
    created = service.start_job(req, actor_user_uuid=actor, actor_is_system_admin=True)
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid=actor)
    assert job is not None
    return job


def test_chat_generates_without_execution_and_restores_across_workers(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, client = chat
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("送信で SQL を実行")
    )
    first = run(service, request())
    assert first.status == JobStatus.DONE
    assert first.result is not None
    assert first.result.results.total == 0
    assert (
        next(step for step in first.steps if step.stage == "execute_sql").status
        == JobStepStatus.SKIPPED
    )
    second = run(service, request("多い順にして", first.job_id))
    assert second.status == JobStatus.DONE
    assert second.conversation_id == first.job_id
    assert second.question == "多い順にして"
    assert "注文一覧" in client.prompts[-1]
    assert "SELECT ID FROM APP.ORDERS" in client.prompts[-1]
    assert "多い順にして" in client.prompts[-1]
    observer = _worker(repository)
    restored = observer.get_sql_chat(first.job_id, actor="user-1")
    assert restored is not None
    assert [turn.job_id for turn in restored.turns] == [first.job_id, second.job_id]
    summaries = observer.list_sql_chats(actor="user-1", profile_ids=None)
    assert [item.id for item in summaries.items] == [first.job_id]
    assert observer.list_sql_chats(actor="other", profile_ids=None).items == []
    assert observer.list_sql_chats(actor="user-1", profile_ids=set()).items == []


def test_chat_list_pages_with_offset_and_total(chat: ChatFixture) -> None:
    """会話の履歴は新しい順に 1 ページずつ返し、任意のページへ移れる（#1265・#1891）。"""
    service, repository, _ = chat
    first = run(service, request())
    second = run(service, request("顧客の一覧"))
    observer = _worker(repository)
    page = observer.list_sql_chats(actor="user-1", profile_ids=None, limit=1)
    assert [item.id for item in page.items] == [second.job_id]
    assert (page.total, page.limit, page.offset, page.has_next) == (2, 1, 0, True)
    rest = observer.list_sql_chats(actor="user-1", profile_ids=None, offset=1, limit=1)
    assert [item.id for item in rest.items] == [first.job_id]
    assert rest.total == 2
    assert rest.has_next is False
    beyond = observer.list_sql_chats(actor="user-1", profile_ids=None, offset=5, limit=1)
    assert beyond.items == []
    assert (beyond.total, beyond.has_next) == (2, False)
    default = observer.list_sql_chats(actor="user-1", profile_ids=None)
    assert default.limit == 25 and len(default.items) == 2


def test_chat_rejects_other_user_and_profile_changes(chat: ChatFixture) -> None:
    service, repository, _ = chat
    first = run(service, request())
    # 続きは会話の持ち主だけ（#3244）。見えない一般の利用者は PermissionError（404）、見える
    # system_admin は OthersContentForbiddenError（403）。
    with pytest.raises(PermissionError):
        service.start_job(
            request("続き", first.job_id), actor_user_uuid="other", actor_is_system_admin=False
        )
    with pytest.raises(OthersContentForbiddenError):
        service.start_job(
            request("続き", first.job_id), actor_user_uuid="admin", actor_is_system_admin=True
        )
    with pytest.raises(PermissionError):
        service.get_sql_chat(first.job_id, actor="other")
    with pytest.raises(PermissionError):
        service.get_job(first.job_id, actor_user_uuid="other", actor_can_manage=False)
    with pytest.raises(PermissionError):
        service.request_job_cancel(first.job_id, actor_user_uuid="other", actor_can_manage=False)
    # system_admin（`all_users` / `actor_can_manage`）は他の利用者の会話を見られる（#1889）。
    assert service.get_sql_chat(first.job_id, actor="admin", all_users=True) is not None
    assert service.get_job(first.job_id, actor_user_uuid="admin", actor_can_manage=True)
    repository.save_profile(
        Nl2SqlProfile(id="another", name="別の業務", allowed_tables=["APP.ORDERS"]),
        expected_etag=None,
    )
    with pytest.raises(ValueError, match="別の業務プロファイル"):
        service.start_job(
            request("続き", first.job_id).model_copy(update={"profile_id": "another"}),
            actor_user_uuid="user-1",
        )


def test_chat_rejects_pending_and_non_chat_parents(chat: ChatFixture) -> None:
    service, _, _ = chat
    pending = service.start_job(request(), actor_user_uuid="user-1")
    with pytest.raises(ValueError, match="終わるか"):
        service.start_job(request("続き", pending.job_id), actor_user_uuid="user-1")
    ordinary = run(service, _request())
    with pytest.raises(ValueError, match="チャット"):
        service.start_job(request("続き", ordinary.job_id), actor_user_uuid="user-1")
    assert service.get_sql_chat(ordinary.job_id, actor="user-1") is None
    with pytest.raises(ValueError, match="見つかりません"):
        service.start_job(request("続き", "missing"), actor_user_uuid="user-1")


def test_existing_jobs_still_execute_by_default(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = chat
    execute = service.execute_sql
    calls: list[str] = []

    def record(
        sql: str,
        allowed: AllowedObjects,
        row_limit: int | None,
        *,
        analysis: AnalyzeData | None = None,
    ) -> tuple[SafetyReport, str, QueryResults]:
        calls.append(sql)
        return execute(sql, allowed, row_limit, analysis=analysis)

    monkeypatch.setattr(service, "execute_sql", record)
    job = run(service, _request())
    assert job.status == JobStatus.DONE
    assert calls == ["SELECT ID FROM APP.ORDERS"]
    assert job.conversation_id == ""


def test_chat_safety_still_blocks_write_sql(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, client = chat
    client.text = '{"sql":"DELETE FROM APP.ORDERS","explanation":"削除"}'
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("危険 SQL を実行")
    )
    job = run(service, request())
    assert job.status == JobStatus.ERROR
    assert job.result is not None and not job.result.safety.is_safe


def test_chat_uses_recent_turns_within_shared_budget(chat: ChatFixture) -> None:
    """既定の共通の予算（8 往復）に入る直近の往復だけを渡す（#1818）。"""
    service, _, client = chat
    previous = None
    for number in range(12):
        job = run(service, request(f"条件-{number:03}", previous))
        assert job.status == JobStatus.DONE
        previous = job.job_id
    assert "条件-002" not in client.prompts[-1]
    assert "条件-003" in client.prompts[-1]
    assert "条件-010" in client.prompts[-1]
    assert "条件-011" in client.prompts[-1]


def test_conversation_input_limits() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(question="続き", previous_job_id="job", generation_only=False)
    with pytest.raises(ValidationError):
        JobCreateRequest(question="あ" * 10001, generation_only=True)


def test_chat_route_accepts_generation_capability_without_execution(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException
    from pr_system_settings.auth.errors import SecurityApiError
    from test_nl2sql_operation_profile_access import _request as api_request

    from app.features.nl2sql import router
    from app.security.permissions import QUERY_GENERATE_PERMISSION

    service, _, _ = chat
    monkeypatch.setattr(router, "nl2sql_service", service)
    actor_request = api_request({"orders-profile"}, permissions={QUERY_GENERATE_PERMISSION})
    created = router.create_job(request(), actor_request).data
    assert created is not None
    listed = router.list_sql_chats(actor_request, OffsetParams(limit=10, offset=0)).data
    assert listed is not None and listed.items[0].id == created.job_id
    assert router.get_sql_chat(created.job_id, actor_request).data is not None
    with pytest.raises(SecurityApiError):
        router.create_job(_request(), actor_request)
    with pytest.raises(HTTPException) as denied:
        router.get_sql_chat(
            created.job_id, api_request(set(), permissions={QUERY_GENERATE_PERMISSION})
        )
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied_job:
        router.get_job(created.job_id, api_request(set(), permissions={QUERY_GENERATE_PERMISSION}))
    assert denied_job.value.status_code == 403


def test_stale_conversation_cannot_overwrite_later_turn(chat: ChatFixture) -> None:
    service, _, _ = chat
    first = run(service, request())
    run(service, request("追加", first.job_id))
    with pytest.raises(ValueError, match="更新されています"):
        service.start_job(request("古い画面から送信", first.job_id), actor_user_uuid="user-1")


def test_chat_history_has_generation_only_marker(chat: ChatFixture) -> None:
    service, repository, _ = chat
    job = run(service, request())
    assert job.result is not None
    history = repository.get_document("history", job.result.history_id)
    assert history is not None and history["generation_only"] is True


def test_chat_rejects_blank_query() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(question=" ", generation_only=True)


def test_chat_accepts_select_ai_agent_and_generates_without_execution(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select AI Agent もチャットで選べる（#890）。SQL ツールは SHOWSQL で、送信では実行しない。"""

    service, _, _ = chat
    assert JobCreateRequest(
        question="注文", engine=Nl2SqlEngine.SELECT_AI_AGENT, generation_only=True
    ).generation_only
    calls: list[dict[str, object]] = []

    def _run_team(**kwargs: object) -> tuple[str, str]:
        # 実 Oracle の DBMS_CLOUD_AI_AGENT.RUN_TEAM の代わりの決定論スタブ。
        calls.append(kwargs)
        sql = "SELECT ID FROM APP.ORDERS" + (" ORDER BY ID DESC" if len(calls) > 1 else "")
        return sql, f"conversation-{len(calls)}"

    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_assert_select_ai_scope_ready", lambda _profile: None)
    monkeypatch.setattr(service._oracle_adapter, "run_select_ai_agent_team", _run_team)  # noqa: SLF001
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("送信で SQL を実行")
    )
    agent = {"engine": Nl2SqlEngine.SELECT_AI_AGENT}
    first = run(service, request().model_copy(update=agent))
    assert first.status == JobStatus.DONE, first.error_message
    assert first.result is not None
    assert first.result.engine == Nl2SqlEngine.SELECT_AI_AGENT
    assert first.result.generated_sql.startswith("SELECT ID FROM APP.ORDERS")
    assert first.result.results.total == 0
    assert (
        next(step for step in first.steps if step.stage == "execute_sql").status
        == JobStepStatus.SKIPPED
    )
    second = run(service, request("新しい順にして", first.job_id).model_copy(update=agent))
    assert second.status == JobStatus.DONE, second.error_message
    assert second.conversation_id == first.job_id
    # 継続のターンは、前の質問と生成 SQL を Agent への依頼文に含める。
    question = str(calls[-1]["question"])
    assert "注文一覧" in question
    assert "SELECT ID FROM APP.ORDERS" in question
    # 利用者の自然言語の入力は「質問」と呼ぶ（#1183）。
    assert "最新の質問: 新しい順にして" in question


def test_client_job_id_replays_the_created_job_after_lost_response(chat: ChatFixture) -> None:
    """送信の応答が届かなくても、画面の決めた ID で同じジョブを取り直せる（#900）。"""

    service, repository, _ = chat
    client_job_id = UUID("6f1c2b0e-4d7a-4c1e-9f3a-2b8d5e7c9a10")
    first_request = request().model_copy(update={"client_job_id": client_job_id})
    created = service.start_job(first_request, actor_user_uuid="user-1")
    assert created.job_id == str(client_job_id)
    # 同じ送信の再送は、2 つ目のジョブを作らず同じジョブを返す。
    replayed = service.start_job(first_request, actor_user_uuid="user-1")
    assert replayed.job_id == str(client_job_id)
    assert replayed.created_at == created.created_at
    assert [document["job_id"] for document in repository.list_documents("jobs", limit=10)] == [
        str(client_job_id)
    ]
    job = service.get_job(str(client_job_id), actor_user_uuid="user-1")
    assert job is not None and job.conversation_id == str(client_job_id)
    # 会話の続きでも、最後の turn が再送のジョブ自身なので「会話が更新されています」にしない。
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=str(client_job_id))
    follow_id = UUID("0c9e8d7f-6a5b-4c3d-8e2f-1a0b9c8d7e6f")
    follow = request("多い順にして", str(client_job_id)).model_copy(
        update={"client_job_id": follow_id}
    )
    assert service.start_job(follow, actor_user_uuid="user-1").job_id == str(follow_id)
    assert service.start_job(follow, actor_user_uuid="user-1").job_id == str(follow_id)


def test_client_job_id_never_overwrites_another_job(chat: ChatFixture) -> None:
    """画面の決めた ID が別の利用者・別の内容のジョブと重なったら上書きせず拒否する（#900）。"""

    from app.features.nl2sql.service import JobIdConflictError

    service, _, _ = chat
    client_job_id = UUID("7a2d3c4b-5e6f-4a1b-8c9d-0e1f2a3b4c5d")
    owned = request().model_copy(update={"client_job_id": client_job_id})
    service.start_job(owned, actor_user_uuid="user-1")
    with pytest.raises(JobIdConflictError):
        service.start_job(owned, actor_user_uuid="other")
    with pytest.raises(JobIdConflictError):
        service.start_job(
            owned.model_copy(update={"question": "別の質問"}), actor_user_uuid="user-1"
        )
    job = service.get_job(str(client_job_id), actor_user_uuid="user-1")
    assert job is not None and job.question == "注文一覧"


def test_client_job_id_must_be_a_uuid() -> None:
    with pytest.raises(ValidationError):
        JobCreateRequest(question="注文一覧", generation_only=True, client_job_id="job-1")


def test_create_job_route_maps_client_job_id_conflict_to_409(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException
    from test_nl2sql_operation_profile_access import _request as api_request

    from app.features.nl2sql import router
    from app.security.permissions import QUERY_GENERATE_PERMISSION

    service, _, _ = chat
    monkeypatch.setattr(router, "nl2sql_service", service)
    actor_request = api_request({"orders-profile"}, permissions={QUERY_GENERATE_PERMISSION})
    owned = request().model_copy(
        update={"client_job_id": UUID("1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e")}
    )
    first = router.create_job(owned, actor_request).data
    replayed = router.create_job(owned, actor_request).data
    assert first is not None and replayed is not None and first.job_id == replayed.job_id
    with pytest.raises(HTTPException) as conflict:
        router.create_job(owned.model_copy(update={"question": "別の質問"}), actor_request)
    assert conflict.value.status_code == 409


def test_chat_rename_and_delete_keep_jobs(chat: ChatFixture) -> None:
    """会話の名前の変更・削除は本人だけ。削除してもジョブは残し、会話から外す（#1817）。"""
    from app.features.nl2sql.service import ChatConversationBusyError

    service, repository, _ = chat
    first = run(service, request())
    second = run(service, request("多い順にして", first.job_id))

    renamed = service.rename_sql_chat(first.job_id, actor="user-1", title=" 注文の確認 ")
    assert renamed.title == "注文の確認"
    observer = _worker(repository)
    listed = observer.list_sql_chats(actor="user-1", profile_ids=None).items
    assert [item.title for item in listed] == ["注文の確認"]
    with pytest.raises(PermissionError):
        service.rename_sql_chat(first.job_id, actor="other", title="横取り")
    with pytest.raises(PermissionError):
        service.delete_sql_chat(first.job_id, actor="other")

    # SQL の生成中のターンがある会話は消さない。
    pending = service.start_job(request("上位 5 件", second.job_id), actor_user_uuid="user-1")
    with pytest.raises(ChatConversationBusyError):
        service.delete_sql_chat(first.job_id, actor="user-1")
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=pending.job_id)

    service.delete_sql_chat(first.job_id, actor="user-1")
    restored = _worker(repository)
    assert restored.list_sql_chats(actor="user-1", profile_ids=None).items == []
    assert restored.get_sql_chat(first.job_id, actor="user-1") is None
    with pytest.raises(ValueError, match="前の会話が見つかりません"):
        restored.start_job(request("続き", pending.job_id), actor_user_uuid="user-1")
    with pytest.raises(LookupError):
        restored.rename_sql_chat(first.job_id, actor="user-1", title="x")
    # ジョブは監査・フィードバック・実行履歴のために残る。
    kept = restored.get_job(second.job_id, actor_user_uuid="user-1")
    assert kept is not None and kept.status == JobStatus.DONE


def test_chat_rename_and_delete_routes(chat: ChatFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PATCH` / `DELETE /nl2sql/chats/{id}`（#1817）。生成中は 409、空の名前は検証で拒否。"""
    from fastapi import HTTPException
    from test_nl2sql_operation_profile_access import _request as api_request

    from app.features.nl2sql import router
    from app.features.nl2sql.models import SqlChatUpdateRequest
    from app.security.permissions import QUERY_GENERATE_PERMISSION

    service, _, _ = chat
    monkeypatch.setattr(router, "nl2sql_service", service)
    actor_request = api_request({"orders-profile"}, permissions={QUERY_GENERATE_PERMISSION})
    created = router.create_job(request(), actor_request).data
    assert created is not None
    with pytest.raises(ValidationError):
        SqlChatUpdateRequest(title="   ")
    with pytest.raises(ValidationError):
        SqlChatUpdateRequest(title="あ" * 81)

    with pytest.raises(HTTPException) as busy:
        router.rename_sql_chat(created.job_id, SqlChatUpdateRequest(title="x"), actor_request)
    assert busy.value.status_code == 409
    with pytest.raises(HTTPException) as busy_delete:
        router.delete_sql_chat(created.job_id, actor_request)
    assert busy_delete.value.status_code == 409
    assert service.run_next_nl2sql_job(worker_id="chat-test", job_id=created.job_id)

    renamed = router.rename_sql_chat(
        created.job_id, SqlChatUpdateRequest(title="注文の確認"), actor_request
    ).data
    assert renamed is not None and renamed.title == "注文の確認"
    router.delete_sql_chat(created.job_id, actor_request)
    with pytest.raises(HTTPException) as missing:
        router.get_sql_chat(created.job_id, actor_request)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as missing_delete:
        router.delete_sql_chat(created.job_id, actor_request)
    assert missing_delete.value.status_code == 404
