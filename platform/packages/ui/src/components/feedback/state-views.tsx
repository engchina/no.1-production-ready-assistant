import { AlertCircle, Inbox, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "../ui/button";
import { MessageText } from "../ui/message-text";
import { Skeleton } from "../ui/skeleton";
import { TimedLoadingState } from "./processing-state";

/**
 * 読込状態。ほかの読み込み中と同じく、領域の先頭に処理の名前と経過時間の行（`TimedLoadingState`）を出し、
 * その下を行の形の Skeleton で覆う（#2510）。1 秒超の取得はブロッキングスピナーでなくこれを使う
 * （progressive-loading）。領域寸法を予約して CLS を防ぐため、行数を rows で指定する。
 */
export function LoadingState({
  label,
  rows = 3,
  operationKey,
  testId,
}: {
  label: string;
  rows?: number;
  operationKey?: string | number | null;
  testId?: string;
}) {
  return (
    <TimedLoadingState label={label} operationKey={operationKey} framed={false} testId={testId}>
      <div className="flex flex-col gap-2" aria-hidden="true">
        {Array.from({ length: rows }).map((_, index) => (
          <Skeleton key={index} className="h-5 w-full" />
        ))}
      </div>
    </TimedLoadingState>
  );
}

/**
 * エラー状態（再試行ボタン付き）。
 * 再試行ラベルは i18n をパッケージに持ち込まないため `retryLabel` で注入（既定「再試行」）。
 */
export function ErrorState({
  message,
  onRetry,
  retryLabel = "再試行",
  details,
}: {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
  /** 技術的な詳細（`ApiErrorDetailList`）。本文の下に、左揃えで全幅に出す（messaging.md §10.3。#906）。 */
  details?: ReactNode;
}) {
  return (
    <div
      role="alert"
      data-surface-tint="danger"
      className="flex flex-col items-center gap-3 rounded-lg border border-danger-border bg-danger-subtle p-8 text-center"
    >
      <AlertCircle size={24} className="text-danger-fg" aria-hidden />
      <p className="text-sm leading-relaxed text-fg">
        <MessageText text={message} />
      </p>
      {details ? <div className="w-full min-w-0 text-left">{details}</div> : null}
      {onRetry ? (
        <Button type="button" variant="secondary" size="sm" icon={RefreshCw} onClick={onRetry}>
          <span>{retryLabel}</span>
        </Button>
      ) : null}
    </div>
  );
}

/** 空状態。任意で操作（作成導線など）を添える。 */
export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-1 py-10 text-center">
      <Inbox size={24} className="text-fg-muted" aria-hidden />
      <p className="mt-1 text-sm leading-relaxed text-fg">
        <MessageText text={title} />
      </p>
      {hint ? (
        <p className="max-w-md text-xs leading-relaxed text-fg-muted">
          <MessageText text={hint} />
        </p>
      ) : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}
