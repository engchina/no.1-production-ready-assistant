"""`/nl2sql/reverse` `/nl2sql/reverse/deep` の業務プロファイルアクセス検証。

reverse は profile の glossary / カタログ(論理名・コメント)を使い、deep はそれを
schema context として Enterprise AI へ送るため、他ルートと同じく許可 profile だけを
受け付ける(Issue: reverse がプロファイルアクセスを検証しない)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    Nl2SqlProfile,
    ReverseSqlRequest,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.domain import SYSTEM_ADMIN_ROLE_CODE, Principal

_SQL = "SELECT ID FROM APP.INVOICES"


def _table(name: str, logical: str) -> SchemaTable:
    return SchemaTable(
        owner="APP",
        table_name=name,
        logical_name=logical,
        columns=[
            SchemaColumn(column_name="ID", logical_name="ID", data_type="NUMBER", nullable=False),
            SchemaColumn(column_name="NAME", logical_name="名称", data_type="VARCHAR2"),
            SchemaColumn(column_name="AMOUNT", logical_name="金額", data_type="NUMBER"),
            SchemaColumn(column_name="CREATED_AT", logical_name="作成日", data_type="DATE"),
        ],
    )


def _service() -> Nl2SqlService:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(
        refreshed_at="2026-09-02T00:00:00+00:00",
        schema_fingerprint="reverse-access-v1",
        current_owner="APP",
        tables=[_table("ORDERS", "注文"), _table("INVOICES", "請求")],
    )
    manifest = {
        (table.owner.upper(), table.table_name.upper()): catalog.refreshed_at
        for table in catalog.tables
    }
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )
    repository.save_profile(
        Nl2SqlProfile(id="sales", name="販売", allowed_tables=["APP.ORDERS"]),
        expected_etag=None,
    )
    repository.save_profile(
        Nl2SqlProfile(
            id="finance",
            name="経理",
            allowed_tables=["APP.INVOICES"],
            glossary={"請求": "INVOICES"},
        ),
        expected_etag=None,
    )
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    service._catalog = repository.load_catalog()  # noqa: SLF001
    return service


def _principal(allowed_profile_ids: set[str], *, admin: bool = False) -> Principal:
    return Principal(
        user_uuid="user-1",
        login_user_id="user1",
        display_name="利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE] if admin else ["ANALYST"],
        permissions={"menu.sql_to_question"},
        data_entitlements=[],
        allowed_profile_ids=set(allowed_profile_ids),
        session_id="session-1",
        csrf_token_hash="csrf",
    )


def _request(principal: Principal | None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    instance = _service()
    monkeypatch.setattr(instance._enterprise_ai_client, "is_configured", lambda: False)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", instance)
    return instance


@pytest.mark.parametrize("route", [nl2sql_router.reverse, nl2sql_router.reverse_deep])
def test_reverse_rejects_profiles_outside_principal_scope(
    service: Nl2SqlService, route: object
) -> None:
    del service
    with pytest.raises(HTTPException) as denied:
        route(  # type: ignore[operator]
            ReverseSqlRequest(sql=_SQL, profile_id="finance"),
            _request(_principal({"sales"})),
        )
    assert denied.value.status_code == 403


@pytest.mark.parametrize("route", [nl2sql_router.reverse, nl2sql_router.reverse_deep])
def test_reverse_allows_permitted_profile_admin_and_unauthenticated(
    service: Nl2SqlService, route: object
) -> None:
    del service
    permitted = route(  # type: ignore[operator]
        ReverseSqlRequest(sql=_SQL, profile_id="finance"),
        _request(_principal({"finance"})),
    )
    assert permitted.data is not None
    assert permitted.data.question

    admin = route(  # type: ignore[operator]
        ReverseSqlRequest(sql=_SQL, profile_id="finance"),
        _request(_principal(set(), admin=True)),
    )
    assert admin.data is not None

    unauthenticated = route(  # type: ignore[operator]
        ReverseSqlRequest(sql=_SQL, profile_id="finance"),
        _request(None),
    )
    assert unauthenticated.data is not None


@pytest.mark.parametrize("route", [nl2sql_router.reverse, nl2sql_router.reverse_deep])
def test_reverse_unknown_profile_is_a_client_error(service: Nl2SqlService, route: object) -> None:
    del service
    with pytest.raises(HTTPException) as missing:
        route(  # type: ignore[operator]
            ReverseSqlRequest(sql=_SQL, profile_id="missing-profile"),
            _request(_principal(set(), admin=True)),
        )
    # 旧実装は ValueError が汎用ハンドラまで抜けて 500 になっていた。
    assert missing.value.status_code == 400
