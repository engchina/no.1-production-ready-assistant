"""Agent の認証/RBAC のドメイン型（#215）。

ユーザー・ロール・セッションは platform の `PLATFORM_*`（`pr_system_settings.auth`）。
ここではロールに Agent の権限と対象範囲（エージェント）を、利用者に実効の対象範囲を足す。
検索・回答プロファイルの判定は RAG が Run の利用者のサービストークンで行う（#750）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pr_system_settings.auth.domain import (
    CONFIGURED_SYSTEM_ADMIN_USER_UUID as CONFIGURED_SYSTEM_ADMIN_USER_UUID,
)
from pr_system_settings.auth.domain import LOCAL_DEBUG_USER_UUID as LOCAL_DEBUG_USER_UUID
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_CODE as SYSTEM_ADMIN_ROLE_CODE
from pr_system_settings.auth.domain import SYSTEM_ADMIN_ROLE_ID as SYSTEM_ADMIN_ROLE_ID
from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord


@dataclass(slots=True)
class RoleRecord(PlatformRoleRecord):
    """共通のロールに、Agent の権限と対象範囲（エージェント）を足す。"""

    permissions: set[str] = field(default_factory=set)
    agent_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Principal(PlatformPrincipal):
    """共通の利用者に、Agent の対象範囲を足す。

    None は制限なし（SYSTEM_ADMIN・構成管理者・local、または `agent.admin` を持つとき）。
    制限ありのときは、有効なロールに割り当てた対象の和集合。
    """

    allowed_agent_ids: frozenset[str] | None = None

    def can_use_agent(self, agent_id: str) -> bool:
        allowed = self.allowed_agent_ids
        return allowed is None or agent_id in allowed


def as_role(role: PlatformRoleRecord) -> RoleRecord:
    """platform の store / service が返すロールを Agent のロールとして扱う。"""
    if not isinstance(role, RoleRecord):
        raise TypeError("Agent のロールではありません。")
    return role


def as_principal(principal: PlatformPrincipal) -> Principal:
    """platform の service が返す利用者を Agent の利用者として扱う。"""
    if not isinstance(principal, Principal):
        raise TypeError("Agent の利用者ではありません。")
    return principal
