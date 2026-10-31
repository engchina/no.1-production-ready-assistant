"""NL2SQL の認証/RBAC のユースケース。

ログイン・セッション・構成管理者・ユーザー / ロールの共通操作・製品をまたぐ権限昇格の防止は
platform の `pr_system_settings.auth.service.AuthService` が持つ（#212）。ここには NL2SQL の権限
（`permissions.py`）・業務プロファイル利用権限・Data Grant と、構成管理者を置く共通 `.env` の場所
だけを置く（構成管理者の `PLATFORM_ADMIN_*` の読み書きは platform の AuthService。#211）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.domain import SessionRecord as PlatformSessionRecord
from pr_system_settings.auth.domain import UserRecord as PlatformUserRecord
from pr_system_settings.auth.errors import LoginFailed as LoginFailed
from pr_system_settings.auth.errors import SecurityApiError as SecurityApiError
from pr_system_settings.auth.service import AuthService

from app import settings as settings_module
from app.settings import Settings, get_settings

from .domain import Principal, RoleRecord, as_principal, as_role
from .passwords import hash_password, verify_password
from .permissions import (
    ALL_PERMISSION_CODES,
    expand_permissions,
    grants_all_profile_access,
    normalize_permission_codes,
    unknown_permission_codes,
)
from .store import (
    SECURITY_SCHEMA_OBJECT_NAMES,
    InMemorySecurityStore,
    OracleSecurityStore,
    SecurityConflict,
    SecurityNotFound,
    SecurityStore,
)

logger = logging.getLogger(__name__)


class SecurityService(AuthService):
    """NL2SQL の認証/RBAC。共通部分は platform の AuthService。"""

    product_key = "nl2sql"
    principal_class = Principal
    role_class = RoleRecord
    # ロール管理・権限管理のどちらかを持つ actor は、アーカイブ済みを含む全ロールを参照できる
    # （#206）。
    role_catalog_permissions = frozenset({"menu.security_roles", "menu.security_permissions"})
    migration_hint = (
        "`uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap` "
        "を実行してから再試行してください。"
    )
    schema_object_names = SECURITY_SCHEMA_OBJECT_NAMES

    store: SecurityStore

    def __init__(self, store: SecurityStore, settings: Settings) -> None:
        super().__init__(store, settings)
        self.settings: Settings = settings

    # ---- NL2SQL の実効権限 ----

    def all_permissions(self) -> set[str]:
        return set(ALL_PERMISSION_CODES)

    def _role_permissions(self, roles: Sequence[PlatformRoleRecord]) -> set[str]:
        return expand_permissions(
            {permission for role in roles for permission in getattr(role, "permissions", set())}
        )

    def _build_principal(
        self,
        user: PlatformUserRecord,
        session: PlatformSessionRecord,
        active_roles: list[PlatformRoleRecord],
    ) -> Principal:
        allowed_profile_ids: set[str] = set()
        for role in active_roles:
            allowed_profile_ids.update(as_role(role).allowed_profile_ids)
        return Principal(
            **self._principal_kwargs(user, session, active_roles),
            allowed_profile_ids=allowed_profile_ids,
        )

    def _role_within_actor(self, actor: Principal, role: PlatformRoleRecord) -> bool:  # type: ignore[override]
        """ロールの権限・業務プロファイル利用権限が、操作者の範囲に収まるか（#1090）。

        ロールの割り当て・ユーザーの管理の判定に使う。権限だけを比べると、自分が使えない業務
        プロファイルを含むロールを自分や他人に割り当てられてしまう（RAG / Agent は対象範囲も
        比べている）。
        """
        if not expand_permissions(set(getattr(role, "permissions", set()))).issubset(
            actor.permissions
        ):
            return False
        if not isinstance(role, RoleRecord) or not isinstance(actor, Principal):
            return False
        return not (
            role.allowed_profile_ids
            and not grants_all_profile_access(actor.permissions)
            and not set(role.allowed_profile_ids).issubset(actor.allowed_profile_ids)
        )

    def _assert_actor_can_restore_role(  # type: ignore[override]
        self, actor: Principal, role: PlatformRoleRecord
    ) -> None:
        role = as_role(role)
        # アーカイブ中の実効権限は空。復元は全権限の再付与として検証する。
        if not actor.is_system_admin and not expand_permissions(role.permissions).issubset(
            actor.permissions
        ):
            raise SecurityApiError(403, "自分が持たない権限を含むロールは復元できません。")
        if role.allowed_profile_ids:
            self._assert_actor_can_manage_profile_access(actor)

    def principal_for_worker(self, user_uuid: str) -> Principal:
        """受理後の非対話実行でも現在の user/role 権限を再計算する。"""
        from app.security.dependencies import LOCAL_DEBUG_USER_UUID, local_debug_principal

        if user_uuid == LOCAL_DEBUG_USER_UUID:
            if not get_settings().local_debug_enabled:
                raise SecurityApiError(403, "ローカル DEBUG の実行権限は解除されています。")
            return local_debug_principal()
        principal = super().principal_for_worker(user_uuid)
        return as_principal(principal)

    def login(
        self,
        login_user_id: str,
        password: str,
        *,
        request_id: str = "",
        client_ip: str = "",
    ) -> tuple[Principal, str, str]:
        principal, token, csrf_token = super().login(
            login_user_id, password, request_id=request_id, client_ip=client_ip
        )
        principal = as_principal(principal)
        return principal, token, csrf_token

    def authenticate_session(self, token: str) -> Principal:
        principal = super().authenticate_session(token)
        return as_principal(principal)

    def archive_role(
        self,
        role_id: str,
        *,
        expected_version: int,
        actor: PlatformPrincipal,
        request_id: str = "",
        client_ip: str = "",
    ) -> RoleRecord:
        return self._nl2sql_role(
            super().archive_role(
                role_id,
                expected_version=expected_version,
                actor=actor,
                request_id=request_id,
                client_ip=client_ip,
            )
        )

    def restore_role(
        self,
        role_id: str,
        *,
        expected_version: int,
        actor: PlatformPrincipal,
        request_id: str = "",
        client_ip: str = "",
    ) -> RoleRecord:
        return self._nl2sql_role(
            super().restore_role(
                role_id,
                expected_version=expected_version,
                actor=actor,
                request_id=request_id,
                client_ip=client_ip,
            )
        )

    def get_role(self, role_id: str) -> RoleRecord | None:
        role = super().get_role(role_id)
        return None if role is None else as_role(role)

    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]:
        return [as_role(role) for role in super().get_roles(role_ids)]

    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        roles = super().list_roles(include_archived=include_archived)
        return [role for role in roles if isinstance(role, RoleRecord)]

    @staticmethod
    def _nl2sql_role(role: PlatformRoleRecord) -> RoleRecord:
        return as_role(role)

    def _hash_password(self, password: str) -> str:
        return hash_password(password)

    def _verify_password(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        return verify_password(password, password_hash)

    # ---- 構成管理者（共通 .env の PLATFORM_ADMIN_*） ----

    def _platform_env_file(self) -> Path | None:
        """構成管理者の資格情報を置く共通 `.env`。テストが差し替えられるよう呼出時に参照する。"""
        return settings_module.PLATFORM_ENV_FILE

    # ---- NL2SQL のロール編集（権限・業務プロファイル利用権限） ----

    def create_role(  # type: ignore[override]
        self,
        *,
        role_code: str,
        display_name: str,
        description: str,
        permissions: set[str] | None = None,
        allowed_profile_ids: set[str] | None = None,
        actor: Principal,
        request_id: str = "",
        client_ip: str = "",
    ) -> RoleRecord:
        normalized_role_code = self._assert_role_code_not_reserved(role_code)
        role = self._build_role(
            role_id=str(uuid4()),
            role_code=normalized_role_code,
            display_name=display_name,
            description=description,
            permissions=permissions or set(),
            allowed_profile_ids=allowed_profile_ids or set(),
            version=1,
        )
        # 全プロファイル権限を含むロールは _build_role で allowed_profile_ids が空になる
        if role.allowed_profile_ids:
            self._assert_actor_can_manage_profile_access(actor)
        try:
            created = self._nl2sql_role(self.store.create_role(role))
        except SecurityConflict as exc:
            raise self._store_error(exc) from exc
        return created

    def update_role(  # type: ignore[override]
        self,
        role_id: str,
        *,
        expected_version: int,
        display_name: str | None = None,
        description: str | None = None,
        permissions: set[str] | None = None,
        allowed_profile_ids: set[str] | None = None,
        actor: Principal,
        request_id: str = "",
        client_ip: str = "",
    ) -> RoleRecord:
        """ロールを更新する。None の項目は現在値を保つ。

        ロール管理画面は基本情報（名称・説明）だけ、権限管理画面は権限と業務プロファイル利用権限だけを
        送る（#206）。どちらもこの経路を通るので、権限昇格の防止は常に同じ判定になる。
        """
        current = self.get_role(role_id)
        if current is None:
            raise SecurityApiError(404, "ロールが見つかりません。")
        if current.is_built_in:
            raise SecurityApiError(409, "組み込み SYSTEM_ADMIN ロールは変更できません。")
        if current.archived:
            raise SecurityApiError(409, "アーカイブ済みロールは変更できません。")
        requested_profile_ids = (
            allowed_profile_ids
            if allowed_profile_ids is not None
            else set(current.allowed_profile_ids)
        )
        role = self._build_role(
            role_id=role_id,
            role_code=current.role_code,
            display_name=current.display_name if display_name is None else display_name,
            description=current.description if description is None else description,
            permissions=set(current.permissions) if permissions is None else permissions,
            allowed_profile_ids=requested_profile_ids,
            version=current.version,
        )
        self._assert_actor_can_add_permissions(actor, current, role)
        # 全プロファイル権限になる場合は allowed_profile_ids がどのみち空になるため差分検査しない
        if (
            role.allowed_profile_ids != current.allowed_profile_ids
            and not grants_all_profile_access(role.permissions)
        ):
            self._assert_actor_can_manage_profile_access(actor)
        try:
            updated = self._nl2sql_role(
                self.store.update_role(role, expected_version=expected_version)
            )
        except (SecurityConflict, SecurityNotFound) as exc:
            raise self._store_error(exc) from exc
        return updated

    def _build_role(
        self,
        *,
        role_id: str,
        role_code: str,
        display_name: str,
        description: str,
        permissions: set[str],
        allowed_profile_ids: set[str],
        version: int,
    ) -> RoleRecord:
        unknown = unknown_permission_codes(permissions)
        if unknown:
            raise SecurityApiError(400, f"未登録の権限コードです: {', '.join(sorted(unknown))}")
        normalized = normalize_permission_codes(permissions)
        normalized_allowed_profile_ids = (
            set()
            if grants_all_profile_access(normalized)
            else {item.strip() for item in allowed_profile_ids if item.strip()}
        )
        return RoleRecord(
            role_id=role_id,
            role_code=role_code,
            display_name=display_name.strip(),
            description=description.strip(),
            is_built_in=False,
            archived=False,
            version=version,
            permissions=normalized,
            allowed_profile_ids=normalized_allowed_profile_ids,
        )

    @staticmethod
    def _assert_actor_can_add_permissions(
        actor: Principal,
        current: RoleRecord,
        updated: RoleRecord,
    ) -> None:
        """ロール編集経由の昇格を防ぐ。追加分の実効権限は actor 自身の権限に収まること。"""
        if actor.is_system_admin:
            return
        added = expand_permissions(updated.permissions) - expand_permissions(current.permissions)
        if not added.issubset(actor.permissions):
            raise SecurityApiError(
                403,
                "自分が持たない権限をロールに追加することはできません。",
            )

    @staticmethod
    def _assert_actor_can_manage_profile_access(
        actor: Principal,
    ) -> None:
        if not actor.is_system_admin:
            raise SecurityApiError(
                403,
                "業務プロファイル利用権限を変更できるのは SYSTEM_ADMIN のみです。",
            )


@lru_cache
def get_security_service() -> SecurityService:
    settings = get_settings()
    store: SecurityStore
    if settings.nl2sql_persistence_mode.strip().lower() == "memory":
        store = InMemorySecurityStore()
    else:
        store = OracleSecurityStore(settings)
    return SecurityService(store, settings)


def reset_security_service() -> None:
    get_security_service.cache_clear()
