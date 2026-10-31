// NL2SQL の処理の経過の工程の定義（#1145 / #1359 / #3226）。
//
// 処理の経過は実行の trace（span の木）の投影（#3226 の案 A）。span は共通の span の表にあり（開始待ち・準備・生成・
// 安全性の確認・実行・結果の整形と、その中のモデルの呼び出しなど）、画面は記録ごとの API（`.../trace`。span の表の
// snapshot）を共有の `useTraceProgressSnapshot` で読んで木にする。ここは工程の span（`kind`）の名前（i18n）と
// 補足（生成方法・参照した表・行数）を付けるだけにする。状態と時刻は span の実測。
import {
  chatProgressLabelState,
  createTraceDetailsLabels,
  type ChatProgressLabels,
  type ChatProgressLabelState,
  type ChatProgressParams,
  type ChatProgressStep,
  type ChatProgressStepDefinitions,
  type ChatProgressStepStatus,
  type TraceDetailsLabels,
} from "@production-ready/ui";

// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import { t } from "../../lib/i18n";
import type { JobData, Nl2SqlEngine } from "./types";

type ChatStage =
  | "queue"
  | "prepare_context"
  | "generate_sql"
  | "safety_check"
  | "execute_sql"
  | "format_results";

/** 段階の名前（実行中・完了・失敗・未実行）。i18n の key は静的に書く（辞書の検査が key を見つけられるように）。 */
const STAGE_LABELS: Record<ChatStage, Record<ChatProgressLabelState, string>> = {
  queue: {
    running: t("chat.progress.queue.running"),
    done: t("chat.progress.queue.done"),
    failed: t("chat.progress.queue.failed"),
    idle: t("chat.progress.queue.idle"),
  },
  prepare_context: {
    running: t("chat.progress.prepare_context.running"),
    done: t("chat.progress.prepare_context.done"),
    failed: t("chat.progress.prepare_context.failed"),
    idle: t("chat.progress.prepare_context.idle"),
  },
  generate_sql: {
    running: t("chat.progress.generate_sql.running"),
    done: t("chat.progress.generate_sql.done"),
    failed: t("chat.progress.generate_sql.failed"),
    idle: t("chat.progress.generate_sql.idle"),
  },
  safety_check: {
    running: t("chat.progress.safety_check.running"),
    done: t("chat.progress.safety_check.done"),
    failed: t("chat.progress.safety_check.failed"),
    idle: t("chat.progress.safety_check.idle"),
  },
  execute_sql: {
    running: t("chat.progress.execute_sql.running"),
    done: t("chat.progress.execute_sql.done"),
    failed: t("chat.progress.execute_sql.failed"),
    idle: t("chat.progress.execute_sql.idle"),
  },
  format_results: {
    running: t("chat.progress.format_results.running"),
    done: t("chat.progress.format_results.done"),
    failed: t("chat.progress.format_results.failed"),
    idle: t("chat.progress.format_results.idle"),
  },
};

const ENGINE_LABELS: Record<Nl2SqlEngine, string> = {
  select_ai: t("chat.engine.selectAi"),
  select_ai_agent: t("chat.engine.selectAiAgent"),
  enterprise_ai_direct: t("chat.engine.enterprise"),
};

/** 共通の部品の文言のうち、NL2SQL の語にそろえるもの（スキップは SQL 生成の画面と同じ「未実行」）。 */
export const CHAT_PROGRESS_LABELS: Partial<ChatProgressLabels> = {
  working: t("chat.progress.working"),
  summary: (count, duration) => t("chat.progress.summary", { count, duration }),
  steps: t("chat.progress.steps"),
  status: {
    pending: t("nl2sql.progress.step.pending"),
    running: t("nl2sql.progress.step.running"),
    done: t("nl2sql.progress.step.done"),
    failed: t("chat.progress.status.failed"),
    skipped: t("nl2sql.progress.step.skipped"),
  },
};

/**
 * 段の名前（#3219）。工程の定義の無い span でも属性 `pr.stage` があれば、処理の経過の行と同じ文言を使う
 * （停止・未実行は未実行の形）。知らない段は undefined。
 */
export function chatStageName(stage: string, status: ChatProgressStepStatus | "cancelled"): string | undefined {
  if (!Object.prototype.hasOwnProperty.call(STAGE_LABELS, stage)) return undefined;
  return label(stage as ChatStage)(status === "cancelled" ? "skipped" : status);
}

/** NL2SQL の trace の文言（root の `TraceDetailsLabelsProvider` で全画面に渡す。段の名前は処理の経過と同じ）。 */
export const NL2SQL_TRACE_DETAILS_LABELS: TraceDetailsLabels = createTraceDetailsLabels(t, { stageName: chatStageName, product: "nl2sql" });

function label(stage: ChatStage) {
  return (status: ChatProgressStepStatus) => STAGE_LABELS[stage][chatProgressLabelState(status)];
}

function count(value: ChatProgressParams[string] | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/** 生成方法の名前（未知の値は出さない）。 */
function engineDetail(params: ChatProgressParams): string | undefined {
  const engine = params.engine;
  return typeof engine === "string" && Object.prototype.hasOwnProperty.call(ENGINE_LABELS, engine)
    ? ENGINE_LABELS[engine as Nl2SqlEngine]
    : undefined;
}

/** 参照した表を短く（backend が渡す最初の数件と「ほか N 件」）。 */
function tablesDetail(params: ChatProgressParams): string | undefined {
  const tables = typeof params.tables === "string" ? params.tables : "";
  if (!tables) return undefined;
  const shown = tables.split(",").filter((name) => name.trim()).length;
  const total = count(params.table_count) ?? shown;
  return total > shown ? t("chat.progress.tablesMore", { tables, count: total - shown }) : tables;
}

/** 実行で取得した行数。 */
function rowsDetail(params: ChatProgressParams): string | undefined {
  const rows = count(params.rows);
  return rows === null ? undefined : t("chat.progress.rows", { count: rows.toLocaleString("ja-JP") });
}

/**
 * DB 管理チャットの SQL の確認（サーバーの分類）の補足（#2264）。1 文なら実行できない・確認語・読み取り専用、
 * 2 文以上なら文の分類ごとの数（「読み取り専用 1・実行できない 1」）。
 */
function dbAdminCheckDetail(params: ChatProgressParams): string | undefined {
  const total = count(params.statements);
  if (!total) return undefined;
  if (total === 1) {
    if ((count(params.blocked) ?? 0) > 0) return t("dbAdminChat.check.blocked");
    const target = typeof params.confirmation_target === "string" ? params.confirmation_target : "";
    if (target) return t("dbAdminChat.check.confirmation", { target });
    return t("dbAdminChat.check.readOnly");
  }
  const kinds = [
    ["readOnly", count(params.read_only) ?? 0],
    ["change", count(params.change) ?? 0],
    ["blocked", count(params.blocked) ?? 0],
  ] as const;
  return kinds
    .filter(([, value]) => value > 0)
    .map(([kind, value]) => t("dbAdminChat.check.count", { kind: t(`dbAdminChat.kind.${kind}`), count: value }))
    .join("・");
}

/**
 * 工程の span の定義（`kind` = backend の工程の id → 名前と補足）。並びと入れ子は span の木（始まった順・親子）。
 *
 * - 開始待ち（ジョブの作成から worker が処理を始めるまで）: worker が起動していない・混んでいるときに、生成では
 *   なく開始を待っていることが分かる。
 * - 生成の段階の補足は生成方法、安全性の確認（完了）は参照した表、実行（完了）は取得した行数。
 *   Select AI / Select AI Agent の SQL で省略した安全性の確認（未実行）は省略の理由。
 * - DB 管理チャットの SQL の確認（`db_admin_check`）は安全性の確認と同じ名前で、補足は文の分類ごとの数。
 */
export const CHAT_PROGRESS_STEP_DEFINITIONS: ChatProgressStepDefinitions = {
  queue: { label: label("queue") },
  prepare_context: { label: label("prepare_context") },
  generate_sql: {
    label: label("generate_sql"),
    detail: (_status, params) => engineDetail(params),
  },
  safety_check: {
    label: label("safety_check"),
    detail: (status, params) =>
      status === "done"
        ? tablesDetail(params)
        : // Select AI / Select AI Agent の SQL で省略した（#1523）。
          status === "skipped" && params.safety_check_skipped === true
          ? t("chat.progress.safety_check.safetyCheckSkipped")
          : undefined,
  },
  db_admin_check: {
    label: label("safety_check"),
    detail: (_status, params) => dbAdminCheckDetail(params),
  },
  execute_sql: {
    label: label("execute_sql"),
    detail: (status, params) => (status === "done" ? rowsDetail(params) : undefined),
  },
  format_results: { label: label("format_results") },
};

function isoFromMs(ms: number): string {
  return new Date(ms).toISOString();
}

/** 送信の応答（ジョブの投入）を待つ間の段階。投入が遅いときに、どこで待っているかを示す。 */
export function chatSubmitProgressSteps(sentAtMs: number): ChatProgressStep[] {
  return [
    {
      id: "submit",
      label: t("chat.progress.submit.running"),
      status: "running",
      startedAt: isoFromMs(sentAtMs),
    },
  ];
}

/**
 * 受け付けたジョブを worker が受け取るまでの仮の行（#3226）。開始待ちは worker が受け取ったときに終わった span として
 * 記録されるので、それまでは span が無い。最初の span が届いたら span の木に置き換わる。
 */
export function chatQueueProgressSteps(createdAt: string | null | undefined): ChatProgressStep[] {
  const step: ChatProgressStep = { id: "queue", label: label("queue")("running"), status: "running" };
  if (createdAt) step.startedAt = createdAt;
  return [step];
}

/** ジョブの処理の経過の API（#3226 の案 A。span の表の snapshot）。 */
export const jobTracePath = (jobId: string) => `/api/nl2sql/jobs/${encodeURIComponent(jobId)}/trace`;

/** 実行履歴の行の処理の経過の API。 */
export const historyTracePath = (historyId: string) =>
  `/api/nl2sql/history/${encodeURIComponent(historyId)}/trace`;

/** DB 管理チャットのターンの処理の経過の API。 */
export const dbAdminChatTurnTracePath = (conversationId: string, turnId: string) =>
  `/api/nl2sql/db-admin-chat/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/trace`;

/** 処理の経過の API の応答（未検証の入力）。イベントは共有の検証（`parseChatProgressEvents`）に渡す。 */
export interface TraceProgressResponse {
  events: unknown;
  /** 書けなかった span の数（1 以上なら「一部の処理だけを記録しています」）。 */
  droppedSpans: number;
}

export function parseTraceProgressResponse(value: unknown): TraceProgressResponse {
  const source = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const dropped = source.dropped_spans;
  return {
    events: Array.isArray(source.events) ? source.events : [],
    droppedSpans: typeof dropped === "number" && Number.isInteger(dropped) && dropped > 0 ? dropped : 0,
  };
}

/** 完了後の全体の所要時間（ジョブの作成から終了まで）。段階の時刻が無い古いジョブにも出す。 */
export function chatJobElapsedMs(job: JobData): number | null {
  if (job.status !== "done" && job.status !== "error") return null;
  const start = Date.parse(job.created_at);
  const end = job.finished_at ? Date.parse(job.finished_at) : Number.NaN;
  if (Number.isFinite(start) && Number.isFinite(end)) return Math.max(0, end - start);
  return job.elapsed_ms ?? null;
}
