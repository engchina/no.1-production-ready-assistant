import {
  ApiErrorDetailList,
  Banner,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  ProcessingIndicator,
  Skeleton,
  StatusBadge,
  TimedLoadingState,
} from "@production-ready/ui";
import { CircleStop, ListChecks } from "lucide-react";

import { ErrorState } from "@/components/StateViews";
import type { EvaluationJob } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";

import {
  EVALUATION_JOB_STATUS_VARIANT,
  evaluationJobCountLabel,
  evaluationJobCurrentCaseLabel,
  evaluationJobStatusLabel,
  evaluationJobTimeLimitLabel,
  isEvaluationJobActive,
  splitEvaluationJobFailure,
} from "./evaluation-job";

/**
 * 品質評価の job（#390）の実行状況。durable job なので、進捗（終わったケースの数 / 全体・今のケース）と
 * サーバーの開始時刻からの経過時間を `ProcessingIndicator placement="job"` で出し、同じ領域に取り消しを置く
 * （messaging.md §3.7）。終わった後は状態と処理時間・実行日時を残し、失敗・取り消しは理由を 1 か所に出す。
 */
export function EvaluationJobPanel({
  job,
  onCancel,
  cancelling,
  title = t("evaluation.job.title.run"),
  testPrefix = "evaluation-run-job",
  currentLabel,
}: {
  job: EvaluationJob;
  onCancel: (job: EvaluationJob) => void;
  cancelling: boolean;
  /** 見出し（判定の校正の job は「校正の実行状況」。#3578）。 */
  title?: string;
  testPrefix?: string;
  /** 実行中の対象の文言（省略時は評価のケースの行。null なら出さない）。 */
  currentLabel?: string | null;
}) {
  const active = isEvaluationJobActive(job);
  const currentCase = currentLabel === undefined ? evaluationJobCurrentCaseLabel(job) : currentLabel;
  return (
    <Card className="min-w-0" data-testid={testPrefix}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <ListChecks size={16} className="text-accent-fg" aria-hidden />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid min-w-0 gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <StatusBadge
              variant={EVALUATION_JOB_STATUS_VARIANT[job.status]}
              label={evaluationJobStatusLabel(job.status)}
            />
            <span
              className="tnum text-sm font-semibold text-fg"
              data-testid={`${testPrefix}-count`}
            >
              {evaluationJobCountLabel(job)}
            </span>
          </div>
          {active ? (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              tone="danger"
              icon={CircleStop}
              loading={cancelling}
              disabled={cancelling}
              onClick={() => onCancel(job)}
            >
              {t("evaluation.job.cancel")}
            </Button>
          ) : null}
        </div>
        {currentCase ? (
          <p
            className="min-w-0 break-words text-sm text-fg"
            data-testid={`${testPrefix}-current-case`}
          >
            {currentCase}
          </p>
        ) : null}
        <ProcessingIndicator
          active={active}
          operationKey={job.job_id}
          startedAt={job.started_at ?? job.created_at}
          finishedAt={job.finished_at}
          label={t("evaluation.actions.running")}
          finalLabel={evaluationJobStatusLabel(job.status)}
          showSlowMessage={active}
          placement="job"
          testId={`${testPrefix}-timing`}
          // 「中止」の要求中は、そのボタンの loading がスピナーを出す（同じ処理のスピナーは 1 つ。#416）。
          activityIcon={cancelling ? "none" : "spinner"}
        />
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
          <span>{evaluationJobTimeLimitLabel(job)}</span>
          {job.finished_at ? (
            <span>{t("evaluation.job.finishedAt", { time: formatDateTime(job.finished_at) })}</span>
          ) : null}
        </div>
        {active ? <p className="text-xs text-fg-muted">{t("evaluation.job.timeLimitHint")}</p> : null}
        {job.status === "FAILED" ? (
          <Banner severity="danger" title={t("evaluation.job.failedTitle")}>
            <EvaluationJobFailureMessage message={job.error_message} />
          </Banner>
        ) : null}
        {job.status === "CANCELLED" ? (
          <Banner severity="info" title={t("evaluation.job.cancelledTitle")} />
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * job の失敗の本文。原因と対処を本文に出し、例外の種別（技術的な詳細）は「詳細」に畳んで、
 * 失敗なので開いて出す（UX 契約 messaging.md §10.3）。
 */
function EvaluationJobFailureMessage({ message }: { message: string | null }) {
  const failure = splitEvaluationJobFailure(message);
  return (
    <div className="space-y-2">
      {failure.message ? <p className="break-words">{failure.message}</p> : null}
      {failure.errorType ? (
        <ApiErrorDetailList
          label={t("evaluation.job.failedDetails")}
          details={[{ label: t("evaluation.job.errorType"), value: failure.errorType }]}
        />
      ) : null}
    </div>
  );
}

/** 戻ってきたときなど、job の状態を読み込んでいる間。結果の形の Skeleton で寸法を予約する。 */
export function EvaluationJobLoading() {
  return (
    <TimedLoadingState
      label={t("evaluation.job.loading")}
      operationKey="evaluation-run-job-loading"
      placement="job"
      testId="evaluation-run-job-loading"
    >
      <Skeleton className="h-6 w-40" aria-hidden="true" />
      <Skeleton className="h-24 w-full" aria-hidden="true" />
    </TimedLoadingState>
  );
}

/** job の状態を取得できないとき。見つからない（404）場合は再実行を案内する。 */
export function EvaluationJobError({
  notFound,
  message,
  onRetry,
}: {
  notFound: boolean;
  message: string | null;
  onRetry: () => void;
}) {
  if (notFound) {
    return <Banner severity="warning">{t("evaluation.job.notFound")}</Banner>;
  }
  return <ErrorState message={message ?? t("evaluation.job.loadError")} onRetry={onRetry} />;
}

/** 実行中の結果の領域。結果の形の Skeleton で寸法を予約する（経過時間は実行状況に出す）。 */
export function EvaluationResultSkeleton() {
  return (
    <section
      className="min-w-0 space-y-4"
      aria-busy="true"
      aria-label={t("evaluation.actions.running")}
      data-testid="evaluation-result-loading"
    >
      <Skeleton className="h-6 w-40" aria-hidden="true" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Skeleton className="h-20 w-full" aria-hidden="true" />
        <Skeleton className="h-20 w-full" aria-hidden="true" />
        <Skeleton className="h-20 w-full" aria-hidden="true" />
        <Skeleton className="h-20 w-full" aria-hidden="true" />
      </div>
      <Skeleton className="h-48 w-full" aria-hidden="true" />
    </section>
  );
}
