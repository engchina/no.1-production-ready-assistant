"""実 Oracle Deep Data Security の opt-in integration suite。

通常の pytest/CI では実行しない。明示 opt-in 時だけ `backend/.env` の接続情報を読み、
DeepSec DDL/DML を実 DB に適用してから、finally でテスト前状態へ戻す。
資格情報、wallet password、DATA USER password、完全な DSN は出力・report しない。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.clients.oracle_runtime import close_oracle_pools
from app.security.deepsec import (
    DATA_GRANT_PREDICATE_MAX_LENGTH,
    DEEPSEC_APPLY_CONFIRMATION,
    DEEPSEC_DATA_ROLE,
    DEEPSEC_DB_ROLE,
    DEEPSEC_RESET_CONFIRMATION,
    PLAN_VERSION,
    build_data_entitlement_statements,
    build_v001_plan,
    get_deepsec_service,
)
from app.security.domain import (
    SYSTEM_ADMIN_ROLE_CODE,
    DataEntitlementRecord,
    Principal,
    RoleRecord,
)
from app.security.schemas import DataEntitlementInput
from app.security.service import SecurityApiError, reset_security_service
from app.settings import get_settings, reset_settings_cache

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_DEEPSEC_INTEGRATION") != "1"
    or os.getenv("NL2SQL_DEEPSEC_INTEGRATION_CONFIRM") != "I_UNDERSTAND_DEEPSEC_DB_MUTATION",
    reason=(
        "実 Oracle DeepSec integration は "
        "NL2SQL_RUN_DEEPSEC_INTEGRATION=1 と "
        "NL2SQL_DEEPSEC_INTEGRATION_CONFIRM=I_UNDERSTAND_DEEPSEC_DB_MUTATION "
        "の明示指定が必要です。"
    ),
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
BACKEND_ENV_FILE = BACKEND_DIR / ".env"
# 接続設定は3製品共通の設定（PLATFORM_*）なので共通 .env から読む（#211）。
# conftest が PLATFORM_ENV_FILE を差し替えるため、実環境の既定の場所を直接見る。
PLATFORM_ENV_FILE = BACKEND_DIR.parents[1] / "platform" / ".env"
REPORT_FILE = Path("/tmp/nl2sql-deepsec-integration-report.json")
_IDENTIFIER_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}")
_REQUIRED_SECURITY_TABLES = frozenset(
    {
        "PLATFORM_USERS",
        "PLATFORM_ROLES",
        "PLATFORM_USER_ROLES",
        "NL2SQL_APP_ROLE_PERMISSIONS",
        "NL2SQL_APP_ROLE_PROFILES",
        "NL2SQL_APP_DATA_ENTITLEMENTS",
        "PLATFORM_AUTH_SESSIONS",
        "NL2SQL_DEEPSEC_MIGRATIONS",
    }
)
_REQUIRED_ENTITLEMENT_COLUMNS = frozenset(
    {
        "TARGET_OWNER",
        "TARGET_OBJECT",
        "TARGET_TYPE",
        "COLUMN_NAMES",
        "SCOPE_MODE",
        "SCOPE_COLUMN",
        "SCOPE_FILTERS",
        "DATA_GRANT_NAME",
        "SQL_CHECKSUM",
        "APPLY_STATUS",
        "APPLY_ERROR_MESSAGE",
        "APPLIED_AT",
        "UPDATED_AT",
    }
)
_REPORT: list[dict[str, Any]] = []


@dataclass(slots=True)
class DeepSecSnapshot:
    configured: bool
    states: list[dict[str, Any]]
    entitlements: list[DataEntitlementRecord]
    applied_entitlement_ids: list[str]


@dataclass(slots=True)
class IterationReport:
    iteration: int
    run_id: str
    prefix: str
    objects: dict[str, str] = field(default_factory=dict)
    checks: list[dict[str, str]] = field(default_factory=list)
    cleanup: list[dict[str, str]] = field(default_factory=list)
    restored: list[dict[str, str]] = field(default_factory=list)

    def check(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": "passed", "detail": detail})

    def cleanup_step(self, name: str, status: str, detail: str = "") -> None:
        self.cleanup.append({"name": name, "status": status, "detail": detail})

    def restore_step(self, name: str, status: str, detail: str = "") -> None:
        self.restored.append({"name": name, "status": status, "detail": detail})

    def asdict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "run_id": self.run_id,
            "prefix": self.prefix,
            "objects": dict(self.objects),
            "checks": list(self.checks),
            "cleanup": list(self.cleanup),
            "restored": list(self.restored),
        }


def _reload_real_backend_env() -> None:
    for env_file in (PLATFORM_ENV_FILE, BACKEND_ENV_FILE):
        if not env_file.exists():
            pytest.fail(f".env が見つかりません: {env_file}")
        values = dotenv_values(env_file)
        for key, value in values.items():
            if value is not None:
                os.environ[str(key)] = str(value)
    reset_settings_cache()
    reset_security_service()
    close_oracle_pools()


def _service() -> Any:
    _reload_real_backend_env()
    return get_deepsec_service()


def _admin_principal() -> Principal:
    return Principal(
        user_uuid="deepsec-real-oracle-integration",
        login_user_id="deepsec.real.oracle.integration",
        display_name="DeepSec Real Oracle Integration",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE],
        permissions={"menu.security_roles", "menu.security_deepsec"},
        data_entitlements=[],
        allowed_profile_ids=set(),
        session_id="deepsec-real-oracle-integration-session",
        csrf_token_hash="deepsec-real-oracle-integration-csrf",
    )


def _ident(value: str) -> str:
    normalized = value.strip().strip('"').upper()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise AssertionError(f"unsafe Oracle identifier for test: {value}")
    return normalized


def _qualified(owner: str, name: str) -> str:
    return f"{_ident(owner)}.{_ident(name)}"


def _exec(cursor: Any, sql: str, params: dict[str, Any] | None = None) -> None:
    cursor.execute(sql, params or {})


def _query_one(cursor: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
    cursor.execute(sql, params or {})
    row = cursor.fetchone()
    return row[0] if row else None


def _fetch_dicts(
    cursor: Any, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    cursor.execute(sql, params or {})
    columns = [str(item[0]).lower() for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _preflight(real_service: Any, report: IterationReport) -> None:
    settings = get_settings()
    if settings.oracle_driver_mode.strip().lower() != "thin":
        pytest.fail(
            "PLATFORM_ORACLE_DRIVER_MODE=thin が必要です。DeepSec は Thin mode のみ対応です。"
        )
    if not settings.oracle_deepsec_enabled:
        pytest.fail("NL2SQL_ORACLE_DEEPSEC_ENABLED=true を設定してください。")
    if not settings.oracle_deepsec_data_user_password:
        pytest.fail("NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD を設定してください。")
    if settings.nl2sql_persistence_mode.strip().lower() != "oracle":
        pytest.fail("NL2SQL_PERSISTENCE_MODE=oracle が必要です。")

    real_service.pools.validate_deepsec_control_configuration()
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM DUAL")
        assert int(cursor.fetchone()[0]) == 1
        report.check("control connection", "SELECT 1")

        tables = set(
            _fetch_table_names(
                cursor,
                """
                SELECT TABLE_NAME FROM USER_TABLES
                 WHERE TABLE_NAME IN (
                   'PLATFORM_USERS',
                   'PLATFORM_ROLES',
                   'PLATFORM_USER_ROLES',
                   'NL2SQL_APP_ROLE_PERMISSIONS',
                   'NL2SQL_APP_DATA_ENTITLEMENTS',
                   'PLATFORM_AUTH_SESSIONS',
                   'NL2SQL_DEEPSEC_MIGRATIONS'
                 )
                """,
            )
        )
        missing_tables = sorted(_REQUIRED_SECURITY_TABLES - tables)
        if missing_tables:
            pytest.fail("security migration 未適用です。missing tables=" + ",".join(missing_tables))
        report.check("security tables", str(len(tables)))

        entitlement_columns = set(
            _fetch_table_names(
                cursor,
                """
                SELECT COLUMN_NAME
                  FROM USER_TAB_COLUMNS
                 WHERE TABLE_NAME = 'NL2SQL_APP_DATA_ENTITLEMENTS'
                """,
            )
        )
        missing_columns = sorted(_REQUIRED_ENTITLEMENT_COLUMNS - entitlement_columns)
        if missing_columns:
            pytest.fail(
                "migration 010 未適用です。先に "
                "`uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap` "
                "を実行してください。missing columns=" + ",".join(missing_columns)
            )
        cursor.execute("""
            SELECT DATA_LENGTH
              FROM USER_TAB_COLUMNS
             WHERE TABLE_NAME = 'NL2SQL_APP_DATA_ENTITLEMENTS'
               AND COLUMN_NAME = 'TARGET_TYPE'
            """)
        target_type_length = int(cursor.fetchone()[0])
        if target_type_length < len("MATERIALIZED VIEW"):
            pytest.fail(
                "migration 011 未適用です。"
                "NL2SQL_APP_DATA_ENTITLEMENTS.TARGET_TYPE は VARCHAR2(32) が必要です。"
            )
        report.check("migration 010 columns", str(len(_REQUIRED_ENTITLEMENT_COLUMNS)))


def _fetch_table_names(
    cursor: Any,
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[str]:
    cursor.execute(sql, params or {})
    return [str(row[0]).upper() for row in cursor.fetchall()]


def _snapshot(real_service: Any) -> DeepSecSnapshot:
    status = real_service.status()
    applied: list[str] = []
    entitlements: list[DataEntitlementRecord] = []
    for role in real_service.security.store.list_roles(include_archived=True):
        for entitlement in role.entitlements:
            entitlements.append(deepcopy(entitlement))
            if entitlement.apply_status == "APPLIED" and entitlement.target_owner:
                applied.append(entitlement.entitlement_id)
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        states = _fetch_dicts(
            cursor,
            """
            SELECT PLAN_VERSION, STEP_NO, STEP_KEY, CHECKSUM, STATUS,
                   NULLIF(ERROR_MESSAGE, '-') ERROR_MESSAGE, EXECUTED_BY_USER_UUID, EXECUTED_AT
              FROM NL2SQL_DEEPSEC_MIGRATIONS
             WHERE PLAN_VERSION = :version AND STEP_NO IN (1, 2, 3, 4)
             ORDER BY STEP_NO
            """,
            {"version": PLAN_VERSION},
        )
    return DeepSecSnapshot(
        configured=bool(status.get("configured")),
        states=states,
        entitlements=entitlements,
        applied_entitlement_ids=applied,
    )


def _apply_foundation(real_service: Any, actor: Principal, report: IterationReport) -> None:
    steps = build_v001_plan(get_settings())
    assert len(steps) == 2
    plan_payload = real_service.plan()
    assert plan_payload["data_user"] == get_settings().oracle_deepsec_data_user
    report.check("plan data user", str(plan_payload["data_user"]))

    for step in steps:
        real_service.apply_step(
            step.step_no,
            step.checksum,
            DEEPSEC_APPLY_CONFIRMATION,
            actor,
        )
        report.check(f"foundation step {step.step_no}", step.key)

    status = real_service.status()
    assert status["configured"] is True
    objects = status.get("objects")
    assert isinstance(objects, dict)
    for key in ("data_user", "data_role", "db_role", "context", "context_package"):
        assert int(objects[key]) > 0
    report.check("foundation status", "configured")


def _assert_foundation_db_objects(real_service: Any, report: IterationReport) -> None:
    settings = get_settings()
    owner = _ident(settings.oracle_user)
    data_user = _ident(settings.oracle_deepsec_data_user)
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        checks = {
            "data user": (
                "SELECT COUNT(*) FROM DBA_END_USERS WHERE USERNAME = :name",
                {"name": data_user},
            ),
            "data role": (
                "SELECT COUNT(*) FROM DBA_DATA_ROLES WHERE DATA_ROLE = :name",
                {"name": DEEPSEC_DATA_ROLE},
            ),
            "db role": (
                "SELECT COUNT(*) FROM DBA_ROLES WHERE ROLE = :name",
                {"name": DEEPSEC_DB_ROLE},
            ),
            "context package": (
                """
                SELECT COUNT(*) FROM ALL_OBJECTS
                 WHERE OWNER = :owner
                   AND OBJECT_NAME = 'NL2SQL_DEEPSEC_CTX_PKG'
                   AND OBJECT_TYPE = 'PACKAGE'
                   AND STATUS = 'VALID'
                """,
                {"owner": owner},
            ),
        }
        for name, (sql, params) in checks.items():
            assert int(_query_one(cursor, sql, params)) > 0
            report.check(f"db object {name}")

        assert (
            int(
                _query_one(
                    cursor,
                    """
                SELECT COUNT(*) FROM DBA_DATA_ROLE_GRANTS
                 WHERE DATA_ROLE = :db_role
                   AND ROLE_TYPE = 'DATABASE ROLE'
                   AND GRANTEE = :data_role
                   AND GRANTEE_TYPE = 'DATA ROLE'
                """,
                    {"data_role": DEEPSEC_DATA_ROLE, "db_role": DEEPSEC_DB_ROLE},
                )
            )
            > 0
        )
        assert (
            int(
                _query_one(
                    cursor,
                    """
                SELECT COUNT(*) FROM DBA_DATA_ROLE_GRANTS
                 WHERE DATA_ROLE = :data_role
                   AND ROLE_TYPE = 'DATA ROLE'
                   AND GRANTEE = :data_user
                   AND GRANTEE_TYPE = 'END USER'
                """,
                    {"data_user": data_user, "data_role": DEEPSEC_DATA_ROLE},
                )
            )
            > 0
        )
        report.check("data role grant chain")


def _exercise_config_update_without_real_env_write(
    real_service: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: IterationReport,
) -> None:
    temp_env = tmp_path / ".env.deepsec.integration"
    shutil.copyfile(BACKEND_ENV_FILE, temp_env)
    temp_env.chmod(0o600)
    monkeypatch.setattr("app.security.deepsec._BACKEND_ENV_FILE", temp_env)
    password = get_settings().oracle_deepsec_data_user_password
    result = real_service.update_config(password)
    assert result["data_user"] == get_settings().oracle_deepsec_data_user
    env_text = temp_env.read_text(encoding="utf-8")
    assert "NL2SQL_ORACLE_DEEPSEC_ENABLED=true" in env_text
    assert "NL2SQL_ORACLE_DEEPSEC_DATA_USER=" in env_text
    assert "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD=" in env_text
    assert BACKEND_ENV_FILE.read_text(encoding="utf-8") != ""
    report.check("update_config temp env only", str(temp_env))


def _create_test_objects(
    real_service: Any,
    prefix: str,
    report: IterationReport,
) -> dict[str, str]:
    owner = _ident(get_settings().oracle_user)
    table_name = _ident(f"{prefix}_T")
    view_source_name = _ident(f"{prefix}_VS")
    view_name = _ident(f"{prefix}_V")
    mview_source_name = _ident(f"{prefix}_MS")
    mview_name = _ident(f"{prefix}_MV")
    all_names = [mview_name, view_name, mview_source_name, view_source_name, table_name]
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        _drop_test_objects(cursor, owner, all_names)
        for source_name in (table_name, view_source_name, mview_source_name):
            _create_test_source_table(cursor, owner, source_name)
        _exec(
            cursor,
            f"""
            CREATE OR REPLACE VIEW {_qualified(owner, view_name)} AS
            SELECT ROW_ID, SCOPE_CODE, PUBLIC_TEXT, SECRET_TEXT
              FROM {_qualified(owner, view_source_name)}
            """,
        )
        _exec(
            cursor,
            f"""
            CREATE MATERIALIZED VIEW {_qualified(owner, mview_name)}
            BUILD IMMEDIATE
            REFRESH COMPLETE ON DEMAND
            AS
            SELECT ROW_ID, SCOPE_CODE, PUBLIC_TEXT, SECRET_TEXT
              FROM {_qualified(owner, mview_source_name)}
            """,
        )
        conn.commit()
    objects = {
        "owner": owner,
        "table": table_name,
        "view_source": view_source_name,
        "view": view_name,
        "materialized_view_source": mview_source_name,
        "materialized_view": mview_name,
    }
    report.objects.update(objects)
    report.check("test objects", json.dumps(objects, sort_keys=True))
    return objects


def _create_test_source_table(cursor: Any, owner: str, table_name: str) -> None:
    _exec(
        cursor,
        f"""
        CREATE TABLE {_qualified(owner, table_name)} (
          ROW_ID NUMBER PRIMARY KEY,
          SCOPE_CODE VARCHAR2(32) NOT NULL,
          PUBLIC_TEXT VARCHAR2(64) NOT NULL,
          SECRET_TEXT VARCHAR2(64) NOT NULL
        )
        """,
    )
    cursor.executemany(
        f"""
        INSERT INTO {_qualified(owner, table_name)}
          (ROW_ID, SCOPE_CODE, PUBLIC_TEXT, SECRET_TEXT)
        VALUES (:row_id, :scope_code, :public_text, :secret_text)
        """,
        [
            {
                "row_id": 1,
                "scope_code": "SALES",
                "public_text": "sales-public",
                "secret_text": "sales-secret",
            },
            {
                "row_id": 2,
                "scope_code": "HR",
                "public_text": "hr-public",
                "secret_text": "hr-secret",
            },
        ],
    )


def _drop_test_objects(cursor: Any, owner: str, names: list[str]) -> None:
    for object_type, ddl_type in (
        ("MATERIALIZED VIEW", "MATERIALIZED VIEW"),
        ("VIEW", "VIEW"),
        ("TABLE", "TABLE"),
    ):
        for name in names:
            cursor.execute(
                """
                SELECT COUNT(*) FROM ALL_OBJECTS
                 WHERE OWNER = :owner AND OBJECT_NAME = :name AND OBJECT_TYPE = :type
                """,
                {"owner": owner, "name": name, "type": object_type},
            )
            if int(cursor.fetchone()[0]) > 0:
                suffix = " CASCADE CONSTRAINTS PURGE" if object_type == "TABLE" else ""
                cursor.execute(f"DROP {ddl_type} {_qualified(owner, name)}{suffix}")


def _create_role_and_user(real_service: Any, prefix: str, actor: Principal) -> tuple[Any, Any]:
    role = real_service.security.create_role(
        role_code=f"{prefix}_R",
        display_name=f"DeepSec integration role {prefix}",
        description="Created by DeepSec real Oracle integration test.",
        permissions=set(),
        entitlements=[],
        actor=actor,
    )
    user, _password = real_service.security.create_user(
        login_user_id=f"{prefix.lower()}.user",
        display_name=f"DeepSec integration user {prefix}",
        role_ids=[role.role_id],
        temporary_password=f"DeepSec{prefix[-4:]}Start123!",
        actor=actor,
    )
    return role, user


def _policy_input(
    *,
    role_id: str,
    owner: str,
    target_object: str,
    target_type: str,
    columns: list[str],
    scope_mode: str,
    scope_code: str,
    scope_column: str,
) -> DataEntitlementRecord:
    return DataEntitlementInput(
        resource_code=f"{owner}.{target_object}",
        scope_code=scope_code,
        capability="SELECT",
        target_owner=owner,
        target_object=target_object,
        target_type=target_type,
        column_names=columns,
        scope_mode=scope_mode,
        scope_column=scope_column,
    ).to_record(role_id)


def _save_and_apply_entitlements(
    real_service: Any,
    role: RoleRecord,
    objects: dict[str, str],
    actor: Principal,
    report: IterationReport,
) -> RoleRecord:
    owner = objects["owner"]
    policies = [
        _policy_input(
            role_id=role.role_id,
            owner=owner,
            target_object=objects["table"],
            target_type="TABLE",
            columns=["ROW_ID", "SCOPE_CODE", "PUBLIC_TEXT"],
            scope_mode="COLUMN_EQUALS",
            scope_code="SALES",
            scope_column="SCOPE_CODE",
        ),
        _policy_input(
            role_id=role.role_id,
            owner=owner,
            target_object=objects["view"],
            target_type="VIEW",
            columns=["ROW_ID", "PUBLIC_TEXT"],
            scope_mode="ALL",
            scope_code="*",
            scope_column="",
        ),
        _policy_input(
            role_id=role.role_id,
            owner=owner,
            target_object=objects["materialized_view"],
            target_type="MATERIALIZED VIEW",
            columns=["ROW_ID", "PUBLIC_TEXT"],
            scope_mode="ALL",
            scope_code="*",
            scope_column="",
        ),
    ]
    saved = real_service.security.update_role_data_entitlements(
        role.role_id,
        expected_version=role.version,
        entitlements=policies,
        actor=actor,
    )
    assert [item.apply_status for item in saved.entitlements] == ["PENDING"] * 3
    report.check("save role policies", "PENDING table/view/materialized view")

    real_service.apply_data_entitlements(
        saved.role_id,
        expected_version=saved.version,
        confirmation=DEEPSEC_APPLY_CONFIRMATION,
        entitlements=saved.entitlements,
        actor=actor,
    )
    applied = real_service.security.store.get_role(saved.role_id)
    assert applied is not None
    assert [item.apply_status for item in applied.entitlements] == ["APPLIED"] * 3
    assert all(item.data_grant_name.startswith("NL2SQL_DG_") for item in applied.entitlements)
    report.check("apply role policies", "APPLIED table/view/materialized view")
    return applied  # type: ignore[no-any-return]


def _assert_data_grants(real_service: Any, role: RoleRecord, report: IterationReport) -> None:
    owner = _ident(get_settings().oracle_user)
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        for entitlement in role.entitlements:
            assert entitlement.data_grant_name
            rows = _fetch_dicts(
                cursor,
                """
                SELECT GRANT_NAME, OBJECT_OWNER, OBJECT_NAME, GRANTEE,
                       USE_DATA_GRANTS_ONLY
                  FROM DBA_DATA_GRANTS
                 WHERE OWNER = :owner AND GRANT_NAME = :grant_name
                """,
                {"owner": owner, "grant_name": entitlement.data_grant_name},
            )
            assert rows
            assert any(row["grantee"] == DEEPSEC_DATA_ROLE for row in rows)
            assert all(row["object_owner"] == entitlement.target_owner for row in rows)
            assert all(row["object_name"] == entitlement.target_object for row in rows)
            assert any(bool(row["use_data_grants_only"]) for row in rows)
            report.check(
                f"data grant {entitlement.target_type}",
                f"{entitlement.data_grant_name}->{entitlement.target_owner}."
                f"{entitlement.target_object}",
            )


def _assert_generated_sql_contract(role: RoleRecord, report: IterationReport) -> None:
    settings = get_settings()
    for entitlement in role.entitlements:
        statements = build_data_entitlement_statements(settings, entitlement)
        preview = "\n".join(statements)
        assert f"TO {DEEPSEC_DATA_ROLE}" in preview
        assert f"ON {entitlement.target_owner}.{entitlement.target_object}" in preview
        assert "CREATE TABLE NL2SQL_DEEPSEC_PROBE" not in preview
        predicate = preview.split("WHERE", 1)[-1].split(f"TO {DEEPSEC_DATA_ROLE}", 1)[0]
        assert len(predicate.strip()) <= DATA_GRANT_PREDICATE_MAX_LENGTH
    report.check("generated SQL contract", "real targets, data role grantee, 4000 predicate max")


def _assert_data_plane(
    real_service: Any,
    user_uuid: str,
    objects: dict[str, str],
    report: IterationReport,
) -> None:
    owner = objects["owner"]
    table = _qualified(owner, objects["table"])
    view = _qualified(owner, objects["view"])
    mview = _qualified(owner, objects["materialized_view"])

    with real_service.pools.data_connection(user_uuid) as conn, conn.cursor() as cursor:
        cursor.execute(f"SELECT ROW_ID, PUBLIC_TEXT, SECRET_TEXT FROM {table} ORDER BY ROW_ID")
        rows = cursor.fetchall()
        assert rows == [(1, "sales-public", None)]

        cursor.execute(f"SELECT ROW_ID, PUBLIC_TEXT FROM {view} ORDER BY ROW_ID")
        assert cursor.fetchall() == [(1, "sales-public"), (2, "hr-public")]

        cursor.execute(f"SELECT ROW_ID, PUBLIC_TEXT FROM {mview} ORDER BY ROW_ID")
        assert cursor.fetchall() == [(1, "sales-public"), (2, "hr-public")]
    report.check("data-plane authorized user", "row/column enforcement")

    with real_service.pools.unscoped_data_connection() as conn, conn.cursor() as cursor:
        cursor.execute(f"SELECT ROW_ID, PUBLIC_TEXT FROM {table} ORDER BY ROW_ID")
        assert cursor.fetchall() == []
    report.check("data-plane no app context", "0 rows")


def _verify_and_reset(
    real_service: Any,
    actor: Principal,
    report: IterationReport,
) -> None:
    verification = real_service.verify(actor)
    assert verification["passed"] is True
    report.check("verify", str(len(verification["checks"])))

    with pytest.raises(SecurityApiError):
        real_service.reset(PLAN_VERSION, "WRONG_CONFIRMATION", actor)
    assert real_service.status()["configured"] is True
    report.check("reset wrong confirmation", "rejected before reset")

    reset = real_service.reset(PLAN_VERSION, DEEPSEC_RESET_CONFIRMATION, actor)
    assert reset["status"] == "RESET"
    assert real_service.status()["configured"] is False
    report.check("reset correct confirmation", "foundation removed")


def _cleanup_test_data(real_service: Any, prefix: str, report: IterationReport) -> None:
    owner = _ident(get_settings().oracle_user)
    role_pattern = f"{prefix}_%"
    login_pattern = f"{prefix.lower()}.%"
    object_names = [
        _ident(f"{prefix}_MV"),
        _ident(f"{prefix}_V"),
        _ident(f"{prefix}_MS"),
        _ident(f"{prefix}_VS"),
        _ident(f"{prefix}_T"),
    ]
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        grant_names = _fetch_table_names(
            cursor,
            """
            SELECT DATA_GRANT_NAME
              FROM NL2SQL_APP_DATA_ENTITLEMENTS e
              JOIN PLATFORM_ROLES r ON r.ROLE_ID = e.ROLE_ID
             WHERE r.ROLE_CODE LIKE :role_pattern
               AND DATA_GRANT_NAME IS NOT NULL
            """,
            {"role_pattern": role_pattern},
        )
        for target in object_names:
            _disable_data_grants_only(cursor, owner, target)
        for grant_name in grant_names:
            cursor.execute(f"DROP DATA GRANT IF EXISTS {_qualified(owner, grant_name)}")
        _drop_test_objects(cursor, owner, object_names)
        cursor.execute(
            """
            DELETE FROM PLATFORM_AUTH_SESSIONS
             WHERE USER_UUID IN (
               SELECT USER_UUID FROM PLATFORM_USERS WHERE LOGIN_USER_ID LIKE :login_pattern
             )
            """,
            {"login_pattern": login_pattern},
        )
        cursor.execute(
            """
            DELETE FROM PLATFORM_USER_ROLES
             WHERE USER_UUID IN (
               SELECT USER_UUID FROM PLATFORM_USERS WHERE LOGIN_USER_ID LIKE :login_pattern
             )
            """,
            {"login_pattern": login_pattern},
        )
        cursor.execute(
            "DELETE FROM PLATFORM_USERS WHERE LOGIN_USER_ID LIKE :login_pattern",
            {"login_pattern": login_pattern},
        )
        cursor.execute(
            """
            DELETE FROM NL2SQL_APP_ROLE_PERMISSIONS
             WHERE ROLE_ID IN (
               SELECT ROLE_ID FROM PLATFORM_ROLES WHERE ROLE_CODE LIKE :role_pattern
             )
            """,
            {"role_pattern": role_pattern},
        )
        cursor.execute(
            """
            DELETE FROM NL2SQL_APP_DATA_ENTITLEMENTS
             WHERE ROLE_ID IN (
               SELECT ROLE_ID FROM PLATFORM_ROLES WHERE ROLE_CODE LIKE :role_pattern
             )
            """,
            {"role_pattern": role_pattern},
        )
        cursor.execute(
            "DELETE FROM PLATFORM_ROLES WHERE ROLE_CODE LIKE :role_pattern",
            {"role_pattern": role_pattern},
        )
        conn.commit()
    report.cleanup_step("test data", "done", prefix)


def _disable_data_grants_only(cursor: Any, owner: str, object_name: str) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) FROM ALL_OBJECTS
         WHERE OWNER = :owner
           AND OBJECT_NAME = :object_name
           AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')
        """,
        {"owner": owner, "object_name": object_name},
    )
    if int(cursor.fetchone()[0]) > 0:
        cursor.execute(f"SET USE DATA GRANTS ONLY ON {_qualified(owner, object_name)} DISABLED")


def _restore_snapshot(
    real_service: Any,
    snapshot: DeepSecSnapshot,
    actor: Principal,
    report: IterationReport,
) -> None:
    reset_security_service()
    close_oracle_pools()
    restored_service = get_deepsec_service()
    if snapshot.configured:
        try:
            _apply_foundation(restored_service, actor, report)
            report.restore_step("foundation", "reapplied")
        except Exception as exc:  # pragma: no cover - integration diagnostic only
            report.restore_step("foundation", "failed", _redact(str(exc)))
            raise
    else:
        report.restore_step("foundation", "left reset")

    if snapshot.configured and snapshot.applied_entitlement_ids:
        by_role: dict[str, list[str]] = {}
        for entitlement in snapshot.entitlements:
            if entitlement.entitlement_id in snapshot.applied_entitlement_ids:
                by_role.setdefault(entitlement.role_id, []).append(entitlement.entitlement_id)
        for role_id, entitlement_ids in by_role.items():
            role = restored_service.security.store.get_role(role_id)
            if role is None:
                continue
            restored_service.apply_data_entitlements(
                role_id,
                expected_version=role.version,
                confirmation=DEEPSEC_APPLY_CONFIRMATION,
                entitlements=[
                    item for item in role.entitlements if item.entitlement_id in entitlement_ids
                ],
                actor=actor,
            )
        report.restore_step("applied entitlements", "reapplied", str(len(by_role)))

    _restore_migration_states(restored_service, snapshot.states)
    _restore_entitlement_metadata(restored_service, snapshot.entitlements)
    report.restore_step("metadata snapshot", "restored")


def _restore_migration_states(real_service: Any, states: list[dict[str, Any]]) -> None:
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM NL2SQL_DEEPSEC_MIGRATIONS "
            "WHERE PLAN_VERSION = :version AND STEP_NO IN (1, 2, 3, 4)",
            {"version": PLAN_VERSION},
        )
        for state in states:
            cursor.execute(
                """
                INSERT INTO NL2SQL_DEEPSEC_MIGRATIONS
                  (PLAN_VERSION, STEP_NO, STEP_KEY, CHECKSUM, STATUS, ERROR_MESSAGE,
                   EXECUTED_BY_USER_UUID, EXECUTED_AT)
                VALUES
                  (:plan_version, :step_no, :step_key, :checksum, :status,
                   :error_message, :executed_by_user_uuid, :executed_at)
                """,
                {
                    "plan_version": state["plan_version"],
                    "step_no": state["step_no"],
                    "step_key": state["step_key"],
                    "checksum": state["checksum"],
                    "status": state["status"],
                    "error_message": state["error_message"] or "-",
                    "executed_by_user_uuid": state["executed_by_user_uuid"],
                    "executed_at": state["executed_at"],
                },
            )
        conn.commit()


def _restore_entitlement_metadata(
    real_service: Any,
    entitlements: list[DataEntitlementRecord],
) -> None:
    with real_service.pools.control_connection() as conn, conn.cursor() as cursor:
        for entitlement in entitlements:
            cursor.execute(
                """
                UPDATE NL2SQL_APP_DATA_ENTITLEMENTS
                   SET DATA_GRANT_NAME = :data_grant_name,
                       SQL_CHECKSUM = :sql_checksum,
                       APPLY_STATUS = :apply_status,
                       APPLY_ERROR_MESSAGE = :apply_error_message,
                       APPLIED_AT = :applied_at,
                       UPDATED_AT = SYSTIMESTAMP
                 WHERE ENTITLEMENT_ID = :entitlement_id
                """,
                {
                    "entitlement_id": entitlement.entitlement_id,
                    "data_grant_name": entitlement.data_grant_name or None,
                    "sql_checksum": entitlement.sql_checksum or None,
                    "apply_status": entitlement.apply_status,
                    "apply_error_message": entitlement.apply_error_message or "-",
                    "applied_at": entitlement.applied_at,
                },
            )
        conn.commit()


def _redact(value: str) -> str:
    settings = get_settings()
    redacted = value
    for secret in (
        settings.oracle_password,
        settings.oracle_wallet_password,
        settings.oracle_deepsec_data_user_password,
        settings.oracle_dsn,
    ):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[:1000]


def _write_report() -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "oracle_docs": [
            "https://docs.oracle.com/en/database/oracle/oracle-database/26/ddscg/data-grants.html",
            "https://docs.oracle.com/en/database/oracle/oracle-database/26/ddscg/create-data-grants.html",
            "https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/set-use-data-grants-only.html",
            "https://docs.oracle.com/en/database/oracle/oracle-database/26/ddscg/application-registrations-users-and-roles.html",
        ],
        "runs": _REPORT,
    }
    REPORT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


@pytest.mark.parametrize("iteration", [1, 2, 3])
def test_deepsec_real_oracle_full_flow(
    iteration: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4().hex[:8].upper()
    prefix = _ident(f"CX_DDS_{iteration}_{run_id[:4]}")
    report = IterationReport(iteration=iteration, run_id=run_id, prefix=prefix)
    actor = _admin_principal()
    reset_done = False
    preflight_ok = False
    snapshot: DeepSecSnapshot | None = None

    real_service = _service()
    try:
        _preflight(real_service, report)
        preflight_ok = True
        snapshot = _snapshot(real_service)
        _apply_foundation(real_service, actor, report)
        _assert_foundation_db_objects(real_service, report)
        _exercise_config_update_without_real_env_write(
            real_service,
            tmp_path,
            monkeypatch,
            report,
        )

        objects = _create_test_objects(real_service, prefix, report)
        role, user = _create_role_and_user(real_service, prefix, actor)
        role = _save_and_apply_entitlements(real_service, role, objects, actor, report)
        _assert_generated_sql_contract(role, report)
        _assert_data_grants(real_service, role, report)
        _assert_data_plane(real_service, user.user_uuid, objects, report)
        _verify_and_reset(real_service, actor, report)
        reset_done = True
    finally:
        try:
            if preflight_ok:
                try:
                    _cleanup_test_data(real_service, prefix, report)
                finally:
                    if snapshot is not None and (reset_done or snapshot.configured):
                        _restore_snapshot(real_service, snapshot, actor, report)
            elif snapshot is not None and (reset_done or snapshot.configured):
                _restore_snapshot(real_service, snapshot, actor, report)
        finally:
            _REPORT.append(report.asdict())
            _write_report()


def test_deepsec_expression_only_isolated_objects() -> None:
    """現行基盤を再構成せず、専用 table/grant のみ作成して実述語を照合する。"""
    import oracledb

    from app.security.deepsec import DEEPSEC_DATA_ROLE, DEEPSEC_DB_ROLE
    from app.security.scope_expression import compile_expression

    service = _service()
    owner = _ident(get_settings().oracle_user)
    suffix = uuid.uuid4().hex[:8].upper()
    employee = f"CX_DSE_{suffix}_E"
    department = f"CX_DSE_{suffix}_D"
    storage = f"CX_DSE_{suffix}_S"
    grant = f"CX_DSE_{suffix}_G"
    parent_grant = f"CX_DSE_{suffix}_P"
    check_name = f"CX_DSE_{suffix}_CK"
    created: list[str] = []
    granted: list[str] = []

    def condition(column: str, value: str) -> dict[str, Any]:
        return {
            "kind": "condition",
            "filter": {
                "column_name": column,
                "operator": "EQ",
                "value_type": "TEXT",
                "value": value,
            },
        }

    root = {
        "kind": "group",
        "operator": "OR",
        "children": [
            {
                "kind": "group",
                "operator": "AND",
                "children": [
                    condition("STATUS", "ACTIVE"),
                    {
                        "kind": "related_exists",
                        "profile_id": "isolated-test",
                        "object_scope_version": 1,
                        "target_owner": owner,
                        "target_object": department,
                        "join_keys": [
                            {"source_column": "DEPT", "target_column": "ID"},
                            {"source_column": "TENANT", "target_column": "TENANT"},
                        ],
                        "condition": {
                            "kind": "group",
                            "operator": "AND",
                            "children": [
                                condition("LOCATION", "TOKYO"),
                                condition("ENABLED", "YES"),
                            ],
                        },
                    },
                ],
            },
            condition("ID", "3"),
        ],
    }
    entitlement = DataEntitlementInput.model_validate(
        {
            "capability": "SELECT",
            "resource_code": f"{owner}.{employee}",
            "target_owner": owner,
            "target_object": employee,
            "column_names": ["ID"],
            "scope_mode": "EXPRESSION",
            "scope_expression": {"version": 1, "root": root},
        }
    ).to_record("isolated")
    try:
        with service.pools.control_connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE {employee}(ID VARCHAR2(8), STATUS VARCHAR2(20), "
                "DEPT VARCHAR2(8), TENANT VARCHAR2(8))"
            )
            created.append(employee)
            cursor.execute(
                f"CREATE TABLE {department}(ID VARCHAR2(8), LOCATION VARCHAR2(20), "
                "ENABLED VARCHAR2(8), TENANT VARCHAR2(8))"
            )
            created.append(department)
            cursor.executemany(
                f"INSERT INTO {employee} VALUES (:1,:2,:3,:4)",
                [
                    ("1", "ACTIVE", "D1", "T"),
                    ("2", "ACTIVE", "D2", "T"),
                    ("3", "INACTIVE", "D3", "T"),
                    ("4", "ACTIVE", None, "T"),
                    ("5", "ACTIVE", "D1", "OTHER"),
                ],
            )
            cursor.executemany(
                f"INSERT INTO {department} VALUES (:1,:2,:3,:4)",
                [
                    ("D1", "TOKYO", "YES", "T"),
                    ("D1", "TOKYO", "YES", "T"),
                    ("D2", "TOKYO", "NO", "T"),
                    ("D2", "OSAKA", "YES", "T"),
                    ("D3", "OSAKA", "YES", "T"),
                ],
            )
            conn.commit()
            for obj in (employee, department):
                cursor.execute(f"GRANT SELECT ON {obj} TO {DEEPSEC_DB_ROLE}")
                cursor.execute(f"SET USE DATA GRANTS ONLY ON {obj} ENABLED")
            predicate = compile_expression(entitlement, {})
            cursor.execute(
                f"CREATE DATA GRANT {grant} AS SELECT (ID) ON {owner}.{employee} "
                f"WHERE {predicate} TO {DEEPSEC_DATA_ROLE}"
            )
            granted.append(grant)
            # 関連表も DeepSec 保護し、条件参照と利用者の直接参照の違いを固定する。
            cursor.execute(
                f"CREATE DATA GRANT {parent_grant} AS SELECT ON {owner}.{department} "
                f"WHERE ID = 'D3' TO {DEEPSEC_DATA_ROLE}"
            )
            granted.append(parent_grant)
        with service.pools.unscoped_data_connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT ID FROM {owner}.{employee} ORDER BY ID")
            assert cursor.fetchall() == [("3",)]
            cursor.execute(f"SELECT ID FROM {owner}.{department}")
            assert cursor.fetchall() == [("D3",)]
        with service.pools.control_connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"CREATE OR REPLACE DATA GRANT {parent_grant} AS SELECT "
                f"ON {owner}.{department} WHERE ID IN ('D1','D3') TO {DEEPSEC_DATA_ROLE}"
            )
        with service.pools.unscoped_data_connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT ID FROM {owner}.{employee} ORDER BY ID")
            assert cursor.fetchall() == [("1",), ("3",)]
        with service.pools.control_connection() as conn, conn.cursor() as cursor:
            from app.security.scope_relations import DependencyPlan, validate_relation_dependency

            # E→D を同じバッチで E の固定条件 + D→E に変更する予定を検証する。
            # 旧 grant を含む現在状態では循環するが、置換後の状態では循環しない。
            with pytest.raises(SecurityApiError, match="循環"):
                validate_relation_dependency(cursor, f"{owner}.{department}", f"{owner}.{employee}")
            dependency_plan = DependencyPlan(
                replaced_grants=frozenset({(owner, grant), (owner, parent_grant)}),
                edges={f"{owner}.{department}": {f"{owner}.{employee}"}},
            )
            validate_relation_dependency(
                cursor, f"{owner}.{department}", f"{owner}.{employee}", dependency_plan
            )
            cursor.execute(
                f"CREATE OR REPLACE DATA GRANT {grant} AS SELECT (ID, DEPT) "
                f"ON {owner}.{employee} WHERE ID = '3' TO {DEEPSEC_DATA_ROLE}"
            )
            cursor.execute(
                f"CREATE OR REPLACE DATA GRANT {parent_grant} AS SELECT ON {owner}.{department} "
                f"WHERE EXISTS (SELECT 1 FROM {owner}.{employee} E "
                f"WHERE E.DEPT = {department}.ID) TO {DEEPSEC_DATA_ROLE}"
            )
            validate_relation_dependency(cursor, f"{owner}.{department}", f"{owner}.{employee}")
        with service.pools.unscoped_data_connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT ID FROM {owner}.{department}")
            assert cursor.fetchall() == [("D3",)]
        with service.pools.control_connection() as conn, conn.cursor() as cursor:
            # migration 004 と同じ SCOPE_EXPRESSION（CLOB）と mode 制約の表を、圧縮表として
            # 隔離して作り、4KB 超の JSON を roundtrip する（旧形からの ALTER は #1648 で削除）。
            cursor.execute(
                f"CREATE TABLE {storage}(SCOPE_MODE VARCHAR2(32), SCOPE_EXPRESSION CLOB, "
                f"CONSTRAINT {check_name} "
                "CHECK (SCOPE_MODE IN ('ALL','COLUMN_EQUALS','FILTERS','EXPRESSION'))) "
                "COLUMN STORE COMPRESS FOR QUERY HIGH ROW LEVEL LOCKING"
            )
            created.append(storage)
            cursor.setinputsizes(payload=oracledb.DB_TYPE_CLOB)
            payload = json.dumps({"root": root, "padding": "x" * 5000}, ensure_ascii=False)
            cursor.execute(
                f"INSERT INTO {storage}(SCOPE_MODE,SCOPE_EXPRESSION) "
                "VALUES ('EXPRESSION',:payload)",
                {"payload": payload},
            )
            cursor.execute(f"SELECT SCOPE_EXPRESSION FROM {storage}")
            raw = cursor.fetchone()[0]
            assert (raw.read() if hasattr(raw, "read") else raw) == payload
            cursor.execute(
                f"CREATE OR REPLACE DATA GRANT {grant} AS SELECT (ID) ON {owner}.{employee} "
                f"WHERE ID IN (SELECT ID FROM {owner}.{employee}) TO {DEEPSEC_DATA_ROLE}"
            )
        with (
            service.pools.unscoped_data_connection() as conn,
            conn.cursor() as cursor,
            pytest.raises(oracledb.DatabaseError, match="ORA-52561"),
        ):
            cursor.execute(f"SELECT ID FROM {owner}.{employee}")
            cursor.fetchall()
    finally:
        with service.pools.control_connection() as conn, conn.cursor() as cursor:
            for name in reversed(granted):
                cursor.execute(f"DROP DATA GRANT IF EXISTS {owner}.{name}")
            for name in reversed(created):
                cursor.execute(f"DROP TABLE {owner}.{name} PURGE")
        close_oracle_pools()
