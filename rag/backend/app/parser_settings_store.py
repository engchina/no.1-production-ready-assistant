"""RAG の文書解析の設定（解析エンジンと外部接続）の保存先 `parser-settings.json`（#1701）。

文書解析の設定は RAG だけのもので、編集も RAG の「文書解析」画面だけ。3製品で共有する
`platform/model-settings.json` には置かない（他製品の保存で節が欠けて RAG が落ち、parser の保存が
共有のモデル設定を書き直していた。#1701）。

- 画面から保存した値は `.env` より優先し、起動時と mtime の変化時に読み込む（別 worker の保存）。
  ファイルが無ければ `.env` と既定値のまま。
- 書き込みは同じディレクトリの一時ファイルからの置き換え（atomic）で、read-modify-write は
  `<file>.lock` の flock で worker 間で直列にする。
- API key は JSON に書かず、RAG の `backend/.env`（`RAG_PARSER_*_API_KEY`）に保存する。ほかの
  プロセスは `.env` の mtime の変化で読み直す（`reload_env_settings_if_changed`）。
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import dotenv_values
from pr_system_settings.env_file import write_env_values

PARSER_SETTINGS_FILE_MODE = 0o600
PARSER_SETTINGS_DIRECTORY_MODE = 0o700


class ParserSettingsStore:
    """`parser-settings.json` と、その secret（`backend/.env`）の読み書き。"""

    def __init__(
        self,
        *,
        resolve_path: Callable[[Any], Path],
        env_file: Callable[[Any], Path],
        load: Callable[[Any, Mapping[str, Any]], None],
        dump: Callable[[Any], Mapping[str, Any]],
        secrets: Sequence[tuple[str, str]],
    ) -> None:
        """`secrets` は (Settings の属性名, `.env` の変数名)。`dump` は secret を返さない。"""
        self._resolve_path = resolve_path
        self._env_file = env_file
        self._load = load
        self._dump = dump
        self._secrets = tuple(secrets)
        self._loaded: tuple[str, int | None] | None = None

    def path(self, settings: Any) -> Path:
        return self._resolve_path(settings)

    def env_file(self, settings: Any) -> Path:
        """API key を保存する `.env`（RAG の `backend/.env`）。"""
        return self._env_file(settings)

    def reset(self) -> None:
        """テストや明示的な再初期化のため、最後に読んだファイルの記録を消す。"""
        self._loaded = None

    def load(self, settings: Any) -> None:
        """保存済みのファイルがあれば Settings へ上書き適用する。無ければ何もしない。"""
        path = self.path(settings)
        if not path.is_file():
            self._loaded = (str(path), None)
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("JSON object ではありません。")
            self._load(settings, raw)
        except (OSError, ValueError) as exc:
            raise ValueError(f"文書解析の設定ファイルを読み込めません: {path}") from exc
        self._loaded = (str(path), mtime_ns)

    def reload_if_changed(self, settings: Any) -> None:
        """別 worker が保存した設定を次回リクエストで取り込む。"""
        path = self.path(settings)
        try:
            mtime_ns = path.stat().st_mtime_ns if path.is_file() else None
        except OSError:
            mtime_ns = None
        if self._loaded == (str(path), mtime_ns):
            return
        self.load(settings)

    @contextmanager
    def lock(self, settings: Any) -> Iterator[None]:
        """read-modify-write を worker 間で直列化する。"""
        path = self.path(settings)
        lock_path = path.with_name(f"{path.name}.lock")
        _ensure_directory(lock_path.parent)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            lock_path.chmod(PARSER_SETTINGS_FILE_MODE)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save(self, settings: Any) -> None:
        """`.env` の API key → JSON の順に保存する。JSON に失敗したら `.env` を元に戻す。

        `.env` を先に書くのは、JSON の mtime を見て再読込する別 worker が新しい key を読めるように
        するため。`lock()` の中で呼ぶ。失敗時は OSError を送出する。
        """
        env_file = self.env_file(settings)
        current = _dotenv_values(env_file)
        values: dict[str, str | None] = {}
        for attr, env in self._secrets:
            value = str(getattr(settings, attr, "") or "").strip() or None
            # 環境変数から来ただけの値は `.env` に書かない（環境変数を変えたら追従させる）。
            if env not in current and value == (os.environ.get(env) or "").strip():
                value = None
            values[env] = value
        previous = {name: current.get(name) or None for name in values}
        if values:
            _write_env_secrets(env_file, values)
        try:
            self._write_document(settings)
        except OSError:
            if values:
                _write_env_secrets(env_file, previous)
            raise

    def _write_document(self, settings: Any) -> None:
        path = self.path(settings)
        document = dict(self._dump(settings))
        _ensure_directory(path.parent)
        tmp_path = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        try:
            tmp_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.chmod(PARSER_SETTINGS_FILE_MODE)
            tmp_path.replace(path)
            path.chmod(PARSER_SETTINGS_FILE_MODE)
        finally:
            tmp_path.unlink(missing_ok=True)


def _dotenv_values(env_file: Path) -> dict[str, str | None]:
    return dict(dotenv_values(env_file)) if env_file.is_file() else {}


def _write_env_secrets(env_file: Path, values: Mapping[str, str | None]) -> None:
    write_env_values(env_file, values, section_comment="# 文書解析の secret（画面から保存）")


def _ensure_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=PARSER_SETTINGS_DIRECTORY_MODE, parents=True, exist_ok=True)
    if not existed:
        path.chmod(PARSER_SETTINGS_DIRECTORY_MODE)
