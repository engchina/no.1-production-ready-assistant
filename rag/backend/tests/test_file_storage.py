"""アップロード原本と取込の成果物のローカル保存先 client のテスト。"""

from pathlib import Path

import pytest

from app.clients.file_storage import FileStorageClient
from app.config import Settings


async def test_local_put_get_roundtrip_and_sanitizes_key(tmp_path: Path) -> None:
    """ローカル保存先 は安全化したキーで保存・取得できる。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    uri = await client.put("uploaded/../policy 001.txt", b"policy body", "text/plain")

    assert uri == "local://uploaded/policy_001.txt"
    assert await client.get(uri) == b"policy body"
    assert not list((tmp_path / "objects" / "uploaded").glob("*.tmp"))


async def test_local_get_rejects_unknown_uri(tmp_path: Path) -> None:
    """local 保存先は未知の URI scheme を local key として扱わない。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    with pytest.raises(ValueError, match="local:// の URI"):
        await client.get("s3://bucket/key.txt")


async def test_local_delete_removes_object_and_is_idempotent(tmp_path: Path) -> None:
    """ローカル保存先 は保存済み object を削除する。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))
    uri = await client.put("uploaded/policy.txt", b"policy body", "text/plain")

    assert await client.delete(uri) is True
    assert await client.delete(uri) is False
    with pytest.raises(FileNotFoundError):
        await client.get(uri)


async def test_local_get_rejects_relative_path_segments(tmp_path: Path) -> None:
    """保存済み参照の取得時は相対パス要素を拒否する。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    with pytest.raises(ValueError, match="相対パス要素"):
        await client.get("local://uploaded/../policy.txt")


async def test_local_put_rejects_empty_key(tmp_path: Path) -> None:
    """空の 保存先キー は保存しない。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    with pytest.raises(ValueError, match="空"):
        await client.put("   ", b"body", "text/plain")


async def test_local_put_rejects_uri_key(tmp_path: Path) -> None:
    """ローカル保存先 の保存 key に URI は受け付けない。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    with pytest.raises(ValueError, match="URI"):
        await client.put("oci://namespace/bucket/key.txt", b"body", "text/plain")


async def test_local_put_rejects_too_deep_key(tmp_path: Path) -> None:
    """異常に深い 保存先キー は保存しない。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))
    too_deep_key = "/".join(f"part-{index}" for index in range(17))

    with pytest.raises(ValueError, match="階層"):
        await client.put(too_deep_key, b"body", "text/plain")


async def test_local_put_rejects_too_long_key_part(tmp_path: Path) -> None:
    """単一要素が長すぎる 保存先キー は保存しない。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    with pytest.raises(ValueError, match="255"):
        await client.put(f"uploaded/{'a' * 256}.txt", b"body", "text/plain")


async def test_local_put_rejects_too_long_key(tmp_path: Path) -> None:
    """全体が長すぎる 保存先キー は保存しない。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))
    too_long_key = "/".join(["a" * 70 for _ in range(15)])

    with pytest.raises(ValueError, match="長すぎ"):
        await client.put(too_long_key, b"body", "text/plain")


async def test_local_delete_prefix_removes_only_objects_under_the_directory(
    tmp_path: Path,
) -> None:
    """prefix 単位の削除は配下の object と空のディレクトリを消し、名前が前方一致する隣は残す。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))
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
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))
    kept = await client.put("artifacts/extractions/doc-1/trace.json", b"a", "application/json")

    with pytest.raises(ValueError, match=message):
        await client.delete_prefix(prefix)

    assert await client.get(kept) == b"a"


async def test_put_always_stores_locally_even_if_old_backend_setting_remains(
    tmp_path: Path,
) -> None:
    """以前の `upload_storage_backend=oci` は読み捨て、ローカルに保存する（#11）。"""
    settings = Settings.model_validate(
        {"upload_storage_backend": "oci", "local_storage_dir": str(tmp_path)}
    )
    assert not hasattr(settings, "upload_storage_backend")

    uri = await FileStorageClient(settings=settings).put("uploaded/a.txt", b"a", "text/plain")

    assert uri == "local://uploaded/a.txt"
    assert (tmp_path / "objects" / "uploaded" / "a.txt").read_bytes() == b"a"


async def test_legacy_oci_reference_is_not_readable(tmp_path: Path) -> None:
    """DB に残った以前の `oci://` の参照は読めない（無いものとして扱う。#11）。"""
    client = FileStorageClient(settings=Settings(local_storage_dir=str(tmp_path)))

    with pytest.raises(FileNotFoundError):
        await client.get("oci://namespace/bucket/uploaded/a.txt")
    assert await client.delete("oci://namespace/bucket/uploaded/a.txt") is False
