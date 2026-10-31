"""3 製品で共有する実行環境とログイン認証の契約。"""

import os
from pathlib import Path
from typing import Literal, Self

from dotenv import dotenv_values
from pydantic import BaseModel, TypeAdapter, model_validator

Environment = Literal["local", "staging", "production"]
AuthMode = Literal["local", "authenticated"]


def load_runtime_environment() -> Environment:
    """Settings を初期化しない CLI とサービスのログにも共通の環境を使う。"""
    value = os.environ.get("PLATFORM_ENVIRONMENT")
    if value is None:
        path = os.environ.get("PLATFORM_ENV_FILE")
        shared = Path(path).expanduser() if path else Path(__file__).resolve().parents[5] / ".env"
        value = dotenv_values(shared).get("PLATFORM_ENVIRONMENT", "staging")
    return TypeAdapter(Environment).validate_python(value)


class RuntimeSettings(BaseModel):
    environment: Environment = "staging"
    auth_mode: AuthMode = "authenticated"
    app_auth_cookie_secure: bool = True

    @model_validator(mode="after")
    def validate_runtime_security(self) -> Self:
        if self.is_deployed:
            if self.auth_mode != "authenticated":
                raise ValueError(
                    "staging / production では PLATFORM_AUTH_MODE=authenticated が必要です。"
                )
            if not self.app_auth_cookie_secure:
                raise ValueError(
                    "staging / production では PLATFORM_AUTH_COOKIE_SECURE=true が必要です。"
                )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_deployed(self) -> bool:
        """検証環境と本番環境に同じ安全検査を適用する。"""
        return self.environment in {"staging", "production"}

    @property
    def local_debug_enabled(self) -> bool:
        return self.environment == "local" and self.auth_mode == "local"

    @property
    def app_auth_enabled(self) -> bool:
        return not self.local_debug_enabled
