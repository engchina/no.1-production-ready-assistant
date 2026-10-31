"""NL2SQL 固有の認証/RBAC の永続化。DDL は migration の責務。

ユーザー・ロール・セッション（PLATFORM_* テーブル）は platform の
`pr_system_settings.auth.store` が持つ（#212）。ここには次の読み書きだけを置く。

- ロールに付ける NL2SQL の権限（`NL2SQL_APP_ROLE_PERMISSIONS`）
- 業務プロファイル利用権限（`NL2SQL_APP_ROLE_PROFILES`）
- Data Grant（`NL2SQL_APP_DATA_ENTITLEMENTS`）
- DeepSec の状態（`NL2SQL_DEEPSEC_MIGRATIONS`）
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
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
    in_list_binds,
    values_by_role_id,
)

from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.settings import Settings

from .domain import (
    DataEntitlementRecord,
    RoleRecord,
    as_role,
    scope_expression_canonical_json,
    scope_expression_from_json,
    scope_filters_canonical_json,
    scope_filters_from_json,
)

NL2SQL_SECURITY_TABLES = frozenset(
    {
        "NL2SQL_APP_ROLE_PERMISSIONS",
        "NL2SQL_APP_ROLE_PROFILES",
        "NL2SQL_APP_DATA_ENTITLEMENTS",
        "NL2SQL_DEEPSEC_MIGRATIONS",
    }
)
SECURITY_SCHEMA_OBJECT_NAMES = frozenset(PLATFORM_AUTH_TABLES) | NL2SQL_SECURITY_TABLES

_ENTITLEMENTS_PRESENT_MESSAGE = (
    "このロールにはデータ権限が残っています。"
    "Deep Data Security で空の Data Grant を適用してから削除してください。"
)


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
    def get_deepsec_states(self) -> dict[tuple[str, int], dict[str, object]]: ...
    def set_deepsec_state(
        self,
        *,
        version: str,
        step_no: int,
        step_key: str,
        checksum: str,
        status: str,
        error_message: str,
        executed_by_user_uuid: str | None,
    ) -> None: ...
    def clear_deepsec_states(self, *, version: str, step_numbers: list[int]) -> None: ...
    def set_deepsec_entitlement_apply_state(
        self,
        entitlement_id: str,
        *,
        status: str,
        data_grant_name: str = "",
        sql_checksum: str = "",
        error_message: str = "",
    ) -> None: ...
    def clear_deepsec_entitlement_apply_states(self) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class InMemorySecurityStore(InMemoryAuthStore):
    """単体テスト用。production は OracleSecurityStore を使う。"""

    role_class: type[PlatformRoleRecord] = RoleRecord
    deepsec_states: dict[tuple[str, int], dict[str, object]] = field(default_factory=dict)

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

    def _role_delete_blocker(self, role: PlatformRoleRecord) -> SecurityConflict | None:
        if isinstance(role, RoleRecord) and role.entitlements:
            return SecurityConflict(
                _ENTITLEMENTS_PRESENT_MESSAGE, code="SECURITY_ROLE_DELETE_ENTITLEMENTS_PRESENT"
            )
        return None

    def get_deepsec_states(self) -> dict[tuple[str, int], dict[str, object]]:
        with self._lock:
            return copy.deepcopy(self.deepsec_states)

    def set_deepsec_state(
        self,
        *,
        version: str,
        step_no: int,
        step_key: str,
        checksum: str,
        status: str,
        error_message: str,
        executed_by_user_uuid: str | None,
    ) -> None:
        with self._lock:
            self.deepsec_states[(version, step_no)] = {
                "step_key": step_key,
                "checksum": checksum,
                "status": status,
                "error_message": error_message,
                "executed_by_user_uuid": executed_by_user_uuid,
                "executed_at": _now() if status in {"APPLIED", "FAILED"} else None,
            }

    def clear_deepsec_states(self, *, version: str, step_numbers: list[int]) -> None:
        with self._lock:
            for step_no in step_numbers:
                self.deepsec_states.pop((version, step_no), None)

    def set_deepsec_entitlement_apply_state(
        self,
        entitlement_id: str,
        *,
        status: str,
        data_grant_name: str = "",
        sql_checksum: str = "",
        error_message: str = "",
    ) -> None:
        with self._lock:
            for role in self.roles.values():
                for entitlement in getattr(role, "entitlements", []):
                    if entitlement.entitlement_id != entitlement_id:
                        continue
                    entitlement.apply_status = status
                    if data_grant_name:
                        entitlement.data_grant_name = data_grant_name
                    if sql_checksum:
                        entitlement.sql_checksum = sql_checksum
                    entitlement.apply_error_message = error_message
                    entitlement.applied_at = (
                        _now() if status == "APPLIED" else entitlement.applied_at
                    )
                    return
            raise SecurityNotFound("データ権限が見つかりません。")

    def clear_deepsec_entitlement_apply_states(self) -> None:
        with self._lock:
            for role in self.roles.values():
                for entitlement in getattr(role, "entitlements", []):
                    entitlement.apply_status = "PENDING"
                    entitlement.apply_error_message = ""
                    entitlement.sql_checksum = ""
                    entitlement.applied_at = None


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
        """権限・業務プロファイル・Data Grant を、ロールの数によらず表ごとに一括で読む（#793）。"""
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
        entitlements = self._entitlements_by_role(cursor, role_ids)
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
                entitlements=entitlements.get(role.role_id, []),
                allowed_profile_ids=allowed_profile_ids.get(role.role_id, set()),
            )
            for role in roles
        ]

    def _entitlements_by_role(
        self, cursor: Any, role_ids: Sequence[str]
    ) -> dict[str, list[DataEntitlementRecord]]:
        entitlements: dict[str, list[DataEntitlementRecord]] = {}
        for placeholders, binds in in_list_binds("role_", role_ids):
            # 展開するのは生成した bind 名だけ。値はすべて bind する。
            cursor.execute(
                f"""
                SELECT ROLE_ID, ENTITLEMENT_ID, RESOURCE_CODE, SCOPE_CODE, CAPABILITY,
                       TARGET_OWNER, TARGET_OBJECT, TARGET_TYPE, COLUMN_NAMES,
                       SCOPE_MODE, SCOPE_COLUMN, SCOPE_FILTERS, DATA_GRANT_NAME, SQL_CHECKSUM,
                       APPLY_STATUS, APPLY_ERROR_MESSAGE, APPLIED_AT, SCOPE_EXPRESSION
                  FROM NL2SQL_APP_DATA_ENTITLEMENTS
                 WHERE ROLE_ID IN ({placeholders})
                 ORDER BY ROLE_ID, TARGET_OWNER, TARGET_OBJECT, SCOPE_CODE, CAPABILITY,
                          ENTITLEMENT_ID
                """,  # nosec B608
                binds,
            )
            for row in cursor.fetchall():
                role_id = str(row[0])
                item = row[1:]
                entitlements.setdefault(role_id, []).append(
                    DataEntitlementRecord(
                        entitlement_id=str(item[0]),
                        role_id=role_id,
                        resource_code=str(item[1]),
                        scope_code=str(item[2]),
                        capability=str(item[3]),
                        target_owner="" if item[4] is None else str(item[4]),
                        target_object="" if item[5] is None else str(item[5]),
                        target_type="TABLE" if item[6] is None else str(item[6]),
                        column_names=self._json_string_list(item[7]),
                        scope_mode="ALL" if item[8] is None else str(item[8]),
                        scope_column="" if item[9] is None else str(item[9]),
                        scope_filters=scope_filters_from_json(item[10]),
                        data_grant_name="" if item[11] is None else str(item[11]),
                        sql_checksum="" if item[12] is None else str(item[12]),
                        apply_status="PENDING" if item[13] is None else str(item[13]),
                        apply_error_message="" if item[14] in (None, "-") else str(item[14]),
                        applied_at=item[15],
                        scope_expression=scope_expression_from_json(item[16]),
                    )
                )
        return entitlements

    @staticmethod
    def _json_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if hasattr(value, "read"):
            value = value.read()
        try:
            payload = json.loads(str(value or "[]"))
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [str(item) for item in payload if str(item).strip()]

    def _before_delete_role(self, cursor: Any, role_id: str) -> None:
        cursor.execute(
            "SELECT COUNT(*) FROM NL2SQL_APP_DATA_ENTITLEMENTS WHERE ROLE_ID = :role_id",
            {"role_id": role_id},
        )
        if int(cursor.fetchone()[0]) > 0:
            raise SecurityConflict(
                _ENTITLEMENTS_PRESENT_MESSAGE, code="SECURITY_ROLE_DELETE_ENTITLEMENTS_PRESENT"
            )
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

    def get_deepsec_states(self) -> dict[tuple[str, int], dict[str, object]]:
        with self.connection("NL2SQL_DEEPSEC_MIGRATIONS") as conn, conn.cursor() as cursor:
            cursor.execute("""
                SELECT PLAN_VERSION, STEP_NO, STEP_KEY, CHECKSUM, STATUS,
                       ERROR_MESSAGE, EXECUTED_BY_USER_UUID, EXECUTED_AT
                  FROM NL2SQL_DEEPSEC_MIGRATIONS
                """)
            return {
                (str(row[0]), int(row[1])): {
                    "step_key": str(row[2]),
                    "checksum": str(row[3]),
                    "status": str(row[4]),
                    "error_message": "" if row[5] in (None, "-") else str(row[5]),
                    "executed_by_user_uuid": str(row[6]) if row[6] else None,
                    "executed_at": row[7],
                }
                for row in cursor.fetchall()
            }

    def set_deepsec_state(
        self,
        *,
        version: str,
        step_no: int,
        step_key: str,
        checksum: str,
        status: str,
        error_message: str,
        executed_by_user_uuid: str | None,
    ) -> None:
        with self.connection("NL2SQL_DEEPSEC_MIGRATIONS") as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                MERGE INTO NL2SQL_DEEPSEC_MIGRATIONS m
                USING (SELECT :version plan_version, :step_no step_no FROM dual) s
                ON (m.PLAN_VERSION = s.plan_version AND m.STEP_NO = s.step_no)
                WHEN MATCHED THEN UPDATE SET
                    STEP_KEY = :step_key, CHECKSUM = :checksum, STATUS = :status,
                    ERROR_MESSAGE = :error_message, EXECUTED_BY_USER_UUID = :executed_by_user_uuid,
                    EXECUTED_AT = CASE WHEN :status IN ('APPLIED', 'FAILED')
                                       THEN SYSTIMESTAMP ELSE NULL END,
                    UPDATED_AT = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (PLAN_VERSION, STEP_NO, STEP_KEY, CHECKSUM, STATUS, ERROR_MESSAGE,
                     EXECUTED_BY_USER_UUID, EXECUTED_AT)
                VALUES
                    (:version, :step_no, :step_key, :checksum, :status, :error_message,
                     :executed_by_user_uuid, CASE WHEN :status IN ('APPLIED', 'FAILED')
                                        THEN SYSTIMESTAMP ELSE NULL END)
                """,
                {
                    "version": version,
                    "step_no": step_no,
                    "step_key": step_key,
                    "checksum": checksum,
                    "status": status,
                    "error_message": error_message[:2000] or "-",
                    "executed_by_user_uuid": executed_by_user_uuid,
                },
            )
            conn.commit()

    def clear_deepsec_states(self, *, version: str, step_numbers: list[int]) -> None:
        if not step_numbers:
            return
        with self.connection("NL2SQL_DEEPSEC_MIGRATIONS") as conn, conn.cursor() as cursor:
            cursor.executemany(
                """
                DELETE FROM NL2SQL_DEEPSEC_MIGRATIONS
                 WHERE PLAN_VERSION = :version AND STEP_NO = :step_no
                """,
                [{"version": version, "step_no": step_no} for step_no in step_numbers],
            )
            conn.commit()

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
        cursor.execute(
            "DELETE FROM NL2SQL_APP_DATA_ENTITLEMENTS WHERE ROLE_ID = :role_id",
            {"role_id": role.role_id},
        )
        for entitlement in role.entitlements:
            if entitlement.scope_expression is not None and hasattr(cursor, "setinputsizes"):
                import oracledb

                cursor.setinputsizes(scope_expression=oracledb.DB_TYPE_CLOB)
            cursor.execute(
                """
                INSERT INTO NL2SQL_APP_DATA_ENTITLEMENTS
                  (ENTITLEMENT_ID, ROLE_ID, RESOURCE_CODE, SCOPE_CODE, CAPABILITY,
                   TARGET_OWNER, TARGET_OBJECT, TARGET_TYPE, COLUMN_NAMES,
                   SCOPE_MODE, SCOPE_COLUMN, SCOPE_FILTERS, DATA_GRANT_NAME, SQL_CHECKSUM,
                   APPLY_STATUS, APPLY_ERROR_MESSAGE, APPLIED_AT, SCOPE_EXPRESSION)
                VALUES
                  (:entitlement_id, :role_id, :resource_code, :scope_code, :capability_code,
                   :target_owner, :target_object, :target_type, :column_names,
                   :scope_mode, :scope_column, :scope_filters, :data_grant_name, :sql_checksum,
                   :apply_status, :apply_error_message, :applied_at, :scope_expression)
                """,
                {
                    "entitlement_id": entitlement.entitlement_id,
                    "role_id": role.role_id,
                    "resource_code": entitlement.resource_code,
                    "scope_code": entitlement.scope_code,
                    "capability_code": entitlement.capability,
                    "target_owner": entitlement.target_owner or None,
                    "target_object": entitlement.target_object or None,
                    "target_type": entitlement.target_type or "TABLE",
                    "column_names": json.dumps(
                        list(entitlement.column_names),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "scope_mode": entitlement.scope_mode or "ALL",
                    "scope_column": entitlement.scope_column or None,
                    "scope_filters": scope_filters_canonical_json(entitlement.scope_filters),
                    "scope_expression": (
                        scope_expression_canonical_json(entitlement.scope_expression)
                        if entitlement.scope_expression is not None
                        else None
                    ),
                    "data_grant_name": entitlement.data_grant_name or None,
                    "sql_checksum": entitlement.sql_checksum or None,
                    "apply_status": entitlement.apply_status or "PENDING",
                    "apply_error_message": entitlement.apply_error_message[:2000] or "-",
                    "applied_at": entitlement.applied_at,
                },
            )

    def set_deepsec_entitlement_apply_state(
        self,
        entitlement_id: str,
        *,
        status: str,
        data_grant_name: str = "",
        sql_checksum: str = "",
        error_message: str = "",
    ) -> None:
        with self.connection("NL2SQL_APP_DATA_ENTITLEMENTS") as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE NL2SQL_APP_DATA_ENTITLEMENTS
                   SET APPLY_STATUS = :status,
                       DATA_GRANT_NAME = COALESCE(:data_grant_name, DATA_GRANT_NAME),
                       SQL_CHECKSUM = COALESCE(:sql_checksum, SQL_CHECKSUM),
                       APPLY_ERROR_MESSAGE = :error_message,
                       APPLIED_AT = CASE
                         WHEN :status = 'APPLIED' THEN SYSTIMESTAMP
                         WHEN :status = 'PENDING' THEN NULL
                         ELSE APPLIED_AT
                       END
                 WHERE ENTITLEMENT_ID = :entitlement_id
                """,
                {
                    "entitlement_id": entitlement_id,
                    "status": status,
                    "data_grant_name": data_grant_name or None,
                    "sql_checksum": sql_checksum or None,
                    "error_message": error_message[:2000] or "-",
                },
            )
            if cursor.rowcount == 0:
                raise SecurityNotFound("データ権限が見つかりません。")
            conn.commit()

    def clear_deepsec_entitlement_apply_states(self) -> None:
        with self.connection("NL2SQL_APP_DATA_ENTITLEMENTS") as conn, conn.cursor() as cursor:
            cursor.execute("""
                UPDATE NL2SQL_APP_DATA_ENTITLEMENTS
                   SET APPLY_STATUS = 'PENDING',
                       SQL_CHECKSUM = NULL,
                       APPLY_ERROR_MESSAGE = '-',
                       APPLIED_AT = NULL,
                       UPDATED_AT = SYSTIMESTAMP
                """)
            conn.commit()
