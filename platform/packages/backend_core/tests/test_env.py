"""共通 `.env` と環境変数名の規則（#211）。"""

from pathlib import Path

import pytest

from pr_backend_core.config import (
    BaseServiceSettings,
    EnvFieldReloader,
    isolate_env_files_for_tests,
    platform_env_file,
    product_env_file,
    product_settings_config,
    settings_env_names,
)


def _settings_class(tmp_path: Path) -> type[BaseServiceSettings]:
    backend_dir = tmp_path / "rag" / "backend"
    backend_dir.mkdir(parents=True)

    class Settings(BaseServiceSettings):
        model_config = product_settings_config(prefix="RAG_", backend_dir=backend_dir)

        oracle_dsn: str = ""
        app_auth_idle_timeout_minutes: int = 60
        rag_chunk_size: int = 1

    return Settings


def test_env_names_split_platform_and_product(tmp_path: Path) -> None:
    names = settings_env_names(_settings_class(tmp_path))
    assert names["oracle_dsn"] == "PLATFORM_ORACLE_DSN"
    assert names["app_auth_idle_timeout_minutes"] == "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES"
    assert names["auth_mode"] == "PLATFORM_AUTH_MODE"
    assert names["rag_chunk_size"] == "RAG_CHUNK_SIZE"
    assert names["log_level"] == "RAG_LOG_LEVEL"


def test_reads_platform_then_product_env_and_ignores_old_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLATFORM_ENV_FILE", raising=False)
    settings_cls = _settings_class(tmp_path)
    (tmp_path / "platform").mkdir()
    (tmp_path / "platform" / ".env").write_text(
        "PLATFORM_ORACLE_DSN=shared\nPLATFORM_AUTH_IDLE_TIMEOUT_MINUTES=30\nPLATFORM_AUTH_MODE=authenticated\n",
        encoding="utf-8",
    )
    (tmp_path / "rag" / "backend" / ".env").write_text(
        "RAG_AUTH_MODE=local\nORACLE_DSN=old\nAUTH_MODE=old\n", encoding="utf-8"
    )
    monkeypatch.setenv("ORACLE_DSN", "old")
    monkeypatch.setenv("RAG_CHUNK_SIZE", "5")
    settings = settings_cls()
    assert settings.oracle_dsn == "shared"  # type: ignore[attr-defined]
    assert settings.app_auth_idle_timeout_minutes == 30  # type: ignore[attr-defined]
    assert settings.auth_mode == "authenticated"  # type: ignore[attr-defined]
    assert settings.rag_chunk_size == 5  # type: ignore[attr-defined]
    # 属性名での生成（テスト用）も使える。
    assert settings_cls(oracle_dsn="kw").oracle_dsn == "kw"  # type: ignore[attr-defined]


def test_old_names_are_not_read_even_without_new_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新名がなくても、旧名（属性名と同じ名前）の環境変数と .env は読まない（#211）。"""
    monkeypatch.delenv("PLATFORM_ENV_FILE", raising=False)
    settings_cls = _settings_class(tmp_path)
    (tmp_path / "rag" / "backend" / ".env").write_text(
        "LOG_LEVEL=WARN\nAUTH_MODE=old\n", encoding="utf-8"
    )
    monkeypatch.setenv("ORACLE_DSN", "old")
    monkeypatch.setenv("RAG_CHUNK_SIZE", "7")
    settings = settings_cls()
    assert settings.oracle_dsn == ""  # type: ignore[attr-defined]
    assert settings.log_level == "INFO"
    assert settings.auth_mode == "authenticated"  # type: ignore[attr-defined]
    assert settings.rag_chunk_size == 7  # type: ignore[attr-defined]


def test_platform_env_file_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_dir = tmp_path / "nl2sql" / "backend"
    monkeypatch.delenv("PLATFORM_ENV_FILE", raising=False)
    assert platform_env_file(backend_dir) == tmp_path.resolve() / "platform" / ".env"
    monkeypatch.setenv("PLATFORM_ENV_FILE", str(tmp_path / "shared.env"))
    assert platform_env_file(backend_dir) == tmp_path / "shared.env"


def test_product_env_file_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_dir = tmp_path / "rag" / "backend"
    monkeypatch.delenv("RAG_ENV_FILE", raising=False)
    assert product_env_file("RAG_", backend_dir) == backend_dir / ".env"
    monkeypatch.setenv("RAG_ENV_FILE", str(tmp_path / "rag.env"))
    assert product_env_file("RAG_", backend_dir) == tmp_path / "rag.env"


def test_isolate_env_files_for_tests_points_both_files_at_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テストは手元の共通 `.env` と製品の `backend/.env` を読まない（#1972）。"""
    monkeypatch.delenv("PLATFORM_ENV_FILE", raising=False)
    monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
    backend_dir = tmp_path / "agent" / "backend"
    isolate_env_files_for_tests("AGENT_")
    shared, product = platform_env_file(backend_dir), product_env_file("AGENT_", backend_dir)
    assert shared != tmp_path.resolve() / "platform" / ".env"
    assert product != backend_dir / ".env"
    assert not shared.exists() and not shared.parent.exists() and not product.exists()


def test_backend_service_env_example_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """新サービスの雛形をコピーしたときの設定解析を検証する。"""
    import os

    for key in tuple(os.environ):
        if key.startswith(("PLATFORM_", "SERVICE_")):
            monkeypatch.delenv(key)
    platform = Path(__file__).resolve().parents[3]
    backend = platform / "templates" / "backend-service"

    class Settings(BaseServiceSettings):
        model_config = product_settings_config(prefix="SERVICE_", backend_dir=backend)

    settings = Settings(_env_file=backend / ".env.example")
    assert settings.cors_origins == ["http://localhost:3000"]


def _reloader_settings_class() -> type[BaseServiceSettings]:
    class Settings(BaseServiceSettings):
        model_config = product_settings_config(prefix="RAG_", backend_dir=Path("/nonexistent/b"))

        local_storage_dir: str = "/data/a"
        oracle_user: str = ""
        oracle_dsn: str = ""

    return Settings


def test_env_field_reloader_applies_only_selected_fields_after_env_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別のプロセスが `.env` に保存した値を、指定した項目だけ取り込む（#2092）。"""
    monkeypatch.delenv("PLATFORM_LOCAL_STORAGE_DIR", raising=False)
    monkeypatch.delenv("PLATFORM_ORACLE_USER", raising=False)
    env_file = tmp_path / "platform.env"
    env_file.write_text("PLATFORM_LOCAL_STORAGE_DIR=/data/a\n", encoding="utf-8")
    settings_cls = _reloader_settings_class()
    settings = settings_cls(_env_file=(env_file,))
    reloader = EnvFieldReloader(
        settings_cls,
        env_files=lambda: (env_file,),
        fields=("local_storage_dir", "oracle_user", "not_a_field"),
    )
    reloader.mark_loaded(settings)

    assert reloader.reload_if_changed(settings) == []

    env_file.write_text(
        "PLATFORM_LOCAL_STORAGE_DIR=/data/b\n"
        "PLATFORM_ORACLE_USER=example-user\n"
        "PLATFORM_ORACLE_DSN=changed-dsn\n",
        encoding="utf-8",
    )

    assert reloader.reload_if_changed(settings) == [
        "local_storage_dir",
        "oracle_user",
    ]
    assert settings.local_storage_dir == "/data/b"  # type: ignore[attr-defined]
    assert settings.oracle_user == "example-user"  # type: ignore[attr-defined]
    # 指定していない項目は読み直さない
    assert settings.oracle_dsn == ""  # type: ignore[attr-defined]
    # 同じ内容では読み直さない
    assert reloader.reload_if_changed(settings) == []


def test_env_field_reloader_keeps_values_given_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境変数で与えた項目は `.env` の値で上書きしない。"""
    monkeypatch.setenv("PLATFORM_LOCAL_STORAGE_DIR", "/data/a")
    env_file = tmp_path / "platform.env"
    settings_cls = _reloader_settings_class()
    settings = settings_cls()
    reloader = EnvFieldReloader(
        settings_cls, env_files=lambda: (env_file,), fields=("local_storage_dir",)
    )
    reloader.mark_loaded(settings)

    env_file.write_text("PLATFORM_LOCAL_STORAGE_DIR=/data/b\n", encoding="utf-8")

    assert reloader.reload_if_changed(settings) == []
    assert settings.local_storage_dir == "/data/a"  # type: ignore[attr-defined]


def test_env_field_reloader_keeps_current_values_when_env_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読み直しに失敗しても止めず、今の値のまま動く。"""
    monkeypatch.delenv("PLATFORM_LOCAL_STORAGE_DIR", raising=False)

    class StrictSettings(BaseServiceSettings):
        model_config = product_settings_config(prefix="RAG_", backend_dir=Path("/nonexistent/b"))

        local_storage_dir: str = "/data/a"
        rag_chunk_size: int = 1

    env_file = tmp_path / "platform.env"
    settings = StrictSettings()
    reloader = EnvFieldReloader(
        StrictSettings, env_files=lambda: (env_file,), fields=("local_storage_dir",)
    )
    reloader.mark_loaded(settings)

    env_file.write_text(
        "PLATFORM_LOCAL_STORAGE_DIR=/data/b\nRAG_CHUNK_SIZE=not-a-number\n",
        encoding="utf-8",
    )

    assert reloader.reload_if_changed(settings) == []
    assert settings.local_storage_dir == "/data/a"


def test_env_field_reloader_rebases_without_applying_when_env_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` の場所が変わったとき（テストの tmp など）は、基準を取り直すだけにする。

    前回の内容から変わった項目だけを反映するので、プロセスの中で変えた値も戻さない。
    """
    monkeypatch.delenv("PLATFORM_LOCAL_STORAGE_DIR", raising=False)
    monkeypatch.delenv("PLATFORM_ORACLE_USER", raising=False)
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    second.write_text("PLATFORM_LOCAL_STORAGE_DIR=/data/b\n", encoding="utf-8")
    current = [first]
    settings_cls = _reloader_settings_class()
    settings = settings_cls(_env_file=(first,))
    reloader = EnvFieldReloader(
        settings_cls,
        env_files=lambda: tuple(current),
        fields=("local_storage_dir", "oracle_user"),
    )
    reloader.mark_loaded(settings)
    current[0] = second

    assert reloader.reload_if_changed(settings) == []
    assert settings.local_storage_dir == "/data/a"  # type: ignore[attr-defined]

    # プロセスの中で変えた値は、`.env` の別の項目の変更では戻さない
    settings.local_storage_dir = "/data/mem"  # type: ignore[attr-defined]
    second.write_text(
        "PLATFORM_LOCAL_STORAGE_DIR=/data/b\nPLATFORM_ORACLE_USER=example-user\n",
        encoding="utf-8",
    )

    assert reloader.reload_if_changed(settings) == ["oracle_user"]
    assert settings.local_storage_dir == "/data/mem"  # type: ignore[attr-defined]
    assert settings.oracle_user == "example-user"  # type: ignore[attr-defined]


def test_env_field_reloader_bases_on_env_values_not_values_applied_after_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基準は `.env` の値にする（#2102）。

    `.env` の後にかけた値（model-settings.json のモデル設定など）を基準にすると、`.env` の
    別の行の変更で、その項目を `.env` の古い値へ戻してしまう。
    """
    monkeypatch.delenv("PLATFORM_LOCAL_STORAGE_DIR", raising=False)
    monkeypatch.delenv("PLATFORM_ORACLE_DSN", raising=False)
    env_file = tmp_path / "platform.env"
    env_file.write_text("PLATFORM_ORACLE_DSN=first-dsn\n", encoding="utf-8")
    settings_cls = _reloader_settings_class()
    settings = settings_cls(_env_file=(env_file,))
    # `.env` を読んだ後に、別の設定ファイルの値をかけた
    settings.local_storage_dir = "/data/b"  # type: ignore[attr-defined]
    reloader = EnvFieldReloader(
        settings_cls,
        env_files=lambda: (env_file,),
        fields=("local_storage_dir", "oracle_dsn"),
    )
    reloader.mark_loaded(settings)

    env_file.write_text("PLATFORM_ORACLE_DSN=second-dsn\n", encoding="utf-8")

    assert reloader.reload_if_changed(settings) == ["oracle_dsn"]
    assert settings.oracle_dsn == "second-dsn"  # type: ignore[attr-defined]
    assert settings.local_storage_dir == "/data/b"  # type: ignore[attr-defined]
