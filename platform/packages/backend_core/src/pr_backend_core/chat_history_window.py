"""チャットの「会話の履歴」の一覧に出す期間（3 製品共通。#2340）。

4 つのチャット（RAG チャット・NL2SQL チャット・NL2SQL DB 管理チャット・Agent チャット）の
左の「会話の履歴」は、最後の更新が直近 N 日の会話だけを出す。表示の絞り込みで、会話は消さない
（期間の外の会話も、ID で開く API・実行の詳細の「会話を開く」・直接のリンクからは今のまま開ける）。

- **値**: 共通の設定（`platform/.env` の `PLATFORM_CHAT_HISTORY_DAYS`。属性は
  `chat_history_days`）。既定 90 日、範囲 0〜3650。0 は期間で絞らない（すべての会話を出す）。
- **基準の時刻**: 会話の最後の更新（続きの質問・名前の変更で進む時刻）。製品は一覧の件数と
  1 ページの両方を同じ条件で絞る（件数とページ送りがずれないように）。
- **応答**: 一覧の応答に期間の日数（`history_days`。0 は絞っていない）を付け、画面が
  「最近 N 日」を出す（文言は `@production-ready/ui` の `chatHistoryWindowLabels`）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import Field

from .schemas import Page

# 既定（`platform/.env.example` と 3 製品の Settings の既定）。
CHAT_HISTORY_DEFAULT_DAYS: Final = 90
# 設定で許す範囲（Settings の `ge` / `le`）。0 は期間で絞らない。
CHAT_HISTORY_DAYS_LIMIT: Final = 3650


def chat_history_days(settings: object) -> int:
    """製品の Settings の `chat_history_days`（共通の設定）。無い属性は既定、範囲の外は寄せる。"""
    raw = getattr(settings, "chat_history_days", CHAT_HISTORY_DEFAULT_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return CHAT_HISTORY_DEFAULT_DAYS
    return min(max(days, 0), CHAT_HISTORY_DAYS_LIMIT)


def chat_history_cutoff(days: int, *, now: datetime | None = None) -> datetime | None:
    """一覧に出す会話の最後の更新の下限（UTC。この時刻以後を出す）。

    `days` が 0 以下なら None（絞らない）。
    """
    if days <= 0:
        return None
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC) - timedelta(days=min(days, CHAT_HISTORY_DAYS_LIMIT))


def within_chat_history(updated_at: datetime | str | None, cutoff: datetime | None) -> bool:
    """会話の最後の更新が期間の中か（メモリの一覧の絞り込み）。`cutoff` が None なら常に True。

    時刻は datetime か ISO 8601 の文字列（タイムゾーンの無い値は UTC とみなす）。読めない・空の値は
    期間の外とする（最後の更新の分からない会話を「最近」として出さない）。
    """
    if cutoff is None:
        return True
    if isinstance(updated_at, str):
        text = updated_at.strip()
        if not text:
            return False
        try:
            updated_at = datetime.fromisoformat(text)
        except ValueError:
            return False
    if not isinstance(updated_at, datetime):
        return False
    moment = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    return moment >= cutoff


class ChatHistoryPage[T](Page[T]):
    """会話の履歴の 1 ページ（共通の `Page` に、一覧を絞った期間の日数を足す）。"""

    history_days: int = Field(
        default=0, ge=0, description="一覧を絞った期間の日数（最後の更新が直近 N 日）。0 は絞らない"
    )

    @classmethod
    def from_page(cls, page: Page[T], *, history_days: int) -> ChatHistoryPage[T]:
        """共通の `Page`（`paginate` / `empty_page` の結果）に期間の日数を付ける。"""
        return cls(
            items=list(page.items),
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_next=page.has_next,
            history_days=max(history_days, 0),
        )


__all__ = [
    "CHAT_HISTORY_DAYS_LIMIT",
    "CHAT_HISTORY_DEFAULT_DAYS",
    "ChatHistoryPage",
    "chat_history_cutoff",
    "chat_history_days",
    "within_chat_history",
]
