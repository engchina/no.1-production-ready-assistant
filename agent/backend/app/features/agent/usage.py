"""業務 Agent の Run のモデル利用量の集計（#772）。

Run に記録した `RunUsage`（組み込み Runtime が SDK の `Usage` から書く）を、期間・業務 Agent・
利用者・モデル・日ごとに合計する。期間は Run の作成日時で決め、日は利用者のタイムゾーン（画面の
ブラウザの IANA 名または固定時差）で区切る。

- Oracle の構成は、保存した Run の事実（`AGENT_RUN_FACTS`）を SQL で集計する
  （`run_facts_store.OracleRunFactsStore`。#794）。
- memory / file の構成は、メモリの Run から作った事実（`RunFact`）をここで集計する。
  対象の Run（利用できる業務 Agent の Run だけ）は呼び出し側が渡す。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, tzinfo
from typing import Literal

from pydantic import BaseModel, Field

from app.features.agent.run_facts import REPORT_PERIOD_DAYS, RunFact
from app.features.agent.timezones import (
    resolve_timezone as resolve_timezone,
)
from app.features.agent.timezones import (
    timezone_name,
)

# 画面で選べる期間（日）。
USAGE_PERIOD_DAYS: tuple[int, ...] = REPORT_PERIOD_DAYS
# 集計元。history = 保存した Run の事実（Oracle）、memory = このプロセスのメモリの Run。
ReportSource = Literal["history", "memory"]


class UsageTotals(BaseModel):
    runs: int = 0
    # 利用量を記録した Run（#772 より前の Run・モデルを呼ぶ前に止まった Run は数えない）。
    runs_with_usage: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, fact: RunFact) -> None:
        self.runs += 1
        if not fact.has_usage:
            return
        self.runs_with_usage += 1
        self.requests += fact.requests or 0
        self.input_tokens += fact.input_tokens or 0
        self.output_tokens += fact.output_tokens or 0
        self.total_tokens += fact.total_tokens or 0


class AgentUsage(UsageTotals):
    agent_id: str
    agent_name: str


class UserUsage(UsageTotals):
    # 作成者のない Run（#233 より前）は None。
    user_uuid: str | None
    display_name: str


class ModelUsage(UsageTotals):
    # 利用量が記録済みでモデル名だけが無い場合は空文字。
    model: str


class DailyUsage(UsageTotals):
    day: date


class UsageReport(BaseModel):
    days: int
    source: ReportSource = "memory"
    timezone: str
    since: datetime
    until: datetime
    totals: UsageTotals
    # 直前の同じ長さの期間（増減の比較に使う）。
    previous: UsageTotals
    by_agent: list[AgentUsage] = Field(default_factory=list)
    by_user: list[UserUsage] = Field(default_factory=list)
    by_model: list[ModelUsage] = Field(default_factory=list)
    by_day: list[DailyUsage] = Field(default_factory=list)


def usage_period(days: int, now: datetime, tz: tzinfo) -> tuple[date, datetime, datetime]:
    """期間の初日・始まり（初日の 0 時）・直前の期間の始まり。"""
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    since = datetime.combine(first_day, datetime.min.time(), tzinfo=tz)
    return first_day, since, since - timedelta(days=days)


def empty_days(first_day: date, days: int) -> dict[date, DailyUsage]:
    return {
        first_day + timedelta(days=offset): DailyUsage(day=first_day + timedelta(days=offset))
        for offset in range(days)
    }


def build_usage_report(
    facts: Iterable[RunFact],
    *,
    days: int,
    now: datetime,
    tz: tzinfo,
    agent_names: Mapping[str, str],
    user_names: Callable[[list[str]], Mapping[str, str]],
    source: ReportSource = "memory",
) -> UsageReport:
    """期間（今日を含む `days` 日）と直前の同じ長さの期間の利用量を集計する。

    `user_names` は作成者の user_uuid の一覧を受け取り、表示名を返す（見つからない人は省く）。
    """
    first_day, since, previous_since = usage_period(days, now, tz)
    totals = UsageTotals()
    previous = UsageTotals()
    by_agent: dict[str, AgentUsage] = {}
    by_user: dict[str | None, UserUsage] = {}
    by_model: dict[str, ModelUsage] = {}
    by_day = empty_days(first_day, days)

    for fact in facts:
        created = fact.created_at
        if created > now:
            continue
        if created < since:
            if created >= previous_since:
                previous.add(fact)
            continue
        totals.add(fact)
        by_agent.setdefault(
            fact.agent_id,
            AgentUsage(
                agent_id=fact.agent_id,
                agent_name=agent_names.get(fact.agent_id) or fact.agent_name,
            ),
        ).add(fact)
        by_user.setdefault(
            fact.user_uuid,
            UserUsage(user_uuid=fact.user_uuid, display_name=""),
        ).add(fact)
        # 欠測をモデルの0利用量として扱わない。記録済みの0回・0 token は内訳に残す。
        if fact.has_usage:
            model = fact.model or ""
            by_model.setdefault(model, ModelUsage(model=model)).add(fact)
        day = created.astimezone(tz).date()
        if day in by_day:
            by_day[day].add(fact)

    return finish_usage_report(
        days=days,
        source=source,
        tz=tz,
        since=since,
        now=now,
        totals=totals,
        previous=previous,
        by_agent=by_agent.values(),
        by_user=by_user.values(),
        by_model=by_model.values(),
        by_day=by_day,
        user_names=user_names,
    )


def finish_usage_report(
    *,
    days: int,
    source: ReportSource,
    tz: tzinfo,
    since: datetime,
    now: datetime,
    totals: UsageTotals,
    previous: UsageTotals,
    by_agent: Iterable[AgentUsage],
    by_user: Iterable[UserUsage],
    by_model: Iterable[ModelUsage],
    by_day: Mapping[date, DailyUsage],
    user_names: Callable[[list[str]], Mapping[str, str]],
) -> UsageReport:
    """集計した値に利用者の表示名を入れ、並べて報告にする（Python と SQL の集計で共通）。"""
    users = list(by_user)
    names = user_names([item.user_uuid for item in users if item.user_uuid])
    for item in users:
        if item.user_uuid:
            item.display_name = names.get(item.user_uuid, "")

    return UsageReport(
        days=days,
        source=source,
        timezone=timezone_name(tz),
        since=since,
        until=now,
        totals=totals,
        previous=previous,
        by_agent=_ranked(by_agent),
        by_user=_ranked(users),
        by_model=_ranked(by_model),
        by_day=[by_day[day] for day in sorted(by_day)],
    )


def _ranked[T: UsageTotals](items: Iterable[T]) -> list[T]:
    """token の多い順（同じなら Run の多い順）。"""
    return sorted(items, key=lambda item: (-item.total_tokens, -item.runs))
