"""オントロジーの資料の保存（ローカルのディレクトリ）。

OCI Object Storage の保存は削除した（#11）。以前に Object Storage へ保存した資料の参照
（`oci://…`）は、読み取れない資料として既存のエラーで返し、削除は済んだものとして扱う。
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest
from fastapi import UploadFile

from app.features.nl2sql.ontology_models import OntologySourceDocument
from app.features.nl2sql.ontology_sources import OntologySourceError, OntologySourceStorage
from app.settings import Settings

_LEGACY_OBJECT_STORAGE_URI = "oci://testns/test-bucket/ontology-sources/key/source/business.txt"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"local_storage_dir": str(tmp_path), "max_upload_bytes": 1024}
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
async def test_save_writes_local_file_and_round_trips(tmp_path: Path) -> None:
    storage = OntologySourceStorage(_settings(tmp_path))

    document = await _save(storage)

    assert Path(document.storage_uri).is_relative_to(tmp_path / "ontology-sources")
    assert document.sha256 == hashlib.sha256(b"sample").hexdigest()
    assert storage.load(document) == b"sample"
    storage.delete(document)
    assert _local_files(tmp_path) == []


def test_missing_local_file_returns_japanese_message(tmp_path: Path) -> None:
    storage = OntologySourceStorage(_settings(tmp_path))

    with pytest.raises(OntologySourceError) as raised:
        storage.load(_document(str(tmp_path / "missing.txt")))

    assert raised.value.code == "ONTOLOGY_SOURCE_OBJECT_NOT_FOUND"
    assert str(tmp_path) not in raised.value.message_ja


def test_legacy_object_storage_reference_is_not_found_without_reading(tmp_path: Path) -> None:
    """以前の Object Storage の参照は、ローカルを探さず「見つからない」の既存のエラーにする。"""
    storage = OntologySourceStorage(_settings(tmp_path))

    with pytest.raises(OntologySourceError) as raised:
        storage.load(_document(_LEGACY_OBJECT_STORAGE_URI))

    assert raised.value.code == "ONTOLOGY_SOURCE_OBJECT_NOT_FOUND"
    assert "testns" not in raised.value.message_ja
    assert "test-bucket" not in raised.value.message_ja


def test_legacy_object_storage_reference_delete_is_noop(tmp_path: Path) -> None:
    """以前の Object Storage の参照の削除は、ローカルに触れずに済んだものとして扱う。"""
    keep = tmp_path / "keep.txt"
    keep.write_bytes(b"keep")
    storage = OntologySourceStorage(_settings(tmp_path))

    storage.delete(_document(_LEGACY_OBJECT_STORAGE_URI))

    assert _local_files(tmp_path) == [keep]
