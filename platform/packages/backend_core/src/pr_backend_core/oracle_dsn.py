"""DSN の接続記述子から再試行の設定を外す（#1212）。

ADB の TLS の接続文字列は `(retry_count=20)(retry_delay=3)` を持つ。python-oracledb は接続記述子の
値を `connect()` / `create_pool()` の `retry_count` / `retry_delay` より優先するため、DB の停止中
（`ORA-12514` / `DPY-6001`）は 1 回の接続に 1 分以上かかる。接続を待たずに失敗を返したいところ
（接続確認・接続 pool）は、記述子から再試行の設定を外す（簡易接続の文字列はそのまま）。

接続先・資格情報はログに出さない。

1 回で終わる CLI（system schema の CLI など。配備が retry する）は、DB の停止中・届かないときに
待たずに失敗を返す（#1779）。`enable_cli_connect_mode()` を最初に呼ぶと、製品の接続 pool の
接続引数（`apply_cli_connect_mode`）が、記述子の TNS の再試行を外し（`dsn_without_retry`）、
`retry_count=0`・短い `tcp_connect_timeout` になる。常駐の server は呼ばない（接続の設定は
今までどおりで、一時の切断に強いまま）。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping

_RETRY_COUNT_RE = re.compile(r"\(\s*retry_count\s*=\s*\d+\s*\)", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"\(\s*retry_delay\s*=\s*\d+\s*\)", re.IGNORECASE)

# CLI の接続の TCP の接続の上限（秒）。設定の値がこれより長くても、CLI ではこれで打ち切る（#1779）。
CLI_TCP_CONNECT_TIMEOUT_SECONDS = 5.0

_cli_connect_mode = threading.Event()


def strip_tns_retry_settings(descriptor: str) -> str:
    """接続記述子から `(retry_count=N)` と `(retry_delay=N)` を取り除く。"""
    return _RETRY_DELAY_RE.sub("", _RETRY_COUNT_RE.sub("", descriptor))


def dsn_without_retry(dsn: str) -> str:
    """接続記述子の DSN から TNS の再試行の設定を外す（簡易接続はそのまま）。"""
    if dsn.lstrip().startswith("("):
        return strip_tns_retry_settings(dsn)
    return dsn


def connect_kwargs_without_retry(
    connect_kwargs: Mapping[str, object],
    *,
    max_tcp_connect_timeout_seconds: float = CLI_TCP_CONNECT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """接続引数から再試行を外す（DSN の TNS の再試行・`retry_count`・`retry_delay`）。

    `tcp_connect_timeout` は、設定の値と `max_tcp_connect_timeout_seconds` の短い方にする（無い・
    不正なら上限の値）。ほかの引数（資格情報・pool の引数）はそのまま。
    """
    kwargs = dict(connect_kwargs)
    dsn = kwargs.get("dsn")
    if isinstance(dsn, str) and dsn.strip():
        kwargs["dsn"] = dsn_without_retry(dsn)
    kwargs["retry_count"] = 0
    kwargs["retry_delay"] = 0
    limit = float(max_tcp_connect_timeout_seconds)
    timeout = kwargs.get("tcp_connect_timeout")
    if isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0:
        kwargs["tcp_connect_timeout"] = min(float(timeout), limit)
    else:
        kwargs["tcp_connect_timeout"] = limit
    return kwargs


def enable_cli_connect_mode() -> None:
    """このプロセスの DB の接続を、再試行しない CLI の接続にする（CLI の最初に呼ぶ。#1779）。

    pool を作る前に呼ぶ（作った後の pool は変わらない）。常駐の server・worker は呼ばない。
    """
    _cli_connect_mode.set()


def disable_cli_connect_mode() -> None:
    """CLI の接続をやめる（テスト用）。"""
    _cli_connect_mode.clear()


def cli_connect_mode_enabled() -> bool:
    return _cli_connect_mode.is_set()


def apply_cli_connect_mode(connect_kwargs: Mapping[str, object]) -> dict[str, object]:
    """CLI の接続なら再試行を外した接続引数、そうでなければ元のままの写しを返す。

    製品の接続 pool（と、その接続引数を使う接続の失敗の診断）が、pool を作る前に通す。
    """
    if _cli_connect_mode.is_set():
        return connect_kwargs_without_retry(connect_kwargs)
    return dict(connect_kwargs)


__all__ = [
    "CLI_TCP_CONNECT_TIMEOUT_SECONDS",
    "apply_cli_connect_mode",
    "cli_connect_mode_enabled",
    "connect_kwargs_without_retry",
    "disable_cli_connect_mode",
    "dsn_without_retry",
    "enable_cli_connect_mode",
    "strip_tns_retry_settings",
]
