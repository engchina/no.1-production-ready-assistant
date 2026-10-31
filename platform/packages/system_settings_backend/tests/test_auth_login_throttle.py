"""ログインの試行の回数の制限（#1087）。

構成管理者（`system_admin`）・DB のユーザー・存在しないユーザーのどれでも、失敗の回数を
ログイン ID と送信元 IP の組・送信元 IP ごとに数え、上限に達したら 429 と `Retry-After` を返す。
時計は差し替えて、窓が過ぎるのをテストで待たない。
"""

from __future__ import annotations

import pytest
from test_auth import ADMIN_PASSWORD, CONFIGURED_PASSWORD, _service, _Settings

from pr_system_settings.auth import passwords
from pr_system_settings.auth.errors import LOGIN_RATE_LIMITED_MESSAGE, SecurityApiError
from pr_system_settings.auth.login_throttle import LoginThrottle, LoginThrottleLimits

WRONG_PASSWORD = "WrongPassword!123"  # nosec B105 - テスト用
IP = "203.0.113.10"
OTHER_IP = "198.51.100.20"
WINDOW_SECONDS = 15 * 60


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _throttled_service(clock: _Clock, **settings: int):  # type: ignore[no-untyped-def]
    service, store = _service()
    for name, value in settings.items():
        setattr(service.settings, name, value)
    service.login_throttle = LoginThrottle(clock=clock)
    return service, store


def _fail(service: object, login_user_id: str, *, ip: str = IP) -> SecurityApiError:
    with pytest.raises(SecurityApiError) as exc:
        service.login(login_user_id, WRONG_PASSWORD, client_ip=ip)  # type: ignore[attr-defined]
    return exc.value


def _assert_unauthorized(service: object, login_user_id: str, *, ip: str = IP) -> None:
    error = _fail(service, login_user_id, ip=ip)
    assert error.status_code == 401, error.public_message


def _assert_rate_limited(error: SecurityApiError, retry_after: int) -> None:
    assert error.status_code == 429
    assert error.code == "SECURITY_RATE_LIMITED"
    assert error.public_message == LOGIN_RATE_LIMITED_MESSAGE
    assert error.retryable is True
    assert error.headers == {"Retry-After": str(retry_after)}


def test_configured_system_admin_is_rate_limited_after_failures(clock: _Clock) -> None:
    service, _ = _throttled_service(clock)
    assert _Settings().app_auth_login_attempt_limit == 5
    for _ in range(5):
        _assert_unauthorized(service, "system_admin")
        clock.now += 10

    # 6 回目は、正しいパスワードでも照合せずに 429（何回でも試せないようにする）。
    with pytest.raises(SecurityApiError) as exc:
        service.login("system_admin", CONFIGURED_PASSWORD, client_ip=IP)
    # 最も古い失敗（50 秒前）が 15 分の窓から出るまで。
    _assert_rate_limited(exc.value, WINDOW_SECONDS - 50)


def test_rate_limit_counts_login_id_case_insensitively(clock: _Clock) -> None:
    service, _ = _throttled_service(clock)
    # 大文字・小文字と前後の空白を変えても、同じログイン ID として数える。
    for login_user_id in (
        "system_admin",
        "System_Admin",
        " SYSTEM_ADMIN ",
        "system_admin",
        "SYSTEM_admin",
    ):
        _assert_unauthorized(service, login_user_id)
    _assert_rate_limited(_fail(service, "system_admin"), WINDOW_SECONDS)


def test_rate_limit_is_lifted_after_window(clock: _Clock) -> None:
    service, _ = _throttled_service(clock)
    for _ in range(5):
        _assert_unauthorized(service, "system_admin")
    _assert_rate_limited(_fail(service, "system_admin"), WINDOW_SECONDS)

    clock.now += WINDOW_SECONDS - 1
    _assert_rate_limited(_fail(service, "system_admin"), 1)

    clock.now += 1
    principal, _, _ = service.login("system_admin", CONFIGURED_PASSWORD, client_ip=IP)
    assert principal.is_system_admin


def test_success_clears_failures_for_login_id(clock: _Clock) -> None:
    service, _ = _throttled_service(clock)
    for _ in range(4):
        _assert_unauthorized(service, "system_admin")
    service.login("system_admin", CONFIGURED_PASSWORD, client_ip=IP)

    # 成功で回数が消えるので、もう一度 5 回まで 401（6 回目で 429）。
    for _ in range(5):
        _assert_unauthorized(service, "system_admin")
    _assert_rate_limited(_fail(service, "system_admin"), WINDOW_SECONDS)


def test_db_user_and_unknown_user_get_same_counting_and_response(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DB ユーザーのロック（失敗 3 回）より上限を小さくし、制限の数え方だけを比べる。
    service, _ = _throttled_service(clock, app_auth_login_attempt_limit=2)
    results: dict[str, list[tuple[int, str, dict[str, str]]]] = {}
    for login_user_id in ("admin", "nobody"):
        results[login_user_id] = [
            (error.status_code, error.public_message, error.headers)
            for error in (_fail(service, login_user_id) for _ in range(3))
        ]
    assert results["admin"] == results["nobody"]
    assert [status for status, _, _ in results["admin"]] == [401, 401, 429]

    # 制限中は、ユーザーの有無に関わらずパスワードを照合しない（応答時間で区別させない）。
    def _no_verify(*args: object, **kwargs: object) -> object:
        raise AssertionError("制限中にパスワードを照合した")

    monkeypatch.setattr(passwords, "password_hasher", _no_verify)
    for login_user_id in ("admin", "nobody"):
        _assert_rate_limited(_fail(service, login_user_id), WINDOW_SECONDS)


def test_db_user_lockout_still_applies_below_rate_limit(clock: _Clock) -> None:
    service, store = _throttled_service(clock)
    for _ in range(3):
        _assert_unauthorized(service, "ADMIN")
    user = store.get_user_by_login_user_id("admin")
    assert user is not None and user.locked_until is not None
    # DB のロック中は、試行の上限に達していなくても正しいパスワードで 401（従来どおり）。
    with pytest.raises(SecurityApiError) as exc:
        service.login("ADMIN", ADMIN_PASSWORD, client_ip=IP)
    assert exc.value.status_code == 401


def test_other_ip_is_not_blocked_by_failures_for_same_login_id(clock: _Clock) -> None:
    """別の送信元の失敗で、構成管理者が締め出されない（Issue #1087 の案 1 の副作用を避ける）。"""
    service, _ = _throttled_service(clock)
    for _ in range(5):
        _assert_unauthorized(service, "system_admin", ip=OTHER_IP)
    _assert_rate_limited(_fail(service, "system_admin", ip=OTHER_IP), WINDOW_SECONDS)

    principal, _, _ = service.login("system_admin", CONFIGURED_PASSWORD, client_ip=IP)
    assert principal.is_system_admin


def test_ip_limit_blocks_spraying_many_login_ids(clock: _Clock) -> None:
    service, _ = _throttled_service(clock, app_auth_login_ip_attempt_limit=4)
    for index in range(4):
        _assert_unauthorized(service, f"user{index}")
    # 送信元 IP の上限に達したら、まだ試していないログイン ID も 429。
    _assert_rate_limited(_fail(service, "system_admin"), WINDOW_SECONDS)
    # 別の送信元は影響を受けない。
    _assert_unauthorized(service, "system_admin", ip=OTHER_IP)


def test_zero_limits_disable_rate_limit(clock: _Clock) -> None:
    service, _ = _throttled_service(
        clock, app_auth_login_attempt_limit=0, app_auth_login_ip_attempt_limit=0
    )
    for _ in range(10):
        _assert_unauthorized(service, "system_admin")


def test_throttle_prunes_expired_keys(clock: _Clock) -> None:
    throttle = LoginThrottle(clock=clock)
    limits = LoginThrottleLimits(per_login_and_ip=5, per_ip=20, window_seconds=60)
    attempt = throttle.begin("a", IP, limits)
    assert attempt.retry_after_seconds is None
    throttle.record_failure(attempt)
    assert not throttle.memory.is_empty()
    clock.now += 60
    # 窓を過ぎた失敗は数えず、鍵も消える。
    assert throttle.memory.purge_expired(60) == 2
    assert throttle.memory.is_empty()
