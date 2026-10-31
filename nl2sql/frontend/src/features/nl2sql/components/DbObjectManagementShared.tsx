import { Children, useId, type ReactNode } from "react";
import {
  Check,
  Code2,
  Download,
  RefreshCw,
  Table2,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";

import {
  compareNames,
  Button,
  Banner,
  DataTable,
  EmptyState,
  type DataTableColumn,
  type DataTableVisibleRows,
  toast,
  StatusBadge,
  Tabs,
  ContentActionBar,
  ProcessingIndicator,
  TimedLoadingState,
  Skeleton,
  type ProcessingActivityIcon,
  type ProcessingPlacement,
  ObjectActionBar,
  RowActionMenu,
  type EntityAction,
  type EntityActionTone,
  FixedSplitPane,
  ListDetailLayout,
  ListDetailPager,
  adjacentListItems,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  ExecutionConfirmationField,
  RowTitleButton,
  LoadMoreFooter,
  ListToolbar,
  type ListCountProps,
  CopyButton,
} from "@production-ready/ui";

import { WarningsBanner } from "@/components/WarningsBanner";
import { DialogOverlayPortal } from "@/components/ui/dialog-overlay";
import {
  DbManagementSearchField,
  DbObjectSearchOwnerFields,
  type DbObjectFilterFieldProps,
  type DbObjectToolbarFilterProps,
} from "@/components/DbObjectFilterFields";
import { listCountLabels, listToolbarLabels } from "@/lib/pagination-labels";
import { FIXED_SPLIT_STORAGE_PREFIX, LIST_DETAIL_STORAGE_PREFIX } from "@/lib/ui-store";
import { ErrorState } from "@/components/StateViews";
import { IdentifierText } from "@/components/IdentifierText";
import { formatDateTime, formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  formatDbObjectName,
  parseDbAdminObjectTarget,
  type DbAdminObjectTarget,
} from "../dbObjectIdentity";
import type { DbAdminExecuteData, DbAdminObjectDetail, DbAdminObjectSummary } from "../types";
import { DbObjectColumnsTable, downloadText } from "./DbAdminShared";
import { DbObjectName } from "./DbObjectName";

export {
  DbManagementSearchField,
  DbManagementSelectField,
  DbObjectSearchOwnerFields,
  DbOwnerPrefixFilterField,
} from "@/components/DbObjectFilterFields";

/**
 * オブジェクト一覧で名前の直下に置くコメント（#509 / #536）。
 * Profile 一覧の補足表示と同じ見た目で最大 2 行に収め、空・空白は `-`、全文は title に入れる。
 * 選択操作の要素から `aria-describedby` でこの id を参照し、読み上げでも名前と関連付ける。
 */
export function DbObjectCommentText({ id, comment }: { id: string; comment?: string | null }) {
  const text = comment?.trim() || "-";
  return (
    <span id={id} className="line-clamp-2 break-words text-xs font-normal leading-5 text-fg-muted [overflow-wrap:anywhere]" title={text}>
      {text}
    </span>
  );
}

export type DbObjectDetailTab = "columns" | "ddl";
export type DbObjectOwnerPrefix = string;
export type DbObjectSortKey = "name" | "row_count" | "owner";
export type DbObjectSortDirection = "asc" | "desc";
export type DbObjectPickerSortKey = "name" | "kind" | "row_count" | "owner";
export type DbObjectPickerSortDirection = "asc" | "desc";
export { formatDbObjectName, parseDbAdminObjectTarget };
export type { DbAdminObjectTarget };

export interface DbObjectSortState {
  key: DbObjectSortKey;
  direction: DbObjectSortDirection;
}

export interface DbObjectPickerSortState {
  key: DbObjectPickerSortKey;
  direction: DbObjectPickerSortDirection;
}

export interface DbObjectGridLabels {
  title: string;
  hint: string;
  loading: string;
  emptyTitle: string;
  emptyHint: string;
  noResultsTitle: string;
  noResultsHint: string;
  objectName: string;
  rows: string;
  owner: string;
  showObject: (name: string) => string;
}

export interface DbObjectDetailLabels {
  actions: string;
  loading: string;
  ddlLoading: string;
  tabsLabel: string;
  columns: string;
  ddl: string;
  export?: string;
  exportAria?: string;
  exactCount?: string;
  exactCountAria?: string;
  drop: string;
}

export interface DbObjectStatusBarLabels {
  ariaLabel: string;
  count: string;
  runtime: string;
  refreshedAt: string;
  refresh: string;
  schemaRefresh: string;
}

export interface DbObjectDropDialogLabels {
  title: string;
  subtitle: string;
  close: string;
  target: string;
  executeTitle: string;
  executeHint: string;
  cancel: string;
  run: string;
}

export interface DbObjectTab<T extends string> {
  id: T;
  label: string;
  icon: LucideIcon;
}

export interface DbObjectStatusMetric {
  label: string;
  value: string;
  testId?: string;
  emphasis?: boolean;
}

export function dbObjectSortValue(item: DbAdminObjectSummary, key: DbObjectSortKey) {
  if (key === "row_count") return item.row_count ?? -1;
  if (key === "name") return formatDbObjectName(item);
  return item.owner;
}

/** 一覧の並べ替えの比較（文字は共通の名前の順。#2753）。 */
export function compareDbObjectSortValues(a: string | number, b: string | number, direction: "asc" | "desc") {
  if (typeof a === "string" && typeof b === "string") return compareNames(a, b, direction);
  const result = a < b ? -1 : a > b ? 1 : 0;
  return direction === "asc" ? result : -result;
}

export function rowCountLabel(rowCount?: number | null) {
  return rowCount == null ? "-" : t("dbAdmin.list.rows", { count: rowCount });
}

export function dbAdminExecuteFailureMessage(result: DbAdminExecuteData, fallback: string) {
  const warning = result.warnings.find((item) => item.trim());
  if (warning) return warning;
  const statementError = result.statements.find((statement) => statement.error_message.trim());
  if (statementError) return statementError.error_message;
  return fallback;
}

export type DbManagementLoadingSkeletonVariant = "list" | "detail" | "compact";

// 一覧の行の最小高さ。表示行数（5/8 行）は DataTable の visibleRows が表頭と行の実測から決める。
export const DB_OBJECT_GRID_ROW_CLASS = INFORMATION_TABLE_ROW_CLASS;

/** 読込スケルトンの棒の data-testid（e2e が形と数を確かめる）。棒は共有の Skeleton をそのまま使う。 */
const SKELETON_BLOCK_TEST_ID = "db-management-skeleton-block";

/**
 * データ準備系の管理画面で共有する読込スケルトン。
 * detail はテーブル／ビュー詳細の既存寸法を正本として維持する。
 */
export function DbManagementLoadingSkeleton({
  idPrefix,
  ariaLabel,
  variant = "detail",
  rows = 8,
  operationKey,
  onCancel,
  placement = "panel",
  testId,
  activityIcon,
}: {
  idPrefix: string;
  ariaLabel: string;
  variant?: DbManagementLoadingSkeletonVariant;
  rows?: number;
  operationKey?: string | number | null;
  onCancel?: () => void;
  placement?: ProcessingPlacement;
  testId?: string;
  activityIcon?: ProcessingActivityIcon;
}) {
  if (variant === "list") {
    return (
      <TimedLoadingState
        label={ariaLabel}
        operationKey={operationKey}
        onCancel={onCancel}
        placement={placement}
        testId={testId ?? `${idPrefix}-list-skeleton`}
        activityIcon={activityIcon}
      >
        <div className="grid gap-2">
          <Skeleton className="h-11" testId={SKELETON_BLOCK_TEST_ID} />
          {Array.from({ length: rows }, (_, index) => (
            <Skeleton key={index} className="h-14" testId={SKELETON_BLOCK_TEST_ID} />
          ))}
        </div>
      </TimedLoadingState>
    );
  }

  if (variant === "compact") {
    return (
      <TimedLoadingState
        label={ariaLabel}
        operationKey={operationKey}
        onCancel={onCancel}
        placement={placement}
        testId={testId ?? `${idPrefix}-compact-skeleton`}
        activityIcon={activityIcon}
      >
        <Skeleton className="h-10" testId={SKELETON_BLOCK_TEST_ID} />
        <Skeleton className="h-24" testId={SKELETON_BLOCK_TEST_ID} />
      </TimedLoadingState>
    );
  }

  return (
    <TimedLoadingState
      label={ariaLabel}
      operationKey={operationKey}
      onCancel={onCancel}
      placement={placement}
      testId={testId ?? `${idPrefix}-detail-skeleton`}
      activityIcon={activityIcon}
    >
      <Skeleton className="h-18" testId={SKELETON_BLOCK_TEST_ID} />
      <Skeleton className="h-11.5" testId={SKELETON_BLOCK_TEST_ID} />
      <Skeleton className="h-82" testId={SKELETON_BLOCK_TEST_ID} />
    </TimedLoadingState>
  );
}

export function DbObjectManagementPanelShell({
  id,
  labelledBy,
  ariaLabel,
  idPrefix,
  className = "",
  splitId,
  minLeftPaneWidthPx,
  minRightPaneWidthPx,
  role = "tabpanel",
  framed = true,
  processing,
  topContent,
  children,
}: {
  id: string;
  /** タブ連携時のみ指定。list+actions 等タブ非連携では省略し role="region" を使う。 */
  labelledBy?: string;
  ariaLabel?: string;
  idPrefix: string;
  className?: string;
  splitId?: string;
  minLeftPaneWidthPx?: number;
  minRightPaneWidthPx?: number;
  /** タブ配下は "tabpanel"(既定)、タブ非連携の独立領域は "region"。 */
  role?: "tabpanel" | "region";
  /**
   * 枠（カード）を付けるか。一覧 + 詳細（`DbObjectListDetail` / `ListDetailLayout`）は一覧と詳細の列がそれぞれ枠を持つので、
   * 外側の枠を付けない（カードの入れ子を 1 段減らす。#2281）。
   */
  framed?: boolean;
  /** 既存内容を保持する明示的な再取得など、作業領域に属する処理状態。 */
  processing?: ReactNode;
  /** 左右分割の外側に置く、画面全幅の補助コンテンツ。 */
  topContent?: ReactNode;
  children: ReactNode;
}) {
  const panelChildren = Children.toArray(children);
  const splitPaneId = splitId && panelChildren.length === 2 ? splitId : null;

  return (
    <section
      id={id}
      role={role}
      aria-labelledby={labelledBy}
      aria-label={ariaLabel}
      aria-busy={processing ? true : undefined}
      className={cn("grid gap-4", framed && "rounded-md border border-border bg-surface p-4 shadow-sm", className)}
      data-testid="management-panel-shell"
      data-management-id={idPrefix}
    >
      {processing}
      {topContent}
      {splitPaneId ? (
        <FixedSplitPane
          storagePrefix={FIXED_SPLIT_STORAGE_PREFIX}
          splitId={splitPaneId}
          minLeftPaneWidthPx={minLeftPaneWidthPx}
          minRightPaneWidthPx={minRightPaneWidthPx}
          left={panelChildren[0]}
          right={panelChildren[1]}
        />
      ) : (
        children
      )}
    </section>
  );
}

export function DbObjectPanelHeader({
  title,
  description,
  icon: Icon,
  headingId,
  action,
}: {
  title: string;
  description?: string;
  icon: LucideIcon;
  headingId?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h2 id={headingId} className="flex items-center gap-2 text-base font-semibold text-fg">
          <Icon size={20} aria-hidden="true" />
          {title}
        </h2>
        {description && <p className="mt-1 text-sm text-fg-muted">{description}</p>}
      </div>
      {action && <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:justify-end">{action}</div>}
    </div>
  );
}

/**
 * DB オブジェクトの一覧・候補の上のツールバー（#600 / #1891）。枠（`bg-surface-sunken`）の中に共通の `ListToolbar` を置き、
 * 左に検索欄（部品が lg で固定）→ 所有者の接頭辞（md）→ `children` の絞り込み（`FilterSelect`。#1933）→「条件をクリア」、
 * 右に件数（`count` = `ListCount`）・状態の文言（`resultLabel`）・一覧への操作（`actions`）を並べる。
 * 件数はここ 1 か所に出し、パネルの見出しのバッジと二重にしない。
 */
export function DbObjectSelectorToolbar({
  searchLabel,
  searchPlaceholder,
  searchValue,
  onSearchChange,
  resultLabel,
  count,
  actions,
  dataTestId,
  className = "",
  ownerPrefixField,
  children,
  activeFilterCount = 0,
  onClearFilters,
  filtersLayout,
}: DbObjectToolbarFilterProps & {
  searchLabel: string;
  searchPlaceholder: string;
  searchValue: string;
  onSearchChange: (value: string) => void;
  /** 件数のほかに右に出す状態の文言（例:「N / M 件を表示、選択 K 件」）。変わったら読み上げる。 */
  resultLabel?: string;
  /**
   * 一覧の件数（`ListCount`。#1891）。絞り込みなしは `matched` を渡さない（「全 N 件」）、絞り込み中は
   * `matched` = 一致した全件・`total` = 絞り込む前の全件（「N 件（全 M 件）」）。`labels` を省くと NL2SQL の文言。
   */
  count?: ListCountProps;
  /** 右端に置く一覧への操作。 */
  actions?: ReactNode;
  dataTestId?: string;
  className?: string;
  ownerPrefixField?: DbObjectFilterFieldProps;
  /** 検索・所有者の右に並べる絞り込み（`FilterSelect`。#1933）。 */
  children?: ReactNode;
}) {
  const summary = resultLabel ? <span aria-live="polite">{resultLabel}</span> : undefined;
  return (
    <div
      className={`min-w-0 rounded-md border border-border bg-surface-sunken p-3 ${className}`}
      data-testid={dataTestId}
    >
      {ownerPrefixField ? (
        <DbObjectSearchOwnerFields
          searchLabel={searchLabel}
          searchPlaceholder={searchPlaceholder}
          searchValue={searchValue}
          onSearchChange={onSearchChange}
          ownerLabel={ownerPrefixField.label}
          ownerPlaceholder={ownerPrefixField.placeholder}
          ownerValue={ownerPrefixField.value}
          onOwnerChange={ownerPrefixField.onChange}
          disabled={ownerPrefixField.disabled}
          filters={children}
          count={count}
          summary={summary}
          actions={actions}
          activeFilterCount={activeFilterCount}
          onClearFilters={onClearFilters}
          filtersLayout={filtersLayout}
        />
      ) : (
        <ListToolbar
          activeFilterCount={activeFilterCount}
          searchActive={Boolean(searchValue.trim())}
          onClearFilters={onClearFilters}
          filtersLayout={filtersLayout}
          labels={listToolbarLabels()}
          search={
            <DbManagementSearchField
              label={searchLabel}
              placeholder={searchPlaceholder}
              value={searchValue}
              onChange={onSearchChange}
            />
          }
          filters={children}
          count={count ? { labels: listCountLabels(), ...count } : undefined}
          summary={summary}
          actions={actions}
        />
      )}
    </div>
  );
}

/**
 * 追加読み込み型の一覧のフッター。共通の `LoadMoreFooter`（#600）に NL2SQL の文言と枠を渡す薄い包み。
 * 件数（「N / M 件を表示、選択 K 件」）と「さらに読み込む」、読み込みの失敗の再試行は共通の部品が持つ。
 */
export function DbObjectSelectorFooter({
  visibleCount,
  totalCount,
  selectedCount,
  hasNextPage,
  loadingNextPage = false,
  loadMoreError = "",
  loadMoreLabel = t("objectSelector.loadMore"),
  dataTestId,
  className = "min-h-10 rounded-md border border-border bg-surface px-3 py-2",
  onLoadMore,
  onRetryLoadMore,
}: {
  visibleCount: number;
  totalCount: number;
  selectedCount?: number;
  hasNextPage?: boolean;
  loadingNextPage?: boolean;
  loadMoreError?: string;
  loadMoreLabel?: string;
  dataTestId?: string;
  /** 外枠の class（一覧 + 詳細の列の下では枠を付けない）。 */
  className?: string;
  onLoadMore?: () => void;
  onRetryLoadMore?: () => void;
}) {
  return (
    <LoadMoreFooter
      summary={
        selectedCount == null
          ? t("objectSelector.resultCount", { visible: visibleCount, total: totalCount })
          : t("objectSelector.resultCountWithSelected", {
              visible: visibleCount,
              total: totalCount,
              selected: selectedCount,
            })
      }
      hasMore={hasNextPage}
      loadingMore={loadingNextPage}
      loadMoreError={loadMoreError || undefined}
      onLoadMore={onLoadMore}
      onRetry={onRetryLoadMore}
      loadMoreLabel={loadMoreLabel}
      retryLabel={t("common.retry")}
      announce
      className={className}
      testId={dataTestId}
    />
  );
}

export function DbObjectSelectionSummary({
  label,
  value,
  badge,
}: {
  label: string;
  value: string;
  badge?: ReactNode;
}) {
  if (!value) return null;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-accent-emphasis bg-surface px-3 py-2 text-sm text-fg">
      <span className="font-medium text-fg">{label}</span>
      <DbObjectName value={value} size="xs" />
      {badge}
    </div>
  );
}

export interface DbObjectPickerItem {
  key: string;
  name: string;
  kind?: string;
  kindLabel?: string;
  kindVariant?: "neutral" | "info" | "success" | "warning" | "danger";
  rowCount?: number | null;
  rowCountLabel?: string;
  owner?: string;
  comment?: string;
}

export function dbObjectPickerSortValue(item: DbObjectPickerItem, key: DbObjectPickerSortKey) {
  if (key === "row_count") return item.rowCount ?? -1;
  if (key === "kind") return item.kind ?? item.kindLabel ?? "";
  if (key === "owner") return item.owner ?? "";
  return item.name;
}

export function sortDbObjectPickerItems<T extends DbObjectPickerItem>(
  items: T[],
  sort: DbObjectPickerSortState
) {
  return items.slice().sort((left, right) => {
    return compareDbObjectSortValues(
      dbObjectPickerSortValue(left, sort.key),
      dbObjectPickerSortValue(right, sort.key),
      sort.direction
    );
  });
}

export function DbSingleObjectPickerList({
  items,
  selectedKey,
  hasActiveFilter,
  loadingKey = "",
  listLabel,
  emptyTitle,
  emptyHint,
  noResultsTitle,
  noResultsHint,
  dataTestId,
  visibleRows = INFORMATION_TABLE_VISIBLE_ROWS,
  onSelect,
  selectAriaLabel,
  selectDisabled,
  sort,
  onSortChange,
  action,
}: {
  items: DbObjectPickerItem[];
  selectedKey: string;
  hasActiveFilter: boolean;
  loadingKey?: string;
  listLabel: string;
  emptyTitle: string;
  emptyHint: string;
  noResultsTitle: string;
  noResultsHint: string;
  dataTestId?: string;
  /** 表示行数（既定: モバイル 5 行・md 以上 8 行）。 */
  visibleRows?: DataTableVisibleRows;
  onSelect: (item: DbObjectPickerItem) => void;
  selectAriaLabel?: (item: DbObjectPickerItem) => string;
  selectDisabled?: (item: DbObjectPickerItem) => boolean;
  sort?: DbObjectPickerSortState;
  onSortChange?: (key: DbObjectPickerSortKey) => void;
  action?: {
    id: string;
    label: string;
    icon?: LucideIcon;
    tone?: EntityActionTone;
    ariaLabel: (item: DbObjectPickerItem) => string;
    visible?: (item: DbObjectPickerItem) => boolean;
    disabled?: (item: DbObjectPickerItem) => boolean;
    onClick: (item: DbObjectPickerItem) => void;
  };
}) {
  const commentIdPrefix = useId();
  if (items.length === 0) {
    return (
      <div className="rounded-md border border-border bg-surface p-4" data-testid={dataTestId}>
        <EmptyState
          title={hasActiveFilter ? noResultsTitle : emptyTitle}
          hint={hasActiveFilter ? noResultsHint : emptyHint}
        />
      </div>
    );
  }

  const selectedItem = items.find((item) => item.key === selectedKey || item.name === selectedKey);
  const sortable = Boolean(sort && onSortChange);
  // md 未満は対象名の列だけを見せ、種類・行数・所有者は名前の下に補足として並べる（横スクロールを出さない）。
  // md 以上の種類・行数は内容幅（バッジ「テーブル」約 70px、「統計未取得」約 60px）より広い固定幅にする。
  const columns: Array<DataTableColumn<DbObjectPickerItem>> = [
    {
      key: "name",
      header: t("objectSelector.column.name"),
      sortable,
      className: "align-top",
      render: (item) => {
        const index = items.indexOf(item);
        const commentId = `${commentIdPrefix}-comment-${index}`;
        const selected = item === selectedItem;
        const selectionDisabled = Boolean(selectDisabled?.(item));
        return (
          <div className="grid min-w-0 gap-1">
            <RowTitleButton
              title={<DbObjectName value={item.name} size="xs" />}
              subtitle={<DbObjectCommentText id={commentId} comment={item.comment} />}
              current={selected}
              aria-label={selectAriaLabel?.(item) ?? t("objectSelector.selectObject", { name: item.name })}
              aria-describedby={commentId}
              disabled={selectionDisabled}
              onClick={() => onSelect(item)}
            />
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted md:hidden" data-testid="db-object-picker-row-meta">
              {item.kindLabel ? <StatusBadge icon={false} variant={item.kindVariant ?? "neutral"} label={item.kindLabel} /> : null}
              <span className="whitespace-nowrap font-sans text-fg">{item.rowCountLabel || "-"}</span>
              <IdentifierText value={item.owner || "-"} className="font-mono" />
            </span>
          </div>
        );
      },
    },
    {
      key: "kind",
      header: t("objectSelector.column.kind"),
      sortable,
      headerClassName: "hidden w-[6.75rem] md:table-cell",
      className: "hidden whitespace-nowrap align-top md:table-cell",
      render: (item) =>
        item.kindLabel ? (
          <StatusBadge icon={false} variant={item.kindVariant ?? "neutral"} label={item.kindLabel} />
        ) : (
          <span className="text-fg-muted">-</span>
        ),
    },
    {
      key: "row_count",
      header: t("objectSelector.column.rows"),
      sortable,
      headerClassName: "hidden w-[6.75rem] md:table-cell",
      className: "hidden whitespace-nowrap align-top font-sans md:table-cell",
      render: (item) => item.rowCountLabel || "-",
    },
    {
      key: "owner",
      header: t("objectSelector.column.owner"),
      sortable,
      headerClassName: "hidden w-[22%] md:table-cell",
      className: "hidden min-w-0 align-top font-mono text-fg-muted md:table-cell",
      render: (item) => <IdentifierText value={item.owner || "-"} />,
    },
  ];
  if (action) {
    columns.push({
      key: "actions",
      header: t("objectSelector.column.actions"),
      align: "right",
      headerClassName: "w-[4rem]",
      className: "align-top",
      render: (item) => {
        const actionLoading = loadingKey === item.key;
        const rowActions: EntityAction[] = [
          {
            id: action.id,
            label: action.label,
            ariaLabel: action.ariaLabel(item),
            icon: action.icon,
            tone: action.tone,
            visible: action.visible?.(item),
            loading: actionLoading,
            disabled: action.disabled?.(item),
            onSelect: () => action.onClick(item),
          },
        ];
        if (!rowActions.some((rowAction) => rowAction.visible !== false)) return null;
        return (
          <RowActionMenu
            actions={rowActions}
            ariaLabel={`${t("objectSelector.column.actions")}: ${item.name}`}
            loading={actionLoading}
            disabled={rowActions.every((rowAction) => rowAction.disabled)}
            testId={dataTestId ? `${dataTestId}-row-actions-${item.key}` : undefined}
          />
        );
      },
    });
  }

  return (
    <DataTable
      columns={columns}
      rows={items}
      getRowKey={(item) => item.key}
      sort={sort ?? null}
      onSortChange={onSortChange ? (next) => onSortChange(next.key as DbObjectPickerSortKey) : undefined}
      selectedRowKey={selectedItem?.key ?? null}
      onRowClick={(item) => {
        if (selectDisabled?.(item)) return;
        onSelect(item);
      }}
      rowProps={(item) => ({
        className: cn(DB_OBJECT_GRID_ROW_CLASS, selectDisabled?.(item) && "cursor-not-allowed hover:bg-surface"),
      })}
      ariaLabel={listLabel}
      scrollTestId={dataTestId}
      tableClassName="w-full table-fixed"
      stickyHeader
      visibleRows={visibleRows}
    />
  );
}

export function DbObjectStepIndicator({
  steps,
  activeIndex,
  ariaLabel,
  dataTestId,
}: {
  steps: string[];
  activeIndex: number;
  ariaLabel: string;
  dataTestId?: string;
}) {
  // 水平ステッパー(Material/Ant 標準): 丸番号(完了は ✓) + 連結線 + 丸の下に中央ラベル。
  // 状態は色 + アイコン/番号で伝達(color-not-only)。等幅分配 + ラベル折返しで 375px でも横溢れなし。
  return (
    <ol className="flex items-start" aria-label={ariaLabel} data-testid={dataTestId}>
      {steps.map((label, index) => {
        const complete = index < activeIndex;
        const current = index === activeIndex;
        const isFirst = index === 0;
        const isLast = index === steps.length - 1;
        return (
          <li
            key={label}
            className="flex flex-1 flex-col items-center gap-2"
            aria-current={current ? "step" : undefined}
          >
            <div className="flex w-full items-center">
              <span
                aria-hidden="true"
                className={`h-0.5 flex-1 rounded-full ${
                  isFirst ? "opacity-0" : index <= activeIndex ? "bg-accent-emphasis" : "bg-border"
                }`}
              />
              <span
                data-surface-tint={!complete && current ? "accent" : undefined}
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition-colors ${
                  complete
                    ? "border-accent-emphasis bg-accent-emphasis text-fg-on-accent"
                    : current
                      ? "border-accent-emphasis bg-accent-subtle text-accent-fg"
                      : "border-border bg-surface text-fg-muted"
                }`}
              >
                {complete ? <Check size={16} aria-hidden="true" /> : <span className="tnum">{index + 1}</span>}
              </span>
              <span
                aria-hidden="true"
                className={`h-0.5 flex-1 rounded-full ${
                  isLast ? "opacity-0" : index < activeIndex ? "bg-accent-emphasis" : "bg-border"
                }`}
              />
            </div>
            <span
              className={`px-1 text-center text-xs font-medium leading-snug ${
                complete || current ? "text-fg" : "text-fg-muted"
              }`}
            >
              {label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function DbObjectStatusMetricItem({
  label,
  value,
  testId,
  emphasis = false,
  density = "default",
}: DbObjectStatusMetric & { density?: "default" | "compact" }) {
  if (density === "compact") {
    return (
      <div className="flex min-w-0 items-baseline gap-2 py-1">
        <dt className="shrink-0 text-xs font-medium text-fg-muted">{label}</dt>
        <dd
          className={`min-w-0 break-words font-semibold tabular-nums text-fg [overflow-wrap:anywhere] ${
            emphasis ? "text-base" : "text-sm"
          }`}
          data-testid={testId}
        >
          {value}
        </dd>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-border bg-surface-sunken px-3 py-2">
      <dt className="text-xs font-medium text-fg-muted">{label}</dt>
      <dd
        className={`mt-1 font-semibold text-fg ${emphasis ? "text-lg" : ""}`}
        data-testid={testId}
      >
        {value}
      </dd>
    </div>
  );
}

export function DbObjectManagementStatusBar({
  ariaLabel,
  metrics,
  actions,
  metricColumnsClass = "sm:grid-cols-3",
  density = "default",
}: {
  ariaLabel: string;
  metrics: DbObjectStatusMetric[];
  actions?: ReactNode;
  metricColumnsClass?: string;
  density?: "default" | "compact";
}) {
  if (density === "compact") {
    return (
      <section
        className="rounded-md border border-border bg-surface px-3 py-2 shadow-sm"
        aria-label={ariaLabel}
        data-density="compact"
      >
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <dl className="flex min-w-0 flex-1 flex-wrap items-center gap-x-5 gap-y-1">
            {metrics.map((metric) => (
              <DbObjectStatusMetricItem
                key={`${metric.label}-${metric.value}`}
                {...metric}
                density="compact"
              />
            ))}
          </dl>
          {actions && <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">{actions}</div>}
        </div>
      </section>
    );
  }

  return (
    <section
      className="rounded-md border border-border bg-surface px-4 py-3 shadow-sm"
      aria-label={ariaLabel}
      data-density="default"
    >
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <dl className={`grid gap-3 ${metricColumnsClass} xl:flex xl:flex-wrap xl:items-center`}>
          {metrics.map((metric) => (
            <DbObjectStatusMetricItem key={`${metric.label}-${metric.value}`} {...metric} />
          ))}
        </dl>
        {actions && <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:justify-end">{actions}</div>}
      </div>
    </section>
  );
}

export function DbObjectStatusBar({
  count,
  runtime,
  refreshedAt,
  loading,
  labels,
  onRefresh,
  onSchemaRefresh,
}: {
  count: number;
  runtime: string;
  refreshedAt: string;
  loading: string;
  labels: DbObjectStatusBarLabels;
  onRefresh: () => void;
  onSchemaRefresh: () => void;
}) {
  return (
    <DbObjectManagementStatusBar
      ariaLabel={labels.ariaLabel}
      metrics={[
        { label: labels.count, value: formatNumber(count), emphasis: true },
        { label: labels.runtime, value: runtime },
        { label: labels.refreshedAt, value: formatDateTime(refreshedAt) },
      ]}
      actions={
        <>
          <Button type="button" variant="secondary" size="sm" loading={loading === "load"} onClick={onRefresh} icon={RefreshCw}>
            <span>{labels.refresh}</span>
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            loading={loading === "schema-refresh"}
            onClick={onSchemaRefresh} icon={RefreshCw}>
            <span>{labels.schemaRefresh}</span>
          </Button>
        </>
      }
    />
  );
}

/**
 * DB オブジェクトの一覧の件数（`ListToolbar` の `count`。#1891）。絞り込みなしは「全 N 件」（N = API の total）、
 * 絞り込み中は「N 件（全 M 件）」（N = 絞り込みに一致した total、M = 絞り込む前の total）。
 * 読み込み中・失敗・絞り込む前の全件がまだ分からないときは出さない（undefined）。
 */
export function dbObjectListCount({
  matched,
  unfilteredTotal,
  filtered,
  ready,
}: {
  matched: number;
  unfilteredTotal?: number;
  filtered: boolean;
  ready: boolean;
}): ListCountProps | undefined {
  if (!ready) return undefined;
  if (!filtered) return { total: matched };
  if (unfilteredTotal == null) return undefined;
  // 読み込みの順で、絞り込む前の全件が一致した件数より古く小さいことがある。M が N を下回らないようにする。
  return { total: Math.max(unfilteredTotal, matched), matched };
}

/**
 * DB オブジェクトの一覧 + 詳細（B 型。#2281）。共有の `ListDetailLayout` に、DB オブジェクトの一覧
 * （左の狭い列: 検索・所有者・絞り込み・件数 → 名前とコメントの 2 段 + 行数（と種類）の表 → 「さらに読み込む」）と、
 * 選んだ対象の詳細（右の広い列）を渡す。選択は URL（`useWorkspaceUrlSelection`）が持ち、先頭の行を自動で選ばない。
 * テーブル / ビューの管理（詳細は `DbObjectDetailPanel`）と、データの管理のデータの表示（詳細はデータの結果。#2315）が使う。
 */
export function DbObjectListDetail({
  idPrefix,
  headingId,
  icon: Icon,
  items,
  selectedName,
  loading,
  search,
  ownerPrefix,
  sort,
  labels,
  showComments = false,
  totalCount,
  unfilteredTotalCount,
  hasNextPage,
  loadingNextPage = false,
  loadMoreError = "",
  error = "",
  loadingActivityIcon,
  detail,
  detailLabel,
  emptyDetail,
  filters,
  activeFilterCount = 0,
  onClearFilters,
  filtersLayout,
  kindLabel,
  formatRowCount = rowCountLabel,
  onSearchChange,
  onOwnerPrefixChange,
  onSortChange,
  onSelect,
  onLoadMore,
  onRetryLoadMore,
  onRetry,
}: {
  idPrefix: string;
  headingId: string;
  icon: LucideIcon;
  items: DbAdminObjectSummary[];
  /** 選んでいる対象（`OWNER.NAME`）。未選択は空文字。 */
  selectedName: string;
  loading: boolean;
  search: string;
  ownerPrefix: DbObjectOwnerPrefix;
  sort: DbObjectSortState;
  labels: DbObjectGridLabels;
  showComments?: boolean;
  /** 今の絞り込み（検索語・所有者）に一致した全件（API の total）。 */
  totalCount?: number;
  /**
   * 絞り込む前の全件（絞り込みの無い同じ問い合わせの total。TanStack Query の同じ queryKey でキャッシュを共有する）。
   * 絞り込み中の件数「N 件（全 M 件）」の M に使う。まだ分からないときは件数を出さない。
   */
  unfilteredTotalCount?: number;
  hasNextPage?: boolean;
  loadingNextPage?: boolean;
  loadMoreError?: string;
  error?: string;
  /**
   * 読込中の表示のスピナー。起点のボタン（PageHeader の「最新情報を取得」など）が loading を出している間は
   * "none" にする（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
   */
  loadingActivityIcon?: ProcessingActivityIcon;
  /** 選んだ対象の詳細（`DbObjectDetailPanel`）。 */
  detail: ReactNode;
  /** 詳細の列の名前（翻訳済み。例:「テーブルの詳細」）。 */
  detailLabel: string;
  /** 未選択の 2 ペインの詳細の空の状態。省略すると列情報・CREATE SQL の案内。 */
  emptyDetail?: ReactNode;
  /** 所有者の右に並べる絞り込み（`FilterSelect` など）。 */
  filters?: ReactNode;
  /** `filters` のうち既定と違う値の数（検索語・所有者は数えない）。 */
  activeFilterCount?: number;
  /** 「条件をクリア」で `filters` を既定に戻す（検索語・所有者はこの部品が戻す）。 */
  onClearFilters?: () => void;
  /** 狭い置き場所の絞り込みの出し方。検索・所有者のほかに絞り込みがあるときは `sheet`（#1933）。 */
  filtersLayout?: "wrap" | "sheet";
  /** 行の右の短い値に添える種類（例:「テーブル」「ビュー」）。省略すると出さない。 */
  kindLabel?: (item: DbAdminObjectSummary) => string;
  /** 行数の表示（既定は件数か `-`）。 */
  formatRowCount?: (rowCount?: number | null) => string;
  onSearchChange: (value: string) => void;
  onOwnerPrefixChange: (value: DbObjectOwnerPrefix) => void;
  onSortChange: (key: DbObjectSortKey) => void;
  onSelect: (name: string) => void;
  onLoadMore?: () => void;
  onRetryLoadMore?: () => void;
  onRetry?: () => void;
}) {
  const hasActiveFilter = Boolean(search.trim()) || Boolean(ownerPrefix.trim()) || activeFilterCount > 0;
  const count = dbObjectListCount({
    matched: totalCount ?? items.length,
    unfilteredTotal: unfilteredTotalCount,
    filtered: hasActiveFilter,
    ready: !loading && !error,
  });
  const { previous, next } = adjacentListItems(items, selectedName || null, (item) => formatDbObjectName(item));

  return (
    <ListDetailLayout
      id={idPrefix}
      listLabel={labels.title}
      detailLabel={detailLabel}
      storagePrefix={LIST_DETAIL_STORAGE_PREFIX}
      listHeader={
        <div className="grid gap-3">
          <h2 id={headingId} className="flex items-center gap-2 text-sm font-semibold text-fg">
            <Icon size={16} aria-hidden="true" />
            {labels.title}
          </h2>
          {/* 件数は見出しのバッジではなく、ツールバーの右の ListCount 1 か所に出す（#1891）。狭い列なので縦に積む。 */}
          <DbObjectSearchOwnerFields
            searchLabel={t("dbAdmin.search.label")}
            searchPlaceholder={t("dbAdmin.search.placeholder")}
            searchValue={search}
            onSearchChange={onSearchChange}
            ownerLabel={t("dbAdmin.owner.label")}
            ownerPlaceholder={t("dbAdmin.ownerPrefix.placeholder")}
            ownerValue={ownerPrefix}
            onOwnerChange={onOwnerPrefixChange}
            count={count}
            testId={`${idPrefix}-toolbar`}
            filters={filters}
            activeFilterCount={activeFilterCount}
            filtersLayout={filtersLayout}
            onClearFilters={() => {
              onSearchChange("");
              onOwnerPrefixChange("");
              onClearFilters?.();
            }}
          />
        </div>
      }
      list={
        loading ? (
          <div className="p-3">
            <DbManagementLoadingSkeleton
              idPrefix={idPrefix}
              ariaLabel={labels.loading}
              variant="list"
              activityIcon={loadingActivityIcon}
            />
          </div>
        ) : error ? (
          <div className="p-3">
            <ErrorState message={error} onRetry={onRetry} />
          </div>
        ) : items.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title={hasActiveFilter ? labels.noResultsTitle : labels.emptyTitle}
              hint={hasActiveFilter ? labels.noResultsHint : labels.emptyHint}
            />
          </div>
        ) : (
          <DataTable
            columns={[
              {
                key: "name",
                header: labels.objectName,
                sortable: true,
                className: "align-top",
                render: (item, index) => {
                  const qualifiedName = formatDbObjectName(item);
                  const commentId = `${idPrefix}-comment-${index}`;
                  return (
                    <RowTitleButton
                      title={<DbObjectName value={qualifiedName} size="xs" />}
                      subtitle={showComments ? <DbObjectCommentText id={commentId} comment={item.comment} /> : undefined}
                      current={qualifiedName === selectedName}
                      aria-label={labels.showObject(qualifiedName)}
                      aria-describedby={showComments ? commentId : undefined}
                      onClick={() => onSelect(qualifiedName)}
                    />
                  );
                },
              },
              {
                key: "row_count",
                header: labels.rows,
                sortable: true,
                align: "right",
                headerClassName: "w-[6.5rem]",
                className: "whitespace-nowrap align-top font-sans tabular-nums",
                render: (item) =>
                  kindLabel ? (
                    // 種類は行数の下に短い値として添える（狭い列で列を増やさない。#2315）。
                    <span className="grid justify-items-end gap-1">
                      <span>{formatRowCount(item.row_count)}</span>
                      <StatusBadge
                        icon={false}
                        variant={item.object_type === "view" ? "info" : "neutral"}
                        label={kindLabel(item)}
                      />
                    </span>
                  ) : (
                    formatRowCount(item.row_count)
                  ),
              },
            ]}
            rows={items}
            getRowKey={(item) => formatDbObjectName(item)}
            sort={sort}
            onSortChange={(nextSort) => onSortChange(nextSort.key as DbObjectSortKey)}
            selectedRowKey={selectedName || null}
            onRowClick={(item) => onSelect(formatDbObjectName(item))}
            rowProps={() => ({ className: DB_OBJECT_GRID_ROW_CLASS })}
            testId={`${idPrefix}-grid`}
            scrollTestId="db-admin-object-list"
            // 一覧の列（既定 20rem・16〜32rem）の高さの中で縦にスクロールし、表頭を固定する。列の枠と二重にしない。
            className="min-h-0 rounded-none border-0"
            tableClassName="w-full table-fixed"
            stickyHeader
          />
        )
      }
      listFooter={
        !loading && !error ? (
          <DbObjectSelectorFooter
            visibleCount={items.length}
            totalCount={totalCount ?? items.length}
            hasNextPage={hasNextPage}
            loadingNextPage={loadingNextPage}
            loadMoreError={loadMoreError}
            dataTestId={`${idPrefix}-footer`}
            className="min-h-10"
            onLoadMore={onLoadMore}
            onRetryLoadMore={onRetryLoadMore}
          />
        ) : undefined
      }
      hasSelection={Boolean(selectedName)}
      detail={detail}
      emptyDetail={emptyDetail ?? <EmptyState title={t("dbAdmin.detail.emptyTitle")} hint={t("dbAdmin.detail.emptyHint")} />}
      detailToolbar={
        selectedName ? (
          <ListDetailPager
            onPrevious={previous ? () => onSelect(formatDbObjectName(previous)) : null}
            onNext={next ? () => onSelect(formatDbObjectName(next)) : null}
            labels={{ previous: t("listDetail.previous"), next: t("listDetail.next") }}
            testId={`${idPrefix}-detail-pager`}
          />
        ) : undefined
      }
      labels={{ resize: t("listDetail.resize"), resizeHint: t("listDetail.resizeHint") }}
    />
  );
}

export function DbObjectDetailPanel({
  idPrefix,
  operationKey,
  headingId,
  detail,
  loading,
  ddlLoading = false,
  error,
  ddlError = "",
  exporting = false,
  countingRows = false,
  tab,
  labels,
  loadingActivityIcon,
  onTabChange,
  onRetry,
  onRetryDdl,
  onCancel,
  onExport,
  onExactCount,
  onDrop,
}: {
  idPrefix: string;
  operationKey?: string | number | null;
  headingId: string;
  detail: DbAdminObjectDetail | null;
  loading: boolean;
  ddlLoading?: boolean;
  error: string;
  ddlError?: string;
  exporting?: boolean;
  countingRows?: boolean;
  tab: DbObjectDetailTab;
  labels: DbObjectDetailLabels;
  /**
   * 読込中の表示のスピナー。起点のボタン（PageHeader の「最新情報を取得」など）が loading を出している間は
   * "none" にする（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
   */
  loadingActivityIcon?: ProcessingActivityIcon;
  onTabChange: (tab: DbObjectDetailTab) => void;
  onRetry: () => void;
  onRetryDdl?: () => void;
  onCancel?: () => void;
  onExport?: (name: string) => void;
  onExactCount?: (name: string) => void;
  onDrop: (name: string) => void;
}) {
  if (loading) {
    return (
      <DbManagementLoadingSkeleton
        idPrefix={idPrefix}
        ariaLabel={labels.loading}
        variant="detail"
        operationKey={operationKey ?? idPrefix}
        onCancel={onCancel}
        activityIcon={loadingActivityIcon}
      />
    );
  }

  if (error) {
    return (
      <section className="grid min-w-0 content-start" data-testid={`${idPrefix}-detail-error`}>
        <ErrorState message={error} onRetry={onRetry} />
      </section>
    );
  }

  if (!detail) {
    return <EmptyState title={t("dbAdmin.detail.emptyTitle")} hint={t("dbAdmin.detail.emptyHint")} />;
  }

  const detailTabs = [
    { id: "columns", label: labels.columns, icon: Table2 },
    { id: "ddl", label: labels.ddl, icon: Code2 },
  ] as const;
  const detailQualifiedName = formatDbObjectName(detail);
  const detailActions: EntityAction[] = [
    {
      id: "exact-count",
      label: labels.exactCount ?? "",
      ariaLabel: labels.exactCountAria,
      visible: Boolean(onExactCount && labels.exactCount && detail.object_type === "table"),
      loading: countingRows,
      onSelect: () => {
        if (onExactCount) onExactCount(detailQualifiedName);
      },
    },
    {
      id: "export",
      label: labels.export ?? "",
      ariaLabel: labels.exportAria,
      icon: Download,
      visible: Boolean(onExport && labels.export && labels.exportAria),
      loading: exporting,
      onSelect: () => {
        if (onExport) onExport(detailQualifiedName);
      },
    },
    {
      id: "drop",
      label: labels.drop,
      icon: Trash2,
      tone: "danger",
      onSelect: () => onDrop(detailQualifiedName),
    },
  ];
  const showRowCountBadge = detail.object_type === "table" || detail.row_count != null;

  return (
    // 枠は一覧 + 詳細（ListDetailLayout）の詳細の列が持つ（カードの入れ子を 1 段減らす。#2281）。
    <section className="grid min-w-0 content-start gap-3" aria-labelledby={headingId} data-testid={`${idPrefix}-detail`}>
      <div
        className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between"
        data-testid={`${idPrefix}-detail-header`}
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={headingId} className="min-w-0">
              <DbObjectName value={detailQualifiedName} size="base" />
            </h2>
            <StatusBadge icon={false} variant="neutral" label={detail.object_type} />
            <StatusBadge icon={false} variant="neutral" label={t("dbAdmin.detail.columnCount", { count: detail.columns.length })} />
            {showRowCountBadge && (
              <StatusBadge
                icon={false}
                variant={detail.row_count != null ? "info" : "neutral"}
                label={rowCountLabel(detail.row_count)}
                className="min-w-[4.5rem]"
              />
            )}
          </div>
          {detail.comment && <p className="mt-2 text-sm leading-6 text-fg">{detail.comment}</p>}
        </div>
        <ObjectActionBar
          actions={detailActions}
          ariaLabel={`${labels.actions}: ${detailQualifiedName}`}
          testId={`${idPrefix}-detail-actions`}
        />
      </div>
      {countingRows || exporting ? (
        // 大きな表の COUNT(*) と Excel 出力は数十秒かかる。スピナーは操作の loading が担う（messaging.md §3.7）。
        <ProcessingIndicator
          active
          label={t(countingRows ? "dbAdmin.detail.progress.counting" : "dbAdmin.detail.progress.exporting", {
            name: detailQualifiedName,
          })}
          operationKey={`${countingRows ? "count" : "export"}:${detailQualifiedName}`}
          placement="action"
          activityIcon="none"
          testId={`${idPrefix}-detail-processing`}
        />
      ) : null}

      <WarningsBanner warnings={detail.warnings} />

      <Tabs
        idPrefix={`${idPrefix}-detail`}
        ariaLabel={labels.tabsLabel}
        value={tab}
        onChange={(id) => onTabChange(id as typeof tab)}
        items={detailTabs.map((item) => ({ id: item.id, label: item.label, icon: item.icon }))}
      />

      {tab === "columns" ? (
        <div
          id={`${idPrefix}-detail-panel-columns`}
          role="tabpanel"
          aria-labelledby={`${idPrefix}-detail-tab-columns`}
          className="min-w-0"
        >
          <DbObjectColumnsTable
            columns={detail.columns}
            showComment
            sampleOf={(column) => column.sample_values.join(", ")}
          />
        </div>
      ) : ddlLoading ? (
        <TimedLoadingState
          label={labels.ddlLoading}
          operationKey={`${idPrefix}-${detailQualifiedName}-ddl`}
          onCancel={onCancel}
          placement="tab"
          testId={`${idPrefix}-ddl-skeleton`}
        >
          <Skeleton className="h-11.5" testId={SKELETON_BLOCK_TEST_ID} />
          <Skeleton className="h-82" testId={SKELETON_BLOCK_TEST_ID} />
        </TimedLoadingState>
      ) : ddlError ? (
        <section
          id={`${idPrefix}-detail-panel-ddl`}
          role="tabpanel"
          aria-labelledby={`${idPrefix}-detail-tab-ddl`}
          className="rounded-md border border-border bg-surface p-3"
          data-testid={`${idPrefix}-ddl-error`}
        >
          <ErrorState message={ddlError} onRetry={onRetryDdl} />
        </section>
      ) : (
        <section
          id={`${idPrefix}-detail-panel-ddl`}
          role="tabpanel"
          aria-labelledby={`${idPrefix}-detail-tab-ddl`}
          className="grid gap-3 rounded-md border border-border bg-surface p-3"
        >
          <ContentActionBar
            ariaLabel={`${labels.ddl}: ${labels.actions}`}
            testId={`${idPrefix}-ddl-actions`}
          >
            <CopyButton
              size="sm"
              text={detail.ddl}
              label={t("dbAdmin.detail.copy")}
              copiedMessage={t("dbAdmin.detail.copied")}
              failedMessage={t("common.action.copyFailed")}
              disabled={!detail.ddl}
            />
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={!detail.ddl}
              onClick={() => {
                try {
                  downloadText(`${detailQualifiedName.toLowerCase().replace(".", "_")}_ddl.sql`, detail.ddl);
                  toast.success(t("common.action.downloaded"));
                } catch {
                  toast.error(t("common.action.downloadFailed"));
                }
              }} icon={Download}>
              <span>{t("dbAdmin.detail.download")}</span>
            </Button>
          </ContentActionBar>
          <pre data-surface="code" className="max-h-96 overflow-auto rounded-md border border-border bg-surface p-3 text-sm leading-6 text-fg">
            <code>{detail.ddl || "-"}</code>
          </pre>
        </section>
      )}
    </section>
  );
}

export function DbObjectManagementTabs<T extends string>({
  idPrefix,
  tabs,
  activeView,
  ariaLabel,
  onViewChange,
  disabled = false,
}: {
  idPrefix: string;
  tabs: Array<DbObjectTab<T>>;
  activeView: T;
  ariaLabel: string;
  onViewChange: (view: T) => void;
  disabled?: boolean;
}) {
  // 共有 Tabs に NL2SQL の view 型を渡す adapter。処理中は全タブを無効にする。
  return (
    <Tabs
      idPrefix={idPrefix}
      ariaLabel={ariaLabel}
      value={activeView}
      onChange={(id) => onViewChange(id as T)}
      items={tabs.map((tab) => ({ id: tab.id, label: tab.label, icon: tab.icon, disabled }))}
    />
  );
}

export function DropDbObjectDialog({
  objectName,
  confirmation,
  loading,
  error,
  labels,
  onConfirmationChange,
  onExecute,
  onClose,
}: {
  objectName: string;
  confirmation: string;
  loading: boolean;
  error?: string;
  labels: DbObjectDropDialogLabels;
  onConfirmationChange: (value: string) => void;
  onExecute: () => void;
  onClose: () => void;
}) {
  const canExecute = confirmation.trim() === objectName;
  // 実行中は閉じられない（結果・失敗の理由をダイアログの中に出すため。データの切り詰めと同じ。#1084）。
  const close = () => {
    if (!loading) onClose();
  };
  return (
    <DialogOverlayPortal className="p-3 sm:items-center">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="drop-db-object-dialog-title"
        className="max-h-[90dvh] w-full max-w-3xl overflow-auto rounded-md border border-border bg-surface-overlay shadow-[var(--shadow-dialog)]"
      >
        <div className="flex items-start justify-between gap-3 border-b border-border bg-surface-overlay px-4 py-3">
          <div>
            <h2 id="drop-db-object-dialog-title" className="text-base font-semibold text-danger-fg">
              {labels.title}
            </h2>
            <p className="mt-1 text-sm text-fg-muted">{labels.subtitle}</p>
          </div>
          <Button type="button" variant="ghost" size="sm" disabled={loading} onClick={close} icon={X}>
            <span>{labels.close}</span>
          </Button>
        </div>
        <div className="grid gap-4 p-4">
          <div className="rounded-md border border-border bg-surface-sunken px-3 py-2">
            <p className="text-xs font-semibold text-fg">{labels.target}</p>
            <p className="mt-1">
              <DbObjectName value={objectName} size="sm" />
            </p>
          </div>
          {error && (
            <Banner severity="danger">
              {error}
            </Banner>
          )}
          <fieldset className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3">
            <legend className="px-1 text-sm font-semibold text-fg">{labels.executeTitle}</legend>
            <ExecutionConfirmationField
              value={confirmation}
              onChange={onConfirmationChange}
              confirmed={canExecute}
              placeholder={objectName}
              expectedLabel={objectName}
              helper={labels.executeHint}
              actions={
                <>
                  <Button type="button" variant="danger" size="lg" loading={loading} disabled={!canExecute} onClick={onExecute} icon={Trash2}>
                    <span>{labels.run}</span>
                  </Button>
                  <Button type="button" variant="secondary" size="lg" disabled={loading} onClick={close}>
                    <span>{labels.cancel}</span>
                  </Button>
                </>
              }
            />
          </fieldset>
        </div>
      </section>
    </DialogOverlayPortal>
  );
}
