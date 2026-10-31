from __future__ import annotations

import asyncio
import json
import stat
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile, ZipInfo

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values
from fastapi import HTTPException
from pr_system_settings import database as shared_database
from pr_system_settings import oci as shared_oci
from pr_system_settings.model import ModelSettingsStore, sanitize_model_test_error
from pr_system_settings.model_test_input import MODEL_TEST_IMAGE_BYTES
from pr_system_settings.oci_database import (
    AutonomousDatabaseInfo,
    OciDatabaseClient,
    WalletDownloadTooLargeError,
)
from pytest import MonkeyPatch

import app.settings as app_settings
from app.features.nl2sql.oracle_adapter import (
    OracleNl2SqlAdapter,
    SelectAiCredentialExistsError,
    oracle_connect_kwargs,
)
from app.features.settings import router as settings_router
from app.main import app
from app.schemas.settings import SelectAiCredentialCreateRequest
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


def _write_oci_signing_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    key_content: bytes | None = None,
    key_exists: bool = True,
) -> tuple[Settings, Path, bytes]:
    settings = get_settings()
    config_file = tmp_path / "config"
    key_file = tmp_path / "oci_api_key.pem"
    if key_content is None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_content = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    if key_exists:
        key_file.write_bytes(key_content)
        key_file.chmod(0o600)
    config_file.write_text(
        "[DEFAULT]\n"
        "user=ocid1.user.oc1..example\n"
        "tenancy=ocid1.tenancy.oc1..example\n"
        f"fingerprint={'aa:' * 15}aa\n"
        "region=ap-osaka-1\n"
        f"key_file={key_file}\n",
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    return settings, key_file, key_content


class _FakeSelectAiCredentialAdapter:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.calls: list[dict[str, Any]] = []

    def get_select_ai_credential_status(self, credential_name: str) -> tuple[str, bool]:
        assert credential_name == "NL2SQL_OCI_CRED"
        return "ADMIN", self.exists

    def create_select_ai_credential(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.exists and not kwargs["recreate"]:
            raise SelectAiCredentialExistsError("exists")
        operation = "recreated" if self.exists else "created"
        self.exists = True
        return operation


@pytest.mark.parametrize(
    ("initial_exists", "recreate", "expected_operation"),
    [(False, False, "created"), (True, True, "recreated")],
)
def test_select_ai_credential_create_and_recreate_persist_safe_settings(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    initial_exists: bool,
    recreate: bool,
    expected_operation: str,
) -> None:
    settings, _key_file, key_content = _write_oci_signing_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        settings,
        "nl2sql_select_ai_credential_name",
        settings.nl2sql_select_ai_credential_name,
    )
    monkeypatch.setattr(
        settings,
        "nl2sql_select_ai_region",
        settings.nl2sql_select_ai_region,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\n", encoding="utf-8")
    fake_adapter = _FakeSelectAiCredentialAdapter(exists=initial_exists)
    monkeypatch.setattr(app_settings, "BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr(
        settings_router,
        "OracleNl2SqlAdapter",
        lambda settings: fake_adapter,
    )

    response = client.post(
        "/api/settings/database/select-ai-credential",
        json={
            "region": "us-chicago-1",
            "confirmation": "ADMIN_EXECUTE",
            "recreate": recreate,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["operation"] == expected_operation
    assert response.json()["data"]["exists"] is True
    assert fake_adapter.calls[0]["credential_name"] == "NL2SQL_OCI_CRED"
    assert fake_adapter.calls[0]["recreate"] is recreate
    normalized_private_key = fake_adapter.calls[0]["private_key"]
    assert "BEGIN PRIVATE KEY" not in normalized_private_key
    assert "\n" not in normalized_private_key
    env_text = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in env_text
    assert "NL2SQL_SELECT_AI_CREDENTIAL_NAME=NL2SQL_OCI_CRED" in env_text
    assert "NL2SQL_SELECT_AI_REGION=us-chicago-1" in env_text
    assert settings.nl2sql_select_ai_credential_name == "NL2SQL_OCI_CRED"
    assert settings.nl2sql_select_ai_region == "us-chicago-1"
    secret_body = b"".join(
        line for line in key_content.splitlines() if not line.startswith(b"-----")
    ).decode("ascii")
    assert secret_body not in response.text
    assert secret_body not in env_text
    assert secret_body not in caplog.text


def test_select_ai_credential_create_succeeds_when_final_status_read_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """作成後の状態の読み直しが失敗しても、作成済みとして 200 を返す（失敗として見せない）。"""
    settings, _key_file, _key_content = _write_oci_signing_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        settings, "nl2sql_select_ai_credential_name", settings.nl2sql_select_ai_credential_name
    )
    monkeypatch.setattr(settings, "nl2sql_select_ai_region", settings.nl2sql_select_ai_region)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    class _FlakyStatusAdapter(_FakeSelectAiCredentialAdapter):
        def get_select_ai_credential_status(self, credential_name: str) -> tuple[str, bool]:
            if self.calls:
                raise RuntimeError("ORA-03113: end-of-file on communication channel")
            return super().get_select_ai_credential_status(credential_name)

    fake_adapter = _FlakyStatusAdapter(exists=False)
    monkeypatch.setattr(app_settings, "BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr(settings_router, "OracleNl2SqlAdapter", lambda settings: fake_adapter)

    response = client.post(
        "/api/settings/database/select-ai-credential",
        json={"region": "ap-osaka-1", "confirmation": "ADMIN_EXECUTE", "recreate": False},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["operation"] == "created"
    assert data["exists"] is True
    assert data["schema_name"] == "ADMIN"
    assert data["region"] == "ap-osaka-1"
    assert "ORA-03113" not in response.text


def test_select_ai_credential_status_failure_returns_problem_contract(
    monkeypatch: MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingAdapter(_FakeSelectAiCredentialAdapter):
        def get_select_ai_credential_status(self, credential_name: str) -> tuple[str, bool]:
            raise RuntimeError("raw Oracle target=ADMIN.NL2SQL_OCI_CRED ocid1.tenancy.oc1..leaked")

    monkeypatch.setattr(
        settings_router,
        "OracleNl2SqlAdapter",
        lambda settings: FailingAdapter(),
    )

    response = client.get(
        "/api/settings/database/select-ai-credential",
        headers={"X-Request-ID": "select-ai-status-request"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "SELECT_AI_CREDENTIAL_STATUS_UNAVAILABLE"
    assert body["problem"]["code"] == "SELECT_AI_CREDENTIAL_STATUS_UNAVAILABLE"
    assert body["problem"]["request_id"] == "select-ai-status-request"
    assert body["problem"]["retryable"] is True
    assert response.headers["X-Request-ID"] == "select-ai-status-request"
    assert "raw Oracle target" not in response.text
    assert "ocid1.tenancy.oc1..leaked" not in response.text
    record = next(r for r in caplog.records if r.message == "select_ai_credential_status_failed")
    assert record.__dict__["diagnostic_category"] == "unknown"
    assert record.__dict__["suggested_action"]
    assert "ocid1.tenancy" not in record.__dict__["summary"]


def test_select_ai_credential_existing_requires_explicit_recreate(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_oci_signing_config(tmp_path, monkeypatch)
    fake_adapter = _FakeSelectAiCredentialAdapter(exists=True)
    monkeypatch.setattr(
        settings_router,
        "OracleNl2SqlAdapter",
        lambda settings: fake_adapter,
    )

    response = client.post(
        "/api/settings/database/select-ai-credential",
        headers={"X-Request-ID": "select-ai-exists-request"},
        json={
            "region": "ap-osaka-1",
            "confirmation": "ADMIN_EXECUTE",
            "recreate": False,
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "SELECT_AI_CREDENTIAL_EXISTS"
    assert body["problem"]["code"] == "SELECT_AI_CREDENTIAL_EXISTS"
    assert body["problem"]["request_id"] == "select-ai-exists-request"
    assert response.headers["X-Request-ID"] == "select-ai-exists-request"
    assert "private_key" not in response.text.lower()


def test_select_ai_credential_rejects_wrong_confirmation_before_reading_key(
    monkeypatch: MonkeyPatch,
) -> None:
    called = False

    def unexpected_adapter(settings: Settings) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(settings_router, "OracleNl2SqlAdapter", unexpected_adapter)
    response = client.post(
        "/api/settings/database/select-ai-credential",
        headers={"X-Request-ID": "select-ai-confirmation-request"},
        json={
            "region": "ap-osaka-1",
            "confirmation": "WRONG",
            "recreate": False,
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "SELECT_AI_CONFIRMATION_REQUIRED"
    assert body["problem"]["code"] == "SELECT_AI_CONFIRMATION_REQUIRED"
    assert body["problem"]["request_id"] == "select-ai-confirmation-request"
    assert body["problem"]["retryable"] is False
    assert called is False


@pytest.mark.parametrize(
    ("key_content", "key_exists", "expected_field"),
    [
        (b"", False, "key_file"),
        (
            b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n",
            True,
            "private_key_encrypted",
        ),
        (
            b"-----BEGIN PRIVATE KEY-----\ndamaged\n-----END PRIVATE KEY-----\n",
            True,
            "private_key",
        ),
    ],
)
def test_select_ai_credential_status_reports_missing_or_invalid_key_without_secret(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    key_content: bytes,
    key_exists: bool,
    expected_field: str,
) -> None:
    _write_oci_signing_config(
        tmp_path,
        monkeypatch,
        key_content=key_content,
        key_exists=key_exists,
    )
    fake_adapter = _FakeSelectAiCredentialAdapter(exists=False)
    monkeypatch.setattr(
        settings_router,
        "OracleNl2SqlAdapter",
        lambda settings: fake_adapter,
    )

    response = client.get("/api/settings/database/select-ai-credential")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["credential_name"] == "NL2SQL_OCI_CRED"
    assert data["schema_name"] == "ADMIN"
    assert data["exists"] is False
    assert data["oci_auth_ready"] is False
    assert expected_field in data["missing_fields"]
    if key_content:
        assert key_content.decode("ascii", errors="ignore") not in response.text

    create_response = client.post(
        "/api/settings/database/select-ai-credential",
        headers={"X-Request-ID": f"select-ai-key-{expected_field}"},
        json={
            "region": "ap-osaka-1",
            "confirmation": "ADMIN_EXECUTE",
            "recreate": False,
        },
    )
    assert create_response.status_code == 422
    body = create_response.json()
    assert body["error_code"] == "SELECT_AI_PRIVATE_KEY_INVALID"
    assert body["problem"]["code"] == "SELECT_AI_PRIVATE_KEY_INVALID"
    assert body["problem"]["request_id"] == f"select-ai-key-{expected_field}"
    assert fake_adapter.calls == []


def test_select_ai_credential_rejects_private_key_with_broad_permissions(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _settings, key_file, _key_content = _write_oci_signing_config(tmp_path, monkeypatch)
    key_file.chmod(0o644)
    fake_adapter = _FakeSelectAiCredentialAdapter()
    monkeypatch.setattr(
        settings_router,
        "OracleNl2SqlAdapter",
        lambda settings: fake_adapter,
    )

    response = client.get("/api/settings/database/select-ai-credential")

    assert response.status_code == 200
    assert response.json()["data"]["missing_fields"] == ["key_file_permissions"]
    assert fake_adapter.calls == []


def test_select_ai_credential_create_reports_incomplete_oci_config(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(tmp_path / "missing"))
    fake_adapter = _FakeSelectAiCredentialAdapter(exists=False)
    monkeypatch.setattr(
        settings_router,
        "OracleNl2SqlAdapter",
        lambda settings: fake_adapter,
    )
    response = client.post(
        "/api/settings/database/select-ai-credential",
        headers={"X-Request-ID": "select-ai-oci-config-request"},
        json={
            "region": "ap-osaka-1",
            "confirmation": "ADMIN_EXECUTE",
            "recreate": False,
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "SELECT_AI_OCI_CONFIG_INCOMPLETE"
    assert body["problem"]["code"] == "SELECT_AI_OCI_CONFIG_INCOMPLETE"
    assert body["problem"]["request_id"] == "select-ai-oci-config-request"


def test_select_ai_credential_routes_keep_sync_oracle_io_off_asgi_event_loop() -> None:
    assert asyncio.iscoroutinefunction(settings_router.get_select_ai_credential) is False
    assert asyncio.iscoroutinefunction(settings_router.create_select_ai_credential) is False


def test_select_ai_credential_defaults_to_chicago_when_unconfigured(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_select_ai_region", "")
    monkeypatch.setattr(settings, "oci_region", "")

    assert settings_router._select_ai_region(settings) == "us-chicago-1"
    assert SelectAiCredentialCreateRequest().region == "us-chicago-1"


def test_read_object_storage_namespace_uses_oci_sdk(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class FakeObjectStorageClient:
        def __init__(self, config: dict[str, Any]) -> None:
            captured["config"] = config

        def get_namespace(self) -> object:
            captured["get_namespace_called"] = True
            return SimpleNamespace(data="mytenancynamespace")

    def fake_from_file(path: str, profile: str) -> dict[str, Any]:
        captured["config_path"] = path
        captured["profile"] = profile
        return {"region": "ap-tokyo-1"}

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(from_file=fake_from_file)
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=FakeObjectStorageClient)
        raise AssertionError(f"unexpected module import: {name}")

    # importlib.import_module 自体を書き換えると、TestClient/AnyIO の遅延 import まで
    # 偽装されて request が待ち続けるため、router が参照する module だけを差し替える。
    monkeypatch.setattr(
        shared_oci,
        "importlib",
        SimpleNamespace(import_module=fake_import_module),
    )
    config_file = tmp_path / "config"
    # 取得に使う config は実行中の設定のもの（要求の config_file は使わない。#1067）。
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")

    resp = client.post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "other"),
            "profile": "OTHER",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"namespace": "mytenancynamespace"}
    assert captured["config_path"] == str(config_file)
    assert captured["profile"] == "DEFAULT"
    assert captured["config"] == {"region": "ap-osaka-1"}
    assert captured["get_namespace_called"] is True


def test_read_object_storage_namespace_reports_oci_errors(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(from_file=lambda path, profile: {"region": "ap-tokyo-1"})
        if name == "oci.object_storage":
            raise RuntimeError("sdk unavailable")
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr(
        shared_oci,
        "importlib",
        SimpleNamespace(import_module=fake_import_module),
    )

    resp = client.post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "config"),
            "profile": "DEFAULT",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 502
    assert "namespace を取得できませんでした" in resp.text


def test_read_object_storage_namespace_refuses_encrypted_private_key_without_prompt(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "encrypted.pem"
    key_file.write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    initialized = False

    class FakeObjectStorageClient:
        def __init__(self, config: dict[str, Any]) -> None:
            nonlocal initialized
            initialized = True

        def get_namespace(self) -> object:
            return SimpleNamespace(data="mytenancynamespace")

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(
                from_file=lambda path, profile: {"key_file": str(key_file), "region": "ap-tokyo-1"}
            )
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=FakeObjectStorageClient)
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr(
        shared_oci,
        "importlib",
        SimpleNamespace(import_module=fake_import_module),
    )

    resp = client.post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "config"),
            "profile": "DEFAULT",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 502
    assert "暗号化されています" in resp.text
    assert initialized is False


def test_update_oci_settings_writes_config_and_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\nPLATFORM_OCI_REGION=old-region\n", encoding="utf-8")
    settings = get_settings()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")

    resp = client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..example",
            "fingerprint": "aa:bb:cc",
            "tenancy": "ocid1.tenancy.oc1..example",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 200
    config_file = home / ".oci" / "config"
    config_text = config_file.read_text(encoding="utf-8")
    assert "user=ocid1.user.oc1..example" in config_text
    assert "fingerprint=aa:bb:cc" in config_text
    assert "tenancy=ocid1.tenancy.oc1..example" in config_text
    assert "region=ap-osaka-1" in config_text
    assert "key_file=~/.oci/oci_api_key.pem" in config_text
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_file.parent.stat().st_mode) == 0o700

    env_text = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in env_text
    assert "PLATFORM_OCI_CONFIG_FILE=~/.oci/config" in env_text
    assert "PLATFORM_OCI_CONFIG_PROFILE=DEFAULT" in env_text
    assert "PLATFORM_OCI_REGION=ap-osaka-1" in env_text


def test_update_oci_settings_does_not_write_empty_config_defaults(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\nPLATFORM_OCI_REGION=old-region\n", encoding="utf-8")
    settings = get_settings()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oci_region", "us-chicago-1")

    resp = client.patch(
        "/api/settings/oci",
        json={"user": "", "fingerprint": "", "tenancy": "", "region": ""},
    )

    assert resp.status_code == 200
    config_text = (home / ".oci" / "config").read_text(encoding="utf-8")
    assert "user=" not in config_text
    assert "fingerprint=" not in config_text
    assert "tenancy=" not in config_text
    assert "region=" not in config_text
    assert "key_file=" not in config_text
    assert settings.oci_region == ""
    env_text = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in env_text
    assert "PLATFORM_OCI_REGION" not in env_text


def test_read_oci_config_reports_missing_profile(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config"
    config_file.write_text(
        "[DEFAULT]\n"
        "user=ocid1.user.oc1..example\n"
        "fingerprint=aa:bb:cc\n"
        "tenancy=ocid1.tenancy.oc1..example\n"
        "region=ap-osaka-1\n",
        encoding="utf-8",
    )

    # 読むのは実行中の設定の config / profile（#1067）。
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "MISSING")

    resp = client.post(
        "/api/settings/oci/config/read",
        json={"config_file": str(config_file), "profile": "DEFAULT"},
    )

    assert resp.status_code == 404
    assert "profile が見つかりません" in resp.text


def test_oci_config_test_returns_elapsed_time_for_missing_config(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(tmp_path / "missing-config"))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")

    resp = client.post("/api/settings/oci/config/test")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["error_type"] == "HTTPException"
    assert isinstance(data["elapsed_ms"], int)
    assert data["elapsed_ms"] >= 0


def test_upload_oci_private_key_saves_fixed_secure_path(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    resp = client.post(
        "/api/settings/oci/key-file",
        files={
            "file": (
                "oci_api_key.pem",
                b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
                "application/x-pem-file",
            )
        },
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"key_file": "~/.oci/oci_api_key.pem", "saved": True}
    key_file = home / ".oci" / "oci_api_key.pem"
    assert key_file.read_text(encoding="utf-8").startswith("-----BEGIN PRIVATE KEY-----")
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700


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
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", False)

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


def test_download_database_wallet_creates_missing_wallet_dir(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    wallet_dir = Path(settings.resolved_oracle_wallet_dir)
    captured: dict[str, Any] = {}

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return _adb_info(adb_ocid, is_dedicated=False)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            captured.update(
                adb_ocid=adb_ocid,
                password=password,
                generate_type=generate_type,
                max_bytes=max_bytes,
            )
            return _wallet_zip_bytes()

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    assert not wallet_dir.exists()

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "downloaded"
    assert data["settings"]["wallet_dir"] == str(wallet_dir)
    assert data["settings"]["wallet_uploaded"] is True
    assert data["settings"]["has_wallet_password"] is True
    assert captured["adb_ocid"] == settings.oracle_adb_ocid
    assert captured["password"] == settings.oracle_password
    assert settings.oracle_wallet_password == settings.oracle_password
    assert str(captured["password"]) not in resp.text
    assert (
        dotenv_values(tmp_path / ".env")["PLATFORM_ORACLE_WALLET_PASSWORD"]
        == settings.oracle_password
    )
    assert (wallet_dir / "tnsnames.ora").is_file()
    assert stat.S_IMODE(wallet_dir.stat().st_mode) == 0o700
    for file_name in shared_database.database_wallet_required_files(settings.oracle_driver_mode):
        assert stat.S_IMODE((wallet_dir / file_name).stat().st_mode) == 0o600


def test_download_database_wallet_repairs_partial_serverless_wallet(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    wallet_dir = Path(settings.resolved_oracle_wallet_dir)
    wallet_dir.mkdir(parents=True)
    (wallet_dir / "tnsnames.ora").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(settings, "oracle_password", "database-secret")
    monkeypatch.setattr(settings, "oracle_wallet_password", "saved-wallet-secret")
    previous_wallet_password = settings.oracle_wallet_password
    captured: dict[str, Any] = {}

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            captured["get_ocid"] = adb_ocid
            return _adb_info(adb_ocid, is_dedicated=False)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            captured.update(
                adb_ocid=adb_ocid,
                password=password,
                generate_type=generate_type,
                max_bytes=max_bytes,
            )
            return _wallet_zip_bytes()

    pool_closed: list[bool] = []
    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)
    monkeypatch.setattr(settings_router, "close_oracle_pool", lambda: pool_closed.append(True))

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "downloaded"
    assert body["data"]["settings"]["wallet_uploaded"] is True
    assert body["data"]["settings"]["available_services"] == ["mydb_high"]
    assert captured["get_ocid"] == settings.oracle_adb_ocid
    assert captured["generate_type"] == "SINGLE"
    assert captured["max_bytes"] == shared_database.ORACLE_WALLET_MAX_BYTES
    password = str(captured["password"])
    assert password == previous_wallet_password
    assert password != settings.oracle_password
    assert settings.oracle_wallet_password == previous_wallet_password
    assert (
        dotenv_values(tmp_path / ".env")["PLATFORM_ORACLE_WALLET_PASSWORD"]
        == previous_wallet_password
    )
    assert password not in resp.text
    assert pool_closed == [True]
    assert stat.S_IMODE(wallet_dir.stat().st_mode) == 0o700
    for file_name in shared_database.database_wallet_required_files(settings.oracle_driver_mode):
        assert stat.S_IMODE((wallet_dir / file_name).stat().st_mode) == 0o600


def test_download_database_wallet_omits_generate_type_for_dedicated(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_download(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return _adb_info(adb_ocid, is_dedicated=True)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            captured["generate_type"] = generate_type
            return _wallet_zip_bytes()

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 200
    assert captured["generate_type"] is None


def test_download_database_wallet_is_idempotent_when_complete(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    shared_database.install_database_wallet(settings, _wallet_zip_bytes(), "wallet.zip")

    class UnexpectedDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            raise AssertionError("complete Wallet must not call OCI")

    monkeypatch.setattr(shared_database, "OciDatabaseClient", UnexpectedDatabaseClient)

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "already_configured"


def test_download_database_wallet_refreshes_when_wallet_password_is_invalid(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    shared_database.install_database_wallet(settings, _wallet_zip_bytes(), "wallet.zip")
    monkeypatch.setattr(settings, "oracle_wallet_password", "wrong-wallet-password")
    captured: dict[str, Any] = {}

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return _adb_info(adb_ocid, is_dedicated=False)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            captured.update(
                adb_ocid=adb_ocid,
                password=password,
                generate_type=generate_type,
                max_bytes=max_bytes,
            )
            return _wallet_zip_bytes()

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)
    monkeypatch.setattr(
        shared_database,
        "_database_wallet_password_is_usable",
        lambda *_, **__: False,
    )

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "downloaded"
    assert settings.oracle_wallet_password == captured["password"]
    assert str(captured["password"]) not in resp.text


def test_download_database_wallet_requires_password_when_db_and_wallet_password_empty(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 422
    assert "DB パスワード" in resp.text


def test_download_database_wallet_requires_adb_ocid(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "oracle_adb_ocid", "")

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 422
    assert "ADB OCID" in resp.text
    assert "手動アップロード" in resp.text


def test_download_database_wallet_requires_complete_oci_configuration(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "oci_config_file", str(tmp_path / "missing-config"))

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 422
    assert "OCI 認証設定" in resp.text
    assert "手動アップロード" in resp.text


def test_download_database_wallet_maps_oci_error_without_leaking_details(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_download(monkeypatch, tmp_path)

    class FailingDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            raise RuntimeError("SDK secret detail must not leak")

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FailingDatabaseClient)

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 502
    assert "IAM 権限" in resp.text
    assert "SDK secret detail" not in resp.text


def test_download_database_wallet_rejects_invalid_upstream_zip(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_download(monkeypatch, tmp_path)

    class InvalidWalletClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return _adb_info(adb_ocid, is_dedicated=False)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            return b"not-a-wallet-zip"

    monkeypatch.setattr(shared_database, "OciDatabaseClient", InvalidWalletClient)

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 502
    assert "内容を検証できませんでした" in resp.text


def test_download_database_wallet_maps_stream_limit_to_413(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_wallet_download(monkeypatch, tmp_path)

    class OversizedWalletClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return _adb_info(adb_ocid, is_dedicated=False)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            raise WalletDownloadTooLargeError

    monkeypatch.setattr(shared_database, "OciDatabaseClient", OversizedWalletClient)

    resp = client.post("/api/settings/database/wallet/download")

    assert resp.status_code == 413
    assert "20 MB" in resp.text


def test_database_wallet_install_lock_returns_conflict(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)

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
    _configure_wallet_download(monkeypatch, tmp_path)
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
    settings = _configure_wallet_download(monkeypatch, tmp_path)
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


def test_database_wallet_config_persist_failure_rolls_back_existing_wallet(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
    wallet_dir = shared_database.install_database_wallet(
        settings,
        _wallet_zip_bytes(),
        "old-wallet.zip",
    )
    old_tnsnames = (wallet_dir / "tnsnames.ora").read_bytes()
    (wallet_dir / "ewallet.pem").unlink()

    def fail_persist(_settings: Settings, _env_file: Path) -> None:
        raise HTTPException(
            status_code=500,
            detail="/secret/backend/.env contains SuperSecret123",
        )

    monkeypatch.setattr(shared_database, "_persist_database_settings", fail_persist)

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return _adb_info(adb_ocid, is_dedicated=False)

        async def download_autonomous_database_wallet(
            self,
            adb_ocid: str,
            password: str,
            generate_type: str | None,
            max_bytes: int,
        ) -> bytes:
            return _wallet_zip_bytes()

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.post(
        "/api/settings/database/wallet/download",
        headers={"X-Request-ID": "wallet-persist-request"},
    )

    assert resp.status_code == 500
    assert resp.json()["error_code"] == "WALLET_INSTALL_FAILED"
    assert resp.json()["problem"]["request_id"] == "wallet-persist-request"
    assert "変更前の状態" in resp.text
    assert "/secret/backend/.env" not in resp.text
    assert "SuperSecret123" not in resp.text
    assert (wallet_dir / "tnsnames.ora").read_bytes() == old_tnsnames
    assert not list(wallet_dir.parent.glob(f".{wallet_dir.name}.backup-*"))


def test_upload_database_wallet_rejects_zip_slip_without_overwriting(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _configure_wallet_download(monkeypatch, tmp_path)
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
    _configure_wallet_download(monkeypatch, tmp_path)
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
    _configure_wallet_download(monkeypatch, tmp_path)
    monkeypatch.setattr(shared_database, "ORACLE_WALLET_MAX_EXTRACTED_BYTES", 8)

    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("wallet.zip", _wallet_zip_bytes(), "application/zip")},
    )

    assert resp.status_code == 413
    assert "展開後サイズ" in resp.text


def test_oci_database_wallet_streaming_applies_limit_and_model_parameters() -> None:
    captured: list[dict[str, str]] = []

    class FakeRaw:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks
            self.closed = False

        def stream(self, chunk_size: int, *, decode_content: bool) -> list[bytes]:
            assert chunk_size == 1024 * 1024
            assert decode_content is True
            return self.chunks

        def close(self) -> None:
            self.closed = True

    class FakeSdkClient:
        def __init__(self, raw: FakeRaw) -> None:
            self.raw = raw

        def generate_autonomous_database_wallet(self, adb_ocid: str, details: Any) -> object:
            assert adb_ocid == "ocid1.autonomousdatabase.oc1..example"
            captured.append(details)
            return SimpleNamespace(data=SimpleNamespace(raw=self.raw))

    async def inline_runner(operation: Any) -> Any:
        return operation()

    factory = lambda **kwargs: kwargs  # noqa: E731 - SDK model factory の最小 fixture
    raw = FakeRaw([b"abc", b"def"])
    sdk_client = FakeSdkClient(raw)
    database_client = OciDatabaseClient(
        settings=Settings(_env_file=None),
        database_client=sdk_client,  # type: ignore[arg-type]
        sdk_call_runner=inline_runner,
        wallet_details_factory=factory,
    )

    result = asyncio.run(
        database_client.download_autonomous_database_wallet(
            "ocid1.autonomousdatabase.oc1..example",
            "temporary-secret",
            "SINGLE",
            6,
        )
    )

    assert result == b"abcdef"
    assert captured == [{"password": "temporary-secret", "generate_type": "SINGLE"}]
    assert raw.closed is True

    dedicated_raw = FakeRaw([b"1234", b"5"])
    dedicated_client = OciDatabaseClient(
        settings=Settings(_env_file=None),
        database_client=FakeSdkClient(dedicated_raw),  # type: ignore[arg-type]
        sdk_call_runner=inline_runner,
        wallet_details_factory=factory,
    )
    with pytest.raises(WalletDownloadTooLargeError):
        asyncio.run(
            dedicated_client.download_autonomous_database_wallet(
                "ocid1.autonomousdatabase.oc1..example",
                "another-secret",
                None,
                4,
            )
        )
    assert captured[-1] == {"password": "another-secret"}
    assert dedicated_raw.closed is True


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
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", False)

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


def test_database_connection_test_rejects_deepsec_thick_driver_mode(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("mydb_high = (...)\n", encoding="utf-8")
    (wallet_dir / "sqlnet.ora").write_text(
        "WALLET_LOCATION=(SOURCE=(METHOD=file))\n",
        encoding="utf-8",
    )
    (wallet_dir / "cwallet.sso").write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(settings, "oracle_driver_mode", "thick")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", str(tmp_path / "instantclient"))
    monkeypatch.setattr(settings, "oracle_wallet_dir", str(wallet_dir))
    monkeypatch.setattr(settings, "oracle_user", "ADMIN")
    monkeypatch.setattr(settings, "oracle_password", "database-secret")
    monkeypatch.setattr(settings, "oracle_dsn", "mydb_high")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", True)

    async def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("DeepSec+thick must not reach python-oracledb")

    monkeypatch.setattr(settings_router, "test_oracle_connection", fail_if_called)

    resp = client.post(
        "/api/settings/database/test",
        json={"user": "ADMIN", "dsn": "mydb_high", "wallet_dir": str(wallet_dir)},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["readiness"] == "invalid_configuration"
    assert "Thin mode" in data["message"]
    assert any("PLATFORM_ORACLE_DRIVER_MODE=thin" in tip for tip in data["troubleshooting"])


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


def test_update_upload_storage_persists_env_and_keeps_namespace(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "object_storage_region", "ap-osaka-1")
    monkeypatch.setattr(settings, "object_storage_namespace", "existingnamespace")

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-nl2sql",
            "object_storage_bucket": "rag-uploads",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["object_storage_namespace"] == "existingnamespace"
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_UPLOAD_STORAGE_BACKEND=oci" in env_text
    assert "PLATFORM_LOCAL_STORAGE_DIR=/u01/data/production-ready-nl2sql" in env_text
    assert "PLATFORM_OBJECT_STORAGE_REGION=ap-osaka-1" in env_text
    assert "PLATFORM_OBJECT_STORAGE_NAMESPACE=existingnamespace" in env_text
    assert "PLATFORM_OBJECT_STORAGE_BUCKET=rag-uploads" in env_text


def test_update_upload_storage_persists_payload_region(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "object_storage_region", "")
    monkeypatch.setattr(settings, "object_storage_namespace", "")

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-nl2sql",
            "object_storage_region": "ap-osaka-1",
            "object_storage_namespace": "existingnamespace",
            "object_storage_bucket": "nl2sql-originals",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["object_storage_region"] == "ap-osaka-1"
    assert data["object_storage_namespace"] == "existingnamespace"
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_OBJECT_STORAGE_REGION=ap-osaka-1" in env_text
    assert "PLATFORM_OBJECT_STORAGE_NAMESPACE=existingnamespace" in env_text


def test_update_upload_storage_rejects_incomplete_oci_settings(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "object_storage_region", "")
    monkeypatch.setattr(settings, "object_storage_namespace", "")

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-nl2sql",
            "object_storage_bucket": "nl2sql-originals",
        },
    )

    assert resp.status_code == 422
    assert "OCI Object Storage 設定が不足しています" in resp.text
    assert not env_file.exists()


def test_upload_storage_defaults_use_nl2sql_names(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("PLATFORM_LOCAL_STORAGE_DIR", raising=False)
    monkeypatch.delenv("PLATFORM_OBJECT_STORAGE_BUCKET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.local_storage_dir == "/u01/data/production-ready-nl2sql"
    # バケットは 3 製品で共通の既定名（#283）。
    assert settings.object_storage_bucket == "production-ready"


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
    assert document["version"] == 7
    assert "api_key" not in document["enterprise_ai"]
    assert document["enterprise_ai"]["models"][1]["model_id"] == "mistral.vision-model"
    assert document["enterprise_ai"]["default_text_model_id"] == "cohere.command-r-plus"
    assert document["enterprise_ai"]["default_vision_model_id"] == "mistral.vision-model"
    assert "generative_ai" not in document
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
    # #45 より前の `generative_ai` の節の旧 OCI Generative AI（Cohere）のモデルは、OpenAI 互換の
    # 経路で呼べないので登録モデルへ移さない（画面で選び直す）。
    assert [(model.model_id, model.kind) for model in settings.oci_enterprise_ai_models] == [
        ("cohere.command-r-plus", "text"),
        ("mistral.vision-model", "text"),
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


def test_oci_config_profile_defaults_to_default_profile() -> None:
    """PLATFORM_OCI_CONFIG_PROFILE だけを読む（旧 OCI_PROFILE の互換は持たない。#211）。"""
    canonical = Settings(_env_file=None, oci_config_profile="CANONICAL")
    empty = Settings(_env_file=None, oci_config_profile="")

    assert canonical.resolved_oci_config_profile == "CANONICAL"
    assert empty.resolved_oci_config_profile == "DEFAULT"
    assert "oci_profile" not in Settings.model_fields


def test_settings_read_platform_env_then_product_env(tmp_path: Path) -> None:
    """共通キーは共通 .env、製品キーは backend/.env から読む（#211）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text(
        "PLATFORM_ORACLE_DSN=shared_high\nPLATFORM_OCI_CONFIG_PROFILE=SHARED\n",
        encoding="utf-8",
    )
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "NL2SQL_LOG_LEVEL=WARNING\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=PRODUCT_DATA_USER\n"
        "NL2SQL_SELECT_AI_REGION=ap-osaka-1\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=(platform_env, backend_env))

    assert settings.oracle_dsn == "shared_high"
    assert settings.oci_config_profile == "SHARED"
    assert settings.log_level == "WARNING"
    assert settings.oracle_deepsec_data_user == "PRODUCT_DATA_USER"
    assert settings.nl2sql_select_ai_region == "ap-osaka-1"


def test_settings_ignore_legacy_env_names(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新名がないとき、旧名（接頭辞なし）を環境変数からも .env からも読まない（#211。互換なし）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("OCI_REGION=legacy-region\n", encoding="utf-8")
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("LOG_LEVEL=WARN\nORACLE_DEEPSEC_DATA_USER=LEGACY\n", encoding="utf-8")
    monkeypatch.delenv("PLATFORM_ORACLE_DSN", raising=False)
    monkeypatch.setenv("ORACLE_DSN", "legacy_high")
    monkeypatch.setenv("OCI_COMPARTMENT_ID", "legacy-compartment")

    settings = Settings(_env_file=(platform_env, backend_env))

    assert settings.oracle_dsn == ""
    assert settings.oci_compartment_id == ""
    assert settings.oci_region == ""
    assert settings.log_level == "INFO"
    assert settings.oracle_deepsec_data_user == "DEEPSEC_DATA_USER"
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


def test_adb_start_uses_oci_database_client(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_adb_ocid", "ocid1.autonomousdatabase.oc1..example")
    monkeypatch.setattr(settings, "oracle_adb_region", "ap-osaka-1")
    calls: list[tuple[str, str]] = []

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            calls.append(("get", adb_ocid))
            return AutonomousDatabaseInfo(
                id=adb_ocid,
                display_name="NL2SQLADB",
                lifecycle_state="STOPPED",
                db_name="NL2SQL",
                cpu_core_count=1,
                data_storage_size_in_tbs=1,
            )

        async def start_autonomous_database(self, adb_ocid: str) -> None:
            calls.append(("start", adb_ocid))

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.post("/api/settings/database/adb/start")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "accepted"
    assert data["lifecycle_state"] == "STARTING"
    assert data["region"] == "ap-osaka-1"
    assert calls == [
        ("get", "ocid1.autonomousdatabase.oc1..example"),
        ("start", "ocid1.autonomousdatabase.oc1..example"),
    ]


def test_adb_info_failure_returns_safe_error_code(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_adb_ocid", "ocid1.autonomousdatabase.oc1..example")
    monkeypatch.setattr(settings, "oracle_adb_region", "ap-osaka-1")

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            raise RuntimeError(_raw_adb_oci_failure())

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.get("/api/settings/database/adb")

    assert resp.status_code == 200
    data = resp.json()["data"]
    _assert_safe_adb_failure(data, error_code="ADB_INFO_UNAVAILABLE")
    assert data["message"].startswith("ADB 情報を取得できませんでした。")
    assert data["region"] == "ap-osaka-1"


def test_adb_start_failure_returns_safe_error_code(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_adb_ocid", "ocid1.autonomousdatabase.oc1..example")
    monkeypatch.setattr(settings, "oracle_adb_region", "ap-osaka-1")

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return AutonomousDatabaseInfo(
                id=adb_ocid,
                display_name="NL2SQLADB",
                lifecycle_state="STOPPED",
                db_name="NL2SQL",
                cpu_core_count=1,
                data_storage_size_in_tbs=1,
            )

        async def start_autonomous_database(self, adb_ocid: str) -> None:
            raise RuntimeError(_raw_adb_oci_failure())

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.post("/api/settings/database/adb/start")

    assert resp.status_code == 200
    data = resp.json()["data"]
    _assert_safe_adb_failure(data, error_code="ADB_START_FAILED")
    assert data["message"].startswith("ADB の起動を開始できませんでした。")
    assert data["lifecycle_state"] == "STOPPED"


def test_adb_stop_failure_returns_safe_error_code(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_adb_ocid", "ocid1.autonomousdatabase.oc1..example")
    monkeypatch.setattr(settings, "oracle_adb_region", "ap-osaka-1")

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            return AutonomousDatabaseInfo(
                id=adb_ocid,
                display_name="NL2SQLADB",
                lifecycle_state="AVAILABLE",
                db_name="NL2SQL",
                cpu_core_count=1,
                data_storage_size_in_tbs=1,
            )

        async def stop_autonomous_database(self, adb_ocid: str) -> None:
            raise RuntimeError(_raw_adb_oci_failure())

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.post("/api/settings/database/adb/stop")

    assert resp.status_code == 200
    data = resp.json()["data"]
    _assert_safe_adb_failure(data, error_code="ADB_STOP_FAILED")
    assert data["message"].startswith("ADB の停止を開始できませんでした。")
    assert data["lifecycle_state"] == "AVAILABLE"


def test_adb_info_not_configured_returns_safe_code(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_adb_ocid", "")
    monkeypatch.setattr(settings, "oracle_adb_region", "ap-osaka-1")

    resp = client.get("/api/settings/database/adb")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "not_configured"
    assert data["error_code"] == "ADB_NOT_CONFIGURED"
    assert data["message"] == "ADB OCID が設定されていません。"
    assert data["id"] is None


def test_update_adb_settings_persists_dedicated_region(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "oracle_adb_ocid", "")
    monkeypatch.setattr(settings, "oracle_adb_region", "")
    calls: list[str] = []

    class FakeDatabaseClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
            calls.append(adb_ocid)
            return AutonomousDatabaseInfo(
                id=adb_ocid,
                display_name="NL2SQLADB",
                lifecycle_state="STOPPED",
                db_name="NL2SQL",
                cpu_core_count=1,
                data_storage_size_in_tbs=1,
            )

    monkeypatch.setattr(shared_database, "OciDatabaseClient", FakeDatabaseClient)

    resp = client.post(
        "/api/settings/database/adb/settings",
        json={"adb_ocid": "ocid1.autonomousdatabase.oc1..saved", "region": "ap-tokyo-1"},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["region"] == "ap-tokyo-1"
    assert settings.oracle_adb_ocid == "ocid1.autonomousdatabase.oc1..saved"
    assert settings.oracle_adb_region == "ap-tokyo-1"
    env_text = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_ORACLE_ADB_OCID=ocid1.autonomousdatabase.oc1..saved" in env_text
    assert "PLATFORM_ORACLE_ADB_REGION=ap-tokyo-1" in env_text
    assert "PLATFORM_OCI_REGION=ap-tokyo-1" not in env_text
    assert calls == ["ocid1.autonomousdatabase.oc1..saved"]


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


def _configure_wallet_download(monkeypatch: MonkeyPatch, tmp_path: Path) -> Settings:
    settings = get_settings()
    wallet_dir = tmp_path / "wallet"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", tmp_path / ".env")
    key_file = tmp_path / "oci_api_key.pem"
    key_file.write_text(
        "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "oci_config"
    config_file.write_text(
        "[DEFAULT]\n"
        "user=ocid1.user.oc1..example\n"
        "fingerprint=aa:bb:cc\n"
        "tenancy=ocid1.tenancy.oc1..example\n"
        "region=ap-osaka-1\n"
        f"key_file={key_file}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "oracle_driver_mode", "thin")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", str(wallet_dir))
    monkeypatch.setattr(settings, "oracle_password", "database-secret")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")
    monkeypatch.setattr(settings, "oracle_adb_ocid", "ocid1.autonomousdatabase.oc1..example")
    monkeypatch.setattr(settings, "oracle_adb_region", "ap-osaka-1")
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    return settings


def _adb_info(adb_ocid: str, *, is_dedicated: bool) -> AutonomousDatabaseInfo:
    return AutonomousDatabaseInfo(
        id=adb_ocid,
        display_name="NL2SQLADB",
        lifecycle_state="AVAILABLE",
        db_name="NL2SQL",
        cpu_core_count=1,
        data_storage_size_in_tbs=1,
        is_dedicated=is_dedicated,
    )


def _raw_adb_oci_failure() -> str:
    return (
        "ServiceError: GET https://database.ap-osaka-1.oraclecloud.com/20160918/"
        "autonomousDatabases/ocid1.autonomousdatabase.oc1..leaked failed "
        "request target=/adb secret=raw-sdk-detail"
    )


def _assert_safe_adb_failure(data: dict[str, Any], *, error_code: str) -> None:
    assert data["status"] == "error"
    assert data["error_code"] == error_code
    message = data["message"]
    assert "ServiceError" not in message
    assert "https://" not in message
    assert "ocid1.autonomousdatabase.oc1..leaked" not in message
    assert "raw-sdk-detail" not in message


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


@pytest.mark.parametrize("target", ["object_storage", "adb"])
def test_failed_settings_persistence_preserves_runtime(
    monkeypatch: MonkeyPatch, target: str
) -> None:
    settings = get_settings()
    before = settings.model_dump()

    def fail_persist(candidate: Settings, *_args: object) -> None:
        assert candidate is not settings
        raise HTTPException(status_code=500, detail="設定を保存できませんでした。")

    if target == "object_storage":
        monkeypatch.setattr(shared_oci, "_persist_oci_object_storage_settings", fail_persist)
        response = client.patch(
            "/api/settings/oci/object-storage",
            json={
                "object_storage_region": "ap-tokyo-1",
                "object_storage_namespace": "newnamespace",
            },
        )
    else:
        monkeypatch.setattr(shared_database, "_persist_adb_settings", fail_persist)
        response = client.post(
            "/api/settings/database/adb/settings",
            json={"adb_ocid": "ocid1.autonomousdatabase.oc1..changed", "region": "ap-tokyo-1"},
        )
    assert response.status_code == 500
    assert settings.model_dump() == before


class _MissingBucketError(Exception):
    """OCI SDK の ServiceError と同じ属性を持つ（HeadBucket の 404 BucketNotFound）。"""

    status = 404
    code = "BucketNotFound"
    request_id = "req-test"


class _MissingBucketStorage:
    def head_bucket(self, *_args: object, **_kwargs: object) -> None:
        raise _MissingBucketError("bucket not found")


def _stub_missing_bucket(monkeypatch: MonkeyPatch) -> None:
    from pr_system_settings import upload_storage_check

    monkeypatch.setattr(
        upload_storage_check,
        "default_object_storage_client",
        lambda _settings: _MissingBucketStorage(),
    )


def test_update_upload_storage_rejects_missing_bucket_without_saving(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """存在しない bucket は、保存せずに理由と直し方を返す（#2101）。"""
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    _stub_missing_bucket(monkeypatch)
    before = settings.upload_storage_backend

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/check",
            "object_storage_region": "us-chicago-1",
            "object_storage_namespace": "examplenamespace",
            "object_storage_bucket": "missing-bucket",
        },
    )

    assert resp.status_code == 422
    body = resp.json()
    detail = json.dumps(body, ensure_ascii=False)  # 製品ごとのエラーの形に依らない
    assert "保存していません" in detail
    assert "404 BucketNotFound" in detail
    assert "missing-bucket" not in detail
    assert settings.upload_storage_backend == before
    assert not env_file.exists() or "missing-bucket" not in env_file.read_text(encoding="utf-8")


def test_upload_storage_check_reports_stages_without_saving(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """「接続を確認」は入力中の値で段階ごとの結果を返し、保存しない（#2101）。"""
    settings = get_settings()
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", env_file)
    _stub_missing_bucket(monkeypatch)

    resp = client.post(
        "/api/settings/upload-storage/test",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/check",
            "object_storage_region": "us-chicago-1",
            "object_storage_namespace": "examplenamespace",
            "object_storage_bucket": "missing-bucket",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert [stage["status"] for stage in data["stages"]] == [
        "success",
        "failed",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert not env_file.exists() or "missing-bucket" not in env_file.read_text(encoding="utf-8")
    assert settings.object_storage_bucket != "missing-bucket"
