"""Agent の Run の span を、共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）に
実の Oracle で書いて読む結合テスト。

CI の実 Oracle の結合テスト（`.github/workflows/oracle-integration.yml`。システムテーブルを
作った後の Oracle AI Database Free）だけで実行する（`AGENT_RUN_ORACLE_INTEGRATION=1`。接続は
`platform/.env` の `PLATFORM_ORACLE_*`）。ほかでは skip する。テストごとに別の trace に書き、
終わったら消す（並列でも互いの行を読まない）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pr_backend_core.chat_progress import chat_progress_details
from pr_backend_core.observability.trace_store import TraceSpanOwner, TraceSpanWriter
from pr_backend_core.observability.tracing import SpanDisplay, SpanUpdate

from app.features.agent.trace_spans import AgentTraceSpans
from app.oracle_connection import connect_platform_oracle, platform_oracle_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_RUN_ORACLE_INTEGRATION") != "1",
    reason="実 Oracle の結合テスト（AGENT_RUN_ORACLE_INTEGRATION=1）だけで実行する",
)

RUN_USER = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_USER = "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.fixture
def trace_id() -> Iterator[str]:
    value = uuid4().hex
    yield value
    with connect_platform_oracle() as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM PLATFORM_TRACE_SPANS WHERE TRACE_ID = :trace_id", {"trace_id": value}
        )
        connection.commit()


def _update(trace_id: str, span_id: str, status: str, **fields: object) -> SpanUpdate:
    started = datetime(2026, 10, 1, tzinfo=UTC)
    return SpanUpdate.model_validate(
        {
            "trace_id": trace_id,
            "span_id": span_id,
            "operation_name": "gen_ai.execute_tool",
            "status": status,
            "started_at": started,
            "finished_at": None if status == "running" else started + timedelta(seconds=1),
            **fields,
        }
    )


def test_run_spans_are_written_and_read_with_the_owner_filter(trace_id: str) -> None:
    spans = AgentTraceSpans()
    spans.use_writer(
        TraceSpanWriter(connection_factory=platform_oracle_connection, flush_seconds=60)
    )
    owner = TraceSpanOwner(
        product="agent", operation_id=f"run_{trace_id}", owner_user_uuid=RUN_USER
    )
    other = TraceSpanOwner(product="rag", operation_id="live-call", owner_user_uuid=OTHER_USER)
    details = chat_progress_details(code=[("引数", '{"query": "合成の語"}', "json")])
    root = "a000000000000001"

    spans.submit(
        _update(trace_id, root, "running", operation_name="agent.execute", root=True), owner
    )
    tool = _update(
        trace_id,
        "a000000000000002",
        "done",
        parent_span_id=root,
        display=SpanDisplay(kind="tool", params={"tool": "synthetic_tool"}, details=details),
        attributes={"gen_ai.tool.name": "synthetic_tool"},
    )
    spans.submit(tool, owner)
    spans.submit(_update(trace_id, "a000000000000003", "done", parent_span_id=root), other)
    spans.submit(_update(trace_id, root, "done", operation_name="agent.execute", root=True), owner)
    assert spans._writer is not None  # noqa: SLF001 - テストで待たずに書く
    spans._writer.flush()  # noqa: SLF001
    assert spans.dropped(trace_id) == 0

    mine = spans.fetch(trace_id, viewer_user_uuid=RUN_USER)
    assert [(span.span_id, span.status, span.root) for span in mine] == [
        (root, "done", True),
        ("a000000000000002", "done", False),
    ]
    [written_tool] = [span for span in mine if span.span_id == "a000000000000002"]
    assert (written_tool.display_kind, written_tool.display_params) == (
        "tool",
        {"tool": "synthetic_tool"},
    )
    assert written_tool.parent_span_id == root and written_tool.finished_at is not None
    # 全員の span（system_admin）は、ほかの利用者が同じ trace に書いた span も読む。
    assert len(spans.fetch(trace_id, viewer_user_uuid=None)) == 3
    # 工程の詳細（引数）は表に入れない。
    with connect_platform_oracle() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM PLATFORM_TRACE_SPANS WHERE TRACE_ID = :trace_id "
            "AND (DISPLAY_PARAMS_JSON LIKE '%合成の語%' OR ATTRIBUTES_JSON LIKE '%合成の語%')",
            {"trace_id": trace_id},
        )
        assert cursor.fetchone()[0] == 0


def test_span_times_keep_sub_seconds_and_order(trace_id: str) -> None:
    """ミリ秒の差の 2 つの span は、書いて読んでも時刻の差と開始の順を保つ（#3271）。

    python-oracledb は `datetime` を既定で DATE（秒まで）で bind する。秒より細かい部分が落ちると、
    同じ秒の span は SPAN_ID の順になり、所要時間も整数の秒になる。後に始まる span に小さい
    SPAN_ID を付けて、開始の時刻で並ぶことを確かめる。
    """
    spans = AgentTraceSpans()
    spans.use_writer(
        TraceSpanWriter(connection_factory=platform_oracle_connection, flush_seconds=60)
    )
    owner = TraceSpanOwner(
        product="agent", operation_id=f"run_{trace_id}", owner_user_uuid=RUN_USER
    )
    base = datetime(2026, 10, 1, 0, 0, 1, 123456, tzinfo=UTC)
    first, second = "b000000000000002", "b000000000000001"
    times = {
        first: (base, base + timedelta(milliseconds=3)),
        second: (base + timedelta(milliseconds=3), base + timedelta(milliseconds=10)),
    }
    for span_id, (started, finished) in times.items():
        spans.submit(
            SpanUpdate.model_validate(
                {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "operation_name": "gen_ai.execute_tool",
                    "status": "done",
                    "started_at": started,
                    "finished_at": finished,
                }
            ),
            owner,
        )
    assert spans._writer is not None  # noqa: SLF001 - テストで待たずに書く
    spans._writer.flush()  # noqa: SLF001
    assert spans.dropped(trace_id) == 0

    read = spans.fetch(trace_id, viewer_user_uuid=RUN_USER)
    assert [span.span_id for span in read] == [first, second]
    assert [(span.started_at, span.finished_at) for span in read] == [
        times[first],
        times[second],
    ]
