"""共通認証基盤（#212）。"""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from pr_system_settings.auth import service as auth_service
from pr_system_settings.auth.dependencies import (
    authorize_request,
    permission_route_path,
    service_token_profile_scope,
)
from pr_system_settings.auth.domain import (
    CONFIGURED_SYSTEM_ADMIN_USER_UUID,
    SYSTEM_ADMIN_ROLE_ID,
    Principal,
    RoleRecord,
)
from pr_system_settings.auth.errors import (
    ROUTE_FORBIDDEN_CODE,
    ROUTE_FORBIDDEN_CODES,
    ROUTE_UNCLASSIFIED_CODE,
    SecurityApiError,
)
from pr_system_settings.auth.migrations import (
    PLATFORM_AUTH_DDL,
    PLATFORM_AUTH_DDL_IGNORED_ERRORS,
    apply_platform_auth_schema,
)
from pr_system_settings.auth.service import AuthService
from pr_system_settings.auth.service_token import (
    PROFILE_SCOPE_CLAIM,
    _segment,
    _signature,
    issue_service_token,
    narrow_to_profile_scope,
    profile_scope_from_claims,
    verify_service_token,
)
from pr_system_settings.auth.store import (
    PRODUCT_ROLE_PERMISSION_TABLES,
    InMemoryAuthStore,
    OracleAuthStore,
)

ADMIN_PASSWORD = "BootstrapPass!123"
CONFIGURED_PASSWORD = "Configured123A"  # nosec B105
SERVICE_SECRET = "x" * 40  # nosec B105 - テスト用の署名鍵


@dataclass
class _Settings:
    service_name: str = "platform-test"
    app_admin_login_user_id: str = "system_admin"
    app_admin_login_user_password: str = CONFIGURED_PASSWORD
    app_auth_absolute_timeout_hours: int = 12
    app_auth_idle_timeout_minutes: int = 60
    app_auth_failed_login_limit: int = 3
    app_auth_lockout_minutes: int = 15
    app_auth_login_attempt_limit: int = 5
    app_auth_login_ip_attempt_limit: int = 20
    app_auth_login_attempt_window_minutes: int = 15
    app_auth_password_min_length: int = 12
    app_auth_password_max_length: int = 128
    # テストを速くするため最小のパラメータにする。
    app_auth_argon2_time_cost: int = 1
    app_auth_argon2_memory_kib: int = 8
    app_auth_argon2_parallelism: int = 1
    app_service_token_secret: str = SERVICE_SECRET


class _ProductService(AuthService):
    """権限を InMemory の product_role_permissions から読む最小の製品（RAG を想定）。"""

    product_key = "rag"
    role_catalog_permissions = frozenset({"menu.security_roles"})

    def all_permissions(self) -> set[str]:
        return {"menu.security_users", "menu.security_roles", "menu.search"}

    def _role_permissions(self, roles: object) -> set[str]:
        store = self.store
        assert isinstance(store, InMemoryAuthStore)
        table = PRODUCT_ROLE_PERMISSION_TABLES["rag"]
        return {
            code
            for role in roles  # type: ignore[attr-defined]
            for code in store.product_role_permissions.get(table, {}).get(role.role_id, set())
        }

    def _role_within_actor(self, actor: Principal, role: RoleRecord) -> bool:
        return self._role_permissions([role]) <= actor.permissions


def _service() -> tuple[_ProductService, InMemoryAuthStore]:
    store = InMemoryAuthStore()
    service = _ProductService(store, _Settings())
    store.bootstrap(
        login_user_id="ADMIN",
        display_name="初期管理者",
        password_hash=service._hash_password(ADMIN_PASSWORD),
    )
    admin = store.get_user_by_login_user_id("admin")
    assert admin is not None
    store.set_password(admin.user_uuid, service._hash_password(ADMIN_PASSWORD), force_change=False)
    return service, store


def _grant(store: InMemoryAuthStore, product: str, role_id: str, *codes: str) -> None:
    table = PRODUCT_ROLE_PERMISSION_TABLES[product]
    store.product_role_permissions.setdefault(table, {})[role_id] = set(codes)


def _admin(service: _ProductService) -> Principal:
    principal, _, _ = service.login("ADMIN", ADMIN_PASSWORD)
    return principal


def test_login_creates_session_and_authenticates_with_role_permissions() -> None:
    service, _ = _service()
    principal, token, csrf = service.login("admin", ADMIN_PASSWORD)
    assert principal.is_system_admin
    assert principal.role_ids == [SYSTEM_ADMIN_ROLE_ID]
    # 画面のアカウント欄に出すロールの表示名（#1644）。
    assert principal.role_display_names == ["システム管理者"]
    assert principal.role_names() == ["システム管理者"]
    again = service.authenticate_session(token)
    assert again.user_uuid == principal.user_uuid
    service.verify_csrf(again, csrf, csrf)
    with pytest.raises(SecurityApiError) as exc:
        service.verify_csrf(again, csrf, "other")
    assert exc.value.status_code == 403


def test_login_locks_after_failed_attempts() -> None:
    service, store = _service()
    for _ in range(3):
        with pytest.raises(SecurityApiError) as exc:
            service.login("ADMIN", "WrongPassword!123")
        assert exc.value.status_code == 401
    user = store.get_user_by_login_user_id("admin")
    assert user is not None and user.locked_until is not None
    # ロック中は正しいパスワードでも拒否する。
    with pytest.raises(SecurityApiError):
        service.login("ADMIN", ADMIN_PASSWORD)


def test_session_expires_on_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _service()
    _, token, _ = service.login("ADMIN", ADMIN_PASSWORD)
    later = datetime.now(UTC) + timedelta(minutes=61)
    monkeypatch.setattr(auth_service, "_now", lambda: later)
    with pytest.raises(SecurityApiError) as exc:
        service.authenticate_session(token)
    assert "有効期限" in exc.value.public_message


def test_configured_system_admin_uses_signed_token_without_tables() -> None:
    service, _ = _service()
    principal, token, _ = service.login("system_admin", CONFIGURED_PASSWORD)
    assert principal.user_uuid == CONFIGURED_SYSTEM_ADMIN_USER_UUID
    assert principal.permissions == service.all_permissions()
    # 表示名は名前だけ（ロールを括弧で連結しない）。ロールは表示名で別に返す（#1644）。
    # 表示名は「システム管理者」、ログイン ID は system_admin（サイドナビの 1 行目。#1696）。
    assert principal.display_name == "システム管理者"
    assert principal.login_user_id == "system_admin"
    assert principal.role_names() == ["システム管理者"]
    assert token.startswith(service.configured_admin_token_prefix + ".")
    assert service.authenticate_session(token).user_uuid == CONFIGURED_SYSTEM_ADMIN_USER_UUID
    # 大小文字だけ違う login ID は DB ユーザーへもフォールバックしない。
    with pytest.raises(SecurityApiError):
        service.login("SYSTEM_ADMIN", CONFIGURED_PASSWORD)
    # 別の接頭辞の token は受け付けない。
    other = token.replace(service.configured_admin_token_prefix, "other-prefix", 1)
    with pytest.raises(SecurityApiError):
        service.authenticate_session(other)


def test_principal_role_names_fall_back_to_code_without_display_name() -> None:
    """表示名の無い principal（local debug など）は、SYSTEM_ADMIN は既定の表示名、ほかは code。"""
    principal = Principal(
        user_uuid="u",
        login_user_id="local",
        display_name="ローカル利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["RAG_EDITOR", "SYSTEM_ADMIN"],
        permissions=set(),
        session_id="s",
        csrf_token_hash="",  # nosec B106 - テストの値
    )
    assert principal.role_names() == ["RAG_EDITOR", "システム管理者"]
    principal.role_display_names = ["編集者", ""]
    assert principal.role_names() == ["編集者", "システム管理者"]


class _OtherProductService(_ProductService):
    product_key = "nl2sql"


def test_configured_system_admin_token_is_shared_across_products() -> None:
    """構成管理者の token は 3 製品で共通（接頭辞と署名鍵が製品に依存しない。#1565）。"""
    service, _ = _service()
    other = _OtherProductService(InMemoryAuthStore(), _Settings(service_name="other-product"))
    assert service.configured_admin_token_prefix == "platform-system-admin-v1"
    assert other.configured_admin_token_prefix == service.configured_admin_token_prefix
    _, token, _ = service.login("system_admin", CONFIGURED_PASSWORD)
    principal = other.authenticate_session(token)
    assert principal.user_uuid == CONFIGURED_SYSTEM_ADMIN_USER_UUID
    assert principal.is_system_admin
    # パスワードが違えば（= 鍵が違えば）受け付けない。
    changed = _OtherProductService(
        InMemoryAuthStore(), _Settings(app_admin_login_user_password="Another1234AB")
    )
    with pytest.raises(SecurityApiError):
        changed.authenticate_session(token)


def test_configured_system_admin_token_key_is_derived_with_pbkdf2() -> None:
    """署名鍵はパスワードの SHA-256 1 回ではなく PBKDF2-HMAC-SHA256 で導出する（#1646）。"""
    import hashlib

    service, _ = _service()
    key = service._configured_system_admin_token_key()
    assert key == hashlib.pbkdf2_hmac(
        "sha256",
        CONFIGURED_PASSWORD.encode("utf-8"),
        f"{service.configured_admin_token_prefix}:system_admin".encode(),
        auth_service.CONFIGURED_ADMIN_TOKEN_KEY_ITERATIONS,
    )
    assert auth_service.CONFIGURED_ADMIN_TOKEN_KEY_ITERATIONS >= 600_000
    plain = f"{service.configured_admin_token_prefix}:system_admin:{CONFIGURED_PASSWORD}"
    assert key != hashlib.sha256(plain.encode("utf-8")).digest()
    # 同じ資格情報の鍵はプロセス内で再利用する（token の検証のたびに導出しない）。
    started = time.perf_counter()
    for _ in range(20):
        assert service._configured_system_admin_token_key() == key
    assert time.perf_counter() - started < 1.0


def test_revoke_session_token_revokes_only_database_sessions() -> None:
    """ログインのときに、ブラウザが持っていた古いセッションを失効させる（#1565）。"""
    service, _ = _service()
    _, token, _ = service.login("ADMIN", ADMIN_PASSWORD)
    service.revoke_session_token(token)
    with pytest.raises(SecurityApiError) as exc:
        service.authenticate_session(token)
    assert exc.value.status_code == 401
    # 構成管理者の token・空・不明な token では何もしない（例外を出さない）。
    _, admin_token, _ = service.login("system_admin", CONFIGURED_PASSWORD)
    service.revoke_session_token(admin_token)
    assert service.authenticate_session(admin_token).is_system_admin
    service.revoke_session_token("")
    service.revoke_session_token("unknown-token")


def test_revoke_session_token_does_not_block_login_when_store_fails() -> None:
    service, store = _service()

    def broken(_: str) -> None:
        raise RuntimeError("DB が止まっている")

    store.get_session_by_token_hash = broken  # type: ignore[method-assign,assignment]
    service.revoke_session_token("some-token")


def test_configured_system_admin_reads_and_writes_platform_env_file(tmp_path: Path) -> None:
    """構成管理者は共通 .env の PLATFORM_ADMIN_* を毎回読み、パスワード変更も書き戻す（#211）。"""
    env_file = tmp_path / "platform.env"
    env_file.write_text(
        "PLATFORM_ADMIN_LOGIN_USER_ID=system_admin\n"
        f"PLATFORM_ADMIN_LOGIN_USER_PASSWORD={CONFIGURED_PASSWORD}\n",
        encoding="utf-8",
    )
    store = InMemoryAuthStore()
    service = _ProductService(store, _Settings(app_admin_login_user_password="Unused1234AB"))
    service._platform_env_file = lambda: env_file  # type: ignore[method-assign]

    principal, _, _ = service.login("system_admin", CONFIGURED_PASSWORD)
    service.change_password(principal, CONFIGURED_PASSWORD, "Changed1234AB")

    content = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_ADMIN_LOGIN_USER_PASSWORD=Changed1234AB" in content
    assert CONFIGURED_PASSWORD not in content
    service.login("system_admin", "Changed1234AB")


def test_last_system_admin_cannot_be_disabled() -> None:
    service, store = _service()
    admin = _admin(service)
    user = store.get_user_by_login_user_id("admin")
    assert user is not None
    with pytest.raises(SecurityApiError) as exc:
        service.update_user(
            user.user_uuid,
            expected_version=user.version,
            display_name=user.display_name,
            status="DISABLED",
            role_ids=user.role_ids,
            actor=admin,
        )
    assert "最後のシステム管理者" in exc.value.public_message


def test_system_admin_role_cannot_be_assigned_to_db_users() -> None:
    """SYSTEM_ADMIN は構成管理者だけが持ち、新規作成・編集のどちらでも付けない（#1792）。"""
    service, store = _service()
    admin = _admin(service)
    role = service.create_role(role_code="VIEWER", display_name="閲覧", description="", actor=admin)
    with pytest.raises(SecurityApiError) as created:
        service.create_user(
            login_user_id="new.admin",
            display_name="新しい管理者",
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            temporary_password=None,
            actor=admin,
        )
    assert created.value.status_code == 409
    assert "構成管理者" in created.value.public_message
    user, _ = service.create_user(
        login_user_id="viewer.user",
        display_name="閲覧者",
        role_ids=[role.role_id],
        temporary_password=None,
        actor=admin,
    )
    with pytest.raises(SecurityApiError) as updated:
        service.update_user(
            user.user_uuid,
            expected_version=user.version,
            display_name=user.display_name,
            status="ACTIVE",
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            actor=admin,
        )
    assert updated.value.status_code == 409
    assert "構成管理者" in updated.value.public_message


def test_bootstrap_db_user_keeps_but_cannot_regain_system_admin() -> None:
    """旧来の初期管理者の DB ユーザーも、解除の後に SYSTEM_ADMIN を付け直せない（#1792）。"""
    service, store = _service()
    admin = _admin(service)
    role = service.create_role(role_code="VIEWER", display_name="閲覧", description="", actor=admin)
    other, _ = service.create_user(
        login_user_id="other.user",
        display_name="別の利用者",
        role_ids=[role.role_id],
        temporary_password=None,
        actor=admin,
    )
    # 最後のシステム管理者の解除の保護を避けるため、もう 1 人を直接の割り当てで用意する。
    store.users[other.user_uuid].role_ids = [SYSTEM_ADMIN_ROLE_ID]
    bootstrap = store.get_user_by_login_user_id("admin")
    assert bootstrap is not None and bootstrap.is_bootstrap_admin
    removed = service.update_user(
        bootstrap.user_uuid,
        expected_version=bootstrap.version,
        display_name=bootstrap.display_name,
        status="ACTIVE",
        role_ids=[role.role_id],
        actor=admin,
    )
    assert removed.role_ids == [role.role_id]
    with pytest.raises(SecurityApiError) as exc:
        service.update_user(
            removed.user_uuid,
            expected_version=removed.version,
            display_name=removed.display_name,
            status="ACTIVE",
            role_ids=[SYSTEM_ADMIN_ROLE_ID],
            actor=admin,
        )
    assert exc.value.status_code == 409


def test_cross_product_permissions_block_role_assignment() -> None:
    """RAG のユーザー管理者は、NL2SQL の権限を持つロールを割り当てられない（#212）。"""
    service, store = _service()
    admin = _admin(service)
    manager_role = service.create_role(
        role_code="RAG_USER_MANAGER", display_name="RAG 管理", description="", actor=admin
    )
    _grant(store, "rag", manager_role.role_id, "menu.security_users", "menu.search")
    search_role = service.create_role(
        role_code="SEARCH_ONLY", display_name="検索", description="", actor=admin
    )
    _grant(store, "rag", search_role.role_id, "menu.search")
    nl2sql_admin_role = service.create_role(
        role_code="NL2SQL_ADMIN_SQL", display_name="NL2SQL 管理 SQL", description="", actor=admin
    )
    _grant(store, "rag", nl2sql_admin_role.role_id, "menu.search")
    _grant(store, "nl2sql", nl2sql_admin_role.role_id, "menu.admin_sql")

    manager, _ = service.create_user(
        login_user_id="rag.manager",
        display_name="RAG 管理者",
        role_ids=[manager_role.role_id],
        temporary_password="ManagerPass!12345",
        actor=admin,
    )
    store.set_password(
        manager.user_uuid, service._hash_password("ManagerPass!12345"), force_change=False
    )
    actor, _, _ = service.login("rag.manager", "ManagerPass!12345")
    assert actor.permissions == {"menu.security_users", "menu.search"}

    # RAG の権限だけのロールは割り当てられる。
    created, _ = service.create_user(
        login_user_id="search.user",
        display_name="検索ユーザー",
        role_ids=[search_role.role_id],
        temporary_password="SearchUser!12345",
        actor=actor,
    )
    assert created.role_ids == [search_role.role_id]
    # NL2SQL の権限を含むロールは、RAG の権限が収まっていても拒否する。
    with pytest.raises(SecurityApiError) as exc:
        service.create_user(
            login_user_id="blocked.user",
            display_name="拒否",
            role_ids=[nl2sql_admin_role.role_id],
            temporary_password="BlockedUser!12345",
            actor=actor,
        )
    assert exc.value.status_code == 403
    # 一覧でも割り当て候補に出さない。
    visible = {role.role_code for role in service.list_roles_for_actor(actor)}
    assert visible == {"RAG_USER_MANAGER", "SEARCH_ONLY"}
    # 復元も同じ判定を通る。
    archived = service.archive_role(
        nl2sql_admin_role.role_id, expected_version=nl2sql_admin_role.version, actor=admin
    )
    with pytest.raises(SecurityApiError) as restore_exc:
        service.restore_role(archived.role_id, expected_version=archived.version, actor=actor)
    assert restore_exc.value.status_code == 403


def test_role_basic_update_keeps_role_and_rejects_reserved_code() -> None:
    service, _ = _service()
    admin = _admin(service)
    with pytest.raises(SecurityApiError) as exc:
        service.create_role(
            role_code="system_admin", display_name="予約", description="", actor=admin
        )
    assert exc.value.code == "SECURITY_ROLE_CODE_RESERVED"
    role = service.create_role(role_code="viewer", display_name="閲覧", description="", actor=admin)
    assert role.role_code == "VIEWER"
    updated = service.update_role(
        role.role_id, expected_version=role.version, display_name="閲覧者", actor=admin
    )
    assert (updated.display_name, updated.description, updated.version) == ("閲覧者", "", 2)


class _Cursor:
    def __init__(self, failures: dict[int, str]) -> None:
        self.failures = failures
        self.executed: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, *_: object) -> None:
        self.executed.append(statement)
        index = len(self.executed)
        if index in self.failures:
            raise RuntimeError(self.failures[index])


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def test_platform_auth_ddl_is_idempotent_and_uses_platform_names() -> None:
    joined = "\n".join(PLATFORM_AUTH_DDL)
    for table in (
        "PLATFORM_USERS",
        "PLATFORM_ROLES",
        "PLATFORM_USER_ROLES",
        "PLATFORM_AUTH_SESSIONS",
    ):
        assert f"CREATE TABLE {table} (" in joined
    assert "NL2SQL_" not in joined
    cursor = _Cursor({1: "ORA-00955: name is already used by an existing object"})
    connection = _Connection(cursor)
    results = apply_platform_auth_schema(connection)
    assert results[0] == {"index": "1", "status": "skipped", "code": "ORA-00955"}
    assert connection.committed
    assert "ORA-00955" in PLATFORM_AUTH_DDL_IGNORED_ERRORS
    failing = _Connection(_Cursor({2: "ORA-01031: insufficient privileges"}))
    with pytest.raises(RuntimeError):
        apply_platform_auth_schema(failing)


def test_role_permission_codes_rejects_unregistered_tables_and_skips_missing() -> None:
    cursor = _Cursor({1: "ORA-00942: table or view does not exist"})
    cursor.fetchall = lambda: []  # type: ignore[attr-defined]

    class _Factory:
        def __enter__(self) -> _Connection:
            return _Connection(cursor)

        def __exit__(self, *_: object) -> None:
            return None

    store = OracleAuthStore(lambda: _Factory())
    result = store.role_permission_codes(["r1"], tables=["RAG_ROLE_PERMISSIONS"])
    assert result == {"RAG_ROLE_PERMISSIONS": {}}
    with pytest.raises(Exception, match="未登録"):
        store.role_permission_codes(["r1"], tables=["PLATFORM_USERS"])


def _authorize_app(
    service: AuthService,
    *,
    permissions: frozenset[str] | None,
    service_token_paths: tuple[str, ...] = (),
    public_paths: tuple[str, ...] = (),
    open_operations: frozenset[tuple[str, str]] | None = None,
) -> Any:
    settings = SimpleNamespace(
        local_debug_enabled=False,
        app_auth_enabled=True,
        app_auth_session_cookie_name="s",
        app_auth_csrf_cookie_name="c",
    )

    async def run_sync(func: Any, *args: Any) -> Any:
        return func(*args)

    async def dependency(request: Request) -> Any:
        async with authorize_request(
            request,
            settings=settings,
            service=service,
            run_sync=run_sync,
            permission_for_route=lambda method, path: permissions,
            public_paths=public_paths,
            authenticated_without_permission=(),
            local_debug_principal=lambda: None,  # type: ignore[arg-type,return-value]
            enter_actor=lambda principal: nullcontext(),
            exit_actor=lambda token: None,
            unclassified_permission="__unclassified__",
            service_token_paths=service_token_paths,
            service_token_audience="rag",
            open_operations=open_operations,
        ):
            yield

    app = FastAPI()

    @app.exception_handler(SecurityApiError)
    async def handler(request: Request, exc: SecurityApiError) -> JSONResponse:
        return JSONResponse({"error_code": exc.code}, status_code=exc.status_code)

    @app.get("/probe", dependencies=[Depends(dependency)])
    def probe() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/probe", dependencies=[Depends(dependency)])
    def probe_post(request: Request) -> dict[str, Any]:
        scope = service_token_profile_scope(request)
        return {
            "user_uuid": request.state.principal.user_uuid,
            "run_id": request.state.service_token_claims.get("run_id"),
            "profile_scope": sorted(scope) if scope is not None else None,
        }

    return TestClient(app)


def test_permission_route_path_drops_path_converter() -> None:
    """`{name:path}` の route も、manifest・OpenAPI と同じ `{name}` の template で照合する。"""
    seen: list[str] = []

    async def capture(request: Request) -> None:
        seen.append(permission_route_path(request))

    app = FastAPI()

    @app.get("/api/items/{owner}/{name:path}", dependencies=[Depends(capture)])
    def item(owner: str, name: str) -> dict[str, str]:
        return {"owner": owner, "name": name}

    response = TestClient(app).get("/api/items/HR/A/B")

    assert response.json() == {"owner": "HR", "name": "A/B"}
    assert seen == ["/items/{owner}/{name}"]
    assert "/api/items/{owner}/{name}" in app.openapi()["paths"]


def test_authorize_request_keeps_route_error_codes() -> None:
    """経路の権限拒否・未登録の API は専用の error_code で返す（#224）。"""
    service, _ = _service()
    _, token, _ = service.login("ADMIN", ADMIN_PASSWORD)
    # SYSTEM_ADMIN 以外にするため、権限のないユーザーでも確かめる。
    admin = _admin(service)
    viewer_role = service.create_role(
        role_code="VIEWER", display_name="閲覧", description="", actor=admin
    )
    user, _ = service.create_user(
        login_user_id="viewer",
        display_name="閲覧者",
        role_ids=[viewer_role.role_id],
        temporary_password="Kq7#Lomber!Vt29",
        actor=admin,
    )
    _, store = service, service.store
    assert isinstance(store, InMemoryAuthStore)
    store.set_password(
        user.user_uuid, service._hash_password("Kq7#Lomber!Vt29"), force_change=False
    )
    _, viewer_token, _ = service.login("viewer", "Kq7#Lomber!Vt29")

    denied = _authorize_app(service, permissions=frozenset({"menu.search"}))
    response = denied.get("/probe", cookies={"s": viewer_token})
    assert (response.status_code, response.json()["error_code"]) == (403, ROUTE_FORBIDDEN_CODE)

    unclassified = _authorize_app(service, permissions=frozenset({"__unclassified__"}))
    response = unclassified.get("/probe", cookies={"s": token})
    assert (response.status_code, response.json()["error_code"]) == (403, ROUTE_UNCLASSIFIED_CODE)

    response = denied.get("/probe")
    assert response.status_code == 401
    assert response.json()["error_code"] == "SECURITY_AUTHENTICATION_REQUIRED"
    assert {ROUTE_FORBIDDEN_CODE, ROUTE_UNCLASSIFIED_CODE} == ROUTE_FORBIDDEN_CODES


def test_service_token_round_trip_and_rejections() -> None:
    """サービストークンは署名・aud・有効期限を確かめる（#230）。"""
    token = issue_service_token(
        SERVICE_SECRET, subject="u1", audience="rag", issuer="agent", claims={"run_id": "r1"}
    )
    claims = verify_service_token(SERVICE_SECRET, token, audience="rag")
    assert (claims["sub"], claims["iss"], claims["run_id"]) == ("u1", "agent", "r1")

    def rejected(value: str, *, audience: str = "rag", now: float | None = None) -> int:
        with pytest.raises(SecurityApiError) as exc:
            verify_service_token(SERVICE_SECRET, value, audience=audience, now=now)
        return exc.value.status_code

    assert rejected(token, audience="nl2sql") == 401
    assert rejected(token + "x") == 401
    assert rejected("a.b") == 401
    other = issue_service_token("y" * 40, subject="u1", audience="rag", issuer="agent")
    assert rejected(other) == 401
    assert rejected(token, now=time.time() + 120) == 401  # 期限切れ（60 秒 + 許容 30 秒）
    long_lived = issue_service_token(
        SERVICE_SECRET, subject="u1", audience="rag", issuer="agent", ttl_seconds=600
    )
    assert verify_service_token(SERVICE_SECRET, long_lived, audience="rag")["sub"] == "u1"
    with pytest.raises(ValueError):
        issue_service_token(
            SERVICE_SECRET, subject="u1", audience="rag", issuer="agent", ttl_seconds=601
        )
    # 鍵が短い（未設定）なら 503。
    with pytest.raises(SecurityApiError) as exc:
        verify_service_token("", token, audience="rag")
    assert exc.value.status_code == 503


def test_service_token_profile_scope_claim() -> None:
    """プロファイルの範囲（#1379）は任意の claim。未設定なら付けず、形の違う claim は 401。"""
    plain = issue_service_token(
        SERVICE_SECRET, subject="u1", audience="rag", issuer="agent", profile_ids=[]
    )
    claims = verify_service_token(SERVICE_SECRET, plain, audience="rag")
    assert PROFILE_SCOPE_CLAIM not in claims
    assert profile_scope_from_claims(claims) is None
    assert profile_scope_from_claims(None) is None

    scoped = issue_service_token(
        SERVICE_SECRET,
        subject="u1",
        audience="rag",
        issuer="agent",
        # claims で渡した範囲は使わない（範囲は profile_ids だけで付ける）。
        claims={PROFILE_SCOPE_CLAIM: ["spoofed"]},
        profile_ids=["p-1", " p-2 ", "p-1"],
    )
    claims = verify_service_token(SERVICE_SECRET, scoped, audience="rag")
    assert claims[PROFILE_SCOPE_CLAIM] == ["p-1", "p-2"]
    assert profile_scope_from_claims(claims) == frozenset({"p-1", "p-2"})

    with pytest.raises(ValueError):
        issue_service_token(
            SERVICE_SECRET, subject="u1", audience="rag", issuer="agent", profile_ids=[""]
        )
    with pytest.raises(ValueError):
        issue_service_token(
            SERVICE_SECRET,
            subject="u1",
            audience="rag",
            issuer="agent",
            profile_ids=[f"p-{index}" for index in range(51)],
        )

    def signed(payload: dict[str, object]) -> str:
        header = _segment({"alg": "HS256", "typ": "JWT"})
        signing_input = f"{header}.{_segment(payload)}"
        return f"{signing_input}.{_signature(SERVICE_SECRET, signing_input)}"

    now = int(time.time())
    base = {"sub": "u1", "aud": "rag", "iss": "agent", "iat": now, "exp": now + 60}
    # 鍵を持つ呼び出し元が作っても、壊れた範囲（空・文字列・数値）は「範囲なし」として通さない。
    for broken in ([], "p-1", [1], [""], ["x" * 129]):
        with pytest.raises(SecurityApiError) as exc:
            verify_service_token(
                SERVICE_SECRET, signed({**base, PROFILE_SCOPE_CLAIM: broken}), audience="rag"
            )
        assert exc.value.status_code == 401
    assert profile_scope_from_claims({PROFILE_SCOPE_CLAIM: "broken"}) == frozenset()


@pytest.mark.parametrize(
    ("allowed", "scope", "expected"),
    [
        (None, None, None),
        ({"a", "b"}, None, frozenset({"a", "b"})),
        (None, frozenset({"a"}), frozenset({"a"})),
        ({"a", "b"}, frozenset({"b", "c"}), frozenset({"b"})),
        ({"a"}, frozenset({"c"}), frozenset()),
    ],
    ids=["no-limit", "user-only", "claim-only", "intersection", "disjoint"],
)
def test_narrow_to_profile_scope(
    allowed: set[str] | None, scope: frozenset[str] | None, expected: frozenset[str] | None
) -> None:
    assert narrow_to_profile_scope(allowed, scope) == expected


def test_authenticate_service_token_uses_current_user_permissions() -> None:
    service, store = _service()
    admin = _admin(service)
    role = service.create_role(role_code="SEARCH", display_name="検索", description="", actor=admin)
    _grant(store, "rag", role.role_id, "menu.search")
    user, _ = service.create_user(
        login_user_id="searcher",
        display_name="検索者",
        role_ids=[role.role_id],
        temporary_password="Kq7#Lomber!Vt29",
        actor=admin,
    )

    def token_for(user_uuid: str) -> str:
        return issue_service_token(
            SERVICE_SECRET, subject=user_uuid, audience="rag", issuer="agent"
        )

    # 初回パスワード変更が済んでいない利用者は使えない。
    with pytest.raises(SecurityApiError) as exc:
        service.authenticate_service_token(token_for(user.user_uuid), audience="rag")
    assert exc.value.status_code == 403
    store.set_password(
        user.user_uuid, service._hash_password("Kq7#Lomber!Vt29"), force_change=False
    )
    principal, claims = service.authenticate_service_token(
        token_for(user.user_uuid), audience="rag"
    )
    assert (principal.user_uuid, principal.permissions) == (user.user_uuid, {"menu.search"})
    assert claims["iss"] == "agent"
    with pytest.raises(SecurityApiError) as exc:
        service.authenticate_service_token(token_for("unknown"), audience="rag")
    assert exc.value.status_code == 403


def test_authorize_request_accepts_service_token_only_on_service_paths() -> None:
    service, _ = _service()
    admin = _admin(service)
    token = issue_service_token(
        SERVICE_SECRET,
        subject=admin.user_uuid,
        audience="rag",
        issuer="agent",
        claims={"run_id": "run-1"},
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = _authorize_app(service, permissions=None, service_token_paths=("/probe",))
    # Cookie も CSRF もなしで、token の利用者として通る。
    response = client.post("/probe", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "user_uuid": admin.user_uuid,
        "run_id": "run-1",
        "profile_scope": None,
    }
    # プロファイルの範囲（#1379）は claim から読む。
    scoped = issue_service_token(
        SERVICE_SECRET,
        subject=admin.user_uuid,
        audience="rag",
        issuer="agent",
        profile_ids=["p-2", "p-1"],
    )
    response = client.post("/probe", headers={"Authorization": f"Bearer {scoped}"})
    assert response.json()["profile_scope"] == ["p-1", "p-2"]
    assert client.post("/probe").status_code == 401
    assert client.post("/probe", headers={"Authorization": "Bearer bad"}).status_code == 401
    # サービス用の path でなければ token は使えない（Cookie が必要）。
    cookie_only = _authorize_app(service, permissions=None)
    assert cookie_only.post("/probe", headers=headers).status_code == 401


def test_authorize_request_opens_only_listed_operations_on_public_paths() -> None:
    """公開 path でも、open_operations にない method は認証を求める（#490）。"""
    service, _ = _service()
    client = _authorize_app(
        service,
        permissions=None,
        public_paths=("/probe",),
        open_operations=frozenset({("GET", "/probe")}),
    )

    assert client.get("/probe").status_code == 200
    # 同じ path の POST は公開しない（Cookie がないので認証で止まる）。
    assert client.post("/probe").status_code in {401, 403}


def test_authorize_request_keeps_path_based_public_paths_without_open_operations() -> None:
    """open_operations を渡さないときは、従来どおり path 単位で公開する（後方互換）。"""
    service, _ = _service()
    client = _authorize_app(service, permissions=None, public_paths=("/probe",))

    assert client.get("/probe").status_code == 200
    # POST も認証を飛ばして route まで届く（route が principal を読むので AttributeError になる）。
    with pytest.raises(AttributeError):
        client.post("/probe")


def test_display_identities_include_configured_admin_and_resolve_login_ids() -> None:
    """画面に出す利用者の名前は表示名を返し、構成管理者は「システム管理者」（#1696）。"""
    service, _ = _service()
    admin = _admin(service)
    role = service.create_role(
        role_code="NAME_VIEWER", display_name="名前確認", description="", actor=admin
    )
    user, _ = service.create_user(
        login_user_id="name.user",
        display_name="名前ユーザー",
        role_ids=[role.role_id],
        temporary_password="NameUserPass!12345",
        actor=admin,
    )

    by_uuid = service.display_identities(
        [CONFIGURED_SYSTEM_ADMIN_USER_UUID, user.user_uuid, "missing-uuid", ""]
    )
    assert by_uuid[CONFIGURED_SYSTEM_ADMIN_USER_UUID].display_name == "システム管理者"
    assert by_uuid[CONFIGURED_SYSTEM_ADMIN_USER_UUID].login_user_id == "system_admin"
    assert by_uuid[user.user_uuid].display_name == "名前ユーザー"
    assert "missing-uuid" not in by_uuid

    by_login = service.display_identities_by_login(["system_admin", "Name.User", "gone", ""])
    assert by_login["system_admin"].display_name == "システム管理者"
    assert by_login["Name.User"].display_name == "名前ユーザー"
    assert "gone" not in by_login and "" not in by_login
