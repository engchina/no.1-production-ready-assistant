"""業務 Agent の画面が使う API の検証とエラーの文（#925）。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.features.agent.runtime import runtime_repository
from app.features.agent.skills import AgentSkillDefinition, skill_registry
from app.main import app

client = TestClient(app)

AGENT_ID = "agent-editor-925"
SKILL_ID = "skill_editor_925"


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    with contextlib.suppress(KeyError, ValueError):
        runtime_repository.delete_agent(AGENT_ID)
    with contextlib.suppress(KeyError, ValueError):
        skill_registry.remove(SKILL_ID)


def _messages(response: object) -> list[str]:
    return response.json()["error_messages"]  # type: ignore[attr-defined, no-any-return]


def test_agent_name_must_not_be_blank() -> None:
    created = client.post("/api/agents", json={"id": AGENT_ID, "name": "   "})
    assert created.status_code == 422
    assert _messages(created) == ["業務 Agent の名前を入力してください。"]

    assert client.post("/api/agents", json={"id": AGENT_ID, "name": "経理"}).status_code == 200
    patched = client.patch(f"/api/agents/{AGENT_ID}", json={"name": ""})
    assert patched.status_code == 422
    assert _messages(patched) == ["業務 Agent の名前を入力してください。"]
    # 名前を送らない変更（有効の切り替えなど）は通す。
    assert client.patch(f"/api/agents/{AGENT_ID}", json={"enabled": False}).status_code == 200


def test_agent_api_errors_are_japanese() -> None:
    assert client.post("/api/agents", json={"id": AGENT_ID, "name": "経理"}).status_code == 200

    duplicate = client.post("/api/agents", json={"id": AGENT_ID, "name": "経理"})
    assert duplicate.status_code == 400
    assert _messages(duplicate) == ["同じ ID の業務 Agent があります。"]

    for response in (
        client.patch("/api/agents/missing-925", json={"name": "x"}),
        client.post("/api/agents/missing-925/publish", json={}),
        client.request("DELETE", "/api/agents/missing-925"),
    ):
        assert response.status_code == 404
        assert _messages(response) == ["業務 Agent が見つかりません。"]

    version = client.post(f"/api/agents/{AGENT_ID}/versions/9/restore")
    assert version.status_code == 404
    assert _messages(version) == ["業務 Agent の版が見つかりません。"]

    default = client.request("DELETE", "/api/agents/default")
    assert default.status_code == 400
    assert _messages(default) == ["組み込みの業務 Agent は削除できません。"]


def test_unknown_skill_message_tells_how_to_fix() -> None:
    """登録から消えたスキルを持つ業務 Agent の保存・公開の文（画面の保存の失敗の帯に出る）。"""
    skill_registry.upsert_custom(AgentSkillDefinition(id=SKILL_ID, name="消えるスキル"))
    created = client.post(
        "/api/agents", json={"id": AGENT_ID, "name": "経理", "skill_ids": [SKILL_ID]}
    )
    assert created.status_code == 200
    skill_registry.remove(SKILL_ID)

    expected = [f"登録されていないスキルがあります: {SKILL_ID}。スキルの選択から外してください。"]
    saved = client.patch(f"/api/agents/{AGENT_ID}", json={"skill_ids": [SKILL_ID]})
    assert saved.status_code == 400
    assert _messages(saved) == expected
    published = client.post(f"/api/agents/{AGENT_ID}/publish", json={})
    assert published.status_code == 400
    assert _messages(published) == expected

    # 外せば保存できる。
    removed = client.patch(f"/api/agents/{AGENT_ID}", json={"skill_ids": []})
    assert removed.status_code == 200
