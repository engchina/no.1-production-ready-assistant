"""profile 単位のアクセス制御ヘルパ(nl2sql router / ontology router 共有)。

`Principal.allowed_profile_ids` に基づく行レベルアクセス判定を 1 箇所に集約する。
ローカル実行(PLATFORM_AUTH_MODE=local)のとき、テナント概念が
無いため全 actor 制約を外す(管理者相当)。認証有効時は authorize_api_request が
principal を必ず設定する。
"""

from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException, Request

from app.security.domain import Principal
from app.security.permissions import PROFILE_MANAGE_PERMISSION

from .models import Nl2SqlProfile


def principal_from_request(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


class _ProfileLister(Protocol):
    def list_profiles(self, *, include_archived: bool = False) -> list[Nl2SqlProfile]: ...


def allowed_profile_ids_for_request(request: Request, service: _ProfileLister) -> set[str] | None:
    """利用者が使える業務プロファイル ID。None は制限なし（system admin・認証無効）。

    業務プロファイル管理権限はアーカイブされていない全 profile を使える。画面の route と
    MCP（#231）が同じ判定を使う。
    """

    principal = principal_from_request(request)
    if principal is None or principal.is_system_admin:
        return None
    if principal.has_permission(PROFILE_MANAGE_PERMISSION):
        return {profile.id for profile in service.list_profiles(include_archived=False)}
    return set(principal.allowed_profile_ids)


def profile_access_denied() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail="この業務プロファイルを利用する権限がありません。",
    )


def assert_profile_access(
    request: Request,
    profile_id: str | None,
    *,
    default_profile: bool = False,
) -> None:
    """profile_id が principal の許可リストに含まれることを検証する。

    system admin、認証無効、業務プロファイル管理権限は無条件で許可。許可外は 403。
    """

    principal = principal_from_request(request)
    if principal is None or principal.has_permission(PROFILE_MANAGE_PERMISSION):
        return
    resolved = (profile_id or ("default" if default_profile else "")).strip()
    if resolved and resolved in principal.allowed_profile_ids:
        return
    raise profile_access_denied()
