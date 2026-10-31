"""Oracle AI Database の接続クライアント境界。

settings API は RAG と同じ `test_oracle_connection` / `close_oracle_pool` を使う。
NL2SQL 側では既存の `OracleNl2SqlAdapter` を最小接続テストに再利用する。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.features.nl2sql.oracle_adapter import OracleAdapterError, OracleNl2SqlAdapter
from app.settings import Settings, get_settings

type DbCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]


class OracleConnectionTimeoutError(TimeoutError):
    """Oracle 接続テストの timeout。"""


def close_oracle_pool() -> None:
    """共有 Oracle pool を閉じる。DeepSec control/data pool も同時に破棄する。"""
    from app.clients.oracle_runtime import close_oracle_pools

    close_oracle_pools()


async def test_oracle_connection(
    settings: Settings | None = None,
    db_call_runner: DbCallRunner | None = None,
) -> None:
    """Oracle へ 1 回だけ接続し、最小クエリで疎通を確認する。"""
    effective_settings = settings or get_settings()
    runner = db_call_runner or _run_db_test_call_in_thread
    timeout_seconds = float(getattr(effective_settings, "oracle_db_test_timeout_seconds", 15.0))
    try:
        await asyncio.wait_for(
            runner(lambda: _test_oracle_connection_sync(effective_settings)),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise OracleConnectionTimeoutError(
            f"Oracle AI Database の接続テストが {timeout_seconds:g} 秒でタイムアウトしました。"
            "データベースの起動状態、Wallet サービス名、ネットワーク到達性を確認してください。"
        ) from exc


def _test_oracle_connection_sync(settings: Settings) -> None:
    ok, message = OracleNl2SqlAdapter(settings).test_connection()
    if not ok:
        raise OracleAdapterError(message)


async def _run_db_test_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期 python-oracledb 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)
