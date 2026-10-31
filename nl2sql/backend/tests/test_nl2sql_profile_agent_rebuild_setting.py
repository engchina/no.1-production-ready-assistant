"""業務プロファイルの「保存時に Select AI Agent アセットも再構築する」の保存（#1690）。

チェックは保存の 1 回だけの実行オプションではなく、業務プロファイルの属性
（`rebuild_agent_assets_on_save`）として保存し、取得・開き直しで保つ。Oracle 反映のジョブは
要求が省略したときに、この値に従う。LLM と Oracle は呼ばない。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request, Response
from test_nl2sql_job_runtime import _repository, _worker

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import (
    Nl2SqlProfile,
    ProfilePatchRequest,
    ProfileSyncJobRequest,
    ProfileUpsertRequest,
)
from app.features.nl2sql.ontology_store import InMemoryOntologyStore
from app.features.nl2sql.profile_sync import ProfileSyncService
from app.features.nl2sql.service import Nl2SqlService


def _anon_request() -> Request:
    return cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    # 業務プロファイルを PAYLOAD_JSON に保存する incremental の repository（memory）を使う。
    service = _worker(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    return service


def test_rebuild_agent_assets_on_save_defaults_to_off(service: Nl2SqlService) -> None:
    created = nl2sql_router.create_profile(
        ProfileUpsertRequest(name="sales_profile"), Response()
    ).data
    assert created is not None
    assert created.rebuild_agent_assets_on_save is False


def test_rebuild_agent_assets_on_save_is_saved_updated_and_reloaded(
    service: Nl2SqlService,
) -> None:
    created = nl2sql_router.create_profile(
        ProfileUpsertRequest(name="sales_profile", rebuild_agent_assets_on_save=True),
        Response(),
    ).data
    assert created is not None
    assert created.rebuild_agent_assets_on_save is True
    # 開き直す（取得し直す）と保存した値が返る。
    reloaded = nl2sql_router.get_profile_detail(created.id, _anon_request(), Response())
    assert not isinstance(reloaded, Response)
    assert reloaded.data is not None
    assert reloaded.data.rebuild_agent_assets_on_save is True

    # ほかの項目だけの PATCH（省略）は値を変えない。
    renamed = nl2sql_router.update_profile(
        created.id,
        ProfilePatchRequest(category="finance"),
        _anon_request(),
        Response(),
        if_match=f'"{created.etag}"',
    ).data
    assert renamed is not None
    assert renamed.rebuild_agent_assets_on_save is True

    turned_off = nl2sql_router.update_profile(
        created.id,
        ProfilePatchRequest(rebuild_agent_assets_on_save=False),
        _anon_request(),
        Response(),
        if_match=f'"{renamed.etag}"',
    ).data
    assert turned_off is not None
    assert turned_off.rebuild_agent_assets_on_save is False
    assert service.get_profile(created.id).rebuild_agent_assets_on_save is False


class _ProfileOnlyService:
    def __init__(self, profile: Nl2SqlProfile) -> None:
        self.profile = profile

    def get_profile(self, profile_id: str) -> Nl2SqlProfile:
        assert profile_id == self.profile.id
        return self.profile.model_copy(deep=True)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        nl2sql_ontology_worker_mode="external",
        nl2sql_profile_sync_job_timeout_seconds=300.0,
    )


@pytest.mark.parametrize(
    ("saved", "requested", "expected"),
    [(True, None, True), (False, None, False), (True, False, False), (False, True, True)],
    ids=[
        "saved-on-omitted",
        "saved-off-omitted",
        "saved-on-requested-off",
        "saved-off-requested-on",
    ],
)
def test_profile_sync_job_follows_saved_setting_when_request_omits_it(
    monkeypatch: pytest.MonkeyPatch,
    saved: bool,
    requested: bool | None,
    expected: bool,
) -> None:
    monkeypatch.setattr("app.features.nl2sql.profile_sync.get_settings", _settings)
    store = InMemoryOntologyStore()
    profile = Nl2SqlProfile(
        id="profile-1", name="SALES", etag="etag-1", rebuild_agent_assets_on_save=saved
    )
    sync = ProfileSyncService(
        service=_ProfileOnlyService(profile),  # type: ignore[arg-type]
        store_provider=lambda: store,
    )
    request = (
        ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE")
        if requested is None
        else ProfileSyncJobRequest(confirmation="ADMIN_EXECUTE", rebuild_agent_assets=requested)
    )

    job = sync.start("profile-1", request, idempotency_key=f"agent-{saved}-{requested}")

    assert job.rebuild_agent_assets is expected
