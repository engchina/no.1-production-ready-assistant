import assert from "node:assert/strict";
import test from "node:test";

import { dbAdminObjectCountsFromPage } from "../src/features/nl2sql/dbAdminObjectCounts.ts";
import type { DbAdminObjectPage, DbAdminObjectSummary } from "../src/features/nl2sql/types.ts";

const object = (name: string, objectType = "table"): DbAdminObjectSummary => ({
  name,
  owner: "APP",
  object_type: objectType,
  row_count: null,
  comment: "",
});

test("DB admin object counts prefer API totals over the loaded page size", () => {
  const counts = dbAdminObjectCountsFromPage(
    {
      total: 151,
      table_count: 149,
      view_count: 2,
    } as DbAdminObjectPage,
    Array.from({ length: 100 }, (_, index) => object(`TABLE_${index}`))
  );

  assert.deepEqual(counts, {
    totalCount: 151,
    tableCount: 149,
    viewCount: 2,
  });
});

test("DB admin object counts fall back to loaded rows when API totals are absent", () => {
  const counts = dbAdminObjectCountsFromPage(undefined, [
    object("ORDERS"),
    object("ORDER_VIEW", "view"),
    object("ORDER_MVIEW", "materialized_view"),
  ]);

  assert.deepEqual(counts, {
    totalCount: 3,
    tableCount: 1,
    viewCount: 2,
  });
});
