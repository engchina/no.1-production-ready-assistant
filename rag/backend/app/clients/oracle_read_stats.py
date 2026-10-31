"""Oracle の読み取りの計測と、LOB を値で受け取る範囲(#1490)。

- ``track_db_calls()`` の中で実行した SQL の回数・行数・LOB の読み取りの回数・時間を数える。
  入れ子にでき、内側で数えた分は外側にも足す(工程の span と、回答の中の工程の両方に出す)。
  値は ``contextvars`` で渡すため、``asyncio.to_thread`` / ``asyncio.gather`` の中の SQL も数える。
- ``read_lobs_as_values()`` の中の読み取り(``_fetch_all``)は、CLOB を str・BLOB を bytes で
  受け取る(``fetch_lobs=False``)。LOB のロケータを行ごとに ``.read()`` する往復が無くなる。
  結果の値は従来(``.read()`` した値)と同じ。書き込み・LOB のロケータを使う経路には効かない。
- ``read_rows_per_round_trip(n)`` の中の読み取り(``_fetch_all``)は、1 回の往復で ``n`` 行まで
  受け取る(``prefetchrows`` / ``arraysize``。python-oracledb の既定は 100 行。#1498)。行の多い
  検索の候補の SQL で、fetch の追加の往復を減らす。結果の値・行の順は変わらない。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class DbCallStats:
    """1 つの範囲で実行した SQL の回数・行数・LOB の読み取りの回数・時間。"""

    execute_count: int = 0
    row_count: int = 0
    lob_read_count: int = 0
    seconds: float = 0.0
    parent: DbCallStats | None = field(default=None, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_execute(self, *, rows: int, seconds: float) -> None:
        """SQL を 1 回実行した(``rows`` 行を読んだ)ことを、この範囲と外側の範囲に足す。"""
        stats: DbCallStats | None = self
        while stats is not None:
            with stats._lock:
                stats.execute_count += 1
                stats.row_count += rows
                stats.seconds += seconds
            stats = stats.parent

    def record_lob_read(self) -> None:
        """LOB を 1 回 ``.read()`` した(DB への 1 往復)ことを足す。"""
        stats: DbCallStats | None = self
        while stats is not None:
            with stats._lock:
                stats.lob_read_count += 1
            stats = stats.parent

    def snapshot(self) -> dict[str, int | float]:
        """trace の属性・工程の記録に出す値。

        ``db_round_trip_count`` は SQL の実行と LOB の読み取りの合計(fetch の追加の往復は含まない)。
        """
        with self._lock:
            return {
                "db_execute_count": self.execute_count,
                "db_row_count": self.row_count,
                "db_lob_read_count": self.lob_read_count,
                "db_round_trip_count": self.execute_count + self.lob_read_count,
                "db_seconds": round(self.seconds, 3),
            }


_CURRENT_DB_CALL_STATS: ContextVar[DbCallStats | None] = ContextVar(
    "rag_oracle_db_call_stats", default=None
)
_READ_LOBS_AS_VALUES: ContextVar[bool] = ContextVar("rag_oracle_read_lobs_as_values", default=False)
_ROWS_PER_ROUND_TRIP: ContextVar[int | None] = ContextVar(
    "rag_oracle_rows_per_round_trip", default=None
)


def current_db_call_stats() -> DbCallStats | None:
    """いま数えている範囲(無ければ None)。"""
    return _CURRENT_DB_CALL_STATS.get()


@contextmanager
def track_db_calls() -> Iterator[DbCallStats]:
    """この中で実行した SQL を数える。外側の範囲があれば、そこにも足す。"""
    stats = DbCallStats(parent=_CURRENT_DB_CALL_STATS.get())
    token = _CURRENT_DB_CALL_STATS.set(stats)
    try:
        yield stats
    finally:
        _CURRENT_DB_CALL_STATS.reset(token)


def lobs_as_values_enabled() -> bool:
    """この中の読み取りで LOB を値(str / bytes)で受け取るか。"""
    return _READ_LOBS_AS_VALUES.get()


@contextmanager
def read_lobs_as_values() -> Iterator[None]:
    """この中の読み取り(``_fetch_all``)で、LOB を値で受け取る。"""
    token = _READ_LOBS_AS_VALUES.set(True)
    try:
        yield
    finally:
        _READ_LOBS_AS_VALUES.reset(token)


def rows_per_round_trip() -> int | None:
    """この中の読み取りで、1 回の往復で受け取る行の数(既定のままなら None)。"""
    return _ROWS_PER_ROUND_TRIP.get()


@contextmanager
def read_rows_per_round_trip(rows: int) -> Iterator[None]:
    """この中の読み取り(``_fetch_all``)で、1 回の往復で ``rows`` 行まで受け取る。"""
    if rows < 1:
        raise ValueError("1 回の往復で受け取る行の数は 1 以上にしてください。")
    token = _ROWS_PER_ROUND_TRIP.set(rows)
    try:
        yield
    finally:
        _ROWS_PER_ROUND_TRIP.reset(token)
