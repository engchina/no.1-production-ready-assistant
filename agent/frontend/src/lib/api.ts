import type { TraceRecordingSettingsData } from "@production-ready/system-settings";
import {
  DEFAULT_API_ERROR_DETAIL_LABELS,
  apiErrorDetail,
  httpApiErrorPresentation,
  isAbortError,
  toApiTransportError,
  type ApiErrorDetailLabels,
  type ApiErrorPresentable,
  type ApiErrorPresentation,
} from "@production-ready/ui";

// モデル設定の API 型は3製品共通（platform の共有パッケージ。#103）。
// データベース設定の API 型は3製品共通（platform の共有パッケージ。#108）。
export type {
  DatabaseConnectionSecurity,
  DatabaseConnectionTestResult,
  DatabaseConnectionTestStatus,
  DatabasePasswordRevealData,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
  DatabaseStatusData,
} from "@production-ready/system-settings";
import type {
  DatabaseConnectionTestResult,
  DatabaseSettingsData,
  DatabaseSettingsUpdate,
  DatabaseStatusData,
} from "@production-ready/system-settings";
// システムテーブルの API 型は3製品共通（platform の共有パッケージ。#325 / #751）。
import type {
  SystemTablesInitializeRequest,
  SystemTablesOperationData,
  SystemTablesStatusData,
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
// Cookie セッションの CSRF と 401 / 403 の通知は3製品共通（platform の共有パッケージ。#220 / #215）。
import { csrfHeader, notifyAuthResponse, type BaseCurrentUser } from "@production-ready/system-settings";
import { appPath } from "./base-path";
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
export interface ApiResponse<T> {
  data: T;
}

export interface ToolDefinition {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  permission_level: "read" | "write" | "sensitive";
  side_effects: boolean;
  timeout_seconds: number;
  max_retries: number;
  audit_tags: string[];
}

/** ツール権限の画面のツール（Control Plane のツールと MCP 接続のツール。#1814）。 */
export interface ToolPolicyTool {
  name: string;
  description: string;
  /** `control-plane`（組み込みのツール）か MCP 接続の ID。 */
  server_id: string;
  server_label: string;
  permission_level: ToolDefinition["permission_level"] | null;
  side_effects: boolean | null;
  /** ポリシーで個別に決めていないとき（既定に従う）の判定。不明なら null。 */
  default_decision: "allow" | "ask" | "deny" | null;
  /** その判定の理由（#1832）。read_only / untrusted_annotation / default_mode。 */
  default_reason?: "standard" | "read_only" | "untrusted_annotation" | "default_mode" | null;
  /** false は一覧に無い（接続を取れない・ツールが無くなった）が、保存したポリシーにある名前。 */
  available: boolean;
}

export interface ToolPolicyConnection {
  server_id: string;
  label: string;
  /** false は URL を設定していない（一覧は取らない）。 */
  configured: boolean;
  /** ツールの注釈（readOnlyHint）を既定の判定に使うか（#1832）。 */
  trust_tool_annotations?: boolean;
  tool_count: number;
  /** 一覧を取れなかった理由。 */
  error: string | null;
}

export interface ToolPolicyToolsData {
  tools: ToolPolicyTool[];
  connections: ToolPolicyConnection[];
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
  trace_id?: string;
  /** 業務 Agent のデータの範囲を当てた記録（#1378。範囲が効かなければ null）。 */
  data_scope?: ToolCallDataScope | null;
}

/** データの範囲を当てた記録（使ったプロファイルと、埋めた / 上書きした / 拒否した / 絞った）。 */
export interface ToolCallDataScope {
  connection: string;
  allowed_profile_ids: string[];
  action?: "filled" | "overridden" | "kept" | "rejected" | "filtered";
  argument?: string;
  profile_id?: string;
  requested_profile_id?: string;
  ignored_knowledge_base_ids?: string[];
}

export interface ToolResult {
  name: string;
  success: boolean;
  output?: Record<string, unknown> | null;
  error?: string | null;
  error_code?: string | null;
  error_details: Record<string, unknown>;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  policy_decision: "allow" | "ask" | "deny";
  approval_required: boolean;
  approval_id?: string | null;
  guardrail_warnings: string[];
  audit_metadata: Record<string, unknown>;
}

export interface RunStep {
  id: string;
  run_id: string;
  kind: string;
  status:
    | "pending"
    | "running"
    | "waiting_approval"
    | "completed"
    | "failed"
    | "cancelled";
  tool_call?: ToolCall | null;
  tool_result?: ToolResult | null;
  approval_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface RunEvent {
  id: string;
  run_id: string;
  type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ApprovalRequest {
  id: string;
  run_id: string;
  step_id: string;
  tool_call: ToolCall;
  status: "pending" | "approved" | "rejected" | "cancelled";
  reason: string;
  decided_by?: string | null;
  decided_at?: string | null;
  created_at: string;
}

/** 回答前の確認（#1839）の選択肢・確認事項・答え。backend の `confirmation.py` と同じ形。 */
export interface ConfirmationOption {
  id: string;
  label: string;
  description?: string;
}

export interface ConfirmationItem {
  id: string;
  prompt: string;
  reason?: string;
  selection: "single" | "multiple";
  options: ConfirmationOption[];
}

export interface ConfirmationAnswer {
  item_id: string;
  option_ids: string[];
  other_text: string;
}

/**
 * 回答前の確認の出所。`agent` は Agent 自身の確認（`ask_user_confirmation`。Run は確認待ちで止まる）、
 * `nl2sql` / `rag` はツールの結果の確認（完了した回答に出し、答えで次の回答を作る）。
 */
export type ConfirmationSource = "agent" | "nl2sql" | "rag";

export interface ConfirmationRequest {
  id: string;
  source: ConfirmationSource;
  call_id?: string;
  items: ConfirmationItem[];
  /** 確認の往復の何回目か（1 から）と上限。 */
  round: number;
  max_rounds: number;
  /** 「確認せずに回答」を出すか（backend が決める。今は 3 つの出所とも出す）。 */
  can_skip: boolean;
  status: "pending" | "answered" | "skipped" | "cancelled" | "expired";
  answers: ConfirmationAnswer[];
  created_at: string;
  decided_at?: string | null;
}

/**
 * 回答前の確認の操作。確認を終えるのは「確認せずに回答」（`skip`）だけで、画面は「確認をやめる」を持たない
 * （質問を取り消すのは入力欄の「停止」。#2311）。
 */
export type ConfirmationAction = "answer" | "skip";

export interface ConfirmationDecisionPayload {
  action: ConfirmationAction;
  answers?: ConfirmationAnswer[];
}

/** 回答前の確認の操作の結果。答え・スキップで作った次の回答（ツールの結果の確認）。 */
export interface ConfirmationOutcome {
  run: RunState;
  next_run?: RunState | null;
}

/** ツールの結果の確認に答えて作った回答の、答えの記録（Run の metadata `clarification_response`）。 */
export interface ConfirmationResponseRecord {
  confirmation_id: string;
  source: ConfirmationSource;
  action: "answer" | "skip";
  /** `free_text_only` は選択肢の無い（自由入力だけで答える）確認事項（#3410）。 */
  entries: { item_id: string; prompt: string; selected: string[]; other_text?: string; free_text_only?: boolean }[];
}

export interface RunState {
  id: string;
  goal: string;
  agent_id: string;
  /** 実行した Runtime（新しい Run は組み込み Runtime の "builtin"。#754）。 */
  runtime_id: string;
  status:
    | "queued"
    | "running"
    | "waiting_approval"
    | "waiting_confirmation"
    | "completed"
    | "failed"
    | "cancelled";
  steps: RunStep[];
  events: RunEvent[];
  approvals: ApprovalRequest[];
  /** 回答前の確認（#1839）。古い Run・確認の無い Run は無し / 空。 */
  confirmations?: ConfirmationRequest[];
  artifacts: Artifact[];
  pending_tool_calls: ToolCall[];
  metadata: Record<string, unknown>;
  /** Run を作った利用者（共通認証の user_uuid）。 */
  created_by_user_uuid?: string | null;
  /** 会話（スレッド。#768）。チャットの 1 往復が 1 Run。 */
  thread_id?: string | null;
  /** 質問に添付したファイル（#2517）。 */
  attachments?: ChatAttachmentApiReference[];
  // 以下は見ている利用者から見た事実（backend の `RunViewer`。#1954）。詳細（`GET /api/runs/{id}`）と会話の Run に
  // 付く。操作の応答の Run には無いことがある（無ければ自分の Run として扱う）。
  /** 見ている利用者が実行した Run か。 */
  executed_by_me?: boolean;
  /** 実行した人の表示名（見る人を問わず。引けなければ空）。 */
  executor_display_name?: string;
  /** 会話の持ち主が見ている利用者か（会話の無い Run は null）。 */
  owned_by_me?: boolean | null;
  /** 会話の持ち主の表示名（会話の無い Run は null。引けなければ空）。 */
  conversation_owner_display_name?: string | null;
  /** 承認・再開・Agent 自身の確認への回答を、見ている利用者ができるか（実行した人と system_admin だけ。#1928）。 */
  can_decide?: boolean;
  /** 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（抜粋・文書は届かない。#3252）。 */
  withheld_evidence_count?: number;
  /**
   * 取り消した人と入口（取り消した Run だけ。#3348）。API キーの名前は見ている利用者が見られるキーだけで、見られなければ空
   * （画面は「MCP」だけ）。詳細（`GET /api/runs/{id}`）と会話の Run に付く。
   */
  cancellation?: RunCancellationView | null;
  /** 回答への評価（#774）。評価していない Run は null / 無し。 */
  feedback?: RunFeedback | null;
  /** 管理者の評価（本人の評価とは別。#774）。 */
  admin_review?: RunFeedback | null;
  /** モデルの利用量（#772）。モデルを呼ぶ前の Run・記録を始める前の Run は null / 無し。 */
  usage?: RunUsage | null;
  /**
   * 処理の経過の span を共通の span の表に書く Run か（#3226 の案 A）。false・無しは古い形式の記録（画面は古い形式の
   * 案内を出す）。処理の経過は `GET /api/runs/{id}/trace`（`agentApi.getRunTrace`）で読む。
   */
  trace_store?: boolean;
  /** 実行の全体の span の trace id（受け付けただけ・開始の前に終わった Run は null）。 */
  trace_id?: string | null;
  /** 表に書けなかった span の数（1 以上なら一部だけの記録）。 */
  trace_dropped_spans?: number;
  created_at: string;
  updated_at: string;
}

/** 業種テンプレート（#780）。業務 Agent の新規作成のフォームに入れる出発点。 */
export interface AgentTemplate {
  id: string;
  category: string;
  name: string;
  description: string;
  instructions: string;
  skill_ids: string[];
  sample_questions: string[];
  evaluation_cases: { question: string; expected: string }[];
  /** 出力の項目（#2549）。 */
  output_fields?: AgentOutputField[];
}

/** 出力の項目の型（#2549）。 */
export type AgentOutputFieldType = "text" | "number" | "date" | "boolean";
/** 品質評価での比べ方（#2562）。meaning は文字の項目だけ（ほかの型は backend が exact にする）。 */
export type AgentOutputFieldMatch = "exact" | "meaning";

/** 業務 Agent の出力の項目（#2549。Run に record_output を渡し、記録した値を表で返す）。 */
export interface AgentOutputField {
  key: string;
  label: string;
  type: AgentOutputFieldType;
  required: boolean;
  description: string;
  match?: AgentOutputFieldMatch;
}

/** 業務の説明から作った業務 Agent の下書きの提案（#2519。保存はしない）。 */
export interface AgentDraftSuggestion {
  name: string;
  description: string;
  instructions: string;
  skill_ids: string[];
  data_scopes: AgentDataScopes;
  /** 出力の項目（#2561。決まった項目で結果を返す業務のときだけ。無ければ空）。 */
  output_fields: AgentOutputField[];
  evaluation_cases: { question: string; expected: string }[];
  /** 提案に使えなかった候補（取得できなかった接続など）。 */
  notices: string[];
}

/**
 * 問い返し（回答前の確認）の確認事項 1 件への答え（#1845）。共通の `ClarificationCard` の答え
 * （`{ itemId, optionIds, otherText }`）と同じ形を snake_case で持つ。
 */
export interface EvaluationClarificationAnswer {
  /** 答える確認事項の ID（NL2SQL の item_id / RAG の条件の id）。空なら、まだ答えていない確認事項へ順に。 */
  item_id: string;
  /** 選ぶ候補（候補の id か表示名）。 */
  option_ids: string[];
  /** 候補に無い答え（その他・自由入力）。 */
  other_text: string;
}

/** 品質評価のケース（#776）。`id` を省くと `case-<番号>`。 */
export interface EvaluationCase {
  id?: string;
  question: string;
  expected: string;
  /** 呼ぶべきツール（任意。`rag_search` のように MCP 接続の名前を省いてもよい）。 */
  expected_tools?: string[];
  /** 期待するツールをこの順に呼ぶ（先頭が最初に呼ぶツール。間にほかのツールを挟んでもよい。#1845）。 */
  expected_tools_in_order?: boolean;
  expected_approval?: boolean;
  /** 問い返しへの答え（任意。#1845）。 */
  clarification_answers?: EvaluationClarificationAnswer[];
  /** ケースの出どころ（フィードバック・Run の詳細から追加したときの Run の ID。#810）。 */
  source_run_id?: string | null;
  /** 期待する出力の項目（任意。#2558）。行ごとに「項目の key → 期待する値」。 */
  expected_output?: Record<string, EvaluationOutputValue>[];
}

/** 出力の項目の値（文字・数値・真偽・null。#2558）。 */
export type EvaluationOutputValue = string | number | boolean | null;

/** 出力の項目の 1 つの判定（#2558）。 */
export interface EvaluationFieldResult {
  row: number;
  field: string;
  label: string;
  expected: EvaluationOutputValue;
  actual: EvaluationOutputValue;
  correct: boolean;
  /** 比べ方（#2562）と、比べ方どおりに判定できなかった理由（意味の判定の失敗など）。 */
  method?: AgentOutputFieldMatch;
  /** 意味の判定の理由（#2568。意味で判定した項目だけ）。 */
  reason?: string;
  note?: string;
}

/** Run から作る評価ケースの下書き（保存しない。#810）。 */
export interface EvaluationCaseDraft {
  agent_id: string;
  agent_name: string;
  source_run_id: string;
  question: string;
  /** 管理者の評価のコメント（無ければ空）。 */
  expected: string;
  /** Run が呼んだ（呼ぼうとした）ツール。 */
  expected_tools: string[];
  /** Run が記録した出力の項目（#2558。期待する出力の下書き）。 */
  expected_output?: Record<string, EvaluationOutputValue>[];
  /** 同じ質問のケースを既に持つ評価セット。 */
  existing_set_ids: string[];
}

/** 評価する版（#810）。published = 公開中の版、draft = 下書き。 */
export type EvaluationTarget = "published" | "draft";
/** 評価した版（版の番号か "draft"。#810 より前の評価は null）。 */
export type EvaluatedAgentVersion = number | "draft" | null;

/** 評価セット（業務 Agent ごとに保存する評価ケースの集まり。#776）。 */
export interface EvaluationSetInput {
  agent_id: string;
  name: string;
  description: string;
  cases: EvaluationCase[];
}

export interface EvaluationSet extends EvaluationSetInput {
  id: string;
  cases: Required<EvaluationCase>[];
  created_by_user_uuid: string | null;
  created_at: string;
  updated_at: string;
}

export interface EvaluationSetItem {
  id: string;
  agent_id: string;
  name: string;
  description: string;
  case_count: number;
  updated_at: string;
  last_job_id: string | null;
  last_job_status: EvaluationJobStatus | null;
  last_pass_rate: number | null;
}

export type JudgeVerdict = "correct" | "incorrect" | "uncertain";
export type EvaluationCaseStatus =
  | "pending"
  | "running"
  | "judged"
  | "run_failed"
  | "needs_approval"
  | "approval_blocked"
  | "definition_changed"
  | "timed_out"
  | "judge_failed"
  | "cancelled";
export type EvaluationJobStatus = "queued" | "running" | "completed" | "cancelled" | "failed";

export interface EvaluationJudgement {
  verdict: JudgeVerdict;
  score: number;
  summary: string;
  missing_points: string[];
}

/** 問い返しへの 1 往復（#1845）。`run_id` の実行が問い返しで止まり、`message` を次の実行で送った。 */
export interface EvaluationClarificationRound {
  run_id: string;
  /** Run が出した確認事項と、当てた答え（`selected` は選んだ候補の表示名。答えを当てなかった確認事項は空。#1861）。 */
  questions: {
    item_id: string;
    kind: string;
    label: string;
    question: string;
    options: string[];
    selected?: string[];
    other_text?: string;
  }[];
  answers: EvaluationClarificationAnswer[];
  message: string;
}

export interface EvaluationCaseResult {
  case: Required<Omit<EvaluationCase, "expected_output" | "expected_approval">> & Pick<EvaluationCase, "expected_output" | "expected_approval">;
  /** 試行の番号（1 から。#62。結果は「ケース × 試行」で、#62 より前の評価は 1 回だけ）。 */
  trial?: number;
  status: EvaluationCaseStatus;
  run_id: string | null;
  answer: string;
  judgement: EvaluationJudgement | null;
  /** 業務 Agent が呼んだ（呼ぼうとした）ツール。評価中は承認が要るツールを実行しない。 */
  tool_calls: string[];
  blocked_tool_calls?: string[];
  executed_tool_calls?: string[];
  /** 期待するツールをすべて呼んだか（順を指定したケースは順も合うか。期待するツールが無いケースは null）。 */
  tool_selection_correct: boolean | null;
  /** 期待するツールを順どおりに呼んだか（順を指定しないケースは null。#1845）。 */
  tool_order_correct?: boolean | null;
  /** 判定のモデルを呼んだ回数（出力の上限で止まったら 1 回再試行する。#1845）。 */
  judge_attempts?: number;
  /** 問い返しへの往復（#1845。`run_id` は最後の実行）。 */
  clarification_rounds?: EvaluationClarificationRound[];
  /** 最後の実行も問い返しで終わった（答えが評価ケースに無い・往復の上限）。 */
  ended_with_clarification?: boolean;
  /** 出力の項目の判定（#2558。期待する出力の無いケースは空と null）。 */
  field_results?: EvaluationFieldResult[];
  field_correct?: boolean | null;
  error: string | null;
  duration_ms: number | null;
}

export interface EvaluationSummary {
  total: number;
  completed: number;
  correct: number;
  incorrect: number;
  uncertain: number;
  errors: number;
  /** 判定の失敗・実行条件の変更で合格率から除くケース。 */
  unjudged?: number;
  approval_blocked?: number;
  /** 正しいと判定したケース / 終わったケース（実行の失敗は不合格に数え、判定できなかったケースは除く）。 */
  pass_rate: number | null;
  average_score: number | null;
  /** ツールの選択の正しさ（期待するツールを指定したケースのうち、すべて呼んだ割合）。 */
  tool_cases: number;
  tool_correct: number;
  tool_accuracy: number | null;
  /** 出力の項目の正しさ（#2558）。すべての項目が正しいケースの割合と、項目ごとの正答率。合格率には含めない。 */
  field_cases?: number;
  field_correct?: number;
  field_accuracy?: number | null;
  field_items?: number;
  field_items_correct?: number;
  field_item_accuracy?: number | null;
  /**
   * 反復試行（#62）。件数と `pass_rate` は試行の単位、`judged_cases` は 1 回以上判定できたケースの数。
   * 区間は 95%（Wilson。単位はケース）。Pass@k / Pass^k の分母は k 回すべてを判定できたケース（`complete_cases`）。
   */
  trials?: number;
  cases?: number;
  judged_cases?: number;
  judged_trials?: number;
  pass_rate_interval?: EvaluationInterval | null;
  complete_cases?: number;
  incomplete_cases?: number;
  missing_trials?: number;
  stable_cases?: number;
  unstable_cases?: number;
  pass_at_k?: number | null;
  pass_at_k_interval?: EvaluationInterval | null;
  pass_hat_k?: number | null;
  pass_hat_k_interval?: EvaluationInterval | null;
}

/** 95% の区間の [下限, 上限]（#62）。 */
export type EvaluationInterval = [number, number];

/** ケースの k 回の試行の安定性（#62）。 */
export type EvaluationStability = "stable_pass" | "stable_fail" | "unstable" | "incomplete" | "unjudged";

/** ケースごとの k 回の試行の集計（#62）。 */
export interface EvaluationCaseTrials {
  case_id: string;
  question: string;
  trials: number;
  judged: number;
  passed: number;
  missing: number;
  rate: number | null;
  pass_at_k: boolean | null;
  pass_hat_k: boolean | null;
  stability: EvaluationStability;
}

export interface EvaluationJob {
  id: string;
  agent_id: string;
  agent_name: string;
  set_id: string;
  set_name: string;
  /** 評価した版（#810）。 */
  agent_version: EvaluatedAgentVersion;
  /** 同じ評価セット・同じ評価ケースの前回（完了した評価）。比べた版と日時（#810）。 */
  previous_job_id: string | null;
  previous_summary: EvaluationSummary | null;
  previous_agent_version: EvaluatedAgentVersion;
  previous_created_at: string | null;
  status: EvaluationJobStatus;
  created_by_user_uuid: string | null;
  results: EvaluationCaseResult[];
  /** 1 ケースを反復した回数（#62）と、評価者（判定のモデルと判定の指示）の指紋。 */
  trials?: number;
  judge_fingerprint?: string | null;
  /** ケースごとの k 回の試行の集計（#62）。 */
  case_trials?: EvaluationCaseTrials[];
  error: string | null;
  summary: EvaluationSummary;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type EvaluationJobItem = Pick<
  EvaluationJob,
  | "id"
  | "agent_id"
  | "agent_name"
  | "set_id"
  | "set_name"
  | "agent_version"
  | "trials"
  | "judge_fingerprint"
  | "status"
  | "summary"
  | "created_at"
  | "finished_at"
>;

/** 評価の比較の変化（合否で決める。#1845）。 */
export type EvaluationComparisonChange = "improved" | "regressed" | "unchanged" | "base_only" | "target_only" | "not_comparable";

export interface EvaluationComparisonOutcome {
  case_id: string;
  /** correct / incorrect / uncertain か、判定まで進まなかったケースの状態。 */
  status: string;
  passed: boolean;
  score: number | null;
  tool_calls: string[];
  tool_selection_correct: boolean | null;
  /** 出力の項目がすべて正しいか（#2558。期待する出力の無いケースは null）。 */
  field_correct?: boolean | null;
  run_id: string | null;
  /** 試行の数・判定できた試行・成功した試行と成功率（#62）。 */
  trials?: number;
  judged_trials?: number;
  passed_trials?: number;
  success_rate?: number | null;
}

export interface EvaluationComparisonRow {
  question: string;
  base: EvaluationComparisonOutcome | null;
  target: EvaluationComparisonOutcome | null;
  change: EvaluationComparisonChange;
  /** 2 つのケースの定義（期待・ツールなど）が同じか（#62。違えば対応比較に入れない）。 */
  same_definition?: boolean;
}

/** 同じ定義のケースどうしの、ケースごとの成功率の差（比べる評価 − 基準。#62）。 */
export interface EvaluationPairedComparison {
  cases: number;
  base_rate: number;
  target_rate: number;
  difference: number;
  interval: EvaluationInterval;
  /** 区間が 0 をまたがない（差の向きが確か）。 */
  conclusive: boolean;
  improved: number;
  regressed: number;
  unchanged: number;
}

/** 2 つの評価をケースごとに並べた比較（#1845）。 */
export interface EvaluationComparison {
  base: EvaluationJobItem;
  target: EvaluationJobItem;
  rows: EvaluationComparisonRow[];
  improved: number;
  regressed: number;
  unchanged: number;
  base_only: number;
  target_only: number;
  /** 比較できる条件か（#62）。 */
  same_cases?: boolean;
  definition_mismatch?: number;
  same_judge?: boolean | null;
  paired?: EvaluationPairedComparison | null;
}

/** 評価の履歴の 1 ページ（共通の `Page`。`total` は対象の評価の件数。#1266）。 */
export type EvaluationJobsPage = Page<EvaluationJobItem>;

/** Run が使ったモデルの量（承認待ちからの再開を含めた累計。#772）。 */
/** 実行の詳細の「取消」（backend の `RunCancellationView`。#3348）。 */
export interface RunCancellationView {
  /** 画面の操作・外部の MCP（API キー）・操作した人の記録の無い自動の取消。 */
  via: "screen" | "mcp" | "system";
  /** 取り消した人の表示名（引けない利用者は空）。 */
  actor_display_name: string;
  by_me: boolean;
  api_key_name: string;
}

export interface RunUsage {
  model: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

/** 利用状況の集計の 1 行（#772）。 */
export interface UsageTotals {
  runs: number;
  /** 利用量を記録した Run（記録を始める前の Run・モデルを呼ぶ前に止まった Run は含まない）。 */
  runs_with_usage: number;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

/** 集計の期間（日。90 日を超える期間は保存した Run の履歴で集計する。#794）。 */
export type ReportPeriodDays = 7 | 30 | 90 | 180 | 365;
export const REPORT_PERIOD_DAYS: readonly ReportPeriodDays[] = [7, 30, 90, 180, 365];
/** 集計元。history = 保存した Run の履歴（Oracle）、memory = バックエンドのメモリの Run（#794）。 */
export type ReportSource = "history" | "memory";

export type UsagePeriodDays = ReportPeriodDays;

export interface UsageReport {
  days: UsagePeriodDays;
  source: ReportSource;
  timezone: string;
  since: string;
  until: string;
  totals: UsageTotals;
  /** 直前の同じ長さの期間。 */
  previous: UsageTotals;
  by_agent: (UsageTotals & { agent_id: string; agent_name: string })[];
  by_user: (UsageTotals & { user_uuid: string | null; display_name: string })[];
  by_model: (UsageTotals & { model: string })[];
  /** 期間のすべての日（古い順。Run の無い日も 0 で入る）。 */
  by_day: (UsageTotals & { day: string })[];
}

export interface ToolAuditRecord {
  step_id: string;
  tool_name: string;
  status: string;
  approval_id?: string | null;
  approval_status?: string | null;
  policy_decision?: string | null;
  permission_level?: string | null;
  side_effects?: boolean | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  success?: boolean | null;
  error?: string | null;
  error_code?: string | null;
  guardrail_warnings: string[];
  trace_id?: string | null;
  artifact_ids: string[];
  audit_metadata: Record<string, unknown>;
}

export interface RunAuditData {
  run_id: string;
  goal: string;
  status: string;
  records: ToolAuditRecord[];
}

export interface ToolCallAuditRecord extends ToolAuditRecord {
  run_id: string;
  run_goal: string;
  run_status: string;
  agent_id: string;
  run_created_at: string;
  run_updated_at: string;
  /** Run のきっかけ（user / evaluation / mcp / replay。#1874）。 */
  run_source?: string;
  /** MCP の Run の API キーの名前（「MCP（キーの名前）」）。 */
  api_key_name?: string | null;
}

/** ツール監査の 1 ページ（共通の `Page` に絞り込みの条件とツール名を足した形。#1266）。 */
export interface ToolCallAuditData extends Page<ToolCallAuditRecord> {
  filters: Record<string, unknown>;
  /** 見られる範囲の監査に記録されたツール名（絞り込みに依らない。MCP 接続のツールを含む。#983）。 */
  tool_names?: string[];
}

export interface ToolCallAuditFilters {
  run_id?: string;
  tool_name?: string;
  status?: string;
  approval_status?: string;
  error_code?: string;
  has_guardrail_warnings?: boolean;
  offset?: number;
  limit?: number;
}

/** Run の RAG の根拠の原文のプレビュー（#2389。URL は RAG が利用者・根拠・版に縛って署名した短命の URL）。 */
export interface RunSourcePreview {
  kind: "pdf" | "image" | "text" | "unsupported";
  file_name: string;
  pages: { page_number: number; width: number; height: number }[];
  /** ページの画像の URL のひな形（`{page}` をページ番号に置き換え、query の `dpi` で解像度）。 */
  page_url_template: string | null;
  content_url: string;
  /** 強調する位置を決める根拠の metadata（頁・bbox・要素の表示領域）。 */
  highlight_metadata: Record<string, unknown>;
  expires_at: string;
  expires_in_seconds: number;
}

/** 段落の印・外した段落・回答全体の注意の理由（#2738。契約は platform/contracts/answer-passages/passage-marks.json）。 */
export interface AnswerPassageReasonData {
  code: string;
  label: string;
  detail?: string;
}

/**
 * 成果物「資料との照合」（kind=`answer_validation`）の内容のうち、段落の印の記録（#2738）。根拠と矛盾した段落だけを
 * 本文から外し（`removed_passages`）、確かめられない段落・規則の検査で注意の要る段落は本文に残して印（`passage_marks`）を
 * 付け、回答全体の注意（`answer_warnings`）を持つ。`withheld` は件数（`claims` は持たない）。この形より前の Run には無い。
 */
export interface AnswerValidationPassageContent {
  passage_marks?: { text: string; state: "unverified" | "warning"; reasons: AnswerPassageReasonData[] }[];
  removed_passages?: { text: string; reasons: AnswerPassageReasonData[] }[];
  answer_warnings?: AnswerPassageReasonData[];
  withheld?: { removed: number; unverified: number; warnings: number; all: boolean; listed: number };
}

export interface Artifact {
  id: string;
  name: string;
  kind: string;
  content: Record<string, unknown>;
  content_ref?: {
    backend: string;
    uri: string;
    content_type: string;
    size_bytes?: number | null;
    sha256?: string | null;
  } | null;
  created_at: string;
}

export interface AgentProfile {
  id: string;
  name: string;
  description: string;
  instructions: string;
  skill_ids: string[];
  /** 組み込み Runtime で使うモデル（空なら既定のテキストモデル。#754）。 */
  model_id?: string;
  /** 公開した版（#770）。利用者の Run は公開中の版（published_version）で実行する。 */
  versions: AgentVersion[];
  published_version: number | null;
  /** 下書きに公開していない変更があるか（公開した版が無いときも true。スキルの更新も含む。#1855）。 */
  unpublished_changes: boolean;
  /** 下書きの項目（名前・指示・スキルの選択など）が公開中の版と違うか（スキルの更新は含めない）。 */
  draft_changed?: boolean;
  /** 公開中の版に固定した後に中身が変わった（消えた）スキルの ID（#1855）。 */
  skill_updates?: string[];
  /** 公開中の版がスキルの中身を固定しているか（false は #1855 より前の版）。 */
  skills_pinned?: boolean;
  /** スキルの更新の扱い（#1867）。pinned = 公開の版に固定（既定）、latest = 常に今のスキル。 */
  skill_policy?: SkillPolicy;
  /** 出力の項目（#2549）。 */
  output_fields?: AgentOutputField[];
  /** 作成に使った業種テンプレート（#810。空は使っていない）。 */
  template_id?: string;
  /** データの範囲（#1378。接続 → 使えるプロファイルと既定）。無い接続は範囲なし。 */
  data_scopes?: AgentDataScopes;
  /** 回答前の確認（#1839）。曖昧な質問の扱い（既定は確認してから回答する）。 */
  clarification_policy?: ClarificationPolicy;
  /** 確認の上限（往復。0 は既定の 3 回、2〜6。NL2SQL の業務プロファイルと同じ）。 */
  clarification_max_rounds?: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  /** 個人の業務 Agent の持ち主（#2523）。null は組織の業務 Agent。 */
  owner_user_uuid?: string | null;
  /** 共有の申請と承認の記録。 */
  share_request?: AgentShareRequest | null;
}

export interface AgentShareRequest {
  status: "requested" | "rejected" | "approved";
  requested_by_user_uuid?: string;
  approved_name?: string;
  note: string;
  requested_at: string;
  /** 却下の理由（status が rejected のとき）。 */
  reason: string;
  decided_at: string | null;
}

/** 曖昧な質問の扱い（#1839）。`confirm` = 確認してから回答する、`assume` = 前提を示して回答する。 */
export type ClarificationPolicy = "confirm" | "assume";

/** データの範囲を持てる接続（標準の MCP 接続。#1378）。 */
export type AgentDataScopeConnection = "nl2sql" | "rag";

export interface AgentDataScope {
  profile_ids: string[];
  default_profile_id: string;
}

export type AgentDataScopes = Partial<Record<AgentDataScopeConnection, AgentDataScope>>;

/** データの範囲に選べるプロファイル（編集者が使えるもの。#1378）。 */
export interface DataScopeCandidate {
  id: string;
  name: string;
  description: string;
}

export interface DataScopeCandidates {
  connection: AgentDataScopeConnection;
  label: string;
  profiles: DataScopeCandidate[];
}

export interface AgentProfileWritePayload {
  id?: string;
  name: string;
  description?: string;
  instructions?: string;
  skill_ids: string[];
  model_id?: string;
  data_scopes?: AgentDataScopes;
  clarification_policy?: ClarificationPolicy;
  clarification_max_rounds?: number;
  enabled: boolean;
  /** 作成に使った業種テンプレート（#810）。 */
  template_id?: string;
  /** 出力の項目（#2549）。 */
  output_fields?: AgentOutputField[];
}

export interface AgentProfilePatchPayload {
  name?: string;
  description?: string;
  instructions?: string;
  skill_ids?: string[];
  model_id?: string;
  data_scopes?: AgentDataScopes;
  clarification_policy?: ClarificationPolicy;
  clarification_max_rounds?: number;
  enabled?: boolean;
  skill_policy?: SkillPolicy;
  /** 出力の項目（#2549）。 */
  output_fields?: AgentOutputField[];
}

export type McpAuthMode = "none" | "api_key" | "oauth_client_credentials" | "service_token";

/** MCP 接続（#757。RAG / NL2SQL / 外部 MCP）。資格情報の値は返らず、設定済みかだけを返す。 */
export interface McpConnectionSettings {
  server_id: string;
  label?: string | null;
  /** URL の userinfo・資格情報らしい query の値は `***` に伏せて返る（#1056）。 */
  base_url?: string | null;
  /** 保存済みの URL に資格情報があり、base_url を伏せて返したか。 */
  base_url_masked?: boolean;
  auth_mode: McpAuthMode;
  /** サービストークンの aud（呼び先の製品名）。service_token のときだけ。 */
  service_audience?: string | null;
  timeout_seconds: number;
  /** builtin（RAG / NL2SQL）/ env / runtime。runtime だけ削除できる。 */
  source: string;
  removable: boolean;
  /** 配備（環境変数）が URL を決めた標準の接続か（#1325）。true なら URL は変えられない。 */
  base_url_locked?: boolean;
  /** URL と認証方式に必要な資格情報がそろっているか。 */
  configured: boolean;
  api_key_configured: boolean;
  oauth_configured: boolean;
  session_configured: boolean;
  /** 共通 .env の PLATFORM_SERVICE_TOKEN_SECRET が 32 文字以上あるか（値は返らない）。 */
  service_token_configured: boolean;
  /** ツールの注釈（readOnlyHint）を既定の判定に使うか（#1832。標準の接続は常に true）。 */
  trust_tool_annotations?: boolean;
}

export interface McpConnectionsData {
  connections: McpConnectionSettings[];
}

export interface McpConnectionWritePayload {
  server_id?: string;
  label?: string | null;
  base_url?: string | null;
  auth_mode?: McpAuthMode;
  api_key?: string | null;
  timeout_seconds?: number;
  session_id?: string | null;
  oauth_token_url?: string | null;
  oauth_client_id?: string | null;
  oauth_client_secret?: string | null;
  oauth_scope?: string | null;
  service_audience?: string | null;
  trust_tool_annotations?: boolean;
}

export interface ExternalMcpToolInfo {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema?: Record<string, unknown> | null;
  server_id?: string | null;
  /** MCP の readOnlyHint。注釈を信頼する接続では、true のツールは既定で承認なし。 */
  read_only: boolean;
  /** ツール権限で個別に決めていないときの判定（#1832）。 */
  default_decision?: "allow" | "ask" | "deny" | null;
  /** Agent のモデルに渡すツール名（`<接続>__<ツール>`）。 */
  function_name?: string | null;
  metadata: Record<string, unknown>;
}

export interface ExternalMcpToolsData {
  tools: ExternalMcpToolInfo[];
  metadata: Record<string, unknown>;
}

export interface AgentSkill {
  id: string;
  name: string;
  description: string;
  instructions: string;
  mcp_requirements: { server_id: string; tool_names: string[] }[];
  /** SKILL.md と同じディレクトリの references/ の参照文書（パスと文字数。本文は返さない。#1803）。 */
  references: { path: string; characters: number }[];
  enabled: boolean;
  tags: string[];
  source: string;
  created_at?: string;
  updated_at?: string;
}

export interface AgentSkillListData {
  skills: AgentSkill[];
  metadata: Record<string, unknown>;
}

export interface AgentSkillWritePayload {
  id?: string;
  name?: string;
  description?: string;
  instructions?: string;
  mcp_requirements?: { server_id: string; tool_names: string[] }[];
  enabled?: boolean;
  tags?: string[];
}

export interface TraceExportRetryData {
  attempted: number;
  succeeded: number;
  requeued: number;
  dropped: number;
  skipped: number;
  queue_size: number;
}

export interface TracePolicySettings {
  trace_events_enabled: boolean;
  trace_events_buffer_size: number;
  trace_events_retention_seconds: number;
  trace_sample_rate: number;
}

export interface ToolPolicySettings {
  default_mode: "approval" | "deny";
  allow: string[];
  ask: string[];
  deny: string[];
}

/** 公開した業務 Agent の版（#770）。 */
export interface AgentVersion {
  version: number;
  name: string;
  description: string;
  instructions: string;
  skill_ids: string[];
  model_id: string;
  note: string;
  published_at: string;
  published_by?: string | null;
  /** 公開のときに固定したスキルの中身（#1855。null は固定していない版）。 */
  skills?: PinnedSkill[] | null;
  /** スキルの更新の扱い（#1867）。 */
  skill_policy?: SkillPolicy;
  /** 出力の項目（#2549）。 */
  output_fields?: AgentOutputField[];
}

/** スキルの更新の扱い（#1867）。 */
export type SkillPolicy = "pinned" | "latest";

/** 指示の差分の 1 行（固定した版 → 今のスキル。#1867）。 */
export interface SkillDiffLine {
  kind: "hunk" | "context" | "added" | "removed";
  text: string;
}

export interface SkillSnapshot {
  name: string;
  description: string;
  enabled: boolean;
  tools: string[];
  references: string[];
}

/** 固定したスキルと今のスキルの差（#1867）。 */
export interface SkillUpdateDetail {
  skill_id: string;
  change: "changed" | "removed";
  pinned: SkillSnapshot;
  current: SkillSnapshot | null;
  instructions_diff: SkillDiffLine[];
  truncated: boolean;
  tools_added: string[];
  tools_removed: string[];
  references_added: string[];
  references_removed: string[];
  references_changed: string[];
}

export interface AgentSkillUpdates {
  agent_id: string;
  published_version: number | null;
  items: SkillUpdateDetail[];
}

export interface SkillUpdatePublishResult {
  agent_id: string;
  name: string;
  status: "published" | "skipped";
  version: number | null;
  previous_version: number | null;
  reason: string | null;
}

/** 業務 Agent の公開の版に固定したスキル（#1855）。 */
export interface PinnedSkill {
  id: string;
  name: string;
  description: string;
  content_hash: string;
  skill_updated_at?: string | null;
}

export interface CreateRunPayload {
  goal: string;
  agent_id?: string;
  metadata?: Record<string, unknown>;
  /** 公開前の下書きで実行する（Agent 管理の権限が要る。#770）。 */
  draft?: boolean;
  /** 続ける会話（#768）。省略すると新しい会話を始める。 */
  thread_id?: string;
  /**
   * 確認の取り消しの操作（#1441・#1445）。`nl2sql` は「データベースの検索は不要」、`rag` は「資料の検索は不要」。
   * この Run ではその種類のツールを使わずに続ける。
   */
  clarification_cancel?: ClarificationKind;
  /** 質問に添付するファイル（`uploadChatAttachment` の `attachment_id`。#2517）。 */
  attachment_ids?: string[];
}

/** 会話の添付の一覧と上限（#2517）。 */
export interface ChatAttachmentListData {
  items: ChatAttachmentApiItem[];
  limits: ChatAttachmentApiLimits;
}

/** 確認の種類（NL2SQL の SQL の生成前の確認・RAG の確認。#1441・#1445）。 */
export type ClarificationKind = "nl2sql" | "rag";

/** チャットの会話の一覧の 1 件（#768）。 */
/** 回答の評価（RAG と同じ値。#774）。 */
export type FeedbackRating = "helpful" | "not_helpful";

/** 役に立たなかった理由（表示の順。#774）。 */
export const FEEDBACK_REASONS = [
  "incorrect",
  "incomplete",
  "not_relevant",
  "answer_untrusted",
  "ambiguous_question",
  "wrong_action",
] as const;
export type FeedbackReason = (typeof FEEDBACK_REASONS)[number];

export interface RunFeedback {
  rating: FeedbackRating;
  reason: FeedbackReason | null;
  comment: string;
  user_uuid: string | null;
  updated_at: string;
}

export interface RunFeedbackPayload {
  rating: FeedbackRating;
  reason?: FeedbackReason;
  comment?: string;
}

export type FeedbackPeriodDays = ReportPeriodDays;

export interface FeedbackSummary {
  rated_runs: number;
  total: number;
  helpful: number;
  not_helpful: number;
  /** 評価が 0 件のときは null。 */
  helpful_rate: number | null;
  reason_counts: { reason: FeedbackReason; count: number }[];
  /** 管理者の評価の件数と、そのうち役に立たなかった件数。 */
  admin_reviewed: number;
  admin_not_helpful: number;
}

export interface FeedbackItem {
  run_id: string;
  thread_id: string | null;
  agent_id: string;
  agent_name: string;
  /** 会話の本人（Run の作成者）。 */
  user_uuid: string | null;
  display_name: string;
  question: string;
  answer: string;
  /** 本人の評価・管理者の評価（どちらかは必ずある）。 */
  feedback: RunFeedback | null;
  admin_review: RunFeedback | null;
  reviewer_display_name: string;
  /** 新しい方の評価の日時。 */
  updated_at: string;
}

/**
 * フィードバックの集計と一覧（共通の `Page` に集計を足した形。#1266）。
 * `items` は絞り込みに合う評価のうち `offset` から `limit` 件（新しい順）、`total` は絞り込みに合う評価の件数
 * （ページングの総数。`summary.total` は期間内の評価の件数で別）。
 */
export interface FeedbackReport extends Page<FeedbackItem> {
  days: FeedbackPeriodDays;
  source: ReportSource;
  since: string;
  until: string;
  summary: FeedbackSummary;
  /** 直前の同じ長さの期間。 */
  previous: FeedbackSummary;
}

export interface FeedbackFilters {
  days: FeedbackPeriodDays;
  agentId?: string;
  rating?: FeedbackRating;
  reason?: FeedbackReason;
  offset?: number;
  limit?: number;
}

/**
 * 確かめるべき実行のシグナルの種類（表示の順。#1822）。定義は backend の `run_signals.py`。
 * - not_helpful: 本人か管理者の評価が「役に立たなかった」
 * - validation_failed: 最終の検証が根拠で確かめられない段落などのために回答を変えた
 * - unvalidated: 最終の検証そのものが失敗した
 * - ungrounded_no_tools: 資料の根拠も業務データの道具（NL2SQL など）も使わずに答えた（#1834）
 * - unanswered: 回答の対応が「根拠不足」か「人への引き継ぎ」
 * - approval_rejected / tool_failed / run_failed: 承認の却下・ツールの失敗・実行の失敗と取り消し
 */
export const SIGNAL_KINDS = [
  "not_helpful",
  "validation_failed",
  "unvalidated",
  "ungrounded_no_tools",
  "unanswered",
  "approval_rejected",
  "tool_failed",
  "run_failed",
] as const;
export type SignalKind = (typeof SIGNAL_KINDS)[number];

export interface SignalSummary {
  /** 期間に作った、終わった実行（品質評価の実行を除く）。 */
  runs: number;
  /** シグナルのある実行。 */
  flagged: number;
  /** 対象の実行が 0 件のときは null。 */
  flagged_rate: number | null;
  /** すべての種類（表示の順。0 件も含む。1 つの実行が複数に入る）。 */
  counts: { kind: SignalKind; count: number }[];
}

export interface SignalItem {
  run_id: string;
  thread_id: string | null;
  agent_id: string;
  agent_name: string;
  user_uuid: string | null;
  display_name: string;
  question: string;
  answer: string;
  status: RunState["status"];
  signals: SignalKind[];
  answer_outcome: string | null;
  validation_result: string | null;
  tool_failures: number;
  approvals_rejected: number;
  /** 業務データの道具の成功した呼び出しの数（記録の無い古い実行は null。#1834）。 */
  data_tool_calls: number | null;
  feedback: RunFeedback | null;
  admin_review: RunFeedback | null;
  created_at: string;
}

/** 確かめるべき実行の集計と一覧（#1822）。`total` は種類の絞り込みに合う実行の件数。 */
export interface SignalReport extends Page<SignalItem> {
  days: FeedbackPeriodDays;
  source: ReportSource;
  since: string;
  until: string;
  kind: SignalKind | null;
  summary: SignalSummary;
  /** 直前の同じ長さの期間。 */
  previous: SignalSummary;
}

export interface SignalFilters {
  days: FeedbackPeriodDays;
  agentId?: string;
  kind?: SignalKind;
  offset?: number;
  limit?: number;
}

export interface ThreadSummary {
  thread_id: string;
  agent_id: string;
  /** 最初の質問の 1 行目。 */
  title: string;
  run_count: number;
  last_status: RunState["status"];
  created_at: string;
  updated_at: string;
  /** 見ている利用者の会話か。system_admin が他の利用者の会話を見るときは false（#1889）。 */
  owned?: boolean;
  /** system_admin の一覧だけに付く利用者の表示名（引けない利用者は空。#1889・#1696）。 */
  actor_display_name?: string;
}

/**
 * 会話の一覧の 1 ページ（新しい順。共通の Page。#1265 / #1266）。`history_days` は一覧を絞った期間
 * （最後の更新が直近 N 日。0 は絞らない。#2340）。
 */
export type ThreadsData = Page<ThreadSummary> & { history_days?: number };

/** 実行履歴の一覧の 1 行（step・Event・成果物は持たない。詳細は `getRun`。#1897）。 */
export interface RunSummary {
  id: string;
  goal: string;
  agent_id: string;
  runtime_id: string;
  status: RunState["status"];
  created_at: string;
  updated_at: string;
  created_by_user_uuid?: string | null;
  thread_id?: string | null;
  pending_approval_count: number;
  /** 一覧の行から承認・却下する承認待ちの承認（最初の 1 件）。 */
  pending_approval?: { id: string; tool_name: string } | null;
  /** 承認がすべて決まり、保存した状態から再開を待つ（手動の「再開」を出す）。 */
  resume_pending: boolean;
  /** 一覧の「きっかけ」（backend の `run_facts.run_source`）と、MCP の Run の API キーの名前（#1874）。 */
  source: string;
  api_key_name?: string | null;
  /** 見ている利用者が実行した Run か・実行した人の表示名・待ちの操作をできるか（#1954）。 */
  executed_by_me?: boolean;
  executor_display_name?: string;
  can_decide?: boolean;
  /** 終了日時と所要時間（終わった Run だけ。実行中・待ちは null）と、モデルの token の合計（#1955）。 */
  finished_at?: string | null;
  duration_ms?: number | null;
  total_tokens?: number | null;
}

/** 実行履歴の状態の絞り込み（`active` は実行中・待機中、`waiting` は承認待ち・確認待ち。#1955）。 */
export type RunListStatus = "active" | "waiting" | RunState["status"];

/** 実行履歴の種類（きっかけ。backend の `run_facts.run_source`）の絞り込み（#1955）。 */
export type RunListKind = "user" | "evaluation" | "mcp" | "replay";
/** 種類の絞り込みで送る値。`not_evaluation` は品質評価の Run を除く（画面の既定。#1983）。 */
export type RunListKindQuery = RunListKind | "not_evaluation";

/** 実行履歴の実行者の絞り込み（すべて / 自分が実行 / 他の人が実行。backend の `RunExecutorFilter`。#1954）。 */
export type RunExecutorFilter = "all" | "mine" | "others";

export interface RunListFilters {
  status?: RunListStatus;
  /** 既定（all）は送らない。 */
  executor?: Exclude<RunExecutorFilter, "all">;
  /** 「すべて」は送らない。 */
  kind?: RunListKindQuery;
  /** 期間の始まり（ISO 8601。`runPeriodSince`）。 */
  since?: string;
  q?: string;
  limit: number;
  offset: number;
}

/**
 * 実行履歴の一覧の 1 ページ。`total` は絞り込んだ件数、`unfiltered_total` は見られる全件数。
 * `creator_display_names` は Run を実行した利用者（UUID）→ 表示名。system_admin が全員の Run を見るときだけ（#1889）。
 */
export type RunsPage = Page<RunSummary> & {
  /** 見られる全件数。種類の絞り込みが品質評価を含まない（既定の「品質評価以外」など）ときは品質評価の Run を除く（#1983）。 */
  unfiltered_total: number;
  /** 見られる品質評価の Run の数（既定で隠れている数。#1983）。 */
  evaluation_total?: number;
  creator_display_names?: Record<string, string>;
  /** 他の人が実行した Run を 1 件でも見られるか（「実行者」の列と絞り込みを出す。#1955）。 */
  has_other_executors?: boolean;
};

/** 承認の一覧の 1 行（#1897）。引数などの詳細は `getApproval`。 */
export interface ApprovalSummary {
  id: string;
  run_id: string;
  tool_name: string;
  status: ApprovalRequest["status"];
  created_at: string;
  decided_by?: string | null;
  decided_at?: string | null;
  run_goal: string;
  run_status: RunState["status"];
  agent_id: string;
}

export interface ApprovalListFilters {
  /** `decided` は判断済み（承認・却下・取り消し）。省略はすべて。 */
  status?: "pending" | "decided";
  q?: string;
  limit: number;
  offset: number;
}

export type ApprovalsPage = Page<ApprovalSummary> & { unfiltered_total: number };

/** 承認の詳細（承認と、その実行の要約）。`user_display_names` は判断者（ログイン ID）→ 表示名（#1696）。 */
export interface ApprovalDetail {
  approval: ApprovalRequest;
  run: RunSummary;
  user_display_names?: Record<string, string>;
}

export interface ThreadData {
  thread_id: string;
  agent_id: string;
  /** 会話の Run（古い順）。 */
  runs: RunState[];
  /** 見ている利用者の会話か（#1889）。 */
  owned?: boolean;
  /** system_admin が見るときの利用者の表示名（#1889）。 */
  actor_display_name?: string;
  /**
   * 持ち主以外（system_admin）が送った往復の、送った人の表示名（Run の ID → 表示名。#1928）。
   * 引けない利用者は空の文字列。
   */
  sender_display_names?: Record<string, string>;
}

/** 組み込み Runtime で選べるモデル（システム設定 > モデル の登録モデル。#754）。 */
export interface BuiltinRuntimeModel {
  model_id: string;
  display_name: string;
}

/** 組み込み Runtime（OpenAI Agents SDK + モデルの API）の状態。API key は含まない（#754）。 */
export interface BuiltinRuntimeStatus {
  id: string;
  name: string;
  sdk: string;
  sdk_version: string;
  model_provider: string;
  /** 既定のテキストモデル（空なら未設定）。 */
  model_id: string;
  ready: boolean;
  error_code?: string | null;
  message?: string | null;
  models: BuiltinRuntimeModel[];
}

/**
 * 業務 Agent・スキル・MCP 接続・実行などの保存先（#839）。`persistent` が false なら再起動で消える。
 * `reason`: `memory_backend` は DB は設定済みで保存先にメモリを明示、`restart_required` は既定（auto）で DB も
 * 設定済みだが起動時は未設定だった（再起動で DB になる）、`database_not_configured` は DB が未設定、
 * `checkpoint_invalid` は既定（auto）で保存済みの checkpoint 全体を読めず、上書きしないようメモリにした（#853）。
 */
export interface RuntimeStorageStatus {
  backend: "memory" | "file" | "oracle_checkpoint" | "oracle_normalized";
  /** 設定の値（`AGENT_RUNTIME_REPOSITORY_BACKEND`。既定は `auto`。#839）。 */
  configured_backend: string;
  persistent: boolean;
  database_configured: boolean;
  reason: "memory_backend" | "restart_required" | "database_not_configured" | "checkpoint_invalid" | null;
  /** 起動時の読み込みで整合しない状態を直した実行の数（#853）。 */
  repaired_runs?: number;
  /** 直せずに読み込まず、保存先に元の JSON のまま残している実行・業務 Agent の数（#853）。 */
  skipped_runs?: number;
  skipped_agents?: number;
}

/**
 * 承認の判断。決定者（`decided_by`）はログイン中の利用者から server が決めるため送らない（#215）。
 */
export interface ApprovalDecisionPayload {
  approved: boolean;
  comment?: string;
  /** 承認して、今後このツールは自動で実行（ツール権限の allow に足す。Agent 管理だけ。#1832）。 */
  always_allow?: boolean;
}

// --- 認証（platform の共通認証。#215） ---

/**
 * `GET /api/auth/me` などが返すログイン中の利用者。`permissions` は implies を展開済み。
 * `allowed_*_ids` が null なら制限なし（SYSTEM_ADMIN・`agent.admin`・ローカル DEBUG）。
 */
export interface CurrentUser extends BaseCurrentUser {
  allowed_agent_ids: string[] | null;
}

/** ロール（共通のロール項目に Agent の権限と対象範囲を足したもの）。 */
export interface SecurityRole {
  role_id: string;
  role_code: string;
  display_name: string;
  description: string;
  is_built_in: boolean;
  archived: boolean;
  version: number;
  permissions: string[];
  agent_ids: string[];
}

/** 権限管理で選べるエージェント（Runtime repository の業務 Agent）。status は enabled / disabled。 */
export interface AgentAccessTarget {
  id: string;
  name: string;
  description: string | null;
  status: string;
}


/** サーバー側でページングする一覧の共通の形（backend の `pr_backend_core.Page[T]`。#1266）。 */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
}

/** 権限管理の対象の候補の 1 ページ（検索とページング。#608）。 */
export type AccessTargetPage<T> = Page<T>;

/** 権限管理画面の保存（`PUT /api/security/roles/{role_id}/access`）。 */
export interface RoleAccessUpdate {
  role_id: string;
  version: number;
  permissions: string[];
  agent_ids: string[];
}

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
  /** backend の内部の文（英語・技術的な原文）。本文には出さず「詳細」の「元のメッセージ」に出す。 */
  rawMessage?: string;
}

export class ApiError extends Error implements ApiErrorPresentable {
  readonly status: number;
  readonly messages: string[];
  readonly errorCode?: string;
  readonly fieldErrors: ApiFieldError[];
  readonly requestId?: string;
  readonly rawMessage?: string;

  constructor(status: number, messages: string[], details: ApiErrorDetails = {}) {
    super(messages[0] ?? `APIエラー (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.messages = messages.length > 0 ? messages : [`APIエラー (${status})`];
    this.errorCode = details.errorCode;
    this.fieldErrors = details.fieldErrors ?? [];
    this.requestId = details.requestId;
    this.rawMessage = details.rawMessage;
  }

  /** 失敗の面の要約と「詳細」（共通の `ApiErrorBanner` / `presentApiError`。#906）。 */
  toApiErrorPresentation(labels: ApiErrorDetailLabels = DEFAULT_API_ERROR_DETAIL_LABELS): ApiErrorPresentation {
    const presentation = httpApiErrorPresentation(this, labels);
    return {
      ...presentation,
      details: [...presentation.details, ...apiErrorDetail(labels.rawMessage, this.rawMessage)],
    };
  }
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

interface ErrorBody {
  detail?: unknown;
  error_messages?: unknown;
  error_code?: unknown;
  /** Control Plane の error code の付いたエラーの補足（`reason` は内部の原文）。 */
  error_details?: { reason?: unknown } | null;
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

/** エラー応答（ApiResponse envelope / FastAPI の detail）から ApiError を作る。 */
async function apiErrorFrom(response: Response): Promise<ApiError> {
  // 本文が無い・JSON でない応答（前段の proxy の 502 / 504 など）は、英語の statusText（`Bad Gateway` 等）を
  // 出さず、ApiError の既定の文にする（#906）。
  let detail = "";
  let body: ErrorBody = {};
  try {
    body = ((await response.json()) ?? {}) as ErrorBody;
    if (Array.isArray(body.error_messages) && typeof body.error_messages[0] === "string") {
      detail = body.error_messages[0];
    } else if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (
      body.detail &&
      typeof body.detail === "object" &&
      "message" in body.detail &&
      typeof body.detail.message === "string"
    ) {
      detail = body.detail.message;
    }
  } catch {
    // Ignore non-JSON error bodies.
  }
  const problemRequestId =
    typeof body.problem?.request_id === "string" ? body.problem.request_id : undefined;
  return new ApiError(response.status, detail ? [detail] : [], {
    errorCode: typeof body.error_code === "string" ? body.error_code : undefined,
    fieldErrors: fieldErrorsOf(body.problem?.field_errors),
    requestId: response.headers.get("X-Request-ID") || problemRequestId,
    rawMessage: typeof body.error_details?.reason === "string" ? body.error_details.reason : undefined,
  });
}

/** 状態を変える method のとき Cookie の CSRF token を `X-CSRF-Token` として付けた headers を作る。 */
function withCsrfHeaders(method: string | undefined, headers: Headers): Headers {
  for (const [name, value] of Object.entries(csrfHeader(method ?? "GET"))) {
    headers.set(name, value);
  }
  return headers;
}


/**
 * Cookie セッションで API を呼ぶ（#215）。状態を変える method には CSRF header を付け、
 * 401 / 403 は共通の認証イベントで通知する。
 */
async function fetchWithSession(path: string, init: RequestInit = {}): Promise<Response> {
  const isFormData = init.body instanceof FormData;
  const headers = new Headers(isFormData ? undefined : { "Content-Type": "application/json" });
  new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  let response: Response;
  try {
    // 配信の基点（`/agent/` など。#1316）はここで付ける。各 API の path は `/api/...` のまま書く。
    response = await fetch(appPath(path), {
      ...init,
      credentials: "same-origin",
      headers: withCsrfHeaders(init.method, headers),
    });
  } catch (cause) {
    throw transportFailure(cause, path, init.method);
  }
  // 403 は error_code が経路の権限拒否のときだけ権限なしの画面へ移す。権限の付与の制限などは
  // 呼び出した画面がその場で理由を表示する（#224）。本文は消費しない。
  if (!response.ok) void notifyAuthResponse(response);
  return response;
}

/**
 * fetch・本文の読み取りが投げた例外のうち、timeout・通信断（`TypeError: Failed to fetch` など）を利用者向けの
 * `ApiTransportError`（日本語の文 + 次の操作。英語の元の文は「詳細」に出す）にする。中止などはそのまま返す（#906）。
 */
function transportFailure(cause: unknown, path: string, method: string | undefined): unknown {
  if (isAbortError(cause)) return cause;
  return toApiTransportError(cause, { method: (method ?? "GET").toUpperCase(), path }) ?? cause;
}

/** ApiResponse を展開し data のみ返す。エラー時は ApiError を投げる。 */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetchWithSession(path, init);
  if (!response.ok) {
    throw await apiErrorFrom(response);
  }
  let json: ApiResponse<T>;
  try {
    json = (await response.json()) as ApiResponse<T>;
  } catch (cause) {
    throw transportFailure(cause, path, init?.method);
  }
  return json.data;
}

/** ファイルの応答（CSV など）を Blob で受け取る。エラー時は ApiError を投げる。 */
async function requestBlob(path: string): Promise<Blob> {
  const response = await fetchWithSession(path);
  if (!response.ok) {
    throw await apiErrorFrom(response);
  }
  try {
    return await response.blob();
  } catch (cause) {
    throw transportFailure(cause, path, "GET");
  }
}

/** ダウンロードの本体と、応答の `Content-Disposition` の名前（無ければ `fallbackName`）。 */
async function requestDownload(path: string, fallbackName: string): Promise<{ blob: Blob; fileName: string }> {
  const response = await fetchWithSession(path);
  if (!response.ok) {
    throw await apiErrorFrom(response);
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const fileName = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName;
  try {
    return { blob: await response.blob(), fileName };
  } catch (cause) {
    throw transportFailure(cause, path, "GET");
  }
}

/** 一覧の query（空の値は送らない）。 */
function listQuery(filters: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

function requestRun(path: string, init?: RequestInit): Promise<RunState> {
  return request<RunState>(path, init);
}

/**
 * Run の処理の経過（共通の span の表の snapshot。#3226 の案 A）。`events` は未検証の入力で、
 * `useTraceProgressSnapshot` が検証する。
 */
export interface RunTraceData {
  target_id: string;
  trace_id: string | null;
  events: unknown;
  /** 表に書けなかった span の数（1 以上なら「一部の処理だけを記録しています。」）。 */
  dropped_spans: number;
}

function auditQuery(filters: ToolCallAuditFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === "") {
      continue;
    }
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export const agentApi = {
  // 組み込み Runtime の状態（SDK の版・既定のモデル・選べるモデル。#754）。
  getRuntimeStatus: () => request<BuiltinRuntimeStatus>("/api/runtime/status"),
  getRuntimeStorage: () => request<RuntimeStorageStatus>("/api/runtime/storage"),
  /** 実行履歴の一覧（サーバー側のページングと絞り込み。行は要約。#1897）。 */
  listRuns: (filters: RunListFilters) => request<RunsPage>(`/api/runs${listQuery(filters)}`),
  /** 承認の一覧（サーバー側のページングと絞り込み。#1897）。 */
  listApprovals: (filters: ApprovalListFilters) => request<ApprovalsPage>(`/api/approvals${listQuery(filters)}`),
  getApproval: (approvalId: string) => request<ApprovalDetail>(`/api/approvals/${encodeURIComponent(approvalId)}`),
  /** 個人の業務 Agent の共有を申請する（持ち主。#2523）。 */
  requestAgentShare: (agentId: string, note: string) =>
    request<AgentProfile>(`/api/agents/${encodeURIComponent(agentId)}/share-request`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
  /** 共有の申請を承認する（Agent 管理。組織の業務 Agent になる。#2523）。 */
  approveAgentShare: (agentId: string) =>
    request<AgentProfile>(`/api/agents/${encodeURIComponent(agentId)}/share-request/approve`, {
      method: "POST",
    }),
  /** 共有の申請を却下する（Agent 管理。理由を持ち主に返す。#2523）。 */
  rejectAgentShare: (agentId: string, reason: string) =>
    request<AgentProfile>(`/api/agents/${encodeURIComponent(agentId)}/share-request/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  publishAgent: (agentId: string, note = "") =>
    request<AgentProfile>(`/api/agents/${encodeURIComponent(agentId)}/publish`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
  /** 公開中の版に固定したスキルと今のスキルの差（#1867）。 */
  getAgentSkillUpdates: (agentId: string) =>
    request<AgentSkillUpdates>(`/api/agents/${encodeURIComponent(agentId)}/skill-updates`),
  /** 選んだ業務 Agent に、スキルの更新だけを取り込んだ新しい版を公開する（#1867）。 */
  publishSkillUpdates: (agentIds: string[], note = "") =>
    request<{ results: SkillUpdatePublishResult[] }>("/api/agent-skill-updates/publish", {
      method: "POST",
      body: JSON.stringify({ agent_ids: agentIds, note }),
    }),
  /** まとめての再公開を取り消す（公開中の版を前の版へ。下書きは変えない。#1867）。 */
  rollbackSkillUpdates: (items: { agent_id: string; version: number }[]) =>
    request<{ agents: AgentProfile[] }>("/api/agent-skill-updates/rollback", {
      method: "POST",
      body: JSON.stringify({ items }),
    }),
  restoreAgentVersion: (agentId: string, version: number) =>
    request<AgentProfile>(
      `/api/agents/${encodeURIComponent(agentId)}/versions/${version}/restore`,
      { method: "POST" },
    ),
  /** 会話の一覧。`draft` は業務 Agent の編集画面で下書きを試した会話（#2515。Agent 管理だけ）。 */
  listThreads: (
    agentId: string | undefined,
    page: { limit: number; offset: number },
    options: { draft?: boolean } = {}
  ) => {
    const params = new URLSearchParams({ limit: String(page.limit), offset: String(page.offset) });
    if (agentId) params.set("agent_id", agentId);
    if (options.draft) params.set("draft", "true");
    return request<ThreadsData>(`/api/threads?${params.toString()}`);
  },
  /** 会話の名前の変更（#1817。作った利用者だけ。前後の空白を除いて 1〜80 文字）。 */
  renameThread: (threadId: string, title: string) =>
    request<ThreadSummary>(`/api/threads/${encodeURIComponent(threadId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  /** 会話の削除（#1817。Run は残し、会話からだけ外す。回答の作成中・承認待ちは 409）。 */
  deleteThread: (threadId: string) =>
    request<null>(`/api/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" }),
  /** チャットの回答への評価（#774）。会話をした利用者だけ。付け直すと上書きする。 */
  putRunFeedback: (runId: string, payload: RunFeedbackPayload) =>
    requestRun(`/api/runs/${encodeURIComponent(runId)}/feedback`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  /** 管理者の評価（#774。Agent 管理の権限）。 */
  putRunAdminReview: (runId: string, payload: RunFeedbackPayload) =>
    requestRun(`/api/runs/${encodeURIComponent(runId)}/admin-review`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  /** フィードバックの集計と一覧（#774）。 */
  getFeedbackReport: (filters: FeedbackFilters) => {
    const query = new URLSearchParams({ days: String(filters.days) });
    if (filters.agentId) query.set("agent_id", filters.agentId);
    if (filters.rating) query.set("rating", filters.rating);
    if (filters.reason) query.set("reason", filters.reason);
    if (filters.offset) query.set("offset", String(filters.offset));
    if (filters.limit) query.set("limit", String(filters.limit));
    return request<FeedbackReport>(`/api/feedback?${query.toString()}`);
  },
  /** 確かめるべき実行（暗黙のシグナル。#1822）。 */
  getFeedbackSignals: (filters: SignalFilters) => {
    const query = new URLSearchParams({ days: String(filters.days) });
    if (filters.agentId) query.set("agent_id", filters.agentId);
    if (filters.kind) query.set("kind", filters.kind);
    if (filters.offset) query.set("offset", String(filters.offset));
    if (filters.limit) query.set("limit", String(filters.limit));
    return request<SignalReport>(`/api/feedback/signals?${query.toString()}`);
  },
  /** 会話（Run の一覧）。チャットは取り直しの `signal`（中止・待ち時間の上限）を渡す（#1160）。 */
  getThread: (threadId: string, options: { signal?: AbortSignal } = {}) =>
    request<ThreadData>(`/api/threads/${encodeURIComponent(threadId)}`, { signal: options.signal }),
  /** チャットにファイルを添付する（会話があれば結び付ける。#2517）。 */
  uploadChatAttachment: (file: File, threadId?: string | null) => {
    const body = new FormData();
    body.append("file", file);
    if (threadId) body.append("conversation_id", threadId);
    return request<ChatAttachmentApiItem>("/api/chat-attachments", { method: "POST", body });
  },
  /** 会話の添付（無ければ送る前の添付）と上限。 */
  listChatAttachments: (threadId?: string | null) =>
    request<ChatAttachmentListData>(
      threadId ? `/api/chat-attachments?conversation_id=${encodeURIComponent(threadId)}` : "/api/chat-attachments"
    ),
  deleteChatAttachment: (attachmentId: string) =>
    request<{ deleted: boolean }>(`/api/chat-attachments/${encodeURIComponent(attachmentId)}`, { method: "DELETE" }),
  createRun: (payload: CreateRunPayload) =>
    requestRun("/api/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getRun: (runId: string) => requestRun(`/api/runs/${runId}`),
  /** Run の処理の経過（共通の span の表の snapshot。実行中は画面が読み直す。#3226 の案 A）。 */
  getRunTrace: (runId: string, options: { signal?: AbortSignal } = {}) =>
    request<RunTraceData>(`/api/runs/${encodeURIComponent(runId)}/trace`, { signal: options.signal }),
  /** Run の RAG の根拠の原文のプレビュー（#2389。画面を見ている利用者として RAG が作る）。 */
  getRunSourcePreview: (runId: string, documentId: string, chunkId: string) =>
    request<RunSourcePreview>(
      `/api/runs/${encodeURIComponent(runId)}/source-preview?${new URLSearchParams({
        document_id: documentId,
        chunk_id: chunkId,
      }).toString()}`
    ),
  getRunAudit: (runId: string) =>
    request<RunAuditData>(`/api/runs/${runId}/audit`),
  listAgentTemplates: () => request<{ templates: AgentTemplate[] }>("/api/agent-templates"),
  /** 業務の説明から下書きを提案する（#2519。Agent 管理だけ。保存はしない）。 */
  suggestAgentDraft: (description: string) =>
    request<AgentDraftSuggestion>("/api/agents/draft-suggestion", {
      method: "POST",
      body: JSON.stringify({ description }),
    }),
  /** 品質評価（#776）。 */
  listEvaluationSets: (agentId?: string) =>
    request<{ sets: EvaluationSetItem[] }>(
      `/api/evaluation-sets${agentId ? `?${new URLSearchParams({ agent_id: agentId }).toString()}` : ""}`
    ),
  getEvaluationSet: (setId: string) => request<EvaluationSet>(`/api/evaluation-sets/${encodeURIComponent(setId)}`),
  createEvaluationSet: (payload: EvaluationSetInput) =>
    request<EvaluationSet>("/api/evaluation-sets", { method: "POST", body: JSON.stringify(payload) }),
  updateEvaluationSet: (setId: string, payload: EvaluationSetInput) =>
    request<EvaluationSet>(`/api/evaluation-sets/${encodeURIComponent(setId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  deleteEvaluationSet: (setId: string) =>
    request<null>(`/api/evaluation-sets/${encodeURIComponent(setId)}`, { method: "DELETE" }),
  /** Excel の評価ケースを読む（保存しない）。 */
  parseEvaluationCasesXlsx: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<{ cases: Required<EvaluationCase>[] }>("/api/evaluation-sets/parse-xlsx", { method: "POST", body });
  },
  downloadEvaluationTemplate: () => requestBlob("/api/evaluation-sets/template.xlsx"),
  downloadEvaluationSetXlsx: (setId: string) =>
    requestBlob(`/api/evaluation-sets/${encodeURIComponent(setId)}/cases.xlsx`),
  /** Run（フィードバック・Run の詳細）から評価ケースの下書きを作る（#810）。 */
  getRunEvaluationCase: (runId: string) =>
    request<EvaluationCaseDraft>(`/api/runs/${encodeURIComponent(runId)}/evaluation-case`),
  /** 評価セットにケースを 1 件足す（同じ質問・上限は 409。#810）。 */
  appendEvaluationCase: (setId: string, payload: EvaluationCase) =>
    request<EvaluationSet>(`/api/evaluation-sets/${encodeURIComponent(setId)}/cases`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  /** 業務 Agent の作成に使った業種テンプレートの評価ケースで評価セットを作る（#810）。 */
  createEvaluationSetFromTemplate: (agentId: string) =>
    request<EvaluationSet>("/api/evaluation-sets/from-template", {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId }),
    }),
  /** 評価を始める（`agent_id` を省くと評価セットの業務 Agent。#1845）。 */
  createEvaluation: (payload: { set_id: string; agent_version?: EvaluationTarget; agent_id?: string; trials?: number }) =>
    request<EvaluationJob>("/api/evaluations", { method: "POST", body: JSON.stringify(payload) }),
  /** 評価の履歴（新しい順のページ。終わった評価は削除せずに残る。#794・#3235）。 */
  listEvaluations: (page: { offset: number; limit: number }) =>
    request<EvaluationJobsPage>(
      `/api/evaluations?${new URLSearchParams({ offset: String(page.offset), limit: String(page.limit) }).toString()}`
    ),
  /** 2 つの評価をケースごとに並べて比べる（業務 Agent をまたいでよい。#1845）。 */
  compareEvaluations: (baseJobId: string, targetJobId: string) =>
    request<EvaluationComparison>(
      `/api/evaluations/compare?${new URLSearchParams({ base_job_id: baseJobId, target_job_id: targetJobId }).toString()}`
    ),
  getEvaluation: (jobId: string) => request<EvaluationJob>(`/api/evaluations/${encodeURIComponent(jobId)}`),
  cancelEvaluation: (jobId: string) =>
    request<EvaluationJob>(`/api/evaluations/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),
  deleteEvaluation: (jobId: string) =>
    request<null>(`/api/evaluations/${encodeURIComponent(jobId)}`, { method: "DELETE" }),
  /** 利用状況（#772）。日は画面のブラウザのタイムゾーンで区切る。 */
  getUsageReport: (days: UsagePeriodDays, timezone: string) =>
    request<UsageReport>(`/api/usage?${new URLSearchParams({ days: String(days), timezone }).toString()}`),
  listToolCallAudit: (filters: ToolCallAuditFilters) =>
    request<ToolCallAuditData>(`/api/audit/tool-calls${auditQuery(filters)}`),
  /** 監査 CSV。Cookie セッションで取得し、401 / 403 は他の API と同じく通知する（#215）。 */
  downloadToolCallAuditCsv: (filters: ToolCallAuditFilters) =>
    requestBlob(`/api/audit/tool-calls.csv${auditQuery(filters)}`),
  listRunArtifacts: (runId: string) =>
    request<{ artifacts: Artifact[] }>(`/api/runs/${runId}/artifacts`),
  getRunArtifact: (runId: string, artifactId: string) =>
    request<Artifact>(`/api/runs/${runId}/artifacts/${artifactId}`),
  cancelRun: (runId: string) =>
    requestRun(`/api/runs/${runId}/cancel`, { method: "POST" }),
  resumeRun: (runId: string) =>
    requestRun(`/api/runs/${runId}/resume`, { method: "POST" }),
  replayRun: (runId: string) =>
    requestRun(`/api/runs/${runId}/replay`, { method: "POST" }),
  /** 回答前の確認（#1839）に答える・確認せずに回答する。 */
  decideConfirmation: (runId: string, confirmationId: string, payload: ConfirmationDecisionPayload) =>
    request<ConfirmationOutcome>(`/api/runs/${runId}/confirmations/${confirmationId}`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  decideApproval: (approvalId: string, payload: ApprovalDecisionPayload) =>
    requestRun(`/api/approvals/${approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  listTools: () => request<{ tools: ToolDefinition[] }>("/api/tools"),
  // `user_display_names` は版の公開者（ログイン ID）→ 表示名（#1696）。
  // `max_published_versions` は版の履歴に残す版の数（AGENT_MAX_PUBLISHED_VERSIONS。#2356）。
  // `owner_display_names` は個人の業務 Agent の持ち主（user_uuid）→ 表示名（#2523）。
  listAgents: () =>
    request<{
      agents: AgentProfile[];
      share_approvals?: Array<{ agent_id: string; name: string; decided_at: string | null }>;
      user_display_names?: Record<string, string>;
      max_published_versions?: number;
      owner_display_names?: Record<string, string>;
    }>("/api/agents"),
  createAgent: (payload: AgentProfileWritePayload) =>
    request<AgentProfile>("/api/agents", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  patchAgent: (agentId: string, payload: AgentProfilePatchPayload) =>
    request<AgentProfile>(`/api/agents/${agentId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  /** データの範囲に選べるプロファイル（#1378。編集者として RAG / NL2SQL に問い合わせる）。 */
  listDataScopeCandidates: (connection: AgentDataScopeConnection) =>
    request<DataScopeCandidates>(`/api/agent-data-scopes/${encodeURIComponent(connection)}/candidates`),
  getTracePolicySettings: () =>
    request<TracePolicySettings>("/api/settings/trace-policy"),
  patchTracePolicySettings: (payload: Partial<TracePolicySettings>) =>
    request<TracePolicySettings>("/api/settings/trace-policy", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  flushTraceExportRetryQueue: (limit = 100, force = false) =>
    request<TraceExportRetryData>(
      `/api/observability/export-retry/flush?limit=${limit}&force=${force}`,
      {
        method: "POST",
      },
    ),
  getToolPolicySettings: () =>
    request<ToolPolicySettings>("/api/settings/tool-policy"),
  listToolPolicyTools: () => request<ToolPolicyToolsData>("/api/settings/tool-policy/tools"),
  patchToolPolicySettings: (payload: Partial<ToolPolicySettings>) =>
    request<ToolPolicySettings>("/api/settings/tool-policy", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  listMcpConnections: () =>
    request<McpConnectionsData>("/api/settings/mcp-connections"),
  createMcpConnection: (payload: McpConnectionWritePayload) =>
    request<McpConnectionSettings>("/api/settings/mcp-connections", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateMcpConnection: (serverId: string, payload: McpConnectionWritePayload) =>
    request<McpConnectionSettings>(
      `/api/settings/mcp-connections/${encodeURIComponent(serverId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  deleteMcpConnection: (serverId: string) =>
    request<McpConnectionsData>(
      `/api/settings/mcp-connections/${encodeURIComponent(serverId)}`,
      { method: "DELETE" },
    ),
  listMcpConnectionTools: (serverId: string) =>
    request<ExternalMcpToolsData>(
      `/api/settings/mcp-connections/${encodeURIComponent(serverId)}/tools`,
    ),
  listSkills: () => request<AgentSkillListData>("/api/skills"),
  getSkill: (skillId: string) =>
    request<AgentSkill>(`/api/skills/${encodeURIComponent(skillId)}`),
  createSkill: (payload: AgentSkillWritePayload) =>
    request<AgentSkill>("/api/skills", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateSkill: (skillId: string, payload: AgentSkillWritePayload) =>
    request<AgentSkill>(`/api/skills/${encodeURIComponent(skillId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  /** Agent Skills の形（`<name>/SKILL.md` ＋ `references/`）の zip（#1874）。 */
  exportSkill: (skillId: string) =>
    requestDownload(`/api/skills/${encodeURIComponent(skillId)}/export`, "skill.zip"),
  deleteSkill: (skillId: string) =>
    request<AgentSkillListData>(`/api/skills/${encodeURIComponent(skillId)}`, {
      method: "DELETE",
    }),
  reloadSkills: () =>
    request<AgentSkillListData>("/api/skills/reload", { method: "POST" }),
};

/**
 * 共通のシステム設定の取得の options。共有の画面は TanStack Query の `signal` を渡し、
 * 画面を離れたら取得を止める（#1117）。
 */
export interface SettingsRequestOptions {
  signal?: AbortSignal;
}

export const api = {
  // DB の状態（画面の DB ゲートが使う。3製品共通の判定と契約。ログイン不要。#325）。
  getDatabaseStatus: (options?: { signal?: AbortSignal }) =>
    request<DatabaseStatusData>("/api/ready/database", { signal: options?.signal }),

  // システムテーブル（運用設定。3製品共通のカードと契約。#751）。
  getSystemTablesStatus: (options?: { signal?: AbortSignal }) =>
    request<SystemTablesStatusData>("/api/settings/database/system-tables", { signal: options?.signal }),
  initializeSystemTables: (body: SystemTablesInitializeRequest) =>
    request<SystemTablesOperationData>("/api/settings/database/system-tables/initialize", jsonBody(body)),

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

  // チャットの添付（設定は製品ごと。#2517）
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

  // 回答の注意書き（設定は製品ごと。#2405・#2449）
  getAnswerNoticeSettings: (options: SettingsRequestOptions = {}) =>
    request<AnswerNoticeSettingsData>("/api/settings/answer-notice", { signal: options.signal }),
  updateAnswerNoticeSettings: (body: AnswerNoticeSettingsUpdate) =>
    request<AnswerNoticeSettingsData>("/api/settings/answer-notice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

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
};
