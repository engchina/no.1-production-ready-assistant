"""既存の基盤オブジェクトと ledger の表示を整合させる。"""

from typing import Any, cast
from unittest.mock import Mock

import pytest

from app.security.deepsec import DeepSecService
from app.security.service import SecurityService
from app.security.store import InMemorySecurityStore
from app.settings import Settings


def test_plan_recognizes_verified_existing_foundation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_construct(
        oracle_user="APP", oracle_dsn="", oracle_deepsec_data_user="DATA_USER_A"
    )
    store = InMemorySecurityStore()
    service = DeepSecService(settings, SecurityService(store, settings), Mock())
    monkeypatch.setattr(service, "status", lambda: {"configured": True})
    plan = service.plan()
    steps = cast(list[dict[str, Any]], plan["steps"])
    assert all(step["status"] == "APPLIED" for step in steps if step["step_no"] in {1, 2})
    assert store.get_deepsec_states() == {}
