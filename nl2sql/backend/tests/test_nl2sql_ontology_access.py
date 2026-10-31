"""ontology router の profile 単位アクセス制御と markdown 分離の回帰テスト。

profile スコープの ontology ルートは `allowed_profile_ids` に基づき 403 を返すこと、
published markdown の fallback が他 profile の内容を返さないことを保証する。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from app.features.nl2sql import ontology_router
from app.features.nl2sql.profile_access import assert_profile_access
from app.security.domain import Principal
from app.security.permissions import PROFILE_MANAGE_PERMISSION


def _principal(
    allowed_profile_ids: set[str],
    *,
    system_admin: bool = False,
    profile_manager: bool = False,
) -> Principal:
    return Principal(
        user_uuid="user-1",
        login_user_id="user1",
        display_name="利用者1",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["SYSTEM_ADMIN"] if system_admin else ["GENERAL"],
        permissions={PROFILE_MANAGE_PERMISSION} if profile_manager else set(),
        data_entitlements=[],
        allowed_profile_ids=allowed_profile_ids,
        session_id="session-1",
        csrf_token_hash="hash",
        password_change_allowed=True,
    )


def _request_with(principal: Principal | None) -> Request:
    return cast(Request, SimpleNamespace(state=SimpleNamespace(principal=principal)))


class TestAssertProfileAccess:
    def test_anonymous_request_is_allowed(self) -> None:
        assert_profile_access(_request_with(None), "profile-a")

    def test_system_admin_is_allowed(self) -> None:
        assert_profile_access(_request_with(_principal(set(), system_admin=True)), "profile-a")

    def test_allowed_profile_is_allowed(self) -> None:
        assert_profile_access(_request_with(_principal({"profile-a"})), "profile-a")

    def test_profile_manager_is_allowed_for_any_profile(self) -> None:
        assert_profile_access(
            _request_with(_principal(set(), profile_manager=True)),
            "profile-b",
        )

    def test_disallowed_profile_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            assert_profile_access(_request_with(_principal({"profile-a"})), "profile-b")
        assert exc_info.value.status_code == 403

    def test_empty_profile_id_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            assert_profile_access(_request_with(_principal({"profile-a"})), "")
        assert exc_info.value.status_code == 403


class TestOntologyRouteProfileAccess:
    """profile スコープの ontology ルートが許可外 profile へ 403 を返す。"""

    def test_get_profile_ontology_view_rejects_disallowed_profile(self) -> None:
        request = _request_with(_principal({"profile-a"}))
        with pytest.raises(HTTPException) as exc_info:
            ontology_router.get_profile_ontology_view("profile-b", request)
        assert exc_info.value.status_code == 403

    def test_get_profile_ontology_markdown_rejects_disallowed_profile(self) -> None:
        request = _request_with(_principal({"profile-a"}))
        with pytest.raises(HTTPException) as exc_info:
            ontology_router.get_profile_ontology_markdown("profile-b", request)
        assert exc_info.value.status_code == 403

    def test_list_profile_ontology_build_jobs_rejects_disallowed_profile(self) -> None:
        request = _request_with(_principal({"profile-a"}))
        with pytest.raises(HTTPException) as exc_info:
            ontology_router.list_profile_ontology_build_jobs("profile-b", request)
        assert exc_info.value.status_code == 403

    def test_search_profile_ontology_context_rejects_disallowed_profile(self) -> None:
        request = _request_with(_principal({"profile-a"}))
        with pytest.raises(HTTPException) as exc_info:
            ontology_router.search_profile_ontology_context(
                "profile-b",
                ontology_router.OntologyContextSearchRequest(
                    question="売上は?", ontology_revision_id="revision-x"
                ),
                request,
            )
        assert exc_info.value.status_code == 403

    def test_list_profile_ontology_proposals_rejects_disallowed_profile(self) -> None:
        request = _request_with(_principal({"profile-a"}))
        with pytest.raises(HTTPException) as exc_info:
            ontology_router.list_profile_ontology_proposals("profile-b", request)
        assert exc_info.value.status_code == 403


class TestPublishedMarkdownProfileIsolation:
    """published markdown の fallback が他 profile の artifact を返さない。"""

    def test_fallback_does_not_leak_other_profile_markdown(self) -> None:
        runtime = ontology_router.ontology_runtime
        ontology = runtime.current_ontology()
        revision_id = ontology.revision.id
        # profile B の published markdown を artifact として保存する
        runtime._save_markdown_artifact(  # noqa: SLF001 - fallback 経路の直接検証
            profile_id="profile-b",
            revision=ontology.revision,
            artifact_type="ontology_markdown_published",
            markdown="# profile B の業務オントロジー",
        )
        # profile A として取得すると B の内容は返らない
        markdown = runtime.published_markdown_for_revision(revision_id, profile_id="profile-a")
        assert "profile B" not in markdown


class TestMarkdownArtifactEtagGuard:
    """etag を提示した保存で artifact 行が無い場合は conflict にする(黙殺しない)。"""

    def test_save_with_expected_etag_and_missing_artifact_raises_conflict(self) -> None:
        runtime = ontology_router.ontology_runtime
        ontology = runtime.current_ontology()
        with pytest.raises(Exception) as exc_info:
            runtime._save_markdown_artifact(  # noqa: SLF001 - ガード条件の直接検証
                profile_id="profile-etag-missing",
                revision=ontology.revision,
                artifact_type="ontology_markdown_draft",
                markdown="# draft",
                expected_etag="stale-etag",
            )
        assert "ONTOLOGY_MARKDOWN_ARTIFACT_MISSING" in str(
            getattr(exc_info.value, "code", "") or exc_info.value
        )
