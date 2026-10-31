import type { ReactNode } from "react";
import {
  DataTable,
  DEFAULT_PAGE_SIZE,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  OffsetPagination,
  PAGE_SIZE_OPTIONS,
  PagedDataTable as SharedPagedDataTable,
  TimedLoadingState,
  cn,
  type DataTableProps,
  type PagedDataTableProps as SharedPagedDataTableProps,
  type ListCountLabels,
  type ListToolbarLabels,
  type PaginationLabels,
  ApiErrorState,
  formatPaginationNumber,
  offsetForPage,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { useWorkspaceState, type WorkspaceField, type WorkspaceValidator } from "@/lib/workspace-state";

/**
 * 共通の Pagination / OffsetPagination / PagedDataTable に渡す Agent の文言（件数の範囲・ページ番号の入力・
 * 最初へ / 前へ / 次へ / 最後へ・表示件数。i18n。#1891）。数値は共通の `formatPaginationNumber`（3 製品で同じ書式）。
 */
export function agentPaginationLabels(): PaginationLabels {
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

/** 共通の `ListToolbar` の `labels`（条件のクリア・狭い置き場所の絞り込みのシート。#1933）。 */
export function listToolbarLabels(): ListToolbarLabels {
  return {
    clearFilters: t("listToolbar.clearFilters"),
    filtersButton: (count) => (count > 0 ? t("listToolbar.filtersActive", { count }) : t("listToolbar.filters")),
    filtersSheetTitle: t("listToolbar.filters"),
    filtersSheetClose: t("listToolbar.filtersSheetClose"),
    showResults: (count) => (count === undefined ? t("listToolbar.showResultsUnknown") : t("listToolbar.showResults", { count })),
  };
}

const isPageNumber: WorkspaceValidator<number> = (value): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 1;

export type ListPageKey = WorkspaceField<"lists">;
export type ListPageSizeKey = WorkspaceField<"listPageSize">;

const isPageSize: WorkspaceValidator<number> = (value): value is number =>
  typeof value === "number" && (PAGE_SIZE_OPTIONS as readonly number[]).includes(value);

/**
 * 作業状態に残す一覧の表示件数（`listPageSize` の field。10 / 25 / 50 / 100、既定 25。#1891・#1952）。
 * 選択肢に無い値（壊れた・古い値）は既定に戻す。
 */
export function usePersistedPageSize(key: ListPageSizeKey): [number, (size: number) => void] {
  const [size, setSize] = useWorkspaceState("listPageSize", key, DEFAULT_PAGE_SIZE, isPageSize);
  return [size, setSize];
}

export type PagedDataTableProps<T> = Omit<
  SharedPagedDataTableProps<T>,
  "paginationLabels" | "page" | "onPageChange" | "pageSizeOptions" | "onPageSizeChange"
> & {
  /**
   * ページ番号と表示件数を作業状態（このタブの sessionStorage）に残す一覧の名前（UX 契約 workspace-state.md）。
   * 一覧 → エディタ → 一覧と移動しても、再読込しても同じページ・同じ件数に戻る。
   * 省略すると、ページと件数は表示している間だけ保つ（開いている対象ごとに変わる一覧など）。
   */
  pageKey?: ListPageKey;
  /** 表示件数の選択（10 / 25 / 50 / 100。#1891）を出すか（既定 true）。行が少ないと決まっている一覧だけ false にする。 */
  pageSizeSelectable?: boolean;
};

/**
 * クライアント側で全件を持つ一覧の標準形（#265、NL2SQL が基準）。
 * 共有の PagedDataTable（表頭の固定・md 未満 5 行 / md 以上 8 行の縦スクロール・既定 25 件/ページ・表示件数の選択）に
 * Agent の文言を渡す。定期的な再取得（Run・承認の 5 秒ごと）で行が変わってもページを戻さない
 * （戻すのは `resetKey` か表示件数が変わったときだけ）。
 */
export function PagedDataTable<T>({
  pageKey,
  pageSizeSelectable = true,
  scrollAriaLabel,
  ...props
}: PagedDataTableProps<T>) {
  // 0 件の空の状態（EmptyState など）は表の外に出す。表の中に置くと、横スクロールする幅の広い表の中央に寄り、
  // 375px で文言と「検索語をクリア」が切れる（RAG / NL2SQL も 0 件は表の代わりに空の状態を出す。#808）。
  if (props.rows.length === 0 && props.empty !== undefined && typeof props.empty !== "string") {
    return <>{props.empty}</>;
  }
  const shared = {
    ...props,
    // 表の中の縦スクロール領域に Tab で到達して読み上げられるようにする（WCAG 2.1.1）。
    scrollAriaLabel: scrollAriaLabel ?? listScrollLabel(props.ariaLabel),
    paginationLabels: agentPaginationLabels(),
    pageSizeOptions: pageSizeSelectable ? PAGE_SIZE_OPTIONS : undefined,
  };
  return pageKey ? (
    <PersistedPagedDataTable<T> {...shared} pageKey={pageKey} pageSizeSelectable={pageSizeSelectable} />
  ) : (
    <SharedPagedDataTable<T> {...shared} />
  );
}

function PersistedPagedDataTable<T>({
  pageKey,
  pageSizeSelectable,
  ...props
}: Omit<SharedPagedDataTableProps<T>, "page" | "onPageChange" | "onPageSizeChange"> & {
  pageKey: ListPageKey;
  pageSizeSelectable: boolean;
}) {
  const [page, setPage] = useWorkspaceState("lists", pageKey, 1, isPageNumber);
  const [pageSize, setPageSize] = usePersistedPageSize(pageKey);
  return (
    <SharedPagedDataTable<T>
      {...props}
      page={page}
      onPageChange={setPage}
      pageSize={pageSizeSelectable ? pageSize : props.pageSize}
      onPageSizeChange={pageSizeSelectable ? setPageSize : undefined}
    />
  );
}

/** 一覧の縦スクロール領域の読み上げ名（「〇〇一覧。スクロールできます。」）。 */
export function listScrollLabel(name: string | undefined): string | undefined {
  return name ? t("list.scrollLabel", { name }) : undefined;
}

/**
 * TanStack Query の初回の取得・失敗・成功で表示を分ける（UX 契約 messaging.md §3.6 / §3.7）。
 * 読み込み中は「〜を読み込んでいます」と経過時間（`TimedLoadingState`）の下に、内容の形の Skeleton を置いて寸法を予約する。
 * 同じ取得を別の領域でも待つときは、片方を `skeletonOnly` にして経過時間を 1 か所だけにする。
 */
export function QueryState<T>({
  query,
  loadingLabel,
  skeleton,
  skeletonOnly = false,
  testId = "query-loading",
  children,
}: {
  query: { isLoading: boolean; error: Error | null; data?: T; refetch?: () => unknown };
  /** i18n 済みの「〜を読み込んでいます」。 */
  loadingLabel: string;
  /** 読み込み後の内容の形をした Skeleton（`TableSkeleton` / `ListSkeleton` / `FormSkeleton` など）。 */
  skeleton: ReactNode;
  skeletonOnly?: boolean;
  testId?: string;
  children: ReactNode;
}) {
  if (query.isLoading) {
    if (skeletonOnly) {
      return (
        <div aria-busy="true" aria-label={loadingLabel} data-testid={testId}>
          {skeleton}
        </div>
      );
    }
    return (
      <TimedLoadingState label={loadingLabel} testId={testId}>
        {skeleton}
      </TimedLoadingState>
    );
  }
  if (query.error) {
    // 取得の失敗は領域だけを占め、再試行を出す（messaging.md §3.6）。
    const refetch = query.refetch;
    return (
      <ApiErrorState
        error={query.error}
        fallback={t("common.error.load")}
        retryLabel={t("common.retry")}
        onRetry={refetch ? () => void refetch() : undefined}
      />
    );
  }
  return <>{children}</>;
}

/** 作業状態に残す一覧のページ番号（`lists` の field）。サーバー側でページングする一覧で使う（#794）。 */
export function usePersistedPage(pageKey: ListPageKey): [number, (page: number) => void] {
  const [page, setPage] = useWorkspaceState("lists", pageKey, 1, isPageNumber);
  return [page, setPage];
}

/**
 * サーバー側でページングする一覧のページ番号と表示件数（作業状態に残す。#1891）。
 * `offset` / `limit` は API に渡す値。表示件数を変えたら 1 ページ目へ戻す。
 */
export function usePersistedServerPaging(pageKey: ListPageKey) {
  const [page, setPage] = usePersistedPage(pageKey);
  const [pageSize, setPageSize] = usePersistedPageSize(pageKey);
  return {
    page,
    setPage,
    pageSize,
    setPageSize: (size: number) => {
      setPageSize(size);
      setPage(1);
    },
    limit: pageSize,
    offset: offsetForPage(page, pageSize),
  };
}

export type ServerPagedDataTableProps<T> = Omit<DataTableProps<T>, "rows" | "stickyHeader" | "visibleRows"> & {
  /** サーバーが返した 1 ページ（共通の `Page`。`items` / `total` / `limit` / `offset`）。 */
  page: { items: T[]; total: number; limit: number; offset: number };
  /** ページの移動（1-based）。作業状態に残すページ番号を変える。 */
  onPageChange: (page: number) => void;
  /** 表示件数を変えたとき（渡すと 10 / 25 / 50 / 100 の選択を出す。#1891）。呼び出し側は 1 ページ目へ戻す。 */
  onPageSizeChange?: (size: number) => void;
  paginationTestId?: string;
  paginationAriaLabel?: string;
};

/**
 * サーバー側でページングする一覧の標準形（#1897。クライアント側の `PagedDataTable` と同じ見た目）。
 * 表頭の固定・表の中の縦スクロール（`INFORMATION_TABLE_VISIBLE_ROWS`）と、直下の共通の `OffsetPagination`。
 * 0 件の空の状態は表の外に出し、範囲外のページ（行が減った・残していたページが無い）は最後のページへ寄せる。
 */
export function ServerPagedDataTable<T>({
  page,
  onPageChange,
  onPageSizeChange,
  paginationTestId,
  paginationAriaLabel,
  rowProps,
  scrollAriaLabel,
  empty,
  ...props
}: ServerPagedDataTableProps<T>) {
  const pager = (
    <OffsetPagination
      offset={page.offset}
      limit={page.limit}
      total={page.total}
      count={page.items.length}
      onPageChange={(next) => onPageChange(next)}
      onPageSizeChange={onPageSizeChange}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      labels={agentPaginationLabels()}
      ariaLabel={paginationAriaLabel}
      testId={paginationTestId}
    />
  );
  if (page.items.length === 0) {
    return (
      <>
        {empty}
        {pager}
      </>
    );
  }
  return (
    <div className="grid min-w-0 gap-2">
      <DataTable<T>
        {...props}
        rows={page.items}
        rowProps={(row, index) => {
          const extra = rowProps?.(row, index);
          return { ...extra, className: cn(INFORMATION_TABLE_ROW_CLASS, extra?.className) };
        }}
        scrollAriaLabel={scrollAriaLabel ?? listScrollLabel(props.ariaLabel)}
        stickyHeader
        visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      />
      {pager}
    </div>
  );
}
