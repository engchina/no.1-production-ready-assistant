import type {
  AssignedRole,
  SecurityRole as SharedSecurityRole,
  SecurityUser,
} from "@production-ready/system-settings";

export type { AssignedRole, SecurityUser };

export type ScopeNode = ScopeGroup | { kind: "condition"; filter: DataEntitlementScopeFilter } | ScopeRelatedExists;
export interface ScopeGroup { kind: "group"; operator: "AND" | "OR"; children: ScopeNode[] }
export interface ScopeExpression { version: 1; root: ScopeGroup }
export interface ScopeJoinKey { source_column: string; target_column: string }
export interface ScopeRelatedExists {
  kind: "related_exists"; profile_id: string; object_scope_version: number;
  target_owner: string; target_object: string; target_type: "TABLE" | "VIEW" | "MATERIALIZED VIEW";
  relation_source: "MANUAL" | "FOREIGN_KEY" | "ONTOLOGY"; relation_id: string; relation_version: string;
  join_keys: ScopeJoinKey[]; condition: ScopeGroup;
}
export interface ScopeProfile { id: string; name: string; object_scope_version: number; objects: string[] }
export interface ScopeRelation { id: string; source: "FOREIGN_KEY" | "ONTOLOGY"; version: string; target: string; join_keys: ScopeJoinKey[] }
export interface ScopeRelationCatalog { profile_id: string; object_scope_version: number; objects: string[]; relations: ScopeRelation[] }

export type DataEntitlementScopeValueType = "TEXT" | "NUMBER" | "TEMPORAL";
export type DataEntitlementScopeValueSource = "LITERAL" | "LOGIN_USER_ID";
export type DataEntitlementScopeOperator =
  | "EQ"
  | "NE"
  | "CONTAINS"
  | "STARTS_WITH"
  | "IN"
  | "GT"
  | "GTE"
  | "LT"
  | "LTE"
  | "BETWEEN"
  | "BEFORE"
  | "ON_OR_BEFORE"
  | "AFTER"
  | "ON_OR_AFTER"
  | "IS_NULL"
  | "IS_NOT_NULL";

export interface DataEntitlementScopeFilter {
  column_name: string;
  operator: DataEntitlementScopeOperator | string;
  value_type: DataEntitlementScopeValueType | string;
  value_source?: DataEntitlementScopeValueSource | string;
  value?: string;
  value_to?: string;
  values?: string[];
}

export interface DataEntitlement {
  entitlement_id?: string;
  resource_code: string;
  scope_code: string;
  capability: string;
  target_owner?: string;
  target_object?: string;
  target_type?: "TABLE" | "VIEW" | "MATERIALIZED VIEW" | string;
  column_names?: string[];
  scope_mode?: "ALL" | "COLUMN_EQUALS" | "FILTERS" | string;
  scope_column?: string;
  scope_expression?: ScopeExpression | null;
  scope_expression_version?: 1;
  scope_filters?: DataEntitlementScopeFilter[];
  data_grant_name?: string;
  sql_checksum?: string;
  apply_status?: "PENDING" | "RUNNING" | "APPLIED" | "FAILED" | string;
  apply_error_message?: string;
  applied_at?: string | null;
  sql?: string[];
  checksum?: string;
}

export interface DeepSecTargetObject {
  name: string;
  owner: string;
  qualified_name?: string;
  object_type: string;
  row_count?: number | null;
  comment: string;
}

export interface DeepSecTargetObjectPage {
  items: DeepSecTargetObject[];
  total: number | null;
  table_count?: number;
  view_count?: number;
  counts_included?: boolean;
  next_cursor: string | null;
  warnings?: string[];
}

export interface DeepSecTargetColumn {
  column_name: string;
  logical_name: string;
  data_type: string;
  nullable: boolean;
  comment: string;
  sample_values?: string[];
}

export interface DeepSecTargetObjectDetail extends DeepSecTargetObject {
  columns: DeepSecTargetColumn[];
  warnings?: string[];
}

export interface CurrentUser {
  user_uuid: string;
  login_user_id: string;
  display_name: string;
  status: string;
  force_password_change: boolean;
  role_codes: string[];
  /** ロールの表示名（`role_codes` と同じ順。#1644）。 */
  role_display_names: string[];
  is_system_admin: boolean;
  permissions: string[];
  data_entitlements: DataEntitlement[];
  allowed_profile_ids: string[];
  debug_mode: boolean;
  password_change_allowed: boolean;
}

export interface SecurityUserDeleteResult {
  deleted: boolean;
  user_uuid: string;
  login_user_id: string;
}

/** 共通のロール項目（platform）に、NL2SQL の権限・Data Grant・業務プロファイル利用権限を足す（#206）。 */
export interface SecurityRole extends SharedSecurityRole {
  permissions: string[];
  data_entitlements: DataEntitlement[];
  allowed_profile_ids: string[];
}

export interface SecurityRoleDeleteResult {
  deleted: boolean;
  role_id: string;
  role_code: string;
}

export interface ProfileAccessProfile {
  id: string;
  name: string;
  category: string;
  description: string;
  archived: boolean;
  /** 業務プロファイルを使えるロール（ロール名の順）。画面はロール名を出し、ID・コードは出さない（#1682）。 */
  allowed_roles: ProfileAccessRole[];
}

/** 業務プロファイルを使えるロール。`role_id` は key にだけ使う（#1682）。 */
export interface ProfileAccessRole {
  role_id: string;
  role_code: string;
  display_name: string;
}

/** 権限管理の業務プロファイルの候補の 1 ページ（検索とページング。#608）。 */
export interface ProfileAccessProfilePage {
  items: ProfileAccessProfile[];
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
}

export interface DeepSecRoleEntitlements {
  role_id: string;
  role_code: string;
  display_name: string;
  description: string;
  is_built_in: boolean;
  archived: boolean;
  version: number;
  data_entitlements: DataEntitlement[];
}

export interface DeepSecDataEntitlementPreview {
  role_id: string;
  version: number;
  data_entitlements: DataEntitlement[];
  cleanup_sql: string[];
  checksum: string;
}

export interface DeepSecDataEntitlementApplyResult {
  role: DeepSecRoleEntitlements;
  status: string;
  checksum: string;
  cleanup_count: number;
  applied_count: number;
}

export interface PermissionDefinition {
  code: string;
  group: string;
  label: string;
  description: string;
  implies: string[];
}

export interface DeepSecStep {
  step_no: number;
  key: string;
  title: string;
  description: string;
  checksum: string;
  status: "PENDING" | "RUNNING" | "APPLIED" | "FAILED";
  error_message: string;
  executed_at: string | null;
  sql: string[];
}

export interface DeepSecPlan {
  version: string;
  driver_mode: string;
  connection_security?: string;
  deepsec_enabled: boolean;
  data_user: string;
  has_data_user_password: boolean;
  steps: DeepSecStep[];
}

export interface DeepSecStatus {
  configured: boolean;
  driver_mode: string;
  connection_security?: string;
  deepsec_enabled: boolean;
  data_user: string;
  has_data_user_password: boolean;
  objects: Record<string, number>;
  message: string;
}

export interface DeepSecVerification {
  version: string;
  passed: boolean;
  checked_at: string;
  checks: Array<{ key: string; passed: boolean; detail: string }>;
}
