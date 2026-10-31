"""MCP 接続の URL に書いた資格情報（userinfo・secret らしい query）を出さない（#1056）。

- 保存の API は userinfo と secret らしい query を 422 の日本語の文で拒否する（値は文に含めない）
- 既に保存した URL は壊さず、一覧・保存の応答では `***` に伏せ、実際の接続には保存した URL を使う
- 画面が伏せた URL を送り返さない（URL を変えない保存）なら、保存した URL はそのまま残る

どの接続も ID を固有にし、テストの最後に消す（並列でも直列でも通る）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import suppress

import httpx
import pytest
from pytest import MonkeyPatch
from security_support import client

from app.features.agent.config import McpConnectionConfig, runtime_config_store
from app.features.agent.mcp_url import (
    is_secret_query_name,
    mask_url_credentials,
    mask_urls_in_text,
    url_credential_error,
)
from app.features.agent.tools import (
    ExternalToolError,
    ToolInvocationContext,
    _fetch_mcp_oauth_bearer_token,
    list_mcp_connection_tools,
)

LEGACY_URL = "https://svc-user:pa55-1056@erp.example.test/mcp?tenant=t1&api_key=k-1056"
MASKED_LEGACY_URL = "https://***@erp.example.test/mcp?tenant=t1&api_key=***"
TOKEN_URL = "https://idp:tp-1056@idp.example.test/token?client_secret=tk-1056"


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "apiKey",
        "APIKEY",
        "X-Api-Key",
        "key",
        "token",
        "access_token",
        "accessToken",
        "client_secret",
        "password",
        "X-Amz-Signature",
        "X-Amz-Credential",
        "sig",
        "auth",
        "jwt",
    ],
)
def test_secret_query_names(name: str) -> None:
    assert is_secret_query_name(name)


@pytest.mark.parametrize(
    "name", ["tenant", "region", "version", "author", "keyword", "monkey", "design", "format"]
)
def test_ordinary_query_names_are_not_secret(name: str) -> None:
    assert not is_secret_query_name(name)


@pytest.mark.parametrize(
    ("url", "masked"),
    [
        (LEGACY_URL, MASKED_LEGACY_URL),
        ("https://only-user@h.test/mcp", "https://***@h.test/mcp"),
        (
            "https://h.test/mcp?X-Amz-Signature=abc&x=1",
            "https://h.test/mcp?X-Amz-Signature=***&x=1",
        ),
        ("https://h.test/mcp?api%5Fkey=abc", "https://h.test/mcp?api%5Fkey=***"),
        ("https://h.test/mcp?tenant=t1", "https://h.test/mcp?tenant=t1"),
        ("https://h.test:8443/api/mcp", "https://h.test:8443/api/mcp"),
    ],
    ids=["userinfo-and-query", "user-only", "signature", "encoded-name", "plain-query", "plain"],
)
def test_mask_url_credentials(url: str, masked: str) -> None:
    assert mask_url_credentials(url) == masked


def test_mask_urls_in_text() -> None:
    text = f"failed to reach {LEGACY_URL} (timeout)"
    assert mask_urls_in_text(text) == f"failed to reach {MASKED_LEGACY_URL} (timeout)"


def test_url_credential_error_does_not_include_values() -> None:
    userinfo = url_credential_error("https://u:secret-1056@h.test/mcp")
    query = url_credential_error("https://h.test/mcp?api_key=secret-1056&token=t")
    assert userinfo is not None and "ユーザー名・パスワード" in userinfo
    assert query is not None and "api_key・token" in query
    assert "secret-1056" not in userinfo + query
    assert url_credential_error("https://h.test/mcp?tenant=t1") is None


# ---------------------------------------------------------------------------
# 保存の API
# ---------------------------------------------------------------------------


@pytest.fixture
def created_ids() -> Iterator[list[str]]:
    ids: list[str] = []
    yield ids
    for server_id in ids:
        with suppress(KeyError, ValueError):
            runtime_config_store.remove_mcp_server(server_id)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://svc:pw-1056@erp.example.test/mcp", "ユーザー名・パスワード（user:pass@）"),
        ("https://svc@erp.example.test/mcp", "ユーザー名・パスワード（user:pass@）"),
        ("https://erp.example.test/mcp?api_key=pw-1056", "資格情報のパラメータ（api_key）"),
        (
            "https://erp.example.test/mcp?x=1&accessToken=pw-1056",
            "資格情報のパラメータ（accessToken）",
        ),
    ],
    ids=["userinfo", "user-only", "api-key-query", "camel-token-query"],
)
def test_create_rejects_credentials_in_url(url: str, message: str, created_ids: list[str]) -> None:
    created_ids.append("cred1056")
    response = client.post(
        "/api/settings/mcp-connections", json={"server_id": "cred1056", "base_url": url}
    )

    assert response.status_code == 422, response.text
    (error,) = response.json()["error_messages"]
    assert message in error
    assert "「認証」の欄" in error
    assert "pw-1056" not in response.text
    with pytest.raises(KeyError):
        runtime_config_store.get_mcp("cred1056")


def test_patch_rejects_credentials_and_keeps_saved_url(created_ids: list[str]) -> None:
    created_ids.append("patch1056")
    runtime_config_store.upsert_mcp_server("patch1056", base_url="https://erp.example.test/mcp")

    response = client.patch(
        "/api/settings/mcp-connections/patch1056",
        json={"base_url": "https://erp.example.test/mcp?token=pw-1056"},
    )

    assert response.status_code == 422, response.text
    assert "pw-1056" not in response.text
    assert runtime_config_store.get_mcp("patch1056").base_url == "https://erp.example.test/mcp"


def test_ordinary_query_is_saved_and_returned_as_is(created_ids: list[str]) -> None:
    created_ids.append("plain1056")
    url = "https://erp.example.test/mcp?tenant=t1"

    response = client.post(
        "/api/settings/mcp-connections", json={"server_id": "plain1056", "base_url": url}
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["base_url"] == url
    assert response.json()["data"]["base_url_masked"] is False


# ---------------------------------------------------------------------------
# 既に保存した URL（保存の検証の前のデータ・宣言）
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_connection() -> Iterator[str]:
    runtime_config_store.restore_mcp_server(
        McpConnectionConfig(server_id="legacy1056", base_url=LEGACY_URL, auth_mode="none")
    )
    yield "legacy1056"
    runtime_config_store.remove_mcp_server("legacy1056")


def _listed(server_id: str) -> dict[str, object]:
    response = client.get("/api/settings/mcp-connections")
    assert response.status_code == 200, response.text
    assert "pa55-1056" not in response.text and "k-1056" not in response.text
    (connection,) = [
        item for item in response.json()["data"]["connections"] if item["server_id"] == server_id
    ]
    return dict(connection)


def test_list_masks_saved_credentials(legacy_connection: str) -> None:
    connection = _listed(legacy_connection)

    assert connection["base_url"] == MASKED_LEGACY_URL
    assert connection["base_url_masked"] is True
    # 保存した URL は壊さない。
    assert runtime_config_store.get_mcp(legacy_connection).base_url == LEGACY_URL


def test_save_without_url_keeps_saved_url_and_masks_response(legacy_connection: str) -> None:
    response = client.patch(
        f"/api/settings/mcp-connections/{legacy_connection}", json={"timeout_seconds": 20}
    )

    assert response.status_code == 200, response.text
    assert "pa55-1056" not in response.text and "k-1056" not in response.text
    assert response.json()["data"]["base_url"] == MASKED_LEGACY_URL
    assert runtime_config_store.get_mcp(legacy_connection).base_url == LEGACY_URL


def test_masked_url_cannot_overwrite_saved_url(legacy_connection: str) -> None:
    response = client.patch(
        f"/api/settings/mcp-connections/{legacy_connection}",
        json={"base_url": MASKED_LEGACY_URL},
    )

    assert response.status_code == 422, response.text
    assert runtime_config_store.get_mcp(legacy_connection).base_url == LEGACY_URL


def test_connection_uses_saved_url(monkeypatch: MonkeyPatch, legacy_connection: str) -> None:
    urls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        result: dict[str, object] = (
            {"capabilities": {}} if body["method"] == "initialize" else {"tools": []}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_client = httpx.Client
    monkeypatch.setattr(
        "app.features.agent.tools.httpx.Client",
        lambda timeout, **_: real_client(transport=httpx.MockTransport(handle), timeout=timeout),
    )

    data = list_mcp_connection_tools(
        legacy_connection, context=ToolInvocationContext(user_uuid="u-1056")
    )

    assert data.tools == []
    assert urls and all("api_key=k-1056" in url for url in urls)


# ---------------------------------------------------------------------------
# エラーの詳細（OAuth のトークン URL）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("timed out"), "external_mcp.oauth_timeout"),
        (httpx.ConnectError(f"cannot reach {TOKEN_URL}"), "external_mcp.oauth_request_error"),
    ],
    ids=["timeout", "request-error"],
)
def test_oauth_error_details_mask_token_url(
    monkeypatch: MonkeyPatch, error: httpx.HTTPError, code: str
) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise error

    real_client = httpx.Client
    monkeypatch.setattr(
        "app.features.agent.tools.httpx.Client",
        lambda timeout, **_: real_client(transport=httpx.MockTransport(handle), timeout=timeout),
    )

    with pytest.raises(ExternalToolError) as raised:
        _fetch_mcp_oauth_bearer_token(
            token_url=TOKEN_URL,
            client_id="client-1056",
            client_secret="secret-1056",  # nosec B106 - テスト用
            scope=None,
            timeout_seconds=1,
        )

    assert raised.value.code == code
    rendered = json.dumps(raised.value.details, ensure_ascii=False) + raised.value.message
    assert "tp-1056" not in rendered and "tk-1056" not in rendered
