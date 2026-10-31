"""スキルの画面が使う API の検証とエラーの文（#926）。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.features.agent.runtime import runtime_repository
from app.features.agent.skills import skill_registry
from app.main import app

client = TestClient(app)

AGENT_ID = "agent-skill-926"
SKILL_ID = "skill_926"


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    with contextlib.suppress(KeyError, ValueError):
        runtime_repository.delete_agent(AGENT_ID)
    with contextlib.suppress(KeyError, ValueError):
        skill_registry.remove(SKILL_ID)


def _messages(response: object) -> list[str]:
    return response.json()["error_messages"]  # type: ignore[attr-defined, no-any-return]


def _create_skill() -> None:
    created = client.post("/api/skills", json={"id": SKILL_ID, "name": "経理の手順"})
    assert created.status_code == 200


def test_skill_used_by_agent_draft_cannot_be_deleted() -> None:
    _create_skill()
    agent = client.post(
        "/api/agents", json={"id": AGENT_ID, "name": "経理の Agent", "skill_ids": [SKILL_ID]}
    )
    assert agent.status_code == 200

    blocked = client.request("DELETE", f"/api/skills/{SKILL_ID}")
    assert blocked.status_code == 409
    assert _messages(blocked) == [
        "このスキルは業務 Agent（経理の Agent）が使っています。"
        "業務 Agent のスキルから外してから削除してください。"
    ]
    assert skill_registry.get(SKILL_ID) is not None

    # 下書きから外しても、公開中の版が使っていれば断る。
    assert client.post(f"/api/agents/{AGENT_ID}/publish", json={}).status_code == 200
    assert client.patch(f"/api/agents/{AGENT_ID}", json={"skill_ids": []}).status_code == 200
    assert client.request("DELETE", f"/api/skills/{SKILL_ID}").status_code == 409

    # 外した下書きを公開すれば削除できる。
    assert client.post(f"/api/agents/{AGENT_ID}/publish", json={}).status_code == 200
    deleted = client.request("DELETE", f"/api/skills/{SKILL_ID}")
    assert deleted.status_code == 200
    assert skill_registry.get(SKILL_ID) is None


@pytest.mark.parametrize("skill_id", ["a/b", "a b", ".hidden", "経理", "x" * 101])
def test_skill_id_must_be_url_safe(skill_id: str) -> None:
    created = client.post("/api/skills", json={"id": skill_id, "name": "x"})
    assert created.status_code == 422
    assert _messages(created) == [
        "スキルの ID は英数字で始め、英数字・_・-・. の 100 文字以内にしてください。"
    ]
    assert skill_registry.get(skill_id.strip()) is None


def test_skill_api_errors_are_japanese() -> None:
    _create_skill()
    duplicate = client.post("/api/skills", json={"id": SKILL_ID, "name": "x"})
    assert duplicate.status_code == 409
    assert _messages(duplicate) == ["同じ ID のスキルがあります。"]

    blank_name = client.patch(f"/api/skills/{SKILL_ID}", json={"name": "  "})
    assert blank_name.status_code == 422
    assert _messages(blank_name) == ["スキルの名前を入力してください。"]

    for response in (
        client.get("/api/skills/missing_926"),
        client.patch("/api/skills/missing_926", json={"name": "x"}),
        client.request("DELETE", "/api/skills/missing_926"),
    ):
        assert response.status_code == 404
        assert _messages(response) == ["スキルが見つかりません。"]

    builtin_patch = client.patch("/api/skills/business-rag-research", json={"name": "x"})
    assert builtin_patch.status_code == 400
    assert _messages(builtin_patch) == [
        "組み込み・ファイル・環境変数のスキルは画面から変更できません。"
        "読み込み元の定義を変更してください。"
    ]
    builtin_delete = client.request("DELETE", "/api/skills/business-rag-research")
    assert builtin_delete.status_code == 400
    assert _messages(builtin_delete) == [
        "組み込み・ファイル・環境変数のスキルは画面から削除できません。"
    ]
    builtin_id = client.post("/api/skills", json={"id": "business-rag-research", "name": "x"})
    assert builtin_id.status_code == 409
    assert _messages(builtin_id) == ["同じ ID のスキルがあります。"]
