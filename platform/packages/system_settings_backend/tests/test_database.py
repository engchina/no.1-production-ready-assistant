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
from pr_system_settings.oci_database import AutonomousDatabaseInfo

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
    oracle_adb_ocid: str = ""
    oracle_adb_region: str = ""
    oci_genai_embedding_dim: int = 1536
    oracle_db_test_timeout_seconds: float = 15.0

    @property
    def resolved_oracle_wallet_dir(self) -> str:
        return self.oracle_wallet_dir

    @property
    def resolved_oracle_adb_region(self) -> str:
        return self.oracle_adb_region


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


class FakeOciDatabaseClient:
    state = "STOPPED"
    fail = False
    started: list[str] = []

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
        if self.fail:
            raise RuntimeError("ServiceError: secret-looking OCI detail")
        return AutonomousDatabaseInfo(
            id=adb_ocid,
            display_name="ragdb",
            lifecycle_state=self.state,
            db_name="RAGDB",
            cpu_core_count=2,
            data_storage_size_in_tbs=1.0,
        )

    async def start_autonomous_database(self, adb_ocid: str) -> None:
        self.started.append(adb_ocid)

    async def stop_autonomous_database(self, adb_ocid: str) -> None:
        self.started.append("stop:" + adb_ocid)


def test_adb_settings_start_stop_and_hidden_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared_db, "OciDatabaseClient", FakeOciDatabaseClient)
    h = Harness(tmp_path)
    assert h.client.get("/api/settings/database/adb").json()["data"]["error_code"] == (
        "ADB_NOT_CONFIGURED"
    )

    saved = h.client.post(
        "/api/settings/database/adb/settings",
        json={"adb_ocid": "ocid1.autonomousdatabase.oc1..x", "region": "ap-osaka-1"},
    ).json()["data"]
    assert saved["lifecycle_state"] == "STOPPED"
    assert dotenv_values(h.env_file)["PLATFORM_ORACLE_ADB_REGION"] == "ap-osaka-1"

    started = h.client.post("/api/settings/database/adb/start").json()["data"]
    assert started["status"] == "accepted"
    assert started["lifecycle_state"] == "STARTING"
    stop = h.client.post("/api/settings/database/adb/stop").json()["data"]
    assert stop["status"] == "already_stopped"

    FakeOciDatabaseClient.fail = True
    try:
        failed = h.client.get("/api/settings/database/adb").json()["data"]
    finally:
        FakeOciDatabaseClient.fail = False
    assert failed["error_code"] == "ADB_INFO_UNAVAILABLE"
    assert "secret-looking" not in failed["message"]


class FakeOciServiceError(Exception):
    """oci.exceptions.ServiceError と同じ属性を持つ失敗（SDK を import しない）。"""

    def __init__(self) -> None:
        super().__init__("Authorization failed: wallet-password-must-not-be-logged")
        self.status = 404
        self.code = "NotAuthorizedOrNotFound"
        self.operation_name = "generate_autonomous_database_wallet"
        self.headers = {"opc-request-id": "req-123"}


def _log_extra(record: Any, key: str) -> Any:
    return getattr(record, key, None)


def _no_wallet_precheck(*_args: Any, **_kwargs: Any) -> None:
    """OCI 認証設定の確認を省き、OCI からの取得へ進める。"""
    return None


def test_wallet_download_failure_is_logged_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OCI からの Wallet の取得の失敗は、画面には一般的な文言、ログには切り分けの項目を出す。"""

    class FailingWalletClient(FakeOciDatabaseClient):
        async def download_autonomous_database_wallet(self, *args: Any) -> bytes:
            raise FakeOciServiceError()

    monkeypatch.setattr(shared_db, "OciDatabaseClient", FailingWalletClient)
    monkeypatch.setattr(shared_db, "prepare_database_wallet_download", _no_wallet_precheck)
    h = Harness(tmp_path)
    h.settings.oracle_adb_ocid = "ocid1.autonomousdatabase.oc1..x"
    h.settings.oracle_adb_region = "ap-osaka-1"
    h.settings.oracle_password = "db-secret-Pass1"

    with caplog.at_level("WARNING", logger=shared_db.logger.name):
        resp = h.client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 502
    assert "NotAuthorizedOrNotFound" not in resp.text
    records = [r for r in caplog.records if r.getMessage() == "database_wallet_download_failed"]
    assert len(records) == 1
    record = records[0]
    assert _log_extra(record, "exception_type") == "FakeOciServiceError"
    assert _log_extra(record, "oci_status") == 404
    assert _log_extra(record, "oci_code") == "NotAuthorizedOrNotFound"
    assert _log_extra(record, "oci_operation") == "generate_autonomous_database_wallet"
    assert _log_extra(record, "opc_request_id") == "req-123"
    assert _log_extra(record, "region") == "ap-osaka-1"
    logged = " ".join(f"{key}={value}" for key, value in vars(record).items())
    assert "db-secret-Pass1" not in logged
    assert "wallet-password-must-not-be-logged" not in logged


def test_wallet_download_invalid_zip_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OCI から取得した Wallet が検証で落ちた理由（自前の文言）をログに残す。"""

    class BrokenWalletClient(FakeOciDatabaseClient):
        async def download_autonomous_database_wallet(self, *args: Any) -> bytes:
            return wallet_zip({"README": "x"})

    monkeypatch.setattr(shared_db, "OciDatabaseClient", BrokenWalletClient)
    monkeypatch.setattr(shared_db, "prepare_database_wallet_download", _no_wallet_precheck)
    h = Harness(tmp_path)
    h.settings.oracle_adb_ocid = "ocid1.autonomousdatabase.oc1..x"
    h.settings.oracle_password = "db-secret-Pass1"

    with caplog.at_level("WARNING", logger=shared_db.logger.name):
        resp = h.client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 502
    records = [r for r in caplog.records if r.getMessage() == "database_wallet_download_invalid"]
    assert len(records) == 1
    assert _log_extra(records[0], "status_code") == 400
    assert "Wallet ZIP" in _log_extra(records[0], "reason")


def test_adb_failure_log_has_oci_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ADB の情報取得・起動・停止の失敗のログに OCI の status / code / request id を入れる。"""

    class FailingInfoClient(FakeOciDatabaseClient):
        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            raise FakeOciServiceError()

    monkeypatch.setattr(shared_db, "OciDatabaseClient", FailingInfoClient)
    h = Harness(tmp_path)
    h.settings.oracle_adb_ocid = "ocid1.autonomousdatabase.oc1..x"

    with caplog.at_level("WARNING", logger=shared_db.logger.name):
        data = h.client.get("/api/settings/database/adb").json()["data"]

    assert data["error_code"] == "ADB_INFO_UNAVAILABLE"
    record = next(r for r in caplog.records if r.getMessage() == "adb_operation_failed")
    assert _log_extra(record, "oci_status") == 404
    assert _log_extra(record, "oci_code") == "NotAuthorizedOrNotFound"
    assert _log_extra(record, "opc_request_id") == "req-123"
