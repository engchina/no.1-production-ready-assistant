from __future__ import annotations

import configparser
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings import oci as shared_oci
from pr_system_settings import oci_connectivity
from pr_system_settings.oci import build_oci_router


class FakeSettings(BaseModel):
    """製品の Settings のうち OCI 設定 API が読む属性だけを持つ。"""

    oci_config_file: str = "~/.oci/config"
    oci_config_profile: str = "DEFAULT"
    oci_region: str = ""
    upload_storage_backend: str = "local"
    local_storage_dir: str = "/u01/data/production-ready-test"
    object_storage_region: str = ""
    object_storage_namespace: str = ""
    object_storage_bucket: str = ""
    max_upload_bytes: int = 1024


@pytest.fixture
def oci_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "oci"
    monkeypatch.setattr(shared_oci, "OCI_PRIVATE_KEY_FILE", str(home / "oci_api_key.pem"))
    return home


def make_client(settings: FakeSettings, env_file: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_oci_router(get_settings=lambda: settings, env_file=lambda: env_file),
        prefix="/api/settings",
    )
    return TestClient(app)


def test_patch_oci_writes_config_keeps_other_profiles_and_env(
    oci_home: Path, tmp_path: Path
) -> None:
    config = oci_home / "config"
    oci_home.mkdir()
    config.write_text("[OTHER]\nuser=ocid1.user.oc1..other\n", encoding="utf-8")
    settings = FakeSettings(oci_config_file=str(config))
    env_file = tmp_path / ".env"

    response = make_client(settings, env_file).patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..aaaa",
            "fingerprint": "ab:cd:ef",
            "tenancy": "ocid1.tenancy.oc1..bbbb",
            "region": "ap-osaka-1",
        },
    )

    assert response.status_code == 200
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config, encoding="utf-8")
    assert parser["DEFAULT"]["user"] == "ocid1.user.oc1..aaaa"
    assert parser["DEFAULT"]["key_file"] == str(oci_home / "oci_api_key.pem")
    assert parser["OTHER"]["user"] == "ocid1.user.oc1..other"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(oci_home.stat().st_mode) == 0o700
    env_text = env_file.read_text(encoding="utf-8")
    assert f"PLATFORM_OCI_CONFIG_FILE={config}" in env_text
    assert "PLATFORM_OCI_CONFIG_PROFILE=DEFAULT" in env_text
    assert "PLATFORM_OCI_REGION=ap-osaka-1" in env_text
    assert settings.oci_region == "ap-osaka-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"user": "not-an-ocid"},
        {"tenancy": "ocid1.user.oc1..x"},
        {"fingerprint": "zz"},
        {"region": "AP_OSAKA\nuser=x"},
    ],
)
def test_patch_oci_rejects_malformed_values_without_writing(
    oci_home: Path, tmp_path: Path, payload: dict[str, str]
) -> None:
    settings = FakeSettings(oci_config_file=str(oci_home / "config"))
    response = make_client(settings, tmp_path / ".env").patch("/api/settings/oci", json=payload)
    assert response.status_code == 422
    assert not (oci_home / "config").exists()


def test_object_storage_persist_failure_keeps_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(shared_oci, "write_env_values", fail)
    settings = FakeSettings(object_storage_region="ap-tokyo-1", object_storage_namespace="old")
    response = make_client(settings, tmp_path / ".env").patch(
        "/api/settings/oci/object-storage",
        json={"object_storage_region": "ap-osaka-1", "object_storage_namespace": "new"},
    )
    assert response.status_code == 500
    assert settings.object_storage_region == "ap-tokyo-1"
    assert settings.object_storage_namespace == "old"


def test_upload_private_key_saves_fixed_path_with_0600(oci_home: Path, tmp_path: Path) -> None:
    pem = b"-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
    response = make_client(FakeSettings(), tmp_path / ".env").post(
        "/api/settings/oci/key-file",
        files={"file": ("my.pem", pem, "application/x-pem-file")},
    )
    assert response.status_code == 200
    key = oci_home / "oci_api_key.pem"
    assert key.read_bytes() == pem
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("name", "content", "status"),
    [
        ("key.txt", b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n", 415),
        ("key.pem", b"", 400),
        ("key.pem", b"not a pem", 400),
        ("key.pem", b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n", 400),
        ("key.pem", b"x" * (64 * 1024 + 1), 413),
    ],
)
def test_upload_private_key_rejects_unusable_files(
    oci_home: Path, tmp_path: Path, name: str, content: bytes, status: int
) -> None:
    response = make_client(FakeSettings(), tmp_path / ".env").post(
        "/api/settings/oci/key-file", files={"file": (name, content, "application/octet-stream")}
    )
    assert response.status_code == status
    assert not (oci_home / "oci_api_key.pem").exists()


def test_static_config_test_does_not_call_oci(
    oci_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(oci_connectivity, "_get_object_storage_namespace", calls.append)
    result = shared_oci.test_oci_config(
        FakeSettings(oci_config_file=str(oci_home / "missing")), verify_with_oci=False
    )
    assert result.status == "failed"
    assert calls == []


def test_get_oci_reports_unreadable_config_instead_of_empty_values(
    oci_home: Path, tmp_path: Path
) -> None:
    """config はあるが読めないとき、空の値を「未設定」として返さず理由を返す（#1067）。"""
    oci_home.mkdir()
    config = oci_home / "config"
    config.write_text("user=ocid1.user.oc1..no-section\n", encoding="utf-8")
    settings = FakeSettings(oci_config_file=str(config))

    data = make_client(settings, tmp_path / ".env").get("/api/settings/oci").json()["data"]

    assert data["user"] == ""
    assert data["config_file_exists"] is True
    assert data["config_error"] == "OCI config ファイルの形式を確認してください。"


@pytest.mark.parametrize(
    ("content", "profile"),
    [
        (None, "DEFAULT"),
        ("[DEFAULT]\nuser=ocid1.user.oc1..a\n", "RAG_PROD"),
        ("[OTHER]\nuser=ocid1.user.oc1..a\n", "DEFAULT"),
    ],
    ids=["no-file", "no-profile", "empty-profile"],
)
def test_get_oci_treats_missing_config_or_profile_as_unset(
    oci_home: Path, tmp_path: Path, content: str | None, profile: str
) -> None:
    oci_home.mkdir()
    config = oci_home / "config"
    if content is not None:
        config.write_text(content, encoding="utf-8")
    settings = FakeSettings(oci_config_file=str(config), oci_config_profile=profile)

    data = make_client(settings, tmp_path / ".env").get("/api/settings/oci").json()["data"]

    assert data["config_error"] is None


def test_read_oci_config_uses_runtime_config_not_requested_path(
    oci_home: Path, tmp_path: Path
) -> None:
    """要求の config_file / profile でサーバーの別のファイルを読ませない（#1067）。"""
    oci_home.mkdir()
    config = oci_home / "config"
    config.write_text("[DEFAULT]\nuser=ocid1.user.oc1..runtime\n", encoding="utf-8")
    other = tmp_path / "other.ini"
    other.write_text("[DEFAULT]\nuser=ocid1.user.oc1..other\n", encoding="utf-8")
    settings = FakeSettings(oci_config_file=str(config))

    response = make_client(settings, tmp_path / ".env").post(
        "/api/settings/oci/config/read",
        json={"config_file": str(other), "profile": "OTHER"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["user"] == "ocid1.user.oc1..runtime"
    assert response.json()["data"]["profile"] == "DEFAULT"


def test_read_namespace_uses_runtime_config_not_requested_path(
    oci_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        data = "runtime-ns"

    class FakeClient:
        def __init__(self, config: dict[str, object]) -> None:
            captured["config"] = config

        def get_namespace(self) -> FakeResponse:
            return FakeResponse()

    def from_file(path: str, profile: str) -> dict[str, object]:
        captured["path"] = path
        captured["profile"] = profile
        return {}

    def import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(from_file=from_file)
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=FakeClient)
        raise AssertionError(name)

    monkeypatch.setattr(shared_oci.importlib, "import_module", import_module)
    config = oci_home / "config"
    settings = FakeSettings(oci_config_file=str(config), oci_config_profile="RUNTIME")

    response = make_client(settings, tmp_path / ".env").post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "other.ini"),
            "profile": "OTHER",
            "region": "ap-osaka-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"namespace": "runtime-ns"}
    assert captured["path"] == str(config)
    assert captured["profile"] == "RUNTIME"
    assert captured["config"] == {"region": "ap-osaka-1"}
