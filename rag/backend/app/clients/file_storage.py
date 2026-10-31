"""アップロード原本と取込の成果物のローカル保存先（`PLATFORM_LOCAL_STORAGE_DIR`）。

保存先はローカルのディレクトリだけ（OCI Object Storage は #11 で削除した）。DB に残った
以前の `oci://` の参照は読めない（取得は FileNotFoundError、削除は「無い」扱い）。
"""

import contextlib
import os
import re
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from app.config import Settings, get_settings

MAX_OBJECT_KEY_LENGTH = 1024
MAX_OBJECT_KEY_DEPTH = 16
MAX_OBJECT_KEY_PART_LENGTH = 255
# prefix 単位の削除で許す最も浅い階層（`artifacts/` のような保存先全体の削除を防ぐ。#303）。
MIN_DELETE_PREFIX_DEPTH = 2
# 削除した OCI Object Storage の保存先の参照（#11）。読めない参照として扱う。
_REMOVED_OCI_URI_PREFIX = "oci://"


class PrefixDeleteResult(NamedTuple):
    """prefix 単位の削除の結果。"""

    deleted: int
    failed: int


class FileStorageClient:
    """ローカル保存先への保存・取得・削除。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """オブジェクトを保存し、参照パス（`local://<key>`）を返す。"""
        del content_type  # ローカル保存では使わない（呼び出し側の契約を保つ）。
        return self._put_to_local(_local_storage_key(key), data)

    async def get(self, key: str) -> bytes:
        """オブジェクトを取得する。無ければ（以前の `oci://` の参照を含む）FileNotFoundError。"""
        if key.strip().startswith(_REMOVED_OCI_URI_PREFIX):
            raise FileNotFoundError("以前の OCI Object Storage の参照は読めません。")
        return self._get_from_local(key)

    async def delete(self, key: str) -> bool:
        """オブジェクトを削除する。存在した場合は True（以前の `oci://` の参照は False）。"""
        if key.strip().startswith(_REMOVED_OCI_URI_PREFIX):
            return False
        return self._delete_from_local(key)

    async def delete_prefix(self, prefix: str) -> PrefixDeleteResult:
        """prefix（末尾 `/` で終わるディレクトリ単位）配下のオブジェクトをすべて削除する。

        文書の削除で、参照が残っていない過去の取込の成果物を後始末するために使う（#303）。
        個々のオブジェクトの削除の失敗は数えて残りを続ける。
        """
        safe_prefix = _delete_prefix_key(prefix)
        if not str(getattr(self._settings, "local_storage_dir", "") or "").strip():
            return PrefixDeleteResult(deleted=0, failed=0)
        return self._delete_prefix_from_local(safe_prefix)

    def _put_to_local(self, safe_key: str, data: bytes) -> str:
        """ローカル保存先へ保存する。"""
        root = Path(self._settings.local_storage_dir).expanduser().resolve()
        path = (root / "objects" / safe_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("保存先キーが不正です。")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)
        return f"local://{safe_key}"

    def _get_from_local(self, key: str) -> bytes:
        """ローカル保存先から取得する。"""
        safe_key = _local_object_key(key)
        root = Path(self._settings.local_storage_dir).expanduser().resolve()
        path = (root / "objects" / safe_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("取得キーが不正です。")
        return path.read_bytes()

    def _delete_from_local(self, key: str) -> bool:
        """ローカル保存先から削除する。"""
        safe_key = _local_object_key(key)
        root = Path(self._settings.local_storage_dir).expanduser().resolve()
        path = (root / "objects" / safe_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("削除キーが不正です。")
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def _delete_prefix_from_local(self, safe_prefix: str) -> PrefixDeleteResult:
        """ローカル保存先の prefix ディレクトリ配下を削除する。symlink は辿らずリンクだけ消す。"""
        objects_root = (Path(self._settings.local_storage_dir).expanduser() / "objects").resolve()
        base = (objects_root / safe_prefix.rstrip("/")).resolve()
        if not base.is_relative_to(objects_root) or base == objects_root:
            raise ValueError("削除 prefix が不正です。")
        if not base.is_dir():
            return PrefixDeleteResult(deleted=0, failed=0)
        deleted = 0
        failed = 0
        for dirpath, dirnames, filenames in os.walk(base, topdown=False):
            current = Path(dirpath)
            for name in filenames:
                try:
                    (current / name).unlink()
                    deleted += 1
                except OSError:
                    failed += 1
            for name in dirnames:
                child = current / name
                try:
                    if child.is_symlink():
                        child.unlink()
                        deleted += 1
                    else:
                        child.rmdir()
                except OSError:
                    # 消せなかったファイルが残るディレクトリは残す（失敗はファイル側で数えている）。
                    continue
        with contextlib.suppress(OSError):
            base.rmdir()
        return PrefixDeleteResult(deleted=deleted, failed=failed)


def _safe_key(key: str, *, reject_relative_segments: bool = False) -> str:
    """保存先キーとして扱える文字だけに正規化する。"""
    normalized = key.strip().replace("\\", "/")
    if reject_relative_segments and any(part in (".", "..") for part in normalized.split("/")):
        raise ValueError("保存先キーに相対パス要素は指定できません。")
    normalized = re.sub(r"[^A-Za-z0-9._/\-]", "_", normalized)
    parts = [part for part in normalized.split("/") if part not in ("", ".", "..")]
    if not parts:
        raise ValueError("保存先キーが空です。")
    if len(parts) > MAX_OBJECT_KEY_DEPTH:
        raise ValueError("保存先キーの階層が深すぎます。")
    if any(len(part) > MAX_OBJECT_KEY_PART_LENGTH for part in parts):
        raise ValueError("保存先キーの各要素は 255 文字以下にしてください。")
    safe_key = "/".join(parts)
    if len(safe_key) > MAX_OBJECT_KEY_LENGTH:
        raise ValueError("保存先キーが長すぎます。")
    return safe_key


def _local_object_key(key: str) -> str:
    """local upload storage backend で扱える参照だけをキー化する。"""
    if "://" in key and not key.startswith("local://"):
        raise ValueError("保存先の参照は local:// の URI だけを扱えます。")
    return _safe_key(key.removeprefix("local://"), reject_relative_segments=True)


def _local_storage_key(key: str) -> str:
    """local upload storage backend の保存キーとして扱える値だけをキー化する。"""
    if "://" in key:
        raise ValueError("保存先キーに URI は指定できません。")
    return _safe_key(key)


def _delete_prefix_key(prefix: str) -> str:
    """prefix 単位の削除の対象を `a/b/` の形へ正規化する。

    浅すぎる prefix・URI・相対パス要素と、`/` で終わらない（ファイル名の前方一致になる）
    ものは拒否する。
    """
    normalized = prefix.strip().replace("\\", "/")
    if "://" in normalized:
        raise ValueError("削除 prefix に URI は指定できません。")
    if not normalized.endswith("/"):
        raise ValueError("削除 prefix は `/` で終わるディレクトリ単位で指定してください。")
    safe_prefix = _safe_key(normalized, reject_relative_segments=True)
    if len(safe_prefix.split("/")) < MIN_DELETE_PREFIX_DEPTH:
        raise ValueError("削除 prefix の階層が浅すぎます。")
    return f"{safe_prefix}/"


def _atomic_write(path: Path, data: bytes) -> None:
    """同一ディレクトリ内の一時ファイル経由で原子的に書き込む。"""
    temporary_path = path.with_name(f".tmp-{uuid4().hex}")
    try:
        temporary_path.write_bytes(data)
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
