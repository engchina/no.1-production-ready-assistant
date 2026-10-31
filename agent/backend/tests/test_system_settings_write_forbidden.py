"""共通のシステム設定の保存・操作の権限の拒否の文（利用者に見せる日本語の文）。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login
from starlette.requests import Request

from app.features.agent.router import require_system_settings_write
from app.security.domain import Principal
from app.security.permissions import MENU_SETTINGS_OCI
from app.security.service import set_security_service

FORBIDDEN_MESSAGE = "この機能を利用する権限がありません。"


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def _principal(permissions: set[str]) -> Principal:
    return Principal(
        user_uuid="u-settings-viewer",
        login_user_id="settings-viewer",
        display_name="設定の閲覧者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["VIEWER"],
        permissions=permissions,
        session_id="s-1",
        csrf_token_hash="",  # nosec B106 - テスト用
        password_change_allowed=True,
    )


def _request(method: str, path: str, principal: Principal | None) -> Request:
    route = type("_Route", (), {"path": path})()
    request = Request(
        {"type": "http", "method": method, "path": path, "headers": [], "route": route}
    )
    request.state.principal = principal
    return request


async def test_require_system_settings_write_denies_in_japanese_without_login_id() -> None:
    """権限の無い保存の 403 は日本語の文で、ログインユーザー ID や英語の内部の文を出さない。"""
    request = _request("PATCH", "/api/settings/upload-storage", _principal({MENU_SETTINGS_OCI}))

    with pytest.raises(HTTPException) as exc:
        await require_system_settings_write(request)

    assert exc.value.status_code == 403
    assert exc.value.detail == FORBIDDEN_MESSAGE
    assert "settings-viewer" not in str(exc.value.detail)


def test_settings_write_without_permission_returns_japanese_403(auth: ProductionAuth) -> None:
    """画面から呼んだときも、保存の権限が無ければ日本語の 403 を返す。"""
    auth.user_with_permissions("settings-viewer", [MENU_SETTINGS_OCI])
    headers = login("settings-viewer")

    response = client.patch(
        "/api/settings/upload-storage",
        json={"upload_storage_backend": "local", "local_storage_dir": "/tmp/x"},  # nosec B108
        headers=headers,
    )

    assert response.status_code == 403
    message = response.json()["error_messages"][0]
    assert message == FORBIDDEN_MESSAGE
    assert "cannot change system settings" not in response.text
