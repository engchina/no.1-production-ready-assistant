import {
  ApiErrorBanner,
  apiErrorMessage,
  Button,
  Banner,
  DataTable,
  Disclosure,
  EmptyState,
  FormSkeleton,
  toast,
  StatusBadge,
  PageHeader,
  FieldError,
  FormGrid,
  FormStatus,
  PageBody,
  useConfirm,
  ProcessingIndicator,
  RowActionMenu,
  ListSkeleton,
  ListToolbar,
  OffsetPagination,
  TableSkeleton,
  TimedLoadingState,
  SelectField,
  TextField,
  formatDisplayDateTime,
} from "@production-ready/ui";
import { useEffect, useMemo, useState } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BarChart3,
  CheckCircle2,
  CircleStop,
  Download,
  FileSpreadsheet,
  Play,
  Trash2,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";


import { ErrorState, LoadingState } from "@/components/StateViews";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { ApiError, apiDelete, apiFetch, apiGet, apiPost, apiPostForm } from "@/lib/api";
import { downloadBlob, downloadFilename } from "@/lib/download";
import { t } from "@/lib/i18n";
import { listCountLabels, paginationLabels } from "@/lib/pagination-labels";
import { useWorkspacePaging } from "@/lib/use-workspace-paging";
import { useWorkspaceState } from "@/components/WorkspaceState";
import { XLSX_TEMPLATE_FILE_FORMATS } from "@/lib/tabular-file-formats";
import { engineLabel } from "../labels";
import { profileDisplayLabel, profileRecordDisplayLabel } from "../profileDisplay";
import { QuestionText } from "../components/QuestionText";
import {
  qualityEvaluationAttemptTimedOut,
  qualityEvaluationLastHeartbeatMs,
  qualityEvaluationLeaseExpired,
  qualityEvaluationPollingInterval,
  validateQualityEvaluationInput,
  type QualityEvaluationValidationCode,
} from "../qualityEvaluationLogic";
import type {
  ProfileSummaryPage,
  QualityEvaluationCapabilities,
  QualityEvaluationEngine,
  QualityEvaluationEngineSummary,
  QualityEvaluationJobPage,
  QualityEvaluationJobSummary,
  QualityEvaluationResult,
  QualityEvaluationResultPage,
  QualityEvaluationStatus,
  QualityEvaluationVerdict,
} from "../types";

const TERMINAL_STATUSES = new Set<QualityEvaluationStatus>([
  "completed",
  "completed_with_errors",
  "failed",
  "cancelled",
]);
const ACTIVE_STATUSES = new Set<QualityEvaluationStatus>(["pending", "running"]);
const sectionClass = "grid min-w-0 gap-5 rounded-lg border border-border bg-surface p-4 shadow-sm lg:p-5";

/** 評価する SQL の生成の方式（モデルの API の 1 つだけ）。 */
const ENGINES: readonly QualityEvaluationEngine[] = ["enterprise_ai_direct"];

type FormErrors = Partial<Record<"profile" | "file" | "engines" | "repeat", string>>;
/**
 * job の中止・削除の失敗（progress = 実行状況の「中止」の下、recent = 最近の job の一覧の上。#995）。
 * 文は共通の API の失敗の表示（messaging.md §10.3.1）で、backend の理由（他の利用者の job・状態の競合など）を出す。
 */
type JobActionError = { origin: "progress" | "recent"; error: unknown; fallback: string } | null;

function qualityEvaluationQueryPollingInterval(
  status: QualityEvaluationStatus | undefined,
  error: unknown
) {
  if (error) return false;
  return qualityEvaluationPollingInterval(status);
}

function retryQualityEvaluationJob(failureCount: number, error: unknown) {
  if (error instanceof ApiError && error.status === 404) return false;
  return failureCount < 3;
}

export function EvaluationPage() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const [searchParams, setSearchParams] = useSearchParams();
  const currentJobId = searchParams.get("job") ?? "";
  // ダウンロードの失敗は押したボタンの直下に出す（template = テンプレート、results = 結果。messaging.md §10.1、#724）。
  const [downloadError, setDownloadError] = useState<{ origin: "template" | "results"; message: string } | null>(null);
  const [profileId, setProfileId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const engines = ENGINES;
  const [repeatCount, setRepeatCount] = useState(1);
  const [formErrors, setFormErrors] = useState<FormErrors>({});
  const [startError, setStartError] = useState("");
  // 最近の job と結果明細のページ番号・表示件数（offset 型。作業状態に残す。#1266 / #1891）。
  const jobPages = useWorkspacePaging("recentJobsPaging");
  const resultPages = useWorkspacePaging("resultsPaging");
  // どのボタンが始めたダウンロードか。スピナーは押したボタンだけが出し、もう一方は無効にするだけ（#819）。
  const [downloading, setDownloading] = useState<"template" | "results" | null>(null);
  const [jobActionError, setJobActionError] = useState<JobActionError>(null);

  const capabilitiesQuery = useQuery({
    queryKey: ["quality-evaluations", "capabilities", profileId],
    queryFn: () => {
      const query = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : "";
      return apiGet<QualityEvaluationCapabilities>(
        `/api/nl2sql/quality-evaluations/capabilities${query}`
      );
    },
    // 業務プロファイルを切り替えて取り直す間も、前の結果で条件のフォームを出したままにする。以前は取り直しのたびに
    // フォームが読み込み中の表示に置き換わり、選択欄のフォーカスが外れていた（#995）。
    placeholderData: keepPreviousData,
  });
  const profilesQuery = useQuery({
    queryKey: ["nl2sql", "profiles", "quality-evaluation"],
    queryFn: () =>
      apiGet<ProfileSummaryPage>("/api/nl2sql/profiles/search?limit=100"),
  });
  const recentJobsQuery = useQuery({
    queryKey: ["quality-evaluations", "jobs", jobPages.offset, jobPages.pageSize],
    queryFn: () =>
      apiGet<QualityEvaluationJobPage>(
        `/api/nl2sql/quality-evaluations?limit=${jobPages.pageSize}&offset=${jobPages.offset}`
      ),
    // ページを移る間も前のページを出したままにする（ページ送りの位置が跳ねない）。
    placeholderData: keepPreviousData,
    refetchInterval: (query) =>
      query.state.error
        ? false
        : query.state.data?.items.some((job) => ACTIVE_STATUSES.has(job.status))
        ? qualityEvaluationPollingInterval("running")
        : false,
  });
  const currentJobQuery = useQuery({
    queryKey: ["quality-evaluations", "job", currentJobId],
    queryFn: () =>
      apiGet<QualityEvaluationJobSummary>(
        `/api/nl2sql/quality-evaluations/${encodeURIComponent(currentJobId)}`
      ),
    enabled: Boolean(currentJobId),
    retry: retryQualityEvaluationJob,
    refetchInterval: (query) =>
      qualityEvaluationQueryPollingInterval(query.state.data?.status, query.state.error),
  });
  const currentJob = currentJobQuery.data ?? null;
  const recentJobs = recentJobsQuery.data?.items ?? [];
  // 今のページが空でも、前のページがある（削除で減った）ならページ送りを出し、共通の部品が最後のページへ寄せる。
  const recentJobsTotal = recentJobsQuery.data?.total ?? 0;
  const showRecentJobsPagination = Boolean(
    recentJobsQuery.data && (recentJobs.length > 0 || jobPages.offset > 0)
  );
  const resultsQuery = useQuery({
    queryKey: ["quality-evaluations", "results", currentJobId, resultPages.offset, resultPages.pageSize],
    queryFn: () =>
      apiGet<QualityEvaluationResultPage>(
        `/api/nl2sql/quality-evaluations/${encodeURIComponent(
          currentJobId
        )}/results?limit=${resultPages.pageSize}&offset=${resultPages.offset}`
      ),
    enabled: Boolean(currentJob && TERMINAL_STATUSES.has(currentJob.status)),
    placeholderData: keepPreviousData,
  });

  // 未選択のまま profile の一覧が届いたレンダーで先頭を選ぶ（effect で setState しない）。
  const firstProfileId = profilesQuery.data?.items[0]?.id;
  if (!profileId && firstProfileId) setProfileId(firstProfileId);

  // job が変わったレンダーで結果のページ位置を先頭に戻す。ページを残した job（作業状態）と同じ job を開き直したとき
  // （再読込・画面の移動）は、残したページのまま。
  const [resultsJobId, setResultsJobId] = useWorkspaceState("resultsJobId", currentJobId);
  if (resultsJobId !== currentJobId) {
    setResultsJobId(currentJobId);
    resultPages.reset();
  }
  const jobChanged = useValuesChanged([currentJobId]);
  if (jobChanged) {
    if (jobActionError?.origin === "progress") setJobActionError(null);
  }

  const currentJobStatus = currentJob?.status;
  useEffect(() => {
    if (currentJobStatus && TERMINAL_STATUSES.has(currentJobStatus)) {
      void queryClient.invalidateQueries({ queryKey: ["quality-evaluations", "jobs"] });
    }
  }, [currentJobStatus, currentJobId, queryClient]);

  const startMutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error(t("qualityEvaluation.file.required"));
      const form = new FormData();
      form.append("profile_id", profileId);
      for (const engine of engines) form.append("engines", engine);
      form.append("repeat_count", String(repeatCount));
      form.append("file", file);
      return apiPostForm<QualityEvaluationJobSummary>(
        "/api/nl2sql/quality-evaluations",
        form
      );
    },
    onSuccess: (job) => {
      setStartError("");
      queryClient.setQueryData(["quality-evaluations", "job", job.job_id], job);
      void queryClient.invalidateQueries({ queryKey: ["quality-evaluations", "jobs"] });
      const next = new URLSearchParams(searchParams);
      next.set("job", job.job_id);
      setSearchParams(next, { replace: true });
      toast.success(t("qualityEvaluation.notice.started"));
    },
    onError: (cause) => {
      setStartError(qualityEvaluationStartErrorMessage(cause));
    },
  });

  const deleteJobMutation = useMutation({
    mutationFn: (job: QualityEvaluationJobSummary) =>
      apiDelete<QualityEvaluationJobSummary>(
        `/api/nl2sql/quality-evaluations/${encodeURIComponent(job.job_id)}`
      ),
    onSuccess: (deleted) => {
      void queryClient.invalidateQueries({ queryKey: ["quality-evaluations", "jobs"] });
      queryClient.removeQueries({ queryKey: ["quality-evaluations", "job", deleted.job_id] });
      queryClient.removeQueries({
        queryKey: ["quality-evaluations", "results", deleted.job_id],
      });
      if (deleted.job_id === currentJobId) {
        const next = new URLSearchParams(searchParams);
        next.delete("job");
        setSearchParams(next, { replace: true });
        resultPages.reset();
      }
      toast.success(t("qualityEvaluation.notice.deleted"));
    },
  });
  const cancelJobMutation = useMutation({
    mutationFn: (job: QualityEvaluationJobSummary) =>
      apiPost<QualityEvaluationJobSummary>(
        `/api/nl2sql/quality-evaluations/${encodeURIComponent(job.job_id)}/cancel`
      ),
    onSuccess: (cancelled) => {
      queryClient.setQueryData(["quality-evaluations", "job", cancelled.job_id], cancelled);
      void queryClient.invalidateQueries({ queryKey: ["quality-evaluations", "jobs"] });
      void queryClient.invalidateQueries({
        queryKey: ["quality-evaluations", "results", cancelled.job_id],
      });
      toast.success(t("qualityEvaluation.notice.cancelled"));
    },
  });

  const capabilities = capabilitiesQuery.data;
  const selectedCapabilities = useMemo(
    () =>
      capabilities?.engines.filter((item) => engines.includes(item.engine)) ?? [],
    [capabilities?.engines, engines]
  );
  const selectedUnavailable = selectedCapabilities.some((item) => !item.available);
  const running = Boolean(currentJob && ACTIVE_STATUSES.has(currentJob.status));
  const conditionsLocked = running || startMutation.isPending;
  // 業務プロファイルの切り替えの取り直しの間（isPlaceholderData）は、前の profile の実行可否で開始させない。
  const capabilitiesRefreshing = capabilitiesQuery.isPlaceholderData;
  const pageLoading = capabilitiesQuery.isPending || profilesQuery.isPending;
  const pageError = capabilitiesQuery.error || profilesQuery.error;

  const validate = () => {
    const codes = validateQualityEvaluationInput({
      profileId,
      file,
      engines: [...engines],
      repeatCount,
      maxFileBytes: capabilities?.limits.max_file_bytes ?? 10 * 1024 * 1024,
      capabilities: capabilities?.engines ?? [],
    });
    const next: FormErrors = Object.fromEntries(
      Object.entries(codes).map(([field, code]) => [
        field,
        validationMessage(code as QualityEvaluationValidationCode, capabilities),
      ])
    );
    setFormErrors(next);
    return Object.keys(next).length === 0;
  };


  const startEvaluation = () => {
    if (conditionsLocked) return;
    setDownloadError(null);
    setStartError("");
    if (validate()) startMutation.mutate();
  };


  const openJob = (jobId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("job", jobId);
    setSearchParams(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const deleteJob = async (job: QualityEvaluationJobSummary) => {
    if (!TERMINAL_STATUSES.has(job.status)) return;
    const confirmed = await confirm({
      title: t("qualityEvaluation.confirm.delete.title"),
      description: t("qualityEvaluation.confirm.delete.description", {
        job: profileRecordDisplayLabel(job),
        createdAt: formatDate(job.created_at),
      }),
      confirmLabel: t("qualityEvaluation.action.delete"),
      tone: "danger",
    });
    if (!confirmed) return;
    setJobActionError(null);
    deleteJobMutation.mutate(job, {
      // 失敗は Toast ではなく最近の job の一覧の上に、backend の理由とともに残す（messaging.md §10。#995）。
      onError: (cause) =>
        setJobActionError({ origin: "recent", error: cause, fallback: t("qualityEvaluation.error.delete") }),
    });
  };

  const cancelJob = async (job: QualityEvaluationJobSummary, origin: "progress" | "recent") => {
    if (!ACTIVE_STATUSES.has(job.status)) return;
    const confirmed = await confirm({
      title: t("qualityEvaluation.confirm.cancel.title"),
      description: t("qualityEvaluation.confirm.cancel.description"),
      confirmLabel: t("qualityEvaluation.confirm.cancel.confirm"),
      tone: "danger",
    });
    if (!confirmed) return;
    setJobActionError(null);
    cancelJobMutation.mutate(job, {
      onError: (cause) =>
        setJobActionError({ origin, error: cause, fallback: t("qualityEvaluation.error.cancel") }),
    });
  };

  const downloadFile = async (path: string, fallbackName: string, origin: "template" | "results") => {
    setDownloading(origin);
    setDownloadError(null);
    try {
      const response = await apiFetch(path);
      if (!response.ok) throw new Error(t("qualityEvaluation.error.download"));
      const blob = await response.blob();
      const filename = downloadFilename(response, fallbackName);
      downloadBlob(filename, blob);
      toast.success(t("qualityEvaluation.notice.downloaded"));
    } catch (cause) {
      setDownloadError({
        origin,
        message: cause instanceof Error ? cause.message : t("qualityEvaluation.error.download"),
      });
    } finally {
      setDownloading(null);
    }
  };

  return (
    <>
      <PageHeader wide title={t("nav.evaluation")} subtitle={t("qualityEvaluation.subtitle")} />
      <PageBody wide className="grid min-w-0 gap-4 lg:gap-6">
        {pageError ? (
          <ErrorState
            message={t("qualityEvaluation.error.load")}
            onRetry={() => {
              void capabilitiesQuery.refetch();
              void profilesQuery.refetch();
            }}
          />
        ) : null}

        <section className={sectionClass} aria-labelledby="quality-evaluation-conditions">
          <SectionHeader
            icon={FileSpreadsheet}
            id="quality-evaluation-conditions"
            title={t("qualityEvaluation.conditions.title")}
            description={t("qualityEvaluation.conditions.description")}
          />
          {pageLoading ? (
            // 読み込み中は条件のフォームの形の Skeleton で領域を予約する（messaging.md §3.6）。
            <TimedLoadingState
              label={t("common.loading")}
              operationKey="quality-evaluation-conditions"
              placement="panel"
              framed={false}
              testId="quality-evaluation-conditions-loading"
            >
              <FormSkeleton fields={4} title={false} />
            </TimedLoadingState>
          ) : pageError ? null : (
            <form
              className="grid min-w-0 gap-5"
              aria-label={t("qualityEvaluation.conditions.title")}
              noValidate
              onSubmit={(event) => {
                event.preventDefault();
                startEvaluation();
              }}
            >
              {capabilities && !capabilities.judge.available ? (
                <Banner
                  severity="warning"
                  title={t("qualityEvaluation.judge.unavailableTitle")}
                >
                  {capabilities?.judge.reason}
                </Banner>
              ) : null}

              {/* 条件の欄の並びは共通の FormGrid（最大 60rem・置き場所 42rem 以上で 2 列。#1891）。 */}
              <FormGrid className="items-start" data-testid="quality-evaluation-input-row">
                <div className="min-w-0" data-testid="quality-evaluation-profile-field">
                  <SelectField
                    id="quality-evaluation-profile"
                    label={t("qualityEvaluation.profile.label")}
                    required
                    value={profileId}
                    placeholder={t("qualityEvaluation.profile.placeholder")}
                    options={(profilesQuery.data?.items ?? [])
                      .filter((profile) => !profile.archived)
                      .map((profile) => ({ value: profile.id, label: profileDisplayLabel(profile) }))}
                    onValueChange={(nextProfileId) => {
                      setProfileId(nextProfileId);
                      setFormErrors((current) => ({ ...current, profile: undefined }));
                    }}
                    error={formErrors.profile}
                    disabled={conditionsLocked}
                    // 業務プロファイル（対象の選択）は lg（#1891 の幅の段）。
                    width="lg"
                  />
                </div>

                <div className="grid min-w-0 content-start gap-2">
                  <FileDropzone
                    label={t("qualityEvaluation.file.label")}
                    accept={XLSX_TEMPLATE_FILE_FORMATS.accept}
                    formatLabel={XLSX_TEMPLATE_FILE_FORMATS.formatLabel}
                    selectedText={
                      file ? t("qualityEvaluation.file.selected", { name: file.name }) : ""
                    }
                    hint={t("qualityEvaluation.file.hint")}
                    errorText={formErrors.file}
                    icon="spreadsheet"
                    required
                    disabled={conditionsLocked}
                    dataTestId="quality-evaluation-file"
                    onReject={(reason) =>
                      setFormErrors((current) => ({
                        ...current,
                        file:
                          reason === "multiple-files"
                            ? t("common.fileDropzone.error.multiple", {
                                formats: XLSX_TEMPLATE_FILE_FORMATS.formatLabel,
                              })
                            : t("qualityEvaluation.file.invalidExtension"),
                      }))
                    }
                    onFiles={([selectedFile]) => {
                      setFile(selectedFile);
                      setFormErrors((current) => ({ ...current, file: undefined }));
                    }}
                  />
                  <div>
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      loading={downloading === "template"}
                      disabled={downloading === "results"}
                      onClick={() =>
                        void downloadFile(
                          "/api/nl2sql/quality-evaluations/template.xlsx",
                          "nl2sql_quality_evaluation_template.xlsx",
                          "template"
                        )
                      } icon={Download}>
                      {t("qualityEvaluation.template.download")}
                    </Button>
                    {downloadError?.origin === "template" ? (
                      <FormStatus tone="danger" message={downloadError.message} className="mt-2" />
                    ) : null}
                  </div>
                </div>
              </FormGrid>

              {/* SQL の生成の方式（モデルの API）がこの環境で使えないときは、理由を開始の前に示す。 */}
              {selectedUnavailable ? (
                <div data-testid="quality-evaluation-engine-unavailable">
                  <Banner severity="warning" title={t("qualityEvaluation.engines.unavailable")}>
                    {selectedCapabilities
                      .filter((item) => !item.available)
                      .map((item) => item.reason)
                      .join(" ")}
                  </Banner>
                </div>
              ) : null}
              <FieldError id="quality-engines-error" message={formErrors.engines} />

              <div
                className="grid min-w-0 gap-3 border-t border-border pt-4 lg:grid-cols-[minmax(13rem,20rem)_minmax(0,1fr)] lg:items-start"
                data-testid="quality-evaluation-run-summary"
              >
                <div className="min-w-0" data-testid="quality-evaluation-repeat-field">
                  <TextField
                    id="quality-evaluation-repeat"
                    label={t("qualityEvaluation.repeat.label")}
                    type="number"
                    min={1}
                    max={10}
                    step={1}
                    inputMode="numeric"
                    // 回数（数値）は xs（#1891 の幅の段）。
                    width="xs"
                    value={repeatCount}
                    disabled={conditionsLocked}
                    helper={t("qualityEvaluation.repeat.hint")}
                    error={formErrors.repeat}
                    onChange={(event) => {
                      setRepeatCount(Number(event.currentTarget.value));
                      setFormErrors((current) => ({ ...current, repeat: undefined }));
                    }}
                  />
                </div>
                <div
                  className="grid min-w-0 content-start gap-1.5 text-sm"
                  data-testid="quality-evaluation-estimate-summary"
                >
                  <div
                    className="font-medium text-fg"
                    data-testid="quality-evaluation-estimate-label"
                  >
                    {t("qualityEvaluation.estimate.title")}
                  </div>
                  <p
                    className="flex min-h-11 min-w-0 items-center break-words leading-6 text-fg-muted"
                    data-testid="quality-evaluation-estimate-value"
                  >
                    {currentJob
                      ? t("qualityEvaluation.estimate.confirmed", {
                          generations: currentJob.total_attempts,
                          analyses: currentJob.total_attempts,
                        })
                      : t("qualityEvaluation.estimate.formula", {
                          engines: engines.length,
                          repeats: repeatCount || 0,
                        })}
                  </p>
                </div>
              </div>

              <div data-testid="quality-evaluation-judge-note">
                <Banner severity="info">{t("qualityEvaluation.judge.note")}</Banner>
              </div>
              <div
                className="grid min-w-0 justify-items-end gap-2 border-t border-border pt-4"
                data-testid="quality-evaluation-action-footer"
              >
                <Button
                  type="submit"
                  size="lg"
                  variant="primary"
                  loading={startMutation.isPending}
                  disabled={
                    running ||
                    startMutation.isPending ||
                    capabilitiesRefreshing ||
                    !capabilities?.judge.available ||
                    selectedUnavailable
                  }
                  icon={Play}>
                  {t("qualityEvaluation.action.start")}
                </Button>
                {startMutation.isPending ? (
                  // 評価ファイルの送信と検証の間。ジョブの作成後は進捗の表示（placement="job"）が引き継ぐ。
                  <ProcessingIndicator
                    active
                    label={t("qualityEvaluation.progress.starting")}
                    operationKey="quality-evaluation-start"
                    placement="action"
                    activityIcon="none"
                    className="w-full justify-self-stretch"
                    testId="quality-evaluation-start-processing"
                  />
                ) : null}
                <FormStatus
                  tone="danger"
                  message={startError}
                  className="w-full justify-self-stretch"
                />
              </div>
            </form>
          )}
        </section>

        <div className="grid min-w-0 gap-4 lg:grid-cols-2 lg:items-start lg:gap-6">
          <section className={sectionClass} aria-labelledby="quality-evaluation-progress">
            <SectionHeader
              icon={Play}
              id="quality-evaluation-progress"
              title={t("qualityEvaluation.progress.title")}
              description={t("qualityEvaluation.progress.description")}
            />
            <div>
              {!currentJobId ? (
                <EmptyState
                  title={t("qualityEvaluation.progress.emptyTitle")}
                  hint={t("qualityEvaluation.progress.emptyHint")}
                />
              ) : currentJobQuery.isLoading ? (
                <LoadingState label={t("common.loading")} placement="job">
                  {/* 文言と経過時間の行の下を、進行の表示の形の Skeleton で覆う（#2510）。 */}
                  <ListSkeleton rows={2} />
                </LoadingState>
              ) : currentJobQuery.isError || !currentJob ? (
                <ErrorState
                  message={t("qualityEvaluation.error.load")}
                  onRetry={() => void currentJobQuery.refetch()}
                />
              ) : (
                <JobProgress
                  job={currentJob}
                  onCancel={(job) => cancelJob(job, "progress")}
                  error={
                    jobActionError?.origin === "progress"
                      ? apiErrorMessage(jobActionError.error, jobActionError.fallback)
                      : ""
                  }
                  cancelling={
                    cancelJobMutation.isPending &&
                    cancelJobMutation.variables?.job_id === currentJob.job_id
                  }
                />
              )}
            </div>
          </section>

          <section className={sectionClass} aria-labelledby="quality-evaluation-summary">
            <SectionHeader
              icon={BarChart3}
              id="quality-evaluation-summary"
              title={t("qualityEvaluation.summary.title")}
              description={t("qualityEvaluation.summary.description")}
              action={
                currentJob && TERMINAL_STATUSES.has(currentJob.status) ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={downloading === "results"}
                    disabled={downloading === "template"}
                    onClick={() =>
                      void downloadFile(
                        `/api/nl2sql/quality-evaluations/${encodeURIComponent(
                          currentJob.job_id
                        )}/results.xlsx`,
                        "nl2sql_quality_evaluation.xlsx",
                        "results"
                      )
                    } icon={Download}>
                    {t("qualityEvaluation.action.download")}
                  </Button>
                ) : null
              }
            />
            {downloading === "results" ? (
              // 結果の Excel は件数に比例して生成に数秒以上かかる。スピナーはボタンの loading が担う。
              <ProcessingIndicator
                active
                label={t("qualityEvaluation.progress.downloading")}
                operationKey="quality-evaluation-download"
                placement="action"
                activityIcon="none"
                testId="quality-evaluation-download-processing"
              />
            ) : null}
            {downloadError?.origin === "results" ? (
              <Banner severity="danger">{downloadError.message}</Banner>
            ) : null}
            <div>
              {!currentJob || !TERMINAL_STATUSES.has(currentJob.status) ? (
                <EmptyState
                  title={t("qualityEvaluation.summary.waitingTitle")}
                  hint={t("qualityEvaluation.summary.waitingHint")}
                />
              ) : currentJob.engine_summaries.length === 0 ? (
                <EmptyState
                  title={t("qualityEvaluation.details.emptyTitle")}
                  hint={currentJob.error_message || t("qualityEvaluation.details.emptyHint")}
                />
              ) : (
                <div className="grid min-w-0 gap-3 xl:grid-cols-2">
                  {currentJob.engine_summaries.map((summary) => (
                    <EngineSummaryCard key={summary.engine} summary={summary} />
                  ))}
                </div>
              )}
            </div>
          </section>
        </div>

        <section className={sectionClass} aria-labelledby="quality-evaluation-details">
          <SectionHeader
            icon={CheckCircle2}
            id="quality-evaluation-details"
            title={t("qualityEvaluation.details.title")}
            description={t("qualityEvaluation.details.description")}
          />
          {/* グリッドの子の最小幅を 0 にし、結果表（min-w 74rem）が枠と Pagination を横へ押し出さないようにする。 */}
          <div className="min-w-0">
            {!currentJob || !TERMINAL_STATUSES.has(currentJob.status) ? (
              <EmptyState
                title={t("qualityEvaluation.details.emptyTitle")}
                hint={t("qualityEvaluation.details.emptyHint")}
              />
            ) : resultsQuery.isLoading ? (
              <TimedLoadingState
                label={t("qualityEvaluation.details.loading")}
                operationKey={`quality-evaluation-results-${currentJobId}`}
                placement="result"
                framed={false}
                testId="quality-evaluation-results-loading"
              >
                <TableSkeleton columns={6} className="hidden md:block" />
                <ListSkeleton rowClassName="h-[7rem]" className="md:hidden" />
              </TimedLoadingState>
            ) : resultsQuery.isError ? (
              <ErrorState
                message={t("qualityEvaluation.error.load")}
                onRetry={() => void resultsQuery.refetch()}
              />
            ) : !resultsQuery.data?.total ? (
              <EmptyState
                title={t("qualityEvaluation.details.emptyTitle")}
                hint={currentJob.error_message || t("qualityEvaluation.details.emptyHint")}
              />
            ) : (
              <div className="grid min-w-0 gap-3">
                {/* 件数は一覧の上の 1 か所（ListCount。#1891）。 */}
                <ListToolbar
                  count={{
                    total: resultsQuery.data.total,
                    labels: listCountLabels(),
                    testId: "quality-evaluation-results-count",
                  }}
                />
                <ResultTable results={resultsQuery.data.items} />
                <OffsetPagination
                  offset={resultPages.offset}
                  limit={resultPages.pageSize}
                  total={resultsQuery.data.total}
                  count={resultsQuery.data.items.length}
                  onPageChange={(page) => resultPages.setPage(page)}
                  onPageSizeChange={resultPages.setPageSize}
                  disabled={resultsQuery.isPlaceholderData}
                  labels={paginationLabels()}
                  ariaLabel={t("qualityEvaluation.pagination.resultsLabel")}
                  testId="quality-evaluation-results-pagination"
                />
              </div>
            )}
          </div>
        </section>

        <section className={sectionClass} aria-labelledby="quality-evaluation-recent">
          <SectionHeader
            icon={FileSpreadsheet}
            id="quality-evaluation-recent"
            title={t("qualityEvaluation.recent.title")}
            description={t("qualityEvaluation.recent.description")}
          />
          {jobActionError?.origin === "recent" ? (
            <ApiErrorBanner
              error={jobActionError.error}
              fallback={jobActionError.fallback}
              testId="quality-evaluation-recent-job-action-error"
            />
          ) : null}
          <div>
            {recentJobsQuery.isLoading ? (
              <TimedLoadingState
                label={t("qualityEvaluation.recent.loading")}
                operationKey="quality-evaluation-recent-jobs"
                placement="panel"
                framed={false}
                testId="quality-evaluation-recent-jobs-loading"
              >
                <ListSkeleton rows={5} rowClassName="h-[5.5rem]" className="max-h-[17.5rem] overflow-hidden" />
              </TimedLoadingState>
            ) : recentJobsQuery.isError ? (
              <ErrorState
                message={t("qualityEvaluation.error.load")}
                onRetry={() => void recentJobsQuery.refetch()}
              />
            ) : (
              <div className="grid min-w-0 gap-3">
                {recentJobsTotal > 0 ? (
                  <ListToolbar
                    count={{
                      total: recentJobsTotal,
                      labels: listCountLabels(),
                      testId: "quality-evaluation-recent-jobs-count",
                    }}
                  />
                ) : null}
                {recentJobs.length === 0 ? (
                  <EmptyState
                    title={t("qualityEvaluation.recent.emptyTitle")}
                    hint={t("qualityEvaluation.recent.emptyHint")}
                  />
                ) : (
                  <div
                    className="grid max-h-[17.5rem] min-w-0 gap-2 overflow-auto pr-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
                    role="region"
                    aria-label={t("qualityEvaluation.recent.scrollRegion")}
                    tabIndex={0}
                    data-testid="quality-evaluation-recent-jobs-scroll-region"
                  >
                    {recentJobs.map((job) => (
                      <article
                        key={job.job_id}
                        data-surface-tint={job.job_id === currentJobId ? "accent" : undefined}
                        className={`grid min-w-0 gap-3 rounded-lg border p-4 md:grid-cols-[1fr_auto] md:items-center ${
                          job.job_id === currentJobId
                            ? "border-accent-emphasis bg-accent-subtle"
                            : "border-border bg-surface-sunken"
                        }`}
                      >
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-semibold text-fg">
                              {profileRecordDisplayLabel(job)}
                            </span>
                            <StatusBadge
                              variant={statusVariant(job.status)}
                              label={statusLabel(job.status)}
                            />
                          </div>
                          <p className="mt-1 break-words text-xs text-fg-muted">
                            {formatDate(job.created_at)} ·{" "}
                            {t("qualityEvaluation.recent.meta", {
                              cases: job.case_count,
                              attempts: job.total_attempts,
                            })}
                          </p>
                        </div>
                        <div className="flex shrink-0 flex-wrap justify-end gap-2">
                          <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            onClick={() => openJob(job.job_id)}
                          >
                            {t("qualityEvaluation.action.view")}
                          </Button>
                          {/* 中止・削除は行内に並べず、行メニューにまとめる（platform/docs/ux-contracts/buttons.md §5.1）。 */}
                          <RowActionMenu
                            ariaLabel={t("qualityEvaluation.action.rowActions", { job: profileRecordDisplayLabel(job) })}
                            testId={`quality-evaluation-job-actions-${job.job_id}`}
                            loading={
                              (cancelJobMutation.isPending && cancelJobMutation.variables?.job_id === job.job_id) ||
                              (deleteJobMutation.isPending && deleteJobMutation.variables?.job_id === job.job_id)
                            }
                            actions={[
                              {
                                id: "cancel",
                                label: t("qualityEvaluation.action.cancel"),
                                ariaLabel: t("qualityEvaluation.action.cancelJob", { job: profileRecordDisplayLabel(job) }),
                                icon: CircleStop,
                                tone: "danger",
                                visible: ACTIVE_STATUSES.has(job.status),
                                disabled: cancelJobMutation.isPending,
                                onSelect: () => cancelJob(job, "recent"),
                              },
                              {
                                id: "delete",
                                label: t("qualityEvaluation.action.delete"),
                                ariaLabel: TERMINAL_STATUSES.has(job.status)
                                  ? t("qualityEvaluation.action.deleteJob", { job: profileRecordDisplayLabel(job) })
                                  : t("qualityEvaluation.action.deleteDisabled"),
                                icon: Trash2,
                                tone: "danger",
                                disabled: !TERMINAL_STATUSES.has(job.status) || deleteJobMutation.isPending,
                                onSelect: () => deleteJob(job),
                              },
                            ]}
                          />
                        </div>
                      </article>
                    ))}
                  </div>
                )}
                {showRecentJobsPagination && recentJobsQuery.data ? (
                  <OffsetPagination
                    offset={jobPages.offset}
                    limit={jobPages.pageSize}
                    total={recentJobsTotal}
                    count={recentJobs.length}
                    onPageChange={(page) => jobPages.setPage(page)}
                    onPageSizeChange={jobPages.setPageSize}
                    disabled={recentJobsQuery.isPlaceholderData}
                    labels={paginationLabels()}
                    ariaLabel={t("qualityEvaluation.pagination.jobsLabel")}
                    testId="quality-evaluation-recent-jobs-pagination"
                  />
                ) : null}
              </div>
            )}
          </div>
        </section>
      </PageBody>
    </>
  );
}

function SectionHeader({
  icon: Icon,
  id,
  title,
  description,
  action,
}: {
  icon: typeof FileSpreadsheet;
  id: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        {/* 見出しと説明の 2 行のまとまりの横の印の箱（文字の 1 行目にそろえる規則の例外。#2371）。 */}
        <span
          data-surface-tint="accent"
          data-icon-align-skip=""
          className="grid size-9 shrink-0 place-items-center rounded-lg bg-accent-subtle text-accent-fg"
        >
          <Icon className="size-5" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <h2 id={id} className="text-base font-semibold text-fg">
            {title}
          </h2>
          <p className="mt-1 text-sm leading-6 text-fg-muted">{description}</p>
        </div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

function JobProgress({
  job,
  onCancel,
  cancelling = false,
  error = "",
}: {
  job: QualityEvaluationJobSummary;
  onCancel?: (job: QualityEvaluationJobSummary) => void | Promise<void>;
  cancelling?: boolean;
  /** 「中止」の失敗。操作の行の直下に出す（messaging.md §10）。 */
  error?: string;
}) {
  const active = ACTIVE_STATUSES.has(job.status);
  const attemptTimeoutSeconds = Math.round(Math.max(1, job.attempt_timeout_seconds || 0));
  const lastHeartbeatMs = qualityEvaluationLastHeartbeatMs(job);
  const lastHeartbeatLabel =
    lastHeartbeatMs === null
      ? t("qualityEvaluation.progress.heartbeatMissing")
      : formatDate(job.heartbeat_at ?? "");
  const attemptTimedOut = qualityEvaluationAttemptTimedOut(job);
  const leaseExpired = qualityEvaluationLeaseExpired(job);
  const percentage = job.total_attempts
    ? Math.round((job.completed_attempts / job.total_attempts) * 100)
    : 0;
  return (
    <div className="grid min-w-0 gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge variant={statusVariant(job.status)} label={statusLabel(job.status)} />
          <span className="text-sm font-semibold tabular-nums text-fg">
            {t("qualityEvaluation.progress.count", {
              completed: job.completed_attempts,
              total: job.total_attempts,
            })}
          </span>
        </div>
        {active && onCancel ? (
          <Button
            type="button"
            size="sm"
            variant="secondary"
            tone="danger"
            loading={cancelling}
            disabled={cancelling}
            onClick={() => void onCancel(job)} icon={CircleStop}>
            {t("qualityEvaluation.action.cancel")}
          </Button>
        ) : null}
      </div>
      <FormStatus tone="danger" message={error} />
      <ProcessingIndicator
        active={active}
        operationKey={job.job_id}
        startedAt={job.started_at ?? job.created_at}
        finishedAt={job.finished_at}
        label={t("qualityEvaluation.progress.processing")}
        finalLabel={statusLabel(job.status)}
        showSlowMessage={active}
        placement="job"
        testId="quality-evaluation-timing"
        // 「中止」の要求中は、そのボタンの loading がスピナーを出す（同じ処理のスピナーは 1 つ。#416）。
        activityIcon={cancelling ? "none" : "spinner"}
      />
      <div
        className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted"
        data-testid="quality-evaluation-job-diagnostics"
      >
        <span>
          {t("qualityEvaluation.progress.lastHeartbeat")}{" "}
          <span className="font-medium text-fg">{lastHeartbeatLabel}</span>
        </span>
        {job.status === "running" ? (
          <span data-testid="quality-evaluation-running-attempts">
            {t("qualityEvaluation.progress.runningAttempts")}{" "}
            <span className="font-medium tabular-nums text-fg">
              {t("qualityEvaluation.progress.runningAttemptsValue", {
                running: job.running_attempts?.length ?? 0,
                limit: Math.max(1, job.concurrency ?? 1),
              })}
            </span>
          </span>
        ) : null}
        <span>
          {t("qualityEvaluation.progress.attemptTimeout")}{" "}
          <span className="font-medium text-fg">
            {t("qualityEvaluation.progress.timeoutSeconds", {
              seconds: attemptTimeoutSeconds,
            })}
          </span>
        </span>
      </div>
      {attemptTimedOut ? (
        <div role="status" aria-live="polite">
          <Banner
            severity="warning"
            title={t("qualityEvaluation.progress.attemptTimedOutTitle")}
          >
            {t("qualityEvaluation.progress.attemptTimedOutHint", {
              seconds: attemptTimeoutSeconds,
            })}
          </Banner>
        </div>
      ) : leaseExpired ? (
        <div role="status" aria-live="polite">
          <Banner severity="warning" title={t("qualityEvaluation.progress.leaseExpiredTitle")}>
            {t("qualityEvaluation.progress.leaseExpiredHint")}
          </Banner>
        </div>
      ) : null}
      <div
        data-surface-tint="neutral"
        className="h-2 overflow-hidden rounded-full bg-surface-hover"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={job.total_attempts}
        aria-valuenow={job.completed_attempts}
        aria-label={t("qualityEvaluation.progress.title")}
      >
        <div
          className="h-full rounded-full bg-accent-emphasis transition-[width] duration-300"
          style={{ width: `${percentage}%` }}
        />
      </div>
      <dl className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-5">
        <Metric
          label={t("qualityEvaluation.progress.currentCase")}
          value={job.current_case_id || "-"}
        />
        <Metric
          label={t("qualityEvaluation.progress.currentEngine")}
          value={job.current_engine ? engineLabel(job.current_engine) : "-"}
        />
        <Metric
          label={t("qualityEvaluation.progress.currentRepeat")}
          value={job.current_repetition ? `${job.current_repetition} / ${job.repeat_count}` : "-"}
        />
        <Metric
          label={t("qualityEvaluation.progress.success")}
          value={String(job.success_count)}
        />
        <Metric
          label={t("qualityEvaluation.progress.errors")}
          value={String(job.error_count)}
          danger={job.error_count > 0}
        />
      </dl>
      {job.error_message ? (
        <div role="status" aria-live="polite">
          <Banner
            severity={job.status === "cancelled" ? "info" : "danger"}
            title={statusLabel(job.status)}
          >
            {job.error_message}
          </Banner>
        </div>
      ) : null}
    </div>
  );
}

function Metric({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return (
    <div data-surface-tint="neutral" className="min-w-0 rounded-lg border border-border bg-surface-hover p-3">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className={`mt-1 break-words text-sm font-semibold ${danger ? "text-danger-fg" : "text-fg"}`}>
        {value}
      </dd>
    </div>
  );
}

function EngineSummaryCard({ summary }: { summary: QualityEvaluationEngineSummary }) {
  return (
    <article className="grid min-w-0 gap-4 rounded-lg border border-border bg-surface-sunken p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold text-fg">{engineLabel(summary.engine)}</h3>
        <StatusBadge
          variant={summary.error_count ? "warning" : "success"}
          label={t("qualityEvaluation.summary.errors", { count: summary.error_count })}
        />
      </div>
      <dl className="grid grid-cols-2 gap-2">
        <Metric
          label={t("qualityEvaluation.summary.successRate")}
          value={formatPercent(summary.generation_success_rate)}
        />
        <Metric
          label={t("qualityEvaluation.summary.consistency")}
          value={formatPercent(summary.normalized_sql_consistency)}
        />
      </dl>
      <div>
        <div className="text-xs font-medium text-fg-muted">
          {t("qualityEvaluation.summary.verdicts")}
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <VerdictBadge verdict="correct" count={summary.correct} />
          <VerdictBadge verdict="incorrect" count={summary.incorrect} />
          <VerdictBadge verdict="uncertain" count={summary.uncertain} />
          <VerdictBadge verdict="not_analyzed" count={summary.not_analyzed} />
        </div>
      </div>
    </article>
  );
}

function ResultTable({ results }: { results: QualityEvaluationResult[] }) {
  return (
    <>
      <DataTable
        columns={[
          {
            key: "case",
            header: t("qualityEvaluation.details.case"),
            className: "max-w-56 py-3 align-top",
            render: (result) => (
              <>
                <div className="font-semibold text-fg">{result.case_id}</div>
                <QuestionText value={result.question} variant="compact" maxLines={2} className="mt-1 text-fg-muted" />
              </>
            ),
          },
          {
            key: "engine",
            header: t("qualityEvaluation.details.engine"),
            className: "py-3 align-top",
            render: (result) => (
              <>
                <div className="font-medium text-fg">{engineLabel(result.engine)}</div>
                <div className="mt-1 text-fg-muted">#{result.repetition_no}</div>
              </>
            ),
          },
          {
            key: "expectedSql",
            header: t("qualityEvaluation.details.expectedSql"),
            className: "max-w-72 py-3 align-top",
            render: (result) => <SqlBlock sql={result.expected_sql} />,
          },
          {
            key: "generatedSql",
            header: t("qualityEvaluation.details.generatedSql"),
            className: "max-w-72 py-3 align-top",
            render: (result) => <SqlBlock sql={result.generated_sql} error={result.generation_error} />,
          },
          {
            key: "judgement",
            header: t("qualityEvaluation.details.judgement"),
            className: "max-w-64 py-3 align-top",
            render: (result) => <ResultJudgement result={result} showAnalysis={false} />,
          },
          {
            key: "elapsed",
            header: t("qualityEvaluation.details.elapsed"),
            className: "whitespace-nowrap py-3 align-top text-fg-muted",
            render: (result) => `${result.total_elapsed_ms} ms`,
          },
        ]}
        rows={results}
        getRowKey={(result) => result.result_id}
        renderRowDetail={(result) =>
          hasResultAnalysis(result) ? (
            <div className="pb-2">
              <ResultAnalysisDetails result={result} />
            </div>
          ) : null
        }
        tableClassName="w-full min-w-[74rem]"
        className="hidden max-h-[30.5rem] rounded-lg md:block"
        scrollAriaLabel={t("qualityEvaluation.details.scrollRegion")}
        scrollTestId="quality-evaluation-results-table"
        stickyHeader
      />
      <div
        className="grid max-h-[37.5rem] min-w-0 gap-3 overflow-auto pr-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring md:hidden"
        role="region"
        aria-label={t("qualityEvaluation.details.scrollRegion")}
        tabIndex={0}
        data-testid="quality-evaluation-results-cards-scroll-region"
      >
        {results.map((result) => (
          <article key={result.result_id} className="grid min-w-0 gap-3 rounded-lg border border-border p-4">
            <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="break-words font-semibold text-fg">{result.case_id}</h3>
                <QuestionText
                  value={result.question}
                  variant="compact"
                  maxLines={2}
                  className="mt-1 text-fg-muted"
                />
              </div>
              <VerdictBadge verdict={result.verdict} />
            </div>
            <div className="text-xs font-medium text-fg-muted">
              {engineLabel(result.engine)} / #{result.repetition_no} / {result.total_elapsed_ms} ms
            </div>
            <div>
              <div className="mb-1 text-xs font-medium text-fg-muted">
                {t("qualityEvaluation.details.expectedSql")}
              </div>
              <SqlBlock sql={result.expected_sql} />
            </div>
            <div>
              <div className="mb-1 text-xs font-medium text-fg-muted">
                {t("qualityEvaluation.details.generatedSql")}
              </div>
              <SqlBlock sql={result.generated_sql} error={result.generation_error} />
            </div>
            <ResultJudgement result={result} />
          </article>
        ))}
      </div>
    </>
  );
}

function ResultJudgement({
  result,
  showAnalysis = true,
}: {
  result: QualityEvaluationResult;
  showAnalysis?: boolean;
}) {
  const hasAnalysis = hasResultAnalysis(result);
  return (
    <div className="grid min-w-0 gap-2">
      <ResultJudgementSummary result={result} />
      {showAnalysis && hasAnalysis ? <ResultAnalysisDetails result={result} /> : null}
    </div>
  );
}

function ResultJudgementSummary({ result }: { result: QualityEvaluationResult }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <VerdictBadge verdict={result.verdict} />
        {result.judge ? (
          <span className="text-xs tabular-nums text-fg-muted">
            {Math.round(result.judge.confidence * 100)}%
          </span>
        ) : null}
        <StatusBadge
          variant={result.deterministic_analysis.is_safe ? "success" : "danger"}
          label={
            result.deterministic_analysis.is_safe
              ? t("qualityEvaluation.details.safe")
              : t("qualityEvaluation.details.unsafe")
          }
        />
      </div>
      {result.judge?.summary ? (
        <p className="break-words text-xs leading-5 text-fg">{result.judge.summary}</p>
      ) : null}
    </>
  );
}

function ResultAnalysisDetails({ result }: { result: QualityEvaluationResult }) {
  const errors = [result.generation_error, result.judge_error].filter(Boolean);
  return (
    <Disclosure
      variant="plain"
      size="sm"
      summary={t("qualityEvaluation.details.analysis")}
      summaryClassName="text-accent-fg"
      summaryProps={{ "data-testid": "quality-evaluation-analysis-toggle" }}
    >
      <div
        data-surface-tint="neutral"
        className="grid min-w-0 gap-2 rounded-md bg-surface-hover p-3 text-fg-muted"
        data-testid="quality-evaluation-analysis-detail"
      >
        <AnalysisList
          label={t("qualityEvaluation.details.differences")}
          items={result.judge?.differences ?? []}
        />
        <AnalysisList
          label={t("qualityEvaluation.details.risks")}
          items={[...(result.judge?.risks ?? []), ...result.deterministic_analysis.risk_findings]}
        />
        {result.judge?.correction_suggestion ? (
          <div>
            <div className="font-medium text-fg">
              {t("qualityEvaluation.details.suggestion")}
            </div>
            <p className="mt-1 break-words">{result.judge.correction_suggestion}</p>
          </div>
        ) : null}
        <AnalysisList label={t("qualityEvaluation.details.error")} items={errors} danger />
      </div>
    </Disclosure>
  );
}

function hasResultAnalysis(result: QualityEvaluationResult) {
  return Boolean(
    result.judge ||
      result.generation_error ||
      result.judge_error ||
      result.deterministic_analysis.risk_findings.length
  );
}

function AnalysisList({
  label,
  items,
  danger = false,
}: {
  label: string;
  items: string[];
  danger?: boolean;
}) {
  if (!items.length) return null;
  return (
    <div>
      <div className={`font-medium ${danger ? "text-danger-fg" : "text-fg"}`}>{label}</div>
      <ul className="mt-1 list-disc space-y-1 pl-4">
        {items.map((item, index) => (
          <li key={`${index}-${item}`} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SqlBlock({ sql, error }: { sql: string; error?: string }) {
  if (!sql) {
    return (
      <span className={`break-words text-xs ${error ? "text-danger-fg" : "text-fg-muted"}`}>
        {error || "-"}
      </span>
    );
  }
  return (
    <pre data-surface-tint="neutral" className="max-h-32 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md bg-surface-hover p-2 font-mono text-xs leading-5 text-fg">
      {sql}
    </pre>
  );
}

function VerdictBadge({
  verdict,
  count,
}: {
  verdict: QualityEvaluationVerdict;
  count?: number;
}) {
  return (
    <StatusBadge
      variant={verdictVariant(verdict)}
      label={`${verdictLabel(verdict)}${count === undefined ? "" : ` ${count}`}`}
    />
  );
}

function statusLabel(status: QualityEvaluationStatus) {
  return t(`qualityEvaluation.status.${status}`);
}

function statusVariant(status: QualityEvaluationStatus) {
  if (status === "completed") return "success" as const;
  if (status === "completed_with_errors") return "warning" as const;
  if (status === "failed") return "danger" as const;
  if (status === "running") return "info" as const;
  return "neutral" as const;
}

function verdictLabel(verdict: QualityEvaluationVerdict) {
  return t(`qualityEvaluation.verdict.${verdict}`);
}

function verdictVariant(verdict: QualityEvaluationVerdict) {
  if (verdict === "correct") return "success" as const;
  if (verdict === "incorrect") return "danger" as const;
  if (verdict === "uncertain") return "warning" as const;
  return "neutral" as const;
}

function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function formatDate(value: string) {
  return formatDisplayDateTime(value, { dateStyle: "medium", timeStyle: "short" }, value);
}

function validationMessage(
  code: QualityEvaluationValidationCode,
  capabilities?: QualityEvaluationCapabilities
) {
  if (code === "profile_required") return t("qualityEvaluation.profile.required");
  if (code === "file_required") return t("qualityEvaluation.file.required");
  if (code === "file_extension") return t("qualityEvaluation.file.invalidExtension");
  if (code === "file_size") {
    return t("qualityEvaluation.file.tooLarge", {
      size: Math.floor((capabilities?.limits.max_file_bytes ?? 10 * 1024 * 1024) / 1024 / 1024),
    });
  }
  if (code === "engine_required") return t("qualityEvaluation.engines.required");
  return t("qualityEvaluation.repeat.invalid");
}

function qualityEvaluationStartErrorMessage(cause: unknown) {
  if (cause instanceof ApiError) return cause.messages.join(" ");
  if (cause instanceof Error && cause.message) return cause.message;
  return t("qualityEvaluation.error.start");
}
