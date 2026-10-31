"""持ち主の無い SQL 履歴・query session を、管理の権限の無い利用者に操作させない（#1126）。

ジョブ（`_assert_job_actor_access`）と同じく、認証済みで管理の権限の無い利用者には、
持ち主が自分と一致しない行（持ち主が空の行を含む）を拒否する。評価・確認への回答のように内容を
足す操作は、system_admin でも持ち主だけ（持ち主の無い行には誰も足せない。#3244）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request

from app.features.nl2sql import ontology_router
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import FeedbackRating, HistoryItem, Nl2SqlEngine
from app.features.nl2sql.service import Nl2SqlService, OthersContentForbiddenError
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.domain import SYSTEM_ADMIN_ROLE_CODE, Principal


def _principal(user_uuid: str, *, admin: bool = False) -> Principal:
    return Principal(
        user_uuid=user_uuid,
        login_user_id=user_uuid,
        display_name=user_uuid,
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE] if admin else ["ANALYST"],
        permissions={"nl2sql.query.generate", "nl2sql.feedback.write"},
        allowed_profile_ids={"default"},
        session_id=f"session-{user_uuid}",
        csrf_token_hash="csrf",
    )


def _history(actor_user_uuid: str) -> HistoryItem:
    return HistoryItem(
        id="history-1",
        question="社員一覧",
        engine=Nl2SqlEngine.SELECT_AI,
        generated_sql="SELECT EMPLOYEE_ID FROM EMPLOYEE",
        created_at="2026-10-01T00:00:00+00:00",
        profile_id="default",
        actor_user_uuid=actor_user_uuid,
    )


def _service(actor_user_uuid: str) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._history = [_history(actor_user_uuid)]  # noqa: SLF001 - 持ち主の境界の fixture
    return service


@pytest.mark.parametrize("owner", ["", "user-a"], ids=["ownerless", "other-owner"])
def test_feedback_rejects_history_not_owned_by_actor(owner: str) -> None:
    service = _service(owner)

    with pytest.raises(PermissionError):
        service.save_feedback("history-1", FeedbackRating.BAD, "上書き", actor_user_uuid="user-b")
    with pytest.raises(PermissionError):
        service.clear_feedback("history-1", actor_user_uuid="user-b")

    history = service._history_by_id("history-1")  # noqa: SLF001
    assert history is not None
    assert history.feedback_rating is None


def test_feedback_keeps_owner_manager_and_auth_disabled_access() -> None:
    owned = _service("user-b")
    saved = owned.save_feedback(
        "history-1", FeedbackRating.GOOD, "自分の履歴", actor_user_uuid="user-b"
    )
    assert saved.saved is True
    owned.clear_feedback("history-1", actor_user_uuid="user-b")

    ownerless = _service("")
    # 評価は実行した本人だけ（#3244）。見える system_admin も持ち主の無い行・他人の行は 403。
    for owner in ("", "user-a"):
        with pytest.raises(OthersContentForbiddenError):
            _service(owner).save_feedback(
                "history-1",
                FeedbackRating.GOOD,
                "管理者",
                actor_user_uuid="manager",
                actor_can_manage=True,
                allowed_profile_ids={"default"},
            )
    # 認証を無効にしたとき（actor が空）は制約しない。
    unauthenticated = ownerless.save_feedback(
        "history-1", FeedbackRating.GOOD, "", actor_user_uuid="", actor_can_manage=True
    )
    assert unauthenticated.saved is True
    ownerless.clear_feedback("history-1", actor_user_uuid="")


def _session_data(owner: str) -> Any:
    return SimpleNamespace(session=SimpleNamespace(actor_user_uuid=owner, profile_id="default"))


def _request(principal: Principal | None) -> Request:
    return cast(Request, SimpleNamespace(state=SimpleNamespace(principal=principal)))


@pytest.mark.parametrize("owner", ["", "user-a"], ids=["ownerless", "other-owner"])
def test_query_session_rejects_session_not_owned_by_actor(owner: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        ontology_router._ensure_query_session_access(  # noqa: SLF001
            _session_data(owner), _request(_principal("user-b"))
        )

    assert exc_info.value.status_code == 404  # 存在を漏らさない（#1889）


def test_query_session_keeps_owner_admin_and_auth_disabled_access() -> None:
    owned = _session_data("user-b")
    assert (
        ontology_router._ensure_query_session_access(  # noqa: SLF001
            owned, _request(_principal("user-b"))
        )
        is owned
    )
    ownerless = _session_data("")
    assert (
        ontology_router._ensure_query_session_access(  # noqa: SLF001
            ownerless, _request(_principal("admin", admin=True))
        )
        is ownerless
    )
    assert ontology_router._ensure_query_session_access(ownerless, _request(None)) is ownerless  # noqa: SLF001


def test_query_session_answers_are_owner_only_even_for_system_admin() -> None:
    """確認への回答・反映など session に内容を足す操作は持ち主だけ（#3244）。

    system_admin は他の利用者の session・持ち主の無い session を見られるが、足すのは 403。
    """
    admin = _request(_principal("admin", admin=True))
    for owner in ("user-a", ""):
        data = _session_data(owner)
        assert ontology_router._ensure_query_session_access(data, admin) is data  # noqa: SLF001
        with pytest.raises(HTTPException) as denied:
            ontology_router._ensure_query_session_access(data, admin, adding=True)  # noqa: SLF001
        assert denied.value.status_code == 403
    owned = _session_data("user-b")
    assert (
        ontology_router._ensure_query_session_access(  # noqa: SLF001
            owned, _request(_principal("user-b")), adding=True
        )
        is owned
    )
    mine = _session_data("admin")
    assert ontology_router._ensure_query_session_access(mine, admin, adding=True) is mine  # noqa: SLF001
    # 見えない他の利用者の session は、足す操作でも 404（存在を漏らさない）。
    with pytest.raises(HTTPException) as hidden:
        ontology_router._ensure_query_session_access(  # noqa: SLF001
            _session_data("user-a"), _request(_principal("user-b")), adding=True
        )
    assert hidden.value.status_code == 404
    # 認証を無効にしたときは制約しない。
    ownerless = _session_data("")
    assert (
        ontology_router._ensure_query_session_access(ownerless, _request(None), adding=True)  # noqa: SLF001
        is ownerless
    )


class _QualityEvaluationService:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.cancelled: list[str] = []

    def peek_job_record(self, job_id: str) -> SimpleNamespace:
        return SimpleNamespace(job_id=job_id, profile_id="default", actor_user_uuid=self.owner)

    def cancel_job(self, job_id: str) -> str:
        self.cancelled.append(job_id)
        return job_id


@pytest.mark.parametrize("owner", ["", "user-a"], ids=["ownerless", "other-owner"])
def test_quality_evaluation_cancel_rejects_job_not_owned_by_actor(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    service = _QualityEvaluationService(owner)
    monkeypatch.setattr(nl2sql_router, "quality_evaluation_service", service)

    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router._quality_evaluation_job_for_access(  # noqa: SLF001
            "job-1", _request(_principal("user-b")), require_actor_owner=True
        )

    assert exc_info.value.status_code == 403
    assert service.cancelled == []
