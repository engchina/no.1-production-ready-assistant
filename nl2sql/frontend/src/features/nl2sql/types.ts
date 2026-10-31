import type { ChatAttachmentApiReference, RunModelUsageRecord, RunUsageMeasurement } from "@production-ready/ui";

import type { OntologyGraph } from "./ontology/types";

export type Nl2SqlEngine = "enterprise_ai_direct";

export type JobStatus = "pending" | "running" | "done" | "error";
export type JobStepStatus = "pending" | "running" | "done" | "error" | "skipped";

export interface JobStepData {
  stage: string;
  status: JobStepStatus;
  elapsed_ms?: number | null;
  /** 段階の開始・終了の時刻（ISO 8601。#1145。古いジョブには無い）。 */
  started_at?: string | null;
  finished_at?: string | null;
}

export interface StageTiming {
  stage: string;
  elapsed_ms: number;
}

export interface EngineTiming {
  engine: string;
  elapsed_ms: number;
  status: "success" | "failed" | "skipped";
  error?: string;
}

export interface TimingEnvelope {
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  elapsed_ms?: number | null;
  stage_timings: StageTiming[];
}

export interface SchemaColumn {
  column_name: string;
  logical_name: string;
  data_type: string;
  nullable: boolean;
  comment: string;
  sample_values: string[];
  /** 列に関連付いた SQL ドメイン(OWNER.NAME)。SQL ドメインに対応したバージョンの dictionary から取得。無ければ空。 */
  domain_name?: string;
}

export interface SchemaTable {
  table_name: string;
  qualified_name?: string;
  logical_name: string;
  owner: string;
  table_type: string;
  comment: string;
  row_count?: number | null;
  columns: SchemaColumn[];
  constraints: string[];
  constraint_details?: Array<{
    constraint_name: string;
    constraint_type: "P" | "R" | "U" | "C";
    owner?: string;
    table_name: string;
    columns: string[];
    referenced_owner?: string | null;
    referenced_table?: string | null;
    referenced_columns: string[];
    delete_rule?: string;
    status?: string;
    deferrable?: string;
  }>;
}

export interface SchemaCatalog {
  refreshed_at: string;
  schema_fingerprint?: string;
  tables: SchemaTable[];
  current_owner?: string;
  excluded_oracle_maintained_count?: number;
  view_dependencies?: Array<{
    owner?: string;
    view_name: string;
    referenced_owner?: string;
    referenced_name: string;
    referenced_type?: string;
  }>;
}

export interface SchemaOwnerSummary {
  owner: string;
  is_current: boolean;
  table_count: number;
  view_count: number;
}

export interface SchemaOwnersData {
  current_owner: string;
  owners: SchemaOwnerSummary[];
  excluded_oracle_maintained_count: number;
}

export interface AllowedObjects {
  table_names: string[];
  columns: Record<string, string[]>;
}

export interface Nl2SqlProfile {
  id: string;
  name: string;
  category?: string;
  description: string;
  allowed_tables: string[];
  allowed_views: string[];
  glossary: Record<string, string>;
  sql_rules: string[];
  default_row_limit: number;
  safety_policy: string;
  few_shot_examples: Array<Record<string, string>>;
  /**
   * 回答前の確認（#1839）の方針。`confirm` は曖昧な質問を確認してから SQL を生成し、`assume` は確認せずに
   * 前提を示して生成する（チャットと Agent の MCP。SQL 生成の画面の「回答前の確認」のボタンは変えない）。
   */
  clarification_policy?: ClarificationPolicy;
  /** 確認の上限（往復。最初の確認を 1 回目に数える）。0 はシステムの既定。 */
  clarification_max_rounds?: number;
  archived: boolean;
  object_scope_version?: number;
  version?: number;
  etag?: string;
  updated_at?: string;
}

export interface ProfileSummary {
  id: string;
  name: string;
  category: string;
  description: string;
  archived: boolean;
  allowed_table_count: number;
  allowed_view_count: number;
  glossary_count: number;
  few_shot_count: number;
  version: number;
  etag: string;
  updated_at: string;
}

export interface ProfileSummaryPage {
  items: ProfileSummary[];
  next_cursor: string | null;
  total: number;
  change_token: number;
}

export interface ProfileUsageContext {
  id: string;
  name: string;
  category: string;
  description: string;
  allowed_tables: string[];
  allowed_views: string[];
  /** 回答前の確認の方針（#1839）。`assume` ならチャットは確認事項を聞かずに作る。 */
  clarification_policy?: ClarificationPolicy;
  archived: boolean;
  object_scope_version: number;
  version: number;
  etag: string;
  updated_at: string;
}

export interface SchemaCatalogHead {
  catalog_version: number;
  schema_fingerprint: string;
  refreshed_at: string;
  object_count: number;
  column_count: number;
  change_token: number;
  etag: string;
}

export interface SchemaObjectSummary {
  owner: string;
  object_name: string;
  object_type: string;
  logical_name: string;
  comment: string;
  row_count?: number | null;
  column_count: number;
  last_ddl_at: string;
}

export interface SchemaObjectPage {
  items: SchemaObjectSummary[];
  next_cursor: string | null;
  total: number | null;
  table_count?: number;
  view_count?: number;
  counts_included?: boolean;
  catalog_version: number;
}

export interface SchemaObjectDetail {
  table: SchemaTable;
  dependencies: NonNullable<SchemaCatalog["view_dependencies"]>;
  catalog_version: number;
  etag: string;
}

export type SchemaRefreshJobStatus = "pending" | "running" | "done" | "error";
export type SchemaRefreshJobMode = "full" | "targeted";

export interface SchemaRefreshTargetObject {
  owner: string;
  object_name: string;
  object_type?: "table" | "view" | "materialized_view" | "unknown";
  expected_state?: "present" | "absent" | "unknown";
}

export interface SchemaRefreshJob {
  job_id: string;
  status: SchemaRefreshJobStatus;
  mode?: SchemaRefreshJobMode;
  source?: string;
  target_objects?: SchemaRefreshTargetObject[];
  requires_full_refresh?: boolean;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  worker_id?: string;
  heartbeat_at?: string | null;
  lease_expires_at?: string | null;
  attempt?: number;
  phase?: "queued" | "scanning" | "fetching" | "persisting" | "done";
  processed_objects?: number;
  total_objects?: number;
  scanned_objects: number;
  changed_objects: number;
  deleted_objects: number;
  catalog_version: number;
  error_code: string;
}

export interface SchemaRefreshActiveJobData {
  active_job: SchemaRefreshJob | null;
}

export interface ProfileUpsertPayload {
  name: string;
  category?: string;
  description?: string;
  allowed_tables: string[];
  allowed_views: string[];
  glossary?: Record<string, string>;
  sql_rules?: string[];
  default_row_limit?: number;
  safety_policy: string;
  few_shot_examples?: Array<Record<string, string>>;
  clarification_policy: ClarificationPolicy;
  clarification_max_rounds: number;
}

/** 回答前の確認の方針（#1839）。 */
export type ClarificationPolicy = "confirm" | "assume";

export interface ProfileLearningMaterialImportData {
  profile_id: string;
  profile_name: string;
  mode: string;
  imported_terms: number;
  imported_rules: number;
  imported_examples: number;
  skipped_count: number;
  warnings: string[];
  profile: Nl2SqlProfile;
}

export interface LegacyLearningMaterialData {
  glossary: Record<string, string>;
  rules: string[];
  warnings?: string[];
}

export interface ProfileRecommendationCandidate {
  profile_id: string;
  profile_name: string;
  score: number;
  matched_terms: string[];
  allowed_tables: string[];
  category?: string;
}

export interface ProfileRecommendationData {
  recommended_profile_id: string;
  recommended_profile_name: string;
  recommended_profile_category?: string;
  confidence: number;
  confidence_threshold?: number;
  reason: string;
  rewritten_question: string;
  recommended_allowed_objects: AllowedObjects;
  candidates: ProfileRecommendationCandidate[];
  recommendation_source?: string;
  classifier_version?: string;
  category_scores?: Record<string, number>;
  warnings?: string[];
}

export interface ClassifierTrainingExample {
  id: string;
  category: string;
  text: string;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
  source: string;
  source_type: "file" | "feedback";
  source_history_id: string;
  created_at: string;
  updated_at: string;
}

export type ClassifierTrainingCandidateStatus =
  | "pending"
  | "added"
  | "already_covered"
  | "conflict"
  | "profile_missing"
  | "source_changed";

export interface ClassifierTrainingCandidate {
  history_id: string;
  question: string;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
  feedback_rating?: FeedbackRating | null;
  feedback_comment: string;
  created_at: string;
  status: ClassifierTrainingCandidateStatus;
  eligible: boolean;
  training_example_id: string;
  conflict_profile_ids: string[];
}

/**
 * offset 型の一覧の 1 ページ（backend の `pr_backend_core.schemas.Page`。#1891）。任意のページへ移れる一覧
 * （共通の `OffsetPagination`）で使う。
 */
export interface OffsetPage<T> {
  items: T[];
  /** 絞り込みを適用した全件。 */
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
}

/** 会話の履歴の 1 ページ。`history_days` は一覧を絞った期間（最後の更新が直近 N 日。0 は絞らない。#2340）。 */
export interface ChatHistoryOffsetPage<T> extends OffsetPage<T> {
  history_days?: number;
}

export interface ClassifierTrainingCandidatesData extends OffsetPage<ClassifierTrainingCandidate> {
  /** 絞り込み（状態・業務プロファイル・検索語）の前の全件（「N 件（全 M 件）」。#1891）。 */
  unfiltered_total: number;
  pending_count: number;
  added_count: number;
  attention_count: number;
}

export interface ClassifierFeedbackImportData {
  imported_count: number;
  skipped_count: number;
  total_examples: number;
  stale: boolean;
  results: Array<{
    history_id: string;
    status: string;
    training_example_id: string;
    profile_id: string;
    message: string;
  }>;
}

export interface ClassifierImportData {
  imported_count: number;
  skipped_count: number;
  total_examples: number;
  categories: string[];
  warnings: string[];
  examples: ClassifierTrainingExample[];
}

export interface ClassifierTrainingDataData {
  total_examples: number;
  categories: string[];
  warnings: string[];
  examples: ClassifierTrainingExample[];
}

export interface ClassifierStatusData {
  ready: boolean;
  trained: boolean;
  stale: boolean;
  classifier_version: string;
  updated_at: string;
  example_count: number;
  category_count: number;
  categories: string[];
  embedding_model: string;
  vector_dimension: number;
  persistence_mode: string;
  recommendation_source: string;
  metrics: Record<string, string | number>;
  trained_example_count: number;
  pending_change_count: number;
  warnings: string[];
}

export interface ClassifierPredictionCandidate {
  category: string;
  score: number;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
}

export interface ClassifierPredictionData {
  recommendation_source: string;
  classifier_version: string;
  predicted_category: string;
  confidence: number;
  candidates: ClassifierPredictionCandidate[];
  warnings: string[];
}

export interface RewriteData {
  original_question: string;
  rewritten_question: string;
  source: string;
  model: string;
  warnings: string[];
}

export interface SafetyReport {
  is_safe: boolean;
  is_select_only: boolean;
  row_limit_applied: number;
  blocked_reason: string;
  warnings: string[];
  referenced_tables: string[];
  referenced_columns: string[];
  /** 安全性の確認をしたか。旧い結果には無い。 */
  safety_checked?: boolean;
  /** 安全性の確認を省略した理由（省略しなかったときは空）。 */
  safety_check_skip_reason?: string;
}

export interface QueryResults {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  total: number;
  returned_count?: number | null;
  has_more?: boolean;
  truncated?: boolean;
  execution_context?: "deterministic" | "oracle_data_plane" | "admin_control_plane";
}

export interface Nl2SqlQuestionInterpretation {
  available: boolean;
  source: string;
  original_question: string;
  rewritten_question: string;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
  target_objects: string[];
  filters: string[];
  group_by: string[];
  order_by: string[];
  aggregations: string[];
  row_limit?: number | null;
  confidence: number;
  warnings: string[];
}

export interface Nl2SqlSqlInterpretation {
  available: boolean;
  source: string;
  summary: string;
  statement_type: string;
  tables: string[];
  columns: string[];
  joins: string[];
  filters: string[];
  aggregations: string[];
  group_by: string[];
  order_by: string[];
  limit?: number | null;
  /** 生成 SQL を業務向けに説明する決定論の処理手順(処理手順を表示 ON のとき非空)。 */
  logical_steps?: string[];
  /** 同じ処理手順を業務者向け/技術者向けで併記した構造化版。 */
  logical_step_details?: Nl2SqlLogicalStep[];
  semantic_graph: Record<string, unknown>;
  warnings: string[];
}

/** 処理手順 1 件の業務者向け(business)/技術者向け(technical)併記。 */
export interface Nl2SqlLogicalStep {
  kind?: string;
  business?: string;
  technical?: string;
}

/** SQL 論理構造 1 項目の業務者向け/技術者向け併記。 */
export interface Nl2SqlLogicalStructureItem {
  kind?: string;
  business?: string;
  technical?: string;
}

export interface Nl2SqlInterpretationArtifact {
  available: boolean;
  question: Nl2SqlQuestionInterpretation;
  sql: Nl2SqlSqlInterpretation;
  ontology_graph?: OntologyGraph | null;
  /** use_ontology_context のエコー。false のとき Ontology 接地確認を表示しない(未指定は互換で表示)。 */
  ontology_grounding_enabled?: boolean;
  /**
   * 接地確認を有効にしたが行わなかった理由。"no_published_ontology" は業務プロファイルに公開された
   * オントロジーが無いこと(ジョブはその場で同期・構築しない。#1168)。空・未指定は接地確認をした。
   */
  ontology_grounding_skip_reason?: "" | "no_published_ontology";
  warnings: string[];
}

export interface Nl2SqlResult {
  history_id?: string;
  engine: Nl2SqlEngine;
  engine_meta: Record<string, unknown>;
  fallback_reason: string;
  original_question: string;
  rewritten_question: string;
  generated_sql: string;
  executable_sql: string;
  explanation: string;
  safety: SafetyReport;
  recommendations: string[];
  repaired_sql: string;
  optimization_hints: string[];
  results: QueryResults;
  timing: TimingEnvelope;
  interpretation?: Nl2SqlInterpretationArtifact | null;
  /** 回答の注意書き（システム設定の文。SQL を実行して答えた job だけ。#2405）。 */
  notice?: string | null;
}

export interface GeneratedSqlPanelData {
  engine: Nl2SqlEngine;
  engine_meta: Record<string, unknown>;
  fallback_reason: string;
  generated_sql: string;
  executable_sql: string;
  explanation: string;
  safety: SafetyReport;
  recommendations: string[];
  repaired_sql: string;
  optimization_hints: string[];
  rewritten_question: string;
  interpretation?: Nl2SqlInterpretationArtifact | null;
}

export interface PreviewData {
  sql: string;
  is_safe: boolean;
  row_limit: number;
  note: string;
  engine: Nl2SqlEngine;
  engine_meta: Record<string, unknown>;
  fallback_reason: string;
  rewritten_question: string;
  executable_sql: string;
  safety?: SafetyReport | null;
  recommendations: string[];
  repaired_sql: string;
  optimization_hints: string[];
  timing?: TimingEnvelope | null;
}

export interface JobCreateData {
  job_id: string;
  status: JobStatus;
  created_at: string;
  steps: JobStepData[];
}

export interface JobData {
  /**
   * 処理の経過の trace id（#3226 の案 A）。span は共通の span の表にあり、処理の経過は
   * `GET /api/nl2sql/jobs/{job_id}/trace` で読む。worker が受け取る前は空。
   */
  trace_id?: string;
  /** 書けなかった span の数（1 以上なら「一部の処理だけを記録しています」）。 */
  trace_dropped_spans?: number;
  job_id: string;
  /** チャットの質問に添付したファイル（#2517）。 */
  attachments?: ChatAttachmentApiReference[];
  question?: string;
  /** 回答前の確認で確かめた質問の文（反映したジョブだけ。チャットの回答に出す。#1442）。 */
  requirement_question?: string;
  /** 回答前の確認で利用者が答えた内容（反映したジョブの、答えた・直した項目。#1839）。 */
  requirement_answers?: { prompt: string; answer: string }[];
  /** 回答前の確認を「確認せずに SQL を生成」で飛ばしたチャットのターン（#1839）。 */
  requirement_skipped?: boolean;
  profile_id?: string;
  conversation_id?: string;
  previous_job_id?: string | null;
  /** 生成と安全検査だけで実行しない（実行の権限が無い利用者のチャット・#1176 より前のチャット）。 */
  generation_only?: boolean;
  /** チャット（会話）のターンか（#1176）。 */
  chat?: boolean;
  /** 生成方法（#1145。チャットの段階の補足に出す）。 */
  engine?: Nl2SqlEngine;
  status: JobStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  elapsed_ms?: number | null;
  result?: Nl2SqlResult | null;
  error_message?: string | null;
  /** 機械判定用の失敗分類(例: SCHEMA_CATALOG_EMPTY)。表示は error_message が正本。 */
  error_code?: string | null;
  /** 失敗の技術的な詳細（例外・Oracle のエラーの元の文）。画面は「詳細」に畳んで出す（#1072）。 */
  error_detail?: string | null;
  warning_message?: string | null;
  timing?: TimingEnvelope | null;
  steps: JobStepData[];
  /**
   * 実行の回数（worker が claim するたびに増える。0 は未着手）。lease の切れたジョブを別の実行が引き継ぐと
   * 段階は初めからになる（#1358。チャットの段階はイベントの試行で作り直す）。
   */
  attempt?: number;
  /** チャットのターンの SQL を最後に実行したときの要約（行は保存しない。#1154）。 */
  last_execution?: SqlChatExecutionSummary | null;
  /**
   * 会話の持ち主以外（system_admin）が送ったターンの送り手の表示名（#1928）。持ち主が送ったターンは
   * null（または無し）、表示名を引けない利用者（削除済みなど）は空。
   */
  sender_display_name?: string | null;
}

/** チャットのターンの SQL を最後に実行したときの要約（#1154）。 */
export interface SqlChatExecutionSummary {
  status: "done" | "error";
  executed_at: string;
  elapsed_ms: number;
  row_count: number;
  column_count: number;
  has_more: boolean;
  error_code?: string | null;
  history_id: string;
  /**
   * ジョブの中で実行したとき（#1176）、結果の行を受け取れる期限（ISO 8601）。行は会話に残さず、
   * 画面が `POST /api/nl2sql/jobs/{job_id}/execution-result` で 1 回だけ受け取る。
   */
  result_expires_at?: string | null;
}

/**
 * `POST /api/nl2sql/jobs/{job_id}/execute`（#1154）と
 * `POST /api/nl2sql/jobs/{job_id}/execution-result`（#1176）の応答。
 */
export interface SqlChatExecuteData {
  job_id: string;
  status: "done" | "error";
  executed_at: string;
  elapsed_ms: number;
  executable_sql: string;
  results: QueryResults;
  /** 1 回の実行で取得する行数の上限（設定値）。 */
  row_limit: number;
  /** セルの文字数の上限（設定値）。 */
  max_cell_chars: number;
  /** 文字数の上限で文字を切ったセルがあるか。 */
  cells_truncated: boolean;
  error_message?: string | null;
  error_code?: string | null;
  error_detail?: string | null;
  history_id: string;
}

export interface HistoryItem {
  /** 実行したジョブと処理の経過の trace id（#3226 の案 A。処理の経過は `GET /api/nl2sql/history/{id}/trace`）。 */
  job_id?: string;
  trace_id?: string;
  trace_dropped_spans?: number;
  generation_only?: boolean;
  id: string;
  question: string;
  engine: Nl2SqlEngine;
  generated_sql: string;
  created_at: string;
  elapsed_ms?: number | null;
  generation_elapsed_ms?: number | null;
  engine_timings?: EngineTiming[];
  stage_timings?: StageTiming[];
  feedback_rating?: "good" | "bad" | null;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
  rewritten_question: string;
  executable_sql: string;
  safety_is_safe: boolean;
  result_row_count: number;
  result_columns: string[];
  feedback_comment: string;
  feedback_updated_at?: string;
  admin_feedback_rating?: FeedbackRating | null;
  admin_feedback_content?: string;
  admin_feedback_updated_at?: string;
  /** 実行した人（executor）。 */
  actor_user_uuid?: string;
  /** system_admin の応答だけに入るログイン ID・表示名（#1696）。 */
  actor_login_user_id?: string;
  actor_display_name?: string;
  /** 見ている利用者が実行した行か（#1954）。 */
  executed_by_me?: boolean;
  /** 実行した人の表示名（見る人を問わず入る。引けなければ空。#1954）。 */
  executor_display_name?: string;
  /** チャットの往復の会話の持ち主の表示名と、見ている利用者が持ち主か（会話の無い実行は null。#1954）。 */
  conversation_owner_display_name?: string | null;
  owned_by_me?: boolean | null;
  /** 実行のきっかけ（画面は空、MCP の入口は `mcp`、Agent の品質評価の Run は `evaluation`。#1986）。 */
  run_source?: "" | "mcp" | "evaluation";
  /** MCP の呼び出し元（外部の MCP クライアントの API キーの名前・Agent の Run の ID。#1986）。 */
  api_key_name?: string;
  agent_run_id?: string;
  /** 種類（チャットの往復 / SQL 生成 / MCP / 品質評価。きっかけと会話の持ち主で backend が決める。#1955・#1986）。 */
  kind?: "chat" | "query" | "mcp" | "evaluation" | null;
  /** チャットの往復の会話の ID（実行の詳細の「会話を開く」に使う。チャットでない実行は意味を持たない。#2343）。 */
  session_id?: string;
  /** 停止で終わった実行の行（#3348）。空・無しは SQL を実行した行。停止の行は SQL・実行結果・評価を持たない。 */
  run_status?: "" | "cancelled";
  /** 取り消した人と入口（停止の行だけ。backend の `HistoryCancellation`。#3348）。 */
  cancellation?: HistoryCancellation | null;
  /**
   * 実行のモデルの呼び出しの回数と利用量（ジョブの中の LLM の呼び出し。3 製品で共通の形。#3543）。この機能より前の行は
   * null（「未計測」。0 にしない）。計測できない呼び出しは数を作らず `unmeasured_sources` に出る。
   */
  usage?: RunModelUsageRecord | null;
  /** 一覧の「token」の列: 合計の token と計測の状態（記録の無い行はどちらも null）。 */
  total_tokens?: number | null;
  usage_measurement?: RunUsageMeasurement | null;
}

/** 実行履歴の「取消」（API キーの名前は見ている利用者が見られるキーだけ。見られなければ空。#3348）。 */
export interface HistoryCancellation {
  via: "screen" | "mcp" | "agent_run";
  /** 取り消した人を記録しているか（認証無効の期間の取消は false）。 */
  actor_recorded: boolean;
  /** 取り消した人の表示名（引けない利用者〔削除済み〕・記録の無い取消は空）。 */
  actor_display_name: string;
  by_me: boolean;
  api_key_name: string;
}

export interface HistoryData extends OffsetPage<HistoryItem> {
  /** 絞り込み（検索語・状態・種類・期間・実行者）の前の全件（「N 件（全 M 件）」。#1891・#1954）。 */
  unfiltered_total: number;
  /** 他の人が実行した行が見えるか（「実行者」の列と絞り込みを出す。#1955）。 */
  others_visible?: boolean;
  /** 種類の既定（品質評価以外）で見える行が 0 件のときだけ、隠している品質評価の行の数（空の状態の案内。#1986）。 */
  hidden_evaluation_total?: number;
}

export type FeedbackRating = "good" | "bad";

export interface FeedbackData {
  history_id: string;
  rating: FeedbackRating;
  saved: boolean;
  comment: string;
  feedback_content: string;
}

export interface FeedbackRecord extends HistoryItem {
  training_status: ClassifierTrainingCandidateStatus | "";
  training_example_id: string;
}

export interface FeedbackListData extends OffsetPage<FeedbackRecord> {
  /** 絞り込み（評価・業務プロファイル・検索語）の前の全件（「N 件（全 M 件）」。#1891）。 */
  unfiltered_total: number;
}

export interface FeedbackClearData {
  history_id: string;
  cleared: boolean;
}

export interface FeedbackIndexData {
  operation: string;
  status: string;
  executed: boolean;
  runtime: string;
  source_history_count: number;
  indexable_count: number;
  indexed_count: number;
  vector_dimension: number;
  vector_backend: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_configured: boolean;
  ddl: string[];
  warnings: string[];
  timing: TimingEnvelope;
}

export interface FeedbackVectorEntry {
  history_id: string;
  question: string;
  generated_sql: string;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
  feedback_rating?: FeedbackRating | null;
  feedback_comment: string;
  admin_feedback_rating?: FeedbackRating | null;
  admin_feedback_content?: string;
  admin_feedback_updated_at?: string;
  indexed: boolean;
  created_at: string;
}

export interface FeedbackEntriesData {
  items: FeedbackVectorEntry[];
  total: number;
  indexed_count: number;
  warnings?: string[];
}

export interface FeedbackSearchConfigData {
  similarity_threshold: number;
  match_limit: number;
}

export interface DemoLearningData {
  seeded_history_count: number;
  seeded_feedback_count: number;
  history_ids: string[];
  profile_ids: string[];
  message: string;
}

export interface SimilarHistoryItem {
  history_id: string;
  question: string;
  sql: string;
  profile_id: string;
  profile_name: string;
  score: number;
  reason: string;
}

export interface SimilarHistoryData {
  items: SimilarHistoryItem[];
  used_for_generation?: boolean;
  engine?: Nl2SqlEngine | null;
}

export type QualityEvaluationEngine = Nl2SqlEngine;
export type QualityEvaluationStatus =
  | "pending"
  | "running"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "cancelled";
export type QualityEvaluationVerdict =
  | "correct"
  | "incorrect"
  | "uncertain"
  | "not_analyzed";

export interface QualityEvaluationEngineCapability {
  engine: QualityEvaluationEngine;
  label: string;
  available: boolean;
  reason: string;
}

export interface QualityEvaluationCapabilities {
  engines: QualityEvaluationEngineCapability[];
  judge: {
    available: boolean;
    reason: string;
    provider: string;
  };
  limits: {
    max_file_bytes: number;
    max_cases: number;
    max_attempts: number;
    min_repeat_count: number;
    max_repeat_count: number;
    attempt_timeout_seconds: number;
  };
}

export interface QualityEvaluationEngineSummary {
  engine: QualityEvaluationEngine;
  total_attempts: number;
  generation_successes: number;
  generation_success_rate: number;
  correct: number;
  incorrect: number;
  uncertain: number;
  not_analyzed: number;
  normalized_sql_consistency: number;
  error_count: number;
}

/** 実行中の 1 つの試行（#2059。試行は上限つきの並行で実行する）。 */
export interface QualityEvaluationRunningAttempt {
  case_no: number;
  case_id: string;
  engine: QualityEvaluationEngine;
  repetition_no: number;
  started_at: string;
  deadline_at?: string | null;
}

export interface QualityEvaluationJobSummary {
  job_id: string;
  profile_id: string;
  profile_name: string;
  profile_category?: string;
  engines: QualityEvaluationEngine[];
  repeat_count: number;
  case_count: number;
  total_attempts: number;
  completed_attempts: number;
  success_count: number;
  error_count: number;
  status: QualityEvaluationStatus;
  current_case_id: string;
  current_engine?: QualityEvaluationEngine | null;
  current_repetition: number;
  current_attempt_started_at?: string | null;
  current_attempt_deadline_at?: string | null;
  /** 実行中の試行（開始の順）。current_* はこのうち最も前に始めた試行（#2059）。 */
  running_attempts?: QualityEvaluationRunningAttempt[];
  /** 試行を同時に実行する数の上限（#2059）。 */
  concurrency?: number;
  engine_summaries: QualityEvaluationEngineSummary[];
  error_message: string;
  heartbeat_at?: string | null;
  lease_expires_at?: string | null;
  attempt_no: number;
  attempt_timeout_seconds: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at: string;
}

export type QualityEvaluationJobPage = OffsetPage<QualityEvaluationJobSummary>;

export interface QualityEvaluationJudge {
  verdict: Exclude<QualityEvaluationVerdict, "not_analyzed">;
  confidence: number;
  summary: string;
  differences: string[];
  risks: string[];
  correction_suggestion: string;
}

export interface QualityEvaluationResult {
  result_id: string;
  job_id: string;
  case_no: number;
  case_id: string;
  excel_row: number;
  question: string;
  expected_sql: string;
  engine: QualityEvaluationEngine;
  repetition_no: number;
  generated_sql: string;
  normalized_sql: string;
  deterministic_analysis: {
    is_safe: boolean;
    is_select_only: boolean;
    safety_checked?: boolean;
    referenced_objects: string[];
    structure_summary: string;
    risk_findings: string[];
  };
  generation_elapsed_ms: number;
  judge_elapsed_ms: number;
  total_elapsed_ms: number;
  verdict: QualityEvaluationVerdict;
  judge?: QualityEvaluationJudge | null;
  generation_error: string;
  judge_error: string;
  created_at: string;
}

export type QualityEvaluationResultPage = OffsetPage<QualityEvaluationResult>;

export interface ReverseSqlData {
  question: string;
  explanation: string;
  referenced_tables: string[];
  sql_structure?: string;
  logical_structure?: string;
  logical_structure_items?: Nl2SqlLogicalStructureItem[];
  logical_steps?: string[];
  logical_step_details?: Nl2SqlLogicalStep[];
  source?: string;
  warnings?: string[];
}

export interface AnalyzeData {
  safety: SafetyReport;
  explanation: string;
  recommendations: string[];
  executable_sql: string;
  repaired_sql: string;
  optimization_hints: string[];
  structure_summary?: string;
  risk_level?: string;
  statement_type?: string;
  object_names?: string[];
  column_names?: string[];
  conditions?: string[];
  group_by?: string[];
  order_by?: string[];
  risk_findings?: string[];
  repair_candidates?: string[];
  operations?: string[];
  filters?: string[];
  joins?: string[];
  aggregations?: string[];
  llm_enhanced?: boolean;
  llm_warnings?: string[];
}

export interface CommentSuggestion {
  object_name: string;
  object_type: string;
  suggested_comment: string;
}

export interface CommentSuggestionData {
  suggestions: CommentSuggestion[];
  source?: string;
  warnings?: string[];
}

export interface CommentApplyItem {
  object_name: string;
  object_type: string;
  comment: string;
}

export interface CommentApplyStatement {
  object_name: string;
  object_type: string;
  comment: string;
  sql: string;
  status: string;
  error_message: string;
}

export interface CommentApplyData {
  executed: boolean;
  runtime: string;
  statements: CommentApplyStatement[];
  schema_refresh_job_id?: string;
  schema_refresh_required?: boolean;
  schema_refresh_reason_code?: string;
  warnings: string[];
  timing: TimingEnvelope;
}

export interface AnnotationSuggestion {
  object_name: string;
  object_type: string;
  annotation_name: string;
  annotation_value: string;
}

export interface AnnotationSuggestionData {
  suggestions: AnnotationSuggestion[];
  source: string;
  warnings: string[];
}

export interface AnnotationApplyItem {
  object_name: string;
  object_type: string;
  annotation_name: string;
  annotation_value: string;
}

export interface AnnotationApplyStatement {
  object_name: string;
  object_type: string;
  annotation_name: string;
  annotation_value: string;
  sql: string;
  status: string;
  error_message: string;
}

export interface AnnotationApplyData {
  executed: boolean;
  runtime: string;
  statements: AnnotationApplyStatement[];
  schema_refresh_job_id?: string;
  schema_refresh_required?: boolean;
  schema_refresh_reason_code?: string;
  warnings: string[];
  timing: TimingEnvelope;
}

export interface DiagnosticCheck {
  name: string;
  status: string;
  message: string;
}

export interface ProfileDeleteData {
  profile: Nl2SqlProfile;
}

export interface DbAdminObjectSummary {
  name: string;
  owner: string;
  qualified_name?: string;
  object_type: string;
  row_count?: number | null;
  comment: string;
}

export interface DbAdminObjectDetail extends DbAdminObjectSummary {
  columns: SchemaColumn[];
  constraints?: string[];
  ddl: string;
  warnings: string[];
}

export interface DbAdminObjectsData {
  runtime: string;
  items: DbAdminObjectSummary[];
  refreshed_at: string;
  warnings: string[];
}

export interface DbAdminObjectPage {
  runtime: string;
  owner: string;
  items: DbAdminObjectSummary[];
  total: number;
  table_count: number;
  view_count: number;
  counts_included?: boolean;
  next_cursor: string | null;
  refreshed_at: string;
  catalog_version: number;
  warnings: string[];
}

export interface DbAdminStatementResult {
  index: number;
  statement_type: string;
  status: string;
  sql: string;
  row_count?: number | null;
  message: string;
  elapsed_ms: number;
  error_message: string;
  error_code?: string;
}

export interface DbAdminExecuteData {
  executed: boolean;
  runtime: string;
  execution_context?:
    | "deterministic"
    | "oracle_data_plane"
    | "admin_control_plane";
  select_result?: QueryResults | null;
  statements: DbAdminStatementResult[];
  committed: boolean;
  rolled_back: boolean;
  schema_refresh_job_id?: string;
  schema_refresh_required?: boolean;
  schema_refresh_reason_code?: string;
  warnings: string[];
  timing: TimingEnvelope;
}

/** DB 管理チャット（#2264）の、実行の前の 1 文の分類（文ごとに実行できる。#2333）。 */
export interface DbAdminChatStatementPlan {
  index: number;
  statement_type: string;
  sql: string;
  /** 読み取り専用の SELECT / WITH…SELECT（確認語なしで実行できる）。 */
  select_only: boolean;
  implicit_commit: boolean;
  /** 方針で止める文・製品の object など、実行できない理由（実行できるなら空）。 */
  blocked_reason: string;
  /** データ・構造を変える文（止めない文）は確認語が要る。 */
  requires_confirmation: boolean;
  /** 確認語（ADMIN_EXECUTE、1 つの対象を消す DROP / TRUNCATE は対象の名前）。不要なら空。 */
  confirmation_target: string;
  /** 参照する object に無い列（#2352）。空でなければ実行できない（AI のエラー分析で直せる）。 */
  unknown_columns: string[];
  /** 列を確かめられなかった object など、止めない注意（#2352）。 */
  warnings: string[];
}

/**
 * DB 管理チャットの、実行の前の分類（サーバーが必ず分類する）。文ごとの分類は `statements`、ここの値は
 * 「すべて実行」（上から順に・最初のエラーで止める）の判定。
 */
export interface DbAdminChatPlan {
  sql: string;
  statements: DbAdminChatStatementPlan[];
  /** すべての文が読み取り専用の SELECT。 */
  read_only: boolean;
  /** 「すべて実行」に確認語が要る（変える文を含む）。 */
  requires_confirmation: boolean;
  /** 「すべて実行」の確認語。不要なら空。 */
  confirmation_target: string;
  implicit_commit: boolean;
  /** 止める文を含む（「すべて実行」はできない。ほかの文は 1 文ずつ実行できる）。 */
  blocked: boolean;
  blocked_reasons: string[];
}

export type DbAdminChatStatementStatus =
  | "succeeded"
  | "failed"
  | "blocked"
  | "not_run"
  | "confirmation_required";

/** 1 文の実行の結果（#2333）。`result` はその文だけの結果。実行しなかった文と確認語の待ちは空。 */
export interface DbAdminChatStatementOutcome {
  index: number;
  status: DbAdminChatStatementStatus;
  result: DbAdminExecuteData | null;
}

/** DB 管理チャットの実行の結果（1 文か「すべて実行」。#2333）。 */
export interface DbAdminChatExecuteData {
  mode: "statement" | "all";
  batch_id: string;
  executed: boolean;
  committed: boolean;
  rolled_back: boolean;
  /** 「すべて実行」で止まった文（最初のエラー）の番号。 */
  stopped_at: number | null;
  statements: DbAdminChatStatementOutcome[];
  schema_refresh_job_id?: string;
  schema_refresh_required?: boolean;
  schema_refresh_reason_code?: string;
  warnings: string[];
  timing: TimingEnvelope;
}

/**
 * ターンの 1 文の最後の実行の要約（#2319・#2333）。結果の行はチャットと同じく保存しない
 * （`result.select_result` は空。行数・列数・打ち切りだけ）。確認語は保存しない。
 */
export interface DbAdminChatStatementLastExecution {
  index: number;
  executed_at: string;
  mode: "statement" | "all";
  batch_id: string;
  confirmation_given: boolean;
  status: "succeeded" | "failed" | "blocked" | "not_run";
  row_count: number | null;
  column_count: number | null;
  has_more: boolean;
  outcome: string;
  result: DbAdminExecuteData | null;
}

/** ターンの最後の「すべて実行」の要約（#2333）。 */
export interface DbAdminChatRunAllSummary {
  executed_at: string;
  batch_id: string;
  confirmation_given: boolean;
  succeeded: boolean;
  statement_count: number;
  succeeded_count: number;
  stopped_at: number | null;
  committed: boolean;
  rolled_back: boolean;
  warnings: string[];
}

/** DB 管理チャットの保存したターン（#2319）。 */
export interface DbAdminChatTurn {
  turn_id: string;
  question: string;
  created_at: string;
  generated_at: string;
  explanation: string;
  warnings: string[];
  context_tables: string[];
  model: string;
  plan: DbAdminChatPlan;
  /** AI のエラー分析で直した SQL に置き換えると進む。 */
  revision: number;
  corrected: boolean;
  /** 文ごとの最後の実行の要約（今の SQL の文のもの。文の番号の順。#2333）。 */
  statement_executions: DbAdminChatStatementLastExecution[];
  last_run_all: DbAdminChatRunAllSummary | null;
  sender_user_uuid?: string | null;
  /** 会話の持ち主以外（system_admin）が送ったターンだけに付く送り手の表示名（引けなければ空）。 */
  sender_display_name?: string | null;
  /** 回答の注意書き（システム設定の文。#2405）。この機能より前のターンは null / 未指定。 */
  notice?: string | null;
  /**
   * 生成したときの処理の経過の trace id（#3226 の案 A）。span は共通の span の表にあり、処理の経過は
   * `GET .../turns/{turn_id}/trace` で読む。古いターンには無い。
   */
  trace_id?: string;
  trace_dropped_spans?: number;
}

/** DB 管理チャットの会話（#2319。チャットの会話と同じ見え方の項目）。 */
export interface DbAdminChatConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at?: string;
  /** 見ている利用者の会話か（system_admin が他の利用者の会話を見るときは false）。 */
  owned?: boolean;
  /** system_admin の一覧だけに付く利用者の表示名。 */
  actor_display_name?: string;
}

/** DB 管理チャットの会話の内容。 */
export interface DbAdminChatData {
  conversation: DbAdminChatConversation;
  turns: DbAdminChatTurn[];
}

/** 質問を送った結果（保存したターンと、その会話）。SQL は実行しない。 */
export interface DbAdminChatSendData {
  conversation: DbAdminChatConversation;
  turn: DbAdminChatTurn;
}

export interface DbAdminChatCorrectionAttempt {
  statement_index: number;
  revision: number;
  sql: string;
  error_code: string;
  error_summary: string;
  executed_at: string;
}

/** DB 管理チャットの失敗の分析と直した SQL。 */
export interface DbAdminChatAnalyzeData {
  previous_attempts: DbAdminChatCorrectionAttempt[];
  cause: string;
  fix: string;
  conclusion: string;
  corrected_plan: DbAdminChatPlan | null;
  source: string;
  model: string;
  warnings: string[];
}

export type SampleDataset = "hr" | "sales" | "inquiries";

export interface SampleDataInfo {
  dataset?: SampleDataset;
  runtime: string;
  profile_id: string;
  confirmation: string;
  objects: string[];
  /** sample object を作成する schema と、objects の所有者付き名前。旧 backend では未返却。 */
  owner?: string;
  object_refs?: Array<{ name: string; owner?: string; qualified_name?: string }>;
  imported_objects: string[];
  /** 同名だが構成がサンプル定義と異なり、利用者のものとみなしたオブジェクト。 */
  conflicting_objects?: string[];
  /** 旧名（SAMPLE_NL2SQL_ 接頭辞）で残っているサンプルオブジェクト。削除時に併せて削除される。 */
  legacy_objects?: string[];
  sql: Record<string, string[]>;
  warnings: string[];
}

export interface SampleDataMutationData {
  dataset?: SampleDataset;
  operation: string;
  step: "tables" | "views" | "data" | "all";
  runtime: string;
  executed: boolean;
  objects: string[];
  statements: DbAdminStatementResult[];
  warnings: string[];
  profile_id: string;
  schema_refresh_job_id?: string;
  schema_refresh_required?: boolean;
  schema_refresh_reason_code?: string;
  timing: TimingEnvelope;
}

export interface DbAdminImportTabularData {
  table_name: string;
  /** 取込先の schema（current schema）。旧 backend では未返却。 */
  owner?: string;
  qualified_name?: string;
  filename: string;
  sheet_name: string;
  mode: string;
  columns: CsvImportColumn[];
  row_count: number;
  executed: boolean;
  preview?: boolean;
  ddl: string;
  insert_sql: string;
  schema_refresh_job_id?: string;
  schema_refresh_required?: boolean;
  schema_refresh_reason_code?: string;
  warnings: string[];
  sample_rows: Array<Record<string, string | null>>;
  timing: TimingEnvelope;
}

export type DbAdminStatementPolicy =
  | "table_ddl"
  | "view_ddl"
  | "data_dml"
  | "comment_sql"
  | "annotation_sql"
  | "domain_sql";

export interface MetadataSqlTarget {
  owner?: string;
  object_name: string;
  object_type: "table" | "view" | "materialized_view";
}

export interface MetadataSqlSampleTarget extends MetadataSqlTarget {
  columns: string[];
}

export interface MetadataSqlSamplePayload {
  targets: MetadataSqlSampleTarget[];
  sample_limit: number;
}

export interface MetadataSqlSampleData {
  sample_text: string;
  sample_count: number;
  runtime: string;
  warnings: string[];
}

export type DomainOperation = "create" | "update" | "rebuild" | "delete";

export interface DomainAnnotation {
  name: string;
  value: string;
}

export interface DomainColumnRef {
  owner: string;
  table_name: string;
  column_name: string;
}

export interface DomainDefinition {
  owner: string;
  name: string;
  qualified_name?: string;
  domain_type: "single" | "multi_column" | "enumerated" | "flexible";
  data_type: string;
  strict: boolean;
  nullable: boolean;
  constraints: string[];
  display: string;
  order: string;
  annotations: DomainAnnotation[];
  columns: DomainColumnRef[];
}

export interface DomainInventoryPayload {
  targets: MetadataSqlTarget[];
}

export interface DomainInventoryData {
  domains: DomainDefinition[];
  domain_text: string;
  runtime: string;
  warnings: string[];
}

export interface MetadataSqlGeneratePayload {
  targets: MetadataSqlTarget[];
  structure_text: string;
  primary_key_text: string;
  foreign_key_text: string;
  sample_text: string;
  extra_text: string;
  /** ドメイン管理だけが送る。 */
  operation?: DomainOperation;
  domain_text?: string;
  domains?: DomainDefinition[];
}

export interface MetadataSqlGenerateData {
  sql: string;
  source: string;
  warnings: string[];
  /** OCI Enterprise AI で作れず、規則ベース（名前をそのまま入れた SQL）にしたか（#1719）。 */
  fallback?: boolean;
  /** 規則ベースにした表（一部の組だけ失敗した場合。空 = 全部）。 */
  fallback_objects?: string[];
  /** 利用者向けの理由（1 文）。 */
  fallback_reason?: string;
  /** 「詳細」に出す失敗の元の文（理由・出力の上限・使った token）。 */
  fallback_detail?: string;
  timing: TimingEnvelope;
}

export interface DbAdminDataPreviewData {
  runtime: string;
  sql: string;
  results: QueryResults;
  warnings: string[];
}

export interface DbAdminCsvUploadData {
  table_name: string;
  filename: string;
  mode: string;
  matched_columns: string[];
  unmatched_csv_columns: string[];
  row_count: number;
  success_count: number;
  error_count: number;
  row_errors: string[];
  hint: string;
  executed: boolean;
  runtime: string;
  sample_rows: Array<Record<string, string | null>>;
  warnings: string[];
  timing: TimingEnvelope;
}

export type DbAdminJoinWherePromptProfile = "sql_structure";

export interface DbAdminJoinWhereData {
  join_text: string;
  where_text: string;
  source: string;
  warnings: string[];
  prompt_profile: DbAdminJoinWherePromptProfile;
  structure_markdown?: string;
}

export interface AdminFeedbackReviewData {
  history_id: string;
  rating: FeedbackRating;
  saved: boolean;
  feedback_content: string;
  similar_history_publish?: SimilarHistoryPublishData | null;
}

export interface SimilarHistoryPublishData {
  history_id: string;
  status: "published" | "unpublished" | "skipped" | "warning";
  runtime: string;
  executed: boolean;
  table_name: string;
  index_name: string;
  warnings: string[];
}

export interface CsvImportColumn {
  source_name: string;
  column_name: string;
  data_type: string;
  nullable: boolean;
}
