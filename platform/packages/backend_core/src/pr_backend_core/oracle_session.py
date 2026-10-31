"""Oracle の接続ごとの初期化（3 製品共通）。

python-oracledb に依存しない（接続オブジェクトを受け取るだけ）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# 新しい接続ごとに 1 回の ALTER SESSION で次の 2 つを設定する（往復を増やさない）。
#
# - RESULT_CACHE_MODE = MANUAL: ADB の既定は result_cache_mode=FORCE。行の外にある大きな
#   JSON(OSON) 列を読む SELECT が result cache の内部エラー
#   (ORA-700 [qesrcCopyQBufferToOpn:large loc])を起こし、heap の破損
#   (ORA-600 [17114] / [kghfrh:ds] など)と接続断が連鎖したため、アプリの接続では使わない(#333)。
# - TIME_ZONE = '+00:00': 時刻は UTC で保存する(#3255)。python-oracledb は接続のときに、
#   process の地方時の offset を session の時間帯にする(thin mode は ORA_SDTZ が無いとき)。
#   また datetime の tzinfo を無視して TIMESTAMP として bind するため、Compute の OS を
#   Asia/Tokyo にすると、UTC の時刻を TIMESTAMP WITH TIME ZONE の列に書く・比べる箇所が
#   9 時間ずれる。OS の時間帯に左右されないよう UTC に固定する。地域名('UTC')ではなく
#   offset にする(名前の時間帯の値は thin mode で読めない: DPY-3022。#3241)。
SESSION_INIT_SQL = "ALTER SESSION SET RESULT_CACHE_MODE = MANUAL TIME_ZONE = '+00:00'"
# session の時間帯を UTC に戻す（`local_session_time_zone` の後）。
SESSION_UTC_SQL = "ALTER SESSION SET TIME_ZONE = '+00:00'"
_SESSION_OFFSET = re.compile(r"[+-](?:0\d|1[0-4]):[0-5]\d")


def init_oracle_session(connection: Any, requested_tag: str | None = None) -> None:
    """新しい接続の初期化。pool の ``session_callback`` にも、単発の接続にも使う。"""
    del requested_tag
    with connection.cursor() as cursor:
        cursor.execute(SESSION_INIT_SQL)


@contextmanager
def local_session_time_zone(connection: Any, offset: str) -> Iterator[None]:
    """業務データの SQL を実行する間だけ、session の時間帯を設定の時間帯の offset にする（#3265）。

    `CURRENT_DATE` / `CURRENT_TIMESTAMP` / `LOCALTIMESTAMP` が設定の時間帯の今を返す
    （`SYSDATE` / `SYSTIMESTAMP` は session に関係なく DB のサーバーの時刻）。抜けるときに
    `'+00:00'` へ戻し、pool に返した接続でアプリの表の時刻が UTC のままになるようにする。
    `offset` は `pr_backend_core.timezones.session_time_zone_offset` の値（`+09:00` の形）。
    UTC（`+00:00`）なら何もしない（往復を増やさない）。
    """
    if not _SESSION_OFFSET.fullmatch(offset):
        raise ValueError(f"session の時間帯は +HH:MM の形で指定してください: {offset!r}")
    if offset in {"+00:00", "-00:00"}:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER SESSION SET TIME_ZONE = '{offset}'")  # nosec B608 - 形を検証済み
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(SESSION_UTC_SQL)
