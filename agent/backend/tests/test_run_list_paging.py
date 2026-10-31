"""実行履歴・承認の一覧のサーバー側のページングと要約（#1897）。

一覧は全 Run（step・Event・成果物）を返さず、行に要る項目の要約を `limit` / `offset` で返す。
承認の詳細は承認の ID からその Run だけを読む。repository は他のテストと共有するため、
業務 Agent の ID を一意にし、検索語でこのテストの Run だけに絞って確かめる。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunState,
    runtime_repository,
)
from app.features.agent.tools import ToolCall
from app.main import app

client = TestClient(app)
APPROVAL_TOOL = "nl2sql__nl2sql_query"


@dataclass
class PagingData:
    agent_id: str
    completed: list[RunState]
    waiting: RunState


def _run(agent_id: str, goal: str, *, approval: bool) -> RunState:
    run = runtime_repository.create_builtin_run(RunCreateRequest(goal=goal, agent_id=agent_id))
    if not approval:
        return run
    assert runtime_repository.begin_builtin_run(run.id) is not None
    return runtime_repository.request_builtin_approvals(
        run.id,
        [ToolCall(name=APPROVAL_TOOL, arguments={"question": goal}, trace_id="call-1")],
        state="{}",
    )


@pytest.fixture
def paging_data() -> Iterator[PagingData]:
    agent_id = f"agent-paging-{uuid4().hex[:12]}"
    runtime_repository.create_agent(AgentProfile(id=agent_id, name="ページング確認の業務"))
    try:
        completed = [_run(agent_id, f"一覧の確認 {index}", approval=False) for index in range(27)]
        waiting = _run(agent_id, "承認の確認 ＡＢＣ", approval=True)
        yield PagingData(agent_id=agent_id, completed=completed, waiting=waiting)
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(agent_id)


def _runs(**params: Any) -> dict[str, Any]:
    response = client.get("/api/runs", params=params)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def test_run_list_is_paged_summaries(paging_data: PagingData) -> None:
    first = _runs(q=paging_data.agent_id)
    assert (first["total"], first["limit"], first["offset"], first["has_next"]) == (28, 25, 0, True)
    items = first["items"]
    assert isinstance(items, list) and len(items) == 25
    # 新しい順。一覧の行は要約だけで、step・Event・成果物を持たない。
    assert items[0]["id"] == paging_data.waiting.id
    assert {"steps", "events", "artifacts", "approvals", "metadata"}.isdisjoint(items[0])
    assert items[0]["pending_approval_count"] == 1
    assert (items[0]["source"], items[0]["api_key_name"]) == ("user", None)
    assert items[0]["pending_approval"]["tool_name"] == APPROVAL_TOOL
    assert items[0]["pending_approval"]["id"] == paging_data.waiting.approvals[0].id
    assert isinstance(first["unfiltered_total"], int) and first["unfiltered_total"] >= 28

    second = _runs(q=paging_data.agent_id, offset=25)
    second_items = second["items"]
    assert isinstance(second_items, list) and len(second_items) == 3
    assert second["has_next"] is False
    listed = {item["id"] for item in [*items, *second_items]}
    assert listed == {run.id for run in [*paging_data.completed, paging_data.waiting]}


def test_run_list_filters_by_status_and_search(paging_data: PagingData) -> None:
    waiting = _runs(q=paging_data.agent_id, status="waiting_approval")
    assert [item["id"] for item in waiting["items"]] == [paging_data.waiting.id]
    # 「実行中・待機中」は queued / running / waiting_confirmation（作っただけの Run は queued）。
    active = _runs(q=paging_data.agent_id, status="active")
    assert active["total"] == 27
    # 検索は全角・半角と大文字・小文字を区別せず、空白区切りの語をすべて含む Run。
    # 業務 Agent の名前でも探せる。
    # 「abc」だけでは Run の ID（16 進）にも当たりうるので、goal にしか無い語を足す（#1952）。
    by_goal = _runs(q=f"{paging_data.agent_id} abc 承認")
    assert [item["id"] for item in by_goal["items"]] == [paging_data.waiting.id]
    by_name = _runs(q="ページング確認の業務 承認")
    assert paging_data.waiting.id in {item["id"] for item in by_name["items"]}

    assert client.get("/api/runs", params={"status": "unknown"}).status_code == 422
    assert client.get("/api/runs", params={"limit": 101}).status_code == 422


def test_run_list_filters_by_kind_period_and_waiting(paging_data: PagingData) -> None:
    """種類（きっかけ）・期間・応答待ちの絞り込み（実行履歴の共通の絞り込み。#1955）。"""
    # 応答待ち（承認待ち・確認待ち）。
    waiting = _runs(q=paging_data.agent_id, status="waiting")
    assert [item["id"] for item in waiting["items"]] == [paging_data.waiting.id]
    # 種類は `run_source`（画面で作った Run は user）。
    assert _runs(q=paging_data.agent_id, kind="user")["total"] == 28
    assert _runs(q=paging_data.agent_id, kind="automation")["total"] == 0
    assert client.get("/api/runs", params={"kind": "unknown"}).status_code == 422
    # 期間は `since` 以後に始めた Run（ISO 8601。タイムゾーンの無い値は UTC）。
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert _runs(q=paging_data.agent_id, since=past)["total"] == 28
    assert _runs(q=paging_data.agent_id, since=future)["total"] == 0
    assert client.get("/api/runs", params={"since": "yesterday"}).status_code == 422


def test_run_list_hides_evaluation_runs_with_not_evaluation(paging_data: PagingData) -> None:
    """品質評価の Run は `kind=not_evaluation`（画面の既定）で除き、`evaluation` で出す（#1983）。

    `metadata` は API では予約で渡せないので、repository で作る（品質評価と同じ）。
    """
    metadata = {"evaluation_job_id": f"job-{uuid4().hex[:8]}", "evaluation_case_id": "case-1"}
    evaluation = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="品質評価のケース", agent_id=paging_data.agent_id, metadata=metadata)
    )
    everything = _runs(q=paging_data.agent_id)
    assert everything["total"] == 29
    assert evaluation.id in {item["id"] for item in everything["items"]}

    default = _runs(q=paging_data.agent_id, kind="not_evaluation", limit=100)
    assert default["total"] == 28
    assert evaluation.id not in {item["id"] for item in default["items"]}
    only = _runs(q=paging_data.agent_id, kind="evaluation")
    listed = [(item["id"], item["source"]) for item in only["items"]]
    assert listed == [(evaluation.id, "evaluation")]

    # 「全 N 件」は既定の範囲（評価以外）で数え、「すべて」「評価」のときは評価も含める。
    # repository は他のテストと共有するので、差（= 見られる品質評価の Run の数）で確かめる。
    hidden = everything["evaluation_total"]
    assert hidden >= 1
    assert everything["unfiltered_total"] - default["unfiltered_total"] == hidden
    assert only["unfiltered_total"] == everything["unfiltered_total"]
    by_user = _runs(q=paging_data.agent_id, kind="user")
    assert by_user["unfiltered_total"] == default["unfiltered_total"]


def test_run_summary_has_duration_and_tokens(paging_data: PagingData) -> None:
    """終わった Run だけ終了日時と所要時間を持つ（実行中・待ちは None。#1955）。"""
    run = paging_data.completed[0]
    assert runtime_repository.begin_builtin_run(run.id) is not None
    runtime_repository.complete_builtin_run(run.id, "回答")
    rows = {item["id"]: item for item in _runs(q=paging_data.agent_id, limit=100)["items"]}
    done = rows[run.id]
    assert done["status"] == "completed"
    assert done["finished_at"] is not None and done["duration_ms"] >= 0
    assert "total_tokens" in done
    waiting = rows[paging_data.waiting.id]
    assert (waiting["finished_at"], waiting["duration_ms"]) == (None, None)


def test_approval_list_and_detail(paging_data: PagingData) -> None:
    approval = paging_data.waiting.approvals[0]
    pending = client.get("/api/approvals", params={"status": "pending", "q": approval.id})
    assert pending.status_code == 200, pending.text
    data = pending.json()["data"]
    assert data["total"] == 1
    [row] = data["items"]
    assert row == {
        **row,
        "id": approval.id,
        "run_id": paging_data.waiting.id,
        "tool_name": APPROVAL_TOOL,
        "status": "pending",
        "run_goal": "承認の確認 ＡＢＣ",
        "run_status": "waiting_approval",
        "agent_id": paging_data.agent_id,
    }
    assert "tool_call" not in row

    runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=False, decided_by="system_admin")
    )
    assert (
        client.get("/api/approvals", params={"status": "pending", "q": approval.id}).json()["data"][
            "total"
        ]
        == 0
    )
    decided = client.get("/api/approvals", params={"status": "decided", "q": approval.id})
    assert [item["status"] for item in decided.json()["data"]["items"]] == ["rejected"]

    detail = client.get(f"/api/approvals/{approval.id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["approval"]["tool_call"]["arguments"] == {"question": "承認の確認 ＡＢＣ"}
    assert body["approval"]["decided_by"] == "system_admin"
    assert body["run"]["id"] == paging_data.waiting.id
    assert "steps" not in body["run"]
    assert body["user_display_names"].get("system_admin")

    missing = client.get("/api/approvals/approval-missing")
    assert missing.status_code == 404
    assert missing.json()["error_messages"] == ["承認の依頼が見つかりません。"]
    assert client.get("/api/approvals", params={"status": "all"}).status_code == 422
