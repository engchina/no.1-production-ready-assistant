"""Object Storage 保存先 client のテスト。"""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.clients.object_storage import (
    OCI_CONNECT_TIMEOUT_SECONDS,
    OCI_MAX_ATTEMPTS,
    OCI_READ_TIMEOUT_SECONDS,
    ObjectStorageClient,
    describe_upload_storage_error,
)
from app.clients.oci_auth import OciPrivateKeyPassPhraseRequiredError
from app.config import Settings


class FakeServiceError(Exception):
    """OCI SDK の ServiceError と同じ `status` / `code` を持つ fake。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


class FakeRequestException(OSError):
    """`oci.exceptions.RequestException`（requests の RequestException）と同じ名前の fake。"""


FakeRequestException.__name__ = "RequestException"


class FakeConnectTimeout(FakeRequestException):
    """`oci.exceptions.ConnectTimeout` と同じ名前の fake。"""


FakeConnectTimeout.__name__ = "ConnectTimeout"


async def test_local_put_get_roundtrip_and_sanitizes_key(tmp_path: Path) -> None:
    """local upload storage backend は安全化したキーで保存・取得できる。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )

    uri = await client.put("uploaded/../policy 001.txt", b"policy body", "text/plain")

    assert uri == "local://uploaded/policy_001.txt"
    assert await client.get(uri) == b"policy body"
    assert not list((tmp_path / "objects" / "uploaded").glob("*.tmp"))


async def test_local_get_rejects_unknown_uri(tmp_path: Path) -> None:
    """local 保存先は未知の URI scheme を local key として扱わない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )

    with pytest.raises(ValueError, match="local:// URI"):
        await client.get("s3://bucket/key.txt")


async def test_local_delete_removes_object_and_is_idempotent(tmp_path: Path) -> None:
    """local upload storage backend は保存済み object を削除する。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )
    uri = await client.put("uploaded/policy.txt", b"policy body", "text/plain")

    assert await client.delete(uri) is True
    assert await client.delete(uri) is False
    with pytest.raises(FileNotFoundError):
        await client.get(uri)


async def test_local_get_rejects_relative_path_segments(tmp_path: Path) -> None:
    """保存済み参照の取得時は相対パス要素を拒否する。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )

    with pytest.raises(ValueError, match="相対パス要素"):
        await client.get("local://uploaded/../policy.txt")


async def test_local_put_rejects_empty_key(tmp_path: Path) -> None:
    """空の Object Storage key は保存しない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )

    with pytest.raises(ValueError, match="空"):
        await client.put("   ", b"body", "text/plain")


async def test_local_put_rejects_uri_key(tmp_path: Path) -> None:
    """local upload storage backend の保存 key に URI は受け付けない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )

    with pytest.raises(ValueError, match="URI"):
        await client.put("oci://namespace/bucket/key.txt", b"body", "text/plain")


async def test_local_put_rejects_too_deep_key(tmp_path: Path) -> None:
    """異常に深い Object Storage key は保存しない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )
    too_deep_key = "/".join(f"part-{index}" for index in range(17))

    with pytest.raises(ValueError, match="階層"):
        await client.put(too_deep_key, b"body", "text/plain")


async def test_local_put_rejects_too_long_key_part(tmp_path: Path) -> None:
    """単一要素が長すぎる Object Storage key は保存しない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )

    with pytest.raises(ValueError, match="255"):
        await client.put(f"uploaded/{'a' * 256}.txt", b"body", "text/plain")


async def test_local_put_rejects_too_long_key(tmp_path: Path) -> None:
    """全体が長すぎる Object Storage key は保存しない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )
    too_long_key = "/".join(["a" * 70 for _ in range(15)])

    with pytest.raises(ValueError, match="長すぎ"):
        await client.put(too_long_key, b"body", "text/plain")


async def test_oci_put_uses_object_storage_sdk_and_returns_uri() -> None:
    """OCI backend は Object Storage SDK へ安全化済み key と content-type を渡す。"""
    sdk = FakeObjectStorageSdkClient()
    client = ObjectStorageClient(
        settings=_oci_settings(),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    uri = await client.put("uploaded/policy 001.txt", b"policy body", "text/plain")

    assert uri == "oci://example-namespace/rag-originals/uploaded/policy_001.txt"
    assert sdk.put_calls == 1
    assert sdk.last_put_request == {
        "namespace_name": "example-namespace",
        "bucket_name": "rag-originals",
        "object_name": "uploaded/policy_001.txt",
        "put_object_body": b"policy body",
        "kwargs": {"content_type": "text/plain"},
    }


async def test_upload_storage_backend_uses_oci_storage_client() -> None:
    """アップロード保存先を OCI にすると Object Storage SDK を使う。"""
    sdk = FakeObjectStorageSdkClient()
    settings = _oci_settings()
    client = ObjectStorageClient(
        settings=settings,
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    uri = await client.put("uploaded/policy.txt", b"policy body", "text/plain")

    assert uri == "oci://example-namespace/rag-originals/uploaded/policy.txt"
    assert sdk.put_calls == 1


async def test_oci_get_uses_object_storage_sdk_for_matching_uri() -> None:
    """OCI backend は返却 URI から object key を復元して bytes を取得する。"""
    sdk = FakeObjectStorageSdkClient(
        get_response=SimpleNamespace(data=SimpleNamespace(content=b"policy body"))
    )
    client = ObjectStorageClient(
        settings=_oci_settings(),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    body = await client.get("oci://example-namespace/rag-originals/uploaded/policy.txt")

    assert body == b"policy body"
    assert sdk.get_calls == 1
    assert sdk.last_get_request == {
        "namespace_name": "example-namespace",
        "bucket_name": "rag-originals",
        "object_name": "uploaded/policy.txt",
    }


async def test_oci_delete_uses_object_storage_sdk_for_matching_uri() -> None:
    """OCI backend は返却 URI から object key を復元して削除する。"""
    sdk = FakeObjectStorageSdkClient()
    client = ObjectStorageClient(
        settings=_oci_settings(),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    deleted = await client.delete("oci://example-namespace/rag-originals/uploaded/policy.txt")

    assert deleted is True
    assert sdk.delete_calls == 1
    assert sdk.last_delete_request == {
        "namespace_name": "example-namespace",
        "bucket_name": "rag-originals",
        "object_name": "uploaded/policy.txt",
    }


async def test_local_delete_prefix_removes_only_objects_under_the_directory(
    tmp_path: Path,
) -> None:
    """prefix 単位の削除は配下の object と空のディレクトリを消し、名前が前方一致する隣は残す。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )
    targets = [
        await client.put("artifacts/extractions/doc-1/trace-a.json", b"a", "application/json"),
        await client.put(
            "artifacts/extractions/doc-1/trace-b/segments/p1.json", b"b", "application/json"
        ),
    ]
    sibling = await client.put("artifacts/extractions/doc-10/trace-a.json", b"c", "text/plain")

    result = await client.delete_prefix("artifacts/extractions/doc-1/")

    assert result.deleted == 2
    assert result.failed == 0
    for target in targets:
        with pytest.raises(FileNotFoundError):
            await client.get(target)
    assert await client.get(sibling) == b"c"
    assert not (tmp_path / "objects" / "artifacts" / "extractions" / "doc-1").exists()
    # 無い prefix は 0 件で終わる（冪等）。
    assert await client.delete_prefix("artifacts/extractions/doc-1/") == (0, 0)


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        ("artifacts/", "浅すぎ"),
        ("artifacts/extractions/doc-1", "ディレクトリ単位"),
        ("artifacts/../extractions/", "相対パス要素"),
        ("oci://namespace/bucket/artifacts/doc-1/", "URI"),
    ],
)
async def test_delete_prefix_rejects_unsafe_prefix(
    tmp_path: Path, prefix: str, message: str
) -> None:
    """保存先全体・相対パス・URI・ファイル名の前方一致になる prefix は削除しない。"""
    client = ObjectStorageClient(
        settings=Settings(upload_storage_backend="local", local_storage_dir=str(tmp_path))
    )
    kept = await client.put("artifacts/extractions/doc-1/trace.json", b"a", "application/json")

    with pytest.raises(ValueError, match=message):
        await client.delete_prefix(prefix)

    assert await client.get(kept) == b"a"


async def test_oci_delete_prefix_lists_all_pages_and_counts_failures() -> None:
    """OCI は prefix で一覧（ページ送り）してから 1 件ずつ消し、個々の失敗は数えて続ける。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.object_names = [
        "artifacts/canonical/doc-1/trace-a/canonical.pdf",
        "artifacts/canonical/doc-1/trace-b/canonical.pdf",
        "artifacts/canonical/doc-1/trace-c/canonical.pdf",
        "artifacts/canonical/doc-10/trace-a/canonical.pdf",
    ]
    sdk.failing_object_names = {"artifacts/canonical/doc-1/trace-b/canonical.pdf"}
    client = ObjectStorageClient(
        settings=_oci_settings(),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    result = await client.delete_prefix("artifacts/canonical/doc-1/")

    assert result.deleted == 2
    assert result.failed == 1
    assert sdk.deleted_object_names == [
        "artifacts/canonical/doc-1/trace-a/canonical.pdf",
        "artifacts/canonical/doc-1/trace-b/canonical.pdf",
        "artifacts/canonical/doc-1/trace-c/canonical.pdf",
    ]
    assert [request["prefix"] for request in sdk.list_requests] == [
        "artifacts/canonical/doc-1/",
        "artifacts/canonical/doc-1/",
    ]
    assert sdk.list_requests[1]["start"] == "artifacts/canonical/doc-1/trace-c/canonical.pdf"


async def test_get_oci_uri_uses_oci_even_when_upload_storage_is_local() -> None:
    """保存先を local に切り替えた後も、既存 OCI URI は取得できる。"""
    sdk = FakeObjectStorageSdkClient(
        get_response=SimpleNamespace(data=SimpleNamespace(content=b"policy body"))
    )
    client = ObjectStorageClient(
        settings=_oci_settings().model_copy(update={"upload_storage_backend": "local"}),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    body = await client.get("oci://example-namespace/rag-originals/uploaded/policy.txt")

    assert body == b"policy body"
    assert sdk.get_calls == 1


async def test_get_local_uri_uses_local_even_when_upload_storage_is_oci(tmp_path: Path) -> None:
    """保存先を OCI に切り替えた後も、既存 local URI は取得できる。"""
    settings = _oci_settings().model_copy(update={"local_storage_dir": str(tmp_path)})
    client = ObjectStorageClient(
        settings=settings,
        storage_client=FakeObjectStorageSdkClient(),
        sdk_call_runner=_run_inline,
    )
    local_uri = await ObjectStorageClient(
        settings=settings.model_copy(update={"upload_storage_backend": "local"})
    ).put("uploaded/policy.txt", b"policy body", "text/plain")

    body = await client.get(local_uri)

    assert body == b"policy body"


async def test_oci_get_rejects_foreign_namespace() -> None:
    """別の namespace（テナンシ）の URI は読まない。"""
    sdk = FakeObjectStorageSdkClient()
    client = ObjectStorageClient(
        settings=_oci_settings(),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    with pytest.raises(ValueError, match="namespace が設定と一致しません"):
        await client.get("oci://other-namespace/rag-originals/uploaded/policy.txt")

    assert sdk.get_calls == 0


async def test_oci_get_and_delete_use_bucket_of_stored_uri_after_bucket_change() -> None:
    """bucket の設定を変えた後も、前の bucket に置いた object を読み・消せる（#2093）。"""
    sdk = FakeObjectStorageSdkClient(
        get_response=SimpleNamespace(data=SimpleNamespace(content=b"old bucket body"))
    )
    client = ObjectStorageClient(
        settings=_oci_settings().model_copy(update={"object_storage_bucket": "new-bucket"}),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    body = await client.get("oci://example-namespace/rag-originals/uploaded/policy.txt")
    deleted = await client.delete("oci://example-namespace/rag-originals/uploaded/policy.txt")

    assert body == b"old bucket body"
    assert deleted is True
    assert sdk.last_get_request is not None
    assert sdk.last_get_request["bucket_name"] == "rag-originals"
    assert sdk.last_delete_request is not None
    assert sdk.last_delete_request["bucket_name"] == "rag-originals"


@pytest.mark.parametrize(
    "uri",
    [
        "oci://example-namespace/bad%bucket/uploaded/policy.txt",
        "oci://example-namespace/rag-originals",
        "oci://example-namespace//uploaded/policy.txt",
    ],
    ids=["invalid-bucket-name", "missing-key", "empty-bucket"],
)
async def test_oci_get_rejects_malformed_uri(uri: str) -> None:
    """URI の bucket 名の文字種と形を検証してから SDK を呼ぶ。"""
    sdk = FakeObjectStorageSdkClient()
    client = ObjectStorageClient(
        settings=_oci_settings(), storage_client=sdk, sdk_call_runner=_run_inline
    )

    with pytest.raises(ValueError):
        await client.get(uri)

    assert sdk.get_calls == 0


async def test_oci_get_raises_file_not_found_when_object_is_missing() -> None:
    """object の無い 404（ObjectNotFound）は local と同じ FileNotFoundError にする（#2093）。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.get_error = FakeServiceError(404, "ObjectNotFound")
    client = ObjectStorageClient(
        settings=_oci_settings(), storage_client=sdk, sdk_call_runner=_run_inline
    )

    with pytest.raises(FileNotFoundError):
        await client.get("oci://example-namespace/rag-originals/uploaded/policy.txt")


async def test_oci_get_keeps_bucket_not_found_as_failure() -> None:
    """bucket が無い 404 は「object が無い」にしない（設定の誤りとして失敗のまま）。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.get_error = FakeServiceError(404, "BucketNotFound")
    client = ObjectStorageClient(
        settings=_oci_settings(), storage_client=sdk, sdk_call_runner=_run_inline
    )

    with pytest.raises(FakeServiceError):
        await client.get("oci://example-namespace/rag-originals/uploaded/policy.txt")


async def test_oci_delete_returns_false_when_object_is_missing() -> None:
    """消す object が既に無ければ、local と同じく False を返す（#2093）。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.delete_error = FakeServiceError(404, "ObjectNotFound")
    client = ObjectStorageClient(
        settings=_oci_settings(), storage_client=sdk, sdk_call_runner=_run_inline
    )

    deleted = await client.delete("oci://example-namespace/rag-originals/uploaded/policy.txt")

    assert deleted is False


async def test_oci_delete_raises_other_service_errors() -> None:
    """権限なしなど、object が無い以外の失敗は送出する。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.delete_error = FakeServiceError(404, "BucketNotFound")
    client = ObjectStorageClient(
        settings=_oci_settings(), storage_client=sdk, sdk_call_runner=_run_inline
    )

    with pytest.raises(FakeServiceError):
        await client.delete("oci://example-namespace/rag-originals/uploaded/policy.txt")


async def test_oci_delete_prefix_does_not_count_already_deleted_objects() -> None:
    """一覧の後に消えた object は失敗に数えない。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.object_names = ["artifacts/canonical/doc-1/a.pdf", "artifacts/canonical/doc-1/b.pdf"]
    sdk.delete_error = FakeServiceError(404, "ObjectNotFound")
    client = ObjectStorageClient(
        settings=_oci_settings(), storage_client=sdk, sdk_call_runner=_run_inline
    )

    result = await client.delete_prefix("artifacts/canonical/doc-1/")

    assert result.deleted == 0
    assert result.failed == 0


async def test_delete_prefix_cleans_local_leftovers_after_switching_to_oci(tmp_path: Path) -> None:
    """OCI に切り替える前に local に置いた成果物も、OCI の成果物と合わせて消す（#2093）。"""
    local_settings = _oci_settings().model_copy(
        update={"upload_storage_backend": "local", "local_storage_dir": str(tmp_path)}
    )
    leftover = await ObjectStorageClient(settings=local_settings).put(
        "artifacts/extractions/doc-1/old-trace.json", b"old", "application/json"
    )
    sdk = FakeObjectStorageSdkClient()
    sdk.object_names = ["artifacts/extractions/doc-1/new-trace.json"]
    client = ObjectStorageClient(
        settings=local_settings.model_copy(update={"upload_storage_backend": "oci"}),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    result = await client.delete_prefix("artifacts/extractions/doc-1/")

    assert result == (2, 0)
    assert sdk.deleted_object_names == ["artifacts/extractions/doc-1/new-trace.json"]
    with pytest.raises(FileNotFoundError):
        await client.get(leftover)


async def test_delete_prefix_touches_oci_only_when_current_or_referenced(tmp_path: Path) -> None:
    """local の保存先では、文書が OCI の参照を持つ（`include_oci`）ときだけ OCI も消す。"""
    sdk = FakeObjectStorageSdkClient()
    sdk.object_names = ["artifacts/extractions/doc-1/oci-trace.json"]
    client = ObjectStorageClient(
        settings=_oci_settings().model_copy(
            update={"upload_storage_backend": "local", "local_storage_dir": str(tmp_path)}
        ),
        storage_client=sdk,
        sdk_call_runner=_run_inline,
    )

    assert await client.delete_prefix("artifacts/extractions/doc-1/") == (0, 0)
    assert sdk.list_requests == []

    result = await client.delete_prefix("artifacts/extractions/doc-1/", include_oci=True)

    assert result == (1, 0)
    assert sdk.deleted_object_names == ["artifacts/extractions/doc-1/oci-trace.json"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeServiceError(404, "BucketNotFound"), "bucket が見つからないか"),
        (FakeServiceError(404, "NotAuthorizedOrNotFound"), "IAM ポリシー"),
        (FakeServiceError(401, "NotAuthenticated"), "OCI 認証"),
        (FakeRequestException("dns failure"), "接続できませんでした"),
        (FakeConnectTimeout("timed out"), "接続できませんでした"),
        (FakeServiceError(500, "InternalServerError"), None),
        (ValueError("OCI Object Storage の namespace / bucket が未設定です。"), None),
    ],
    ids=[
        "bucket-not-found",
        "not-authorized",
        "not-authenticated",
        "unreachable",
        "connect-timeout",
        "server-error",
        "missing-config",
    ],
)
def test_describe_upload_storage_error(error: Exception, expected: str | None) -> None:
    """OCI の失敗の種類ごとに、利用者が直す場所の分かる文言を返す（#2093）。"""
    message = describe_upload_storage_error(error)

    if expected is None:
        assert message is None
    else:
        assert message is not None
        assert expected in message
        assert "example-namespace" not in message


async def test_oci_get_rejects_unknown_uri() -> None:
    """OCI 保存先は未知の URI scheme を OCI key として扱わない。"""
    client = ObjectStorageClient(
        settings=_oci_settings(),
        storage_client=FakeObjectStorageSdkClient(),
        sdk_call_runner=_run_inline,
    )

    with pytest.raises(ValueError, match="oci:// URI"):
        await client.get("s3://bucket/policy.txt")


async def test_oci_backend_requires_namespace_and_bucket() -> None:
    """OCI backend は namespace/bucket の未設定を fail fast する。"""
    client = ObjectStorageClient(
        settings=Settings.model_construct(
            upload_storage_backend="oci",
            object_storage_namespace="",
            object_storage_bucket="",
        ),
        storage_client=FakeObjectStorageSdkClient(),
        sdk_call_runner=_run_inline,
    )

    with pytest.raises(ValueError, match="namespace / bucket"):
        await client.put("uploaded/policy.txt", b"body", "text/plain")


def test_oci_client_prefers_object_storage_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Object Storage SDK client は専用リージョンを OCI 共通リージョンより優先する。

    接続の時間切れと再試行の回数も区切る（SDK の既定の 600 秒の再試行にしない。#2093）。
    """
    captured_config: dict[str, object] = {}
    captured_kwargs: dict[str, object] = {}

    class CapturingObjectStorageSdkClient(FakeObjectStorageSdkClient):
        def __init__(self, config: dict[str, object], **kwargs: object) -> None:
            super().__init__()
            captured_config.update(config)
            captured_kwargs.update(kwargs)

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(
                from_file=lambda path, profile: {
                    "path": path,
                    "profile": profile,
                    "region": "ap-tokyo-1",
                }
            )
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=CapturingObjectStorageSdkClient)
        if name == "oci.retry":
            return FAKE_OCI_RETRY
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("app.clients.object_storage.importlib.import_module", fake_import_module)
    client = ObjectStorageClient(
        settings=_oci_settings().model_copy(
            update={
                "oci_region": "us-chicago-1",
                "object_storage_region": "ap-osaka-1",
            }
        )
    )

    assert isinstance(client._client(), CapturingObjectStorageSdkClient)
    assert captured_config["region"] == "ap-osaka-1"
    assert captured_config["profile"] == "DEFAULT"
    assert captured_kwargs["timeout"] == (OCI_CONNECT_TIMEOUT_SECONDS, OCI_READ_TIMEOUT_SECONDS)
    retry_strategy = captured_kwargs["retry_strategy"]
    assert isinstance(retry_strategy, SimpleNamespace)
    assert retry_strategy.options["max_attempts"] == OCI_MAX_ATTEMPTS


def test_oci_client_keeps_config_region_when_object_storage_region_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """専用 region 未設定時は PLATFORM_OCI_REGION ではなく config file の region を使う。"""
    captured_config: dict[str, object] = {}

    class CapturingObjectStorageSdkClient(FakeObjectStorageSdkClient):
        def __init__(self, config: dict[str, object], **kwargs: object) -> None:
            super().__init__()
            captured_config.update(config)

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(
                from_file=lambda path, profile: {
                    "path": path,
                    "profile": profile,
                    "region": "ap-tokyo-1",
                }
            )
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=CapturingObjectStorageSdkClient)
        if name == "oci.retry":
            return FAKE_OCI_RETRY
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("app.clients.object_storage.importlib.import_module", fake_import_module)
    client = ObjectStorageClient(
        settings=_oci_settings().model_copy(
            update={
                "oci_region": "us-chicago-1",
                "object_storage_region": "",
            }
        )
    )

    assert isinstance(client._client(), CapturingObjectStorageSdkClient)
    assert captured_config["region"] == "ap-tokyo-1"


def test_oci_client_refuses_encrypted_private_key_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """暗号化 OCI API key は Object Storage SDK client 作成前に止める。"""
    key_file = tmp_path / "encrypted.pem"
    key_file.write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    initialized = False

    class CapturingObjectStorageSdkClient(FakeObjectStorageSdkClient):
        def __init__(self, config: dict[str, object], **kwargs: object) -> None:
            nonlocal initialized
            initialized = True
            super().__init__()

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(
                from_file=lambda path, profile: {"key_file": str(key_file), "region": "ap-tokyo-1"}
            )
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=CapturingObjectStorageSdkClient)
        if name == "oci.retry":
            return FAKE_OCI_RETRY
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("app.clients.object_storage.importlib.import_module", fake_import_module)
    client = ObjectStorageClient(
        settings=_oci_settings().model_copy(update={"oci_config_file": str(tmp_path / "config")})
    )

    with pytest.raises(OciPrivateKeyPassPhraseRequiredError, match="pass_phrase"):
        client._client()

    assert initialized is False


class FakeObjectStorageSdkClient:
    """OCI Object Storage SDK client の最小 fake。"""

    def __init__(self, get_response: object | None = None) -> None:
        self._get_response = get_response or SimpleNamespace(data=SimpleNamespace(content=b""))
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0
        self.last_put_request: dict[str, object] | None = None
        self.last_get_request: dict[str, object] | None = None
        self.last_delete_request: dict[str, object] | None = None
        self.object_names: list[str] = []
        self.failing_object_names: set[str] = set()
        self.deleted_object_names: list[str] = []
        self.list_requests: list[dict[str, object]] = []
        self.get_error: Exception | None = None
        self.delete_error: Exception | None = None

    def put_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
        put_object_body: bytes,
        **kwargs: object,
    ) -> object:
        self.put_calls += 1
        self.last_put_request = {
            "namespace_name": namespace_name,
            "bucket_name": bucket_name,
            "object_name": object_name,
            "put_object_body": put_object_body,
            "kwargs": kwargs,
        }
        return SimpleNamespace(data=None)

    def get_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
    ) -> object:
        self.get_calls += 1
        self.last_get_request = {
            "namespace_name": namespace_name,
            "bucket_name": bucket_name,
            "object_name": object_name,
        }
        if self.get_error is not None:
            raise self.get_error
        return self._get_response

    def delete_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
    ) -> object:
        self.delete_calls += 1
        self.last_delete_request = {
            "namespace_name": namespace_name,
            "bucket_name": bucket_name,
            "object_name": object_name,
        }
        self.deleted_object_names.append(object_name)
        if self.delete_error is not None:
            raise self.delete_error
        if object_name in self.failing_object_names:
            raise RuntimeError("delete failed")
        return SimpleNamespace(data=None)

    def list_objects(
        self,
        namespace_name: str,
        bucket_name: str,
        **kwargs: object,
    ) -> object:
        """`object_names` を prefix で絞り、2 件ずつのページで返す（next_start_with 付き）。"""
        self.list_requests.append(kwargs)
        prefix = str(kwargs.get("prefix") or "")
        start = kwargs.get("start")
        names = sorted(name for name in self.object_names if name.startswith(prefix))
        if isinstance(start, str):
            names = [name for name in names if name >= start]
        page, rest = names[:2], names[2:]
        return SimpleNamespace(
            data=SimpleNamespace(
                objects=[SimpleNamespace(name=name) for name in page],
                next_start_with=rest[0] if rest else None,
            )
        )


def _oci_settings() -> Settings:
    return Settings.model_construct(
        upload_storage_backend="oci",
        oci_config_file="~/.oci/config",
        oci_config_profile="DEFAULT",
        oci_region="ap-osaka-1",
        object_storage_region="ap-osaka-1",
        object_storage_namespace="example-namespace",
        object_storage_bucket="rag-originals",
    )


class _FakeRetryStrategyBuilder:
    """`oci.retry.RetryStrategyBuilder` の fake。渡した値をそのまま返す。"""

    def __init__(self, **options: object) -> None:
        self._options = options

    def get_retry_strategy(self) -> SimpleNamespace:
        return SimpleNamespace(options=self._options)


FAKE_OCI_RETRY = SimpleNamespace(RetryStrategyBuilder=_FakeRetryStrategyBuilder)


async def _run_inline(operation: Callable[[], Any]) -> Any:
    """テストでは同期 fake を同一 thread で実行する。"""
    return operation()
