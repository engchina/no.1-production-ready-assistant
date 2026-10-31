"""チャットの「会話の履歴」の期間（最後の更新が直近 N 日。#2340）。

NL2SQL チャットと DB 管理チャットの会話の一覧は、共通の設定 `PLATFORM_CHAT_HISTORY_DAYS` の期間の
中の会話だけを、件数と 1 ページの両方で返す。期間の外の会話は消さず、ID では開ける。
LLM と Oracle は決定論のスタブ（memory の保存先）と、SQL の文字列の確認で置き換える。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from test_nl2sql_db_admin_chat_history import ALICE, PAGE, _answer, _http, _send, setup
from test_nl2sql_job_runtime import _worker
from test_sql_chat import ChatFixture, chat, request, run

from app.features.nl2sql import router
from app.features.nl2sql.db_admin_chat_store import DbAdminChatStore
from app.features.nl2sql.incremental_store import OracleIncrementalNl2SqlRepository
from app.features.nl2sql.service import Nl2SqlService, StoredJob
from app.settings import get_settings

__all__ = ["chat", "setup"]  # pytest の fixture を他のテストの module から使う

OLD = (datetime.now(UTC) - timedelta(days=91)).isoformat()


def test_sql_chat_list_excludes_conversations_outside_history_days(chat: ChatFixture) -> None:
    """期間の外の会話は一覧と件数から外れ、続きのターンを送ると会話の最後の更新が進んで戻る。"""
    service, repository, _ = chat
    first = run(service, request())
    second = run(service, request("顧客の一覧"))
    # 最初の会話の最後の更新を 91 日前にする（会話の最初のジョブの記録の更新時刻）。
    repository._documents[("jobs", first.job_id)]["_updated_at"] = OLD  # noqa: SLF001

    observer = _worker(repository)
    page = observer.list_sql_chats(actor="user-1", profile_ids=None, history_days=90)
    assert [item.id for item in page.items] == [second.job_id]
    assert (page.total, page.history_days) == (1, 90)
    # 0 は期間で絞らない。
    unfiltered = observer.list_sql_chats(actor="user-1", profile_ids=None, history_days=0)
    assert unfiltered.total == 2 and unfiltered.history_days == 0
    # 期間の外の会話も ID では開ける。
    assert observer.get_sql_chat(first.job_id, actor="user-1") is not None

    # 続きのターンを送ると、会話の最後の更新（続きのターンの記録）が期間の中になり、一覧に戻る。
    # 会話の最初のジョブの記録は書き直さない（投入の往復を増やさない）。
    follow_up = run(service, request("多い順にして", first.job_id))
    assert follow_up.conversation_id == first.job_id
    assert repository._documents[("jobs", first.job_id)]["_updated_at"] == OLD  # noqa: SLF001
    page = _worker(repository).list_sql_chats(actor="user-1", profile_ids=None, history_days=90)
    assert {item.id for item in page.items} == {first.job_id, second.job_id}
    assert page.total == 2


def test_sql_chat_route_uses_the_shared_setting(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, _ = chat
    first = run(service, request())
    repository._documents[("jobs", first.job_id)]["_updated_at"] = OLD  # noqa: SLF001
    observer = _worker(repository)
    monkeypatch.setattr(router, "nl2sql_service", observer)
    monkeypatch.setattr(router, "_allowed_profile_ids_for_request", lambda _request: None)
    monkeypatch.setattr(router, "_chat_all_users", lambda _request: False)
    monkeypatch.setattr(get_settings(), "chat_history_days", 30)

    principal = type("P", (), {"user_uuid": "user-1"})()
    http = type("R", (), {"state": type("S", (), {"principal": principal})()})()
    listed = router.list_sql_chats(http, PAGE).data
    assert listed is not None
    assert listed.items == [] and listed.total == 0 and listed.history_days == 30


def test_sql_chat_list_without_repository_uses_the_last_turn() -> None:
    """保存先の無い構成（memory）は、会話のジョブの最も新しい時刻で絞る。"""
    service = Nl2SqlService.__new__(Nl2SqlService)
    service._incremental_repository = None  # noqa: SLF001
    now = datetime.now(UTC).isoformat()
    jobs = {
        "recent": _chat_job("recent", created_at=now),
        "old": _chat_job("old", created_at=OLD),
        "continued": _chat_job("continued", created_at=OLD),
        "continued-2": _chat_job("continued-2", created_at=now, conversation="continued"),
    }
    service._lock = threading.RLock()  # noqa: SLF001
    service._jobs = jobs  # noqa: SLF001
    page = service.list_sql_chats(actor="user-1", profile_ids=None, history_days=90)
    assert [item.id for item in page.items] == ["recent", "continued"]
    assert page.total == 2


def _chat_job(job_id: str, *, created_at: str, conversation: str = "") -> StoredJob:
    return StoredJob(
        job_id=job_id,
        request=request(previous=conversation or None),
        conversation_id=conversation or job_id,
        actor_user_uuid="user-1",
        created_at=created_at,
    )


def test_oracle_chat_list_filters_by_updated_at_in_db_time() -> None:
    """Oracle は件数と 1 ページの両方で、会話の文書の UPDATED_AT を DB の時刻と日数で比べる。"""
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0,)
    cursor.fetchall.return_value = []
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = OracleIncrementalNl2SqlRepository(connection_factory=lambda: context)

    repository.list_documents_page(
        "jobs",
        cursor=None,
        offset=0,
        limit=10,
        payload_filters={"chat_root": "1"},
        conversation_active_within_days=90,
    )
    for call in cursor.execute.call_args_list[:2]:
        sql, binds = call.args
        assert "ENTITY_ID IN (SELECT JSON_VALUE(a.PAYLOAD_JSON, '$.conversation_id'" in sql
        assert "a.UPDATED_AT >= SYSTIMESTAMP - NUMTODSINTERVAL(" in sql
        assert ":conversation_active_within_days, 'DAY'))" in sql
        assert binds["conversation_active_within_days"] == 90

    cursor.execute.reset_mock()
    repository.count_documents("jobs", payload_filters={"chat_root": "1"})
    sql, binds = cursor.execute.call_args.args
    assert (
        "conversation_active_within_days" not in sql
        and "conversation_active_within_days" not in binds
    )


def test_db_admin_chat_list_excludes_conversations_outside_history_days(
    setup: tuple[Nl2SqlService, object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _adapter, ai = setup
    ai.responses.extend([_answer("SELECT 1 FROM DUAL"), _answer("SELECT 2 FROM DUAL")])  # type: ignore[attr-defined]
    old = _send(ALICE, "質問 1").conversation.id
    recent = _send(ALICE, "質問 2").conversation.id
    store = service._db_admin_chat_store  # noqa: SLF001
    stored = store._conversations[old]  # noqa: SLF001
    stored.summary = stored.summary.model_copy(update={"updated_at": OLD})
    monkeypatch.setattr(get_settings(), "chat_history_days", 90)

    listed = router.list_db_admin_chats(_http(ALICE), PAGE).data
    assert listed is not None
    assert [item.id for item in listed.items] == [recent]
    assert (listed.total, listed.history_days) == (1, 90)
    # 期間の外の会話も ID では開ける。
    assert router.get_db_admin_chat(old, _http(ALICE)).data is not None

    monkeypatch.setattr(get_settings(), "chat_history_days", 0)
    listed = router.list_db_admin_chats(_http(ALICE), PAGE).data
    assert listed is not None and listed.total == 2 and listed.history_days == 0


def test_db_admin_chat_store_oracle_filters_count_and_page() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0,)
    cursor.fetchall.return_value = []
    context = MagicMock()
    context.__enter__.return_value = connection
    store = DbAdminChatStore(lambda: context)

    store.list_conversations(owner_user_uuid="alice", offset=0, limit=25, updated_within_days=90)
    count_sql, count_binds = cursor.execute.call_args_list[0].args
    page_sql, page_binds = cursor.execute.call_args_list[1].args
    predicate = "UPDATED_AT >= SYSTIMESTAMP - NUMTODSINTERVAL(:days, 'DAY')"
    assert predicate in count_sql and predicate in page_sql
    assert "OWNER_USER_UUID = :owner AND" in count_sql
    assert count_binds == {"owner": "alice", "days": 90}
    assert page_binds["days"] == 90

    cursor.execute.reset_mock()
    store.list_conversations(owner_user_uuid=None, offset=0, limit=25)
    count_sql, count_binds = cursor.execute.call_args_list[0].args
    assert "WHERE" not in count_sql and count_binds == {}
