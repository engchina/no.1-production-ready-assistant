import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { Fragment, useCallback, useLayoutEffect, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react";

import { stabilizeMeasuredSize } from "../../lib/measured-size";
import { cn } from "../../lib/utils";
import { SKELETON_CLASS } from "../ui/skeleton";

export type SortDirection = "asc" | "desc";

export interface DataTableSort {
  key: string;
  direction: SortDirection;
}

export interface DataTableColumn<T> {
  /** 列キー（sort 識別・fallback 値取得に使う）。 */
  key: string;
  /** 見出し（翻訳済み）。 */
  header: ReactNode;
  /** セル描画。省略時は row[key] を文字列化。 */
  render?: (row: T, index: number) => ReactNode;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  /** td/th 共通の追加 class。 */
  className?: string;
  headerClassName?: string;
  /** 行を識別する列。`<th scope="row">` として描画する（スクリーンリーダーが行見出しとして読む）。 */
  rowHeader?: boolean;
}

/** 表示行数。`{ base, md }` は md（48rem）以上で `md` を使う。 */
export type DataTableVisibleRows = number | { base: number; md?: number };

/** 行（`<tr>`）へ渡せる属性。 */
export interface DataTableRowProps {
  className?: string;
  "aria-label"?: string;
  "data-testid"?: string;
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: readonly T[];
  getRowKey: (row: T, index: number) => string | number;
  /** 制御式ソート。並べ替え自体は caller が行う（presentational）。 */
  sort?: DataTableSort | null;
  onSortChange?: (sort: DataTableSort) => void;
  /**
   * 行クリック（マウス操作の補助）。行内の button / a / input / label 等を押したときは発火しない。
   * キーボードでは行を選べないため、行を開く・選ぶ操作は行内の button でも提供する。
   */
  onRowClick?: (row: T) => void;
  /** master-detail で詳細に表示中の行。`aria-current="true"` と選択背景を付ける。 */
  selectedRowKey?: string | number | null;
  /** 複数選択（チェックボックス行）で選択背景を付ける。状態はチェックボックス側で伝える。 */
  isRowSelected?: (row: T, index: number) => boolean;
  /** 行ごとの class / aria-label / data-testid。 */
  rowProps?: (row: T, index: number) => DataTableRowProps;
  /** 行の直後に全幅の補足行を描画する（null で描画しない）。 */
  renderRowDetail?: (row: T, index: number) => ReactNode;
  /** 読込中は行の代わりにスケルトンを描画。 */
  loading?: boolean;
  /** 読込中のスケルトン行数（既定は visibleRows の行数、無ければ 3）。 */
  loadingRows?: number;
  /** 行が空かつ非読込のとき表示（EmptyState 等）。 */
  empty?: ReactNode;
  /** 密表示（py を詰める）。 */
  dense?: boolean;
  /** 表頭をスクロール領域の上端に固定する。 */
  stickyHeader?: boolean;
  /**
   * 表頭 + 先頭 N 行の実測高さでスクロール領域の max-height を決め、以降は内部スクロールにする。
   * 2 行セルなどで行高が変わっても N 行ちょうどが見える（固定 rem の計算は行高が変わると行数がずれる）。
   */
  visibleRows?: DataTableVisibleRows;
  /** desktop ではページ送りの領域を残し、表示行数の高さから画面の余白へ拡張する。 */
  growToViewport?: boolean;
  /** 縦横のスクロールを親に任せる（全高のシートの本文など）。 */
  scrollWithinParent?: boolean;
  /** visibleRows 未満の行数でも N 行分の高さを確保する（最後の行の高さで補う）。 */
  fillVisibleRows?: boolean;
  /** 外枠（スクロール領域）の追加 class。 */
  className?: string;
  /** `<table>` の追加 class（`table-fixed` / `min-w-[52rem]` 等）。 */
  tableClassName?: string;
  /** テーブルの aria-label（任意）。 */
  ariaLabel?: string;
  /** <table> に付与する data-testid（既存 e2e の getByTestId 互換用）。 */
  testId?: string;
  /** 指定するとスクロール領域を `role="region"` + Tab 到達可能にし、この名前で読み上げる（WCAG 2.1.1）。 */
  scrollAriaLabel?: string;
  /** スクロール領域（外枠）の data-testid。 */
  scrollTestId?: string;
}

const ALIGN_CLASS: Record<NonNullable<DataTableColumn<unknown>["align"]>, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

/**
 * 選択行: 淡アクセント面 + 先頭セルの左バー（Sidebar の現在地と同じ 0.25rem）。
 * 左バーは選択を背景色の差（1.1:1 程度）だけに頼らず位置と形でも示す（WCAG 1.4.1）。
 * 内側の文字は data-surface-tint="accent"（tokens/colors.css）が淡青面用に 1 段深くする（WCAG 1.4.3）。
 * renderRowDetail の詳細行も、選択行の続きとして同じ面と左バーを持つ。
 */
const SELECTED_ROW_CLASS = "bg-accent-subtle [&>:first-child]:shadow-[inset_0.25rem_0_0_var(--color-accent-fg)]";

/** 行クリックを発火させない行内の操作要素。 */
const INTERACTIVE_SELECTOR =
  'a,button,input,select,textarea,label,summary,[role="button"],[role="checkbox"],[role="link"],[role="menuitem"],[role="switch"],[contenteditable="true"],[data-row-action]';

/** md ブレークポイント（Tailwind v4 の md = 48rem。メディアクエリの rem はルート文字サイズに依存しない）。 */
const MD_QUERY = "(min-width: 48rem)";

function ariaSortValue(sort: DataTableSort | null | undefined, key: string) {
  if (!sort || sort.key !== key) return "none" as const;
  return sort.direction === "asc" ? ("ascending" as const) : ("descending" as const);
}

/**
 * 行クリックとして扱わないクリックか。行内の操作要素（行そのものは除く）と、
 * portal で行の外に描画された子（行メニューの項目等。React のイベントは portal からも行へ伝わる）を除く。
 */
export function isInteractiveRowTarget(target: EventTarget | null, row: Element) {
  if (!(target instanceof Element)) return false;
  if (!row.contains(target)) return true;
  const interactive = target.closest(INTERACTIVE_SELECTOR);
  return Boolean(interactive && interactive !== row && row.contains(interactive));
}

/** 表示行数の設定を現在の幅で解決する。 */
export function resolveVisibleRows(visibleRows: DataTableVisibleRows | undefined, isMd: boolean) {
  if (visibleRows == null) return undefined;
  if (typeof visibleRows === "number") return visibleRows;
  return isMd && visibleRows.md != null ? visibleRows.md : visibleRows.base;
}

/**
 * スクロール領域の高さ（border-box）を実測値から求める。
 * tableTop / rowBottoms は同じ座標系（getBoundingClientRect）の値。chrome は border + 横スクロールバー。
 */
export function measureVisibleRowsHeight({
  tableTop,
  headerBottom,
  rowBottoms,
  rows,
  chrome,
  fill,
}: {
  tableTop: number;
  headerBottom: number;
  rowBottoms: readonly number[];
  rows: number;
  chrome: number;
  fill: boolean;
}) {
  if (rows <= 0) return undefined;
  if (rowBottoms.length === 0) return undefined;
  const shown = Math.min(rows, rowBottoms.length);
  let bottom = rowBottoms[shown - 1];
  if (fill && shown < rows) {
    const lastTop = shown >= 2 ? rowBottoms[shown - 2] : headerBottom;
    const lastHeight = Math.max(bottom - lastTop, 0);
    bottom += lastHeight * (rows - shown);
  }
  return Math.ceil(bottom - tableTop + chrome);
}

/**
 * 測り直した高さを、今の値との差が 1px 以内なら大きい方に寄せて変えない（#1222）。
 * 共通の `stabilizeMeasuredSize`（`larger`。中身を収める高さなので欠けさせない）を使う。
 */
export function stabilizeMeasuredHeight(current: number | undefined, next: number | undefined) {
  return stabilizeMeasuredSize(current, next, { prefer: "larger" });
}

/**
 * `ResizeObserver` の通知で測り直すか（#1232）。
 *
 * 測った高さは同じスクロール領域の `max-height` / `height` に書くので、スクロール領域の「高さ」の変化には
 * 自分の書き込みが含まれる。それにも測り直すと、拡大 125% などの丸めで測り直しの値が前と 1〜2px 違う
 * たびに書き直し → 測り直し … と循環し、下までスクロールした画面が揺れ続ける（#1222 のヒステリシスは
 * 1px の往復しか止めない）。スクロール領域は幅が変わったとき（横スクロールバーの出入り・分割ペインの
 * ドラッグ・サイドバーの開閉）だけ測り直し、行の追加・折り返しなど内容の変化は表の通知で測り直す。
 * `widths.scroller` は前回の通知のスクロール領域の幅（content-box）で、この関数が更新する。
 */
export function shouldRemeasureOnResize(
  entries: ReadonlyArray<{ target: unknown; contentRect: { width: number } }>,
  scroller: unknown,
  widths: { scroller?: number }
) {
  let remeasure = false;
  for (const entry of entries) {
    if (entry.target === scroller) {
      const width = entry.contentRect.width;
      if (widths.scroller !== undefined && widths.scroller === width) continue;
      widths.scroller = width;
    }
    remeasure = true;
  }
  return remeasure;
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(false);
  useLayoutEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    setMatches(media.matches);
    const onChange = () => setMatches(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/**
 * 共通データテーブル。列定義 + 制御式ソート（aria-sort）+ 空/読込 + 横スクロール内蔵。
 * 一覧に必要な固定表頭・表示行数の固定・行の選択状態も持つ。生 <table> の手書きを置換する。
 */
export function DataTable<T>({
  columns,
  rows,
  getRowKey,
  sort,
  onSortChange,
  onRowClick,
  selectedRowKey,
  isRowSelected,
  rowProps,
  renderRowDetail,
  loading,
  loadingRows,
  empty,
  dense,
  stickyHeader,
  visibleRows,
  growToViewport = false,
  scrollWithinParent = false,
  fillVisibleRows,
  className,
  tableClassName,
  ariaLabel,
  testId,
  scrollAriaLabel,
  scrollTestId,
}: DataTableProps<T>) {
  const cellPad = dense ? "px-3 py-1.5" : "px-3 py-2";
  const scrollRef = useRef<HTMLDivElement>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const isMd = useMediaQuery(MD_QUERY);
  const rowLimit = resolveVisibleRows(visibleRows, isMd);
  const skeletonRows = loadingRows ?? rowLimit ?? 3;
  const [measuredHeight, setMeasuredHeight] = useState<number>();
  const selectable = selectedRowKey !== undefined || Boolean(isRowSelected);

  const measure = useCallback(() => {
    const scroller = scrollRef.current;
    const table = tableRef.current;
    if (!scroller || !table || rowLimit == null) {
      setMeasuredHeight(undefined);
      return;
    }
    const tableTop = table.getBoundingClientRect().top;
    const header = table.tHead?.getBoundingClientRect();
    const body = table.tBodies[0];
    const rowBottoms: number[] = [];
    // 補足行（renderRowDetail）は直前のデータ行に含めて数える。
    for (const row of Array.from(body?.rows ?? [])) {
      if (row.dataset.rowKind === "detail") {
        if (rowBottoms.length > 0) rowBottoms[rowBottoms.length - 1] = row.getBoundingClientRect().bottom;
        continue;
      }
      if (row.dataset.rowKind !== "data" && row.dataset.rowKind !== "skeleton") continue;
      rowBottoms.push(row.getBoundingClientRect().bottom);
    }
    const chrome = scroller.offsetHeight - scroller.clientHeight;
    let next = measureVisibleRowsHeight({
      tableTop,
      headerBottom: header?.bottom ?? tableTop,
      rowBottoms,
      rows: rowLimit,
      chrome,
      fill: Boolean(fillVisibleRows),
    });
    if (growToViewport && isMd && next != null) {
      // 復元済みのページスクロールを高さの余白と取り違えない。
      let pageScroll = window.scrollY;
      for (let parent = scroller.parentElement; parent; parent = parent.parentElement) {
        if (/auto|scroll/.test(getComputedStyle(parent).overflowY)) {
          pageScroll += parent.scrollTop;
        }
      }
      const rootRem = Number.parseFloat(getComputedStyle(document.documentElement).fontSize);
      // ページ送りと PageBody の下余白に 6rem を確保する。
      const available = window.innerHeight - scroller.getBoundingClientRect().top - pageScroll - 6 * rootRem;
      next = Math.max(next, available);
    }
    setMeasuredHeight((current) => {
      const stable = stabilizeMeasuredHeight(current, next);
      return stable === current ? current : stable;
    });
  }, [rowLimit, fillVisibleRows, growToViewport, isMd]);

  useLayoutEffect(() => {
    measure();
    if (rowLimit == null || typeof ResizeObserver === "undefined") return;
    const scroller = scrollRef.current;
    // 自分で書いた高さの変化（スクロール領域の高さだけの変化）では測り直さない（#1232）。
    const widths: { scroller?: number } = {};
    const observer = new ResizeObserver((entries) => {
      if (shouldRemeasureOnResize(entries, scroller, widths)) measure();
    });
    if (tableRef.current) observer.observe(tableRef.current);
    if (scroller) observer.observe(scroller);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure, rowLimit, rows, loading]);

  function toggleSort(key: string) {
    if (!onSortChange) return;
    const nextDirection: SortDirection =
      sort?.key === key && sort.direction === "asc" ? "desc" : "asc";
    onSortChange({ key, direction: nextDirection });
  }

  function handleRowClick(event: MouseEvent<HTMLTableRowElement>, row: T) {
    if (!onRowClick || isInteractiveRowTarget(event.target, event.currentTarget)) return;
    onRowClick(row);
  }

  const showEmpty = !loading && rows.length === 0;
  const scrollable = rowLimit != null || Boolean(scrollAriaLabel);
  const scrollStyle: CSSProperties | undefined =
    measuredHeight == null
      ? undefined
      : fillVisibleRows
        ? { height: measuredHeight, maxHeight: measuredHeight }
        : { maxHeight: measuredHeight };
  // sticky の表頭はスクロールすると tbody の上罫線が離れるため、罫線を th の内側に持たせる。
  const headerCellSticky = stickyHeader ? "shadow-[inset_0_-1px_0_var(--color-border)]" : undefined;

  return (
    <div
      ref={scrollRef}
      role={scrollAriaLabel ? "region" : undefined}
      aria-label={scrollAriaLabel}
      tabIndex={scrollAriaLabel ? 0 : undefined}
      data-testid={scrollTestId}
      style={scrollStyle}
      className={cn(
        // relative: 行の中の読み上げ専用のラベル（`sr-only` は position: absolute）の包含ブロックを表のスクロール領域にする。
        // 無いと包含ブロックが表の外になり、横スクロールで隠れた列のラベルが本文（main）を横にはみ出させる。
        "relative rounded-md border border-border bg-surface",
        scrollWithinParent ? "overflow-visible" : scrollable ? "overflow-auto [scrollbar-gutter:stable]" : "overflow-x-auto",
        scrollAriaLabel && "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring",
        className
      )}
    >
      <table
        ref={tableRef}
        className={cn("min-w-full text-left text-xs", !stickyHeader && "divide-y divide-border", tableClassName)}
        aria-label={ariaLabel}
        data-testid={testId}
      >
        <thead className={cn("bg-surface-sunken text-fg-muted", stickyHeader && "sticky top-0 z-10")}>
          <tr>
            {columns.map((column) => {
              const alignClass = column.align ? ALIGN_CLASS[column.align] : "text-left";
              if (column.sortable && onSortChange) {
                const active = sort?.key === column.key;
                const Icon = !active ? ArrowUpDown : sort?.direction === "asc" ? ArrowUp : ArrowDown;
                return (
                  <th
                    key={column.key}
                    scope="col"
                    aria-sort={ariaSortValue(sort, column.key)}
                    className={cn("p-0", alignClass, "font-semibold", headerCellSticky, column.headerClassName)}
                  >
                    {/* 当たり判定は <th> 全体（文字高だけのボタンは 24px の最小タップ領域を割る）。タッチ端末では 44px。 */}
                    <button
                      type="button"
                      onClick={() => toggleSort(column.key)}
                      className={cn(
                        cellPad,
                        "flex min-h-[var(--button-height-sm)] w-full cursor-pointer items-center gap-1 whitespace-nowrap font-semibold transition-colors hover:bg-surface-hover hover:text-fg focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring forced-colors:hover:bg-[Highlight] forced-colors:hover:text-[HighlightText]",
                        column.align === "right" ? "justify-end" : column.align === "center" ? "justify-center" : "justify-start",
                        active ? "text-fg" : "text-fg-muted"
                      )}
                    >
                      <span>{column.header}</span>
                      <Icon size={14} className="shrink-0" aria-hidden="true" />
                    </button>
                  </th>
                );
              }
              return (
                <th
                  key={column.key}
                  scope="col"
                  className={cn(cellPad, alignClass, "font-semibold", "whitespace-nowrap", headerCellSticky, column.headerClassName)}
                >
                  {column.header}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="divide-y divide-border/70 text-fg">
          {loading
            ? Array.from({ length: skeletonRows }).map((_, rowIndex) => (
                <tr key={`skeleton-${rowIndex}`} aria-hidden="true" data-row-kind="skeleton">
                  {columns.map((column) => (
                    <td key={column.key} className={cellPad}>
                      <span className={cn(SKELETON_CLASS, "block h-4 w-full rounded")} />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row, index) => {
                const rowKey = getRowKey(row, index);
                const current = selectedRowKey != null && rowKey === selectedRowKey;
                const selected = current || Boolean(isRowSelected?.(row, index));
                const extra = rowProps?.(row, index);
                const detail = renderRowDetail?.(row, index);
                return (
                  <Fragment key={rowKey}>
                    <tr
                      data-row-kind="data"
                      data-selected={selectable ? (selected ? "true" : "false") : undefined}
                      data-surface-tint={selected ? "accent" : undefined}
                      aria-current={current ? "true" : undefined}
                      aria-label={extra?.["aria-label"]}
                      data-testid={extra?.["data-testid"]}
                      onClick={onRowClick ? (event) => handleRowClick(event, row) : undefined}
                      className={cn(
                        (onRowClick || selectable) && "transition-colors",
                        onRowClick && "cursor-pointer",
                        selected ? SELECTED_ROW_CLASS : onRowClick && "hover:bg-surface-hover",
                        extra?.className
                      )}
                    >
                      {columns.map((column) => {
                        const alignClass = column.align ? ALIGN_CLASS[column.align] : "text-left";
                        const content = column.render
                          ? column.render(row, index)
                          : String((row as Record<string, unknown>)[column.key] ?? "");
                        const cellClass = cn(cellPad, alignClass, column.rowHeader && "font-normal", column.className);
                        return column.rowHeader ? (
                          <th key={column.key} scope="row" className={cellClass}>
                            {content}
                          </th>
                        ) : (
                          <td key={column.key} className={cellClass}>
                            {content}
                          </td>
                        );
                      })}
                    </tr>
                    {detail != null && detail !== false ? (
                      <tr
                        data-row-kind="detail"
                        data-surface-tint={selected ? "accent" : undefined}
                        className={selected ? SELECTED_ROW_CLASS : undefined}
                      >
                        <td colSpan={Math.max(columns.length, 1)} className={cellPad}>
                          {detail}
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
          {showEmpty ? (
            <tr>
              <td colSpan={Math.max(columns.length, 1)} className="px-3 py-6 text-center text-fg-muted">
                {empty}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
