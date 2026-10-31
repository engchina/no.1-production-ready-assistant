import type { BrowserContext, Route } from "@playwright/test";

/**
 * e2e の mock の処理の経過（3 製品共通の契約。#1359・#3226 の案 A）。
 *
 * 処理の経過は実行の trace（span の木）の投影。backend と同じく、実行の全体の span（root。画面は行にしない）の下に、
 * 始まった工程の span（`kind` が工程の id・`name` が span の名前）を始まった順に開始（running）→ 終了（done /
 * failed / skipped）で作り、最後に終端を出す。始まっていない工程の span は無い。
 *
 * backend は span を共通の span の表に書き、画面は記録ごとの API（`.../trace`）で span の表の snapshot（span ごとに
 * 最新の状態 1 件と終端）を読む。mock の span の表（`recordMockTrace`）は記録の ID ごとに snapshot を持ち、
 * `routeMockTraces`（e2e の fixture が context ごとに入れる）が API に答える。mock の記録の応答を作るときに
 * `withJobProgress` などで span の表へ書く（応答の前に書くので、画面が読むときには入っている）。
 */

export const JOB_PROGRESS_STEPS = [
  "queue",
  "prepare_context",
  "generate_sql",
  "safety_check",
  "execute_sql",
  "format_results",
] as const;

export type JobProgressStep = (typeof JOB_PROGRESS_STEPS)[number];
type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";
type Params = Record<string, string | number | boolean>;

/** 工程の span の中の span（モデルの呼び出しなど。工程の定義の無い技術の span）。 */
export interface JobProgressChildSpec {
  name: string;
  status: Exclude<StepStatus, "pending">;
  attributes?: Params;
}

export interface JobProgressStepSpec {
  status: StepStatus;
  startedAt?: string;
  finishedAt?: string;
  /** 補足の値。`stopped: true` は停止で終わった span（未実行と停止の印）。 */
  params?: Params;
  /** 終わった段階に付ける、段を開いて見る詳細（#2294。backend の `ChatProgressStepDetails` と同じ形）。 */
  details?: Record<string, unknown>;
  /** 工程の span の中の span。 */
  children?: JobProgressChildSpec[];
}

export interface JobProgressSpec {
  /** 工程の状態（書かない・待機中の工程は始まっていないので span が無い）。 */
  steps: Partial<Record<JobProgressStep, StepStatus | JobProgressStepSpec>>;
  terminal?: "done" | "failed" | "cancelled";
  /** 時刻を書かない工程の開始・終了の時刻。 */
  at: string;
  attempt?: number;
}

/** mock の trace id（ジョブ ID から決める。合成の値）。 */
export function jobProgressTraceId(jobId: string): string {
  let hash = 0;
  for (const char of jobId) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash.toString(16).padStart(8, "0").repeat(4);
}

function spanId(index: number): string {
  return (index + 1).toString(16).padStart(16, "0");
}

const ROOT_SPAN_ID = spanId(0);

/** 工程の状態から span のイベントの一覧を作る。 */
export function jobProgressEvents(jobId: string, spec: JobProgressSpec): Record<string, unknown>[] {
  const events: Record<string, unknown>[] = [];
  const traceId = jobProgressTraceId(jobId);
  const push = (event: Record<string, unknown>) =>
    events.push({
      schema_version: 1,
      seq: events.length + 1,
      target_id: jobId,
      attempt: spec.attempt ?? 0,
      emitted_at: spec.at,
      ...event,
    });
  const span = (
    id: string,
    name: string,
    parentId: string | null,
    state: { status: Exclude<StepStatus, "pending">; startedAt: string; finishedAt: string; kind?: string; params?: Params; details?: Record<string, unknown>; attributes?: Params; root?: boolean }
  ) => {
    const { stopped, ...params } = state.params ?? {};
    const base = {
      type: "step",
      step_id: id,
      ...(state.kind ? { kind: state.kind } : {}),
      name,
      ...(parentId ? { parent_id: parentId } : {}),
      ...(state.root ? { root: true } : {}),
      trace_id: traceId,
      started_at: state.startedAt,
      ...(Object.keys(params).length > 0 ? { params } : {}),
      ...(state.attributes ? { attributes: state.attributes } : {}),
    };
    push({ ...base, status: "running" });
    if (state.status === "running") return;
    push({
      ...base,
      status: state.status,
      finished_at: state.finishedAt,
      ...(state.details ? { details: state.details } : {}),
      ...(stopped === true ? { cancelled: true } : {}),
    });
  };
  const started = JOB_PROGRESS_STEPS.flatMap((step) => {
    const raw = spec.steps[step];
    if (!raw) return [];
    const state: JobProgressStepSpec = typeof raw === "string" ? { status: raw } : raw;
    return state.status === "pending" ? [] : [[step, state] as const];
  });
  if (started.length === 0 && !spec.terminal) return events;
  const rootStarted = started[0]?.[1].startedAt ?? spec.at;
  push({ type: "step", step_id: ROOT_SPAN_ID, name: "nl2sql.execute", root: true, trace_id: traceId, status: "running", started_at: rootStarted });
  for (const [step, state] of started) {
    // mock の span の id は工程の id（画面の行の `data-step-id`・test id で引ける。実の id は不透明な 16 桁）。
    const id: string = step;
    const startedAt = state.startedAt ?? spec.at;
    const finishedAt = state.finishedAt ?? spec.at;
    const status = state.status as Exclude<StepStatus, "pending">;
    // 子の span は親の開始の後・親の終了の前に記録する。
    const children = state.children ?? [];
    const childIds = children.map((_, index) => `${step}.${index + 1}`);
    const { stopped, ...params } = state.params ?? {};
    const base = {
      type: "step",
      step_id: id,
      kind: step,
      name: `nl2sql.${step}`,
      parent_id: ROOT_SPAN_ID,
      trace_id: traceId,
      started_at: startedAt,
      ...(Object.keys(params).length > 0 ? { params } : {}),
    };
    push({ ...base, status: "running" });
    children.forEach((child, index) =>
      span(childIds[index]!, child.name, id, { status: child.status, startedAt, finishedAt, attributes: child.attributes })
    );
    if (status === "running") continue;
    push({
      ...base,
      status,
      finished_at: finishedAt,
      ...(state.details ? { details: state.details } : {}),
      ...(stopped === true ? { cancelled: true } : {}),
    });
  }
  if (spec.terminal) {
    const rootStatus = spec.terminal === "done" ? "done" : spec.terminal === "failed" ? "failed" : "skipped";
    push({ type: "step", step_id: ROOT_SPAN_ID, name: "nl2sql.execute", root: true, trace_id: traceId, status: rootStatus, started_at: rootStarted, finished_at: spec.at });
    push({ type: "terminal", status: spec.terminal });
  }
  return events;
}

/** span のイベントの一覧を、span の表の snapshot（span ごとに最新の状態 1 件と終端。番号は一覧の中）にする。 */
export function traceSnapshot(events: readonly Record<string, unknown>[]): Record<string, unknown>[] {
  const spans = new Map<string, Record<string, unknown>>();
  let terminal: Record<string, unknown> | undefined;
  for (const event of events) {
    if (event.type === "terminal") terminal = event;
    else if (typeof event.step_id === "string") {
      spans.delete(event.step_id);
      spans.set(event.step_id, event);
    }
  }
  const started = [...spans.values()].sort((a, b) =>
    String(a.started_at ?? "").localeCompare(String(b.started_at ?? "")),
  );
  return [...started, ...(terminal ? [terminal] : [])].map((event, index) => ({ ...event, seq: index + 1 }));
}

interface MockTrace {
  events: Record<string, unknown>[];
  droppedSpans: number;
  /** テストが直接書いた span（worker が書いた span の代わり）。記録の応答（`withJobProgress`）で上書きしない。 */
  pinned: boolean;
}

/** mock の span の表（記録の ID → snapshot）。test ごとに fixture が空にする。 */
const mockTraces = new Map<string, MockTrace>();

export function resetMockTraces(): void {
  mockTraces.clear();
}

/**
 * 記録（ジョブ・実行履歴の行・DB 管理チャットのターン）の span を mock の span の表に書く。`pinned` はテストが worker の
 * 代わりに書く span（記録の応答を待たずに表に入る span。記録の応答では上書きしない）。
 */
export function recordMockTrace(
  targetId: string,
  events: readonly Record<string, unknown>[],
  { droppedSpans = 0, pinned = false }: { droppedSpans?: number; pinned?: boolean } = {},
): void {
  mockTraces.set(targetId, { events: traceSnapshot(events), droppedSpans, pinned });
}

const TRACE_ROUTE =
  /\/api\/nl2sql\/(?:jobs\/([^/?]+)|history\/([^/?]+)|db-admin-chat\/conversations\/[^/?]+\/turns\/([^/?]+))\/trace(?:\?|$)/;

async function fulfillMockTrace(route: Route): Promise<void> {
  const match = TRACE_ROUTE.exec(new URL(route.request().url()).pathname);
  const id = decodeURIComponent(match?.[1] ?? match?.[2] ?? match?.[3] ?? "");
  const trace = mockTraces.get(id);
  // span の無い終わった記録（実行履歴の行・DB 管理チャットのターン）は、backend と同じく終端だけ（古い形式）。
  const legacy = [{ schema_version: 1, seq: 1, target_id: id, attempt: 0, emitted_at: "2026-01-01T00:00:00Z", type: "terminal", status: "done" }];
  const events = trace?.events ?? (match?.[1] ? [] : legacy);
  await route.fulfill({ json: { data: { events, dropped_spans: trace?.droppedSpans ?? 0 } } });
}

/**
 * 処理の経過の API（`/jobs/{id}/trace`・`/history/{id}/trace`・`/db-admin-chat/.../turns/{id}/trace`）に mock の
 * span の表から答える。spec の page の route は先に当たるので、e2e の fixture が page の route を包み、処理の経過の
 * 要求（`isTraceRequest`）は `route.fallback()` でここへ回す。
 */
export async function routeMockTraces(context: BrowserContext): Promise<void> {
  await context.route(TRACE_ROUTE, fulfillMockTrace);
}

/** 処理の経過の API の要求か（広い pattern の mock の route で `route.fallback()` する）。 */
export function isTraceRequest(route: Route): boolean {
  return TRACE_ROUTE.test(new URL(route.request().url()).pathname);
}

/** 完了したジョブ（すべての工程が完了）のイベント。 */
export function doneJobProgressEvents(jobId: string, at: string): Record<string, unknown>[] {
  return jobProgressEvents(jobId, {
    at,
    terminal: "done",
    steps: Object.fromEntries(JOB_PROGRESS_STEPS.map((step) => [step, "done"])),
  });
}

const JOB_STAGES = JOB_PROGRESS_STEPS.slice(1);

interface MockTurn {
  job_id: string;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  engine?: unknown;
  error_code?: unknown;
  steps?: unknown[];
  result?: unknown;
  last_execution?: unknown;
  /** 安全性の確認の完了で backend が記録する参照した表（結果が届く前の段階の mock 用。無ければ結果の値）。 */
  referenced_tables?: unknown;
  progress_events?: unknown;
  attempt?: unknown;
}

interface MockStep {
  stage: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function jobStageStatus(turn: MockTurn, step: MockStep | undefined, index: number, reported: number): StepStatus {
  if (step) return step.status === "error" ? "failed" : (step.status as StepStatus);
  if (turn.status === "done") return "done";
  // 段階の無いジョブ（古い mock）は、最初の段階を今の段階にする。
  if (index === 0 && reported === 0) return turn.status === "error" ? "failed" : "running";
  return "pending";
}

/**
 * mock のジョブ（状態・`steps`）から、backend が記録するのと同じ span のイベントを作る（mock の backend）。
 *
 * - 待機中のジョブ（worker が受け取る前）は span が無い（画面は開始待ちの仮の行を出す）。
 * - 開始待ち: worker が始めたら作成から開始までの終わった span。
 * - 工程: `steps` の状態（`error` は失敗）。書かれていない工程は、完了したジョブなら完了、それ以外は始まっていない。
 * - 終端のジョブは実行中の工程を失敗（停止なら停止で未実行）にする。始まっていない工程は span が無い。
 * - 終端でないのに実行中の工程が無ければ、最後の工程を実行中にする（backend は結果の保存まで実行中）。
 * - 補足の値: 生成方法、参照した表（安全性の確認の完了）、取得した行数（実行の完了）。
 */
export function turnProgressEvents(mock: object): Record<string, unknown>[] {
  const turn = mock as MockTurn;
  const at = turn.created_at;
  if (turn.status === "pending") return [];
  const terminal = turn.status === "done" || turn.status === "error";
  const cancelled = turn.error_code === "JOB_CANCELLED";
  const reported = new Map(((turn.steps ?? []) as MockStep[]).map((step) => [step.stage, step] as const));
  const steps: Partial<Record<JobProgressStep, JobProgressStepSpec>> = {};
  if (turn.started_at || turn.status !== "error") {
    steps.queue = { status: "done", startedAt: at, finishedAt: turn.started_at ?? at };
  }
  const lastDone = JOB_STAGES.reduce(
    (last, stage, index) => (reported.get(stage)?.status === "done" ? index : last),
    -1,
  );
  JOB_STAGES.forEach((stage, index) => {
    const step = reported.get(stage);
    let status = jobStageStatus(turn, step, index, reported.size);
    // 後の段階が完了していれば、前の段階も完了している。
    if (index < lastDone && (status === "pending" || status === "running")) status = "done";
    if (status === "pending") return;
    const params: Params = {};
    if (stage === "generate_sql" && typeof turn.engine === "string") params.engine = turn.engine;
    if (stage === "safety_check" && status === "done") {
      const tables = turn.referenced_tables ?? record(record(turn.result).safety).referenced_tables;
      if (Array.isArray(tables) && tables.length > 0) {
        params.tables = tables.slice(0, 3).join(", ");
        params.table_count = tables.length;
      }
    }
    if (stage === "execute_sql" && status === "done") {
      const rows = record(turn.last_execution).row_count;
      if (typeof rows === "number") params.rows = rows;
    }
    if (terminal && (status === "running" || (cancelled && status === "failed"))) {
      if (cancelled) {
        params.stopped = true;
        status = "skipped";
      } else status = "failed";
    }
    const started = step?.started_at ?? at;
    steps[stage] = {
      status,
      startedAt: started,
      finishedAt: step?.finished_at ?? turn.finished_at ?? at,
      params: Object.keys(params).length > 0 ? params : undefined,
    };
  });
  const current = Object.values(steps).some((step) => step?.status === "running");
  if (!terminal && !current) {
    steps.format_results = { ...(steps.format_results ?? { startedAt: at }), status: "running" };
  }
  const attempt = typeof turn.attempt === "number" ? Math.max(0, turn.attempt - 1) : 0;
  return jobProgressEvents(turn.job_id, {
    at,
    attempt,
    steps,
    terminal: terminal ? (turn.status === "done" ? "done" : cancelled ? "cancelled" : "failed") : undefined,
  });
}

/**
 * mock のジョブ（会話のターン・SQL 生成のジョブ）の span を mock の span の表に書き、応答の記録を返す（テストが
 * 自分で書いたイベント `progress_events` はそのまま書く）。応答には trace id だけを入れる（backend と同じ）。
 */
export function withJobProgress<T extends object>(turn: T): T {
  const mock = turn as unknown as MockTurn;
  const events = Array.isArray(mock.progress_events)
    ? (mock.progress_events as Record<string, unknown>[])
    : turnProgressEvents(turn);
  if (!mockTraces.get(mock.job_id)?.pinned) recordMockTrace(mock.job_id, events);
  const { progress_events: _events, ...rest } = turn as T & { progress_events?: unknown };
  return { ...rest, trace_id: events.length > 0 ? jobProgressTraceId(mock.job_id) : "" } as unknown as T;
}

/** mock の記録（実行履歴の行・DB 管理チャットのターン）の `progress_events` を span の表に書き、応答から外す。 */
export function withRecordedTrace<T extends object>(id: string, record: T): T {
  const { progress_events: events, ...rest } = record as T & { progress_events?: unknown };
  if (Array.isArray(events)) recordMockTrace(id, events as Record<string, unknown>[]);
  return rest as unknown as T;
}

interface MockStatementPlan {
  blocked_reason?: string;
  select_only?: boolean;
  requires_confirmation?: boolean;
  confirmation_target?: string;
}

/**
 * DB 管理チャットのターンの処理の経過（backend の `generate_db_admin_chat_sql` と同じ span の木。#3226）: 実行の全体の
 * 下に生成（`generate_sql`）と SQL の確認（`db_admin_check`。補足は文の分類ごとの数）。すべての文が止める文なら確認は失敗。
 */
export function dbAdminChatProgressEvents(
  turnId: string,
  plan: { statements: MockStatementPlan[] },
  { createdAt, generatedAt }: { createdAt: string; generatedAt: string },
): Record<string, unknown>[] {
  const events: Record<string, unknown>[] = [];
  const traceId = jobProgressTraceId(turnId);
  const push = (event: Record<string, unknown>) =>
    events.push({ schema_version: 1, seq: events.length + 1, target_id: turnId, attempt: 0, emitted_at: generatedAt, type: "step", trace_id: traceId, ...event });
  const statements = plan.statements;
  const blocked = statements.filter((item) => item.blocked_reason).length;
  const params: Params = {
    statements: statements.length,
    read_only: statements.filter((item) => !item.blocked_reason && item.select_only).length,
    change: statements.filter((item) => !item.blocked_reason && !item.select_only).length,
    blocked,
  };
  if (statements.length === 1 && statements[0]?.requires_confirmation) params.confirmation_target = statements[0].confirmation_target ?? "";
  const root = { step_id: ROOT_SPAN_ID, name: "nl2sql.db_admin_chat", root: true, started_at: createdAt };
  const generate = { step_id: spanId(1), kind: "generate_sql", name: "nl2sql.generate_sql", parent_id: ROOT_SPAN_ID, started_at: createdAt, params: { engine: "enterprise_ai_direct" } };
  const check = { step_id: spanId(2), kind: "db_admin_check", name: "nl2sql.safety_check", parent_id: ROOT_SPAN_ID, started_at: generatedAt, params };
  push({ ...root, status: "running" });
  push({ ...generate, status: "running" });
  push({ ...generate, status: "done", finished_at: generatedAt });
  push({ ...check, status: "running" });
  push({ ...check, status: statements.length > 0 && blocked === statements.length ? "failed" : "done", finished_at: generatedAt });
  push({ ...root, status: "done", finished_at: generatedAt });
  events.push({ schema_version: 1, seq: events.length + 1, target_id: turnId, attempt: 0, emitted_at: generatedAt, type: "terminal", status: "done" });
  return events;
}
