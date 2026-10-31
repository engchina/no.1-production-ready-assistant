from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request, Response
from pr_backend_core.api import CursorParams
from pydantic import ValidationError

from app.features.nl2sql import ontology_router, profile_sync
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.incremental_store import (
    IncrementalVersionConflict,
    MemoryIncrementalNl2SqlRepository,
)
from app.features.nl2sql.models import (
    Nl2SqlEngine,
    Nl2SqlProfile,
    ProfilePatchRequest,
    ProfileSelectAiConfig,
    ProfileSelectAiProfileRequest,
    ProfileSyncJobRequest,
    ProfileUpsertRequest,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError
from app.features.nl2sql.service import (
    Nl2SqlService,
    ProfileNameConflict,
    ProfileOracleCleanupFailed,
)
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.domain import Principal

# router を直接呼ぶときの既定の cursor / limit（HTTP では cursor_params の依存が作る）。
_PAGING = CursorParams(cursor=None, limit=50)


def _anon_request() -> Request:
    """認証無効(principal なし)相当の request。router の RBAC 引数へ渡す。"""
    return cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))


def _profile_user_request(allowed_profile_ids: set[str]) -> Request:
    """業務 profile 制限ユーザー相当の request。"""
    principal = Principal(
        user_uuid="user-1",
        login_user_id="user1",
        display_name="利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["ANALYST"],
        permissions=set(),
        allowed_profile_ids=set(allowed_profile_ids),
        session_id="session-1",
        csrf_token_hash="csrf",
    )
    return cast(Request, SimpleNamespace(state=SimpleNamespace(principal=principal)))


def _assert_raises_404(route_call: Callable[[], object]) -> None:
    with pytest.raises(HTTPException) as exc:
        route_call()
    assert exc.value.status_code == 404


class _FakeOracleCleanupAdapter:
    def __init__(self, *, fail_agent: bool = False) -> None:
        self.fail_agent = fail_agent
        self.calls: list[tuple[str, dict[str, str]]] = []

    def drop_select_ai_agent_assets(self, **kwargs: str) -> dict[str, str]:
        self.calls.append(("agent", kwargs))
        if self.fail_agent:
            raise OracleAdapterError("agent cleanup failed")
        return {"runtime": "oracle", "package": "DBMS_CLOUD_AI_AGENT"}

    def drop_select_ai_profile(self, **kwargs: str) -> dict[str, str]:
        self.calls.append(("profile", kwargs))
        return {"runtime": "oracle", "package": "DBMS_CLOUD_AI"}


class _ProfileNameConflictService:
    def archive_profile(self, _profile_id: str) -> None:
        raise ProfileNameConflict("SALES_PROFILE")

    def restore_profile(self, _profile_id: str) -> None:
        raise ProfileNameConflict("SALES_PROFILE")


def test_profile_upsert_request_uses_name_for_select_ai_profile_name() -> None:
    request = ProfileUpsertRequest(
        name="sales_profile",
        select_ai_config={"profile_name": "LEGACY_PROFILE"},
    )

    assert request.name == "SALES_PROFILE"
    assert request.select_ai_config.profile_name == "SALES_PROFILE"


def test_profile_upsert_request_rejects_invalid_name() -> None:
    with pytest.raises(ValidationError):
        ProfileUpsertRequest(name="1PROFILE")

    with pytest.raises(ValidationError):
        ProfileUpsertRequest(name="新プロファイル")


def test_profile_name_uniqueness_rejects_case_variants_and_archived_profiles() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    created = service.create_profile(Nl2SqlProfile(id="profile-a", name="sales_profile"))

    with pytest.raises(ProfileNameConflict):
        service.create_profile(Nl2SqlProfile(id="profile-b", name="SALES_PROFILE"))

    service.archive_profile(created.id)
    with pytest.raises(ProfileNameConflict):
        service.create_profile(Nl2SqlProfile(id="profile-c", name="sales_profile"))

    other = service.create_profile(Nl2SqlProfile(id="profile-d", name="orders_profile"))
    with pytest.raises(ProfileNameConflict):
        service.update_profile(
            other.id,
            lambda profile: profile.model_copy(update={"name": "Sales_Profile"}),
        )


def test_profile_name_conflict_returns_422_field_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    created = nl2sql_router.create_profile(
        ProfileUpsertRequest(name="sales_profile"),
        Response(),
    ).data
    assert created is not None
    archived = nl2sql_router.archive_profile(created.id, _anon_request()).data
    assert archived is not None
    assert archived.archived is True

    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.create_profile(ProfileUpsertRequest(name="SALES_PROFILE"), Response())

    assert exc_info.value.status_code == 422
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail == {
        "code": "NL2SQL_PROFILE_NAME_CONFLICT",
        "message_ja": "業務プロファイル名「SALES_PROFILE」は既に使用されています。",
        "field_errors": [
            {
                "pointer": "/name",
                "code": "profile_name_conflict",
                "message": "業務プロファイル名「SALES_PROFILE」は既に使用されています。",
            }
        ],
    }


def test_profile_archive_restore_name_conflict_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", _ProfileNameConflictService())

    calls: tuple[Callable[[], object], ...] = (
        lambda: nl2sql_router.archive_profile("profile-a", _anon_request()),
        lambda: nl2sql_router.restore_profile("profile-a", _anon_request()),
    )
    for call in calls:
        with pytest.raises(HTTPException) as exc_info:
            call()
        assert exc_info.value.status_code == 422
        assert cast(dict[str, Any], exc_info.value.detail)["code"] == (
            "NL2SQL_PROFILE_NAME_CONFLICT"
        )


def test_profile_search_invalid_cursor_returns_422_without_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.save_profile(Nl2SqlProfile(id="profile-a", name="PROFILE_A"), expected_etag=None)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    persistence_failures: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "app.features.nl2sql.service.record_persistence_failure",
        lambda *args, **kwargs: persistence_failures.append((args, kwargs)),
    )

    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.search_profiles(
            _anon_request(), Response(), CursorParams(cursor="not-a-cursor", limit=50)
        )

    assert exc_info.value.status_code == 422
    assert "cursor が不正" in str(exc_info.value.detail)
    assert persistence_failures == []


def test_profile_search_router_rejects_unknown_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.save_profile(Nl2SqlProfile(id="profile-a", name="PROFILE_A"), expected_etag=None)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.search_profiles(_anon_request(), Response(), _PAGING, sort="updated_at")

    assert exc_info.value.status_code == 422

    response = Response()
    page = nl2sql_router.search_profiles(
        _anon_request(),
        response,
        _PAGING,
        sort="tables",
        direction="desc",
    )
    assert not isinstance(page, Response)
    # sort ごとに並びが変わるため ETag も分岐する。
    assert "tables-desc" in response.headers["etag"]


def test_profile_requests_reject_blank_glossary_keys_and_control_characters() -> None:
    with pytest.raises(ValidationError, match="用語キー"):
        ProfileUpsertRequest(name="sales_profile", glossary={"  ": "INVOICES.TOTAL_AMOUNT"})

    with pytest.raises(ValidationError, match="制御文字"):
        ProfilePatchRequest(glossary={"売上": "INVOICES.\x01TOTAL_AMOUNT"})

    request = ProfilePatchRequest(glossary={" 売上 ": " INVOICES.TOTAL_AMOUNT "})

    assert request.glossary == {"売上": "INVOICES.TOTAL_AMOUNT"}


def test_learning_material_upload_rejects_large_file_before_read() -> None:
    class TooLargeUpload:
        filename = "terms.xlsx"
        size = nl2sql_router.LEARNING_MATERIAL_UPLOAD_MAX_BYTES + 1

        async def read(self, _size: int = -1) -> bytes:
            raise AssertionError("oversized learning material must be rejected before read")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(nl2sql_router.import_legacy_terms(cast(Any, TooLargeUpload())))

    assert exc_info.value.status_code == 413


def test_profile_patch_ignores_separate_select_ai_profile_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    created = nl2sql_router.create_profile(
        ProfileUpsertRequest(name="sales_profile"),
        Response(),
    ).data
    assert created is not None

    updated = nl2sql_router.update_profile(
        created.id,
        ProfilePatchRequest(
            select_ai_config={"profile_name": "OTHER_PROFILE", "region": "ap-osaka-1"}
        ),
        _anon_request(),
        Response(),
    ).data

    assert updated is not None
    assert updated.name == "SALES_PROFILE"
    assert updated.select_ai_config.profile_name == "SALES_PROFILE"
    assert updated.select_ai_config.region == "ap-osaka-1"


def test_profile_create_update_and_restore_never_materialize_ontology_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(
        ontology_router.ontology_runtime,
        "materialize_profile_view",
        lambda _profile_id: pytest.fail("profile mutation must not materialize view"),
    )

    created = nl2sql_router.create_profile(
        ProfileUpsertRequest(name="invoice_profile", allowed_tables=["APP.INVOICES"]),
        Response(),
    ).data
    assert created is not None
    assert created.name == "INVOICE_PROFILE"
    assert created.select_ai_config.profile_name == "INVOICE_PROFILE"
    updated = nl2sql_router.update_profile(
        created.id,
        ProfileUpsertRequest(name="invoice_profile_v2", allowed_tables=["APP.INVOICES"]),  # type: ignore[arg-type]
        _anon_request(),
        Response(),
    ).data
    assert updated is not None
    assert updated.name == "INVOICE_PROFILE_V2"
    assert updated.select_ai_config.profile_name == "INVOICE_PROFILE_V2"
    assert updated.select_ai_config.previous_profile_name == "INVOICE_PROFILE"
    archived = nl2sql_router.archive_profile(created.id, _anon_request()).data
    assert archived is not None
    assert archived.archived is True
    restored = nl2sql_router.restore_profile(created.id, _anon_request()).data
    assert restored is not None
    assert restored.archived is False


def test_default_profile_delete_uses_shared_physical_delete_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    cleanup_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ontology_router.ontology_build_service,
        "cancel_profile_jobs",
        lambda profile_id: cleanup_calls.append(("build", profile_id)),
    )
    monkeypatch.setattr(
        profile_sync.profile_sync_service,
        "cancel_for_profile",
        lambda profile_id: cleanup_calls.append(("sync", profile_id)),
    )
    monkeypatch.setattr(
        ontology_router.ontology_runtime,
        "delete_profile_state",
        lambda profile_id: cleanup_calls.append(("runtime", profile_id)),
    )

    deleted = nl2sql_router.delete_profile("default", _anon_request()).data

    assert deleted is not None
    assert deleted.profile.id == "default"
    assert [item.engine for item in deleted.oracle_cleanup] == [
        Nl2SqlEngine.SELECT_AI_AGENT,
        Nl2SqlEngine.SELECT_AI,
    ]
    assert {item.status for item in deleted.oracle_cleanup} == {"skipped"}
    assert service.list_profiles(include_archived=True) == []
    assert cleanup_calls == [
        ("build", "default"),
        ("sync", "default"),
        ("runtime", "default"),
    ]


def test_default_profile_delete_honors_incremental_preconditions_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository()
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(
        ontology_router.ontology_build_service,
        "cancel_profile_jobs",
        lambda _profile_id: 0,
    )
    monkeypatch.setattr(
        profile_sync.profile_sync_service,
        "cancel_for_profile",
        lambda _profile_id: 0,
    )
    monkeypatch.setattr(
        ontology_router.ontology_runtime,
        "delete_profile_state",
        lambda _profile_id: 0,
    )
    service.get_profile("default")

    with pytest.raises(HTTPException) as missing_precondition:
        nl2sql_router.delete_profile("default", _anon_request())
    assert missing_precondition.value.status_code == 428

    with pytest.raises(HTTPException) as conflict:
        nl2sql_router.delete_profile("default", _anon_request(), if_match='"stale"')
    assert conflict.value.status_code == 409
    assert conflict.value.headers is not None
    current_etag = conflict.value.headers["ETag"]
    assert current_etag

    deleted = nl2sql_router.delete_profile(
        "default",
        _anon_request(),
        if_match=current_etag,
    ).data
    assert deleted is not None
    assert deleted.profile.id == "default"

    with pytest.raises(HTTPException) as missing:
        nl2sql_router.delete_profile("default", _anon_request(), if_match=current_etag)
    assert missing.value.status_code == 404


def test_profile_not_found_routes_return_404_with_incremental_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = MemoryIncrementalNl2SqlRepository()  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(
        profile_sync,
        "profile_sync_service",
        profile_sync.ProfileSyncService(service=service),
    )

    request = _anon_request()
    missing_id = "missing-profile"
    cases: list[Callable[[], object]] = [
        lambda: nl2sql_router.archive_profile(missing_id, request),
        lambda: nl2sql_router.restore_profile(missing_id, request),
        lambda: nl2sql_router.export_profile_learning_material(missing_id, request),
        lambda: nl2sql_router.upsert_profile_select_ai_profile(
            missing_id,
            ProfileSelectAiProfileRequest(confirmation="ADMIN_EXECUTE"),
            request,
        ),
        lambda: nl2sql_router.create_profile_oracle_sync_job(
            missing_id,
            ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
            "sync-missing-profile",
            request,
        ),
        lambda: nl2sql_router.refresh_select_ai_profile(request, profile_id=missing_id),
        lambda: nl2sql_router.refresh_select_ai_agent_assets(request, profile_id=missing_id),
    ]

    for route_call in cases:
        _assert_raises_404(route_call)


def test_archived_profile_active_only_routes_return_404_with_incremental_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = MemoryIncrementalNl2SqlRepository()  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(
        profile_sync,
        "profile_sync_service",
        profile_sync.ProfileSyncService(service=service),
    )
    request = _anon_request()

    archived = nl2sql_router.archive_profile("default", request).data
    assert archived is not None
    assert archived.archived is True

    cases: list[Callable[[], object]] = [
        lambda: nl2sql_router.export_profile_learning_material("default", request),
        lambda: nl2sql_router.upsert_profile_select_ai_profile(
            "default",
            ProfileSelectAiProfileRequest(confirmation="ADMIN_EXECUTE"),
            request,
        ),
        lambda: nl2sql_router.create_profile_oracle_sync_job(
            "default",
            ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE"),
            "sync-archived-profile",
            request,
        ),
        lambda: nl2sql_router.refresh_select_ai_profile(request, profile_id="default"),
        lambda: nl2sql_router.refresh_select_ai_agent_assets(request, profile_id="default"),
    ]

    for route_call in cases:
        _assert_raises_404(route_call)


def test_restricted_profile_search_uses_current_summary_etag_after_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = MemoryIncrementalNl2SqlRepository()  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    request = _profile_user_request({"default"})

    first_response = Response()
    first_result = nl2sql_router.search_profiles(request, first_response, _PAGING)
    assert not isinstance(first_result, Response)
    first_page = first_result.data
    assert first_page is not None
    first_etag = first_response.headers["ETag"]
    first_summary = first_page.items[0]

    updated = service.update_profile(
        "default",
        lambda profile: profile.model_copy(update={"name": "RENAMED_PROFILE"}),
        expected_etag=first_summary.etag,
    )

    second_response = Response()
    second_result = nl2sql_router.search_profiles(
        request,
        second_response,
        _PAGING,
        if_none_match=first_etag,
    )

    assert not isinstance(second_result, Response)
    second_page = second_result.data
    assert second_page is not None
    assert second_response.headers["ETag"] != first_etag
    assert second_page.items[0].name == "RENAMED_PROFILE"
    assert second_page.items[0].etag == updated.etag
    assert second_page.items[0].version == updated.version
    assert second_page.items[0].etag != first_summary.etag


def test_profile_delete_executes_oracle_agent_then_profile_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = _FakeOracleCleanupAdapter()
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_oracle_adapter", adapter)

    deleted = service.delete_profile_with_oracle_cleanup("default")

    assert deleted.profile.id == "default"
    assert [item.engine for item in deleted.oracle_cleanup] == [
        Nl2SqlEngine.SELECT_AI_AGENT,
        Nl2SqlEngine.SELECT_AI,
    ]
    assert [call[0] for call in adapter.calls] == ["agent", "profile"]
    assert adapter.calls[0][1]["profile_name"] == "NL2SQL_DEFAULT_PROFILE"
    assert adapter.calls[0][1]["team_name"] == "NL2SQL_DEFAULT_TEAM"
    assert adapter.calls[1][1]["profile_name"] == "NL2SQL_DEFAULT_PROFILE"
    assert service.list_profiles(include_archived=True) == []


def test_cleanup_archived_profile_uses_stored_select_ai_profile_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = _FakeOracleCleanupAdapter()
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    profile = service.create_profile(
        Nl2SqlProfile(
            id="sales",
            name="SALES_PROFILE",
            select_ai_config=ProfileSelectAiConfig(profile_name="SALES_PROFILE"),
        )
    )
    service.archive_profile(profile.id)

    cleanup = service.cleanup_select_ai_assets(
        profile_id=profile.id,
        engines=[Nl2SqlEngine.SELECT_AI],
        confirmation="ADMIN_EXECUTE",
    )

    assert cleanup[0].profile_name == "SALES_PROFILE"
    assert adapter.calls == [("profile", {"profile_name": "SALES_PROFILE"})]


def test_profile_delete_stale_etag_does_not_cleanup_oracle_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository()
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001
    adapter = _FakeOracleCleanupAdapter()
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_oracle_adapter", adapter)

    with pytest.raises(IncrementalVersionConflict):
        service.delete_profile_with_oracle_cleanup("default", expected_etag="stale")

    assert adapter.calls == []
    assert service.get_profile("default").id == "default"


def test_profile_delete_oracle_cleanup_failure_keeps_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = _FakeOracleCleanupAdapter(fail_agent=True)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_oracle_adapter", adapter)

    with pytest.raises(ProfileOracleCleanupFailed):
        service.delete_profile_with_oracle_cleanup("default")

    assert [call[0] for call in adapter.calls] == ["agent", "profile"]
    assert service.get_profile("default").id == "default"


def test_profile_delete_oracle_cleanup_failure_returns_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = _FakeOracleCleanupAdapter(fail_agent=True)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    with pytest.raises(HTTPException) as exc:
        nl2sql_router.delete_profile("default", _anon_request())

    assert exc.value.status_code == 502
    assert "業務プロファイルは削除していません" in str(exc.value.detail)
    assert service.get_profile("default").id == "default"
