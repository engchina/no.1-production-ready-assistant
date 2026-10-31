import type { CallContent, CallDetails, RunStepDetailCode, RunStepDetailField, RunStepDetails } from "../data/run-step-details";
import { DEFAULT_TRACE_DETAILS_LABELS, type TraceDetailsLabels } from "../../lib/trace-labels";
import { isMcpProtocolSpan, traceSpanDisplayName, traceSpanFields } from "../../lib/trace-tree";
import type { ChatProgressStep, ChatProgressStepStatus } from "./chat-progress";
import { CHAT_PROGRESS_STATUS_RANK } from "./chat-progress-steps";

/**
 * チャットの処理の段階のイベント（3 製品共通の契約。#1359）。
 *
 * 正本は `platform/contracts/chat-progress/chat-progress-events.json`（backend の `pr_backend_core.chat_progress`
 * から生成する）。段階の一覧の snapshot を置き換えるのではなく、追記型のイベントを番号（`seq`）の順に積む。
 *
 * - 段階のイベント（`type: "step"`）は、その段階の今の状態の全体（状態・時刻・補足・名前の値）を持つ。
 * - 終端のイベント（`type: "terminal"`）の後、同じ試行（`attempt`）の段階のイベントは来ない。
 * - `seq` は対象（`target_id`）ごとに 1 から連続する。試行が増えたら段階の一覧を作り直す。
 *
 * 段階の名前は契約に入れない。製品の段階の定義（`ChatProgressStepDefinitions`。`kind` → i18n の名前）で付ける。
 *
 * **段は span（#3226）**: 処理の経過は trace（span の木）の投影で、段のイベントは span の開始・表示用のデータの変更・
 * 終了を表す（`name`・`parent_id`・`root` を持つ）。段は親子から入れ子にして木の順に並べ、実行の全体の span
 * （`root`）は行にしない（見出しの要約が全体を表す）。名前は `kind` の製品の定義、無ければ共通の規則（#3219 の技術の
 * span の名前）で付ける。
 */

export const CHAT_PROGRESS_SCHEMA_VERSION = 1;
/** SSE のイベントの名前（`event:`）。 */
export const CHAT_PROGRESS_SSE_EVENT = "chat_progress";
/** イベントの無い間の heartbeat（`event: heartbeat`、`data: {"last_seq": N}`）。 */
export const CHAT_PROGRESS_SSE_HEARTBEAT_EVENT = "heartbeat";

export const CHAT_PROGRESS_STEP_STATUSES: readonly ChatProgressStepStatus[] = [
  "pending",
  "running",
  "done",
  "failed",
  "skipped",
];
export const CHAT_PROGRESS_TERMINAL_STATUSES = ["done", "failed", "cancelled"] as const;
export type ChatProgressTerminalStatus = (typeof CHAT_PROGRESS_TERMINAL_STATUSES)[number];

/** 名前・補足を組み立てる値（ツール名・件数など）。 */
export type ChatProgressParamValue = string | number | boolean;
export type ChatProgressParams = Readonly<Record<string, ChatProgressParamValue>>;

interface ChatProgressEventBase {
  schema_version: 1;
  /** 対象ごとに 1 から連続して増える番号（SSE の `id:`）。 */
  seq: number;
  /** 対象（RAG の回答のメッセージ・NL2SQL のジョブ・Agent の Run）の id。 */
  target_id: string;
  /** 試行。引き継いだ実行で増え、増えたら段階の一覧を作り直す。 */
  attempt: number;
  /** ISO 8601。 */
  emitted_at: string;
}

export interface ChatProgressStepEvent extends ChatProgressEventBase {
  type: "step";
  step_id: string;
  /** 段階の定義の種類（名前の key）。省略時は `step_id`。 */
  kind?: string;
  status: ChatProgressStepStatus;
  started_at?: string;
  finished_at?: string;
  detail?: string;
  params?: ChatProgressParams;
  /** 段を開いて見る詳細（見出しと値・行・SQL や JSON。backend が長さを切り、秘密らしい値を伏せる。#2294）。 */
  details?: RunStepDetails;
  /** span の名前（段が span のとき。#3226）。 */
  name?: string;
  /** 親の span の id。 */
  parent_id?: string;
  /** 実行の全体の span。 */
  root?: boolean;
  trace_id?: string;
  /** span の安全な属性（モデル・ツール・利用量・試行）。 */
  attributes?: ChatProgressParams;
  /** 停止で終わった span（状態は skipped）。 */
  cancelled?: boolean;
  /** span を書いた製品（`rag` / `nl2sql` / `agent`。#3272）。 */
  product?: string;
  /** span を書いた製品が付けた工程の名前（名詞。別の製品の画面が出す。#3272）。 */
  display_name?: string;
}

export interface ChatProgressTerminalEvent extends ChatProgressEventBase {
  type: "terminal";
  status: ChatProgressTerminalStatus;
}

export type ChatProgressEvent = ChatProgressStepEvent | ChatProgressTerminalEvent;

/** polling の応答（`since` より後のイベント）。 */
export interface ChatProgressPage {
  target_id: string;
  attempt: number;
  events: ChatProgressEvent[];
  /** 記録の最後の番号。 */
  last_seq: number;
  /** 今の試行が終わったか。 */
  terminal: boolean;
}

// ---------------------------------------------------------------------------
// 入力の検証（SSE・API の応答は未検証の入力）
// ---------------------------------------------------------------------------

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function positiveInt(value: unknown, min: number): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= min ? value : null;
}

function params(value: unknown): ChatProgressParams | undefined {
  const source = record(value);
  if (!source) return undefined;
  const entries = Object.entries(source).filter(
    (entry): entry is [string, ChatProgressParamValue] =>
      typeof entry[1] === "string" || typeof entry[1] === "number" || typeof entry[1] === "boolean"
  );
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

const CODE_LANGUAGES = ["json", "sql", "text"] as const;

/** 段の詳細を検証する（形の違う要素は捨てる。中身が無ければ undefined）。 */
export function parseChatProgressStepDetails(value: unknown): RunStepDetails | undefined {
  const source = record(value);
  if (!source) return undefined;
  const fields: RunStepDetailField[] = (Array.isArray(source.fields) ? source.fields : []).flatMap((item) => {
    const field = record(item);
    const label = text(field?.label);
    const fieldValue = text(field?.value);
    if (!label || !fieldValue) return [];
    return [field?.mono === true ? { label, value: fieldValue, mono: true } : { label, value: fieldValue }];
  });
  const lines = (Array.isArray(source.lines) ? source.lines : []).filter(
    (line): line is string => typeof line === "string" && line.length > 0
  );
  const code: RunStepDetailCode[] = (Array.isArray(source.code) ? source.code : []).flatMap((item) => {
    const block = record(item);
    const body = text(block?.text);
    if (!body) return [];
    const result: RunStepDetailCode = { text: body };
    const label = text(block?.label);
    if (label) result.label = label;
    const language = CODE_LANGUAGES.find((candidate) => candidate === block?.language);
    if (language) result.language = language;
    return [result];
  });
  const rawCall = record(source.call);
  const readContent = (value: unknown): CallContent => {
    const item = record(value);
    const status = item?.status;
    return status === "recorded" || status === "truncated"
      ? { status, text: typeof item?.text === "string" ? item.text : "" }
      : { status: status === "withheld" ? "withheld" : "not_recorded", text: "" };
  };
  const call: CallDetails | undefined = rawCall ? { ...(rawCall.capture_enabled === false ? {capture_enabled: false} : {}), input: readContent(rawCall.input), output: readContent(rawCall.output), schema: readContent(rawCall.schema) } : undefined;
  if (!fields.length && !lines.length && !code.length && !call) return undefined;
  const details: RunStepDetails = {};
  if (call) details.call = call;
  if (fields.length) details.fields = fields;
  if (lines.length) details.lines = lines;
  if (code.length) details.code = code;
  if (source.truncated === true) details.truncated = true;
  return details;
}

/** 1 件のイベントを検証する。形の違うものは null。 */
export function parseChatProgressEvent(value: unknown): ChatProgressEvent | null {
  const source = record(value);
  if (!source) return null;
  const seq = positiveInt(source.seq, 1);
  const attempt = positiveInt(source.attempt ?? 0, 0);
  const targetId = text(source.target_id);
  if (seq === null || attempt === null || !targetId) return null;
  const base = {
    schema_version: 1 as const,
    seq,
    target_id: targetId,
    attempt,
    emitted_at: text(source.emitted_at) ?? "",
  };
  if (source.type === "terminal") {
    const status = CHAT_PROGRESS_TERMINAL_STATUSES.find((candidate) => candidate === source.status);
    return status ? { ...base, type: "terminal", status } : null;
  }
  if (source.type !== "step") return null;
  const stepId = text(source.step_id);
  const status = CHAT_PROGRESS_STEP_STATUSES.find((candidate) => candidate === source.status);
  if (!stepId || !status) return null;
  const event: ChatProgressStepEvent = { ...base, type: "step", step_id: stepId, status };
  const kind = text(source.kind);
  if (kind) event.kind = kind;
  const startedAt = text(source.started_at);
  if (startedAt) event.started_at = startedAt;
  const finishedAt = text(source.finished_at);
  if (finishedAt) event.finished_at = finishedAt;
  const detail = text(source.detail);
  if (detail) event.detail = detail;
  const values = params(source.params);
  if (values) event.params = values;
  const details = parseChatProgressStepDetails(source.details);
  if (details) event.details = details;
  const name = text(source.name);
  if (name) event.name = name.slice(0, 128);
  const parentId = text(source.parent_id);
  if (parentId) event.parent_id = parentId;
  if (source.root === true) event.root = true;
  const traceId = text(source.trace_id);
  if (traceId && /^[0-9a-f]{32}$/.test(traceId)) event.trace_id = traceId;
  const attributes = params(source.attributes);
  if (attributes) event.attributes = attributes;
  if (source.cancelled === true) event.cancelled = true;
  const product = text(source.product);
  if (product && /^[a-z0-9_]{1,32}$/.test(product)) event.product = product;
  const displayName = text(source.display_name)?.trim();
  if (displayName) event.display_name = displayName.slice(0, 128);
  return event;
}

/** イベントの一覧を検証する（形の違う要素は捨てる）。 */
export function parseChatProgressEvents(value: unknown): ChatProgressEvent[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseChatProgressEvent).filter((event): event is ChatProgressEvent => event !== null);
}

/** polling の応答を検証する。形の違うものは null。 */
export function parseChatProgressPage(value: unknown): ChatProgressPage | null {
  const source = record(value);
  if (!source) return null;
  const targetId = text(source.target_id);
  const lastSeq = positiveInt(source.last_seq, 0);
  const attempt = positiveInt(source.attempt ?? 0, 0);
  if (!targetId || lastSeq === null || attempt === null) return null;
  return {
    target_id: targetId,
    attempt,
    events: parseChatProgressEvents(source.events),
    last_seq: lastSeq,
    terminal: source.terminal === true,
  };
}

// ---------------------------------------------------------------------------
// 組み立て（イベント → 段階の一覧）。backend の `fold_chat_progress_events` と同じ規則。
// ---------------------------------------------------------------------------

/** 段が span のときの span の値（#3226）。 */
export interface ChatProgressEventSpan {
  name: string;
  parentId?: string;
  root?: boolean;
  traceId?: string;
  attributes?: ChatProgressParams;
  cancelled?: boolean;
  product?: string;
  displayName?: string;
}

/** 組み立てた 1 つの段階（名前を付ける前）。 */
export interface ChatProgressEventStep {
  id: string;
  kind: string;
  status: ChatProgressStepStatus;
  startedAt?: string;
  finishedAt?: string;
  detail?: string;
  params?: ChatProgressParams;
  details?: RunStepDetails;
  span?: ChatProgressEventSpan;
}

export interface ChatProgressEventsState {
  /** 対象（`target_id`）。変わったら作り直す。 */
  key: string | null;
  /** 今の試行。 */
  attempt: number;
  /** 続けて適用した最後の番号（取り直しの `since`・SSE の再開の位置）。 */
  lastSeq: number;
  /** 今の試行の終端。 */
  terminal: ChatProgressTerminalStatus | null;
  /** 段階（最初に出た順）。 */
  steps: ChatProgressEventStep[];
  /** 番号が飛んで、まだ適用していないイベント（前の番号が届いたら適用する）。 */
  pending: ChatProgressEvent[];
}

export interface ChatProgressEventsInput {
  key: string | null;
  events: readonly ChatProgressEvent[];
  /**
   * この一覧が `since` より後のイベントをすべて含む（polling の応答）。番号が飛んでいても、それより前は
   * 記録に無いとみなして進める（取り直しで埋まらない飛びで止まらない）。
   */
  since?: number;
}

export function createChatProgressEventsState(key: string | null): ChatProgressEventsState {
  return { key, attempt: 0, lastSeq: 0, terminal: null, steps: [], pending: [] };
}

function applyEvent(state: ChatProgressEventsState, event: ChatProgressEvent): ChatProgressEventsState {
  const next: ChatProgressEventsState = { ...state, lastSeq: event.seq };
  if (event.attempt < state.attempt) return next;
  if (event.attempt > state.attempt) {
    next.attempt = event.attempt;
    next.terminal = null;
    next.steps = [];
  }
  if (event.type === "terminal") return { ...next, terminal: event.status };
  if (next.terminal !== null) return next;
  const index = next.steps.findIndex((step) => step.id === event.step_id);
  const before = index >= 0 ? next.steps[index] : undefined;
  // 状態は進む方へだけ変える（戻るイベントは古い。#1358）。
  if (before && CHAT_PROGRESS_STATUS_RANK[event.status] < CHAT_PROGRESS_STATUS_RANK[before.status]) return next;
  const step: ChatProgressEventStep = {
    id: event.step_id,
    kind: event.kind ?? before?.kind ?? event.step_id,
    status: event.status,
  };
  const startedAt = event.started_at ?? before?.startedAt;
  if (startedAt) step.startedAt = startedAt;
  if (event.finished_at) step.finishedAt = event.finished_at;
  if (event.detail) step.detail = event.detail;
  if (event.params) step.params = event.params;
  if (event.details) step.details = event.details;
  if (event.name) {
    const span: ChatProgressEventSpan = { name: event.name };
    if (event.parent_id) span.parentId = event.parent_id;
    if (event.root) span.root = true;
    if (event.trace_id) span.traceId = event.trace_id;
    if (event.attributes) span.attributes = event.attributes;
    if (event.cancelled) span.cancelled = true;
    if (event.product) span.product = event.product;
    if (event.display_name) span.displayName = event.display_name;
    step.span = span;
  }
  const steps = [...next.steps];
  if (index >= 0) steps[index] = step;
  else steps.push(step);
  return { ...next, steps };
}

/**
 * イベントを段階の一覧の状態に積む（3 製品共通の規則。純粋な関数）。
 *
 * - 対象（`key`）が変わったら作り直す。別の対象のイベントは捨てる。
 * - `seq` の順に、続けて（前の番号の次から）適用する。適用済みの番号以下（古い・重複）は捨て、番号が飛んだ
 *   イベントは前の番号が届くまで取っておく（`pending`。届かなければ取り直しの `since` で埋める）。
 * - 試行（`attempt`）が増えたら一覧を作り直し、古い試行のイベントは捨てる。
 * - 段階は `step_id` で結び、最初に出た順に並べる。状態は進む方へだけ変える。終端の後の段階のイベントは捨てる。
 *
 * 変わらなければ `state` をそのまま返す。
 */
export function reduceChatProgressEvents(
  state: ChatProgressEventsState | null,
  { key, events, since }: ChatProgressEventsInput
): ChatProgressEventsState {
  let current = state === null || state.key !== key ? createChatProgressEventsState(key) : state;
  if (key === null) return current;
  const incoming = events.filter((event) => event.target_id === key);
  if (incoming.length === 0 && (since === undefined || current.pending.length === 0)) return current;
  const bySeq = new Map<number, ChatProgressEvent>();
  for (const event of [...current.pending, ...incoming]) {
    if (event.seq > current.lastSeq && !bySeq.has(event.seq)) bySeq.set(event.seq, event);
  }
  const ordered = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  let next = current;
  const held: ChatProgressEvent[] = [];
  for (const event of ordered) {
    const contiguous = event.seq === next.lastSeq + 1;
    // polling の応答は `since` の後をすべて含む（その前の飛びは記録に無い）。
    const covered = since !== undefined && since >= next.lastSeq && incoming.includes(event);
    if (contiguous || (covered && held.length === 0)) next = applyEvent(next, event);
    else held.push(event);
  }
  const samePending =
    held.length === current.pending.length && held.every((event, index) => event === current.pending[index]);
  if (next === current && samePending) return current;
  current = { ...next, pending: samePending ? current.pending : held };
  return current;
}

// ---------------------------------------------------------------------------
// 段階の定義（製品が持つ: id・順序・i18n の名前）
// ---------------------------------------------------------------------------

/** 名前の状態（実行中「〜しています」・完了「〜しました」・失敗「〜できませんでした」・未実行は名詞）。 */
export type ChatProgressLabelState = "running" | "done" | "failed" | "idle";

export function chatProgressLabelState(status: ChatProgressStepStatus): ChatProgressLabelState {
  return status === "running" || status === "done" || status === "failed" ? status : "idle";
}

/** 1 つの種類（`kind`）の段階の定義。 */
export interface ChatProgressStepDefinition {
  /** 状態と値（`params`）から名前を作る（製品の i18n）。 */
  label: (status: ChatProgressStepStatus, params: ChatProgressParams) => string;
  /** 状態と値から補足を作る。省略時・undefined はイベントの `detail`。 */
  detail?: (status: ChatProgressStepStatus, params: ChatProgressParams) => string | undefined;
  /** 値から右の列の計測値（`ChatProgressStep.meta`。例:「LLM 1 回」）を作る。 */
  meta?: (status: ChatProgressStepStatus, params: ChatProgressParams) => string | undefined;
  /** 値から入れ子の深さ（`ChatProgressStep.level`）を作る。 */
  level?: (params: ChatProgressParams) => number | undefined;
  /**
   * 段の詳細（`ChatProgressStep.details`）を作る。省略時はイベントの `details` をそのまま使う（#2294）。
   * 見出しを製品の i18n で付け直すときなどに使う。
   */
  details?: (
    status: ChatProgressStepStatus,
    params: ChatProgressParams,
    details: RunStepDetails | undefined
  ) => RunStepDetails | undefined;
}

/** 製品の段階の定義（`kind` → 定義）。並びは backend の記録の順（最初に出た順）。 */
export type ChatProgressStepDefinitions = Readonly<Record<string, ChatProgressStepDefinition>>;

const EMPTY_PARAMS: ChatProgressParams = Object.freeze({});

/** 名前の付け方の設定（span の段の名前・root の扱い。#3226）。 */
export interface ChatProgressLabelOptions {
  /** 技術の span の名前・停止の文言（既定は共通の日本語）。 */
  traceLabels?: TraceDetailsLabels;
}

interface OrderedStep {
  step: ChatProgressEventStep;
  level: number;
  /** 製品の境目の行（見える親の行・画面と違う製品の span）の製品。 */
  product?: string;
}

/**
 * span の段を木の順（親の直後に子。兄弟は始まった順）に並べ、入れ子の深さを付ける（#3226）。実行の全体の span（root）と
 * MCP の手続きの span（`initialize`・通知・`tools/list`。#3272）は行にせず、子を親の位置に上げる。親の無い段（親が記録の
 * 上限を超えた・別の区間）も最上位に出す。
 */
function orderSpanSteps(steps: readonly ChatProgressEventStep[], viewer: string | undefined): OrderedStep[] {
  const ids = new Set(steps.map((step) => step.id));
  const children = new Map<string, ChatProgressEventStep[]>();
  const tops: ChatProgressEventStep[] = [];
  for (const step of steps) {
    const parent = step.span?.parentId;
    if (parent && ids.has(parent) && parent !== step.id) {
      const list = children.get(parent) ?? [];
      list.push(step);
      children.set(parent, list);
    } else {
      tops.push(step);
    }
  }
  const ordered: OrderedStep[] = [];
  const visited = new Set<string>();
  // 製品の境目: 見える親の行（最上位は画面の製品。無ければ最初の span の製品）と製品が違う行に製品名を添える（#3272）。
  const base = viewer ?? tops[0]?.span?.product;
  const visit = (step: ChatProgressEventStep, level: number, inherited: string | undefined) => {
    if (visited.has(step.id)) return;
    visited.add(step.id);
    const hidden = step.span?.root === true || isMcpProtocolSpan(step.span?.attributes ?? {});
    const product = step.span?.product;
    if (!hidden) ordered.push({ step, level, ...(product && inherited && product !== inherited ? { product } : {}) });
    const next = hidden ? inherited : (product ?? inherited);
    for (const child of children.get(step.id) ?? []) visit(child, hidden ? level : level + 1, next);
  };
  for (const step of tops) visit(step, 0, base);
  // cycle の段も捨てずに最上位に出す。
  for (const step of steps) if (!visited.has(step.id)) visit(step, 0, base);
  return ordered;
}


function mergeDetails(product: RunStepDetails | undefined, technical: RunStepDetailField[]): RunStepDetails | undefined {
  if (!technical.length) return product;
  return { ...(product ?? {}), fields: [...(product?.fields ?? []), ...technical] };
}

/**
 * 組み立てた段階に名前を付ける（未知の種類は id をそのまま出す。span の段は共通の規則で付ける）。
 *
 * 名前は、画面の製品の span なら製品の工程の定義、次に span を書いた製品が付けた名前（`display_name`）、最後に共通の
 * 規則（技術の span の名前）。別の製品が書いた span（`traceLabels.product` と違う製品）は画面の製品の定義を使わない
 * （#3272。種類の名前が製品の間で重なっても取り違えない）。製品の境目の行には製品名（`product`）を添える。
 */
export function labelChatProgressSteps(
  steps: readonly ChatProgressEventStep[],
  definitions: ChatProgressStepDefinitions,
  options: ChatProgressLabelOptions = {}
): ChatProgressStep[] {
  const traceLabels = options.traceLabels ?? DEFAULT_TRACE_DETAILS_LABELS;
  const viewer = traceLabels.product;
  const spanMode = steps.some((step) => step.span);
  const ordered: OrderedStep[] = spanMode ? orderSpanSteps(steps, viewer) : steps.map((step) => ({ step, level: 0 }));
  return ordered.map(({ step, level: treeLevel, product }) => {
    const span = step.span;
    const foreign = Boolean(viewer && span?.product && span.product !== viewer);
    const definition = !foreign && Object.prototype.hasOwnProperty.call(definitions, step.kind) ? definitions[step.kind] : undefined;
    const values = step.params ?? EMPTY_PARAMS;
    const spanName = span
      ? traceSpanDisplayName(
          {
            operationName: span.name,
            status: span.cancelled ? "cancelled" : step.status === "running" || step.status === "pending" ? "done" : step.status,
            root: span.root === true,
            attributes: span.attributes ?? EMPTY_PARAMS,
          },
          traceLabels
        )
      : undefined;
    const result: ChatProgressStep = {
      id: step.id,
      label: definition ? definition.label(step.status, values) : (span?.displayName ?? spanName ?? step.id),
      status: step.status,
    };
    if (span) result.operationName = span.name;
    if (product) result.product = traceLabels.productName(product);
    // 状態で変わらない名詞の名前（「SQL の生成」）。失敗のカードの止まった段に使う（#2342）。
    const name = definition ? definition.label("pending", values) : undefined;
    if (name && name !== result.label) result.name = name;
    if (step.startedAt) result.startedAt = step.startedAt;
    if (step.finishedAt) result.finishedAt = step.finishedAt;
    const detail = definition?.detail?.(step.status, values) ?? step.detail ?? (span?.cancelled ? traceLabels.cancelled : undefined);
    if (detail) result.detail = detail;
    const meta = definition?.meta?.(step.status, values);
    if (meta) result.meta = meta;
    const level = spanMode ? treeLevel : definition?.level?.(values);
    if (level) result.level = level;
    if (span?.parentId) result.parentId = span.parentId;
    const productDetails = definition?.details ? definition.details(step.status, values, step.details) : step.details;
    const details = span
      ? mergeDetails(productDetails, [
          ...traceSpanFields(undefined, span.name, span.attributes ?? EMPTY_PARAMS, traceLabels),
          ...(span.parentId ? [{ label: "Parent span ID", value: span.parentId, mono: true, technical: true }] : []),
          ...(span.product ? [{ label: "Product", value: span.product, mono: true, technical: true }] : []),
        ])
      : productDetails;
    if (details) result.details = details;
    return result;
  });
}

/** 段が span の記録の trace id（実行の全体の span。無ければ最初の span）。 */
export function chatProgressTraceIdOf(events: readonly ChatProgressEvent[]): string | undefined {
  const spans = events.filter((event): event is ChatProgressStepEvent => event.type === "step" && Boolean(event.trace_id));
  return (spans.find((event) => event.root) ?? spans[0])?.trace_id;
}

/** 段が span の記録か（#3226。古い形式の段だけの記録と分ける）。 */
export function chatProgressHasSpans(events: readonly ChatProgressEvent[]): boolean {
  return events.some((event) => event.type === "step" && Boolean(event.name));
}

/** 保存済みのイベント（完了した回答など）を、名前を付けた段階の一覧にする。 */
export function chatProgressStepsFromEvents(
  events: readonly ChatProgressEvent[],
  definitions: ChatProgressStepDefinitions,
  key: string | null = events[0]?.target_id ?? null,
  options: ChatProgressLabelOptions = {}
): ChatProgressStep[] {
  const state = reduceChatProgressEvents(null, { key, events, since: 0 });
  return labelChatProgressSteps(state.steps, definitions, options);
}

/** 保存済みのイベントの終端（無ければ null）。 */
export function chatProgressTerminalOf(
  events: readonly ChatProgressEvent[],
  key: string | null = events[0]?.target_id ?? null
): ChatProgressTerminalStatus | null {
  return reduceChatProgressEvents(null, { key, events, since: 0 }).terminal;
}
