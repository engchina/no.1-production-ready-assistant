import { bindWorkspaceOwner, clearWorkspaceDrafts } from "../src/lib/workspace-drafts";
import assert from "node:assert/strict";
import test from "node:test";

import {
  ACTIVE_JOB_ID_KEY,
  ACTIVE_JOB_SNAPSHOT_TTL_MS,
  ACTIVE_JOB_STARTED_AT_KEY,
  clearActiveJobSnapshot,
  scopedActiveJobStorage,
  hasLegacyActiveJobSnapshot,
  isJobInFlight,
  isJobTerminal,
  persistActiveJobSnapshot,
  readActiveJobSnapshot,
  type ActiveJobStorage,
} from "../src/features/nl2sql/jobPersistence.ts";
import { elapsedSecondsSince, formatElapsed } from "../src/features/nl2sql/operationTiming.ts";
import {
  elapsedMsBetween,
  formatElapsedClock,
  operationTimestampMs,
} from "../src/lib/operationTiming.ts";
import {
  adjustFixedSplitFraction,
  clampFixedSplitFraction,
  clampFixedSplitFractionToPaneWidths,
  fixedSplitFractionBounds,
  fixedSplitFractionForRatio,
  fixedSplitGridTemplateColumns,
  fixedSplitStateForFraction,
  fixedSplitStateForRatio,
  fixedSplitStorageKey,
  isFixedSplitRatio,
  nearestFixedSplitRatio,
  nextFixedSplitRatio,
  nextFixedSplitStateFromFraction,
  parseFixedSplitStorageValue,
  serializeFixedSplitState,
} from "@production-ready/ui";
import { FIXED_SPLIT_STORAGE_PREFIX } from "../src/lib/ui-store.ts";
import {
  previewExecutePayload,
  previewToJob,
  sqlExecutePayload,
} from "../src/features/nl2sql/previewState.ts";
import {
  querySqlGenerationUrl,
  parseNl2SqlEngine,
  prefillFromSearchParams,
} from "../src/features/nl2sql/queryPrefillState.ts";
import { formatSampleValues, formatSchemaCount } from "../src/features/nl2sql/schemaDisplayCore.ts";
import type { HistoryItem, PreviewData, SchemaColumn, SchemaTable } from "../src/features/nl2sql/types.ts";
import {
  buildSchemaInsertText,
  buildSchemaSqlIdentifierText,
  buildTableInsertText,
  buildTableSqlIdentifierText,
  emptySelection,
  insertTextAtRange,
  isSchemaEmptyError,
  leadingNewlinePrefix,
  normalizeObjectIdentifier,
  toAllowedObjects,
  toSchemaSelection,
  toggleColumnSelection,
  toggleTableSelection,
} from "../src/features/nl2sql/workbenchState.ts";

const invoiceColumn: SchemaColumn = {
  column_name: "TOTAL_AMOUNT",
  logical_name: "請求金額",
  data_type: "NUMBER",
  nullable: false,
  comment: "合計金額",
  sample_values: ["12000", "9800", "45100", "500"],
};

const invoiceTable: SchemaTable = {
  table_name: "INVOICES",
  logical_name: "請求",
  owner: "APP",
  table_type: "TABLE",
  comment: "請求データ",
  row_count: 12034,
  columns: [invoiceColumn],
  constraints: ["PK_INVOICES"],
};

class MemoryStorage implements ActiveJobStorage {
  readonly items = new Map<string, string>();
  get length() { return this.items.size; }
  key(index: number) { return [...this.items.keys()][index] ?? null; }

  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.items.set(key, value);
  }

  removeItem(key: string): void {
    this.items.delete(key);
  }
}

test("schema insertion uses denpyo-style logical table and column names", () => {
  assert.equal(buildSchemaInsertText(invoiceTable, invoiceColumn), "\"請求\".\"請求金額\"");
});

test("table insertion builds logical and physical table identifiers", () => {
  assert.equal(buildTableInsertText(invoiceTable), "\"請求\"");
  assert.equal(buildTableSqlIdentifierText(invoiceTable), "\"APP\".\"INVOICES\"");
});

test("fixed split pane stores ratio per page split id", () => {
  assert.equal(
    fixedSplitStorageKey("profile-management-oracle", FIXED_SPLIT_STORAGE_PREFIX),
    "production-ready-nl2sql.fixedSplitPane.profile-management-oracle"
  );
  assert.equal(isFixedSplitRatio("leftWide"), true);
  assert.equal(isFixedSplitRatio("freeResize"), false);
});

test("fixed split pane cycles equal to left-wide to right-wide", () => {
  assert.equal(nextFixedSplitRatio("equal"), "leftWide");
  assert.equal(nextFixedSplitRatio("leftWide"), "rightWide");
  assert.equal(nextFixedSplitRatio("rightWide"), "equal");
});

test("fixed split pane reads legacy and draggable storage values", () => {
  const legacy = parseFixedSplitStorageValue("leftWide");
  assert.equal(legacy.ratio, "leftWide");
  assert.ok(Math.abs(legacy.leftFraction - fixedSplitFractionForRatio("leftWide")) < 0.000001);

  const restored = parseFixedSplitStorageValue(serializeFixedSplitState(fixedSplitStateForFraction(0.6)));
  assert.equal(restored.ratio, "leftWide");
  assert.equal(restored.leftFraction, 0.6);

  const ratioOnly = parseFixedSplitStorageValue(JSON.stringify({ ratio: "rightWide" }));
  assert.deepEqual(ratioOnly, fixedSplitStateForRatio("rightWide"));

  assert.deepEqual(parseFixedSplitStorageValue("not-json"), fixedSplitStateForRatio("equal"));
});

test("fixed split pane starts at 50% when storage is absent and keeps stored ratios", () => {
  const equal = fixedSplitStateForRatio("equal");
  assert.deepEqual(parseFixedSplitStorageValue(null), equal);
  assert.deepEqual(parseFixedSplitStorageValue("leftWide"), fixedSplitStateForRatio("leftWide"));
  assert.deepEqual(parseFixedSplitStorageValue("not-json"), equal);
});

test("fixed split pane clamps dragged fraction and detects nearest fixed ratio", () => {
  assert.equal(clampFixedSplitFraction(0.1), 0.25);
  assert.equal(clampFixedSplitFraction(0.9), 0.75);
  assert.equal(nearestFixedSplitRatio(0.5), "equal");
  assert.equal(nearestFixedSplitRatio(0.63), "leftWide");
  assert.equal(nearestFixedSplitRatio(0.37), "rightWide");
});

test("fixed split pane double-click cycle rounds draggable fractions before advancing", () => {
  assert.deepEqual(nextFixedSplitStateFromFraction(0.5), fixedSplitStateForRatio("leftWide"));
  assert.deepEqual(
    nextFixedSplitStateFromFraction(fixedSplitFractionForRatio("leftWide")),
    fixedSplitStateForRatio("rightWide")
  );
  assert.deepEqual(
    nextFixedSplitStateFromFraction(fixedSplitFractionForRatio("rightWide")),
    fixedSplitStateForRatio("equal")
  );
  assert.deepEqual(nextFixedSplitStateFromFraction(0.6), fixedSplitStateForRatio("rightWide"));
});

test("fixed split pane keyboard adjustment uses pixel-equivalent fractions", () => {
  assert.equal(adjustFixedSplitFraction(0.5, 24, 1000), 0.524);
  assert.equal(adjustFixedSplitFraction(0.5, -72, 1000), 0.428);
  assert.equal(adjustFixedSplitFraction(0.5, -400, 1000), 0.25);
});

test("fixed split pane keeps both panes outside the reserved divider track", () => {
  assert.deepEqual(fixedSplitFractionBounds(1000, 320, 400), {
    minFraction: 0.32,
    maxFraction: 0.6,
  });
  assert.equal(clampFixedSplitFractionToPaneWidths(0.25, 1000, 320, 400), 0.32);
  assert.equal(clampFixedSplitFractionToPaneWidths(0.75, 1000, 320, 400), 0.6);
  assert.deepEqual(fixedSplitFractionBounds(700, 400, 400), {
    minFraction: 0.5,
    maxFraction: 0.5,
  });
});

test("fixed split pane grid templates use equal and golden-ratio tracks", () => {
  assert.equal(fixedSplitGridTemplateColumns("equal"), "minmax(0, 1fr) 14px minmax(0, 1fr)");
  assert.match(fixedSplitGridTemplateColumns("leftWide"), /1\.618fr/);
  assert.match(fixedSplitGridTemplateColumns("rightWide"), /1\.618fr/);
  assert.equal(fixedSplitGridTemplateColumns(0.6), "minmax(0, 0.6fr) 14px minmax(0, 0.4fr)");
});

test("schema SQL insertion uses quoted physical table and column names", () => {
  assert.equal(
    buildSchemaSqlIdentifierText(invoiceTable, invoiceColumn),
    "\"APP\".\"INVOICES\".\"TOTAL_AMOUNT\""
  );
});

test("selection converts to allowed_objects without empty column arrays", () => {
  const selection = {
    tableNames: ["INVOICES", "CUSTOMERS"],
    columns: {
      INVOICES: ["TOTAL_AMOUNT"],
      CUSTOMERS: [],
    },
  };

  assert.deepEqual(toAllowedObjects(selection), {
    table_names: ["INVOICES", "CUSTOMERS"],
    columns: {
      INVOICES: ["TOTAL_AMOUNT"],
    },
  });
});

test("schema selection round-trips backend allowed_objects", () => {
  assert.deepEqual(
    toSchemaSelection({
      table_names: ["INVOICES"],
      columns: { INVOICES: ["TOTAL_AMOUNT"] },
    }),
    {
      tableNames: ["INVOICES"],
      columns: { INVOICES: ["TOTAL_AMOUNT"] },
    }
  );
});

test("table and column toggles keep allowed table scope in sync", () => {
  const selectedTable = toggleTableSelection(emptySelection(), "INVOICES");
  assert.deepEqual(selectedTable.tableNames, ["INVOICES"]);

  const selectedColumn = toggleColumnSelection(emptySelection(), "INVOICES", "TOTAL_AMOUNT");
  assert.deepEqual(selectedColumn, {
    tableNames: ["INVOICES"],
    columns: { INVOICES: ["TOTAL_AMOUNT"] },
  });

  const removedColumn = toggleColumnSelection(selectedColumn, "INVOICES", "TOTAL_AMOUNT");
  assert.deepEqual(removedColumn, {
    tableNames: ["INVOICES"],
    columns: { INVOICES: [] },
  });
});

test("question insertion replaces selected text at the cursor range", () => {
  assert.equal(
    insertTextAtRange("未入金の金額を見たい", "\"請求\".\"請求金額\"", 4, 6),
    "未入金の\"請求\".\"請求金額\"を見たい"
  );
});

test("object identifier normalization keeps owner, uppercases unquoted parts and keeps quoted case (#561)", () => {
  assert.equal(normalizeObjectIdentifier("EMPLOYEE"), "EMPLOYEE");
  assert.equal(normalizeObjectIdentifier("employee"), "EMPLOYEE");
  assert.equal(normalizeObjectIdentifier("APP.EMPLOYEE"), "APP.EMPLOYEE");
  assert.equal(normalizeObjectIdentifier('"EMPLOYEE"'), "EMPLOYEE");
  assert.equal(normalizeObjectIdentifier('"APP"."EMPLOYEE"'), "APP.EMPLOYEE");
  // 引用名は大文字の同名表と別のキー。
  assert.equal(normalizeObjectIdentifier('app."Employee"'), 'APP."Employee"');
  assert.notEqual(normalizeObjectIdentifier('APP."Employee"'), normalizeObjectIdentifier("APP.EMPLOYEE"));
});

test("schema insert prepends a newline only when not at start / not after a newline", () => {
  // 先頭挿入は改行なし（先頭空行を防ぐ）
  assert.equal(leadingNewlinePrefix("", 0), "");
  assert.equal(leadingNewlinePrefix("既存の質問", 0), "");
  // 途中/末尾は各項目を改行区切りにするため改行を前置
  assert.equal(leadingNewlinePrefix("既存の質問", 5), "\n");
  assert.equal(leadingNewlinePrefix("既存\"請求\"", 2), "\n");
  // 直前がすでに改行なら二重改行にしない
  assert.equal(leadingNewlinePrefix("既存\n", 3), "");
  // 直前が全角コロン（穴埋めテンプレートの「ラベル：」直後）はインラインで埋める
  assert.equal(leadingNewlinePrefix("対象テーブル：", 7), "");
  // 直前が等号・比較演算子ならインライン
  assert.equal(leadingNewlinePrefix("部署=", 3), "");
  assert.equal(leadingNewlinePrefix("売上>", 3), "");
  // 直前が開き括弧・カンマならインライン
  assert.equal(leadingNewlinePrefix("IN(", 3), "");
  assert.equal(leadingNewlinePrefix("A,", 2), "");
  // 直前が空白（半角/全角）ならインライン
  assert.equal(leadingNewlinePrefix("部署 ", 3), "");
  assert.equal(leadingNewlinePrefix("部署　", 3), "");
  // 上記以外（閉じ引用符・和字）は改行を前置
  assert.equal(leadingNewlinePrefix('既存"請求"', 6), "\n");
});

test("elapsed time helpers format live and final timings", () => {
  assert.equal(elapsedSecondsSince(1_000, 4_499), 3);
  assert.equal(elapsedSecondsSince(5_000, 4_000), 0);
  assert.equal(formatElapsed(null), "-");
  assert.equal(formatElapsed(850), "850ms");
  assert.equal(formatElapsed(1_250), "1.3秒");
  assert.equal(formatElapsedClock(59_000), "00:59");
  assert.equal(formatElapsedClock(60_000), "01:00");
  assert.equal(formatElapsedClock(3_723_000), "1:02:03");
});

test("elapsed time helpers normalize server timestamps and completed durations", () => {
  assert.equal(operationTimestampMs("2026-07-29T00:00:00.000Z"), 1_785_283_200_000);
  assert.equal(operationTimestampMs("invalid"), null);
  assert.equal(operationTimestampMs(Number.NaN), null);
  assert.equal(
    elapsedMsBetween("2026-07-29T00:00:00.000Z", "2026-07-29T00:00:01.250Z"),
    1_250,
  );
  assert.equal(elapsedMsBetween(null, null), null);
});

test("schema metadata helpers format counts and sample values for compact UI", () => {
  assert.equal(formatSchemaCount(invoiceTable.row_count), "12,034");
  assert.equal(formatSchemaCount(null), "-");
  assert.equal(formatSampleValues(invoiceColumn.sample_values), "12000, 9800, 45100");
});

test("SQL execution payload trims SQL and preserves execution scope", () => {
  assert.deepEqual(
    sqlExecutePayload(
      " SELECT TOTAL_AMOUNT FROM INVOICES ",
      { table_names: ["INVOICES"], columns: { INVOICES: ["TOTAL_AMOUNT"] } }
    ),
    {
      sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
      allowed_objects: { table_names: ["INVOICES"], columns: { INVOICES: ["TOTAL_AMOUNT"] } },
    }
  );
});

test("active job persistence restores and clears polling state", () => {
  const storage = new MemoryStorage();
  assert.equal(readActiveJobSnapshot(storage, 123), null);

  persistActiveJobSnapshot(storage, "job-123", 1_700_000);
  assert.deepEqual(readActiveJobSnapshot(storage, 123), {
    jobId: "job-123",
    startedAtMs: 1_700_000,
  });

  storage.setItem(ACTIVE_JOB_STARTED_AT_KEY, "not-a-number");
  assert.deepEqual(readActiveJobSnapshot(storage, 555), {
    jobId: "job-123",
    startedAtMs: 555,
  });
  // 欠損した startedAt は現在時刻で書き戻され、以後 TTL 判定が効く。
  assert.equal(storage.getItem(ACTIVE_JOB_STARTED_AT_KEY), "555");

  clearActiveJobSnapshot(storage, "job-123");
  assert.equal(storage.getItem(ACTIVE_JOB_ID_KEY), null);
  assert.equal(storage.getItem(ACTIVE_JOB_STARTED_AT_KEY), null);
});

test("cleanup from an idle page or an older job preserves another job snapshot", () => {
  const storage = new MemoryStorage();
  persistActiveJobSnapshot(storage, "job-other-tab", 1_700_000);

  for (const expectedJobId of [null, "job-completed"]) {
    clearActiveJobSnapshot(storage, expectedJobId);
    assert.equal(storage.getItem(ACTIVE_JOB_ID_KEY), "job-other-tab");
    assert.equal(storage.getItem(ACTIVE_JOB_STARTED_AT_KEY), "1700000");
  }
});

test("schema-empty error is detected by error_code first, message fragment as fallback", () => {
  // code がある場合は code のみで判定する(文言変更に影響されない)。
  assert.equal(
    isSchemaEmptyError({ message: "別の文言", code: "SCHEMA_CATALOG_EMPTY" }),
    true
  );
  assert.equal(
    isSchemaEmptyError({ message: "Schema catalog が空です。", code: "OTHER_CODE" }),
    false
  );
  // 移行期フォールバック: code が無い応答は従来の文言一致で判定する。
  assert.equal(isSchemaEmptyError({ message: "Schema catalog が空です。refresh してください。" }), true);
  assert.equal(isSchemaEmptyError({ message: "権限がありません。" }), false);
});

test("active job snapshot within TTL is restored", () => {
  const storage = new MemoryStorage();
  const startedAt = 1_000_000;
  persistActiveJobSnapshot(storage, "job-ttl-ok", startedAt);

  const now = startedAt + ACTIVE_JOB_SNAPSHOT_TTL_MS;
  assert.deepEqual(readActiveJobSnapshot(storage, now), {
    jobId: "job-ttl-ok",
    startedAtMs: startedAt,
  });
});

test("active job snapshot past TTL is discarded and storage is cleared", () => {
  const storage = new MemoryStorage();
  const startedAt = 1_000_000;
  persistActiveJobSnapshot(storage, "job-ttl-expired", startedAt);

  const now = startedAt + ACTIVE_JOB_SNAPSHOT_TTL_MS + 1;
  assert.equal(readActiveJobSnapshot(storage, now), null);
  assert.equal(storage.getItem(ACTIVE_JOB_ID_KEY), null);
  assert.equal(storage.getItem(ACTIVE_JOB_STARTED_AT_KEY), null);
});

test("job status helpers identify in-flight and terminal states", () => {
  assert.equal(isJobInFlight("pending"), true);
  assert.equal(isJobInFlight("running"), true);
  assert.equal(isJobInFlight("done"), false);
  assert.equal(isJobTerminal("done"), true);
  assert.equal(isJobTerminal("error"), true);
  assert.equal(isJobTerminal("running"), false);
});

test("preview response maps to a synthetic timeline job", () => {
  const preview: PreviewData = {
    sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    is_safe: true,
    row_limit: 0,
    note: "preview ready",
    engine: "select_ai",
    engine_meta: { profile_name: "P" },
    fallback_reason: "",
    rewritten_question: "請求金額を一覧で見る",
    executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    safety: null,
    recommendations: ["SELECT-only です。"],
    repaired_sql: "",
    optimization_hints: [],
    timing: null,
  };

  const job = previewToJob(preview, "請求金額を一覧で見たい");

  // 生成〜安全確認は完了、実行・整形はプレビュー時点では未実行(skipped)。
  assert.equal(job.job_id, "preview");
  assert.equal(job.status, "done");
  assert.deepEqual(
    job.steps.map((s) => [s.stage, s.status]),
    [
      ["prepare_context", "done"],
      ["generate_sql", "done"],
      ["safety_check", "done"],
      ["execute_sql", "skipped"],
      ["format_results", "skipped"],
    ]
  );
  // タイムライン `generate_sql` ステップが参照する result の主要フィールド。
  assert.equal(job.result?.original_question, "請求金額を一覧で見たい");
  assert.equal(job.result?.generated_sql, "SELECT TOTAL_AMOUNT FROM INVOICES");
  assert.equal(job.result?.explanation, "preview ready");
  assert.equal(job.result?.rewritten_question, "請求金額を一覧で見る");
  assert.deepEqual(job.result?.recommendations, ["SELECT-only です。"]);
  // safety 未指定時は preview からフォールバックを合成する。
  assert.equal(job.result?.safety.is_safe, true);
  assert.equal(job.result?.safety.is_select_only, true);
  // まだ実行していないため結果は空。
  assert.deepEqual(job.result?.results, { columns: [], rows: [], total: 0 });
});

test("Select AI の SQL で省略した安全性の確認は、プレビューのタイムラインでも未実行（#1523）", () => {
  const preview: PreviewData = {
    sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    is_safe: true,
    row_limit: 0,
    note: "preview ready",
    engine: "select_ai",
    engine_meta: {},
    fallback_reason: "",
    rewritten_question: "",
    executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    safety: {
      is_safe: true,
      is_select_only: true,
      row_limit_applied: 0,
      blocked_reason: "",
      warnings: [],
      referenced_tables: ["APP.INVOICES"],
      referenced_columns: [],
      safety_checked: false,
      safety_check_skip_reason: "Select AI が生成した SELECT 文のため省略しました。",
    },
    recommendations: [],
    repaired_sql: "",
    optimization_hints: [],
    timing: null,
  };

  const job = previewToJob(preview, "請求金額を一覧で見たい");

  assert.equal(job.steps.find((step) => step.stage === "safety_check")?.status, "skipped");
});

test("preview execute payload preserves selected allowed objects", () => {
  assert.deepEqual(
    previewExecutePayload(
      " SELECT TOTAL_AMOUNT FROM INVOICES ",
      { table_names: ["INVOICES"], columns: { INVOICES: ["TOTAL_AMOUNT"] } }
    ),
    {
      sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
      allowed_objects: {
        table_names: ["INVOICES"],
        columns: { INVOICES: ["TOTAL_AMOUNT"] },
      },
    }
  );
});

test("history rerun URL and query prefill preserve question, engine, and profile", () => {
  const item: HistoryItem = {
    id: "h1",
    question: "請求 金額を再実行",
    engine: "select_ai_agent",
    generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    created_at: "2026-01-01T00:00:00Z",
    elapsed_ms: 12,
    feedback_rating: null,
    profile_id: "finance",
    profile_name: "経理",
    rewritten_question: "請求金額",
    executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES FETCH FIRST 100 ROWS ONLY",
    safety_is_safe: true,
    result_row_count: 1,
    result_columns: ["TOTAL_AMOUNT"],
    feedback_comment: "",
  };

  const url = querySqlGenerationUrl(item);
  assert.equal(url, "/query?question=%E8%AB%8B%E6%B1%82+%E9%87%91%E9%A1%8D%E3%82%92%E5%86%8D%E5%AE%9F%E8%A1%8C&engine=select_ai_agent&profile_id=finance");
  assert.deepEqual(prefillFromSearchParams(new URLSearchParams(url.split("?")[1])), {
    question: "請求 金額を再実行",
    engine: "select_ai_agent",
    profileId: "finance",
  });
  assert.equal(parseNl2SqlEngine("bad_engine"), null);
  // 廃止済みの "auto" は prefill でも受理しない(既定エンジンに倒す)。
  assert.equal(parseNl2SqlEngine("auto"), null);
});

test("job snapshots isolate owners and databases and cannot return after logout", () => {
  const session = new MemoryStorage();
  bindWorkspaceOwner(session, "alice");
  const aliceDb1 = scopedActiveJobStorage(session, "alice", "db1");
  const aliceDb2 = scopedActiveJobStorage(session, "alice", "db2");
  persistActiveJobSnapshot(aliceDb1, "job-db1", 1000);
  persistActiveJobSnapshot(aliceDb2, "job-db2", 1001);
  assert.equal(readActiveJobSnapshot(aliceDb1, 1100)?.jobId, "job-db1");
  assert.equal(readActiveJobSnapshot(aliceDb2, 1100)?.jobId, "job-db2");
  clearActiveJobSnapshot(aliceDb2, "job-db2");
  assert.equal(readActiveJobSnapshot(aliceDb1, 1100)?.jobId, "job-db1");
  bindWorkspaceOwner(session, "bob");
  const bob = scopedActiveJobStorage(session, "bob", "db1");
  assert.equal(readActiveJobSnapshot(bob, 1100), null);
  assert.equal(readActiveJobSnapshot(aliceDb1, 1100), null);
  assert.throws(() => persistActiveJobSnapshot(aliceDb1, "late-job", 1100));
  persistActiveJobSnapshot(bob, "bob-job", 1200);
  clearWorkspaceDrafts(session);
  assert.equal(session.length, 0);
  assert.throws(() => persistActiveJobSnapshot(bob, "late-job", 1300));
});

test("legacy detection is read-only and excludes expired shared records", () => {
  const local = new MemoryStorage();
  assert.equal(hasLegacyActiveJobSnapshot(local, 1000), false);
  persistActiveJobSnapshot(local, "legacy-job", 1000);
  assert.equal(hasLegacyActiveJobSnapshot(local, 1001), true);
  assert.equal(hasLegacyActiveJobSnapshot(local, 1001 + ACTIVE_JOB_SNAPSHOT_TTL_MS), false);
  assert.equal(local.getItem(ACTIVE_JOB_ID_KEY), "legacy-job");
  assert.equal(local.getItem(ACTIVE_JOB_STARTED_AT_KEY), "1000");
});
