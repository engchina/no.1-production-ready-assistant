"""RAG system schema の CLI（`app.rag.system_schema_cli`）のテスト。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.rag import system_schema_cli
from app.rag.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    SystemSchemaError,
    system_schema_manager,
)


def test_delete_orphans_passes_confirmed_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def delete_orphaned_rows(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"operation": "orphans_deleted", "deleted_row_count": 3}

    monkeypatch.setattr(system_schema_manager, "delete_orphaned_rows", delete_orphaned_rows)

    code = system_schema_cli.main(
        ["delete-orphans", "--constraint", "RAG_CHUNK_SETS_DOCUMENT_FK", "--expected-rows", "3"]
    )

    assert code == 0
    assert captured == {"constraint_name": "RAG_CHUNK_SETS_DOCUMENT_FK", "expected_orphan_rows": 3}
    assert json.loads(capsys.readouterr().out)["deleted_row_count"] == 3


def test_delete_orphans_requires_constraint_and_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("件数を確認していない削除を実行してはならない")

    monkeypatch.setattr(system_schema_manager, "delete_orphaned_rows", fail)

    with pytest.raises(SystemExit):
        system_schema_cli.main(["delete-orphans", "--constraint", "RAG_CHUNK_SETS_DOCUMENT_FK"])
    capsys.readouterr()

    code = system_schema_cli.main(
        ["delete-orphans", "--constraint", "RAG_CHUNK_SETS_DOCUMENT_FK", "--expected-rows", "-1"]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "SCHEMA_ORPHAN_ROWS_INVALID"


def test_delete_orphans_reports_business_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def delete_orphaned_rows(**_kwargs: Any) -> dict[str, Any]:
        raise SystemSchemaError(
            "SCHEMA_ORPHAN_ROWS_CHANGED", "件数が増えています。", status_code=409
        )

    monkeypatch.setattr(system_schema_manager, "delete_orphaned_rows", delete_orphaned_rows)

    code = system_schema_cli.main(
        ["delete-orphans", "--constraint", "RAG_CHUNK_SETS_DOCUMENT_FK", "--expected-rows", "1"]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().err) == {
        "error_code": "SCHEMA_ORPHAN_ROWS_CHANGED",
        "error_message": "件数が増えています。",
    }


def test_initialize_passes_destructive_approval_only_when_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`initialize` は `--allow-destructive` があるときだけ承認を渡す（#619）。"""
    calls: list[dict[str, Any]] = []

    def initialize(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if not kwargs.get("allow_destructive"):
            raise SystemSchemaError(
                DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
                "データを削除する migration が未適用です（20260930_005）。",
                status_code=409,
            )
        return {"operation": "migrated"}

    monkeypatch.setattr(system_schema_manager, "initialize", initialize)

    assert system_schema_cli.main(["initialize"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert "--allow-destructive" in error["hint"]

    assert system_schema_cli.main(["initialize", "--allow-destructive"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "migrated"
    assert calls == [{"allow_destructive": False}, {"allow_destructive": True}]


def test_check_returns_zero_only_when_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配備の確認（#1648）: ready なら 0、それ以外は 1 と 1 行の要約（ORA コードを含む）。"""

    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: {"status": "ready", "operation_state": {"status": "idle"}},
    )
    assert system_schema_cli.main(["check"]) == 0
    assert capsys.readouterr().out.strip() == "status=ready"

    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: {
            "status": "missing",
            "operation_state": {"status": "failed", "last_error_code": "ORA-01031"},
        },
    )
    assert system_schema_cli.main(["check"]) == 1
    assert "ORA-01031" in capsys.readouterr().out

    def unavailable() -> dict[str, Any]:
        raise RuntimeError("ORA-01017: invalid credential secret")

    monkeypatch.setattr(system_schema_manager, "status", unavailable)
    assert system_schema_cli.main(["check"]) == 1
    output = capsys.readouterr().out
    assert "ORA-01017" in output
    assert "secret" not in output


def test_initialize_reports_ora_code_for_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """lease の前の失敗（制御テーブルの作成の権限不足など）も ORA コードを出す（#1648）。"""

    def initialize(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ORA-01031: insufficient privileges")

    monkeypatch.setattr(system_schema_manager, "initialize", initialize)
    assert system_schema_cli.main(["initialize"]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "ORA-01031"


def test_pool_timeout_reports_real_cause_without_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pool の時間切れ（DPY-4005）は、直接 1 回だけ接続して本当の原因を出す（#1744）。"""
    from pr_backend_core import oracle_connect_diagnosis

    calls: list[dict[str, Any]] = []

    class _Oracledb:
        def connect(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise RuntimeError(
                'DPY-6001: Service "x_high" is not registered with the listener at host '
                '"adb.example.com" port 1522. (Similar to ORA-12514)'
            )

    def timeout(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("DPY-4005: timed out waiting for the connection pool")

    monkeypatch.setattr(oracle_connect_diagnosis, "_default_oracledb_loader", lambda: _Oracledb())
    monkeypatch.setattr(
        system_schema_cli,
        "_connect_kwargs",
        lambda: {"user": "APP", "password": "Pa55-word-x", "dsn": "db_high", "min": 1},
    )
    monkeypatch.setattr(system_schema_manager, "initialize", timeout)
    monkeypatch.setattr(system_schema_manager, "status", lambda: timeout())

    assert system_schema_cli.main(["initialize"]) == 1
    captured = capsys.readouterr()
    # stderr には、診断の 1 行の log（JSON Lines）の後に、CLI の結果（整形した JSON）が出る。
    error = json.loads(captured.err[captured.err.index("{\n") :])
    assert error["error_code"] == "DPY-6001"
    assert error["cause"] == "service_unavailable"
    assert "ADB の停止" in error["hint"]
    assert "起動" in error["hint"]
    assert "Pa55-word-x" not in captured.err + captured.out
    assert len(calls) == 1
    assert calls[0]["retry_count"] == 0 and "min" not in calls[0]

    assert system_schema_cli.main(["check"]) == 1
    output = capsys.readouterr().out
    assert output.startswith(
        "状態を読めません（SCHEMA_STATUS_UNAVAILABLE: DPY-6001: DB のサービスが接続を"
    )
    assert "Pa55-word-x" not in output


def test_shared_pool_connect_kwargs_match_the_pool() -> None:
    """診断の直接の接続は、共有 pool と同じ接続先・利用者を使う（#1744）。"""
    from app.clients.oracle import _oracle_connect_kwargs, shared_pool_connect_kwargs
    from app.config import get_settings

    settings = get_settings()
    assert shared_pool_connect_kwargs(settings) == _oracle_connect_kwargs(settings)


@pytest.mark.parametrize("argv", [["status"], ["check"]], ids=["status", "check"])
def test_cli_connects_without_tns_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """CLI は TNS の再試行を外した接続で DB に繋ぐ（DB の停止中に 1〜2 分待たない。#1779）。"""
    from pr_backend_core.oracle_dsn import cli_connect_mode_enabled

    seen: list[bool] = []

    def status() -> dict[str, Any]:
        seen.append(cli_connect_mode_enabled())
        return {"state": "ready"}

    assert not cli_connect_mode_enabled()
    monkeypatch.setattr(system_schema_manager, "status", status)
    system_schema_cli.main(argv)
    capsys.readouterr()
    assert seen == [True]
