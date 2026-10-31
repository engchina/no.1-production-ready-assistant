"""利用者フィードバック API のテスト。"""

from datetime import UTC, datetime

import pytest
from pr_system_settings.auth.visibility import OTHERS_CONTENT_FORBIDDEN_MESSAGE
from pydantic import ValidationError

from app.api.routes import feedback as feedback_route
from app.main import app
from app.rag.request_context import current_audit_request_context, user_id_hash_for
from app.schemas.feedback import FeedbackRequest
from tests.security_support import enable_production_auth, login
from tests.support import TEST_USER_ID, AsgiTestClient

client = AsgiTestClient(app)


def test_feedback_request_validates_target_and_reason() -> None:
    """対象ごとの ID と低評価理由を必須にする。"""
    with pytest.raises(ValidationError, match="文書 ID とチャンク ID"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="citation",
            source_surface="search",
            rating="not_helpful",
            reason="not_relevant",
        )
    with pytest.raises(ValidationError, match="理由を選択"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="answer",
            source_surface="chat",
            rating="not_helpful",
        )
    with pytest.raises(ValidationError, match="対応していない理由"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="answer",
            source_surface="chat",
            rating="not_helpful",
            reason="missing_evidence",
        )


def test_feedback_request_validates_content_source_shape() -> None:
    """chat message と search snapshot を混在させない。"""
    snapshot = {"question": "質問", "answer": "回答", "citations": []}
    with pytest.raises(ValidationError, match="同時に指定できません"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="answer",
            source_surface="chat",
            message_id="message-1",
            content_snapshot=snapshot,
            rating="helpful",
        )
    with pytest.raises(ValidationError, match="RAG 検索の評価だけ"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="answer",
            source_surface="chat",
            content_snapshot=snapshot,
            rating="helpful",
        )


def test_feedback_request_limits_comment_and_clears_it_for_helpful() -> None:
    """低評価コメントは 1000 文字まで、高評価では保存しない。"""
    helpful = FeedbackRequest(
        trace_id="trace-1",
        search_answer_profile_id="bv-1",
        target_type="answer",
        source_surface="search",
        rating="helpful",
        comment="保存しないコメント",
    )
    assert helpful.comment is None
    with pytest.raises(ValidationError, match="at most 1000"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="answer",
            source_surface="search",
            rating="not_helpful",
            reason="incorrect",
            comment="長" * 1001,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_id", "t" * 65),
        ("search_answer_profile_id", "b" * 65),
        ("document_id", "d" * 65),
        ("chunk_id", "c" * 129),
    ],
)
def test_feedback_request_rejects_ids_longer_than_oracle_columns(
    field: str,
    value: str,
) -> None:
    """ID は保存先の Oracle 列幅を超えて受け付けない。"""
    payload = {
        "trace_id": "trace-1",
        "search_answer_profile_id": "bv-1",
        "target_type": "citation",
        "source_surface": "search",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "rating": "helpful",
        field: value,
    }

    with pytest.raises(ValidationError, match="at most"):
        FeedbackRequest.model_validate(payload)


def test_submit_feedback_keeps_metadata_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文を送らない既存 client は分類 metadata だけで保存できる。"""
    fake = FakeFeedbackClient()
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": "trace-1",
            "search_answer_profile_id": "bv-1",
            "target_type": "answer",
            "source_surface": "chat",
            "rating": "not_helpful",
            "reason": "incorrect",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["feedback_id"] == "feedback-1"
    assert fake.saved == [
        {
            "trace_id": "trace-1",
            "search_answer_profile_id": "bv-1",
            "target_type": "answer",
            "source_surface": "chat",
            "document_id": None,
            "chunk_id": None,
            "rating": "not_helpful",
            "reason": "incorrect",
            "comment_hash": None,
            "comment_chars": 0,
        }
    ]
    assert fake.saved_details == [None]
    assert fake.user_id_hash is not None


def test_submit_feedback_saves_search_snapshot_and_optional_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFeedbackClient()
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": "trace-1",
            "search_answer_profile_id": "bv-1",
            "target_type": "answer",
            "source_surface": "search",
            "rating": "not_helpful",
            "reason": "incorrect",
            "comment": "根拠が古いです。",
            "content_snapshot": {
                "question": "最新規程は？",
                "answer": "2024年版です。",
                "citations": [],
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["comment"] == "根拠が古いです。"
    assert fake.saved[0]["comment_chars"] == 8
    assert fake.saved_details == [
        {
            "message_id": None,
            "content_source": "search_snapshot",
            "question_text": "最新規程は？",
            "answer_text": "2024年版です。",
            "citations": [],
            "comment_text": "根拠が古いです。",
            "corrected_answer_text": None,
        }
    ]


def test_submit_citation_feedback_from_search_without_answer_keeps_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答を生成しない検索の引用の評価でも、質問と根拠を保存する（#978）。"""
    fake = FakeFeedbackClient()
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": "trace-search-only",
            "search_answer_profile_id": "bv-1",
            "target_type": "citation",
            "source_surface": "search",
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "rating": "helpful",
            "content_snapshot": {
                "question": " 経費の上限は？ ",
                "answer": "  ",
                "citations": [{"document_id": "doc-1", "chunk_id": "chunk-1"}],
            },
        },
    )

    assert response.status_code == 200
    details = fake.saved_details[0]
    assert details is not None
    assert details["question_text"] == "経費の上限は？"
    assert details["answer_text"] is None
    assert details["citations"][0]["chunk_id"] == "chunk-1"  # type: ignore[index]


def test_answer_feedback_snapshot_still_requires_the_answer() -> None:
    """回答の評価の snapshot は回答の本文が必須のまま（#978）。"""
    with pytest.raises(ValidationError, match="回答の本文を入力してください"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="answer",
            source_surface="search",
            content_snapshot={"question": "質問", "answer": None, "citations": []},
            rating="helpful",
        )
    with pytest.raises(ValidationError, match="質問の本文を入力してください"):
        FeedbackRequest(
            trace_id="trace-1",
            search_answer_profile_id="bv-1",
            target_type="citation",
            source_surface="search",
            document_id="doc-1",
            chunk_id="chunk-1",
            content_snapshot={"question": " ", "citations": []},
            rating="helpful",
        )


def test_submit_search_snapshot_does_not_depend_on_the_search_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """監査を Oracle に保存しない既定の構成でも、検索のフィードバックを保存できる（#457）。"""
    fake = FakeFeedbackClient()
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": "trace-without-audit",
            "search_answer_profile_id": "bv-1",
            "target_type": "answer",
            "source_surface": "search",
            "rating": "helpful",
            "content_snapshot": {"question": "質問", "answer": "回答", "citations": []},
        },
    )

    assert response.status_code == 200
    assert fake.saved[0]["trace_id"] == "trace-without-audit"


def test_submit_feedback_resolves_chat_content_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFeedbackClient()
    fake.message_context = {
        "message_id": "message-1",
        # 評価するのは会話の持ち主（呼んだ利用者。#3244）。
        "conversation_user_id_hash": user_id_hash_for(TEST_USER_ID),
        "question_text": "質問",
        "answer_text": "回答",
        "citations": [],
        "content_source": "chat_message",
    }
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": "trace-1",
            "search_answer_profile_id": "bv-1",
            "target_type": "answer",
            "source_surface": "chat",
            "message_id": "message-1",
            "rating": "helpful",
        },
    )

    assert response.status_code == 200
    assert fake.message_lookup == ("message-1", "trace-1")
    assert fake.saved_details[0] == {**fake.message_context, "comment_text": None}


def test_only_the_conversation_owner_rates_chat_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """チャットの回答を評価できるのは会話の持ち主だけ（system_admin でも 403。#3244）。"""
    fake = FakeFeedbackClient()
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)
    caller = user_id_hash_for(TEST_USER_ID)
    body = {
        "trace_id": "trace-1",
        "search_answer_profile_id": "bv-1",
        "target_type": "answer",
        "source_surface": "chat",
        "rating": "helpful",
    }
    context = {
        "message_id": "message-1",
        "question_text": "質問",
        "answer_text": "回答",
        "citations": [],
        "content_source": "chat_message",
    }

    # 他の利用者の会話の回答（メッセージ ID あり・なし）は 403 で、何も保存しない。
    fake.message_context = {**context, "conversation_user_id_hash": "other-owner-hash"}
    refused = client.post("/api/feedback", json={**body, "message_id": "message-1"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_messages"] == [OTHERS_CONTENT_FORBIDDEN_MESSAGE]
    fake.chat_answer_owner = "other-owner-hash"
    assert client.post("/api/feedback", json=body).status_code == 403
    assert fake.saved == []

    # 自分の会話の回答は今どおり保存し、持ち主の hash は評価の記録に入れない。
    fake.message_context = {**context, "conversation_user_id_hash": caller}
    own = client.post("/api/feedback", json={**body, "message_id": "message-1"})
    assert own.status_code == 200, own.text
    saved = fake.saved_details[0]
    assert saved is not None and saved["message_id"] == "message-1"
    assert "conversation_user_id_hash" not in saved
    fake.chat_answer_owner = caller
    assert client.post("/api/feedback", json=body).status_code == 200
    # 会話に無い trace（RAG 検索など）は持ち主で絞らない。
    fake.chat_answer_owner = None
    assert client.post("/api/feedback", json=body).status_code == 200
    assert len(fake.saved) == 3
    # 持ち主の分からない古い会話の回答には誰も評価を足せない（メッセージ ID あり・なし）。
    fake.chat_answer_owner = ""
    assert client.post("/api/feedback", json=body).status_code == 403
    fake.message_context = {**context, "conversation_user_id_hash": None}
    ownerless = client.post("/api/feedback", json={**body, "message_id": "message-1"})
    assert ownerless.status_code == 403
    assert len(fake.saved) == 3


def test_submit_feedback_rejects_unknown_search_answer_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFeedbackClient(view_exists=False)
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": "trace-1",
            "search_answer_profile_id": "missing",
            "target_type": "answer",
            "source_surface": "search",
            "rating": "helpful",
        },
    )

    assert response.status_code == 404
    assert not fake.saved


def test_current_feedback_returns_latest_items(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFeedbackClient()
    fake.current = [
        {
            "feedback_id": "feedback-2",
            "trace_id": "trace-1",
            "search_answer_profile_id": "bv-1",
            "target_type": "citation",
            "source_surface": "chat",
            "document_id": "doc-1",
            "chunk_id": "doc-1:0",
            "message_id": None,
            "rating": "not_helpful",
            "reason": "not_relevant",
            "comment": None,
            "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        }
    ]
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.get("/api/feedback/current?trace_id=trace-1")

    assert response.status_code == 200
    assert response.json()["data"][0]["feedback_id"] == "feedback-2"
    assert fake.current_trace == "trace-1"


def test_feedback_dashboard_builds_latest_vote_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFeedbackClient()
    fake.dashboard = (
        [
            {
                "feedback_id": "feedback-2",
                "trace_id": "trace-1",
                "search_answer_profile_id": "bv-1",
                "search_answer_profile_name": "経理",
                "target_type": "answer",
                "source_surface": "chat",
                "document_id": None,
                "chunk_id": None,
                "rating": "not_helpful",
                "reason": "incorrect",
                "created_at": datetime(2026, 7, 1, tzinfo=UTC),
                "conversation_id": "conversation-1",
                "conversation_title": "経費精算",
                "message_id": "message-1",
                "model": "model-a",
                "file_name": None,
            }
        ],
        3,
        [
            {"target_type": "answer", "rating": "helpful", "reason": None, "item_count": 1},
            {
                "target_type": "answer",
                "rating": "not_helpful",
                "reason": "incorrect",
                "item_count": 1,
            },
            {
                "target_type": "citation",
                "rating": "helpful",
                "reason": None,
                "item_count": 1,
            },
        ],
        [],
    )
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.get("/api/feedback?period_days=30&limit=20&offset=0")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["summary"] == {
        "total": 3,
        "helpful_count": 2,
        "not_helpful_count": 1,
        "helpful_rate": 0.6667,
        "answer_total": 2,
        "answer_helpful_rate": 0.5,
        "citation_total": 1,
        "citation_helpful_rate": 1.0,
        "reason_counts": [{"reason": "incorrect", "count": 1}],
    }
    assert data["previous_summary"]["total"] == 0
    assert data["items"]["total"] == 3


def test_feedback_dashboard_without_period_covers_all_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期間「すべて」（period_days を省略）は期間で絞らず、前期間比も返さない（#978）。"""
    seen: dict[str, object] = {}

    class RecordingClient(FakeFeedbackClient):
        async def list_feedback_dashboard_rows(
            self, **kwargs: object
        ) -> tuple[
            list[dict[str, object]],
            int,
            list[dict[str, object]],
            list[dict[str, object]],
        ]:
            seen.update(kwargs)
            return self.dashboard

    monkeypatch.setattr(feedback_route, "OracleClient", lambda: RecordingClient())

    response = client.get("/api/feedback?sort_order=newest&limit=50&offset=0")

    assert response.status_code == 200
    assert seen["period_days"] is None
    assert response.json()["data"]["previous_summary"] is None

    response = client.get("/api/feedback?period_days=7")
    assert seen["period_days"] == 7
    assert response.json()["data"]["previous_summary"] is not None


def test_feedback_detail_returns_full_context(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFeedbackClient()
    fake.detail = {
        "feedback_id": "feedback-1",
        "trace_id": "trace-1",
        "search_answer_profile_id": "bv-1",
        "search_answer_profile_name": "経理",
        "target_type": "answer",
        "source_surface": "search",
        "document_id": None,
        "chunk_id": None,
        "message_id": None,
        "rating": "not_helpful",
        "reason": "incorrect",
        "comment": "古いです。",
        "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        "conversation_id": None,
        "conversation_title": None,
        "model": None,
        "file_name": None,
        "question_preview": "最新規程は？",
        "comment_preview": "古いです。",
        "has_comment": True,
        "content_source": "search_snapshot",
        "question": "最新規程は？",
        "answer": "2024年版です。",
        "citations": [],
        "execution": {"outcome": "success", "citation_count": 1},
    }
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.get("/api/feedback/feedback-1")

    assert response.status_code == 200
    assert response.json()["data"]["answer"] == "2024年版です。"
    assert response.json()["data"]["execution"]["citation_count"] == 1


def test_feedback_permissions_split_view_and_approved_faq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一覧・詳細・評価ケースは menu.feedback、承認 FAQ への反映だけ rag.feedback.manage。

    一覧は利用者の検索・回答プロファイルの範囲で絞る（Oracle の条件は test_security_scope で確認）。
    """
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("searcher", ["menu.search"])
    auth.user_with_permissions("viewer", ["menu.feedback"], search_answer_profile_ids=["bv-1"])
    auth.user_with_permissions("feedback-manager", ["rag.feedback.manage"])
    contexts: list[object] = []

    class ScopeRecordingClient(FakeFeedbackClient):
        async def list_feedback_dashboard_rows(
            self, **kwargs: object
        ) -> tuple[
            list[dict[str, object]],
            int,
            list[dict[str, object]],
            list[dict[str, object]],
        ]:
            contexts.append(current_audit_request_context().allowed_search_answer_profile_ids)
            return await super().list_feedback_dashboard_rows(**kwargs)

    monkeypatch.setattr(feedback_route, "OracleClient", lambda: ScopeRecordingClient())
    read_paths = (
        ("GET", "/api/feedback"),
        ("GET", "/api/feedback/feedback-1"),
        ("GET", "/api/feedback/feedback-1/evaluation-case"),
    )
    promote = ("POST", "/api/feedback/feedback-1/approved-faq")

    searcher = login(client, "searcher")
    for method, path in (*read_paths, promote):
        assert client.request(method, path, headers=searcher).status_code == 403, path

    viewer = login(client, "viewer")
    listed = client.get("/api/feedback", headers=viewer)
    assert listed.status_code == 200
    assert contexts == [frozenset({"bv-1"})]
    for method, path in read_paths[1:]:
        # 権限は通る（fake に該当データがないため 404）。
        assert client.request(method, path, headers=viewer).status_code == 404, path
    assert client.request(*promote, headers=viewer).status_code == 403

    manager = login(client, "feedback-manager")
    assert client.get("/api/feedback", headers=manager).status_code == 200
    assert client.request(*promote, headers=manager).status_code == 404


class FakeFeedbackClient:
    def __init__(self, *, view_exists: bool = True) -> None:
        self.view_exists = view_exists
        self.saved: list[dict[str, object]] = []
        self.saved_details: list[dict[str, object] | None] = []
        self.current: list[dict[str, object]] = []
        self.current_trace: str | None = None
        self.user_id_hash: str | None = None
        self.dashboard: tuple[
            list[dict[str, object]],
            int,
            list[dict[str, object]],
            list[dict[str, object]],
        ] = ([], 0, [], [])
        self.message_context: dict[str, object] | None = None
        self.message_lookup: tuple[str, str] | None = None
        # trace の回答の会話の持ち主（メッセージ ID の無いチャットの評価。#3244）。
        self.chat_answer_owner: str | None = None
        self.detail: dict[str, object] | None = None

    async def get_search_answer_profile(self, search_answer_profile_id: str) -> object | None:
        return object() if self.view_exists else None

    async def save_feedback(
        self,
        payload: dict[str, object],
        *,
        details: dict[str, object] | None = None,
    ) -> str:
        self.user_id_hash = current_audit_request_context().user_id_hash
        self.saved.append(payload)
        self.saved_details.append(details)
        return "feedback-1"

    async def get_feedback_message_context(
        self,
        message_id: str,
        trace_id: str,
    ) -> dict[str, object] | None:
        self.message_lookup = (message_id, trace_id)
        return self.message_context

    async def get_chat_answer_owner_hash(self, trace_id: str) -> str | None:
        return self.chat_answer_owner

    async def list_current_feedback(self, trace_id: str) -> list[dict[str, object]]:
        self.current_trace = trace_id
        return self.current

    async def list_feedback_dashboard_rows(
        self, **_: object
    ) -> tuple[
        list[dict[str, object]],
        int,
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        return self.dashboard

    async def get_feedback_detail(self, feedback_id: str) -> dict[str, object] | None:
        return self.detail if feedback_id == "feedback-1" else None

    async def feedback_exists(self, feedback_id: str) -> bool:
        return feedback_id == "feedback-1" and self.detail is not None


def test_feedback_on_guide_preview_answers_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """業務ガイドの下書きで試した回答には、フィードバックを送れない（#1288）。"""
    from app.rag.support_guide_runtime import GUIDE_PREVIEW_TRACE_PREFIX

    fake = FakeFeedbackClient()
    monkeypatch.setattr(feedback_route, "OracleClient", lambda: fake)

    response = client.post(
        "/api/feedback",
        json={
            "trace_id": GUIDE_PREVIEW_TRACE_PREFIX + "0" * 32,
            "search_answer_profile_id": "bv-1",
            "target_type": "answer",
            "source_surface": "search",
            "rating": "helpful",
        },
    )

    assert response.status_code == 409
    assert "下書きで試した回答" in response.json()["error_messages"][0]
    assert fake.saved == []
