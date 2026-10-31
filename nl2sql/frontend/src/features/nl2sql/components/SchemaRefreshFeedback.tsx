import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { PageHeaderStatusBadge } from "@/components/PageHeaderStatusBadge";
import { t } from "@/lib/i18n";

import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import {
  schemaRefreshHeaderPresentation,
  schemaRefreshProcessingLabel,
  type SchemaSyncScope,
} from "../schemaRefreshPresentation";
import type { MutationSchemaSync } from "../useMutationSchemaSync";
import {
  ApiErrorDetailList,
  Banner,
  Button,
  DEFAULT_API_ERROR_DETAIL_LABELS,
  ProcessingIndicator,
  presentApiError,
  type ApiErrorDetail,
  type ProcessingActivityIcon,
  type ProcessingPlacement,
} from "@production-ready/ui";

export function SchemaRefreshHeaderStatus({ testId }: { testId?: string }) {
  const { error, isStarting, job } = useSchemaRefreshCoordinator();
  const presentation = schemaRefreshHeaderPresentation(job, {
    starting: isStarting,
    error: Boolean(error),
  });
  if (!presentation) return null;
  return (
    <PageHeaderStatusBadge
      variant={presentation.variant}
      label={presentation.label}
      announcementLabel={presentation.announcementLabel}
      testId={testId}
    />
  );
}

export function SchemaRefreshProcessing({
  placement = "workspace",
  className = "rounded-md border border-border bg-surface-sunken px-3 py-2",
  testId,
  activityIcon,
}: {
  placement?: ProcessingPlacement;
  className?: string;
  testId?: string;
  /**
   * 省略時は、押した「スキーマを更新」が送信中（`loading`）の間だけ "none"、job の間は "spinner"。
   * スキーマの更新は画面をまたいで続く durable job なので、job の間のスピナーはこの進行の表示が 1 つだけ
   * 出し、ボタンは `disabled` にするだけ（UX 契約 messaging §3.7「進捗の表示に残す」、#821）。
   */
  activityIcon?: ProcessingActivityIcon;
}) {
  const { isRefreshing, job, startingOrigin } = useSchemaRefreshCoordinator();
  if (!isRefreshing) return null;
  return (
    <ProcessingIndicator
      active
      label={schemaRefreshProcessingLabel(job)}
      operationKey={job?.job_id || "schema-refresh-starting"}
      startedAt={job?.started_at ?? job?.created_at}
      placement={placement}
      className={className}
      testId={testId}
      activityIcon={activityIcon ?? (startingOrigin ? "none" : "spinner")}
      announceActivity={false}
      announceSlow={false}
    />
  );
}

/**
 * 変更の後の DB 構造の同期の案内（`useMutationSchemaSync` の `view`）を、起点の操作の結果の直下に 1 か所だけ
 * 出す（UX 契約 messaging §10.1・§9 P2。#2031）。
 *
 * - 差分同期の job: 進行（`SchemaRefreshProcessing`）だけ。完了で消える。
 * - 再取得を求める案内: warning の `Banner` と「DB 構造を再取得」。押すとボタンは送信の間だけ `loading`、
 *   job の間は `disabled` にし、進行（経過時間・スピナー）は同じ Banner の中のボタンの直下に出す
 *   （durable job のスピナーは進行の表示が 1 つだけ出す。messaging §3.7、#821）。完了で案内ごと消える。
 * - 失敗: 同じ場所の danger の `Banner`（原因・次の行動・開いた「詳細」）と「DB 構造を再取得」。
 *
 * `scope` が `view.scope` と同じときだけ描く（ページの先頭と結果の直下の 2 か所に置き、どちらか 1 つが出る）。
 * `fallbackProgress` は、案内が進行を出していないときに、別の起点（ヘッダー・別の画面）の再取得の進行を
 * この場所に出す（同じ画面のほかの場所が進行を出しているときは false を渡す）。
 */
export function SchemaSyncNotice({
  sync,
  scope = "inline",
  testId,
  actionTestId,
  processingTestId,
  className,
  fallbackProgress = false,
  children,
}: {
  sync: MutationSchemaSync;
  scope?: SchemaSyncScope;
  fallbackProgress?: boolean;
  testId?: string;
  /** 案内の中の「DB 構造を再取得」の data-testid。 */
  actionTestId?: string;
  processingTestId?: string;
  className?: string;
  /** 本文の下に足す内容（失敗した job の警告の一覧など。同じ案内の中に 1 か所だけ出す）。 */
  children?: ReactNode;
}) {
  const { isRefreshing, startingOrigin } = useSchemaRefreshCoordinator();
  const { view } = sync;
  const starting = startingOrigin === sync.noticeOrigin;
  if (!view || view.scope !== scope) {
    return fallbackProgress && isRefreshing && !sync.ownsProgress ? (
      <SchemaRefreshProcessing placement="job" testId={processingTestId} className={className} />
    ) : null;
  }
  if (view.kind === "syncing") {
    return scope === "inline" ? (
      <SchemaRefreshProcessing placement="job" testId={processingTestId} className={className} />
    ) : null;
  }
  const presentation = view.error ? presentApiError(view.error, view.message || t("dataMgmt.schemaJob.error")) : null;
  const details: ApiErrorDetail[] = presentation
    ? presentation.details
    : view.errorCode
      ? [{ label: DEFAULT_API_ERROR_DETAIL_LABELS.errorCode, value: view.errorCode }]
      : [];
  // 再取得の間は、押した案内の文だけを残し、失敗の詳細は畳んだまま出さない。
  const showDetails = !view.refreshing && details.length > 0;
  const title = presentation && !view.refreshing ? presentation.summary : view.title;
  const body = presentation && !view.refreshing ? presentation.nextAction : view.message;
  return (
    <div className={className ? `min-w-0 ${className}` : "min-w-0"} data-testid={testId}>
      <Banner
        severity={view.severity}
        title={title}
        action={
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            // 押したときの送信の間だけ回し、job の間は無効にするだけ（スピナーは進行の表示。#821）。
            loading={starting}
            disabled={isRefreshing && !starting}
            onClick={() => void sync.refresh()}
            data-testid={actionTestId}
          >
            <span>{t("common.action.schemaRefresh")}</span>
          </Button>
        }
        progress={
          view.refreshing ? (
            <SchemaRefreshProcessing placement="job" className="" testId={processingTestId} />
          ) : null
        }
      >
        {body || showDetails || children ? (
          <div className="min-w-0 space-y-2">
            {body ? <p>{body}</p> : null}
            {showDetails ? <ApiErrorDetailList details={details} /> : null}
            {children}
          </div>
        ) : null}
      </Banner>
    </div>
  );
}
