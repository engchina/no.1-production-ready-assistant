"""言語資源の全体の上書きの保存先 `language-pack.json`（#2181）。

- 中身は組み込みのパックとの差（「節 → 項目 → 値」）だけ。読み込んだら組み込みに重ね、
  ``rag_parser_core.language.set_default_pack`` でこのプロセスの既定にする。
- 起動のあとの最初の ``get_settings`` と、ファイルの mtime が変わったとき（別の worker の保存）
  に読む。
- 書き込みは同じディレクトリの一時ファイルからの置き換え（atomic）。
- 保存ごとの版（日時・保存した人・hash・上書き）を同じディレクトリの
  ``<名前>.history.json`` に残し、前の版に戻せる（新しい順に ``LANGUAGE_PACK_HISTORY_LIMIT`` 件
  まで）。ponytail: 履歴は読んで足して書く。同時の保存は片方の版が落ちうる（管理者の設定で
  頻度は低い。要るなら保存をファイルのロックで囲む）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from rag_parser_core.language import (
    LanguagePack,
    builtin_pack,
    overrides_from,
    resolve,
    set_default_pack,
)

LANGUAGE_PACK_FILE_MODE = 0o600
# 残す版の数（新しい順）。
LANGUAGE_PACK_HISTORY_LIMIT = 5


def history_path(path: Path) -> Path:
    """保存の履歴のファイル（``language-pack.json`` なら ``language-pack.history.json``）。"""
    return path.with_name(f"{path.stem}.history.json")


class LanguagePackStore:
    """`language-pack.json` の読み書きと、このプロセスの既定の言語資源。"""

    def __init__(self) -> None:
        self._loaded: tuple[str, int | None] | None = None

    def reset(self) -> None:
        self._loaded = None
        set_default_pack(None)

    def overrides(self, path: Path) -> dict[str, dict[str, Any]]:
        """保存した上書き（無い・読めないファイルは空）。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        overrides = data.get("overrides") if isinstance(data, Mapping) else None
        return dict(overrides) if isinstance(overrides, Mapping) else {}

    def reload_if_changed(self, path: Path) -> None:
        """ファイルが変わっていれば読み直して既定の言語資源にする（壊れた中身は組み込みのまま）。"""
        key = (str(path), _mtime(path))
        if self._loaded == key:
            return
        self._loaded = key
        try:
            set_default_pack(resolve(builtin_pack(), self.overrides(path)))
        except ValueError:
            # 手で壊した保存は使わない（画面の保存は検証してから書く）。組み込みで動き続ける。
            set_default_pack(None)

    def history(self, path: Path) -> list[dict[str, Any]]:
        """保存の版（新しい順。無い・読めないファイルは空）。"""
        try:
            data = json.loads(history_path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        versions = data.get("versions") if isinstance(data, Mapping) else None
        return [dict(version) for version in versions or [] if isinstance(version, Mapping)][
            :LANGUAGE_PACK_HISTORY_LIMIT
        ]

    def save(
        self,
        path: Path,
        pack: LanguagePack,
        *,
        saved_by: str | None = None,
        restored_from: str | None = None,
    ) -> dict[str, Any]:
        """組み込みとの差を保存し、このプロセスの既定にする。保存の版を履歴に足して返す。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        overrides = overrides_from(pack, builtin_pack())
        _write(path, {"overrides": overrides})
        set_default_pack(pack)
        self._loaded = (str(path), _mtime(path))
        version = {
            "id": uuid4().hex,
            "saved_at": datetime.now(UTC).isoformat(),
            "saved_by": saved_by,
            "hash": pack.hash,
            "ingest_hash": pack.ingest_hash,
            "restored_from": restored_from,
            "overrides": overrides,
        }
        versions = [version, *self.history(path)][:LANGUAGE_PACK_HISTORY_LIMIT]
        _write(history_path(path), {"versions": versions})
        return version


def _write(path: Path, data: Mapping[str, Any]) -> None:
    """同じディレクトリの一時ファイルから置き換える（atomic）。"""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, LANGUAGE_PACK_FILE_MODE)
    os.replace(temporary, path)


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


LANGUAGE_PACK_STORE = LanguagePackStore()
