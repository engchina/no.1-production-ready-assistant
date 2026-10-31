"""Run の事実の保存先（`AGENT_RUN_FACTS`）と、SQL での集計（#794）。

利用状況・フィードバックは、以前はプロセス内の Run（snapshot）をリクエストのたびに Python で
集計していた。Oracle の構成（`AGENT_RUNTIME_REPOSITORY_BACKEND=oracle_*`）では、Run の事実を
1 Run = 1 行で `AGENT_RUN_FACTS` に保存し、期間（7〜365 日）・日・業務 Agent・利用者・モデルの
集計とフィードバックの一覧のページングを SQL で行う。

- 書き込み: Runtime repository が Run の事実が変わる時点（作成・状態の変化・利用量・評価）で
  `record_run` を呼ぶ。ここでは事実を作ってキューに入れるだけで、バックグラウンドのスレッドが
  まとめて MERGE する（Run の実行を待たせない・止めない）。失敗はログに残して捨てる
  （次の起動の backfill で、Runtime repository にある Run の事実を書き直す）。
- 起動時: `backfill` が Runtime repository の Run の事実をすべて MERGE する
  （既存の Run の集計を消さない）。
- 日時は UTC の `TIMESTAMP`（タイムゾーンなし）で保存する。session のタイムゾーンに左右されず、
  日の区切りは `FROM_TZ(CREATED_AT, 'UTC') AT TIME ZONE (:timezone)`（利用者のタイムゾーン）。
- memory / file の構成は保存しない（`reporting_store()` が None。メモリの Run を集計する）。
- テーブルはシステムテーブル（`app.system_schema`）が作る。無い間は集計をメモリの Run に戻す。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from importlib import import_module
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pr_backend_core.api import offset_fetch_binds, offset_fetch_clause, paginate

from app.features.agent import storage_backend
from app.features.agent.feedback import (
    FeedbackItem,
    FeedbackReport,
    FeedbackSummary,
    build_feedback_report,
    feedback_items,
    finish_summary,
)
from app.features.agent.run_facts import RunFact, fact_from_run
from app.features.agent.run_signals import (
    ANY_SIGNAL_CONDITION,
    SIGNAL_CONDITIONS,
    SIGNAL_SCOPE_CONDITION,
    SignalKind,
    SignalReport,
    build_signal_report,
    finish_signal_summary,
    signal_items,
)
from app.features.agent.runtime import FeedbackRating, FeedbackReason, RunFeedback, RunState
from app.features.agent.timezones import timezone_name
from app.features.agent.usage import (
    AgentUsage,
    ModelUsage,
    UsageReport,
    UsageTotals,
    UserUsage,
    build_usage_report,
    empty_days,
    finish_usage_report,
    usage_period,
)
from app.oracle_connection import connect_platform_oracle

logger = logging.getLogger(__name__)

RUN_FACTS_TABLE = "AGENT_RUN_FACTS"
# 1 回の MERGE（executemany）の行数。
WRITE_BATCH_SIZE = 200
# Oracle の IN 句の上限（ORA-01795）。超えるときは OR でつなぐ。
_IN_LIST_LIMIT = 1000
# 列の長さ（DDL と合わせる。CHAR の長さ）。
_AGENT_NAME_MAX = 400
_MODEL_MAX = 256
_COMMENT_MAX = 1000
_OUTCOME_MAX = 32
_VALIDATION_MAX = 16
_ORACLE_BACKENDS = {"oracle", "oracle_checkpoint", "oracle_normalized"}

UserNames = Callable[[list[str]], Mapping[str, str]]


class RunFactsUnavailableError(RuntimeError):
    """事実のテーブルが無い（システムテーブルが未作成）。集計はメモリの Run に戻す。"""


class RunFactsStore(Protocol):
    def upsert(self, facts: Sequence[RunFact]) -> None: ...

    def usage_report(
        self,
        *,
        days: int,
        now: datetime,
        tz: tzinfo,
        agent_ids: frozenset[str] | None,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> UsageReport: ...

    def feedback_report(
        self,
        *,
        days: int,
        now: datetime,
        agent_ids: frozenset[str] | None,
        agent_id: str | None,
        rating: FeedbackRating | None,
        reason: FeedbackReason | None,
        offset: int,
        limit: int,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> FeedbackReport: ...

    def signal_report(
        self,
        *,
        days: int,
        now: datetime,
        agent_ids: frozenset[str] | None,
        agent_id: str | None,
        kind: SignalKind | None,
        offset: int,
        limit: int,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> SignalReport: ...


# ---- テスト・開発用の保存先 -------------------------------------------------------------


class MemoryRunFactsStore:
    """事実をプロセス内に持つ保存先（テストの偽物。集計は Python）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.facts: dict[str, RunFact] = {}

    def upsert(self, facts: Sequence[RunFact]) -> None:
        with self._lock:
            for fact in facts:
                self.facts[fact.run_id] = fact.model_copy(deep=True)

    def _scoped(self, agent_ids: frozenset[str] | None) -> list[RunFact]:
        with self._lock:
            return [
                fact
                for fact in self.facts.values()
                if agent_ids is None or fact.agent_id in agent_ids
            ]

    def usage_report(
        self,
        *,
        days: int,
        now: datetime,
        tz: tzinfo,
        agent_ids: frozenset[str] | None,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> UsageReport:
        return build_usage_report(
            self._scoped(agent_ids),
            days=days,
            now=now,
            tz=tz,
            agent_names=agent_names,
            user_names=user_names,
            source="history",
        )

    def feedback_report(
        self,
        *,
        days: int,
        now: datetime,
        agent_ids: frozenset[str] | None,
        agent_id: str | None,
        rating: FeedbackRating | None,
        reason: FeedbackReason | None,
        offset: int,
        limit: int,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> FeedbackReport:
        return build_feedback_report(
            self._scoped(agent_ids),
            days=days,
            now=now,
            agent_id=agent_id,
            rating=rating,
            reason=reason,
            agent_names=agent_names,
            user_names=user_names,
            offset=offset,
            limit=limit,
            source="history",
        )

    def signal_report(
        self,
        *,
        days: int,
        now: datetime,
        agent_ids: frozenset[str] | None,
        agent_id: str | None,
        kind: SignalKind | None,
        offset: int,
        limit: int,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> SignalReport:
        return build_signal_report(
            self._scoped(agent_ids),
            days=days,
            now=now,
            agent_id=agent_id,
            kind=kind,
            agent_names=agent_names,
            user_names=user_names,
            offset=offset,
            limit=limit,
            source="history",
        )


# ---- Oracle ----------------------------------------------------------------------------

# MERGE の列（bind の名前と列。UPDATED_AT は事実を書いた時刻で SQL が入れる）。
_MERGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "RUN_ID"),
    ("agent_id", "AGENT_ID"),
    ("agent_name", "AGENT_NAME"),
    ("agent_version", "AGENT_VERSION"),
    ("thread_id", "THREAD_ID"),
    ("user_uuid", "CREATED_BY_USER_UUID"),
    ("status", "STATUS"),
    ("run_source", "RUN_SOURCE"),
    ("model", "MODEL"),
    ("requests", "REQUESTS"),
    ("input_tokens", "INPUT_TOKENS"),
    ("output_tokens", "OUTPUT_TOKENS"),
    ("total_tokens", "TOTAL_TOKENS"),
    ("feedback_rating", "FEEDBACK_RATING"),
    ("feedback_reason", "FEEDBACK_REASON"),
    ("feedback_comment", "FEEDBACK_COMMENT"),
    ("feedback_user_uuid", "FEEDBACK_USER_UUID"),
    ("feedback_at", "FEEDBACK_AT"),
    ("admin_rating", "ADMIN_RATING"),
    ("admin_reason", "ADMIN_REASON"),
    ("admin_comment", "ADMIN_COMMENT"),
    ("admin_user_uuid", "ADMIN_USER_UUID"),
    ("admin_at", "ADMIN_AT"),
    ("rated_at", "RATED_AT"),
    ("question", "QUESTION"),
    ("answer", "ANSWER"),
    ("created_at", "CREATED_AT"),
    ("finished_at", "FINISHED_AT"),
)
# 確かめるべき実行のシグナルの列（#1822。migration 012 で足した）。
_SIGNAL_MERGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("answer_outcome", "ANSWER_OUTCOME"),
    ("validation_result", "VALIDATION_RESULT"),
    ("tool_failures", "TOOL_FAILURES"),
    ("approvals_rejected", "APPROVALS_REJECTED"),
)
# 業務データの道具の成功した呼び出しの数（#1834。migration 014 で足した）。
_DATA_TOOL_MERGE_COLUMNS: tuple[tuple[str, str], ...] = (("data_tool_calls", "DATA_TOOL_CALLS"),)
DATA_TOOL_BINDS = frozenset(name for name, _column in _DATA_TOOL_MERGE_COLUMNS)
SIGNAL_BINDS = frozenset(name for name, _column in _SIGNAL_MERGE_COLUMNS) | DATA_TOOL_BINDS


def merge_sql(columns: Sequence[tuple[str, str]]) -> str:
    """1 Run = 1 行の MERGE（列は固定の定数。UPDATED_AT は書いた時刻の UTC）。"""
    names = [column for _bind, column in columns]
    selected = ",\n        ".join(f":{bind} AS {column}" for bind, column in columns)
    updates = ",\n    ".join(
        f"target.{column} = source.{column}" for column in names if column != "RUN_ID"
    )
    return (
        f"MERGE INTO {RUN_FACTS_TABLE} target\n"  # nosec B608 - 固定のテーブル名と列
        f"USING (\n    SELECT\n        {selected}\n    FROM dual\n) source\n"
        "ON (target.RUN_ID = source.RUN_ID)\n"
        f"WHEN MATCHED THEN UPDATE SET\n    {updates},\n"
        "    target.UPDATED_AT = SYS_EXTRACT_UTC(SYSTIMESTAMP)\n"
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}, UPDATED_AT)\n"
        f"VALUES ({', '.join(f'source.{column}' for column in names)}, "
        "SYS_EXTRACT_UTC(SYSTIMESTAMP))"
    )


MERGE_SQL = merge_sql((*_MERGE_COLUMNS, *_SIGNAL_MERGE_COLUMNS, *_DATA_TOOL_MERGE_COLUMNS))
# システムテーブルを更新するまでの古いテーブルへの MERGE（ORA-00904 のときだけ、新しい方から
# 順に試す）。
# 更新するまでの間も、利用状況・フィードバックと、あるだけのシグナルの事実を書き続ける。
# migration 014 の前（業務データの道具の列が無い）。
MERGE_SQL_012 = merge_sql((*_MERGE_COLUMNS, *_SIGNAL_MERGE_COLUMNS))
# migration 012 の前（シグナルの列が無い）。
LEGACY_MERGE_SQL = merge_sql(_MERGE_COLUMNS)
# (SQL, 除く bind)。最新の形から古い形の順。
MERGE_FALLBACKS: tuple[tuple[str, frozenset[str]], ...] = (
    (MERGE_SQL, frozenset()),
    (MERGE_SQL_012, DATA_TOOL_BINDS),
    (LEGACY_MERGE_SQL, SIGNAL_BINDS),
)

# 利用量の集計の測度（Run 数・利用量を記録した Run 数・各量の合計）。
_USAGE_MEASURES = (
    "COUNT(*), COUNT(REQUESTS), NVL(SUM(REQUESTS), 0), NVL(SUM(INPUT_TOKENS), 0), "
    "NVL(SUM(OUTPUT_TOKENS), 0), NVL(SUM(TOTAL_TOKENS), 0)"
)
# 利用状況の切り口（列は固定。利用者の入力を SQL に入れない）。
USAGE_DIMENSIONS: dict[str, str] = {
    "agent": "AGENT_ID",
    "user": "CREATED_BY_USER_UUID",
    "model": "MODEL",
    "day": "RUN_DAY",
}
_FEEDBACK_ITEM_COLUMNS = (
    "RUN_ID, THREAD_ID, AGENT_ID, AGENT_NAME, CREATED_BY_USER_UUID, QUESTION, ANSWER, "
    "FEEDBACK_RATING, FEEDBACK_REASON, FEEDBACK_COMMENT, FEEDBACK_USER_UUID, FEEDBACK_AT, "
    "ADMIN_RATING, ADMIN_REASON, ADMIN_COMMENT, ADMIN_USER_UUID, ADMIN_AT, "
    "STATUS, CREATED_AT"
)


def to_utc_naive(value: datetime | None) -> datetime | None:
    """保存・比較に使う UTC の naive な日時（列は TIMESTAMP。session のタイムゾーンに依らない）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def from_utc_naive(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"unexpected timestamp: {type(value).__name__}")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clip(value: str | None, limit: int) -> str | None:
    if value is None or value == "":
        return None
    return value[:limit]


def fact_binds(fact: RunFact) -> dict[str, Any]:
    """MERGE の bind（列の長さに切り、日時は UTC の naive にする）。"""
    feedback = fact.feedback
    review = fact.admin_review
    return {
        "run_id": fact.run_id,
        "agent_id": fact.agent_id,
        "agent_name": _clip(fact.agent_name, _AGENT_NAME_MAX),
        "agent_version": _clip(fact.agent_version, 32),
        "thread_id": fact.thread_id,
        "user_uuid": fact.user_uuid,
        "status": fact.status,
        "run_source": fact.source,
        "model": _clip(fact.model, _MODEL_MAX),
        "requests": fact.requests,
        "input_tokens": fact.input_tokens,
        "output_tokens": fact.output_tokens,
        "total_tokens": fact.total_tokens,
        "feedback_rating": feedback.rating.value if feedback else None,
        "feedback_reason": feedback.reason.value if feedback and feedback.reason else None,
        "feedback_comment": _clip(feedback.comment, _COMMENT_MAX) if feedback else None,
        "feedback_user_uuid": feedback.user_uuid if feedback else None,
        "feedback_at": to_utc_naive(feedback.updated_at) if feedback else None,
        "admin_rating": review.rating.value if review else None,
        "admin_reason": review.reason.value if review and review.reason else None,
        "admin_comment": _clip(review.comment, _COMMENT_MAX) if review else None,
        "admin_user_uuid": review.user_uuid if review else None,
        "admin_at": to_utc_naive(review.updated_at) if review else None,
        "rated_at": to_utc_naive(fact.rated_at),
        "question": fact.question,
        "answer": fact.answer,
        "created_at": to_utc_naive(fact.created_at),
        "finished_at": to_utc_naive(fact.finished_at),
        "answer_outcome": _clip(fact.answer_outcome, _OUTCOME_MAX),
        "validation_result": _clip(fact.validation_result, _VALIDATION_MAX),
        "tool_failures": fact.tool_failures,
        "approvals_rejected": fact.approvals_rejected,
        "data_tool_calls": fact.data_tool_calls,
    }


def agent_scope_clause(agent_ids: frozenset[str] | None, binds: dict[str, Any]) -> str:
    """利用できる業務 Agent に絞る条件（None は制限なし。値は bind にする）。"""
    if agent_ids is None:
        return ""
    ids = sorted(agent_ids)
    groups: list[str] = []
    for start in range(0, len(ids), _IN_LIST_LIMIT):
        names: list[str] = []
        for index, value in enumerate(ids[start : start + _IN_LIST_LIMIT], start=start):
            binds[f"scope_{index}"] = value
            names.append(f":scope_{index}")
        groups.append(f"AGENT_ID IN ({', '.join(names)})")
    return " AND (" + " OR ".join(groups) + ")"


def usage_source_sql(scope: str, *, fixed_offset: bool = False) -> str:
    """利用状況の集計の元（期間と直前の期間の Run。日は利用者のタイムゾーンで区切る）。

    bind を含む式を GROUP BY に書くと ORA-00979 になるため、inline view の列にしてから集計する。
    `AT TIME ZONE` の直後に素の bind を書くと ORA-00907 になるため、bind を括弧で囲む（#1822 で
    実 Oracle の結合テストが見つけた。それまでは Oracle の構成でもメモリの Run を集計した）。
    """
    local_time = (
        "CREATED_AT + NUMTODSINTERVAL(:timezone_minutes, 'MINUTE')"
        if fixed_offset
        else "FROM_TZ(CREATED_AT, 'UTC') AT TIME ZONE (:timezone)"
    )
    return (
        "SELECT AGENT_ID, AGENT_NAME, CREATED_BY_USER_UUID, MODEL, REQUESTS, INPUT_TOKENS, "
        "OUTPUT_TOKENS, TOTAL_TOKENS, "
        "CASE WHEN CREATED_AT >= :since THEN 1 ELSE 0 END AS IN_PERIOD, "
        f"TO_CHAR({local_time}, 'YYYY-MM-DD') AS RUN_DAY "
        f"FROM {RUN_FACTS_TABLE} "  # nosec B608 - 固定のテーブル名と bind の条件
        f"WHERE CREATED_AT >= :previous_since AND CREATED_AT <= :until{scope}"
    )


def usage_totals_sql(scope: str, *, fixed_offset: bool = False) -> str:
    return (
        f"SELECT IN_PERIOD, {_USAGE_MEASURES} "  # nosec B608 - 固定の列
        f"FROM ({usage_source_sql(scope, fixed_offset=fixed_offset)}) GROUP BY IN_PERIOD"
    )


def usage_dimension_sql(dimension: str, scope: str, *, fixed_offset: bool = False) -> str:
    column = USAGE_DIMENSIONS[dimension]
    # モデル内訳だけは利用量記録済みの実行が対象。他の内訳は全実行数を維持する。
    usage_filter = " AND REQUESTS IS NOT NULL" if dimension == "model" else ""
    return (
        f"SELECT {column}, MAX(AGENT_NAME), {_USAGE_MEASURES} "  # nosec B608 - 固定の列
        f"FROM ({usage_source_sql(scope, fixed_offset=fixed_offset)}) "
        f"WHERE IN_PERIOD = 1{usage_filter} GROUP BY {column}"
    )


def feedback_filters(
    *,
    agent_ids: frozenset[str] | None,
    agent_id: str | None,
    binds: dict[str, Any],
) -> str:
    clause = agent_scope_clause(agent_ids, binds)
    if agent_id:
        binds["agent_id"] = agent_id
        clause += " AND AGENT_ID = :agent_id"
    return clause


def feedback_source_sql(filters: str) -> str:
    return (
        "SELECT FEEDBACK_RATING, FEEDBACK_REASON, ADMIN_RATING, "
        "CASE WHEN RATED_AT >= :since THEN 1 ELSE 0 END AS IN_PERIOD "
        f"FROM {RUN_FACTS_TABLE} "  # nosec B608 - 固定のテーブル名と bind の条件
        f"WHERE RATED_AT >= :previous_since AND RATED_AT <= :until{filters}"
    )


def feedback_summary_sql(filters: str) -> str:
    return (
        "SELECT IN_PERIOD, COUNT(FEEDBACK_RATING), "
        "SUM(CASE WHEN FEEDBACK_RATING = 'helpful' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN FEEDBACK_RATING = 'not_helpful' THEN 1 ELSE 0 END), "
        "COUNT(ADMIN_RATING), "
        "SUM(CASE WHEN ADMIN_RATING = 'not_helpful' THEN 1 ELSE 0 END), COUNT(*) "
        f"FROM ({feedback_source_sql(filters)}) GROUP BY IN_PERIOD"  # nosec B608
    )


def feedback_reasons_sql(filters: str) -> str:
    return (
        "SELECT IN_PERIOD, FEEDBACK_REASON, COUNT(*) "
        f"FROM ({feedback_source_sql(filters)}) "  # nosec B608 - 固定の列と bind の条件
        "WHERE FEEDBACK_RATING = 'not_helpful' AND FEEDBACK_REASON IS NOT NULL "
        "GROUP BY IN_PERIOD, FEEDBACK_REASON"
    )


def feedback_match_clause(
    rating: FeedbackRating | None, reason: FeedbackReason | None, binds: dict[str, Any]
) -> str:
    """評価・理由の絞り込み（本人の評価か管理者の評価のどちらかが合えばよい）。"""
    if rating is None and reason is None:
        return ""
    sides: list[str] = []
    for prefix in ("FEEDBACK", "ADMIN"):
        conditions = [f"{prefix}_RATING IS NOT NULL"]
        if rating is not None:
            conditions.append(f"{prefix}_RATING = :rating")
        if reason is not None:
            conditions.append(f"{prefix}_REASON = :reason")
        sides.append("(" + " AND ".join(conditions) + ")")
    if rating is not None:
        binds["rating"] = rating.value
    if reason is not None:
        binds["reason"] = reason.value
    return " AND (" + " OR ".join(sides) + ")"


def feedback_items_where(filters: str, match: str) -> str:
    return f"WHERE RATED_AT >= :since AND RATED_AT <= :until{filters}{match}"


def feedback_count_sql(where: str) -> str:
    return f"SELECT COUNT(*) FROM {RUN_FACTS_TABLE} {where}"  # nosec B608 - 固定の条件と bind


def feedback_page_sql(where: str) -> str:
    return (
        f"SELECT {_FEEDBACK_ITEM_COLUMNS} FROM {RUN_FACTS_TABLE} {where} "  # nosec B608
        "ORDER BY RATED_AT DESC, RUN_ID DESC "
        + offset_fetch_clause(offset_bind="page_offset", limit_bind="page_limit")
    )


# 確かめるべき実行（#1822）。対象は期間に作った、終わった Run（品質評価の Run を除く）。
_SIGNAL_SOURCE_COLUMNS = (
    "STATUS, FEEDBACK_RATING, ADMIN_RATING, VALIDATION_RESULT, ANSWER_OUTCOME, "
    "TOOL_FAILURES, APPROVALS_REJECTED, DATA_TOOL_CALLS"
)
_SIGNAL_ITEM_COLUMNS = (
    "RUN_ID, THREAD_ID, AGENT_ID, AGENT_NAME, CREATED_BY_USER_UUID, QUESTION, ANSWER, "
    "FEEDBACK_RATING, FEEDBACK_REASON, FEEDBACK_COMMENT, FEEDBACK_USER_UUID, FEEDBACK_AT, "
    "ADMIN_RATING, ADMIN_REASON, ADMIN_COMMENT, ADMIN_USER_UUID, ADMIN_AT, "
    "STATUS, CREATED_AT, ANSWER_OUTCOME, VALIDATION_RESULT, TOOL_FAILURES, APPROVALS_REJECTED, "
    "RUN_SOURCE, DATA_TOOL_CALLS"
)


def signal_source_sql(filters: str) -> str:
    """期間と直前の期間の対象の Run（集計の元）。"""
    return (
        f"SELECT {_SIGNAL_SOURCE_COLUMNS}, "  # nosec B608 - 固定の列とテーブル名と bind の条件
        "CASE WHEN CREATED_AT >= :since THEN 1 ELSE 0 END AS IN_PERIOD "
        f"FROM {RUN_FACTS_TABLE} "
        f"WHERE CREATED_AT >= :previous_since AND CREATED_AT <= :until "
        f"AND {SIGNAL_SCOPE_CONDITION}{filters}"
    )


def signal_summary_sql(filters: str) -> str:
    """期間ごとの対象の Run の数・シグナルのある Run の数・種類ごとの数（`SignalKind` の順）。"""
    measures = ", ".join(
        f"SUM(CASE WHEN {SIGNAL_CONDITIONS[kind]} THEN 1 ELSE 0 END)" for kind in SignalKind
    )
    return (
        "SELECT IN_PERIOD, COUNT(*), "  # nosec B608 - 固定の条件
        f"SUM(CASE WHEN {ANY_SIGNAL_CONDITION} THEN 1 ELSE 0 END), {measures} "
        f"FROM ({signal_source_sql(filters)}) GROUP BY IN_PERIOD"
    )


def signal_items_where(filters: str, kind: SignalKind | None) -> str:
    condition = SIGNAL_CONDITIONS[kind] if kind is not None else ANY_SIGNAL_CONDITION
    return (
        "WHERE CREATED_AT >= :since AND CREATED_AT <= :until "
        f"AND {SIGNAL_SCOPE_CONDITION}{filters} AND {condition}"
    )


def signal_page_sql(where: str) -> str:
    return (
        f"SELECT {_SIGNAL_ITEM_COLUMNS} FROM {RUN_FACTS_TABLE} {where} "  # nosec B608
        "ORDER BY CREATED_AT DESC, RUN_ID DESC "
        + offset_fetch_clause(offset_bind="page_offset", limit_bind="page_limit")
    )


def _int(value: Any) -> int:
    return int(value or 0)


def fact_from_signal_row(row: Sequence[Any]) -> RunFact:
    """`signal_page_sql` の 1 行（列の順は `_SIGNAL_ITEM_COLUMNS`）。"""
    fact = fact_from_feedback_row(row[:19])
    return fact.model_copy(
        update={
            "answer_outcome": str(row[19]) if row[19] else None,
            "validation_result": str(row[20]) if row[20] else None,
            "tool_failures": _int(row[21]),
            "approvals_rejected": _int(row[22]),
            "source": str(row[23] or fact.source),
            "data_tool_calls": None if row[24] is None else _int(row[24]),
        }
    )


def _lob_text(value: Any) -> str | None:
    if value is None:
        return None
    read = getattr(value, "read", None)
    return str(read()) if callable(read) else str(value)


def _totals(row: Sequence[Any], target: UsageTotals) -> None:
    target.runs = int(row[0] or 0)
    target.runs_with_usage = int(row[1] or 0)
    target.requests = int(row[2] or 0)
    target.input_tokens = int(row[3] or 0)
    target.output_tokens = int(row[4] or 0)
    target.total_tokens = int(row[5] or 0)


def _feedback(
    rating: Any, reason: Any, comment: Any, user_uuid: Any, at: Any
) -> RunFeedback | None:
    if rating is None:
        return None
    return RunFeedback(
        rating=FeedbackRating(str(rating)),
        reason=FeedbackReason(str(reason)) if reason else None,
        comment=str(comment or ""),
        user_uuid=str(user_uuid) if user_uuid else None,
        updated_at=from_utc_naive(at) or datetime.now(UTC),
    )


def fact_from_feedback_row(row: Sequence[Any]) -> RunFact:
    """`feedback_page_sql` の 1 行（列の順は `_FEEDBACK_ITEM_COLUMNS`）。"""
    return RunFact(
        run_id=str(row[0]),
        thread_id=str(row[1]) if row[1] else None,
        agent_id=str(row[2]),
        agent_name=str(row[3] or ""),
        user_uuid=str(row[4]) if row[4] else None,
        question=_lob_text(row[5]) or "",
        answer=_lob_text(row[6]),
        feedback=_feedback(row[7], row[8], row[9], row[10], row[11]),
        admin_review=_feedback(row[12], row[13], row[14], row[15], row[16]),
        status=str(row[17]),
        created_at=from_utc_naive(row[18]) or datetime.now(UTC),
    )


def _is_table_missing(exc: Exception) -> bool:
    text = str(exc)
    return "ORA-00942" in text or "table or view does not exist" in text


def _is_column_missing(exc: Exception) -> bool:
    """列が無い（ORA-00904。シグナルの列を足す migration 012 の前のテーブル）。"""
    text = str(exc)
    return "ORA-00904" in text or "invalid identifier" in text


def _merge_input_sizes() -> dict[str, Any]:
    """MERGE の bind の型（executemany は先頭の行の値で型を決めるため、None の列も型を固定する）。

    質問・回答は 4000 byte を超えうるため CLOB で bind する。
    """
    oracledb = import_module("oracledb")
    numbers = (
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tool_failures",
        "approvals_rejected",
        "data_tool_calls",
    )
    timestamps = ("feedback_at", "admin_at", "rated_at", "created_at", "finished_at")
    return {
        "question": oracledb.DB_TYPE_CLOB,
        "answer": oracledb.DB_TYPE_CLOB,
        **{name: oracledb.DB_TYPE_NUMBER for name in numbers},
        **{name: oracledb.DB_TYPE_TIMESTAMP for name in timestamps},
    }


class OracleRunFactsStore:
    """`AGENT_RUN_FACTS`（共通の `PLATFORM_ORACLE_*` の接続。テーブルはシステムテーブルが作る）。"""

    def __init__(self, connect_factory: Callable[[], Any] | None = None) -> None:
        self._connect = connect_factory or connect_platform_oracle

    def upsert(self, facts: Sequence[RunFact]) -> None:
        if not facts:
            return
        rows = [fact_binds(fact) for fact in facts]
        sizes = _merge_input_sizes()
        with self._connect() as connection:
            for index, (sql, excluded) in enumerate(MERGE_FALLBACKS):
                try:
                    with connection.cursor() as cursor:
                        cursor.setinputsizes(
                            **{key: value for key, value in sizes.items() if key not in excluded}
                        )
                        cursor.executemany(
                            sql,
                            [
                                {key: value for key, value in row.items() if key not in excluded}
                                for row in rows
                            ],
                        )
                    break
                except Exception as exc:
                    # システムテーブルの更新（migration 012 / 014）の前: 無い列を除いて書き直す。
                    if index == len(MERGE_FALLBACKS) - 1 or not _is_column_missing(exc):
                        raise
                    connection.rollback()
            connection.commit()

    def _query(self, statements: Sequence[tuple[str, dict[str, Any]]]) -> list[list[Any]]:
        """複数の SELECT を 1 つの接続で実行する（CLOB は接続を閉じる前に読む）。"""
        import oracledb

        results: list[list[Any]] = []
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                for sql, binds in statements:
                    cursor.setinputsizes(
                        **{
                            name: oracledb.DB_TYPE_TIMESTAMP
                            for name, value in binds.items()
                            if isinstance(value, datetime)
                        }
                    )
                    cursor.execute(sql, binds)
                    rows = cursor.fetchall()
                    results.append(
                        [tuple(_lob_text(v) if hasattr(v, "read") else v for v in r) for r in rows]
                    )
        except Exception as exc:
            if _is_table_missing(exc):
                raise RunFactsUnavailableError(RUN_FACTS_TABLE) from exc
            raise
        return results

    def usage_report(
        self,
        *,
        days: int,
        now: datetime,
        tz: tzinfo,
        agent_ids: frozenset[str] | None,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> UsageReport:
        first_day, since, previous_since = usage_period(days, now, tz)
        totals = UsageTotals()
        previous = UsageTotals()
        by_day = empty_days(first_day, days)
        by_agent: list[AgentUsage] = []
        by_user: list[UserUsage] = []
        by_model: list[ModelUsage] = []
        if agent_ids is None or agent_ids:
            binds: dict[str, Any] = {
                "since": to_utc_naive(since),
                "previous_since": to_utc_naive(previous_since),
                "until": to_utc_naive(now),
            }
            fixed_offset = not isinstance(tz, ZoneInfo)
            if fixed_offset:
                offset = now.astimezone(tz).utcoffset()
                if offset is None:
                    raise ValueError("タイムゾーンの時差を解決できません。")
                binds["timezone_minutes"] = int(offset.total_seconds() // 60)
            else:
                binds["timezone"] = timezone_name(tz)
            scope = agent_scope_clause(agent_ids, binds)
            dimensions = ("agent", "user", "model", "day")
            results = self._query(
                [(usage_totals_sql(scope, fixed_offset=fixed_offset), binds)]
                + [
                    (usage_dimension_sql(dimension, scope, fixed_offset=fixed_offset), binds)
                    for dimension in dimensions
                ]
            )
            for row in results[0]:
                _totals(row[1:], totals if int(row[0]) == 1 else previous)
            for row in results[1]:
                item = AgentUsage(
                    agent_id=str(row[0]),
                    agent_name=agent_names.get(str(row[0])) or str(row[1] or ""),
                )
                _totals(row[2:], item)
                by_agent.append(item)
            for row in results[2]:
                user = UserUsage(user_uuid=str(row[0]) if row[0] else None, display_name="")
                _totals(row[2:], user)
                by_user.append(user)
            for row in results[3]:
                model = ModelUsage(model=str(row[0] or ""))
                _totals(row[2:], model)
                by_model.append(model)
            for row in results[4]:
                day = date.fromisoformat(str(row[0]))
                if day in by_day:
                    _totals(row[2:], by_day[day])
        return finish_usage_report(
            days=days,
            source="history",
            tz=tz,
            since=since,
            now=now,
            totals=totals,
            previous=previous,
            by_agent=by_agent,
            by_user=by_user,
            by_model=by_model,
            by_day=by_day,
            user_names=user_names,
        )

    def feedback_report(
        self,
        *,
        days: int,
        now: datetime,
        agent_ids: frozenset[str] | None,
        agent_id: str | None,
        rating: FeedbackRating | None,
        reason: FeedbackReason | None,
        offset: int,
        limit: int,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> FeedbackReport:
        since = now - timedelta(days=days)
        previous_since = since - timedelta(days=days)
        summaries = {1: FeedbackSummary(), 0: FeedbackSummary()}
        items: list[FeedbackItem] = []
        total = 0
        if agent_ids is None or agent_ids:
            binds: dict[str, Any] = {
                "since": to_utc_naive(since),
                "previous_since": to_utc_naive(previous_since),
                "until": to_utc_naive(now),
            }
            filters = feedback_filters(agent_ids=agent_ids, agent_id=agent_id, binds=binds)
            page_binds = {key: value for key, value in binds.items() if key != "previous_since"}
            where = feedback_items_where(filters, feedback_match_clause(rating, reason, page_binds))
            count_binds = dict(page_binds)
            page_binds.update(
                offset_fetch_binds(
                    offset=offset, limit=limit, offset_bind="page_offset", limit_bind="page_limit"
                )
            )
            summary_rows, reason_rows, count_rows, page_rows = self._query(
                [
                    (feedback_summary_sql(filters), binds),
                    (feedback_reasons_sql(filters), binds),
                    (feedback_count_sql(where), count_binds),
                    (feedback_page_sql(where), page_binds),
                ]
            )
            reasons: dict[int, dict[FeedbackReason, int]] = {0: {}, 1: {}}
            for row in reason_rows:
                reasons[int(row[0])][FeedbackReason(str(row[1]))] = int(row[2] or 0)
            for row in summary_rows:
                period = int(row[0])
                summary = FeedbackSummary(
                    rated_runs=int(row[6] or 0),
                    total=int(row[1] or 0),
                    helpful=int(row[2] or 0),
                    not_helpful=int(row[3] or 0),
                    admin_reviewed=int(row[4] or 0),
                    admin_not_helpful=int(row[5] or 0),
                )
                summaries[period] = finish_summary(summary, reasons[period])
            total = int(count_rows[0][0] or 0) if count_rows else 0
            items = feedback_items(
                [fact_from_feedback_row(row) for row in page_rows], agent_names, user_names, now
            )
        return FeedbackReport(
            days=days,
            source="history",
            since=since,
            until=now,
            summary=summaries[1],
            previous=summaries[0],
            **dict(paginate(items, total=total, limit=limit, offset=offset)),
        )

    def signal_report(
        self,
        *,
        days: int,
        now: datetime,
        agent_ids: frozenset[str] | None,
        agent_id: str | None,
        kind: SignalKind | None,
        offset: int,
        limit: int,
        agent_names: Mapping[str, str],
        user_names: UserNames,
    ) -> SignalReport:
        since = now - timedelta(days=days)
        previous_since = since - timedelta(days=days)
        summaries = {1: finish_signal_summary(0, 0, {}), 0: finish_signal_summary(0, 0, {})}
        facts: list[RunFact] = []
        total = 0
        if agent_ids is None or agent_ids:
            binds: dict[str, Any] = {
                "since": to_utc_naive(since),
                "previous_since": to_utc_naive(previous_since),
                "until": to_utc_naive(now),
            }
            filters = feedback_filters(agent_ids=agent_ids, agent_id=agent_id, binds=binds)
            page_binds = {key: value for key, value in binds.items() if key != "previous_since"}
            where = signal_items_where(filters, kind)
            count_binds = dict(page_binds)
            page_binds.update(
                offset_fetch_binds(
                    offset=offset, limit=limit, offset_bind="page_offset", limit_bind="page_limit"
                )
            )
            summary_rows, count_rows, page_rows = self._query(
                [
                    (signal_summary_sql(filters), binds),
                    (feedback_count_sql(where), count_binds),
                    (signal_page_sql(where), page_binds),
                ]
            )
            for row in summary_rows:
                counts = {
                    signal: _int(value) for signal, value in zip(SignalKind, row[3:], strict=False)
                }
                summaries[int(row[0])] = finish_signal_summary(_int(row[1]), _int(row[2]), counts)
            total = _int(count_rows[0][0]) if count_rows else 0
            facts = [fact_from_signal_row(row) for row in page_rows]
        return SignalReport(
            days=days,
            source="history",
            since=since,
            until=now,
            kind=kind,
            summary=summaries[1],
            previous=summaries[0],
            **dict(
                paginate(
                    signal_items(facts, agent_names, user_names),
                    total=total,
                    limit=limit,
                    offset=offset,
                )
            ),
        )


# ---- バックグラウンドの書き込み ---------------------------------------------------------


class RunFactsWriter:
    """事実をキューに入れ、デーモンのスレッドがまとめて保存する（同じ Run は最後の事実だけ）。"""

    def __init__(self, store: RunFactsStore, *, batch_size: int = WRITE_BATCH_SIZE) -> None:
        self._store = store
        self._batch_size = max(1, batch_size)
        self._condition = threading.Condition()
        self._pending: dict[str, RunFact] = {}
        self._busy = False
        self._thread: threading.Thread | None = None

    def submit(self, facts: Iterable[RunFact]) -> None:
        with self._condition:
            for fact in facts:
                # 古い事実を後から書かないよう、入れ直して最後に回す。
                self._pending.pop(fact.run_id, None)
                self._pending[fact.run_id] = fact
            if not self._pending:
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="agent-run-facts-writer", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        """キューが空になるまで待つ（テスト・終了時）。間に合わなければ False。"""
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._pending and not self._busy, timeout=timeout
            )

    def _loop(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    self._condition.wait()
                keys = list(self._pending)[: self._batch_size]
                batch = [self._pending.pop(key) for key in keys]
                self._busy = True
            try:
                self._store.upsert(batch)
            except Exception as exc:  # noqa: BLE001 - 保存の失敗で Run を止めない（次の起動で書き直す）
                logger.warning(
                    "agent_run_facts_not_saved",
                    extra={
                        "count": len(batch),
                        "error_type": type(exc).__name__,
                        "table_missing": _is_table_missing(exc),
                    },
                )
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()


# ---- 構成 ------------------------------------------------------------------------------

_config_lock = threading.Lock()
_configured = False
_store: RunFactsStore | None = None
_writer: RunFactsWriter | None = None


def _default_store() -> RunFactsStore | None:
    # Run の保存先と同じ決定に従う（`auto` は起動時に 1 回だけ判定する。#839）。
    backend = storage_backend.resolved_backend()
    return OracleRunFactsStore() if backend in _ORACLE_BACKENDS else None


def configure(store: RunFactsStore | None) -> None:
    """保存先を差し替える（テスト）。None は保存しない（集計はメモリの Run）。"""
    global _configured, _store, _writer
    with _config_lock:
        _store = store
        _writer = RunFactsWriter(store) if store is not None else None
        _configured = True


def reset() -> None:
    """構成を設定（`AGENT_RUNTIME_REPOSITORY_BACKEND`）から決め直す（テスト）。"""
    global _configured, _store, _writer
    with _config_lock:
        _configured = False
        _store = None
        _writer = None


def _ensure_configured() -> RunFactsWriter | None:
    global _configured, _store, _writer
    if _configured:
        return _writer
    with _config_lock:
        if not _configured:
            _store = _default_store()
            _writer = RunFactsWriter(_store) if _store is not None else None
            _configured = True
        return _writer


def reporting_store() -> RunFactsStore | None:
    """集計に使う保存先（Oracle の構成だけ。None は呼び出し側がメモリの Run を集計する）。"""
    _ensure_configured()
    return _store


def record_run(run: RunState, *, agent_name: str = "") -> None:
    """Run の事実をキューに入れる（Runtime repository の lock の中から呼ぶ。I/O はしない）。"""
    writer = _ensure_configured()
    if writer is None:
        return
    writer.submit([fact_from_run(run, agent_name=agent_name)])


def record_runs(runs: Iterable[RunState], agent_names: Mapping[str, str]) -> int:
    """複数の Run の事実をキューに入れる（起動時の backfill・snapshot の置き換え）。"""
    writer = _ensure_configured()
    if writer is None:
        return 0
    facts = [fact_from_run(run, agent_name=agent_names.get(run.agent_id, "")) for run in runs]
    writer.submit(facts)
    return len(facts)


def backfill(repository: Any) -> int:
    """Runtime repository にある Run の事実をすべて保存する（起動時。今見えている Run を残す）。"""
    if _ensure_configured() is None:
        return 0
    agent_names = {agent.id: agent.name for agent in repository.list_agents()}
    count = record_runs(repository.list_runs(), agent_names)
    logger.info("agent_run_facts_backfill_queued", extra={"count": count})
    return count


def flush(timeout: float = 5.0) -> bool:
    writer = _writer
    return True if writer is None else writer.flush(timeout)


__all__ = [
    "LEGACY_MERGE_SQL",
    "MERGE_FALLBACKS",
    "MERGE_SQL",
    "MERGE_SQL_012",
    "RUN_FACTS_TABLE",
    "USAGE_DIMENSIONS",
    "MemoryRunFactsStore",
    "OracleRunFactsStore",
    "RunFactsStore",
    "RunFactsUnavailableError",
    "RunFactsWriter",
    "agent_scope_clause",
    "backfill",
    "configure",
    "fact_binds",
    "flush",
    "record_run",
    "record_runs",
    "reporting_store",
    "reset",
]
