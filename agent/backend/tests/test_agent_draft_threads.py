"""業務 Agent の編集画面で下書きを試す会話（#2515）。

会話は始めた Run の種類（下書き / 公開中の版）に固定し、チャットの会話の履歴には
公開中の版の会話だけ、編集画面の「試す」の履歴には下書きの会話だけを出す。
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
)

from app.features.agent.runtime import (
    RunCreateRequest,
    ThreadKindMismatchError,
    runtime_repository,
)
from app.security.permissions import MENU_CHAT, MENU_RUNS, RUNS_OPERATE
from app.security.service import set_security_service

AGENT_ID = "agent-draft-threads-2515"


@pytest.fixture
def scheduled(monkeypatch: MonkeyPatch) -> Iterator[list[str]]:
    """API の Run は作るだけで実行しない。"""
    runs: list[str] = []
    monkeypatch.setattr(
        "app.features.agent.router._schedule_builtin_run", lambda run: runs.append(run.id)
    )
    try:
        yield runs
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)


def _published_agent() -> None:
    created = client.post(
        "/api/agents", json={"id": AGENT_ID, "name": "Agent A", "instructions": "v1 の指示"}
    )
    assert created.status_code == 200, created.text
    assert client.post(f"/api/agents/{AGENT_ID}/publish", json={}).status_code == 200


def _thread_ids(*, draft: bool) -> list[str]:
    response = client.get(
        "/api/threads", params={"agent_id": AGENT_ID, **({"draft": "true"} if draft else {})}
    )
    assert response.status_code == 200, response.text
    return [item["thread_id"] for item in response.json()["data"]["items"]]


def test_draft_and_published_conversations_are_listed_separately(scheduled: list[str]) -> None:
    _published_agent()
    draft = client.post("/api/runs", json={"goal": "試す", "agent_id": AGENT_ID, "draft": True})
    assert draft.status_code == 200, draft.text
    draft_thread = draft.json()["data"]["thread_id"]
    published = client.post("/api/runs", json={"goal": "質問", "agent_id": AGENT_ID})
    assert published.status_code == 200, published.text
    published_thread = published.json()["data"]["thread_id"]

    # 下書きの会話は続けて試せる（同じ種類の続き）。
    follow_up = client.post(
        "/api/runs",
        json={"goal": "続き", "agent_id": AGENT_ID, "draft": True, "thread_id": draft_thread},
    )
    assert follow_up.status_code == 200, follow_up.text

    assert _thread_ids(draft=False) == [published_thread]
    assert _thread_ids(draft=True) == [draft_thread]
    # 下書きの会話も、持ち主は会話を開ける。
    opened = client.get(f"/api/threads/{draft_thread}")
    assert opened.status_code == 200, opened.text
    assert [run["goal"] for run in opened.json()["data"]["runs"]] == ["試す", "続き"]


@pytest.mark.parametrize(
    ("start_draft", "continue_draft"),
    [(True, False), (False, True)],
    ids=["draft-thread-published-run", "published-thread-draft-run"],
)
def test_conversation_kind_cannot_be_mixed(
    scheduled: list[str], start_draft: bool, continue_draft: bool
) -> None:
    _published_agent()
    first = client.post(
        "/api/runs", json={"goal": "最初", "agent_id": AGENT_ID, "draft": start_draft}
    )
    assert first.status_code == 200, first.text
    thread_id = first.json()["data"]["thread_id"]

    mixed = client.post(
        "/api/runs",
        json={
            "goal": "続き",
            "agent_id": AGENT_ID,
            "draft": continue_draft,
            "thread_id": thread_id,
        },
    )

    assert mixed.status_code == 409, mixed.text
    assert "新しい会話で質問してください" in mixed.json()["error_messages"][0]
    # 断った続きは Run を作らない。
    assert scheduled == [first.json()["data"]["id"]]


def test_repository_treats_other_kind_thread_as_missing(scheduled: list[str]) -> None:
    """画面以外の呼び出し（MCP など）には、種類の違う会話は無い会話として見える。"""
    _published_agent()
    draft = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="試す", agent_id=AGENT_ID, draft=True)
    )

    with pytest.raises(ThreadKindMismatchError):
        runtime_repository.create_builtin_run(
            RunCreateRequest(goal="続き", agent_id=AGENT_ID, thread_id=draft.thread_id)
        )


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_draft_conversations_need_agent_admin(auth: ProductionAuth) -> None:
    auth.user_with_permissions(
        "operator-2515", [MENU_CHAT, MENU_RUNS, RUNS_OPERATE], agent_ids=["default"]
    )

    refused = client.get("/api/threads", params={"draft": "true"}, headers=login("operator-2515"))
    allowed = client.get("/api/threads", headers=login("operator-2515"))

    assert refused.status_code == 403
    assert "Agent 管理" in refused.json()["error_messages"][0]
    assert allowed.status_code == 200, allowed.text
