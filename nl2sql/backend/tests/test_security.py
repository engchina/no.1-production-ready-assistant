"""Application auth/RBAC の回帰テスト。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from pr_backend_core.api import OffsetParams

from app.cli.app_security_migrate import main as security_migrate_main
from app.cli.app_security_migrate import split_ddl
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import HistoryItem, Nl2SqlEngine, Nl2SqlProfile
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.main import _assert_route_manifest, app
from app.security import dependencies as security_dependencies
from app.security.dependencies import authorize_api_request, local_debug_principal
from app.security.domain import (
    SYSTEM_ADMIN_ROLE_CODE,
    SYSTEM_ADMIN_ROLE_ID,
    DataEntitlementRecord,
    DataEntitlementScopeFilter,
    Principal,
    RoleRecord,
    UserRecord,
    scope_filters_scope_code,
)
from app.security.passwords import PasswordPolicyError, hash_password, validate_password
from app.security.permissions import (
    ALL_PERMISSION_CODES,
    FEEDBACK_MANAGE_PERMISSION,
    FEEDBACK_WRITE_PERMISSION,
    LEARNING_MATERIAL_MANAGE_PERMISSION,
    OPEN_API_OPERATIONS,
    PERMISSION_CATALOG,
    PERSISTENCE_RECOVER_PERMISSION,
    PROFILE_MANAGE_PERMISSION,
    PROFILE_READ_PERMISSION,
    QUERY_GENERATE_PERMISSION,
    ROUTE_PERMISSIONS,
    SAMPLE_DATA_MANAGE_PERMISSION,
    SCHEMA_READ_PERMISSION,
    SCHEMA_REFRESH_PERMISSION,
    SECURITY_USER_ROLE_ROUTE_PERMISSIONS,
    SELECT_AI_ASSETS_MANAGE_PERMISSION,
    SELECT_AI_ASSETS_READ_PERMISSION,
    SELECT_AI_ASSETS_REFRESH_PERMISSION,
    SQL_EXECUTE_PERMISSION,
    SYSTEM_STATUS_READ_PERMISSION,
    UNCLASSIFIED_PERMISSION,
    expand_permissions,
    grants_all_profile_access,
    permission_for_route,
)
from app.security.router import auth_router as security_router
from app.security.schemas import (
    CurrentUserData,
    DataEntitlementInput,
    PasswordChangeRequest,
    RoleData,
    UserCreateRequest,
    user_data,
)
from app.security.service import (
    LoginFailed,
    SecurityApiError,
    SecurityService,
    reset_security_service,
)
from app.security.store import (
    InMemorySecurityStore,
    OracleSecurityStore,
    SecurityConflict,
    SecurityMigrationRequired,
    SecurityStore,
)
from app.settings import Settings, get_settings


def _security_endpoint(path: str, method: str) -> Any:
    """共通 router（platform の build_auth_router）の endpoint を path と method で引く。"""
    for route in security_router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return cast(Any, route).endpoint
    raise AssertionError(f"{method} {path} is not registered")


def _settings() -> Settings:
    return Settings.model_construct(
        oracle_user="DBADMIN",
        oracle_password="DbAdminPass!123",
        app_admin_login_user_id="system_admin",
        app_admin_login_user_password="AppAdminPass123",
        oracle_dsn="test",
        nl2sql_persistence_mode="memory",
        auth_mode="authenticated",
        app_auth_failed_login_limit=5,
        app_auth_lockout_minutes=15,
        app_auth_idle_timeout_minutes=60,
        app_auth_absolute_timeout_hours=12,
        app_auth_password_min_length=12,
        app_auth_password_max_length=128,
    )


def _patch_app_admin_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    login_user_id: str = "system_admin",
    password: str = "AppAdminPass123",
) -> Path:
    """構成管理者の資格情報を置く共通 .env（platform/.env。#211）を差し替える。"""
    env_file = tmp_path / "platform.env"
    env_file.write_text(
        "PLATFORM_AUTH_COOKIE_SECURE=false\n"
        f"PLATFORM_ADMIN_LOGIN_USER_ID={login_user_id}\n"
        f"PLATFORM_ADMIN_LOGIN_USER_PASSWORD={password}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.settings.PLATFORM_ENV_FILE", env_file)
    return env_file


def _service() -> SecurityService:
    store = InMemorySecurityStore()
    assert (
        store.bootstrap(
            login_user_id="admin",
            display_name="ADMIN（システム管理者）",
            password_hash=hash_password("BootstrapPass!123"),
        )
        is True
    )
    assert (
        store.bootstrap(
            login_user_id="admin",
            display_name="ADMIN（システム管理者）",
            password_hash=hash_password("BootstrapPass!123"),
        )
        is False
    )
    service = SecurityService(store, _settings())
    return service


def _login(service: SecurityService) -> tuple[Principal, str, str]:
    return service.login("admin", "BootstrapPass!123")


async def _inline_threadpool(
    function: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> object:
    """AnyIO worker が使えない sandbox でも API 契約だけを検証する。"""
    return function(*args, **kwargs)


def _patch_security_threadpools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.security.dependencies.run_in_threadpool", _inline_threadpool)
    monkeypatch.setattr("app.security.router.run_in_threadpool", _inline_threadpool)


def _configure_memory_api_auth(monkeypatch: pytest.MonkeyPatch) -> SecurityService:
    _patch_security_threadpools(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_auth_cookie_secure", False)
    monkeypatch.setattr(settings, "oracle_user", "DBADMIN")
    monkeypatch.setattr(settings, "oracle_password", "DbAdminPass!123")
    monkeypatch.setattr(settings, "app_admin_login_user_id", "system_admin")
    monkeypatch.setattr(settings, "app_admin_login_user_password", "AppAdminPass123")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()
    from app.security.service import get_security_service

    service = get_security_service()
    assert isinstance(service.store, InMemorySecurityStore)
    assert service.store.bootstrap(
        login_user_id="ADMIN",
        display_name="ADMIN（システム管理者）",
        password_hash=hash_password("BootstrapPass!123"),
    )
    return service


async def _login_api(
    client: httpx.AsyncClient,
    login_user_id: str,
    password: str,
) -> tuple[dict[str, object], str]:
    response = await client.post(
        "/api/auth/login",
        json={"login_user_id": login_user_id, "password": password},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("pr_csrf")
    assert csrf
    return cast(dict[str, object], response.json()["data"]), csrf


async def _create_role_api(
    client: httpx.AsyncClient,
    csrf: str,
    *,
    role_code: str,
    display_name: str,
    permissions: list[str],
) -> str:
    """ロール管理で作成し、権限管理で権限を付ける（#206 で API を分けた）。"""
    created = await client.post(
        "/api/security/roles",
        headers={"X-CSRF-Token": csrf},
        json={"role_code": role_code, "display_name": display_name},
    )
    assert created.status_code == 200, created.text
    role = created.json()["data"]
    granted = await client.put(
        f"/api/security/roles/{role['role_id']}/permissions",
        headers={"X-CSRF-Token": csrf},
        json={"version": role["version"], "permissions": permissions},
    )
    assert granted.status_code == 200, granted.text
    return cast(str, role["role_id"])


def _create_active_user(
    service: SecurityService,
    actor: Principal,
    *,
    login_user_id: str,
    display_name: str,
    role_ids: list[str],
    password: str,
) -> UserRecord:
    user, _ = service.create_user(
        login_user_id=login_user_id,
        display_name=display_name,
        role_ids=role_ids,
        temporary_password=password,
        actor=actor,
    )
    service.store.set_password(user.user_uuid, hash_password(password), force_change=False)
    active = service.store.get_user(user.user_uuid)
    assert active is not None
    return active


class _NoAuthTableStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        raise AssertionError(f"auth table store must not be used for env admin: {name}")


class _MissingSecuritySchemaStore:
    def get_user_by_login_user_id(self, _normalized_login_user_id: str) -> UserRecord | None:
        raise RuntimeError('ORA-00942: table or view "ADMIN"."PLATFORM_USERS" does not exist')

    def list_users(self) -> list[UserRecord]:
        raise RuntimeError('ORA-00942: table or view "ADMIN"."PLATFORM_USERS" does not exist')


class _MissingRoleProfilesStore(InMemorySecurityStore):
    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        _ = include_archived
        raise RuntimeError(
            'ORA-00942: table or view "ADMIN"."NL2SQL_APP_ROLE_PROFILES" does not exist'
        )

    def get_role(self, role_id: str) -> RoleRecord | None:
        _ = role_id
        raise RuntimeError(
            'ORA-00942: table or view "ADMIN"."NL2SQL_APP_ROLE_PROFILES" does not exist'
        )


class _RecordingCursor:
    def __init__(self, fetchone_rows: list[tuple[int]] | None = None) -> None:
        self.executed: list[tuple[str, dict[str, object]]] = []
        self._fetchone_rows = list(fetchone_rows or [])

    def execute(self, sql: str, params: Mapping[str, object] | None = None) -> None:
        self.executed.append((sql, dict(params or {})))

    def fetchone(self) -> tuple[int]:
        return self._fetchone_rows.pop(0) if self._fetchone_rows else (0,)


class _BareMissingTableCursor:
    def __enter__(self) -> _BareMissingTableCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ORA-00942: table or view does not exist")


class _BareMissingTableConnection:
    def cursor(self) -> _BareMissingTableCursor:
        return _BareMissingTableCursor()


class _BareMissingTableAdapter:
    @contextmanager
    def connection(self) -> Iterator[_BareMissingTableConnection]:
        yield _BareMissingTableConnection()

    # 共通認証の store は pool の接続を借りる（#793）。
    pooled_connection = connection


def test_oracle_store_maps_bare_ora_00942_to_operation_security_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OracleSecurityStore(_settings())
    monkeypatch.setattr(store, "_adapter", _BareMissingTableAdapter())
    operations = [
        (store.list_users, "PLATFORM_USERS"),
        (store.list_roles, "PLATFORM_ROLES"),
        (lambda: store.get_session_by_token_hash("missing"), "PLATFORM_AUTH_SESSIONS"),
        (store.get_deepsec_states, "NL2SQL_DEEPSEC_MIGRATIONS"),
    ]

    for operation, expected_object in operations:
        with pytest.raises(SecurityMigrationRequired) as exc_info:
            operation()  # type: ignore[operator]
        assert exc_info.value.object_name == expected_object


def test_debug_auth_bypass_is_local_only_and_has_system_admin_permissions() -> None:
    local = Settings.model_construct(auth_mode="local", environment="local")
    production = Settings.model_construct(auth_mode="local", environment="production")
    disabled = Settings.model_construct(auth_mode="authenticated", environment="local")

    assert local.local_debug_enabled is True
    assert production.local_debug_enabled is False
    assert disabled.local_debug_enabled is False

    principal = local_debug_principal()
    assert principal.is_system_admin is True
    assert principal.force_password_change is False
    assert principal.password_change_allowed is False
    assert principal.permissions == set(ALL_PERMISSION_CODES)


def test_local_debug_me_and_logout_need_no_session_or_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")

    async def exercise() -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/logout",
                "headers": [],
                "client": ("127.0.0.1", 50000),
            }
        )
        authorization = cast(AsyncGenerator[None, None], authorize_api_request(request))
        await anext(authorization)
        me = _security_endpoint("/auth/me", "GET")
        logout = _security_endpoint("/auth/logout", "POST")
        change_password = _security_endpoint("/auth/password/change", "POST")
        current = me(request)
        assert current.data is not None
        assert current.data.model_dump() == {
            "user_uuid": "00000000-0000-0000-0000-000000000000",
            "login_user_id": "local-debug",
            "display_name": "ローカル DEBUG 管理者",
            "status": "ACTIVE",
            "force_password_change": False,
            "role_codes": ["SYSTEM_ADMIN"],
            "role_display_names": ["システム管理者"],
            "is_system_admin": True,
            "permissions": sorted(ALL_PERMISSION_CODES),
            "data_entitlements": [],
            "allowed_profile_ids": [],
            "debug_mode": True,
            "password_change_allowed": False,
            # 画面の日時の表示に使うシステムの時間帯（PLATFORM_TIMEZONE。#3265）。
            "timezone": "Asia/Tokyo",
        }
        logged_out = logout(request, Response())
        assert logged_out.data == {"logged_out": False}
        with pytest.raises(SecurityApiError, match="DEBUG"):
            change_password(
                PasswordChangeRequest(
                    current_password="unused",
                    new_password="unused",
                ),
                request,
                Response(),
            )
        await authorization.aclose()

    asyncio.run(exercise())


def test_debug_flag_cannot_bypass_auth_outside_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "auth_mode", "authenticated")

    class RejectingSecurityService:
        @staticmethod
        def authenticate_session(token: str) -> Principal:
            assert token == ""
            raise SecurityApiError(401, "ログインしてください。")

    async def inline_threadpool(function: Callable[..., object], *args: object) -> object:
        return function(*args)

    monkeypatch.setattr(
        security_dependencies,
        "get_security_service",
        lambda: RejectingSecurityService(),
    )
    monkeypatch.setattr(security_dependencies, "run_in_threadpool", inline_threadpool)

    async def exercise() -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/auth/me",
                "headers": [],
                "client": ("127.0.0.1", 50000),
                "route": SimpleNamespace(path="/auth/me"),
            }
        )
        authorization = authorize_api_request(request)
        # 共通認証は error_code を落とさないよう SecurityApiError のまま返す（#224）。
        with pytest.raises(SecurityApiError) as error:
            await anext(authorization)
        assert error.value.status_code == 401

    asyncio.run(exercise())


def test_bootstrap_login_session_and_password_independence() -> None:
    service = _service()
    principal, token, csrf = _login(service)
    assert principal.is_system_admin
    assert principal.force_password_change
    assert service.authenticate_session(token).user_uuid == principal.user_uuid
    service.verify_csrf(principal, csrf, csrf)
    with pytest.raises(SecurityApiError, match="安全性"):
        service.verify_csrf(principal, csrf, "different")

    service.change_password(principal, "BootstrapPass!123", "IndependentPass!456")
    with pytest.raises(SecurityApiError, match="ログイン"):
        service.authenticate_session(token)
    changed, _, _ = service.login("ADMIN", "IndependentPass!456")
    assert changed.force_password_change is False


def test_current_user_data_marks_non_reserved_login_with_system_admin_role() -> None:
    service = _service()
    principal, _, _ = _login(service)

    current = CurrentUserData.from_principal(principal)

    assert current.login_user_id == "admin"
    assert current.is_system_admin is True
    assert current.permissions == sorted(principal.permissions)
    assert principal.has_permission("menu.security_users") is True


def test_login_session_uses_sixty_minute_default_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    monkeypatch.setattr("pr_system_settings.auth.service._now", lambda: now)
    service = _service()

    principal, token, _csrf = _login(service)

    store = cast(InMemorySecurityStore, service.store)
    session = next(iter(store.sessions.values()))
    assert principal.session_id == session.session_id
    assert session.idle_expires_at == now + timedelta(minutes=60)
    assert session.absolute_expires_at == now + timedelta(hours=12)
    assert service.authenticate_session(token).user_uuid == principal.user_uuid


def test_session_activity_refreshes_idle_timeout_without_exceeding_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_at = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    current_time = {"value": login_at}
    monkeypatch.setattr("pr_system_settings.auth.service._now", lambda: current_time["value"])
    service = _service()
    _principal, token, _csrf = _login(service)
    store = cast(InMemorySecurityStore, service.store)
    session_id = next(iter(store.sessions))
    store.sessions[session_id].idle_expires_at = login_at + timedelta(hours=12)

    current_time["value"] = login_at + timedelta(hours=11, minutes=30)
    authenticated = service.authenticate_session(token)

    refreshed = store.sessions[session_id]
    assert authenticated.session_id == session_id
    assert refreshed.last_seen_at == current_time["value"]
    assert refreshed.idle_expires_at == refreshed.absolute_expires_at
    assert refreshed.absolute_expires_at == login_at + timedelta(hours=12)


@pytest.mark.parametrize("expired_field", ["idle_expires_at", "absolute_expires_at"])
def test_expired_session_is_revoked(
    expired_field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    monkeypatch.setattr("pr_system_settings.auth.service._now", lambda: now)
    service = _service()
    _principal, token, _csrf = _login(service)
    store = cast(InMemorySecurityStore, service.store)
    session_id = next(iter(store.sessions))
    setattr(store.sessions[session_id], expired_field, now - timedelta(seconds=1))

    with pytest.raises(SecurityApiError, match="セッションの有効期限"):
        service.authenticate_session(token)

    assert store.sessions[session_id].revoked_at is not None


def test_configured_system_admin_login_uses_app_admin_credentials_without_auth_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_app_admin_env(monkeypatch, tmp_path)
    store = _NoAuthTableStore()
    service = SecurityService(cast(SecurityStore, store), _settings())

    principal, token, csrf = service.login("system_admin", "AppAdminPass123")

    assert principal.is_system_admin
    assert principal.force_password_change is False
    assert principal.password_change_allowed is True
    assert principal.login_user_id == "system_admin"
    assert token.startswith("platform-system-admin-v1.")
    assert csrf
    authenticated = service.authenticate_session(token)
    assert authenticated.is_system_admin
    assert authenticated.user_uuid == principal.user_uuid
    assert authenticated.password_change_allowed is True
    service.verify_csrf(authenticated, csrf, csrf)
    service.logout(authenticated)
    assert store.calls == []


def test_configured_system_admin_wrong_password_does_not_touch_auth_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_app_admin_env(monkeypatch, tmp_path)
    store = _NoAuthTableStore()
    service = SecurityService(cast(SecurityStore, store), _settings())

    with pytest.raises(LoginFailed):
        service.login("system_admin", "wrong-password")
    assert store.calls == []


@pytest.mark.parametrize("login_user_id", ["System_Admin", "SYSTEM_ADMIN"])
def test_system_admin_case_variants_do_not_fall_back_to_db_users(
    login_user_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_app_admin_env(monkeypatch, tmp_path)
    store = _NoAuthTableStore()
    service = SecurityService(cast(SecurityStore, store), _settings())

    with pytest.raises(LoginFailed):
        service.login(login_user_id, "AppAdminPass123")
    assert store.calls == []


def test_configured_system_admin_requires_fixed_login_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_app_admin_env(monkeypatch, tmp_path, login_user_id="app-admin")
    service = SecurityService(cast(SecurityStore, _NoAuthTableStore()), _settings())

    with pytest.raises(SecurityApiError) as error:
        service.login("system_admin", "AppAdminPass123")
    assert error.value.status_code == 503
    assert "system_admin" in error.value.public_message


def test_configured_system_admin_ignores_legacy_keys_and_product_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧キー（APP_ADMIN_*）と製品の backend/.env は読まない（#211。互換なし）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text(
        "APP_ADMIN_LOGIN_USER_ID=system_admin\nAPP_ADMIN_PASSWORD=LegacyAdminPass123\n",
        encoding="utf-8",
    )
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "PLATFORM_ADMIN_LOGIN_USER_ID=system_admin\n"
        "PLATFORM_ADMIN_LOGIN_USER_PASSWORD=ProductEnvPass123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.settings.PLATFORM_ENV_FILE", platform_env)
    monkeypatch.setattr("app.settings.BACKEND_ENV_FILE", backend_env)
    service = SecurityService(cast(SecurityStore, _NoAuthTableStore()), _settings())

    for password in ("LegacyAdminPass123", "ProductEnvPass123"):
        with pytest.raises(LoginFailed):
            service.login("system_admin", password)
    # どちらにもなければ Settings（環境変数）の値を使う。
    principal, _token, _csrf = service.login("system_admin", "AppAdminPass123")
    assert principal.login_user_id == "system_admin"


def test_configured_system_admin_password_change_updates_env_without_auth_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = _patch_app_admin_env(monkeypatch, tmp_path)
    store = _NoAuthTableStore()
    service = SecurityService(cast(SecurityStore, store), _settings())
    principal, token, _csrf = service.login("system_admin", "AppAdminPass123")

    changed = service.change_password(principal, "AppAdminPass123", "UpdatedPass123A")

    assert changed.user_uuid == principal.user_uuid
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_ADMIN_LOGIN_USER_ID=system_admin" in env_text
    assert "PLATFORM_ADMIN_LOGIN_USER_PASSWORD=UpdatedPass123A" in env_text
    # 共通 .env の他の値は残す。
    assert "PLATFORM_AUTH_COOKIE_SECURE=false" in env_text
    assert "AppAdminPass123" not in env_text
    with pytest.raises(SecurityApiError):
        service.authenticate_session(token)
    with pytest.raises(LoginFailed):
        service.login("system_admin", "AppAdminPass123")
    relogged, _, _ = service.login("system_admin", "UpdatedPass123A")
    assert relogged.is_system_admin
    assert store.calls == []


def test_database_credentials_no_longer_bypass_auth_tables() -> None:
    service = SecurityService(cast(SecurityStore, _MissingSecuritySchemaStore()), _settings())

    with pytest.raises(SecurityApiError) as error:
        service.login("DBADMIN", "DbAdminPass!123")
    assert error.value.status_code == 409
    assert error.value.code == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    assert "app_security_migrate" in error.value.public_message


def test_table_user_login_reports_migration_required_when_auth_table_is_missing() -> None:
    service = SecurityService(cast(SecurityStore, _MissingSecuritySchemaStore()), _settings())

    with pytest.raises(SecurityApiError) as error:
        service.login("app.user", "AppUserPass!123")
    assert error.value.status_code == 409
    assert error.value.code == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    assert "app_security_migrate" in error.value.public_message


def test_list_users_reports_security_migration_required() -> None:
    service = SecurityService(cast(SecurityStore, _MissingSecuritySchemaStore()), _settings())

    with pytest.raises(SecurityApiError) as error:
        service.list_users()

    assert error.value.status_code == 409
    assert error.value.code == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    assert "PLATFORM_USERS" not in error.value.public_message


def test_missing_role_profiles_table_reports_security_migration_required() -> None:
    service = SecurityService(cast(SecurityStore, _MissingRoleProfilesStore()), _settings())

    with pytest.raises(SecurityApiError) as list_error:
        service.list_roles(include_archived=True)
    with pytest.raises(SecurityApiError) as get_error:
        service.get_role("role-id")

    for error in (list_error.value, get_error.value):
        assert error.status_code == 409
        assert "app_security_migrate" in error.public_message
        assert "--skip-bootstrap" in error.public_message


def test_password_policy_requires_all_character_classes_without_expiry() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password("onlylowercase", login_user_id="user", min_length=12, max_length=128)
    with pytest.raises(PasswordPolicyError, match="推測"):
        validate_password("Password123!", login_user_id="user", min_length=12, max_length=128)
    validate_password("StrongPass!234", login_user_id="user", min_length=12, max_length=128)


def test_password_policy_ignores_login_user_id_similarity_for_short_names() -> None:
    validate_password("AaaStrong!234", login_user_id="a", min_length=12, max_length=128)
    validate_password("Number1!2345", login_user_id="1", min_length=12, max_length=128)
    with pytest.raises(PasswordPolicyError, match="推測"):
        validate_password("AbcStrong!234", login_user_id="abc", min_length=12, max_length=128)


def test_security_migration_splitter_never_executes_comment_only_buffers() -> None:
    assert split_ddl("-- header\nCREATE TABLE EXAMPLE (ID NUMBER);\n-- trailing") == [
        "CREATE TABLE EXAMPLE (ID NUMBER)"
    ]


def test_security_ddl_is_the_final_shape_without_legacy_migrations() -> None:
    """セキュリティの DDL は 004 の 1 つだけで、最終の形の表を作る（#1648）。

    旧名・旧形からの移行（改名・列の追加・行の削除と追加）の migration は、初めての release の前に
    削除した。圧縮表でも作れるよう、JSON の列は VARCHAR2 のままにする。
    """
    migration_dir = Path(__file__).resolve().parents[1] / "migrations"
    base_sql = (migration_dir / "004_app_security_rbac.sql").read_text(encoding="utf-8")

    assert "TARGET_TYPE VARCHAR2(32)" in base_sql
    assert "COLUMN_NAMES VARCHAR2(4000)" in base_sql
    assert "SCOPE_FILTERS VARCHAR2(4000)" in base_sql
    assert "SCOPE_EXPRESSION CLOB" in base_sql
    assert "'EXPRESSION'" in base_sql
    assert "CREATE TABLE NL2SQL_APP_ROLE_PROFILES" in base_sql
    assert "PRIMARY KEY (ROLE_ID, PROFILE_ID)" in base_sql
    assert base_sql.count("ON DELETE CASCADE") == 2
    statements = split_ddl(base_sql)
    assert all(statement.startswith("CREATE TABLE") for statement in statements)
    for removed in (
        "005_security_namespace_nl2sql.sql",
        "009_remove_security_audit_log.sql",
        "016_app_role_profiles.sql",
        "023_security_permissions_menu.sql",
        "024_platform_auth_tables.sql",
        "025_role_access_cascade.sql",
    ):
        assert not (migration_dir / removed).exists()


def test_dashboard_permission_is_retired_for_appearance_settings() -> None:
    assert "dashboard.view" not in ALL_PERMISSION_CODES
    assert "menu.settings_appearance" in ALL_PERMISSION_CODES

    service = _service()
    actor, _, _ = _login(service)

    role = service.create_role(
        role_code="LEGACY_DASHBOARD",
        display_name="旧ダッシュボード",
        description="",
        permissions={"dashboard.view"},
        entitlements=[],
        actor=actor,
    )
    assert role.permissions == {"menu.settings_appearance"}


def test_stale_permission_codes_are_hidden_from_role_api_and_principals() -> None:
    service = _service()
    store = service.store
    actor, _, _ = _login(service)

    store.create_role(
        RoleRecord(
            role_id="role-stale",
            role_code="STALE_ROLE",
            display_name="旧権限を含むロール",
            description="",
            is_built_in=False,
            archived=False,
            version=1,
            permissions={"dashboard.view", "menu.settings_appearance", "search.view"},
        )
    )
    service.create_user(
        login_user_id="stale.user",
        display_name="旧権限ユーザー",
        role_ids=["role-stale"],
        temporary_password="IndependentPass!456",
        actor=actor,
    )

    stale_role = store.get_role("role-stale")
    assert stale_role is not None
    assert "dashboard.view" not in RoleData.from_record(stale_role).permissions

    principal, _, _ = service.login("stale.user", "IndependentPass!456")
    assert "dashboard.view" not in principal.permissions
    assert {"menu.settings_appearance", "menu.query", "menu.direct_sql"} <= principal.permissions


def test_data_entitlement_capability_is_structured() -> None:
    with pytest.raises(ValueError, match="capability"):
        DataEntitlementInput(
            resource_code="HR.EMPLOYEES",
            scope_code="SALES",
            capability="ARBITRARY_SQL",
        )


@pytest.mark.parametrize("resource_code", ["SALES.ORDER$HIST", "APP#DATA.T#1"])
def test_data_entitlement_input_accepts_oracle_identifier_special_chars(
    resource_code: str,
) -> None:
    """DeepSec は resource_code に OWNER.OBJECT を入れるため $ # を含む Oracle 名も受理する。"""
    record = DataEntitlementInput(resource_code=resource_code.lower(), capability="select")
    assert record.resource_code == resource_code


def test_data_entitlement_input_generates_scope_filter_code() -> None:
    record = DataEntitlementInput(
        resource_code="HR.ORDERS",
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        column_names=["REGION_CODE"],
        scope_mode="FILTERS",
        scope_filters=[
            {
                "column_name": "REGION_CODE",
                "operator": "EQ",
                "value_type": "TEXT",
                "value": "SALES",
            }
        ],
    ).to_record("role-sales")

    assert record.scope_code == scope_filters_scope_code(record.scope_filters)
    assert record.scope_code.startswith("FILTERS:")
    assert record.scope_filters[0].column_name == "REGION_CODE"


def test_data_entitlement_input_accepts_number_app_user_id_scope_filter() -> None:
    record = DataEntitlementInput(
        resource_code="HR.ORDERS",
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        column_names=["APP_OWNER_USER_ID"],
        scope_mode="FILTERS",
        scope_filters=[
            {
                "column_name": "APP_OWNER_USER_ID",
                "operator": "EQ",
                "value_type": "NUMBER",
                "value_source": "LOGIN_USER_ID",
                "value": "123",
            }
        ],
    ).to_record("role-sales")

    assert record.scope_filters[0].value_source == "LOGIN_USER_ID"
    assert record.scope_filters[0].value == ""
    assert record.scope_filters[0].value_type == "NUMBER"


@pytest.mark.parametrize("value", ["1.5", "-1", "0", ""])
def test_data_entitlement_input_rejects_number_eq_non_positive_integer(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="正整数"):
        DataEntitlementInput(
            resource_code="HR.ORDERS",
            capability="SELECT",
            target_owner="HR",
            target_object="ORDERS",
            column_names=["APP_OWNER_USER_ID"],
            scope_mode="FILTERS",
            scope_filters=[
                {
                    "column_name": "APP_OWNER_USER_ID",
                    "operator": "EQ",
                    "value_type": "NUMBER",
                    "value": value,
                }
            ],
        )


def test_deepsec_entitlement_update_preserves_role_metadata_and_permissions() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="メニュー権限は DeepSec 画面から変更しない",
        permissions={"menu.query"},
        entitlements=[("HR.EMPLOYEES", "SALES", "SELECT")],
        actor=actor,
    )

    updated = service.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[
            ("HR.EMPLOYEES", "SALES", "SELECT"),
            ("HR.EMPLOYEES", "HR", "SELECT"),
        ],
        actor=actor,
    )

    assert updated.version == role.version + 1
    assert updated.role_code == role.role_code
    assert updated.display_name == role.display_name
    assert updated.description == role.description
    assert updated.permissions == role.permissions
    assert {(item.scope_code, item.capability) for item in updated.entitlements} == {
        ("SALES", "SELECT"),
        ("HR", "SELECT"),
    }
    with pytest.raises(SecurityApiError, match="更新"):
        service.update_role_data_entitlements(
            role.role_id,
            expected_version=role.version,
            entitlements=[],
            actor=actor,
        )
    with pytest.raises(SecurityApiError, match="SYSTEM_ADMIN"):
        service.update_role_data_entitlements(
            SYSTEM_ADMIN_ROLE_ID,
            expected_version=1,
            entitlements=[],
            actor=actor,
        )


def test_deepsec_entitlement_update_preserves_applied_state_when_policy_unchanged() -> None:
    service = _service()
    store = cast(InMemorySecurityStore, service.store)
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    applied_at = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-1",
        role_id=role.role_id,
        resource_code="HR.EMPLOYEES",
        scope_code="*",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode="ALL",
        data_grant_name="NL2SQL_DG_APPLIED",
        sql_checksum="abc123",
        apply_status="APPLIED",
        applied_at=applied_at,
    )
    store.roles[role.role_id] = RoleRecord(
        role_id=role.role_id,
        role_code=role.role_code,
        display_name=role.display_name,
        description=role.description,
        is_built_in=role.is_built_in,
        archived=role.archived,
        version=role.version,
        permissions=set(role.permissions),
        entitlements=[entitlement],
    )

    updated = service.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[replace(entitlement)],
        actor=actor,
    )

    stored = updated.entitlements[0]
    assert stored.apply_status == "APPLIED"
    assert stored.apply_error_message == ""
    assert stored.data_grant_name == "NL2SQL_DG_APPLIED"
    assert stored.sql_checksum == "abc123"
    assert stored.applied_at == applied_at


def test_deepsec_entitlement_update_resets_apply_state_when_policy_changes() -> None:
    service = _service()
    store = cast(InMemorySecurityStore, service.store)
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    applied_at = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-1",
        role_id=role.role_id,
        resource_code="HR.EMPLOYEES",
        scope_code="*",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode="ALL",
        data_grant_name="NL2SQL_DG_APPLIED",
        sql_checksum="abc123",
        apply_status="APPLIED",
        applied_at=applied_at,
    )
    store.roles[role.role_id] = RoleRecord(
        role_id=role.role_id,
        role_code=role.role_code,
        display_name=role.display_name,
        description=role.description,
        is_built_in=role.is_built_in,
        archived=role.archived,
        version=role.version,
        permissions=set(role.permissions),
        entitlements=[entitlement],
    )
    changed = replace(
        entitlement,
        column_names=["EMPLOYEE_ID"],
        apply_error_message="client supplied stale error",
    )

    updated = service.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[changed],
        actor=actor,
    )

    stored = updated.entitlements[0]
    assert stored.apply_status == "PENDING"
    assert stored.apply_error_message == ""
    assert stored.data_grant_name == "NL2SQL_DG_APPLIED"
    assert stored.sql_checksum == ""
    assert stored.applied_at is None


def test_deepsec_entitlement_update_resets_apply_state_when_scope_filters_change() -> None:
    service = _service()
    store = cast(InMemorySecurityStore, service.store)
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    filters = [
        DataEntitlementScopeFilter(
            column_name="REGION_CODE",
            operator="EQ",
            value_type="TEXT",
            value="SALES",
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-1",
        role_id=role.role_id,
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["REGION_CODE"],
        scope_mode="FILTERS",
        scope_filters=filters,
        data_grant_name="NL2SQL_DG_APPLIED",
        sql_checksum="abc123",
        apply_status="APPLIED",
        applied_at=datetime(2026, 8, 23, 1, 0, tzinfo=UTC),
    )
    store.roles[role.role_id] = RoleRecord(
        role_id=role.role_id,
        role_code=role.role_code,
        display_name=role.display_name,
        description=role.description,
        is_built_in=role.is_built_in,
        archived=role.archived,
        version=role.version,
        permissions=set(role.permissions),
        entitlements=[entitlement],
    )
    changed_filters = [replace(filters[0], value="HR")]

    updated = service.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[
            replace(
                entitlement,
                scope_code=scope_filters_scope_code(changed_filters),
                scope_filters=changed_filters,
            )
        ],
        actor=actor,
    )

    stored = updated.entitlements[0]
    assert stored.scope_code == scope_filters_scope_code(changed_filters)
    assert stored.apply_status == "PENDING"
    assert stored.sql_checksum == ""
    assert stored.applied_at is None


def test_deepsec_entitlement_update_rejects_client_supplied_applied_state_for_new_policy() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    forged = DataEntitlementRecord(
        entitlement_id="entitlement-forged",
        role_id=role.role_id,
        resource_code="HR.EMPLOYEES",
        scope_code="*",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID"],
        scope_mode="ALL",
        data_grant_name="NL2SQL_DG_FORGED",
        sql_checksum="forged",
        apply_status="APPLIED",
        applied_at=datetime(2026, 8, 23, 1, 0, tzinfo=UTC),
    )

    updated = service.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[forged],
        actor=actor,
    )

    stored = updated.entitlements[0]
    assert stored.apply_status == "PENDING"
    assert stored.sql_checksum == ""
    assert stored.applied_at is None


def test_oracle_role_access_uses_safe_data_entitlement_bind_names() -> None:
    cursor = _RecordingCursor()
    role = RoleRecord(
        role_id="role-1",
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        is_built_in=False,
        archived=False,
        version=1,
        permissions={"search.execute", "search.view"},
        entitlements=[
            DataEntitlementRecord(
                entitlement_id="entitlement-1",
                role_id="role-1",
                resource_code="HR.EMPLOYEES",
                scope_code="SALES",
                capability="SELECT",
                target_owner="HR",
                target_object="EMPLOYEES",
                target_type="TABLE",
                column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
                scope_mode="COLUMN_EQUALS",
                scope_column="DEPARTMENT_CODE",
            )
        ],
    )

    OracleSecurityStore._replace_role_access(cursor, role)

    permission_inserts = [
        item for item in cursor.executed if "INSERT INTO NL2SQL_APP_ROLE_PERMISSIONS" in item[0]
    ]
    assert len(permission_inserts) == 2
    data_inserts = [
        item for item in cursor.executed if "INSERT INTO NL2SQL_APP_DATA_ENTITLEMENTS" in item[0]
    ]
    assert len(data_inserts) == 1
    sql, params = data_inserts[0]
    assert re.search(r":(?:id|resource|scope|capability)\b", sql) is None
    assert params == {
        "entitlement_id": "entitlement-1",
        "role_id": "role-1",
        "resource_code": "HR.EMPLOYEES",
        "scope_code": "SALES",
        "capability_code": "SELECT",
        "target_owner": "HR",
        "target_object": "EMPLOYEES",
        "target_type": "TABLE",
        "column_names": '["EMPLOYEE_ID","DISPLAY_NAME"]',
        "scope_mode": "COLUMN_EQUALS",
        "scope_column": "DEPARTMENT_CODE",
        "scope_filters": "[]",
        "scope_expression": None,
        "data_grant_name": None,
        "sql_checksum": None,
        "apply_status": "PENDING",
        "apply_error_message": "-",
        "applied_at": None,
    }


def test_bootstrap_system_admin_has_no_fake_probe_entitlement() -> None:
    service = _service()
    service.bootstrap()

    system_admin = service.store.get_role(SYSTEM_ADMIN_ROLE_ID)

    assert system_admin is not None
    assert system_admin.entitlements == []


def test_multiple_roles_union_permissions_and_data_entitlements() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role_a = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"search.view"},
        entitlements=[("HR.EMPLOYEES", "SALES", "SELECT")],
        allowed_profile_ids={"sales-profile"},
        actor=actor,
    )
    role_b = service.create_role(
        role_code="QUERY_RUNNER",
        display_name="検索実行",
        description="",
        permissions={"search.execute"},
        entitlements=[("FIN.INVOICES", "APPROVED", "SELECT")],
        allowed_profile_ids={"finance-profile"},
        actor=actor,
    )
    user, password = service.create_user(
        login_user_id="query.user",
        display_name="検索ユーザー",
        role_ids=[role_a.role_id, role_b.role_id],
        temporary_password="QueryUserPass!123",
        actor=actor,
    )
    assert password == "QueryUserPass!123"
    principal, _, _ = service.login(user.login_user_id, password)
    assert principal.permissions >= {
        "menu.query",
        "menu.direct_sql",
        "menu.sql_to_question",
        "menu.history",
    }
    assert {(item.scope_code, item.capability) for item in principal.data_entitlements} == {
        ("SALES", "SELECT"),
        ("APPROVED", "SELECT"),
    }
    assert principal.allowed_profile_ids == {"sales-profile", "finance-profile"}


def test_role_profile_access_is_exposed_on_role_and_current_user_data() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="PROFILE_SCOPED_QUERY",
        display_name="業務 profile 制限",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        allowed_profile_ids={"finance", "sales"},
        actor=actor,
    )
    user, password = service.create_user(
        login_user_id="profile.scoped",
        display_name="業務 profile 制限ユーザー",
        role_ids=[role.role_id],
        temporary_password="ProfileScopedPass!123",
        actor=actor,
    )

    principal, _, _ = service.login(user.login_user_id, password)

    assert RoleData.from_record(role).allowed_profile_ids == ["finance", "sales"]
    assert CurrentUserData.from_principal(principal).allowed_profile_ids == [
        "finance",
        "sales",
    ]


def test_profile_manage_role_grants_all_profile_access_without_stored_ids() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="PROFILE_MANAGER",
        display_name="業務 profile 管理",
        description="",
        permissions={"menu.profiles"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=actor,
    )
    user, password = service.create_user(
        login_user_id="profile.manager.all",
        display_name="業務 profile 全体管理ユーザー",
        role_ids=[role.role_id],
        temporary_password="ProfileManagerPass!123",
        actor=actor,
    )

    principal, _, _ = service.login(user.login_user_id, password)

    assert role.allowed_profile_ids == set()
    assert principal.allowed_profile_ids == set()
    assert principal.has_permission(PROFILE_MANAGE_PERMISSION)
    assert principal.can_use_profile("finance")


def test_non_system_admin_cannot_change_role_profile_access() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role_manager = service.create_role(
        role_code="ROLE_MANAGER",
        display_name="ロール管理",
        description="",
        permissions={"menu.security_roles"},
        entitlements=[],
        actor=actor,
    )
    target = service.create_role(
        role_code="PROFILE_TARGET",
        display_name="業務 profile 対象",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=actor,
    )
    manager_user, password = service.create_user(
        login_user_id="role.manager",
        display_name="ロール管理者",
        role_ids=[role_manager.role_id],
        temporary_password="RoleManagerPass!123",
        actor=actor,
    )
    manager, _, _ = service.login(manager_user.login_user_id, password)

    with pytest.raises(SecurityApiError) as clear_error:
        service.update_role(
            target.role_id,
            expected_version=target.version,
            display_name=target.display_name,
            description=target.description,
            permissions=set(target.permissions),
            allowed_profile_ids=set(),
            actor=manager,
        )
    assert clear_error.value.status_code == 403

    unchanged = service.update_role(
        target.role_id,
        expected_version=target.version,
        display_name="業務 profile 対象更新",
        description=target.description,
        permissions=set(target.permissions),
        allowed_profile_ids=None,
        actor=manager,
    )

    assert unchanged.allowed_profile_ids == {"default"}


@pytest.mark.parametrize("role_code", [SYSTEM_ADMIN_ROLE_CODE, "system_admin", " System_Admin "])
def test_reserved_system_admin_role_code_cannot_be_created(role_code: str) -> None:
    service = _service()
    actor, _, _ = _login(service)

    with pytest.raises(SecurityApiError) as error:
        service.create_role(
            role_code=role_code,
            display_name="追加システム管理者",
            description="",
            permissions={"menu.security_roles"},
            entitlements=[],
            actor=actor,
        )

    assert error.value.status_code == 409
    assert error.value.code == "SECURITY_ROLE_CODE_RESERVED"
    assert error.value.field_errors == (
        {
            "pointer": "/role_code",
            "code": "reserved",
            "message": (
                "SYSTEM_ADMIN は組み込みロール専用のコードです。"
                "別のロールコードを入力してください。"
            ),
        },
    )
    assert [role.role_id for role in service.store.list_roles(include_archived=True)] == [
        SYSTEM_ADMIN_ROLE_ID
    ]


def test_last_system_admin_cannot_be_disabled_or_unassigned() -> None:
    service = _service()
    actor, _, _ = _login(service)
    admin = service.store.get_user(actor.user_uuid)
    assert admin is not None
    assert admin.role_ids == [SYSTEM_ADMIN_ROLE_ID]
    with pytest.raises(SecurityApiError, match="最後のシステム管理者"):
        service.update_user(
            admin.user_uuid,
            expected_version=admin.version,
            display_name=admin.display_name,
            status="DISABLED",
            role_ids=admin.role_ids,
            actor=actor,
        )
    with pytest.raises(SecurityConflict, match="最後のシステム管理者"):
        service.store.update_user(
            admin.user_uuid,
            expected_version=admin.version,
            display_name=admin.display_name,
            status="DISABLED",
            role_ids=admin.role_ids,
        )


def test_bootstrap_user_is_marked_in_user_response() -> None:
    service = _service()
    actor, _, _ = _login(service)
    admin = service.store.get_user(actor.user_uuid)

    assert admin is not None
    assert admin.is_bootstrap_admin is True
    assert user_data(admin).is_bootstrap_admin is True


def test_disabled_user_can_be_deleted_with_sessions_and_login_id_reused() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="DELETE_USER_ROLE",
        display_name="削除対象ユーザー用",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    user, password = service.create_user(
        login_user_id="delete.target",
        display_name="削除対象",
        role_ids=[role.role_id],
        temporary_password="DeleteTargetPass!123",
        actor=actor,
    )
    service.login(user.login_user_id, password)
    disabled = service.update_user(
        user.user_uuid,
        expected_version=user.version,
        display_name=user.display_name,
        status="DISABLED",
        role_ids=user.role_ids,
        actor=actor,
    )

    deleted = service.delete_user(
        user.user_uuid,
        expected_version=disabled.version,
        actor=actor,
    )

    assert deleted.user_uuid == user.user_uuid
    assert service.store.get_user(user.user_uuid) is None
    assert isinstance(service.store, InMemorySecurityStore)
    assert all(session.user_uuid != user.user_uuid for session in service.store.sessions.values())
    recreated, _ = service.create_user(
        login_user_id="delete.target",
        display_name="再作成ユーザー",
        role_ids=[role.role_id],
        temporary_password="RecreatedPass!123",
        actor=actor,
    )
    assert recreated.user_uuid != user.user_uuid


def test_user_delete_rejects_active_self_bootstrap_and_stale_version() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="DELETE_USER_GUARDS",
        display_name="削除ガード",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    user, _ = service.create_user(
        login_user_id="delete.guards",
        display_name="削除ガード対象",
        role_ids=[role.role_id],
        temporary_password="DeleteGuardsPass!123",
        actor=actor,
    )

    with pytest.raises(SecurityApiError) as active_error:
        service.delete_user(user.user_uuid, expected_version=user.version, actor=actor)
    assert active_error.value.code == "SECURITY_USER_DELETE_REQUIRES_DISABLED"

    bootstrap = service.store.get_user(actor.user_uuid)
    assert bootstrap is not None
    with pytest.raises(SecurityApiError) as self_error:
        service.delete_user(bootstrap.user_uuid, expected_version=bootstrap.version, actor=actor)
    assert self_error.value.code == "SECURITY_USER_DELETE_SELF_FORBIDDEN"

    other_admin = replace(actor, user_uuid="configured-system-admin")
    with pytest.raises(SecurityApiError) as protected_error:
        service.delete_user(
            bootstrap.user_uuid,
            expected_version=bootstrap.version,
            actor=other_admin,
        )
    assert protected_error.value.code == "SECURITY_USER_DELETE_PROTECTED"

    disabled = service.update_user(
        user.user_uuid,
        expected_version=user.version,
        display_name=user.display_name,
        status="DISABLED",
        role_ids=user.role_ids,
        actor=actor,
    )
    with pytest.raises(SecurityApiError) as stale_error:
        service.delete_user(
            user.user_uuid,
            expected_version=disabled.version - 1,
            actor=actor,
        )
    assert stale_error.value.code == "SECURITY_STATE_CONFLICT"
    assert service.store.get_user(user.user_uuid) is not None


def test_archived_unassigned_role_without_entitlements_can_be_deleted() -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="DELETE_ROLE_TARGET",
        display_name="削除対象ロール",
        description="削除対象",
        permissions={"menu.query"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=actor,
    )
    archived = service.archive_role(
        role.role_id,
        expected_version=role.version,
        actor=actor,
    )

    deleted = service.delete_role(
        role.role_id,
        expected_version=archived.version,
        actor=actor,
    )

    assert deleted.role_code == "DELETE_ROLE_TARGET"
    assert service.store.get_role(role.role_id) is None


def test_role_delete_rejects_active_built_in_assigned_entitled_and_stale_targets() -> None:
    service = _service()
    actor, _, _ = _login(service)
    active = service.create_role(
        role_code="DELETE_ROLE_ACTIVE",
        display_name="有効ロール",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    with pytest.raises(SecurityApiError) as active_error:
        service.delete_role(active.role_id, expected_version=active.version, actor=actor)
    assert active_error.value.code == "SECURITY_ROLE_DELETE_REQUIRES_ARCHIVED"

    system_role = service.store.get_role(SYSTEM_ADMIN_ROLE_ID)
    assert system_role is not None
    with pytest.raises(SecurityApiError) as built_in_error:
        service.delete_role(
            system_role.role_id,
            expected_version=system_role.version,
            actor=actor,
        )
    assert built_in_error.value.code == "SECURITY_ROLE_DELETE_PROTECTED"

    assigned = service.create_role(
        role_code="DELETE_ROLE_ASSIGNED",
        display_name="割当済みロール",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    service.create_user(
        login_user_id="assigned.delete.role",
        display_name="割当ユーザー",
        role_ids=[assigned.role_id],
        temporary_password="AssignedDeletePass!123",
        actor=actor,
    )
    assigned_archived = service.archive_role(
        assigned.role_id,
        expected_version=assigned.version,
        actor=actor,
    )
    with pytest.raises(SecurityApiError) as assigned_error:
        service.delete_role(
            assigned.role_id,
            expected_version=assigned_archived.version,
            actor=actor,
        )
    assert assigned_error.value.code == "SECURITY_ROLE_DELETE_ASSIGNED"

    entitled = service.create_role(
        role_code="DELETE_ROLE_ENTITLED",
        display_name="データ権限あり",
        description="",
        permissions={"menu.query"},
        entitlements=[("HR.EMPLOYEES", "*", "SELECT")],
        actor=actor,
    )
    entitled_archived = service.archive_role(
        entitled.role_id,
        expected_version=entitled.version,
        actor=actor,
    )
    with pytest.raises(SecurityApiError) as entitlement_error:
        service.delete_role(
            entitled.role_id,
            expected_version=entitled_archived.version,
            actor=actor,
        )
    assert entitlement_error.value.code == "SECURITY_ROLE_DELETE_ENTITLEMENTS_PRESENT"

    stale = service.create_role(
        role_code="DELETE_ROLE_STALE",
        display_name="競合ロール",
        description="",
        permissions=set(),
        entitlements=[],
        actor=actor,
    )
    stale_archived = service.archive_role(
        stale.role_id,
        expected_version=stale.version,
        actor=actor,
    )
    with pytest.raises(SecurityApiError) as stale_error:
        service.delete_role(
            stale.role_id,
            expected_version=stale_archived.version - 1,
            actor=actor,
        )
    assert stale_error.value.code == "SECURITY_STATE_CONFLICT"
    assert service.store.get_role(stale.role_id) is not None


def test_system_admin_role_cannot_be_assigned_to_new_user() -> None:
    service = _service()
    actor, _, _ = _login(service)

    with pytest.raises(SecurityApiError, match="構成管理者"):
        service.create_user(
            login_user_id="extra.admin",
            display_name="追加管理者",
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            temporary_password="ExtraAdminPass!123",
            actor=actor,
        )

    assert service.store.get_user_by_login_user_id("extra.admin") is None


@pytest.mark.parametrize("login_user_id", ["system_admin", "System_Admin", "SYSTEM_ADMIN"])
def test_reserved_system_admin_login_user_id_cannot_be_created(login_user_id: str) -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )

    with pytest.raises(SecurityApiError, match="構成管理者専用"):
        service.create_user(
            login_user_id=login_user_id,
            display_name="予約名",
            role_ids=[role.role_id],
            temporary_password="ReservedPass123A",
            actor=actor,
        )


_SHORT_LOGIN_USER_IDS = ["0", "1", "2", "00", "01", "a", "b", "aa", "bb", "a1", "001"]


@pytest.mark.parametrize("login_user_id", _SHORT_LOGIN_USER_IDS)
def test_user_create_request_accepts_short_login_user_ids(login_user_id: str) -> None:
    payload = UserCreateRequest(
        login_user_id=login_user_id,
        display_name=f"user_{login_user_id}",
        role_ids=["role-user"],
        temporary_password="ShortLoginPass!789",
    )

    assert payload.login_user_id == login_user_id


@pytest.mark.parametrize("login_user_id", ["", " ", ".", "_", "-", "__", ".-_", "a" * 65])
def test_user_create_request_rejects_empty_or_punctuation_only_login_user_ids(
    login_user_id: str,
) -> None:
    with pytest.raises(ValueError):
        UserCreateRequest(
            login_user_id=login_user_id,
            display_name="短いログインユーザーID",
            role_ids=["role-user"],
            temporary_password="ShortLoginPass!789",
        )


@pytest.mark.parametrize("login_user_id", _SHORT_LOGIN_USER_IDS)
def test_short_login_user_ids_can_be_created_and_login(login_user_id: str) -> None:
    service = _service()
    actor, _, _ = _login(service)
    role = service.create_role(
        role_code="QUERY_VIEWER",
        display_name="検索閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=actor,
    )
    password = "ShortLoginPass!789"

    user, returned_password = service.create_user(
        login_user_id=login_user_id,
        display_name=f"user_{login_user_id}",
        role_ids=[role.role_id],
        temporary_password=password,
        actor=actor,
    )
    principal, _token, _csrf = service.login(login_user_id, password)

    assert user.login_user_id == login_user_id
    assert returned_password == password
    assert principal.user_uuid == user.user_uuid


def test_security_users_api_accepts_one_character_login_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_app_admin_env(monkeypatch, tmp_path)
    _configure_memory_api_auth(monkeypatch)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _current, csrf = await _login_api(client, "system_admin", "AppAdminPass123")
            role_response = await client.post(
                "/api/security/roles",
                headers={"X-CSRF-Token": csrf},
                json={
                    "role_code": "SHORT_LOGIN_USER",
                    "display_name": "短いログインユーザーID",
                },
            )
            assert role_response.status_code == 200
            role_id = role_response.json()["data"]["role_id"]

            user_response = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "1",
                    "display_name": "1文字ログイン",
                    "temporary_password": "ShortApiPass!789",
                    "role_ids": [role_id],
                },
            )

            assert user_response.status_code == 200
            assert user_response.json()["data"]["user"]["login_user_id"] == "1"

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_system_admin_role_cannot_be_added_to_non_bootstrap_user() -> None:
    service = _service()
    actor, _, _ = _login(service)
    user, _password = service.create_user(
        login_user_id="query.user",
        display_name="検索ユーザー",
        role_ids=[],
        temporary_password="QueryUserPass!123",
        actor=actor,
    )

    with pytest.raises(SecurityApiError, match="構成管理者"):
        service.update_user(
            user.user_uuid,
            expected_version=user.version,
            display_name=user.display_name,
            status="ACTIVE",
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            actor=actor,
        )


def test_legacy_non_bootstrap_system_admin_can_be_removed_but_not_reassigned() -> None:
    service = _service()
    actor, _, _ = _login(service)
    legacy_admin = service.store.create_user(
        UserRecord(
            user_uuid="legacy-admin-user",
            login_user_id="legacy.admin",
            display_name="旧管理者",
            password_hash="legacy-hash",
            status="ACTIVE",
            force_password_change=False,
            failed_login_count=0,
            locked_until=None,
            version=1,
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            is_bootstrap_admin=False,
        )
    )

    removed = service.update_user(
        legacy_admin.user_uuid,
        expected_version=legacy_admin.version,
        display_name=legacy_admin.display_name,
        status="ACTIVE",
        role_ids=[],
        actor=actor,
    )

    assert removed.role_ids == []
    with pytest.raises(SecurityApiError, match="構成管理者"):
        service.update_user(
            removed.user_uuid,
            expected_version=removed.version,
            display_name=removed.display_name,
            status="ACTIVE",
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            actor=actor,
        )


def test_login_lockout_is_generic() -> None:
    service = _service()
    for _ in range(5):
        with pytest.raises(SecurityApiError) as error:
            service.login("ADMIN", "wrong")
        assert (
            error.value.public_message == "ログインユーザーIDまたはパスワードを確認してください。"
        )
    user = service.store.get_user_by_login_user_id("admin")
    assert user is not None
    assert user.locked_until is not None
    assert user.locked_until > datetime.now(UTC)


def test_login_api_is_rate_limited_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """構成管理者のパスワードを何回でも試せない（#1087）。429 の文と Retry-After を返す。"""
    _configure_memory_api_auth(monkeypatch)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(5):
                failed = await client.post(
                    "/api/auth/login",
                    json={"login_user_id": "system_admin", "password": "WrongPass12345"},
                )
                assert failed.status_code == 401
            limited = await client.post(
                "/api/auth/login",
                json={"login_user_id": "system_admin", "password": "AppAdminPass123"},
            )
            assert limited.status_code == 429
            assert limited.json()["error_messages"] == [
                "ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。"
            ]
            assert limited.json()["error_code"] == "SECURITY_RATE_LIMITED"
            assert int(limited.headers["Retry-After"]) > 0

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_every_api_route_is_classified_by_manifest() -> None:
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        route_path = path.removeprefix("/api")
        for method in operations:
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            # Public/auth-only routes deliberately return None; all others require a code.
            permission = permission_for_route(method, route_path)
            if route_path not in {
                "/health",
                "/ready",
                "/ready/database",
                "/auth/login",
                "/auth/me",
                "/auth/logout",
                "/auth/password/change",
            }:
                assert not (permission and UNCLASSIFIED_PERMISSION in permission), (
                    f"unclassified route: {method.upper()} {path}"
                )

    # 権限なしで通す操作は method 単位で OPEN_API_OPERATIONS と一致する（#490）。
    open_operations = {
        (method.upper(), path.removeprefix("/api"))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api")
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        and permission_for_route(method, path.removeprefix("/api")) is None
    }
    assert open_operations == set(OPEN_API_OPERATIONS)
    assert permission_for_route("DELETE", "/auth/me") is not None

    assert permission_for_route("POST", "/nl2sql/execute") == frozenset({SQL_EXECUTE_PERMISSION})
    assert permission_for_route("POST", "/nl2sql/jobs") == frozenset({QUERY_GENERATE_PERMISSION})
    assert permission_for_route("POST", "/nl2sql/rewrite") == frozenset({QUERY_GENERATE_PERMISSION})
    assert permission_for_route("POST", "/nl2sql/analyze") == frozenset({SQL_EXECUTE_PERMISSION})
    assert permission_for_route("POST", "/nl2sql/query-sessions/{session_id}/execute") == frozenset(
        {SQL_EXECUTE_PERMISSION}
    )
    assert permission_for_route(
        "POST", "/nl2sql/query-sessions/{session_id}/generate-sql"
    ) == frozenset({QUERY_GENERATE_PERMISSION})
    assert permission_for_route("POST", "/nl2sql/db-admin/execute") == frozenset({"menu.admin_sql"})
    assert permission_for_route("POST", "/nl2sql/db-admin/extract-join-where") == frozenset(
        {"menu.view_management"}
    )
    assert permission_for_route("POST", "/nl2sql/db-admin/statements") == frozenset(
        {
            "menu.admin_sql",
            "menu.table_management",
            "menu.view_management",
            "menu.data_management",
            "menu.comment_management",
            "menu.annotation_management",
            "menu.domain_management",
        }
    )
    assert permission_for_route("GET", "/nl2sql/db-admin/tables/{table_name}") == frozenset(
        {
            "menu.table_management",
            "menu.comment_management",
            "menu.annotation_management",
            "menu.domain_management",
        }
    )
    assert permission_for_route("GET", "/nl2sql/db-admin/views/{view_name}") == frozenset(
        {
            "menu.view_management",
            "menu.comment_management",
            "menu.annotation_management",
            "menu.domain_management",
        }
    )
    assert permission_for_route("POST", "/nl2sql/domains/generate-sql") == frozenset(
        {"menu.domain_management"}
    )
    assert permission_for_route("GET", "/nl2sql/profiles/search") == frozenset(
        {PROFILE_READ_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/profiles/{profile_id}/usage-context") == frozenset(
        {PROFILE_READ_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/profiles/{profile_id}/ontology-view") == frozenset(
        {PROFILE_MANAGE_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/profiles/{profile_id}") == frozenset(
        {PROFILE_MANAGE_PERMISSION}
    )
    assert permission_for_route("POST", "/schema/refresh-jobs") == frozenset(
        {SCHEMA_REFRESH_PERMISSION}
    )
    assert permission_for_route("GET", "/schema/refresh-jobs/active") == frozenset(
        {SCHEMA_READ_PERMISSION}
    )
    assert permission_for_route("GET", "/schema/objects") == frozenset({SCHEMA_READ_PERMISSION})
    assert permission_for_route("GET", "/nl2sql/feedback") == frozenset(
        {FEEDBACK_MANAGE_PERMISSION}
    )
    assert permission_for_route("POST", "/nl2sql/feedback") == frozenset(
        {FEEDBACK_WRITE_PERMISSION, FEEDBACK_MANAGE_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/select-ai/db-profiles") == frozenset(
        {SELECT_AI_ASSETS_READ_PERMISSION}
    )
    assert permission_for_route("POST", "/nl2sql/select-ai/db-profiles/refresh-jobs") == frozenset(
        {SELECT_AI_ASSETS_REFRESH_PERMISSION}
    )
    assert permission_for_route("POST", "/nl2sql/select-ai/db-profiles") == frozenset(
        {SELECT_AI_ASSETS_MANAGE_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/legacy-learning-material") == frozenset(
        {LEARNING_MATERIAL_MANAGE_PERMISSION, "menu.glossary_rules", "menu.global_rules"}
    )
    assert permission_for_route("POST", "/nl2sql/sample-data/import") == frozenset(
        {SAMPLE_DATA_MANAGE_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/diagnostics") == frozenset(
        {SYSTEM_STATUS_READ_PERMISSION}
    )
    assert permission_for_route("GET", "/nl2sql/persistence") is None
    assert permission_for_route("POST", "/nl2sql/persistence/recover") == frozenset(
        {PERSISTENCE_RECOVER_PERMISSION}
    )
    role_readers = {"menu.security_users", "menu.security_roles", "menu.security_permissions"}
    assert permission_for_route("GET", "/security/roles") == frozenset(role_readers)
    assert permission_for_route("GET", "/security/roles/{role_id}") == frozenset(role_readers)
    assert permission_for_route("POST", "/security/roles") == frozenset({"menu.security_roles"})
    assert permission_for_route("PATCH", "/security/roles/{role_id}") == frozenset(
        {"menu.security_roles"}
    )
    assert permission_for_route("POST", "/security/roles/{role_id}/restore") == frozenset(
        {"menu.security_roles"}
    )
    # 権限の付与はロール管理から分けた権限管理だけが行う（#206）。
    assert permission_for_route("PUT", "/security/roles/{role_id}/permissions") == frozenset(
        {"menu.security_permissions"}
    )
    assert permission_for_route("GET", "/security/permissions") == frozenset(
        {"menu.security_permissions"}
    )
    assert permission_for_route("GET", "/security/profile-access/profiles") == frozenset(
        {"menu.security_permissions"}
    )
    assert permission_for_route("GET", "/security/deepsec/target-objects") == frozenset(
        {"menu.security_deepsec"}
    )
    assert permission_for_route(
        "GET", "/security/deepsec/target-objects/{owner}/{object_name}"
    ) == frozenset({"menu.security_deepsec"})
    assert permission_for_route("POST", "/security/deepsec/config/sync-password") == frozenset(
        {"menu.security_deepsec"}
    )


@pytest.mark.parametrize(
    ("menu", "own_kind", "other_kind"),
    [("menu.glossary_rules", "terms", "rules"), ("menu.global_rules", "rules", "terms")],
    ids=["glossary", "global-rules"],
)
def test_glossary_and_global_rules_menus_only_open_their_own_material(
    monkeypatch: pytest.MonkeyPatch, menu: str, own_kind: str, other_kind: str
) -> None:
    """用語・同義語 / 共通ルールの権限は、業務プロファイル管理ともう一方の取込を含まない。

    #1006。
    """
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    role = service.create_role(
        role_code="LEARNING_MENU_ONLY",
        display_name="学習素材のメニューだけ",
        description="",
        permissions={menu},
        entitlements=[],
        actor=admin,
    )
    assert not grants_all_profile_access(role.permissions)
    user = _create_active_user(
        service,
        admin,
        login_user_id="learning.menu",
        display_name="学習素材の担当",
        role_ids=[role.role_id],
        password="LearningMenu!8642",
    )
    principal = service.principal_for_worker(user.user_uuid)
    assert PROFILE_MANAGE_PERMISSION not in principal.permissions
    assert LEARNING_MATERIAL_MANAGE_PERMISSION not in principal.permissions
    assert not principal.can_use_profile("default")

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "learning.menu", "LearningMenu!8642")
            material = await client.get("/api/nl2sql/legacy-learning-material")
            assert material.status_code == 200, material.text
            own = await client.get(f"/api/nl2sql/legacy-learning-material/{own_kind}/export.xlsx")
            assert own.status_code == 200, own.text
            other = await client.get(
                f"/api/nl2sql/legacy-learning-material/{other_kind}/export.xlsx"
            )
            assert other.status_code == 403
            other_import = await client.post(
                f"/api/nl2sql/legacy-learning-material/{other_kind}/import",
                headers={"X-CSRF-Token": csrf},
                files={"file": ("material.xlsx", b"x", "application/octet-stream")},
            )
            assert other_import.status_code == 403
            assert (await client.get("/api/nl2sql/profiles")).status_code == 403
            assert (
                await client.post(
                    "/api/nl2sql/profiles",
                    headers={"X-CSRF-Token": csrf},
                    json={"name": "作れない"},
                )
            ).status_code == 403

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


@pytest.mark.parametrize(
    ("permissions", "expected_status"),
    [({"menu.security_deepsec"}, 200), ({"menu.security_permissions"}, 403)],
    ids=["deepsec", "other-menu"],
)
def test_deepsec_target_object_detail_route_uses_registered_permission(
    monkeypatch: pytest.MonkeyPatch,
    permissions: set[str],
    expected_status: int,
) -> None:
    """`{object_name:path}` の route も、登録した権限で判定する（常に 403 にしない）。"""
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    role = service.create_role(
        role_code="DEEPSEC_DETAIL_READER",
        display_name="DeepSec の対象の詳細",
        description="",
        permissions=permissions,
        entitlements=[],
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="deepsec.detail",
        display_name="DeepSec 担当",
        role_ids=[role.role_id],
        password="DeepSecDetail!8642",
    )
    calls: list[tuple[str, str, str]] = []

    class _DeepSecStub:
        def target_object_detail(
            self, *, owner: str, object_name: str, object_type: str
        ) -> dict[str, object]:
            calls.append((owner, object_name, object_type))
            return {"name": object_name, "owner": owner, "object_type": "TABLE", "columns": []}

    monkeypatch.setattr("app.security.router.get_deepsec_service", lambda: _DeepSecStub())

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _login_api(client, "deepsec.detail", "DeepSecDetail!8642")
            response = await client.get(
                "/api/security/deepsec/target-objects/HR/EMPLOYEES",
                params={"object_type": "TABLE"},
            )
            assert response.status_code == expected_status, response.text
            if expected_status == 200:
                assert response.json()["data"]["qualified_name"] == "HR.EMPLOYEES"
                # "/" を含む表名も 1 つの object_name として受ける（`:path`）。
                slashed = await client.get(
                    "/api/security/deepsec/target-objects/HR/%22A%2FB%22",
                )
                assert slashed.status_code == 200, slashed.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    if expected_status == 200:
        assert calls == [("HR", "EMPLOYEES", "TABLE"), ("HR", '"A/B"', "")]
    else:
        assert calls == []


def test_security_audit_permission_and_api_are_removed() -> None:
    catalog_codes = {item.code for item in PERMISSION_CATALOG}

    assert "menu.security_audit" not in catalog_codes
    assert "security.audit.view" not in ALL_PERMISSION_CODES
    assert permission_for_route("GET", "/security/audit/page") == frozenset(
        {UNCLASSIFIED_PERMISSION}
    )
    assert not any(path.startswith("/api/security/audit") for path in app.openapi()["paths"])


_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_USER_ROLE_PREFIXES = ("/security/users", "/security/roles")


def _mounted_user_role_operations() -> set[tuple[str, str]]:
    return {
        (method.upper(), path.removeprefix("/api"))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api") and path.removeprefix("/api").startswith(_USER_ROLE_PREFIXES)
        for method in operations
        if method.upper() in _HTTP_METHODS
    }


def _legacy_user_role_permission(method: str, route_path: str) -> frozenset[str]:
    """#503 より前の前方一致による割り当て（同じ権限のままかを比べるために残す）。"""

    if route_path.startswith("/security/users"):
        return frozenset({"menu.security_users"})
    if route_path == "/security/roles/{role_id}/permissions":
        return frozenset({"menu.security_permissions"})
    if method == "GET":
        return frozenset(
            {"menu.security_users", "menu.security_roles", "menu.security_permissions"}
        )
    return frozenset({"menu.security_roles"})


def test_user_role_routes_are_registered_per_operation() -> None:
    """ユーザー・ロール管理の API は (method, path) ごとに登録し、実在の route と一致する。

    #503 で前方一致をやめた。
    """

    mounted = _mounted_user_role_operations()
    assert len(mounted) == 17
    assert set(SECURITY_USER_ROLE_ROUTE_PERMISSIONS) == mounted
    for permissions in SECURITY_USER_ROLE_ROUTE_PERMISSIONS.values():
        assert permissions
        assert permissions <= ALL_PERMISSION_CODES


def test_user_role_route_permissions_match_legacy_prefix_rules() -> None:
    """明示の登録にしても、ユーザー・ロール管理の API の権限は前方一致のときと同じ（#503）。"""

    for method, route_path in sorted(_mounted_user_role_operations()):
        assert permission_for_route(method, route_path) == _legacy_user_role_permission(
            method, route_path
        ), f"{method} {route_path}"
    assert permission_for_route("DELETE", "/security/users/{user_uuid}") == frozenset(
        {"menu.security_users"}
    )
    assert permission_for_route("POST", "/security/users/{user_uuid}/reset-password") == (
        frozenset({"menu.security_users"})
    )
    assert permission_for_route("POST", "/security/roles/{role_id}/archive") == frozenset(
        {"menu.security_roles"}
    )
    assert permission_for_route("DELETE", "/security/roles/{role_id}") == frozenset(
        {"menu.security_roles"}
    )


def test_unregistered_user_role_routes_are_denied() -> None:
    """前方一致をやめたので、登録のない method・path は既定で拒否する（#503）。"""

    unclassified = frozenset({UNCLASSIFIED_PERMISSION})
    assert permission_for_route("POST", "/security/users/{user_uuid}/impersonate") == unclassified
    assert permission_for_route("PUT", "/security/users/{user_uuid}") == unclassified
    assert permission_for_route("GET", "/security/users-export") == unclassified
    assert permission_for_route("POST", "/security/roles/{role_id}/clone") == unclassified
    assert permission_for_route("GET", "/security/roles/{role_id}/permissions") == unclassified


def test_route_manifest_check_rejects_unregistered_user_route() -> None:
    """`/security/users` 配下に未登録の route を足すと、起動時の manifest の検査で失敗する。

    #503 の前は前方一致で通っていた。
    """

    application = FastAPI()

    @application.post("/api/security/users/{user_uuid}/impersonate")
    def _impersonate(user_uuid: str) -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {"user_uuid": user_uuid}

    @application.get("/api/security/users")
    def _list_users() -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {}

    with pytest.raises(RuntimeError) as error:
        _assert_route_manifest(application)
    missing = str(error.value).split(": ", 1)[1].split(", ")
    assert missing == ["POST /api/security/users/{user_uuid}/impersonate"]


def _mounted_api_operations() -> set[tuple[str, str]]:
    return {
        (method.upper(), path.removeprefix("/api"))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api")
        for method in operations
        if method.upper() in _HTTP_METHODS
    }


def test_route_permissions_cover_every_mounted_route() -> None:
    """mount された API は OPEN_API_OPERATIONS か ROUTE_PERMISSIONS の一方だけに載る（#510）。

    前方一致をやめたので、manifest に登録のない route は拒否になる。ここで登録と実在の
    route の差（登録漏れ・消えた route の登録の残り）を見つける。
    """

    mounted = _mounted_api_operations()
    # SQL チャットの一覧・会話取得も、生成権限で保護する（#871）。チャットのターンの SQL の実行
    # （#1154）と、ジョブの中で実行した結果の受け取り（#1176）は実行の権限で保護する。ジョブの
    # 処理の段階のイベント（polling・SSE。#1359）はジョブの取得と同じ権限。生成の方式の選択肢
    # （#1524）は業務プロファイルの管理と SQL の生成の権限。業務プロファイルの保存（Oracle 反映）の
    # 事前条件の Select AI の Credential の状態（#1683 / #1688）は、Credential が前提の操作の
    # いずれかの権限で読める。会話の名前の変更・削除（#1817）は生成の権限で本人の会話だけ。
    # 実行履歴の 1 行（全幅の詳細。#1955）は一覧と同じ権限。DB 管理チャット（#2264）の生成・実行・
    # 失敗の分析と会話の履歴（#2319）は menu.db_admin_chat だけ。回答の注意書きの取得・保存
    # （#2405）は menu.settings_answer_notice。チャットの添付（#2517）は生成の権限、使うかの設定は
    # menu.settings_chat_attachments。軌跡の本文記録は menu.settings_trace_recording。
    assert len(mounted) == 304
    assert len(ROUTE_PERMISSIONS) == 292
    assert ROUTE_PERMISSIONS[("GET", "/nl2sql/select-ai/credential-readiness")] == frozenset(
        {
            "nl2sql.profiles.manage",
            "nl2sql.select_ai_assets.refresh",
            "nl2sql.select_ai_assets.manage",
            QUERY_GENERATE_PERMISSION,
            "menu.sample_data",
            "menu.data_management",
            "menu.evaluation",
        }
    )
    assert ROUTE_PERMISSIONS[("GET", "/nl2sql/engines")] == frozenset(
        {PROFILE_MANAGE_PERMISSION, QUERY_GENERATE_PERMISSION}
    )
    for path in ("/nl2sql/chats", "/nl2sql/chats/{conversation_id}"):
        assert ROUTE_PERMISSIONS[("GET", path)] == frozenset({QUERY_GENERATE_PERMISSION})
    # 処理の経過（#3226 の案 A）は記録と同じ権限。
    for path, record in (
        ("/nl2sql/jobs/{job_id}/trace", "/nl2sql/jobs/{job_id}"),
        ("/nl2sql/history/{history_id}/trace", "/nl2sql/history/{history_id}"),
        (
            "/nl2sql/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/trace",
            "/nl2sql/db-admin-chat/conversations/{conversation_id}",
        ),
    ):
        assert ROUTE_PERMISSIONS[("GET", path)] == ROUTE_PERMISSIONS[("GET", record)]
    assert set(ROUTE_PERMISSIONS).isdisjoint(OPEN_API_OPERATIONS)
    assert set(ROUTE_PERMISSIONS) | set(OPEN_API_OPERATIONS) == mounted
    for key, permissions in ROUTE_PERMISSIONS.items():
        assert permissions, key
        assert permissions <= ALL_PERMISSION_CODES, key


# #510 の前（前方一致）の判定結果。前方一致の分岐ごとに代表の route を 1 つ以上置き、
# method で分けていた分岐は両方の method を置く。明示の登録にしても同じ権限であることを確かめる。
_LEGACY_PREFIX_RULE_RESULTS: tuple[tuple[str, str, set[str]], ...] = (
    ("GET", "/security/permissions", {"menu.security_permissions"}),
    ("GET", "/security/profile-access/profiles", {"menu.security_permissions"}),
    ("PATCH", "/security/deepsec/config", {"menu.security_deepsec"}),
    (
        "PATCH",
        "/settings/oci/object-storage",
        {"menu.settings_oci", "menu.settings_upload_storage"},
    ),
    ("POST", "/settings/oci/key-file", {"menu.settings_oci"}),
    ("GET", "/settings/upload-storage", {"menu.settings_oci", "menu.settings_upload_storage"}),
    ("PATCH", "/settings/upload-storage", {"menu.settings_upload_storage"}),
    ("POST", "/settings/upload-storage/test", {"menu.settings_upload_storage"}),
    ("POST", "/settings/model/test", {"menu.settings_model"}),
    ("POST", "/settings/database/system-tables/initialize", {"menu.settings_system_tables"}),
    ("POST", "/settings/database/wallet", {"menu.settings_database"}),
    ("POST", "/schema/refresh-jobs", {"nl2sql.schema.refresh"}),
    ("GET", "/schema/refresh-jobs/{job_id}", {"nl2sql.schema.read"}),
    ("GET", "/schema/owners", {"nl2sql.schema.read"}),
    (
        "GET",
        "/nl2sql/db-admin/tables/{table_name}/export.xlsx",
        {
            "menu.annotation_management",
            "menu.comment_management",
            "menu.domain_management",
            "menu.table_management",
        },
    ),
    (
        "GET",
        "/nl2sql/db-admin/views",
        {
            "menu.annotation_management",
            "menu.comment_management",
            "menu.domain_management",
            "menu.view_management",
        },
    ),
    ("POST", "/nl2sql/db-admin/truncate-table", {"menu.data_management", "menu.table_management"}),
    ("POST", "/nl2sql/db-admin/drop-table", {"menu.table_management"}),
    ("POST", "/nl2sql/db-admin/drop-view", {"menu.view_management"}),
    ("POST", "/nl2sql/db-admin/import-tabular", {"menu.data_management", "menu.table_management"}),
    ("POST", "/nl2sql/db-admin/upload-csv", {"menu.data_management"}),
    (
        "POST",
        "/nl2sql/db-admin/preview-data/export.xlsx",
        {
            "menu.annotation_management",
            "menu.comment_management",
            "menu.data_management",
            "menu.domain_management",
            "menu.table_management",
            "menu.view_management",
        },
    ),
    ("POST", "/nl2sql/db-admin/execute", {"menu.admin_sql"}),
    (
        "POST",
        "/nl2sql/db-admin/statements",
        {
            "menu.admin_sql",
            "menu.annotation_management",
            "menu.comment_management",
            "menu.data_management",
            "menu.domain_management",
            "menu.table_management",
            "menu.view_management",
        },
    ),
    (
        "GET",
        "/nl2sql/db-admin/objects",
        {
            "menu.admin_sql",
            "menu.annotation_management",
            "menu.comment_management",
            "menu.data_management",
            "menu.domain_management",
            "menu.global_rules",
            "menu.glossary_rules",
            "menu.sample_data",
            "menu.table_management",
            "menu.view_management",
        },
    ),
    ("POST", "/nl2sql/db-admin/analyze-error", {"menu.admin_sql"}),
    ("POST", "/nl2sql/sample-data/delete", {"nl2sql.sample_data.manage"}),
    ("POST", "/nl2sql/comments/suggest", {"menu.comment_management"}),
    ("POST", "/nl2sql/annotations/apply", {"menu.annotation_management"}),
    ("POST", "/nl2sql/domains/inventory", {"menu.domain_management"}),
    (
        "POST",
        "/nl2sql/metadata-samples",
        {"menu.annotation_management", "menu.comment_management", "menu.domain_management"},
    ),
    (
        "POST",
        "/nl2sql/synthetic-data/runs/{run_id}/apply",
        {"menu.data_management", "menu.sample_data"},
    ),
    ("GET", "/nl2sql/profiles/search", {"nl2sql.profiles.read"}),
    ("GET", "/nl2sql/profiles/{profile_id}/usage-context", {"nl2sql.profiles.read"}),
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/learning-material/import",
        {"nl2sql.learning_material.manage", "nl2sql.profiles.manage"},
    ),
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities",
        {
            "menu.ontology_build",
            "nl2sql.ontology.actions.execute",
            "nl2sql.profiles.read",
            "nl2sql.sql.execute",
        },
    ),
    (
        "PATCH",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/binding",
        {"nl2sql.ontology.capabilities.manage"},
    ),
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/invoke",
        {"nl2sql.sql.execute"},
    ),
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/preview",
        {"nl2sql.ontology.actions.execute"},
    ),
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/workspace",
        {"menu.ontology_build", "nl2sql.profiles.read", "nl2sql.query.generate"},
    ),
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-releases/{release_id}/rollback",
        {"menu.ontology_build", "nl2sql.profiles.manage"},
    ),
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-validation-jobs/{job_id}",
        {"menu.ontology_build", "nl2sql.profiles.read", "nl2sql.query.generate"},
    ),
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-proposals", {"nl2sql.profiles.manage"}),
    ("DELETE", "/nl2sql/profiles/{profile_id}", {"nl2sql.profiles.manage"}),
    # 共通ルールの画面の権限でも取り込める（#1006 で追加。用語・同義語の権限では取り込めない）。
    (
        "POST",
        "/nl2sql/legacy-learning-material/rules/import",
        {"nl2sql.learning_material.manage", "menu.global_rules"},
    ),
    ("POST", "/nl2sql/ontology/revisions/{revision_id}/publish", {"menu.ontology_build"}),
    ("GET", "/nl2sql/ontology-build/{job_id}", {"menu.ontology_build"}),
    (
        "POST",
        "/nl2sql/oracle-sync-jobs/{job_id}/retry",
        {"nl2sql.profiles.manage", "nl2sql.select_ai_assets.refresh"},
    ),
    ("GET", "/nl2sql/select-ai/db-profile-refresh-jobs/{job_id}", {"nl2sql.select_ai_assets.read"}),
    ("POST", "/nl2sql/select-ai/db-profiles/refresh-jobs", {"nl2sql.select_ai_assets.refresh"}),
    ("PATCH", "/nl2sql/select-ai/db-profiles/{profile_name}", {"nl2sql.select_ai_assets.manage"}),
    ("GET", "/nl2sql/select-ai/db-profiles/{profile_name}", {"nl2sql.select_ai_assets.read"}),
    (
        "POST",
        "/nl2sql/select-ai/feedback/vector-index",
        {"nl2sql.feedback.manage", "nl2sql.select_ai_assets.manage"},
    ),
    ("POST", "/nl2sql/select-ai/profiles/refresh", {"nl2sql.select_ai_assets.refresh"}),
    ("GET", "/nl2sql/select-ai/profiles/export.json", {"nl2sql.select_ai_assets.manage"}),
    ("POST", "/nl2sql/select-ai/profiles/import-json", {"nl2sql.select_ai_assets.manage"}),
    ("POST", "/nl2sql/select-ai/assets/cleanup", {"nl2sql.select_ai_assets.manage"}),
    ("POST", "/nl2sql/select-ai-agent/assets/refresh", {"nl2sql.select_ai_assets.refresh"}),
    ("POST", "/nl2sql/select-ai-agent/assets/cleanup", {"nl2sql.select_ai_assets.manage"}),
    ("POST", "/nl2sql/select-ai-agent/run-team", {"nl2sql.select_ai_assets.manage"}),
    ("POST", "/nl2sql/select-ai-agent/conversations/create", {"nl2sql.select_ai_assets.manage"}),
    ("GET", "/nl2sql/select-ai-agent/privileges/check", {"nl2sql.select_ai_assets.read"}),
    ("POST", "/nl2sql/feedback/admin-review", {"nl2sql.feedback.manage"}),
    ("POST", "/nl2sql/feedback-index/rebuild", {"nl2sql.feedback.manage"}),
    ("PATCH", "/nl2sql/feedback-config", {"nl2sql.feedback.manage"}),
    ("GET", "/nl2sql/feedback", {"nl2sql.feedback.manage"}),
    ("POST", "/nl2sql/feedback", {"nl2sql.feedback.manage", "nl2sql.feedback.write"}),
    (
        "DELETE",
        "/nl2sql/feedback/{history_id}",
        {"nl2sql.feedback.manage", "nl2sql.feedback.write"},
    ),
    (
        "DELETE",
        "/nl2sql/classifier/training-data/{example_id}",
        {"menu.question_classifier_models"},
    ),
    ("GET", "/nl2sql/quality-evaluations/{job_id}/results.xlsx", {"menu.evaluation"}),
    ("POST", "/nl2sql/reverse/deep", {"menu.sql_to_question"}),
    ("GET", "/nl2sql/history", {"menu.history", "nl2sql.feedback.manage", "nl2sql.query.generate"}),
    ("POST", "/nl2sql/preview", {"nl2sql.query.generate"}),
    ("POST", "/nl2sql/analyze", {"nl2sql.sql.execute"}),
    ("POST", "/nl2sql/jobs", {"nl2sql.query.generate"}),
    (
        "POST",
        "/nl2sql/jobs/{job_id}/cancel",
        {"menu.history", "nl2sql.feedback.manage", "nl2sql.query.generate"},
    ),
    ("POST", "/nl2sql/query-sessions/{session_id}/execute", {"nl2sql.sql.execute"}),
    ("PATCH", "/nl2sql/query-sessions/{session_id}/intent", {"nl2sql.query.generate"}),
    ("POST", "/nl2sql/similar-history", {"nl2sql.query.generate"}),
    ("POST", "/nl2sql/recommend-profile", {"nl2sql.query.generate"}),
    (
        "POST",
        "/nl2sql/demo/learning",
        {"nl2sql.feedback.manage", "nl2sql.learning_material.manage"},
    ),
    ("POST", "/nl2sql/persistence/recover", {"nl2sql.persistence.recover"}),
    ("GET", "/nl2sql/diagnostics", {"nl2sql.system_status.read"}),
)


@pytest.mark.parametrize(
    ("method", "route_path", "expected"),
    _LEGACY_PREFIX_RULE_RESULTS,
    ids=[f"{method} {path}" for method, path, _ in _LEGACY_PREFIX_RULE_RESULTS],
)
def test_route_permissions_match_legacy_prefix_rules(
    method: str, route_path: str, expected: set[str]
) -> None:
    """前方一致をやめても、登録済みの API の権限は変わらない（#510）。"""

    assert (method, route_path) in ROUTE_PERMISSIONS
    assert permission_for_route(method, route_path) == frozenset(expected)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_select_ai_credential_uses_its_own_menu_permission(method: str) -> None:
    """Select AI Credential は運用設定の専用の画面の権限で操作する（#658）。"""

    assert permission_for_route(method, "/settings/database/select-ai-credential") == frozenset(
        {"menu.settings_select_ai_credential"}
    )
    catalog = {item.code: item for item in PERMISSION_CATALOG}
    assert catalog["menu.settings_select_ai_credential"].group == "運用設定"
    assert catalog["menu.security_permissions"].group == "セキュリティ設定"


@pytest.mark.parametrize(
    ("method", "route_path"),
    [
        # いずれも #510 の前は前方一致で既存の権限に割り当てられていた。
        ("GET", "/nl2sql/new-feature"),
        ("POST", "/nl2sql/new-feature/export"),
        ("POST", "/nl2sql/db-admin/new-operation"),
        ("GET", "/nl2sql/profiles/{profile_id}/new-view"),
        ("DELETE", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}"),
        ("PUT", "/nl2sql/select-ai/db-profiles/{profile_name}"),
        ("POST", "/nl2sql/persistence/new-operation"),
        ("GET", "/schema/new-listing"),
        ("POST", "/settings/oci/new-operation"),
        ("DELETE", "/settings/database"),
        ("GET", "/security/deepsec/new-listing"),
        ("POST", "/security/profile-access/profiles"),
    ],
)
def test_unregistered_routes_are_denied(method: str, route_path: str) -> None:
    """登録のない method・path は、前方一致の範囲の下でも既定で拒否する（#510）。"""

    assert permission_for_route(method, route_path) == frozenset({UNCLASSIFIED_PERMISSION})


def test_route_manifest_check_rejects_unregistered_routes_under_former_prefixes() -> None:
    """前方一致の範囲に未登録の route を足すと、起動時の manifest の検査で失敗する（#510）。"""

    application = FastAPI()

    @application.post("/api/nl2sql/profiles/{profile_id}/new-operation")
    def _new_profile_operation(profile_id: str) -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {"profile_id": profile_id}

    @application.get("/api/settings/model/new-listing")
    def _new_model_listing() -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {}

    @application.get("/api/nl2sql/profiles/search")
    def _search_profiles() -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {}

    @application.get("/api/nl2sql/persistence")
    def _persistence() -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {}

    with pytest.raises(RuntimeError) as error:
        _assert_route_manifest(application)
    missing = str(error.value).split(": ", 1)[1].split(", ")
    assert missing == [
        "GET /api/settings/model/new-listing",
        "POST /api/nl2sql/profiles/{profile_id}/new-operation",
    ]


def test_schema_refresh_active_static_route_precedes_job_id_route() -> None:
    from app.features.schema.router import router as schema_router

    schema_paths = [
        path
        for route in schema_router.routes
        if (path := getattr(route, "path", "")).startswith("/schema/refresh-jobs/")
    ]

    assert schema_paths.index("/schema/refresh-jobs/active") < schema_paths.index(
        "/schema/refresh-jobs/{job_id}"
    )


def test_sql_use_roles_can_read_profile_usage_context_without_profile_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    admin_user = service.store.get_user(admin.user_uuid)
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )
    query_role = service.create_role(
        role_code="QUERY_ONLY",
        display_name="SQL 生成のみ",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=admin,
    )
    reverse_role = service.create_role(
        role_code="SQL_TO_QUESTION_ONLY",
        display_name="SQL から質問生成のみ",
        description="",
        permissions={"menu.sql_to_question"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=admin,
    )
    profile_manager_role = service.create_role(
        role_code="PROFILE_MANAGER",
        display_name="業務プロファイル管理",
        description="",
        permissions={"menu.profiles"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="query.only",
        display_name="SQL 生成のみ",
        role_ids=[query_role.role_id],
        password="QueryOnlyPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="reverse.only",
        display_name="SQL から質問生成のみ",
        role_ids=[reverse_role.role_id],
        password="ReverseOnlyPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="profile.manager",
        display_name="業務プロファイル管理",
        role_ids=[profile_manager_role.role_id],
        password="ProfileManagePass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            current, csrf = await _login_api(client, "query.only", "QueryOnlyPass!123")
            assert current["role_codes"] == ["QUERY_ONLY"]
            assert set(current["permissions"]) == {  # type: ignore[call-overload]
                "menu.query",
                FEEDBACK_WRITE_PERMISSION,
                PROFILE_READ_PERMISSION,
                QUERY_GENERATE_PERMISSION,
                SCHEMA_READ_PERMISSION,
                SQL_EXECUTE_PERMISSION,
            }

            profile_search = await client.get("/api/nl2sql/profiles/search")
            assert profile_search.status_code == 200
            usage_context = await client.get("/api/nl2sql/profiles/default/usage-context")
            assert usage_context.status_code == 200
            usage_payload = usage_context.json()["data"]
            assert usage_payload["id"] == "default"
            assert "few_shot_examples" not in usage_payload
            assert "select_ai_config" not in usage_payload
            assert (await client.get("/api/schema/objects")).status_code == 200
            active_schema_refresh = await client.get("/api/schema/refresh-jobs/active")
            assert active_schema_refresh.status_code == 200
            assert "active_job" in active_schema_refresh.json()["data"]
            assert (await client.get("/api/nl2sql/profiles/default")).status_code == 403
            ontology_view = await client.get("/api/nl2sql/profiles/default/ontology-view")
            assert ontology_view.status_code == 403
            denied_profile_create = await client.post(
                "/api/nl2sql/profiles",
                headers={"X-CSRF-Token": csrf},
                json={"name": "NO_ACCESS"},
            )
            assert denied_profile_create.status_code == 403
            denied_schema_refresh = await client.post(
                "/api/schema/refresh-jobs",
                headers={"X-CSRF-Token": csrf},
            )
            assert denied_schema_refresh.status_code == 403

            _, csrf = await _login_api(client, "reverse.only", "ReverseOnlyPass!123")
            reverse_current = await client.get("/api/auth/me")
            assert reverse_current.status_code == 200
            assert set(reverse_current.json()["data"]["permissions"]) == {
                "menu.sql_to_question",
                PROFILE_READ_PERMISSION,
                SCHEMA_READ_PERMISSION,
            }
            reverse_response = await client.post(
                "/api/nl2sql/reverse/deep",
                headers={"X-CSRF-Token": csrf},
                json={"sql": "SELECT EMPLOYEE_NAME FROM EMPLOYEE", "profile_id": "default"},
            )
            assert reverse_response.status_code == 200
            assert (await client.get("/api/nl2sql/profiles/default")).status_code == 403

            await _login_api(client, "profile.manager", "ProfileManagePass!123")
            full_profile = await client.get("/api/nl2sql/profiles/default")
            assert full_profile.status_code == 200
            full_payload = full_profile.json()["data"]
            assert "few_shot_examples" in full_payload
            assert "select_ai_config" in full_payload

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_nl2sql_profiles_are_filtered_and_profile_managers_use_all_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    admin_user = service.store.get_user(admin.user_uuid)
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )
    query_role = service.create_role(
        role_code="QUERY_DEFAULT_PROFILE",
        display_name="標準 profile SQL 生成",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=admin,
    )
    profile_manager_role = service.create_role(
        role_code="PROFILE_DEFAULT_MANAGER",
        display_name="標準 profile 管理",
        description="",
        permissions={"menu.profiles"},
        entitlements=[],
        actor=admin,
    )
    blocked_role = service.create_role(
        role_code="QUERY_NO_PROFILE",
        display_name="profile 未付与 SQL 生成",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="query.default.profile",
        display_name="標準 profile SQL 生成",
        role_ids=[query_role.role_id],
        password="QueryDefaultPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="profile.default.manager",
        display_name="標準 profile 管理",
        role_ids=[profile_manager_role.role_id],
        password="ProfileDefaultPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="query.no.profile",
        display_name="profile 未付与 SQL 生成",
        role_ids=[blocked_role.role_id],
        password="QueryNoProfilePass!123",
    )

    feature_service = Nl2SqlService(store=MemoryNl2SqlStore())
    feature_service.create_profile(
        Nl2SqlProfile(
            id="finance",
            name="財務プロファイル",
            category="会計",
            description="財務部門向け",
        )
    )
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", feature_service)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            current, csrf = await _login_api(
                client,
                "query.default.profile",
                "QueryDefaultPass!123",
            )
            assert current["allowed_profile_ids"] == ["default"]
            profile_search = await client.get("/api/nl2sql/profiles/search")
            assert profile_search.status_code == 200
            assert [item["id"] for item in profile_search.json()["data"]["items"]] == ["default"]
            allowed_etag = profile_search.headers["ETag"]
            default_usage = await client.get("/api/nl2sql/profiles/default/usage-context")
            assert default_usage.status_code == 200
            finance_usage = await client.get("/api/nl2sql/profiles/finance/usage-context")
            assert finance_usage.status_code == 403
            preview = await client.post(
                "/api/nl2sql/preview",
                headers={"X-CSRF-Token": csrf},
                json={"question": "社員一覧", "profile_id": "default"},
            )
            assert preview.status_code != 403
            denied_preview = await client.post(
                "/api/nl2sql/preview",
                headers={"X-CSRF-Token": csrf},
                json={"question": "売上一覧", "profile_id": "finance"},
            )
            assert denied_preview.status_code == 403
            denied_rewrite = await client.post(
                "/api/nl2sql/rewrite",
                headers={"X-CSRF-Token": csrf},
                json={"question": "売上一覧", "profile_id": "finance"},
            )
            assert denied_rewrite.status_code == 403
            denied_evaluation = await client.get(
                "/api/nl2sql/quality-evaluations/capabilities?profile_id=finance"
            )
            assert denied_evaluation.status_code == 403

            manager_current, _ = await _login_api(
                client, "profile.default.manager", "ProfileDefaultPass!123"
            )
            assert manager_current["allowed_profile_ids"] == []
            manager_search = await client.get("/api/nl2sql/profiles/search")
            assert manager_search.status_code == 200
            assert {item["id"] for item in manager_search.json()["data"]["items"]} >= {
                "default",
                "finance",
            }
            assert (await client.get("/api/nl2sql/profiles/default")).status_code == 200
            assert (await client.get("/api/nl2sql/profiles/finance")).status_code == 200

            _, csrf = await _login_api(client, "query.no.profile", "QueryNoProfilePass!123")
            empty_search = await client.get("/api/nl2sql/profiles/search")
            assert empty_search.status_code == 200
            assert empty_search.json()["data"]["items"] == []
            assert empty_search.headers["ETag"] != allowed_etag
            blocked_default_usage = await client.get("/api/nl2sql/profiles/default/usage-context")
            assert blocked_default_usage.status_code == 403
            denied_default_preview = await client.post(
                "/api/nl2sql/preview",
                headers={"X-CSRF-Token": csrf},
                json={"question": "社員一覧"},
            )
            assert denied_default_preview.status_code == 403

            await _login_api(client, "ADMIN", "BootstrapPass!123")
            admin_search = await client.get("/api/nl2sql/profiles/search")
            assert admin_search.status_code == 200
            assert {item["id"] for item in admin_search.json()["data"]["items"]} >= {
                "default",
                "finance",
            }
            admin_finance_usage = await client.get("/api/nl2sql/profiles/finance/usage-context")
            assert admin_finance_usage.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_nl2sql_capability_boundaries_and_feedback_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    query_role = service.create_role(
        role_code="QUERY_CAPABILITY_ONLY",
        display_name="SQL 生成のみ",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    direct_role = service.create_role(
        role_code="DIRECT_SQL_ONLY",
        display_name="SELECT SQL 実行のみ",
        description="",
        permissions={"menu.direct_sql"},
        entitlements=[],
        actor=admin,
    )
    feedback_role = service.create_role(
        role_code="FEEDBACK_MANAGER",
        display_name="フィードバック管理",
        description="",
        permissions={"menu.feedback_management"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=admin,
    )
    # SQL 生成評価は生成の権限を含み、実行の権限を含まない（#242）。
    evaluation_role = service.create_role(
        role_code="EVALUATION_ONLY",
        display_name="SQL 生成評価のみ",
        description="",
        permissions={"menu.evaluation"},
        entitlements=[],
        actor=admin,
    )
    data_role = service.create_role(
        role_code="DATA_MANAGER",
        display_name="データ管理",
        description="",
        permissions={"menu.data_management"},
        entitlements=[],
        actor=admin,
    )
    query_user = _create_active_user(
        service,
        admin,
        login_user_id="query.capability",
        display_name="SQL 生成ユーザー",
        role_ids=[query_role.role_id],
        password="QueryCapabilityPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="direct.only",
        display_name="SELECT SQL 実行ユーザー",
        role_ids=[direct_role.role_id],
        password="DirectOnlyPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="evaluation.only",
        display_name="SQL 生成評価ユーザー",
        role_ids=[evaluation_role.role_id],
        password="EvaluationOnlyPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="feedback.manager",
        display_name="フィードバック管理者",
        role_ids=[feedback_role.role_id],
        password="FeedbackManagerPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="data.manager",
        display_name="データ管理者",
        role_ids=[data_role.role_id],
        password="DataManagerPass!123",
    )

    feature_service = Nl2SqlService(store=MemoryNl2SqlStore())
    feature_service._history = [  # noqa: SLF001 - actor boundary fixture
        HistoryItem(
            id="query-history",
            question="社員一覧",
            engine=Nl2SqlEngine.SELECT_AI,
            generated_sql="SELECT EMPLOYEE_ID FROM EMPLOYEE",
            created_at="2026-08-20T00:00:00+00:00",
            actor_user_uuid=query_user.user_uuid,
        ),
        HistoryItem(
            id="other-history",
            question="部署一覧",
            engine=Nl2SqlEngine.SELECT_AI,
            generated_sql="SELECT DEPARTMENT_ID FROM DEPARTMENT",
            created_at="2026-08-20T00:01:00+00:00",
            actor_user_uuid="other-user",
        ),
    ]
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", feature_service)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/nl2sql/persistence")).status_code == 401
            _, csrf = await _login_api(client, "query.capability", "QueryCapabilityPass!123")
            persistence = await client.get("/api/nl2sql/persistence")
            assert persistence.status_code == 200
            assert persistence.json()["data"]["ready"] is True
            assert (await client.get("/api/nl2sql/diagnostics")).status_code == 403
            assert (
                await client.post(
                    "/api/nl2sql/persistence/recover",
                    headers={"X-CSRF-Token": csrf},
                )
            ).status_code == 403
            own_feedback = await client.post(
                "/api/nl2sql/feedback",
                headers={"X-CSRF-Token": csrf},
                json={
                    "history_id": "query-history",
                    "rating": "good",
                    "comment": "自分の履歴",
                },
            )
            assert own_feedback.status_code == 200
            other_feedback = await client.post(
                "/api/nl2sql/feedback",
                headers={"X-CSRF-Token": csrf},
                json={
                    "history_id": "other-history",
                    "rating": "bad",
                    "comment": "他人の履歴",
                },
            )
            assert other_feedback.status_code == 404  # 存在を漏らさない（#1889）
            assert (await client.get("/api/nl2sql/feedback")).status_code == 403
            assert (
                await client.post(
                    "/api/nl2sql/sample-data/import",
                    headers={"X-CSRF-Token": csrf},
                    json={"step": "all", "confirmation": "ADMIN_EXECUTE"},
                )
            ).status_code == 403
            assert (await client.get("/api/nl2sql/legacy-learning-material")).status_code == 403
            assert (
                await client.post(
                    "/api/nl2sql/select-ai/db-profiles/refresh-jobs",
                    headers={"X-CSRF-Token": csrf},
                )
            ).status_code == 403

            _, csrf = await _login_api(client, "direct.only", "DirectOnlyPass!123")
            analyze = await client.post(
                "/api/nl2sql/analyze",
                headers={"X-CSRF-Token": csrf},
                json={"sql": "SELECT EMPLOYEE_ID FROM EMPLOYEE"},
            )
            assert analyze.status_code == 200
            assert (
                await client.post(
                    "/api/nl2sql/jobs",
                    headers={"X-CSRF-Token": csrf},
                    json={"question": "社員一覧", "profile_id": "default"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/nl2sql/rewrite",
                    headers={"X-CSRF-Token": csrf},
                    json={"question": "社員一覧", "profile_id": "default"},
                )
            ).status_code == 403

            # 生成の権限だけでは、SQL を実行する job を作れない（#242）。
            _, csrf = await _login_api(client, "evaluation.only", "EvaluationOnlyPass!123")
            evaluation_job = await client.post(
                "/api/nl2sql/jobs",
                headers={"X-CSRF-Token": csrf},
                json={"question": "社員一覧", "profile_id": "default"},
            )
            assert evaluation_job.status_code == 403
            assert evaluation_job.json()["error_code"] == "SECURITY_ROUTE_FORBIDDEN"

            _, csrf = await _login_api(
                client,
                "feedback.manager",
                "FeedbackManagerPass!123",
            )
            feedback_list = await client.get("/api/nl2sql/feedback")
            assert feedback_list.status_code == 200
            assert {item["id"] for item in feedback_list.json()["data"]["items"]} == {
                "query-history",
                "other-history",
            }
            manager_feedback = await client.post(
                "/api/nl2sql/feedback",
                headers={"X-CSRF-Token": csrf},
                json={
                    "history_id": "other-history",
                    "rating": "good",
                    "comment": "管理の権限でも他人の履歴は SQL 生成の評価で更新しない",
                },
            )
            assert manager_feedback.status_code == 404  # 「AI 活用」は本人だけ（#1889）
            assert (await client.get("/api/nl2sql/feedback-index")).status_code == 200

            await _login_api(client, "data.manager", "DataManagerPass!123")
            assert (await client.get("/api/nl2sql/select-ai/db-profiles")).status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_security_migration_preview_lists_platform_and_nl2sql_ddl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["app_security_migrate"])

    assert security_migrate_main() == 0

    output = capsys.readouterr().out
    assert "migration=platform-auth" in output
    assert "migration=004 statements=4" in output
    assert "mode=preview" in output
    # 配備が retry する CLI。DB の停止中に TNS の再試行で長く待たない（#1779）。
    from pr_backend_core.oracle_dsn import cli_connect_mode_enabled

    assert cli_connect_mode_enabled()
    for removed in ("migration=005", "migration=009", "migration=024", "migration=025"):
        assert removed not in output


class _MigrationCursor:
    def __init__(self, tables: set[str]) -> None:
        self.tables = tables

    def __enter__(self) -> _MigrationCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, binds: dict[str, str] | None = None) -> None:
        self.statement = statement
        self.binds = binds or {}

    def fetchall(self) -> list[tuple[str]]:
        if "USER_TABLES" in self.statement:
            names = set(self.binds.values())
            return [(name,) for name in sorted(self.tables & names)]
        return []


class _MigrationConnection:
    def __init__(self, tables: set[str]) -> None:
        self.tables = tables

    def cursor(self) -> _MigrationCursor:
        return _MigrationCursor(self.tables)


def _patch_security_migration(monkeypatch: pytest.MonkeyPatch, tables: set[str]) -> list[list[str]]:
    import contextlib

    import app.cli.app_security_migrate as migrate

    batches: list[list[str]] = []

    class _Manager:
        @contextlib.contextmanager
        def control_connection(self) -> Any:
            yield _MigrationConnection(tables)

    class _Executor:
        def execute(self, connection: object, statements: list[str], **_: object) -> Any:
            batches.append(list(statements))
            return [{"status": "ok", "index": index} for index, _ in enumerate(statements, 1)]

    monkeypatch.setattr(migrate, "get_oracle_pool_manager", lambda: _Manager())
    monkeypatch.setattr(migrate, "oracle_statement_executor", _Executor())
    return batches


def test_security_migration_creates_platform_tables_before_nl2sql_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLATFORM_* の作成 → NL2SQL 固有の DDL（004）の順で、作成だけを行う（#212・#1648）。"""
    import app.cli.app_security_migrate as migrate

    batches = _patch_security_migration(monkeypatch, set())
    result = migrate.apply_security_migrations()

    assert len(batches) == 2
    assert any("CREATE TABLE PLATFORM_ROLES" in statement for statement in batches[0])
    nl2sql_statements = "\n".join(batches[1])
    assert "CREATE TABLE NL2SQL_APP_ROLE_PERMISSIONS (" in nl2sql_statements
    assert "CREATE TABLE NL2SQL_APP_USERS" not in nl2sql_statements
    assert "REFERENCES PLATFORM_ROLES" in nl2sql_statements
    assert result["legacy_tables"] == []


def test_security_migration_never_renames_or_drops_legacy_tables(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """既存の ADB の同じ schema に旧名の表があっても、承認なしに改名・削除・変更しない（#1648）。

    以前は毎回の配備で RAG_APP_* → NL2SQL_APP_* → PLATFORM_* の RENAME、旧監査ログの表の DROP、
    権限の行の DELETE を実行していた。今は名前を出して残す。
    """
    legacy = {
        "RAG_APP_USERS",
        "RAG_APP_ROLE_PERMISSIONS",
        "RAG_AUTH_AUDIT_LOG",
        "NL2SQL_APP_USERS",
        "NL2SQL_AUTH_AUDIT_LOG",
        "PLATFORM_USERS",
    }
    batches = _patch_security_migration(monkeypatch, legacy)
    monkeypatch.setattr("sys.argv", ["app_security_migrate", "--apply", "--skip-bootstrap"])

    assert security_migrate_main() == 0

    executed = [statement.upper() for batch in batches for statement in batch]
    # 列を足すだけの ALTER（共通の表の #1874 の RESOURCE_SCOPES）は既存の行を変えないので許す。
    destructive = re.compile(
        r"^\s*(RENAME|DROP|DELETE|TRUNCATE|UPDATE|INSERT|MERGE)\b"
        r"|^\s*ALTER\b(?!\s+TABLE\s+\w+\s+ADD\s*\()"
    )
    assert not [statement for statement in executed if destructive.match(statement)]
    for name in legacy - {"PLATFORM_USERS"}:
        assert not any(name in statement for statement in executed)
    captured = capsys.readouterr()
    assert "legacy_tables=5" in captured.out
    for name in ("RAG_APP_USERS", "RAG_AUTH_AUDIT_LOG", "NL2SQL_APP_USERS"):
        assert name in captured.err
    assert "改名・削除・変更せずに残します" in captured.err


def test_missing_security_tables_lists_tables_the_apply_creates() -> None:
    import app.cli.app_security_migrate as migrate

    connection = _MigrationConnection(set(migrate.SECURITY_TABLES) - {"NL2SQL_APP_ROLE_PROFILES"})
    assert migrate.missing_security_tables(connection) == ["NL2SQL_APP_ROLE_PROFILES"]
    assert migrate.missing_security_tables(_MigrationConnection(set(migrate.SECURITY_TABLES))) == []


def test_auth_api_sets_http_only_session_and_requires_csrf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_security_threadpools(monkeypatch)
    _patch_app_admin_env(monkeypatch, tmp_path, password="BootstrapPass123")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_auth_cookie_secure", False)
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_password", "BootstrapPass!123")
    monkeypatch.setattr(settings, "app_admin_login_user_id", "system_admin")
    monkeypatch.setattr(settings, "app_admin_login_user_password", "BootstrapPass123")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthenticated = await client.get("/api/auth/me")
            assert unauthenticated.status_code == 401

            login = await client.post(
                "/api/auth/login",
                json={"login_user_id": "system_admin", "password": "BootstrapPass123"},
            )
            assert login.status_code == 200
            assert "HttpOnly" in login.headers.get_list("set-cookie")[0]
            me = await client.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json()["data"]["role_codes"] == ["SYSTEM_ADMIN"]
            # 構成のシステム管理者は名前だけを表示名にし、ロールは表示名で返す（#1644）。
            assert me.json()["data"]["display_name"] == "システム管理者"
            assert me.json()["data"]["role_display_names"] == ["システム管理者"]
            assert me.json()["data"]["force_password_change"] is False

            no_csrf = await client.post("/api/auth/logout")
            assert no_csrf.status_code == 403
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            logout = await client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": csrf},
            )
            assert logout.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_deepsec_config_patch_updates_runtime_without_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_security_threadpools(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NL2SQL_LOG_LEVEL=INFO\n"
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=false\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=OldSecret123\n"
        "NL2SQL_RUNTIME_MODE=oracle\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_dsn", "")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", False)
    monkeypatch.setattr(settings, "oracle_deepsec_data_user", "")
    monkeypatch.setattr(settings, "oracle_deepsec_data_user_password", "")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            invalid = await client.patch(
                "/api/security/deepsec/config",
                json={"data_user_password": "short"},
            )
            assert invalid.status_code == 422
            assert settings.oracle_deepsec_enabled is False
            assert settings.oracle_deepsec_data_user_password == ""
            assert closed == []

            # 「${...}」は .env を読み直すと展開されて別の値になるので保存しない。
            interpolated = await client.patch(
                "/api/security/deepsec/config",
                json={"data_user_password": "Abc${HOME}defghij"},
            )
            assert interpolated.status_code == 422
            assert "${HOME}" not in interpolated.text
            assert settings.oracle_deepsec_data_user_password == ""
            assert closed == []

            response = await client.patch(
                "/api/security/deepsec/config",
                json={"data_user_password": "DeepSecret!456"},
            )
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["deepsec_enabled"] is True
            assert data["data_user"] == "DEEPSEC_DATA_USER"
            assert data["has_data_user_password"] is True
            assert "DeepSecret!456" not in response.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    assert settings.oracle_deepsec_enabled is True
    assert settings.oracle_deepsec_data_user == "DEEPSEC_DATA_USER"
    assert settings.oracle_deepsec_data_user_password == "DeepSecret!456"
    assert closed == [True]
    env_text = env_file.read_text(encoding="utf-8")
    assert "NL2SQL_ORACLE_DEEPSEC_ENABLED=true" in env_text
    assert "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER" in env_text
    assert "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=DeepSecret!456" in env_text
    # 既存の DeepSec key の位置で置き換え、他の製品設定は残す（#211）。
    assert env_text == (
        "NL2SQL_LOG_LEVEL=INFO\n"
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=DeepSecret!456\n"
        "NL2SQL_RUNTIME_MODE=oracle\n"
    )


def test_deepsec_config_sync_password_endpoint_uses_saved_secret_without_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_security_threadpools(monkeypatch)
    calls: list[str] = []

    def sync_saved(_service: object) -> dict[str, object]:
        settings = get_settings()
        calls.append(settings.oracle_deepsec_data_user_password)
        return {
            "configured": True,
            "driver_mode": "thin",
            "connection_security": "walletless_tls",
            "deepsec_enabled": True,
            "data_user": "DEEPSEC_DATA_USER",
            "has_data_user_password": True,
            "objects": {},
            "message": "同期しました。",
        }

    monkeypatch.setattr(
        "app.security.deepsec.DeepSecService.sync_saved_data_user_password",
        sync_saved,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_dsn", "test")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_connection_security", "walletless_tls")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", True)
    monkeypatch.setattr(settings, "oracle_deepsec_data_user", "DEEPSEC_DATA_USER")
    monkeypatch.setattr(settings, "oracle_deepsec_data_user_password", "DeepSecret!Stored")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/security/deepsec/config/sync-password")
            assert response.status_code == 200
            assert response.json()["data"]["has_data_user_password"] is True
            assert "DeepSecret!Stored" not in response.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    assert calls == ["DeepSecret!Stored"]
    operation = app.openapi()["paths"]["/api/security/deepsec/config/sync-password"]["post"]
    assert "requestBody" not in operation


def test_deepsec_config_sync_password_endpoint_treats_ora28007_as_success_when_login_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_security_threadpools(monkeypatch)
    login_probes: list[str] = []

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-28007: The password cannot be reused.")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    def control_connection(_manager: object) -> SyncConnection:
        return SyncConnection()

    def validate_data_user_login(manager) -> None:  # type: ignore[no-untyped-def]
        login_probes.append(manager.settings.oracle_deepsec_data_user_password)

    monkeypatch.setattr(
        "app.clients.oracle_runtime.OraclePoolManager.control_connection",
        control_connection,
    )
    monkeypatch.setattr(
        "app.clients.oracle_runtime.OraclePoolManager.validate_data_user_login",
        validate_data_user_login,
    )
    monkeypatch.setattr(
        "app.security.deepsec.DeepSecService.status",
        lambda _service: {
            "configured": True,
            "driver_mode": "thin",
            "connection_security": "walletless_tls",
            "deepsec_enabled": True,
            "data_user": "DEEPSEC_DATA_USER",
            "has_data_user_password": True,
            "objects": {},
            "message": "同期しました。",
        },
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_dsn", "test")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_connection_security", "walletless_tls")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", True)
    monkeypatch.setattr(settings, "oracle_deepsec_data_user", "DEEPSEC_DATA_USER")
    monkeypatch.setattr(settings, "oracle_deepsec_data_user_password", "DeepSecret!Stored")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/security/deepsec/config/sync-password")
            assert response.status_code == 200
            assert response.json()["data"]["has_data_user_password"] is True
            assert "DeepSecret!Stored" not in response.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    assert login_probes == ["DeepSecret!Stored"]


def test_deepsec_config_sync_password_endpoint_keeps_409_when_ora28007_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_security_threadpools(monkeypatch)

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-28007: password=DeepSecret!Stored cannot be reused")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    def control_connection(_manager: object) -> SyncConnection:
        return SyncConnection()

    def fail_data_user_login(_manager: object) -> None:
        raise RuntimeError("ORA-01017: invalid credential password=DeepSecret!Stored")

    monkeypatch.setattr(
        "app.clients.oracle_runtime.OraclePoolManager.control_connection",
        control_connection,
    )
    monkeypatch.setattr(
        "app.clients.oracle_runtime.OraclePoolManager.validate_data_user_login",
        fail_data_user_login,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_dsn", "test")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_connection_security", "walletless_tls")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", True)
    monkeypatch.setattr(settings, "oracle_deepsec_data_user", "DEEPSEC_DATA_USER")
    monkeypatch.setattr(settings, "oracle_deepsec_data_user_password", "DeepSecret!Stored")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/security/deepsec/config/sync-password")
            assert response.status_code == 409
            body = response.json()
            assert "新しい DATA USER パスワード" in body["problem"]["detail"]
            assert "DeepSecret!Stored" not in response.text
            assert "[REDACTED]" in response.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_deepsec_config_patch_sync_failure_returns_409_without_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_security_threadpools(monkeypatch)
    env_file = tmp_path / ".env"
    original_env = (
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=false\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=OldSecret!123\n"
    )
    env_file.write_text(original_env, encoding="utf-8")
    env_file.chmod(0o600)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    def fail_sync(_service: object) -> bool:
        raise SecurityApiError(
            409,
            "DeepSec DATA USER パスワードを Oracle END USER へ同期できませんでした: "
            "ORA-01031 [REDACTED]",
        )

    monkeypatch.setattr(
        "app.security.deepsec.DeepSecService._sync_existing_data_user_password",
        fail_sync,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_dsn", "test")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_connection_security", "walletless_tls")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", False)
    monkeypatch.setattr(settings, "oracle_deepsec_data_user", "DEEPSEC_DATA_USER")
    monkeypatch.setattr(settings, "oracle_deepsec_data_user_password", "OldSecret!123")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.patch(
                "/api/security/deepsec/config",
                json={"data_user_password": "DeepSecret!456"},
            )
            assert response.status_code == 409
            body = response.json()
            assert (
                "DeepSec DATA USER パスワードを Oracle END USER へ同期" in body["problem"]["detail"]
            )
            assert "DeepSecret!456" not in response.text
            assert "[REDACTED]" in response.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    assert settings.oracle_deepsec_enabled is False
    assert settings.oracle_deepsec_data_user_password == "OldSecret!123"
    assert env_file.read_text(encoding="utf-8") == original_env
    assert closed == [True]


def test_api_enforces_menu_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_security_threadpools(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_auth_cookie_secure", False)
    monkeypatch.setattr(settings, "oracle_user", "DBADMIN")
    monkeypatch.setattr(settings, "oracle_password", "DbAdminPass!123")
    monkeypatch.setattr(settings, "app_admin_login_user_id", "system_admin")
    monkeypatch.setattr(settings, "app_admin_login_user_password", "AppAdminPass123")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()
    from app.security.service import get_security_service

    service = get_security_service()
    assert isinstance(service.store, InMemorySecurityStore)
    assert service.store.bootstrap(
        login_user_id="ADMIN",
        display_name="ADMIN（システム管理者）",
        password_hash=hash_password("BootstrapPass!123"),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/auth/login",
                json={"login_user_id": "ADMIN", "password": "BootstrapPass!123"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            forced = await client.get("/api/security/users")
            assert forced.status_code == 403
            changed = await client.post(
                "/api/auth/password/change",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": "BootstrapPass!123",
                    "new_password": "IndependentPass!456",
                },
            )
            assert changed.status_code == 200
            await client.post(
                "/api/auth/login",
                json={"login_user_id": "ADMIN", "password": "IndependentPass!456"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            role_id = await _create_role_api(
                client,
                csrf,
                role_code="HISTORY_VIEWER",
                display_name="履歴閲覧",
                permissions=["menu.history"],
            )
            deepsec_role_id = await _create_role_api(
                client,
                csrf,
                role_code="DEEPSEC_MANAGER",
                display_name="DeepSec 管理",
                permissions=["menu.security_deepsec"],
            )
            user_response = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "viewer.user",
                    "display_name": "履歴閲覧ユーザー",
                    "temporary_password": "ViewerStart!123",
                    "role_ids": [role_id],
                },
            )
            assert user_response.status_code == 200
            deepsec_user_response = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "deepsec.user",
                    "display_name": "DeepSec 管理ユーザー",
                    "temporary_password": "DeepSecStart!123",
                    "role_ids": [deepsec_role_id],
                },
            )
            assert deepsec_user_response.status_code == 200
            await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

            await client.post(
                "/api/auth/login",
                json={"login_user_id": "viewer.user", "password": "ViewerStart!123"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            await client.post(
                "/api/auth/password/change",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": "ViewerStart!123",
                    "new_password": "ViewerActive!456",
                },
            )
            await client.post(
                "/api/auth/login",
                json={"login_user_id": "viewer.user", "password": "ViewerActive!456"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            assert (await client.get("/api/nl2sql/history")).status_code == 200
            denied_execute = await client.post(
                "/api/nl2sql/preview",
                headers={"X-CSRF-Token": csrf},
                json={"question": "社員一覧"},
            )
            assert denied_execute.status_code == 403
            assert (await client.get("/api/security/users")).status_code == 403
            assert (await client.get("/api/security/deepsec/data-entitlements")).status_code == 403

            await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
            await client.post(
                "/api/auth/login",
                json={"login_user_id": "deepsec.user", "password": "DeepSecStart!123"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            await client.post(
                "/api/auth/password/change",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": "DeepSecStart!123",
                    "new_password": "DeepSecActive!456",
                },
            )
            await client.post(
                "/api/auth/login",
                json={"login_user_id": "deepsec.user", "password": "DeepSecActive!456"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            assert (await client.get("/api/security/roles")).status_code == 403
            entitlement_response = await client.get("/api/security/deepsec/data-entitlements")
            assert entitlement_response.status_code == 200
            assert any(
                item["role_code"] == "DEEPSEC_MANAGER"
                for item in entitlement_response.json()["data"]
            )
            deepsec_role_version = next(
                item["version"]
                for item in entitlement_response.json()["data"]
                if item["role_id"] == deepsec_role_id
            )
            patch_response = await client.patch(
                f"/api/security/deepsec/data-entitlements/{deepsec_role_id}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": deepsec_role_version,
                    "data_entitlements": [
                        {
                            "resource_code": "HR.EMPLOYEES",
                            "scope_code": "SALES",
                            "capability": "SELECT",
                            "target_owner": "HR",
                            "target_object": "EMPLOYEES",
                            "target_type": "TABLE",
                            "column_names": ["EMPLOYEE_ID", "DISPLAY_NAME"],
                            "scope_mode": "COLUMN_EQUALS",
                            "scope_column": "DEPARTMENT_CODE",
                        }
                    ],
                },
            )
            assert patch_response.status_code == 200
            assert patch_response.json()["data"]["data_entitlements"][0]["scope_code"] == "SALES"
            stored_role = service.store.get_role(deepsec_role_id)
            assert stored_role is not None
            assert stored_role.display_name == "DeepSec 管理"
            assert stored_role.permissions == {"menu.security_deepsec"}

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_user_manager_can_load_role_options_but_not_manage_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    user_manager_role = service.create_role(
        role_code="USER_MANAGER",
        display_name="ユーザー管理",
        description="",
        permissions={"menu.security_users"},
        entitlements=[],
        actor=admin,
    )
    role_manager_role = service.create_role(
        role_code="ROLE_MANAGER",
        display_name="ロール管理",
        description="",
        permissions={"menu.security_roles"},
        entitlements=[],
        actor=admin,
    )
    query_role = service.create_role(
        role_code="QUERY_VIEWER_FOR_USER_MANAGER",
        display_name="SQL 生成",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    archived_role = service.create_role(
        role_code="ARCHIVED_USER_MANAGER",
        display_name="廃止ユーザー管理",
        description="",
        permissions={"menu.security_users"},
        entitlements=[],
        actor=admin,
    )
    service.archive_role(
        archived_role.role_id,
        expected_version=archived_role.version,
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="user.manager",
        display_name="ユーザー管理者",
        role_ids=[user_manager_role.role_id],
        password="UserManagerPass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "user.manager", "UserManagerPass!123")

            users_response = await client.get("/api/security/users")
            assert users_response.status_code == 200

            roles_response = await client.get("/api/security/roles?include_archived=true")
            assert roles_response.status_code == 200
            assert [item["role_code"] for item in roles_response.json()["data"]] == ["USER_MANAGER"]

            own_role = await client.get(f"/api/security/roles/{user_manager_role.role_id}")
            assert own_role.status_code == 200
            hidden_role = await client.get(f"/api/security/roles/{query_role.role_id}")
            assert hidden_role.status_code == 404

            create_role = await client.post(
                "/api/security/roles",
                headers={"X-CSRF-Token": csrf},
                json={"role_code": "SHOULD_NOT_CREATE", "display_name": "作成不可"},
            )
            assert create_role.status_code == 403

            update_role = await client.patch(
                f"/api/security/roles/{user_manager_role.role_id}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": user_manager_role.version,
                    "display_name": "変更不可",
                    "description": "",
                    "permissions": ["menu.security_users"],
                    "data_entitlements": [],
                },
            )
            assert update_role.status_code == 403

            archive_role = await client.post(
                f"/api/security/roles/{role_manager_role.role_id}/archive",
                headers={"X-CSRF-Token": csrf},
                json={"version": role_manager_role.version},
            )
            assert archive_role.status_code == 403
            restore_role = await client.post(
                f"/api/security/roles/{archived_role.role_id}/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": archived_role.version},
            )
            assert restore_role.status_code == 403
            delete_role = await client.delete(
                f"/api/security/roles/{archived_role.role_id}",
                headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            )
            assert delete_role.status_code == 403
            assert (await client.get("/api/security/permissions")).status_code == 403

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_security_delete_api_requires_csrf_and_if_match_and_deletes_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    admin_user = service.store.get_user(admin.user_uuid)
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )
    role = service.create_role(
        role_code="DELETE_API_ROLE",
        display_name="削除 API ロール",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    user, _ = service.create_user(
        login_user_id="delete.api.user",
        display_name="削除 API ユーザー",
        role_ids=[role.role_id],
        temporary_password="DeleteApiUserPass!123",
        actor=admin,
    )
    disabled = service.update_user(
        user.user_uuid,
        expected_version=user.version,
        display_name=user.display_name,
        status="DISABLED",
        role_ids=user.role_ids,
        actor=admin,
    )
    archived = service.archive_role(
        role.role_id,
        expected_version=role.version,
        actor=admin,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "ADMIN", "BootstrapPass!123")
            user_path = f"/api/security/users/{user.user_uuid}"

            no_csrf = await client.delete(
                user_path,
                headers={"If-Match": f'"{disabled.version}"'},
            )
            assert no_csrf.status_code == 403

            missing_version = await client.delete(
                user_path,
                headers={"X-CSRF-Token": csrf},
            )
            assert missing_version.status_code == 428
            assert missing_version.json()["problem"]["code"] == "SECURITY_VERSION_REQUIRED"

            invalid_version = await client.delete(
                user_path,
                headers={"X-CSRF-Token": csrf, "If-Match": f'W/"{disabled.version}"'},
            )
            assert invalid_version.status_code == 400
            assert invalid_version.json()["problem"]["code"] == "SECURITY_VERSION_INVALID"

            stale = await client.delete(
                user_path,
                headers={"X-CSRF-Token": csrf, "If-Match": f'"{disabled.version - 1}"'},
            )
            assert stale.status_code == 409
            assert stale.json()["problem"]["code"] == "SECURITY_STATE_CONFLICT"

            deleted_user = await client.delete(
                user_path,
                headers={"X-CSRF-Token": csrf, "If-Match": f'"{disabled.version}"'},
            )
            assert deleted_user.status_code == 200
            assert deleted_user.json()["data"] == {
                "deleted": True,
                "user_uuid": user.user_uuid,
                "login_user_id": "delete.api.user",
            }
            deleted_user_again = await client.delete(
                user_path,
                headers={"X-CSRF-Token": csrf, "If-Match": f'"{disabled.version}"'},
            )
            assert deleted_user_again.status_code == 404
            assert deleted_user_again.json()["problem"]["code"] == "SECURITY_RESOURCE_NOT_FOUND"

            role_path = f"/api/security/roles/{role.role_id}"
            role_missing_version = await client.delete(
                role_path,
                headers={"X-CSRF-Token": csrf},
            )
            assert role_missing_version.status_code == 428

            deleted_role = await client.delete(
                role_path,
                headers={"X-CSRF-Token": csrf, "If-Match": f'"{archived.version}"'},
            )
            assert deleted_role.status_code == 200
            assert deleted_role.json()["data"] == {
                "deleted": True,
                "role_id": role.role_id,
                "role_code": "DELETE_API_ROLE",
            }
            deleted_role_again = await client.delete(
                role_path,
                headers={"X-CSRF-Token": csrf, "If-Match": f'"{archived.version}"'},
            )
            assert deleted_role_again.status_code == 404
            assert deleted_role_again.json()["problem"]["code"] == "SECURITY_RESOURCE_NOT_FOUND"

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_security_conflicts_and_validation_use_problem_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin_user = service.store.get_user_by_login_user_id("admin")
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "ADMIN", "BootstrapPass!123")
            headers = {
                "X-CSRF-Token": csrf,
                "X-Request-ID": "security-problem-contract-test",
            }
            role_payload = {"role_code": "DATA_USER", "display_name": "データユーザー"}
            created_role = await client.post(
                "/api/security/roles",
                headers=headers,
                json=role_payload,
            )
            assert created_role.status_code == 200
            role_id = created_role.json()["data"]["role_id"]

            duplicate_role = await client.post(
                "/api/security/roles",
                headers=headers,
                json=role_payload,
            )
            assert_problem_contract(
                duplicate_role,
                code="SECURITY_ROLE_CODE_CONFLICT",
                pointer="/role_code",
            )

            reserved_role = await client.post(
                "/api/security/roles",
                headers=headers,
                json={
                    **role_payload,
                    "role_code": "system_admin",
                    "display_name": "追加システム管理者",
                },
            )
            assert_problem_contract(
                reserved_role,
                code="SECURITY_ROLE_CODE_RESERVED",
                pointer="/role_code",
            )

            user_payload = {
                "login_user_id": "duplicate.user",
                "display_name": "重複確認ユーザー",
                "temporary_password": "DuplicatePass!123",
                "role_ids": [role_id],
            }
            created_user = await client.post(
                "/api/security/users",
                headers=headers,
                json=user_payload,
            )
            assert created_user.status_code == 200

            duplicate_user = await client.post(
                "/api/security/users",
                headers=headers,
                json=user_payload,
            )
            assert_problem_contract(
                duplicate_user,
                code="SECURITY_USER_LOGIN_ID_CONFLICT",
                pointer="/login_user_id",
            )
            assert duplicate_user.json()["problem"]["detail"] == (
                "このログインユーザーIDは既に使用されています。別のIDを入力してください。"
            )

            invalid_user = await client.post(
                "/api/security/users",
                headers=headers,
                json={"login_user_id": "", "display_name": ""},
            )
            assert invalid_user.status_code == 422
            invalid_body = invalid_user.json()
            assert invalid_body["error_code"] == "REQUEST_VALIDATION_FAILED"
            assert {item["pointer"] for item in invalid_body["problem"]["field_errors"]} == {
                "/login_user_id",
                "/display_name",
                "/role_ids",
            }
            # ロールは画面と同じく必須（#540）。欄の下に出す文言は画面と同じ。
            role_error = next(
                item
                for item in invalid_body["problem"]["field_errors"]
                if item["pointer"] == "/role_ids"
            )
            assert role_error["message"] == "ロールを選択してください。"

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_missing_users_table_uses_409_problem_contract_without_oracle_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin_user = service.store.get_user_by_login_user_id("admin")
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login_response = await client.post(
                "/api/auth/login",
                json={"login_user_id": "ADMIN", "password": "BootstrapPass!123"},
            )
            assert login_response.status_code == 200

            def missing_users() -> list[UserRecord]:
                raise RuntimeError(
                    'ORA-00942: table or view "ADMIN"."PLATFORM_USERS" does not exist'
                )

            monkeypatch.setattr(service.store, "list_users", missing_users)
            response = await client.get(
                "/api/security/users",
                headers={"X-Request-ID": "security-migration-request"},
            )

        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
        assert body["problem"]["code"] == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
        assert body["problem"]["request_id"] == "security-migration-request"
        assert body["problem"]["retryable"] is False
        assert "app_security_migrate" in body["problem"]["detail"]
        assert "ORA-00942" not in response.text
        assert "PLATFORM_USERS" not in response.text

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def assert_problem_contract(
    response: httpx.Response,
    *,
    code: str,
    pointer: str,
) -> None:
    assert response.status_code == 409
    body = response.json()
    problem = body["problem"]
    assert body["error_code"] == code
    assert body["error_messages"][0] == problem["detail"]
    assert problem["code"] == code
    assert problem["status"] == 409
    assert problem["type"] == f"urn:nl2sql:problem:{code.lower().replace('_', '-')}"
    assert problem["retryable"] is False
    assert problem["field_errors"][0]["pointer"] == pointer
    assert problem["request_id"] == "security-problem-contract-test"
    assert response.headers["X-Request-ID"] == problem["request_id"]


def test_role_manager_cannot_manage_users(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    role_manager_role = service.create_role(
        role_code="ROLE_MANAGER_ONLY",
        display_name="ロール管理のみ",
        description="",
        permissions={"menu.security_roles"},
        entitlements=[],
        actor=admin,
    )
    query_role = service.create_role(
        role_code="QUERY_ROLE_FOR_ROLE_MANAGER",
        display_name="SQL 生成",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    target = _create_active_user(
        service,
        admin,
        login_user_id="query.target",
        display_name="検索ユーザー",
        role_ids=[query_role.role_id],
        password="QueryTargetPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="role.manager",
        display_name="ロール管理者",
        role_ids=[role_manager_role.role_id],
        password="RoleManagerPass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "role.manager", "RoleManagerPass!123")

            roles_response = await client.get("/api/security/roles")
            assert roles_response.status_code == 200
            assert any(
                item["role_code"] == "ROLE_MANAGER_ONLY" for item in roles_response.json()["data"]
            )
            # 権限の付与は権限管理（menu.security_permissions）の担当（#206）。
            assert (await client.get("/api/security/permissions")).status_code == 403

            create_role = await client.post(
                "/api/security/roles",
                headers={"X-CSRF-Token": csrf},
                json={"role_code": "ROLE_MANAGER_CREATED", "display_name": "作成可"},
            )
            assert create_role.status_code == 200
            created = create_role.json()["data"]
            grant = await client.put(
                f"/api/security/roles/{created['role_id']}/permissions",
                headers={"X-CSRF-Token": csrf},
                json={"version": created["version"], "permissions": []},
            )
            assert grant.status_code == 403

            assert (await client.get("/api/security/users")).status_code == 403
            create_user = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "blocked.user",
                    "display_name": "作成不可",
                    "temporary_password": "BlockedUserPass!123",
                    "role_ids": [query_role.role_id],
                },
            )
            assert create_user.status_code == 403

            update_user = await client.patch(
                f"/api/security/users/{target.user_uuid}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": target.version,
                    "display_name": target.display_name,
                    "status": target.status,
                    "role_ids": target.role_ids,
                },
            )
            assert update_user.status_code == 403
            delete_user = await client.delete(
                f"/api/security/users/{target.user_uuid}",
                headers={"X-CSRF-Token": csrf, "If-Match": f'"{target.version}"'},
            )
            assert delete_user.status_code == 403

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_user_manager_cannot_assign_role_outside_own_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    user_manager_role = service.create_role(
        role_code="LIMITED_USER_MANAGER",
        display_name="限定ユーザー管理",
        description="",
        permissions={"menu.security_users"},
        entitlements=[],
        actor=admin,
    )
    role_manager_role = service.create_role(
        role_code="FORBIDDEN_ROLE_MANAGER",
        display_name="ロール管理",
        description="",
        permissions={"menu.security_roles"},
        entitlements=[],
        actor=admin,
    )
    admin_sql_role = service.create_role(
        role_code="FORBIDDEN_ADMIN_SQL",
        display_name="管理 SQL",
        description="",
        permissions={"menu.admin_sql"},
        entitlements=[],
        actor=admin,
    )
    target = _create_active_user(
        service,
        admin,
        login_user_id="managed.user",
        display_name="管理対象",
        role_ids=[user_manager_role.role_id],
        password="ManagedUserPass!123",
    )
    high_privilege_user = _create_active_user(
        service,
        admin,
        login_user_id="high.user",
        display_name="高権限ユーザー",
        role_ids=[role_manager_role.role_id],
        password="HighUserPass!123",
    )
    _create_active_user(
        service,
        admin,
        login_user_id="limited.manager",
        display_name="限定管理者",
        role_ids=[user_manager_role.role_id],
        password="LimitedManagerPass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "limited.manager", "LimitedManagerPass!123")

            denied_role_manager = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "blocked.role.manager",
                    "display_name": "ロール管理付与不可",
                    "temporary_password": "BlockedRolePass!123",
                    "role_ids": [role_manager_role.role_id],
                },
            )
            assert denied_role_manager.status_code == 403

            denied_admin_sql = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "blocked.admin.sql",
                    "display_name": "管理 SQL 付与不可",
                    "temporary_password": "BlockedAdminPass!123",
                    "role_ids": [admin_sql_role.role_id],
                },
            )
            assert denied_admin_sql.status_code == 403

            denied_update = await client.patch(
                f"/api/security/users/{target.user_uuid}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": target.version,
                    "display_name": target.display_name,
                    "status": target.status,
                    "role_ids": [role_manager_role.role_id],
                },
            )
            assert denied_update.status_code == 403

            denied_reset = await client.post(
                f"/api/security/users/{high_privilege_user.user_uuid}/reset-password",
                headers={"X-CSRF-Token": csrf},
                json={"temporary_password": "TakeoverPass!123"},
            )
            assert denied_reset.status_code == 403

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_user_manager_can_assign_subset_role_and_runtime_access_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    manager_role = service.create_role(
        role_code="USER_AND_QUERY_MANAGER",
        display_name="ユーザー管理と SQL 生成",
        description="",
        permissions={"menu.security_users", "menu.query"},
        entitlements=[],
        actor=admin,
    )
    query_role = service.create_role(
        role_code="QUERY_SUBSET",
        display_name="SQL 生成のみ",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    direct_sql_role = service.create_role(
        role_code="DIRECT_SQL_NOT_ASSIGNABLE",
        display_name="SELECT SQL 実行",
        description="",
        permissions={"menu.direct_sql"},
        entitlements=[],
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="query.manager",
        display_name="SQL 生成ユーザー管理者",
        role_ids=[manager_role.role_id],
        password="QueryManagerPass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "query.manager", "QueryManagerPass!123")

            roles_response = await client.get("/api/security/roles")
            assert roles_response.status_code == 200
            role_codes = {item["role_code"] for item in roles_response.json()["data"]}
            assert {"USER_AND_QUERY_MANAGER", "QUERY_SUBSET"} <= role_codes
            assert direct_sql_role.role_code not in role_codes

            created = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "query.subset.user",
                    "display_name": "SQL 生成利用者",
                    "temporary_password": "SubsetStartPass!123",
                    "role_ids": [query_role.role_id],
                },
            )
            assert created.status_code == 200

            _, csrf = await _login_api(client, "query.subset.user", "SubsetStartPass!123")
            changed = await client.post(
                "/api/auth/password/change",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": "SubsetStartPass!123",
                    "new_password": "SubsetActivePass!456",
                },
            )
            assert changed.status_code == 200

            current, _ = await _login_api(
                client,
                "query.subset.user",
                "SubsetActivePass!456",
            )
            assert current["role_codes"] == ["QUERY_SUBSET"]
            assert set(current["permissions"]) == {  # type: ignore[call-overload]
                "menu.query",
                FEEDBACK_WRITE_PERMISSION,
                PROFILE_READ_PERMISSION,
                QUERY_GENERATE_PERMISSION,
                SCHEMA_READ_PERMISSION,
                SQL_EXECUTE_PERMISSION,
            }
            assert (await client.get("/api/nl2sql/history")).status_code == 200
            assert (await client.get("/api/security/users")).status_code == 403

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_role_manager_can_add_profile_manage_to_role_with_explicit_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全プロファイル権限を含意する menu.profiles の追加は profile 差分検査を受けない。"""
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    manager_role = service.create_role(
        role_code="ROLE_MANAGER_PROFILES",
        display_name="ロール管理 + 業務プロファイル",
        description="",
        permissions={"menu.security_roles", "menu.security_permissions", "menu.profiles"},
        entitlements=[],
        actor=admin,
    )
    target = service.create_role(
        role_code="PROFILE_SCOPED",
        display_name="個別プロファイル",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        allowed_profile_ids={"default"},
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="role.manager",
        display_name="ロール管理者",
        role_ids=[manager_role.role_id],
        password="RoleManagerPass!123",
    )

    async def patch_target(
        client: httpx.AsyncClient,
        csrf: str,
        permissions: list[str],
        allowed_profile_ids: list[str],
    ) -> httpx.Response:
        current = service.get_role(target.role_id)
        assert current is not None
        return await client.put(
            f"/api/security/roles/{target.role_id}/permissions",
            headers={"X-CSRF-Token": csrf},
            json={
                "version": current.version,
                "permissions": permissions,
                "allowed_profile_ids": allowed_profile_ids,
            },
        )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "role.manager", "RoleManagerPass!123")

            # 個別プロファイルの変更自体は SYSTEM_ADMIN のみ(従来どおり)
            denied = await patch_target(client, csrf, ["menu.query"], [])
            assert denied.status_code == 403

            # frontend と同じく空配列で送っても、全プロファイル権限になるなら受理される
            granted = await patch_target(client, csrf, ["menu.query", "menu.profiles"], [])
            assert granted.status_code == 200, granted.text
            assert granted.json()["data"]["allowed_profile_ids"] == []
            assert "menu.profiles" in granted.json()["data"]["permissions"]

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_role_manager_cannot_add_permissions_beyond_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """ロール編集経由で自分が持たない権限を(自分にも他人にも)付与できない。"""
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    manager_role = service.create_role(
        role_code="ROLE_MANAGER_HISTORY",
        display_name="ロール管理 + 実行履歴",
        description="",
        permissions={"menu.security_roles", "menu.security_permissions", "menu.history"},
        entitlements=[],
        actor=admin,
    )
    other_role = service.create_role(
        role_code="ADMIN_SQL_ONLY",
        display_name="管理 SQL",
        description="",
        permissions={"menu.admin_sql"},
        entitlements=[],
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="role.manager",
        display_name="ロール管理者",
        role_ids=[manager_role.role_id],
        password="RoleManagerPass!123",
    )

    async def patch_permissions(
        client: httpx.AsyncClient, csrf: str, role_id: str, permissions: list[str]
    ) -> httpx.Response:
        current = service.get_role(role_id)
        assert current is not None
        return await client.put(
            f"/api/security/roles/{role_id}/permissions",
            headers={"X-CSRF-Token": csrf},
            json={"version": current.version, "permissions": permissions},
        )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "role.manager", "RoleManagerPass!123")
            assert (await client.get("/api/security/users")).status_code == 403

            # 自分のロールへ未保持の権限を追加 → 403、実効権限も変わらない
            self_escalation = await patch_permissions(
                client,
                csrf,
                manager_role.role_id,
                [
                    "menu.security_roles",
                    "menu.security_permissions",
                    "menu.history",
                    "menu.security_users",
                ],
            )
            assert self_escalation.status_code == 403
            assert "自分が持たない権限" in self_escalation.json()["error_messages"][0]
            assert (await client.get("/api/security/users")).status_code == 403

            # 他人のロールへ未保持の権限を追加 → 403
            other_escalation = await patch_permissions(
                client, csrf, other_role.role_id, ["menu.admin_sql", "menu.security_deepsec"]
            )
            assert other_escalation.status_code == 403

            # 自分が持つ権限の追加と、未保持でも既存権限の削除は可能
            allowed = await patch_permissions(client, csrf, other_role.role_id, ["menu.history"])
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["data"]["permissions"] == ["menu.history"]

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    manager_after = service.get_role(manager_role.role_id)
    assert manager_after is not None
    assert manager_after.permissions == {
        "menu.security_roles",
        "menu.security_permissions",
        "menu.history",
    }


def test_archived_role_cannot_be_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    """アーカイブ済みロールは PATCH で編集(や黙った復元)ができない。"""
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    service.store.set_password(
        admin.user_uuid, hash_password("BootstrapPass!123"), force_change=False
    )
    role = service.create_role(
        role_code="OLD_ROLE",
        display_name="旧ロール",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    role = service.archive_role(role.role_id, expected_version=role.version, actor=admin)
    assert role.archived

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "ADMIN", "BootstrapPass!123")
            response = await client.patch(
                f"/api/security/roles/{role.role_id}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": role.version,
                    "display_name": "旧ロール(編集)",
                    "description": "",
                    "permissions": ["menu.query", "menu.security_users"],
                },
            )
            assert response.status_code == 409
            assert "アーカイブ済みロールは変更できません" in response.json()["error_messages"][0]

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()

    unchanged = service.get_role(role.role_id)
    assert unchanged is not None
    assert unchanged.archived is True
    assert unchanged.display_name == "旧ロール"
    assert unchanged.permissions == {"menu.query"}
    assert unchanged.version == role.version


def test_role_patch_preserves_deepsec_entitlements(monkeypatch: pytest.MonkeyPatch) -> None:
    """ロール管理の PATCH と権限管理の PUT は DeepSec の Data Grant 定義を一切変更しない。"""
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    service.store.set_password(
        admin.user_uuid, hash_password("BootstrapPass!123"), force_change=False
    )
    role = service.create_role(
        role_code="SALES_VIEWER",
        display_name="営業閲覧",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    role = service.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[
            DataEntitlementRecord(
                entitlement_id="",
                role_id=role.role_id,
                resource_code="SALES.ORDERS",
                scope_code="",
                capability="SELECT",
                target_owner="SALES",
                target_object="ORDERS",
                column_names=["ORDER_ID", "REGION"],
                scope_mode="FILTERS",
                scope_filters=[
                    DataEntitlementScopeFilter(column_name="REGION", operator="EQ", value="JP")
                ],
                data_grant_name="NL2SQL_DG_TEST",
                apply_status="APPLIED",
            )
        ],
        actor=admin,
    )
    # DeepSec apply 完了後の状態(APPLIED + grant 名 + checksum)を再現する
    service.store.set_deepsec_entitlement_apply_state(
        role.entitlements[0].entitlement_id,
        status="APPLIED",
        data_grant_name="NL2SQL_DG_TEST",
        sql_checksum="checksum-test",
    )
    applied_role = service.get_role(role.role_id)
    assert applied_role is not None
    before = applied_role.entitlements[0]
    assert before.apply_status == "APPLIED"

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "ADMIN", "BootstrapPass!123")
            response = await client.patch(
                f"/api/security/roles/{role.role_id}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": applied_role.version,
                    "display_name": "営業閲覧(改名)",
                    "description": "",
                    "permissions": ["menu.query", "menu.history"],
                    # 旧 frontend が送っていた項目は無視される
                    "data_entitlements": [
                        {"resource_code": "SALES.ORDERS", "scope_code": "*", "capability": "SELECT"}
                    ],
                    "allowed_profile_ids": [],
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["display_name"] == "営業閲覧(改名)"
            # ロール管理の PATCH は基本情報だけを更新し、権限は変えない（#206）。
            assert data["permissions"] == ["menu.query"]
            assert_entitlement_preserved(data)

            # 権限管理の PUT も権限だけを更新し、名称と Data Grant を保つ。
            granted = await client.put(
                f"/api/security/roles/{role.role_id}/permissions",
                headers={"X-CSRF-Token": csrf},
                json={"version": data["version"], "permissions": ["menu.query", "menu.history"]},
            )
            assert granted.status_code == 200, granted.text
            granted_data = granted.json()["data"]
            assert granted_data["display_name"] == "営業閲覧(改名)"
            assert granted_data["permissions"] == ["menu.history", "menu.query"]
            assert_entitlement_preserved(granted_data)

    def assert_entitlement_preserved(data: dict[str, Any]) -> None:
        assert len(data["data_entitlements"]) == 1
        after = data["data_entitlements"][0]
        assert after["entitlement_id"] == before.entitlement_id
        assert after["target_owner"] == "SALES"
        assert after["target_object"] == "ORDERS"
        assert after["column_names"] == ["ORDER_ID", "REGION"]
        assert after["scope_mode"] == "FILTERS"
        assert after["scope_code"] == before.scope_code
        assert [item["column_name"] for item in after["scope_filters"]] == ["REGION"]
        assert after["data_grant_name"] == "NL2SQL_DG_TEST"
        assert after["sql_checksum"] == "checksum-test"
        assert after["apply_status"] == "APPLIED"

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_role_permission_change_reflects_on_existing_session_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    dynamic_role = service.create_role(
        role_code="DYNAMIC_RUNTIME_ROLE",
        display_name="動的反映ロール",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="dynamic.user",
        display_name="動的反映ユーザー",
        role_ids=[dynamic_role.role_id],
        password="DynamicUserPass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _login_api(client, "dynamic.user", "DynamicUserPass!123")
            assert (await client.get("/api/nl2sql/history")).status_code == 200
            assert (await client.get("/api/security/users")).status_code == 403

            service.update_role(
                dynamic_role.role_id,
                expected_version=dynamic_role.version,
                display_name=dynamic_role.display_name,
                description=dynamic_role.description,
                permissions={"menu.security_users"},
                actor=admin,
            )

            assert (await client.get("/api/security/users")).status_code == 200
            assert (await client.get("/api/nl2sql/history")).status_code == 403

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_archived_or_unassigned_role_removes_runtime_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    archived_runtime_role = service.create_role(
        role_code="ARCHIVED_RUNTIME_QUERY",
        display_name="アーカイブ反映",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    unassigned_runtime_role = service.create_role(
        role_code="UNASSIGNED_RUNTIME_QUERY",
        display_name="解除反映",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    archived_user = _create_active_user(
        service,
        admin,
        login_user_id="archived.runtime.user",
        display_name="アーカイブ反映ユーザー",
        role_ids=[archived_runtime_role.role_id],
        password="ArchivedRuntimePass!123",
    )
    unassigned_user = _create_active_user(
        service,
        admin,
        login_user_id="unassigned.runtime.user",
        display_name="解除反映ユーザー",
        role_ids=[unassigned_runtime_role.role_id],
        password="UnassignedRuntimePass!123",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _login_api(client, archived_user.login_user_id, "ArchivedRuntimePass!123")
            assert (await client.get("/api/nl2sql/history")).status_code == 200
            service.archive_role(
                archived_runtime_role.role_id,
                expected_version=archived_runtime_role.version,
                actor=admin,
            )
            assert (await client.get("/api/nl2sql/history")).status_code == 403
            me_after_archive = await client.get("/api/auth/me")
            assert me_after_archive.status_code == 200
            assert me_after_archive.json()["data"]["permissions"] == []

            await _login_api(client, unassigned_user.login_user_id, "UnassignedRuntimePass!123")
            assert (await client.get("/api/nl2sql/history")).status_code == 200
            service.update_user(
                unassigned_user.user_uuid,
                expected_version=unassigned_user.version,
                display_name=unassigned_user.display_name,
                status=unassigned_user.status,
                role_ids=[],
                actor=admin,
            )
            assert (await client.get("/api/nl2sql/history")).status_code == 403
            me_after_unassign = await client.get("/api/auth/me")
            assert me_after_unassign.status_code == 200
            assert me_after_unassign.json()["data"]["permissions"] == []

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_restore_role_reactivates_existing_session_and_assigned_role_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    admin_user = service.store.get_user(admin.user_uuid)
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )
    runtime_role = service.create_role(
        role_code="RESTORE_RUNTIME_QUERY",
        display_name="復元反映",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    assigned_user = _create_active_user(
        service,
        admin,
        login_user_id="restore.runtime.user",
        display_name="復元反映ユーザー",
        role_ids=[runtime_role.role_id],
        password="RestoreRuntimePass!123",
    )
    archived = service.archive_role(
        runtime_role.role_id,
        expected_version=runtime_role.version,
        actor=admin,
    )

    def assigned_role_archived(payload: dict[str, object]) -> bool:
        assert payload["role_ids"] == [runtime_role.role_id]
        assigned_roles = cast(list[dict[str, object]], payload["assigned_roles"])
        assert assigned_roles[0]["role_code"] == "RESTORE_RUNTIME_QUERY"
        return bool(assigned_roles[0]["archived"])

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as user_client,
            httpx.AsyncClient(transport=transport, base_url="http://test") as admin_client,
        ):
            await _login_api(user_client, assigned_user.login_user_id, "RestoreRuntimePass!123")
            assert (await user_client.get("/api/nl2sql/history")).status_code == 403
            before_me = await user_client.get("/api/auth/me")
            assert before_me.status_code == 200
            assert before_me.json()["data"]["permissions"] == []

            _, csrf = await _login_api(admin_client, "ADMIN", "BootstrapPass!123")
            before_users = await admin_client.get("/api/security/users")
            assert before_users.status_code == 200
            before_user = next(
                item
                for item in before_users.json()["data"]
                if item["user_uuid"] == assigned_user.user_uuid
            )
            assert assigned_role_archived(before_user) is True

            restored = await admin_client.post(
                f"/api/security/roles/{runtime_role.role_id}/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": archived.version},
            )
            assert restored.status_code == 200
            restored_data = restored.json()["data"]
            assert restored_data["archived"] is False
            assert restored_data["version"] == archived.version + 1

            after_users = await admin_client.get("/api/security/users")
            assert after_users.status_code == 200
            after_user = next(
                item
                for item in after_users.json()["data"]
                if item["user_uuid"] == assigned_user.user_uuid
            )
            assert assigned_role_archived(after_user) is False

            after_me = await user_client.get("/api/auth/me")
            assert after_me.status_code == 200
            assert after_me.json()["data"]["role_codes"] == ["RESTORE_RUNTIME_QUERY"]
            assert set(after_me.json()["data"]["permissions"]) == {
                "menu.query",
                FEEDBACK_WRITE_PERMISSION,
                PROFILE_READ_PERMISSION,
                QUERY_GENERATE_PERMISSION,
                SCHEMA_READ_PERMISSION,
                SQL_EXECUTE_PERMISSION,
            }
            assert (await user_client.get("/api/nl2sql/history")).status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_restore_role_api_rejects_invalid_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    admin_user = service.store.get_user(admin.user_uuid)
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )
    active_role = service.create_role(
        role_code="RESTORE_ACTIVE_TARGET",
        display_name="復元不要",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    archived_role = service.create_role(
        role_code="RESTORE_CONFLICT_TARGET",
        display_name="復元競合",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    archived = service.archive_role(
        archived_role.role_id,
        expected_version=archived_role.version,
        actor=admin,
    )
    system_role = service.store.get_role(SYSTEM_ADMIN_ROLE_ID)
    assert system_role is not None

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "ADMIN", "BootstrapPass!123")

            built_in = await client.post(
                f"/api/security/roles/{SYSTEM_ADMIN_ROLE_ID}/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": system_role.version},
            )
            assert built_in.status_code == 409

            missing = await client.post(
                "/api/security/roles/missing-role/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": 1},
            )
            assert missing.status_code == 404

            not_archived = await client.post(
                f"/api/security/roles/{active_role.role_id}/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": active_role.version},
            )
            assert not_archived.status_code == 409

            conflict = await client.post(
                f"/api/security/roles/{archived_role.role_id}/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": archived.version - 1},
            )
            assert conflict.status_code == 409

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_user_api_includes_archived_assigned_role_metadata_without_runtime_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    admin_user = service.store.get_user(admin.user_uuid)
    assert admin_user is not None
    service.store.set_password(
        admin_user.user_uuid,
        hash_password("BootstrapPass!123"),
        force_change=False,
    )
    archived_role = service.create_role(
        role_code="ARCHIVED_METADATA_ROLE",
        display_name="アーカイブ表示ロール",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    assigned_user = _create_active_user(
        service,
        admin,
        login_user_id="archived.metadata.user",
        display_name="アーカイブ表示ユーザー",
        role_ids=[archived_role.role_id],
        password="ArchivedMetadataPass!123",
    )
    service.archive_role(
        archived_role.role_id,
        expected_version=archived_role.version,
        actor=admin,
    )

    def assert_archived_assignment(payload: dict[str, object]) -> None:
        assert payload["role_ids"] == [archived_role.role_id]
        assert payload["assigned_roles"] == [
            {
                "role_id": archived_role.role_id,
                "role_code": "ARCHIVED_METADATA_ROLE",
                "display_name": "アーカイブ表示ロール",
                "is_built_in": False,
                "archived": True,
                "deleted": False,
            }
        ]

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "ADMIN", "BootstrapPass!123")
            users_response = await client.get("/api/security/users")
            assert users_response.status_code == 200
            user_rows = users_response.json()["data"]
            user_row = next(
                item for item in user_rows if item["user_uuid"] == assigned_user.user_uuid
            )
            assert_archived_assignment(user_row)

            detail = await client.get(f"/api/security/users/{assigned_user.user_uuid}")
            assert detail.status_code == 200
            assert_archived_assignment(detail.json()["data"])

            updated = await client.patch(
                f"/api/security/users/{assigned_user.user_uuid}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "version": assigned_user.version,
                    "display_name": "アーカイブ表示ユーザー更新",
                    "status": "ACTIVE",
                    "role_ids": [archived_role.role_id],
                },
            )
            assert updated.status_code == 200
            updated_user = updated.json()["data"]
            assert updated_user["display_name"] == "アーカイブ表示ユーザー更新"
            assert_archived_assignment(updated_user)

            disabled = await client.post(
                f"/api/security/users/{assigned_user.user_uuid}/disable",
                headers={"X-CSRF-Token": csrf},
                json={"version": updated_user["version"]},
            )
            assert disabled.status_code == 200
            assert_archived_assignment(disabled.json()["data"])

            enabled = await client.post(
                f"/api/security/users/{assigned_user.user_uuid}/enable",
                headers={"X-CSRF-Token": csrf},
                json={"version": disabled.json()["data"]["version"]},
            )
            assert enabled.status_code == 200
            assert_archived_assignment(enabled.json()["data"])

            reset = await client.post(
                f"/api/security/users/{assigned_user.user_uuid}/reset-password",
                headers={"X-CSRF-Token": csrf},
                json={"temporary_password": "ArchivedMetadataReset!123"},
            )
            assert reset.status_code == 200
            assert_archived_assignment(reset.json()["data"]["user"])

            current, _ = await _login_api(
                client,
                "archived.metadata.user",
                "ArchivedMetadataReset!123",
            )
            assert current["role_codes"] == []
            assert current["permissions"] == []

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


def test_user_data_marks_unresolved_assigned_role_as_inactive() -> None:
    user = UserRecord(
        user_uuid="missing-role-user",
        login_user_id="missing.role",
        display_name="ロール不明ユーザー",
        password_hash="unused",
        status="ACTIVE",
        force_password_change=False,
        failed_login_count=0,
        locked_until=None,
        version=1,
        role_ids=["missing-role-id"],
    )

    data = user_data(user)

    assert data.role_ids == ["missing-role-id"]
    assert data.assigned_roles[0].role_id == "missing-role-id"
    # 参照できないロールは名前の欄に ID を入れず、削除済みとして返す（#1682）。
    assert data.assigned_roles[0].display_name == ""
    assert data.assigned_roles[0].role_code == ""
    assert data.assigned_roles[0].deleted is True
    assert data.assigned_roles[0].archived is True


def test_history_api_scopes_items_to_actor_except_system_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_auth_cookie_secure", False)
    monkeypatch.setattr(settings, "oracle_user", "DBADMIN")
    monkeypatch.setattr(settings, "oracle_password", "DbAdminPass!123")
    monkeypatch.setattr(settings, "app_admin_login_user_id", "system_admin")
    monkeypatch.setattr(settings, "app_admin_login_user_password", "AppAdminPass123")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    _patch_security_threadpools(monkeypatch)
    reset_security_service()
    from app.security.service import get_security_service

    service = get_security_service()
    assert isinstance(service.store, InMemorySecurityStore)
    assert service.store.bootstrap(
        login_user_id="ADMIN",
        display_name="ADMIN（システム管理者）",
        password_hash=hash_password("BootstrapPass!123"),
    )

    history_service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", history_service)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/auth/login",
                json={"login_user_id": "ADMIN", "password": "BootstrapPass!123"},
            )
            assert login.status_code == 200
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            changed = await client.post(
                "/api/auth/password/change",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": "BootstrapPass!123",
                    "new_password": "IndependentPass!456",
                },
            )
            assert changed.status_code == 200
            admin_login = await client.post(
                "/api/auth/login",
                json={"login_user_id": "ADMIN", "password": "IndependentPass!456"},
            )
            assert admin_login.status_code == 200
            admin_user_id = admin_login.json()["data"]["user_uuid"]
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            role_id = await _create_role_api(
                client,
                csrf,
                role_code="HISTORY_OWNER_VIEWER",
                display_name="履歴閲覧",
                permissions=["menu.history"],
            )
            user_response = await client.post(
                "/api/security/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "login_user_id": "owner.viewer",
                    "display_name": "履歴所有者",
                    "temporary_password": "ViewerStart!123",
                    "role_ids": [role_id],
                },
            )
            assert user_response.status_code == 200
            viewer_user_id = user_response.json()["data"]["user"]["user_uuid"]

            history_service._history = [  # noqa: SLF001 - API scope regression fixture
                HistoryItem(
                    id="legacy-history",
                    question="所有者不明",
                    engine=Nl2SqlEngine.SELECT_AI,
                    generated_sql="SELECT 1 FROM DUAL",
                    created_at="2026-08-17T00:00:00+00:00",
                ),
                HistoryItem(
                    id="admin-history",
                    question="管理者履歴",
                    engine=Nl2SqlEngine.SELECT_AI,
                    generated_sql="SELECT 1 FROM DUAL",
                    created_at="2026-08-17T00:01:00+00:00",
                    actor_user_uuid=admin_user_id,
                ),
                HistoryItem(
                    id="viewer-history",
                    question="自分の履歴",
                    engine=Nl2SqlEngine.SELECT_AI,
                    generated_sql="SELECT 1 FROM DUAL",
                    created_at="2026-08-17T00:02:00+00:00",
                    actor_user_uuid=viewer_user_id,
                ),
                HistoryItem(
                    id="other-history",
                    question="他人の履歴",
                    engine=Nl2SqlEngine.SELECT_AI,
                    generated_sql="SELECT 1 FROM DUAL",
                    created_at="2026-08-17T00:03:00+00:00",
                    actor_user_uuid="other-user",
                ),
            ]

            admin_history = await client.get("/api/nl2sql/history")
            assert admin_history.status_code == 200
            assert [item["id"] for item in admin_history.json()["data"]["items"]] == [
                "other-history",
                "viewer-history",
                "admin-history",
                "legacy-history",
            ]
            await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

            await client.post(
                "/api/auth/login",
                json={"login_user_id": "owner.viewer", "password": "ViewerStart!123"},
            )
            csrf = client.cookies.get("pr_csrf")
            assert csrf
            viewer_changed = await client.post(
                "/api/auth/password/change",
                headers={"X-CSRF-Token": csrf},
                json={
                    "current_password": "ViewerStart!123",
                    "new_password": "ReadOnlyPass!456",
                },
            )
            assert viewer_changed.status_code == 200
            viewer_login = await client.post(
                "/api/auth/login",
                json={"login_user_id": "owner.viewer", "password": "ReadOnlyPass!456"},
            )
            assert viewer_login.status_code == 200
            scoped_history = await client.get("/api/nl2sql/history")
            assert scoped_history.status_code == 200
            assert [item["id"] for item in scoped_history.json()["data"]["items"]] == [
                "viewer-history"
            ]

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


@pytest.mark.parametrize("actor_kind", ["system_admin", "data_operator", "denied"])
def test_nested_synthetic_routes_use_authenticated_effective_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, actor_kind: str
) -> None:
    """実 app の login→多重 include router→RBAC を通し、principal 注入で迂回しない。"""
    from app.features.nl2sql import synthetic_router
    from app.features.nl2sql.service import nl2sql_service
    from app.features.nl2sql.synthetic_service import SyntheticService
    from app.features.nl2sql.synthetic_store import SyntheticStore

    _patch_app_admin_env(monkeypatch, tmp_path)
    security = _configure_memory_api_auth(monkeypatch)
    synthetic = SyntheticService(get_settings(), store=SyntheticStore())
    monkeypatch.setattr(synthetic_router, "get_synthetic_service", lambda: synthetic)
    monkeypatch.setattr(nl2sql_service, "ensure_persistence_available", lambda: None)
    login_id, password = "system_admin", "AppAdminPass123"
    if actor_kind != "system_admin":
        admin, _, _ = security.login(login_id, password)
        role = security.create_role(
            role_code="SYNTHETIC_READER",
            display_name="生成状態参照",
            description="",
            permissions=(
                {"menu.data_management"} if actor_kind == "data_operator" else {"menu.query"}
            ),
            entitlements=[],
            actor=admin,
        )
        login_id, password = "synthetic-reader", "SyntheticReaderPass!123"
        _create_active_user(
            security,
            admin,
            login_user_id=login_id,
            display_name="生成状態参照",
            role_ids=[role.role_id],
            password=password,
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            current, csrf = await _login_api(client, login_id, password)
            assert (await client.get("/api/auth/me")).status_code == 200
            if actor_kind == "system_admin":
                assert current["is_system_admin"] is True
            response = await client.get("/api/nl2sql/synthetic-data/runs")
            assert response.status_code == (403 if actor_kind == "denied" else 200), response.text
            if actor_kind != "denied":
                assert response.json()["data"] == []
            # 有効な role でも確認語の検証は省略しない。未認可は domain mutation 前に拒否。
            response = await client.post(
                "/api/nl2sql/synthetic-data/runs",
                headers={"X-CSRF-Token": csrf},
                json={
                    "table_name": "APP.T",
                    "profile_name": "P",
                    "idempotency_key": "nested-route-key-001",
                },
            )
            assert response.status_code == (403 if actor_kind == "denied" else 400), response.text

    asyncio.run(exercise())


def test_db_admin_chat_requires_its_own_menu_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 管理チャット（#2264）は menu.db_admin_chat だけで通し、管理 SQL を実行の権限では通さない。

    画面の API（Cookie / CSRF）だけで、権限の無い利用者は API でも拒否する。権限管理の画面から
    自分の持たない menu.db_admin_chat を付与することもできない（越権の防止）。
    """

    import importlib

    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore
    from app.security.request_actor import actor_scope

    for route in ("generate", "execute"):
        assert permission_for_route("POST", f"/nl2sql/db-admin-chat/{route}") == frozenset(
            {"menu.db_admin_chat"}
        )
    # 会話の履歴（#2319）も同じメニュー権限だけ。
    conversation = "/nl2sql/db-admin-chat/conversations/{conversation_id}"
    for method, path in (
        ("GET", "/nl2sql/db-admin-chat/conversations"),
        ("GET", conversation),
        ("PATCH", conversation),
        ("DELETE", conversation),
        ("PUT", f"{conversation}/turns/{{turn_id}}/plan"),
        ("POST", f"{conversation}/turns/{{turn_id}}/analyze-error"),
    ):
        assert permission_for_route(method, path) == frozenset({"menu.db_admin_chat"})
    security = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = _login(security)
    nl2sql = Nl2SqlService(store=MemoryNl2SqlStore())
    router = importlib.import_module("app.features.nl2sql.router")
    monkeypatch.setattr(router, "nl2sql_service", nl2sql)
    monkeypatch.setattr(nl2sql, "_use_oracle_runtime", lambda: False)
    monkeypatch.setattr(nl2sql, "_current_schema_owner", lambda: "APP")

    async def exercise() -> None:
        for index, permissions in enumerate(
            [
                {"menu.admin_sql"},
                {"menu.db_admin_chat"},
                {"menu.security_permissions", "menu.security_roles", "menu.admin_sql"},
            ]
        ):
            role = security.create_role(
                role_code=f"DBCHAT_{index}",
                display_name=f"DB 管理チャットのテスト {index}",
                description="操作権限テスト",
                permissions=permissions,
                entitlements=[],
                actor=admin,
            )
            user = _create_active_user(
                security,
                admin,
                login_user_id=f"dbchat_{index}",
                display_name=f"dbchat {index}",
                role_ids=[role.role_id],
                password="DbChatPass!123",
            )
            allowed = "menu.db_admin_chat" in permissions
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                _, csrf = await _login_api(client, f"dbchat_{index}", "DbChatPass!123")
                headers = {"X-CSRF-Token": csrf}
                for route, body, allowed_status in (
                    # 無い会話のターンの実行は、権限があれば 404（存在を漏らさない）。
                    (
                        "execute",
                        {"conversation_id": "missing", "turn_id": "missing", "statement_index": 1},
                        404,
                    ),
                    (
                        "conversations/missing/turns/missing/analyze-error",
                        {"statement_index": 1},
                        404,
                    ),
                ):
                    response = await client.post(
                        f"/api/nl2sql/db-admin-chat/{route}", headers=headers, json=body
                    )
                    assert response.status_code == (allowed_status if allowed else 403), (
                        permissions,
                        route,
                        response.text,
                    )
                listed = await client.get(
                    "/api/nl2sql/db-admin-chat/conversations", headers=headers
                )
                assert listed.status_code == (200 if allowed else 403), listed.text
                # API の判定を通さないサービスの直呼びも同じ判定になる。
                with actor_scope(user.user_uuid):
                    if allowed:
                        nl2sql.list_db_admin_chats(actor=user.user_uuid, all_users=False)
                    else:
                        with pytest.raises(SecurityApiError) as denied:
                            nl2sql.list_db_admin_chats(actor=user.user_uuid, all_users=False)
                        assert denied.value.status_code == 403
                if "menu.security_permissions" in permissions:
                    # 自分の持たない menu.db_admin_chat は、自分のロールにも付与できない。
                    current = security.store.get_role(role.role_id)
                    assert current is not None
                    granted = await client.put(
                        f"/api/security/roles/{role.role_id}/permissions",
                        headers=headers,
                        json={
                            "version": current.version,
                            "permissions": sorted({*permissions, "menu.db_admin_chat"}),
                        },
                    )
                    assert granted.status_code == 403, granted.text

    asyncio.run(exercise())


def test_data_preparation_actions_enforce_policy_and_revalidate_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import importlib

    from app.features.nl2sql.models import (
        DbAdminExecuteData,
        DbAdminStatementsRequest,
        TimingEnvelope,
    )
    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore
    from app.security.request_actor import actor_scope

    security = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = _login(security)
    nl2sql = Nl2SqlService(store=MemoryNl2SqlStore())
    router = importlib.import_module("app.features.nl2sql.router")
    monkeypatch.setattr(router, "nl2sql_service", nl2sql)
    monkeypatch.setattr(nl2sql, "_use_oracle_runtime", lambda: False)
    monkeypatch.setattr(nl2sql, "_current_schema_owner", lambda: "APP")
    monkeypatch.setattr(nl2sql, "_db_admin_truncate_target_type", lambda *_: "table")
    executed: list[str] = []

    def execute(request: object) -> DbAdminExecuteData:
        executed.append(str(getattr(request, "policy", "admin_sql")))
        return DbAdminExecuteData(
            executed=True,
            runtime="deterministic",
            timing=TimingEnvelope(created_at="2026-09-11T00:00:00Z", elapsed_ms=0),
        )

    monkeypatch.setattr(nl2sql, "_execute_db_admin_statements", execute, raising=False)
    monkeypatch.setattr(nl2sql, "_execute_db_admin_sql", execute, raising=False)
    policies = {
        "table_ddl": "table_management",
        "view_ddl": "view_management",
        "data_dml": "data_management",
        "comment_sql": "comment_management",
        "annotation_sql": "annotation_management",
        "domain_sql": "domain_management",
    }

    async def exercise() -> None:
        for index, menu in enumerate([*policies.values(), "admin_sql", "history"]):
            role = security.create_role(
                role_code=f"PREP_{index}",
                display_name=menu,
                description="操作権限テスト",
                permissions={f"menu.{menu}"},
                entitlements=[],
                actor=admin,
            )
            user = _create_active_user(
                security,
                admin,
                login_user_id=f"prep_{index}",
                display_name=menu,
                role_ids=[role.role_id],
                password="PreparePass!123",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                _, csrf = await _login_api(client, f"prep_{index}", "PreparePass!123")
                headers = {"X-CSRF-Token": csrf}
                for policy, required_menu in policies.items():
                    allowed = menu in {required_menu, "admin_sql"}
                    before = len(executed)
                    response = await client.post(
                        "/api/nl2sql/db-admin/statements",
                        headers=headers,
                        json={
                            "policy": policy,
                            "sql": "SELECT 1 FROM DUAL",
                            "confirmation": "ADMIN_EXECUTE",
                        },
                    )
                    assert response.status_code == (200 if allowed else 403), (
                        menu,
                        policy,
                        response.text,
                    )
                    assert len(executed) == before + int(allowed)
                    # API の coarse check を通さない domain 直呼びも同じ判定になる。
                    with actor_scope(user.user_uuid):
                        request = DbAdminStatementsRequest(policy=policy, sql="SELECT 1 FROM DUAL")
                        if allowed:
                            nl2sql.execute_db_admin_statements(request)
                        else:
                            with pytest.raises(SecurityApiError) as denied:
                                nl2sql.execute_db_admin_statements(request)
                            assert denied.value.status_code == 403
                before = len(executed)
                response = await client.post(
                    "/api/nl2sql/db-admin/execute",
                    headers=headers,
                    json={"sql": "SELECT 1 FROM DUAL"},
                )
                assert response.status_code == (200 if menu == "admin_sql" else 403)
                assert len(executed) == before + int(menu == "admin_sql")
                for mode in ["create", "replace", "append", "truncate"]:
                    allowed = menu == "data_management" or (
                        menu == "table_management" and mode == "create"
                    )
                    response = await client.post(
                        "/api/nl2sql/db-admin/import-tabular",
                        headers=headers,
                        json={
                            "table_name": "REVIEW_TEST",
                            "filename": "test.csv",
                            "mode": mode,
                            "content_base64": base64.b64encode(b"ID\n1\n").decode(),
                            "confirmation": "ADMIN_EXECUTE",
                        },
                    )
                    assert response.status_code == (200 if allowed else 403), (
                        menu,
                        mode,
                        response.text,
                    )
                before = len(executed)
                response = await client.post(
                    "/api/nl2sql/db-admin/truncate-table",
                    headers=headers,
                    json={
                        "table_name": "REVIEW_TEST",
                        "owner": "APP",
                        "confirmation": "APP.REVIEW_TEST",
                    },
                )
                allowed = menu in {"table_management", "data_management"}
                assert response.status_code == (200 if allowed else 403), (menu, response.text)
                assert len(executed) == before + int(allowed)
                # JOIN/WHERE 条件抽出はビュー管理の画面だけが使うので、画面と同じ権限（#934）。
                response = await client.post(
                    "/api/nl2sql/db-admin/extract-join-where",
                    headers=headers,
                    json={
                        "ddl": "CREATE VIEW V AS SELECT A.ID FROM A JOIN B ON A.ID = B.ID",
                    },
                )
                assert response.status_code == (200 if menu == "view_management" else 403), (
                    menu,
                    response.text,
                )
                if menu == "comment_management":
                    # 同じ cookie / actor でもロール削除は次の実行から拒否する。
                    security.update_role(
                        role.role_id,
                        expected_version=role.version,
                        display_name=role.display_name,
                        description=role.description,
                        permissions={"menu.history"},
                        allowed_profile_ids=role.allowed_profile_ids,
                        actor=admin,
                    )
                    revoked = await client.post(
                        "/api/nl2sql/db-admin/statements",
                        headers=headers,
                        json={"policy": "comment_sql", "sql": "COMMENT ON TABLE X IS 'x'"},
                    )
                    assert revoked.status_code == 403
                    with actor_scope(user.user_uuid):
                        with pytest.raises(SecurityApiError) as denied:
                            nl2sql.execute_db_admin_statements(
                                DbAdminStatementsRequest(
                                    policy="comment_sql", sql="COMMENT ON TABLE X IS 'x'"
                                )
                            )
                        assert denied.value.status_code == 403

    asyncio.run(exercise())


@pytest.mark.parametrize("assigned_to_actor", [True, False])
def test_role_restore_api_rejects_permission_escalation(
    monkeypatch: pytest.MonkeyPatch, assigned_to_actor: bool
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    manager = service.create_role(
        role_code="RESTORE_REVIEW_MANAGER",
        display_name="ロール管理",
        description="",
        permissions={"menu.security_roles"},
        entitlements=[],
        actor=admin,
    )
    target = service.create_role(
        role_code="RESTORE_REVIEW_TARGET",
        display_name="検索権限",
        description="",
        permissions={"menu.query"},
        entitlements=[],
        actor=admin,
    )
    actor_user = _create_active_user(
        service,
        admin,
        login_user_id="restore.review.manager",
        display_name="管理者",
        role_ids=[manager.role_id, target.role_id] if assigned_to_actor else [manager.role_id],
        password="ReviewManager!123",
    )
    other = _create_active_user(
        service,
        admin,
        login_user_id="restore.review.other",
        display_name="別ユーザー",
        role_ids=[target.role_id],
        password="ReviewOther!123",
    )
    archived = service.archive_role(target.role_id, expected_version=target.version, actor=admin)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "restore.review.manager", "ReviewManager!123")
            response = await client.post(
                f"/api/security/roles/{target.role_id}/restore",
                headers={"X-CSRF-Token": csrf},
                json={"version": archived.version},
            )
            assert response.status_code == 403
            assert service.store.get_role(target.role_id) == archived
            assert (await client.get("/api/nl2sql/history")).status_code == 403
            for user in [actor_user, other]:
                assert not service.principal_for_worker(user.user_uuid).has_permission("menu.query")
            restored = service.restore_role(
                target.role_id, expected_version=archived.version, actor=admin
            )
            assert not restored.archived
            assert service.principal_for_worker(other.user_uuid).has_permission("menu.query")

    try:
        asyncio.run(exercise())
    finally:
        reset_security_service()


@pytest.mark.parametrize("store_kind", ["memory", "oracle"])
@pytest.mark.parametrize("target_kind", ["implied", "profile", "allowed"])
def test_role_restore_validates_access_before_store_write(
    monkeypatch: pytest.MonkeyPatch, store_kind: str, target_kind: str
) -> None:
    from dataclasses import replace

    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    manager = service.create_role(
        role_code="RESTORE_LIMITED",
        display_name="管理",
        description="",
        permissions={"menu.security_roles", "menu.query"},
        entitlements=[],
        actor=admin,
    )
    user = _create_active_user(
        service,
        admin,
        login_user_id="restore.limited",
        display_name="管理",
        role_ids=[manager.role_id],
        password="ReviewManager!123",
    )
    actor = service.principal_for_worker(user.user_uuid)
    target = replace(
        manager,
        role_id="restore-target",
        archived=True,
        permissions={"menu.profiles"} if target_kind == "implied" else {"menu.query"},
        allowed_profile_ids={"profile-review"} if target_kind == "profile" else set(),
    )
    store = service.store if store_kind == "memory" else OracleSecurityStore(_settings())
    monkeypatch.setattr(store, "get_role", lambda _id: target)
    # 他製品の権限テーブル（製品をまたぐ権限昇格の確認。#212）は空として扱う。
    monkeypatch.setattr(
        store, "role_permission_codes", lambda role_ids, *, tables: {t: {} for t in tables}
    )
    writes: list[str] = []

    def restore(role_id: str, *, expected_version: int) -> RoleRecord:
        writes.append(role_id)
        return replace(target, archived=False, version=expected_version + 1)

    monkeypatch.setattr(store, "restore_role", restore)
    service = SecurityService(store, service.settings)
    if target_kind == "allowed":
        assert not service.restore_role(
            target.role_id, expected_version=target.version, actor=actor
        ).archived
        assert writes == [target.role_id]
    else:
        with pytest.raises(SecurityApiError) as error:
            service.restore_role(target.role_id, expected_version=target.version, actor=actor)
        assert error.value.status_code == 403
        assert writes == []
        assert store.get_role(target.role_id) == target


@pytest.mark.parametrize(
    "permission, expected_read", [("menu.settings_oci", 200), ("menu.settings_appearance", 403)]
)
def test_oci_settings_can_read_shared_storage_without_write_permission(
    monkeypatch: pytest.MonkeyPatch, permission: str, expected_read: int
) -> None:
    service = _configure_memory_api_auth(monkeypatch)
    admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
    role = service.create_role(
        role_code="SETTINGS_REVIEW",
        display_name="設定レビュー",
        description="",
        permissions={permission},
        entitlements=[],
        actor=admin,
    )
    _create_active_user(
        service,
        admin,
        login_user_id="settings.review",
        display_name="設定担当",
        role_ids=[role.role_id],
        password="SettingsReview!123",
    )
    before = get_settings().model_dump()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            _, csrf = await _login_api(client, "settings.review", "SettingsReview!123")
            assert (await client.get("/api/settings/upload-storage")).status_code == expected_read
            response = await client.patch(
                "/api/settings/upload-storage",
                headers={"X-CSRF-Token": csrf},
                json={
                    "backend": "local",
                    "local_storage_dir": "/tmp/forbidden-review",
                    "object_storage_bucket": "review",
                },
            )
            assert response.status_code == 403
            assert get_settings().model_dump() == before

    asyncio.run(exercise())


def test_profile_access_profiles_search_and_page_on_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """権限管理の業務プロファイルの候補は `q` / `limit` / `offset` / `ids` で絞る（#608）。"""
    from app.features.nl2sql import service as nl2sql_service_module
    from app.security import router as security_router_module

    feature_service = Nl2SqlService(store=MemoryNl2SqlStore())
    for index in range(1, 6):
        feature_service.create_profile(
            Nl2SqlProfile(
                id=f"p{index}",
                name=f"検索試験 {index}" if index <= 3 else f"財務試験 {index}",
                category="試験会計" if index == 5 else "試験営業",
                description="",
            )
        )
    monkeypatch.setattr(nl2sql_service_module, "nl2sql_service", feature_service)
    monkeypatch.setattr(security_router_module, "get_security_service", _service)

    def ids_of(**kwargs: Any) -> tuple[list[str], int, bool]:
        params: dict[str, Any] = {"include_archived": False, "q": "", "ids": None, **kwargs}
        paging = OffsetParams(limit=params.pop("limit", 50), offset=params.pop("offset", 0))
        page = security_router_module.list_profile_access_profiles(paging, **params).data
        assert page is not None
        return [item.id for item in page.items if item.id != "default"], page.total, page.has_next

    assert ids_of(q="検索試験", limit=2) == (["p1", "p2"], 3, True)
    assert ids_of(q="検索試験", limit=2, offset=2) == (["p3"], 3, False)
    # カテゴリも照合する。
    assert ids_of(q="試験会計")[0] == ["p5"]
    # 選択済みの名前の解決（ids）は検索語と関係なく ID で引く。
    assert ids_of(ids=["p4", "p2", "missing"])[0] == ["p2", "p4"]


def test_profile_access_profiles_return_role_names_not_only_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """業務プロファイルを使えるロールは、ロール名も返す（画面に UUID を出さない。#1682）。"""
    from app.features.nl2sql import service as nl2sql_service_module
    from app.security import router as security_router_module

    feature_service = Nl2SqlService(store=MemoryNl2SqlStore())
    for profile_id in ("p1", "p2"):
        feature_service.create_profile(
            Nl2SqlProfile(
                id=profile_id, name=f"ロール表示 {profile_id}", category="", description=""
            )
        )
    security = _service()
    actor, _, _ = _login(security)
    role = security.create_role(
        role_code="PROFILE_P1_USER",
        display_name="P1 の利用者",
        description="",
        allowed_profile_ids={"p1"},
        actor=actor,
    )
    monkeypatch.setattr(nl2sql_service_module, "nl2sql_service", feature_service)
    monkeypatch.setattr(security_router_module, "get_security_service", lambda: security)

    page = security_router_module.list_profile_access_profiles(
        OffsetParams(limit=50, offset=0), include_archived=False, q="", ids=["p1", "p2"]
    ).data
    assert page is not None
    allowed = {item.id: item.allowed_roles for item in page.items}
    p1 = {item.role_code: item for item in allowed["p1"]}
    p2 = {item.role_code: item for item in allowed["p2"]}
    # 構成の system admin はすべての業務プロファイルを使え、ロール名を添えて返る。
    assert SYSTEM_ADMIN_ROLE_CODE in p1 and SYSTEM_ADMIN_ROLE_CODE in p2
    assert p1[SYSTEM_ADMIN_ROLE_CODE].display_name
    assert p1["PROFILE_P1_USER"].display_name == "P1 の利用者"
    assert p1["PROFILE_P1_USER"].role_id == role.role_id
    assert "PROFILE_P1_USER" not in p2
    names = [item.display_name for item in allowed["p1"]]
    assert names == sorted(names)
    for roles in allowed.values():
        for item in roles:
            assert item.display_name and item.display_name != item.role_id


@pytest.mark.parametrize(
    "menu", ["menu.comment_management", "menu.annotation_management", "menu.domain_management"]
)
def test_metadata_menus_imply_schema_read_and_refresh(menu: str) -> None:
    """画面の「スキーマを更新」と実行後の job の追跡の API を、同じメニュー権限で通す（#972）。"""
    effective = expand_permissions({menu})
    assert {SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION} <= effective
    for method, path in (
        ("POST", "/schema/refresh-jobs"),
        ("GET", "/schema/refresh-jobs/{job_id}"),
        ("GET", "/schema/refresh-jobs/active"),
    ):
        allowed = permission_for_route(method, path)
        assert allowed is not None, (method, path)
        assert allowed & effective, (method, path)
    # テーブル・ビュー・データの管理の操作権限は含めない。
    assert "menu.table_management" not in effective
    assert "menu.admin_sql" not in effective


def test_user_manager_cannot_assign_role_with_profiles_or_data_grants_outside_own_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """権限が収まっても、自分が使えない業務プロファイル・Data Grant を含むロールは割り当てられない。

    #1090。
    """
    service = _configure_memory_api_auth(monkeypatch)
    try:
        admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
        manager_role = service.create_role(
            role_code="SCOPED_USER_MANAGER",
            display_name="ユーザー管理（P1 と SALES）",
            description="",
            permissions={"menu.security_users", "menu.query"},
            entitlements=[("HR.EMPLOYEES", "SALES", "SELECT")],
            allowed_profile_ids={"P1"},
            actor=admin,
        )
        other_profile_role = service.create_role(
            role_code="QUERY_P2",
            display_name="P2 の SQL 生成",
            description="",
            permissions={"menu.query"},
            entitlements=[],
            allowed_profile_ids={"P2"},
            actor=admin,
        )
        other_grant_role = service.create_role(
            role_code="QUERY_HR_GRANT",
            display_name="HR の Data Grant",
            description="",
            permissions={"menu.query"},
            entitlements=[("HR.EMPLOYEES", "HR", "SELECT")],
            actor=admin,
        )
        subset_role = service.create_role(
            role_code="QUERY_P1_SALES",
            display_name="P1 と SALES の SQL 生成",
            description="",
            permissions={"menu.query"},
            entitlements=[("HR.EMPLOYEES", "SALES", "SELECT")],
            allowed_profile_ids={"P1"},
            actor=admin,
        )
        manager_user = _create_active_user(
            service,
            admin,
            login_user_id="scoped.manager",
            display_name="範囲付きのユーザー管理者",
            role_ids=[manager_role.role_id],
            password="ScopedManagerPass!123",
        )
        manager, _, _ = service.login("scoped.manager", "ScopedManagerPass!123")
        assert manager.allowed_profile_ids == {"P1"}

        # 自分自身への割り当て（権限の昇格）を拒否する。新しいユーザーへの割り当ても同じ。
        for role in (other_profile_role, other_grant_role):
            with pytest.raises(SecurityApiError, match="割り当てる権限がありません") as error:
                service.update_user(
                    manager_user.user_uuid,
                    expected_version=manager_user.version,
                    display_name=manager_user.display_name,
                    status="ACTIVE",
                    role_ids=[manager_role.role_id, role.role_id],
                    actor=manager,
                )
            assert error.value.status_code == 403
            with pytest.raises(SecurityApiError, match="割り当てる権限がありません"):
                service.create_user(
                    login_user_id=f"new.{role.role_code.lower()}",
                    display_name="新しいユーザー",
                    role_ids=[role.role_id],
                    temporary_password="NewUserStartPass!123",
                    actor=manager,
                )
        again, _, _ = service.login("scoped.manager", "ScopedManagerPass!123")
        assert again.allowed_profile_ids == {"P1"}
        assert {item.scope_code for item in again.data_entitlements} == {"SALES"}

        # 範囲外のロールは割り当ての候補にも出さない。範囲内のロールは今までどおり割り当てられる。
        candidate_codes = {role.role_code for role in service.list_roles_for_actor(manager)}
        assert "QUERY_P2" not in candidate_codes
        assert "QUERY_HR_GRANT" not in candidate_codes
        assert "QUERY_P1_SALES" in candidate_codes
        created, _ = service.create_user(
            login_user_id="subset.user",
            display_name="範囲内の利用者",
            role_ids=[subset_role.role_id],
            temporary_password="SubsetStartPass!123",
            actor=manager,
        )
        assert created.role_ids == [subset_role.role_id]
    finally:
        reset_security_service()
