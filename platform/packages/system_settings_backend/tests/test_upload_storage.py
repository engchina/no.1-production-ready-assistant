from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings import upload_storage
from pr_system_settings.env_file import write_env_values
from pr_system_settings.upload_storage import build_upload_storage_router


class FakeSettings(BaseModel):
    """製品の Settings と同じ属性だけを持つテスト用 model。"""

    local_storage_dir: str = "/u01/data/production-ready-test"
    max_upload_bytes: int = 200 * 1024 * 1024


def make_client(settings: FakeSettings, env_file: Path, *, deny_write: bool = False) -> TestClient:
    def forbid() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    app = FastAPI()
    app.include_router(
        build_upload_storage_router(
            get_settings=lambda: settings,
            env_file=lambda: env_file,
            write_dependencies=[Depends(forbid)] if deny_write else [],
        ),
        prefix="/api/settings",
    )
    return TestClient(app)


def test_get_returns_runtime_values(tmp_path: Path) -> None:
    data = (
        make_client(FakeSettings(), tmp_path / ".env")
        .get("/api/settings/upload-storage")
        .json()["data"]
    )
    assert data == {
        "local_storage_dir": "/u01/data/production-ready-test",
        "readiness": "ok",
        "max_upload_bytes": 200 * 1024 * 1024,
        "config_source": "runtime",
    }


def test_get_reports_missing_when_local_dir_is_empty(tmp_path: Path) -> None:
    data = (
        make_client(FakeSettings(local_storage_dir=""), tmp_path / ".env")
        .get("/api/settings/upload-storage")
        .json()["data"]
    )
    assert data["readiness"] == "missing"


def test_patch_persists_only_local_dir_and_mutates_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    # 旧版の OCI Object Storage の行は残して読まない（#11）。
    env_file.write_text(
        "# 既存\nPLATFORM_UPLOAD_STORAGE_BACKEND=oci\nPLATFORM_LOCAL_STORAGE_DIR=/old\n",
        encoding="utf-8",
    )
    env_file.chmod(0o640)
    settings = FakeSettings()

    response = make_client(settings, env_file).patch(
        "/api/settings/upload-storage", json={"local_storage_dir": " /u01/data/x "}
    )

    assert response.status_code == 200
    assert response.json()["data"]["readiness"] == "ok"
    assert settings.local_storage_dir == "/u01/data/x"
    content = env_file.read_text(encoding="utf-8")
    assert (
        content
        == "# 既存\nPLATFORM_UPLOAD_STORAGE_BACKEND=oci\nPLATFORM_LOCAL_STORAGE_DIR=/u01/data/x\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640


def test_patch_ignores_removed_object_storage_fields(tmp_path: Path) -> None:
    """旧画面が送る `backend` / `object_storage_*` は無視して、ローカルの値だけを保存する。"""
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    response = make_client(settings, env_file).patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/y",
            "object_storage_bucket": "b",
        },
    )
    assert response.status_code == 200
    assert settings.local_storage_dir == "/u01/data/y"
    assert env_file.read_text(encoding="utf-8").count("OBJECT_STORAGE") == 0


@pytest.mark.parametrize(
    "local_dir",
    ["/u01/data/x\nPLATFORM_OTHER=1", "/u01/data/\x00x", "/u01/data/x\ry"],
    ids=["newline", "nul", "cr"],
)
def test_patch_rejects_control_characters_in_local_storage_dir(
    tmp_path: Path, local_dir: str
) -> None:
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    response = make_client(settings, env_file).patch(
        "/api/settings/upload-storage", json={"local_storage_dir": local_dir}
    )
    assert response.status_code == 422
    assert settings.local_storage_dir == "/u01/data/production-ready-test"
    assert not env_file.exists()


def test_patch_keeps_runtime_when_env_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(upload_storage, "write_env_values", fail)
    settings = FakeSettings()
    client = make_client(settings, tmp_path / ".env")

    response = client.patch(
        "/api/settings/upload-storage", json={"local_storage_dir": "/u01/data/changed"}
    )

    assert response.status_code == 500
    assert settings.local_storage_dir == "/u01/data/production-ready-test"
    get_data = client.get("/api/settings/upload-storage").json()["data"]
    assert get_data["local_storage_dir"] == "/u01/data/production-ready-test"


def test_write_dependencies_apply_only_to_patch(tmp_path: Path) -> None:
    client = make_client(FakeSettings(), tmp_path / ".env", deny_write=True)
    assert client.get("/api/settings/upload-storage").status_code == 200
    assert client.patch("/api/settings/upload-storage", json={}).status_code == 403


def test_object_storage_check_endpoint_is_removed(tmp_path: Path) -> None:
    client = make_client(FakeSettings(), tmp_path / ".env")
    assert client.post("/api/settings/upload-storage/test", json={}).status_code in {404, 405}


def test_write_env_values_removes_none_and_quotes_special_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nB=2\nA=dup\n", encoding="utf-8")
    write_env_values(env_file, {"A": "a b", "B": None, "C": "c"}, section_comment="# 追加")
    assert env_file.read_text(encoding="utf-8") == 'A="a b"\n\n# 追加\nC=c\n'
