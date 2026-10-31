"""Argon2id password hash（3製品共通。#212）。policy と一時 password は users_roles。"""

from __future__ import annotations

import secrets
from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher


@lru_cache(maxsize=8)
def password_hasher(time_cost: int, memory_kib: int, parallelism: int) -> PasswordHash:
    return PasswordHash(
        (
            Argon2Hasher(
                time_cost=time_cost,
                memory_cost=memory_kib,
                parallelism=parallelism,
            ),
        )
    )


def hash_password(password: str, *, time_cost: int, memory_kib: int, parallelism: int) -> str:
    return password_hasher(time_cost, memory_kib, parallelism).hash(password)


def verify_password(
    password: str, password_hash: str, *, time_cost: int, memory_kib: int, parallelism: int
) -> tuple[bool, str | None]:
    """照合結果と、パラメータ更新が必要なときの新しい hash を返す。"""
    return password_hasher(time_cost, memory_kib, parallelism).verify_and_update(
        password, password_hash
    )


@lru_cache(maxsize=8)
def dummy_password_hash(time_cost: int, memory_kib: int, parallelism: int) -> str:
    """照合の時間をそろえるためのダミーの hash（argon2 のパラメータごとに process で 1 回作る）。

    元の文字列は乱数で、どの入力とも一致しない。保存・ログ出力はしない。
    """
    return hash_password(
        secrets.token_urlsafe(32),
        time_cost=time_cost,
        memory_kib=memory_kib,
        parallelism=parallelism,
    )


def verify_dummy_password(
    password: str, *, time_cost: int, memory_kib: int, parallelism: int
) -> None:
    """存在しない・使えないユーザーのログインでも、本物と同じ重さの argon2 の照合をする。

    応答時間でログインユーザー ID の有無・状態が分からないようにする（OWASP Authentication
    Cheat Sheet の「Authentication Responses」。#1105）。結果は使わない。
    """
    password_hasher(time_cost, memory_kib, parallelism).verify(
        password, dummy_password_hash(time_cost, memory_kib, parallelism)
    )
