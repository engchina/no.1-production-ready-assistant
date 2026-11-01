import { compareNames } from "@production-ready/ui";

import {
  ontologyJoinConditionText,
  ontologyPhysicalNodeLabel,
} from "./physicalIdentity";

export type OntologyJsonValue =
  | string
  | number
  | boolean
  | null
  | OntologyJsonValue[]
  | { [key: string]: OntologyJsonValue };

export type OntologyRevisionStatus = "draft" | "published" | "archived";
export type OntologyReviewStatus =
  | "draft"
  | "proposed"
  | "reviewed"
  | "approved"
  | "rejected"
  | "published"
  | "orphaned";
export type OntologyValidationStatus = "passed" | "warning" | "blocked" | "unreviewed";
export type OntologyDirection = "forward" | "reverse" | "directed" | "bidirectional";
export type OntologyCardinality = "one_to_one" | "one_to_many" | "many_to_one" | "many_to_many" | "unknown";
export type OntologyNodeKind =
  | "schema"
  | "table"
  | "view"
  | "column"
  | "object_type"
  | "interface"
  | "function"
  | "action_type"
  | "shared_property"
  | "value_type"
  | "enumeration"
  | "object_set"
  | "business_entity"
  | "business_event"
  | "property"
  | "metric"
  | "business_term"
  | "business_rule"
  | "enum_value"
  | "question_intent"
  | "query_plan"
  | "cte"
  | "sql_table"
  | "sql_column"
  | "sql_join"
  | "sql_filter"
  | "sql_aggregate"
  | "sql_group"
  | "sql_having"
  | "sql_order"
  | "sql_limit"
  | "sql_window"
  | "sql_artifact"
  | "validation_finding"
  | "execution_preview"
  | "unknown";

export interface OntologyRevision {
  id: string;
  version: number;
  status: OntologyRevisionStatus;
  schema_fingerprint: string;
  etag: string;
  created_at?: string;
  published_at?: string | null;
  reasoning_status?: "not_started" | "materializing" | "validating" | "ready" | "failed";
  rdf_graph_name?: string;
  inferred_graph_name?: string;
  shacl_report_artifact_id?: string;
  renderer_version?: string;
  artifact_hashes?: Record<string, string>;
}

export interface OntologyPhysicalMapping {
  owner?: string;
  object_name?: string;
  object_type?: "TABLE" | "VIEW" | string;
  column_name?: string | null;
  stable_id?: string;
}

export interface OntologyPhysicalObjectRef {
  node_id?: string;
  owner?: string;
  object_name: string;
  object_type?: "table" | "view" | string;
}

export interface OntologyPhysicalMappingDetail {
  object_ref: OntologyPhysicalObjectRef;
  column_refs?: Array<{
    node_id?: string;
    owner?: string;
    object_name: string;
    column_name: string;
    ordinal?: number | null;
  }>;
  expression_sql?: string;
  lineage_source_ids?: string[];
}

export interface OntologyJoinCondition {
  source_column?: string;
  target_column?: string;
  left?: { owner?: string; object_name?: string; column_name?: string };
  right?: { owner?: string; object_name?: string; column_name?: string };
  operator?: string;
  ordinal?: number;
}

export interface OntologyNode {
  id: string;
  revision_id?: string;
  kind: OntologyNodeKind;
  technical_name?: string;
  business_name_ja: string;
  description?: string;
  description_ja?: string;
  physical_mapping?: OntologyPhysicalMapping | null;
  physical_mappings?: OntologyPhysicalMappingDetail[];
  aliases?: string[];
  source?: string;
  confidence?: number;
  review_status?: OntologyReviewStatus;
  validation_status?: OntologyValidationStatus;
  metadata?: Record<string, OntologyJsonValue>;
  provenance?: {
    source_kind?: string;
    source_id?: string;
    source_detail?: string;
    inferred_by?: string;
    observed_at?: string;
    evidence?: OntologyEvidence[];
  };
  business_rule_definition?: BusinessRuleDefinition | null;
  enum_value_definition?: EnumValueDefinition | null;
}

export interface OntologyEvidence {
  source_document_id: string;
  source_sha256: string;
  locator_kind: "page" | "paragraph" | "sheet_row" | "line" | "qa_row" | "schema_object";
  locator: string;
  excerpt_hash: string;
  excerpt_ja?: string;
}

export interface BusinessRuleDefinition {
  rule_kind: "constraint" | "calculation" | "classification" | "validation";
  statement_ja: string;
  applies_to_node_ids: string[];
  expression?: BusinessRuleExpression | null;
  severity: "info" | "warning" | "violation";
  execution_mode: "shacl" | "sql_definition" | "documentation";
}

export interface BusinessRuleExpression {
  operator:
    | "all"
    | "any"
    | "not"
    | "eq"
    | "ne"
    | "lt"
    | "lte"
    | "gt"
    | "gte"
    | "in"
    | "not_in"
    | "is_null"
    | "not_null";
  property_node_id?: string;
  value?: OntologyJsonValue;
  values?: OntologyJsonValue[];
  children?: BusinessRuleExpression[];
}

export interface EnumValueDefinition {
  code: string;
  label_ja: string;
  aliases: string[];
  physical_literal: OntologyJsonValue;
  data_type: "string" | "integer" | "number" | "boolean" | "date" | "datetime";
  property_node_id: string;
}

export interface OntologyEdge {
  id: string;
  kind?: string;
  source_node_id: string;
  target_node_id: string;
  relationship_name_ja: string;
  description_ja?: string;
  direction?: OntologyDirection;
  cardinality?: OntologyCardinality;
  join_conditions?: OntologyJoinCondition[];
  allowed_join_types?: string[];
  source?: string;
  review_status?: OntologyReviewStatus;
  validation_status?: OntologyValidationStatus;
  enabled?: boolean;
  metadata?: Record<string, OntologyJsonValue>;
}

export interface OntologyGraph {
  revision_id?: string;
  revision?: OntologyRevision;
  nodes: OntologyNode[];
  edges: OntologyEdge[];
}

export interface ProfileOntologyView {
  id: string;
  profile_id: string;
  ontology_revision_id: string;
  etag?: string;
  selected_node_ids?: string[];
  selected_edge_ids?: string[];
  node_ids?: string[];
  edge_ids?: string[];
  allowed_path_ids?: string[];
  table_usage?: Record<string, string>;
  column_policies?: Record<string, string>;
  graph?: OntologyGraph;
  activation_scenarios_ja?: string[];
  activation_keywords?: string[];
  scenario_version?: number;
  source_profile_etag?: string;
  source_profile_scope_fingerprint?: string;
  table_usages_ja?: Record<string, string>;
  draft_node_overrides?: Array<{
    node_id: string;
    business_name_ja?: string;
    table_usage?: string;
  }>;
  draft_edge_overrides?: Array<{
    edge_id: string;
    cardinality?: OntologyCardinality;
    allowed_path?: boolean;
  }>;
}

export interface ProfileOntologyViewData {
  profile_ontology_view?: ProfileOntologyView;
  ontology_graph?: OntologyGraph;
  materialized?: boolean;
  stale?: boolean;
  warnings_ja?: string[];
}

export interface IntentConcept {
  id?: string;
  node_id?: string;
  ontology_node_id?: string;
  name?: string;
  name_ja?: string;
  role?: string;
  aggregation?: string;
  granularity?: string;
  physical_object_ids?: string[];
  formula_description_ja?: string;
  confidence?: number;
}

export interface IntentFilter {
  id?: string;
  field?: string;
  property_node_id?: string;
  label_ja?: string;
  operator: string;
  value: OntologyJsonValue;
  value_type?: string;
  node_id?: string;
  required?: boolean;
}

export interface IntentTimeRange {
  field?: string;
  property_node_id?: string;
  label_ja?: string;
  start?: string | null;
  end?: string | null;
  start_inclusive?: boolean;
  end_inclusive?: boolean;
  relative?: string | null;
  relative_expression?: string;
  timezone?: string;
  granularity?: string | null;
}

export interface IntentSort {
  field?: string;
  target_id?: string;
  direction: "asc" | "desc";
}

export interface IntentRelationshipPath {
  id?: string;
  label?: string;
  name_ja?: string;
  node_ids: string[];
  edge_ids?: string[];
  reviewed?: boolean;
  approved?: boolean;
  explanation_ja?: string;
}

export interface IntentAmbiguity {
  id?: string;
  code?: string;
  message?: string;
  message_ja?: string;
  field?: string;
  options?: string[];
  resolution?: string | null;
  blocking?: boolean;
  resolved?: boolean;
}

export type ClarificationMode = "review_only" | "guided";
// --- 回答前の確認（SQL の要件の下書き。#1439） ---

export type RequirementSection =
  | "select"
  | "from"
  | "join"
  | "where"
  | "period"
  | "group_by"
  | "order_by"
  | "limit"
  | "subquery"
  | "other";
export type RequirementStatus = "confirmed" | "inferred" | "missing" | "ambiguous";
export type RequirementSource =
  | "user"
  | "question"
  | "schema"
  | "profile"
  | "glossary"
  | "ontology"
  | "calendar";
export type RequirementAnswerKind = "single_select" | "multi_select" | "free_text";
export type RequirementPhase = "needs_answer" | "ready" | "unanswerable";

export interface RequirementPeriod {
  expression_ja: string;
  relative_kind: string;
  count?: number | null;
  fiscal_year_start_month?: number | null;
  reference_basis: "execution_date" | "latest_data_date";
  reference_date?: string;
  date_column?: string;
  start_date?: string;
  end_date?: string;
  range_ja?: string;
  sql_condition?: string;
}

export interface RequirementCandidate {
  id: string;
  label_ja: string;
  description_ja?: string;
  evidence_ja?: string;
  source: RequirementSource;
  tables: string[];
  columns: string[];
  relationship_ids: string[];
  sql_hint?: string;
  period?: RequirementPeriod | null;
  refs_ja?: string;
}

export interface RequirementItem {
  id: string;
  key: string;
  section: RequirementSection;
  label_ja: string;
  value_ja: string;
  status: RequirementStatus;
  reason_ja?: string;
  question_ja?: string;
  answer_kind: RequirementAnswerKind;
  candidates: RequirementCandidate[];
  source: RequirementSource;
  tables: string[];
  columns: string[];
  relationship_ids: string[];
  sql_hint?: string;
  period?: RequirementPeriod | null;
  refs_ja?: string;
  answered: boolean;
  answer_ja?: string;
}

export interface SqlRequirementDraft {
  version: number;
  question_original: string;
  clarified_question_ja: string;
  items: RequirementItem[];
  unanswerable_reason_ja?: string;
  warnings_ja: string[];
  reference_date?: string;
  model?: string;
  sql_skeleton?: string;
}

export interface RequirementState {
  status: RequirementPhase;
  current_item?: RequirementItem | null;
  unresolved_items: RequirementItem[];
  answered_count: number;
  review_round: number;
  max_review_rounds: number;
  review_limit_reached: boolean;
  select_from_clear: boolean;
  minimum_missing_ja: string[];
  can_apply: boolean;
  summary_ja: string;
  sql_skeleton: string;
  message_ja: string;
}

export interface QuestionIntentGraph {
  version: number;
  question_original?: string;
  question_effective?: string;
  profile_view_id?: string;
  ontology_revision_id?: string;
  question?: string;
  rewritten_question?: string | null;
  entities: Array<IntentConcept | string>;
  metrics: Array<IntentConcept | string>;
  dimensions: Array<IntentConcept | string>;
  filters: IntentFilter[];
  time_range?: IntentTimeRange | null;
  grain?: string | null;
  granularity?: string | null;
  sort?: IntentSort[];
  sorts?: IntentSort[];
  limit?: number | null;
  candidate_paths?: IntentRelationshipPath[];
  selected_path_id?: string | null;
  ambiguities?: IntentAmbiguity[];
  confidence?: number;
  created_at?: string;
  graph?: OntologyGraph;
}

export interface SqlSemanticItem {
  id?: string;
  scope_id?: string;
  catalog?: string;
  owner?: string;
  expression?: string;
  expression_sql?: string;
  query_sql?: string;
  source_sql?: string;
  qualified_name?: string;
  /** 表示用の所有者付き名前。SQL に owner が無い表は実行時の current schema で補った値。 */
  resolved_owner?: string;
  resolved_qualified_name?: string;
  name?: string;
  output_name?: string;
  alias?: string | null;
  table?: string | null;
  column?: string | null;
  lineage?: string[];
  referenced_columns?: string[];
  function_name?: string;
  clause?: string;
  is_cte?: boolean;
  /** SELECT の項目が `*` / `別名.*` の列展開か（`COUNT(*)` は含まない。projections だけが持つ）。 */
  contains_wildcard?: boolean;
  /**
   * SQL で owner / 表（または別名）/ 列名が二重引用符で囲まれていたか。`SALES."Mixed_Case"` と
   * 大文字の同名表 `SALES.MIXED_CASE` を区別して接地する（#573）。旧 artifact には無い。
   */
  owner_quoted?: boolean;
  table_quoted?: boolean;
  name_quoted?: boolean;
}

export interface SqlSemanticJoin extends SqlSemanticItem {
  source_table?: string;
  target_table?: string;
  join_type?: string;
  condition?: string;
  condition_sql?: string;
  left_source?: string;
  right_source?: string;
  using_columns?: string[];
  referenced_columns?: string[];
  is_cartesian?: boolean;
  ontology_edge_id?: string | null;
  reviewed_path?: boolean;
}

export interface SqlSemanticGraph {
  version?: number;
  sql_hash?: string;
  dialect: string;
  statement_type?: string;
  raw_sql?: string;
  parse_status?: "parsed" | "blocked";
  ctes: Array<SqlSemanticItem | string>;
  tables: Array<SqlSemanticItem | string>;
  columns: Array<SqlSemanticItem | string>;
  joins: Array<SqlSemanticJoin | string>;
  projections?: Array<SqlSemanticItem | string>;
  filters: Array<SqlSemanticItem | string>;
  aggregates: Array<SqlSemanticItem | string>;
  group_by?: Array<SqlSemanticItem | string>;
  groups?: Array<SqlSemanticItem | string>;
  having: Array<SqlSemanticItem | string>;
  order_by?: Array<SqlSemanticItem | string>;
  orders?: Array<SqlSemanticItem | string>;
  windows: Array<SqlSemanticItem | string>;
  set_operations?: Array<SqlSemanticItem | string>;
  subqueries?: Array<SqlSemanticItem | string>;
  limit?: number | null;
  lineage?: Record<string, string[]> | Array<Record<string, unknown>>;
  parse_warnings?: string[];
  graph?: OntologyGraph;
}

export type ValidationSeverity = "pass" | "passed" | "warning" | "blocker";

export interface OntologyValidationFinding {
  id?: string;
  code: string;
  severity: ValidationSeverity;
  message?: string;
  message_ja?: string;
  node_ids?: string[];
  intent_element_ids?: string[];
  sql_element_ids?: string[];
  ontology_node_ids?: string[];
  path?: string | null;
  remediation?: string | null;
  suggested_action_ja?: string | null;
}

export interface OntologyValidationReport {
  id?: string;
  status?: "passed" | "warning" | "blocked";
  is_valid?: boolean;
  intent_version?: number;
  sql_hash?: string;
  ontology_revision_id?: string;
  findings: OntologyValidationFinding[];
  intent_coverage: number;
  business_summary?: string;
  passed_count?: number;
  warning_count?: number;
  blocker_count?: number;
  validation_hash?: string;
  created_at?: string;
}

export type GraphPatchOperationName = "add" | "replace" | "remove";

export interface GraphPatchOperation {
  op: GraphPatchOperationName;
  path: string;
  value?: OntologyJsonValue;
  label?: string;
  reason_ja?: string;
}

export interface GraphPatch {
  base_version: number;
  operations: GraphPatchOperation[];
  reason?: string;
  summary_ja?: string;
  suggested_question?: string | null;
}

export interface OntologyProposal {
  id: string;
  session_id: string;
  profile_id?: string;
  ontology_revision_id?: string;
  base_revision_id?: string;
  kind?: "alias" | "metric_definition" | "relationship" | "mapping" | "profile_policy" | "query_example";
  status: "draft" | "submitted" | "accepted" | "rejected";
  patch?: GraphPatch | null;
  summary?: string;
  title_ja?: string;
  description_ja?: string;
  proposal_payload?: {
    kind?: string;
    values?: Record<string, OntologyJsonValue>;
  };
  created_at?: string;
}

export interface OntologyProposalReviewData {
  proposal: OntologyProposal;
  draft?: {
    revision: OntologyRevision;
    nodes: OntologyNode[];
    edges: OntologyEdge[];
  } | null;
}

// --- AI オントロジー構築(backend の OntologyBuildJob と対応) ---

export type OntologyBuildStepName =
  | "source_extraction"
  | "schema_context"
  | "schema_naming"
  | "qa_extraction"
  | "text_extraction"
  | "proposal_registration";
export type OntologyBuildStepStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "skipped"
  | "failed";
export type OntologyBuildStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "succeeded_with_warnings"
  | "failed"
  | "cancelled";

export interface OntologyBuildStep {
  name: OntologyBuildStepName;
  status: OntologyBuildStepStatus;
  detail_ja?: string;
  /** 機械可読の状態/失敗コード(backend が付与。旧 job は空)。 */
  code?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface OntologyBuildEvent {
  phase?: "freeze" | "evidence" | "concepts" | "validation" | "markdown" | "save" | null;
  at: string;
  message_ja: string;
  /** 機械可読イベントコード(例: MARKDOWN_DRAFT_UPDATED。旧 job は空)。 */
  code?: string;
  /** イベントの帰属ステップ(backend が付与。旧 job は null)。 */
  step?: OntologyBuildStepName | null;
}

export interface OntologyBuildJob {
  result_bundle_id?: string;
  concept_coverage?: {kind:string;status:string;count:number;reason_ja:string}[];
  definition_phases?: { name: "freeze" | "evidence" | "concepts" | "objects" | "shared" | "capabilities" | "validation" | "markdown" | "save"; status: "pending" | "running" | "succeeded" | "failed" | "skipped"; detail_ja: string; started_at?: string; finished_at?: string }[];
  id: string;
  profile_id: string;
  status: OntologyBuildStatus;
  steps: OntologyBuildStep[];
  events?: OntologyBuildEvent[];
  proposal_ids: string[];
  draft_revision_id?: string;
  draft_etag?: string;
  source_document_ids?: string[];
  sources?: OntologySourceProgress[];
  markdown_output?: string;
  warnings_ja: string[];
  error_message_ja?: string;
  /** 機械可読の失敗コード(例: SCHEMA_SCOPE_UNRESOLVED。旧 job は空)。 */
  error_code?: string;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface OntologySourceProgress {
  source_document_id: string;
  filename: string;
  status: "stored" | "extracting" | "extracted" | "failed";
  extracted_chunk_count?: number;
  warnings_ja?: string[];
  error_message_ja?: string;
}

export type OntologySourceRole = "source" | "qa";

export interface OntologySourceDocument {
  id: string;
  profile_id: string;
  filename: string;
  source_role?: OntologySourceRole;
  media_type?: string;
  size_bytes?: number;
  status: "stored" | "extracting" | "extracted" | "failed";
  extracted_chunk_count?: number;
  warnings_ja?: string[];
  error_message_ja?: string;
  created_at?: string;
  updated_at?: string;
}

export interface OntologyPublishJob {
  id: string;
  revision_id: string;
  requested_etag?: string;
  finished_at?: string | null;
  profile_id?: string;
  status: "queued" | "materializing" | "validating" | "succeeded" | "failed";
  rdf_graph_name?: string;
  inferred_graph_name?: string;
  shacl_conforms?: boolean | null;
  shacl_report_artifact_id?: string;
  warnings_ja?: string[];
  error_code?: string;
  error_message_ja?: string;
}

/** SQL 生成が実際に使う ontology-context 検索(語彙+埋め込み+推論展開)の 1 ヒット。 */
export interface OntologyContextHit {
  node: OntologyNode;
  score: number;
  matched_terms: string[];
  sources: string[];
  /** "asserted" 以外は OWL2RL 推論で拡張されたノード。 */
  inference_source: string;
}

export interface OntologyContextSearchResult {
  profile_id: string;
  profile_view_id: string;
  ontology_revision_id: string;
  hits: OntologyContextHit[];
  nodes: OntologyNode[];
  edges: OntologyEdge[];
  mermaid: string;
  llm_markdown: string;
  context_hash: string;
}

export interface OntologyFinding {
  severity: string;
  message_ja?: string;
  message?: string;
  code?: string;
  definition_id?: string;
  field?: string;
}

export interface OntologyMarkdownState {
  draft_markdown: string;
  published_markdown: string;
  draft_revision: OntologyRevision | null;
  published_revision: OntologyRevision | null;
  draft_version?: number | null;
  published_version?: number | null;
  draft_etag: string;
  published_at?: string | null;
  published_findings?: OntologyFinding[];
  published_data_report?: Record<string, unknown> | null;
  published_diagnostics_available?: boolean;
}

export interface OntologyMarkdownDraftPatch {
  markdown: string;
  base_etag: string;
}

export interface OntologyProfileRecommendationCandidate {
  profile_id: string;
  profile_name: string;
  ontology_revision_id: string;
  score: number;
  matched_scenarios_ja: string[];
  matched_terms: string[];
  reasons_ja: string[];
}

export interface OntologyProfileRecommendation {
  id: string;
  question_hash: string;
  ontology_revision_id: string;
  candidates: OntologyProfileRecommendationCandidate[];
  selected_profile_id?: string;
  selected_revision_id?: string;
  confirmed_at?: string | null;
  expires_at: string;
}

export interface SqlArtifact {
  id?: string;
  intent_version?: number;
  ontology_revision_id?: string;
  sql?: string;
  raw_sql?: string;
  generated_sql?: string;
  sql_hash: string;
  generation_context_hash: string;
  semantic_graph?: SqlSemanticGraph | null;
  validation_report?: OntologyValidationReport | null;
  created_at?: string;
  generated_at?: string;
}

export type QuerySessionState =
  | "interpreting"
  | "awaiting_intent_confirmation"
  | "generating_sql"
  | "awaiting_sql_confirmation"
  | "executing"
  | "done"
  | "error"
  | "cancelled";

export interface QuerySessionExecutionBinding {
  session_id?: string;
  artifact_id: string;
  ontology_revision_id: string;
  intent_version: number;
  sql_hash: string;
  validation_hash: string;
  generation_context_hash: string;
}

export interface OntologyPerformanceCheck {
  available: boolean;
  total_cost?: number | null;
  estimated_cardinality?: number | null;
  full_table_scans?: string[];
  warning?: string;
}

export interface QuerySessionPreview {
  sql: string;
  executable_sql?: string;
  is_safe: boolean;
  row_limit: number;
  note?: string;
  engine?: "enterprise_ai_direct";
  engine_meta?: Record<string, OntologyJsonValue>;
  fallback_reason?: string;
  rewritten_question?: string;
  safety?: Record<string, OntologyJsonValue> | null;
  recommendations?: string[];
  repaired_sql?: string;
  optimization_hints?: string[];
  timing?: Record<string, OntologyJsonValue> | null;
}

export interface QuerySession {
  id: string;
  profile_id: string;
  profile_view_id?: string;
  ontology_revision_id: string;
  state?: QuerySessionState;
  status?: QuerySessionState;
  question?: string;
  original_question?: string;
  current_intent_version?: number;
  intents?: QuestionIntentGraph[];
  current_sql_artifact_id?: string | null;
  sql_artifacts?: SqlArtifact[];
  intent_confirmed_version?: number | null;
  sql_confirmation?: QuerySessionExecutionBinding | null;
  execution?: Record<string, OntologyJsonValue> | null;
  clarification_mode?: ClarificationMode;
  requirement?: SqlRequirementDraft | null;
  requirement_review_round?: number;
  requirement_applied_at?: string | null;
  requirement_state?: RequirementState | null;
  /** チャットの回答前の確認（#1442）。同じ会話の前のターンと、要件の検査に渡した会話の履歴。 */
  chat?: boolean;
  chat_previous_job_id?: string | null;
  conversation_history?: { question: string; sql: string; status: string }[];
  proposal_ids?: string[];
  suggested_question?: string | null;
  intent_version?: number;
  intent_graph?: QuestionIntentGraph;
  profile_ontology_view?: ProfileOntologyView | null;
  ontology_graph?: OntologyGraph | null;
  sql_artifact?: SqlArtifact | null;
  sql_semantic_graph?: SqlSemanticGraph | null;
  validation_report?: OntologyValidationReport | null;
  execution_binding?: QuerySessionExecutionBinding | null;
  result?: Record<string, OntologyJsonValue> | null;
  preview?: QuerySessionPreview | null;
  performance_check?: OntologyPerformanceCheck | null;
  proposals?: OntologyProposal[];
  created_at?: string;
  updated_at?: string;
  error_code?: string | null;
  error_message?: string | null;
  error_message_ja?: string | null;
  cancelled_at?: string | null;
}

export interface QuerySessionCreateRequest {
  question: string;
  profile_id: string;
  allowed_node_ids?: string[];
  allowed_edge_ids?: string[];
  allowed_objects?: {
    table_names: string[];
    columns: Record<string, string[]>;
  };
  profile_confirmation_token?: string;
  row_limit?: number;
  engine?: "enterprise_ai_direct";
  clarification_mode?: ClarificationMode;
  /**
   * チャットの回答前の確認（#1442。guided だけ）。業務プロファイルは画面で選んでいるので推薦の確認は無い。
   * `previous_job_id` は同じ会話の前のターン（その会話の前の質問と SQL を要件の検査に渡す）。
   */
  chat?: boolean;
  previous_job_id?: string | null;
  attachment_ids?: string[];
}

export interface ClarificationAnswerRequest {
  base_version: number;
  item_id: string;
  selected_candidate_ids: string[];
  free_text?: string;
}

export interface RequirementApplyRequest {
  base_version: number;
}

export interface QuerySessionIntentPatchRequest extends GraphPatch {}

export interface QuerySessionGenerateSqlRequest {
  base_version: number;
  intent_version: number;
  ontology_revision_id: string;
  confirm_intent: true;
}

export interface QuerySessionSqlConfirmationRequest extends QuerySessionExecutionBinding {
  confirm_sql: true;
}

export interface QuerySessionExecuteRequest extends QuerySessionExecutionBinding {
  confirm_sql: true;
}

export interface OntologyImprovementProposalRequest {
  base_revision_id: string;
  intent_version: number;
  patch?: GraphPatch;
  summary?: string;
}

export interface VisibleOntologyGraph extends OntologyGraph {
  total_node_count: number;
  total_edge_count: number;
  hidden_node_count: number;
  hidden_edge_count: number;
  hidden_node_kinds: Partial<Record<OntologyNodeKind, number>>;
}

/**
 * 関係カード下段に出す補足の種別。
 * join = Join 条件式 / physical = 物理対応名 / description = 関係の説明 / none = 補足なし。
 * contains(含む)や maps_to のように構造上 Join 条件を持たない関係でも空欄を出さないための区別。
 */
export type OntologyRelationshipDetailKind = "join" | "physical" | "description" | "none";

export interface OntologyRelationshipRow {
  edge_id: string;
  source_node_id: string;
  source_label: string;
  relationship_label: string;
  target_node_id: string;
  target_label: string;
  join_condition: string;
  detail_kind: OntologyRelationshipDetailKind;
  detail_text: string;
  validation_status: OntologyValidationStatus;
}

export type RelationshipSortKey = "source" | "relationship" | "target" | "status";
export type SortDirection = "asc" | "desc";

const QUERY_SESSION_STAGE_ORDER: QuerySessionState[] = [
  "interpreting",
  "awaiting_intent_confirmation",
  "generating_sql",
  "awaiting_sql_confirmation",
  "executing",
  "done",
  "error",
];

export function querySessionStageIndex(state: QuerySessionState): number {
  return QUERY_SESSION_STAGE_ORDER.indexOf(state);
}

export function querySessionState(session: QuerySession): QuerySessionState {
  return session.status ?? session.state ?? (session.error_code || session.error_message_ja ? "error" : "interpreting");
}

export function currentIntentForSession(session: QuerySession): QuestionIntentGraph | null {
  if (session.intent_graph) return session.intent_graph;
  if (!session.intents?.length) return null;
  const version = session.current_intent_version ?? session.intent_version;
  return session.intents.find((intent) => intent.version === version) ?? session.intents.at(-1) ?? null;
}

export function currentSqlArtifactForSession(session: QuerySession): SqlArtifact | null {
  if (session.sql_artifact) return session.sql_artifact;
  if (!session.sql_artifacts?.length) return null;
  if (session.current_sql_artifact_id) {
    const current = session.sql_artifacts.find((artifact) => artifact.id === session.current_sql_artifact_id);
    if (current) return current;
  }
  return session.sql_artifacts.at(-1) ?? null;
}

export function currentValidationForSession(session: QuerySession): OntologyValidationReport | null {
  return session.validation_report ?? currentSqlArtifactForSession(session)?.validation_report ?? null;
}

export function currentIntentVersionForSession(session: QuerySession): number {
  return session.current_intent_version ?? session.intent_version ?? currentIntentForSession(session)?.version ?? 0;
}

export function hasGraphPatchVersionConflict(baseVersion: number, currentVersion: number): boolean {
  return baseVersion !== currentVersion;
}

export function executionBindingForSession(session: QuerySession): QuerySessionExecutionBinding | null {
  if (session.execution_binding) return session.execution_binding;
  const artifact = currentSqlArtifactForSession(session);
  const validation = currentValidationForSession(session);
  const sqlHash = artifact?.sql_hash;
  const validationHash = validation?.validation_hash;
  const generationContextHash = artifact?.generation_context_hash;
  if (!artifact?.id || !sqlHash || !validationHash || !generationContextHash) return null;
  return {
    session_id: session.id,
    artifact_id: artifact.id,
    ontology_revision_id: session.ontology_revision_id,
    intent_version: currentIntentVersionForSession(session),
    sql_hash: sqlHash,
    validation_hash: validationHash,
    generation_context_hash: generationContextHash,
  };
}

export function boundedOntologyGraph(graph: OntologyGraph, maxVisibleNodes = 100): VisibleOntologyGraph {
  const safeLimit = Math.max(1, Math.min(100, Math.trunc(maxVisibleNodes) || 100));
  const nodes = graph.nodes.slice(0, safeLimit);
  const visibleNodeIds = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter(
    (edge) => visibleNodeIds.has(edge.source_node_id) && visibleNodeIds.has(edge.target_node_id)
  );
  const hiddenNodes = graph.nodes.slice(safeLimit);
  const hiddenNodeKinds = hiddenNodes.reduce<Partial<Record<OntologyNodeKind, number>>>((result, node) => {
    result[node.kind] = (result[node.kind] ?? 0) + 1;
    return result;
  }, {});
  return {
    revision_id: graph.revision_id,
    nodes,
    edges,
    total_node_count: graph.nodes.length,
    total_edge_count: graph.edges.length,
    hidden_node_count: hiddenNodes.length,
    hidden_edge_count: graph.edges.length - edges.length,
    hidden_node_kinds: hiddenNodeKinds,
  };
}

export function profileScopedOntologyGraph(
  view: ProfileOntologyView | null | undefined,
  fallback: OntologyGraph
): OntologyGraph {
  const graph = view?.graph ?? fallback;
  const scopedNodeIds = view?.node_ids ?? view?.selected_node_ids;
  const scopedEdgeIds = view?.edge_ids ?? view?.selected_edge_ids;
  if (scopedNodeIds === undefined && scopedEdgeIds === undefined) return graph;
  const allowedNodes = new Set(scopedNodeIds ?? []);
  const nodes = graph.nodes.filter((node) => allowedNodes.has(node.id));
  const visibleNodeIds = new Set(nodes.map((node) => node.id));
  const allowedEdges = new Set(scopedEdgeIds ?? []);
  const edges = graph.edges.filter(
    (edge) =>
      allowedEdges.has(edge.id)
      && visibleNodeIds.has(edge.source_node_id)
      && visibleNodeIds.has(edge.target_node_id)
  );
  return { revision_id: graph.revision_id, nodes, edges };
}

function nodeLabel(node: OntologyNode | undefined, fallback: string): string {
  return node?.business_name_ja || fallback;
}

function relationshipDetail(
  graph: OntologyGraph,
  edge: OntologyEdge,
  nodesById: Map<string, OntologyNode>
): { kind: OntologyRelationshipDetailKind; text: string } {
  const joinText = ontologyJoinConditionText(graph, edge);
  if (joinText) return { kind: "join", text: joinText };
  const description = (edge.description_ja ?? "").trim();
  if (description) return { kind: "description", text: description };
  const physical =
    ontologyPhysicalNodeLabel(nodesById.get(edge.target_node_id)) ||
    ontologyPhysicalNodeLabel(nodesById.get(edge.source_node_id));
  if (physical) return { kind: "physical", text: physical };
  return { kind: "none", text: "" };
}

export function ontologyRelationshipRows(graph: OntologyGraph): OntologyRelationshipRow[] {
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  return graph.edges.map((edge) => {
    const detail = relationshipDetail(graph, edge, nodesById);
    return {
      edge_id: edge.id,
      source_node_id: edge.source_node_id,
      source_label: nodeLabel(nodesById.get(edge.source_node_id), edge.source_node_id),
      relationship_label: edge.relationship_name_ja,
      target_node_id: edge.target_node_id,
      target_label: nodeLabel(nodesById.get(edge.target_node_id), edge.target_node_id),
      join_condition: detail.kind === "join" ? detail.text : "",
      detail_kind: detail.kind,
      detail_text: detail.text,
      validation_status:
        edge.validation_status ??
        (["published", "reviewed", "approved"].includes(edge.review_status ?? "")
          ? "passed"
          : "unreviewed"),
    };
  });
}

export function sortOntologyRelationshipRows(
  rows: OntologyRelationshipRow[],
  key: RelationshipSortKey,
  direction: SortDirection
): OntologyRelationshipRow[] {
  const valueForKey = (row: OntologyRelationshipRow): string => {
    if (key === "source") return row.source_label;
    if (key === "relationship") return row.relationship_label;
    if (key === "target") return row.target_label;
    return row.validation_status;
  };
  const multiplier = direction === "asc" ? 1 : -1;
  return rows.slice().sort((left, right) =>
    compareNames(valueForKey(left), valueForKey(right)) * multiplier
  );
}

function normalizeIntentConcept(value: IntentConcept | string): IntentConcept {
  return typeof value === "string" ? { name: value } : value;
}

function intentConceptName(concept: IntentConcept): string {
  return concept.name_ja || concept.name || concept.ontology_node_id || concept.node_id || "名称未設定";
}

function stableGraphId(prefix: string, value: string, index: number): string {
  const normalized = value
    .trim()
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
  return `${prefix}:${normalized || index}`;
}

export function intentGraphToOntologyGraph(intent: QuestionIntentGraph): OntologyGraph {
  if (intent.graph) return intent.graph;
  const rootId = `intent:v${intent.version}`;
  const nodes: OntologyNode[] = [
    {
      id: rootId,
      kind: "question_intent",
      business_name_ja: intent.rewritten_question || intent.question || "質問の解釈",
      review_status: "draft",
    },
  ];
  const edges: OntologyEdge[] = [];
  const appendConcepts = (
    values: Array<IntentConcept | string>,
    kind: "business_entity" | "metric" | "property",
    relationship: string,
    prefix: string
  ) => {
    values.map(normalizeIntentConcept).forEach((concept, index) => {
      const name = intentConceptName(concept);
      const id = concept.ontology_node_id || concept.node_id || stableGraphId(prefix, name, index);
      nodes.push({
        id,
        kind,
        business_name_ja: name,
        confidence: concept.confidence,
        review_status: "reviewed",
      });
      edges.push({
        id: `${rootId}:${prefix}:${index}`,
        source_node_id: rootId,
        target_node_id: id,
        relationship_name_ja: relationship,
        validation_status: "passed",
      });
    });
  };
  appendConcepts(intent.entities, "business_entity", "対象", "entity");
  appendConcepts(intent.metrics, "metric", "指標", "metric");
  appendConcepts(intent.dimensions, "property", "切り口", "dimension");
  intent.filters.forEach((filter, index) => {
    const field = filter.label_ja || filter.field || filter.property_node_id || "条件";
    const id = filter.id || stableGraphId("filter", field, index);
    nodes.push({
      id,
      kind: "sql_filter",
      business_name_ja: `${field} ${filter.operator} ${String(filter.value)}`,
      review_status: "draft",
    });
    edges.push({
      id: `${rootId}:filter:${index}`,
      source_node_id: rootId,
      target_node_id: id,
      relationship_name_ja: "条件",
      validation_status: "passed",
    });
  });
  (intent.ambiguities ?? []).forEach((ambiguity, index) => {
    const id = ambiguity.id || `ambiguity:${index}`;
    nodes.push({
      id,
      kind: "validation_finding",
      business_name_ja: ambiguity.message_ja || ambiguity.message || "解釈に確認が必要です",
      review_status: "draft",
      validation_status: ambiguity.blocking === false ? "warning" : "blocked",
    });
    edges.push({
      id: `${rootId}:ambiguity:${index}`,
      source_node_id: rootId,
      target_node_id: id,
      relationship_name_ja: "要確認",
      validation_status: ambiguity.blocking === false ? "warning" : "blocked",
    });
  });
  return { nodes, edges };
}

function normalizeSqlItem(value: SqlSemanticItem | string): SqlSemanticItem {
  return typeof value === "string" ? { expression: value } : value;
}

function normalizeSqlJoin(value: SqlSemanticJoin | string): SqlSemanticJoin {
  return typeof value === "string" ? { expression: value, condition: value } : value;
}

function sqlItemExpression(item: SqlSemanticItem): string {
  return (
    item.expression ||
    item.expression_sql ||
    item.query_sql ||
    item.source_sql ||
    item.qualified_name ||
    item.output_name ||
    item.name ||
    "SQL 要素"
  );
}

export function sqlSemanticGraphToOntologyGraph(sqlGraph: SqlSemanticGraph): OntologyGraph {
  if (sqlGraph.graph) return sqlGraph.graph;
  const rootId = "sql:artifact";
  const nodes: OntologyNode[] = [
    {
      id: rootId,
      kind: "sql_artifact",
      business_name_ja: "生成 SQL",
      review_status: sqlGraph.parse_status === "blocked" ? "draft" : "reviewed",
      validation_status: sqlGraph.parse_status === "blocked" ? "blocked" : "passed",
    },
  ];
  const edges: OntologyEdge[] = [];
  const appendItems = (
    values: Array<SqlSemanticItem | string>,
    kind: OntologyNodeKind,
    relationship: string,
    prefix: string
  ) => {
    values.map(normalizeSqlItem).forEach((item, index) => {
      const expression = sqlItemExpression(item);
      const id = item.id || stableGraphId(prefix, expression, index);
      nodes.push({
        id,
        kind,
        business_name_ja: item.alias || expression,
        review_status: "reviewed",
        metadata: item.lineage ? { lineage: item.lineage } : undefined,
      });
      edges.push({
        id: `${rootId}:${prefix}:${index}`,
        source_node_id: rootId,
        target_node_id: id,
        relationship_name_ja: relationship,
        validation_status: "passed",
      });
    });
  };
  appendItems(sqlGraph.ctes, "cte", "CTE", "cte");
  appendItems(sqlGraph.tables, "sql_table", "参照", "table");
  appendItems(sqlGraph.columns, "sql_column", "列", "column");
  appendItems(sqlGraph.filters, "sql_filter", "絞り込み", "filter");
  appendItems(sqlGraph.aggregates, "sql_aggregate", "集計", "aggregate");
  appendItems(sqlGraph.groups ?? sqlGraph.group_by ?? [], "sql_group", "粒度", "group");
  appendItems(sqlGraph.having, "sql_having", "集計後条件", "having");
  appendItems(sqlGraph.orders ?? sqlGraph.order_by ?? [], "sql_order", "並び順", "order");
  appendItems(sqlGraph.windows, "sql_window", "ウィンドウ", "window");
  sqlGraph.joins.map(normalizeSqlJoin).forEach((join, index) => {
    const expression =
      join.expression || join.condition || join.condition_sql || "JOIN";
    const id = join.id || stableGraphId("join", expression, index);
    nodes.push({
      id,
      kind: "sql_join",
      business_name_ja: expression,
      review_status: join.reviewed_path === false ? "draft" : "reviewed",
      validation_status: join.reviewed_path === false ? "blocked" : "passed",
    });
    edges.push({
      id: `${rootId}:join:${index}`,
      source_node_id: rootId,
      target_node_id: id,
      relationship_name_ja: join.join_type || "JOIN",
      validation_status: join.reviewed_path === false ? "blocked" : "passed",
      metadata: join.ontology_edge_id ? { ontology_edge_id: join.ontology_edge_id } : undefined,
    });
  });
  if (sqlGraph.limit != null) {
    const id = "sql:limit";
    nodes.push({
      id,
      kind: "sql_limit",
      business_name_ja: `上限 ${sqlGraph.limit} 件`,
      review_status: "reviewed",
    });
    edges.push({
      id: `${rootId}:limit`,
      source_node_id: rootId,
      target_node_id: id,
      relationship_name_ja: "件数制限",
      validation_status: "passed",
    });
  }
  return { nodes, edges };
}
