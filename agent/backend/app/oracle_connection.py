"""アプリとしての Oracle AI Database 接続（共通 `.env` の `PLATFORM_ORACLE_*`。Thin mode。#215）。

- システム設定 > データベース の接続テスト（`features/agent/router.py`）・共通認証の store
  （`security/store.py`）・システムテーブル（`system_schema.py`）・Run の repository
  （`features/agent/runtime.py`）・定義の保存（`features/agent/control_plane_store.py`）が使う。
  Run の repository の別の接続設定（旧 `AGENT_RUNTIME_ORACLE_*`）は #764 で削除した。
- 接続テスト以外は、プロセスで 1 つの接続 pool（`pr_backend_core.oracle_pool`）から借りる（#793）。
  Wallet / mTLS の ADB へ要求のたびに接続し直すと、1 回ごとに handshake と認証の往復がかかるため。
  pool は接続引数が変わると作り直し、DB 設定の保存時と終了時に閉じる。大きさは
  `AGENT_ORACLE_POOL_MIN_CONNECTIONS` / `AGENT_ORACLE_POOL_MAX_CONNECTIONS`。
- システム設定 > データベース の接続テストは、保存前の候補の値を試すため pool を使わない。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pr_backend_core.oracle_dsn import apply_cli_connect_mode, dsn_without_tns_retry
from pr_backend_core.oracle_pool import OraclePoolSize, SharedOraclePool

DEFAULT_POOL_MIN_CONNECTIONS = 1
# 共通認証・システムテーブル・Run の保存・定義の保存がすべてこの pool を使うため、RAG / NL2SQL の
# pool（4）より少し大きくする。
DEFAULT_POOL_MAX_CONNECTIONS = 8

_PLATFORM_POOL = SharedOraclePool(
    name="agent-platform",
    size=OraclePoolSize(DEFAULT_POOL_MIN_CONNECTIONS, DEFAULT_POOL_MAX_CONNECTIONS),
)


def oracle_connect_kwargs(settings: Any) -> dict[str, object]:
    """`oracledb.connect` の引数（Thin mode。Wallet があれば mTLS）。

    再試行しない（`retry_count=0`）。Wallet の別名の接続記述子の `(retry_count=20)(retry_delay=3)`
    は引数より優先されるため、別名を再試行の設定を外した記述子に置き換える（DB の停止中に 1 回の
    接続で 1 分以上待たない。RAG / NL2SQL の接続テストと同じ。#1212）。
    """
    wallet_dir = str(settings.oracle_wallet_dir or "").strip()
    kwargs: dict[str, object] = {
        "user": settings.oracle_user,
        "dsn": dsn_without_tns_retry(str(settings.oracle_dsn or ""), wallet_dir or None),
        "retry_count": 0,
        "retry_delay": 0,
    }
    tcp_connect_timeout = float(settings.oracle_tcp_connect_timeout_seconds)
    if tcp_connect_timeout > 0:
        kwargs["tcp_connect_timeout"] = tcp_connect_timeout
    if str(settings.oracle_password or "").strip():
        kwargs["password"] = settings.oracle_password
    if wallet_dir:
        kwargs["config_dir"] = str(Path(wallet_dir).expanduser())
        kwargs["wallet_location"] = str(Path(wallet_dir).expanduser())
    if str(settings.oracle_wallet_password or "").strip():
        kwargs["wallet_password"] = settings.oracle_wallet_password
    return kwargs


def platform_oracle_connect_kwargs(settings: Any) -> dict[str, object]:
    """Settings（`PLATFORM_ORACLE_*`）から接続引数を作る。Wallet は解決済みの配置先を使う。"""
    dsn = str(settings.oracle_dsn or "").strip()
    user = str(settings.oracle_user or "").strip()
    if not dsn or not user:
        raise RuntimeError(
            "PLATFORM_ORACLE_DSN と PLATFORM_ORACLE_USER を"
            "共通 .env（platform/.env）に設定してください。"
        )
    return oracle_connect_kwargs(
        SimpleNamespace(
            oracle_user=user,
            oracle_dsn=dsn,
            oracle_password=settings.oracle_password,
            oracle_wallet_dir=settings.resolved_oracle_wallet_dir,
            oracle_wallet_password=settings.oracle_wallet_password,
            oracle_tcp_connect_timeout_seconds=settings.oracle_tcp_connect_timeout_seconds,
        )
    )


def _current_settings(settings: Any | None) -> Any:
    if settings is not None:
        return settings
    from app.settings import get_settings

    return get_settings()


def _pool_kwargs(settings: Any) -> dict[str, object]:
    """接続引数（未設定なら RuntimeError。pool は作らない）。pool の大きさも設定にそろえる。

    CLI（`enable_cli_connect_mode` を呼んだプロセス）だけ、接続記述子の TNS の再試行も外し、短い
    `tcp_connect_timeout` にする（#1779）。常駐の server の接続引数は変わらない。
    """
    kwargs = apply_cli_connect_mode(platform_oracle_connect_kwargs(settings))
    _PLATFORM_POOL.resize(
        OraclePoolSize.of(
            getattr(settings, "oracle_pool_min_connections", DEFAULT_POOL_MIN_CONNECTIONS),
            getattr(settings, "oracle_pool_max_connections", DEFAULT_POOL_MAX_CONNECTIONS),
        )
    )
    return kwargs


@contextmanager
def platform_oracle_connection(settings: Any | None = None) -> Iterator[Any]:
    """`PLATFORM_ORACLE_*` の接続を pool から借りる（#793）。

    未コミットの変更を持たない（例外時は rollback して返す。返すと未コミットの変更は rollback
    される）。`init_oracle_session` は pool が新しい接続ごとに当てる。
    """
    kwargs = _pool_kwargs(_current_settings(settings))
    with _PLATFORM_POOL.connection(kwargs) as connection:
        yield connection


def connect_platform_oracle() -> Any:
    """`PLATFORM_ORACLE_*` の接続を pool から借りる（呼び出し側が `with` で返し、commit する）。

    `with connection:` を抜ける（`close()`）と pool に返り、未コミットの変更は rollback される。
    """
    return _PLATFORM_POOL.acquire(_pool_kwargs(_current_settings(None)))


def close_platform_oracle_pool() -> None:
    """pool を閉じる（終了時・DB 設定の保存時）。次に借りるときに作り直す。"""
    _PLATFORM_POOL.close()
