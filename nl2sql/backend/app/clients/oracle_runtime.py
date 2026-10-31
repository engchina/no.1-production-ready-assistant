"""Oracle driver 初期化と DeepSec 用 Thin-only control/data pool。"""

from __future__ import annotations

import hashlib
import importlib
import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache
from typing import Any

from pr_backend_core.oracle_dsn import apply_cli_connect_mode
from pr_backend_core.oracle_session import init_oracle_session

from app.features.nl2sql.oracle_adapter import (
    OracleAdapterError,
    close_auth_connection_pool,
    close_runtime_connection_pool,
    close_state_connection_pool,
    ensure_deepsec_thin_mode,
    oracle_connect_kwargs,
)
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
_ORACLE_INVALID_CREDENTIAL_RE = re.compile(r"\bORA-01017\b", re.IGNORECASE)
_ORACLE_ACCOUNT_LOCKED_RE = re.compile(r"\bORA-28000\b", re.IGNORECASE)
DEEPSEC_DATA_USER_INVALID_CREDENTIAL_MESSAGE = (
    "DeepSec DATA USER の Oracle ログインに失敗しました。Deep Data Security 画面で "
    "DATA USER パスワードを保存し直し、Oracle END USER へ同期してください。"
    "解消しない場合は DATA USER 認証の「Oracle へ同期」を再実行してください。"
)
DEEPSEC_DATA_USER_LOCKED_MESSAGE = (
    "DeepSec DATA USER の Oracle アカウントがロックされているため、業務データに接続できません。"
    "データベース管理者にロックの解除を依頼し、Deep Data Security 画面で DATA USER パスワードを"
    "保存し直して Oracle END USER へ同期してください。"
)
# DATA USER のログインが資格情報・アカウントのロックで失敗したら、この秒数のあいだ再接続しない。
# 要求のたびにログインを試すと、1 回ごとに接続の確立（遅延の大きいネットワークで約 3 秒）を待ち、
# 失敗したログインの回数で Oracle がアカウントをロックし続ける。設定の保存（`close_oracle_pools`）で
# 忘れる。
DATA_USER_LOGIN_FAILURE_BACKOFF_SECONDS = 60.0
# 時刻の取得（テストで差し替える）。
_monotonic = time.monotonic
# pool が埋まっているときに待つ上限（ミリ秒）。無期限に待たない（`SharedOraclePool` と同じ）。
_POOL_WAIT_TIMEOUT_MS = 30_000


class OraclePoolManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._oracledb: Any | None = None
        self._control_pool: Any | None = None
        self._data_pool: Any | None = None
        # DATA USER の pool を作ったときの認証情報（#1022）。別 worker が DeepSec 画面で
        # pool を作ったときの接続の設定の指紋。DATA USER の password の保存（#1022）・別の製品や
        # 別の worker が保存したデータベースの設定（#2102）で `get_settings()` が値を読み直したら、
        # 古い pool を作り直す。
        self._data_pool_identity: str | None = None
        self._control_pool_identity: str | None = None
        self._lock = threading.RLock()
        # DATA USER のログインの失敗（時刻・利用者に出す文）。期間内は再接続せずに失敗させる。
        self._data_login_failure: tuple[float, str] | None = None

    def validate_deepsec_configuration(self) -> None:
        """共有 DATA USER の direct logon に必要な DeepSec 設定を検証する。"""
        ensure_deepsec_thin_mode(self.settings)
        if (
            self.settings.oracle_deepsec_enabled
            and not self.settings.oracle_deepsec_data_user_password
        ):
            raise OracleAdapterError(
                "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD を設定してください。"
            )

    def validate_deepsec_control_configuration(self) -> None:
        """DeepSec 管理 DDL 用の control-plane 設定を検証する。"""
        ensure_deepsec_thin_mode(self.settings)

    def validate_data_user_login(self) -> None:
        """保存済み DATA USER 認証情報で direct logon できることだけを検証する。"""
        ensure_deepsec_thin_mode(self.settings)
        if not self.settings.oracle_deepsec_data_user_password:
            raise OracleAdapterError(
                "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD を設定してください。"
            )
        connection: Any | None = None
        pool: Any | None = None
        try:
            with self._lock:
                oracledb = self._load_oracledb()
                self._initialize_driver(oracledb)
                kwargs = oracle_connect_kwargs(
                    self.settings,
                    user=self.settings.oracle_deepsec_data_user,
                    password=self.settings.oracle_deepsec_data_user_password,
                )
                kwargs.update(min=1, max=1, increment=1, session_callback=init_oracle_session)
                pool = oracledb.create_pool(**kwargs)
            connection = pool.acquire()
        except Exception as exc:
            if _is_invalid_data_user_credential_error(exc):
                raise OracleAdapterError(DEEPSEC_DATA_USER_INVALID_CREDENTIAL_MESSAGE) from exc
            raise
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            if pool is not None:
                with suppress(Exception):
                    pool.close(force=True)

    @contextmanager
    def control_connection(self) -> Iterator[Any]:
        pool = self._get_pool(data_plane=False)
        connection = pool.acquire()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def data_connection(self, actor_user_uuid: str) -> Iterator[Any]:
        if not actor_user_uuid:
            raise OracleAdapterError("データ接続には認証済み actor_user_uuid が必要です。")
        with self._data_connection_raw() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.callproc("NL2SQL_DEEPSEC_CTX_PKG.SET_APP_USER_UUID", [actor_user_uuid])
                yield connection
            finally:
                self._clear_context_or_drop(connection)

    @contextmanager
    def unscoped_data_connection(self) -> Iterator[Any]:
        """DeepSec verification の no-context probe 専用。"""
        with self._data_connection_raw() as connection:
            self._clear_context_or_drop(connection)
            yield connection

    @contextmanager
    def _data_connection_raw(self) -> Iterator[Any]:
        self.validate_deepsec_configuration()
        if not self.settings.oracle_deepsec_enabled:
            raise OracleAdapterError("Deep Data Security が有効ではありません。")
        self._raise_if_data_login_recently_failed()
        pool: Any | None = None
        try:
            pool = self._get_pool(data_plane=True)
            connection = pool.acquire()
        except Exception as exc:
            message = _data_user_login_failure_message(exc)
            if message is None:
                raise
            self._remember_data_login_failure(pool, message, exc)
            raise OracleAdapterError(message) from exc
        self._data_login_failure = None
        self._apply_call_timeout(connection)
        try:
            yield connection
        finally:
            try:
                connection.close()
            except Exception:
                logger.warning("oracle_data_connection_close_failed", exc_info=True)
                self._drop_connection_once(pool, connection)

    def close(self) -> None:
        with self._lock:
            for pool in (self._data_pool, self._control_pool):
                if pool is not None:
                    with suppress(Exception):
                        pool.close(force=True)
            self._data_pool = None
            self._data_pool_identity = None
            self._control_pool = None
            self._control_pool_identity = None
            self._data_login_failure = None

    def _raise_if_data_login_recently_failed(self) -> None:
        """直前の DATA USER のログインの失敗から間もなければ、接続を試さずに同じ文で失敗させる。"""

        failure = self._data_login_failure
        if failure is None:
            return
        failed_at, message = failure
        if _monotonic() - failed_at < DATA_USER_LOGIN_FAILURE_BACKOFF_SECONDS:
            raise OracleAdapterError(message)
        self._data_login_failure = None

    def _remember_data_login_failure(
        self, pool: Any | None, message: str, exc: BaseException
    ) -> None:
        """ログインの失敗を覚え、作った pool を捨てる（次は新しい pool で 1 回だけ試す）。"""

        self._data_login_failure = (_monotonic(), message)
        with self._lock:
            if pool is not None and self._data_pool is pool:
                self._data_pool = None
            else:
                pool = None
        if pool is not None:
            with suppress(Exception):
                pool.close(force=True)
        logger.warning(
            "oracle_deepsec_data_user_login_failed",
            extra={
                "exception_type": type(exc).__name__,
                "account_locked": bool(_ORACLE_ACCOUNT_LOCKED_RE.search(str(exc))),
                "backoff_seconds": DATA_USER_LOGIN_FAILURE_BACKOFF_SECONDS,
            },
        )

    def _pool_identity(self, *, data_plane: bool) -> str:
        """pool の接続の設定の指紋（資格情報を含むため、生値は保持・出力しない）。"""
        settings = self.settings
        user, password = (
            (settings.oracle_deepsec_data_user, settings.oracle_deepsec_data_user_password)
            if data_plane
            else (settings.oracle_user, settings.oracle_password)
        )
        values = (
            user,
            password,
            settings.oracle_dsn,
            settings.oracle_connection_security,
            settings.resolved_oracle_wallet_dir,
            settings.oracle_wallet_password,
            settings.nl2sql_oracle_connect_timeout_seconds,
        )
        return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()

    def _get_pool(self, *, data_plane: bool) -> Any:
        with self._lock:
            current = self._data_pool if data_plane else self._control_pool
            current_identity = (
                self._data_pool_identity if data_plane else self._control_pool_identity
            )
            # 指紋の記録が無い pool（ここで作っていない。テストが差し込んだもの）は作り直さない。
            if (
                current is not None
                and current_identity is not None
                and current_identity != self._pool_identity(data_plane=data_plane)
            ):
                # 接続の設定が変わった。古い設定の接続を使い続けないよう、古い pool を閉じる
                # （DATA USER の password の保存の後と同じ。#1022）。
                if data_plane:
                    self._data_pool = None
                    self._data_pool_identity = None
                else:
                    self._control_pool = None
                    self._control_pool_identity = None
                with suppress(Exception):
                    current.close(force=True)
                logger.info(
                    "oracle_pool_settings_changed",
                    extra={"pool": "nl2sql-deepsec-data" if data_plane else "nl2sql-control"},
                )
            current = self._data_pool if data_plane else self._control_pool
            if current is not None:
                return current
            ensure_deepsec_thin_mode(self.settings)
            oracledb = self._load_oracledb()
            self._initialize_driver(oracledb)
            if data_plane:
                kwargs = oracle_connect_kwargs(
                    self.settings,
                    user=self.settings.oracle_deepsec_data_user,
                    password=self.settings.oracle_deepsec_data_user_password,
                )
            else:
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
            try:
                pool = oracledb.create_pool(**kwargs)
            except Exception as exc:
                if data_plane and _is_invalid_data_user_credential_error(exc):
                    raise OracleAdapterError(DEEPSEC_DATA_USER_INVALID_CREDENTIAL_MESSAGE) from exc
                raise
            if data_plane:
                self._data_pool = pool
                self._data_pool_identity = self._pool_identity(data_plane=True)
            else:
                self._control_pool = pool
                self._control_pool_identity = self._pool_identity(data_plane=False)
            return pool

    def _load_oracledb(self) -> Any:
        if self._oracledb is None:
            self._oracledb = importlib.import_module("oracledb")
        return self._oracledb

    def _initialize_driver(self, oracledb: Any) -> None:
        ensure_deepsec_thin_mode(self.settings)
        if self.settings.oracle_driver_mode == "thin":
            return
        if not self.settings.oracle_client_lib_dir:
            return
        if getattr(oracledb, "is_thin_mode", lambda: True)():
            oracledb.init_oracle_client(lib_dir=self.settings.oracle_client_lib_dir)

    def _apply_call_timeout(self, connection: Any) -> None:
        call_timeout_ms = int(
            max(1.0, float(self.settings.nl2sql_oracle_call_timeout_seconds)) * 1000
        )
        if hasattr(connection, "call_timeout"):
            connection.call_timeout = call_timeout_ms

    def _clear_context_or_drop(self, connection: Any) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.callproc("NL2SQL_DEEPSEC_CTX_PKG.CLEAR_APP_USER")
        except Exception as exc:
            logger.warning("oracle_deepsec_context_clear_failed", exc_info=True)
            pool = self._data_pool
            if pool is not None:
                self._drop_connection_once(pool, connection)
            raise OracleAdapterError(
                "DeepSec context を消去できないため接続を破棄しました。"
            ) from exc

    def _drop_connection_once(self, pool: Any, connection: Any) -> None:
        marker = "_nl2sql_oracle_pool_dropped"
        if getattr(connection, marker, False):
            return
        with suppress(Exception):
            setattr(connection, marker, True)
        with suppress(Exception):
            pool.drop(connection)


def _is_invalid_data_user_credential_error(exc: Exception) -> bool:
    return bool(_ORACLE_INVALID_CREDENTIAL_RE.search(str(exc)))


def _data_user_login_failure_message(exc: BaseException) -> str | None:
    """DATA USER のログインの失敗（資格情報・アカウントのロック）なら利用者に出す文を返す。"""

    text = str(exc)
    cause = exc.__cause__
    if cause is not None:
        text = f"{text} {cause}"
    if _ORACLE_ACCOUNT_LOCKED_RE.search(text):
        return DEEPSEC_DATA_USER_LOCKED_MESSAGE
    if _ORACLE_INVALID_CREDENTIAL_RE.search(text):
        return DEEPSEC_DATA_USER_INVALID_CREDENTIAL_MESSAGE
    return None


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
    # 業務データの読み取り・Select AI の生成の接続 pool も閉じる（#904）。
    close_runtime_connection_pool()
