"""system_admin が他の利用者の「AI 活用」のデータにできるのは、閲覧と運用の操作だけ（#3244。
#1928 を置き換え）。

- 閲覧: 他の利用者の会話・Run・処理の経過・実行履歴を見られる。
- 運用の操作: 取消・会話の削除・承認の判断・再開・確認をやめる。操作した人を残す（取消の
  イベントの `cancelled_by`・承認の `decided_by`）。再実行は system_admin 自身の新しい会話の Run。
- 内容を足す操作は持ち主だけ: 続きの質問・回答前の確認への回答・評価・会話の名前の変更は、
  system_admin でも他の利用者の会話・Run には 403（`OTHERS_CONTENT_FORBIDDEN_MESSAGE`）。外部の
  MCP も同じ（`OTHERS_CONTENT_FORBIDDEN`）。持ち主は今どおりできる。
- 既存のデータ: #3244 より前に system_admin が持ち主の会話で続けた往復は消さず、送った人の
  表示名つきで出す。
- system_admin でない利用者は、他の利用者の会話・Run のどの操作も 404（存在を漏らさない）。
- 持ち主の側（#1954）: system_admin が持ち主の会話で実行して待ちになった Run は、持ち主には
  `can_decide=False`・実行した人の表示名つきで返す（操作は 404 のまま）。持ち主は待ちを止めて
  同じ質問を自分の権限の新しい Run で送り直せる。system_admin の承認の一覧には出る。
- 実行履歴（#1954）: 行は実行した人と会話の持ち主に出て、`executed_by_me`・`executor_display_name`
  と `executor=all|mine|others` の絞り込み（件数も同じ条件）。詳細に会話の持ち主の表示名。

組み込み Runtime は呼ばず、Run の実行（画面の API と MCP の両方）を差し替えて回答で完了にする。
利用者・業務 Agent・Run はテストごとに作る（並列でも直列でも通る）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from pr_system_settings.auth.api_keys import api_key_rate_limiter
from pr_system_settings.auth.visibility import OTHERS_CONTENT_FORBIDDEN_MESSAGE
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import mcp_server, router
from app.features.agent.confirmation import ConfirmationRequest, item_from_arguments
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunEventType,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.features.agent.tools import ToolCall
from app.security.permissions import APPROVALS_DECIDE, RUNS_OPERATE, RUNS_VIEW
from app.security.service import set_security_service

APPROVAL_TOOL = "nl2sql__nl2sql_query"
ASK_ARGUMENTS = {
    "question": "どの期間の売上を見ますか？",
    "reason": "期間が質問から決められません。",
    "options": [{"label": "今月", "description": ""}, {"label": "今年度", "description": ""}],
    "multiple": False,
}
USER_PERMISSIONS = [RUNS_VIEW, RUNS_OPERATE, APPROVALS_DECIDE]


@dataclass
class People:
    agent_id: str
    other_agent_id: str
    alice: str  # 会話の持ち主（一般の利用者）
    bob: str  # 同じ業務 Agent を使える、別の一般の利用者
    admin: str  # system_admin
    alice_login: str
    bob_login: str
    admin_login: str


def _complete(run: RunState) -> None:
    """組み込み Runtime の代わりに、質問を受けて回答で完了にする。"""
    if runtime_repository.begin_builtin_run(run.id) is not None:
        runtime_repository.complete_builtin_run(run.id, f"回答: {run.goal}")


@pytest.fixture
def people(monkeypatch: MonkeyPatch) -> Iterator[People]:
    auth: ProductionAuth = enable_production_auth(monkeypatch)
    suffix = uuid4().hex[:8]
    agent_id = f"agent1928-{suffix}"
    other_agent_id = f"agent1928-other-{suffix}"
    for item_id, name in ((agent_id, "経理の Agent"), (other_agent_id, "人事の Agent")):
        runtime_repository.create_agent(
            AgentProfile(id=item_id, name=name, instructions="答える。")
        )
    alice = auth.user_with_permissions(f"alice-{suffix}", USER_PERMISSIONS, agent_ids=[agent_id])
    auth.store.users[alice.user_uuid].display_name = "アリス"
    bob = auth.user_with_permissions(f"bob-{suffix}", USER_PERMISSIONS, agent_ids=[agent_id])
    admin = auth.create_user(f"admin-{suffix}", system_admin=True)
    auth.store.users[admin.user_uuid].display_name = "管理 花子"
    monkeypatch.setattr(router, "_schedule_builtin_run", _complete)
    api_key_rate_limiter.reset()
    try:
        yield People(
            agent_id=agent_id,
            other_agent_id=other_agent_id,
            alice=alice.user_uuid,
            bob=bob.user_uuid,
            admin=admin.user_uuid,
            alice_login=alice.login_user_id,
            bob_login=bob.login_user_id,
            admin_login=admin.login_user_id,
        )
    finally:
        set_security_service(None)
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id in {agent_id, other_agent_id}
            ]:
                repository._runs.pop(run_id)
        for item_id in (agent_id, other_agent_id):
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(item_id)


def _ask(headers: dict[str, str], agent_id: str, goal: str, thread_id: str | None = None) -> Any:
    body: dict[str, Any] = {"goal": goal, "agent_id": agent_id}
    if thread_id is not None:
        body["thread_id"] = thread_id
    return client.post("/api/runs", json=body, headers=headers)


def _waiting_approval(people: People, user_uuid: str, thread_id: str | None = None) -> RunState:
    """`user_uuid` の Run を、ツールの承認で中断した状態にする（モデルは呼ばない）。"""
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="承認の要る質問", agent_id=people.agent_id, thread_id=thread_id),
        created_by_user_uuid=user_uuid,
        all_users=True,
    )
    assert runtime_repository.begin_builtin_run(run.id) is not None
    return runtime_repository.request_builtin_approvals(
        run.id,
        [ToolCall(name=APPROVAL_TOOL, arguments={"question": "売上"}, trace_id="call-1")],
        state="{}",
    )


def _legacy_admin_turn(people: People, thread_id: str, goal: str) -> RunState:
    """#3244 より前に system_admin が持ち主の会話で続けた往復（既存のデータ）を作る。"""
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=people.agent_id, thread_id=thread_id),
        created_by_user_uuid=people.admin,
        all_users=True,
    )
    _complete(run)
    return runtime_repository.get_run(run.id)


def test_system_admin_views_but_does_not_continue_another_users_chat(people: People) -> None:
    alice, bob = login(people.alice_login), login(people.bob_login)
    admin = login(people.admin_login)
    first = _ask(alice, people.agent_id, "今月の売上は？")
    assert first.status_code == 200, first.text
    thread_id = first.json()["data"]["thread_id"]
    first_id = first.json()["data"]["id"]

    # system_admin は他の利用者の会話を見られる（一覧・詳細・Run の詳細・処理の経過）。
    seen_by_admin = client.get("/api/threads?limit=100", headers=admin).json()["data"]["items"]
    [admin_summary] = [item for item in seen_by_admin if item["thread_id"] == thread_id]
    assert (admin_summary["owned"], admin_summary["actor_display_name"]) == (False, "アリス")
    detail = client.get(f"/api/threads/{thread_id}", headers=admin)
    assert detail.status_code == 200, detail.text
    assert (detail.json()["data"]["owned"], len(detail.json()["data"]["runs"])) == (False, 1)
    assert client.get(f"/api/runs/{first_id}", headers=admin).status_code == 200
    assert client.get(f"/api/runs/{first_id}/trace", headers=admin).status_code == 200

    # 続きの質問・評価・名前の変更は持ち主だけ（system_admin でも 403。#3244）。
    continued = _ask(admin, people.agent_id, "前年と比べると？", thread_id)
    assert continued.status_code == 403, continued.text
    assert OTHERS_CONTENT_FORBIDDEN_MESSAGE in continued.text
    rated = client.put(f"/api/runs/{first_id}/feedback", json={"rating": "helpful"}, headers=admin)
    assert rated.status_code == 403, rated.text
    renamed = client.patch(f"/api/threads/{thread_id}", json={"title": "x"}, headers=admin)
    assert renamed.status_code == 403, renamed.text
    # どれも持ち主の会話を変えていない。
    own = client.get(f"/api/threads/{thread_id}", headers=alice).json()["data"]
    assert [run["id"] for run in own["runs"]] == [first_id]
    assert own["sender_display_names"] == {}
    assert runtime_repository.get_run(first_id).feedback is None
    assert (
        runtime_repository.get_thread(thread_id, user_uuid=people.alice)
        .runs[0]
        .metadata.get("thread_title")
        is None
    )

    # system_admin は自分の会話なら今どおり続けられる。
    admin_own = _ask(admin, people.agent_id, "管理者の質問")
    assert admin_own.status_code == 200, admin_own.text
    admin_more = _ask(admin, people.agent_id, "続き", admin_own.json()["data"]["thread_id"])
    assert admin_more.status_code == 200, admin_more.text

    # 持ち主は今どおり続けて質問し、評価し、名前を変えられる。
    after = _ask(alice, people.agent_id, "内訳は？", thread_id)
    assert after.status_code == 200, after.text
    assert after.json()["data"]["created_by_user_uuid"] == people.alice
    owner_rated = client.put(
        f"/api/runs/{first_id}/feedback", json={"rating": "helpful"}, headers=alice
    )
    assert owner_rated.status_code == 200, owner_rated.text
    assert owner_rated.json()["data"]["feedback"]["user_uuid"] == people.alice
    owner_renamed = client.patch(
        f"/api/threads/{thread_id}", json={"title": "売上の会話"}, headers=alice
    )
    assert owner_renamed.status_code == 200, owner_renamed.text

    # system_admin でない利用者は、他の利用者の会話を見ることも続けることもできない（404）。
    assert client.get(f"/api/threads/{thread_id}", headers=bob).status_code == 404
    assert _ask(bob, people.agent_id, "割り込み", thread_id).status_code == 404
    assert thread_id not in {
        item["thread_id"]
        for item in client.get("/api/threads?limit=100", headers=bob).json()["data"]["items"]
    }


def test_existing_turns_of_the_system_admin_keep_the_sender_name(people: People) -> None:
    """#3244 より前に system_admin が続けた往復は消さず、送った人の表示名つきで出す。"""
    alice, admin = login(people.alice_login), login(people.admin_login)
    first = _ask(alice, people.agent_id, "今月の売上は？")
    thread_id = first.json()["data"]["thread_id"]
    by_admin = _legacy_admin_turn(people, thread_id, "前年と比べると？")

    own = client.get(f"/api/threads/{thread_id}", headers=alice).json()["data"]
    assert [run["id"] for run in own["runs"]] == [first.json()["data"]["id"], by_admin.id]
    assert own["runs"][1]["artifacts"][0]["content"]["text"] == "回答: 前年と比べると？"
    assert (own["owned"], own["actor_display_name"]) == (True, "")
    assert own["sender_display_names"] == {by_admin.id: "管理 花子"}
    detail = client.get(f"/api/threads/{thread_id}", headers=admin).json()["data"]
    assert (detail["owned"], detail["sender_display_names"]) == (False, {by_admin.id: "管理 花子"})
    # 持ち主の実行履歴にも、自分の会話の system_admin の Run が出る。
    history = client.get("/api/runs?limit=100", headers=alice).json()["data"]["items"]
    assert by_admin.id in {item["id"] for item in history}
    # 持ち主は、自分の会話の system_admin の往復も評価できる（自分の会話の内容）。system_admin は
    # 自分が実行した往復でも、他の利用者の会話の中なら評価できない。
    assert (
        client.put(
            f"/api/runs/{by_admin.id}/feedback", json={"rating": "helpful"}, headers=admin
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/runs/{by_admin.id}/feedback", json={"rating": "helpful"}, headers=alice
        ).status_code
        == 200
    )


def test_users_other_than_system_admin_get_404_for_every_run_operation(people: People) -> None:
    bob = login(people.bob_login)
    created = _ask(login(people.alice_login), people.agent_id, "今月の売上は？")
    run_id = created.json()["data"]["id"]
    waiting = _waiting_approval(people, people.alice)
    [approval] = waiting.approvals
    queued = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="待っている質問", agent_id=people.agent_id),
        created_by_user_uuid=people.alice,
    )
    requests = [
        ("get", f"/api/runs/{run_id}", None),
        ("get", f"/api/runs/{run_id}/artifacts", None),
        ("get", f"/api/runs/{run_id}/trace", None),
        ("put", f"/api/runs/{run_id}/feedback", {"rating": "helpful"}),
        ("post", f"/api/runs/{run_id}/replay", None),
        ("post", f"/api/runs/{queued.id}/cancel", None),
        ("post", f"/api/runs/{waiting.id}/resume", None),
        ("post", f"/api/runs/{run_id}/confirmations/confirmation_x", {"action": "skip"}),
        ("get", f"/api/approvals/{approval.id}", None),
        ("post", f"/api/approvals/{approval.id}/decision", {"approved": True}),
        ("patch", f"/api/threads/{created.json()['data']['thread_id']}", {"title": "x"}),
        ("delete", f"/api/threads/{created.json()['data']['thread_id']}", None),
    ]
    for method, url, body in requests:
        kwargs: dict[str, Any] = {"headers": bob}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(url, **kwargs)
        assert response.status_code == 404, (method, url, response.text)
    # どの操作も Run を変えていない。
    assert runtime_repository.get_run(queued.id).status == RunStatus.QUEUED
    assert runtime_repository.get_run(run_id).feedback is None


def test_system_admin_cancels_resumes_replays_and_decides_on_others_runs(people: People) -> None:
    admin = login(people.admin_login)
    # 取消: 操作した system_admin をイベントに残す。
    queued = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="待っている質問", agent_id=people.agent_id),
        created_by_user_uuid=people.alice,
    )
    cancelled = client.post(f"/api/runs/{queued.id}/cancel", headers=admin)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"
    [event] = [
        item
        for item in runtime_repository.get_run(queued.id).events
        if item.type == RunEventType.RUN_CANCELLED
    ]
    assert event.payload["cancelled_by"] == people.admin_login

    # 承認の判断: 決定者は system_admin。再開は止まった Run を起こし直す（Run の利用者のまま）。
    waiting = _waiting_approval(people, people.alice)
    [approval] = waiting.approvals
    assert client.get(f"/api/approvals/{approval.id}", headers=admin).status_code == 200
    decided = client.post(
        f"/api/approvals/{approval.id}/decision", json={"approved": True}, headers=admin
    )
    assert decided.status_code == 200, decided.text
    [stored] = runtime_repository.get_run(waiting.id).approvals
    assert (stored.status.value, stored.decided_by) == ("approved", people.admin_login)
    assert runtime_repository.get_run(waiting.id).created_by_user_uuid == people.alice
    resumable = runtime_repository.get_run(waiting.id)
    resumed = client.post(f"/api/runs/{resumable.id}/resume", headers=admin)
    # 承認の後は自動で再開を待つ。止まっている Run だけ再開でき、そうでなければ 409
    # （404 ではない）。
    assert resumed.status_code in {200, 409}, resumed.text

    # 再実行: 新しい Run は操作した system_admin として、system_admin 自身の新しい会話で実行する
    # （持ち主の会話には足さない。#3244）。
    original = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="再実行する質問", agent_id=people.agent_id),
        created_by_user_uuid=people.alice,
    )
    _complete(original)
    replayed = client.post(f"/api/runs/{original.id}/replay", headers=admin)
    assert replayed.status_code == 200, replayed.text
    data = replayed.json()["data"]
    assert data["created_by_user_uuid"] == people.admin
    assert data["metadata"]["replayed_from_run_id"] == original.id
    assert data["thread_id"] != original.thread_id
    assert data["thread_owner_user_uuid"] == people.admin
    alice_threads = client.get("/api/threads?limit=100", headers=login(people.alice_login))
    assert data["thread_id"] not in {
        item["thread_id"] for item in alice_threads.json()["data"]["items"]
    }

    # 会話の削除も運用の操作として system_admin ができる。
    owned = _ask(login(people.alice_login), people.agent_id, "消す会話")
    deleted = client.delete(f"/api/threads/{owned.json()['data']['thread_id']}", headers=admin)
    assert deleted.status_code == 200, deleted.text


def test_admin_does_not_answer_the_confirmation_in_another_users_chat(people: People) -> None:
    alice, admin = login(people.alice_login), login(people.admin_login)
    created = _ask(alice, people.agent_id, "売上は？")
    run = runtime_repository.get_run(created.json()["data"]["id"])
    # ツールの結果の確認（NL2SQL）。答えると同じ会話に次の Run を作る。
    request = ConfirmationRequest(
        source="nl2sql", call_id="call-1", items=[item_from_arguments(ASK_ARGUMENTS)]
    )
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 完了した Run に確認を出した状態にする
        repository._runs[run.id].confirmations.append(request)
    body = {
        "action": "answer",
        "answers": [{"item_id": "item_1", "option_ids": ["option_1"], "other_text": ""}],
    }
    # system_admin は答えも確認せずに回答も足せない（403。#3244）。確認は答えていないまま。
    for action_body in (body, {"action": "skip"}):
        refused = client.post(
            f"/api/runs/{run.id}/confirmations/{request.id}", json=action_body, headers=admin
        )
        assert refused.status_code == 403, refused.text
        assert OTHERS_CONTENT_FORBIDDEN_MESSAGE in refused.text
    assert runtime_repository.get_run(run.id).confirmations[0].status == "pending"
    assert [
        item["id"]
        for item in client.get(f"/api/threads/{run.thread_id}", headers=alice).json()["data"][
            "runs"
        ]
    ] == [run.id]

    # 持ち主は今どおり答え、次の Run は持ち主として実行する。
    answered = client.post(
        f"/api/runs/{run.id}/confirmations/{request.id}", json=body, headers=alice
    )
    assert answered.status_code == 200, answered.text
    next_run = answered.json()["data"]["next_run"]
    assert (next_run["created_by_user_uuid"], next_run["thread_owner_user_uuid"]) == (
        people.alice,
        people.alice,
    )
    assert next_run["thread_id"] == run.thread_id


def test_admin_cancels_the_confirmation_in_another_users_chat(people: People) -> None:
    """確認をやめる（取り消し）は運用の操作で、system_admin も他の利用者の Run にできる。"""
    admin = login(people.admin_login)
    created = _ask(login(people.alice_login), people.agent_id, "売上は？")
    run = runtime_repository.get_run(created.json()["data"]["id"])
    request = ConfirmationRequest(
        source="nl2sql", call_id="call-1", items=[item_from_arguments(ASK_ARGUMENTS)]
    )
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 完了した Run に確認を出した状態にする
        repository._runs[run.id].confirmations.append(request)
    cancelled = client.post(
        f"/api/runs/{run.id}/confirmations/{request.id}", json={"action": "cancel"}, headers=admin
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["next_run"] is None
    assert runtime_repository.get_run(run.id).confirmations[0].status == "cancelled"


def test_the_owner_does_not_decide_approvals_of_runs_by_the_system_admin(people: People) -> None:
    """承認したツールは Run の利用者の権限で実行するため、持ち主の会話でも判断は実行した人か
    system_admin だけ（持ち主は Run を読める）。"""
    alice = login(people.alice_login)
    created = _ask(alice, people.agent_id, "売上は？")
    waiting = _waiting_approval(people, people.admin, created.json()["data"]["thread_id"])
    [approval] = waiting.approvals
    assert client.get(f"/api/runs/{waiting.id}", headers=alice).status_code == 200
    assert client.get(f"/api/approvals/{approval.id}", headers=alice).status_code == 404
    refused = client.post(
        f"/api/approvals/{approval.id}/decision", json={"approved": True}, headers=alice
    )
    assert refused.status_code == 404
    # 再開と Agent 自身の確認への答えも、Run の利用者（system_admin）の権限で実行し直すので同じ。
    assert client.post(f"/api/runs/{waiting.id}/resume", headers=alice).status_code == 404
    agent_question = ConfirmationRequest(
        source="agent", call_id="call-agent", items=[item_from_arguments(ASK_ARGUMENTS)]
    )
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - Agent 自身の確認を出した状態にする
        repository._runs[waiting.id].confirmations.append(agent_question)
    skipped = client.post(
        f"/api/runs/{waiting.id}/confirmations/{agent_question.id}",
        json={"action": "skip"},
        headers=alice,
    )
    assert skipped.status_code == 404


def test_owner_sees_who_the_waiting_run_of_the_system_admin_waits_for(people: People) -> None:
    """system_admin が持ち主の会話で実行して止まった Run（#1954）。持ち主は待っている相手を知り、
    操作は 404 のまま、自分で質問し直す（止めて、同じ質問を自分の権限で送る）ことができる。"""
    alice, bob, admin = (
        login(people.alice_login),
        login(people.bob_login),
        login(people.admin_login),
    )
    created = _ask(alice, people.agent_id, "売上は？")
    thread_id = created.json()["data"]["thread_id"]
    waiting = _waiting_approval(people, people.admin, thread_id)
    [approval] = waiting.approvals

    # 持ち主の会話: 自分の往復は操作でき、system_admin の往復は実行した人の名前つきで操作できない。
    own = client.get(f"/api/threads/{thread_id}", headers=alice).json()["data"]
    mine, by_admin = own["runs"]
    assert (mine["executed_by_me"], mine["can_decide"], mine["owned_by_me"]) == (True, True, True)
    assert by_admin["id"] == waiting.id
    assert by_admin["status"] == "waiting_approval"
    assert (
        by_admin["executed_by_me"],
        by_admin["can_decide"],
        by_admin["executor_display_name"],
        by_admin["owned_by_me"],
        by_admin["conversation_owner_display_name"],
    ) == (False, False, "管理 花子", True, "アリス")
    detail = client.get(f"/api/runs/{waiting.id}", headers=alice).json()["data"]
    assert (detail["can_decide"], detail["executor_display_name"]) == (False, "管理 花子")
    # 待ちの操作は 404 のまま（#1928）。持ち主の承認の一覧にも出さない。
    assert client.post(f"/api/runs/{waiting.id}/resume", headers=alice).status_code == 404
    assert (
        client.post(
            f"/api/approvals/{approval.id}/decision", json={"approved": True}, headers=alice
        ).status_code
        == 404
    )
    listed = client.get("/api/approvals?limit=100", headers=alice).json()["data"]["items"]
    assert approval.id not in {item["id"] for item in listed}

    # system_admin の側: 他の利用者の会話で始めた Run の待ちは、承認の一覧に出て判断できる。
    pending = client.get("/api/approvals?status=pending&limit=100", headers=admin).json()["data"]
    assert approval.id in {item["id"] for item in pending["items"]}
    seen = client.get(f"/api/threads/{thread_id}", headers=admin).json()["data"]["runs"][1]
    assert (seen["executed_by_me"], seen["can_decide"], seen["owned_by_me"]) == (True, True, False)
    assert seen["conversation_owner_display_name"] == "アリス"

    # 別の一般の利用者には見えない（404）。
    assert client.get(f"/api/runs/{waiting.id}", headers=bob).status_code == 404
    assert client.get(f"/api/threads/{thread_id}", headers=bob).status_code == 404

    # 自分で質問し直す: 持ち主は待ちを止め（取消は持ち主もできる）、同じ質問を同じ会話で送る。
    # 新しい Run は持ち主の権限（created_by = 持ち主。RAG / NL2SQL のサービストークンの sub も
    # この利用者）で実行する。
    stopped = client.post(f"/api/runs/{waiting.id}/cancel", headers=alice)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["status"] == "cancelled"
    again = _ask(alice, people.agent_id, waiting.goal, thread_id)
    assert again.status_code == 200, again.text
    rerun = runtime_repository.get_run(again.json()["data"]["id"])
    assert (rerun.created_by_user_uuid, rerun.thread_owner_user_uuid, rerun.thread_id) == (
        people.alice,
        people.alice,
        thread_id,
    )
    assert rerun.status == RunStatus.COMPLETED
    # 持ち主の「再実行」も持ち主の権限で動く（新しい会話の Run）。
    replayed = client.post(f"/api/runs/{waiting.id}/replay", headers=alice)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["data"]["created_by_user_uuid"] == people.alice


def test_run_history_shows_runs_to_the_executor_and_the_conversation_owner(
    people: People,
) -> None:
    """実行履歴（#1954）: 行は実行した人と会話の持ち主に出て、実行者で絞れる（件数も同じ条件）。"""
    alice, bob, admin = (
        login(people.alice_login),
        login(people.bob_login),
        login(people.admin_login),
    )
    own = _ask(alice, people.agent_id, "自分の質問").json()["data"]
    # #3244 より前に system_admin が持ち主の会話で続けた Run（既存のデータ）。
    in_alice = _legacy_admin_turn(people, own["thread_id"], "管理者の続き").model_dump(mode="json")
    admin_own = _ask(admin, people.agent_id, "管理者の自分の会話").json()["data"]

    def history(headers: dict[str, str], executor: str | None = None) -> dict[str, Any]:
        url = "/api/runs?limit=100" + (f"&executor={executor}" if executor else "")
        response = client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()["data"]
        return data

    ours = {own["id"], in_alice["id"], admin_own["id"]}
    seen = {item["id"]: item for item in history(alice)["items"]}
    assert set(seen) & ours == {own["id"], in_alice["id"]}
    assert (seen[own["id"]]["executed_by_me"], seen[own["id"]]["executor_display_name"]) == (
        True,
        "アリス",
    )
    assert (
        seen[in_alice["id"]]["executed_by_me"],
        seen[in_alice["id"]]["executor_display_name"],
    ) == (False, "管理 花子")
    mine = history(alice, "mine")
    assert ({item["id"] for item in mine["items"]}, mine["total"]) == ({own["id"]}, 1)
    others = history(alice, "others")
    assert ({item["id"] for item in others["items"]}, others["total"]) == ({in_alice["id"]}, 1)
    assert others["unfiltered_total"] == 2
    # 他の人が実行した Run を見られる利用者だけに「実行者」の列と絞り込みを出す（#1955）。
    assert history(alice)["has_other_executors"] is True
    # 詳細: 実行者と会話の持ち主。
    detail = client.get(f"/api/runs/{in_alice['id']}", headers=alice).json()["data"]
    assert (
        detail["executor_display_name"],
        detail["conversation_owner_display_name"],
        detail["owned_by_me"],
        detail["executed_by_me"],
    ) == ("管理 花子", "アリス", True, False)

    # system_admin: 全員の Run。自分が実行 / 他の人が実行で絞れる。
    admin_mine = {item["id"] for item in history(admin, "mine")["items"]}
    assert admin_mine & ours == {in_alice["id"], admin_own["id"]}
    admin_others = {item["id"] for item in history(admin, "others")["items"]}
    assert admin_others & ours == {own["id"]}

    # 別の一般の利用者には、どれも出ない・詳細は 404。
    assert {item["id"] for item in history(bob)["items"]} & ours == set()
    assert history(admin)["has_other_executors"] is True
    assert client.get(f"/api/runs/{in_alice['id']}", headers=bob).status_code == 404
    assert client.get("/api/runs?executor=everyone", headers=alice).status_code == 422


# ---- 外部の MCP（API キー）-------------------------------------------------------------------


def _issue_key(headers: dict[str, str], **body: Any) -> str:
    payload = {"name": "Claude Code", "products": ["agent"], **body}
    response = client.post("/api/account/api-keys", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    token: str = response.json()["data"]["token"]
    return token


def _tool(token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    content: dict[str, Any] = response.json()["result"]["structuredContent"]
    return content


def test_mcp_key_of_system_admin_does_not_continue_another_users_chat(
    monkeypatch: MonkeyPatch, people: People
) -> None:
    monkeypatch.setattr(mcp_server, "wait_poll_seconds", 0.01)
    created = _ask(login(people.alice_login), people.agent_id, "今月の売上は？")
    thread_id = created.json()["data"]["thread_id"]
    first_id = created.json()["data"]["id"]

    admin_session = login(people.admin_login)
    admin_key = _issue_key(admin_session)
    # 他の利用者の会話の続きは、system_admin のキーでも拒む（画面と同じ。#3244）。
    refused = _tool(
        admin_key,
        "agent_chat",
        {"agent_id": people.agent_id, "question": "前年と比べると？", "thread_id": thread_id},
    )
    assert (refused["error_code"], refused["status"]) == ("OTHERS_CONTENT_FORBIDDEN", 403)
    assert [
        run.id for run in runtime_repository.get_thread(thread_id, user_uuid=people.alice).runs
    ] == [first_id]
    # 他の利用者の Run は読める。
    read = _tool(admin_key, "agent_get_run", {"run_id": first_id, "wait_seconds": 0})
    assert read["run_id"] == first_id
    # 回答前の確認への答えも同じく拒む。
    request = ConfirmationRequest(
        source="nl2sql", call_id="call-1", items=[item_from_arguments(ASK_ARGUMENTS)]
    )
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 完了した Run に確認を出した状態にする
        repository._runs[first_id].confirmations.append(request)
    answer = _tool(
        admin_key,
        "agent_answer_confirmation",
        {"run_id": first_id, "confirmation_id": request.id, "action": "skip", "wait_seconds": 0},
    )
    assert (answer["error_code"], answer["status"]) == ("OTHERS_CONTENT_FORBIDDEN", 403)
    assert runtime_repository.get_run(first_id).confirmations[0].status == "pending"

    # system_admin のキーでも、自分の会話は続けられる。
    own = _tool(admin_key, "agent_chat", {"agent_id": people.agent_id, "question": "質問"})
    assert own["answer"] == "回答: 質問", own
    more = _tool(
        admin_key,
        "agent_chat",
        {"agent_id": people.agent_id, "question": "続き", "thread_id": own["thread_id"]},
    )
    assert more["answer"] == "回答: 続き", more
    assert runtime_repository.get_run(more["run_id"]).metadata["source"] == "mcp"

    # キーの業務 Agent の範囲は system_admin でもそのまま（範囲の外は 403）。
    scoped = _issue_key(admin_session, resource_scopes={"agent": [people.other_agent_id]})
    outside = _tool(
        scoped,
        "agent_chat",
        {"agent_id": people.agent_id, "question": "続き", "thread_id": thread_id},
    )
    assert (outside["error_code"], outside["status"]) == ("AGENT_OUT_OF_KEY_SCOPE", 403)

    # system_admin でない利用者のキーは、他の利用者の会話・Run を「無い」として扱う。
    bob_key = _issue_key(login(people.bob_login))
    hidden_thread = _tool(
        bob_key,
        "agent_chat",
        {"agent_id": people.agent_id, "question": "割り込み", "thread_id": thread_id},
    )
    assert hidden_thread["error_code"] == "THREAD_NOT_FOUND"
    hidden = _tool(bob_key, "agent_get_run", {"run_id": first_id, "wait_seconds": 0})
    assert hidden["error_code"] == "RUN_NOT_FOUND"


# 外部の MCP の取消（agent_cancel_run。#3240）: 止める人 × 実行した人 → 結果。画面の取消と同じ人
# （実行した利用者・会話の持ち主・system_admin）だけが止められ、ほかの利用者は「無い」として扱う。
CANCEL_MATRIX = [
    ("alice", "alice", "cancelled"),
    # 会話の持ち主は、system_admin が持ち主の会話で実行した Run も止められる。
    ("alice", "admin", "cancelled"),
    # system_admin は他の利用者の Run も止められる（運用の操作。#3244）。
    ("admin", "alice", "cancelled"),
    ("bob", "alice", "RUN_NOT_FOUND"),
]


@pytest.mark.parametrize(
    ("canceller", "runner", "expected"),
    CANCEL_MATRIX,
    ids=["runner", "thread-owner", "system_admin", "other-user"],
)
def test_mcp_cancel_run_is_allowed_to_the_same_people_as_the_screen(
    monkeypatch: MonkeyPatch, people: People, canceller: str, runner: str, expected: str
) -> None:
    monkeypatch.setattr(mcp_server, "wait_poll_seconds", 0.01)
    logins = {"alice": people.alice_login, "bob": people.bob_login, "admin": people.admin_login}
    users = {"alice": people.alice, "admin": people.admin}
    first = _ask(login(people.alice_login), people.agent_id, "合成の質問")
    thread_id = first.json()["data"]["thread_id"]
    # 会話の持ち主（alice）の会話の、まだ始まっていない Run。
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の続き", agent_id=people.agent_id, thread_id=thread_id),
        created_by_user_uuid=users[runner],
        all_users=True,
    )
    response = client.post(
        "/api/account/api-keys",
        json={"name": "外部のクライアント", "products": ["agent"]},
        headers=login(logins[canceller]),
    )
    assert response.status_code == 200, response.text
    key = response.json()["data"]
    output = _tool(key["token"], "agent_cancel_run", {"run_id": run.id})
    stored = runtime_repository.get_run(run.id)
    if expected == "RUN_NOT_FOUND":
        assert (output["error_code"], output["status"]) == ("RUN_NOT_FOUND", 404)
        assert stored.status == RunStatus.QUEUED
        return
    assert (output["result"], output["status"]) == (expected, "cancelled")
    # 操作した人と入口（MCP・キーの ID）を残し、Run と会話の持ち主は変えない。
    [event] = [item for item in stored.events if item.type == RunEventType.RUN_CANCELLED]
    assert event.payload["cancelled_by"] == logins[canceller]
    assert (event.payload["via"], event.payload["api_key_id"]) == ("mcp", key["key"]["id"])
    assert stored.created_by_user_uuid == users[runner]
    assert runtime_repository.get_thread(thread_id, user_uuid=people.alice) is not None
    if canceller == "admin":
        # 止められても、他の利用者の会話の続きは system_admin でも 403 のまま。
        refused = _tool(
            key["token"],
            "agent_chat",
            {"agent_id": people.agent_id, "question": "合成", "thread_id": thread_id},
        )
        assert (refused["error_code"], refused["status"]) == ("OTHERS_CONTENT_FORBIDDEN", 403)
