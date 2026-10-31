"""Run・定義の保存先の読み込み（#1212）。

DB に接続できないあいだも backend は起動し（RAG / NL2SQL と同じ）、業務の API は 503 を返す。
DB に接続できるようになれば、再起動せずに Run の repository と定義を読み込む。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.features.agent import control_plane_store, runtime, storage_backend, storage_bootstrap
from app.features.agent.control_plane_store import MemoryItemStore
from app.features.agent.runtime import (
    AgentRuntimeCheckpointCorruptError,
    AgentRuntimeStorageUnavailableError,
)

_UNAVAILABLE = "Agent の保存先のデータベースに接続できません（DPY-6005）。"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Storage:
    """Run の repository・定義の復元・履歴の準備を差し替え、呼ばれた順を記録する。"""

    def __init__(self, monkeypatch: MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.repository_error: BaseException | None = None
        self.restore_error: BaseException | None = None
        real_repository: Callable[[], Any] = runtime.get_runtime_repository

        def get_repository() -> Any:
            self.calls.append("repository")
            if self.repository_error is not None:
                raise self.repository_error
            return real_repository()

        def restore() -> dict[str, int]:
            self.calls.append("restore")
            if self.restore_error is not None:
                raise self.restore_error
            return {}

        monkeypatch.setattr(runtime, "get_runtime_repository", get_repository)
        monkeypatch.setattr(control_plane_store, "restore_control_plane", restore)
        monkeypatch.setattr(
            storage_bootstrap, "_prepare_history", lambda: self.calls.append("history")
        )


@pytest.fixture
def clock(monkeypatch: MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(storage_bootstrap, "_monotonic", fake)
    storage_bootstrap.reset()
    return fake


@pytest.fixture
def storage(monkeypatch: MonkeyPatch, clock: _Clock) -> _Storage:
    return _Storage(monkeypatch)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/agents", True),
        ("/api/runs/run_1/events", True),
        ("/api/settings/mcp-connections", True),
        ("/api/runtime/storage", True),
        ("/api/readiness", True),
        ("/api/health", False),
        ("/api/ready", False),
        ("/api/ready/database", False),
        ("/api/auth/login", False),
        ("/api/settings/database", False),
        ("/api/settings/database/system-tables/initialize", False),
        ("/api/settings/model/test", False),
        ("/api/settings/oci/config/read", False),
        ("/api/security/users", False),
        ("/api/security/roles/role_1/access", False),
    ],
)
def test_only_business_apis_wait_for_the_storage(path: str, expected: bool) -> None:
    assert storage_bootstrap.requires_storage(path) is expected


def test_loads_repository_then_definitions_then_history_once(storage: _Storage) -> None:
    assert storage_bootstrap.is_ready() is False

    storage_bootstrap.ensure_ready()
    storage_bootstrap.ensure_ready()

    assert storage_bootstrap.is_ready() is True
    assert storage.calls == ["repository", "restore", "history"]


def test_unreachable_database_is_retried_without_restart(storage: _Storage, clock: _Clock) -> None:
    storage.repository_error = AgentRuntimeStorageUnavailableError(_UNAVAILABLE)

    with pytest.raises(AgentRuntimeStorageUnavailableError, match="DPY-6005"):
        storage_bootstrap.ensure_ready()
    # 失敗の直後は接続を試さずに同じ理由を返す（要求ごとに接続を待たない）。
    with pytest.raises(AgentRuntimeStorageUnavailableError, match="DPY-6005"):
        storage_bootstrap.ensure_ready()
    assert storage.calls == ["repository"]

    # repository は読めたが、定義の読み込みで接続が切れた。
    storage.repository_error = None
    storage.restore_error = RuntimeError("DPY-4011: the database or network closed the connection")
    clock.advance(storage_bootstrap.FAILURE_COOLDOWN_SECONDS)
    with pytest.raises(AgentRuntimeStorageUnavailableError, match="DPY-4011"):
        storage_bootstrap.ensure_ready()
    assert storage_bootstrap.is_ready() is False

    storage.restore_error = None
    clock.advance(storage_bootstrap.FAILURE_COOLDOWN_SECONDS)
    storage_bootstrap.ensure_ready()

    assert storage_bootstrap.is_ready() is True
    assert storage.calls == [
        "repository",
        "repository",
        "restore",
        "repository",
        "restore",
        "history",
    ]


def test_non_connection_errors_of_the_definitions_do_not_block(storage: _Storage) -> None:
    """定義の読み込みの接続以外の障害は、今までどおり記録して先へ進む。"""
    storage.restore_error = RuntimeError("ORA-01031: insufficient privileges")

    storage_bootstrap.ensure_ready()

    assert storage_bootstrap.is_ready() is True
    assert storage.calls == ["repository", "restore", "history"]


def test_corrupt_checkpoint_returns_how_to_fix_it(storage: _Storage) -> None:
    storage.repository_error = AgentRuntimeCheckpointCorruptError(
        "Agent の保存先の checkpoint（CHECKPOINT_KEY='default'）を読み込めません。"
    )

    with pytest.raises(AgentRuntimeStorageUnavailableError, match="CHECKPOINT_KEY='default'"):
        storage_bootstrap.ensure_ready()


def test_unexpected_errors_point_to_the_log(storage: _Storage) -> None:
    storage.repository_error = ValueError("wallet=/secret/path")

    with pytest.raises(AgentRuntimeStorageUnavailableError) as caught:
        storage_bootstrap.ensure_ready()

    assert "agent_storage_load_failed" in str(caught.value)
    assert "/secret/path" not in str(caught.value)


def test_definitions_follow_the_memory_fallback_of_a_corrupt_checkpoint(
    storage: _Storage, monkeypatch: MonkeyPatch
) -> None:
    control_plane_store.set_control_plane_store(MemoryItemStore())
    monkeypatch.setattr(storage_backend, "fell_back_to_memory", lambda: True)
    try:
        storage_bootstrap.ensure_ready()
        # 先に作った store を捨て、Run と同じ保存先（memory）で作り直す。
        assert control_plane_store._store is None
    finally:
        control_plane_store.set_control_plane_store(None)


def test_business_api_returns_503_until_the_database_is_reachable(
    storage: _Storage, clock: _Clock
) -> None:
    from security_support import client

    storage.repository_error = AgentRuntimeStorageUnavailableError(_UNAVAILABLE)

    response = client.get("/api/agents")

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error_code"] == "agent_storage_unavailable"
    assert body["error_messages"] == [_UNAVAILABLE]
    assert response.headers["Retry-After"] == "15"
    # DB ゲート・稼働確認は読み込みを待たない。
    assert client.get("/api/health").status_code == 200
    assert storage.calls == ["repository"]

    storage.repository_error = None
    clock.advance(storage_bootstrap.FAILURE_COOLDOWN_SECONDS)
    response = client.get("/api/agents")

    assert response.status_code == 200, response.text
    assert storage_bootstrap.is_ready() is True


def test_background_load_retries_until_ready(storage: _Storage, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(storage_bootstrap, "RETRY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(storage_bootstrap, "FAILURE_COOLDOWN_SECONDS", 0.0)
    failures = iter([AgentRuntimeStorageUnavailableError(_UNAVAILABLE)] * 2)
    real_get: Callable[[], Any] = runtime.get_runtime_repository

    def flaky() -> Any:
        error = next(failures, None)
        if error is not None:
            raise error
        return real_get()

    monkeypatch.setattr(runtime, "get_runtime_repository", flaky)

    storage_bootstrap.start_background_load()
    try:
        deadline = time.monotonic() + 5
        while not storage_bootstrap.is_ready() and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        storage_bootstrap.stop_background_load()

    assert storage_bootstrap.is_ready() is True
    assert "restore" in storage.calls
