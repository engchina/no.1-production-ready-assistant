"""DB に接続できないときの本当の原因の診断（#1744）。fake の oracledb で確かめる。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from pr_backend_core import oracle_connect_diagnosis as diagnosis_module
from pr_backend_core.oracle_connect_diagnosis import (
    diagnose_oracle_connect_failure,
    explain_oracle_connect_error,
    install_oracledb_thread_excepthook,
    probe_connect_kwargs,
    redact_connect_secrets,
)

PASSWORD = "S3cret-Passw0rd#1"
WALLET_PASSWORD = "Wa11et-Passw0rd#2"
POOL_TIMEOUT = "DPY-4005: timed out waiting for the connection pool to return a connection"


class _OracleError(Exception):
    """python-oracledb の例外の形（`args[0].full_code` と文字列）。"""

    def __init__(self, message: str) -> None:
        code = message.split(":", 1)[0]
        super().__init__(type("_Error", (), {"full_code": code, "message": message})())
        self._message = message

    def __str__(self) -> str:
        return self._message


class _Connection:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeOracledb:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.connections: list[_Connection] = []

    def connect(self, **kwargs: Any) -> _Connection:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        connection = _Connection()
        self.connections.append(connection)
        return connection


def _kwargs(**extra: object) -> dict[str, object]:
    return {
        "user": "APP_USER",
        "password": PASSWORD,
        "dsn": "mydb_high",
        "wallet_password": WALLET_PASSWORD,
        **extra,
    }


def _all_text(diagnosis: Any, caplog: pytest.LogCaptureFixture) -> str:
    fields = diagnosis.as_fields() if diagnosis is not None else {}
    records = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    return f"{fields} {diagnosis} {records}"


@pytest.mark.parametrize(
    ("message", "category", "code"),
    [
        (
            "DPY-6005: cannot connect to database (CONNECTION_ID=abc).\ntimed out",
            "unreachable",
            "DPY-6005",
        ),
        (
            "DPY-6005: cannot connect to database (CONNECTION_ID=abc).\n"
            "[Errno 111] Connection refused",
            "unreachable",
            "DPY-6005",
        ),
        (
            "DPY-6005: cannot connect to database (CONNECTION_ID=abc).\n"
            "[SSL] PEM lib (_ssl.c:3900)",
            "wallet",
            "DPY-6005",
        ),
        (
            "ORA-01017: invalid credential or not authorized; logon denied",
            "invalid_credentials",
            "ORA-01017",
        ),
        ("ORA-28000: The account is locked.", "account_locked", "ORA-28000"),
        ("ORA-28001: the password has expired", "password_expired", "ORA-28001"),
        (
            "DPY-6000: Listener refused connection. (Similar to ORA-12506)",
            "acl_rejected",
            "ORA-12506",
        ),
        (
            'DPY-6001: Service "x_high.adb.oraclecloud.com" is not registered with the listener '
            'at host "adb.example.com" port 1522. (Similar to ORA-12514)',
            "service_unavailable",
            "DPY-6001",
        ),
        (
            "ORA-12514: Cannot connect to database. Service x is not registered",
            "service_unavailable",
            "ORA-12514",
        ),
        ('DPY-4000: unable to find "nodb_high" in /wallet/tnsnames.ora', "dsn", "DPY-4000"),
        ("DPY-4027: no configuration directory specified", "wallet", "DPY-4027"),
        ("ORA-29024: Certificate validation failure", "wallet", "ORA-29024"),
    ],
    ids=[
        "timeout",
        "refused",
        "wallet-pem",
        "ora-01017",
        "ora-28000",
        "ora-28001",
        "ora-12506",
        "dpy-6001",
        "ora-12514",
        "dsn-alias",
        "no-config-dir",
        "certificate",
    ],
)
def test_explain_maps_codes_to_japanese_reason_and_action(
    message: str, category: str, code: str
) -> None:
    diagnosis = explain_oracle_connect_error(_OracleError(message))

    assert diagnosis is not None
    assert diagnosis.category == category
    assert diagnosis.code == code
    assert diagnosis.reason.endswith("。")
    assert diagnosis.action
    assert diagnosis.hint == f"{diagnosis.reason}{diagnosis.action}"
    assert diagnosis.as_fields()["cause"] == category


def test_explain_returns_none_for_non_connection_errors() -> None:
    assert explain_oracle_connect_error(_OracleError("ORA-00942: table does not exist")) is None
    assert explain_oracle_connect_error(ValueError("bad value")) is None


def test_stopped_adb_hint_tells_how_to_start_it() -> None:
    diagnosis = explain_oracle_connect_error(
        _OracleError("DPY-6005: cannot connect to database (CONNECTION_ID=abc).\ntimed out")
    )

    assert diagnosis is not None
    assert "ADB が停止しているか" in diagnosis.reason
    assert "起動" in diagnosis.action


def test_pool_timeout_probes_one_direct_connect_and_reports_real_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "tnsnames.ora").write_text(
        "mydb_high = (description= (retry_count=20)(retry_delay=3)"
        "(address=(protocol=tcps)(port=1522)(host=adb.example.com))"
        "(connect_data=(service_name=x_high.adb.oraclecloud.com)))\n",
        encoding="utf-8",
    )
    fake = _FakeOracledb(_OracleError("ORA-01017: invalid credential or not authorized"))
    caplog.set_level(logging.DEBUG)

    diagnosis = diagnose_oracle_connect_failure(
        _OracleError(POOL_TIMEOUT),
        connect_kwargs=lambda: _kwargs(
            config_dir=str(tmp_path),
            wallet_location=str(tmp_path),
            min=1,
            max=4,
            getmode=3,
            wait_timeout=30_000,
            session_callback=object(),
        ),
        oracledb_loader=lambda: fake,
    )

    assert diagnosis is not None
    assert diagnosis.category == "invalid_credentials"
    assert diagnosis.code == "ORA-01017"
    assert diagnosis.probed is True
    # 再試行しない 1 回だけの接続（account lock を招かない）。
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["retry_count"] == 0
    assert call["retry_delay"] == 0
    assert call["tcp_connect_timeout"] == 10.0
    assert "retry_count=20" not in str(call["dsn"])
    assert "adb.example.com" in str(call["dsn"])
    for pool_only in ("min", "max", "getmode", "wait_timeout", "session_callback"):
        assert pool_only not in call
    assert PASSWORD not in _all_text(diagnosis, caplog)


def test_pool_timeout_with_working_direct_connect_is_reported_as_transient() -> None:
    fake = _FakeOracledb()

    diagnosis = diagnose_oracle_connect_failure(
        _OracleError(POOL_TIMEOUT),
        connect_kwargs=_kwargs(tcp_connect_timeout=5.0),
        oracledb_loader=lambda: fake,
    )

    assert diagnosis is not None
    assert diagnosis.category == "pool_timeout"
    assert diagnosis.probed is True
    assert "直接の接続はできました" in diagnosis.reason
    assert fake.calls[0]["tcp_connect_timeout"] == 5.0
    assert fake.connections[0].closed == 1


def test_pool_timeout_without_connect_kwargs_does_not_probe() -> None:
    def broken_kwargs() -> dict[str, object]:
        raise RuntimeError(f"settings broken password={PASSWORD}")

    diagnosis = diagnose_oracle_connect_failure(
        _OracleError(POOL_TIMEOUT), connect_kwargs=broken_kwargs
    )

    assert diagnosis is not None
    assert diagnosis.category == "pool_timeout"
    assert diagnosis.probed is False
    assert "ADB が停止しているか" in diagnosis.action


def test_non_pool_connection_error_is_explained_without_probe() -> None:
    fake = _FakeOracledb()

    diagnosis = diagnose_oracle_connect_failure(
        _OracleError("ORA-28000: The account is locked."),
        connect_kwargs=_kwargs,
        oracledb_loader=lambda: fake,
    )

    assert diagnosis is not None
    assert diagnosis.category == "account_locked"
    assert fake.calls == []


@pytest.mark.parametrize(
    "probe_message",
    [
        f"ORA-01017: logon denied for APP_USER/{PASSWORD}@adb.example.com",
        f"DPY-6005: cannot connect (password={PASSWORD})\n[SSL] wallet_password={WALLET_PASSWORD}",
        f"DPY-6005: cannot connect {PASSWORD} {WALLET_PASSWORD}\ntimed out",
        f"DPY-4000: unable to find tcps://APP_USER:{PASSWORD}@adb.example.com",
    ],
    ids=["easy-connect", "assignment", "raw-values", "url-credential"],
)
def test_secrets_never_appear_in_diagnosis_or_logs(
    probe_message: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = _FakeOracledb(_OracleError(probe_message))

    diagnosis = diagnose_oracle_connect_failure(
        _OracleError(POOL_TIMEOUT), connect_kwargs=_kwargs, oracledb_loader=lambda: fake
    )

    assert diagnosis is not None
    assert diagnosis.detail
    text = _all_text(diagnosis, caplog)
    assert PASSWORD not in text
    assert WALLET_PASSWORD not in text


def test_redact_connect_secrets_masks_values_and_credentials() -> None:
    text = redact_connect_secrets(
        f"x {PASSWORD} user/pw1234@host password='abc' tcps://u:p@h",
        {"password": PASSWORD},
    )

    assert PASSWORD not in text
    assert "pw1234" not in text
    assert "'abc'" not in text
    assert "u:p@" not in text


def test_probe_connect_kwargs_strips_retry_from_descriptor_dsn() -> None:
    kwargs = probe_connect_kwargs(
        {"dsn": "(description=(retry_count=20)(retry_delay=3)(address=(host=h)))", "min": 1}
    )

    assert kwargs["dsn"] == "(description=(address=(host=h)))"
    assert "min" not in kwargs
    assert kwargs["tcp_connect_timeout"] == 10.0


@pytest.fixture
def restore_excepthook() -> Any:
    previous = threading.excepthook
    diagnosis_module._ORACLEDB_HOOK_INSTALLED.clear()
    yield
    threading.excepthook = previous
    diagnosis_module._ORACLEDB_HOOK_INSTALLED.clear()


def _bg_task_func() -> None:
    raise ValueError("list.remove(x): x not in list")


def _other_task() -> None:
    raise ValueError("other failure")


def test_oracledb_background_thread_error_becomes_one_log_line(
    restore_excepthook: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[str] = []
    threading.excepthook = lambda args: seen.append(str(args.exc_value))
    install_oracledb_thread_excepthook()
    install_oracledb_thread_excepthook()  # 2 回目は何もしない
    caplog.set_level(logging.WARNING)

    worker = threading.Thread(target=_bg_task_func)
    worker.start()
    worker.join()
    other = threading.Thread(target=_other_task)
    other.start()
    other.join()

    records = [r for r in caplog.records if r.getMessage() == "oracledb_background_thread_error"]
    assert len(records) == 1
    assert records[0].__dict__["error_message"] == "list.remove(x): x not in list"
    assert records[0].__dict__["exception_type"] == "ValueError"
    # oracledb のスレッドでない例外は、今までの hook に渡す。
    assert seen == ["other failure"]
