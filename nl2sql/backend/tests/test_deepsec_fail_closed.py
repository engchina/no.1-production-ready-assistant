"""DeepSec の Data Grant の付け外しが、どの途中の状態でも fail-closed であること（#1022）。

実 Oracle には接続しない。fake の接続が実行された文を順に解釈し、次の権限の状態を持つ。

- DB role（NL2SQL_APP_DB_ROLE）が SELECT を持つ対象
- `USE DATA GRANTS ONLY` が有効な対象
- Data Grant（名前 → 対象）、DATA USER（END USER）と DB role の有無

Oracle Deep Data Security では、`USE DATA GRANTS ONLY` が無効な表は従来の object privilege で
読めるため、「DB role に SELECT があり、DATA GRANTS ONLY が無効」の対象は、DATA USER が
行の制限なしに読める（fail-open）。各文の実行の直後と最後に、その状態が無いことを確かめる。
途中の失敗は、n 番目の文で ORA を返すことで作る（文の実行は本物の
`OracleStatementExecutor` を通す）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

import app.settings as app_settings
from app.clients.oracle_runtime import OraclePoolManager
from app.clients.oracle_statement_executor import OracleStatementExecutor
from app.security.deepsec import (
    DEEPSEC_APPLY_CONFIRMATION,
    DEEPSEC_RESET_CONFIRMATION,
    DeepSecService,
    _revoke_target_select_statement,
)
from app.security.domain import DataEntitlementRecord, Principal, RoleRecord
from app.security.service import SecurityApiError, SecurityService
from app.security.store import InMemorySecurityStore
from app.settings import Settings

TARGET = "HR.EMPLOYEES"
_ORA_FAILURE = "ORA-01031: insufficient privileges"


def _settings() -> Settings:
    return Settings.model_construct(
        oracle_user="APP_OWNER",
        oracle_password="ControlPass!123",
        app_admin_login_user_id="system_admin",
        app_admin_login_user_password="AppAdminPass123",
        oracle_dsn="test",
        oracle_driver_mode="thin",
        oracle_connection_security="walletless_tls",
        oracle_client_lib_dir="",
        oracle_wallet_dir="",
        oracle_wallet_password="",
        oracle_deepsec_enabled=True,
        oracle_deepsec_data_user="DEEPSEC_DATA_USER",
        oracle_deepsec_data_user_password="DeepSecret!123",
        nl2sql_oracle_call_timeout_seconds=120.0,
        nl2sql_persistence_mode="memory",
        app_auth_password_min_length=12,
        app_auth_password_max_length=128,
    )


def _principal() -> Principal:
    return Principal(
        user_uuid="actor",
        login_user_id="actor",
        display_name="actor",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["SYSTEM_ADMIN"],
        permissions=set(),
        data_entitlements=[],
        allowed_profile_ids=set(),
        session_id="session",
        csrf_token_hash="csrf",
    )


def _entitlement(
    entitlement_id: str = "entitlement-sales",
    *,
    data_grant_name: str = "",
    apply_status: str = "PENDING",
) -> DataEntitlementRecord:
    return DataEntitlementRecord(
        entitlement_id=entitlement_id,
        role_id="role-sales",
        resource_code=TARGET,
        scope_code="SALES",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode="COLUMN_EQUALS",
        scope_column="DEPARTMENT_CODE",
        data_grant_name=data_grant_name,
        apply_status=apply_status,
    )


def _role(entitlements: list[DataEntitlementRecord]) -> RoleRecord:
    return RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="営業テーブルを参照するロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=entitlements,
    )


@dataclass
class _OracleGrantModel:
    """実行された DDL から、DATA USER が対象を読めるかを判断するための最小の状態。"""

    role_select: set[str] = field(default_factory=set)
    data_grants_only: set[str] = field(default_factory=set)
    data_grants: dict[str, str] = field(default_factory=dict)
    end_user_exists: bool = True
    db_role_exists: bool = True
    fail_at: int | None = None
    executed: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def unrestricted_targets(self) -> set[str]:
        if not (self.end_user_exists and self.db_role_exists):
            return set()
        return {target for target in self.role_select if target not in self.data_grants_only}

    def apply(self, sql: str) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            return
        self.executed.append(sql)
        if self.fail_at is not None and len(self.executed) == self.fail_at:
            raise RuntimeError(_ORA_FAILURE)
        if match := re.search(r"REVOKE SELECT ON (\S+) FROM NL2SQL_APP_DB_ROLE", sql):
            self.role_select.discard(match.group(1))
        elif match := re.search(r"SET USE DATA GRANTS ONLY ON (\S+) (ENABLED|DISABLED)", sql):
            if match.group(2) == "ENABLED":
                self.data_grants_only.add(match.group(1))
            else:
                self.data_grants_only.discard(match.group(1))
        elif match := re.match(r"\s*GRANT SELECT ON (\S+) TO NL2SQL_APP_DB_ROLE\s*$", sql):
            self.role_select.add(match.group(1))
        elif match := re.match(r"\s*DROP DATA GRANT IF EXISTS (\S+)\s*$", sql):
            self.data_grants.pop(match.group(1), None)
        elif match := re.search(
            r"CREATE OR REPLACE DATA GRANT (\S+)\s+AS SELECT \([^)]*\)\s+ON (\S+)", sql
        ):
            self.data_grants[match.group(1)] = match.group(2)
        elif "DROP END USER" in sql:
            self.end_user_exists = False
        elif "DROP ROLE NL2SQL_APP_DB_ROLE" in sql:
            self.db_role_exists = False
            self.role_select.clear()
        unrestricted = self.unrestricted_targets()
        if unrestricted:
            self.violations.append(
                f"{len(self.executed)} 番目の文の後に制限なしで読める: {sorted(unrestricted)}"
            )


class _ModelCursor:
    def __init__(self, model: _OracleGrantModel) -> None:
        self._model = model
        self.rowcount = 0

    def __enter__(self) -> _ModelCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object | None = None) -> None:
        self._model.apply(sql)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def fetchone(self) -> tuple[Any, ...] | None:
        return (0,)


class _ModelConnection:
    def __init__(self, model: _OracleGrantModel) -> None:
        self._model = model

    def cursor(self) -> _ModelCursor:
        return _ModelCursor(self._model)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ModelPool:
    def __init__(self, model: _OracleGrantModel) -> None:
        self._model = model

    def acquire(self) -> _ModelConnection:
        return _ModelConnection(self._model)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    store: InMemorySecurityStore,
    model: _OracleGrantModel,
) -> DeepSecService:
    settings = _settings()
    security = SecurityService(store, settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _ModelPool(model))
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args, **_kw: None)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: None)
    return DeepSecService(settings, security, manager)


def _apply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: list[DataEntitlementRecord],
    desired: list[DataEntitlementRecord],
    model: _OracleGrantModel,
) -> tuple[InMemorySecurityStore, SecurityApiError | None]:
    store = InMemorySecurityStore()
    service = _service(monkeypatch, store, model)
    store.roles["role-sales"] = _role(current)
    error: SecurityApiError | None = None
    try:
        service.apply_data_entitlements(
            "role-sales",
            expected_version=1,
            confirmation=DEEPSEC_APPLY_CONFIRMATION,
            entitlements=desired,
            actor=_principal(),
        )
    except SecurityApiError as exc:
        error = exc
    return store, error


def _applied_model(**kwargs: Any) -> _OracleGrantModel:
    """対象の Data Grant を適用済みの状態（SELECT・DATA GRANTS ONLY・Data Grant がある）。"""

    return _OracleGrantModel(
        role_select={TARGET},
        data_grants_only={TARGET},
        data_grants={"APP_OWNER.NL2SQL_DG_OLD": TARGET},
        **kwargs,
    )


def _statement_count(model_factory: Any, run: Any) -> int:
    model = model_factory()
    run(model)
    return len(model.executed)


# --- 最後の Data Grant の削除 -------------------------------------------------------------


def _delete_last_grant(monkeypatch: pytest.MonkeyPatch, model: _OracleGrantModel) -> Any:
    return _apply(
        monkeypatch,
        current=[_entitlement(data_grant_name="NL2SQL_DG_OLD", apply_status="APPLIED")],
        desired=[],
        model=model,
    )


def test_deleting_last_data_grant_revokes_select_before_disabling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _applied_model()

    _store, error = _delete_last_grant(monkeypatch, model)

    assert error is None
    assert model.violations == []
    # 最後には SELECT も Data Grant も残らない（DATA GRANTS ONLY は元どおり無効）。
    assert model.role_select == set()
    assert model.data_grants == {}
    assert model.data_grants_only == set()
    assert "REVOKE SELECT ON HR.EMPLOYEES FROM NL2SQL_APP_DB_ROLE" in model.executed[0]
    assert "SET USE DATA GRANTS ONLY ON HR.EMPLOYEES DISABLED" in model.executed[-1]


def test_deleting_last_data_grant_is_fail_closed_at_every_failure_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = _statement_count(_applied_model, lambda m: _delete_last_grant(monkeypatch, m))
    assert total == 3
    for fail_at in range(1, total + 1):
        model = _applied_model(fail_at=fail_at)

        store, error = _delete_last_grant(monkeypatch, model)

        assert error is not None and error.status_code == 409
        # 失敗した文の後ろは実行しない。
        assert len(model.executed) == fail_at
        assert model.violations == [], f"fail_at={fail_at}"
        assert model.unrestricted_targets() == set(), f"fail_at={fail_at}"
        stored = store.get_role("role-sales")
        assert stored is not None
        assert [item.apply_status for item in stored.entitlements] == ["FAILED"]


# --- 新しい対象への Data Grant の適用 ------------------------------------------------------


def _apply_new_target(monkeypatch: pytest.MonkeyPatch, model: _OracleGrantModel) -> Any:
    return _apply(monkeypatch, current=[], desired=[_entitlement()], model=model)


def test_new_data_grant_enables_data_grants_only_before_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _OracleGrantModel()

    store, error = _apply_new_target(monkeypatch, model)

    assert error is None
    assert model.violations == []
    assert model.role_select == {TARGET}
    assert model.data_grants_only == {TARGET}
    assert list(model.data_grants.values()) == [TARGET]
    stored = store.get_role("role-sales")
    assert stored is not None
    assert [item.apply_status for item in stored.entitlements] == ["APPLIED"]


def test_new_data_grant_is_fail_closed_at_every_failure_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = _statement_count(_OracleGrantModel, lambda m: _apply_new_target(monkeypatch, m))
    assert total == 4
    for fail_at in range(1, total + 1):
        model = _OracleGrantModel(fail_at=fail_at)

        store, error = _apply_new_target(monkeypatch, model)

        assert error is not None and error.status_code == 409
        assert len(model.executed) == fail_at
        assert model.violations == [], f"fail_at={fail_at}"
        assert model.unrestricted_targets() == set(), f"fail_at={fail_at}"
        # 下書きは保存しない（適用前のロールのまま）。
        stored = store.get_role("role-sales")
        assert stored is not None
        assert stored.entitlements == []


# --- 同じ対象の Data Grant の置き換え ------------------------------------------------------


def _replace_grant(monkeypatch: pytest.MonkeyPatch, model: _OracleGrantModel) -> Any:
    current = _entitlement(data_grant_name="NL2SQL_DG_OLD", apply_status="APPLIED")
    replacement = replace(current, entitlement_id="entitlement-new", data_grant_name="")
    return _apply(monkeypatch, current=[current], desired=[replacement], model=model)


def test_replacing_data_grant_is_fail_closed_at_every_failure_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = _statement_count(_applied_model, lambda m: _replace_grant(monkeypatch, m))
    assert total == 5
    for fail_at in [None, *range(1, total + 1)]:
        model = _applied_model(fail_at=fail_at)

        _store, error = _replace_grant(monkeypatch, model)

        assert (error is None) is (fail_at is None)
        assert model.violations == [], f"fail_at={fail_at}"
        assert model.unrestricted_targets() == set(), f"fail_at={fail_at}"


# --- DeepSec 構成の解除 -------------------------------------------------------------------


def _reset(monkeypatch: pytest.MonkeyPatch, model: _OracleGrantModel) -> SecurityApiError | None:
    store = InMemorySecurityStore()
    service = _service(monkeypatch, store, model)
    store.roles["role-sales"] = _role(
        [_entitlement(data_grant_name="NL2SQL_DG_OLD", apply_status="APPLIED")]
    )
    monkeypatch.setattr(service.pools, "close", lambda: None)
    try:
        service.reset("V001", DEEPSEC_RESET_CONFIRMATION, _principal())
    except SecurityApiError as exc:
        return exc
    return None


def test_reset_is_fail_closed_at_every_failure_point(monkeypatch: pytest.MonkeyPatch) -> None:
    total = _statement_count(_applied_model, lambda m: _reset(monkeypatch, m))
    for fail_at in [None, *range(1, total + 1)]:
        model = _applied_model(fail_at=fail_at)

        error = _reset(monkeypatch, model)

        assert (error is None) is (fail_at is None)
        if fail_at is not None:
            assert len(model.executed) == fail_at
        assert model.violations == [], f"fail_at={fail_at}"
        assert model.unrestricted_targets() == set(), f"fail_at={fail_at}"


# --- 失敗の記録と文言 -----------------------------------------------------------------------


def test_apply_failure_records_failed_with_japanese_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _applied_model(fail_at=1)

    store, error = _delete_last_grant(monkeypatch, model)

    assert error is not None
    message = error.public_message
    assert message.startswith("Data Grant の適用が途中で止まりました。")
    assert "1 番目の文で失敗しました。" in message
    assert _ORA_FAILURE in message
    assert "SQL execution failed" not in message
    stored = store.get_role("role-sales")
    assert stored is not None
    assert stored.entitlements[0].apply_status == "FAILED"
    assert stored.entitlements[0].apply_error_message == message
    # 失敗の記録は role の version を変えない（同じ version で再適用できる）。
    assert stored.version == 1


def test_apply_validation_error_before_oracle_does_not_record_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _applied_model()
    store = InMemorySecurityStore()
    service = _service(monkeypatch, store, model)
    store.roles["role-sales"] = _role(
        [_entitlement(data_grant_name="NL2SQL_DG_OLD", apply_status="APPLIED")]
    )

    def reject(*_args: object, **_kwargs: object) -> None:
        raise SecurityApiError(400, "Data Grant に含める列を選択してください。")

    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", reject)

    with pytest.raises(SecurityApiError):
        service.apply_data_entitlements(
            "role-sales",
            expected_version=1,
            confirmation=DEEPSEC_APPLY_CONFIRMATION,
            entitlements=[_entitlement("entitlement-new")],
            actor=_principal(),
        )

    assert model.executed == []
    stored = store.get_role("role-sales")
    assert stored is not None
    assert stored.entitlements[0].apply_status == "APPLIED"


def test_statement_failure_without_oracle_message_is_japanese(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecurityStore()
    service = _service(monkeypatch, store, _OracleGrantModel())
    store.roles["role-sales"] = _role([])
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda *_args, **_kwargs: [{"status": "error", "index": 2, "error_message": ""}],
    )

    with pytest.raises(SecurityApiError) as exc_info:
        service.apply_data_entitlements(
            "role-sales",
            expected_version=1,
            confirmation=DEEPSEC_APPLY_CONFIRMATION,
            entitlements=[_entitlement()],
            actor=_principal(),
        )

    message = exc_info.value.public_message
    assert "SQL execution failed" not in message
    assert "2 番目の文で失敗しました。Oracle の応答はありません。" in message


def test_revoke_statement_quotes_case_sensitive_target() -> None:
    statement = _revoke_target_select_statement("SALES", '"Mixed_Case"')

    assert 'REVOKE SELECT ON SALES."Mixed_Case" FROM NL2SQL_APP_DB_ROLE' in statement
    assert "TABLE_NAME = 'Mixed_Case'" in statement
    # REVOKE の後にも SELECT が残っていたら、無効化へ進ませずにエラーにする。
    assert "RAISE_APPLICATION_ERROR" in statement


# --- 文の実行器 ------------------------------------------------------------------------------


def test_statement_executor_stops_after_first_error_when_requested() -> None:
    model = _OracleGrantModel(fail_at=2)
    statements = [
        f"GRANT SELECT ON {TARGET} TO NL2SQL_APP_DB_ROLE",
        f"SET USE DATA GRANTS ONLY ON {TARGET} ENABLED",
        f"SET USE DATA GRANTS ONLY ON {TARGET} DISABLED",
    ]

    results = OracleStatementExecutor().execute(
        _ModelConnection(model), statements, atomic=False, stop_on_error=True
    )

    assert [item["status"] for item in results] == ["success", "error"]
    assert len(model.executed) == 2


def test_statement_executor_keeps_running_by_default() -> None:
    model = _OracleGrantModel(fail_at=1)

    results = OracleStatementExecutor().execute(
        _ModelConnection(model),
        ["DROP DATA GRANT IF EXISTS A.B", "DROP DATA GRANT IF EXISTS A.C"],
        atomic=False,
    )

    assert [item["status"] for item in results] == ["error", "success"]


# --- 設定の他 worker への反映 ---------------------------------------------------------------


def test_get_settings_reloads_deepsec_values_saved_by_another_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "backend.env"
    monkeypatch.setattr(app_settings, "BACKEND_ENV_FILE", env_file)
    monkeypatch.delenv("NL2SQL_ORACLE_DEEPSEC_ENABLED", raising=False)
    monkeypatch.delenv("NL2SQL_ORACLE_DEEPSEC_DATA_USER", raising=False)
    monkeypatch.delenv("NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD", raising=False)
    app_settings.reset_settings_cache()
    try:
        settings = app_settings.get_settings()
        # 既定は無効（#1707）。
        assert settings.oracle_deepsec_enabled is False

        # 別の worker の update_config が backend/.env を書いた状態
        # （有効にし、利用者の password を保存）。
        env_file.write_text(
            "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
            "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
            "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=OtherWorker!123\n",
            encoding="utf-8",
        )

        reloaded = app_settings.get_settings()
        assert reloaded is settings
        assert settings.oracle_deepsec_enabled is True
        assert settings.oracle_deepsec_data_user_password == "OtherWorker!123"
    finally:
        app_settings.reset_settings_cache()


def test_get_settings_does_not_override_deepsec_values_given_by_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "backend.env"
    monkeypatch.setattr(app_settings, "BACKEND_ENV_FILE", env_file)
    monkeypatch.setenv("NL2SQL_ORACLE_DEEPSEC_ENABLED", "false")
    app_settings.reset_settings_cache()
    try:
        settings = app_settings.get_settings()
        env_file.write_text("NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n", encoding="utf-8")

        app_settings.get_settings()

        assert settings.oracle_deepsec_enabled is False
    finally:
        app_settings.reset_settings_cache()


def test_data_pool_is_recreated_when_data_user_password_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    created: list[dict[str, object]] = []
    closed: list[str] = []

    class Pool:
        def __init__(self, password: object) -> None:
            self.password = password

        def close(self, *, force: bool = False) -> None:
            _ = force
            closed.append(str(self.password))

    class OracleDb:
        @staticmethod
        def create_pool(**kwargs: object) -> Pool:
            created.append(kwargs)
            return Pool(kwargs.get("password"))

    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_load_oracledb", lambda: OracleDb())
    monkeypatch.setattr(
        "app.clients.oracle_runtime.oracle_connect_kwargs",
        lambda _settings, **kwargs: dict(kwargs),
    )

    first = manager._get_pool(data_plane=True)
    assert manager._get_pool(data_plane=True) is first

    settings.oracle_deepsec_data_user_password = "Rotated!Secret123"
    second = manager._get_pool(data_plane=True)

    assert second is not first
    assert closed == ["DeepSecret!123"]
    assert [item.get("password") for item in created] == ["DeepSecret!123", "Rotated!Secret123"]


def test_control_pool_is_recreated_when_database_settings_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別の製品が保存したデータベースの設定を読み直したら、管理の接続 pool も作り直す（#2102）。"""
    settings = _settings()
    created: list[dict[str, object]] = []
    closed: list[object] = []

    class Pool:
        def __init__(self, dsn: object) -> None:
            self.dsn = dsn

        def close(self, *, force: bool = False) -> None:
            _ = force
            closed.append(self.dsn)

    class OracleDb:
        @staticmethod
        def create_pool(**kwargs: object) -> Pool:
            created.append(kwargs)
            return Pool(kwargs.get("dsn"))

    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_load_oracledb", lambda: OracleDb())
    monkeypatch.setattr(
        "app.clients.oracle_runtime.oracle_connect_kwargs",
        lambda current, **kwargs: {"dsn": current.oracle_dsn, **kwargs},
    )

    first = manager._get_pool(data_plane=False)
    data = manager._get_pool(data_plane=True)
    assert manager._get_pool(data_plane=False) is first

    settings.oracle_dsn = "other_high"
    second = manager._get_pool(data_plane=False)

    assert second is not first
    assert closed == ["test"]
    assert [item["dsn"] for item in created] == ["test", "test", "other_high"]
    # DATA USER の pool も、接続先が変わったら作り直す
    assert manager._get_pool(data_plane=True) is not data


def test_deepsec_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deep Data Security の既定は無効（配備の stack・.env.example と同じ。#1707）。

    使う環境だけが有効にする。
    """
    monkeypatch.delenv("NL2SQL_ORACLE_DEEPSEC_ENABLED", raising=False)
    assert app_settings.Settings.model_fields["oracle_deepsec_enabled"].default is False
