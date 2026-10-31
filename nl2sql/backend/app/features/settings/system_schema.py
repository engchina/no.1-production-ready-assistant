"""NL2SQL の versioned system table を明示操作する共有 manager。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pr_system_settings.system_schema import (
    IGNORED_APPLY_CODES,
    IGNORED_DROP_CODES,
    SystemSchemaBusyError,
    SystemSchemaError,
    SystemSchemaManagerBase,
    SystemSchemaStatus,
    bind_list,
    columns_from_statements,
    iso_timestamp,
    oracle_error_code,
)
from pr_system_settings.system_schema import (
    SystemSchemaActiveJobsError as _SharedActiveJobsError,
)
from pr_system_settings.system_schema import (
    classify_system_schema_status as classify_schema_status,
)

from app.clients.oracle_runtime import get_oracle_pool_manager
from app.features.nl2sql.object_identity import qualified_object_name
from app.settings import get_settings

logger = logging.getLogger(__name__)

RECREATE_CONFIRMATION = "RECREATE_NL2SQL_SYSTEM_TABLES"
CONTROL_TABLE = "NL2SQL_SCHEMA_OPERATIONS"
MIGRATION_TABLE = "NL2SQL_SCHEMA_MIGRATIONS"

# DROP で読み飛ばす Oracle のエラーに、sequence が無い（ORA-02289）を足す。
_IGNORED_DROP_CODES = IGNORED_DROP_CODES | {"ORA-02289"}
_ACTIVE_JOB_STATES = ("ACCEPTED", "PENDING", "QUEUED", "RUNNING", "PROCESSING")
_CREATE_OBJECT_PATTERN = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+|UNIQUE\s+|VECTOR\s+)?"
    r"(TABLE|INDEX|SEQUENCE|PACKAGE(?:\s+BODY)?)\s+([A-Z][A-Z0-9_$#]*)",
    flags=re.IGNORECASE,
)
_ADD_CONSTRAINT_PATTERN = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Z][A-Z0-9_$#]*)\s+" r"ADD\s+CONSTRAINT\s+([A-Z][A-Z0-9_$#]*)\b",
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class MigrationArtifact:
    version: int
    filename: str
    description: str

    @property
    def path(self) -> Path:
        return Path(__file__).resolve().parents[3] / "migrations" / self.filename

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    @property
    def created_objects(self) -> frozenset[tuple[str, str]]:
        """この artifact が所有する CREATE object を決定論的に抽出する。"""

        return frozenset(
            (name.upper(), object_type.upper())
            for object_type, name in _CREATE_OBJECT_PATTERN.findall(
                self.path.read_text(encoding="utf-8")
            )
        )


@dataclass(frozen=True, slots=True)
class ManagedForeignKey:
    """再試行時に定義まで検証する system schema の外部キー。"""

    name: str
    table_name: str
    columns: tuple[str, ...]
    referenced_table_name: str
    referenced_columns: tuple[str, ...]
    delete_rule: str = "CASCADE"


MIGRATIONS: tuple[MigrationArtifact, ...] = (
    MigrationArtifact(0, "000_system_schema_control.sql", "system schema control"),
    MigrationArtifact(1, "001_ontology_store.sql", "ontology store"),
    MigrationArtifact(2, "002_ontology_semantics.sql", "ontology semantics"),
    MigrationArtifact(3, "003_incremental_nl2sql_state.sql", "incremental nl2sql state"),
    MigrationArtifact(
        5,
        "005_feedback_classifier_learning.sql",
        "feedback classifier learning metadata",
    ),
    MigrationArtifact(6, "006_incremental_job_leases.sql", "incremental job leases"),
    MigrationArtifact(
        7,
        "007_profile_ontology_lifecycle.sql",
        "profile ontology lifecycle integrity",
    ),
    MigrationArtifact(
        8,
        "008_quality_evaluation_jobs.sql",
        "durable NL2SQL quality evaluation jobs",
    ),
    MigrationArtifact(
        9,
        "009_rbac_permission_cleanup.sql",
        "RBAC permission cleanup for appearance settings",
    ),
    MigrationArtifact(
        15,
        "015_remove_ontology_rdf_network.sql",
        "remove retired ontology semantic staging objects",
    ),
    MigrationArtifact(
        17,
        "017_quality_evaluation_cancelled_status.sql",
        "allow cancelled quality evaluation jobs",
    ),
    MigrationArtifact(
        18,
        "018_unique_profile_names.sql",
        "case-insensitive unique NL2SQL profile names",
    ),
    MigrationArtifact(19, "019_synthetic_runs.sql", "durable synthetic data runs"),
    MigrationArtifact(
        21, "021_profile_ontology_revisions.sql", "profile-owned ontology publication"
    ),
    MigrationArtifact(
        22, "022_ontology_capability_transaction.sql", "atomic capability execution audit"
    ),
    MigrationArtifact(23, "023_admin_audit_log.sql", "durable DB admin execution audit"),
    MigrationArtifact(
        24, "024_db_admin_chat_conversations.sql", "DB admin chat conversation history"
    ),
)

# DROP 対象は必ずこの manifest に明記する。NL2SQL_* の prefix scan は使用しない。
MANAGED_TABLES: tuple[str, ...] = (
    CONTROL_TABLE,
    MIGRATION_TABLE,
    "NL2SQL_ONTOLOGY_REVISIONS",
    "NL2SQL_ONTOLOGY_NODES",
    "NL2SQL_ONTOLOGY_EDGES",
    "NL2SQL_ONTOLOGY_PROFILE_VIEWS",
    "NL2SQL_ONTOLOGY_QUERY_SESSIONS",
    "NL2SQL_ONTOLOGY_ARTIFACTS",
    "NL2SQL_ONTOLOGY_PROPOSALS",
    "NL2SQL_ONTOLOGY_IDEMPOTENCY",
    "NL2SQL_ONTOLOGY_SOURCE_DOCS",
    "NL2SQL_ONTOLOGY_JOBS",
    "NL2SQL_ONTOLOGY_RECOMMENDATIONS",
    "NL2SQL_CHANGE_TOKENS",
    "NL2SQL_PROFILES",
    "NL2SQL_SCHEMA_CATALOG_HEAD",
    "NL2SQL_SCHEMA_OBJECTS",
    "NL2SQL_SCHEMA_COLUMNS",
    "NL2SQL_SCHEMA_CONSTRAINTS",
    "NL2SQL_SCHEMA_DEPENDENCIES",
    "NL2SQL_SCHEMA_SAMPLES",
    "NL2SQL_SCHEMA_REFRESH_JOBS",
    "NL2SQL_STATE_DOCUMENTS",
    "NL2SQL_MIGRATION_OUTBOX",
    "NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS",
    "NL2SQL_EVALUATION_JOBS",
    "NL2SQL_EVALUATION_RESULTS",
    "NL2SQL_SYNTHETIC_LOCKS",
    "NL2SQL_SYNTHETIC_RUNS",
    "NL2SQL_ADMIN_AUDIT_LOG",
    "NL2SQL_DB_ADMIN_CHATS",
    "NL2SQL_DB_ADMIN_CHAT_TURNS",
)

MANAGED_INDEXES: tuple[str, ...] = (
    "IX_NL2SQL_ONT_NODE_PHYSICAL",
    "IX_NL2SQL_ONT_NODE_EMBED",
    "IX_NL2SQL_ONT_EDGE_SOURCE",
    "IX_NL2SQL_ONT_EDGE_TARGET",
    "IX_NL2SQL_ONT_SESSION_PROFILE",
    "IX_NL2SQL_ONT_ART_SESSION",
    "IX_NL2SQL_ONT_PROP_SESSION",
    "IX_NL2SQL_ONT_IDEMPOTENCY_RESOURCE",
    "IX_NL2SQL_ONT_SOURCE_PROFILE",
    "IX_NL2SQL_ONT_JOB_STATE",
    "IX_NL2SQL_ONT_REC_QUESTION",
    "UX_NL2SQL_ONT_ONE_PUBLISHED",
    "IX_NL2SQL_PROFILES_LIST",
    "IX_NL2SQL_SCHEMA_OBJECT_LIST",
    "IX_NL2SQL_SCHEMA_COLUMN_SEARCH",
    "IX_NL2SQL_SCHEMA_REFRESH_STATE",
    "IX_NL2SQL_STATE_DOCUMENT_LIST",
    "UX_NL2SQL_MIGRATION_OUTBOX_VERSION",
    "IX_NL2SQL_ONT_VIEW_REVISION",
    "IX_NL2SQL_ONT_PROPOSAL_PROFILE",
    "IX_NL2SQL_SCHEMA_REFRESH_LEASE",
    "IX_NL2SQL_EVAL_JOB_STATE",
    "IX_NL2SQL_EVAL_JOB_LEASE",
    "UX_NL2SQL_PROFILES_NAME",
    "IX_NL2SQL_SYNTHETIC_STATE",
    "IX_NL2SQL_ADMIN_AUDIT_TIME",
    "IX_NL2SQL_ADMIN_AUDIT_ACTOR",
    "IX_NL2SQL_DB_ADMIN_CHAT_OWNER",
    "IX_NL2SQL_DB_ADMIN_CHAT_TIME",
    "IX_NL2SQL_DB_ADMIN_CHAT_TURN_CONV",
)

MANAGED_SEQUENCES: tuple[str, ...] = ("NL2SQL_MIGRATION_SNAPSHOT_SEQ",)

MANAGED_PACKAGES: tuple[str, ...] = ("NL2SQL_ONT_ACTION_TX",)

MANAGED_OBJECTS: tuple[tuple[str, str], ...] = (
    *((name, "TABLE") for name in MANAGED_TABLES),
    *((name, "INDEX") for name in MANAGED_INDEXES),
    *((name, "SEQUENCE") for name in MANAGED_SEQUENCES),
    *((name, "PACKAGE") for name in MANAGED_PACKAGES),
    *((name, "PACKAGE BODY") for name in MANAGED_PACKAGES),
)
DOMAIN_TABLES = frozenset(MANAGED_TABLES) - {CONTROL_TABLE, MIGRATION_TABLE}

# Explicitly preserved even though some names share the NL2SQL namespace.
# PLATFORM_* は 3 製品で共有する認証テーブル（#212）。NL2SQL のシステムテーブル再作成で消さない。
PRESERVED_TABLES = frozenset(
    {
        "PLATFORM_TRACE_RECORDING_SETTINGS",
        "PLATFORM_USERS",
        "PLATFORM_ROLES",
        "PLATFORM_USER_ROLES",
        "PLATFORM_AUTH_SESSIONS",
        "PLATFORM_LOGIN_ATTEMPTS",
        "PLATFORM_API_KEYS",
        "PLATFORM_ANSWER_NOTICE",
        # 会話の添付（#2517）。
        "PLATFORM_CHAT_ATTACHMENTS",
        "PLATFORM_CHAT_ATTACHMENT_SETTINGS",
        # 処理の経過の span の表（#3226）。
        "PLATFORM_TRACE_SPANS",
        "NL2SQL_APP_ROLE_PERMISSIONS",
        "NL2SQL_APP_ROLE_PROFILES",
        "NL2SQL_APP_DATA_ENTITLEMENTS",
        "NL2SQL_DEEPSEC_MIGRATIONS",
        "NL2SQL_FEEDBACK_VECTORS",
    }
)

MANAGED_FOREIGN_KEYS: tuple[ManagedForeignKey, ...] = (
    ManagedForeignKey(
        name="FK_NL2SQL_ONT_VIEW_REV_PROFILE",
        table_name="NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS",
        columns=("PROFILE_ID",),
        referenced_table_name="NL2SQL_PROFILES",
        referenced_columns=("PROFILE_ID",),
    ),
    ManagedForeignKey(
        name="FK_NL2SQL_ONT_VIEW_PROFILE",
        table_name="NL2SQL_ONTOLOGY_PROFILE_VIEWS",
        columns=("PROFILE_ID",),
        referenced_table_name="NL2SQL_PROFILES",
        referenced_columns=("PROFILE_ID",),
    ),
    ManagedForeignKey(
        name="FK_NL2SQL_SYNTHETIC_RUN",
        table_name="NL2SQL_SYNTHETIC_LOCKS",
        columns=("RUN_ID",),
        referenced_table_name="NL2SQL_SYNTHETIC_RUNS",
        referenced_columns=("RUN_ID",),
    ),
)
_MANAGED_FOREIGN_KEYS_BY_NAME = {constraint.name: constraint for constraint in MANAGED_FOREIGN_KEYS}


class SystemSchemaActiveJobsError(_SharedActiveJobsError):
    def __init__(self) -> None:
        super().__init__(
            "実行中の schema refresh、Ontology、または SQL生成評価 job があります。"
            "完了または停止してから再実行してください。"
        )


def split_migration_sql(sql: str) -> list[str]:
    """現在の versioned migration（単純 SQL）を comment 除外して分割する。"""

    statements: list[str] = []
    current: list[str] = []
    in_plsql = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped == "/":
            statement = "\n".join(current).strip()
            if statement:
                statements.append(
                    statement if in_plsql else _strip_sql_statement_terminator(statement)
                )
            current = []
            in_plsql = False
            continue
        if not current:
            in_plsql = _is_plsql_block_start(stripped)
        current.append(line)
        statement = "\n".join(current).strip()
        if in_plsql:
            if not re.match(r"^CREATE\s", statement, re.IGNORECASE) and _plsql_block_complete(
                statement
            ):
                statements.append(statement)
                current = []
                in_plsql = False
        elif stripped.endswith(";"):
            statement = _strip_sql_statement_terminator(statement)
            if statement:
                statements.append(statement)
            current = []
    if current:
        statement = "\n".join(current).strip()
        if statement:
            statements.append(statement if in_plsql else _strip_sql_statement_terminator(statement))
    return statements


def _strip_sql_statement_terminator(statement: str) -> str:
    """Oracle driver に渡す通常 SQL から SQL*Plus 風の終端だけを外す。"""

    stripped = statement.rstrip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return stripped.strip()


def _is_plsql_block_start(stripped_line: str) -> bool:
    return (
        re.match(
            r"^(BEGIN|DECLARE|CREATE\s+(?:OR\s+REPLACE\s+)?PACKAGE)\b",
            stripped_line,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _plsql_block_complete(statement: str) -> bool:
    """PL/SQL anonymous block の最外層 END; まで待つ。"""

    sql = _blank_sql_string_literals(statement)
    if (
        re.search(
            r"\bEND\b(?!\s+(?:IF|LOOP|CASE)\b)(?:\s+[A-Z][A-Z0-9_$#]*)?\s*;\s*$",
            sql,
            flags=re.IGNORECASE,
        )
        is None
    ):
        return False
    begin_count = len(re.findall(r"\bBEGIN\b", sql, flags=re.IGNORECASE))
    end_count = len(
        re.findall(
            r"\bEND\b(?!\s+(?:IF|LOOP|CASE)\b)",
            sql,
            flags=re.IGNORECASE,
        )
    )
    return begin_count > 0 and end_count >= begin_count


def _blank_sql_string_literals(statement: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    while index < len(statement):
        char = statement[index]
        if char == "'":
            result.append(" ")
            if in_string and index + 1 < len(statement) and statement[index + 1] == "'":
                result.append(" ")
                index += 2
                continue
            in_string = not in_string
        elif in_string:
            result.append(" ")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _object_owner_fields(owner: str, name: str) -> dict[str, str]:
    """表示用の所有者と `OWNER.OBJECT`。owner が不明なら名前だけにし、推測で補わない。"""

    if not owner:
        return {"owner": "", "qualified_name": ""}
    return {"owner": owner, "qualified_name": qualified_object_name(owner, name)}


def managed_columns_from_migrations() -> dict[str, tuple[str, ...]]:
    """migration を順に当てた後の管理対象の表の列（表名 → 列名。#2458）。

    NL2SQL の DDL の正本は版ごとの SQL で、後の版が `ALTER TABLE … ADD` で列を足す。表の作成は
    「無ければ作る」ので、台帳が適用済みでも既存の表に列が欠けることがある。状態の確認でこの一覧と
    USER_TAB_COLUMNS を比べ、欠けた列があれば ready にしない。
    """

    columns = columns_from_statements(
        statement
        for migration in MIGRATIONS
        for statement in split_migration_sql(migration.path.read_text(encoding="utf-8"))
    )
    return {name: columns[name] for name in MANAGED_TABLES if name in columns}


MANAGED_COLUMNS: dict[str, tuple[str, ...]] = managed_columns_from_migrations()


def classify_system_schema_status(
    objects: set[tuple[str, str]],
    applied_checksums: dict[int, str],
    *,
    columns_current: bool = True,
) -> SystemSchemaStatus:
    """Dictionary / ledger snapshot を四つの公開状態へ決定論的に分類する（規則は platform）。"""

    return classify_schema_status(
        objects,
        domain_tables=DOMAIN_TABLES,
        managed_objects=MANAGED_OBJECTS,
        migrations_current=all(
            applied_checksums.get(migration.version) == migration.checksum
            for migration in MIGRATIONS
        ),
        columns_current=columns_current,
    )


class SystemSchemaManager(SystemSchemaManagerBase):
    """NL2SQL の migration・manifest・外部キーの検証を、platform の骨格（lease・台帳）に渡す。"""

    control_table = CONTROL_TABLE
    migration_table = MIGRATION_TABLE
    migration_key_column = "VERSION_NO"
    managed_tables = MANAGED_TABLES
    managed_columns = MANAGED_COLUMNS
    recreate_confirmation = RECREATE_CONFIRMATION
    log_prefix = "nl2sql"
    lock_timeout_guidance = (
        "実行中の schema refresh、Ontology、SQL生成評価 job を完了または停止してから、"
    )
    ignored_drop_codes = _IGNORED_DROP_CODES
    logger = logger

    def __init__(
        self,
        connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        *,
        lease_seconds: int = 900,
        ddl_lock_timeout_seconds: int | None = None,
    ) -> None:
        super().__init__(
            connection_factory or self._default_connection,
            lease_seconds=lease_seconds,
            ddl_lock_timeout_seconds=(
                get_settings().nl2sql_system_schema_ddl_lock_timeout_seconds
                if ddl_lock_timeout_seconds is None
                else ddl_lock_timeout_seconds
            ),
        )

    @staticmethod
    @contextmanager
    def _default_connection() -> Iterator[Any]:
        with get_oracle_pool_manager().control_connection() as connection:
            yield connection

    def _migration_key(self, value: Any) -> int:
        return int(value)

    def _table_identity_fields(self, name: str, owner: str) -> dict[str, Any]:
        return _object_owner_fields(owner, name)

    def current_epoch(self) -> int | None:
        """別 replica の cache invalidation 用 epoch。control table 未作成時は None。"""

        with self._connection_factory() as connection:
            objects = self._load_objects(connection)
            if (CONTROL_TABLE, "TABLE") not in objects:
                return None
            operation = self._load_operation(connection)
            return int(operation["schema_epoch"])

    def _initialize_on(self, connection: Any, owner: str, *, recreate: bool) -> dict[str, Any]:
        applied_versions: list[int] = []
        dropped_count = 0
        before = self._status_on(connection)
        if before["status"] == "ready" and not recreate:
            result = self._no_op_result(connection, owner)
            self._purge_orphan_ontology_records(connection)
            return result
        self._configure_ddl_lock_timeout(connection)
        if recreate:
            self._assert_no_active_jobs(connection)
            dropped_count = self._drop_managed_objects(connection, owner)
            migrations_to_apply = list(MIGRATIONS)
        else:
            migrations_to_apply = self._plan_migrations(before)
        for migration in migrations_to_apply:
            self._heartbeat(connection, owner)
            self._apply_migration(connection, migration)
            applied_versions.append(migration.version)
            self._heartbeat(connection, owner)
        self._finish_operation(connection, owner, increment_epoch=True)
        after = self._status_on(connection)
        self._require_columns_present(after)
        if after["status"] != "ready":
            raise SystemSchemaError(
                "SCHEMA_POSTCONDITION_FAILED",
                "システムテーブル操作は完了しましたが、必須オブジェクトが不足しています。状態を再取得して再試行してください。",
            )
        if not recreate:
            # 全再作成の後は表が空のため、掃除は要らない。
            self._purge_orphan_ontology_records(connection)
        operation = self._operation_name(before["status"], recreate=recreate)
        self._log_operation_succeeded(
            operation,
            after,
            dropped_object_count=dropped_count,
            applied_versions=applied_versions,
        )
        return {
            **after,
            "operation": operation,
            "dropped_object_count": dropped_count,
            "created_object_count": (
                int(after["existing_object_count"]) - 1
                if recreate
                else max(
                    0,
                    int(after["existing_object_count"]) - int(before["existing_object_count"]),
                )
            ),
        }

    def _purge_orphan_ontology_records(self, connection: Any) -> None:
        """存在しない業務プロファイルのオントロジーの成果物・終わったジョブの記録を消す（#1758）。

        業務プロファイルの削除のときに消せなかったもの（削除が途中で失敗した・#1743 より前に
        削除した業務プロファイルの残り）を、冪等な初期化（画面の「作成・更新」と配備の
        `--initialize`）のたびに片づける。実行中のジョブ・全体のジョブ・所有者の無い成果物・他の
        製品の表には触らない。system table は ready の後なので表はそろっている。失敗しても
        初期化は失敗にしない（次の初期化か業務プロファイルの削除でやり直す）。
        """

        from app.features.nl2sql.incremental_store import purge_orphan_profile_ontology_records

        try:
            with connection.cursor() as cursor:
                artifacts, jobs = purge_orphan_profile_ontology_records(cursor)
            connection.commit()
        except Exception as exc:
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
            logger.warning(
                "nl2sql_orphan_ontology_purge_failed",
                extra={"error_code": oracle_error_code(exc), "exception_type": type(exc).__name__},
            )
            return
        if artifacts or jobs:
            logger.info(
                "nl2sql_orphan_ontology_purged",
                extra={"artifact_count": artifacts, "job_count": jobs},
            )

    def _plan_migrations(self, status: dict[str, Any]) -> list[MigrationArtifact]:
        """未適用/checksum 不一致と欠損 object の所有 migration だけを選ぶ。"""

        pending_versions = {int(version) for version in status["pending_versions"]}
        missing_objects = {
            (str(item["name"]).upper(), str(item["object_type"]).upper())
            for item in status["missing_objects"]
        }
        owned_objects = frozenset(
            object_key for migration in MIGRATIONS for object_key in migration.created_objects
        )
        uncovered = missing_objects - owned_objects
        if uncovered:
            labels = ", ".join(f"{kind}:{name}" for name, kind in sorted(uncovered))
            raise SystemSchemaError(
                "SCHEMA_MIGRATION_MANIFEST_INCOMPLETE",
                f"不足オブジェクトに対応する migration がありません ({labels})。",
            )
        return [
            migration
            for migration in MIGRATIONS
            if migration.version in pending_versions
            or bool(migration.created_objects.intersection(missing_objects))
        ]

    def _status_on(self, connection: Any) -> dict[str, Any]:
        objects = self._load_objects(connection)
        expected = set(MANAGED_OBJECTS)
        existing = expected.intersection(objects)
        missing = sorted(
            (
                {"name": name, "object_type": object_type}
                for name, object_type in expected - existing
            ),
            key=lambda item: (item["object_type"], item["name"]),
        )
        applied = self._load_migrations(connection, objects)
        matching_versions = [
            item.version for item in MIGRATIONS if applied.get(item.version) == item.checksum
        ]
        pending_versions = [
            item.version for item in MIGRATIONS if applied.get(item.version) != item.checksum
        ]
        # 台帳が適用済みでも、既存の表に正本の列が欠けていれば ready にしない（#2458）。
        absent_columns = self._missing_columns(connection, objects)
        status = classify_system_schema_status(
            set(objects), applied, columns_current=not absent_columns
        )
        owner = self._load_schema_owner(connection)
        table_metadata = self._load_table_metadata(connection, objects, owner=owner)
        object_metadata = self._build_object_metadata(objects, table_metadata, owner=owner)
        return {
            "status": status,
            "schema_head": MIGRATIONS[-1].version,
            "applied_versions": sorted(matching_versions),
            "pending_versions": pending_versions,
            "expected_object_count": len(expected),
            "existing_object_count": len(existing),
            "expected_table_count": len(MANAGED_TABLES),
            "existing_table_count": sum((name, "TABLE") in objects for name in MANAGED_TABLES),
            "missing_objects": missing,
            "missing_columns": absent_columns,
            "tables": table_metadata,
            "objects": object_metadata,
            "operation_state": self._operation_payload(connection, objects),
        }

    def _load_objects(self, connection: Any) -> dict[tuple[str, str], Any]:
        names = tuple(dict.fromkeys(name for name, _object_type in MANAGED_OBJECTS))
        placeholders, binds = bind_list("object_name_", names)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT OBJECT_NAME, OBJECT_TYPE, CREATED FROM USER_OBJECTS "
                f"WHERE OBJECT_NAME IN ({placeholders}) "  # nosec B608 - fixed manifest binds
                "AND OBJECT_TYPE IN ('TABLE', 'INDEX', 'SEQUENCE', 'PACKAGE', 'PACKAGE BODY')",
                binds,
            )
            return {(str(row[0]).upper(), str(row[1]).upper()): row[2] for row in cursor.fetchall()}

    @staticmethod
    def _load_schema_owner(connection: Any) -> str:
        """USER_OBJECTS / USER_TABLES の所有者（接続ユーザー）。取得できなければ空文字。"""

        with connection.cursor() as cursor:
            cursor.execute("SELECT USER FROM DUAL")
            row = cursor.fetchone()
        return str(row[0] or "").strip().upper() if row else ""

    @staticmethod
    def _build_object_metadata(
        objects: dict[tuple[str, str], Any],
        table_metadata: Sequence[dict[str, Any]],
        *,
        owner: str = "",
    ) -> list[dict[str, Any]]:
        """既存の table 統計と USER_OBJECTS snapshot を全 manifest object へ展開する。"""

        tables_by_name = {str(item["name"]): item for item in table_metadata}
        result: list[dict[str, Any]] = []
        for name, object_type in MANAGED_OBJECTS:
            table = tables_by_name.get(name) if object_type == "TABLE" else None
            result.append(
                {
                    "name": name,
                    **_object_owner_fields(owner, name),
                    "object_type": object_type,
                    "exists": (name, object_type) in objects,
                    "estimated_rows": (table.get("estimated_rows") if table is not None else None),
                    "created_at": iso_timestamp(objects.get((name, object_type))),
                    "last_analyzed_at": (
                        table.get("last_analyzed_at") if table is not None else None
                    ),
                }
            )
        return result

    def _ensure_control_schema(self) -> None:
        with self._connection_factory() as connection:
            objects = self._load_objects(connection)
            if (CONTROL_TABLE, "TABLE") in objects and (MIGRATION_TABLE, "TABLE") in objects:
                return
            self._configure_ddl_lock_timeout(connection)
            self._apply_migration(connection, MIGRATIONS[0])

    def _apply_migration(self, connection: Any, migration: MigrationArtifact) -> None:
        statements = split_migration_sql(migration.path.read_text(encoding="utf-8"))
        with connection.cursor() as cursor:
            for statement in statements:
                foreign_key = self._managed_foreign_key_for_statement(statement)
                if foreign_key is not None:
                    state = self._foreign_key_state(connection, foreign_key)
                    if state == "matching":
                        continue
                    if state == "mismatch":
                        raise self._foreign_key_mismatch_error(foreign_key)
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    code = oracle_error_code(exc)
                    if foreign_key is not None and code in {"ORA-02264", "ORA-02275"}:
                        state = self._foreign_key_state(connection, foreign_key)
                        if state == "matching":
                            continue
                        if state == "mismatch":
                            raise self._foreign_key_mismatch_error(foreign_key) from exc
                    if code in IGNORED_APPLY_CODES:
                        continue
                    if (
                        migration.version == 9
                        and code == "ORA-00942"
                        and "NL2SQL_APP_ROLE_PERMISSIONS" in statement.upper()
                    ):
                        continue
                    raise
            self._merge_migration(
                cursor,
                key=migration.version,
                description=migration.description,
                checksum=migration.checksum,
            )
            connection.commit()

    @staticmethod
    def _managed_foreign_key_for_statement(
        statement: str,
    ) -> ManagedForeignKey | None:
        match = _ADD_CONSTRAINT_PATTERN.search(statement)
        if match is None:
            return None
        table_name, constraint_name = (value.upper() for value in match.groups())
        foreign_key = _MANAGED_FOREIGN_KEYS_BY_NAME.get(constraint_name)
        if foreign_key is None:
            return None
        if foreign_key.table_name != table_name:
            raise SystemSchemaError(
                "SCHEMA_CONSTRAINT_MISMATCH",
                f"外部キー {constraint_name} の対象テーブル定義が一致しません。",
                status_code=409,
            )
        return foreign_key

    def _foreign_key_state(
        self,
        connection: Any,
        expected: ManagedForeignKey,
    ) -> Literal["missing", "matching", "mismatch"]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT child.TABLE_NAME,
                       child.CONSTRAINT_TYPE,
                       child.DELETE_RULE,
                       parent.TABLE_NAME,
                       child.STATUS,
                       child.VALIDATED,
                       child.DEFERRABLE,
                       child.DEFERRED
                  FROM USER_CONSTRAINTS child
                  LEFT JOIN USER_CONSTRAINTS parent
                    ON parent.OWNER = child.R_OWNER
                   AND parent.CONSTRAINT_NAME = child.R_CONSTRAINT_NAME
                 WHERE child.CONSTRAINT_NAME = :constraint_name
                """,
                {"constraint_name": expected.name},
            )
            row = cursor.fetchone()
        if row is None:
            return "missing"
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COLUMN_NAME
                  FROM USER_CONS_COLUMNS
                 WHERE CONSTRAINT_NAME = :constraint_name
                 ORDER BY POSITION
                """,
                {"constraint_name": expected.name},
            )
            columns = tuple(str(item[0]).upper() for item in cursor.fetchall())
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT referenced_column.COLUMN_NAME
                  FROM USER_CONSTRAINTS child
                  JOIN USER_CONS_COLUMNS referenced_column
                    ON referenced_column.OWNER = child.R_OWNER
                   AND referenced_column.CONSTRAINT_NAME = child.R_CONSTRAINT_NAME
                 WHERE child.CONSTRAINT_NAME = :constraint_name
                 ORDER BY referenced_column.POSITION
                """,
                {"constraint_name": expected.name},
            )
            referenced_columns = tuple(str(item[0]).upper() for item in cursor.fetchall())
        actual = (
            str(row[0]).upper(),
            str(row[1]).upper(),
            str(row[2]).upper(),
            str(row[3]).upper() if row[3] else "",
            str(row[4]).upper(),
            str(row[5]).upper(),
            str(row[6]).upper(),
            str(row[7]).upper(),
            columns,
            referenced_columns,
        )
        wanted = (
            expected.table_name,
            "R",
            expected.delete_rule,
            expected.referenced_table_name,
            "ENABLED",
            "VALIDATED",
            "NOT DEFERRABLE",
            "IMMEDIATE",
            expected.columns,
            expected.referenced_columns,
        )
        return "matching" if actual == wanted else "mismatch"

    @staticmethod
    def _foreign_key_mismatch_error(
        foreign_key: ManagedForeignKey,
    ) -> SystemSchemaError:
        return SystemSchemaError(
            "SCHEMA_CONSTRAINT_MISMATCH",
            f"外部キー {foreign_key.name} は存在しますが、期待する定義と一致しません。"
            "Oracle の制約定義を確認してから再試行してください。",
            status_code=409,
        )

    def _assert_no_active_jobs(self, connection: Any) -> None:
        objects = self._load_objects(connection)
        for table_name in (
            "NL2SQL_SCHEMA_REFRESH_JOBS",
            "NL2SQL_ONTOLOGY_JOBS",
            "NL2SQL_EVALUATION_JOBS",
            "NL2SQL_SYNTHETIC_RUNS",
        ):
            if (table_name, "TABLE") not in objects:
                continue
            states = (*_ACTIVE_JOB_STATES, "VERIFYING", "UNKNOWN")
            placeholders, binds = bind_list("job_state_", states)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table_name} "  # nosec B608 - fixed manifest value
                    f"WHERE UPPER(STATUS) IN ({placeholders})",  # nosec B608
                    binds,
                )
                row = cursor.fetchone()
            if row and int(row[0] or 0) > 0:
                raise SystemSchemaActiveJobsError()

    def _drop_managed_objects(self, connection: Any, owner: str) -> int:
        objects = self._load_objects(connection)
        dropped = 0
        for package_name in reversed(MANAGED_PACKAGES):
            if (package_name, "PACKAGE") in objects:
                dropped += self._execute_drop(connection, f"DROP PACKAGE {package_name}")
                self._heartbeat(connection, owner)
        for index_name in reversed(MANAGED_INDEXES):
            if (index_name, "INDEX") not in objects:
                continue
            dropped += self._execute_drop(connection, f"DROP INDEX {index_name}")
            self._heartbeat(connection, owner)
        for table_name in reversed(MANAGED_TABLES):
            if table_name == CONTROL_TABLE or (table_name, "TABLE") not in objects:
                continue
            dropped += self._execute_drop(
                connection,
                f"DROP TABLE {table_name} CASCADE CONSTRAINTS PURGE",
            )
            self._heartbeat(connection, owner)
        for sequence_name in reversed(MANAGED_SEQUENCES):
            if (sequence_name, "SEQUENCE") not in objects:
                continue
            dropped += self._execute_drop(connection, f"DROP SEQUENCE {sequence_name}")
            self._heartbeat(connection, owner)
        return dropped


system_schema_manager = SystemSchemaManager()


__all__ = [
    "CONTROL_TABLE",
    "DOMAIN_TABLES",
    "MANAGED_FOREIGN_KEYS",
    "MANAGED_INDEXES",
    "MANAGED_OBJECTS",
    "MANAGED_SEQUENCES",
    "MANAGED_TABLES",
    "MIGRATIONS",
    "ManagedForeignKey",
    "PRESERVED_TABLES",
    "RECREATE_CONFIRMATION",
    "SystemSchemaActiveJobsError",
    "SystemSchemaBusyError",
    "SystemSchemaError",
    "SystemSchemaManager",
    "classify_system_schema_status",
    "split_migration_sql",
    "system_schema_manager",
]
