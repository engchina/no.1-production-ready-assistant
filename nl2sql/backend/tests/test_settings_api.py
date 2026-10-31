from __future__ import annotations

import asyncio
import json
import stat
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZipInfo

import httpx
import pytest
from pr_system_settings import database as shared_database
from pr_system_settings.model import ModelSettingsStore, sanitize_model_test_error
from pr_system_settings.model_test_input import MODEL_TEST_IMAGE_BYTES
from pytest import MonkeyPatch

import app.settings as app_settings
from app.features.nl2sql.oracle_adapter import (
    OracleNl2SqlAdapter,
    oracle_connect_kwargs,
)
from app.features.settings import router as settings_router
from app.main import app
from app.settings import Settings, get_settings, load_persisted_model_settings


class _AsgiTestClient:
    """httpx 0.x と Starlette 1.x の同期 TestClient 非互換を避ける薄い test client。"""

    @staticmethod
    def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as async_client:
                return await async_client.request(method, path, **kwargs)

        return asyncio.run(send())

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("PATCH", path, **kwargs)


client = _AsgiTestClient()


def test_upload_database_wallet_extracts_to_thick_resolved_wallet_dir(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    client_lib_dir = tmp_path / "instantclient_23_26"
    monkeypatch.setattr(settings, "oracle_driver_mode", "thick")
    monkeypatch.setattr(settings, "oracle_connection_security", "wallet_mtls")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", str(client_lib_dir))
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")

    wallet_zip = _wallet_zip_bytes()
    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("Wallet_MYDB.zip", wallet_zip, "application/zip")},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    wallet_dir = client_lib_dir / "network" / "admin"
    assert data["driver_mode"] == settings.oracle_driver_mode
    assert data["client_lib_dir"] == str(client_lib_dir)
    assert data["wallet_dir"] == str(wallet_dir)
    assert data["wallet_uploaded"] is True
    assert "mydb_high" in data["available_services"]
    assert (wallet_dir / "tnsnames.ora").is_file()
    assert not (wallet_dir / "readme").exists()
    assert stat.S_IMODE(wallet_dir.stat().st_mode) == 0o700
    for file_name in shared_database.database_wallet_required_files(settings.oracle_driver_mode):
        assert stat.S_IMODE((wallet_dir / file_name).stat().st_mode) == 0o600


def test_database_wallet_state_uses_thin_mtls_required_files(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir(parents=True)
    (wallet_dir / "tnsnames.ora").write_text("mydb_high = (...)\n", encoding="utf-8")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", str(wallet_dir))

    data = shared_database.database_settings_data(settings)

    assert data.wallet_uploaded is False
    assert data.available_services == []

    (wallet_dir / "ewallet.pem").write_text("dummy", encoding="utf-8")

    configured = shared_database.database_settings_data(settings)

    assert configured.wallet_uploaded is True
    assert configured.available_services == ["mydb_high"]


def test_reveal_database_password_returns_saved_secret_only_on_explicit_route(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_password", "database-secret")

    settings_resp = client.get("/api/settings/database")
    reveal_resp = client.post("/api/settings/database/password/reveal")

    assert settings_resp.status_code == 200
    assert "database-secret" not in settings_resp.text
    assert settings_resp.json()["data"]["has_password"] is True
    assert reveal_resp.status_code == 200
    assert reveal_resp.headers["cache-control"] == "no-store"
    assert reveal_resp.headers["pragma"] == "no-cache"
    assert reveal_resp.json()["data"] == {"password": "database-secret"}


def test_reveal_database_password_reports_missing_without_secret(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_password", "")

    resp = client.post("/api/settings/database/password/reveal")

    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    assert "保存済み DB パスワードがありません" in resp.text
    assert "secret" not in resp.text.lower()


def test_database_wallet_install_lock_returns_conflict(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_upload(monkeypatch, tmp_path)

    with shared_database.database_wallet_install_lock(settings):
        resp = client.post(
            "/api/settings/database/wallet",
            files={"file": ("wallet.zip", _wallet_zip_bytes(), "application/zip")},
        )

    assert resp.status_code == 409
    assert "処理中" in resp.text


def test_database_wallet_install_lock_permission_error_is_structured(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_upload(monkeypatch, tmp_path)
    original_open = Path.open

    def deny_lock_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name == ".wallet.install.lock":
            raise PermissionError("/secret/wallet-parent is not writable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_lock_open)

    resp = client.post(
        "/api/settings/database/wallet",
        headers={"X-Request-ID": "wallet-storage-request"},
        files={"file": ("wallet.zip", _wallet_zip_bytes(), "application/zip")},
    )

    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "WALLET_STORAGE_UNAVAILABLE"
    assert body["problem"]["code"] == "WALLET_STORAGE_UNAVAILABLE"
    assert body["problem"]["request_id"] == "wallet-storage-request"
    assert body["problem"]["retryable"] is True
    assert "書き込み権限" in body["problem"]["detail"]
    assert "/secret/wallet-parent" not in resp.text


def test_database_wallet_install_failure_restores_existing_wallet(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_upload(monkeypatch, tmp_path)
    wallet_dir = shared_database.install_database_wallet(
        settings,
        _wallet_zip_bytes(),
        "old-wallet.zip",
    )
    old_tnsnames = (wallet_dir / "tnsnames.ora").read_bytes()

    def fail_permissions(path: Path) -> None:
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(shared_database, "_secure_database_wallet", fail_permissions)

    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("new-wallet.zip", _wallet_zip_bytes(), "application/zip")},
    )

    assert resp.status_code == 500
    assert resp.json()["error_code"] == "WALLET_INSTALL_FAILED"
    assert resp.json()["problem"]["retryable"] is True
    assert "simulated chmod failure" not in resp.text
    assert (wallet_dir / "tnsnames.ora").read_bytes() == old_tnsnames
    assert not list(wallet_dir.parent.glob(f".{wallet_dir.name}.backup-*"))


def test_upload_database_wallet_rejects_zip_slip_without_overwriting(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_upload(monkeypatch, tmp_path)
    wallet_dir = shared_database.install_database_wallet(
        settings,
        _wallet_zip_bytes(),
        "old-wallet.zip",
    )
    old_tnsnames = (wallet_dir / "tnsnames.ora").read_bytes()
    unsafe_zip = BytesIO()
    with ZipFile(unsafe_zip, "w") as archive:
        archive.writestr("../tnsnames.ora", "unsafe")

    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("unsafe.zip", unsafe_zip.getvalue(), "application/zip")},
    )

    assert resp.status_code == 400
    assert "安全でないファイルパス" in resp.text
    assert (wallet_dir / "tnsnames.ora").read_bytes() == old_tnsnames


def test_upload_database_wallet_rejects_symlink_member(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_upload(monkeypatch, tmp_path)
    unsafe_zip = BytesIO()
    with ZipFile(unsafe_zip, "w") as archive:
        symlink = ZipInfo("Wallet_MYDB/cwallet.sso")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(symlink, "tnsnames.ora")

    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("symlink.zip", unsafe_zip.getvalue(), "application/zip")},
    )

    assert resp.status_code == 400
    assert "シンボリックリンク" in resp.text


def test_upload_database_wallet_enforces_extracted_size_limit(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_upload(monkeypatch, tmp_path)
    monkeypatch.setattr(shared_database, "ORACLE_WALLET_MAX_EXTRACTED_BYTES", 8)

    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("wallet.zip", _wallet_zip_bytes(), "application/zip")},
    )

    assert resp.status_code == 413
    assert "展開後サイズ" in resp.text


def test_update_database_settings_preserves_client_lib_dir_in_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    env_file = tmp_path / ".env"
    client_lib_dir = tmp_path / "instantclient_23_26"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "oracle_driver_mode", "thick")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", str(client_lib_dir))
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_password", "old-password")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")

    resp = client.patch(
        "/api/settings/database",
        json={
            "user": "ADMIN",
            "dsn": "mydb_high",
            "wallet_dir": str(client_lib_dir / "network" / "admin"),
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["driver_mode"] == "thick"
    assert data["connection_security"] == "wallet_mtls"
    assert data["client_lib_dir"] == str(client_lib_dir)
    assert data["wallet_dir"] == str(client_lib_dir / "network" / "admin")
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_ORACLE_DRIVER_MODE=thick" in env_text
    assert "PLATFORM_ORACLE_CONNECTION_SECURITY=wallet_mtls" in env_text
    assert f"PLATFORM_ORACLE_CLIENT_LIB_DIR={client_lib_dir}" in env_text
    assert f"PLATFORM_ORACLE_CLIENT_LIB_DIR={client_lib_dir / 'network' / 'admin'}" not in env_text
    assert f"PLATFORM_ORACLE_WALLET_DIR={client_lib_dir / 'network' / 'admin'}" in env_text
    assert "PLATFORM_ORACLE_PASSWORD=old-password" in env_text


def test_update_database_settings_persists_thin_wallet_dir_separately(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    env_file = tmp_path / ".env"
    wallet_dir = tmp_path / "wallet"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_connection_security", "wallet_mtls")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", str(wallet_dir))
    monkeypatch.setattr(settings, "oracle_password", "old-password")
    monkeypatch.setattr(settings, "oracle_wallet_password", "wallet-secret")

    resp = client.patch(
        "/api/settings/database",
        json={
            "user": "ADMIN",
            "dsn": "mydb_high",
            "wallet_dir": str(wallet_dir),
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["driver_mode"] == "thin"
    assert data["connection_security"] == "wallet_mtls"
    assert data["client_lib_dir"] == ""
    assert data["wallet_dir"] == str(wallet_dir)
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_ORACLE_DRIVER_MODE=thin" in env_text
    assert "PLATFORM_ORACLE_CONNECTION_SECURITY=wallet_mtls" in env_text
    assert "PLATFORM_ORACLE_CLIENT_LIB_DIR=\n" in env_text
    assert f"PLATFORM_ORACLE_WALLET_DIR={wallet_dir}" in env_text
    assert "PLATFORM_ORACLE_WALLET_PASSWORD=wallet-secret" in env_text


def test_database_connection_test_rejects_dsn_missing_from_wallet(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("mydb_high = (...)\n", encoding="utf-8")
    (wallet_dir / "ewallet.pem").write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", str(wallet_dir))
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_password", "database-secret")
    monkeypatch.setattr(settings, "oracle_dsn", "missing_high")

    async def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid Wallet DSN must not reach python-oracledb")

    monkeypatch.setattr(settings_router, "test_oracle_connection", fail_if_called)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "ADMIN",
            "dsn": "missing_high",
            "wallet_dir": str(wallet_dir),
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["readiness"] == "invalid"
    assert "tnsnames.ora" in data["message"]
    assert any("tnsnames.ora" in tip for tip in data["troubleshooting"])


def test_database_connection_test_rejects_walletless_tls_wallet_alias(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_connection_security", "walletless_tls")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_password", "database-secret")
    monkeypatch.setattr(settings, "oracle_dsn", "mydb_high")

    async def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("walletless TLS alias must not reach python-oracledb")

    monkeypatch.setattr(settings_router, "test_oracle_connection", fail_if_called)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "ADMIN",
            "dsn": "mydb_high",
            "connection_security": "walletless_tls",
            "wallet_dir": "",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["readiness"] == "walletless_tls_dsn_required"
    assert "Walletless TLS" in data["message"]
    assert any("Walletless TLS" in tip for tip in data["troubleshooting"])


def test_upload_storage_defaults_use_nl2sql_names(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("PLATFORM_LOCAL_STORAGE_DIR", raising=False)

    settings = Settings(_env_file=None)

    assert settings.local_storage_dir == "/u01/data/production-ready-nl2sql"


def test_update_upload_storage_persists_local_dir_only(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """アップロード保存先はローカルのディレクトリだけを保存する（Object Storage は #11 で削除）。"""
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "local_storage_dir", settings.local_storage_dir)

    resp = client.patch(
        "/api/settings/upload-storage",
        json={"local_storage_dir": "/u01/data/production-ready-nl2sql-next"},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["local_storage_dir"] == "/u01/data/production-ready-nl2sql-next"
    assert "backend" not in data
    assert "object_storage_bucket" not in data
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_LOCAL_STORAGE_DIR=/u01/data/production-ready-nl2sql-next" in env_text
    assert "OBJECT_STORAGE" not in env_text
    assert "UPLOAD_STORAGE_BACKEND" not in env_text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings/oci"),
        ("PATCH", "/api/settings/oci"),
        ("POST", "/api/settings/oci/config/read"),
        ("POST", "/api/settings/oci/config/test"),
        ("POST", "/api/settings/oci/key-file"),
        ("PATCH", "/api/settings/oci/object-storage"),
        ("POST", "/api/settings/oci/object-storage/namespace"),
        ("POST", "/api/settings/upload-storage/test"),
        ("POST", "/api/settings/database/wallet/download"),
    ],
)
def test_oci_settings_routes_are_removed(method: str, path: str) -> None:
    """OCI 認証・Object Storage の確認・Wallet の OCI からの取得の API は持たない（#11）。"""
    response = client._request(method, path)
    assert response.status_code in {404, 405}


def test_update_model_settings_persists_v2_json_and_env_secret(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    model_settings_file = tmp_path / "model-settings.json"
    env_file = tmp_path / ".env"
    env_file.write_text("PLATFORM_OCI_ENTERPRISE_AI_API_KEY=saved-secret\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(settings, "model_settings_file", str(model_settings_file))
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    settings.set_runtime_enterprise_ai_api_key("saved-secret")

    resp = client.patch(
        "/api/settings/model",
        json={
            "enterprise_ai": {
                "endpoint": "https://enterprise-ai.example.com",
                "project_ocid": "ocid1.project.oc1..example",
                "api_key": "",
                "has_api_key": True,
                "clear_api_key": False,
                "models": [
                    {
                        "model_id": "cohere.command-r-plus",
                        "display_name": "回答生成",
                        "vision_enabled": False,
                    },
                    {
                        "model_id": "mistral.vision-model",
                        "display_name": "画像解析",
                        "vision_enabled": True,
                    },
                ],
                "default_text_model_id": "cohere.command-r-plus",
                "default_vision_model_id": "mistral.vision-model",
                "api_path": "/responses",
                "vlm_input_mode": "auto",
                "text_payload_template": "",
                "vision_payload_template": "",
                "text_response_path": "",
                "vision_response_path": "",
                "timeout_seconds": 600.0,
                "max_retries": 3,
            },
            "generative_ai": {
                "embedding_model": "cohere.embed-v4.0",
                "embedding_dim": 1536,
                "rerank_model": "cohere.rerank-v4.0-fast",
            },
        },
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["settings"]["enterprise_ai"]["connections"][0]["api_key"] == ""
    models = resp.json()["data"]["settings"]["enterprise_ai"]["models"]
    assert [model["model_id"] for model in models] == [
        "cohere.command-r-plus",
        "mistral.vision-model",
    ]
    assert models[1]["vision_enabled"] is True
    document = json.loads(model_settings_file.read_text(encoding="utf-8"))
    assert document["version"] == 6
    assert "api_key" not in document["enterprise_ai"]
    assert document["enterprise_ai"]["models"][1]["model_id"] == "mistral.vision-model"
    assert document["enterprise_ai"]["default_text_model_id"] == "cohere.command-r-plus"
    assert document["enterprise_ai"]["default_vision_model_id"] == "mistral.vision-model"
    assert document["generative_ai"]["embedding_dim"] == 1536
    assert (
        env_file.read_text(encoding="utf-8") == "PLATFORM_OCI_ENTERPRISE_AI_API_KEY=saved-secret\n"
    )
    assert resp.json()["data"]["secret_source"] == "environment"
    assert resp.json()["data"]["legacy_secret_detected"] is False
    assert stat.S_IMODE(model_settings_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_enterprise_ai_api_key_env_update_and_clear_are_atomic(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# keep this comment\n"
        "PLATFORM_OCI_ENTERPRISE_AI_API_KEY=old-secret\n"
        "PLATFORM_OCI_REGION=x\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)

    ModelSettingsStore._write_api_key(env_file, "new-secret")

    updated = env_file.read_text(encoding="utf-8")
    assert "old-secret" not in updated
    assert "PLATFORM_OCI_ENTERPRISE_AI_API_KEY=new-secret" in updated
    assert "# keep this comment" in updated
    assert "PLATFORM_OCI_REGION=x" in updated
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    ModelSettingsStore._write_api_key(env_file, None)

    cleared = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_OCI_ENTERPRISE_AI_API_KEY" not in cleared
    assert "# keep this comment" in cleared
    assert "PLATFORM_OCI_REGION=x" in cleared
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_load_persisted_model_settings_applies_runtime_fields(tmp_path: Path) -> None:
    model_settings_file = tmp_path / "model-settings.json"
    model_settings_file.write_text(
        """
        {
          "version": 1,
          "enterprise_ai": {
            "endpoint": "https://enterprise-ai.example.com",
            "project_ocid": "ocid1.project.oc1..example",
            "api_key": "persisted-secret",
            "models": [
              {
                "model_id": "cohere.command-r-plus",
                "display_name": "回答生成",
                "vision_enabled": false
              },
              {
                "model_id": "mistral.vision-model",
                "display_name": "画像解析",
                "vision_enabled": true
              }
            ],
            "default_model_id": "cohere.command-r-plus",
            "api_path": "/responses",
            "vlm_input_mode": "inline_image",
            "timeout_seconds": 120,
            "max_retries": 4
          },
          "generative_ai": {
            "embedding_model": "cohere.embed-v4.0",
            "embedding_dim": 1536,
            "rerank_model": "cohere.rerank-v4.0-fast"
          }
        }
        """,
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, model_settings_file=str(model_settings_file))

    load_persisted_model_settings(settings)

    assert settings.oci_enterprise_ai_endpoint == "https://enterprise-ai.example.com"
    assert settings.oci_enterprise_ai_api_key == "persisted-secret"
    assert settings.model_secret_source == "legacy_json"
    assert settings.legacy_model_secret_detected is True
    assert settings.oci_enterprise_ai_default_text_model == "cohere.command-r-plus"
    assert settings.oci_enterprise_ai_default_vision_model == "mistral.vision-model"
    assert [model.model_id for model in settings.oci_enterprise_ai_models] == [
        "cohere.command-r-plus",
        "mistral.vision-model",
    ]
    assert settings.oci_enterprise_ai_vlm_input_mode == "inline_image"
    assert settings.oci_enterprise_ai_max_retries == 4
    assert settings.oci_genai_embed_model_id == "cohere.embed-v4.0"
    assert settings.oci_genai_rerank_model_id == "cohere.rerank-v4.0-fast"


def test_environment_model_secret_takes_precedence_over_v1_json(tmp_path: Path) -> None:
    model_settings_file = tmp_path / "model-settings.json"
    model_settings_file.write_text(
        '{"version":1,"enterprise_ai":{"api_key":"legacy-secret"}}',
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        model_settings_file=str(model_settings_file),
        oci_enterprise_ai_api_key="environment-secret",
    )

    load_persisted_model_settings(settings)

    assert settings.oci_enterprise_ai_api_key == "environment-secret"
    assert settings.model_secret_source == "environment"
    assert settings.legacy_model_secret_detected is True


def test_v3_model_settings_never_reads_api_key_field(tmp_path: Path) -> None:
    model_settings_file = tmp_path / "model-settings.json"
    model_settings_file.write_text(
        '{"version":3,"enterprise_ai":{"api_key":"must-not-load"}}',
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, model_settings_file=str(model_settings_file))

    load_persisted_model_settings(settings)

    assert settings.oci_enterprise_ai_api_key == ""
    assert settings.model_secret_source == "missing"
    assert settings.legacy_model_secret_detected is True


def test_settings_read_platform_env_then_product_env(tmp_path: Path) -> None:
    """共通キーは共通 .env、製品キーは backend/.env から読む（#211）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text(
        "PLATFORM_ORACLE_DSN=shared_high\n",
        encoding="utf-8",
    )
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "NL2SQL_LOG_LEVEL=WARNING\nNL2SQL_DEFAULT_ROW_LIMIT=250\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=(platform_env, backend_env))

    assert settings.oracle_dsn == "shared_high"
    assert settings.log_level == "WARNING"
    assert settings.nl2sql_default_row_limit == 250


def test_settings_ignore_legacy_env_names(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新名がないとき、旧名（接頭辞なし）を環境変数からも .env からも読まない（#211。互換なし）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("OCI_REGION=legacy-region\n", encoding="utf-8")
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("LOG_LEVEL=WARN\nDEFAULT_ROW_LIMIT=999\n", encoding="utf-8")
    monkeypatch.delenv("PLATFORM_ORACLE_DSN", raising=False)
    monkeypatch.setenv("ORACLE_DSN", "legacy_high")
    monkeypatch.setenv("OCI_COMPARTMENT_ID", "legacy-compartment")

    settings = Settings(_env_file=(platform_env, backend_env))

    assert settings.oracle_dsn == ""
    assert not hasattr(settings, "oci_compartment_id")
    assert not hasattr(settings, "oci_region")
    assert settings.log_level == "INFO"
    assert settings.nl2sql_default_row_limit == 100
    # 属性名での生成（テストや内部の Settings(oracle_dsn=...)）は引き続き使える。
    assert Settings(_env_file=None, oracle_dsn="named_high").oracle_dsn == "named_high"


def test_model_settings_file_resolves_next_to_platform_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """model-settings.json は3製品で共有し、相対パスは共通 .env の場所から解決する（#211）。"""
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", tmp_path / "shared" / ".env")

    assert (
        app_settings.resolve_model_settings_file("model-settings.json")
        == (tmp_path / "shared" / "model-settings.json").resolve()
    )
    assert app_settings.resolve_model_settings_file(str(tmp_path / "abs.json")) == (
        tmp_path / "abs.json"
    )


def test_nonlocal_security_boundaries_fail_closed() -> None:
    with pytest.raises(ValueError, match="PLATFORM_AUTH_MODE=authenticated"):
        Settings(
            _env_file=None,
            environment="production",
            auth_mode="local",
        )
    with pytest.raises(ValueError, match="PLATFORM_AUTH_COOKIE_SECURE=true"):
        Settings(
            _env_file=None,
            environment="production",
            auth_mode="authenticated",
            app_auth_cookie_secure=False,
        )

    settings = Settings(
        _env_file=None,
        environment="production",
        auth_mode="authenticated",
        app_auth_cookie_secure=True,
    )
    assert settings.local_debug_enabled is False


def test_model_settings_test_calls_enterprise_client(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeEnterpriseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def generate(self, prompt: str, context: str = "") -> str:
            captured["prompt"] = prompt
            captured["context"] = context
            captured["endpoint"] = self.settings.oci_enterprise_ai_endpoint
            captured["model_id"] = self.settings.oci_enterprise_ai_default_text_model
            captured["api_key"] = self.settings.oci_enterprise_ai_api_key
            return "テスト応答"

    monkeypatch.setattr(settings_router, "OciEnterpriseAiClient", FakeEnterpriseClient)

    resp = client.post(
        "/api/settings/model/test",
        json={
            "target_type": "enterprise_text",
            "model_id": "cohere.command-r-plus",
            "settings": {
                "enterprise_ai": {
                    "endpoint": "https://enterprise-ai.example.com",
                    "project_ocid": "ocid1.project.oc1..example",
                    "api_key": "request-secret",
                    "models": [
                        {
                            "model_id": "cohere.command-r-plus",
                            "display_name": "回答生成",
                            "vision_enabled": False,
                        }
                    ],
                    "default_text_model_id": "cohere.command-r-plus",
                    "api_path": "/responses",
                    "vlm_input_mode": "auto",
                    "timeout_seconds": 10,
                    "max_retries": 0,
                },
                "generative_ai": {
                    "embedding_model": "cohere.embed-v4.0",
                    "embedding_dim": 1536,
                    "rerank_model": "cohere.rerank-v4.0-fast",
                },
            },
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "success"
    assert data["details"]["response_chars"] == 5
    assert captured["endpoint"] == "https://enterprise-ai.example.com"
    assert captured["model_id"] == "cohere.command-r-plus"
    assert captured["api_key"] == "request-secret"
    assert captured["context"] == (
        "これは Production Ready NL2SQL のモデル接続テスト用コンテキストです。"
    )
    deprecated_product_label = "Production Ready " + "RAG"
    assert deprecated_product_label not in captured["context"]


def test_model_settings_error_sanitizer_never_returns_secret() -> None:
    sanitized = sanitize_model_test_error(
        "gateway rejected Bearer request-secret with status 401",
        ["request-secret"],
    )

    assert "request-secret" not in sanitized
    assert sanitized == "gateway rejected Bearer <secret> with status 401"


def test_model_settings_test_enterprise_vision_uses_smoke_image_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    observed: list[tuple[Settings, bytes, str, str]] = []

    class FakeEnterpriseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def generate_from_image(
            self,
            image_bytes: bytes,
            prompt: str,
            *,
            mime_type: str,
            response_format: object | None = None,
        ) -> str:
            observed.append((self.settings, image_bytes, prompt, mime_type))
            return "画像を確認しました。"

    monkeypatch.setattr(settings_router, "OciEnterpriseAiClient", FakeEnterpriseClient)

    resp = client.post(
        "/api/settings/model/test",
        json={
            "target_type": "enterprise_vision",
            "model_id": "google.gemini-2.5-flash",
            "settings": {
                "enterprise_ai": {
                    "endpoint": "https://enterprise-ai.example.com",
                    "project_ocid": "ocid1.project.oc1..example",
                    "api_key": "request-secret",
                    "models": [
                        {
                            "model_id": "cohere.command-r-plus",
                            "display_name": "回答生成",
                            "vision_enabled": False,
                        },
                        {
                            "model_id": "google.gemini-2.5-flash",
                            "display_name": "Vision",
                            "vision_enabled": True,
                        },
                    ],
                    "default_text_model_id": "cohere.command-r-plus",
                    "api_path": "/responses",
                    "vlm_input_mode": "auto",
                    "timeout_seconds": 10,
                    "max_retries": 0,
                },
                "generative_ai": {
                    "embedding_model": "cohere.embed-v4.0",
                    "embedding_dim": 1536,
                    "rerank_model": "cohere.rerank-v4.0-fast",
                },
            },
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "success"
    assert data["details"]["surface"] == "vision"
    assert data["details"]["response_chars"] == len("画像を確認しました。")
    assert observed[0][0].oci_enterprise_ai_default_vision_model == "google.gemini-2.5-flash"
    assert observed[0][1] == MODEL_TEST_IMAGE_BYTES
    assert observed[0][2]
    assert observed[0][3] == "image/jpeg"


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
def test_adb_control_routes_are_removed(method: str, path: str) -> None:
    """ADB の情報取得・起動・停止の API は持たない（#10。起動・停止は docker 側の責務）。"""
    response = client.get(path) if method == "GET" else client.post(path)
    assert response.status_code in {404, 405}


def test_oracle_adapter_wallet_only_connection_uses_wallet_kwargs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text(
        "mydb_high = "
        "(DESCRIPTION=(retry_count=20)(retry_delay=3)"
        "(ADDRESS=(PROTOCOL=tcps)(HOST=db.example.com)(PORT=1522))"
        "(CONNECT_DATA=(SERVICE_NAME=mydb_high)))\n",
        encoding="utf-8",
    )
    (wallet_dir / "ewallet.pem").write_text("dummy", encoding="utf-8")
    captured: dict[str, Any] = {}

    class FakeOracleCursor:
        def __enter__(self) -> FakeOracleCursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            captured["sql"] = sql

        def fetchone(self) -> tuple[int]:
            return (1,)

    class FakeOracleConnection:
        def cursor(self) -> FakeOracleCursor:
            return FakeOracleCursor()

        def close(self) -> None:
            captured["closed"] = True

    class FakeOracleDbModule:
        def connect(self, **kwargs: object) -> FakeOracleConnection:
            captured["connect_kwargs"] = kwargs
            return FakeOracleConnection()

    def fake_import_module(name: str) -> object:
        if name == "oracledb":
            return FakeOracleDbModule()
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr(
        "app.features.nl2sql.oracle_adapter.importlib.import_module",
        fake_import_module,
    )
    settings = Settings(
        oracle_user="ADMIN",
        oracle_password="",
        oracle_dsn="mydb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir=str(wallet_dir),
        nl2sql_oracle_connect_timeout_seconds=3,
    )

    ok, message = OracleNl2SqlAdapter(settings).test_connection()

    assert ok is True, message
    kwargs = captured["connect_kwargs"]
    assert kwargs["user"] == "ADMIN"
    assert "password" not in kwargs
    assert kwargs["config_dir"] == str(wallet_dir)
    assert kwargs["wallet_location"] == str(wallet_dir)
    assert kwargs["tcp_connect_timeout"] == 3
    assert "retry_count" not in str(kwargs["dsn"]).lower()
    assert captured["sql"] == "SELECT 1 FROM DUAL"
    assert captured["closed"] is True


def test_oracle_connect_kwargs_walletless_tls_skips_wallet_kwargs(tmp_path: Path) -> None:
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("mydb_high = (...)\n", encoding="utf-8")
    settings = Settings(
        oracle_user="ADMIN",
        oracle_password="database-secret",
        oracle_dsn="adb.example.oraclecloud.com:1522/mydb_high",
        oracle_connection_security="walletless_tls",
        oracle_wallet_dir=str(wallet_dir),
        nl2sql_oracle_connect_timeout_seconds=3,
    )

    kwargs = oracle_connect_kwargs(settings)

    assert kwargs["user"] == "ADMIN"
    assert kwargs["password"] == "database-secret"
    assert kwargs["dsn"] == "adb.example.oraclecloud.com:1522/mydb_high"
    assert kwargs["tcp_connect_timeout"] == 3
    assert "config_dir" not in kwargs
    assert "wallet_location" not in kwargs
    assert "wallet_password" not in kwargs


def test_oracle_connect_kwargs_wallet_mtls_requires_wallet_readiness(tmp_path: Path) -> None:
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("mydb_high = (...)\n", encoding="utf-8")
    settings = Settings(
        oracle_user="ADMIN",
        oracle_password="database-secret",
        oracle_dsn="mydb_high",
        oracle_connection_security="wallet_mtls",
        oracle_wallet_dir=str(wallet_dir),
    )

    with pytest.raises(Exception, match="ewallet.pem"):
        oracle_connect_kwargs(settings)

    (wallet_dir / "ewallet.pem").write_text("dummy", encoding="utf-8")

    kwargs = oracle_connect_kwargs(settings)

    assert kwargs["config_dir"] == str(wallet_dir)
    assert kwargs["wallet_location"] == str(wallet_dir)


def _configure_wallet_upload(monkeypatch: MonkeyPatch, tmp_path: Path) -> Settings:
    settings = get_settings()
    wallet_dir = tmp_path / "wallet"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", str(wallet_dir))
    monkeypatch.setattr(settings, "oracle_password", "database-secret")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")
    return settings


def _wallet_zip_bytes() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Wallet_MYDB/tnsnames.ora",
            "mydb_high = (DESCRIPTION=(ADDRESS=(PROTOCOL=tcps)(HOST=db.example.com)))\n",
        )
        archive.writestr("Wallet_MYDB/sqlnet.ora", "WALLET_LOCATION=(SOURCE=(METHOD=file))\n")
        archive.writestr("Wallet_MYDB/cwallet.sso", "dummy")
        archive.writestr("Wallet_MYDB/ewallet.pem", "dummy")
        archive.writestr("Wallet_MYDB/readme", "skip me")
    return buffer.getvalue()
