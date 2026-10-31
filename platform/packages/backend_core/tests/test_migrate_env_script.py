"""`platform/scripts/migrate_env_to_platform.py`（#211）。"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_env_to_platform.py"
_spec = importlib.util.spec_from_file_location("migrate_env_to_platform", SCRIPT)
assert _spec is not None and _spec.loader is not None
migrate_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_env)


def test_moves_common_keys_and_prefixes_product_keys(tmp_path: Path) -> None:
    backend_env = tmp_path / ".env"
    backend_env.write_text(
        "# アプリ\nLOG_LEVEL=INFO\nNL2SQL_RUNTIME_MODE=oracle\n"
        "ORACLE_DSN=db_high\nAPP_ADMIN_LOGIN_USER_PASSWORD='Secret#1'\n"
        "OCI_REGION=ap-osaka-1\nAPP_ADMIN_USERNAME=old\n",
        encoding="utf-8",
    )
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("PLATFORM_OCI_REGION=us-chicago-1\n", encoding="utf-8")

    migrate_env.main(
        [
            "--product",
            "nl2sql",
            "--backend-env",
            str(backend_env),
            "--platform-env",
            str(platform_env),
            "--apply",
        ]
    )

    assert backend_env.read_text(encoding="utf-8") == (
        "# アプリ\nNL2SQL_LOG_LEVEL=INFO\nNL2SQL_RUNTIME_MODE=oracle\n"
    )
    platform_text = platform_env.read_text(encoding="utf-8")
    assert "PLATFORM_ORACLE_DSN=db_high" in platform_text
    assert "PLATFORM_ADMIN_LOGIN_USER_PASSWORD='Secret#1'" in platform_text
    # 競合は共通 .env の値を残す。
    assert "PLATFORM_OCI_REGION=us-chicago-1" in platform_text
    assert "ap-osaka-1" not in platform_text
    assert (tmp_path / ".env.bak-211").is_file()
