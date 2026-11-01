"""認証/RBAC API schema。"""

from __future__ import annotations

from pr_system_settings.auth.router import DisplayTimezoneData
from pr_system_settings.auth.router import LoginRequest as LoginRequest
from pr_system_settings.auth.router import PasswordChangeRequest as PasswordChangeRequest
from pr_system_settings.auth.router import assigned_role_data as assigned_role_data
from pr_system_settings.auth.router import user_data as user_data
from pr_system_settings.users_roles import AssignedRoleData as AssignedRoleData
from pr_system_settings.users_roles import PasswordResetData as PasswordResetData
from pr_system_settings.users_roles import PasswordResetRequest as PasswordResetRequest
from pr_system_settings.users_roles import RoleCreateRequest as RoleCreateRequest
from pr_system_settings.users_roles import RoleData as SharedRoleData
from pr_system_settings.users_roles import RoleDeleteData as RoleDeleteData
from pr_system_settings.users_roles import RoleUpdateRequest as RoleUpdateRequest
from pr_system_settings.users_roles import UserCreateData as UserCreateData
from pr_system_settings.users_roles import UserCreateRequest as UserCreateRequest
from pr_system_settings.users_roles import UserData as UserData
from pr_system_settings.users_roles import UserDeleteData as UserDeleteData
from pr_system_settings.users_roles import UserUpdateRequest as UserUpdateRequest
from pr_system_settings.users_roles import VersionRequest as VersionRequest
from pydantic import BaseModel, Field

from .domain import (
    Principal,
    RoleRecord,
)
from .permissions import PermissionDefinition, normalize_permission_codes


class RolePermissionsUpdateRequest(BaseModel):
    """権限管理画面の保存。ロールの menu 権限と業務プロファイル利用権限だけを更新する。"""

    version: int = Field(ge=1)
    permissions: list[str] = Field(default_factory=list)
    allowed_profile_ids: list[str] | None = None


# 版だけを送る操作は共通契約の VersionRequest と同じ形。
RoleArchiveRequest = VersionRequest
RoleRestoreRequest = VersionRequest


class RoleData(SharedRoleData):
    """共通のロール項目に、NL2SQL の権限・業務プロファイル利用権限を足す。"""

    permissions: list[str]
    allowed_profile_ids: list[str]

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
            allowed_profile_ids=sorted(role.allowed_profile_ids),
        )


class CurrentUserData(DisplayTimezoneData):
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
    allowed_profile_ids: list[str] = Field(default_factory=list)
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
            role_codes=principal.role_codes,
            role_display_names=principal.role_names(),
            is_system_admin=principal.is_system_admin,
            permissions=sorted(principal.permissions),
            allowed_profile_ids=sorted(principal.allowed_profile_ids),
            debug_mode=debug_mode,
            password_change_allowed=principal.password_change_allowed and not debug_mode,
        )


class ProfileAccessRoleData(BaseModel):
    """業務プロファイルを使えるロール。画面はロール名を出し、ID（UUID）・コードは出さない（#1682）。"""

    role_id: str
    role_code: str
    display_name: str


class ProfileAccessProfileData(BaseModel):
    id: str
    name: str
    category: str = ""
    description: str = ""
    archived: bool = False
    # ロール名の順。ID だけを返すと画面が UUID を出すため、名前とコードを添える（#1682）。
    allowed_roles: list[ProfileAccessRoleData] = Field(default_factory=list)


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
