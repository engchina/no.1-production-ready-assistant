"""共通の span の表（`PLATFORM_TRACE_SPANS`）の書き込み・読み出しと、MCP の trace
の受け渡し（#3226 の案 A）。

実 DB は使わない（DB-API の形の fake で SQL と bind を確かめる）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_backend_core.chat_progress import (
    ChatProgressStepEvent,
    chat_progress_details,
    parse_chat_progress_events,
)
from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability import trace_store
from pr_backend_core.observability.mcp_trace import (
    incoming_mcp_traceparent,
    mcp_server_trace,
    mcp_trace_meta,
    operation_trace_parent,
    traceparent_links,
)
from pr_backend_core.observability.trace_store import (
    StoredTraceSpan,
    TraceSpanOwner,
    TraceSpanWriter,
    fetch_trace_spans,
    purge_trace_spans,
    trace_progress_events,
)
from pr_backend_core.observability.tracing import (
    SpanUpdate,
    configure_tracing,
    observed_span,
    operation_trace,
    shutdown_tracing,
)

OWNER = TraceSpanOwner(product="rag", operation_id="op-a", owner_user_uuid="user-a")
# python-oracledb の `DB_TYPE_TIMESTAMP` の代わり（共有パッケージのテストは oracledb を入れない）。
DB_TYPE_TIMESTAMP = object()


@pytest.fixture(autouse=True)
def reset_tracing() -> Iterator[None]:
    yield
    shutdown_tracing()


@pytest.fixture(autouse=True)
def fake_oracledb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trace_store,
        "_load_oracledb",
        lambda: SimpleNamespace(DB_TYPE_TIMESTAMP=DB_TYPE_TIMESTAMP),
    )


class FakeCursor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.rowcount = 0

    def setinputsizes(self, **sizes: Any) -> None:
        self.db.calls.append(("setinputsizes", "", dict(sizes)))

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        if self.db.fail_writes:
            self.db.fail_writes -= 1
            raise RuntimeError("ORA-03113")
        self.db.calls.append(("executemany", sql, [dict(row) for row in rows]))
        for row in rows:
            self.db.rows[(row["trace_id"], row["span_id"])] = dict(row)

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        self.db.calls.append(("execute", sql, dict(binds)))
        self.rowcount = self.db.purge_counts.pop(0) if self.db.purge_counts else 0

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.db.select_rows


class FakeConnection:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        self.db.commits += 1


class FakeDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.commits = 0
        self.fail_writes = 0
        self.purge_counts: list[int] = []
        self.select_rows: list[tuple[Any, ...]] = []

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        yield FakeConnection(self)


def update(
    span_id: str,
    status: str = "running",
    *,
    name: str = "llm",
    root: bool = False,
    kind: str = "internal",
) -> SpanUpdate:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SpanUpdate(
        span_id=span_id.rjust(16, "0"),
        trace_id="a" * 32,
        operation_name=name,
        status=status,  # type: ignore[arg-type]
        started_at=now,
        finished_at=None if status == "running" else now + timedelta(seconds=1),
        root=root,
        span_kind=kind,  # type: ignore[arg-type]
    )


def test_writer_coalesces_and_never_writes_step_details() -> None:
    """開始と終了は 1 行にまとめ、工程の詳細（検索文など）は表に入れない。"""
    db = FakeDb()
    writer = TraceSpanWriter(db.connect)
    configure_tracing(TracingSettings(), service_name="synthetic")
    with operation_trace("rag.answer", operation_id="op-a", listener=writer.listener(OWNER)):
        with observed_span("rag.engine.stage") as span:
            span.set_display(
                kind="answer_step",
                params={"name": "文書検索", "llm_calls": 1},
                details=chat_progress_details(fields=[("検索文", "synthetic-private-query")]),
            )
        with observed_span("mcp.request", kind="client"):
            pass
    writer.flush()
    rows = list(db.rows.values())
    assert {row["name"] for row in rows} == {"rag.answer", "rag.engine.stage", "mcp.request"}
    stage = next(row for row in rows if row["name"] == "rag.engine.stage")
    assert stage["status"] == "done" and stage["finished_at"] is not None
    assert stage["display_kind"] == "answer_step"
    assert json.loads(stage["display_params_json"]) == {"name": "文書検索", "llm_calls": 1}
    assert "synthetic-private-query" not in json.dumps(db.calls, default=str)
    root = next(row for row in rows if row["name"] == "rag.answer")
    assert root["is_root"] == 1 and root["owner_user_uuid"] == "user-a" and root["product"] == "rag"
    # 開始と終了が同じ batch に入れば、span ごとに 1 行だけ書く。
    assert sum(len(call[2]) for call in db.calls if call[0] == "executemany") == 3


def test_writer_binds_span_times_as_timestamp_to_keep_sub_seconds() -> None:
    """python-oracledb は `datetime` を既定で DATE（秒まで）で bind するので、開始・終了の時刻は
    TIMESTAMP で bind して秒より細かい部分を残す（#3271）。"""
    db = FakeDb()
    writer = TraceSpanWriter(db.connect)
    started = datetime(2026, 1, 1, 0, 0, 1, 123456, tzinfo=UTC)
    writer.submit(
        SpanUpdate(
            span_id="1".rjust(16, "0"),
            trace_id="a" * 32,
            operation_name="llm",
            status="done",
            started_at=started,
            finished_at=started + timedelta(milliseconds=5),
        ),
        OWNER,
    )
    writer.flush()
    kinds = [call[0] for call in db.calls]
    assert kinds == ["setinputsizes", "executemany"]
    assert db.calls[0][2] == {
        "started_at": DB_TYPE_TIMESTAMP,
        "finished_at": DB_TYPE_TIMESTAMP,
    }
    [row] = db.calls[1][2]
    assert row["started_at"] == datetime(2026, 1, 1, 0, 0, 1, 123456)
    assert row["finished_at"] == datetime(2026, 1, 1, 0, 0, 1, 128456)


def test_full_queue_drops_only_normal_span_starts_and_counts_them() -> None:
    db = FakeDb()
    writer = TraceSpanWriter(db.connect, queue_size=2)
    writer.submit(update("1"), OWNER)
    writer.submit(update("2"), OWNER)
    writer.submit(update("3"), OWNER)  # 満杯: 開始の知らせは捨てる
    writer.submit(update("4", "done"), OWNER)  # 終了は開始の知らせを押し出して入れる
    writer.submit(update("5", root=True), OWNER)  # 境界（root）は捨てない
    writer.submit(update("6", name="mcp.request", kind="client"), OWNER)
    writer.submit(update("7", name="mcp.server", kind="server"), OWNER)
    assert writer.dropped("a" * 32) == 2
    writer.flush()
    written = {row["span_id"].lstrip("0") for row in db.rows.values()}
    assert written == {"2", "4", "5", "6", "7"}


def test_failed_writes_retry_then_drop_normal_spans_but_keep_boundary_spans() -> None:
    db = FakeDb()
    clock = [0.0]
    writer = TraceSpanWriter(
        db.connect,
        retries=2,
        retry_backoff_seconds=0,
        boundary_retry_seconds=60,
        clock=lambda: clock[0],
    )
    writer.submit(update("1", "done"), OWNER)
    writer.submit(update("2", "done", root=True), OWNER)
    db.fail_writes = 2
    writer.flush()
    writer.flush()
    # 通常の span は 2 回失敗して捨て、数える。境界の span は再送を続ける。
    assert writer.dropped("a" * 32) == 1
    writer.flush()
    assert {row["span_id"].lstrip("0") for row in db.rows.values()} == {"2"}
    # 境界の span も、再送の期限を過ぎたら諦めて数える。
    writer.submit(update("3", "done", root=True), OWNER)
    db.fail_writes = 1
    writer.flush()
    clock[0] = 120
    db.fail_writes = 1
    writer.flush()
    assert writer.dropped("a" * 32) == 2


def test_retention_zero_never_deletes_and_positive_days_delete_old_spans() -> None:
    db = FakeDb()
    assert purge_trace_spans(db.connect, retention_days=0) == 0
    assert db.calls == []
    db.purge_counts = [3, 1]
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert purge_trace_spans(db.connect, retention_days=30, batch=3, now=now) == 4
    executes = [call for call in db.calls if call[0] == "execute"]
    assert "DELETE FROM PLATFORM_TRACE_SPANS" in executes[0][1]
    assert executes[0][2]["cutoff"] == datetime(2026, 1, 30)
    # 期限の時刻も TIMESTAMP で bind する（#3271）。
    assert ("setinputsizes", "", {"cutoff": DB_TYPE_TIMESTAMP}) in db.calls
    writer = TraceSpanWriter(db.connect)
    assert writer.retention_days == 0


def _row(span_id: str, parent: str | None, name: str, **extra: Any) -> tuple[Any, ...]:
    values = {
        "attempt": 0,
        "root": 0,
        "owner": "user-a",
        "status": "done",
        "finished": datetime(2026, 1, 1, 0, 0, 2),
        "kind": None,
        "params": None,
        "attrs": None,
        "operation": "op-a",
        **extra,
    }
    return (
        "a" * 32,
        span_id,
        parent,
        "rag",
        values["operation"],
        values["attempt"],
        values["root"],
        values["owner"],
        name,
        "internal",
        values["status"],
        datetime(2026, 1, 1, 0, 0, 1),
        values["finished"],
        values["kind"],
        values["params"],
        values["attrs"],
    )


def test_fetch_filters_by_owner_unless_system_admin() -> None:
    db = FakeDb()
    db.select_rows = [_row("1" * 16, None, "rag.answer", root=1, params='{"name":"x","bad":[1]}')]
    with db.connect() as connection:
        spans = fetch_trace_spans(connection, "a" * 32, viewer_user_uuid="user-a")
        fetch_trace_spans(connection, "a" * 32, viewer_user_uuid=None)
    own, admin = db.calls
    assert "OWNER_USER_UUID = :viewer" in own[1] and own[2]["viewer"] == "user-a"
    assert ":viewer" not in admin[1] and "viewer" not in admin[2]
    assert spans[0].root and spans[0].display_params == {"name": "x"}
    assert spans[0].started_at.tzinfo is not None
    # 時刻は UTC の TIMESTAMP にして読む（名前つきの時間帯は thin mode で読めない。#3241）。
    assert "SYS_EXTRACT_UTC(STARTED_AT) AS STARTED_AT" in own[1]
    assert "SYS_EXTRACT_UTC(FINISHED_AT) AS FINISHED_AT" in own[1]


def test_sql_writes_utc_offset_not_named_time_zone() -> None:
    # 地域名の時間帯（'UTC'）で書くと、python-oracledb の thin mode が値を読めない
    # （DPY-3022。#3241）。
    for sql in (trace_store._MERGE_SQL, trace_store._PURGE_SQL):
        assert "'UTC'" not in sql
        assert "'+00:00'" in sql


def _stored(span_id: str, parent: str | None, **extra: Any) -> StoredTraceSpan:
    base: dict[str, Any] = {
        "trace_id": "a" * 32,
        "span_id": span_id,
        "parent_span_id": parent,
        "product": "nl2sql",
        "operation_id": "job-a",
        "attempt": 0,
        "root": False,
        "owner_user_uuid": "user-a",
        "name": "nl2sql.generate_sql",
        "span_kind": "internal",
        "status": "done",
        "started_at": datetime(2026, 1, 1, tzinfo=UTC),
        "finished_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        "display_kind": None,
        "display_params": {},
        "attributes": {},
    }
    base.update(extra)
    return StoredTraceSpan(**base)


def test_progress_events_use_latest_attempt_and_product_record_as_final_state() -> None:
    spans = [
        _stored("1" * 16, None, root=True, attempt=0, name="nl2sql.execute"),
        _stored("2" * 16, "1" * 16, attempt=0),
        _stored("3" * 16, None, root=True, attempt=1, name="nl2sql.execute", status="running"),
        _stored("4" * 16, "3" * 16, attempt=1, display_kind="generate_sql", status="running"),
        _stored("5" * 16, "3" * 16, attempt=1, status="cancelled"),
    ]
    events = trace_progress_events(
        spans,
        target_id="job-a",
        terminal="failed",
        details={"4" * 16: {"fields": [{"label": "エンジン", "value": "synthetic"}]}},
    )
    steps = [event for event in events if event["type"] == "step"]
    # 前の試行の木は出さない。製品の記録が終端なら、表で実行中の span はその状態で閉じる。
    assert [event["step_id"][0] for event in steps] == ["3", "4", "5"]
    assert steps[1]["status"] == "failed" and "finished_at" in steps[1]
    assert (
        steps[1]["kind"] == "generate_sql"
        and steps[1]["details"]["fields"][0]["value"] == "synthetic"
    )
    assert steps[2]["status"] == "skipped" and steps[2]["cancelled"] is True
    assert steps[0]["root"] is True
    assert events[-1] == {**events[-1], "type": "terminal", "status": "failed"}


def test_writer_puts_the_product_step_name_first_in_display_params() -> None:
    """製品が渡す工程の名前は `display_name` として名前の値の先頭に入れる（#3272。列は足さない）。

    名前の無い span（技術の span）は今までどおり。製品が名前を返さなければ値は変えない。
    """
    db = FakeDb()
    named = {"rag.engine.stage": "表示の工程 A"}
    writer = TraceSpanWriter(
        db.connect, display_name=lambda update: named.get(update.operation_name)
    )
    configure_tracing(TracingSettings(), service_name="synthetic")
    with operation_trace("rag.answer", operation_id="op-a", listener=writer.listener(OWNER)):
        with observed_span("rag.engine.stage") as span:
            span.set_display(kind="answer_step", params={"name": "工程 A", "llm_calls": 1})
        with observed_span("gen_ai.chat", kind="client"):
            pass
    writer.flush()
    rows = {row["name"]: row for row in db.rows.values()}
    stage = json.loads(rows["rag.engine.stage"]["display_params_json"])
    assert list(stage) == ["display_name", "name", "llm_calls"]
    assert stage["display_name"] == "表示の工程 A"
    assert rows["gen_ai.chat"]["display_params_json"] is None


def test_progress_events_carry_the_product_of_each_span() -> None:
    """処理の経過のイベントは、span を書いた製品を持つ（#3272。画面は製品の境目に製品名を出す）。"""
    spans = [
        _stored("1" * 16, None, root=True, product="agent", name="agent.execute"),
        _stored("2" * 16, "1" * 16, product="rag", display_params={"display_name": "工程 B"}),
    ]
    steps = [
        event
        for event in trace_progress_events(spans, target_id="run-a")
        if event["type"] == "step"
    ]
    # 工程の名前は製品の工程の定義の値（params）と分けて送る。
    assert [
        (event["product"], event.get("display_name"), event.get("params")) for event in steps
    ] == [
        ("agent", None, None),
        ("rag", "工程 B", None),
    ]
    parsed = parse_chat_progress_events(steps)
    assert [
        (event.product, event.display_name)
        for event in parsed
        if isinstance(event, ChatProgressStepEvent)
    ] == [("agent", None), ("rag", "工程 B")]


def test_mcp_trace_context_trusts_only_service_tokens() -> None:
    """サービストークンの traceparent だけを親にする。

    外部のものは新しい trace と span link にする。
    """
    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    caller_trace = "b" * 32
    caller_span = "c" * 16
    incoming = f"00-{caller_trace}-{caller_span}-01"
    assert incoming_mcp_traceparent({"traceparent": incoming}, "invalid") == incoming
    assert incoming_mcp_traceparent({"traceparent": "invalid"}, incoming) == incoming
    db = FakeDb()
    writer = TraceSpanWriter(db.connect)
    owner = TraceSpanOwner(product="rag", operation_id="mcp-a", owner_user_uuid="user-a")
    with mcp_server_trace(
        operation_id="mcp-a",
        trusted=True,
        traceparent=incoming,
        listener=writer.listener(owner),
        method="tools/call",
        tool_name="tool_a",
    ) as trusted:
        assert mcp_trace_meta()["traceparent"].split("-")[1] == caller_trace
    with mcp_server_trace(operation_id="mcp-b", trusted=False, traceparent=incoming) as external:
        pass
    writer.flush()
    assert trusted.telemetry_trace_id == caller_trace
    server_row = next(iter(db.rows.values()))
    assert server_row["parent_span_id"] == caller_span and server_row["span_kind"] == "server"
    assert server_row["is_root"] == 1
    # OpenTelemetry の MCP の semantic conventions の method とツールの名前（#3272）。
    attributes = json.loads(server_row["attributes_json"])
    assert (attributes["mcp.method.name"], attributes["gen_ai.tool.name"]) == (
        "tools/call",
        "tool_a",
    )
    assert external.telemetry_trace_id != caller_trace
    assert shutdown_tracing()
    exported = {span.context.trace_id: span for span in exporter.get_finished_spans()}
    linked = exported[int(external.telemetry_trace_id or "0", 16)]
    assert [f"{link.context.trace_id:032x}" for link in linked.links] == [caller_trace]


def test_owner_requires_operation_and_owner() -> None:
    # 空の値は Oracle で NULL になり、同じまとめの書き込みごと失敗するので、作るときに断る（#3226）
    # 。
    with pytest.raises(ValueError):
        TraceSpanOwner(product="rag", operation_id="op-a", owner_user_uuid=" ")
    with pytest.raises(ValueError):
        TraceSpanOwner(product="rag", operation_id="", owner_user_uuid="user-a")


def test_operation_trace_parent_continues_only_trusted_mcp_traces() -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(TracingSettings(), service_name="synthetic", exporter=exporter)
    caller_trace = "d" * 32
    incoming = f"00-{caller_trace}-{'e' * 16}-01"
    # 信頼した MCP の要求の中は、今の trace を続ける（links は空）。
    with (
        mcp_server_trace(operation_id="mcp-a", trusted=True, traceparent=incoming),
        operation_trace_parent() as links,
    ):
        assert links == ()
        with operation_trace("rag.answer", operation_id="op-a") as continued:
            pass
    # 画面の要求などの span の中は、新しい trace にして今の span を link に残す。
    with (
        operation_trace("http.request", operation_id="req-b") as request,
        operation_trace_parent() as links,
    ):
        assert len(links) == 1
        with operation_trace("rag.answer", operation_id="op-b", links=links) as detached:
            pass
    assert continued.telemetry_trace_id == caller_trace
    assert detached.telemetry_trace_id not in {request.telemetry_trace_id, caller_trace}
    assert [f"{link.context.trace_id:032x}" for link in links] == [request.telemetry_trace_id]
    assert shutdown_tracing()


def test_traceparent_links_ignores_invalid_values() -> None:
    valid = f"00-{'f' * 32}-{'1' * 16}-01"
    assert [f"{link.context.trace_id:032x}" for link in traceparent_links(valid)] == ["f" * 32]
    assert traceparent_links("invalid") == []
    assert traceparent_links(None) == []
