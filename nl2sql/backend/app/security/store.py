"""NL2SQL 固有の認証/RBAC の永続化。DDL は migration の責務。

ユーザー・ロール・セッション（PLATFORM_* テーブル）は platform の
`pr_system_settings.auth.store` が持つ（#212）。ここには次の読み書きだけを置く。

- ロールに付ける NL2SQL の権限（`NL2SQL_APP_ROLE_PERMISSIONS`）
- 業務プロファイル利用権限（`NL2SQL_APP_ROLE_PROFILES`）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.errors import SecurityConflict as SecurityConflict
from pr_system_settings.auth.errors import SecurityMigrationRequired as SecurityMigrationRequired
from pr_system_settings.auth.errors import SecurityNotFound as SecurityNotFound
from pr_system_settings.auth.errors import SecurityStoreError as SecurityStoreError
from pr_system_settings.auth.errors import missing_security_migration_object
from pr_system_settings.auth.store import (
    PLATFORM_AUTH_TABLES,
    AuthStore,
    InMemoryAuthStore,
    OracleAuthStore,
    values_by_role_id,
)

from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.settings import Settings

from .domain import RoleRecord, as_role

NL2SQL_SECURITY_TABLES = frozenset(
    {
        "NL2SQL_APP_ROLE_PERMISSIONS",
        "NL2SQL_APP_ROLE_PROFILES",
    }
)
SECURITY_SCHEMA_OBJECT_NAMES = frozenset(PLATFORM_AUTH_TABLES) | NL2SQL_SECURITY_TABLES


def _raise_missing_security_migration_if_needed(exc: Exception, object_name: str) -> None:
    missing_object = missing_security_migration_object(exc, SECURITY_SCHEMA_OBJECT_NAMES)
    if missing_object == object_name.upper() or (
        missing_object is None and "ORA-00942" in str(exc).upper()
    ):
        raise SecurityMigrationRequired(object_name.upper()) from exc


class SecurityStore(AuthStore, Protocol):
    def get_role(self, role_id: str) -> RoleRecord | None: ...
    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]: ...
    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]: ...
    def create_role(self, role: PlatformRoleRecord) -> RoleRecord: ...
    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord: ...
    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...
    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class InMemorySecurityStore(InMemoryAuthStore):
    """単体テスト用。production は OracleSecurityStore を使う。"""

    role_class: type[PlatformRoleRecord] = RoleRecord

    def get_role(self, role_id: str) -> RoleRecord | None:
        role = super().get_role(role_id)
        return None if role is None else as_role(role)

    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]:
        return [as_role(role) for role in super().get_roles(role_ids)]

    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        return [
            role
            for role in super().list_roles(include_archived=include_archived)
            if isinstance(role, RoleRecord)
        ]

    def create_role(self, role: PlatformRoleRecord) -> RoleRecord:
        created = super().create_role(role)
        return as_role(created)

    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord:
        updated = super().update_role(role, expected_version=expected_version)
        return as_role(updated)

    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        archived = super().archive_role(role_id, expected_version=expected_version)
        return as_role(archived)

    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        restored = super().restore_role(role_id, expected_version=expected_version)
        return as_role(restored)


class OracleSecurityStore(OracleAuthStore):
    """Oracle AI Database backed security store。NL2SQL 固有のテーブルを hook で読み書きする。"""

    role_class = RoleRecord
    schema_object_names = SECURITY_SCHEMA_OBJECT_NAMES

    def __init__(self, settings: Settings) -> None:
        self._adapter = OracleNl2SqlAdapter(settings)
        # 呼び出しのたびに _adapter を引く（テストで adapter を差し替えられるように）。
        # 要求ごとに通る認証の読み書きのため、接続は pool から借りる（#793）。
        super().__init__(lambda: self._adapter.pooled_connection())

    def get_role(self, role_id: str) -> RoleRecord | None:
        role = super().get_role(role_id)
        return None if role is None else as_role(role)

    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]:
        return [as_role(role) for role in super().get_roles(role_ids)]

    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        return [
            role
            for role in super().list_roles(include_archived=include_archived)
            if isinstance(role, RoleRecord)
        ]

    def create_role(self, role: PlatformRoleRecord) -> RoleRecord:
        created = super().create_role(role)
        return as_role(created)

    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord:
        updated = super().update_role(role, expected_version=expected_version)
        return as_role(updated)

    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        archived = super().archive_role(role_id, expected_version=expected_version)
        return as_role(archived)

    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        restored = super().restore_role(role_id, expected_version=expected_version)
        return as_role(restored)

    def _role_details(self, cursor: Any, role: PlatformRoleRecord) -> RoleRecord:
        return as_role(self._roles_details(cursor, [role])[0])

    def _roles_details(
        self, cursor: Any, roles: Sequence[PlatformRoleRecord]
    ) -> list[PlatformRoleRecord]:
        """権限・業務プロファイルを、ロールの数によらず表ごとに一括で読む（#793）。"""
        role_ids = [role.role_id for role in roles]
        permissions = values_by_role_id(
            cursor, "SELECT ROLE_ID, PERMISSION_CODE FROM NL2SQL_APP_ROLE_PERMISSIONS", role_ids
        )
        try:
            allowed_profile_ids = values_by_role_id(
                cursor, "SELECT ROLE_ID, PROFILE_ID FROM NL2SQL_APP_ROLE_PROFILES", role_ids
            )
        except Exception as exc:
            _raise_missing_security_migration_if_needed(exc, "NL2SQL_APP_ROLE_PROFILES")
            raise
        return [
            RoleRecord(
                role_id=role.role_id,
                role_code=role.role_code,
                display_name=role.display_name,
                description=role.description,
                is_built_in=role.is_built_in,
                archived=role.archived,
                version=role.version,
                permissions=permissions.get(role.role_id, set()),
                allowed_profile_ids=allowed_profile_ids.get(role.role_id, set()),
            )
            for role in roles
        ]

    def _before_delete_role(self, cursor: Any, role_id: str) -> None:
        cursor.execute(
            "DELETE FROM NL2SQL_APP_ROLE_PERMISSIONS WHERE ROLE_ID = :role_id",
            {"role_id": role_id},
        )
        try:
            cursor.execute(
                "DELETE FROM NL2SQL_APP_ROLE_PROFILES WHERE ROLE_ID = :role_id",
                {"role_id": role_id},
            )
        except Exception as exc:
            _raise_missing_security_migration_if_needed(exc, "NL2SQL_APP_ROLE_PROFILES")
            raise

    def _replace_role_details(self, cursor: Any, role: PlatformRoleRecord) -> None:
        self._replace_role_access(cursor, as_role(role))

    @staticmethod
    def _replace_role_access(cursor: Any, role: RoleRecord) -> None:
        cursor.execute(
            "DELETE FROM NL2SQL_APP_ROLE_PERMISSIONS WHERE ROLE_ID = :role_id",
            {"role_id": role.role_id},
        )
        for permission in sorted(role.permissions):
            cursor.execute(
                "INSERT INTO NL2SQL_APP_ROLE_PERMISSIONS (ROLE_ID, PERMISSION_CODE) "
                "VALUES (:role_id, :code)",
                {"role_id": role.role_id, "code": permission},
            )
        try:
            cursor.execute(
                "DELETE FROM NL2SQL_APP_ROLE_PROFILES WHERE ROLE_ID = :role_id",
                {"role_id": role.role_id},
            )
            for profile_id in sorted(role.allowed_profile_ids):
                cursor.execute(
                    "INSERT INTO NL2SQL_APP_ROLE_PROFILES (ROLE_ID, PROFILE_ID) "
                    "VALUES (:role_id, :profile_id)",
                    {"role_id": role.role_id, "profile_id": profile_id},
                )
        except Exception as exc:
            _raise_missing_security_migration_if_needed(exc, "NL2SQL_APP_ROLE_PROFILES")
            raise
