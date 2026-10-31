import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { INFORMATION_LIST_ROW_CLASS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import {
  CursorPagination,
  OffsetPagination,
  type CursorPaginationProps,
  type OffsetPaginationProps,
} from "../data/pagination";
import { ApiErrorBanner } from "../feedback/api-error-banner";
import { TimedLoadingState } from "../feedback/processing-state";
import { Button } from "../ui/button";
import { ListSkeleton } from "../ui/skeleton";

/** 履歴の 1 行（製品の会話・スレッドの型から写す）。 */
export interface ChatHistoryItem {
  id: string;
  /** 会話の名前（翻訳済み。名前の無い会話は「無題の会話」などを製品が入れる）。 */
  title: string;
  /** 名前の下の補足（更新日時・件数など。翻訳済み）。 */
  meta?: ReactNode;
  /** 補足の後ろの状態（`StatusBadge` など。完了以外の状態だけを出す）。 */
  badge?: ReactNode;
}

export interface ChatHistoryListLabels {
  /** 一覧の名前（例:「会話の履歴」）。 */
  list: string;
  /** 読み込み中の文（例:「会話を読み込んでいます」）。 */
  loading: string;
  /** 読み込めなかったときの文（例:「会話の一覧を読み込めませんでした。」）。 */
  error: string;
  /** 再試行のボタン（例:「再試行」）。 */
  retry: string;
  /** 0 件の文（例:「まだ会話がありません」）。 */
  empty: string;
}

/**
 * 会話の履歴のページング（3 製品共通。#1265 / #1266）。共通の `OffsetPagination` / `CursorPagination` で出す。
 * offset の API（3 製品。NL2SQL も #1891 で移した）は `type: "offset"`（今のページが空になったら最後のページへ寄せる）、
 * カーソルの API は `type: "cursor"`（前へ戻るカーソルは製品が積む）。1 ページしかないときは出さない。
 */
export type ChatHistoryPagination =
  | ({ type: "offset" } & Omit<OffsetPaginationProps, "testId" | "className">)
  | ({ type: "cursor" } & Omit<CursorPaginationProps, "testId" | "className">);

export interface ChatHistoryListTestIds {
  /** 対象の一覧を待っている間の形だけの Skeleton（`waiting`）。 */
  skeleton?: string;
  loading?: string;
  error?: string;
  list?: string;
  pagination?: string;
}

export interface ChatHistoryListProps {
  items: ChatHistoryItem[];
  /** 開いている会話（`aria-current` と行の地の色）。 */
  currentId?: string | null;
  /** 会話を選ぶ。lg 未満のシートは製品がここで閉じる（`ChatHistoryPanel.closeSheet`）。 */
  onSelect: (item: ChatHistoryItem) => void;
  /** 選べない間（送信の要求中など）。 */
  disabled?: boolean;
  /**
   * 画面の対象（検索・回答プロファイル / 業務 Agent など）を読み込んでいて、履歴をまだ取得できない（#1153）。
   * 「まだ会話がありません」と出さず、一覧の形の Skeleton だけを出す（経過時間は対象のカードが出すので重ねない）。
   */
  waiting?: boolean;
  /** 最初の読み込み中。 */
  loading?: boolean;
  /** 一覧を読めなかったときの失敗（読めていない一覧を「まだ会話がありません」と出さない）。 */
  error?: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  labels: ChatHistoryListLabels;
  /** 行の右の操作（名前の変更・削除など。API がある製品だけ）。sm 以上はホバー・フォーカスの間と開いている会話で出す。 */
  renderActions?: (item: ChatHistoryItem, current: boolean) => ReactNode;
  /** 行をその場で編集している間の中身（名前の変更の欄）。`null` なら通常の行。 */
  renderEditor?: (item: ChatHistoryItem) => ReactNode | null;
  /** 一覧の下のページング（共通の `Pagination`。#1265）。一覧の下に常に見せる。 */
  pagination?: ChatHistoryPagination;
  testIds?: ChatHistoryListTestIds;
  /** 読み込み中の経過時間の key（同じ取得の経過時間を 1 か所に出す）。 */
  operationKey?: string;
}

/**
 * チャットの会話の履歴の一覧（3 製品共通。UX 契約 page-archetypes.md §6、#1161）。`ChatLayout` の `historyContent` に渡す。
 *
 * - 行は名前（1 行で省略し `title` で全文）・補足（更新日時・件数）・状態のバッジ。開いている会話は地の色と `aria-current`。
 * - 読み込み中は文言と経過時間と行の形の `Skeleton`、失敗は `ApiErrorBanner` と再試行、0 件は短い文（messaging.md §3.6）。
 * - 「新しい会話」は一覧に置かない（会話の領域の上端の行の 1 か所。#889）。
 * - パネル・シートの高さまで伸ばし、超えたら一覧の中をスクロールする。`pagination` のページ送りは一覧の下に常に見せる
 *   （3 製品とも 25 件/ページ。件数「a - b / n 件」と「前へ / N / M ページ / 次へ」。#1265）。
 */
const HISTORY_PAGINATION_CLASS = "shrink-0 border-t border-border pt-2";

function HistoryPagination({ pagination, testId }: { pagination: ChatHistoryPagination; testId?: string }) {
  if (pagination.type === "cursor") {
    const { type: _type, ...props } = pagination;
    return <CursorPagination {...props} testId={testId} className={HISTORY_PAGINATION_CLASS} />;
  }
  const { type: _type, ...props } = pagination;
  return <OffsetPagination {...props} testId={testId} className={HISTORY_PAGINATION_CLASS} />;
}

export function ChatHistoryList({
  items,
  currentId,
  onSelect,
  disabled = false,
  waiting = false,
  loading = false,
  error,
  onRetry,
  retrying = false,
  labels,
  renderActions,
  renderEditor,
  pagination,
  testIds,
  operationKey,
}: ChatHistoryListProps) {
  if (waiting) return <ListSkeleton rows={3} rowClassName="h-12" testId={testIds?.skeleton} />;
  if (loading) {
    return (
      <TimedLoadingState label={labels.loading} operationKey={operationKey} framed={false} testId={testIds?.loading}>
        <ListSkeleton rows={3} rowClassName="h-12" />
      </TimedLoadingState>
    );
  }
  if (error) {
    return (
      <ApiErrorBanner
        error={error}
        fallback={labels.error}
        testId={testIds?.error}
        action={
          onRetry ? (
            <Button type="button" variant="secondary" size="sm" icon={RefreshCw} loading={retrying} onClick={onRetry}>
              {labels.retry}
            </Button>
          ) : undefined
        }
      />
    );
  }
  if (items.length === 0) {
    return (
      <>
        <p className="px-1 text-sm text-fg-muted">{labels.empty}</p>
        {pagination ? <HistoryPagination pagination={pagination} testId={testIds?.pagination} /> : null}
      </>
    );
  }
  return (
    <>
      <ul
        className="min-h-0 flex-1 space-y-1 overflow-y-auto overscroll-contain [scrollbar-gutter:stable]"
        aria-label={labels.list}
        data-testid={testIds?.list}
      >
        {items.map((item) => {
          const current = item.id === currentId;
          const editor = renderEditor?.(item) ?? null;
          if (editor !== null) {
            return (
              <li key={item.id}>
                <div data-surface-tint="accent" className="rounded-md bg-accent-subtle p-2">
                  {editor}
                </div>
              </li>
            );
          }
          const actions = renderActions?.(item, current);
          return (
            <li key={item.id} className="group">
              <div
                data-surface-tint={current ? "accent" : undefined}
                className={cn(
                  "grid grid-cols-[minmax(0,1fr)_auto] rounded-md transition-colors",
                  INFORMATION_LIST_ROW_CLASS,
                  current ? "bg-accent-subtle text-fg" : "text-fg-muted hover:bg-surface-hover hover:text-fg"
                )}
              >
                <button
                  type="button"
                  onClick={() => onSelect(item)}
                  disabled={disabled}
                  aria-current={current ? "true" : undefined}
                  className="flex min-w-0 flex-col justify-center gap-0.5 rounded-md px-3 py-2 text-left text-sm focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <span className="truncate font-medium" title={item.title}>
                    {item.title}
                  </span>
                  {item.meta || item.badge ? (
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs tabular-nums text-fg-muted">
                      {item.meta ? <span>{item.meta}</span> : null}
                      {item.badge}
                    </span>
                  ) : null}
                </button>
                {actions ? (
                  <div
                    className={cn(
                      "mr-1 flex items-center gap-1 self-center transition-opacity sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100",
                      current && "sm:opacity-100"
                    )}
                  >
                    {actions}
                  </div>
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
      {pagination ? <HistoryPagination pagination={pagination} testId={testIds?.pagination} /> : null}
    </>
  );
}
