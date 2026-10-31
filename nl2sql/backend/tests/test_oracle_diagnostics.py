"""DB 不要で接続障害ログの診断・秘匿・出力経路を検証する。"""

import json
import logging
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pr_system_settings import database as shared_database
from pythonjsonlogger.json import JsonFormatter

from app.clients.oracle import OracleConnectionTimeoutError
from app.clients.oracle_diagnostics import oracle_connection_diagnostics
from app.features.nl2sql.oracle_adapter import OracleAdapterError, OracleNl2SqlAdapter
from app.features.settings import router as settings_router
from app.settings import Settings


@pytest.mark.parametrize(
    ("error", "category", "action"),
    [
        (
            RuntimeError("DPY-6005: DPY-6001: ORA-12514"),
            "service_not_registered",
            "PLATFORM_ORACLE_DSN",
        ),
        (RuntimeError("DPY-6001"), "service_not_registered", "tnsnames.ora"),
        (RuntimeError("ORA-01017"), "invalid_credentials", "PLATFORM_ORACLE_PASSWORD"),
        (RuntimeError("ORA-28000"), "account_unavailable", "DB 管理者"),
        (RuntimeError("ORA-28001"), "account_unavailable", "有効期限"),
        (RuntimeError("DPY-4000"), "dsn_resolution_failed", "PLATFORM_ORACLE_WALLET_DIR"),
        (RuntimeError("ORA-12506"), "network_access_denied", "ACL"),
        (RuntimeError("DPI-1047"), "client_unavailable", "PLATFORM_ORACLE_DRIVER_MODE"),
        (RuntimeError("DPY-4011"), "connection_closed", "切断"),
        (RuntimeError("DPY-6005: timed out"), "connection_timeout", "TIMEOUT_SECONDS"),
        (OracleConnectionTimeoutError("private detail"), "connection_timeout", "TIMEOUT_SECONDS"),
        (RuntimeError("ORA-12170"), "connection_timeout", "TIMEOUT_SECONDS"),
        (RuntimeError("ORA-12541"), "connection_unavailable", "ホスト・ポート"),
        (RuntimeError("DPY-6005"), "connection_unavailable", "原因を特定できない"),
        (RuntimeError("private detail"), "unknown", "未特定"),
        (RuntimeError("ORA-00942"), "unknown", "実行処理"),
    ],
)
def test_diagnostics(error: Exception, category: str, action: str) -> None:
    result = oracle_connection_diagnostics(error)
    assert result["diagnostic_category"] == category
    assert action in str(result["summary"]) + str(result["suggested_action"])
    assert "private detail" not in json.dumps(result)


def test_diagnostics_extract_codes_from_chain_without_raw_values() -> None:
    inner = RuntimeError("dpy-6001: secret-host /private/wallet secret-password (ORA-12514)")
    outer = OracleAdapterError("DPY-6005: secret-user secret-dsn DPY-6005")
    outer.__cause__ = inner
    inner.__cause__ = outer  # 不正な循環チェーンでも終了する。
    result = oracle_connection_diagnostics(outer)
    assert result["oracle_error_codes"] == ["DPY-6005", "DPY-6001", "ORA-12514"]
    assert result["diagnostic_category"] == "service_not_registered"
    assert "secret" not in json.dumps(result)
    assert "/private/wallet" not in json.dumps(result)


def test_suppressed_context_does_not_override_current_error() -> None:
    error = RuntimeError("DPY-6005")
    error.__context__ = RuntimeError("ORA-01017")
    error.__suppress_context__ = True
    assert oracle_connection_diagnostics(error)["oracle_error_codes"] == ["DPY-6005"]


@pytest.mark.parametrize("recover", [True, False])
def test_connect_logs_only_final_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, recover: bool
) -> None:
    adapter = OracleNl2SqlAdapter(
        Settings(
            _env_file=None,
            oracle_user="APP",
            oracle_password="fixture-secret",
            oracle_dsn="localhost/FREEPDB1",
            oracle_connection_security="walletless_tls",
        ),
        connect_attempts=2,
    )
    error = RuntimeError("DPY-6005: timed out fixture-secret")
    driver = MagicMock()
    driver.connect.side_effect = [error, MagicMock() if recover else error]
    monkeypatch.setattr(adapter, "_load_oracledb", lambda: driver)
    monkeypatch.setattr(adapter, "_init_client", lambda _: None)
    monkeypatch.setattr("app.features.nl2sql.oracle_adapter.time.sleep", lambda _: None)
    if recover:
        with adapter.connection():
            pass
    else:
        with pytest.raises(OracleAdapterError), adapter.connection():
            pytest.fail("failed connection must not yield")
    records = [r for r in caplog.records if getattr(r, "event", "") == "oracle_connection_failed"]
    assert len(records) == (0 if recover else 1)
    if not recover:
        record = records[0]
        assert record.__dict__["attempts"] == 2
        assert record.__dict__["diagnostic_category"] == "connection_timeout"
        assert "制限時間" in record.getMessage()
        assert "fixture-secret" not in str(record.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [OracleConnectionTimeoutError("private"), RuntimeError("DPY-6001: private")]
)
async def test_settings_connection_test_logs_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    async def fail(_settings: Settings) -> None:
        raise error

    monkeypatch.setattr(shared_database, "database_readiness", lambda *_: "ok")
    monkeypatch.setattr(settings_router, "test_oracle_connection", fail)
    app = FastAPI()
    app.include_router(settings_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/settings/database/test")
    assert response.json()["data"]["status"] == "failed"
    record = next(r for r in caplog.records if r.message == "database_connection_test_failed")
    assert record.__dict__["summary"]
    assert record.__dict__["suggested_action"]
    assert "private" not in str(record.__dict__)


def test_application_json_logs_render_japanese_without_unicode_escapes() -> None:
    import app.main  # noqa: F401 - 実際のアプリのログ設定を検証する。

    formatter = next(
        h.formatter for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)
    )
    record = logging.makeLogRecord(
        {
            "msg": "接続失敗",
            "levelname": "ERROR",
            **oracle_connection_diagnostics(RuntimeError("DPY-6001")),
        }
    )
    output = formatter.format(record)
    assert "接続失敗" in output
    assert "登録されていません" in output
    assert json.loads(output)["oracle_error_codes"] == ["DPY-6001"]
