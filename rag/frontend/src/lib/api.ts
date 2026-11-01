import type { TraceRecordingSettingsData } from "@production-ready/system-settings";
/**
 * バックエンド API クライアント。
 *
 * - レスポンスは共通エンベロープ `ApiResponse<T>`（snake_case）。
 * - `/api/*` は Vite dev/preview proxy または本番で利用者が置く reverse proxy でバックエンドへ転送される。
 * - 型はバックエンドの Pydantic スキーマ（snake_case）にそのまま対応させる。
 */

import { appPath } from "./base-path";
import { t } from "./i18n";
import {
  ApiTransportError,
  httpApiErrorPresentation,
  isAbortError,
  toApiTransportError,
  type ApiErrorDetail,
  type ApiErrorDetailLabels,
  type ApiErrorPresentable,
  type ApiErrorPresentation,
  type ApiTransportRequest,
  type RunModelUsageRecord,
} from "@production-ready/ui";
// Cookie セッションの CSRF と 401 / 403 の通知は3製品共通（platform の共有パッケージ。#220 / #214）。
import { csrfHeader, notifyAuthResponse, notifyAuthStatus } from "@production-ready/system-settings";
import type { BaseCurrentUser } from "@production-ready/system-settings";

// OCI 認証 API の型は platform の共有パッケージが正本（#100）。
// モデル設定の API 型は3製品共通（platform の共有パッケージ。#103）。
// データベース設定の API 型は3製品共通（platform の共有パッケージ。#108）。
export type {
  DatabaseConnectionSecurity,
  DatabaseConnectionTestResult,
  DatabaseConnectionTestStatus,
  DatabasePasswordRevealData,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
} from "@production-ready/system-settings";
import type {
  DatabaseConnectionTestResult,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
} from "@production-ready/system-settings";
export type {
  EnterpriseAiConfiguredModel,
  EnterpriseAiModelSettings,
  EnterpriseAiVlmInputMode,
  EnterpriseAiModelKind,
  ModelSettingsData,
  ModelSettingsPayload,
  ModelSettingsSecretSource,
  ModelSettingsTestRequest,
  ModelSettingsTestResult,
  ModelSettingsTestStatus,
  ModelSettingsTestTargetType,
} from "@production-ready/system-settings";
import type {
  ModelSettingsData,
  ModelSettingsPayload,
  ModelSettingsTestRequest,
  ModelSettingsTestResult,
} from "@production-ready/system-settings";

// 回答の注意書き API の型は platform の共有パッケージが正本（#2405）。
import type {
  AnswerNoticeSettingsData,
  AnswerNoticeSettingsUpdate,
  ChatAttachmentSettingsData,
  ChatAttachmentSettingsUpdate,
} from "@production-ready/system-settings";
import type {
  ChatAttachmentApiItem,
  ChatAttachmentApiLimits,
  ChatAttachmentApiReference,
} from "@production-ready/ui";

// アップロード保存先 API の型は platform の共有パッケージが正本（#97）。
export type {
  UploadStorageSettingsData,
  UploadStorageSettingsUpdate,
} from "@production-ready/system-settings";
import type {
  UploadStorageSettingsData,
  UploadStorageSettingsUpdate,
} from "@production-ready/system-settings";

/** 削除 transaction と保存先の後始末を待つ。通信不明時は文書の残存を別途確認する。 */
export const DOCUMENT_DELETE_TIMEOUT_MS = 120_000;

export const API_REQUEST_TIMEOUT_MS = resolveTimeoutMs(
  import.meta.env.VITE_API_TIMEOUT_MS,
  30_000,
);

/**
 * 回答を LLM で生成する `POST /api/search`（非ストリーム）の timeout（#375）。backend は回答生成を
 * `RAG_ANSWER_TIMEOUT_SECONDS`（上限 600 秒）で打ち切って 504 と理由（時間切れになった工程）を返すので、
 * 画面はそれより 30 秒長く待つ。SSE（`/api/search/stream`・チャットの送信）は画面で打ち切らない。
 */
export const ANSWER_GENERATION_TIMEOUT_MS = 630_000;

/**
 * 品質評価の job の状態を取得する間隔（#390）。評価は job（投入 → 状態の取得 → 結果）で動き、
 * 投入と状態の取得は通常の API の timeout に収まる。
 */
export const EVALUATION_JOB_POLL_INTERVAL_MS = 2_000;

/** チャットが会話の回答の保存有無を一度に引き当てる trace_id の上限（backend と同じ）。 */
export const ANSWER_TRACE_ID_FILTER_MAX = 100;

/** DB 停止時に warning_messages を併せて返す閲覧系レスポンス。 */
export type Degradable<T> = T & { warning_messages: string[] };
export type JsonValue =
  string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type FileStatus =
  | "UPLOADED"
  | "PREPROCESSING"
  | "PREPROCESSED"
  | "INGESTING"
  | "REVIEW"
  | "CHUNKING"
  | "CHUNKED"
  | "INDEXING"
  | "INDEXED"
  | "ERROR";
export type SearchMode = "hybrid" | "vector" | "keyword";
export type KnowledgeBaseStatus = "ACTIVE" | "ARCHIVED";
export type CitationFeedbackRating = "helpful" | "not_helpful";
export type CitationFeedbackReason =
  | "incorrect"
  | "incomplete"
  | "missing_evidence"
  | "not_relevant"
  | "answer_untrusted"
  | "missing_knowledge"
  | "outdated_source"
  | "ambiguous_question";
export type FeedbackTargetType = "answer" | "citation";
export type FeedbackSourceSurface = "search" | "chat";
export type SourceModality =
  "pdf" | "image" | "text" | "html" | "email" | "office" | "unknown";
export type SourcePreviewKind =
  "pdf" | "image" | "text" | "html" | "email" | "office" | "unsupported";
export type IngestionJobStatus =
  "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED" | "CANCELLED";
/** 評価の失敗理由（#591）。保存済みの結果には削除した理由が残るため、表示は string で受ける。 */
export type EvaluationFailureReason =
  | "retrieval_miss"
  | "partial_recall"
  | "unexpected_answer"
  | "unexpected_refusal"
  | "answer_keyword_miss"
  | "low_groundedness"
  | "unsupported_claim"
  | "claim_unassessed"
  | "missing_content"
  | "answer_failed"
  | "answer_evaluation_error"
  | "unexpected_handling"
  | "step_missing"
  | "forbidden_action"
  | "condition_missing"
  | "evidence_miss"
  | "known_condition_reasked"
  | "screen_missing"
  | "wrong_screen"
  | "off_target"
  | "case_error";
/** 評価の指標（#591）。検索・根拠・回答の 3 つの観点の 9 つと、業務支援の対応の 4 つ（#1231）。 */
export type EvaluationMetricName = "required_evidence_recall" | "mrr" | "answer_correctness" | "requirement_coverage" | "claim_support_rate" | "response_decision_accuracy";

/** 回答の対応（#1231）。評価のケースは受け入れる対応を複数持てる。 */
export type EvaluationOutcome =
  | "answered"
  | "conditional"
  | "needs_clarification"
  | "needs_environment_data"
  | "needs_human"
  | "insufficient_evidence";

export type ParserAdapterBackend = "local" | "docling" | "unstructured";
export type ParserAdapterBackendName = "docling" | "unstructured";
export type ParserAdapterStatus =
  "active" | "available" | "disabled" | "ignored" | "missing";
export type ParserAdapterScoreBackend = "local" | ParserAdapterBackendName;
export type ParserAdapterScoreStatus =
  "recommended" | "eligible" | "available" | "disabled" | "ignored" | "missing";
export type ParserAdapterSourceKind =
  "pdf" | "image" | "office" | "html" | "email" | "text" | "unknown";

export interface ApiResponse<T> {
  data: T | null;
  error_messages: string[];
  warning_messages: string[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
}

/** 会話の履歴の 1 ページ。`history_days` は一覧を絞った期間（最後の更新が直近 N 日。0 は絞らない。#2340）。 */
export interface ChatHistoryPage<T> extends Page<T> {
  history_days?: number;
}

// --- 認証（platform の共通認証。#214） ---

/**
 * `GET /api/auth/me` などが返すログイン中の利用者。`permissions` は implies を展開済み。
 * `allowed_*_ids` が null なら制限なし（SYSTEM_ADMIN・`rag.*.manage`・ローカル DEBUG）。
 */
export interface CurrentUser extends BaseCurrentUser {
  allowed_search_answer_profile_ids: string[] | null;
  allowed_knowledge_base_ids: string[] | null;
}

/** ロール（共通のロール項目に RAG の権限と対象範囲を足したもの）。 */
export interface SecurityRole {
  role_id: string;
  role_code: string;
  display_name: string;
  description: string;
  is_built_in: boolean;
  archived: boolean;
  version: number;
  permissions: string[];
  search_answer_profile_ids: string[];
  knowledge_base_ids: string[];
}

/** 権限管理で選べる対象（検索・回答プロファイル・ナレッジベース）。 */
export interface AccessTarget {
  id: string;
  name: string;
  status: string;
  description: string | null;
}

/** 権限管理の対象の種類（`GET /api/security/access-targets/{kind}`。#608）。 */
export type AccessTargetKind = "search-answer-profiles" | "knowledge-bases";

/** 権限管理の対象の候補の 1 ページ（検索とページング。#608）。 */
export interface AccessTargetPage {
  items: AccessTarget[];
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
}

/** 権限管理画面の保存（`PUT /api/security/roles/{role_id}/access`）。 */
export interface RoleAccessUpdate {
  role_id: string;
  version: number;
  permissions: string[];
  search_answer_profile_ids: string[];
  knowledge_base_ids: string[];
}

export type DatabaseAvailability =
  "ok" | "not_configured" | "unreachable" | "setup_required";

export interface DatabaseStatusData {
  status: DatabaseAvailability;
  check: string;
  detail: string | null;
  schema_status: SystemTableSchemaStatus | null;
}

// --- ドキュメント ---
export interface KnowledgeBaseRef {
  id: string;
  name: string;
}

export interface SourceProfile {
  original_file_name: string;
  sanitized_file_name: string;
  extension: string | null;
  content_type: string;
  inferred_content_type: string | null;
  file_size_bytes: number;
  content_sha256: string;
  modality: SourceModality;
  parser_profile: string;
  parser_backend: string;
  parser_version: string;
  preview_kind: SourcePreviewKind;
  text_charset: string | null;
  duplicate_of_document_id: string | null;
  unsupported_reason: string | null;
  quality_status: "ready" | "warning" | string;
  quality_warnings: string[];
}

export type IngestionJobPhase = "PREPROCESS" | "EXTRACT" | "CHUNK" | "INDEX";

/**
 * 取込の job が worker の空きを待っている（順番待ち）ことの情報（#2026）。一覧と詳細で同じ値。
 * 「人の確認が要る（確認待ち）」と分けて出す。
 */
export interface IngestionQueueWait {
  phase: IngestionJobPhase;
  /** 待っている job。抽出の完了から自動進行の job の投入までの猶予の間（#733）は null。 */
  job_id: string | null;
  queued_at: string | null;
  /** worker が claim する順（FIFO）で前にある、claim できる QUEUED の job の数。猶予の間は null。 */
  jobs_ahead: number | null;
  /** 同時に処理できる job の数（`RAG_INGESTION_QUEUE_WORKER_CONCURRENCY`）。 */
  concurrency: number;
}

export interface DocumentElementTextEdit {
  element_id: string;
  text: string;
  /** 編集を始めたときの文。今の文と違えば、ほかの保存が先に変えていたため 409（#2616）。 */
  base_text: string;
}

export interface DocumentTableCellTextEdit {
  table_id: string;
  row: number;
  col: number;
  text: string;
  /** 編集を始めたときの文。今の文と違えば、ほかの保存が先に変えていたため 409（#2616）。 */
  base_text: string;
}

export interface DocumentReviewEditsRequest {
  element_edits?: DocumentElementTextEdit[];
  table_cell_edits?: DocumentTableCellTextEdit[];
}

export interface DocumentApproveRequest extends DocumentReviewEditsRequest {
  raw_text?: string | null;
}

export interface IngestionJob {
  id: string;
  document_id: string;
  /** 文書のファイル名。一覧・取得の応答だけが持つ（作成直後の応答などでは null。#306）。 */
  document_file_name?: string | null;
  recipe_id: string | null;
  recipe_revision: number | null;
  status: IngestionJobStatus;
  phase: IngestionJobPhase;
  parser_profile: string;
  quality_warnings: string[];
  skip_reason: string | null;
  error_message: string | null;
  attempt_count: number;
  max_attempts: number;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** 実行中の抽出の段（解析・図表の説明）の進捗（#1999）。無い・古い backend は null / 省略。 */
  progress?: IngestionJobProgress | null;
}

export type IngestionParseSegmentStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

/**
 * PDF の段（ページの範囲）1 つの解析の状態（#2001）。worker は抽出セグメントの行と同じ時に
 * 書き換え、段の帯・「抽出: N / M ページ」・段のカードはこの記録から作る。
 */
export interface IngestionParseSegmentProgress {
  segment_id: string;
  page_start: number;
  page_end: number;
  status: IngestionParseSegmentStatus;
  /** 解析中の段の中で終わったページの数。 */
  completed: number;
  /** 前の試行で成功した段の抽出結果を使った（解析し直していない）。 */
  reused: boolean;
}

/**
 * 抽出の中の解析の段（Docling などの解析サービス）のページの進捗（#1999・#2001）。
 * PDF を段ごとに解析するときは `segments` に段ごとの状態を持ち、`completed` / `total` は
 * 解析中の段の中のページの数（終わったら原本全体のページ数）。
 */
export interface IngestionParseProgress {
  engine: string | null;
  unit: "page";
  completed: number;
  failed: number;
  /** 解析サービスがページ数を返すまで null。 */
  total: number | null;
  finished: boolean;
  /** 段ごとの状態（段に分けずに解析するときは空・省略）。 */
  segments?: IngestionParseSegmentProgress[];
}

/** 抽出の中の図・表の説明（Vision）の段の件数の進捗（#1999）。 */
export interface IngestionVisionProgress {
  phase: string | null;
  succeeded: number;
  failed: number;
  skipped: number;
  running: number;
  pending: number;
  /** 見つかった対象の数（表は図の後に見つかるため、total_final が偽の間は増えうる）。 */
  total_known: number;
  total_final: boolean;
  current_page: number | null;
  current_kind: "picture" | "table" | null;
  finished: boolean;
  /**
   * 段ごとの解析と並べて説明するとき（#2002）: 今説明している段のページの範囲と、説明の終わった段の数・
   * 段の数（並べないときは 0 / 省略）。
   */
  segment_page_start?: number | null;
  segment_page_end?: number | null;
  segments_completed?: number;
  segments_total?: number;
}

/** 取込の段の AI の呼び出しの状態（待ちの理由・並行の数。#2002）。 */
export interface IngestionCallProgress {
  stage:
    | "vision"
    | "heading_levels"
    | "field_extraction"
    | "navigation_summary"
    | "raptor_summary"
    | "embedding"
    | "rerank";
  succeeded: number;
  failed: number;
  retries: number;
  total: number | null;
  /** 今の同時の呼び出しの数（429 で減らし、成功が続くと max_concurrency まで戻す）。 */
  concurrency: number;
  max_concurrency: number;
  in_flight: number;
  /**
   * 待っている理由（rate_limited = API の流量の制限、breaker_open = 失敗が続いたための一時停止、
   * final_retry = 失敗した件の最後の再試行の前、ほかはエラーの種類ごとの再試行の待ち）。
   */
  waiting_reason: string | null;
  waiting_seconds: number | null;
  finished: boolean;
}

export interface IngestionJobProgress {
  stage: "parse" | "vision";
  parse: IngestionParseProgress | null;
  vision: IngestionVisionProgress | null;
  /** 今の（最後の）段の AI の呼び出しの状態（#2002）。 */
  calls?: IngestionCallProgress | null;
  /** worker が最後に進捗を書いた時刻。 */
  updated_at: string;
}

export type LanguagePackRelation = "current" | "equivalent" | "different" | "unrecorded";
export interface LanguagePackRecipeUse {
  recipe_id: string | null;
  chunk_set_id: string | null;
  release_id: string | null;
  version: number | null;
  ingest_hash: string | null;
  relation: LanguagePackRelation;
  built_at: string | null;
  job_id: string | null;
  target_release_id: string | null;
}
export interface LanguagePackReprocessingTarget {
  document_id: string;
  file_name: string;
  recipe_id: string;
  slot_no: number;
  config_revision: number;
  used_release_id: string | null;
  used_ingest_hash: string | null;
  relation: LanguagePackRelation;
}
export interface LanguagePackReprocessingTargets {
  current_release: LanguagePackRelease;
  targets: LanguagePackReprocessingTarget[];
  unavailable_count: number;
}
export interface LanguagePackReprocessingResult {
  items: { document_id: string; recipe_id: string; job_id: string | null; outcome: "accepted" | "rejected"; reason: string | null }[];
  accepted_count: number;
  rejected_count: number;
}

export interface LanguagePackDocumentUse {
  current_release_id: string;
  current_version: number;
  recipes: LanguagePackRecipeUse[];
}

export interface DocumentSummary {
  language_pack?: LanguagePackDocumentUse | null;
  id: string;
  file_name: string;
  status: FileStatus;
  category_name: string | null;
  content_type: string | null;
  file_size_bytes: number | null;
  content_sha256: string | null;
  duplicate_of_document_id: string | null;
  uploaded_at: string;
  indexed_at: string | null;
  knowledge_bases: KnowledgeBaseRef[];
  source_profile: SourceProfile | null;
  /** 検索対象のレシピの派生情報レイヤーに、作り直しが必要なものがあるか（一覧だけが返す。#550）。 */
  layers_rebuild_required?: boolean;
  /**
   * 待機中・実行中の取込 job があるか（一覧だけが返す。#2012）。投入直後の job は worker が拾うまで
   * 状態が変わらないため、一覧の定期の取り直しの判定に使う。
   */
  ingestion_active?: boolean;
  /** 一覧に出す状態（レシピ1）の取込の job が順番待ちのとき（#2026）。一覧の API だけが埋める。 */
  queue_wait?: IngestionQueueWait | null;
  /** この文書を置き換えた新しい版の文書（#1248）。null は今有効な版。 */
  superseded_by_document_id?: string | null;
  /** 新しい版の文書名（利用者が見られる文書のときだけ）。 */
  superseded_by_file_name?: string | null;
  superseded_at?: string | null;
  /** 新しい版に置き換えた文書（旧版）か。旧版は既定で回答の検索の対象から外れる。 */
  is_superseded?: boolean;
}

/** 文書を置き換えた新しい版の設定・解除（#1248）。null で解除。 */
export interface DocumentSupersededByRequest {
  superseded_by_document_id: string | null;
}

export interface DuplicateDocumentRef {
  id: string;
  file_name: string;
  status: FileStatus;
  uploaded_at: string;
  indexed_at: string | null;
}

export interface DocumentPreprocessArtifact {
  derivation_id: string;
  profile: string;
  converted: boolean;
  converter_name: string | null;
  converter_version: string | null;
  source_content_type: string | null;
  source_sha256: string | null;
  object_storage_path: string | null;
  content_type: string | null;
  sha256: string | null;
  file_name: string;
  page_map: Record<string, number>;
  warnings: string[];
}

export interface DocumentElement {
  kind: string;
  text: string;
  order: number;
  element_id?: string | null;
  parent_id?: string | null;
  content_kind?: string | null;
  source_parser?: string | null;
  page_number?: number | null;
  bbox?: number[] | null;
  section_path?: string[];
  confidence?: number | null;
  metadata?: Record<string, string | number | boolean | null>;
}

export interface ExtractionPage {
  page_number: number;
  label?: string | null;
  width?: number | null;
  height?: number | null;
  rotation?: number | null;
  element_ids: string[];
  metadata?: Record<string, string | number | boolean | null>;
}

export interface ExtractionTableCell {
  row: number;
  col: number;
  text: string;
  row_span: number;
  col_span: number;
  page_number?: number | null;
  bbox?: number[] | null;
  confidence?: number | null;
  metadata?: Record<string, string | number | boolean | null>;
}

export interface ExtractionTable {
  table_id: string;
  element_id?: string | null;
  page_number?: number | null;
  caption?: string | null;
  cells: ExtractionTableCell[];
  metadata?: Record<string, string | number | boolean | null>;
}

export interface ExtractionAsset {
  asset_id: string;
  kind: string;
  object_path?: string | null;
  page_number?: number | null;
  bbox?: number[] | null;
  alt_text?: string | null;
  /** 図表 VLM 要約(有効時のみ)。 */
  summary?: string | null;
  metadata?: Record<string, string | number | boolean | null>;
}

/** 章節 navigation tree のノード(ナビゲーション要約 有効時は summary 付き)。 */
export interface DocumentNavigationNode {
  section_id: string;
  title: string;
  section_path: string[];
  depth: number;
  parent_section_id: string | null;
  page_start: number | null;
  page_end: number | null;
  summary: string | null;
}

/** schema 駆動で抽出した項目(メタデータ/項目抽出 有効時のみ)。 */
export interface ExtractionField {
  name: string;
  value: string;
  value_type: string;
  confidence: number | null;
  page_number: number | null;
}

export interface StructuredExtraction {
  raw_text: string;
  document_type: string;
  confidence: number;
  warnings: string[];
  elements: DocumentElement[];
  pages: ExtractionPage[];
  tables: ExtractionTable[];
  assets: ExtractionAsset[];
  parser_artifacts: Record<string, string | number | boolean | null>;
}

export interface DocumentDetail extends DocumentSummary {
  object_storage_path: string | null;
  preprocess_artifact: DocumentPreprocessArtifact | null;
  extraction: Record<string, unknown>;
  error_message: string | null;
  duplicate_source: DuplicateDocumentRef | null;
  /** 文書の分類と有効期間(検索の分類フィルタと基準日に使う)。未設定は null。 */
  classification?: DocumentClassification | null;
}

/** 文書の分類と有効期間。日付は YYYY-MM-DD、終了日は排他的(当日は期間外)。 */
export interface DocumentClassification {
  large_category: string | null;
  middle_category: string | null;
  small_category: string | null;
  effective_from: string | null;
  effective_to: string | null;
}

/** 文書の分類の一覧の 1 件（1 = 大分類、2 = 中分類、3 = 小分類）と、その下の分類（並び順。#2131）。 */
export interface DocumentCategory {
  id: string;
  name: string;
  level: number;
  parent_id: string | null;
  /** この分類（とその下の分類）を使っている文書の数。 */
  document_count: number;
  children: DocumentCategory[];
}

export interface DocumentDeleteResult {
  id: string;
  file_name: string;
  object_storage_path: string | null;
  object_deleted: boolean;
  artifact_deleted_count: number;
  artifact_delete_failed_count: number;
}

/** 一括の投入（#2674）の 1 回の要求に入れられる文書の数。backend の BULK_INGESTION_MAX_DOCUMENTS と同じ。 */
export const BULK_INGESTION_MAX_DOCUMENTS = 10_000;

/**
 * 一括の投入の待ち時間（#2674）。1 万件の判定と登録（失敗の後のやり直しで出力の初期化が要る文書は
 * 1 件の投入と同じ処理）を 1 回の要求で行うため、通常の 30 秒では足りない。backend の
 * 長い要求の上限（600 秒）にそろえる。途中で切れても、再送は受付済みの文書を二重に登録しない。
 */
export const BULK_INGESTION_TIMEOUT_MS = 600_000;

/** 一括の投入の 1 文書の結果。 */
export type BulkIngestionOutcome = "accepted" | "already_active" | "skipped" | "rejected";

export interface BulkIngestionItem {
  document_id: string;
  outcome: BulkIngestionOutcome;
  /** 機械が読む理由（not_found / job_active / already_indexed / duplicate_content など）。 */
  reason: string | null;
  /** 利用者向けの文（backend の日本語）。 */
  message: string | null;
  job_id: string | null;
}

export interface BulkIngestionReasonCount {
  outcome: BulkIngestionOutcome;
  reason: string;
  count: number;
  message: string | null;
}

/** 一括の投入の結果。accepted の件数の job は応答の前に永続化済み。 */
export interface BulkIngestionResult {
  requested_count: number;
  unique_count: number;
  accepted_count: number;
  already_active_count: number;
  skipped_count: number;
  rejected_count: number;
  reasons: BulkIngestionReasonCount[];
  items: BulkIngestionItem[];
}

/** 条件に一致する文書の ID（新しい順に最大 limit 件）と、条件に一致する件数。 */
export interface DocumentIdList {
  ids: string[];
  total: number;
  limit: number;
}

/** 削除の前に確認する影響（#303）。正本を参照する重複文書の件数と、その所属ナレッジベース。 */
export interface DocumentDeleteImpact {
  document_id: string;
  duplicate_count: number;
  knowledge_bases: KnowledgeBaseRef[];
}

/**
 * 選んだ文書解析エンジン（既定は Docling。PDF と画像だけ）で扱えない形式の案内（#286）。
 * 取込は始めず、処理レシピで Unstructured を選ぶよう案内する。
 */
export interface ParserSourceNotice {
  code: string;
  backend: string;
  file_format: string;
  suggested_backend: string | null;
  message: string;
}

export interface UploadResult {
  id: string;
  file_name: string;
  status: FileStatus;
  file_size_bytes: number;
  content_sha256: string;
  duplicate_of_document_id: string | null;
  knowledge_bases: KnowledgeBaseRef[];
  source_profile: SourceProfile;
  ingestion_started: boolean;
  parser_notice?: ParserSourceNotice | null;
}

export interface BatchUploadFailedItem {
  file_name: string;
  status_code: number;
  message: string;
  source_profile: SourceProfile | null;
}

/** プレビューのページ画像 1 ページの表示寸法（pt。ページの /Rotate を反映した向き）。 */
export interface DocumentPreviewPage {
  page_number: number;
  width: number;
  height: number;
}

export interface DocumentPreviewPages {
  page_count: number;
  pages: DocumentPreviewPage[];
}

function documentPreviewPagesBase(id: string, recipeId?: string | null): string {
  const base = `/api/documents/${encodeURIComponent(id)}`;
  return recipeId
    ? `${base}/recipes/${encodeURIComponent(recipeId)}/preview-pages`
    : `${base}/preview-pages`;
}

export interface DocumentChunkView {
  document_id: string;
  chunk_id: string;
  chunk_index: number;
  text: string;
  page_start: number | null;
  page_end: number | null;
  bbox: number[] | null;
  section_path: string | null;
  content_kind: string | null;
  chunk_group_id: string | null;
  source_parser: string | null;
  element_ids: string[];
  metadata: Record<string, JsonValue>;
}

/** 文書の chunk_set(variant = 1 レシピのチャンク集合)1 件分。 */
export type DocumentLayerStatusName =
  | "not_requested"
  | "planned_only"
  | "materialized"
  | "needs_reingest"
  | "error";

/** レイヤーの作成後に変わると作り直しが必要になる入力（backend の layer_fingerprint。#550）。 */
export type DocumentLayerRebuildInput =
  | "field_schema_hash"
  | "chunk_metadata_contract"
  | "navigation_summary_max_nodes";

export interface DocumentMaterializationLayerStatus {
  layer_id: string | null;
  requested: boolean;
  status: DocumentLayerStatusName;
  reason: string | null;
  /** 作ったときの入力が今の設定と違う（status とは別の印。指紋の無い古い行は false）。 */
  rebuild_required?: boolean;
  rebuild_inputs?: string[];
}

export interface DocumentChunkSetLayerStatuses {
  metadata: DocumentMaterializationLayerStatus;
  graph: DocumentMaterializationLayerStatus;
  navigation: DocumentMaterializationLayerStatus;
}

export interface DocumentChunkSet {
  chunk_set_id: string;
  extraction_recipe_id: string | null;
  extraction_status: DocumentLayerStatusName;
  extraction_reason: string | null;
  status: string;
  chunk_count: number;
  vector_count: number;
  /** 配信中(serving)か。文書につき 1 つだけ true。candidate(実験)は false。 */
  is_serving: boolean;
  /** chunk_set の作成日時。診断行の「chunk_set 作成」表示に使う。 */
  created_at: string | null;
  /** 親抽出(extraction)の ID。parser×preprocess ごとに分かれる 2 階層の上位キー。 */
  extraction_id: string | null;
  /** 親抽出の parser backend(2 階層表示のラベル)。 */
  parser: string | null;
  /** 親抽出の前処理プロファイル(2 階層表示のラベル)。 */
  preprocess: string | null;
  knowledge_base_ids: string[];
  serving_knowledge_base_ids: string[];
  layer_statuses: DocumentChunkSetLayerStatuses;
}

export interface DocumentChunkPreviewRequest {
  chunking_strategy?: ChunkingStrategyName;
  chunk_size?: number;
  chunk_overlap?: number;
  chunk_min_chars?: number;
  chunk_delimiter?: string;
  chunk_child_target_chars?: number;
  chunk_table_child_target_chars?: number;
  chunk_parent_target_chars?: number;
  chunk_parent_max_pages?: number;
  chunk_parent_max_children?: number;
}

export interface DocumentChunkPreviewStats {
  chunk_count: number;
  min_chars: number;
  average_chars: number;
  max_chars: number;
  overflow_count: number;
  embedding_overflow_count: number;
}

export interface DocumentChunkPreviewResponse {
  chunks: DocumentChunkView[];
  stats: DocumentChunkPreviewStats;
  warnings: string[];
}

export type DocumentExtractionExportFormat =
  "json" | "markdown" | "html" | "chunks";

export interface DocumentExtractionExport {
  document_id: string;
  file_name: string;
  format: DocumentExtractionExportFormat;
  content_type: string;
  content: string;
  payload: Record<string, unknown>;
  chunks: DocumentChunkView[];
  parser_backend: string | null;
  parser_profile: string | null;
  page_count: number;
  element_count: number;
  table_count: number;
  asset_count: number;
}

export interface IngestionSegment {
  segment_id: string;
  document_id: string;
  recipe_id: string | null;
  status: string;
  parser_backend: string;
  parser_profile: string;
  page_start: number | null;
  page_end: number | null;
  progress_unit: "page" | "slide" | "sheet" | "source" | string;
  progress_start: number | null;
  progress_end: number | null;
  attempt_count: number;
  artifact_path: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface BatchUploadResult {
  items: UploadResult[];
  failed_items: BatchUploadFailedItem[];
  total_count: number;
  uploaded_count: number;
  failed_count: number;
}

// --- ナレッジベース ---
export interface KnowledgeBaseSummary extends KnowledgeBaseRef {
  description: string | null;
  status: KnowledgeBaseStatus;
  default_search_mode: SearchMode;
  document_count: number;
  indexed_document_count: number;
  error_document_count: number;
  searchable_chunk_count: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

/** KB 単位の取込上書き(Parser/Chunking)。null はグローバル継承。 */
/** 前処理 excel_to_jsonl の選択肢（#1221）。 */
export interface ExcelOptions {
  /** auto は帳票なら form、手順書なら procedure、密な表なら table（#2516）。 */
  mode: "auto" | "table" | "procedure" | "form";
  /** 表頭の行（1 始まり）。null は先頭の行から推定する。 */
  header_row: number | null;
  header_row_count: number;
  sheets: string[];
  exclude_sheets: string[];
  include_hidden_sheets: boolean;
  /** 非表示の行・列のセルも読むか（#2526。オートフィルターで隠れた行は読む）。 */
  include_hidden_cells: boolean;
  exclude_columns: string[];
  /** 読む範囲（`A3:F200` か `シート名!A3:F200`。空はシート全体。#1229）。 */
  ranges: string[];
  /** 1 シートの空の行・列で分かれた表を別々に読むか（#2516）。 */
  split_tables: boolean;
  /** 表を分ける空の行・列の数（1〜20。これ以上続いたら別の表）。 */
  table_gap_rows: number;
  table_gap_columns: number;
  /** auto で帳票と判定する点数の下限（0〜1。#2516）。 */
  form_detection_threshold: number;
  /** 列の役割（既定値・例示など）を表頭の語から決めるか（#1281）。 */
  column_role_detection?: "auto" | "off";
  /** 列の役割の指定（列名か列の記号 → 役割。none は付けない。#1281）。 */
  column_roles?: Record<string, ExcelColumnRoleSetting>;
}

/** Excel の列の役割（#1281。backend の `ColumnRoleSetting`）。 */
export type ExcelColumnRoleSetting =
  | "definition"
  | "default"
  | "example"
  | "current"
  | "recommended"
  | "allowed"
  | "none";

export interface KnowledgeBaseIngestionConfig {
  preprocess_profile: PreprocessProfileName | null;
  /** 前処理 excel_to_jsonl の選択肢（#1221）。null は全体の既定に従う。 */
  excel_options?: ExcelOptions | null;
  parser_adapter_backend: ParserAdapterBackend | null;
  parser_docling_enabled: boolean | null;
  parser_unstructured_enabled: boolean | null;
  /** 図・画像を AI で読み取る(Vision)。解析エンジンに関係なく使える(#497)。 */
  vision_enabled: boolean | null;
  /** 見出しの階層の推定（AI。#2277）。null は全体の既定（無効）に従う。 */
  heading_level_inference_enabled?: boolean | null;
  chunking_strategy: ChunkingStrategyName | null;
  chunk_size: number | null;
  chunk_overlap: number | null;
  chunk_min_chars: number | null;
  /** 固定分割の分割符（escape 表現。#2457）。null は全体の既定に従う。 */
  chunk_delimiter?: string | null;
  /** 親子階層（small-to-big）の分割パラメータ(分割方式が small_to_big のときだけ効く)。 */
  chunk_child_target_chars?: number | null;
  chunk_table_child_target_chars?: number | null;
  chunk_parent_target_chars?: number | null;
  chunk_parent_max_pages?: number | null;
  chunk_parent_max_children?: number | null;
  graph_profile: GraphProfileName | null;
  /**
   * 実体・別名索引（#1362 / #1388）。索引の保存の後に、表の行・本文の定義の形から実体を作る。null は全体の
   * 既定（無効）に従う。列名は表の行の名前・属性にする列（空は列名で決める）。
   */
  entity_index_enabled?: boolean | null;
  entity_name_columns?: string[] | null;
  entity_attribute_columns?: string[] | null;
  field_extraction_enabled: boolean | null;
  navigation_summary_enabled: boolean | null;
  auto_parse_after_preprocess_enabled: boolean | null;
  auto_chunk_after_extract_enabled: boolean | null;
  auto_index_after_chunk_enabled: boolean | null;
}

/** 検索・回答設定。Search Answer Profile の query 設定として使う。 */
export interface KnowledgeBaseQueryConfig {
  // 検索モード・検索オプション・根拠確認・回答スタイルは #595 で削除した（保存済みの値は backend が読み捨てる）。
  // 回答エンジンの選択(answer_engine)は #594 で削除した(回答は回答フローだけ)。
  /** 回答フローの設定。null / 未指定はグローバル継承。 */
  query_strategy?: QueryStrategyName | null;
  answer_flow?: AnswerFlowName | null;
  neighbor_child_count?: number | null;
  rerank_enabled?: boolean | null;
  // 画面目録で操作画面を探す(LLM の呼び出しが 1 回増える。#554)。
  screen_linking_enabled?: boolean | null;
  // 根拠の無い要求だけを探し直す(LLM は呼ばない。検索が要求ごとに 1 回増える。#1279)。
  request_coverage_retrieval_enabled?: boolean | null;
  // 業務ガイドの手順ごとに探し、手順の参照の頁の根拠を入れる(LLM は呼ばない。#2395)。ON / OFF と回数・期限の上限は
  // 全体設定だけで決め、検索・回答プロファイルでは保存しない(#3523)。
  guide_step_retrieval_enabled?: boolean | null;
  guide_step_max_queries?: number | null;
  guide_step_deadline_seconds?: number | null;
  // 質問から抽出項目の条件を読み取る(LLM の呼び出しが 1 回増える。#652)。
  auto_field_filter_enabled?: boolean | null;
  /**
   * 実体・別名による検索拡張(LLM は呼ばない)。検索方法の全体設定だけで決め、プロファイルでは保存しない。
   * 文書レシピで実体・別名索引を有効にした文書にだけ効く。
   */
  entity_expansion_enabled?: boolean | null;
  /** 1 回の検索で加える根拠の上限(1〜20)。null は既定(6)。 */
  entity_expansion_max_chunks?: number | null;
  /**
   * 文書構造索引で根拠の候補を広げる(AI は呼ばない)。検索方法の全体設定だけで決める。
   * null / 未指定は全体の既定(検索方法の画面。既定は使わない)。
   */
  graph_expansion_enabled?: boolean | null;
  /** 1 回の検索で加える根拠の上限(1〜20)。全体設定だけで決め、検索・回答プロファイルでは保存しない(#3523)。 */
  graph_expansion_max_chunks?: number | null;
  /**
   * 当たった根拠の章節から親の章節へ段階的に広げる(#1436。段ごとに AI の呼び出しが 1 回増える)。
   * null / 未指定は既定(使う)、false は使わない。上限は null が既定(4 段 / 50%)。
   */
  section_expansion_enabled?: boolean | null;
  section_expansion_max_levels?: number | null;
  section_expansion_input_token_percent?: number | null;
  /**
   * rerank の後、根拠の起点に採る同じ文書の chunk の上限(0〜20。0 は上限なし。#2167)。超えた chunk は後ろへ回す。
   * null は既定(5)。
   */
  max_anchors_per_document?: number | null;
  /**
   * 業務ガイドの照合で、質問と質問の例の埋め込みの近さを点に足す(#2398。質問ごとに埋め込みの呼び出しが 1 回増える)。
   * null / 未指定は全体の既定(使わない)。
   */
  /** 業務ガイドの使用。未指定・null は OFF（全体の ON を継承しない）。 */
  support_guide_enabled?: boolean | null;
  support_guide_semantic_match_enabled?: boolean | null;
  /** 上位の 2 つの業務ガイドの点が近いとき、どれに沿うかを確かめる(#2398)。null / 未指定は全体の既定(確かめない)。 */
  support_guide_close_match_clarification_enabled?: boolean | null;
}

/**
 * プロファイルの窓口の上書きと業務ガイドの引き継ぎ先が空のときに継承する、システム設定の窓口（#2683）。
 * 回答の注意書きが無効なら `enabled: false`（窓口の案内を出さない）で、`contact` は空。
 */
export interface InheritedAnswerContactData {
  enabled: boolean;
  contact: string;
}

/** 参照するナレッジベースで実体・別名索引を持つ文書の数(#1388)。 */
export interface EntityIndexCoverageData {
  document_count: number;
}

export type QueryStrategyName =
  | "auto_routing"
  | "simple_retrieval"
  | "rag_fusion"
  | "query_decomposition"
  | "step_back_prompting"
  | "hyde";

export type AnswerFlowName = "crag" | "standard_rag";


/** KB 単位の構築設定。query は legacy 互換として読めるが KB runtime では使わない。 */
export interface KnowledgeBaseAdapterConfig {
  version: number;
  ingestion: KnowledgeBaseIngestionConfig;
  query: KnowledgeBaseQueryConfig;
}

export interface KnowledgeBaseDetail extends KnowledgeBaseSummary {
  retrieval_config: Record<string, unknown>;
  adapter_config: KnowledgeBaseAdapterConfig;
  /** 既存 retrieval_config に legacy query 設定が残っており、現在は無視されている。 */
  legacy_query_config_ignored?: boolean;
}

export const DEFAULT_KNOWLEDGE_BASE_NAME = "DEFAULT";

export interface KnowledgeBaseGraphNode {
  id: string;
  name: string;
  type: string | null;
  confidence: number;
}

export interface KnowledgeBaseGraphEdge {
  id: string;
  source: string;
  target: string;
  type: string | null;
  confidence: number;
}

export interface KnowledgeBaseGraphData {
  status: "ok" | "empty";
  nodes: KnowledgeBaseGraphNode[];
  edges: KnowledgeBaseGraphEdge[];
  truncated: boolean;
}

export interface KnowledgeBaseCreateRequest {
  name: string;
  description?: string | null;
  default_search_mode?: SearchMode;
  retrieval_config?: Record<string, unknown>;
}

export interface KnowledgeBaseUpdateRequest {
  name?: string | null;
  description?: string | null;
  default_search_mode?: SearchMode | null;
  retrieval_config?: Record<string, unknown> | null;
}

export type SearchAnswerProfileStatus = "ACTIVE" | "ARCHIVED";

export const DEFAULT_SEARCH_ANSWER_PROFILE_NAME = "DEFAULT";

export interface SearchAnswerProfileRef {
  id: string;
  name: string;
}

/** Search Answer Profile の設定一式。query は検索・回答設定。 */
export interface SearchAnswerProfileConfig {
  version: number;
  answer_model_id?: string | null;
  knowledge_base_ids: string[];
  query: KnowledgeBaseQueryConfig;
  /**
   * 回答前の確認の方針（#1839）。confirm = 曖昧な質問を確認してから回答する / assume = 確認せずに前提を示して
   * 回答する。古い保存には無い（confirm として扱う）。
   */
  clarification_policy?: "confirm" | "assume";
  /**
   * 言語資源の上書き（#2181。全体の言語資源に重ねる。検索・回答の項目だけ）。古い保存には無い（上書き無し）。
   */
  language_pack?: LanguagePackSections;
  /**
   * 回答の注意書きの窓口の上書き（#2400。窓口の名前・連絡の方法の 1 行）。空・古い保存に無いときはシステム設定の窓口。
   */
  contact_override?: string;
  /** 資料で答えた回答の窓口の案内を「資料から分かる仕様を示し、当てはまらなければ窓口へ」の形にするか（#2400。既定 false）。 */
  // system prompt・既定言語は #595 で削除した（回答は回答プロンプトのテンプレートで作る）。
  // 配信モード（serving_mode）は #1331 で削除した（文書の全 active レシピを常に融合する）。
}

export interface SearchAnswerProfileSummary extends SearchAnswerProfileRef {
  description: string | null;
  status: SearchAnswerProfileStatus;
  knowledge_base_count: number;
  /** 参照 KB のうちアーカイブ済みの件数（検索対象にならない。#302）。 */
  archived_knowledge_base_count?: number;
  /** 参照 KB のうち存在しない件数（検索対象にならない。#302）。 */
  missing_knowledge_base_count?: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

/** 検索・回答プロファイルが参照する KB（アーカイブ済みを含む。status で見分ける）。 */
export interface SearchAnswerProfileKnowledgeBaseRef extends KnowledgeBaseRef {
  status: KnowledgeBaseStatus;
}

export interface SearchAnswerProfileDetail extends SearchAnswerProfileSummary {
  config: SearchAnswerProfileConfig;
  knowledge_bases: SearchAnswerProfileKnowledgeBaseRef[];
  /** 参照 KB のうち存在しない ID（#302）。 */
  missing_knowledge_base_ids?: string[];
}

export interface SearchAnswerProfileCreateRequest {
  name: string;
  description?: string | null;
  config?: SearchAnswerProfileConfig;
}

export interface SearchAnswerProfileUpdateRequest {
  name?: string | null;
  description?: string | null;
  config?: SearchAnswerProfileConfig;
}

/** 文書の取込設定スナップショット(3 層モデル: 文書単位の単一レシピ)と global 既定とのドリフト状況。 */
/** 処理設定パネルに渡すレシピの設定（保存した上書きと、global 既定を重ねた有効値）。 */
export interface DocumentProcessingConfigData {
  processing_config: DocumentProcessingConfig;
  effective_processing_config: DocumentProcessingConfig;
}

export interface DocumentProcessingConfig extends KnowledgeBaseIngestionConfig {
  /** 章節の抽出規則の方式（#715）。null は全体の既定に従う。 */
  section_rules_mode?: SectionRulesMode | null;
}

/** 章節の抽出規則（#715）。parser は解析エンジンの見出し、custom は独自の規則。 */
export type SectionRulesMode = "parser" | "legal" | "official" | "numbered" | "custom";

export interface SectionRule {
  name: string;
  pattern: string;
  level: number;
  enabled: boolean;
}

export interface SectionRulesSettingsData {
  mode: SectionRulesMode;
  rules: SectionRule[];
  presets: Partial<Record<SectionRulesMode, SectionRule[]>>;
}

export type DocumentRecipeStepStatus =
  | "PENDING"
  | "QUEUED"
  | "RUNNING"
  | "NEEDS_REVIEW"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export interface DocumentRecipeStep {
  phase: IngestionJobPhase;
  status: DocumentRecipeStepStatus;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  /** status が QUEUED の工程の順番待ちの情報（#2026）。 */
  queue_wait?: IngestionQueueWait | null;
}

export interface DocumentRecipeView {
  recipe_id: string;
  document_id: string;
  slot_no: 1 | 2 | 3;
  status: FileStatus;
  failed_phase: IngestionJobPhase | null;
  processing_config: DocumentProcessingConfig;
  effective_processing_config: DocumentProcessingConfig;
  preprocess_artifact: DocumentPreprocessArtifact | null;
  active_extraction_recipe_id: string | null;
  active_chunk_set_id: string | null;
  /** Embedding / 索引の前の保存済み chunk_set(最新の CHUNKED)。INDEX の再実行が使う(#2467)。 */
  pending_chunk_set_id?: string | null;
  chunk_count: number;
  vector_count: number;
  config_revision: number;
  materialized_revision: number | null;
  searchable: boolean;
  needs_reprocessing: boolean;
  error_message: string | null;
  steps: DocumentRecipeStep[];
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface DocumentRecipeDeleteResult {
  recipe_id: string;
  document_id: string;
  removed_chunk_set_count: number;
}

export interface KnowledgeBaseDocumentAssignmentRequest {
  document_ids: string[];
}

export interface DocumentKnowledgeBaseReplaceRequest {
  knowledge_base_ids: string[];
}

// --- 検索 ---
export interface SearchRequestBody {
  // 検索の方式（mode / strategy）・rerank の件数・回答スタイルは、回答エンジンが使わないため送らない（#595）。
  query: string;
  top_k?: number;
  filters?: Record<string, string>;
  knowledge_base_ids?: string[];
  /** 検索対象の検索・回答プロファイル（1 つ。#635）。 */
  search_answer_profile_id?: string | null;
  /** 回答を作らずに検索だけを行う(LLM を呼ばない。#593)。 */
  retrieval_only?: boolean;
  /** false なら回答を生成しない（RAG 検索の画面。CRAG と回答の LLM を使わない。#649）。 */
  generate_answer?: boolean;
  /** 質問から読み取った条件のうち、利用者が外した項目の名前（#652）。 */
  auto_field_filter_excluded?: string[];
  /** 回答のモデル（既定のテキストモデルか既定の Vision モデル。未指定はテキスト。#675）。 */
  model_id?: string;
}

export interface RetrievedChunk {
  document_id: string;
  chunk_id: string;
  text: string;
  score: number;
  rerank_score: number | null;
  file_name: string | null;
  category_name: string | null;
  metadata: Record<string, JsonValue>;
}

// --- チャット（会話 / マルチモデル比較）---
export type ConversationStatus = "ACTIVE" | "ARCHIVED";
export type MessageRole = "USER" | "ASSISTANT" | "SYSTEM";
/**
 * メッセージの状態。回答は作成を始めた時点で `STREAMING`（作成中）で保存され、終わったら `COMPLETE`・
 * `ERROR`（失敗・中断）・`CANCELLED`（利用者の停止）になる（#1175）。
 */
export type MessageStatus = "STREAMING" | "COMPLETE" | "ERROR" | "CANCELLED";

export interface ChatMessage {
  message_id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  model: string | null;
  citations: RetrievedChunk[];
  /** 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（抜粋・文書は届かない。#3252）。 */
  withheld_evidence_count?: number;
  trace_id: string | null;
  status: MessageStatus;
  reply_to_message_id: string | null;
  created_at: string;
  /** 回答前に確認した内容（USER のメッセージだけ。確認しなかった質問は null / 未指定。#1839）。 */
  clarification?: ChatClarificationRecord | null;
  /** 回答の注意書き（システム設定の文。backend が回答の確定の後に付ける。確認の質問・無効のときは null / 未指定。#2405）。 */
  notice?: string | null;
  /** 会話の持ち主が送った質問か。system_admin が他の利用者の会話で送った質問は false（#1928）。 */
  sent_by_owner?: boolean;
  /** 持ち主以外が送った質問の、送った人の表示名（引けない利用者は空。#1928）。 */
  sender_display_name?: string;
  /** 質問に添付したファイル（#2517）。 */
  attachments?: ChatAttachmentApiReference[];
  /**
   * 回答の段落の印・根拠と矛盾して外した段落・回答全体の注意（ASSISTANT の完了した回答。#2738。契約は
   * platform/contracts/answer-passages/passage-marks.json）。この記録より前の回答は未指定（今までの表示のまま）。
   */
  passage_marks?: AnswerPassageMarkData[];
  removed_passages?: AnswerRemovedPassageData[];
  answer_warnings?: AnswerPassageReasonData[];
  /**
   * 回答モデルへ添付した根拠の画像の件数（ASSISTANT の完了した回答。回答の記録の診断の `models.attached_images`。
   * #3444）。画像を添付しなかった回答・記録の無い回答は 0 / 未指定。
   */
  answer_attached_images?: number;
}

/** 段落の印・外した段落・回答全体の注意の理由（#2738）。label は利用者に見せる短い文（backend の文をそのまま出す）。 */
export interface AnswerPassageReasonData {
  code: string;
  label: string;
  detail?: string;
}

export interface AnswerPassageMarkData {
  /** 回答の本文の段落の原文（段落の照合に使う）。 */
  text: string;
  state: "unverified" | "warning";
  reasons: AnswerPassageReasonData[];
}

export interface AnswerRemovedPassageData {
  text: string;
  reasons: AnswerPassageReasonData[];
}

export interface ConversationSummary {
  id: string;
  search_answer_profile_id: string;
  title: string | null;
  status: ConversationStatus;
  message_count: number;
  created_at: string;
  updated_at: string;
  /** 見ている利用者の会話か。system_admin が他の利用者の会話を見るときは false（#1889。操作は本人と同じ。#1928）。 */
  owned?: boolean;
  /** system_admin の一覧・詳細だけに付く利用者の表示名（引けない利用者は空。#1889・#1696）。 */
  actor_display_name?: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: ChatMessage[];
}

export interface ConversationCreateBody {
  search_answer_profile_id: string;
  title?: string | null;
}

export interface ConversationUpdateBody {
  title: string;
}

export interface ChatMessageRequestBody {
  content: string;
  question_context_token?: string;
  model_ids?: string[];
  top_k?: number;
  /** 利用者が選んだ類似の承認済み FAQ の id。質問と一緒に LLM へ渡す（#684）。 */
  approved_faq_id?: string;
  /** 利用者が確認の質問に答えた内容（#717）。backend がルールから引き直す。 */
  clarification?: ClarificationAnswer;
  /** 回答前の確認を「確認せずに回答」で飛ばした確認の id（会話に残すだけ。#1839）。 */
  clarification_skipped_rule_id?: string;
  /** 画面が決めた質問の id（32 桁の 16 進。#1175）。送信の直後（`start` の前）の停止もこの id で取り消せる。 */
  client_message_id?: string;
  /** 質問に添付するファイル（`uploadChatAttachment` の `attachment_id`。#2517）。 */
  attachment_ids?: string[];
}

/** 会話の添付の一覧と上限（#2517）。 */
export interface ChatAttachmentListData {
  items: ChatAttachmentApiItem[];
  limits: ChatAttachmentApiLimits;
}

/** ルールの確認の質問（チャットの確認。#717）。 */
export interface ClarificationSection {
  document_id: string;
  document_name: string;
  section_id: string;
  title: string;
  page_start: number | null;
  page_end: number | null;
}

export interface ClarificationOption {
  id: string;
  label: string;
  description: string;
  search_terms: string[];
  premise: string;
  sections: ClarificationSection[];
}

export interface RuleClarification {
  question: string;
  multiple: boolean;
  allow_other: boolean;
  options: ClarificationOption[];
}

export interface ClarificationSuggestionData {
  rule_id: string;
  rule_title: string;
  clarification: RuleClarification;
}

export interface ClarificationAnswer {
  rule_id: string;
  option_ids: string[];
  other_text: string;
}

/** 回答前に確認した内容（USER のメッセージに付く。#1839）。 */
export interface ChatClarificationRecord {
  outcome: "answered" | "skipped";
  /** `free_text_only` は選択肢の無い（自由入力だけで答える）確認事項（答えに「その他: 」を付けない。#3410）。 */
  entries: { prompt: string; selected?: string[]; other_text?: string; free_text_only?: boolean }[];
}

/** 回答に選べるモデル（既定のテキストモデルと既定の Vision モデルだけ。先頭が既定。#675）。 */
export interface CompareModel {
  model_id: string;
  display_name: string;
  /** 役割（#675）。既定のテキストモデルが画像対応モデルも兼ねるときは 1 件で `text_vision`（#888）。 */
  kind: "text" | "vision" | "text_vision";
}

export interface SearchDiagnostics {
  /** 回答エンジンの名前（"grounded"）。 */
  retrieval_strategy: string;
  /** "grounded"（回答）/ "retrieval_only"（検索だけ）。 */
  retrieval_strategy_adapter?: string;
  filter_keys: string[];
  knowledge_base_count: number;
  search_answer_profile_applied?: string | null;
  config_fingerprint: string;
  /** 回答エンジンの記録（検索の工程・根拠の評価など）。 */
  answer?: Record<string, JsonValue> | null;
}

export interface SearchResponse {
  answer: string;
  citations: RetrievedChunk[];
  trace_id: string;
  elapsed_ms: number;
  diagnostics: SearchDiagnostics;
  /** 回答の注意書き（システム設定の文。backend が回答の確定の後に付ける。確認の質問・無効のときは null / 未指定。#2405）。 */
  notice?: string | null;
}

export interface FeedbackRequestBody {
  trace_id: string;
  search_answer_profile_id: string;
  target_type: FeedbackTargetType;
  source_surface: FeedbackSourceSurface;
  document_id?: string | null;
  chunk_id?: string | null;
  message_id?: string | null;
  content_snapshot?: FeedbackContentSnapshot | null;
  rating: CitationFeedbackRating;
  reason?: CitationFeedbackReason | null;
  comment?: string | null;
  /** 修正した回答(回答を「役に立たなかった」と評価したときだけ)。 */
  corrected_answer?: string | null;
}

/** 応答は画面の snapshot を返さない（backend が除外する）。 */
export interface FeedbackSubmissionResponse extends Omit<FeedbackRequestBody, "content_snapshot"> {
  feedback_id: string;
}

export interface CurrentFeedbackItem extends Omit<
  FeedbackSubmissionResponse,
  "search_answer_profile_id" | "source_surface"
> {
  search_answer_profile_id: string | null;
  source_surface: FeedbackSourceSurface | null;
  created_at: string;
}

export interface FeedbackCitationSnapshot {
  document_id: string;
  chunk_id: string;
  file_name: string | null;
  section_title: string | null;
  page_number: number | null;
  content_preview: string | null;
  rerank_score: number | null;
}

export interface FeedbackContentSnapshot {
  question: string;
  /** 回答を生成しない検索（検索結果だけ）では null（#978）。 */
  answer: string | null;
  citations: FeedbackCitationSnapshot[];
}

export interface FeedbackReasonCount {
  reason: CitationFeedbackReason;
  count: number;
}

export interface FeedbackSummary {
  total: number;
  helpful_count: number;
  not_helpful_count: number;
  helpful_rate: number;
  answer_total: number;
  answer_helpful_rate: number;
  citation_total: number;
  citation_helpful_rate: number;
  reason_counts: FeedbackReasonCount[];
}

export interface FeedbackItem extends CurrentFeedbackItem {
  search_answer_profile_name: string | null;
  conversation_id: string | null;
  conversation_title: string | null;
  message_id: string | null;
  model: string | null;
  file_name: string | null;
  question_preview: string | null;
  comment_preview: string | null;
  has_comment: boolean;
}

export interface FeedbackExecutionInfo {
  outcome: string | null;
  search_mode: string | null;
  elapsed_ms: number | null;
  retrieved_count: number | null;
  reranked_count: number | null;
  citation_count: number | null;
  config_fingerprint: string | null;
}

export interface FeedbackDetail extends FeedbackItem {
  content_source: "chat_message" | "search_snapshot" | null;
  question: string | null;
  answer: string | null;
  comment: string | null;
  citations: FeedbackCitationSnapshot[];
  execution: FeedbackExecutionInfo;
}

export type AnswerPromptKey = `${string}.${string}` | "vlm_answer" | "image_retrieval" | "heading_levels" | "field_extraction" | "navigation_summary" | "raptor_summary_system";

/** 機能別の設定画面で編集できるプロンプト。 */
export interface AnswerPromptView {
  key: AnswerPromptKey;
  content: string;
  default_content: string;
  customized: boolean;
  required_placeholders: string[];
  required_tokens?: string[];
  updated_at: string | null;
}

export interface AnswerPromptsData {
  prompts: AnswerPromptView[];
  /** 取込から評価までの各段の読み取り専用プロンプト(コードで管理。処理の順)。 */
  stages: AnswerPromptStage[];
}

/** 処理の順の段の区分（backend の answer_prompts.PROMPT_PHASES）。 */
export type AnswerPromptPhase =
  | "ingestion"
  | "indexing"
  | "knowledge"
  | "question"
  | "retrieval"
  | "grounding"
  | "generation"
  | "audit"
  | "evaluation";

export interface AnswerPromptStage {
  id: string;
  phase: AnswerPromptPhase;
  prompts: { id: string; content: string; prompt_key?: AnswerPromptKey | null }[];
}

export interface FeedbackApprovedFaqPromotion {
  search_answer_profile_id: string;
  question: string;
  inserted_count: number;
  deleted_count: number;
}

export interface FeedbackDashboard {
  summary: FeedbackSummary;
  previous_summary: FeedbackSummary | null;
  items: Page<FeedbackItem>;
}

export interface FeedbackListParams {
  search_answer_profile_id?: string;
  target_type?: FeedbackTargetType;
  rating?: CitationFeedbackRating;
  reason?: CitationFeedbackReason;
  period_days?: number | null;
  q?: string;
  sort_order?: "newest" | "oldest";
  limit?: number;
  offset?: number;
}

// --- 評価 ---
export interface EvaluationCase {
  id: string;
  query: string;
  relevant_document_ids?: string[];
  /** 同じ文書とみなす別の文書の ID（正解の文書の ID → 別の文書の ID。同じ名前の文書。#2086）。 */
  equivalent_document_ids?: Record<string, string[]>;
  /** 標準回答。あるケースは回答を標準回答と LLM で比較する（#591）。 */
  standard_answer?: string | null;
  /** false は答えるべきでない質問。省略時は期待値（文書・語・標準回答）の有無で決まる。 */
  answerable?: boolean | null;
  /** 分類（#1226）。結果の分類ごとの内訳に使う。 */
  category?: EvaluationCaseCategory | null;
  /** 業務支援の採点（#1231）。受け入れる対応・手順の語（順）・勧めてはいけない表現・触れるべき条件。 */
  expected_response_decision?: "answer" | "clarify" | "hold" | null;
  expected_outcomes?: EvaluationOutcome[];
  required_answer_points?: string[];
  /** required_answer_points と同等の別の手順の列（#1284）。いちばん点の高い列で採点する。 */
  required_conditions?: string[];
  /** 区分（#1284）。dev は調整に使うケース、holdout は最後の確認だけに使うケース。 */
  split?: EvaluationCaseSplit | null;
  /** 質問と一緒に渡す既知の条件の値（条件の id → 値。#1284）。 */
  conditions?: Record<string, string>;
  /** 確認の質問への返答の列（#1284）。 */
  turns?: EvaluationTurn[];
  /** 最後の回答に必要な根拠（#1284）。 */
  required_evidence?: EvaluationEvidence[];
  /** 回答が案内すべき画面（名前と別名。#2402）。 */
}

/** 回答が案内すべき画面（#2402）。名前と、同じ画面の別の呼び方。 */
export interface EvaluationScreen {
  name: string;
  aliases?: string[];
}

/** 評価のケースの区分（#1284）。 */
export type EvaluationCaseSplit = "dev" | "holdout";

/** 確認の質問への返答の 1 往復（#1284）。期待はこの往復の回答だけを採点する。 */
export interface EvaluationTurn {
  reply: string;
  conditions?: Record<string, string>;
  expected_response_decision?: "answer" | "clarify" | "hold" | null;
  expected_outcomes?: EvaluationOutcome[];
  required_answer_points?: string[];
  required_conditions?: string[];
}

/**
 * 回答に必要な根拠（#1284）。引用の chunk が書いた条件（文書・ページ・シート・行・章節・本文）を
 * すべて満たせば取れたとみなす（#2065・#2086）。label は Excel の「期待する根拠」の元の 1 行
 * （採点には使わない）。
 */
export interface EvaluationEvidence {
  id: string;
  document_id?: string | null;
  text?: string | null;
  page_start?: number | null;
  page_end?: number | null;
  section?: string | null;
  sheet?: string | null;
  row_start?: number | null;
  row_end?: number | null;
  label?: string | null;
}

/** 評価リクエストの Excel の誤り（#2065）。行・列はファイル全体の誤りでは null。 */
export interface EvaluationImportIssue {
  row: number | null;
  column: string | null;
  message: string;
}

/** 評価リクエストの Excel の 1 行のプレビュー（#2065）。 */
export interface EvaluationImportRow {
  row: number;
  case_id: string;
  question: string;
  evidence: string[];
  standard_answer_preview: string | null;
  has_standard_answer: boolean;
  /** 根拠も標準回答も無い行は false（資料に答えが無い質問として、回答を控えたかを測る）。 */
  expected_response_decision?: "answer" | "clarify" | "hold" | null;
  /** 案内すべき画面（Excel に書いた元の行。#2402。古い作業状態には無い）。 */
  required_answer_points?: string[];
  /** 回答の過程（#2402）。期待の下書きを作るときに下書きの API へ渡す。 */
  answer_process?: string | null;
}

/** 期待の下書きを作る 1 行（#2402）。 */
export interface EvaluationProcessRow {
  case_id: string;
  row: number;
  question: string;
  answer_process: string;
  standard_answer?: string;
}

/** 回答の過程に書いた資料と、ナレッジベースの文書との対応（業務ガイドの取り込みと同じ形）。 */
export interface EvaluationProcessReference {
  name: string;
  pages: string;
  section: string;
  status: "resolved" | "unresolved" | "ambiguous";
  document_id: string | null;
  file_name: string | null;
  candidates: string[];
}

/**
 * 回答の過程から作った評価の期待の下書き（#2402。保存しない）。画面で確かめてから、ケースの期待に足す。
 * error があるときは期待は空。
 */
export interface EvaluationProcessDraft {
  case_id: string;
  row: number;
  required_answer_points: string[];
  required_conditions: string[];
  expected_outcomes: EvaluationOutcome[];
  relevant_document_ids: string[];
  required_evidence: EvaluationEvidence[];
  references: EvaluationProcessReference[];
  warnings: string[];
  error: string | null;
}

/**
 * 評価リクエストの Excel の検証の結果（#2065。サーバーには保存しない）。誤りが 1 つでもあれば
 * cases は空。警告（#2086。形式に合わない位置・同じ名前の文書）があっても評価できる。
 * document_names は根拠に書いた文書の ID → ファイル名。
 */
export interface EvaluationImportResult {
  file_name: string;
  row_count: number;
  cases: EvaluationCase[];
  rows: EvaluationImportRow[];
  errors: EvaluationImportIssue[];
  error_count: number;
  warnings: EvaluationImportIssue[];
  warning_count: number;
  search_answer_profile_id?: string | null;
  knowledge_base_ids: string[];
  document_names: Record<string, string>;
  max_rows: number;
  max_bytes: number;
}

/** ダウンロードしたファイル（Excel のテンプレートなど）。 */
export interface DownloadedFile {
  blob: Blob;
  fileName: string;
}

export type EvaluationThresholds = Partial<Record<EvaluationMetricName, number | null>>;

/**
 * 画面からの評価の request（#2065）。評価の基準は常に設定の既定（suite・thresholds を送らない）、
 * 検索・回答の設定は選んだプロファイル。ケースは評価リクエストの Excel の検証の結果。
 */
export interface EvaluationRunRequestBody {
  cases: EvaluationCase[];
  search_answer_profile_id: string;
  /** 比べる検索・回答プロファイル（#2402）。同じケースをプロファイルの範囲でも流す。 */
  compare_search_answer_profile_id?: string | null;
}

/** 標準回答による LLM の評価の要約（詳細は回答の記録）。 */
export interface EvaluationAnswerJudgement {
  /** completed / error / input_too_large / timeout / unavailable */
  status: string;
  /** 評価の基準の指標がすべて閾値以上か（#680）。 */
  passed: boolean | null;
  claims_supported: boolean | null;
  requirement_coverage: number | null;
  missing_content: boolean | null;
  message: string | null;
}

/** 評価のケースの分類（#1226。答えられるかの種類）。 */
export type EvaluationCaseCategory =
  | "document_answerable"
  | "clarification_required"
  | "environment_data_required"
  | "knowledge_missing"
  | "conflicting_sources";

/** 分類ごとの結果の内訳（#1226）。率は測れたケースだけの割合で、測れなければ null。 */
export interface EvaluationCategorySummary {
 case_count:number; error_count:number;
 metrics:Partial<Record<EvaluationMetricName,number|null>>;
 metric_case_counts:Partial<Record<EvaluationMetricName,number>>;
}

/** 複数往復のケースの 1 回の回答の採点（#1284）。turn 0 は最初の質問への回答。 */
export interface EvaluationTurnResult {
  turn: number;
  trace_id: string;
  observed_outcome?: EvaluationOutcome | null;
  outcome_source?: string | null;
  handling_correct?: boolean | null;
  step_order_score?: number | null;
  missing_steps?: string[];
  forbidden_checked?: boolean;
  forbidden_hits?: string[];
  condition_coverage?: number | null;
  missing_conditions?: string[];
  reasked_conditions?: string[];
  elapsed_ms: number;
}

/** 区分ごとの結果（#1284）。metrics は全体と同じ指標をその区分のケースだけで求めた値。 */
export interface EvaluationSplitSummary {
  case_count: number;
  error_count: number;
  metrics: Partial<Record<string, number | null>>;
  metric_case_counts: Partial<Record<string, number>>;
  failure_reason_counts: Partial<Record<string, number>>;
}

/** 1 ケースの結果。測れない指標は null（保存済みの古い結果では欄が無いことがある）。 */
export interface EvaluationCaseResult extends Partial<Record<EvaluationMetricName, number | null>> {
  case_id: string; trace_id: string; status: "success"|"error";
  category?: EvaluationCaseCategory|null; split?: EvaluationCaseSplit|null;
  retrieved_document_ids: string[]; relevant_document_ids:string[]; hit_document_ids:string[];
  expected_decisions?: string[]; observed_outcome?: EvaluationOutcome|null; outcome_source?:string|null;
  missing_evidence?:string[]; answer_evaluation?:EvaluationAnswerJudgement|null;
  failure_reasons:string[]; diagnostics:SearchDiagnostics;
  elapsed_ms:number; error_type:string|null; error_stage?:string|null; error_message:string|null;
}

export interface EvaluationThresholdFailure {
  /** 保存済みの結果には削除した指標の名前が残ることがある。 */
  metric: string;
  actual: number;
  threshold: number;
}

/** 評価結果。各指標は測れたケースだけの平均で、測れなかった指標は null。 */
export type EvaluationMetrics = Partial<Record<EvaluationMetricName, number | null>> & {
 metrics_version?:string; case_count:number; error_count:number; evaluation_suite:string;
 metric_case_counts?:Partial<Record<EvaluationMetricName,number>>; passed:boolean|null;
 applied_thresholds?:Partial<Record<EvaluationMetricName,number>>;
 threshold_failures:EvaluationThresholdFailure[]; failure_reason_counts:Partial<Record<string,number>>;
 category_breakdown?:Partial<Record<string,EvaluationCategorySummary>>;
 split_breakdown?:Partial<Record<string,EvaluationSplitSummary>>;
 decision_breakdown?:Partial<Record<string,EvaluationCategorySummary>>;
 case_results:EvaluationCaseResult[]; comparison?:EvaluationComparison|null;
};

/** 比べるために検索・回答プロファイルの範囲で流した評価の結果（#2402）。 */
export interface EvaluationComparison {
  search_answer_profile_id: string;
  metrics: EvaluationMetrics;
}

// run: 品質評価。calibration: 判定の校正（#3578。結果は判定の校正の API で読む）。
export type EvaluationJobKind = "run" | "calibration";
export type EvaluationJobStatus = "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";

/**
 * 品質評価の job（#390）。進捗は終わったケースの数と今のケース。結果は成功のときだけ（`run_result`）。
 */
export interface EvaluationJob {
  job_id: string;
  kind: EvaluationJobKind;
  status: EvaluationJobStatus;
  total_cases: number;
  completed_cases: number;
  current_case_id: string | null;
  current_case_started_at: string | null;
  time_limit_seconds: number;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  run_result: EvaluationMetrics | null;
}

// --- 品質評価: 判定の校正（#3578） ---

export type CalibrationAspect = "requirement_coverage" | "answer_correctness" | "claim_support";
export type CalibrationLabel = "pass" | "fail" | "cannot_judge";
export type CalibrationRowStatus = "pending" | "agreed" | "disagreed" | "adjudicated";
export type CalibrationSplit = "dev" | "holdout" | "synthetic";

export interface CalibrationSourceJob {
  job_id: string;
  created_at: string;
  finished_at: string | null;
  case_count: number;
  compared: boolean;
}

export interface CalibrationAspectCriterion {
  aspect: CalibrationAspect;
  metric: string;
  criteria: string;
  requires_standard_answer: boolean;
}

export interface CalibrationErrorType {
  id: string;
  group: string;
  title: string;
  aspects: string;
  holdout_target: number;
}

export interface CalibrationAspectProgress {
  aspect: CalibrationAspect;
  rows: number;
  labeled: number;
  cannot_judge: number;
  holdout_rows: number;
  holdout_pass: number;
  holdout_fail: number;
}

export interface CalibrationProgress {
  aspects: CalibrationAspectProgress[];
  error_types: { id: string; holdout_count: number }[];
  holdout_target: number;
  label_target: number;
}

export interface CalibrationSplitResult {
  split: CalibrationSplit;
  total: number;
  judged: number;
  coverage: number | null;
  accuracy: number | null;
  kappa: number | null;
  false_pass_rate: number | null;
  false_fail_rate: number | null;
}

export interface CalibrationAspectResult {
  aspect: CalibrationAspect;
  passed: boolean;
  failures: string[];
  splits: CalibrationSplitResult[];
  variance: { runs: number; items: number; unstable: number; unstable_rate: number | null } | null;
}

export interface CalibrationSummary {
  calibration_id: string;
  label_set_id: string;
  status: EvaluationJobStatus;
  created_at: string;
  finished_at: string | null;
  repeats: number;
  judge_model_id: string | null;
  judge_rubric_version: string | null;
  prompt_sha256: string | null;
  labels_sha256: string | null;
  passed: boolean | null;
  error_message: string | null;
  aspects: CalibrationAspectResult[];
  current: boolean;
}

export interface LabelSetSummary {
  label_set_id: string;
  name: string;
  origin: "jobs" | "import";
  created_at: string;
  human_item_count: number;
  synthetic_item_count: number;
  row_count: number;
  annotator_count: number;
  my_labeled: number;
  resolved_rows: number;
  created_by_me: boolean;
  latest_calibration: CalibrationSummary | null;
}

export interface LabelSetListData {
  items: LabelSetSummary[];
  can_manage: boolean;
}

export interface LabelSetDetail {
  summary: LabelSetSummary;
  rubric_id: string;
  rubric_version: string | null;
  aspects: CalibrationAspectCriterion[];
  error_types: CalibrationErrorType[];
  my_code: string | null;
  my_progress: CalibrationProgress;
  final_progress: CalibrationProgress | null;
  can_manage: boolean;
  skipped: Record<string, number>;
  holdout_ratio: number | null;
}

export interface CalibrationEvidence {
  id: string;
  text: string;
}

export interface AnnotationRow {
  item_id: string;
  aspect: CalibrationAspect;
  split: "dev" | "holdout";
  question: string;
  answer: string;
  standard_answer: string | null;
  evidence: CalibrationEvidence[];
  label: CalibrationLabel | null;
  error_type: string | null;
  note: string | null;
}

export interface AnnotationRowsData {
  items: AnnotationRow[];
  total: number;
  offset: number;
  limit: number;
  unlabeled: number;
}

export interface CalibrationLabelInput {
  item_id: string;
  aspect: CalibrationAspect;
  label: CalibrationLabel | null;
  error_type?: string | null;
  note?: string | null;
}

export interface AnnotatorLabel {
  code: string;
  label: CalibrationLabel;
  error_type: string | null;
  note: string | null;
}

export interface AspectAgreement {
  aspect: CalibrationAspect;
  rows: number;
  agreed: number;
  disagreed: number;
  adjudicated: number;
  pending: number;
  pairs: { annotators: string[]; compared: number; agreement: number | null; kappa: number | null }[];
}

export interface AgreementRow {
  item_id: string;
  aspect: CalibrationAspect;
  split: "dev" | "holdout";
  status: CalibrationRowStatus;
  question: string;
  answer: string;
  standard_answer: string | null;
  evidence: CalibrationEvidence[];
  annotations: AnnotatorLabel[];
  final_label: CalibrationLabel | null;
  adjudication: AnnotatorLabel | null;
}

export interface AgreementData {
  annotators: string[];
  aspects: AspectAgreement[];
  rows: AgreementRow[];
  total: number;
  offset: number;
  limit: number;
}

export interface SyntheticItem {
  item_id: string;
  base_item_id: string;
  kind: string;
  description: string;
  fail_aspects: string[];
  answer: string;
  excluded: boolean;
}

export interface JudgeCalibrationAspectStatus {
  aspect: CalibrationAspect;
  metric: string;
  calibrated: boolean;
  kappa: number | null;
  calibration_id: string | null;
  calibrated_at: string | null;
  judge_rubric_version: string | null;
}

export interface JudgeCalibrationStatus {
  judge_model_id: string | null;
  prompt_sha256: string;
  aspects: JudgeCalibrationAspectStatus[];
}

export interface LabelSetCreateBody {
  name: string;
  job_ids: string[];
  include_synthetic: boolean;
}

export type AgreementFilter = "all" | "disagreed" | "pending" | "agreed" | "adjudicated";

// --- 設定: モデル ---
// --- 設定: データベース ---

export type SystemTableSchemaStatus =
  "missing" | "partial" | "outdated" | "ready";
export type SystemTableOperationStatus = "idle" | "running" | "failed";
export type SystemTableOperationKind = "initialize" | "recreate";
export type SystemTableOperationResult =
  "no_op" | "initialized" | "migrated" | "recreated";

export interface SystemTableObjectData {
  name: string;
  object_type: string;
}

export interface SystemTableMetadata {
  name: string;
  exists: boolean;
  estimated_rows: number | null;
  created_at: string | null;
  last_analyzed_at: string | null;
}

export interface SystemTableOperationState {
  status: SystemTableOperationStatus;
  operation_kind: SystemTableOperationKind | null;
  lease_expires_at: string | null;
  last_error_code: string | null;
  schema_epoch: number;
  updated_at: string | null;
}

export interface SystemTablesStatusData {
  status: SystemTableSchemaStatus;
  schema_version: string;
  schema_head: string;
  applied_versions: string[];
  pending_versions: string[];
  expected_object_count: number;
  existing_object_count: number;
  expected_table_count: number;
  existing_table_count: number;
  missing_objects: SystemTableObjectData[];
  retired_objects: SystemTableObjectData[];
  // 外部キーの差分（不足・参照先のない行は #505、削除規則の違い・無効化は #511）。
  missing_foreign_keys?: SystemTableForeignKeyData[];
  orphaned_foreign_keys?: SystemTableForeignKeyData[];
  mismatched_foreign_keys?: SystemTableForeignKeyData[];
  disabled_foreign_keys?: SystemTableForeignKeyData[];
  tables: SystemTableMetadata[];
  operation_state: SystemTableOperationState;
}

export interface SystemTableForeignKeyData {
  name: string;
  table_name: string;
  columns: string[];
  referenced_table_name: string;
  referenced_columns: string[];
  delete_rule: string;
  orphan_rows: number | null;
  current_name?: string | null;
  current_delete_rule?: string | null;
}

export interface SystemTablesInitializeRequest {
  recreate: boolean;
  confirmation?: string;
}

export interface SystemTablesOperationData extends SystemTablesStatusData {
  operation: SystemTableOperationResult;
  dropped_object_count: number;
  created_object_count: number;
}

/** 参照先のない行の削除（#511）。`expected_orphan_rows` は利用者が確認した件数。 */
export interface SystemTablesDeleteOrphansRequest {
  constraint_name: string;
  expected_orphan_rows: number;
}

export interface SystemTablesOrphanDeletionData extends SystemTablesStatusData {
  operation: "no_op" | "orphans_deleted";
  deleted_row_count: number;
  foreign_key: SystemTableForeignKeyData;
}

// --- 設定: HuggingFace モデルダウンロード ---
export interface HuggingFaceSettingsData {
  endpoint: string;
  token_configured: boolean;
  config_source: "runtime";
}

export interface HuggingFaceSettingsUpdate {
  endpoint: string;
  token?: string;
  clear_token?: boolean;
}

// --- 設定: アップロード保存先 ---

// --- 設定: Parser adapter ---
export interface ParserAdapterStatusData {
  backend: ParserAdapterBackendName;
  package_name: string;
  import_name: string;
  distribution_name: string | null;
  install_package: string;
  enabled: boolean;
  selected: boolean;
  installed: boolean;
  status: ParserAdapterStatus;
  version: string | null;
  warning_code: string | null;
}

export interface ParserAdapterScorecardEntryData {
  backend: ParserAdapterScoreBackend;
  rank: number;
  score: number;
  status: ParserAdapterScoreStatus;
  recommended: boolean;
  executable: boolean;
  selected: boolean;
  enabled: boolean;
  installed: boolean;
  metric_source: string;
  metric_count: number;
  signals: Record<string, number>;
  reason_codes: string[];
  warning_codes: string[];
}

export interface ParserAdapterScorecardData {
  selected_backend: ParserAdapterBackend;
  recommended_backend: ParserAdapterScoreBackend;
  metrics_source: string;
  metrics_applied_to: ParserAdapterScoreBackend | null;
  entries: ParserAdapterScorecardEntryData[];
}

export interface ParserAdapterSourceRouteData {
  source_kind: ParserAdapterSourceKind | string;
  candidate_order: ParserAdapterScoreBackend[];
  attempted_order: ParserAdapterScoreBackend[];
  active_order: ParserAdapterScoreBackend[];
  selected_backend: ParserAdapterScoreBackend;
  reason_codes: string[];
  warning_codes: string[];
}

export interface ParserAdapterBackendSourceMatrixData {
  evidence_source: "runtime_routes";
  required_source_kinds: string[];
  covered_source_kinds: string[];
  missing_source_kinds: string[];
  backend_source_kinds: Partial<Record<ParserAdapterScoreBackend, string[]>>;
  route_evidence: ParserAdapterSourceRouteData[];
}

export interface ParserBackendCapabilityData {
  backend: string;
  modalities: string[];
  extensions: string[];
}

export interface ParserAdapterSettingsData {
  adapter_backend: ParserAdapterBackend;
  effective_order: ParserAdapterBackendName[];
  adapters: ParserAdapterStatusData[];
  scorecard: ParserAdapterScorecardData;
  source_routes: ParserAdapterSourceRouteData[];
  backend_source_kind_matrix: ParserAdapterBackendSourceMatrixData;
  capabilities: ParserBackendCapabilityData[];
  /** 「解析後の処理」の全体の既定（#528）。保存先は backend/.env。 */
  vision_enabled: boolean;
  /** 見出しの階層の推定（AI。#2277）。 */
  heading_level_inference_enabled?: boolean;
  field_extraction_enabled: boolean;
  navigation_summary_enabled: boolean;
  /** 1 文書の取込で要約する章節数の実効上限。読み取り専用。 */
  navigation_summary_max_nodes?: number;
  raptor_enabled?: boolean;
  /** 図・表の説明（Vision）を同時に呼ぶ数の上限（RAG_VISION_CONCURRENCY。1〜16。#2002）。 */
  vision_concurrency?: number;
  config_source: "runtime";
}

/** `adapter_backend` を省略すると解析エンジンの設定は変えない（「解析後の処理」だけの保存。#528）。 */
export interface ParserAdapterSettingsUpdate {
  adapter_backend?: ParserAdapterBackend;
  docling_enabled?: boolean;
  unstructured_enabled?: boolean;
  vision_enabled?: boolean;
  heading_level_inference_enabled?: boolean;
  field_extraction_enabled?: boolean;
  navigation_summary_enabled?: boolean;
  raptor_enabled?: boolean;
  vision_concurrency?: number;
}

/** 設定の概要: 工程の自動進行と、レシピ 11 項目の全体の既定（#528）。 */
export interface PipelineSettingsData {
  auto_parse_after_preprocess_enabled: boolean;
  auto_chunk_after_extract_enabled: boolean;
  auto_index_after_chunk_enabled: boolean;
  /** レシピで何も上書きしないときの実効値（レシピの「グローバル設定に従う」と同じ）。 */
  recipe_defaults: DocumentProcessingConfig;
  /** 検索インデックス画面と同じ、保存済みの検索精度。文書レシピでは上書きしない。 */
  vector_index_profile: VectorIndexProfileName;
  config_source: "runtime";
}

export type PipelineAutoAdvanceField =
  | "auto_parse_after_preprocess_enabled"
  | "auto_chunk_after_extract_enabled"
  | "auto_index_after_chunk_enabled";

export type PipelineSettingsUpdate = Partial<Record<PipelineAutoAdvanceField, boolean>>;

// --- 設定: Chunking アダプター ---
export type ChunkingStrategyName =
  | "structure_aware"
  | "recursive_character"
  | "small_to_big"
  | "markdown_heading"
  | "page_level"
  | "fixed_size"
  | "fixed_delimiter";

// --- 設定: 前処理(Preprocess)アダプター ---
export type PreprocessProfileName =
  | "passthrough"
  | "office_to_pdf"
  | "csv_to_jsonl"
  | "excel_to_jsonl"
  | "word_to_jsonl";

export interface PreprocessProfileStatusData {
  name: PreprocessProfileName;
  origin: string;
  recommended_for: string[];
  selected: boolean;
  in_process: boolean;
  requires_service: boolean;
  available: boolean;
}

export interface PreprocessSettingsData {
  profile: PreprocessProfileName;
  service_enabled: boolean;
  service_url: string;
  canonical_artifact_prefix: string;
  profiles: PreprocessProfileStatusData[];
  config_source: "runtime";
}

export interface PreprocessSettingsUpdate {
  profile: PreprocessProfileName;
}

// --- サービス管理（各工程のサービスの稼働可視化・起動/停止）---
// カテゴリと工程の設定画面の対応は components/settings/service-stages.ts（並びはサイドナビ。#638）。
export type ServiceCategory = "preprocess" | "parser";
export type ServiceProfile = "cpu" | "gpu" | "oci";
// systemd の unit の状態 + /health（#286）。starting は unit が動いているが /health にまだ届かない、
// failed は unit が失敗して止まった、not_installed は unit が登録されていない。
export type ServiceRuntimeStatus =
  | "running"
  | "degraded"
  | "starting"
  | "failed"
  | "stopped"
  | "not_installed"
  | "unconfigured";
export type ServiceExecutionPolicy = "selected_adapter" | "default_adapter";
// 起動 = systemctl enable --now、停止 = systemctl disable --now、再起動 = systemctl restart。
export type ServiceAction = "start" | "stop" | "restart";

export interface ServiceModelCacheData {
  /** サービスの実行ユーザーのキャッシュ親ディレクトリ（~/.cache）。 */
  path: string;
  editable: false;
}

export interface ServiceCatalogItemData {
  service_id: string;
  category: ServiceCategory;
  profile: ServiceProfile;
  label_key: string;
  execution_policy: ServiceExecutionPolicy;
  configured: boolean;
  /** 起動/停止・ログに使う systemd の unit 名。 */
  systemd_unit: string;
  model_cache: ServiceModelCacheData | null;
}

export interface ServiceStatusData extends ServiceCatalogItemData {
  status: ServiceRuntimeStatus;
}

export type DeploymentMode = "local" | "staging" | "production";

export interface ServiceCatalogData {
  control_enabled: boolean;
  deployment_mode: DeploymentMode;
  services: ServiceCatalogItemData[];
}

export interface ServiceControlResultData {
  service_id: string;
  action: ServiceAction;
  status: ServiceRuntimeStatus;
}

export type ServiceLogsSource = "journald";

export interface ServiceLogsData {
  service_id: string;
  source: ServiceLogsSource;
  lines: number;
  content: string;
}

export interface ChunkingStrategyStatusData {
  name: ChunkingStrategyName;
  origin: string;
  recommended_for: string[];
  selected: boolean;
}

/** 言語資源（#2181）。項目は「節 → 項目 → 値」（値は語の一覧か選択の文字列）。 */
export type LanguagePackFieldKind =
  | "words"
  | "pairs"
  | "number_patterns"
  | "patterns"
  | "char_values"
  | "word_map"
  | "enum_map"
  | "char_ranges"
  | "choice"
  | "text";

export interface LanguagePackField {
  section: string;
  key: string;
  kind: LanguagePackFieldKind;
  /** enum_map の値・choice の選択肢。 */
  choices: string[];
  /** 取込のときに使う書き方か（変えたら文書の再分割が要る）。 */
  ingest: boolean;
  /** 1 つの語の長さの上限（回答の定型文のような文の項目は長い）。 */
  max_chars: number;
  /** 数値マッピングの値の下限。 */
  min_value?: number;
}

export type LanguagePackValue = string[] | string;
export type LanguagePackSections = Record<string, Record<string, LanguagePackValue>>;

export interface LanguagePackData {
  fields: LanguagePackField[];
  builtin: LanguagePackSections;
  overrides: LanguagePackSections;
  effective: LanguagePackSections;
  hash: string;
  /** 今の言語資源で分割していない文書の数（分割し直しが要る。DB に届かなければ null）。 */
  outdated_documents?: number | null;
  unrecorded_documents?: number | null;
  current_release?: LanguagePackRelease;
}

export interface QuestionUnderstandingPreviewData {
  original_text: string;
  saved: { inquiry: Record<string, unknown>; task_contract: Record<string, unknown>; hash: string };
  draft: { inquiry: Record<string, unknown>; task_contract: Record<string, unknown>; hash: string };
}

export interface LanguagePackPreviewResult {
  references: { label: string; kind: string; key: string; document_title: string | null }[];
  title_keys: string[];
  heading_keys: string[];
}

export interface LanguagePackPreviewData {
  saved: LanguagePackPreviewResult;
  draft: LanguagePackPreviewResult;
}

export interface LanguagePackRelease {
  id: string;
  version: number;
  effective: LanguagePackSections;
  hash: string;
  ingest_hash: string;
  published_at: string;
  published_by: string | null;
  previous_id: string | null;
  restored_from: string | null;
  change_note: string;
}

export interface LanguagePackReleaseState {
  draft_revision: number;
  draft_effective: LanguagePackSections;
  current: LanguagePackRelease;
}

export interface LanguagePackReleaseList {
  releases: LanguagePackRelease[];
  total: number;
}

export interface LanguagePackPublishRequest {
  expected_revision: number;
  expected_current_id: string;
  change_note: string;
}

export interface LanguagePackReleaseComparison {
  before_id: string;
  after_id: string;
  differences: { section: string; key: string; before: LanguagePackValue | null; after: LanguagePackValue | null; ingest: boolean }[];
  ingest_changed: boolean;
  impact_requires_review: boolean;
  affected_stages: string[];
}

export interface ChunkingSettingsData {
  strategy: ChunkingStrategyName;
  chunk_size: number;
  overlap: number;
  min_chars: number;
  delimiter: string;
  context_header_enabled: boolean;
  /** 親子階層（small-to-big）の分割パラメータ(子と親の大きさの 5 項目)。 */
  chunk_child_target_chars: number;
  chunk_table_child_target_chars: number;
  chunk_parent_target_chars: number;
  chunk_parent_max_pages: number;
  chunk_parent_max_children: number;
  strategies: ChunkingStrategyStatusData[];
  config_source: "runtime";
}

export interface ChunkingSettingsUpdate {
  strategy: ChunkingStrategyName;
  chunk_size: number;
  overlap: number;
  min_chars: number;
  delimiter: string;
  /** 親子階層（small-to-big）の分割パラメータ(子と親の大きさの 5 項目)。 */
  chunk_child_target_chars: number;
  chunk_table_child_target_chars: number;
  chunk_parent_target_chars: number;
  chunk_parent_max_pages: number;
  chunk_parent_max_children: number;
}

/** 回答の検索と生成の全体既定(全体設定だけで決め、検索・回答プロファイルでは上書きしない。#593)。 */
export interface AnsweringSettingsData {
  answer_audit_mode: "none" | "standard" | "detailed";
  answer_evidence_refinement_mode: "legacy" | "cited_only" | "cited_required";
  query_strategy: QueryStrategyName;
  answer_flow: AnswerFlowName;
  neighbor_child_count: number;
  rerank_enabled: boolean;
  screen_linking_enabled: boolean;
  request_coverage_retrieval_enabled: boolean;
  auto_field_filter_enabled: boolean;
  /** 関係情報で根拠の候補を広げる(#2055。既定 false)。 */
  graph_expansion_enabled: boolean;
  entity_expansion_enabled: boolean;
  entity_expansion_max_chunks: number;
  section_expansion_enabled: boolean;
  section_expansion_max_levels: number;
  section_expansion_input_token_percent: number;
  config_source: "runtime";
}

export type AnsweringSettingsUpdate = Partial<Omit<AnsweringSettingsData, "config_source">>;

/** 回答の記録の保持日数(0 は無期限)。 */
export interface AnswerRecordSettingsData {
  retention_days: number;
  config_source: "runtime";
}

/** メタデータ/項目抽出のスキーマ定義(取込・検索・回答設定)。 */
export type ExtractionFieldValueType = "string" | "number" | "date" | "bool";

export interface ExtractionFieldDefinition {
  name: string;
  description: string;
  value_type: ExtractionFieldValueType;
}

/** 全体の既定の項目の定義。uses_standard なら一度も保存しておらず、fields は標準の項目（#556）。 */
export interface ExtractionFieldsSettingsData {
  fields: ExtractionFieldDefinition[];
  uses_standard: boolean;
}

/** 検索の絞り込みに使える項目（#549）。 */
export interface SearchExtractionFieldsData {
  fields: ExtractionFieldDefinition[];
}

/** ナレッジベースの項目抽出の定義（#548）。inherits_default なら fields は全体の既定。 */
export interface KnowledgeBaseExtractionFieldsData {
  inherits_default: boolean;
  fields: ExtractionFieldDefinition[];
}

// --- 設定: Vector Index アダプター ---
export type VectorIndexProfileName = "balanced" | "accurate" | "fast";
/** 実際の索引と推奨ビルドの比較結果(backend の判定。#562)。unknown = 実際の値を確認できない。 */
export type VectorIndexBuildStatus = "match" | "reprovision" | "unknown";

export interface VectorIndexProfileStatusData {
  name: VectorIndexProfileName;
  origin: string;
  recommended_for: string[];
  selected: boolean;
  target_accuracy: number;
  neighbors: number;
  efconstruction: number;
  distance: string;
  index_status: VectorIndexBuildStatus;
}

export interface VectorIndexSettingsData {
  profile: VectorIndexProfileName;
  target_accuracy: number;
  neighbors: number;
  efconstruction: number;
  distance: string;
  requires_reprovision: boolean;
  index_status: VectorIndexBuildStatus;
  /** 実際の索引の値。確認できないときは null。 */
  actual_neighbors: number | null;
  actual_efconstruction: number | null;
  profiles: VectorIndexProfileStatusData[];
  reindex_sql: string;
  config_source: "runtime";
}

export interface VectorIndexSettingsUpdate {
  profile: VectorIndexProfileName;
}

// --- 設定: Evaluation アダプター ---
/** 評価の基準（閾値のプリセット。#591）。 */
export type EvaluationSuiteName = "standard" | "strict";

export interface EvaluationSuiteStatusData {
  name: EvaluationSuiteName;
  origin: string;
  recommended_for: string[];
  selected: boolean;
  thresholds: Record<string, number>;
}

export interface EvaluationSettingsData {
  suite: EvaluationSuiteName;
  thresholds: Record<string, number>;
  suites: EvaluationSuiteStatusData[];
  config_source: "runtime";
}

export interface EvaluationSettingsUpdate {
  suite: EvaluationSuiteName;
}

// --- 設定: 文書構造索引の構築 ---
/** off = 構築しない(既定)、entities = 文書と章・節の見出しのつながりを構築する(#621)。 */
export type GraphProfileName = "off" | "entities";

export interface GraphProfileStatusData {
  name: GraphProfileName;
  selected: boolean;
}

export interface EntityIndexSettingsData {
  enabled: boolean;
}

export interface GraphSettingsData {
  profile: GraphProfileName;
  enabled: boolean;
  profiles: GraphProfileStatusData[];
  config_source: "runtime";
}

export interface GraphSettingsUpdate {
  profile: GraphProfileName;
}

// --- 設定: OCI config ---

/** API 由来のエラー。`messages` は日本語のユーザー向け文言。 */
/** 入力項目に結び付く API の問題（JSON Pointer と表示文言）。 */
export interface ApiFieldError {
  pointer: string;
  message: string;
  /** 入力の検証エラー（422）の Pydantic の種別と技術的な原文（「詳細」に出す。#1065）。 */
  code?: string;
  raw_location?: string;
  raw_message?: string;
}

export interface ApiErrorDetails {
  /** 機械判定用のエラーコード（共通認証・ユーザー / ロール操作の `error_code`）。 */
  errorCode?: string;
  fieldErrors?: ApiFieldError[];
  requestId?: string;
  /** 応答が届かなかった失敗（timeout・通信断）の `ApiTransportError`（#906）。 */
  cause?: ApiTransportError;
  /**
   * 「詳細」に足す技術的な項目（stream の失敗の trace_id・エラーの種類・工程・理由など。#2023）。
   * HTTP ステータス・エラーコード・request ID の後ろに並べる。
   */
  extraDetails?: ApiErrorDetail[];
}

export class ApiError extends Error implements ApiErrorPresentable {
  readonly status: number;
  readonly messages: string[];
  readonly errorCode?: string;
  readonly fieldErrors: ApiFieldError[];
  readonly requestId?: string;
  readonly extraDetails: ApiErrorDetail[];
  /** 応答に理由がなく、既定の文言（「APIエラー (状態コード)」）にしたか。文言の一致で判定しない。 */
  readonly isFallbackMessage: boolean;

  constructor(status: number, messages: string[], details: ApiErrorDetails = {}) {
    const isFallbackMessage = messages.length === 0;
    const resolved = isFallbackMessage
      ? [t("common.apiError", { status })]
      : messages;
    super(resolved[0], details.cause ? { cause: details.cause } : undefined);
    this.name = "ApiError";
    this.status = status;
    this.messages = resolved;
    this.isFallbackMessage = isFallbackMessage;
    this.errorCode = details.errorCode;
    this.fieldErrors = details.fieldErrors ?? [];
    this.requestId = details.requestId;
    this.extraDetails = (details.extraDetails ?? []).filter((item) => item.value.trim());
  }

  /** 失敗の面の要約と「詳細」（共通の `ApiErrorBanner` / `presentApiError`。#906）。 */
  toApiErrorPresentation(labels?: ApiErrorDetailLabels): ApiErrorPresentation {
    const presentation = httpApiErrorPresentation(this, labels);
    return { ...presentation, details: [...presentation.details, ...this.extraDetails] };
  }
}

/**
 * 応答が届かなかった失敗（timeout・通信断）を `ApiError` にする（#906）。
 *
 * 画面の多くは `error instanceof ApiError ? error.message : 既定の文` で失敗を出すため、`message` を
 * 利用者向けの日本語（何が起きたか + 次の操作）にした `ApiError` で投げる。ブラウザの英語の文
 * （`Failed to fetch` など）は `cause` の `ApiTransportError` に残し、`ApiErrorBanner` の「詳細」にだけ出す。
 * status は timeout が 408、通信断が 0（応答なし）。
 */
export function apiErrorFromTransport(transport: ApiTransportError): ApiError {
  return new ApiError(transport.kind === "timeout" ? 408 : 0, [transport.message], {
    cause: transport,
  });
}

/**
 * fetch・本文の読み取りが投げた例外が timeout・通信断なら、利用者向けの `ApiError` を返す。
 * 利用者の中止（`AbortError`）などは `null` を返し、呼び出し側はそのまま投げる。stream の直接 fetch も使う。
 */
export function apiErrorFromFetchFailure(
  cause: unknown,
  request: ApiTransportRequest,
): ApiError | null {
  if (cause instanceof ApiError) return cause;
  const transport = toApiTransportError(cause, request);
  return transport ? apiErrorFromTransport(transport) : null;
}

interface ErrorEnvelope {
  error_messages?: unknown;
  error_code?: unknown;
  problem?: { field_errors?: unknown; request_id?: unknown } | null;
}

function fieldErrorsOf(value: unknown): ApiFieldError[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const { pointer, message } = record;
    if (typeof message !== "string") return [];
    const fieldError: ApiFieldError = { pointer: typeof pointer === "string" ? pointer : "", message };
    for (const key of ["code", "raw_location", "raw_message"] as const) {
      const value = record[key];
      if (typeof value === "string" && value) fieldError[key] = value;
    }
    return [fieldError];
  });
}

/** エラー応答（ApiResponse envelope）から ApiError を作る。stream の直接 fetch も使う。 */
export function apiErrorFromEnvelope(
  status: number,
  envelope: unknown,
  requestId?: string | null,
): ApiError {
  const body = (envelope && typeof envelope === "object" ? envelope : {}) as ErrorEnvelope;
  const messages = Array.isArray(body.error_messages)
    ? body.error_messages.filter((item): item is string => typeof item === "string")
    : [];
  const problemRequestId =
    typeof body.problem?.request_id === "string" ? body.problem.request_id : undefined;
  return new ApiError(status, messages, {
    errorCode: typeof body.error_code === "string" ? body.error_code : undefined,
    fieldErrors: fieldErrorsOf(body.problem?.field_errors),
    requestId: requestId || problemRequestId,
  });
}

/** 状態を変える method のとき Cookie の CSRF token を `X-CSRF-Token` として付けた headers を作る。 */
export function withCsrfHeaders(method: string | undefined, headers?: HeadersInit): Headers {
  const merged = new Headers(headers);
  for (const [name, value] of Object.entries(csrfHeader(method ?? "GET"))) {
    merged.set(name, value);
  }
  return merged;
}

/**
 * 応答の 401 / 403 を共通の認証イベント（ログインへ / 権限なしの画面へ）として通知する。
 * 403 は error_code が経路の権限拒否のときだけ権限なしの画面へ移し、検索・回答プロファイル / KB の範囲外
 * （`RAG_SCOPE_FORBIDDEN`）や権限の付与の制限などは呼び出した画面がその場で表示する（#224）。
 * 本文を読む前に呼ぶ（本文は消費しない）。
 */
export function notifyResponseAuthStatus(res: Response): void {
  void notifyAuthResponse(res);
}

function resolveTimeoutMs(value: unknown, fallbackMs: number): number {
  const parsed = typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallbackMs;
}

function runtimeApiTimeoutOverrideMs(): number | null {
  if (typeof window === "undefined") return null;
  const value = Number(
    (window as unknown as { __RAG_API_TIMEOUT_MS__?: string | number })
      .__RAG_API_TIMEOUT_MS__,
  );
  return Number.isFinite(value) && value > 0 ? value : null;
}

async function parseEnvelope<T>(res: Response): Promise<ApiResponse<T>> {
  try {
    return (await res.json()) as ApiResponse<T>;
  } catch (cause) {
    // 本文の読み取り中の中止・timeout・通信断は、空の成功にせず呼び出し側で失敗にする（#906）。
    if (cause instanceof Error && cause.name !== "SyntaxError") throw cause;
    return { data: null, error_messages: [], warning_messages: [] };
  }
}

interface RequestOptions {
  timeoutMs?: number;
}

/**
 * ApiResponse エンベロープを取得し、エラー時は ApiError を投げる。
 * Cookie セッションの CSRF header の付与と、401 / 403 の認証イベントの通知もここで行う（#214）。
 */
async function requestEnvelope<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<ApiResponse<T>> {
  const timeoutMs =
    runtimeApiTimeoutOverrideMs() ??
    options.timeoutMs ??
    API_REQUEST_TIMEOUT_MS;
  const controller = new AbortController();
  const externalSignal = init?.signal;
  let timedOut = false;
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  const abortFromExternal = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) {
    abortFromExternal();
  } else {
    externalSignal?.addEventListener("abort", abortFromExternal, {
      once: true,
    });
  }

  if (timeoutMs > 0) {
    timeoutId = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  }

  const headers = withCsrfHeaders(init?.method, init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  try {
    // 送る URL には配信の base を付ける（`/rag/` の下で配信するとき。#1316）。エラーの表示の path は付けない。
    const res = await fetch(appPath(path), {
      ...init,
      credentials: "same-origin",
      signal: controller.signal,
      headers,
    });
    const envelope = await parseEnvelope<T>(res);
    if (!res.ok) {
      const error = apiErrorFromEnvelope(res.status, envelope, res.headers.get("X-Request-ID"));
      // 本文は読み終えているので、読み取った error_code で通知する（#224）。
      notifyAuthStatus(res.status, error.requestId, error.errorCode);
      throw error;
    }
    return envelope;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    const request = { method: (init?.method ?? "GET").toUpperCase(), path, timeoutMs };
    if (timedOut) {
      throw apiErrorFromTransport(new ApiTransportError("timeout", request, error));
    }
    if (isAbortError(error)) throw error;
    throw apiErrorFromFetchFailure(error, request) ?? error;
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}

/** ApiResponse を展開し data のみ返す。エラー時は ApiError を投げる。 */
export async function request<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  const envelope = await requestEnvelope<T>(path, init, options);
  return envelope.data as T;
}

/**
 * DB 停止時に縮退応答(空 data + warning_messages)を返す閲覧系 API 用。
 * data オブジェクトへ `warning_messages` を併設して返すため、既存の
 * data アクセス(`page.items` 等)を壊さずに縮退状態を画面へ伝えられる。
 */
async function requestDegradable<T extends object>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<Degradable<T>> {
  const envelope = await requestEnvelope<T>(path, init, options);
  return {
    ...(envelope.data as T),
    warning_messages: envelope.warning_messages ?? [],
  };
}

/** 通信の失敗と確定した削除失敗を分け、通信の失敗後に文書の残存を確認する。 */
async function deleteDocumentWithConfirmation(id: string, fileName?: string): Promise<Degradable<DocumentDeleteResult>> {
  const path = `/api/documents/${encodeURIComponent(id)}`;
  try {
    return await requestDegradable<DocumentDeleteResult>(path, {method: "DELETE"}, {timeoutMs: DOCUMENT_DELETE_TIMEOUT_MS});
  } catch (error) {
    if (!(error instanceof ApiError) || ![0, 404, 408, 500, 502, 503, 504].includes(error.status)) throw error;
    // DELETE は再送しない。存在確認だけを行い、削除済みと確認できたときだけ一覧から外す。
    for (let attempt = 0; attempt < 10; attempt++) {
      try {
        await request<DocumentDetail>(path, undefined, {timeoutMs: 5_000});
      } catch (lookupError) {
        if (lookupError instanceof ApiError && lookupError.status === 404) {
          return {
            id, file_name: fileName ?? id, object_storage_path: null, object_deleted: false,
            artifact_deleted_count: 0, artifact_delete_failed_count: 0,
            warning_messages: [t("fileList.delete.confirmedAfterTransport")],
          };
        }
        if (lookupError instanceof ApiError && [401, 403].includes(lookupError.status)) throw lookupError;
      }
      if (attempt < 9) await new Promise<void>((resolve) => setTimeout(resolve, 2_000));
    }
    throw new ApiError(0, [t("fileList.delete.unconfirmed")], {errorCode: "document_delete_unconfirmed"});
  }
}

/** アップロードの送信済みバイト数（multipart の本文全体に対する値）。 */
export interface UploadTransferProgress {
  loaded: number;
  total: number;
}

/**
 * 文書アップロード用の multipart 送信（#306）。
 *
 * `fetch` では送信（request body）の進み具合を取れないため、XHR の `upload.onprogress` で送信済みの
 * バイト数を通知する。CSRF header・401 / 403 の認証イベント・エラーの ApiError 化は `requestEnvelope` と
 * 同じにする。大きなファイルの送信・保存は既定の 30 秒を超えるため、時間では打ち切らない（#280。途中で
 * 打ち切ると、backend では保存済みなのに画面は失敗と表示し、再送で同じ文書が二重に登録される）。
 */
function requestUpload<T>(
  path: string,
  body: FormData,
  onUploadProgress?: (progress: UploadTransferProgress) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", appPath(path));
    const headers = withCsrfHeaders("POST");
    if (!headers.has("Accept")) headers.set("Accept", "application/json");
    headers.forEach((value, name) => xhr.setRequestHeader(name, value));
    if (onUploadProgress) {
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && event.total > 0) {
          onUploadProgress({ loaded: event.loaded, total: event.total });
        }
      });
    }
    xhr.addEventListener("load", () => {
      let envelope: ApiResponse<T>;
      try {
        envelope = JSON.parse(xhr.responseText) as ApiResponse<T>;
      } catch {
        envelope = { data: null, error_messages: [], warning_messages: [] } as ApiResponse<T>;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        const error = apiErrorFromEnvelope(
          xhr.status,
          envelope,
          xhr.getResponseHeader("X-Request-ID"),
        );
        notifyAuthStatus(xhr.status, error.requestId, error.errorCode);
        reject(error);
        return;
      }
      resolve(envelope.data as T);
    });
    // 接続の失敗は fetch の通信断と同じく、利用者向けの文の ApiError にする（#906）。
    xhr.addEventListener("error", () =>
      reject(
        apiErrorFromTransport(
          new ApiTransportError(
            "network",
            { method: "POST", path },
            new TypeError("upload request failed"),
          ),
        ),
      ),
    );
    xhr.addEventListener("abort", () => reject(new DOMException("upload aborted", "AbortError")));
    xhr.send(body);
  });
}

/**
 * ファイルを取得する（Excel のテンプレートなど。#2065）。失敗は JSON の envelope を ApiError にする。
 * 認証の通知・CSRF・配信の base は requestEnvelope と同じ。ファイル名は Content-Disposition から取る。
 */
async function requestFile(
  path: string,
  fallbackName: string,
  options: RequestInit = {},
): Promise<DownloadedFile> {
  const headers = withCsrfHeaders(options.method ?? "GET", options.headers);
  const res = await fetch(appPath(path), { ...options, credentials: "same-origin", headers });
  if (!res.ok) {
    const envelope = await parseEnvelope<null>(res);
    const error = apiErrorFromEnvelope(res.status, envelope, res.headers.get("X-Request-ID"));
    notifyAuthStatus(res.status, error.requestId, error.errorCode);
    throw error;
  }
  return { blob: await res.blob(), fileName: contentDispositionFileName(res) ?? fallbackName };
}

/** Content-Disposition の filename（無ければ null）。 */
export function contentDispositionFileName(res: Pick<Response, "headers">): string | null {
  const header = res.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.trim());
    } catch {
      // 読めない名前は plain の filename に任せる。
    }
  }
  return /filename="?([^";]+)"?/i.exec(header)?.[1]?.trim() || null;
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function jsonPutBody(body: unknown): RequestInit {
  return { ...jsonBody(body), method: "PUT" };
}

function labelSetPath(labelSetId: string): string {
  return `/api/evaluation/calibration/label-sets/${encodeURIComponent(labelSetId)}`;
}

function supportGuidesPath(searchAnswerProfileId: string): string {
  return `/api/search-answer-profiles/${encodeURIComponent(searchAnswerProfileId)}/support-guides`;
}

function ingestionJobSearch(force: boolean, phase: IngestionJobPhase): string {
  const search = new URLSearchParams();
  if (force) search.set("force", "true");
  search.set("phase", phase);
  return search.toString();
}

/**
 * 共通のシステム設定の取得の options。共有の画面は TanStack Query の `signal` を渡し、
 * 画面を離れたら取得を止める（#1117）。
 */
export interface SettingsRequestOptions {
  signal?: AbortSignal;
}

export const api = {
  // 認証・ユーザー / ロール・権限管理は securityApi（lib/security-api.ts。#214）。

  // データベース利用可否(設定の有無 + 実接続プローブ)。DB ゲートが参照する。
  getDatabaseStatus: () => request<DatabaseStatusData>("/api/ready/database"),

  // ドキュメント
  listDocuments: (
    params: {
      status?: FileStatus;
      /** レシピ1の取込の job が順番待ちの段（#2677）。`status` とは同時に渡さない。 */
      queued_phase?: IngestionJobPhase;
      q?: string;
      knowledge_base_id?: string;
      language_pack_relation?: LanguagePackRelation;
      current_release_id?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    if (params.queued_phase) search.set("queued_phase", params.queued_phase);
    if (params.q) search.set("q", params.q);
    if (params.knowledge_base_id)
      search.set("knowledge_base_id", params.knowledge_base_id);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.offset != null) search.set("offset", String(params.offset));
    if (params.language_pack_relation) search.set("language_pack_relation", params.language_pack_relation);
    if (params.current_release_id) search.set("current_release_id", params.current_release_id);
    const qs = search.toString();
    return requestDegradable<Page<DocumentSummary>>(
      `/api/documents${qs ? `?${qs}` : ""}`,
    );
  },
  prepareLanguagePackReprocessing: (document_ids: string[], expected_release_id: string) =>
    request<LanguagePackReprocessingTargets>("/api/documents/language-pack/reprocessing-targets", jsonBody({ document_ids, expected_release_id })),
  enqueueLanguagePackReprocessing: (expected_release_id: string, targets: { document_id: string; recipe_id: string; phase: IngestionJobPhase; expected_config_revision: number }[]) =>
    request<LanguagePackReprocessingResult>("/api/documents/language-pack/reprocessing-jobs", jsonBody({ expected_release_id, targets })),
  getDocumentCitationChunk: (id: string, chunkId: string) =>
    request<RetrievedChunk>(`/api/documents/${encodeURIComponent(id)}/chunks/${encodeURIComponent(chunkId)}`),
  getDocument: (id: string) =>
    request<DocumentDetail>(`/api/documents/${encodeURIComponent(id)}`),
  listDocumentChunkSets: (id: string) =>
    request<DocumentChunkSet[]>(
      `/api/documents/${encodeURIComponent(id)}/chunk-sets`,
    ),
  listDocumentRecipes: (id: string) =>
    request<DocumentRecipeView[]>(
      `/api/documents/${encodeURIComponent(id)}/recipes`,
    ),
  createDocumentRecipe: (id: string, copyFromRecipeId: string | null) =>
    request<DocumentRecipeView>(
      `/api/documents/${encodeURIComponent(id)}/recipes`,
      jsonBody({ copy_from_recipe_id: copyFromRecipeId }),
    ),
  updateDocumentRecipe: (
    id: string,
    recipeId: string,
    body: DocumentProcessingConfig,
  ) =>
    request<DocumentRecipeView>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(recipeId)}`,
      { ...jsonBody(body), method: "PUT" },
    ),
  deleteDocumentRecipe: (id: string, recipeId: string) =>
    request<DocumentRecipeDeleteResult>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(recipeId)}`,
      { method: "DELETE" },
    ),
  listDocumentRecipeChunks: (id: string, recipeId: string) =>
    request<DocumentChunkView[]>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(recipeId)}/chunks`,
    ),
  previewDocumentRecipeChunks: (
    id: string,
    recipeId: string,
    body: DocumentChunkPreviewRequest,
  ) =>
    request<DocumentChunkPreviewResponse>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/chunk-preview`,
      jsonBody(body),
    ),
  exportDocumentRecipeExtraction: (
    id: string,
    recipeId: string,
    format: DocumentExtractionExportFormat = "markdown",
  ) => {
    const search = new URLSearchParams({ format });
    return request<DocumentExtractionExport>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/extraction-export?${search.toString()}`,
    );
  },
  /**
   * 抽出エクスポートのダウンロード URL（`download=true` で `Content-Disposition: attachment` の
   * ファイルを返す。ファイル名は backend が文書名とレシピから決める。#561）。
   */
  documentRecipeExtractionExportUrl: (
    id: string,
    recipeId: string,
    format: DocumentExtractionExportFormat,
  ) => {
    const search = new URLSearchParams({ format, download: "true" });
    return appPath(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/extraction-export?${search.toString()}`,
    );
  },
  listDocumentIngestionJobs: (id: string) =>
    request<IngestionJob[]>(
      `/api/documents/${encodeURIComponent(id)}/ingestion-jobs`,
    ),
  listDocumentIngestionSegments: (id: string) =>
    request<IngestionSegment[]>(
      `/api/documents/${encodeURIComponent(id)}/ingestion-segments`,
    ),
  // 削除は成功しても原本・artifact の後始末の失敗を warning_messages で返すため、併せて返す（#281）。
  deleteDocument: deleteDocumentWithConfirmation,
  listDocumentKnowledgeBases: (id: string) =>
    request<KnowledgeBaseRef[]>(
      `/api/documents/${encodeURIComponent(id)}/knowledge-bases`,
    ),
  /** 一覧と同じ条件の文書の ID（一括の操作の対象。#2674）。 */
  listDocumentIds: (
    params: {
      status?: FileStatus;
      queued_phase?: IngestionJobPhase;
      q?: string;
      knowledge_base_id?: string;
      language_pack_relation?: LanguagePackRelation;
      current_release_id?: string;
      limit?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    if (params.queued_phase) search.set("queued_phase", params.queued_phase);
    if (params.q) search.set("q", params.q);
    if (params.knowledge_base_id) search.set("knowledge_base_id", params.knowledge_base_id);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.language_pack_relation) search.set("language_pack_relation", params.language_pack_relation);
    if (params.current_release_id) search.set("current_release_id", params.current_release_id);
    const qs = search.toString();
    return request<DocumentIdList>(`/api/documents/ids${qs ? `?${qs}` : ""}`);
  },
  /** 文書のファイル準備をまとめて受け付ける（#2674）。受付済みの job は応答の前に永続化される。 */
  bulkEnqueueDocumentIngestionJobs: (documentIds: string[]) =>
    request<BulkIngestionResult>(
      "/api/documents/ingestion-jobs/bulk",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: documentIds }),
      },
      { timeoutMs: BULK_INGESTION_TIMEOUT_MS },
    ),
  getDocumentDeleteImpact: (ids: string[]) => {
    const search = new URLSearchParams();
    for (const id of ids) search.append("document_id", id);
    return request<DocumentDeleteImpact[]>(`/api/documents/delete-impact?${search.toString()}`);
  },
  getDocumentSections: (id: string, recipeId?: string | null) =>
    request<DocumentSectionsData>(
      `/api/documents/${encodeURIComponent(id)}/sections${
        recipeId ? `?recipe_id=${encodeURIComponent(recipeId)}` : ""
      }`,
    ),
  saveDocumentSections: (id: string, body: DocumentSectionsSaveRequest) =>
    request<DocumentSectionsData>(`/api/documents/${encodeURIComponent(id)}/sections`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  resetDocumentSections: (id: string, recipeId?: string | null) =>
    request<DocumentSectionsData>(
      `/api/documents/${encodeURIComponent(id)}/sections${
        recipeId ? `?recipe_id=${encodeURIComponent(recipeId)}` : ""
      }`,
      { method: "DELETE" },
    ),

  getSectionRulesSettings: () => request<SectionRulesSettingsData>("/api/settings/section-rules"),
  saveSectionRulesSettings: (body: { mode: SectionRulesMode; rules: SectionRule[] }) =>
    request<SectionRulesSettingsData>("/api/settings/section-rules", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  resetSectionRulesSettings: () =>
    request<SectionRulesSettingsData>("/api/settings/section-rules", { method: "DELETE" }),
  listDocumentCategories: () => request<DocumentCategory[]>("/api/document-categories"),
  createDocumentCategory: (body: { parent_id: string | null; name: string }) =>
    request<DocumentCategory[]>("/api/document-categories", { ...jsonBody(body), method: "POST" }),
  renameDocumentCategory: (id: string, name: string) =>
    request<{ updated_document_count: number }>(`/api/document-categories/${encodeURIComponent(id)}`, {
      ...jsonBody({ name }),
      method: "PATCH",
    }),
  moveDocumentCategory: (id: string, direction: "up" | "down") =>
    request<DocumentCategory[]>(`/api/document-categories/${encodeURIComponent(id)}/move`, {
      ...jsonBody({ direction }),
      method: "POST",
    }),
  deleteDocumentCategory: (id: string) =>
    request<DocumentCategory[]>(`/api/document-categories/${encodeURIComponent(id)}`, { method: "DELETE" }),
  saveDocumentClassification: (id: string, body: DocumentClassification) =>
    request<DocumentDetail>(`/api/documents/${encodeURIComponent(id)}/classification`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  saveDocumentSupersededBy: (id: string, body: DocumentSupersededByRequest) =>
    request<DocumentDetail>(`/api/documents/${encodeURIComponent(id)}/superseded-by`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  replaceDocumentKnowledgeBases: (
    id: string,
    body: DocumentKnowledgeBaseReplaceRequest,
  ) =>
    request<KnowledgeBaseRef[]>(
      `/api/documents/${encodeURIComponent(id)}/knowledge-bases`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  uploadDocument: (
    file: File,
    knowledgeBaseIds: string[] = [],
    onUploadProgress?: (progress: UploadTransferProgress) => void,
  ) => {
    const form = new FormData();
    form.append("file", file);
    for (const id of knowledgeBaseIds) {
      form.append("knowledge_base_ids", id);
    }
    return requestUpload<UploadResult>("/api/documents/upload", form, onUploadProgress);
  },
  batchUploadDocuments: (
    files: File[],
    knowledgeBaseIds: string[] = [],
    onUploadProgress?: (progress: UploadTransferProgress) => void,
  ) => {
    const form = new FormData();
    for (const file of files) {
      form.append("files", file);
    }
    for (const id of knowledgeBaseIds) {
      form.append("knowledge_base_ids", id);
    }
    return requestUpload<BatchUploadResult>(
      "/api/documents/batch-upload",
      form,
      onUploadProgress,
    );
  },
  enqueueDocumentIngestionJob: (
    id: string,
    force = false,
    phase: IngestionJobPhase = "PREPROCESS",
  ) =>
    request<IngestionJob>(
      `/api/documents/${encodeURIComponent(id)}/ingestion-jobs?${ingestionJobSearch(force, phase)}`,
      { method: "POST" },
    ),
  enqueueDocumentRecipeJob: (
    id: string,
    recipeId: string,
    phase: IngestionJobPhase = "PREPROCESS",
    expectedLanguagePackReleaseId?: string,
    expectedConfigRevision?: number,
  ) =>
    request<IngestionJob>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/ingestion-jobs?phase=${encodeURIComponent(phase)}${expectedLanguagePackReleaseId ? `&expected_language_pack_release_id=${encodeURIComponent(expectedLanguagePackReleaseId)}` : ""}${expectedConfigRevision != null ? `&expected_config_revision=${expectedConfigRevision}` : ""}`,
      { method: "POST" },
    ),
  retryFailedDocumentIngestionSegments: (
    id: string,
    recipeId?: string | null,
  ) =>
    request<IngestionJob>(
      `/api/documents/${encodeURIComponent(id)}/ingestion-segments/retry${
        recipeId ? `?recipe_id=${encodeURIComponent(recipeId)}` : ""
      }`,
      { method: "POST" },
    ),
  approveDocumentRecipe: (
    id: string,
    recipeId: string,
    payload?: DocumentApproveRequest,
  ) =>
    request<IngestionJob>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/approve`,
      payload ? jsonBody(payload) : { method: "POST" },
    ),
  saveDocumentRecipeReviewEdits: (
    id: string,
    recipeId: string,
    payload: DocumentReviewEditsRequest,
  ) =>
    request<DocumentRecipeView>(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/review-edits`,
      { ...jsonBody(payload), method: "PATCH" },
    ),
  getIngestionJob: (id: string) =>
    request<IngestionJob>(
      `/api/documents/ingestion-jobs/${encodeURIComponent(id)}`,
    ),
  /** 原本/処理後ファイルの配信 URL（プレビュー/ダウンロード用）。 */
  documentContentUrl: (
    id: string,
    options: {
      variant?: "original" | "prepared";
      disposition?: "inline" | "attachment";
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (options.variant) search.set("variant", options.variant);
    if (options.disposition) search.set("disposition", options.disposition);
    const qs = search.toString();
    return appPath(`/api/documents/${encodeURIComponent(id)}/content${qs ? `?${qs}` : ""}`);
  },
  documentRecipeContentUrl: (
    id: string,
    recipeId: string,
    options: {
      variant?: "original" | "prepared";
      disposition?: "inline" | "attachment";
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (options.variant) search.set("variant", options.variant);
    if (options.disposition) search.set("disposition", options.disposition);
    const qs = search.toString();
    return appPath(
      `/api/documents/${encodeURIComponent(id)}/recipes/${encodeURIComponent(
        recipeId,
      )}/content${qs ? `?${qs}` : ""}`,
    );
  },
  /** PDF をページ画像で表示するためのページ一覧（bbox の強調を重ねる。#349）。 */
  getDocumentPreviewPages: (
    id: string,
    options: { recipeId?: string | null; variant?: "original" | "prepared" } = {},
  ) =>
    request<DocumentPreviewPages>(
      `${documentPreviewPagesBase(id, options.recipeId)}${
        options.variant ? `?variant=${options.variant}` : ""
      }`,
    ),
  documentPreviewPageImageUrl: (
    id: string,
    pageNumber: number,
    options: { recipeId?: string | null; variant?: "original" | "prepared"; dpi?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (options.variant) search.set("variant", options.variant);
    if (options.dpi) search.set("dpi", String(options.dpi));
    const qs = search.toString();
    return appPath(`${documentPreviewPagesBase(id, options.recipeId)}/${pageNumber}${qs ? `?${qs}` : ""}`);
  },

  // ナレッジベース
  listKnowledgeBases: (
    params: {
      status?: KnowledgeBaseStatus;
      q?: string;
      limit?: number;
      offset?: number;
      /** 指定した ID の KB だけを返す（選択済みの名前・状態の解決用。#302）。 */
      ids?: string[];
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    if (params.q) search.set("q", params.q);
    for (const id of params.ids ?? []) search.append("ids", id);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.offset != null) search.set("offset", String(params.offset));
    const qs = search.toString();
    return requestDegradable<Page<KnowledgeBaseSummary>>(
      `/api/knowledge-bases${qs ? `?${qs}` : ""}`,
    );
  },
  getKnowledgeBase: (id: string) =>
    request<KnowledgeBaseDetail>(
      `/api/knowledge-bases/${encodeURIComponent(id)}`,
    ),
  // DB が止まっていると backend は空の図と warning_messages で縮退するため、warning も返す（空と区別する）。
  getKnowledgeBaseGraph: (id: string, limit = 80) =>
    requestDegradable<KnowledgeBaseGraphData>(
      `/api/knowledge-bases/${encodeURIComponent(id)}/graph?limit=${limit}`,
    ),
  // KB ごとの項目抽出の定義（#548）。fields: null で全体の既定に戻す。
  getKnowledgeBaseExtractionFields: (id: string) =>
    request<KnowledgeBaseExtractionFieldsData>(
      `/api/knowledge-bases/${encodeURIComponent(id)}/extraction-fields`,
    ),
  updateKnowledgeBaseExtractionFields: (
    id: string,
    body: { fields: ExtractionFieldDefinition[] | null },
  ) =>
    request<KnowledgeBaseExtractionFieldsData>(
      `/api/knowledge-bases/${encodeURIComponent(id)}/extraction-fields`,
      { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    ),
  // 検索の絞り込みに使える項目（選んだ検索・回答プロファイルの KB の定義の和集合。#549）。
  getSearchExtractionFields: (searchAnswerProfileId: string) =>
    request<SearchExtractionFieldsData>(
      `/api/search/extraction-fields?${new URLSearchParams({
        search_answer_profile_id: searchAnswerProfileId,
      }).toString()}`,
    ),
  createKnowledgeBase: (body: KnowledgeBaseCreateRequest) =>
    request<KnowledgeBaseDetail>("/api/knowledge-bases", jsonBody(body)),
  updateKnowledgeBase: (id: string, body: KnowledgeBaseUpdateRequest) =>
    request<KnowledgeBaseDetail>(
      `/api/knowledge-bases/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  archiveKnowledgeBase: (id: string) =>
    request<KnowledgeBaseDetail>(
      `/api/knowledge-bases/${encodeURIComponent(id)}/archive`,
      {
        method: "POST",
      },
    ),
  assignDocumentsToKnowledgeBase: (
    id: string,
    body: KnowledgeBaseDocumentAssignmentRequest,
  ) =>
    request<KnowledgeBaseDetail>(
      `/api/knowledge-bases/${encodeURIComponent(id)}/documents`,
      jsonBody(body),
    ),
  removeDocumentFromKnowledgeBase: (
    knowledgeBaseId: string,
    documentId: string,
  ) =>
    request<KnowledgeBaseDetail>(
      `/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(
        documentId,
      )}`,
      { method: "DELETE" },
    ),

  // 検索・回答プロファイル(Search Answer Profile)
  listSearchAnswerProfiles: (
    params: {
      status?: SearchAnswerProfileStatus;
      q?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    if (params.q) search.set("q", params.q);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.offset != null) search.set("offset", String(params.offset));
    const qs = search.toString();
    return requestDegradable<Page<SearchAnswerProfileSummary>>(
      `/api/search-answer-profiles${qs ? `?${qs}` : ""}`,
    );
  },
  getSearchAnswerProfile: (id: string) =>
    request<SearchAnswerProfileDetail>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}`,
    ),
  /** 継承するシステム設定の窓口（プロファイルの画面の権限で読む。#2683）。 */
  getInheritedAnswerContact: () =>
    request<InheritedAnswerContactData>("/api/search-answer-profiles/inherited-contact"),
  /** 参照するナレッジベースで実体・別名索引を持つ文書の数(拡張の開閉の案内。#1388)。 */
  entityIndexCoverage: (knowledgeBaseIds: string[]) =>
    request<EntityIndexCoverageData>(
      "/api/search-answer-profiles/entity-index-coverage",
      jsonBody({ knowledge_base_ids: knowledgeBaseIds }),
    ),
  createSearchAnswerProfile: (body: SearchAnswerProfileCreateRequest) =>
    request<SearchAnswerProfileDetail>("/api/search-answer-profiles", jsonBody(body)),
  updateSearchAnswerProfile: (id: string, body: SearchAnswerProfileUpdateRequest) =>
    request<SearchAnswerProfileDetail>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  archiveSearchAnswerProfile: (id: string) =>
    request<SearchAnswerProfileDetail>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/archive`,
      {
        method: "POST",
      },
    ),
  getQuestionUnderstanding: (id: string) =>
    request<LanguagePackData>(`/api/search-answer-profiles/${id}/question-understanding`),
  saveQuestionUnderstanding: (id: string, overrides: LanguagePackSections) =>
    request<LanguagePackData>(`/api/search-answer-profiles/${id}/question-understanding`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ overrides }),
    }),
  previewQuestionUnderstanding: (id: string, text: string, overrides: LanguagePackSections) =>
    request<QuestionUnderstandingPreviewData>(`/api/search-answer-profiles/${id}/question-understanding/preview`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, overrides }),
    }),
  getDomainKeywords: (id: string) =>
    request<DomainKeywordsData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/domain-keywords`,
    ),
  saveDomainKeywords: (id: string, keywords: string[]) =>
    request<DomainKeywordsData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/domain-keywords`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keywords }),
      },
    ),
  previewDomainKeywordImport: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<DomainKeywordImportPreviewData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/domain-keywords/import/preview`,
      { method: "POST", body: form },
    );
  },
  importDomainKeywords: (id: string, file: File, mode: DomainKeywordImportMode) => {
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    return request<DomainKeywordImportData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/domain-keywords/import`,
      { method: "POST", body: form },
    );
  },
  getApprovedFaq: (id: string) =>
    request<ApprovedFaqListData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq`,
    ),
  addApprovedFaq: (id: string, body: { question: string; answer: string }) =>
    request<ApprovedFaqMutationData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq`,
      jsonBody(body),
    ),
  deleteApprovedFaq: (id: string, ids: string[]) =>
    request<ApprovedFaqMutationData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq/delete`,
      jsonBody({ ids }),
    ),
  previewApprovedFaqImport: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ApprovedFaqImportPreviewData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq/import/preview`,
      { method: "POST", body: form },
    );
  },
  importApprovedFaq: (id: string, file: File, mode: ApprovedFaqImportMode) => {
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    return request<ApprovedFaqMutationData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq/import`,
      { method: "POST", body: form },
    );
  },
  /**
   * purpose="chat" はチャットの提示（一致度の下限が高く、最大 3 件。#684）。
   * `signal` は RAG 検索の「停止」で照会を止めるのに使う（#915）。
   */
  suggestApprovedFaq: (
    id: string,
    query: string,
    purpose: "search" | "chat" = "search",
    signal?: AbortSignal,
  ) =>
    request<ApprovedFaqSuggestionsData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq/suggest`,
      { ...jsonBody({ query, purpose }), signal },
    ),
  suggestClarification: (id: string, query: string, conversationId?: string | null) =>
    request<{ suggestion: ClarificationSuggestionData | null; question_context_token?: string | null }>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/clarifications/suggest`,
      jsonBody({ query, ...(conversationId ? { conversation_id: conversationId } : {}) }),
    ),
  saveRuleClarification: (id: string, ruleId: string, clarification: RuleClarification | null) =>
    request<RuntimeKnowledgeData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/runtime-knowledge/rules/${encodeURIComponent(ruleId)}/clarification`,
      { ...jsonBody({ clarification }), method: "PUT" },
    ),
  setApprovedFaqEnabled: (id: string, enabled: boolean) =>
    request<ApprovedFaqListData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/approved-faq/settings`,
      { ...jsonBody({ enabled }), method: "PUT" },
    ),
  listAnswerRecords: (params: {
    searchAnswerProfileId: string;
    limit: number;
    offset?: number;
    /** 指定するとその回答だけを返す（チャットが会話の回答の保存有無を引き当てる）。 */
    traceIds?: string[];
  }) => {
    const search = new URLSearchParams({
      search_answer_profile_id: params.searchAnswerProfileId,
      limit: String(params.limit),
      offset: String(params.offset ?? 0),
    });
    for (const traceId of params.traceIds ?? []) search.append("trace_id", traceId);
    return request<Page<AnswerRecordSummary>>(`/api/search/answers?${search.toString()}`);
  },
  // 実行履歴（チャット・RAG 検索の実行。#1890）。見え方は backend が決める（system_admin は全員、
  // それ以外は自分が実行した実行と、自分の会話で他の人が実行した実行。#1889・#1954）。
  listRuns: (params: RunHistoryListParams) => {
    const search = new URLSearchParams({
      limit: String(params.limit),
      offset: String(params.offset),
    });
    if (params.q) search.set("q", params.q);
    if (params.surface) search.set("surface", params.surface);
    if (params.status) search.set("status", params.status);
    if (params.executor && params.executor !== "all") search.set("executor", params.executor);
    if (params.since) search.set("since", params.since);
    return request<Page<RunSummary>>(`/api/history/runs?${search.toString()}`);
  },
  getRun: (runId: string) =>
    request<RunDetail>(`/api/history/runs/${encodeURIComponent(runId)}`),
  /** 実行の処理の経過（実行の詳細と同じ見え方。共通の span の表の snapshot。#3226）。 */
  getRunTrace: (runId: string, signal?: AbortSignal) =>
    request<TraceSnapshotData>(`/api/history/runs/${encodeURIComponent(runId)}/trace`, { signal }),
  getAnswerRecord: (traceId: string) =>
    request<AnswerRecordDetail>(
      `/api/search/answers/${encodeURIComponent(traceId)}`,
    ),
  getRuntimeKnowledge: (id: string) =>
    request<RuntimeKnowledgeData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/runtime-knowledge`,
    ),
  editRuntimeKnowledge: (id: string, body: RuntimeKnowledgeEditRequest) =>
    request<RuntimeKnowledgeData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/runtime-knowledge/edit`,
      jsonBody(body),
    ),
  previewRuntimeKnowledge: (id: string, question: string) =>
    request<RuntimeKnowledgePreviewData>(
      `/api/search-answer-profiles/${encodeURIComponent(id)}/runtime-knowledge/preview`,
      jsonBody({ question }),
    ),
  // 業務ガイド（#1237）。下書きの保存は base_revision で照合し、違えば 409。
  listSupportGuides: (id: string, includeArchived: boolean) =>
    request<SupportGuideSummary[]>(
      `${supportGuidesPath(id)}${includeArchived ? "?include_archived=true" : ""}`,
    ),
  getSupportGuide: (id: string, guideId: string) =>
    request<SupportGuideDetail>(`${supportGuidesPath(id)}/${encodeURIComponent(guideId)}`),
  createSupportGuide: (id: string, draft: SupportGuideContent) =>
    request<SupportGuideDetail>(supportGuidesPath(id), jsonBody({ draft })),
  saveSupportGuideDraft: (
    id: string,
    guideId: string,
    draft: SupportGuideContent,
    baseRevision: number,
  ) =>
    request<SupportGuideDetail>(`${supportGuidesPath(id)}/${encodeURIComponent(guideId)}`, {
      ...jsonBody({ draft, base_revision: baseRevision }),
      method: "PUT",
    }),
  validateSupportGuide: (id: string, guideId: string) =>
    request<SupportGuideValidationData>(
      `${supportGuidesPath(id)}/${encodeURIComponent(guideId)}/validate`,
      { method: "POST" },
    ),
  publishSupportGuide: (id: string, guideId: string, baseRevision: number) =>
    request<SupportGuideDetail>(
      `${supportGuidesPath(id)}/${encodeURIComponent(guideId)}/publish`,
      jsonBody({ base_revision: baseRevision }),
    ),
  getSupportGuideRevision: (id: string, guideId: string, revision: number) =>
    request<SupportGuideRevision>(
      `${supportGuidesPath(id)}/${encodeURIComponent(guideId)}/revisions/${revision}`,
    ),
  // 下書きを戻す版の内容で置き換えるので、読み込んだ下書きの版を照合する（違えば 409。#1278）。
  rollbackSupportGuide: (id: string, guideId: string, revision: number, baseRevision: number) =>
    request<SupportGuideDetail>(
      `${supportGuidesPath(id)}/${encodeURIComponent(guideId)}/rollback`,
      jsonBody({ revision, base_revision: baseRevision }),
    ),
  setSupportGuideArchived: (id: string, guideId: string, archived: boolean) =>
    request<SupportGuideDetail>(
      `${supportGuidesPath(id)}/${encodeURIComponent(guideId)}/${archived ? "archive" : "restore"}`,
      { method: "POST" },
    ),
  exportSupportGuides: (id: string) =>
    request<SupportGuideExportData>(`${supportGuidesPath(id)}/export`),
  previewSupportGuideImport: (id: string, guides: unknown[]) =>
    request<SupportGuideImportPreviewData>(
      `${supportGuidesPath(id)}/import/preview`,
      jsonBody({ guides }),
    ),
  importSupportGuides: (id: string, guides: unknown[]) =>
    request<SupportGuideImportData>(`${supportGuidesPath(id)}/import`, jsonBody({ guides })),
  /** 回答の過程の Excel のシートと列。列を選べば、その列で行を読む（#2397）。 */
  readSupportGuideProcessWorkbook: (
    id: string,
    file: File,
    selection?: SupportGuideProcessWorkbookSelection,
  ) => {
    const form = new FormData();
    form.append("file", file);
    if (selection) {
      form.append("sheet", selection.sheet);
      form.append("question_column", String(selection.question_column));
      form.append("process_column", String(selection.process_column));
      if (selection.answer_column !== null) {
        form.append("answer_column", String(selection.answer_column));
      }
      form.append("start_row", String(selection.start_row));
    }
    return request<SupportGuideProcessWorkbookData>(
      `${supportGuidesPath(id)}/import/process/workbook`,
      { method: "POST", body: form },
    );
  },
  /** 行ごとに AI で業務ガイドの下書きを作る（保存しない。1 行で AI を 1 回呼ぶので回答生成の timeout）。 */
  previewSupportGuideProcessImport: (id: string, rows: SupportGuideProcessRow[]) =>
    request<SupportGuideProcessPreviewData>(
      `${supportGuidesPath(id)}/import/process/preview`,
      jsonBody({ rows }),
      { timeoutMs: ANSWER_GENERATION_TIMEOUT_MS },
    ),
  /** 保存した下書きで試しに答える（#1288）。回答を作るので回答生成の timeout を使う。 */
  trySupportGuideDraft: (
    id: string,
    guideId: string,
    body: { query: string; draft_revision: number; conditions: Record<string, string> },
  ) =>
    request<SupportGuideDraftTryData>(
      `${supportGuidesPath(id)}/${encodeURIComponent(guideId)}/try`,
      jsonBody(body),
      { timeoutMs: ANSWER_GENERATION_TIMEOUT_MS },
    ),

  // チャット（会話 / マルチモデル比較）
  listConversations: (
    params: { search_answer_profile_id?: string; limit?: number; offset?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.search_answer_profile_id)
      search.set("search_answer_profile_id", params.search_answer_profile_id);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.offset != null) search.set("offset", String(params.offset));
    const qs = search.toString();
    return requestDegradable<ChatHistoryPage<ConversationSummary>>(
      `/api/chat/conversations${qs ? `?${qs}` : ""}`,
    );
  },
  createConversation: (body: ConversationCreateBody) =>
    request<ConversationDetail>("/api/chat/conversations", jsonBody(body)),
  getConversation: (id: string) =>
    request<ConversationDetail>(
      `/api/chat/conversations/${encodeURIComponent(id)}`,
    ),
  updateConversation: (id: string, body: ConversationUpdateBody) =>
    request<ConversationSummary>(
      `/api/chat/conversations/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  deleteConversation: (id: string) =>
    request<null>(`/api/chat/conversations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  /**
   * 質問（`messageId`）への回答の作成を止める（#1175）。接続を切っても作成は止まらないので、「停止」はこの API で
   * 取り消す。作成中の回答は `CANCELLED` で保存される。
   */
  cancelChatAnswer: (conversationId: string, messageId: string) =>
    request<{ cancelled: boolean }>(
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/cancel`,
      { method: "POST" },
    ),
  /**
   * 回答（`messageId` は ASSISTANT のメッセージ）の処理の経過（共通の span の表の snapshot。#3226）。会話を開ける
   * 利用者だけ（他人の会話は 404）。イベントは未検証の入力として `useTraceProgressSnapshot` が検証する。
   */
  getChatAnswerTrace: (conversationId: string, messageId: string, signal?: AbortSignal) =>
    request<TraceSnapshotData>(
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/trace`,
      { signal },
    ),
  /** RAG 検索の実行（配信の `run` の trace_id）の処理の経過（実行中は表を実行の id で引く。#3226）。 */
  getSearchRunTrace: (runId: string, signal?: AbortSignal) =>
    request<TraceSnapshotData>(`/api/search/runs/${encodeURIComponent(runId)}/trace`, { signal }),
  listCompareModels: () => request<CompareModel[]>("/api/chat/models"),
  listSearchAnswerModels: () => request<CompareModel[]>("/api/search/models"),
  listProfileAnswerModels: () => request<CompareModel[]>("/api/search-answer-profiles/answer-models"),

  // 検索
  // 検索・回答プロファイル / KB の範囲外の 403（RAG_SCOPE_FORBIDDEN）は理由をその場で見せる（#214 / #224）。
  search: (body: SearchRequestBody) =>
    request<SearchResponse>("/api/search", jsonBody(body), {
      timeoutMs: ANSWER_GENERATION_TIMEOUT_MS,
    }),
  submitFeedback: (body: FeedbackRequestBody) =>
    request<FeedbackSubmissionResponse>("/api/feedback", jsonBody(body)),
  getCurrentFeedback: (traceId: string) => {
    const search = new URLSearchParams({ trace_id: traceId });
    return request<CurrentFeedbackItem[]>(
      `/api/feedback/current?${search.toString()}`,
    );
  },
  listFeedback: (params: FeedbackListParams = {}) => {
    const search = new URLSearchParams();
    if (params.search_answer_profile_id)
      search.set("search_answer_profile_id", params.search_answer_profile_id);
    if (params.target_type) search.set("target_type", params.target_type);
    if (params.rating) search.set("rating", params.rating);
    if (params.reason) search.set("reason", params.reason);
    if (params.period_days != null)
      search.set("period_days", String(params.period_days));
    if (params.q) search.set("q", params.q);
    if (params.sort_order) search.set("sort_order", params.sort_order);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.offset != null) search.set("offset", String(params.offset));
    const qs = search.toString();
    return request<FeedbackDashboard>(`/api/feedback${qs ? `?${qs}` : ""}`);
  },
  getFeedbackDetail: (id: string) =>
    request<FeedbackDetail>(`/api/feedback/${encodeURIComponent(id)}`),
  getAnswerPrompts: () => request<AnswerPromptsData>("/api/settings/answer-prompts"),
  saveAnswerPrompt: (key: AnswerPromptKey, content: string) =>
    request<AnswerPromptsData>(`/api/settings/answer-prompts/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
  resetAnswerPrompt: (key: AnswerPromptKey) =>
    request<AnswerPromptsData>(`/api/settings/answer-prompts/${key}`, { method: "DELETE" }),
  promoteFeedbackToApprovedFaq: (id: string) =>
    request<FeedbackApprovedFaqPromotion>(
      `/api/feedback/${encodeURIComponent(id)}/approved-faq`,
      { method: "POST" },
    ),
  // 回答のフィードバックから作る品質評価のケース（評価リクエストの Excel の 1 行。#2065）。
  downloadFeedbackEvaluationCase: (id: string) =>
    requestFile(`/api/feedback/${encodeURIComponent(id)}/evaluation-case`, "evaluation_case.xlsx"),

  // 評価
  // 品質評価は job で実行する（#390）。同期の /api/evaluation/run は画面から使わない。
  submitRunEvaluationJob: (body: EvaluationRunRequestBody) =>
    request<EvaluationJob>("/api/evaluation/jobs/run", jsonBody(body)),
  // 評価リクエストの Excel（#2065）。テンプレートのダウンロードと、アップロードした Excel の検証。
  // テンプレートは選んだプロファイルが参照する文書の一覧（ファイル名・文書 ID）を「文書一覧」に入れる（#2086）。
  downloadEvaluationTemplate: (searchAnswerProfileId: string | null = null) => {
    const query = new URLSearchParams();
    if (searchAnswerProfileId) query.set("search_answer_profile_id", searchAnswerProfileId);
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return requestFile(`/api/evaluation/cases/template${suffix}`, "evaluation_request_template.xlsx");
  },
  importEvaluationCases: (file: File, searchAnswerProfileId: string) => {
    const body = new FormData();
    body.append("file", file);
    body.append("search_answer_profile_id", searchAnswerProfileId);
    return request<EvaluationImportResult>("/api/evaluation/cases/import", { method: "POST", body });
  },
  /** 回答の過程から評価の期待の下書きを作る（#2402。1 回 10 行まで。保存しない）。 */
  draftEvaluationProcess: (body: { knowledge_base_ids: string[]; rows: EvaluationProcessRow[] }) =>
    request<{ drafts: EvaluationProcessDraft[] }>(
      "/api/evaluation/cases/process-drafts",
      jsonBody(body)
    ),
  downloadEvaluationResults: (jobId: string, body: {
    rows: EvaluationImportRow[]; cases: EvaluationCase[];
    profile_name: string | null; compare_profile_name: string | null;
  }) => requestFile(`/api/evaluation/jobs/${encodeURIComponent(jobId)}/results.xlsx`, "evaluation_results.xlsx", jsonBody(body)),
  getEvaluationJob: (jobId: string) =>
    request<EvaluationJob>(`/api/evaluation/jobs/${encodeURIComponent(jobId)}`),
  cancelEvaluationJob: (jobId: string) =>
    request<EvaluationJob>(`/api/evaluation/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    }),
  // 品質評価: 判定の校正（#3578）。記入は自分の判定だけ、全員の判定・裁定・校正は管理の権限。
  getJudgeCalibrationStatus: () =>
    request<JudgeCalibrationStatus>("/api/evaluation/calibration/status"),
  listCalibrationSources: () =>
    request<{ jobs: CalibrationSourceJob[] }>("/api/evaluation/calibration/sources"),
  listLabelSets: () => request<LabelSetListData>("/api/evaluation/calibration/label-sets"),
  createLabelSet: (body: LabelSetCreateBody) =>
    request<LabelSetSummary>("/api/evaluation/calibration/label-sets", jsonBody(body)),
  importLabelSet: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<{ label_set: LabelSetSummary; imported_items: number; imported_labels: number }>(
      "/api/evaluation/calibration/label-sets/import",
      { method: "POST", body }
    );
  },
  deleteLabelSet: (labelSetId: string) =>
    request<{ deleted: boolean }>(labelSetPath(labelSetId), { method: "DELETE" }),
  getLabelSet: (labelSetId: string) => request<LabelSetDetail>(labelSetPath(labelSetId)),
  listAnnotationRows: (
    labelSetId: string,
    params: { aspect?: string; status?: "all" | "unlabeled" | "labeled"; offset: number; limit: number }
  ) => {
    const query = new URLSearchParams();
    if (params.aspect) query.set("aspect", params.aspect);
    if (params.status) query.set("status", params.status);
    query.set("offset", String(params.offset));
    query.set("limit", String(params.limit));
    return request<AnnotationRowsData>(`${labelSetPath(labelSetId)}/rows?${query.toString()}`);
  },
  saveCalibrationLabel: (labelSetId: string, body: CalibrationLabelInput) =>
    request<CalibrationProgress>(`${labelSetPath(labelSetId)}/labels`, jsonPutBody(body)),
  getCalibrationAgreement: (
    labelSetId: string,
    params: { aspect?: string; status: AgreementFilter; offset: number; limit: number }
  ) => {
    const query = new URLSearchParams();
    if (params.aspect) query.set("aspect", params.aspect);
    query.set("status", params.status);
    query.set("offset", String(params.offset));
    query.set("limit", String(params.limit));
    return request<AgreementData>(`${labelSetPath(labelSetId)}/agreement?${query.toString()}`);
  },
  saveCalibrationAdjudication: (labelSetId: string, body: CalibrationLabelInput) =>
    request<AgreementData>(`${labelSetPath(labelSetId)}/adjudications`, jsonPutBody(body)),
  listSyntheticItems: (labelSetId: string) =>
    request<{ items: SyntheticItem[] }>(`${labelSetPath(labelSetId)}/synthetic`),
  setSyntheticExcluded: (labelSetId: string, itemId: string, excluded: boolean) =>
    request<SyntheticItem>(
      `${labelSetPath(labelSetId)}/synthetic/${encodeURIComponent(itemId)}`,
      jsonPutBody({ excluded })
    ),
  listCalibrations: (labelSetId: string) =>
    request<{ items: CalibrationSummary[] }>(`${labelSetPath(labelSetId)}/calibrations`),
  startCalibration: (labelSetId: string, repeats: number) =>
    request<{ job: EvaluationJob; calibration: CalibrationSummary }>(
      `${labelSetPath(labelSetId)}/calibrations`,
      jsonBody({ repeats })
    ),
  downloadLabelSet: (labelSetId: string) =>
    requestFile(`${labelSetPath(labelSetId)}/export`, `labels-${labelSetId.slice(0, 8)}.json`),

  // 設定: モデル
  getModelSettings: (options: SettingsRequestOptions = {}) =>
    request<ModelSettingsData>("/api/settings/model", { signal: options.signal }),
  updateModelSettings: (body: ModelSettingsPayload) =>
    request<ModelSettingsData>("/api/settings/model", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  testModelSettings: (body: ModelSettingsTestRequest) =>
    request<ModelSettingsTestResult>(
      "/api/settings/model/test",
      jsonBody(body),
    ),

  // 設定: データベース
  getDatabaseSettings: (options: SettingsRequestOptions = {}) =>
    request<DatabaseSettingsData>("/api/settings/database", { signal: options.signal }),
  updateDatabaseSettings: (body: DatabaseSettingsUpdate) =>
    request<DatabaseSettingsData>("/api/settings/database", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  uploadDatabaseWallet: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<DatabaseSettingsData>("/api/settings/database/wallet", {
      method: "POST",
      body: form,
    });
  },
  testDatabaseSettings: (body: DatabaseSettingsUpdate) =>
    request<DatabaseConnectionTestResult>(
      "/api/settings/database/test",
      jsonBody(body),
    ),
  getSystemTablesStatus: () =>
    request<SystemTablesStatusData>("/api/settings/database/system-tables"),
  initializeSystemTables: (body: SystemTablesInitializeRequest) =>
    request<SystemTablesOperationData>(
      "/api/settings/database/system-tables/initialize",
      jsonBody(body),
    ),
  deleteSystemTableOrphanedRows: (body: SystemTablesDeleteOrphansRequest) =>
    request<SystemTablesOrphanDeletionData>(
      "/api/settings/database/system-tables/orphaned-rows/delete",
      jsonBody(body),
    ),

  // 設定: HuggingFace モデルダウンロード
  getHuggingFaceSettings: () =>
    request<HuggingFaceSettingsData>("/api/settings/huggingface"),
  updateHuggingFaceSettings: (body: HuggingFaceSettingsUpdate) =>
    request<HuggingFaceSettingsData>("/api/settings/huggingface", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // チャットの添付（#2517）
  uploadChatAttachment: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    // 画像の読み取り（Vision モデル）は時間がかかるため、待ち時間を長くする。
    return request<ChatAttachmentApiItem>("/api/chat-attachments", { method: "POST", body }, { timeoutMs: 180_000 });
  },
  listChatAttachments: () => request<ChatAttachmentListData>("/api/chat-attachments"),
  deleteChatAttachment: (attachmentId: string) =>
    request<{ deleted: boolean }>(`/api/chat-attachments/${encodeURIComponent(attachmentId)}`, {
      method: "DELETE",
    }),
  // 設定: チャットの添付（設定は製品ごと。#2517）
  getTraceRecordingSettings: (options: SettingsRequestOptions = {}) =>
    request<TraceRecordingSettingsData>("/api/settings/trace-recording", { signal: options.signal }),
  updateTraceRecordingSettings: (body: { enabled: boolean }) =>
    request<TraceRecordingSettingsData>("/api/settings/trace-recording", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getChatAttachmentSettings: (options: SettingsRequestOptions = {}) =>
    request<ChatAttachmentSettingsData>("/api/settings/chat-attachments", { signal: options.signal }),
  updateChatAttachmentSettings: (body: ChatAttachmentSettingsUpdate) =>
    request<ChatAttachmentSettingsData>("/api/settings/chat-attachments", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // 設定: 回答の注意書き（3 製品で共通の文。#2405）
  getAnswerNoticeSettings: (options: SettingsRequestOptions = {}) =>
    request<AnswerNoticeSettingsData>("/api/settings/answer-notice", { signal: options.signal }),
  updateAnswerNoticeSettings: (body: AnswerNoticeSettingsUpdate) =>
    request<AnswerNoticeSettingsData>("/api/settings/answer-notice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // 設定: アップロード保存先
  getUploadStorageSettings: (options: SettingsRequestOptions = {}) =>
    request<UploadStorageSettingsData>("/api/settings/upload-storage", {
      signal: options.signal,
    }),
  updateUploadStorageSettings: (body: UploadStorageSettingsUpdate) =>
    request<UploadStorageSettingsData>("/api/settings/upload-storage", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getParserAdapterSettings: () =>
    request<ParserAdapterSettingsData>("/api/settings/parser-adapters"),
  updateParserAdapterSettings: (body: ParserAdapterSettingsUpdate) =>
    request<ParserAdapterSettingsData>("/api/settings/parser-adapters", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // サービス管理: 前処理 / Parser マイクロサービスの稼働可視化・起動/停止
  getServiceCatalog: () => request<ServiceCatalogData>("/api/services/catalog"),
  getServiceStatus: (serviceId: string) =>
    request<ServiceStatusData>(
      `/api/services/${encodeURIComponent(serviceId)}/status`,
    ),
  getServiceLogs: (serviceId: string, lines = 200) =>
    request<ServiceLogsData>(
      `/api/services/${encodeURIComponent(serviceId)}/logs?lines=${encodeURIComponent(String(lines))}`,
    ),
  controlService: (serviceId: string, action: ServiceAction) =>
    request<ServiceControlResultData>(
      `/api/services/${encodeURIComponent(serviceId)}/${action}`,
      { method: "POST" },
    ),

  // 設定: Chunking アダプター
  getPreprocessSettings: () =>
    request<PreprocessSettingsData>("/api/settings/preprocess"),
  updatePreprocessSettings: (body: PreprocessSettingsUpdate) =>
    request<PreprocessSettingsData>("/api/settings/preprocess", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getLanguagePack: () => request<LanguagePackData>("/api/settings/language-pack"),
  getLanguagePackDraft: () => request<LanguagePackReleaseState>("/api/settings/language-pack/draft"),
  compareLanguagePackDraft: (expected_revision: number, expected_current_id: string) =>
    request<LanguagePackReleaseComparison>(`/api/settings/language-pack/draft/comparison?${new URLSearchParams({ expected_revision: String(expected_revision), expected_current_id })}`),
  updateLanguagePackDraft: (expected_revision: number, overrides: LanguagePackSections) =>
    request<LanguagePackReleaseState>("/api/settings/language-pack/draft", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision, overrides }) }),
  getLanguagePackReleases: (offset: number, limit: number) =>
    request<LanguagePackReleaseList>(`/api/settings/language-pack/releases?offset=${offset}&limit=${limit}`),
  getLanguagePackRelease: (id: string) =>
    request<LanguagePackRelease>(`/api/settings/language-pack/releases/${encodeURIComponent(id)}`),
  compareLanguagePackReleases: (before_id: string, after_id: string) =>
    request<LanguagePackReleaseComparison>(`/api/settings/language-pack/releases/compare?${new URLSearchParams({ before_id, after_id })}`),
  publishLanguagePack: (body: LanguagePackPublishRequest) =>
    request<LanguagePackReleaseState>("/api/settings/language-pack/publish", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  restoreLanguagePackRelease: (id: string, body: LanguagePackPublishRequest) =>
    request<LanguagePackReleaseState>(`/api/settings/language-pack/releases/${encodeURIComponent(id)}/restore`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  previewLanguagePack: (text: string, overrides: LanguagePackSections) =>
    request<LanguagePackPreviewData>("/api/settings/language-pack/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, overrides }),
    }),
  getChunkingSettings: () =>
    request<ChunkingSettingsData>("/api/settings/chunking"),
  updateChunkingSettings: (body: ChunkingSettingsUpdate) =>
    request<ChunkingSettingsData>("/api/settings/chunking", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // 設定: 回答の検索と生成・回答の記録
  getAnsweringSettings: () => request<AnsweringSettingsData>("/api/settings/answering"),
  updateAnsweringSettings: (body: AnsweringSettingsUpdate) =>
    request<AnsweringSettingsData>("/api/settings/answering", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getAnswerRecordSettings: () =>
    request<AnswerRecordSettingsData>("/api/settings/answer-records"),
  updateAnswerRecordSettings: (body: { retention_days: number }) =>
    request<AnswerRecordSettingsData>("/api/settings/answer-records", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getExtractionFieldsSettings: () =>
    request<ExtractionFieldsSettingsData>("/api/settings/extraction-fields"),
  updateExtractionFieldsSettings: (body: { fields: ExtractionFieldDefinition[] }) =>
    request<ExtractionFieldsSettingsData>("/api/settings/extraction-fields", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // 保存した全体の既定を消し、標準の項目に戻す（#556）。
  resetExtractionFieldsSettings: () =>
    request<ExtractionFieldsSettingsData>("/api/settings/extraction-fields", { method: "DELETE" }),
  getPipelineSettings: () => request<PipelineSettingsData>("/api/settings/pipeline"),
  updatePipelineSettings: (body: PipelineSettingsUpdate) =>
    request<PipelineSettingsData>("/api/settings/pipeline", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // 設定: Vector Index アダプター
  getVectorIndexSettings: () =>
    request<VectorIndexSettingsData>("/api/settings/vector-index"),
  updateVectorIndexSettings: (body: VectorIndexSettingsUpdate) =>
    request<VectorIndexSettingsData>("/api/settings/vector-index", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // 設定: Evaluation アダプター
  getEvaluationSettings: () =>
    request<EvaluationSettingsData>("/api/settings/evaluation-suite"),
  updateEvaluationSettings: (body: EvaluationSettingsUpdate) =>
    request<EvaluationSettingsData>("/api/settings/evaluation-suite", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // 設定: 文書構造索引の構築
  getEntityIndexSettings: () => request<EntityIndexSettingsData>("/api/settings/entity-index"),
  updateEntityIndexSettings: (body: EntityIndexSettingsData) =>
    request<EntityIndexSettingsData>("/api/settings/entity-index", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getGraphSettings: () => request<GraphSettingsData>("/api/settings/graph"),
  updateGraphSettings: (body: GraphSettingsUpdate) =>
    request<GraphSettingsData>("/api/settings/graph", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

// --- 検索・回答プロファイルの知識: ドメインキーワード(rag_poc 由来) ---
export interface DomainKeywordsData {
  search_answer_profile_id: string;
  keywords: string[];
}

export type DomainKeywordImportMode = "INSERT" | "REPLACE";

export interface DomainKeywordImportPreviewData {
  total: number;
  rows: { row: number; keyword: string }[];
}

export interface DomainKeywordImportData extends DomainKeywordsData {
  inserted_count: number;
}

// --- 検索・回答プロファイルの知識: Approved FAQ(類似問) ---
export type ApprovedFaqImportMode = "INSERT" | "DELETE_THEN_INSERT";

export interface ApprovedFaqRecordData {
  id: string;
  question: string;
  answer: string;
  alternate_questions: string[];
  status: string;
}

export interface ApprovedFaqListData {
  search_answer_profile_id: string;
  records: ApprovedFaqRecordData[];
  /** 回答の前に類似問を提示するか（検索・回答プロファイルごと。未設定はオン。#684）。 */
  enabled: boolean;
}

export interface ApprovedFaqMutationData extends ApprovedFaqListData {
  inserted_count: number;
  deleted_count: number;
}

export interface ApprovedFaqImportPreviewData {
  total: number;
  rows: { question: string; answer: string; row: number }[];
}

/** 文書の章節（章節ナビゲーション。#713）。並び順 + 階層（1〜6）で木を表す。 */
export type DocumentSectionOrigin = "extraction" | "manual";

export interface DocumentSection {
  id: string;
  title: string;
  level: number;
  page_start: number | null;
  page_end: number | null;
  origin: DocumentSectionOrigin;
  source_section_id: string | null;
  edited: boolean;
  /** 人の修正の後に新しく抽出され、自動で足した章節（保存すると外れる。#721）。 */
  added_from_extraction?: boolean;
}

export interface DocumentSectionsData {
  document_id: string;
  /** 人の修正（manual）か、抽出結果（extraction）か。 */
  source: DocumentSectionOrigin;
  /** 抽出結果の章節を作った方式（#715）。 */
  rules_mode: SectionRulesMode;
  summary_rules_match?: boolean | null;
  sections: DocumentSection[];
  extraction_section_count: number;
  page_count: number | null;
  revision: number | null;
  updated_at: string | null;
}

export interface DocumentSectionsSaveRequest {
  sections: DocumentSection[];
  base_revision: number | null;
  recipe_id?: string | null;
}

export interface ApprovedFaqSuggestionData {
  id: string;
  question: string;
  matched_question: string;
  answer: string;
  score: number;
  direct: boolean;
}

export interface ApprovedFaqSuggestionsData {
  suggestions: ApprovedFaqSuggestionData[];
}

// --- 検索・回答プロファイルの知識: 用語・同義語と回答ルール(runtime knowledge) ---
export type RuntimeKnowledgeKind = "terms" | "rules";

export interface RuntimeKnowledgeData {
  search_answer_profile_id: string;
  terms: Record<string, JsonValue>[];
  rules: Record<string, JsonValue>[];
}

export interface RuntimeKnowledgeEditRequest {
  kind: RuntimeKnowledgeKind;
  selected?: string | null;
  name?: string;
  title?: string;
  labels?: string;
  content?: string;
  source?: string;
  purpose?: "confirmation" | "generation" | "both";
  instruction_kind?: "presentation" | "requirement" | "verification";
  exclusive_group?: string;
  priority?: number;
  enabled?: boolean;
  delete?: boolean;
}

export interface RuntimeKnowledgePreviewData {
  expanded_question: string;
  matched_terms: string[];
  matched_rules: string[];
  conflicting_rules?: string[];
}

// --- 業務ガイド（SupportGuide。#1237）。型は backend の app/schemas/support_guide.py と同じ ---
export type SupportGuideStatus = "active" | "archived";
export type SupportGuideConditionType = "enum" | "text" | "boolean";
export type SupportGuideConditionSource = "user" | "document" | "tool";
export type SupportGuideUnknownHandling = "ask" | "branch" | "handoff";
export type SupportGuideImpactScope = "individual" | "group" | "all";
export type SupportGuideBranchOperator = "equals" | "in" | "unknown";
export type SupportGuideTool =
  | "rag_search"
  | "rag_read_source"
  | "rag_retrieve_evidence"
  | "nl2sql_query";

export interface SupportGuideCondition {
  id: string;
  label: string;
  type: SupportGuideConditionType;
  allowed_values: string[];
  required: boolean;
  source: SupportGuideConditionSource;
  unknown_handling: SupportGuideUnknownHandling;
  question: string;
  /** 選択肢の言い換え（選択肢 → 質問に出る語）。質問にこの語が出れば聞き直さない。 */
  value_aliases?: Record<string, string[]>;
}

export interface SupportGuideStep {
  id: string;
  title: string;
  purpose: string;
  depends_on: string[];
  retrieval_hints: string[];
  evidence_requirements: string[];
  allowed_tools: string[];
  done_when: string;
  /** この手順が参照する資料と頁（#2395。ガイドの references は全手順の共通）。 */
  references: SupportGuideReference[];
}

export interface SupportGuideBranch {
  id: string;
  when: { condition_id: string; operator: SupportGuideBranchOperator; values: string[] };
  goto_step: string;
  note: string;
}

/** 参照する資料の頁の範囲（両端を含む。#2395）。 */
export interface SupportGuidePageRange {
  page_start: number;
  page_end: number;
}

/** 表計算の資料のシートと行の範囲（行が無ければシートの全体。頁とは別の位置。#2663）。 */
export interface SupportGuideSheetRange {
  sheet_name: string;
  row_start?: number | null;
  row_end?: number | null;
  /** A1 形式のセルの範囲（例: B3:D10）。 */
  cell_range?: string;
}

export interface SupportGuideReference {
  document_id: string;
  title: string;
  section_path: string[];
  version: string;
  /** 根拠にする頁の範囲（空なら文書の全体。#2395）。 */
  page_ranges: SupportGuidePageRange[];
  /** 表計算の資料の根拠にするシートの行の範囲（頁の範囲とは同時に書かない。#2663）。 */
  sheet_ranges?: SupportGuideSheetRange[];
}

export interface SupportGuideCompletion {
  id: string;
  description: string;
  check_method: string;
}

/** 業務ガイドの内容（下書き・公開の版で同じ形）。 */
export interface SupportGuideContent {
  schema_version: 2;
  title: string;
  description: string;
  goal: { expected_result: string; intent_examples: string[]; match_terms: string[] };
  applicability: {
    business_domains: string[];
    object_types: string[];
    versions: string[];
    effective_from: string | null;
    effective_to: string | null;
  };
  conditions: SupportGuideCondition[];
  steps: SupportGuideStep[];
  branches: SupportGuideBranch[];
  references: SupportGuideReference[];
  completion: SupportGuideCompletion[];
  impact: {
    scope: SupportGuideImpactScope;
    approval_required: boolean;
    approval_note: string;
    /** 影響範囲と承認が係る手順の id（空なら業務ガイドのすべての場合に係る。#1320）。 */
    steps: string[];
  };
  handoff: { conditions: string[]; contact: string };
}

/** 検証で見つけた問題。severity=error は公開できない。 */
export interface SupportGuideIssue {
  severity: "error" | "warning";
  code: string;
  /** 問題のある項目（例: steps[1].depends_on）。 */
  path: string;
  message: string;
}

export interface SupportGuideRevisionSummary {
  revision: number;
  title: string;
  content_sha256: string;
  published_at: string;
  published_by: string | null;
  rollback_from: number | null;
}

export interface SupportGuideRevision extends SupportGuideRevisionSummary {
  content: SupportGuideContent;
}

export interface SupportGuideSummary {
  guide_id: string;
  search_answer_profile_id: string;
  status: SupportGuideStatus;
  title: string;
  draft_revision: number;
  published_revision: number | null;
  /** 下書きが公開の版と違うか（公開していなければ true）。 */
  has_unpublished_changes: boolean;
  updated_at: string;
  updated_by: string | null;
}

export interface SupportGuideDetail extends SupportGuideSummary {
  draft: SupportGuideContent;
  published: SupportGuideContent | null;
  revisions: SupportGuideRevisionSummary[];
  /** 公開の履歴に残す版の数（RAG_SUPPORT_GUIDE_MAX_PUBLISHED_REVISIONS。#2356）。 */
  max_published_revisions: number;
  issues: SupportGuideIssue[];
}

export interface SupportGuideValidationData {
  valid: boolean;
  issues: SupportGuideIssue[];
}

/** 取込の差分（#1288）の節。basic = 題名・説明。 */
export type SupportGuideDiffSection =
  | "basic"
  | "goal"
  | "applicability"
  | "conditions"
  | "steps"
  | "branches"
  | "references"
  | "completion"
  | "impact"
  | "handoff";
export type SupportGuideChangeKind = "added" | "removed" | "changed";

/** 既存のガイドと取り込むガイドの違い 1 つ。 */
export interface SupportGuideChange {
  section: SupportGuideDiffSection;
  kind: SupportGuideChangeKind;
  /** 行の id（資料は document_id）。行の無い節は空。 */
  key: string;
  label: string;
  /** 変わった項目（changed のときだけ）。 */
  fields: string[];
}

/** 取り込むガイドと同じ id・名前の既存のガイドとの差分（#1288）。 */
export interface SupportGuideImportDiff {
  guide_id: string;
  title: string;
  matched_by: "id" | "title";
  /** 比べた既存の内容（公開の版があれば公開の版、無ければ下書き）。 */
  base: "published" | "draft";
  revision: number;
  status: SupportGuideStatus;
  changes: SupportGuideChange[];
}

export interface SupportGuideImportItem {
  index: number;
  title: string | null;
  valid: boolean;
  issues: SupportGuideIssue[];
  existing?: SupportGuideImportDiff | null;
}

/** 業務ガイドで答えたときの要約（回答の診断の guide）。 */
export interface SupportGuideAnswerSummary {
  guide_id: string;
  revision: number;
  title: string;
  decision: "answer" | "branch" | "clarify" | "handoff";
  known_conditions: { id: string; label: string; value: string | null; source?: string | null }[];
  unknown_conditions: { id: string; label: string; handling?: string; state?: string; candidates?: string[] }[];
  /** 下書きで試し、その下書きを使ったとき true。 */
  draft?: boolean;
}

/** 業務ガイドの手順の充足（#2491）。 */
export type SupportGuideStepStatus =
  | "explained"
  | "partial"
  | "unknown"
  | "reference_only"
  | "unused"
  | "no_evidence"
  | "not_searched";

/** 下書きで試した回答の、業務ガイドの手順ごとの充足（#2686）。 */
export interface SupportGuideDraftTryStep {
  step_id: string;
  title: string;
  status: SupportGuideStepStatus;
  /** 回答の充足で必須の手順か（条件が決まらない分岐の手順は false）。 */
  required: boolean;
}

/** 下書きで試した回答（#1288）。利用者の回答の履歴・評価には入らない。 */
export interface SupportGuideDraftTryData {
  trace_id: string;
  guide_id: string;
  draft_revision: number;
  published_revision: number | null;
  /** この下書きのガイドが回答に使われたか。 */
  guide_used: boolean;
  guide: SupportGuideAnswerSummary | null;
  outcome: string | null;
  notice?: string | null;
  answer: string;
  citations: RetrievedChunk[];
  clarifications: { condition_id: string; label?: string; question?: string; options?: string[] }[];
  /** この下書きの手順ごとの充足（#2686）。ガイドを使わなかった・手順で答えなかったときは空。 */
  steps?: SupportGuideDraftTryStep[];
  elapsed_ms: number;
}

export interface SupportGuideImportPreviewData {
  items: SupportGuideImportItem[];
  importable_count: number;
}

export interface SupportGuideImportData {
  created: SupportGuideSummary[];
}

export interface SupportGuideExportData {
  schema_version: 2;
  guides: SupportGuideContent[];
}

/** 回答の過程の表の 1 行（Excel の行か、貼り付けたテキスト。#2397）。 */
export interface SupportGuideProcessRow {
  row: number;
  question: string;
  process: string;
  standard_answer?: string;
}

export interface SupportGuideProcessSheet {
  name: string;
  columns: { index: number; letter: string; header: string }[];
  row_count: number;
}

/** Excel のシートと列と、選んだ列で読んだ行。 */
export interface SupportGuideProcessWorkbookData {
  file_name: string;
  sheets: SupportGuideProcessSheet[];
  sheet: string | null;
  rows: SupportGuideProcessRow[];
  issues: string[];
  max_rows: number;
  truncated: boolean;
  next_start_row: number | null;
}

export interface SupportGuideProcessWorkbookSelection {
  sheet: string;
  question_column: number;
  process_column: number;
  answer_column: number | null;
  start_row: number;
}

export interface SupportGuideProcessReference {
  name: string;
  pages: string;
  /** 回答の過程に書いた表計算のシート・行・セル（書いたまま。#2663）。 */
  sheet_name?: string;
  rows?: string;
  cells?: string;
  section: string;
  /** この資料を根拠にする手順の id（空なら全手順に共通の資料。#2395）。 */
  step_ids: string[];
  status: "resolved" | "unresolved" | "ambiguous";
  document_id: string | null;
  file_name: string | null;
  candidates: string[];
}

export interface SupportGuideRuleCandidate {
  title: string;
  triggers: string[];
  content: string;
}

export interface SupportGuideProcessItem {
  row: number;
  question: string;
  valid: boolean;
  content: SupportGuideContent | null;
  issues: SupportGuideIssue[];
  references: SupportGuideProcessReference[];
  rule_candidates: SupportGuideRuleCandidate[];
}

export interface SupportGuideProcessPreviewData {
  items: SupportGuideProcessItem[];
  importable_count: number;
}

// --- 実行履歴（チャット・RAG 検索の実行。#1890） ---
export type RunSurface = "search" | "chat";
export type RunStatus = "success" | "blocked" | "error";
/** 資料との照合（回答の監査で、質問の要求ごとに根拠で答えられたか）。 */
export type RunVerification = "verified" | "partial" | "unverified";

/** 実行履歴の「実行者」の絞り込み（すべて / 自分が実行 / 他の人が実行。#1954）。 */
export type RunExecutorFilter = "all" | "mine" | "others";

export interface RunHistoryListParams {
  q?: string;
  surface?: RunSurface;
  status?: RunStatus;
  executor?: RunExecutorFilter;
  /** 開始日時がこの時刻以後の実行だけ（ISO 8601。期間の絞り込み。#1955）。 */
  since?: string;
  limit: number;
  offset: number;
}

export interface RunSummary {
  run_id: string;
  surface: RunSurface;
  search_answer_profile_id: string | null;
  search_answer_profile_name: string | null;
  /** 安全チェックで止めた質問は保存しないので null。一覧は先頭の一部だけ。 */
  question: string | null;
  answer_generated: boolean;
  status: RunStatus;
  answer_outcome: string | null;
  verification: RunVerification | null;
  validation_execution?: string | null;
  confidence: string | null;
  needs_human_review: boolean | null;
  citation_count: number;
  elapsed_ms: number | null;
  error_stage: string | null;
  model_id: string | null;
  conversation_id: string | null;
  message_id: string | null;
  created_at: string;
  /** 見ている利用者自身が実行したか（持ち主の会話で system_admin が実行した行は false。#1954）。 */
  executed_by_me: boolean;
  /** 実行した利用者（実行者）の表示名。見る人を問わず返る（引けない利用者は空文字。#1954）。 */
  executor_display_name: string;
  /**
   * モデルの利用量の合計（入力 + 出力の token）と計測の状態（#3466。一覧の「token」の列）。利用量の記録の無い実行は
   * どちらも null（「未計測」。0 にしない）、LLM を呼ばなかった実行は 0 と complete。古い backend は返さない。
   */
  total_tokens?: number | null;
  usage_measurement?: RunModelUsage["measurement"] | null;
}

export interface RunFeedback {
  feedback_id: string;
  target_type: "answer" | "citation";
  rating: "helpful" | "not_helpful";
  reason: string | null;
  document_id: string | null;
  created_at: string;
}

/** 作った回答を保存できなかった記録（回答は返す。#2342）。 */
export interface StorageFailure {
  /** 保存できなかったもの（回答の記録 / 会話の回答）。 */
  step: "answer_record_save" | "chat_message_save";
  error_type: string;
  /** ORA-03146 などのコード（例外の本文は持たない）。 */
  error_code: string | null;
}

export interface RunDetail extends RunSummary {
  error_type: string | null;
  /** 失敗のエラーコード（ORA-03146 など。#2342）。古い記録には無い。 */
  error_code?: string | null;
  /** 回答を作ったが保存できなかった記録（#2342）。古い記録には無い。 */
  storage_failures?: StorageFailure[];
  /** 会話の持ち主の表示名（会話の無い・削除された会話の実行は null、引けない利用者は空文字。#1954）。 */
  conversation_owner_display_name: string | null;
  /** 見ている利用者が会話の持ち主か（会話の無い実行は null。#1954）。 */
  owned_by_me: boolean | null;
  answer: string | null;
  /** 回答の記録が保持期間で消えた・読めないとき false。 */
  answer_available: boolean;
  /** 回答に付けた注意書き（回答の記録から。この機能より前の回答は null / 未指定。#2405）。 */
  answer_notice?: string | null;
  citations: RetrievedChunk[];
  /** 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（抜粋・文書は届かない。#3252）。 */
  withheld_evidence_count?: number;
  answer_diagnostics: Record<string, JsonValue> | null;
  feedback: RunFeedback[];
  /** RAG 検索の実行の件数と画面の絞り込み（「RAG 検索で開く」が画面に戻す。#3309）。チャット・古い記録は null。 */
  search_conditions?: RunSearchConditions | null;
  /**
   * 実行のモデルの呼び出しの回数と利用量（#3419）。この機能より前の記録・trace の始まる前に失敗した実行は null（未計測）。
   * LLM を呼ばなかった実行は `requests` が 0 の実測。
   */
  usage?: RunModelUsage | null;
}

/** 1 回の実行のモデルの利用量（#3419）。形は 3 製品で共通（`@production-ready/ui` の `RunModelUsageRecord`）。 */
export type RunModelUsage = RunModelUsageRecord;

/** RAG 検索の実行の条件（#3309）。`filters` は画面が送れる絞り込み（分類・基準日・抽出項目の値）だけ。 */
export interface RunSearchConditions {
  top_k: number | null;
  filters: Record<string, string>;
}

/**
 * 処理の経過の snapshot（共通の span の表を、記録の判定を通して読んだもの。#3226）。`events` は span の段のイベント
 * （未検証の入力）。`dropped_spans` は書けなかった span の数（1 以上のときだけ。「一部の処理だけを記録しています。」）。
 */
export interface TraceSnapshotData {
  events: unknown[];
  dropped_spans?: number | null;
}

// --- 保存された回答(rag_poc の answer JSON 相当) ---
export interface AnswerRecordSummary {
  trace_id: string;
  search_answer_profile_id: string | null;
  surface: "search" | "chat";
  answer_engine: string;
  question: string;
  rewritten_question: string | null;
  confidence: string | null;
  created_at: string;
}

export interface AnswerRecordDetail extends AnswerRecordSummary {
  answer: string;
  citations: RetrievedChunk[];
  /** 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（抜粋・文書は届かない。#3252）。 */
  withheld_evidence_count?: number;
  answer_diagnostics: Record<string, JsonValue>;
  /** 回答に付けた注意書き（この機能より前の回答・確認の質問は null / 未指定。#2405）。 */
  notice?: string | null;

}
