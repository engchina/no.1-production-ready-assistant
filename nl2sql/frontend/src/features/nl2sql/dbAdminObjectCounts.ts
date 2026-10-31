import type { DbAdminObjectPage, DbAdminObjectSummary } from "./types";

export interface DbAdminObjectCounts {
  totalCount: number;
  tableCount: number;
  viewCount: number;
}

function finiteCount(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function dbAdminObjectCountsFromPage(
  page: Pick<DbAdminObjectPage, "total" | "table_count" | "view_count"> | undefined,
  loadedItems: DbAdminObjectSummary[]
): DbAdminObjectCounts {
  const loadedTableCount = loadedItems.filter(
    (item) => !item.object_type.toLowerCase().includes("view")
  ).length;
  const loadedViewCount = loadedItems.length - loadedTableCount;
  return {
    totalCount: Math.max(finiteCount(page?.total, loadedItems.length), loadedItems.length),
    tableCount: Math.max(finiteCount(page?.table_count, loadedTableCount), loadedTableCount),
    viewCount: Math.max(finiteCount(page?.view_count, loadedViewCount), loadedViewCount),
  };
}
