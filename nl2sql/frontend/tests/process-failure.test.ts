// チャットのジョブの失敗を、3 製品共通の失敗のカード（ProcessFailureNotice）の形に写す（#2342）。
import assert from "node:assert/strict";
import test from "node:test";

import { chatProgressStepsFromEvents, type ChatProgressEvent } from "@production-ready/ui";

import { CHAT_PROGRESS_STEP_DEFINITIONS } from "../src/features/nl2sql/chatProgress.ts";
import { chatJobFailure } from "../src/features/nl2sql/processFailure.ts";

const ROOT = "0".repeat(16);
/** 処理の経過の span のイベント（#3226。実行の全体の下に工程の span、生成の中にモデルの呼び出し）。 */
const event = (seq: number, step_id: string, status: "running" | "done" | "failed", value: Partial<ChatProgressEvent> = {}) =>
  ({
    schema_version: 1, seq, target_id: "job-1", attempt: 1, emitted_at: "", type: "step", step_id, status,
    parent_id: ROOT, trace_id: "d".repeat(32), ...value,
  }) as ChatProgressEvent;
// 工程の定義で名前を付けた一覧（失敗した段の名前は「SQL を生成できませんでした」、名詞の名前は「SQL の生成」）。
const steps = chatProgressStepsFromEvents(
  [
    event(1, ROOT, "running", { name: "nl2sql.execute", root: true, parent_id: undefined }),
    event(2, "1".padStart(16, "0"), "done", { kind: "prepare_context", name: "nl2sql.prepare_context" }),
    event(3, "2".padStart(16, "0"), "failed", { kind: "generate_sql", name: "nl2sql.generate_sql" }),
    event(4, "3".padStart(16, "0"), "failed", { name: "gen_ai.chat", parent_id: "2".padStart(16, "0") }),
  ],
  CHAT_PROGRESS_STEP_DEFINITIONS
);

test("止まった段は失敗した段の名詞の名前（状態の文ではない）", () => {
  assert.equal(steps[1].label, "SQL を生成できませんでした");
  assert.equal(steps[1].name, "SQL の生成");
  // 段の中の span（モデルの呼び出し）は入れ子。
  assert.equal(steps[2].level, 1);
});

test("1 文目に止まった段、本文に backend の文、詳細にコード・ジョブ ID・時刻・元のメッセージ", () => {
  const failure = chatJobFailure(
    {
      job_id: "job-1",
      error_message: "SQL を生成できませんでした。",
      error_code: "SQL_GENERATION_FAILED",
      error_detail: "ORA-00942: table or view does not exist",
      finished_at: "2026-10-18T06:30:41Z",
    },
    steps
  );
  // 段の中の span が失敗しても、止まった段は工程の段の名前。
  assert.equal(failure.summary, "「SQL の生成」で処理が止まりました。");
  assert.equal(failure.cause, "SQL を生成できませんでした。");
  assert.match(failure.nextAction ?? "", /ジョブ ID を添えて管理者に連絡/);
  // 再試行の要否は backend の文に任せる（「もう一度」を足さない）。
  assert.doesNotMatch(failure.nextAction ?? "", /もう一度/);
  assert.equal(failure.failedStep, "SQL の生成");
  assert.equal(failure.errorCode, "SQL_GENERATION_FAILED");
  assert.equal(failure.traceId, "job-1");
  assert.deepEqual(failure.extraDetails, [
    { label: "元のメッセージ", value: "ORA-00942: table or view does not exist" },
  ]);
});

test("段の分からない失敗は定型の 1 文目", () => {
  const failure = chatJobFailure({ job_id: "job-2", error_message: "失敗しました。" }, []);
  assert.equal(failure.summary, "SQL の生成と実行ができませんでした。");
  assert.deepEqual(failure.extraDetails, []);
});
