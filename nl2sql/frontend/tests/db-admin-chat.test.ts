import assert from "node:assert/strict";
import test from "node:test";

import {
  dbAdminChatBlockedStatementLabels,
  dbAdminChatCanRunAll,
  dbAdminChatCanRunStatement,
  dbAdminChatConfirmed,
  dbAdminChatExecutionFromOutcome,
  dbAdminChatGenerateStep,
  dbAdminChatSavedExecutions,
  dbAdminChatStatementKind,
  dbAdminChatTraceProgress,
  dbAdminExecutionSucceeded,
  dbAdminFailureText,
} from "../src/features/nl2sql/dbAdminChat.ts";
import { CHAT_PROGRESS_STEP_DEFINITIONS } from "../src/features/nl2sql/chatProgress.ts";
import {
  chatProgressHasSpans,
  chatProgressStepsFromEvents,
  chatProgressTraceIdOf,
  parseChatProgressEvents,
} from "@production-ready/ui";
import type {
  DbAdminChatExecuteData,
  DbAdminChatPlan,
  DbAdminChatStatementPlan,
  DbAdminChatTurn,
  DbAdminExecuteData,
} from "../src/features/nl2sql/types.ts";

function statement(index: number, sql: string, overrides: Partial<DbAdminChatStatementPlan> = {}): DbAdminChatStatementPlan {
  const select = /^\s*(select|with)\b/i.test(sql);
  return {
    index,
    statement_type: select ? "SELECT" : sql.trim().split(/\s+/)[0]!.toUpperCase(),
    sql,
    select_only: select,
    implicit_commit: false,
    blocked_reason: "",
    requires_confirmation: !select,
    confirmation_target: select ? "" : "ADMIN_EXECUTE",
    unknown_columns: [],
    warnings: [],
    ...overrides,
  };
}

function plan(statements: DbAdminChatStatementPlan[], overrides: Partial<DbAdminChatPlan> = {}): DbAdminChatPlan {
  const changing = statements.filter((item) => !item.select_only);
  const blocked = statements.some((item) => item.blocked_reason);
  return {
    sql: statements.map((item) => `${item.sql};`).join("\n"),
    statements,
    read_only: statements.length > 0 && changing.length === 0,
    requires_confirmation: changing.length > 0 && !blocked,
    confirmation_target: changing.length > 0 && !blocked ? "ADMIN_EXECUTE" : "",
    implicit_commit: false,
    blocked,
    blocked_reasons: statements.map((item) => item.blocked_reason).filter(Boolean),
    ...overrides,
  };
}

function result(overrides: Partial<DbAdminExecuteData> = {}): DbAdminExecuteData {
  return {
    executed: true,
    runtime: "oracle",
    statements: [
      {
        index: 1,
        statement_type: "SELECT",
        status: "executed",
        sql: "SELECT 1 FROM DUAL",
        message: "1 rows",
        elapsed_ms: 1,
        error_message: "",
      },
    ],
    committed: false,
    rolled_back: false,
    warnings: [],
    timing: { created_at: "2026-10-18T00:00:00Z", elapsed_ms: 5 },
    ...overrides,
  } as DbAdminExecuteData;
}

const SELECTS = plan([statement(1, "SELECT 1 FROM DUAL"), statement(2, "SELECT 2 FROM DUAL")]);
const MIXED = plan([statement(1, "SELECT 1 FROM DUAL"), statement(2, "DELETE FROM T1")]);
const WITH_BLOCKED = plan([
  statement(1, "SELECT 1 FROM DUAL"),
  statement(2, "ALTER SYSTEM SET OPEN_CURSORS = 300", {
    blocked_reason: "ALTER SYSTEM は実行できません。",
    requires_confirmation: false,
    confirmation_target: "",
  }),
]);

test("文の分類は読み取り専用 / 変更あり / 実行できない", () => {
  assert.deepEqual(WITH_BLOCKED.statements.map(dbAdminChatStatementKind), ["readOnly", "blocked"]);
  assert.equal(dbAdminChatStatementKind(MIXED.statements[1]!), "change");
});

test("SELECT の文は確認語なしで実行でき、変える文は確認語が一致したときだけ、止める文は実行できない", () => {
  const [select, mutation] = MIXED.statements;
  assert.equal(dbAdminChatCanRunStatement(select!, ""), true);
  assert.equal(dbAdminChatCanRunStatement(mutation!, ""), false);
  assert.equal(dbAdminChatCanRunStatement(mutation!, "admin_execute"), false);
  assert.equal(dbAdminChatCanRunStatement(mutation!, " ADMIN_EXECUTE "), true);
  const drop = statement(1, "DROP TABLE APP.T1", { confirmation_target: "APP.T1" });
  assert.equal(dbAdminChatConfirmed(drop, "ADMIN_EXECUTE"), false);
  assert.equal(dbAdminChatConfirmed(drop, "APP.T1"), true);
  assert.equal(dbAdminChatCanRunStatement(WITH_BLOCKED.statements[1]!, "ADMIN_EXECUTE"), false);
  // 止める文があっても、ほかの文は実行できる。
  assert.equal(dbAdminChatCanRunStatement(WITH_BLOCKED.statements[0]!, ""), true);
});

test("「すべて実行」は SELECT だけなら確認語なし、変える文を含めば確認語を 1 回、止める文があれば押せない", () => {
  assert.equal(dbAdminChatCanRunAll(SELECTS, ""), true);
  assert.equal(dbAdminChatCanRunAll(MIXED, ""), false);
  assert.equal(dbAdminChatCanRunAll(MIXED, "ADMIN_EXECUTE"), true);
  assert.equal(dbAdminChatCanRunAll(WITH_BLOCKED, ""), false);
  assert.equal(dbAdminChatBlockedStatementLabels(WITH_BLOCKED), "#2");
  assert.equal(dbAdminChatCanRunAll(plan([]), ""), false);
});

/**
 * ターンの処理の経過の API が返す span の表の snapshot（生成 → SQL の確認。span ごとに 1 件と終端。合成の値。
 * #3226 の案 A）。
 */
function generationSnapshot(checkStatus: "done" | "failed"): unknown[] {
  const root = "0".repeat(16);
  const traceId = "c".repeat(32);
  const event = (seq: number, value: Record<string, unknown>) => ({
    schema_version: 1, seq, target_id: "turn-1", attempt: 0, emitted_at: "2026-10-23T00:00:04Z", type: "step",
    trace_id: traceId, started_at: "2026-10-23T00:00:01Z", ...value,
  });
  return [
    event(1, { step_id: root, name: "nl2sql.db_admin_chat", root: true, status: "done", finished_at: "2026-10-23T00:00:04Z" }),
    event(2, {
      step_id: "1".padStart(16, "0"), kind: "generate_sql", name: "nl2sql.generate_sql", parent_id: root,
      params: { engine: "enterprise_ai_direct" }, status: "done", finished_at: "2026-10-23T00:00:03Z",
    }),
    event(3, {
      step_id: "2".padStart(16, "0"), kind: "db_admin_check", name: "nl2sql.safety_check", parent_id: root,
      params: { statements: 2, read_only: 1, change: 0, blocked: 1 }, status: checkStatus,
      finished_at: "2026-10-23T00:00:04Z",
    }),
    { schema_version: 1, seq: 4, target_id: "turn-1", attempt: 0, emitted_at: "2026-10-23T00:00:04Z", type: "terminal", status: "done" },
  ];
}

function generationSteps(checkStatus: "done" | "failed") {
  const events = parseChatProgressEvents(generationSnapshot(checkStatus));
  return { steps: chatProgressStepsFromEvents(events, CHAT_PROGRESS_STEP_DEFINITIONS, "turn-1"), events };
}

test("処理の経過は生成のときの span の木（生成 → SQL の確認。実行の段を足さない。#3226）", () => {
  const { steps, events } = generationSteps("done");
  assert.equal(chatProgressHasSpans(events), true);
  assert.equal(chatProgressTraceIdOf(events), "c".repeat(32));
  assert.deepEqual(
    steps.map((step) => [step.label, step.status]),
    [
      ["SQL を生成しました", "done"],
      ["SQL の種類と参照範囲を検査しました", "done"],
    ],
  );
  assert.equal(steps[0]?.detail, "Enterprise AI");
  // 止める文があっても、ほかの文は実行できる（backend が段を完了にする）。補足は文の分類ごとの数。
  assert.equal(steps[1]?.detail, "読み取り専用 1・実行できない 1");
  assert.equal(steps.some((step) => step.label.includes("実行")), false);
  assert.equal(generationSteps("failed").steps[1]?.status, "failed");
});

test("ターンの処理の経過: span の無い古いターンは古い形式、書けなかった span があれば一部だけの記録", () => {
  const legacy = [{ schema_version: 1, seq: 1, target_id: "turn-old", attempt: 0, emitted_at: "2026-10-23T00:00:04Z", type: "terminal", status: "done" }];
  assert.equal(dbAdminChatTraceProgress("turn-old", { events: legacy, droppedSpans: 0 }).legacy, true);
  const partial = dbAdminChatTraceProgress("turn-old", { events: legacy, droppedSpans: 3 });
  assert.equal(partial.legacy, false);
  assert.equal(partial.partial, true);
  // まだ読めていないターンは何も出さない（古い形式とも出さない）。
  assert.deepEqual(dbAdminChatTraceProgress("turn-1", undefined), { steps: [], legacy: false, partial: false });
  const done = dbAdminChatTraceProgress("turn-1", { events: generationSnapshot("done"), droppedSpans: 0 });
  assert.equal(done.legacy, false);
  assert.equal(done.traceId, "c".repeat(32));
  assert.equal(done.steps.length, 2);
});

test("送信中は生成の仮の行", () => {
  const pending = dbAdminChatGenerateStep("running", 1_000);
  assert.equal(pending.status, "running");
  assert.equal(pending.label, "SQL を生成しています");
  assert.equal(pending.startedAt, new Date(1_000).toISOString());
});

test("失敗の内容を AI のエラー分析に渡す形にする", () => {
  const failed = result({
    executed: false,
    statements: [
      {
        index: 2,
        statement_type: "DROP",
        status: "error",
        sql: "DROP TABLE T9",
        error_message: "ORA-00942: table or view does not exist",
        message: "",
        elapsed_ms: 1,
      },
    ],
    warnings: ["rollback しました"],
  });
  assert.equal(dbAdminExecutionSucceeded(failed), false);
  assert.match(dbAdminFailureText(failed), /#2 DROP: ORA-00942/);
  assert.match(dbAdminFailureText(failed), /rollback しました/);
});

function runAllData(overrides: Partial<DbAdminChatExecuteData> = {}): DbAdminChatExecuteData {
  return {
    mode: "all",
    batch_id: "batch-1",
    executed: true,
    committed: false,
    rolled_back: true,
    stopped_at: 2,
    statements: [
      { index: 1, status: "succeeded", result: result() },
      { index: 2, status: "failed", result: result({ executed: false }) },
      { index: 3, status: "not_run", result: null },
    ],
    warnings: [],
    timing: { created_at: "2026-10-18T00:00:00Z", elapsed_ms: 5 },
    ...overrides,
  } as DbAdminChatExecuteData;
}

test("「すべて実行」の文ごとの結果を、成功・失敗・実行しなかった文の状態にする", () => {
  const data = runAllData();
  assert.deepEqual(
    data.statements.map((outcome) => dbAdminChatExecutionFromOutcome(outcome, data, 1_000, 2_000).status),
    ["done", "failed", "not_run"],
  );
  const pending = dbAdminChatExecutionFromOutcome(
    { index: 1, status: "confirmation_required", result: null },
    runAllData({ warnings: ["確認語が違います"] }),
    1_000,
    2_000,
  );
  assert.equal(pending.status, "failed");
  assert.match(String((pending.requestError as Error).message), /確認語が違います/);
});

function savedTurn(overrides: Partial<DbAdminChatTurn> = {}): DbAdminChatTurn {
  return {
    turn_id: "turn-1",
    question: "数量",
    created_at: "2026-10-18T00:00:00Z",
    generated_at: "2026-10-18T00:00:02Z",
    explanation: "",
    warnings: [],
    context_tables: [],
    model: "test-text-model",
    plan: SELECTS,
    revision: 0,
    corrected: false,
    statement_executions: [
      {
        index: 1,
        executed_at: "2026-10-18T00:00:10Z",
        mode: "all",
        batch_id: "batch-1",
        confirmation_given: false,
        status: "succeeded",
        row_count: 12,
        column_count: 3,
        has_more: false,
        outcome: "成功（12 行）",
        result: result({ timing: { created_at: "2026-10-18T00:00:09Z", elapsed_ms: 400 } as DbAdminExecuteData["timing"] }),
      },
      {
        index: 2,
        executed_at: "2026-10-18T00:00:10Z",
        mode: "all",
        batch_id: "batch-1",
        confirmation_given: false,
        status: "not_run",
        row_count: null,
        column_count: null,
        has_more: false,
        outcome: "実行しなかった（前の文の失敗で止めた）",
        result: null,
      },
    ],
    last_run_all: {
      executed_at: "2026-10-18T00:00:10Z",
      batch_id: "batch-1",
      confirmation_given: false,
      succeeded: false,
      statement_count: 2,
      succeeded_count: 1,
      stopped_at: 2,
      committed: false,
      rolled_back: false,
      warnings: [],
    },
    ...overrides,
  };
}

test("開き直したターンは、保存した文ごとの最後の実行と「すべて実行」の要約から状態を作る（行は持たない）", () => {
  const executions = dbAdminChatSavedExecutions(savedTurn());
  const first = executions.statements[1];
  assert.ok(first);
  assert.equal(first.status, "done");
  assert.equal(first.saved?.row_count, 12);
  assert.equal(first.finishedAtMs, Date.parse("2026-10-18T00:00:10Z"));
  assert.equal(first.startedAtMs, Date.parse("2026-10-18T00:00:10Z") - 400);
  assert.equal(executions.statements[2]?.status, "not_run");
  assert.equal(executions.runAll?.status, "failed");
  assert.equal(executions.runAll?.saved?.stopped_at, 2);
  const empty = dbAdminChatSavedExecutions(savedTurn({ statement_executions: [], last_run_all: null }));
  assert.deepEqual(empty, { statements: {}, runAll: null });
});
