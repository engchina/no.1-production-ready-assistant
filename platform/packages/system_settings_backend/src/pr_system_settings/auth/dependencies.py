"""共通認証の fail-closed 認可（3製品共通。NL2SQL の実装を基準に移設。#212）。

製品は FastAPI の dependency から `authorize_request` を呼び、
権限の manifest（`permission_for_route`）・Cookie 名・local debug の扱いを渡す。
認可に通った利用者は `request.state.principal` に入る。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Protocol

from fastapi import HTTPException, Request

from .api_keys import ApiKeyRecord, bearer_api_key
from .domain import Principal
from .errors import (
    PASSWORD_CHANGE_REQUIRED_CODE,
    ROUTE_FORBIDDEN_CODE,
    ROUTE_UNCLASSIFIED_CODE,
    SecurityApiError,
)
from .service import AuthService
from .service_token import profile_scope_from_claims

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
UNCLASSIFIED_PERMISSION = "__unclassified__"
# API キーを MCP 以外の API に付けた（#1805）。画面用の Cookie / CSRF の API は機械から呼ばせない。
API_KEY_NOT_ALLOWED_CODE = "SECURITY_API_KEY_NOT_ALLOWED"
# path の変換指定（`{object_name:path}` の `:path`）。権限の manifest と OpenAPI は変換指定のない
# `{object_name}` で書くので、照合の前に除く（#998）。
_PATH_CONVERTER_RE = re.compile(r"\{([^{}:]+):[^{}]+\}")


class AuthRequestSettings(Protocol):
    @property
    def local_debug_enabled(self) -> bool: ...
    @property
    def app_auth_enabled(self) -> bool: ...
    @property
    def app_auth_session_cookie_name(self) -> str: ...
    @property
    def app_auth_csrf_cookie_name(self) -> str: ...


RunSync = Callable[..., Awaitable[Any]]


def permission_route_path(request: Request, *, api_prefix: str = "/api") -> str:
    """多重 include の prefix を含む、ルーターが照合済みの template を使用する。

    path の変換指定（`{name:path}` など）は除き、OpenAPI・権限の manifest と同じ `{name}` にする。
    """
    # FastAPI の遅延 include では scope['route'] は元の APIRoute のまま。
    # OpenAPI と同じ完全な path は effective route context に保存される。
    fastapi_scope = request.scope.get("fastapi")
    effective = (
        fastapi_scope.get("effective_route_context") if isinstance(fastapi_scope, dict) else None
    )
    path = getattr(effective, "path", None)
    if not isinstance(path, str):
        route = request.scope.get("route")
        path = str(getattr(route, "path", request.url.path))
    return _PATH_CONVERTER_RE.sub(r"{\1}", path).removeprefix(api_prefix)


@asynccontextmanager
async def authorize_request(
    request: Request,
    *,
    settings: AuthRequestSettings,
    service: AuthService,
    run_sync: RunSync,
    permission_for_route: Callable[[str, str], frozenset[str] | None],
    public_paths: Collection[str],
    authenticated_without_permission: Collection[str],
    local_debug_principal: Callable[[], Principal],
    enter_actor: Callable[[Principal], Any],
    exit_actor: Callable[[Any], None],
    unclassified_permission: str = UNCLASSIFIED_PERMISSION,
    service_token_paths: Collection[str] = (),
    service_token_audience: str = "",
    open_operations: Collection[tuple[str, str]] | None = None,
    api_key_paths: Collection[str] = (),
) -> AsyncIterator[None]:
    """1 リクエストの認可。`async with` の中で route を実行する。

    - local debug: 全権限のローカル利用者として通す（DB session を作らない）。
    - 認証無効: そのまま通す。
    - 公開 path: そのまま通す。
    - それ以外: session Cookie を検証し、更新系は CSRF を照合し、強制パスワード変更中は
      `authenticated_without_permission` 以外を拒否し、manifest の権限を確認する（登録外は拒否）。
    - `service_token_paths`（例: MCP の `/mcp`）: Cookie の代わりに
      `Authorization: Bearer <サービストークン>` の `sub` の利用者として認証する（#230）。
      Cookie を使わないので CSRF は照合しない。claims は `request.state.service_token_claims`。
    - `api_key_paths`（外部に MCP を公開する製品の `/mcp`。#1805・#1874）を渡すと、
      `Authorization: Bearer prak_…`（API キー）を local を含めて最初に検証し、キーの実行する
      利用者として認証する。`api_key_paths` 以外の API にキーを付けたら 401。渡さないと
      キーを扱わない（Cookie の通常の認証になる）。
    - `open_operations`（method × path）を渡すと、公開 path でも集合にある操作だけ認証を飛ばす。
      同じ path に method を足しても黙って公開されない（集合にない操作は通常の認証と権限の判定）。
      渡さないときは path 単位（従来どおり。#490）。
    """
    api_key = bearer_api_key(request.headers.get("authorization", "")) if api_key_paths else None
    if api_key is not None and not _is_open_public_operation(
        request, public_paths=public_paths, open_operations=open_operations
    ):
        # API キー（#1805）は local でも検証する（local の素通りにしない）。MCP の入口だけで使える。
        principal = await _authenticate_api_key(
            request,
            api_key,
            settings=settings,
            service=service,
            run_sync=run_sync,
            permission_for_route=permission_for_route,
            local_debug_principal=local_debug_principal,
            unclassified_permission=unclassified_permission,
            api_key_paths=api_key_paths,
        )
        request.state.principal = principal
        token = enter_actor(principal)
        try:
            yield
        finally:
            exit_actor(token)
        return
    if settings.local_debug_enabled:
        principal = local_debug_principal()
        request.state.principal = principal
        token = enter_actor(principal)
        try:
            yield
        finally:
            exit_actor(token)
        return
    if not settings.app_auth_enabled:
        yield
        return
    if _is_open_public_operation(
        request, public_paths=public_paths, open_operations=open_operations
    ):
        yield
        return
    route_path = permission_route_path(request)
    try:
        if route_path in service_token_paths:
            principal = await _authenticate_service_token(
                request, service=service, run_sync=run_sync, audience=service_token_audience
            )
        else:
            session_token = request.cookies.get(settings.app_auth_session_cookie_name, "")
            principal = await run_sync(service.authenticate_session, session_token)
        request.state.principal = principal
        if route_path not in service_token_paths and request.method.upper() not in SAFE_METHODS:
            cookie_csrf = request.cookies.get(settings.app_auth_csrf_cookie_name, "")
            header_csrf = request.headers.get("X-CSRF-Token", "")
            await run_sync(service.verify_csrf, principal, cookie_csrf, header_csrf)
        if principal.force_password_change and route_path not in authenticated_without_permission:
            raise SecurityApiError(
                403, "初回パスワード変更を完了してください。", code=PASSWORD_CHANGE_REQUIRED_CODE
            )
        permissions = permission_for_route(request.method, route_path)
        if permissions and unclassified_permission in permissions:
            raise SecurityApiError(
                403, "この API は権限一覧に登録されていません。", code=ROUTE_UNCLASSIFIED_CODE
            )
        if permissions is not None and not principal.has_any_permission(set(permissions)):
            raise SecurityApiError(
                403, "この機能を利用する権限がありません。", code=ROUTE_FORBIDDEN_CODE
            )
    except SecurityApiError:
        # error_code を落とさないよう、製品の SecurityApiError の handler で返す（#224）。
        raise
    token = enter_actor(principal)
    try:
        yield
    finally:
        exit_actor(token)


def _is_open_public_operation(
    request: Request,
    *,
    public_paths: Collection[str],
    open_operations: Collection[tuple[str, str]] | None,
) -> bool:
    route_path = permission_route_path(request)
    return route_path in public_paths and (
        open_operations is None or (request.method.upper(), route_path) in open_operations
    )


async def _authenticate_api_key(
    request: Request,
    api_key: str,
    *,
    settings: AuthRequestSettings,
    service: AuthService,
    run_sync: RunSync,
    permission_for_route: Callable[[str, str], frozenset[str] | None],
    local_debug_principal: Callable[[], Principal],
    unclassified_permission: str,
    api_key_paths: Collection[str],
) -> Principal:
    """`Authorization: Bearer prak_…` の利用者（#1805）。

    MCP の入口（`api_key_paths`。`POST /api/mcp`）だけで使える。それ以外の API に付けたら
    401。Cookie を使わないので CSRF は照合しない。ツールごとの権限は MCP のツールが利用者の
    権限で判定する（サービストークンと同じ）。
    """
    route_path = permission_route_path(request)
    if route_path not in api_key_paths:
        raise SecurityApiError(
            401,
            "API キーは MCP の入口（POST /api/mcp）でだけ使えます。"
            "画面の API はログインして使ってください。",
            code=API_KEY_NOT_ALLOWED_CODE,
        )
    authenticated: tuple[Principal, ApiKeyRecord] = await run_sync(
        partial(
            service.authenticate_api_key,
            api_key,
            local_principal=local_debug_principal,
            local_enabled=settings.local_debug_enabled,
        )
    )
    principal, record = authenticated
    request.state.api_key_id = record.key_id
    # キーの名前・製品ごとの対象の範囲（#1874）。呼び先の MCP のツールが範囲で絞り、
    # 監査にキーの名前を残す。
    request.state.api_key = record
    permissions = permission_for_route(request.method, route_path)
    if permissions and unclassified_permission in permissions:
        raise SecurityApiError(
            403, "この API は権限一覧に登録されていません。", code=ROUTE_UNCLASSIFIED_CODE
        )
    if permissions is not None and not principal.has_any_permission(set(permissions)):
        raise SecurityApiError(
            403, "この機能を利用する権限がありません。", code=ROUTE_FORBIDDEN_CODE
        )
    return principal


async def _authenticate_service_token(
    request: Request, *, service: AuthService, run_sync: RunSync, audience: str
) -> Principal:
    if not audience:
        raise ValueError("service_token_audience を指定してください。")
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise SecurityApiError(401, "サービストークンを Authorization header で指定してください。")
    authenticated: tuple[Principal, dict[str, Any]] = await run_sync(
        partial(service.authenticate_service_token, token.strip(), audience=audience)
    )
    request.state.service_token_claims = authenticated[1]
    return authenticated[0]


def current_principal[P: Principal](request: Request, principal_class: type[P]) -> P:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, principal_class):
        raise HTTPException(status_code=401, detail="ログインしてください。")
    return principal


def request_context(request: Request) -> tuple[str, str]:
    request_id = request.headers.get("X-Request-ID", "")[:128]
    client_ip = request.client.host[:128] if request.client else ""
    return request_id, client_ip


def service_token_profile_scope(request: Request) -> frozenset[str] | None:
    """サービストークンの呼び出しで使ってよいプロファイル（#1379）。claim が無ければ None。

    Cookie の画面の呼び出しと local（token を見ない）は None（範囲なし）。
    """
    claims = getattr(request.state, "service_token_claims", None)
    return profile_scope_from_claims(claims if isinstance(claims, dict) else None)
