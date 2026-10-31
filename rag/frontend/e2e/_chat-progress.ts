import type { Page } from "@playwright/test";

/**
 * 処理の経過のイベント（3 製品共通の契約。#1359）を spec の mock で作る。
 *
 * 処理の経過は回答の trace（span の木）の投影で、span は共通の span の表にある（#3226 の案 A）。画面は記録ごとの
 * trace の API（`.../messages/{回答の id}/trace`・`/history/runs/{id}/trace`・`/search/runs/{id}/trace`）で、表の
 * span を段のイベントにした snapshot を読み直す。mock は span の開始（実行中）・終了（完了・失敗・スキップ）を番号
 * （`seq`）つきのイベントで作り、trace の API の応答にする。`step_id` は span の id、親子は `parent_id`、実行の全体は
 * `root`。工程の名前（文言）は入れない（画面が工程の種類と工程名から i18n で付ける）。
 *
 * - 時間をおいて流す配信の mock（init script の fetch）は、chunk に `traceSse` の印を入れ、`installTraceRecorder` が
 *   その印を配信から外して記録する（送った時点までの span を trace の API が返す）。
 * - 一度に返す mock は `mockTraceApi` で記録ごとの snapshot を返す。
 */

export type ChatStepStatus = "pending" | "running" | "done" | "failed" | "skipped";
export type ChatStepParams = Record<string, string | number | boolean>;

/** mock の trace id（span のイベントの `trace_id`。「処理の詳細」の Trace ID に出る）。 */
export const SPAN_TRACE_ID = "0123456789abcdef0123456789abcdef";

export interface ChatProgressStepEventJson {
  schema_version: 1;
  seq: number;
  target_id: string;
  attempt: number;
  emitted_at: string;
  type: "step";
  step_id: string;
  kind?: string;
  status: ChatStepStatus;
  started_at?: string;
  finished_at?: string;
  params?: ChatStepParams;
  details?: Record<string, unknown>;
  name: string;
  parent_id?: string;
  root?: boolean;
  trace_id: string;
  attributes?: ChatStepParams;
  cancelled?: boolean;
}

export interface ChatProgressTerminalEventJson {
  schema_version: 1;
  seq: number;
  target_id: string;
  attempt: number;
  emitted_at: string;
  type: "terminal";
  status: "done" | "failed" | "cancelled";
}

export type ChatProgressEventJson = ChatProgressStepEventJson | ChatProgressTerminalEventJson;

/** span の形（名前・工程の種類と値・安全な属性・親の key）。 */
export interface SpanSpec {
  name: string;
  kind?: string;
  params?: ChatStepParams;
  attributes?: ChatStepParams;
  /** 親の span の key（`spanProgressRecorder` の key）。 */
  parent?: string;
  root?: boolean;
}

const PIPELINE_STEP_KINDS = new Set(["history_rewrite", "field_filter", "answer_guardrail", "answer_record_save"]);

/** RAG の span（backend の pipeline と同じ名前・種類・属性）。 */
export const ragSpan = {
  /** 実行の全体（回答は `rag.answer`、回答を作らない検索は `rag.search`）。 */
  root: (generateAnswer = true): SpanSpec => ({ name: generateAnswer ? "rag.answer" : "rag.search", root: true }),
  /** パイプラインの段（`rag.<段>`。回答フローの外の工程は種類を持つ）。 */
  stage: (stage: string, parent = "root"): SpanSpec => ({
    name: `rag.${stage}`,
    ...(PIPELINE_STEP_KINDS.has(stage) ? { kind: stage } : {}),
    attributes: { "pr.stage": stage },
    parent,
  }),
  /** 回答フローの工程（`rag.engine.stage`。種類 `answer_step`・工程名）。 */
  step: (name: string, parent = "answer"): SpanSpec => ({
    name: "rag.engine.stage",
    kind: "answer_step",
    params: { name },
    attributes: { "pr.stage": name },
    parent,
  }),
  /** モデルの呼び出しなどの技術の span。 */
  technical: (name: string, parent: string, attributes: ChatStepParams = {}): SpanSpec => ({ name, attributes, parent }),
};

interface SpanState {
  spec: SpanSpec;
  status: ChatStepStatus;
  started_at: string;
  finished_at?: string;
  params?: ChatStepParams;
  details?: Record<string, unknown>;
  cancelled?: boolean;
}

/** span の key → `step_id`（16 桁の hex。spec が test id を組み立てられるように決まった値にする）。 */
export function spanStepId(key: string): string {
  let hash = 0;
  for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `${hash.toString(16).padStart(8, "0")}${key.length.toString(16).padStart(8, "0")}`.slice(0, 16);
}

/**
 * 配信の時刻に置き換える印（`__NOW__`）で記録するときの、span の開始の時刻の印。mock は最初に見た時刻を覚え、同じ印を
 * 同じ時刻に置き換える（終了のイベントにも開始の時刻を持たせる。mock の init script が `/__START_([A-Za-z0-9-]+)__/g` で置き換える）。
 */
export function spanStartMark(key: string): string {
  return `__START_${key}__`;
}

/**
 * 1 つの回答（`targetId`）の span のイベントを記録する。`now` は時刻の文字列（配信の時刻に置き換える印でもよい）。
 * `startedAt` は span の開始の時刻（既定は開始したときの `now()`。配信の時刻の印なら `spanStartMark`）。
 */
export function spanProgressRecorder(
  targetId: string,
  now: () => string = () => new Date().toISOString(),
  startedAt: (key: string) => string = () => now()
) {
  let seq = 0;
  const spans = new Map<string, SpanState>();
  const base = () => ({ schema_version: 1 as const, seq: ++seq, target_id: targetId, attempt: 0, emitted_at: now() });

  function event(key: string, state: SpanState): ChatProgressStepEventJson {
    const { spec } = state;
    return {
      ...base(),
      type: "step",
      step_id: spanStepId(key),
      kind: spec.kind ?? spanStepId(key),
      status: state.status,
      started_at: state.started_at,
      ...(state.finished_at ? { finished_at: state.finished_at } : {}),
      ...(state.params ? { params: state.params } : {}),
      ...(state.details ? { details: state.details } : {}),
      name: spec.name,
      ...(spec.parent ? { parent_id: spanStepId(spec.parent) } : {}),
      ...(spec.root ? { root: true } : {}),
      trace_id: SPAN_TRACE_ID,
      ...(spec.attributes && Object.keys(spec.attributes).length ? { attributes: spec.attributes } : {}),
      ...(state.cancelled ? { cancelled: true } : {}),
    };
  }

  return {
    get seq() {
      return seq;
    },
    /** span を始める（実行中）。 */
    start(key: string, spec: SpanSpec): ChatProgressEventJson[] {
      const state: SpanState = {
        spec,
        status: "running",
        started_at: startedAt(key),
        ...(spec.params ? { params: spec.params } : {}),
      };
      spans.set(key, state);
      return [event(key, state)];
    },
    /** span を終える。`params` は前の値に重ねる。停止（`cancelled`）は skipped と印。 */
    finish(
      key: string,
      status: "done" | "failed" | "skipped" | "cancelled" = "done",
      extra: { params?: ChatStepParams; details?: Record<string, unknown> } = {}
    ): ChatProgressEventJson[] {
      const before = spans.get(key);
      if (!before) throw new Error(`開いていない span: ${key}`);
      const params = extra.params ? { ...(before.params ?? {}), ...extra.params } : before.params;
      const state: SpanState = {
        ...before,
        status: status === "cancelled" ? "skipped" : status,
        finished_at: now(),
        ...(params ? { params } : {}),
        ...(extra.details ? { details: extra.details } : {}),
        ...(status === "cancelled" ? { cancelled: true } : {}),
      };
      spans.set(key, state);
      return [event(key, state)];
    },
    /** 終端を記録する。 */
    terminal(status: "done" | "failed" | "cancelled"): ChatProgressEventJson[] {
      return [{ ...base(), type: "terminal", status }];
    },
  };
}

export interface SavedSpan {
  key: string;
  spec: SpanSpec;
  status?: "done" | "failed" | "skipped" | "cancelled" | "running";
  params?: ChatStepParams;
  details?: Record<string, unknown>;
  /** 開始と終了の時刻（`options.base` からの秒）。無ければ記録した時刻。 */
  at?: [number, number];
}

/**
 * 保存済みの回答の処理の経過（会話の取得の `progress`・実行の詳細の `progress_events`）。並べた順に span を始め、
 * 子から順に（後ろから）終える。`running` の span は終えない。
 */
export function savedSpanProgress(
  targetId: string,
  spans: SavedSpan[],
  options: { terminal?: "done" | "failed" | "cancelled"; base?: string } = {}
): ChatProgressEventJson[] {
  const base = Date.parse(options.base ?? new Date().toISOString());
  let current: number | null = null;
  const recorder = spanProgressRecorder(targetId, () => new Date(current ?? Date.now()).toISOString());
  const at = (span: SavedSpan, index: 0 | 1) => (span.at ? base + span.at[index] * 1000 : null);
  const events: ChatProgressEventJson[] = [];
  for (const span of spans) {
    current = at(span, 0);
    events.push(...recorder.start(span.key, span.spec));
  }
  for (const span of [...spans].reverse()) {
    if (span.status === "running") continue;
    current = at(span, 1);
    events.push(...recorder.finish(span.key, span.status ?? "done", { params: span.params, details: span.details }));
  }
  current = null;
  if (options.terminal) events.push(...recorder.terminal(options.terminal));
  return events;
}

/** よく使う回答の span の木（実行の全体・回答の段・工程・回答の安全チェック）。 */
export function answerSpans(steps: string[], options: { guardrail?: boolean } = {}): SavedSpan[] {
  return [
    { key: "root", spec: ragSpan.root() },
    { key: "answer", spec: ragSpan.stage("answer") },
    ...steps.map((name, index) => ({ key: `step-${index}`, spec: ragSpan.step(name) })),
    ...(options.guardrail ? [{ key: "guardrail", spec: ragSpan.stage("answer_guardrail") }] : []),
  ];
}

/** trace の API の応答（snapshot。`droppedSpans` は書けなかった span の数）。 */
export function traceSnapshotJson(events: ChatProgressEventJson[], droppedSpans: number | null = null) {
  return { data: { events, dropped_spans: droppedSpans }, error_messages: [], warning_messages: [] };
}

/** 記録の trace の API の path（`/trace` の前の id）。 */
const TRACE_PATH = /\/api\/(?:chat\/conversations\/[^/]+\/messages|history\/runs|search\/runs|search\/answers)\/([^/?]+)\/trace(?:\?|$)/;

export type TraceSnapshotSource =
  | ChatProgressEventJson[]
  | { events: ChatProgressEventJson[]; droppedSpans?: number | null };

/**
 * 記録ごとの trace の API を返す（id → イベント。関数なら読むたびに呼ぶ）。無い id は空の snapshot。
 * 画面の route（会話の取得など）より先に当たるよう、それらの route の後に呼ぶ（後に登録した route が先に当たる）。
 */
export async function mockTraceApi(
  page: Page,
  snapshots: Record<string, TraceSnapshotSource> | ((id: string) => TraceSnapshotSource | undefined)
): Promise<{ requests: string[] }> {
  const requests: string[] = [];
  await page.route(TRACE_PATH, async (route) => {
    const id = decodeURIComponent(TRACE_PATH.exec(route.request().url())?.[1] ?? "");
    requests.push(id);
    const source = typeof snapshots === "function" ? snapshots(id) : snapshots[id];
    const value = source === undefined ? [] : source;
    const json = Array.isArray(value)
      ? traceSnapshotJson(value)
      : traceSnapshotJson(value.events, value.droppedSpans ?? null);
    await route.fulfill({ json });
  });
  return { requests };
}

/** 配信の mock の chunk に入れる span の記録の印（`installTraceRecorder` が外して trace の API で返す）。 */
export function traceSse(events: ChatProgressEventJson[]): string {
  return events.map((event) => `event: __trace\ndata: ${JSON.stringify(event)}\n\n`).join("");
}

/**
 * 画面の fetch を包み、span の記録（`traceSse` の印）を表へ書いたものとして記録する init script を入れる。
 *
 * - 配信（`text/event-stream`）の応答は、流れる本文から印の block を外して記録する（page.route の一度に返す配信も、
 *   init script の時間をおいて流す配信も同じ）。init script の配信の mock は `window.__ragTraceRecord(text)` で直接
 *   記録してもよい。
 * - 記録のある対象の trace の API は、送った時点までの span を返す。記録の無い対象は元の fetch（page.route）へ渡す。
 *
 * 配信の mock の init script より先に呼ぶ（その mock の「元の fetch」がこの fetch になる）。
 */
export async function installTraceRecorder(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const win = window as unknown as {
      __ragTrace: Record<string, unknown[]>;
      __ragTraceRecord?: (text: string) => string;
    };
    if (win.__ragTraceRecord) return;
    win.__ragTrace = {};
    const marker = "event: __trace\n";
    const record = (text: string) =>
      text
        .split(/(?<=\n\n)/)
        .filter((block) => {
          if (!block.startsWith(marker)) return true;
          const data = block.slice(marker.length).replace(/^data: /, "").trim();
          const event = JSON.parse(data) as { target_id: string };
          (win.__ragTrace[event.target_id] ??= []).push(event);
          return false;
        })
        .join("");
    win.__ragTraceRecord = record;
    const originalFetch = window.fetch.bind(window);
    const tracePath =
      /\/api\/(?:chat\/conversations\/[^/]+\/messages|history\/runs|search\/runs|search\/answers)\/([^/?]+)\/trace(?:\?|$)/;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const match = tracePath.exec(url);
      if (match) {
        const events = win.__ragTrace[decodeURIComponent(match[1])];
        if (events !== undefined) {
          return new Response(
            JSON.stringify({ data: { events, dropped_spans: null }, error_messages: [], warning_messages: [] }),
            { status: 200, headers: { "content-type": "application/json" } }
          );
        }
        return originalFetch(input, init);
      }
      const response = await originalFetch(input, init);
      if (!response.body || !(response.headers.get("content-type") ?? "").includes("text/event-stream")) return response;
      // 配信の本文から span の記録の印を外す（block の途中で切れた分は次の chunk と合わせる）。
      const decoder = new TextDecoder();
      const encoder = new TextEncoder();
      let pending = "";
      const body = response.body.pipeThrough(
        new TransformStream<Uint8Array, Uint8Array>({
          transform(chunk, controller) {
            pending += decoder.decode(chunk, { stream: true });
            const end = pending.lastIndexOf("\n\n");
            if (end < 0) return;
            const complete = pending.slice(0, end + 2);
            pending = pending.slice(end + 2);
            const rest = record(complete);
            if (rest) controller.enqueue(encoder.encode(rest));
          },
          flush(controller) {
            if (pending) controller.enqueue(encoder.encode(record(pending)));
          },
        })
      );
      return new Response(body, { status: response.status, headers: response.headers });
    };
  });
}
