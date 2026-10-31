"""サービス設定。共通基底 BaseServiceSettings を継承し、ドメイン設定を足す。"""

from functools import lru_cache
from pathlib import Path

from pr_backend_core.config import BaseServiceSettings, product_settings_config

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseServiceSettings):
    """サービス固有設定。

    共通 `platform/.env`（`PLATFORM_*`）→ `backend/.env`（製品の接頭辞）の順に読む（#211）。
    OCI/Oracle 等の共通の接続設定は属性名だけ足せば `PLATFORM_*` から読む（例: oracle_dsn）。
    製品の接頭辞は実際の製品名に合わせて変える。
    """

    model_config = product_settings_config(prefix="SERVICE_", backend_dir=BACKEND_DIR)

    service_name: str = "production-ready-service"


@lru_cache
def get_settings() -> Settings:
    """設定シングルトン。"""
    return Settings()
