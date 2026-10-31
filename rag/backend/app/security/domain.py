"""RAG の認証/RBAC のドメイン型（#214）。

ユーザー・ロール・セッションは platform の `PLATFORM_*`（`pr_system_settings.auth`）。
ここではロールに RAG の権限と対象範囲（検索・回答プロファイル・ナレッジベース）を、利用者に実効の
対象範囲を足す。
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
    """共通のロールに、RAG の権限と対象範囲（検索・回答プロファイル・ナレッジベース）を足す。"""

    permissions: set[str] = field(default_factory=set)
    search_answer_profile_ids: set[str] = field(default_factory=set)
    knowledge_base_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Principal(PlatformPrincipal):
    """共通の利用者に、RAG の対象範囲を足す。

    None は制限なし（SYSTEM_ADMIN・構成管理者・local、または `rag.*.manage` を持つとき）。
    制限ありのときは、有効なロールに割り当てた対象の和集合。
    """

    allowed_search_answer_profile_ids: frozenset[str] | None = None
    allowed_knowledge_base_ids: frozenset[str] | None = None

    def can_use_search_answer_profile(self, search_answer_profile_id: str) -> bool:
        allowed = self.allowed_search_answer_profile_ids
        return allowed is None or search_answer_profile_id in allowed

    def can_use_knowledge_base(self, knowledge_base_id: str) -> bool:
        allowed = self.allowed_knowledge_base_ids
        return allowed is None or knowledge_base_id in allowed


def as_role(role: PlatformRoleRecord) -> RoleRecord:
    """platform の store / service が返すロールを RAG のロールとして扱う。"""
    if not isinstance(role, RoleRecord):
        raise TypeError("RAG のロールではありません。")
    return role


def as_principal(principal: PlatformPrincipal) -> Principal:
    """platform の service が返す利用者を RAG の利用者として扱う。"""
    if not isinstance(principal, Principal):
        raise TypeError("RAG の利用者ではありません。")
    return principal
