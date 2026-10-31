"""システムテーブルの管理の骨格（`pr_system_settings.system_schema`。#325）の決定論テスト。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from pr_system_settings.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    RECREATE_CONFIRMATION_REQUIRED,
    ForeignKeySpec,
    SystemSchemaActiveJobsError,
    SystemSchemaBusyError,
    SystemSchemaError,
    SystemSchemaManagerBase,
    SystemSchemaPreconditionError,
    SystemTableForeignKeyData,
    SystemTableOperationState,
    SystemTablesDeleteOrphansRequest,
    SystemTablesInitializeRequest,
    add_foreign_key_sql,
    bind_list,
    clamp_ddl_lock_timeout,
    classify_system_schema_status,
    columns_from_create_table,
    columns_from_statements,
    delete_orphan_rows_sql,
    deployment_check_error_summary,
    deployment_check_summary,
    drop_foreign_key_sql,
    enable_foreign_key_sql,
    foreign_keys_from_create_table,
    idle_operation_state,
    inspect_foreign_keys,
    iso_timestamp,
    load_existing_columns,
    missing_columns,
    operation_failure_fields,
    operation_failure_hint,
    oracle_error_code,
    orphan_rows_sql,
    require_destructive_migration_confirmation,
    require_recreate_confirmation,
    system_tables_status_error,
    validate_foreign_key_sql,
)

DOMAIN = ("DEMO_ITEMS", "DEMO_LOGS")
MANAGED = (
    ("DEMO_SCHEMA_OPERATIONS", "TABLE"),
    ("DEMO_ITEMS", "TABLE"),
    ("DEMO_LOGS", "TABLE"),
    ("DEMO_ITEMS_IDX", "INDEX"),
)


# ---- 状態の分類・確認語・安全化 -----------------------------------------------


def _classify(objects: set[tuple[str, str]], *, current: bool = True) -> str:
    return classify_system_schema_status(
        objects,
        domain_tables=DOMAIN,
        managed_objects=MANAGED,
        retired_objects=(("DEMO_OLD_IDX", "INDEX"),),
        migrations_current=current,
    )


def test_status_is_classified_into_four_states() -> None:
    assert _classify(set()) == "missing"
    # 制御テーブルだけでは業務テーブルが無いので missing。
    assert _classify({("DEMO_SCHEMA_OPERATIONS", "TABLE")}) == "missing"
    assert _classify({("DEMO_ITEMS", "TABLE")}) == "partial"
    assert _classify(set(MANAGED), current=False) == "outdated"
    assert _classify(set(MANAGED) | {("DEMO_OLD_IDX", "INDEX")}) == "outdated"
    assert _classify(set(MANAGED)) == "ready"


def test_recreate_confirmation_requires_exact_match() -> None:
    require_recreate_confirmation(recreate=False, confirmation=None, expected="RECREATE_DEMO")
    require_recreate_confirmation(
        recreate=True, confirmation="RECREATE_DEMO", expected="RECREATE_DEMO"
    )
    for wrong in (None, "", "recreate_demo", " RECREATE_DEMO"):
        with pytest.raises(SystemSchemaError) as error:
            require_recreate_confirmation(
                recreate=True, confirmation=wrong, expected="RECREATE_DEMO"
            )
        assert error.value.code == RECREATE_CONFIRMATION_REQUIRED
        assert error.value.status_code == 422


def test_errors_expose_only_ora_codes() -> None:
    assert oracle_error_code(RuntimeError("password=secret ora-00942: missing")) == "ORA-00942"
    assert oracle_error_code(RuntimeError("password=secret")) == "SCHEMA_OPERATION_FAILED"

    unavailable = system_tables_status_error(RuntimeError("dsn=secret ORA-12514"))
    assert unavailable.status_code == 503
    assert unavailable.detail == "システムテーブルの状態を取得できませんでした (ORA-12514)。"
    hidden = system_tables_status_error(RuntimeError("dsn=secret"))
    assert "secret" not in str(hidden.detail)
    assert "SCHEMA_STATUS_UNAVAILABLE" in str(hidden.detail)


def test_only_lock_timeout_is_retryable() -> None:
    locked = SystemSchemaError("ORA-00054", "locked", status_code=409)
    assert locked.retryable is True
    assert locked.retry_headers == {"Retry-After": "5"}
    other = SystemSchemaError("ORA-00600", "failed")
    assert other.retryable is False
    assert other.retry_headers is None
    jobs = SystemSchemaActiveJobsError("実行中の job があります。")
    assert (jobs.code, jobs.status_code, jobs.public_message) == (
        "SCHEMA_JOBS_RUNNING",
        409,
        "実行中の job があります。",
    )


def test_small_helpers() -> None:
    assert bind_list("name_", ["A", "B"]) == (":name_0, :name_1", {"name_0": "A", "name_1": "B"})
    assert iso_timestamp(None) is None
    assert iso_timestamp(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05+00:00"
    assert clamp_ddl_lock_timeout(-1) == 0
    assert clamp_ddl_lock_timeout(999) == 120
    assert idle_operation_state()["status"] == "idle"


def test_deployment_check_summary_is_ok_only_when_ready_and_idle() -> None:
    """配備の確認（#1648）: ready で操作が実行中でないときだけ OK。NG は原因を 1 行にまとめる。"""

    assert deployment_check_summary(
        {"status": "ready", "operation_state": idle_operation_state()}
    ) == (
        True,
        "status=ready",
    )
    ok, summary = deployment_check_summary(
        {"status": "ready", "operation_state": {"status": "running"}}
    )
    assert not ok
    assert "操作 running" in summary

    # 権限の不足で作れなかった: 前回の操作の失敗の ORA コードを出す（missing は不足の一覧を
    # 出さない）。
    ok, summary = deployment_check_summary(
        {
            "status": "missing",
            "missing_objects": [{"name": "DEMO_ITEMS"}],
            "pending_versions": ["001"],
            "operation_state": {"status": "failed", "last_error_code": "ORA-01031"},
        }
    )
    assert not ok
    assert summary == "status=missing・操作 failed・前回の操作の失敗 ORA-01031"

    names = [{"name": f"DEMO_{index}"} for index in range(7)]
    ok, summary = deployment_check_summary(
        {
            "status": "partial",
            "missing_objects": names,
            "pending_versions": ["001", "002"],
            "pending_destructive_migrations": [{"name": "003_drop", "description": "x"}],
            "operation_state": idle_operation_state(),
        }
    )
    assert not ok
    assert "不足 7 件（DEMO_0, DEMO_1, DEMO_2, DEMO_3, DEMO_4, …）" in summary
    assert "未適用の migration 2 件" in summary
    assert "承認が要る削除 1 件（003_drop。書き出してから --allow-destructive で初期化）" in summary

    # 台帳が最新でも列が欠けている（#2450）。
    ok, summary = deployment_check_summary(
        {
            "status": "outdated",
            "missing_columns": [{"table_name": "DEMO_ITEMS", "column_name": "PAYLOAD"}],
            "operation_state": idle_operation_state(),
        }
    )
    assert not ok
    assert summary == "status=outdated・不足の列 1 件（DEMO_ITEMS.PAYLOAD）"


def test_deployment_check_error_summary_exposes_only_codes_or_safe_hint() -> None:
    # DB に接続できない失敗は、コードと理由・対処を出す（#1744）。例外の文（secret）は出さない。
    summary = deployment_check_error_summary(
        RuntimeError("ORA-01017: invalid username/password; secret")
    )
    assert summary.startswith(
        "状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ORA-01017: DB のユーザー名か password が"
    )
    assert "secret" not in summary
    assert (
        deployment_check_error_summary(RuntimeError("ORA-01031: insufficient privileges; secret"))
        == "状態を読めません（ORA-01031）"
    )
    # ORA でない失敗は手がかり（型と message の 1 行目）を出し、代入の値は伏せる（#1701）。
    assert deployment_check_error_summary(ValueError("password=secret")) == (
        "状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ValueError: password=***）"
    )
    assert (
        deployment_check_error_summary(SystemSchemaError("SCHEMA_X", "message"))
        == "状態を読めません（SCHEMA_X）"
    )


def test_api_models_keep_the_contract() -> None:
    state = SystemTableOperationState.model_validate(idle_operation_state())
    assert state.schema_epoch == 0
    assert SystemTablesInitializeRequest().model_dump() == {
        "recreate": False,
        "confirmation": None,
        "allow_destructive": False,
    }
    with pytest.raises(ValueError):
        SystemTablesInitializeRequest(recreate=True, confirmation="X" * 129)


# ---- lease・台帳・初期化の手順（fake の Oracle） --------------------------------


class _FakeDatabase:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], datetime | None] = {}
        self.migrations: dict[str, str] = {}
        self.operation: dict[str, Any] = {
            "status": "IDLE",
            "operation_kind": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error_code": None,
            "schema_epoch": 0,
            "updated_at": datetime.now(UTC),
        }
        self.statements: list[str] = []

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield _FakeConnection(self)


class _FakeConnection:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.database)

    def commit(self) -> None:
        return None


class _FakeCursor:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self.rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> None:
        sql = re.sub(r"\s+", " ", statement).strip().upper()
        params = params or {}
        self.database.statements.append(sql)
        self.rows = []
        self.rowcount = 0
        operation = self.database.operation
        now = datetime.now(UTC)
        if sql.startswith("SELECT STATUS, OPERATION_KIND"):
            self.rows = [
                (
                    operation["status"],
                    operation["operation_kind"],
                    operation["lease_expires_at"],
                    operation["last_error_code"],
                    operation["schema_epoch"],
                    operation["updated_at"],
                )
            ]
        elif sql.startswith("SELECT MIGRATION_NAME, CHECKSUM FROM DEMO_SCHEMA_MIGRATIONS"):
            self.rows = sorted(self.database.migrations.items())
        elif sql.startswith("SELECT TABLE_NAME, NUM_ROWS, LAST_ANALYZED FROM USER_TABLES"):
            self.rows = [(str(value), 12, None) for value in params.values()]
        elif sql.startswith("MERGE INTO DEMO_SCHEMA_MIGRATIONS"):
            self.database.migrations[str(params["migration_name"])] = str(params["checksum"])
        elif sql.startswith("DROP "):
            name = sql.split()[2]
            if not any(key[0] == name for key in self.database.objects):
                raise RuntimeError("ORA-00942: table or view does not exist")
            self.database.objects = {
                key: value for key, value in self.database.objects.items() if key[0] != name
            }
        elif "SET STATUS = 'RUNNING'" in sql:
            expires = operation["lease_expires_at"]
            if operation["status"] == "RUNNING" and expires is not None and expires >= now:
                return
            operation.update(
                status="RUNNING",
                operation_kind=params["operation_kind"],
                lease_owner=params["lease_owner"],
                lease_expires_at=now + timedelta(seconds=int(params["lease_seconds"])),
                last_error_code=None,
            )
            self.rowcount = 1
        elif sql.startswith("UPDATE DEMO_SCHEMA_OPERATIONS"):
            if operation["lease_owner"] != params.get("lease_owner"):
                return
            if "SET STATUS = 'IDLE'" in sql:
                if "SCHEMA_EPOCH = SCHEMA_EPOCH + 1" in sql:
                    operation["schema_epoch"] += 1
                operation.update(
                    status="IDLE", operation_kind=None, lease_owner=None, lease_expires_at=None
                )
            elif "SET STATUS = 'FAILED'" in sql:
                operation.update(
                    status="FAILED",
                    operation_kind=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error_code=params["error_code"],
                )
            elif operation["status"] == "RUNNING":
                operation["lease_expires_at"] = now + timedelta(
                    seconds=int(params["lease_seconds"])
                )
            else:
                return
            self.rowcount = 1


class _DemoManager(SystemSchemaManagerBase):
    control_table = "DEMO_SCHEMA_OPERATIONS"
    migration_table = "DEMO_SCHEMA_MIGRATIONS"
    migration_key_column = "MIGRATION_NAME"
    managed_tables = ("DEMO_SCHEMA_OPERATIONS", "DEMO_SCHEMA_MIGRATIONS", "DEMO_ITEMS")
    recreate_confirmation = "RECREATE_DEMO_SYSTEM_TABLES"
    log_prefix = "demo"
    lock_timeout_guidance = "デモの job を止めてから、"
    guards_destructive_migrations = True

    def __init__(self, database: _FakeDatabase, *, fail_with: str | None = None) -> None:
        super().__init__(database.connection, lease_seconds=1, ddl_lock_timeout_seconds=7)
        self.database = database
        self.fail_with = fail_with
        self.control_schema_calls = 0
        # 製品が状態に出す、データを消す未適用の migration（#619）。
        self.pending_destructive: list[dict[str, str]] = []

    def _ensure_control_schema(self) -> None:
        self.control_schema_calls += 1
        self.database.objects[("DEMO_SCHEMA_OPERATIONS", "TABLE")] = datetime.now(UTC)
        self.database.objects[("DEMO_SCHEMA_MIGRATIONS", "TABLE")] = datetime.now(UTC)

    def _status_on(self, connection: Any) -> dict[str, Any]:
        objects = dict(self.database.objects)
        applied = self._load_migrations(connection, objects)
        return {
            "status": classify_system_schema_status(
                set(objects),
                domain_tables=("DEMO_ITEMS",),
                managed_objects=(("DEMO_ITEMS", "TABLE"),),
                migrations_current=applied.get("001") == "sum-1",
            ),
            "existing_object_count": len(objects),
            "tables": self._load_table_metadata(connection, objects, owner="DEMO"),
            "operation_state": self._operation_payload(connection, objects),
            "pending_destructive_migrations": list(self.pending_destructive),
        }

    def _table_identity_fields(self, name: str, owner: str) -> dict[str, Any]:
        return {"qualified_name": f"{owner}.{name}"}

    def _initialize_on(self, connection: Any, owner: str, *, recreate: bool) -> dict[str, Any]:
        before = self._status_on(connection)
        if before["status"] == "ready" and not recreate:
            return self._no_op_result(connection, owner)
        self._configure_ddl_lock_timeout(connection)
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        dropped = self._execute_drop(connection, "DROP TABLE DEMO_ITEMS") if recreate else 0
        self._heartbeat(connection, owner)
        self.database.objects[("DEMO_ITEMS", "TABLE")] = datetime.now(UTC)
        with connection.cursor() as cursor:
            self._merge_migration(cursor, key="001", description="items", checksum="sum-1")
        self._finish_operation(connection, owner, increment_epoch=True)
        after = self._status_on(connection)
        return {
            **after,
            "operation": self._operation_name(before["status"], recreate=recreate),
            "dropped_object_count": dropped,
        }


def test_initialize_then_no_op_increments_epoch_only_on_change() -> None:
    database = _FakeDatabase()
    manager = _DemoManager(database)

    initialized = manager.initialize()
    assert initialized["operation"] == "initialized"
    assert initialized["status"] == "ready"
    assert initialized["operation_state"]["schema_epoch"] == 1
    assert initialized["operation_state"]["status"] == "idle"
    assert "ALTER SESSION SET DDL_LOCK_TIMEOUT = 7" in database.statements
    assert manager.is_ready() is True

    no_op = manager.initialize()
    assert no_op["operation"] == "no_op"
    assert (no_op["dropped_object_count"], no_op["created_object_count"]) == (0, 0)
    assert no_op["operation_state"]["schema_epoch"] == 1


def test_initialize_clears_the_database_status_cache() -> None:
    """システムテーブルの操作の後は、DB ゲートの `ok` の cache を使わない（#793）。"""
    from pr_system_settings.database_status import DatabaseStatusCache, DatabaseStatusData

    cache = DatabaseStatusCache(30)
    cache.put("key", DatabaseStatusData(status="ok", check="ok"))
    manager = _DemoManager(_FakeDatabase())

    manager.initialize()

    assert cache.get("key") is None


def test_recreate_checks_confirmation_before_touching_the_database() -> None:
    database = _FakeDatabase()
    manager = _DemoManager(database)

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize(recreate=True, confirmation="RECREATE")
    assert error.value.status_code == 422
    assert manager.control_schema_calls == 0
    assert database.statements == []

    manager.initialize()
    recreated = manager.initialize(recreate=True, confirmation="RECREATE_DEMO_SYSTEM_TABLES")
    assert recreated["operation"] == "recreated"
    assert recreated["dropped_object_count"] == 1
    assert recreated["operation_state"]["schema_epoch"] == 2
    # 既に無い object の DROP は 0 件として読み飛ばす。
    with database.connection() as connection:
        assert manager._execute_drop(connection, "DROP TABLE DEMO_UNKNOWN") == 0


def test_initialize_stops_on_pending_destructive_migrations_without_approval() -> None:
    """データを消す未適用の migration は、承認が無ければ DB を変えずに 409 で止める（#619）。"""

    database = _FakeDatabase()
    manager = _DemoManager(database)
    manager.initialize()
    database.migrations.clear()
    manager.pending_destructive = [{"name": "002_drop_items", "description": "items を消す"}]
    epoch = database.operation["schema_epoch"]

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert error.value.status_code == 409
    assert "002_drop_items" in error.value.public_message
    # lease を取らず、失敗としても記録しない（DB を変えていない）。
    assert manager.control_schema_calls == 1
    assert database.operation["status"] == "IDLE"
    assert database.operation["last_error_code"] is None
    assert database.operation["schema_epoch"] == epoch

    migrated = manager.initialize(allow_destructive=True)
    assert migrated["operation"] == "migrated"
    assert migrated["operation_state"]["schema_epoch"] == epoch + 1


def test_recreate_does_not_ask_for_destructive_approval() -> None:
    """全再作成は確認語で承認済みのため、データを消す migration の承認を別に求めない（#619）。"""

    database = _FakeDatabase()
    manager = _DemoManager(database)
    manager.initialize()
    manager.pending_destructive = [{"name": "002_drop_items", "description": "items を消す"}]

    recreated = manager.initialize(recreate=True, confirmation="RECREATE_DEMO_SYSTEM_TABLES")
    assert recreated["operation"] == "recreated"


def test_require_destructive_migration_confirmation_passes_without_pending_or_with_approval() -> (
    None
):
    require_destructive_migration_confirmation([], allow_destructive=False)
    require_destructive_migration_confirmation(
        [{"name": "002", "description": "x"}], allow_destructive=True
    )
    with pytest.raises(SystemSchemaError):
        require_destructive_migration_confirmation(
            [{"name": "002", "description": "x"}], allow_destructive=False
        )


def test_lease_rejects_concurrent_operation_and_allows_expired_takeover() -> None:
    database = _FakeDatabase()
    manager = _DemoManager(database)
    manager._ensure_control_schema()

    manager._claim_lease("first", "initialize")
    assert database.operation["operation_kind"] == "initialize"
    with pytest.raises(SystemSchemaBusyError) as busy:
        manager.initialize()
    assert busy.value.status_code == 409
    # 他者の lease の延長・終了はできない。
    with database.connection() as connection:
        with pytest.raises(SystemSchemaBusyError):
            manager._heartbeat(connection, "second")
        with pytest.raises(SystemSchemaBusyError):
            manager._finish_operation(connection, "second", increment_epoch=True)
    assert database.operation["lease_owner"] == "first"

    database.operation["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    assert manager.initialize()["status"] == "ready"


def test_failure_is_recorded_safely_and_can_be_retried() -> None:
    database = _FakeDatabase()
    manager = _DemoManager(database, fail_with="password=secret ORA-00600 full sql")

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == "ORA-00600"
    assert error.value.status_code == 500
    assert "secret" not in error.value.public_message
    assert database.operation["status"] == "FAILED"
    assert database.operation["last_error_code"] == "ORA-00600"
    assert manager.status()["operation_state"]["last_error_code"] == "ORA-00600"

    manager.fail_with = None
    assert manager.initialize()["operation"] == "initialized"


def test_lock_timeout_is_a_retryable_conflict_with_product_guidance() -> None:
    database = _FakeDatabase()
    manager = _DemoManager(database, fail_with="ORA-00054: resource busy")

    with pytest.raises(SystemSchemaError) as error:
        manager.initialize()
    assert error.value.code == "ORA-00054"
    assert error.value.status_code == 409
    assert error.value.retry_headers == {"Retry-After": "5"}
    assert error.value.public_message == (
        "Oracle の対象オブジェクトのロックが 7 秒以内に解放されませんでした (ORA-00054)。"
        "デモの job を止めてから、状態を再取得して再試行してください。"
    )


def test_status_reports_table_metadata_and_idle_state_without_control_table() -> None:
    database = _FakeDatabase()
    manager = _DemoManager(database)

    status = manager.status()
    assert status["status"] == "missing"
    assert status["operation_state"] == idle_operation_state()
    assert [table["exists"] for table in status["tables"]] == [False, False, False]

    manager.initialize()
    tables = {table["name"]: table for table in manager.status()["tables"]}
    assert tables["DEMO_ITEMS"]["exists"] is True
    assert tables["DEMO_ITEMS"]["estimated_rows"] == 12
    assert tables["DEMO_ITEMS"]["qualified_name"] == "DEMO.DEMO_ITEMS"


# ---- 外部キーの差分（#505） ---------------------------------------------------

_CANONICAL_ITEMS = """CREATE TABLE demo_items (
    item_id   VARCHAR2(36) PRIMARY KEY,
    parent_id VARCHAR2(36),
    tenant_id VARCHAR2(36),
    owner_id  VARCHAR2(36),
    CONSTRAINT demo_items_parent_fk
        FOREIGN KEY (parent_id) REFERENCES demo_parents (parent_id) ON DELETE CASCADE,
    CONSTRAINT demo_items_owner_fk
        FOREIGN KEY (tenant_id, owner_id)
        REFERENCES demo_owners (tenant_id, owner_id)
        ON DELETE SET NULL,
    CONSTRAINT demo_items_self_fk FOREIGN KEY (item_id) REFERENCES demo_items (item_id)
)"""


def test_foreign_keys_are_parsed_from_create_table_only() -> None:
    specs = {spec.name: spec for spec in foreign_keys_from_create_table(_CANONICAL_ITEMS)}

    assert specs["DEMO_ITEMS_PARENT_FK"] == ForeignKeySpec(
        name="DEMO_ITEMS_PARENT_FK",
        table_name="DEMO_ITEMS",
        columns=("PARENT_ID",),
        referenced_table_name="DEMO_PARENTS",
        referenced_columns=("PARENT_ID",),
        delete_rule="CASCADE",
    )
    assert specs["DEMO_ITEMS_OWNER_FK"].columns == ("TENANT_ID", "OWNER_ID")
    assert specs["DEMO_ITEMS_OWNER_FK"].delete_rule == "SET NULL"
    assert specs["DEMO_ITEMS_SELF_FK"].delete_rule == "NO ACTION"
    assert (
        foreign_keys_from_create_table(
            "ALTER TABLE demo_items ADD CONSTRAINT x_fk FOREIGN KEY (a) REFERENCES b (a)"
        )
        == []
    )
    assert foreign_keys_from_create_table("CREATE TABLE demo_plain (id NUMBER PRIMARY KEY)") == []


def test_foreign_key_spec_rejects_unsafe_identifiers_and_shapes() -> None:
    base = {
        "name": "DEMO_FK",
        "table_name": "DEMO_ITEMS",
        "columns": ("PARENT_ID",),
        "referenced_table_name": "DEMO_PARENTS",
        "referenced_columns": ("PARENT_ID",),
    }
    for override in (
        {"table_name": "DEMO_ITEMS; DROP TABLE X"},
        {"columns": ('"quoted"',)},
        {"columns": ("A", "B")},
        {"columns": ()},
        {"delete_rule": "RESTRICT"},
    ):
        with pytest.raises(ValueError):
            ForeignKeySpec(**{**base, **override})  # type: ignore[arg-type]


def test_foreign_key_ddl_and_orphan_sql() -> None:
    specs = {spec.name: spec for spec in foreign_keys_from_create_table(_CANONICAL_ITEMS)}
    parent = specs["DEMO_ITEMS_PARENT_FK"]
    owner = specs["DEMO_ITEMS_OWNER_FK"]

    assert add_foreign_key_sql(parent, validate=True) == (
        "ALTER TABLE DEMO_ITEMS ADD CONSTRAINT DEMO_ITEMS_PARENT_FK FOREIGN KEY (PARENT_ID) "
        "REFERENCES DEMO_PARENTS (PARENT_ID) ON DELETE CASCADE"
    )
    assert add_foreign_key_sql(owner, validate=False) == (
        "ALTER TABLE DEMO_ITEMS ADD CONSTRAINT DEMO_ITEMS_OWNER_FK "
        "FOREIGN KEY (TENANT_ID, OWNER_ID) REFERENCES DEMO_OWNERS (TENANT_ID, OWNER_ID) "
        "ON DELETE SET NULL ENABLE NOVALIDATE"
    )
    assert add_foreign_key_sql(specs["DEMO_ITEMS_SELF_FK"], validate=True).endswith(
        "REFERENCES DEMO_ITEMS (ITEM_ID)"
    )
    # 列のどれかが NULL の行は Oracle も検査しないので数えない。
    assert orphan_rows_sql(owner) == (
        "SELECT COUNT(*) FROM DEMO_ITEMS child "
        "WHERE child.TENANT_ID IS NOT NULL AND child.OWNER_ID IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM DEMO_OWNERS parent "
        "WHERE parent.TENANT_ID = child.TENANT_ID AND parent.OWNER_ID = child.OWNER_ID)"
    )


def test_classify_treats_missing_foreign_keys_as_outdated() -> None:
    assert (
        classify_system_schema_status(
            set(MANAGED),
            domain_tables=DOMAIN,
            managed_objects=MANAGED,
            migrations_current=True,
            foreign_keys_current=False,
        )
        == "outdated"
    )
    # 表が足りないときは partial が優先（FK は表の作成で作られる）。
    assert (
        classify_system_schema_status(
            {("DEMO_ITEMS", "TABLE")},
            domain_tables=DOMAIN,
            managed_objects=MANAGED,
            migrations_current=True,
            foreign_keys_current=False,
        )
        == "partial"
    )


def test_classify_treats_missing_columns_as_outdated() -> None:
    """台帳が最新でも、既存の表に正本の列が無ければ outdated（#2450）。"""
    assert (
        classify_system_schema_status(
            set(MANAGED),
            domain_tables=DOMAIN,
            managed_objects=MANAGED,
            migrations_current=True,
            columns_current=False,
        )
        == "outdated"
    )


def test_columns_from_create_table_skips_constraints_and_comments() -> None:
    statement = """CREATE TABLE demo_items (
    item_id    VARCHAR2(64) PRIMARY KEY,
    -- 注釈の行（列, ではない）
    amount     NUMBER(19, 2) DEFAULT 0 NOT NULL,
    payload    JSON,
    owner_id   VARCHAR2(64),
    CONSTRAINT demo_items_amount_ck CHECK (amount >= 0),
    CONSTRAINT demo_items_owner_fk
        FOREIGN KEY (owner_id) REFERENCES demo_owners (owner_id)
)"""
    assert columns_from_create_table(statement) == (
        "DEMO_ITEMS",
        ("ITEM_ID", "AMOUNT", "PAYLOAD", "OWNER_ID"),
    )
    assert columns_from_create_table("CREATE INDEX demo_items_idx ON demo_items (owner_id)") is None
    assert columns_from_create_table("ALTER TABLE demo_items ADD (memo CLOB)") is None


def test_columns_from_create_table_keeps_comment_markers_inside_literals() -> None:
    statement = """CREATE TABLE demo_items (
    item_id VARCHAR2(64) DEFAULT 'a--b, (c' NOT NULL, /* 注釈, の中 */
    memo    CLOB
)"""
    assert columns_from_create_table(statement) == ("DEMO_ITEMS", ("ITEM_ID", "MEMO"))


def test_columns_from_statements_folds_versioned_ddl() -> None:
    """版ごとの migration を順に当てた後の列（#2458）。"""

    statements = [
        "CREATE TABLE demo_items (item_id VARCHAR2(64) PRIMARY KEY, legacy_flag CHAR(1))",
        # 既にある表の CREATE TABLE は作られない（ORA-00955）ので、列を変えない。
        "CREATE TABLE demo_items (item_id VARCHAR2(64), other_col NUMBER)",
        "ALTER TABLE demo_items ADD (\n    worker_id VARCHAR2(256),\n    attempt_no NUMBER(10)\n)",
        "ALTER TABLE demo_items ADD note VARCHAR2(10) DEFAULT 'x,y'",
        "ALTER TABLE demo_items ADD CONSTRAINT demo_items_ck CHECK (attempt_no >= 0)",
        "ALTER TABLE demo_items MODIFY (attempt_no DEFAULT 0 NOT NULL)",
        "ALTER TABLE demo_items DROP COLUMN legacy_flag",
        "ALTER TABLE demo_items RENAME COLUMN note TO memo",
        "CREATE TABLE demo_logs (log_id NUMBER, a NUMBER, b NUMBER)",
        "ALTER TABLE demo_logs DROP (a, b)",
        "CREATE TABLE demo_retired (retired_id NUMBER)",
        "DROP TABLE demo_retired CASCADE CONSTRAINTS PURGE",
        # 正本で作っていない表は追わない。
        "ALTER TABLE other_items ADD (extra NUMBER)",
        "CREATE INDEX demo_items_idx ON demo_items (worker_id)",
    ]
    assert columns_from_statements(statements) == {
        "DEMO_ITEMS": ("ITEM_ID", "WORKER_ID", "ATTEMPT_NO", "MEMO"),
        "DEMO_LOGS": ("LOG_ID",),
    }


def test_columns_from_statements_reads_execute_immediate_literals() -> None:
    """PL/SQL の block は、EXECUTE IMMEDIATE の文字列リテラルの DDL を当てる。"""

    statements = [
        "CREATE TABLE demo_items (item_id VARCHAR2(64))",
        """BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE demo_items ADD (profile_id VARCHAR2(128))';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -1430 THEN RAISE; END IF;
END;""",
        """DECLARE
    v_count NUMBER;
BEGIN
    -- 'ALTER TABLE demo_items ADD (commented_out NUMBER)' は注釈なので当てない
    EXECUTE IMMEDIATE q'[ALTER TABLE demo_items ADD (status VARCHAR2(16) DEFAULT 'new')]';
    EXECUTE IMMEDIATE 'ALTER TABLE demo_items ADD (label VARCHAR2(8) '
        || 'DEFAULT ''a'')';
    EXECUTE IMMEDIATE v_sql;
END;""",
        "BEGIN EXECUTE IMMEDIATE 'DROP TABLE missing_table'; END;",
    ]
    assert columns_from_statements(statements) == {
        "DEMO_ITEMS": ("ITEM_ID", "PROFILE_ID", "STATUS", "LABEL"),
    }


def test_missing_columns_ignores_absent_tables() -> None:
    expected = {"DEMO_ITEMS": ("ITEM_ID", "PAYLOAD"), "DEMO_LOGS": ("LOG_ID",)}
    # 表そのものが無い（DEMO_LOGS）ときは object の不足で出すので、列には出さない。
    assert missing_columns(expected, {"DEMO_ITEMS": {"ITEM_ID"}}) == [
        {"table_name": "DEMO_ITEMS", "column_name": "PAYLOAD"}
    ]
    assert missing_columns(expected, {"DEMO_ITEMS": {"ITEM_ID", "PAYLOAD"}}) == []


def test_load_existing_columns_reads_user_tab_columns() -> None:
    executed: list[tuple[str, dict[str, Any]]] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, binds: dict[str, Any]) -> None:
            executed.append((sql, binds))

        def fetchall(self) -> list[tuple[str, str]]:
            return [("DEMO_ITEMS", "item_id"), ("DEMO_ITEMS", "PAYLOAD")]

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    assert load_existing_columns(_Connection(), ["DEMO_ITEMS", "DEMO_ITEMS"]) == {
        "DEMO_ITEMS": {"ITEM_ID", "PAYLOAD"}
    }
    assert executed[0][0].startswith("SELECT TABLE_NAME, COLUMN_NAME FROM USER_TAB_COLUMNS")
    assert list(executed[0][1].values()) == ["DEMO_ITEMS"]
    assert load_existing_columns(_Connection(), []) == {}
    assert len(executed) == 1


class _ForeignKeyDatabase:
    """USER_TABLES・USER_CONSTRAINTS・孤立した行の件数と、FK の DDL だけを扱う fake。"""

    def __init__(self, tables: set[str]) -> None:
        self.tables = set(tables)
        # 制約名 → (定義, 既存の行を検査済みか)
        self.foreign_keys: dict[str, tuple[ForeignKeySpec, bool]] = {}
        self.orphans: dict[str, int] = {}
        # 数えた後に孤立した行が増えた（検査付きの追加だけが ORA-02298 で失敗する）。
        self.orphans_after_count: dict[str, int] = {}
        self.fail_orphan_count = False
        # 無効化されている FK の名前（#511）。
        self.disabled: set[str] = set()
        # 行の削除で起こす Oracle のエラー（ORA-02292 など）。
        self.fail_delete_with: str | None = None
        # 数えた後に孤立した行が増え、最初の VALIDATE だけが ORA-02298 になる（#511）。
        self.orphans_after_delete: dict[str, int] = {}
        # 制御テーブルの更新（lease の取得・延長・終了・失敗の記録）。
        self.operation_updates: list[str] = []
        self.statements: list[str] = []

    @contextmanager
    def connection(self) -> Iterator[_ForeignKeyConnection]:
        yield _ForeignKeyConnection(self)


class _ForeignKeyConnection:
    def __init__(self, database: _ForeignKeyDatabase) -> None:
        self.database = database

    def cursor(self) -> _ForeignKeyCursor:
        return _ForeignKeyCursor(self.database)

    def commit(self) -> None:
        return None


class _ForeignKeyCursor:
    def __init__(self, database: _ForeignKeyDatabase) -> None:
        self.database = database
        self.rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> _ForeignKeyCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> None:
        sql = re.sub(r"\s+", " ", statement).strip()
        upper = sql.upper()
        params = params or {}
        self.database.statements.append(sql)
        self.rows = []
        self.rowcount = 0
        if upper.startswith("SELECT TABLE_NAME FROM USER_TABLES"):
            self.rows = [(name,) for name in params.values() if name in self.database.tables]
        elif upper.startswith("SELECT CHILD.CONSTRAINT_NAME"):
            names = set(params.values())
            self.rows = [
                (
                    spec.name,
                    spec.table_name,
                    spec.referenced_table_name,
                    spec.delete_rule,
                    "DISABLED" if spec.name in self.database.disabled else "ENABLED",
                    "VALIDATED" if validated else "NOT VALIDATED",
                    column,
                    referenced,
                )
                for spec, validated in self.database.foreign_keys.values()
                if spec.table_name in names
                for column, referenced in zip(spec.columns, spec.referenced_columns, strict=True)
            ]
        elif upper.startswith("SELECT COUNT(*) FROM"):
            if self.database.fail_orphan_count:
                raise RuntimeError("ORA-00904: invalid identifier")
            table = upper.split()[3]
            self.rows = [(self.database.orphans.get(table, 0),)]
        elif upper.startswith("DELETE FROM"):
            table = upper.split()[2]
            if self.database.fail_delete_with:
                raise RuntimeError(self.database.fail_delete_with)
            self.rowcount = self.database.orphans.get(table, 0)
            self.database.orphans[table] = self.database.orphans_after_delete.pop(table, 0)
        elif upper.startswith("ALTER TABLE") and " DROP CONSTRAINT " in upper:
            name = upper.split()[-1]
            if name not in self.database.foreign_keys:
                raise RuntimeError("ORA-02443: Cannot drop constraint - nonexistent constraint")
            self.database.foreign_keys.pop(name)
            self.database.disabled.discard(name)
        elif upper.startswith("ALTER TABLE") and " MODIFY CONSTRAINT " in upper:
            match = re.match(r"ALTER TABLE (\S+) MODIFY CONSTRAINT (\S+) (.+)$", upper)
            assert match is not None
            table, name, state = match.groups()
            spec, validated = self.database.foreign_keys[name]
            validate = state in {"ENABLE VALIDATE", "VALIDATE"}
            if validate and self.database.orphans.get(table, 0):
                raise RuntimeError("ORA-02298: cannot validate - parent keys not found")
            self.database.disabled.discard(name)
            self.database.foreign_keys[name] = (spec, validate)
        elif upper.startswith("ALTER TABLE"):
            match = re.match(r"ALTER TABLE (\S+) ADD CONSTRAINT (\S+) ", upper)
            assert match is not None
            table, name = match.groups()
            spec = next(
                spec
                for spec in foreign_keys_from_create_table(_CANONICAL_ITEMS)
                if spec.name == name
            )
            if any(
                existing.signature == spec.signature
                for existing, _ in self.database.foreign_keys.values()
            ):
                raise RuntimeError("ORA-02275: such a referential constraint already exists")
            novalidate = upper.endswith("ENABLE NOVALIDATE")
            orphans = self.database.orphans.get(table, 0) + self.database.orphans_after_count.get(
                table, 0
            )
            if not novalidate and orphans:
                raise RuntimeError("ORA-02298: cannot validate - parent keys not found")
            self.database.foreign_keys[name] = (spec, not novalidate)
        elif upper.startswith("UPDATE DEMO_SCHEMA_OPERATIONS"):
            # lease は常に取れる（取得・延長・終了・失敗の記録を順に残す）。
            if "SET STATUS = 'RUNNING'" in upper:
                self.database.operation_updates.append("running")
            elif "SCHEMA_EPOCH = SCHEMA_EPOCH + 1" in upper:
                self.database.operation_updates.append("idle+epoch")
            elif "SET STATUS = 'IDLE'" in upper:
                self.database.operation_updates.append("idle")
            elif "SET STATUS = 'FAILED'" in upper:
                self.database.operation_updates.append(f"failed:{params['error_code']}")
            self.rowcount = 1


class _ForeignKeyManager(_DemoManager):
    managed_foreign_keys = tuple(foreign_keys_from_create_table(_CANONICAL_ITEMS))

    def __init__(self, database: _ForeignKeyDatabase) -> None:
        SystemSchemaManagerBase.__init__(
            self, database.connection, lease_seconds=1, ddl_lock_timeout_seconds=7
        )

    def _ensure_control_schema(self) -> None:
        return None

    def _status_on(self, connection: Any) -> dict[str, Any]:
        drift = self._foreign_key_drift(connection)
        return {
            "status": "ready" if drift.current else "outdated",
            **drift.status_fields(),
            "operation_state": {"schema_epoch": 0},
        }


def test_inspect_compares_definitions_and_counts_orphans() -> None:
    specs = {spec.name: spec for spec in foreign_keys_from_create_table(_CANONICAL_ITEMS)}
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    database.orphans["DEMO_ITEMS"] = 3
    with database.connection() as connection:
        drift = inspect_foreign_keys(connection, list(specs.values()))
    # 参照先の表（DEMO_OWNERS）が無い FK は比べない。
    assert [(spec.name, orphans) for spec, orphans in drift.missing] == [
        ("DEMO_ITEMS_PARENT_FK", 3),
        ("DEMO_ITEMS_SELF_FK", 3),
    ]
    assert drift.orphaned == []

    # 別名でも同じ定義の FK があれば不足にしない。検査していない FK の孤立した行は警告に出す。
    renamed = ForeignKeySpec(
        name="LEGACY_PARENT_FK",
        table_name="DEMO_ITEMS",
        columns=("PARENT_ID",),
        referenced_table_name="DEMO_PARENTS",
        referenced_columns=("PARENT_ID",),
        delete_rule="CASCADE",
    )
    database.foreign_keys[renamed.name] = (renamed, False)
    with database.connection() as connection:
        drift = inspect_foreign_keys(connection, list(specs.values()))
    assert [spec.name for spec, _ in drift.missing] == ["DEMO_ITEMS_SELF_FK"]
    assert drift.status_fields()["orphaned_foreign_keys"] == [
        {
            "name": "LEGACY_PARENT_FK",
            "table_name": "DEMO_ITEMS",
            "columns": ["PARENT_ID"],
            "referenced_table_name": "DEMO_PARENTS",
            "referenced_columns": ["PARENT_ID"],
            "delete_rule": "CASCADE",
            "orphan_rows": 3,
        }
    ]
    SystemTableForeignKeyData.model_validate(drift.status_fields()["orphaned_foreign_keys"][0])

    # 数えられない（列がまだ無い等）ときも状態の取得は失敗させない。
    database.fail_orphan_count = True
    with database.connection() as connection:
        drift = inspect_foreign_keys(connection, list(specs.values()))
    assert [(spec.name, orphans) for spec, orphans in drift.missing] == [
        ("DEMO_ITEMS_SELF_FK", None)
    ]
    assert drift.orphaned == []


def test_apply_adds_validated_or_novalidate_foreign_keys_without_deleting_rows() -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS", "DEMO_OWNERS"})
    database.orphans["DEMO_ITEMS"] = 0
    manager = _ForeignKeyManager(database)

    with database.connection() as connection:
        added = manager._apply_missing_foreign_keys(connection, "owner")
        again = manager._apply_missing_foreign_keys(connection, "owner")

    assert [(item["name"], item["validated"]) for item in added] == [
        ("DEMO_ITEMS_PARENT_FK", True),
        ("DEMO_ITEMS_OWNER_FK", True),
        ("DEMO_ITEMS_SELF_FK", True),
    ]
    assert again == []
    assert not any("NOVALIDATE" in statement for statement in database.statements)

    orphaned = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    orphaned.orphans["DEMO_ITEMS"] = 2
    with orphaned.connection() as connection:
        added = _ForeignKeyManager(orphaned)._apply_missing_foreign_keys(connection, "owner")
    assert [(item["name"], item["validated"], item["orphan_rows"]) for item in added] == [
        ("DEMO_ITEMS_PARENT_FK", False, 2),
        ("DEMO_ITEMS_SELF_FK", False, 2),
    ]
    # 孤立した行があると分かっているときは、検査付きの追加を試さない。既存の行は消さない。
    assert all(
        statement.endswith("ENABLE NOVALIDATE")
        for statement in orphaned.statements
        if statement.startswith("ALTER TABLE")
    )
    assert not any(statement.upper().startswith("DELETE") for statement in orphaned.statements)


def test_apply_falls_back_to_novalidate_when_orphans_appear_after_count() -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    database.orphans_after_count["DEMO_ITEMS"] = 1
    manager = _ForeignKeyManager(database)

    with database.connection() as connection:
        added = manager._apply_missing_foreign_keys(connection, "owner")

    assert [(item["name"], item["validated"]) for item in added] == [
        ("DEMO_ITEMS_PARENT_FK", False),
        ("DEMO_ITEMS_SELF_FK", False),
    ]
    alters = [statement for statement in database.statements if statement.startswith("ALTER")]
    assert [statement.endswith("ENABLE NOVALIDATE") for statement in alters] == [
        False,
        True,
        False,
        True,
    ]


# ---- 削除規則の違い・無効化・参照先の無い行の削除（#511） -------------------------


def _canonical(name: str) -> ForeignKeySpec:
    return next(
        spec for spec in foreign_keys_from_create_table(_CANONICAL_ITEMS) if spec.name == name
    )


def _with_rule(
    spec: ForeignKeySpec, delete_rule: str, *, name: str | None = None
) -> ForeignKeySpec:
    return ForeignKeySpec(
        name=name or spec.name,
        table_name=spec.table_name,
        columns=spec.columns,
        referenced_table_name=spec.referenced_table_name,
        referenced_columns=spec.referenced_columns,
        delete_rule=delete_rule,
    )


def _all_present(database: _ForeignKeyDatabase) -> None:
    for spec in foreign_keys_from_create_table(_CANONICAL_ITEMS):
        database.foreign_keys[spec.name] = (spec, True)


def test_foreign_key_repair_and_orphan_ddl() -> None:
    parent = _canonical("DEMO_ITEMS_PARENT_FK")
    assert (
        drop_foreign_key_sql(parent)
        == "ALTER TABLE DEMO_ITEMS DROP CONSTRAINT DEMO_ITEMS_PARENT_FK"
    )
    assert enable_foreign_key_sql(parent, validate=True) == (
        "ALTER TABLE DEMO_ITEMS MODIFY CONSTRAINT DEMO_ITEMS_PARENT_FK ENABLE VALIDATE"
    )
    assert enable_foreign_key_sql(parent, validate=False).endswith(" ENABLE NOVALIDATE")
    assert validate_foreign_key_sql(parent) == (
        "ALTER TABLE DEMO_ITEMS MODIFY CONSTRAINT DEMO_ITEMS_PARENT_FK VALIDATE"
    )
    # 数える SQL と同じ条件で消す（FK の列のどれかが NULL の行は消さない）。
    assert delete_orphan_rows_sql(parent) == (
        "DELETE FROM DEMO_ITEMS child WHERE child.PARENT_ID IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM DEMO_PARENTS parent WHERE parent.PARENT_ID = child.PARENT_ID)"
    )
    assert orphan_rows_sql(parent).removeprefix("SELECT COUNT(*) FROM ") == (
        delete_orphan_rows_sql(parent).removeprefix("DELETE FROM ")
    )
    request = SystemTablesDeleteOrphansRequest(constraint_name="X_FK", expected_orphan_rows=0)
    assert request.model_dump() == {"constraint_name": "X_FK", "expected_orphan_rows": 0}
    with pytest.raises(ValueError):
        SystemTablesDeleteOrphansRequest(constraint_name="X_FK", expected_orphan_rows=-1)


def test_inspect_reports_delete_rule_mismatch_and_disabled_foreign_keys() -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS", "DEMO_OWNERS"})
    _all_present(database)
    # 旧版が別名・NO ACTION で作った FK（正本は CASCADE）と、無効化された FK。
    database.foreign_keys.pop("DEMO_ITEMS_PARENT_FK")
    legacy = _with_rule(_canonical("DEMO_ITEMS_PARENT_FK"), "NO ACTION", name="LEGACY_PARENT_FK")
    database.foreign_keys[legacy.name] = (legacy, True)
    database.disabled.add("DEMO_ITEMS_OWNER_FK")
    database.foreign_keys["DEMO_ITEMS_OWNER_FK"] = (_canonical("DEMO_ITEMS_OWNER_FK"), False)
    database.orphans["DEMO_ITEMS"] = 4

    with database.connection() as connection:
        drift = inspect_foreign_keys(connection, list(_ForeignKeyManager.managed_foreign_keys))

    assert drift.current is False
    assert drift.missing == []
    fields = drift.status_fields()
    assert fields["mismatched_foreign_keys"] == [
        {
            "name": "DEMO_ITEMS_PARENT_FK",
            "table_name": "DEMO_ITEMS",
            "columns": ["PARENT_ID"],
            "referenced_table_name": "DEMO_PARENTS",
            "referenced_columns": ["PARENT_ID"],
            "delete_rule": "CASCADE",
            "orphan_rows": 4,
            "current_name": "LEGACY_PARENT_FK",
            "current_delete_rule": "NO ACTION",
        }
    ]
    assert [(item["name"], item["orphan_rows"]) for item in fields["disabled_foreign_keys"]] == [
        ("DEMO_ITEMS_OWNER_FK", 4)
    ]
    # 無効化された FK は、孤立した行の警告ではなく更新の差分として出す。
    assert fields["orphaned_foreign_keys"] == []
    for item in (*fields["mismatched_foreign_keys"], *fields["disabled_foreign_keys"]):
        SystemTableForeignKeyData.model_validate(item)

    # 削除規則の違いと無効化が重なるときは、削除規則の違いとして扱う（DROP と ADD で両方直る）。
    database.disabled.add("LEGACY_PARENT_FK")
    with database.connection() as connection:
        drift = inspect_foreign_keys(connection, list(_ForeignKeyManager.managed_foreign_keys))
    assert [item.current.name for item in drift.mismatched] == ["LEGACY_PARENT_FK"]
    assert [spec.name for spec, _ in drift.disabled] == ["DEMO_ITEMS_OWNER_FK"]


def test_repair_recreates_mismatched_and_enables_disabled_without_deleting_rows() -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS", "DEMO_OWNERS"})
    _all_present(database)
    database.foreign_keys.pop("DEMO_ITEMS_PARENT_FK")
    legacy = _with_rule(_canonical("DEMO_ITEMS_PARENT_FK"), "SET NULL", name="LEGACY_PARENT_FK")
    database.foreign_keys[legacy.name] = (legacy, True)
    database.disabled.add("DEMO_ITEMS_SELF_FK")
    database.foreign_keys["DEMO_ITEMS_SELF_FK"] = (_canonical("DEMO_ITEMS_SELF_FK"), False)
    manager = _ForeignKeyManager(database)

    with database.connection() as connection:
        repaired = manager._repair_foreign_keys(connection, "owner")
        again = manager._repair_foreign_keys(connection, "owner")

    assert repaired["added"] == []
    assert [
        (item["name"], item["current_name"], item["current_delete_rule"], item["validated"])
        for item in repaired["recreated"]
    ] == [("DEMO_ITEMS_PARENT_FK", "LEGACY_PARENT_FK", "SET NULL", True)]
    assert [(item["name"], item["validated"]) for item in repaired["enabled"]] == [
        ("DEMO_ITEMS_SELF_FK", True)
    ]
    assert again == {"added": [], "recreated": [], "enabled": []}
    assert "LEGACY_PARENT_FK" not in database.foreign_keys
    assert database.foreign_keys["DEMO_ITEMS_PARENT_FK"] == (
        _canonical("DEMO_ITEMS_PARENT_FK"),
        True,
    )
    assert database.disabled == set()
    alters = [statement for statement in database.statements if statement.startswith("ALTER")]
    assert alters == [
        "ALTER TABLE DEMO_ITEMS DROP CONSTRAINT LEGACY_PARENT_FK",
        "ALTER TABLE DEMO_ITEMS ADD CONSTRAINT DEMO_ITEMS_PARENT_FK FOREIGN KEY (PARENT_ID) "
        "REFERENCES DEMO_PARENTS (PARENT_ID) ON DELETE CASCADE",
        "ALTER TABLE DEMO_ITEMS MODIFY CONSTRAINT DEMO_ITEMS_SELF_FK ENABLE VALIDATE",
    ]
    assert not any(statement.upper().startswith("DELETE") for statement in database.statements)


def test_repair_uses_novalidate_when_orphan_rows_exist() -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    _all_present(database)
    database.foreign_keys.pop("DEMO_ITEMS_OWNER_FK")  # 参照先の表が無いので比べない
    database.foreign_keys.pop("DEMO_ITEMS_PARENT_FK")
    legacy = _with_rule(_canonical("DEMO_ITEMS_PARENT_FK"), "NO ACTION")
    database.foreign_keys[legacy.name] = (legacy, False)
    database.disabled.add("DEMO_ITEMS_SELF_FK")
    database.foreign_keys["DEMO_ITEMS_SELF_FK"] = (_canonical("DEMO_ITEMS_SELF_FK"), False)
    database.orphans["DEMO_ITEMS"] = 2
    manager = _ForeignKeyManager(database)

    with database.connection() as connection:
        repaired = manager._repair_foreign_keys(connection, "owner")

    assert [(item["name"], item["validated"]) for item in repaired["recreated"]] == [
        ("DEMO_ITEMS_PARENT_FK", False)
    ]
    assert [(item["name"], item["validated"]) for item in repaired["enabled"]] == [
        ("DEMO_ITEMS_SELF_FK", False)
    ]
    # 孤立した行があると分かっているときは、検査付きの DDL を試さない。既存の行は消さない。
    alters = [statement for statement in database.statements if statement.startswith("ALTER")]
    assert alters[1].endswith(" ON DELETE CASCADE ENABLE NOVALIDATE")
    assert alters[2].endswith(" ENABLE NOVALIDATE")
    assert not any(statement.upper().startswith("DELETE") for statement in database.statements)


def test_enable_falls_back_to_novalidate_when_orphans_appear_after_count() -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    spec = _canonical("DEMO_ITEMS_SELF_FK")
    database.foreign_keys[spec.name] = (spec, False)
    database.disabled.add(spec.name)
    database.orphans["DEMO_ITEMS"] = 1

    with database.connection() as connection:
        validated = SystemSchemaManagerBase._enable_foreign_key(connection, spec, validate=True)

    assert validated is False
    assert [statement.rsplit(" ", 1)[-1] for statement in database.statements] == [
        "VALIDATE",
        "NOVALIDATE",
    ]
    # 既に無い FK の削除は読み飛ばす（別の操作が先に削除した）。
    with database.connection() as connection:
        SystemSchemaManagerBase._drop_foreign_key(connection, _canonical("DEMO_ITEMS_PARENT_FK"))


def _orphaned_database(orphans: int = 3) -> _ForeignKeyDatabase:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    _all_present(database)
    database.foreign_keys.pop("DEMO_ITEMS_OWNER_FK")
    spec = _canonical("DEMO_ITEMS_PARENT_FK")
    database.foreign_keys[spec.name] = (spec, False)  # NOVALIDATE で追加した
    database.orphans["DEMO_ITEMS"] = orphans
    return database


def test_delete_orphaned_rows_deletes_confirmed_rows_and_validates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = _orphaned_database(3)
    manager = _ForeignKeyManager(database)
    assert [item["name"] for item in manager.status()["orphaned_foreign_keys"]] == [
        "DEMO_ITEMS_PARENT_FK"
    ]

    with caplog.at_level("INFO"):
        result = manager.delete_orphaned_rows(
            constraint_name="demo_items_parent_fk", expected_orphan_rows=3
        )

    assert result["operation"] == "orphans_deleted"
    assert result["deleted_row_count"] == 3
    assert result["foreign_key"]["name"] == "DEMO_ITEMS_PARENT_FK"
    assert result["orphaned_foreign_keys"] == []
    assert database.foreign_keys["DEMO_ITEMS_PARENT_FK"][1] is True
    assert database.operation_updates == ["running", "idle+epoch"]
    deletes = [statement for statement in database.statements if statement.startswith("DELETE")]
    assert deletes == [delete_orphan_rows_sql(_canonical("DEMO_ITEMS_PARENT_FK"))]
    assert "ALTER TABLE DEMO_ITEMS MODIFY CONSTRAINT DEMO_ITEMS_PARENT_FK VALIDATE" in (
        database.statements
    )
    record = next(
        item for item in caplog.records if item.msg == "demo_system_schema_operation_succeeded"
    )
    assert (record.operation, record.constraint_name, record.table_name) == (
        "orphans_deleted",
        "DEMO_ITEMS_PARENT_FK",
        "DEMO_ITEMS",
    )
    assert (record.expected_orphan_rows, record.deleted_row_count) == (3, 3)

    # もう消す行も検査も無いときは、DB を変えずに no_op。
    database.operation_updates.clear()
    again = manager.delete_orphaned_rows(
        constraint_name="DEMO_ITEMS_PARENT_FK", expected_orphan_rows=0
    )
    assert (again["operation"], again["deleted_row_count"]) == ("no_op", 0)
    assert database.operation_updates == ["running", "idle"]


def test_delete_orphaned_rows_validates_even_when_no_rows_remain() -> None:
    database = _orphaned_database(0)
    result = _ForeignKeyManager(database).delete_orphaned_rows(
        constraint_name="DEMO_ITEMS_PARENT_FK", expected_orphan_rows=0
    )
    assert (result["operation"], result["deleted_row_count"]) == ("orphans_deleted", 0)
    assert database.foreign_keys["DEMO_ITEMS_PARENT_FK"][1] is True


def test_delete_orphaned_rows_refuses_more_rows_than_confirmed() -> None:
    database = _orphaned_database(5)
    manager = _ForeignKeyManager(database)

    with pytest.raises(SystemSchemaError) as error:
        manager.delete_orphaned_rows(constraint_name="DEMO_ITEMS_PARENT_FK", expected_orphan_rows=3)

    assert isinstance(error.value, SystemSchemaPreconditionError)
    assert (error.value.code, error.value.status_code) == ("SCHEMA_ORPHAN_ROWS_CHANGED", 409)
    assert "確認時 3 件、現在 5 件" in error.value.public_message
    assert not any(statement.startswith("DELETE") for statement in database.statements)
    # 前提の確認で止めたときは lease を返し、失敗として記録しない。
    assert database.operation_updates == ["running", "idle"]


@pytest.mark.parametrize(
    ("constraint_name", "expected_code", "expected_status"),
    [
        ("DEMO_UNKNOWN_FK", "SCHEMA_FOREIGN_KEY_NOT_FOUND", 404),
        ("demo_items; select 1", "SCHEMA_FOREIGN_KEY_NOT_FOUND", 404),
        ("DEMO_ITEMS_SELF_FK_DISABLED", "SCHEMA_FOREIGN_KEY_OUTDATED", 409),
        ("DEMO_ITEMS_PARENT_LEGACY", "SCHEMA_FOREIGN_KEY_OUTDATED", 409),
    ],
    ids=["unknown", "unsafe", "disabled", "mismatched"],
)
def test_delete_orphaned_rows_requires_a_current_managed_foreign_key(
    constraint_name: str, expected_code: str, expected_status: int
) -> None:
    database = _ForeignKeyDatabase({"DEMO_ITEMS", "DEMO_PARENTS"})
    self_fk = _with_rule(
        _canonical("DEMO_ITEMS_SELF_FK"), "NO ACTION", name="DEMO_ITEMS_SELF_FK_DISABLED"
    )
    database.foreign_keys[self_fk.name] = (self_fk, False)
    database.disabled.add(self_fk.name)
    legacy = _with_rule(
        _canonical("DEMO_ITEMS_PARENT_FK"), "NO ACTION", name="DEMO_ITEMS_PARENT_LEGACY"
    )
    database.foreign_keys[legacy.name] = (legacy, False)
    database.orphans["DEMO_ITEMS"] = 1

    with pytest.raises(SystemSchemaPreconditionError) as error:
        _ForeignKeyManager(database).delete_orphaned_rows(
            constraint_name=constraint_name, expected_orphan_rows=1
        )

    assert (error.value.code, error.value.status_code) == (expected_code, expected_status)
    # 識別子の形でない入力は文言にそのまま出さない。
    assert "select 1" not in error.value.public_message.lower()
    assert not any(
        statement.startswith(("DELETE", "ALTER TABLE")) for statement in database.statements
    )


def test_delete_orphaned_rows_retries_validate_once_and_reports_child_rows() -> None:
    database = _orphaned_database(2)
    database.orphans_after_delete["DEMO_ITEMS"] = 1
    result = _ForeignKeyManager(database).delete_orphaned_rows(
        constraint_name="DEMO_ITEMS_PARENT_FK", expected_orphan_rows=2
    )
    assert result["deleted_row_count"] == 3
    assert sum(statement.startswith("DELETE") for statement in database.statements) == 2

    # 消す行を別の表の NO ACTION の FK が参照している（ORA-02292）。失敗として記録する。
    blocked = _orphaned_database(2)
    blocked.fail_delete_with = "ORA-02292: integrity constraint violated - child record found"
    with pytest.raises(SystemSchemaError) as error:
        _ForeignKeyManager(blocked).delete_orphaned_rows(
            constraint_name="DEMO_ITEMS_PARENT_FK", expected_orphan_rows=2
        )
    assert (error.value.code, error.value.status_code) == ("ORA-02292", 409)
    assert "別の表の行が参照" in error.value.public_message
    assert blocked.operation_updates[-1] == "failed:ORA-02292"
    assert blocked.foreign_keys["DEMO_ITEMS_PARENT_FK"][1] is False


class _ParserSection(BaseModel):
    adapter_backend: int


def test_operation_failure_hint_follows_cause_chain_with_first_lines() -> None:
    """設定の読み込みの失敗は、外側の message（path）と原因の 1 行目を出す（#1701）。"""
    try:
        try:
            raise ValueError("JSON object ではありません。\n2 行目は出さない")
        except ValueError as inner:
            raise ValueError("設定ファイルを読み込めません: /srv/settings.json") from inner
    except ValueError as exc:
        hint = operation_failure_hint(exc)

    assert hint == (
        "ValueError: 設定ファイルを読み込めません: /srv/settings.json"
        " ← ValueError: JSON object ではありません。"
    )


def test_operation_failure_hint_hides_validation_input_values() -> None:
    """pydantic の検証エラーは件数と項目の位置だけを出し、値（input_value）は出さない。"""
    try:
        _ParserSection.model_validate({"adapter_backend": "top-secret-value"})
    except ValidationError as exc:
        hint = operation_failure_hint(exc)

    assert hint == "ValidationError: _ParserSection の検証エラー 1 件（adapter_backend）"
    assert "top-secret-value" not in (hint or "")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("api_key=sk-secret-1 を確認", "RuntimeError: api_key=*** を確認"),
        ("PASSWORD: hunter2", "RuntimeError: PASSWORD: ***"),
        (
            "connect https://user:pw@db.example.com/x",
            "RuntimeError: connect https://***@db.example.com/x",
        ),
        ("", "RuntimeError"),
    ],
    ids=["assignment", "colon", "url-credential", "empty"],
)
def test_operation_failure_hint_masks_secrets(message: str, expected: str) -> None:
    assert operation_failure_hint(RuntimeError(message)) == expected


def test_operation_failure_hint_is_none_for_oracle_errors_and_truncates() -> None:
    assert operation_failure_hint(RuntimeError("ORA-01031: insufficient privileges")) is None
    hint = operation_failure_hint(RuntimeError("x" * 1000)) or ""
    assert hint.endswith("…")
    assert len(hint) < 400


class _ProbeOracledb:
    """直接の接続の fake（#1744）。`error` を投げ、呼ばれた引数を残す。"""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def connect(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

        class _Connection:
            def close(self) -> None:
                return None

        return _Connection()


def test_operation_failure_fields_probe_real_cause_of_pool_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pool の時間切れ（DPY-4005）は、直接 1 回接続して本当の原因と対処を出す（#1744）。"""

    from pr_backend_core import oracle_connect_diagnosis

    fake = _ProbeOracledb(RuntimeError("ORA-28000: The account is locked. password=hunter22"))
    monkeypatch.setattr(oracle_connect_diagnosis, "_default_oracledb_loader", lambda: fake)
    kwargs = {"user": "APP", "password": "hunter22", "dsn": "db_high", "min": 1}

    fields = operation_failure_fields(
        RuntimeError("DPY-4005: timed out waiting for the connection pool to return a connection"),
        connect_kwargs=lambda: kwargs,
    )

    assert fields["code"] == "ORA-28000"
    assert fields["cause"] == "account_locked"
    assert "ロックされています" in fields["hint"]
    assert "ACCOUNT UNLOCK" in fields["hint"]
    assert "hunter22" not in str(fields)
    assert len(fake.calls) == 1
    assert fake.calls[0]["retry_count"] == 0
    assert "min" not in fake.calls[0]

    summary = deployment_check_error_summary(
        RuntimeError("DPY-4005: timed out waiting for the connection pool"),
        connect_kwargs=lambda: kwargs,
    )
    assert summary.startswith(
        "状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ORA-28000: DB のユーザーがロックされています"
    )
    assert "hunter22" not in summary


def test_operation_failure_fields_keep_ora_code_and_hint_for_other_failures() -> None:
    assert operation_failure_fields(RuntimeError("ORA-01031: insufficient privileges")) == {
        "code": "ORA-01031"
    }
    assert operation_failure_fields(ValueError("password=secret")) == {
        "code": "SCHEMA_OPERATION_FAILED",
        "hint": "ValueError: password=***",
    }


class _ColumnsManager(_DemoManager):
    managed_columns = {"DEMO_ITEMS": ("ITEM_ID", "PAYLOAD"), "DEMO_LOGS": ("LOG_ID",)}


def test_manager_missing_columns_reads_only_existing_tables() -> None:
    """骨格の列の比較（#2458）: 無い表は比べず、欠けた列だけを返す。製品は class 属性で渡す。"""

    executed: list[dict[str, Any]] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _sql: str, binds: dict[str, Any]) -> None:
            executed.append(binds)

        def fetchall(self) -> list[tuple[str, str]]:
            return [("DEMO_ITEMS", "ITEM_ID")]

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    manager = _ColumnsManager(_FakeDatabase())
    objects = {("DEMO_ITEMS", "TABLE"), ("DEMO_ITEMS_IDX", "INDEX")}

    assert manager._missing_columns(_Connection(), objects) == [
        {"table_name": "DEMO_ITEMS", "column_name": "PAYLOAD"}
    ]
    assert [list(binds.values()) for binds in executed] == [["DEMO_ITEMS"]]
    # 正本の列を渡さない製品は比べない（DB に問い合わせない）。
    assert _DemoManager(_FakeDatabase())._missing_columns(_Connection(), objects) == []
    assert len(executed) == 1


def test_require_columns_present_stops_with_column_names() -> None:
    SystemSchemaManagerBase._require_columns_present({"missing_columns": []})

    with pytest.raises(SystemSchemaError) as raised:
        SystemSchemaManagerBase._require_columns_present(
            {
                "missing_columns": [
                    {"table_name": "DEMO_ITEMS", "column_name": "PAYLOAD"},
                    {"table_name": "DEMO_ITEMS", "column_name": "MEMO"},
                ]
            }
        )

    assert raised.value.code == "SCHEMA_POSTCONDITION_FAILED"
    assert "DEMO_ITEMS.PAYLOAD, DEMO_ITEMS.MEMO" in raised.value.public_message
