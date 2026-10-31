"""3 製品共通の時間帯（#3255）。

- **値**: 共通の設定（`platform/.env` の `PLATFORM_TIMEZONE`。属性は `timezone`）。
  既定は `UTC`（日本時間にするなら `Asia/Tokyo`。#3274）。IANA の地域名だけを受ける
  （夏時間のある地域も正しく扱うため、固定の offset は受けない）。
  配備では props の `platform.env` から書き、`platform/deploy/suite-init.sh` が
  Compute の OS の時間帯を合わせる。
- **使い道**: 「今日」（基準日）・既定の時間帯など、利用者の暦で決めるもの。
  OS の時間帯（`date.today()`）に頼らず、この設定から決める。
- **時刻の保存は UTC のまま**: DB の session の時間帯は
  `oracle_session.init_oracle_session` が `'+00:00'` に固定する。
  設定した時間帯で時刻を保存しない（瞬間を一意に保つ）。
- **業務データの SQL の「今」**: 業務データの SELECT を実行する間だけ、session の時間帯を
  `session_time_zone_offset` の offset にする（`oracle_session.local_session_time_zone`。#3265）。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 既定（`platform/.env.example`・`suite-init.sh` の既定と同じ）。
DEFAULT_TIMEZONE: Final = "UTC"
# IANA の地域名の形（`Asia/Tokyo`・`UTC`・`America/Argentina/Buenos_Aires`・`Etc/GMT-9`）。
# `suite-init.sh` も同じ形で確かめる。
_TIMEZONE_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*")


def validate_timezone_name(value: object) -> str:
    """IANA の地域名を確かめて返す。空は既定。固定の offset（`+09:00`）と未知の名前は拒む。"""
    name = str(value if value is not None else "").strip() or DEFAULT_TIMEZONE
    if not _TIMEZONE_NAME.fullmatch(name):
        raise ValueError(
            "時間帯は IANA の地域名（例: Asia/Tokyo）で指定してください"
            "（+09:00 のような固定の offset は使えません）。"
        )
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"時間帯 {name} が見つかりません。IANA の地域名を指定してください。"
        ) from exc
    return name


def configured_timezone_name(settings: object) -> str:
    """製品の Settings の `timezone`（共通の設定）。無い属性・不正な値は既定。"""
    try:
        return validate_timezone_name(getattr(settings, "timezone", DEFAULT_TIMEZONE))
    except ValueError:
        return DEFAULT_TIMEZONE


def configured_timezone(settings: object) -> ZoneInfo:
    """製品の Settings の時間帯。"""
    return ZoneInfo(configured_timezone_name(settings))


def local_now(settings: object, *, now: datetime | None = None) -> datetime:
    """設定した時間帯の今の時刻（timezone 付き）。`now` の時間帯の無い値は UTC とみなす。"""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(configured_timezone(settings))


def local_today(settings: object, *, now: datetime | None = None) -> date:
    """設定した時間帯の今日（基準日の既定）。OS の時間帯に左右されない。"""
    return local_now(settings, now=now).date()


def session_time_zone_offset(settings: object, *, now: datetime | None = None) -> str:
    """設定した時間帯の今の UTC との差（`+09:00` の形。Oracle の session の TIME_ZONE に渡す）。

    地域名（`Asia/Tokyo`）ではなく offset にする: 地域名の session の時間帯の
    TIMESTAMP WITH TIME ZONE は python-oracledb の thin mode で読めない（DPY-3022。#3241）。
    夏時間のある地域は今の時刻の差になるため、実行のたびに求める（接続に固定しない）。
    """
    offset = local_now(settings, now=now).utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


__all__ = [
    "DEFAULT_TIMEZONE",
    "configured_timezone",
    "configured_timezone_name",
    "local_now",
    "local_today",
    "session_time_zone_offset",
    "validate_timezone_name",
]
