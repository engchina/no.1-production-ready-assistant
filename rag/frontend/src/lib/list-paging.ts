import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS, type ListCountProps } from "@production-ready/ui";

import { listCountLabels } from "./pagination-labels";
import { useWorkspaceState, type WorkspaceField } from "./workspace-state";

/**
 * 一覧の件数とページの表示件数の、RAG の画面に共通の小さな規則（#1891）。
 * 部品（`ListCount` / `Pagination`）は `@production-ready/ui` にあり、ここは製品の状態から props を作るだけ。
 */

/** 作業状態に残した表示件数が、選べる段（10 / 25 / 50 / 100）のどれかか。 */
export function isPageSize(value: unknown): value is number {
  return typeof value === "number" && (PAGE_SIZE_OPTIONS as readonly number[]).includes(value);
}

/**
 * `ListToolbar` の `count`（`ListCount`）の props を作る。
 *
 * - `matchedTotal`: 今の条件（検索語・絞り込み）に一致した全件（API の `total`）。未取得なら件数を出さない。
 * - `filtered`: 検索語・絞り込みを掛けているか。掛けていなければ「全 N 件」。
 * - `allTotal`: 条件を外した全件（絞り込み中だけ別に数える）。未取得の間は件数を出さない（「全 ? 件」を出さない）。
 *
 * 一覧の検索欄（`SearchField`）の `resultCountLabel` が同じ件数を読み上げるので、`live` は false（二重に読ませない）。
 */
export function listCountProps({
  matchedTotal,
  filtered,
  allTotal,
  live = false,
}: {
  matchedTotal: number | undefined;
  filtered: boolean;
  allTotal?: number | undefined;
  live?: boolean;
}): ListCountProps | undefined {
  if (matchedTotal === undefined) return undefined;
  if (!filtered) return { total: matchedTotal, labels: listCountLabels(), live };
  if (allTotal === undefined) return undefined;
  // 条件を外した件数の取得と一覧の取得の間に件数が変わっても、一致した件数が全件を超えて見えないようにする。
  return { total: Math.max(allTotal, matchedTotal), matched: matchedTotal, labels: listCountLabels(), live };
}

/** クライアント側で全件を持つ一覧（`PagedDataTable`）のページと表示件数の作業状態。 */
export interface ClientListPaging {
  /** どの一覧の分か（検索・回答プロファイルの ID・絞り込みなど）。変わったら 1 ページ目から。 */
  scope: string;
  page: number;
  pageSize: number;
}

function isClientListPaging(value: unknown): value is ClientListPaging {
  const paging = value as ClientListPaging;
  return (
    typeof paging === "object" &&
    paging !== null &&
    typeof paging.scope === "string" &&
    Number.isInteger(paging.page) &&
    paging.page >= 1 &&
    isPageSize(paging.pageSize)
  );
}

/**
 * `PagedDataTable` のページ番号と表示件数を作業状態（sessionStorage）に残す（AGENTS.md「ページ番号は作業状態に残す」。
 * #1891）。`scope`（対象・絞り込み）が変わったら 1 ページ目に戻し、表示件数は保つ。
 * 戻り値はそのまま `PagedDataTable` に渡す（`page` / `onPageChange` / `pageSize` / `onPageSizeChange` / `resetKey`）。
 */
export function useClientListPaging(field: WorkspaceField, scope: string) {
  const [paging, setPaging] = useWorkspaceState<ClientListPaging>(
    field,
    { scope, page: 1, pageSize: DEFAULT_PAGE_SIZE },
    isClientListPaging
  );
  const page = paging.scope === scope ? paging.page : 1;
  return {
    page,
    onPageChange: (next: number) => setPaging((current) => ({ ...current, scope, page: next })),
    pageSize: paging.pageSize,
    onPageSizeChange: (next: number) => setPaging({ scope, page: 1, pageSize: next }),
    resetKey: scope,
  };
}
