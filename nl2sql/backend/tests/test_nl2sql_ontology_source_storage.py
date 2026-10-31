"""オントロジーの資料の OCI Object Storage の保存(#2442)。SDK はスタブ。"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import pytest
from fastapi import UploadFile

from app.features.nl2sql import ontology_sources
from app.features.nl2sql.ontology_models import OntologySourceDocument
from app.features.nl2sql.ontology_sources import (
    OCI_CONNECT_TIMEOUT_SECONDS,
    OCI_MAX_ATTEMPTS,
    OCI_READ_TIMEOUT_SECONDS,
    OCI_RETRY_MAX_WAIT_SECONDS,
    OCI_RETRY_TOTAL_ELAPSED_SECONDS,
    OntologySourceError,
    OntologySourceStorage,
    parse_ontology_source_object_uri,
)
from app.settings import Settings

_NAMESPACE = "testns"
_BUCKET = "test-bucket"


class _ServiceError(Exception):
    """oci.exceptions.ServiceError の形(status・code・request_id)のスタブ。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = "req-1"


class ConnectTimeout(Exception):  # noqa: N818 - requests の型の名前に合わせる
    """接続の時間切れ(requests.exceptions.ConnectTimeout と同じ名前)。"""


class _StubObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], bytes] = {}
        self.calls: list[tuple[str, str, str, str]] = []
        self.put_kwargs: list[dict[str, object]] = []
        self.failures: dict[str, Exception] = {}

    def put_object(
        self, namespace: str, bucket: str, name: str, body: BinaryIO, **kwargs: object
    ) -> None:
        self.calls.append(("put", namespace, bucket, name))
        if "put" in self.failures:
            raise self.failures["put"]
        self.put_kwargs.append(kwargs)
        self.objects[(namespace, bucket, name)] = body.read()

    def get_object(self, namespace: str, bucket: str, name: str) -> Any:
        self.calls.append(("get", namespace, bucket, name))
        if "get" in self.failures:
            raise self.failures["get"]
        key = (namespace, bucket, name)
        if key not in self.objects:
            raise _ServiceError(404, "ObjectNotFound", f"{bucket}/{name} not found")
        return SimpleNamespace(data=SimpleNamespace(content=self.objects[key]))

    def delete_object(self, namespace: str, bucket: str, name: str) -> None:
        self.calls.append(("delete", namespace, bucket, name))
        if "delete" in self.failures:
            raise self.failures["delete"]
        key = (namespace, bucket, name)
        if key not in self.objects:
            raise _ServiceError(404, "ObjectNotFound", f"{bucket}/{name} not found")
        del self.objects[key]


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "local_storage_dir": str(tmp_path),
        "max_upload_bytes": 1024,
        "upload_storage_backend": "oci",
        "object_storage_namespace": _NAMESPACE,
        "object_storage_bucket": _BUCKET,
    }
    values.update(overrides)
    return Settings(**values)


async def _save(storage: OntologySourceStorage, content: bytes = b"sample") -> Any:
    with tempfile.SpooledTemporaryFile() as stream:
        stream.write(content)
        stream.seek(0)
        upload = UploadFile(filename="business.txt", file=cast(BinaryIO, stream))
        return await storage.save_upload(profile_id="profile-a", upload=upload)


def _document(storage_uri: str) -> OntologySourceDocument:
    return OntologySourceDocument(
        id="ontology_source_test",
        profile_id="profile-a",
        filename="business.txt",
        size_bytes=6,
        sha256=hashlib.sha256(b"sample").hexdigest(),
        storage_uri=storage_uri,
    )


def _local_files(tmp_path: Path) -> list[Path]:
    return [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_oci_save_uses_namespace_uri_and_round_trips(tmp_path: Path) -> None:
    client = _StubObjectStorage()
    storage = OntologySourceStorage(_settings(tmp_path), storage_client=client)

    document = await _save(storage)

    profile_key = hashlib.sha256(b"profile-a").hexdigest()[:24]
    object_name = f"ontology-sources/{profile_key}/{document.id}/business.txt"
    assert document.storage_uri == f"oci://{_NAMESPACE}/{_BUCKET}/{object_name}"
    assert client.put_kwargs == [{"content_type": "text/plain"}]
    assert _local_files(tmp_path) == []
    assert storage.load(document) == b"sample"

    storage.delete(document)
    assert client.objects == {}


@pytest.mark.asyncio
async def test_oci_load_and_delete_use_namespace_and_bucket_of_reference(
    tmp_path: Path,
) -> None:
    client = _StubObjectStorage()
    document = await _save(OntologySourceStorage(_settings(tmp_path), storage_client=client))
    # 保存の後に保存先の設定を変えても、参照の namespace・bucket から読み・消せる。
    changed = OntologySourceStorage(
        _settings(tmp_path, object_storage_namespace="otherns", object_storage_bucket="other"),
        storage_client=client,
    )

    assert changed.load(document) == b"sample"
    changed.delete(document)
    assert [call[:3] for call in client.calls[1:]] == [
        ("get", _NAMESPACE, _BUCKET),
        ("delete", _NAMESPACE, _BUCKET),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (_ServiceError(404, "BucketNotFound", "Bucket secret-bucket not found"), "IAM ポリシー"),
        (_ServiceError(401, "NotAuthenticated", "secret-bucket auth"), "OCI 認証"),
        (ConnectTimeout("secret-bucket.example timed out"), "接続できなかった"),
        (RuntimeError("secret-bucket internal"), "時間をおいて"),
    ],
)
async def test_oci_save_failure_returns_generic_japanese_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    expected: str,
) -> None:
    client = _StubObjectStorage()
    client.failures["put"] = failure
    storage = OntologySourceStorage(_settings(tmp_path), storage_client=client)

    with caplog.at_level(logging.WARNING), pytest.raises(OntologySourceError) as raised:
        await _save(storage)

    assert raised.value.code == "ONTOLOGY_SOURCE_STORAGE_FAILED"
    assert expected in raised.value.message_ja
    assert str(raised.value) == raised.value.message_ja
    for secret in ("secret-bucket", _NAMESPACE, _BUCKET):
        assert secret not in raised.value.message_ja
    # OCI の詳細はログだけに出す
    record = next(r for r in caplog.records if r.message == "ontology_source_object_storage_failed")
    assert record.exc_info is not None and "secret-bucket" in str(record.exc_info[1])
    assert getattr(record, "operation", None) == "put"
    assert _local_files(tmp_path) == []


@pytest.mark.asyncio
async def test_oci_save_without_namespace_fails_before_calling_oci(tmp_path: Path) -> None:
    client = _StubObjectStorage()
    storage = OntologySourceStorage(
        _settings(tmp_path, object_storage_namespace=""), storage_client=client
    )

    with pytest.raises(OntologySourceError) as raised:
        await _save(storage)

    assert raised.value.code == "ONTOLOGY_SOURCE_STORAGE_NOT_CONFIGURED"
    assert client.calls == []
    assert _local_files(tmp_path) == []


@pytest.mark.parametrize(
    "storage_uri",
    [
        # namespace を含まない旧形式の参照
        f"oci://{_BUCKET}/ontology-sources/abc/ontology_source_x/business.txt",
        f"oci://{_NAMESPACE}/{_BUCKET}/other-prefix/business.txt",
        f"oci://{_NAMESPACE}/{_BUCKET}/ontology-sources/../business.txt",
        f"oci://{_NAMESPACE}/{_BUCKET}",
        f"oci://{_NAMESPACE}/bad bucket/ontology-sources/a/b/business.txt",
    ],
)
def test_invalid_reference_is_rejected_without_calling_oci(storage_uri: str) -> None:
    client = _StubObjectStorage()
    storage = OntologySourceStorage(
        Settings(object_storage_namespace=_NAMESPACE, object_storage_bucket=_BUCKET),
        storage_client=client,
    )

    with pytest.raises(OntologySourceError) as loaded:
        storage.load(_document(storage_uri))
    with pytest.raises(OntologySourceError) as deleted:
        storage.delete(_document(storage_uri))

    assert loaded.value.code == deleted.value.code == "ONTOLOGY_SOURCE_REFERENCE_INVALID"
    assert client.calls == []


def test_parse_reference_splits_namespace_bucket_and_object() -> None:
    assert parse_ontology_source_object_uri("oci://ns/bucket/ontology-sources/k/id/資料 A.txt") == (
        "ns",
        "bucket",
        "ontology-sources/k/id/資料 A.txt",
    )


def test_missing_object_on_load_and_delete(tmp_path: Path) -> None:
    client = _StubObjectStorage()
    storage = OntologySourceStorage(_settings(tmp_path), storage_client=client)
    document = _document(f"oci://{_NAMESPACE}/{_BUCKET}/ontology-sources/k/id/business.txt")

    with pytest.raises(OntologySourceError) as raised:
        storage.load(document)
    assert raised.value.code == "ONTOLOGY_SOURCE_OBJECT_NOT_FOUND"
    # 既に無い object の削除は成功として扱う
    storage.delete(document)


def test_oci_read_and_delete_failures_hide_oci_details(tmp_path: Path) -> None:
    client = _StubObjectStorage()
    client.failures["get"] = _ServiceError(403, "NotAuthorized", "secret-bucket denied")
    client.failures["delete"] = _ServiceError(500, "InternalError", "secret-bucket error")
    storage = OntologySourceStorage(_settings(tmp_path), storage_client=client)
    document = _document(f"oci://{_NAMESPACE}/{_BUCKET}/ontology-sources/k/id/business.txt")

    with pytest.raises(OntologySourceError) as loaded:
        storage.load(document)
    with pytest.raises(OntologySourceError) as deleted:
        storage.delete(document)

    assert loaded.value.code == "ONTOLOGY_SOURCE_STORAGE_READ_FAILED"
    assert "読み取りする権限" in loaded.value.message_ja
    assert deleted.value.code == "ONTOLOGY_SOURCE_STORAGE_DELETE_FAILED"
    assert "削除できませんでした" in deleted.value.message_ja
    for error in (loaded.value, deleted.value):
        assert "secret-bucket" not in error.message_ja
        assert _BUCKET not in error.message_ja


def test_missing_local_file_returns_japanese_message(tmp_path: Path) -> None:
    storage = OntologySourceStorage(_settings(tmp_path, upload_storage_backend="local"))

    with pytest.raises(OntologySourceError) as raised:
        storage.load(_document(str(tmp_path / "missing.txt")))

    assert raised.value.code == "ONTOLOGY_SOURCE_OBJECT_NOT_FOUND"
    assert str(tmp_path) not in raised.value.message_ja


def test_client_has_timeout_and_bounded_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class _Builder:
        def __init__(self, **kwargs: object) -> None:
            captured["retry"] = kwargs

        def get_retry_strategy(self) -> str:
            return "bounded-retry"

    class _Client:
        def __init__(self, config: object, **kwargs: object) -> None:
            captured["config"] = config
            captured["client"] = kwargs

    modules = {
        "oci.config": SimpleNamespace(),
        "oci.object_storage": SimpleNamespace(ObjectStorageClient=_Client),
        "oci.retry": SimpleNamespace(RetryStrategyBuilder=_Builder),
    }
    monkeypatch.setattr(
        ontology_sources, "importlib", SimpleNamespace(import_module=modules.__getitem__)
    )
    monkeypatch.setattr(
        ontology_sources,
        "load_oci_config_without_prompt",
        lambda _module, _file, _profile, region=None: {"region": region},
    )
    storage = OntologySourceStorage(_settings(tmp_path, object_storage_region="ap-test-1"))

    storage._object_storage_client()

    assert captured["config"] == {"region": "ap-test-1"}
    assert captured["client"] == {
        "timeout": (OCI_CONNECT_TIMEOUT_SECONDS, OCI_READ_TIMEOUT_SECONDS),
        "retry_strategy": "bounded-retry",
    }
    assert captured["retry"] == {
        "max_attempts": OCI_MAX_ATTEMPTS,
        "total_elapsed_time_seconds": OCI_RETRY_TOTAL_ELAPSED_SECONDS,
        "retry_max_wait_between_calls_seconds": OCI_RETRY_MAX_WAIT_SECONDS,
    }
