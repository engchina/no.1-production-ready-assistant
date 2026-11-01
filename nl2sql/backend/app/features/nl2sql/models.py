"""NL2SQL API models.

外部サービスへ依存しない契約をここに集約する。Oracle / OCI の実呼び出しは
service 層の adapter に閉じ込め、API と UI は同じ shape を使い続ける。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pr_backend_core import CursorPage, Page
from pr_backend_core.chat_history_window import ChatHistoryPage
from pr_backend_core.chat_progress import ChatProgressEvent
from pr_backend_core.observability.model_usage import (
    RunModelUsage,
    UsageMeasurement,
    parse_run_usage,
)
from pr_system_settings.chat_attachments import ChatAttachmentReference
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from .object_identity import qualified_object_name
from .ontology_models import OntologySqlGenerationContext

_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")
_MAX_SQL_ROW_LIMIT = 100000


def _validate_sql_row_limit(value: int | None) -> int | None:
    """SQL 実行の取得件数上限（画面の「取得件数上限」と同じ規則・文言。#541）。"""
    if value is not None and not 1 <= value <= _MAX_SQL_ROW_LIMIT:
        raise ValueError(
            f"取得件数上限は 1 以上 {_MAX_SQL_ROW_LIMIT} 以下の整数を入力してください。"
        )
    return value


class Nl2SqlEngine(StrEnum):
    """NL2SQL の SQL 生成の方式（OCI Enterprise AI を直接呼ぶ方式だけ。#7）。"""

    ENTERPRISE_AI_DIRECT = "enterprise_ai_direct"


def read_stored_engine(value: object) -> object:
    """保存済みの記録（履歴・ジョブ・評価）の生成方式を読む。

    削除した生成方式（#7）で保存された記録も読めるよう、今の enum に無い値は唯一の方式として
    読む。利用者の要求の検証には使わない（要求の未知の値は 422 にする）。
    """

    if isinstance(value, str) and value not in Nl2SqlEngine._value2member_map_:
        return Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    return value


StoredNl2SqlEngine = Annotated[Nl2SqlEngine, BeforeValidator(read_stored_engine)]


class JobStatus(StrEnum):
    """非同期ジョブ状態。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class JobStepStatus(StrEnum):
    """非同期ジョブ内の処理段階。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"


class FeedbackRating(StrEnum):
    """実行結果へのフィードバック。"""

    GOOD = "good"
    BAD = "bad"


class SampleDataset(StrEnum):
    """業務別サンプル。未指定は従来の人事サンプル。"""

    HR = "hr"
    SALES = "sales"
    INQUIRIES = "inquiries"


class SampleDataStep(StrEnum):
    """SQL Assist sample data import step."""

    TABLES = "tables"
    VIEWS = "views"
    DATA = "data"
    ALL = "all"


class StageTiming(BaseModel):
    """処理段階ごとの経過時間。"""

    stage: str
    elapsed_ms: int


class EngineTiming(BaseModel):
    """SQL 生成エンジンの試行ごとの経過時間。"""

    engine: str
    elapsed_ms: int
    status: Literal["success", "failed", "skipped"]
    error: str = ""


class JobStepData(BaseModel):
    """UI へ公開する非同期ジョブの段階別進捗。"""

    stage: str
    status: JobStepStatus = JobStepStatus.PENDING
    elapsed_ms: int | None = None
    # 段階の開始・終了の時刻（ISO 8601。チャットの段階の表示が使う。#1145。
    # 古い snapshot には無い）。
    started_at: str | None = None
    finished_at: str | None = None


class TimingEnvelope(BaseModel):
    """同期/非同期レスポンス共通の時間情報。"""

    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int | None = None
    stage_timings: list[StageTiming] = Field(default_factory=list)


class SchemaColumn(BaseModel):
    """Oracle column metadata for UI schema picking."""

    column_name: str
    logical_name: str
    data_type: str
    nullable: bool = True
    comment: str = ""
    sample_values: list[str] = Field(default_factory=list)
    domain_name: str = ""
    """列に関連付いた SQL ドメイン(OWNER.NAME)。無ければ空。

    SQL ドメインに対応したバージョンの dictionary から取得する。
    """


class SchemaConstraintDetail(BaseModel):
    """順序を保った Oracle constraint metadata。"""

    constraint_name: str
    constraint_type: Literal["P", "R", "U", "C"]
    owner: str = "APP"
    table_name: str
    columns: list[str] = Field(default_factory=list)
    referenced_owner: str | None = None
    referenced_table: str | None = None
    referenced_columns: list[str] = Field(default_factory=list)
    delete_rule: str = "NO ACTION"
    status: str = "ENABLED"
    deferrable: str = "NOT DEFERRABLE"


class SchemaViewDependency(BaseModel):
    """Oracle view から参照 object への lineage。"""

    owner: str = "APP"
    view_name: str
    referenced_owner: str = "APP"
    referenced_name: str
    referenced_type: str = "TABLE"


class SchemaTable(BaseModel):
    """Oracle table metadata for NL2SQL object restriction."""

    table_name: str
    logical_name: str
    owner: str = "APP"
    table_type: str = "table"
    comment: str = ""
    row_count: int | None = None
    columns: list[SchemaColumn] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    constraint_details: list[SchemaConstraintDetail] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qualified_name(self) -> str:
        """查询/API 专用的规范对象身份。"""

        return qualified_object_name(self.owner, self.table_name)


class SchemaOwnerSummary(BaseModel):
    """当前连接用户可访问的一个非 Oracle 维护 schema。"""

    owner: str
    is_current: bool = False
    table_count: int = 0
    view_count: int = 0


class SchemaOwnersData(BaseModel):
    """Schema 发现结果。"""

    current_owner: str
    owners: list[SchemaOwnerSummary] = Field(default_factory=list)
    excluded_oracle_maintained_count: int = 0


class SchemaCatalog(BaseModel):
    """UI が表示する schema catalog."""

    refreshed_at: str
    tables: list[SchemaTable]
    schema_fingerprint: str = ""
    view_dependencies: list[SchemaViewDependency] = Field(default_factory=list)
    current_owner: str = ""
    excluded_oracle_maintained_count: int = 0


class SchemaCatalogHead(BaseModel):
    """大規模 catalog を読み込まずに返せる active catalog metadata。"""

    catalog_version: int = 0
    schema_fingerprint: str = ""
    refreshed_at: str = ""
    object_count: int = 0
    column_count: int = 0
    change_token: int = 0
    etag: str = ""


class SchemaObjectSummary(BaseModel):
    """Schema picker / search 用の軽量 object 行。"""

    owner: str
    object_name: str
    object_type: str = "TABLE"
    logical_name: str = ""
    comment: str = ""
    row_count: int | None = None
    column_count: int = 0
    last_ddl_at: str = ""


class SchemaObjectPage(CursorPage[SchemaObjectSummary]):
    """Keyset cursor で返す schema object page（共通の CursorPage に件数の内訳と版を足す）。"""

    items: list[SchemaObjectSummary] = Field(default_factory=list)
    table_count: int = 0
    view_count: int = 0
    counts_included: bool = True
    refreshed_at: str = ""
    catalog_version: int = 0


class SchemaObjectDetail(BaseModel):
    """選択された object だけを展開した schema detail。"""

    table: SchemaTable
    dependencies: list[SchemaViewDependency] = Field(default_factory=list)
    catalog_version: int = 0
    etag: str = ""


class AllowedObjects(BaseModel):
    """ユーザーが今回の質問で許可する table / column 範囲。"""

    table_names: list[str] = Field(default_factory=list)
    columns: dict[str, list[str]] = Field(default_factory=dict)
    enforce_table_scope: bool = False


PROFILE_IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def normalize_profile_identifier(value: str) -> str:
    return value.strip().upper()


def validate_profile_identifier(value: str) -> str:
    if not PROFILE_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError("名称は英字で開始し、英字・数字・アンダースコアのみ使用できます。")
    return value


ClarificationPolicy = Literal["confirm", "assume"]
# 回答前の確認の往復の上限（業務プロファイルで選べる最大。#1839）。
CLARIFICATION_MAX_ROUNDS_LIMIT = 6


def _validate_clarification_max_rounds(value: int | None) -> int | None:
    """確認の上限は 0（システムの既定）か 2 回以上（1 回は AI の再検査が 0 回になり使えない）。"""

    if value is not None and value == 1:
        raise ValueError("確認の上限は 2 回以上にしてください（0 はシステムの既定）。")
    return value


class Nl2SqlProfile(BaseModel):
    """業務/Profile 単位の NL2SQL 設定。"""

    id: str
    name: str
    category: str = ""
    description: str = ""
    allowed_tables: list[str] = Field(default_factory=list)
    allowed_views: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    sql_rules: list[str] = Field(default_factory=list)
    default_row_limit: int = 100
    safety_policy: str = "select_only"
    few_shot_examples: list[dict[str, str]] = Field(default_factory=list)
    # 以前の版が保存した生成の方式・DB 側の AI profile の設定の項目は読まない（保存済みの JSON に
    # 残っていても無視する。#7）。全体ルールだけは `fold_legacy_rules` が `sql_rules` へ移す。
    # 回答前の確認（#1839。旧名「AI要件確認」）の業務プロファイルの方針。`confirm` は曖昧な質問を
    # 確認してから SQL を生成し、`assume` は確認せずに前提を示して生成する（チャットと MCP の
    # `nl2sql_query`。SQL 生成の画面の「回答前の確認」のボタンは利用者の明示の操作なので変えない）。
    clarification_policy: ClarificationPolicy = "confirm"
    # 確認の上限（往復。最初の確認を 1 回目に数える）。0 はシステムの既定
    # （`NL2SQL_REQUIREMENT_MAX_REVIEW_ROUNDS` + 1）。AI の再検査の上限はこの値 - 1。
    clarification_max_rounds: int = Field(default=0, ge=0, le=CLARIFICATION_MAX_ROUNDS_LIMIT)
    archived: bool = False
    version: int = Field(default=1, ge=1)
    etag: str = ""
    updated_at: str = ""
    object_scope_version: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def fold_legacy_rules(cls, data: Any) -> Any:
        """以前の版が DB 側の AI profile の設定に入れた全体ルールを `sql_rules` へ移す（#7）。

        以前の版は全体ルールを `sql_rules` でなく、削除した生成方式（Select AI）の設定の
        `additional_instructions`（1 行 1 件）に保存していた。保存済みの業務プロファイルの
        全体ルールを失わないよう、読むときに `sql_rules` の後ろへ足す（重複は除く）。
        """

        if not isinstance(data, Mapping):
            return data
        legacy = data.get(_LEGACY_RULES_CONFIG_KEY)
        if not isinstance(legacy, Mapping):
            return data
        text = legacy.get("additional_instructions")
        if not isinstance(text, str) or not text.strip():
            return data
        rules = [str(rule).strip() for rule in data.get("sql_rules") or [] if str(rule).strip()]
        for line in text.splitlines():
            if line.strip() and line.strip() not in rules:
                rules.append(line.strip())
        return {**data, "sql_rules": rules}


# 以前の版の業務プロファイルの JSON で、全体ルールを持っていた設定の key（#7。読むだけ）。
_LEGACY_RULES_CONFIG_KEY = "select_ai_config"


class ProfileSummary(BaseModel):
    """一覧で full profile payload を転送しないための summary。"""

    id: str
    name: str
    category: str = ""
    description: str = ""
    archived: bool = False
    allowed_table_count: int = 0
    allowed_view_count: int = 0
    glossary_count: int = 0
    few_shot_count: int = 0
    version: int = 1
    etag: str = ""
    updated_at: str = ""


class ProfileUsageContext(BaseModel):
    """AI 活用画面が参照する profile の最小利用コンテキスト。"""

    id: str
    name: str
    category: str = ""
    description: str = ""
    allowed_tables: list[str] = Field(default_factory=list)
    allowed_views: list[str] = Field(default_factory=list)
    # 回答前の確認の方針（#1839）。チャットは `assume` なら確認事項を聞かずに作る。
    clarification_policy: ClarificationPolicy = "confirm"
    archived: bool = False
    object_scope_version: int = 1
    version: int = 1
    etag: str = ""
    updated_at: str = ""


class ProfileSummaryPage(CursorPage[ProfileSummary]):
    """業務 profile の keyset cursor page（共通の CursorPage に change_token を足す）。"""

    items: list[ProfileSummary] = Field(default_factory=list)
    change_token: int = 0


class SchemaRefreshJobStatus(StrEnum):
    """Schema refresh worker の永続 job 状態。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class SchemaRefreshPhase(StrEnum):
    """Schema refresh の安定した進捗 phase。"""

    QUEUED = "queued"
    SCANNING = "scanning"
    FETCHING = "fetching"
    PERSISTING = "persisting"
    DONE = "done"


class SchemaRefreshMode(StrEnum):
    """Schema refresh の実行範囲。"""

    FULL = "full"
    TARGETED = "targeted"


class SchemaRefreshTargetObject(BaseModel):
    """Targeted schema refresh で Oracle へ確認する object。"""

    owner: str
    object_name: str
    object_type: Literal["table", "view", "materialized_view", "unknown"] = "unknown"
    expected_state: Literal["present", "absent", "unknown"] = "unknown"


class SchemaRefreshJob(BaseModel):
    """非同期 schema refresh の公開契約。"""

    job_id: str
    status: SchemaRefreshJobStatus = SchemaRefreshJobStatus.PENDING
    mode: SchemaRefreshMode = SchemaRefreshMode.FULL
    source: str = "manual"
    target_objects: list[SchemaRefreshTargetObject] = Field(default_factory=list)
    requires_full_refresh: bool = False
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    worker_id: str = ""
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    attempt: int = 0
    phase: SchemaRefreshPhase = SchemaRefreshPhase.QUEUED
    processed_objects: int = 0
    total_objects: int = 0
    scanned_objects: int = 0
    changed_objects: int = 0
    deleted_objects: int = 0
    catalog_version: int | None = None
    error_code: str = ""


class SchemaRefreshActiveJobData(BaseModel):
    """画面復帰時に再接続する実行中 schema refresh job。"""

    active_job: SchemaRefreshJob | None = None


def _normalize_glossary_payload(value: Any) -> Any:
    """Profile API から受ける glossary は空キーと制御文字を保存前に拒否する。"""

    if value is None or not isinstance(value, Mapping):
        return value
    normalized: dict[str, str] = {}
    for raw_key, raw_definition in value.items():
        key = str(raw_key or "").strip()
        definition = str(raw_definition or "").strip()
        if not key:
            raise ValueError("用語キーは空にできません。")
        if _CONTROL_CHARACTER_RE.search(key) or _CONTROL_CHARACTER_RE.search(definition):
            raise ValueError("用語キーと定義に制御文字は使用できません。")
        if definition:
            normalized[key] = definition
    return normalized


class ProfileUpsertRequest(BaseModel):
    """Profile 作成/更新 request."""

    name: str = Field(min_length=1)
    category: str = ""
    description: str = ""
    allowed_tables: list[str] = Field(default_factory=list)
    allowed_views: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    sql_rules: list[str] = Field(default_factory=list)
    default_row_limit: int = Field(default=100, ge=1, le=5000)
    safety_policy: str = "select_only"
    few_shot_examples: list[dict[str, str]] = Field(default_factory=list)
    clarification_policy: ClarificationPolicy = "confirm"
    clarification_max_rounds: int = Field(default=0, ge=0, le=CLARIFICATION_MAX_ROUNDS_LIMIT)

    _check_clarification_max_rounds = field_validator("clarification_max_rounds")(
        _validate_clarification_max_rounds
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            return normalize_profile_identifier(value)
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_profile_identifier(value)

    @field_validator("glossary", mode="before")
    @classmethod
    def normalize_glossary(cls, value: Any) -> Any:
        return _normalize_glossary_payload(value)


class ProfilePatchRequest(BaseModel):
    """Profile 更新 request。省略された項目は既存値を保持する。"""

    name: str | None = Field(default=None, min_length=1)
    category: str | None = None
    description: str | None = None
    allowed_tables: list[str] | None = None
    allowed_views: list[str] | None = None
    glossary: dict[str, str] | None = None
    sql_rules: list[str] | None = None
    default_row_limit: int | None = Field(default=None, ge=1, le=5000)
    safety_policy: str | None = None
    few_shot_examples: list[dict[str, str]] | None = None
    clarification_policy: ClarificationPolicy | None = None
    clarification_max_rounds: int | None = Field(
        default=None, ge=0, le=CLARIFICATION_MAX_ROUNDS_LIMIT
    )

    _check_clarification_max_rounds = field_validator("clarification_max_rounds")(
        _validate_clarification_max_rounds
    )

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            return normalize_profile_identifier(value)
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_profile_identifier(value) if value is not None else value

    @field_validator("glossary", mode="before")
    @classmethod
    def normalize_glossary(cls, value: Any) -> Any:
        return _normalize_glossary_payload(value)


class StrictMutationRequest(BaseModel):
    """Reject stale mutation fields instead of silently changing their meaning."""

    model_config = ConfigDict(extra="forbid")


class AdminExecutionConfirmation(StrictMutationRequest):
    """Common confirmation fields for admin/destructive operations."""

    confirmation: str = ""
    reason: str = ""


class ProfileLearningMaterialImportData(BaseModel):
    """Terms / rules / few-shot learning material import response."""

    profile_id: str
    profile_name: str
    mode: str = "merge"
    imported_terms: int = 0
    imported_rules: int = 0
    imported_examples: int = 0
    skipped_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    profile: Nl2SqlProfile


class LegacyLearningMaterialData(BaseModel):
    """全 profile で共有するグローバル用語と旧 rules 互換データ."""

    glossary: dict[str, str] = Field(default_factory=dict)
    rules: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_rule_entries(cls, value: Any) -> Any:
        """旧 snapshot の CATEGORY/RULE 行を、順序を保って全局ルールへ移行する。"""
        if not isinstance(value, dict) or "rules" in value:
            return value
        migrated = dict(value)
        rules: list[str] = []
        seen: set[str] = set()
        for entry in migrated.pop("rule_entries", []) or []:
            rule = str(entry.get("rule") if isinstance(entry, dict) else "").strip()
            if rule and rule not in seen:
                seen.add(rule)
                rules.append(rule)
        migrated["rules"] = rules
        return migrated


class SafetyReport(BaseModel):
    """SQL safety analysis."""

    is_safe: bool
    is_select_only: bool
    row_limit_applied: int
    blocked_reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    referenced_tables: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)
    # 安全確認をしたか（#1523）。利用者の SQL・LLM が生成した SQL は確かめる。
    safety_checked: bool = True
    # 安全確認を省略した理由（利用者向けの文。省略しなかったときは空）。
    safety_check_skip_reason: str = ""


class QueryResults(BaseModel):
    """SQL execution results."""

    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    returned_count: int | None = None
    has_more: bool = False
    truncated: bool = False
    execution_context: Literal[
        "deterministic",
        "oracle_data_plane",
        "admin_control_plane",
    ] = "deterministic"

    @field_serializer("rows", when_used="json")
    def serialize_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # JSON の数値を browser が Number に変えても失わない範囲。内部の整数は変更しない。
        max_safe_integer = 2**53 - 1
        return [
            {
                key: str(value)
                if isinstance(value, int) and abs(value) > max_safe_integer
                else value
                for key, value in row.items()
            }
            for row in rows
        ]

    @model_validator(mode="after")
    def fill_returned_count(self) -> QueryResults:
        if self.returned_count is None:
            self.returned_count = len(self.rows)
        return self


class ExplainPlanOperation(BaseModel):
    """Oracle PLAN_TABLE の安全な要約行。"""

    operation: str
    options: str = ""
    owner: str = ""
    object_name: str = ""
    cost: int | None = None
    cardinality: int | None = None
    bytes: int | None = None


class ExplainPlanData(BaseModel):
    """意味 gate と独立した性能 check。利用不可でも実行許可は変更しない。"""

    available: bool = False
    total_cost: int | None = None
    estimated_cardinality: int | None = None
    full_table_scans: list[str] = Field(default_factory=list)
    operations: list[ExplainPlanOperation] = Field(default_factory=list)
    warning: str = ""


class DbAdminObjectSummary(BaseModel):
    """Database admin table/view summary."""

    name: str
    owner: str = ""
    qualified_name: str = ""
    object_type: str = "table"
    row_count: int | None = None
    comment: str = ""

    @model_validator(mode="after")
    def fill_qualified_name(self) -> DbAdminObjectSummary:
        if self.qualified_name or not self.owner:
            return self
        self.qualified_name = qualified_object_name(self.owner, self.name)
        return self


class DbAdminObjectDetail(BaseModel):
    """Database admin table/view detail with columns and DDL."""

    name: str
    owner: str = ""
    qualified_name: str = ""
    object_type: str = "table"
    row_count: int | None = None
    comment: str = ""
    columns: list[SchemaColumn] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    ddl: str = ""
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_qualified_name(self) -> DbAdminObjectDetail:
        if self.qualified_name or not self.owner:
            return self
        self.qualified_name = qualified_object_name(self.owner, self.name)
        return self


class DbAdminObjectsData(BaseModel):
    """Database admin object list response."""

    runtime: str = "deterministic"
    items: list[DbAdminObjectSummary] = Field(default_factory=list)
    refreshed_at: str = ""
    warnings: list[str] = Field(default_factory=list)


class DbAdminObjectPage(CursorPage[DbAdminObjectSummary]):
    """管理画面向けの軽量・keyset page。Catalog/CLOB は読み込まない。"""

    runtime: str = "deterministic"
    owner: str = ""
    items: list[DbAdminObjectSummary] = Field(default_factory=list)
    total: int = 0
    table_count: int = 0
    view_count: int = 0
    counts_included: bool = True
    refreshed_at: str = ""
    catalog_version: int = 0
    warnings: list[str] = Field(default_factory=list)


class DbAdminDropTableRequest(AdminExecutionConfirmation):
    """Drop table execution request."""

    table_name: str = Field(min_length=1)
    owner: str = ""
    purge: bool = True


class DbAdminTruncateTableRequest(AdminExecutionConfirmation):
    """Truncate table data execution request."""

    table_name: str = Field(min_length=1)
    owner: str = ""


class DbAdminStatementResult(BaseModel):
    """One admin SQL statement execution result."""

    index: int
    statement_type: str
    status: str
    sql: str = ""
    row_count: int | None = None
    message: str = ""
    elapsed_ms: int = 0
    error_message: str = ""
    error_code: str = ""


class DbAdminExecuteRequest(AdminExecutionConfirmation):
    """Admin SQL executor request.

    This intentionally lives outside the normal SELECT-only NL2SQL query path.
    """

    sql: str = Field(min_length=1)
    row_limit: int = 100

    @field_validator("row_limit")
    @classmethod
    def validate_row_limit(cls, value: int) -> int:
        _validate_sql_row_limit(value)
        return value


class DbAdminExecuteData(BaseModel):
    """Admin SQL executor response."""

    executed: bool = False
    runtime: str = "deterministic"
    execution_context: Literal[
        "deterministic",
        "oracle_data_plane",
        "admin_control_plane",
    ] = "deterministic"
    select_result: QueryResults | None = None
    statements: list[DbAdminStatementResult] = Field(default_factory=list)
    committed: bool = False
    rolled_back: bool = False
    schema_refresh_job_id: str = ""
    schema_refresh_required: bool = False
    schema_refresh_reason_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope


class DbObjectNameRef(BaseModel):
    """表示用の所有者付き object 名（qualified_object_name と同じ規則）。"""

    name: str
    owner: str = ""
    qualified_name: str = ""


class SampleDataInfo(BaseModel):
    """Optional SQL Assist sample package status."""

    dataset: SampleDataset = SampleDataset.HR
    runtime: str = "deterministic"
    profile_id: str = ""
    confirmation: str = "ADMIN_EXECUTE"
    objects: list[str] = Field(default_factory=list)
    # sample object を作成する schema（current schema）と、objects の所有者付き名前。
    owner: str = ""
    object_refs: list[DbObjectNameRef] = Field(default_factory=list)
    imported_objects: list[str] = Field(default_factory=list)
    # 同名だが種類・列構成がサンプル定義と異なり、利用者のものとみなしたオブジェクト。
    conflicting_objects: list[str] = Field(default_factory=list)
    # 旧名（SAMPLE_NL2SQL_ 接頭辞）で残っているサンプルオブジェクト。削除時に併せて削除する。
    legacy_objects: list[str] = Field(default_factory=list)
    sql: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SampleDataMutationRequest(AdminExecutionConfirmation):
    """Sample data import/delete execution request."""

    dataset: SampleDataset = SampleDataset.HR
    step: SampleDataStep = SampleDataStep.ALL


class SampleDataMutationData(BaseModel):
    """Sample data import/delete response."""

    operation: str
    dataset: SampleDataset = SampleDataset.HR
    step: SampleDataStep = SampleDataStep.ALL
    runtime: str = "deterministic"
    executed: bool = False
    objects: list[str] = Field(default_factory=list)
    statements: list[DbAdminStatementResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    profile_id: str = ""
    schema_refresh_job_id: str = ""
    schema_refresh_required: bool = False
    schema_refresh_reason_code: str = ""
    timing: TimingEnvelope


class DbAdminImportTabularRequest(AdminExecutionConfirmation):
    """CSV/XLSX/XLS tabular import execution request."""

    table_name: str = Field(min_length=1)
    content_base64: str = Field(min_length=1)
    filename: str = "upload.csv"
    sheet_name: str = ""
    mode: str = "create"
    preview: bool = False
    column_types: dict[str, str] = Field(default_factory=dict)

    @field_validator("column_types")
    @classmethod
    def validate_column_types(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, data_type in value.items():
            kind = " ".join(data_type.upper().split())
            varchar = re.fullmatch(r"VARCHAR2\(([1-9][0-9]*) CHAR\)", kind)
            if kind not in {
                "NUMBER",
                "DATE",
                "TIMESTAMP",
                "TIMESTAMP WITH TIME ZONE",
                "CLOB",
            } and not (varchar and int(varchar.group(1)) <= 4000):
                raise ValueError(
                    "列の型は NUMBER、DATE、TIMESTAMP、TIMESTAMP WITH TIME ZONE、"
                    "CLOB、VARCHAR2(n CHAR) で指定してください。"
                )
            normalized[name] = kind
        return normalized

    max_rows: int | None = Field(default=None, ge=1, le=50000)


DbAdminStatementPolicy = Literal[
    "table_ddl",
    "view_ddl",
    "data_dml",
    "comment_sql",
    "annotation_sql",
    "domain_sql",
]


class DbAdminStatementsRequest(AdminExecutionConfirmation):
    """文種 whitelist 付き複数 statement 実行 request(SQL Assist 移植)。"""

    sql: str = Field(min_length=1)
    policy: DbAdminStatementPolicy


class DbAdminDropViewRequest(AdminExecutionConfirmation):
    """Drop view execution request."""

    view_name: str = Field(min_length=1)
    owner: str = ""


class DbAdminDataPreviewRequest(BaseModel):
    """テーブル/ビューのデータ表示 request。"""

    object_name: str = Field(min_length=1)
    owner: str = ""
    limit: int = Field(default=100, ge=1, le=100000)
    where_clause: str = ""


class DbAdminDataPreviewData(BaseModel):
    """テーブル/ビューのデータ表示 response。"""

    runtime: str = "deterministic"
    sql: str = ""
    results: QueryResults
    warnings: list[str] = Field(default_factory=list)


class DbAdminCsvUploadRequest(AdminExecutionConfirmation):
    """既存テーブルへの CSV アップロード(INSERT / TRUNCATE&INSERT)request。"""

    table_name: str = Field(min_length=1)
    owner: str = ""
    content_base64: str = Field(min_length=1)
    filename: str = "upload.csv"
    mode: Literal["insert", "truncate_insert"] = "insert"
    max_rows: int | None = Field(default=None, ge=1, le=50000)


class DbAdminCsvUploadData(BaseModel):
    """CSV アップロード execution response。"""

    table_name: str
    filename: str = ""
    mode: str = "insert"
    matched_columns: list[str] = Field(default_factory=list)
    unmatched_csv_columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    success_count: int = 0
    error_count: int = 0
    row_errors: list[str] = Field(default_factory=list)
    hint: str = ""
    executed: bool = False
    runtime: str = "deterministic"
    sample_rows: list[dict[str, str | None]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope


class DbAdminAiAnalysisRequest(BaseModel):
    """Admin SQL 実行結果の AI 分析 request。"""

    sql: str = ""
    result_text: str = ""
    target: Literal["table", "view", "data", "comment", "annotation"] = "table"


class DbAdminAiAnalysisData(BaseModel):
    """Admin SQL 実行結果の AI 分析 response。"""

    analysis: str = ""
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class DbAdminJoinWhereRequest(BaseModel):
    """ビュー DDL の JOIN/WHERE 条件抽出 request。"""

    ddl: str = Field(min_length=1)
    prompt_profile: Literal["sql_structure"] = "sql_structure"


class DbAdminChatHistoryTurn(BaseModel):
    """DB 管理チャットの同じ会話の前の往復（続きの質問の文脈。保存したターンから作る。#2319）。"""

    question: str = Field(default="", max_length=4000)
    sql: str = Field(default="", max_length=20000)
    # 実行の結果の短い要約（成功 / 失敗のメッセージ）。
    outcome: str = Field(default="", max_length=2000)


class DbAdminChatGenerateRequest(BaseModel):
    """DB 管理チャットの SQL の生成（Enterprise AI Direct だけ）。

    前の往復は画面から受け取らず、サーバーが保存した会話から復元する（#2319）。`conversation_id` が
    空なら新しい会話を始める（会話の ID は最初のターンの ID）。`client_turn_id` は画面が送信の前に
    決めるターンの ID で、応答が届かずに送り直しても同じターンを返す（二重に生成しない）。
    """

    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(default="", max_length=64)
    client_turn_id: UUID


class DbAdminChatStatementPlan(BaseModel):
    """実行の前の 1 文の分類（文ごとに実行できる。#2333）。"""

    index: int
    statement_type: str
    sql: str
    # 読み取り専用の SELECT / WITH…SELECT（確認語なしで実行できる）。
    select_only: bool = False
    implicit_commit: bool = False
    # 方針で止める文・製品の object など、実行できない理由（実行できるなら空）。
    blocked_reason: str = ""
    # データ・構造を変える文（読み取り専用の SELECT 以外で、止めない文）は確認語が要る。
    requires_confirmation: bool = False
    # 確認語（`ADMIN_EXECUTE`、1 つの対象を消す DROP / TRUNCATE は対象の名前）。不要なら空。
    confirmation_target: str = ""
    # 参照する object に無い列（#2352）。空でなければ `blocked_reason` に実際の列を添えて実行を
    # 止める（AI のエラー分析で直した SQL に置き換えると分類し直す）。
    unknown_columns: list[str] = Field(default_factory=list)
    # 列を確かめられなかった object など、止めない注意（#2352）。
    warnings: list[str] = Field(default_factory=list)


class DbAdminChatPlan(BaseModel):
    """実行の前の分類（生成エンジンに関係なく、サーバーで分類する）。

    文ごとの分類は `statements`。ここの値は「すべて実行」（上から順に・最初のエラーで止める）
    の判定。
    """

    sql: str = ""
    statements: list[DbAdminChatStatementPlan] = Field(default_factory=list)
    # すべての文が読み取り専用の SELECT（「すべて実行」に確認語は要らない）。
    read_only: bool = False
    # 「すべて実行」に確認語が要る（変える文を含む）。
    requires_confirmation: bool = False
    # 「すべて実行」の確認語（変える文が 1 つならその文の確認語、2 つ以上なら `ADMIN_EXECUTE`）。
    confirmation_target: str = ""
    implicit_commit: bool = False
    # 止める文を含む（「すべて実行」はできない。ほかの文は 1 文ずつ実行できる）。
    blocked: bool = False
    blocked_reasons: list[str] = Field(default_factory=list)


class DbAdminChatGenerateData(BaseModel):
    """DB 管理チャットの SQL の生成の結果。SQL は実行しない（実行は利用者が押す）。"""

    question: str
    explanation: str = ""
    plan: DbAdminChatPlan
    context_tables: list[str] = Field(default_factory=list)
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope
    # 生成の処理の経過（#3226 の案 A）: span は共通の span の表にあり、ここは trace id・書けなかった
    # span の数・工程の詳細（span id → 詳細。表には入れない内容）だけ。
    trace_id: str = ""
    trace_dropped_spans: int = 0
    span_details: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DbAdminChatTurnExecuteRequest(AdminExecutionConfirmation):
    """DB 管理チャットのターンの SQL の実行（#2319・#2333）。

    SQL は画面から受け取らず、ターンに保存した SQL（AI のエラー分析で直したら直した SQL）を使う。
    `mode="statement"` は `statement_index`（1 始まり）の 1 文、`mode="all"`
    はすべての文を上から順に
    実行する（最初のエラーで止める）。
    """

    conversation_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    mode: Literal["statement", "all"] = "statement"
    statement_index: int | None = Field(default=None, ge=1)
    row_limit: int = 100

    @field_validator("row_limit")
    @classmethod
    def validate_row_limit(cls, value: int) -> int:
        _validate_sql_row_limit(value)
        return value

    @model_validator(mode="after")
    def validate_target(self) -> DbAdminChatTurnExecuteRequest:
        if self.mode == "statement" and self.statement_index is None:
            raise ValueError(
                'mode="statement" には statement_index'
                "（実行する文の 1 始まりの番号）を渡してください。"
            )
        if self.mode == "all" and self.statement_index is not None:
            raise ValueError('mode="all" には statement_index を渡さないでください。')
        return self


class DbAdminChatPlanUpdateRequest(BaseModel):
    """ターンの SQL を、AI のエラー分析で直した SQL に置き換える（サーバーが分類し直す。#2319）。

    `statement_index`（1 始まり）を渡すとその文だけを置き換える（#2333）。無ければターンの SQL
    の全体。
    """

    sql: str = Field(min_length=1, max_length=200000)
    statement_index: int | None = Field(default=None, ge=1)


class DbAdminChatAnalyzeRequest(BaseModel):
    """DB 管理チャットの失敗の AI 分析。"""

    statement_index: int = Field(ge=1)


class DbAdminChatCorrectionAttempt(BaseModel):
    """サーバーが保存する文ごとの失敗。結果の行・確認語は含めない。"""

    statement_index: int
    revision: int
    sql: str
    error_code: str = ""
    error_summary: str
    executed_at: str


class DbAdminChatAnalyzeData(BaseModel):
    """失敗の分析と直した SQL（直した SQL も確認からやり直す）。"""

    cause: str
    fix: str
    conclusion: str
    corrected_plan: DbAdminChatPlan | None = None
    source: str = "deterministic"
    model: str = ""
    warnings: list[str] = Field(default_factory=list)
    previous_attempts: list[DbAdminChatCorrectionAttempt] = Field(default_factory=list)


DbAdminChatStatementStatus = Literal[
    "succeeded", "failed", "blocked", "not_run", "confirmation_required"
]


class DbAdminChatStatementOutcome(BaseModel):
    """1 文の実行の結果（#2333）。

    `result` はその文だけの実行の結果（`statements` は 1 件、SELECT は `select_result`）。
    「すべて実行」で
    実行しなかった文（`not_run`）と確認語の待ちは空。
    """

    index: int
    status: DbAdminChatStatementStatus
    result: DbAdminExecuteData | None = None


class DbAdminChatExecuteData(BaseModel):
    """DB 管理チャットの実行の結果（1 文か「すべて実行」。#2333）。

    `committed` / `rolled_back` は実行の単位（1 文、または「すべて実行」の 1 つのトランザクション）
    の値。
    """

    mode: Literal["statement", "all"]
    # 監査の行を結び付ける実行の ID（「すべて実行」は文ごとの監査の行に同じ ID が入る）。
    batch_id: str
    executed: bool = False
    committed: bool = False
    rolled_back: bool = False
    # 「すべて実行」で止まった文（最初のエラー）の番号。
    stopped_at: int | None = None
    statements: list[DbAdminChatStatementOutcome] = Field(default_factory=list)
    schema_refresh_job_id: str = ""
    schema_refresh_required: bool = False
    schema_refresh_reason_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope


class DbAdminChatStatementLastExecution(BaseModel):
    """ターンの 1 文の最後の実行の要約（#2319・#2333）。

    結果の行はチャットと同じく保存しない（`result.select_result` は空。行数・列数・打ち切りだけ）。
    会話を開き直したら要約と「もう一度実行」を出す。確認語は保存しない（実行は 1 回ごとに確認）。
    """

    index: int
    executed_at: str
    mode: Literal["statement", "all"] = "statement"
    batch_id: str = ""
    confirmation_given: bool = False
    status: Literal["succeeded", "failed", "blocked", "not_run"]
    row_count: int | None = None
    column_count: int | None = None
    has_more: bool = False
    # 続きの質問の文脈に渡す短い要約（成功・行数・失敗のメッセージ）。
    outcome: str = ""
    result: DbAdminExecuteData | None = None


class DbAdminChatRunAllSummary(BaseModel):
    """ターンの最後の「すべて実行」の要約（#2333）。"""

    executed_at: str
    batch_id: str
    confirmation_given: bool = False
    succeeded: bool = False
    statement_count: int = 0
    succeeded_count: int = 0
    stopped_at: int | None = None
    committed: bool = False
    rolled_back: bool = False
    warnings: list[str] = Field(default_factory=list)


class DbAdminChatTurn(BaseModel):
    """DB 管理チャットの保存したターン（#2319）。画面が開き直したときの再表示に要るものを持つ。"""

    turn_id: str
    question: str
    created_at: str
    generated_at: str
    explanation: str = ""
    warnings: list[str] = Field(default_factory=list)
    context_tables: list[str] = Field(default_factory=list)
    model: str = ""
    plan: DbAdminChatPlan
    # AI のエラー分析で直した SQL に置き換えると進む（画面の実行の状態を版ごとに分ける）。
    revision: int = 0
    corrected: bool = False
    # 文ごとの最後の実行の要約（今の SQL の文のもの。文の番号の順。#2333）。
    statement_executions: list[DbAdminChatStatementLastExecution] = Field(default_factory=list)
    correction_attempts: list[DbAdminChatCorrectionAttempt] = Field(default_factory=list)
    last_run_all: DbAdminChatRunAllSummary | None = None
    # 会話の持ち主以外（system_admin）が送ったターンだけに付く送り手（#1928 と同じ）。
    sender_user_uuid: str | None = None
    sender_display_name: str | None = None
    # 回答の注意書き（システム設定の文。#2405）。モデルに渡す会話の履歴には入れない。
    notice: str | None = None
    # 生成したときの処理の経過（#3226 の案 A）。span は共通の span の表にあり、ターンには trace id・
    # 書けなかった span の数・工程の詳細（span id → 詳細）だけを置く。画面は
    # `GET .../turns/{turn_id}/trace` で読む。実行（利用者が後から押す操作）は足さない。
    trace_id: str = ""
    trace_dropped_spans: int = 0
    span_details: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DbAdminChatSummary(BaseModel):
    """DB 管理チャットの会話（#2319。チャットの `SqlChatSummary` と同じ見え方の項目）。"""

    id: str
    title: str
    created_at: str
    updated_at: str = ""
    # 会話の持ち主（応答には出さない。表示名の解決と本人の会話の判定に使う）。
    actor_user_uuid: str = Field(default="", exclude=True)
    owned: bool = True
    # system_admin の一覧だけに出す利用者の表示名（引けない利用者は空）。
    actor_display_name: str = ""


class DbAdminChatPage(ChatHistoryPage[DbAdminChatSummary]):
    """DB 管理チャットの会話の履歴の 1 ページ（新しい順。offset 型）。

    期間（最後の更新が直近 N 日）は `history_days`（#2340）。
    """


class DbAdminChatData(BaseModel):
    """DB 管理チャットの会話の内容。"""

    conversation: DbAdminChatSummary
    turns: list[DbAdminChatTurn] = Field(default_factory=list)


class DbAdminChatSendData(BaseModel):
    """質問を送った結果（保存したターンと、その会話）。SQL は実行しない。"""

    conversation: DbAdminChatSummary
    turn: DbAdminChatTurn


class DbAdminJoinWhereData(BaseModel):
    """ビュー DDL の JOIN/WHERE 条件抽出 response。"""

    join_text: str = "None"
    where_text: str = "None"
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)
    prompt_profile: Literal["sql_structure"] = "sql_structure"
    structure_markdown: str = ""


class Nl2SqlResult(BaseModel):
    """NL2SQL job result."""

    business_release_id: str = ""
    history_id: str = ""
    engine: StoredNl2SqlEngine
    engine_meta: dict[str, Any] = Field(default_factory=dict)
    fallback_reason: str = ""
    original_question: str
    rewritten_question: str
    generated_sql: str
    executable_sql: str
    explanation: str
    safety: SafetyReport
    recommendations: list[str] = Field(default_factory=list)
    repaired_sql: str = ""
    optimization_hints: list[str] = Field(default_factory=list)
    results: QueryResults
    timing: TimingEnvelope
    interpretation: Nl2SqlInterpretationArtifact | None = None
    # 回答の注意書き（システム設定の文。#2405）。SQL を実行して答えた job（DONE）だけに付ける。
    # モデルに渡す会話の履歴（前の質問と SQL）には入れない。
    notice: str | None = None


class Nl2SqlQuestionInterpretation(BaseModel):
    """質問を業務実行向けに解釈した表示用 artifact。"""

    available: bool = False
    source: str = "deterministic"
    original_question: str = ""
    rewritten_question: str = ""
    profile_id: str = ""
    profile_name: str = ""
    profile_category: str = ""
    target_objects: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    aggregations: list[str] = Field(default_factory=list)
    row_limit: int | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


class Nl2SqlLogicalStep(BaseModel):
    """処理手順 1 件を業務者向け(business)/技術者向け(technical)で併記する。"""

    kind: str = ""
    business: str = ""
    technical: str = ""


class Nl2SqlLogicalStructureItem(BaseModel):
    """SQL 論理構造 1 項目を業務者向け/技術者向けで併記する。"""

    kind: str = ""
    business: str = ""
    technical: str = ""


class Nl2SqlSqlInterpretation(BaseModel):
    """生成 SQL の意味構造を表示する artifact。"""

    available: bool = False
    source: str = "sql_semantics"
    summary: str = ""
    statement_type: str = ""
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    aggregations: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    limit: int | None = None
    # 生成 SQL を業務向けに説明する決定論の処理手順(include_interpretation 時のみ非空)。
    logical_steps: list[str] = Field(default_factory=list)
    # 同じ手順を業務者向け/技術者向けで併記する構造化版(UI は空でなければこちらを描画)。
    logical_step_details: list[Nl2SqlLogicalStep] = Field(default_factory=list)
    semantic_graph: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Nl2SqlOntologyGraphSnapshot(BaseModel):
    """SQL 生成結果に同梱する profile-scoped Ontology graph snapshot。"""

    revision_id: str = ""
    revision: dict[str, Any] | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


# 接地確認を有効にしたが行わなかった理由（空は行った、または無効）。#1168
OntologyGroundingSkipReason = Literal["", "no_published_ontology"]


class Nl2SqlInterpretationArtifact(BaseModel):
    """質問解釈と SQL 意味グラフの表示用 artifact。"""

    available: bool = False
    question: Nl2SqlQuestionInterpretation = Field(default_factory=Nl2SqlQuestionInterpretation)
    sql: Nl2SqlSqlInterpretation = Field(default_factory=Nl2SqlSqlInterpretation)
    ontology_graph: Nl2SqlOntologyGraphSnapshot | None = None
    # 接地確認をしたか（JobCreateRequest.ontology_grounding_requested のエコー。#1172）。
    # False のとき UI は Ontology 接地確認を表示しない。
    # 既存永続 job(フィールド無し)は従来挙動を保つため default True。
    ontology_grounding_enabled: bool = True
    # 接地確認を有効にしたが行わなかった理由。"no_published_ontology" は、業務プロファイルに
    # 公開されたオントロジーが無いこと（ジョブの中でその場で同期・構築しない。#1168）。
    # 空は接地確認をした（または無効）こと。失敗はこの項目でなく warnings に出す。
    ontology_grounding_skip_reason: OntologyGroundingSkipReason = ""
    warnings: list[str] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    """自然言語から SQL を生成し、実行せずに safety を返す。"""

    question: str = Field(min_length=1)
    engine: Nl2SqlEngine = Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    profile_id: str | None = None
    allowed_objects: AllowedObjects = Field(default_factory=AllowedObjects)
    row_limit: int | None = Field(default=None, ge=1, le=5000)
    ontology_context: OntologySqlGenerationContext | None = None
    use_glossary: bool = False


class PreviewData(BaseModel):
    """Preview response.

    既存テスト互換のため sql/is_safe/row_limit/note を残し、詳細情報を足す。
    """

    sql: str
    is_safe: bool
    row_limit: int
    note: str
    engine: StoredNl2SqlEngine = Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    engine_meta: dict[str, Any] = Field(default_factory=dict)
    fallback_reason: str = ""
    rewritten_question: str = ""
    executable_sql: str = ""
    safety: SafetyReport | None = None
    recommendations: list[str] = Field(default_factory=list)
    repaired_sql: str = ""
    optimization_hints: list[str] = Field(default_factory=list)
    timing: TimingEnvelope | None = None


class ExecuteRequest(BaseModel):
    """Profile と独立した SQL execution request.

    実行スコープは route 側で principal に許可された業務プロファイル群へ強制される。
    """

    model_config = ConfigDict(extra="ignore")

    sql: str = Field(min_length=1)
    allowed_objects: AllowedObjects = Field(default_factory=AllowedObjects)
    # 未指定/null は「total result maximum なし」として保持する（API の契約。画面は常に明示する）。
    # driver memory 保護は Oracle adapter の batch fetch で行い、100 件へ暗黙正規化しない。
    row_limit: int | None = None

    @field_validator("row_limit")
    @classmethod
    def validate_row_limit(cls, value: int | None) -> int | None:
        # 画面の「取得件数上限」と同じ規則・文言（#541）。
        return _validate_sql_row_limit(value)


# 質問の長さの上限（チャットの入力欄の maxLength と同じ）。
JOB_QUESTION_MAX_LENGTH = 10000
# 永続化したジョブの snapshot を読み戻すときの validation context の key。以前の規則で受け付けて
# 保存した質問（空白だけ・上限超え）でも読めるように、入力の検証を掛けない（#1054）。
STORED_JOB_REQUEST_CONTEXT = "stored_job_request"


class SqlRequirementSpecItem(BaseModel):
    """回答前の確認で確かめた要件の 1 項目（表・列は物理名。#1439）。"""

    section: str
    section_label_ja: str = ""
    label_ja: str = ""
    value_ja: str = ""
    status: str = ""
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    sql_hint: str = ""
    period_range_ja: str = ""


class SqlRequirementSpec(BaseModel):
    """SQL の生成に渡す、回答前の確認の構造化した要件（#1439）。

    画面からは受け取らない。job の作成の API が、反映済みの回答前の確認の session から作る。
    """

    session_id: str
    profile_id: str
    clarified_question: str
    tables: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    items: list[SqlRequirementSpecItem] = Field(default_factory=list)
    unresolved_ja: list[str] = Field(default_factory=list)
    summary_ja: str = ""
    # 回答前の確認の LLM の呼び出しの利用量（session から写す。生成の prompt には入れない）。
    # 実行履歴の行でジョブの利用量と合算する（#3552）。確認が LLM を呼ばなかった・この機能より前の
    # session は None。
    usage: RunModelUsage | None = None

    @field_validator("usage", mode="before")
    @classmethod
    def drop_unreadable_usage(cls, value: Any) -> RunModelUsage | None:
        """読めない利用量の記録は無いものとする（保存したジョブの要求そのものは読む）。"""
        if isinstance(value, RunModelUsage):
            return value
        return parse_run_usage(value)

    def prompt_block(self) -> str:
        """直接の LLM の prompt に入れる「必ず守る条件」（物理名で書く）。"""

        lines = [
            "## 回答前の確認で利用者と確かめた SQL の要件（必ず守る条件）",
            f"- 使う表（FROM）: {'、'.join(self.tables)}（これ以外の表を使わない）",
        ]
        if self.joins:
            lines.append(f"- 表の結合の条件: {' / '.join(self.joins)}")
        for item in self.items:
            if item.section in {"from"}:
                continue
            detail = []
            if item.columns:
                detail.append(f"列: {'、'.join(item.columns)}")
            if item.period_range_ja:
                detail.append(f"期間: {item.period_range_ja}")
            if item.sql_hint:
                detail.append(f"SQL: {item.sql_hint}")
            suffix = f"（{' / '.join(detail)}）" if detail else ""
            lines.append(f"- {item.section_label_ja} {item.label_ja}: {item.value_ja}{suffix}")
        if self.unresolved_ja:
            lines.append("- 確認しないまま進めた項目（質問と Schema から妥当に判断する）:")
            lines.extend(f"  - {value}" for value in self.unresolved_ja)
        return "\n".join(lines)


class RequirementAnswerRecord(BaseModel):
    """回答前の確認で利用者が答えた 1 件（会話に「回答前に確認した内容」として残す。#1839）。"""

    prompt: str
    answer: str


# 実行のきっかけ（実行履歴の種類。#1986）。画面の実行は空。
RunSource = Literal["", "mcp", "evaluation"]
RUN_SOURCE_MCP: RunSource = "mcp"
RUN_SOURCE_EVALUATION: RunSource = "evaluation"
# 記録する呼び出し元の名前・ID の長さの上限（API キーの名前・Agent の Run の ID）。
_ORIGIN_TEXT_MAX_CHARS = 128


def _origin_text(value: object) -> str:
    return value.strip()[:_ORIGIN_TEXT_MAX_CHARS] if isinstance(value, str) else ""


@dataclass(frozen=True)
class JobOrigin:
    """ジョブの実行のきっかけ（MCP の入口が決め、ジョブ → 実行履歴の行に残す。#1986）。

    ジョブの作成の API（`JobCreateRequest`）の項目にしない: 画面の利用者が送った値で種類を
    変えられないように、MCP の入口が認証の結果（API キー・サービストークンの claim）からだけ作る。
    """

    run_source: RunSource = ""
    api_key_name: str = ""
    agent_run_id: str = ""

    def history_fields(self) -> dict[str, str]:
        """`HistoryItem` の項目（`run_source` / `api_key_name` / `agent_run_id`）。"""
        return {
            "run_source": self.run_source,
            "api_key_name": self.api_key_name,
            "agent_run_id": self.agent_run_id,
        }

    def snapshot(self) -> dict[str, str]:
        """ジョブの snapshot に入れる項目（worker が読み戻す）。"""
        return {
            "origin_run_source": self.run_source,
            "origin_api_key_name": self.api_key_name,
            "origin_agent_run_id": self.agent_run_id,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> JobOrigin:
        """snapshot から読む（#1986 より前のジョブ・知らない値は画面の実行とみなす）。"""
        source = data.get("origin_run_source")
        return cls(
            run_source=source if source in (RUN_SOURCE_MCP, RUN_SOURCE_EVALUATION) else "",
            api_key_name=_origin_text(data.get("origin_api_key_name")),
            agent_run_id=_origin_text(data.get("origin_agent_run_id")),
        )

    @classmethod
    def for_mcp(
        cls,
        *,
        api_key_name: object = None,
        service_token_claims: Mapping[str, Any] | None = None,
    ) -> JobOrigin:
        """MCP の入口の呼び出し元から決める。

        API キー（外部の MCP クライアント）は `mcp` とキーの名前。サービストークン（Agent の Run）は
        claim `run_source` が `evaluation`（品質評価の Run）なら `evaluation`、それ以外は `mcp`。
        claim は `PLATFORM_SERVICE_TOKEN_SECRET` で署名済みで、API キーの利用者は付けられない。
        値は実行履歴の見え方（種類）だけに使い、権限・対象範囲の判定には使わない。
        """
        claims = service_token_claims or {}
        evaluation = claims.get("run_source") == RUN_SOURCE_EVALUATION
        return cls(
            run_source=RUN_SOURCE_EVALUATION if evaluation else RUN_SOURCE_MCP,
            api_key_name=_origin_text(api_key_name),
            agent_run_id=_origin_text(claims.get("run_id")),
        )


class JobCreateRequest(BaseModel):
    """非同期 NL2SQL job create request."""

    question: str
    # 生成の方式（OCI Enterprise AI を直接呼ぶ方式だけ。#7）。
    engine: Nl2SqlEngine = Field(
        default=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        description="生成の方式（enterprise_ai_direct だけ）",
    )
    profile_id: str | None = None
    allowed_objects: AllowedObjects = Field(default_factory=AllowedObjects)
    row_limit: int | None = Field(default=None, ge=1, le=5000)
    use_glossary: bool = False
    # SQL の生成の prompt に公開版のオントロジーの文脈を入れる（公開版の確定もこれで決まる）。
    use_ontology_context: bool = True
    # 生成後の接地確認（結果の整形の段階で公開版のグラフを読み、`interpretation.ontology_graph`
    # を作る）の要否。未指定（None）は `use_ontology_context` に従う（SQL 生成の画面・MCP の既定）。
    # 接地確認を表示しないチャットは false を送り、生成の文脈だけを使う（#1172）。
    # 公開版の確定が前提のため、`use_ontology_context` が false なら指定にかかわらず行わない。
    include_ontology_grounding: bool | None = None
    include_interpretation: bool = False
    # 生成と安全検査だけを行い、SQL を実行しない（実行の段階は SKIPPED）。既定は実行あり。
    # チャットのターンは、SQL 生成の画面と同じく生成した SQL を同じジョブで実行する（#1176）。
    # 実行の権限（`nl2sql.sql.execute`）が無い利用者のチャットは、route がこれを true にする。
    # 旧版のチャット（#1176 より前）はこれだけでチャットのターンを表していた（`is_chat_turn`）。
    generation_only: bool = False
    # チャット（会話）のターン（#1176）。会話の継続・本人だけの参照・会話の一覧・ターンの
    # SQL の実行の対象になる。実行は `generation_only` で決める（SQL 生成のジョブと同じ）。
    chat: bool = False
    previous_job_id: str | None = Field(default=None, max_length=64)
    # 画面が送信の前に決める job ID（UUID。#900）。送信の応答が届かなかった（timeout・通信断）
    # ときも、画面はこの ID でジョブを取り直して表示を続けられる。同じ ID の再送は同じジョブを返す。
    client_job_id: UUID | None = None
    # 回答前の確認で反映した session（#1439）。job の作成の API が session を検査し、構造化した
    # 要件を `sql_requirement` に入れる（画面が送った `sql_requirement` は使わない）。
    requirement_session_id: str | None = Field(default=None, max_length=128)
    sql_requirement: SqlRequirementSpec | None = None
    # 回答前の確認を「確認せずに SQL を生成」で飛ばしたチャットのターン（#1839）。
    # 会話の利用者の側に「確認せずに SQL を生成しました」を残すためだけに使い、生成は変えない。
    requirement_skipped: bool = False
    # チャットの質問に添付するファイル（#2517。`POST /api/chat-attachments` の `attachment_id`）。
    # 送信で会話に結び付け、会話の続きの SQL の生成も添付の表・文書を使う。
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)
    # 結び付けた添付の名前と種類（ジョブの作成が `attachment_ids` から作る。画面が送った値は
    # 使わない）。
    attachments: list[ChatAttachmentReference] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str, info: ValidationInfo) -> str:
        # チャット（generation_only）だけでなく、SQL 生成画面・API・MCP のジョブも同じ規則で拒否する
        # （#1054）。保存済みの snapshot の読み戻しは、以前の規則で受け付けた値を残す。
        if isinstance(info.context, Mapping) and info.context.get(STORED_JOB_REQUEST_CONTEXT):
            return value
        if not value.strip():
            raise ValueError("質問を入力してください。")
        if len(value) > JOB_QUESTION_MAX_LENGTH:
            raise ValueError(f"質問は {JOB_QUESTION_MAX_LENGTH} 文字以内で入力してください。")
        return value

    @field_validator("engine", mode="before")
    @classmethod
    def read_stored_engine_value(cls, value: object, info: ValidationInfo) -> object:
        # 保存済みの snapshot の読み戻しだけ、削除した生成方式（#7）の値を唯一の方式として読む。
        if isinstance(info.context, Mapping) and info.context.get(STORED_JOB_REQUEST_CONTEXT):
            return read_stored_engine(value)
        return value

    @model_validator(mode="after")
    def validate_chat_continuation(self) -> JobCreateRequest:
        if self.previous_job_id and not self.is_chat_turn:
            raise ValueError("会話の継続はチャットのジョブで利用できます。")
        return self

    @property
    def is_chat_turn(self) -> bool:
        """チャット（会話）のターンか（旧版のチャットは `generation_only` だけ。#1176）。"""
        return self.chat or self.generation_only

    @property
    def generation_question(self) -> str:
        """SQL の生成に使う質問の文。

        回答前の確認を反映したジョブは、確かめた質問の文（`sql_requirement.clarified_question`）。
        チャットのターンは画面に利用者の質問を残し、生成だけを確かめた文で行う（#1442）。
        """
        requirement = self.sql_requirement
        if requirement is not None and requirement.clarified_question.strip():
            return requirement.clarified_question
        return self.question

    @property
    def requirement_answers(self) -> list[RequirementAnswerRecord]:
        """回答前の確認で利用者が答えた・直した項目（#1839）。

        反映した要件の項目のうち、利用者が答えたもの（`status == "answered"`）。答えた項目は
        確認事項の文を消しているので、項目の名前（無ければ SQL の部分の名前）を確認事項にする。
        """

        requirement = self.sql_requirement
        if requirement is None:
            return []
        return [
            RequirementAnswerRecord(
                prompt=item.label_ja or item.section_label_ja or item.section,
                answer=item.value_ja,
            )
            for item in requirement.items
            if item.status == "answered" and item.value_ja.strip()
        ]

    @property
    def ontology_grounding_requested(self) -> bool:
        """生成後の接地確認をするか（#1172）。"""

        return self.use_ontology_context and self.include_ontology_grounding is not False


class JobCreateData(BaseModel):
    """非同期 job create response."""

    job_id: str
    status: JobStatus
    created_at: str
    steps: list[JobStepData] = Field(default_factory=list)


class SqlChatExecutionSummary(BaseModel):
    """チャットのターンの SQL を最後に実行したときの要約（#1154）。

    結果の行は保存しない（会話は長く残るため、業務データの行を会話に残さない）。会話を開き直した
    画面は、この要約と「もう一度実行」を出す。
    """

    status: Literal["done", "error"]
    executed_at: str
    elapsed_ms: int = 0
    row_count: int = 0
    column_count: int = 0
    has_more: bool = False
    error_code: str | None = None
    # 実行を記録した実行履歴（監査）の ID。
    history_id: str = ""
    # ジョブの中で実行したとき（#1176）、結果の行を受け取れる期限（ISO 8601）。行は会話に残さず、
    # 画面が 1 回だけ受け取る（`POST /jobs/{job_id}/execution-result`）。明示の実行（`/execute`）は
    # 応答で行を返すので None。
    result_expires_at: str | None = None


class SqlChatExecuteData(BaseModel):
    """チャットのターンの SQL の実行の結果（#1154）。

    実行の失敗（Oracle のエラー・実行の時点の安全検査の不合格）は 200 で `status="error"` にし、
    SQL 生成のジョブと同じく `error_message`（利用者向け）・`error_code`・`error_detail`（技術的な
    詳細）に分ける。
    """

    job_id: str
    status: Literal["done", "error"]
    executed_at: str
    elapsed_ms: int = 0
    executable_sql: str = ""
    results: QueryResults
    # 1 回の実行で取得する行数の上限・セルの文字数の上限（設定値。画面の打ち切りの文言に使う）。
    row_limit: int
    max_cell_chars: int
    # セルの文字数の上限で文字を切ったセルがあるか。
    cells_truncated: bool = False
    error_message: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    history_id: str = ""


class JobData(BaseModel):
    """非同期 job status response."""

    # 処理の経過（#3226 の案 A）: span は共通の span の表にあり、画面は `GET /jobs/{job_id}/trace`
    # で読む。ここは trace id と、書けなかった span の数（1 以上なら「一部の処理だけを記録して
    # います」）。
    trace_id: str = ""
    trace_dropped_spans: int = 0

    business_release_id: str = ""
    # チャットの質問に添付したファイル（#2517。名前・種類・大きさ）。
    attachments: list[ChatAttachmentReference] = Field(default_factory=list)
    job_id: str
    status: JobStatus
    # 実行に使った業務プロファイル（未指定のジョブは "default"）。MCP の結果に載せる（#231）。
    profile_id: str = ""
    question: str = ""
    # 回答前の確認で確かめた質問の文（反映したジョブだけ。チャットの回答に「確かめた要件」として
    # 出す。#1442）。
    requirement_question: str = ""
    # 回答前の確認で利用者が答えた内容（反映したジョブの、利用者が答えた・直した項目。#1839）。
    requirement_answers: list[RequirementAnswerRecord] = Field(default_factory=list)
    # 回答前の確認を飛ばしたチャットのターン（#1839）。
    requirement_skipped: bool = False
    conversation_id: str = ""
    previous_job_id: str | None = None
    generation_only: bool = False
    # チャット（会話）のターンか（#1176。旧版のチャットは generation_only だけで表す）。
    chat: bool = False
    # 生成方法（チャットの段階の補足に出す。#1145）。
    engine: StoredNl2SqlEngine | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int | None = None
    result: Nl2SqlResult | None = None
    error_message: str | None = None
    # 機械判定用の失敗分類(例: SCHEMA_CATALOG_EMPTY)。表示文言は error_message が正本。
    error_code: str | None = None
    # 失敗の技術的な詳細（例外・Oracle のエラーの元の文）。error_message は利用者向けの 1 文目と
    # 次の操作だけにし、画面はこれを「詳細」に畳んで出す（messaging.md §10.3。#1072）。
    error_detail: str | None = None
    warning_message: str | None = None
    timing: TimingEnvelope | None = None
    steps: list[JobStepData] = Field(default_factory=list)
    # 実行の回数（worker が claim するたびに増える。0 は未着手）。lease の切れたジョブを別の実行が
    # 引き継ぐと段階は初めからになる（#1358）。処理の段階のイベントの試行（`attempt`）は
    # これより 1 小さい（最初の実行が 0。#1359）。
    attempt: int = 0
    # チャットのターンの SQL を最後に実行したときの要約（行は持たない。#1154）。
    last_execution: SqlChatExecutionSummary | None = None
    # 会話の持ち主以外（system_admin）が送ったターンの送り手（応答には出さない。表示名の解決に
    # 使う。#1928）。持ち主が送ったターン・会話の外のジョブは None。
    sender_user_uuid: str | None = Field(default=None, exclude=True)
    # 会話の持ち主以外が送ったターンの送り手の表示名（共通の表示名。#1928・#1696）。持ち主が
    # 送ったターンは None、表示名を引けない利用者は空（画面が「削除済みのユーザー」と出す）。
    sender_display_name: str | None = None


class SqlChatUpdateRequest(BaseModel):
    """会話の名前の変更（#1817。RAG・Agent の会話名と同じ規則: 前後の空白を除いて 1〜80 文字）。"""

    title: str = Field(..., min_length=1, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SqlChatSummary(BaseModel):
    """永続ジョブを会話の単位で表示する。"""

    id: str
    title: str
    profile_id: str
    created_at: str
    # 会話の持ち主（応答には出さない。表示名の解決と本人の会話の判定に使う。#1889）。
    actor_user_uuid: str = Field(default="", exclude=True)
    # 見ている利用者の会話か（system_admin が他の利用者の会話を見るときは false。画面は履歴に
    # 持ち主の表示名を添える。続き・SQL の実行は system_admin もできる。#1889・#1928）。
    owned: bool = True
    # system_admin の一覧だけに出す利用者の表示名（#1889・#1696。引けない利用者は空）。
    actor_display_name: str = ""


class SqlChatPage(ChatHistoryPage[SqlChatSummary]):
    """会話の履歴の 1 ページ（最後の更新の新しい順。任意のページへ移る offset 型。#1265・#1891）。

    期間（最後の更新が直近 N 日）は `history_days`（#2340）。
    """


class SqlChatData(BaseModel):
    conversation: SqlChatSummary
    turns: list[JobData]


class TraceProgressData(BaseModel):
    """記録（ジョブ・実行履歴・DB 管理チャットのターン）の処理の経過（#3226 の案 A）。

    共通の span の表の snapshot（span ごとに最新の状態 1 件と、記録が終わっていれば終端）。`seq` は
    この一覧の中の番号。`dropped_spans` は書けなかった span の数（1 以上なら一部だけの記録）。
    """

    events: list[ChatProgressEvent] = Field(default_factory=list)
    dropped_spans: int = 0


# 実行履歴の行の状態（#3348）。空は SQL を実行した行、`cancelled` は停止で終わったジョブの行。
HistoryRunStatus = Literal["", "cancelled"]
HISTORY_RUN_STATUS_CANCELLED: HistoryRunStatus = "cancelled"


class HistoryCancellation(BaseModel):
    """実行履歴の「取消」（取り消した人と入口。応答だけに付ける。#3348）。

    `via` は画面の操作（`screen`）・外部の MCP クライアントの `nl2sql_cancel_job`（`mcp`。
    API キー）・Agent の実行から（`agent_run`。Agent の Run の取消に連動した取消と、Agent が
    呼んだ取消）。
    API キーの名前は見ている利用者が見られるキーだけ（見られなければ空で、画面は「MCP」だけ）。
    """

    via: Literal["screen", "mcp", "agent_run"]
    # 取り消した人を記録しているか（認証無効の期間の取消は False）。
    actor_recorded: bool = False
    # 取り消した人の表示名（引けない利用者〔削除済み〕・記録の無い取消は ""）。
    actor_display_name: str = ""
    by_me: bool = False
    api_key_name: str = ""


class HistoryItem(BaseModel):
    """実行履歴。"""

    generation_only: bool = False

    # 処理の経過（#3226 の案 A）: 実行したジョブ（span の `OPERATION_ID`）・trace id・書けなかった
    # span の数・工程の詳細（span id → 詳細。表には入れない内容）。画面は
    # `GET /history/{history_id}/trace` で読む（一覧・詳細の応答には工程の詳細を入れない）。
    job_id: str = ""
    trace_id: str = ""
    trace_dropped_spans: int = 0
    span_details: dict[str, dict[str, Any]] = Field(default_factory=dict)

    business_release_id: str = ""
    id: str
    question: str
    engine: StoredNl2SqlEngine
    generated_sql: str
    created_at: str
    elapsed_ms: int | None = None
    generation_elapsed_ms: int | None = None
    engine_timings: list[EngineTiming] = Field(default_factory=list)
    stage_timings: list[StageTiming] = Field(default_factory=list)
    feedback_rating: FeedbackRating | None = None
    profile_id: str = ""
    profile_name: str = ""
    profile_category: str = ""
    rewritten_question: str = ""
    executable_sql: str = ""
    safety_is_safe: bool = True
    result_row_count: int = 0
    result_columns: list[str] = Field(default_factory=list)
    feedback_comment: str = ""
    feedback_updated_at: str = ""
    # 実行結果を評価した利用者（実行した本人か、system_admin。#1928）。
    feedback_actor_user_uuid: str = ""
    admin_feedback_rating: FeedbackRating | None = None
    admin_feedback_content: str = ""
    admin_feedback_updated_at: str = ""
    session_id: str = ""
    # 実行した人（executor）。利用量・監査・API キーの上限もこの人に数える（#1928・#1954）。
    actor_user_uuid: str = ""
    # チャットの往復なら会話の持ち主（会話の最初のターンの actor。#1954）。会話の無い実行
    # （SQL 生成・SQL 直接実行など）は空。行は実行した人と会話の持ち主の両方に見える。
    conversation_owner_user_uuid: str = ""
    # 履歴 API の管理者向け応答で現在のユーザー情報を補完する。
    actor_login_user_id: str = ""
    actor_display_name: str = ""
    # 以下は履歴 API の応答で、見ている利用者に合わせて補完する（保存した値は使わない。#1954）。
    # 見ている利用者が実行した行か。
    executed_by_me: bool = False
    # 実行した人の表示名（見る人を問わず返す。引けなければ空）。
    executor_display_name: str = ""
    # 会話の持ち主の表示名と、見ている利用者が持ち主か（会話の無い実行は None）。
    conversation_owner_display_name: str | None = None
    owned_by_me: bool | None = None
    # 実行のきっかけ（#1986）。画面の実行は空、MCP の入口（`POST /api/mcp`）から来た実行は
    # `mcp`（外部の MCP クライアントの API キーか、Agent の Run のサービストークン）、Agent の
    # 品質評価の Run のサービストークン（claim `run_source` = `evaluation`）は `evaluation`。
    # #1986 より前の行は空のまま（移行で埋めない）。
    run_source: RunSource = ""
    # MCP の呼び出し元（技術的な詳細と種類の表示）。API キーの名前（外部の MCP クライアント）と、
    # Agent の Run の ID（サービストークンの claim `run_id`）。どちらも秘密ではない。
    api_key_name: str = ""
    agent_run_id: str = ""
    # 停止で終わった実行（#3348）。空は SQL を実行した行（完了・ブロックは `safety_is_safe`）。
    # 停止の行は SQL・実行結果・評価を持たない。
    run_status: HistoryRunStatus = ""
    # 取り消した人と入口（停止の行だけ。取消の要求の記録から写す。#3348）。入口は MCP の
    # `nl2sql_cancel_job`（`mcp`。API キーの ID か Agent の Run の ID）か画面（空）。応答では
    # `cancellation` に置き換え、この 4 つは返さない（`_with_history_people`）。
    cancelled_by_user_uuid: str = ""
    cancel_via: str = ""
    cancel_api_key_id: str = ""
    cancel_agent_run_id: str = ""
    # 取り消した人と入口の表示（応答だけ。見ている利用者に合わせて補完する。#3348）。
    cancellation: HistoryCancellation | None = None
    # 種類（チャットの往復 `chat` / SQL 生成 `query` / MCP `mcp` / 品質評価 `evaluation`）。
    # きっかけ（`run_source`）と会話の持ち主の有無で決め、応答だけに入れる（#1955・#1986）。
    kind: Literal["chat", "query", "mcp", "evaluation"] | None = None
    ontology_trace_summary: dict[str, Any] = Field(default_factory=dict)
    # 実行のモデルの呼び出しの回数と利用量（ジョブの trace の `gen_ai.chat` の span から集める。
    # 3 製品共通の `pr_backend_core.observability.model_usage`。#3543）。この機能より前の行と、
    # trace の始まる前に止まった実行は None（画面は「未計測」）。
    usage: RunModelUsage | None = None
    # 一覧の「token」の列（合計の token と計測の状態。`usage` から応答だけに入れる。#3543）。
    # 利用量の記録の無い行はどちらも None（0 にしない）。
    total_tokens: int | None = None
    usage_measurement: UsageMeasurement | None = None

    @field_validator("usage", mode="before")
    @classmethod
    def drop_unreadable_usage(cls, value: Any) -> RunModelUsage | None:
        """読めない利用量の記録は未計測にする（行そのものは読む）。"""
        if isinstance(value, RunModelUsage):
            return value
        return parse_run_usage(value)

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_feedback_rating(cls, value: Any) -> Any:
        """廃止済み評価(旧 needs_review 等)を持つ snapshot は None(未評価)へ縮退する。"""
        if not isinstance(value, dict):
            return value
        rating = value.get("feedback_rating")
        if rating is not None and rating not in tuple(FeedbackRating):
            migrated = dict(value)
            migrated["feedback_rating"] = None
            return migrated
        return value


class HistoryData(Page[HistoryItem]):
    """実行履歴の 1 ページ（任意のページへ移る offset 型。#1891）。

    `total` は絞り込み後の件数、`unfiltered_total` は絞り込み（質問・評価・安全性）を掛けない、
    利用者が見られる範囲の件数（画面の「N 件（全 M 件）」）。種類の絞り込みが品質評価を含まない
    （既定の「品質評価以外」など）ときは、品質評価の行を除いて数える（#1986。Agent と同じ）。
    `others_visible` は、見ている利用者に他の人が実行した行が見えるか（system_admin と、自分の
    会話で他の人が実行した行を持つ利用者。画面の「実行者」の列と絞り込みを出す。#1955）。
    """

    unfiltered_total: int = 0
    others_visible: bool = False
    # 種類の既定（品質評価以外）で見える行が 0 件のときだけ、隠している品質評価の行の数（空の状態に
    # 「品質評価の実行（N 件）は既定で表示しません」を出す。ほかは 0。#1986）。
    hidden_evaluation_total: int = 0


class FeedbackRequest(BaseModel):
    """実行結果への feedback request."""

    history_id: str
    rating: FeedbackRating
    comment: str = ""
    # 「違う」のときは利用者コメントが必須（画面の欄と同じ規則・文言。#540）。省略時も検証する。
    feedback_content: str = Field(default="", validate_default=True)

    @field_validator("feedback_content")
    @classmethod
    def require_content_when_bad(cls, value: str, info: ValidationInfo) -> str:
        """旧名 comment だけを送る client も受け付け、どちらも空の「違う」だけを拒否する。"""
        comment = info.data.get("comment", "")
        if info.data.get("rating") == FeedbackRating.BAD and not (value.strip() or comment.strip()):
            raise ValueError("「違う」のときは利用者コメントを入力してください。")
        return value

    @model_validator(mode="after")
    def normalize_feedback_content(self) -> FeedbackRequest:
        """UI の feedback_content 名と既存 comment 名を互換にする。"""
        if not self.comment and self.feedback_content:
            self.comment = self.feedback_content
        elif not self.feedback_content and self.comment:
            self.feedback_content = self.comment
        return self


class FeedbackData(BaseModel):
    """Feedback response."""

    history_id: str
    rating: FeedbackRating
    saved: bool
    comment: str = ""
    feedback_content: str = ""


class FeedbackClearData(BaseModel):
    """アプリ内 feedback を履歴を残したまま解除した response。"""

    history_id: str
    cleared: bool = True


class FeedbackRecord(HistoryItem):
    """管理画面向けのアプリ内 feedback と classifier 連携状態。"""

    training_status: str = ""
    training_example_id: str = ""


class FeedbackListData(Page[FeedbackRecord]):
    """アプリ内 feedback 一覧の 1 ページ（offset 型。#1891）。

    `unfiltered_total` は絞り込み（評価・業務プロファイル・キーワード）を掛けない件数。
    """

    unfiltered_total: int = 0


class FeedbackIndexRequest(StrictMutationRequest):
    """Feedback learning index management request."""

    include_bad: bool = Field(
        default=False,
        deprecated=True,
        description="互換用の非推奨フィールドです。現在は管理者レビューが良い履歴のみ索引対象です。",
    )


class FeedbackIndexData(BaseModel):
    """Feedback learning vector index status / operation response."""

    operation: str
    status: str
    executed: bool = False
    runtime: str = "deterministic"
    source_history_count: int = 0
    indexable_count: int = 0
    indexed_count: int = 0
    vector_dimension: int = EMBEDDING_DIMENSIONS
    vector_backend: str = "oracle_26ai"
    embedding_provider: str = "genai"
    embedding_model: str = ""
    embedding_configured: bool = False
    ddl: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope


class FeedbackVectorEntry(BaseModel):
    """Feedback vector/index management row shown in Learning operations."""

    history_id: str
    question: str
    generated_sql: str
    profile_id: str = ""
    profile_name: str = ""
    profile_category: str = ""
    feedback_rating: FeedbackRating | None = None
    feedback_comment: str = ""
    admin_feedback_rating: FeedbackRating | None = None
    admin_feedback_content: str = ""
    admin_feedback_updated_at: str = ""
    indexed: bool = False
    created_at: str = ""


class FeedbackEntriesData(BaseModel):
    """Feedback management entries response."""

    items: list[FeedbackVectorEntry] = Field(default_factory=list)
    total: int = 0
    indexed_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class FeedbackEntriesDeleteRequest(BaseModel):
    """Delete feedback/history entries from the learning store."""

    history_ids: list[str] = Field(default_factory=list)


class FeedbackSearchConfigRequest(BaseModel):
    """Similarity search defaults used when request does not override them."""

    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    match_limit: int | None = Field(default=None, ge=1, le=20)


# 類似検索インデックスの「最低スコア」の既定（設定を保存していないときの値。#1721）。
DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD = 0.9


class FeedbackSearchConfigData(BaseModel):
    """Current feedback learning retrieval config."""

    similarity_threshold: float = DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD
    match_limit: int = 3


class DemoLearningData(BaseModel):
    """Demo learning data seed response."""

    seeded_history_count: int
    seeded_feedback_count: int
    history_ids: list[str] = Field(default_factory=list)
    profile_ids: list[str] = Field(default_factory=list)
    message: str


class SimilarHistoryRequest(BaseModel):
    """類似履歴検索 request."""

    question: str = Field(min_length=1)
    profile_id: str | None = None
    engine: Nl2SqlEngine | None = None
    limit: int | None = Field(default=None, ge=1, le=20)


class SimilarHistoryItem(BaseModel):
    """類似履歴の 1 件。"""

    history_id: str
    question: str
    sql: str
    profile_id: str = ""
    profile_name: str = ""
    score: float
    reason: str


class SimilarHistoryData(BaseModel):
    """類似履歴検索 response."""

    items: list[SimilarHistoryItem] = Field(default_factory=list)
    used_for_generation: bool = True
    engine: StoredNl2SqlEngine | None = None


class ProfileRecommendationRequest(BaseModel):
    """質問から業務 profile / schema 範囲を推薦する request."""

    question: str = Field(min_length=1)
    current_profile_id: str | None = None


class ProfileRecommendationCandidate(BaseModel):
    """Profile recommendation candidate."""

    profile_id: str
    profile_name: str
    score: float
    matched_terms: list[str] = Field(default_factory=list)
    allowed_tables: list[str] = Field(default_factory=list)
    category: str = ""


class ProfileRecommendationData(BaseModel):
    """Profile recommendation response."""

    recommended_profile_id: str
    recommended_profile_name: str
    recommended_profile_category: str = ""
    confidence: float
    confidence_threshold: float = 0.3
    reason: str
    rewritten_question: str
    recommended_allowed_objects: AllowedObjects
    candidates: list[ProfileRecommendationCandidate] = Field(default_factory=list)
    recommendation_source: str = "deterministic"
    classifier_version: str = ""
    category_scores: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ClassifierTrainingExample(BaseModel):
    """Imported classifier training example."""

    id: str
    category: str
    text: str
    profile_id: str = ""
    profile_name: str = ""
    profile_category: str = ""
    source: str = ""
    source_type: Literal["file", "feedback"] = "file"
    source_history_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class ClassifierTrainingExampleUpdateRequest(BaseModel):
    """Training example の質問/Profile 対応を修正する request。"""

    text: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)


class ClassifierTrainingCandidate(BaseModel):
    """SQL feedback から導出した classifier training 候補。"""

    history_id: str
    question: str
    profile_id: str = ""
    profile_name: str = ""
    profile_category: str = ""
    feedback_rating: FeedbackRating | None = None
    feedback_comment: str = ""
    created_at: str = ""
    status: Literal[
        "pending",
        "added",
        "already_covered",
        "conflict",
        "profile_missing",
        "source_changed",
    ]
    eligible: bool = False
    training_example_id: str = ""
    conflict_profile_ids: list[str] = Field(default_factory=list)


class ClassifierTrainingCandidatesData(Page[ClassifierTrainingCandidate]):
    """feedback training 候補一覧の 1 ページ（offset 型。件数の内訳を足す。#1891）。

    `unfiltered_total` は絞り込み（状態・業務プロファイル・キーワード・履歴）を掛けない件数。
    """

    unfiltered_total: int = 0
    pending_count: int = 0
    added_count: int = 0
    attention_count: int = 0


class ClassifierFeedbackSelection(BaseModel):
    """Feedback 候補の確認対象。profile_id は確認時の修正値を許可する。"""

    history_id: str = Field(min_length=1)
    profile_id: str = ""


class ClassifierFeedbackImportRequest(BaseModel):
    """確認済み feedback を training data に追加する request。"""

    items: list[ClassifierFeedbackSelection] = Field(min_length=1, max_length=100)


class ClassifierFeedbackImportResult(BaseModel):
    """Feedback ごとの追加結果。"""

    history_id: str
    status: str
    training_example_id: str = ""
    profile_id: str = ""
    message: str = ""


class ClassifierFeedbackImportData(BaseModel):
    """Feedback training data 追加 response。"""

    imported_count: int = 0
    skipped_count: int = 0
    total_examples: int = 0
    stale: bool = False
    results: list[ClassifierFeedbackImportResult] = Field(default_factory=list)


class ClassifierImportData(BaseModel):
    """Classifier training data import response."""

    imported_count: int
    skipped_count: int = 0
    total_examples: int
    categories: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    examples: list[ClassifierTrainingExample] = Field(default_factory=list)


class ClassifierTrainingDataData(BaseModel):
    """Classifier training data listing response."""

    total_examples: int = 0
    categories: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    examples: list[ClassifierTrainingExample] = Field(default_factory=list)


class ClassifierStatusData(BaseModel):
    """Classifier training/runtime status."""

    ready: bool = False
    trained: bool = False
    stale: bool = False
    classifier_version: str = ""
    updated_at: str = ""
    example_count: int = 0
    category_count: int = 0
    categories: list[str] = Field(default_factory=list)
    embedding_model: str = ""
    vector_dimension: int = EMBEDDING_DIMENSIONS
    persistence_mode: str = "memory"
    recommendation_source: str = "deterministic"
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    trained_example_count: int = 0
    pending_change_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ClassifierModelInfo(BaseModel):
    """Persisted current classifier model metadata."""

    version: str
    active: bool = False
    updated_at: str = ""
    category_count: int = 0
    categories: list[str] = Field(default_factory=list)
    embedding_model: str = ""
    vector_dimension: int = EMBEDDING_DIMENSIONS
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    source: str = "oracle_state"


class ClassifierModelImportData(BaseModel):
    """Single classifier model artifact import response.

    ``active_version`` is retained for compatibility with the former registry API.
    """

    imported: bool = False
    active_version: str = ""
    model: ClassifierModelInfo | None = None
    warnings: list[str] = Field(default_factory=list)


class ClassifierTrainRequest(BaseModel):
    """Train LogisticRegression classifier from imported examples."""

    min_examples_per_category: int = Field(default=1, ge=1, le=100)


class ClassifierPredictRequest(BaseModel):
    """Classifier prediction request."""

    question: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)


class ClassifierPredictionCandidate(BaseModel):
    """Classifier prediction candidate mapped to a profile when possible."""

    category: str
    score: float
    profile_id: str = ""
    profile_name: str = ""
    profile_category: str = ""


class ClassifierPredictionData(BaseModel):
    """Classifier prediction response."""

    recommendation_source: str
    classifier_version: str = ""
    predicted_category: str = ""
    confidence: float = 0.0
    candidates: list[ClassifierPredictionCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RewriteRequest(BaseModel):
    """Question rewrite request."""

    question: str = Field(min_length=1)
    profile_id: str | None = None
    use_glossary: bool = True
    # LLM 書き換えを廃止したため未使用。既存 client 互換のため field だけ残す。
    extra_prompt: str = ""


class RewriteData(BaseModel):
    """Question rewrite response."""

    original_question: str
    rewritten_question: str
    source: str = "deterministic"
    model: str = ""
    warnings: list[str] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    """SQL analysis request."""

    sql: str = Field(min_length=1)
    allowed_objects: AllowedObjects = Field(default_factory=AllowedObjects)
    row_limit: int | None = Field(default=None, ge=1, le=5000)
    use_llm: bool = False


class AnalyzeData(BaseModel):
    """SQL analysis response."""

    safety: SafetyReport
    explanation: str
    recommendations: list[str]
    executable_sql: str
    repaired_sql: str = ""
    optimization_hints: list[str] = Field(default_factory=list)
    structure_summary: str = ""
    risk_level: str = "low"
    statement_type: str = "SELECT"
    object_names: list[str] = Field(default_factory=list)
    column_names: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    risk_findings: list[str] = Field(default_factory=list)
    repair_candidates: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    aggregations: list[str] = Field(default_factory=list)
    llm_enhanced: bool = False
    llm_warnings: list[str] = Field(default_factory=list)


class ProfileDeleteData(BaseModel):
    """業務 profile 削除の結果。"""

    profile: Nl2SqlProfile


class AdminFeedbackReviewRequest(BaseModel):
    """管理者によるアプリ内 feedback review request。"""

    history_id: str = Field(min_length=1)
    rating: FeedbackRating
    # 「違う」のときは管理者レビューコメントが必須（画面の欄と同じ規則・文言。#540）。
    feedback_content: str = Field(default="", validate_default=True)

    @field_validator("feedback_content")
    @classmethod
    def require_content_when_bad(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("rating") == FeedbackRating.BAD and not value.strip():
            raise ValueError("「違う」のときは管理者レビューコメントを入力してください。")
        return value


class SimilarHistoryPublishData(BaseModel):
    """管理者 review に連動した類似履歴公開状態。"""

    history_id: str = ""
    status: Literal["published", "unpublished", "skipped", "warning"] = "skipped"
    runtime: str = "deterministic"
    executed: bool = False
    table_name: str = ""
    index_name: str = ""
    warnings: list[str] = Field(default_factory=list)


class AdminFeedbackReviewData(BaseModel):
    """管理者 feedback review response."""

    history_id: str
    rating: FeedbackRating
    saved: bool
    feedback_content: str = ""
    similar_history_publish: SimilarHistoryPublishData | None = None


class ReverseSqlRequest(BaseModel):
    """SQL から自然言語説明を生成する request."""

    sql: str = Field(min_length=1)
    profile_id: str | None = None
    use_glossary: bool = False


class StructureToSqlRequest(BaseModel):
    """編集した論理構造のみを SQL 生成要件として受け付ける。"""

    logical_structure: str = Field(min_length=1, max_length=100_000)
    profile_id: str | None = None
    use_glossary: bool = False


class QuestionToSqlRequest(BaseModel):
    """表示中の自然言語質問と対象 Profile だけを SQL 生成要件として受け付ける。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=100_000)
    profile_id: str | None = None


class StructureToSqlOutput(BaseModel):
    """LLM は再構築不能の場合に空 SQL と理由を返す。成功 API 応答とは分離する。"""

    sql: str
    explanation: str = ""


class StructureToSqlData(BaseModel):
    sql: str = Field(min_length=1)
    explanation: str = ""
    source: str = "enterprise_ai"
    warnings: list[str] = Field(default_factory=list)


class ReverseStructureOutput(BaseModel):
    logical_structure: str = Field(min_length=1, max_length=100_000)


class ReverseQuestionOutput(BaseModel):
    question: str = Field(min_length=1, max_length=100_000)
    explanation: str = ""
    logical_steps: list[str] = Field(default_factory=list, max_length=20)


class ReverseSqlData(BaseModel):
    """SQL reverse explanation response."""

    question: str
    explanation: str
    referenced_tables: list[str]
    sql_structure: str = ""
    logical_structure: str = ""
    logical_structure_items: list[Nl2SqlLogicalStructureItem] = Field(default_factory=list)
    logical_steps: list[str] = Field(default_factory=list)
    logical_step_details: list[Nl2SqlLogicalStep] = Field(default_factory=list)
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class CommentSuggestion(BaseModel):
    """Table / column comment suggestion."""

    object_name: str
    object_type: str
    suggested_comment: str


class CommentSuggestionRequest(BaseModel):
    """Comment generation options."""

    use_llm: bool = False
    max_items: int = Field(default=120, ge=1, le=500)


class CommentSuggestionData(BaseModel):
    """Comment suggestions response."""

    suggestions: list[CommentSuggestion]
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class CommentApplyItem(BaseModel):
    """Table / column comment apply request item."""

    object_name: str = Field(min_length=1, max_length=260)
    object_type: str = Field(default="column", min_length=1, max_length=32)
    comment: str = Field(min_length=1, max_length=4000)


class CommentApplyRequest(AdminExecutionConfirmation):
    """Restricted COMMENT ON execution request."""

    items: list[CommentApplyItem] = Field(default_factory=list)


class CommentApplyStatement(BaseModel):
    """Generated COMMENT ON statement result."""

    object_name: str
    object_type: str
    comment: str
    sql: str
    status: str = "pending"
    error_message: str = ""


class CommentApplyData(BaseModel):
    """Restricted COMMENT ON execution response."""

    executed: bool = False
    runtime: str = "deterministic"
    statements: list[CommentApplyStatement] = Field(default_factory=list)
    schema_refresh_job_id: str = ""
    schema_refresh_required: bool = False
    schema_refresh_reason_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope


class AnnotationSuggestion(BaseModel):
    """Oracle annotation suggestion for a table/view/column."""

    object_name: str
    object_type: str = "table"
    annotation_name: str = "DESCRIPTION"
    annotation_value: str


class AnnotationSuggestionData(BaseModel):
    """Annotation suggestions response."""

    suggestions: list[AnnotationSuggestion] = Field(default_factory=list)
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class AnnotationApplyItem(BaseModel):
    """Annotation apply request item."""

    object_name: str = Field(min_length=1, max_length=260)
    object_type: str = Field(default="table", min_length=1, max_length=32)
    annotation_name: str = Field(default="DESCRIPTION", min_length=1, max_length=64)
    annotation_value: str = Field(min_length=1, max_length=4000)


class AnnotationApplyRequest(AdminExecutionConfirmation):
    """Restricted Oracle annotation execution request."""

    items: list[AnnotationApplyItem] = Field(default_factory=list)


class AnnotationApplyStatement(BaseModel):
    """Generated Oracle annotation statement result."""

    object_name: str
    object_type: str
    annotation_name: str
    annotation_value: str
    sql: str
    status: str = "pending"
    error_message: str = ""


class AnnotationApplyData(BaseModel):
    """Oracle annotation apply response."""

    executed: bool = False
    runtime: str = "deterministic"
    statements: list[AnnotationApplyStatement] = Field(default_factory=list)
    schema_refresh_job_id: str = ""
    schema_refresh_required: bool = False
    schema_refresh_reason_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    timing: TimingEnvelope


class MetadataSqlTarget(BaseModel):
    """Comment / annotation generation target object."""

    owner: str = ""
    object_name: str = Field(min_length=1, max_length=260)
    object_type: Literal[
        "table",
        "view",
        "materialized_view",
        "materialized view",
        "TABLE",
        "VIEW",
        "MATERIALIZED_VIEW",
        "MATERIALIZED VIEW",
    ] = "table"

    @field_validator("object_type", mode="before")
    @classmethod
    def normalize_object_type(cls, value: str) -> str:
        return str(value or "table").lower()


class DomainAnnotation(BaseModel):
    """ドメインに付いた annotation(名前と値)。"""

    name: str = Field(min_length=1, max_length=1024)
    value: str = Field(default="", max_length=4000)


class DomainColumnRef(BaseModel):
    """ドメインが関連付いた表の列。"""

    owner: str = ""
    table_name: str = Field(min_length=1, max_length=128)
    column_name: str = Field(min_length=1, max_length=128)


class DomainDefinition(BaseModel):
    """dictionary(ALL_DOMAINS 等)から復元した既存ドメインの定義。"""

    owner: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    domain_type: Literal["single", "multi_column", "enumerated", "flexible"] = "single"
    data_type: str = ""
    strict: bool = False
    nullable: bool = True
    constraints: list[str] = Field(default_factory=list)
    display: str = ""
    order: str = ""
    annotations: list[DomainAnnotation] = Field(default_factory=list)
    columns: list[DomainColumnRef] = Field(default_factory=list)
    """schema 内でこのドメインが関連付いた全ての表列(選択外の表を含む)。"""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qualified_name(self) -> str:
        return qualified_object_name(self.owner, self.name)


class DomainInventoryRequest(BaseModel):
    """対象表の列に付いた既存ドメインの取得要求。"""

    targets: list[MetadataSqlTarget] = Field(default_factory=list, max_length=100)


class DomainInventoryData(BaseModel):
    """既存ドメインの定義と、LLM へ渡す表示テキスト。"""

    domains: list[DomainDefinition] = Field(default_factory=list)
    domain_text: str = ""
    runtime: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


DomainOperation = Literal["create", "update", "rebuild", "delete"]


class MetadataSqlSampleTarget(MetadataSqlTarget):
    """対象オブジェクトから代表値を取得するための列指定。"""

    columns: list[str] = Field(default_factory=list, max_length=1000)


class MetadataSqlSampleRequest(BaseModel):
    """コメント/アノテーション SQL 生成前のサンプル再取得要求。"""

    targets: list[MetadataSqlSampleTarget] = Field(default_factory=list, max_length=100)
    sample_limit: int = Field(default=10, ge=0, le=100)


class MetadataSqlSampleData(BaseModel):
    """SQL 生成に渡す再取得済みサンプル。"""

    sample_text: str = ""
    sample_count: int = 0
    runtime: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class MetadataSqlGenerateRequest(BaseModel):
    """SQL Assist comment / annotation generation input."""

    targets: list[MetadataSqlTarget] = Field(default_factory=list)
    structure_text: str = ""
    primary_key_text: str = ""
    foreign_key_text: str = ""
    sample_text: str = ""
    extra_text: str = ""
    operation: DomainOperation = "create"
    """ドメイン管理だけが使う操作種別。コメント/アノテーションでは無視する。"""
    domain_text: str = ""
    domains: list[DomainDefinition] = Field(default_factory=list, max_length=200)


class MetadataSqlGenerateData(BaseModel):
    """Generated comment / annotation SQL."""

    sql: str = ""
    source: str = "deterministic"
    warnings: list[str] = Field(default_factory=list)
    # OCI Enterprise AI で作れず、規則ベース（名前をそのまま入れた SQL）にしたか（#1719）。
    # 未設定で AI を使わなかった場合は False（`warnings` に出す）。
    fallback: bool = False
    # 規則ベースにした表（一部の組だけ失敗した場合。空 = 全部）。
    fallback_objects: list[str] = Field(default_factory=list)
    # 利用者向けの理由（1 文）と、「詳細」に出す失敗の元の文（理由・上限・使った token）。
    fallback_reason: str = ""
    fallback_detail: str = ""
    timing: TimingEnvelope


class DiagnosticCheck(BaseModel):
    """接続/設定診断の 1 項目。"""

    name: str
    status: str
    message: str


class DiagnosticReadiness(BaseModel):
    """運用 readiness の集約表示。"""

    area: str
    label: str
    status: str
    summary: str
    next_action: str = ""
    related_checks: list[str] = Field(default_factory=list)


class DiagnosticSmokeCheck(BaseModel):
    """Manual/live smoke check item for Oracle / OCI NL2SQL engines."""

    id: str
    label: str
    category: str
    status: str
    method: str = ""
    endpoint: str = ""
    request_hint: str = ""
    command: str = ""
    expected: str
    next_action: str = ""
    related_readiness: list[str] = Field(default_factory=list)


class DiagnosticConfigVar(BaseModel):
    """診断設定ガイドで表示する env var。値は返さず状態だけ返す。"""

    name: str
    status: str
    required: bool = True
    note: str = ""


class DiagnosticConfigGuide(BaseModel):
    """OCI / Oracle 設定を完了するための非 secret ガイド。"""

    id: str
    label: str
    status: str
    summary: str
    next_action: str = ""
    required_env_vars: list[DiagnosticConfigVar] = Field(default_factory=list)
    optional_env_vars: list[DiagnosticConfigVar] = Field(default_factory=list)
    env_template: str = ""
    smoke_command: str = ""
    related_readiness: list[str] = Field(default_factory=list)


class DiagnosticsData(BaseModel):
    """OCI / Oracle / engine 設定診断 response."""

    checks: list[DiagnosticCheck]
    readiness: list[DiagnosticReadiness] = Field(default_factory=list)
    smoke_checks: list[DiagnosticSmokeCheck] = Field(default_factory=list)
    config_guides: list[DiagnosticConfigGuide] = Field(default_factory=list)


class PersistenceStatusData(BaseModel):
    """NL2SQL 共有状態の永続化可用性。"""

    mode: Literal["memory", "oracle"]
    ready: bool
    durable: bool
    writable: bool
    snapshot_loaded: bool = Field(
        description="Deprecated: incremental backend は snapshot をロードしません。",
        deprecated=True,
    )
    reason_code: str | None = None
    checked_at: str
    # incremental: Oracle の entity 単位の repository。snapshot: memory（local / CI）の 1 つの状態。
    state_backend: Literal["incremental", "snapshot"] = "snapshot"
    circuit_state: Literal["closed", "open", "half_open"] = "closed"
    retry_after_seconds: int = 0


class CsvImportColumn(BaseModel):
    """CSV import column mapping."""

    source_name: str
    column_name: str
    data_type: str
    nullable: bool = True


class DbAdminImportTabularData(BaseModel):
    """Tabular import execution response."""

    table_name: str
    # 取込先の schema（current schema）と所有者付きの表名。
    owner: str = ""
    qualified_name: str = ""
    filename: str = ""
    sheet_name: str = ""
    mode: str = "create"
    columns: list[CsvImportColumn]
    row_count: int
    preview: bool = False
    executed: bool
    ddl: str
    insert_sql: str
    schema_refresh_job_id: str = ""
    schema_refresh_required: bool = False
    schema_refresh_reason_code: str = ""
    warnings: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, str | None]] = Field(default_factory=list)
    timing: TimingEnvelope
