"""`platform/scripts/migrate_model_env_names.py`（#499）。"""

import importlib.util
import stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_model_env_names.py"
_spec = importlib.util.spec_from_file_location("migrate_model_env_names", SCRIPT)
assert _spec is not None and _spec.loader is not None
migrate_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_script)


def test_renames_default_and_vlm_models_and_keeps_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# モデル\n"
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL=text-a\n"
        "PLATFORM_OCI_ENTERPRISE_AI_LLM_MODEL=llm-old\n"
        "PLATFORM_OCI_ENTERPRISE_AI_LLM_PATH=/responses\n"
        "PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL=vision-b\n",
        encoding="utf-8",
    )

    assert migrate_script.main(["--env-file", str(env_file), "--apply"]) == 0

    assert env_file.read_text(encoding="utf-8") == (
        "# モデル\n"
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL=text-a\n"
        "PLATFORM_OCI_ENTERPRISE_AI_LLM_PATH=/responses\n"
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL=vision-b\n"
    )
    assert (tmp_path / ".env.bak-499").is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_llm_model_is_used_when_default_model_is_blank() -> None:
    content, messages = migrate_script.migrate(
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL=\nPLATFORM_OCI_ENTERPRISE_AI_LLM_MODEL=llm-a\n"
    )

    assert content == "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL=llm-a\n"
    assert any("PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL" in message for message in messages)


def test_existing_new_name_wins_and_old_lines_are_removed() -> None:
    content, messages = migrate_script.migrate(
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL=new-vision\n"
        "PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL=old-vision\n"
    )

    assert content == "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL=new-vision\n"
    assert any(message.startswith("競合") for message in messages)


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL=vision-b\n"
    env_file.write_text(original, encoding="utf-8")

    assert migrate_script.main(["--env-file", str(env_file)]) == 0

    assert env_file.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.bak-499").exists()
