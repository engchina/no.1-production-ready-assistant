"""取込の job が、重い段の枠を解析の区間の後に返すための仕組み(#2702)。

worker は重い段(ファイル準備・抽出)の job の同時実行数を、解析サービスの CPU・メモリを守るために
全体の枠より小さく絞る(#2027)。ところが同じ job の中の項目抽出は、解析の後に OCI Enterprise AI の
応答を待つだけで CPU を使わない。job が終わるまで重い段の枠を持つと、LLM の応答待ちの間も次の文書の
解析が始まらず、全体の枠が空いていても重い段の上限の数でしか進まない。

そこで job の中で解析の区間が終わったら ``release_heavy_ingestion_slot()`` を呼び、worker に重い段の
枠を返す(全体の枠は job が終わるまで持つ)。worker は job を始めるときに ``heavy_slot_releaser`` で
返し方を置く。

- in-process で job を実行するときは、置いた関数をそのまま呼ぶ。
- 子プロセスで job を実行するとき(process isolation)は、親が pipe の書き込み側の fd を子へ渡し
  (``--heavy-slot-release-fd``)、子は返すときに 1 byte を書く。親は読めたら枠を返す。子が書かずに
  終わったとき(失敗・取り消しを含む)は、job の終わりで worker が枠を返す。
- worker の外(同期の取込・評価・テスト)では何もしない。
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Iterator
from contextvars import ContextVar

logger = logging.getLogger(__name__)

HeavySlotRelease = Callable[[], None]

_RELEASE: ContextVar[HeavySlotRelease | None] = ContextVar(
    "rag_ingestion_heavy_slot_release", default=None
)


def current_heavy_slot_release() -> HeavySlotRelease | None:
    """今の job の重い段の枠の返し方(worker の外では None)。"""
    return _RELEASE.get()


@contextlib.contextmanager
def heavy_slot_releaser(release: HeavySlotRelease) -> Iterator[None]:
    """この中で ``release_heavy_ingestion_slot()`` が呼ばれたら ``release`` を 1 回だけ呼ぶ。"""
    released = False

    def release_once() -> None:
        nonlocal released
        if released:
            return
        released = True
        release()

    token = _RELEASE.set(release_once)
    try:
        yield
    finally:
        _RELEASE.reset(token)


def release_heavy_ingestion_slot() -> None:
    """解析の区間が終わった。以降は LLM の応答待ちが主のため、重い段の枠を返す。

    返し方が無ければ(worker の外)何もしない。返す失敗は取込を止めない(job の終わりで返る)。
    """
    release = _RELEASE.get()
    if release is None:
        return
    try:
        release()
    except Exception:  # noqa: BLE001 - 枠を早く返すのは補助。失敗しても job の終わりで返る。
        logger.warning("ingestion_heavy_slot_release_failed", exc_info=True)


def fd_heavy_slot_release(fd: int) -> HeavySlotRelease:
    """子プロセスの側: 親から渡された pipe の fd に 1 byte を書いて閉じる返し方。"""

    def release() -> None:
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)

    return release
