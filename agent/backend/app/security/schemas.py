"""Agent の認証/RBAC API の schema（#215）。

ユーザー・ロールの共通項目は platform の `pr_system_settings.users_roles`。ここではロールに
Agent の権限と対象範囲を、ログイン中の利用者に実効の権限と対象範囲を足す。
"""

from __future__ import annotations

from pr_system_settings.auth.router import DisplayTimezoneData
from pr_system_settings.users_roles import RoleData as SharedRoleData
from pydantic import BaseModel, Field

from .domain import Principal, RoleRecord
from .permissions import PermissionDefinition, normalize_permission_codes

MAX_ACCESS_TARGETS = 1000


class CurrentUserData(DisplayTimezoneData):
    """`/auth/me` などの応答。`permissions` は implies を展開した実効権限。"""

    user_uuid: str
    login_user_id: str
    display_name: str
    status: str
    force_password_change: bool
    role_codes: list[str]
    # ロールの表示名（`role_codes` と同じ順）。サイドナビのアカウント欄に出す（#1644）。
    role_display_names: list[str]
    is_system_admin: bool
    permissions: list[str]
    # None（null）は制限なし。
    allowed_agent_ids: list[str] | None = None
    debug_mode: bool = False
    password_change_allowed: bool

    @classmethod
    def from_principal(cls, principal: Principal, *, debug_mode: bool = False) -> CurrentUserData:
        return cls(
            user_uuid=principal.user_uuid,
            login_user_id=principal.login_user_id,
            display_name=principal.display_name,
            status=principal.status,
            force_password_change=principal.force_password_change,
            role_codes=list(principal.role_codes),
            role_display_names=principal.role_names(),
            is_system_admin=principal.is_system_admin,
            permissions=sorted(principal.permissions),
            allowed_agent_ids=_sorted_or_none(principal.allowed_agent_ids),
            debug_mode=debug_mode,
            password_change_allowed=principal.password_change_allowed and not debug_mode,
        )


class RoleData(SharedRoleData):
    """共通のロール項目に、Agent の権限と対象範囲を足す。"""

    permissions: list[str]
    agent_ids: list[str]

    @classmethod
    def from_record(cls, role: RoleRecord) -> RoleData:
        return cls(
            role_id=role.role_id,
            role_code=role.role_code,
            display_name=role.display_name,
            description=role.description,
            is_built_in=role.is_built_in,
            archived=role.archived,
            version=role.version,
            permissions=sorted(normalize_permission_codes(role.permissions)),
            agent_ids=sorted(role.agent_ids),
        )


class RoleAccessUpdateRequest(BaseModel):
    """権限管理画面の保存。ロールの Agent 権限と対象範囲だけを置き換える。"""

    version: int = Field(ge=1)
    permissions: list[str] = Field(default_factory=list, max_length=200)
    agent_ids: list[str] = Field(default_factory=list, max_length=MAX_ACCESS_TARGETS)


class PermissionData(BaseModel):
    code: str
    group: str
    label: str
    description: str
    implies: list[str]

    @classmethod
    def from_definition(cls, definition: PermissionDefinition) -> PermissionData:
        return cls(
            code=definition.code,
            group=definition.group,
            label=definition.label,
            description=definition.description,
            implies=list(definition.implies),
        )


class AgentTargetData(BaseModel):
    """権限管理画面で選べるエージェント（Runtime repository の業務 Agent）。"""

    id: str
    name: str
    description: str | None = None
    # enabled / disabled
    status: str


def _sorted_or_none(values: frozenset[str] | None) -> list[str] | None:
    return None if values is None else sorted(values)
