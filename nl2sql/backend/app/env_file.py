"""`.env` ファイル更新の共有排他制御。"""

from __future__ import annotations

import fcntl
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

DEFAULT_ENV_FILE_MODE = 0o600

_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _process_lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(path, threading.RLock())


@contextmanager
def locked_env_file(path: Path) -> Iterator[Path]:
    """同一 `.env` の read-modify-replace 全体を process/thread 間で直列化する。"""
    env_path = _canonical_path(path)
    process_lock = _process_lock_for(env_path)
    with process_lock:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = env_path.with_name(f".{env_path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            with suppress(OSError):
                lock_path.chmod(DEFAULT_ENV_FILE_MODE)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield env_path
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def replace_env_file(
    path: Path,
    content: str,
    *,
    default_mode: int = DEFAULT_ENV_FILE_MODE,
) -> None:
    """同一ディレクトリの一時ファイルから `.env` を atomic replace する。"""
    env_path = _canonical_path(path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(env_path.stat().st_mode) if env_path.exists() else default_mode
    tmp_path = env_path.with_name(f".{env_path.name}.tmp-{uuid4().hex}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.chmod(mode)
        tmp_path.replace(env_path)
        env_path.chmod(mode)
    finally:
        with suppress(OSError):
            tmp_path.unlink()
