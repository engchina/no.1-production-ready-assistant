"""Profile 所有の業務定義。物理 graph・表示文書から独立した v2 契約。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DefinitionKind = Literal[
    "object_type",
    "property",
    "link_type",
    "function",
    "action_type",
    "interface",
    "shared_property",
    "value_type",
    "metric",
    "business_rule",
    "enumeration",
    "business_event",
    "object_set",
]
DEFINITION_KINDS: tuple[DefinitionKind, ...] = (
    "object_type",
    "property",
    "link_type",
    "interface",
    "function",
    "action_type",
    "shared_property",
    "value_type",
    "enumeration",
    "metric",
    "business_rule",
    "business_event",
    "object_set",
)


class DefinitionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DefinitionSource(DefinitionContract):
    source_id: str
    locator: str
    kind: Literal["database", "document", "qa", "manual"]
    sha256: str
    text: str


class DefinitionPhase(DefinitionContract):
    name: Literal[
        "freeze",
        "evidence",
        "concepts",
        "objects",
        "shared",
        "capabilities",
        "validation",
        "markdown",
        "save",
    ]
    status: Literal["pending", "running", "succeeded", "failed", "skipped"] = "pending"
    detail_ja: str = ""
    started_at: str | None = None
    finished_at: str | None = None


class DefinitionEvidence(DefinitionContract):
    source_id: str
    locator: str
    excerpt_ja: str = ""
    source_sha256: str = ""
    verified: bool = False
    source_kind: Literal["database", "document", "qa", "manual", "inference"] = "inference"
    assertion: str = ""


class DefinitionMapping(DefinitionContract):
    owner: str
    object_name: str
    column_name: str = ""
    expression_sql: str = ""

    @field_validator("column_name")
    @classmethod
    def whole_object_column(cls, value: str) -> str:
        # AI が表・ビュー全体を `*` と書いても、列なしの対応として扱う（#1720）。
        return "" if value.strip() == "*" else value

    @model_validator(mode="after")
    def drop_wildcard_expression(self) -> DefinitionMapping:
        # `OWNER.OBJECT.*` の式は列なしの対応と同じ意味（式として検証しない）。
        if not self.column_name and self.expression_sql.strip().endswith("*"):
            self.expression_sql = ""
        return self


class TypedParameter(DefinitionContract):
    api_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    name_ja: str
    data_type: Literal["string", "integer", "number", "boolean", "date", "datetime", "object"]
    required: bool = True


class DefinitionBase(DefinitionContract):
    api_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.]*$")
    name_ja: str = Field(min_length=1)
    description_ja: str = ""
    id: str = ""
    aliases: list[str] = Field(default_factory=list)
    evidence: list[DefinitionEvidence] = Field(default_factory=list)
    missing_information_ja: list[str] = Field(default_factory=list)
    mappings: list[DefinitionMapping] = Field(default_factory=list)
    review_status: Literal["unreviewed", "reviewed"] = "unreviewed"
    origin: Literal["ai", "manual", "legacy"] = "ai"


class PropertyBinding(DefinitionContract):
    interface_property: str
    property: str


class ActionAssignment(DefinitionContract):
    property: str
    parameter: str


class EnumMember(DefinitionContract):
    code: str
    label_ja: str


class InterfaceImplementation(DefinitionContract):
    interface: str
    property_mapping: list[PropertyBinding] = Field(default_factory=list)


class ObjectTypeDefinition(DefinitionBase):
    kind: Literal["object_type"] = "object_type"
    primary_key: list[str] = Field(default_factory=list)
    title_property: str = Field(
        default="",
        description="任意の表示用プロパティ参照。同じオブジェクトに属する property の api_name。"
        "根拠がなければ空文字。JSON のキー出力は必須でも、値の設定は必須ではない。"
        "未設定を資料不足・必須項目不足として報告しない。",
    )
    grain_ja: str = ""
    properties: list[str] = Field(default_factory=list)
    implements: list[InterfaceImplementation] = Field(default_factory=list)
    lifecycle_ja: str = ""


class PropertyDefinition(DefinitionBase):
    kind: Literal["property", "shared_property", "value_type"] = "property"
    object_type: str = ""
    # 業務の型（Base Type: string / integer / number / boolean / date / datetime）と、精度・桁を
    # 含む物理の型（Physical Type: `NUMBER(12,2)` など）を分けて持つ（Foundry の Property。#1729）。
    data_type: str = ""
    physical_type: str = ""
    required: bool = False
    unit: str = ""
    currency: str = ""
    timezone: str = ""
    allowed_values: list[str] = Field(default_factory=list)
    shared_property: str = ""
    value_type: str = ""
    writable: bool = False


class LinkTypeDefinition(DefinitionBase):
    kind: Literal["link_type"] = "link_type"
    source: str
    target: str
    inverse_name_ja: str = ""
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many", "unknown"] = (
        "unknown"
    )
    optional: bool = True
    join_expression_sql: str = ""
    intermediary_object: str = ""
    valid_time_ja: str = ""


class FunctionDefinition(DefinitionBase):
    kind: Literal["function"] = "function"
    parameters: list[TypedParameter] = Field(default_factory=list)
    return_type: str = ""
    dependencies: list[str] = Field(default_factory=list)
    expression_sql: str = ""
    implementation_key: str = ""
    null_policy_ja: str = ""
    error_policy_ja: str = ""


class ActionTypeDefinition(DefinitionBase):
    kind: Literal["action_type"] = "action_type"
    object_type: str
    parameters: list[TypedParameter] = Field(default_factory=list)
    assignments: list[ActionAssignment] = Field(default_factory=list)
    preconditions_ja: list[str] = Field(default_factory=list)
    implementation_key: str = ""
    affected_properties: list[str] = Field(default_factory=list)
    permission_requirement_ja: str = ""
    failure_policy_ja: str = ""


class InterfaceDefinition(DefinitionBase):
    kind: Literal["interface"] = "interface"
    properties: list[TypedParameter] = Field(default_factory=list)
    extends: list[str] = Field(default_factory=list)
    required_links: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)


class MetricDefinitionV2(DefinitionBase):
    kind: Literal["metric"] = "metric"
    expression_sql: str = ""
    filter_sql: str = ""
    aggregation: str = ""
    grain: list[str] = Field(default_factory=list)
    distinct_keys: list[str] = Field(default_factory=list)
    time_property: str = ""
    time_policy_ja: str = ""
    unit: str = ""
    currency: str = ""
    additivity: Literal["additive", "semi_additive", "non_additive", "unknown"] = "unknown"
    null_policy_ja: str = ""
    dependencies: list[str] = Field(default_factory=list)


class BusinessRuleDefinitionV2(DefinitionBase):
    kind: Literal["business_rule"] = "business_rule"
    applies_to: list[str] = Field(default_factory=list)
    predicate_sql: str = ""
    severity: Literal["warning", "error"] = "error"


class EnumerationDefinition(DefinitionBase):
    kind: Literal["enumeration"] = "enumeration"
    property: str
    values: list[EnumMember] = Field(default_factory=list)


class BusinessEventDefinition(DefinitionBase):
    kind: Literal["business_event"] = "business_event"
    object_type: str
    timestamp_property: str = ""
    properties: list[str] = Field(default_factory=list)


class ObjectSetDefinition(DefinitionBase):
    kind: Literal["object_set"] = "object_set"
    object_type: str
    filter_sql: str = ""
    parameters: list[TypedParameter] = Field(default_factory=list)


BusinessDefinition = Annotated[
    ObjectTypeDefinition
    | PropertyDefinition
    | LinkTypeDefinition
    | FunctionDefinition
    | ActionTypeDefinition
    | InterfaceDefinition
    | MetricDefinitionV2
    | BusinessRuleDefinitionV2
    | EnumerationDefinition
    | BusinessEventDefinition
    | ObjectSetDefinition,
    Field(discriminator="kind"),
]


class ConceptCoverage(DefinitionContract):
    kind: DefinitionKind
    status: Literal["generated", "insufficient_evidence", "not_applicable", "failed"]
    count: int = Field(default=0, ge=0)
    reason_ja: str = ""


class DefinitionFinding(DefinitionContract):
    code: str
    # info は graph を作れるが、補えば SQL の精度が上がる項目（公開を止めず、控えめに出す。#1720）。
    severity: Literal["info", "warning", "error"]
    definition_id: str = ""
    field: str = ""
    message_ja: str


class DefinitionConflict(DefinitionContract):
    definition_id: str
    current: BusinessDefinition
    proposed: BusinessDefinition


class ProfileOntologyBundle(DefinitionContract):
    format_version: Literal[2] = 2
    id: str
    profile_id: str
    job_id: str
    schema_fingerprint: str
    profile_fingerprint: str
    source_revision_id: str = ""
    parent_id: str = ""
    etag: str = ""
    status: Literal["draft", "published"] = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    definitions: list[BusinessDefinition] = Field(default_factory=list)
    coverage: list[ConceptCoverage] = Field(default_factory=list)
    findings: list[DefinitionFinding] = Field(default_factory=list)
    conflicts: list[DefinitionConflict] = Field(default_factory=list)
    notes_ja: str = ""
    sources: list[DefinitionSource] = Field(default_factory=list)
    requires_revalidation: bool = False
    schema_context_fingerprint: str = ""
    validation_report: dict[str, Any] = Field(default_factory=dict)
    review_records: list[dict[str, str]] = Field(default_factory=list)


class DefinitionEditRequest(DefinitionContract):
    definitions: list[BusinessDefinition]


class DefinitionNotesRequest(DefinitionContract):
    notes_ja: str = Field(max_length=50000)


class DefinitionReviewRequest(DefinitionContract):
    definition_ids: list[str]
    confirmed: Literal[True]


class DefinitionPublishRequest(DefinitionContract):
    expected_head: str
    confirmed: Literal[True]


class DefinitionAnalyzeRequest(DefinitionContract):
    instruction_ja: str = Field(min_length=1, max_length=12000)


class DefinitionChangeExtraction(DefinitionContract):
    """変更だけを返し、削除は既存 ID を明示する。空配列は削除と解釈しない。"""

    definitions: list[BusinessDefinition] = Field(default_factory=list)
    deleted_definition_ids: list[str] = Field(default_factory=list)


class DefinitionApplyRequest(DefinitionContract):
    confirmed: Literal[True]


class DefinitionResolveRequest(DefinitionContract):
    choice: Literal["current", "proposed"]


class DefinitionAcceptanceCase(DefinitionContract):
    question_ja: str = Field(min_length=1, max_length=2000)
    expected_concepts: list[str] = Field(default_factory=list)
    expected_path: list[str] = Field(default_factory=list)
    sql: str = Field(default="", max_length=12000)
    expected_rows: list[dict[str, Any]] = Field(default_factory=list)


class DefinitionDataValidationRequest(DefinitionContract):
    confirmed: Literal[True]
    sample_limit: int = Field(default=50, ge=1, le=200)
    acceptance_cases: list[DefinitionAcceptanceCase] = Field(default_factory=list, max_length=30)
