"""Agent の認証/RBAC の永続化（#215）。DDL はシステムテーブル（`app.system_schema`）の責務。

ユーザー・ロール・セッション（`PLATFORM_*`）は platform の `pr_system_settings.auth.store`。
ここにはロールに付ける Agent のデータだけを置く。

- 権限コード（`AGENT_ROLE_PERMISSIONS`。製品をまたぐ権限昇格の判定も読む）
- 対象範囲のエージェント（`AGENT_ROLE_AGENTS`。エージェントの削除で
  `remove_agent_assignments` が消す）

接続は共通 `.env` の `PLATFORM_ORACLE_*`（`app.oracle_connection`）。Runtime repository の
`AGENT_RUNTIME_ORACLE_*` とは独立している。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.errors import SecurityConflict as SecurityConflict
from pr_system_settings.auth.errors import SecurityNotFound as SecurityNotFound
from pr_system_settings.auth.store import (
    PLATFORM_AUTH_TABLES,
    PRODUCT_ROLE_PERMISSION_TABLES,
    AuthStore,
    ConnectionFactory,
    InMemoryAuthStore,
    OracleAuthStore,
    values_by_role_id,
)

from app.oracle_connection import platform_oracle_connection

from .domain import RoleRecord, as_role

ROLE_PERMISSIONS_TABLE = "AGENT_ROLE_PERMISSIONS"
ROLE_AGENTS_TABLE = "AGENT_ROLE_AGENTS"
AGENT_SECURITY_TABLES = (ROLE_PERMISSIONS_TABLE, ROLE_AGENTS_TABLE)
SECURITY_SCHEMA_OBJECT_NAMES = frozenset(PLATFORM_AUTH_TABLES) | frozenset(AGENT_SECURITY_TABLES)

# 製品をまたぐ権限昇格の判定（platform）が読むテーブル名と一致させる。
if PRODUCT_ROLE_PERMISSION_TABLES.get("agent") != ROLE_PERMISSIONS_TABLE:  # pragma: no cover
    raise RuntimeError(
        "PRODUCT_ROLE_PERMISSION_TABLES['agent'] と Agent の権限テーブル名が違います。"
    )


class SecurityStore(AuthStore, Protocol):
    def get_role(self, role_id: str) -> RoleRecord | None: ...
    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]: ...
    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]: ...
    def create_role(self, role: PlatformRoleRecord) -> RoleRecord: ...
    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord: ...
    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...
    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...
    def remove_agent_assignments(self, agent_id: str) -> int: ...


@dataclass
class InMemorySecurityStore(InMemoryAuthStore):
    """単体テスト用。local / production はどちらも OracleSecurityStore を使う（#750）。"""

    role_class: type[PlatformRoleRecord] = RoleRecord

    def remove_agent_assignments(self, agent_id: str) -> int:
        removed = 0
        with self._lock:
            for role in self.roles.values():
                agent_role = as_role(role)
                if agent_id in agent_role.agent_ids:
                    agent_role.agent_ids.discard(agent_id)
                    removed += 1
        return removed

    def get_role(self, role_id: str) -> RoleRecord | None:
        role = super().get_role(role_id)
        return None if role is None else as_role(role)

    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]:
        return [as_role(role) for role in super().get_roles(role_ids)]

    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        return [as_role(role) for role in super().list_roles(include_archived=include_archived)]

    def create_role(self, role: PlatformRoleRecord) -> RoleRecord:
        return as_role(super().create_role(role))

    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord:
        return as_role(super().update_role(role, expected_version=expected_version))

    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        return as_role(super().archive_role(role_id, expected_version=expected_version))

    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        return as_role(super().restore_role(role_id, expected_version=expected_version))


class OracleSecurityStore(OracleAuthStore):
    """Oracle の store。Agent のロールのデータを hook で同じトランザクションに読み書きする。"""

    role_class = RoleRecord
    schema_object_names = SECURITY_SCHEMA_OBJECT_NAMES

    def __init__(self, connection_factory: ConnectionFactory = platform_oracle_connection) -> None:
        super().__init__(connection_factory)

    def get_role(self, role_id: str) -> RoleRecord | None:
        role = super().get_role(role_id)
        return None if role is None else as_role(role)

    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]:
        return [as_role(role) for role in super().get_roles(role_ids)]

    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]:
        return [as_role(role) for role in super().list_roles(include_archived=include_archived)]

    def create_role(self, role: PlatformRoleRecord) -> RoleRecord:
        return as_role(super().create_role(role))

    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord:
        return as_role(super().update_role(role, expected_version=expected_version))

    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        return as_role(super().archive_role(role_id, expected_version=expected_version))

    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        return as_role(super().restore_role(role_id, expected_version=expected_version))

    def _role_details(self, cursor: Any, role: PlatformRoleRecord) -> RoleRecord:
        return as_role(self._roles_details(cursor, [role])[0])

    def _roles_details(
        self, cursor: Any, roles: Sequence[PlatformRoleRecord]
    ) -> list[PlatformRoleRecord]:
        """ロールの権限と対象範囲を、ロールの数によらず表ごとに一括で読む（#793）。"""
        role_ids = [role.role_id for role in roles]
        permissions = values_by_role_id(
            cursor, "SELECT ROLE_ID, PERMISSION_CODE FROM AGENT_ROLE_PERMISSIONS", role_ids
        )
        agent_ids = values_by_role_id(
            cursor, "SELECT ROLE_ID, AGENT_ID FROM AGENT_ROLE_AGENTS", role_ids
        )
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
                agent_ids=agent_ids.get(role.role_id, set()),
            )
            for role in roles
        ]

    def _replace_role_details(self, cursor: Any, role: PlatformRoleRecord) -> None:
        agent_role = as_role(role)
        self._delete_role_details(cursor, agent_role.role_id)
        for code in sorted(agent_role.permissions):
            cursor.execute(
                "INSERT INTO AGENT_ROLE_PERMISSIONS (ROLE_ID, PERMISSION_CODE) "
                "VALUES (:role_id, :code)",
                {"role_id": agent_role.role_id, "code": code},
            )
        for agent_id in sorted(agent_role.agent_ids):
            cursor.execute(
                "INSERT INTO AGENT_ROLE_AGENTS (ROLE_ID, AGENT_ID) VALUES (:role_id, :agent_id)",
                {"role_id": agent_role.role_id, "agent_id": agent_id},
            )

    def _before_delete_role(self, cursor: Any, role_id: str) -> None:
        # FK は ON DELETE CASCADE だが、ロール本体の削除と同じトランザクションで明示的に消す。
        self._delete_role_details(cursor, role_id)

    @staticmethod
    def _delete_role_details(cursor: Any, role_id: str) -> None:
        binds = {"role_id": role_id}
        cursor.execute("DELETE FROM AGENT_ROLE_PERMISSIONS WHERE ROLE_ID = :role_id", binds)
        cursor.execute("DELETE FROM AGENT_ROLE_AGENTS WHERE ROLE_ID = :role_id", binds)

    def remove_agent_assignments(self, agent_id: str) -> int:
        """削除したエージェントを全ロールの対象範囲から外す（ロールの版は変えない。#750）。"""
        with self.connection(ROLE_AGENTS_TABLE) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM AGENT_ROLE_AGENTS WHERE AGENT_ID = :agent_id", {"agent_id": agent_id}
            )
            removed = int(cursor.rowcount or 0)
            connection.commit()
        return removed
