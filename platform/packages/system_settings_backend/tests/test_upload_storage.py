from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings import upload_storage, upload_storage_check
from pr_system_settings.env_file import write_env_values
from pr_system_settings.upload_storage import build_upload_storage_router
from pr_system_settings.upload_storage_check import CHECK_OBJECT_PREFIX, check_object_storage


class FakeSettings(BaseModel):
    """製品の Settings と同じ属性だけを持つテスト用 model。"""

    upload_storage_backend: str = "local"
    local_storage_dir: str = "/u01/data/production-ready-test"
    object_storage_region: str = ""
    object_storage_namespace: str = ""
    object_storage_bucket: str = ""
    oci_region: str = ""
    max_upload_bytes: int = 200 * 1024 * 1024


class FakeServiceError(Exception):
    """OCI SDK の ServiceError と同じ属性（status・code・request_id）を持つ。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code} secret-namespace secret-bucket")
        self.status = status
        self.code = code
        self.request_id = "req-1"


class FakeObjectStorage:
    """OCI Object Storage の client のスタブ（呼び出しを記録し、指定した操作で失敗する）。"""

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.fail = fail or {}
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.objects: dict[str, bytes] = {}

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    def head_bucket(self, namespace: str, bucket: str) -> object:
        self._record("head_bucket", namespace, bucket)
        return object()

    def put_object(
        self, namespace: str, bucket: str, name: str, body: bytes, **_kwargs: object
    ) -> object:
        self._record("put_object", namespace, bucket, name)
        self.objects[name] = body
        return object()

    def get_object(self, namespace: str, bucket: str, name: str) -> object:
        self._record("get_object", namespace, bucket, name)
        return SimpleNamespace(data=SimpleNamespace(content=self.objects[name]))

    def delete_object(self, namespace: str, bucket: str, name: str) -> object:
        self._record("delete_object", namespace, bucket, name)
        self.objects.pop(name, None)
        return object()


def make_client(
    settings: FakeSettings,
    env_file: Path,
    *,
    deny_write: bool = False,
    storage: FakeObjectStorage | None = None,
) -> TestClient:
    def forbid() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    fake = storage or FakeObjectStorage()
    app = FastAPI()
    app.include_router(
        build_upload_storage_router(
            get_settings=lambda: settings,
            env_file=lambda: env_file,
            write_dependencies=[Depends(forbid)] if deny_write else [],
            object_storage_client=lambda _settings: fake,
        ),
        prefix="/api/settings",
    )
    return TestClient(app)


def test_get_returns_runtime_values_and_falls_back_to_oci_region(tmp_path: Path) -> None:
    settings = FakeSettings(oci_region="ap-osaka-1")
    data = (
        make_client(settings, tmp_path / ".env").get("/api/settings/upload-storage").json()["data"]
    )
    assert data == {
        "backend": "local",
        "local_storage_dir": "/u01/data/production-ready-test",
        "object_storage_region": "ap-osaka-1",
        "object_storage_namespace": "",
        "object_storage_bucket": "",
        "readiness": "ok",
        "max_upload_bytes": 200 * 1024 * 1024,
        "config_source": "runtime",
        "storage_check": None,
    }


def test_patch_persists_only_upload_keys_and_mutates_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 既存\nPLATFORM_OCI_REGION=ap-osaka-1\nPLATFORM_LOCAL_STORAGE_DIR=/old\n",
        encoding="utf-8",
    )
    env_file.chmod(0o640)
    settings = FakeSettings(object_storage_namespace="ns-keep")

    response = make_client(settings, env_file).patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": " /u01/data/x ",
            "object_storage_region": "us-chicago-1",
            "object_storage_bucket": "originals",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["readiness"] == "ok"
    assert response.json()["data"]["storage_check"]["status"] == "success"
    # namespace を省略したので既存値を保つ
    assert settings.object_storage_namespace == "ns-keep"
    assert settings.upload_storage_backend == "oci"
    assert settings.local_storage_dir == "/u01/data/x"
    content = env_file.read_text(encoding="utf-8")
    assert (
        "# 既存\nPLATFORM_OCI_REGION=ap-osaka-1\nPLATFORM_LOCAL_STORAGE_DIR=/u01/data/x\n"
        in content
    )
    assert "PLATFORM_UPLOAD_STORAGE_BACKEND=oci" in content
    assert "PLATFORM_OBJECT_STORAGE_REGION=us-chicago-1" in content
    assert "PLATFORM_OBJECT_STORAGE_NAMESPACE=ns-keep" in content
    assert "PLATFORM_OBJECT_STORAGE_BUCKET=originals" in content
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640


def test_patch_rejects_incomplete_oci_settings_without_changes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    response = make_client(settings, env_file).patch(
        "/api/settings/upload-storage",
        json={"backend": "oci", "local_storage_dir": "/x", "object_storage_bucket": "b"},
    )
    assert response.status_code == 422
    assert "Object Storage リージョン" in response.json()["detail"]
    assert "Object Storage namespace" in response.json()["detail"]
    assert settings.upload_storage_backend == "local"
    assert not env_file.exists()


def test_patch_rejects_unsafe_object_storage_names(tmp_path: Path) -> None:
    response = make_client(FakeSettings(), tmp_path / ".env").patch(
        "/api/settings/upload-storage",
        json={"backend": "oci", "object_storage_bucket": "bad bucket;rm"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "region",
    [
        # OCI SDK は region を `https://objectstorage.{region}...` にそのまま入れるため、
        # 別の host へ署名付きの要求を送れてしまう（#1047）。
        "evil.example/x#",
        "ap-tokyo-1.evil.example",
        "AP-TOKYO-1",
        "ap tokyo 1",
    ],
    ids=["path", "dot", "upper", "space"],
)
def test_patch_rejects_unsafe_object_storage_region(tmp_path: Path, region: str) -> None:
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    response = make_client(settings, env_file).patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "object_storage_region": region,
            "object_storage_namespace": "ns",
            "object_storage_bucket": "b",
        },
    )
    assert response.status_code == 422
    assert settings.upload_storage_backend == "local"
    assert not env_file.exists()


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
        "/api/settings/upload-storage",
        json={"backend": "local", "local_storage_dir": local_dir},
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
        "/api/settings/upload-storage",
        json={"backend": "local", "local_storage_dir": "/u01/data/changed"},
    )

    assert response.status_code == 500
    assert settings.local_storage_dir == "/u01/data/production-ready-test"
    get_data = client.get("/api/settings/upload-storage").json()["data"]
    assert get_data["local_storage_dir"] == "/u01/data/production-ready-test"


def test_write_dependencies_apply_only_to_patch(tmp_path: Path) -> None:
    client = make_client(FakeSettings(), tmp_path / ".env", deny_write=True)
    assert client.get("/api/settings/upload-storage").status_code == 200
    assert (
        client.patch("/api/settings/upload-storage", json={"backend": "local"}).status_code == 403
    )
    # 確認も確認用の object を書くので、保存と同じ権限を要る（#2101）
    assert client.post("/api/settings/upload-storage/test", json=_OCI_PAYLOAD).status_code == 403


def test_write_env_values_removes_none_and_quotes_special_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nB=2\nA=dup\n", encoding="utf-8")
    write_env_values(env_file, {"A": "a b", "B": None, "C": "c"}, section_comment="# 追加")
    assert env_file.read_text(encoding="utf-8") == 'A="a b"\n\n# 追加\nC=c\n'


_OCI_PAYLOAD = {
    "backend": "oci",
    "object_storage_region": "us-chicago-1",
    "object_storage_namespace": "example-ns",
    "object_storage_bucket": "example-bucket",
}


def test_check_runs_bucket_write_read_delete_and_removes_check_object() -> None:
    """bucket → 書き込み → 読み取り → 削除の順に試し、確認用の object を残さない（#2101）。"""
    storage = FakeObjectStorage()
    settings = FakeSettings(
        upload_storage_backend="oci",
        object_storage_region="us-chicago-1",
        object_storage_namespace="example-ns",
        object_storage_bucket="example-bucket",
    )

    result = check_object_storage(settings, client_factory=lambda _settings: storage)

    assert result.status == "success"
    assert [stage.key for stage in result.stages] == ["config", "bucket", "write", "read", "delete"]
    assert {stage.status for stage in result.stages} == {"success"}
    assert [name for name, _ in storage.calls] == [
        "head_bucket",
        "put_object",
        "get_object",
        "delete_object",
    ]
    object_name = storage.calls[1][1][2]
    assert isinstance(object_name, str) and object_name.startswith(CHECK_OBJECT_PREFIX)
    assert storage.objects == {}


@pytest.mark.parametrize(
    ("failing", "status", "code", "stage", "hint"),
    [
        ("head_bucket", 404, "BucketNotFound", "bucket", "read buckets"),
        ("head_bucket", 401, "NotAuthenticated", "bucket", "OCI 認証"),
        ("put_object", 404, "BucketNotFound", "write", "manage objects"),
        ("get_object", 403, "NotAuthorized", "read", "read objects"),
        ("delete_object", 404, "NotAuthorizedOrNotFound", "delete", "manage objects"),
    ],
    ids=["no-bucket", "auth", "no-write", "no-read", "no-delete"],
)
def test_patch_rejects_bucket_or_permission_errors_without_saving(
    tmp_path: Path, failing: str, status: int, code: str, stage: str, hint: str
) -> None:
    """bucket が無い・権限が無い・認証の拒否は、保存せずに 422 で理由と直し方を返す（#2101）。"""
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    storage = FakeObjectStorage(fail={failing: FakeServiceError(status, code)})

    response = make_client(settings, env_file, storage=storage).patch(
        "/api/settings/upload-storage", json=_OCI_PAYLOAD
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "保存していません" in detail
    assert hint in detail
    assert f"{status} {code}" in detail
    # namespace・bucket の値と、例外の文は応答に出さない
    assert "example-ns" not in detail and "example-bucket" not in detail
    assert "secret-" not in detail
    assert settings.upload_storage_backend == "local"
    assert not env_file.exists()
    # 書き込んだ後に失敗したら、確認用の object を消す（削除の失敗では残る）
    if failing in {"get_object"}:
        assert storage.calls[-1][0] == "delete_object"
        assert storage.objects == {}
    result = check_object_storage(
        settings.model_copy(update={"upload_storage_backend": "oci"}),
        client_factory=lambda _settings: FakeObjectStorage(
            fail={failing: FakeServiceError(status, code)}
        ),
    )
    failed = next(item for item in result.stages if item.status == "failed")
    assert failed.key == stage
    assert result.retryable is False


@pytest.mark.parametrize(
    "error",
    [TimeoutError("read timed out"), FakeServiceError(503, "ServiceUnavailable")],
    ids=["timeout", "unavailable"],
)
def test_patch_saves_with_warning_when_check_is_temporarily_unavailable(
    tmp_path: Path, error: BaseException
) -> None:
    """接続できない・時間切れ・5xx で確かめられなかったときは保存し、失敗の結果を返す（#2101）。"""
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    storage = FakeObjectStorage(fail={"head_bucket": error})

    response = make_client(settings, env_file, storage=storage).patch(
        "/api/settings/upload-storage", json=_OCI_PAYLOAD
    )

    assert response.status_code == 200
    check = response.json()["data"]["storage_check"]
    assert check["status"] == "failed"
    assert check["retryable"] is True
    assert settings.upload_storage_backend == "oci"
    assert "PLATFORM_OBJECT_STORAGE_BUCKET=example-bucket" in env_file.read_text(encoding="utf-8")


def test_patch_to_local_does_not_call_object_storage(tmp_path: Path) -> None:
    storage = FakeObjectStorage(fail={"head_bucket": FakeServiceError(404, "BucketNotFound")})
    response = make_client(FakeSettings(), tmp_path / ".env", storage=storage).patch(
        "/api/settings/upload-storage",
        json={"backend": "local", "local_storage_dir": "/u01/data/x"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["storage_check"] is None
    assert storage.calls == []


def test_check_endpoint_tests_unsaved_values_without_saving(tmp_path: Path) -> None:
    """「接続を確認」は入力中の値で試し、保存しない（#2101）。"""
    env_file = tmp_path / ".env"
    settings = FakeSettings()
    storage = FakeObjectStorage(fail={"put_object": FakeServiceError(404, "BucketNotFound")})
    client = make_client(settings, env_file, storage=storage)

    response = client.post("/api/settings/upload-storage/test", json=_OCI_PAYLOAD)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert [(item["key"], item["status"]) for item in data["stages"]] == [
        ("config", "success"),
        ("bucket", "success"),
        ("write", "failed"),
        ("read", "skipped"),
        ("delete", "skipped"),
    ]
    assert storage.calls[0][1] == ("example-ns", "example-bucket")
    assert settings.upload_storage_backend == "local"
    assert not env_file.exists()
    # local は確かめない
    assert (
        client.post(
            "/api/settings/upload-storage/test",
            json={"backend": "local", "local_storage_dir": "/x"},
        ).status_code
        == 422
    )


def test_check_reports_unreadable_oci_config_as_config_stage() -> None:
    """OCI 認証の設定を読めないときは、OCI を呼ばずに config の段階で失敗する。"""

    def broken(_settings: object) -> object:
        raise FileNotFoundError("/home/user/.oci/config")

    result = check_object_storage(FakeSettings(), client_factory=broken)

    assert result.status == "failed"
    assert result.retryable is False
    assert result.stages[0].key == "config" and result.stages[0].status == "failed"
    assert "/home/user" not in result.message
    assert {stage.status for stage in result.stages[1:]} == {"skipped"}


def test_default_client_factory_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """製品のテストが既定の client を差し替えられる（router に client を渡さないとき）。"""
    storage = FakeObjectStorage()
    monkeypatch.setattr(upload_storage_check, "default_object_storage_client", lambda _s: storage)
    app = FastAPI()
    app.include_router(
        build_upload_storage_router(
            get_settings=lambda: FakeSettings(), env_file=lambda: tmp_path / ".env"
        ),
        prefix="/api/settings",
    )

    response = TestClient(app).post("/api/settings/upload-storage/test", json=_OCI_PAYLOAD)

    assert response.json()["data"]["status"] == "success"
    assert storage.calls
