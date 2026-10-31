"""GET /nl2sql/history の offset pagination（任意のページへ移る。#1891）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from pr_backend_core.api import OffsetParams

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.incremental_store import (
    HISTORY_QUESTION_SORT_PREFIX_CHARS,
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
)
from app.features.nl2sql.japanese_collation import japanese_collation_key
from app.features.nl2sql.models import FeedbackRating, HistoryData, HistoryItem, Nl2SqlEngine
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.domain import SYSTEM_ADMIN_ROLE_CODE, Principal, UserIdentity, UserRecord
from app.security.service import SecurityApiError, SecurityService
from app.security.store import InMemorySecurityStore, OracleSecurityStore
from app.settings import Settings


@pytest.fixture(autouse=True)
def history_security(monkeypatch: pytest.MonkeyPatch) -> SecurityService:
    security = SecurityService(InMemorySecurityStore(), Settings())
    monkeypatch.setattr(nl2sql_router, "get_security_service", lambda: security)
    return security


def _paging(offset: int = 0, limit: int = 50) -> OffsetParams:
    """router を直接呼ぶときの offset / limit（HTTP では offset_params の依存が作る）。"""
    return OffsetParams(limit=limit, offset=offset)


def _history(index: int, actor: str) -> HistoryItem:
    return HistoryItem(
        id=f"hist-{index:03d}",
        question=f"質問 {index}",
        engine=Nl2SqlEngine.SELECT_AI,
        generated_sql=f"SELECT {index} FROM DUAL",
        created_at=f"2026-09-02T00:00:{index:02d}+00:00",
        profile_id="default",
        actor_user_uuid=actor,
    )


def _memory_service(items: list[HistoryItem]) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = list(items)  # noqa: SLF001 - white-box contract test
    return service


def _incremental_service(items: list[HistoryItem]) -> Nl2SqlService:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    for item in items:
        repository.put_document(
            "history",
            item.id,
            item.model_dump(mode="json"),
            profile_id=item.profile_id,
            status=item.feedback_rating.value if item.feedback_rating else "unrated",
        )
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    return service


def _principal(*, admin: bool, user_uuid: str = "user-1") -> Principal:
    return Principal(
        user_uuid=user_uuid,
        login_user_id=user_uuid,
        display_name="利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE] if admin else ["ANALYST"],
        permissions={"menu.history"},
        data_entitlements=[],
        allowed_profile_ids={"default"},
        session_id="session-1",
        csrf_token_hash="csrf",
    )


def _request(principal: Principal | None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


_ITEMS = [
    _history(1, "user-1"),
    _history(2, "user-2"),
    _history(3, "user-1"),
    _history(4, "user-2"),
    _history(5, "user-1"),
]


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
def test_list_history_pages_through_all_items(factory: object) -> None:
    service = factory(_ITEMS)  # type: ignore[operator]

    first = service.list_history(limit=2)
    assert [item.id for item in first.items] == ["hist-005", "hist-004"]
    assert (first.total, first.limit, first.offset, first.has_next) == (5, 2, 0, True)

    second = service.list_history(offset=2, limit=2)
    assert [item.id for item in second.items] == ["hist-003", "hist-002"]
    assert second.has_next

    # 最後のページへ直接移れる（旧実装は 50 件固定で、51 件目以降には到達できなかった）。
    third = service.list_history(offset=4, limit=2)
    assert [item.id for item in third.items] == ["hist-001"]
    assert third.has_next is False

    # 末尾を越えた offset は空のページと正しい全件数（画面が最後のページへ戻す。#1891）。
    beyond = service.list_history(offset=10, limit=2)
    assert beyond.items == []
    assert (beyond.total, beyond.offset, beyond.has_next) == (5, 10, False)


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
def test_list_history_keeps_actor_filter_across_pages(factory: object) -> None:
    service = factory(_ITEMS)  # type: ignore[operator]

    first = service.list_history(viewer_user_uuid="user-1", all_users=False, limit=2)
    assert [item.id for item in first.items] == ["hist-005", "hist-003"]
    assert first.total == 3
    assert first.unfiltered_total == 3

    second = service.list_history(viewer_user_uuid="user-1", all_users=False, offset=2, limit=2)
    assert [item.id for item in second.items] == ["hist-001"]
    assert second.has_next is False


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
def test_list_history_filters_query_rating_and_safety_before_paging(factory: object) -> None:
    items = [
        _history(1, "user-1").model_copy(
            update={
                "feedback_rating": FeedbackRating.GOOD,
                "feedback_comment": "期待通り",
                "safety_is_safe": True,
            }
        ),
        _history(2, "user-1").model_copy(
            update={
                "question": "監査ログを削除",
                "generated_sql": "DELETE FROM AUDIT_LOG",
                "feedback_rating": None,
                "safety_is_safe": False,
            }
        ),
        _history(3, "user-2").model_copy(
            update={
                "question": "監査ログを確認",
                "generated_sql": "SELECT * FROM AUDIT_LOG",
                "feedback_rating": None,
                "safety_is_safe": False,
            }
        ),
    ]
    service = factory(items)  # type: ignore[operator]

    page = service.list_history(
        viewer_user_uuid="user-1",
        all_users=False,
        limit=10,
        query="AUDIT_LOG",
        rating="unrated",
        safety="blocked",
    )

    assert [item.id for item in page.items] == ["hist-002"]
    assert page.total == 1
    assert page.has_next is False
    # 「全 M 件」は絞り込みを掛けない、見られる範囲（本人の履歴）の件数。
    assert page.unfiltered_total == 2


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
def test_list_history_unfiltered_total_skips_the_extra_count_without_filters(
    factory: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """絞り込みが無いときは全件数をもう一度数えない（`unfiltered_total` は `total` と同じ）。"""
    service = factory(_ITEMS)  # type: ignore[operator]
    count = MagicMock(side_effect=AssertionError("絞り込みが無いのに件数を数え直した"))
    monkeypatch.setattr(service, "_history_count", count)

    page = service.list_history(viewer_user_uuid="user-1", all_users=False, limit=2)

    assert page.total == page.unfiltered_total == 3
    count.assert_not_called()


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
@pytest.mark.parametrize(
    ("filters", "expected_total"),
    [
        ({"query": "質問 1"}, 1),
        ({"rating": "unrated"}, 3),
        ({"safety": "blocked"}, 0),
    ],
    ids=["query", "rating", "safety"],
)
def test_list_history_unfiltered_total_ignores_each_user_filter(
    factory: object, filters: dict[str, str], expected_total: int
) -> None:
    service = factory(_ITEMS)  # type: ignore[operator]

    page = service.list_history(viewer_user_uuid="user-1", all_users=False, limit=2, **filters)

    assert page.total == expected_total
    assert page.unfiltered_total == 3


def test_list_history_clamps_limit() -> None:
    service = _memory_service([_history(index, "user-1") for index in range(1, 260)])

    page = service.list_history(limit=10_000)

    assert len(page.items) == 200
    assert page.has_next


def test_history_route_scopes_non_admin_and_passes_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _memory_service(_ITEMS)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    own = nl2sql_router.history(_request(_principal(admin=False)), _paging(limit=2))  # type: ignore[arg-type]
    assert own.data is not None
    assert [item.id for item in own.data.items] == ["hist-005", "hist-003"]
    assert own.data.total == 3

    own_rest = nl2sql_router.history(
        _request(_principal(admin=False)),  # type: ignore[arg-type]
        _paging(2, 2),
    )
    assert own_rest.data is not None
    assert [item.id for item in own_rest.data.items] == ["hist-001"]

    everyone = nl2sql_router.history(_request(_principal(admin=True)), _paging(limit=200))  # type: ignore[arg-type]
    assert everyone.data is not None
    assert everyone.data.total == 5
    assert len(everyone.data.items) == 5

    unauthenticated = nl2sql_router.history(_request(None), _paging())  # type: ignore[arg-type]
    assert unauthenticated.data is not None
    assert unauthenticated.data.total == 5


def test_history_route_accepts_server_side_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _memory_service(
        [
            _history(1, "user-1").model_copy(
                update={
                    "question": "請求金額を確認",
                    "feedback_rating": FeedbackRating.BAD,
                    "safety_is_safe": True,
                }
            ),
            _history(2, "user-1").model_copy(
                update={
                    "question": "監査ログを削除",
                    "generated_sql": "DELETE FROM AUDIT_LOG",
                    "feedback_rating": None,
                    "safety_is_safe": False,
                }
            ),
        ]
    )
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    blocked = nl2sql_router.history(
        _request(_principal(admin=False)),  # type: ignore[arg-type]
        _paging(limit=10),
        q="AUDIT_LOG",
        rating="unrated",
        safety="blocked",
    )

    assert blocked.data is not None
    assert [item.id for item in blocked.data.items] == ["hist-002"]


@pytest.mark.parametrize(
    ("rating", "safety"),
    [
        ("needs_review", "all"),
        ("all", "unknown"),
    ],
)
def test_history_route_rejects_invalid_filters(rating: str, safety: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.history(
            _request(_principal(admin=True)),  # type: ignore[arg-type]
            _paging(),
            rating=rating,
            safety=safety,
        )
    assert exc_info.value.status_code == 422


_SORT_ITEMS = [
    _history(1, "user-1").model_copy(update={"question": "c 在庫の確認"}),
    _history(2, "user-1").model_copy(update={"question": "a 売上の確認"}),
    _history(3, "user-1").model_copy(update={"question": "e 請求の確認"}),
    _history(4, "user-1").model_copy(update={"question": "b 顧客の確認"}),
    _history(5, "user-1").model_copy(update={"question": "d 部署の確認"}),
]


def _all_pages(service: Nl2SqlService, *, sort_key: str, direction: str) -> list[list[str]]:
    pages: list[list[str]] = []
    offset = 0
    while True:
        page = service.list_history(offset=offset, limit=2, sort_key=sort_key, direction=direction)
        pages.append([item.id for item in page.items])
        assert page.total == 5
        if not page.has_next:
            return pages
        offset += page.limit


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
@pytest.mark.parametrize(
    ("sort_key", "direction", "expected"),
    [
        (
            "question",
            "asc",
            [["hist-002", "hist-004"], ["hist-001", "hist-005"], ["hist-003"]],
        ),
        (
            "question",
            "desc",
            [["hist-003", "hist-005"], ["hist-001", "hist-004"], ["hist-002"]],
        ),
        (
            "created_at",
            "asc",
            [["hist-001", "hist-002"], ["hist-003", "hist-004"], ["hist-005"]],
        ),
        (
            "created_at",
            "desc",
            [["hist-005", "hist-004"], ["hist-003", "hist-002"], ["hist-001"]],
        ),
    ],
    ids=["question-asc", "question-desc", "created-asc", "created-desc"],
)
def test_list_history_sorts_on_the_server_across_pages(
    factory: object, sort_key: str, direction: str, expected: list[list[str]]
) -> None:
    """並べ替えはページごとではなく全件で行い、続きのページも同じ順にする（#1764）。"""
    service = factory(_SORT_ITEMS)  # type: ignore[operator]

    assert _all_pages(service, sort_key=sort_key, direction=direction) == expected


# 実 ADB（JAPANESE_M）で確かめた順と同じ質問・同じ期待の順
# （tests/test_nl2sql_history_sort_oracle_live.py の _ROWS / _QUESTION_ASC。#1777）。
_LONG_PREFIX = "長" * (HISTORY_QUESTION_SORT_PREFIX_CHARS + 50)
_JAPANESE_SORT_ITEMS = [
    _history(index, "user-1").model_copy(update={"question": question})
    for index, question in [
        (1, "顧客の確認"),
        (2, "abc の売上"),
        (3, f"注文 {_LONG_PREFIX} 1"),
        (4, "顧客の確認"),
        (5, "がっこうの数"),
        (6, f"注文 {_LONG_PREFIX} 2"),
        (7, "かきの在庫"),
        (8, "アイスの売上"),
    ]
]
_JAPANESE_QUESTION_ASC = [
    "hist-002",
    "hist-008",
    "hist-007",
    "hist-005",
    "hist-001",
    "hist-004",
    "hist-003",
    "hist-006",
]


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_memory_history_sort_matches_the_oracle_japanese_order(
    factory: object, direction: str
) -> None:
    """質問の並べ替えは日本語の辞書順で、2 件ずつのページをたどっても抜け・重なりがない（#1777）。

    binary（文字コード）の順では、カタカナ（アイス）がひらがなの後、「顧客」が「注文」の後になる。
    """
    service = factory(_JAPANESE_SORT_ITEMS)  # type: ignore[operator]
    ids: list[str] = []
    for page_index in range(10):
        page = service.list_history(
            offset=page_index * 2, limit=2, sort_key="question", direction=direction
        )
        assert page.total == len(_JAPANESE_SORT_ITEMS)
        ids.extend(item.id for item in page.items)
        if not page.has_next:
            break
    expected = _JAPANESE_QUESTION_ASC if direction == "asc" else _JAPANESE_QUESTION_ASC[::-1]
    assert ids == expected
    # 任意のページへ直接移っても同じ順（3 ページ目だけを読む）。
    third = service.list_history(offset=4, limit=2, sort_key="question", direction=direction)
    assert [item.id for item in third.items] == expected[4:6]


@pytest.mark.parametrize(
    "expected",
    [
        # 実 ADB の NLSSORT(..., 'NLS_SORT=JAPANESE_M') の順（記号を除く）。
        [
            "123",
            "１２３",
            "9",
            "abc",
            "Abc",
            "ABC",
            "zeta",
            "ｚｅｔａ",
            "Ω",
            "дом",
            "ぁ",
            "あ",
            "ア",
            "あいう",
            "アイウ",
            "ｱｲｳ",
            "いろは",
            "イロハ",
            "う",
            "ゔ",
            "かき",
            "カキ",
            "がき",
            "きく",
            "さくら",
            "ー",
            "愛知",
            "顧客",
            "桜",
            "請求金額",
            "大阪",
            "注文",
            "長長",
            "東京",
            "売上",
            "部署",
        ]
    ],
    ids=["oracle-japanese-m"],
)
def test_japanese_collation_key_follows_the_oracle_japanese_m_order(expected: list[str]) -> None:
    shuffled = list(reversed(expected))
    assert sorted(shuffled, key=japanese_collation_key) == expected


def test_history_route_passes_sort_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", _memory_service(_SORT_ITEMS))

    page = nl2sql_router.history(
        _request(_principal(admin=True)),  # type: ignore[arg-type]
        _paging(limit=2),
        sort="question",
        direction="asc",
    )

    assert page.data is not None
    assert [item.id for item in page.data.items] == ["hist-002", "hist-004"]


@pytest.mark.parametrize(
    ("sort", "direction"),
    [("profile", "asc"), ("question", "up")],
    ids=["unknown-sort", "unknown-direction"],
)
def test_history_route_rejects_invalid_sort(sort: str, direction: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.history(
            _request(_principal(admin=True)),  # type: ignore[arg-type]
            _paging(),
            sort=sort,
            direction=direction,
        )
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/nl2sql/history",
        "/api/nl2sql/feedback",
        "/api/nl2sql/classifier/training-candidates",
        "/api/nl2sql/quality-evaluations",
        "/api/nl2sql/quality-evaluations/missing-job/results",
        "/api/nl2sql/chats",
        "/api/nl2sql/select-ai/feedback?profile_name=DEFAULT",
    ],
    ids=[
        "history",
        "feedback",
        "candidates",
        "quality-jobs",
        "quality-results",
        "chats",
        "select-ai-feedback",
    ],
)
@pytest.mark.parametrize(
    "query", ["limit=101", "limit=0", "offset=-1"], ids=["over-max", "zero", "negative-offset"]
)
async def test_offset_list_routes_reject_out_of_range_paging(path: str, query: str) -> None:
    """一覧の `limit` は 1〜100、`offset` は 0 以上。外れた値は黙って丸めず 422（#1891）。"""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        separator = "&" if "?" in path else "?"
        response = await client.get(f"{path}{separator}{query}")
    assert response.status_code == 422, response.text


async def test_select_ai_feedback_route_returns_the_offset_page_with_default_limit() -> None:
    """Select AI feedback も共通の `Page` と `unfiltered_total`。既定の limit は 25（#1891）。"""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/nl2sql/select-ai/feedback?profile_name=DEFAULT")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {"items", "total", "limit", "offset", "has_next", "unfiltered_total"} <= set(data)
    assert (data["limit"], data["offset"]) == (25, 0)


async def test_history_route_returns_the_offset_page_shape() -> None:
    """応答は共通の `Page`（items / total / limit / offset / has_next）と `unfiltered_total`。"""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/nl2sql/history?limit=100&offset=5")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {"items", "total", "limit", "offset", "has_next", "unfiltered_total"} <= set(data)
    assert "next_cursor" not in data
    assert (data["limit"], data["offset"]) == (100, 5)


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
def test_history_executor_identity_is_admin_only_and_does_not_mutate_history(
    factory: object, monkeypatch: pytest.MonkeyPatch, history_security: SecurityService
) -> None:
    store = history_security.store
    for index in [1, 2]:
        store.create_user(
            UserRecord(
                user_uuid=f"user-{index}",
                login_user_id=f"analyst{index}",
                display_name="同じ表示名",
                password_hash="not-a-real-hash",
                status="ACTIVE" if index == 1 else "DISABLED",
                force_password_change=False,
                failed_login_count=0,
                locked_until=None,
                version=1,
            )
        )
    original = [
        _history(1, "user-1"),
        _history(2, "user-2"),
        _history(3, "deleted"),
        _history(4, ""),
    ]
    service = factory(original)  # type: ignore[operator]
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    before = service.list_history().model_dump()
    first = nl2sql_router.history(_request(_principal(admin=True)), _paging(limit=2)).data  # type: ignore[arg-type]
    assert first is not None
    assert [(item.actor_user_uuid, item.actor_login_user_id) for item in first.items] == [
        ("", ""),
        ("deleted", ""),
    ]
    second = nl2sql_router.history(
        _request(_principal(admin=True)),  # type: ignore[arg-type]
        _paging(2, 2),
    ).data
    assert second is not None
    assert [(item.actor_login_user_id, item.actor_display_name) for item in second.items] == [
        ("analyst2", "同じ表示名"),
        ("analyst1", "同じ表示名"),
    ]
    assert "password_hash" not in second.model_dump_json()
    assert service.list_history().model_dump() == before
    user = store.get_user("user-1")
    assert user is not None
    store.update_user(
        user.user_uuid,
        expected_version=user.version,
        display_name="変更後の名前",
        status=user.status,
        role_ids=user.role_ids,
    )
    renamed = nl2sql_router.history(_request(_principal(admin=True)), _paging()).data  # type: ignore[arg-type]
    assert renamed is not None
    assert renamed.items[-1].actor_display_name == "変更後の名前"
    store.delete_user("user-2", expected_version=1)
    after_delete = nl2sql_router.history(_request(_principal(admin=True)), _paging()).data  # type: ignore[arg-type]
    assert after_delete is not None
    deleted = next(item for item in after_delete.items if item.actor_user_uuid == "user-2")
    assert deleted.actor_login_user_id == deleted.actor_display_name == ""
    assert service.list_history().model_dump() == before

    looked_up: list[list[str]] = []
    lookup = store.get_user_identities

    def recording_lookup(ids: list[str]) -> dict[str, UserIdentity]:
        looked_up.append(sorted(ids))
        return lookup(ids)

    monkeypatch.setattr(store, "get_user_identities", recording_lookup)
    own = nl2sql_router.history(_request(_principal(admin=False)), _paging()).data  # type: ignore[arg-type]
    assert own is not None
    assert [item.actor_user_uuid for item in own.items] == ["user-1"]
    # 一般の利用者には、見られる行の実行した人の表示名だけを返す（ログイン ID と管理者向けの
    # 項目は返さない。#1954）。他の利用者の名前は引かない。
    assert own.items[0].actor_display_name == own.items[0].actor_login_user_id == ""
    assert own.items[0].executor_display_name == "変更後の名前"
    assert own.items[0].executed_by_me is True
    assert looked_up == [["user-1"]]
    with pytest.raises(SecurityApiError) as exc:
        history_security.history_user_identities(_principal(admin=False), ["user-2"])
    assert exc.value.status_code == 403


def test_oracle_history_user_identities_batch_only_public_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OracleSecurityStore(Settings.model_construct())
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("user-1", "analyst1", "利用者")]
    context = MagicMock()
    context.__enter__.return_value = connection
    connect = MagicMock(return_value=context)
    monkeypatch.setattr(store, "connection", connect)
    assert store.get_user_identities([]) == {}
    connect.assert_not_called()
    identities = store.get_user_identities(["user-1", "user-1", "missing", ""])
    assert identities == {"user-1": UserIdentity("user-1", "analyst1", "利用者")}
    cursor.execute.assert_called_once()
    sql, binds = cursor.execute.call_args.args
    assert sql.startswith("SELECT USER_UUID, LOGIN_USER_ID, DISPLAY_NAME FROM PLATFORM_USERS ")
    assert set(binds.values()) == {"user-1", "missing"}
    assert "user-1" not in sql
    assert "PASSWORD" not in sql and "ROLE" not in sql


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
def test_history_resolves_configured_admin_without_changing_saved_identity(
    factory: object, monkeypatch: pytest.MonkeyPatch, history_security: SecurityService
) -> None:
    configured_uuid = "00000000-0000-0000-0000-000000000002"
    original = [_history(1, configured_uuid), _history(2, "unknown"), _history(3, "")]
    service = factory(original)  # type: ignore[operator]
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    before = service.list_history().model_dump()
    for viewer in [_principal(admin=True), _principal(admin=True, user_uuid=configured_uuid)]:
        data = nl2sql_router.history(_request(viewer), _paging()).data  # type: ignore[arg-type]
        assert data is not None
        items = {item.actor_user_uuid: item for item in data.items}
        assert items[configured_uuid].actor_login_user_id == "system_admin"
        assert items[configured_uuid].actor_display_name == "システム管理者"
        for user_uuid in ["unknown", ""]:
            assert items[user_uuid].actor_login_user_id == ""
            assert items[user_uuid].actor_display_name == ""
        assert service.list_history().model_dump() == before


def test_configured_admin_history_identity_uses_no_credentials_or_auth_tables(
    monkeypatch: pytest.MonkeyPatch, history_security: SecurityService
) -> None:
    configured_uuid = "00000000-0000-0000-0000-000000000002"
    lookup = MagicMock(side_effect=AssertionError("auth tables must not be read"))
    credentials = MagicMock(side_effect=AssertionError("credentials must not be read"))
    monkeypatch.setattr(history_security.store, "get_user_identities", lookup)
    monkeypatch.setattr(history_security, "_configured_system_admin_credentials", credentials)
    admin = _principal(admin=True)
    assert history_security.history_user_identities(admin, []) == {}
    expected = UserIdentity(configured_uuid, "system_admin", "システム管理者")
    assert history_security.history_user_identities(
        admin, [configured_uuid, configured_uuid, ""]
    ) == {configured_uuid: expected}
    with pytest.raises(SecurityApiError) as exc:
        history_security.history_user_identities(_principal(admin=False), [configured_uuid])
    assert exc.value.status_code == 403
    lookup.assert_not_called()
    credentials.assert_not_called()


def test_history_identity_resolves_only_reserved_uuid_and_keeps_other_users(
    monkeypatch: pytest.MonkeyPatch, history_security: SecurityService
) -> None:
    configured_uuid = "00000000-0000-0000-0000-000000000002"
    ordinary = UserIdentity("user-1", "another_admin", "別の管理者")
    lookup = MagicMock(return_value={ordinary.user_uuid: ordinary})
    monkeypatch.setattr(history_security.store, "get_user_identities", lookup)
    result = history_security.history_user_identities(
        _principal(admin=True), [configured_uuid, "user-1", "missing", ""]
    )
    lookup.assert_called_once_with(["user-1", "missing"])
    assert result == {
        "user-1": ordinary,
        configured_uuid: UserIdentity(configured_uuid, "system_admin", "システム管理者"),
    }


# 実行した人と会話の持ち主（#1954）。hist-001: alice が自分の会話で実行、hist-002: admin が
# alice の会話で実行、hist-003: admin が会話の無い実行、hist-004: bob、hist-005: 記録の無い行。
_RUN_ITEMS = [
    _history(1, "alice").model_copy(update={"conversation_owner_user_uuid": "alice"}),
    _history(2, "admin").model_copy(update={"conversation_owner_user_uuid": "alice"}),
    _history(3, "admin"),
    _history(4, "bob").model_copy(update={"conversation_owner_user_uuid": "bob"}),
    _history(5, ""),
]


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service])
@pytest.mark.parametrize(
    ("viewer", "all_users", "executor", "expected", "unfiltered"),
    [
        ("alice", False, "all", ["hist-002", "hist-001"], 2),
        ("alice", False, "mine", ["hist-001"], 2),
        ("alice", False, "others", ["hist-002"], 2),
        ("bob", False, "all", ["hist-004"], 1),
        ("bob", False, "others", [], 1),
        ("carol", False, "all", [], 0),
        ("admin", True, "all", ["hist-005", "hist-004", "hist-003", "hist-002", "hist-001"], 5),
        ("admin", True, "mine", ["hist-003", "hist-002"], 5),
        # 実行した人の記録の無い行は「他の人が実行」に入る（`run_executor_matches` と同じ）。
        ("admin", True, "others", ["hist-005", "hist-004", "hist-001"], 5),
        # 利用者の無い要求（認証無効）は、すべての行を自分の実行とみなす。
        ("", True, "mine", ["hist-005", "hist-004", "hist-003", "hist-002", "hist-001"], 5),
        ("", True, "others", [], 5),
    ],
    ids=[
        "alice-all",
        "alice-mine",
        "alice-others",
        "bob-all",
        "bob-others",
        "stranger",
        "admin-all",
        "admin-mine",
        "admin-others",
        "anonymous-mine",
        "anonymous-others",
    ],
)
def test_list_history_shows_runs_to_executor_and_conversation_owner(
    factory: object,
    viewer: str,
    all_users: bool,
    executor: str,
    expected: list[str],
    unfiltered: int,
) -> None:
    service = factory(_RUN_ITEMS)  # type: ignore[operator]

    page = service.list_history(
        viewer_user_uuid=viewer,
        all_users=all_users,
        executor=executor,
        limit=10,
    )

    assert [item.id for item in page.items] == expected
    assert page.total == len(expected)
    assert page.unfiltered_total == unfiltered


def test_list_history_rejects_unknown_executor_filter() -> None:
    with pytest.raises(ValueError, match="executor"):
        _memory_service(_RUN_ITEMS).list_history(executor="everyone")  # type: ignore[arg-type]


def test_oracle_history_visibility_is_one_sql_condition() -> None:
    """Oracle の一覧・件数は、見え方（実行した人か会話の持ち主）と「他の人が実行」を SQL で絞る。

    値は bind で渡し、SQL に埋め込まない。実行した人の記録の無い行は「他の人が実行」に入る。
    """
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0,)
    cursor.fetchall.return_value = []
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = OracleIncrementalNl2SqlRepository(connection_factory=lambda: context)

    repository.list_documents_page(
        "history",
        cursor=None,
        offset=0,
        limit=10,
        payload_any_filters={
            "actor_user_uuid": "alice",
            "conversation_owner_user_uuid": "alice",
        },
        payload_exclude_filters={"actor_user_uuid": "alice"},
    )

    count_sql, count_binds = cursor.execute.call_args_list[0].args
    page_sql, page_binds = cursor.execute.call_args_list[1].args
    actor = "JSON_VALUE(PAYLOAD_JSON, '$.actor_user_uuid' RETURNING VARCHAR2(4000) NULL ON ERROR)"
    owner = (
        "JSON_VALUE(PAYLOAD_JSON, '$.conversation_owner_user_uuid' "
        "RETURNING VARCHAR2(4000) NULL ON ERROR)"
    )
    for sql, binds in ((count_sql, count_binds), (page_sql, page_binds)):
        assert f"({actor} = :payload_any_0 OR {owner} = :payload_any_1)" in sql
        assert f"({actor} IS NULL OR {actor} <> :payload_exclude_0)" in sql
        assert "alice" not in sql
        assert binds["payload_any_0"] == binds["payload_any_1"] == "alice"
        assert binds["payload_exclude_0"] == "alice"

    # 値がすべて空の OR は、どの行にも一致させない（利用者の分からない要求に全件を見せない）。
    cursor.execute.reset_mock()
    repository.count_documents("history", payload_any_filters={"actor_user_uuid": ""})
    empty_sql, _binds = cursor.execute.call_args.args
    assert "1 = 0" in empty_sql


# ---------------------------------------------------------------------------
# 実行履歴の共通の絞り込み（状態・種類・期間）と、詳細・「実行者」の列の判断（#1955）

# hist-001〜005 の created_at は 2026-09-02T00:00:01〜05+00:00。hist-002 と hist-004 はブロック。
_KIND_ITEMS = [
    item.model_copy(update={"safety_is_safe": item.id not in {"hist-002", "hist-004"}})
    for item in _RUN_ITEMS
]


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service], ids=["memory", "repo"])
@pytest.mark.parametrize(
    ("status", "kind", "since", "expected"),
    [
        ("all", "all", "", ["hist-005", "hist-004", "hist-003", "hist-002", "hist-001"]),
        ("succeeded", "all", "", ["hist-005", "hist-003", "hist-001"]),
        ("blocked", "all", "", ["hist-004", "hist-002"]),
        ("all", "chat", "", ["hist-004", "hist-002", "hist-001"]),
        ("all", "query", "", ["hist-005", "hist-003"]),
        ("blocked", "chat", "", ["hist-004", "hist-002"]),
        # 期間の開始はタイムゾーン付きの ISO 8601（画面の `toISOString()` の Z も読む）。
        ("all", "all", "2026-09-02T00:00:03.000Z", ["hist-005", "hist-004", "hist-003"]),
        ("all", "all", "2026-09-02T09:00:04+09:00", ["hist-005", "hist-004"]),
        ("succeeded", "query", "2026-09-02T00:00:04Z", ["hist-005"]),
    ],
    ids=[
        "all",
        "succeeded",
        "blocked",
        "chat",
        "query",
        "blocked-chat",
        "since-z",
        "since-offset",
        "combined",
    ],
)
def test_list_history_filters_status_kind_and_since(
    factory: object, status: str, kind: str, since: str, expected: list[str]
) -> None:
    service = factory(_KIND_ITEMS)  # type: ignore[operator]

    page = service.list_history(status=status, kind=kind, since=since, limit=10)

    assert [item.id for item in page.items] == expected
    assert page.total == len(expected)
    assert page.unfiltered_total == 5


def test_list_history_status_and_safety_that_disagree_match_nothing() -> None:
    page = _memory_service(_KIND_ITEMS).list_history(status="succeeded", safety="blocked")
    assert page.items == []
    assert (page.total, page.unfiltered_total) == (0, 5)


@pytest.mark.parametrize(
    ("argument", "value"),
    [("status", "failed"), ("kind", "api"), ("since", "yesterday"), ("since", "2026-09-02")],
    ids=["status", "kind", "since-text", "since-naive"],
)
def test_list_history_rejects_invalid_common_filters(argument: str, value: str) -> None:
    with pytest.raises(ValueError, match=argument):
        _memory_service(_KIND_ITEMS).list_history(**{argument: value})  # type: ignore[arg-type]


# MCP（hist-006。admin が API キーで）と、Agent の品質評価の Run の実行（hist-007 は admin、
# hist-008 は alice、hist-009 は carol）を足した一覧（#1986）。品質評価は既定（`not_evaluation`）で
# 隠す。
_ORIGIN_ITEMS = [
    *_KIND_ITEMS,
    _history(6, "admin").model_copy(update={"run_source": "mcp", "api_key_name": "Claude Code"}),
    _history(7, "admin").model_copy(update={"run_source": "evaluation", "agent_run_id": "run-7"}),
    _history(8, "alice").model_copy(update={"run_source": "evaluation", "agent_run_id": "run-8"}),
    _history(9, "carol").model_copy(update={"run_source": "evaluation", "agent_run_id": "run-9"}),
]


@pytest.mark.parametrize("factory", [_memory_service, _incremental_service], ids=["memory", "repo"])
@pytest.mark.parametrize(
    ("kind", "status", "expected", "unfiltered"),
    [
        (
            "all",
            "all",
            ["hist-009", "hist-008", "hist-007", "hist-006", "hist-005", "hist-004", "hist-003"]
            + ["hist-002", "hist-001"],
            9,
        ),
        (
            "not_evaluation",
            "all",
            ["hist-006", "hist-005", "hist-004", "hist-003", "hist-002", "hist-001"],
            6,
        ),
        ("evaluation", "all", ["hist-009", "hist-008", "hist-007"], 9),
        ("mcp", "all", ["hist-006"], 6),
        # SQL 生成は画面の行だけ（MCP・品質評価の行は会話の持ち主が無くても入れない）。
        ("query", "all", ["hist-005", "hist-003"], 6),
        ("chat", "all", ["hist-004", "hist-002", "hist-001"], 6),
        ("not_evaluation", "blocked", ["hist-004", "hist-002"], 6),
    ],
    ids=["all", "not-evaluation", "evaluation", "mcp", "query", "chat", "not-evaluation-blocked"],
)
def test_list_history_kind_separates_mcp_and_evaluation(
    factory: object, kind: str, status: str, expected: list[str], unfiltered: int
) -> None:
    """種類で MCP・品質評価を絞る（#1986）。

    「全 M 件」は品質評価を含まない種類なら品質評価を除く（Agent の実行履歴と同じ）。
    """
    service = factory(_ORIGIN_ITEMS)  # type: ignore[operator]

    page = service.list_history(kind=kind, status=status, limit=10)

    assert [item.id for item in page.items] == expected
    assert page.total == len(expected)
    assert page.unfiltered_total == unfiltered
    # 見える行があるときは、隠している品質評価の行を数えない。
    assert page.hidden_evaluation_total == 0


def test_history_route_returns_mcp_and_evaluation_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """行の種類はきっかけ（MCP・品質評価）を先に見る（#1986）。

    既定（品質評価以外）で見える行が無いときだけ、隠している品質評価の行の数を返す。
    """
    service = _incremental_service(_ORIGIN_ITEMS)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    admin = _route_page(_principal(admin=True, user_uuid="admin"), kind="all")
    kinds = {item.id: item.kind for item in admin.items}
    assert kinds["hist-006"] == "mcp"
    assert kinds["hist-007"] == kinds["hist-008"] == "evaluation"
    assert kinds["hist-005"] == "query"
    assert kinds["hist-001"] == "chat"
    hidden = _route_page(_principal(admin=True, user_uuid="admin"), kind="not_evaluation")
    assert "hist-007" not in {item.id for item in hidden.items}
    assert (hidden.unfiltered_total, hidden.hidden_evaluation_total) == (6, 0)
    alice = _route_page(_principal(admin=False, user_uuid="alice"), kind="not_evaluation")
    assert [item.id for item in alice.items] == ["hist-002", "hist-001"]
    # carol は品質評価の行（hist-009）しか無い: 既定では 0 件で、隠している数を返す（空の状態）。
    carol = _route_page(_principal(admin=False, user_uuid="carol"), kind="not_evaluation")
    assert (carol.items, carol.unfiltered_total, carol.hidden_evaluation_total) == ([], 0, 1)
    shown = _route_page(_principal(admin=False, user_uuid="carol"), kind="evaluation")
    assert [item.id for item in shown.items] == ["hist-009"]
    detail = nl2sql_router.history_item(
        _request(_principal(admin=True, user_uuid="admin")),  # type: ignore[arg-type]
        "hist-007",
    )
    assert detail.data is not None
    assert (detail.data.kind, detail.data.agent_run_id) == ("evaluation", "run-7")


def test_oracle_history_not_evaluation_excludes_the_run_source() -> None:
    """品質評価以外は `run_source` が無いか品質評価でない行（#1986 より前の行も残す）。"""
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0,)
    cursor.fetchall.return_value = []
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = OracleIncrementalNl2SqlRepository(connection_factory=lambda: context)

    repository.list_documents_page(
        "history",
        cursor=None,
        offset=0,
        limit=10,
        payload_exclude_filters={"run_source": "evaluation"},
    )

    page_sql, page_binds = cursor.execute.call_args_list[1].args
    source = "JSON_VALUE(PAYLOAD_JSON, '$.run_source' RETURNING VARCHAR2(4000) NULL ON ERROR)"
    assert f"({source} IS NULL OR {source} <> :payload_exclude_0)" in page_sql
    assert page_binds["payload_exclude_0"] == "evaluation"


def _route_page(principal: Principal | None, **filters: str) -> HistoryData:
    response = nl2sql_router.history(
        _request(principal),  # type: ignore[arg-type]
        _paging(limit=10),
        **filters,  # type: ignore[arg-type]
    )
    assert response.data is not None
    return response.data


def test_history_route_returns_kind_and_others_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """行の種類（会話の持ち主の有無）と、他の人が実行した行が見えるか（列と絞り込みの判断）。"""
    service = _incremental_service(_KIND_ITEMS)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    alice = _route_page(_principal(admin=False, user_uuid="alice"), kind="chat")
    assert [item.id for item in alice.items] == ["hist-002", "hist-001"]
    assert {item.kind for item in alice.items} == {"chat"}
    # alice の会話で admin が実行した行がある（絞り込みに関わらず、見られる範囲で決める）。
    assert alice.others_visible is True
    assert _route_page(_principal(admin=False, user_uuid="bob")).others_visible is False
    admin = _route_page(_principal(admin=True, user_uuid="admin"), kind="query")
    assert [item.kind for item in admin.items] == ["query", "query"]
    assert admin.others_visible is True
    # 認証無効はすべての行を自分の実行とみなす（「実行者」を出さない）。
    assert _route_page(None).others_visible is False

    with pytest.raises(HTTPException) as invalid:
        _route_page(_principal(admin=True, user_uuid="admin"), since="not-a-date")
    assert invalid.value.status_code == 422


@pytest.mark.parametrize(
    ("viewer", "admin", "history_id", "visible"),
    [
        ("alice", False, "hist-001", True),
        # alice の会話で admin が実行した往復は、持ち主の alice にも見える（#1954）。
        ("alice", False, "hist-002", True),
        ("alice", False, "hist-003", False),
        ("alice", False, "hist-004", False),
        ("bob", False, "hist-002", False),
        ("admin", True, "hist-004", True),
        ("admin", True, "hist-005", True),
        ("admin", True, "missing", False),
    ],
    ids=[
        "own",
        "owner-of-admin-run",
        "admin-standalone",
        "other-user",
        "stranger-conversation",
        "admin-any",
        "admin-unrecorded",
        "missing",
    ],
)
def test_history_item_route_follows_the_list_visibility(
    monkeypatch: pytest.MonkeyPatch, viewer: str, admin: bool, history_id: str, visible: bool
) -> None:
    """詳細（`?id=<run_id>`）は一覧と同じ見え方。見えない行と無い行はどちらも 404（#1955）。"""
    service = _incremental_service(_KIND_ITEMS)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    request = _request(_principal(admin=admin, user_uuid=viewer))

    if not visible:
        with pytest.raises(HTTPException) as hidden:
            nl2sql_router.history_item(request, history_id)  # type: ignore[arg-type]
        assert hidden.value.status_code == 404
        return
    response = nl2sql_router.history_item(request, history_id)  # type: ignore[arg-type]
    assert response.data is not None
    assert response.data.id == history_id
    assert response.data.kind == ("query" if history_id in {"hist-003", "hist-005"} else "chat")
    stored = service.get_history_item(history_id)
    assert stored is not None
    assert response.data.executed_by_me is (stored.actor_user_uuid == viewer)


def test_oracle_history_kind_and_since_are_sql_conditions() -> None:
    """種類は会話の持ち主の有無（`IS [NOT] NULL`）、期間は実行日時の式と bind で比べる（#1955）。"""
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0,)
    cursor.fetchall.return_value = []
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = OracleIncrementalNl2SqlRepository(connection_factory=lambda: context)

    repository.list_documents_page(
        "history",
        cursor=None,
        offset=0,
        limit=10,
        payload_present_filters={"conversation_owner_user_uuid": True},
        created_since="2026-09-02T00:00:03+00:00",
    )

    owner = (
        "JSON_VALUE(PAYLOAD_JSON, '$.conversation_owner_user_uuid' "
        "RETURNING VARCHAR2(4000) NULL ON ERROR)"
    )
    for call in cursor.execute.call_args_list[:2]:
        sql, binds = call.args
        assert f"{owner} IS NOT NULL" in sql
        assert "JSON_VALUE(PAYLOAD_JSON, '$.created_at'" in sql
        assert ">= :created_since" in sql
        assert "2026-09-02" not in sql
        assert binds["created_since"] == "2026-09-02T00:00:03+00:00"

    cursor.execute.reset_mock()
    repository.count_documents(
        "history", payload_present_filters={"conversation_owner_user_uuid": False}
    )
    query_sql, _binds = cursor.execute.call_args.args
    assert f"{owner} IS NULL" in query_sql
