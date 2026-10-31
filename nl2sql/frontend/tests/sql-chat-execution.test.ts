import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  chatResultCsvFilename,
  directSqlPrefill,
  directSqlPrefillState,
  isIncompleteResult,
  lastExecutionText,
  toChatResultTable,
} from "../src/features/nl2sql/chatSqlExecution.ts";

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../src/features/nl2sql");

test("NL2SQL の結果（列名をキーにした行）を共通の結果の表の形（列の順の値の配列）にする", () => {
  const table = toChatResultTable({
    columns: ["ID", "NAME", "NOTE"],
    rows: [
      { ID: 1, NAME: "A", NOTE: null },
      { ID: 2, NAME: "B" },
    ],
  });
  assert.deepEqual(table.columns, [{ name: "ID" }, { name: "NAME" }, { name: "NOTE" }]);
  // 欠けた列は NULL として渡す（空文字と区別する）。
  assert.deepEqual(table.rows, [
    [1, "A", null],
    [2, "B", null],
  ]);
  assert.equal(isIncompleteResult({ has_more: true }), true);
  assert.equal(isIncompleteResult({ truncated: true }), true);
  assert.equal(isIncompleteResult({}), false);
});

test("会話を開き直したときは前回の実行の要約と「もう一度実行」の案内を出す", () => {
  const summary = {
    status: "done" as const,
    executed_at: "2026-10-05T05:03:12Z",
    elapsed_ms: 800,
    row_count: 1200,
    column_count: 5,
    has_more: false,
    history_id: "history-1",
  };
  assert.equal(
    lastExecutionText(summary, "10/5 14:03"),
    "前回の実行（10/5 14:03）: 1,200 行・5 列。結果の行は保存していないため、見るにはもう一度実行してください。",
  );
  assert.match(lastExecutionText({ ...summary, has_more: true }, "10/5 14:03"), /先頭の 1,200 行を取得（さらに行があります）/u);
  assert.match(lastExecutionText({ ...summary, status: "error" }, "10/5 14:03"), /失敗しました/u);
  // 他の利用者の会話を見ているとき（実行は持ち主だけ。#3244）は「もう一度実行」を案内しない。
  assert.equal(
    lastExecutionText(summary, "10/5 14:03", { readOnly: true }),
    "前回の実行（10/5 14:03）: 1,200 行・5 列。結果の行は保存していません。",
  );
  for (const variant of [{ ...summary, has_more: true }, { ...summary, status: "error" as const }]) {
    assert.doesNotMatch(lastExecutionText(variant, "10/5 14:03", { readOnly: true }), /もう一度実行/u);
  }
});

test("CSV のファイル名と「SELECT SQL を実行」への受け渡し（URL に SQL を載せない）", () => {
  assert.match(chatResultCsvFilename("2026-10-05T05:03:12Z"), /^nl2sql-chat-result-\d{8}-\d{6}\.csv$/u);
  assert.equal(chatResultCsvFilename("invalid"), "nl2sql-chat-result.csv");
  assert.equal(directSqlPrefill(directSqlPrefillState(" SELECT 1 FROM DUAL ")), "SELECT 1 FROM DUAL");
  assert.equal(directSqlPrefill(null), "");
  assert.equal(directSqlPrefill({ prefillSql: 1 }), "");
});

test("チャットは共通の結果の表（ResultTable。旧名 ChatResultTable）を使う", () => {
  // 画面のデータの結果も同じ部品（components/QueryResultTable.tsx。tests/query-result-table.test.ts、#1178）。
  const chat = readFileSync(resolve(sourceRoot, "components/ChatSqlExecution.tsx"), "utf8");
  assert.match(chat, /<(?:Chat)?ResultTable\b/u);
});
