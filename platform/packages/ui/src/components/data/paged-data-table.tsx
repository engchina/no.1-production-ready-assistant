import { useMemo, useState } from "react";

import { INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_ROW_CLASS, INFORMATION_TABLE_VISIBLE_ROWS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { DataTable, type DataTableProps } from "./data-table";
import { DEFAULT_PAGE_SIZE, Pagination, usePagination, type PaginationLabels } from "./pagination";

export type { PaginationLabels } from "./pagination";

export type PagedDataTableProps<T> = Omit<DataTableProps<T>, "stickyHeader" | "visibleRows"> & {
  /** Pagination の文言（省略時は共通の既定 `DEFAULT_PAGINATION_LABELS`。製品は i18n の文字列を渡す）。 */
  paginationLabels?: Partial<PaginationLabels>;
  /** 1 ページの件数（既定 25 件）。`onPageSizeChange` と一緒に渡すと制御式の表示件数になる。 */
  pageSize?: number;
  /**
   * 表示件数の選択肢（例 `PAGE_SIZE_OPTIONS`）。渡すとページ送りの左に表示件数の選択を出す（#1891）。
   * `onPageSizeChange` が無ければ、件数は部品が持つ（初期値は `pageSize`）。
   */
  pageSizeOptions?: readonly number[];
  /** 表示件数を変えたとき（制御式。作業状態に残すときに渡す）。変えると 1 ページ目へ戻る。 */
  onPageSizeChange?: (size: number) => void;
  /**
   * 1 ページ目へ戻す契機（検索語・絞り込み・対象の切り替えなど）。
   * 省略時もページを保つ（定期的な再取得で行が変わってもページを戻さない。行が減って範囲外になったら表示だけ末尾に寄せる）。
   */
  resetKey?: unknown;
  /** 制御式のページ番号（作業状態に保持するとき。UX 契約 workspace-state.md）。 */
  page?: number;
  onPageChange?: (page: number) => void;
  /** ページ送りの位置。結果シートでは表より前に置き、開いた直後に操作できるようにする。 */
  paginationPosition?: "top" | "bottom";
  /** 1 ページでもページ送りを表示する。 */
  showSinglePagePagination?: boolean;
  paginationTestId?: string;
  paginationAriaLabel?: string;
  /** list は実測の 5 / 8 行。bounded は長いセルも共通の高さ上限で止める。page は親でスクロールする。 */
  viewport?: "list" | "page" | "bounded";
};

/**
 * クライアント側で全件を持つ一覧の標準形（#265、NL2SQL が基準）。
 * 表頭を固定し、md 未満 5 行・md 以上 8 行で表の中を縦スクロールにして、直下に共通の Pagination（既定 25 件/ページ）を置く。
 * 1 ページしかないときは Pagination を出さない（表示件数の選択があるときは範囲と選択だけ）。行の最小高さは
 * `INFORMATION_TABLE_ROW_CLASS`。件数は表の上の `ListToolbar` の `ListCount` が出す（#1891）。
 */
export function PagedDataTable<T>({
  rows,
  rowProps,
  paginationLabels,
  pageSize = DEFAULT_PAGE_SIZE,
  pageSizeOptions,
  onPageSizeChange,
  resetKey,
  page: controlledPage,
  onPageChange,
  paginationTestId,
  paginationAriaLabel,
  paginationPosition = "bottom",
  showSinglePagePagination = false,
  viewport = "list",
  className,
  ...props
}: PagedDataTableProps<T>) {
  const [innerPageSize, setInnerPageSize] = useState(pageSize);
  // 表示件数の持ち主: onPageSizeChange があれば呼び出し側（制御式）、pageSizeOptions だけなら部品、どちらも無ければ
  // pageSize の prop（ResultTable のように外の選択欄で件数を変える一覧）。
  const size = onPageSizeChange || !pageSizeOptions ? pageSize : innerPageSize;
  const selectable = Boolean(pageSizeOptions) || Boolean(onPageSizeChange);
  // 表示件数を変えたら 1 ページ目へ戻す（resetKey に件数を足す。resetKey を省いたときは、行が変わってもページを戻さない）。
  const combinedResetKey = useMemo(() => ({ resetKey, size }), [resetKey, size]);
  const { page, setPage, totalPages, pageItems, range } = usePagination(rows, size, {
    page: controlledPage,
    onPageChange,
    resetKey: selectable ? combinedResetKey : resetKey,
  });
  const pagination = (
    <Pagination
      page={page}
      totalPages={totalPages}
      range={range}
      onPageChange={setPage}
      pageSize={selectable ? size : undefined}
      pageSizeOptions={pageSizeOptions}
      onPageSizeChange={selectable ? (next) => (onPageSizeChange ?? setInnerPageSize)(next) : undefined}
      labels={paginationLabels}
      ariaLabel={paginationAriaLabel}
      showSinglePage={showSinglePagePagination}
      testId={paginationTestId}
    />
  );
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-2">
      {paginationPosition === "top" ? pagination : null}
      <DataTable<T>
        {...props}
        rows={pageItems}
        rowProps={(row, index) => {
          const extra = rowProps?.(row, index);
          return { ...extra, className: cn(INFORMATION_TABLE_ROW_CLASS, extra?.className) };
        }}
        stickyHeader
        visibleRows={viewport === "list" ? INFORMATION_TABLE_VISIBLE_ROWS : undefined}
        className={cn(viewport === "bounded" && INFORMATION_LIST_SCROLL_CLASS, className)}
        scrollWithinParent={viewport === "page"}
      />
      {paginationPosition === "bottom" ? pagination : null}
    </div>
  );
}
