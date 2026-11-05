"""Oracle AI Database への接続（ユーザー・パスワード・DSN）を扱う。"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_engine.config import load_dotenv

DEFAULT_ADB_DB_USER = "admin"


@dataclass(frozen=True)
class AdbSettings:
    """Oracle 接続設定。DSN は `host:port/service_name` か接続記述子です。"""

    dsn: str
    db_user: str
    db_password: str
    tcp_connect_timeout: float
    connect_retry_count: int
    connect_retry_delay: int
    # process 内の接続プールの最大接続数。0 でプールを使わず毎回 connect する。
    pool_max: int = 4


def load_adb_settings(
    env_path: str | Path = ".env",
    environ: Mapping[str, str] | None = None,
) -> AdbSettings:
    """接続設定を読み込みます。DSN とパスワードは必須です。"""
    env_file = Path(env_path).expanduser()
    if environ is None:
        load_dotenv(env_file)
        env = os.environ
    else:
        env = environ

    dsn = _required(env, "ADB_DSN")
    db_user = env.get("ADB_DB_USER", DEFAULT_ADB_DB_USER).strip() or DEFAULT_ADB_DB_USER
    db_password = _required(env, "ADB_DB_PASSWORD")
    tcp_connect_timeout = _env_float(env, "ADB_CONNECT_TIMEOUT_SECONDS", 10.0)
    connect_retry_count = _env_int(env, "ADB_CONNECT_RETRY_COUNT", 0)
    connect_retry_delay = _env_int(env, "ADB_CONNECT_RETRY_DELAY_SECONDS", 0)
    pool_max = max(0, _env_int(env, "ADB_POOL_MAX", 4))

    return AdbSettings(
        dsn=dsn,
        db_user=db_user,
        db_password=db_password,
        tcp_connect_timeout=tcp_connect_timeout,
        connect_retry_count=connect_retry_count,
        connect_retry_delay=connect_retry_delay,
        pool_max=pool_max,
    )


_POOLS: dict[tuple[AdbSettings, int], Any] = {}
_POOLS_LOCK = threading.Lock()


def connect_adb_thin(settings: AdbSettings, oracledb_module: Any | None = None) -> Any:
    """Thin 接続を返します。

    `settings.pool_max > 0` で driver が `create_pool` を持つときは、設定ごとに 1 つ作る process 内プールから
    `acquire()` した接続を返す（#887）。プール接続は `close()`（`with` の終了）でプールへ戻る。それ以外は
    従来どおり `connect()` で新規接続を開く。接続を閉じる責任は呼び出し元にあり、driver の例外はそのまま伝播する。
    """
    if oracledb_module is None:
        try:
            import oracledb as oracledb_module
        except ImportError as exc:
            raise RuntimeError("The oracledb package is required for ADB Thin connections.") from exc

    # CLOB を str、BLOB を bytes として直接受け取る。既定（LOB ロケータ）では chunk 読込のたびに行 × CLOB 列
    # （本文・検索文・metadata など 7 列）の .read() 往復が発生する。読み手（_lob_to_str / _read_json）は
    # str / bytes をそのまま扱う。python-oracledb の module 既定なのでプロセス全体に効く (#846)。
    defaults = getattr(oracledb_module, "defaults", None)
    if defaults is not None and getattr(defaults, "fetch_lobs", None) is not False:
        defaults.fetch_lobs = False

    if settings.pool_max > 0 and hasattr(oracledb_module, "create_pool"):
        return _connection_pool(settings, oracledb_module).acquire()

    return oracledb_module.connect(
        user=settings.db_user,
        password=settings.db_password,
        params=_connect_params(settings, oracledb_module.ConnectParams),
    )


def _connect_params(settings: AdbSettings, params_class: Any) -> Any:
    """ConnectParams / PoolParams を DSN・timeout・retry の設定で組み立てる。"""
    params = params_class(
        tcp_connect_timeout=settings.tcp_connect_timeout,
        retry_count=settings.connect_retry_count,
        retry_delay=settings.connect_retry_delay,
    )
    params.parse_connect_string(settings.dsn)
    params.set(
        tcp_connect_timeout=settings.tcp_connect_timeout,
        retry_count=settings.connect_retry_count,
        retry_delay=settings.connect_retry_delay,
    )
    return params


def _connection_pool(settings: AdbSettings, oracledb_module: Any) -> Any:
    """設定ごとの接続プールを返す（無ければ lock の下で 1 回だけ作る）。

    min=0 なので起動時に接続は開かず、初回の acquire で開く。増分 1、最大 `pool_max`。driver 既定の
    ping_interval（60 秒）で長時間放置後の切断を検出する。プールは process 終了まで保持する。
    """
    key = (settings, id(oracledb_module))
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = oracledb_module.create_pool(
                user=settings.db_user,
                password=settings.db_password,
                params=_connect_params(settings, oracledb_module.PoolParams),
                min=0,
                max=settings.pool_max,
                increment=1,
            )
            _POOLS[key] = pool
    return pool


def smoke_test_adb_connection(settings: AdbSettings) -> int:
    """ADB に接続して select 1 による疎通確認を実行します。"""
    with connect_adb_thin(settings) as connection, connection.cursor() as cursor:
        cursor.execute("select 1 from dual")
        row = cursor.fetchone()
    if not row:
        raise RuntimeError("ADB smoke test returned no rows.")
    return int(row[0])


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return default
