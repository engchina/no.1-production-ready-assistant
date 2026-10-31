"""Argon2id password hash。policy と一時 password は3製品共通（pr_system_settings。#206）。"""

from __future__ import annotations

from pr_system_settings.auth.passwords import password_hasher
from pr_system_settings.users_roles import PasswordPolicyError as PasswordPolicyError
from pr_system_settings.users_roles import (
    generate_temporary_password as generate_temporary_password,
)
from pr_system_settings.users_roles import validate_password as validate_password
from pwdlib import PasswordHash

from app.settings import get_settings


def hash_password(password: str) -> str:
    return _password_hash().hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    return _password_hash().verify_and_update(password, password_hash)


def _password_hash() -> PasswordHash:
    """今の argon2 の設定の hasher（共通の `password_hasher` は設定の値ごとに作り置く）。

    別の製品・別の worker が共通 `.env` を書き換え、`get_settings()` が読み直した値に追従する
    （以前はプロセスで 1 回だけ作っていた。#2102）。
    """
    settings = get_settings()
    return password_hasher(
        settings.app_auth_argon2_time_cost,
        settings.app_auth_argon2_memory_kib,
        settings.app_auth_argon2_parallelism,
    )
