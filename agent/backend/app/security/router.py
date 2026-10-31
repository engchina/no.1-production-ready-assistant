"""認証・ユーザー・ロール（platform の共通 router）と、Agent の権限管理 API（#215）。

- `/auth/*`・`/security/users*`・`/security/roles*`: platform の `build_auth_router`
- `GET /security/permissions`: 権限カタログ
- `GET /security/access-targets/agents`: 権限管理で選べるエージェント（検索とページング。#608）
- `PUT /security/roles/{role_id}/access`: ロールの Agent 権限と対象範囲（エージェント）の保存

検索・回答プロファイルの判定は RAG が Run の利用者の
サービストークンで行うため、Agent は持たない（#750）。

"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
from starlette.concurrency import run_in_threadpool

import app.settings as app_settings
from app.features.agent.runtime import runtime_repository

from .dependencies import current_principal, local_debug_principal, request_context
from .domain import as_principal, as_role
from .permissions import PERMISSION_CATALOG
from .schemas import (
    AgentTargetData,
    CurrentUserData,
    PermissionData,
    RoleAccessUpdateRequest,
    RoleData,
)
from .service import get_security_service

router = APIRouter(tags=["security"])


def _current_user_data(principal: PlatformPrincipal, debug_mode: bool) -> CurrentUserData:
    return CurrentUserData.from_principal(as_principal(principal), debug_mode=debug_mode)


def _role_data(role: PlatformRoleRecord) -> RoleData:
    return RoleData.from_record(as_role(role))


# 認証 API とユーザー管理・ロール管理（基本情報）は 3 製品共通（platform。#212）。
auth_router = build_auth_router(
    get_service=lambda: get_security_service(),
    get_settings=lambda: app_settings.get_settings(),
    current_principal=current_principal,
    request_context=request_context,
    local_debug_principal=local_debug_principal,
    current_user_model=CurrentUserData,
    current_user_data=_current_user_data,
    role_model=RoleData,
    role_data=_role_data,
    # 外部の MCP クライアント向けの API キー（#1874。自分のキーの発行・一覧・削除と管理の API）。
    api_keys=True,
    # 画面の接続先に出す公開の URL（PLATFORM_PUBLIC_BASE_URL。#1870）。
    public_base_url=lambda: app_settings.get_settings().app_public_base_url,
)
router.include_router(auth_router)


@router.get("/security/permissions", response_model=ApiResponse[list[PermissionData]])
def permission_catalog() -> ApiResponse[list[PermissionData]]:
    """権限管理画面の権限カタログ（code / group / label / description / implies）。"""
    return ApiResponse(data=[PermissionData.from_definition(item) for item in PERMISSION_CATALOG])


def _matches_target_query(query: str, *values: str | None) -> bool:
    """権限管理の候補の検索（名前・ID・説明の部分一致。大文字と小文字を区別しない）。"""
    needle = query.strip().casefold()
    return not needle or any(needle in (value or "").casefold() for value in values)


@router.get("/security/access-targets/agents", response_model=ApiResponse[Page[AgentTargetData]])
def list_agent_access_targets(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=50, max_limit=ACCESS_TARGET_PAGE_LIMIT_MAX))
    ],
    q: Annotated[str, Query(max_length=200)] = "",
    ids: Annotated[list[str] | None, Query(max_length=ACCESS_TARGET_PAGE_LIMIT_MAX)] = None,
) -> ApiResponse[Page[AgentTargetData]]:
    """権限管理画面で選べるエージェント（Runtime repository の業務 Agent。無効を含む）。

    検索とページング（#608。1 ページの上限は 3 製品で同じ `ACCESS_TARGET_PAGE_LIMIT_MAX`）:
    - `q`: 名前・ID・説明の部分一致。
    - `ids`: その ID だけ（ロールに選択済みの対象の名前の解決に使う）。画面は 50 件ずつ読む。
    - 一覧は利用者の対象範囲で絞る（SYSTEM_ADMIN と `agent.admin` を持つ利用者は全件）。
    - 個人の業務 Agent（持ち主あり）は、共有の申請を承認するまでロールに割り当てないので出さない
      （#2523）。
    """
    principal = as_principal(current_principal(request))
    selected = set(ids) if ids is not None else None
    agents = [
        AgentTargetData(
            id=agent.id,
            name=agent.name,
            description=agent.description or None,
            status="enabled" if agent.enabled else "disabled",
        )
        for agent in runtime_repository.list_agents()
        if principal.can_use_agent(agent.id)
        and agent.owner_user_uuid is None
        and (selected is None or agent.id in selected)
        and _matches_target_query(q, agent.name, agent.id, agent.description)
    ]
    return ApiResponse(data=paginate_slice(agents, paging))


@router.put("/security/roles/{role_id}/access", response_model=ApiResponse[RoleData])
async def update_role_access(
    role_id: str,
    payload: RoleAccessUpdateRequest,
    request: Request,
    response: Response,
) -> ApiResponse[RoleData]:
    """権限管理画面の保存。

    ロールの Agent 権限と対象範囲（エージェント）だけを置き換える。
    """
    actor = current_principal(request)
    request_id, client_ip = request_context(request)
    # エージェント ID の存在確認は利用者の範囲と無関係に行う（範囲外は 403、存在しない ID は 400）。
    agents = await run_in_threadpool(runtime_repository.list_agents)
    known_agent_ids = {agent.id for agent in agents}
    # 個人の業務 Agent は、共有の申請を承認して組織の業務 Agent にしてから割り当てる（#2523）。
    personal = sorted(
        agent.name
        for agent in agents
        if agent.owner_user_uuid is not None and agent.id in set(payload.agent_ids)
    )
    if personal:
        raise HTTPException(
            status_code=409,
            detail=(
                f"個人の業務 Agent（{'、'.join(personal)}）はロールに割り当てられません。"
                "共有の申請を承認してから割り当ててください。"
            ),
        )
    role = await run_in_threadpool(
        lambda: get_security_service().update_role_access(
            role_id,
            expected_version=payload.version,
            permissions=payload.permissions,
            agent_ids=payload.agent_ids,
            known_agent_ids=known_agent_ids,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
    )
    response.headers["ETag"] = f'"{role.version}"'
    return ApiResponse(data=RoleData.from_record(role))
