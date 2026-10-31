"""製品をまたぐ MCP の trace（Agent → NL2SQL の `POST /api/mcp`。#3226 の案 A）。

- サービストークン（Agent の Run）の要求は、来た traceparent（`params._meta` を優先）を親にして、
  受けた server の span（`mcp.server`）を共通の span の表に書く（Agent の `mcp.request` の下に木が
  つながる）。ツールが作るジョブは、その span の下で worker が実行する（同じ trace）。
- API キーの外部の MCP クライアントの traceparent は親にしない（新しい trace）。
- `tools/call` のほかの method は span を作らない。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from pr_backend_core.mcp import McpServer, McpTool
from pr_backend_core.observability.tracing import capture_trace_context, observed_span
from pydantic import BaseModel
from test_nl2sql_job_runtime import _repository, _request, _worker
from trace_span_fake import TraceTable, trace_table  # noqa: F401 - fixture

from app.features.mcp.router import traced_mcp_server
from app.features.nl2sql.models import RUN_SOURCE_MCP, JobOrigin, JobStatus
from app.features.nl2sql.trace_spans import STAGE_DISPLAY_NAMES
from app.settings import get_settings

_AGENT_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_AGENT_SPAN = "00f067aa0ba902b7"
_TRACEPARENT = f"00-{_AGENT_TRACE}-{_AGENT_SPAN}-01"
_HEADER_TRACEPARENT = f"00-{'1' * 32}-{'2' * 16}-01"


class _Empty(BaseModel):
    pass


def _request_with(*, service_token: bool, header: str | None = None) -> Any:
    state = SimpleNamespace(principal=SimpleNamespace(user_uuid="user-1"))
    if service_token:
        state.service_token_claims = {"sub": "user-1"}
    else:
        state.api_key = SimpleNamespace(name="キー A")
    return SimpleNamespace(state=state, headers={"traceparent": header} if header else {})


def _server(seen: dict[str, Any]) -> McpServer:
    def run(_: _Empty) -> dict[str, str]:
        with observed_span("nl2sql.tool_work"):
            seen["context"] = capture_trace_context()
        return {"ok": "yes"}

    return McpServer(
        name="nl2sql",
        version="test",
        tools=[McpTool(name="probe", description="合成", input_model=_Empty, handler=run)],
    )


def _call(server: McpServer, *, meta: dict[str, str] | None = None) -> Any:
    params: dict[str, Any] = {"name": "probe", "arguments": {}}
    if meta is not None:
        params["_meta"] = meta
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return anyio.run(lambda: server.handle(message, has_any_permission=lambda _: True))


def test_service_token_call_is_a_child_of_the_agent_span(
    trace_table: TraceTable,  # noqa: F811
) -> None:
    seen: dict[str, Any] = {}
    server = traced_mcp_server(
        _request_with(service_token=True, header=_HEADER_TRACEPARENT), _server(seen)
    )
    response = _call(server, meta={"traceparent": _TRACEPARENT})
    assert response["result"]["isError"] is False
    trace_table.flush()
    rows = trace_table.db.spans(trace_id=_AGENT_TRACE)
    [root] = [row for row in rows if row["name"] == "mcp.server"]
    # `params._meta` の traceparent を header より優先し、Agent の span の下に server の span
    # を置く。
    assert root["parent_span_id"] == _AGENT_SPAN
    assert root["span_kind"] == "server" and root["is_root"] == 1
    assert root["owner_user_uuid"] == "user-1" and root["product"] == "nl2sql"
    # OpenTelemetry の MCP の semantic conventions の method と呼んだツール（#3272）。
    attributes = json.loads(root["attributes_json"])
    assert (attributes["mcp.method.name"], attributes["gen_ai.tool.name"]) == (
        "tools/call",
        "probe",
    )
    [work] = [row for row in rows if row["name"] == "nl2sql.tool_work"]
    [tool] = [row for row in rows if row["name"] == "gen_ai.execute_tool"]
    assert tool["parent_span_id"] == root["span_id"]
    assert work["parent_span_id"] == tool["span_id"]
    assert seen["context"]["traceparent"].split("-")[1] == _AGENT_TRACE


def test_api_key_call_starts_a_new_trace(trace_table: TraceTable) -> None:  # noqa: F811
    server = traced_mcp_server(
        _request_with(service_token=False, header=_HEADER_TRACEPARENT), _server({})
    )
    _call(server, meta={"traceparent": _TRACEPARENT})
    trace_table.flush()
    [root] = [row for row in trace_table.db.rows.values() if row["name"] == "mcp.server"]
    assert root["parent_span_id"] is None
    assert root["trace_id"] not in {_AGENT_TRACE, "1" * 32}


def test_other_methods_do_not_write_spans(trace_table: TraceTable) -> None:  # noqa: F811
    server = traced_mcp_server(_request_with(service_token=True), _server({}))
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    anyio.run(lambda: server.handle(message, has_any_permission=lambda _: True))
    trace_table.flush()
    assert trace_table.db.rows == {}


def test_job_from_the_mcp_call_runs_under_the_server_span(
    trace_table: TraceTable,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP のツールが作ったジョブは、worker が server の span の下で実行する。

    Agent と同じ trace になる。
    """

    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    service = _worker(_repository())
    created: dict[str, str] = {}

    def run(_: _Empty) -> dict[str, str]:
        job = service.start_job(
            _request(),
            actor_user_uuid="user-1",
            actor_is_system_admin=False,
            origin=JobOrigin(run_source=RUN_SOURCE_MCP),
        )
        created["job_id"] = job.job_id
        return {"job_id": job.job_id}

    server = traced_mcp_server(
        _request_with(service_token=True),
        McpServer(
            name="nl2sql",
            version="test",
            tools=[McpTool(name="probe", description="合成", input_model=_Empty, handler=run)],
        ),
    )
    _call(server, meta={"traceparent": _TRACEPARENT})
    job_id = created["job_id"]
    assert service.run_next_nl2sql_job(worker_id="mcp-test", job_id=job_id)
    job = service.get_job(job_id, actor_user_uuid="user-1")
    assert job is not None and job.status == JobStatus.DONE and job.trace_id == _AGENT_TRACE
    trace_table.flush()
    rows = trace_table.db.spans(trace_id=_AGENT_TRACE)
    [server_span] = [row for row in rows if row["name"] == "mcp.server"]
    [job_root] = [row for row in rows if row["operation_id"] == job_id and row["is_root"]]
    [tool] = [row for row in rows if row["name"] == "gen_ai.execute_tool"]
    assert tool["parent_span_id"] == server_span["span_id"]
    assert job_root["parent_span_id"] == tool["span_id"]
    # ジョブの処理の経過はジョブの span だけ（同じ trace の server の span は入れない）。
    progress = service.get_job_trace(job_id, actor_user_uuid="user-1")
    assert progress is not None
    names = {event.name for event in progress.trace.events if event.type == "step"}
    assert "mcp.server" not in names and "nl2sql.execute" in names
    # 工程の span は NL2SQL の画面と同じ工程の名前（名詞）を `display_name` に持つ（#3272。別の
    # 製品の画面〔Agent の実行の詳細〕が出す）。工程でない span には付けない。
    named: dict[str, str | None] = {}
    for row in rows:
        if row["operation_id"] != job_id:
            continue
        stage = json.loads(row["attributes_json"] or "{}").get("pr.stage")
        display_name = json.loads(row["display_params_json"] or "{}").get("display_name")
        named[str(stage)] = display_name
    assert named["None"] is None
    stages = {stage: name for stage, name in named.items() if stage != "None"}
    assert stages and stages == {stage: STAGE_DISPLAY_NAMES[stage] for stage in stages}
