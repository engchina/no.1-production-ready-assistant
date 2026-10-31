"""ログインの応答時間でユーザーの有無・状態が分からないこと（#1105）。

存在しない・無効・ロック中のユーザーでも、存在するユーザーと同じ重さの argon2 の照合をしてから
同じ 401 を返す（OWASP Authentication Cheat Sheet の「Authentication Responses」）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_auth import ADMIN_PASSWORD, _service

from pr_system_settings.auth import passwords
from pr_system_settings.auth.errors import SecurityApiError

WRONG_PASSWORD = "WrongPassword!123"  # nosec B105 - テスト用


@pytest.fixture
def hashes_checked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """argon2 の照合に渡した hash を記録する（照合そのものは本物を呼ぶ）。"""
    checked: list[str] = []
    original = passwords.password_hasher

    class _Recording:
        def __init__(self, hasher: object) -> None:
            self._hasher = hasher

        def __getattr__(self, name: str) -> object:
            return getattr(self._hasher, name)

        def verify(self, password: str, password_hash: str) -> bool:
            checked.append(password_hash)
            return self._hasher.verify(password, password_hash)  # type: ignore[attr-defined]

        def verify_and_update(self, password: str, password_hash: str) -> tuple[bool, str | None]:
            checked.append(password_hash)
            return self._hasher.verify_and_update(password, password_hash)  # type: ignore[attr-defined]

    monkeypatch.setattr(
        passwords,
        "password_hasher",
        lambda *args: _Recording(original(*args)),
    )
    return checked


def _assert_login_failed(service: object, login_user_id: str) -> None:
    with pytest.raises(SecurityApiError) as exc:
        service.login(login_user_id, WRONG_PASSWORD)  # type: ignore[attr-defined]
    assert exc.value.status_code == 401
    assert exc.value.public_message == "ログインユーザーIDまたはパスワードを確認してください。"


def test_unknown_user_login_runs_argon2_with_dummy_hash(hashes_checked: list[str]) -> None:
    service, _ = _service()

    _assert_login_failed(service, "nobody")

    # 存在しないユーザーでも argon2 の照合を 1 回する（同じパラメータのダミーの hash）。
    assert len(hashes_checked) == 1
    assert hashes_checked[0].startswith("$argon2id$v=19$m=8,t=1,p=1$")


def test_disabled_and_locked_user_login_runs_argon2(hashes_checked: list[str]) -> None:
    service, store = _service()
    admin = store.get_user_by_login_user_id("admin")
    assert admin is not None

    store.users[admin.user_uuid] = replace(admin, status="DISABLED")
    _assert_login_failed(service, "admin")
    assert len(hashes_checked) == 1

    store.users[admin.user_uuid] = replace(
        admin, locked_until=datetime.now(UTC) + timedelta(minutes=5)
    )
    _assert_login_failed(service, "admin")
    assert len(hashes_checked) == 2
    # 無効・ロック中は失敗を数え直さない（ロックの延長をしない。従来どおり）。
    current = store.get_user_by_login_user_id("admin")
    assert current is not None and current.failed_login_count == 0


def test_dummy_hash_is_reused_and_never_matches() -> None:
    first = passwords.dummy_password_hash(1, 8, 1)
    assert first == passwords.dummy_password_hash(1, 8, 1)
    assert passwords.dummy_password_hash(1, 16, 1) != first
    passwords.verify_dummy_password(ADMIN_PASSWORD, time_cost=1, memory_kib=8, parallelism=1)
    assert not passwords.password_hasher(1, 8, 1).verify(ADMIN_PASSWORD, first)
