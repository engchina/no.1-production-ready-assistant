"""検索の基準日(as_of)の既定を、設定の時間帯の今日で 1 回の検索の初めに決める(#3249)。

OS の時間帯(`date.today()`)は使わない。日本時間の日付の境界(UTC 14:59 / 15:00 = JST 23:59 / 0:00)を
固定した時計で確かめる。DB は呼ばない(検索の下流の関数を差し替えて、渡った条件を読む)。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from rag_engine.adapters.oracle.store import _classification_filter_sql, search_adb_hybrid_chunks
from rag_engine.config import DEFAULT_TIMEZONE, get_settings
from rag_engine.knowledge.classification import (
    ClassificationFilter,
    business_today,
    classification_filter_from_values,
    with_effective_as_of,
)

# 日本時間の 2026-04-01 0:00 の直前と直後(UTC では 2026-03-31)。
BEFORE_JST_MIDNIGHT = datetime(2026, 3, 31, 14, 59, tzinfo=UTC)
AT_JST_MIDNIGHT = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("timezone", "now", "expected"),
    [
        ("Asia/Tokyo", BEFORE_JST_MIDNIGHT, date(2026, 3, 31)),
        ("Asia/Tokyo", AT_JST_MIDNIGHT, date(2026, 4, 1)),
        ("UTC", AT_JST_MIDNIGHT, date(2026, 3, 31)),
        # 不明な名前・空は既定(UTC。#3274)。
        ("Asia/Osaka", AT_JST_MIDNIGHT, date(2026, 3, 31)),
        ("", AT_JST_MIDNIGHT, date(2026, 3, 31)),
    ],
    ids=["tokyo-before-midnight", "tokyo-at-midnight", "utc", "unknown-name", "empty"],
)
def test_business_today_uses_timezone_not_os_clock(timezone: str, now: datetime, expected: date) -> None:
    assert business_today(timezone, now=now) == expected


def test_business_today_treats_naive_time_as_utc() -> None:
    assert business_today("Asia/Tokyo", now=AT_JST_MIDNIGHT.replace(tzinfo=None)) == date(2026, 4, 1)


def test_with_effective_as_of_keeps_explicit_date_and_classification() -> None:
    explicit = classification_filter_from_values(large_category="10_業務A", as_of="2025-04-01")
    assert with_effective_as_of(explicit, date(2026, 4, 1)) is explicit

    filled = with_effective_as_of(classification_filter_from_values(large_category="10_業務A"), date(2026, 4, 1))
    assert filled.as_of == "2026-04-01"
    assert filled.large_category == explicit.large_category
    assert with_effective_as_of(None, date(2026, 4, 1)) == ClassificationFilter(as_of="2026-04-01")


def test_settings_timezone_comes_from_platform_timezone() -> None:
    assert get_settings(environ={}, dotenv_path=None).timezone == DEFAULT_TIMEZONE
    assert get_settings(environ={"PLATFORM_TIMEZONE": "UTC"}, dotenv_path=None).timezone == "UTC"


def test_direct_filter_sql_defaults_to_default_timezone_today() -> None:
    with patch(
        "rag_engine.adapters.oracle.store.business_today",
        side_effect=lambda timezone: business_today(timezone, now=AT_JST_MIDNIGHT),
    ):
        _sql, binds = _classification_filter_sql("c", None)
    # 既定(UTC。#3274)の今日。
    assert binds["as_of"] == "2026-03-31"


class _ConnectionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.parametrize(
    ("timezone", "requested", "expected"),
    [
        ("Asia/Tokyo", "", "2026-04-01"),
        ("UTC", "", "2026-03-31"),
        ("Asia/Tokyo", "2025-04-01", "2025-04-01"),
    ],
    ids=["tokyo-default", "utc-default", "explicit"],
)
def test_search_decides_as_of_once_for_every_downstream_query(timezone: str, requested: str, expected: str) -> None:
    """vector・Oracle Text・chunk の読み取りが、検索の初めに決めた同じ基準日で有効期間を判定する。"""
    settings = replace(
        get_settings(environ={}, dotenv_path=None),
        timezone=timezone,
        embedding_output_dimensions=1536,
        image_embedding_enabled=False,
    )
    seen: list[str] = []

    def _record(*_: object, classification_filter: ClassificationFilter | None = None, **__: object) -> list:
        seen.append(classification_filter.as_of if classification_filter is not None else "")
        return []

    def _count(*args: object, **kwargs: object) -> int:
        _record(*args, **kwargs)
        return 1

    with (
        patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
        patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext()),
        patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
        patch("rag_engine.adapters.oracle.store._available_embedding_count", side_effect=_count),
        patch("rag_engine.adapters.oracle.store._vector_search", side_effect=_record),
        patch("rag_engine.adapters.oracle.store._text_search", side_effect=_record),
        patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", side_effect=_record),
        patch(
            "rag_engine.adapters.oracle.store.business_today",
            side_effect=lambda tz: business_today(tz, now=AT_JST_MIDNIGHT),
        ) as today,
    ):
        search_adb_hybrid_chunks(
            chunk_run_id="run-1",
            retrieval_queries=["質問"],
            settings=settings,
            query_embedder=lambda *_: [0.1] * 1536,
            classification_filter=classification_filter_from_values(as_of=requested),
        )

    # 今日は検索の初めに 1 回だけ決める(下流で決め直さない)。
    assert today.call_count == 1
    assert seen
    assert set(seen) == {expected}
