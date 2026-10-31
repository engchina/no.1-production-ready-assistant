"""回答の期限（協調的な取り消し。#1422）。

回答フローは worker thread で動く。呼び出し側（backend）が回答を上限で打ち切っても、thread は
外から止められないため、LLM 1 回の timeout・再試行のまま LLM を呼び続けてしまう。

呼び出し側は回答の期限（`Deadline`）を `bind_deadline` で文脈（`ContextVar`）に置き、打ち切る
ときに `Deadline.cancel()` を呼ぶ。worker thread は文脈を引き継ぐ（`asyncio.to_thread` /
`contextvars.copy_context`）ので、thread の中の LLM の呼び出しは同じ期限を見て、

- 呼ぶ前に `check_deadline()` で期限を確かめ（過ぎていれば `DeadlineExceeded`）、
- 1 回の timeout を `remaining_seconds()` に縮め、
- 期限を過ぎたら再試行しない。

期限が無い文脈（取込・単体の CLI など）では何もしない（従来どおりの timeout と再試行）。

回答の時間の配分（#1516）: 回答フローの工程（章節の拡張・抽出、生成と監査の是正の round）は同じ期限を見て、
回答の確定（引用の照合・本文の組み立て・安全チェック・記録の保存）の分（`ANSWER_FINALIZE_RESERVE_SECONDS`）を
残す。工程の中の LLM の呼び出しには、その分を引いた子の期限（`Deadline(left, parent=...)`）を渡し、子の期限で
切れた（外側の期限はまだ残る）ことを `deadline_cut_short()` で見分けて、確かめ終わった分で回答を確定する。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 回答の確定（引用の照合・本文の組み立て・回答の安全チェック・記録の保存）に残す秒数（#1516）。
ANSWER_FINALIZE_RESERVE_SECONDS = 30.0
# 生成と監査の 1 組（grounded の round）の見積もりの既定（秒）。実測が無いとき（章節の拡張が生成に残す分・
# 是正の round の最初の見積もり）に使う。長い根拠の列挙の問で、推論するモデル（DeepSeek）は 1 組 125〜344 秒
# かかった（#1516）。
DEFAULT_GENERATION_ROUND_SECONDS = 240.0


class DeadlineExceeded(TimeoutError):
    """回答の期限を過ぎた・取り消された（それ以上 LLM を呼ばない）。"""


class Deadline:
    """回答の期限。`timeout_seconds` が None のときは時刻の期限を持たず、取り消しだけを伝える。

    `parent` を渡すと、親の期限と取り消しも引き継ぐ（期限は早いほう）。thread をまたいで
    共有するので、取り消しは `threading.Event` で持つ。
    """

    def __init__(self, timeout_seconds: float | None = None, *, parent: Deadline | None = None) -> None:
        self._expires_at = None if timeout_seconds is None else time.monotonic() + max(0.0, float(timeout_seconds))
        self._parent = parent
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """取り消す（以後の LLM の呼び出しは `DeadlineExceeded` になる）。"""
        self._cancelled.set()

    def expire(self) -> None:
        """自分の時刻の期限を今にする（#1548）。

        LLM の呼び出しが、残りの時間で出せる出力の上限に届いて終わらなかった（期限までに終わらないと分かった）
        ときに使う。取り消し（`cancel`）と違い、親（外側の期限）は切らないので、子の期限なら `cut_short` が真に
        なり、呼び出し側は時刻の期限で切れたときと同じく確かめ終わった分で確定する。
        """
        now = time.monotonic()
        self._expires_at = now if self._expires_at is None else min(self._expires_at, now)

    @property
    def cancelled(self) -> bool:
        if self._cancelled.is_set():
            return True
        return self._parent.cancelled if self._parent is not None else False

    def remaining(self) -> float | None:
        """残りの秒数。期限が無ければ None、取り消し済み・期限切れは 0。"""
        if self.cancelled:
            return 0.0
        remaining: float | None = None
        if self._expires_at is not None:
            remaining = max(0.0, self._expires_at - time.monotonic())
        if self._parent is not None:
            parent_remaining = self._parent.remaining()
            if parent_remaining is not None:
                remaining = parent_remaining if remaining is None else min(remaining, parent_remaining)
        return remaining

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0.0

    @property
    def cut_short(self) -> bool:
        """自分の時刻の期限で切れ、親（外側の期限）はまだ切れていない・取り消されていないか（#1516）。

        回答の確定の分を残した子の期限で LLM の呼び出しを打ち切ったときに真。外側の期限が切れた・取り消された
        （利用者が止めた・待つ側が抜けた）ときは偽で、呼び出し側は確かめ終わった分で確定せずに止める。
        """
        if self._cancelled.is_set() or self._expires_at is None or time.monotonic() < self._expires_at:
            return False
        return self._parent is None or not self._parent.expired

    def check(self) -> None:
        """期限を過ぎた・取り消されたら `DeadlineExceeded` を送出する。"""
        if self.cancelled:
            raise DeadlineExceeded("回答の処理が取り消されました。")
        if self.expired:
            raise DeadlineExceeded("回答の期限を過ぎました。")


_current: ContextVar[Deadline | None] = ContextVar("rag_engine_answer_deadline", default=None)


@contextmanager
def bind_deadline(deadline: Deadline | None) -> Iterator[Deadline | None]:
    """`deadline` をこの文脈の回答の期限にする（None は期限なし）。"""
    token = _current.set(deadline)
    try:
        yield deadline
    finally:
        _current.reset(token)


def current_deadline() -> Deadline | None:
    """この文脈の回答の期限（無ければ None）。"""
    return _current.get()


def remaining_seconds() -> float | None:
    """この文脈の回答の期限までの残りの秒数（期限が無ければ None）。"""
    deadline = _current.get()
    return None if deadline is None else deadline.remaining()


def check_deadline() -> None:
    """この文脈の回答の期限を過ぎていたら `DeadlineExceeded` を送出する（期限が無ければ何もしない）。"""
    deadline = _current.get()
    if deadline is not None:
        deadline.check()


def check_cancelled() -> None:
    """この文脈の回答が取り消されていたら `DeadlineExceeded` を送出する（#2691）。

    呼び出し元の切断・停止で待つ側が抜けた回答の thread が、次の工程を始めないために工程の境目で
    確かめる。時刻の期限で切れただけ（確かめ終わった分で確定する。#1516）では止めない。
    """
    deadline = _current.get()
    if deadline is not None and deadline.cancelled:
        raise DeadlineExceeded("回答の処理が取り消されました。")


def expire_deadline() -> None:
    """この文脈の回答の期限の時刻を今にする（期限が無ければ何もしない。`Deadline.expire`。#1548）。"""
    deadline = _current.get()
    if deadline is not None:
        deadline.expire()


def deadline_cut_short() -> bool:
    """この文脈の期限が、確定の分を残した子の期限で切れたか（外側の期限はまだ残る。#1516）。"""
    deadline = _current.get()
    return deadline is not None and deadline.cut_short
