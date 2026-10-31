"""認証/RBAC API schema。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pr_backend_core import CursorPage
from pr_system_settings.auth.router import DisplayTimezoneData
from pr_system_settings.auth.router import LoginRequest as LoginRequest
from pr_system_settings.auth.router import PasswordChangeRequest as PasswordChangeRequest
from pr_system_settings.auth.router import assigned_role_data as assigned_role_data
from pr_system_settings.auth.router import user_data as user_data
from pr_system_settings.users_roles import AssignedRoleData as AssignedRoleData
from pr_system_settings.users_roles import PasswordResetData as PasswordResetData
from pr_system_settings.users_roles import PasswordResetRequest as PasswordResetRequest
from pr_system_settings.users_roles import RoleCreateRequest as RoleCreateRequest
from pr_system_settings.users_roles import RoleData as SharedRoleData
from pr_system_settings.users_roles import RoleDeleteData as RoleDeleteData
from pr_system_settings.users_roles import RoleUpdateRequest as RoleUpdateRequest
from pr_system_settings.users_roles import UserCreateData as UserCreateData
from pr_system_settings.users_roles import UserCreateRequest as UserCreateRequest
from pr_system_settings.users_roles import UserData as UserData
from pr_system_settings.users_roles import UserDeleteData as UserDeleteData
from pr_system_settings.users_roles import UserUpdateRequest as UserUpdateRequest
from pr_system_settings.users_roles import VersionRequest as VersionRequest
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.features.nl2sql.object_identity import (
    canonical_object_part,
    canonical_qualified_name,
    qualified_object_name,
)

from .domain import (
    LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE,
    LOGIN_USER_ID_SCOPE_VALUE_SOURCE,
    DataEntitlementRecord,
    DataEntitlementScopeFilter,
    Principal,
    RoleRecord,
    scope_expression_scope_code,
    scope_filters_scope_code,
)
from .permissions import PermissionDefinition, normalize_permission_codes

# 引用付き token（`"Mixed_Case"`）は 128 byte + 引用符 2 文字まで入力を受け付け、
# canonical_object_part で 128 byte 以内か検証する。
_IDENTIFIER_INPUT_MAX_LENGTH = 130
_APPLY_STATUSES = {"PENDING", "RUNNING", "APPLIED", "FAILED"}
_SCOPE_FILTER_OPERATORS = {
    "EQ",
    "NE",
    "CONTAINS",
    "STARTS_WITH",
    "IN",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "BETWEEN",
    "BEFORE",
    "ON_OR_BEFORE",
    "AFTER",
    "ON_OR_AFTER",
    "IS_NULL",
    "IS_NOT_NULL",
}
_SCOPE_FILTER_VALUE_TYPES = {"TEXT", "NUMBER", "TEMPORAL"}
_SCOPE_FILTER_VALUE_SOURCES = {"LITERAL", LOGIN_USER_ID_SCOPE_VALUE_SOURCE}
_POSITIVE_INTEGER_VALUE_RE = re.compile(r"[1-9]\d*")
_MAX_SCOPE_FILTERS = 8
_MAX_SCOPE_FILTER_VALUES = 25


def _normalize_oracle_identifier(value: str, field_name: str) -> str:
    """DeepSec の owner / object / column を object_identity と同じ canonical token にする。

    引用されていない名前は大文字、引用された名前は大文字小文字を保ち、引用が必要な名前だけ
    `"..."` で保存する。以前の実装は引用符を外して大文字化していたため、`"Mixed_Case"` が
    大文字の同名表 `MIXED_CASE` として扱われていた。
    """

    try:
        return canonical_object_part(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} は有効な Oracle identifier で指定してください。"
            '大文字小文字の混在や記号を含む名前は "Mixed_Case" のように二重引用符で囲みます。'
        ) from exc


def _canonical_resource_code(value: str) -> str:
    """`OWNER.OBJECT` の resource code を canonical な修飾名にする。"""

    stripped = value.strip()
    if not stripped:
        return ""
    if '"' in stripped:
        try:
            return canonical_qualified_name(stripped)
        except ValueError as exc:
            raise ValueError(
                "OWNER.OBJECT 形式の有効な Oracle identifier で指定してください。"
            ) from exc
    normalized = stripped.upper()
    # 引用が不要な名前と旧 resource code は、従来どおり大文字の単純連結で保存する。
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#.-]{0,260}", normalized):
        raise ValueError("英大文字・数字・アンダースコア等で指定してください。")
    return normalized


class DataEntitlementScopeFilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column_name: str = Field(min_length=1, max_length=_IDENTIFIER_INPUT_MAX_LENGTH)
    operator: str = Field(min_length=1, max_length=32)
    value_type: str = Field(default="TEXT", max_length=32)
    value_source: str = Field(default="LITERAL", max_length=32)
    value: str = Field(default="", max_length=512)
    value_to: str = Field(default="", max_length=512)
    values: list[str] = Field(default_factory=list, max_length=_MAX_SCOPE_FILTER_VALUES)

    @field_validator("column_name")
    @classmethod
    def normalize_column_name(cls, value: str) -> str:
        return _normalize_oracle_identifier(value, "column_name")

    @field_validator("operator")
    @classmethod
    def normalize_operator(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _SCOPE_FILTER_OPERATORS:
            raise ValueError("scope filter operator が不正です。")
        return normalized

    @field_validator("value_type")
    @classmethod
    def normalize_value_type(cls, value: str) -> str:
        normalized = value.strip().upper() or "TEXT"
        if normalized not in _SCOPE_FILTER_VALUE_TYPES:
            raise ValueError("scope filter value_type が不正です。")
        return normalized

    @field_validator("value_source")
    @classmethod
    def normalize_value_source(cls, value: str) -> str:
        normalized = value.strip().upper() or "LITERAL"
        if normalized == LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE:
            return LOGIN_USER_ID_SCOPE_VALUE_SOURCE
        if normalized not in _SCOPE_FILTER_VALUE_SOURCES:
            raise ValueError("scope filter value_source が不正です。")
        return normalized

    @field_validator("value", "value_to")
    @classmethod
    def normalize_filter_value(cls, value: str) -> str:
        normalized = value.strip()
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("scope filter value に制御文字は使用できません。")
        return normalized

    @field_validator("values")
    @classmethod
    def normalize_filter_values(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            normalized_item = str(item).strip()
            if not normalized_item:
                continue
            if any(ord(char) < 32 for char in normalized_item):
                raise ValueError("scope filter values に制御文字は使用できません。")
            if normalized_item not in normalized:
                normalized.append(normalized_item)
        return normalized[:_MAX_SCOPE_FILTER_VALUES]

    @model_validator(mode="after")
    def validate_value_source_contract(self) -> DataEntitlementScopeFilterInput:
        if self.value_source == LOGIN_USER_ID_SCOPE_VALUE_SOURCE:
            if self.operator != "EQ" or self.value_type not in {"TEXT", "NUMBER"}:
                raise ValueError(
                    "ログインユーザーID は文字列列または NUMBER 列の EQ 条件でのみ指定できます。"
                )
            self.value = ""
            self.value_to = ""
            self.values = []
        elif (
            self.value_source == "LITERAL"
            and self.value_type == "NUMBER"
            and self.operator == "EQ"
            and not _POSITIVE_INTEGER_VALUE_RE.fullmatch(self.value)
        ):
            raise ValueError("NUMBER の EQ scope 値は正整数で指定してください。")
        return self

    def to_record(self) -> DataEntitlementScopeFilter:
        return DataEntitlementScopeFilter(
            column_name=self.column_name,
            operator=self.operator,
            value_type=self.value_type,
            value_source=self.value_source,
            value=self.value,
            value_to=self.value_to,
            values=list(self.values),
        )


class ScopeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["condition"]
    filter: DataEntitlementScopeFilterInput


class ScopeJoinKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_column: str
    target_column: str

    @field_validator("source_column", "target_column")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _normalize_oracle_identifier(value, "関連キー")


class ScopeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["group"]
    operator: Literal["AND", "OR"]
    children: list[
        Annotated[ScopeCondition | ScopeGroup | ScopeRelatedExists, Field(discriminator="kind")]
    ] = Field(min_length=1, max_length=23)


class ScopeRelatedExists(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["related_exists"]
    profile_id: str = Field(min_length=1, max_length=128)
    object_scope_version: int = Field(ge=1)
    target_owner: str
    target_object: str
    target_type: Literal["TABLE", "VIEW", "MATERIALIZED VIEW"] = "TABLE"
    relation_source: Literal["MANUAL", "FOREIGN_KEY", "ONTOLOGY"] = "MANUAL"
    relation_id: str = Field(default="", max_length=256)
    relation_version: str = Field(default="", max_length=128)
    join_keys: list[ScopeJoinKey] = Field(min_length=1, max_length=8)
    condition: ScopeGroup

    @field_validator("target_owner", "target_object")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _normalize_oracle_identifier(value, "関連テーブル")


ScopeGroup.model_rebuild()
ScopeRelatedExists.model_rebuild()


class ScopeExpression(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    root: ScopeGroup

    @model_validator(mode="after")
    def validate_limits(self) -> ScopeExpression:
        conditions = 0
        related = 0

        def visit(
            node: ScopeCondition | ScopeGroup | ScopeRelatedExists, depth: int, in_relation: bool
        ) -> None:
            nonlocal conditions, related
            if isinstance(node, ScopeGroup):
                if depth > 3:
                    raise ValueError("条件グループは最大 3 階層です。")
                for child in node.children:
                    visit(child, depth + (1 if isinstance(child, ScopeGroup) else 0), in_relation)
            elif isinstance(node, ScopeCondition):
                conditions += 1
            else:
                if in_relation:
                    raise ValueError("関連テーブルの多段参照は指定できません。")
                related += 1
                visit(node.condition, depth + 1, True)

        visit(self.root, 1, False)
        if conditions > 20 or related > 3:
            raise ValueError("条件は 20 件、関連テーブル条件は 3 件以内にしてください。")
        return self


class DataEntitlementInput(BaseModel):
    entitlement_id: str | None = Field(default=None, max_length=36)
    resource_code: str = Field(default="", max_length=261)
    scope_code: str = Field(default="*", max_length=256)
    capability: str = Field(min_length=1, max_length=64)
    target_owner: str = Field(default="", max_length=_IDENTIFIER_INPUT_MAX_LENGTH)
    target_object: str = Field(default="", max_length=_IDENTIFIER_INPUT_MAX_LENGTH)
    target_type: str = Field(default="TABLE", max_length=32)
    column_names: list[str] = Field(default_factory=list)
    scope_mode: str = Field(default="ALL", max_length=32)
    scope_column: str = Field(default="", max_length=_IDENTIFIER_INPUT_MAX_LENGTH)
    scope_expression: ScopeExpression | None = None
    scope_expression_version: Literal[1] | None = None
    scope_filters: list[DataEntitlementScopeFilterInput] = Field(
        default_factory=list,
        max_length=_MAX_SCOPE_FILTERS,
    )

    @field_validator("resource_code")
    @classmethod
    def normalize_resource_code(cls, value: str) -> str:
        return _canonical_resource_code(value)

    @field_validator("capability")
    @classmethod
    def normalize_capability(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "SELECT":
            raise ValueError("capability は SELECT を指定してください。")
        return normalized

    @field_validator("scope_code")
    @classmethod
    def normalize_scope(cls, value: str) -> str:
        normalized = value.strip() or "*"
        if not normalized or any(ord(char) < 32 for char in normalized):
            raise ValueError("有効なデータ範囲を指定してください。")
        return normalized

    @field_validator("target_owner", "target_object", "scope_column")
    @classmethod
    def normalize_optional_identifier(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            return ""
        return _normalize_oracle_identifier(value, info.field_name or "identifier")

    @field_validator("target_type")
    @classmethod
    def normalize_target_type(cls, value: str) -> str:
        normalized = value.strip().upper() or "TABLE"
        if normalized not in {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
            raise ValueError("target_type は TABLE、VIEW、MATERIALIZED VIEW のいずれかです。")
        return normalized

    @field_validator("column_names")
    @classmethod
    def normalize_column_names(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for column in value:
            if not str(column).strip():
                continue
            normalized_column = _normalize_oracle_identifier(str(column), "column_names")
            if normalized_column not in normalized:
                normalized.append(normalized_column)
        return normalized

    @field_validator("scope_mode")
    @classmethod
    def normalize_scope_mode(cls, value: str) -> str:
        normalized = value.strip().upper() or "ALL"
        if normalized not in {"ALL", "COLUMN_EQUALS", "FILTERS", "EXPRESSION"}:
            raise ValueError(
                "scope_mode は ALL、COLUMN_EQUALS、FILTERS、EXPRESSION のいずれかです。"
            )
        return normalized

    @model_validator(mode="after")
    def validate_expression_mode(self) -> DataEntitlementInput:
        if self.target_owner and self.target_object:
            # 保存キーは対象の canonical token から作り、送信された resource_code と食い違わせない。
            self.resource_code = f"{self.target_owner}.{self.target_object}"
        if self.scope_mode == "EXPRESSION":
            if self.scope_expression is None or self.scope_filters or self.scope_column:
                raise ValueError("EXPRESSION は条件ツリーのみ指定してください。")
        elif self.scope_expression is not None:
            raise ValueError("条件ツリーには EXPRESSION モードを指定してください。")
        return self

    def to_record(self, role_id: str) -> DataEntitlementRecord:
        scope_filters = [item.to_record() for item in self.scope_filters]
        scope_code = self.scope_code
        if self.scope_mode == "ALL":
            scope_code = "*"
        elif self.scope_mode == "EXPRESSION":
            if self.scope_expression is None:
                raise ValueError("条件ツリーを指定してください。")
            scope_code = scope_expression_scope_code(self.scope_expression.model_dump())
        elif self.scope_mode == "FILTERS":
            scope_code = scope_filters_scope_code(scope_filters)
        return DataEntitlementRecord(
            entitlement_id=self.entitlement_id or "",
            role_id=role_id,
            resource_code=self.resource_code,
            scope_code=scope_code,
            capability=self.capability,
            target_owner=self.target_owner,
            target_object=self.target_object,
            target_type=self.target_type,
            column_names=list(self.column_names),
            scope_mode=self.scope_mode,
            scope_column=self.scope_column,
            scope_filters=scope_filters,
            scope_expression=self.scope_expression.model_dump() if self.scope_expression else None,
            scope_expression_version=self.scope_expression_version,
        )


class RolePermissionsUpdateRequest(BaseModel):
    """権限管理画面の保存。ロールの menu 権限と業務プロファイル利用権限だけを更新する。"""

    version: int = Field(ge=1)
    permissions: list[str] = Field(default_factory=list)
    allowed_profile_ids: list[str] | None = None


# 版だけを送る操作は共通契約の VersionRequest と同じ形。
RoleArchiveRequest = VersionRequest
RoleRestoreRequest = VersionRequest


class DeepSecApplyRequest(BaseModel):
    checksum: str = Field(min_length=64, max_length=64)
    confirmation: str = Field(default="", max_length=128)


class DeepSecResetRequest(BaseModel):
    confirmation: str = Field(default="", max_length=128)


class DeepSecConfigUpdate(BaseModel):
    data_user_password: str = Field(min_length=12, max_length=256)

    @field_validator("data_user_password")
    @classmethod
    def validate_data_user_password(cls, value: str) -> str:
        # 「${」は backend/.env を読むとき（python-dotenv）に引用符の種類によらず環境変数として
        # 展開され、Oracle に設定した値と変わるので受け付けない（#1000）。
        if (
            '"' in value
            or "${" in value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
        ):
            raise ValueError(
                "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD は二重引用符・「${」・制御文字を"
                "含めずに指定してください。"
            )
        return value


class DataEntitlementData(BaseModel):
    entitlement_id: str
    resource_code: str
    scope_code: str
    capability: str
    target_owner: str = ""
    target_object: str = ""
    target_type: str = "TABLE"
    column_names: list[str] = Field(default_factory=list)
    scope_mode: str = "ALL"
    scope_column: str = ""
    scope_expression: ScopeExpression | None = None
    scope_filters: list[DataEntitlementScopeFilterInput] = Field(default_factory=list)
    data_grant_name: str = ""
    sql_checksum: str = ""
    apply_status: str = "PENDING"
    apply_error_message: str = ""
    applied_at: datetime | None = None
    sql: list[str] = Field(default_factory=list)
    checksum: str = ""

    @classmethod
    def from_record(
        cls,
        record: DataEntitlementRecord,
        *,
        sql: list[str] | None = None,
        checksum: str = "",
    ) -> DataEntitlementData:
        return cls(
            entitlement_id=record.entitlement_id,
            resource_code=record.resource_code,
            scope_code=record.scope_code,
            capability=record.capability,
            target_owner=record.target_owner,
            target_object=record.target_object,
            target_type=record.target_type,
            column_names=list(record.column_names),
            scope_mode=record.scope_mode,
            scope_column=record.scope_column,
            scope_expression=(
                ScopeExpression.model_validate(record.scope_expression)
                if record.scope_expression
                else None
            ),
            scope_filters=[
                DataEntitlementScopeFilterInput(
                    column_name=item.column_name,
                    operator=item.operator,
                    value_type=item.value_type,
                    value_source=item.value_source,
                    value=item.value,
                    value_to=item.value_to,
                    values=list(item.values),
                )
                for item in record.scope_filters
            ],
            data_grant_name=record.data_grant_name,
            sql_checksum=record.sql_checksum,
            apply_status=(
                record.apply_status if record.apply_status in _APPLY_STATUSES else "PENDING"
            ),
            apply_error_message=record.apply_error_message,
            applied_at=record.applied_at,
            sql=list(sql or []),
            checksum=checksum or record.sql_checksum,
        )


class RoleData(SharedRoleData):
    """共通のロール項目に、NL2SQL の権限・業務プロファイル・Data Grant を足す。"""

    permissions: list[str]
    data_entitlements: list[DataEntitlementData]
    allowed_profile_ids: list[str]

    @classmethod
    def from_record(cls, role: RoleRecord) -> RoleData:
        return cls(
            role_id=role.role_id,
            role_code=role.role_code,
            display_name=role.display_name,
            description=role.description,
            is_built_in=role.is_built_in,
            archived=role.archived,
            version=role.version,
            permissions=sorted(normalize_permission_codes(role.permissions)),
            data_entitlements=[DataEntitlementData.from_record(item) for item in role.entitlements],
            allowed_profile_ids=sorted(role.allowed_profile_ids),
        )


class DeepSecTargetObjectData(BaseModel):
    """DeepSec Data Grant picker 用の live Oracle object summary。

    `owner` / `name` はカタログ上の値（引用符なし・大文字小文字を保持）、`qualified_name` は
    `qualified_object_name` の canonical 修飾名（引用が必要な部分だけ `"..."`）。
    """

    name: str
    owner: str = ""
    qualified_name: str = ""
    object_type: str
    row_count: int | None = None
    comment: str = ""

    @model_validator(mode="after")
    def fill_qualified_name(self) -> DeepSecTargetObjectData:
        if self.qualified_name or not self.owner:
            return self
        self.qualified_name = qualified_object_name(self.owner, self.name)
        return self


class DeepSecTargetObjectPageData(CursorPage[DeepSecTargetObjectData]):
    """DeepSec Data Grant picker 用の keyset page（共通の CursorPage に件数の内訳と警告を足す）。"""

    runtime: str = "oracle"
    owner: str = ""
    items: list[DeepSecTargetObjectData] = Field(default_factory=list)
    counts_included: bool = False
    warnings: list[str] = Field(default_factory=list)


class DeepSecTargetColumnData(BaseModel):
    # Data Grant の column_names / scope 条件と同じ canonical token（引用が必要な列だけ "..."）。
    column_name: str
    logical_name: str = ""
    data_type: str = ""
    nullable: bool = True
    comment: str = ""
    sample_values: list[str] = Field(default_factory=list)


class DeepSecTargetObjectDetailData(DeepSecTargetObjectData):
    columns: list[DeepSecTargetColumnData] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DeepSecDataEntitlementUpdateRequest(BaseModel):
    version: int = Field(ge=1)
    data_entitlements: list[DataEntitlementInput] = Field(default_factory=list)


class DeepSecDataEntitlementPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    data_entitlements: list[DataEntitlementInput]


class DeepSecDataEntitlementPreviewData(BaseModel):
    role_id: str
    version: int
    data_entitlements: list[DataEntitlementData]
    cleanup_sql: list[str] = Field(default_factory=list)
    checksum: str


class DeepSecDataEntitlementApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    confirmation: str = Field(default="", max_length=128)
    data_entitlements: list[DataEntitlementInput]


class DeepSecRoleEntitlementsData(BaseModel):
    role_id: str
    role_code: str
    display_name: str
    description: str
    is_built_in: bool
    archived: bool
    version: int
    data_entitlements: list[DataEntitlementData]

    @classmethod
    def from_record(cls, role: RoleRecord) -> DeepSecRoleEntitlementsData:
        return cls(
            role_id=role.role_id,
            role_code=role.role_code,
            display_name=role.display_name,
            description=role.description,
            is_built_in=role.is_built_in,
            archived=role.archived,
            version=role.version,
            data_entitlements=[DataEntitlementData.from_record(item) for item in role.entitlements],
        )


class DeepSecDataEntitlementApplyData(BaseModel):
    role: DeepSecRoleEntitlementsData
    status: str
    checksum: str
    cleanup_count: int = Field(ge=0)
    applied_count: int = Field(ge=0)


class CurrentUserData(DisplayTimezoneData):
    user_uuid: str
    login_user_id: str
    display_name: str
    status: str
    force_password_change: bool
    role_codes: list[str]
    # ロールの表示名（`role_codes` と同じ順）。サイドナビのアカウント欄に出す（#1644）。
    role_display_names: list[str]
    is_system_admin: bool
    permissions: list[str]
    data_entitlements: list[DataEntitlementData]
    allowed_profile_ids: list[str] = Field(default_factory=list)
    debug_mode: bool = False
    password_change_allowed: bool

    @classmethod
    def from_principal(cls, principal: Principal, *, debug_mode: bool = False) -> CurrentUserData:
        return cls(
            user_uuid=principal.user_uuid,
            login_user_id=principal.login_user_id,
            display_name=principal.display_name,
            status=principal.status,
            force_password_change=principal.force_password_change,
            role_codes=principal.role_codes,
            role_display_names=principal.role_names(),
            is_system_admin=principal.is_system_admin,
            permissions=sorted(principal.permissions),
            data_entitlements=[
                DataEntitlementData.from_record(item) for item in principal.data_entitlements
            ],
            allowed_profile_ids=sorted(principal.allowed_profile_ids),
            debug_mode=debug_mode,
            password_change_allowed=principal.password_change_allowed and not debug_mode,
        )


class ProfileAccessRoleData(BaseModel):
    """業務プロファイルを使えるロール。画面はロール名を出し、ID（UUID）・コードは出さない（#1682）。"""

    role_id: str
    role_code: str
    display_name: str


class ProfileAccessProfileData(BaseModel):
    id: str
    name: str
    category: str = ""
    description: str = ""
    archived: bool = False
    # ロール名の順。ID だけを返すと画面が UUID を出すため、名前とコードを添える（#1682）。
    allowed_roles: list[ProfileAccessRoleData] = Field(default_factory=list)


class PermissionData(BaseModel):
    code: str
    group: str
    label: str
    description: str
    implies: list[str]

    @classmethod
    def from_definition(cls, definition: PermissionDefinition) -> PermissionData:
        return cls(
            code=definition.code,
            group=definition.group,
            label=definition.label,
            description=definition.description,
            implies=list(definition.implies),
        )
