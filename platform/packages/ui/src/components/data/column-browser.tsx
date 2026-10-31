import { ChevronLeft, ChevronRight } from "lucide-react";
import { useRef, type KeyboardEvent, type ReactNode } from "react";

import { INFORMATION_LIST_ROW_CLASS, INFORMATION_LIST_SCROLL_CLASS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";

/** カラムブラウザの 1 行（1 つの項目）。 */
export interface ColumnBrowserItem {
  id: string;
  /** 項目の名前（翻訳済み）。 */
  label: string;
  /** 名前の下の補足（下の段の数・件数など。翻訳済み）。 */
  meta?: ReactNode;
  /** 下の段がある（行の右端に `ChevronRight` を出す）。 */
  hasChildren?: boolean;
  /** 行の右端の操作（`RowActionMenu` など）。 */
  actions?: ReactNode;
  testId?: string;
}

/** カラムブラウザの 1 列（1 つの段）。 */
export interface ColumnBrowserColumn {
  id: string;
  /** 列の見出し（段の名前。翻訳済み）。 */
  title: string;
  /** 見出しの横の件数（「3 件」など。翻訳済み）。 */
  count?: ReactNode;
  /** 見出しの右端の操作（その段への追加など。`size="sm"` の `Button`）。 */
  headerAction?: ReactNode;
  items: readonly ColumnBrowserItem[];
  /** この列で選んでいる項目（右の列に下の段を出している項目）。 */
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  /**
   * 上の段の項目を選んでいない列の案内（「大分類を選ぶと中分類が表示されます」）。渡すと項目の代わりに出す。
   * 狭い置き場所では、案内の無い一番右の列を表示中の列にする。
   */
  placeholder?: ReactNode;
  /** 項目が 0 件のときの表示（`EmptyState` など）。 */
  empty?: ReactNode;
  /** 一覧の上に差し込む内容（追加の入力行など）。 */
  before?: ReactNode;
  /** 項目の行の代わりに描く内容（名前の変更の入力など）。`undefined` を返すと通常の行。 */
  renderItem?: (item: ColumnBrowserItem) => ReactNode | undefined;
  testId?: string;
}

export interface ColumnBrowserProps {
  columns: readonly ColumnBrowserColumn[];
  /** カラムブラウザ全体の読み上げ名（翻訳済み）。 */
  ariaLabel: string;
  /**
   * 狭い置き場所で表示する列（`ColumnBrowserColumn.id`）。省略すると案内の無い一番右の列。
   * 検索の結果から開いた項目の列を見せるときに渡す（案内の出ている列は表示しない）。
   */
  activeColumnId?: string | null;
  /** 狭い置き場所で表示中の列の上の段へ戻るボタンの文言（`index` は表示中の列）。 */
  backLabel?: (index: number) => string;
  /** 狭い置き場所で上の段へ戻る（製品は `index - 1` の列の選択を外す）。 */
  onBack?: (index: number) => void;
  className?: string;
  testId?: string;
}

/** 列の数ごとの、広い置き場所（コンテナ 48rem 以上）の grid の列。 */
const GRID_COLUMNS_CLASS: Record<number, string> = {
  1: "@3xl:grid-cols-1",
  2: "@3xl:grid-cols-2",
  3: "@3xl:grid-cols-3",
  4: "@3xl:grid-cols-4",
};

const ROW_BUTTON_SELECTOR = "[data-column-browser-row]";

/**
 * 階層の一覧を列で見るカラムブラウザ（Miller columns。#2141）。段（大分類 → 中分類 → 小分類など）を
 * 左から列に並べ、選んだ項目の下の段を右の列に出す。各列は見出し（段の名前・件数・追加）と、中で縦に
 * スクロールする行の一覧（一覧の表示密度の 5 / 8 行）を持ち、ページ全体を伸ばさない。
 *
 * - 置き場所（コンテナ）が 48rem 以上は全列を横に並べる。狭いと表示中の列（`activeColumnId`。省略時は案内の
 *   無い一番右の列）だけを出し、見出しの上の戻るボタンで上の段へ戻る（`onBack`）。
 * - 行は名前と補足のボタン（選択中は `aria-current="true"`・淡アクセント面 + 左バー）と、右端の操作。
 * - キーボード: ↑↓ / Home / End で列の中、→ で右の列の先頭、← で左の列の選択中の行へ移る。
 * - 選択の状態は製品が持つ（URL・作業状態に残す）。部品は状態を持たない。
 */
export function ColumnBrowser({
  columns,
  ariaLabel,
  activeColumnId,
  backLabel,
  onBack,
  className,
  testId,
}: ColumnBrowserProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const firstPlaceholder = columns.findIndex((column) => column.placeholder !== undefined);
  const deepestIndex = Math.max(0, (firstPlaceholder === -1 ? columns.length : firstPlaceholder) - 1);
  const requestedIndex = columns.findIndex((column) => column.id === activeColumnId);
  const activeIndex = requestedIndex >= 0 && requestedIndex <= deepestIndex ? requestedIndex : deepestIndex;

  const rowsOf = (columnIndex: number) =>
    Array.from(
      rootRef.current?.querySelectorAll<HTMLButtonElement>(
        `[data-column-browser-column="${columnIndex}"] ${ROW_BUTTON_SELECTOR}`
      ) ?? []
    );

  const focusColumn = (columnIndex: number, prefer: "selected" | "first") => {
    const rows = rowsOf(columnIndex);
    const target =
      (prefer === "selected" ? rows.find((row) => row.getAttribute("aria-current") === "true") : undefined) ??
      rows[0];
    target?.focus();
    return Boolean(target);
  };

  const onKeyDown = (columnIndex: number, event: KeyboardEvent<HTMLUListElement>) => {
    const current = (event.target as HTMLElement).closest<HTMLButtonElement>(ROW_BUTTON_SELECTOR);
    if (!current) return;
    const rows = rowsOf(columnIndex);
    const index = rows.indexOf(current);
    const move = (next: number) => {
      event.preventDefault();
      rows[Math.max(0, Math.min(rows.length - 1, next))]?.focus();
    };
    switch (event.key) {
      case "ArrowDown":
        return move(index + 1);
      case "ArrowUp":
        return move(index - 1);
      case "Home":
        return move(0);
      case "End":
        return move(rows.length - 1);
      case "ArrowRight":
        if (columnIndex < columns.length - 1 && focusColumn(columnIndex + 1, "first")) event.preventDefault();
        return;
      case "ArrowLeft":
        if (columnIndex > 0 && focusColumn(columnIndex - 1, "selected")) event.preventDefault();
        return;
      default:
        return;
    }
  };

  return (
    <div ref={rootRef} className={cn("@container", className)} data-testid={testId}>
      <div
        role="group"
        aria-label={ariaLabel}
        className={cn("grid gap-3", GRID_COLUMNS_CLASS[columns.length] ?? GRID_COLUMNS_CLASS[3])}
      >
        {columns.map((column, columnIndex) => {
          const active = columnIndex === activeIndex;
          const headingId = `${column.id}-column-heading`;
          return (
            <section
              key={column.id}
              aria-labelledby={headingId}
              data-column-browser-column={columnIndex}
              data-active={active ? "true" : undefined}
              data-testid={column.testId}
              className={cn(
                "min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-surface",
                active ? "flex" : "hidden @3xl:flex"
              )}
            >
              {columnIndex > 0 && onBack ? (
                <div className="border-b border-border px-2 py-1 @3xl:hidden">
                  <Button variant="ghost" size="sm" icon={ChevronLeft} onClick={() => onBack(columnIndex)}>
                    {backLabel?.(columnIndex) ?? columns[columnIndex - 1].title}
                  </Button>
                </div>
              ) : null}
              <div className="flex min-h-[var(--button-height-md)] items-center gap-2 border-b border-border px-3 py-2">
                <h3 id={headingId} className="min-w-0 flex-1 truncate text-sm font-semibold text-fg" title={column.title}>
                  {column.title}
                  {column.count !== undefined ? (
                    <span className="ml-2 text-xs font-normal tabular-nums text-fg-muted">{column.count}</span>
                  ) : null}
                </h3>
                {column.placeholder === undefined ? column.headerAction : null}
              </div>
              {column.placeholder !== undefined ? (
                <div className="px-3 py-6 text-sm text-fg-muted">{column.placeholder}</div>
              ) : (
                <>
                  {column.before}
                  {column.items.length === 0 ? (
                    column.empty ? <div className="px-3 py-4">{column.empty}</div> : null
                  ) : (
                    <ul
                      aria-labelledby={headingId}
                      onKeyDown={(event) => onKeyDown(columnIndex, event)}
                      className={cn("space-y-0.5 p-1", INFORMATION_LIST_SCROLL_CLASS)}
                    >
                      {column.items.map((item) => {
                        const custom = column.renderItem?.(item);
                        if (custom !== undefined) {
                          return (
                            <li key={item.id} data-testid={item.testId}>
                              {custom}
                            </li>
                          );
                        }
                        const selected = column.selectedId === item.id;
                        return (
                          <li key={item.id} data-testid={item.testId} className="group">
                            <div
                              data-surface-tint={selected ? "accent" : undefined}
                              className={cn(
                                "grid grid-cols-[minmax(0,1fr)_auto] rounded-md transition-colors",
                                INFORMATION_LIST_ROW_CLASS,
                                selected
                                  ? "bg-accent-subtle shadow-[inset_0.25rem_0_0_var(--color-accent-fg)]"
                                  : "hover:bg-surface-hover"
                              )}
                            >
                              <button
                                type="button"
                                data-column-browser-row=""
                                aria-current={selected ? "true" : undefined}
                                onClick={() => column.onSelect?.(item.id)}
                                className="flex min-w-0 cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-left focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring"
                              >
                                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                                  <span className="break-words text-sm font-medium text-fg [overflow-wrap:anywhere]">
                                    {item.label}
                                  </span>
                                  {item.meta ? (
                                    <span className="text-xs tabular-nums text-fg-muted">{item.meta}</span>
                                  ) : null}
                                </span>
                                {item.hasChildren ? (
                                  <ChevronRight size={16} aria-hidden="true" className="shrink-0 text-fg-muted" />
                                ) : null}
                              </button>
                              {item.actions ? <div className="mr-1 flex items-center">{item.actions}</div> : null}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
