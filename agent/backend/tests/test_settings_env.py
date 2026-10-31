"""Settings の環境変数名と共通 `.env` の読み込み（#211）。"""

import json
from pathlib import Path

import pytest
from pr_backend_core.config import settings_env_names
from pr_system_settings.env_file import write_env_values

import app.settings as app_settings
from app.oracle_connection import oracle_connect_kwargs
from app.settings import Settings, resolve_model_settings_file


def test_settings_env_names_split_platform_and_agent() -> None:
    names = settings_env_names(Settings)

    assert names["oracle_dsn"] == "PLATFORM_ORACLE_DSN"
    assert names["oci_region"] == "PLATFORM_OCI_REGION"
    assert names["local_storage_dir"] == "PLATFORM_LOCAL_STORAGE_DIR"
    assert names["model_settings_file"] == "PLATFORM_MODEL_SETTINGS_FILE"
    assert names["oci_enterprise_ai_api_key"] == "PLATFORM_OCI_ENTERPRISE_AI_API_KEY"
    assert names["service_name"] == "AGENT_SERVICE_NAME"
    assert names["log_level"] == "AGENT_LOG_LEVEL"
    assert names["cors_origins"] == "AGENT_CORS_ORIGINS"
    assert names["max_upload_bytes"] == "AGENT_MAX_UPLOAD_BYTES"
    # 既に AGENT_ で始まる属性は接頭辞を重ねない。
    assert names["agent_runtime_repository_backend"] == "AGENT_RUNTIME_REPOSITORY_BACKEND"
    # Run の保存先は共通の PLATFORM_ORACLE_* で接続する（旧 AGENT_RUNTIME_ORACLE_* は無い。#764）。
    assert not [name for name in names.values() if name.startswith("AGENT_RUNTIME_ORACLE_")]
    assert all(name.startswith(("PLATFORM_", "AGENT_")) for name in names.values())


def test_settings_reads_platform_env_then_backend_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("PLATFORM_ORACLE_DSN", "AGENT_LOG_LEVEL", "ORACLE_DSN", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("PLATFORM_ORACLE_DSN=shared_high\n", encoding="utf-8")
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("AGENT_LOG_LEVEL=DEBUG\n", encoding="utf-8")

    settings = Settings(_env_file=(platform_env, backend_env))

    assert settings.oracle_dsn == "shared_high"
    assert settings.log_level == "DEBUG"


def test_legacy_env_names_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PLATFORM_OCI_REGION", "AGENT_LOG_LEVEL", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCI_REGION", "ap-osaka-1")
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    settings = Settings(_env_file=legacy_env)

    assert settings.oci_region == ""
    assert settings.log_level == "INFO"


def test_model_settings_file_resolves_next_to_platform_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", tmp_path / "platform" / ".env")

    assert (
        resolve_model_settings_file("model-settings.json")
        == (tmp_path / "platform" / "model-settings.json").resolve()
    )
    absolute = tmp_path / "data" / "model-settings.json"
    assert resolve_model_settings_file(str(absolute)) == absolute


def test_platform_and_product_env_examples_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """実際の雛形を読み込み、初回起動時の設定解析エラーを検出する。"""
    import os

    from app.settings import Settings

    for key in tuple(os.environ):
        if key.startswith(("PLATFORM_", "AGENT_")):
            monkeypatch.delenv(key)
    backend = Path(__file__).resolve().parents[1]
    settings = Settings(
        _env_file=(backend.parents[1] / "platform" / ".env.example", backend / ".env.example")
    )

    assert settings.cors_origins
    assert all(origin.startswith("http://") for origin in settings.cors_origins)
    assert settings.oci_enterprise_ai_models == []
    assert settings.oracle_password in ("", None)


def test_product_mcp_timeouts_are_explicit_in_env_example() -> None:
    """`.env.example` は RAG / NL2SQL の MCP の待ち時間を既定と同じ値で明示する（#1430）。"""
    from app.features.agent import tools
    from app.features.agent.router import _MCP_TIMEOUT_MAX_SECONDS

    settings = Settings(_env_file=None)
    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    entries = dict(
        line.split("=", 1)
        for line in lines.splitlines()
        if line.startswith("AGENT_EXTERNAL_") and "_TIMEOUT_SECONDS=" in line
    )
    assert float(entries["AGENT_EXTERNAL_RAG_TIMEOUT_SECONDS"]) == (
        settings.agent_external_rag_timeout_seconds
    )
    assert float(entries["AGENT_EXTERNAL_NL2SQL_TIMEOUT_SECONDS"]) == (
        settings.agent_external_nl2sql_timeout_seconds
    )
    # RAG の回答の上限（600 秒）+ 余裕 30 秒。画面の「MCP 接続」の上限（900 秒）以下。
    assert settings.agent_external_rag_timeout_seconds == 600 + 30 <= _MCP_TIMEOUT_MAX_SECONDS
    # NL2SQL の MCP の 1 回の待ち（wait_seconds）の上限 45 秒と SQL の生成前の確認（LLM。#1441）の
    # 480 秒（1 回 240 秒 × 試行 2 回。#2240）に、HTTP の往復・ジョブの登録・DB の負荷の余裕 75 秒を
    # 足した 600 秒（#1432・#2240）。続きの取得の 1 回の待ち（20 秒）と余裕も収まる。
    nl2sql_max_wait_seconds = 45
    requirement_seconds = 240 * 2
    assert (
        settings.agent_external_nl2sql_timeout_seconds
        == nl2sql_max_wait_seconds + requirement_seconds + 75
        == 600
    )
    assert settings.agent_external_nl2sql_timeout_seconds - tools._JOB_POLL_MARGIN_SECONDS >= max(
        nl2sql_max_wait_seconds, tools._JOB_POLL_WAIT_SECONDS
    )
    assert settings.agent_external_nl2sql_timeout_seconds <= _MCP_TIMEOUT_MAX_SECONDS


def test_evaluation_case_timeout_is_explicit_in_env_example() -> None:
    """`.env.example` は品質評価の 1 ケースの上限を既定（0 = 自動）と同じ値で明示する（#1432）。"""
    settings = Settings(_env_file=None)
    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    entries = dict(
        line.split("=", 1)
        for line in lines.splitlines()
        if line.startswith("AGENT_EVALUATION_CASE_TIMEOUT_SECONDS=")
    )
    assert (
        float(entries["AGENT_EVALUATION_CASE_TIMEOUT_SECONDS"])
        == (settings.agent_evaluation_case_timeout_seconds)
        == 0
    )


def test_get_settings_picks_up_upload_storage_saved_by_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別の製品・worker が共通 `.env` に保存したアップロード保存先を、再起動なしで使う（#2092）。"""
    for name in ("PLATFORM_UPLOAD_STORAGE_BACKEND", "PLATFORM_OBJECT_STORAGE_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    platform_env = tmp_path / "platform.env"
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


_SHARED_ENV_NAMES = (
    "PLATFORM_OCI_REGION",
    "PLATFORM_OCI_CONFIG_PROFILE",
    "PLATFORM_ORACLE_DSN",
    "PLATFORM_ORACLE_USER",
    "PLATFORM_ORACLE_WALLET_DIR",
    "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES",
    "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL",
)


def test_get_settings_picks_up_shared_settings_saved_by_another_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG・NL2SQL の画面で保存した共通の設定（OCI 認証・DB・認証）を再起動なしで使う（#2102）。"""
    for name in _SHARED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    platform_env = tmp_path / "platform.env"
    write_env_values(
        platform_env,
        {
            "PLATFORM_OCI_REGION": "region-a",
            "PLATFORM_ORACLE_DSN": "db-a_high",
            "PLATFORM_ORACLE_USER": "APP_A",
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
