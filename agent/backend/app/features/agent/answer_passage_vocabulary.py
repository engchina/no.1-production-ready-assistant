"""回答の段落の分け方の語（RAG と Agent の共有の契約。#2197）。

語の正本は ``platform/contracts/answer-passages/vocabulary.json``。Agent の
``answer_passages`` と RAG の ``operation_audit`` が同じ語から規則を組み立てる（規則の形は
両方のコードにあり、``platform/contracts/answer-passages/*.json`` の同じ事例で一致を
確かめる）。配備はリポジトリの全体を置くので、実行のときもこのファイルを読む。
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

_RELATIVE = Path("platform/contracts/answer-passages/vocabulary.json")


@cache
def vocabulary() -> dict[str, Any]:
    """共有の語彙（このファイルから上の階層をたどって ``platform/contracts`` を探す）。"""
    for parent in Path(__file__).resolve().parents:
        path = parent / _RELATIVE
        if path.is_file():
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return data
    raise FileNotFoundError(f"共有の語彙が見つかりません: {_RELATIVE}")


def alt(key: str) -> str:
    """語の一覧の選択（長い語を先に。語の中の空白は 0 個以上の空白）。"""
    words = sorted(vocabulary()[key], key=len, reverse=True)
    return "(?:" + "|".join(re.escape(word).replace(r"\ ", r"\s*") for word in words) + ")"
