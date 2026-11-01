"""Oracle Database の直接接続と ADB mTLS 接続（配置済みの Wallet）を扱う。"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_engine.config import load_dotenv

DEFAULT_ADB_DB_NAME = "AIRAGADB"
DEFAULT_ADB_DB_USER = "admin"
DEFAULT_ADB_TNS_SERVICE = "medium"
REQUIRED_MTLS_WALLET_FILES = ("tnsnames.ora", "ewallet.pem")


@dataclass(frozen=True)
class AdbSettings:
    """Oracle 接続設定。TCP Easy Connect では Wallet は不要です。"""

    db_name: str
    tns_alias: str
    wallet_dir: Path
    wallet_password: str
    db_user: str
    db_password: str
    tcp_connect_timeout: float
    connect_retry_count: int
    connect_retry_delay: int
    # process 内の接続プールの最大接続数。0 でプールを使わず毎回 connect する。
    pool_max: int = 4

    @property
    def uses_wallet(self) -> bool:
        """TCP Easy Connect のみ Wallet を省略し、既存 mTLS 設定を維持します。"""
        return not _is_tcp_easy_connect(self.tns_alias)


def _is_tcp_easy_connect(dsn: str) -> bool:
    """host:port/service または tcp:// 形式を判別します。TCPS は除外します。"""
    value = dsn.strip().lower()
    return "/" in value and not value.startswith("(") and ("://" not in value or value.startswith("tcp://"))


def load_adb_settings(
    env_path: str | Path = ".env",
    environ: Mapping[str, str] | None = None,
) -> AdbSettings:
    """接続設定を読み込み、TCP Easy Connect 以外は Wallet のパスワードを必須とします。"""
    env_file = Path(env_path).expanduser()
    base_dir = env_file.resolve().parent
    if environ is None:
        load_dotenv(env_file)
        env = os.environ
    else:
        env = environ

    db_name = env.get("ADB_DB_NAME", DEFAULT_ADB_DB_NAME).strip() or DEFAULT_ADB_DB_NAME
    tns_alias = env.get("ADB_TNS_ALIAS", f"{db_name}_{DEFAULT_ADB_TNS_SERVICE}").strip()
    direct = _is_tcp_easy_connect(tns_alias)
    wallet_dir = _resolve_path(
        env.get("ADB_WALLET_DIR", f".wallets/{db_name.lower()}"),
        base_dir,
    )
    wallet_password = env.get("ADB_WALLET_PASSWORD", "").strip() if direct else _required(env, "ADB_WALLET_PASSWORD")
    db_user = env.get("ADB_DB_USER", DEFAULT_ADB_DB_USER).strip() or DEFAULT_ADB_DB_USER
    db_password = _required(env, "ADB_DB_PASSWORD")
    tcp_connect_timeout = _env_float(env, "ADB_CONNECT_TIMEOUT_SECONDS", 10.0)
    connect_retry_count = _env_int(env, "ADB_CONNECT_RETRY_COUNT", 0)
    connect_retry_delay = _env_int(env, "ADB_CONNECT_RETRY_DELAY_SECONDS", 0)
    pool_max = max(0, _env_int(env, "ADB_POOL_MAX", 4))

    return AdbSettings(
        db_name=db_name,
        tns_alias=tns_alias,
        wallet_dir=wallet_dir,
        wallet_password=wallet_password,
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
    """Thin 接続を返します。TCP Easy Connect では Wallet を読み込みません。

    `settings.pool_max > 0` で driver が `create_pool` を持つときは、設定ごとに 1 つ作る process 内プールから
    `acquire()` した接続を返す（#887）。プール接続は `close()`（`with` の終了）でプールへ戻る。それ以外は
    従来どおり `connect()` で新規接続を開く。接続を閉じる責任は呼び出し元にあり、driver の例外はそのまま伝播する。
    """
    if settings.uses_wallet:
        validate_mtls_wallet(settings)
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
    """ConnectParams / PoolParams を wallet・timeout・retry の設定で組み立てる。"""
    wallet_kwargs = {}
    if settings.uses_wallet:
        wallet_kwargs = dict(
            config_dir=str(settings.wallet_dir),
            wallet_location=str(settings.wallet_dir),
            wallet_password=settings.wallet_password,
        )
    params = params_class(
        **wallet_kwargs,
        tcp_connect_timeout=settings.tcp_connect_timeout,
        retry_count=settings.connect_retry_count,
        retry_delay=settings.connect_retry_delay,
    )
    params.parse_connect_string(settings.tns_alias)
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


def validate_mtls_wallet(settings: AdbSettings) -> None:
    """ADB 接続に必要な Wallet ファイルが揃っているかを検証します。"""
    missing = [name for name in REQUIRED_MTLS_WALLET_FILES if not (settings.wallet_dir / name).is_file()]
    if missing:
        missing_files = ", ".join(missing)
        raise RuntimeError(
            f"ADB mTLS wallet is not ready in {settings.wallet_dir}: missing {missing_files}. "
            "Upload the ADB wallet files to the wallet directory before connecting."
        )


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


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path
