"""Agent の認証/RBAC の API テスト（#215）。

- production mode のログイン（DB ユーザー・構成管理者）・CSRF・強制パスワード変更
- 権限の既定拒否（manifest 未登録・権限なし 403）と manifest の完全性
- production で Cookie がないリクエスト（401）
- `/security/*`（権限カタログ・対象の一覧・ロールの権限と対象範囲の保存と昇格防止）
- 廃止した権限コード（`menu.dashboard`。#262）が DB に残っていても壊れないこと
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from pytest import MonkeyPatch
from security_support import (
    CONFIGURED_ADMIN_PASSWORD,
    USER_PASSWORD,
    ProductionAuth,
    client,
    enable_production_auth,
    login,
    login_configured_admin,
    session_headers,
)

import app.features.agent.router as agent_router
from app.features.agent.runtime import AgentProfile, runtime_repository
from app.main import app
from app.security import dependencies as security_dependencies
from app.security.permissions import (
    ALL_PERMISSION_CODES,
    API_KEY_API_PATHS,
    AUTHENTICATED_WITHOUT_PERMISSION,
    CAPABILITY_ROLES,
    LOGIN_ONLY_API_PATHS,
    OPEN_API_OPERATIONS,
    PERMISSION_CATALOG,
    PUBLIC_API_PATHS,
    RETIRED_PERMISSION_CODES,
    ROUTE_PERMISSIONS,
    UNCLASSIFIED_PERMISSION,
    expand_permissions,
    permission_for_route,
)
from app.security.service import set_security_service
from app.settings import get_settings

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


@contextmanager
def _agent(agent_id: str, name: str | None = None) -> Iterator[AgentProfile]:
    agent = runtime_repository.create_agent(AgentProfile(id=agent_id, name=name or agent_id))
    try:
        yield agent
    finally:
        with suppress(KeyError, ValueError):
            runtime_repository.delete_agent(agent_id)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def _api_operations(application: FastAPI = app) -> list[tuple[str, str]]:
    operations: list[tuple[str, str]] = []
    for path, methods in application.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in methods:
            if method.upper() in HTTP_METHODS:
                operations.append((method.upper(), path.removeprefix("/api")))
    return operations


def _unclassified_operations(operations: list[tuple[str, str]]) -> list[str]:
    return [
        f"{method} {path}"
        for method, path in operations
        if (permissions := permission_for_route(method, path)) is not None
        and UNCLASSIFIED_PERMISSION in permissions
    ]


def test_every_api_route_is_classified_by_manifest() -> None:
    """全 API（method × path）が manifest に登録されている（登録外は既定で拒否）。"""
    operations = _api_operations()
    # 外部 Runtime・Binding の API は #754 で削除した。下限は収集の取りこぼしを検出するため。
    assert len(operations) > 80
    assert _unclassified_operations(operations) == []
    open_operations = {
        (method, path) for method, path in operations if permission_for_route(method, path) is None
    }
    # 権限なしで通すのは公開・ログインだけの path と、外部の MCP クライアントの入口（#1874。
    # API キーで認証し、ツールごとに利用者の権限で判定する）。
    assert {path for _method, path in open_operations} == (
        set(PUBLIC_API_PATHS)
        | set(AUTHENTICATED_WITHOUT_PERMISSION)
        | set(API_KEY_API_PATHS)
        | set(LOGIN_ONLY_API_PATHS)
    )
    # 権限なしで通す path に method を足したら、ここで気づく（method 単位で照合する。#490）。
    assert open_operations == set(OPEN_API_OPERATIONS)


def test_manifest_entries_match_existing_routes_and_known_permissions() -> None:
    """manifest の明示登録は実在する route だけで、権限コードはカタログにあるもの。"""
    operations = set(_api_operations())
    assert set(ROUTE_PERMISSIONS) - operations == set()
    for permissions in ROUTE_PERMISSIONS.values():
        assert permissions
        assert permissions <= ALL_PERMISSION_CODES
    # `/api` は HTTP の route だけ（Run のイベントの push は SSE、操作は REST。#2260）。
    websocket_paths = {
        str(getattr(route, "path", ""))
        for route in agent_router.router.routes
        if type(route).__name__ == "APIWebSocketRoute"
    }
    assert websocket_paths == set()


_USER_ROLE_PREFIXES = ("/security/users", "/security/roles")
# 製品固有の権限管理の route（`/security/roles` 配下だが共通のロール管理ではない）。
_AGENT_ROLE_ACCESS_ROUTE = ("PUT", "/security/roles/{role_id}/access")


def _mounted_user_role_operations() -> set[tuple[str, str]]:
    return {
        (method, path)
        for method, path in _api_operations()
        if path.startswith(_USER_ROLE_PREFIXES) and (method, path) != _AGENT_ROLE_ACCESS_ROUTE
    }


def _legacy_user_role_permission(method: str, route_path: str) -> frozenset[str]:
    """#503 より前の前方一致による割り当て（同じ権限のままかを比べるために残す）。"""
    if route_path == "/security/users" or route_path.startswith("/security/users/"):
        return frozenset({"menu.security_users"})
    if method == "GET":
        return frozenset(
            {"menu.security_users", "menu.security_roles", "menu.security_permissions"}
        )
    return frozenset({"menu.security_roles"})


def test_user_role_routes_are_registered_per_operation() -> None:
    """共通のユーザー・ロール管理の API は (method, path) ごとに manifest に登録する（#503）。"""
    mounted = _mounted_user_role_operations()
    assert len(mounted) == 16
    assert mounted <= set(ROUTE_PERMISSIONS)


def test_user_role_route_permissions_match_legacy_prefix_rules() -> None:
    """明示の登録にしても、ユーザー・ロール管理の API の権限は前方一致のときと同じ（#503）。"""
    for method, route_path in sorted(_mounted_user_role_operations()):
        assert permission_for_route(method, route_path) == _legacy_user_role_permission(
            method, route_path
        ), f"{method} {route_path}"
    assert _perm("GET", "/security/users") == {"menu.security_users"}
    assert _perm("POST", "/security/users/{user_uuid}/unlock") == {"menu.security_users"}
    assert _perm("GET", "/security/roles") == {
        "menu.security_users",
        "menu.security_roles",
        "menu.security_permissions",
    }
    assert _perm("DELETE", "/security/roles/{role_id}") == {"menu.security_roles"}
    assert _perm("POST", "/security/roles/{role_id}/archive") == {"menu.security_roles"}


def test_unregistered_user_role_routes_are_denied() -> None:
    """前方一致をやめたので、登録のない method・path は既定で拒否する（#503）。"""
    unclassified = frozenset({UNCLASSIFIED_PERMISSION})
    assert permission_for_route("POST", "/security/users/{user_uuid}/impersonate") == unclassified
    assert permission_for_route("PUT", "/security/users/{user_uuid}") == unclassified
    assert permission_for_route("POST", "/security/roles/{role_id}/clone") == unclassified
    assert permission_for_route("GET", "/security/roles/{role_id}/access") == unclassified


def test_completeness_check_flags_unregistered_user_route() -> None:
    """`/security/users` 配下に未登録の route を足すと、完全性の検査で見つかる（#503）。"""
    application = FastAPI()

    @application.post("/api/security/users/{user_uuid}/impersonate")
    def _impersonate(user_uuid: str) -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {"user_uuid": user_uuid}

    @application.get("/api/security/users")
    def _list_users() -> dict[str, str]:  # pragma: no cover - 呼ばない
        return {}

    assert _unclassified_operations(_api_operations(application)) == [
        "POST /security/users/{user_uuid}/impersonate"
    ]


def test_manifest_denies_unknown_routes_by_default() -> None:
    assert permission_for_route("GET", "/unknown") == frozenset({UNCLASSIFIED_PERMISSION})
    assert permission_for_route("DELETE", "/runs") == frozenset({UNCLASSIFIED_PERMISSION})
    assert permission_for_route("POST", "/security/permissions") == frozenset(
        {UNCLASSIFIED_PERMISSION}
    )


def _perm(method: str, path: str) -> set[str]:
    return set(permission_for_route(method, path) or ())


def test_manifest_key_assignments() -> None:
    """変更系は capability（従来のロール）、読み取りは画面のメニュー権限。"""
    # 業務 Agent の作成は、自分の業務 Agent の作成（持ち主の判定は router。#2523）でもできる。
    assert _perm("POST", "/agents") == {"agent.admin", "agent.agents.build"}
    assert _perm("POST", "/agents/{agent_id}/share-request/approve") == {"agent.admin"}
    assert _perm("PATCH", "/settings/oci") == {"menu.settings_oci"}
    assert _perm("POST", "/settings/oci/object-storage/namespace") == {
        "menu.settings_oci",
        "menu.settings_upload_storage",
    }
    assert _perm("PATCH", "/settings/model") == {"menu.settings_model"}
    assert _perm("POST", "/settings/database/adb/start") == {"menu.settings_database"}
    assert _perm("PATCH", "/settings/mcp-connections/{server_id}") == {"agent.admin"}
    assert _perm("PATCH", "/settings/tool-policy") == {"agent.admin"}
    assert _perm("POST", "/runs") == {"agent.runs.operate", "agent.admin"}
    assert _perm("POST", "/runs/{run_id}/cancel") == {"agent.runs.operate", "agent.admin"}
    assert _perm("POST", "/approvals/{approval_id}/decision") == {
        "agent.approvals.decide",
        "agent.admin",
    }
    assert _perm("GET", "/runs") == {"menu.runs", "menu.approvals"}
    assert _perm("GET", "/tools") == {"menu.audit", "agent.admin"}
    assert _perm("GET", "/observability/status") == {"menu.audit"}
    assert _perm("GET", "/settings/mcp-connections") == {
        "menu.settings_external_mcp",
        "menu.skills",
    }
    assert _perm("GET", "/settings/mcp-connections/{server_id}/tools") == {
        "menu.settings_external_mcp"
    }
    assert _perm("GET", "/audit/tool-calls") == {"menu.audit"}
    assert _perm("GET", "/settings/oci") == {"menu.settings_oci"}
    assert _perm("GET", "/security/roles/{role_id}") == {
        "menu.security_users",
        "menu.security_roles",
        "menu.security_permissions",
    }
    assert _perm("POST", "/security/roles") == {"menu.security_roles"}
    assert _perm("PATCH", "/security/users/{user_uuid}") == {"menu.security_users"}
    assert _perm("PUT", "/security/roles/{role_id}/access") == {"menu.security_permissions"}
    # 外部 Runtime の Binding の MCP は #754 で削除した（公開 path に残さない）。
    assert "/mcp/{binding_id}" not in PUBLIC_API_PATHS
    # 外部の MCP クライアントの入口（#1874）。`POST /mcp` だけを権限なしで通し、GET は通さない。
    assert ("POST", "/mcp") in OPEN_API_OPERATIONS
    assert ("GET", "/mcp") not in OPEN_API_OPERATIONS
    assert frozenset({"/mcp"}) == API_KEY_API_PATHS
    assert _perm("GET", "/security/api-keys") == {"menu.security_api_keys"}
    assert _perm("DELETE", "/security/api-keys/{key_id}") == {"menu.security_api_keys"}
    assert permission_for_route("GET", "/runtime/status") == frozenset(
        {"menu.runtimes", "menu.agents", "agent.runs.operate"}
    )


def test_retired_permission_codes_are_not_in_catalog_or_manifest() -> None:
    """廃止したコード（#262 / #757）はカタログ・implies・manifest に現れない。"""
    assert RETIRED_PERMISSION_CODES == (
        "menu.dashboard",
        "menu.settings_external_rag",
        "menu.settings_external_nl2sql",
        "menu.settings_connection",
        "menu.plugin_marketplaces",
        "menu.plugins",
        "menu.settings_api_keys",
        "menu.settings_runtime_snapshot",
    )
    retired = set(RETIRED_PERMISSION_CODES)
    assert not retired & ALL_PERMISSION_CODES
    assert all(not retired & set(item.implies) for item in PERMISSION_CATALOG)
    for permissions in ROUTE_PERMISSIONS.values():
        assert not retired & permissions
    assert expand_permissions(retired) == set()


def test_capabilities_map_to_legacy_roles_and_imply_menus() -> None:
    assert CAPABILITY_ROLES == {
        "agent.runs.view": "viewer",
        "agent.runs.operate": "operator",
        "agent.approvals.decide": "approver",
        "agent.audit.view": "auditor",
        "agent.admin": "admin",
    }
    assert expand_permissions({"agent.runs.view"}) == {"agent.runs.view", "menu.runs"}
    assert "menu.approvals" in expand_permissions({"agent.approvals.decide"})
    assert "menu.audit" in expand_permissions({"agent.audit.view"})
    admin = expand_permissions({"agent.admin"})
    assert {"menu.agents", "menu.settings_oci", "menu.settings_external_mcp"} <= admin
    # ユーザーとロール・権限管理は agent.admin に含めない。
    assert not {"menu.security_users", "menu.security_roles", "menu.security_permissions"} & admin
    # メニュー権限は capability を暗黙に含まない（昇格しない）。
    assert expand_permissions({"menu.runs"}) == {"menu.runs"}
    assert expand_permissions({"unknown.code"}) == set()


def test_unclassified_route_is_denied_even_for_system_admin(
    monkeypatch: MonkeyPatch, auth: ProductionAuth
) -> None:
    """manifest が未登録を返す route は、ログイン済みでも 403（SYSTEM_ADMIN も同じ）。"""
    headers = login_configured_admin()
    assert client.get("/api/runs", headers=headers).status_code == 200
    monkeypatch.setattr(
        security_dependencies,
        "permission_for_route",
        lambda method, path: frozenset({UNCLASSIFIED_PERMISSION}),
    )
    response = client.get("/api/runs", headers=headers)
    assert response.status_code == 403
    assert response.json()["error_messages"] == ["この API は権限一覧に登録されていません。"]


# ---------------------------------------------------------------------------
# production mode のログイン
# ---------------------------------------------------------------------------


def test_production_without_cookie_is_401(auth: ProductionAuth) -> None:
    """production で Cookie がなければ、どの保護 API も 401（全開放にしない）。"""
    for method, path in (
        ("GET", "/api/runs"),
        ("GET", "/api/agents"),
        ("GET", "/api/tools"),
        ("GET", "/api/observability/status"),
        ("GET", "/api/audit/tool-calls"),
        ("GET", "/api/settings/mcp-connections"),
        ("GET", "/api/settings/oci"),
        ("GET", "/api/security/permissions"),
        ("POST", "/api/runs"),
    ):
        response = client.request(method, path, headers={"X-Agent-Roles": "admin"})
        assert response.status_code == 401, (method, path, response.text)
    assert client.get("/api/runs").json()["error_messages"] == ["ログインしてください。"]


def test_production_public_paths_do_not_require_login(
    auth: ProductionAuth, monkeypatch: MonkeyPatch
) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code == 200
    # DB の状態 API は画面の DB ゲートがログイン前に使う（#325）。接続設定が無ければ接続を試さない。
    monkeypatch.setattr(
        agent_router,
        "get_settings",
        lambda: SimpleNamespace(oracle_user="", oracle_dsn="", oracle_wallet_dir=""),
    )
    response = client.get("/api/ready/database")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "not_configured"
    # 未定義の path は認証の前に 404。
    assert client.get("/api/not-defined").status_code == 404


def test_database_user_login_me_and_logout(auth: ProductionAuth) -> None:
    user = auth.user_with_permissions(
        "operator1",
        ["agent.runs.operate", "menu.agents"],
        agent_ids=["default"],
    )
    login_response = client.post(
        "/api/auth/login", json={"login_user_id": "operator1", "password": USER_PASSWORD}
    )
    assert login_response.status_code == 200
    data = login_response.json()["data"]
    assert data["user_uuid"] == user.user_uuid
    assert data["is_system_admin"] is False
    # ロールの表示名は role_codes と同じ順（サイドナビのアカウント欄に出す。#1644）。
    assert len(data["role_display_names"]) == len(data["role_codes"]) == 1
    assert data["role_display_names"][0].startswith("ロール ")
    # implies を展開した実効権限。
    assert set(data["permissions"]) == {
        "agent.runs.operate",
        "menu.agents",
        "menu.chat",
        "menu.runs",
    }
    assert data["allowed_agent_ids"] == ["default"]
    assert "allowed_search_answer_profile_ids" not in data
    assert data["debug_mode"] is False
    assert data["password_change_allowed"] is True
    headers = session_headers(login_response)
    set_cookie = login_response.headers.get_list("set-cookie")
    assert any(item.startswith("pr_session=") and "HttpOnly" in item for item in set_cookie)
    assert any(item.startswith("pr_csrf=") for item in set_cookie)

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["login_user_id"] == "operator1"

    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_login_rejects_invalid_credentials(auth: ProductionAuth) -> None:
    auth.user_with_permissions("viewer1", ["agent.runs.view"])
    response = client.post(
        "/api/auth/login", json={"login_user_id": "viewer1", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error_messages"] == [
        "ログインユーザーIDまたはパスワードを確認してください。"
    ]
    assert response.json()["error_code"] == "SECURITY_AUTHENTICATION_REQUIRED"


def test_login_is_rate_limited_with_retry_after(auth: ProductionAuth) -> None:
    """構成管理者のパスワードを何回でも試せない（#1087）。429 の文と Retry-After を返す。"""
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json={"login_user_id": "system_admin", "password": "WrongPass12345"}
        )
        assert response.status_code == 401
    response = client.post(
        "/api/auth/login",
        json={"login_user_id": "system_admin", "password": CONFIGURED_ADMIN_PASSWORD},
    )
    assert response.status_code == 429
    assert response.json()["error_messages"] == [
        "ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。"
    ]
    assert response.json()["error_code"] == "SECURITY_RATE_LIMITED"
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["X-Request-ID"]


def test_configured_system_admin_login_has_all_permissions_and_no_scope(
    auth: ProductionAuth,
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"login_user_id": "system_admin", "password": CONFIGURED_ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["is_system_admin"] is True
    assert set(data["permissions"]) == set(ALL_PERMISSION_CODES)
    assert data["allowed_agent_ids"] is None
    # 構成管理者の token は 3 製品で共通の接頭辞（ほかの製品でも同じ token で使える。#1565）。
    assert str(response.cookies.get("pr_session")).startswith("platform-system-admin-v1.")
    headers = session_headers(response)
    assert client.get("/api/runs", headers=headers).status_code == 200
    assert client.get("/api/settings/tool-policy", headers=headers).status_code == 200


def test_invalid_session_cookie_is_401_and_headers_are_not_trusted(auth: ProductionAuth) -> None:
    """Cookie が不正なら 401。`X-Agent-Roles` などの自己申告の header は使わない（#750）。"""
    for headers in (
        {"cookie": "pr_session=tampered", "X-Agent-Roles": "admin"},
        {"X-Agent-Roles": "admin", "X-Agent-Business-Views": "*"},
        {"X-Agent-Actor": "ops"},
    ):
        assert client.get("/api/runs", headers=headers).status_code == 401, headers
    assert (
        client.post("/api/runs", json={"goal": "x"}, headers={"X-Agent-Roles": "admin"}).status_code
        == 401
    )


def test_permission_denied_returns_403(auth: ProductionAuth) -> None:
    auth.user_with_permissions("auditor1", ["agent.audit.view"])
    auth.user_with_permissions("admin1", ["agent.admin"])
    auditor = login("auditor1")
    response = client.get("/api/settings/mcp-connections", headers=auditor)
    assert response.status_code == 403
    assert response.json()["error_messages"] == ["この機能を利用する権限がありません。"]
    assert client.get("/api/audit/tool-calls", headers=auditor).status_code == 200
    admin = login("admin1")
    assert client.get("/api/settings/mcp-connections", headers=admin).status_code == 200


def test_menu_without_capability_can_open_page_but_not_read_runs(auth: ProductionAuth) -> None:
    """Run 画面のメニューだけでは Run の実データは読めない（capability が必要）。"""
    auth.user_with_permissions("menu-only", ["menu.runs"])
    headers = login("menu-only")
    response = client.get("/api/runs", headers=headers)
    assert response.status_code == 403
    assert response.json()["error_messages"][0].startswith("この操作を行う権限がありません。")


def test_runtime_status_is_readable_with_the_runtimes_menu(auth: ProductionAuth) -> None:
    """実行環境の状態は Run のデータではないので、メニュー権限だけで読める（#1041）。"""
    auth.user_with_permissions("runtime-ops", ["menu.runtimes"])
    auth.user_with_permissions("tables-only", ["menu.settings_system_tables"])
    ops = login("runtime-ops")
    assert client.get("/api/runtime/status", headers=ops).status_code == 200
    assert client.get("/api/runtime/storage", headers=ops).status_code == 200
    # Run の実データは従来どおり capability が要る。
    assert client.get("/api/runs", headers=ops).status_code == 403
    assert client.get("/api/runtime/status", headers=login("tables-only")).status_code == 403


def test_user_without_roles_is_denied_everything_except_auth(auth: ProductionAuth) -> None:
    """ロールのない利用者は既定で何もできない（ログイン系だけ使える）。"""
    auth.create_user("nobody")
    headers = login("nobody")
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    for method, path in (
        ("GET", "/api/runs"),
        ("GET", "/api/agents"),
        ("GET", "/api/skills"),
        ("GET", "/api/tools"),
        ("GET", "/api/audit/tool-calls"),
        ("GET", "/api/security/permissions"),
        ("GET", "/api/security/users"),
        ("GET", "/api/settings/oci"),
    ):
        assert client.request(method, path, headers=headers).status_code == 403, path


def test_state_changing_request_requires_csrf_token(auth: ProductionAuth) -> None:
    auth.user_with_permissions("security-admin", ["menu.security_roles"])
    headers = login("security-admin")
    body = {"role_code": "READER", "display_name": "閲覧者", "description": ""}
    without_csrf = {"cookie": headers["cookie"]}
    response = client.post("/api/security/roles", json=body, headers=without_csrf)
    assert response.status_code == 403
    assert "安全性" in response.json()["error_messages"][0]
    wrong_csrf = {**headers, "X-CSRF-Token": "tampered"}
    assert client.post("/api/security/roles", json=body, headers=wrong_csrf).status_code == 403
    created = client.post("/api/security/roles", json=body, headers=headers)
    assert created.status_code == 200
    data = created.json()["data"]
    assert data["permissions"] == []
    assert data["agent_ids"] == []


def test_forced_password_change_blocks_other_apis(auth: ProductionAuth) -> None:
    role = auth.create_role(["agent.runs.view"])
    auth.create_user("new-user", [role], force_password_change=True)
    headers = login("new-user")
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    response = client.get("/api/runs", headers=headers)
    assert response.status_code == 403
    assert response.json()["error_messages"] == ["初回パスワード変更を完了してください。"]
    changed = client.post(
        "/api/auth/password/change",
        json={"current_password": USER_PASSWORD, "new_password": "NewPassword!2026x"},
        headers=headers,
    )
    assert changed.status_code == 200
    relogin = login("new-user", "NewPassword!2026x")
    assert client.get("/api/runs", headers=relogin).status_code == 200


def test_local_mode_uses_all_permissions_without_login() -> None:
    """local は全権限・対象範囲の制限なしのローカル利用者（ログイン不要）。"""
    assert get_settings().auth_mode == "local"
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["debug_mode"] is True
    assert data["is_system_admin"] is True
    assert data["role_display_names"] == ["システム管理者"]
    assert set(data["permissions"]) == set(ALL_PERMISSION_CODES)
    assert data["allowed_agent_ids"] is None
    assert data["password_change_allowed"] is False
    # 従来どおり API はログインなしで使える。
    assert client.get("/api/runs").status_code == 200


def test_local_mode_uses_oracle_security_store(monkeypatch: MonkeyPatch) -> None:
    """local でもユーザー・ロールは Oracle の PLATFORM_*（RAG / NL2SQL と同じ。#750）。

    InMemory にすると、ユーザー管理・ロール管理・権限管理が空になる。
    """
    from app.security import service as security_service
    from app.security.store import OracleSecurityStore

    assert get_settings().auth_mode == "local"
    set_security_service(None)
    try:
        assert isinstance(security_service.get_security_service().store, OracleSecurityStore)
    finally:
        set_security_service(None)


# ---------------------------------------------------------------------------
# 権限管理 API
# ---------------------------------------------------------------------------


def test_permission_catalog_lists_menus_and_capabilities(auth: ProductionAuth) -> None:
    auth.user_with_permissions("perm-admin", ["menu.security_permissions"])
    headers = login("perm-admin")
    response = client.get("/api/security/permissions", headers=headers)
    assert response.status_code == 200
    codes = [item["code"] for item in response.json()["data"]]
    assert codes == [item.code for item in PERMISSION_CATALOG]
    groups = {item["code"]: item["group"] for item in response.json()["data"]}
    # capability のグループは NL2SQL / RAG と同じ「参照権限 / 実行権限 / 管理権限」（#791）。
    assert groups["agent.runs.view"] == groups["agent.audit.view"] == "参照権限"
    assert groups["agent.runs.operate"] == groups["agent.approvals.decide"] == "実行権限"
    assert groups["agent.admin"] == "管理権限"
    admin = next(item for item in response.json()["data"] if item["code"] == "agent.admin")
    assert "menu.agents" in admin["implies"]


def _target_items(headers: dict[str, str], kind: str, query: str = "") -> list[dict[str, Any]]:
    response = client.get(f"/api/security/access-targets/{kind}{query}", headers=headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["data"]["items"]
    return items


def test_access_targets_lists_agents(auth: ProductionAuth) -> None:
    with _agent("agent-target-215", "対象 Agent"):
        headers = login_configured_admin()
        agents = {item["id"]: item for item in _target_items(headers, "agents")}
        assert agents["agent-target-215"]["name"] == "対象 Agent"
        assert agents["agent-target-215"]["status"] == "enabled"
        assert "default" in agents

        # 範囲が制限された利用者には範囲内だけを見せる。
        auth.user_with_permissions(
            "scoped-perm-admin", ["menu.security_permissions"], agent_ids=["default"]
        )
        scoped = login("scoped-perm-admin")
        assert [item["id"] for item in _target_items(scoped, "agents")] == ["default"]
    # 検索・回答プロファイルの候補は持たない（RAG が判定する。#750）。
    response = client.get("/api/security/access-targets/search-answer-profiles", headers=headers)
    assert response.status_code in {403, 404}


def test_access_targets_search_and_page_on_server(auth: ProductionAuth) -> None:
    """権限管理の候補は `q` / `limit` / `offset` / `ids` でサーバー側で絞る（#608）。"""
    with (
        _agent("agent608-a", "検索 Agent A"),
        _agent("agent608-b", "検索 Agent B"),
        _agent("agent608-c", "検索 Agent C"),
    ):
        headers = login_configured_admin()
        assert [item["id"] for item in _target_items(headers, "agents", "?q=agent608-b")] == [
            "agent608-b"
        ]
        assert [item["id"] for item in _target_items(headers, "agents", "?ids=agent608-c")] == [
            "agent608-c"
        ]
        page = client.get(
            "/api/security/access-targets/agents?q=agent608-&limit=2&offset=0", headers=headers
        ).json()["data"]
        assert [item["id"] for item in page["items"]] == ["agent608-a", "agent608-b"]
        assert page["total"] == 3
        assert page["has_next"] is True
        rest = client.get(
            "/api/security/access-targets/agents?q=agent608-&limit=2&offset=2", headers=headers
        ).json()["data"]
        assert [item["id"] for item in rest["items"]] == ["agent608-c"]
        assert rest["has_next"] is False
        assert (
            client.get("/api/security/access-targets/agents?limit=101", headers=headers).status_code
            == 422
        )


def _put_access(headers: dict[str, str], role_id: str, **body: Any) -> Any:
    return client.put(f"/api/security/roles/{role_id}/access", json=body, headers=headers)


def test_update_role_access_saves_permissions_and_targets(auth: ProductionAuth) -> None:
    target = auth.create_role([])
    headers = login_configured_admin()
    response = _put_access(
        headers,
        target.role_id,
        version=1,
        permissions=["agent.runs.view", "menu.agents"],
        agent_ids=["default"],
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["permissions"] == ["agent.runs.view", "menu.agents"]
    assert data["agent_ids"] == ["default"]
    assert "search_answer_profile_ids" not in data
    assert response.headers["ETag"] == '"2"'
    # agent.admin を含むロールは対象を空に正規化する（制限を受けない）。
    admin_role = _put_access(
        headers,
        target.role_id,
        version=2,
        permissions=["agent.admin"],
        agent_ids=["default"],
    )
    assert admin_role.status_code == 200
    assert admin_role.json()["data"]["agent_ids"] == []
    # GET /security/roles にも Agent の権限と対象が出る。
    listed = client.get("/api/security/roles", headers=headers).json()["data"]
    role = next(item for item in listed if item["role_id"] == target.role_id)
    assert role["permissions"] == ["agent.admin"]


def test_update_role_access_validates_codes_and_targets(auth: ProductionAuth) -> None:
    target = auth.create_role([])
    headers = login_configured_admin()
    unknown_code = _put_access(headers, target.role_id, version=1, permissions=["rag.x"])
    assert unknown_code.status_code == 400
    unknown_agent = _put_access(headers, target.role_id, version=1, agent_ids=["no-such-agent"])
    assert unknown_agent.status_code == 400
    assert "エージェントが見つかりません" in unknown_agent.json()["error_messages"][0]
    stale = _put_access(headers, target.role_id, version=9, permissions=[])
    assert stale.status_code == 409
    builtin = _put_access(
        headers, "00000000-0000-0000-0000-000000000001", version=1, permissions=[]
    )
    assert builtin.status_code == 409


def test_update_role_access_prevents_privilege_escalation(auth: ProductionAuth) -> None:
    """操作者が持たない権限・範囲外の対象をロールに足すと 403。"""
    with _agent("agent-escalation-a"), _agent("agent-escalation-b"):
        auth.user_with_permissions(
            "delegate",
            ["menu.security_permissions", "agent.runs.view"],
            agent_ids=["agent-escalation-a"],
        )
        target = auth.create_role([])
        headers = login("delegate")
        for body, message in (
            ({"permissions": ["agent.admin"]}, "権限"),
            ({"permissions": ["agent.runs.operate"]}, "権限"),
            ({"permissions": ["menu.security_users"]}, "権限"),
            ({"agent_ids": ["agent-escalation-b"]}, "エージェント"),
        ):
            response = _put_access(headers, target.role_id, version=1, **body)
            assert response.status_code == 403, body
            assert message in response.json()["error_messages"][0]
        allowed = _put_access(
            headers,
            target.role_id,
            version=1,
            permissions=["agent.runs.view"],
            agent_ids=["agent-escalation-a"],
        )
        assert allowed.status_code == 200, allowed.text


def test_deleted_agent_is_removed_from_roles_and_does_not_block_saving(
    auth: ProductionAuth,
) -> None:
    """削除したエージェントはロールの対象範囲から外れ、残っていても保存を止めない（#750）。"""
    headers = login_configured_admin()
    with _agent("agent-deleted-750"), _agent("agent-kept-750"):
        target = auth.create_role([], agent_ids=["agent-deleted-750", "agent-kept-750"])
        deleted = client.delete("/api/agents/agent-deleted-750", headers=headers)
        assert deleted.status_code == 200, deleted.text
        stored = auth.store.get_role(target.role_id)
        assert stored is not None
        assert stored.agent_ids == {"agent-kept-750"}

        # 後始末に失敗して削除済みの ID が残ったロールも、表示どおりに保存すれば外れる。
        # 新しく足す未知の ID だけは 400。
        stale = auth.create_role([], agent_ids=["agent-gone-750", "agent-kept-750"])
        saved = _put_access(
            headers,
            stale.role_id,
            version=1,
            agent_ids=["agent-gone-750", "agent-kept-750"],
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["agent_ids"] == ["agent-kept-750"]
        unknown = _put_access(
            headers, stale.role_id, version=2, agent_ids=["agent-kept-750", "agent-new-750"]
        )
        assert unknown.status_code == 400
        assert "agent-new-750" in unknown.json()["error_messages"][0]


def test_retired_permission_code_left_in_db_is_ignored(auth: ProductionAuth) -> None:
    """migration 前の DB に `menu.dashboard`（#262）が残っていても、ログイン・権限管理の表示と保存・
    ロールの割り当てが壊れない（未知のコードは実効権限・表示から除き、保存で消える）。"""
    with _agent("agent-retired-a"):
        stale = auth.create_role(
            ["menu.dashboard", "agent.runs.view"], agent_ids=["agent-retired-a"]
        )
        auth.create_user("stale-user", [stale])
        stale_headers = login("stale-user")
        me = client.get("/api/auth/me", headers=stale_headers)
        assert me.status_code == 200
        assert set(me.json()["data"]["permissions"]) == {"agent.runs.view", "menu.runs"}
        assert client.get("/api/runs", headers=stale_headers).status_code == 200

        # 権限管理の画面は廃止コードを表示せず、表示どおりに保存すれば DB からも消える。
        headers = login_configured_admin()
        listed = client.get("/api/security/roles", headers=headers).json()["data"]
        shown = next(item for item in listed if item["role_id"] == stale.role_id)
        assert shown["permissions"] == ["agent.runs.view"]
        saved = _put_access(
            headers,
            stale.role_id,
            version=shown["version"],
            permissions=shown["permissions"],
            agent_ids=shown["agent_ids"],
        )
        assert saved.status_code == 200, saved.text
        stored = auth.store.get_role(stale.role_id)
        assert stored is not None
        assert stored.permissions == {"agent.runs.view"}

        # 廃止コードが残るロールも、同じ権限・範囲の操作者なら割り当てられる。
        leftover = auth.create_role(
            ["menu.dashboard", "agent.runs.view"], agent_ids=["agent-retired-a"]
        )
        manager = auth.create_role(
            ["menu.security_users", "agent.runs.view"], agent_ids=["agent-retired-a"]
        )
        auth.create_user("retired-manager", [manager])
        created = client.post(
            "/api/security/users",
            json={
                "login_user_id": "retired-member",
                "display_name": "メンバー",
                "role_ids": [leftover.role_id],
            },
            headers=login("retired-manager"),
        )
        assert created.status_code == 200, created.text


def test_role_assignment_respects_agent_scope(auth: ProductionAuth) -> None:
    """ユーザーへのロール割り当ても、操作者の権限・範囲に収まるロールだけ（共通の昇格防止）。"""
    with _agent("agent-assign-a"), _agent("agent-assign-b"):
        manager = auth.create_role(
            ["menu.security_users", "agent.runs.view"], agent_ids=["agent-assign-a"]
        )
        auth.create_user("user-manager", [manager])
        inside = auth.create_role(["agent.runs.view"], agent_ids=["agent-assign-a"])
        outside = auth.create_role(["agent.runs.view"], agent_ids=["agent-assign-b"])
        headers = login("user-manager")
        body = {"login_user_id": "member1", "display_name": "メンバー", "role_ids": []}
        denied = client.post(
            "/api/security/users", json={**body, "role_ids": [outside.role_id]}, headers=headers
        )
        assert denied.status_code == 403
        created = client.post(
            "/api/security/users", json={**body, "role_ids": [inside.role_id]}, headers=headers
        )
        assert created.status_code == 200, created.text


def test_migration_required_is_reported(auth: ProductionAuth, monkeypatch: MonkeyPatch) -> None:
    """production で認証のテーブルがなければ、migration の案内（409）を返す。"""
    from pr_system_settings.auth.errors import SecurityMigrationRequired

    auth.user_with_permissions("viewer-mig", ["agent.runs.view"])

    def missing(*_args: object, **_kwargs: object) -> None:
        raise SecurityMigrationRequired("PLATFORM_USERS")

    monkeypatch.setattr(auth.store, "get_user_by_login_user_id", missing)
    response = client.post(
        "/api/auth/login", json={"login_user_id": "viewer-mig", "password": USER_PASSWORD}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    assert "システムテーブル" in body["error_messages"][0]
    assert "agent_system_schema" in body["error_messages"][0]


def test_router_uses_local_debug_principal_with_all_permissions() -> None:
    """local のローカル利用者は全権限・対象範囲の制限なしとして router の RBAC に使う（#750）。"""
    from types import SimpleNamespace

    from app.security.dependencies import local_debug_principal

    connection = SimpleNamespace(state=SimpleNamespace(principal=local_debug_principal()))
    policy = agent_router._actor_policy(connection)
    assert "admin" in policy.roles
    assert policy.agent_ids is None
    # 利用者がいなければ何も許可しない（header の RBAC に切り替えない）。
    anonymous = agent_router._actor_policy(SimpleNamespace(state=SimpleNamespace()))
    assert anonymous.roles == set()
    assert anonymous.agent_ids == set()


class _RecordingCursor:
    def __init__(self, rows: dict[str, list[tuple[str, ...]]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._last = ""

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> None:
        self._last = statement
        self.executed.append((statement, dict(params or {})))

    def fetchall(self) -> list[tuple[str, ...]]:
        for table, rows in self.rows.items():
            if table in self._last:
                return rows
        return []


def test_oracle_store_reads_and_replaces_agent_role_details() -> None:
    from app.security.domain import RoleRecord
    from app.security.store import OracleSecurityStore

    store = OracleSecurityStore(connection_factory=lambda: None)  # type: ignore[arg-type,return-value]
    base = RoleRecord(
        role_id="role-1",
        role_code="OPS",
        display_name="運用",
        description="",
        is_built_in=False,
        archived=False,
        version=3,
    )
    cursor = _RecordingCursor(
        {
            "AGENT_ROLE_PERMISSIONS": [("role-1", "agent.runs.view")],
            "AGENT_ROLE_AGENTS": [("role-1", "default")],
        }
    )
    loaded = store._role_details(cursor, base)
    assert loaded.permissions == {"agent.runs.view"}
    assert loaded.agent_ids == {"default"}
    assert all(params == {"role_0": "role-1"} for _, params in cursor.executed)

    cursor = _RecordingCursor({})
    loaded.agent_ids = {"a-2", "a-1"}
    store._replace_role_details(cursor, loaded)
    statements = [" ".join(sql.split()) for sql, _ in cursor.executed]
    assert statements[:2] == [
        "DELETE FROM AGENT_ROLE_PERMISSIONS WHERE ROLE_ID = :role_id",
        "DELETE FROM AGENT_ROLE_AGENTS WHERE ROLE_ID = :role_id",
    ]
    assert not any("SEARCH_ANSWER_PROFILE" in statement for statement in statements)
    inserted_agents = [
        params["agent_id"]
        for sql, params in cursor.executed
        if "INSERT INTO AGENT_ROLE_AGENTS" in sql
    ]
    assert inserted_agents == ["a-1", "a-2"]


def test_oracle_store_reads_role_details_in_one_query_per_table() -> None:
    """ロールの一覧のロールの権限・対象範囲は、ロールの数によらず表ごとに 1 回で読む（#793）。"""
    from app.security.domain import RoleRecord
    from app.security.store import OracleSecurityStore

    store = OracleSecurityStore(connection_factory=lambda: None)  # type: ignore[arg-type,return-value]
    roles = [
        RoleRecord(
            role_id=f"role-{index}",
            role_code=f"R{index}",
            display_name=f"R{index}",
            description="",
            is_built_in=False,
            archived=False,
            version=1,
        )
        for index in range(25)
    ]
    cursor = _RecordingCursor(
        {
            "AGENT_ROLE_PERMISSIONS": [("role-3", "agent.runs.view"), ("role-3", "agent.admin")],
            "AGENT_ROLE_AGENTS": [("role-7", "default")],
        }
    )

    loaded = store._roles_details(cursor, roles)

    assert len(cursor.executed) == 2
    assert [role.role_id for role in loaded] == [role.role_id for role in roles]
    by_id = {role.role_id: role for role in loaded}
    assert by_id["role-3"].permissions == {"agent.runs.view", "agent.admin"}  # type: ignore[attr-defined]
    assert by_id["role-7"].agent_ids == {"default"}  # type: ignore[attr-defined]
    assert by_id["role-0"].permissions == set()  # type: ignore[attr-defined]


def test_platform_oracle_connection_reuses_one_pool(monkeypatch: MonkeyPatch) -> None:
    """共通 DB の接続は要求ごとに張らず、pool から借りる（#793）。"""
    from app import oracle_connection
    from app.settings import Settings

    created: list[dict[str, Any]] = []
    acquired: list[Any] = []

    class _Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_: object) -> None:
            self.close()

        def rollback(self) -> None:
            return None

    class _Pool:
        def acquire(self) -> _Connection:
            connection = _Connection()
            acquired.append(connection)
            return connection

        def close(self, force: bool = False) -> None:
            return None

    class _FakeOracledb:
        POOL_GETMODE_TIMEDWAIT = 3

        @staticmethod
        def create_pool(**kwargs: Any) -> _Pool:
            created.append(kwargs)
            return _Pool()

        @staticmethod
        def connect(**_kwargs: Any) -> Any:
            raise AssertionError("pool を使わずに接続している")

    pool = oracle_connection._PLATFORM_POOL
    pool.close()
    monkeypatch.setattr(pool, "_oracledb_loader", lambda: _FakeOracledb)
    settings = Settings(
        _env_file=None,
        oracle_user="ADMIN",
        oracle_password="secret",  # nosec B106 - テスト用
        oracle_dsn="suiteadb_high",
        oracle_client_lib_dir="",
        oracle_pool_max_connections=6,
    )
    monkeypatch.setattr(oracle_connection, "_current_settings", lambda value: value or settings)
    try:
        for _ in range(3):
            with oracle_connection.platform_oracle_connection():
                pass
        with oracle_connection.connect_platform_oracle() as _connection:
            pass
    finally:
        oracle_connection.close_platform_oracle_pool()

    assert len(created) == 1
    assert (created[0]["min"], created[0]["max"]) == (1, 6)
    assert created[0]["user"] == "ADMIN"
    assert len(acquired) == 4
    assert all(connection.closed for connection in acquired)


def test_platform_oracle_connection_uses_platform_settings() -> None:
    from app.oracle_connection import platform_oracle_connect_kwargs
    from app.settings import Settings

    settings = Settings(
        _env_file=None,
        oracle_user="ADMIN",
        oracle_password="secret",  # nosec B106 - テスト用
        oracle_dsn="suiteadb_high",
        oracle_client_lib_dir="/opt/instantclient",
        oracle_wallet_password="wallet-secret",  # nosec B106 - テスト用
    )
    kwargs = platform_oracle_connect_kwargs(settings)
    assert kwargs["user"] == "ADMIN"
    assert kwargs["dsn"] == "suiteadb_high"
    assert kwargs["wallet_location"] == "/opt/instantclient/network/admin"
    assert kwargs["wallet_password"] == "wallet-secret"  # nosec B105 - テスト用
    with pytest.raises(RuntimeError, match="PLATFORM_ORACLE_DSN"):
        platform_oracle_connect_kwargs(Settings(_env_file=None, oracle_dsn="", oracle_user=""))


def test_wallet_alias_is_connected_without_the_descriptor_retry(tmp_path: Path) -> None:
    """Wallet の別名の `(retry_count=20)(retry_delay=3)` で、DB の停止中に長く待たない（#1212）。"""
    from app.oracle_connection import oracle_connect_kwargs

    (tmp_path / "tnsnames.ora").write_text(
        "suiteadb_high = (description= (retry_count=20)(retry_delay=3)"
        "(address=(protocol=tcps)(port=1522)(host=adb.example.com))"
        "(connect_data=(service_name=x_suiteadb_high.adb.oraclecloud.com)))\n",
        encoding="utf-8",
    )
    kwargs = oracle_connect_kwargs(
        SimpleNamespace(
            oracle_user="ADMIN",
            oracle_dsn="suiteadb_high",
            oracle_password="",
            oracle_wallet_dir=str(tmp_path),
            oracle_wallet_password="",
            oracle_tcp_connect_timeout_seconds=10.0,
        )
    )

    dsn = str(kwargs["dsn"])
    assert dsn.startswith("(description=")
    assert "retry_count" not in dsn
    assert "retry_delay" not in dsn
    assert "service_name=x_suiteadb_high.adb.oraclecloud.com" in dsn
    assert (kwargs["retry_count"], kwargs["retry_delay"]) == (0, 0)
    assert kwargs["config_dir"] == str(tmp_path)


@pytest.mark.parametrize("cli", [False, True], ids=["server", "cli"])
def test_pool_kwargs_strip_descriptor_retry_only_in_cli(cli: bool) -> None:
    """常駐の server の pool は接続記述子の DSN をそのまま使う（一時の切断に強いまま）。

    CLI（`enable_cli_connect_mode`）だけ、記述子の `(retry_count=20)(retry_delay=3)` も外し、短い
    `tcp_connect_timeout` にする（DB の停止中に長く待たない。#1779）。
    """
    from pr_backend_core.oracle_dsn import CLI_TCP_CONNECT_TIMEOUT_SECONDS, enable_cli_connect_mode

    from app.oracle_connection import _pool_kwargs
    from app.settings import Settings

    descriptor = (
        "(description= (retry_count=20)(retry_delay=3)"
        "(address=(protocol=tcps)(port=1522)(host=adb.example.com))"
        "(connect_data=(service_name=x_suiteadb_high.adb.oraclecloud.com)))"
    )
    settings = Settings(
        _env_file=None,
        oracle_user="ADMIN",
        oracle_password="secret",  # nosec B106 - テスト用
        oracle_dsn=descriptor,
        oracle_client_lib_dir="",
        oracle_wallet_dir="",
        oracle_tcp_connect_timeout_seconds=10.0,
    )
    if cli:
        enable_cli_connect_mode()
    kwargs = _pool_kwargs(settings)

    assert (kwargs["retry_count"], kwargs["retry_delay"]) == (0, 0)
    if cli:
        assert kwargs["dsn"] == descriptor.replace("(retry_count=20)(retry_delay=3)", "")
        assert kwargs["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS
    else:
        assert kwargs["dsn"] == descriptor
        assert kwargs["tcp_connect_timeout"] == 10.0


def _read_missing_oci_config(headers: dict[str, str], tmp_path: Any) -> int:
    response = client.post(
        "/api/settings/oci/config/read",
        json={"config_file": str(tmp_path / "missing-config"), "profile": "DEFAULT"},
        headers=headers,
    )
    return response.status_code


def test_system_settings_actions_use_menu_permissions_for_session(
    monkeypatch: MonkeyPatch, auth: ProductionAuth, tmp_path: Any
) -> None:
    """共通のシステム設定の保存・操作は Cookie ではメニュー権限で許可（RAG / NL2SQL と同じ）。"""
    auth.user_with_permissions("oci-admin", ["menu.settings_oci"])
    auth.user_with_permissions("model-admin", ["menu.settings_model"])
    oci_admin = login("oci-admin")
    model_admin = login("model-admin")
    # 権限があれば認可を通り、処理の結果（ファイルがない）になる。
    assert _read_missing_oci_config(oci_admin, tmp_path) not in {401, 403}
    assert _read_missing_oci_config(model_admin, tmp_path) == 403
    # Agent 固有の設定は agent.admin のまま。
    assert client.patch("/api/settings/tool-policy", json={}, headers=oci_admin).status_code == 403

    # router 側の判定（require_system_settings_write）も同じ権限を確認する。
    monkeypatch.setattr(security_dependencies, "permission_for_route", lambda *_args: None)
    assert _read_missing_oci_config(model_admin, tmp_path) == 403
    assert _read_missing_oci_config(oci_admin, tmp_path) not in {401, 403}


def test_api_key_menu_can_list_and_revoke_other_users_keys(auth: ProductionAuth) -> None:
    """専用メニュー権限だけで全員のキーを管理でき、権限なしでは一覧も削除も拒否する。"""
    auth.user_with_permissions("key-owner", [])
    auth.user_with_permissions("key-manager", ["menu.security_api_keys"])
    owner = login("key-owner")
    issued = client.post(
        "/api/account/api-keys", headers=owner, json={"name": "検証キー A", "products": ["agent"]}
    )
    assert issued.status_code == 200
    key_id = issued.json()["data"]["key"]["id"]
    assert client.get("/api/security/api-keys", headers=owner).status_code == 403
    assert client.delete(f"/api/security/api-keys/{key_id}", headers=owner).status_code == 403
    manager = login("key-manager")
    listed = client.get("/api/security/api-keys", headers=manager)
    assert listed.status_code == 200
    assert key_id in {key["id"] for key in listed.json()["data"]["keys"]}
    assert client.delete(f"/api/security/api-keys/{key_id}", headers=manager).status_code == 200
    assert client.get("/api/account/api-keys", headers=owner).json()["data"]["keys"] == []
    assert "menu.security_api_keys" in expand_permissions(["agent.admin"])
