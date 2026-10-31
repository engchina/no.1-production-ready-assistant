import { ListPlus, RefreshCw } from "lucide-react";

import { isTimeoutError } from "../../lib/api-error";
import { cn } from "../../lib/utils";
import { Banner } from "../ui/banner";
import { Button } from "../ui/button";
import { Spinner } from "../ui/spinner";

export interface LoadMoreFooterProps {
  /** 件数の文言（翻訳済み。例:「100 / 3,000 件を表示、選択 3 件」）。 */
  summary: string;
  /** 続きがあるか（「さらに読み込む」を出す）。 */
  hasMore?: boolean;
  loadingMore?: boolean;
  /** 続きの読み込みの失敗（再試行付きの Banner で出す。出している間は「さらに読み込む」を出さない）。 */
  loadMoreError?: string;
  onLoadMore?: () => void;
  /** 失敗の後の再試行（省略時は onLoadMore）。 */
  onRetry?: () => void;
  loadMoreLabel: string;
  retryLabel: string;
  /** 条件を変えて取り直している間（件数の代わりに出す）。 */
  refreshing?: boolean;
  refreshingLabel?: string;
  /** 件数を aria-live（polite）で伝えるか（既定 false。選択のたびに読み上げない）。 */
  announce?: boolean;
  /** 条件の取り直し中・パネルが無効の間など、続きを読ませない（ボタンを無効にする。消すと高さが変わる。#1266）。 */
  disabled?: boolean;
  className?: string;
  testId?: string;
}

/**
 * 追加読み込み型の一覧のフッター（#600。NL2SQL の `DbObjectSelectorFooter` を共通にした）。
 * 左に件数（「N / M 件を表示」）、右に「さらに読み込む」。続きの読み込みに失敗したら、再試行付きの Banner を出す
 * （同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
 * カーソル型・offset 型のどちらの API でも使う（UX 契約 page-archetypes.md「一覧の型と、基準から外す例外」）。
 */
export function LoadMoreFooter({
  summary,
  hasMore = false,
  loadingMore = false,
  loadMoreError,
  onLoadMore,
  onRetry,
  loadMoreLabel,
  retryLabel,
  refreshing = false,
  refreshingLabel,
  announce = false,
  disabled = false,
  className,
  testId,
}: LoadMoreFooterProps) {
  const retry = onRetry ?? onLoadMore;
  return (
    <div className={cn("grid min-w-0 gap-2", className)} data-testid={testId}>
      <div className="flex flex-col gap-2 sm:min-h-[var(--button-height-sm)] sm:flex-row sm:items-center sm:justify-between">
        {refreshing && refreshingLabel ? (
          <p className="flex items-center gap-2 text-xs text-fg-muted" role="status">
            <Spinner size={14} className="text-accent-fg" />
            <span>{refreshingLabel}</span>
          </p>
        ) : (
          <p className="text-xs text-fg-muted tnum" aria-live={announce ? "polite" : undefined}>
            {summary}
          </p>
        )}
        {hasMore && onLoadMore && !loadMoreError ? (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={ListPlus}
            className="w-full sm:w-auto"
            loading={loadingMore}
            disabled={disabled}
            onClick={onLoadMore}
            data-testid={testId ? `${testId}-load-more` : undefined}
          >
            <span>{loadMoreLabel}</span>
          </Button>
        ) : null}
      </div>
      {loadMoreError ? (
        <Banner
          severity="danger"
          action={
            retry ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                icon={RefreshCw}
                className="w-full sm:w-auto"
                loading={loadingMore}
                disabled={disabled}
                onClick={retry}
              >
                <span>{retryLabel}</span>
              </Button>
            ) : undefined
          }
        >
          {loadMoreError}
        </Banner>
      ) : null}
    </div>
  );
}

/**
 * 続きの読み込みの失敗の文言（#1266。NL2SQL の `objectListLoadMoreErrorMessage` を共通にした）。
 * 待ち時間の上限（`TimeoutError`）は `timeoutMessage`、ほかは例外の message、message が無ければ `fallback`。
 */
export function loadMoreErrorMessage(
  error: unknown,
  { timeoutMessage, fallback }: { timeoutMessage: string; fallback: string }
) {
  if (isTimeoutError(error)) return timeoutMessage;
  return error instanceof Error && error.message ? error.message : fallback;
}
