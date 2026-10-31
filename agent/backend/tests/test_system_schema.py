"""Agent のシステムテーブル（#751）の決定論テスト。

Oracle の辞書（USER_OBJECTS / USER_TABLES / USER_CONSTRAINTS）・台帳・操作の lease を
fake で再現する。manager の状態の分類・作成・更新・全再作成、API の権限と契約、CLI、
DB ゲートの schema の確認を確かめる。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES
from pr_system_settings.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    ForeignKeySpec,
    columns_from_statements,
    foreign_keys_from_create_table,
)
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

import app.features.settings.system_tables as system_tables_api
from app.cli import agent_system_schema
from app.security.permissions import ALL_PERMISSION_CODES, permission_for_route
from app.security.service import set_security_service
from app.system_schema import (
    _RENAMED_SKILL_IDS_013,
    CONTROL_PLANE_STATEMENTS,
    CONTROL_STATEMENTS,
    CONTROL_TABLE,
    DOMAIN_TABLES,
    MANAGED_COLUMNS,
    MANAGED_FOREIGN_KEYS,
    MANAGED_OBJECTS,
    MANAGED_TABLES,
    MIGRATION_TABLE,
    MIGRATIONS,
    RECREATE_CONFIRMATION,
    RETIRED_MANAGED_OBJECTS,
    RETIRED_TABLES_APPROVAL,
    RUN_FACTS_DATA_TOOL_STATEMENTS,
    RUN_FACTS_SIGNAL_STATEMENTS,
    RUN_FACTS_STATEMENTS,
    RUNTIME_CHECKPOINT_VERSION_STATEMENTS,
    RUNTIME_STATEMENTS,
    SystemSchemaError,
    SystemSchemaManager,
    classify_system_schema_status,
    managed_manifest_from_schema,
)


class _FakeDatabase:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], datetime | None] = {}
        self.migrations: dict[str, str] = {}
        self.foreign_keys: dict[str, tuple[ForeignKeySpec, bool]] = {}
        self.operation: dict[str, Any] | None = None
        self.executed: list[str] = []
        # AGENT_ROLE_PERMISSIONS の行（ROLE_ID, PERMISSION_CODE）。
        self.role_permissions: set[tuple[str, str]] = set()
        # AGENT_CONTROL_PLANE_ITEMS の行（ITEM_KIND, ITEM_ID）。
        self.control_plane_items: set[tuple[str, str]] = set()
        # kind `automation` の行の ITEM_JSON（ITEM_ID → 定義。#1799 の migration 009 が読む）。
        self.automation_documents: dict[str, dict[str, Any]] = {}
        # 表 → 正本にあるが欠けている列（取り消した migration が改名・削除した列など。#2458）。
        self.removed_columns: dict[str, set[str]] = {}

    def table(self, name: str) -> None:
        self.objects[(name, "TABLE")] = datetime.now(UTC)

    @staticmethod
    def idle_operation() -> dict[str, Any]:
        return {
            "status": "IDLE",
            "operation_kind": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error_code": None,
            "schema_epoch": 0,
            "updated_at": datetime.now(UTC),
        }


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

    def execute(self, statement: str, parameters: dict[str, Any] | None = None) -> None:
        sql = re.sub(r"\s+", " ", statement).strip()
        upper = sql.upper()
        params = parameters or {}
        db = self.database
        self.rows = []
        self.rowcount = 0
        db.executed.append(upper)
        names = {str(value).upper() for value in params.values()}

        if upper.startswith("SELECT OBJECT_NAME, OBJECT_TYPE, CREATED FROM USER_OBJECTS"):
            self.rows = [
                (name, kind, created)
                for (name, kind), created in sorted(db.objects.items())
                if name in names
            ]
        elif upper.startswith(f"SELECT MIGRATION_NAME, CHECKSUM FROM {MIGRATION_TABLE}"):
            self.rows = sorted(db.migrations.items())
        elif upper.startswith("SELECT TABLE_NAME, NUM_ROWS, LAST_ANALYZED FROM USER_TABLES"):
            self.rows = [(n, 0, None) for n in sorted(names) if (n, "TABLE") in db.objects]
        elif upper.startswith("SELECT TABLE_NAME FROM USER_TABLES"):
            self.rows = [(n,) for n in sorted(names) if (n, "TABLE") in db.objects]
        elif upper.startswith("SELECT TABLE_NAME, COLUMN_NAME FROM USER_TAB_COLUMNS"):
            # 表は正本の列を持つ（`removed_columns` の列だけを欠く）。
            self.rows = [
                (n, column)
                for n in sorted(names)
                if (n, "TABLE") in db.objects
                for column in MANAGED_COLUMNS.get(n, ())
                if column not in db.removed_columns.get(n, set())
            ]
        elif upper.startswith("SELECT STATUS, OPERATION_KIND"):
            op = db.operation
            if op is not None:
                self.rows = [
                    (
                        op["status"],
                        op["operation_kind"],
                        op["lease_expires_at"],
                        op["last_error_code"],
                        op["schema_epoch"],
                        op["updated_at"],
                    )
                ]
        elif upper.startswith("SELECT CHILD.CONSTRAINT_NAME"):
            self.rows = [
                (
                    fk.name,
                    fk.table_name,
                    fk.referenced_table_name,
                    fk.delete_rule,
                    "ENABLED",
                    "VALIDATED" if validated else "NOT VALIDATED",
                    column,
                    referenced,
                )
                for fk, validated in sorted(db.foreign_keys.values(), key=lambda i: i[0].name)
                if fk.table_name in names
                for column, referenced in zip(fk.columns, fk.referenced_columns, strict=True)
            ]
        elif upper.startswith("CREATE TABLE"):
            name = upper.split()[2]
            if (name, "TABLE") in db.objects:
                raise RuntimeError("ORA-00955: name is already used by an existing object")
            db.table(name)
            for foreign_key in foreign_keys_from_create_table(statement):
                db.foreign_keys[foreign_key.name] = (foreign_key, True)
        elif (
            index := re.match(r"CREATE (?:UNIQUE )?INDEX ([A-Z][A-Z0-9_$#]*)", upper)
        ) is not None:
            name = index.group(1)
            if (name, "INDEX") in db.objects:
                raise RuntimeError("ORA-00955: name is already used by an existing object")
            db.objects[(name, "INDEX")] = datetime.now(UTC)
        elif upper.startswith("DROP INDEX"):
            db.objects.pop((upper.split()[2], "INDEX"), None)
        elif upper.startswith("DROP TABLE"):
            name = upper.split()[2]
            if (name, "TABLE") not in db.objects:
                raise RuntimeError("ORA-00942: table or view does not exist")
            db.objects.pop((name, "TABLE"))
            db.foreign_keys = {k: v for k, v in db.foreign_keys.items() if v[0].table_name != name}
            if name == MIGRATION_TABLE:
                db.migrations.clear()
        elif upper.startswith(f"INSERT INTO {CONTROL_TABLE}"):
            if db.operation is not None:
                raise RuntimeError("ORA-00001: unique constraint violated")
            db.operation = db.idle_operation()
        elif upper.startswith(f"MERGE INTO {MIGRATION_TABLE}"):
            db.migrations[str(params["migration_name"])] = str(params["checksum"])
        elif upper.startswith("DELETE FROM AGENT_ROLE_PERMISSIONS WHERE PERMISSION_CODE IN"):
            codes = set(re.findall(r"'([^']+)'", sql))
            matched = {row for row in db.role_permissions if row[1] in codes}
            db.role_permissions -= matched
            self.rowcount = len(matched)
        elif upper.startswith(
            "INSERT INTO AGENT_ROLE_PERMISSIONS (ROLE_ID, PERMISSION_CODE) SELECT"
        ):
            # 廃止したコードを持つロールに新しいコードを足す（既に持つロールには足さない）。
            new_code, *retired_codes, _ = re.findall(r"'([^']+)'", sql)
            added = {
                (role_id, new_code)
                for role_id, code in db.role_permissions
                if code in retired_codes
            } - db.role_permissions
            db.role_permissions |= added
            self.rowcount = len(added)
        elif upper.startswith(
            "DELETE FROM AGENT_CONTROL_PLANE_ITEMS WHERE ITEM_KIND = 'AUTOMATION' AND JSON_VALUE"
        ):
            # 指定のトリガーの自動実行の行を消す（#1799）。
            _kind, _path, trigger = re.findall(r"'([^']+)'", sql)
            removed = [
                item_id
                for item_id, document in db.automation_documents.items()
                if document.get("trigger") == trigger
            ]
            for item_id in removed:
                db.automation_documents.pop(item_id)
                db.control_plane_items.discard(("automation", item_id))
            self.rowcount = len(removed)
        elif upper.startswith("UPDATE AGENT_CONTROL_PLANE_ITEMS SET ITEM_JSON = JSON_TRANSFORM"):
            # 自動実行の定義の JSON から項目を除く（#1799。無い項目は無視する）。
            fields = re.findall(r"REMOVE '\$\.([a-z_]+)'", sql)
            for document in db.automation_documents.values():
                for field in fields:
                    document.pop(field, None)
            self.rowcount = len(db.automation_documents)
        elif upper.startswith("DELETE FROM AGENT_CONTROL_PLANE_ITEMS WHERE ITEM_KIND ="):
            (kind,) = re.findall(r"'([^']+)'", sql)
            matched_items = {row for row in db.control_plane_items if row[0] == kind}
            db.control_plane_items -= matched_items
            self.rowcount = len(matched_items)
        elif upper.startswith(f"UPDATE {CONTROL_TABLE}"):
            self._update_operation(upper, params)
        elif upper.startswith("ALTER TABLE") and " FOREIGN KEY " in upper:
            match = re.match(
                r"ALTER TABLE (\S+) ADD CONSTRAINT (\S+) FOREIGN KEY \(([^)]*)\) "
                r"REFERENCES (\S+) \(([^)]*)\)(?: ON DELETE (CASCADE|SET NULL))?",
                upper,
            )
            assert match is not None, upper
            table, name, columns, referenced, referenced_columns, rule = match.groups()
            spec = ForeignKeySpec(
                name=name,
                table_name=table,
                columns=tuple(item.strip() for item in columns.split(",")),
                referenced_table_name=referenced,
                referenced_columns=tuple(item.strip() for item in referenced_columns.split(",")),
                delete_rule=rule or "NO ACTION",
            )
            if any(fk.signature == spec.signature for fk, _ in db.foreign_keys.values()):
                raise RuntimeError("ORA-02275: such a referential constraint already exists")
            db.foreign_keys[name] = (spec, True)

    def _update_operation(self, upper: str, params: dict[str, Any]) -> None:
        op = self.database.operation
        if op is None:
            return
        now = datetime.now(UTC)
        owner = str(params.get("lease_owner") or "")
        if "SET STATUS = 'RUNNING'" in upper:
            if (
                op["status"] == "RUNNING"
                and op["lease_expires_at"]
                and op["lease_expires_at"] >= now
            ):
                return
            op.update(
                status="RUNNING",
                operation_kind=params["operation_kind"],
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=int(params["lease_seconds"])),
                last_error_code=None,
            )
            self.rowcount = 1
            return
        if op.get("lease_owner") != owner:
            return
        if "SET STATUS = 'IDLE'" in upper:
            if "SCHEMA_EPOCH = SCHEMA_EPOCH + 1" in upper:
                op["schema_epoch"] = int(op["schema_epoch"]) + 1
            op.update(status="IDLE", operation_kind=None, lease_owner=None, lease_expires_at=None)
        elif "SET STATUS = 'FAILED'" in upper:
            op.update(
                status="FAILED",
                operation_kind=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=params["error_code"],
            )
        else:
            op["lease_expires_at"] = now + timedelta(seconds=int(params["lease_seconds"]))
        self.rowcount = 1


class _FakeConnection:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.database)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _manager(database: _FakeDatabase) -> SystemSchemaManager:
    @contextmanager
    def factory() -> Iterator[_FakeConnection]:
        yield _FakeConnection(database)

    return SystemSchemaManager(factory)


def _legacy_database() -> _FakeDatabase:
    """#751 より前に `agent_security_migrate` で作った DB（台帳と制御テーブルが無い）。"""
    database = _FakeDatabase()
    for name in (
        *PLATFORM_AUTH_TABLES,
        "AGENT_ROLE_PERMISSIONS",
        "AGENT_ROLE_AGENTS",
        "AGENT_ROLE_BUSINESS_VIEWS",
    ):
        database.table(name)
    # 旧 CLI の CREATE TABLE も同じ FK（ON DELETE CASCADE）を持っていた。
    for foreign_key in MANAGED_FOREIGN_KEYS:
        database.foreign_keys[foreign_key.name] = (foreign_key, True)
    database.role_permissions = {
        ("role-a", "menu.dashboard"),
        ("role-a", "menu.runs"),
        # 配布元の機能の削除（#1798）で `menu.plugins` に置き換えるコード。
        ("role-a", "menu.plugin_marketplaces"),
        ("role-b", "menu.plugin_marketplaces"),
        ("role-b", "menu.plugins"),
        # 外部向けの MCP と API キーの削除（#1809）で消すコード。
        ("role-a", "menu.settings_api_keys"),
        # 「外観と証明書」の画面の削除（#19）で消すコード（migration 018）。
        ("role-b", "menu.settings_appearance"),
    }
    database.control_plane_items = {
        ("marketplace", "official"),
        ("plugin", "sample"),
        ("skill", "sample"),
        # 外部向けの MCP と API キーの削除（#1809）。API キーの行は migration 011 が消す。
        ("api_key", "key_1"),
        # 自動実行（#1799）。Webhook の行は migration 009 が消す。
        ("automation", "auto-webhook"),
        ("automation", "auto-schedule"),
    }
    database.automation_documents = {
        "auto-webhook": {
            "id": "auto-webhook",
            "trigger": "webhook",
            "schedule": None,
            "webhook_token_hash": "0" * 64,
            "webhook_token_prefix": "prwh_abcd",
        },
        "auto-schedule": {
            "id": "auto-schedule",
            "trigger": "schedule",
            "schedule": {"frequency": "daily"},
            "webhook_token_hash": None,
            "webhook_token_prefix": None,
        },
    }
    return database


# ---- manifest -----------------------------------------------------------------


def test_manifest_matches_ddl_and_excludes_shared_auth_tables() -> None:
    assert managed_manifest_from_schema() == set(MANAGED_OBJECTS)
    assert MANAGED_TABLES[:2] == (CONTROL_TABLE, MIGRATION_TABLE)
    assert {
        "AGENT_ROLE_PERMISSIONS",
        "AGENT_ROLE_AGENTS",
        # Run・業務 Agent の保存先と、画面で変えた定義（#764）。
        "AGENT_RUNTIME_CHECKPOINTS",
        # Run・業務 Agent の 1 件 = 1 行（#1939）。
        "AGENT_RUNTIME_ENTITIES",
        "AGENT_RUNTIME_RUNS",
        "AGENT_RUNTIME_EVENTS",
        "AGENT_RUNTIME_STEPS",
        "AGENT_RUNTIME_APPROVALS",
        "AGENT_RUNTIME_ARTIFACTS",
        "AGENT_CONTROL_PLANE_ITEMS",
        # 利用状況・フィードバックの集計に使う Run の事実（#794）。
        "AGENT_RUN_FACTS",
    } == DOMAIN_TABLES
    # 監査の検索に使う projection の索引（エラーコードは JSON_VALUE の関数索引）。
    assert any(
        "AGENT_RUNTIME_STEPS_ERROR_CODE_IX" in statement
        and "JSON_VALUE(TOOL_RESULT_JSON" in statement
        for statement in RUNTIME_STATEMENTS
    )
    # 既存の DB では Runtime repository が作っていたテーブルを、migration 006 で管理対象にする。
    migration_006 = next(item for item in MIGRATIONS if item.name.startswith("20261002_006"))
    # 006 の DDL は適用済みの checksum を変えないよう、版の列を足す前の形で固定する（#1916）。
    # 行の表（#1939）は 016 が作る（006 の DDL には入れない）。
    assert migration_006.statements[1:] == (*RUNTIME_STATEMENTS[3:], *CONTROL_PLANE_STATEMENTS)
    assert "VERSION_NO" not in migration_006.statements[0]
    assert migration_006.checksum == (
        "5443fab145f3619005f31c0734de4af53fc66cad101d59ad7588546f1ca9594a"
    )
    # checkpoint の版と最後に書いたプロセス（#1916）は列を足すだけ（データを消さない）で、最新の
    # 形の CREATE TABLE にもある。適用の後は動いている backend が checkpoint を読み直す。
    migration_015 = next(item for item in MIGRATIONS if item.name.startswith("20261015_015"))
    assert migration_015.statements == RUNTIME_CHECKPOINT_VERSION_STATEMENTS
    assert not migration_015.destructive
    assert migration_015.rewrites == frozenset({"runtime"})
    for column in ("VERSION_NO NUMBER(19) DEFAULT 0 NOT NULL", "WRITER_ID VARCHAR2(200)"):
        assert any(f"ADD ({column})" in statement for statement in migration_015.statements)
        assert column in RUNTIME_STATEMENTS[0]
    # Run・業務 Agent の 1 件 = 1 行（#1939）。全体の snapshot を行に移し、移す前の snapshot を
    # legacy_snapshot の行に残す（データを消さないので承認を求めない）。適用の後は読み直す。
    migration_016 = next(item for item in MIGRATIONS if item.name.startswith("20261015_016"))
    assert not migration_016.destructive
    assert migration_016.rewrites == frozenset({"runtime"})
    create_table, create_index, backup, move_runs, move_agents, header = migration_016.statements
    assert create_table.strip().startswith("CREATE TABLE AGENT_RUNTIME_ENTITIES")
    assert create_table in RUNTIME_STATEMENTS and create_index in RUNTIME_STATEMENTS
    assert "'legacy_snapshot'" in backup
    assert "'$.runs[*]'" in move_runs and "ITEM_RANK = 1" in move_runs
    assert "'$.agents[*]'" in move_agents
    assert "REMOVE '$.runs'" in header and "SET '$.storage' = 'entities'" in header
    for statement in (backup, move_runs, move_agents, header):
        # 移す前の形の行だけ（もう一度当てても変えない）。
        assert "JSON_VALUE(c.SNAPSHOT_JSON, '$.storage') IS NULL" in statement
    # 既存の DB には migration 007 で Run の事実のテーブルと索引を足す（#794）。
    migration_007 = next(item for item in MIGRATIONS if item.name.startswith("20261003_007"))
    # 007 の DDL は適用済みの checksum を変えないよう、列を足す前の形で固定する（#1822）。
    assert "CREATE TABLE AGENT_RUN_FACTS" in migration_007.statements[0]
    assert "VALIDATION_RESULT" not in migration_007.statements[0]
    assert migration_007.statements[1:] == RUN_FACTS_STATEMENTS[1:]
    assert migration_007.checksum == (
        "e68d96b47280b7fd320aaa2eca38504f1226909c942625994c458660ad486581"
    )
    assert not migration_007.destructive
    # 配布元の機能の削除（#1798）は読まれない設定を消すだけなので、承認を求めない。
    migration_008 = next(item for item in MIGRATIONS if item.name.startswith("20261014_008"))
    assert not migration_008.destructive
    # 自動実行のトリガーはスケジュールだけ（#1799）。migration 009 が Webhook の自動実行の行と、
    # 残る定義の JSON のトリガー・Webhook の秘密の項目を消す。
    migration_009 = next(item for item in MIGRATIONS if item.name.startswith("20261014_009"))
    delete_webhook, strip_fields = migration_009.statements
    assert "ITEM_KIND = 'automation'" in delete_webhook
    assert "JSON_VALUE(ITEM_JSON, '$.trigger') = 'webhook'" in delete_webhook
    for field in ("trigger", "webhook_token_hash", "webhook_token_prefix"):
        assert f"REMOVE '$.{field}'" in strip_fields
    assert not migration_009.destructive
    # プラグインの削除（#1803）は導入済みのプラグインを消すので、承認を求める。
    migration_010 = next(item for item in MIGRATIONS if item.name.startswith("20261014_010"))
    assert migration_010.destructive
    # 外部向けの MCP と API キーの削除（#1809）は使えないキーを消すだけなので、承認を求めない。
    migration_011 = next(item for item in MIGRATIONS if item.name.startswith("20261014_011"))
    assert not migration_011.destructive
    # 確かめるべき実行のシグナルの列（#1822）は列を足すだけ（データを消さない）で、最新の形の
    # CREATE TABLE にも同じ列がある（未初期化の DB は migration を実行せずに記録する）。
    # 業務データの道具の呼び出しの数（#1834）も列を足すだけで、最新の形の CREATE TABLE にもある。
    migration_014 = next(item for item in MIGRATIONS if item.name.startswith("20261014_014"))
    assert migration_014.statements == RUN_FACTS_DATA_TOOL_STATEMENTS
    assert not migration_014.destructive
    assert "ADD (DATA_TOOL_CALLS NUMBER(10))" in migration_014.statements[0]
    assert "DATA_TOOL_CALLS NUMBER(10)" in RUN_FACTS_STATEMENTS[0]
    migration_012 = next(item for item in MIGRATIONS if item.name.startswith("20261014_012"))
    assert migration_012.statements == RUN_FACTS_SIGNAL_STATEMENTS
    assert not migration_012.destructive
    for column in ("ANSWER_OUTCOME", "VALIDATION_RESULT", "TOOL_FAILURES", "APPROVALS_REJECTED"):
        assert any(f"ADD ({column} " in statement for statement in migration_012.statements)
        assert f"{column} " in RUN_FACTS_STATEMENTS[0]
    # 組み込みのスキルの ID の改名（#1816）。checkpoint の JSON の引用符つきの ID を置き換えるだけで
    # データを消さないので、承認を求めない。
    migration_013 = next(item for item in MIGRATIONS if item.name.startswith("20261014_013"))
    assert not migration_013.destructive
    [rename] = migration_013.statements
    assert rename.startswith("UPDATE AGENT_RUNTIME_CHECKPOINTS SET SNAPSHOT_JSON = REPLACE(")
    for old_id, new_id in _RENAMED_SKILL_IDS_013:
        assert f"""'"{old_id}"', '"{new_id}"'""" in rename
        assert f"""DBMS_LOB.INSTR(SNAPSHOT_JSON, '"{old_id}"') > 0""" in rename
    assert {
        "AGENT_RUN_FACTS_CREATED_IX",
        "AGENT_RUN_FACTS_AGENT_CREATED_IX",
        "AGENT_RUN_FACTS_RATED_IX",
    } <= {name for name, kind in MANAGED_OBJECTS if kind == "INDEX"}
    # 共通認証の表は管理対象にしない（全再作成でも RAG / NL2SQL のユーザー・ロールを消さない）。
    assert not set(PLATFORM_AUTH_TABLES) & set(MANAGED_TABLES)
    assert ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE") in RETIRED_MANAGED_OBJECTS
    assert {fk.name for fk in MANAGED_FOREIGN_KEYS} == {
        "FK_AGENT_ROLE_PERMISSIONS_ROLE",
        "FK_AGENT_ROLE_AGENTS_ROLE",
    }
    assert all(fk.delete_rule == "CASCADE" for fk in MANAGED_FOREIGN_KEYS)
    # 名前は AGENT_ で始める（root の docs/agents/naming.md）。
    assert all(name.startswith("AGENT_") for name, _ in MANAGED_OBJECTS)
    assert len({migration.name for migration in MIGRATIONS}) == len(MIGRATIONS)


def test_classify_status() -> None:
    applied = {migration.name: migration.checksum for migration in MIGRATIONS}
    everything = set(MANAGED_OBJECTS)
    assert classify_system_schema_status(set(), {}) == "missing"
    assert classify_system_schema_status({("AGENT_ROLE_AGENTS", "TABLE")}, applied) == "partial"
    assert classify_system_schema_status(everything, applied) == "ready"
    assert classify_system_schema_status(everything, {}) == "outdated"
    assert (
        classify_system_schema_status(everything | {RETIRED_MANAGED_OBJECTS[0]}, applied)
        == "outdated"
    )


# ---- manager ------------------------------------------------------------------


def test_initialize_fresh_database_creates_everything_and_is_idempotent() -> None:
    database = _FakeDatabase()
    manager = _manager(database)
    assert manager.status()["status"] == "missing"

    result = manager.initialize()

    assert result["operation"] == "initialized"
    assert result["status"] == "ready"
    assert result["pending_versions"] == []
    assert set(PLATFORM_AUTH_TABLES) <= {name for name, kind in database.objects if kind == "TABLE"}
    # 組み込み SYSTEM_ADMIN ロールを MERGE で用意する（ユーザーは作らない）。
    assert any(item.startswith("MERGE INTO PLATFORM_ROLES") for item in database.executed)
    assert not any("INSERT INTO PLATFORM_USERS" in item for item in database.executed)
    # PLATFORM_ROLES を先に作り、AGENT_ROLE_* はその後（FK の参照先）。
    creates = [item.split()[2] for item in database.executed if item.startswith("CREATE TABLE")]
    assert creates.index("PLATFORM_ROLES") < creates.index("AGENT_ROLE_PERMISSIONS")
    assert result["operation_state"]["schema_epoch"] == 1
    assert {item["name"] for item in result["objects"]} == {name for name, _ in MANAGED_OBJECTS}

    again = manager.initialize()
    assert again["operation"] == "no_op"
    assert again["operation_state"]["schema_epoch"] == 1


def test_legacy_database_requires_approval_then_migrates() -> None:
    """旧 CLI で作った DB は、検索・回答プロファイルの
    表の削除（#750 から未使用）を承認してから更新する。"""
    database = _legacy_database()
    manager = _manager(database)
    status = manager.status()
    assert status["status"] == "partial"
    # 台帳が無いので、廃止した表の削除も別に承認を求める（#1648）。
    assert [item["name"] for item in status["pending_destructive_migrations"]] == [
        "20261002_003_retire_role_business_views",
        "20261014_010_remove_plugins",
        RETIRED_TABLES_APPROVAL,
    ]
    assert status["retired_objects"] == [
        {"name": "AGENT_ROLE_BUSINESS_VIEWS", "object_type": "TABLE"}
    ]

    with pytest.raises(SystemSchemaError) as refused:
        manager.initialize()
    assert refused.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE") in database.objects

    result = manager.initialize(allow_destructive=True)
    assert result["operation"] == "migrated"
    assert result["status"] == "ready"
    assert ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE") not in database.objects
    assert ("AGENT_ROLE_AGENTS_AGENT_IDX", "INDEX") in database.objects
    # 廃止した権限コードだけを消す（配布元のメニュー権限は 008 が `menu.plugins` に引き継ぎ、
    # 010 がプラグインの機能の削除で消す。#1798 / #1803）。
    assert database.role_permissions == {("role-a", "menu.runs")}
    # 配布元（008）・Webhook の自動実行（009。#1799）・導入済みのプラグイン（010）・
    # API キー（011。#1809）の行を消し、ほかの定義は残す。残る自動実行の定義からトリガーと
    # Webhook の秘密の項目を除く。
    assert database.control_plane_items == {("skill", "sample"), ("automation", "auto-schedule")}
    assert database.automation_documents == {
        "auto-schedule": {"id": "auto-schedule", "schedule": {"frequency": "daily"}}
    }
    assert set(database.migrations) == {migration.name for migration in MIGRATIONS}


def test_retired_table_without_ledger_is_not_dropped_without_approval() -> None:
    """台帳の無い schema の廃止した表は、missing でも承認が無ければ消さない（#1648）。"""
    database = _FakeDatabase()
    database.table("AGENT_ROLE_BUSINESS_VIEWS")
    manager = _manager(database)

    status = manager.status()
    assert status["status"] == "missing"
    approvals = status["pending_destructive_migrations"]
    assert [item["name"] for item in approvals] == [RETIRED_TABLES_APPROVAL]
    assert "AGENT_ROLE_BUSINESS_VIEWS" in approvals[0]["description"]

    with pytest.raises(SystemSchemaError) as refused:
        manager.initialize()
    assert refused.value.code == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE") in database.objects
    assert not any(item.startswith("DROP") for item in database.executed)

    result = manager.initialize(allow_destructive=True)
    assert result["status"] == "ready"
    assert ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE") not in database.objects


def test_retired_table_with_ledger_is_dropped_as_agent_owned() -> None:
    """台帳に記録があれば Agent が作った表として、承認なしに消す（#1648）。"""
    database = _FakeDatabase()
    manager = _manager(database)
    manager.initialize()
    database.table("AGENT_ROLE_BUSINESS_VIEWS")

    status = manager.status()
    assert status["status"] == "outdated"
    assert status["pending_destructive_migrations"] == []
    assert manager.initialize()["status"] == "ready"
    assert ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE") not in database.objects


def test_recreate_requires_confirmation_and_keeps_shared_auth_tables() -> None:
    database = _FakeDatabase()
    manager = _manager(database)
    manager.initialize()
    with pytest.raises(SystemSchemaError):
        manager.initialize(recreate=True, confirmation="wrong")

    result = manager.initialize(recreate=True, confirmation=RECREATE_CONFIRMATION)

    assert result["operation"] == "recreated"
    assert result["status"] == "ready"
    dropped = [item.split()[2] for item in database.executed if item.startswith("DROP TABLE")]
    # 全再作成は Run の保存先・定義の表も消す（共通認証の表は残す）。
    assert set(dropped) == (set(DOMAIN_TABLES) | {MIGRATION_TABLE})
    assert not set(dropped) & set(PLATFORM_AUTH_TABLES)


def test_managed_columns_match_the_migration_path() -> None:
    """正本の最新の形の列は、migration を順に当てた後の列と同じ（#2458）。

    既存の DB は migration で、未初期化の DB は正本の DDL で作るので、どちらの経路でも状態の
    列の比較が同じ結果になる。
    """

    assert set(MANAGED_COLUMNS) == set(MANAGED_TABLES)
    assert "DATA_TOOL_CALLS" in MANAGED_COLUMNS["AGENT_RUN_FACTS"]
    assert {"VERSION_NO", "WRITER_ID"} <= set(MANAGED_COLUMNS["AGENT_RUNTIME_CHECKPOINTS"])
    migrated = columns_from_statements(
        (*CONTROL_STATEMENTS, *(statement for item in MIGRATIONS for statement in item.statements))
    )
    assert {name: set(columns) for name, columns in migrated.items()} == {
        name: set(columns) for name, columns in MANAGED_COLUMNS.items()
    }


def test_status_detects_missing_columns_even_when_ledger_is_current() -> None:
    """台帳がすべて適用済みでも、正本の列が欠けた表があれば ready にしない（#2458）。"""

    database = _FakeDatabase()
    manager = _manager(database)
    manager.initialize()
    assert manager.status()["missing_columns"] == []
    database.removed_columns = {"AGENT_RUN_FACTS": {"DATA_TOOL_CALLS"}}

    status = manager.status()

    assert status["status"] == "outdated"
    assert status["pending_versions"] == []
    assert status["missing_columns"] == [
        {"table_name": "AGENT_RUN_FACTS", "column_name": "DATA_TOOL_CALLS"}
    ]
    # 列を足す migration は台帳で適用済みなので、更新では直らない。列の名前を出して止める。
    with pytest.raises(SystemSchemaError) as raised:
        manager.initialize()
    assert raised.value.code == "SCHEMA_POSTCONDITION_FAILED"
    assert "AGENT_RUN_FACTS.DATA_TOOL_CALLS" in raised.value.public_message


# ---- API・DB ゲート・CLI --------------------------------------------------------


@pytest.fixture
def fake_manager(monkeypatch: MonkeyPatch) -> SystemSchemaManager:
    manager = _manager(_legacy_database())
    monkeypatch.setattr(system_tables_api, "system_schema_manager", manager)
    import app.features.agent.router as agent_router

    monkeypatch.setattr(agent_router, "system_schema_manager", manager)
    return manager


def test_system_tables_routes_require_menu_permission() -> None:
    assert permission_for_route("GET", "/settings/database/system-tables") == frozenset(
        {"menu.settings_system_tables"}
    )
    assert permission_for_route("POST", "/settings/database/system-tables/initialize") == (
        frozenset({"menu.settings_system_tables"})
    )
    assert "menu.settings_system_tables" in ALL_PERMISSION_CODES


def test_system_tables_api_status_and_initialize(fake_manager: SystemSchemaManager) -> None:
    status = client.get("/api/settings/database/system-tables")
    assert status.status_code == 200, status.text
    assert status.json()["data"]["status"] == "partial"

    refused = client.post("/api/settings/database/system-tables/initialize", json={})
    assert refused.status_code == 409
    assert refused.json()["error_code"] == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED

    done = client.post(
        "/api/settings/database/system-tables/initialize", json={"allow_destructive": True}
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "ready"
    assert done.json()["data"]["operation"] == "migrated"


def test_system_tables_api_returns_missing_columns(monkeypatch: MonkeyPatch) -> None:
    database = _FakeDatabase()
    manager = _manager(database)
    manager.initialize()
    database.removed_columns = {"AGENT_RUNTIME_CHECKPOINTS": {"WRITER_ID"}}
    monkeypatch.setattr(system_tables_api, "system_schema_manager", manager)

    status = client.get("/api/settings/database/system-tables")

    assert status.status_code == 200, status.text
    data = status.json()["data"]
    assert data["status"] == "outdated"
    assert data["missing_columns"] == [
        {"table_name": "AGENT_RUNTIME_CHECKPOINTS", "column_name": "WRITER_ID"}
    ]


def test_system_tables_api_is_denied_without_permission(
    monkeypatch: MonkeyPatch, fake_manager: SystemSchemaManager
) -> None:
    auth: ProductionAuth = enable_production_auth(monkeypatch)
    try:
        auth.user_with_permissions("runs-only", ["agent.runs.view"])
        headers = login("runs-only")
        response = client.get("/api/settings/database/system-tables", headers=headers)
        assert response.status_code == 403
        auth.user_with_permissions("tables-admin", ["menu.settings_system_tables"])
        allowed = client.get("/api/settings/database/system-tables", headers=login("tables-admin"))
        assert allowed.status_code == 200, allowed.text
    finally:
        set_security_service(None)


def test_database_gate_reports_setup_required_until_ready(
    monkeypatch: MonkeyPatch, fake_manager: SystemSchemaManager
) -> None:
    import pr_system_settings.database_status as database_status

    import app.features.agent.router as agent_router

    # 接続設定の判定と接続確認は通ったことにして、schema の確認だけを見る。
    monkeypatch.setattr(database_status, "database_readiness", lambda *_args: "ok")

    async def connected(_settings: object) -> None:
        return None

    monkeypatch.setattr(agent_router, "_test_database_status_connection", connected)

    # local でも短絡せず、システムテーブルの状態を確かめる（RAG と同じ。#751）。
    before = client.get("/api/ready/database").json()["data"]
    assert before["status"] == "setup_required"
    fake_manager.initialize(allow_destructive=True)
    after = client.get("/api/ready/database").json()["data"]
    assert after["status"] == "ok"


def test_cli_status_and_initialize(
    monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = _manager(_legacy_database())
    monkeypatch.setattr(agent_system_schema, "system_schema_manager", manager)

    assert agent_system_schema.main(["--status"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "partial"
    assert agent_system_schema.main(["--initialize"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"]["code"] == DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED
    assert agent_system_schema.main(["--initialize", "--allow-destructive"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "ready"

    class _Broken:
        def status(self) -> dict[str, Any]:
            raise RuntimeError("DPY-6005: cannot connect password=secret")

    monkeypatch.setattr(agent_system_schema, "system_schema_manager", _Broken())
    assert agent_system_schema.main(["--status"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    # DB に接続できない失敗は、本当のコードと、理由・対処を日本語で出す（#1744）。
    error = json.loads(output)["error"]
    assert error["code"] == "DPY-6005"
    assert error["cause"] == "unreachable"
    assert error["hint"].startswith("ADB が停止しているか、この接続元から DB に届きません。")
    assert error["detail"] == "DPY-6005: cannot connect password=***"


def test_cli_check_returns_zero_only_when_ready(
    monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配備の確認（#1648）: ready なら 0、それ以外（読めないときも）は 1 と 1 行の要約。"""
    database = _FakeDatabase()
    manager = _manager(database)
    monkeypatch.setattr(agent_system_schema, "system_schema_manager", manager)

    assert agent_system_schema.main(["--check"]) == 1
    assert capsys.readouterr().out.strip() == "status=missing"
    manager.initialize()
    assert agent_system_schema.main(["--check"]) == 0
    assert capsys.readouterr().out.strip() == "status=ready"

    class _Denied:
        def status(self) -> dict[str, Any]:
            raise RuntimeError("ORA-01031: insufficient privileges password=secret")

        def initialize(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("ORA-01031: insufficient privileges password=secret")

    monkeypatch.setattr(agent_system_schema, "system_schema_manager", _Denied())
    assert agent_system_schema.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "ORA-01031" in output
    assert "secret" not in output
    assert agent_system_schema.main(["--initialize"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "ORA-01031"
    assert "secret" not in output


def test_cli_reports_real_cause_of_pool_timeout_without_secrets(
    monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pool の時間切れ（DPY-4005）は、直接 1 回だけ接続して本当の原因を出す（#1744）。"""
    from pr_backend_core import oracle_connect_diagnosis

    calls: list[dict[str, Any]] = []

    class _Oracledb:
        def connect(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise RuntimeError("ORA-01017: invalid credential or not authorized; logon denied")

    class _Timeout:
        def status(self) -> dict[str, Any]:
            raise RuntimeError("DPY-4005: timed out waiting for the connection pool")

        def initialize(self, **_kwargs: Any) -> dict[str, Any]:
            return self.status()

    monkeypatch.setattr(oracle_connect_diagnosis, "_default_oracledb_loader", lambda: _Oracledb())
    monkeypatch.setattr(
        agent_system_schema,
        "_connect_kwargs",
        lambda: {"user": "APP", "password": "Pa55-word-x", "dsn": "db_high"},
    )
    monkeypatch.setattr(agent_system_schema, "system_schema_manager", _Timeout())

    assert agent_system_schema.main(["--initialize"]) == 1
    output = capsys.readouterr().out
    error = json.loads(output)["error"]
    assert error["code"] == "ORA-01017"
    assert error["cause"] == "invalid_credentials"
    assert "password が違います" in error["hint"]
    assert "Pa55-word-x" not in output
    assert len(calls) == 1 and calls[0]["retry_count"] == 0

    assert agent_system_schema.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert output.startswith(
        "状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ORA-01017: DB のユーザー名か password が"
    )
    assert "Pa55-word-x" not in output


@pytest.mark.parametrize("argv", [["--status"], ["--check"]], ids=["status", "check"])
def test_cli_connects_without_tns_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """CLI は TNS の再試行を外した接続で DB に繋ぐ（DB の停止中に長く待たない。#1779）。"""
    from pr_backend_core.oracle_dsn import cli_connect_mode_enabled

    from app.cli import agent_system_schema

    seen: list[bool] = []

    class _Manager:
        def status(self) -> dict[str, Any]:
            seen.append(cli_connect_mode_enabled())
            raise RuntimeError("ORA-01031: insufficient privileges")

    assert not cli_connect_mode_enabled()
    monkeypatch.setattr(agent_system_schema, "system_schema_manager", _Manager())
    assert agent_system_schema.main(argv) == 1
    capsys.readouterr()
    assert seen == [True]


def test_builtin_skill_rename_migration_rewrites_saved_agents() -> None:
    """migration 013 の置き換え（REPLACE と同じ文字列の置き換え）で、保存済みの業務 Agent の下書きと
    各版の `skill_ids` が新しい ID になり、重なった ID は 1 つになる（#1816）。"""
    from app.features.agent.runtime import AgentProfile
    from app.features.agent.skills import skill_registry

    saved = {
        "id": "agent-1816",
        "name": "保存済みの業務 Agent",
        "skill_ids": [
            "rag_then_structured_data",
            "rag_and_structured_data",
            "business_rag_research",
        ],
        "versioned": True,
        "published_version": 1,
        "versions": [
            {"version": 1, "name": "v1", "skill_ids": ["structured_data_query"]},
        ],
        # 質問の文の中の語（引用符で囲んだ JSON の値ではない）は変えない。
        "description": "business_rag_research を使う",
    }
    snapshot = json.dumps({"agents": [saved]}, ensure_ascii=False)
    for old_id, new_id in _RENAMED_SKILL_IDS_013:
        snapshot = snapshot.replace(f'"{old_id}"', f'"{new_id}"')

    agent = AgentProfile.model_validate(json.loads(snapshot)["agents"][0])

    assert agent.skill_ids == ["rag-and-structured-data", "business-rag-research"]
    assert agent.versions[0].skill_ids == ["structured-data-query"]
    assert agent.description == "business_rag_research を使う"
    assert all(skill_registry.get(skill_id) is not None for skill_id in agent.skill_ids)
