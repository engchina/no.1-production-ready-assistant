"""RAG の認証/RBAC のユースケース（#214）。

ログイン・セッション・CSRF・構成管理者・ユーザー / ロールの共通操作・製品をまたぐ権限昇格の防止は
platform の `pr_system_settings.auth.service.AuthService`。ここには RAG の実効権限と対象範囲
（検索・回答プロファイル・ナレッジベース）の組み立てと、権限管理（ロールの権限・対象範囲の更新）だけを置く。
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Sequence
from pathlib import Path

from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.domain import SessionRecord as PlatformSessionRecord
from pr_system_settings.auth.domain import UserRecord as PlatformUserRecord
from pr_system_settings.auth.errors import SecurityApiError as SecurityApiError
from pr_system_settings.auth.service import AuthService

from app import config as config_module
from app.config import Settings

from .domain import SYSTEM_ADMIN_ROLE_CODE, Principal, RoleRecord, as_principal, as_role
from .permissions import (
    ALL_PERMISSION_CODES,
    MENU_SECURITY_PERMISSIONS,
    MENU_SECURITY_ROLES,
    expand_permissions,
    grants_all_knowledge_bases,
    grants_all_search_answer_profiles,
    normalize_permission_codes,
    unknown_permission_codes,
)
from .store import (
    SECURITY_SCHEMA_OBJECT_NAMES,
    OracleSecurityStore,
    SecurityConflict,
    SecurityNotFound,
    SecurityStore,
)


class SecurityService(AuthService):
    """RAG の認証/RBAC。共通部分は platform の AuthService。"""

    product_key = "rag"
    principal_class = Principal
    role_class = RoleRecord
    # ロール管理・権限管理のどちらかを持つ操作者は、アーカイブ済みを含む全ロールを参照できる。
    role_catalog_permissions = frozenset({MENU_SECURITY_ROLES, MENU_SECURITY_PERMISSIONS})
    migration_hint = (
        "システム設定 > データベース の「システムテーブル」で作成・更新するか、"
        "`uv run python -m app.rag.system_schema_cli initialize` を実行してから再試行してください。"
    )
    schema_object_names = SECURITY_SCHEMA_OBJECT_NAMES

    store: SecurityStore

    def __init__(self, store: SecurityStore, settings: Settings) -> None:
        super().__init__(store, settings)
        self.settings: Settings = settings

    # ---- 構成管理者（共通 .env の PLATFORM_ADMIN_*。#211） ----

    def _platform_env_file(self) -> Path | None:
        """テストが差し替えられるよう、呼び出し時に `app.config.PLATFORM_ENV_FILE` を引く。"""
        return config_module.PLATFORM_ENV_FILE

    # ---- 実効権限と対象範囲 ----

    def all_permissions(self) -> set[str]:
        return set(ALL_PERMISSION_CODES)

    def _role_permissions(self, roles: Sequence[PlatformRoleRecord]) -> set[str]:
        return expand_permissions(
            {code for role in roles for code in getattr(role, "permissions", set())}
        )

    def _build_principal(
        self,
        user: PlatformUserRecord,
        session: PlatformSessionRecord,
        active_roles: list[PlatformRoleRecord],
    ) -> Principal:
        kwargs = self._principal_kwargs(user, session, active_roles)
        permissions: set[str] = kwargs["permissions"]
        is_system_admin = SYSTEM_ADMIN_ROLE_CODE in kwargs["role_codes"]
        rag_roles = [as_role(role) for role in active_roles if isinstance(role, RoleRecord)]
        allowed_search_answer_profile_ids = (
            None
            if is_system_admin or grants_all_search_answer_profiles(permissions)
            else frozenset(item for role in rag_roles for item in role.search_answer_profile_ids)
        )
        allowed_knowledge_base_ids = (
            None
            if is_system_admin or grants_all_knowledge_bases(permissions)
            else frozenset(item for role in rag_roles for item in role.knowledge_base_ids)
        )
        return Principal(
            **kwargs,
            allowed_search_answer_profile_ids=allowed_search_answer_profile_ids,
            allowed_knowledge_base_ids=allowed_knowledge_base_ids,
        )

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
        return as_principal(principal), token, csrf_token

    def authenticate_session(self, token: str) -> Principal:
        return as_principal(super().authenticate_session(token))

    def get_role(self, role_id: str) -> RoleRecord | None:
        role = super().get_role(role_id)
        return None if role is None else as_role(role)

    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]:
        return [as_role(role) for role in super().get_roles(role_ids)]

    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        return [as_role(role) for role in super().list_roles(include_archived=include_archived)]

    # ---- 権限昇格の防止（ロール割り当て・復元） ----

    def _role_within_actor(self, actor: PlatformPrincipal, role: PlatformRoleRecord) -> bool:
        """ロールの権限と対象範囲が、操作者の権限と対象範囲に収まるか。"""
        if not isinstance(role, RoleRecord) or not isinstance(actor, Principal):
            return False
        if not expand_permissions(role.permissions).issubset(actor.permissions):
            return False
        return _targets_within(
            role.search_answer_profile_ids, actor.allowed_search_answer_profile_ids
        ) and _targets_within(role.knowledge_base_ids, actor.allowed_knowledge_base_ids)

    def _assert_actor_can_restore_role(
        self, actor: PlatformPrincipal, role: PlatformRoleRecord
    ) -> None:
        # アーカイブ中の実効権限は空。復元は全権限・全対象の再付与として検証する。
        if not actor.is_system_admin and not self._role_within_actor(actor, role):
            raise SecurityApiError(
                403, "自分が持たない権限または対象範囲を含むロールは復元できません。"
            )

    # ---- 権限管理（ロールの権限と対象範囲） ----

    def update_role_access(
        self,
        role_id: str,
        *,
        expected_version: int,
        permissions: Iterable[str],
        search_answer_profile_ids: Iterable[str],
        knowledge_base_ids: Iterable[str],
        known_search_answer_profile_ids: Collection[str],
        known_knowledge_base_ids: Collection[str],
        actor: PlatformPrincipal,
        request_id: str = "",
        client_ip: str = "",
    ) -> RoleRecord:
        """権限管理画面の保存。ロールの RAG 権限と対象範囲だけを置き換える。

        - 組み込み / アーカイブ済みのロールは 409、未知の権限コード・対象 ID は 400。
        - SYSTEM_ADMIN 以外は、自分が持たない権限・自分の範囲外の対象を足すと 403。
        - `rag.*.manage` を含むロールは全対象を利用できるため、対象リストを空にする。
        """
        current = self.get_role(role_id)
        if current is None:
            raise SecurityApiError(404, "ロールが見つかりません。")
        if current.is_built_in:
            raise SecurityApiError(409, "組み込み SYSTEM_ADMIN ロールは変更できません。")
        if current.archived:
            raise SecurityApiError(409, "アーカイブ済みロールは変更できません。")
        requested_permissions = [code for code in permissions]
        unknown = unknown_permission_codes(requested_permissions)
        if unknown:
            raise SecurityApiError(400, f"未登録の権限コードです: {', '.join(sorted(unknown))}")
        normalized_permissions = normalize_permission_codes(requested_permissions)
        next_search_answer_profile_ids = _clean_ids(search_answer_profile_ids)
        next_knowledge_base_ids = _clean_ids(knowledge_base_ids)
        unknown_views = next_search_answer_profile_ids - set(known_search_answer_profile_ids)
        if unknown_views:
            raise SecurityApiError(
                400, f"検索・回答プロファイルが見つかりません: {', '.join(sorted(unknown_views))}"
            )
        unknown_bases = next_knowledge_base_ids - set(known_knowledge_base_ids)
        if unknown_bases:
            raise SecurityApiError(
                400, f"ナレッジベースが見つかりません: {', '.join(sorted(unknown_bases))}"
            )
        if grants_all_search_answer_profiles(normalized_permissions):
            next_search_answer_profile_ids = set()
        if grants_all_knowledge_bases(normalized_permissions):
            next_knowledge_base_ids = set()
        principal = as_principal(actor)
        if not principal.is_system_admin:
            added_permissions = expand_permissions(normalized_permissions) - expand_permissions(
                current.permissions
            )
            if not added_permissions.issubset(principal.permissions):
                raise SecurityApiError(
                    403, "自分が持たない権限をロールに追加することはできません。"
                )
            if not _targets_within(
                next_search_answer_profile_ids - current.search_answer_profile_ids,
                principal.allowed_search_answer_profile_ids,
            ):
                raise SecurityApiError(
                    403,
                    "自分が利用できない検索・回答プロファイルをロールに追加することはできません。",
                )
            if not _targets_within(
                next_knowledge_base_ids - current.knowledge_base_ids,
                principal.allowed_knowledge_base_ids,
            ):
                raise SecurityApiError(
                    403, "自分が利用できないナレッジベースをロールに追加することはできません。"
                )
        updated = RoleRecord(
            role_id=current.role_id,
            role_code=current.role_code,
            display_name=current.display_name,
            description=current.description,
            is_built_in=current.is_built_in,
            archived=current.archived,
            version=current.version,
            permissions=normalized_permissions,
            search_answer_profile_ids=next_search_answer_profile_ids,
            knowledge_base_ids=next_knowledge_base_ids,
        )
        try:
            return as_role(self.store.update_role(updated, expected_version=expected_version))
        except (SecurityConflict, SecurityNotFound) as exc:
            raise self._store_error(exc) from exc


def _clean_ids(values: Iterable[str]) -> set[str]:
    return {value.strip() for value in values if value and value.strip()}


def _targets_within(targets: Collection[str], allowed: frozenset[str] | None) -> bool:
    """対象が操作者の範囲に収まるか（None は制限なし）。"""
    return allowed is None or set(targets).issubset(allowed)


_SERVICE: SecurityService | None = None
_SERVICE_LOCK = threading.Lock()


def get_security_service() -> SecurityService:
    """process で 1 つの service（Oracle の PLATFORM_* / RAG_ROLE_* を使う）。"""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = SecurityService(OracleSecurityStore(), config_module.get_settings())
    return _SERVICE


def set_security_service(service: SecurityService | None) -> None:
    """テスト用: service を差し替える（None で既定に戻す）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service
