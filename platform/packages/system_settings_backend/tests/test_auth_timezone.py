"""`/auth/me` が返すシステムの時間帯（#3265）。画面は日時をこの時間帯で表示する。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings.auth.domain import LOCAL_DEBUG_USER_UUID, Principal
from pr_system_settings.auth.router import DisplayTimezoneData, build_auth_router


class _CurrentUser(DisplayTimezoneData):
    user_uuid: str


def _principal() -> Principal:
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


def _settings(**values: object) -> SimpleNamespace:
    return SimpleNamespace(
        local_debug_enabled=True,
        app_auth_cookie_secure=False,
        app_auth_absolute_timeout_hours=1,
        app_auth_session_cookie_name="session",
        app_auth_csrf_cookie_name="csrf",
        **values,
    )


def _client(settings: SimpleNamespace, model: type[BaseModel] = _CurrentUser) -> TestClient:
    def current(request: Request) -> Principal:
        return _principal()

    app = FastAPI()
    app.include_router(
        build_auth_router(
            get_service=lambda: None,  # type: ignore[arg-type,return-value]
            get_settings=lambda: settings,  # type: ignore[arg-type,return-value]
            current_principal=current,
            request_context=lambda request: ("", ""),
            local_debug_principal=_principal,
            current_user_model=model,
            current_user_data=lambda principal, debug: _CurrentUser(user_uuid=principal.user_uuid),
            role_model=_CurrentUser,
            role_data=lambda role: _CurrentUser(user_uuid=""),
        )
    )
    return TestClient(app)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("Asia/Tokyo", "Asia/Tokyo"), ("America/New_York", "America/New_York"), ("+09:00", "UTC")],
    ids=["tokyo", "region", "invalid-falls-back"],
)
def test_me_and_login_return_configured_timezone(configured: str, expected: str) -> None:
    client = _client(_settings(timezone=configured))
    me = client.get("/auth/me").json()["data"]
    assert me == {"timezone": expected, "user_uuid": LOCAL_DEBUG_USER_UUID}
    login = client.post("/auth/login", json={"login_user_id": "a", "password": "b"})
    assert login.json()["data"]["timezone"] == expected


def test_me_defaults_to_utc_when_setting_is_missing() -> None:
    # 既定は UTC（#3274）。
    assert _client(_settings()).get("/auth/me").json()["data"]["timezone"] == "UTC"


def test_current_user_model_must_carry_timezone() -> None:
    class _WithoutTimezone(BaseModel):
        user_uuid: str

    with pytest.raises(TypeError, match="DisplayTimezoneData"):
        _client(_settings(), model=_WithoutTimezone)
