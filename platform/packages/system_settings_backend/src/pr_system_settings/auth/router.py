"""共通認証の API（3製品共通。NL2SQL の実装を基準に移設。#212）。

`/auth/login|me|logout|password/change` と、ユーザー管理・ロール管理（基本情報）の
`/security/users*` / `/security/roles*`、外部の MCP クライアント向けの API キーの
`/account/api-keys*`（自分のキー）と `/security/api-keys*`（管理）を作る（3 製品。
#1805・#1874）。ロールに付ける権限の API は製品が持つ。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Header, Query, Request, Response
from pr_backend_core import ApiResponse
from pr_backend_core.timezones import DEFAULT_TIMEZONE, configured_timezone_name
from pydantic import BaseModel, Field

from ..users_roles import (
    AssignedRoleData,
    PasswordResetData,
    PasswordResetRequest,
    RoleCreateRequest,
    RoleDeleteData,
    RoleUpdateRequest,
    UserCreateData,
    UserCreateRequest,
    UserData,
    UserDeleteData,
    UserUpdateRequest,
    VersionRequest,
)
from .api_keys import (
    ApiKeyCreated,
    ApiKeyCreateRequest,
    ApiKeyRecord,
    ApiKeysListData,
    MyApiKeysListData,
    api_key_view,
    normalize_public_base_url,
)
from .domain import LOCAL_DEBUG_USER_UUID, Principal, RoleRecord, UserRecord
from .errors import SecurityApiError
from .service import AuthService


class LoginRequest(BaseModel):
    login_user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class DisplayTimezoneData(BaseModel):
    """`/auth/me` とログインの応答に足す、システムの時間帯（#3265）。

    製品の `current_user_model` はこれを継承する。値は共通の設定 `PLATFORM_TIMEZONE`
    （`pr_backend_core.timezones`）で、router が応答のたびに入れる。画面は日時をこの時間帯で
    表示する（ブラウザの時間帯に従わない）。認証の確認の応答に載せるため、画面は日時を出す前に
    追加の要求なしで時間帯を知る。
    """

    timezone: str = DEFAULT_TIMEZONE


class AuthCookieSettings(Protocol):
    @property
    def local_debug_enabled(self) -> bool: ...
    @property
    def app_auth_cookie_secure(self) -> bool: ...
    @property
    def app_auth_absolute_timeout_hours(self) -> int: ...
    @property
    def app_auth_session_cookie_name(self) -> str: ...
    @property
    def app_auth_csrf_cookie_name(self) -> str: ...


def assigned_role_data(role: RoleRecord) -> AssignedRoleData:
    return AssignedRoleData(
        role_id=role.role_id,
        role_code=role.role_code,
        display_name=role.display_name,
        is_built_in=role.is_built_in,
        archived=role.archived,
    )


def user_data(
    user: UserRecord,
    *,
    roles_by_id: Mapping[str, RoleRecord] | None = None,
) -> UserData:
    role_lookup = roles_by_id or {}
    return UserData(
        user_uuid=user.user_uuid,
        login_user_id=user.login_user_id,
        display_name=user.display_name,
        status=user.status,
        force_password_change=user.force_password_change,
        locked_until=user.locked_until,
        version=user.version,
        role_ids=user.role_ids,
        assigned_roles=[
            (
                assigned_role_data(role_lookup[role_id])
                if role_id in role_lookup
                else AssignedRoleData.unresolved(role_id)
            )
            for role_id in user.role_ids
        ],
        is_bootstrap_admin=user.is_bootstrap_admin,
    )


def parse_if_match_version(if_match: str | None) -> int:
    """削除 API の `If-Match: "<version>"` を数値の版にする。"""
    if if_match is None or not if_match.strip():
        raise SecurityApiError(
            428,
            "削除には If-Match header で現在のバージョンを指定してください。"
            "表示を更新して再試行してください。",
            code="SECURITY_VERSION_REQUIRED",
        )
    normalized = if_match.strip()
    if normalized.startswith("W/"):
        raise SecurityApiError(
            400,
            "If-Match header には weak ETag ではなく現在の数値バージョンを指定してください。",
            code="SECURITY_VERSION_INVALID",
        )
    if normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    if not normalized.isdecimal() or int(normalized) < 1:
        raise SecurityApiError(
            400,
            "If-Match header には現在の数値バージョンを指定してください。",
            code="SECURITY_VERSION_INVALID",
        )
    return int(normalized)


def build_auth_router(
    *,
    get_service: Callable[[], AuthService],
    get_settings: Callable[[], AuthCookieSettings],
    current_principal: Callable[[Request], Principal],
    request_context: Callable[[Request], tuple[str, str]],
    local_debug_principal: Callable[[], Principal],
    current_user_model: type[BaseModel],
    current_user_data: Callable[[Principal, bool], BaseModel],
    role_model: type[BaseModel],
    role_data: Callable[[RoleRecord], BaseModel],
    tags: list[str | Enum] | None = None,
    api_keys: bool = False,
    public_base_url: Callable[[], str] | None = None,
) -> APIRouter:
    """共通認証の API を作る。

    - `current_user_data(principal, debug_mode)`: `/auth/me` などの応答（製品が項目を足せる）。
      `current_user_model` は `DisplayTimezoneData` を継承し、`timezone` は router が入れる。
    - `role_data(role)`: ロールの応答（製品が権限などを足した RoleData）。
    - `api_keys`: 外部の MCP クライアント向けの API キーの API（`/account/api-keys*`・
      `/security/api-keys*`）を足す
      （外部に MCP を公開する 3 製品。#1805・#1874）。
    - `public_base_url()`: 自分のキーの一覧が返す公開の基底の URL
      （`PLATFORM_PUBLIC_BASE_URL`。#1870）。
    """
    if not issubclass(current_user_model, DisplayTimezoneData):
        raise TypeError("current_user_model は DisplayTimezoneData を継承してください。")
    router = APIRouter(tags=tags or ["security"])
    current_user_response: Any = ApiResponse[current_user_model]  # type: ignore[valid-type]
    role_response: Any = ApiResponse[role_model]  # type: ignore[valid-type]
    role_list_response: Any = ApiResponse[list[role_model]]  # type: ignore[valid-type]

    def set_auth_cookie(response: Response, *, name: str, value: str, httponly: bool) -> None:
        settings = get_settings()
        response.set_cookie(
            name,
            value,
            httponly=httponly,
            secure=settings.app_auth_cookie_secure,
            samesite="lax",
            path="/",
            max_age=settings.app_auth_absolute_timeout_hours * 3600,
        )

    def clear_auth_cookies(response: Response) -> None:
        settings = get_settings()
        response.delete_cookie(settings.app_auth_session_cookie_name, path="/")
        response.delete_cookie(settings.app_auth_csrf_cookie_name, path="/")

    def user_payload(principal: Principal, debug_mode: bool) -> BaseModel:
        # システムの時間帯は利用者ではなく設定から入れる（製品の current_user_data は扱わない）。
        return current_user_data(principal, debug_mode).model_copy(
            update={"timezone": configured_timezone_name(get_settings())}
        )

    def roles_by_id() -> dict[str, RoleRecord]:
        return {role.role_id: role for role in get_service().list_roles(include_archived=True)}

    def to_user_data(user: UserRecord) -> UserData:
        return user_data(user, roles_by_id=roles_by_id())

    @router.post("/auth/login", response_model=current_user_response)
    def login(payload: LoginRequest, request: Request, response: Response) -> Any:
        if get_settings().local_debug_enabled:
            return ApiResponse(data=user_payload(local_debug_principal(), True))
        request_id, client_ip = request_context(request)
        service = get_service()
        principal, session_token, csrf_token = service.login(
            payload.login_user_id, payload.password, request_id=request_id, client_ip=client_ip
        )
        settings = get_settings()
        # Cookie は 3 製品で共有する。ブラウザが持っていた古いセッションは失効させる（#1565）。
        previous_token = request.cookies.get(settings.app_auth_session_cookie_name, "")
        if previous_token and previous_token != session_token:
            service.revoke_session_token(previous_token)
        set_auth_cookie(
            response, name=settings.app_auth_session_cookie_name, value=session_token, httponly=True
        )
        set_auth_cookie(
            response, name=settings.app_auth_csrf_cookie_name, value=csrf_token, httponly=False
        )
        return ApiResponse(data=user_payload(principal, False))

    @router.get("/auth/me", response_model=current_user_response)
    def me(request: Request) -> Any:
        return ApiResponse(
            data=user_payload(current_principal(request), get_settings().local_debug_enabled)
        )

    @router.post("/auth/logout", response_model=ApiResponse[dict[str, bool]])
    def logout(request: Request, response: Response) -> ApiResponse[dict[str, bool]]:
        if get_settings().local_debug_enabled:
            return ApiResponse(data={"logged_out": False})
        principal = current_principal(request)
        request_id, client_ip = request_context(request)
        get_service().logout(principal, request_id=request_id, client_ip=client_ip)
        clear_auth_cookies(response)
        return ApiResponse(data={"logged_out": True})

    @router.post("/auth/password/change", response_model=ApiResponse[dict[str, bool]])
    def change_password(
        payload: PasswordChangeRequest, request: Request, response: Response
    ) -> ApiResponse[dict[str, bool]]:
        if get_settings().local_debug_enabled:
            raise SecurityApiError(409, "ローカル DEBUG モードではパスワードを変更できません。")
        principal = current_principal(request)
        request_id, client_ip = request_context(request)
        get_service().change_password(
            principal,
            payload.current_password,
            payload.new_password,
            request_id=request_id,
            client_ip=client_ip,
        )
        clear_auth_cookies(response)
        return ApiResponse(data={"changed": True})

    @router.get("/security/users", response_model=ApiResponse[list[UserData]])
    def list_users() -> ApiResponse[list[UserData]]:
        users = get_service().list_users()
        lookup = roles_by_id()
        return ApiResponse(data=[user_data(user, roles_by_id=lookup) for user in users])

    @router.post("/security/users", response_model=ApiResponse[UserCreateData])
    def create_user(payload: UserCreateRequest, request: Request) -> ApiResponse[UserCreateData]:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        user, password = get_service().create_user(
            login_user_id=payload.login_user_id,
            display_name=payload.display_name,
            role_ids=payload.role_ids,
            temporary_password=payload.temporary_password,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(
            data=UserCreateData(user=to_user_data(user), temporary_password=password)
        )

    @router.get("/security/users/{user_uuid}", response_model=ApiResponse[UserData])
    def get_user(user_uuid: str) -> ApiResponse[UserData]:
        user = get_service().store.get_user(user_uuid)
        if user is None:
            raise SecurityApiError(404, "ユーザーが見つかりません。")
        return ApiResponse(data=to_user_data(user))

    @router.patch("/security/users/{user_uuid}", response_model=ApiResponse[UserData])
    def update_user(
        user_uuid: str, payload: UserUpdateRequest, request: Request, response: Response
    ) -> ApiResponse[UserData]:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        user = get_service().update_user(
            user_uuid,
            expected_version=payload.version,
            display_name=payload.display_name,
            status=payload.status,
            role_ids=payload.role_ids,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        response.headers["ETag"] = f'"{user.version}"'
        return ApiResponse(data=to_user_data(user))

    @router.delete("/security/users/{user_uuid}", response_model=ApiResponse[UserDeleteData])
    def delete_user(
        user_uuid: str,
        request: Request,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> ApiResponse[UserDeleteData]:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        deleted = get_service().delete_user(
            user_uuid,
            expected_version=parse_if_match_version(if_match),
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(
            data=UserDeleteData(user_uuid=deleted.user_uuid, login_user_id=deleted.login_user_id)
        )

    @router.post(
        "/security/users/{user_uuid}/reset-password", response_model=ApiResponse[PasswordResetData]
    )
    def reset_password(
        user_uuid: str, payload: PasswordResetRequest, request: Request
    ) -> ApiResponse[PasswordResetData]:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        user, password = get_service().reset_password(
            user_uuid,
            payload.temporary_password,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(
            data=PasswordResetData(user=to_user_data(user), temporary_password=password)
        )

    @router.post("/security/users/{user_uuid}/unlock", response_model=ApiResponse[UserData])
    def unlock_user(user_uuid: str, request: Request) -> ApiResponse[UserData]:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        user = get_service().unlock_user(
            user_uuid, actor=actor, request_id=request_id, client_ip=client_ip
        )
        return ApiResponse(data=to_user_data(user))

    def change_user_status(
        user_uuid: str, payload: VersionRequest, request: Request, status: str
    ) -> ApiResponse[UserData]:
        service = get_service()
        current = service.store.get_user(user_uuid)
        if current is None:
            raise SecurityApiError(404, "ユーザーが見つかりません。")
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        updated = service.update_user(
            user_uuid,
            expected_version=payload.version,
            display_name=current.display_name,
            status=status,
            role_ids=current.role_ids,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(data=to_user_data(updated))

    @router.post("/security/users/{user_uuid}/enable", response_model=ApiResponse[UserData])
    def enable_user(
        user_uuid: str, payload: VersionRequest, request: Request
    ) -> ApiResponse[UserData]:
        return change_user_status(user_uuid, payload, request, "ACTIVE")

    @router.post("/security/users/{user_uuid}/disable", response_model=ApiResponse[UserData])
    def disable_user(
        user_uuid: str, payload: VersionRequest, request: Request
    ) -> ApiResponse[UserData]:
        return change_user_status(user_uuid, payload, request, "DISABLED")

    @router.get("/security/roles", response_model=role_list_response)
    def list_roles(request: Request, include_archived: bool = Query(default=False)) -> Any:
        service = get_service()
        principal = getattr(request.state, "principal", None)
        roles: Sequence[RoleRecord]
        if isinstance(principal, Principal):
            roles = service.list_roles_for_actor(principal, include_archived=include_archived)
        else:
            roles = service.list_roles(include_archived=include_archived)
        return ApiResponse(data=[role_data(role) for role in roles])

    @router.post("/security/roles", response_model=role_response)
    def create_role(payload: RoleCreateRequest, request: Request) -> Any:
        """ロール管理画面の新規作成。権限は製品の権限管理 API で付ける（#206）。"""
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        role = get_service().create_role(
            role_code=payload.role_code,
            display_name=payload.display_name,
            description=payload.description,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(data=role_data(role))

    @router.get("/security/roles/{role_id}", response_model=role_response)
    def get_role(role_id: str, request: Request) -> Any:
        service = get_service()
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, Principal):
            role = service.get_role_for_actor(role_id, principal)
        else:
            role = service.store.get_role(role_id)
        if role is None:
            raise SecurityApiError(404, "ロールが見つかりません。")
        return ApiResponse(data=role_data(role))

    @router.patch("/security/roles/{role_id}", response_model=role_response)
    def update_role(
        role_id: str, payload: RoleUpdateRequest, request: Request, response: Response
    ) -> Any:
        """ロール管理画面の基本情報（名称・説明）の更新。権限は変えない（#206）。"""
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        role = get_service().update_role(
            role_id,
            expected_version=payload.version,
            display_name=payload.display_name,
            description=payload.description,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        response.headers["ETag"] = f'"{role.version}"'
        return ApiResponse(data=role_data(role))

    @router.post("/security/roles/{role_id}/archive", response_model=role_response)
    def archive_role(role_id: str, payload: VersionRequest, request: Request) -> Any:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        role = get_service().archive_role(
            role_id,
            expected_version=payload.version,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(data=role_data(role))

    @router.post("/security/roles/{role_id}/restore", response_model=role_response)
    def restore_role(role_id: str, payload: VersionRequest, request: Request) -> Any:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        role = get_service().restore_role(
            role_id,
            expected_version=payload.version,
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(data=role_data(role))

    @router.delete("/security/roles/{role_id}", response_model=ApiResponse[RoleDeleteData])
    def delete_role(
        role_id: str,
        request: Request,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> ApiResponse[RoleDeleteData]:
        actor = current_principal(request)
        request_id, client_ip = request_context(request)
        deleted = get_service().delete_role(
            role_id,
            expected_version=parse_if_match_version(if_match),
            actor=actor,
            request_id=request_id,
            client_ip=client_ip,
        )
        return ApiResponse(
            data=RoleDeleteData(role_id=deleted.role_id, role_code=deleted.role_code)
        )

    if api_keys:
        _add_api_key_routes(
            router,
            get_service=get_service,
            current_principal=current_principal,
            public_base_url=public_base_url or (lambda: ""),
        )
    return router


def _add_api_key_routes(
    router: APIRouter,
    *,
    get_service: Callable[[], AuthService],
    current_principal: Callable[[Request], Principal],
    public_base_url: Callable[[], str],
) -> None:
    """API キー（外部の MCP クライアント向け。3 製品共通。#1805）の API。

    - `/account/api-keys*`: 自分のキーの一覧・発行・失効（セルフサービス。ログインだけで使え、
      メニュー権限を要しない。自分のキーだけを返す・消す）。
    - `/security/api-keys*`: 管理の画面の全員のキーの一覧・失効（製品の manifest でメニュー権限
      `menu.security_api_keys` を要求する）。発行は持たない（キーは各利用者が自分で発行する）。
    """

    def owner_names(service: AuthService, records: Sequence[ApiKeyRecord]) -> dict[str, str]:
        identities = service.display_identities(
            sorted({record.owner_user_uuid for record in records})
        )
        names = {uuid: identity.display_name for uuid, identity in identities.items()}
        names.setdefault(LOCAL_DEBUG_USER_UUID, "ローカル利用者")
        return names

    def list_data(service: AuthService, records: Sequence[ApiKeyRecord]) -> ApiKeysListData:
        names = owner_names(service, records)
        return ApiKeysListData(keys=[api_key_view(record, names) for record in records])

    @router.get("/account/api-keys", response_model=ApiResponse[MyApiKeysListData])
    def list_my_api_keys(request: Request) -> ApiResponse[MyApiKeysListData]:
        """自分の API キー（秘密と hash は返さない）と、画面の接続先に出す公開の基底の URL。"""
        service = get_service()
        records = service.list_api_keys(current_principal(request))
        return ApiResponse(
            data=MyApiKeysListData(
                keys=list_data(service, records).keys,
                public_base_url=normalize_public_base_url(public_base_url()),
            )
        )

    @router.post("/account/api-keys", response_model=ApiResponse[ApiKeyCreated])
    def create_my_api_key(
        payload: ApiKeyCreateRequest, request: Request
    ) -> ApiResponse[ApiKeyCreated]:
        """自分の API キーを発行する。秘密（`token`）はこの応答で 1 回だけ返す。"""
        service = get_service()
        record, token = service.create_api_key(payload, actor=current_principal(request))
        view = api_key_view(record, owner_names(service, [record]))
        return ApiResponse(data=ApiKeyCreated(key=view, token=token))

    @router.delete("/account/api-keys/{key_id}", response_model=ApiResponse[dict[str, str]])
    def revoke_my_api_key(key_id: str, request: Request) -> ApiResponse[dict[str, str]]:
        """自分の API キーを失効させる（削除。すぐに使えなくなる）。"""
        record = get_service().revoke_api_key(key_id, actor=current_principal(request))
        return ApiResponse(data={"id": record.key_id})

    @router.get("/security/api-keys", response_model=ApiResponse[ApiKeysListData])
    def list_all_api_keys(request: Request) -> ApiResponse[ApiKeysListData]:
        """全員の API キー（管理の画面。秘密と hash は返さない）。"""
        service = get_service()
        records = service.list_api_keys(current_principal(request), all_users=True)
        return ApiResponse(data=list_data(service, records))

    @router.delete("/security/api-keys/{key_id}", response_model=ApiResponse[dict[str, str]])
    def revoke_any_api_key(key_id: str, request: Request) -> ApiResponse[dict[str, str]]:
        """利用者の API キーを失効させる（管理の画面）。"""
        record = get_service().revoke_api_key(
            key_id, actor=current_principal(request), any_user=True
        )
        return ApiResponse(data={"id": record.key_id})
