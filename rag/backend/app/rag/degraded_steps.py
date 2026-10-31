"""回答の補助の処理が失敗して縮退した記録(#3259・#3286)。

回答の診断の ``degraded_steps`` に、処理の種類・回数・例外の型の名前だけを出す(例外の文・SQL・
本文・識別子は出さない。ログの exc_info だけに残す)。回答エンジン(``app.rag.answer_engine``)と、
その前の段(``app.rag.pipeline`` の会話の履歴での書き換え・抽出項目の条件・会話の添付)が同じ記録を
使う。処理の種類の一覧は ``rag/docs/answer-capability-inventory.md``。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import cast

# 失敗を記録する関数(処理の種類, 例外)。
ReportDegraded = Callable[[str, BaseException], None]


def not_recorded(kind: str, exc: BaseException) -> None:
    """記録しない(呼び出し元が回答の診断を持たないとき。ログは呼び出し元が残す)。"""


class DegradedSteps:
    """失敗して空のまま続けた補助の処理の記録(#3259)。

    (処理の種類, 例外の型の名前) → {kind, count, exc_type}。「当たらなかった」と「読めなかった」を
    診断で分ける。例外の文・SQL・本文・識別子は入れない(ログの exc_info だけに残す)。rag_engine の
    thread からも記録するので lock で守る。回答エンジンの前の段(会話の履歴での書き換え・抽出項目の
    条件・会話の添付。``app.rag.pipeline``)も同じ記録を回答エンジンへ渡す(#3286)。
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, object]] = {}
        self._lock = threading.Lock()

    def record_degraded(self, kind: str, exc: BaseException) -> None:
        """補助の処理 ``kind`` が ``exc`` で失敗し、空のまま続けたことを数える。"""
        exc_type = type(exc).__name__
        with self._lock:
            entry = self._entries.setdefault(
                (kind, exc_type), {"kind": kind, "count": 0, "exc_type": exc_type}
            )
            entry["count"] = cast(int, entry["count"]) + 1

    def steps(self) -> list[dict[str, object]]:
        """失敗した補助の処理の記録(最初に失敗した順)。"""
        with self._lock:
            return [dict(entry) for entry in self._entries.values()]

    def failed(self, kind: str) -> bool:
        """処理 ``kind`` が 1 回でも失敗したか。"""
        with self._lock:
            return any(entry["kind"] == kind for entry in self._entries.values())
