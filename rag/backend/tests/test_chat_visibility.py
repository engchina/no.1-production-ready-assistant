"""「AI 活用」のチャットの会話の見え方（#1889・#3244）。

system_admin は全員の会話（利用者の表示名つき）を見て、運用の操作（停止・購読し直し・削除）が
できる。続きの質問（確認への回答・添付を含む）・名前の変更・回答の評価は会話の持ち主だけで、
system_admin でも他の利用者の会話には 403。それ以外の利用者は自分の会話だけ（ほかの利用者の会話は
404）。#3244 より前に system_admin が送った質問は消さず、送った人の表示名を添えて出す。
SQL の条件（`_oracle_conversation_where`）と、条件どおりに絞る fake の上の API を確かめる。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pr_system_settings.auth.domain import CONFIGURED_SYSTEM_ADMIN_USER_UUID
from pr_system_settings.auth.visibility import OTHERS_CONTENT_FORBIDDEN_MESSAGE
from pr_system_settings.chat_attachments import ChatAttachmentService, InMemoryChatAttachmentStore
from pytest import MonkeyPatch

from app.api.routes import chat as chat_route
from app.clients import oracle as oracle_module
from app.clients.oracle import (
    StoredConversation,
    StoredMessage,
    _oracle_conversation_access_predicate_sql,
    _oracle_conversation_where,
    _with_conversation_access_bind,
)
from app.main import app
from app.rag import chat_attachments
from app.rag.request_context import (
    AuditRequestContext,
    caller_owns,
    current_audit_request_context,
    reset_audit_request_context,
    set_audit_request_context,
    user_id_hash_for,
)
from app.security.permissions import MENU_CHAT
from app.security.service import set_security_service
from app.security.user_names import display_names_by_user_hash
from tests.security_support import (
    ProductionAuth,
    enable_production_auth,
    login,
    login_configured_admin,
)
from tests.support import AsgiTestClient
from tests.test_chat_api import FakeChatOracle, _stub_stream

client = AsgiTestClient(app)


class OwnedChatOracle(FakeChatOracle):
    """作った利用者を記録し、SQL の会話の条件（system_admin は全員）どおりに絞る fake。"""

    # 持ち主は作った利用者（持ち主の分からない会話もそのまま確かめる）。
    assign_caller_as_owner = False

    def _visible(self, conversation: StoredConversation) -> bool:
        # SQL の条件と同じく、利用者の分からない context には何も見せない（#3250）。
        context = current_audit_request_context()
        return context.all_users_visible or (
            context.user_id_hash is not None and conversation.user_id_hash == context.user_id_hash
        )

    async def create_conversation(
        self, *, search_answer_profile_id: str, title: str | None = None
    ) -> StoredConversation:
        conversation = await super().create_conversation(
            search_answer_profile_id=search_answer_profile_id, title=title
        )
        conversation.user_id_hash = current_audit_request_context().user_id_hash
        return conversation

    async def append_message(self, message: StoredMessage) -> StoredMessage:
        # 発話の利用者は送った人（OracleClient と同じく request の context から）。
        if await self.get_conversation(message.conversation_id) is None:
            raise KeyError(message.conversation_id)
        stored = await super().append_message(message)
        stored.user_id_hash = current_audit_request_context().user_id_hash
        return stored

    async def list_conversations(
        self,
        *,
        search_answer_profile_id: str | None = None,
        updated_since: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[StoredConversation]:
        items = await super().list_conversations(
            search_answer_profile_id=search_answer_profile_id, updated_since=updated_since
        )
        visible = [item for item in items if self._visible(item)]
        return visible[offset : (offset + limit) if limit is not None else None]

    async def get_conversation(self, conversation_id: str) -> StoredConversation | None:
        conversation = await super().get_conversation(conversation_id)
        return conversation if conversation is not None and self._visible(conversation) else None

    async def delete_conversation(self, conversation_id: str) -> None:
        if await self.get_conversation(conversation_id) is None:
            raise KeyError(conversation_id)
        await super().delete_conversation(conversation_id)

    async def rename_conversation(self, conversation_id: str, title: str) -> StoredConversation:
        if await self.get_conversation(conversation_id) is None:
            raise KeyError(conversation_id)
        return await super().rename_conversation(conversation_id, title)


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


@pytest.fixture
def oracle(monkeypatch: MonkeyPatch) -> OwnedChatOracle:
    fake = OwnedChatOracle()
    monkeypatch.setattr(chat_route, "OracleClient", lambda *a, **k: fake)
    return fake


@pytest.fixture
def attachment_service() -> Iterator[ChatAttachmentService]:
    created = ChatAttachmentService(
        InMemoryChatAttachmentStore(), product="rag", settings=lambda: SimpleNamespace()
    )
    chat_attachments.set_chat_attachment_service(created)
    yield created
    chat_attachments.set_chat_attachment_service(None)


def _user(auth: ProductionAuth, login_user_id: str, display_name: str) -> dict[str, str]:
    user = auth.user_with_permissions(
        login_user_id, [MENU_CHAT], search_answer_profile_ids=["bv-1"]
    )
    auth.store.users[user.user_uuid].display_name = display_name
    return login(client, login_user_id)


def test_conversation_sql_drops_owner_predicate_only_for_system_admin() -> None:
    """会話を読む条件: system_admin（`all_users_visible`）は持ち主で絞らない（閲覧と運用。#1889）。

    発話の追加・名前の変更・評価は、この条件で読んだ後に route が持ち主で 403 にする（#3244）。
    """
    base = AuditRequestContext(tenant_id_hash="tenant", user_id_hash="admin-hash")
    token = set_audit_request_context(replace(base, all_users_visible=True))
    try:
        sql, binds = _oracle_conversation_where()
        assert "user_id_hash" not in sql
        assert "conversation_user_id_hash" not in binds
        assert "user_id_hash" not in _oracle_conversation_access_predicate_sql(alias="c")
        assert "conversation_user_id_hash" not in _with_conversation_access_bind({})
    finally:
        reset_audit_request_context(token)
    token = set_audit_request_context(base)
    try:
        sql, binds = _oracle_conversation_where()
        assert "c.user_id_hash = :conversation_user_id_hash" in sql
        assert binds["conversation_user_id_hash"] == "admin-hash"
        access_sql = _oracle_conversation_access_predicate_sql()
        assert "user_id_hash = :conversation_user_id_hash" in access_sql
        assert _with_conversation_access_bind({})["conversation_user_id_hash"] == "admin-hash"
    finally:
        reset_audit_request_context(token)


@pytest.mark.parametrize(
    "context",
    [None, AuditRequestContext(tenant_id_hash="tenant"), AuditRequestContext()],
    ids=["no-context", "context-without-user", "system-access"],
)
def test_conversation_sql_without_a_user_returns_nothing(
    context: AuditRequestContext | None,
) -> None:
    """利用者の分からない context は、全員の会話ではなく 0 件（fail-closed。#3250）。

    文脈の無い処理（`NO_ACCESS_AUDIT_REQUEST_CONTEXT`）・利用者の無い context・範囲の制限の無い
    信頼した処理（`system_access_context`）のどれも、持ち主の条件を外さない。
    """
    token = set_audit_request_context(context) if context is not None else None
    try:
        sql, binds = _oracle_conversation_where()
        access_sql = _oracle_conversation_access_predicate_sql()
        conversation_binds = _with_conversation_access_bind({})
    finally:
        if token is not None:
            reset_audit_request_context(token)
    assert "1 = 0" in sql
    assert "1 = 0" in access_sql
    assert "conversation_user_id_hash" not in binds
    assert "conversation_user_id_hash" not in conversation_binds


def test_caller_without_a_user_owns_no_conversation() -> None:
    """利用者の分からない context は、どの会話の持ち主でもない（内容を足せない。#3250）。"""
    assert not caller_owns("owner-hash")
    assert not caller_owns(None)
    token = set_audit_request_context(AuditRequestContext(user_id_hash="owner-hash"))
    try:
        assert caller_owns("owner-hash")
        assert not caller_owns("other-hash")
        assert not caller_owns(None)
    finally:
        reset_audit_request_context(token)


def test_users_see_own_conversations_and_system_admin_sees_everyone(
    auth: ProductionAuth, oracle: OwnedChatOracle
) -> None:
    alice = _user(auth, "chat-alice", "アリス")
    bob = _user(auth, "chat-bob", "ボブ")
    created = client.post(
        "/api/chat/conversations", json={"search_answer_profile_id": "bv-1"}, headers=alice
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["data"]["id"]
    assert created.json()["data"]["owned"] is True

    # ほかの利用者には一覧に出さず、詳細・名前の変更・削除・送信は 404。
    listed = client.get("/api/chat/conversations", headers=bob).json()["data"]["items"]
    assert conversation_id not in {item["id"] for item in listed}
    path = f"/api/chat/conversations/{conversation_id}"
    assert client.get(path, headers=bob).status_code == 404
    assert client.patch(path, json={"title": "x"}, headers=bob).status_code == 404
    assert client.delete(path, headers=bob).status_code == 404
    sent = client.post(f"{path}/messages/stream", json={"content": "続き"}, headers=bob)
    assert sent.status_code == 404

    # 本人には利用者の表示名を出さない。
    own = client.get(path, headers=alice).json()["data"]
    assert (own["owned"], own["actor_display_name"]) == (True, "")

    # 持ち主は名前を変えられる。
    renamed = client.patch(path, json={"title": "確認済み"}, headers=alice)
    assert renamed.status_code == 200, renamed.text

    # system_admin は全員の会話を利用者の表示名つきで見て、削除できる。
    # 名前の変更は持ち主だけ（#3244）。
    admin = login_configured_admin(client)
    items = client.get("/api/chat/conversations", headers=admin).json()["data"]["items"]
    [item] = [entry for entry in items if entry["id"] == conversation_id]
    assert (item["owned"], item["actor_display_name"]) == (False, "アリス")
    detail = client.get(path, headers=admin).json()["data"]
    assert (detail["owned"], detail["actor_display_name"]) == (False, "アリス")
    refused = client.patch(path, json={"title": "管理者の名前"}, headers=admin)
    assert refused.status_code == 403
    assert refused.json()["error_messages"] == [OTHERS_CONTENT_FORBIDDEN_MESSAGE]
    assert oracle.conversations[conversation_id].title == "確認済み"
    assert client.delete(path, headers=admin).status_code == 200


def test_system_admin_cannot_continue_another_users_conversation(
    auth: ProductionAuth,
    oracle: OwnedChatOracle,
    monkeypatch: MonkeyPatch,
    attachment_service: ChatAttachmentService,
) -> None:
    """system_admin は他の利用者の会話に続きの質問・添付を足せない（403。#3244）。

    system_admin の権限・対象範囲で作った回答と引用が持ち主の会話に入るのを防ぐ。閲覧・停止・
    購読し直しはできる。持ち主は今どおり送れる。ほかの一般の利用者は 404 のまま。
    """
    _stub_stream(monkeypatch, oracle, ["m1"])
    alice = _user(auth, "send-alice", "アリス")
    bob = _user(auth, "send-bob", "ボブ")
    created = client.post(
        "/api/chat/conversations", json={"search_answer_profile_id": "bv-1"}, headers=alice
    ).json()["data"]
    path = f"/api/chat/conversations/{created['id']}"
    own = client.post(f"{path}/messages/stream", json={"content": "質問 A"}, headers=alice)
    assert own.status_code == 200, own.text
    assert "event: all_done" in own.text
    messages = oracle.messages[created["id"]]
    before = [(item.role, item.content) for item in messages]
    assert before == [("USER", "質問 A"), ("ASSISTANT", "回答")]

    admin = login_configured_admin(client)
    # 続きの質問（確認への回答も同じ API）は 403 で、発話も回答も足さない。
    sent = client.post(f"{path}/messages/stream", json={"content": "続きの質問"}, headers=admin)
    assert sent.status_code == 403, sent.text
    assert sent.json()["error_messages"] == [OTHERS_CONTENT_FORBIDDEN_MESSAGE]
    clarified = client.post(
        f"{path}/messages/stream",
        json={"content": "質問 A", "clarification_skipped_rule_id": "rule-1"},
        headers=admin,
    )
    assert clarified.status_code == 403
    # 会話への添付も内容を足す操作なので 403（会話の持ち主の添付にしない）。
    monkeypatch.setattr(oracle_module, "OracleClient", lambda *a, **k: oracle)
    attached = client.post(
        "/api/chat-attachments",
        headers=admin,
        data={"conversation_id": created["id"]},
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert attached.status_code == 403, attached.text
    # 会話の添付の一覧は閲覧なので見られる。
    listed = client.get(
        "/api/chat-attachments", params={"conversation_id": created["id"]}, headers=admin
    )
    assert listed.status_code == 200, listed.text
    pending = attachment_service.store.list_pending(
        "rag", CONFIGURED_SYSTEM_ADMIN_USER_UUID, now=datetime.now(UTC)
    )
    assert pending == []
    assert [(item.role, item.content) for item in oracle.messages[created["id"]]] == before

    # 閲覧と運用の操作（購読し直し・停止）はできる。
    admin_view = client.get(path, headers=admin)
    assert admin_view.status_code == 200
    assert admin_view.json()["data"]["owned"] is False
    question_id = messages[0].id
    resumed = client.get(f"{path}/messages/{question_id}/stream", headers=admin)
    assert resumed.status_code == 200, resumed.text
    assert "event: all_done" in resumed.text
    stopped = client.post(f"{path}/messages/{question_id}/cancel", headers=admin)
    assert stopped.status_code == 200, stopped.text

    # 持ち主は今どおり続きを送れる。
    again = client.post(f"{path}/messages/stream", json={"content": "質問 B"}, headers=alice)
    assert again.status_code == 200, again.text
    assert [item.content for item in oracle.messages[created["id"]]][-2:] == ["質問 B", "回答"]

    # 一般の利用者は他人の会話に送れず、停止・購読し直しも 404（存在を漏らさない）。
    assert (
        client.post(f"{path}/messages/stream", json={"content": "割り込み"}, headers=bob)
    ).status_code == 404
    assert (client.post(f"{path}/messages/{question_id}/cancel", headers=bob)).status_code == 404
    assert (client.get(f"{path}/messages/{question_id}/stream", headers=bob)).status_code == 404


def test_ownerless_conversation_is_read_only_even_for_system_admin(
    auth: ProductionAuth, oracle: OwnedChatOracle, monkeypatch: MonkeyPatch
) -> None:
    """持ち主の分からない古い会話（`user_id_hash` が NULL）には誰も足せない（#3244）。

    platform の `can_add_user_content` と同じ規則。system_admin には閲覧と削除だけの会話として出し、
    続きの質問・名前の変更は 403。一般の利用者には見えない（404）。
    """
    _stub_stream(monkeypatch, oracle, ["m1"])
    alice = _user(auth, "ownerless-alice", "アリス")
    created = client.post(
        "/api/chat/conversations", json={"search_answer_profile_id": "bv-1"}, headers=alice
    ).json()["data"]
    oracle.conversations[created["id"]].user_id_hash = None
    path = f"/api/chat/conversations/{created['id']}"
    assert client.get(path, headers=alice).status_code == 404

    admin = login_configured_admin(client)
    detail = client.get(path, headers=admin)
    assert detail.status_code == 200
    assert detail.json()["data"]["owned"] is False
    sent = client.post(f"{path}/messages/stream", json={"content": "続き"}, headers=admin)
    assert sent.status_code == 403, sent.text
    renamed = client.patch(path, json={"title": "x"}, headers=admin)
    assert renamed.status_code == 403
    assert oracle.messages.get(created["id"], []) == []
    assert client.delete(path, headers=admin).status_code == 200


def test_earlier_system_admin_questions_keep_the_sender_name(
    auth: ProductionAuth, oracle: OwnedChatOracle, monkeypatch: MonkeyPatch
) -> None:
    """#3244 より前に system_admin が送った質問は消さず、送った人の表示名を添えて出す。"""
    _stub_stream(monkeypatch, oracle, ["m1"])
    alice = _user(auth, "earlier-alice", "アリス")
    created = client.post(
        "/api/chat/conversations", json={"search_answer_profile_id": "bv-1"}, headers=alice
    ).json()["data"]
    path = f"/api/chat/conversations/{created['id']}"
    assert (
        client.post(f"{path}/messages/stream", json={"content": "質問 A"}, headers=alice)
    ).status_code == 200
    # 以前の system_admin の発話（送った人の hash で保存した行）を合成する。
    admin_hash = user_id_hash_for(CONFIGURED_SYSTEM_ADMIN_USER_UUID)
    earlier = oracle.messages[created["id"]][0]
    oracle.messages[created["id"]].append(
        replace(earlier, id="earlier-admin-question", content="以前の質問", user_id_hash=admin_hash)
    )

    detail = client.get(path, headers=alice).json()["data"]
    assert detail["owned"] is True
    assert [
        (item["role"], item["sent_by_owner"], item["sender_display_name"])
        for item in detail["messages"]
    ] == [
        ("USER", True, ""),
        ("ASSISTANT", True, ""),
        ("USER", False, "システム管理者"),
    ]
    admin = login_configured_admin(client)
    admin_view = client.get(path, headers=admin).json()["data"]
    assert admin_view["owned"] is False
    assert admin_view["messages"][2]["sender_display_name"] == "システム管理者"


def test_owner_names_are_resolved_from_hashes(auth: ProductionAuth) -> None:
    """会話は利用者を hash でしか持たないので、利用者の UUID を同じ hash にして表示名を引く。"""
    user = auth.user_with_permissions("hash-user", [MENU_CHAT])
    auth.store.users[user.user_uuid].display_name = "ハッシュ 太郎"
    owner_hash = user_id_hash_for(user.user_uuid)
    local_hash = user_id_hash_for("local-user")
    assert owner_hash is not None and local_hash is not None
    names = display_names_by_user_hash({owner_hash, local_hash, "missing-hash"})
    assert names == {owner_hash: "ハッシュ 太郎", local_hash: "ローカル利用者"}
    assert display_names_by_user_hash(set()) == {}
