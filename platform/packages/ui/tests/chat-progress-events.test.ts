import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  CHAT_PROGRESS_SCHEMA_VERSION,
  CHAT_PROGRESS_SSE_EVENT,
  CHAT_PROGRESS_SSE_HEARTBEAT_EVENT,
  CHAT_PROGRESS_STEP_STATUSES,
  CHAT_PROGRESS_TERMINAL_STATUSES,
  chatProgressHasSpans,
  chatProgressLabelState,
  chatProgressStepsFromEvents,
  chatProgressTerminalOf,
  chatProgressTraceIdOf,
  parseChatProgressEvent,
  parseChatProgressEvents,
  parseChatProgressPage,
  parseChatProgressStepDetails,
  reduceChatProgressEvents,
  type ChatProgressEvent,
  type ChatProgressEventsState,
  type ChatProgressStepDefinitions,
  type ChatProgressStepEvent,
  type ChatProgressStepStatus,
  type ChatProgressTerminalEvent,
  createTraceDetailsLabels,
  TRACE_DETAILS_JA,
} from "../src";

// #1359: チャットの処理の段階のイベント（3 製品共通の契約）と、イベントを段階の一覧にまとめる reducer。

const here = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(
  readFileSync(resolve(here, "../../../contracts/chat-progress/chat-progress-events.json"), "utf-8")
) as {
  schema_version: number;
  absent_fields: string;
  statuses: string[];
  terminal_statuses: string[];
  sse: { event: string; heartbeat_event: string };
  event: { oneOf?: { $ref: string }[]; $defs: Record<string, { properties: Record<string, unknown>; required?: string[] }> };
  page: { properties: Record<string, unknown>; required?: string[] };
};

function step(
  seq: number,
  stepId: string,
  status: ChatProgressStepStatus,
  extra: Partial<ChatProgressStepEvent> = {}
): ChatProgressStepEvent {
  return {
    schema_version: 1,
    type: "step",
    seq,
    target_id: "job-1",
    attempt: 0,
    emitted_at: "2026-10-09T00:00:00.000Z",
    step_id: stepId,
    status,
    ...extra,
  };
}

function terminal(seq: number, status: ChatProgressTerminalEvent["status"] = "done", attempt = 0): ChatProgressTerminalEvent {
  return {
    schema_version: 1,
    type: "terminal",
    seq,
    target_id: "job-1",
    attempt,
    emitted_at: "2026-10-09T00:00:09.000Z",
    status,
  };
}

function reduce(events: ChatProgressEvent[], state: ChatProgressEventsState | null = null, since?: number) {
  return reduceChatProgressEvents(state, { key: "job-1", events, since });
}

const statuses = (state: ChatProgressEventsState) => Object.fromEntries(state.steps.map((s) => [s.id, s.status]));

describe("契約（platform/contracts/chat-progress）", () => {
  it("状態・終端・SSE のイベントの名前・版が backend の契約と同じ", () => {
    expect(contract.schema_version).toBe(CHAT_PROGRESS_SCHEMA_VERSION);
    // 値の無い任意の項目は省く（null を送らない。parse は null も無いものとして扱う）。
    expect(contract.absent_fields).toBe("omitted");
    expect(contract.statuses).toEqual([...CHAT_PROGRESS_STEP_STATUSES]);
    expect(contract.terminal_statuses).toEqual([...CHAT_PROGRESS_TERMINAL_STATUSES]);
    expect(contract.sse.event).toBe(CHAT_PROGRESS_SSE_EVENT);
    expect(contract.sse.heartbeat_event).toBe(CHAT_PROGRESS_SSE_HEARTBEAT_EVENT);
  });

  it("イベントと polling の応答の項目が型と同じ", () => {
    const defs = contract.event.$defs;
    const stepFields: (keyof ChatProgressStepEvent)[] = [
      "schema_version",
      "seq",
      "target_id",
      "attempt",
      "emitted_at",
      "type",
      "step_id",
      "kind",
      "status",
      "started_at",
      "finished_at",
      "detail",
      "params",
      "details",
      "name",
      "parent_id",
      "root",
      "trace_id",
      "attributes",
      "cancelled",
      "product",
      "display_name",
    ];
    expect(Object.keys(defs.ChatProgressStepEvent.properties).sort()).toEqual([...stepFields].sort());
    // 段の詳細（#2294）の形は共有 UI の `RunStepDetails` と同じ。
    expect(Object.keys(defs.ChatProgressStepDetails.properties).sort()).toEqual(["call", "code", "fields", "lines", "truncated"]);
    expect(Object.keys(defs.ChatProgressDetailField.properties).sort()).toEqual(["label", "mono", "value"]);
    expect(Object.keys(defs.ChatProgressDetailCode.properties).sort()).toEqual(["label", "language", "text"]);
    const terminalFields: (keyof ChatProgressTerminalEvent)[] = [
      "schema_version",
      "seq",
      "target_id",
      "attempt",
      "emitted_at",
      "type",
      "status",
    ];
    expect(Object.keys(defs.ChatProgressTerminalEvent.properties).sort()).toEqual([...terminalFields].sort());
    expect(Object.keys(contract.page.properties).sort()).toEqual(["attempt", "events", "last_seq", "target_id", "terminal"]);
  });
});

describe("parseChatProgressEvent", () => {
  it("形の違うイベントは捨てる（SSE・API の応答は未検証の入力）", () => {
    expect(parseChatProgressEvent(step(1, "a", "running"))).toEqual(step(1, "a", "running"));
    expect(parseChatProgressEvent({ ...step(1, "a", "running"), status: "unknown" })).toBeNull();
    expect(parseChatProgressEvent({ ...step(0, "a", "running") })).toBeNull();
    expect(parseChatProgressEvent({ ...step(1, "", "running") })).toBeNull();
    expect(parseChatProgressEvent({ ...terminal(2), status: "skipped" })).toBeNull();
    expect(parseChatProgressEvent("x")).toBeNull();
    expect(parseChatProgressEvents([step(1, "a", "running"), null, { type: "x" }])).toHaveLength(1);
  });

  it("params は文字列・数・真偽の値だけを残す", () => {
    const event = parseChatProgressEvent({ ...step(1, "a", "done"), params: { tool: "rag_search", count: 3, nested: { x: 1 } } });
    expect(event && event.type === "step" ? event.params : null).toEqual({ tool: "rag_search", count: 3 });
  });

  it("polling の応答を検証する", () => {
    expect(parseChatProgressPage({ target_id: "job-1", attempt: 0, events: [step(1, "a", "running")], last_seq: 1, terminal: false })).toEqual({
      target_id: "job-1",
      attempt: 0,
      events: [step(1, "a", "running")],
      last_seq: 1,
      terminal: false,
    });
    expect(parseChatProgressPage({ target_id: "job-1", events: [] })).toBeNull();
  });
});

describe("reduceChatProgressEvents", () => {
  it("seq の順に積み、段階は最初に出た順に並べる", () => {
    const state = reduce([step(1, "queue", "pending"), step(2, "generate", "pending"), step(3, "queue", "running")]);
    expect(state.steps.map((s) => s.id)).toEqual(["queue", "generate"]);
    expect(statuses(state)).toEqual({ queue: "running", generate: "pending" });
    expect(state.lastSeq).toBe(3);
  });

  it("古い・重複のイベントは捨て、変わらなければ同じ状態を返す", () => {
    const first = reduce([step(1, "a", "running"), step(2, "a", "done")]);
    const again = reduce([step(1, "a", "running"), step(2, "a", "done")], first);
    expect(again).toBe(first);
    expect(statuses(reduce([step(1, "a", "pending")], first))).toEqual({ a: "done" });
  });

  it("順序の入れ替わったイベントは、前の番号が届いてから適用する", () => {
    const early = reduce([step(1, "a", "running"), step(3, "b", "running")]);
    expect(statuses(early)).toEqual({ a: "running" });
    expect(early.pending.map((e) => e.seq)).toEqual([3]);
    expect(early.lastSeq).toBe(1);
    const filled = reduce([step(2, "a", "done")], early);
    expect(statuses(filled)).toEqual({ a: "done", b: "running" });
    expect(filled.pending).toEqual([]);
    expect(filled.lastSeq).toBe(3);
  });

  it("polling の応答（since の後をすべて含む）は、記録に無い飛びを越えて進める", () => {
    const early = reduce([step(1, "a", "running"), step(4, "c", "running")]);
    const page = reduce([step(3, "b", "done"), step(4, "c", "running")], early, 1);
    expect(statuses(page)).toEqual({ a: "running", b: "done", c: "running" });
    expect(page.lastSeq).toBe(4);
    expect(page.pending).toEqual([]);
  });

  it("状態は進む方へだけ変え、同じ段階の間の確定（完了 → 失敗）は受け付ける", () => {
    const state = reduce([step(1, "a", "done"), step(2, "a", "running"), step(3, "a", "failed")]);
    expect(statuses(state)).toEqual({ a: "failed" });
  });

  it("開始の時刻は後のイベントに無ければ前の値を残す", () => {
    const state = reduce([
      step(1, "a", "running", { started_at: "2026-10-09T00:00:01.000Z" }),
      step(2, "a", "done", { finished_at: "2026-10-09T00:00:02.000Z", detail: "3 件" }),
    ]);
    expect(state.steps[0]).toMatchObject({ startedAt: "2026-10-09T00:00:01.000Z", finishedAt: "2026-10-09T00:00:02.000Z", detail: "3 件" });
  });

  it("終端の後の、同じ試行の段階のイベントは捨てる", () => {
    const state = reduce([step(1, "a", "running"), step(2, "a", "done"), terminal(3), step(4, "b", "running")]);
    expect(state.terminal).toBe("done");
    expect(statuses(state)).toEqual({ a: "done" });
  });

  it("試行が増えたら作り直し、古い試行のイベントは捨てる", () => {
    const first = reduce([step(1, "a", "done"), step(2, "b", "running")]);
    const retried = reduce([step(3, "a", "running", { attempt: 1 }), step(4, "b", "done", { attempt: 0 })], first);
    expect(retried.attempt).toBe(1);
    expect(statuses(retried)).toEqual({ a: "running" });
  });

  it("対象が変わったら作り直し、別の対象のイベントは捨てる", () => {
    const first = reduce([step(1, "a", "done")]);
    const other = reduceChatProgressEvents(first, { key: "job-2", events: [step(2, "a", "running")] });
    expect(other.key).toBe("job-2");
    expect(other.steps).toEqual([]);
    expect(reduceChatProgressEvents(null, { key: null, events: [step(1, "a", "running")] }).steps).toEqual([]);
  });
});

describe("段階の名前", () => {
  const definitions: ChatProgressStepDefinitions = {
    tool: {
      label: (status, params) => `${String(params.tool)} を${chatProgressLabelState(status)}`,
      detail: (status, params) => (status === "done" && params.count !== undefined ? `${String(params.count)} 件` : undefined),
    },
  };

  it("定義の種類（kind）と値（params）から名前と補足を付け、未知の種類は id を出す", () => {
    const events = [
      step(1, "tool:rag_search", "done", { kind: "tool", params: { tool: "rag_search", count: 2 } }),
      step(2, "unknown", "running", { detail: "補足" }),
      terminal(3, "failed"),
    ];
    expect(chatProgressStepsFromEvents(events, definitions)).toEqual([
      // 状態で変わらない名詞の名前（未実行の名前）も付ける（失敗のカードの止まった段。#2342）。
      { id: "tool:rag_search", label: "rag_search をdone", name: "rag_search をidle", status: "done", detail: "2 件" },
      { id: "unknown", label: "unknown", status: "running", detail: "補足" },
    ]);
    expect(chatProgressTerminalOf(events)).toBe("failed");
    expect(chatProgressTerminalOf([])).toBeNull();
  });
});

describe("段の詳細（#2294）", () => {
  it("検証で形の違う要素を捨て、中身が無ければ details を付けない", () => {
    expect(
      parseChatProgressStepDetails({
        fields: [{ label: "結果", value: "安全" }, { label: "", value: "x" }, { label: "表", value: "T1", mono: true }, "x"],
        lines: ["行", 3, ""],
        code: [{ label: "SQL", language: "sql", text: "SELECT 1" }, { language: "bash", text: "ls" }, { text: "" }],
        truncated: true,
      })
    ).toEqual({
      fields: [
        { label: "結果", value: "安全" },
        { label: "表", value: "T1", mono: true },
      ],
      lines: ["行"],
      code: [{ label: "SQL", language: "sql", text: "SELECT 1" }, { text: "ls" }],
      truncated: true,
    });
    expect(parseChatProgressStepDetails({ fields: [], truncated: true })).toBeUndefined();
    expect(parseChatProgressStepDetails("x")).toBeUndefined();
    const event = parseChatProgressEvent({ ...step(1, "a", "done"), details: { lines: [] } });
    expect(event && "details" in event).toBe(false);
  });

  it("イベントの詳細を段に積み、定義の details で作り直せる", () => {
    const details = { lines: ["選んだ条: 第 1 条"] };
    const events = [
      step(1, "s1", "running", { kind: "tool", params: { tool: "a" } }),
      step(2, "s1", "done", { kind: "tool", params: { tool: "a" }, details }),
    ];
    expect(reduce(events).steps[0].details).toEqual(details);
    const plain: ChatProgressStepDefinitions = { tool: { label: () => "ツール" } };
    expect(chatProgressStepsFromEvents(events, plain)[0].details).toEqual(details);
    const relabeled: ChatProgressStepDefinitions = {
      tool: { label: () => "ツール", details: (_status, _params, value) => (value ? { lines: [...(value.lines ?? []), "追加"] } : undefined) },
    };
    expect(chatProgressStepsFromEvents(events, relabeled)[0].details).toEqual({ lines: ["選んだ条: 第 1 条", "追加"] });
  });
});

// #3226: 処理の経過は trace（span の木）の投影。段のイベントは span の開始・変更・終了。
describe("span の段（#3226）", () => {
  const at = (seconds: number) => new Date(Date.UTC(2026, 0, 1, 0, 0, seconds)).toISOString();
  const trace = "a".repeat(32);
  let seq = 0;
  const span = (id: string, parent: string | undefined, name: string, status: ChatProgressStepStatus, extra: Partial<ChatProgressStepEvent> = {}): ChatProgressStepEvent => ({
    schema_version: 1, seq: ++seq, target_id: "t", attempt: 0, emitted_at: at(seq), type: "step",
    step_id: id, status, name, trace_id: trace, started_at: at(0), ...(parent ? { parent_id: parent } : {}),
    ...(status === "running" ? {} : { finished_at: at(5) }), ...extra,
  });
  const definitions: ChatProgressStepDefinitions = {
    answer_step: { label: (status, params) => `工程 ${String(params.name)}${status === "running" ? "（実行中）" : ""}`, meta: (_s, params) => (params.llm_calls ? `LLM ${params.llm_calls} 回` : undefined) },
  };

  it("親子から木の順に並べ、root は行にせず、名前は定義と共通の規則で付ける", () => {
    seq = 0;
    const events: ChatProgressEvent[] = [
      span("r", undefined, "rag.answer", "running", { root: true }),
      span("a", "r", "rag.engine.stage", "running", { kind: "answer_step", params: { name: "検索" } }),
      span("b", "r", "rag.history_rewrite", "done", { attributes: { "pr.stage": "history_rewrite" } }),
      span("c", "a", "gen_ai.embeddings", "done", { attributes: { "gen_ai.request.model": "embed-x" } }),
      span("a", "r", "rag.engine.stage", "done", { kind: "answer_step", params: { name: "検索", llm_calls: 1 } }),
      span("d", "a", "mcp.request", "skipped", { cancelled: true }),
    ];
    const steps = chatProgressStepsFromEvents(events, definitions, "t");
    expect(steps.map((step) => [step.label, step.level ?? 0])).toEqual([
      ["工程 検索", 0],
      ["テキストの埋め込みを作る · embed-x", 1],
      ["MCP の呼び出し", 1],
      ["処理を実行する", 0],
    ]);
    expect(steps[0].meta).toBe("LLM 1 回");
    expect(steps[2].detail).toBe("停止");
    // 開いた詳細に元の span の名前と技術の値を残す。
    expect(steps[1].details?.fields).toContainEqual({ label: "Span name", value: "gen_ai.embeddings", mono: true, technical: true });
    expect(chatProgressTraceIdOf(events)).toBe(trace);
    expect(chatProgressHasSpans(events)).toBe(true);
  });

  // #3272: 1 回のツールの呼び出しの MCP の手続き（initialize・通知・tools/list）は行にせず、tools/call だけを出す。
  it("MCP の手続きの span は行にせず、tools/call の行に呼んだツールを添える", () => {
    seq = 0;
    const events: ChatProgressEvent[] = [
      span("r", undefined, "agent.execute", "done", { root: true }),
      span("t", "r", "gen_ai.execute_tool", "done", { attributes: { "gen_ai.tool.name": "tool_a" } }),
      span("i", "t", "mcp.request", "done", { attributes: { "mcp.method.name": "initialize" } }),
      span("n", "t", "mcp.request", "done", { attributes: { "mcp.method.name": "notifications/initialized" } }),
      span("l", "t", "mcp.request", "done", { attributes: { "mcp.method.name": "tools/list" } }),
      span("c", "t", "mcp.request", "done", { attributes: { "mcp.method.name": "tools/call", "gen_ai.tool.name": "tool_a" } }),
      span("s", "c", "mcp.server", "done", { root: true, attributes: { "mcp.method.name": "tools/call" } }),
      span("g", "s", "gen_ai.chat", "done", { attributes: { "gen_ai.request.model": "m" } }),
      // method の無い古い記録は今までどおり出す。
      span("o", "t", "mcp.request", "done"),
    ];
    const steps = chatProgressStepsFromEvents(events, {}, "t");
    expect(steps.map((step) => [step.label, step.level ?? 0])).toEqual([
      ["ツールの実行 · tool_a", 0],
      ["MCP の呼び出し · tool_a", 1],
      ["モデルを呼び出す（用途の記録なし）", 2],
      ["MCP の呼び出し", 1],
    ]);
    expect(steps[1].details?.fields).toContainEqual({ label: "mcp.method.name", value: "tools/call", mono: true, technical: true });
  });

  // #3272: 別の製品が書いた span は、画面の製品の工程の定義で訳さずに、書いた製品が付けた名前を出し、製品の境目に製品名を添える。
  it("別の製品の span は書いた製品の名前（display_name）で出し、製品の境目の行に製品名を添える", () => {
    seq = 0;
    const agentLabels = createTraceDetailsLabels((key) => TRACE_DETAILS_JA[key], { product: "agent" });
    const own: ChatProgressStepDefinitions = {
      ...definitions,
      tool: { label: () => "ツールの実行（画面の定義）" },
    };
    const events: ChatProgressEvent[] = [
      span("r", undefined, "agent.execute", "done", { root: true, product: "agent" }),
      span("t", "r", "gen_ai.execute_tool", "done", { kind: "tool", product: "agent" }),
      span("c", "t", "mcp.request", "done", { product: "agent", attributes: { "mcp.method.name": "tools/call", "gen_ai.tool.name": "tool_a" } }),
      span("s", "c", "mcp.server", "done", { root: true, product: "rag" }),
      span("o", "s", "rag.search", "done", { root: true, product: "rag" }),
      // 画面（Agent）の定義に同じ種類があっても、別の製品の span には使わない。
      span("a", "o", "rag.engine.stage", "done", { kind: "answer_step", product: "rag", params: { name: "工程 A" }, display_name: "表示の工程 A" }),
      span("e", "a", "gen_ai.embeddings", "done", { product: "rag", attributes: { "gen_ai.request.model": "embed-x" } }),
      span("b", "o", "rag.retrieval", "done", { product: "rag", display_name: "表示の段 B", attributes: { "pr.stage": "retrieval" } }),
    ];
    const steps = chatProgressStepsFromEvents(events, own, "t", { traceLabels: agentLabels });
    expect(steps.map((step) => [step.label, step.level ?? 0, step.product ?? null])).toEqual([
      ["ツールの実行（画面の定義）", 0, null],
      ["MCP の呼び出し · tool_a", 1, null],
      ["表示の工程 A", 2, "RAG"],
      ["テキストの埋め込みを作る · embed-x", 3, null],
      ["表示の段 B", 2, "RAG"],
    ]);
    expect(steps[2].meta).toBeUndefined();
    expect(steps[2].details?.fields).toContainEqual({ label: "Product", value: "rag", mono: true, technical: true });
    // 画面の製品の span は製品の定義で名前を付け、製品名を添えない。
    const ragLabels = createTraceDetailsLabels((key) => TRACE_DETAILS_JA[key], { product: "rag" });
    const inRag = chatProgressStepsFromEvents(events.slice(3), own, "t", { traceLabels: ragLabels });
    expect(inRag.map((step) => [step.label, step.product ?? null])).toEqual([
      ["工程 工程 A", null],
      ["テキストの埋め込みを作る · embed-x", null],
      ["表示の段 B", null],
    ]);
    // 製品の無い古い記録は今までどおり（定義で名前を付け、製品名を出さない）。
    const legacy = events.map((event) => ({ ...event, product: undefined }) as ChatProgressEvent);
    expect(chatProgressStepsFromEvents(legacy, own, "t", { traceLabels: agentLabels }).map((step) => step.product ?? null)).toEqual([null, null, null, null, null]);
    expect(parseChatProgressEvent({ ...events[3], product: "Not Valid" })).not.toHaveProperty("product");
  });

  it("実行中の span は実行中の行として出し、古い形式の段だけの記録は span の記録としない", () => {
    seq = 0;
    const running = chatProgressStepsFromEvents([span("r", undefined, "op", "running", { root: true }), span("x", "r", "gen_ai.chat", "running", { attributes: { "gen_ai.request.model": "m" } })], {}, "t");
    expect(running).toMatchObject([{ label: "モデルを呼び出す（用途の記録なし）", status: "running" }]);
    const legacy: ChatProgressEvent[] = [{ schema_version: 1, seq: 1, target_id: "t", attempt: 0, emitted_at: at(1), type: "step", step_id: "s1", status: "done" }];
    expect(chatProgressHasSpans(legacy)).toBe(false);
  });
});


describe("深い木の実際の親 ID（#3790）", () => {
  it("複数 request の SDK を各親に結び、親不在や親なしを近接行へ結び直さない", () => {
    const events: ChatProgressEvent[] = [step(1, "root", "done", { name: "agent.execute", root: true })];
    let parent = "root";
    for (let i = 0; i < 6; i++) {
      const id = `stage-${i}`;
      events.push(step(events.length + 1, id, "done", { name: "stage", parent_id: parent }));
      parent = id;
    }
    for (const id of ["r1", "r2", "cache"]) {
      events.push(step(events.length + 1, id, "done", { name: "rerank.request", parent_id: parent }));
      if (id !== "cache") events.push(step(events.length + 1, `${id}-sdk`, "done", { name: "gen_ai.rerank", parent_id: id }));
    }
    events.push(step(events.length + 1, "orphan", "done", { name: "gen_ai.rerank", parent_id: "missing" }));
    events.push(step(events.length + 1, "no-parent", "done", { name: "gen_ai.rerank" }));
    const rows = chatProgressStepsFromEvents(events, {}, "job-1");
    expect(rows.find(row => row.id === "r1-sdk")).toMatchObject({ level: 7, parentId: "r1" });
    expect(rows.find(row => row.id === "r2-sdk")).toMatchObject({ level: 7, parentId: "r2" });
    expect(rows.find(row => row.id === "cache")).toMatchObject({ level: 6, parentId: parent });
    expect(rows.find(row => row.id === "orphan")).toMatchObject({ parentId: "missing" });
    expect(rows.find(row => row.id === "orphan")?.level ?? 0).toBe(0);
    expect(rows.find(row => row.id === "no-parent")?.parentId).toBeUndefined();
    expect(rows.find(row => row.id === "no-parent")?.level ?? 0).toBe(0);
    expect(rows).toHaveLength(13);
  });
});

it("呼び出しの入力・結果・Schema と閲覧制限を保持する", () => {
  const content = { status: "recorded", text: "synthetic-input" };
  const call = { input: content, output: { status: "withheld", text: "must-not-show" }, schema: { status: "truncated", text: "{}" } };
  const parsed = parseChatProgressStepDetails({ call });
  expect(parsed?.call?.input).toEqual(content);
  expect(parsed?.call?.output).toEqual({ status: "withheld", text: "" });
  expect(parsed?.call?.schema.status).toBe("truncated");
  expect(parseChatProgressStepDetails({ fields: [{ label: "旧記録", value: "値" }] })?.call).toBeUndefined();
});
