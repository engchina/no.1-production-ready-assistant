"""チャットの「会話の履歴」の期間（#2340）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pr_backend_core import Page
from pr_backend_core.chat_history_window import (
    CHAT_HISTORY_DAYS_LIMIT,
    CHAT_HISTORY_DEFAULT_DAYS,
    ChatHistoryPage,
    chat_history_cutoff,
    chat_history_days,
    within_chat_history,
)
from pr_backend_core.config import PLATFORM_SETTING_FIELDS, settings_env_name

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def test_setting_is_shared_platform_field() -> None:
    assert "chat_history_days" in PLATFORM_SETTING_FIELDS
    assert settings_env_name("RAG_", "chat_history_days") == "PLATFORM_CHAT_HISTORY_DAYS"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, CHAT_HISTORY_DEFAULT_DAYS),
        (30, 30),
        (0, 0),
        (-5, 0),
        (CHAT_HISTORY_DAYS_LIMIT + 1, CHAT_HISTORY_DAYS_LIMIT),
        ("bad", CHAT_HISTORY_DEFAULT_DAYS),
    ],
    ids=["missing", "value", "zero", "negative", "over-limit", "invalid"],
)
def test_chat_history_days_reads_settings(value: object, expected: int) -> None:
    settings = SimpleNamespace() if value is None else SimpleNamespace(chat_history_days=value)
    assert chat_history_days(settings) == expected


def test_cutoff_is_days_before_now_in_utc() -> None:
    assert chat_history_cutoff(90, now=NOW) == NOW - timedelta(days=90)
    # タイムゾーンの違う今も UTC にする。
    jst = NOW.astimezone(timezone(timedelta(hours=9)))
    cutoff = chat_history_cutoff(1, now=jst)
    assert cutoff == NOW - timedelta(days=1)
    assert cutoff is not None and cutoff.tzinfo == UTC
    # タイムゾーンの無い今は UTC とみなす。
    assert chat_history_cutoff(1, now=NOW.replace(tzinfo=None)) == NOW - timedelta(days=1)


def test_zero_days_does_not_filter() -> None:
    assert chat_history_cutoff(0, now=NOW) is None
    assert within_chat_history(datetime(2000, 1, 1, tzinfo=UTC), None)
    assert within_chat_history(None, None)


def test_within_chat_history_compares_last_update() -> None:
    cutoff = chat_history_cutoff(90, now=NOW)
    assert within_chat_history(NOW - timedelta(days=89), cutoff)
    assert within_chat_history(NOW - timedelta(days=90), cutoff)  # 境界は含む
    assert not within_chat_history(NOW - timedelta(days=91), cutoff)
    # ISO 8601 の文字列（保存の値）と、タイムゾーンの無い値（UTC とみなす）。
    assert within_chat_history((NOW - timedelta(days=1)).isoformat(), cutoff)
    assert not within_chat_history("2026-01-01T00:00:00", cutoff)
    # 最後の更新の分からない会話は出さない。
    assert not within_chat_history("", cutoff)
    assert not within_chat_history("not-a-time", cutoff)
    assert not within_chat_history(None, cutoff)


def test_chat_history_page_carries_history_days() -> None:
    page = Page[str](items=["a"], total=1, limit=25, offset=0, has_next=False)
    converted = ChatHistoryPage[str].from_page(page, history_days=90)
    assert converted.model_dump() == {
        "items": ["a"],
        "total": 1,
        "limit": 25,
        "offset": 0,
        "has_next": False,
        "history_days": 90,
    }
    assert ChatHistoryPage[str].from_page(page, history_days=-1).history_days == 0
