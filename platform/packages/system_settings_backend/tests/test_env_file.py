"""共通 `.env` の書き出し（`format_env_value` / `write_env_values`）の往復のテスト（#1112）。

secret を画面から保存した値と、再起動後に python-dotenv（pydantic-settings も同じ）で読み戻した値が
同じになること（前後の空白・`${...}`・引用符・改行）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

from pr_system_settings.env_file import format_env_value, write_env_values

ROUND_TRIP_VALUES = [
    "plain",
    "  pass with spaces  ",
    " ",
    "a${HOME}b",
    "${PLATFORM_ORACLE_USER}",
    "${:-x}",
    "$${HOME}",
    "abc${",
    "a$b$",
    "${HOME:-fallback}",
    'quote"inside',
    "single'quote",
    "back\\slash",
    "back\\slash with space",
    "trailing backslash\\",
    "hash # inside",
    "hash#inside",
    "line1\nline2",
    "cr\rlf",
    "tab\tinside",
    "日本語のパスワード",
    "=equals=",
]


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES, ids=range(len(ROUND_TRIP_VALUES)))
def test_write_env_values_round_trips_with_python_dotenv(tmp_path: Path, value: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PLATFORM_ORACLE_USER=APP\n", encoding="utf-8")

    write_env_values(env_file, {"PLATFORM_SECRET": value}, section_comment="# secret")

    assert dotenv_values(env_file)["PLATFORM_SECRET"] == value
    # 次の保存で行が崩れない（値が 1 行に収まり、ほかの key を壊さない）。
    write_env_values(env_file, {"PLATFORM_OTHER": "x"}, section_comment="# other")
    values = dotenv_values(env_file)
    assert values["PLATFORM_SECRET"] == value
    assert values["PLATFORM_ORACLE_USER"] == "APP"
    assert values["PLATFORM_OTHER"] == "x"


def test_write_env_values_round_trips_with_pydantic_settings(tmp_path: Path) -> None:
    """製品の Settings（pydantic-settings の `env_file`）で読んでも同じ値になる。"""
    env_file = tmp_path / ".env"
    password = "  Pa${HOME}ss \"w'rd\\  "
    write_env_values(env_file, {"PLATFORM_ORACLE_PASSWORD": password}, section_comment="# DB")

    class _Settings(BaseSettings):
        model_config = SettingsConfigDict(env_file=env_file, env_prefix="PLATFORM_")

        oracle_password: str = ""

    assert _Settings().oracle_password == password


def test_format_env_value_keeps_simple_values_unquoted() -> None:
    assert format_env_value("") == ""
    assert format_env_value("ap-tokyo-1") == "ap-tokyo-1"
    assert format_env_value("/u01/data/rag") == "/u01/data/rag"
    assert format_env_value("a b") == '"a b"'
