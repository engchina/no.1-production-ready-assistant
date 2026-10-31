"""実行環境と認証の共通契約を合成設定で検証する。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pr_backend_core.config import BaseServiceSettings, RuntimeSettings, product_settings_config


def test_defaults_require_authenticated_staging() -> None:
    settings = RuntimeSettings()
    assert settings.environment == "staging"
    assert settings.auth_mode == "authenticated"
    assert settings.app_auth_cookie_secure
    assert settings.is_deployed
    assert settings.app_auth_enabled
    assert not settings.local_debug_enabled


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_require_auth_and_secure_cookies(environment: str) -> None:
    for overrides in ({"auth_mode": "local"}, {"app_auth_cookie_secure": False}):
        with pytest.raises(ValidationError):
            RuntimeSettings(environment=environment, **overrides)


@pytest.mark.parametrize("environment", ["dev", "development", "prod", "", "PRODUCTION"])
def test_invalid_environment_is_rejected(environment: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(environment=environment)


def test_legacy_auth_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(auth_mode="production")


def test_local_bypass_requires_explicit_environment_and_mode() -> None:
    local = RuntimeSettings(environment="local", auth_mode="local", app_auth_cookie_secure=False)
    assert local.local_debug_enabled
    assert not local.app_auth_enabled
    assert not RuntimeSettings(environment="local").local_debug_enabled
    # model_construct や属性の変更で validator を通さなくても、非 local は認証を要求する。
    invalid = RuntimeSettings.model_construct(environment="production", auth_mode="local")
    assert invalid.app_auth_enabled
    assert not invalid.local_debug_enabled


@pytest.mark.parametrize("prefix", ["RAG_", "NL2SQL_", "AGENT_"])
def test_products_read_same_shared_settings(
    prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "platform.env"
    shared.write_text("PLATFORM_ENVIRONMENT=production\nPLATFORM_AUTH_MODE=authenticated\n")
    monkeypatch.setenv("PLATFORM_ENV_FILE", str(shared))
    monkeypatch.setenv(f"{prefix}ENVIRONMENT", "local")
    monkeypatch.setenv(f"{prefix}AUTH_MODE", "local")

    class Settings(BaseServiceSettings):
        model_config = product_settings_config(prefix=prefix, backend_dir=tmp_path)

    settings = Settings()
    assert settings.is_production
    assert settings.app_auth_enabled
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "staging")
    assert Settings().environment == "staging"


def test_early_logging_reads_shared_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_backend_core.config.runtime import load_runtime_environment

    shared = tmp_path / "platform.env"
    shared.write_text("PLATFORM_ENVIRONMENT=production\n")
    monkeypatch.setenv("PLATFORM_ENV_FILE", str(shared))
    monkeypatch.delenv("PLATFORM_ENVIRONMENT", raising=False)
    assert load_runtime_environment() == "production"
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "staging")
    assert load_runtime_environment() == "staging"
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "prod")
    with pytest.raises(ValidationError):
        load_runtime_environment()
