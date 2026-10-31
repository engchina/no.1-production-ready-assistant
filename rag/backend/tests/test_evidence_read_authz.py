"""根拠を読む入口の認可の行列（入口 × 主体 × 範囲。OWASP API1 / BOLA。#3252）。

入口ごとの詳しいテストは各 module（``test_source_preview``・``test_figure_url``・
``test_mcp_profile_scope``・``test_chat_visibility``・``test_run_history`` など）にあり、ここでは
「どの入口を、どの主体と範囲で呼ぶと、何が返るか」を 1 か所の表で固定する。新しい入口・経路で判定が
抜けたら、この表の行が落ちる。

- 保存した根拠の再表示（チャットの会話の取得・実行履歴の詳細・保存された回答の詳細）: 見る人の
  今の権限で文書を確かめ直し、見られない根拠は抜粋・文書を返さず件数だけ（判断 A）。
- 会話の履歴の入口: 他の利用者の会話は 404、system_admin は閲覧だけ（内容を足すのは 403。#3244）、
  別の会話のメッセージ ID を混ぜた呼び出しは 404、履歴の組み立てに別の会話は入らない。
- 署名つきの URL（原文のプレビュー・図）: 読むたびにトークンの利用者の今の状態・権限・範囲を
  確かめる（期限切れ 410・改ざん 403・権限を外した / 無効にした 403・範囲の外 404）。トークンは
  文書・chunk・版に縛り、頁の番号は同じ文書の中だけを読む。
- MCP の文書を読むツール: 呼んだ利用者の今の範囲で読む（範囲の外は source_not_found）。

Oracle・Object Storage・LLM は fake / スタブ（合成の内容）で、実サービスは呼ばない。
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pr_system_settings.auth.service_token import issue_service_token
from pr_system_settings.auth.visibility import OTHERS_CONTENT_FORBIDDEN_MESSAGE
from pytest import MonkeyPatch

from app.api.routes import chat as chat_route
from app.api.routes import documents as documents_route
from app.api.routes import figures as figures_route
from app.api.routes import history as history_route
from app.api.routes import search as search_route
from app.api.routes import source_previews as source_previews_route
from app.clients.oracle import StoredConversation, StoredMessage
from app.config import get_settings
from app.main import app
from app.mcp import tools as mcp_tools
from app.rag import document_crop, section_map_service
from app.rag.chat_answer_runs import ChatAnswerRunService
from app.rag.figure_url import issue_figure_token
from app.rag.request_context import current_audit_request_context, user_id_hash_for
from app.rag.source_preview import SOURCE_PREVIEW_TTL_SECONDS, issue_source_preview_token
from app.security.domain import RoleRecord
from app.security.permissions import MENU_CHAT
from app.security.service import set_security_service
from tests.security_support import (
    ProductionAuth,
    enable_production_auth,
    login,
    login_configured_admin,
)
from tests.support import AsgiTestClient
from tests.test_chat_api import _FakePipeline, _stub_stream
from tests.test_chat_visibility import OwnedChatOracle
from tests.test_figure_url import _figure_chunk, _ScopedFigureOracle
from tests.test_mcp_api import SECRET, _figure_pdf, _FigureStorage
from tests.test_mcp_profile_scope import _RecordingReadOracle, _RecordingSectionStore
from tests.test_run_history import _row
from tests.test_source_preview import _chunk, _documents, _PreviewOracle, _Storage

client = AsgiTestClient(app)

# 合成の文書 → ナレッジベース（保存した根拠の確かめ直しの fake の見え方）。
DOCUMENT_KB = {"doc-a": "kb-a", "doc-b": "kb-b"}
ATTACHMENT_ID = "attachment:att-1"


def _citation(document_id: str, chunk_id: str) -> dict[str, object]:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": f"{document_id} の合成の抜粋",
        "score": 0.5,
        "file_name": f"{document_id}.pdf",
        "metadata": {"page_start": 1},
    }


SAVED_CITATIONS = [
    _citation("doc-a", "doc-a:c1"),
    _citation("doc-b", "doc-b:c1"),
    # 会話の添付の根拠は RAG の文書ではないので、会話を見られる人には確かめずに出す。
    {**_citation(ATTACHMENT_ID, "att-1:c1"), "metadata": {"chat_attachment_id": "att-1"}},
]
# 診断の「読んでいない節」（文書へのリンク）も、見られない文書の分は出さない。
SAVED_DIAGNOSTICS = {
    "outcome": "answered",
    "section_expansion": {
        "unread": [
            {"document_id": "doc-a", "line": "A の節", "page": 1},
            {"document_id": "doc-b", "line": "B の節", "page": 2},
        ]
    },
}


def _readable_by_context(document_ids: Sequence[str]) -> set[str]:
    """``accessible_document_ids`` の fake: 見る人の今の範囲（監査 context の KB）で絞る。"""
    allowed = current_audit_request_context().allowed_knowledge_base_ids
    return {
        item
        for item in document_ids
        if item in DOCUMENT_KB and (allowed is None or DOCUMENT_KB[item] in allowed)
    }


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    production = enable_production_auth(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    yield production
    set_security_service(None)


class Principals:
    """持ち主（KB a と b）・ほかの利用者・system_admin。持ち主の範囲は後から変えられる。"""

    def __init__(self, auth: ProductionAuth, permissions: list[str]) -> None:
        self.auth = auth
        self.owner = auth.user_with_permissions(
            "evidence-owner",
            permissions,
            search_answer_profile_ids=["bv-1"],
            knowledge_base_ids=["kb-a", "kb-b"],
        )
        self.other = auth.user_with_permissions(
            "evidence-other",
            permissions,
            search_answer_profile_ids=["bv-1"],
            knowledge_base_ids=["kb-a", "kb-b"],
        )

    def owner_role(self) -> RoleRecord:
        role = self.auth.store.roles[self.auth.store.users[self.owner.user_uuid].role_ids[0]]
        assert isinstance(role, RoleRecord)
        return role

    def headers(self, principal: str) -> dict[str, str]:
        if principal == "system_admin":
            return login_configured_admin(client)
        if principal == "other":
            return login(client, "evidence-other")
        if principal == "owner_kb_removed":
            # URL・記録を作った後に、持ち主の範囲から KB b を外した。
            self.owner_role().knowledge_base_ids = {"kb-a"}
        return login(client, "evidence-owner")


# 主体 → (status, 見える文書の根拠, 権限の無い根拠の数, 診断の読んでいない節の文書)。
SAVED_EVIDENCE_MATRIX = [
    ("owner", 200, ["doc-a", "doc-b", ATTACHMENT_ID], 0, ["doc-a", "doc-b"]),
    ("owner_kb_removed", 200, ["doc-a", ATTACHMENT_ID], 1, ["doc-a"]),
    ("other", 404, None, None, None),
    # system_admin は閲覧できる（範囲の制限なし）。内容を足す操作は別の行（#3244）。
    ("system_admin", 200, ["doc-a", "doc-b", ATTACHMENT_ID], 0, ["doc-a", "doc-b"]),
]
SAVED_EVIDENCE_IDS = [row[0] for row in SAVED_EVIDENCE_MATRIX]


def _assert_evidence(
    data: dict[str, Any],
    documents: list[str],
    withheld: int,
    *,
    unread: list[str] | None = None,
    diagnostics_key: str | None = None,
) -> None:
    assert [item["document_id"] for item in data["citations"]] == documents
    assert data["withheld_evidence_count"] == withheld
    # 見られない根拠の抜粋・文書名は応答のどこにも残さない。
    assert "doc-b の合成の抜粋" not in json.dumps(data, ensure_ascii=False) or withheld == 0
    if diagnostics_key is not None and unread is not None:
        lines = data[diagnostics_key]["section_expansion"]["unread"]
        assert [item["document_id"] for item in lines] == unread


# ---------------------------------------------------------------------------
# 保存した根拠の再表示: チャットの会話の取得
# ---------------------------------------------------------------------------


class _EvidenceChatOracle(OwnedChatOracle):
    async def accessible_document_ids(self, document_ids: Sequence[str]) -> set[str]:
        return _readable_by_context(document_ids)


@pytest.fixture
def chat_oracle(monkeypatch: MonkeyPatch) -> _EvidenceChatOracle:
    fake = _EvidenceChatOracle()
    monkeypatch.setattr(chat_route, "OracleClient", lambda *a, **k: fake)
    return fake


def _seed_conversation(oracle: _EvidenceChatOracle, owner_uuid: str, suffix: str) -> str:
    owner_hash = user_id_hash_for(owner_uuid)
    now = datetime.now(UTC)
    conversation_id = f"conv-{suffix}"
    oracle.conversations[conversation_id] = StoredConversation(
        id=conversation_id,
        search_answer_profile_id="bv-1",
        created_at=now,
        updated_at=now,
        message_count=2,
        user_id_hash=owner_hash,
    )
    oracle.messages[conversation_id] = [
        StoredMessage(
            id=f"q-{suffix}",
            conversation_id=conversation_id,
            role="USER",
            content="合成の質問",
            created_at=now,
            user_id_hash=owner_hash,
        ),
        StoredMessage(
            id=f"a-{suffix}",
            conversation_id=conversation_id,
            role="ASSISTANT",
            content="合成の回答",
            created_at=now,
            reply_to_message_id=f"q-{suffix}",
            citations=[dict(item) for item in SAVED_CITATIONS],
            trace_id=f"trace-{suffix}",
            user_id_hash=owner_hash,
        ),
    ]
    return conversation_id


@pytest.mark.parametrize(
    ("principal", "status", "documents", "withheld", "unread"),
    SAVED_EVIDENCE_MATRIX,
    ids=SAVED_EVIDENCE_IDS,
)
def test_chat_conversation_rechecks_saved_citations(
    auth: ProductionAuth,
    chat_oracle: _EvidenceChatOracle,
    principal: str,
    status: int,
    documents: list[str] | None,
    withheld: int | None,
    unread: list[str] | None,
) -> None:
    del unread
    principals = Principals(auth, [MENU_CHAT])
    conversation_id = _seed_conversation(chat_oracle, principals.owner.user_uuid, "chat")
    response = client.get(
        f"/api/chat/conversations/{conversation_id}", headers=principals.headers(principal)
    )
    assert response.status_code == status, response.text
    if documents is None or withheld is None:
        return
    [_, answer] = response.json()["data"]["messages"]
    _assert_evidence(answer, documents, withheld)
    # 回答の本文は保存した文のまま（既知の限界。本文から資料の部分は消せない）。
    assert answer["content"] == "合成の回答"


# ---------------------------------------------------------------------------
# 保存した根拠の再表示: 実行履歴の詳細
# ---------------------------------------------------------------------------


class _HistoryStore:
    """実行履歴の store の fake（SQL の見え方の条件: system_admin 以外は自分の実行だけ）。"""

    rows: dict[str, dict[str, object]] = {}

    def __init__(self, oracle: object) -> None:
        del oracle

    async def get_run(self, run_id: str) -> dict[str, object] | None:
        row = self.rows.get(run_id)
        context = current_audit_request_context()
        if row is None:
            return None
        if context.all_users_visible or row["user_id_hash"] == context.user_id_hash:
            return dict(row)
        return None

    async def list_run_feedback(self, run_id: str) -> list[dict[str, object]]:
        return []


class _AnswerOracle:
    """回答の記録の fake（保存した引用・診断と、今の範囲の文書の確かめ）。"""

    async def get_answer_record(self, trace_id: str) -> dict[str, object] | None:
        return {
            "trace_id": trace_id,
            "search_answer_profile_id": "bv-1",
            "surface": "chat",
            "answer_engine": "grounded",
            "question": "合成の質問",
            "rewritten_question": None,
            "answer": "合成の回答",
            "citations_json": [dict(item) for item in SAVED_CITATIONS],
            "diagnostics_json": json.loads(json.dumps(SAVED_DIAGNOSTICS)),
            "answer_notice": None,
            "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        }

    async def accessible_document_ids(self, document_ids: Sequence[str]) -> set[str]:
        return _readable_by_context(document_ids)


@pytest.mark.parametrize(
    ("principal", "status", "documents", "withheld", "unread"),
    SAVED_EVIDENCE_MATRIX,
    ids=SAVED_EVIDENCE_IDS,
)
def test_run_history_detail_rechecks_saved_evidence(
    auth: ProductionAuth,
    monkeypatch: MonkeyPatch,
    principal: str,
    status: int,
    documents: list[str] | None,
    withheld: int | None,
    unread: list[str] | None,
) -> None:
    principals = Principals(auth, ["menu.history"])
    _HistoryStore.rows = {
        "run-1": _row(
            "run-1",
            surface="chat",
            answer_generated=1,
            user_id_hash=user_id_hash_for(principals.owner.user_uuid),
            user_uuid=principals.owner.user_uuid,
        )
        | {"question_full": "合成の質問", "error_type": None, "stages_json": {}}
    }
    monkeypatch.setattr(history_route, "RunHistoryStore", _HistoryStore)
    monkeypatch.setattr(history_route, "OracleClient", lambda *a, **k: _AnswerOracle())
    response = client.get("/api/history/runs/run-1", headers=principals.headers(principal))
    assert response.status_code == status, response.text
    if documents is None or withheld is None:
        return
    data = response.json()["data"]
    _assert_evidence(data, documents, withheld, unread=unread, diagnostics_key="answer_diagnostics")
    assert data["answer"] == "合成の回答"


# ---------------------------------------------------------------------------
# 保存した根拠の再表示: 保存された回答の詳細
# ---------------------------------------------------------------------------


class _ScopedAnswerOracle(_AnswerOracle):
    """回答の記録の見え方（持ち主だけ。system_admin と回答の管理は全件。#304）。"""

    owner_hash: str | None = None

    async def get_answer_record(self, trace_id: str) -> dict[str, object] | None:
        context = current_audit_request_context()
        if not context.answer_records_unrestricted and context.user_id_hash != self.owner_hash:
            return None
        return await super().get_answer_record(trace_id)


@pytest.mark.parametrize(
    ("principal", "status", "documents", "withheld", "unread"),
    SAVED_EVIDENCE_MATRIX,
    ids=SAVED_EVIDENCE_IDS,
)
def test_saved_answer_detail_rechecks_saved_evidence(
    auth: ProductionAuth,
    monkeypatch: MonkeyPatch,
    principal: str,
    status: int,
    documents: list[str] | None,
    withheld: int | None,
    unread: list[str] | None,
) -> None:
    principals = Principals(auth, ["menu.search"])
    oracle = _ScopedAnswerOracle()
    oracle.owner_hash = user_id_hash_for(principals.owner.user_uuid)
    monkeypatch.setattr(search_route, "OracleClient", lambda *a, **k: oracle)
    response = client.get("/api/search/answers/trace-1", headers=principals.headers(principal))
    assert response.status_code == status, response.text
    if documents is None or withheld is None:
        return
    data = response.json()["data"]
    _assert_evidence(data, documents, withheld, unread=unread, diagnostics_key="answer_diagnostics")


# ---------------------------------------------------------------------------
# 会話の履歴の入口（#3257 の分）
# ---------------------------------------------------------------------------


class _HistoryPipeline(_FakePipeline):
    """回答フローに渡した会話の履歴を残す pipeline の stub。"""

    histories: list[list[str]] = []

    async def run(self, request: Any, *args: Any, history: Any = None, **kwargs: Any) -> Any:
        _HistoryPipeline.histories.append([turn.content for turn in history or []])
        return await super().run(request, *args, history=history, **kwargs)  # type: ignore[no-untyped-call]


@pytest.mark.parametrize(
    ("principal", "read", "send"),
    [("owner", 200, 200), ("other", 404, 404), ("system_admin", 200, 403)],
    ids=["owner", "other", "system_admin"],
)
def test_conversation_history_entries_by_principal(
    auth: ProductionAuth,
    chat_oracle: _EvidenceChatOracle,
    monkeypatch: MonkeyPatch,
    principal: str,
    read: int,
    send: int,
) -> None:
    """会話の ID を知っているだけでは、履歴・処理の経過を読めず、続きの質問もできない。"""
    principals = Principals(auth, [MENU_CHAT])
    conversation_id = _seed_conversation(chat_oracle, principals.owner.user_uuid, "own")
    other_conversation = _seed_conversation(chat_oracle, principals.other.user_uuid, "foreign")
    _stub_stream(monkeypatch, chat_oracle, ["m1"])
    monkeypatch.setattr(chat_route, "RagPipeline", _HistoryPipeline)
    _HistoryPipeline.histories = []
    chat_oracle.messages[other_conversation][0].content = "別の会話の質問"
    chat_oracle.messages[other_conversation][1].content = "別の会話の回答"
    headers = principals.headers(principal)
    base = f"/api/chat/conversations/{conversation_id}"

    assert client.get(base, headers=headers).status_code == read
    assert client.get(f"{base}/messages/a-own/trace", headers=headers).status_code == read
    sent = client.post(f"{base}/messages/stream", json={"content": "続き"}, headers=headers)
    assert sent.status_code == send, sent.text
    if send == 403:
        assert sent.json()["error_messages"] == [OTHERS_CONTENT_FORBIDDEN_MESSAGE]
    if send == 200:
        # 履歴の組み立て（質問の単独化を含む）に別の会話の発話は入らない。
        assert _HistoryPipeline.histories
        assert all(
            "別の会話の質問" not in turns and "別の会話の回答" not in turns
            for turns in _HistoryPipeline.histories
        )


def test_message_ids_of_another_conversation_are_not_found(
    auth: ProductionAuth, chat_oracle: _EvidenceChatOracle, monkeypatch: MonkeyPatch
) -> None:
    """自分の会話の path に、別の会話（他の利用者の会話）のメッセージ ID を混ぜても読めない。"""
    principals = Principals(auth, [MENU_CHAT])
    own = _seed_conversation(chat_oracle, principals.owner.user_uuid, "own")
    _seed_conversation(chat_oracle, principals.other.user_uuid, "foreign")
    headers = principals.headers("owner")
    base = f"/api/chat/conversations/{own}/messages"
    assert client.get(f"{base}/a-own/trace", headers=headers).status_code == 200
    assert client.get(f"{base}/a-foreign/trace", headers=headers).status_code == 404
    assert client.get(f"{base}/q-foreign/stream", headers=headers).status_code == 404
    # まだ始まっていない質問への停止は 200 で覚えておくが、覚えた取消は受けた会話に結び付く。
    # 別の会話の質問の ID を混ぜても、その会話の回答は止まらない（#3302）。
    service = ChatAnswerRunService()
    monkeypatch.setattr(chat_route, "get_chat_answer_run_service", lambda: service)
    stopped = client.post(f"{base}/q-next/cancel", headers=headers)
    assert stopped.status_code == 200, stopped.text
    foreign = service.register(
        run_id="q-next",
        conversation_id="conv-foreign",
        tenant_id_hash=None,
        user_id_hash=None,
        store=chat_oracle,
    )
    assert foreign.cancel_requested is False
    client.post(f"{base}/q-own-next/cancel", headers=headers)
    own_run = service.register(
        run_id="q-own-next",
        conversation_id=own,
        tenant_id_hash=None,
        user_id_hash=None,
        store=chat_oracle,
    )
    assert own_run.cancel_requested is True


# ---------------------------------------------------------------------------
# 署名つきの URL（原文のプレビュー・図）
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_targets(monkeypatch: MonkeyPatch) -> None:
    """kb-1 を使える利用者にだけ根拠の chunk が見える fake（原文のプレビューと図）。"""
    _PreviewOracle.visible = {("d1", "c1"): _chunk("d1", "c1", "文書 A.pdf", page_start=1)}
    _PreviewOracle.stale = set()
    _PreviewOracle.documents = _documents()
    _Storage.files = {"docs/d1/source.pdf": _figure_pdf(), "docs/d3/source.json": b"{}"}
    _Storage.reads = []
    monkeypatch.setattr(mcp_tools, "OracleClient", _PreviewOracle)
    monkeypatch.setattr(source_previews_route, "OracleClient", _PreviewOracle)
    monkeypatch.setattr(documents_route, "ObjectStorageClient", _Storage)
    _ScopedFigureOracle.visible = {("d1", "c-fig"): _figure_chunk()}
    _ScopedFigureOracle.stale = set()
    _ScopedFigureOracle.contexts = []
    _FigureStorage.files = {"docs/d1/r-1/prepared.pdf": _figure_pdf()}
    _FigureStorage.reads = []
    monkeypatch.setattr(figures_route, "OracleClient", _ScopedFigureOracle)
    monkeypatch.setattr(document_crop, "ObjectStorageClient", _FigureStorage)


def _preview_token(subject: str, *, document_id: str = "d1", now: float | None = None) -> str:
    token, _ = issue_source_preview_token(
        SECRET,
        subject=subject,
        document_id=document_id,
        chunk_id="c1",
        chunk_set_id="cs-1",
        recipe_id=None,
        variant="original",
        now=now,
    )
    return token


def _figure_token(subject: str, *, now: float | None = None) -> str:
    token, _ = issue_figure_token(
        SECRET,
        subject=subject,
        document_id="d1",
        chunk_id="c-fig",
        chunk_set_id="cs-1",
        now=now,
    )
    return token


def _rewrite_document(token: str, document_id: str) -> str:
    """署名を変えずに中身の文書を書き換えたトークン（path の書き換え）。"""
    version, body, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["doc"] = document_id
    rewritten = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{version}.{rewritten}.{signature}"


SIGNED_ENTRIES = {
    "preview_page": lambda token: f"/api/source-previews/{token}/pages/1?dpi=72",
    "preview_content": lambda token: f"/api/source-previews/{token}/content",
    "figure": lambda token: f"/api/figures/{token}",
}


def _setup_signed_user(auth: ProductionAuth, case: str) -> str:
    """主体・範囲の場合ごとの、トークンの利用者（URL の発行の後に権限を変える場合を含む）。"""
    user = auth.user_with_permissions(
        f"signed-{case}", ["menu.search"], knowledge_base_ids=["kb-1"]
    )
    role = auth.store.roles[auth.store.users[user.user_uuid].role_ids[0]]
    assert isinstance(role, RoleRecord)
    if case == "permission_removed":
        role.permissions = {"menu.upload"}
    elif case == "kb_removed":
        role.knowledge_base_ids = {"kb-2"}
    elif case == "user_disabled":
        auth.store.users[user.user_uuid].status = "INACTIVE"
    return user.user_uuid


# 場合 → 期待する status（3 つの入口で同じ）。
SIGNED_MATRIX = [
    ("valid", 200),
    ("permission_removed", 403),
    ("user_disabled", 403),
    ("kb_removed", 404),
    ("expired", 410),
    ("rewritten_document", 403),
    ("unknown_user", 403),
]


@pytest.mark.parametrize("entry", list(SIGNED_ENTRIES), ids=list(SIGNED_ENTRIES))
@pytest.mark.parametrize(("case", "status"), SIGNED_MATRIX, ids=[row[0] for row in SIGNED_MATRIX])
def test_signed_urls_recheck_the_token_user_on_every_read(
    auth: ProductionAuth,
    signed_targets: None,
    entry: str,
    case: str,
    status: int,
) -> None:
    del signed_targets
    subject = (
        "00000000-0000-0000-0000-00000000dead"
        if case == "unknown_user"
        else _setup_signed_user(auth, case)
    )
    issue = _figure_token if entry == "figure" else _preview_token
    now = time.time() - SOURCE_PREVIEW_TTL_SECONDS - 5 if case == "expired" else None
    token = issue(subject, now=now)
    if case == "rewritten_document":
        token = _rewrite_document(token, "d3")
    response = client.get(SIGNED_ENTRIES[entry](token))
    assert response.status_code == status, response.text
    # 成功も失敗も、cache・Referer に残さない。
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_signed_preview_pages_stay_in_the_bound_document(
    auth: ProductionAuth, signed_targets: None
) -> None:
    """頁の番号の path は書き換えられるが、読むのはトークンの文書のファイルだけ（範囲は文書）。"""
    del signed_targets
    subject = _setup_signed_user(auth, "valid")
    token = _preview_token(subject)
    assert client.get(f"/api/source-previews/{token}/pages/1?dpi=72").status_code == 200
    # 文書に無い頁は 422（別の文書の頁にはならない）。
    assert client.get(f"/api/source-previews/{token}/pages/9").status_code == 422
    assert set(_Storage.reads) == {"docs/d1/source.pdf"}
    # 別の文書用のトークンを使い回しても、その文書の根拠が見えなければ読めない。
    other = _preview_token(subject, document_id="d3")
    assert client.get(f"/api/source-previews/{other}/content").status_code == 404
    # 図のトークンを原文のプレビューに、原文のプレビューのトークンを図に使えない（用途の鍵が違う）。
    assert client.get(f"/api/source-previews/{_figure_token(subject)}/content").status_code == 403
    assert client.get(f"/api/figures/{token}").status_code == 403


# ---------------------------------------------------------------------------
# MCP の文書を読むツール
# ---------------------------------------------------------------------------


class _ScopedReadOracle(_RecordingReadOracle):
    """kb-1 を使える利用者にだけ chunk が見える fake（読んだ範囲も残す）。"""

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> Any:
        context = current_audit_request_context()
        self.contexts.append(context)
        allowed = context.allowed_knowledge_base_ids
        if allowed is not None and "kb-1" not in allowed:
            return None
        return await super().retrievable_chunk(document_id, chunk_id)

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        return False

    async def accessible_document_ids(self, document_ids: Sequence[str]) -> set[str]:
        context = current_audit_request_context()
        self.contexts.append(context)
        allowed = context.allowed_knowledge_base_ids
        return set(document_ids) if allowed is None or "kb-1" in allowed else set()


def _mcp(name: str, arguments: dict[str, Any], subject: str) -> Any:
    token = issue_service_token(
        SECRET, subject=subject, audience="rag", issuer="agent", claims={"run_id": "run-1"}
    )
    return client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )


MCP_READ_TOOLS: dict[str, dict[str, Any]] = {
    "rag_read_source": {"document_id": "d1", "chunk_id": "c1"},
    "rag_read_section": {"document_id": "d1", "section_id": "s1"},
    "rag_read_document": {"document_id": "d1"},
    "rag_readable_documents": {"document_ids": ["d1"]},
}
# 場合 → (HTTP の status, ツールのエラーの code。None は成功、"scope" は読んだ範囲だけを確かめる)。
MCP_MATRIX = [
    ("valid", 200, None),
    ("kb_removed", 200, mcp_tools.SOURCE_NOT_FOUND_CODE),
    ("permission_removed", 200, "MCP_TOOL_FORBIDDEN"),
    ("user_disabled", 403, None),
]


@pytest.mark.parametrize("tool", list(MCP_READ_TOOLS), ids=list(MCP_READ_TOOLS))
@pytest.mark.parametrize(("case", "status", "code"), MCP_MATRIX, ids=[row[0] for row in MCP_MATRIX])
def test_mcp_reading_tools_read_within_the_callers_current_scope(
    auth: ProductionAuth,
    monkeypatch: MonkeyPatch,
    tool: str,
    case: str,
    status: int,
    code: str | None,
) -> None:
    # 章節の store の fake は `_RecordingReadOracle.contexts` に残すので、同じ一覧を使う。
    _RecordingReadOracle.contexts = _ScopedReadOracle.contexts = []
    monkeypatch.setattr(mcp_tools, "OracleClient", _ScopedReadOracle)
    monkeypatch.setattr(section_map_service, "OracleClient", _ScopedReadOracle)
    monkeypatch.setattr(section_map_service, "SectionMapStore", _RecordingSectionStore)
    subject = _setup_signed_user(auth, case)
    response = _mcp(tool, MCP_READ_TOOLS[tool], subject)
    assert response.status_code == status, response.text
    if status != 200:
        return
    result = response.json()["result"]
    if code == "MCP_TOOL_FORBIDDEN":
        assert result["structuredContent"]["error_code"] == code
        assert _ScopedReadOracle.contexts == []
        return
    # 読むのは呼んだ利用者の今の範囲（URL・Run の利用者の範囲を借りない）。
    expected = frozenset({"kb-2"}) if case == "kb_removed" else frozenset({"kb-1"})
    assert {context.allowed_knowledge_base_ids for context in _ScopedReadOracle.contexts} == {
        expected
    }
    content = result["structuredContent"]
    if tool == "rag_readable_documents":
        assert content["readable_document_ids"] == ([] if case == "kb_removed" else ["d1"])
    elif tool == "rag_read_source":
        if code is None:
            assert result["isError"] is False, result
        else:
            assert content["error_code"] == code
    else:
        # 章節・文書を読むツールは fake に索引が無いので、範囲の中でも見つからない
        # （範囲だけを確かめる）。
        assert content["error_code"] == mcp_tools.SOURCE_NOT_FOUND_CODE


def test_readable_documents_tool_is_not_listed_for_models() -> None:
    """保存した根拠の確かめ直しは Agent の画面だけが名前で呼ぶ内部のツール（一覧に出さない）。"""
    server = mcp_tools.build_rag_mcp_server(None)  # type: ignore[arg-type]
    assert server.tools["rag_readable_documents"].listed is False
    assert all(
        tool.listed for name, tool in server.tools.items() if name != "rag_readable_documents"
    )


# ---------------------------------------------------------------------------
# 画面（Cookie）の文書の頁の入口
# ---------------------------------------------------------------------------


class _ScopedDocumentOracle:
    """文書の取得の fake（SQL と同じく、範囲の外の文書は無いものとして扱う）。"""

    async def get_document(self, document_id: str) -> Any:
        allowed = current_audit_request_context().allowed_knowledge_base_ids
        if document_id != "d1" or (allowed is not None and "kb-1" not in allowed):
            return None
        detail = _documents()["d1"]
        detail.preprocess_artifact = None
        return detail


@pytest.mark.parametrize(
    ("knowledge_bases", "permissions", "status"),
    [
        (["kb-1"], ["menu.search"], 200),
        (["kb-2"], ["menu.search"], 404),
        (["kb-1"], ["menu.upload"], 200),
        (["kb-1"], [], 403),
    ],
    ids=["in-scope", "out-of-scope", "documents-menu", "no-permission"],
)
def test_document_preview_pages_follow_the_viewer_scope(
    auth: ProductionAuth,
    monkeypatch: MonkeyPatch,
    knowledge_bases: list[str],
    permissions: list[str],
    status: int,
) -> None:
    monkeypatch.setattr(documents_route, "OracleClient", lambda *a, **k: _ScopedDocumentOracle())
    monkeypatch.setattr(documents_route, "ObjectStorageClient", _Storage)
    _Storage.files = {"docs/d1/source.pdf": _figure_pdf()}
    auth.user_with_permissions("pages-viewer", permissions, knowledge_base_ids=knowledge_bases)
    headers = login(client, "pages-viewer")
    pages = client.get("/api/documents/d1/preview-pages", headers=headers)
    page = client.get("/api/documents/d1/preview-pages/1?dpi=72", headers=headers)
    assert (pages.status_code, page.status_code) == (status, status), pages.text
