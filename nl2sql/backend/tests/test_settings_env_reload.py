"""共通 `.env` に別のプロセスが保存した設定の読み直し（#2092・#2102）。"""

import json
from pathlib import Path

import pytest
from pr_system_settings.env_file import write_env_values

import app.settings as app_settings
from app.features.nl2sql.oracle_adapter import oracle_connect_kwargs


def test_get_settings_picks_up_upload_storage_saved_by_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG・Agent・別の worker が保存したアップロード保存先を、再起動なしで使う。"""
    for name in ("PLATFORM_UPLOAD_STORAGE_BACKEND", "PLATFORM_OBJECT_STORAGE_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    platform_env = tmp_path / "shared-platform.env"
    platform_env.write_text("PLATFORM_UPLOAD_STORAGE_BACKEND=local\n", encoding="utf-8")
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", platform_env)
    app_settings.reset_settings_cache()
    try:
        settings = app_settings.get_settings()
        assert settings.upload_storage_backend == "local"

        platform_env.write_text(
            "PLATFORM_UPLOAD_STORAGE_BACKEND=oci\nPLATFORM_OBJECT_STORAGE_BUCKET=example-bucket\n",
            encoding="utf-8",
        )
        reloaded = app_settings.get_settings()

        assert reloaded is settings
        assert reloaded.upload_storage_backend == "oci"
        assert reloaded.object_storage_bucket == "example-bucket"
    finally:
        app_settings.reset_settings_cache()


def test_get_settings_keeps_upload_storage_given_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境変数で与えたアップロード保存先は、`.env` の変更で上書きしない。"""
    monkeypatch.setenv("PLATFORM_UPLOAD_STORAGE_BACKEND", "local")
    platform_env = tmp_path / "shared-platform.env"
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", platform_env)
    app_settings.reset_settings_cache()
    try:
        settings = app_settings.get_settings()
        platform_env.write_text("PLATFORM_UPLOAD_STORAGE_BACKEND=oci\n", encoding="utf-8")

        assert app_settings.get_settings().upload_storage_backend == "local"
        assert settings.upload_storage_backend == "local"
    finally:
        app_settings.reset_settings_cache()


_SHARED_ENV_NAMES = (
    "PLATFORM_OCI_REGION",
    "PLATFORM_OCI_CONFIG_PROFILE",
    "PLATFORM_ORACLE_DSN",
    "PLATFORM_ORACLE_USER",
    "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES",
    "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL",
    "PLATFORM_ORACLE_CONNECTION_SECURITY",
)


def test_get_settings_picks_up_shared_settings_saved_by_another_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG・Agent の画面で保存した共通の設定（OCI 認証・DB・認証）を再起動なしで使う（#2102）。"""
    for name in _SHARED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    platform_env = tmp_path / "shared-platform.env"
    write_env_values(
        platform_env,
        {
            "PLATFORM_OCI_REGION": "region-a",
            "PLATFORM_ORACLE_DSN": "db-a_high",
            "PLATFORM_ORACLE_USER": "APP_A",
            # 接続引数を Wallet のファイルなしで作れるようにする
            "PLATFORM_ORACLE_CONNECTION_SECURITY": "walletless_tls",
        },
        section_comment="# test",
    )
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", platform_env)
    app_settings.reset_settings_cache()
    try:
        settings = app_settings.get_settings()
        assert (settings.oci_region, settings.oracle_dsn) == ("region-a", "db-a_high")

        # 別の製品の保存と同じ書き方（`write_env_values`）で共通 `.env` を書き換える。
        write_env_values(
            platform_env,
            {
                "PLATFORM_OCI_REGION": "region-b",
                "PLATFORM_OCI_CONFIG_PROFILE": "OTHER",
                "PLATFORM_ORACLE_DSN": "db-b_high",
                "PLATFORM_ORACLE_USER": "APP_B",
                "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES": "17",
            },
            section_comment="# test",
        )
        reloaded = app_settings.get_settings()

        assert reloaded is settings
        assert reloaded.oci_region == "region-b"
        assert reloaded.oci_config_profile == "OTHER"
        assert (reloaded.oracle_dsn, reloaded.oracle_user) == ("db-b_high", "APP_B")
        assert reloaded.app_auth_idle_timeout_minutes == 17
        # 接続 pool（`SharedOraclePool`）は接続引数の指紋で作り直す。接続引数が新しい値になる。
        kwargs = oracle_connect_kwargs(reloaded)
        assert (kwargs["dsn"], kwargs["user"]) == ("db-b_high", "APP_B")
    finally:
        app_settings.reset_settings_cache()


def test_platform_env_reload_keeps_model_settings_file_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共通 `.env` を読み直しても、model-settings.json のモデル設定を `.env` の値で戻さない。"""
    for name in _SHARED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    platform_env = tmp_path / "platform.env"
    platform_env.write_text(
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL=env-model\nPLATFORM_OCI_REGION=region-a\n",
        encoding="utf-8",
    )
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "enterprise_ai": {
                    "connections": [
                        {"connection_id": "primary", "endpoint": "https://primary.example/v1"}
                    ],
                    "models": [{"model_id": "json-model"}],
                    "default_text_model_id": "json-model",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", platform_env)
    app_settings.reset_settings_cache()
    try:
        settings = app_settings.get_settings()
        assert settings.oci_enterprise_ai_default_text_model == "json-model"

        platform_env.write_text(
            "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL=env-model-2\n"
            "PLATFORM_OCI_REGION=region-b\n",
            encoding="utf-8",
        )
        reloaded = app_settings.get_settings()

        assert reloaded.oci_region == "region-b"
        assert reloaded.oci_enterprise_ai_default_text_model == "json-model"
    finally:
        app_settings.reset_settings_cache()
