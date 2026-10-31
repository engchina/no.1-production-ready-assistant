"""確かめるべき実行（暗黙のシグナル。#1822）。

フィードバックの明示の評価は回答のごく一部にしか付かないため、Run に残る暗黙のシグナルと自動の
検証の結果から「確かめるべき実行」を集める（LangSmith・Langfuse などの feedback → triage → eval →
fix の流れ）。モデルは呼ばない（保存した Run の事実だけで決める）。

シグナルの定義（Python の `fact_signals` と SQL の `SIGNAL_CONDITIONS` は同じ規則）:

- `not_helpful`: 本人か管理者の評価が「役に立たなかった」。
- `validation_failed`: 最終の検証が、根拠と矛盾する段落を外した・未確認の印を付けた・回答全体の
  警告（決定的な検査の error など）を出した・読めない根拠があった（`run_facts.VALIDATION_FAILED`。
  警告の印だけは数えない。#2738）。
- `unvalidated`: 最終の検証そのものが失敗した（`run_facts.VALIDATION_UNVALIDATED`）。
- `ungrounded_no_tools`: 資料の根拠を使わずに答え（RAG の根拠のツールを持つ Agent の
  `run_facts.VALIDATION_UNGROUNDED` か、持たない Agent の `VALIDATION_NO_EVIDENCE_TOOLS`）、
  業務データの道具（NL2SQL など RAG 以外の MCP 接続のツール）も成功していない
  （`DATA_TOOL_CALLS = 0`。#1834）。
  - 業務データの道具で答えた Run（資料とデータの横断の Agent のデータだけの質問）は数えない。
  - 拒答した Run（回答の対応が `unanswered` の語。ツールを呼ばずに答えた回答を拒答に置き換える
    ガード #1835 の `no_tool_grounding` を含む）は「答えられなかった」に数え、ここには数えない
    （ガードをすり抜けて本文を出した Run だけ）。
  - 記録の無い古い事実（`DATA_TOOL_CALLS` が NULL）も数えない。
- `unanswered`: 回答の対応が「資料から答えられない」（insufficient_evidence）か「人への引き継ぎ」
  （needs_human）。
- `approval_rejected`: 承認を却下したツールがある。
- `tool_failed`: 失敗したツールの呼び出しがある。
- `run_failed`: 実行が失敗した・取り消した。

対象は期間（Run を作った日時）と業務 Agent で絞った、終わった Run（完了・失敗・取り消し）。
品質評価の Run（起点 `evaluation`）は品質評価の画面で見るため含めない。Oracle の構成は保存した
Run の事実（`AGENT_RUN_FACTS`）を SQL で集計し（`run_facts_store`）、memory / file の構成は
メモリの Run から作った事実をここで集計する。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum

from pr_backend_core import Page
from pr_backend_core.api import DEFAULT_PAGE_LIMIT, paginate
from pydantic import BaseModel, Field

from app.features.agent.answer_outcome import INSUFFICIENT_EVIDENCE, NEEDS_HUMAN
from app.features.agent.run_facts import (
    RUN_SOURCE_EVALUATION,
    VALIDATION_FAILED,
    VALIDATION_NO_EVIDENCE_TOOLS,
    VALIDATION_UNGROUNDED,
    VALIDATION_UNVALIDATED,
    RunFact,
)
from app.features.agent.runtime import FeedbackRating, RunFeedback, RunStatus
from app.features.agent.usage import ReportSource

SIGNAL_PAGE_SIZE = DEFAULT_PAGE_LIMIT
SIGNAL_PAGE_SIZE_MAX = 100


class SignalKind(StrEnum):
    """確かめるべき実行のシグナルの種類（並びは画面の並び）。"""

    NOT_HELPFUL = "not_helpful"
    VALIDATION_FAILED = "validation_failed"
    UNVALIDATED = "unvalidated"
    UNGROUNDED_NO_TOOLS = "ungrounded_no_tools"
    UNANSWERED = "unanswered"
    APPROVAL_REJECTED = "approval_rejected"
    TOOL_FAILED = "tool_failed"
    RUN_FAILED = "run_failed"


# 「答えられなかった」とする回答の対応（#1305 の語彙）。
UNANSWERED_OUTCOMES: tuple[str, ...] = (INSUFFICIENT_EVIDENCE, NEEDS_HUMAN)
# 「根拠を使わずに答えた」とする最終の検証の結果（資料の根拠を使わなかった回答。#1834）。
UNGROUNDED_VALIDATIONS: tuple[str, ...] = (VALIDATION_UNGROUNDED, VALIDATION_NO_EVIDENCE_TOOLS)
# 「実行の失敗・取り消し」とする状態。
FAILED_RUN_STATUSES: tuple[str, ...] = (RunStatus.FAILED.value, RunStatus.CANCELLED.value)
# 対象にする Run の状態（終わった Run）。
SIGNAL_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
)


def _sql_list(values: Iterable[str]) -> str:
    # 固定の定数だけを並べる（利用者の入力は入れない）。
    return ", ".join(f"'{value}'" for value in values)


_NOT_HELPFUL = FeedbackRating.NOT_HELPFUL.value
# 種類ごとの SQL の条件（列は `AGENT_RUN_FACTS`。値は固定の定数）。
SIGNAL_CONDITIONS: dict[SignalKind, str] = {
    SignalKind.NOT_HELPFUL: (
        f"(FEEDBACK_RATING = '{_NOT_HELPFUL}' OR ADMIN_RATING = '{_NOT_HELPFUL}')"
    ),
    SignalKind.VALIDATION_FAILED: f"VALIDATION_RESULT = '{VALIDATION_FAILED}'",
    SignalKind.UNVALIDATED: f"VALIDATION_RESULT = '{VALIDATION_UNVALIDATED}'",
    SignalKind.UNGROUNDED_NO_TOOLS: (
        f"(VALIDATION_RESULT IN ({_sql_list(UNGROUNDED_VALIDATIONS)}) AND DATA_TOOL_CALLS = 0 "
        f"AND (ANSWER_OUTCOME IS NULL OR ANSWER_OUTCOME NOT IN ({_sql_list(UNANSWERED_OUTCOMES)})))"
    ),
    SignalKind.UNANSWERED: f"ANSWER_OUTCOME IN ({_sql_list(UNANSWERED_OUTCOMES)})",
    SignalKind.APPROVAL_REJECTED: "APPROVALS_REJECTED > 0",
    SignalKind.TOOL_FAILED: "TOOL_FAILURES > 0",
    SignalKind.RUN_FAILED: f"STATUS IN ({_sql_list(FAILED_RUN_STATUSES)})",
}
# どれかのシグナルがある。
ANY_SIGNAL_CONDITION = "(" + " OR ".join(SIGNAL_CONDITIONS[kind] for kind in SignalKind) + ")"
# 対象の Run（終わった Run・品質評価の Run を除く）。
SIGNAL_SCOPE_CONDITION = (
    f"STATUS IN ({_sql_list(SIGNAL_RUN_STATUSES)}) AND RUN_SOURCE <> '{RUN_SOURCE_EVALUATION}'"
)


def _not_helpful(item: RunFeedback | None) -> bool:
    return item is not None and item.rating == FeedbackRating.NOT_HELPFUL


def fact_signals(fact: RunFact) -> list[SignalKind]:
    """Run の事実のシグナル（`SIGNAL_CONDITIONS` と同じ規則。並びは `SignalKind` の順）。"""
    checks = {
        SignalKind.NOT_HELPFUL: _not_helpful(fact.feedback) or _not_helpful(fact.admin_review),
        SignalKind.VALIDATION_FAILED: fact.validation_result == VALIDATION_FAILED,
        SignalKind.UNVALIDATED: fact.validation_result == VALIDATION_UNVALIDATED,
        SignalKind.UNGROUNDED_NO_TOOLS: (
            fact.validation_result in UNGROUNDED_VALIDATIONS
            and fact.data_tool_calls == 0
            and fact.answer_outcome not in UNANSWERED_OUTCOMES
        ),
        SignalKind.UNANSWERED: fact.answer_outcome in UNANSWERED_OUTCOMES,
        SignalKind.APPROVAL_REJECTED: fact.approvals_rejected > 0,
        SignalKind.TOOL_FAILED: fact.tool_failures > 0,
        SignalKind.RUN_FAILED: fact.status in FAILED_RUN_STATUSES,
    }
    return [kind for kind in SignalKind if checks[kind]]


def in_signal_scope(fact: RunFact) -> bool:
    """対象の Run か（`SIGNAL_SCOPE_CONDITION` と同じ規則）。"""
    return fact.status in SIGNAL_RUN_STATUSES and fact.source != RUN_SOURCE_EVALUATION


class SignalCount(BaseModel):
    kind: SignalKind
    count: int = 0


class SignalSummary(BaseModel):
    """期間の対象の実行の数と、シグナルのある実行の数・種類ごとの数（1 つの実行が複数に入る）。"""

    runs: int = 0
    flagged: int = 0
    # 対象の実行が 0 件のときは None（画面は「—」）。
    flagged_rate: float | None = None
    # すべての種類（`SignalKind` の順。0 件も含む）。
    counts: list[SignalCount] = Field(default_factory=list)


class SignalItem(BaseModel):
    run_id: str
    thread_id: str | None
    agent_id: str
    agent_name: str
    user_uuid: str | None
    display_name: str
    question: str
    answer: str
    status: str
    signals: list[SignalKind]
    answer_outcome: str | None = None
    validation_result: str | None = None
    tool_failures: int = 0
    approvals_rejected: int = 0
    data_tool_calls: int | None = None
    feedback: RunFeedback | None = None
    admin_review: RunFeedback | None = None
    created_at: datetime


class SignalReport(Page[SignalItem]):
    """確かめるべき実行の集計と一覧（共通の `Page` に集計を足した形）。

    `items` は絞り込み（種類）に合う実行のうち `offset` から `limit` 件（新しい順）、`total` は
    絞り込みに合う実行の件数（ページングの総数）。
    """

    days: int
    source: ReportSource = "memory"
    since: datetime
    until: datetime
    kind: SignalKind | None = None
    summary: SignalSummary
    # 直前の同じ長さの期間（増減の比較に使う）。
    previous: SignalSummary


def finish_signal_summary(
    runs: int, flagged: int, counts: Mapping[SignalKind, int]
) -> SignalSummary:
    """件数から集計を作る（SQL の集計と共通）。"""
    return SignalSummary(
        runs=runs,
        flagged=flagged,
        flagged_rate=flagged / runs if runs else None,
        counts=[SignalCount(kind=kind, count=int(counts.get(kind, 0))) for kind in SignalKind],
    )


def summarize_signals(facts: Iterable[RunFact]) -> SignalSummary:
    runs = 0
    flagged = 0
    counts: dict[SignalKind, int] = {}
    for fact in facts:
        runs += 1
        signals = fact_signals(fact)
        if signals:
            flagged += 1
        for kind in signals:
            counts[kind] = counts.get(kind, 0) + 1
    return finish_signal_summary(runs, flagged, counts)


def build_signal_report(
    facts: Iterable[RunFact],
    *,
    days: int,
    now: datetime,
    agent_id: str | None,
    kind: SignalKind | None,
    agent_names: Mapping[str, str],
    user_names: Callable[[list[str]], Mapping[str, str]],
    offset: int = 0,
    limit: int = SIGNAL_PAGE_SIZE,
    source: ReportSource = "memory",
) -> SignalReport:
    since = now - timedelta(days=days)
    previous_since = since - timedelta(days=days)
    current: list[RunFact] = []
    previous: list[RunFact] = []
    for fact in facts:
        if not in_signal_scope(fact) or (agent_id and fact.agent_id != agent_id):
            continue
        if since <= fact.created_at <= now:
            current.append(fact)
        elif previous_since <= fact.created_at < since:
            previous.append(fact)

    matched = [
        fact
        for fact in current
        if (signals := fact_signals(fact)) and (kind is None or kind in signals)
    ]
    # 新しい順（同じ日時は Run の ID の降順。SQL の ORDER BY と同じ）。
    matched.sort(key=lambda fact: (fact.created_at, fact.run_id), reverse=True)
    return SignalReport(
        days=days,
        source=source,
        since=since,
        until=now,
        kind=kind,
        summary=summarize_signals(current),
        previous=summarize_signals(previous),
        **dict(
            paginate(
                signal_items(matched[offset : offset + limit], agent_names, user_names),
                total=len(matched),
                limit=limit,
                offset=offset,
            )
        ),
    )


def signal_items(
    facts: list[RunFact],
    agent_names: Mapping[str, str],
    user_names: Callable[[list[str]], Mapping[str, str]],
) -> list[SignalItem]:
    """一覧の行（会話の本人の表示名とシグナルを入れる）。"""
    people = sorted({fact.user_uuid for fact in facts if fact.user_uuid})
    names = user_names(people) if people else {}
    return [
        SignalItem(
            run_id=fact.run_id,
            thread_id=fact.thread_id,
            agent_id=fact.agent_id,
            agent_name=agent_names.get(fact.agent_id) or fact.agent_name,
            user_uuid=fact.user_uuid,
            display_name=names.get(fact.user_uuid, "") if fact.user_uuid else "",
            question=fact.question,
            answer=fact.answer or "",
            status=fact.status,
            signals=fact_signals(fact),
            answer_outcome=fact.answer_outcome,
            validation_result=fact.validation_result,
            tool_failures=fact.tool_failures,
            approvals_rejected=fact.approvals_rejected,
            data_tool_calls=fact.data_tool_calls,
            feedback=fact.feedback,
            admin_review=fact.admin_review,
            created_at=fact.created_at,
        )
        for fact in facts
    ]


__all__ = [
    "ANY_SIGNAL_CONDITION",
    "SIGNAL_CONDITIONS",
    "SIGNAL_PAGE_SIZE",
    "SIGNAL_PAGE_SIZE_MAX",
    "SIGNAL_SCOPE_CONDITION",
    "SignalCount",
    "SignalItem",
    "SignalKind",
    "SignalReport",
    "SignalSummary",
    "build_signal_report",
    "fact_signals",
    "finish_signal_summary",
    "in_signal_scope",
    "signal_items",
    "summarize_signals",
]
