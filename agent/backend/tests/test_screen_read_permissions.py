"""画面を開いた直後に読む API が、その画面のメニュー権限で読めること（#1113）。

経路の権限拒否（403 `SECURITY_ROUTE_FORBIDDEN`）で画面は権限なしの画面へ移るため、
画面が開いた直後に読む API が 1 つでも拒否されると、その画面を使えない。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.security.permissions import (
    MENU_AUTOMATIONS,
    MENU_EVALUATION,
    MENU_FEEDBACK,
    MENU_RUNS,
    RUNS_OPERATE,
    RUNS_VIEW,
)
from app.security.service import set_security_service


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


@pytest.mark.parametrize("menu", [MENU_AUTOMATIONS, MENU_EVALUATION, MENU_FEEDBACK])
def test_agent_pickers_can_read_the_agents_in_scope(auth: ProductionAuth, menu: str) -> None:
    """自動実行・品質評価・フィードバックは業務 Agent を選ぶ・名前で示す（対象範囲で絞る）。"""
    login_id = f"menu-{menu}"
    auth.user_with_permissions(login_id, [menu], agent_ids=["default"])
    response = client.get("/api/agents", headers=login(login_id))
    assert response.status_code == 200
    assert [agent["id"] for agent in response.json()["data"]["agents"]] == ["default"]


def test_run_operators_can_check_the_runtime_before_creating(auth: ProductionAuth) -> None:
    """実行の作成の前に実行環境の状態を確かめる。閲覧だけの利用者は作成しないので読まない。"""
    auth.user_with_permissions("operator-1095", [RUNS_OPERATE], agent_ids=["default"])
    response = client.get("/api/runtime/status", headers=login("operator-1095"))
    assert response.status_code == 200

    auth.user_with_permissions("viewer-1095", [MENU_RUNS, RUNS_VIEW], agent_ids=["default"])
    denied = client.get("/api/runtime/status", headers=login("viewer-1095"))
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "SECURITY_ROUTE_FORBIDDEN"
