from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from pr_backend_core.config import (
    PLATFORM_SETTING_FIELDS,
    BaseServiceSettings,
    PlatformEnvSourcesMixin,
    TimezoneSettings,
    product_settings_config,
    settings_env_name,
)
from pr_backend_core.timezones import (
    DEFAULT_TIMEZONE,
    configured_timezone,
    configured_timezone_name,
    local_now,
    local_today,
    session_time_zone_offset,
    validate_timezone_name,
)


class _ProductSettings(PlatformEnvSourcesMixin, TimezoneSettings, BaseSettings):
    model_config = product_settings_config(prefix="RAG_", backend_dir=Path("."))


def test_timezone_is_a_platform_setting_with_utc_default() -> None:
    assert "timezone" in PLATFORM_SETTING_FIELDS
    assert settings_env_name("RAG_", "timezone") == "PLATFORM_TIMEZONE"
    assert DEFAULT_TIMEZONE == "UTC"  # #3274
    assert BaseServiceSettings().timezone == "UTC"


def test_timezone_is_read_from_platform_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLATFORM_TIMEZONE", raising=False)
    env = tmp_path / "platform.env"
    env.write_text("PLATFORM_TIMEZONE=Asia/Tokyo\n", encoding="utf-8")
    assert _ProductSettings(_env_file=(env,)).timezone == "Asia/Tokyo"
    # 旧名（属性名と同じ環境変数名）は読まない（#211）。既定は UTC（#3274）。
    env.write_text("TIMEZONE=Asia/Tokyo\n", encoding="utf-8")
    assert _ProductSettings(_env_file=(env,)).timezone == "UTC"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Asia/Tokyo", "Asia/Tokyo"),
        (" UTC ", "UTC"),
        ("America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
        ("Etc/GMT-9", "Etc/GMT-9"),
        ("", "UTC"),
        (None, "UTC"),
    ],
    ids=["tokyo", "utc-trimmed", "three-levels", "etc-offset-zone", "empty", "none"],
)
def test_validate_timezone_name_accepts_iana_names(value: object, expected: str) -> None:
    assert validate_timezone_name(value) == expected


@pytest.mark.parametrize(
    "value",
    ["+09:00", "UTC+9", "Asia/Osaka", "../../etc/passwd", "Asia/Tokyo;rm", "/Asia/Tokyo"],
    ids=["offset", "utc-plus", "unknown-city", "path", "shell", "absolute"],
)
def test_validate_timezone_name_rejects_offsets_and_unknown_names(value: str) -> None:
    with pytest.raises(ValueError):
        validate_timezone_name(value)
    with pytest.raises(ValidationError):
        TimezoneSettings(timezone=value)


def test_configured_timezone_falls_back_to_default() -> None:
    # 既定は UTC（#3274）。
    assert configured_timezone_name(SimpleNamespace()) == "UTC"
    assert configured_timezone_name(SimpleNamespace(timezone="+09:00")) == "UTC"
    assert configured_timezone(SimpleNamespace(timezone="UTC")) == ZoneInfo("UTC")


def test_local_today_uses_configured_timezone_not_os_clock() -> None:
    # 日本の 0:00〜9:00 は UTC では前の日。基準日は設定した時間帯の日にする。
    moment = datetime(2026, 10, 24, 16, 30, tzinfo=UTC)  # 2026-10-25 01:30 JST
    assert local_today(SimpleNamespace(timezone="Asia/Tokyo"), now=moment) == date(2026, 10, 25)
    assert local_today(SimpleNamespace(timezone="UTC"), now=moment) == date(2026, 10, 24)
    # 時間帯の無い値は UTC とみなす。
    naive = datetime(2026, 10, 24, 16, 30)
    assert local_now(SimpleNamespace(timezone="Asia/Tokyo"), now=naive).isoformat() == (
        "2026-10-25T01:30:00+09:00"
    )
    assert local_now(SimpleNamespace(), now=naive).isoformat() == "2026-10-24T16:30:00+00:00"


@pytest.mark.parametrize(
    ("name", "now", "expected"),
    [
        ("Asia/Tokyo", datetime(2026, 10, 24, 23, 30, tzinfo=UTC), "+09:00"),
        ("UTC", datetime(2026, 10, 24, 23, 30, tzinfo=UTC), "+00:00"),
        # 夏時間のある地域は、その時刻の差（実行のたびに求める）。
        ("America/New_York", datetime(2026, 7, 1, 12, tzinfo=UTC), "-04:00"),
        ("America/New_York", datetime(2026, 12, 1, 12, tzinfo=UTC), "-05:00"),
        ("Asia/Kolkata", datetime(2026, 1, 1, tzinfo=UTC), "+05:30"),
    ],
    ids=["tokyo", "utc", "new-york-summer", "new-york-winter", "half-hour"],
)
def test_session_time_zone_offset_is_the_current_offset(
    name: str, now: datetime, expected: str
) -> None:
    # Oracle の session の TIME_ZONE は地域名ではなく offset（thin mode の DPY-3022。#3265）。
    assert session_time_zone_offset(SimpleNamespace(timezone=name), now=now) == expected
