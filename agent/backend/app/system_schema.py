"""Agent のシステムテーブル（versioned Oracle system schema）を明示操作する manager（#751）。

RAG / NL2SQL と同じく、platform の骨格
（`pr_system_settings.system_schema.SystemSchemaManagerBase`）に、Agent の manifest と
DDL の正本を渡す。アプリの起動時には DDL を実行しない。管理 API（運用設定 > システムテーブル）と
CLI（`python -m app.cli.agent_system_schema`）から明示されたときだけ、不足分の作成・
未適用の migration・廃止 object の削除・確認語付きの全再作成を行う。

- 接続は共通 `.env` の `PLATFORM_ORACLE_*`（`app.oracle_connection`）。
- 共通認証の `PLATFORM_*` と組み込み SYSTEM_ADMIN ロールは、`AGENT_ROLE_*` の FK の参照先なので
  先に冪等に用意する（RAG と同じ）。管理対象ではないため、全再作成でも削除しない。
- 利用状況・フィードバックの集計に使う Run の事実（`AGENT_RUN_FACTS`。#794）もここで作る。
- Run・業務 Agent の保存先（`AGENT_RUNTIME_CHECKPOINTS` と監査用の projection
  `AGENT_RUNTIME_*`）と、画面で変えた定義（Skill・MCP 接続・ツール権限）の
  `AGENT_CONTROL_PLANE_ITEMS` もここで作る（#764。以前は Runtime repository が別の接続設定で
  作っていた）。全再作成はこれらも消す（Run の履歴も消える）。
- これらの保存済みのデータを書き換える migration（`MigrationArtifact.rewrites`）と全再作成は、
  動いている backend のメモリ（Run の repository・定義の registry）を保存先から読み直す（#1844。
  `storage_bootstrap.rewrite_saved_data`）。結果の `restart_required` は、読み直せないプロセス
  （dispatcher・読み直しに失敗した backend）の再起動の案内。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from pr_system_settings.auth.migrations import TRACE_SPAN_TABLES, apply_platform_auth_schema
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES, OracleAuthStore
from pr_system_settings.chat_attachments import CHAT_ATTACHMENT_TABLES
from pr_system_settings.system_schema import (
    IGNORED_APPLY_CODES,
    IGNORED_DROP_CODES,
    ForeignKeySpec,
    SystemSchemaError,
    SystemSchemaManagerBase,
    SystemSchemaStatus,
    bind_list,
    columns_from_statements,
    foreign_keys_from_create_table,
    iso_timestamp,
)
from pr_system_settings.system_schema import (
    classify_system_schema_status as classify_schema_status,
)

from app.oracle_connection import platform_oracle_connection
from app.security.permissions import RETIRED_PERMISSION_CODES

logger = logging.getLogger(__name__)

# 保存済みのデータの置き場所（`storage_bootstrap.SAVED_DATA_AREAS` と同じ）。runtime は Run・業務
# Agent の checkpoint（AGENT_RUNTIME_CHECKPOINTS）、control_plane は画面・API で変えた定義
# （AGENT_CONTROL_PLANE_ITEMS）。
RUNTIME_DATA = "runtime"
CONTROL_PLANE_DATA = "control_plane"
ALL_SAVED_DATA: frozenset[str] = frozenset({RUNTIME_DATA, CONTROL_PLANE_DATA})

RECREATE_CONFIRMATION = "RECREATE_AGENT_SYSTEM_TABLES"
CONTROL_TABLE = "AGENT_SCHEMA_OPERATIONS"
MIGRATION_TABLE = "AGENT_SCHEMA_MIGRATIONS"
DEFAULT_DDL_LOCK_TIMEOUT_SECONDS = 5

_CREATE_TABLE_PATTERN = re.compile(r"^\s*CREATE\s+TABLE\s+([A-Z][A-Z0-9_$#]*)\b", re.IGNORECASE)
_CREATE_INDEX_PATTERN = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+([A-Z][A-Z0-9_$#]*)\b", re.IGNORECASE
)

# ---- DDL の正本 ---------------------------------------------------------------

# 適用済み migration の台帳と、複数 replica の間で排他する操作の lease（RAG / NL2SQL と同じ形）。
CONTROL_STATEMENTS: tuple[str, ...] = (
    f"""
    CREATE TABLE {MIGRATION_TABLE} (
        MIGRATION_NAME VARCHAR2(128) NOT NULL,
        DESCRIPTION VARCHAR2(256) NOT NULL,
        CHECKSUM VARCHAR2(64) NOT NULL,
        APPLIED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        CONSTRAINT PK_AGENT_SCHEMA_MIGRATIONS PRIMARY KEY (MIGRATION_NAME)
    )
    """,
    f"""
    CREATE TABLE {CONTROL_TABLE} (
        OPERATION_KEY VARCHAR2(32) NOT NULL,
        STATUS VARCHAR2(16) DEFAULT 'IDLE' NOT NULL,
        OPERATION_KIND VARCHAR2(16),
        LEASE_OWNER VARCHAR2(64),
        LEASE_EXPIRES_AT TIMESTAMP WITH TIME ZONE,
        LAST_ERROR_CODE VARCHAR2(64),
        SCHEMA_EPOCH NUMBER(19) DEFAULT 0 NOT NULL,
        UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        CONSTRAINT PK_AGENT_SCHEMA_OPERATIONS PRIMARY KEY (OPERATION_KEY),
        CONSTRAINT CK_AGENT_SCHEMA_OP_STATUS CHECK (STATUS IN ('IDLE', 'RUNNING', 'FAILED'))
    )
    """,
    f"""
    INSERT INTO {CONTROL_TABLE} (OPERATION_KEY, STATUS, SCHEMA_EPOCH)
    VALUES ('system_schema', 'IDLE', 0)
    """,
)

# ロールに付ける Agent の権限と対象範囲（#215 / #750）。ロール本体は共通の PLATFORM_ROLES。
# エージェントは Runtime repository の状態のため、AGENT_ID に FK は持てない。エージェントの削除で
# `remove_agent_assignments` が AGENT_ID で消すため、AGENT_ID の index を持つ。
BASE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE AGENT_ROLE_PERMISSIONS (
        ROLE_ID VARCHAR2(36) NOT NULL,
        PERMISSION_CODE VARCHAR2(128) NOT NULL,
        CONSTRAINT PK_AGENT_ROLE_PERMISSIONS PRIMARY KEY (ROLE_ID, PERMISSION_CODE),
        CONSTRAINT FK_AGENT_ROLE_PERMISSIONS_ROLE FOREIGN KEY (ROLE_ID)
            REFERENCES PLATFORM_ROLES (ROLE_ID) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE AGENT_ROLE_AGENTS (
        ROLE_ID VARCHAR2(36) NOT NULL,
        AGENT_ID VARCHAR2(128) NOT NULL,
        CONSTRAINT PK_AGENT_ROLE_AGENTS PRIMARY KEY (ROLE_ID, AGENT_ID),
        CONSTRAINT FK_AGENT_ROLE_AGENTS_ROLE FOREIGN KEY (ROLE_ID)
            REFERENCES PLATFORM_ROLES (ROLE_ID) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX AGENT_ROLE_AGENTS_AGENT_IDX ON AGENT_ROLE_AGENTS (AGENT_ID)",
)

# Run・業務 Agent の保存先と、oracle_normalized の監査用の projection（#764）。名前は
# `app.features.agent.runtime` の RUNTIME_CHECKPOINT_TABLE / RUNTIME_PROJECTION_PREFIX と、
# `runtime_checkpoint_store.ENTITY_TABLE` と同じ。
#
# AGENT_RUNTIME_CHECKPOINTS は保存先の head（#1939 から）: VERSION_NO は書くたびに 1 つ増やす版、
# WRITER_ID は最後に書いたプロセス（#1916）、SNAPSHOT_JSON は小さな header（形式・全体を書き直した
# 版・読み込みで退避した記録）。書くプロセスは head の行を lock してから書く。
# AGENT_RUNTIME_ENTITIES は Run・業務 Agent の 1 件 = 1 行（ENTITY_KIND は run / agent。
# migration 016 が移す前の snapshot を legacy_snapshot として残す）。CHANGE_VERSION はその行を
# 書いたときの head の版で、別のプロセスの変更（前回の同期の版より新しい行）を読むのに使う
# （`runtime_checkpoint_store`）。削除は行を消さずに ENTITY_JSON を NULL にする。
_RUNTIME_CHECKPOINTS_TABLE = """
    CREATE TABLE AGENT_RUNTIME_CHECKPOINTS (
        CHECKPOINT_KEY VARCHAR2(128) PRIMARY KEY,
        SNAPSHOT_JSON CLOB NOT NULL,
        UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        VERSION_NO NUMBER(19) DEFAULT 0 NOT NULL,
        WRITER_ID VARCHAR2(200)
    )
    """
# migration 006 が作った形（checksum に入るため固定する。最新の形は _RUNTIME_CHECKPOINTS_TABLE）。
_RUNTIME_CHECKPOINTS_TABLE_006 = """
    CREATE TABLE AGENT_RUNTIME_CHECKPOINTS (
        CHECKPOINT_KEY VARCHAR2(128) PRIMARY KEY,
        SNAPSHOT_JSON CLOB NOT NULL,
        UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
    )
    """
_RUNTIME_ENTITIES_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE AGENT_RUNTIME_ENTITIES (
        CHECKPOINT_KEY VARCHAR2(128) NOT NULL,
        ENTITY_KIND VARCHAR2(32) NOT NULL,
        ENTITY_ID VARCHAR2(200) NOT NULL,
        ENTITY_JSON CLOB,
        CHANGE_VERSION NUMBER(19) NOT NULL,
        WRITER_ID VARCHAR2(200),
        UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        CONSTRAINT PK_AGENT_RUNTIME_ENTITIES PRIMARY KEY (CHECKPOINT_KEY, ENTITY_KIND, ENTITY_ID)
    )
    """,
    "CREATE INDEX AGENT_RUNTIME_ENTITIES_CHANGE_IX "
    "ON AGENT_RUNTIME_ENTITIES (CHECKPOINT_KEY, CHANGE_VERSION)",
)
RUNTIME_STATEMENTS: tuple[str, ...] = (
    _RUNTIME_CHECKPOINTS_TABLE,
    *_RUNTIME_ENTITIES_STATEMENTS,
    """
    CREATE TABLE AGENT_RUNTIME_RUNS (
        RUN_ID VARCHAR2(128) PRIMARY KEY,
        AGENT_ID VARCHAR2(128) NOT NULL,
        STATUS VARCHAR2(32) NOT NULL,
        GOAL CLOB NOT NULL,
        METADATA_JSON CLOB,
        PENDING_TOOL_CALLS_JSON CLOB,
        CREATED_AT TIMESTAMP WITH TIME ZONE NOT NULL,
        UPDATED_AT TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    """
    CREATE TABLE AGENT_RUNTIME_EVENTS (
        EVENT_ID VARCHAR2(128) PRIMARY KEY,
        RUN_ID VARCHAR2(128) NOT NULL,
        EVENT_TYPE VARCHAR2(128) NOT NULL,
        MESSAGE CLOB NOT NULL,
        PAYLOAD_JSON CLOB,
        CREATED_AT TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    """
    CREATE TABLE AGENT_RUNTIME_STEPS (
        STEP_ID VARCHAR2(128) PRIMARY KEY,
        RUN_ID VARCHAR2(128) NOT NULL,
        KIND VARCHAR2(64) NOT NULL,
        STATUS VARCHAR2(32) NOT NULL,
        TOOL_NAME VARCHAR2(256),
        APPROVAL_ID VARCHAR2(128),
        TOOL_CALL_JSON CLOB,
        TOOL_RESULT_JSON CLOB,
        STARTED_AT TIMESTAMP WITH TIME ZONE,
        COMPLETED_AT TIMESTAMP WITH TIME ZONE
    )
    """,
    """
    CREATE TABLE AGENT_RUNTIME_APPROVALS (
        APPROVAL_ID VARCHAR2(128) PRIMARY KEY,
        RUN_ID VARCHAR2(128) NOT NULL,
        STEP_ID VARCHAR2(128) NOT NULL,
        TOOL_NAME VARCHAR2(256) NOT NULL,
        STATUS VARCHAR2(32) NOT NULL,
        REASON CLOB NOT NULL,
        DECIDED_BY VARCHAR2(256),
        DECIDED_AT TIMESTAMP WITH TIME ZONE,
        CREATED_AT TIMESTAMP WITH TIME ZONE NOT NULL,
        TOOL_CALL_JSON CLOB NOT NULL
    )
    """,
    """
    CREATE TABLE AGENT_RUNTIME_ARTIFACTS (
        ARTIFACT_ID VARCHAR2(128) PRIMARY KEY,
        RUN_ID VARCHAR2(128) NOT NULL,
        NAME VARCHAR2(512) NOT NULL,
        KIND VARCHAR2(128) NOT NULL,
        CONTENT_JSON CLOB NOT NULL,
        CREATED_AT TIMESTAMP WITH TIME ZONE NOT NULL
    )
    """,
    "CREATE INDEX AGENT_RUNTIME_RUNS_STATUS_CREATED_IX ON AGENT_RUNTIME_RUNS (STATUS, CREATED_AT)",
    "CREATE INDEX AGENT_RUNTIME_EVENTS_RUN_TYPE_CREATED_IX "
    "ON AGENT_RUNTIME_EVENTS (RUN_ID, EVENT_TYPE, CREATED_AT)",
    "CREATE INDEX AGENT_RUNTIME_STEPS_RUN_TOOL_STATUS_IX "
    "ON AGENT_RUNTIME_STEPS (RUN_ID, TOOL_NAME, STATUS, COMPLETED_AT)",
    "CREATE INDEX AGENT_RUNTIME_STEPS_ERROR_CODE_IX ON AGENT_RUNTIME_STEPS "
    "(JSON_VALUE(TOOL_RESULT_JSON, '$.error_code' RETURNING VARCHAR2(128)))",
    "CREATE INDEX AGENT_RUNTIME_APPROVALS_RUN_STATUS_IX "
    "ON AGENT_RUNTIME_APPROVALS (RUN_ID, STATUS, CREATED_AT)",
    "CREATE INDEX AGENT_RUNTIME_ARTIFACTS_RUN_KIND_IX "
    "ON AGENT_RUNTIME_ARTIFACTS (RUN_ID, KIND, CREATED_AT)",
)

# migration 006 が作った Run の保存先の形（checksum に入るため固定する。行の表は 016 が作る）。
_RUNTIME_STATEMENTS_006: tuple[str, ...] = (
    _RUNTIME_CHECKPOINTS_TABLE_006,
    *RUNTIME_STATEMENTS[1 + len(_RUNTIME_ENTITIES_STATEMENTS) :],
)

# checkpoint の版と最後に書いたプロセス（#1916。migration 015）。既存のテーブルに列を足す
# （データは消さない。既存の行の版は 0 から始まる）。これから checkpoint の JSON を書き換える
# migration は `VERSION_NO = VERSION_NO + 1` も行う（別のプロセスが古いメモリで書き戻さず、
# まとめ直すように）。
RUNTIME_CHECKPOINT_VERSION_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE AGENT_RUNTIME_CHECKPOINTS ADD (VERSION_NO NUMBER(19) DEFAULT 0 NOT NULL)",
    "ALTER TABLE AGENT_RUNTIME_CHECKPOINTS ADD (WRITER_ID VARCHAR2(200))",
)

# 全体の snapshot（1 行の JSON）を、Run・業務 Agent の 1 件 = 1 行に移す（#1939。migration 016）。
# - 移す前の snapshot はそのまま AGENT_RUNTIME_ENTITIES の legacy_snapshot の行に残す（データを
#   失わない。アプリは読まない。全再作成で消える）。
# - runs / agents の要素を、id ごとに 1 行にする（id の無い要素・同じ id の 2 つ目以降は移さない。
#   読み込みが退避の記録に移す前の形でだけ起きる。元の snapshot に残る）。
# - head の SNAPSHOT_JSON は runs / agents を除いた header にし（退避の記録 quarantined は残す）、
#   版を 1 つ進める。移した行の CHANGE_VERSION は進めた版。
# 対象は移す前の形の行だけ（`$.storage` が無く、JSON として読める）。もう一度当てても変えない。
# 移した後の行の ORA-00001 は起きない（同じ id は 1 行だけ選ぶ）。
_LEGACY_CHECKPOINT_FILTER = (
    "c.SNAPSHOT_JSON IS JSON AND JSON_VALUE(c.SNAPSHOT_JSON, '$.storage') IS NULL"
)


def _move_snapshot_items_sql(kind: str, path: str) -> str:
    return (
        "INSERT INTO AGENT_RUNTIME_ENTITIES (CHECKPOINT_KEY, ENTITY_KIND, ENTITY_ID, ENTITY_JSON, "
        "CHANGE_VERSION, WRITER_ID) "
        f"SELECT CHECKPOINT_KEY, '{kind}', ENTITY_ID, ENTITY_JSON, VERSION_NO + 1, "
        "'migration:20261015_016' FROM ("
        "SELECT c.CHECKPOINT_KEY, c.VERSION_NO, jt.ENTITY_ID, jt.ENTITY_JSON, "
        "ROW_NUMBER() OVER (PARTITION BY c.CHECKPOINT_KEY, jt.ENTITY_ID ORDER BY jt.ITEM_NO) "
        "AS ITEM_RANK "
        "FROM AGENT_RUNTIME_CHECKPOINTS c, "
        f"JSON_TABLE(c.SNAPSHOT_JSON, '{path}' COLUMNS ("
        "ITEM_NO FOR ORDINALITY, "
        "ENTITY_ID VARCHAR2(200) PATH '$.id', "
        "ENTITY_JSON CLOB FORMAT JSON PATH '$')) jt "
        f"WHERE {_LEGACY_CHECKPOINT_FILTER}"
        ") WHERE ITEM_RANK = 1 AND ENTITY_ID IS NOT NULL"
    )


RUNTIME_ENTITY_MIGRATION_STATEMENTS: tuple[str, ...] = (
    *_RUNTIME_ENTITIES_STATEMENTS,
    "INSERT INTO AGENT_RUNTIME_ENTITIES (CHECKPOINT_KEY, ENTITY_KIND, ENTITY_ID, ENTITY_JSON, "
    "CHANGE_VERSION, WRITER_ID) "
    "SELECT c.CHECKPOINT_KEY, 'legacy_snapshot', '20261015_016', c.SNAPSHOT_JSON, "
    "c.VERSION_NO + 1, 'migration:20261015_016' FROM AGENT_RUNTIME_CHECKPOINTS c "
    f"WHERE {_LEGACY_CHECKPOINT_FILTER}",
    _move_snapshot_items_sql("run", "$.runs[*]"),
    _move_snapshot_items_sql("agent", "$.agents[*]"),
    "UPDATE AGENT_RUNTIME_CHECKPOINTS c SET c.SNAPSHOT_JSON = JSON_TRANSFORM(c.SNAPSHOT_JSON, "
    "REMOVE '$.runs', REMOVE '$.agents', REMOVE '$.memory', REMOVE '$.control_plane_state', "
    "SET '$.storage' = 'entities' RETURNING CLOB), "
    "c.VERSION_NO = c.VERSION_NO + 1, c.WRITER_ID = 'migration:20261015_016', "
    "c.UPDATED_AT = SYSTIMESTAMP "
    f"WHERE {_LEGACY_CHECKPOINT_FILTER}",
)

# 画面・API で変えた定義（#764）。ITEM_KIND は skill / mcp_connection / tool_policy /
# evaluation_set / evaluation_job。ITEM_JSON は定義の JSON
# （MCP 接続の秘密は暗号化した値）。
CONTROL_PLANE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE AGENT_CONTROL_PLANE_ITEMS (
        ITEM_KIND VARCHAR2(32) NOT NULL,
        ITEM_ID VARCHAR2(200) NOT NULL,
        ITEM_JSON CLOB NOT NULL,
        UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        CONSTRAINT PK_AGENT_CONTROL_PLANE_ITEMS PRIMARY KEY (ITEM_KIND, ITEM_ID)
    )
    """,
)

# Run の事実（1 Run = 1 行。#794）。利用状況・フィードバックを期間（〜365 日）・日・業務 Agent・
# 利用者・モデルで SQL 集計する（名前は `run_facts_store.RUN_FACTS_TABLE` と同じ）。
# 日時は UTC の TIMESTAMP（タイムゾーンなし。session のタイムゾーンに依らない）。RATED_AT は
# 本人の評価と管理者の評価の新しい方（フィードバックの期間と一覧の並び）。Run の snapshot
# （AGENT_RUNTIME_CHECKPOINTS）とは別に残る分析用の履歴で、Run を整理しても集計は残る。
RUN_FACTS_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE AGENT_RUN_FACTS (
        RUN_ID VARCHAR2(128) NOT NULL,
        AGENT_ID VARCHAR2(128) NOT NULL,
        AGENT_NAME VARCHAR2(400 CHAR),
        AGENT_VERSION VARCHAR2(32),
        THREAD_ID VARCHAR2(128),
        CREATED_BY_USER_UUID VARCHAR2(64),
        STATUS VARCHAR2(32) NOT NULL,
        RUN_SOURCE VARCHAR2(32) NOT NULL,
        MODEL VARCHAR2(256 CHAR),
        REQUESTS NUMBER(10),
        INPUT_TOKENS NUMBER(19),
        OUTPUT_TOKENS NUMBER(19),
        TOTAL_TOKENS NUMBER(19),
        FEEDBACK_RATING VARCHAR2(16),
        FEEDBACK_REASON VARCHAR2(32),
        FEEDBACK_COMMENT VARCHAR2(1000 CHAR),
        FEEDBACK_USER_UUID VARCHAR2(64),
        FEEDBACK_AT TIMESTAMP,
        ADMIN_RATING VARCHAR2(16),
        ADMIN_REASON VARCHAR2(32),
        ADMIN_COMMENT VARCHAR2(1000 CHAR),
        ADMIN_USER_UUID VARCHAR2(64),
        ADMIN_AT TIMESTAMP,
        RATED_AT TIMESTAMP,
        QUESTION CLOB NOT NULL,
        ANSWER CLOB,
        CREATED_AT TIMESTAMP NOT NULL,
        FINISHED_AT TIMESTAMP,
        UPDATED_AT TIMESTAMP NOT NULL,
        ANSWER_OUTCOME VARCHAR2(32),
        VALIDATION_RESULT VARCHAR2(16),
        TOOL_FAILURES NUMBER(10),
        APPROVALS_REJECTED NUMBER(10),
        DATA_TOOL_CALLS NUMBER(10),
        CONSTRAINT PK_AGENT_RUN_FACTS PRIMARY KEY (RUN_ID)
    )
    """,
    "CREATE INDEX AGENT_RUN_FACTS_CREATED_IX ON AGENT_RUN_FACTS (CREATED_AT)",
    "CREATE INDEX AGENT_RUN_FACTS_AGENT_CREATED_IX ON AGENT_RUN_FACTS (AGENT_ID, CREATED_AT)",
    "CREATE INDEX AGENT_RUN_FACTS_RATED_IX ON AGENT_RUN_FACTS (RATED_AT)",
)

# 確かめるべき実行のシグナルの列（#1822。migration 012）。既存のテーブルに足す（データは消さない）。
# ANSWER_OUTCOME は回答の対応（#1305）、VALIDATION_RESULT は最終の検証の結果
# （`run_facts.VALIDATION_*`）、TOOL_FAILURES / APPROVALS_REJECTED は失敗したツールの呼び出しと
# 却下した承認の数。
RUN_FACTS_SIGNAL_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE AGENT_RUN_FACTS ADD (ANSWER_OUTCOME VARCHAR2(32))",
    "ALTER TABLE AGENT_RUN_FACTS ADD (VALIDATION_RESULT VARCHAR2(16))",
    "ALTER TABLE AGENT_RUN_FACTS ADD (TOOL_FAILURES NUMBER(10))",
    "ALTER TABLE AGENT_RUN_FACTS ADD (APPROVALS_REJECTED NUMBER(10))",
)

# 業務データの道具（NL2SQL など RAG 以外の MCP 接続のツール）の成功した呼び出しの数（#1834。
# migration 014）。確かめるべき実行の「根拠を使わずに答えた」で、業務データの道具で答えた Run を
# 除く。
RUN_FACTS_DATA_TOOL_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE AGENT_RUN_FACTS ADD (DATA_TOOL_CALLS NUMBER(10))",
)

# migration 007 が作った形（checksum に入るため固定する。最新の形は RUN_FACTS_STATEMENTS）。
_RUN_FACTS_STATEMENTS_007: tuple[str, ...] = (
    """
    CREATE TABLE AGENT_RUN_FACTS (
        RUN_ID VARCHAR2(128) NOT NULL,
        AGENT_ID VARCHAR2(128) NOT NULL,
        AGENT_NAME VARCHAR2(400 CHAR),
        AGENT_VERSION VARCHAR2(32),
        THREAD_ID VARCHAR2(128),
        CREATED_BY_USER_UUID VARCHAR2(64),
        STATUS VARCHAR2(32) NOT NULL,
        RUN_SOURCE VARCHAR2(32) NOT NULL,
        MODEL VARCHAR2(256 CHAR),
        REQUESTS NUMBER(10),
        INPUT_TOKENS NUMBER(19),
        OUTPUT_TOKENS NUMBER(19),
        TOTAL_TOKENS NUMBER(19),
        FEEDBACK_RATING VARCHAR2(16),
        FEEDBACK_REASON VARCHAR2(32),
        FEEDBACK_COMMENT VARCHAR2(1000 CHAR),
        FEEDBACK_USER_UUID VARCHAR2(64),
        FEEDBACK_AT TIMESTAMP,
        ADMIN_RATING VARCHAR2(16),
        ADMIN_REASON VARCHAR2(32),
        ADMIN_COMMENT VARCHAR2(1000 CHAR),
        ADMIN_USER_UUID VARCHAR2(64),
        ADMIN_AT TIMESTAMP,
        RATED_AT TIMESTAMP,
        QUESTION CLOB NOT NULL,
        ANSWER CLOB,
        CREATED_AT TIMESTAMP NOT NULL,
        FINISHED_AT TIMESTAMP,
        UPDATED_AT TIMESTAMP NOT NULL,
        CONSTRAINT PK_AGENT_RUN_FACTS PRIMARY KEY (RUN_ID)
    )
    """,
    "CREATE INDEX AGENT_RUN_FACTS_CREATED_IX ON AGENT_RUN_FACTS (CREATED_AT)",
    "CREATE INDEX AGENT_RUN_FACTS_AGENT_CREATED_IX ON AGENT_RUN_FACTS (AGENT_ID, CREATED_AT)",
    "CREATE INDEX AGENT_RUN_FACTS_RATED_IX ON AGENT_RUN_FACTS (RATED_AT)",
)

# 未初期化の DB と全再作成で作る、Agent の管理対象の DDL の全体。
DOMAIN_STATEMENTS: tuple[str, ...] = (
    *BASE_STATEMENTS,
    *RUNTIME_STATEMENTS,
    *CONTROL_PLANE_STATEMENTS,
    *RUN_FACTS_STATEMENTS,
)


@dataclass(frozen=True, slots=True)
class MigrationArtifact:
    """台帳に記録する 1 migration（checksum は SQL から決まる）。"""

    name: str
    description: str
    statements: tuple[str, ...]
    # データを消す migration の説明（空なら破壊的でない。checksum には含めない。#619）。
    destructive_note: str = ""
    # 書き換える保存済みのデータ（`RUNTIME_DATA` / `CONTROL_PLANE_DATA`）。動いている backend の
    # メモリを適用の後に読み直す（#1844。checksum には含めない）。
    rewrites: frozenset[str] = frozenset()

    @property
    def checksum(self) -> str:
        normalized = "\n;\n".join(" ".join(statement.split()) for statement in self.statements)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def destructive(self) -> bool:
        return bool(self.destructive_note)


def _retired_codes_sql(codes: tuple[str, ...]) -> str:
    return ", ".join(f"'{code}'" for code in codes)


# migration の SQL は checksum に入るため、適用済みの migration の対象は固定する。
# 新しく廃止したコードは新しい migration で消す（全体は RETIRED_PERMISSION_CODES）。
_RETIRED_CODES_002 = ("menu.dashboard",)
_RETIRED_CODES_004 = ("menu.settings_external_rag", "menu.settings_external_nl2sql")
_RETIRED_CODES_005 = ("menu.settings_connection",)
# 008 は削除ではなく `menu.plugins` への置き換え（ロールの割り当てを引き継ぐ。#1798）。
_RETIRED_CODES_008 = ("menu.plugin_marketplaces",)
_RETIRED_CODES_010 = ("menu.plugins",)
_RETIRED_CODES_011 = ("menu.settings_api_keys",)
_RETIRED_CODES_017 = ("menu.settings_runtime_snapshot",)
_RETIRED_CODES_018 = ("menu.automations",)
_RETIRED_CODES_019 = ("menu.settings_appearance",)
if set(_RETIRED_CODES_002) | set(_RETIRED_CODES_004) | set(_RETIRED_CODES_005) | set(
    _RETIRED_CODES_008
) | set(_RETIRED_CODES_010) | set(_RETIRED_CODES_011) | set(_RETIRED_CODES_017) | set(
    _RETIRED_CODES_018
) | set(_RETIRED_CODES_019) != set(
    RETIRED_PERMISSION_CODES
):  # pragma: no cover - 定義の誤りを起動時に検出
    raise RuntimeError("廃止した権限コードを消す migration を追加してください。")

# 013 で改名する組み込みのスキルの ID（旧 → 新。#1816）。migration の SQL は checksum に
# 入るため固定する。
_RENAMED_SKILL_IDS_013: tuple[tuple[str, str], ...] = (
    ("business_rag_research", "business-rag-research"),
    ("structured_data_query", "structured-data-query"),
    ("rag_and_structured_data", "rag-and-structured-data"),
    # #1573 で `rag_and_structured_data` に改名した旧 ID（読み込みの読み替えは #1816 で消した）。
    ("rag_then_structured_data", "rag-and-structured-data"),
)


def _replace_skill_ids_sql(column: str, renames: tuple[tuple[str, str], ...]) -> str:
    """JSON の文字列値（引用符つきの ID）を置き換える入れ子の REPLACE（CLOB のまま）。"""
    expression = column
    for old, new in renames:
        expression = f"REPLACE({expression}, '\"{old}\"', '\"{new}\"')"
    return expression


# 未初期化の DB（`missing`）と全再作成では、正本の DDL で最新の形を作り、migration は実行せずに
# 記録だけする。それ以外は未適用・checksum 不一致の migration を順に適用する。
MIGRATIONS: tuple[MigrationArtifact, ...] = (
    MigrationArtifact(
        "20261002_001_role_access",
        "agent role permissions and agent scope",
        BASE_STATEMENTS,
    ),
    MigrationArtifact(
        "20261002_002_remove_retired_permission_codes",
        "remove retired permission codes from roles",
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_002)})",
        ),
    ),
    MigrationArtifact(
        "20261002_003_retire_role_business_views",
        "retire agent role business views",
        ("DROP TABLE AGENT_ROLE_BUSINESS_VIEWS CASCADE CONSTRAINTS PURGE",),
        destructive_note=(
            "権限管理でロールに割り当てていた業務ビュー（AGENT_ROLE_BUSINESS_VIEWS）を削除します。"
            "#750 から使っていません。業務ビューの権限は RAG の権限管理で割り当てます。"
            "割り当てを控える必要があれば、先にテーブルを書き出してください。"
        ),
    ),
    MigrationArtifact(
        "20261002_004_remove_external_settings_permissions",
        "remove external RAG / NL2SQL menu permissions (merged into MCP connections)",
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_004)})",
        ),
    ),
    MigrationArtifact(
        "20261002_005_remove_connection_settings_permission",
        "remove the placeholder agent connection settings menu permission",
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_005)})",
        ),
    ),
    MigrationArtifact(
        "20261002_006_runtime_and_control_plane_tables",
        "manage run checkpoint / projection tables and add control plane items (#764)",
        # 以前は Runtime repository が作っていた。既存のテーブルは ORA-00955 として読み飛ばす。
        (*_RUNTIME_STATEMENTS_006, *CONTROL_PLANE_STATEMENTS),
    ),
    MigrationArtifact(
        "20261003_007_run_facts",
        "add run facts for usage / feedback reports beyond the in-memory runs (#794)",
        # 既存の Run の事実は、アプリの起動時の backfill が Runtime repository から書く。
        _RUN_FACTS_STATEMENTS_007,
    ),
    MigrationArtifact(
        "20261014_008_remove_plugin_distribution_sources",
        "replace the plugin distribution source menu permission with menu.plugins and "
        "remove its stored sources (#1798)",
        # プラグインの配布元の機能（画面・API・ITEM_KIND）を削除した。ロールのメニュー権限は
        # プラグインの画面の `menu.plugins` へ移し、読む処理の無くなった配布元の定義の行を消す
        # （導入済みのプラグインの行 `ITEM_KIND = 'plugin'` は残す）。読まれないだけの設定のため、
        # 破壊的な migration にはしない。
        (
            "INSERT INTO AGENT_ROLE_PERMISSIONS (ROLE_ID, PERMISSION_CODE) "  # nosec B608 - 固定値
            "SELECT retired.ROLE_ID, 'menu.plugins' FROM AGENT_ROLE_PERMISSIONS retired "
            f"WHERE retired.PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_008)}) "
            "AND NOT EXISTS (SELECT 1 FROM AGENT_ROLE_PERMISSIONS current_code "
            "WHERE current_code.ROLE_ID = retired.ROLE_ID "
            "AND current_code.PERMISSION_CODE = 'menu.plugins')",
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_008)})",
            "DELETE FROM AGENT_CONTROL_PLANE_ITEMS WHERE ITEM_KIND = 'marketplace'",
        ),
        rewrites=frozenset({CONTROL_PLANE_DATA}),
    ),
    MigrationArtifact(
        "20261014_009_remove_automation_webhook",
        "remove webhook automations and the trigger / webhook fields of automations (#1799)",
        # 自動実行のトリガーはスケジュールだけになった（#1799）。Webhook の自動実行は実行の手段が
        # 無くなり復元もできないので消し、残る定義の JSON からトリガーと Webhook の秘密の
        # 項目を除く。使えなくなった定義の後始末なので、承認は求めない
        # （廃止した権限コードの削除と同じ）。
        (
            "DELETE FROM AGENT_CONTROL_PLANE_ITEMS WHERE ITEM_KIND = 'automation' "
            "AND JSON_VALUE(ITEM_JSON, '$.trigger') = 'webhook'",
            "UPDATE AGENT_CONTROL_PLANE_ITEMS SET ITEM_JSON = JSON_TRANSFORM(ITEM_JSON, "
            "REMOVE '$.trigger', REMOVE '$.webhook_token_hash', REMOVE '$.webhook_token_prefix' "
            "RETURNING CLOB) WHERE ITEM_KIND = 'automation'",
        ),
        rewrites=frozenset({CONTROL_PLANE_DATA}),
    ),
    MigrationArtifact(
        "20261014_010_remove_plugins",
        "remove the plugin menu permission and the installed plugins (#1803)",
        # プラグインの機能を削除した（スキルだけを持つ）。ロールのメニュー権限と、導入済みの
        # プラグインの定義の行を消す。
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_010)})",
            "DELETE FROM AGENT_CONTROL_PLANE_ITEMS WHERE ITEM_KIND = 'plugin'",
        ),
        rewrites=frozenset({CONTROL_PLANE_DATA}),
        destructive_note=(
            "導入済みのプラグイン（AGENT_CONTROL_PLANE_ITEMS の ITEM_KIND = 'plugin' の行）と、"
            "ロールのメニュー権限「プラグイン」を削除します。#1803 でプラグインの機能を削除し、"
            "読む処理はありません。プラグインが配っていたスキル・MCP 接続を使い続けるなら、"
            "先にスキル・MCP 接続として登録し直してください。"
        ),
    ),
    MigrationArtifact(
        "20261014_011_remove_agent_mcp_api_keys",
        "remove the api key menu permission and the stored api keys (#1809)",
        # Agent は外部に MCP を公開しない（`POST /mcp` と API キーを削除した。#1809）。ロールの
        # メニュー権限「API キー」と、読む処理の無くなった API キーの行（秘密の hash だけ）を消す。
        # キーは `POST /mcp` が無くなった時点で使えないため、破壊的な migration にはしない。
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_011)})",
            "DELETE FROM AGENT_CONTROL_PLANE_ITEMS WHERE ITEM_KIND = 'api_key'",
        ),
        rewrites=frozenset({CONTROL_PLANE_DATA}),
    ),
    MigrationArtifact(
        "20261014_012_run_fact_signals",
        "add answer outcome / validation result / tool failure / rejected approval columns "
        "to run facts for the runs to review (#1822)",
        # 列を足すだけ（データは消さない）。既存の Run の事実の新しい列は、アプリの起動時の
        # backfill が Runtime repository にある Run から書く（保存期間の外の Run は空のまま）。
        RUN_FACTS_SIGNAL_STATEMENTS,
    ),
    MigrationArtifact(
        "20261014_013_rename_builtin_skill_ids",
        "rename the builtin skill ids to agent skills names in saved agents (#1816)",
        # 組み込みのスキルを Agent Skills の形式（repo 直下の skills/<name>/SKILL.md）にし、ID を
        # 仕様の `name`（小文字・数字・ハイフン）に変えた。保存済みの業務 Agent（下書きと各版）の
        # `skill_ids` は Run・業務 Agent の checkpoint の JSON の中にあるため、JSON の文字列値
        # （引用符つき）を置き換える。#1573 で改名した旧 ID も同じ新 ID にする（重なった ID は
        # 読み込みで 1 つにする）。データは消さないので、破壊的な migration にはしない。
        (
            "UPDATE AGENT_RUNTIME_CHECKPOINTS SET SNAPSHOT_JSON = "
            + _replace_skill_ids_sql("SNAPSHOT_JSON", _RENAMED_SKILL_IDS_013)
            + " WHERE "
            + " OR ".join(
                f"DBMS_LOB.INSTR(SNAPSHOT_JSON, '\"{old}\"') > 0"
                for old, _new in _RENAMED_SKILL_IDS_013
            ),
        ),
        rewrites=frozenset({RUNTIME_DATA}),
    ),
    MigrationArtifact(
        "20261014_014_run_fact_data_tool_calls",
        "add the data tool call count to run facts for the ungrounded answers to review (#1834)",
        # 列を足すだけ（データは消さない）。既存の Run の事実は、アプリの起動時の backfill が
        # Runtime repository にある Run から書く（保存期間の外の Run は空のままで、シグナルに
        # しない）。
        RUN_FACTS_DATA_TOOL_STATEMENTS,
    ),
    MigrationArtifact(
        "20261015_015_runtime_checkpoint_version",
        "add the version and writer columns to the run checkpoint so that two processes "
        "do not overwrite each other's updates (#1916)",
        # 列を足すだけ（データは消さない）。動いている backend は適用の後に checkpoint を読み直し、
        # その時点から版を確かめて書く（列が無いあいだは今までどおり確かめずに書き、警告を残す）。
        RUNTIME_CHECKPOINT_VERSION_STATEMENTS,
        rewrites=frozenset({RUNTIME_DATA}),
    ),
    MigrationArtifact(
        "20261015_016_runtime_entities",
        "store runs and business agents one row each instead of rewriting the whole snapshot "
        "on every change (#1939)",
        # 全体の snapshot を Run・業務 Agent の行に移す（データは消さない。移す前の snapshot は
        # legacy_snapshot の行に残す）。動いている backend は適用の後に保存先を読み直す（適用の
        # 前は業務の API が 503 で、この更新へ案内する）。
        RUNTIME_ENTITY_MIGRATION_STATEMENTS,
        rewrites=frozenset({RUNTIME_DATA}),
    ),
    MigrationArtifact(
        "20261015_017_remove_runtime_snapshot_menu",
        "remove the backup and restore menu permission (#1961)",
        # 運用設定の「バックアップと復元」（snapshot の書き出し・取り込み）を削除した。ロールの
        # メニュー権限だけを消す（データは消さない）。
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_017)})",
        ),
    ),
    MigrationArtifact(
        "20261031_018_remove_scheduled_runs_menu",
        "remove the scheduled run menu permission (#20)",
        # 業務 Agent の定時実行の機能を削除した（スケジューラ・API・画面を消した）。ロールの
        # メニュー権限だけを消す。定義を保存していた AGENT_CONTROL_PLANE_ITEMS の行は残す
        # （読む処理が無いので害は無く、消すと復元できないため）。
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_018)})",
        ),
    ),
    MigrationArtifact(
        "20261031_019_remove_appearance_menu",
        "remove the appearance and certificate menu permission (#19)",
        # Nginx と自作 CA の TLS 証明書を配備から外し、システム設定の「外観と証明書」の画面を
        # 削除した。ロールのメニュー権限だけを消す（データは消さない）。
        (
            "DELETE FROM AGENT_ROLE_PERMISSIONS "  # nosec B608 - 固定の権限コード
            f"WHERE PERMISSION_CODE IN ({_retired_codes_sql(_RETIRED_CODES_019)})",
        ),
    ),
)

# ---- manifest -----------------------------------------------------------------

# 作成の依存順。全再作成はこの逆順で DROP する（制御テーブルは残す）。
MANAGED_TABLES: tuple[str, ...] = (
    CONTROL_TABLE,
    MIGRATION_TABLE,
    "AGENT_ROLE_PERMISSIONS",
    "AGENT_ROLE_AGENTS",
    "AGENT_RUNTIME_CHECKPOINTS",
    "AGENT_RUNTIME_ENTITIES",
    "AGENT_RUNTIME_RUNS",
    "AGENT_RUNTIME_EVENTS",
    "AGENT_RUNTIME_STEPS",
    "AGENT_RUNTIME_APPROVALS",
    "AGENT_RUNTIME_ARTIFACTS",
    "AGENT_CONTROL_PLANE_ITEMS",
    "AGENT_RUN_FACTS",
)
MANAGED_INDEXES: tuple[str, ...] = (
    "AGENT_ROLE_AGENTS_AGENT_IDX",
    "AGENT_RUNTIME_ENTITIES_CHANGE_IX",
    "AGENT_RUNTIME_RUNS_STATUS_CREATED_IX",
    "AGENT_RUNTIME_EVENTS_RUN_TYPE_CREATED_IX",
    "AGENT_RUNTIME_STEPS_RUN_TOOL_STATUS_IX",
    "AGENT_RUNTIME_STEPS_ERROR_CODE_IX",
    "AGENT_RUNTIME_APPROVALS_RUN_STATUS_IX",
    "AGENT_RUNTIME_ARTIFACTS_RUN_KIND_IX",
    "AGENT_RUN_FACTS_CREATED_IX",
    "AGENT_RUN_FACTS_AGENT_CREATED_IX",
    "AGENT_RUN_FACTS_RATED_IX",
)
MANAGED_OBJECTS: tuple[tuple[str, str], ...] = (
    *((name, "TABLE") for name in MANAGED_TABLES),
    *((name, "INDEX") for name in MANAGED_INDEXES),
)
# 現行では使わないが既存の DB に残りうる object（作成・更新で削除する）。
RETIRED_MANAGED_OBJECTS: tuple[tuple[str, str], ...] = (
    # 権限管理の業務ビュー（#750 で使わなくなった）。更新では migration 003 が消す。
    # 全再作成は migration を実行せず記録だけするため、ここにも載せて残さない。
    ("AGENT_ROLE_BUSINESS_VIEWS", "TABLE"),
)
DOMAIN_TABLES = frozenset(MANAGED_TABLES) - {CONTROL_TABLE, MIGRATION_TABLE}
# 3 製品共通の認証テーブルと会話の添付の表（#2517）。先に作るが管理対象ではないため、
# 全再作成でも削除しない。
PRESERVED_TABLES: tuple[str, ...] = (
    *PLATFORM_AUTH_TABLES,
    "PLATFORM_TRACE_RECORDING_SETTINGS",
    *CHAT_ATTACHMENT_TABLES,
    *TRACE_SPAN_TABLES,
)

MANAGED_FOREIGN_KEYS: tuple[ForeignKeySpec, ...] = tuple(
    foreign_key
    for statement in BASE_STATEMENTS
    for foreign_key in foreign_keys_from_create_table(statement)
)

# 正本の表の列（表名 → 列名。#2458）。CONTROL_STATEMENTS と DOMAIN_STATEMENTS は最新の形なので、
# 状態の確認でこの一覧と USER_TAB_COLUMNS を比べ、台帳が適用済みでも欠けた列があれば
# ready にしない。
MANAGED_COLUMNS: dict[str, tuple[str, ...]] = columns_from_statements(
    (*CONTROL_STATEMENTS, *DOMAIN_STATEMENTS)
)

if set(PRESERVED_TABLES) & set(MANAGED_TABLES):  # pragma: no cover - 定義の誤りを起動時に検出
    raise RuntimeError("共通認証のテーブルを Agent の管理対象に含めないでください。")


def managed_manifest_from_schema() -> set[tuple[str, str]]:
    """DDL の正本の CREATE TABLE / INDEX（manifest と一致することをテストで確かめる）。"""

    objects: set[tuple[str, str]] = set()
    for statement in (*CONTROL_STATEMENTS, *DOMAIN_STATEMENTS):
        if (table := _CREATE_TABLE_PATTERN.match(statement)) is not None:
            objects.add((table.group(1).upper(), "TABLE"))
        if (index := _CREATE_INDEX_PATTERN.match(statement)) is not None:
            objects.add((index.group(1).upper(), "INDEX"))
    return objects


def classify_system_schema_status(
    objects: set[tuple[str, str]],
    applied_checksums: dict[str, str],
    *,
    foreign_keys_current: bool = True,
    columns_current: bool = True,
) -> SystemSchemaStatus:
    """Dictionary / 台帳の snapshot を四つの公開状態へ分類する（規則は platform）。"""

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
    objects: Collection[tuple[str, str]], applied_checksums: Mapping[str, str]
) -> list[str]:
    """作成・更新で削除する廃止した表のうち、Agent が作ったと台帳から分からないもの（#1648）。

    台帳（AGENT_SCHEMA_MIGRATIONS）に記録があれば、この schema は Agent が初期化したもので、廃止した
    表も Agent が作ったものとして承認なしに消す（データを消す migration は別に承認を求める）。記録が
    無い schema（既存の ADB の、旧版の単独の製品・別のアプリケーションと共有する schema など）では、
    同じ名前の表を承認なしに消さない。
    """

    if applied_checksums:
        return []
    return [
        name
        for name, object_type in RETIRED_MANAGED_OBJECTS
        if object_type == "TABLE" and (name, "TABLE") in objects
    ]


def pending_destructive_migrations(
    status: str,
    pending_versions: Sequence[str],
    *,
    retired_tables: Sequence[str] = (),
) -> list[dict[str, str]]:
    """未適用の、データを消す migration（未初期化の DB は実行しないので無い。#619）と、
    台帳に記録の無い廃止した表の削除（状態によらず。#1648）。"""

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
                    "この schema には Agent の台帳（AGENT_SCHEMA_MIGRATIONS）の記録が無く、"
                    "Agent が作った表か確かめられません。別のアプリケーションや旧版の表なら、"
                    "Agent を専用の schema（DB ユーザー）に配備してください。削除してよい場合は、"
                    "必要なデータを書き出してから承認してください。"
                ),
            }
        )
    return items


class SavedDataOutcome(Protocol):
    def restart_required(self) -> dict[str, object] | None: ...


class SavedDataGuard(Protocol):
    """保存済みのデータを書き換える処理を包み、同じプロセスのメモリを読み直す（#1844）。"""

    def __call__[T](
        self, areas: Collection[str], rewrite: Callable[[], T]
    ) -> tuple[T, SavedDataOutcome]: ...


def _rewrite_saved_data[T](
    areas: Collection[str], rewrite: Callable[[], T]
) -> tuple[T, SavedDataOutcome]:
    # 遅延 import（Agent の機能はこのモジュールを import する。CLI でも同じ処理を通る）。
    from app.features.agent.storage_bootstrap import rewrite_saved_data

    return rewrite_saved_data(areas, rewrite)


def rewritten_saved_data(before: Mapping[str, Any], *, recreate: bool) -> frozenset[str]:
    """この操作が書き換える保存済みのデータ（全再作成はすべて、未初期化の DB は無し）。"""

    if recreate:
        return ALL_SAVED_DATA
    if before["status"] == "missing":
        return frozenset()
    pending = set(before["pending_versions"])
    return frozenset(
        area for migration in MIGRATIONS if migration.name in pending for area in migration.rewrites
    )


class SystemSchemaManager(SystemSchemaManagerBase):
    """Agent の manifest と DDL の正本を、platform の骨格（lease・台帳）に渡す。"""

    control_table = CONTROL_TABLE
    migration_table = MIGRATION_TABLE
    migration_key_column = "MIGRATION_NAME"
    managed_tables = MANAGED_TABLES
    managed_foreign_keys = MANAGED_FOREIGN_KEYS
    managed_columns = MANAGED_COLUMNS
    # データを消す未適用の migration は、承認が無ければ作成・更新で当てない（#619）。
    guards_destructive_migrations = True
    recreate_confirmation = RECREATE_CONFIRMATION
    log_prefix = "agent"
    lock_timeout_guidance = "権限管理の保存を止めてから、"
    logger = logger

    def __init__(
        self,
        connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        *,
        lease_seconds: int = 900,
        ddl_lock_timeout_seconds: int = DEFAULT_DDL_LOCK_TIMEOUT_SECONDS,
        saved_data_guard: SavedDataGuard | None = None,
    ) -> None:
        super().__init__(
            connection_factory or platform_oracle_connection,
            lease_seconds=lease_seconds,
            ddl_lock_timeout_seconds=ddl_lock_timeout_seconds,
        )
        self._saved_data_guard: SavedDataGuard = saved_data_guard or _rewrite_saved_data

    def _operation_kind(self, recreate: bool) -> str:
        # AGENT_SCHEMA_OPERATIONS.OPERATION_KIND は大文字で書く（RAG と同じ）。
        return "RECREATE" if recreate else "INITIALIZE"

    # ---- 状態 -------------------------------------------------------------

    def _status_on(self, connection: Any) -> dict[str, Any]:
        objects = self._load_objects(connection)
        expected = set(MANAGED_OBJECTS)
        existing = expected.intersection(objects)
        applied = self._load_migrations(connection, objects)
        matching = [m.name for m in MIGRATIONS if applied.get(m.name) == m.checksum]
        pending = [m.name for m in MIGRATIONS if applied.get(m.name) != m.checksum]
        foreign_keys = self._foreign_key_drift(connection)
        table_metadata = self._load_table_metadata(connection, objects)
        # 台帳が適用済みでも、既存の表に正本の列が欠けていれば ready にしない（#2458）。
        absent_columns = self._missing_columns(connection, objects)
        status = classify_system_schema_status(
            set(objects),
            applied,
            foreign_keys_current=foreign_keys.current,
            columns_current=not absent_columns,
        )
        return {
            "status": status,
            "schema_head": MIGRATIONS[-1].name,
            "applied_versions": matching,
            "pending_versions": pending,
            "pending_destructive_migrations": pending_destructive_migrations(
                status, pending, retired_tables=unowned_retired_tables(objects, applied)
            ),
            "expected_object_count": len(expected),
            "existing_object_count": len(existing),
            "expected_table_count": len(MANAGED_TABLES),
            "existing_table_count": sum((name, "TABLE") in objects for name in MANAGED_TABLES),
            "missing_objects": [
                {"name": name, "object_type": object_type}
                for name, object_type in sorted(
                    expected - existing, key=lambda item: (item[1], item[0])
                )
            ],
            "retired_objects": [
                {"name": name, "object_type": object_type}
                for name, object_type in RETIRED_MANAGED_OBJECTS
                if (name, object_type) in objects
            ],
            "missing_columns": absent_columns,
            **foreign_keys.status_fields(),
            "tables": table_metadata,
            # 全管理 object（テーブル・索引）。詳細の一覧を概要の件数とそろえる
            # （RAG / NL2SQL と同じ）。
            "objects": self._build_object_metadata(objects, table_metadata),
            "operation_state": self._operation_payload(connection, objects),
        }

    @staticmethod
    def _build_object_metadata(
        objects: dict[tuple[str, str], Any],
        table_metadata: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
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

    @staticmethod
    def _load_objects(connection: Any) -> dict[tuple[str, str], Any]:
        inspected = tuple(dict.fromkeys((*MANAGED_OBJECTS, *RETIRED_MANAGED_OBJECTS)))
        names = tuple(dict.fromkeys(name for name, _object_type in inspected))
        placeholders, binds = bind_list("object_name_", names)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT OBJECT_NAME, OBJECT_TYPE, CREATED FROM USER_OBJECTS "
                f"WHERE OBJECT_NAME IN ({placeholders}) "  # nosec B608 - 固定の bind
                "AND OBJECT_TYPE IN ('TABLE', 'INDEX')",
                binds,
            )
            return {(str(row[0]).upper(), str(row[1]).upper()): row[2] for row in cursor.fetchall()}

    # ---- 作成・更新 ---------------------------------------------------------

    def _ensure_control_schema(self) -> None:
        with self._connection_factory() as connection:
            self._configure_ddl_lock_timeout(connection)
            self._execute_statements(connection, CONTROL_STATEMENTS)

    def _initialize_on(self, connection: Any, owner: str, *, recreate: bool) -> dict[str, Any]:
        dropped_count = 0
        applied_names: list[str] = []
        self._configure_ddl_lock_timeout(connection)
        # AGENT_ROLE_* は PLATFORM_ROLES を参照するため、共通認証の表を先に作る（冪等）。
        self._apply_platform_auth_schema(connection)
        before = self._status_on(connection)
        if before["status"] == "ready" and not recreate:
            return self._no_op_result(connection, owner)

        def apply() -> int:
            dropped = self._drop_managed_objects(connection, owner) if recreate else 0
            self._heartbeat(connection, owner)
            # 全再作成は台帳（AGENT_SCHEMA_MIGRATIONS）も消すため、制御テーブルから作り直す
            # （冪等）。
            self._execute_statements(connection, (*CONTROL_STATEMENTS, *DOMAIN_STATEMENTS))
            if before["status"] == "missing" or recreate:
                for migration in MIGRATIONS:
                    self._record_migration(connection, migration)
                    applied_names.append(migration.name)
                return dropped
            pending = set(before["pending_versions"])
            for migration in MIGRATIONS:
                if migration.name not in pending:
                    continue
                self._heartbeat(connection, owner)
                self._execute_statements(
                    connection,
                    migration.statements,
                    ignored_codes=IGNORED_APPLY_CODES | IGNORED_DROP_CODES,
                )
                self._record_migration(connection, migration)
                applied_names.append(migration.name)
            return dropped

        # 保存済みのデータを書き換える間は、このプロセスの保存を止め、後でメモリを読み直す
        # （#1844）。
        rewritten = rewritten_saved_data(before, recreate=recreate)
        dropped, saved_data = self._saved_data_guard(rewritten, apply)
        dropped_count += dropped
        dropped_count += self._drop_retired_objects(connection)
        foreign_keys = self._repair_foreign_keys(connection, owner)
        self._heartbeat(connection, owner)
        interim = self._status_on(connection)
        self._require_columns_present(interim)
        if interim["status"] != "ready":
            raise SystemSchemaError(
                "SCHEMA_POSTCONDITION_FAILED",
                "システムテーブル操作後も必須オブジェクトが不足しています。"
                "状態を再取得して再試行してください。",
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
        )
        return {
            **after,
            "operation": operation,
            "dropped_object_count": dropped_count,
            "created_object_count": (
                max(0, int(after["existing_object_count"]) - 1)
                if recreate
                else max(
                    0, int(after["existing_object_count"]) - int(before["existing_object_count"])
                )
            ),
            "rewritten_saved_data": sorted(rewritten),
            "restart_required": saved_data.restart_required(),
        }

    @staticmethod
    def _apply_platform_auth_schema(connection: Any) -> None:
        """共通認証の PLATFORM_* と組み込み SYSTEM_ADMIN ロールを冪等に用意する。

        ユーザーは作らない（最初は構成管理者 `system_admin` でログインする）。
        """
        apply_platform_auth_schema(connection)
        with connection.cursor() as cursor:
            OracleAuthStore._merge_system_admin_role(cursor)
        connection.commit()

    def _record_migration(self, connection: Any, migration: MigrationArtifact) -> None:
        with connection.cursor() as cursor:
            self._merge_migration(
                cursor,
                key=migration.name,
                description=migration.description,
                checksum=migration.checksum,
            )
        connection.commit()

    def _drop_managed_objects(self, connection: Any, owner: str) -> int:
        objects = self._load_objects(connection)
        dropped = 0
        for index_name in reversed(MANAGED_INDEXES):
            if (index_name, "INDEX") in objects:
                dropped += self._execute_drop(connection, f"DROP INDEX {index_name}")
        retired_tables = tuple(
            name for name, object_type in RETIRED_MANAGED_OBJECTS if object_type == "TABLE"
        )
        for table_name in reversed((*MANAGED_TABLES, *retired_tables)):
            if table_name == CONTROL_TABLE or (table_name, "TABLE") not in objects:
                continue
            dropped += self._execute_drop(
                connection, f"DROP TABLE {table_name} CASCADE CONSTRAINTS PURGE"
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


system_schema_manager = SystemSchemaManager()


__all__ = [
    "ALL_SAVED_DATA",
    "BASE_STATEMENTS",
    "CONTROL_PLANE_DATA",
    "CONTROL_PLANE_STATEMENTS",
    "CONTROL_STATEMENTS",
    "DOMAIN_STATEMENTS",
    "RUNTIME_STATEMENTS",
    "RUN_FACTS_DATA_TOOL_STATEMENTS",
    "RUNTIME_CHECKPOINT_VERSION_STATEMENTS",
    "RUN_FACTS_SIGNAL_STATEMENTS",
    "RUN_FACTS_STATEMENTS",
    "CONTROL_TABLE",
    "DOMAIN_TABLES",
    "MANAGED_FOREIGN_KEYS",
    "MANAGED_INDEXES",
    "MANAGED_OBJECTS",
    "MANAGED_TABLES",
    "MIGRATIONS",
    "MIGRATION_TABLE",
    "PRESERVED_TABLES",
    "RECREATE_CONFIRMATION",
    "RETIRED_MANAGED_OBJECTS",
    "RETIRED_TABLES_APPROVAL",
    "RUNTIME_DATA",
    "SavedDataGuard",
    "SavedDataOutcome",
    "MigrationArtifact",
    "SystemSchemaError",
    "SystemSchemaManager",
    "classify_system_schema_status",
    "managed_manifest_from_schema",
    "pending_destructive_migrations",
    "rewritten_saved_data",
    "system_schema_manager",
    "unowned_retired_tables",
]
