"""個人の業務 Agent と共有の申請（#2523）。

`agent.agents.build` だけを持つ利用者は、自分の業務 Agent を作り、試し、公開して自分で使える。
共有の申請を Agent 管理が承認すると組織の業務 Agent になり、ロールに割り当てて共有する。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
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

from app.features.agent.runtime import runtime_repository
from app.security.permissions import ADMIN, AGENTS_BUILD, RUNS_OPERATE
from app.security.service import set_security_service


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


@pytest.fixture
def scheduled(monkeypatch: MonkeyPatch) -> Iterator[list[str]]:
    """API の Run は作るだけで実行しない。作った業務 Agent と Run は後で消す。"""
    runs: list[str] = []
    monkeypatch.setattr(
        "app.features.agent.router._schedule_builtin_run", lambda run: runs.append(run.id)
    )
    before = {agent.id for agent in runtime_repository.list_agents()}
    try:
        yield runs
    finally:
        created = {agent.id for agent in runtime_repository.list_agents()} - before
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id in created
            ]:
                repository._runs.pop(run_id)
        for agent_id in created:
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)


def _create(headers: dict[str, str], agent_id: str) -> Any:
    return client.post(
        "/api/agents",
        json={"id": agent_id, "name": f"Agent {agent_id}", "instructions": "v1"},
        headers=headers,
    )


def _agent_ids(headers: dict[str, str]) -> set[str]:
    response = client.get("/api/agents", headers=headers)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]["agents"]}


def test_builder_owns_and_manages_own_agent(auth: ProductionAuth, scheduled: list[str]) -> None:
    builder = auth.user_with_permissions("builder-2523", [AGENTS_BUILD])
    auth.user_with_permissions("other-2523", [AGENTS_BUILD])
    mine, other = login("builder-2523"), login("other-2523")

    created = _create(mine, "personal-2523")
    assert created.status_code == 200, created.text
    assert created.json()["data"]["owner_user_uuid"] == builder.user_uuid
    # 持ち主はロールに割り当てなくても使える。ほかの利用者には見えない。
    assert "personal-2523" in _agent_ids(mine)
    assert "personal-2523" not in _agent_ids(other)
    assert (
        client.patch(
            "/api/agents/personal-2523", json={"instructions": "v2"}, headers=other
        ).status_code
        == 404
    )

    patched = client.patch("/api/agents/personal-2523", json={"instructions": "v2"}, headers=mine)
    assert patched.status_code == 200, patched.text
    draft = client.post(
        "/api/runs", json={"goal": "試す", "agent_id": "personal-2523", "draft": True}, headers=mine
    )
    assert draft.status_code == 200, draft.text
    assert client.get("/api/threads", params={"draft": "true"}, headers=mine).status_code == 200
    published = client.post("/api/agents/personal-2523/publish", json={}, headers=mine)
    assert published.status_code == 200, published.text
    run = client.post("/api/runs", json={"goal": "使う", "agent_id": "personal-2523"}, headers=mine)
    assert run.status_code == 200, run.text

    # 管理者は個人の Agent も見て、持ち主の表示名を読める。
    admin = login_configured_admin()
    listed = client.get("/api/agents", headers=admin).json()["data"]
    assert "personal-2523" in {item["id"] for item in listed["agents"]}
    assert listed["owner_display_names"][builder.user_uuid] == "builder-2523"

    assert client.delete("/api/agents/personal-2523", headers=mine).status_code == 200


def test_builder_cannot_change_organization_agents(
    auth: ProductionAuth, scheduled: list[str]
) -> None:
    auth.user_with_permissions("builder-org-2523", [AGENTS_BUILD], agent_ids=["default"])
    headers = login("builder-org-2523")

    patched = client.patch("/api/agents/default", json={"instructions": "x"}, headers=headers)
    draft = client.post(
        "/api/runs", json={"goal": "試す", "agent_id": "default", "draft": True}, headers=headers
    )

    assert patched.status_code == 403
    assert "Agent 管理か持ち主" in patched.json()["error_messages"][0]
    assert draft.status_code == 403
    assert client.post("/api/agents/default/publish", json={}, headers=headers).status_code == 403


def test_users_without_build_cannot_create(auth: ProductionAuth) -> None:
    auth.user_with_permissions("operator-2523", [RUNS_OPERATE], agent_ids=["default"])
    assert _create(login("operator-2523"), "denied-2523").status_code == 403


def test_admin_created_agents_are_organization_agents(
    auth: ProductionAuth, scheduled: list[str]
) -> None:
    auth.user_with_permissions("admin-2523", [ADMIN])
    created = _create(login("admin-2523"), "org-2523")
    assert created.status_code == 200, created.text
    assert created.json()["data"]["owner_user_uuid"] is None


def _put_access(role_id: str, version: int, agent_ids: list[str]) -> Any:
    return client.put(
        f"/api/security/roles/{role_id}/access",
        json={"version": version, "permissions": ["agent.runs.operate"], "agent_ids": agent_ids},
        headers=login_configured_admin(),
    )


def test_share_request_approval_makes_organization_agent(
    auth: ProductionAuth, scheduled: list[str]
) -> None:
    builder = auth.user_with_permissions("builder-share-2523", [AGENTS_BUILD])
    mine = login("builder-share-2523")
    admin = login_configured_admin()
    assert _create(mine, "share-2523").status_code == 200

    # 公開してから申請する。
    early = client.post("/api/agents/share-2523/share-request", json={}, headers=mine)
    assert early.status_code == 409
    assert client.post("/api/agents/share-2523/publish", json={}, headers=mine).status_code == 200

    # 承認する前は、ロールに割り当てられず、権限管理の候補にも出ない。
    role = auth.create_role([])
    refused = _put_access(role.role_id, 1, ["share-2523"])
    assert refused.status_code == 409, refused.text
    assert "共有の申請を承認してから" in refused.json()["error_messages"][0]
    targets = client.get("/api/security/access-targets/agents", headers=admin).json()["data"]
    assert "share-2523" not in {item["id"] for item in targets["items"]}

    requested = client.post(
        "/api/agents/share-2523/share-request", json={"note": "部署で使いたい"}, headers=mine
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["data"]["share_request"]["status"] == "requested"
    again = client.post("/api/agents/share-2523/share-request", json={}, headers=mine)
    assert again.status_code == 409
    # 承認は Agent 管理だけ。
    assert (
        client.post("/api/agents/share-2523/share-request/approve", headers=mine).status_code == 403
    )

    approved = client.post("/api/agents/share-2523/share-request/approve", headers=admin)
    assert approved.status_code == 200, approved.text
    data = approved.json()["data"]
    assert data["owner_user_uuid"] is None
    assert data["share_request"]["status"] == "approved"
    assert data["share_request"]["requested_by_user_uuid"] == builder.user_uuid
    assert data["share_request"]["note"] == ""
    assert "share-2523" not in _agent_ids(mine)
    notices = client.get("/api/agents", headers=mine).json()["data"]["share_approvals"]
    assert len(notices) == 1
    assert set(notices[0]) == {"agent_id", "name", "decided_at"}
    assert notices[0]["agent_id"] == "share-2523"
    assert notices[0]["name"] == "Agent share-2523"
    auth.user_with_permissions("other-share-notice", [AGENTS_BUILD])
    other = login("other-share-notice")
    assert client.get("/api/agents", headers=other).json()["data"]["share_approvals"] == []
    # 組織の業務 Agent になったので、元の持ち主は変えられず（ロールが無ければ使えない）、
    # ロールに割り当てられる。
    assert (
        client.patch("/api/agents/share-2523", json={"instructions": "x"}, headers=mine).status_code
        == 404
    )
    assert _put_access(role.role_id, 1, ["share-2523"]).status_code == 200


def test_share_request_rejection_returns_reason(auth: ProductionAuth, scheduled: list[str]) -> None:
    builder = auth.user_with_permissions("builder-reject-2523", [AGENTS_BUILD])
    mine = login("builder-reject-2523")
    admin = login_configured_admin()
    assert _create(mine, "reject-2523").status_code == 200
    assert client.post("/api/agents/reject-2523/publish", json={}, headers=mine).status_code == 200
    assert (
        client.post("/api/agents/reject-2523/share-request", json={}, headers=mine).status_code
        == 200
    )

    empty = client.post(
        "/api/agents/reject-2523/share-request/reject", json={"reason": ""}, headers=admin
    )
    assert empty.status_code == 422
    rejected = client.post(
        "/api/agents/reject-2523/share-request/reject",
        json={"reason": "評価の問いを足してください"},
        headers=admin,
    )

    assert rejected.status_code == 200, rejected.text
    data = rejected.json()["data"]
    assert data["owner_user_uuid"] == builder.user_uuid
    assert data["share_request"]["status"] == "rejected"
    assert data["share_request"]["reason"] == "評価の問いを足してください"
    # 却下の後は、直して申請し直せる。
    assert (
        client.post("/api/agents/reject-2523/share-request", json={}, headers=mine).status_code
        == 200
    )


def test_owner_can_evaluate_own_draft(auth: ProductionAuth, scheduled: list[str]) -> None:
    """持ち主は自分の Agent の下書きを評価できる（#2522 の判定に持ち主を足す）。"""
    from app.features.agent.evaluation import evaluation_set_store, evaluation_store

    auth.user_with_permissions("builder-eval-2523", [AGENTS_BUILD])
    mine = login("builder-eval-2523")
    assert _create(mine, "eval-2523").status_code == 200
    set_response = client.post(
        "/api/evaluation-sets",
        json={
            "agent_id": "eval-2523",
            "name": "評価セット A",
            "description": "",
            "cases": [{"question": "質問", "expected": "要点"}],
        },
        headers=mine,
    )
    assert set_response.status_code == 200, set_response.text
    try:
        started = client.post(
            "/api/evaluations",
            json={"set_id": set_response.json()["data"]["id"], "agent_version": "draft"},
            headers=mine,
        )
        assert started.status_code == 202, started.text
    finally:
        evaluation_store.clear()
        evaluation_set_store.clear()
