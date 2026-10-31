"""RAG の versioned Oracle system schema を明示操作する manager。

アプリ起動時には DDL を実行しない。管理 API / CLI から明示された場合だけ、
``oracle_schema`` の正本を使って不足オブジェクトの作成・migration・全再作成を行う。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

from pr_system_settings.auth.migrations import TRACE_SPAN_TABLES, apply_platform_auth_schema
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES, OracleAuthStore
from pr_system_settings.chat_attachments import CHAT_ATTACHMENT_TABLES
from pr_system_settings.system_schema import (
    DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
    ForeignKeySpec,
    SystemSchemaBusyError,
    SystemSchemaError,
    SystemSchemaManagerBase,
    SystemSchemaStatus,
    bind_list,
    columns_from_statements,
    foreign_keys_from_create_table,
    iso_timestamp,
    oracle_error_code,
)
from pr_system_settings.system_schema import (
    SystemSchemaActiveJobsError as _SharedActiveJobsError,
)
from pr_system_settings.system_schema import (
    classify_system_schema_status as classify_schema_status,
)

from app.clients.oracle import (
    ORACLE_TEXT_LEXER_PREFERENCE,
    ORACLE_TEXT_STOPLIST,
    OracleClient,
)
from app.rag.oracle_schema import (
    SCHEMA_VERSION,
    OracleSchemaSection,
    oracle_schema_migration_sections,
    oracle_schema_sections,
    split_sql_statements,
)
from app.rag.search_answer_profile_migration import (
    INDEX_RENAMES,
    RENAME_MIGRATION,
    TABLE_RENAMES,
    current_schema_sql,
    historical_sections,
    legacy_tables_exist,
)

logger = logging.getLogger(__name__)

RECREATE_CONFIRMATION = "RECREATE_RAG_SYSTEM_TABLES"
CONTROL_TABLE = "RAG_SCHEMA_OPERATIONS"
MIGRATION_TABLE = "RAG_SCHEMA_MIGRATIONS"
DEFAULT_DDL_LOCK_TIMEOUT_SECONDS = 5
_ACTIVE_JOB_STATES = ("QUEUED", "RUNNING")
_CREATE_TABLE_PATTERN = re.compile(
    r"^\s*CREATE\s+TABLE\s+([A-Z][A-Z0-9_$#]*)\b",
    flags=re.IGNORECASE,
)
_CREATE_INDEX_PATTERN = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+|VECTOR\s+)?INDEX\s+([A-Z][A-Z0-9_$#]*)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MigrationArtifact:
    """Python DDL 正本から得た 1 migration。"""

    name: str
    table_name: str
    sql: str
    # データを消す migration の説明（空なら破壊的でない。checksum には含めない。#619）。
    destructive_note: str = ""

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def destructive(self) -> bool:
        return bool(self.destructive_note)


MIGRATIONS: tuple[MigrationArtifact, ...] = tuple(
    MigrationArtifact(
        section.name,
        section.table_name.upper(),
        section.sql,
        destructive_note=section.destructive_note,
    )
    for section in oracle_schema_migration_sections()
)

# 作成依存順。再作成時はこの逆順で DROP する。
# prefix scan は使用せず、この manifest へ追加された RAG object だけを管理する。
MANAGED_TABLES: tuple[str, ...] = (
    CONTROL_TABLE,
    MIGRATION_TABLE,
    "RAG_LANGUAGE_PACK_RELEASES",
    "RAG_LANGUAGE_PACK_STATE",
    "RAG_DOCUMENTS",
    "RAG_DOCUMENT_RECIPES",
    "RAG_KNOWLEDGE_BASES",
    "RAG_DOCUMENT_KNOWLEDGE_BASES",
    "RAG_SEARCH_ANSWER_PROFILES",
    "RAG_ANSWER_RECORDS",
    "RAG_ANSWER_PROMPTS",
    # 実行履歴（チャット・RAG 検索の実行。#1890）。
    "RAG_RUN_HISTORY",
    # 文書の分類の一覧（#2131）。
    "RAG_DOCUMENT_CATEGORIES",
    # 文書の削除の後始末の記録（#2452）。
    "RAG_DOCUMENT_CLEANUPS",
    "RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE",
    "RAG_SUPPORT_GUIDES",
    "RAG_SUPPORT_GUIDE_REVISIONS",
    # 業務ガイドの公開の版の質問の例の埋め込み（#2398）。RAG_SUPPORT_GUIDE_REVISIONS を参照する。
    "RAG_SUPPORT_GUIDE_EMBEDDINGS",
    # 人が修正した文書の章節（#713）。RAG_DOCUMENTS を参照する。
    "RAG_DOCUMENT_SECTIONS",
    "RAG_CONVERSATIONS",
    "RAG_MESSAGES",
    "RAG_INGESTION_JOBS",
    "RAG_INGESTION_SEGMENTS",
    "RAG_CHUNKS",
    "RAG_CHUNK_SETS",
    "RAG_DOCUMENT_EXTRACTIONS",
    "RAG_ARTIFACT_LAYERS",
    "RAG_SEARCH_AUDIT",
    "RAG_INGESTION_AUDIT",
    "RAG_GRAPH_ENTITIES",
    "RAG_GRAPH_RELATIONSHIPS",
    "RAG_GRAPH_ENTITY_CHUNKS",
    # 実体の層（#1362）。RAG_CHUNK_SETS・RAG_DOCUMENTS・RAG_CHUNKS を参照する。
    "RAG_ENTITIES",
    "RAG_ENTITY_ALIASES",
    "RAG_ENTITY_CHUNKS",
    # 章節と chunk の対応（#1438）。RAG_DOCUMENTS・RAG_CHUNKS を参照する。
    "RAG_CHUNK_SET_SECTIONS",
    "RAG_SECTION_CHUNKS",
    "RAG_CITATION_FEEDBACK",
    "RAG_FEEDBACK_DETAILS",
    "RAG_EVALUATION_RUNS",
    # 品質評価の job（#390）。
    "RAG_EVALUATION_JOBS",
    # 品質評価の判定の校正（#3578）。項目・判定・結果は記入の集合を参照する。
    "RAG_EVALUATION_LABEL_SETS",
    "RAG_EVALUATION_LABEL_ITEMS",
    "RAG_EVALUATION_LABELS",
    "RAG_EVALUATION_CALIBRATIONS",
    # ロールの RAG 権限と対象範囲（#214）。PLATFORM_ROLES と検索・回答プロファイル・KB を参照する。
    "RAG_ROLE_PERMISSIONS",
    "RAG_ROLE_SEARCH_ANSWER_PROFILES",
    "RAG_ROLE_KNOWLEDGE_BASES",
)

# 3 製品共通の認証テーブル（platform の `apply_platform_auth_schema` が作る）。RAG の初期化は
# 先にこれを作るが、管理対象ではないため全再作成でも削除しない（#214）。会話の添付の表も
# 同じ（#2517）。
PRESERVED_TABLES: tuple[str, ...] = (
    *PLATFORM_AUTH_TABLES,
    "PLATFORM_TRACE_RECORDING_SETTINGS",
    *CHAT_ATTACHMENT_TABLES,
    *TRACE_SPAN_TABLES,
)

MANAGED_INDEXES: tuple[str, ...] = (
    "RAG_LANG_RELEASE_PREVIOUS_IDX",
    "RAG_LANG_RELEASE_RESTORE_IDX",
    "RAG_LANG_STATE_CURRENT_IDX",
    "RAG_SUPPORT_GUIDES_PROFILE_IDX",
    "RAG_ANSWER_RECORDS_PROFILE_IDX",
    "RAG_ANSWER_RECORDS_OWNER_IDX",
    "RAG_RUN_HISTORY_CREATED_IDX",
    "RAG_RUN_HISTORY_OWNER_IDX",
    "RAG_DOCUMENT_CATEGORIES_NAME_UX",
    "RAG_DOCUMENT_CATEGORIES_PARENT_IDX",
    "RAG_DOCUMENT_CLEANUPS_DOCUMENT_IDX",
    "RAG_DOCUMENT_CLEANUPS_UPDATED_IDX",
    "RAG_RUN_HISTORY_CONVERSATION_IDX",
    "RAG_DOCUMENTS_CONTENT_SHA256_IDX",
    "RAG_DOCUMENTS_DUPLICATE_IDX",
    "RAG_DOCUMENTS_STATUS_UPLOADED_IDX",
    "RAG_DOCUMENTS_TENANT_STATUS_UPLOADED_IDX",
    "RAG_DOCUMENT_RECIPES_STATUS_IDX",
    "RAG_KNOWLEDGE_BASES_TENANT_NAME_UIDX",
    "RAG_KNOWLEDGE_BASES_TENANT_STATUS_IDX",
    "RAG_DOCUMENT_KNOWLEDGE_BASES_DOCUMENT_IDX",
    "RAG_DOCUMENT_KNOWLEDGE_BASES_TENANT_KB_IDX",
    "RAG_SEARCH_ANSWER_PROFILES_TENANT_NAME_UIDX",
    "RAG_SEARCH_ANSWER_PROFILES_TENANT_STATUS_IDX",
    "RAG_CONVERSATIONS_SEARCH_ANSWER_PROFILE_IDX",
    "RAG_CONVERSATIONS_TENANT_PROFILE_UPDATED_IDX",
    "RAG_MESSAGES_CONVERSATION_CREATED_IDX",
    "RAG_MESSAGES_REPLY_TO_IDX",
    "RAG_MESSAGES_TENANT_CREATED_IDX",
    "RAG_MESSAGES_TRACE_IDX",
    "RAG_INGESTION_JOBS_DOCUMENT_IDX",
    "RAG_INGESTION_JOBS_LEASE_IDX",
    "RAG_INGESTION_JOBS_RECIPE_IDX",
    "RAG_INGESTION_JOBS_TENANT_QUEUED_IDX",
    "RAG_INGESTION_SEGMENTS_DOCUMENT_STATUS_IDX",
    "RAG_INGESTION_SEGMENTS_RECIPE_STATUS_IDX",
    "RAG_INGESTION_SEGMENTS_TENANT_STATUS_IDX",
    "RAG_CHUNKS_EMBEDDING_HNSW_IDX",
    "RAG_CHUNKS_TEXT_IDX",
    "RAG_CHUNKS_TENANT_DOCUMENT_IDX",
    "RAG_CHUNKS_CHUNK_SET_IDX",
    "RAG_CHUNK_SETS_DOCUMENT_IDX",
    "RAG_CHUNK_SETS_SERVING_IDX",
    "RAG_CHUNK_SETS_RECIPE_IDX",
    "RAG_CHUNK_SETS_RECIPE_ACTIVE_UIDX",
    "RAG_CHUNK_SETS_EXTRACTION_IDX",
    "RAG_DOC_EXT_STATUS_IDX",
    "RAG_ARTIFACT_LAYERS_PARENT_IDX",
    "RAG_ARTIFACT_LAYERS_DOCUMENT_IDX",
    "RAG_SEARCH_AUDIT_CONFIG_IDX",
    "RAG_SEARCH_AUDIT_CREATED_OUTCOME_IDX",
    "RAG_SEARCH_AUDIT_QUERY_HASH_IDX",
    "RAG_SEARCH_AUDIT_TENANT_CREATED_IDX",
    "RAG_SEARCH_AUDIT_TRACE_IDX",
    "RAG_INGESTION_AUDIT_DOCUMENT_CREATED_IDX",
    "RAG_INGESTION_AUDIT_PARSER_CREATED_IDX",
    "RAG_INGESTION_AUDIT_SOURCE_SHA256_IDX",
    "RAG_INGESTION_AUDIT_TENANT_CREATED_IDX",
    "RAG_INGESTION_AUDIT_TRACE_IDX",
    "RAG_GRAPH_ENTITIES_CHUNK_SET_IDX",
    "RAG_GRAPH_ENTITIES_TENANT_NAME_IDX",
    "RAG_GRAPH_REL_SOURCE_IDX",
    "RAG_GRAPH_REL_TARGET_IDX",
    "RAG_GRAPH_ENTITY_CHUNKS_CHUNK_IDX",
    "RAG_GRAPH_ENTITY_CHUNKS_CHUNK_SET_IDX",
    "RAG_ENTITIES_CHUNK_SET_IDX",
    "RAG_ENTITIES_DOCUMENT_IDX",
    "RAG_ENTITY_ALIASES_KEY_IDX",
    "RAG_ENTITY_CHUNKS_CHUNK_IDX",
    "RAG_CHUNK_SET_SECTIONS_DOCUMENT_IDX",
    "RAG_SECTION_CHUNKS_CHUNK_IDX",
    "RAG_SECTION_CHUNKS_DOCUMENT_IDX",
    "RAG_CITATION_FEEDBACK_TRACE_IDX",
    "RAG_CITATION_FEEDBACK_TENANT_CREATED_IDX",
    "RAG_FEEDBACK_BUSINESS_CREATED_IDX",
    "RAG_FEEDBACK_USER_TRACE_IDX",
    "RAG_FEEDBACK_DETAILS_TENANT_CREATED_IDX",
    "RAG_FEEDBACK_DETAILS_MESSAGE_IDX",
    "RAG_FEEDBACK_DETAILS_TEXT_IDX",
    "RAG_EVALUATION_RUNS_RESULT_HASH_IDX",
    "RAG_EVALUATION_RUNS_TENANT_CREATED_IDX",
    "RAG_EVALUATION_JOBS_STATUS_IDX",
    "RAG_EVALUATION_JOBS_OWNER_CREATED_IDX",
    "RAG_EVALUATION_LABEL_SETS_TENANT_CREATED_IDX",
    "RAG_EVALUATION_LABELS_ANNOTATOR_IDX",
    "RAG_EVALUATION_CALIBRATIONS_SET_CREATED_IDX",
    "RAG_EVALUATION_CALIBRATIONS_JUDGE_IDX",
    "RAG_ROLE_SEARCH_ANSWER_PROFILES_VIEW_IDX",
    "RAG_ROLE_KNOWLEDGE_BASES_KB_IDX",
)

MANAGED_TEXT_OBJECTS: tuple[tuple[str, str], ...] = (
    (ORACLE_TEXT_LEXER_PREFERENCE, "TEXT_PREFERENCE"),
    (ORACLE_TEXT_STOPLIST, "TEXT_STOPLIST"),
)

MANAGED_OBJECTS: tuple[tuple[str, str], ...] = (
    *((name, "TABLE") for name in MANAGED_TABLES),
    *((name, "INDEX") for name in MANAGED_INDEXES),
    *MANAGED_TEXT_OBJECTS,
)

# 旧 migration が一時的に作成したが、現行 runtime では使用しない object。
RETIRED_MANAGED_OBJECTS: tuple[tuple[str, str], ...] = (
    *((name, "TABLE") for name in TABLE_RENAMES),
    *((name, "INDEX") for name in INDEX_RENAMES),
    ("RAG_KB_CHUNK_SET_BINDINGS", "TABLE"),
    ("RAG_KB_CS_BIND_CS_IDX", "INDEX"),
    ("RAG_DOCUMENT_EXTRACTIONS_DOCUMENT_IDX", "INDEX"),
    # 2026-06 の schema が同じ列式を旧名で作成していた。Oracle は同一列リストの
    # 別名 index 作成を ORA-01408 で拒否するため、現行名の作成前にだけ明示削除する。
    ("RAG_INGESTION_SEGMENTS_RECIPE_IDX", "INDEX"),
    # 旧 standard の回答エンジンだけが使っていた表（#596）。更新では migration
    # `20260930_005_retire_standard_engine_objects` が消す。全再作成は migration を実行せず
    # 記録だけするため、ここにも載せて残さない（index・制約は表と一緒に消える）。
    ("RAG_PROMPT_VERSIONS", "TABLE"),
    ("RAG_GENERATION_SETTINGS", "TABLE"),
    ("RAG_AGENT_MEMORIES", "TABLE"),
    # 関係情報の claims / community summary の表（#621。読む経路が無かった）。更新では migration
    # `20260930_007_retire_graph_claims_community` が消す。全再作成のためにここにも載せる。
    ("RAG_GRAPH_CLAIMS", "TABLE"),
    ("RAG_GRAPH_COMMUNITY_SUMMARIES", "TABLE"),
    # 回答生成のプロンプトの表の旧名（#599）。更新では migration
    # `20260930_008_answer_prompts_table` が改名するか、行を `RAG_ANSWER_PROMPTS` へ写してから、
    # ここで消す（行は消えない）。
    ("RAG_DOCRAG_PROMPTS", "TABLE"),
)

DOMAIN_TABLES = frozenset(MANAGED_TABLES) - {CONTROL_TABLE, MIGRATION_TABLE}

if set(PRESERVED_TABLES) & set(MANAGED_TABLES):  # pragma: no cover - 定義の誤りを起動時に検出
    raise RuntimeError("共通認証のテーブルを RAG の管理対象に含めないでください。")


class SystemSchemaActiveJobsError(_SharedActiveJobsError):
    def __init__(self) -> None:
        super().__init__(
            "待機中または実行中の取込ジョブがあります。完了またはキャンセルしてから再実行してください。"
        )


def managed_manifest_from_schema(
    sections: Sequence[OracleSchemaSection] | None = None,
) -> set[tuple[str, str]]:
    """DDL 正本の CREATE TABLE / INDEX と Text object を抽出する。"""

    objects: set[tuple[str, str]] = set(MANAGED_TEXT_OBJECTS)
    for section in sections or oracle_schema_sections():
        for statement in split_sql_statements(section.sql):
            table_match = _CREATE_TABLE_PATTERN.match(statement)
            if table_match is not None:
                objects.add((table_match.group(1).upper(), "TABLE"))
            index_match = _CREATE_INDEX_PATTERN.match(statement)
            if index_match is not None:
                objects.add((index_match.group(1).upper(), "INDEX"))
    return objects


def managed_foreign_keys_from_schema(
    sections: Sequence[OracleSchemaSection] | None = None,
) -> tuple[ForeignKeySpec, ...]:
    """DDL 正本の CREATE TABLE にある外部キー（#505）。

    表の作成は「無ければ作る」なので、古い版で作った表には後から足した FK が無いまま残る。
    状態の確認でこの一覧と USER_CONSTRAINTS を比べ、更新の操作で足す。
    """

    return tuple(
        foreign_key
        for section in sections or oracle_schema_sections()
        for statement in split_sql_statements(section.sql)
        for foreign_key in foreign_keys_from_create_table(statement)
    )


MANAGED_FOREIGN_KEYS: tuple[ForeignKeySpec, ...] = managed_foreign_keys_from_schema()


def managed_columns_from_schema(
    sections: Sequence[OracleSchemaSection] | None = None,
) -> dict[str, tuple[str, ...]]:
    """DDL 正本の CREATE TABLE の列（表名 → 列名。#2450）。

    表の作成は「無ければ作る」なので、台帳が適用済みでも既存の表に列が欠けることがある（取り消した
    migration が列を改名・削除した表など）。状態の確認でこの一覧と USER_TAB_COLUMNS を比べ、欠けた
    列があれば ready にしない。
    """

    return columns_from_statements(
        statement
        for section in sections or oracle_schema_sections()
        for statement in split_sql_statements(section.sql)
    )


MANAGED_COLUMNS: dict[str, tuple[str, ...]] = managed_columns_from_schema()


def classify_system_schema_status(
    objects: set[tuple[str, str]],
    applied_checksums: dict[str, str],
    *,
    foreign_keys_current: bool = True,
    columns_current: bool = True,
) -> SystemSchemaStatus:
    """Dictionary / ledger snapshot を四つの公開状態へ分類する（分類の規則は platform）。"""

    return classify_schema_status(
        objects,
        domain_tables=DOMAIN_TABLES,
        managed_objects=MANAGED_OBJECTS,
        retired_objects=RETIRED_MANAGED_OBJECTS,
        migrations_current=all(
            applied_checksums.get(migration.name) == migration.checksum for migration in MIGRATIONS
        ),
        foreign_keys_current=foreign_keys_current,
        columns_current=columns_current,
    )


# 台帳に記録の無い schema に残る廃止した表の削除（#1648）。状態の
# `pending_destructive_migrations` にこの名前で出し、作成・更新は承認（`allow_destructive`）が
# 無ければ止める。
RETIRED_TABLES_APPROVAL = "retired_tables_without_ledger"


def unowned_retired_tables(
    objects: Collection[tuple[str, str]],
    applied_checksums: Mapping[str, str],
) -> list[str]:
    """作成・更新で削除する廃止した表のうち、RAG が作ったと台帳から分からないもの（#1648）。

    台帳（RAG_SCHEMA_MIGRATIONS）に記録があれば、この schema は RAG が初期化したもので、廃止した表も
    RAG が作ったものとして承認なしに消す（データを消す migration は別に承認を求める）。記録が無い
    schema（既存の ADB の、旧版の単独の製品・別のアプリケーションと共有する schema など）では、同じ
    名前の表を承認なしに消さない。旧名の表（TABLE_RENAMES）は消さずに改名するため含めない。
    """

    if applied_checksums:
        return []
    return [
        name
        for name, object_type in RETIRED_MANAGED_OBJECTS
        if object_type == "TABLE" and name not in TABLE_RENAMES and (name, "TABLE") in objects
    ]


def pending_destructive_migrations(
    status: str,
    pending_versions: Sequence[str],
    *,
    retired_tables: Sequence[str] = (),
) -> list[dict[str, str]]:
    """未適用の、データを消す migration（#619）と、台帳に記録の無い廃止した表の削除（#1648）。

    未初期化（`missing`）の DB は migration を実行せずに記録だけするので、migration で消えるデータは
    無い。それ以外で未適用・checksum 不一致のものを、作成・更新の前に承認させる。
    `retired_tables`（`unowned_retired_tables`）は、状態によらず（`missing` でも）承認させる。
    """

    pending = set(pending_versions)
    items = (
        []
        if status == "missing"
        else [
            {"name": migration.name, "description": migration.destructive_note}
            for migration in MIGRATIONS
            if migration.destructive and migration.name in pending
        ]
    )
    if retired_tables:
        items.append(
            {
                "name": RETIRED_TABLES_APPROVAL,
                "description": (
                    f"廃止した表（{', '.join(retired_tables)}）を削除します。"
                    "この schema には RAG の台帳（RAG_SCHEMA_MIGRATIONS）の記録が無く、"
                    "RAG が作った表か確かめられません。別のアプリケーションや旧版の表なら、"
                    "RAG を専用の schema（DB ユーザー）に配備してください。削除してよい場合は、"
                    "必要なデータを書き出してから承認してください。"
                ),
            }
        )
    return items


class SystemSchemaManager(SystemSchemaManagerBase):
    """RAG の manifest・DDL 正本・Oracle Text object を、platform の骨格（lease・台帳）に渡す。"""

    control_table = CONTROL_TABLE
    migration_table = MIGRATION_TABLE
    migration_key_column = "MIGRATION_NAME"
    managed_tables = MANAGED_TABLES
    managed_foreign_keys = MANAGED_FOREIGN_KEYS
    managed_columns = MANAGED_COLUMNS
    # データを消す未適用の migration は、承認が無ければ作成・更新で当てない（#619）。
    guards_destructive_migrations = True
    recreate_confirmation = RECREATE_CONFIRMATION
    log_prefix = "rag"
    lock_timeout_guidance = "取込処理を停止してから、"
    logger = logger

    def __init__(
        self,
        connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        *,
        lease_seconds: int = 900,
        ddl_lock_timeout_seconds: int = DEFAULT_DDL_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            connection_factory or self._default_connection,
            lease_seconds=lease_seconds,
            ddl_lock_timeout_seconds=ddl_lock_timeout_seconds,
        )

    @staticmethod
    @contextmanager
    def _default_connection() -> Iterator[Any]:
        connection = OracleClient().acquire_connection()
        try:
            yield connection
        finally:
            connection.close()

    def _operation_kind(self, recreate: bool) -> str:
        # RAG_SCHEMA_OPERATIONS の CHECK 制約は大文字（'INITIALIZE' / 'RECREATE'）。
        return "RECREATE" if recreate else "INITIALIZE"

    def _initialize_on(self, connection: Any, owner: str, *, recreate: bool) -> dict[str, Any]:
        dropped_count = 0
        applied_names: list[str] = []
        self._configure_ddl_lock_timeout(connection)
        # RAG_ROLE_* は PLATFORM_ROLES を参照するため、共通認証の表を先に作る（冪等）。
        self._apply_platform_auth_schema(connection)
        before = self._status_on(connection)
        if before["status"] == "ready" and not recreate:
            return self._no_op_result(connection, owner)

        if recreate:
            self._assert_no_active_jobs(connection)
            dropped_count += self._drop_managed_objects(connection, owner)

        legacy = legacy_tables_exist(connection) if not recreate else False
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME FROM USER_TABLES WHERE TABLE_NAME IN (:n0, :n1, :n2)",
                {f"n{i}": name for i, name in enumerate(TABLE_RENAMES.values())},
            )
            renamed_started = bool(cursor.fetchall())
        if (
            legacy
            and renamed_started
            and any(name != RENAME_MIGRATION for name in before["pending_versions"])
        ):
            raise SystemSchemaError(
                "PROFILE_RENAME_INCOMPLETE",
                (
                    "改名途中の schema に未適用の旧 migratio"
                    "n があります。バックアップから状態を確認してください。"
                ),
            )
        if legacy and not renamed_started:
            # 旧版の未適用 migration は旧 schema で完了してから改名する。
            legacy_base = [
                statement
                for section in historical_sections()["base"]
                for statement in split_sql_statements(section["sql"])
                if _CREATE_INDEX_PATTERN.match(statement) is None
            ]
            self._execute_statements(connection, legacy_base)
            pending = set(before["pending_versions"])
            for migration in MIGRATIONS:
                if migration.name == RENAME_MIGRATION or migration.name not in pending:
                    continue
                self._heartbeat(connection, owner)
                self._execute_statements(connection, split_sql_statements(migration.sql))
                self._record_migration(connection, migration)
                applied_names.append(migration.name)
        # DDL は auto-commit。ledger 未記録の中断後もこの冪等 block を再開する。
        rename = next(m for m in MIGRATIONS if m.name == RENAME_MIGRATION)
        if (
            legacy
            or before["status"] != "missing"
            and RENAME_MIGRATION in before["pending_versions"]
        ):
            self._apply_migration(connection, rename)
            applied_names.append(rename.name)

        self._heartbeat(connection, owner)
        self._apply_base_non_index_statements(connection)
        fresh_database = before["status"] == "missing" and not legacy or recreate
        if fresh_database:
            for migration in MIGRATIONS:
                self._record_migration(connection, migration)
                applied_names.append(migration.name)
        else:
            pending = set(before["pending_versions"])
            for migration in MIGRATIONS:
                if migration.name not in pending or migration.name in applied_names:
                    continue
                self._heartbeat(connection, owner)
                self._apply_migration(connection, migration)
                applied_names.append(migration.name)

        dropped_count += self._drop_retired_objects(connection)
        self._apply_missing_indexes(connection)
        # 古い版で作った表に、後から正本に足した FK を補い（#505）、削除規則が違う FK を作り直し、
        # 無効化された FK を有効にする（#511）。孤立した行は消さない。
        foreign_keys = self._repair_foreign_keys(connection, owner)
        self._heartbeat(connection, owner)
        interim = self._status_on(connection)
        self._require_columns_present(interim)
        if interim["status"] != "ready":
            raise SystemSchemaError(
                "SCHEMA_POSTCONDITION_FAILED",
                "システムテーブル操作後も必須オブジェクトが不足しています。状態を再取得して再試行してください。",
            )
        self._finish_operation(connection, owner, increment_epoch=True)
        after = self._status_on(connection)

        operation = self._operation_name(before["status"], recreate=recreate)
        self._log_operation_succeeded(
            operation,
            after,
            dropped_object_count=dropped_count,
            applied_migrations=applied_names,
            added_foreign_keys=[item["name"] for item in foreign_keys["added"]],
            recreated_foreign_keys=[item["name"] for item in foreign_keys["recreated"]],
            enabled_foreign_keys=[item["name"] for item in foreign_keys["enabled"]],
            novalidate_foreign_keys=[
                item["name"]
                for items in foreign_keys.values()
                for item in items
                if not item["validated"]
            ],
        )
        return {
            **after,
            "operation": operation,
            "dropped_object_count": dropped_count,
            "created_object_count": (
                max(0, int(after["existing_object_count"]) - 1)
                if recreate
                else max(
                    0,
                    int(after["existing_object_count"]) - int(before["existing_object_count"]),
                )
            ),
        }

    def _status_on(self, connection: Any) -> dict[str, Any]:
        objects = self._load_objects(connection)
        expected = set(MANAGED_OBJECTS)
        existing = expected.intersection(objects)
        applied = self._load_migrations(connection, objects)
        matching = [
            migration.name
            for migration in MIGRATIONS
            if applied.get(migration.name) == migration.checksum
        ]
        pending = [
            migration.name
            for migration in MIGRATIONS
            if applied.get(migration.name) != migration.checksum
        ]
        foreign_keys = self._foreign_key_drift(connection)
        table_metadata = self._load_table_metadata(connection, objects)
        # 台帳が適用済みでも、既存の表に正本の列が欠けていれば ready にしない（#2450）。
        absent_columns = self._missing_columns(connection, objects)
        status = classify_system_schema_status(
            set(objects),
            applied,
            foreign_keys_current=foreign_keys.current,
            columns_current=not absent_columns,
        )
        return {
            "status": status,
            "schema_version": SCHEMA_VERSION,
            "schema_head": MIGRATIONS[-1].name if MIGRATIONS else SCHEMA_VERSION,
            "applied_versions": matching,
            "pending_versions": pending,
            "pending_destructive_migrations": pending_destructive_migrations(
                status,
                pending,
                retired_tables=unowned_retired_tables(objects, applied),
            ),
            "expected_object_count": len(expected),
            "existing_object_count": len(existing),
            "expected_table_count": len(MANAGED_TABLES),
            "existing_table_count": sum((name, "TABLE") in objects for name in MANAGED_TABLES),
            "missing_objects": [
                {"name": name, "object_type": object_type}
                for name, object_type in sorted(
                    expected - existing,
                    key=lambda item: (item[1], item[0]),
                )
            ],
            "retired_objects": [
                {"name": name, "object_type": object_type}
                for name, object_type in RETIRED_MANAGED_OBJECTS
                if (name, object_type) in objects
            ],
            "missing_columns": absent_columns,
            # 既存の表に無い FK（更新で足す）と、既存の行を検査していない FK の孤立した行（#505）。
            # 削除規則が正本と違う FK と、無効化された FK（更新で直す。#511）。
            **foreign_keys.status_fields(),
            "tables": table_metadata,
            # 全管理 object（テーブル・索引・Oracle Text の設定）。詳細の一覧を概要の
            # 「存在オブジェクト / 必須オブジェクト」と同じ件数にそろえる（NL2SQL と同じ。#658）。
            "objects": self._build_object_metadata(objects, table_metadata),
            "operation_state": self._operation_payload(connection, objects),
        }

    @staticmethod
    def _build_object_metadata(
        objects: dict[tuple[str, str], Any],
        table_metadata: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """USER_OBJECTS の snapshot とテーブルの統計を、全管理 object の行へ展開する。"""

        tables_by_name = {str(item["name"]): item for item in table_metadata}
        result: list[dict[str, Any]] = []
        for name, object_type in MANAGED_OBJECTS:
            table = tables_by_name.get(name) if object_type == "TABLE" else None
            result.append(
                {
                    "name": name,
                    "object_type": object_type,
                    "exists": (name, object_type) in objects,
                    "estimated_rows": table.get("estimated_rows") if table else None,
                    "created_at": iso_timestamp(objects.get((name, object_type))),
                    "last_analyzed_at": table.get("last_analyzed_at") if table else None,
                }
            )
        return result

    def _load_objects(self, connection: Any) -> dict[tuple[str, str], Any]:
        inspected = tuple(dict.fromkeys((*MANAGED_OBJECTS, *RETIRED_MANAGED_OBJECTS)))
        names = tuple(dict.fromkeys(name for name, _object_type in inspected))
        placeholders, binds = bind_list("object_name_", names)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT OBJECT_NAME, OBJECT_TYPE, CREATED FROM USER_OBJECTS "
                f"WHERE OBJECT_NAME IN ({placeholders}) "  # nosec B608 - fixed binds
                "AND OBJECT_TYPE IN ('TABLE', 'INDEX')",
                binds,
            )
            objects = {
                (str(row[0]).upper(), str(row[1]).upper()): row[2] for row in cursor.fetchall()
            }
        # ベクトル索引（HNSW）は版によって USER_OBJECTS に出ない（Autonomous Database Free 23.9 で
        # 確認。#10）。INDEX は USER_INDEXES でも確かめ、作成済みの索引を「不足」と誤らない。
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT INDEX_NAME FROM USER_INDEXES WHERE INDEX_NAME IN ({placeholders})",  # nosec B608 - fixed binds
                binds,
            )
            for row in cursor.fetchall():
                objects.setdefault((str(row[0]).upper(), "INDEX"), None)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT PRE_NAME FROM CTX_USER_PREFERENCES "
                "WHERE PRE_NAME = :preference_name AND PRE_CLASS = 'LEXER'",
                {"preference_name": ORACLE_TEXT_LEXER_PREFERENCE},
            )
            if cursor.fetchone() is not None:
                objects[(ORACLE_TEXT_LEXER_PREFERENCE, "TEXT_PREFERENCE")] = None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT SPL_NAME FROM CTX_USER_STOPLISTS WHERE SPL_NAME = :stoplist_name",
                {"stoplist_name": ORACLE_TEXT_STOPLIST},
            )
            if cursor.fetchone() is not None:
                objects[(ORACLE_TEXT_STOPLIST, "TEXT_STOPLIST")] = None
        return objects

    def _ensure_control_schema(self) -> None:
        control_section = oracle_schema_sections()[0]
        with self._connection_factory() as connection:
            self._configure_ddl_lock_timeout(connection)
            self._execute_statements(connection, split_sql_statements(control_section.sql))

    @staticmethod
    def _apply_platform_auth_schema(connection: Any) -> None:
        """共通認証の PLATFORM_* と組み込み SYSTEM_ADMIN ロールを冪等に用意する。

        ユーザーは作らない（最初は構成管理者 `system_admin` でログインする）。
        """
        apply_platform_auth_schema(connection)
        with connection.cursor() as cursor:
            OracleAuthStore._merge_system_admin_role(cursor)
        connection.commit()

    def _apply_base_non_index_statements(self, connection: Any) -> None:
        statements = [
            statement
            for section in oracle_schema_sections()
            for statement in split_sql_statements(section.sql)
            if _CREATE_INDEX_PATTERN.match(statement) is None
        ]
        self._execute_statements(connection, statements)

    def _apply_missing_indexes(self, connection: Any) -> None:
        objects = self._load_objects(connection)
        missing = {name for name in MANAGED_INDEXES if (name, "INDEX") not in objects}
        statements = [
            statement
            for section in oracle_schema_sections()
            for statement in split_sql_statements(section.sql)
            if (match := _CREATE_INDEX_PATTERN.match(statement)) is not None
            and match.group(1).upper() in missing
        ]
        self._execute_statements(connection, statements)

    def _apply_migration(self, connection: Any, migration: MigrationArtifact) -> None:
        self._execute_statements(
            connection,
            split_sql_statements(
                migration.sql
                if migration.name == RENAME_MIGRATION
                else current_schema_sql(migration.sql)
            ),
        )
        self._record_migration(connection, migration)

    def _record_migration(self, connection: Any, migration: MigrationArtifact) -> None:
        with connection.cursor() as cursor:
            self._merge_migration(
                cursor,
                key=migration.name,
                description=migration.table_name.lower(),
                checksum=migration.checksum,
            )
            connection.commit()

    def _assert_no_active_jobs(self, connection: Any) -> None:
        objects = self._load_objects(connection)
        if ("RAG_INGESTION_JOBS", "TABLE") not in objects:
            return
        placeholders, binds = bind_list("job_status_", _ACTIVE_JOB_STATES)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) FROM RAG_INGESTION_JOBS WHERE UPPER(STATUS) IN ({placeholders})",  # nosec B608 - fixed states
                binds,
            )
            row = cursor.fetchone()
        if row and int(row[0] or 0) > 0:
            raise SystemSchemaActiveJobsError()

    def _drop_managed_objects(self, connection: Any, owner: str) -> int:
        objects = self._load_objects(connection)
        dropped = 0
        retired_indexes = tuple(
            name for name, object_type in RETIRED_MANAGED_OBJECTS if object_type == "INDEX"
        )
        for index_name in reversed((*MANAGED_INDEXES, *retired_indexes)):
            if (index_name, "INDEX") not in objects:
                continue
            dropped += self._execute_drop(connection, f"DROP INDEX {index_name}")
            self._heartbeat(connection, owner)
        dropped += self._drop_text_objects(connection, objects)
        retired_tables = tuple(
            name for name, object_type in RETIRED_MANAGED_OBJECTS if object_type == "TABLE"
        )
        for table_name in reversed((*MANAGED_TABLES, *retired_tables)):
            if table_name == CONTROL_TABLE or (table_name, "TABLE") not in objects:
                continue
            dropped += self._execute_drop(
                connection,
                f"DROP TABLE {table_name} CASCADE CONSTRAINTS PURGE",
            )
            self._heartbeat(connection, owner)
        return dropped

    def _drop_retired_objects(self, connection: Any) -> int:
        objects = self._load_objects(connection)
        dropped = 0
        for name, object_type in reversed(RETIRED_MANAGED_OBJECTS):
            if (name, object_type) not in objects:
                continue
            statement = (
                f"DROP TABLE {name} CASCADE CONSTRAINTS PURGE"
                if object_type == "TABLE"
                else f"DROP INDEX {name}"
            )
            dropped += self._execute_drop(connection, statement)
        return dropped

    @staticmethod
    def _drop_text_objects(
        connection: Any,
        objects: dict[tuple[str, str], Any],
    ) -> int:
        dropped = 0
        statements = (
            (
                (ORACLE_TEXT_LEXER_PREFERENCE, "TEXT_PREFERENCE"),
                f"BEGIN CTX_DDL.DROP_PREFERENCE('{ORACLE_TEXT_LEXER_PREFERENCE}'); END;",
            ),
            (
                (ORACLE_TEXT_STOPLIST, "TEXT_STOPLIST"),
                f"BEGIN CTX_DDL.DROP_STOPLIST('{ORACLE_TEXT_STOPLIST}'); END;",
            ),
        )
        with connection.cursor() as cursor:
            for key, statement in statements:
                if key not in objects:
                    continue
                cursor.execute(statement)
                dropped += 1
            connection.commit()
        return dropped


system_schema_manager = SystemSchemaManager()


__all__ = [
    "CONTROL_TABLE",
    "DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED",
    "DOMAIN_TABLES",
    "MANAGED_COLUMNS",
    "MANAGED_FOREIGN_KEYS",
    "MANAGED_INDEXES",
    "MANAGED_OBJECTS",
    "MANAGED_TABLES",
    "MANAGED_TEXT_OBJECTS",
    "MIGRATIONS",
    "MIGRATION_TABLE",
    "PRESERVED_TABLES",
    "RECREATE_CONFIRMATION",
    "RETIRED_MANAGED_OBJECTS",
    "RETIRED_TABLES_APPROVAL",
    "MigrationArtifact",
    "SystemSchemaActiveJobsError",
    "SystemSchemaBusyError",
    "SystemSchemaError",
    "SystemSchemaManager",
    "classify_system_schema_status",
    "managed_columns_from_schema",
    "managed_foreign_keys_from_schema",
    "managed_manifest_from_schema",
    "oracle_error_code",
    "pending_destructive_migrations",
    "system_schema_manager",
    "unowned_retired_tables",
]
