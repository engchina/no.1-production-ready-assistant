import { ArrowRight, Bot, FileKey, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import {
  Banner,
  Button,
  ButtonLink,
  StatusBadge,
  DISPLAY_DATE_TIME_SECONDS,
  formatDisplayDateTime,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";

import { engineLabel } from "../labels";
import {
  profileSaveProgressPresentation,
  type ProfileSaveProgressStatus,
  type ProfileSaveProgressStepId,
  type ProfileSaveProgressStepStatus,
} from "../profileSyncPresentation";
import { SELECT_AI_CREDENTIAL_MISSING_CODE } from "../selectAiCredentialReadiness";
import type { AssetRefreshData, ProfileSyncJobData } from "../types";
import { WorkflowProgressStrip, type WorkflowProgressTone } from "./WorkflowProgressStrip";

type StatusBadgeVariant = "neutral" | "info" | "success" | "danger" | "warning";

function statusVariant(status: ProfileSaveProgressStatus): StatusBadgeVariant {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "submission_failed") return "danger";
  if (status === "cancelled") return "warning";
  return "info";
}

function progressTone(status: ProfileSaveProgressStatus): WorkflowProgressTone {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "submission_failed") return "danger";
  if (status === "cancelled") return "neutral";
  return "active";
}

function stepStatusLabel(status: ProfileSaveProgressStepStatus) {
  return t(`nl2sql.progress.step.${status}`);
}

function progressMessage(
  job: ProfileSyncJobData | null,
  status: ProfileSaveProgressStatus,
  failedStep: ProfileSaveProgressStepId | undefined,
) {
  if (status === "submission_failed") return t("profiles.oracle.progress.message.submissionFailed");
  if (status === "succeeded") return t("profiles.oracle.progress.message.succeeded");
  // DBMS_CLOUD_AI Profile の反映は済み、後の工程だけが失敗した「一部の成功」は、済んだことを先に書く。
  if (status === "failed" && failedStep === "rebuild_agent_assets") {
    return t("profiles.oracle.progress.message.agentFailed");
  }
  if (status === "failed" && failedStep === "verify") {
    return t("profiles.oracle.progress.message.verifyFailed");
  }
  if (status === "failed") return t("profiles.oracle.progress.message.failed");
  if (status === "cancelled") return t("profiles.oracle.progress.message.cancelled");
  if (status === "queued") return t("profiles.oracle.progress.message.queued");
  return t(`profiles.oracle.sync.phase.${job?.phase ?? "syncing_oracle_profile"}`);
}

function failureMessage(
  job: ProfileSyncJobData | null,
  submissionError: string,
  failedStep: ProfileSaveProgressStepId | undefined,
  credentialMissing: boolean,
) {
  if (credentialMissing) {
    // 開始の前に 409 で止まった（行は保存済み）か、反映の手順で Credential が無かった（#1683）。
    return submissionError && !job
      ? t("profiles.oracle.sync.credentialMissingBeforeStart")
      : t("profiles.oracle.sync.credentialMissing");
  }
  const detail = submissionError || job?.error_message_ja || "";
  const summary = submissionError
    ? t("profiles.oracle.sync.savedButFailed")
    : failedStep === "rebuild_agent_assets"
      ? t("profiles.oracle.sync.agentFailed")
      : failedStep === "verify"
        ? t("profiles.oracle.sync.verifyFailed")
        : t("profiles.oracle.sync.failed");
  return detail ? `${summary} ${detail}` : summary;
}

function AgentAssetDetails({ result }: { result: AssetRefreshData }) {
  return (
    <section
      className="mt-3 grid min-w-0 gap-3 border-l border-border pl-3"
      aria-label={t("profiles.oracle.assets.lastRefresh")}
      data-testid={`profile-asset-status-${result.engine}`}
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          {/* 見出しの 1 行目の中心にそろえる（#2371）。 */}
          <span className="pr-icon-line text-accent-fg">
            <Bot size={16} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <p className="break-words font-semibold text-fg">{engineLabel(result.engine)}</p>
            <p className="mt-1 break-words text-xs text-fg-muted">
              {result.refreshed_at ? formatDisplayDateTime(result.refreshed_at, DISPLAY_DATE_TIME_SECONDS, "-") : "-"}
            </p>
          </div>
        </div>
        <StatusBadge variant={result.refreshed ? "success" : "warning"} label={result.status} />
      </div>
      <dl className="grid min-w-0 gap-2 sm:grid-cols-2">
        {Object.entries(result.asset_names).map(([name, value]) => (
          <div key={name} className="grid min-w-0 gap-1 rounded-md bg-surface p-2 text-sm">
            <dt className="break-words text-xs font-medium uppercase text-fg-muted">{name}</dt>
            <dd className="min-w-0 break-all font-mono text-xs leading-5 text-fg">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      {result.warning ? <Banner severity="warning">{result.warning}</Banner> : null}
    </section>
  );
}

export function ProfileSaveProgress({
  job,
  submissionError,
  submissionErrorCode = "",
  rebuildAgentAssets,
  retrying,
  onRetry,
}: {
  job: ProfileSyncJobData | null;
  submissionError: string;
  /** 反映の開始の失敗の code（409 `SELECT_AI_CREDENTIAL_MISSING` など）。 */
  submissionErrorCode?: string;
  rebuildAgentAssets: boolean;
  retrying: boolean;
  onRetry: () => void;
}) {
  const presentation = profileSaveProgressPresentation(job, {
    rebuildAgentAssets,
    submissionError,
  });
  if (!presentation) return null;

  const failed = presentation.status === "failed" || presentation.status === "submission_failed";
  const failedStep = presentation.steps.find((step) => step.status === "error")?.id;
  const credentialMissing =
    job?.error_code === SELECT_AI_CREDENTIAL_MISSING_CODE ||
    (Boolean(submissionError) && submissionErrorCode === SELECT_AI_CREDENTIAL_MISSING_CODE);
  const shortJobId = job
    ? `${job.job_id.slice(0, 12)}${job.job_id.length > 12 ? "…" : ""}`
    : "";

  return (
    <WorkflowProgressStrip
      active={presentation.active}
      operationKey={job?.job_id ?? (submissionError ? "profile-sync-submission-failed" : null)}
      startedAt={job?.started_at || job?.created_at}
      finishedAt={job?.finished_at}
      title={t("profiles.oracle.progress.title")}
      titleId="profile-save-progress-title"
      message={progressMessage(job, presentation.status, failedStep)}
      statusLabel={t(`profiles.oracle.progress.status.${presentation.status}`)}
      statusVariant={statusVariant(presentation.status)}
      tone={progressTone(presentation.status)}
      stepsAriaLabel={t("profiles.oracle.progress.stepsLabel")}
      testId="profile-save-progress"
      dataJobStatus={presentation.status}
      role={presentation.active ? "status" : undefined}
      meta={
        shortJobId ? (
          <span className="min-w-0 max-w-full break-all font-mono">
            {t("profiles.oracle.progress.jobId", { id: shortJobId })}
          </span>
        ) : undefined
      }
      steps={presentation.steps.map((step) => ({
        id: step.id,
        label: t(`profiles.oracle.progress.${step.id}.label`),
        description: t(`profiles.oracle.progress.${step.id}.description`),
        status: step.status,
        statusLabel: stepStatusLabel(step.status),
        open:
          step.status === "running" ||
          step.status === "error" ||
          (step.id === "rebuild_agent_assets" && Boolean(job?.agent_result)),
        testId: `profile-save-step-${step.id}`,
        dataStatus: step.status,
        content:
          step.id === "rebuild_agent_assets" && job?.agent_result ? (
            <AgentAssetDetails result={job.agent_result} />
          ) : undefined,
      }))}
      footer={
        failed ? (
          <Banner
            severity="danger"
            className="mx-4 mb-4"
            action={
              <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
                {credentialMissing ? (
                  <ButtonLink
                    // 前提の案内（PreconditionBanner）と同じ導線: 運用設定の「Select AI Credential」（#1688）。
                    to={APP_ROUTES.settingsSelectAiCredential}
                    linkComponent={Link}
                    variant="secondary"
                    size="sm"
                    icon={FileKey}
                    trailingIcon={ArrowRight}
                    className="w-full sm:w-auto"
                    testId="profile-save-progress-open-credential"
                  >
                    {t("selectAiCredential.precondition.open")}
                  </ButtonLink>
                ) : null}
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="w-full sm:w-auto"
                  loading={retrying}
                  onClick={onRetry} icon={RefreshCw}>
                  <span>{t("profiles.oracle.sync.retry")}</span>
                </Button>
              </div>
            }
          >
            {failureMessage(job, submissionError, failedStep, credentialMissing)}
          </Banner>
        ) : undefined
      }
    />
  );
}
