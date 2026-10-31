"""「AI 活用」の下の機能のデータの見え方（#1889・#1954・#3244）。

system_admin は全員のデータを見て、運用の操作（停止・削除）ができる。新しい実行・内容を足す操作
（続きの質問・SQL の実行・回答前の確認・回答の評価・名前の変更）は持ち主だけで、system_admin でも
他の利用者のデータには 403（#3244。#1928 を置き換え）。それ以外の利用者は自分のデータだけを扱い、
他人のデータの詳細・操作は 404（存在を漏らさない）。#3244 より前に system_admin が他の利用者の
会話に送ったターン・実行は消さず、送り手の表示名つきで持ち主にも見せる。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from pr_backend_core.api import OffsetParams
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_CODE, UserIdentity
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)
from test_nl2sql_job_runtime import _PROFILE_ID, _request
from test_sql_chat import ChatFixture, chat, request  # noqa: F401 - fixture を使う

from app.features.nl2sql import router
from app.features.nl2sql.models import (
    AllowedObjects,
    AnalyzeData,
    FeedbackRating,
    FeedbackRequest,
    HistoryItem,
    JobCreateRequest,
    JobOrigin,
    Nl2SqlEngine,
    QueryResults,
    SafetyReport,
    SqlChatUpdateRequest,
)
from app.features.nl2sql.service import Nl2SqlService, OthersContentForbiddenError
from app.security.domain import Principal
from app.security.permissions import (
    FEEDBACK_MANAGE_PERMISSION,
    QUERY_GENERATE_PERMISSION,
    SQL_EXECUTE_PERMISSION,
)
from app.security.request_actor import current_actor_context


def _principal(user_uuid: str, *, admin: bool = False, permissions: set[str] | None = None) -> Any:
    return Principal(
        user_uuid=user_uuid,
        login_user_id=user_uuid,
        display_name=f"{user_uuid} さん",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE] if admin else ["ANALYST"],
        permissions={QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION, *(permissions or set())},
        data_entitlements=[],
        allowed_profile_ids={_PROFILE_ID},
        session_id=f"session-{user_uuid}",
        csrf_token_hash="csrf",  # nosec B106 - テスト用
    )


def _http(principal: Any) -> Request:
    return cast(Request, SimpleNamespace(state=SimpleNamespace(principal=principal)))


class _Names:
    """共通の表示名（`gone` は削除済みの利用者で、表示名を引けない）。"""

    def display_identities(self, user_uuids: list[str]) -> dict[str, UserIdentity]:
        return {
            uuid: UserIdentity(user_uuid=uuid, login_user_id=uuid, display_name=f"{uuid} さん")
            for uuid in user_uuids
            if uuid != "gone"
        }


ADMIN = _principal("admin", admin=True)
ALICE = _principal("alice")
# 製品の管理権限（フィードバック管理）を持っても、「AI 活用」では全員を見ない。
BOB = _principal("bob", permissions={FEEDBACK_MANAGE_PERMISSION})
PAGE = OffsetParams(limit=10, offset=0)


def _chat_turn(question: str, previous: str | None = None) -> JobCreateRequest:
    """今の画面が送るチャットのターン（生成と実行。#1176）。"""
    return _request().model_copy(
        update={"question": question, "chat": True, "previous_job_id": previous}
    )


@pytest.fixture
def chats(chat: ChatFixture, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:  # noqa: F811
    service = chat[0]
    monkeypatch.setattr(router, "nl2sql_service", service)
    monkeypatch.setattr(router, "get_security_service", lambda: _Names())
    ids: dict[str, str] = {}
    for owner in ("alice", "bob"):
        created = service.start_job(
            _chat_turn(f"{owner} の注文"), actor_user_uuid=owner, actor_is_system_admin=False
        )
        assert service.run_next_nl2sql_job(worker_id="visibility", job_id=created.job_id)
        ids[owner] = created.job_id
    return ids


def _send(service: Nl2SqlService, previous: str, principal: Any, question: str = "続き") -> str:
    """画面の route でチャットの続きを送り、worker で処理する。"""
    created = router.create_job(_chat_turn(question, previous), _http(principal)).data
    assert created is not None
    assert service.run_next_nl2sql_job(worker_id="visibility", job_id=created.job_id)
    return created.job_id


@contextmanager
def _before_3244(service: Nl2SqlService) -> Iterator[None]:
    """#3244 より前の記録を作る（system_admin が他の利用者の会話に送ったターン・実行）。

    今は持ち主だけが会話に足せるので、持ち主の判定を外して当時の記録を再現する。
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, "_assert_chat_owner_adds", lambda *args, **kwargs: None)
        yield


def _legacy_admin_turn(
    service: Nl2SqlService, previous: str, principal: Any = None, question: str = "続き"
) -> str:
    """#3244 より前に system_admin が他の利用者の会話に送ったターン。"""
    with _before_3244(service):
        return _send(service, previous, principal or ADMIN, question)


def _assert_others_content_forbidden(call: Callable[[], object]) -> None:
    with pytest.raises(HTTPException) as denied:
        call()
    assert denied.value.status_code == OTHERS_CONTENT_FORBIDDEN_STATUS == 403
    assert denied.value.detail == OTHERS_CONTENT_FORBIDDEN_MESSAGE


def test_chat_list_is_own_for_users_and_everyone_for_system_admin(chats: dict[str, str]) -> None:
    alice = router.list_sql_chats(_http(ALICE), PAGE).data
    assert alice is not None
    assert [item.id for item in alice.items] == [chats["alice"]]
    assert alice.items[0].owned and alice.items[0].actor_display_name == ""
    bob = router.list_sql_chats(_http(BOB), PAGE).data
    assert bob is not None and [item.id for item in bob.items] == [chats["bob"]]

    admin = router.list_sql_chats(_http(ADMIN), PAGE).data
    assert admin is not None
    assert {item.id for item in admin.items} == {chats["alice"], chats["bob"]}
    assert {item.actor_display_name for item in admin.items} == {"alice さん", "bob さん"}
    assert not any(item.owned for item in admin.items)
    # 持ち主の UUID は応答に出さない。
    assert "actor_user_uuid" not in admin.items[0].model_dump()


def test_chat_detail_and_operations_hide_other_users_chats(chats: dict[str, str]) -> None:
    other = chats["bob"]
    history_id = router.get_job(other, _http(BOB)).data.result.history_id  # type: ignore[union-attr]
    for call in (
        lambda: router.get_sql_chat(other, _http(ALICE)),
        lambda: router.rename_sql_chat(other, SqlChatUpdateRequest(title="x"), _http(ALICE)),
        lambda: router.delete_sql_chat(other, _http(ALICE)),
        lambda: router.get_job(other, _http(ALICE)),
        lambda: router.get_job_trace(other, _http(ALICE)),
        lambda: router.cancel_job(other, _http(ALICE)),
        lambda: router.execute_chat_turn(other, _http(ALICE)),
        lambda: router.take_chat_execution_result(other, _http(ALICE)),
        lambda: router.create_job(_chat_turn("続き", other), _http(ALICE)),
        lambda: router.feedback(
            FeedbackRequest(history_id=history_id, rating=FeedbackRating.GOOD), _http(ALICE)
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            call()
        assert denied.value.status_code == 404
    own = router.get_sql_chat(chats["alice"], _http(ALICE)).data
    assert own is not None and own.conversation.owned


def test_system_admin_cannot_continue_or_execute_other_users_chat(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system_admin は他の利用者の会話を見られるが、続きの質問・SQL の実行は 403（#3244）。

    system_admin の権限・接続（#904）で作った SQL・実行の結果が持ち主の会話に入らない。持ち主は
    今どおり続けられ、自分の接続で実行する。
    """
    service = chat[0]
    actors: list[tuple[str, bool]] = []
    execute = service.execute_sql

    def record(
        sql: str,
        allowed: AllowedObjects,
        row_limit: int | None,
        *,
        analysis: AnalyzeData | None = None,
    ) -> tuple[SafetyReport, str, QueryResults]:
        context = current_actor_context()
        assert context is not None
        actors.append((context.user_uuid, context.is_system_admin))
        return execute(sql, allowed, row_limit, analysis=analysis)

    monkeypatch.setattr(service, "execute_sql", record)
    alice_chat = chats["alice"]

    _assert_others_content_forbidden(
        lambda: router.create_job(_chat_turn("多い順にして", alice_chat), _http(ADMIN))
    )
    _assert_others_content_forbidden(lambda: router.execute_chat_turn(alice_chat, _http(ADMIN)))
    # ジョブも実行も作らない。
    assert actors == []
    data = router.get_sql_chat(alice_chat, _http(ADMIN)).data
    assert data is not None and [turn.job_id for turn in data.turns] == [alice_chat]
    assert not data.conversation.owned
    # 見ること（ジョブ・処理の経過）は今どおりできる。
    assert router.get_job(alice_chat, _http(ADMIN)).data is not None
    assert router.get_job_trace(alice_chat, _http(ADMIN)).data is not None
    # 見えない一般の利用者は 404（存在を漏らさない）。
    for call in (
        lambda: router.create_job(_chat_turn("続き", alice_chat), _http(BOB)),
        lambda: router.execute_chat_turn(alice_chat, _http(BOB)),
    ):
        with pytest.raises(HTTPException) as hidden:
            call()
        assert hidden.value.status_code == 404

    # 持ち主は今どおり続けて、自分の接続（#904）で実行する。
    alice_turn = _send(service, alice_chat, ALICE, "多い順にして")
    executed = router.execute_chat_turn(alice_turn, _http(ALICE)).data
    assert executed is not None and executed.status == "done"
    assert actors == [("alice", False), ("alice", False)]
    data = router.get_sql_chat(alice_chat, _http(ALICE)).data
    assert data is not None
    assert [turn.job_id for turn in data.turns] == [alice_chat, alice_turn]
    assert [turn.sender_display_name for turn in data.turns] == [None, None]
    # system_admin の自分の会話は今どおり続けられる。
    admin_chat = router.create_job(_chat_turn("admin の注文"), _http(ADMIN)).data
    assert admin_chat is not None
    assert service.run_next_nl2sql_job(worker_id="visibility", job_id=admin_chat.job_id)
    assert _send(service, admin_chat.job_id, ADMIN, "多い順にして")


def test_turns_sent_by_system_admin_before_3244_stay_visible_and_owner_only(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """#3244 より前に system_admin が送ったターンは消さず、送り手つきで持ち主にも見せる。

    そのターンの後に続けられ、そのターンの SQL を実行できるのは持ち主だけ（system_admin は 403）。
    結果の行は実行した人だけが受け取る。
    """
    service = chat[0]
    alice_chat = chats["alice"]
    admin_turn = _legacy_admin_turn(service, alice_chat, question="多い順にして")
    for viewer in (ALICE, ADMIN):
        data = router.get_sql_chat(alice_chat, _http(viewer)).data
        assert data is not None
        assert [turn.job_id for turn in data.turns] == [alice_chat, admin_turn]
        assert [turn.sender_display_name for turn in data.turns] == [None, "admin さん"]
        # 送り手の UUID は応答に出さない。
        assert "sender_user_uuid" not in data.turns[1].model_dump()
    # 会話の持ち主は変わらない。
    alice_list = router.list_sql_chats(_http(ALICE), PAGE).data
    assert alice_list is not None and [item.id for item in alice_list.items] == [alice_chat]
    # 当時の実行の結果の行は送った system_admin だけが受け取る（持ち主は「もう一度実行」）。
    with pytest.raises(HTTPException) as not_runner:
        router.take_chat_execution_result(admin_turn, _http(ALICE))
    assert not_runner.value.status_code == 404

    _assert_others_content_forbidden(
        lambda: router.create_job(_chat_turn("上位 5 件だけ", admin_turn), _http(ADMIN))
    )
    _assert_others_content_forbidden(lambda: router.execute_chat_turn(admin_turn, _http(ADMIN)))
    executed = router.execute_chat_turn(admin_turn, _http(ALICE)).data
    assert executed is not None and executed.status == "done"
    alice_turn = _send(service, admin_turn, ALICE, "上位 5 件だけ")
    data = router.get_sql_chat(alice_chat, _http(ALICE)).data
    assert data is not None
    assert [turn.job_id for turn in data.turns] == [alice_chat, admin_turn, alice_turn]
    assert [turn.sender_display_name for turn in data.turns] == [None, "admin さん", None]
    for previous in (alice_turn, admin_turn):
        with pytest.raises(HTTPException) as denied:
            router.create_job(_chat_turn("続き", previous), _http(BOB))
        assert denied.value.status_code == 404


def test_system_admin_cancels_but_cannot_answer_or_rate_other_users_chat(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """停止は system_admin もできる。回答前の確認と回答の評価は持ち主だけ（#3244）。"""
    service = chat[0]
    alice_chat = chats["alice"]

    # 回答前の確認（チャット）は会話の続きの一部: 持ち主だけが始められ、system_admin は 403、
    # 見えない一般の利用者は 404。
    def history(actor: str, *, admin: bool) -> list[dict[str, str]]:
        return service.chat_requirement_history(
            question="多い順にして",
            profile_id=_PROFILE_ID,
            previous_job_id=alice_chat,
            actor_user_uuid=actor,
            actor_is_system_admin=admin,
        )

    assert [turn["query"] for turn in history("alice", admin=False)] == ["alice の注文"]
    with pytest.raises(OthersContentForbiddenError):
        history("admin", admin=True)
    with pytest.raises(PermissionError):
        history("bob", admin=False)

    # 持ち主が送って処理中のターンは、system_admin も見て止められる。
    created = router.create_job(_chat_turn("多い順にして", alice_chat), _http(ALICE)).data
    assert created is not None
    pending = created.job_id
    assert router.get_job_trace(pending, _http(ADMIN)).data is not None
    hidden: tuple[Callable[[], object], ...] = (
        lambda: router.get_job(pending, _http(BOB)),
        lambda: router.cancel_job(pending, _http(BOB)),
    )
    for call in hidden:
        with pytest.raises(HTTPException) as denied:
            call()
        assert denied.value.status_code == 404
    cancelled = router.cancel_job(pending, _http(ADMIN)).data
    assert cancelled is not None and cancelled.job_id == pending
    assert router.cancel_job(pending, _http(ALICE)).data is not None

    # 持ち主の回答の評価は持ち主だけ。system_admin は 403 で、評価を書かない。
    alice_job = router.get_job(alice_chat, _http(ALICE)).data
    assert alice_job is not None and alice_job.result is not None
    history_id = alice_job.result.history_id
    good = FeedbackRequest(history_id=history_id, rating=FeedbackRating.GOOD)
    _assert_others_content_forbidden(lambda: router.feedback(good, _http(ADMIN)))
    stored = service._history_by_id(history_id)  # noqa: SLF001
    assert stored is not None and stored.feedback_rating is None
    saved = router.feedback(good, _http(ALICE)).data
    assert saved is not None and saved.saved
    stored = service._history_by_id(history_id)  # noqa: SLF001
    assert stored is not None
    assert stored.feedback_rating == FeedbackRating.GOOD
    assert stored.feedback_actor_user_uuid == "alice"
    assert stored.actor_user_uuid == "alice"


def test_system_admin_deletes_but_cannot_rename_other_users_chat(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """名前の変更は持ち主だけ（system_admin は 403）。削除は system_admin もできる（#3244）。"""
    other = chats["alice"]
    admin_turn = _legacy_admin_turn(chat[0], other)
    data = router.get_sql_chat(other, _http(ADMIN)).data
    assert data is not None
    assert not data.conversation.owned
    assert data.conversation.actor_display_name == "alice さん"

    _assert_others_content_forbidden(
        lambda: router.rename_sql_chat(other, SqlChatUpdateRequest(title="確認済み"), _http(ADMIN))
    )
    unchanged = router.get_sql_chat(other, _http(ALICE)).data
    assert unchanged is not None and unchanged.conversation.title == "alice の注文"
    renamed = router.rename_sql_chat(other, SqlChatUpdateRequest(title="確認済み"), _http(ALICE))
    assert renamed.data is not None and renamed.data.title == "確認済み"
    # 会話を消すと、#3244 より前に system_admin が送ったターンも会話から外れる。
    router.delete_sql_chat(other, _http(ADMIN))
    alice = router.list_sql_chats(_http(ALICE), PAGE).data
    assert alice is not None and alice.items == []
    with pytest.raises(HTTPException) as gone:
        router.create_job(_chat_turn("続き", admin_turn), _http(ALICE))
    assert gone.value.status_code == 400


def test_turn_sender_of_deleted_user_is_empty(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """表示名を引けない送り手（削除済みの利用者）は空（画面が「削除済みのユーザー」と出す）。"""
    gone = _principal("gone", admin=True)
    _legacy_admin_turn(chat[0], chats["alice"], gone)
    data = router.get_sql_chat(chats["alice"], _http(ALICE)).data
    assert data is not None
    assert [turn.sender_display_name for turn in data.turns] == [None, ""]


def _history_page(principal: Any, executor: str = "all") -> Any:
    page = router.history(_http(principal), PAGE, executor=executor).data  # type: ignore[arg-type]
    assert page is not None
    return page


def _result_history_id(service: Nl2SqlService, job_id: str) -> str:
    job = service.get_job(job_id, actor_user_uuid="admin")
    assert job is not None and job.result is not None and job.result.history_id
    return job.result.history_id


def test_history_shows_runs_to_executor_and_conversation_owner(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """実行履歴の行は、実行した人と会話の持ち主の両方に見える（system_admin は全員。#1954）。

    system_admin が alice の会話で実行した往復は alice の一覧にも出る（`executed_by_me=False`・
    実行した人の表示名）。bob には出ない。会話の無い実行（SQL 生成の画面）は実行した人だけ。
    """
    service = chat[0]
    admin_turn = _legacy_admin_turn(service, chats["alice"], question="多い順にして")
    standalone = router.create_job(_request(), _http(ADMIN)).data
    assert standalone is not None
    assert service.run_next_nl2sql_job(worker_id="visibility", job_id=standalone.job_id)
    from_admin_id = _result_history_id(service, admin_turn)
    standalone_id = _result_history_id(service, standalone.job_id)

    # 保存した行には、実行した人と会話の持ち主を残す（会話の無い実行は持ち主が空）。
    stored = service._history_by_id(from_admin_id)  # noqa: SLF001
    assert stored is not None
    assert (stored.actor_user_uuid, stored.conversation_owner_user_uuid) == ("admin", "alice")
    stored_standalone = service._history_by_id(standalone_id)  # noqa: SLF001
    assert stored_standalone is not None
    assert (stored_standalone.actor_user_uuid, stored_standalone.conversation_owner_user_uuid) == (
        "admin",
        "",
    )

    alice = _history_page(ALICE)
    assert alice.total == alice.unfiltered_total == 2
    by_id = {item.id: item for item in alice.items}
    assert standalone_id not in by_id
    from_admin = by_id[from_admin_id]
    assert from_admin.executed_by_me is False
    assert from_admin.executor_display_name == "admin さん"
    assert from_admin.conversation_owner_display_name == "alice さん"
    assert from_admin.owned_by_me is True
    # 管理者向けのログイン ID・利用者の列は一般の利用者に返さない。
    assert from_admin.actor_login_user_id == from_admin.actor_display_name == ""
    own = next(item for item in alice.items if item.id != from_admin_id)
    assert (own.executed_by_me, own.owned_by_me, own.executor_display_name) == (
        True,
        True,
        "alice さん",
    )

    # 「実行者」の絞り込み。件数（total）も同じ条件で、「全 M 件」は見られる範囲。
    mine = _history_page(ALICE, "mine")
    assert [item.id for item in mine.items] == [own.id]
    assert (mine.total, mine.unfiltered_total) == (1, 2)
    others = _history_page(ALICE, "others")
    assert [item.id for item in others.items] == [from_admin_id]
    assert (others.total, others.unfiltered_total) == (1, 2)

    # bob（別の一般の利用者）には alice の会話の往復も、system_admin の実行も出ない。
    bob = _history_page(BOB)
    assert bob.total == 1
    assert all(item.executed_by_me for item in bob.items)
    assert _history_page(BOB, "others").total == 0

    # system_admin は全員の行。会話の無い実行は持ち主が None。
    admin = _history_page(ADMIN)
    assert admin.total == 4
    admin_by_id = {item.id: item for item in admin.items}
    assert admin_by_id[from_admin_id].executed_by_me is True
    assert admin_by_id[from_admin_id].owned_by_me is False
    assert admin_by_id[from_admin_id].actor_display_name == "admin さん"
    assert admin_by_id[standalone_id].conversation_owner_display_name is None
    assert admin_by_id[standalone_id].owned_by_me is None
    assert _history_page(ADMIN, "mine").total == 2
    admin_others = _history_page(ADMIN, "others")
    assert {item.executor_display_name for item in admin_others.items} == {
        "alice さん",
        "bob さん",
    }
    assert (admin_others.total, admin_others.unfiltered_total) == (2, 4)


def test_job_origin_is_kept_in_the_snapshot_and_the_history_row(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """MCP の入口が決めたきっかけは、ジョブの snapshot（worker）を通って実行履歴の行に残る。

    #1986。画面のジョブ（きっかけを渡さない）は空のまま。
    """
    service = chat[0]
    origin = JobOrigin(run_source="evaluation", agent_run_id="run-eval-1")
    created = service.start_job(
        _request(), actor_user_uuid="admin", actor_is_system_admin=True, origin=origin
    )
    stored_job = service._jobs[created.job_id]  # noqa: SLF001
    snapshot = service._job_to_snapshot(stored_job)  # noqa: SLF001
    assert service._job_from_snapshot(snapshot).origin == origin  # noqa: SLF001
    # #1986 より前のジョブ（項目の無い snapshot）と知らない値は画面の実行とみなす。
    legacy = {key: value for key, value in snapshot.items() if not key.startswith("origin_")}
    assert service._job_from_snapshot(legacy).origin == JobOrigin()  # noqa: SLF001
    assert JobOrigin.from_snapshot({"origin_run_source": "admin"}) == JobOrigin()

    assert service.run_next_nl2sql_job(worker_id="origin", job_id=created.job_id)
    history_id = _result_history_id(service, created.job_id)
    row = service._history_by_id(history_id)  # noqa: SLF001
    assert row is not None
    assert (row.run_source, row.agent_run_id, row.api_key_name) == ("evaluation", "run-eval-1", "")
    screen = router.create_job(_request(), _http(ADMIN)).data
    assert screen is not None
    assert service.run_next_nl2sql_job(worker_id="origin", job_id=screen.job_id)
    screen_row = service._history_by_id(_result_history_id(service, screen.job_id))  # noqa: SLF001
    assert screen_row is not None
    assert screen_row.run_source == ""


def test_history_owner_sees_admin_execution_of_owner_turn(
    chat: ChatFixture,  # noqa: F811
    chats: dict[str, str],
) -> None:
    """持ち主のターンを system_admin が「もう一度実行」した記録も、持ち主の一覧に出る（#1954）。

    今は system_admin は他の利用者の会話で実行できない（#3244）ので、それより前の記録で確かめる。
    """
    service = chat[0]
    with _before_3244(service):
        executed = router.execute_chat_turn(chats["alice"], _http(ADMIN)).data
    assert executed is not None and executed.history_id
    alice = _history_page(ALICE, "others")
    assert [item.id for item in alice.items] == [executed.history_id]
    assert alice.items[0].executor_display_name == "admin さん"
    stored = service._history_by_id(executed.history_id)  # noqa: SLF001
    assert stored is not None and stored.actor_user_uuid == "admin"
    assert _history_page(BOB, "others").total == 0


def test_history_rejects_unknown_executor_filter(chats: dict[str, str]) -> None:
    with pytest.raises(HTTPException) as invalid:
        _history_page(ALICE, "everyone")
    assert invalid.value.status_code == 422


KEY_A = "key-alice"
KEY_ADMIN = "key-admin"


class _NamesAndKeys(_Names):
    """表示名と、見ている利用者が見られる API キーの名前（system_admin は全員、ほかは自分の分）。"""

    KEYS = {KEY_A: ("alice", "合成のキー A"), KEY_ADMIN: ("admin", "合成のキー 管理")}

    def api_key_names(self, actor: Any, key_ids: list[str]) -> dict[str, str]:
        return {
            key_id: name
            for key_id, (owner, name) in self.KEYS.items()
            if key_id in key_ids and (actor.is_system_admin or owner == actor.user_uuid)
        }


def _cancelled(index: int, **record: str) -> HistoryItem:
    return HistoryItem(
        id=f"cancelled-{index}",
        question="合成の質問",
        engine=Nl2SqlEngine.SELECT_AI,
        generated_sql="",
        created_at=f"2026-10-26T00:00:0{index}+00:00",
        profile_id=_PROFILE_ID,
        actor_user_uuid="alice",
        run_status="cancelled",
        **record,
    )


def test_history_cancellation_shows_who_and_the_key_visible_to_the_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止の行の「取消」（#3348）: 取り消した人と入口、見ている人が見られるキーの名前だけ。

    記録の値（取り消した人の UUID・キーの ID・Agent の Run の ID）は応答に出さない。
    """
    monkeypatch.setattr(router, "get_security_service", lambda: _NamesAndKeys())
    rows = [
        _cancelled(
            1, cancelled_by_user_uuid="alice", cancel_via="mcp", cancel_api_key_id="key-alice"
        ),
        _cancelled(
            2, cancelled_by_user_uuid="admin", cancel_via="mcp", cancel_api_key_id="key-admin"
        ),
        _cancelled(
            3, cancelled_by_user_uuid="alice", cancel_via="mcp", cancel_agent_run_id="run-1"
        ),
        _cancelled(4, cancelled_by_user_uuid="gone"),
        HistoryItem(
            id="done",
            question="合成の質問",
            engine=Nl2SqlEngine.SELECT_AI,
            generated_sql="SELECT 1 FROM DUAL",
            created_at="2026-10-26T00:00:00+00:00",
            actor_user_uuid="alice",
        ),
    ]

    def view(principal: Any) -> dict[str, Any]:
        items = router._with_history_people(  # noqa: SLF001 - 応答の補完の契約
            rows,
            viewer_user_uuid=principal.user_uuid,
            is_system_admin=principal.is_system_admin,
            principal=principal,
        )
        for item in items:
            assert (
                item.cancelled_by_user_uuid,
                item.cancel_via,
                item.cancel_api_key_id,
                item.cancel_agent_run_id,
            ) == ("", "", "", "")
        return {
            item.id: item.cancellation.model_dump() if item.cancellation else None for item in items
        }

    def cancel(via: str, name: str, by_me: bool, key: str = "") -> dict[str, Any]:
        return {
            "via": via,
            "actor_recorded": True,
            "actor_display_name": name,
            "by_me": by_me,
            "api_key_name": key,
        }

    assert view(ALICE) == {
        "cancelled-1": cancel("mcp", "alice さん", True, "合成のキー A"),
        # system_admin のキーは持ち主には見えない（「MCP」だけ）。
        "cancelled-2": cancel("mcp", "admin さん", False),
        "cancelled-3": cancel("agent_run", "alice さん", True),
        # 削除済みの利用者（表示名を引けない）は、記録はあるが名前が空。
        "cancelled-4": cancel("screen", "", False),
        "done": None,
    }
    admin = view(ADMIN)
    assert admin["cancelled-1"] == cancel("mcp", "alice さん", False, "合成のキー A")
    assert admin["cancelled-2"] == cancel("mcp", "admin さん", True, "合成のキー 管理")
