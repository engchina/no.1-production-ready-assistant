/**
 * e2e の mock の Run が持つ、チャットの処理の経過のイベント（3 製品共通の契約。#1359）。
 *
 * 処理の経過は Run の trace（span の木。#3226）の投影で、backend は実行の区間の span の開始・終了をイベントに記録する
 * （共通の span の表。#3226 の案 A）。e2e は実 backend を起動しないので、spec が `ProgressLog` で backend と同じ形の
 * span のイベントを積み、mock の Run の `progress_events`（mock の中だけの記録）に入れる。mock は backend と同じく、
 * span ごとの最新の状態と Run の状態の終端にした snapshot（`GET /api/runs/{id}/trace`）で返す。イベントは追記型
 * （span のイベントはその span の今の状態の全体）なので、進めるときは同じ log に足していく。
 *
 * span の id は spec が付ける読みやすい名前（`chat-1`・`tool:<名前>`・`approval_wait` など。画面の test id に出る。
 * backend は 16 桁の hex。工程の種類の名前〔`plan` など〕と同じ id にしない: 種類の無い span は id が種類の代わりに
 * なる）。名前から span の名前と工程の種類を backend と同じ規則で決める:
 * - `chat*` → `gen_ai.chat`。種類（`plan` / `respond`）はモデルの呼び出しが終わってから spec が `kind` で付ける
 *   （実行中・失敗は種類なしで「モデルの呼び出し · モデル名」）。
 * - `tool:<名前>` → `gen_ai.execute_tool`（種類 `tool`、params `{ tool }`）。
 * - `approval_wait*` → `agent.approval_wait`（種類 `approval`）。待ちが終わってから、再開の区間で記録する。
 * - `confirmation_wait*` → `agent.confirmation_wait`（種類 `confirmation`）。
 * - `mcp*` → `mcp.request`。
 */

type Json = Record<string, unknown>;
type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";
type TerminalStatus = "done" | "failed" | "cancelled";
type Params = Record<string, string | number | boolean>;

/** mock の trace id（32 桁）。 */
export const SYNTHETIC_TRACE_ID = "5".repeat(32);
/** mock のモデルの呼び出しのモデル名。 */
export const SYNTHETIC_MODEL = "synthetic-model";
/** 実行の区間（root）の span の id。 */
export const EXECUTE_SPAN = "span-execute";

export interface ProgressStepOptions {
  /** 工程の種類（省略時は名前の規則）。 */
  kind?: string;
  params?: Params;
  startedAt?: string;
  finishedAt?: string;
  /** 段を開いて見る詳細（backend が span の終わりのイベントに入れる。#2373）。 */
  details?: Record<string, unknown>;
  /** 親の span（省略時は今の区間の root）。 */
  parent?: string;
  /** span の名前（省略時は名前の規則）。 */
  name?: string;
  /** span の属性（省略時はモデル名・ツール名）。 */
  attributes?: Params;
  /** span を書いた製品（省略時は Agent。呼び先の RAG / NL2SQL の span は `rag` / `nl2sql`。#3272）。 */
  product?: string;
  /** 実行の全体の span（呼び先の製品の server の span・実行の span）。 */
  root?: boolean;
  /** span を書いた製品が付けた工程の名前（#3272）。 */
  displayName?: string;
}

interface SpanState {
  name: string;
  parent?: string;
  root?: boolean;
  status: StepStatus;
  kind?: string;
  params?: Params;
  startedAt?: string;
  finishedAt?: string;
  details?: Record<string, unknown>;
  attributes?: Params;
  product?: string;
  displayName?: string;
}

function spanName(id: string): string | undefined {
  if (id.startsWith("chat")) return "gen_ai.chat";
  if (id.startsWith("tool:")) return "gen_ai.execute_tool";
  if (id.startsWith("approval_wait")) return "agent.approval_wait";
  if (id.startsWith("confirmation_wait")) return "agent.confirmation_wait";
  if (id.startsWith("mcp")) return "mcp.request";
  return undefined;
}

/** 名前から工程の種類を決める（モデルの呼び出しの種類は、終わったときに spec が `kind` で付ける）。 */
function defaultKind(name: string): string | undefined {
  if (name === "gen_ai.execute_tool") return "tool";
  if (name === "agent.approval_wait") return "approval";
  if (name === "agent.confirmation_wait") return "confirmation";
  return undefined;
}

function defaultAttributes(id: string, name: string): Params | undefined {
  if (name === "gen_ai.chat") return { "gen_ai.operation.name": "chat", "gen_ai.request.model": SYNTHETIC_MODEL };
  if (name === "gen_ai.execute_tool") {
    const tool = id.slice("tool:".length).replace(/#\d+$/, "");
    return { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool };
  }
  return undefined;
}

const FINISHED: ReadonlySet<StepStatus> = new Set(["done", "failed", "skipped"]);

export class ProgressLog {
  readonly events: Json[] = [];
  private readonly spans = new Map<string, SpanState>();
  private root = EXECUTE_SPAN;
  private resumes = 0;

  constructor(
    readonly runId: string,
    private readonly clock: () => string = () => new Date().toISOString(),
    startedAt?: string
  ) {
    this.write(EXECUTE_SPAN, { name: "agent.execute", root: true, status: "running", startedAt: startedAt ?? clock() });
  }

  /** span の今の状態を足す。始まった時刻・params・属性は前のイベントから引き継ぐ（backend の記録と同じ）。 */
  step(id: string, status: StepStatus, options: ProgressStepOptions = {}): this {
    const now = this.clock();
    const before = this.spans.get(id);
    const name = options.name ?? before?.name ?? spanName(id) ?? id;
    let startedAt = options.startedAt ?? before?.startedAt;
    if (!startedAt && status !== "pending") startedAt = now;
    const finishedAt = options.finishedAt ?? (FINISHED.has(status) ? now : undefined);
    const params = options.params ?? before?.params ?? (name === "gen_ai.execute_tool" ? { tool: String(defaultAttributes(id, name)?.["gen_ai.tool.name"]) } : undefined);
    this.write(id, {
      name,
      parent: options.parent ?? (before ? before.parent : this.root),
      status,
      root: options.root ?? before?.root,
      kind: options.kind ?? before?.kind ?? defaultKind(name),
      params,
      startedAt,
      finishedAt,
      details: options.details ?? before?.details,
      attributes: options.attributes ?? before?.attributes ?? defaultAttributes(id, name),
      product: options.product ?? before?.product,
      displayName: options.displayName ?? before?.displayName,
    });
    return this;
  }

  /**
   * 承認・確認の答えの後の再開（#3226）。今の区間の root を閉じ、実行の区間の子の新しい root を開く（同じ trace）。
   * 待ちの span（`approval_wait` など）は、再開の後に `step(..., "done", { startedAt, finishedAt })` で足す。
   */
  resume(startedAt?: string): this {
    this.closeRoot("done");
    this.resumes += 1;
    const id = this.resumes === 1 ? "span-resume" : `span-resume#${this.resumes}`;
    this.write(id, { name: "agent.resume", root: true, parent: EXECUTE_SPAN, status: "running", startedAt: startedAt ?? this.clock() });
    this.root = id;
    return this;
  }

  /** 承認・確認を待つために今の区間を閉じる（root を完了にする。待ちの行は出さない）。 */
  pause(): this {
    this.closeRoot("done");
    return this;
  }

  /** 終端（完了・失敗・停止）。backend と同じく、開いている span を閉じてから終端を足す。 */
  terminal(status: TerminalStatus): this {
    const closing: StepStatus = status === "done" ? "done" : status === "failed" ? "failed" : "skipped";
    for (const [id, span] of this.spans) {
      if (span.status === "running") this.step(id, closing);
      else if (span.status === "pending") this.step(id, "skipped");
    }
    this.events.push({
      schema_version: 1,
      type: "terminal",
      seq: this.events.length + 1,
      target_id: this.runId,
      attempt: 0,
      emitted_at: this.clock(),
      status,
    });
    return this;
  }

  private closeRoot(status: StepStatus) {
    const root = this.spans.get(this.root);
    if (root && root.status === "running") this.write(this.root, { ...root, status, finishedAt: this.clock() });
  }

  private write(id: string, span: SpanState) {
    this.spans.set(id, span);
    const event: Json = {
      schema_version: 1,
      type: "step",
      seq: this.events.length + 1,
      target_id: this.runId,
      attempt: 0,
      emitted_at: this.clock(),
      step_id: id,
      status: span.status,
      name: span.name,
      trace_id: SYNTHETIC_TRACE_ID,
      // backend は表の span の製品（PRODUCT 列）を入れる（#3272）。
      product: span.product ?? "agent",
    };
    if (span.displayName) event.display_name = span.displayName;
    if (span.kind && span.kind !== id) event.kind = span.kind;
    if (span.parent) event.parent_id = span.parent;
    if (span.root) event.root = true;
    if (span.startedAt) event.started_at = span.startedAt;
    if (span.finishedAt) event.finished_at = span.finishedAt;
    if (span.params) event.params = span.params;
    if (span.details) event.details = span.details;
    if (span.attributes) event.attributes = span.attributes;
    this.events.push(event);
  }
}

/** mock の Run のツールの step（`ProgressLog` に写すのに要るところだけ）。 */
interface MockStep {
  id?: unknown;
  status?: unknown;
  tool_call?: { name?: unknown } | null;
  started_at?: unknown;
  completed_at?: unknown;
}

const STEP_STATUS: Readonly<Record<string, StepStatus>> = {
  completed: "done",
  failed: "failed",
  cancelled: "skipped",
};

/**
 * spec がイベントを入れていない mock の Run の、状態とツールの step から作るイベント（backend が記録する span の木と
 * 同じ形: モデルの呼び出し → ツール → … → 回答の作成）。状態が進んでも前の番号のイベントは変わらない（追記型。
 * 前の状態で作ったイベントが、進んだ状態で作ったイベントの先頭と同じ）ので、取り直しのたびに作っても画面の記録と
 * 矛盾しない。ツールの span は Run の step と結ぶ（params の `run_step`）。
 */
export function progressEventsForStatus(run: Json): Json[] {
  const id = String(run.id);
  const status = String(run.status);
  const createdAt = typeof run.created_at === "string" ? run.created_at : new Date(0).toISOString();
  const endedAt = typeof run.updated_at === "string" ? run.updated_at : createdAt;
  // 受け付けただけの Run には span が無い。承認の後の再開を待つ Run（保存した SDK の状態がある）は、前の区間の
  // span（モデルの呼び出しの完了）を持つ。
  const resumePending = typeof (run.metadata as { _builtin_sdk_state?: unknown } | undefined)?._builtin_sdk_state === "string";
  if (status === "queued" && !resumePending) return [];
  const log = new ProgressLog(id, () => endedAt, createdAt);
  const titles = (run.metadata as { tool_titles?: Record<string, unknown> } | undefined)?.tool_titles ?? {};
  const steps = (Array.isArray(run.steps) ? (run.steps as MockStep[]) : []).filter((step) => step.tool_call?.name);
  let chat = 1;
  log.step(`chat-${chat}`, "running", { startedAt: createdAt });
  const counts = new Map<string, number>();
  for (const step of steps) {
    const stepStatus = STEP_STATUS[String(step.status)] ?? (step.status === "running" ? "running" : undefined);
    if (!stepStatus) continue;
    log.step(`chat-${chat}`, "done", { kind: "plan" });
    const name = String(step.tool_call?.name);
    const count = (counts.get(name) ?? 0) + 1;
    counts.set(name, count);
    const spanId = count === 1 ? `tool:${name}` : `tool:${name}#${count}`;
    const title = typeof titles[name] === "string" ? (titles[name] as string) : undefined;
    const params: Params = { tool: name, run_step: String(step.id), ...(title ? { title } : {}) };
    const startedAt = typeof step.started_at === "string" ? step.started_at : createdAt;
    log.step(spanId, "running", { params, startedAt });
    if (stepStatus === "running") return log.events;
    log.step(spanId, stepStatus, { finishedAt: typeof step.completed_at === "string" ? step.completed_at : endedAt });
    chat += 1;
    log.step(`chat-${chat}`, "running");
  }
  switch (status) {
    case "completed":
      return log.step(`chat-${chat}`, "done", { kind: "respond" }).terminal("done").events;
    case "failed":
      return log.step(`chat-${chat}`, "failed").terminal("failed").events;
    case "cancelled":
      return log.terminal("cancelled").events;
    case "queued":
    case "waiting_approval":
    case "waiting_confirmation":
      return log.step(`chat-${chat}`, "done", { kind: "plan" }).pause().events;
    default:
      return log.events;
  }
}
