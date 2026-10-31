import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import {
  ApiErrorDetailList,
  Banner,
  Button,
  DEFAULT_API_ERROR_DETAIL_LABELS,
  ProcessingIndicator,
  presentApiError,
  type ApiErrorDetail,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";

import {
  dbProfileListSyncProcessingLabel,
  type DbProfileListSyncScope,
} from "../dbProfileListSyncPresentation";
import type { DbProfileListSync } from "../useDbProfileListSync";

/** DB Profile 一覧の再取得・差分同期の進行（経過時間）。 */
export function DbProfileListSyncProcessing({
  sync,
  className = "rounded-md border border-border bg-surface-sunken px-3 py-2",
  testId,
}: {
  sync: DbProfileListSync;
  className?: string;
  testId?: string;
}) {
  if (!sync.busy) return null;
  return (
    <ProcessingIndicator
      active
      label={dbProfileListSyncProcessingLabel(sync.job)}
      operationKey={sync.job?.job_id || "db-profile-list-refresh-starting"}
      startedAt={sync.job?.started_at ?? sync.job?.created_at}
      placement="job"
      className={className}
      testId={testId}
      // 押したボタンが送信中（loading）の間は回さず、job の間はこの表示が 1 つだけ回す（messaging §3.7、#821）。
      activityIcon={sync.noticeStarting || sync.externalStarting ? "none" : "spinner"}
      announceActivity={false}
      announceSlow={false}
    />
  );
}

/**
 * 変更の後の DB Profile 一覧の同期の案内（`useDbProfileListSync` の `view`）を、起点の操作の結果の直下に
 * 1 か所だけ出す（DB 構造の `SchemaSyncNotice` と同じ形。messaging §10.1・§10.2、#2034）。
 *
 * - 差分同期の job: 進行だけ（`scope` が `inline` のとき）。完了で消える。
 * - 再取得を求める案内: warning の `Banner` と「DB Profile 一覧を再取得」。押すとボタンは送信の間だけ
 *   `loading`、job の間は `disabled` にし、進行は同じ Banner の中のボタンの直下に出す。完了で案内ごと消える。
 * - 失敗: 同じ場所の danger の `Banner`（原因・次の行動・開いた「詳細」）と「DB Profile 一覧を再取得」。
 *
 * `scope` が `view.scope` と同じときだけ描く（ページの先頭と結果の直下の 2 か所に置き、どちらか 1 つが出る）。
 */
export function DbProfileListSyncNotice({
  sync,
  scope = "inline",
  testId,
  actionTestId,
  processingTestId,
  className,
  actions,
  children,
}: {
  sync: DbProfileListSync;
  scope?: DbProfileListSyncScope;
  testId?: string;
  actionTestId?: string;
  processingTestId?: string;
  className?: string;
  /** 「DB Profile 一覧を再取得」の右に並べる操作（別の画面を開く導線など）。 */
  actions?: ReactNode;
  /** 本文の下に足す内容。 */
  children?: ReactNode;
}) {
  const { view } = sync;
  if (!view || view.scope !== scope) return null;
  if (view.kind === "syncing") {
    // ページの先頭（案内の外のボタンで始めた再取得）の進行は、ページの処理中の表示が出す（重ねない）。
    return scope === "inline" ? (
      <DbProfileListSyncProcessing sync={sync} testId={processingTestId} className={className} />
    ) : null;
  }
  const presentation = view.error
    ? presentApiError(view.error, view.message || t("profiles.dbProfileRefresh.error"))
    : null;
  const details: ApiErrorDetail[] = presentation
    ? presentation.details
    : view.errorCode
      ? [{ label: DEFAULT_API_ERROR_DETAIL_LABELS.errorCode, value: view.errorCode }]
      : [];
  // 再取得の間は、押した案内の文だけを残し、失敗の詳細は出さない。
  const showDetails = !view.refreshing && details.length > 0;
  const title = presentation && !view.refreshing ? presentation.summary : view.title;
  const body = presentation && !view.refreshing ? presentation.nextAction : view.message;
  return (
    <div className={className ? `min-w-0 ${className}` : "min-w-0"} data-testid={testId}>
      <Banner
        severity={view.severity}
        title={title}
        action={
          <>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              icon={RefreshCw}
              className="w-full sm:w-auto"
              // 押したときの送信の間だけ回し、job の間は無効にするだけ（スピナーは進行の表示。#821）。
              loading={sync.noticeStarting}
              disabled={sync.busy && !sync.noticeStarting}
              onClick={() => void sync.refresh()}
              data-testid={actionTestId}
            >
              <span>{t("profiles.action.dbProfileRefresh")}</span>
            </Button>
            {actions}
          </>
        }
        progress={
          view.refreshing ? (
            <DbProfileListSyncProcessing sync={sync} className="" testId={processingTestId} />
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
