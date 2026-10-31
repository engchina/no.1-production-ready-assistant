"""保存先の状態（`GET /api/runtime/storage`。#839）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.features.agent import (
    control_plane_store,
    run_facts_store,
    runtime,
    storage_backend,
    storage_status,
)
from app.features.agent.control_plane_store import FileItemStore, MemoryItemStore, OracleItemStore
from app.security.permissions import (
    ADMIN,
    MENU_AGENTS,
    MENU_CHAT,
    MENU_RUNS,
    MENU_RUNTIMES,
    MENU_SETTINGS_EXTERNAL_MCP,
    MENU_SETTINGS_SYSTEM_TABLES,
    permission_for_route,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _restore_store() -> Iterator[None]:
    yield
    control_plane_store.set_control_plane_store(None)


def _database(monkeypatch: MonkeyPatch, *, configured: bool) -> None:
    monkeypatch.setattr(
        storage_backend, "database_readiness", lambda _settings: "ok" if configured else "missing"
    )


def test_memory_with_database_configured_points_to_the_backend_setting(
    monkeypatch: MonkeyPatch,
) -> None:
    """DB は設定済みでも保存先が memory なら、理由は「保存先の設定」（DB の設定へ案内しない）。"""
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "memory")
    control_plane_store.set_control_plane_store(MemoryItemStore())
    _database(monkeypatch, configured=True)

    status = storage_status.runtime_storage_status()

    assert status.backend == "memory"
    assert status.persistent is False
    assert status.database_configured is True
    assert status.reason == "memory_backend"


def test_memory_without_database_points_to_the_database_settings(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "memory")
    control_plane_store.set_control_plane_store(MemoryItemStore())
    _database(monkeypatch, configured=False)

    status = storage_status.runtime_storage_status()

    assert status.persistent is False
    assert status.database_configured is False
    assert status.reason == "database_not_configured"


def test_oracle_backend_is_persistent(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "oracle")
    control_plane_store.set_control_plane_store(OracleItemStore(connect_factory=lambda: None))
    _database(monkeypatch, configured=True)

    status = storage_status.runtime_storage_status()

    assert status.backend == "oracle_checkpoint"
    assert status.persistent is True
    assert status.reason is None


def test_file_without_snapshot_path_is_memory(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_repository_backend", "file")
    monkeypatch.setattr(settings, "agent_runtime_snapshot_path", None)
    assert storage_status.effective_backend() == "memory"

    monkeypatch.setattr(settings, "agent_runtime_snapshot_path", str(tmp_path / "runtime.json"))
    control_plane_store.set_control_plane_store(FileItemStore(tmp_path / "items.json"))
    _database(monkeypatch, configured=False)
    status = storage_status.runtime_storage_status()
    assert status.backend == "file"
    assert status.persistent is True
    assert status.reason is None


def test_readiness_failure_is_reported_as_not_configured(monkeypatch: MonkeyPatch) -> None:
    def broken(_settings: object) -> str:
        raise OSError("wallet")

    monkeypatch.setattr(storage_backend, "database_readiness", broken)
    assert storage_status.database_configured() is False


def test_storage_api_returns_status_without_connection_details(monkeypatch: MonkeyPatch) -> None:
    from security_support import client

    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "memory")
    control_plane_store.set_control_plane_store(MemoryItemStore())
    _database(monkeypatch, configured=True)

    response = client.get("/api/runtime/storage")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "backend": "memory",
        "configured_backend": "memory",
        "persistent": False,
        "database_configured": True,
        "reason": "memory_backend",
        "repaired_runs": 0,
        "skipped_runs": 0,
        "skipped_agents": 0,
    }


def test_storage_api_is_readable_from_the_pages_that_show_the_notice() -> None:
    allowed = permission_for_route("GET", "/runtime/storage")
    assert allowed is not None
    for code in (
        MENU_AGENTS,
        MENU_RUNS,
        MENU_RUNTIMES,
        MENU_SETTINGS_EXTERNAL_MCP,
        MENU_SETTINGS_SYSTEM_TABLES,
        ADMIN,
    ):
        assert code in allowed
    # チャットだけの利用者には出さない（直せない案内を出さない）。
    assert MENU_CHAT not in allowed


# ---- 既定の `auto`（DB の設定がそろっていれば Oracle。#839） ----------------------------------


def test_default_backend_is_auto() -> None:
    from app.settings import Settings

    assert Settings.model_fields["agent_runtime_repository_backend"].default == "auto"


def test_auto_uses_oracle_when_the_database_is_configured(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "auto")
    _database(monkeypatch, configured=True)

    assert storage_backend.resolved_backend() == "oracle_checkpoint"
    # 定義の store・Run の事実も同じ決定に従う。
    assert isinstance(control_plane_store.build_control_plane_store(), OracleItemStore)
    assert isinstance(run_facts_store._default_store(), run_facts_store.OracleRunFactsStore)


def test_auto_uses_memory_without_the_database(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "auto")
    _database(monkeypatch, configured=False)

    assert storage_backend.resolved_backend() == "memory"
    store = control_plane_store.build_control_plane_store()
    assert store.persistent is False
    control_plane_store.set_control_plane_store(store)
    status = storage_status.runtime_storage_status()
    assert status.configured_backend == "auto"
    assert status.reason == "database_not_configured"


def test_auto_decides_once_and_asks_for_restart_after_the_database_is_set(
    monkeypatch: MonkeyPatch,
) -> None:
    """起動の後に DB を設定しても、Run と定義の保存先がずれないよう、再起動まで memory のまま。"""
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "auto")
    _database(monkeypatch, configured=False)
    assert storage_backend.resolved_backend() == "memory"
    control_plane_store.set_control_plane_store(control_plane_store.build_control_plane_store())

    _database(monkeypatch, configured=True)
    assert storage_backend.resolved_backend() == "memory"
    status = storage_status.runtime_storage_status()
    assert status.persistent is False
    assert status.database_configured is True
    assert status.reason == "restart_required"


class _UnreachableOracle:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("DPY-6005: cannot connect to database")


@pytest.mark.parametrize("backend", ["auto", "oracle_checkpoint"])
def test_unreachable_oracle_does_not_fall_back_to_memory(
    monkeypatch: MonkeyPatch, backend: str
) -> None:
    """ADB の停止中などは memory にせず、接続できた時点で Oracle を読み込む（#1212）。"""
    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", backend)
    _database(monkeypatch, configured=True)
    monkeypatch.setattr(runtime, "AgentRuntimeOracleCheckpointRepository", _UnreachableOracle)

    with pytest.raises(runtime.AgentRuntimeStorageUnavailableError, match="DPY-6005"):
        runtime.build_runtime_repository()

    assert storage_backend.fell_back_to_memory() is False
    # 定義の store も Oracle のまま（Run と定義の保存先がずれない）。
    assert control_plane_store.build_control_plane_store().persistent is True


def test_auto_does_not_hide_non_connection_errors(monkeypatch: MonkeyPatch) -> None:
    """接続以外の失敗（SQL・データの不整合など）は memory にせず返す（黙って捨てない）。"""

    class _Broken:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise ValueError("checkpoint の JSON が壊れています")

    monkeypatch.setattr(get_settings(), "agent_runtime_repository_backend", "auto")
    _database(monkeypatch, configured=True)
    monkeypatch.setattr(runtime, "AgentRuntimeOracleCheckpointRepository", _Broken)

    with pytest.raises(ValueError):
        runtime.build_runtime_repository()
    assert storage_backend.fell_back_to_memory() is False
