"""Cookie のセッションの利用者から作る ActorPolicy の絞り込みテスト（#215）。

- Run・監査・承認・成果物・SSE・`GET /agents`・
Binding 一覧が、利用者のエージェント・検索・回答プロファイルで

  絞られること（header の RBAC 情報は使わない）
- 承認の決定者（decided_by）が利用者になること
- 「AI 活用」の Run（実行履歴・承認・詳細・操作）は、system_admin は全員、それ以外は自分の Run
  だけ（ほかの利用者の Run は 404。#1889）。範囲の判定は自分の Run に重ねてかかる。system_admin が
  他の利用者の会話で操作する流れは test_admin_operates_others.py（#1928）
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from pytest import MonkeyPatch
from security_support import (
    ProductionAuth,
    client,
    enable_production_auth,
    login,
    login_configured_admin,
)

from app.features.agent import builtin_runtime
from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunState,
    runtime_repository,
)
from app.features.agent.tools import ToolCall
from app.features.agent.user_names import login_display_names, user_display_names
from app.security.domain import CONFIGURED_SYSTEM_ADMIN_USER_UUID
from app.security.service import set_security_service

AGENT_A = "agent-scope-a-215"
AGENT_B = "agent-scope-b-215"
APPROVAL_TOOL = "nl2sql__nl2sql_query"


@dataclass
class ScopeData:
    run_a1: RunState  # agent A / bv-a（承認待ち）
    # agent A / bv-b。検索・回答プロファイルは Agen
    # t の対象範囲ではない（RAG が判定する。#750）ため見える。
    #
    run_a2: RunState
    run_b: RunState  # agent B / bv-a
    run_a_no_view: RunState  # agent A / 検索・回答プロファイルなし


def _create_run(agent_id: str, search_answer_profile_id: str | None, *, approval: bool) -> RunState:
    metadata = (
        {"search_answer_profile_id": search_answer_profile_id} if search_answer_profile_id else {}
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal=f"scope {agent_id} {search_answer_profile_id}",
            agent_id=agent_id,
            metadata=metadata,
        )
    )
    if not approval:
        return run
    # 組み込み Runtime がツールの承認で中断したときと同じ状態にする（モデルは呼ばない）。
    assert runtime_repository.begin_builtin_run(run.id) is not None
    return runtime_repository.request_builtin_approvals(
        run.id,
        [ToolCall(name=APPROVAL_TOOL, arguments={"question": "範囲を確認する"}, trace_id="call-1")],
        state="{}",
    )


@pytest.fixture
def scope_data() -> Iterator[ScopeData]:
    for agent_id in (AGENT_A, AGENT_B):
        runtime_repository.create_agent(AgentProfile(id=agent_id, name=agent_id))
    try:
        yield ScopeData(
            run_a1=_create_run(AGENT_A, "bv-a", approval=True),
            run_a2=_create_run(AGENT_A, "bv-b", approval=True),
            run_b=_create_run(AGENT_B, "bv-a", approval=True),
            run_a_no_view=_create_run(AGENT_A, None, approval=False),
        )
    finally:
        for agent_id in (AGENT_A, AGENT_B):
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def _scoped_user(
    auth: ProductionAuth,
    login_user_id: str,
    permissions: list[str],
    *,
    owns: ScopeData | None = None,
) -> str:
    """業務 Agent A だけを使える利用者。`owns` の Run をこの利用者の Run にする（#1889）。"""
    user = auth.user_with_permissions(login_user_id, permissions, agent_ids=[AGENT_A])
    if owns is not None:
        _own(owns, user.user_uuid)
    return user.user_uuid


def _own(scope_data: ScopeData, user_uuid: str) -> None:
    """scope_data の Run を `user_uuid` の Run にする（「AI 活用」は自分の Run だけ。#1889）。"""
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - テスト用に持ち主だけを変える
        for run in (
            scope_data.run_a1,
            scope_data.run_a2,
            scope_data.run_b,
            scope_data.run_a_no_view,
        ):
            repository._runs[run.id].created_by_user_uuid = user_uuid  # noqa: SLF001


def _all_pages(path: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """一覧の全ページ（#1897。repository は同じプロセスのテストと共有するので、件数に依らず読む）。

    各ページの `creator_display_names` は、そのページの行に `creator_display_names` として写す。
    """
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(path, params={"limit": 100, "offset": offset}, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        names = data.get("creator_display_names", {})
        items.extend({**item, "creator_display_names": names} for item in data["items"])
        if not data["has_next"]:
            return items
        offset += data["limit"]


def _run_ids(headers: dict[str, str]) -> set[str]:
    return {item["id"] for item in _all_pages("/api/runs", headers)}


def _approval_ids(headers: dict[str, str]) -> set[str]:
    return {item["id"] for item in _all_pages("/api/approvals", headers)}


def test_run_list_and_detail_are_scoped_by_principal(
    auth: ProductionAuth, scope_data: ScopeData
) -> None:
    _scoped_user(auth, "scoped-viewer", ["agent.runs.view"], owns=scope_data)
    # header の自己申告（全ロール・全検索・回答プロファイル
    # ）は使わない（header の RBAC は無い。#750）。
    #
    headers = {
        **login("scoped-viewer"),
        "X-Agent-Roles": "admin",
        "X-Agent-Business-Views": "*",
    }
    ids = _run_ids(headers)
    assert scope_data.run_a1.id in ids
    assert scope_data.run_a_no_view.id in ids
    assert scope_data.run_a2.id in ids
    assert scope_data.run_b.id not in ids

    assert client.get(f"/api/runs/{scope_data.run_a1.id}", headers=headers).status_code == 200
    # 承認の一覧・詳細も同じ対象範囲（#1897）。範囲外の承認は詳細も 404。
    approval_ids = _approval_ids(headers)
    assert scope_data.run_a1.approvals[0].id in approval_ids
    assert scope_data.run_b.approvals[0].id not in approval_ids
    in_scope = client.get(f"/api/approvals/{scope_data.run_a1.approvals[0].id}", headers=headers)
    assert in_scope.status_code == 200, in_scope.text
    out_of_scope = client.get(f"/api/approvals/{scope_data.run_b.approvals[0].id}", headers=headers)
    assert out_of_scope.status_code == 404
    for run in (scope_data.run_b,):
        assert client.get(f"/api/runs/{run.id}", headers=headers).status_code == 403
        artifacts = client.get(f"/api/runs/{run.id}/artifacts", headers=headers)
        assert artifacts.status_code == 403
        events = client.get(f"/api/runs/{run.id}/events", headers=headers)
        assert events.status_code == 403
    allowed_events = client.get(f"/api/runs/{scope_data.run_a1.id}/events", headers=headers)
    assert allowed_events.status_code == 200
    assert allowed_events.headers["content-type"].startswith("text/event-stream")
    assert (
        client.get(f"/api/runs/{scope_data.run_a1.id}/artifacts", headers=headers).status_code
        == 200
    )
    # viewer は操作できない（header の admin も効かない）。
    cancel = client.post(f"/api/runs/{scope_data.run_a1.id}/cancel", headers=headers)
    assert cancel.status_code == 403

    # 同じ範囲の、ほかの利用者の Run は一覧に出さず、詳細・成果物・イベントは 404（#1889）。
    _scoped_user(auth, "scoped-peer", ["agent.runs.view", "agent.runs.operate"])
    peer = login("scoped-peer")
    assert _run_ids(peer) == set()
    for path in ("", "/artifacts", "/events", "/trace"):
        assert (
            client.get(f"/api/runs/{scope_data.run_a1.id}{path}", headers=peer).status_code == 404
        )
    for action in ("cancel", "resume", "replay"):
        response = client.post(f"/api/runs/{scope_data.run_a1.id}/{action}", headers=peer)
        assert response.status_code == 404, action


def test_only_system_admin_sees_all_runs(auth: ProductionAuth, scope_data: ScopeData) -> None:
    """全員の Run を見られるのは system_admin だけ（`agent.admin` は理由にしない。#1889）。"""
    auth.user_with_permissions("agent-admin", ["agent.admin"])
    expected = {
        scope_data.run_a1.id,
        scope_data.run_a2.id,
        scope_data.run_b.id,
        scope_data.run_a_no_view.id,
    }
    assert not expected & _run_ids(login("agent-admin"))
    admin = login_configured_admin()
    assert expected <= _run_ids(admin)
    runs = _all_pages("/api/runs", admin)
    # 持ち主の分からない古い Run（created_by なし）は表示名の対応表に入らない（画面は削除済み）。
    assert all(run["created_by_user_uuid"] is None for run in runs if run["id"] in expected)
    # 一般の利用者には「利用者」の対応表を返さない。
    owner = _scoped_user(auth, "owner-1889", ["agent.runs.view"], owns=scope_data)
    auth.store.users[owner].display_name = "持ち主 太郎"
    assert all(
        run["creator_display_names"] == {} for run in _all_pages("/api/runs", login("owner-1889"))
    )
    # 対応表はページの行の分（#1897）。この利用者の Run の行のページで確かめる。
    owned = [run for run in _all_pages("/api/runs", admin) if run["id"] in expected]
    assert owned and all(run["creator_display_names"][owner] == "持ち主 太郎" for run in owned)


def test_audit_is_scoped_by_principal(auth: ProductionAuth, scope_data: ScopeData) -> None:
    _scoped_user(auth, "scoped-auditor", ["agent.audit.view"], owns=scope_data)
    headers = login("scoped-auditor")
    audit = client.get(f"/api/runs/{scope_data.run_a1.id}/audit", headers=headers)
    assert audit.status_code == 200
    assert client.get(f"/api/runs/{scope_data.run_b.id}/audit", headers=headers).status_code == 403
    records = client.get("/api/audit/tool-calls?limit=1000", headers=headers)
    assert records.status_code == 200
    run_ids = {item["run_id"] for item in records.json()["data"]["items"]}
    assert scope_data.run_a1.id in run_ids
    assert scope_data.run_b.id not in run_ids
    csv = client.get("/api/audit/tool-calls.csv?limit=5000", headers=headers)
    assert csv.status_code == 200
    assert scope_data.run_b.id not in csv.text


def test_audit_tool_names_are_scoped_by_principal(
    auth: ProductionAuth, scope_data: ScopeData
) -> None:
    """ツール名の選択肢にも、対象範囲の外の業務 Agent の Run のツール名を出さない（#983）。"""
    other_tool = "scope_b_only__lookup"
    run = runtime_repository.create_builtin_run(RunCreateRequest(goal="scope b", agent_id=AGENT_B))
    assert runtime_repository.begin_builtin_run(run.id) is not None
    runtime_repository.request_builtin_approvals(
        run.id, [ToolCall(name=other_tool, arguments={}, trace_id="call-b")], state="{}"
    )
    _scoped_user(auth, "scoped-auditor-names", ["agent.audit.view"])

    records = client.get("/api/audit/tool-calls", headers=login("scoped-auditor-names"))

    assert records.status_code == 200
    tool_names = records.json()["data"]["tool_names"]
    assert APPROVAL_TOOL in tool_names
    assert other_tool not in tool_names
    admin = client.get("/api/audit/tool-calls", headers=login_configured_admin())
    assert other_tool in admin.json()["data"]["tool_names"]
    del scope_data


def test_approval_is_scoped_and_decided_by_principal(
    auth: ProductionAuth, scope_data: ScopeData
) -> None:
    # 承認の権限を持つ人も、同じ範囲のほかの利用者の Run の承認は判断できない（404。#1889）。
    _scoped_user(auth, "peer-approver", ["agent.approvals.decide"])
    peer = client.post(
        f"/api/approvals/{scope_data.run_a1.approvals[0].id}/decision",
        json={"approved": True},
        headers=login("peer-approver"),
    )
    assert peer.status_code == 404
    assert peer.json()["error_messages"] == ["承認の依頼が見つかりません。"]

    _scoped_user(auth, "scoped-approver", ["agent.approvals.decide"], owns=scope_data)
    headers = login("scoped-approver")
    out_of_scope = scope_data.run_b.approvals[0].id
    denied = client.post(
        f"/api/approvals/{out_of_scope}/decision",
        json={"approved": False, "decided_by": "spoofed"},
        headers=headers,
    )
    assert denied.status_code == 403
    assert denied.json()["error_messages"] == ["この業務 Agent を利用する権限がありません。"]
    missing = client.post(
        "/api/approvals/approval-missing/decision", json={"approved": True}, headers=headers
    )
    assert missing.status_code == 404
    assert missing.json()["error_messages"] == ["承認の依頼が見つかりません。"]
    approval_id = scope_data.run_a1.approvals[0].id
    decided = client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"approved": False, "decided_by": "spoofed-user", "comment": "範囲内"},
        headers=headers,
    )
    assert decided.status_code == 200, decided.text
    approval = next(
        item for item in decided.json()["data"]["approvals"] if item["id"] == approval_id
    )
    # body の decided_by ではなく、ログイン中の利用者が決定者になる。
    assert approval["decided_by"] == "scoped-approver"


def test_operator_run_creation_is_scoped(auth: ProductionAuth, scope_data: ScopeData) -> None:
    _scoped_user(auth, "scoped-operator", ["agent.runs.operate"])
    headers = login("scoped-operator")
    denied_agent = client.post(
        "/api/runs",
        json={
            "goal": "範囲外",
            "agent_id": AGENT_B,
            "metadata": {"search_answer_profile_id": "bv-a"},
        },
        headers=headers,
    )
    assert denied_agent.status_code == 403
    # チャットの送信の失敗にそのまま出るので、英語・内部の ID を出さない。
    assert denied_agent.json()["error_messages"] == ["この業務 Agent を利用する権限がありません。"]
    created = client.post(
        "/api/runs",
        json={
            "goal": "範囲内",
            "agent_id": AGENT_A,
            "metadata": {"search_answer_profile_id": "bv-a"},
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text


@pytest.mark.parametrize(
    "metadata",
    [
        # 承認待ちから再開する SDK の状態。偽ると、最初からの実行ではなく再開になる（#1130）。
        {"_builtin_sdk_state": "{}"},
        {"_runtime_dispatch_lease": {"owner": "x"}},
        {"evaluation_dry_run": True},
        {"automation_id": "automation-of-another-user"},
        {"source": "mcp", "search_answer_profile_id": "bv-a"},
    ],
    ids=["sdk-state", "dispatch-lease", "evaluation-dry-run", "automation-id", "source"],
)
def test_run_creation_rejects_reserved_metadata(
    auth: ProductionAuth,
    scope_data: ScopeData,
    monkeypatch: MonkeyPatch,
    metadata: dict[str, object],
) -> None:
    """利用者は Control Plane の予約の metadata を付けて Run を作れない（#1130）。"""
    scheduled: list[str] = []

    async def record(run_id: str) -> None:
        scheduled.append(run_id)

    monkeypatch.setattr(builtin_runtime, "resume_run", record)
    monkeypatch.setattr(builtin_runtime, "execute_run", record)
    _scoped_user(auth, "reserved-metadata-operator", ["agent.runs.operate"])
    headers = login("reserved-metadata-operator")
    before = {run.id for run in runtime_repository.list_runs()}

    response = client.post(
        "/api/runs",
        json={"goal": "予約の metadata", "agent_id": AGENT_A, "metadata": metadata},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "metadata" in response.json()["error_messages"][0]
    assert {run.id for run in runtime_repository.list_runs()} == before
    assert scheduled == []


def test_run_creation_keeps_unreserved_metadata(
    auth: ProductionAuth, scope_data: ScopeData, monkeypatch: MonkeyPatch
) -> None:
    async def record(run_id: str) -> None:
        return None

    monkeypatch.setattr(builtin_runtime, "execute_run", record)
    _scoped_user(auth, "unreserved-metadata-operator", ["agent.runs.operate"])
    headers = login("unreserved-metadata-operator")

    response = client.post(
        "/api/runs",
        json={
            "goal": "予約していない metadata",
            "agent_id": AGENT_A,
            "metadata": {"search_answer_profile_id": "bv-a", "ticket": "T-1"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    metadata = response.json()["data"]["metadata"]
    assert metadata["search_answer_profile_id"] == "bv-a"
    assert metadata["ticket"] == "T-1"


def test_agent_list_is_scoped(auth: ProductionAuth, scope_data: ScopeData) -> None:
    _scoped_user(auth, "scoped-agents", ["agent.runs.operate", "menu.agents"])
    headers = login("scoped-agents")
    agents = client.get("/api/agents", headers=headers)
    assert agents.status_code == 200
    assert [item["id"] for item in agents.json()["data"]["agents"]] == [AGENT_A]

    admin = login_configured_admin()
    all_agents = {
        item["id"] for item in client.get("/api/agents", headers=admin).json()["data"]["agents"]
    }
    assert {AGENT_A, AGENT_B, "default"} <= all_agents


def test_trace_events_are_scoped_for_restricted_principal(
    auth: ProductionAuth, scope_data: ScopeData
) -> None:
    from app.observability import record_runtime_event

    for run in (scope_data.run_a1, scope_data.run_b):
        record_runtime_event("run.scope_check", {"run_id": run.id, "scope": "215"})
    _scoped_user(auth, "trace-auditor", ["agent.audit.view"])
    headers = login("trace-auditor")
    response = client.get("/api/observability/events?limit=1000", headers=headers)
    assert response.status_code == 200
    run_ids = {item["run_id"] for item in response.json()["data"]["events"]}
    assert scope_data.run_b.id not in run_ids
    assert None not in run_ids
    admin = login_configured_admin()
    all_ids = {
        item["run_id"]
        for item in client.get("/api/observability/events?limit=1000", headers=admin).json()[
            "data"
        ]["events"]
    }
    assert scope_data.run_b.id in all_ids


def test_approval_decider_and_version_publisher_are_shown_by_display_name(
    auth: ProductionAuth, scope_data: ScopeData
) -> None:
    """判断者・公開者はログイン ID で保存し、一覧の応答は表示名を添える（#1696）。

    構成のシステム管理者は「システム管理者」、引けない利用者と記号（`system`）は対応表に入れない
    （画面は UUID・ログイン ID を出さずに「（削除済みのユーザー）」「システム（自動）」と出す）。
    """
    approver = auth.user_with_permissions(
        "named-approver", ["agent.approvals.decide"], agent_ids=[AGENT_A]
    )
    auth.store.users[approver.user_uuid].display_name = "承認 花子"
    _own(scope_data, approver.user_uuid)
    approval_id = scope_data.run_a1.approvals[0].id
    decided = client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"approved": True},
        headers=login("named-approver"),
    )
    assert decided.status_code == 200, decided.text
    # 削除済みの利用者・自動の処理の判断は表示名を引けない。
    gone = scope_data.run_a2.approvals[0]
    runtime_repository.decide_approval(
        gone.id, ApprovalDecisionRequest(approved=False, decided_by="deleted.user")
    )

    admin = login_configured_admin()
    detail = client.get(f"/api/approvals/{approval_id}", headers=admin)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    # 保存するのはログイン ID のまま。
    assert data["approval"]["decided_by"] == "named-approver"
    assert data["user_display_names"]["named-approver"] == "承認 花子"
    gone_detail = client.get(f"/api/approvals/{gone.id}", headers=admin).json()["data"]
    assert gone_detail["approval"]["decided_by"] == "deleted.user"
    assert "deleted.user" not in gone_detail["user_display_names"]

    published = client.post(f"/api/agents/{AGENT_A}/publish", json={}, headers=admin)
    assert published.status_code == 200, published.text
    assert published.json()["data"]["versions"][-1]["published_by"] == "system_admin"
    agents = client.get("/api/agents", headers=admin).json()["data"]
    assert agents["user_display_names"] == {"system_admin": "システム管理者"}

    # UUID で記録した利用者も、構成のシステム管理者を表示名で引ける。
    names = user_display_names([CONFIGURED_SYSTEM_ADMIN_USER_UUID, approver.user_uuid, "missing"])
    assert names == {
        CONFIGURED_SYSTEM_ADMIN_USER_UUID: "システム管理者",
        approver.user_uuid: "承認 花子",
    }
    assert login_display_names(["system_admin", "named-approver", "system", "gone"]) == {
        "system_admin": "システム管理者",
        "named-approver": "承認 花子",
    }


def test_chat_threads_are_own_and_system_admin_sees_everyone(
    auth: ProductionAuth, scope_data: ScopeData
) -> None:
    """チャットの会話も「AI 活用」の規則（#1889）。

    system_admin は全員の会話を利用者つきで見られるが、他の利用者の会話の名前の変更・続きの質問は
    403（持ち主だけ。#3244。詳細は test_admin_operates_others.py）。ほかの利用者の会話は 404。
    """
    alice = _scoped_user(auth, "thread-alice", ["agent.runs.operate"])
    auth.store.users[alice].display_name = "アリス"
    _scoped_user(auth, "thread-bob", ["agent.runs.operate"])
    thread_id = scope_data.run_a_no_view.thread_id
    assert thread_id is not None
    _own(scope_data, alice)

    def thread_ids(headers: dict[str, str]) -> set[str]:
        response = client.get("/api/threads?limit=100", headers=headers)
        assert response.status_code == 200, response.text
        return {item["thread_id"] for item in response.json()["data"]["items"]}

    assert thread_id in thread_ids(login("thread-alice"))
    bob = login("thread-bob")
    assert thread_id not in thread_ids(bob)
    assert client.get(f"/api/threads/{thread_id}", headers=bob).status_code == 404
    renamed_by_bob = client.patch(f"/api/threads/{thread_id}", json={"title": "x"}, headers=bob)
    assert renamed_by_bob.status_code == 404
    assert client.delete(f"/api/threads/{thread_id}", headers=bob).status_code == 404

    admin = login_configured_admin()
    listed = client.get("/api/threads?limit=100", headers=admin).json()["data"]["items"]
    [item] = [entry for entry in listed if entry["thread_id"] == thread_id]
    assert (item["owned"], item["actor_display_name"]) == (False, "アリス")
    assert "owner_user_uuid" not in item
    detail = client.get(f"/api/threads/{thread_id}", headers=admin).json()["data"]
    assert (detail["owned"], detail["actor_display_name"]) == (False, "アリス")
    own = client.get(f"/api/threads/{thread_id}", headers=login("thread-alice")).json()["data"]
    assert (own["owned"], own["actor_display_name"]) == (True, "")
    renamed = client.patch(f"/api/threads/{thread_id}", json={"title": "確認済み"}, headers=admin)
    assert renamed.status_code == 403, renamed.text
    by_owner = client.patch(
        f"/api/threads/{thread_id}", json={"title": "確認済み"}, headers=login("thread-alice")
    )
    assert by_owner.status_code == 200, by_owner.text
    assert by_owner.json()["data"]["title"] == "確認済み"
    # system_admin でない利用者は他人の会話を続けられない（404）。
    by_bob = client.post(
        "/api/runs",
        json={"goal": "続き", "agent_id": AGENT_A, "thread_id": thread_id},
        headers=bob,
    )
    assert by_bob.status_code == 404
