import {
  ApiErrorState as UiApiErrorState,
  LoadingState,
  EmptyState,
  ErrorState as UiErrorState,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";

// Loading / Empty はそのまま再公開。
export { LoadingState, EmptyState };

/**
 * エラー状態。共有 UI パッケージの ErrorState に RAG の i18n（再試行ラベル）を注入するラッパ。
 */
export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return <UiErrorState message={message} onRetry={onRetry} retryLabel={t("common.retry")} />;
}

/**
 * 取得の失敗（API の失敗）のエラー状態。要約と次の操作を本文に、技術的な詳細（HTTP ステータス・request ID・
 * 通信断のときのブラウザの英語の文など）を「詳細」に出す（UX 契約 messaging.md §10.3。#906）。
 */
export function ApiErrorState({
  error,
  fallback,
  onRetry,
}: {
  error: unknown;
  fallback: string;
  onRetry?: () => void;
}) {
  return (
    <UiApiErrorState
      error={error}
      fallback={fallback}
      onRetry={onRetry}
      retryLabel={t("common.retry")}
    />
  );
}
