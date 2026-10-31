import {
  PageBody,
  PageHeader,
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  ProcessingIndicator,
  Skeleton,
  StatusBadge,
  Switch,
  TabPanel,
  Tabs,
  TimedLoadingState,
  useConfirm,
  ListToolbar,
  PagedDataTable,
  PAGE_SIZE_OPTIONS,
} from "@production-ready/ui";
import {
  BarChart3,
  CheckCircle2,
  ClipboardCheck,
  Download,
  Minus,
  Settings2,
  TrendingDown,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { type FormEvent, type ReactNode, createContext, useContext, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { EmptyState } from "@/components/StateViews";
import {
  SearchAnswerProfileSelect,
  SearchAnswerProfileSelectSkeleton,
} from "@/components/search-answer-profiles/SearchAnswerProfileSelect";
import {
  api,
  ApiError,
  type EvaluationJob,
  type EvaluationJobStatus,
  type EvaluationCaseResult,
  type EvaluationCategorySummary,
  type EvaluationImportResult,
  type EvaluationMetricName,
  type EvaluationMetrics,
  type EvaluationProcessDraft,
} from "@/lib/api";
import { saveDownloadedFile } from "@/lib/download";
import { t } from "@/lib/i18n";
import { isNullableString, useWorkspaceState } from "@/lib/workspace-state";
import {
  useCancelEvaluationJob,
  useEvaluationJob,
  useEvaluationSettings,
  useImportEvaluationCases,
  useSearchAnswerProfiles,
  useSubmitRunEvaluationJob,
} from "@/lib/queries";
import { APP_ROUTES } from "@/lib/routes";
import { toast } from "@/lib/toast";

import {
  evaluationCaseErrorSummary,
  isEvaluationJobActive,
  isEvaluationJobId,
} from "./evaluation-job";
import {
  applyProcessDrafts,
  buildEvaluationCaseLabels,
  evaluationProcessRows,
  evaluationCaseRowLabel,
  evaluationEvidenceSummary,
  type EvaluationCaseLabel,
  type EvaluationCaseLabels,
} from "./evaluation-input";
import {
  comparisonRows,
  EVALUATION_PERSPECTIVES,
  formatMetricDelta,
  isPerspectiveShown,
  LOWER_IS_BETTER_METRICS,
  type ComparisonDirection,
  type EvaluationComparisonRow,
  STANDARD_ANSWER_METRICS,
  failureReasonLabel,
  formatMetricValue,
  metricCaseCount,
  metricDescription,
  metricLabel,
  metricValue,
  orderedThresholdEntries,
  perspectiveDescription,
  perspectiveLabel,
  suiteLabel,
} from "./evaluation-metrics";
import {
  EvaluationJobError,
  EvaluationJobLoading,
  EvaluationJobPanel,
  EvaluationResultSkeleton,
} from "./EvaluationJobPanel";
import { JudgeCalibrationBadge } from "./calibration/JudgeCalibrationBadge";
import { JudgeCalibrationTab } from "./calibration/JudgeCalibrationTab";
import { EvaluationProcessDrafts } from "./EvaluationProcessDrafts";
import {
  EVALUATION_UPLOAD_ID,
  EvaluationRequestUpload,
  evaluationUploadSelectionError,
} from "./EvaluationRequestUpload";

const ResultExportContext = createContext<{
  jobId: string;
  input: EvaluationImportResult | null;
  profileName: string | null;
  compareProfileName: string | null;
} | null>(null);

const COMPARE_SWITCH_ID = "evaluation-compare-switch";
const COMPARE_PROFILE_ID = "evaluation-compare-profile";
/** アップロードの上限（backend の `EVALUATION_EXCEL_MAX_ROWS` / `_BYTES`。検証の前の案内に出す）。 */
const EVALUATION_EXCEL_MAX_ROWS = 200;
const EVALUATION_EXCEL_MAX_MEGABYTES = 10;
const PAGE_TABS_PREFIX = "evaluation-page";
type PageTab = "run" | "calibration";

/**
 * 品質評価（改善・運用 › 品質評価。#2065）。評価する検索・回答プロファイル（必須）と評価リクエストの Excel
 * （テンプレートのダウンロード・アップロード・検証）を条件に評価を実行し、評価の基準は設定の既定を
 * 表示だけする。評価は job で動き（#390）、結果は Excel の行（質問・期待する根拠）に合わせて出す。
 */
export function EvaluationClient() {
  // 評価の実行と判定の校正（#3578）のタブ。`?tab=calibration` で判定の校正を直接開ける。
  const [searchParams, setSearchParams] = useSearchParams();
  const pageTab: PageTab = searchParams.get("tab") === "calibration" ? "calibration" : "run";
  const changePageTab = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === "calibration") next.set("tab", "calibration");
    else next.delete("tab");
    setSearchParams(next, { replace: true });
  };
  const runMutation = useSubmitRunEvaluationJob();
  const importMutation = useImportEvaluationCases();
  const settingsQuery = useEvaluationSettings();
  // 評価は job で動く（#390）。job id だけを作業状態に残し、画面に戻ったらサーバーの状態
  // （進捗・結果）を確かめる（workspace-state.md）。戻っただけで評価を送り直さない。
  const runJob = useEvaluationJobState();
  // 評価の条件（プロファイル・検証した Excel）は、ページを行き来しても再読込しても残す
  // （workspace-state.md）。選んだファイルそのものはメモリだけに持つ（プロファイルを変えたら検証し直す）。
  const [profileId, setProfileId] = useWorkspaceState<string | null>(
    "evaluation.profileId",
    null,
    isNullableString
  );
  const [importResult, setImportResult] = useWorkspaceState<EvaluationImportResult | null>(
    "evaluation.import",
    null,
    isImportResult
  );
  const [runInputSnapshot, setRunInputSnapshot] = useWorkspaceState<EvaluationImportResult | null>(
    "evaluation.runInput", null, isImportResult
  );
  // 実行した評価のケースの見出し（結果の表の質問・期待する根拠）。Excel を差し替えても結果と合う。
  const [runCaseLabels, setRunCaseLabels] = useWorkspaceState<EvaluationCaseLabels>(
    "evaluation.runCaseLabels",
    {},
    isCaseLabels
  );
  // 検索・回答プロファイルでも評価して比較するか（#2402）。条件と同じく作業状態に残す。
  const [compareEnabled, setCompareEnabled] = useWorkspaceState<boolean>(
    "evaluation.compareEnabled",
    false,
    isBoolean
  );
  const [compareProfileId, setCompareProfileId] = useWorkspaceState<string | null>(
    "evaluation.compareProfileId",
    null,
    isNullableString
  );
  // 実行した評価の比べたプロファイルの名前（結果の見出し。条件を変えても結果と合う）。
  const [runProfileName, setRunProfileName] = useWorkspaceState<string | null>(
    "evaluation.runProfileName",
    null,
    isNullableString
  );
  const [runCompareName, setRunCompareName] = useWorkspaceState<string | null>(
    "evaluation.runCompareName",
    null,
    isNullableString
  );
  const [compareError, setCompareError] = useState<string | null>(null);
  // 回答の過程から作った期待の下書きと、確かめて評価に使うか（#2402）。検証した Excel ごとに作り直す。
  const [processDrafts, setProcessDrafts] = useWorkspaceState<
    Record<string, EvaluationProcessDraft>
  >("evaluation.processDrafts", {}, isProcessDrafts);
  const [useProcessDrafts, setUseProcessDrafts] = useWorkspaceState<boolean>(
    "evaluation.useProcessDrafts",
    false,
    isBoolean
  );
  const profilesQuery = useSearchAnswerProfiles({ status: "ACTIVE", limit: 50, offset: 0 });
  const profiles = profilesQuery.data?.items ?? [];
  // 選んだファイル（プロファイルを変えたときに検証し直す）。再読込の後は無い（検証の結果だけが残る）。
  const [file, setFile] = useState<File | null>(null);
  const importSequence = useRef(0);
  const [fileName, setFileName] = useState<string | null>(importResult?.file_name ?? null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [runError, setRunError] = useState("");
  const [templateDownloading, setTemplateDownloading] = useState(false);
  const [templateError, setTemplateError] = useState<string | null>(null);

  const runActive = runMutation.isPending || isEvaluationJobActive(runJob.job);
  const validating = importMutation.isPending;
  // プロファイルを変えた後の検証の結果は、期待する根拠のファイル名の照合が変わるため使えない。
  const importStale =
    importResult !== null && importResult.search_answer_profile_id !== profileId;
  const shownResult = importStale ? null : importResult;
  const processRows = shownResult ? evaluationProcessRows(shownResult) : [];
  const uploadNotice = validating
    ? null
    : fileName && !profileId
      ? t("evaluation.import.needsKnowledgeBase")
      : importStale && !file
        ? t("evaluation.import.staleKnowledgeBase")
        : null;

  const validate = (target: File, id: string) => {
    importSequence.current += 1;
    const sequence = importSequence.current;
    setUploadError(null);
    setRunError("");
    importMutation.mutate(
      { file: target, searchAnswerProfileId: id },
      {
        // 検証中に別のファイル・プロファイルに変えたときは、前の結果を捨てる。
        onSuccess: (result) => {
          if (sequence !== importSequence.current) return;
          setImportResult(result);
          setProcessDrafts({});
          setUseProcessDrafts(false);
        },
        onError: (error) => {
          if (sequence !== importSequence.current) return;
          setImportResult(null);
          setUploadError(error instanceof ApiError ? error.message : t("evaluation.import.error"));
        },
      }
    );
  };

  const selectFiles = (files: File[]) => {
    const selectionError = evaluationUploadSelectionError(files);
    if (selectionError) {
      setUploadError(selectionError);
      return;
    }
    const [selected] = files;
    if (!selected) return;
    setFile(selected);
    setFileName(selected.name);
    setImportResult(null);
    setProcessDrafts({});
    setUseProcessDrafts(false);
    if (profileId) validate(selected, profileId);
    else setUploadError(null);
  };

  const changeProfile = (id: string) => {
    importSequence.current += 1;
    setProfileId(id);
    setProfileError(null);
    setProcessDrafts({});
    setUseProcessDrafts(false);
    if (compareProfileId === id) setCompareProfileId(null);
    if (file) validate(file, id);
  };

  const downloadTemplate = async () => {
    setTemplateError(null);
    setTemplateDownloading(true);
    try {
      saveDownloadedFile(await api.downloadEvaluationTemplate(profileId));
      toast.success(t("evaluation.import.templateDone"));
    } catch (error) {
      setTemplateError(
        error instanceof ApiError ? error.message : t("evaluation.import.templateError")
      );
    } finally {
      setTemplateDownloading(false);
    }
  };

  const runEvaluation = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (runActive || validating) return;
    // 未選択・未検証・誤りは、押したときに欄の直下へ出し、画面の並び順で最初の欄へ移す（#541）。
    const nextProfileError = profileId
      ? null
      : t("evaluation.knowledgeBaseScope.required");
    const nextUploadError = !fileName
      ? t("evaluation.import.required")
      : !shownResult
        ? importStale && !file
          ? t("evaluation.import.staleKnowledgeBase")
          : profileId
            ? (uploadError ?? t("evaluation.import.required"))
            : null
        : shownResult.error_count > 0
          ? t("evaluation.import.fixErrors")
          : null;
    const nextCompareError =
      compareEnabled && (!compareProfileId || compareProfileId === profileId)
        ? t("evaluation.compare.profileRequired")
        : null;
    setProfileError(nextProfileError);
    setUploadError(nextUploadError);
    setCompareError(nextCompareError);
    if (nextProfileError) {
      document.getElementById("evaluation-profile")?.focus();
      return;
    }
    if (nextUploadError || !shownResult) {
      document.getElementById(EVALUATION_UPLOAD_ID)?.focus();
      return;
    }
    if (nextCompareError) {
      document.getElementById(COMPARE_PROFILE_ID)?.focus();
      return;
    }
    if (!profileId) return;
    const compareId = compareEnabled ? compareProfileId : null;
    // 確かめた期待の下書きを足したケース（#2402。オンにしたときだけ）。
    const runInput = useProcessDrafts ? applyProcessDrafts(shownResult, processDrafts) : shownResult;
    setRunError("");
    runMutation.reset();
    try {
      const job = await runMutation.mutateAsync({
        cases: runInput.cases,
        search_answer_profile_id: profileId,
        ...(compareId ? { compare_search_answer_profile_id: compareId } : {}),
      });
      setRunCaseLabels(buildEvaluationCaseLabels(runInput));
      setRunInputSnapshot(runInput);
      setRunProfileName(profiles.find((profile) => profile.id === profileId)?.name ?? profileId);
      setRunCompareName(
        compareId ? (profiles.find((profile) => profile.id === compareId)?.name ?? compareId) : null
      );
      runJob.start(job);
      toast.info(t("evaluation.toast.started"));
    } catch (error) {
      setRunError(error instanceof ApiError ? error.message : t("evaluation.error.run"));
    }
  };

  return (
    <div>
      <PageHeader
        wide
        title={t("nav.evaluation")}
        subtitle={t("evaluation.subtitle")}
        tabs={
          <Tabs
            idPrefix={PAGE_TABS_PREFIX}
            ariaLabel={t("evaluation.tabs.label")}
            value={pageTab}
            onChange={changePageTab}
            items={[
              { id: "run", label: t("evaluation.tabs.run") },
              { id: "calibration", label: t("evaluation.tabs.calibration") },
            ]}
          />
        }
      />
      <PageBody wide>
        <TabPanel id="calibration" value={pageTab} idPrefix={PAGE_TABS_PREFIX}>
          <JudgeCalibrationTab />
        </TabPanel>
        <TabPanel id="run" value={pageTab} idPrefix={PAGE_TABS_PREFIX} className="grid min-w-0 gap-6">
        {/* 評価の基準を先に読み、条件を選んで実行する順に、縦に全幅で並べる（#3295。間隔は PageBody の gap）。 */}
        <SuiteSummary query={settingsQuery} />
        <Card className="min-w-0" data-testid="evaluation-conditions">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ClipboardCheck size={16} className="text-accent-fg" aria-hidden />
              {t("evaluation.conditions.title")}
            </CardTitle>
            <CardDescription>{t("evaluation.conditions.description")}</CardDescription>
          </CardHeader>
          <CardContent>
            <form
              className="grid min-w-0 gap-5"
              aria-label={t("evaluation.conditions.title")}
              noValidate
              onSubmit={(event) => void runEvaluation(event)}
            >
              <div className="min-w-0" data-testid="evaluation-profile">
                {profilesQuery.isPending ? (
                  <TimedLoadingState
                    label={t("searchAnswerProfiles.loading")}
                    operationKey="evaluation-profiles-load"
                    framed={false}
                  >
                    <SearchAnswerProfileSelectSkeleton />
                  </TimedLoadingState>
                ) : profilesQuery.isError ? (
                  <Banner severity="warning">{t("evaluation.compare.profilesLoadError")}</Banner>
                ) : (
                  <SearchAnswerProfileSelect
                    id="evaluation-profile"
                    items={profiles}
                    value={profileId}
                    onChange={changeProfile}
                    disabled={runActive}
                    error={profileError ?? undefined}
                  />
                )}
                <p className="mt-1 text-xs text-fg-muted">{t("evaluation.knowledgeBaseScope.helper")}</p>
              </div>

              <EvaluationRequestUpload
                fileName={fileName}
                result={shownResult}
                validating={validating}
                disabled={runMutation.isPending}
                error={uploadError}
                notice={uploadNotice}
                maxRows={EVALUATION_EXCEL_MAX_ROWS}
                maxMegabytes={EVALUATION_EXCEL_MAX_MEGABYTES}
                templateDownloading={templateDownloading}
                templateError={templateError}
                onFiles={selectFiles}
                onDownloadTemplate={() => void downloadTemplate()}
              />

              {shownResult && shownResult.error_count === 0 && processRows.length ? (
                <EvaluationProcessDrafts
                  rows={processRows}
                  knowledgeBaseIds={shownResult.knowledge_base_ids}
                  drafts={processDrafts}
                  onDrafts={setProcessDrafts}
                  confirmed={useProcessDrafts}
                  onConfirmedChange={setUseProcessDrafts}
                  disabled={runMutation.isPending}
                />
              ) : null}
              <CompareOption
                enabled={compareEnabled}
                onEnabledChange={(enabled) => {
                  setCompareEnabled(enabled);
                  setCompareError(null);
                }}
                profileId={compareProfileId}
                onProfileChange={(id) => {
                  setCompareProfileId(id);
                  setCompareError(null);
                }}
                query={profilesQuery}
                baseProfileId={profileId}
                error={compareError}
                disabled={runMutation.isPending}
              />
              <div className="grid min-w-0 gap-3">
                <div>
                  <Button
                    type="submit"
                    loading={runMutation.isPending}
                    disabled={runActive || validating}
                    icon={BarChart3}
                  >
                    {t("evaluation.actions.run")}
                  </Button>
                </div>
                {/* 投入の失敗は操作の行の直下に出す（messaging.md §10）。 */}
                {runError ? <Banner severity="danger">{runError}</Banner> : null}
                {runMutation.isPending ? (
                  // job の投入（検証と作成）の間。ボタンの loading がスピナーを担うので、ここは文言と
                  // 経過時間だけを出す（messaging.md §3.7）。作成後は実行状況（placement="job"）が引き継ぐ。
                  <ProcessingIndicator
                    active
                    label={t("evaluation.actions.starting")}
                    operationKey="evaluation-run"
                    placement="action"
                    activityIcon="none"
                    testId="evaluation-run-processing"
                  />
                ) : null}
              </div>
            </form>
          </CardContent>
        </Card>

        {runJob.jobId ? (
          <EvaluationJobSection
            state={runJob}
            renderResult={(job) =>
              job.run_result ? (
                <ResultExportContext.Provider value={{jobId:job.job_id,input:runInputSnapshot,
                  profileName:runProfileName,compareProfileName:runCompareName}}>
                {job.run_result.comparison ? (
                  <EvaluationComparisonResult
                    metrics={job.run_result}
                    baseName={runProfileName}
                    comparedName={runCompareName}
                    caseLabels={runCaseLabels}
                  />
                ) : (
                  <EvaluationResult metrics={job.run_result} caseLabels={runCaseLabels} />
                )}
                </ResultExportContext.Provider>
              ) : null
            }
          />
        ) : runMutation.isPending ? (
          <EvaluationResultSkeleton />
        ) : (
          <Card>
            <CardContent className="pt-5">
              <EmptyState
                title={t("evaluation.result.empty")}
                hint={t("evaluation.result.emptyHint")}
              />
            </CardContent>
          </Card>
        )}
        </TabPanel>
      </PageBody>
    </div>
  );
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean";
}

/** 作業状態に残した期待の下書き（ケースの id → 下書き。壊れた値は捨てる）。 */
function isProcessDrafts(value: unknown): value is Record<string, EvaluationProcessDraft> {
  return (
    isRecord(value) &&
    Object.values(value).every(
      (draft) =>
        isRecord(draft) &&
        typeof draft.case_id === "string" &&
        Array.isArray(draft.required_answer_points) &&
        Array.isArray(draft.required_evidence) &&
        Array.isArray(draft.references)
    )
  );
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 作業状態に残した検証の結果の形（古い版・壊れた値は捨てる）。 */
function isImportResult(value: unknown): value is EvaluationImportResult | null {
  if (value === null) return true;
  return (
    isRecord(value) &&
    typeof value.file_name === "string" &&
    typeof value.error_count === "number" &&
    Array.isArray(value.cases) &&
    Array.isArray(value.rows) &&
    Array.isArray(value.errors) &&
    isStringArray(value.knowledge_base_ids) &&
    isRecord(value.document_names)
  );
}

function isCaseLabels(value: unknown): value is EvaluationCaseLabels {
  return (
    isRecord(value) &&
    Object.values(value).every(
      (label) => isRecord(label) && typeof label.question === "string" && Array.isArray(label.evidence)
    )
  );
}

interface EvaluationJobState {
  jobId: string;
  job: EvaluationJob | null;
  query: ReturnType<typeof useEvaluationJob>;
  start: (job: EvaluationJob) => void;
  cancel: (job: EvaluationJob) => Promise<void>;
  cancelling: boolean;
}

/**
 * 評価の job の状態。job id を作業状態に残し、実行中は状態を取得し続ける。
 * 実行中から終わりへ移ったときだけ、完了・失敗を 1 回通知する（messaging.md §4.2）。
 */
function useEvaluationJobState(): EvaluationJobState {
  const [jobId, setJobId] = useWorkspaceState<string>("evaluation.runJobId", "", isEvaluationJobId);
  const query = useEvaluationJob(jobId || null);
  const cancelMutation = useCancelEvaluationJob();
  const confirm = useConfirm();
  const job = query.data ?? null;
  const observed = useRef<{ jobId: string; status: EvaluationJobStatus } | null>(null);

  useEffect(() => {
    if (!job) return;
    const previous = observed.current;
    observed.current = { jobId: job.job_id, status: job.status };
    if (previous?.jobId !== job.job_id || previous.status !== "RUNNING") return;
    if (job.status === "SUCCEEDED") {
      toast.success(t("evaluation.toast.succeeded"));
    } else if (job.status === "FAILED") {
      toast.error(t("evaluation.toast.failed"));
    }
  }, [job]);

  const cancel = async (target: EvaluationJob) => {
    const confirmed = await confirm({
      title: t("evaluation.job.cancelConfirm.title"),
      description: t("evaluation.job.cancelConfirm.description"),
      confirmLabel: t("evaluation.job.cancelConfirm.confirm"),
      tone: "danger",
    });
    if (!confirmed) return;
    cancelMutation.mutate(target.job_id, {
      onSuccess: () => toast.success(t("evaluation.toast.cancelled")),
      onError: (error) =>
        toast.error(error instanceof ApiError ? error.message : t("evaluation.error.cancel")),
    });
  };

  return {
    jobId,
    job,
    query,
    start: (started) => {
      observed.current = { jobId: started.job_id, status: started.status };
      setJobId(started.job_id);
    },
    cancel,
    cancelling: cancelMutation.isPending,
  };
}

/** job の実行状況と、実行中は結果の Skeleton、成功したら結果を出す。 */
function EvaluationJobSection({
  state,
  renderResult,
}: {
  state: EvaluationJobState;
  renderResult: (job: EvaluationJob) => ReactNode;
}) {
  const { job, query } = state;
  if (!job) {
    if (query.isError) {
      const error = query.error;
      return (
        <EvaluationJobError
          notFound={error instanceof ApiError && error.status === 404}
          message={error instanceof ApiError ? error.message : null}
          onRetry={() => void query.refetch()}
        />
      );
    }
    return <EvaluationJobLoading />;
  }
  // 実行中に状態の取得が失敗したとき（再起動・通信断など）。取得は自動で続けるので、最後に分かっている
  // 状態のまま、パネルの直下に取得できていないことを出す（取得できたら消える。#977）。
  const pollError = query.isError && job.status === "RUNNING" ? query.error : null;
  return (
    <>
      <EvaluationJobPanel
        job={job}
        onCancel={(target) => void state.cancel(target)}
        cancelling={state.cancelling}
      />
      {pollError ? (
        <Banner severity="warning">
          {pollError instanceof ApiError && pollError.status === 404
            ? t("evaluation.job.notFound")
            : t("evaluation.job.pollError")}
        </Banner>
      ) : null}
      {job.status === "RUNNING" ? <EvaluationResultSkeleton /> : null}
      {job.status === "SUCCEEDED" ? renderResult(job) : null}
    </>
  );
}

/**
 * 評価の基準（表示だけ。#2065）。評価は常に設定の既定の基準を使うので、画面では選ばせず、適用される
 * 閾値と設定の画面（取込・検索・回答設定 › 評価の基準）へのリンクだけを出す。
 */
function SuiteSummary({ query }: { query: ReturnType<typeof useEvaluationSettings> }) {
  const suite = query.data?.suite ?? null;
  const status = suite ? (query.data?.suites.find((item) => item.name === suite) ?? null) : null;
  const thresholdEntries = orderedThresholdEntries(status?.thresholds ?? query.data?.thresholds);
  return (
    <Card className="min-w-0" data-testid="evaluation-suite">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ClipboardCheck size={16} className="text-accent-fg" aria-hidden />
          {t("evaluation.suite.title")}
        </CardTitle>
        <CardDescription>{t("evaluation.suite.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {query.isPending ? (
          <TimedLoadingState
            label={t("settings.loading")}
            operationKey="evaluation-suite-load"
            framed={false}
            testId="evaluation-suite-loading"
          >
            <Skeleton className="h-5 w-32" aria-hidden="true" />
            <Skeleton className="h-16 w-full" aria-hidden="true" />
          </TimedLoadingState>
        ) : query.isError ? (
          <Banner severity="warning">
            {query.error instanceof ApiError ? query.error.message : t("evaluation.error.run")}
          </Banner>
        ) : (
          <>
            {suite ? (
              <p className="text-sm font-medium text-fg" data-testid="evaluation-suite-current">
                {t("evaluation.suite.current", { suite: suiteLabel(suite) })}
              </p>
            ) : null}
            <div
              data-surface-tint="neutral"
              className="rounded-md border border-border bg-surface-hover p-3"
            >
              <p className="text-xs font-medium text-fg-muted">
                {t("evaluation.suite.thresholdsPreview")}
              </p>
              {thresholdEntries.length ? (
                <ul className="mt-2 flex flex-wrap gap-1.5" data-testid="evaluation-suite-thresholds">
                  {thresholdEntries.map(([metric, value]) => (
                    <li
                      key={metric}
                      className="inline-flex min-h-6 items-center rounded-md bg-surface px-2 text-xs font-medium text-fg ring-1 ring-border"
                    >
                      {metricLabel(metric)}
                      <span className="tnum ml-1 font-semibold text-accent-fg">
                        {formatMetricValue(value)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-sm text-fg">{t("evaluation.suite.noThresholds")}</p>
              )}
            </div>
          </>
        )}
        <Link
          to={APP_ROUTES.settingsEvaluation}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-accent-fg hover:underline"
        >
          <Settings2 size={14} aria-hidden />
          {t("evaluation.suite.settingsLink")}
        </Link>
      </CardContent>
    </Card>
  );
}

/**
 * 検索・回答プロファイルでも評価して比較する選択（#2402）。オンにしたときだけ、比べる
 * プロファイルの欄を出す（段階的に開く）。
 */
function CompareOption({
  enabled,
  onEnabledChange,
  profileId,
  onProfileChange,
  query,
  baseProfileId,
  error,
  disabled,
}: {
  enabled: boolean;
  onEnabledChange: (enabled: boolean) => void;
  profileId: string | null;
  onProfileChange: (id: string) => void;
  query: ReturnType<typeof useSearchAnswerProfiles>;
  baseProfileId: string | null;
  error: string | null;
  disabled: boolean;
}) {
  return (
    <div className="grid min-w-0 gap-3" data-testid="evaluation-compare">
      <div className="flex items-start gap-3">
        <Switch
          id={COMPARE_SWITCH_ID}
          checked={enabled}
          disabled={disabled}
          aria-describedby={`${COMPARE_SWITCH_ID}-help`}
          onCheckedChange={onEnabledChange}
        />
        <div className="min-w-0">
          <label htmlFor={COMPARE_SWITCH_ID} className="cursor-pointer text-sm font-medium text-fg">
            {t("evaluation.compare.toggle")}
          </label>
          <p id={`${COMPARE_SWITCH_ID}-help`} className="text-xs text-fg-muted">
            {t("evaluation.compare.helper")}
          </p>
        </div>
      </div>
      {enabled ? (
        query.isPending ? (
          // RAG 検索・チャットと同じく、先頭に文言と経過時間の行、その下に欄の形の Skeleton（#2510）。
          <TimedLoadingState
            label={t("searchAnswerProfiles.loading")}
            operationKey="evaluation-compare-profiles-load"
            framed={false}
            testId="evaluation-compare-profiles-loading"
          >
            <SearchAnswerProfileSelectSkeleton />
          </TimedLoadingState>
        ) : query.isError ? (
          <Banner severity="warning">{t("evaluation.compare.profilesLoadError")}</Banner>
        ) : (
          <SearchAnswerProfileSelect
            id={COMPARE_PROFILE_ID}
            label={t("evaluation.compare.profileLabel")}
            items={(query.data?.items ?? []).filter((profile) => profile.id !== baseProfileId)}
            value={profileId}
            onChange={onProfileChange}
            disabled={disabled}
            error={error ?? undefined}
          />
        )
      ) : null}
    </div>
  );
}

/**
 * 二つの検索・回答プロファイルの結果を並べる（#2402）。指標の表（差はポイントと
 * 改善・悪化の文言とアイコンで示し、色だけに頼らない）と、どちらの結果の詳細を見るかのタブ。
 */
function EvaluationComparisonResult({
  metrics,
  baseName,
  comparedName,
  caseLabels,
}: {
  metrics: EvaluationMetrics;
  baseName: string | null;
  comparedName: string | null;
  caseLabels: EvaluationCaseLabels;
}) {
  const [tab, setTab] = useState("base");
  const compared = metrics.comparison?.metrics;
  if (!compared) return null;
  const name = comparedName ?? metrics.comparison?.search_answer_profile_id ?? "";
  const rows = comparisonRows(metrics, compared);
  const tabs = [
    { id: "base", label: t("evaluation.compare.tab.base", { name: baseName ?? "" }) },
    { id: "compared", label: t("evaluation.compare.tab.compared", { name }) },
  ];
  return (
    <section className="min-w-0 space-y-4" aria-labelledby="evaluation-compare-title">
      <div className="space-y-1">
        <h2 id="evaluation-compare-title" className="text-base font-semibold text-fg">
          {t("evaluation.compare.title")}
        </h2>
        <p className="text-xs text-fg-muted">
          {t("evaluation.compare.description", { name, baseName: baseName ?? "" })}
        </p>
      </div>
      <div data-testid="evaluation-compare-table">
        <DataTable<EvaluationComparisonRow>
          ariaLabel={t("evaluation.compare.title")}
          rows={rows}
          getRowKey={(row) => row.metric}
          columns={[
            {
              key: "metric",
              header: t("evaluation.compare.column.metric"),
              rowHeader: true,
              className: "min-w-[9rem] font-medium text-fg",
              render: (row) => (
                <span className="grid gap-0.5">
                  <span>{metricLabel(row.metric)}</span>
                  {LOWER_IS_BETTER_METRICS.has(row.metric) ? (
                    <span className="text-xs font-normal text-fg-muted">
                      {t("evaluation.compare.lowerIsBetter")}
                    </span>
                  ) : null}
                </span>
              ),
            },
            {
              key: "base",
              header: t("evaluation.compare.column.base", { name: baseName ?? "" }),
              className: "tnum whitespace-nowrap",
              render: (row) => formatMetricValue(row.base),
            },
            {
              key: "compared",
              header: t("evaluation.compare.column.compared", { name }),
              className: "tnum whitespace-nowrap",
              render: (row) => formatMetricValue(row.compared),
            },
            {
              key: "delta",
              header: t("evaluation.compare.column.delta"),
              className: "whitespace-nowrap",
              render: (row) => <DeltaCell delta={row.delta} direction={row.direction} />,
            },
          ]}
        />
      </div>
      <Tabs
        items={tabs}
        value={tab}
        onChange={setTab}
        ariaLabel={t("evaluation.compare.tabsLabel")}
        idPrefix="evaluation-compare"
      />
      <TabPanel id="base" value={tab} idPrefix="evaluation-compare">
        <EvaluationResult metrics={metrics} caseLabels={caseLabels} />
      </TabPanel>
      <TabPanel id="compared" value={tab} idPrefix="evaluation-compare">
        <EvaluationResult metrics={compared} caseLabels={caseLabels} />
      </TabPanel>
    </section>
  );
}

const DIRECTION_KEYS = {
  better: "evaluation.compare.better",
  worse: "evaluation.compare.worse",
  same: "evaluation.compare.same",
} as const;

/** 差（ポイント）と向き。向きはアイコンと文言で示す（色だけに頼らない）。 */
function DeltaCell({
  delta,
  direction,
}: {
  delta: number | null;
  direction: ComparisonDirection | null;
}) {
  if (direction === null) return <span className="text-fg-muted">—</span>;
  const Icon = direction === "better" ? TrendingUp : direction === "worse" ? TrendingDown : Minus;
  const tone =
    direction === "better"
      ? "text-success-fg"
      : direction === "worse"
        ? "text-danger-fg"
        : "text-fg-muted";
  return (
    <span className="inline-flex items-center gap-1.5" data-testid="evaluation-compare-delta">
      <Icon size={14} className={`shrink-0 ${tone}`} aria-hidden />
      <span className="tnum">{formatMetricDelta(delta)}</span>
      <span className="text-xs text-fg-muted">{t(DIRECTION_KEYS[direction])}</span>
    </span>
  );
}

function EvaluationResult({
  metrics,
  caseLabels,
}: {
  metrics: EvaluationMetrics;
  caseLabels: EvaluationCaseLabels;
}) {
  const failedMetrics = new Set(metrics.threshold_failures.map((failure) => failure.metric));
  const failureReasons = Object.entries(metrics.failure_reason_counts).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number" && entry[1] > 0
  );
  return (
    <section className="min-w-0 space-y-4" aria-labelledby="evaluation-result-title">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="evaluation-result-title" className="text-base font-semibold text-fg">
          {t("evaluation.result.title")}
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          {metrics.evaluation_suite ? (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-sunken px-2.5 py-1 text-xs font-medium text-fg-muted">
              <ClipboardCheck size={14} aria-hidden />
              {t("evaluation.suite.applied")}: {suiteLabel(metrics.evaluation_suite)}
            </span>
          ) : null}
          {metrics.error_count > 0 ? (
            <StatusBadge
              variant="danger"
              label={t("evaluation.errors", {
                count: metrics.error_count,
                total: metrics.case_count,
              })}
            />
          ) : null}
          <PassedBadge passed={metrics.passed} />
        </div>
      </div>

      {EVALUATION_PERSPECTIVES.filter((perspective) => isPerspectiveShown(metrics, perspective)).map((perspective) => (
        <MetricGroup
          key={perspective.id}
          groupId={perspective.id}
          title={perspectiveLabel(perspective.id)}
          description={perspectiveDescription(perspective.id)}
          metricNames={perspective.metrics}
          metrics={metrics}
          failedMetrics={failedMetrics}
        />
      ))}

      <CategoryBreakdown metrics={metrics} />
      <DecisionBreakdown metrics={metrics} />

      {failureReasons.length ? (
        <div className="rounded-md border border-border bg-surface p-4 text-sm">
          <p className="font-medium text-fg">{t("evaluation.failureReasons")}</p>
          <ul className="mt-3 flex flex-wrap gap-2" data-testid="evaluation-failure-reasons">
            {failureReasons.map(([reason, count]) => (
              <li
                key={reason}
                className="rounded-full border border-border bg-surface-sunken px-2.5 py-1 text-xs text-fg-muted"
              >
                {failureReasonLabel(reason)}: <span className="tnum">{count}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <CaseTable metrics={metrics} caseLabels={caseLabels} />
    </section>
  );
}

/** 1 つの観点（検索・根拠・回答・案内）の指標。測れなかった指標は「—」と「対象のケースなし」。 */
function MetricGroup({
  groupId,
  title,
  description,
  metricNames,
  metrics,
  failedMetrics,
}: {
  groupId: string;
  title: string;
  description: string;
  metricNames: readonly EvaluationMetricName[];
  metrics: EvaluationMetrics;
  failedMetrics: ReadonlySet<string>;
}) {
  const titleId = `evaluation-perspective-${groupId}`;
  return (
    <section aria-labelledby={titleId} className="space-y-2" data-testid={titleId}>
      <div>
        <h3 id={titleId} className="text-sm font-semibold text-fg">
          {title}
        </h3>
        <p className="text-xs text-fg-muted">{description}</p>
      </div>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {metricNames.map((metric) => {
          const value = metricValue(metrics, metric);
          const count = metricCaseCount(metrics, metric);
          const threshold = metrics.applied_thresholds?.[metric]
            ?? metrics.threshold_failures.find(failure => failure.metric === metric)?.threshold;
          return (
            <Card key={metric} data-testid={`evaluation-metric-${metric}`}>
              <CardContent className="space-y-1.5 pt-5">
                <p className="text-xs font-medium text-fg-muted" title={metricDescription(metric)}>
                  {metricLabel(metric)}
                </p>
                <p className="tnum text-2xl font-semibold text-fg">
                  {formatMetricValue(value)}
                  {threshold != null ? (
                    <span className="text-sm font-normal text-fg-muted"
                      aria-label={t("evaluation.metric.threshold", {value:formatMetricValue(threshold)})}
                      data-testid={`evaluation-threshold-${metric}`}>
                      {" / "}{formatMetricValue(threshold)}
                    </span>
                  ) : null}
                </p>
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-fg-muted">
                  {value === null ? (
                    <span>
                      {STANDARD_ANSWER_METRICS.has(metric as EvaluationMetricName)
                        ? t("evaluation.metric.needsStandardAnswer")
                        : t("evaluation.metric.notMeasured")}
                    </span>
                  ) : null}
                  {count !== null ? (
                    <span className="tnum">{t("evaluation.metric.caseCount", { count })}</span>
                  ) : null}
                  {failedMetrics.has(metric) ? (
                    <StatusBadge variant="warning" label={t("evaluation.metric.belowThreshold")} />
                  ) : null}
                  {/* 観点の判定が今の評価器で校正済みか（#3578。指標の値は変えない）。 */}
                  <JudgeCalibrationBadge metric={metric} />
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </section>
  );
}

function CaseTable({
  metrics,
  caseLabels,
}: {
  metrics: EvaluationMetrics;
  caseLabels: EvaluationCaseLabels;
}) {
  const exportContext = useContext(ResultExportContext);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const download = async () => {
    if (!exportContext || downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const input = exportContext.input;
      const fallbackRows = Object.entries(caseLabels).map(([case_id, label]) => ({
        row: Number(case_id.replace("row-", "")) || 0, case_id,
        question: label.question, evidence: label.evidence.map(item => item.label),
        standard_answer_preview: null, has_standard_answer: false,
      }));
      saveDownloadedFile(await api.downloadEvaluationResults(exportContext.jobId, {
        rows: input?.rows ?? fallbackRows, cases: input?.cases ?? [],
        profile_name: exportContext.profileName, compare_profile_name: exportContext.compareProfileName,
      }));
      toast.success(t("evaluation.export.done"));
    } catch (error) {
      setDownloadError(error instanceof ApiError ? error.message : t("evaluation.export.error"));
    } finally { setDownloading(false); }
  };
  const results = metrics.case_results;
  // 評価の結果ごとに 1 ページ目から。結果は trace の ID（ケースごとに一意）で見分ける（#1891）。
  const paging = useClientListPaging(
    "evaluation.casesPaging",
    `${results.length}:${results[0]?.trace_id ?? ""}:${results.at(-1)?.trace_id ?? ""}`
  );
  const hasEvidence = results.some((result) => (caseLabels[result.case_id]?.evidence.length ?? 0) > 0);
  return (
    <section aria-labelledby="evaluation-cases-title">
      {/* 見出しの右に件数（ListCount。#1891）。 */}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 id="evaluation-cases-title" className="text-sm font-semibold text-fg">
          {t("evaluation.cases")}
        </h3>
        <ListToolbar
          count={listCountProps({ matchedTotal: results.length, filtered: false, live: true })}
          // 見出しの残りの幅を持たせる（ListToolbar は container query の箱なので、幅を内容から決めない）。
          className="min-w-0 flex-1"
          testId="evaluation-case-toolbar"
        />
        {exportContext ? (
          <Button type="button" size="sm" variant="secondary" icon={Download}
            loading={downloading} onClick={() => void download()}>
            {t("evaluation.export.action")}
          </Button>
        ) : null}
      </div>
      {downloadError ? <Banner severity="danger">{downloadError}</Banner> : null}
      <PagedDataTable<EvaluationCaseResult>
        viewport="bounded"
        paginationLabels={paginationLabels()}
        {...paging}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        paginationAriaLabel={t("evaluation.case.paginationLabel")}
        columns={[
          {
            key: "question",
            header: t("evaluation.case.question"),
            rowHeader: true,
            // 375px でも質問が 1 文字ずつ折り返さない幅（表は横にスクロールする）。
            className: "min-w-[10rem] break-words font-medium text-fg",
            render: (result) => (
              <CaseQuestionCell result={result} label={caseLabels[result.case_id]} />
            ),
          },
          ...(hasEvidence
            ? [
                {
                  key: "evidence",
                  header: t("evaluation.case.evidence"),
                  className: "min-w-[10rem]",
                  render: (result: EvaluationCaseResult) => (
                    <EvidenceCell result={result} label={caseLabels[result.case_id]} />
                  ),
                },
              ]
            : []),
          ...EVALUATION_PERSPECTIVES.flatMap(group => group.metrics.map(metric => ({
            key: metric, header: `${perspectiveLabel(group.id)}: ${metricLabel(metric)}`,
            className: "tnum whitespace-nowrap",
            render: (result: EvaluationCaseResult) => formatMetricValue(result[metric]),
          }))),
          {
            key: "failures",
            header: t("evaluation.case.failures"),
            headerClassName: "hidden md:table-cell",
            className: "hidden break-words text-xs text-fg-muted md:table-cell",
            render: (result) =>
              result.failure_reasons.length
                ? result.failure_reasons.map(failureReasonLabel).join("、")
                : "-",
          },
          {
            key: "trace",
            header: t("evaluation.case.trace"),
            headerClassName: "hidden xl:table-cell",
            className: "tnum hidden whitespace-nowrap text-xs text-fg-muted xl:table-cell",
            render: (result) => result.trace_id.slice(0, 12),
          },
        ]}
        rows={results}
        getRowKey={(result) => result.case_id}
        scrollAriaLabel={t("evaluation.case.scrollLabel")}
        scrollTestId="evaluation-case-scroll-region"
        paginationTestId="evaluation-case-pagination"
        tableClassName="w-full min-w-[22.86rem] text-sm md:min-w-[48.57rem]"
      />
    </section>
  );
}

/**
 * ケースの質問（Excel の行と質問）と、失敗したケース（status=error）の理由・時間切れになった工程（#383）。
 * 375px でも見えるよう、狭い幅で隠す「理由」の列ではなく質問の列に出す。見出しの無い古い結果は id。
 */
function CaseQuestionCell({
  result,
  label,
}: {
  result: EvaluationCaseResult;
  label: EvaluationCaseLabel | undefined;
}) {
  const error = evaluationCaseErrorSummary(result);
  return (
    <div className="grid min-w-0 gap-1" data-testid="evaluation-case-question">
      {label ? (
        <>
          <span className="tnum text-xs font-normal text-fg-muted">
            {evaluationCaseRowLabel(result.case_id)}
          </span>
          <span>{label.question}</span>
        </>
      ) : (
        <span>{result.case_id}</span>
      )}
      {error ? (
        <div className="grid min-w-0 gap-1.5" data-testid="evaluation-case-error">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge variant="danger" label={t("evaluation.case.error")} />
            {error.stageLabel ? (
              <span className="inline-flex items-center rounded-md bg-surface-sunken px-2 py-0.5 text-xs font-medium text-fg-muted ring-1 ring-border">
                {t("evaluation.case.errorStage", { stage: error.stageLabel })}
              </span>
            ) : null}
          </div>
          <p className="break-words text-xs font-normal text-fg-muted">{error.message}</p>
        </div>
      ) : null}
    </div>
  );
}

/**
 * 期待する根拠の照合の結果（#2065）。見つかった数と、根拠ごとの見つかった / 見つからない（アイコンと
 * 読み上げの文言で示し、色だけに頼らない）。測れないケース（検索をしない回答・失敗）は「測れず」。
 */
function EvidenceCell({
  result,
  label,
}: {
  result: EvaluationCaseResult;
  label: EvaluationCaseLabel | undefined;
}) {
  const summary = evaluationEvidenceSummary(result, label);
  if (!summary) return <span className="text-fg-muted">—</span>;
  if (!summary.measured) {
    return <span className="text-xs text-fg-muted">{t("evaluation.case.evidenceNotMeasured")}</span>;
  }
  return (
    <div className="grid min-w-0 gap-1" data-testid="evaluation-case-evidence">
      <span className="tnum text-sm font-medium text-fg">
        {t("evaluation.case.evidenceFound", { found: summary.found, total: summary.total })}
      </span>
      <ul className="grid gap-0.5">
        {summary.items.map((item, index) => (
          <li
            key={`${index}:${item.label}`}
            className="flex min-w-0 items-start gap-1 text-xs text-fg-muted"
          >
            <span className="pr-icon-line">
              {item.found ? (
                <CheckCircle2 size={12} className="text-success-fg" aria-hidden />
              ) : (
                <XCircle size={12} className="text-danger-fg" aria-hidden />
              )}
            </span>
            <span className="min-w-0 break-all">
              <span className="sr-only">
                {t(item.found ? "evaluation.case.evidenceItemFound" : "evaluation.case.evidenceItemMissing")}
                :{" "}
              </span>
              {item.label}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * 業務支援の採点（#1231）。回答の対応と期待どおりか（アイコンと文言）、不足の手順・危険な表現・
 * 触れていない条件。推定した対応は「（推定）」を付ける。
 */
function PassedBadge({ passed }: { passed: boolean | null }) {
  if (passed === null) return <StatusBadge variant="neutral" label={t("evaluation.metric.unassessed")} />;
  return (
    <StatusBadge
      variant={passed ? "success" : "danger"}
      label={passed ? t("evaluation.status.passed") : t("evaluation.status.failed")}
    />
  );
}

/** 分類ごとの結果（#1226）。ケースに分類があるときだけ出す。 */
function CategoryBreakdown({ metrics }: { metrics: EvaluationMetrics }) {
  const rows = Object.entries(metrics.category_breakdown ?? {})
    .filter((entry): entry is [string, EvaluationCategorySummary] => Boolean(entry[1]))
    .map(([category, summary]) => ({ category, ...summary }));
  if (!rows.length) return null;
  return (
    <div className="space-y-2" data-testid="evaluation-category-breakdown">
      <p className="text-sm font-medium text-fg">{t("evaluation.category.title")}</p>
      <DataTable
        ariaLabel={t("evaluation.category.title")}
        rows={rows}
        getRowKey={(row) => row.category}
        columns={[
          {
            key: "category",
            header: t("evaluation.category.column.category"),
            render: (row) => categoryLabel(row.category),
          },
          {
            key: "count",
            header: t("evaluation.category.column.count"),
            className: "tnum",
            render: (row) =>
              row.error_count
                ? t("evaluation.category.countWithErrors", { count: row.case_count, errors: row.error_count })
                : String(row.case_count),
          },
          ...EVALUATION_PERSPECTIVES.flatMap(group => group.metrics.map(metric => ({
            key: metric, header: `${perspectiveLabel(group.id)}: ${metricLabel(metric)}`,
            className: "tnum whitespace-nowrap",
            render: (row: (typeof rows)[number]) => formatMetricValue(row.metrics[metric]),
          }))),
        ]}
      />
    </div>
  );
}

function DecisionBreakdown({metrics}:{metrics:EvaluationMetrics}) {
 const rows=Object.entries(metrics.decision_breakdown ?? {}).flatMap(([decision,summary])=>summary?[{decision,...summary}]:[]);
 if (!rows.length) return null;
 const labels={answer:"evaluation.decision.answer",clarify:"evaluation.decision.clarify",hold:"evaluation.decision.hold"} as const;
 return <section className="space-y-2" data-testid="evaluation-decision-breakdown">
   <h3 className="text-sm font-semibold text-fg">{t("evaluation.decision.breakdown")}</h3>
   <DataTable ariaLabel={t("evaluation.decision.breakdown")} rows={rows} getRowKey={row=>row.decision} columns={[
     {key:"decision",header:t("evaluation.perspective.decision"),render:row=>row.decision in labels?t(labels[row.decision as keyof typeof labels]):row.decision},
     {key:"count",header:t("evaluation.category.column.count"),render:row=>String(row.metric_case_counts.response_decision_accuracy ?? 0)},
     {key:"score",header:metricLabel("response_decision_accuracy"),render:row=>formatMetricValue(row.metrics.response_decision_accuracy)},
   ]}/>
 </section>;
}

const CATEGORY_KEYS = {
  document_answerable: "evaluation.category.document_answerable",
  clarification_required: "evaluation.category.clarification_required",
  environment_data_required: "evaluation.category.environment_data_required",
  knowledge_missing: "evaluation.category.knowledge_missing",
  conflicting_sources: "evaluation.category.conflicting_sources",
  uncategorized: "evaluation.category.uncategorized",
} as const;

function categoryLabel(category: string): string {
  return category in CATEGORY_KEYS ? t(CATEGORY_KEYS[category as keyof typeof CATEGORY_KEYS]) : category;
}
