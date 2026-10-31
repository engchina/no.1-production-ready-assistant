"""DB 管理チャットの会話の履歴（#2319）: 保存・開き直し・続きの文脈・実行の要約・見え方。

見え方は「AI 活用」と同じ（#3244）: system_admin は他の利用者の会話を見て削除できるが、続きの質問・
実行・SQL の置き換え・エラーの分析・名前の変更は持ち主だけ（403）。

LLM（OCI Enterprise AI）と Oracle は決定論のスタブで置き換える（memory の保存先）。表・列の名前は
合成のもの。route の関数は画面の request の代わりに principal だけを持つ値で呼ぶ。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from fastapi import HTTPException, Request
from pr_backend_core.api import OffsetParams
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_CODE, UserIdentity
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)
from test_nl2sql_db_admin_chat import _catalog_service, _FakeAdapter, _FakeEnterpriseAi
from trace_span_fake import TraceTable, trace_table  # noqa: F401 - fixture

from app.features.nl2sql import router
from app.features.nl2sql.models import (
    DbAdminChatAnalyzeRequest,
    DbAdminChatGenerateRequest,
    DbAdminChatPlanUpdateRequest,
    DbAdminChatTurnExecuteRequest,
    SqlChatUpdateRequest,
)
from app.features.nl2sql.service import (
    DB_ADMIN_CHAT_TURN_LIMIT,
    DbAdminChatConversationFullError,
    Nl2SqlService,
)
from app.security.domain import Principal

PAGE = OffsetParams(limit=25, offset=0)


def _principal(user_uuid: str, *, admin: bool = False) -> Any:
    return Principal(
        user_uuid=user_uuid,
        login_user_id=user_uuid,
        display_name=f"{user_uuid} さん",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE] if admin else ["DBA"],
        permissions={"menu.db_admin_chat"},
        allowed_profile_ids=set(),
        session_id=f"session-{user_uuid}",
        csrf_token_hash="csrf",  # nosec B106 - テスト用
    )


def _http(principal: Any, *, disconnected: bool = False) -> Request:
    async def is_disconnected() -> bool:
        return disconnected

    return cast(
        Request,
        SimpleNamespace(
            state=SimpleNamespace(principal=principal), is_disconnected=is_disconnected
        ),
    )


class _Names:
    def display_identities(self, user_uuids: list[str]) -> dict[str, UserIdentity]:
        return {
            uuid_: UserIdentity(user_uuid=uuid_, login_user_id=uuid_, display_name=f"{uuid_} さん")
            for uuid_ in user_uuids
        }


ADMIN = _principal("admin", admin=True)
ALICE = _principal("alice")
BOB = _principal("bob")


def _answer(sql: str, explanation: str = "説明") -> dict[str, str]:
    return {"sql": sql, "explanation": explanation}


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi]:
    adapter = _FakeAdapter([{"index": 1, "statement_type": "DELETE", "status": "success"}])
    ai = _FakeEnterpriseAi([])
    service = _catalog_service(adapter, ai)
    monkeypatch.setattr(router, "nl2sql_service", service)
    monkeypatch.setattr(router, "get_security_service", lambda: _Names())
    return service, adapter, ai


def _send(principal: Any, question: str, conversation_id: str = "", turn_id: str = "") -> Any:
    data = router.db_admin_chat_generate(
        DbAdminChatGenerateRequest(
            question=question,
            conversation_id=conversation_id,
            client_turn_id=uuid.UUID(turn_id) if turn_id else uuid.uuid4(),
        ),
        _http(principal),
    ).data
    assert data is not None
    return data


def test_first_question_starts_a_conversation_and_is_reopened(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
    trace_table: TraceTable,  # noqa: F811
) -> None:
    _service, adapter, ai = setup
    ai.responses.append(_answer("SELECT QTY FROM APP.ITEM_STOCK", "在庫の数量です"))
    turn_id = str(uuid.uuid4())
    sent = _send(ALICE, "ITEM_STOCK の QTY を見る", turn_id=turn_id)
    # 会話の ID は最初のターンの ID。SQL は生成するだけで実行しない。
    assert sent.conversation.id == turn_id
    assert sent.conversation.title == "ITEM_STOCK の QTY を見る"
    assert sent.turn.plan.read_only is True
    assert sent.turn.context_tables == ["APP.ITEM_STOCK"]
    assert adapter.select_calls == [] and adapter.statement_calls == []

    listed = router.list_db_admin_chats(_http(ALICE), PAGE).data
    assert listed is not None
    assert [item.id for item in listed.items] == [turn_id]
    assert listed.total == 1 and listed.items[0].owned

    reopened = router.get_db_admin_chat(turn_id, _http(ALICE)).data
    assert reopened is not None
    [turn] = reopened.turns
    assert turn.question == "ITEM_STOCK の QTY を見る"
    assert turn.plan.sql == "SELECT QTY FROM APP.ITEM_STOCK"
    assert turn.explanation == "在庫の数量です"
    assert turn.statement_executions == [] and turn.last_run_all is None
    assert turn.sender_user_uuid is None
    # 生成したときの処理の経過（#3226 の案 A）: ターンには trace id を残し、span の表から読む。
    # 工程の詳細は処理の経過の API だけが返す（会話の応答には入れない）。
    assert turn.trace_id and turn.trace_id == sent.turn.trace_id
    assert turn.span_details == {}
    trace_table.flush()
    progress = router.get_db_admin_chat_turn_trace(turn_id, turn_id, _http(ALICE)).data
    assert progress is not None
    assert {event.target_id for event in progress.events} == {turn_id}
    assert progress.events[-1].type == "terminal"
    steps = [event for event in progress.events if event.type == "step" and event.kind]
    assert [event.kind for event in steps] == ["generate_sql", "db_admin_check"]
    # 既定では軌跡の本文を保存せず、会話の SQL と説明は上の検証どおり残る。
    assert all(event.details is None for event in steps)
    # 他の利用者の会話のターンは 404。
    with pytest.raises(HTTPException) as missing:
        router.get_db_admin_chat_turn_trace(turn_id, turn_id, _http(BOB))
    assert missing.value.status_code == 404


def test_resend_with_the_same_turn_id_does_not_generate_twice(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, _adapter, ai = setup
    ai.responses.append(_answer("SELECT 1 FROM DUAL"))
    turn_id = str(uuid.uuid4())
    first = _send(ALICE, "版", turn_id=turn_id)
    # 応答が届かずに送り直しても（会話の ID はまだ空のまま）、保存したターンを返す。
    again = _send(ALICE, "版", turn_id=turn_id)
    assert again.turn.turn_id == first.turn.turn_id
    assert len(ai.calls) == 1
    reopened = router.get_db_admin_chat(turn_id, _http(ALICE)).data
    assert reopened is not None and len(reopened.turns) == 1


def test_follow_up_uses_saved_turns_and_execution_outcome_as_context(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, adapter, ai = setup
    ai.responses.append(_answer("SELECT QTY FROM APP.ITEM_STOCK"))
    first = _send(ALICE, "ITEM_STOCK の数量")
    conversation_id = first.conversation.id
    executed = router.db_admin_chat_execute(
        DbAdminChatTurnExecuteRequest(
            conversation_id=conversation_id, turn_id=first.turn.turn_id, statement_index=1
        ),
        _http(ALICE),
    ).data
    assert executed is not None and executed.executed is True
    # 実行は画面から SQL を受け取らず、ターンに保存した SQL を使う。
    assert adapter.select_calls[-1][0] == "SELECT QTY FROM APP.ITEM_STOCK"

    ai.responses.append(_answer("DELETE FROM APP.ITEM_STOCK WHERE QTY = 0"))
    second = _send(ALICE, "そのうち 0 の行を消す", conversation_id=conversation_id)
    assert second.conversation.id == conversation_id
    context = ai.calls[-1]["context"]
    assert "ITEM_STOCK の数量" in context
    assert "SELECT QTY FROM APP.ITEM_STOCK" in context
    assert "成功（1 行）" in context
    reopened = router.get_db_admin_chat(conversation_id, _http(ALICE)).data
    assert reopened is not None
    assert [turn.question for turn in reopened.turns] == [
        "ITEM_STOCK の数量",
        "そのうち 0 の行を消す",
    ]


def test_stopped_generation_is_not_saved(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    service, _adapter, ai = setup

    def generate(disconnected: bool) -> Any:
        # FastAPI と同じく、sync の route を anyio の worker thread で動かす。
        return anyio.run(
            anyio.to_thread.run_sync,
            lambda: router.db_admin_chat_generate(
                DbAdminChatGenerateRequest(question="版", client_turn_id=uuid.uuid4()),
                _http(ALICE, disconnected=disconnected),
            ),
        )

    ai.responses.append(_answer("SELECT 1 FROM DUAL"))
    with pytest.raises(HTTPException) as stopped:
        generate(True)
    assert stopped.value.status_code == 409
    # 画面が待っている（接続が切れていない）ときは保存する。
    ai.responses.append(_answer("SELECT 1 FROM DUAL"))
    kept = generate(False).data
    assert kept is not None
    router.delete_db_admin_chat(kept.conversation.id, _http(ALICE))
    # 直に呼んだときも、待つのをやめたら保存しない。
    ai.responses.append(_answer("SELECT 1 FROM DUAL"))
    assert (
        service.send_db_admin_chat_question(
            DbAdminChatGenerateRequest(question="版", client_turn_id=uuid.uuid4()),
            actor="alice",
            all_users=False,
            keep_result=lambda: False,
        )
        is None
    )
    listed = router.list_db_admin_chats(_http(ALICE), PAGE).data
    assert listed is not None and listed.total == 0


def test_execution_summary_keeps_counts_but_not_rows(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, _adapter, ai = setup
    ai.responses.append(_answer("SELECT N FROM APP.ITEM_STOCK"))
    sent = _send(ALICE, "数量")
    router.db_admin_chat_execute(
        DbAdminChatTurnExecuteRequest(
            conversation_id=sent.conversation.id,
            turn_id=sent.turn.turn_id,
            statement_index=1,
            row_limit=50,
        ),
        _http(ALICE),
    )
    reopened = router.get_db_admin_chat(sent.conversation.id, _http(ALICE)).data
    assert reopened is not None
    [last] = reopened.turns[0].statement_executions
    assert last.status == "succeeded"
    assert (last.row_count, last.column_count, last.has_more) == (1, 1, False)
    assert last.confirmation_given is False
    assert last.outcome == "成功（1 行）"
    # 結果の行はチャットと同じく会話に残さない。
    assert last.result is not None and last.result.select_result is None


def test_mutation_waiting_for_confirmation_is_not_recorded_and_confirmed_is(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, adapter, ai = setup
    ai.responses.append(_answer("DELETE FROM APP.ITEM_STOCK WHERE QTY = 0"))
    sent = _send(ALICE, "0 の行を消す")
    request = DbAdminChatTurnExecuteRequest(
        conversation_id=sent.conversation.id, turn_id=sent.turn.turn_id, statement_index=1
    )
    pending = router.db_admin_chat_execute(request, _http(ALICE)).data
    assert pending is not None and pending.executed is False
    assert adapter.statement_calls == []
    reopened = router.get_db_admin_chat(sent.conversation.id, _http(ALICE)).data
    assert reopened is not None and reopened.turns[0].statement_executions == []

    confirmed = router.db_admin_chat_execute(
        request.model_copy(update={"confirmation": "ADMIN_EXECUTE"}), _http(ALICE)
    ).data
    assert confirmed is not None and confirmed.committed is True
    reopened = router.get_db_admin_chat(sent.conversation.id, _http(ALICE)).data
    assert reopened is not None
    [last] = reopened.turns[0].statement_executions
    assert last.confirmation_given is True and last.status == "succeeded"
    assert last.result is not None and last.result.committed is True


def test_corrected_sql_is_reclassified_and_resets_the_execution(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, _adapter, ai = setup
    ai.responses.append(_answer("SELECT QTY FROM APP.ITEM_STOCK"))
    sent = _send(ALICE, "数量")
    router.db_admin_chat_execute(
        DbAdminChatTurnExecuteRequest(
            conversation_id=sent.conversation.id, turn_id=sent.turn.turn_id, statement_index=1
        ),
        _http(ALICE),
    )
    corrected = router.replace_db_admin_chat_turn_sql(
        sent.conversation.id,
        sent.turn.turn_id,
        DbAdminChatPlanUpdateRequest(sql="DROP TABLE APP.ITEM_STOCK PURGE"),
        _http(ALICE),
    ).data
    assert corrected is not None
    assert corrected.revision == 1 and corrected.corrected is True
    assert corrected.statement_executions == []
    # サーバーが分類し直す（1 つの対象を消す DROP の確認語は対象の名前）。
    assert corrected.plan.requires_confirmation is True
    assert corrected.plan.confirmation_target == "APP.ITEM_STOCK"
    blocked = router.replace_db_admin_chat_turn_sql(
        sent.conversation.id,
        sent.turn.turn_id,
        DbAdminChatPlanUpdateRequest(sql="ALTER SYSTEM SET OPEN_CURSORS = 300"),
        _http(ALICE),
    ).data
    assert blocked is not None and blocked.plan.blocked is True and blocked.revision == 2


def test_other_users_conversations_are_404_and_system_admin_sees_all(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, _adapter, ai = setup
    ai.responses.extend([_answer("SELECT 1 FROM DUAL"), _answer("SELECT 2 FROM DUAL")])
    alice = _send(ALICE, "alice の質問")
    bob = _send(BOB, "bob の質問")
    other = bob.conversation.id
    turn_id = bob.turn.turn_id
    for call in (
        lambda: router.get_db_admin_chat(other, _http(ALICE)),
        lambda: router.rename_db_admin_chat(other, SqlChatUpdateRequest(title="x"), _http(ALICE)),
        lambda: router.delete_db_admin_chat(other, _http(ALICE)),
        lambda: router.db_admin_chat_execute(
            DbAdminChatTurnExecuteRequest(
                conversation_id=other, turn_id=turn_id, statement_index=1
            ),
            _http(ALICE),
        ),
        lambda: router.replace_db_admin_chat_turn_sql(
            other, turn_id, DbAdminChatPlanUpdateRequest(sql="SELECT 3 FROM DUAL"), _http(ALICE)
        ),
        lambda: _send(ALICE, "続き", conversation_id=other),
        # 消した・無い会話への続きの質問も同じ 404。
        lambda: _send(ALICE, "続き", conversation_id="missing-conversation"),
    ):
        with pytest.raises(HTTPException) as hidden:
            call()
        assert hidden.value.status_code == 404
    mine = router.list_db_admin_chats(_http(ALICE), PAGE).data
    assert mine is not None and [item.id for item in mine.items] == [alice.conversation.id]
    assert mine.items[0].actor_display_name == ""

    everyone = router.list_db_admin_chats(_http(ADMIN), PAGE).data
    assert everyone is not None
    assert {item.id for item in everyone.items} == {alice.conversation.id, other}
    assert {item.actor_display_name for item in everyone.items} == {"alice さん", "bob さん"}
    assert not any(item.owned for item in everyone.items)
    assert "actor_user_uuid" not in everyone.items[0].model_dump()


def test_system_admin_views_and_deletes_but_cannot_add_to_other_users_conversation(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    """system_admin は他の利用者の会話を見て削除できる。内容を足す・変える操作は 403（#3244）。"""
    service, _adapter, ai = setup
    ai.responses.extend([_answer("SELECT 1 FROM DUAL"), _answer("SELECT 2 FROM DUAL")])
    bob = _send(BOB, "bob の質問")
    conversation_id = bob.conversation.id
    turn_id = bob.turn.turn_id
    viewed = router.get_db_admin_chat(conversation_id, _http(ADMIN)).data
    assert viewed is not None and viewed.conversation.owned is False
    assert viewed.conversation.actor_display_name == "bob さん"
    for call in (
        lambda: _send(ADMIN, "管理者の続き", conversation_id=conversation_id),
        lambda: router.db_admin_chat_execute(
            DbAdminChatTurnExecuteRequest(
                conversation_id=conversation_id, turn_id=turn_id, statement_index=1
            ),
            _http(ADMIN),
        ),
        lambda: router.replace_db_admin_chat_turn_sql(
            conversation_id,
            turn_id,
            DbAdminChatPlanUpdateRequest(sql="SELECT 3 FROM DUAL"),
            _http(ADMIN),
        ),
        lambda: router.db_admin_chat_analyze_error(
            conversation_id, turn_id, DbAdminChatAnalyzeRequest(statement_index=1), _http(ADMIN)
        ),
        lambda: router.rename_db_admin_chat(
            conversation_id, SqlChatUpdateRequest(title="x"), _http(ADMIN)
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            call()
        assert denied.value.status_code == OTHERS_CONTENT_FORBIDDEN_STATUS == 403
        assert denied.value.detail == OTHERS_CONTENT_FORBIDDEN_MESSAGE
    # 何も足していない（ターン・実行の要約・名前・SQL はそのまま）。
    owner_view = router.get_db_admin_chat(conversation_id, _http(BOB)).data
    assert owner_view is not None and owner_view.conversation.owned is True
    assert owner_view.conversation.title == "bob の質問"
    [turn] = owner_view.turns
    assert turn.statement_executions == [] and turn.plan.sql == "SELECT 1 FROM DUAL"

    # #3244 より前に system_admin が送ったターンは消さず、送り手つきで持ち主にも見せる。実行できる
    # のは持ち主だけ。
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            service, "_db_admin_chat_owned_conversation", service._db_admin_chat_conversation
        )
        legacy = _send(ADMIN, "管理者の続き", conversation_id=conversation_id)
    owner_view = router.get_db_admin_chat(conversation_id, _http(BOB)).data
    assert owner_view is not None
    assert [item.sender_display_name for item in owner_view.turns] == [None, "admin さん"]
    legacy_execute = DbAdminChatTurnExecuteRequest(
        conversation_id=conversation_id, turn_id=legacy.turn.turn_id, statement_index=1
    )
    with pytest.raises(HTTPException) as denied:
        router.db_admin_chat_execute(legacy_execute, _http(ADMIN))
    assert denied.value.status_code == 403
    executed = router.db_admin_chat_execute(legacy_execute, _http(BOB)).data
    assert executed is not None and executed.executed is True

    # 削除は system_admin もできる。
    router.delete_db_admin_chat(conversation_id, _http(ADMIN))
    listed = router.list_db_admin_chats(_http(BOB), PAGE).data
    assert listed is not None and listed.total == 0


def test_rename_and_delete_conversation(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, _adapter, ai = setup
    ai.responses.append(_answer("SELECT 1 FROM DUAL"))
    sent = _send(ALICE, "版")
    renamed = router.rename_db_admin_chat(
        sent.conversation.id, SqlChatUpdateRequest(title="  新しい名前  "), _http(ALICE)
    ).data
    assert renamed is not None and renamed.title == "新しい名前"
    router.delete_db_admin_chat(sent.conversation.id, _http(ALICE))
    with pytest.raises(HTTPException) as gone:
        router.get_db_admin_chat(sent.conversation.id, _http(ALICE))
    assert gone.value.status_code == 404
    listed = router.list_db_admin_chats(_http(ALICE), PAGE).data
    assert listed is not None and listed.total == 0


def test_conversation_turn_limit(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _adapter, ai = setup
    monkeypatch.setattr("app.features.nl2sql.service.DB_ADMIN_CHAT_TURN_LIMIT", 2)
    ai.responses.extend([_answer("SELECT 1 FROM DUAL"), _answer("SELECT 2 FROM DUAL")])
    first = _send(ALICE, "1")
    _send(ALICE, "2", conversation_id=first.conversation.id)
    with pytest.raises(DbAdminChatConversationFullError):
        service.send_db_admin_chat_question(
            DbAdminChatGenerateRequest(
                question="3", conversation_id=first.conversation.id, client_turn_id=uuid.uuid4()
            ),
            actor="alice",
            all_users=False,
        )
    with pytest.raises(HTTPException) as full:
        _send(ALICE, "3", conversation_id=first.conversation.id)
    assert full.value.status_code == 409
    assert DB_ADMIN_CHAT_TURN_LIMIT == 50


def test_history_pages_are_newest_first(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, _adapter, ai = setup
    ai.responses.extend([_answer(f"SELECT {index} FROM DUAL") for index in range(3)])
    ids = [_send(ALICE, f"質問 {index}").conversation.id for index in range(3)]
    first = router.list_db_admin_chats(_http(ALICE), OffsetParams(limit=2, offset=0)).data
    second = router.list_db_admin_chats(_http(ALICE), OffsetParams(limit=2, offset=2)).data
    assert first is not None and second is not None
    assert [item.id for item in first.items] == [ids[2], ids[1]]
    assert [item.id for item in second.items] == [ids[0]]
    assert first.total == second.total == 3


def test_multiple_statements_are_saved_per_statement_and_reopened(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, adapter, ai = setup
    ai.responses.append(_answer("SELECT 1 AS N FROM DUAL;\nSELECT 2 AS N FROM DUAL;"))
    sent = _send(ALICE, "版と表領域")
    # 複数の SELECT も実行できる（文ごとの分類。#2333）。
    assert sent.turn.plan.blocked is False
    assert [item.index for item in sent.turn.plan.statements] == [1, 2]
    executed = router.db_admin_chat_execute(
        DbAdminChatTurnExecuteRequest(
            conversation_id=sent.conversation.id, turn_id=sent.turn.turn_id, mode="all"
        ),
        _http(ALICE),
    ).data
    assert executed is not None
    assert [item.status for item in executed.statements] == ["succeeded", "succeeded"]
    assert len(adapter.script_calls) == 1
    reopened = router.get_db_admin_chat(sent.conversation.id, _http(ALICE)).data
    assert reopened is not None
    [turn] = reopened.turns
    assert [(item.index, item.status, item.mode) for item in turn.statement_executions] == [
        (1, "succeeded", "all"),
        (2, "succeeded", "all"),
    ]
    assert all(item.batch_id == executed.batch_id for item in turn.statement_executions)
    assert turn.last_run_all is not None and turn.last_run_all.succeeded is True

    # 続きの質問の文脈に、文ごとの結果を渡す。
    ai.responses.append(_answer("SELECT 3 FROM DUAL"))
    _send(ALICE, "続き", conversation_id=sent.conversation.id)
    context = ai.calls[-1]["context"]
    assert "#1: 成功（1 行）" in context and "#2: 成功（1 行）" in context


def test_run_all_with_a_forbidden_statement_and_bad_index_are_400(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    _service, adapter, ai = setup
    ai.responses.append(_answer("SELECT 1 FROM DUAL; ALTER SYSTEM SET OPEN_CURSORS = 300"))
    sent = _send(ALICE, "設定")
    with pytest.raises(HTTPException) as refused:
        router.db_admin_chat_execute(
            DbAdminChatTurnExecuteRequest(
                conversation_id=sent.conversation.id, turn_id=sent.turn.turn_id, mode="all"
            ),
            _http(ALICE),
        )
    assert refused.value.status_code == 400
    assert "すべて実行できません" in str(refused.value.detail)
    assert adapter.script_calls == []
    with pytest.raises(HTTPException) as missing:
        router.replace_db_admin_chat_turn_sql(
            sent.conversation.id,
            sent.turn.turn_id,
            DbAdminChatPlanUpdateRequest(sql="SELECT 2 FROM DUAL", statement_index=5),
            _http(ALICE),
        )
    assert missing.value.status_code == 400
    replaced = router.replace_db_admin_chat_turn_sql(
        sent.conversation.id,
        sent.turn.turn_id,
        DbAdminChatPlanUpdateRequest(sql="SELECT 2 FROM DUAL", statement_index=2),
        _http(ALICE),
    ).data
    assert replaced is not None and replaced.plan.blocked is False
    assert [item.sql for item in replaced.plan.statements] == [
        "SELECT 1 FROM DUAL",
        "SELECT 2 FROM DUAL",
    ]


def test_db_admin_chat_never_appears_in_the_run_history(
    setup: tuple[Nl2SqlService, _FakeAdapter, _FakeEnterpriseAi],
) -> None:
    """DB 管理チャットの生成・実行は「実行履歴」に行を作らない（会話と監査にだけ残す）。"""
    service, _adapter, ai = setup
    ai.responses.append(
        _answer("SELECT 1 AS N FROM DUAL; DELETE FROM APP.ITEM_STOCK WHERE QTY = 0")
    )
    sent = _send(ALICE, "数量と削除")
    conversation_id, turn_id = sent.conversation.id, sent.turn.turn_id
    router.db_admin_chat_execute(
        DbAdminChatTurnExecuteRequest(
            conversation_id=conversation_id, turn_id=turn_id, statement_index=1
        ),
        _http(ALICE),
    )
    router.db_admin_chat_execute(
        DbAdminChatTurnExecuteRequest(
            conversation_id=conversation_id,
            turn_id=turn_id,
            mode="all",
            confirmation="ADMIN_EXECUTE",
        ),
        _http(ALICE),
    )
    for kind in ("all", "not_evaluation", "chat", "query"):
        listed = service.list_history(all_users=True, kind=kind)
        assert (listed.total, listed.unfiltered_total, listed.items) == (0, 0, [])
    page = router.history(_http(ADMIN), PAGE, kind="all").data
    assert page is not None and page.total == 0 and page.items == []
    # 会話と監査には残る。
    assert service._admin_audit_store.list_recent()
    reopened = router.get_db_admin_chat(conversation_id, _http(ALICE)).data
    assert reopened is not None and reopened.turns[0].statement_executions
