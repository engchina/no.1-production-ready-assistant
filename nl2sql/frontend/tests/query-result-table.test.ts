import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  isIncompleteResult,
  queryResultCsvFilename,
  queryResultExecutionContextKey,
  toResultTableData,
} from "../src/features/nl2sql/queryResultTable.ts";

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../src/features/nl2sql");
const read = (path: string) => readFileSync(resolve(sourceRoot, path), "utf8");

test("NL2SQL の結果（列名をキーにした行）を共通の結果の表の形（列の順の値の配列）にする（#1178）", () => {
  const table = toResultTableData({
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

test("実行の接続は既定（deterministic）で VPD が無いときだけ出さない", () => {
  assert.equal(queryResultExecutionContextKey({}), null);
  assert.equal(queryResultExecutionContextKey({ execution_context: "deterministic" }), null);
  assert.equal(
    queryResultExecutionContextKey({ execution_context: "admin_control_plane" }),
    "queryResults.executionContext.admin_control_plane",
  );
  assert.equal(
    queryResultExecutionContextKey({ execution_context: "deterministic", vpd_context_enforced: true }),
    "queryResults.executionContext.deterministic",
  );
});

test("CSV のファイル名は画面ごとの接頭辞と利用者の地域の時刻", () => {
  assert.match(
    queryResultCsvFilename("nl2sql-direct-sql", new Date("2026-10-05T05:03:12Z")),
    /^nl2sql-direct-sql-\d{8}-\d{6}\.csv$/u,
  );
  // 時刻はシステムの設定の時間帯（既定 UTC。#3265・#3274）。
  assert.equal(
    queryResultCsvFilename("nl2sql-direct-sql", new Date("2026-10-05T05:03:12Z")),
    "nl2sql-direct-sql-20261005-050312.csv",
  );
  assert.equal(queryResultCsvFilename("nl2sql-result", new Date("invalid")), "nl2sql-result.csv");
});

test("データの結果は共通の ResultTable で出し、画面ごとの表・ページングを持たない（#1178）", () => {
  const wrapper = read("components/QueryResultTable.tsx");
  assert.match(wrapper, /<ResultTable\b/u);
  // 表・ページング・要約を製品で再実装しない。
  assert.doesNotMatch(wrapper, /<DataTable\b|<Pagination\b|usePagination\(/u);
  assert.equal(existsSync(resolve(sourceRoot, "components/resultTableColumns.tsx")), false);
  assert.equal(existsSync(resolve(sourceRoot, "components/Nl2SqlResultTable.tsx")), false);

  const dbAdmin = read("components/DbAdminShared.tsx");
  assert.doesNotMatch(dbAdmin, /function QueryResultsTable\b/u);
  assert.match(dbAdmin, /<QueryResultTable\b/u);

  for (const page of [
    "Nl2SqlWorkbench.tsx",
    "pages/DirectSqlPage.tsx",
    "pages/DataManagementPage.tsx",
    "pages/TableManagementPage.tsx",
  ]) {
    const source = read(page);
    assert.match(source, /<QueryResult(?:Table|Card)\b/u, page);
    assert.doesNotMatch(source, /QueryResultsTable|Nl2SqlResultTable/u, page);
  }
});
