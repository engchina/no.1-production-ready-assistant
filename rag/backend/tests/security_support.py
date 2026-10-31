"""production mode（共通認証）の API テスト補助（#214）。

InMemory の store で共通認証の service を差し替え、DB ユーザーと構成管理者でログインする。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_ID, UserRecord
from pytest import MonkeyPatch

from app.config import get_settings
from app.security.domain import RoleRecord
from app.security.service import SecurityService, set_security_service
from app.security.store import InMemorySecurityStore
from tests.support import AsgiTestClient

CONFIGURED_ADMIN_PASSWORD = "RagSystemPass2026"  # nosec B105 - テスト用
USER_PASSWORD = "UserPassword!2026"  # nosec B105 - テスト用
SESSION_COOKIE = "pr_session"
CSRF_COOKIE = "pr_csrf"


@dataclass
class ProductionAuth:
    """production mode のテスト用の service と store。"""

    service: SecurityService
    store: InMemorySecurityStore
    _counter: int = field(default=0)

    def create_role(
        self,
        permissions: Iterable[str] = (),
        *,
        search_answer_profile_ids: Iterable[str] = (),
        knowledge_base_ids: Iterable[str] = (),
        role_code: str | None = None,
    ) -> RoleRecord:
        self._counter += 1
        role = RoleRecord(
            role_id=str(uuid4()),
            role_code=role_code or f"ROLE_{self._counter}",
            display_name=f"ロール {self._counter}",
            description="",
            is_built_in=False,
            archived=False,
            version=1,
            permissions=set(permissions),
            search_answer_profile_ids=set(search_answer_profile_ids),
            knowledge_base_ids=set(knowledge_base_ids),
        )
        return self.store.create_role(role)

    def create_user(
        self,
        login_user_id: str,
        roles: Iterable[RoleRecord] = (),
        *,
        force_password_change: bool = False,
        system_admin: bool = False,
    ) -> UserRecord:
        role_ids = [role.role_id for role in roles]
        if system_admin:
            role_ids.append(SYSTEM_ADMIN_ROLE_ID)
        return self.store.create_user(
            UserRecord(
                user_uuid=str(uuid4()),
                login_user_id=login_user_id,
                display_name=login_user_id,
                password_hash=self.service._hash_password(USER_PASSWORD),
                status="ACTIVE",
                force_password_change=force_password_change,
                failed_login_count=0,
                locked_until=None,
                version=1,
                role_ids=role_ids,
            )
        )

    def user_with_permissions(
        self,
        login_user_id: str,
        permissions: Iterable[str],
        *,
        search_answer_profile_ids: Iterable[str] = (),
        knowledge_base_ids: Iterable[str] = (),
    ) -> UserRecord:
        role = self.create_role(
            permissions,
            search_answer_profile_ids=search_answer_profile_ids,
            knowledge_base_ids=knowledge_base_ids,
        )
        return self.create_user(login_user_id, [role])


def enable_production_auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    """PLATFORM_AUTH_MODE=authenticated と InMemory の共通認証 service にする。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_admin_login_user_id", "system_admin")
    monkeypatch.setattr(settings, "app_admin_login_user_password", CONFIGURED_ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "app_auth_cookie_secure", False)
    # テストを速くするため Argon2 のコストを下げる（本番の既定値は変えない）。
    monkeypatch.setattr(settings, "app_auth_argon2_time_cost", 1)
    monkeypatch.setattr(settings, "app_auth_argon2_memory_kib", 8192)
    monkeypatch.setattr(settings, "app_auth_argon2_parallelism", 1)
    store = InMemorySecurityStore()
    store._ensure_system_admin_role()
    service = SecurityService(store, settings)
    set_security_service(service)
    return ProductionAuth(service=service, store=store)


def session_headers(response: httpx.Response) -> dict[str, str]:
    """ログイン応答の Cookie から、以降の request の Cookie / CSRF header を作る。"""
    session = response.cookies.get(SESSION_COOKIE)
    csrf = response.cookies.get(CSRF_COOKIE)
    assert session and csrf, response.text
    return {"cookie": f"{SESSION_COOKIE}={session}; {CSRF_COOKIE}={csrf}", "X-CSRF-Token": csrf}


def login(
    client: AsgiTestClient, login_user_id: str, password: str = USER_PASSWORD
) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"login_user_id": login_user_id, "password": password}
    )
    assert response.status_code == 200, response.text
    return session_headers(response)


def login_configured_admin(client: AsgiTestClient) -> dict[str, str]:
    return login(client, "system_admin", CONFIGURED_ADMIN_PASSWORD)
