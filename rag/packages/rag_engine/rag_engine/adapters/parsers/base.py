"""レイアウト解析アダプターが共有する契約と依存確認ユーティリティ。"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rag_engine.config import Settings
from rag_engine.models.layout import LayoutRecord, PageImage


@dataclass(frozen=True)
class AdapterAvailability:
    """アダプターが現在利用できるかと、UI に出す理由を保持します。"""

    available: bool
    message: str


@dataclass(frozen=True)
class AnalysisContext:
    """1 回の解析でアダプターへ渡す入力ファイル、ページ、設定をまとめます。"""

    pdf_path: Path
    run_dir: Path
    pages: list[PageImage]
    settings: Settings
    min_confidence: float = 0.0


class LayoutAdapter(Protocol):
    """解析エンジンごとの実装が満たす共通インターフェースです。"""

    engine_id: str
    label: str

    def availability(self) -> AdapterAvailability:
        """必要な依存と設定が揃っているかを返します。"""
        ...

    def analyze(self, context: AnalysisContext) -> list[LayoutRecord]:
        """解析コンテキストから LayoutRecord の一覧を生成します。"""
        ...


def has_module(name: str) -> bool:
    """Python module が import 可能かを副作用なしで確認します。"""
    return importlib.util.find_spec(name) is not None


def missing_dependency_message(package: str, install: str) -> str:
    """不足依存をインストールするための UI 向けメッセージを作ります。"""
    return f"{package} が未インストールです。プロジェクト直下で `{install}` を実行してから再試行してください。"


def extra_install_command(extra: str) -> str:
    """任意 extra を現在の仮想環境へ追加する pip コマンドを返します。"""
    return f".venv/bin/pip install -e '.[{extra}]'"


def object_to_plain(value):
    """SDK モデルなどを JSON 保存しやすい素朴な dict/list へ変換する。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [object_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): object_to_plain(item) for key, item in value.items()}
    if hasattr(value, "swagger_types"):
        result = {}
        for key in getattr(value, "swagger_types", {}):
            result[key] = object_to_plain(getattr(value, key, None))
        return result
    if hasattr(value, "__dict__"):
        return {str(key).lstrip("_"): object_to_plain(item) for key, item in vars(value).items() if not key.startswith("__")}
    return str(value)
