import { WarningsBanner } from "@/components/WarningsBanner";
import { ErrorState } from "@/components/StateViews";
import {
  apiErrorMessage,
  Button,
  buttonVariants,
  Banner,
  DataTable,
  EmptyState,
  FormStatus,
  SelectField,
  FilterSelect,
  isFilterActive,
  toast,
  usePagination,
  lastPageOffset,
  ListToolbar,
  OffsetPagination,
  StatusBadge,
  PageHeader,
  PageBody,
  useConfirm,
  BulkSelectionActions,
  ProcessingIndicator,
  RowActionMenu,
  type EntityAction,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  Pagination,
  PAGE_SIZE_OPTIONS,
  TextareaField,
} from "@production-ready/ui";
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Save,
  CheckSquare,
  BrainCircuit,
  Download,
  FileSpreadsheet,
  Link2,
  ListChecks,
  Pencil,
  Play,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { PageHeaderStatusBadge } from "@/components/PageHeaderStatusBadge";
import { PageNotice } from "@/components/page-notice";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { formatDateTime, formatNumber } from "@/lib/format";
import { apiDelete, apiGet, apiPatch, apiPost, apiPostForm, isAbortError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { listCountLabels, listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { useWorkspacePaging, type WorkspacePaging } from "@/lib/use-workspace-paging";
import { useWorkspaceState } from "@/components/WorkspaceState";
import { APP_ROUTES } from "@/lib/routes";
import { appPath } from "@/lib/base-path";
import { XLSX_TEMPLATE_FILE_FORMATS } from "@/lib/tabular-file-formats";
import { useRequestScope } from "@/lib/useRequestScope";
import {
  DbManagementLoadingSkeleton,
  DbManagementSearchField,
  DbObjectManagementPanelShell,
  DbObjectManagementTabs,
  DbObjectPanelHeader,
  DbObjectStepIndicator,
} from "../components/DbObjectManagementShared";
import { QuestionText } from "../components/QuestionText";
import {
  profileDisplayLabel,
  profileNameLabel,
  profileRecordDisplayLabel,
  profileSelectOption,
} from "../profileDisplay";
import type {
  ClassifierImportData,
  ClassifierFeedbackImportData,
  ClassifierPredictionData,
  ClassifierStatusData,
  ClassifierTrainingDataData,
  ClassifierTrainingCandidate,
  ClassifierTrainingCandidatesData,
  ClassifierTrainingExample,
  ProfileSummary,
  ProfileSummaryPage,
} from "../types";

type ActiveView = "trainingData" | "train" | "test" | "candidates";
/**
 * 操作の失敗を出す位置（起点の操作の直下。messaging.md §10.1、#724）。
 * import = 取込のファイル欄の下、trainingData = 学習データの一覧（再読み込み・行の編集・削除）の上、
 * train = 学習のボタンの下、predict = 分類テストのボタンの下。ページの読み込みの失敗だけページ先頭に出す。
 */
type ActionErrorOrigin = "import" | "trainingData" | "train" | "predict";
type ActionError = { origin: ActionErrorOrigin; message: string } | null;
type ClassifierPredictionSnapshot = ClassifierPredictionData & {
  inputQuestion: string;
  modelVersion: string;
  finishedAt: string;
};

const CANDIDATE_STATUS_VALUES = [
  "all",
  "pending",
  "added",
  "already_covered",
  "conflict",
  "profile_missing",
  "source_changed",
] as const;

interface CandidateFilters {
  search: string;
  status: string;
  profileId: string;
}

/** 学習候補の取得の位置（offset 型のページング。1 ページは表示件数（既定 25 件）。#1891）。 */
interface CandidatePosition {
  offset: number;
  limit: number;
}

export function QuestionClassifierModelsPage() {
  const confirm = useConfirm();
  const [searchParams] = useSearchParams();
  const [activeView, setActiveView] = useState<ActiveView>(
    searchParams.get("tab") === "candidates" ? "candidates" : "trainingData"
  );
  const focusedCandidateHistoryId = searchParams.get("history_id")?.trim() ?? "";
  const [profiles, setProfiles] = useState<ProfileSummary[]>([]);
  const [question, setQuestion] = useState("登録済みの表から主要な列を一覧したい");
  const [classifierStatus, setClassifierStatus] = useState<ClassifierStatusData | null>(null);
  const [classifierTrainingData, setClassifierTrainingData] = useState<ClassifierTrainingDataData | null>(null);
  const [candidates, setCandidates] = useState<ClassifierTrainingCandidatesData | null>(null);
  // 学習候補の絞り込みとページ番号・表示件数は作業状態に残し、再読込・画面の移動のあとも同じ一覧に戻る（#1891）。
  const [candidateSearch, setCandidateSearch] = useWorkspaceState("candidateSearch", "");
  const [candidateStatus, setCandidateStatus] = useWorkspaceState("candidateStatus", "all");
  const [candidateProfileId, setCandidateProfileId] = useWorkspaceState("candidateProfileId", "");
  const candidatePaging = useWorkspacePaging("candidatePaging");
  const [candidateAppliedFilters, setCandidateAppliedFilters] = useState<CandidateFilters>(() => ({
    search: candidateSearch, status: candidateStatus, profileId: candidateProfileId,
  }));
  const [candidateHasActiveFilters, setCandidateHasActiveFilters] = useState(false);
  const [candidateError, setCandidateError] = useState("");
  const [candidateActionError, setCandidateActionError] = useState("");
  const [selectedCandidates, setSelectedCandidates] = useState<Set<string>>(new Set());
  const [candidateProfileOverrides, setCandidateProfileOverrides] = useState<Record<string, string>>({});
  const [classifierPrediction, setClassifierPrediction] = useState<ClassifierPredictionSnapshot | null>(null);
  const [classifierReplace, setClassifierReplace] = useState(false);
  // 学習データの検索語とページ番号・表示件数は作業状態に残す（#1891）。
  const [trainingSearch, setTrainingSearch] = useWorkspaceState("trainingSearch", "");
  const trainingPaging = useWorkspacePaging("trainingPaging");
  const [trainingFilename, setTrainingFilename] = useState("");
  const [editingExampleId, setEditingExampleId] = useState("");
  const [editingText, setEditingText] = useState("");
  const [editingProfileId, setEditingProfileId] = useState("");
  // 初回の読み込み（mount 時の effect）の間は "load" から始める。
  const [loading, setLoading] = useState("load");
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState<ActionError>(null);
  const showActionError = (origin: ActionErrorOrigin, err: unknown, fallback: string) =>
    setActionError({ origin, message: apiErrorMessage(err, fallback) });
  const actionErrorFor = (origin: ActionErrorOrigin) =>
    actionError?.origin === origin ? actionError.message : "";
  const [editingBaseline, setEditingBaseline] = useState("");
  const editingDirty = Boolean(editingExampleId &&
    JSON.stringify([editingText, editingProfileId]) !== editingBaseline);
  const confirmDiscard = () => confirm({
    title: t("qcm.training.discard.title"),
    description: t("qcm.training.discard.description"),
    confirmLabel: t("qcm.training.discard.confirm"),
    tone: "danger",
    dismissOnOverlay: false,
  });
  useUnsavedChangesGuard(editingDirty, confirmDiscard);
  const cancelEditing = async () => {
    if (loading || (editingDirty && !(await confirmDiscard()))) return;
    setEditingExampleId("");
  };
  const loadSequence = useRef(0);
  const retryCandidates = useRef<(() => void) | null>(null);
  const candidateLoadSequence = useRef(0);
  const { abortAll, run: runScopedRequest } = useRequestScope();

  const filteredExamples = useMemo(() => {
    const query = trainingSearch.trim().toLowerCase();
    const examples = classifierTrainingData?.examples ?? [];
    if (!query) return examples;
    return examples.filter(
      (example) =>
        example.category.toLowerCase().includes(query) ||
        (example.profile_name ?? "").toLowerCase().includes(query) ||
        example.text.toLowerCase().includes(query) ||
        (example.source ?? "").toLowerCase().includes(query)
    );
  }, [classifierTrainingData, trainingSearch]);

  const candidateUrl = (position: CandidatePosition, filters: CandidateFilters) => {
    const params = new URLSearchParams({
      limit: String(position.limit),
      offset: String(position.offset),
      status: filters.status,
    });
    if (filters.profileId) params.set("profile_id", filters.profileId);
    if (filters.search.trim()) params.set("q", filters.search.trim());
    if (focusedCandidateHistoryId) params.set("history_id", focusedCandidateHistoryId);
    return `/api/nl2sql/classifier/training-candidates?${params.toString()}`;
  };

  const currentCandidatePosition = (): CandidatePosition => ({
    offset: candidatePaging.offset,
    limit: candidatePaging.pageSize,
  });

  // 取得したページが範囲の外（追加・絞り込みで減った）なら、最後のページを取り直す（空のページを出さない）。
  const fetchCandidatesInRange = async (
    position: CandidatePosition,
    filters: CandidateFilters,
    signal?: AbortSignal
  ): Promise<{ data: ClassifierTrainingCandidatesData; position: CandidatePosition }> => {
    const data = await apiGet<ClassifierTrainingCandidatesData>(candidateUrl(position, filters), { signal });
    if (data.items.length > 0 || position.offset === 0) return { data, position };
    const last = { offset: lastPageOffset(data.total, position.limit), limit: position.limit };
    return {
      data: await apiGet<ClassifierTrainingCandidatesData>(candidateUrl(last, filters), { signal }),
      position: last,
    };
  };

  // 表示したページの位置を作業状態に残す（取得に成功したときだけ。失敗したら表示していたページに留まる）。
  const commitCandidatePosition = (position: CandidatePosition) => {
    if (position.limit !== candidatePaging.pageSize) candidatePaging.setPageSize(position.limit);
    candidatePaging.setPage(Math.floor(position.offset / Math.max(1, position.limit)) + 1);
  };

  /**
   * 学習候補を取り直す（絞り込みの変更は先頭のページ、ページ送り・表示件数の変更はその位置、操作の後は今のページ）。
   * 成功したらページの位置を残して true を返す。失敗したら表示していたページに留まり、「再試行」で同じ取得をやり直す。
   */
  const loadCandidates = async (
    position: CandidatePosition = currentCandidatePosition(),
    filters: CandidateFilters = {
      search: candidateSearch,
      status: candidateStatus,
      profileId: candidateProfileId,
    }
  ): Promise<boolean> => {
    retryCandidates.current = () => void loadCandidates(position, filters);
    // 条件を続けて変えたとき、遅れて返った古い条件の応答で新しい条件の一覧を上書きしない（#535）。
    const sequence = ++candidateLoadSequence.current;
    setLoading("candidates-load");
    setCandidateError("");
    setCandidateActionError("");
    try {
      const { data, position: loaded } = await fetchCandidatesInRange(position, filters);
      if (sequence !== candidateLoadSequence.current) return true;
      setCandidateAppliedFilters(filters);
      setCandidates(data);
      commitCandidatePosition(loaded);
      setCandidateHasActiveFilters(
        Boolean(filters.search.trim()) || filters.status !== "all" || Boolean(filters.profileId)
      );
      setSelectedCandidates(new Set());
      return true;
    } catch (err) {
      if (sequence !== candidateLoadSequence.current) return true;
      setCandidateError(apiErrorMessage(err, t("qcm.candidates.error.load")));
      return false;
    } finally {
      if (sequence === candidateLoadSequence.current) setLoading("");
    }
  };

  // 絞り込みを変えたら先頭のページから取り直す（成功したらページの位置も先頭に残す）。
  const applyCandidateFilters = (filters: CandidateFilters) =>
    void loadCandidates({ offset: 0, limit: candidatePaging.pageSize }, filters);

  // 画面の情報をまとめて取り直す。loading / message は呼び出し側で先に設定しておく。
  // state の更新は応答の callback の中だけで行う（effect からも呼ぶため）。
  // 「最新情報を取得」を押した場所（ヘッダー / 失敗の案内の「再試行」）。押したボタンだけを回す（#819・#2034）。
  const [loadOrigin, setLoadOrigin] = useState<"header" | "notice">("header");
  const fetchAll = (announce: boolean) => {
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    return runScopedRequest(async (signal) => {
      const [profileData, classifierData, trainingData, candidateResult] = await Promise.all([
        apiGet<ProfileSummaryPage>("/api/nl2sql/profiles/search?limit=100", {
          signal,
        }),
        apiGet<ClassifierStatusData>("/api/nl2sql/classifier", { signal }),
        apiGet<ClassifierTrainingDataData>("/api/nl2sql/classifier/training-data", {
          signal,
        }),
        fetchCandidatesInRange(currentCandidatePosition(), candidateAppliedFilters, signal),
      ]);
      if (signal.aborted || sequence !== loadSequence.current) return;
      setProfiles(profileData.items);
      setClassifierStatus(classifierData);
      setClassifierTrainingData(trainingData);
      setCandidates(candidateResult.data);
      commitCandidatePosition(candidateResult.position);
      setCandidateHasActiveFilters(
        Boolean(candidateAppliedFilters.search.trim()) ||
          candidateAppliedFilters.status !== "all" ||
          Boolean(candidateAppliedFilters.profileId)
      );

      // 失敗の案内の「再試行」は、取り直せるまで案内を残す（押すと消えて結果が分からなくなるのを防ぐ。#2034）。
      setMessage("");
      if (announce) toast.success(t("common.action.refreshed"));
    })
      .catch((err: unknown) => {
        if (isAbortError(err)) {
          return;
        }
        setMessage(apiErrorMessage(err, t("qcm.error.load")));
      })
      .finally(() => {
        if (sequence === loadSequence.current) setLoading("");
      });
  };

  const load = async (announce = false, origin: "header" | "notice" = "header") => {
    if (loading) return;
    setLoading("load");
    setLoadOrigin(origin);
    // 失敗の案内の「再試行」では案内を残し、その中に進行を出す（ヘッダーからの再読込は案内を消してから読む）。
    if (origin === "header") setMessage("");
    setActionError(null);
    setCandidateError("");
    await fetchAll(announce);
  };

  // 初回の読み込み。loading は初期値の "load"、message / candidateError は初期値の空のまま始める。
  // mount のときだけ取得する。取得の関数は毎レンダー作り直すので、最新のものを ref から呼ぶ（abortAll は固定の関数）。
  const fetchAllRef = useRef(fetchAll);
  useLayoutEffect(() => { fetchAllRef.current = fetchAll; });
  useEffect(() => {
    void fetchAllRef.current(false);
    return () => {
      loadSequence.current += 1;
      abortAll();
    };
  }, [abortAll]);

  const refreshTrainingData = async (announce = false) => {
    if (loading) return;
    setLoading("training-load");
    setActionError(null);
    try {
      setClassifierTrainingData(await apiGet<ClassifierTrainingDataData>("/api/nl2sql/classifier/training-data"));
      if (announce) toast.success(t("common.action.refreshed"));
    } catch (err) {
      showActionError("trainingData", err, t("qcm.error.trainingData"));
    } finally {
      setLoading("");
    }
  };

  const resetCandidateFilters = () => {
    const filters: CandidateFilters = { search: "", status: "all", profileId: "" };
    setCandidateSearch(filters.search);
    setCandidateStatus(filters.status);
    setCandidateProfileId(filters.profileId);
    applyCandidateFilters(filters);
  };


  const importSelectedCandidates = async () => {
    if (loading) return;
    const selected = candidates?.items.filter((item) => selectedCandidates.has(item.history_id)) ?? [];
    if (selected.length === 0) return;
    const ok = await confirm({
      title: t("qcm.candidates.confirmTitle"),
      description: t("qcm.candidates.confirmDescription", { count: selected.length }),
      confirmLabel: t("qcm.candidates.addSelected"),
      tone: "info",
    });
    if (!ok) return;
    setLoading("candidates-import");
    setCandidateActionError("");
    try {
      const data = await apiPost<ClassifierFeedbackImportData>(
        "/api/nl2sql/classifier/training-data/from-feedback",
        {
          items: selected.map((item) => ({
            history_id: item.history_id,
            profile_id: candidateProfileOverrides[item.history_id] || item.profile_id,
          })),
        }
      );
      const [statusData, trainingData] = await Promise.all([
        apiGet<ClassifierStatusData>("/api/nl2sql/classifier"),
        apiGet<ClassifierTrainingDataData>("/api/nl2sql/classifier/training-data"),
      ]);
      setClassifierStatus(statusData);
      setClassifierTrainingData(trainingData);
      await loadCandidates();
      toast.success(t("qcm.candidates.added", { count: data.imported_count }));
    } catch (err) {
      setCandidateActionError(apiErrorMessage(err, t("qcm.candidates.error.add")));
    } finally {
      setLoading("");
    }
  };

  const importClassifierTraining = async (file: File) => {
    if (loading) return;
    if (editingDirty && !(await confirmDiscard())) return;
    setTrainingFilename(file.name);
    if (classifierReplace) {
      const currentCount = classifierTrainingData?.total_examples ?? 0;
      const ok = await confirm({
        title: t("qcm.training.replaceConfirmTitle"),
        description: t("qcm.training.replaceConfirmDescription", {
          count: currentCount,
          filename: file.name,
        }),
        confirmLabel: t("qcm.training.replaceConfirmConfirm"),
        tone: "warning",
      });
      if (!ok) {
        setTrainingFilename("");
        return;
      }
    }
    setLoading("classifier-import");
    setActionError(null);
    try {
      const data = await uploadClassifierTrainingFile(file, classifierReplace);
      setEditingExampleId("");
      setClassifierStatus(await apiGet<ClassifierStatusData>("/api/nl2sql/classifier"));
      setClassifierTrainingData(await apiGet<ClassifierTrainingDataData>("/api/nl2sql/classifier/training-data"));
      // 取込の成功は Toast だけで知らせる（同じ起点の結果を面と二重に出さない。messaging.md §10.1 / §10.2）。
      toast.success(t("learning.classifier.imported", { count: data.imported_count, total: data.total_examples }));
    } catch (err) {
      showActionError("import", err, t("learning.error.classifier"));
    } finally {
      setLoading("");
    }
  };

  const trainClassifier = async () => {
    if (loading) return;
    setLoading("classifier-train");
    setActionError(null);
    const previousVersion = classifierStatus?.classifier_version ?? "";
    try {
      const data = await apiPost<ClassifierStatusData>("/api/nl2sql/classifier/train", {
        min_examples_per_category: 1,
      });
      setClassifierStatus(data);
      const versionChanged = Boolean(
        data.classifier_version && data.classifier_version !== previousVersion
      );
      if (data.ready && data.warnings.length === 0 && versionChanged) {
        toast.success(t("learning.classifier.trained"));
      } else {
        setActionError({ origin: "train", message: data.warnings.join(" ") || t("learning.error.classifier") });
      }
    } catch (err) {
      // 失敗は学習のボタンの直下だけに出す（Toast と面を重ねない。messaging.md §10.1）。
      showActionError("train", err, t("learning.error.classifier"));
    } finally {
      setLoading("");
    }
  };

  const predictClassifier = async () => {
    const text = question.trim();
    if (!text || loading) return;
    setLoading("classifier-predict");
    setActionError(null);
    setClassifierPrediction(null);
    try {
      const prediction = await apiPost<ClassifierPredictionData>("/api/nl2sql/classifier/predict", {
          question: text,
          top_k: 3,
        });
      setClassifierPrediction({
        ...prediction,
        inputQuestion: question,
        modelVersion: classifierStatus?.classifier_version ?? "",
        finishedAt: new Date().toISOString(),
      });
    } catch (err) {
      showActionError("predict", err, t("learning.error.classifier"));
    } finally {
      setLoading("");
    }
  };

  const startEditingExample = async (example: ClassifierTrainingExample) => {
    if (loading || (editingDirty && !(await confirmDiscard()))) return;
    setEditingBaseline(JSON.stringify([example.text, example.profile_id]));
    setEditingExampleId(example.id);
    setEditingText(example.text);
    setEditingProfileId(example.profile_id);
  };

  const saveTrainingExample = async () => {
    if (loading) return;
    if (!editingExampleId || !editingText.trim() || !editingProfileId) return;
    setLoading(`training-save-${editingExampleId}`);
    setActionError(null);
    try {
      await apiPatch(`/api/nl2sql/classifier/training-data/${editingExampleId}`, {
        text: editingText.trim(),
        profile_id: editingProfileId,
      });
      const [statusData, trainingData] = await Promise.all([
        apiGet<ClassifierStatusData>("/api/nl2sql/classifier"),
        apiGet<ClassifierTrainingDataData>("/api/nl2sql/classifier/training-data"),
      ]);
      setClassifierStatus(statusData);
      setClassifierTrainingData(trainingData);
      setEditingExampleId("");
      toast.success(t("qcm.training.updated"));
    } catch (err) {
      showActionError("trainingData", err, t("qcm.training.error.update"));
    } finally {
      setLoading("");
    }
  };

  const deleteTrainingExample = async (example: ClassifierTrainingExample) => {
    if (loading) return;
    const ok = await confirm({
      title: t("qcm.training.deleteTitle"),
      description: t("qcm.training.deleteDescription"),
      confirmLabel: t("qcm.training.delete"),
      tone: "danger",
    });
    if (!ok) return;
    setLoading(`training-delete-${example.id}`);
    setActionError(null);
    try {
      const trainingData = await apiDelete<ClassifierTrainingDataData>(
        `/api/nl2sql/classifier/training-data/${example.id}`
      );
      setClassifierTrainingData(trainingData);
      setClassifierStatus(await apiGet<ClassifierStatusData>("/api/nl2sql/classifier"));
      toast.success(t("qcm.training.deleted"));
    } catch (err) {
      showActionError("trainingData", err, t("qcm.training.error.delete"));
    } finally {
      setLoading("");
    }
  };

  return (
    <>
      <PageHeader wide
        title={t("nav.questionClassifierModels")}
        subtitle={t("qcm.subtitle")}
        status={
          classifierStatus ? (
            <PageHeaderStatusBadge
              testId="qcm-model-status"
              variant={
                classifierStatus.ready
                  ? classifierStatus.stale
                    ? "warning"
                    : "success"
                  : "neutral"
              }
              label={
                classifierStatus.ready
                  ? classifierStatus.stale
                    ? t("learning.classifier.stale")
                    : t("learning.classifier.ready")
                  : t("learning.classifier.notReady")
              }
            />
          ) : undefined
        }
        meta={
          classifierStatus?.updated_at
            ? `${t("qcm.metric.updatedAt")}: ${formatDateTime(classifierStatus.updated_at)}`
            : undefined
        }
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => load(true),
            loading: loading === "load" && loadOrigin === "header",
            disabled: Boolean(loading),
          },
        ]}
      />
      <PageBody wide>
        {/* 学習候補の絞り込みの読み込み（candidates-load）では無効にしない。無効にすると入力中の候補検索の欄から
            フォーカスが外れ、続けて打った文字が入らない（#984）。その間、候補の一覧は Skeleton に置き換わる。 */}
        <fieldset
          disabled={Boolean(loading) && !(loading === "candidates-load" && activeView === "candidates")}
          className="m-0 grid min-w-0 gap-4 border-0 p-0"
        >
        <PageNotice
          notice={message ? { tone: "danger", message } : null}
          action={
            message ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                loading={loading === "load" && loadOrigin === "notice"}
                disabled={Boolean(loading) && !(loading === "load" && loadOrigin === "notice")}
                onClick={() => void load(true, "notice")}
                icon={RefreshCw}
              >
                <span>{t("common.retry")}</span>
              </Button>
            ) : undefined
          }
          progress={
            message && loading === "load" && loadOrigin === "notice" ? (
              <ProcessingIndicator
                active
                label={t("common.processing.refreshing")}
                operationKey="question-learning-retry"
                placement="action"
                testId="question-learning-retry-processing"
                activityIcon="none"
              />
            ) : null
          }
        />
        {loading === "load" && !(message && loadOrigin === "notice") ? (
          <ProcessingIndicator
            active
            label={t("common.processing.refreshing")}
            operationKey="question-learning-refresh"
            placement="workspace"
            className="rounded-md border border-border bg-surface-sunken px-3 py-2"
            testId="question-learning-workspace-processing"
            activityIcon="none"
          />
        ) : null}

        <DbObjectManagementTabs
          activeView={activeView}
          tabs={[
            { id: "trainingData", label: t("qcm.tabs.trainingData"), icon: FileSpreadsheet },
            { id: "train", label: t("qcm.tabs.train"), icon: Play },
            { id: "test", label: t("qcm.tabs.test"), icon: Search },
            { id: "candidates", label: t("qcm.tabs.candidates"), icon: ListChecks },
          ]}
          idPrefix="question-classifier-models"
          ariaLabel={t("qcm.tabs.label")}
          onViewChange={setActiveView}
        />

        {activeView === "trainingData" && (
          <DbObjectManagementPanelShell
            id="question-classifier-models-panel-trainingData"
            labelledBy="question-classifier-models-tab-trainingData"
            idPrefix="question-classifier-models"
            ariaLabel={t("qcm.training.workspace")}
          >
            <TrainingDataPanel
              examples={filteredExamples}
              totalExamples={classifierTrainingData?.total_examples ?? 0}
              categories={classifierTrainingData?.categories ?? []}
              warnings={classifierTrainingData?.warnings ?? []}
              search={trainingSearch}
              paging={trainingPaging}
              filename={trainingFilename}
              replace={classifierReplace}
              loading={loading}
              importError={actionErrorFor("import")}
              listError={actionErrorFor("trainingData")}
              onSearchChange={setTrainingSearch}
              onReplaceChange={setClassifierReplace}
              onRefresh={() => void refreshTrainingData(true)}
              onImport={(file) => void importClassifierTraining(file)}
              onClearFile={() => setTrainingFilename("")}
              profiles={profiles}
              editingExampleId={editingExampleId}
              editingText={editingText}
              editingProfileId={editingProfileId}
              onStartEdit={startEditingExample}
              onEditTextChange={setEditingText}
              onEditProfileChange={setEditingProfileId}
              onCancelEdit={() => void cancelEditing()}
              onSaveEdit={() => void saveTrainingExample()}
              onDelete={(example) => void deleteTrainingExample(example)}
            />
          </DbObjectManagementPanelShell>
        )}

        {activeView === "train" && (
          <DbObjectManagementPanelShell
            id="question-classifier-models-panel-train"
            labelledBy="question-classifier-models-tab-train"
            idPrefix="question-classifier-models"
            ariaLabel={t("qcm.train.workspace")}
          >
            <ModelTrainPanel
              status={classifierStatus}
              trainingData={classifierTrainingData}
              loading={loading === "classifier-train"}
              error={actionErrorFor("train")}
              onTrain={() => void trainClassifier()}
            />
          </DbObjectManagementPanelShell>
        )}

        {activeView === "test" && (
          <DbObjectManagementPanelShell
            id="question-classifier-models-panel-test"
            labelledBy="question-classifier-models-tab-test"
            idPrefix="question-classifier-models"
            ariaLabel={t("qcm.test.workspace")}
          >
            <ModelTestPanel
              question={question}
              prediction={classifierPrediction}
              modelVersion={classifierStatus?.classifier_version ?? ""}
              loading={loading === "classifier-predict"}
              error={actionErrorFor("predict")}
              ready={Boolean(classifierStatus?.ready)}
              onQuestionChange={setQuestion}
              onPredict={() => void predictClassifier()}
            />
          </DbObjectManagementPanelShell>
        )}

        {activeView === "candidates" && (
          <DbObjectManagementPanelShell
            id="question-classifier-models-panel-candidates"
            labelledBy="question-classifier-models-tab-candidates"
            idPrefix="question-classifier-models"
            ariaLabel={t("qcm.candidates.workspace")}
          >
            <TrainingCandidatesPanel
              profiles={profiles}
              data={candidates}
              search={candidateSearch}
              status={candidateStatus}
              profileId={candidateProfileId}
              selected={selectedCandidates}
              profileOverrides={candidateProfileOverrides}
              offset={candidatePaging.offset}
              pageSize={candidatePaging.pageSize}
              loading={loading}
              error={candidateError}
              actionError={candidateActionError}
              hasActiveFilters={candidateHasActiveFilters}
              // 一覧の絞り込みは条件を変えたらすぐ適用する（検索語は SearchField の debounce・IME 対応。#535）。
              onSearchChange={(value) => {
                setCandidateSearch(value);
                applyCandidateFilters({ search: value, status: candidateStatus, profileId: candidateProfileId });
              }}
              onStatusChange={(value) => {
                setCandidateStatus(value);
                applyCandidateFilters({ search: candidateSearch, status: value, profileId: candidateProfileId });
              }}
              onProfileFilterChange={(value) => {
                setCandidateProfileId(value);
                applyCandidateFilters({ search: candidateSearch, status: candidateStatus, profileId: value });
              }}
              // 失敗した取得（絞り込み・ページ送り・表示件数の変更）を同じ条件でやり直す。
              onRetry={() => retryCandidates.current?.()}
              onResetFilters={resetCandidateFilters}
              onSelectionChange={setSelectedCandidates}
              onProfileOverrideChange={(historyId, value) =>
                setCandidateProfileOverrides((current) => ({ ...current, [historyId]: value }))
              }
              onAddSelected={() => void importSelectedCandidates()}
              onPageChange={(offset) =>
                void loadCandidates({ offset, limit: candidatePaging.pageSize }, candidateAppliedFilters)
              }
              onPageSizeChange={(limit) => void loadCandidates({ offset: 0, limit }, candidateAppliedFilters)}
            />
          </DbObjectManagementPanelShell>
        )}
        </fieldset>
      </PageBody>

    </>
  );
}

function TrainingDataPanel({
  examples,
  totalExamples,
  categories,
  warnings,
  search,
  filename,
  replace,
  loading,
  importError,
  listError,
  onSearchChange,
  onReplaceChange,
  onRefresh,
  onImport,
  onClearFile,
  profiles,
  editingExampleId,
  editingText,
  editingProfileId,
  onStartEdit,
  onEditTextChange,
  onEditProfileChange,
  onCancelEdit,
  onSaveEdit,
  onDelete,
  paging,
}: {
  /** 一覧のページ番号と表示件数（作業状態。#1891）。 */
  paging: WorkspacePaging;
  examples: ClassifierTrainingExample[];
  totalExamples: number;
  categories: string[];
  warnings: string[];
  search: string;
  filename: string;
  replace: boolean;
  loading: string;
  importError: string;
  listError: string;
  onSearchChange: (value: string) => void;
  onReplaceChange: (value: boolean) => void;
  onRefresh: () => void;
  onImport: (file: File) => void;
  onClearFile: () => void;
  profiles: ProfileSummary[];
  editingExampleId: string;
  editingText: string;
  editingProfileId: string;
  onStartEdit: (example: ClassifierTrainingExample) => void;
  onEditTextChange: (value: string) => void;
  onEditProfileChange: (value: string) => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onDelete: (example: ClassifierTrainingExample) => void;
}) {
  return (
    <div className="grid gap-4">
      <DbObjectPanelHeader
        icon={FileSpreadsheet}
        title={t("qcm.training.title")}
        action={
          <Button type="button" variant="secondary" size="sm" loading={loading === "training-load"} onClick={onRefresh} icon={RefreshCw}>
            <span>{t("qcm.training.refresh")}</span>
          </Button>
        }
      />

      {/* 検索に一致した件数は一覧のツールバーの ListCount が出す（二重にしない。#1891）。 */}
      <div className="grid gap-3 md:grid-cols-2">
        <CompactFact label={t("learning.classifier.examples")} value={formatNumber(totalExamples)} />
        <CompactFact label={t("learning.classifier.categories")} value={formatNumber(categories.length)} />
      </div>

      {/* 取込: ファイル選択（2）と置換オプション（1）を同じ行に置き、ドロップゾーンだけを行全体に伸ばさない。 */}
      <div className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] lg:items-center lg:gap-x-6">
        <FileDropzone
          label={t("qcm.training.file")}
          accept={XLSX_TEMPLATE_FILE_FORMATS.accept}
          selectedText={filename ? t("qcm.file.selected", { filename }) : ""}
          formatLabel={XLSX_TEMPLATE_FILE_FORMATS.formatLabel}
          hint={t("qcm.training.noFile")}
          replaceText={t("qcm.file.replace")}
          clearText={t("qcm.file.clear")}
          icon="spreadsheet"
          required
          disabled={loading === "classifier-import"}
          loading={loading === "classifier-import"}
          dataTestId="qcm-training-file-field"
          onFiles={([file]) => onImport(file)}
          onClear={onClearFile}
        />
        <label className="flex min-h-11 items-start gap-3 rounded-md border border-border bg-surface p-3 text-sm text-fg">
          <input
            type="checkbox"
            checked={replace}
            onChange={(event) => onReplaceChange(event.currentTarget.checked)}
            className="mt-1 h-4 w-4 rounded border-border text-accent-fg"
          />
          <span>{t("learning.classifier.replace")}</span>
        </label>
      </div>

      {/* 取込の失敗は取込の欄の直下に出す（messaging.md §10.1）。成功は Toast だけ。 */}
      {importError ? (
        <Banner severity="danger">
          {importError}
        </Banner>
      ) : null}

      <WarningsBanner warnings={warnings} />

      <div className="grid gap-3">
        {/* 一覧のツールバー: 検索欄（lg）・件数・一覧全体の操作（XLSX 出力）を共通の ListToolbar で並べる（#1891）。 */}
        <ListToolbar
          testId="qcm-training-toolbar"
          search={
            <DbManagementSearchField
              label={t("dbAdmin.search.label")}
              placeholder={t("qcm.training.searchPlaceholder")}
              value={search}
              onChange={onSearchChange}
            />
          }
          count={{
            total: totalExamples,
            matched: search.trim() ? examples.length : undefined,
            labels: listCountLabels(),
            testId: "qcm-training-count",
          }}
          actions={
            // ブラウザが直接開く URL のため、配信の前置き（`/nl2sql/` など。#1316）を付ける。
            <a
              className={buttonVariants({ variant: "secondary", size: "md" })}
              href={appPath("/api/nl2sql/classifier/training-data/export.xlsx")}
            >
              <Download size={16} aria-hidden="true" />
              <span>{t("learning.classifier.exportXlsx")}</span>
            </a>
          }
        />
        {/* 一覧の再読み込み・行の編集・削除の失敗は一覧の直上に出す（messaging.md §10.1）。 */}
        {listError ? <Banner severity="danger">{listError}</Banner> : null}
        <TrainingDataTable
          examples={examples}
          search={search}
          paging={paging}
          profiles={profiles}
          loading={loading}
          editingExampleId={editingExampleId}
          editingText={editingText}
          editingProfileId={editingProfileId}
          onStartEdit={onStartEdit}
          onEditTextChange={onEditTextChange}
          onEditProfileChange={onEditProfileChange}
          onCancelEdit={onCancelEdit}
          onSaveEdit={onSaveEdit}
          onDelete={onDelete}
        />
      </div>
    </div>
  );
}

function TrainingDataTable({
  examples,
  search,
  paging,
  profiles,
  loading,
  editingExampleId,
  editingText,
  editingProfileId,
  onStartEdit,
  onEditTextChange,
  onEditProfileChange,
  onCancelEdit,
  onSaveEdit,
  onDelete,
}: {
  examples: ClassifierTrainingExample[];
  search: string;
  paging: WorkspacePaging;
  profiles: ProfileSummary[];
  loading: string;
  editingExampleId: string;
  editingText: string;
  editingProfileId: string;
  onStartEdit: (example: ClassifierTrainingExample) => void;
  onEditTextChange: (value: string) => void;
  onEditProfileChange: (value: string) => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onDelete: (example: ClassifierTrainingExample) => void;
}) {
  const {
    page: currentPage,
    setPage,
    totalPages,
    pageItems: visibleExamples,
    range,
  } = usePagination(examples, paging.pageSize, {
    // ページ番号は作業状態に残す（#1891）。1 ページ目へ戻すのは検索語を変えたときだけ。行の編集・削除・
    // 再読み込みで一覧を取り直しても今のページに留まる（#984）。表示件数を変えると 1 ページ目へ戻る（hook が戻す）。
    page: paging.page,
    onPageChange: paging.setPage,
    resetKey: search.trim(),
  });
  const hasFilter = Boolean(search.trim());

  if (examples.length === 0) {
    return (
      <EmptyState
        title={hasFilter ? t("qcm.training.noResultsTitle") : t("qcm.training.emptyTitle")}
        hint={hasFilter ? t("qcm.training.noResultsHint") : t("qcm.training.emptyHint")}
      />
    );
  }

  return (
    <div className="grid gap-2">
      <DataTable
        columns={[
          {
            key: "profile",
            header: t("qcm.training.profile"),
            headerClassName: "w-[14rem]",
            className: "break-words align-top font-semibold",
            render: (example) =>
              editingExampleId === example.id ? (
                <SelectField
                  id={`qcm-training-edit-profile-${example.id}`}
                  label={t("qcm.training.editProfile")}
                  labelHidden
                  value={editingProfileId}
                  options={profiles
                    .filter((profile) => !profile.archived)
                    .map((profile) => ({ value: profile.id, label: profileDisplayLabel(profile) }))}
                  onValueChange={onEditProfileChange}
                />
              ) : (
                <span className="block">{profileRecordDisplayLabel(example)}</span>
              ),
          },
          {
            key: "question",
            header: t("qcm.training.question"),
            className: "break-words align-top text-sm leading-6",
            render: (example) =>
              editingExampleId === example.id ? (
                <TextareaField
                  id={`qcm-training-edit-question-${example.id}`}
                  label={t("qcm.training.editQuestion")}
                  labelHidden
                  value={editingText}
                  onChange={(event) => onEditTextChange(event.currentTarget.value)}
                  rows={3}
                />
              ) : (
                example.text
              ),
          },
          {
            key: "category",
            header: t("qcm.training.category"),
            headerClassName: "w-[10rem]",
            className: "break-words align-top",
            render: (example) => example.category,
          },
          {
            key: "source",
            header: t("qcm.training.source"),
            headerClassName: "w-[11rem]",
            className: "break-words align-top text-fg-muted",
            render: (example) => (
              <>
                <StatusBadge
                  icon={false}
                  variant={example.source_type === "feedback" ? "info" : "neutral"}
                  label={example.source_type === "feedback" ? t("qcm.training.sourceFeedback") : t("qcm.training.sourceFile")}
                />
                <span className="mt-1 block font-sans">{example.source || "-"}</span>
              </>
            ),
          },
          {
            key: "actions",
            header: t("qcm.training.actions"),
            headerClassName: "w-[12rem]",
            className: "align-top",
            render: (example) => {
              const editing = editingExampleId === example.id;
              const rowActions: EntityAction[] = editing
                ? []
                : [
                    {
                      id: "edit",
                      label: t("qcm.training.edit"),
                      icon: Pencil,
                      onSelect: () => onStartEdit(example),
                    },
                    {
                      id: "delete",
                      label: t("qcm.training.delete"),
                      icon: Trash2,
                      tone: "danger",
                      loading: loading === `training-delete-${example.id}`,
                      onSelect: () => onDelete(example),
                    },
                  ];
              return (
                <div className="flex flex-wrap justify-end gap-2">
                  {editing ? (
                    <>
                      <Button icon={Save}
                        type="button"
                        size="sm"
                        loading={loading === `training-save-${example.id}`}
                        disabled={!editingText.trim() || !editingProfileId}
                        onClick={onSaveEdit}
                      >
                        {t("qcm.training.save")}
                      </Button>
                      <Button type="button" variant="secondary" size="sm" onClick={onCancelEdit}>
                        {t("qcm.training.cancel")}
                      </Button>
                    </>
                  ) : (
                    <RowActionMenu
                      actions={rowActions}
                      ariaLabel={t("qcm.training.rowActions", { text: example.text })}
                      loading={loading === `training-delete-${example.id}`}
                      testId={`qcm-training-row-actions-${example.id}`}
                    />
                  )}
                </div>
              );
            },
          },
        ]}
        rows={visibleExamples}
        getRowKey={(example) => example.id}
        rowProps={() => ({ className: `${INFORMATION_TABLE_ROW_CLASS} hover:bg-surface-hover` })}
        testId="qcm-training-data-table"
        tableClassName="w-full min-w-[62rem] table-fixed"
        // 高さは md 未満 5 行・md 以上 8 行の実測（#403。以前は手書きの 42rem）。
        stickyHeader
        visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      />
      <Pagination
        page={currentPage}
        totalPages={totalPages}
        onPageChange={setPage}
        range={range}
        pageSize={paging.pageSize}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        onPageSizeChange={paging.setPageSize}
        labels={paginationLabels()}
        ariaLabel={t("qcm.training.pagination.label")}
        testId="qcm-training-data-pagination"
      />
    </div>
  );
}

function ModelTrainPanel({
  status,
  trainingData,
  loading,
  error,
  onTrain,
}: {
  status: ClassifierStatusData | null;
  trainingData: ClassifierTrainingDataData | null;
  loading: boolean;
  error: string;
  onTrain: () => void;
}) {
  const canTrain = (trainingData?.total_examples ?? status?.example_count ?? 0) > 0;
  return (
    <div className="grid gap-4">
      <DbObjectPanelHeader
        icon={Play}
        title={t("qcm.train.title")}
        description={t("qcm.train.hint")}
        action={
          <Button type="button" size="sm" loading={loading} disabled={!canTrain} onClick={onTrain} icon={BrainCircuit}>
            <span>{t("learning.classifier.train")}</span>
          </Button>
        }
      />
      {loading ? (
        // 学習データの embedding と分類器の学習を順に行い、件数が多いと数十秒かかる（messaging.md §3.7）。
        <ProcessingIndicator
          active
          label={t("qcm.train.progress")}
          operationKey="qcm-train"
          placement="action"
          activityIcon="none"
          testId="qcm-train-processing"
        />
      ) : null}
      {/* 学習の失敗・未完了は学習のボタンの直下に出す（messaging.md §10.1）。 */}
      {error ? <Banner severity="danger">{error}</Banner> : null}
      <DbObjectStepIndicator
        steps={[t("qcm.train.stepData"), t("qcm.train.stepEmbedding"), t("qcm.train.stepFit")]}
        activeIndex={status?.ready ? 3 : canTrain ? 1 : 0}
        ariaLabel={t("qcm.train.steps")}
        dataTestId="qcm-train-steps"
      />
      <div className="grid gap-3 lg:grid-cols-3">
        <CompactFact label={t("learning.classifier.examples")} value={formatNumber(trainingData?.total_examples ?? status?.example_count ?? 0)} />
        <CompactFact label={t("learning.classifier.model")} value={status?.embedding_model || "cohere.embed-v4.0"} />
        <CompactFact label={t("learning.classifier.dimension")} value={formatNumber(status?.vector_dimension ?? 1536)} />
      </div>
      {/* 埋め込みモデルは固定（表示だけ）。選べないことを無効の選択欄で示す。モデル名が入る幅（#613）。 */}
      <SelectField
        id="qcm-train-embedding-model"
        label={t("learning.classifier.model")}
        value={status?.embedding_model || "cohere.embed-v4.0"}
        options={[{ value: status?.embedding_model || "cohere.embed-v4.0", label: status?.embedding_model || "cohere.embed-v4.0" }]}
        onValueChange={() => undefined}
        disabled
        width="lg"
      />
      <section className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3">
        <div className="flex flex-wrap gap-2">
          <StatusBadge variant={status?.ready ? "success" : "warning"} label={status?.ready ? t("learning.classifier.ready") : t("learning.classifier.notReady")} />
          {status?.stale && <StatusBadge variant="warning" label={t("learning.classifier.stale")} />}
          <StatusBadge icon={false} variant="neutral" label={status?.persistence_mode ?? "memory"} />
          <StatusBadge icon={false} variant="neutral" label={status?.recommendation_source ?? "deterministic"} />
          {status?.classifier_version && <StatusBadge icon={false} variant="info" label={status.classifier_version} />}
        </div>
        {/* 学習の結果（ボタンの直下）が同じ警告を出しているときは、状態の欄に重ねない（同じ文は 1 か所。#2034）。 */}
        <WarningsBanner
          warnings={error && error === (status?.warnings ?? []).join(" ") ? [] : status?.warnings}
        />
        {status?.metrics && Object.keys(status.metrics).length > 0 && (
          <dl className="grid gap-2 md:grid-cols-3">
            {Object.entries(status.metrics).map(([key, value]) => (
              <CompactFact key={key} label={key} value={String(value)} />
            ))}
          </dl>
        )}
      </section>
    </div>
  );
}

function ModelTestPanel({
  question,
  prediction,
  modelVersion,
  loading,
  error,
  ready,
  onQuestionChange,
  onPredict,
}: {
  question: string;
  prediction: ClassifierPredictionSnapshot | null;
  modelVersion: string;
  loading: boolean;
  error: string;
  ready: boolean;
  onQuestionChange: (value: string) => void;
  onPredict: () => void;
}) {
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
      <section className="grid content-start gap-4">
        <DbObjectPanelHeader
          icon={Search}
          title={t("qcm.test.title")}
          description={t("qcm.test.hint")}
          action={
            <Button type="button" size="sm" loading={loading} disabled={loading || !question.trim() || !ready} onClick={onPredict} icon={Search}>
              <span>{t("learning.classifier.predict")}</span>
            </Button>
          }
        />
        {loading ? (
          <ProcessingIndicator
            active
            label={t("qcm.test.progress")}
            operationKey="qcm-predict"
            placement="action"
            activityIcon="none"
            testId="qcm-predict-processing"
          />
        ) : null}
        {/* 分類テストの失敗は実行のボタンの直下に出す（messaging.md §10.1）。 */}
        {error ? <Banner severity="danger">{error}</Banner> : null}
        {/* 分類テストは質問が空では実行できない（backend: ClassifierPredictRequest.question min_length=1）。 */}
        <TextareaField
          id="qcm-test-question"
          label={t("qcm.test.text")}
          required
          width="full"
          value={question}
          disabled={loading}
          onChange={(event) => onQuestionChange(event.currentTarget.value)}
          rows={6}
        />
      </section>
      <section className="grid content-start gap-3 rounded-md border border-border bg-surface-sunken p-4">
        <h3 className="text-sm font-semibold text-fg">{t("qcm.test.result")}</h3>
        {prediction ? (
          <>
            {(prediction.inputQuestion !== question || prediction.modelVersion !== modelVersion) && (
              <Banner severity="info" title={t("workspace.previousResult")}>
                {t("workspace.executedAt", { date: formatDateTime(prediction.finishedAt) })}
                {" — "}{t("workspace.inputChanged")}
              </Banner>
            )}
            <div className="flex flex-wrap gap-2">
              <StatusBadge icon={false} variant={prediction.recommendation_source === "classifier" ? "success" : "neutral"} label={prediction.recommendation_source} />
              <StatusBadge icon={false} variant="info" label={t("learning.classifier.confidence", { confidence: Math.round(prediction.confidence * 100) })} />
            </div>
            <CompactFact label={t("qcm.test.predictedCategory")} value={prediction.predicted_category || "-"} />
            {prediction.candidates.length > 0 && (
              <DataTable
                columns={[
                  {
                    key: "category",
                    header: t("qcm.test.category"),
                    className: "break-words font-semibold",
                    render: (candidate) => candidate.category,
                  },
                  {
                    key: "probability",
                    header: t("qcm.test.probability"),
                    headerClassName: "w-[7rem]",
                    className: "font-sans",
                    render: (candidate) => `${Math.round(candidate.score * 100)}%`,
                  },
                  {
                    key: "profile",
                    header: t("nl2sql.profile.label"),
                    headerClassName: "w-[11rem]",
                    className: "break-words text-fg-muted",
                    render: (candidate) => profileRecordDisplayLabel(candidate),
                  },
                ]}
                rows={prediction.candidates}
                getRowKey={(candidate) => candidate.category}
                rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
                tableClassName="w-full min-w-[28rem] table-fixed"
                scrollTestId="qcm-test-candidates-scroll-region"
                stickyHeader
                visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
              />
            )}
            <WarningsBanner warnings={prediction.warnings} />
          </>
        ) : (
          <EmptyState title={t("qcm.test.emptyTitle")} hint={t("qcm.test.emptyHint")} />
        )}
      </section>
    </div>
  );
}

function candidateStatusLabel(status: ClassifierTrainingCandidate["status"]) {
  return t(`qcm.candidates.status.${status}`);
}

function TrainingCandidatesPanel({
  profiles,
  data,
  search,
  status,
  profileId,
  selected,
  profileOverrides,
  offset,
  pageSize,
  loading,
  error,
  actionError,
  hasActiveFilters,
  onSearchChange,
  onStatusChange,
  onProfileFilterChange,
  onRetry,
  onResetFilters,
  onSelectionChange,
  onProfileOverrideChange,
  onAddSelected,
  onPageChange,
  onPageSizeChange,
}: {
  profiles: ProfileSummary[];
  data: ClassifierTrainingCandidatesData | null;
  search: string;
  status: string;
  profileId: string;
  selected: Set<string>;
  profileOverrides: Record<string, string>;
  /** 表示しているページの先頭の位置と 1 ページの件数（作業状態。offset 型のページング。#1891）。 */
  offset: number;
  pageSize: number;
  loading: string;
  error: string;
  actionError: string;
  hasActiveFilters: boolean;
  onSearchChange: (value: string) => void;
  onStatusChange: (value: string) => void;
  onProfileFilterChange: (value: string) => void;
  onRetry: () => void;
  onResetFilters: () => void;
  onSelectionChange: (value: Set<string>) => void;
  onProfileOverrideChange: (historyId: string, value: string) => void;
  onAddSelected: () => void;
  onPageChange: (offset: number) => void;
  onPageSizeChange: (limit: number) => void;
}) {
  const activeProfiles = profiles.filter((profile) => !profile.archived);
  const items = data?.items ?? [];
  const profileOptions = activeProfiles.map(profileSelectOption);
  const selectable = items.filter((item) =>
    item.status === "pending" ||
    (item.status === "profile_missing" && profileOverrides[item.history_id])
  );
  const allSelected = selectable.length > 0 && selectable.every((item) => selected.has(item.history_id));
  const selectedOnPageCount = selectable.filter((item) => selected.has(item.history_id)).length;
  const isLoading = loading === "candidates-load" || (loading === "load" && data === null);

  const selectPage = () => {
    const next = new Set(selected);
    selectable.forEach((item) => next.add(item.history_id));
    onSelectionChange(next);
  };

  const clearPage = () => {
    const next = new Set(selected);
    selectable.forEach((item) => next.delete(item.history_id));
    onSelectionChange(next);
  };

  return (
    <div className="grid min-w-0 gap-4">
      <DbObjectPanelHeader
        icon={ListChecks}
        title={t("qcm.candidates.title")}
        description={t("qcm.candidates.hint")}
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <CompactFact label={t("qcm.candidates.pending")} value={formatNumber(data?.pending_count ?? 0)} />
        <CompactFact label={t("qcm.candidates.addedMetric")} value={formatNumber(data?.added_count ?? 0)} />
        <CompactFact label={t("qcm.candidates.attention")} value={formatNumber(data?.attention_count ?? 0)} />
      </div>

      {/* 一覧の絞り込みは条件を変えたらすぐ適用する（「絞り込み」ボタンを置かない。#535）。検索・絞り込み・件数は
          共通の ListToolbar（検索欄は lg、件数は右の 1 か所。#1891）。 */}
      <div className="rounded-md border border-border bg-surface-sunken p-3">
        <ListToolbar
          testId="qcm-candidate-filters"
          search={
            <DbManagementSearchField
              label={t("qcm.candidates.search")}
              placeholder={t("qcm.candidates.searchPlaceholder")}
              value={search}
              onChange={onSearchChange}
            />
          }
          filters={
            <>
              {/* 状態は 7 つなので FilterChipGroup（5 つまで）ではなく FilterSelect（#1933）。 */}
              <FilterSelect
                id="qcm-candidate-status-filter"
                label={t("qcm.candidates.statusFilter")}
                value={status}
                options={CANDIDATE_STATUS_VALUES.map((value) => ({
                  value,
                  label: t(`qcm.candidates.status.${value}`),
                }))}
                onValueChange={onStatusChange}
                width="md"
              />
              <FilterSelect
                id="qcm-candidate-profile-filter"
                label={t("nl2sql.profile.label")}
                value={profileId}
                options={[
                  { value: "", label: t("qcm.candidates.allProfiles") },
                  ...profileOptions,
                ]}
                onValueChange={onProfileFilterChange}
                width="lg"
              />
            </>
          }
          activeFilterCount={(isFilterActive(status, "all") ? 1 : 0) + (isFilterActive(profileId, "") ? 1 : 0)}
          searchActive={Boolean(search.trim())}
          onClearFilters={onResetFilters}
          // 検索・状態・業務プロファイルの 3 つ。狭い置き場所では「絞り込み（N）」のシートにまとめる（#1933）。
          filtersLayout="sheet"
          labels={listToolbarLabels()}
          count={
            data
              ? {
                  total: data.unfiltered_total ?? data.total,
                  matched: hasActiveFilters ? data.total : undefined,
                  labels: listCountLabels(),
                  testId: "qcm-candidate-count",
                }
              : undefined
          }
        />
      </div>

      {isLoading ? (
        <DbManagementLoadingSkeleton
          idPrefix="qcm-candidates"
          ariaLabel={t("qcm.candidates.loading")}
          variant="list"
          rows={3}
          placement={loading === "candidates-load" ? "result" : "panel"}
          // PageHeader の「最新情報を取得」の読込はそのボタンの loading がスピナーを出す（同じ処理のスピナーは 1 つ。
          // messaging §3.7、#416）。絞り込みの読込は操作したボタンがない（#535）ので、ここでスピナーを出す。
          activityIcon={loading === "candidates-load" ? "spinner" : "none"}
        />
      ) : error ? (
        <ErrorState
          message={error}
          onRetry={onRetry}
          retryLabel={t("common.retry")}
        />
      ) : items.length > 0 ? (
        <div className="grid gap-3">
          <div
            className="flex flex-col gap-3 rounded-md border border-border bg-surface-sunken p-3 sm:flex-row sm:items-center sm:justify-between"
            data-testid="qcm-candidate-bulk-actions"
          >
            <BulkSelectionActions
              selectLabel={t("common.selection.selectVisible")}
              clearLabel={t("common.selection.clearVisible")}
              selectDisabled={selectable.length === 0 || allSelected}
              clearDisabled={selectedOnPageCount === 0}
              dataTestId="qcm-candidate-page-selection-actions"
              onSelectAll={selectPage}
              onClearAll={clearPage}
            />
            <div className="flex min-w-0 flex-col gap-2 sm:items-end">
              <FormStatus tone="danger" message={actionError} className="max-w-xl" />
              <Button
                type="button"
                size="sm"
                className="w-full sm:w-auto"
                loading={loading === "candidates-import"}
                disabled={selected.size === 0}
                onClick={onAddSelected} icon={CheckSquare}>
                <span>{t("qcm.candidates.addSelectedWithCount", { count: selected.size })}</span>
              </Button>
            </div>
          </div>

          {/* 学習候補の追加は、候補を選ぶ一覧（ListPicker）ではなく、行ごとに業務プロファイルを確定してから追加する
              作業の一覧（行の中に入力と操作がある）なので、今の形のままにする（UX 契約 page-archetypes.md
              「大量の候補から選ぶ」の画面ごとの判断。#608）。 */}
          <ul
            className="divide-y divide-border/70 rounded-md border border-border bg-surface"
            aria-label={t("qcm.candidates.listAria")}
            data-testid="qcm-candidate-list"
          >
            {items.map((item) => {
              const override = profileOverrides[item.history_id] || item.profile_id;
              const canSelect =
                item.status === "pending" ||
                (item.status === "profile_missing" && Boolean(profileOverrides[item.history_id]));
              const fallbackProfileOption =
                override && !profileOptions.some((option) => option.value === override)
                  ? [{
                      value: override,
                      label: profileNameLabel(item.profile_name),
                      description: item.profile_category || "-",
                    }]
                  : [];
              const itemProfileOptions = [
                ...(!override ? [{ value: "", label: t("qcm.candidates.selectProfile") }] : []),
                ...fallbackProfileOption,
                ...profileOptions,
              ];
              const resolvedProfile = activeProfiles.find((profile) => profile.id === override);
              const resolvedProfileName = resolvedProfile
                ? profileDisplayLabel(resolvedProfile)
                : profileRecordDisplayLabel(item);
              const conflictProfileLabels = item.conflict_profile_ids
                .map((id) => activeProfiles.find((profile) => profile.id === id))
                .map((profile) => (profile ? profileDisplayLabel(profile) : "-"));
              const selectedItem = selected.has(item.history_id);
              const statusVariant =
                item.status === "pending"
                  ? "success"
                  : item.status === "conflict" || item.status === "profile_missing" || item.status === "source_changed"
                    ? "warning"
                    : "neutral";
              return (
                <li
                  key={item.history_id}
                  data-testid="qcm-training-candidate"
                  // 淡青面（bg-accent-subtle）の上の副次テキストを 1 段深くする（DataTable の選択行と同じ。#1595・WCAG 1.4.3）。
                  data-surface-tint={selectedItem ? "accent" : undefined}
                  className={`grid min-w-0 gap-3 border-l-2 p-3 transition-colors xl:grid-cols-[minmax(20rem,1fr)_10rem_minmax(15rem,18rem)_auto] xl:items-start ${
                    selectedItem
                      ? "border-l-accent-fg bg-accent-subtle"
                      : "border-l-transparent hover:bg-surface-hover"
                  }`}
                >
                  <div className="flex min-w-0 items-start gap-1">
                    <label
                      className={`flex h-11 w-11 shrink-0 items-start justify-center rounded-md pt-1 ${
                        canSelect ? "cursor-pointer hover:bg-surface-hover" : "cursor-not-allowed opacity-50"
                      }`}
                    >
                      <input
                        type="checkbox"
                        aria-label={t("qcm.candidates.select", { question: item.question })}
                        checked={selectedItem}
                        disabled={!canSelect}
                        onChange={(event) => {
                          const next = new Set(selected);
                          if (event.currentTarget.checked) next.add(item.history_id);
                          else next.delete(item.history_id);
                          onSelectionChange(next);
                        }}
                        className="h-4 w-4 rounded border-border text-accent-fg"
                      />
                    </label>
                    <div className="min-w-0 pt-0.5">
                      <div className="min-w-0 text-sm leading-6 text-fg">
                        <QuestionText
                          value={item.question}
                          variant="select"
                          maxLines={1}
                          testId="qcm-candidate-question"
                        />
                      </div>
                      {item.feedback_comment && (
                        <p className="mt-1 break-words border-l-2 border-accent-emphasis pl-2 text-sm leading-6 text-fg [overflow-wrap:anywhere]">
                          {item.feedback_comment}
                        </p>
                      )}
                      <p className="mt-1 font-sans text-xs tabular-nums text-fg-muted">
                        {formatDateTime(item.created_at)}
                      </p>
                      {item.conflict_profile_ids.length > 0 && (
                        <p className="mt-1 break-words text-sm text-warning-fg [overflow-wrap:anywhere]">
                          {t("qcm.candidates.conflicts", { profiles: conflictProfileLabels.join(", ") })}
                        </p>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-wrap content-start gap-2 xl:pt-1">
                    <StatusBadge variant={statusVariant} label={candidateStatusLabel(item.status)} />
                  </div>

                  {(item.status === "pending" || item.status === "profile_missing") && (
                    <SelectField
                      id={`qcm-candidate-profile-${item.history_id}`}
                      label={t("qcm.candidates.confirmProfile")}
                      value={override}
                      options={itemProfileOptions}
                      onValueChange={(value) => onProfileOverrideChange(item.history_id, value)}
                      placeholder={t("qcm.candidates.selectProfile")}
                      // 確定する業務プロファイル（対象の選択）は lg（#1891 の幅の段）。
                      width="lg"
                    />
                  )}
                  {item.status !== "pending" && item.status !== "profile_missing" && (
                    <div className="grid min-w-0 content-start gap-1 xl:pt-1">
                      <p className="text-xs font-medium text-fg-muted">{t("nl2sql.profile.label")}</p>
                      <div className="min-w-0">
                        <StatusBadge icon={false} variant="info" label={resolvedProfileName} />
                      </div>
                    </div>
                  )}

                  <div className="min-w-0 xl:justify-self-end xl:pt-1">
                    <a
                      className={`${buttonVariants({ variant: "secondary", size: "sm" })} w-full sm:w-auto`}
                      href={appPath(`${APP_ROUTES.feedbackManagement}?tab=appFeedback&history_id=${encodeURIComponent(item.history_id)}`)}
                    >
                      <Link2 size={16} aria-hidden="true" />
                      <span>{t("qcm.candidates.openFeedback")}</span>
                    </a>
                  </div>
                </li>
              );
            })}
          </ul>

          <OffsetPagination
            offset={offset}
            limit={pageSize}
            total={data?.total ?? 0}
            count={items.length}
            onPageChange={(_page, offset) => onPageChange(offset)}
            onPageSizeChange={onPageSizeChange}
            labels={paginationLabels()}
            ariaLabel={t("qcm.candidates.pagination.label")}
            testId="qcm-candidate-pagination"
            className="rounded-md border border-border bg-surface-sunken p-3"
          />
        </div>
      ) : (
        <div className="rounded-md border border-border bg-surface-sunken p-3">
          <EmptyState
            title={hasActiveFilters ? t("qcm.candidates.noResultsTitle") : t("qcm.candidates.emptyTitle")}
            hint={hasActiveFilters ? t("qcm.candidates.noResultsHint") : t("qcm.candidates.emptyHint")}
            action={
              hasActiveFilters ? (
                <Button type="button" variant="secondary" size="sm" onClick={onResetFilters}>
                  {t("qcm.candidates.resetFilters")}
                </Button>
              ) : undefined
            }
          />
        </div>
      )}
    </div>
  );
}

function CompactFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-surface p-3">
      <p className="text-xs font-medium text-fg-muted">{label}</p>
      <p className="mt-1 break-words text-sm font-semibold text-fg">{value}</p>
    </div>
  );
}

async function uploadClassifierTrainingFile(file: File, replace: boolean): Promise<ClassifierImportData> {
  const form = new FormData();
  form.append("file", file);
  form.append("replace", String(replace));
  // 失敗の理由（ApiResponse の error_messages）は共通の apiPostForm が読む。以前は error / detail だけを見ていて、
  // backend の理由（列の不足など）が捨てられ「Classifier 操作に失敗しました。」だけになっていた（#984）。
  return apiPostForm<ClassifierImportData>("/api/nl2sql/classifier/training-data/import", form);
}
