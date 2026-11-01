"""NL2SQL の trace の木の形（処理の経過の親子。#3226・#3272・#3477）。

画面の処理の経過は span の木の投影なので、工程の中で起きた処理（LLM・DB の呼び出し・再試行・
切り替え）が、その工程の span の子になっていることを主な経路で確かめる。

- ジョブ（生成 + 実行）: 工程の span は実行の全体（`nl2sql.execute`）の子で、SQL の生成の LLM の
  呼び出し（`gen_ai.chat`。HTTP の再試行は試行ごと）は `nl2sql.generate_sql` の子。
- MCP の `nl2sql_query`: 回答前の確認の LLM の呼び出しは確認の工程（`nl2sql.requirement`）の子、
  ジョブは確認の工程の外（ツールの span の子）で作る。

LLM は OCI Enterprise AI の実の client を httpx の mock で、DB は fake の接続で動かす。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pr_backend_core.observability.tracing import (
    capture_trace_context,
    observed_span,
)
from test_enterprise_ai_client import _direct_settings, _mock_http
from test_mcp import (  # noqa: F401 - fixture
    _call,
    _FakeNl2SqlService,
    _ReadyRequirementRuntime,
    _run,
    _structured,
    _token,
    _Users,
    fake_service,
    users,
)
from test_nl2sql_job_runtime import _GENERATED, _repository, _request, _worker
from trace_span_fake import TraceTable, trace_table  # noqa: F401 - fixture

from app.features.nl2sql import enterprise_ai_client, ontology_router
from app.features.nl2sql.sql_requirement import RequirementAiUnavailableError
from app.features.nl2sql.trace_spans import MCP_STAGE_DISPLAY_NAMES, REQUIREMENT_STAGE
from app.settings import get_settings

_STAGES = [
    "nl2sql.queue",
    "nl2sql.attachment_evidence",
    "nl2sql.prepare_context",
    "nl2sql.generate_sql",
    "nl2sql.safety_check",
    "nl2sql.execute_sql",
    "nl2sql.format_results",
]
_AGENT_TRACEPARENT = f"00-{'4' * 32}-{'5' * 16}-01"

type Row = dict[str, Any]


def _attributes(row: Row) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(row["attributes_json"] or "{}")
    return loaded


def _children(rows: list[Row], parent: Row, name: str | None = None) -> list[Row]:
    found = [
        row
        for row in rows
        if row["parent_span_id"] == parent["span_id"] and (name is None or row["name"] == name)
    ]
    return sorted(found, key=lambda row: (row["started_at"], row["span_id"]))


def test_job_generation_retry_and_execution_nest_under_their_stages(
    monkeypatch: pytest.MonkeyPatch,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    calls: list[int] = []

    def handler(request: Any) -> Any:
        import httpx

        calls.append(1)
        if len(calls) == 1:
            # 1 回目は一時的な失敗（再試行する）。
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"output_text": _GENERATED})

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    client = enterprise_ai_client.OciEnterpriseAiDirectClient(_direct_settings())
    service = _worker(_repository(), client)  # type: ignore[arg-type]
    created = service.start_job(
        _request().model_copy(update={"chat": True}),
        actor_user_uuid="user-1",
        actor_is_system_admin=True,
    )
    assert service.run_next_nl2sql_job(worker_id="tree-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None and job.status.value == "done", job
    trace_table.flush()

    rows = trace_table.db.spans(operation_id=created.job_id)
    [root] = [row for row in rows if row["is_root"]]
    assert root["name"] == "nl2sql.execute"
    # 工程は実行の全体の子で、順に並ぶ。
    assert [row["name"] for row in _children(rows, root)] == _STAGES
    [generate] = _children(rows, root, "nl2sql.generate_sql")
    # SQL の生成の LLM の呼び出しは、再試行も含めて生成の工程の子（試行ごとの兄弟）。
    chats = [row for row in rows if row["name"] == "gen_ai.chat"]
    assert len(chats) == 2 and len(calls) == 2
    assert {_attributes(row)["pr.gen_ai.purpose"] for row in chats} == {"sql.generate"}
    assert {row["parent_span_id"] for row in chats} == {generate["span_id"]}
    attempts = sorted(chats, key=lambda row: _attributes(row)["pr.attempt"])
    assert [(_attributes(row)["pr.attempt"], row["status"]) for row in attempts] == [
        (1, "failed"),
        (2, "done"),
    ]
    assert generate["status"] == "done"


class _LlmRequirementRuntime(_ReadyRequirementRuntime):
    """回答前の確認の下書きを LLM で作る（実の runtime と同じく `gen_ai.chat` の span を開く）。"""

    def create_session(self, request: Any, **kwargs: Any) -> Any:
        with observed_span("gen_ai.chat", kind="client"):
            return super().create_session(request, **kwargs)


class _UnconfiguredRequirementRuntime(_ReadyRequirementRuntime):
    """OCI Enterprise AI を設定していない構成（回答前の確認をせずに SQL を作る）。"""

    def create_session(self, request: Any, **kwargs: Any) -> Any:
        raise RequirementAiUnavailableError("REQUIREMENT_AI_NOT_CONFIGURED", "未設定")


def _query_from_agent(
    users: _Users,  # noqa: F811
    fake_service: _FakeNl2SqlService,  # noqa: F811
    trace_table: TraceTable,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Row], str]:
    """Agent（サービストークン・`params._meta` の traceparent）から nl2sql_query を呼ぶ。

    戻り値は書いた span と、ジョブを作った時点の今の span の id（ジョブの親になる span）。
    """

    job_parent: list[str] = []
    start_job = fake_service.start_job

    def traced_start_job(*args: Any, **kwargs: Any) -> Any:
        job_parent.append(capture_trace_context()["traceparent"].split("-")[2])
        return start_job(*args, **kwargs)

    monkeypatch.setattr(fake_service, "start_job", traced_start_job)
    user = users.create("mcp.tree", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    body = _call("nl2sql_query", {"question": "合成の一覧", "profile_id": "sales"})
    body["params"]["_meta"] = {"traceparent": _AGENT_TRACEPARENT}
    is_error, content = _structured(_run(body, token=_token(user.user_uuid)))
    assert is_error is False and content["status"] == "done", content
    trace_table.flush()
    [parent] = job_parent
    return list(trace_table.db.rows.values()), parent


def test_mcp_query_confirms_the_requirement_in_its_own_stage(
    users: _Users,  # noqa: F811
    fake_service: _FakeNl2SqlService,  # noqa: F811
    trace_table: TraceTable,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ontology_router, "ontology_runtime", _LlmRequirementRuntime())
    rows, job_parent = _query_from_agent(users, fake_service, trace_table, monkeypatch)

    [server] = [row for row in rows if row["name"] == "mcp.server"]
    # Agent の `tools/call` の mcp.request の下につながる。
    assert server["parent_span_id"] == "5" * 16
    [tool] = _children(rows, server)
    assert tool["name"] == "gen_ai.execute_tool"
    [requirement] = _children(rows, tool)
    assert requirement["name"] == "nl2sql.requirement"
    assert requirement["display_kind"] == REQUIREMENT_STAGE
    assert requirement["status"] == "done"
    # 別の製品の画面（Agent の実行の詳細）は、NL2SQL が付けた工程の名前で出す（#3272）。
    params = json.loads(requirement["display_params_json"] or "{}")
    assert params["display_name"] == MCP_STAGE_DISPLAY_NAMES[REQUIREMENT_STAGE] == "回答前の確認"
    # 確認の LLM の呼び出しは確認の工程の子。
    [chat] = [row for row in rows if row["name"] == "gen_ai.chat"]
    assert chat["parent_span_id"] == requirement["span_id"]
    # ジョブは確認の工程の外で作る（ジョブの root はツールの span の子。確認の工程の兄弟）。
    assert job_parent == tool["span_id"]


def test_mcp_query_without_the_requirement_ai_skips_the_stage(
    users: _Users,  # noqa: F811
    fake_service: _FakeNl2SqlService,  # noqa: F811
    trace_table: TraceTable,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ontology_router, "ontology_runtime", _UnconfiguredRequirementRuntime())
    rows, job_parent = _query_from_agent(users, fake_service, trace_table, monkeypatch)

    [server] = [row for row in rows if row["name"] == "mcp.server"]
    [tool] = _children(rows, server)
    assert tool["name"] == "gen_ai.execute_tool"
    [requirement] = _children(rows, tool)
    assert (requirement["name"], requirement["status"]) == ("nl2sql.requirement", "skipped")
    assert job_parent == tool["span_id"]
