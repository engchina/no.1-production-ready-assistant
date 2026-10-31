"""Database gate と NL2SQL snapshot persistence の回帰テスト。"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.api.health as health_routes
import app.features.nl2sql.router as nl2sql_routes
from app.clients.oracle import OracleConnectionTimeoutError
from app.features.nl2sql.models import DbAdminObjectPage, Nl2SqlProfile
from app.features.nl2sql.oracle_adapter import OracleAdapterError
from app.features.nl2sql.service import (
    Nl2SqlPersistenceUnavailable,
    Nl2SqlRepositoryOperationFailed,
    Nl2SqlService,
)
from app.main import app
from app.readiness import (
    READINESS_OK,
    READINESS_WALLET_NOT_FOUND,
    oracle_readiness_check,
    readiness_checks,
)
from app.settings import get_settings


class _ControllableStore:
    mode = "oracle"

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self.snapshot = copy.deepcopy(snapshot)
        self.fail_load = False
        self.fail_save = False
        self.load_calls = 0
        self.save_calls = 0

    def load_snapshot(self) -> dict[str, Any] | None:
        self.load_calls += 1
        if self.fail_load:
            raise RuntimeError("ORA-12514: secret connect descriptor")
        return copy.deepcopy(self.snapshot)

    def save_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError("ORA-03113: connection lost")
        self.snapshot = copy.deepcopy(snapshot)

    def check(self) -> tuple[bool, str]:
        return (not self.fail_load, "ok" if not self.fail_load else "unreachable")


# Walletless TLS の DSN は Wallet の別名ではなく TCPS 接続文字列 / Easy Connect にする（#325）。
WALLETLESS_DSN = "adb.example.oraclecloud.com:1522/service_high"


def _settings(**updates: Any) -> Any:
    return get_settings().model_copy(update=updates)


def _status_fields(data: dict[str, Any]) -> dict[str, Any]:
    """3製品共通の応答（#325）のうち、NL2SQL が値を返す項目だけを取り出す。"""
    assert data["schema_status"] is None
    assert data["adb_lifecycle_state"] is None
    return {key: data[key] for key in ("status", "check", "detail")}


async def _get_database_status() -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get("/api/ready/database")


@pytest.mark.asyncio
async def test_database_ready_allows_deterministic_memory_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def probe(_settings: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="deterministic",
            nl2sql_persistence_mode="memory",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)

    response = await _get_database_status()

    assert response.status_code == 200
    assert _status_fields(response.json()["data"]) == {
        "status": "ok",
        "check": "ok",
        "detail": "memory",
    }
    assert called is False


@pytest.mark.asyncio
async def test_database_ready_reports_not_configured_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def probe(_settings: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="oracle",
            nl2sql_persistence_mode="oracle",
            oracle_user="",
            oracle_dsn="",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)

    response = await _get_database_status()

    assert response.status_code == 200
    assert _status_fields(response.json()["data"]) == {
        "status": "not_configured",
        "check": "missing",
        "detail": None,
    }
    assert called is False


def test_generic_readiness_wallet_mtls_requires_wallet_files_even_with_password(
    tmp_path: Path,
) -> None:
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("nl2sqldb_high=(DESCRIPTION=...)\n", encoding="utf-8")
    settings = _settings(
        nl2sql_runtime_mode="oracle",
        nl2sql_persistence_mode="oracle",
        oracle_user="APP",
        oracle_password="secret",
        oracle_dsn="nl2sqldb_high",
        oracle_connection_security="wallet_mtls",
        oracle_wallet_dir=str(wallet_dir),
        oracle_driver_mode="thin",
    )

    assert oracle_readiness_check(settings) == READINESS_WALLET_NOT_FOUND
    assert readiness_checks(settings) == {"oracle": READINESS_WALLET_NOT_FOUND}

    (wallet_dir / "ewallet.pem").write_text("dummy", encoding="utf-8")

    assert oracle_readiness_check(settings) == READINESS_OK


def test_generic_readiness_walletless_tls_allows_password_without_wallet(
    tmp_path: Path,
) -> None:
    settings = _settings(
        nl2sql_runtime_mode="oracle",
        nl2sql_persistence_mode="oracle",
        oracle_user="APP",
        oracle_password="secret",
        oracle_dsn="adb.example.oraclecloud.com:1522/nl2sqldb_high",
        oracle_connection_security="walletless_tls",
        oracle_wallet_dir=str(tmp_path / "missing-wallet"),
    )

    assert oracle_readiness_check(settings) == READINESS_OK
    assert readiness_checks(settings) == {"oracle": READINESS_OK}


@pytest.mark.asyncio
async def test_database_ready_wallet_mtls_missing_files_skips_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("nl2sqldb_high=(DESCRIPTION=...)\n", encoding="utf-8")

    async def probe(_settings: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="oracle",
            nl2sql_persistence_mode="oracle",
            oracle_user="APP",
            oracle_password="secret",
            oracle_dsn="nl2sqldb_high",
            oracle_connection_security="wallet_mtls",
            oracle_wallet_dir=str(wallet_dir),
            oracle_driver_mode="thin",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)

    response = await _get_database_status()

    assert response.status_code == 200
    assert _status_fields(response.json()["data"]) == {
        "status": "not_configured",
        "check": READINESS_WALLET_NOT_FOUND,
        "detail": None,
    }
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "check"),
    [
        # システム設定画面と同じ判定（#325）: Walletless TLS で Wallet の別名は使えない。
        (
            {"oracle_connection_security": "walletless_tls", "oracle_dsn": "service_high"},
            "walletless_tls_dsn_required",
        ),
        # DeepSec は Thin mode だけ対応する（NL2SQL 固有の判定を注入している）。
        (
            {
                "oracle_connection_security": "walletless_tls",
                "oracle_dsn": WALLETLESS_DSN,
                "oracle_deepsec_enabled": True,
                "oracle_driver_mode": "thick",
            },
            "invalid_configuration",
        ),
    ],
)
async def test_database_ready_uses_system_settings_readiness(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
    check: str,
) -> None:
    called = False

    async def probe(_settings: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="oracle",
            nl2sql_persistence_mode="oracle",
            oracle_user="APP",
            oracle_password="secret",
            **updates,
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)

    response = await _get_database_status()

    assert _status_fields(response.json()["data"]) == {
        "status": "not_configured",
        "check": check,
        "detail": None,
    }
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            OracleConnectionTimeoutError("private timeout detail"),
            "Oracle connection probe timed out.",
        ),
        (
            OracleAdapterError("ORA-12514: listener rejected secret descriptor"),
            "Oracle connection probe failed (ORA-12514).",
        ),
    ],
)
async def test_database_ready_redacts_probe_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    detail: str,
) -> None:
    async def probe(_settings: Any) -> None:
        raise error

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="oracle",
            nl2sql_persistence_mode="oracle",
            oracle_user="APP",
            oracle_password="secret",
            oracle_dsn=WALLETLESS_DSN,
            oracle_connection_security="walletless_tls",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)

    response = await _get_database_status()

    assert response.status_code == 200
    assert _status_fields(response.json()["data"]) == {
        "status": "unreachable",
        "check": "ok",
        "detail": detail,
    }
    assert "secret" not in response.text
    record = next(r for r in caplog.records if r.message == "database_status_unreachable")
    assert record.__dict__["summary"]
    assert record.__dict__["suggested_action"]
    assert record.exc_info  # 既存の調査用 traceback も保持する。


@pytest.mark.asyncio
async def test_database_ready_reports_successful_oracle_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def probe(_settings: Any) -> None:
        return None

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="deterministic",
            nl2sql_persistence_mode="oracle",
            oracle_user="APP",
            oracle_password="secret",
            oracle_dsn=WALLETLESS_DSN,
            oracle_connection_security="walletless_tls",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)
    monkeypatch.setattr(
        health_routes,
        "nl2sql_service",
        SimpleNamespace(uses_incremental_store=False),
    )

    response = await _get_database_status()

    assert response.status_code == 200
    assert _status_fields(response.json()["data"]) == {
        "status": "ok",
        "check": "ok",
        "detail": None,
    }


@pytest.mark.asyncio
async def test_database_ready_distinguishes_pending_migration_from_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def probe(_settings: Any) -> None:
        return None

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="oracle",
            nl2sql_persistence_mode="oracle",
            oracle_user="APP",
            oracle_password="secret",
            oracle_dsn=WALLETLESS_DSN,
            oracle_connection_security="walletless_tls",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)
    monkeypatch.setattr(
        health_routes,
        "nl2sql_service",
        SimpleNamespace(
            uses_incremental_store=True,
            check_incremental_store=lambda: (False, "migration 3 is required"),
        ),
    )

    response = await _get_database_status()

    assert response.status_code == 200
    assert _status_fields(response.json()["data"]) == {
        "status": "setup_required",
        "check": "migration_required",
        "detail": "migration 3 is required",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RuntimeError("DPY-4005: timed out waiting for the connection pool"), "unreachable"),
        (RuntimeError("ORA-03113: end-of-file on communication channel"), "unreachable"),
        (RuntimeError("ORA-00942: table or view does not exist"), "setup_required"),
    ],
    ids=["pool_timeout", "connection_lost", "dictionary"],
)
async def test_database_ready_classifies_incremental_store_check_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: str,
) -> None:
    """migration の確認の失敗は、接続の失敗なら unreachable、それ以外は setup_required（#820）。"""

    async def probe(_settings: Any) -> None:
        return None

    def check_incremental_store() -> tuple[bool, str]:
        raise error

    monkeypatch.setattr(
        health_routes,
        "get_settings",
        lambda: _settings(
            nl2sql_runtime_mode="oracle",
            nl2sql_persistence_mode="oracle",
            oracle_user="APP",
            oracle_password="secret",
            oracle_dsn=WALLETLESS_DSN,
            oracle_connection_security="walletless_tls",
        ),
    )
    monkeypatch.setattr(health_routes, "test_oracle_connection", probe)
    monkeypatch.setattr(
        health_routes,
        "nl2sql_service",
        SimpleNamespace(
            uses_incremental_store=True,
            check_incremental_store=check_incremental_store,
        ),
    )

    response = await _get_database_status()

    data = response.json()["data"]
    assert data["status"] == expected_status
    assert data["check"] == "migration_check_failed"


def test_startup_load_failure_never_writes_default_snapshot() -> None:
    store = _ControllableStore()
    store.fail_load = True

    service = Nl2SqlService(store=store)

    assert store.load_calls == 1
    assert store.save_calls == 0
    assert service.persistence_status().ready is False
    assert service.persistence_status().reason_code == "snapshot_load_failed"


def test_profile_save_failure_rolls_back_ghost_state_and_closes_writes() -> None:
    store = _ControllableStore()
    service = Nl2SqlService(store=store)
    baseline_ids = {profile.id for profile in service.list_profiles(include_archived=True)}
    store.fail_save = True

    with pytest.raises(Nl2SqlPersistenceUnavailable):
        service.create_profile(Nl2SqlProfile(id="ghost", name="Ghost profile"))

    assert {profile.id for profile in service.list_profiles(include_archived=True)} == baseline_ids
    assert service.persistence_status().writable is False
    assert service.persistence_status().reason_code == "snapshot_save_failed"


def test_recovery_reloads_existing_snapshot_before_reopening() -> None:
    durable_store = _ControllableStore()
    writer = Nl2SqlService(store=durable_store)
    writer.create_profile(Nl2SqlProfile(id="persisted", name="保存済みプロファイル"))
    snapshot = copy.deepcopy(durable_store.snapshot)

    recovering_store = _ControllableStore(snapshot)
    recovering_store.fail_load = True
    recovered = Nl2SqlService(store=recovering_store)
    assert recovering_store.save_calls == 0
    with pytest.raises(Nl2SqlPersistenceUnavailable):
        recovered.ensure_persistence_available()

    recovering_store.fail_load = False
    status = recovered.recover_persistence()

    assert status.ready is True
    assert status.writable is True
    assert {profile.id for profile in recovered.list_profiles(include_archived=True)} >= {
        "persisted"
    }


def test_legacy_classifier_registry_migration_keeps_only_the_active_model() -> None:
    active_artifact = {
        "version": "active-model",
        "updated_at": "2026-07-19T08:00:00+00:00",
        "model_base64": "YWN0aXZl",
        "categories": ["監査", "標準業務プロファイル"],
        "embedding_model": "deterministic-hash-1536",
        "vector_dimension": 1536,
        "metrics": {},
    }
    store = _ControllableStore(
        {
            "classifier_artifact": active_artifact,
            "classifier_model_registry": {
                "active-model": active_artifact,
                "archived-model": {
                    **active_artifact,
                    "version": "archived-model",
                },
            },
        }
    )

    service = Nl2SqlService(store=store)

    assert service.classifier_status().classifier_version == "active-model"
    assert store.snapshot is not None
    assert store.snapshot["classifier_artifact"]["version"] == "active-model"
    assert "classifier_model_registry" not in store.snapshot
    assert not hasattr(service, "_classifier_model_registry")


def test_legacy_classifier_registry_does_not_reactivate_an_archived_model() -> None:
    store = _ControllableStore(
        {
            "classifier_artifact": None,
            "classifier_model_registry": {
                "archived-model": {
                    "version": "archived-model",
                    "model_base64": "YXJjaGl2ZWQ=",
                    "categories": ["監査", "標準業務プロファイル"],
                }
            },
        }
    )

    service = Nl2SqlService(store=store)

    assert service.classifier_status().ready is False
    assert service.classifier_status().classifier_version == ""
    assert store.snapshot is not None
    assert store.snapshot["classifier_artifact"] is None
    assert "classifier_model_registry" not in store.snapshot


@pytest.mark.asyncio
async def test_profile_api_returns_retryable_503_without_ghost_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ControllableStore()
    service = Nl2SqlService(store=store)
    baseline_ids = {profile.id for profile in service.list_profiles(include_archived=True)}
    store.fail_save = True
    monkeypatch.setattr(nl2sql_routes, "nl2sql_service", service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/nl2sql/profiles",
            json={"name": "save_blocked_profile"},
        )
        blocked_read = await client.get("/api/nl2sql/profiles")
        persistence = await client.get("/api/nl2sql/persistence")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["data"] is None
    assert response.json()["error_messages"]
    assert response.json()["error_code"] == "snapshot_save_failed"
    assert blocked_read.status_code == 503
    assert blocked_read.headers["Retry-After"] == "5"
    assert persistence.status_code == 200
    assert persistence.json()["data"]["ready"] is False
    assert {profile.id for profile in service.list_profiles(include_archived=True)} == baseline_ids


@pytest.mark.asyncio
async def test_repository_programming_error_returns_local_500_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        def ensure_persistence_available(self) -> None:
            return None

        def list_db_admin_objects_page(self, **_kwargs: Any) -> None:
            raise Nl2SqlRepositoryOperationFailed("schema_object_query_failed")

    monkeypatch.setattr(nl2sql_routes, "nl2sql_service", Service())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/nl2sql/db-admin/objects?limit=10")

    assert response.status_code == 500
    assert "Retry-After" not in response.headers
    assert response.json()["error_code"] == "schema_object_query_failed"
    assert "ORA-" not in response.text


@pytest.mark.asyncio
async def test_db_admin_objects_route_passes_owner_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        kwargs: dict[str, Any] = {}

        def ensure_persistence_available(self) -> None:
            return None

        def list_db_admin_objects_page(self, **kwargs: Any) -> DbAdminObjectPage:
            self.kwargs = kwargs
            return DbAdminObjectPage(owner=kwargs.get("owner", ""), items=[])

    service = Service()
    monkeypatch.setattr(nl2sql_routes, "nl2sql_service", service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/nl2sql/db-admin/objects?limit=10&owner=ADMIN")

    assert response.status_code == 200
    assert service.kwargs["owner"] == "ADMIN"


@pytest.mark.asyncio
async def test_workspace_context_changes_only_with_connection_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        nl2sql_runtime_mode="deterministic",
        nl2sql_persistence_mode="memory",
        oracle_user="OWNER_A",
        oracle_dsn="db-a/service",
    )
    monkeypatch.setattr(health_routes, "get_settings", lambda: settings)
    first = (await _get_database_status()).json()["data"]
    assert len(first["context_id"]) == 64
    assert "OWNER_A" not in str(first)
    assert "db-a/service" not in str(first)
    settings.oracle_password = "changed-test-value"
    assert (await _get_database_status()).json()["data"]["context_id"] == first["context_id"]
    settings.oracle_user = "OWNER_B"
    assert (await _get_database_status()).json()["data"]["context_id"] != first["context_id"]
    settings.oracle_user = "OWNER_A"
    settings.oracle_dsn = "db-b/service"
    assert (await _get_database_status()).json()["data"]["context_id"] != first["context_id"]
