"""process をまたいで使う、時間で切れる小さな結果の記録（ローカルのファイル。#3507）。

1 台の Compute の複数の backend の worker（と取込などの worker）は同じ利用者で動くので、同じディレクトリの
ファイルで結果を共有する。DB の表は作らない（共有の DB に新しい表を足さない）。

- 1 件 = 1 ファイル（名前は key の SHA-256）。key の本文（質問など）はファイルに書かない。
- 書き込みは同じディレクトリの一時ファイルに書いてから rename で置き換える（読む側は古い内容か新しい内容の
  どちらかだけを見る）。同じ key を同時に書いても、どちらか一方の完全な内容が残る。
- 有効期間（`ttl_seconds`）は書いた時刻（内容に持つ）から数える。過ぎた記録・壊れた記録・読めない記録は
  記録が無いのと同じに扱う（例外を上げない）。
- 件数の上限（`max_entries`）を超えたら、書いた時刻の古いファイルから消す。
- 記録の失敗（ディレクトリを作れない・書けない）は、記録しないで続ける（呼び出し元は LLM の結果を使う）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

logger = logging.getLogger(__name__)

# 記録の形式の版。形式を変えたら上げる（古い形式は読まない）。
_FORMAT_VERSION = 1
_SUFFIX = ".json"
_TEMP_PREFIX = ".tmp-"
# 書き込みの途中で process が止まって残った一時ファイルを消すまでの時間（秒）。
_STALE_TEMP_SECONDS = 3600.0
# process をまたぐ記録を読みも書きもしない実行（品質評価。同じ質問の再実行でも実際の費用を測る）。
_BYPASS: ContextVar[bool] = ContextVar("rag_engine_shared_cache_bypass", default=False)


@contextmanager
def bypass_shared_cache() -> Iterator[None]:
    """この文脈（とそこから作る task・thread の文脈）の間、process をまたぐ記録を使わない（品質評価。#3507）。"""
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def shared_cache_bypassed() -> bool:
    """今の文脈で process をまたぐ記録を使わないか。"""
    return _BYPASS.get()


def cache_key_digest(key: object) -> str:
    """key（JSON にできる値）の SHA-256。dict の順は揃え、tuple は list と同じに扱う。"""
    canonical = json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SharedResultCache:
    """ディレクトリの中のファイルに、key ごとの JSON の値を有効期間つきで記録する。"""

    def __init__(
        self,
        directory: Path,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock

    def _path(self, key: object) -> Path:
        return self.directory / f"{cache_key_digest(key)}{_SUFFIX}"

    def get(self, key: object) -> Any | None:
        """記録した値。無い・期限切れ・壊れている・読めないときは None。"""
        path = self._path(key)
        try:
            with path.open("rb") as stream:
                payload = json.loads(stream.read().decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError):
            logger.warning("shared result cache entry unreadable", exc_info=True)
            return None
        if not isinstance(payload, dict) or payload.get("v") != _FORMAT_VERSION or "value" not in payload:
            return None
        created = payload.get("created")
        if not isinstance(created, int | float) or isinstance(created, bool):
            return None
        # 期限切れの記録は消さない（別の process が同じ key を書き直した直後のファイルを消さない）。件数の上限で消える。
        if self._clock() - float(created) > self.ttl_seconds:
            return None
        return payload["value"]

    def put(self, key: object, value: object) -> bool:
        """値を記録する（一時ファイル + rename）。記録できたら True。"""
        path = self._path(key)
        payload = json.dumps({"v": _FORMAT_VERSION, "created": self._clock(), "value": value}, ensure_ascii=False)
        temporary: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with NamedTemporaryFile(dir=self.directory, prefix=_TEMP_PREFIX, suffix=_SUFFIX, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except (OSError, TypeError, ValueError):
            logger.warning("shared result cache write failed", exc_info=True)
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._evict()
        return True

    def _evict(self) -> None:
        """件数の上限を超えた分を、更新の古いファイルから消す。古い一時ファイルも消す。"""
        entries: list[tuple[float, Path]] = []
        now = time.time()
        try:
            with os.scandir(self.directory) as scanner:
                for entry in scanner:
                    if not entry.name.endswith(_SUFFIX):
                        continue
                    try:
                        modified = entry.stat().st_mtime
                    except OSError:
                        continue
                    if entry.name.startswith(_TEMP_PREFIX):
                        if now - modified > _STALE_TEMP_SECONDS:
                            Path(entry.path).unlink(missing_ok=True)
                        continue
                    entries.append((modified, Path(entry.path)))
        except OSError:
            logger.warning("shared result cache scan failed", exc_info=True)
            return
        excess = len(entries) - self.max_entries
        if excess <= 0:
            return
        entries.sort(key=lambda item: item[0])
        for _, path in entries[:excess]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("shared result cache eviction failed", exc_info=True)


def shared_result_cache(directory: Path | str | None, *, ttl_seconds: float, max_entries: int) -> SharedResultCache | None:
    """設定から記録を作る。ディレクトリが無い・有効期間か件数が 0・使わない文脈なら None（process の中の記録だけを使う）。"""
    if _BYPASS.get() or directory is None or not str(directory).strip() or ttl_seconds <= 0 or max_entries <= 0:
        return None
    return SharedResultCache(Path(directory).expanduser(), ttl_seconds=ttl_seconds, max_entries=max_entries)
