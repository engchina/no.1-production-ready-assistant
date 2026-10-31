"""実行の作成と capability の拒否の文（利用者に見せる日本語の文。技術的な原文は「詳細」へ）。"""

from __future__ import annotations

from collections.abc import Iterator

import anyio
import httpx
import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent.runtime import RunCreateRequest, RunState, runtime_repository
from app.main import app
from app.security.permissions import MENU_RUNS, MENU_SKILLS
from app.security.service import set_security_service


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_create_run_value_error_is_japanese_and_keeps_reason_in_details(
    monkeypatch: MonkeyPatch,
) -> None:
    """repository の ValueError（英語の内部の文）を 409 の本文にそのまま出さない。"""

    def reject(_request: RunCreateRequest, **_: object) -> RunState:
        raise ValueError("agent disabled")

    monkeypatch.setattr(runtime_repository, "create_builtin_run", reject)

    response = client.post("/api/runs", json={"goal": "売上を教えて", "agent_id": "default"})

    assert response.status_code == 409
    body = response.json()
    message = body["error_messages"][0]
    assert "agent disabled" not in message
    assert "この業務 Agent では今は実行できません。" in message
    assert body["error_code"] == "run_not_created"
    assert body["error_details"] == {"reason": "agent disabled"}


def test_create_run_validation_error_is_not_agent_state(monkeypatch: MonkeyPatch) -> None:
    """保存した値を読めない（ValidationError）のを「業務 Agent の状態」の 409 にしない（#2548）。"""

    def broken(_request: RunCreateRequest, **_: object) -> RunState:
        RunState.model_validate({"goal": 1})
        raise AssertionError("unreachable")

    monkeypatch.setattr(runtime_repository, "create_builtin_run", broken)

    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await http.post("/api/runs", json={"goal": "質問", "agent_id": "default"})

    response = anyio.run(post)

    assert response.status_code == 500
    assert "run_not_created" not in response.text


def test_capability_denied_message_is_japanese(auth: ProductionAuth) -> None:
    """メニュー権限だけで capability の要る API を呼ぶと、必要な権限を日本語で示す。"""
    auth.user_with_permissions("menu-only-runs", [MENU_RUNS, MENU_SKILLS])
    headers = login("menu-only-runs")

    for path in ("/api/runs", "/api/skills"):
        response = client.get(path, headers=headers)
        assert response.status_code == 403
        body = response.json()
        message = body["error_messages"][0]
        assert "requires one of roles" not in message
        assert "menu-only-runs" not in message
        assert message == (
            "この操作を行う権限がありません。必要な権限（いずれか）: "
            "実行履歴の参照、監査ログの参照、業務 Agent の実行、承認の判断、Agent 管理"
        )
        # 経路の権限拒否（権限なしの画面へ移す SECURITY_ROUTE_FORBIDDEN）にはしない。
        assert body.get("error_code") is None


def test_capability_denied_message_lists_only_the_required_capability() -> None:
    from app.features.agent.router import _capability_denied_message

    assert _capability_denied_message({"admin"}) == (
        "この操作を行う権限がありません。必要な権限（いずれか）: Agent 管理"
    )
    assert _capability_denied_message({"approver"}) == (
        "この操作を行う権限がありません。必要な権限（いずれか）: 承認の判断、Agent 管理"
    )
