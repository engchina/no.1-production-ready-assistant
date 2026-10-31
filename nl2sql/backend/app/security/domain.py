"""認証/RBAC の内部ドメイン型。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_CODE as SYSTEM_ADMIN_ROLE_CODE
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_ID as SYSTEM_ADMIN_ROLE_ID
from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.domain import SessionRecord as SessionRecord
from pr_system_settings.auth.domain import UserIdentity as UserIdentity
from pr_system_settings.auth.domain import UserRecord as UserRecord

from app.features.nl2sql.object_identity import canonical_object_part

SCOPE_FILTER_CODE_PREFIX = "FILTERS:"
LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE = "APP_USER_ID"
LOGIN_USER_ID_SCOPE_VALUE_SOURCE = "LOGIN_USER_ID"


@dataclass(slots=True)
class DataEntitlementScopeFilter:
    column_name: str
    operator: str
    value_type: str = "TEXT"
    value_source: str = "LITERAL"
    value: str = ""
    value_to: str = ""
    values: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DataEntitlementRecord:
    entitlement_id: str
    role_id: str
    resource_code: str
    scope_code: str
    capability: str
    target_owner: str = ""
    target_object: str = ""
    target_type: str = "TABLE"
    column_names: list[str] = field(default_factory=list)
    scope_mode: str = "ALL"
    scope_column: str = ""
    scope_filters: list[DataEntitlementScopeFilter] = field(default_factory=list)
    scope_expression: dict[str, Any] | None = None
    scope_expression_version: int | None = None
    data_grant_name: str = ""
    sql_checksum: str = ""
    apply_status: str = "PENDING"
    apply_error_message: str = ""
    applied_at: datetime | None = None


@dataclass(slots=True)
class RoleRecord(PlatformRoleRecord):
    """共通のロール（platform）に、NL2SQL の権限・Data Grant・業務プロファイル利用権限を足す。"""

    permissions: set[str] = field(default_factory=set)
    entitlements: list[DataEntitlementRecord] = field(default_factory=list)
    allowed_profile_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Principal(PlatformPrincipal):
    """共通の利用者（platform）に、NL2SQL の Data Grant と業務プロファイル利用権限を足す。"""

    data_entitlements: list[DataEntitlementRecord] = field(default_factory=list)
    allowed_profile_ids: set[str] = field(default_factory=set)

    def can_use_profile(self, profile_id: str | None) -> bool:
        if self.is_system_admin or "nl2sql.profiles.manage" in self.permissions:
            return True
        normalized = str(profile_id or "").strip()
        return bool(normalized and normalized in self.allowed_profile_ids)


def _scope_filter_column_token(column_name: str) -> str:
    try:
        return canonical_object_part(column_name)
    except ValueError:
        # 検証前の旧データでも canonical JSON は作れるようにする（SQL 生成時に拒否される）。
        return column_name.strip().upper()


def scope_filter_payload(filter_item: DataEntitlementScopeFilter) -> dict[str, object]:
    value_source = filter_item.value_source.strip().upper() or "LITERAL"
    if value_source == LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE:
        value_source = LOGIN_USER_ID_SCOPE_VALUE_SOURCE
    payload: dict[str, object] = {
        "column_name": _scope_filter_column_token(filter_item.column_name),
        "operator": filter_item.operator.strip().upper(),
        "value_type": filter_item.value_type.strip().upper(),
        "value": filter_item.value.strip(),
        "value_to": filter_item.value_to.strip(),
        "values": [str(item).strip() for item in filter_item.values if str(item).strip()],
    }
    if value_source != "LITERAL":
        payload["value_source"] = value_source
    return payload


def scope_filters_canonical_json(filters: list[DataEntitlementScopeFilter]) -> str:
    return json.dumps(
        [scope_filter_payload(item) for item in filters],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def scope_filters_scope_code(filters: list[DataEntitlementScopeFilter]) -> str:
    digest = hashlib.sha256(scope_filters_canonical_json(filters).encode("utf-8")).hexdigest()
    return f"{SCOPE_FILTER_CODE_PREFIX}{digest[:32].upper()}"


def scope_filter_from_mapping(value: Mapping[str, Any]) -> DataEntitlementScopeFilter:
    raw_values = value.get("values", [])
    values = raw_values if isinstance(raw_values, list) else []
    value_source = str(value.get("value_source") or "LITERAL")
    if value_source.strip().upper() == LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE:
        value_source = LOGIN_USER_ID_SCOPE_VALUE_SOURCE
    return DataEntitlementScopeFilter(
        column_name=str(value.get("column_name") or ""),
        operator=str(value.get("operator") or ""),
        value_type=str(value.get("value_type") or "TEXT"),
        value_source=value_source,
        value=str(value.get("value") or ""),
        value_to=str(value.get("value_to") or ""),
        values=[str(item) for item in values],
    )


def scope_filters_from_json(value: object) -> list[DataEntitlementScopeFilter]:
    if value in (None, "", "[]"):
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    filters: list[DataEntitlementScopeFilter] = []
    for item in parsed:
        if isinstance(item, Mapping):
            filters.append(scope_filter_from_mapping(item))
    return filters


def scope_expression_canonical_json(expression: dict[str, Any] | None) -> str:
    return json.dumps(expression, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def scope_expression_scope_code(expression: dict[str, Any] | None) -> str:
    return (
        "EXPRESSION:"
        + hashlib.sha256(scope_expression_canonical_json(expression).encode()).hexdigest()
    )


def scope_expression_from_json(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "read"):
        value = value.read()
    parsed = json.loads(str(value))
    if parsed is not None and not isinstance(parsed, dict):
        raise ValueError("保存済み条件ツリーが不正です。")
    return parsed


def as_role(role: PlatformRoleRecord) -> RoleRecord:
    """platform の store / service が返すロールを NL2SQL のロールとして扱う。"""
    if not isinstance(role, RoleRecord):
        raise TypeError("NL2SQL のロールではありません。")
    return role


def as_principal(principal: PlatformPrincipal) -> Principal:
    """platform の service が返す利用者を NL2SQL の利用者として扱う。"""
    if not isinstance(principal, Principal):
        raise TypeError("NL2SQL の利用者ではありません。")
    return principal
