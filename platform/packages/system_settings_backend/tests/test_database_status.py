"""DB の状態 API（`GET /ready/database`。#325）の単体テスト。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pr_system_settings.database_status import (
    DatabaseSchemaProbeResult,
    DatabaseStatusCache,
    DatabaseStatusData,
    build_database_status_router,
    clear_database_status_cache,
    database_context_id,
    database_status_cache_key,
    safe_connection_error_detail,
)

PEM = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
TNSNAMES = "ragdb_high = (description=(address=(protocol=tcps)(port=1522)(host=adb.example)))\n"


class FakeSettings(BaseModel):
    """製品の Settings のうち DB の状態 API が読む属性だけを持つ。"""

    oracle_user: str = "APP"
    oracle_password: str = "db-secret-password"
    oracle_dsn: str = "adb.example.oraclecloud.com:1522/secret_service_high"
    oracle_driver_mode: str = "thin"
    oracle_connection_security: str = "walletless_tls"
    oracle_wallet_dir: str = ""
    oracle_wallet_password: str = ""

    @property
    def resolved_oracle_wallet_dir(self) -> str:
        return self.oracle_wallet_dir


class Harness:
    def __init__(self, **kwargs: Any) -> None:
        self.settings = FakeSettings()
        self.calls: list[str] = []
        self.connect_error: Exception | None = None
        self.probe_result = DatabaseSchemaProbeResult()
        self.probe_error: Exception | None = None

        async def test_connection(_settings: Any) -> None:
            self.calls.append("connect")
            if self.connect_error is not None:
                raise self.connect_error

        async def schema_probe(_settings: Any) -> DatabaseSchemaProbeResult:
            self.calls.append("probe")
            if self.probe_error is not None:
                raise self.probe_error
            return self.probe_result

        kwargs.setdefault("schema_probe", schema_probe)
        app = FastAPI()
        app.include_router(
            build_database_status_router(
                get_settings=lambda: self.settings,
                test_connection=test_connection,
                **kwargs,
            ),
            prefix="/api",
        )
        self.client = TestClient(app)

    def get(self) -> dict[str, Any]:
        response = self.client.get("/api/ready/database")
        assert response.status_code == 200
        data: dict[str, Any] = response.json()["data"]
        return data


def test_ok_runs_readiness_connection_and_probe_in_order() -> None:
    harness = Harness()

    data = harness.get()

    assert data["status"] == "ok"
    assert data["check"] == "ok"
    assert data["detail"] is None
    assert harness.calls == ["connect", "probe"]
    assert set(data) == {
        "status",
        "check",
        "detail",
        "context_id",
        "schema_status",
        "adb_lifecycle_state",
    }


def test_ok_without_schema_probe() -> None:
    harness = Harness(schema_probe=None)

    data = harness.get()

    assert data["status"] == "ok"
    assert harness.calls == ["connect"]


def test_not_configured_skips_connection() -> None:
    harness = Harness()
    harness.settings.oracle_user = ""

    data = harness.get()

    assert data == {
        "status": "not_configured",
        "check": "missing",
        "detail": None,
        "context_id": data["context_id"],
        "schema_status": None,
        "adb_lifecycle_state": None,
    }
    assert harness.calls == []


def test_not_configured_uses_the_same_readiness_as_system_settings(tmp_path: Path) -> None:
    """Wallet mTLS でパスワードがあっても、Wallet が不備なら接続を試さない（RAG の変更点）。"""
    harness = Harness()
    harness.settings.oracle_connection_security = "wallet_mtls"
    harness.settings.oracle_dsn = "ragdb_high"
    harness.settings.oracle_wallet_dir = str(tmp_path / "wallet")

    assert harness.get()["check"] == "wallet_not_found"
    assert harness.get()["status"] == "not_configured"

    wallet = tmp_path / "wallet"
    wallet.mkdir()
    (wallet / "tnsnames.ora").write_text(TNSNAMES, encoding="utf-8")
    (wallet / "ewallet.pem").write_text(PEM, encoding="utf-8")
    assert harness.get()["status"] == "ok"

    harness.settings.oracle_dsn = "otherdb_high"
    assert harness.get()["check"] == "invalid"
    assert harness.calls == ["connect", "probe"]


def test_walletless_tls_rejects_wallet_alias_dsn() -> None:
    harness = Harness()
    harness.settings.oracle_dsn = "service_high"

    data = harness.get()

    assert data["status"] == "not_configured"
    assert data["check"] == "walletless_tls_dsn_required"
    assert harness.calls == []


def test_extra_readiness_runs_first() -> None:
    harness = Harness(extra_readiness=lambda _settings: "invalid_configuration")

    data = harness.get()

    assert data["status"] == "not_configured"
    assert data["check"] == "invalid_configuration"
    assert harness.calls == []


def test_unreachable_does_not_leak_connection_details(caplog: pytest.LogCaptureFixture) -> None:
    harness = Harness(connection_failure_log_extra=lambda _exc: {"summary": "listener"})
    harness.connect_error = RuntimeError(
        "DPY-6005: cannot connect to database (CONNECTION_ID=abc). "
        "ORA-12514: listener at adb.example.oraclecloud.com:1522 does not know of "
        "service secret_service_high user=APP password=db-secret-password"
    )

    with caplog.at_level(logging.WARNING):
        data = harness.get()

    assert data["status"] == "unreachable"
    assert data["check"] == "ok"
    assert data["detail"] == "Oracle connection probe failed (ORA-12514)."
    body = json.dumps(data, ensure_ascii=False)
    for secret in (
        "adb.example.oraclecloud.com",
        "1522",
        "secret_service_high",
        "CONNECTION_ID",
        "db-secret-password",
        "APP",
    ):
        assert secret not in body
    assert harness.calls == ["connect"]
    record = next(r for r in caplog.records if r.message == "database_status_unreachable")
    assert record.__dict__["summary"] == "listener"
    assert record.__dict__["exception_type"] == "RuntimeError"
    assert record.exc_info


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (TimeoutError("host secret"), "Oracle connection probe timed out."),
        (
            type("OracleConnectionTimeoutError", (RuntimeError,), {})("host secret"),
            "Oracle connection probe timed out.",
        ),
        (RuntimeError("DPY-6005: host secret"), "Oracle connection probe failed (DPY-6005)."),
        (RuntimeError("host secret"), "Oracle connection probe failed."),
    ],
)
def test_safe_connection_error_detail(error: Exception, detail: str) -> None:
    assert safe_connection_error_detail(error) == detail


def test_setup_required_from_schema_probe() -> None:
    harness = Harness()
    harness.probe_result = DatabaseSchemaProbeResult(
        status="setup_required", schema_status="missing"
    )

    data = harness.get()

    assert data["status"] == "setup_required"
    assert data["check"] == "ok"
    assert data["schema_status"] == "missing"


def test_schema_probe_can_override_check_and_status() -> None:
    harness = Harness()
    harness.probe_result = DatabaseSchemaProbeResult(
        status="unreachable", check="migration_check_failed", detail="Oracle connection probe."
    )

    data = harness.get()

    assert data["status"] == "unreachable"
    assert data["check"] == "migration_check_failed"
    assert data["detail"] == "Oracle connection probe."


def test_schema_probe_error_is_setup_required_with_safe_detail() -> None:
    harness = Harness()
    harness.probe_error = RuntimeError("ORA-00942: table SECRET_OWNER.RAG_X does not exist")

    data = harness.get()

    assert data["status"] == "setup_required"
    assert data["check"] == "schema_check_failed"
    assert data["detail"] == "システムテーブルの状態を確認できませんでした (ORA-00942)。"
    assert "SECRET_OWNER" not in json.dumps(data)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("DPY-4005: timed out waiting for the connection pool to return a connection"),
        RuntimeError("DPY-6005: cannot connect to database (CONNECTION_ID=x). ORA-12541"),
        RuntimeError("ORA-03113: end-of-file on communication channel"),
        TimeoutError(),
    ],
    ids=["pool_timeout", "cannot_connect", "eof", "timeout"],
)
def test_schema_probe_connection_error_is_unreachable(error: Exception) -> None:
    """接続確認の後に接続・pool が失敗しても「初期化が必要」と案内しない（#820）。"""
    harness = Harness()
    harness.probe_error = error

    data = harness.get()

    assert data["status"] == "unreachable"
    assert data["check"] == "ok"
    assert data["detail"].startswith("Oracle connection probe")


def test_wrapped_pool_timeout_from_schema_probe_is_unreachable() -> None:
    """製品が包んだ例外（`raise ... from exc`）も、原因のコードで分類する。"""
    harness = Harness()
    try:
        try:
            raise RuntimeError("DPY-4005: timed out waiting for the connection pool")
        except RuntimeError as inner:
            raise ValueError("システムテーブルの状態を取得できません。") from inner
    except ValueError as outer:
        harness.probe_error = outer

    data = harness.get()

    assert data["status"] == "unreachable"
    assert data["detail"] == "Oracle connection probe failed (DPY-4005)."


def test_adb_lifecycle_is_added_only_when_unreachable() -> None:
    """接続できないときだけ ADB の状態を返す（画面が停止中・起動中を出し分ける。#820）。"""
    calls: list[str] = []

    async def lifecycle(_settings: Any) -> str | None:
        calls.append("adb")
        return "STOPPED"

    harness = Harness(adb_lifecycle=lifecycle, ok_cache_seconds=0)
    assert harness.get()["adb_lifecycle_state"] is None
    harness.probe_result = DatabaseSchemaProbeResult(status="setup_required")
    assert harness.get()["adb_lifecycle_state"] is None
    assert calls == []

    harness.connect_error = RuntimeError("DPY-6005: cannot connect to database")
    data = harness.get()

    assert data["status"] == "unreachable"
    assert data["adb_lifecycle_state"] == "STOPPED"
    assert calls == ["adb"]

    harness.connect_error = None
    harness.probe_error = RuntimeError("DPY-4005: timed out waiting for the connection pool")
    assert harness.get()["adb_lifecycle_state"] == "STOPPED"


def test_default_adb_lifecycle_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADB OCID が無ければ OCI を呼ばない。取得の失敗は None（判定は変えない）。"""
    from types import SimpleNamespace

    from pr_system_settings import database_status as module

    requested: list[str] = []
    outcome: dict[str, Any] = {"state": "starting"}

    class FakeClient:
        def __init__(self, settings: Any) -> None:
            self.settings = settings

        async def get_autonomous_database(self, adb_ocid: str) -> Any:
            requested.append(adb_ocid)
            if isinstance(outcome["state"], Exception):
                raise outcome["state"]
            return SimpleNamespace(lifecycle_state=outcome["state"])

    monkeypatch.setattr(module, "OciDatabaseClient", FakeClient)

    def run(settings: Any) -> str | None:
        return asyncio.run(module.adb_lifecycle_state(settings))

    assert run(SimpleNamespace(oracle_adb_ocid="")) is None
    assert requested == []
    settings = SimpleNamespace(oracle_adb_ocid=" ocid1.autonomousdatabase.oc1..x ")
    assert run(settings) == "STARTING"
    assert requested == ["ocid1.autonomousdatabase.oc1..x"]
    outcome["state"] = RuntimeError("NotAuthorizedOrNotFound")
    assert run(settings) is None


def test_short_circuit_skips_every_check() -> None:
    harness = Harness(
        short_circuit=lambda _settings: DatabaseStatusData(
            status="ok", check="ok", detail="memory"
        ),
    )
    harness.settings.oracle_user = ""

    data = harness.get()

    assert data["status"] == "ok"
    assert data["detail"] == "memory"
    assert len(data["context_id"]) == 64
    assert harness.calls == []


def test_short_circuit_none_continues() -> None:
    harness = Harness(short_circuit=lambda _settings: None)

    assert harness.get()["status"] == "ok"
    assert harness.calls == ["connect", "probe"]


def test_context_id_changes_only_with_connection_identity() -> None:
    harness = Harness()
    first = harness.get()["context_id"]
    assert len(first) == 64
    assert "APP" not in first

    harness.settings.oracle_password = "changed"
    assert harness.get()["context_id"] == first
    harness.settings.oracle_user = "OTHER"
    assert harness.get()["context_id"] != first


def test_context_fields_can_be_injected() -> None:
    harness = Harness(context_fields=lambda settings: ["mode", settings.oracle_user])

    assert harness.get()["context_id"] == database_context_id(["mode", "APP"])


# ---- `ok` の結果の cache（#793） ------------------------------------------------


def test_ok_is_cached_and_skips_connection_and_probe() -> None:
    """全画面の再読み込みのたびに接続確認とシステムテーブルの確認をやり直さない。"""
    harness = Harness()

    first = harness.get()
    for _ in range(3):
        assert harness.get() == first

    assert harness.calls == ["connect", "probe"]


def test_cache_can_be_disabled() -> None:
    harness = Harness(ok_cache_seconds=0)

    harness.get()
    harness.get()

    assert harness.calls == ["connect", "probe", "connect", "probe"]


def test_failures_are_not_cached() -> None:
    """不通・準備が必要は cache せず、直したらすぐに ok になる。"""
    harness = Harness()
    harness.connect_error = RuntimeError("DPY-6005: cannot connect")
    assert harness.get()["status"] == "unreachable"
    assert harness.get()["status"] == "unreachable"
    harness.connect_error = None
    harness.probe_result = DatabaseSchemaProbeResult(
        status="setup_required", schema_status="missing"
    )
    assert harness.get()["status"] == "setup_required"
    harness.probe_result = DatabaseSchemaProbeResult()
    assert harness.get()["status"] == "ok"

    assert harness.calls == ["connect", "connect", "connect", "probe", "connect", "probe"]


def test_cache_still_checks_settings_every_time() -> None:
    """設定の不足は cache より先に判定する（接続設定を消したらすぐ not_configured）。"""
    harness = Harness()
    assert harness.get()["status"] == "ok"
    harness.settings.oracle_user = ""

    assert harness.get()["status"] == "not_configured"
    assert harness.calls == ["connect", "probe"]


def test_cache_is_keyed_by_connection_and_credentials() -> None:
    harness = Harness()
    harness.get()

    # 資格情報だけが変わったとき（context_id は同じ）も確かめ直す。
    harness.settings.oracle_password = "rotated-password"
    harness.get()
    harness.settings.oracle_dsn = "other.example.oraclecloud.com:1522/other_high"
    harness.get()

    assert harness.calls == ["connect", "probe"] * 3


def test_clear_database_status_cache() -> None:
    harness = Harness()
    harness.get()
    clear_database_status_cache()
    harness.get()

    assert harness.calls == ["connect", "probe"] * 2


def test_cache_expires() -> None:
    now = [100.0]
    cache = DatabaseStatusCache(30, clock=lambda: now[0])
    data = DatabaseStatusData(status="ok", check="ok", context_id="ctx")
    cache.put("key", data)

    assert cache.get("key") == data
    now[0] += 30
    assert cache.get("key") is None


def test_cache_key_does_not_contain_secrets() -> None:
    settings = FakeSettings()
    key = database_status_cache_key(settings, "ctx")

    assert settings.oracle_password not in key
    assert len(key) == 64
