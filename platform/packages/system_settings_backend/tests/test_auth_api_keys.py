"""外部の MCP クライアント向けの API キー（3 製品共通。#1805・#1874）。"""

from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_auth import _admin, _grant, _ProductService, _service

from pr_system_settings.auth import api_keys as api_keys_module
from pr_system_settings.auth.api_keys import (
    API_KEY_MAX_PER_USER,
    API_KEYS_TABLE,
    ApiKeyCreateRequest,
    ApiKeyRateLimiter,
    OracleApiKeyStore,
    api_key_view,
    bearer_api_key,
    dump_resource_scopes,
    hash_api_key,
    load_resource_scopes,
    normalize_public_base_url,
)
from pr_system_settings.auth.dependencies import API_KEY_NOT_ALLOWED_CODE, authorize_request
from pr_system_settings.auth.domain import LOCAL_DEBUG_USER_UUID, Principal
from pr_system_settings.auth.errors import SecurityApiError, SecurityMigrationRequired
from pr_system_settings.auth.migrations import PLATFORM_AUTH_DDL
from pr_system_settings.auth.router import DisplayTimezoneData, build_auth_router
from pr_system_settings.auth.service import (
    API_KEY_INVALID_CODE,
    API_KEY_PRODUCT_FORBIDDEN_CODE,
    API_KEY_RATE_LIMITED_CODE,
)
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES, InMemoryAuthStore

USER_PASSWORD = "Kq7#Lomber!Vt29"  # nosec B105 - テスト用
TOO_MANY_AGENTS = [f"agent-{index}" for index in range(51)]


def _user(service: _ProductService, store: InMemoryAuthStore, login: str, *codes: str) -> Principal:
    """`codes` の RAG 権限を持つロールの、初回パスワード変更が済んだ利用者。"""
    admin = _admin(service)
    role = service.create_role(
        role_code=f"R_{login.upper()}", display_name=login, description="", actor=admin
    )
    _grant(store, "rag", role.role_id, *codes)
    user, _ = service.create_user(
        login_user_id=login,
        display_name=f"{login} さん",
        role_ids=[role.role_id],
        temporary_password=USER_PASSWORD,
        actor=admin,
    )
    store.set_password(user.user_uuid, service._hash_password(USER_PASSWORD), force_change=False)
    principal, _, _ = service.login(login, USER_PASSWORD)
    return principal


def _create(service: _ProductService, actor: Principal, **overrides: Any) -> tuple[Any, str]:
    payload = {"name": "Claude Code", "products": ["rag"], **overrides}
    return service.create_api_key(ApiKeyCreateRequest.model_validate(payload), actor=actor)


def _authenticate(service: _ProductService, token: str, *, local: bool = False) -> Principal:
    principal, _ = service.authenticate_api_key(
        token, local_principal=_local_principal, local_enabled=local
    )
    return principal


def _local_principal() -> Principal:
    return Principal(
        user_uuid=LOCAL_DEBUG_USER_UUID,
        login_user_id="local",
        display_name="ローカル利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["SYSTEM_ADMIN"],
        permissions=set(),
        session_id="local-debug",
        csrf_token_hash="",  # nosec B106 - テスト用
    )


# ---- 発行・一覧・失効 ----------------------------------------------------------------


def test_create_returns_secret_once_and_stores_only_hash() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    record, token = _create(service, user, products=["agent", "nl2sql", "rag"])
    assert re.fullmatch(r"prak_[0-9a-f]{16}_[A-Za-z0-9_-]{43}", token)
    assert token.startswith(record.token_prefix)
    assert record.token_hash == hash_api_key(token)
    # 並びは製品の順（rag / nl2sql / agent。#1874）。
    assert record.products == ["rag", "nl2sql", "agent"]
    assert record.owner_user_uuid == user.user_uuid
    stored = service.api_key_store.get(record.key_id)
    assert stored is not None
    assert token not in repr(stored)
    # 既定の期限は 90 日、null は無期限。
    assert record.expires_at is not None
    assert timedelta(days=89) < record.expires_at - record.created_at <= timedelta(days=90)
    unlimited, _ = _create(service, user, expires_in_days=None)
    assert unlimited.expires_at is None


@pytest.mark.parametrize(
    "payload",
    [
        {"name": " ", "products": ["rag"]},
        {"name": "x", "products": []},
        {"name": "x", "products": ["sql"]},
        {"name": "x", "products": ["rag"], "expires_in_days": 7},
        {"name": "x" * 101, "products": ["rag"]},
        # 対象の範囲（#1874）: 範囲を持てない製品・選んでいない製品・空・形の違う ID・多すぎは断る。
        {"name": "x", "products": ["rag"], "resource_scopes": {"rag": ["p1"]}},
        {"name": "x", "products": ["rag"], "resource_scopes": {"agent": ["a1"]}},
        {"name": "x", "products": ["agent"], "resource_scopes": {"agent": []}},
        {"name": "x", "products": ["agent"], "resource_scopes": {"agent": ["a/b"]}},
        {"name": "x", "products": ["agent"], "resource_scopes": {"agent": TOO_MANY_AGENTS}},
    ],
    ids=[
        "blank-name",
        "no-product",
        "unknown-product",
        "bad-expiry",
        "long-name",
        "scope-unsupported-product",
        "scope-unselected-product",
        "scope-empty",
        "scope-bad-id",
        "scope-too-many",
    ],
)
def test_create_request_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ApiKeyCreateRequest.model_validate(payload)


def test_create_keeps_resource_scopes_and_returns_them_in_the_view() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    record, _ = _create(
        service,
        user,
        products=["rag", "agent"],
        resource_scopes={"agent": [" sales-agent", "hr-agent", "sales-agent"]},
    )
    # 空白を除き、重複を 1 つにする。範囲の無い製品（rag）は利用者が使えるすべて。
    assert record.resource_scopes == {"agent": ["sales-agent", "hr-agent"]}
    assert record.scope("agent") == frozenset({"sales-agent", "hr-agent"})
    assert record.scope("rag") is None
    stored = service.api_key_store.get(record.key_id)
    assert stored is not None and stored.resource_scopes == record.resource_scopes
    assert api_key_view(record).resource_scopes == {"agent": ["sales-agent", "hr-agent"]}
    unscoped, _ = _create(service, user, products=["agent"])
    assert unscoped.scope("agent") is None


def test_resource_scopes_round_trip_and_broken_values_allow_nothing() -> None:
    assert dump_resource_scopes({}) is None
    text = dump_resource_scopes({"agent": ["a1", "a2"]})
    assert load_resource_scopes(text) == {"agent": ["a1", "a2"]}
    assert load_resource_scopes(SimpleNamespace(read=lambda: text)) == {"agent": ["a1", "a2"]}
    assert load_resource_scopes(None) == {}
    # 壊れた値を「範囲なし（すべて）」として通さない。
    assert load_resource_scopes("{broken") == {"rag": [], "nl2sql": [], "agent": []}


def test_self_service_lists_and_revokes_only_own_keys() -> None:
    service, store = _service()
    alice = _user(service, store, "alice", "menu.search")
    bob = _user(service, store, "bob", "menu.search")
    admin = _admin(service)
    alice_key, _ = _create(service, alice, name="alice")
    bob_key, _ = _create(service, bob, name="bob")
    assert [item.key_id for item in service.list_api_keys(alice)] == [alice_key.key_id]
    # システム管理者でも、自分の画面（セルフサービス）は自分のキーだけ。
    assert service.list_api_keys(admin) == []
    # 管理の画面（all_users / any_user）は全員の分。
    assert {item.key_id for item in service.list_api_keys(admin, all_users=True)} == {
        alice_key.key_id,
        bob_key.key_id,
    }
    with pytest.raises(SecurityApiError) as denied:
        service.revoke_api_key(bob_key.key_id, actor=alice)
    assert denied.value.status_code == 404
    service.revoke_api_key(bob_key.key_id, actor=admin, any_user=True)
    service.revoke_api_key(alice_key.key_id, actor=alice)
    assert service.list_api_keys(admin, all_users=True) == []


def test_api_key_names_follow_the_viewer_visibility() -> None:
    """実行履歴の「MCP（キーの名前）」: system_admin は全員、それ以外は自分のキーだけ（#3348）。"""
    service, store = _service()
    alice = _user(service, store, "alice", "menu.search")
    bob = _user(service, store, "bob", "menu.search")
    admin = _admin(service)
    alice_key, _ = _create(service, alice, name="alice のキー")
    bob_key, _ = _create(service, bob, name="bob のキー")
    wanted = [alice_key.key_id, bob_key.key_id, "missing", ""]
    assert service.api_key_names(alice, wanted) == {alice_key.key_id: "alice のキー"}
    assert service.api_key_names(admin, wanted) == {
        alice_key.key_id: "alice のキー",
        bob_key.key_id: "bob のキー",
    }
    assert service.api_key_names(alice, []) == {}
    # 削除したキーは名前を返さない。
    service.revoke_api_key(alice_key.key_id, actor=alice)
    assert service.api_key_names(admin, wanted) == {bob_key.key_id: "bob のキー"}


def test_each_user_can_hold_a_limited_number_of_keys() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    for index in range(API_KEY_MAX_PER_USER):
        _create(service, user, name=f"key-{index}")
    with pytest.raises(SecurityApiError) as denied:
        _create(service, user, name="one-more")
    assert (denied.value.status_code, denied.value.code) == (409, "SECURITY_API_KEY_LIMIT")
    # ほかの利用者の上限とは別に数える。
    other = _user(service, store, "other", "menu.search")
    _create(service, other)


# ---- 認証 --------------------------------------------------------------------------


def test_authenticate_uses_current_role_permissions_and_records_last_used() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    record, token = _create(service, user)
    principal = _authenticate(service, token)
    assert (principal.user_uuid, principal.permissions) == (user.user_uuid, {"menu.search"})
    assert principal.session_id == f"api-key:{record.key_id}"
    stored = service.api_key_store.get(record.key_id)
    assert stored is not None and stored.last_used_at is not None
    # 権限は利用者の現在のロールから毎回計算する（ロールを変えればキーの権限も変わる）。
    _grant(store, "rag", user.role_ids[0], "menu.chat")
    assert _authenticate(service, token).permissions == {"menu.chat"}


@pytest.mark.parametrize(
    "mutate",
    ["wrong-secret", "unknown-id", "malformed", "revoked"],
    ids=["wrong-secret", "unknown-id", "malformed", "revoked"],
)
def test_authenticate_rejects_invalid_keys(mutate: str) -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    record, token = _create(service, user)
    if mutate == "wrong-secret":
        token = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    elif mutate == "unknown-id":
        token = token.replace(record.key_id, "0" * 16)
    elif mutate == "malformed":
        token = "prak_short"
    else:
        service.revoke_api_key(record.key_id, actor=user)
    with pytest.raises(SecurityApiError) as denied:
        _authenticate(service, token)
    assert (denied.value.status_code, denied.value.code) == (401, API_KEY_INVALID_CODE)


def test_authenticate_rejects_expired_key() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    record, token = _create(service, user, expires_in_days=30)
    expired = replace(record, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    service.api_key_store.create(expired)
    with pytest.raises(SecurityApiError) as denied:
        _authenticate(service, token)
    assert denied.value.status_code == 401
    assert "期限" in denied.value.public_message


def test_authenticate_rejects_key_for_other_product() -> None:
    service, store = _service()  # product_key = "rag"
    user = _user(service, store, "searcher", "menu.search")
    _, token = _create(service, user, products=["nl2sql", "agent"])
    with pytest.raises(SecurityApiError) as denied:
        _authenticate(service, token)
    assert (denied.value.status_code, denied.value.code) == (403, API_KEY_PRODUCT_FORBIDDEN_CODE)
    assert "RAG では使えません" in denied.value.public_message


def test_rate_limiter_counts_calls_in_the_last_minute() -> None:
    limiter = ApiKeyRateLimiter()
    start = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    assert limiter.acquire("k", limit=2, now=start) is None
    assert limiter.acquire("k", limit=2, now=start + timedelta(seconds=10)) is None
    # 3 回目は数えずに、最初の呼び出しが窓を出るまでの秒数を返す。
    assert limiter.acquire("k", limit=2, now=start + timedelta(seconds=20)) == 40
    assert limiter.acquire("other", limit=2, now=start + timedelta(seconds=20)) is None
    assert limiter.acquire("k", limit=2, now=start + timedelta(seconds=61)) is None
    # 0 は上限なし。
    assert all(limiter.acquire("free", limit=0, now=start) is None for _ in range(100))


def test_authenticate_rejects_calls_over_the_per_minute_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service()
    monkeypatch.setattr(service.settings, "app_api_key_calls_per_minute", 2, raising=False)
    user = _user(service, store, "searcher", "menu.search")
    _, token = _create(service, user)
    _authenticate(service, token)
    _authenticate(service, token)
    with pytest.raises(SecurityApiError) as limited:
        _authenticate(service, token)
    assert (limited.value.status_code, limited.value.code) == (429, API_KEY_RATE_LIMITED_CODE)
    assert int(limited.value.headers["Retry-After"]) >= 1
    assert limited.value.retryable


def test_authenticate_rejects_disabled_owner() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    _, token = _create(service, user)
    current = store.get_user(user.user_uuid)
    assert current is not None
    store.update_user(
        user.user_uuid,
        expected_version=current.version,
        display_name=current.display_name,
        status="DISABLED",
        role_ids=current.role_ids,
    )
    with pytest.raises(SecurityApiError) as denied:
        _authenticate(service, token)
    assert denied.value.status_code == 403


def test_local_keys_work_only_in_local_mode() -> None:
    service, _ = _service()
    record, token = _create(service, _local_principal())
    assert record.owner_user_uuid == LOCAL_DEBUG_USER_UUID
    assert _authenticate(service, token, local=True).user_uuid == LOCAL_DEBUG_USER_UUID
    with pytest.raises(SecurityApiError) as denied:
        _authenticate(service, token, local=False)
    assert denied.value.status_code == 401


def test_last_used_is_saved_at_most_every_ten_minutes() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    record, _ = _create(service, user)
    first = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    service.api_key_store.touch_last_used(record.key_id, now=first)
    service.api_key_store.touch_last_used(record.key_id, now=first + timedelta(minutes=9))
    stored = service.api_key_store.get(record.key_id)
    assert stored is not None and stored.last_used_at == first
    later = first + timedelta(minutes=10)
    service.api_key_store.touch_last_used(record.key_id, now=later)
    stored = service.api_key_store.get(record.key_id)
    assert stored is not None and stored.last_used_at == later


def test_deleting_user_deletes_owned_keys() -> None:
    service, store = _service()
    admin = _admin(service)
    user = _user(service, store, "searcher", "menu.search")
    _create(service, user)
    current = store.get_user(user.user_uuid)
    assert current is not None
    disabled = service.update_user(
        user.user_uuid,
        expected_version=current.version,
        display_name=current.display_name,
        status="DISABLED",
        role_ids=current.role_ids,
        actor=admin,
    )
    service.delete_user(user.user_uuid, expected_version=disabled.version, actor=admin)
    assert service.list_api_keys(admin, all_users=True) == []


# ---- 認可（authorize_request） -------------------------------------------------------


def _client(
    service: _ProductService, *, local: bool = False, api_key_paths: tuple[str, ...]
) -> Any:
    settings = SimpleNamespace(
        local_debug_enabled=local,
        app_auth_enabled=not local,
        app_auth_session_cookie_name="s",
        app_auth_csrf_cookie_name="c",
    )

    async def run_sync(func: Any, *args: Any) -> Any:
        return func(*args)

    async def dependency(request: Request) -> Any:
        async with authorize_request(
            request,
            settings=settings,
            service=service,
            run_sync=run_sync,
            permission_for_route=lambda method, path: None,
            public_paths=("/health",),
            authenticated_without_permission=(),
            local_debug_principal=_local_principal,
            enter_actor=lambda principal: nullcontext(),
            exit_actor=lambda token: None,
            service_token_paths=("/mcp",),
            service_token_audience="rag",
            api_key_paths=api_key_paths,
        ):
            yield

    app = FastAPI()

    @app.exception_handler(SecurityApiError)
    async def handler(request: Request, exc: SecurityApiError) -> JSONResponse:
        return JSONResponse({"error_code": exc.code}, status_code=exc.status_code)

    for path in ("/api/mcp", "/api/settings", "/api/health"):

        @app.post(path, dependencies=[Depends(dependency)])
        def probe(request: Request) -> dict[str, Any]:
            principal = getattr(request.state, "principal", None)
            return {"user_uuid": getattr(principal, "user_uuid", None)}

    return TestClient(app)


def test_authorize_request_accepts_api_key_only_on_mcp() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    _, token = _create(service, user)
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(service, api_key_paths=("/mcp",))
    # Cookie も CSRF もなしで、キーの利用者として通る。
    response = client.post("/api/mcp", headers=headers)
    assert (response.status_code, response.json()) == (200, {"user_uuid": user.user_uuid})
    # 画面用の API にキーを付けたら 401（Cookie / CSRF の API を機械から呼ばせない）。
    response = client.post("/api/settings", headers=headers)
    assert (response.status_code, response.json()["error_code"]) == (
        401,
        API_KEY_NOT_ALLOWED_CODE,
    )
    # 公開 path はキーを見ない。
    assert client.post("/api/health", headers=headers).status_code == 200
    # キーなし・不正なキーは 401。
    assert client.post("/api/mcp").status_code == 401
    bad = {"Authorization": "Bearer prak_0000000000000000_" + "A" * 43}
    response = client.post("/api/mcp", headers=bad)
    assert (response.status_code, response.json()["error_code"]) == (401, API_KEY_INVALID_CODE)


def test_authorize_request_checks_api_key_even_in_local_mode() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    _, token = _create(service, user)
    client = _client(service, local=True, api_key_paths=("/mcp",))
    # local でもキーは素通りにしない（不正なキーは 401、正しいキーはキーの利用者）。
    bad = {"Authorization": "Bearer prak_0000000000000000_" + "A" * 43}
    assert client.post("/api/mcp", headers=bad).status_code == 401
    response = client.post("/api/mcp", headers={"Authorization": f"Bearer {token}"})
    assert response.json() == {"user_uuid": user.user_uuid}
    # キーなしの local はローカル利用者。
    assert client.post("/api/mcp").json() == {"user_uuid": LOCAL_DEBUG_USER_UUID}


def test_authorize_request_ignores_api_keys_without_api_key_paths() -> None:
    """キーを扱わない製品（Agent）は、キーをサービストークンとして検証して 401 にする。"""
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    _, token = _create(service, user)
    client = _client(service, api_key_paths=())
    response = client.post("/api/mcp", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error_code"] != API_KEY_NOT_ALLOWED_CODE


def test_bearer_api_key_reads_only_prak_tokens() -> None:
    assert bearer_api_key("Bearer prak_x") == "prak_x"
    assert bearer_api_key("bearer  prak_x ") == "prak_x"
    assert bearer_api_key("Bearer eyJ.a.b") is None
    assert bearer_api_key("Basic prak_x") is None
    assert bearer_api_key("") is None


# ---- 管理 API ------------------------------------------------------------------------


def _router_paths(*, api_keys: bool) -> set[str]:
    class _Model(DisplayTimezoneData):
        pass

    router = build_auth_router(
        get_service=lambda: None,  # type: ignore[arg-type,return-value]
        get_settings=lambda: None,  # type: ignore[arg-type,return-value]
        current_principal=lambda request: None,  # type: ignore[arg-type,return-value]
        request_context=lambda request: ("", ""),
        local_debug_principal=_local_principal,
        current_user_model=_Model,
        current_user_data=lambda principal, debug: _Model(),
        role_model=_Model,
        role_data=lambda role: _Model(),
        api_keys=api_keys,
    )
    return {f"{method} {route.path}" for route in router.routes for method in route.methods}  # type: ignore[attr-defined]


def test_api_key_routes_are_added_only_when_requested() -> None:
    expected = {
        "GET /account/api-keys",
        "POST /account/api-keys",
        "DELETE /account/api-keys/{key_id}",
        "GET /security/api-keys",
        "DELETE /security/api-keys/{key_id}",
    }
    assert expected <= _router_paths(api_keys=True)
    assert not expected & _router_paths(api_keys=False)
    # 管理の画面はキーを発行しない（キーは各利用者が自分で発行する）。
    assert "POST /security/api-keys" not in _router_paths(api_keys=True)


def test_api_key_routes_create_list_and_revoke() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    other = _user(service, store, "other", "menu.search")
    _, _ = _create(service, other, name="other")

    class _Model(DisplayTimezoneData):
        pass

    def current(request: Request) -> Principal:
        return user

    app = FastAPI()
    app.include_router(
        build_auth_router(
            get_service=lambda: service,
            get_settings=lambda: None,  # type: ignore[arg-type,return-value]
            current_principal=current,
            request_context=lambda request: ("", ""),
            local_debug_principal=_local_principal,
            current_user_model=_Model,
            current_user_data=lambda principal, debug: _Model(),
            role_model=_Model,
            role_data=lambda role: _Model(),
            api_keys=True,
            public_base_url=lambda: "https://203.0.113.10:8443/",
        )
    )

    @app.exception_handler(SecurityApiError)
    async def handler(request: Request, exc: SecurityApiError) -> JSONResponse:
        return JSONResponse({"error_code": exc.code}, status_code=exc.status_code)

    client = TestClient(app)
    created = client.post(
        "/account/api-keys",
        json={"name": "Claude Code", "products": ["rag", "nl2sql"], "expires_in_days": 30},
    )
    assert created.status_code == 200
    body = created.json()["data"]
    token = body["token"]
    assert body["key"]["owner_display_name"] == "searcher さん"
    mine = client.get("/account/api-keys").json()["data"]
    # 自分の一覧は画面の接続先に出す公開の基底の URL（末尾の / を外す）も返す（#1870）。
    assert mine["public_base_url"] == "https://203.0.113.10:8443"
    listed = mine["keys"]
    # 自分の画面は自分のキーだけ。
    assert [item["id"] for item in listed] == [body["key"]["id"]]
    # 一覧は秘密も hash も返さない。
    assert token not in str(listed) and hash_api_key(token) not in str(listed)
    assert listed[0]["products"] == ["rag", "nl2sql"]
    # 管理の画面（manifest がメニュー権限を確かめる）は全員の分。
    everyone = client.get("/security/api-keys").json()["data"]["keys"]
    assert {item["owner_display_name"] for item in everyone} == {"searcher さん", "other さん"}
    assert client.delete(f"/account/api-keys/{body['key']['id']}").status_code == 200
    assert client.get("/account/api-keys").json()["data"]["keys"] == []
    assert client.delete(f"/account/api-keys/{body['key']['id']}").status_code == 404
    other_id = next(item["id"] for item in everyone if item["owner_display_name"] == "other さん")
    # 自分の画面からほかの利用者のキーは消せない。管理の画面からは消せる。
    assert client.delete(f"/account/api-keys/{other_id}").status_code == 404
    assert client.delete(f"/security/api-keys/{other_id}").status_code == 200
    assert client.get("/security/api-keys").json()["data"]["keys"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("  https://203.0.113.10/  ", "https://203.0.113.10"),
        ("http://10.0.0.5:8080", "http://10.0.0.5:8080"),
        ("https://203.0.113.10/rag/", ""),
        ("ftp://203.0.113.10", ""),
        ("203.0.113.10", ""),
        ("https://host name", ""),
    ],
    ids=["empty", "trailing-slash", "http-port", "path", "scheme", "no-scheme", "space"],
)
def test_normalize_public_base_url(value: str, expected: str) -> None:
    assert normalize_public_base_url(value) == expected


# ---- 保存（Oracle）・DDL ------------------------------------------------------------------


def test_platform_api_keys_table_is_in_platform_auth_ddl() -> None:
    joined = "\n".join(PLATFORM_AUTH_DDL)
    assert "CREATE TABLE PLATFORM_API_KEYS" in joined
    assert "CONSTRAINT PK_PLATFORM_API_KEYS PRIMARY KEY (KEY_ID)" in joined
    assert "CREATE INDEX IX_PLATFORM_API_KEYS_OWNER ON PLATFORM_API_KEYS" in joined
    # 対象の範囲の列（#1874）は、#1805 で作った既存の表にも足す（列があれば読み飛ばす）。
    assert "RESOURCE_SCOPES CLOB," in joined
    assert "ALTER TABLE PLATFORM_API_KEYS ADD (RESOURCE_SCOPES CLOB)" in joined
    assert API_KEYS_TABLE in PLATFORM_AUTH_TABLES


class _Cursor:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], *, fail: Exception | None) -> None:
        self.log = log
        self.fail = fail
        self.rowcount = 1

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.log.append((" ".join(sql.split()), binds or {}))


class _Connection:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], fail: Exception | None) -> None:
        self.log = log
        self.fail = fail
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self.log, fail=self.fail)

    def commit(self) -> None:
        self.commits += 1


def _oracle_store(fail: Exception | None = None) -> tuple[OracleApiKeyStore, list[Any]]:
    log: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def factory() -> Any:
        yield _Connection(log, fail)

    return OracleApiKeyStore(factory), log


def test_oracle_store_updates_last_used_only_after_the_interval() -> None:
    store, log = _oracle_store()
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    store.touch_last_used("abc", now=now)
    sql, binds = log[-1]
    assert sql.startswith("UPDATE PLATFORM_API_KEYS SET LAST_USED_AT = :now")
    assert "LAST_USED_AT IS NULL OR LAST_USED_AT <= :threshold" in sql
    assert binds["threshold"] == now - api_keys_module.LAST_USED_PERSIST_INTERVAL


def test_oracle_store_reports_missing_table_as_migration_required() -> None:
    store, _ = _oracle_store(RuntimeError("ORA-00942: table or view does not exist"))
    with pytest.raises(SecurityMigrationRequired) as missing:
        store.list()
    assert missing.value.object_name == API_KEYS_TABLE


def test_oracle_store_reports_missing_column_as_migration_required() -> None:
    store, _ = _oracle_store(RuntimeError('ORA-00904: "RESOURCE_SCOPES": invalid identifier'))
    with pytest.raises(SecurityMigrationRequired) as missing:
        store.get("abc")
    assert missing.value.object_name == API_KEYS_TABLE


def test_oracle_store_saves_resource_scopes_as_json() -> None:
    store, log = _oracle_store()
    record, _ = api_keys_module.new_api_key(
        name="n", owner_user_uuid="u", products=["agent"], resource_scopes={"agent": ["a1"]}
    )
    store.create(record)
    sql, binds = log[-1]
    assert "RESOURCE_SCOPES" in sql
    assert binds["resource_scopes"] == '{"agent": ["a1"]}'


def test_service_maps_missing_table_to_migration_error() -> None:
    service, store = _service()
    user = _user(service, store, "searcher", "menu.search")
    broken, _ = _oracle_store(RuntimeError("ORA-00942: table or view does not exist"))
    service.api_key_store = broken
    with pytest.raises(SecurityApiError) as denied:
        service.list_api_keys(user)
    assert (denied.value.status_code, denied.value.code) == (
        409,
        "SECURITY_SCHEMA_MIGRATION_REQUIRED",
    )
