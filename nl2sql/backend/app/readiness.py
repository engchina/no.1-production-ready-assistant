"""依存設定の readiness チェック（/ready 用）。

Oracle の接続設定の判定は、システム設定画面・DB の状態 API（`/api/ready/database`）と同じ
platform の `database_readiness` を使う（#325）。NL2SQL 固有の判定（DeepSec）は
`deepsec_readiness` として同じものを渡す。
"""

from pr_system_settings.database import database_readiness

from app.settings import Settings

READINESS_OK = "ok"
READINESS_MISSING = "missing"
READINESS_MISSING_CREDENTIALS = "missing_credentials"
READINESS_WALLET_NOT_FOUND = "wallet_not_found"
READINESS_INVALID_CONFIGURATION = "invalid_configuration"


def uses_oracle(settings: Settings) -> bool:
    """Oracle を使う構成か（deterministic + memory だけは DB を使わない）。"""
    runtime = settings.nl2sql_runtime_mode.strip().lower()
    persistence = settings.nl2sql_persistence_mode.strip().lower()
    return not (runtime == "deterministic" and persistence == "memory")


def readiness_checks(settings: Settings) -> dict[str, str]:
    """共通 readiness では Oracle 依存時だけ設定状態を返す。"""
    if not uses_oracle(settings):
        return {}
    return {"oracle": oracle_readiness_check(settings)}


def deepsec_readiness(settings: Settings) -> str | None:
    """DeepSec は Thin mode だけ対応する（NL2SQL 固有の readiness）。問題がなければ None。"""
    if settings.oracle_deepsec_enabled and settings.oracle_driver_mode.strip().lower() != "thin":
        return READINESS_INVALID_CONFIGURATION
    return None


def oracle_readiness_check(settings: Settings) -> str:
    """Oracle 接続に必要な設定の状態を返す（接続は試さない）。"""
    return database_readiness(settings, deepsec_readiness)
