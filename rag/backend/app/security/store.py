"""RAG の認証/RBAC の永続化（#214）。DDL はシステムテーブル初期化（`oracle_schema`）の責務。

ユーザー・ロール・セッション（`PLATFORM_*`）は platform の `pr_system_settings.auth.store`。
ここにはロールに付ける RAG のデータだけを置く。

- 権限コード（`RAG_ROLE_PERMISSIONS`。製品をまたぐ権限昇格の判定も読む）
- 対象範囲の検索・回答プロファイル（`RAG_ROLE_SEARCH_ANSWER_PROFILES`）
- 対象範囲のナレッジベース（`RAG_ROLE_KNOWLEDGE_BASES`）
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.errors import SecurityConflict as SecurityConflict
from pr_system_settings.auth.errors import SecurityNotFound as SecurityNotFound
from pr_system_settings.auth.store import (
    PLATFORM_AUTH_TABLES,
    PRODUCT_ROLE_PERMISSION_TABLES,
    AuthStore,
    InMemoryAuthStore,
    OracleAuthStore,
    values_by_role_id,
)

from .domain import RoleRecord, as_role

ROLE_PERMISSIONS_TABLE = "RAG_ROLE_PERMISSIONS"
ROLE_SEARCH_ANSWER_PROFILES_TABLE = "RAG_ROLE_SEARCH_ANSWER_PROFILES"
ROLE_KNOWLEDGE_BASES_TABLE = "RAG_ROLE_KNOWLEDGE_BASES"
RAG_SECURITY_TABLES = (
    ROLE_PERMISSIONS_TABLE,
    ROLE_SEARCH_ANSWER_PROFILES_TABLE,
    ROLE_KNOWLEDGE_BASES_TABLE,
)
SECURITY_SCHEMA_OBJECT_NAMES = frozenset(PLATFORM_AUTH_TABLES) | frozenset(RAG_SECURITY_TABLES)

# 製品をまたぐ権限昇格の判定（platform）が読むテーブル名と一致させる。
if PRODUCT_ROLE_PERMISSION_TABLES.get("rag") != ROLE_PERMISSIONS_TABLE:  # pragma: no cover
    raise RuntimeError("PRODUCT_ROLE_PERMISSION_TABLES['rag'] と RAG の権限テーブル名が違います。")


class SecurityStore(AuthStore, Protocol):
    def get_role(self, role_id: str) -> RoleRecord | None: ...
    def get_roles(self, role_ids: Sequence[str]) -> list[RoleRecord]: ...
    def list_roles(self, *, include_archived: bool = False) -> list[RoleRecord]: ...
    def create_role(self, role: PlatformRoleRecord) -> RoleRecord: ...
    def update_role(self, role: PlatformRoleRecord, *, expected_version: int) -> RoleRecord: ...
    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...
    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...


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
    """Oracle の store。RAG のロールのデータを hook で同じトランザクションに読み書きする。"""

    role_class = RoleRecord
    schema_object_names = SECURITY_SCHEMA_OBJECT_NAMES

    def __init__(self) -> None:
        super().__init__(_pooled_connection)

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
            cursor, "SELECT ROLE_ID, PERMISSION_CODE FROM RAG_ROLE_PERMISSIONS", role_ids
        )
        search_answer_profile_ids = values_by_role_id(
            cursor,
            "SELECT ROLE_ID, SEARCH_ANSWER_PROFILE_ID FROM RAG_ROLE_SEARCH_ANSWER_PROFILES",
            role_ids,
        )
        knowledge_base_ids = values_by_role_id(
            cursor, "SELECT ROLE_ID, KNOWLEDGE_BASE_ID FROM RAG_ROLE_KNOWLEDGE_BASES", role_ids
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
                search_answer_profile_ids=search_answer_profile_ids.get(role.role_id, set()),
                knowledge_base_ids=knowledge_base_ids.get(role.role_id, set()),
            )
            for role in roles
        ]

    def _replace_role_details(self, cursor: Any, role: PlatformRoleRecord) -> None:
        rag_role = as_role(role)
        self._delete_role_details(cursor, rag_role.role_id)
        for code in sorted(rag_role.permissions):
            cursor.execute(
                "INSERT INTO RAG_ROLE_PERMISSIONS (ROLE_ID, PERMISSION_CODE) "
                "VALUES (:role_id, :code)",
                {"role_id": rag_role.role_id, "code": code},
            )
        for search_answer_profile_id in sorted(rag_role.search_answer_profile_ids):
            cursor.execute(
                "INSERT INTO RAG_ROLE_SEARCH_ANSWER_PROFILES (ROLE_ID, SEARCH_ANSWER_PROFILE_ID) "
                "VALUES (:role_id, :search_answer_profile_id)",
                {"role_id": rag_role.role_id, "search_answer_profile_id": search_answer_profile_id},
            )
        for knowledge_base_id in sorted(rag_role.knowledge_base_ids):
            cursor.execute(
                "INSERT INTO RAG_ROLE_KNOWLEDGE_BASES (ROLE_ID, KNOWLEDGE_BASE_ID) "
                "VALUES (:role_id, :knowledge_base_id)",
                {"role_id": rag_role.role_id, "knowledge_base_id": knowledge_base_id},
            )

    def _before_delete_role(self, cursor: Any, role_id: str) -> None:
        # FK は ON DELETE CASCADE だが、ロール本体の削除と同じトランザクションで明示的に消す。
        self._delete_role_details(cursor, role_id)

    @staticmethod
    def _delete_role_details(cursor: Any, role_id: str) -> None:
        binds = {"role_id": role_id}
        cursor.execute("DELETE FROM RAG_ROLE_PERMISSIONS WHERE ROLE_ID = :role_id", binds)
        cursor.execute(
            "DELETE FROM RAG_ROLE_SEARCH_ANSWER_PROFILES WHERE ROLE_ID = :role_id", binds
        )
        cursor.execute("DELETE FROM RAG_ROLE_KNOWLEDGE_BASES WHERE ROLE_ID = :role_id", binds)


@contextmanager
def _pooled_connection() -> Iterator[Any]:
    """RAG の Oracle 接続 pool から、未コミットの変更を持たない接続を借りる。"""
    from app.clients.oracle import OracleClient

    connection = OracleClient().acquire_connection()
    try:
        yield connection
    except BaseException:
        try:
            connection.rollback()
        finally:
            connection.close()
        raise
    else:
        connection.close()
