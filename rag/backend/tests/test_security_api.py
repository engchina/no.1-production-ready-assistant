"""RAG の認証/RBAC の API テスト（#214）。

- production mode のログイン（DB ユーザー・構成管理者）・CSRF・強制パスワード変更
- 権限の既定拒否（manifest 未登録・権限なし 403）と manifest の完全性
- `/security/*`（権限カタログ・対象の一覧・ロールの権限と対象範囲の保存と昇格防止）
- 利用者から作る監査 / 対象範囲の context（client の header の範囲は使わない）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_ID
from pr_system_settings.auth.errors import (
    CSRF_INVALID_CODE,
    PASSWORD_CHANGE_REQUIRED_CODE,
    ROUTE_FORBIDDEN_CODE,
    ROUTE_FORBIDDEN_CODES,
)
from pytest import MonkeyPatch

from app.api.routes import knowledge_bases as knowledge_bases_route
from app.api.routes import search_answer_profile_knowledge as profile_knowledge_route
from app.api.routes import search_answer_profiles as search_answer_profiles_route
from app.config import get_settings
from app.main import app
from app.rag.request_context import AuditRequestContext, current_audit_request_context
from app.schemas.knowledge_base import KnowledgeBaseDetail, KnowledgeBaseStatus
from app.schemas.search_answer_profile import SearchAnswerProfileDetail, SearchAnswerProfileStatus
from app.security import dependencies as security_dependencies
from app.security import router as security_router_module
from app.security.domain import Principal
from app.security.permissions import (
    ALL_PERMISSION_CODES,
    AUTHENTICATED_WITHOUT_PERMISSION,
    LOGIN_ONLY_API_PATHS,
    OPEN_API_OPERATIONS,
    PERMISSION_CATALOG,
    PUBLIC_API_PATHS,
    ROUTE_PERMISSIONS,
    SERVICE_TOKEN_API_PATHS,
    UNCLASSIFIED_PERMISSION,
    expand_permissions,
    permission_for_route,
)
from app.security.service import SecurityApiError
from tests.security_support import (
    CONFIGURED_ADMIN_PASSWORD,
    USER_PASSWORD,
    ProductionAuth,
    enable_production_auth,
    login,
    login_configured_admin,
    session_headers,
)
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
NOW = datetime(2026, 9, 27, tzinfo=UTC)
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# fake: 利用者の対象範囲（監査 context）で絞る検索・回答プロファイル・KB
# ---------------------------------------------------------------------------


class ScopedFakeOracle:
    """Oracle の SQL と同じく、現在の監査 context
    の対象範囲で検索・回答プロファイル・KB を絞る fake。"""

    VIEW_IDS = ("bv-1", "bv-2", "bv-3")
    BASE_IDS = ("kb-1", "kb-2", "kb-3")

    def __init__(self) -> None:
        self.contexts: list[AuditRequestContext] = []

    def _record(self) -> AuditRequestContext:
        context = current_audit_request_context()
        self.contexts.append(context)
        return context

    def _visible_views(self) -> list[str]:
        allowed = self._record().allowed_search_answer_profile_ids
        return [item for item in self.VIEW_IDS if allowed is None or item in allowed]

    def _visible_bases(self) -> list[str]:
        allowed = self._record().allowed_knowledge_base_ids
        return [item for item in self.BASE_IDS if allowed is None or item in allowed]

    @staticmethod
    def _view(view_id: str) -> SearchAnswerProfileDetail:
        return SearchAnswerProfileDetail(
            id=view_id,
            name=f"検索・回答プロファイル {view_id}",
            status=SearchAnswerProfileStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )

    @staticmethod
    def _base(base_id: str) -> KnowledgeBaseDetail:
        return KnowledgeBaseDetail(
            id=base_id,
            name=f"KB {base_id}",
            status=KnowledgeBaseStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )

    async def ensure_default_search_answer_profile(self) -> SearchAnswerProfileDetail:
        return self._view("bv-1")

    @staticmethod
    def _matching(
        ids: list[str],
        *,
        name: str,
        query: object = None,
        selected: object = None,
    ) -> list[str]:
        # Oracle の SQL と同じく、名前の部分一致（q）と ID の指定（ids）で絞る（#608）。
        values = ids
        if isinstance(selected, (list, tuple)):
            values = [item for item in values if item in selected]
        if isinstance(query, str) and query:
            values = [item for item in values if query.lower() in f"{name} {item}".lower()]
        return values

    @staticmethod
    def _page(values: list[str], limit: object, offset: object) -> list[str]:
        start = offset if isinstance(offset, int) else 0
        return values[start : start + limit] if isinstance(limit, int) else values[start:]

    async def list_search_answer_profiles(
        self, **kwargs: object
    ) -> list[SearchAnswerProfileDetail]:
        values = self._matching(
            self._visible_views(),
            name="検索・回答プロファイル",
            query=kwargs.get("query"),
            selected=kwargs.get("search_answer_profile_ids"),
        )
        return [
            self._view(item)
            for item in self._page(values, kwargs.get("limit"), kwargs.get("offset"))
        ]

    async def count_search_answer_profiles(self, **kwargs: object) -> int:
        return len(
            self._matching(
                self._visible_views(),
                name="検索・回答プロファイル",
                query=kwargs.get("query"),
                selected=kwargs.get("search_answer_profile_ids"),
            )
        )

    async def get_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail | None:
        return (
            self._view(search_answer_profile_id)
            if search_answer_profile_id in self._visible_views()
            else None
        )

    async def list_knowledge_bases(self, **kwargs: object) -> list[KnowledgeBaseDetail]:
        values = self._matching(
            self._visible_bases(),
            name="KB",
            query=kwargs.get("query"),
            selected=kwargs.get("knowledge_base_ids"),
        )
        return [
            self._base(item)
            for item in self._page(values, kwargs.get("limit"), kwargs.get("offset"))
        ]

    async def count_knowledge_bases(self, **kwargs: object) -> int:
        return len(
            self._matching(
                self._visible_bases(),
                name="KB",
                query=kwargs.get("query"),
                selected=kwargs.get("knowledge_base_ids"),
            )
        )

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail | None:
        return self._base(knowledge_base_id) if knowledge_base_id in self._visible_bases() else None

    async def list_access_target_ids(self) -> tuple[set[str], set[str]]:
        # 権限管理の ID 検証は範囲を外して呼ばれる（範囲外は 403、存在しない ID は 400）。
        context = self._record()
        assert context.allowed_search_answer_profile_ids is None
        assert context.allowed_knowledge_base_ids is None
        return set(self.VIEW_IDS), set(self.BASE_IDS)


@pytest.fixture
def scoped_oracle(monkeypatch: MonkeyPatch) -> ScopedFakeOracle:
    fake = ScopedFakeOracle()
    for module in (
        search_answer_profiles_route,
        profile_knowledge_route,
        knowledge_bases_route,
        security_router_module,
    ):
        monkeypatch.setattr(module, "OracleClient", lambda *_args, **_kwargs: fake)
    return fake


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    return enable_production_auth(monkeypatch)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


# OpenAPI の生成（`app.openapi()`）は 1 回 1〜3 秒かかり、結果は app に保存される。
# `-n`（xdist）でも manifest のテストを同じ worker で流し、生成を 1 回にする（#401）。
_OPENAPI_GROUP = pytest.mark.xdist_group("rag-openapi")


def _api_operations() -> list[tuple[str, str]]:
    operations: list[tuple[str, str]] = []
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in methods:
            if method.upper() in HTTP_METHODS:
                operations.append((method.upper(), path.removeprefix("/api")))
    return operations


@_OPENAPI_GROUP
def test_every_api_route_is_classified_by_manifest() -> None:
    """全 API（method × path）が manifest に登録されている（登録外は既定で拒否）。"""
    operations = _api_operations()
    assert len(operations) > 170
    unclassified = [
        f"{method} {path}"
        for method, path in operations
        if (permissions := permission_for_route(method, path)) is not None
        and UNCLASSIFIED_PERMISSION in permissions
    ]
    assert unclassified == []
    open_operations = {
        (method, path) for method, path in operations if permission_for_route(method, path) is None
    }
    # MCP（#232）は認証済みなら通し、権限はツールごとに判定する。
    assert {path for _method, path in open_operations} == (
        set(PUBLIC_API_PATHS)
        | set(AUTHENTICATED_WITHOUT_PERMISSION)
        | set(SERVICE_TOKEN_API_PATHS)
        | set(LOGIN_ONLY_API_PATHS)
    )
    # 権限なしで通す path に method を足したら、ここで気づく（method 単位で照合する。#476）。
    assert open_operations == set(OPEN_API_OPERATIONS)


@_OPENAPI_GROUP
def test_manifest_entries_match_existing_routes_and_known_permissions() -> None:
    """manifest の明示登録は実在する route だけで、権限コードはカタログにあるもの。"""
    operations = set(_api_operations())
    assert set(ROUTE_PERMISSIONS) - operations == set()
    for permissions in ROUTE_PERMISSIONS.values():
        assert permissions
        assert permissions <= ALL_PERMISSION_CODES


def test_manifest_denies_unknown_routes_by_default() -> None:
    assert permission_for_route("GET", "/unknown") == frozenset({UNCLASSIFIED_PERMISSION})
    assert permission_for_route("DELETE", "/search-answer-profiles") == frozenset(
        {UNCLASSIFIED_PERMISSION}
    )
    assert permission_for_route("POST", "/security/permissions") == frozenset(
        {UNCLASSIFIED_PERMISSION}
    )


def _perm(method: str, path: str) -> set[str]:
    return set(permission_for_route(method, path) or ())


def test_manifest_key_assignments() -> None:
    """作成・アーカイブは manage、利用系の読み取りは複数画面のいずれか。"""
    assert _perm("POST", "/search-answer-profiles") == {"rag.search_answer_profiles.manage"}
    assert _perm("POST", "/search-answer-profiles/{search_answer_profile_id}/archive") == {
        "rag.search_answer_profiles.manage"
    }
    assert _perm("PATCH", "/search-answer-profiles/{search_answer_profile_id}") == {
        "menu.search_answer_profiles"
    }
    assert _perm("POST", "/knowledge-bases") == {"rag.knowledge_bases.manage"}
    assert _perm("POST", "/knowledge-bases/{knowledge_base_id}/archive") == {
        "rag.knowledge_bases.manage"
    }
    assert _perm("GET", "/feedback") == {"menu.feedback"}
    assert _perm("GET", "/feedback/{feedback_id}") == {"menu.feedback"}
    assert _perm("GET", "/feedback/{feedback_id}/evaluation-case") == {"menu.feedback"}
    assert _perm("POST", "/feedback/{feedback_id}/approved-faq") == {"rag.feedback.manage"}
    assert _perm("POST", "/feedback") == {"menu.search", "menu.chat"}
    # システムテーブルは運用設定の専用の画面（#658）。データベース設定の権限では開けない。
    assert _perm("GET", "/settings/database/system-tables") == {"menu.settings_system_tables"}
    assert _perm("POST", "/settings/database/system-tables/initialize") == {
        "rag.system_tables.manage"
    }
    # 参照先のない行の削除（#511）は作成・更新と同じ権限。
    assert _perm("POST", "/settings/database/system-tables/orphaned-rows/delete") == {
        "rag.system_tables.manage"
    }
    assert {"menu.search", "menu.chat", "menu.upload", "menu.file_list"} <= _perm(
        "GET", "/knowledge-bases"
    )
    assert _perm("GET", "/search-answer-profiles") == {
        "menu.search",
        "menu.chat",
        "menu.feedback",
        "menu.search_answer_profiles",
        "menu.evaluation",
    }
    assert _perm("GET", "/security/roles/{role_id}") == {
        "menu.security_users",
        "menu.security_roles",
        "menu.security_permissions",
    }
    assert _perm("POST", "/security/roles") == {"menu.security_roles"}
    assert _perm("PATCH", "/security/users/{user_uuid}") == {"menu.security_users"}
    assert _perm("PUT", "/security/roles/{role_id}/access") == {"menu.security_permissions"}
    # 全体の既定の入口（#528）: 設定の概要と、文書解析の「解析後の処理」。
    assert _perm("GET", "/settings/pipeline") == {"menu.settings_pipeline"}
    assert _perm("PATCH", "/settings/pipeline") == {"menu.settings_pipeline"}
    assert _perm("PATCH", "/settings/parser-adapters") == {"menu.settings_parser_adapters"}
    assert _perm("PATCH", "/settings/extraction-fields") == {"menu.settings_parser_adapters"}
    # 回答の注意書き（設定は製品ごと。#2405・#2449）。
    assert _perm("GET", "/settings/answer-notice") == {"menu.settings_answer_notice"}
    assert _perm("PUT", "/settings/answer-notice") == {"menu.settings_answer_notice"}


def test_capabilities_imply_their_menu() -> None:
    assert "menu.search_answer_profiles" in expand_permissions(
        {"rag.search_answer_profiles.manage"}
    )
    assert "menu.knowledge_bases" in expand_permissions({"rag.knowledge_bases.manage"})
    assert "menu.feedback" in expand_permissions({"rag.feedback.manage"})
    assert "menu.settings_system_tables" in expand_permissions({"rag.system_tables.manage"})
    # メニュー権限は capability を暗黙に含まない（昇格しない）。
    assert expand_permissions({"menu.search_answer_profiles"}) == {"menu.search_answer_profiles"}
    assert expand_permissions({"unknown.code"}) == set()


def test_unclassified_route_is_denied_even_for_logged_in_user(
    monkeypatch: MonkeyPatch, auth: ProductionAuth
) -> None:
    """manifest が未登録を返す route は、ログイン済みでも 403（SYSTEM_ADMIN も同じ）。"""
    auth.create_user("root", system_admin=True)
    headers = login(client, "root")
    assert client.get("/api/security/permissions", headers=headers).status_code == 200

    monkeypatch.setattr(
        security_dependencies,
        "permission_for_route",
        lambda method, path: frozenset({UNCLASSIFIED_PERMISSION}),
    )
    response = client.get("/api/security/permissions", headers=headers)
    assert response.status_code == 403
    assert response.json()["error_messages"] == ["この API は権限一覧に登録されていません。"]


# ---------------------------------------------------------------------------
# production mode のログイン
# ---------------------------------------------------------------------------


def test_production_rejects_protected_api_without_session(auth: ProductionAuth) -> None:
    response = client.get("/api/security/permissions")
    assert response.status_code == 401
    assert response.json()["error_messages"] == ["ログインしてください。"]


def test_production_public_paths_do_not_require_login(auth: ProductionAuth) -> None:
    assert client.get("/api/health").status_code == 200
    # 未定義の path は認証の前に 404。
    assert client.get("/api/not-defined").status_code == 404


def test_database_user_login_me_and_logout(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    user = auth.user_with_permissions(
        "analyst",
        ["menu.search", "rag.feedback.manage"],
        search_answer_profile_ids=["bv-2"],
        knowledge_base_ids=["kb-1"],
    )
    login_response = client.post(
        "/api/auth/login", json={"login_user_id": "analyst", "password": USER_PASSWORD}
    )
    assert login_response.status_code == 200
    data = login_response.json()["data"]
    assert data["user_uuid"] == user.user_uuid
    assert data["is_system_admin"] is False
    # ロールの表示名は role_codes と同じ順（サイドナビのアカウント欄に出す。#1644）。
    assert len(data["role_display_names"]) == len(data["role_codes"]) == 1
    assert data["role_display_names"][0].startswith("ロール ")
    # implies を展開した実効権限。
    assert set(data["permissions"]) == {"menu.search", "rag.feedback.manage", "menu.feedback"}
    assert data["allowed_search_answer_profile_ids"] == ["bv-2"]
    assert data["allowed_knowledge_base_ids"] == ["kb-1"]
    assert data["debug_mode"] is False
    assert data["password_change_allowed"] is True
    assert "chat_enabled" not in data
    headers = session_headers(login_response)
    set_cookie = login_response.headers.get_list("set-cookie")
    assert any(item.startswith("pr_session=") and "HttpOnly" in item for item in set_cookie)
    assert any(item.startswith("pr_csrf=") for item in set_cookie)

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["login_user_id"] == "analyst"

    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_login_revokes_session_cookie_the_browser_already_had(auth: ProductionAuth) -> None:
    """Cookie は 3 製品で共有する（#1565）。

    ログインし直したら、ブラウザが持っていた古いセッションは失効する。
    """
    auth.user_with_permissions("analyst", ["menu.search"])
    auth.user_with_permissions("reviewer", ["menu.search"])
    first = session_headers(
        client.post("/api/auth/login", json={"login_user_id": "analyst", "password": USER_PASSWORD})
    )
    assert client.get("/api/auth/me", headers=first).status_code == 200

    second_response = client.post(
        "/api/auth/login",
        json={"login_user_id": "reviewer", "password": USER_PASSWORD},
        headers={"cookie": first["cookie"]},
    )
    assert second_response.status_code == 200
    second = session_headers(second_response)
    assert client.get("/api/auth/me", headers=first).status_code == 401
    me = client.get("/api/auth/me", headers=second)
    assert me.status_code == 200
    assert me.json()["data"]["login_user_id"] == "reviewer"

    # 構成管理者の token（DB の行を持たない）を持っていても、ログインは妨げない。
    admin = login_configured_admin(client)
    again = client.post(
        "/api/auth/login",
        json={"login_user_id": "analyst", "password": USER_PASSWORD},
        headers={"cookie": admin["cookie"]},
    )
    assert again.status_code == 200


def test_login_rejects_invalid_credentials(auth: ProductionAuth) -> None:
    auth.user_with_permissions("analyst", ["menu.search"])
    response = client.post(
        "/api/auth/login", json={"login_user_id": "analyst", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error_messages"] == [
        "ログインユーザーIDまたはパスワードを確認してください。"
    ]


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
    assert data["allowed_search_answer_profile_ids"] is None
    assert data["allowed_knowledge_base_ids"] is None
    # 構成管理者の token は 3 製品で共通の接頭辞（ほかの製品でも同じ token で使える。#1565）。
    assert str(response.cookies.get("pr_session")).startswith("platform-system-admin-v1.")
    headers = session_headers(response)
    assert client.get("/api/security/permissions", headers=headers).status_code == 200


def test_configured_admin_rejects_wrong_password(auth: ProductionAuth) -> None:
    response = client.post(
        "/api/auth/login", json={"login_user_id": "system_admin", "password": "WrongPass12345"}
    )
    assert response.status_code == 401


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


def test_permission_denied_returns_403(auth: ProductionAuth) -> None:
    auth.user_with_permissions("searcher", ["menu.search"])
    auth.user_with_permissions("builder", ["menu.security_permissions"])
    searcher = login(client, "searcher")
    response = client.get("/api/security/permissions", headers=searcher)
    assert response.status_code == 403
    assert response.json()["error_messages"] == ["この機能を利用する権限がありません。"]
    builder = login(client, "builder")
    assert client.get("/api/security/permissions", headers=builder).status_code == 200


def test_user_without_roles_is_denied_everything_except_auth(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """ロールのない利用者は既定で何もできない（ログイン系だけ使える）。"""
    auth.create_user("nobody")
    headers = login(client, "nobody")
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    for method, path in (
        ("GET", "/api/search-answer-profiles"),
        ("POST", "/api/search-answer-profiles/bv-1/domain-keywords/import/preview"),
        ("POST", "/api/search-answer-profiles/bv-1/domain-keywords/import"),
        ("GET", "/api/knowledge-bases"),
        ("GET", "/api/security/permissions"),
        ("GET", "/api/security/users"),
        ("GET", "/api/settings/oci"),
    ):
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403, path
        # 経路の権限拒否は専用の error_code（frontend は権限なしの画面へ移す。#224）。
        assert response.json()["error_code"] == ROUTE_FORBIDDEN_CODE, path


def test_state_changing_request_requires_csrf_token(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    auth.user_with_permissions("security-admin", ["menu.security_roles"])
    headers = login(client, "security-admin")
    body = {"role_code": "READER", "display_name": "閲覧者", "description": ""}
    without_csrf = {"cookie": headers["cookie"]}
    response = client.post("/api/security/roles", json=body, headers=without_csrf)
    assert response.status_code == 403
    assert "安全性" in response.json()["error_messages"][0]
    assert response.json()["error_code"] == CSRF_INVALID_CODE
    wrong_csrf = {**headers, "X-CSRF-Token": "tampered"}
    assert client.post("/api/security/roles", json=body, headers=wrong_csrf).status_code == 403
    created = client.post("/api/security/roles", json=body, headers=headers)
    assert created.status_code == 200
    assert created.json()["data"]["permissions"] == []


def test_forced_password_change_blocks_other_apis(auth: ProductionAuth) -> None:
    role = auth.create_role(["menu.security_permissions"])
    auth.create_user("new-user", [role], force_password_change=True)
    headers = login(client, "new-user")
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    response = client.get("/api/security/permissions", headers=headers)
    assert response.status_code == 403
    assert response.json()["error_messages"] == ["初回パスワード変更を完了してください。"]
    assert response.json()["error_code"] == PASSWORD_CHANGE_REQUIRED_CODE


def test_local_mode_uses_all_permissions_without_login() -> None:
    """local は全権限・対象範囲の制限なしのローカル利用者（ログイン不要）。"""
    assert get_settings().auth_mode == "local"
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["debug_mode"] is True
    assert data["is_system_admin"] is True
    assert set(data["permissions"]) == set(ALL_PERMISSION_CODES)
    assert data["allowed_search_answer_profile_ids"] is None
    assert data["allowed_knowledge_base_ids"] is None
    assert data["password_change_allowed"] is False


# ---------------------------------------------------------------------------
# 監査 / 対象範囲の context
# ---------------------------------------------------------------------------


def test_production_context_comes_from_principal_not_headers(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """production は X-User-ID と x-rag-allowed-* を使わず、利用者から範囲を決める。"""
    user = auth.user_with_permissions(
        "viewer", ["menu.search"], search_answer_profile_ids=["bv-2"], knowledge_base_ids=["kb-3"]
    )
    headers = {
        **login(client, "viewer"),
        "X-User-ID": "spoofed-user",
        "x-rag-allowed-knowledge-base-ids": "kb-1,kb-2,kb-3",
        "x-rag-allowed-document-ids": "doc-1",
    }
    response = client.get("/api/search-answer-profiles", headers=headers)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]["items"]] == ["bv-2"]
    context = scoped_oracle.contexts[-1]
    assert context.allowed_search_answer_profile_ids == frozenset({"bv-2"})
    assert context.allowed_knowledge_base_ids == frozenset({"kb-3"})
    assert context.allowed_document_ids is None
    # production は client の X-Tenant-ID を使わない（テストの client は常に送っている。#225）。
    assert context.tenant_id_hash is None

    expected = _principal_context_user_hash(user.user_uuid)
    assert context.user_id_hash == expected


def _principal_context_user_hash(user_uuid: str) -> str | None:
    from app.rag.request_context import audit_request_context_for_principal

    return audit_request_context_for_principal(
        {},
        request_id="x",
        user_uuid=user_uuid,
        allowed_search_answer_profile_ids=None,
        allowed_knowledge_base_ids=None,
    ).user_id_hash


def test_local_mode_keeps_header_scope(scoped_oracle: ScopedFakeOracle) -> None:
    """local はこれまでどおり header の利用者・範囲を使う。"""
    response = client.get(
        "/api/knowledge-bases", headers={"x-rag-allowed-knowledge-base-ids": "kb-2"}
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]["items"]] == ["kb-2"]
    context = scoped_oracle.contexts[-1]
    assert context.allowed_knowledge_base_ids == frozenset({"kb-2"})
    assert context.user_id_hash is not None


def test_scope_filters_search_answer_profile_list_and_detail(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """範囲外の検索・回答プロファイルは一覧に出ず、取得・更新は 404（存在しないものとして扱う）。"""
    auth.user_with_permissions(
        "viewer", ["menu.search_answer_profiles"], search_answer_profile_ids=["bv-1"]
    )
    auth.user_with_permissions("view-manager", ["rag.search_answer_profiles.manage"])
    headers = login(client, "viewer")
    listed = client.get("/api/search-answer-profiles", headers=headers)
    assert [item["id"] for item in listed.json()["data"]["items"]] == ["bv-1"]
    assert listed.json()["data"]["total"] == 1
    assert client.get("/api/search-answer-profiles/bv-1", headers=headers).status_code == 200
    assert client.get("/api/search-answer-profiles/bv-2", headers=headers).status_code == 404
    for suffix in ("import/preview", "import"):
        response = client.post(
            f"/api/search-answer-profiles/bv-2/domain-keywords/{suffix}",
            files={"file": ("keywords.xlsx", b"unread", "application/octet-stream")},
            headers=headers,
        )
        assert response.status_code == 404
    # 作成・アーカイブは rag.search_answer_profiles.manage が必要。
    assert (
        client.post("/api/search-answer-profiles/bv-1/archive", headers=headers).status_code == 403
    )

    manager = login(client, "view-manager")
    listed = client.get("/api/search-answer-profiles", headers=manager)
    assert [item["id"] for item in listed.json()["data"]["items"]] == ["bv-1", "bv-2", "bv-3"]


def test_scope_filters_knowledge_bases(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    auth.user_with_permissions("builder", ["menu.knowledge_bases"], knowledge_base_ids=["kb-2"])
    headers = login(client, "builder")
    listed = client.get("/api/knowledge-bases", headers=headers)
    assert [item["id"] for item in listed.json()["data"]["items"]] == ["kb-2"]
    assert client.get("/api/knowledge-bases/kb-1", headers=headers).status_code == 404
    assert client.get("/api/knowledge-bases/kb-2", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# /security/*
# ---------------------------------------------------------------------------


def test_permission_catalog_requires_permission_management(auth: ProductionAuth) -> None:
    auth.user_with_permissions("perm-admin", ["menu.security_permissions"])
    auth.user_with_permissions("role-admin", ["menu.security_roles"])
    headers = login(client, "perm-admin")
    response = client.get("/api/security/permissions", headers=headers)
    assert response.status_code == 200
    items = response.json()["data"]
    assert [item["code"] for item in items] == [item.code for item in PERMISSION_CATALOG]
    by_code = {item["code"]: item for item in items}
    assert by_code["menu.security_permissions"]["group"] == "セキュリティ設定"
    assert by_code["menu.settings_system_tables"]["group"] == "運用設定"
    assert by_code["menu.security_users"]["group"] == "ユーザーとロール"
    assert by_code["menu.settings_oci"]["group"] == "システム設定"
    assert by_code["rag.search_answer_profiles.manage"]["implies"] == [
        "menu.search_answer_profiles"
    ]
    role_admin = login(client, "role-admin")
    assert client.get("/api/security/permissions", headers=role_admin).status_code == 403


def _access_target_ids(headers: dict[str, str], kind: str, query: str = "") -> list[str]:
    response = client.get(f"/api/security/access-targets/{kind}{query}", headers=headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["data"]["items"]]


def test_access_targets_are_limited_to_actor_scope(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    auth.user_with_permissions(
        "limited",
        ["menu.security_permissions"],
        search_answer_profile_ids=["bv-1"],
        knowledge_base_ids=["kb-2", "kb-3"],
    )
    auth.user_with_permissions(
        "view-manager", ["menu.security_permissions", "rag.search_answer_profiles.manage"]
    )
    limited_headers = login(client, "limited")
    limited = client.get(
        "/api/security/access-targets/search-answer-profiles", headers=limited_headers
    )
    assert limited.status_code == 200
    data = limited.json()["data"]
    assert data["items"] == [
        {
            "id": "bv-1",
            "name": "検索・回答プロファイル bv-1",
            "status": "ACTIVE",
            "description": None,
        }
    ]
    assert data["total"] == 1
    assert _access_target_ids(limited_headers, "knowledge-bases") == ["kb-2", "kb-3"]

    manager = login(client, "view-manager")
    assert _access_target_ids(manager, "search-answer-profiles") == ["bv-1", "bv-2", "bv-3"]
    assert _access_target_ids(manager, "knowledge-bases") == []

    admin = login_configured_admin(client)
    assert len(_access_target_ids(admin, "search-answer-profiles")) == 3
    assert len(_access_target_ids(admin, "knowledge-bases")) == 3


def test_access_targets_search_and_page_on_server(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """権限管理の候補は `q` / `limit` / `offset` / `ids` でサーバー側で絞る（#608）。"""
    admin = login_configured_admin(client)
    page = client.get(
        "/api/security/access-targets/knowledge-bases?limit=2&offset=0", headers=admin
    ).json()["data"]
    assert [item["id"] for item in page["items"]] == ["kb-1", "kb-2"]
    assert page["total"] == 3
    assert page["has_next"] is True
    rest = client.get(
        "/api/security/access-targets/knowledge-bases?limit=2&offset=2", headers=admin
    ).json()["data"]
    assert [item["id"] for item in rest["items"]] == ["kb-3"]
    assert rest["has_next"] is False
    assert _access_target_ids(admin, "search-answer-profiles", "?q=bv-2") == ["bv-2"]
    assert _access_target_ids(admin, "search-answer-profiles", "?ids=bv-3&ids=bv-1") == [
        "bv-1",
        "bv-3",
    ]
    # 範囲外の ID を指定しても、利用者の範囲の外の対象は返さない。
    auth.user_with_permissions(
        "limited", ["menu.security_permissions"], search_answer_profile_ids=["bv-1"]
    )
    limited = login(client, "limited")
    assert _access_target_ids(limited, "search-answer-profiles", "?ids=bv-1&ids=bv-2") == ["bv-1"]
    too_many = "&".join(f"ids=bv-{index}" for index in range(101))
    response = client.get(
        f"/api/security/access-targets/search-answer-profiles?{too_many}", headers=admin
    )
    assert response.status_code == 422
    assert (
        client.get(
            "/api/security/access-targets/search-answer-profiles?limit=101", headers=admin
        ).status_code
        == 422
    )


def _put_access(
    headers: dict[str, str],
    role_id: str,
    *,
    version: int = 1,
    permissions: list[str] | None = None,
    search_answer_profile_ids: list[str] | None = None,
    knowledge_base_ids: list[str] | None = None,
) -> Any:
    return client.put(
        f"/api/security/roles/{role_id}/access",
        json={
            "version": version,
            "permissions": permissions or [],
            "search_answer_profile_ids": search_answer_profile_ids or [],
            "knowledge_base_ids": knowledge_base_ids or [],
        },
        headers=headers,
    )


def test_system_admin_updates_role_access(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    role = auth.create_role()
    headers = login_configured_admin(client)
    response = _put_access(
        headers,
        role.role_id,
        permissions=["menu.search", "menu.chat"],
        search_answer_profile_ids=["bv-1", "bv-2"],
        knowledge_base_ids=["kb-1"],
    )
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == '"2"'
    data = response.json()["data"]
    assert data["permissions"] == ["menu.chat", "menu.search"]
    assert data["search_answer_profile_ids"] == ["bv-1", "bv-2"]
    assert data["knowledge_base_ids"] == ["kb-1"]
    assert data["version"] == 2
    stored = auth.store.get_role(role.role_id)
    assert stored is not None and stored.search_answer_profile_ids == {"bv-1", "bv-2"}

    # 版が古い保存は 409。
    stale = _put_access(headers, role.role_id, version=1, permissions=["menu.search"])
    assert stale.status_code == 409

    # ロール一覧・詳細も RAG の権限と対象範囲を返す。
    listed = client.get("/api/security/roles", headers=headers)
    by_id = {item["role_id"]: item for item in listed.json()["data"]}
    assert by_id[role.role_id]["knowledge_base_ids"] == ["kb-1"]


def test_role_access_update_validates_input(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    role = auth.create_role()
    archived = auth.create_role()
    auth.store.archive_role(archived.role_id, expected_version=1)
    headers = login_configured_admin(client)

    unknown_code = _put_access(headers, role.role_id, permissions=["menu.unknown"])
    assert unknown_code.status_code == 400
    assert "menu.unknown" in unknown_code.json()["error_messages"][0]
    assert _put_access(headers, role.role_id, search_answer_profile_ids=["bv-9"]).status_code == 400
    assert _put_access(headers, role.role_id, knowledge_base_ids=["kb-9"]).status_code == 400
    assert _put_access(headers, SYSTEM_ADMIN_ROLE_ID).status_code == 409
    assert _put_access(headers, archived.role_id, version=2).status_code == 409
    assert _put_access(headers, "missing-role").status_code == 404


def test_retired_permission_code_left_in_store_is_ignored(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """DB に残った廃止済みの権限コード（`menu.dashboard`。#261、`menu.settings_appearance`。
    #19）は、実効権限・権限管理の表示・ロールの割り当て・保存のどれでも無視し、エラーにしない
    （migration の適用前でも壊れない）。"""
    stale = auth.create_role(
        ["menu.search", "menu.dashboard", "menu.settings_appearance"],
        search_answer_profile_ids=["bv-1"],
    )
    auth.create_user("member", [stale])
    member, _token, _csrf = auth.service.login("member", USER_PASSWORD)
    assert member.permissions == {"menu.search"}

    # 範囲の限られた管理者も、古いコードが残ったロールを割り当てられる（昇格とみなさない）。
    auth.user_with_permissions(
        "user-admin", ["menu.security_users", "menu.search"], search_answer_profile_ids=["bv-1"]
    )
    delegate = login(client, "user-admin")
    assigned = client.post(
        "/api/security/users",
        json={
            "login_user_id": "member-2",
            "display_name": "member-2",
            "role_ids": [stale.role_id],
            "temporary_password": "TempPassword!2026",
        },
        headers=delegate,
    )
    assert assigned.status_code == 200, assigned.text

    headers = login_configured_admin(client)
    listed = client.get("/api/security/roles", headers=headers)
    by_id = {item["role_id"]: item for item in listed.json()["data"]}
    assert by_id[stale.role_id]["permissions"] == ["menu.search"]
    catalog = client.get("/api/security/permissions", headers=headers).json()["data"]
    assert {"menu.dashboard", "menu.settings_appearance"}.isdisjoint(
        {item["code"] for item in catalog}
    )

    # 権限管理画面は表示したコードをそのまま保存する。保存後は古いコードも消える。
    saved = _put_access(
        headers, stale.role_id, permissions=["menu.search"], search_answer_profile_ids=["bv-1"]
    )
    assert saved.status_code == 200, saved.text
    stored = auth.store.get_role(stale.role_id)
    assert stored is not None and stored.permissions == {"menu.search"}


def test_manage_permission_clears_target_lists(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """rag.*.manage は全対象を使えるため、対象リストを空に正規化する。"""
    role = auth.create_role()
    response = _put_access(
        login_configured_admin(client),
        role.role_id,
        permissions=["rag.search_answer_profiles.manage", "rag.knowledge_bases.manage"],
        search_answer_profile_ids=["bv-1"],
        knowledge_base_ids=["kb-1"],
    )
    assert response.status_code == 200
    assert response.json()["data"]["search_answer_profile_ids"] == []
    assert response.json()["data"]["knowledge_base_ids"] == []


def test_role_access_update_prevents_permission_escalation(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    role = auth.create_role()
    auth.user_with_permissions(
        "delegate",
        ["menu.security_permissions", "menu.search"],
        search_answer_profile_ids=["bv-1"],
        knowledge_base_ids=["kb-1"],
    )
    headers = login(client, "delegate")

    # 自分が持たない権限（メニュー・capability）は足せない。
    for permissions in (["menu.upload"], ["rag.search_answer_profiles.manage"]):
        response = _put_access(headers, role.role_id, permissions=permissions)
        assert response.status_code == 403
        assert "権限" in response.json()["error_messages"][0]
        # 権限の付与の制限は経路の権限拒否ではない（その場で理由を表示する。#224）。
        assert response.json()["error_code"] not in ROUTE_FORBIDDEN_CODES
    # 自分の範囲外の検索・回答プロファイル / KB は足せない（存在はする ID）。
    assert _put_access(headers, role.role_id, search_answer_profile_ids=["bv-2"]).status_code == 403
    assert _put_access(headers, role.role_id, knowledge_base_ids=["kb-2"]).status_code == 403
    # 自分の権限・範囲の内側なら保存できる。
    allowed = _put_access(
        headers,
        role.role_id,
        permissions=["menu.search"],
        search_answer_profile_ids=["bv-1"],
        knowledge_base_ids=["kb-1"],
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["data"]["search_answer_profile_ids"] == ["bv-1"]


def test_role_access_update_requires_permission_management(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    role = auth.create_role()
    auth.user_with_permissions("role-admin", ["menu.security_roles"])
    headers = login(client, "role-admin")
    assert _put_access(headers, role.role_id).status_code == 403


def test_restricted_actor_cannot_assign_role_beyond_scope(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """ユーザー管理でも、自分の範囲外の対象を持つロールは割り当てられない。"""
    wide = auth.create_role(["menu.search"], search_answer_profile_ids=["bv-1", "bv-2"])
    narrow = auth.create_role(["menu.search"], search_answer_profile_ids=["bv-1"])
    auth.user_with_permissions(
        "user-admin", ["menu.security_users", "menu.search"], search_answer_profile_ids=["bv-1"]
    )
    headers = login(client, "user-admin")

    def create(role_id: str, login_user_id: str) -> Any:
        return client.post(
            "/api/security/users",
            json={
                "login_user_id": login_user_id,
                "display_name": login_user_id,
                "role_ids": [role_id],
                "temporary_password": "TempPassword!2026",
            },
            headers=headers,
        )

    assert create(wide.role_id, "member-wide").status_code == 403
    assert create(narrow.role_id, "member-narrow").status_code == 200


# ---------------------------------------------------------------------------
# service: 実効権限と対象範囲
# ---------------------------------------------------------------------------


def test_principal_scope_is_union_of_active_roles(auth: ProductionAuth) -> None:
    first = auth.create_role(
        ["menu.search"], search_answer_profile_ids=["bv-1"], knowledge_base_ids=["kb-1"]
    )
    second = auth.create_role(["menu.chat"], search_answer_profile_ids=["bv-2"])
    archived = auth.create_role(["menu.upload"], search_answer_profile_ids=["bv-3"])
    auth.create_user("member", [first, second, archived])
    # アーカイブしたロールの権限・対象範囲は実効に含めない。
    auth.store.archive_role(archived.role_id, expected_version=1)

    principal, _token, _csrf = auth.service.login("member", USER_PASSWORD)
    assert isinstance(principal, Principal)
    assert principal.permissions == {"menu.search", "menu.chat"}
    assert principal.allowed_search_answer_profile_ids == frozenset({"bv-1", "bv-2"})
    assert principal.allowed_knowledge_base_ids == frozenset({"kb-1"})


def test_principal_scope_is_unrestricted_for_manage_and_system_admin(auth: ProductionAuth) -> None:
    manager = auth.create_role(["rag.knowledge_bases.manage"], search_answer_profile_ids=["bv-1"])
    auth.create_user("kb-manager", [manager])
    auth.create_user("root", system_admin=True)

    kb_manager, _token, _csrf = auth.service.login("kb-manager", USER_PASSWORD)
    assert kb_manager.allowed_knowledge_base_ids is None
    assert kb_manager.allowed_search_answer_profile_ids == frozenset({"bv-1"})
    root, _token, _csrf = auth.service.login("root", USER_PASSWORD)
    assert root.allowed_search_answer_profile_ids is None
    assert root.allowed_knowledge_base_ids is None


def test_restore_role_rejects_scope_beyond_actor(auth: ProductionAuth) -> None:
    role = auth.create_role(["menu.search"], search_answer_profile_ids=["bv-2"])
    auth.store.archive_role(role.role_id, expected_version=1)
    auth.create_user(
        "delegate",
        [
            auth.create_role(
                ["menu.security_roles", "menu.search"], search_answer_profile_ids=["bv-1"]
            )
        ],
    )
    actor, _token, _csrf = auth.service.login("delegate", USER_PASSWORD)
    with pytest.raises(SecurityApiError) as denied:
        auth.service.restore_role(role.role_id, expected_version=2, actor=actor)
    assert denied.value.status_code == 403


# ---------------------------------------------------------------------------
# Oracle store: ロールの RAG 権限と対象範囲の読み書き
# ---------------------------------------------------------------------------


class _RecordingCursor:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection
        self._rows: list[tuple[object, ...]] = []
        self.rowcount = 1

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: dict[str, object] | None = None) -> None:
        sql = " ".join(statement.split())
        self._connection.statements.append((sql, dict(parameters or {})))
        self._rows = []
        for prefix, rows in self._connection.rows.items():
            if sql.startswith(prefix):
                self._rows = list(rows)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _RecordingConnection:
    def __init__(self, rows: dict[str, list[tuple[object, ...]]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, dict[str, object]]] = []
        self.commits = 0

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1


def _oracle_store(connection: _RecordingConnection) -> Any:
    from contextlib import contextmanager

    from app.security.store import OracleSecurityStore

    @contextmanager
    def factory() -> Any:
        yield connection

    store = OracleSecurityStore.__new__(OracleSecurityStore)
    store._connection_factory = factory
    return store


def test_oracle_store_reads_role_permissions_and_scope() -> None:
    connection = _RecordingConnection(
        {
            "SELECT ROLE_ID, ROLE_CODE": [("role-1", "READER", "閲覧者", "-", 0, 0, 3)],
            "SELECT ROLE_ID, PERMISSION_CODE FROM RAG_ROLE_PERMISSIONS": [
                ("role-1", "menu.search")
            ],
            "SELECT ROLE_ID, SEARCH_ANSWER_PROFILE_ID FROM RAG_ROLE_SEARCH_ANSWER_PROFILES": [
                ("role-1", "bv-1"),
                ("role-1", "bv-2"),
            ],
            "SELECT ROLE_ID, KNOWLEDGE_BASE_ID FROM RAG_ROLE_KNOWLEDGE_BASES": [("role-1", "kb-1")],
        }
    )
    role = _oracle_store(connection).get_role("role-1")
    assert role is not None
    assert role.permissions == {"menu.search"}
    assert role.search_answer_profile_ids == {"bv-1", "bv-2"}
    assert role.knowledge_base_ids == {"kb-1"}
    assert role.description == ""


def test_oracle_store_lists_roles_without_a_query_per_role() -> None:
    """ロールの一覧は、ロールの数によらず PLATFORM_ROLES と RAG の 3 表を 1 回ずつ読む（#793）。"""
    connection = _RecordingConnection(
        {
            "SELECT ROLE_ID, ROLE_CODE": [
                (f"role-{index}", f"R{index:02d}", f"R{index}", None, 0, 0, 1)
                for index in range(30)
            ],
            "SELECT ROLE_ID, PERMISSION_CODE FROM RAG_ROLE_PERMISSIONS": [
                ("role-2", "menu.search"),
                ("role-2", "menu.chat"),
            ],
            "SELECT ROLE_ID, SEARCH_ANSWER_PROFILE_ID FROM RAG_ROLE_SEARCH_ANSWER_PROFILES": [
                ("role-5", "bv-1")
            ],
            "SELECT ROLE_ID, KNOWLEDGE_BASE_ID FROM RAG_ROLE_KNOWLEDGE_BASES": [("role-9", "kb-1")],
        }
    )

    roles = {role.role_id: role for role in _oracle_store(connection).list_roles()}

    assert len(roles) == 30
    assert len(connection.statements) == 4
    assert roles["role-2"].permissions == {"menu.search", "menu.chat"}
    assert roles["role-5"].search_answer_profile_ids == {"bv-1"}
    assert roles["role-9"].knowledge_base_ids == {"kb-1"}
    assert roles["role-0"].permissions == set()


def test_oracle_store_replaces_role_access_in_same_transaction() -> None:
    from app.security.domain import RoleRecord

    connection = _RecordingConnection(
        {"SELECT ROLE_ID, ROLE_CODE": [("role-1", "READER", "閲覧者", "-", 0, 0, 2)]}
    )
    store = _oracle_store(connection)
    store.update_role(
        RoleRecord(
            role_id="role-1",
            role_code="READER",
            display_name="閲覧者",
            description="",
            is_built_in=False,
            archived=False,
            version=1,
            permissions={"menu.search"},
            search_answer_profile_ids={"bv-1"},
            knowledge_base_ids={"kb-1", "kb-2"},
        ),
        expected_version=1,
    )
    writes = [
        (sql, params)
        for sql, params in connection.statements
        if sql.startswith(("UPDATE", "DELETE", "INSERT"))
    ]
    assert [sql.split(" (")[0] for sql, _ in writes] == [
        "UPDATE PLATFORM_ROLES SET DISPLAY_NAME = :display_name, DESCRIPTION = :description,"
        " VERSION_NO = VERSION_NO + 1, UPDATED_AT = SYSTIMESTAMP WHERE ROLE_ID = :role_id AND"
        " VERSION_NO = :expected_version",
        "DELETE FROM RAG_ROLE_PERMISSIONS WHERE ROLE_ID = :role_id",
        "DELETE FROM RAG_ROLE_SEARCH_ANSWER_PROFILES WHERE ROLE_ID = :role_id",
        "DELETE FROM RAG_ROLE_KNOWLEDGE_BASES WHERE ROLE_ID = :role_id",
        "INSERT INTO RAG_ROLE_PERMISSIONS",
        "INSERT INTO RAG_ROLE_SEARCH_ANSWER_PROFILES",
        "INSERT INTO RAG_ROLE_KNOWLEDGE_BASES",
        "INSERT INTO RAG_ROLE_KNOWLEDGE_BASES",
    ]
    assert writes[-1][1] == {"role_id": "role-1", "knowledge_base_id": "kb-2"}
    # ロール本体と RAG のデータは 1 回の commit でまとめて確定する。
    assert connection.commits == 1


def test_role_basic_info_update_keeps_rag_access(
    auth: ProductionAuth, scoped_oracle: ScopedFakeOracle
) -> None:
    """ロール管理（共通画面）の名称変更は、RAG の権限と対象範囲を変えない。"""
    role = auth.create_role(
        ["menu.search"], search_answer_profile_ids=["bv-1"], knowledge_base_ids=["kb-1"]
    )
    auth.user_with_permissions("role-admin", ["menu.security_roles"])
    headers = login(client, "role-admin")
    response = client.patch(
        f"/api/security/roles/{role.role_id}",
        json={"version": 1, "display_name": "新しい名前", "description": "説明"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["display_name"] == "新しい名前"
    assert data["permissions"] == ["menu.search"]
    assert data["search_answer_profile_ids"] == ["bv-1"]
    assert data["knowledge_base_ids"] == ["kb-1"]


def test_local_mode_login_and_logout_do_not_create_sessions() -> None:
    login_response = client.post(
        "/api/auth/login", json={"login_user_id": "anyone", "password": "anything"}
    )
    assert login_response.status_code == 200
    assert login_response.json()["data"]["debug_mode"] is True
    assert "set-cookie" not in login_response.headers
    assert client.post("/api/auth/logout").json()["data"] == {"logged_out": False}
