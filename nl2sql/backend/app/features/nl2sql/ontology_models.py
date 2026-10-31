"""NL2SQL Ontology の API / 永続化境界で共有する契約。

このモジュールは既存 NL2SQL API から独立させ、段階的な移行中でも旧 payload を
壊さない。Ontology は実データ行を物化せず、業務概念、物理 mapping、問い合わせの
解釈と SQL の意味を版管理された graph として表現する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pr_backend_core.observability.model_usage import RunModelUsage, parse_run_usage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ontology_definitions import BusinessDefinition, ConceptCoverage, DefinitionPhase


def utc_now() -> datetime:
    """UTC の timezone-aware timestamp を返す。"""

    return datetime.now(UTC)


class OntologyContract(BaseModel):
    """不明な mutation field を黙って受理しない共通契約。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class OntologyRevisionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class OntologyReasoningStatus(StrEnum):
    NOT_STARTED = "not_started"
    MATERIALIZING = "materializing"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"


class OntologyReviewStatus(StrEnum):
    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    ORPHANED = "orphaned"


class OntologySourceKind(StrEnum):
    INTROSPECTED = "introspected"
    MANUAL = "manual"
    INFERRED = "inferred"
    MIGRATED = "migrated"
    QUERY_SESSION = "query_session"


class OntologyNodeKind(StrEnum):
    SCHEMA = "schema"
    TABLE = "table"
    VIEW = "view"
    COLUMN = "column"
    OBJECT_TYPE = "object_type"
    INTERFACE = "interface"
    FUNCTION = "function"
    ACTION_TYPE = "action_type"
    SHARED_PROPERTY = "shared_property"
    VALUE_TYPE = "value_type"
    ENUMERATION = "enumeration"
    OBJECT_SET = "object_set"
    BUSINESS_ENTITY = "business_entity"
    BUSINESS_EVENT = "business_event"
    PROPERTY = "property"
    METRIC = "metric"
    BUSINESS_TERM = "business_term"
    BUSINESS_RULE = "business_rule"
    ENUM_VALUE = "enum_value"
    QUESTION_INTENT = "question_intent"
    QUERY_PLAN = "query_plan"
    SQL_ARTIFACT = "sql_artifact"
    VALIDATION_FINDING = "validation_finding"
    EXECUTION_PREVIEW = "execution_preview"


class OntologyEdgeKind(StrEnum):
    CONTAINS = "contains"
    FOREIGN_KEY = "foreign_key"
    LINK_TYPE = "link_type"
    BUSINESS_RELATIONSHIP = "business_relationship"
    MAPS_TO = "maps_to"
    LINEAGE = "lineage"
    USES = "uses"
    JOINS = "joins"
    FILTERS = "filters"
    GROUPS_BY = "groups_by"
    SORTS_BY = "sorts_by"
    IS_A = "is_a"
    DOMAIN = "domain"
    RANGE = "range"
    INSTANCE_OF = "instance_of"
    HAS_VALUE = "has_value"
    GOVERNS = "governs"


class RelationshipDirection(StrEnum):
    DIRECTED = "directed"
    BIDIRECTIONAL = "bidirectional"


class RelationshipCardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"
    UNKNOWN = "unknown"


class JoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    CROSS = "cross"
    SEMI = "semi"
    ANTI = "anti"


class OntologyRevision(OntologyContract):
    """Ontology の不変 revision header。空の所有者は旧共有版。"""

    profile_id: str = ""
    format_version: int = 1

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: OntologyRevisionStatus = OntologyRevisionStatus.DRAFT
    schema_fingerprint: str = ""
    etag: str = ""
    parent_revision_id: str | None = None
    note: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    published_at: datetime | None = None
    reasoning_status: OntologyReasoningStatus = OntologyReasoningStatus.NOT_STARTED
    rdf_graph_name: str = ""
    inferred_graph_name: str = ""
    shacl_report_artifact_id: str = ""
    renderer_version: str = ""
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    publish_job_id: str = ""


class OntologyEvidenceLocatorKind(StrEnum):
    PAGE = "page"
    PARAGRAPH = "paragraph"
    SHEET_ROW = "sheet_row"
    LINE = "line"
    QA_ROW = "qa_row"
    SCHEMA_OBJECT = "schema_object"


class OntologyEvidence(OntologyContract):
    source_document_id: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    locator_kind: OntologyEvidenceLocatorKind
    locator: str = Field(min_length=1)
    excerpt_hash: str = Field(min_length=64, max_length=64)
    excerpt_ja: str = Field(default="", max_length=500)


class OntologyProvenance(OntologyContract):
    source_kind: OntologySourceKind
    source_id: str = ""
    source_detail: str = ""
    inferred_by: str = ""
    observed_at: datetime = Field(default_factory=utc_now)
    evidence: list[OntologyEvidence] = Field(default_factory=list)


class BusinessRuleKind(StrEnum):
    CONSTRAINT = "constraint"
    CALCULATION = "calculation"
    CLASSIFICATION = "classification"
    VALIDATION = "validation"


class BusinessRuleSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    VIOLATION = "violation"


class BusinessRuleExecutionMode(StrEnum):
    SHACL = "shacl"
    SQL_DEFINITION = "sql_definition"
    DOCUMENTATION = "documentation"


class BusinessRuleExpression(OntologyContract):
    """任意コードを受け付けない、業務制約用の固定演算子 AST。"""

    operator: Literal[
        "all",
        "any",
        "not",
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "is_null",
        "not_null",
    ]
    property_node_id: str = ""
    value: Any = None
    values: list[Any] = Field(default_factory=list)
    children: list[BusinessRuleExpression] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> BusinessRuleExpression:
        if self.operator in {"all", "any"} and not self.children:
            raise ValueError("all/any rule expression requires children.")
        if self.operator == "not" and len(self.children) != 1:
            raise ValueError("not rule expression requires exactly one child.")
        if self.operator not in {"all", "any", "not"} and not self.property_node_id:
            raise ValueError("comparison rule expression requires property_node_id.")
        if self.operator in {"in", "not_in"} and not self.values:
            raise ValueError("in/not_in rule expression requires values.")
        return self


class BusinessRuleDefinition(OntologyContract):
    rule_kind: BusinessRuleKind
    statement_ja: str = Field(min_length=1)
    applies_to_node_ids: list[str] = Field(min_length=1)
    expression: BusinessRuleExpression | None = None
    severity: BusinessRuleSeverity = BusinessRuleSeverity.VIOLATION
    execution_mode: BusinessRuleExecutionMode = BusinessRuleExecutionMode.DOCUMENTATION

    @model_validator(mode="after")
    def validate_execution_mode(self) -> BusinessRuleDefinition:
        if self.execution_mode == BusinessRuleExecutionMode.SHACL and self.expression is None:
            raise ValueError("SHACL business rule requires expression.")
        if self.execution_mode == BusinessRuleExecutionMode.SHACL and self.rule_kind not in {
            BusinessRuleKind.CONSTRAINT,
            BusinessRuleKind.VALIDATION,
        }:
            raise ValueError("SHACL execution is limited to constraint/validation rules.")
        if (
            self.execution_mode == BusinessRuleExecutionMode.SQL_DEFINITION
            and self.rule_kind != BusinessRuleKind.CALCULATION
        ):
            raise ValueError("SQL definition execution is limited to calculation rules.")
        return self


class EnumValueDefinition(OntologyContract):
    code: str = Field(min_length=1)
    label_ja: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    physical_literal: Any = None
    data_type: Literal["string", "integer", "number", "boolean", "date", "datetime"] = "string"
    property_node_id: str = Field(min_length=1)


class PhysicalObjectRef(OntologyContract):
    """owner を含む Oracle object の安定参照。"""

    node_id: str = ""
    owner: str = ""
    object_name: str = Field(min_length=1)
    object_type: Literal["table", "view"] = "table"


class PhysicalColumnRef(OntologyContract):
    node_id: str = ""
    owner: str = ""
    object_name: str = Field(min_length=1)
    column_name: str = Field(min_length=1)
    ordinal: int | None = Field(default=None, ge=1)


class PhysicalMapping(OntologyContract):
    object_ref: PhysicalObjectRef
    column_refs: list[PhysicalColumnRef] = Field(default_factory=list)
    expression_sql: str = ""
    lineage_source_ids: list[str] = Field(default_factory=list)


class JoinCondition(OntologyContract):
    left: PhysicalColumnRef
    right: PhysicalColumnRef
    operator: Literal["=", "<", ">", "<=", ">=", "!="] = "="
    ordinal: int = Field(default=1, ge=1)


class OntologyNode(OntologyContract):
    """物理 node と業務 node の共通表現。"""

    id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    kind: OntologyNodeKind
    technical_name: str = ""
    business_name_ja: str = Field(min_length=1)
    description_ja: str = ""
    aliases: list[str] = Field(default_factory=list)
    physical_mappings: list[PhysicalMapping] = Field(default_factory=list)
    provenance: OntologyProvenance
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    review_status: OntologyReviewStatus = OntologyReviewStatus.PROPOSED
    embedding_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    business_rule_definition: BusinessRuleDefinition | None = None
    enum_value_definition: EnumValueDefinition | None = None

    @model_validator(mode="after")
    def validate_typed_definition(self) -> OntologyNode:
        if self.kind == OntologyNodeKind.BUSINESS_RULE and self.business_rule_definition is None:
            raise ValueError("business_rule node requires business_rule_definition.")
        if (
            self.kind != OntologyNodeKind.BUSINESS_RULE
            and self.business_rule_definition is not None
        ):
            raise ValueError("business_rule_definition is only valid for business_rule nodes.")
        if self.kind == OntologyNodeKind.ENUM_VALUE and self.enum_value_definition is None:
            raise ValueError("enum_value node requires enum_value_definition.")
        if self.kind != OntologyNodeKind.ENUM_VALUE and self.enum_value_definition is not None:
            raise ValueError("enum_value_definition is only valid for enum_value nodes.")
        return self


class OntologyEdge(OntologyContract):
    """複合 join と cardinality を失わない Ontology relationship。"""

    id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    kind: OntologyEdgeKind
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    relationship_name_ja: str = Field(min_length=1)
    description_ja: str = ""
    direction: RelationshipDirection = RelationshipDirection.DIRECTED
    cardinality: RelationshipCardinality = RelationshipCardinality.UNKNOWN
    join_conditions: list[JoinCondition] = Field(default_factory=list)
    allowed_join_types: list[JoinType] = Field(default_factory=lambda: [JoinType.INNER])
    provenance: OntologyProvenance
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    review_status: OntologyReviewStatus = OntologyReviewStatus.PROPOSED
    metadata: dict[str, Any] = Field(default_factory=dict)


class ColumnQueryPolicy(OntologyContract):
    queryable: bool = True
    filterable: bool = True
    groupable: bool = True
    aggregatable: bool = False
    masked: bool = False
    required_filter: bool = False
    note_ja: str = ""


class ProfileOntologyView(OntologyContract):
    """共有 Ontology を profile の利用範囲へ射影した view。"""

    id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    view_version: int = Field(default=1, ge=1)
    status: OntologyRevisionStatus = OntologyRevisionStatus.PUBLISHED
    etag: str = ""
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    physical_objects: list[PhysicalObjectRef] = Field(default_factory=list)
    table_usages_ja: dict[str, str] = Field(default_factory=dict)
    column_policies: dict[str, ColumnQueryPolicy] = Field(default_factory=dict)
    allowed_path_ids: list[str] = Field(default_factory=list)
    draft_node_overrides: list[dict[str, Any]] = Field(default_factory=list)
    draft_edge_overrides: list[dict[str, Any]] = Field(default_factory=list)
    draft_schema_fingerprint: str = ""
    draft_physical_scope: dict[str, list[str]] = Field(default_factory=dict)
    archived: bool = False
    updated_at: datetime = Field(default_factory=utc_now)
    published_at: datetime | None = None
    activation_scenarios_ja: list[str] = Field(default_factory=list)
    activation_keywords: list[str] = Field(default_factory=list)
    scenario_version: int = Field(default=1, ge=1)
    # Profile mutation では view を更新しない。Ontology page が永続化した時点の
    # Profile version/scope を保持し、read-only preview で stale を判定する。
    source_profile_etag: str = ""
    source_profile_scope_fingerprint: str = ""


class MetricAggregation(StrEnum):
    NONE = "none"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class MetricAdditivity(StrEnum):
    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    NON_ADDITIVE = "non_additive"
    UNKNOWN = "unknown"


class MetricDefinition(OntologyContract):
    """公開オントロジーで確認済みとして扱う正式指標定義。"""

    id: str = Field(min_length=1)
    metric_node_id: str = Field(min_length=1)
    expression_sql: str = Field(min_length=1)
    filter_sql: str = ""
    aggregation: MetricAggregation = MetricAggregation.NONE
    base_column_node_ids: list[str] = Field(default_factory=list)
    grain_node_ids: list[str] = Field(default_factory=list)
    distinct_key_node_ids: list[str] = Field(default_factory=list)
    unit: str = ""
    currency: str = ""
    additivity: MetricAdditivity = MetricAdditivity.UNKNOWN
    null_policy_ja: str = ""
    description_ja: str = ""


class IntentEntity(OntologyContract):
    id: str = Field(min_length=1)
    ontology_node_id: str = ""
    name_ja: str = Field(min_length=1)
    role: Literal["subject", "related", "event"] = "subject"
    physical_object_ids: list[str] = Field(default_factory=list)


class IntentMetric(OntologyContract):
    id: str = Field(min_length=1)
    ontology_node_id: str = ""
    name_ja: str = Field(min_length=1)
    aggregation: str = ""
    formula_description_ja: str = ""
    metric_definition_id: str = ""
    expression_sql: str = ""
    grain_node_ids: list[str] = Field(default_factory=list)


class IntentDimension(OntologyContract):
    id: str = Field(min_length=1)
    ontology_node_id: str = ""
    name_ja: str = Field(min_length=1)
    granularity: str = ""


class IntentFilter(OntologyContract):
    id: str = Field(min_length=1)
    property_node_id: str = ""
    label_ja: str = Field(min_length=1)
    operator: str = "="
    value: Any = None
    value_type: str = "string"
    required: bool = False


class IntentTimeRange(OntologyContract):
    property_node_id: str = ""
    label_ja: str = "期間"
    start: str | None = None
    end: str | None = None
    start_inclusive: bool = True
    end_inclusive: bool = True
    relative_expression: str = ""
    timezone: str = "Asia/Tokyo"


class IntentSort(OntologyContract):
    target_id: str = Field(min_length=1)
    direction: Literal["asc", "desc"] = "asc"


class IntentRelationshipPath(OntologyContract):
    id: str = Field(min_length=1)
    name_ja: str = Field(min_length=1)
    edge_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    approved: bool = False
    explanation_ja: str = ""


class IntentAmbiguity(OntologyContract):
    id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message_ja: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    resolution: str | None = None
    blocking: bool = True
    resolved: bool = False


class ClarificationMode(StrEnum):
    """QuerySession の質問確認方式。既存 client は review_only を維持する。"""

    REVIEW_ONLY = "review_only"
    GUIDED = "guided"


class RequirementSection(StrEnum):
    """回答前の確認の「SQL の要件の下書き」の欄（#1439）。

    GROUP BY・ORDER BY・件数の上限は SELECT と別の独立した欄にする。WITH / サブクエリと
    相対的な期間も独立した欄。LLM が SQL の生成に要ると判断した、ほかの項目は OTHER。
    """

    SELECT = "select"
    FROM = "from"
    JOIN = "join"
    WHERE = "where"
    PERIOD = "period"
    GROUP_BY = "group_by"
    ORDER_BY = "order_by"
    LIMIT = "limit"
    SUBQUERY = "subquery"
    OTHER = "other"


class RequirementStatus(StrEnum):
    """項目の状態。確定 / AI の推測 / 不足 / あいまい。"""

    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class RequirementSource(StrEnum):
    """候補・推測の根拠。表のデータ（代表値・件数）は根拠にしない。"""

    USER = "user"
    QUESTION = "question"
    SCHEMA = "schema"
    PROFILE = "profile"
    GLOSSARY = "glossary"
    ONTOLOGY = "ontology"
    CALENDAR = "calendar"


class RequirementAnswerKind(StrEnum):
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    FREE_TEXT = "free_text"


class RequirementReferenceBasis(StrEnum):
    """相対的な期間の基準日。データを読まないため、実行日を既定の候補にする。"""

    EXECUTION_DATE = "execution_date"
    LATEST_DATA_DATE = "latest_data_date"


class RequirementPeriod(OntologyContract):
    """相対的な期間の表現（「今年度」「先月」など）を、具体的な日付の範囲にしたもの。"""

    expression_ja: str = ""
    relative_kind: str = ""
    count: int | None = Field(default=None, ge=1, le=120)
    fiscal_year_start_month: int | None = Field(default=None, ge=1, le=12)
    fiscal_year_start_source: str = ""
    reference_basis: RequirementReferenceBasis = RequirementReferenceBasis.EXECUTION_DATE
    reference_date: str = ""
    # 物理名（`TABLE.COLUMN`）。
    date_column: str = ""
    # 両端を含む日付（`YYYY-MM-DD`）。基準日がデータの最新日のときは空（SQL で求める）。
    start_date: str = ""
    end_date: str = ""
    range_ja: str = ""
    sql_condition: str = ""


class RequirementCandidate(OntologyContract):
    """質問に答えるときの候補。Schema・業務プロファイル・用語集・オントロジーの根拠があるものだけ。"""

    id: str = Field(min_length=1)
    label_ja: str = Field(min_length=1)
    description_ja: str = ""
    evidence_ja: str = ""
    source: RequirementSource = RequirementSource.SCHEMA
    # 表は `OWNER.TABLE`、列は `OWNER.TABLE.COLUMN` の物理名（Schema に実在するもの）。
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    sql_hint: str = ""
    period: RequirementPeriod | None = None
    # 画面に出す物理名（論理名を括弧で添える）。プログラムが Schema から作る。
    refs_ja: str = ""


class RequirementItem(OntologyContract):
    """SQL の要件の 1 項目。"""

    id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    section: RequirementSection
    label_ja: str = Field(min_length=1)
    value_ja: str = ""
    status: RequirementStatus
    reason_ja: str = ""
    question_ja: str = ""
    answer_kind: RequirementAnswerKind = RequirementAnswerKind.FREE_TEXT
    candidates: list[RequirementCandidate] = Field(default_factory=list)
    source: RequirementSource = RequirementSource.QUESTION
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    sql_hint: str = ""
    period: RequirementPeriod | None = None
    refs_ja: str = ""
    # 利用者が答えた・直した項目。以後の LLM の検査でも変えない。
    answered: bool = False
    answer_ja: str = ""


class RequirementConversationTurn(OntologyContract):
    """チャットの回答前の確認に渡す、同じ会話の前のターン（質問と生成した SQL。#1442）。

    結果の行・個人情報・権限設定は持たない。LLM には参照資料として渡す。
    """

    question: str = Field(default="", max_length=10000)
    sql: str = Field(default="", max_length=16000)
    status: str = ""


class SqlRequirementDraft(OntologyContract):
    """LLM が作り、プログラムが検査した「SQL の要件の下書き」。"""

    version: int = Field(default=1, ge=1)
    question_original: str = Field(min_length=1)
    clarified_question_ja: str = ""
    items: list[RequirementItem] = Field(default_factory=list)
    unanswerable_reason_ja: str = ""
    warnings_ja: list[str] = Field(default_factory=list)
    reference_date: str = ""
    model: str = ""
    # 確定した要件から、プログラムが決定的に組み立てた SQL の骨組み（実行しない。生成の SQL
    # ではない）。
    sql_skeleton: str = ""


class RequirementAnswerRecord(OntologyContract):
    item_id: str = Field(min_length=1)
    item_key: str = ""
    selected_candidate_ids: list[str] = Field(default_factory=list)
    free_text: str = Field(default="", max_length=2000)
    draft_version: int = Field(default=1, ge=1)
    answered_at: datetime = Field(default_factory=utc_now)


class RequirementPhase(StrEnum):
    NEEDS_ANSWER = "needs_answer"
    READY = "ready"
    UNANSWERABLE = "unanswerable"


class RequirementState(OntologyContract):
    """画面に返す確認の状態（保存しない。session と下書きからプログラムで作る）。"""

    status: RequirementPhase
    current_item: RequirementItem | None = None
    unresolved_items: list[RequirementItem] = Field(default_factory=list)
    answered_count: int = Field(default=0, ge=0)
    review_round: int = Field(default=0, ge=0)
    max_review_rounds: int = Field(default=1, ge=1)
    review_limit_reached: bool = False
    # 最低の基準: SELECT（何を出すか）と FROM（どの表から）が明確。
    select_from_clear: bool = False
    minimum_missing_ja: list[str] = Field(default_factory=list)
    can_apply: bool = False
    summary_ja: str = ""
    sql_skeleton: str = ""
    message_ja: str = ""


class QuestionIntentGraph(OntologyContract):
    """自然言語質問を SQL より前に確認する業務意味 graph。"""

    version: int = Field(default=1, ge=1)
    question_original: str = Field(min_length=1)
    question_effective: str = Field(min_length=1)
    profile_view_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    entities: list[IntentEntity] = Field(default_factory=list)
    metrics: list[IntentMetric] = Field(default_factory=list)
    dimensions: list[IntentDimension] = Field(default_factory=list)
    filters: list[IntentFilter] = Field(default_factory=list)
    time_range: IntentTimeRange | None = None
    granularity: str = ""
    sorts: list[IntentSort] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=5000)
    candidate_paths: list[IntentRelationshipPath] = Field(default_factory=list)
    selected_path_id: str | None = None
    ambiguities: list[IntentAmbiguity] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)


class SqlCteDefinition(OntologyContract):
    id: str
    name: str
    scope_id: str
    query_sql: str
    depends_on: list[str] = Field(default_factory=list)


class SqlTableReference(OntologyContract):
    id: str
    scope_id: str
    catalog: str = ""
    owner: str = ""
    name: str
    alias: str = ""
    qualified_name: str
    is_cte: bool = False
    source_sql: str = ""
    # SQL で owner / 表名が二重引用符で囲まれていたか。Oracle は引用なしを大文字、引用ありを
    # 書かれたとおりに解釈するため、許可表の照合で `"Mixed_Case"` と `MIXED_CASE` を
    # 区別する（#561）。
    owner_quoted: bool = False
    name_quoted: bool = False
    # 表示用の所有者付き名前。SQL に owner が無い表は実行時の current schema で補う
    # （owner / qualified_name は SQL に書かれたままの値を保持する）。
    resolved_owner: str = ""
    resolved_qualified_name: str = ""


class SqlColumnReference(OntologyContract):
    id: str
    scope_id: str
    catalog: str = ""
    owner: str = ""
    table: str = ""
    name: str
    clause: str
    expression_sql: str
    # SQL で owner / 表（または別名）/ 列名が二重引用符で囲まれていたか。列単位の許可チェックで
    # `"Amount"` と `AMOUNT` を区別する（#563）。
    owner_quoted: bool = False
    table_quoted: bool = False
    name_quoted: bool = False


class SqlJoinReference(OntologyContract):
    id: str
    scope_id: str
    left_source: str
    right_source: str
    join_type: str
    condition_sql: str = ""
    using_columns: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)
    is_cartesian: bool = False


class SqlProjection(OntologyContract):
    id: str
    scope_id: str
    output_name: str
    expression_sql: str
    referenced_columns: list[str] = Field(default_factory=list)
    contains_aggregate: bool = False
    contains_window: bool = False
    contains_wildcard: bool = False


class SqlPredicate(OntologyContract):
    id: str
    scope_id: str
    clause: Literal["where", "having", "qualify"]
    expression_sql: str
    referenced_columns: list[str] = Field(default_factory=list)


class SqlAggregate(OntologyContract):
    id: str
    scope_id: str
    function_name: str
    expression_sql: str
    referenced_columns: list[str] = Field(default_factory=list)


class SqlGroupExpression(OntologyContract):
    id: str
    scope_id: str
    expression_sql: str
    referenced_columns: list[str] = Field(default_factory=list)


class SqlOrderExpression(OntologyContract):
    id: str
    scope_id: str
    expression_sql: str
    direction: Literal["asc", "desc"] = "asc"
    referenced_columns: list[str] = Field(default_factory=list)


class SqlWindowExpression(OntologyContract):
    id: str
    scope_id: str
    expression_sql: str
    partition_by: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)


class SqlSetOperation(OntologyContract):
    id: str
    scope_id: str
    operator: Literal["union", "union_all", "intersect", "except"]
    expression_sql: str


class SqlSubqueryReference(OntologyContract):
    id: str
    scope_id: str
    alias: str = ""
    query_sql: str


class SqlLineage(OntologyContract):
    id: str
    scope_id: str
    output_name: str
    source_columns: list[str] = Field(default_factory=list)


class SqlSemanticGraph(OntologyContract):
    """sqlglot AST から決定論的に生成する SQL の意味 graph。"""

    version: int = Field(default=1, ge=1)
    sql_hash: str
    dialect: Literal["oracle"] = "oracle"
    statement_type: str
    raw_sql: str
    parse_complete: bool = True
    ctes: list[SqlCteDefinition] = Field(default_factory=list)
    tables: list[SqlTableReference] = Field(default_factory=list)
    columns: list[SqlColumnReference] = Field(default_factory=list)
    joins: list[SqlJoinReference] = Field(default_factory=list)
    projections: list[SqlProjection] = Field(default_factory=list)
    filters: list[SqlPredicate] = Field(default_factory=list)
    aggregates: list[SqlAggregate] = Field(default_factory=list)
    groups: list[SqlGroupExpression] = Field(default_factory=list)
    having: list[SqlPredicate] = Field(default_factory=list)
    orders: list[SqlOrderExpression] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=0)
    windows: list[SqlWindowExpression] = Field(default_factory=list)
    set_operations: list[SqlSetOperation] = Field(default_factory=list)
    subqueries: list[SqlSubqueryReference] = Field(default_factory=list)
    lineage: list[SqlLineage] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)


class ValidationSeverity(StrEnum):
    PASS = "pass"  # nosec B105
    WARNING = "warning"
    BLOCKER = "blocker"


class OntologyValidationFinding(OntologyContract):
    id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    severity: ValidationSeverity
    message_ja: str = Field(min_length=1)
    intent_element_ids: list[str] = Field(default_factory=list)
    sql_element_ids: list[str] = Field(default_factory=list)
    ontology_node_ids: list[str] = Field(default_factory=list)
    suggested_action_ja: str = ""


class OntologyValidationReport(OntologyContract):
    """Question ↔ SQL ↔ Profile view の三方 validation。"""

    id: str = Field(min_length=1)
    intent_version: int = Field(ge=1)
    sql_hash: str
    ontology_revision_id: str
    is_valid: bool
    intent_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    findings: list[OntologyValidationFinding] = Field(default_factory=list)
    passed_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    blocker_count: int = Field(default=0, ge=0)
    validation_hash: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class SqlSemanticAnalysis(OntologyContract):
    graph: SqlSemanticGraph | None = None
    validation: OntologyValidationReport
    parser: Literal["sqlglot"] = "sqlglot"


class GraphPatchOperation(OntologyContract):
    op: Literal["add", "replace", "remove"]
    path: str = Field(pattern=r"^/")
    value: Any = None
    reason_ja: str = ""


class GraphPatch(OntologyContract):
    base_version: int = Field(ge=1)
    operations: list[GraphPatchOperation] = Field(min_length=1)
    summary_ja: str = ""


class OntologyProposalStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    # 新規 AI 構築の実行時に、前回のレビュー一覧を一掃するための終端状態。
    # レビュー一覧(list_proposals_by_profile)からは除外され UI には現れない。
    SUPERSEDED = "superseded"


class OntologyProposalKind(StrEnum):
    ALIAS = "alias"
    METRIC_DEFINITION = "metric_definition"
    RELATIONSHIP = "relationship"
    MAPPING = "mapping"
    PROFILE_POLICY = "profile_policy"
    QUERY_EXAMPLE = "query_example"


class OntologyProposalPayload(OntologyContract):
    kind: OntologyProposalKind = OntologyProposalKind.QUERY_EXAMPLE
    target_node_id: str = ""
    target_edge_id: str = ""
    values: dict[str, Any] = Field(default_factory=dict)


class OntologyProposal(OntologyContract):
    id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    base_revision_id: str = Field(min_length=1)
    title_ja: str = Field(min_length=1)
    description_ja: str = ""
    kind: OntologyProposalKind = OntologyProposalKind.QUERY_EXAMPLE
    proposal_payload: OntologyProposalPayload = Field(default_factory=OntologyProposalPayload)
    patch: GraphPatch | None = None
    status: OntologyProposalStatus = OntologyProposalStatus.SUBMITTED
    created_at: datetime = Field(default_factory=utc_now)


# --- AI オントロジー構築(Enterprise AI 出力契約と job) ------------------------------------
#
# LLM 出力は必ずこれらの schema で検証し、profile view スコープ外の参照は proposal 化せず
# warnings に落とす。生成物は OntologyProposal(承認フロー)経由でのみ Ontology に入る。


class OntologyBuildJoinConditionCandidate(OntologyContract):
    """LLM が返す join 条件候補。列は OWNER.OBJECT.COLUMN 形式。"""

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    operator: Literal["=", "<", ">", "<=", ">=", "!="] = "="


class OntologyEntityNamingCandidate(OntologyContract):
    object_name: str = Field(min_length=1, description="OWNER.OBJECT または OBJECT")
    business_name_ja: str = Field(min_length=1)
    description_ja: str = ""
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class OntologyRelationshipCandidate(OntologyContract):
    source_object: str = Field(min_length=1)
    target_object: str = Field(min_length=1)
    relationship_name_ja: str = Field(min_length=1)
    cardinality: RelationshipCardinality = RelationshipCardinality.UNKNOWN
    join_conditions: list[OntologyBuildJoinConditionCandidate] = Field(default_factory=list)
    evidence_ja: str = ""
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class OntologyMetricCandidate(OntologyContract):
    metric_name_ja: str = Field(min_length=1)
    expression_sql: str = Field(min_length=1)
    aggregation: MetricAggregation = MetricAggregation.NONE
    base_columns: list[str] = Field(default_factory=list, description="OWNER.OBJECT.COLUMN")
    unit: str = ""
    description_ja: str = ""
    evidence_ja: str = ""
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class OntologySynonymCandidate(OntologyContract):
    target: str = Field(min_length=1, description="OWNER.OBJECT または OWNER.OBJECT.COLUMN")
    aliases: list[str] = Field(min_length=1)
    evidence_ja: str = ""


class OntologyBuildExtraction(OntologyContract):
    """AI オントロジー構築 1 ステップ分の LLM 出力契約。"""

    entities: list[OntologyEntityNamingCandidate] = Field(default_factory=list)
    relationships: list[OntologyRelationshipCandidate] = Field(default_factory=list)
    metrics: list[OntologyMetricCandidate] = Field(default_factory=list)
    synonyms: list[OntologySynonymCandidate] = Field(default_factory=list)
    definitions: list[BusinessDefinition] = Field(default_factory=list)
    coverage: list[ConceptCoverage] = Field(default_factory=list)
    warnings_ja: list[str] = Field(default_factory=list)


class OntologyConceptExtraction(OntologyContract):
    """新規生成の正本。旧形式の候補は OntologyBuildExtraction でのみ読み込む。"""

    definitions: list[BusinessDefinition] = Field(default_factory=list)
    coverage: list[ConceptCoverage] = Field(default_factory=list)
    warnings_ja: list[str] = Field(default_factory=list)


class QaPair(OntologyContract):
    """Q/A Excel の 1 行(質問と正解 SQL)。"""

    question: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    note_ja: str = ""


class OntologyBuildStepName(StrEnum):
    SOURCE_EXTRACTION = "source_extraction"
    SCHEMA_CONTEXT = "schema_context"
    SCHEMA_NAMING = "schema_naming"
    QA_EXTRACTION = "qa_extraction"
    TEXT_EXTRACTION = "text_extraction"
    PROPOSAL_REGISTRATION = "proposal_registration"


class OntologyBuildStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class OntologyBuildStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    # 一部の LLM 抽出 batch が失敗したが、成功分で Markdown 下書きを生成できた状態
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OntologySourceStatus(StrEnum):
    STORED = "stored"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    FAILED = "failed"


class OntologySourceRole(StrEnum):
    SOURCE = "source"
    QA = "qa"


class OntologySourceDocument(OntologyContract):
    id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    source_role: OntologySourceRole = OntologySourceRole.SOURCE
    media_type: str = "application/octet-stream"
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    storage_uri: str = Field(min_length=1)
    status: OntologySourceStatus = OntologySourceStatus.STORED
    extracted_chunk_count: int = Field(default=0, ge=0)
    warnings_ja: list[str] = Field(default_factory=list)
    error_message_ja: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OntologySourceProgress(OntologyContract):
    source_document_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    status: OntologySourceStatus = OntologySourceStatus.STORED
    extracted_chunk_count: int = Field(default=0, ge=0)
    warnings_ja: list[str] = Field(default_factory=list)
    error_message_ja: str = ""


class OntologyBuildStep(OntologyContract):
    name: OntologyBuildStepName
    status: OntologyBuildStepStatus = OntologyBuildStepStatus.PENDING
    detail_ja: str = ""
    # 機械可読の状態/失敗コード。frontend は日本語文言ではなくこのコードで分岐する。
    code: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


OntologyBuildPhase = Literal["freeze", "evidence", "concepts", "validation", "markdown", "save"]


class OntologyBuildEvent(OntologyContract):
    """アクティビティタイムライン 1 行(時刻付きの進捗イベント)。"""

    at: datetime = Field(default_factory=utc_now)
    message_ja: str = Field(min_length=1)
    # 機械可読イベントコード(例: MARKDOWN_DRAFT_UPDATED)。文言正規表現の代替。
    code: str = ""
    # イベントの帰属ステップ(frontend のステップ別タイムライン用)。
    step: OntologyBuildStepName | None = None
    # 入力別 step と独立した表示工程。旧イベントには存在しない。
    phase: OntologyBuildPhase | None = None


class OntologyBuildJob(OntologyContract):
    """永続化され、in-process または独立 worker が処理する AI 構築 job。"""

    id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    status: OntologyBuildStatus = OntologyBuildStatus.QUEUED
    steps: list[OntologyBuildStep] = Field(default_factory=list)
    events: list[OntologyBuildEvent] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    draft_revision_id: str = ""
    draft_etag: str = ""
    result_bundle_id: str = ""
    definition_phases: list[DefinitionPhase] = Field(default_factory=list)
    concept_coverage: list[ConceptCoverage] = Field(default_factory=list)
    profile_fingerprint: str = ""
    schema_context_fingerprint: str = ""
    source_document_ids: list[str] = Field(default_factory=list)
    sources: list[OntologySourceProgress] = Field(default_factory=list)
    markdown_output: str = ""
    warnings_ja: list[str] = Field(default_factory=list)
    error_message_ja: str = ""
    # 機械可読の失敗コード(例: SCHEMA_SCOPE_EMPTY / LLM_EXTRACTION_FAILED)。
    error_code: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class OntologyPublishStatus(StrEnum):
    QUEUED = "queued"
    MATERIALIZING = "materializing"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OntologyPublishJob(OntologyContract):
    id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    requested_etag: str = Field(min_length=1)
    profile_id: str = ""
    status: OntologyPublishStatus = OntologyPublishStatus.QUEUED
    rdf_graph_name: str = ""
    inferred_graph_name: str = ""
    shacl_conforms: bool | None = None
    shacl_report_artifact_id: str = ""
    warnings_ja: list[str] = Field(default_factory=list)
    error_code: str = ""
    error_message_ja: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ProfileRecommendationCandidateV2(OntologyContract):
    profile_id: str = Field(min_length=1)
    profile_name: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    matched_scenarios_ja: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    reasons_ja: list[str] = Field(default_factory=list)


class ProfileRecommendation(OntologyContract):
    id: str = Field(min_length=1)
    question_hash: str = Field(min_length=64, max_length=64)
    ontology_revision_id: str = Field(min_length=1)
    candidates: list[ProfileRecommendationCandidateV2] = Field(default_factory=list)
    selected_profile_id: str = ""
    selected_revision_id: str = ""
    confirmed_at: datetime | None = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)


class OntologyContextHit(OntologyContract):
    node: OntologyNode
    score: float = Field(ge=0.0, le=1.0)
    matched_terms: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    inference_source: str = "asserted"


class OntologyContextSearchResult(OntologyContract):
    profile_id: str = Field(min_length=1)
    profile_view_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    business_release_id: str = ""
    hits: list[OntologyContextHit] = Field(default_factory=list)
    nodes: list[OntologyNode] = Field(default_factory=list)
    edges: list[OntologyEdge] = Field(default_factory=list)
    mermaid: str = ""
    llm_markdown: str = ""
    owl_turtle: str = ""
    shacl_turtle: str = ""
    context_hash: str = Field(min_length=64, max_length=64)


class OntologySqlGenerationContext(OntologyContract):
    """確認済み intent から SQL 生成エンジンへ渡す決定論的な業務制約。"""

    session_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_view_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    business_release_id: str = ""
    intent_version: int = Field(ge=1)
    question_effective: str = Field(min_length=1)
    allowed_object_names: list[str] = Field(default_factory=list)
    allowed_column_names: dict[str, list[str]] = Field(default_factory=dict)
    entity_node_ids: list[str] = Field(default_factory=list)
    metric_node_ids: list[str] = Field(default_factory=list)
    dimension_node_ids: list[str] = Field(default_factory=list)
    filter_summaries_ja: list[str] = Field(default_factory=list)
    time_range_summary_ja: str = ""
    granularity: str = ""
    sort_summaries_ja: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=5000)
    selected_path_id: str = ""
    approved_join_edge_ids: list[str] = Field(default_factory=list)
    join_condition_summaries: list[str] = Field(default_factory=list)
    metric_definitions: list[MetricDefinition] = Field(default_factory=list)
    qa_sql_examples: list[QaPair] = Field(default_factory=list)
    qa_sql_patterns: list[dict[str, Any]] = Field(default_factory=list)
    warnings_ja: list[str] = Field(default_factory=list)
    llm_markdown: str = ""
    # LLM プロンプト用の erDiagram 表現。context_hash の計算対象には含めない
    # (永続化済み session・確認 binding との互換を保つ)。
    mermaid_er: str = ""
    context_hash: str = Field(min_length=1)


class QuerySessionStatus(StrEnum):
    INTERPRETING = "interpreting"
    AWAITING_INTENT_CONFIRMATION = "awaiting_intent_confirmation"
    GENERATING_SQL = "generating_sql"
    AWAITING_SQL_CONFIRMATION = "awaiting_sql_confirmation"
    EXECUTING = "executing"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class SqlArtifact(OntologyContract):
    id: str = Field(min_length=1)
    intent_version: int = Field(ge=1)
    ontology_revision_id: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    sql_hash: str = Field(min_length=1)
    generation_context_hash: str = Field(default="")
    semantic_graph: SqlSemanticGraph | None = None
    validation_report: OntologyValidationReport
    created_at: datetime = Field(default_factory=utc_now)


class SqlConfirmationBinding(OntologyContract):
    artifact_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    intent_version: int = Field(ge=1)
    sql_hash: str = Field(min_length=1)
    validation_hash: str = Field(min_length=1)
    generation_context_hash: str = Field(min_length=1)
    confirmed_at: datetime = Field(default_factory=utc_now)


class QueryExecutionRecord(OntologyContract):
    binding: SqlConfirmationBinding
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    row_count: int | None = Field(default=None, ge=0)
    result_ref: str = ""


class QuerySession(OntologyContract):
    """二段階確認を含む問い合わせ session の全 version。"""

    id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_view_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    business_release_id: str = ""
    status: QuerySessionStatus = QuerySessionStatus.INTERPRETING
    original_question: str = Field(min_length=1)
    current_intent_version: int = Field(default=1, ge=1)
    intents: list[QuestionIntentGraph] = Field(default_factory=list)
    sql_artifacts: list[SqlArtifact] = Field(default_factory=list)
    current_sql_artifact_id: str | None = None
    intent_confirmed_version: int | None = None
    sql_confirmation: SqlConfirmationBinding | None = None
    execution: QueryExecutionRecord | None = None
    clarification_mode: ClarificationMode = ClarificationMode.REVIEW_ONLY
    # 回答前の確認（guided）の SQL の要件の下書き・回答・LLM の検査の回数（#1439）。
    requirement: SqlRequirementDraft | None = None
    requirement_answers: list[RequirementAnswerRecord] = Field(default_factory=list)
    requirement_review_round: int = Field(default=0, ge=0)
    requirement_applied_at: datetime | None = None
    # 回答前の確認の LLM の呼び出しの利用量（下書き・再検査の要求ごとに足す）。反映した要件と一緒に
    # ジョブへ渡し、実行履歴の行でジョブの利用量と合算する（#3552）。この機能より前の session と
    # LLM を呼ばなかった session は None。
    requirement_usage: RunModelUsage | None = None
    # チャットの回答前の確認（#1442）。会話の続き（前のターンの job）と、要件の検査に渡した
    # 会話の履歴。チャットの session はチャットのジョブにだけ、SQL 生成の画面の session は
    # SQL 生成の画面にだけ使う。
    chat: bool = False
    chat_previous_job_id: str | None = None
    conversation_history: list[RequirementConversationTurn] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    actor_user_uuid: str = ""
    actor_is_system_admin: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error_code: str = ""
    error_message_ja: str = ""
    cancelled_at: datetime | None = None

    @field_validator("requirement_usage", mode="before")
    @classmethod
    def drop_unreadable_requirement_usage(cls, value: Any) -> RunModelUsage | None:
        """読めない利用量の記録は無いものとする（session そのものは読む。#3552）。"""
        if isinstance(value, RunModelUsage):
            return value
        return parse_run_usage(value)


class QuerySessionCreate(OntologyContract):
    question: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_view_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    business_release_id: str = ""
    intent: QuestionIntentGraph | None = None
    clarification_mode: ClarificationMode = ClarificationMode.REVIEW_ONLY
    actor_user_uuid: str = ""
    actor_is_system_admin: bool = False
    chat: bool = False
    chat_previous_job_id: str | None = None
    conversation_history: list[RequirementConversationTurn] = Field(default_factory=list)


class SqlConfirmationRequest(OntologyContract):
    artifact_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    intent_version: int = Field(ge=1)
    sql_hash: str = Field(min_length=1)
    validation_hash: str = Field(min_length=1)
    generation_context_hash: str = Field(min_length=1)
