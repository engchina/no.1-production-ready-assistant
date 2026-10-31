"""利用状況のブラウザのタイムゾーン。

画面はブラウザの時間帯を送る。送られない（API / MCP から省いた）ときは、3 製品共通の設定の時間帯
（`PLATFORM_TIMEZONE`。既定 UTC。#3255）を使う。
"""

from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pr_backend_core.timezones import configured_timezone_name

from app.settings import get_settings

__all__ = ["default_timezone_name", "resolve_timezone", "timezone_name"]

_OFFSET = re.compile(r"([+-])([01][0-9]|2[0-3]):([0-5][0-9])")


def default_timezone_name() -> str:
    """時間帯を省いたときの既定（共通の設定の時間帯）。"""
    return configured_timezone_name(get_settings())


def resolve_timezone(name: str | None) -> tzinfo:
    """IANA 名と ±HH:MM の固定時差を解決し、不正な値は拒否する。空は共通の設定の時間帯。"""
    value = (name or "").strip() or default_timezone_name()
    match = _OFFSET.fullmatch(value)
    if match:
        sign, hours, minutes = match.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        return timezone(-delta if sign == "-" else delta, name=value)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("タイムゾーンが正しくありません。") from exc


def timezone_name(tz: tzinfo) -> str:
    """地域名を保持し、固定時差はブラウザと同じ表記で返す。"""
    return tz.key if isinstance(tz, ZoneInfo) else str(tz.tzname(None))
