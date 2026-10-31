"""Oracle driver 初期化と、システムの表の管理 DDL 用の control pool。"""

from __future__ import annotations

import hashlib
import importlib
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache
from typing import Any

from pr_backend_core.oracle_dsn import apply_cli_connect_mode
from pr_backend_core.oracle_session import init_oracle_session

from app.features.nl2sql.oracle_adapter import (
    close_auth_connection_pool,
    close_runtime_connection_pool,
    close_state_connection_pool,
    oracle_connect_kwargs,
)
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
# pool が埋まっているときに待つ上限（ミリ秒）。無期限に待たない（`SharedOraclePool` と同じ）。
_POOL_WAIT_TIMEOUT_MS = 30_000


class OraclePoolManager:
    """システムの表の管理 DDL（migration・schema の作成）が使う control pool を持つ。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._oracledb: Any | None = None
        self._control_pool: Any | None = None
        # pool を作ったときの接続の設定の指紋。別の製品や別の worker が保存したデータベースの
        # 設定（#2102）で `get_settings()` が値を読み直したら、古い pool を作り直す。
        self._control_pool_identity: str | None = None
        self._lock = threading.RLock()

    @contextmanager
    def control_connection(self) -> Iterator[Any]:
        pool = self._get_pool()
        connection = pool.acquire()
        try:
            yield connection
        finally:
            connection.close()

    def close(self) -> None:
        with self._lock:
            pool = self._control_pool
            if pool is not None:
                with suppress(Exception):
                    pool.close(force=True)
            self._control_pool = None
            self._control_pool_identity = None

    def _pool_identity(self) -> str:
        """pool の接続の設定の指紋（資格情報を含むため、生値は保持・出力しない）。"""
        settings = self.settings
        values = (
            settings.oracle_user,
            settings.oracle_password,
            settings.oracle_dsn,
            settings.oracle_connection_security,
            settings.resolved_oracle_wallet_dir,
            settings.oracle_wallet_password,
            settings.nl2sql_oracle_connect_timeout_seconds,
        )
        return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()

    def _get_pool(self) -> Any:
        with self._lock:
            current = self._control_pool
            current_identity = self._control_pool_identity
            # 指紋の記録が無い pool（ここで作っていない。テストが差し込んだもの）は作り直さない。
            if (
                current is not None
                and current_identity is not None
                and current_identity != self._pool_identity()
            ):
                # 接続の設定が変わった。古い設定の接続を使い続けないよう、古い pool を閉じる。
                self._control_pool = None
                self._control_pool_identity = None
                with suppress(Exception):
                    current.close(force=True)
                logger.info("oracle_pool_settings_changed", extra={"pool": "nl2sql-control"})
            current = self._control_pool
            if current is not None:
                return current
            oracledb = self._load_oracledb()
            self._initialize_driver(oracledb)
            kwargs = oracle_connect_kwargs(self.settings)
            # CLI（`enable_cli_connect_mode` を呼んだプロセス）だけ、接続記述子の TNS の再試行も
            # 外し、短い tcp_connect_timeout にする（#1779）。常駐の server の接続引数は変わらない。
            kwargs = apply_cli_connect_mode(kwargs)
            # result cache を使わない（ADB の内部エラーと接続断を避ける。#333）。
            kwargs.update(min=1, max=4, increment=1, session_callback=init_oracle_session)
            timed_wait = getattr(oracledb, "POOL_GETMODE_TIMEDWAIT", None)
            if timed_wait is not None:
                # 接続を借りる待ちを無期限にしない（要求・worker のスレッドを止め続けない）。
                kwargs.update(getmode=timed_wait, wait_timeout=_POOL_WAIT_TIMEOUT_MS)
            pool = oracledb.create_pool(**kwargs)
            self._control_pool = pool
            self._control_pool_identity = self._pool_identity()
            return pool

    def _load_oracledb(self) -> Any:
        if self._oracledb is None:
            self._oracledb = importlib.import_module("oracledb")
        return self._oracledb

    def _initialize_driver(self, oracledb: Any) -> None:
        if self.settings.oracle_driver_mode == "thin":
            return
        if not self.settings.oracle_client_lib_dir:
            return
        if getattr(oracledb, "is_thin_mode", lambda: True)():
            oracledb.init_oracle_client(lib_dir=self.settings.oracle_client_lib_dir)


@lru_cache
def get_oracle_pool_manager() -> OraclePoolManager:
    return OraclePoolManager(get_settings())


def close_oracle_pools() -> None:
    if get_oracle_pool_manager.cache_info().currsize:
        get_oracle_pool_manager().close()
    get_oracle_pool_manager.cache_clear()
    # 共通認証の接続 pool も閉じる（DB 設定の保存時・終了時。#793）。
    close_auth_connection_pool()
    # 状態の保存先の接続 pool も閉じる（#830）。
    close_state_connection_pool()
    # 業務データの読み取りの接続 pool も閉じる（#904）。
    close_runtime_connection_pool()
