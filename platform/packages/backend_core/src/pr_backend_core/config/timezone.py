"""3 製品共通の時間帯の設定（`PLATFORM_TIMEZONE`。#3255）。

値の意味は `pr_backend_core.timezones`。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ..timezones import DEFAULT_TIMEZONE, validate_timezone_name


class TimezoneSettings(BaseModel):
    timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        description="利用者の暦の時間帯（IANA の地域名）。今日（基準日）・既定の時間帯に使う",
    )

    @field_validator("timezone", mode="before")
    @classmethod
    def _validate_timezone(cls, value: object) -> str:
        return validate_timezone_name(value)
