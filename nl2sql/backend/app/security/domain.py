"""認証/RBAC の内部ドメイン型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_CODE as SYSTEM_ADMIN_ROLE_CODE
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_ID as SYSTEM_ADMIN_ROLE_ID
from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.domain import SessionRecord as SessionRecord
from pr_system_settings.auth.domain import UserIdentity as UserIdentity
from pr_system_settings.auth.domain import UserRecord as UserRecord


@dataclass(slots=True)
class RoleRecord(PlatformRoleRecord):
    """共通のロール（platform）に、NL2SQL の権限・業務プロファイル利用権限を足す。"""

    permissions: set[str] = field(default_factory=set)
    allowed_profile_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Principal(PlatformPrincipal):
    """共通の利用者（platform）に、NL2SQL の業務プロファイル利用権限を足す。"""

    allowed_profile_ids: set[str] = field(default_factory=set)

    def can_use_profile(self, profile_id: str | None) -> bool:
        if self.is_system_admin or "nl2sql.profiles.manage" in self.permissions:
            return True
        normalized = str(profile_id or "").strip()
        return bool(normalized and normalized in self.allowed_profile_ids)


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
