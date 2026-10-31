import { CircleStop, Database } from "lucide-react";
import type { ReactNode } from "react";

import { RunUsedModels, ChatProgress, Button, Banner, FormStatus } from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { CHAT_PROGRESS_LABELS, chatQueueProgressSteps, jobTracePath } from "../chatProgress";
import type { JobData } from "../types";
import { useNl2SqlTraceProgress } from "../useTraceProgress";
import { GeneratedSqlSummary } from "./GeneratedSqlPanel";
import { JobFailureBody } from "./JobFailureBody";
import { ProgressLegacyNotice } from "./ProgressLegacyNotice";

export function OperationStatusStrip({
  job,
  profileId = "",
  startedAtMs,
  catalogEmpty = false,
  importingSample = false,
  onImportSample,
  sampleImportProgress,
  sampleImportError = "",
  sampleImportUnavailableHint = "",
  onPreviewExecute,
  previewExecuteLoading = false,
  onCancelJob,
  cancelRequesting = false,
  cancelAccepted = false,
  cancelErrorMessage = "",
}: {
  job: JobData | null;
  profileId?: string;
  startedAtMs: number | null;
  /** schema catalog が空（サンプル未投入）か。空 catalog 由来の失敗をアクション化する。 */
  catalogEmpty?: boolean;
  importingSample?: boolean;
  onImportSample?: () => void;
  /** 取込の進行（失敗の案内の中の「サンプルデータを取込」の直下に出す。#2034）。 */
  sampleImportProgress?: ReactNode;
  /** 取込の失敗（同じ案内の中に出す。Toast・ページの先頭に重ねない。#2034）。 */
  sampleImportError?: string;
  sampleImportUnavailableHint?: string;
  /** プレビューの生成 SQL に実行ボタンを出すためのハンドラ。 */
  onPreviewExecute?: () => void;
  previewExecuteLoading?: boolean;
  /** 実行中 job の協調キャンセル要求(POST /jobs/{id}/cancel)。取消可能な処理は同じ領域に置く。 */
  onCancelJob?: () => void;
  cancelRequesting?: boolean;
  /** 中止の要求を受け付けた。backend が実行中の段階を終えて止まるまで、受付を出して中止を押せなくする（#918）。 */
  cancelAccepted?: boolean;
  /** 中止の要求の失敗。中止のボタンの行に出す（messaging §10.1。#918）。 */
  cancelErrorMessage?: string;
}) {
  const active = job?.status === "pending" || job?.status === "running";
  const finalElapsed =
    job?.elapsed_ms ?? job?.result?.timing.elapsed_ms ?? job?.timing?.elapsed_ms;
  // 処理の経過は実行の span の木（#3226 の案 A）。span は worker が共通の span の表に書き、実行中はジョブの処理の
  // 経過の API（span の表の snapshot）を読み直す（段の中の span も実行中に出る）。worker が受け取るまでは開始待ちの
  // 仮の行、span の無い古い形式の記録は案内の文を出す。
  const progress = useNl2SqlTraceProgress({
    targetId: job?.job_id ?? "",
    path: job ? jobTracePath(job.job_id) : null,
    active,
  });
  if (!job) return null;

  const queuePlaceholder = progress.steps.length === 0 && active;
  const steps = queuePlaceholder ? chatQueueProgressSteps(job.created_at) : progress.steps;
  const legacy = !active && progress.legacy;
  const progressKey = `${job.job_id}#${job.attempt ?? 0}`;
  // プレビュー経路: execute/format 未実行(skipped)。「完了」ではなく確認を促す文言に差し替える。
  const warningMessage = job.warning_message?.trim();
  const errorMessage = job.status === "error" ? job.error_message?.trim() : "";
  // 利用者要求のキャンセルは「失敗」ではなく警告トーンで表示する。
  const cancelled = job.error_code === "JOB_CANCELLED";
  const cancelledMessage = cancelled ? errorMessage : "";
  const failureMessage = cancelled ? "" : errorMessage;

  return (
    <div data-testid="nl2sql-job-progress" data-job-status={job.status}>
    {legacy ? (
      <div className="mx-4 my-3">
        <ProgressLegacyNotice testId="nl2sql-job-progress-legacy" />
      </div>
    ) : (
      <ChatProgress
        active={active}
        activityIndicator={!cancelRequesting}
        // 仮の行から span の木に替わったら一覧を作り直す（仮の行を残さない）。
        progressKey={queuePlaceholder ? `${progressKey}:queue` : progressKey}
        startedAt={startedAtMs}
        elapsedMs={finalElapsed}
        cancelled={cancelled}
        reconnecting={active && progress.failed}
        traceId={progress.traceId}
        processDetailsProps={{ metadata: { conversationId: null, messageId: job.job_id } }}
        footer={<RunUsedModels models={[]} events={progress.events} />}
        testId="nl2sql-job"
        labels={CHAT_PROGRESS_LABELS}
        partial={progress.partial}
        steps={steps}
      />
    )}
        <>
          {active && onCancelJob && (
            <div className="mx-4 mb-4 flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
              <div className="mr-auto min-w-0" data-testid="nl2sql-job-cancel-feedback">
                <FormStatus
                  tone={cancelAccepted ? "info" : "danger"}
                  message={cancelAccepted ? t("nl2sql.job.cancelAccepted") : cancelErrorMessage}
                />
              </div>
              {/* 取り消せる停止は赤文字の ghost（buttons.md の Action Button の注）。 */}
              <Button icon={CircleStop}
                type="button"
                variant="ghost"
                tone="danger"
                size="sm"
                loading={cancelRequesting}
                disabled={cancelAccepted}
                onClick={onCancelJob}
              >
                {t("nl2sql.job.cancel")}
              </Button>
            </div>
          )}

          {warningMessage && (
            <Banner severity="warning" className="mx-4 mb-4">
              {warningMessage}
            </Banner>
          )}

          {cancelledMessage && (
            <div data-testid="nl2sql-job-cancelled">
              <Banner severity="warning" className="mx-4 mb-4">
                {cancelledMessage}
              </Banner>
            </div>
          )}

          {failureMessage && (
            <Banner
              severity="danger"
              className="mx-4 mb-4"
              // 取込の進行と失敗は、押した「サンプルデータを取込」の直下（同じ案内の中）に出す（#2034）。
              progress={
                job.status === "error" && catalogEmpty && onImportSample
                  ? sampleImportProgress ??
                    (sampleImportError ? (
                      <div data-testid="nl2sql-sample-import-error">
                        <FormStatus tone="danger" message={sampleImportError} />
                      </div>
                    ) : undefined)
                  : undefined
              }
              action={job.status === "error" && catalogEmpty && onImportSample ? (
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="primary"
                    size="sm"
                    loading={importingSample}
                    onClick={onImportSample} icon={Database}>
                    <span>{t("nl2sql.sample.import")}</span>
                  </Button>
                  <span className="text-xs text-fg-muted">{t("nl2sql.sample.importHint")}</span>
                </div>
              ) : undefined}
            >
              <JobFailureBody
                message={failureMessage}
                errorCode={job.error_code}
                errorDetail={job.error_detail}
              />
              {job.status === "error" && catalogEmpty && !onImportSample && sampleImportUnavailableHint && (
                <p className="text-xs text-fg-muted">{sampleImportUnavailableHint}</p>
              )}
            </Banner>
          )}
        </>
    {job.result ? (
      <div data-testid="nl2sql-generated-sql">
      <GeneratedSqlSummary result={job.result} profileId={profileId} onExecute={onPreviewExecute} executeLoading={previewExecuteLoading} />
      </div>
    ) : null}
    </div>
  );
}
