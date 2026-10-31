/**
 * Run の中の表のデータ（ツールの結果・成果物。#1158）を取り出すロジックの確認。
 * 表の形の判定そのもの（`toTabularData`）は platform の packages/ui の単体テストが確かめる。
 * Agent の frontend は Vitest を持たないため、画面を開かずに Playwright の test で関数を確かめる。
 */
import type { Artifact, RunState, RunStep } from "../src/lib/api";
import { artifactTable, runToolResultTables } from "../src/lib/run-tables";

import { expect, test } from "./fixtures/test";

const T0 = "2026-10-05T09:00:00.000Z";

function step(id: string, name: string, output: Record<string, unknown> | null, overrides: Partial<RunStep> = {}): RunStep {
  return {
    id,
    run_id: "run-1",
    kind: "tool",
    status: "completed",
    tool_call: { name, arguments: {} },
    tool_result: {
      name,
      success: true,
      output,
      error_details: {},
      started_at: T0,
      completed_at: T0,
      duration_ms: 10,
      policy_decision: "allow",
      approval_required: false,
      guardrail_warnings: [],
      audit_metadata: {},
    },
    ...overrides,
  };
}

function run(steps: RunStep[]): RunState {
  return {
    id: "run-1",
    goal: "部門別の売上は？",
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps,
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_at: T0,
    updated_at: T0,
  };
}

const JOB_DONE = {
  job_id: "job-1",
  status: "done",
  columns: ["DEPARTMENT", "AMOUNT"],
  rows: [{ DEPARTMENT: "営業", AMOUNT: 1200 }],
  total: 1,
  has_more: false,
  truncated: false,
};

// 画面幅に関係しないので、題名の「（desktop）」で mobile-375 の project では実行しない（playwright.config.ts の grepInvert）。
test.describe("Run の表のデータ（desktop）", () => {
  test("表の形の結果だけを呼んだ順に取り出す（表でない結果・失敗・未完了は含めない）", () => {
    const tables = runToolResultTables(
      run([
        step("s1", "rag__rag_search", { answer: "回答", evidence: [{ evidence_id: "c1" }] }),
        step("s2", "nl2sql__nl2sql_query", JOB_DONE),
        step("s3", "custom__list", { rows: [{ a: 1 }] }, { status: "running" }),
        step("s4", "custom__failed", { rows: [{ a: 1 }] }, { status: "failed" }),
        step("s5", "custom__list", { rows: [{ name: "A" }, { name: "B" }] }),
      ])
    );
    expect(tables.map((table) => [table.stepId, table.toolName, table.data.rows.length])).toEqual([
      ["s2", "nl2sql__nl2sql_query", 1],
      ["s5", "custom__list", 2],
    ]);
  });

  test("同じジョブを取り直した結果は最後の結果だけを残す（実行中のジョブの空の結果は表にしない）", () => {
    const tables = runToolResultTables(
      run([
        step("s1", "nl2sql__nl2sql_query", { job_id: "job-1", status: "running", columns: [], rows: [] }),
        step("s2", "nl2sql__nl2sql_get_job", { ...JOB_DONE, rows: [] }),
        step("s3", "nl2sql__nl2sql_get_job", JOB_DONE),
      ])
    );
    expect(tables.map((table) => table.stepId)).toEqual(["s3"]);
  });

  test("成果物は回答・RAG の根拠を表にせず、構造化データ・表の形の JSON を表にする", () => {
    const artifact = (kind: string, content: Record<string, unknown>): Artifact => ({
      id: kind,
      name: kind,
      kind,
      content,
      created_at: T0,
    });
    expect(artifactTable(artifact("answer", { text: "回答", rows: [{ a: 1 }] }))).toBeNull();
    expect(artifactTable(artifact("rag_evidence", { rows: [{ a: 1 }] }))).toBeNull();
    expect(artifactTable(artifact("structured_table", JOB_DONE))?.columns.map((column) => column.name)).toEqual([
      "DEPARTMENT",
      "AMOUNT",
    ]);
    expect(artifactTable(artifact("json", { rows: [1] }))).toBeNull();
  });
});

// 添付などの組み込みツールも、保存した表示名を結果の表に使う。
test("結果の表はツールの表示名を優先する", () => {
  const value = run([step("s1", "chat_attachment_read_rows", { columns: ["VALUE"], rows: [[1]] })]);
  value.metadata = { tool_titles: { chat_attachment_read_rows: "添付の行の読み取り" } };
  expect(runToolResultTables(value)[0]?.toolName).toBe("添付の行の読み取り");
});
