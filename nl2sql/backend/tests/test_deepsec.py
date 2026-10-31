"""DeepSec V001 registry と connection context lifecycle。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pr_backend_core.oracle_session import init_oracle_session
from pydantic import ValidationError

from app.clients import oracle_runtime
from app.clients.oracle_runtime import OraclePoolManager
from app.features.nl2sql.oracle_adapter import OracleAdapterError
from app.security.deepsec import (
    DEEPSEC_APPLY_CONFIRMATION,
    DEEPSEC_RESET_CONFIRMATION,
    PASSWORD_PLACEHOLDER,
    DeepSecService,
    build_data_entitlement_statements,
    build_v001_plan,
    build_v001_reset_statements,
)
from app.security.domain import (
    DataEntitlementRecord,
    DataEntitlementScopeFilter,
    Principal,
    RoleRecord,
    scope_filters_scope_code,
)
from app.security.schemas import DeepSecDataEntitlementApplyRequest
from app.security.service import SecurityApiError, SecurityService
from app.security.store import InMemorySecurityStore
from app.settings import Settings


def _settings(
    *,
    driver_mode: str = "thin",
    connection_security: str = "walletless_tls",
    deepsec_enabled: bool = True,
    data_user_password: str = "DeepSecret!123",
    wallet_dir: str = "",
    call_timeout_seconds: float = 120.0,
) -> Settings:
    return Settings.model_construct(
        oracle_user="APP_OWNER",
        oracle_password="ControlPass!123",
        app_admin_login_user_id="system_admin",
        app_admin_login_user_password="AppAdminPass123",
        oracle_dsn="test",
        oracle_driver_mode=driver_mode,
        oracle_connection_security=connection_security,
        oracle_client_lib_dir="/opt/oracle/instantclient",
        oracle_wallet_dir=wallet_dir,
        oracle_wallet_password="",
        oracle_deepsec_enabled=deepsec_enabled,
        oracle_deepsec_data_user="DEEPSEC_DATA_USER",
        oracle_deepsec_data_user_password=data_user_password,
        nl2sql_oracle_call_timeout_seconds=call_timeout_seconds,
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


def _data_grant_checksum_for_test(statements: tuple[str, ...]) -> str:
    payload = "\n-- statement --\n".join(statement.strip() for statement in statements)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _TargetObjectCursor:
    def __init__(self, pool: _TargetObjectPools) -> None:
        self._pool = pool
        self._rows: list[tuple[Any, ...]] = []
        self._index = 0

    def __enter__(self) -> _TargetObjectCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
        binds = dict(params or {})
        self._pool.executed.append((sql, binds))
        upper_sql = sql.upper()
        self._index = 0
        if "SELECT COUNT(*)" in upper_sql:
            self._rows = [(self._pool.count_result,)]
            return
        if "FROM ALL_TAB_COLUMNS" in upper_sql:
            self._rows = list(self._pool.column_rows)
            return

        rows = list(self._pool.object_rows)
        if "NVL(U.ORACLE_MAINTAINED, 'N') = 'N'" in upper_sql:
            rows = [
                row
                for row in rows
                if str(row[0] or "").upper() not in self._pool.oracle_maintained_owners
            ]
        if self._pool.apply_prefix_filters:
            # SQL のシステムテーブルの接頭辞の除外（#1708）を再現する。
            excluded_prefixes = [
                prefix
                for prefix in ("NL2SQL_", "PLATFORM_", "RAG_", "AGENT_")
                if f"UPPER(O.OBJECT_NAME) NOT LIKE '{prefix.replace('_', chr(92) + '_')}%'"
                in upper_sql
            ]
            rows = [
                row
                for row in rows
                if not str(row[1] or "").upper().startswith(tuple(excluded_prefixes))
            ]
        allowed_owners = {
            str(value).upper()
            for key, value in binds.items()
            if key.startswith("owner_") and key.removeprefix("owner_").isdigit()
        }
        if allowed_owners:
            rows = [row for row in rows if str(row[0] or "").upper() in allowed_owners]
        exact_owner = str(binds.get("owner") or "").upper()
        if exact_owner:
            rows = [row for row in rows if str(row[0] or "").upper() == exact_owner]
        exact_object = str(binds.get("object_name") or "").upper()
        if exact_object:
            rows = [row for row in rows if str(row[1] or "").upper() == exact_object]
        exact_type = str(binds.get("object_type") or "").upper()
        if exact_type:
            rows = [row for row in rows if str(row[2] or "").upper() == exact_type]
        owner_prefix = str(binds.get("owner_prefix") or "").upper().rstrip("%")
        if owner_prefix:
            rows = [row for row in rows if str(row[0] or "").upper().startswith(owner_prefix)]
        query = str(binds.get("object_query") or "").upper().strip("%")
        if query:
            rows = [
                row
                for row in rows
                # SQL と同じく `OWNER.OBJECT` とコメントを照合する（#608）。
                if query in f"{str(row[0] or '').upper()}.{str(row[1] or '').upper()}"
                or query in str(row[4] or "").upper()
            ]
        cursor_owner = str(binds.get("cursor_owner") or "").upper()
        cursor_name = str(binds.get("cursor_name") or "").upper()
        if cursor_owner and cursor_name:
            rows = [
                row
                for row in rows
                if (str(row[0] or "").upper(), str(row[1] or "").upper())
                > (cursor_owner, cursor_name)
            ]
        fetch_limit_value = binds.get("fetch_limit")
        fetch_limit = (
            int(fetch_limit_value) if isinstance(fetch_limit_value, int | str) else len(rows)
        )
        self._rows = rows[:fetch_limit]

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows


class _TargetObjectConnection:
    def __init__(self, pool: _TargetObjectPools) -> None:
        self._pool = pool

    def cursor(self) -> _TargetObjectCursor:
        return _TargetObjectCursor(self._pool)


class _TargetObjectPools:
    def __init__(
        self,
        *,
        object_rows: list[tuple[Any, ...]],
        column_rows: list[tuple[Any, ...]] | None = None,
        oracle_maintained_owners: set[str] | None = None,
        count_result: int | None = None,
        apply_prefix_filters: bool = True,
    ) -> None:
        self.object_rows = object_rows
        self.apply_prefix_filters = apply_prefix_filters
        self.column_rows = column_rows or []
        self.oracle_maintained_owners = {
            owner.upper() for owner in (oracle_maintained_owners or set())
        }
        self.count_result = count_result if count_result is not None else len(object_rows)
        self.executed: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def control_connection(self) -> Iterator[Any]:
        yield _TargetObjectConnection(self)


def _target_object_service(
    settings: Settings,
    pool: _TargetObjectPools,
) -> DeepSecService:
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    return DeepSecService(settings, security, cast(OraclePoolManager, pool))


def test_data_entitlement_apply_request_requires_full_snapshot() -> None:
    payload = DeepSecDataEntitlementApplyRequest.model_validate(
        {
            "version": 1,
            "confirmation": DEEPSEC_APPLY_CONFIRMATION,
            "data_entitlements": [],
        }
    )

    assert payload.data_entitlements == []
    with pytest.raises(ValidationError):
        DeepSecDataEntitlementApplyRequest.model_validate(
            {
                "version": 1,
                "confirmation": DEEPSEC_APPLY_CONFIRMATION,
                "entitlement_ids": ["entitlement-sales"],
            }
        )


def _mark_deepsec_steps_applied(store: InMemorySecurityStore, settings: Settings) -> None:
    for step in build_v001_plan(settings):
        store.set_deepsec_state(
            version="V001",
            step_no=step.step_no,
            step_key=step.key,
            checksum=step.checksum,
            status="APPLIED",
            error_message="",
            executed_by_user_uuid="actor",
        )
    for step_no, step_key in ((3, "legacy_verification_object"), (4, "legacy_data_grants")):
        store.set_deepsec_state(
            version="V001",
            step_no=step_no,
            step_key=step_key,
            checksum=f"legacy-{step_no}",
            status="APPLIED",
            error_message="",
            executed_by_user_uuid="actor",
        )


def _insert_real_entitlement_role(
    store: InMemorySecurityStore,
    *,
    apply_status: str = "PENDING",
    data_grant_name: str = "",
) -> RoleRecord:
    role = RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="営業テーブルを参照するロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[
            DataEntitlementRecord(
                entitlement_id="entitlement-sales",
                role_id="role-sales",
                resource_code="HR.EMPLOYEES",
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
        ],
    )
    store.roles[role.role_id] = role
    return role


def test_v001_registry_is_stable_and_preview_never_contains_secret() -> None:
    settings = _settings()
    first = build_v001_plan(settings)
    second = build_v001_plan(settings)
    assert [step.checksum for step in first] == [step.checksum for step in second]
    preview = "\n".join(statement for step in first for statement in step.statements)
    # V001.1 は DATA USER を作る文を含むが、計画とチェックサムは placeholder のまま（#1676）。
    assert PASSWORD_PLACEHOLDER in preview
    assert settings.oracle_deepsec_data_user_password not in preview
    other = build_v001_plan(_settings(data_user_password="AnotherSecret!456"))
    assert [step.checksum for step in first] == [step.checksum for step in other]


def test_v001_plan_contains_foundation_only_without_probe_flow() -> None:
    plan = build_v001_plan(_settings())
    preview = "\n".join(statement for step in plan for statement in step.statements)

    assert [step.step_no for step in plan] == [1, 2]
    assert "NL2SQL_DEEPSEC_PROBE" not in preview
    assert "CREATE TABLE" not in preview
    assert "CREATE DATA ROLE IF NOT EXISTS NL2SQL_APP_DATA_ROLE" in preview
    assert "GRANT SELECT ON APP_OWNER.PLATFORM_USER_ROLES TO NL2SQL_APP_DB_ROLE" in preview
    assert "GRANT SELECT ON APP_OWNER.PLATFORM_ROLES TO NL2SQL_APP_DB_ROLE" in preview
    assert "GRANT SELECT ON APP_OWNER.NL2SQL_APP_DATA_ENTITLEMENTS TO NL2SQL_APP_DB_ROLE" in preview
    assert "ALTER END USER" not in preview
    assert "GRANT NL2SQL_APP_DB_ROLE TO NL2SQL_APP_DATA_ROLE" in preview
    assert "GRANT DATA ROLE NL2SQL_APP_DATA_ROLE TO DEEPSEC_DATA_USER" in preview
    # 新しい配備では END USER が無いので、DATA ROLE を付ける前に作る（#1676）。
    role_statements = list(plan[0].statements)
    create_end_user = (
        "CREATE END USER IF NOT EXISTS DEEPSEC_DATA_USER "
        f"IDENTIFIED BY {PASSWORD_PLACEHOLDER} SCHEMA APP_OWNER"
    )
    assert create_end_user in role_statements
    assert role_statements.index(create_end_user) < role_statements.index(
        "GRANT DATA ROLE NL2SQL_APP_DATA_ROLE TO DEEPSEC_DATA_USER"
    )
    assert sum("END USER" in statement for statement in plan[1].statements) == 0


def test_target_objects_read_live_metadata_and_filter_owner_search() -> None:
    pool = _TargetObjectPools(
        object_rows=[
            ("HR", "EMPLOYEES", "TABLE", 12, "Employees master"),
            ("HR", "V_EMPLOYEES", "VIEW", None, "Employee view"),
            ("SALES", "ORDERS", "TABLE", 34, "Sales orders"),
            ("SALES", "MV_ORDER_SUMMARY", "MATERIALIZED VIEW", 8, "Order summary"),
        ],
    )
    service = _target_object_service(_settings(), pool)

    page = service.target_objects(limit=10)
    items = cast(list[dict[str, object]], page["items"])
    assert [item["object_type"] for item in items] == [
        "TABLE",
        "VIEW",
        "TABLE",
        "MATERIALIZED VIEW",
    ]

    filtered = service.target_objects(limit=10, owner_prefix="sal", q="summary")
    filtered_items = cast(list[dict[str, object]], filtered["items"])
    assert filtered_items == [
        {
            "name": "MV_ORDER_SUMMARY",
            "owner": "SALES",
            "qualified_name": "SALES.MV_ORDER_SUMMARY",
            "object_type": "MATERIALIZED VIEW",
            "row_count": 8,
            "comment": "Order summary",
        }
    ]
    first_sql, first_binds = pool.executed[0]
    _second_sql, second_binds = pool.executed[1]
    assert "ALL_OBJECTS" in first_sql
    assert "NL2SQL_SCHEMA_OBJECTS" not in first_sql
    assert first_binds["fetch_limit"] == 11
    assert second_binds["owner_prefix"] == "SAL%"
    assert second_binds["object_query"] == "%SUMMARY%"

    # 対象の選択は検索できる選択欄 1 つで探すので、スキーマ名・`OWNER.OBJECT` でも見つかる（#608）。
    by_owner = cast(list[dict[str, object]], service.target_objects(limit=10, q="hr")["items"])
    assert [item["qualified_name"] for item in by_owner] == ["HR.EMPLOYEES", "HR.V_EMPLOYEES"]
    by_qualified = cast(
        list[dict[str, object]], service.target_objects(limit=10, q="sales.ord")["items"]
    )
    assert [item["qualified_name"] for item in by_qualified] == ["SALES.ORDERS"]
    third_sql, _third_binds = pool.executed[2]
    assert "UPPER(o.owner || '.' || o.object_name) LIKE :object_query" in third_sql


def test_target_objects_exclude_system_objects_and_allowlist_owners() -> None:
    settings = _settings().model_copy(
        update={"nl2sql_schema_owner_allowlist": ["app_owner", "sales", "mdsys"]}
    )
    pool = _TargetObjectPools(
        object_rows=[
            ("SYS", "DUAL", "TABLE", None, "Oracle maintained"),
            ("MDSYS", "SPATIAL_TABLE", "TABLE", None, "Oracle maintained"),
            ("APP_OWNER", "PLATFORM_USERS", "TABLE", 3, "internal table"),
            ("APP_OWNER", "BUSINESS_VIEW", "VIEW", None, "business view"),
            ("OTHER", "CUSTOMERS", "TABLE", 4, "not allowlisted"),
            ("SALES", "ORDERS", "TABLE", 7, "orders"),
        ],
        oracle_maintained_owners={"MDSYS"},
    )
    service = _target_object_service(settings, pool)

    page = service.target_objects(limit=10)
    items = cast(list[dict[str, object]], page["items"])

    assert [item["qualified_name"] for item in items] == [
        "APP_OWNER.BUSINESS_VIEW",
        "SALES.ORDERS",
    ]
    sql = pool.executed[0][0]
    assert "JOIN ALL_USERS u" in sql
    assert "NVL(u.oracle_maintained, 'N') = 'N'" in sql
    for prefix in ("NL2SQL", "PLATFORM", "RAG", "AGENT"):
        assert f"UPPER(o.object_name) NOT LIKE '{prefix}\\_%' ESCAPE '\\'" in sql
    assert "o.owner IN (:owner_0, :owner_1, :owner_2)" in sql


def test_target_objects_fill_page_with_views_after_system_tables() -> None:
    """共通基盤・他製品のシステムテーブルが多くても、1 ページ目に view まで届く（#1708）。

    以前は `PLATFORM_` / `RAG_` / `AGENT_` を SQL で除かず、取った行から Python で落としていたため、
    1 ページ目が業務の表 3 件だけになり、後ろに並ぶ view が次のページに回っていた。
    """

    system_rows = [
        ("ADMIN", f"{prefix}{index:02d}", "TABLE", 0, "")
        for prefix in ("AGENT_RUNS_", "PLATFORM_USERS_", "RAG_CHUNKS_")
        for index in range(20)
    ]
    business_rows = [
        ("ADMIN", "DEPARTMENT", "TABLE", 5, ""),
        ("ADMIN", "EMPLOYEE", "TABLE", 20, ""),
        ("ADMIN", "PROJECT", "TABLE", 8, ""),
        ("ADMIN", "V_DEPT_PROJECT", "VIEW", None, ""),
        ("ADMIN", "V_EMP_DEPT", "VIEW", None, ""),
    ]
    rows = sorted([*system_rows, *business_rows], key=lambda row: (row[0], row[1]))
    pool = _TargetObjectPools(object_rows=rows, count_result=len(business_rows))
    service = _target_object_service(_settings(), pool)

    page = service.target_objects(limit=50, include_counts=True)
    items = cast(list[dict[str, object]], page["items"])

    assert [(item["qualified_name"], item["object_type"]) for item in items] == [
        ("ADMIN.DEPARTMENT", "TABLE"),
        ("ADMIN.EMPLOYEE", "TABLE"),
        ("ADMIN.PROJECT", "TABLE"),
        ("ADMIN.V_DEPT_PROJECT", "VIEW"),
        ("ADMIN.V_EMP_DEPT", "VIEW"),
    ]
    assert page["next_cursor"] is None
    assert page["total"] == len(business_rows)
    page_sql = pool.executed[0][0]
    count_sql = pool.executed[1][0]
    assert "SELECT COUNT(*)" in count_sql
    for sql in (page_sql, count_sql):
        for prefix in ("NL2SQL", "PLATFORM", "RAG", "AGENT"):
            assert f"UPPER(o.object_name) NOT LIKE '{prefix}\\_%' ESCAPE '\\'" in sql


def test_target_objects_cursor_continues_past_rows_hidden_in_python() -> None:
    """Python の可視性の判定で落とした行がページの末尾に続いても、次のページへ進める（#1708）。"""

    pool = _TargetObjectPools(
        object_rows=[
            ("APP", "ORDERS", "TABLE", 1, ""),
            ("APP", "PLATFORM_A", "TABLE", 0, ""),
            ("APP", "PLATFORM_B", "TABLE", 0, ""),
            ("APP", "PLATFORM_C", "TABLE", 0, ""),
            ("APP", "V_ORDERS", "VIEW", None, ""),
        ],
        # SQL の除外が効かない行（カタログの名前の揺れなど）を Python だけが落とす場合を再現する。
        apply_prefix_filters=False,
    )
    service = _target_object_service(_settings(), pool)

    first = service.target_objects(limit=3)
    assert [item["qualified_name"] for item in cast(list[dict[str, object]], first["items"])] == [
        "APP.ORDERS"
    ]
    assert first["next_cursor"]

    second = service.target_objects(limit=3, cursor=cast(str, first["next_cursor"]))
    assert [item["qualified_name"] for item in cast(list[dict[str, object]], second["items"])] == [
        "APP.V_ORDERS"
    ]
    assert second["next_cursor"] is None


def test_target_object_detail_returns_columns_from_live_metadata() -> None:
    pool = _TargetObjectPools(
        object_rows=[("SALES", "ORDERS", "TABLE", 34, "Sales orders")],
        column_rows=[
            ("ORDER_ID", "NUMBER(10)", "N", "注文ID"),
            ("CUSTOMER_NAME", "VARCHAR2(128)", "Y", "顧客名"),
        ],
    )
    service = _target_object_service(_settings(), pool)

    detail = service.target_object_detail(
        owner="sales",
        object_name="orders",
        object_type="table",
    )
    columns = cast(list[dict[str, object]], detail["columns"])

    assert detail["qualified_name"] == "SALES.ORDERS"
    assert detail["object_type"] == "TABLE"
    assert columns == [
        {
            "column_name": "ORDER_ID",
            "logical_name": "注文ID",
            "data_type": "NUMBER(10)",
            "nullable": False,
            "comment": "注文ID",
            "sample_values": [],
        },
        {
            "column_name": "CUSTOMER_NAME",
            "logical_name": "顧客名",
            "data_type": "VARCHAR2(128)",
            "nullable": True,
            "comment": "顧客名",
            "sample_values": [],
        },
    ]
    assert "FROM ALL_TAB_COLUMNS" in pool.executed[-1][0]


def test_verify_fails_when_predicate_table_grants_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StatusCursor:
        def __init__(self) -> None:
            self.sql = ""
            self.params: dict[str, object] = {}

        def __enter__(self) -> StatusCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
            self.sql = sql
            self.params = dict(params or {})

        def fetchone(self) -> tuple[int]:
            if "DBA_TAB_PRIVS" not in self.sql:
                return (1,)
            table_name = str(self.params.get("table_name") or "")
            return (0,) if table_name == "PLATFORM_USER_ROLES" else (1,)

        def fetchall(self) -> list[tuple[object, ...]]:
            return []

    class StatusConnection:
        def cursor(self) -> StatusCursor:
            return StatusCursor()

        def close(self) -> None:
            return None

    class StatusPool:
        def acquire(self) -> StatusConnection:
            return StatusConnection()

    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: StatusPool())
    service = DeepSecService(settings, security, manager)

    status = service.status()
    result = service.verify(_principal())

    assert status["configured"] is False
    assert status["objects"]["predicate_user_roles_grant"] == 0  # type: ignore[index]
    assert result["passed"] is False
    predicate_check = next(
        item
        for item in result["checks"]  # type: ignore[attr-defined]
        if item["key"] == "predicate_table_grants"
    )
    assert predicate_check["passed"] is False
    assert "PLATFORM_USER_ROLES" in predicate_check["detail"]


@pytest.mark.parametrize(
    ("grantee", "grantee_type", "expected_passed"),
    [
        ("NL2SQL_APP_DATA_ROLE", "DATA ROLE", True),
        ("DEEPSEC_DATA_USER", "END USER", False),
    ],
)
def test_verify_requires_managed_data_grant_on_data_role(
    monkeypatch: pytest.MonkeyPatch,
    grantee: str,
    grantee_type: str,
    expected_passed: bool,
) -> None:
    class VerifyCursor:
        def __init__(self) -> None:
            self.sql = ""

        def __enter__(self) -> VerifyCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: dict[str, object] | None = None) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[object, ...]]:
            if "DBA_POLICIES" in self.sql:
                return []
            predicate = "ORA_END_USER_CONTEXT.CLIENT_IDENTIFIER = 'actor'"
            return [
                (
                    "NL2SQL_DG_VERIFY",
                    "HR",
                    "EMPLOYEES",
                    grantee,
                    grantee_type,
                    True,
                    predicate,
                )
            ]

    class VerifyConnection:
        def cursor(self) -> VerifyCursor:
            return VerifyCursor()

        def close(self) -> None:
            return None

    class VerifyPool:
        def acquire(self) -> VerifyConnection:
            return VerifyConnection()

    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    _insert_real_entitlement_role(
        store,
        apply_status="APPLIED",
        data_grant_name="NL2SQL_DG_VERIFY",
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: VerifyPool())
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(
        service,
        "status",
        lambda: {
            "configured": True,
            "message": "ok",
            "objects": {
                "predicate_user_roles_grant": 1,
                "predicate_roles_grant": 1,
                "predicate_data_entitlements_grant": 1,
            },
        },
    )

    result = service.verify(_principal())

    assert result["passed"] is expected_passed
    data_grant_check = next(
        item
        for item in result["checks"]  # type: ignore[attr-defined]
        if item["key"] == "data_grant:entitlement-sales"
    )
    assert data_grant_check["passed"] is expected_passed
    assert f"data_role_rows={1 if expected_passed else 0}" in data_grant_check["detail"]
    assert f"direct_end_user_rows={0 if expected_passed else 1}" in data_grant_check["detail"]


def test_verify_fails_when_target_has_enabled_vpd_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class VerifyCursor:
        def __init__(self) -> None:
            self.sql = ""

        def __enter__(self) -> VerifyCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: dict[str, object] | None = None) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[object, ...]]:
            if "DBA_POLICIES" in self.sql:
                return [
                    (
                        "SQL_ASSIST_VPD_LEGACY_POL",
                        "HR",
                        "SQL_ASSIST_VPD_LEGACY_FN",
                        "CONTEXT_SENSITIVE",
                    )
                ]
            return [
                (
                    "NL2SQL_DG_VERIFY",
                    "HR",
                    "EMPLOYEES",
                    "NL2SQL_APP_DATA_ROLE",
                    "DATA ROLE",
                    True,
                    "ORA_END_USER_CONTEXT.CLIENT_IDENTIFIER = 'actor'",
                )
            ]

    class VerifyConnection:
        def cursor(self) -> VerifyCursor:
            return VerifyCursor()

        def close(self) -> None:
            return None

    class VerifyPool:
        def acquire(self) -> VerifyConnection:
            return VerifyConnection()

    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    _insert_real_entitlement_role(
        store,
        apply_status="APPLIED",
        data_grant_name="NL2SQL_DG_VERIFY",
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: VerifyPool())
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(
        service,
        "status",
        lambda: {
            "configured": True,
            "message": "ok",
            "objects": {
                "predicate_user_roles_grant": 1,
                "predicate_roles_grant": 1,
                "predicate_data_entitlements_grant": 1,
            },
        },
    )

    result = service.verify(_principal())

    assert result["passed"] is False
    vpd_check = next(
        item
        for item in result["checks"]  # type: ignore[attr-defined]
        if item["key"] == "vpd_policy:entitlement-sales"
    )
    assert vpd_check["passed"] is False
    assert "SQL_ASSIST_VPD_LEGACY_POL" in vpd_check["detail"]
    data_grant_check = next(
        item
        for item in result["checks"]  # type: ignore[attr-defined]
        if item["key"] == "data_grant:entitlement-sales"
    )
    assert data_grant_check["passed"] is True


def test_data_entitlement_sql_targets_real_object_with_role_predicate() -> None:
    settings = _settings()
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-sales",
        role_id="role-sales",
        resource_code="HR.EMPLOYEES",
        scope_code="SALES",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode="COLUMN_EQUALS",
        scope_column="DEPARTMENT_CODE",
    )

    statements = build_data_entitlement_statements(settings, entitlement)
    sql = "\n".join(statements)

    # #1022: DATA GRANTS ONLY を先に有効にし、DB role の SELECT は最後に付ける（fail-closed）。
    assert statements[0] == "SET USE DATA GRANTS ONLY ON HR.EMPLOYEES ENABLED"
    assert "CREATE OR REPLACE DATA GRANT APP_OWNER.NL2SQL_DG_" in sql
    assert "AS SELECT (EMPLOYEE_ID, DISPLAY_NAME)" in sql
    assert "ON HR.EMPLOYEES" in sql
    assert "TO NL2SQL_APP_DATA_ROLE" in sql
    assert "TO DEEPSEC_DATA_USER" not in sql
    assert "ORA_END_USER_CONTEXT.CLIENT_IDENTIFIER" in sql
    assert "SYS_CONTEXT('NL2SQL_APP_USER_CTX', 'APP_USER_ID')" not in sql
    assert "APP_OWNER.PLATFORM_USER_ROLES" in sql
    assert "APP_OWNER.NL2SQL_APP_DATA_ENTITLEMENTS" in sql
    assert "e.ENTITLEMENT_ID = 'entitlement-sales'" in sql
    assert "e.ROLE_ID = 'role-sales'" in sql
    assert "e.CAPABILITY = 'SELECT'" in sql
    assert "e.APPLY_STATUS = 'APPLIED'" in sql
    assert "HR.EMPLOYEES.DEPARTMENT_CODE = e.SCOPE_CODE" in sql
    assert statements[-1] == "GRANT SELECT ON HR.EMPLOYEES TO NL2SQL_APP_DB_ROLE"


def test_data_entitlement_sql_builds_structured_scope_filters() -> None:
    settings = _settings()
    filters = [
        DataEntitlementScopeFilter(
            column_name="REGION_CODE",
            operator="CONTAINS",
            value_type="TEXT",
            value="A_%'",
        ),
        DataEntitlementScopeFilter(
            column_name="TOTAL_AMOUNT",
            operator="BETWEEN",
            value_type="NUMBER",
            value="10",
            value_to="20.5",
        ),
        DataEntitlementScopeFilter(
            column_name="ORDERED_AT",
            operator="ON_OR_AFTER",
            value_type="TEMPORAL",
            value="2026-08-23",
        ),
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-filtered",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["REGION_CODE", "TOTAL_AMOUNT", "ORDERED_AT"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )

    statements = build_data_entitlement_statements(
        settings,
        entitlement,
        column_types={
            "REGION_CODE": "VARCHAR2",
            "TOTAL_AMOUNT": "NUMBER",
            "ORDERED_AT": "DATE",
        },
    )
    sql = "\n".join(statements)

    assert "HR.ORDERS.REGION_CODE LIKE '%A\\_\\%''%' ESCAPE '\\'" in sql
    assert "HR.ORDERS.TOTAL_AMOUNT BETWEEN 10 AND 20.5" in sql
    assert "HR.ORDERS.ORDERED_AT >= DATE '2026-08-23'" in sql
    assert "HR.ORDERS.REGION_CODE = e.SCOPE_CODE" not in sql


def test_data_entitlement_sql_builds_login_user_id_scope_filter() -> None:
    settings = _settings()
    filters = [
        DataEntitlementScopeFilter(
            column_name="APP_OWNER_USER_ID",
            operator="EQ",
            value_type="TEXT",
            value_source="LOGIN_USER_ID",
            value="ignored",
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-own-rows",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["ORDER_ID", "APP_OWNER_USER_ID"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )

    statements = build_data_entitlement_statements(
        settings,
        entitlement,
        column_types={"APP_OWNER_USER_ID": "VARCHAR2"},
    )
    sql = "\n".join(statements)

    assert (
        "HR.ORDERS.APP_OWNER_USER_ID = SYS_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID')"
    ) in sql
    assert "HR.ORDERS.APP_OWNER_USER_ID = 'ignored'" not in sql


def test_data_entitlement_sql_builds_number_login_user_id_scope_filter() -> None:
    settings = _settings()
    filters = [
        DataEntitlementScopeFilter(
            column_name="APP_OWNER_USER_ID",
            operator="EQ",
            value_type="NUMBER",
            value_source="LOGIN_USER_ID",
            value="ignored",
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-own-rows",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["ORDER_ID", "APP_OWNER_USER_ID"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )

    statements = build_data_entitlement_statements(
        settings,
        entitlement,
        column_types={"APP_OWNER_USER_ID": "NUMBER"},
    )
    sql = "\n".join(statements)

    assert "HR.ORDERS.APP_OWNER_USER_ID = CASE WHEN REGEXP_LIKE(" in sql
    assert "SYS_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID'), '^[0-9]+$')" in sql
    assert ("THEN TO_NUMBER(SYS_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID')) END") in sql
    assert "NULLIF" not in sql
    assert "HR.ORDERS.APP_OWNER_USER_ID = ignored" not in sql


def test_data_entitlement_sql_accepts_number_eq_positive_integer_literal() -> None:
    settings = _settings()
    filters = [
        DataEntitlementScopeFilter(
            column_name="APP_OWNER_USER_ID",
            operator="EQ",
            value_type="NUMBER",
            value="123",
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-owner-id",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["APP_OWNER_USER_ID"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )

    statements = build_data_entitlement_statements(
        settings,
        entitlement,
        column_types={"APP_OWNER_USER_ID": "NUMBER"},
    )

    assert "HR.ORDERS.APP_OWNER_USER_ID = 123" in "\n".join(statements)


@pytest.mark.parametrize("value", ["1.5", "-1", "0", ""])
def test_data_entitlement_sql_rejects_number_eq_non_positive_integer_literal(
    value: str,
) -> None:
    settings = _settings()
    filters = [
        DataEntitlementScopeFilter(
            column_name="APP_OWNER_USER_ID",
            operator="EQ",
            value_type="NUMBER",
            value=value,
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-owner-id",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["APP_OWNER_USER_ID"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )

    with pytest.raises(SecurityApiError, match="正整数"):
        build_data_entitlement_statements(
            settings,
            entitlement,
            column_types={"APP_OWNER_USER_ID": "NUMBER"},
        )


def test_data_entitlement_sql_accepts_materialized_view_target() -> None:
    settings = _settings()
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-summary",
        role_id="role-analytics",
        resource_code="DW.SALES_SUMMARY_MV",
        scope_code="*",
        capability="SELECT",
        target_owner="DW",
        target_object="SALES_SUMMARY_MV",
        target_type="MATERIALIZED VIEW",
        column_names=["REGION_CODE", "TOTAL_AMOUNT"],
        scope_mode="ALL",
        scope_column="",
    )

    statements = build_data_entitlement_statements(settings, entitlement)
    sql = "\n".join(statements)

    assert "CREATE OR REPLACE DATA GRANT APP_OWNER.NL2SQL_DG_" in sql
    assert "AS SELECT (REGION_CODE, TOTAL_AMOUNT)" in sql
    assert "ON DW.SALES_SUMMARY_MV" in sql
    # #1022: DATA GRANTS ONLY を先に有効にし、DB role の SELECT は最後に付ける（fail-closed）。
    assert statements[0] == "SET USE DATA GRANTS ONLY ON DW.SALES_SUMMARY_MV ENABLED"
    assert statements[-1] == "GRANT SELECT ON DW.SALES_SUMMARY_MV TO NL2SQL_APP_DB_ROLE"


def test_data_entitlement_validation_accepts_mview_with_table_dictionary_row() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, _params: dict[str, str]) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str, ...]]:
            if "OBJECT_TYPE" in self.sql:
                return [("TABLE",), ("MATERIALIZED VIEW",)]
            return [
                ("REGION_CODE", "VARCHAR2"),
                ("TOTAL_AMOUNT", "NUMBER"),
            ]

    service = DeepSecService(
        _settings(),
        SecurityService(InMemorySecurityStore(), _settings()),
        OraclePoolManager(_settings()),
    )
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-summary",
        role_id="role-analytics",
        resource_code="DW.SALES_SUMMARY_MV",
        scope_code="*",
        capability="SELECT",
        target_owner="DW",
        target_object="SALES_SUMMARY_MV",
        target_type="MATERIALIZED VIEW",
        column_names=["REGION_CODE", "TOTAL_AMOUNT"],
        scope_mode="ALL",
        scope_column="",
    )

    service._validate_data_entitlement(FakeCursor(), entitlement)  # noqa: SLF001


def test_data_entitlement_validation_accepts_structured_filters() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, _params: dict[str, str]) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str, ...]]:
            if "OBJECT_TYPE" in self.sql:
                return [("TABLE",)]
            return [
                ("REGION_CODE", "VARCHAR2"),
                ("TOTAL_AMOUNT", "NUMBER"),
                ("ORDERED_AT", "DATE"),
            ]

    filters = [
        DataEntitlementScopeFilter(
            column_name="REGION_CODE",
            operator="IN",
            value_type="TEXT",
            values=["SALES", "HR"],
        ),
        DataEntitlementScopeFilter(
            column_name="TOTAL_AMOUNT",
            operator="GTE",
            value_type="NUMBER",
            value="100",
        ),
        DataEntitlementScopeFilter(
            column_name="ORDERED_AT",
            operator="BEFORE",
            value_type="TEMPORAL",
            value="2026-09-01",
        ),
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-filtered",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["REGION_CODE", "TOTAL_AMOUNT"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )
    service = DeepSecService(
        _settings(),
        SecurityService(InMemorySecurityStore(), _settings()),
        OraclePoolManager(_settings()),
    )

    columns = service._validate_data_entitlement(FakeCursor(), entitlement)  # noqa: SLF001

    assert columns["TOTAL_AMOUNT"] == "NUMBER"


def test_data_entitlement_validation_rejects_filter_type_mismatch() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, _params: dict[str, str]) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str, ...]]:
            if "OBJECT_TYPE" in self.sql:
                return [("TABLE",)]
            return [("TOTAL_AMOUNT", "NUMBER")]

    filters = [
        DataEntitlementScopeFilter(
            column_name="TOTAL_AMOUNT",
            operator="CONTAINS",
            value_type="TEXT",
            value="100",
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-filtered",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["TOTAL_AMOUNT"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )
    service = DeepSecService(
        _settings(),
        SecurityService(InMemorySecurityStore(), _settings()),
        OraclePoolManager(_settings()),
    )

    with pytest.raises(SecurityApiError, match="value_type"):
        service._validate_data_entitlement(FakeCursor(), entitlement)  # noqa: SLF001


def test_data_entitlement_validation_rejects_app_user_id_without_eq() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, _params: dict[str, str]) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str, ...]]:
            if "OBJECT_TYPE" in self.sql:
                return [("TABLE",)]
            return [("APP_OWNER_USER_ID", "VARCHAR2")]

    filters = [
        DataEntitlementScopeFilter(
            column_name="APP_OWNER_USER_ID",
            operator="NE",
            value_type="TEXT",
            value_source="LOGIN_USER_ID",
        )
    ]
    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-filtered",
        role_id="role-sales",
        resource_code="HR.ORDERS",
        scope_code=scope_filters_scope_code(filters),
        capability="SELECT",
        target_owner="HR",
        target_object="ORDERS",
        target_type="TABLE",
        column_names=["APP_OWNER_USER_ID"],
        scope_mode="FILTERS",
        scope_filters=filters,
    )
    service = DeepSecService(
        _settings(),
        SecurityService(InMemorySecurityStore(), _settings()),
        OraclePoolManager(_settings()),
    )

    with pytest.raises(SecurityApiError, match="ログインユーザーID"):
        service._validate_data_entitlement(FakeCursor(), entitlement)  # noqa: SLF001


@pytest.mark.parametrize(
    ("owner", "object_name", "allowlist"),
    [
        ("APP_OWNER", "RAG_DOCUMENTS", []),
        ("APP_OWNER", "AGENT_RUNS", []),
        ("APP_OWNER", "NL2SQL_PROFILES", []),
        ("HR", "PLATFORM_USERS", []),
        ("FINANCE", "LEDGER", ["HR"]),
    ],
    ids=["rag", "agent", "nl2sql-state", "platform", "owner-not-allowed"],
)
def test_data_entitlement_validation_rejects_targets_outside_picker(
    owner: str, object_name: str, allowlist: list[str]
) -> None:
    """API を直接呼んでも、対象の選択欄に出ない表へ Data Grant を作らない。"""
    executed: list[str] = []

    class FakeCursor:
        def execute(self, sql: str, _params: dict[str, str]) -> None:
            executed.append(sql)

        def fetchall(self) -> list[tuple[str, ...]]:
            return [("TABLE",)]

    entitlement = DataEntitlementRecord(
        entitlement_id="entitlement-outside",
        role_id="role-sales",
        resource_code=f"{owner}.{object_name}",
        scope_code="*",
        capability="SELECT",
        target_owner=owner,
        target_object=object_name,
        target_type="TABLE",
        column_names=["ID"],
    )
    settings = _settings()
    settings.nl2sql_schema_owner_allowlist = allowlist
    service = DeepSecService(
        settings,
        SecurityService(InMemorySecurityStore(), settings),
        OraclePoolManager(settings),
    )

    with pytest.raises(SecurityApiError, match="Data Grant の対象にできません") as error:
        service._validate_data_entitlement(FakeCursor(), entitlement)  # noqa: SLF001
    assert error.value.status_code == 400
    assert executed == []


def test_data_entitlement_apply_rejects_predicate_over_4000_before_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_role_id = "role-" + "x" * 4000
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    store.roles[long_role_id] = RoleRecord(
        role_id=long_role_id,
        role_code="LONG_ROLE",
        display_name="長いロール",
        description="predicate length guard",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[
            DataEntitlementRecord(
                entitlement_id="entitlement-long",
                role_id=long_role_id,
                resource_code="HR.EMPLOYEES",
                scope_code="*",
                capability="SELECT",
                target_owner="HR",
                target_object="EMPLOYEES",
                target_type="TABLE",
                column_names=["EMPLOYEE_ID"],
                scope_mode="ALL",
            )
        ],
    )
    executed: list[bool] = []
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda *_args, **_kwargs: executed.append(True),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError, match="4000"):
        service.apply_data_entitlements(
            long_role_id,
            expected_version=1,
            confirmation=DEEPSEC_APPLY_CONFIRMATION,
            entitlements=cast(RoleRecord, store.roles[long_role_id]).entitlements,
            actor=_principal(),
        )

    assert executed == []


def test_data_entitlements_reports_missing_scope_filters_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    security = SecurityService(InMemorySecurityStore(), settings)
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    def raise_missing_column(*_args: object, **_kwargs: object) -> list[RoleRecord]:
        raise RuntimeError('ORA-00904: "SCOPE_FILTERS": invalid identifier')

    monkeypatch.setattr(security, "list_roles", raise_missing_column)

    with pytest.raises(SecurityApiError) as exc_info:
        service.data_entitlements()

    assert exc_info.value.status_code == 409
    assert "SCOPE_FILTERS" in exc_info.value.public_message
    assert "migration" in exc_info.value.public_message


def test_plan_reports_missing_deepsec_migration_table_as_409() -> None:
    class MissingDeepSecMigrationStore(InMemorySecurityStore):
        def get_deepsec_states(self) -> dict[tuple[str, int], dict[str, object]]:
            raise RuntimeError(
                'ORA-00942: table or view "ADMIN"."NL2SQL_DEEPSEC_MIGRATIONS" does not exist'
            )

    settings = _settings()
    security = SecurityService(MissingDeepSecMigrationStore(), settings)
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    with pytest.raises(SecurityApiError) as exc_info:
        service.plan()

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    assert "app_security_migrate" in exc_info.value.public_message
    assert "ORA-00942" not in exc_info.value.public_message
    assert "NL2SQL_DEEPSEC_MIGRATIONS" not in exc_info.value.public_message


def test_v001_application_context_sets_login_user_id_and_clears_context() -> None:
    context_step = build_v001_plan(_settings())[1]
    package_spec = context_step.statements[0]
    package_body = context_step.statements[1]
    compile_check = context_step.statements[2]

    assert context_step.key == "application_context"
    assert package_spec.strip().endswith("END NL2SQL_DEEPSEC_CTX_PKG;")
    assert package_body.strip().endswith("END NL2SQL_DEEPSEC_CTX_PKG;")
    assert "PROCEDURE SET_APP_USER_UUID(p_user_uuid IN VARCHAR2)" in package_spec
    assert "WHERE USER_UUID = p_user_uuid AND STATUS = 'ACTIVE'" in package_body
    assert "DBMS_SESSION.CLEAR_CONTEXT" not in package_body
    assert "SELECT LOGIN_USER_ID INTO v_login_user_id" in package_body
    assert (
        "DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID', v_login_user_id)"
        in package_body
    )
    assert "DBMS_SESSION.SET_IDENTIFIER(p_user_uuid)" in package_body
    assert "DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID', NULL)" in package_body
    assert "DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'APP_USER_ID', NULL)" in package_body
    assert "DBMS_SESSION.CLEAR_IDENTIFIER" in package_body
    assert "ALL_ERRORS" in compile_check
    assert "NL2SQL_DEEPSEC_CTX_PKG compile error" in compile_check


def test_apply_rejects_unknown_checksum_before_oracle_execution() -> None:
    settings = _settings()
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))
    with pytest.raises(SecurityApiError, match="チェックサム"):
        service.apply_step(1, "0" * 64, DEEPSEC_APPLY_CONFIRMATION, _principal())


def test_apply_role_step_creates_missing_end_user_before_data_role_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END USER が無い新しい配備から V001.1 を適用できる（#1676）。"""

    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role_step = build_v001_plan(settings)[0]
    executed: list[list[str]] = []
    executor_kwargs: list[dict[str, object]] = []

    def fake_execute(_conn: object, statements: list[str], **kwargs: object) -> list[object]:
        executed.append(list(statements))
        executor_kwargs.append(kwargs)
        return [
            {"status": "success", "index": index}
            for index, _statement in enumerate(statements, start=1)
        ]

    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: None)
    monkeypatch.setattr("app.security.deepsec.oracle_statement_executor.execute", fake_execute)
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    result = service.apply_step(
        role_step.step_no,
        role_step.checksum,
        DEEPSEC_APPLY_CONFIRMATION,
        _principal(),
    )

    assert result["status"] == "APPLIED"
    assert PASSWORD_PLACEHOLDER not in repr(result)
    assert settings.oracle_deepsec_data_user_password not in repr(result)
    statements = executed[0]
    create_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("CREATE END USER IF NOT EXISTS DEEPSEC_DATA_USER")
    )
    grant_index = statements.index("GRANT DATA ROLE NL2SQL_APP_DATA_ROLE TO DEEPSEC_DATA_USER")
    assert create_index < grant_index
    # 実行する文だけ placeholder を保存済みのパスワードに置き換える。
    assert statements[create_index] == (
        'CREATE END USER IF NOT EXISTS DEEPSEC_DATA_USER IDENTIFIED BY "DeepSecret!123" '
        "SCHEMA APP_OWNER"
    )
    assert not any(statement.startswith("ALTER END USER") for statement in statements)
    assert executor_kwargs[0]["include_sql"] is False
    plan = service.plan()
    step_payload = plan["steps"][0]  # type: ignore[index]
    assert step_payload["status"] == "APPLIED"
    plan_sql = "\n".join(step_payload["sql"])
    assert PASSWORD_PLACEHOLDER in plan_sql
    assert settings.oracle_deepsec_data_user_password not in repr(plan)


def test_apply_without_saved_data_user_password_returns_409_before_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """パスワード未保存の適用は 500 でなく、保存を促す 409 で止める（#1676）。"""

    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))
    role_step = build_v001_plan(settings)[0]

    def fail_if_executed(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise AssertionError("Oracle executor must not run without a saved DATA USER password")

    monkeypatch.setattr("app.security.deepsec.oracle_statement_executor.execute", fail_if_executed)

    with pytest.raises(SecurityApiError) as exc_info:
        service.apply_step(
            role_step.step_no,
            role_step.checksum,
            DEEPSEC_APPLY_CONFIRMATION,
            _principal(),
        )

    assert exc_info.value.status_code == 409
    assert "DATA USER パスワードが保存されていません" in str(exc_info.value.public_message)
    assert "「DATA USER 認証」で DATA USER パスワードを保存してから" in str(
        exc_info.value.public_message
    )
    assert store.get_deepsec_states() == {}
    plan = service.plan()
    assert plan["has_data_user_password"] is False


@pytest.mark.parametrize(
    ("oracle_error", "expected", "unexpected"),
    [
        (
            "ORA-52516: END USER or DATA ROLE DEEPSEC_DATA_USER does not exist.",
            "DATA USER（END USER）がまだ Oracle にありません。",
            "DATA ROLE NL2SQL_APP_DATA_ROLE がまだ",
        ),
        (
            "ORA-52516: END USER or DATA ROLE NL2SQL_APP_DATA_ROLE does not exist.",
            "DATA ROLE NL2SQL_APP_DATA_ROLE がまだ Oracle にありません。",
            "DATA USER（END USER）がまだ",
        ),
        (
            "ORA-52516: 別の言語の応答",
            "DATA USER（END USER）がまだ Oracle にありません。",
            "DATA ROLE NL2SQL_APP_DATA_ROLE がまだ",
        ),
    ],
    ids=["end-user", "data-role", "localized"],
)
def test_apply_maps_ora_52516_to_next_action_guidance(
    monkeypatch: pytest.MonkeyPatch,
    oracle_error: str,
    expected: str,
    unexpected: str,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role_step = build_v001_plan(settings)[0]
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, _statements, **_kwargs: [
            {"status": "success", "index": 1},
            # Oracle の応答にパスワードが混ざっても利用者には出さない。
            {"status": "error", "index": 9, "error_message": f"{oracle_error} DeepSecret!123"},
        ],
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError) as exc_info:
        service.apply_step(
            role_step.step_no,
            role_step.checksum,
            DEEPSEC_APPLY_CONFIRMATION,
            _principal(),
        )

    detail = str(exc_info.value.public_message)
    assert exc_info.value.status_code == 409
    assert expected in detail
    assert unexpected not in detail
    assert "9 番目の文で失敗しました。" in detail
    assert "ORA-52516" in detail
    assert "DeepSecret!123" not in detail
    plan = service.plan()
    step_payload = plan["steps"][0]  # type: ignore[index]
    assert step_payload["status"] == "FAILED"
    assert "DeepSecret!123" not in str(step_payload["error_message"])


@pytest.mark.parametrize("confirmation", ["", "ADMIN", "admin_execute"])
def test_apply_requires_confirmation_before_oracle_or_state(
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))
    step = build_v001_plan(settings)[0]
    executed: list[bool] = []

    def fail_if_executed(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        executed.append(True)
        raise AssertionError("Oracle executor must not run without ADMIN_EXECUTE confirmation")

    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        fail_if_executed,
    )

    with pytest.raises(SecurityApiError, match="confirmation=ADMIN_EXECUTE"):
        service.apply_step(step.step_no, step.checksum, confirmation, _principal())

    assert executed == []
    assert store.get_deepsec_states() == {}


@pytest.mark.parametrize("confirmation", ["", "ADMIN", "admin_execute"])
def test_data_entitlement_apply_requires_confirmation_before_oracle(
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store)
    service = DeepSecService(settings, security, OraclePoolManager(settings))
    executed: list[bool] = []

    def fail_if_validated(*_args: object, **_kwargs: object) -> None:
        executed.append(True)
        raise AssertionError("metadata validation must not run without ADMIN_EXECUTE")

    monkeypatch.setattr(service, "_validate_data_entitlement", fail_if_validated)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda *_args, **_kwargs: executed.append(True),
    )

    with pytest.raises(SecurityApiError, match="confirmation=ADMIN_EXECUTE"):
        service.apply_data_entitlements(
            "role-sales",
            expected_version=role.version,
            confirmation=confirmation,
            entitlements=role.entitlements,
            actor=_principal(),
        )

    assert executed == []
    assert store.get_role("role-sales").entitlements[0].apply_status == "PENDING"  # type: ignore[union-attr]


def test_data_entitlement_apply_executes_generated_sql_and_marks_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store)
    executed: list[str] = []
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.extend(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(service, "_managed_oracle_data_grants", lambda _cursor: [])

    result = service.apply_data_entitlements(
        "role-sales",
        expected_version=role.version,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=role.entitlements,
        actor=_principal(),
    )

    stored = store.get_role("role-sales").entitlements[0]  # type: ignore[union-attr]
    assert result["status"] == "APPLIED"
    assert stored.apply_status == "APPLIED"
    assert stored.data_grant_name.startswith("NL2SQL_DG_")
    assert stored.sql_checksum
    # #1022: DATA GRANTS ONLY を先に有効にし、DB role の SELECT は最後に付ける（fail-closed）。
    assert executed[0] == "SET USE DATA GRANTS ONLY ON HR.EMPLOYEES ENABLED"
    assert executed[1] == f"DROP DATA GRANT IF EXISTS APP_OWNER.{stored.data_grant_name}"
    assert "CREATE OR REPLACE DATA GRANT APP_OWNER.NL2SQL_DG_" in executed[2]
    assert "TO NL2SQL_APP_DATA_ROLE" in executed[2]
    assert "TO DEEPSEC_DATA_USER" not in executed[2]
    assert "e.ROLE_ID = 'role-sales'" in executed[2]
    assert executed[3] == "GRANT SELECT ON HR.EMPLOYEES TO NL2SQL_APP_DB_ROLE"


def test_data_entitlement_apply_allows_empty_policy_sync_without_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    store.roles["role-sales"] = RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="営業テーブルを参照するロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[],
    )
    executed: list[str] = []
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: executed.extend(list(statements)),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(service, "_managed_oracle_data_grants", lambda _cursor: [])

    result = service.apply_data_entitlements(
        "role-sales",
        expected_version=1,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=[],
        actor=_principal(),
    )

    assert result["status"] == "APPLIED"
    assert result["applied_count"] == 0
    assert result["cleanup_count"] == 0
    assert executed == []


def test_data_entitlement_apply_all_deleted_drops_stale_grants_and_disables_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store, data_grant_name="NL2SQL_DG_OLD")
    executed: list[str] = []
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.extend(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)
    result = service.apply_data_entitlements(
        "role-sales",
        expected_version=role.version,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=[],
        actor=_principal(),
    )

    assert result["status"] == "APPLIED"
    assert result["applied_count"] == 0
    # #1022: 以前は DISABLED → DROP の順で、DB role の SELECT を外さなかった（DATA USER が
    # 全行を読める fail-open）。SELECT の REVOKE → DROP → DISABLED の順にする。
    assert result["cleanup_count"] == 3
    assert "REVOKE SELECT ON HR.EMPLOYEES FROM NL2SQL_APP_DB_ROLE" in executed[0]
    assert executed[1] == "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_OLD"
    assert "SET USE DATA GRANTS ONLY ON HR.EMPLOYEES DISABLED" in executed[2]
    assert store.get_role("role-sales").entitlements == []  # type: ignore[union-attr]


def test_data_entitlement_apply_replaces_deleted_grant_with_new_grant_on_same_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store, data_grant_name="NL2SQL_DG_OLD")
    replacement = replace(
        role.entitlements[0],
        entitlement_id="entitlement-new",
        data_grant_name="",
    )
    executed: list[str] = []
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.extend(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)
    result = service.apply_data_entitlements(
        "role-sales",
        expected_version=role.version,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=[replacement],
        actor=_principal(),
    )

    assert result["status"] == "APPLIED"
    assert result["applied_count"] == 1
    assert result["cleanup_count"] == 1
    assert executed[0] == "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_OLD"
    # #1022: DATA GRANTS ONLY を先に有効にし、DB role の SELECT は最後に付ける（fail-closed）。
    assert executed[1] == "SET USE DATA GRANTS ONLY ON HR.EMPLOYEES ENABLED"
    assert "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_" in executed[2]
    assert "CREATE OR REPLACE DATA GRANT APP_OWNER.NL2SQL_DG_" in executed[3]
    assert executed[4] == "GRANT SELECT ON HR.EMPLOYEES TO NL2SQL_APP_DB_ROLE"


def test_data_entitlement_preview_and_apply_cover_all_role_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store)
    second = replace(
        role.entitlements[0],
        entitlement_id="entitlement-orders",
        resource_code="HR.ORDERS",
        target_object="ORDERS",
        scope_code="*",
        scope_mode="ALL",
        scope_column="",
        data_grant_name="",
    )
    desired = [role.entitlements[0], second]
    executed: list[str] = []
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.extend(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    preview = service.preview_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=desired,
        actor=_principal(),
    )

    assert len(preview["data_entitlements"]) == 2  # type: ignore[arg-type]
    assert preview["cleanup_sql"] == []
    assert all(item["sql"] for item in preview["data_entitlements"])  # type: ignore[attr-defined]
    assert len(preview["checksum"]) == 64  # type: ignore[arg-type]

    result = service.apply_data_entitlements(
        role.role_id,
        expected_version=role.version,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=desired,
        actor=_principal(),
    )

    stored = store.get_role(role.role_id)
    assert result["applied_count"] == 2
    assert result["checksum"] == preview["checksum"]
    assert stored is not None
    assert stored.version == 2
    assert [item.apply_status for item in stored.entitlements] == ["APPLIED", "APPLIED"]
    assert "GRANT SELECT ON HR.EMPLOYEES TO NL2SQL_APP_DB_ROLE" in executed
    assert "GRANT SELECT ON HR.ORDERS TO NL2SQL_APP_DB_ROLE" in executed


def test_data_entitlement_preview_includes_role_scoped_deletion_without_other_role_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store, data_grant_name="NL2SQL_DG_SALES")
    store.roles["role-audit"] = RoleRecord(
        role_id="role-audit",
        role_code="AUDIT_VIEWER",
        display_name="監査閲覧",
        description="同じ対象を利用する別ロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[
            replace(
                role.entitlements[0],
                entitlement_id="entitlement-audit",
                role_id="role-audit",
                data_grant_name="NL2SQL_DG_AUDIT",
            )
        ],
    )
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    executed: list[str] = []
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.extend(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    preview = service.preview_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=[],
        actor=_principal(),
    )

    assert preview["data_entitlements"] == []
    assert preview["cleanup_sql"] == ["DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_SALES"]
    assert "NL2SQL_DG_AUDIT" not in "\n".join(preview["cleanup_sql"])
    stored = store.get_role(role.role_id)
    assert stored is not None
    assert stored.entitlements[0].data_grant_name == "NL2SQL_DG_SALES"

    result = service.apply_data_entitlements(
        role.role_id,
        expected_version=role.version,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=[],
        actor=_principal(),
    )

    assert result["cleanup_count"] == 1
    assert executed == ["DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_SALES"]
    selected_after = store.get_role(role.role_id)
    other_after = store.get_role("role-audit")
    assert selected_after is not None
    assert selected_after.entitlements == []
    assert other_after is not None
    assert other_after.entitlements[0].data_grant_name == "NL2SQL_DG_AUDIT"


def test_data_entitlement_apply_rejects_stale_version_before_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store)
    executed: list[bool] = []
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda *_args, **_kwargs: executed.append(True),
    )
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    with pytest.raises(SecurityApiError, match="別の操作"):
        service.apply_data_entitlements(
            role.role_id,
            expected_version=role.version + 1,
            confirmation=DEEPSEC_APPLY_CONFIRMATION,
            entitlements=role.entitlements,
            actor=_principal(),
        )

    assert executed == []
    stored = store.get_role(role.role_id)
    assert stored is not None
    assert stored.version == role.version


def test_data_entitlement_apply_failure_preserves_previous_role_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role = _insert_real_entitlement_role(store, data_grant_name="NL2SQL_DG_SALES")
    desired = [replace(role.entitlements[0], column_names=["EMPLOYEE_ID"])]
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda *_args, **_kwargs: [{"status": "error", "index": 1, "error_message": "ORA-20000"}],
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError, match="ORA-20000"):
        service.apply_data_entitlements(
            role.role_id,
            expected_version=role.version,
            confirmation=DEEPSEC_APPLY_CONFIRMATION,
            entitlements=desired,
            actor=_principal(),
        )

    stored = store.get_role(role.role_id)
    assert stored is not None
    assert stored.version == role.version
    assert stored.entitlements[0].column_names == ["EMPLOYEE_ID", "DISPLAY_NAME"]
    assert stored.entitlements[0].data_grant_name == "NL2SQL_DG_SALES"


def test_data_entitlement_preview_generates_sql_without_store_or_oracle_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    role = RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="営業テーブルを参照するロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[],
    )
    store.roles[role.role_id] = role
    security = SecurityService(store, settings)
    entitlement = DataEntitlementRecord(
        entitlement_id="",
        role_id="role-sales",
        resource_code="HR.EMPLOYEES",
        scope_code="SALES",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode="COLUMN_EQUALS",
        scope_column="DEPARTMENT_CODE",
    )
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda *_args, **_kwargs: pytest.fail("preview must not execute Oracle DDL"),
    )
    monkeypatch.setattr(
        store,
        "set_deepsec_entitlement_apply_state",
        lambda *_args, **_kwargs: pytest.fail("preview must not update apply state"),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    result = service.preview_data_entitlements(
        "role-sales",
        expected_version=1,
        entitlements=[entitlement],
        actor=_principal(),
    )

    preview = result["data_entitlements"][0]  # type: ignore[index]
    assert result["role_id"] == "role-sales"
    assert preview["entitlement_id"]
    assert preview["data_grant_name"].startswith("NL2SQL_DG_")
    assert preview["checksum"]
    assert "CREATE OR REPLACE DATA GRANT APP_OWNER.NL2SQL_DG_" in "\n".join(preview["sql"])
    assert store.get_role("role-sales").entitlements == []  # type: ignore[union-attr]


def test_data_entitlement_preview_id_keeps_saved_apply_sql_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    role = RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="営業テーブルを参照するロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[],
    )
    store.roles[role.role_id] = role
    security = SecurityService(store, settings)
    draft = DataEntitlementRecord(
        entitlement_id="",
        role_id="role-sales",
        resource_code="HR.EMPLOYEES",
        scope_code="SALES",
        capability="SELECT",
        target_owner="HR",
        target_object="EMPLOYEES",
        target_type="TABLE",
        column_names=["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode="COLUMN_EQUALS",
        scope_column="DEPARTMENT_CODE",
    )
    monkeypatch.setattr(DeepSecService, "_validate_data_entitlement", lambda *_args: None)
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    preview = service.preview_data_entitlements(  # type: ignore[index]
        "role-sales",
        expected_version=1,
        entitlements=[draft],
        actor=_principal(),
    )["data_entitlements"][0]
    saved = replace(draft, entitlement_id=str(preview["entitlement_id"]))
    security.update_role_data_entitlements(
        "role-sales",
        expected_version=1,
        entitlements=[saved],
        actor=_principal(),
    )
    stored = store.get_role("role-sales").entitlements[0]  # type: ignore[union-attr]
    apply_sql = build_data_entitlement_statements(settings, stored)

    assert str(preview["data_grant_name"]) in "\n".join(apply_sql)
    assert preview["checksum"] == _data_grant_checksum_for_test(apply_sql)


def test_data_entitlement_preview_rejects_invalid_columns() -> None:
    class PreviewCursor:
        def __init__(self) -> None:
            self.sql = ""

        def __enter__(self) -> PreviewCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: dict[str, str]) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str, ...]]:
            if "OBJECT_TYPE" in self.sql:
                return [("TABLE",)]
            return [("EMPLOYEE_ID", "NUMBER")]

    class PreviewConnection:
        def cursor(self) -> PreviewCursor:
            return PreviewCursor()

        def close(self) -> None:
            return None

    settings = _settings()
    store = InMemorySecurityStore()
    store.roles["role-sales"] = RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="営業テーブルを参照するロール",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[],
    )
    manager = OraclePoolManager(settings)
    manager._control_pool = _FakePool(PreviewConnection())  # type: ignore[arg-type]  # noqa: SLF001
    service = DeepSecService(settings, SecurityService(store, settings), manager)

    with pytest.raises(SecurityApiError, match="対象 object に存在しない列"):
        service.preview_data_entitlements(
            "role-sales",
            expected_version=1,
            entitlements=[
                DataEntitlementRecord(
                    entitlement_id="",
                    role_id="role-sales",
                    resource_code="HR.EMPLOYEES",
                    scope_code="*",
                    capability="SELECT",
                    target_owner="HR",
                    target_object="EMPLOYEES",
                    target_type="TABLE",
                    column_names=["MISSING_COLUMN"],
                    scope_mode="ALL",
                    scope_column="",
                )
            ],
            actor=_principal(),
        )


def test_plan_ignores_stale_checksum_state() -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    current_step = build_v001_plan(settings)[0]
    store.set_deepsec_state(
        version="V001",
        step_no=current_step.step_no,
        step_key=current_step.key,
        checksum="0" * 64,
        status="APPLIED",
        error_message="",
        executed_by_user_uuid="actor",
    )
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    plan = service.plan()

    assert plan["has_data_user_password"] is True
    assert plan["steps"][0]["status"] == "PENDING"  # type: ignore[index]


def test_plan_marks_stale_application_context_for_reapply() -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    context_step = build_v001_plan(settings)[1]
    store.set_deepsec_state(
        version="V001",
        step_no=context_step.step_no,
        step_key=context_step.key,
        checksum="legacy-clear-context-checksum",
        status="APPLIED",
        error_message="",
        executed_by_user_uuid="actor",
    )
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    plan = service.plan()

    assert plan["steps"][1]["key"] == "application_context"  # type: ignore[index]
    assert plan["steps"][1]["status"] == "PENDING"  # type: ignore[index]


def test_apply_application_context_after_stale_checksum_closes_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role_step, context_step = build_v001_plan(settings)[:2]
    store.set_deepsec_state(
        version="V001",
        step_no=role_step.step_no,
        step_key=role_step.key,
        checksum=role_step.checksum,
        status="APPLIED",
        error_message="",
        executed_by_user_uuid="actor",
    )
    store.set_deepsec_state(
        version="V001",
        step_no=context_step.step_no,
        step_key=context_step.key,
        checksum="legacy-clear-context-checksum",
        status="APPLIED",
        error_message="",
        executed_by_user_uuid="actor",
    )
    closed: list[bool] = []
    executed: list[list[str]] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.append(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    result = service.apply_step(
        context_step.step_no,
        context_step.checksum,
        DEEPSEC_APPLY_CONFIRMATION,
        _principal(),
    )

    assert result["status"] == "APPLIED"
    assert closed == [True]
    assert len(executed) == 1
    assert any(
        "DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID', NULL)" in statement
        for statement in executed[0]
    )
    assert any(
        "DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'APP_USER_ID', NULL)" in statement
        for statement in executed[0]
    )
    assert any("DBMS_SESSION.CLEAR_IDENTIFIER" in statement for statement in executed[0])
    assert any("NL2SQL_DEEPSEC_CTX_PKG compile error" in statement for statement in executed[0])
    plan = service.plan()
    assert plan["steps"][1]["status"] == "APPLIED"  # type: ignore[index]


def test_apply_application_context_compile_error_marks_step_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    role_step, context_step = build_v001_plan(settings)[:2]
    store.set_deepsec_state(
        version="V001",
        step_no=role_step.step_no,
        step_key=role_step.key,
        checksum=role_step.checksum,
        status="APPLIED",
        error_message="",
        executed_by_user_uuid="actor",
    )
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, _statements, **_kwargs: [
            {"status": "success", "index": 1},
            {"status": "success", "index": 2},
            {
                "status": "error",
                "index": 3,
                "error_message": "ORA-20002: NL2SQL_DEEPSEC_CTX_PKG compile error",
            },
        ],
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError, match="compile error") as exc_info:
        service.apply_step(
            context_step.step_no,
            context_step.checksum,
            DEEPSEC_APPLY_CONFIRMATION,
            _principal(),
        )

    assert exc_info.value.status_code == 409
    assert closed == []
    plan = service.plan()
    assert plan["steps"][1]["status"] == "FAILED"  # type: ignore[index]
    assert "compile error" in plan["steps"][1]["error_message"]  # type: ignore[index]


@pytest.mark.parametrize("confirmation", ["", "ADMIN_EXECUTE", "admin_reset"])
def test_reset_requires_confirmation_before_oracle_or_state(
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    _mark_deepsec_steps_applied(store, settings)
    _insert_real_entitlement_role(
        store,
        apply_status="APPLIED",
        data_grant_name="NL2SQL_DG_MANAGED",
    )
    service = DeepSecService(settings, security, OraclePoolManager(settings))
    executed: list[bool] = []

    def fail_if_executed(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        executed.append(True)
        raise AssertionError("Oracle executor must not run without ADMIN_RESET confirmation")

    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        fail_if_executed,
    )

    with pytest.raises(SecurityApiError, match="confirmation=ADMIN_RESET"):
        service.reset("V001", confirmation, _principal())

    assert executed == []
    assert len(store.get_deepsec_states()) == 4


def test_reset_executes_fixed_teardown_and_clears_states_without_data_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_text = (
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=DeepSecret!123\n"
    )
    env_file.write_text(env_text, encoding="utf-8")
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    settings = _settings(data_user_password="")
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    _mark_deepsec_steps_applied(store, settings)
    _insert_real_entitlement_role(
        store,
        apply_status="APPLIED",
        data_grant_name="NL2SQL_DG_MANAGED",
    )
    closed: list[bool] = []
    executed: list[str] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, statements, **_kwargs: (
            executed.extend(list(statements))  # type: ignore[func-returns-value]
            or [
                {"status": "success", "index": index}
                for index, _statement in enumerate(statements, start=1)
            ]
        ),
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    result = service.reset("V001", DEEPSEC_RESET_CONFIRMATION, _principal())

    assert result["status"] == "RESET"
    assert result["step_numbers"] == [1, 2, 3, 4]
    assert closed == [True]
    assert len(executed) == len(
        build_v001_reset_statements(settings, store.get_role("role-sales").entitlements)  # type: ignore[union-attr]
    )
    # #1022: DATA GRANTS ONLY を無効にする前に、対象の SELECT を DB role から外す。
    assert "REVOKE SELECT ON HR.EMPLOYEES FROM NL2SQL_APP_DB_ROLE" in executed[0]
    assert "SET USE DATA GRANTS ONLY ON HR.EMPLOYEES DISABLED" in executed[1]
    assert executed[2] == "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_MANAGED"
    assert "SET USE DATA GRANTS ONLY ON APP_OWNER.NL2SQL_DEEPSEC_PROBE DISABLED" in executed[3]
    assert executed[4] == "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DEEPSEC_PROBE_SENSITIVE"
    assert executed[5] == "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DEEPSEC_PROBE_ROWS"
    assert "DROP TABLE APP_OWNER.NL2SQL_DEEPSEC_PROBE CASCADE CONSTRAINTS PURGE" in executed[6]
    assert "DROP CONTEXT NL2SQL_APP_USER_CTX" in executed[7]
    assert "DROP PACKAGE APP_OWNER.NL2SQL_DEEPSEC_CTX_PKG" in executed[8]
    assert executed[9] == "DROP END USER IF EXISTS DEEPSEC_DATA_USER"
    assert executed[10] == "DROP DATA ROLE IF EXISTS NL2SQL_APP_DATA_ROLE"
    assert "DROP ROLE NL2SQL_APP_DB_ROLE" in executed[11]
    assert store.get_deepsec_states() == {}
    assert store.get_role("role-sales").entitlements[0].apply_status == "PENDING"  # type: ignore[union-attr]
    assert settings.oracle_deepsec_enabled is True
    assert settings.oracle_deepsec_data_user_password == ""
    assert env_file.read_text(encoding="utf-8") == env_text


def test_reset_failure_keeps_states_and_does_not_close_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    _mark_deepsec_steps_applied(store, settings)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    monkeypatch.setattr(
        "app.security.deepsec.oracle_statement_executor.execute",
        lambda _conn, _statements, **_kwargs: [
            {"status": "success", "index": 1},
            {
                "status": "error",
                "index": 2,
                "error_message": "ORA-01031: insufficient privileges",
            },
        ],
    )
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: _FakePool(_FakeConnection([])))
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError, match="ORA-01031") as exc_info:
        service.reset("V001", DEEPSEC_RESET_CONFIRMATION, _principal())

    assert exc_info.value.status_code == 409
    assert closed == []
    assert len(store.get_deepsec_states()) == 4


def test_update_config_persists_runtime_settings_and_closes_pools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NL2SQL_LOG_LEVEL=INFO",
                "NL2SQL_RUNTIME_MODE=oracle",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    settings = _settings(deepsec_enabled=False, data_user_password="")
    settings.oracle_dsn = ""
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    # 「${...}」は .env を読み直すと環境変数として展開されるので保存しない。
    with pytest.raises(SecurityApiError) as interpolated:
        service.update_config("Abc${HOME}defghij")
    assert interpolated.value.status_code == 400
    assert settings.oracle_deepsec_enabled is False
    assert closed == []

    status = service.update_config("DeepSecret!456")

    assert status["deepsec_enabled"] is True
    assert status["data_user"] == "DEEPSEC_DATA_USER"
    assert status["has_data_user_password"] is True
    assert settings.oracle_deepsec_enabled is True
    assert settings.oracle_deepsec_data_user == "DEEPSEC_DATA_USER"
    assert settings.oracle_deepsec_data_user_password == "DeepSecret!456"
    assert closed == [True]
    env_text = env_file.read_text(encoding="utf-8")
    # DeepSec の設定は製品の backend/.env の末尾に節を作って書き、他の値は残す（#211）。
    assert env_text == (
        "NL2SQL_LOG_LEVEL=INFO\n"
        "NL2SQL_RUNTIME_MODE=oracle\n"
        "\n"
        "# Deep Data Security\n"
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=DeepSecret!456\n"
    )
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_update_config_syncs_existing_data_user_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NL2SQL_LOG_LEVEL=INFO\n", encoding="utf-8")
    env_file.chmod(0o600)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))
    calls: list[tuple[str, object | None]] = []

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: object | None = None) -> None:
            calls.append((sql, params))

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="OldSecret!123")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(
        service,
        "status",
        lambda: {
            "deepsec_enabled": True,
            "data_user": "DEEPSEC_DATA_USER",
            "has_data_user_password": True,
        },
    )

    status = service.update_config("DeepSecret!456")

    executed_sql = "\n".join(sql for sql, _params in calls)
    assert status["has_data_user_password"] is True
    assert "SELECT COUNT(*) FROM DBA_END_USERS" in executed_sql
    assert "ALTER END USER IF EXISTS DEEPSEC_DATA_USER" in executed_sql
    assert 'IDENTIFIED BY "DeepSecret!456"' in executed_sql
    assert PASSWORD_PLACEHOLDER not in executed_sql
    assert closed == [True]
    env_text = env_file.read_text(encoding="utf-8")
    assert "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=DeepSecret!456" in env_text


def test_sync_saved_config_password_uses_saved_password_and_closes_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    calls: list[tuple[str, object | None]] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: object | None = None) -> None:
            calls.append((sql, params))

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="DeepSecret!Saved")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(
        service,
        "status",
        lambda: {
            "deepsec_enabled": True,
            "data_user": "DEEPSEC_DATA_USER",
            "has_data_user_password": True,
        },
    )

    status = service.sync_saved_data_user_password()

    executed_sql = "\n".join(sql for sql, _params in calls)
    assert status["has_data_user_password"] is True
    assert "SELECT COUNT(*) FROM DBA_END_USERS" in executed_sql
    assert "ALTER END USER IF EXISTS DEEPSEC_DATA_USER" in executed_sql
    assert 'IDENTIFIED BY "DeepSecret!Saved"' in executed_sql
    assert PASSWORD_PLACEHOLDER not in executed_sql
    assert closed == [True]


def test_sync_saved_config_password_treats_ora28007_as_success_when_saved_login_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    calls: list[str] = []
    login_probes: list[str] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            calls.append(sql)
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-28007: The password cannot be reused.")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="DeepSecret!Saved")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    monkeypatch.setattr(
        manager,
        "validate_data_user_login",
        lambda: login_probes.append(settings.oracle_deepsec_data_user_password),
    )
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(service, "status", lambda: {"has_data_user_password": True})

    status = service.sync_saved_data_user_password()

    assert status["has_data_user_password"] is True
    assert any("ALTER END USER" in sql for sql in calls)
    assert login_probes == ["DeepSecret!Saved"]
    assert closed == [True]


def test_sync_saved_config_password_keeps_409_when_ora28007_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-28007: password=DeepSecret!Saved cannot be reused")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="DeepSecret!Saved")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())

    def fail_login_probe() -> None:
        raise RuntimeError("ORA-01017: invalid credential password=DeepSecret!Saved")

    monkeypatch.setattr(manager, "validate_data_user_login", fail_login_probe)
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError, match="新しい DATA USER パスワード") as exc_info:
        service.sync_saved_data_user_password()

    message = str(exc_info.value)
    assert exc_info.value.status_code == 409
    assert "DeepSecret!Saved" not in message
    assert "[REDACTED]" in message
    assert closed == [True]


def test_sync_saved_config_password_requires_saved_password() -> None:
    settings = _settings(deepsec_enabled=True, data_user_password="")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    with pytest.raises(SecurityApiError, match="保存済み DATA USER パスワード") as exc_info:
        service.sync_saved_data_user_password()

    assert exc_info.value.status_code == 409


def test_sync_saved_config_password_creates_end_user_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    calls: list[str] = []
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            calls.append(sql)

        def fetchone(self) -> tuple[int]:
            return (0,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="DeepSecret!Saved")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(service, "status", lambda: {"has_data_user_password": True})

    status = service.sync_saved_data_user_password()

    assert status["has_data_user_password"] is True
    assert any("DBA_END_USERS" in sql for sql in calls)
    assert any("CREATE END USER IF NOT EXISTS DEEPSEC_DATA_USER" in sql for sql in calls)
    assert not any("ALTER END USER" in sql for sql in calls)
    assert 'IDENTIFIED BY "DeepSecret!Saved"' in "\n".join(calls)
    assert closed == [True]


def test_update_config_skips_password_sync_when_data_user_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NL2SQL_LOG_LEVEL=INFO\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: None)
    calls: list[str] = []

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            calls.append(sql)

        def fetchone(self) -> tuple[int]:
            return (0,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=False, data_user_password="")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(service, "status", lambda: {"has_data_user_password": True})

    service.update_config("DeepSecret!456")

    assert any("DBA_END_USERS" in sql for sql in calls)
    assert not any("ALTER END USER" in sql for sql in calls)
    assert settings.oracle_deepsec_data_user_password == "DeepSecret!456"


def test_update_config_rolls_back_when_password_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    original_env = (
        "PLATFORM_ORACLE_USER=APP_OWNER\n"
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=OldSecret!123\n"
    )
    env_file.write_text(original_env, encoding="utf-8")
    env_file.chmod(0o600)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-01031 password=DeepSecret!456")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="OldSecret!123")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError) as exc_info:
        service.update_config("DeepSecret!456")

    assert exc_info.value.status_code == 409
    assert "DeepSec DATA USER パスワードを Oracle END USER へ同期できませんでした" in str(
        exc_info.value
    )
    assert "DeepSecret!456" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    assert settings.oracle_deepsec_enabled is True
    assert settings.oracle_deepsec_data_user_password == "OldSecret!123"
    assert env_file.read_text(encoding="utf-8") == original_env
    assert closed == [True]


def test_update_config_keeps_password_when_ora28007_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PLATFORM_ORACLE_USER=APP_OWNER\n"
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=OldSecret!123\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    closed: list[bool] = []
    login_probes: list[str] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-28007: The password cannot be reused.")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="OldSecret!123")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())
    monkeypatch.setattr(
        manager,
        "validate_data_user_login",
        lambda: login_probes.append(settings.oracle_deepsec_data_user_password),
    )
    service = DeepSecService(settings, security, manager)
    monkeypatch.setattr(service, "status", lambda: {"has_data_user_password": True})

    status = service.update_config("DeepSecret!456")

    assert status["has_data_user_password"] is True
    assert settings.oracle_deepsec_data_user_password == "DeepSecret!456"
    assert "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=DeepSecret!456" in env_file.read_text(
        encoding="utf-8"
    )
    assert login_probes == ["DeepSecret!456"]
    assert closed == [True]


def test_update_config_rolls_back_when_ora28007_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    original_env = (
        "PLATFORM_ORACLE_USER=APP_OWNER\n"
        "NL2SQL_ORACLE_DEEPSEC_ENABLED=true\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER\n"
        "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=OldSecret!123\n"
    )
    env_file.write_text(original_env, encoding="utf-8")
    env_file.chmod(0o600)
    closed: list[bool] = []
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", env_file)
    monkeypatch.setattr("app.security.deepsec.close_oracle_pools", lambda: closed.append(True))

    class SyncCursor:
        def __enter__(self) -> SyncCursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object | None = None) -> None:
            if "ALTER END USER" in sql:
                raise RuntimeError("ORA-28007: password=DeepSecret!456 cannot be reused")

        def fetchone(self) -> tuple[int]:
            return (1,)

    class SyncConnection:
        def __enter__(self) -> SyncConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> SyncCursor:
            return SyncCursor()

    settings = _settings(deepsec_enabled=True, data_user_password="OldSecret!123")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    manager = OraclePoolManager(settings)
    monkeypatch.setattr(manager, "control_connection", lambda: SyncConnection())

    def fail_login_probe() -> None:
        raise RuntimeError("ORA-01017: invalid credential password=DeepSecret!456")

    monkeypatch.setattr(manager, "validate_data_user_login", fail_login_probe)
    service = DeepSecService(settings, security, manager)

    with pytest.raises(SecurityApiError, match="新しい DATA USER パスワード") as exc_info:
        service.update_config("DeepSecret!456")

    message = str(exc_info.value)
    assert exc_info.value.status_code == 409
    assert "DeepSecret!456" not in message
    assert "[REDACTED]" in message
    assert settings.oracle_deepsec_data_user_password == "OldSecret!123"
    assert env_file.read_text(encoding="utf-8") == original_env
    assert closed == [True]


@pytest.mark.parametrize(
    "password",
    ["short", "Invalid\nPass123", "Invalid\x7fPass123", 'Invalid"Pass123'],
)
def test_update_config_rejects_invalid_data_user_password(password: str) -> None:
    settings = _settings()
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    with pytest.raises(SecurityApiError, match="NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD"):
        service.update_config(password)


def test_update_config_rejects_thick_driver_before_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[bool] = []
    monkeypatch.setattr(
        "app.security.deepsec._write_deepsec_config_env",
        lambda _settings: persisted.append(True),
    )
    settings = _settings(driver_mode="thick", deepsec_enabled=False, data_user_password="")
    security = SecurityService(InMemorySecurityStore(), settings)
    security.bootstrap()
    service = DeepSecService(settings, security, OraclePoolManager(settings))

    with pytest.raises(SecurityApiError, match="Thin mode"):
        service.update_config("DeepSecret!456")

    assert persisted == []
    assert settings.oracle_deepsec_enabled is False
    assert settings.oracle_deepsec_data_user_password == ""


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, list[str]]], *, fail_clear: bool = False) -> None:
        self.calls = calls
        self.fail_clear = fail_clear

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def callproc(self, name: str, values: list[str] | None = None) -> None:
        self.calls.append((name, list(values or [])))
        if self.fail_clear and name.endswith("CLEAR_APP_USER"):
            raise RuntimeError("clear failed")


class _FakeConnection:
    def __init__(
        self,
        calls: list[tuple[str, list[str]]],
        *,
        fail_clear: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.calls = calls
        self.fail_clear = fail_clear
        self.fail_close = fail_close
        self.closed = 0
        self.call_timeout = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.calls, fail_clear=self.fail_clear)

    def close(self) -> None:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("DPY-1001: not connected to database")


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.dropped: list[_FakeConnection] = []
        self.closed_force_values: list[bool] = []

    def acquire(self) -> _FakeConnection:
        return self.connection

    def drop(self, connection: _FakeConnection) -> None:
        self.dropped.append(connection)

    def close(self, *, force: bool = False) -> None:
        self.closed_force_values.append(force)


class _FakeOracleDb:
    POOL_GETMODE_TIMEDWAIT = 3

    def __init__(self) -> None:
        self.thin_mode = True
        self.init_calls: list[str] = []
        self.pool_kwargs: list[dict[str, object]] = []

    def is_thin_mode(self) -> bool:
        return self.thin_mode

    def init_oracle_client(self, *, lib_dir: str) -> None:
        self.init_calls.append(lib_dir)
        self.thin_mode = False

    def create_pool(self, **kwargs: object) -> _FakePool:
        self.pool_kwargs.append(kwargs)
        return _FakePool(_FakeConnection([]))


def test_deepsec_configuration_accepts_thin() -> None:
    OraclePoolManager(_settings(driver_mode="thin")).validate_deepsec_configuration()


def test_deepsec_configuration_rejects_thick() -> None:
    manager = OraclePoolManager(_settings(driver_mode="thick"))

    with pytest.raises(OracleAdapterError, match="Thin mode"):
        manager.validate_deepsec_configuration()


def test_settings_validation_rejects_deepsec_thick_driver_mode() -> None:
    with pytest.raises(ValueError, match="PLATFORM_ORACLE_DRIVER_MODE=thin"):
        Settings(oracle_deepsec_enabled=True, oracle_driver_mode="thick")


def test_deepsec_configuration_requires_data_user_password() -> None:
    manager = OraclePoolManager(_settings(driver_mode="thin", data_user_password=""))

    with pytest.raises(OracleAdapterError, match="NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD"):
        manager.validate_deepsec_configuration()


def test_data_user_login_probe_uses_data_credentials_without_context_calls() -> None:
    calls: list[tuple[str, list[str]]] = []
    connection = _FakeConnection(calls)
    pool = _FakePool(connection)

    class ProbeOracleDb(_FakeOracleDb):
        def create_pool(self, **kwargs: object) -> _FakePool:
            self.pool_kwargs.append(kwargs)
            return pool

    manager = OraclePoolManager(_settings(driver_mode="thin", deepsec_enabled=False))
    fake_oracledb = ProbeOracleDb()
    manager._oracledb = fake_oracledb

    manager.validate_data_user_login()

    assert fake_oracledb.pool_kwargs == [
        {
            "user": "DEEPSEC_DATA_USER",
            "password": "DeepSecret!123",
            "dsn": "test",
            "tcp_connect_timeout": 5,
            "min": 1,
            "max": 1,
            "increment": 1,
            "session_callback": init_oracle_session,
        }
    ]
    assert calls == []
    assert connection.closed == 1
    assert pool.closed_force_values == [True]
    assert manager._data_pool is None


def test_data_pool_uses_thin_driver_and_data_user_credentials() -> None:
    manager = OraclePoolManager(_settings(driver_mode="thin"))
    fake_oracledb = _FakeOracleDb()
    manager._oracledb = fake_oracledb

    manager._get_pool(data_plane=True)

    assert fake_oracledb.init_calls == []
    assert fake_oracledb.pool_kwargs == [
        {
            "user": "DEEPSEC_DATA_USER",
            "password": "DeepSecret!123",
            "dsn": "test",
            "tcp_connect_timeout": 5,
            "min": 1,
            "max": 4,
            "increment": 1,
            "session_callback": init_oracle_session,
            "getmode": 3,
            "wait_timeout": 30_000,
        }
    ]


def test_data_pool_invalid_credentials_points_to_data_user_password_sync() -> None:
    class FailingOracleDb(_FakeOracleDb):
        def create_pool(self, **kwargs: object) -> _FakePool:
            self.pool_kwargs.append(kwargs)
            raise RuntimeError("ORA-01017 invalid credential password=DeepSecret!123")

    manager = OraclePoolManager(_settings(driver_mode="thin"))
    fake_oracledb = FailingOracleDb()
    manager._oracledb = fake_oracledb

    with pytest.raises(OracleAdapterError) as exc_info:
        manager._get_pool(data_plane=True)

    message = str(exc_info.value)
    assert "DeepSec DATA USER の Oracle ログインに失敗しました" in message
    assert "DATA USER パスワードを保存し直し" in message
    assert "DeepSecret!123" not in message
    assert fake_oracledb.pool_kwargs[0]["user"] == "DEEPSEC_DATA_USER"
    # 新しい接続ごとに result cache を無効にする（ADB の内部エラーを避ける。#333）。
    assert fake_oracledb.pool_kwargs[0]["session_callback"] is init_oracle_session


def test_deepsec_pool_rejects_thick_before_driver_initialization() -> None:
    manager = OraclePoolManager(_settings(driver_mode="thick"))
    fake_oracledb = _FakeOracleDb()
    manager._oracledb = fake_oracledb

    with pytest.raises(OracleAdapterError, match="Thin mode"):
        manager._get_pool(data_plane=True)

    assert fake_oracledb.init_calls == []
    assert fake_oracledb.pool_kwargs == []


def test_thick_control_pool_initializes_oracle_client_when_deepsec_disabled() -> None:
    manager = OraclePoolManager(_settings(driver_mode="thick", deepsec_enabled=False))
    fake_oracledb = _FakeOracleDb()
    manager._oracledb = fake_oracledb

    manager._get_pool(data_plane=False)

    assert fake_oracledb.init_calls == ["/opt/oracle/instantclient"]
    assert fake_oracledb.pool_kwargs == [
        {
            "user": "APP_OWNER",
            "password": "ControlPass!123",
            "dsn": "test",
            "tcp_connect_timeout": 5,
            "min": 1,
            "max": 4,
            "increment": 1,
            "session_callback": init_oracle_session,
            "getmode": 3,
            "wait_timeout": 30_000,
        }
    ]


def test_control_and_data_pools_share_wallet_mtls_network_settings(tmp_path: Path) -> None:
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    for file_name in ("tnsnames.ora", "sqlnet.ora", "cwallet.sso", "ewallet.pem"):
        (wallet_dir / file_name).write_text("dummy", encoding="utf-8")
    manager = OraclePoolManager(
        _settings(connection_security="wallet_mtls", wallet_dir=str(wallet_dir))
    )
    fake_oracledb = _FakeOracleDb()
    manager._oracledb = fake_oracledb

    manager._get_pool(data_plane=False)
    manager._get_pool(data_plane=True)

    assert fake_oracledb.pool_kwargs == [
        {
            "user": "APP_OWNER",
            "dsn": "test",
            "tcp_connect_timeout": 5,
            "password": "ControlPass!123",
            "config_dir": str(wallet_dir),
            "wallet_location": str(wallet_dir),
            "wallet_password": "ControlPass!123",
            "min": 1,
            "max": 4,
            "increment": 1,
            "session_callback": init_oracle_session,
            "getmode": 3,
            "wait_timeout": 30_000,
        },
        {
            "user": "DEEPSEC_DATA_USER",
            "dsn": "test",
            "tcp_connect_timeout": 5,
            "password": "DeepSecret!123",
            "config_dir": str(wallet_dir),
            "wallet_location": str(wallet_dir),
            "wallet_password": "ControlPass!123",
            "min": 1,
            "max": 4,
            "increment": 1,
            "session_callback": init_oracle_session,
            "getmode": 3,
            "wait_timeout": 30_000,
        },
    ]


def test_data_pool_sets_and_clears_each_actor_without_cross_user_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    connection = _FakeConnection(calls)
    pool = _FakePool(connection)
    manager = OraclePoolManager(_settings(call_timeout_seconds=7.5))
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: pool)

    with manager.data_connection("user-a"):
        pass
    with manager.data_connection("user-b"):
        pass

    assert calls == [
        ("NL2SQL_DEEPSEC_CTX_PKG.SET_APP_USER_UUID", ["user-a"]),
        ("NL2SQL_DEEPSEC_CTX_PKG.CLEAR_APP_USER", []),
        ("NL2SQL_DEEPSEC_CTX_PKG.SET_APP_USER_UUID", ["user-b"]),
        ("NL2SQL_DEEPSEC_CTX_PKG.CLEAR_APP_USER", []),
    ]
    assert connection.call_timeout == 7_500


def test_unscoped_data_connection_applies_call_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    connection = _FakeConnection(calls)
    pool = _FakePool(connection)
    manager = OraclePoolManager(_settings(call_timeout_seconds=2.25))
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: pool)

    with manager.unscoped_data_connection():
        assert connection.call_timeout == 2_250

    assert calls == [("NL2SQL_DEEPSEC_CTX_PKG.CLEAR_APP_USER", [])]


def test_context_clear_failure_drops_connection_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    connection = _FakeConnection(calls, fail_clear=True)
    pool = _FakePool(connection)
    manager = OraclePoolManager(_settings())
    manager._data_pool = pool
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: pool)

    with pytest.raises(Exception, match="context"), manager.data_connection("user-a"):
        pass
    assert pool.dropped == [connection]


def test_context_clear_failure_is_not_masked_by_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    connection = _FakeConnection(calls, fail_clear=True, fail_close=True)
    pool = _FakePool(connection)
    manager = OraclePoolManager(_settings())
    manager._data_pool = pool
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: pool)

    with (
        pytest.raises(OracleAdapterError, match="DeepSec context"),
        manager.data_connection("user-a"),
    ):
        pass

    assert pool.dropped == [connection]
    assert connection.closed == 1


def test_data_connection_close_failure_after_success_drops_without_failing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    connection = _FakeConnection(calls, fail_close=True)
    pool = _FakePool(connection)
    manager = OraclePoolManager(_settings())
    manager._data_pool = pool
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: pool)

    with manager.data_connection("user-a"):
        pass

    assert calls == [
        ("NL2SQL_DEEPSEC_CTX_PKG.SET_APP_USER_UUID", ["user-a"]),
        ("NL2SQL_DEEPSEC_CTX_PKG.CLEAR_APP_USER", []),
    ]
    assert pool.dropped == [connection]
    assert connection.closed == 1


@pytest.mark.parametrize(
    ("oracle_error", "expected_message"),
    [
        ("ORA-28000: The account is locked.", oracle_runtime.DEEPSEC_DATA_USER_LOCKED_MESSAGE),
        (
            "ORA-01017: invalid credential or not authorized; logon denied",
            oracle_runtime.DEEPSEC_DATA_USER_INVALID_CREDENTIAL_MESSAGE,
        ),
    ],
    ids=["account_locked", "invalid_credential"],
)
def test_data_user_login_failure_fails_fast_without_retrying_login(
    monkeypatch: pytest.MonkeyPatch, oracle_error: str, expected_message: str
) -> None:
    """DATA USER のログインの失敗は日本語の文で返し、期間内は要求ごとにログインし直さない。"""

    class LockedPool(_FakePool):
        def __init__(self) -> None:
            super().__init__(_FakeConnection([]))
            self.acquired = 0

        def acquire(self) -> _FakeConnection:
            self.acquired += 1
            raise RuntimeError(oracle_error)

    class LockedOracleDb(_FakeOracleDb):
        def __init__(self) -> None:
            super().__init__()
            self.pools: list[LockedPool] = []

        def create_pool(self, **kwargs: object) -> LockedPool:
            self.pool_kwargs.append(kwargs)
            pool = LockedPool()
            self.pools.append(pool)
            return pool

    clock = [1_000.0]
    monkeypatch.setattr(oracle_runtime, "_monotonic", lambda: clock[0])
    manager = OraclePoolManager(_settings())
    fake_oracledb = LockedOracleDb()
    manager._oracledb = fake_oracledb

    with pytest.raises(OracleAdapterError) as first, manager.data_connection("user-a"):
        pass
    assert str(first.value) == expected_message
    assert "DeepSecret" not in str(first.value)
    # 失敗した pool は捨てる（次は新しい pool で試す）。
    assert fake_oracledb.pools[0].closed_force_values == [True]
    assert manager._data_pool is None

    # 期間内の要求は、接続を試さずにすぐ同じ文で失敗する（ログインの失敗の回数を増やさない）。
    clock[0] += oracle_runtime.DATA_USER_LOGIN_FAILURE_BACKOFF_SECONDS - 1
    with (
        pytest.raises(OracleAdapterError, match="DeepSec DATA USER"),
        manager.data_connection("user-b"),
    ):
        pass
    assert len(fake_oracledb.pools) == 1
    assert fake_oracledb.pools[0].acquired == 1

    # 期間を過ぎたら 1 回だけ試し直す。
    clock[0] += 2
    with (
        pytest.raises(OracleAdapterError, match="DeepSec DATA USER"),
        manager.data_connection("user-a"),
    ):
        pass
    assert len(fake_oracledb.pools) == 2
    assert fake_oracledb.pools[1].acquired == 1

    # DeepSec の設定の保存（`close_oracle_pools` → `close`）で忘れる。
    manager.close()
    assert manager._data_login_failure is None


def test_data_connection_other_errors_are_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """ログイン以外の失敗（pool の待ちの timeout 等）は覚えず、次の要求で普通に試す。"""

    class BusyPool(_FakePool):
        def acquire(self) -> _FakeConnection:
            raise RuntimeError("DPY-4005: timed out waiting for the connection pool")

    manager = OraclePoolManager(_settings())
    monkeypatch.setattr(manager, "_get_pool", lambda *, data_plane: BusyPool(_FakeConnection([])))

    with pytest.raises(RuntimeError, match="DPY-4005"), manager.data_connection("user-a"):
        pass
    assert manager._data_login_failure is None
