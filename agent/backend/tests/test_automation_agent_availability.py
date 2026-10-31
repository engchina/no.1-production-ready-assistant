"""自動実行の保存と業務 Agent の状態（#927）。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.features.agent.automations import automation_store
from app.features.agent.runtime import runtime_repository
from app.main import app

client = TestClient(app)

AGENT_ID = "agent-automation-927"


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    for item in automation_store.list():
        if item.agent_id in {AGENT_ID, "missing-927"}:
            with contextlib.suppress(KeyError):
                automation_store.delete(item.id)
    with contextlib.suppress(KeyError, ValueError):
        runtime_repository.delete_agent(AGENT_ID)


def _messages(response: Any) -> list[str]:
    return list(response.json()["error_messages"])


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "agent_id": AGENT_ID,
        "name": "毎朝の要約",
        "goal": "昨日の売上を要約してください。",
        "enabled": True,
        "trigger": "schedule",
        **overrides,
    }


def _published_agent() -> None:
    assert client.post("/api/agents", json={"id": AGENT_ID, "name": "経理"}).status_code == 200
    assert client.post(f"/api/agents/{AGENT_ID}/publish", json={}).status_code == 200


def test_automation_can_be_disabled_after_agent_becomes_unavailable() -> None:
    _published_agent()
    created = client.post("/api/automations", json=_body())
    assert created.status_code == 200
    automation_id = created.json()["data"]["id"]

    assert client.patch(f"/api/agents/{AGENT_ID}", json={"enabled": False}).status_code == 200

    # 有効のままの保存は断る（実行できない業務 Agent の自動実行を動かし続けない）。
    kept = client.put(f"/api/automations/{automation_id}", json=_body(goal="変更"))
    assert kept.status_code == 422
    assert "この業務 Agent は実行できない状態です。" in _messages(kept)[0]

    # 無効にする保存はできる。
    disabled = client.put(f"/api/automations/{automation_id}", json=_body(enabled=False))
    assert disabled.status_code == 200
    assert disabled.json()["data"]["enabled"] is False
    assert disabled.json()["data"]["next_run_at"] is None

    # 無効のままなら、ほかの項目も直せる。
    renamed = client.put(
        f"/api/automations/{automation_id}", json=_body(enabled=False, name="止めた要約")
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["name"] == "止めた要約"


def test_automation_with_missing_agent_is_422_not_500() -> None:
    created = client.post("/api/automations", json=_body(agent_id="missing-927"))
    assert created.status_code == 422
    assert "業務 Agent が見つかりません。" in _messages(created)[0]

    # 業務 Agent を消した後の自動実行も、無効にして保存できる。有効のままなら 422。
    _published_agent()
    item = client.post("/api/automations", json=_body())
    assert item.status_code == 200
    automation_id = item.json()["data"]["id"]
    runtime_repository.delete_agent(AGENT_ID)
    kept = client.put(f"/api/automations/{automation_id}", json=_body())
    assert kept.status_code == 422
    assert "業務 Agent が見つかりません。" in _messages(kept)[0]
    disabled = client.put(f"/api/automations/{automation_id}", json=_body(enabled=False))
    assert disabled.status_code == 200
