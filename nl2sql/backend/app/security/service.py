"""NL2SQL の認証/RBAC のユースケース。

ログイン・セッション・構成管理者・ユーザー / ロールの共通操作・製品をまたぐ権限昇格の防止は
platform の `pr_system_settings.auth.service.AuthService` が持つ（#212）。ここには NL2SQL の権限
（`permissions.py`）・業務プロファイル利用権限・Data Grant と、構成管理者を置く共通 `.env` の場所
だけを置く（構成管理者の `PLATFORM_ADMIN_*` の読み書きは platform の AuthService。#211）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from copy import deepcopy
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

from .domain import (
    DataEntitlementRecord,
    Principal,
    RoleRecord,
    as_principal,
    as_role,
    scope_expression_canonical_json,
    scope_expression_scope_code,
    scope_filters_canonical_json,
    scope_filters_scope_code,
)
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

DataEntitlementDraft = tuple[str, str, str] | DataEntitlementRecord
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
        entitlements: dict[tuple[str, str, str], DataEntitlementRecord] = {}
        allowed_profile_ids: set[str] = set()
        for role in active_roles:
            role = as_role(role)
            allowed_profile_ids.update(role.allowed_profile_ids)
            for entitlement in role.entitlements:
                key = (
                    entitlement.entitlement_id or entitlement.resource_code,
                    entitlement.scope_code,
                    entitlement.capability,
                )
                entitlements[key] = entitlement
        return Principal(
            **self._principal_kwargs(user, session, active_roles),
            data_entitlements=list(entitlements.values()),
            allowed_profile_ids=allowed_profile_ids,
        )

    def _role_within_actor(self, actor: Principal, role: PlatformRoleRecord) -> bool:  # type: ignore[override]
        """ロールの権限・業務プロファイル利用権限・Data Grant が、操作者の範囲に収まるか（#1090）。

        ロールの割り当て・ユーザーの管理の判定に使う。権限だけを比べると、自分が使えない業務プロファイルや
        データを含むロールを自分や他人に割り当てられてしまう（RAG / Agent は対象範囲も比べている）。
        """
        if not expand_permissions(set(getattr(role, "permissions", set()))).issubset(
            actor.permissions
        ):
            return False
        if not isinstance(role, RoleRecord) or not isinstance(actor, Principal):
            return False
        if (
            role.allowed_profile_ids
            and not grants_all_profile_access(actor.permissions)
            and not set(role.allowed_profile_ids).issubset(actor.allowed_profile_ids)
        ):
            return False
        actor_grants = {
            self._data_entitlement_policy_signature(entitlement)
            for entitlement in actor.data_entitlements
        }
        return all(
            self._data_entitlement_policy_signature(entitlement) in actor_grants
            for entitlement in role.entitlements
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

    def _assert_role_deletable(self, role: PlatformRoleRecord) -> None:
        if isinstance(role, RoleRecord) and role.entitlements:
            raise SecurityApiError(
                409,
                "このロールにはデータ権限が残っています。"
                "Deep Data Security で空の Data Grant を適用してから削除してください。",
                code="SECURITY_ROLE_DELETE_ENTITLEMENTS_PRESENT",
            )

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

    # ---- NL2SQL のロール編集（権限・業務プロファイル・Data Grant） ----

    def create_role(  # type: ignore[override]
        self,
        *,
        role_code: str,
        display_name: str,
        description: str,
        permissions: set[str] | None = None,
        entitlements: list[DataEntitlementDraft] | None = None,
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
            entitlements=entitlements or [],
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
            # Data Grant は DeepSec 画面のみが管理するため、ロール編集では現在値を保持する。
            entitlements=list(current.entitlements),
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

    def update_role_data_entitlements(
        self,
        role_id: str,
        *,
        expected_version: int,
        entitlements: list[DataEntitlementDraft],
        actor: Principal,
        request_id: str = "",
        client_ip: str = "",
    ) -> RoleRecord:
        current = self.get_role(role_id)
        if current is None:
            raise SecurityApiError(404, "ロールが見つかりません。")
        if current.is_built_in:
            raise SecurityApiError(409, "組み込み SYSTEM_ADMIN ロールは変更できません。")
        if current.archived:
            raise SecurityApiError(409, "アーカイブ済みロールは変更できません。")
        data_records = self._data_entitlement_records(
            role_id,
            entitlements,
            current_entitlements=current.entitlements,
        )
        role = RoleRecord(
            role_id=current.role_id,
            role_code=current.role_code,
            display_name=current.display_name,
            description=current.description,
            is_built_in=current.is_built_in,
            archived=current.archived,
            version=current.version,
            permissions=set(current.permissions),
            entitlements=data_records,
            allowed_profile_ids=set(current.allowed_profile_ids),
        )
        try:
            updated = self._nl2sql_role(
                self.store.update_role(role, expected_version=expected_version)
            )
        except (SecurityConflict, SecurityNotFound) as exc:
            raise self._store_error(exc) from exc
        return updated

    def commit_role_data_entitlement_sync(
        self,
        role_id: str,
        *,
        expected_version: int,
        entitlements: list[DataEntitlementRecord],
        actor: Principal,
    ) -> RoleRecord:
        """Oracle 同期成功後のロール全体 Data Grant snapshot を確定する。"""
        _ = actor
        current = self.get_role(role_id)
        if current is None:
            raise SecurityApiError(404, "ロールが見つかりません。")
        if current.is_built_in:
            raise SecurityApiError(409, "組み込み SYSTEM_ADMIN ロールは変更できません。")
        if current.archived:
            raise SecurityApiError(409, "アーカイブ済みロールは変更できません。")
        if current.version != expected_version:
            raise SecurityApiError(
                409,
                "ロールが別の操作で更新されています。表示を更新して再試行してください。",
            )
        role = RoleRecord(
            role_id=current.role_id,
            role_code=current.role_code,
            display_name=current.display_name,
            description=current.description,
            is_built_in=current.is_built_in,
            archived=current.archived,
            version=current.version,
            permissions=set(current.permissions),
            entitlements=list(entitlements),
            allowed_profile_ids=set(current.allowed_profile_ids),
        )
        try:
            return self._nl2sql_role(
                self.store.update_role(role, expected_version=expected_version)
            )
        except (SecurityConflict, SecurityNotFound) as exc:
            raise self._store_error(exc) from exc

    def _build_role(
        self,
        *,
        role_id: str,
        role_code: str,
        display_name: str,
        description: str,
        permissions: set[str],
        entitlements: list[DataEntitlementDraft],
        allowed_profile_ids: set[str],
        version: int,
    ) -> RoleRecord:
        unknown = unknown_permission_codes(permissions)
        if unknown:
            raise SecurityApiError(400, f"未登録の権限コードです: {', '.join(sorted(unknown))}")
        normalized = normalize_permission_codes(permissions)
        data_records = self._data_entitlement_records(role_id, entitlements)
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
            entitlements=data_records,
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

    @staticmethod
    def _data_entitlement_policy_signature(
        entitlement: DataEntitlementRecord,
    ) -> tuple[object, ...]:
        return (
            # 識別子は canonical token（引用名は大文字小文字を保持）なので大文字化せずに比べる。
            # 大文字化すると "Mixed" と "MIXED" 等の別 object を同じ適用状態として引き継いでしまう。
            entitlement.resource_code.strip(),
            entitlement.scope_code.strip(),
            entitlement.capability.strip().upper(),
            entitlement.target_owner.strip(),
            entitlement.target_object.strip(),
            entitlement.target_type.strip().upper(),
            tuple(column.strip() for column in entitlement.column_names),
            entitlement.scope_mode.strip().upper(),
            entitlement.scope_column.strip(),
            scope_filters_canonical_json(entitlement.scope_filters),
            scope_expression_canonical_json(entitlement.scope_expression),
        )

    @classmethod
    def _data_entitlement_records(
        cls,
        role_id: str,
        entitlements: Sequence[DataEntitlementDraft],
        *,
        current_entitlements: list[DataEntitlementRecord] | None = None,
    ) -> list[DataEntitlementRecord]:
        records: list[DataEntitlementRecord] = []
        seen: set[tuple[str, str, str, str, str, str, str, str, str]] = set()
        current_by_id = {
            entitlement.entitlement_id: entitlement
            for entitlement in current_entitlements or []
            if entitlement.entitlement_id
        }
        for entitlement in entitlements:
            if isinstance(entitlement, DataEntitlementRecord):
                record = DataEntitlementRecord(
                    entitlement_id=entitlement.entitlement_id or str(uuid4()),
                    role_id=role_id,
                    resource_code=entitlement.resource_code,
                    scope_code=(
                        "*"
                        if entitlement.scope_mode.strip().upper() == "ALL"
                        else (
                            scope_filters_scope_code(entitlement.scope_filters)
                            if entitlement.scope_mode.strip().upper() == "FILTERS"
                            else entitlement.scope_code
                        )
                    ),
                    capability=entitlement.capability,
                    target_owner=entitlement.target_owner,
                    target_object=entitlement.target_object,
                    target_type=entitlement.target_type,
                    column_names=list(entitlement.column_names),
                    scope_mode=entitlement.scope_mode,
                    scope_column=entitlement.scope_column,
                    scope_filters=list(entitlement.scope_filters),
                    scope_expression=deepcopy(entitlement.scope_expression),
                    scope_expression_version=entitlement.scope_expression_version,
                    data_grant_name=entitlement.data_grant_name,
                    sql_checksum=entitlement.sql_checksum,
                    apply_status=entitlement.apply_status,
                    apply_error_message=entitlement.apply_error_message,
                    applied_at=entitlement.applied_at,
                )
                if record.scope_mode == "EXPRESSION":
                    from .schemas import DataEntitlementInput

                    validated = DataEntitlementInput.model_validate(
                        {
                            "capability": record.capability,
                            "scope_mode": record.scope_mode,
                            "scope_expression": record.scope_expression,
                            "scope_filters": record.scope_filters,
                            "scope_column": record.scope_column,
                        }
                    )
                    if validated.scope_expression is None:
                        raise SecurityApiError(400, "条件ツリーを指定してください。")
                    record.scope_expression = validated.scope_expression.model_dump()
                    record.scope_code = scope_expression_scope_code(record.scope_expression)
                current = current_by_id.get(record.entitlement_id)
                if (
                    current is not None
                    and current.scope_mode == "EXPRESSION"
                    and record.scope_mode != "EXPRESSION"
                    and not (record.scope_mode == "ALL" and record.scope_expression_version == 1)
                ):
                    raise SecurityApiError(
                        409, "条件ツリーを旧形式で上書きできません。最新画面で編集してください。"
                    )
                if current is not None:
                    if cls._data_entitlement_policy_signature(
                        record
                    ) == cls._data_entitlement_policy_signature(current):
                        record.apply_status = current.apply_status
                        record.apply_error_message = current.apply_error_message
                        record.data_grant_name = current.data_grant_name
                        record.sql_checksum = current.sql_checksum
                        record.applied_at = current.applied_at
                    else:
                        record.apply_status = "PENDING"
                        record.apply_error_message = ""
                        record.data_grant_name = current.data_grant_name
                        record.sql_checksum = ""
                        record.applied_at = None
                elif current_entitlements is not None:
                    record.apply_status = "PENDING"
                    record.apply_error_message = ""
                    record.sql_checksum = ""
                    record.applied_at = None
            else:
                resource, scope, capability = entitlement
                record = DataEntitlementRecord(
                    entitlement_id=str(uuid4()),
                    role_id=role_id,
                    resource_code=resource,
                    scope_code=scope,
                    capability=capability,
                )
            key = (
                record.resource_code,
                record.scope_code,
                record.capability,
                record.target_owner,
                record.target_object,
                ",".join(record.column_names),
                record.scope_mode,
                record.scope_column,
                scope_filters_canonical_json(record.scope_filters),
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        return records


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
