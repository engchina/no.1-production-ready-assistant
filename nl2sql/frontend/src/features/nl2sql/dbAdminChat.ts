// DB 管理チャット（#2264）の画面のロジック。会話とターンはサーバーが保存し（#2319）、続きの質問の
// 文脈もサーバーが保存した会話から作る。結果の行は保存しない（開き直したら要約と「もう一度実行」）。
//
// 生成した SQL はサーバーが文に分け（PL/SQL のブロックは 1 文）、文ごとに分類・実行する（#2333。
// SQL Developer などのワークシートと同じ）。「すべて実行」は上から順に実行し、最初のエラーで止める。
//
// 処理の経過は backend が生成のときに共通の span の表に書いた span の木（#3226 の案 A）を、ターンの処理の経過の
// API で読み、NL2SQL のチャットと同じ工程の定義（`CHAT_PROGRESS_STEP_DEFINITIONS`）と共通の `ChatProgress` で出す
// （生成 → SQL の確認（サーバーの分類））。実行は経過に足さない（文の区画に出す）。
import {
  chatProgressHasSpans,
  chatProgressStepsFromEvents,
  chatProgressTraceIdOf,
  parseChatProgressEvents,
  type ChatProgressStep,
  type ChatProgressStepStatus,
  type TraceDetailsLabels,
} from "@production-ready/ui";

// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import { t } from "../../lib/i18n";
import { CHAT_PROGRESS_STEP_DEFINITIONS, type TraceProgressResponse } from "./chatProgress";
import type {
  DbAdminChatExecuteData,
  DbAdminChatPlan,
  DbAdminChatRunAllSummary,
  DbAdminChatStatementLastExecution,
  DbAdminChatStatementOutcome,
  DbAdminChatStatementPlan,
  DbAdminChatTurn,
  DbAdminExecuteData,
} from "./types";

/** 会話の往復の上限（backend の DB_ADMIN_CHAT_TURN_LIMIT と同じ。超えたら新しい会話を勧める）。 */
export const DB_ADMIN_CHAT_TURN_LIMIT = 50;

/** 1 文の実行の状態。 */
export interface DbAdminChatExecution {
  /** `not_run` は「すべて実行」が前の文の失敗で止めたため実行しなかった文。 */
  status: "running" | "done" | "failed" | "not_run";
  startedAtMs: number;
  finishedAtMs?: number;
  result?: DbAdminExecuteData | null;
  /** 開き直したときの保存した要約（行は持たない）。この画面で受け取った結果には無い。 */
  saved?: DbAdminChatStatementLastExecution | null;
  /** 実行の要求そのものの失敗（権限・通信・timeout）。SQL の失敗は `result` の文の状態。 */
  requestError?: unknown;
}

/** ターンの「すべて実行」の状態。 */
export interface DbAdminChatRunAll {
  status: "running" | "done" | "failed";
  startedAtMs: number;
  finishedAtMs?: number;
  data?: DbAdminChatExecuteData | null;
  /** 開き直したときの保存した要約。 */
  saved?: DbAdminChatRunAllSummary | null;
  requestError?: unknown;
}

/** 1 往復の実行の状態（文の番号ごとの実行と「すべて実行」）。 */
export interface DbAdminChatTurnExecutions {
  statements: Record<number, DbAdminChatExecution>;
  runAll: DbAdminChatRunAll | null;
}

/** 文の分類（バッジ）: 読み取り専用 / 変更あり / 実行できない。 */
export type DbAdminChatStatementKind = "readOnly" | "change" | "blocked";

export function dbAdminChatStatementKind(statement: DbAdminChatStatementPlan): DbAdminChatStatementKind {
  if (statement.blocked_reason) return "blocked";
  return statement.select_only ? "readOnly" : "change";
}

function savedTimes(executedAt: string, elapsedMs: number): { startedAtMs: number; finishedAtMs?: number } {
  const finishedAtMs = Date.parse(executedAt);
  return Number.isFinite(finishedAtMs)
    ? { startedAtMs: finishedAtMs - elapsedMs, finishedAtMs }
    : { startedAtMs: Date.now() };
}

/** 保存した 1 文の最後の実行の要約を、画面の実行の状態にする（行は保存していない）。 */
export function dbAdminChatSavedStatementExecution(
  saved: DbAdminChatStatementLastExecution,
): DbAdminChatExecution {
  return {
    status: saved.status === "succeeded" ? "done" : saved.status === "not_run" ? "not_run" : "failed",
    ...savedTimes(saved.executed_at, saved.result?.timing?.elapsed_ms ?? 0),
    result: saved.result,
    saved,
  };
}

/** 保存したターンの文ごとの最後の実行と「すべて実行」の要約を、画面の実行の状態にする。 */
export function dbAdminChatSavedExecutions(turn: DbAdminChatTurn): DbAdminChatTurnExecutions {
  const statements: Record<number, DbAdminChatExecution> = {};
  for (const saved of turn.statement_executions ?? []) {
    statements[saved.index] = dbAdminChatSavedStatementExecution(saved);
  }
  const last = turn.last_run_all;
  return {
    statements,
    runAll: last
      ? { status: last.succeeded ? "done" : "failed", ...savedTimes(last.executed_at, 0), saved: last }
      : null,
  };
}

/** 実行の応答の 1 文の結果を、画面の実行の状態にする。 */
export function dbAdminChatExecutionFromOutcome(
  outcome: DbAdminChatStatementOutcome,
  data: DbAdminChatExecuteData,
  startedAtMs: number,
  finishedAtMs: number,
): DbAdminChatExecution {
  if (outcome.status === "not_run") return { status: "not_run", startedAtMs, finishedAtMs, result: null };
  if (outcome.status === "confirmation_required") {
    // 画面は確認語が合うまで押せないので、ここに来るのは確認語の規則が変わったときだけ。
    return {
      status: "failed",
      startedAtMs,
      finishedAtMs,
      result: null,
      requestError: new Error(data.warnings[0] ?? t("dbAdminChat.execute.failed")),
    };
  }
  return {
    status: outcome.status === "succeeded" ? "done" : "failed",
    startedAtMs,
    finishedAtMs,
    result: outcome.result,
  };
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

/** 段階の名前はチャットと同じ定義から作る（文言を 2 つの画面で分けない）。 */
function stepLabel(stage: "generate_sql" | "safety_check", status: ChatProgressStepStatus) {
  return CHAT_PROGRESS_STEP_DEFINITIONS[stage]?.label(status, {}) ?? stage;
}

/** 生成の段階（送信中・失敗の表示。応答が届くまでの仮の行）。 */
export function dbAdminChatGenerateStep(
  status: ChatProgressStepStatus,
  startedAtMs: number,
  finishedAtMs?: number,
): ChatProgressStep {
  return {
    id: "generate_sql",
    label: stepLabel("generate_sql", status),
    status,
    detail: t("chat.engine.enterprise"),
    startedAt: iso(startedAtMs),
    finishedAt: finishedAtMs === undefined ? undefined : iso(finishedAtMs),
  };
}

/** 1 往復の処理の経過（#3226 の案 A）。 */
export interface DbAdminChatProgress {
  steps: ChatProgressStep[];
  traceId?: string;
  /** span の無い古い形式のターン（処理の経過の位置に古い形式の案内を出す）。 */
  legacy: boolean;
  /** 書けなかった span がある（「一部の処理だけを記録しています。」を出す）。 */
  partial: boolean;
}

/**
 * 1 往復の処理の経過: ターンの処理の経過の API（共通の span の表の snapshot）の span の木（生成 → SQL の確認と、その中の
 * モデルの呼び出し）。実行（「実行」「もう一度実行」「すべて実行」）は利用者が後から押す操作なので経過に足さず、その文の
 * 区画の実行の表示に出す。経過は生成したときのまま変わらない。まだ読めていない（`trace` が無い）ときは何も出さない。
 */
export function dbAdminChatTraceProgress(
  turnId: string,
  trace: TraceProgressResponse | undefined,
  traceLabels?: TraceDetailsLabels,
): DbAdminChatProgress {
  const events = parseChatProgressEvents(trace?.events);
  const steps = chatProgressStepsFromEvents(events, CHAT_PROGRESS_STEP_DEFINITIONS, turnId, { traceLabels });
  const traceId = chatProgressTraceIdOf(events);
  const partial = (trace?.droppedSpans ?? 0) > 0;
  return {
    steps,
    ...(traceId ? { traceId } : {}),
    legacy: events.length > 0 && !chatProgressHasSpans(events) && !partial,
    partial,
  };
}

/** 実行の結果が成功か（全部の文が成功・実行済み）。 */
export function dbAdminExecutionSucceeded(result: DbAdminExecuteData): boolean {
  return (
    result.statements.length > 0 &&
    result.statements.every((item) => item.status === "success" || item.status === "executed")
  );
}

/** 実行の結果のうち、AI のエラー分析に渡す失敗の内容（失敗の文と ORA のメッセージ・警告）。 */
export function dbAdminFailureText(result: DbAdminExecuteData): string {
  const lines = result.statements
    .filter((item) => item.error_message)
    .map((item) => `#${item.index} ${item.statement_type}: ${item.error_message}`);
  return [...lines, ...result.warnings].join("\n");
}

/** 確認語が一致したか（前後の空白は無視。確認語が要らないなら常に true）。 */
export function dbAdminChatConfirmed(
  target: { requires_confirmation: boolean; confirmation_target: string },
  value: string,
): boolean {
  if (!target.requires_confirmation) return true;
  return value.trim() === target.confirmation_target;
}

/** 1 文を実行できるか（止める文でなく、確認語が要るなら一致している）。 */
export function dbAdminChatCanRunStatement(statement: DbAdminChatStatementPlan, confirmation: string): boolean {
  return !statement.blocked_reason && dbAdminChatConfirmed(statement, confirmation);
}

/** 「すべて実行」できるか（止める文が無く、確認語が要るなら一致している）。 */
export function dbAdminChatCanRunAll(plan: DbAdminChatPlan, confirmation: string): boolean {
  return plan.statements.length > 0 && !plan.blocked && dbAdminChatConfirmed(plan, confirmation);
}

/** 「すべて実行」を止める文の番号（「#2・#4」）。 */
export function dbAdminChatBlockedStatementLabels(plan: DbAdminChatPlan): string {
  return plan.statements
    .filter((statement) => statement.blocked_reason)
    .map((statement) => `#${statement.index}`)
    .join("・");
}
