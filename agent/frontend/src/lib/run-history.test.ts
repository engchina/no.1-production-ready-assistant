import {
  chatProgressStepsFromEvents,
  chatProgressTimelineStep,
  chatProgressTraceIdOf,
  parseChatProgressEvents,
} from "@production-ready/ui";
import { describe, expect, it } from "vitest";

import type { RunState } from "@/lib/api";
import { AGENT_CHAT_PROGRESS_STEPS } from "@/lib/chat-progress";
import {
  DEFAULT_AGENT_RUN_KIND,
  backendRunExecutor,
  backendRunKind,
  backendRunStatus,
  commonRunStatus,
  runDurationMs,
  runFinishedAt,
  runStatusLabel,
  agentProgressDefinitions,
  progressRunSteps,
  runConversationHref,
  runProgressIsLegacy,
  runTimelineSteps,
  runProgressSteps,
  runToolTitles,
  toolStepDetails,
} from "@/lib/run-history";

const NOW = "2026-10-15T09:00:00.000Z";

function run(overrides: Partial<RunState> = {}): RunState {
  return {
    id: "run-1",
    goal: "売上を調べる",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  } as RunState;
}

// 実行履歴の共通の型（#1955）。
describe("実行履歴の状態の写し（run-history.md「状態の写し方」）", () => {
  it("Agent の状態を共通の 7 つに写し、承認待ち・確認待ちだけ Agent の言い方で上書きする", () => {
    expect(commonRunStatus("waiting_approval")).toBe("waiting");
    expect(commonRunStatus("waiting_confirmation")).toBe("waiting");
    expect(commonRunStatus("completed")).toBe("succeeded");
    expect(commonRunStatus("cancelled")).toBe("cancelled");
    expect(runStatusLabel("waiting_approval")).toBe("承認待ち");
    expect(runStatusLabel("waiting_confirmation")).toBe("確認待ち");
    expect(runStatusLabel("completed")).toBeUndefined();
  });

  it("共通の絞り込みの値を backend の条件に写す", () => {
    expect(backendRunStatus("all")).toBeUndefined();
    expect(backendRunStatus("succeeded")).toBe("completed");
    expect(backendRunStatus("waiting")).toBe("waiting");
    expect(backendRunExecutor("self")).toBe("mine");
    expect(backendRunExecutor("others")).toBe("others");
    expect(backendRunExecutor("all")).toBeUndefined();
    // 種類の既定は品質評価を除く（#1983）。「すべて」は送らない。
    expect(DEFAULT_AGENT_RUN_KIND).toBe("not_evaluation");
    expect(backendRunKind(DEFAULT_AGENT_RUN_KIND)).toBe("not_evaluation");
    expect(backendRunKind("all")).toBeUndefined();
    expect(backendRunKind("evaluation")).toBe("evaluation");
  });
});

describe("詳細の時間と処理の経過", () => {
  it("終わった実行だけ終了日時と所要時間を持つ（終わりの Event の時刻）", () => {
    const done = run({
      events: [
        { id: "e1", run_id: "run-1", type: "run.completed", message: "", payload: {}, created_at: "2026-10-15T09:00:03.200Z" },
      ],
    });
    expect(runFinishedAt(done)).toBe("2026-10-15T09:00:03.200Z");
    expect(runDurationMs(done)).toBe(3200);
    expect(runFinishedAt(run({ status: "running" }))).toBeNull();
    expect(runDurationMs(run({ status: "waiting_approval" }))).toBeNull();
  });

  // #2324: ツールの表示名（MCP の title。metadata `tool_titles`）を読む。
  it("ツールの表示名を metadata から読み、名前の定義で表示名を使う", () => {
    expect(runToolTitles(run({ metadata: { tool_titles: { rag__rag_search: "ナレッジベースの検索", bad: 1 } } }))).toEqual({
      rag__rag_search: "ナレッジベースの検索",
    });
    expect(runToolTitles(run({ metadata: { tool_titles: ["x"] } }))).toEqual({});
    const label = AGENT_CHAT_PROGRESS_STEPS.tool.label;
    expect(label("running", { tool: "rag__rag_search", title: "ナレッジベースの検索" })).toBe(
      "「ナレッジベースの検索」を実行しています"
    );
    expect(label("failed", { tool: "rag__rag_search", title: " " })).toBe("選ばれたツールを呼び出せませんでした");
  });
});

// #3226: 処理の経過は Run の trace（span の木）の投影。
const TRACE = "0123456789abcdef0123456789abcdef";

function span(seq: number, id: string, name: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    seq,
    attempt: 0,
    target_id: "run-1",
    emitted_at: NOW,
    type: "step",
    step_id: id,
    status: "done",
    name,
    trace_id: TRACE,
    started_at: NOW,
    finished_at: NOW,
    ...extra,
  };
}

const toolStep = (id: string, name: string, extra: Partial<RunState["steps"][number]> = {}) =>
  ({
    id,
    run_id: "run-1",
    kind: "tool",
    status: "completed",
    tool_call: { name, arguments: { query: "条件 A" } },
    ...extra,
  }) as RunState["steps"][number];

/** 共通の span の表の snapshot（`GET /api/runs/{id}/trace` の events。#3226 の案 A）。 */
const SPAN_EVENTS = parseChatProgressEvents([
  span(1, "a000000000000001", "agent.execute", { root: true }),
  span(2, "a000000000000002", "gen_ai.chat", { parent_id: "a000000000000001", kind: "plan" }),
  span(3, "a000000000000003", "gen_ai.execute_tool", {
    parent_id: "a000000000000001",
    kind: "tool",
    params: { tool: "rag__rag_search", title: "ナレッジベースの検索", run_step: "s1" },
    attributes: { "gen_ai.tool.name": "rag__rag_search" },
  }),
  span(4, "a000000000000004", "mcp.request", { parent_id: "a000000000000003" }),
  span(5, "a000000000000005", "gen_ai.chat", { parent_id: "a000000000000001", kind: "respond" }),
  { schema_version: 1, seq: 6, attempt: 0, target_id: "run-1", emitted_at: NOW, type: "terminal", status: "done" },
]);

function spanRun(overrides: Partial<RunState> = {}): RunState {
  return run({
    trace_store: true,
    trace_id: TRACE,
    steps: [
      toolStep("s1", "rag__rag_search", {
        tool_call: {
          name: "rag__rag_search",
          arguments: { query: "条件 A" },
          data_scope: { profile_id: "profile-a", action: "filled" },
        } as never,
        tool_result: { name: "rag__rag_search", success: true, output: { answer: "回答 A" } } as never,
      }),
    ],
    ...overrides,
  });
}

describe("処理の経過（span の木。Issue 3226）", () => {
  it("span のイベントを木の順に並べ、実行の全体の span は行にせず、工程の種類で名前を付ける", () => {
    const steps = runTimelineSteps(spanRun(), SPAN_EVENTS);
    expect(steps.map((step) => [step.label, step.level ?? 0])).toEqual([
      ["質問に応じた次の操作を選びました", 0],
      ["「ナレッジベースの検索」を実行しました", 0],
      ["MCP の呼び出し", 1],
      ["回答を作りました", 0],
    ]);
    expect(steps.map((step) => step.id)).not.toContain("a000000000000001");
    expect(chatProgressTraceIdOf(SPAN_EVENTS)).toBe(TRACE);
  });

  it("ツールの行は Run の step と結び、データの範囲の記録・引数と結果・表の結果を中身にする", () => {
    const state = spanRun();
    expect(progressRunSteps(SPAN_EVENTS, state.steps).get("a000000000000003")?.id).toBe("s1");
    const tool = runTimelineSteps(state, SPAN_EVENTS, (step) => `表:${step.id}`)[1];
    expect(tool.children).toBe("表:s1");
    const fields = tool.details?.fields ?? [];
    // 内部名を先頭に出し（#2324）、共通の技術の値（Span name）を後ろに足す。
    expect(fields[0]).toEqual({ label: "ツール", value: "rag__rag_search", mono: true });
    expect(fields.some((field) => field.label === "Span name" && field.value === "gen_ai.execute_tool")).toBe(true);
    expect(tool.details?.code?.map((block) => block.label)).toEqual(["引数", "結果"]);
    expect(tool.details?.code?.[1].text).toContain("回答 A");
    expect(tool.details?.lines?.length).toBeGreaterThan(0);
  });

  it("span の終わりのイベントの詳細は、結果をまだ持たない step で上書きしない（Issue 2373）", () => {
    const eventDetails = { fields: [{ label: "ツール", value: "rag__rag_search", mono: true }], lines: ["イベントの詳細"] };
    const definitions = agentProgressDefinitions([toolStep("s1", "rag__rag_search", { status: "running" })]);
    const details = definitions.tool.details?.("done", { tool: "rag__rag_search", run_step: "s1" }, eventDetails);
    expect(details).toEqual(eventDetails);
    const withResult = agentProgressDefinitions([
      toolStep("s1", "rag__rag_search", {
        tool_result: { name: "rag__rag_search", success: true, output: { answer: "回答 A" } } as never,
      }),
    ]);
    expect(withResult.tool.details?.("done", { run_step: "s1" }, eventDetails)?.code?.[1].text).toContain("回答 A");
    // ツールの step が無い Run は Agent の段の定義のまま。
    expect(agentProgressDefinitions([])).toBe(AGENT_CHAT_PROGRESS_STEPS);
  });

  it("承認の待ちは待ったツールを補足に出し、却下したツールはスキップと理由で出す", () => {
    const events = parseChatProgressEvents([
      span(1, "b000000000000001", "agent.resume", { root: true }),
      span(2, "b000000000000002", "agent.approval_wait", {
        parent_id: "b000000000000001",
        kind: "approval",
        params: { tools: "記録の登録", round: 1 },
      }),
      span(3, "b000000000000003", "gen_ai.execute_tool", {
        parent_id: "b000000000000001",
        kind: "tool",
        status: "skipped",
        params: { tool: "write", rejected: true },
      }),
    ]);
    const steps = runTimelineSteps(run({ trace_store: true }), events);
    expect(steps.map((step) => [step.label, step.detail, step.status])).toEqual([
      ["承認の判断を受け取りました", "記録の登録", "done"],
      ["選ばれたツールを呼び出す", "承認されませんでした", "skipped"],
    ]);
  });

  it("共通の span の表に書く前の終わった Run は古い形式として扱い、span の無い記録は行を作らない", () => {
    // 表に書く前の Run（`trace_store` が無い）が終わっていれば古い形式（画面は古い形式の案内）。
    expect(runProgressIsLegacy(run({ status: "completed" }))).toBe(true);
    expect(runProgressIsLegacy(run({ status: "failed", trace_store: false }))).toBe(true);
    // 実行中・受け付けただけの Run と、表に書く Run（開始の前に停止した Run を含む）は古い形式ではない。
    expect(runProgressIsLegacy(run({ status: "running" }))).toBe(false);
    expect(runProgressIsLegacy(run({ status: "queued" }))).toBe(false);
    expect(runProgressIsLegacy(run({ status: "cancelled", trace_store: true }))).toBe(false);
    expect(runProgressIsLegacy(spanRun())).toBe(false);
    // span の名前の無い段だけのイベント（#3226 の前の形）は行にしない。
    const stepsOnly = parseChatProgressEvents([
      { schema_version: 1, seq: 1, attempt: 0, target_id: "run-1", emitted_at: NOW, type: "step", step_id: "plan", status: "done" },
    ]);
    expect(runTimelineSteps(spanRun(), stepsOnly)).toEqual([]);
    expect(runTimelineSteps(spanRun(), [])).toEqual([]);
  });

  it("チャットの処理の経過（イベント → 名前）と実行の詳細の行は同じ", () => {
    const state = spanRun();
    const live = runProgressSteps(
      state,
      SPAN_EVENTS,
      chatProgressStepsFromEvents(SPAN_EVENTS, agentProgressDefinitions(state.steps), state.id)
    );
    expect(runTimelineSteps(state, SPAN_EVENTS)).toEqual(live.map(chatProgressTimelineStep));
  });

  it("長い結果は先頭だけにして省略の印を付け、ツールの呼び出しでなければ詳細を付けない", () => {
    const long = toolStep("s1", "nl2sql__query", {
      tool_call: { name: "nl2sql__query", arguments: {} },
      tool_result: { name: "nl2sql__query", success: true, output: { text: "x".repeat(10_000) } } as never,
    });
    const details = toolStepDetails(long);
    expect(details?.truncated).toBe(true);
    expect(details?.code).toHaveLength(1);
    // 引数も結果も無いツールの段も、内部名だけは詳細に出す。
    expect(toolStepDetails(toolStep("s2", "a", { tool_call: { name: "a", arguments: {} } }))).toEqual({
      fields: [{ label: "ツール", value: "a", mono: true }],
    });
    expect(toolStepDetails(toolStep("s3", "a", { tool_call: undefined }))).toBeUndefined();
  });
});

describe("runConversationHref（会話を開く。Issue 2343）", () => {
  const canOpen = { seesAllUsers: false, canOpenChat: true };

  it("チャットの画面から送った Run は、その会話をチャットで開く", () => {
    expect(runConversationHref(run({ thread_id: "t1", owned_by_me: true }), canOpen)).toBe(
      "/chat?agent_id=default&thread_id=t1"
    );
  });

  it("会話の無い Run・チャットでない Run・開けない人には出さない", () => {
    expect(runConversationHref(run({ thread_id: null }), canOpen)).toBeNull();
    expect(runConversationHref(run({ thread_id: "t1", metadata: { source: "mcp" } }), canOpen)).toBeNull();
    expect(runConversationHref(run({ thread_id: "t1", metadata: { evaluation_job_id: "j" } }), canOpen)).toBeNull();
    expect(runConversationHref(run({ thread_id: "t1", metadata: { automation_id: "a" } }), canOpen)).toBeNull();
    expect(runConversationHref(run({ thread_id: "t1", owned_by_me: false }), canOpen)).toBeNull();
    expect(runConversationHref(run({ thread_id: "t1" }), { seesAllUsers: false, canOpenChat: false })).toBeNull();
    // system_admin は他の利用者の会話も開ける。
    expect(runConversationHref(run({ thread_id: "t1", owned_by_me: false }), { seesAllUsers: true, canOpenChat: true })).toBe(
      "/chat?agent_id=default&thread_id=t1"
    );
  });
});
