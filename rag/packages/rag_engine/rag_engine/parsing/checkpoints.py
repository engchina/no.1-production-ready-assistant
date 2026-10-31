"""単一ホストの解析checkpointを排他・atomic置換・fsyncで永続化する。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any


class CheckpointError(RuntimeError):
    """保存不能・破損・重複実行時に高コスト処理を継続させないエラー。"""


def digest_json(value: Any) -> str:
    """JSONのキー順に依存しないSHA-256を返す。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    """ファイル全体のSHA-256をストリーム計算する。"""
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    """ファイルとPOSIXの親ディレクトリをfsyncする。失敗時はCheckpointErrorを送出する。"""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix="." + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        raise CheckpointError(f"checkpointの保存に失敗しました: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def checkpoint_lock(directory: Path, name: str = ".analysis.lock"):
    """同じrunの同時変更を拒否し、プロセス終了時はOSにlockを解放させる。"""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

                def unlock():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def unlock():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise CheckpointError(f"run lockを取得できません（使用中またはI/O失敗）: {directory}") from exc
        try:
            yield
        finally:
            unlock()


def save_base_checkpoint(path: Path, *, identity: dict, request: dict, pages: list, records: list, pdf_path: str) -> None:
    """Vision変更前の解析と入力条件を保存する。API認証情報は保存しない。"""
    body = {
        "schema_version": 1,
        "identity": identity,
        "request": request,
        "pages": [asdict(p) for p in pages],
        "records": [asdict(r) for r in records],
        "pdf_path": str(pdf_path),
        "pdf_sha256": file_digest(Path(pdf_path)),
        "page_hashes": {p.image_path: file_digest(Path(p.image_path)) for p in pages if Path(p.image_path).is_file()},
    }
    atomic_json(path, {"body": body, "sha256": digest_json(body)})


def load_base_checkpoint(path: Path) -> dict:
    """checksumと版を検証する。破損・旧runは再解析にfallbackせず明示的に拒否する。"""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        body = envelope["body"]
        if body["schema_version"] != 1 or envelope["sha256"] != digest_json(body):
            raise ValueError("checksum/schema mismatch")
        return body
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckpointError(f"有効な基礎解析checkpointがありません: {path}") from exc
