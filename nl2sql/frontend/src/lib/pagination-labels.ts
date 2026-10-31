import {
  formatPaginationNumber,
  type ListCountLabels,
  type ListToolbarLabels,
  type PaginationLabels,
} from "@production-ready/ui";

import { t } from "./i18n";

/**
 * 共通の `Pagination` / `CursorPagination` / `PagedDataTable` に渡す NL2SQL の文言（件数の範囲・ページ番号の入力・
 * 最初へ / 前へ / 次へ / 最後へ・表示件数。#1266 / #1891）。文言は i18n（`pager.*`）、数値は共通の
 * `formatPaginationNumber`（桁区切り。3 製品で同じ書式）。画面ごとの aria-label は呼び出し側の `ariaLabel` で上書きする。
 */
export function paginationLabels(): PaginationLabels {
  const n = formatPaginationNumber;
  return {
    summary: ({ start, end, total }) => t("pager.range", { start: n(start), end: n(end), total: n(total) }),
    pageIndicator: (page, total) => t("pager.page", { page: n(page), total: n(total) }),
    pageCount: (total) => t("pager.pageCount", { total: n(total) }),
    cursorPage: (page) => t("pager.cursorPage", { page: n(page) }),
    pageInput: t("pager.pageInput"),
    pageOutOfRange: (total) => t("pager.outOfRange", { total: n(total) }),
    first: t("pager.first"),
    prev: t("pager.prev"),
    next: t("pager.next"),
    last: t("pager.last"),
    pageSize: t("pager.pageSize"),
    pageSizeOption: (size) => t("pager.pageSizeOption", { count: n(size) }),
    ariaLabel: t("pager.label"),
  };
}

/** 共通の `ListCount`（`ListToolbar` の `count`）に渡す文言（「全 N 件」「N 件（全 M 件）」。#1891）。 */
export function listCountLabels(): ListCountLabels {
  return {
    total: (total) => t("listCount.total", { total }),
    filtered: (matched, total) => t("listCount.filtered", { matched, total }),
  };
}

/** 共通の `ListToolbar` の「条件をクリア」・狭い置き場所の絞り込みのシートの文言（#1933）。 */
export function listToolbarLabels(): ListToolbarLabels {
  return {
    clearFilters: t("listToolbar.clearFilters"),
    filtersButton: (activeCount) =>
      activeCount > 0 ? t("listToolbar.filtersActive", { count: activeCount }) : t("listToolbar.filters"),
    filtersSheetTitle: t("listToolbar.filters"),
    filtersSheetClose: t("listToolbar.filtersClose"),
    showResults: (matched) =>
      matched === undefined ? t("listToolbar.showResults") : t("listToolbar.showMatched", { matched }),
  };
}
