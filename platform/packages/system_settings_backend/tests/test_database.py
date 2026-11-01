from __future__ import annotations

import io
import stat
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZipInfo

import pytest
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings import database as shared_db
from pr_system_settings.database import build_database_router

PEM = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
TNSNAMES = "ragdb_high = (description=(address=(protocol=tcps)(port=1522)(host=adb.example)))\n"


class FakeSettings(BaseModel):
    """製品の Settings のうちデータベース設定 API が読む属性だけを持つ。"""

    oracle_user: str = ""
    oracle_password: str = ""
    oracle_dsn: str = ""
    oracle_driver_mode: str = "thin"
    oracle_connection_security: str = "wallet_mtls"
    oracle_client_lib_dir: str = ""
    oracle_wallet_dir: str = ""
    oracle_wallet_password: str = ""
    oracle_db_test_timeout_seconds: float = 15.0

    @property
    def resolved_oracle_wallet_dir(self) -> str:
        return self.oracle_wallet_dir


def wallet_zip(files: dict[str, str] | None = None, *, prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    content = files or {"tnsnames.ora": TNSNAMES, "ewallet.pem": PEM, "README": "x"}
    with ZipFile(buffer, "w") as archive:
        for name, text in content.items():
            archive.writestr(prefix + name, text)
    return buffer.getvalue()


class Harness:
    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        self.settings = FakeSettings(oracle_wallet_dir=str(tmp_path / "wallet"))
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

    def upload(self, data: bytes, name: str = "wallet.zip") -> Any:
        return self.client.post(
            "/api/settings/database/wallet",
            files={"file": (name, data, "application/zip")},
        )


def test_patch_persists_then_applies_and_keeps_secret_out_of_response(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    resp = h.client.patch(
        "/api/settings/database",
        json={"user": "app", "dsn": "ragdb_high", "password": "db-secret"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["has_password"] is True
    assert "db-secret" not in resp.text
    env = dotenv_values(h.env_file)
    assert env["PLATFORM_ORACLE_USER"] == "app"
    assert env["PLATFORM_ORACLE_PASSWORD"] == "db-secret"
    assert env["PLATFORM_ORACLE_DRIVER_MODE"] == "thin"
    assert env["PLATFORM_ORACLE_CONNECTION_SECURITY"] == "wallet_mtls"
    assert h.settings.oracle_password == "db-secret"
    assert h.saved == ["app"]

    # 空欄は保持、clear は削除。
    h.client.patch("/api/settings/database", json={"user": "app", "dsn": "ragdb_high"})
    assert h.settings.oracle_password == "db-secret"
    h.client.patch(
        "/api/settings/database",
        json={"user": "app", "dsn": "ragdb_high", "clear_password": True},
    )
    assert h.settings.oracle_password == ""


def test_patch_failure_keeps_runtime(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.env_file = tmp_path  # directory なので書き込みに失敗する
    resp = h.client.patch("/api/settings/database", json={"user": "app", "dsn": "x"})
    assert resp.status_code == 500
    assert h.settings.oracle_user == ""
    assert h.saved == []


def test_same_path_compares_requested_path_lexically(tmp_path: Path) -> None:
    """利用者の path は字句だけで比べ、symlink を辿るのは設定の path だけ（#1646）。"""
    _same_path = shared_db._same_path
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert _same_path(str(tmp_path / "x" / ".." / "real"), str(real))
    # 設定が symlink で、画面が実体の path を送るときは同じとみなす。
    assert _same_path(str(real), str(link))
    # 利用者の path の symlink は辿らない（filesystem を参照しない）。
    assert not _same_path(str(link), str(real))
    assert not _same_path("", str(real))
    assert not _same_path(str(real), "")


def test_patch_rejects_other_wallet_dir_so_wallet_install_cannot_replace_it(
    tmp_path: Path,
) -> None:
    """API から別の Wallet の保存先を保存させない（Wallet の設置はそこを置き換えて削除する）。"""
    h = Harness(tmp_path)
    other = tmp_path / "important"
    other.mkdir()
    (other / "keep.txt").write_text("keep")

    resp = h.client.patch(
        "/api/settings/database",
        json={"user": "app", "dsn": "ragdb_high", "wallet_dir": str(other)},
    )
    assert resp.status_code == 422
    assert "PLATFORM_ORACLE_WALLET_DIR" in resp.json()["detail"]
    assert h.settings.oracle_wallet_dir == str(tmp_path / "wallet")
    assert not h.env_file.exists()
    test = h.client.post(
        "/api/settings/database/test",
        json={"user": "app", "dsn": "ragdb_high", "wallet_dir": str(other)},
    )
    assert test.status_code == 422

    assert h.upload(wallet_zip()).status_code == 200
    assert (other / "keep.txt").read_text() == "keep"

    # 今の保存先（画面が送る値）と空欄は受け取る。
    for wallet_dir in (str(tmp_path / "wallet"), str(tmp_path / "x" / ".." / "wallet"), ""):
        resp = h.client.patch(
            "/api/settings/database",
            json={"user": "app", "dsn": "ragdb_high", "wallet_dir": wallet_dir},
        )
        assert resp.status_code == 200
    assert dotenv_values(h.env_file)["PLATFORM_ORACLE_WALLET_DIR"] == str(tmp_path / "wallet")


def test_connection_security_is_ignored_unless_enabled(tmp_path: Path) -> None:
    disabled = Harness(tmp_path / "a")
    disabled.client.patch(
        "/api/settings/database",
        json={"user": "u", "dsn": "d", "connection_security": "walletless_tls"},
    )
    assert disabled.settings.oracle_connection_security == "wallet_mtls"

    enabled = Harness(tmp_path / "b", connection_security_enabled=True)
    enabled.client.patch(
        "/api/settings/database",
        json={"user": "u", "dsn": "d", "connection_security": "walletless_tls"},
    )
    assert enabled.settings.oracle_connection_security == "walletless_tls"


def test_wallet_upload_installs_securely_and_lists_services(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    resp = h.upload(wallet_zip(prefix="Wallet_ragdb/"))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["wallet_uploaded"] is True
    assert data["available_services"] == ["ragdb_high"]
    wallet = tmp_path / "wallet"
    assert stat.S_IMODE(wallet.stat().st_mode) == 0o700
    assert stat.S_IMODE((wallet / "ewallet.pem").stat().st_mode) == 0o600
    assert not (wallet / "README").exists()
    assert h.saved == [""]


@pytest.mark.parametrize(
    ("data", "name", "status"),
    [
        (wallet_zip(), "wallet.txt", 415),
        (b"", "wallet.zip", 400),
        (b"not a zip", "wallet.zip", 400),
        (wallet_zip({"tnsnames.ora": TNSNAMES}), "wallet.zip", 400),
        (wallet_zip({"../evil.ora": "x"}), "wallet.zip", 400),
    ],
    # 生成した zip の bytes は作成時刻を含む。xdist の worker ごとにテスト ID が
    # 変わらないよう ID を固定する（#344）。
    ids=["not-zip-name", "empty", "not-a-zip", "missing-files", "path-traversal"],
)
def test_wallet_upload_rejects_unsafe_files_and_keeps_previous_wallet(
    tmp_path: Path, data: bytes, name: str, status: int
) -> None:
    h = Harness(tmp_path)
    assert h.upload(wallet_zip()).status_code == 200
    resp = h.upload(data, name)
    assert resp.status_code == status
    assert (tmp_path / "wallet" / "ewallet.pem").read_text() == PEM
    assert not (tmp_path / "evil.ora").exists()


def test_wallet_upload_rejects_symlink(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        info = ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    assert Harness(tmp_path).upload(buffer.getvalue()).status_code == 400


def test_wallet_upload_returns_409_while_another_install_runs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with shared_db.database_wallet_install_lock(h.settings):
        assert h.upload(wallet_zip()).status_code == 409


def test_readiness_values(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    settings = h.settings
    assert shared_db.database_readiness(settings) == "missing"
    settings.oracle_user, settings.oracle_dsn = "app", "ragdb_high"
    assert shared_db.database_readiness(settings) == "wallet_not_found"
    h.upload(wallet_zip())
    assert shared_db.database_readiness(settings) == "ok"
    settings.oracle_dsn = "other_high"
    assert shared_db.database_readiness(settings) == "invalid"
    settings.oracle_connection_security = "walletless_tls"
    assert shared_db.database_readiness(settings) == "walletless_tls_dsn_required"
    assert shared_db.database_readiness(settings, lambda _s: "invalid_configuration") == (
        "invalid_configuration"
    )


def test_connection_test_uses_unsaved_values_and_explains_oracle_errors(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.upload(wallet_zip())
    body = {"user": "app", "dsn": "ragdb_high", "password": "unsaved"}

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


def test_wallet_download_route_is_removed(tmp_path: Path) -> None:
    """クラウドの API からの Wallet の自動取得は持たない（#11。Wallet はアップロードだけ）。"""
    h = Harness(tmp_path)
    assert h.client.post("/api/settings/database/wallet/download").status_code in {404, 405}


def test_settings_data_has_no_adb_fields(tmp_path: Path) -> None:
    data = Harness(tmp_path).client.get("/api/settings/database").json()["data"]
    assert "adb_ocid" not in data
    assert "region" not in data
