from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings import database as shared_db
from pr_system_settings.database import build_database_router

DSN = "db.example.com:1521/app_svc"


class FakeSettings(BaseModel):
    """製品の Settings のうちデータベース設定 API が読む属性だけを持つ。"""

    oracle_user: str = ""
    oracle_password: str = ""
    oracle_dsn: str = ""
    oracle_driver_mode: str = "thin"
    oracle_client_lib_dir: str = ""
    oracle_db_test_timeout_seconds: float = 15.0


class Harness:
    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        self.settings = FakeSettings()
        self.env_file = tmp_path / ".env"
        self.saved: list[str] = []
        self.connect_error: Exception | None = None
        self.connected: list[FakeSettings] = []

        async def test_connection(candidate: Any) -> None:
            self.connected.append(candidate)
            if self.connect_error is not None:
                raise self.connect_error

        app = FastAPI()
        app.include_router(
            build_database_router(
                get_settings=lambda: self.settings,
                env_file=lambda: self.env_file,
                test_connection=test_connection,
                on_saved=lambda settings: self.saved.append(settings.oracle_user),
                **kwargs,
            ),
            prefix="/api/settings",
        )
        self.client = TestClient(app)


def test_patch_persists_then_applies_and_keeps_secret_out_of_response(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    resp = h.client.patch(
        "/api/settings/database",
        json={"user": "app", "dsn": DSN, "password": "db-secret"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["has_password"] is True
    assert "db-secret" not in resp.text
    env = dotenv_values(h.env_file)
    assert env["PLATFORM_ORACLE_USER"] == "app"
    assert env["PLATFORM_ORACLE_PASSWORD"] == "db-secret"
    assert env["PLATFORM_ORACLE_DRIVER_MODE"] == "thin"
    assert h.settings.oracle_password == "db-secret"
    assert h.saved == ["app"]

    # 空欄は保持、clear は削除。
    h.client.patch("/api/settings/database", json={"user": "app", "dsn": DSN})
    assert h.settings.oracle_password == "db-secret"
    h.client.patch(
        "/api/settings/database",
        json={"user": "app", "dsn": DSN, "clear_password": True},
    )
    assert h.settings.oracle_password == ""


def test_patch_failure_keeps_runtime(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.env_file = tmp_path  # directory なので書き込みに失敗する
    resp = h.client.patch("/api/settings/database", json={"user": "app", "dsn": "x"})
    assert resp.status_code == 500
    assert h.settings.oracle_user == ""
    assert h.saved == []


def test_readiness_values(tmp_path: Path) -> None:
    settings = Harness(tmp_path).settings
    assert shared_db.database_readiness(settings) == "missing"
    settings.oracle_user, settings.oracle_dsn = "app", DSN
    assert shared_db.database_readiness(settings) == "missing_credentials"
    settings.oracle_password = "db-secret"
    assert shared_db.database_readiness(settings) == "ok"
    settings.oracle_dsn = "(description=(address=(host=db.example.com)))"
    assert shared_db.database_readiness(settings) == "ok"
    settings.oracle_dsn = "other_alias"
    assert shared_db.database_readiness(settings) == "invalid"
    assert shared_db.database_readiness(settings, lambda _s: "invalid_configuration") == (
        "invalid_configuration"
    )


def test_connection_test_uses_unsaved_values_and_explains_oracle_errors(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    body = {"user": "app", "dsn": DSN, "password": "unsaved"}

    ok = h.client.post("/api/settings/database/test", json=body).json()["data"]
    assert ok["status"] == "success"
    assert h.connected[-1].oracle_password == "unsaved"
    assert h.settings.oracle_user == ""  # 保存していない

    h.connect_error = RuntimeError("ORA-01017: invalid username/password")
    failed = h.client.post("/api/settings/database/test", json=body).json()["data"]
    assert failed["status"] == "failed"
    assert "ORA-01017" in failed["message"]
    assert failed["details"]["oracle_error_codes"] == "ORA-01017"
    assert any("DB パスワード" in tip for tip in failed["troubleshooting"])

    not_ready = h.client.post("/api/settings/database/test", json={"user": "", "dsn": ""}).json()[
        "data"
    ]
    assert not_ready["readiness"] == "missing"


def test_password_reveal_only_when_enabled(tmp_path: Path) -> None:
    disabled = Harness(tmp_path / "a")
    assert disabled.client.post("/api/settings/database/password/reveal").status_code in {404, 405}

    enabled = Harness(tmp_path / "b", password_reveal_enabled=True)
    assert enabled.client.post("/api/settings/database/password/reveal").status_code == 404
    enabled.settings.oracle_password = "db-secret"
    resp = enabled.client.post("/api/settings/database/password/reveal")
    assert resp.json()["data"]["password"] == "db-secret"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings/database/adb"),
        *(
            ("POST", f"/api/settings/database/adb/{action}")
            for action in ("settings", "start", "stop")
        ),
    ],
)
def test_adb_control_routes_are_removed(tmp_path: Path, method: str, path: str) -> None:
    """ADB の情報取得・起動・停止の API は持たない（#10。起動・停止は docker 側の責務）。"""
    h = Harness(tmp_path)
    assert h.client.request(method, path).status_code in {404, 405}


def test_settings_data_has_no_adb_fields(tmp_path: Path) -> None:
    data = Harness(tmp_path).client.get("/api/settings/database").json()["data"]
    assert "adb_ocid" not in data
    assert "region" not in data
