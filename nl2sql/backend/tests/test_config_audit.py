"""Configuration catalog / secret boundary audit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_audit import (
    ENV_ASSIGNMENT_RE,
    AuditPaths,
    audit_configuration,
    settings_env_keys,
    stable_audit_json,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
PLATFORM_ENV_EXAMPLE = BACKEND_DIR.parents[1] / "platform" / ".env.example"


def _paths(
    tmp_path: Path,
    *,
    env_path: Path | None = None,
    platform_env_path: Path | None = None,
    model_settings_path: Path | None = None,
) -> AuditPaths:
    return AuditPaths(
        example_path=BACKEND_DIR / ".env.example",
        env_path=env_path or tmp_path / "missing.env",
        platform_example_path=PLATFORM_ENV_EXAMPLE,
        platform_env_path=platform_env_path or tmp_path / "missing-platform.env",
        model_settings_path=model_settings_path or tmp_path / "missing-model-settings.json",
    )


def test_repository_env_example_is_complete_and_secret_free(tmp_path: Path) -> None:
    """製品キーは backend/.env.example、共通キーは platform/.env.example にそろっている。"""
    result = audit_configuration(_paths(tmp_path))

    assert result.ok is True
    assert result.findings == []


def test_product_env_example_has_only_product_keys() -> None:
    """backend/.env.example は NL2SQL_* だけを持ち、共通キーは platform/.env.example が持つ。"""
    platform_keys, product_keys = settings_env_keys()
    example_keys = {
        match.group("key")
        for line in (BACKEND_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
        if (match := ENV_ASSIGNMENT_RE.match(line)) is not None
    }

    assert all(key.startswith("PLATFORM_") for key in platform_keys)
    assert all(key.startswith("NL2SQL_") for key in product_keys)
    assert example_keys == product_keys


def test_audit_reports_platform_keys_left_in_product_env(tmp_path: Path) -> None:
    """移行し忘れた共通キーや旧名が製品の .env に残っていれば error にする。"""
    env_file = tmp_path / ".env"
    env_file.write_text("PLATFORM_ORACLE_DSN=db_high\nORACLE_DSN=db_high\n", encoding="utf-8")
    env_file.chmod(0o600)
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("PLATFORM_ORACLE_DSN=db_high\nNL2SQL_DEBUG=true\n", encoding="utf-8")
    platform_env.chmod(0o644)

    result = audit_configuration(
        _paths(tmp_path, env_path=env_file, platform_env_path=platform_env)
    )
    findings = {item.code: item for item in result.findings}

    assert result.ok is False
    assert findings["ENV_ACTUAL_UNKNOWN_KEYS"].keys == ("ORACLE_DSN", "PLATFORM_ORACLE_DSN")
    assert findings["PLATFORM_ENV_ACTUAL_UNKNOWN_KEYS"].keys == ("NL2SQL_DEBUG",)
    assert "PLATFORM_ENV_ACTUAL_PERMISSIONS_NOT_0600" in findings


def test_audit_reports_unknown_duplicate_malformed_and_security_combinations(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PLATFORM_ENVIRONMENT=production\n"
        "PLATFORM_AUTH_MODE=local\n"
        "PLATFORM_AUTH_MODE=authenticated\n"
        "PLATFORM_AUTH_MODE=authenticated\n"
        "UNKNOWN_SETTING=value\n"
        "not an assignment\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("PLATFORM_AUTH_COOKIE_SECURE=false\n", encoding="utf-8")
    platform_env.chmod(0o600)

    result = audit_configuration(
        _paths(tmp_path, env_path=env_file, platform_env_path=platform_env)
    )
    codes = {item.code for item in result.findings}

    assert result.ok is False
    assert "ENV_ACTUAL_DUPLICATE_KEYS" in codes
    assert "ENV_ACTUAL_MALFORMED_LINES" in codes
    assert "ENV_ACTUAL_UNKNOWN_KEYS" in codes
    assert "INVALID_RUNTIME_SETTINGS" in codes


def test_audit_detects_legacy_json_without_disclosing_secret(tmp_path: Path) -> None:
    model_settings = tmp_path / "model-settings.json"
    model_settings.write_text(
        json.dumps(
            {
                "version": 1,
                "enterprise_ai": {"api_key": "do-not-disclose"},
            }
        ),
        encoding="utf-8",
    )
    model_settings.chmod(0o600)

    result = audit_configuration(_paths(tmp_path, model_settings_path=model_settings))
    output = stable_audit_json(result)

    assert "MODEL_SETTINGS_LEGACY_SECRET" in output
    assert "MODEL_SETTINGS_VERSION_LEGACY" in output
    assert "do-not-disclose" not in output


def test_init_script_keeps_thin_mtls_without_instant_client() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    init_script = (repo_root / "init_script.sh").read_text(encoding="utf-8").lower()

    assert '"${app_root}/props/platform.env" "${platform_repo_dir}/.env"' in init_script
    assert "instantclient" not in init_script
    # 自前のコードは Docker イメージを作らない（#356）。配備は init_script.sh の unit だけ。
    assert not (repo_root / "backend" / "Dockerfile").exists()


def test_platform_and_product_env_examples_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """実際の雛形を読み込み、初回起動時の設定解析エラーを検出する。"""
    import os

    from app.settings import Settings

    for key in tuple(os.environ):
        if key.startswith(("PLATFORM_", "NL2SQL_")):
            monkeypatch.delenv(key)
    backend = Path(__file__).resolve().parents[1]
    settings = Settings(
        _env_file=(backend.parents[1] / "platform" / ".env.example", backend / ".env.example")
    )

    assert settings.cors_origins
    assert all(origin.startswith("http://") for origin in settings.cors_origins)
    assert settings.enterprise_ai_models == []
    assert settings.oracle_password in ("", None)
