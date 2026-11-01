import {
  chatProgressHasSpans,
  chatProgressStepsFromEvents,
  chatProgressTimelineStep,
  runStepDetailsJson,
  type ChatProgressEvent,
  type ChatProgressLabelOptions,
  type ChatProgressStep,
  type ChatProgressStepDefinitions,
  type RunStepDetailCode,
  type RunStepDetailField,
  type RunStepDetails,
  type RunExecutorFilter as CommonRunExecutorFilter,
  type RunCancellation,
  type RunDetailLabelsInput,
  type RunHistoryLabelsInput,
  type RunStatus as CommonRunStatus,
  type RunStatusFilter as CommonRunStatusFilter,
  type RunTimelineStep,
} from "@production-ready/ui";

import type { RunExecutorFilter, RunListKind, RunListKindQuery, RunListStatus, RunState, RunStep } from "@/lib/api";
import { AGENT_CHAT_PROGRESS_STEPS } from "@/lib/chat-progress";
import { dataScopeNotes } from "@/lib/data-scope";
import { t } from "@/lib/i18n";
import { runSourceOf } from "@/lib/run-source";
import { APP_ROUTES } from "@/lib/routes";
import { runStatusView } from "@/lib/status-labels";

/*
 * Agent の実行履歴を共通の実行履歴の型（UX 契約 run-history.md。#1955）に写す。
 * 状態は共通の 7 つ（`RunStatus`）、文言は Agent に固有の言い方（承認待ち・確認待ち）を `statusLabel` で上書きする。
 */

/** Agent の Run の状態 → 共通の状態（run-history.md「状態の写し方」）。 */
export function commonRunStatus(status: RunState["status"]): CommonRunStatus {
  switch (status) {
    case "queued":
      return "queued";
    case "running":
      return "running";
    case "waiting_approval":
    case "waiting_confirmation":
      return "waiting";
    case "completed":
      return "succeeded";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
  }
}

/**
 * 状態の文言の上書き（Agent に固有の言い方だけ。waiting_approval →「承認待ち」、waiting_confirmation →「確認待ち」）。
 * それ以外は共通の文言（待機中・実行中・完了・失敗・停止）のまま（3 製品で同じ語。run-history.md「状態の写し方」）。
 */
export function runStatusLabel(status: RunState["status"]): string | undefined {
  return status === "waiting_approval" || status === "waiting_confirmation" ? runStatusView(status).label : undefined;
}

/** 一覧の状態の絞り込みに出す共通の状態（Agent が写せるものだけ。ブロックは無い）。 */
export const AGENT_RUN_STATUS_OPTIONS: readonly CommonRunStatus[] = [
  "queued",
  "running",
  "waiting",
  "succeeded",
  "failed",
  "cancelled",
];

/** 共通の状態の絞り込み → backend の `status`（`waiting` は承認待ち・確認待ち）。 */
export function backendRunStatus(filter: CommonRunStatusFilter): RunListStatus | undefined {
  switch (filter) {
    case "all":
      return undefined;
    case "succeeded":
      return "completed";
    case "blocked":
      // Agent には「ブロック」が無い（選択肢にも出さない）。
      return undefined;
    default:
      return filter;
  }
}

/** 共通の実行者の絞り込み（self）→ backend の値（mine）。 */
export function backendRunExecutor(filter: CommonRunExecutorFilter): Exclude<RunExecutorFilter, "all"> | undefined {
  if (filter === "all") return undefined;
  return filter === "self" ? "mine" : "others";
}

/** 種類（きっかけ）の絞り込みの値（一つずつ選ぶもの）。 */
export const AGENT_RUN_KINDS: readonly RunListKind[] = ["user", "mcp", "replay", "evaluation"];

/**
 * 種類の絞り込みの既定。品質評価の Run（評価のケースごとの Run）は利用者の実行に混ざると埋もれさせるので、
 * 既定では隠し、「すべて」か「品質評価」を選んだときに出す（#1983）。
 */
export const DEFAULT_AGENT_RUN_KIND = "not_evaluation";

export type AgentRunKindFilter = "all" | typeof DEFAULT_AGENT_RUN_KIND | RunListKind;

/** 画面の種類の絞り込み → `GET /api/runs` の `kind`（「すべて」は送らない）。 */
export function backendRunKind(filter: AgentRunKindFilter): RunListKindQuery | undefined {
  return filter === "all" ? undefined : filter;
}

/** 一覧・詳細で使う共通の部品の文言（Agent の i18n）。 */
export function agentRunHistoryLabels(): RunHistoryLabelsInput {
  return {
    table: t("run.history"),
    columns: {
      title: t("run.column.question"),
      status: t("common.status"),
      kind: t("run.column.kind"),
      executor: t("run.column.executor"),
      startedAt: t("run.column.startedAt"),
      duration: t("run.column.duration"),
      usage: t("run.column.usage"),
      actions: t("run.actions"),
    },
    self: t("run.self"),
    openDetail: (title) => t("run.openDetail", { title }),
    rowActions: (title) => t("common.entityActions", { name: title }),
  };
}

/** 詳細の共通の部品（RunSummary・RunTimeline）の文言（Agent の i18n）。 */
export function agentRunDetailLabels(): RunDetailLabelsInput {
  return {
    summary: t("run.section.summary"),
    question: t("run.section.question"),
    answer: t("run.section.answer"),
    timeline: t("run.section.timeline"),
    processDetails: t("run.section.processDetails"),
    facts: {
      startedAt: t("run.fact.startedAt"),
      finishedAt: t("run.fact.finishedAt"),
      duration: t("run.fact.duration"),
      kind: t("run.fact.kind"),
      executor: t("run.executor"),
      owner: t("run.conversationOwner"),
      model: t("run.fact.model"),
      tokens: t("run.fact.tokens"),
      cost: t("run.fact.cost"),
      runId: t("run.fact.runId"),
      cancellation: t("run.fact.cancellation"),
    },
    self: t("run.self"),
    cancellation: {
      screen: t("run.cancellation.screen"),
      mcp: t("run.cancellation.mcp"),
      mcpKey: (name) => t("run.cancellation.mcpKey", { name }),
      system: t("run.cancellation.system"),
    },
    timelineSummary: (count, duration) => t("run.timelineSummary", { count, duration }),
    stepStatus: {
      pending: t("run.stepStatus.pending"),
      running: t("run.stepStatus.running"),
      waiting: t("run.stepStatus.waiting"),
      done: t("run.stepStatus.done"),
      failed: t("run.stepStatus.failed"),
      skipped: t("run.stepStatus.skipped"),
    },
  };
}

/** 終わった Run の終了日時（終わりの Event の時刻。backend の `run_finished_at` と同じ判定）。 */
export function runFinishedAt(run: RunState): string | null {
  if (!["completed", "failed", "cancelled"].includes(run.status)) return null;
  const terminal = [...run.events]
    .reverse()
    .find((event) => event.type === "run.completed" || event.type === "runtime.failed" || event.type === "run.cancelled");
  return terminal?.created_at ?? run.updated_at;
}

export function runDurationMs(run: RunState): number | null {
  const finished = runFinishedAt(run);
  if (!finished) return null;
  const ms = Date.parse(finished) - Date.parse(run.created_at);
  return Number.isFinite(ms) ? Math.max(0, ms) : null;
}

/** Run に渡したツールの表示名（MCP の `title`。backend の metadata `tool_titles`）。 */
export function runToolTitles(run: Pick<RunState, "metadata">): Record<string, string> {
  const value = run.metadata?.tool_titles;
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string" && entry[1] !== "")
  );
}

/** ツールの span の params の、Run の step の id の key（backend の `RUN_STEP_PARAM`。#3226）。 */

export const RUN_STEP_PARAM = "run_step";

const TERMINAL_RUN_STATUSES: ReadonlySet<RunState["status"]> = new Set(["completed", "failed", "cancelled"]);

/** 結果不明の外部への書き込み（#72。backend の `external_writes.unknown_steps`）。 */
export interface OutcomeUnknownWrite {
  stepId: string;
  toolName: string;
  operationKey: string;
}

/**
 * 結果が不明な外部への書き込みの step（届いた後の失敗・中断。backend の `external_writes.unknown_steps` と同じ判定）。
 * 終わった Run の実行中の書き込み（取消の後に結果が届かないまま止まった）も含める。
 */
export function runOutcomeUnknownWrites(run: Pick<RunState, "status" | "steps">): OutcomeUnknownWrite[] {
  const ended = TERMINAL_RUN_STATUSES.has(run.status);
  return run.steps.flatMap((step) => {
    const key = step.tool_call?.operation_key;
    if (!step.tool_call || !key) return [];
    const unknown =
      (step.tool_result?.success === false && step.tool_result.error_details?.outcome === "unknown") ||
      (ended && step.status === "running");
    return unknown ? [{ stepId: step.id, toolName: step.tool_call.name, operationKey: key }] : [];
  });
}

/**
 * 処理の経過の行（span の id）と、ツールの span が結んだ Run の step（引数・結果・データの範囲）。backend はツールの
 * span の params に Run の step の id を入れる（#3226）。チャットと実行の詳細で同じ対応にする（#2294）。
 */
export function progressRunSteps(events: readonly ChatProgressEvent[], steps: readonly RunStep[]): Map<string, RunStep> {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const pairs = new Map<string, RunStep>();
  for (const event of events) {
    if (event.type !== "step") continue;
    const id = event.params?.[RUN_STEP_PARAM];
    const step = typeof id === "string" ? byId.get(id) : undefined;
    if (step) pairs.set(event.step_id, step);
  }
  return pairs;
}

/**
 * ツールの段の詳細（ツールの内部名・データの範囲の記録・エラー・引数と結果の JSON。#2294）。チャットの処理の経過で
 * 段を開いて見る。段の名前は表示名（MCP の `title`）のことがあるので、調べる用に内部名を先頭に出す（#2324）。
 * 長い JSON は先頭だけにして省略の印を付ける。ツールの呼び出しでなければ undefined（段に開閉のボタンを付けない）。
 */
export function toolStepDetails(step: RunStep): RunStepDetails | undefined {
  const lines = dataScopeNotes(step.tool_call?.data_scope);
  const error = step.tool_result?.error;
  const args = step.tool_call?.arguments;
  const output = step.tool_result?.output;
  const code: RunStepDetailCode[] = [];
  let truncated = false;
  if (args && Object.keys(args).length > 0) {
    const json = runStepDetailsJson(args);
    truncated ||= json.truncated;
    code.push({ label: t("chat.progress.details.arguments"), language: "json", text: json.text });
  }
  if (output != null) {
    const json = runStepDetailsJson(output);
    truncated ||= json.truncated;
    code.push({ label: t("chat.progress.details.result"), language: "json", text: json.text });
  }
  const name = step.tool_call?.name;
  if (!name && !lines.length && !error && !code.length) return undefined;
  const details: RunStepDetails = {};
  const fields: RunStepDetailField[] = [];
  if (name) fields.push({ label: t("chat.progress.details.tool"), value: name, mono: true });
  if (error) fields.push({ label: t("chat.progress.details.error"), value: error });
  if (fields.length) details.fields = fields;
  if (lines.length) details.lines = lines;
  if (code.length) details.code = code;
  if (truncated) details.truncated = true;
  return details;
}

/**
 * Agent の段の定義（`AGENT_CHAT_PROGRESS_STEPS`）に、ツールの span と結んだ Run の step の詳細（データの範囲の記録・
 * 取り直した結果）を足したもの。span の終わりのイベントは backend が詳細（内部名・引数・結果）を持って届く（#2373）
 * ので、結果をまだ持たない step（取り直す前）はイベントの詳細を残す。
 */
export function agentProgressDefinitions(steps: readonly RunStep[]): ChatProgressStepDefinitions {
  const byId = new Map(steps.filter((step) => step.tool_call).map((step) => [step.id, step]));
  if (byId.size === 0) return AGENT_CHAT_PROGRESS_STEPS;
  const tool = AGENT_CHAT_PROGRESS_STEPS.tool;
  return {
    ...AGENT_CHAT_PROGRESS_STEPS,
    tool: {
      ...tool,
      details: (_status, params, details) => {
        const id = params[RUN_STEP_PARAM];
        const step = typeof id === "string" ? byId.get(id) : undefined;
        if (!step || (details && !step.tool_result)) return details;
        return toolStepDetails(step) ?? details;
      },
    },
  };
}

/**
 * 処理の経過が古い形式の記録か（#3226）。共通の span の表（案 A）に書く前の Run は `trace_store` が無く、終わって
 * いれば処理の経過を出せない（画面は古い形式の案内を出す）。実行中・受け付けただけの Run と、表に書く Run（開始の前に
 * 終わった Run を含む）は古い形式ではない。
 */
export function runProgressIsLegacy(run: Pick<RunState, "status" | "trace_store">): boolean {
  return run.trace_store !== true && TERMINAL_RUN_STATUSES.has(run.status);
}

/** 処理の経過の行に、ツールの span と結んだ Run の step の表の結果を中身として付ける（チャットと履歴で同じ）。 */
export function runProgressSteps(
  run: Pick<RunState, "steps">,
  events: readonly ChatProgressEvent[],
  progress: ChatProgressStep[],
  tableOf?: (step: RunStep) => RunTimelineStep["children"]
): ChatProgressStep[] {
  if (!tableOf) return progress;
  const pairs = progressRunSteps(events, run.steps);
  if (pairs.size === 0) return progress;
  return progress.map((item) => {
    const step = pairs.get(item.id);
    const table = step && tableOf(step);
    return table ? { ...item, children: table } : item;
  });
}

/**
 * 実行の詳細の処理の経過（`RunTimeline`）。チャットの処理の経過と同じ記録（共通の span の表の snapshot → 木。#3226）
 * を、同じ名前（`AGENT_CHAT_PROGRESS_STEPS` と共通の span の名前）で写す。span の無い記録は空。
 */
export function runTimelineSteps(
  run: Pick<RunState, "id" | "steps">,
  events: readonly ChatProgressEvent[],
  tableOf?: (step: RunStep) => RunTimelineStep["children"],
  options: ChatProgressLabelOptions = {}
): RunTimelineStep[] {
  if (!chatProgressHasSpans(events)) return [];
  const progress = chatProgressStepsFromEvents(events, agentProgressDefinitions(run.steps), run.id, options);
  return runProgressSteps(run, events, progress, tableOf).map(chatProgressTimelineStep);
}

/**
 * 実行の詳細の「会話を開く」の URL（#2343。3 製品で同じ規則）。チャットの画面から送った Run（きっかけが画面）で、
 * 会話（thread）があり、会話を見られる人（system_admin か会話の持ち主）がチャットを開ける権限を持つときだけ。
 * MCP・品質評価・再実行の Run は、開く元のチャットの会話が無いので null（操作を出さない）。
 */
export function runConversationHref(
  run: Pick<RunState, "agent_id" | "thread_id" | "metadata" | "owned_by_me">,
  { seesAllUsers, canOpenChat }: { seesAllUsers: boolean; canOpenChat: boolean }
): string | null {
  if (!canOpenChat || !run.thread_id || runSourceOf(run.metadata) !== "user") return null;
  if (!seesAllUsers && run.owned_by_me === false) return null;
  return `${APP_ROUTES.chat}?${new URLSearchParams({ agent_id: run.agent_id, thread_id: run.thread_id }).toString()}`;
}

/**
 * 実行の詳細の「取消」（共通の `RunSummary` の `cancellation`。#3348）。取り消していない Run は null。
 * 引けない利用者（削除済みなど）は「（削除済みのユーザー）」。
 */
export function runCancellation(run: Pick<RunState, "cancellation">): RunCancellation | null {
  const value = run.cancellation;
  if (!value) return null;
  if (value.via === "system") return { via: "system" };
  return {
    via: value.via,
    actor: { name: value.actor_display_name || t("run.userUnknown"), isSelf: value.by_me },
    apiKeyName: value.api_key_name || null,
  };
}
