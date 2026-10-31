"""チャットの回答へのフィードバックの集計と一覧（#774）。

Run に保存した評価（`RunFeedback`）を、期間（評価の更新日時）・業務 Agent で絞って集計し、
評価・理由でさらに絞った一覧を新しい順にページで返す。集計は RAG の「フィードバック集計」と同じく、
前の同じ長さの期間と比べる。

- Oracle の構成は、保存した Run の事実（`AGENT_RUN_FACTS`）を SQL で集計し、一覧も SQL で
  ページングする（`run_facts_store.OracleRunFactsStore`。#794）。
- memory / file の構成は、メモリの Run から作った事実（`RunFact`）をここで集計する。
  対象の Run（利用できる業務 Agent の Run だけ）は呼び出し側が渡す。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta

from pr_backend_core import Page
from pr_backend_core.api import DEFAULT_PAGE_LIMIT, paginate
from pydantic import BaseModel, Field

from app.features.agent.run_facts import REPORT_PERIOD_DAYS, RunFact
from app.features.agent.runtime import FeedbackRating, FeedbackReason, RunFeedback
from app.features.agent.usage import ReportSource

# 画面で選べる期間（日）。
FEEDBACK_PERIOD_DAYS: tuple[int, ...] = REPORT_PERIOD_DAYS
# 一覧の 1 ページの既定と上限（画面の `Pagination` は既定 25 件/ページ。#1952）。
FEEDBACK_PAGE_SIZE = DEFAULT_PAGE_LIMIT
FEEDBACK_PAGE_SIZE_MAX = 100


class ReasonCount(BaseModel):
    reason: FeedbackReason
    count: int


class FeedbackSummary(BaseModel):
    """利用者（会話の本人）の評価の集計と、管理者の評価の件数。"""

    # 本人か管理者の評価がある実行の数（両方あっても 1 件）。一覧の全件数。
    rated_runs: int = 0
    total: int = 0
    helpful: int = 0
    not_helpful: int = 0
    # 評価が 0 件のときは None（画面は「—」）。
    helpful_rate: float | None = None
    reason_counts: list[ReasonCount] = Field(default_factory=list)
    # 管理者の評価（#774）。
    admin_reviewed: int = 0
    admin_not_helpful: int = 0


class FeedbackItem(BaseModel):
    run_id: str
    thread_id: str | None
    agent_id: str
    agent_name: str
    # 会話の本人（Run の作成者）。
    user_uuid: str | None
    display_name: str
    question: str
    answer: str
    # 本人の評価・管理者の評価（どちらかは必ずある）。
    feedback: RunFeedback | None = None
    admin_review: RunFeedback | None = None
    reviewer_display_name: str = ""
    # 新しい方の評価の日時。
    updated_at: datetime


class FeedbackReport(Page[FeedbackItem]):
    """フィードバックの集計と一覧（共通の `Page` に集計を足した形。#1266）。

    `items` は絞り込みに合う評価のうち `offset` から `limit` 件（新しい順）、`total` は
    絞り込みに合う評価の件数（ページングの総数。`summary.total` は期間内の評価の件数で別）。
    """

    days: int
    source: ReportSource = "memory"
    since: datetime
    until: datetime
    summary: FeedbackSummary
    # 直前の同じ長さの期間（増減の比較に使う）。
    previous: FeedbackSummary


def _matches(fact: RunFact, rating: FeedbackRating | None, reason: FeedbackReason | None) -> bool:
    """評価・理由の絞り込み（本人の評価か管理者の評価のどちらかが合えばよい）。"""
    return any(
        item is not None
        and (rating is None or item.rating == rating)
        and (reason is None or item.reason == reason)
        for item in (fact.feedback, fact.admin_review)
    )


def build_feedback_report(
    facts: Iterable[RunFact],
    *,
    days: int,
    now: datetime,
    agent_id: str | None,
    rating: FeedbackRating | None,
    reason: FeedbackReason | None,
    agent_names: Mapping[str, str],
    user_names: Callable[[list[str]], Mapping[str, str]],
    offset: int = 0,
    limit: int = FEEDBACK_PAGE_SIZE,
    source: ReportSource = "memory",
) -> FeedbackReport:
    since = now - timedelta(days=days)
    previous_since = since - timedelta(days=days)
    current: list[RunFact] = []
    previous: list[RunFact] = []
    for fact in facts:
        rated_at = fact.rated_at
        if rated_at is None or (agent_id and fact.agent_id != agent_id):
            continue
        if since <= rated_at <= now:
            current.append(fact)
        elif previous_since <= rated_at < since:
            previous.append(fact)

    matched = [fact for fact in current if _matches(fact, rating, reason)]
    # 新しい順（同じ日時は Run の ID の降順。SQL の ORDER BY と同じ）。
    matched.sort(key=lambda fact: (fact.rated_at or now, fact.run_id), reverse=True)
    return FeedbackReport(
        days=days,
        source=source,
        since=since,
        until=now,
        summary=summarize_feedback(current),
        previous=summarize_feedback(previous),
        **dict(
            paginate(
                feedback_items(matched[offset : offset + limit], agent_names, user_names, now),
                total=len(matched),
                limit=limit,
                offset=offset,
            )
        ),
    )


def summarize_feedback(facts: list[RunFact]) -> FeedbackSummary:
    reasons: dict[FeedbackReason, int] = {}
    summary = FeedbackSummary()
    for fact in facts:
        summary.rated_runs += int(fact.feedback is not None or fact.admin_review is not None)
        review = fact.admin_review
        if review is not None:
            summary.admin_reviewed += 1
            if review.rating == FeedbackRating.NOT_HELPFUL:
                summary.admin_not_helpful += 1
        feedback = fact.feedback
        if feedback is None:
            continue
        summary.total += 1
        if feedback.rating == FeedbackRating.HELPFUL:
            summary.helpful += 1
        else:
            summary.not_helpful += 1
            if feedback.reason is not None:
                reasons[feedback.reason] = reasons.get(feedback.reason, 0) + 1
    return finish_summary(summary, reasons)


def finish_summary(
    summary: FeedbackSummary, reasons: Mapping[FeedbackReason, int]
) -> FeedbackSummary:
    """役に立った割合と、理由の件数の多い順（同じなら理由の定義順）を入れる（SQL の集計と共通）。"""
    if summary.total:
        summary.helpful_rate = summary.helpful / summary.total
    order = list(FeedbackReason)
    ranked = sorted(reasons.items(), key=lambda pair: (-pair[1], order.index(pair[0])))
    summary.reason_counts = [ReasonCount(reason=item, count=count) for item, count in ranked]
    return summary


def feedback_items(
    facts: list[RunFact],
    agent_names: Mapping[str, str],
    user_names: Callable[[list[str]], Mapping[str, str]],
    now: datetime,
) -> list[FeedbackItem]:
    """一覧の行（会話の本人と管理者の表示名を入れる）。"""
    people = {fact.user_uuid for fact in facts if fact.user_uuid} | {
        fact.admin_review.user_uuid
        for fact in facts
        if fact.admin_review is not None and fact.admin_review.user_uuid
    }
    names = user_names(sorted(people)) if people else {}
    items: list[FeedbackItem] = []
    for fact in facts:
        owner = fact.user_uuid
        review = fact.admin_review
        reviewer = review.user_uuid if review is not None else None
        items.append(
            FeedbackItem(
                run_id=fact.run_id,
                thread_id=fact.thread_id,
                agent_id=fact.agent_id,
                agent_name=agent_names.get(fact.agent_id) or fact.agent_name,
                user_uuid=owner,
                display_name=names.get(owner, "") if owner else "",
                question=fact.question,
                answer=fact.answer or "",
                feedback=fact.feedback,
                admin_review=review,
                reviewer_display_name=names.get(reviewer, "") if reviewer else "",
                updated_at=fact.rated_at or now,
            )
        )
    return items
