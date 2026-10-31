"""認証・ユーザー・ロール（platform の共通 router）と、NL2SQL の権限 API。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pr_backend_core import ApiResponse, Page
from pr_backend_core.api import (
    ACCESS_TARGET_PAGE_LIMIT_MAX,
    OffsetParams,
    offset_params,
    paginate_slice,
)
from pr_system_settings.auth.domain import Principal as PlatformPrincipal
from pr_system_settings.auth.domain import RoleRecord as PlatformRoleRecord
from pr_system_settings.auth.router import build_auth_router

from app.api.concurrency import run_sync_io
from app.settings import get_settings

from .dependencies import current_principal, local_debug_principal, request_context
from .domain import SYSTEM_ADMIN_ROLE_CODE, as_principal, as_role
from .permissions import PERMISSION_CATALOG, grants_all_profile_access
from .schemas import (
    CurrentUserData,
    PermissionData,
    ProfileAccessProfileData,
    ProfileAccessRoleData,
    RoleData,
    RolePermissionsUpdateRequest,
)
from .service import get_security_service

router = APIRouter(tags=["security"])
run_in_threadpool = run_sync_io


def _current_user_data(principal: PlatformPrincipal, debug_mode: bool) -> CurrentUserData:
    return CurrentUserData.from_principal(as_principal(principal), debug_mode=debug_mode)


def _role_data(role: PlatformRoleRecord) -> RoleData:
    return RoleData.from_record(as_role(role))


# 認証 API とユーザー管理・ロール管理（基本情報）は 3 製品共通（platform。#212）。
auth_router = build_auth_router(
    get_service=lambda: get_security_service(),
    get_settings=lambda: get_settings(),
    current_principal=current_principal,
    request_context=request_context,
    local_debug_principal=local_debug_principal,
    current_user_model=CurrentUserData,
    current_user_data=_current_user_data,
    role_model=RoleData,
    role_data=_role_data,
    # 外部の MCP クライアント向けの API キー（`/account/api-keys*`・`/security/api-keys*`。#1805）。
    api_keys=True,
    # 画面の接続先に出す公開の URL（PLATFORM_PUBLIC_BASE_URL。#1870）。
    public_base_url=lambda: get_settings().app_public_base_url,
)
router.include_router(auth_router)


@router.put("/security/roles/{role_id}/permissions", response_model=ApiResponse[RoleData])
def update_role_permissions(
    role_id: str,
    payload: RolePermissionsUpdateRequest,
    request: Request,
    response: Response,
) -> ApiResponse[RoleData]:
    """権限管理画面の保存。menu 権限と業務プロファイル利用権限だけを更新する（#206）。"""
    actor = current_principal(request)
    request_id, client_ip = request_context(request)
    role = get_security_service().update_role(
        role_id,
        expected_version=payload.version,
        permissions=set(payload.permissions),
        allowed_profile_ids=(
            set(payload.allowed_profile_ids) if payload.allowed_profile_ids is not None else None
        ),
        actor=actor,
        request_id=request_id,
        client_ip=client_ip,
    )
    response.headers["ETag"] = f'"{role.version}"'
    return ApiResponse(data=RoleData.from_record(role))


# 権限管理の「利用できる対象」の候補の 1 ページの上限（#608）。
# 画面は 50 件ずつ読み、選択済みの名前は `ids` で読む。


def _matches_profile_query(query: str, *values: str) -> bool:
    """業務プロファイルの候補の検索（名前・カテゴリ・説明の部分一致。大文字と小文字を区別しない）。"""
    needle = query.strip().casefold()
    return not needle or any(needle in value.casefold() for value in values)


@router.get(
    "/security/profile-access/profiles",
    response_model=ApiResponse[Page[ProfileAccessProfileData]],
)
def list_profile_access_profiles(
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=50, max_limit=ACCESS_TARGET_PAGE_LIMIT_MAX))
    ],
    include_archived: Annotated[bool, Query()] = False,
    q: Annotated[str, Query(max_length=200)] = "",
    ids: Annotated[list[str] | None, Query(max_length=ACCESS_TARGET_PAGE_LIMIT_MAX)] = None,
) -> ApiResponse[Page[ProfileAccessProfileData]]:
    """権限管理画面向けに業務 profile の利用権限カタログを、検索とページングで返す（#608）。

    - `q`: 名前・カテゴリ・説明の部分一致。
    - `ids`: その ID だけ（ロールに選択済みの対象の名前の解決に使う。アーカイブ済みも返す）。
    """
    from app.features.nl2sql.service import nl2sql_service

    selected = set(ids) if ids is not None else None
    profiles = [
        profile
        for profile in nl2sql_service.list_profiles(
            include_archived=include_archived or selected is not None
        )
        if (selected is None or profile.id in selected)
        and _matches_profile_query(q, profile.name, profile.category, profile.description)
    ]
    page = paginate_slice(profiles, paging)
    page_profiles = page.items
    profile_ids = {profile.id for profile in page_profiles}
    roles = get_security_service().list_roles(include_archived=True)
    allowed_roles_by_profile: dict[str, list[ProfileAccessRoleData]] = {
        profile_id: [] for profile_id in profile_ids
    }
    for role in roles:
        if role.archived:
            continue
        access_role = ProfileAccessRoleData(
            role_id=role.role_id, role_code=role.role_code, display_name=role.display_name
        )
        if role.role_code == SYSTEM_ADMIN_ROLE_CODE or grants_all_profile_access(role.permissions):
            for profile_id in profile_ids:
                allowed_roles_by_profile[profile_id].append(access_role)
            continue
        for profile_id in role.allowed_profile_ids:
            if profile_id in allowed_roles_by_profile:
                allowed_roles_by_profile[profile_id].append(access_role)
    return ApiResponse(
        data=Page(
            items=[
                ProfileAccessProfileData(
                    id=profile.id,
                    name=profile.name,
                    category=profile.category,
                    description=profile.description,
                    archived=profile.archived,
                    allowed_roles=sorted(
                        allowed_roles_by_profile.get(profile.id, []),
                        key=lambda role: (
                            role.display_name or role.role_code,
                            role.role_code,
                        ),
                    ),
                )
                for profile in page_profiles
            ],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_next=page.has_next,
        )
    )


@router.get("/security/permissions", response_model=ApiResponse[list[PermissionData]])
def permission_catalog() -> ApiResponse[list[PermissionData]]:
    return ApiResponse(data=[PermissionData.from_definition(item) for item in PERMISSION_CATALOG])
