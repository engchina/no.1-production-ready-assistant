"""Run の事実（`AGENT_RUN_FACTS`）の保存と、SQL での集計（#794）の決定論テスト。

Oracle には接続しない。保存先は `MemoryRunFactsStore`（偽物）と、SQL と bind を記録して
用意した行を返す偽の接続で確かめる。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import run_facts_store
from app.features.agent.run_facts import (
    RUN_SOURCE_EVALUATION,
    RUN_SOURCE_MCP,
    RUN_SOURCE_USER,
    RunFact,
    fact_from_run,
)
from app.features.agent.run_facts_store import (
    MERGE_SQL,
    MemoryRunFactsStore,
    OracleRunFactsStore,
    RunFactsUnavailableError,
    RunFactsWriter,
    agent_scope_clause,
    fact_binds,
)
from app.features.agent.runtime import (
    AgentRuntimeRepository,
    FeedbackRating,
    FeedbackReason,
    RunCreateRequest,
    RunFeedback,
    RunUsage,
)
from app.security.permissions import MENU_FEEDBACK, MENU_USAGE
from app.security.service import set_security_service
from app.settings import get_settings

TOKYO = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 東京の 10/2 12:00
ALICE = "aaaaaaaa-0000-0000-0000-000000000001"


def _fact(
    run_id: str,
    created_at: datetime,
    *,
    agent_id: str = "default",
    tokens: int | None = 100,
    rated_at: datetime | None = None,
) -> RunFact:
    return RunFact(
        run_id=run_id,
        agent_id=agent_id,
        agent_name="保存した名前",
        status="completed",
        user_uuid=ALICE,
        model="model-a" if tokens is not None else None,
        requests=1 if tokens is not None else None,
        input_tokens=tokens,
        output_tokens=0 if tokens is not None else None,
        total_tokens=tokens,
        feedback=(
            RunFeedback(rating=FeedbackRating.HELPFUL, user_uuid=ALICE, updated_at=rated_at)
            if rated_at is not None
            else None
        ),
        question=f"{run_id} の質問",
        answer=f"{run_id} の回答",
        created_at=created_at,
    )


# ---- 事実の取り出し ---------------------------------------------------------------------


def test_fact_is_taken_from_the_run_snapshot() -> None:
    repository = AgentRuntimeRepository()
    run = repository.create_builtin_run(
        RunCreateRequest(agent_id="default", goal="今月の売上は？", metadata={"source": "mcp"}),
        created_by_user_uuid=ALICE,
    )
    repository.record_builtin_usage(
        run.id,
        RunUsage(model="m", requests=2, input_tokens=10, output_tokens=5, total_tokens=15),
    )
    completed = repository.complete_builtin_run(run.id, "300 万円です。")

    fact = fact_from_run(completed, agent_name="資料・データ横断アシスタント")

    assert (fact.run_id, fact.agent_name, fact.agent_version) == (
        run.id,
        "資料・データ横断アシスタント",
        "1",
    )
    assert (fact.status, fact.source, fact.user_uuid) == ("completed", RUN_SOURCE_MCP, ALICE)
    assert (fact.model, fact.requests, fact.total_tokens) == ("m", 2, 15)
    assert (fact.question, fact.answer) == ("今月の売上は？", "300 万円です。")
    assert fact.finished_at is not None and fact.finished_at >= fact.created_at
    queued = fact_from_run(run)
    assert (queued.finished_at, queued.has_usage, queued.answer) == (None, False, None)
    evaluation = run.model_copy(update={"metadata": {"evaluation_job_id": "eval_1"}})
    assert fact_from_run(evaluation).source == RUN_SOURCE_EVALUATION
    assert fact_from_run(run.model_copy(update={"metadata": {}})).source == RUN_SOURCE_USER


def test_repository_queues_facts_when_they_change() -> None:
    """作成・利用量・完了・評価で事実を保存する（Oracle の構成だけ。ここでは偽の保存先）。"""
    store = MemoryRunFactsStore()
    run_facts_store.configure(store)
    repository = AgentRuntimeRepository()
    run = repository.create_builtin_run(
        RunCreateRequest(agent_id="default", goal="質問"), created_by_user_uuid=ALICE
    )
    assert run_facts_store.flush()
    assert store.facts[run.id].status == "queued"

    repository.record_builtin_usage(
        run.id, RunUsage(model="m", requests=1, input_tokens=3, output_tokens=4, total_tokens=7)
    )
    repository.complete_builtin_run(run.id, "回答")
    repository.set_run_feedback(
        run.id,
        RunFeedback(
            rating=FeedbackRating.NOT_HELPFUL, reason=FeedbackReason.INCOMPLETE, user_uuid=ALICE
        ),
    )
    assert run_facts_store.flush()

    fact = store.facts[run.id]
    assert (fact.status, fact.total_tokens, fact.answer) == ("completed", 7, "回答")
    assert fact.feedback is not None and fact.feedback.reason == FeedbackReason.INCOMPLETE
    assert fact.agent_name == "資料・データ横断アシスタント"

    cancelled = repository.create_builtin_run(RunCreateRequest(agent_id="default", goal="q"))
    repository.cancel_run(cancelled.id)
    assert run_facts_store.flush()
    assert store.facts[cancelled.id].status == "cancelled"


def test_memory_backend_does_not_queue_facts() -> None:
    run_facts_store.configure(None)
    assert run_facts_store.reporting_store() is None
    repository = AgentRuntimeRepository()
    repository.create_builtin_run(RunCreateRequest(agent_id="default", goal="q"))
    assert run_facts_store.backfill(repository) == 0


def test_store_follows_the_runtime_repository_backend(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_repository_backend", "oracle_checkpoint")
    run_facts_store.reset()
    assert isinstance(run_facts_store.reporting_store(), OracleRunFactsStore)
    monkeypatch.setattr(settings, "agent_runtime_repository_backend", "memory")
    run_facts_store.reset()
    assert run_facts_store.reporting_store() is None


def test_backfill_queues_every_run_in_the_repository() -> None:
    store = MemoryRunFactsStore()
    repository = AgentRuntimeRepository()
    first = repository.create_builtin_run(RunCreateRequest(agent_id="default", goal="1"))
    second = repository.create_builtin_run(RunCreateRequest(agent_id="default", goal="2"))
    run_facts_store.configure(store)

    assert run_facts_store.backfill(repository) == 2
    assert run_facts_store.flush()
    assert set(store.facts) == {first.id, second.id}


class _FailingStore(MemoryRunFactsStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def upsert(self, facts: Sequence[RunFact]) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("ORA-00942: table or view does not exist")
        super().upsert(facts)


def test_writer_logs_failures_and_keeps_going(caplog: pytest.LogCaptureFixture) -> None:
    store = _FailingStore()
    writer = RunFactsWriter(store, batch_size=1)
    caplog.set_level(logging.WARNING)

    writer.submit([_fact("lost", NOW)])
    assert writer.flush()
    writer.submit([_fact("saved", NOW), _fact("saved", NOW + timedelta(seconds=1))])
    assert writer.flush()

    assert "agent_run_facts_not_saved" in caplog.text
    assert set(store.facts) == {"saved"}
    # 同じ Run は最後の事実だけを書く。
    assert store.facts["saved"].created_at == NOW + timedelta(seconds=1)


# ---- API ---------------------------------------------------------------------------------


def test_usage_and_feedback_beyond_the_runs_in_memory_come_from_saved_facts() -> None:
    """メモリに無い古い Run も、保存した事実から 365 日まで集計する。"""
    store = MemoryRunFactsStore()
    now = datetime.now(UTC)
    store.upsert(
        [
            _fact(
                "old-794", now - timedelta(days=200), tokens=500, rated_at=now - timedelta(days=199)
            ),
            _fact("recent-794", now - timedelta(days=2), tokens=100),
        ]
    )
    run_facts_store.configure(store)

    usage = client.get("/api/usage", params={"days": 365}).json()["data"]
    assert (usage["source"], usage["totals"]["runs"], usage["totals"]["total_tokens"]) == (
        "history",
        2,
        600,
    )
    # 業務 Agent の名前は今の名前（無ければ保存した名前）。
    assert usage["by_agent"][0]["agent_name"] == "資料・データ横断アシスタント"
    assert client.get("/api/usage", params={"days": 90}).json()["data"]["totals"]["runs"] == 1

    feedback = client.get("/api/feedback", params={"days": 365}).json()["data"]
    assert (feedback["source"], feedback["total"]) == ("history", 1)
    assert feedback["items"][0]["question"] == "old-794 の質問"


def test_reports_fall_back_to_memory_until_the_table_exists() -> None:
    class _Missing(MemoryRunFactsStore):
        def usage_report(self, **_kwargs: Any) -> Any:
            raise RunFactsUnavailableError("AGENT_RUN_FACTS")

        def feedback_report(self, **_kwargs: Any) -> Any:
            raise RunFactsUnavailableError("AGENT_RUN_FACTS")

    run_facts_store.configure(_Missing())
    assert client.get("/api/usage").json()["data"]["source"] == "memory"
    assert client.get("/api/feedback").json()["data"]["source"] == "memory"


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_saved_facts_are_limited_to_allowed_agents(auth: ProductionAuth) -> None:
    store = MemoryRunFactsStore()
    now = datetime.now(UTC)
    store.upsert(
        [
            _fact("sales-794", now - timedelta(days=1), agent_id="sales-794", rated_at=now),
            _fact("hr-794", now - timedelta(days=1), agent_id="hr-794", rated_at=now),
        ]
    )
    run_facts_store.configure(store)
    auth.user_with_permissions("facts-794", [MENU_USAGE, MENU_FEEDBACK], agent_ids=["sales-794"])
    headers = login("facts-794")

    usage = client.get("/api/usage", headers=headers).json()["data"]
    feedback = client.get("/api/feedback", headers=headers).json()["data"]

    assert [item["agent_id"] for item in usage["by_agent"]] == ["sales-794"]
    assert [item["run_id"] for item in feedback["items"]] == ["sales-794"]


# ---- Oracle の SQL -----------------------------------------------------------------------


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def setinputsizes(self, **sizes: Any) -> None:
        self.connection.input_sizes.update(sizes)

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        self.connection.executed.append((sql, rows))

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        if self.connection.error is not None:
            raise self.connection.error
        self.connection.executed.append((sql, dict(binds)))
        self.rows = self.connection.answer(sql)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self, answers: dict[str, list[tuple[Any, ...]]] | None = None) -> None:
        self.answers = answers or {}
        self.executed: list[tuple[str, Any]] = []
        self.input_sizes: dict[str, Any] = {}
        self.commits = 0
        self.error: Exception | None = None

    def answer(self, sql: str) -> list[tuple[Any, ...]]:
        for marker, rows in self.answers.items():
            if marker in sql:
                return rows
        return []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1


def _no_names(_uuids: list[str]) -> dict[str, str]:
    return {}


def _oracle(connection: _Connection) -> OracleRunFactsStore:
    @contextmanager
    def connect() -> Iterator[_Connection]:
        yield connection

    return OracleRunFactsStore(connect)


def test_upsert_merges_facts_with_utc_timestamps_and_clob_text() -> None:
    connection = _Connection()
    rated = datetime(2026, 10, 2, 12, 0, tzinfo=TOKYO)
    fact = _fact("r1", NOW, rated_at=rated).model_copy(update={"agent_name": "長" * 500})

    _oracle(connection).upsert([fact])

    [(sql, rows)] = connection.executed
    assert sql == MERGE_SQL and "MERGE INTO AGENT_RUN_FACTS" in sql
    row = rows[0]
    # 日時は UTC の naive（TIMESTAMP 列）。東京の 12:00 は UTC の 03:00。
    assert row["created_at"] == datetime(2026, 10, 2, 3, 0)
    assert row["feedback_at"] == row["rated_at"] == datetime(2026, 10, 2, 3, 0)
    assert (row["feedback_rating"], row["admin_rating"], row["question"]) == (
        "helpful",
        None,
        "r1 の質問",
    )
    assert len(row["agent_name"]) == 400
    # CLOB と、None になりうる数値・日時の列の型を固定する（executemany は先頭の行で型を決める）。
    assert {"question", "answer", "total_tokens", "finished_at"} <= set(connection.input_sizes)
    assert set(connection.input_sizes) <= set(fact_binds(fact))
    assert connection.commits == 1
    # MERGE の bind はすべて SQL にある（余分な bind は Oracle がエラーにする）。
    assert all(f":{name}" in MERGE_SQL for name in fact_binds(fact))


def test_agent_scope_uses_binds_and_splits_long_lists() -> None:
    binds: dict[str, Any] = {}
    assert agent_scope_clause(None, binds) == ""
    clause = agent_scope_clause(frozenset(f"a{index:04d}" for index in range(1001)), binds)
    assert clause.count("AGENT_ID IN (") == 2 and " OR " in clause
    assert len(binds) == 1001 and "a0000" not in clause


def test_usage_report_is_aggregated_in_sql() -> None:
    connection = _Connection(
        {
            "GROUP BY IN_PERIOD": [(1, 3, 2, 2, 400, 120, 520), (0, 1, 1, 1, 50, 10, 60)],
            "GROUP BY AGENT_ID": [("sales", "営業", 1, 1, 1, 300, 100, 400)],
            "GROUP BY CREATED_BY_USER_UUID": [
                (ALICE, None, 2, 1, 1, 100, 20, 120),
                (None, None, 1, 1, 1, 0, 0, 0),
            ],
            "GROUP BY MODEL": [
                ("model-a", None, 1, 1, 1, 100, 20, 120),
                (None, None, 1, 1, 1, 300, 100, 400),
            ],
            "GROUP BY RUN_DAY": [
                ("2026-10-02", None, 2, 2, 2, 400, 120, 520),
                ("2025-01-01", None, 1, 0, 0, 0, 0, 0),
            ],
        }
    )

    report = _oracle(connection).usage_report(
        days=7,
        now=NOW,
        tz=TOKYO,
        agent_ids=frozenset({"sales", "default"}),
        agent_names={},
        user_names=lambda uuids: {uuid: "Alice" for uuid in uuids},
    )

    assert report.source == "history"
    assert (report.totals.runs, report.totals.total_tokens, report.previous.total_tokens) == (
        3,
        520,
        60,
    )
    assert [(item.agent_id, item.agent_name) for item in report.by_agent] == [("sales", "営業")]
    assert [(item.user_uuid, item.display_name) for item in report.by_user] == [
        (ALICE, "Alice"),
        (None, ""),
    ]
    # モデル名だけが無い記録の token は保持し、利用量未記録とは区別する。
    assert [(item.model, item.total_tokens) for item in report.by_model] == [
        ("", 400),
        ("model-a", 120),
    ]
    assert len(report.by_day) == 7
    assert {item.day: item.runs for item in report.by_day}[date(2026, 10, 2)] == 2
    sql, binds = connection.executed[0]
    # 日は利用者のタイムゾーンで区切り、期間は東京の 9/26 0 時（UTC の 9/25 15:00）から。
    assert "FROM_TZ(CREATED_AT, 'UTC') AT TIME ZONE (:timezone)" in sql
    assert binds["timezone"] == "Asia/Tokyo"
    assert binds["since"] == datetime(2026, 9, 25, 15, 0)
    assert binds["previous_since"] == datetime(2026, 9, 18, 15, 0)
    assert binds["until"] == datetime(2026, 10, 2, 3, 0)
    assert {binds["scope_0"], binds["scope_1"]} == {"sales", "default"}
    assert len(connection.executed) == 5
    model_sql = next(sql for sql, _ in connection.executed if "GROUP BY MODEL" in sql)
    assert "WHERE IN_PERIOD = 1 AND REQUESTS IS NOT NULL GROUP BY MODEL" in model_sql
    for statement, _ in connection.executed:
        if statement != model_sql:
            assert "AND REQUESTS IS NOT NULL" not in statement


def test_usage_report_without_allowed_agents_does_not_query() -> None:
    connection = _Connection()
    report = _oracle(connection).usage_report(
        days=30, now=NOW, tz=TOKYO, agent_ids=frozenset(), agent_names={}, user_names=_no_names
    )
    assert (report.totals.runs, len(report.by_day), connection.executed) == (0, 30, [])


def test_feedback_report_is_aggregated_and_paged_in_sql() -> None:
    import oracledb

    rated = datetime(2026, 10, 1, 0, 0)
    connection = _Connection(
        {
            "GROUP BY IN_PERIOD, FEEDBACK_REASON": [
                (1, "incorrect", 2),
                (1, "incomplete", 3),
                (0, "incorrect", 1),
            ],
            "GROUP BY IN_PERIOD": [(1, 10, 5, 5, 2, 1, 11), (0, 4, 3, 1, 0, 0, 4)],
            "SELECT COUNT(*)": [(42,)],
            "OFFSET :page_offset": [
                (
                    "r1",
                    "t1",
                    "sales",
                    "営業",
                    ALICE,
                    "質問",
                    "回答",
                    "not_helpful",
                    "incorrect",
                    "違う",
                    ALICE,
                    rated,
                    "helpful",
                    None,
                    "",
                    "admin-uuid",
                    rated,
                    "completed",
                    rated,
                )
            ],
        }
    )

    report = _oracle(connection).feedback_report(
        days=30,
        now=NOW,
        agent_ids=None,
        agent_id="sales",
        rating=FeedbackRating.NOT_HELPFUL,
        reason=None,
        offset=20,
        limit=10,
        agent_names={"sales": "営業の Agent"},
        user_names=lambda uuids: {ALICE: "Alice", "admin-uuid": "管理者"},
    )

    assert (report.summary.total, report.summary.helpful_rate, report.previous.total) == (
        10,
        0.5,
        4,
    )
    assert [(item.reason, item.count) for item in report.summary.reason_counts] == [
        (FeedbackReason.INCOMPLETE, 3),
        (FeedbackReason.INCORRECT, 2),
    ]
    assert (report.summary.admin_reviewed, report.summary.admin_not_helpful) == (2, 1)
    assert (report.summary.rated_runs, report.previous.rated_runs) == (11, 4)
    assert connection.input_sizes["until"] is oracledb.DB_TYPE_TIMESTAMP
    assert connection.input_sizes["since"] is oracledb.DB_TYPE_TIMESTAMP
    assert (report.total, report.offset, report.limit, report.has_next) == (42, 20, 10, True)
    [item] = report.items
    assert (item.agent_name, item.display_name, item.reviewer_display_name) == (
        "営業の Agent",
        "Alice",
        "管理者",
    )
    assert item.feedback is not None and item.feedback.reason == FeedbackReason.INCORRECT
    assert item.feedback.updated_at == rated.replace(tzinfo=UTC)
    assert item.admin_review is not None and item.admin_review.rating == FeedbackRating.HELPFUL
    executed = dict(connection.executed)
    page_sql = next(sql for sql in executed if "OFFSET :page_offset" in sql)
    page_binds = executed[page_sql]
    assert (page_binds["page_offset"], page_binds["page_limit"], page_binds["agent_id"]) == (
        20,
        10,
        "sales",
    )
    # 評価の絞り込みは一覧と件数だけ（集計は期間と業務 Agent で数える）。
    assert page_binds["rating"] == "not_helpful" and "previous_since" not in page_binds
    assert "ORDER BY RATED_AT DESC, RUN_ID DESC" in page_sql
    summary_sql = next(sql for sql in executed if "COUNT(FEEDBACK_RATING)" in sql)
    assert "rating" not in executed[summary_sql]


def test_missing_table_is_reported_as_unavailable() -> None:
    connection = _Connection()
    connection.error = RuntimeError("ORA-00942: table or view does not exist")
    with pytest.raises(RunFactsUnavailableError):
        _oracle(connection).usage_report(
            days=7, now=NOW, tz=TOKYO, agent_ids=None, agent_names={}, user_names=_no_names
        )


def test_reports_fall_back_to_memory_when_the_database_fails() -> None:
    connection = _Connection()
    connection.error = RuntimeError("DPY-6005: cannot connect to database")
    run_facts_store.configure(_oracle(connection))

    response = client.get("/api/usage")

    assert response.status_code == 200
    assert response.json()["data"]["source"] == "memory"
