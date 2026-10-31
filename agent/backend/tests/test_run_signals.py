"""確かめるべき実行（暗黙のシグナル。#1822）の決定論テスト。

Oracle には接続しない。シグナルの SQL の条件は、Python の規則（`fact_signals`）と同じ結果に
なることを SQLite で確かめ、集計・一覧の SQL は SQL と bind を記録する偽の接続で確かめる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import run_facts_store
from app.features.agent.answer_validation import (
    ANSWER_VALIDATION_KIND,
    ANSWER_VALIDATION_NAME,
    validation_content,
)
from app.features.agent.run_facts import (
    RUN_SOURCE_EVALUATION,
    VALIDATION_FAILED,
    VALIDATION_NO_EVIDENCE_TOOLS,
    VALIDATION_PASSED,
    VALIDATION_SKIPPED,
    VALIDATION_UNGROUNDED,
    VALIDATION_UNVALIDATED,
    RunFact,
    fact_from_run,
)
from app.features.agent.run_facts_store import (
    LEGACY_MERGE_SQL,
    MERGE_SQL,
    MERGE_SQL_012,
    SIGNAL_BINDS,
    MemoryRunFactsStore,
    OracleRunFactsStore,
    fact_binds,
)
from app.features.agent.run_signals import (
    ANY_SIGNAL_CONDITION,
    SIGNAL_CONDITIONS,
    SIGNAL_SCOPE_CONDITION,
    SignalKind,
    build_signal_report,
    fact_signals,
    in_signal_scope,
)
from app.features.agent.runtime import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    FeedbackRating,
    FeedbackReason,
    RunFeedback,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
)
from app.features.agent.tools import ToolCall, ToolResult
from app.security.permissions import MENU_FEEDBACK
from app.security.service import set_security_service

NOW = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)
ALICE = "aaaaaaaa-0000-0000-0000-000000000001"


def _validation(status: str, **content: Any) -> Artifact:
    body = validation_content(status, reason=content.pop("reason", None))
    body.update(content)
    return Artifact(name=ANSWER_VALIDATION_NAME, kind=ANSWER_VALIDATION_KIND, content=body)


def _answer(outcome: str | None) -> Artifact:
    content: dict[str, Any] = {"text": "回答"}
    if outcome is not None:
        content["outcome"] = {"value": outcome, "basis": "answer_passages", "signals": []}
    return Artifact(name="回答", kind="answer", content=content)


def _fact(
    run_id: str,
    created_at: datetime = NOW - timedelta(hours=1),
    *,
    status: str = "completed",
    agent_id: str = "default",
    source: str = "user",
    feedback: FeedbackRating | None = None,
    admin: FeedbackRating | None = None,
    outcome: str | None = None,
    validation: str | None = None,
    tool_failures: int = 0,
    approvals_rejected: int = 0,
    data_tool_calls: int | None = 0,
) -> RunFact:
    def rating(value: FeedbackRating | None) -> RunFeedback | None:
        if value is None:
            return None
        reason = FeedbackReason.INCORRECT if value == FeedbackRating.NOT_HELPFUL else None
        return RunFeedback(rating=value, reason=reason, user_uuid=ALICE, updated_at=created_at)

    return RunFact(
        run_id=run_id,
        agent_id=agent_id,
        agent_name="保存した名前",
        status=status,
        source=source,
        user_uuid=ALICE,
        feedback=rating(feedback),
        admin_review=rating(admin),
        answer_outcome=outcome,
        validation_result=validation,
        tool_failures=tool_failures,
        approvals_rejected=approvals_rejected,
        data_tool_calls=data_tool_calls,
        question=f"{run_id} の質問",
        answer=f"{run_id} の回答",
        created_at=created_at,
    )


# ---- Run から事実を取り出す ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        (None, None),
        (
            _validation("completed", valid=True, withheld={"removed": 0, "unverified": 0}),
            VALIDATION_PASSED,
        ),
        (_validation("completed", withheld={"removed": 2, "unverified": 0}), VALIDATION_FAILED),
        (
            _validation(
                "completed",
                withheld={"removed": 0, "unverified": 0},
                answer_warnings=[{"code": "requests", "label": "要求", "detail": ""}],
            ),
            VALIDATION_FAILED,
        ),
        # 警告の印（規則の検査・続く手順）だけなら合格（#2738）。
        (
            _validation("completed", withheld={"removed": 0, "unverified": 0, "warnings": 2}),
            VALIDATION_PASSED,
        ),
        (_validation("completed", withheld={"removed": 0, "unverified": 1}), VALIDATION_FAILED),
        (
            _validation(
                "completed",
                withheld={"removed": 0, "unverified": 0, "all": False},
                result={"stale_evidence": [{"document_id": "d1"}]},
            ),
            VALIDATION_FAILED,
        ),
        (_validation("unvalidated", reason="validator_unavailable"), VALIDATION_UNVALIDATED),
        (_validation("unvalidated", reason="no_rag_evidence"), VALIDATION_UNGROUNDED),
        (_validation("skipped", reason="clarification_only"), VALIDATION_SKIPPED),
        # 資料に無い・答えられないことだけを述べる拒答（#1879。根拠を使わずに答えたに数えない）。
        (_validation("skipped", reason="absence_only"), VALIDATION_SKIPPED),
        # RAG の根拠のツールを持たない Agent の根拠の無い回答（#1834）。
        (_validation("skipped", reason="no_rag_evidence"), VALIDATION_NO_EVIDENCE_TOOLS),
    ],
    ids=[
        "none",
        "passed",
        "removed",
        "answer-warnings",
        "rule-warnings-only",
        "unverified",
        "stale-evidence",
        "validator-failed",
        "no-rag-evidence",
        "skipped",
        "absence-only",
        "no-evidence-tools",
    ],
)
def test_validation_result_is_taken_from_the_latest_validation(
    artifact: Artifact | None, expected: str | None
) -> None:
    artifacts = [_answer("answered")]
    if artifact is not None:
        # 古い検証の記録より、最も新しい記録を使う。
        artifacts = [_validation("unvalidated", reason="validation_error"), artifact, *artifacts]
    run = RunState(
        id="r1", goal="質問", agent_id="default", status=RunStatus.COMPLETED, artifacts=artifacts
    )
    assert fact_from_run(run).validation_result == expected


def test_fact_counts_failed_tools_rejected_approvals_and_keeps_the_outcome() -> None:
    call = ToolCall(name="nl2sql__nl2sql_query", arguments={})
    run = RunState(
        id="r1",
        goal="質問",
        agent_id="default",
        status=RunStatus.COMPLETED,
        steps=[
            RunStep(run_id="r1", status=StepStatus.FAILED, tool_call=call),
            RunStep(run_id="r1", status=StepStatus.FAILED, tool_call=call),
            RunStep(run_id="r1", status=StepStatus.COMPLETED, tool_call=call),
        ],
        approvals=[
            ApprovalRequest(
                run_id="r1",
                step_id="s1",
                tool_call=call,
                status=ApprovalStatus.REJECTED,
                reason="書き込み",
            ),
            # 終了で無効にした承認は却下に数えない。
            ApprovalRequest(
                run_id="r1",
                step_id="s2",
                tool_call=call,
                status=ApprovalStatus.CANCELLED,
                reason="書き込み",
            ),
        ],
        artifacts=[_answer("insufficient_evidence")],
    )

    fact = fact_from_run(run)

    assert (fact.tool_failures, fact.approvals_rejected, fact.answer_outcome) == (
        2,
        1,
        "insufficient_evidence",
    )
    assert fact_signals(fact) == [
        SignalKind.UNANSWERED,
        SignalKind.APPROVAL_REJECTED,
        SignalKind.TOOL_FAILED,
    ]
    # 失敗した呼び出しと結果の無い呼び出しは、業務データの道具を使ったことにしない。
    assert fact.data_tool_calls == 0
    # 回答の対応の無い（記録していない）Run は None。
    bare = run.model_copy(update={"artifacts": [_answer(None)], "steps": [], "approvals": []})
    assert fact_from_run(bare).answer_outcome is None
    assert fact_signals(fact_from_run(bare)) == []


def _step(name: str, *, success: bool = True, trace_id: str | None = None) -> RunStep:
    return RunStep(
        run_id="r1",
        status=StepStatus.COMPLETED if success else StepStatus.FAILED,
        tool_call=ToolCall(name=name, arguments={}, trace_id=trace_id),
        tool_result=ToolResult(name=name, success=success, output={}),
    )


@pytest.mark.parametrize(
    ("steps", "data_tool_calls", "flagged"),
    [
        ([], 0, True),
        # Control Plane のツール・RAG のツールは業務データの道具ではない。
        ([_step("skill_reference_read"), _step("rag__rag_lookup_guides")], 0, True),
        # 業務データの道具（NL2SQL など RAG 以外の MCP 接続のツール）で答えた。
        ([_step("nl2sql__nl2sql_query"), _step("crm__customer_lookup")], 2, False),
        # 失敗した呼び出しと、確認の取り消しで Control Plane が閉じた SQL の生成前の確認は数えない。
        (
            [
                _step("nl2sql__nl2sql_query", success=False),
                _step("nl2sql__nl2sql_cancel_requirement", trace_id="clarification_cancel_x"),
            ],
            0,
            True,
        ),
    ],
    ids=["no-tools", "control-plane-and-rag", "data-tools", "failed-and-cancelled"],
)
def test_ungrounded_answers_without_data_tools_are_flagged(
    steps: list[RunStep], data_tool_calls: int, flagged: bool
) -> None:
    """資料の根拠も業務データの道具も使わずに答えた回答だけを確かめる（#1834）。"""
    run = RunState(
        id="r1",
        goal="質問",
        agent_id="default",
        status=RunStatus.COMPLETED,
        steps=steps,
        artifacts=[_validation("unvalidated", reason="no_rag_evidence"), _answer("answered")],
    )

    fact = fact_from_run(run)

    assert (fact.validation_result, fact.data_tool_calls) == (
        VALIDATION_UNGROUNDED,
        data_tool_calls,
    )
    assert (SignalKind.UNGROUNDED_NO_TOOLS in fact_signals(fact)) is flagged


# ---- シグナルの規則（Python と SQL が同じ） -----------------------------------------------

_PARITY_FACTS = [
    _fact("plain"),
    _fact("owner-bad", feedback=FeedbackRating.NOT_HELPFUL),
    _fact("owner-good", feedback=FeedbackRating.HELPFUL),
    _fact("admin-bad", feedback=FeedbackRating.HELPFUL, admin=FeedbackRating.NOT_HELPFUL),
    _fact("validation-failed", validation=VALIDATION_FAILED),
    _fact("validation-passed", validation=VALIDATION_PASSED),
    _fact("unvalidated", validation=VALIDATION_UNVALIDATED),
    _fact("ungrounded", validation=VALIDATION_UNGROUNDED),
    _fact("ungrounded-data", validation=VALIDATION_UNGROUNDED, data_tool_calls=2),
    _fact("ungrounded-unknown", validation=VALIDATION_UNGROUNDED, data_tool_calls=None),
    _fact("passed-no-tools", validation=VALIDATION_PASSED),
    _fact("no-evidence-tools", validation=VALIDATION_NO_EVIDENCE_TOOLS),
    _fact("no-evidence-tools-data", validation=VALIDATION_NO_EVIDENCE_TOOLS, data_tool_calls=1),
    # ツールを呼ばずに答えた回答を拒答に置き換えた Run（#1835）は「答えられなかった」だけに数える。
    _fact("refused", validation=VALIDATION_UNGROUNDED, outcome="insufficient_evidence"),
    _fact("ungrounded-conditional", validation=VALIDATION_UNGROUNDED, outcome="conditional"),
    _fact("insufficient", outcome="insufficient_evidence"),
    _fact("needs-human", outcome="needs_human"),
    _fact("conditional", outcome="conditional"),
    _fact("clarification", outcome="needs_clarification"),
    _fact("rejected", approvals_rejected=1),
    _fact("tool-failed", tool_failures=3),
    _fact("failed", status="failed"),
    _fact("cancelled", status="cancelled"),
    _fact("running", status="running", tool_failures=1),
    _fact("evaluation", source=RUN_SOURCE_EVALUATION, status="failed"),
    _fact(
        "many",
        status="failed",
        feedback=FeedbackRating.NOT_HELPFUL,
        validation=VALIDATION_FAILED,
        tool_failures=1,
        approvals_rejected=2,
        outcome="needs_human",
    ),
]


def test_signals_follow_the_definitions() -> None:
    signals = {fact.run_id: fact_signals(fact) for fact in _PARITY_FACTS}
    assert signals["plain"] == [] and signals["owner-good"] == []
    assert signals["owner-bad"] == signals["admin-bad"] == [SignalKind.NOT_HELPFUL]
    assert signals["validation-failed"] == [SignalKind.VALIDATION_FAILED]
    assert signals["validation-passed"] == []
    assert signals["unvalidated"] == [SignalKind.UNVALIDATED]
    # 資料の根拠も業務データの道具も使わずに答えた回答だけをシグナルにする（#1834）。業務データの
    # 道具で答えた回答・記録の無い古い事実・検証を通った回答は数えない。
    assert signals["ungrounded"] == [SignalKind.UNGROUNDED_NO_TOOLS]
    assert signals["ungrounded-data"] == signals["ungrounded-unknown"] == []
    assert signals["passed-no-tools"] == []
    # RAG の根拠のツールを持たない Agent が、どの道具も使わずに答えた回答も数える。
    assert signals["no-evidence-tools"] == [SignalKind.UNGROUNDED_NO_TOOLS]
    assert signals["no-evidence-tools-data"] == []
    assert signals["refused"] == [SignalKind.UNANSWERED]
    assert signals["ungrounded-conditional"] == [SignalKind.UNGROUNDED_NO_TOOLS]
    assert signals["insufficient"] == signals["needs-human"] == [SignalKind.UNANSWERED]
    assert signals["conditional"] == signals["clarification"] == []
    assert signals["rejected"] == [SignalKind.APPROVAL_REJECTED]
    assert signals["tool-failed"] == [SignalKind.TOOL_FAILED]
    assert signals["failed"] == signals["cancelled"] == [SignalKind.RUN_FAILED]
    assert signals["many"] == [
        SignalKind.NOT_HELPFUL,
        SignalKind.VALIDATION_FAILED,
        SignalKind.UNANSWERED,
        SignalKind.APPROVAL_REJECTED,
        SignalKind.TOOL_FAILED,
        SignalKind.RUN_FAILED,
    ]
    # 終わっていない Run と品質評価の Run は対象にしない。
    scoped = {fact.run_id for fact in _PARITY_FACTS if in_signal_scope(fact)}
    assert {"running", "evaluation"}.isdisjoint(scoped)


def test_validation_results_fit_the_column() -> None:
    """最終の検証の結果の値は列（VARCHAR2(16)）に収まる（切ると SQL の条件に合わなくなる）。"""
    values = [
        VALIDATION_PASSED,
        VALIDATION_FAILED,
        VALIDATION_UNVALIDATED,
        VALIDATION_UNGROUNDED,
        VALIDATION_NO_EVIDENCE_TOOLS,
        VALIDATION_SKIPPED,
    ]
    assert all(len(value) <= 16 for value in values)


def _sqlite_with(facts: list[RunFact]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    columns = (
        "RUN_ID, STATUS, RUN_SOURCE, FEEDBACK_RATING, ADMIN_RATING, VALIDATION_RESULT, "
        "ANSWER_OUTCOME, TOOL_FAILURES, APPROVALS_REJECTED, DATA_TOOL_CALLS"
    )
    connection.execute(f"CREATE TABLE AGENT_RUN_FACTS ({columns})")
    for fact in facts:
        binds = fact_binds(fact)
        connection.execute(
            f"INSERT INTO AGENT_RUN_FACTS ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binds["run_id"],
                binds["status"],
                binds["run_source"],
                binds["feedback_rating"],
                binds["admin_rating"],
                binds["validation_result"],
                binds["answer_outcome"],
                binds["tool_failures"],
                binds["approvals_rejected"],
                binds["data_tool_calls"],
            ),
        )
    return connection


def _matching(connection: sqlite3.Connection, condition: str) -> set[str]:
    rows = connection.execute(f"SELECT RUN_ID FROM AGENT_RUN_FACTS WHERE {condition}")  # noqa: S608
    return {str(row[0]) for row in rows}


@pytest.mark.parametrize("kind", list(SignalKind), ids=[kind.value for kind in SignalKind])
def test_sql_conditions_match_the_python_rules(kind: SignalKind) -> None:
    connection = _sqlite_with(_PARITY_FACTS)
    expected = {fact.run_id for fact in _PARITY_FACTS if kind in fact_signals(fact)}
    assert _matching(connection, SIGNAL_CONDITIONS[kind]) == expected


def test_sql_scope_and_any_signal_match_the_python_rules() -> None:
    connection = _sqlite_with(_PARITY_FACTS)
    assert _matching(connection, SIGNAL_SCOPE_CONDITION) == {
        fact.run_id for fact in _PARITY_FACTS if in_signal_scope(fact)
    }
    assert _matching(connection, ANY_SIGNAL_CONDITION) == {
        fact.run_id for fact in _PARITY_FACTS if fact_signals(fact)
    }
    # 列が NULL の古い事実（列を足す前に書いた事実）はシグナルにしない。
    connection.execute(
        "INSERT INTO AGENT_RUN_FACTS (RUN_ID, STATUS, RUN_SOURCE) "
        "VALUES ('old', 'completed', 'user')"
    )
    assert "old" not in _matching(connection, ANY_SIGNAL_CONDITION)


# ---- 集計と一覧（memory / file） ----------------------------------------------------------


def _report(facts: list[RunFact], **filters: Any) -> Any:
    return build_signal_report(
        facts,
        days=filters.get("days", 7),
        now=NOW,
        agent_id=filters.get("agent_id"),
        kind=filters.get("kind"),
        agent_names={"default": "資料・データ横断アシスタント"},
        user_names=lambda uuids: {uuid: "Alice" for uuid in uuids},
        offset=filters.get("offset", 0),
        limit=filters.get("limit", 10),
    )


def test_report_counts_signals_compares_periods_and_lists_newest_first() -> None:
    facts = [
        _fact("r1", NOW - timedelta(hours=1), tool_failures=1),
        _fact("r2", NOW - timedelta(hours=2), feedback=FeedbackRating.NOT_HELPFUL),
        _fact("r3", NOW - timedelta(hours=3), status="failed", tool_failures=2),
        _fact("r4", NOW - timedelta(hours=4)),
        _fact("other", NOW - timedelta(hours=1), agent_id="sales", tool_failures=1),
        # 対象外（評価の Run・実行中）と、前の 7 日。
        _fact("evaluation", NOW - timedelta(hours=1), source="evaluation", status="failed"),
        _fact("running", NOW - timedelta(hours=1), status="running"),
        _fact("prev", NOW - timedelta(days=8), validation=VALIDATION_FAILED),
        _fact("prev-ok", NOW - timedelta(days=9)),
        _fact("too-old", NOW - timedelta(days=30), status="failed"),
    ]

    report = _report(facts)

    assert (report.summary.runs, report.summary.flagged) == (5, 4)
    assert report.summary.flagged_rate == pytest.approx(0.8)
    counts = {item.kind: item.count for item in report.summary.counts}
    assert [item.kind for item in report.summary.counts] == list(SignalKind)
    assert counts[SignalKind.TOOL_FAILED] == 3
    assert counts[SignalKind.NOT_HELPFUL] == counts[SignalKind.RUN_FAILED] == 1
    assert counts[SignalKind.VALIDATION_FAILED] == 0
    assert (report.previous.runs, report.previous.flagged) == (2, 1)
    # 新しい順（同じ日時は Run の ID の降順）。
    assert [item.run_id for item in report.items] == ["r1", "other", "r2", "r3"]
    first = next(item for item in report.items if item.run_id == "r3")
    assert first.signals == [SignalKind.TOOL_FAILED, SignalKind.RUN_FAILED]
    assert (first.display_name, first.agent_name, first.tool_failures) == (
        "Alice",
        "資料・データ横断アシスタント",
        2,
    )
    # 業務 Agent と種類で絞る（集計は種類で絞らない）。
    scoped = _report(facts, agent_id="default", kind=SignalKind.TOOL_FAILED)
    assert [item.run_id for item in scoped.items] == ["r1", "r3"]
    assert scoped.summary.runs == 4 and scoped.kind == SignalKind.TOOL_FAILED
    # 対象の Run が無ければ割合は None。
    assert _report([]).summary.flagged_rate is None


def test_report_pages_the_list() -> None:
    facts = [
        _fact(f"r{index:02d}", NOW - timedelta(minutes=index), tool_failures=1)
        for index in range(23)
    ]
    page = _report(facts, offset=20, limit=10)
    assert (page.total, page.offset, page.limit, page.has_next) == (23, 20, 10, False)
    assert [item.run_id for item in page.items] == ["r20", "r21", "r22"]


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
        self.connection.input_sizes.append(sizes)

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        if sql in self.connection.missing_columns:
            raise RuntimeError('ORA-00904: "DATA_TOOL_CALLS": invalid identifier')
        self.connection.executed.append((sql, rows))

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        self.connection.executed.append((sql, dict(binds)))
        self.rows = next(
            (rows for marker, rows in self.connection.answers.items() if marker in sql), []
        )

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self, answers: dict[str, list[tuple[Any, ...]]] | None = None) -> None:
        self.answers = answers or {}
        self.executed: list[tuple[str, Any]] = []
        self.input_sizes: list[dict[str, Any]] = []
        # 列が無いとして ORA-00904 にする MERGE（システムテーブルを更新する前のテーブル）。
        self.missing_columns: set[str] = set()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _oracle(connection: _Connection) -> OracleRunFactsStore:
    @contextmanager
    def connect() -> Iterator[_Connection]:
        yield connection

    return OracleRunFactsStore(connect)


def test_upsert_writes_the_signal_columns() -> None:
    connection = _Connection()
    fact = _fact("r1", outcome="needs_human", validation=VALIDATION_FAILED, tool_failures=2)

    _oracle(connection).upsert([fact])

    [(sql, rows)] = connection.executed
    assert sql == MERGE_SQL
    assert (rows[0]["answer_outcome"], rows[0]["validation_result"], rows[0]["tool_failures"]) == (
        "needs_human",
        "failed",
        2,
    )
    assert {"tool_failures", "approvals_rejected"} <= set(connection.input_sizes[0])
    for column in ("ANSWER_OUTCOME", "VALIDATION_RESULT", "TOOL_FAILURES", "APPROVALS_REJECTED"):
        assert f"target.{column} = source.{column}" in MERGE_SQL
    assert connection.commits == 1


def test_upsert_keeps_writing_signals_before_the_data_tool_column_exists() -> None:
    """migration 014 の前は、業務データの道具の列だけを除いて書く（#1834）。"""
    connection = _Connection()
    connection.missing_columns = {MERGE_SQL}

    _oracle(connection).upsert([_fact("r1", tool_failures=1, data_tool_calls=3)])

    [(sql, rows)] = connection.executed
    assert sql == MERGE_SQL_012
    assert "data_tool_calls" not in rows[0] and rows[0]["tool_failures"] == 1
    assert "data_tool_calls" not in connection.input_sizes[1]
    assert all(f":{name}" in MERGE_SQL_012 for name in rows[0])
    assert "DATA_TOOL_CALLS" not in MERGE_SQL_012 and "ANSWER_OUTCOME" in MERGE_SQL_012
    assert (connection.rollbacks, connection.commits) == (1, 1)


def test_upsert_keeps_writing_facts_before_the_signal_columns_exist() -> None:
    """システムテーブルを更新する（migration 012）までは、シグナルの列を除いて書く。"""
    connection = _Connection()
    connection.missing_columns = {MERGE_SQL, MERGE_SQL_012}

    _oracle(connection).upsert([_fact("r1", tool_failures=1)])

    [(sql, rows)] = connection.executed
    assert sql == LEGACY_MERGE_SQL
    assert SIGNAL_BINDS.isdisjoint(rows[0]) and SIGNAL_BINDS.isdisjoint(connection.input_sizes[2])
    assert all(f":{name}" in LEGACY_MERGE_SQL for name in rows[0])
    assert "ANSWER_OUTCOME" not in LEGACY_MERGE_SQL
    assert (connection.rollbacks, connection.commits) == (2, 1)


def test_signal_report_is_aggregated_and_paged_in_sql() -> None:
    created = datetime(2026, 10, 13, 0, 0)
    connection = _Connection(
        {
            "GROUP BY IN_PERIOD": [
                (1, 20, 6, 2, 1, 0, 2, 1, 1, 3, 1),
                (0, 10, 2, 1, 0, 0, 0, 0, 0, 1, 0),
            ],
            "SELECT COUNT(*)": [(13,)],
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
                    created,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "completed",
                    created,
                    "insufficient_evidence",
                    "failed",
                    2,
                    None,
                    "user",
                    0,
                )
            ],
        }
    )

    report = _oracle(connection).signal_report(
        days=7,
        now=NOW,
        agent_ids=frozenset({"sales"}),
        agent_id="sales",
        kind=SignalKind.TOOL_FAILED,
        offset=10,
        limit=10,
        agent_names={"sales": "営業の Agent"},
        user_names=lambda uuids: {ALICE: "Alice"},
    )

    assert report.source == "history" and report.kind == SignalKind.TOOL_FAILED
    assert (report.summary.runs, report.summary.flagged, report.summary.flagged_rate) == (
        20,
        6,
        0.3,
    )
    counts = {item.kind: item.count for item in report.summary.counts}
    assert counts == {
        SignalKind.NOT_HELPFUL: 2,
        SignalKind.VALIDATION_FAILED: 1,
        SignalKind.UNVALIDATED: 0,
        SignalKind.UNGROUNDED_NO_TOOLS: 2,
        SignalKind.UNANSWERED: 1,
        SignalKind.APPROVAL_REJECTED: 1,
        SignalKind.TOOL_FAILED: 3,
        SignalKind.RUN_FAILED: 1,
    }
    assert (report.previous.runs, report.previous.flagged) == (10, 2)
    assert (report.total, report.offset, report.limit) == (13, 10, 10)
    [item] = report.items
    assert item.signals == [
        SignalKind.NOT_HELPFUL,
        SignalKind.VALIDATION_FAILED,
        SignalKind.UNANSWERED,
        SignalKind.TOOL_FAILED,
    ]
    assert (item.agent_name, item.display_name, item.approvals_rejected) == (
        "営業の Agent",
        "Alice",
        0,
    )
    assert item.created_at == created.replace(tzinfo=UTC)
    executed = dict(connection.executed)
    summary_sql = next(sql for sql in executed if "GROUP BY IN_PERIOD" in sql)
    # 集計は種類で絞らず、対象の Run（終わった Run・評価の Run を除く）を期間で数える。
    assert SIGNAL_SCOPE_CONDITION in summary_sql
    assert SIGNAL_CONDITIONS[SignalKind.TOOL_FAILED] in summary_sql
    assert executed[summary_sql]["previous_since"] == datetime(2026, 9, 30, 3, 0)
    page_sql = next(sql for sql in executed if "OFFSET :page_offset" in sql)
    assert page_sql.rstrip().endswith("FETCH NEXT :page_limit ROWS ONLY")
    assert f"AND {SIGNAL_CONDITIONS[SignalKind.TOOL_FAILED]} ORDER BY CREATED_AT DESC" in page_sql
    page_binds = executed[page_sql]
    assert (page_binds["page_offset"], page_binds["page_limit"], page_binds["agent_id"]) == (
        10,
        10,
        "sales",
    )
    assert page_binds["scope_0"] == "sales" and "previous_since" not in page_binds
    count_sql = next(sql for sql in executed if sql.startswith("SELECT COUNT(*)"))
    assert ANY_SIGNAL_CONDITION not in count_sql


def test_signal_report_without_allowed_agents_does_not_query() -> None:
    connection = _Connection()
    report = _oracle(connection).signal_report(
        days=30,
        now=NOW,
        agent_ids=frozenset(),
        agent_id=None,
        kind=None,
        offset=0,
        limit=10,
        agent_names={},
        user_names=lambda uuids: {},
    )
    assert (report.summary.runs, report.total, connection.executed) == (0, 0, [])
    assert len(report.summary.counts) == len(SignalKind)


# ---- API ----------------------------------------------------------------------------------


def test_signals_api_lists_runs_to_review_from_saved_facts() -> None:
    store = MemoryRunFactsStore()
    now = datetime.now(UTC)
    store.upsert(
        [
            _fact("signal-1822-a", now - timedelta(days=1), tool_failures=1),
            _fact("signal-1822-b", now - timedelta(days=2), outcome="needs_human"),
            _fact("signal-1822-ok", now - timedelta(days=2)),
            _fact("signal-1822-old", now - timedelta(days=200), status="failed"),
        ]
    )
    run_facts_store.configure(store)

    data = client.get("/api/feedback/signals", params={"days": 30}).json()["data"]
    assert (data["source"], data["total"], data["summary"]["runs"]) == ("history", 2, 3)
    assert [item["run_id"] for item in data["items"]] == ["signal-1822-a", "signal-1822-b"]
    assert data["items"][1]["signals"] == ["unanswered"]
    by_kind = client.get(
        "/api/feedback/signals", params={"days": 365, "kind": "run_failed"}
    ).json()["data"]
    assert [item["run_id"] for item in by_kind["items"]] == ["signal-1822-old"]
    assert client.get("/api/feedback/signals", params={"days": 14}).status_code == 422
    assert client.get("/api/feedback/signals", params={"kind": "unknown"}).status_code == 422
    assert client.get("/api/feedback/signals", params={"limit": 101}).status_code == 422


def test_signals_api_falls_back_to_runs_in_memory() -> None:
    """保存先が無い（memory / file）・DB の障害では、メモリの Run から集める。"""

    class _Broken(MemoryRunFactsStore):
        def signal_report(self, **_kwargs: Any) -> Any:
            raise RuntimeError('ORA-00904: "VALIDATION_RESULT": invalid identifier')

    run_facts_store.configure(_Broken())
    response = client.get("/api/feedback/signals")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["source"] == "memory"


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_signals_api_needs_the_feedback_menu_and_is_limited_to_allowed_agents(
    auth: ProductionAuth,
) -> None:
    store = MemoryRunFactsStore()
    now = datetime.now(UTC)
    store.upsert(
        [
            _fact("sales-1822", now - timedelta(days=1), agent_id="sales-1822", tool_failures=1),
            _fact("hr-1822", now - timedelta(days=1), agent_id="hr-1822", tool_failures=1),
        ]
    )
    run_facts_store.configure(store)
    auth.user_with_permissions("signals-1822", [MENU_FEEDBACK], agent_ids=["sales-1822"])
    auth.user_with_permissions("nobody-1822", [], agent_ids=["sales-1822"])

    allowed = client.get("/api/feedback/signals", headers=login("signals-1822"))
    assert allowed.status_code == 200, allowed.text
    assert [item["run_id"] for item in allowed.json()["data"]["items"]] == ["sales-1822"]
    denied = client.get("/api/feedback/signals", headers=login("nobody-1822"))
    assert denied.status_code == 403
