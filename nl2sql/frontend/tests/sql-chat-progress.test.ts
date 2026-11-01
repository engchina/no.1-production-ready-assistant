import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  chatProgressHasSpans,
  chatProgressStepsFromEvents,
  chatProgressTerminalOf,
  chatProgressTraceIdOf,
  type ChatProgressEvent,
  type ChatProgressParams,
  type ChatProgressStepStatus,
  type ChatProgressTerminalStatus,
} from "@production-ready/ui";

import {
  CHAT_PROGRESS_STEP_DEFINITIONS,
  chatJobElapsedMs,
  chatQueueProgressSteps,
  dbAdminChatTurnTracePath,
  historyTracePath,
  jobTracePath,
  parseTraceProgressResponse,
  chatSubmitProgressSteps,
  NL2SQL_TRACE_DETAILS_LABELS,
  chatStageName,
} from "../src/features/nl2sql/chatProgress.ts";
import type { JobData } from "../src/features/nl2sql/types.ts";

// #1145: チャットのジョブの段階を 3 製品共通の段階の形にする。
// #3226: 処理の経過は実行の trace（span の木）の投影。backend は span のイベントを記録し、画面は工程の span の
// 定義（名前と補足）だけを持つ。実行の全体（root）は行にしない。

const created = "2026-10-04T00:00:00.000Z";
const TRACE_ID = "a".repeat(32);
const ROOT_ID = "0".repeat(16);
const STEPS = ["queue", "prepare_context", "generate_sql", "safety_check", "execute_sql", "format_results"];

interface SpanState {
  status: ChatProgressStepStatus;
  params?: ChatProgressParams;
  /** 親の span（省略は実行の全体）。 */
  parent?: string;
  name?: string;
  kind?: string | null;
  cancelled?: boolean;
}

function spanId(index: number): string {
  return (index + 1).toString(16).padStart(16, "0");
}

/** backend の記録と同じ span のイベント（実行の全体の下に、始まった順に開始 → 終了）。 */
function events(
  states: Record<string, SpanState>,
  terminal?: ChatProgressTerminalStatus,
): ChatProgressEvent[] {
  const list: ChatProgressEvent[] = [];
  const base = () => ({
    schema_version: 1 as const,
    seq: list.length + 1,
    target_id: "j1",
    attempt: 0,
    emitted_at: created,
  });
  list.push({ ...base(), type: "step", step_id: ROOT_ID, status: "running", started_at: created, name: "nl2sql.execute", root: true, trace_id: TRACE_ID });
  Object.entries(states).forEach(([key, state], index) => {
    const kind = state.kind === undefined ? key : state.kind;
    const span = {
      type: "step" as const,
      step_id: spanId(index),
      ...(kind ? { kind } : {}),
      name: state.name ?? `nl2sql.${key}`,
      parent_id: state.parent ?? ROOT_ID,
      trace_id: TRACE_ID,
      started_at: created,
      ...(state.params ? { params: state.params } : {}),
    };
    list.push({ ...base(), ...span, status: "running" });
    if (state.status === "running") return;
    list.push({ ...base(), ...span, status: state.status, finished_at: created, ...(state.cancelled ? { cancelled: true } : {}) });
  });
  if (terminal) list.push({ ...base(), type: "terminal", status: terminal });
  return list;
}

const view = (list: ChatProgressEvent[]) => chatProgressStepsFromEvents(list, CHAT_PROGRESS_STEP_DEFINITIONS, undefined, { traceLabels: NL2SQL_TRACE_DETAILS_LABELS });
const summary = (list: ChatProgressEvent[]) => view(list).map((step) => `${step.label}:${step.status}`);

test("送信の応答待ちは「質問を送信しています」の 1 段階", () => {
  const [step] = chatSubmitProgressSteps(Date.parse(created));
  assert.equal(step.id, "submit");
  assert.equal(step.status, "running");
  assert.equal(step.label, "質問を送信しています");
  assert.equal(step.startedAt, created);
});

test("worker が受け取るまでの仮の行は開始待ち（span が届いたら木に置き換わる。#3226）", () => {
  const [step] = chatQueueProgressSteps(created);
  assert.equal(step.status, "running");
  assert.equal(step.label, "処理の開始を待っています");
  assert.equal(step.startedAt, created);
});

test("工程の span に名前を付け、実行の全体は行にしない（並びは始まった順）", () => {
  const running = view(
    events({
      queue: { status: "done" },
      prepare_context: { status: "done" },
      generate_sql: { status: "running", params: { engine: "enterprise_ai_direct" } },
    }),
  );
  assert.deepEqual(
    running.map((step) => step.label),
    ["処理を開始しました", "質問と参照できる表を整理しました", "SQL を生成しています"],
  );
  assert.equal(running[2].status, "running");
  assert.equal(running[2].detail, "Enterprise AI");
  // 始まっていない段の行は無い（span は始まったときに出る）。
  assert.ok(!running.some((step) => step.status === "pending"));
});

test("段の中の span（モデルの呼び出し）は段の下に入れ子で出る", () => {
  const list = events({
    queue: { status: "done" },
    generate_sql: { status: "running", params: { engine: "enterprise_ai_direct" } },
    model: { status: "running", kind: null, name: "gen_ai.chat", parent: spanId(1) },
  });
  const steps = view(list);
  assert.equal(steps.length, 3);
  assert.equal(steps[2].level, 1);
  assert.match(steps[2].label, /モデルを呼び出す（用途の記録なし）/);
  assert.equal(chatProgressTraceIdOf(list), TRACE_ID);
});

test("完了したジョブは参照した表と取得した行数を補足する", () => {
  const list = events(
    {
      queue: { status: "done" },
      prepare_context: { status: "done" },
      generate_sql: { status: "done", params: { engine: "enterprise_ai_direct" } },
      safety_check: { status: "done", params: { tables: "APP.A, APP.B, APP.C", table_count: 4 } },
      execute_sql: { status: "done", params: { rows: 1234 } },
      format_results: { status: "done" },
    },
    "done",
  );
  const steps = view(list);
  assert.deepEqual(
    steps.map((step) => step.status),
    STEPS.map(() => "done"),
  );
  assert.equal(steps[2].detail, "Enterprise AI");
  assert.equal(steps[3].detail, "APP.A, APP.B, APP.C ほか 1 件");
  assert.equal(steps[4].label, "SQL を実行しました");
  assert.equal(steps[4].detail, "1,234 行");
  assert.equal(steps[5].label, "SQL の実行結果を表示用に整えました");
  assert.equal(chatProgressTerminalOf(list), "done");

  // 表が 3 件以下なら件数を足さない。未知の生成方法・形の違う値は出さない。
  const few = view(
    events({
      generate_sql: { status: "running", params: { engine: "unknown" } },
      safety_check: { status: "done", params: { tables: "APP.A", table_count: 1 } },
      execute_sql: { status: "done", params: { rows: "x" } },
    }),
  );
  assert.equal(few[0].detail, undefined);
  assert.equal(few[1].detail, "APP.A");
  assert.equal(few[2].detail, undefined);
});

test("backend の段の詳細（生成した SQL）を段を開いて見る詳細の先頭にする（#2294）", () => {
  const list = events({ queue: { status: "done" }, generate_sql: { status: "running", params: { engine: "enterprise_ai_direct" } } });
  const details = { code: [{ label: "生成した SQL", language: "sql" as const, text: "SELECT 1 FROM DUAL" }] };
  list.push({
    schema_version: 1,
    seq: list.length + 1,
    target_id: "j1",
    attempt: 0,
    emitted_at: created,
    type: "step",
    step_id: spanId(1),
    kind: "generate_sql",
    name: "nl2sql.generate_sql",
    parent_id: ROOT_ID,
    trace_id: TRACE_ID,
    status: "done",
    started_at: created,
    finished_at: created,
    params: { engine: "enterprise_ai_direct" },
    details,
  });
  const steps = view(list);
  assert.deepEqual(steps[1].details?.code, details.code);
});

test("失敗した段は「〜できませんでした」で、後の段は始まらない", () => {
  const steps = view(
    events(
      {
        queue: { status: "done" },
        prepare_context: { status: "done" },
        generate_sql: { status: "failed", params: { engine: "enterprise_ai_direct" } },
      },
      "failed",
    ),
  );
  assert.equal(steps.length, 3);
  assert.equal(steps[2].label, "SQL を生成できませんでした");
  assert.equal(steps[2].name, "SQL の生成");
});

test("止めた段（停止の span）は未実行と停止の文言", () => {
  const list = events(
    {
      queue: { status: "done" },
      prepare_context: { status: "skipped", cancelled: true },
    },
    "cancelled",
  );
  const steps = view(list);
  assert.ok(!steps.some((step) => step.status === "failed"));
  assert.equal(steps[1].status, "skipped");
  assert.equal(steps[1].detail, NL2SQL_TRACE_DETAILS_LABELS.cancelled);
  assert.equal(chatProgressTerminalOf(list), "cancelled");
});

test("生成だけのターンは実行を「未実行」にして結果の整形へ進む", () => {
  assert.deepEqual(
    summary(
      events({
        queue: { status: "done" },
        prepare_context: { status: "done" },
        generate_sql: { status: "done" },
        safety_check: { status: "done" },
        execute_sql: { status: "skipped" },
        format_results: { status: "running" },
      }),
    ).slice(4),
    ["SQL の実行:skipped", "SQL の実行結果を表示用に整えています:running"],
  );
});

test("DB 管理チャットの SQL の確認は安全性の確認と同じ名前で、補足は文の分類ごとの数", () => {
  const steps = view(
    events({
      generate_sql: { status: "done", params: { engine: "enterprise_ai_direct" } },
      check: { status: "done", kind: "db_admin_check", params: { statements: 2, read_only: 1, change: 0, blocked: 1 } },
    }),
  );
  assert.equal(steps[1].label, "SQL の種類と参照範囲を検査しました");
  assert.equal(steps[1].detail, "読み取り専用 1・実行できない 1");
  const single = (params: ChatProgressParams) =>
    view(events({ check: { status: "done", kind: "db_admin_check", params } }))[0].detail;
  assert.equal(single({ statements: 1, read_only: 1, change: 0, blocked: 0 }), "読み取り専用（確認語は不要）");
  assert.equal(single({ statements: 1, read_only: 0, change: 1, blocked: 0, confirmation_target: "ADMIN_EXECUTE" }), "確認語 ADMIN_EXECUTE が必要");
  assert.equal(single({ statements: 1, read_only: 0, change: 0, blocked: 1 }), "実行できない文です");
});

test("span の無い記録（段だけのイベント）は span の記録ではない", () => {
  const legacy: ChatProgressEvent[] = [
    { schema_version: 1, seq: 1, target_id: "j1", attempt: 0, emitted_at: created, type: "step", step_id: "prepare_context", status: "done" },
    { schema_version: 1, seq: 2, target_id: "j1", attempt: 0, emitted_at: created, type: "terminal", status: "done" },
  ];
  assert.equal(chatProgressHasSpans(legacy), false);
  assert.equal(chatProgressHasSpans(events({ queue: { status: "done" } }, "done")), true);
});

test("処理の経過の API は記録ごと（#3226 の案 A）で、応答のイベントと書けなかった span の数を読む", () => {
  assert.equal(jobTracePath("job/1"), "/api/nl2sql/jobs/job%2F1/trace");
  assert.equal(historyTracePath("h 1"), "/api/nl2sql/history/h%201/trace");
  assert.equal(
    dbAdminChatTurnTracePath("c1", "t1"),
    "/api/nl2sql/db-admin-chat/conversations/c1/turns/t1/trace",
  );
  const snapshot = events({ queue: { status: "done" } }, "done");
  assert.deepEqual(parseTraceProgressResponse({ events: snapshot, dropped_spans: 2 }), {
    events: snapshot,
    droppedSpans: 2,
  });
  // 形の違う値は空・0 にする（未検証の入力）。
  assert.deepEqual(parseTraceProgressResponse({ events: "x", dropped_spans: -1 }), { events: [], droppedSpans: 0 });
  assert.deepEqual(parseTraceProgressResponse(null), { events: [], droppedSpans: 0 });
});

test("全体の所要時間はジョブの作成から終了まで（終わっていなければ出さない）", () => {
  const job = (overrides: Partial<JobData>): JobData => ({
    job_id: "j1",
    status: "done",
    created_at: created,
    steps: [],
    ...overrides,
  });
  assert.equal(chatJobElapsedMs(job({ finished_at: "2026-10-04T00:00:12.000Z" })), 12_000);
  assert.equal(chatJobElapsedMs(job({ status: "running" })), null);
  assert.equal(chatJobElapsedMs(job({ finished_at: null, elapsed_ms: 900 })), 900);
});

// #3214 / #3219: 工程の定義の無い段の span（属性 `pr.stage`）も、処理の経過の段と同じ関数で名前にする。
test("段の span の名前は処理の経過の工程と同じ", () => {
  const stageName = NL2SQL_TRACE_DETAILS_LABELS.stageName;
  assert.equal(stageName, chatStageName);
  assert.equal(stageName?.("generate_sql", "done"), "SQL を生成しました");
  assert.equal(stageName?.("safety_check", "failed"), "SQL の種類と参照範囲を検査できませんでした");
  assert.equal(stageName?.("execute_sql", "cancelled"), "SQL の実行");
  assert.equal(stageName?.("unknown_stage", "done"), undefined);
  // 画面の製品（#3272。別の製品が書いた span は NL2SQL の工程の定義で訳さない）。
  assert.equal(NL2SQL_TRACE_DETAILS_LABELS.product, "nl2sql");
});

// #3272: backend が span に書く工程の名前（別の製品の画面が出す名前）は、NL2SQL の画面の工程の名詞の名前と同じ。
test("backend の工程の名前（STAGE_DISPLAY_NAMES）は画面の工程の名詞の名前と同じ", () => {
  const source = readFileSync(new URL("../../backend/app/features/nl2sql/trace_spans.py", import.meta.url), "utf8");
  const block = source.slice(source.indexOf("STAGE_DISPLAY_NAMES: dict"), source.indexOf("def span_display_name"));
  const pairs = [...block.matchAll(/"([a-z_]+)": "([^"]+)"/g)].map((match) => [match[1], match[2]]);
  assert.deepEqual(
    pairs.map(([stage]) => stage),
    STEPS,
  );
  for (const [stage, name] of pairs) assert.equal(chatStageName(stage, "skipped"), name, stage);
});
