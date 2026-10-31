"""類似履歴 / few-shot の母集団取得が全履歴を読まないことの回帰テスト。

呼び出しは質問入力のデバウンスごと・job ごとに発生するため、管理者 GOOD の履歴だけを
DB 側で絞り、上限件数で止める(Issue: 類似履歴が毎回全履歴を DB からロードする)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql import service as service_module
from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    FeedbackRating,
    HistoryItem,
    Nl2SqlEngine,
    Nl2SqlProfile,
    SimilarHistoryRequest,
)
from app.features.nl2sql.service import Nl2SqlService, SimilarHistoryCandidate
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.domain import Principal
from app.security.permissions import QUERY_GENERATE_PERMISSION
from app.settings import get_settings


def _history(
    index: int,
    *,
    admin: FeedbackRating | None,
    profile_id: str = "default",
    question: str = "請求金額を確認したい",
    rewritten_question: str | None = None,
    generated_sql: str = "SELECT TOTAL_AMOUNT FROM APP.INVOICES",
) -> HistoryItem:
    return HistoryItem(
        id=f"hist-{index:03d}",
        question=question,
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        generated_sql=generated_sql,
        created_at=f"2026-09-02T00:00:{index:02d}+00:00",
        profile_id=profile_id,
        profile_name=f"{profile_id} profile",
        rewritten_question=rewritten_question if rewritten_question is not None else question,
        safety_is_safe=True,
        admin_feedback_rating=admin,
        admin_feedback_updated_at=f"2026-09-02T01:00:{index:02d}+00:00" if admin else "",
    )


def _incremental_service(repository: MemoryIncrementalNl2SqlRepository) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    return service


def _seed(repository: MemoryIncrementalNl2SqlRepository, items: list[HistoryItem]) -> None:
    for item in items:
        repository.put_document(
            "history",
            item.id,
            item.model_dump(mode="json"),
            profile_id=item.profile_id,
            status=item.feedback_rating.value if item.feedback_rating else "unrated",
        )


def _principal(allowed_profile_ids: set[str]) -> Principal:
    return Principal(
        user_uuid="user-1",
        login_user_id="user1",
        display_name="利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["ANALYST"],
        permissions={QUERY_GENERATE_PERMISSION},
        data_entitlements=[],
        allowed_profile_ids=allowed_profile_ids,
        session_id="session-1",
        csrf_token_hash="csrf",
    )


def _request(principal: Principal | None) -> Any:
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def is_configured(self) -> bool:
        return True

    def module_available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0 if index == 0 else 0.0 for index in range(1536)] for _text in texts]


class _RecordingOracleAdapter:
    def __init__(self) -> None:
        self.search_kwargs: dict[str, Any] | None = None

    def search_feedback_vector_index(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_kwargs = kwargs
        return [
            {"history_id": "hist-001", "profile_id": "sales", "score": 0.91},
            {"history_id": "hist-002", "profile_id": "finance", "score": 0.9},
            {"history_id": "hist-003", "profile_id": "hr", "score": 0.99},
        ]


def test_pool_is_filtered_to_admin_good_on_the_repository_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _seed(
        repository,
        [
            _history(1, admin=FeedbackRating.GOOD),
            _history(2, admin=None),
            _history(3, admin=FeedbackRating.BAD),
            _history(4, admin=FeedbackRating.GOOD),
        ],
    )
    service = _incremental_service(repository)
    seen_filters: list[dict[str, str] | None] = []
    original = repository.list_documents_page

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen_filters.append(dict(kwargs.get("payload_filters") or {}) or None)
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "list_documents_page", spy)
    monkeypatch.setattr(
        service,
        "_history_snapshot",
        lambda **_kwargs: pytest.fail("全履歴 snapshot を読んではならない"),
    )

    pool = service._similar_history_pool()  # noqa: SLF001

    assert sorted(item.id for item in pool) == ["hist-001", "hist-004"]
    assert seen_filters and all(
        filters == {"admin_feedback_rating": "good"} for filters in seen_filters
    )


def test_pool_stops_at_the_configured_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _seed(repository, [_history(index, admin=FeedbackRating.GOOD) for index in range(1, 8)])
    service = _incremental_service(repository)
    monkeypatch.setattr(service_module, "_SIMILAR_HISTORY_POOL_LIMIT", 3)

    pool = service._similar_history_pool()  # noqa: SLF001

    assert len(pool) == 3


def test_similar_history_only_surfaces_admin_good_items() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _seed(
        repository,
        [_history(1, admin=FeedbackRating.GOOD), _history(2, admin=None)],
    )
    service = _incremental_service(repository)

    data = service.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id=None, limit=5)
    )

    assert [entry.history_id for entry in data.items] == ["hist-001"]


def test_similar_history_filters_to_requested_profile() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD, profile_id="sales"),
        _history(2, admin=FeedbackRating.GOOD, profile_id="finance"),
    ]

    data = service.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id="sales", limit=5)
    )

    assert [entry.profile_id for entry in data.items] == ["sales"]


def test_similar_history_without_profile_filters_to_allowed_profiles() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD, profile_id="sales"),
        _history(2, admin=FeedbackRating.GOOD, profile_id="finance"),
    ]

    data = service.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id=None, limit=5),
        allowed_profile_ids={"sales"},
    )
    empty = service.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id=None, limit=5),
        allowed_profile_ids=set(),
    )

    assert [entry.profile_id for entry in data.items] == ["sales"]
    assert empty.items == []


def test_similar_history_route_scopes_empty_profile_to_principal_allowed_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD, profile_id="sales"),
        _history(2, admin=FeedbackRating.GOOD, profile_id="finance"),
    ]
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    response = nl2sql_router.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id=None, limit=5),
        _request(_principal({"sales"})),
    )

    assert response.data is not None
    assert [entry.profile_id for entry in response.data.items] == ["sales"]


def test_similar_history_response_projects_private_history_fields() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    # 最低スコアの既定（0.9。#1721）ではなく、候補の絞り込み・並びを確かめるため 0 にする。
    service._feedback_similarity_threshold = 0.0  # noqa: SLF001
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD, profile_id="sales").model_copy(
            update={
                "actor_user_uuid": "other-user",
                "feedback_comment": "利用者だけのコメント",
                "admin_feedback_content": "管理者レビュー詳細",
                "session_id": "session-secret",
                "rewritten_question": "書き換え済み質問",
            }
        )
    ]

    data = service.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id="sales", limit=5)
    )

    assert len(data.items) == 1
    payload = data.items[0].model_dump(mode="json")
    assert payload == {
        "history_id": "hist-001",
        "question": "請求金額を確認したい",
        "sql": "SELECT TOTAL_AMOUNT FROM APP.INVOICES",
        "profile_id": "sales",
        "profile_name": "sales profile",
        "score": data.items[0].score,
        "reason": data.items[0].reason,
    }
    assert "item" not in payload
    assert "actor_user_uuid" not in payload
    assert "feedback_comment" not in payload
    assert "admin_feedback_content" not in payload
    assert "session_id" not in payload
    assert "rewritten_question" not in payload


def test_few_shot_examples_do_not_cross_profiles() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD, profile_id="sales"),
        _history(2, admin=FeedbackRating.GOOD, profile_id="finance"),
    ]

    examples = service._learning_examples_for_generation(  # noqa: SLF001
        question="請求金額を確認したい",
        profile=Nl2SqlProfile(id="sales", name="sales profile"),
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
    )

    assert [example.history_id for example in examples] == ["hist-001"]


def test_similar_history_and_generation_share_threshold_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._feedback_similarity_threshold = 0.75  # noqa: SLF001
    service._feedback_match_limit = 2  # noqa: SLF001
    ranked = [
        SimilarHistoryCandidate(
            item=_history(1, admin=FeedbackRating.GOOD), score=0.92, reason="high"
        ),
        SimilarHistoryCandidate(
            item=_history(2, admin=FeedbackRating.GOOD), score=0.81, reason="mid"
        ),
        SimilarHistoryCandidate(
            item=_history(3, admin=FeedbackRating.GOOD), score=0.74, reason="low"
        ),
    ]
    monkeypatch.setattr(
        service,
        "_similar_history_candidates",
        lambda **_kwargs: ranked,
    )

    request = SimilarHistoryRequest(
        question="請求金額を確認したい",
        profile_id="default",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
    )
    data = service.similar_history(request)
    examples = service._learning_examples_for_generation(  # noqa: SLF001
        question=request.question,
        profile=Nl2SqlProfile(id="default", name="default profile"),
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
    )

    assert data.used_for_generation is True
    assert [entry.history_id for entry in data.items] == ["hist-001", "hist-002"]
    assert [example.history_id for example in examples] == ["hist-001", "hist-002"]


def test_similar_history_generation_reserves_profile_few_shot_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._feedback_similarity_threshold = 0.0  # noqa: SLF001
    service._feedback_match_limit = 3  # noqa: SLF001
    ranked = [
        SimilarHistoryCandidate(
            item=_history(1, admin=FeedbackRating.GOOD), score=0.92, reason="1"
        ),
        SimilarHistoryCandidate(
            item=_history(2, admin=FeedbackRating.GOOD), score=0.91, reason="2"
        ),
        SimilarHistoryCandidate(item=_history(3, admin=FeedbackRating.GOOD), score=0.9, reason="3"),
    ]
    profile = Nl2SqlProfile(
        id="default",
        name="default profile",
        few_shot_examples=[
            {"question": "固定例1", "sql": "SELECT 1 FROM DUAL"},
            {"question": "固定例2", "sql": "SELECT 2 FROM DUAL"},
            {"question": "固定例3", "sql": "SELECT 3 FROM DUAL"},
        ],
    )
    monkeypatch.setattr(
        service,
        "_similar_history_candidates",
        lambda **_kwargs: ranked,
    )
    monkeypatch.setattr(service, "get_profile", lambda _profile_id=None: profile)

    data = service.similar_history(
        SimilarHistoryRequest(
            question="請求金額を確認したい",
            profile_id="default",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        )
    )
    examples = service._learning_examples_for_generation(  # noqa: SLF001
        question="請求金額を確認したい",
        profile=profile,
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
    )

    assert [entry.history_id for entry in data.items] == ["hist-001", "hist-002"]
    assert [example.source for example in examples] == [
        "profile_few_shot",
        "profile_few_shot",
        "profile_few_shot",
        "similar_history",
        "similar_history",
    ]
    assert [example.history_id for example in examples if example.history_id] == [
        "hist-001",
        "hist-002",
    ]


def test_select_ai_engines_do_not_surface_similar_history_for_few_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    profile = Nl2SqlProfile(
        id="default",
        name="default profile",
        few_shot_examples=[{"question": "固定例", "sql": "SELECT 1 FROM DUAL"}],
    )
    monkeypatch.setattr(service, "get_profile", lambda _profile_id=None: profile)
    monkeypatch.setattr(
        service,
        "_similar_history_candidates",
        lambda **_kwargs: pytest.fail("Select AI 系では類似履歴を few-shot 候補化しない"),
    )

    for engine in (Nl2SqlEngine.SELECT_AI, Nl2SqlEngine.SELECT_AI_AGENT):
        data = service.similar_history(
            SimilarHistoryRequest(
                question="請求金額を確認したい",
                profile_id="default",
                engine=engine,
            )
        )
        examples = service._learning_examples_for_generation(  # noqa: SLF001
            question="請求金額を確認したい",
            profile=profile,
            engine=engine,
        )

        assert data.items == []
        assert data.used_for_generation is False
        assert data.engine == engine
        assert examples == []


def test_oracle_vector_history_receives_allowed_profile_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_feedback_embedding_enabled", True)
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = _RecordingOracleAdapter()
    service._oracle_adapter = cast(Any, adapter)  # noqa: SLF001
    service._embedding_client = cast(Any, _FakeEmbeddingClient())  # noqa: SLF001
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD, profile_id="sales"),
        _history(2, admin=FeedbackRating.GOOD, profile_id="finance"),
        _history(3, admin=FeedbackRating.GOOD, profile_id="hr"),
    ]

    data = service.similar_history(
        SimilarHistoryRequest(question="請求金額を確認したい", profile_id=None, limit=5),
        allowed_profile_ids={"sales", "finance"},
    )

    assert adapter.search_kwargs is not None
    assert adapter.search_kwargs["profile_ids"] == {"sales", "finance"}
    assert {entry.profile_id for entry in data.items} == {"sales", "finance"}


def test_oracle_vector_history_embeds_template_values_without_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_feedback_embedding_enabled", True)
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = _RecordingOracleAdapter()
    embedding_client = _FakeEmbeddingClient()
    service._oracle_adapter = cast(Any, adapter)  # noqa: SLF001
    service._embedding_client = cast(Any, embedding_client)  # noqa: SLF001
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD),
    ]

    service.similar_history(
        SimilarHistoryRequest(
            question="対象テーブル：\n抽出項目：従業員情報\n抽出条件：",
            profile_id="default",
            limit=5,
        )
    )

    assert embedding_client.texts[0] == "従業員情報"


def test_feedback_embedding_text_ignores_question_template_labels() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    text = service._feedback_embedding_text(  # noqa: SLF001
        _history(
            1,
            admin=FeedbackRating.GOOD,
            question="対象テーブル：\n抽出項目：従業員情報\n抽出条件：",
            generated_sql="SELECT EMPLOYEE_NAME FROM APP.EMPLOYEE",
        )
    )

    assert "question: 従業員情報" in text
    assert "対象テーブル" not in text
    assert "抽出項目" not in text
    assert "抽出条件" not in text


def test_oracle_vector_empty_template_does_not_embed_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_feedback_embedding_enabled", True)
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    embedding_client = _FakeEmbeddingClient()
    service._oracle_adapter = cast(Any, _RecordingOracleAdapter())  # noqa: SLF001
    service._embedding_client = cast(Any, embedding_client)  # noqa: SLF001
    service._history = [  # noqa: SLF001
        _history(
            1,
            admin=FeedbackRating.GOOD,
            question="対象テーブル：\n抽出項目：\n抽出条件：",
            generated_sql="SELECT 1 FROM DUAL",
        ),
    ]

    data = service.similar_history(
        SimilarHistoryRequest(
            question="対象テーブル：\n抽出項目：\n抽出条件：",
            profile_id="default",
            limit=5,
        )
    )

    assert embedding_client.texts == []
    assert data.items == []


def test_similar_history_ignores_question_template_labels_for_scoring() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    # 最低スコアの既定（0.9。#1721）ではなく、候補の絞り込み・並びを確かめるため 0 にする。
    service._feedback_similarity_threshold = 0.0  # noqa: SLF001
    service._history = [  # noqa: SLF001
        _history(
            1,
            admin=FeedbackRating.GOOD,
            question="対象テーブル：\n抽出項目：部署情報\n抽出条件：",
            generated_sql="SELECT DEPARTMENT_NAME FROM APP.DEPARTMENT",
        ),
        _history(
            2,
            admin=FeedbackRating.GOOD,
            question="対象テーブル：\n抽出項目：\n抽出条件：",
            generated_sql="SELECT 1 FROM DUAL",
        ),
        _history(
            3,
            admin=FeedbackRating.GOOD,
            question="従業員情報から氏名と入社日を確認したい",
            generated_sql="SELECT EMPLOYEE_NAME, HIRE_DATE FROM APP.EMPLOYEE",
        ),
    ]

    data = service.similar_history(
        SimilarHistoryRequest(
            question="対象テーブル：\n抽出項目：従業員情報\n抽出条件：",
            profile_id="default",
            limit=5,
        )
    )

    assert [entry.history_id for entry in data.items[:2]] == ["hist-003", "hist-001"]
    scores = {entry.history_id: entry.score for entry in data.items}
    assert scores["hist-003"] > scores["hist-001"]
    assert "hist-002" not in scores
    reasons = " ".join(entry.reason for entry in data.items)
    assert "対象テーブル" not in reasons
    assert "抽出項目" not in reasons
    assert "抽出条件" not in reasons


def test_empty_question_template_does_not_match_similar_history_by_labels() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(
            1,
            admin=FeedbackRating.GOOD,
            question="対象テーブル：\n抽出項目：\n抽出条件：",
            generated_sql="SELECT 1 FROM DUAL",
        ),
        _history(
            2,
            admin=FeedbackRating.GOOD,
            question='対象テーブル："部署情報"\n抽出項目：\n抽出条件：',
            generated_sql="SELECT DEPARTMENT_NAME FROM APP.DEPARTMENT",
        ),
    ]

    data = service.similar_history(
        SimilarHistoryRequest(
            question="対象テーブル：\n抽出項目：\n抽出条件：",
            profile_id="default",
            limit=5,
        )
    )

    assert data.items == []


def test_memory_pool_matches_repository_semantics() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD),
        _history(2, admin=None),
    ]

    pool = service._similar_history_pool()  # noqa: SLF001

    assert [item.id for item in pool] == ["hist-001"]


def test_history_page_payload_filters_apply_in_memory_mode() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [  # noqa: SLF001
        _history(1, admin=FeedbackRating.GOOD),
        _history(2, admin=FeedbackRating.BAD),
    ]

    items, _cursor, total = service._history_page(  # noqa: SLF001
        cursor=None,
        limit=10,
        payload_filters={"admin_feedback_rating": "bad"},
    )

    assert total == 1
    assert [item.id for item in items] == ["hist-002"]
