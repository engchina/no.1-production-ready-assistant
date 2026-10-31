"""外部の MCP クライアントの API キーで RAG の MCP（`POST /api/mcp`）を呼ぶ（#1805）。

production mode（InMemory の共通認証）で、利用者が自分の画面（`/api/account/api-keys`）から
キーを発行し、`Authorization: Bearer prak_…` で MCP を呼ぶ。Oracle・LLM は呼ばない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.main import app
from app.security.permissions import permission_for_route
from tests.security_support import ProductionAuth, enable_production_auth, login
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    return enable_production_auth(monkeypatch)


def _issue(session: dict[str, str], products: list[str] | None = None) -> dict[str, Any]:
    response = client.post(
        "/api/account/api-keys",
        json={"name": "Claude Code", "products": products or ["rag"], "expires_in_days": 30},
        headers=session,
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _rpc(method: str, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers=headers or {},
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_account_api_keys_need_login_but_no_menu_permission(auth: ProductionAuth) -> None:
    assert permission_for_route("POST", "/account/api-keys") is None
    assert permission_for_route("GET", "/security/api-keys") == frozenset(
        {"menu.security_api_keys"}
    )
    auth.user_with_permissions("viewer", ["menu.upload"])
    assert client.get("/api/account/api-keys").status_code == 401
    session = login(client, "viewer")
    assert client.get("/api/account/api-keys", headers=session).json()["data"]["keys"] == []
    # 全員のキー（管理の画面）はメニュー権限が要る。
    assert client.get("/api/security/api-keys", headers=session).status_code == 403


def test_api_key_calls_mcp_as_the_user(auth: ProductionAuth) -> None:
    auth.user_with_permissions("searcher", ["menu.search"])
    session = login(client, "searcher")
    created = _issue(session)
    headers = _bearer(created["token"])
    initialized = _rpc("initialize", headers)
    assert initialized.status_code == 200, initialized.text
    tools = _rpc("tools/list", headers).json()["result"]["tools"]
    # ツールは利用者の権限で決まる（検索の権限で検索のツール）。
    assert "rag_search" in {tool["name"] for tool in tools}
    listed = client.get("/api/account/api-keys", headers=session).json()["data"]["keys"]
    assert listed[0]["last_used_at"] is not None
    assert created["token"] not in str(listed)


def test_api_key_is_rejected_outside_mcp_and_with_bad_keys(auth: ProductionAuth) -> None:
    auth.user_with_permissions("searcher", ["menu.search"])
    session = login(client, "searcher")
    token = _issue(session)["token"]
    # 画面の API（Cookie / CSRF）にキーを付けても使えない。
    denied = client.get("/api/account/api-keys", headers=_bearer(token))
    assert (denied.status_code, denied.json()["error_code"]) == (
        401,
        "SECURITY_API_KEY_NOT_ALLOWED",
    )
    assert _rpc("tools/list").status_code == 401
    assert _rpc("tools/list", _bearer(token[:-2] + "zz")).status_code == 401
    # NL2SQL だけのキーは RAG で 403。
    nl2sql_only = _issue(session, ["nl2sql"])["token"]
    response = _rpc("tools/list", _bearer(nl2sql_only))
    assert (response.status_code, response.json()["error_code"]) == (
        403,
        "SECURITY_API_KEY_PRODUCT_FORBIDDEN",
    )


def test_expired_revoked_and_disabled_keys_are_rejected(auth: ProductionAuth) -> None:
    user = auth.user_with_permissions("searcher", ["menu.search"])
    session = login(client, "searcher")
    created = _issue(session)
    token = created["token"]
    key_id = created["key"]["id"]
    record = auth.service.api_key_store.get(key_id)
    assert record is not None
    # 期限切れ。
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    auth.service.api_key_store.create(record)
    assert _rpc("tools/list", _bearer(token)).status_code == 401
    # 失効（自分の画面から削除）。
    second = _issue(session)
    revoked = client.delete(f"/api/account/api-keys/{second['key']['id']}", headers=session)
    assert revoked.status_code == 200
    assert _rpc("tools/list", _bearer(second["token"])).status_code == 401
    # 利用者を無効にすると、キーも使えない。
    third = _issue(session)
    current = auth.store.get_user(user.user_uuid)
    assert current is not None
    auth.store.update_user(
        user.user_uuid,
        expected_version=current.version,
        display_name=current.display_name,
        status="DISABLED",
        role_ids=current.role_ids,
    )
    assert _rpc("tools/list", _bearer(third["token"])).status_code == 403
