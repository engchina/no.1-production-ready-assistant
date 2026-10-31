import type { ReactNode } from "react";

import {
  DEFAULT_API_ERROR_DETAIL_LABELS,
  presentApiError,
  type ApiErrorDetail,
  type ApiErrorDetailLabels,
} from "../../lib/api-error";
import { cn } from "../../lib/utils";
import { Banner } from "../ui/banner";
import { Disclosure } from "../ui/disclosure";
import { ErrorState } from "./state-views";

export interface ApiErrorDetailListProps {
  details: readonly ApiErrorDetail[];
  /** 折りたたみの見出し（既定「詳細」）。 */
  label?: string;
  /** 失敗のときは開いて出す（messaging.md §10.3）。 */
  defaultOpen?: boolean;
}

/**
 * 失敗の技術的な詳細（要求・待ち時間の上限・HTTP ステータス・エラーコード・エラー種別・元の文・request ID）を
 * 「詳細」の折りたたみ（`Disclosure`）に並べる（UX 契約 messaging.md §9 P3 / §10.3）。項目が無ければ描かない。
 */
export function ApiErrorDetailList({
  details,
  label = DEFAULT_API_ERROR_DETAIL_LABELS.details,
  defaultOpen = true,
}: ApiErrorDetailListProps) {
  if (details.length === 0) return null;
  return (
    <Disclosure variant="plain" size="sm" summary={label} defaultOpen={defaultOpen}>
      <dl className="grid min-w-0 gap-x-4 gap-y-1 text-xs text-fg-muted sm:grid-cols-2">
        {details.map((item) => (
          <div key={item.label} className="min-w-0">
            <dt className="break-words font-medium text-fg">{item.label}</dt>
            <dd className="whitespace-pre-line break-all">{item.value}</dd>
          </div>
        ))}
      </dl>
    </Disclosure>
  );
}

export interface ApiErrorBannerProps {
  /** API の失敗（`ApiTransportError`・製品の `ApiError`・その他の例外）。 */
  error: unknown;
  /** 要約に使える文が無いときの、画面の操作に合わせた文（「〜を読み込めませんでした。」）。 */
  fallback: string;
  /** 画面の操作に合わせた文で要約を差し替える。 */
  summary?: string;
  /** 画面の操作に合わせた文で次の操作を差し替える。 */
  nextAction?: string;
  /** Banner の右の操作（再試行など）。 */
  action?: ReactNode;
  testId?: string;
  className?: string;
  labels?: ApiErrorDetailLabels;
}

/**
 * API の失敗を danger の `Banner` で出す（UX 契約 messaging.md §10.3。#900 / #906）。
 *
 * 1 文目に何が起きたか、次に次の操作を出し、技術的な詳細は「詳細」に畳んで失敗のときは開いて出す
 * （`SettingsTestResultPanel` と同じ形）。ブラウザの英語の文（`Failed to fetch` など）は要約に出さない。
 */
export function ApiErrorBanner({
  error,
  fallback,
  summary,
  nextAction,
  action,
  testId,
  className,
  labels = DEFAULT_API_ERROR_DETAIL_LABELS,
}: ApiErrorBannerProps) {
  const presentation = presentApiError(error, fallback, labels);
  const next = nextAction ?? presentation.nextAction;
  return (
    <div className={cn("min-w-0", className)} data-testid={testId}>
      <Banner severity="danger" title={summary ?? presentation.summary} action={action}>
        {next || presentation.details.length > 0 ? (
          <div className="min-w-0 space-y-2">
            {next ? <p>{next}</p> : null}
            <ApiErrorDetailList details={presentation.details} label={labels.details} />
          </div>
        ) : null}
      </Banner>
    </div>
  );
}

export interface ApiErrorStateProps {
  /** 取得の失敗（`ApiTransportError`・製品の `ApiError`・その他の例外）。 */
  error: unknown;
  /** 要約に使える文が無いときの文（「〜を読み込めませんでした。」）。 */
  fallback: string;
  onRetry?: () => void;
  retryLabel?: string;
  labels?: ApiErrorDetailLabels;
}

/**
 * 領域の取得の失敗（`ErrorState`。再試行付き）を、API の失敗の要約・次の操作・「詳細」で出す
 * （UX 契約 messaging.md §3.6 / §10.3。#906）。ブラウザの英語の文は「詳細」にだけ出す。
 */
export function ApiErrorState({
  error,
  fallback,
  onRetry,
  retryLabel,
  labels = DEFAULT_API_ERROR_DETAIL_LABELS,
}: ApiErrorStateProps) {
  const presentation = presentApiError(error, fallback, labels);
  return (
    <ErrorState
      message={[presentation.summary, presentation.nextAction].filter(Boolean).join("")}
      onRetry={onRetry}
      retryLabel={retryLabel}
      details={<ApiErrorDetailList details={presentation.details} label={labels.details} />}
    />
  );
}
